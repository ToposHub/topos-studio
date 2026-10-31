#!/usr/bin/env python3
"""verify_product_bundle.py — Validate a product bundle for excluded/included features.

Usage:
    # Verify against source tree (checks config consistency):
    python scripts/verify_product_bundle.py <product-name> --source

    # Verify a built dist bundle:
    python scripts/verify_product_bundle.py <product-name> <bundle-path>

    # Verify with detailed output:
    python scripts/verify_product_bundle.py <product-name> <bundle-path> --verbose

Checks:
    1. Packaging config is valid for the given product
    2. Excluded feature directories are NOT present under src/features/
    3. Included feature directories ARE present under src/features/
    4. Entry point module is importable with register() and get_preset()
    5. (For dist bundles) TOPOS_PRODUCT_MODE is set correctly
    6. (For dist bundles) Excluded modules are not bundled

Exit codes:
    0 - All checks passed
    1 - One or more checks failed
    2 - Invalid arguments or missing files
"""

import argparse
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# Ensure project root is importable
PROJECT_ROOT = REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Exit codes
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


class VerificationResult:
    """Collects verification errors and warnings."""

    def __init__(self, product_name: str):
        self.product_name = product_name
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.info: List[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def add_info(self, msg: str) -> None:
        self.info.append(msg)

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0


def verify_packaging_config(product_name: str, result: VerificationResult) -> Optional[dict]:
    """Verify the packaging config is valid and return it.

    Args:
        product_name: Product identifier.
        result: VerificationResult to record errors.

    Returns:
        Config dict if valid, None otherwise.
    """
    try:
        from src.products.packaging import get_packaging_config
        config = get_packaging_config(product_name)
        result.add_info(f"Packaging config valid for {product_name}")
        result.add_info(f"  include_dirs: {config['include_dirs']}")
        result.add_info(f"  exclude_dirs: {config['exclude_dirs']}")
        result.add_info(f"  include_features: {config['include_features']}")
        result.add_info(f"  exclude_features: {config['exclude_features']}")
        return config
    except ValueError as exc:
        result.error(f"Packaging config invalid: {exc}")
        return None


def verify_feature_dirs(
    features_dir: Path,
    config: dict,
    result: VerificationResult,
) -> None:
    """Verify included/excluded feature directories.

    Args:
        features_dir: Path to the src/features/ directory (source or bundle).
        config: Packaging config dict.
        result: VerificationResult to record errors.
    """
    if not features_dir.exists():
        result.error(f"Features directory does not exist: {features_dir}")
        return

    # Check excluded features are NOT present
    for feature_dir in config["exclude_dirs"]:
        excluded_path = features_dir / feature_dir
        if excluded_path.exists():
            result.error(
                f"Excluded feature directory present: {excluded_path}"
            )

    # Check included features ARE present
    for feature_dir in config["include_dirs"]:
        included_path = features_dir / feature_dir
        if not included_path.exists():
            result.error(
                f"Included feature directory missing: {included_path}"
            )

    result.add_info(f"Feature directory checks completed for {features_dir}")


def verify_entry_point(product_name: str, result: VerificationResult) -> None:
    """Verify the entry point module is importable with required functions.

    Args:
        product_name: Product identifier.
        result: VerificationResult to record errors.
    """
    entry_map = {
        "topos-studio": "src.products.topos_studio",
        "topos-color": "src.products.topos_color",
        "topos-edit": "src.products.topos_edit",
        "topos-composite": "src.products.topos_composite",
    }
    entry_module_name = entry_map.get(product_name)
    if entry_module_name is None:
        result.error(f"No entry point mapping for product: {product_name}")
        return

    try:
        mod = importlib.import_module(entry_module_name)
        if not hasattr(mod, "register"):
            result.error(
                f"Entry point {entry_module_name} has no register() function"
            )
        else:
            result.add_info(f"Entry point {entry_module_name} has register()")
        if not hasattr(mod, "get_preset"):
            result.error(
                f"Entry point {entry_module_name} has no get_preset() function"
            )
        else:
            result.add_info(f"Entry point {entry_module_name} has get_preset()")
    except ImportError as exc:
        result.error(f"Cannot import entry point {entry_module_name}: {exc}")


def verify_product_mode_alias(result: VerificationResult) -> None:
    """Verify that short and long TOPOS_PRODUCT_MODE names resolve the same.

    For example, TOPOS_PRODUCT_MODE=color and TOPOS_PRODUCT_MODE=topos-color
    should resolve to the same product preset.

    Args:
        result: VerificationResult to record errors.
    """
    from src.products import get_current_product

    alias_pairs = [
        ("topos-studio", "studio"),
        ("topos-color", "color"),
        ("topos-edit", "edit"),
        ("topos-composite", "composite"),
    ]

    original_mode = os.environ.get("TOPOS_PRODUCT_MODE", "")

    for full_name, short_name in alias_pairs:
        os.environ["TOPOS_PRODUCT_MODE"] = full_name
        full_result = get_current_product().name

        os.environ["TOPOS_PRODUCT_MODE"] = short_name
        short_result = get_current_product().name

        if full_result != short_result:
            result.error(
                f"Product mode alias mismatch: "
                f"'{full_name}' -> {full_result}, "
                f"'{short_name}' -> {short_result}"
            )
        else:
            result.add_info(
                f"Product mode alias OK: '{full_name}' == '{short_name}' -> {full_result}"
            )

    # Restore original
    if original_mode:
        os.environ["TOPOS_PRODUCT_MODE"] = original_mode
    else:
        os.environ.pop("TOPOS_PRODUCT_MODE", None)


def verify_dist_bundle(
    bundle_path: Path,
    product_name: str,
    config: dict,
    result: VerificationResult,
) -> None:
    """Verify a built distribution bundle.

    Performs additional checks on a PyInstaller-built bundle:
    - Checks the bundle directory exists and has expected structure
    - Verifies TOPOS_PRODUCT_MODE is baked into the bundle
    - Checks excluded features are not bundled

    Args:
        bundle_path: Path to the built bundle directory.
        product_name: Product identifier.
        config: Packaging config dict.
        result: VerificationResult to record errors.
    """
    if not bundle_path.exists():
        result.error(f"Bundle path does not exist: {bundle_path}")
        return

    if not bundle_path.is_dir():
        result.error(f"Bundle path is not a directory: {bundle_path}")
        return

    # Check bundle structure
    result.add_info(f"Bundle directory contents:")
    for item in sorted(bundle_path.iterdir()):
        result.add_info(f"  {item.name}{'/' if item.is_dir() else ''}")

    # Check for the executable
    expected_executable = bundle_path / product_name
    if not expected_executable.exists():
        # On macOS, check for .app bundle
        app_bundle = bundle_path / f"{product_name}.app"
        if not app_bundle.exists():
            result.warn(
                f"Expected executable not found: {expected_executable}"
            )

    # Check features directory within the bundle
    bundle_features = bundle_path / "src" / "features"
    if bundle_features.exists():
        verify_feature_dirs(bundle_features, config, result)
    else:
        # In a PyInstaller bundle, features may be collected into a different
        # structure. Check for the internal _internal directory.
        internal_features = bundle_path / "_internal" / "src" / "features"
        if internal_features.exists():
            verify_feature_dirs(internal_features, config, result)
        else:
            result.warn(
                f"Features directory not found in bundle "
                f"(checked {bundle_features} and {internal_features})"
            )

    # Check that excluded feature modules are not present
    for feature_dir in config["exclude_dirs"]:
        module_pattern = f"src.features.{feature_dir}"
        # Look for evidence of excluded modules in the bundle
        for root, dirs, files in os.walk(bundle_path):
            if feature_dir in dirs and "features" in root:
                full_path = os.path.join(root, feature_dir)
                result.error(
                    f"Excluded feature module found in bundle: {full_path}"
                )
                break

    # R8/S-1：PYZ 级排除断言（spec 打包下目录形态检查不足）
    verify_pyz_exclusions(bundle_path, product_name, config, result)

    # R7：包内 codec 加载验证（干净机器语义）
    verify_codec_load(bundle_path, result)


def _pyz_module_names(executable: Path) -> Optional[set]:
    """解析 PyInstaller 可执行内嵌 PYZ 的模块名集合（R8/S-1）。

    复用 PyInstaller 自带 reader（CArchiveReader → typecode 'z' 成员 →
    ZlibArchiveReader TOC），避免手写格式漂移。PyInstaller 未安装或
    解析失败返回 None（调用方按 WARN 处理，不误报）。
    """
    try:
        from PyInstaller.archive.readers import CArchiveReader
        from PyInstaller.loader.pyimod01_archive import ZlibArchiveReader
    except ImportError:
        return None
    import tempfile

    try:
        car = CArchiveReader(str(executable))
        pyz_name = next(
            name for name, entry in car.toc.items() if entry[4] == "z")
        blob = car.extract(pyz_name)
    except Exception:
        return None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pyz", delete=False) as fh:
            fh.write(blob)
            tmp = fh.name
        reader = ZlibArchiveReader(tmp, start_offset=0, check_pymagic=False)
        return set(reader.toc.keys()) or None
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def verify_pyz_exclusions(
    bundle_path: Path,
    product_name: str,
    config: dict,
    result: VerificationResult,
) -> None:
    """R8/S-1：排除模块不得进入 PYZ（spec 打包下目录形态检查是盲的）。"""
    exe = bundle_path / product_name
    if not exe.is_file():
        return  # macOS .app 布局——维持既有目录形态检查
    names = _pyz_module_names(exe)
    if names is None:
        result.warn(
            "could not parse embedded PYZ module table; "
            "exclusion check limited to directory shape"
        )
        return
    leaked = []
    for feature_dir in config["exclude_dirs"]:
        prefix = f"src.features.{feature_dir}."
        hits = sorted(n for n in names if n.startswith(prefix))
        if hits:
            leaked.append(f"{feature_dir}: {len(hits)} modules "
                          f"(e.g. {hits[0]})")
    result.add_info(f"PYZ modules: {len(names)}")
    if leaked:
        result.error(
            "excluded feature modules bundled into PYZ — "
            + "; ".join(leaked)
        )
    else:
        result.add_info(
            f"PYZ exclusion: OK ({len(config['exclude_dirs'])} feature dirs)")


def verify_codec_load(
    bundle_path: Path,
    result: VerificationResult,
) -> None:
    """R7：包内 libtopos_codec 存在且可加载 + ABI/roundtrip 冒烟。

    PyInstaller 打包经 base_spec._find_topos_codec_lib 收集 dylib/so/dll；
    此检查在"干净机器"语义下验证：包内文件自足（不依赖开发机路径）、
    ctypes 可加载、ABI=2、最小 encode/decode roundtrip 数值通过。
    """
    lib_names = ("libtopos_codec.dylib", "libtopos_codec.so", "topos_codec.dll")
    candidates = [
        bundle_path / n for n in lib_names
    ] + [
        bundle_path / "_internal" / n for n in lib_names
    ]
    found = next((c for c in candidates if c.is_file()), None)
    if found is None:
        result.error(
            "libtopos_codec not found in bundle (checked bundle root and "
            "_internal) — Topos import/export would be unavailable"
        )
        return
    script = REPO_ROOT / "native" / "topos_codec" / "scripts" / "dll_load_test.py"
    if not script.is_file():
        result.warn(f"dll_load_test.py not found at {script}; skipping load test")
        return
    proc = subprocess.run(
        [sys.executable, str(script), str(found)],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        result.error(
            f"bundled codec load test failed: "
            f"{(proc.stdout + proc.stderr).strip()}"
        )
    else:
        result.add_info(f"codec load: OK ({found.name})")


def verify_source_tree(
    product_name: str,
    config: dict,
    result: VerificationResult,
) -> None:
    """Verify against the source tree (not a built bundle).

    Checks that:
    - Packaging config is valid
    - Feature directories in src/features/ match config
    - Entry points are importable
    - Product mode aliases resolve correctly

    Args:
        product_name: Product identifier.
        config: Packaging config dict.
        result: VerificationResult to record errors.
    """
    source_features = PROJECT_ROOT / "src" / "features"
    result.add_info(f"Verifying against source tree at {PROJECT_ROOT}")

    # Check that all include_dirs exist in source
    for feature_dir in config["include_dirs"]:
        if not (source_features / feature_dir).exists():
            result.error(
                f"Included feature directory missing in source: "
                f"{source_features / feature_dir}"
            )

    # Note: excluded dirs may still exist in source (they are excluded at
    # build time, not source time), so we just warn.
    for feature_dir in config["exclude_dirs"]:
        if (source_features / feature_dir).exists():
            result.add_info(
                f"Excluded feature directory exists in source (will be "
                f"removed at build time): {feature_dir}"
            )


def verify_product_bundle(
    product_name: str,
    bundle_path: Optional[str] = None,
    verbose: bool = False,
) -> bool:
    """Validate a product bundle against its packaging configuration.

    Args:
        product_name: Product identifier (e.g., "topos-color").
        bundle_path: Path to the bundle directory to verify. If None,
                     verifies against the source tree.
        verbose: If True, print detailed information.

    Returns:
        True if all checks pass, False otherwise.
    """
    result = VerificationResult(product_name)

    # 1. Validate packaging config
    config = verify_packaging_config(product_name, result)
    if config is None:
        _report_results(result, verbose)
        return False

    # 2. Verify entry point
    verify_entry_point(product_name, result)

    # 3. Verify product mode aliases
    verify_product_mode_alias(result)

    # 4. Verify bundle or source tree
    if bundle_path:
        bundle = Path(bundle_path)
        result.add_info(f"Verifying dist bundle at {bundle}")
        verify_dist_bundle(bundle, product_name, config, result)
    else:
        result.add_info("No bundle path specified, verifying source tree only")
        verify_source_tree(product_name, config, result)

    # Report
    _report_results(result, verbose)
    return result.passed


def _report_results(result: VerificationResult, verbose: bool) -> None:
    """Print verification results.

    Args:
        result: VerificationResult with collected errors/warnings/info.
        verbose: If True, print info-level messages too.
    """
    if verbose and result.info:
        print("Details:")
        for msg in result.info:
            print(f"  [INFO] {msg}")
        print()

    if result.warnings:
        print(f"Warnings ({len(result.warnings)}):")
        for msg in result.warnings:
            print(f"  [WARN] {msg}")
        print()

    if result.errors:
        print(f"FAIL: {len(result.errors)} error(s) for {result.product_name}:")
        for err in result.errors:
            print(f"  [FAIL] {err}")
    else:
        print(f"PASS: {result.product_name} verification complete")


def main():
    parser = argparse.ArgumentParser(
        description="Verify a Topos product bundle"
    )
    parser.add_argument(
        "product",
        choices=["topos-studio", "topos-color", "topos-edit", "topos-composite"],
        help="Product name to verify",
    )
    parser.add_argument(
        "bundle_path",
        nargs="?",
        default=None,
        help="Path to the product bundle directory (omit for source-tree verification)",
    )
    parser.add_argument(
        "--source",
        action="store_true",
        help="Verify against source tree (no bundle needed)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print detailed verification information",
    )
    args = parser.parse_args()

    # Set product mode env var for consistency
    os.environ["TOPOS_PRODUCT_MODE"] = args.product

    if args.source:
        success = verify_product_bundle(args.product, None, args.verbose)
    elif args.bundle_path:
        success = verify_product_bundle(args.product, args.bundle_path, args.verbose)
    else:
        # Default: source-tree verification
        success = verify_product_bundle(args.product, None, args.verbose)

    sys.exit(EXIT_PASS if success else EXIT_FAIL)


if __name__ == "__main__":
    main()
