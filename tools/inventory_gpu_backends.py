#!/usr/bin/env python3
"""P0-01: GPU backend & path inventory (reproducible, stdlib only).

Re-measures the plan's key structural facts (docs/
metal_backend_migration_and_cross_platform_gpu_plan.md §2.1–§2.5) on the
current checkout and emits a paired ``.json`` + ``.md`` archive:

    tests/media/results/gpu_backend_inventory.json
    tests/media/results/gpu_backend_inventory.md

Usage:
    .venv/bin/python tools/inventory_gpu_backends.py [--output-dir DIR]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
from pathlib import Path
from typing import Iterable, Pattern

REPO = Path(__file__).resolve().parent.parent

GL_IMPORT_RE = re.compile(
    r"^\s*(import moderngl\b|from moderngl\b|from OpenGL[.\s]|import OpenGL\b)", re.MULTILINE
)
GLO_RE = re.compile(r"\.glo\b")
QWIDGET_REF_RE = re.compile(r"QOpenGLWidget")
QWIDGET_SUBCLASS_RE = re.compile(
    r"^class\s+\w+\s*\(\s*[\w.]*QOpenGLWidget\s*\)", re.MULTILINE
)
VIEWPORT_BASES = (
    "GLCanvasViewport|CompositeViewer|ProgramViewer|SourceViewer|GpuScopeWidget"
)
QWIDGET_SUBCLASS2_RE = re.compile(
    rf"^class\s+\w+\s*\(\s*(?:{VIEWPORT_BASES})\s*\)", re.MULTILINE
)
VERSION_RE = re.compile(r"#version\s+([0-9]+(?:\s+core)?(?:\s+fragment\s+shader)?(?:\s+es)?)")
PROGRAM_RE = re.compile(r"\.program\(")
USAMPLER_RE = re.compile(r"\busampler2D\b")
IMAGESTORE_RE = re.compile(r"\bimageStore\b")
COMPUTE_CONST_RE = re.compile(r"^(_[A-Z0-9_]+_COMPUTE[A-Z0-9_]*)\s*=", re.MULTILINE)
LOCAL_SIZE_RE = re.compile(r"\blocal_size_x\b")
METAL_RE = re.compile(
    r"MTLDevice|MTLTexture\b|MTLCommandQueue|CVMetalTextureCache|MetalPerformanceShaders"
)
CDLL_NONE_RE = re.compile(r"ctypes\.CDLL\(None\)")
FROM_OPENGL_GL_RE = re.compile(r"^\s*from OpenGL import GL\b", re.MULTILINE)
SHARE_CTX_RE = re.compile(r"AA_ShareOpenGLContexts")


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:
        return "unknown"


def _py_files(*roots: Path) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            out.append(p)
    return out


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _hit_map(files: Iterable[Path], pattern: Pattern[str]) -> dict[str, int]:
    hits: dict[str, int] = {}
    for p in files:
        n = len(pattern.findall(_read(p)))
        if n:
            hits[str(p.relative_to(REPO))] = n
    return dict(sorted(hits.items(), key=lambda kv: -kv[1]))


def _count_lines(paths: Iterable[Path]) -> tuple[int, int]:
    files = list(paths)
    return len(files), sum(_read(p).count("\n") + 1 for p in files)


def _symbol_line(path: Path, symbol: str) -> str:
    for i, line in enumerate(_read(path).splitlines(), 1):
        if symbol in line:
            return f"{path.relative_to(REPO)}:{i}"
    return f"{path.relative_to(REPO)}:??"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO / "tests" / "media" / "results",
    )
    args = parser.parse_args()

    src = REPO / "src"
    ext = REPO / "external"
    nat = REPO / "native"

    src_py = _py_files(src)
    ext_py = _py_files(ext)
    nat_py = _py_files(nat)

    # --- OpenGL/ModernGL footprint -------------------------------------
    gl_files = {p for p in src_py + ext_py + nat_py if GL_IMPORT_RE.search(_read(p))}
    gl_lines = sum(_read(p).count("\n") + 1 for p in gl_files)
    glo_hits = _hit_map(src_py, GLO_RE)
    qw_refs = _hit_map(src_py, QWIDGET_REF_RE)
    qw_subclasses = sorted(
        f"{p.relative_to(REPO)}:{m.group(0)}"
        for p in src_py
        for m in [QWIDGET_SUBCLASS_RE.search(_read(p))]
        if m
    )
    qw_subclasses2 = sorted(
        f"{p.relative_to(REPO)}:{m.group(0)}"
        for p in src_py
        for m in [QWIDGET_SUBCLASS2_RE.search(_read(p))]
        if m
    )
    cdll_hits = _hit_map(src_py, CDLL_NONE_RE)
    from_gl_gl_hits = _hit_map(src_py, FROM_OPENGL_GL_RE)
    share_ctx_hits = _hit_map(src_py, SHARE_CTX_RE)

    # --- shader corpus ---------------------------------------------------
    glsl_files = [p for p in src.rglob("*.glsl")]
    glsl_n, glsl_lines = _count_lines(glsl_files)
    version_buckets: dict[str, int] = {}
    version_files: set[str] = set()
    for p in src_py + glsl_files:
        text = _read(p)
        for m in VERSION_RE.finditer(text):
            key = " ".join(m.group(1).split())
            version_buckets[key] = version_buckets.get(key, 0) + 1
            version_files.add(str(p.relative_to(REPO)))
    program_hits = _hit_map(src_py, PROGRAM_RE)
    usampler_hits = _hit_map(src_py, USAMPLER_RE)
    imagestore_hits = _hit_map(src_py, IMAGESTORE_RE)

    composite_shaders = src / "features" / "composite" / "core" / "shaders.py"
    csm = src / "features" / "composite" / "render" / "compute_shader_manager.py"
    hist = src / "features" / "composite" / "evaluators" / "histogram_manager.py"
    compute_consts = sorted(set(COMPUTE_CONST_RE.findall(_read(composite_shaders))))
    local_size_in_shaders = len(LOCAL_SIZE_RE.findall(_read(composite_shaders)))
    csm_text = _read(csm)
    registered: list[str] = []
    in_block = False
    for line in csm_text.splitlines():
        if "_COMPUTE_SOURCES" in line and "=" in line:
            in_block = True
            continue
        if in_block:
            if line.strip().startswith("}"):
                break
            m = re.search(r'"(\w+)"\s*:\s*(_[A-Z0-9_]+)', line)
            if m:
                registered.append(m.group(1))
    local_size_probes = sum(
        1 for f in (csm, hist) if LOCAL_SIZE_X_STUB_RE.search(_read(f))
    )
    referenced_consts = [
        name
        for name in compute_consts
        if len(re.findall(rf"\b{name}\b", _read(composite_shaders))) > 1
        or _referenced_elsewhere(name, composite_shaders, src_py)
    ]

    # --- Metal footprint (expect zero) -----------------------------------
    metal_hits = _hit_map(src_py + ext_py + nat_py, METAL_RE)
    metal_files = [p for p in list(src.rglob("*.metal")) + list(ext.rglob("*.metal"))]

    # --- existing abstractions -------------------------------------------
    def _find_file(rel: str) -> str:
        p = REPO / rel
        return rel if p.exists() else f"MISSING:{rel}"

    abstractions = {
        "GpuPresentFrame": _find_file("src/shared/gpu/present_frame.py"),
        "TextureLease": _find_file("src/shared/gpu/present_frame.py"),
        "OutputSlotPool": _find_file("src/shared/gpu/present_frame.py"),
        "EffectSpec": _find_file("src/features/shared/effects/effect_spec.py"),
        "EffectCapability": _find_file("src/features/shared/effects/effect_capability.py"),
        "DecoderBackend": _find_file(
            "external/file_explorer/plugins/video_pro/base_player.py"
        ),
        "playback_should_prefer_ffmpeg": _find_file(
            "src/shared/media/macos_vt/hardware_capability.py"
        ),
        "gpu_frame_gpu_api_literal": _find_file("src/shared/media/gpu_frame.py"),
    }

    # --- renderer inventory ----------------------------------------------
    key_files = [
        "src/features/color/core/async_renderer.py",
        "src/shared/video/video_async_renderer.py",
        "src/features/edit/core/edit_gpu_preview_renderer.py",
        "src/features/color/core/shader_compiler.py",
        "src/features/color/core/shader_nodes.py",
        "src/features/color/core/video_denoise_shaders.py",
        "src/shared/video/yuv_gpu_upload.py",
        "src/shared/gpu/gpu_scope_engine.py",
        "src/shared/gpu/present_frame.py",
        "src/shared/gpu/gl_backend.py",
        "src/shared/gpu/gl_context_hub.py",
        "src/features/composite/core/shaders.py",
        "src/features/composite/render/compute_shader_manager.py",
        "src/features/composite/evaluators/histogram_manager.py",
        "src/features/composite/ui/composite_viewport.py",
        "src/features/edit/ui/program_viewer.py",
        "src/features/edit/ui/source_viewer.py",
        "src/features/color/ui/gl_viewport.py",
        "src/shared/ui/widgets/gpu_scope_widget.py",
        "src/shared/ui/widgets/cie_scope_widget.py",
    ]
    renderer_lines = {
        rel: _read(REPO / rel).count("\n") + 1 for rel in key_files if (REPO / rel).exists()
    }

    # --- platform decode baselines ---------------------------------------
    macos_vt = list((src / "shared" / "media" / "macos_vt").glob("*.py"))
    win_hw = list((src / "shared" / "media" / "windows_hw").glob("*.py"))
    macos_n, macos_lines = _count_lines(macos_vt)
    win_n, win_lines = _count_lines(win_hw)

    inventory = {
        "schema": "topos.gpu_backend_inventory.v1",
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "git_sha": _git_sha(),
        "opengl_footprint": {
            "gl_import_files": len(gl_files),
            "gl_import_lines": gl_lines,
            "by_tree": {
                "src": len([p for p in gl_files if src in p.parents or p == src]),
                "external": len([p for p in gl_files if ext in p.parents]),
                "native": len([p for p in gl_files if nat in p.parents]),
            },
            "glo_handoff_sites": sum(glo_hits.values()),
            "glo_handoff_files": len(glo_hits),
            "glo_top_files": dict(list(glo_hits.items())[:15]),
            "qopenglwidget_refs": sum(qw_refs.values()),
            "qopenglwidget_files": len(qw_refs),
            "qopenglwidget_subclasses": qw_subclasses,
            "qopenglwidget_subclass2": qw_subclasses2,
            "ctypes_cdll_none_sites": sum(cdll_hits.values()),
            "ctypes_cdll_none_files": cdll_hits,
            "from_opengl_import_gl_files": from_gl_gl_hits,
            "aa_share_opengl_contexts": share_ctx_hits,
        },
        "shader_corpus": {
            "glsl_files": glsl_n,
            "glsl_lines": glsl_lines,
            "version_directives": version_buckets,
            "version_files": len(version_files),
            "program_compile_sites": sum(program_hits.values()),
            "usampler2d_sites": sum(usampler_hits.values()),
            "imagestore_sites": sum(imagestore_hits.values()),
            "compute_constants_composite": compute_consts,
            "compute_constants_referenced": referenced_consts,
            "compute_registered_in_manager": registered,
            "local_size_x_in_composite_shaders": local_size_in_shaders,
            "local_size_probe_stubs": local_size_probes,
        },
        "metal_footprint": {
            "py_symbol_hits": metal_hits,
            "metal_files": [str(p.relative_to(REPO)) for p in metal_files],
        },
        "existing_abstractions": abstractions,
        "key_file_lines": renderer_lines,
        "platform_decode_baseline": {
            "macos_vt_files": macos_n,
            "macos_vt_lines": macos_lines,
            "windows_hw_files": win_n,
            "windows_hw_lines": win_lines,
        },
    }

    out_dir: Path = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "gpu_backend_inventory.json"
    md_path = out_dir / "gpu_backend_inventory.md"
    json_path.write_text(json.dumps(inventory, indent=2, ensure_ascii=False) + "\n")
    md_path.write_text(_render_md(inventory))

    print(f"wrote {json_path}")
    print(f"wrote {md_path}")


LOCAL_SIZE_X_STUB_RE = re.compile(r"local_size_x\s*=\s*1")


def _referenced_elsewhere(name: str, self_path: Path, files: list[Path]) -> bool:
    pat = re.compile(rf"\b{name}\b")
    for p in files:
        if p == self_path:
            continue
        if pat.search(_read(p)):
            return True
    return False


def _render_md(inv: dict) -> str:
    gl = inv["opengl_footprint"]
    sc = inv["shader_corpus"]
    pl = inv["platform_decode_baseline"]
    lines = [
        "# GPU 后端清点（P0-01，自动生成）",
        "",
        f"> 由 `tools/inventory_gpu_backends.py` 生成于 {inv['generated_at']}，"
        f"commit `{inv['git_sha']}`。数字为当前 checkout 实测；行号以符号名为准。",
        "",
        "## OpenGL/ModernGL 足迹",
        "",
        "| 指标 | 值 |",
        "|---|---:|",
        f"| 触碰 GL 的 .py 文件（src+external+native） | {gl['gl_import_files']} |",
        f"| 其中行数 | {gl['gl_import_lines']} |",
        f"| `.glo` 交接点 / 文件数 | {gl['glo_handoff_sites']} / {gl['glo_handoff_files']} |",
        f"| `QOpenGLWidget` 引用 / 文件数 | {gl['qopenglwidget_refs']} / {gl['qopenglwidget_files']} |",
        f"| `ctypes.CDLL(None)` 直连 GL | {gl['ctypes_cdll_none_sites']} 处 |",
        f"| `from OpenGL import GL` | {sum(gl['from_opengl_import_gl_files'].values())} 处 / {len(gl['from_opengl_import_gl_files'])} 文件 |",
        "",
        "QOpenGLWidget 子类：",
        "",
    ]
    lines += [f"- `{s}`" for s in gl["qopenglwidget_subclasses"]]
    if gl.get("qopenglwidget_subclass2"):
        lines += ["二级子类："] + [f"- `{s}`" for s in gl["qopenglwidget_subclass2"]]
    lines += [
        "",
        f"`AA_ShareOpenGLContexts`：{'、'.join(f'`{k}`' for k in gl['aa_share_opengl_contexts'])}",
        "",
        "`.glo` 交接最多的文件（前 15）：",
        "",
        "| 文件 | 处数 |",
        "|---|---:|",
    ]
    lines += [f"| `{k}` | {v} |" for k, v in gl["glo_top_files"].items()]
    lines += [
        "",
        "## Shader 语料",
        "",
        "| 指标 | 值 |",
        "|---|---:|",
        f"| `.glsl` 文件 / 行数 | {sc['glsl_files']} / {sc['glsl_lines']} |",
        f"| `#version` 指令总数（文件数） | {sum(sc['version_directives'].values())}（{sc['version_files']}） |",
        f"| `.program(` 编译点 | {sc['program_compile_sites']} |",
        f"| `usampler2D` | {sc['usampler2d_sites']} |",
        f"| `imageStore` | {sc['imagestore_sites']} |",
        f"| composite compute 常量 | {len(sc['compute_constants_composite'])}（被引用 {len(sc['compute_constants_referenced'])}） |",
        f"| manager 已注册 compute | {len(sc['compute_registered_in_manager'])} |",
        "",
        "`#version` 分布："
        + "、".join(f"`{k}`×{v}" for k, v in sorted(sc["version_directives"].items()))
        + "",
        "",
        "## Metal 足迹（预期为 0）",
        "",
    ]
    if inv["metal_footprint"]["py_symbol_hits"] or inv["metal_footprint"]["metal_files"]:
        lines += [f"- 符号命中：`{inv['metal_footprint']['py_symbol_hits']}`"]
    else:
        lines += ["- 无任何 Metal 符号/文件（Phase 1 是真正起点）"]
    lines += [
        "",
        "## 平台解码基线不对称",
        "",
        "| 平台 | 文件 | 行数 |",
        "|---|---:|---:|",
        f"| macOS `macos_vt/` | {pl['macos_vt_files']} | {pl['macos_vt_lines']} |",
        f"| Windows `windows_hw/` | {pl['windows_hw_files']} | {pl['windows_hw_lines']} |",
        f"| Metal | 0 | 0 |",
        "",
        "## 既有抽象（复用，勿重建）",
        "",
    ]
    lines += [f"- `{k}` → `{v}`" for k, v in inv["existing_abstractions"].items()]
    lines += [
        "",
        "## 关键文件行数",
        "",
        "| 文件 | 行数 |",
        "|---|---:|",
    ]
    lines += [f"| `{k}` | {v} |" for k, v in sorted(inv["key_file_lines"].items(), key=lambda kv: -kv[1])]
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
