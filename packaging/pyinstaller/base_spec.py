"""Base PyInstaller spec template shared by all Topos product variants.

This module provides a helper function that generates a PyInstaller Analysis
configured for a given product.  Each product spec file imports
``generate_analysis`` and customises it further if needed.

Usage in a product spec file (e.g. ``topos_color_spec.py``)::

    from packaging.pyinstaller.base_spec import generate_analysis

    product_name = "topos-color"
    entry_module = "apps.topos_color.main"
    entry_function = "main"
    exclude_feature_dirs = ["edit", "composite"]

    a = generate_analysis(
        product_name=product_name,
        entry_module=entry_module,
        exclude_feature_dirs=exclude_feature_dirs,
    )
"""

import os
import sys
from pathlib import Path
from typing import List, Optional

# Project root (three levels up from this file)
PROJECT_ROOT = str(Path(__file__).resolve().parents[2])


def get_exclude_modules(exclude_feature_dirs: List[str]) -> List[str]:
    """Return PyInstaller ``--exclude-module`` arguments for excluded features.

    For each excluded feature directory we exclude the matching Python package
    so PyInstaller does not bundle it.

    Args:
        exclude_feature_dirs: Feature directory names to exclude (e.g. ``["edit", "composite"]``).

    Returns:
        List of dotted module paths to pass as ``excludes`` to Analysis.
    """
    base = "src.features"
    return [f"{base}.{d}" for d in exclude_feature_dirs]



def _collect_pyav_dlls() -> List[tuple]:
    """收集 PyAV 包目录内的 FFmpeg/av 运行库 DLL（Windows 为主）。

    返回 PyInstaller ``binaries`` 项；av 包不存在（裁剪构建）时返回空表。
    系统 DLL（d3d11.dll/dxgi.dll 等）永不打包——由目标机系统提供。
    """
    try:
        import av as _av  # noqa: F401 — 仅定位包目录
        import importlib.util

        spec = importlib.util.find_spec("av")
        if spec is None or not spec.submodule_search_locations:
            return []
        av_dir = list(spec.submodule_search_locations)[0]
    except Exception:
        return []
    out: List[tuple] = []
    for dll in Path(av_dir).glob("*.dll"):
        out.append((str(dll), "av"))
    # PyAV ≥14 wheel（delvewheel 重定位）：FFmpeg 运行库在 av.libs/，
    # _core.pyd 运行时按同目录相对布局查找，须保持该层级。
    av_libs = Path(av_dir).parent / "av.libs"
    if av_libs.is_dir():
        for dll in av_libs.glob("*.dll"):
            out.append((str(dll), "av.libs"))
    return out

def _collect_cuda_bindings() -> tuple:
    """W06（Windows 计划项）：可选收集 cuda-bindings（cuda 驻留解码）。

    构建机安装了 ``cuda-windows`` extra（``cuda.bindings`` 可发现）时：
    - 显式登记动态导入（``cuda.bindings.driver`` 等在产品代码里均为
      函数内延迟导入，静态分析可能漏）；
    - 收集 wheel 内的本地二进制扩展（``cuda/bindings/**/*.pyd|.so``，
      含 _internal 子模块），按原目录层级放置。

    未安装 extra 时**什么都不打包**：冻结产物运行探针
    ``probe_cuda_resident_capability()`` 如实报
    ``cuda_bindings_unavailable``，cuda 驻留路径 fail-closed 回退下载
    planar/CPU——可选能力缺席不阻塞安装，也不冒充可用。
    ``nvcuda.dll`` 由目标机 NVIDIA 驱动提供，永不打包。

    Returns:
        (hidden_imports, binaries) 二元组。
    """
    import importlib.util

    if importlib.util.find_spec("cuda") is None:
        return [], []
    hidden: List[str] = ["cuda", "cuda.bindings"]
    for mod in ("cuda.bindings.driver", "cuda.bindings.runtime"):
        if importlib.util.find_spec(mod) is not None:
            hidden.append(mod)
    binaries: List[tuple] = []
    try:
        spec = importlib.util.find_spec("cuda.bindings")
        if spec is None or not spec.submodule_search_locations:
            return hidden, binaries
        bindings_dir = Path(list(spec.submodule_search_locations)[0])
        cuda_root = bindings_dir.parent  # site-packages/cuda/
        for ext_file in cuda_root.rglob("*"):
            if ext_file.suffix not in (".pyd", ".so"):
                continue
            rel = ext_file.relative_to(cuda_root.parent)
            binaries.append((str(ext_file), str(rel.parent)))
    except Exception:
        # 收集失败不阻塞构建：产物按"无 extra"行为运行（探针回退）
        return hidden, binaries
    return hidden, binaries


def generate_analysis(
    product_name: str,
    entry_module: str,
    exclude_feature_dirs: Optional[List[str]] = None,
    extra_excludes: Optional[List[str]] = None,
    extra_hidden_imports: Optional[List[str]] = None,
):
    """Create a PyInstaller Analysis for a Topos product.

    This function is intended to be called from a product-specific spec file.
    It returns the Analysis object that PyInstaller consumes.

    **Important**: PyInstaller must be installed for this to work at build time.
    If PyInstaller is not installed, calling this function will raise an
    ImportError with a helpful message.

    Args:
        product_name: Product identifier (e.g. ``"topos-color"``).
        entry_module: Dotted path to the entry module (e.g. ``"apps.topos_color.main"``).
        exclude_feature_dirs: Feature directories to exclude from the bundle.
        extra_excludes: Additional module names to exclude.
        extra_hidden_imports: Additional hidden imports to include.

    Returns:
        PyInstaller Analysis object.

    Raises:
        ImportError: If PyInstaller is not installed.
    """
    try:
        from PyInstaller.building.build_main import Analysis
    except ImportError:
        raise ImportError(
            "PyInstaller is required for building. "
            "Install it with: pip install pyinstaller"
        )

    # R7/R8-C3：未显式给排除表时从产品预设推导（单一真相，防 spec 硬编码
    # 漂移）。注意必须在 None 归一化之前判定——`or []` 会把 None 变成空表
    # 使推导分支成为死代码（R8 独立复审实证：698 个排除模块进包）。
    if exclude_feature_dirs is None:
        exclude_feature_dirs = get_preset_exclude_dirs(product_name)
    extra_excludes = extra_excludes or []
    extra_hidden_imports = extra_hidden_imports or []

    # Build the list of excluded modules
    excludes = get_exclude_modules(exclude_feature_dirs) + extra_excludes

    # Common hidden imports that PyInstaller may miss
    hidden_imports = [
        entry_module,
        "src.products",
        "src.products.packaging",
        "src.shared.feature_flags",
        "src.shared.actions",
        "src.shared.preferences",
        "src.shared.ui.shortcut_manager",
        "src.app.feature_registry",
        "src.app.app_context",
        "src.app.main_window",
        "src.main",
    ] + extra_hidden_imports

    # Set the product mode env var so the built app knows which product it is.
    # We do this at the Analysis level via runtime hooks.
    os.environ["TOPOS_PRODUCT_MODE"] = product_name

    # Path to the entry module's main.py
    entry_path = os.path.join(PROJECT_ROOT, entry_module.replace(".", os.sep) + ".py")

    datas = [
        # Include shared i18n locale files
        (
            os.path.join(PROJECT_ROOT, "src", "shared", "i18n", "locales"),
            "src/shared/i18n/locales",
        ),
    ]
    # Feature modules load their translations from <module>/locales at
    # runtime (register_i18n → I18N.load_module_translations); without these
    # entries every non-English feature string falls back to English in
    # packaged builds.
    for _feature in ("ai_workflow", "color", "composite", "deliver",
                     "edit", "teaching_animation"):
        _feature_locales = os.path.join(
            PROJECT_ROOT, "src", "features", _feature, "locales")
        if os.path.isdir(_feature_locales):
            datas.append((
                _feature_locales,
                f"src/features/{_feature}/locales",
            ))
    # Feature SUBpackages can carry their own locales too (G2: edit/insights
    # has its own tables with keys the edit module tables do not contain).
    # The loop above only sees top-level feature dirs, so sweep second-level
    # locales dirs explicitly.
    for _sub_locales in sorted(
        Path(PROJECT_ROOT).glob(os.path.join("src", "features", "*", "*", "locales"))
    ):
        if _sub_locales.is_dir():
            datas.append((
                str(_sub_locales),
                _sub_locales.relative_to(PROJECT_ROOT).as_posix(),
            ))
    datas += [
        # Node usage help documents (per-node bilingual Markdown).
        (
            os.path.join(PROJECT_ROOT, "src", "shared", "node_help", "docs"),
            "src/shared/node_help/docs",
        ),
        # Global checkbox checkmark icon used by the application stylesheet.
        (
            os.path.join(PROJECT_ROOT, "src", "shared", "assets", "icons", "checkmark.svg"),
            "src/shared/assets/icons",
        ),
        # Factory default layout — the only source "Reset to Default" reads.
        (
            os.path.join(PROJECT_ROOT, "default_layout.json"),
            ".",
        ),
    ]

    # Filter datas to only include files/directories that exist.
    datas = [(src, dst) for src, dst in datas if os.path.exists(src)]

    # libtopos_codec：Topos 导入/导出/代理的 native 核心（阶段 10 分发）。
    # 绑定层（topos_binding.candidate_library_paths）会在 sys._MEIPASS 根查找，
    # 因此打进 '.'。优先取 universal SDK 构建，回退各配置构建目录。
    binaries = []
    codec_lib = _find_topos_codec_lib()
    if codec_lib is not None:
        binaries.append((codec_lib, "."))
    else:
        print(
            "WARNING: libtopos_codec not found — Topos 导出/解码功能将不可用。\n"
            "         先运行: bash native/topos_codec/run_tests.sh 或 "
            "scripts/build_sdk.sh",
            file=sys.stderr,
        )

    # W7-03（Windows 计划项）：PyAV 及其捆绑的 FFmpeg DLL 显式收集。
    # PyAV 的 wheel 把 av-*.dll 与 FFmpeg 运行库（avcodec/avformat/...）
    # 放在 av 包目录内；缺 hook 或 hook 未覆盖时 hwaccel 硬解/硬编在打包
    # 产物里静默缺失——回退 CPU 而不留诊断（违反显式诊断约束）。
    binaries += _collect_pyav_dlls()

    # W06（Windows 计划项）：可选收集 cuda-bindings（cuda 驻留解码 extra）。
    cuda_hidden, cuda_binaries = _collect_cuda_bindings()
    hidden_imports += cuda_hidden
    binaries += cuda_binaries

    analysis = Analysis(
        [entry_path],
        pathex=[PROJECT_ROOT],
        binaries=binaries,
        datas=datas,
        hiddenimports=hidden_imports,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=excludes,
        noarchive=False,
    )

    return analysis


def _find_topos_codec_lib() -> Optional[str]:
    """Locate the built libtopos_codec shared library for bundling.

    Search order: env override → dist SDK → release/perf/debug build dirs.
    Returns an absolute path string, or ``None`` when nothing was built yet.
    """
    env = os.environ.get("TOPOS_CODEC_LIB")
    if env and os.path.exists(env):
        return env
    native = os.path.join(PROJECT_ROOT, "native", "topos_codec")
    candidates: List[str] = []
    dist = os.path.join(native, "dist")
    if os.path.isdir(dist):
        for entry in sorted(os.listdir(dist), reverse=True):
            lib = os.path.join(dist, entry, "lib")
            if os.path.isdir(lib):
                for name in ("libtopos_codec.dylib", "libtopos_codec.so", "topos_codec.dll"):
                    candidates.append(os.path.join(lib, name))
    for cfg in ("release", "perf", "debug"):
        for name in ("libtopos_codec.dylib", "libtopos_codec.so", "topos_codec.dll"):
            candidates.append(os.path.join(native, "build", cfg, name))
    for cand in candidates:
        if os.path.exists(cand):
            return cand
    return None


# ---------------------------------------------------------------------------
# Product spec template helper
# ---------------------------------------------------------------------------

# Mapping of product name to entry module
PRODUCT_ENTRY_MAP = {
    "topos-studio": "apps.topos_studio.main",
    "topos-color": "apps.topos_color.main",
    "topos-edit": "apps.topos_edit.main",
    "topos-composite": "apps.topos_composite.main",
}


def get_preset_exclude_dirs(product_name: str) -> List[str]:
    """产品预设的排除特征目录（src.products.packaging 单一真相源）。

    R7：spec 内不再硬编码排除表——曾与产品配置漂移（topos-color 漏排
    teaching_animation）。未知产品名抛 ValueError（与 entry 查找同语义）。
    """
    from src.products.packaging import PRODUCT_PRESETS

    preset = PRODUCT_PRESETS.get(product_name)
    if preset is None:
        raise ValueError(f"Unknown product name: {product_name!r}")
    return sorted(preset.disabled_features)


def get_product_entry_module(product_name: str) -> str:
    """Return the entry module dotted path for a product.

    Args:
        product_name: Product identifier (e.g. ``"topos-color"``).

    Returns:
        Dotted module path string.

    Raises:
        ValueError: If the product name is unknown.
    """
    if product_name not in PRODUCT_ENTRY_MAP:
        raise ValueError(
            f"Unknown product: {product_name!r}. "
            f"Available: {list(PRODUCT_ENTRY_MAP.keys())}"
        )
    return PRODUCT_ENTRY_MAP[product_name]
