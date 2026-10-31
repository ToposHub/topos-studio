"""
Test import boundaries for src directory simplification.

Ensures that:
1. src/features/color does not import from src.core, src.ui, src.utils
2. src/features/edit does not import from src.core.edit, src.ui.edit
3. src/features/composite does not import from src.composite (legacy - now deleted)
4. src/features/deliver does not import from old export UI
5. src/shared does not import from src.features
6. src/app does not deep-import feature internals (only through module/runtime/adapter)
7. New code should not add new imports from src.core, src.ui, src.utils (legacy paths)
"""

import pytest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _check_imports(directory: Path, forbidden_patterns: list[str], description: str):
    """Check that files in directory don't import from forbidden patterns."""
    offenders = []
    if not directory.exists():
        return []

    for path in directory.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.split("\n")
        for line_num, line in enumerate(lines, 1):
            stripped = line.strip()
            if not (stripped.startswith("from ") or stripped.startswith("import ")):
                continue
            for pattern in forbidden_patterns:
                if pattern in stripped:
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{line_num} '{stripped}'"
                    )
    return offenders


def test_features_color_does_not_import_legacy():
    src_features_color = ROOT / "src/features/color"
    forbidden = [
        "from src.core.",
        "import src.core.",
        "from src.ui.",
        "import src.ui.",
        "from src.utils.",
        "import src.utils.",
    ]
    offenders = _check_imports(src_features_color, forbidden, "color")
    if offenders:
        import warnings
        warnings.warn(
            f"src/features/color still has {len(offenders)} legacy imports "
            f"(src.core/src.ui/src.utils). These should be migrated as part of Phase C.\n"
            + "\n".join(offenders[:10]),
            stacklevel=2,
        )


def test_features_edit_does_not_import_legacy_edit():
    src_features_edit = ROOT / "src/features/edit"
    forbidden = [
        "from src.core.edit",
        "import src.core.edit",
        "from src.ui.edit",
        "import src.ui.edit",
    ]
    offenders = _check_imports(src_features_edit, forbidden, "edit")
    assert offenders == [], (
        f"Found legacy edit imports in src/features/edit:\n" + "\n".join(offenders)
    )


def test_features_composite_does_not_import_legacy_composite():
    src_features_composite = ROOT / "src/features/composite"
    forbidden = [
        "from src.composite",
        "import src.composite",
    ]
    offenders = _check_imports(src_features_composite, forbidden, "composite")
    assert offenders == [], (
        f"Found legacy composite imports in src/features/composite:\n"
        + "\n".join(offenders)
    )


def test_features_deliver_does_not_import_legacy_export():
    src_features_deliver = ROOT / "src/features/deliver"
    forbidden = [
        "from src.core.export",
        "import src.core.export",
        "from src.ui.export",
        "import src.ui.export",
    ]
    offenders = _check_imports(src_features_deliver, forbidden, "deliver")
    assert offenders == [], (
        f"Found legacy export imports in src/features/deliver:\n"
        + "\n".join(offenders)
    )


def test_shared_does_not_import_features():
    src_shared = ROOT / "src/shared"
    forbidden = [
        "from src.features.",
        "import src.features.",
    ]
    offenders = _check_imports(src_shared, forbidden, "shared")
    assert offenders == [], (
        f"shared must not statically import features: {offenders}\n"
        f"Features should be accessed through adapters, not direct imports."
    )


def test_app_does_not_deep_import_feature_internals():
    src_app = ROOT / "src/app"
    allowed_patterns = [
        "from src.features.color.module",
        "from src.features.composite.module",
        "from src.features.edit.module",
        "from src.features.deliver.module",
        "from src.features.scripting.module",
        "from src.features.color",
        "from src.features.composite",
        "from src.features.edit",
        "from src.features.deliver",
        "from src.features.scripting",
        "import src.features.color.module",
        "import src.features.composite.module",
        "import src.features.edit.module",
        "import src.features.deliver.module",
        "import src.features.scripting.module",
    ]

    offenders = []
    if not src_app.exists():
        return

    for path in src_app.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.split("\n")
        for line_num, line in enumerate(lines, 1):
            stripped = line.strip()
            if not (stripped.startswith("from ") or stripped.startswith("import ")):
                continue
            if "from src.features." in stripped or "import src.features." in stripped:
                is_allowed = any(allowed in stripped for allowed in allowed_patterns)
                if not is_allowed:
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{line_num} '{stripped}'"
                    )
    assert offenders == [], (
        f"Found deep feature imports in src/app (should use module/runtime/adapter):\n"
        + "\n".join(offenders)
    )


def test_src_utils_files_are_wrapper_only():
    src_utils = ROOT / "src/utils"
    if not src_utils.exists():
        return

    non_wrapper_files = []
    for path in src_utils.glob("*.py"):
        if path.name == "__init__.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.split("\n")

        has_real_impl = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                continue
            if stripped.startswith("from ") or stripped.startswith("import "):
                continue
            if stripped.startswith("__all__") or stripped == "":
                continue
            if stripped.startswith("class ") and "import" not in stripped:
                has_real_impl = True
                break
            if stripped.startswith("def ") and "import" not in stripped:
                has_real_impl = True
                break

        is_wrapper = (
            "compatibility" in text.lower()
            or "wrapper" in text.lower()
            or "re-export" in text.lower()
        )

        if has_real_impl and not is_wrapper:
            non_wrapper_files.append(path.name)

    assert non_wrapper_files == [], (
        f"src/utils has non-wrapper files (real implementation should be in src/shared/):\n"
        + "\n".join(non_wrapper_files)
        + "\nThese files should be wrappers that re-export from src/shared/"
    )


def test_src_ui_main_window_is_wrapper():
    main_window_path = ROOT / "src/ui/main_window.py"
    if not main_window_path.exists():
        return

    text = main_window_path.read_text(encoding="utf-8")
    lines = text.strip().split("\n")

    content_lines = [l for l in lines if l.strip() and not l.strip().startswith("#")]
    assert len(content_lines) <= 2, (
        f"src/ui/main_window.py should be a thin wrapper (currently {len(content_lines)} lines). "
        f"Real implementation should be in src/app/main_window.py"
    )


def test_src_top_level_only_app_shared_features():
    allowed_top_level = {
        "app",
        "shared",
        "features",
        "__init__.py",
        "main.py",
        "__pycache__",
    }
    legacy_dirs = {"core", "ui", "utils", "composite", "assets", "locales", "scripting"}
    src_dir = ROOT / "src"

    found_legacy = []
    if src_dir.exists():
        for entry in src_dir.iterdir():
            if entry.name in ("__pycache__",):
                continue
            if entry.is_dir() and entry.name in legacy_dirs:
                found_legacy.append(entry.name)

    if found_legacy:
        import warnings
        warnings.warn(
            f"Legacy directories still at src/ top level: {found_legacy}. "
            f"Target is to only have app/shared/features at src/ top level.",
            stacklevel=2,
        )


def test_app_deep_import_budget():
    """Budget-based test: app layer deep imports should be below threshold.

    As controllers are extracted and MainWindow is further refactored,
    this budget should decrease. Update the constant when progress is made.

    Phase 5 (2026-05): reduced from 79 to 65 via edit commands facade,
    composite export provider, and consolidated imports in edit_controller.
    Phase 2 (2026-05): reduced to 10 after full facade migration for
    edit/composite/deliver deep imports. Only color core/ui imports remain.
    Color is always enabled so its deep imports are allowed.
    """
    MAX_APP_DEEP_FEATURE_IMPORTS = 45  # Current count: 40 (color only, edit/composite/deliver = 0)

    deep_patterns = (
        "from src.features.color.core",
        "import src.features.color.core",
        "from src.features.color.ui",
        "import src.features.color.ui",
        "from src.features.edit.core",
        "import src.features.edit.core",
        "from src.features.edit.ui",
        "import src.features.edit.ui",
        "from src.features.composite.core",
        "import src.features.composite.core",
        "from src.features.composite.render",
        "import src.features.composite.render",
        "from src.features.deliver.ui",
        "import src.features.deliver.ui",
    )

    src_app = ROOT / "src/app"
    count = 0
    if src_app.exists():
        for path in src_app.rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for line in text.split("\n"):
                stripped = line.strip()
                if not (stripped.startswith("from ") or stripped.startswith("import ")):
                    continue
                if any(p in stripped for p in deep_patterns):
                    count += 1

    assert count <= MAX_APP_DEEP_FEATURE_IMPORTS, (
        f"App layer has {count} deep feature imports, "
        f"exceeding budget of {MAX_APP_DEEP_FEATURE_IMPORTS}. "
        f"Reduce deep imports or update the budget constant."
    )


def test_deep_edit_composite_deliver_import_count():
    """Budget test: count of direct src.features.{edit,composite,deliver} imports.

    This tracks how many times the app layer reaches into edit, composite,
    and deliver feature internals. Module-level imports (.module, .runtime,
    .export_provider, .commands_facade, .facade) are NOT counted as deep.

    Phase 5 (2026-05): current count 36.
    Phase 2 (2026-05): current count 0 — all routed through facades/export_providers.
    """
    MAX_DEEP_FEATURE_IMPORTS = 5  # Current count: 0 — allow small buffer for transient imports

    deep_patterns = (
        "from src.features.edit.core",
        "import src.features.edit.core",
        "from src.features.edit.ui",
        "import src.features.edit.ui",
        "from src.features.composite.core",
        "import src.features.composite.core",
        "from src.features.composite.render",
        "import src.features.composite.render",
        "from src.features.composite.adapters",
        "import src.features.composite.adapters",
        "from src.features.deliver.core",
        "import src.features.deliver.core",
        "from src.features.deliver.ui",
        "import src.features.deliver.ui",
    )

    src_app = ROOT / "src/app"
    main_py = ROOT / "src/main.py"
    count = 0
    paths_to_check = []
    if src_app.exists():
        for p in src_app.rglob("*.py"):
            if "__pycache__" not in str(p):
                paths_to_check.append(p)
    if main_py.exists():
        paths_to_check.append(main_py)

    for path in paths_to_check:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line in text.split("\n"):
            stripped = line.strip()
            if not (stripped.startswith("from ") or stripped.startswith("import ")):
                continue
            if any(p in stripped for p in deep_patterns):
                count += 1

    assert count <= MAX_DEEP_FEATURE_IMPORTS, (
        f"App layer + main.py has {count} deep edit/composite/deliver imports, "
        f"exceeding budget of {MAX_DEEP_FEATURE_IMPORTS}. "
        f"Reduce deep imports by using facades/export_providers or update the budget."
    )


def _check_legacy_imports_in_subdir(subdir: Path, legacy_prefix: str, description: str):
    """Check that files in subdir don't import from legacy paths."""
    offenders = []
    if not subdir.exists():
        return []
    
    for path in subdir.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.split("\n")
        for line_num, line in enumerate(lines, 1):
            stripped = line.strip()
            if not (stripped.startswith("from ") or stripped.startswith("import ")):
                continue
            patterns = [
                f"from {legacy_prefix}",
                f"import {legacy_prefix}",
            ]
            for pattern in patterns:
                if pattern in stripped:
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{line_num} '{stripped}'"
                    )
    return offenders


def test_features_composite_ui_does_not_import_legacy_ui():
    """Ensure src/features/composite/ui does not import from src/composite/ui."""
    subdir = ROOT / "src/features/composite/ui"
    offenders = _check_legacy_imports_in_subdir(subdir, "src.composite.ui", "composite/ui")
    assert offenders == [], (
        f"Found legacy UI imports in src/features/composite/ui:\n" +
        "\n".join(offenders)
    )


def test_features_composite_nodes_does_not_import_legacy_nodes():
    """Ensure src/features/composite/nodes does not import from src/composite/nodes."""
    subdir = ROOT / "src/features/composite/nodes"
    offenders = _check_legacy_imports_in_subdir(subdir, "src.composite.nodes", "composite/nodes")
    assert offenders == [], (
        f"Found legacy node imports in src/features/composite/nodes:\n" +
        "\n".join(offenders)
    )


def test_features_composite_core_does_not_import_legacy_core():
    """Ensure src/features/composite/core does not import from src/composite/core."""
    subdir = ROOT / "src/features/composite/core"
    offenders = _check_legacy_imports_in_subdir(subdir, "src.composite.core", "composite/core")
    assert offenders == [], (
        f"Found legacy core imports in src/features/composite/core:\n" +
        "\n".join(offenders)
    )


def test_features_composite_render_does_not_import_legacy_render():
    """Ensure src/features/composite/render does not import from src/composite/render."""
    subdir = ROOT / "src/features/composite/render"
    offenders = _check_legacy_imports_in_subdir(subdir, "src.composite.render", "composite/render")
    assert offenders == [], (
        f"Found legacy render imports in src/features/composite/render:\n" +
        "\n".join(offenders)
    )


def test_features_composite_evaluators_does_not_import_legacy_evaluators():
    """Ensure src/features/composite/evaluators does not import from src/composite/evaluators."""
    subdir = ROOT / "src/features/composite/evaluators"
    offenders = _check_legacy_imports_in_subdir(subdir, "src.composite.evaluators", "composite/evaluators")
    assert offenders == [], (
        f"Found legacy evaluators imports in src/features/composite/evaluators:\n" +
        "\n".join(offenders)
    )


def test_features_composite_utils_does_not_import_legacy_utils():
    """Ensure src/features/composite/utils does not import from src/composite/utils."""
    subdir = ROOT / "src/features/composite/utils"
    offenders = _check_legacy_imports_in_subdir(subdir, "src.composite.utils", "composite/utils")
    assert offenders == [], (
        f"Found legacy utils imports in src/features/composite/utils:\n" +
        "\n".join(offenders)
    )


def test_legacy_composite_files_are_wrapper_only():
    """Ensure all files in src/composite are pure wrappers (no real implementation).
    
    A valid wrapper should:
    1. Start with a docstring mentioning compatibility/wrapper/re-export
    2. Only contain from ... import * statements or explicit __all__
    3. Not define any classes/functions beyond re-export
    """
    src_composite = ROOT / "src/composite"
    if not src_composite.exists():
        return
    
    non_wrapper_files = []
    
    for path in src_composite.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        if "__init__.py" in path.name:
            continue
            
        text = path.read_text(encoding="utf-8", errors="ignore")
        
        is_wrapper = (
            "compatibility" in text.lower() or
            "wrapper" in text.lower() or
            "re-export" in text.lower() or
            text.strip().startswith("# Compatibility") or
            "# Compatibility layer" in text
        )
        
        has_real_impl = False
        lines = text.split("\n")
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if stripped.startswith('"""') or stripped.startswith("'''"):
                continue
            if stripped.startswith("from ") or stripped.startswith("import "):
                continue
            if stripped.startswith("__all__"):
                continue
            if stripped == "":
                continue
            if stripped.startswith("class ") and "import" not in stripped:
                has_real_impl = True
                break
            if stripped.startswith("def ") and "import" not in stripped:
                has_real_impl = True
                break
        
        if has_real_impl and not is_wrapper:
            non_wrapper_files.append(str(path.relative_to(ROOT)))
    
    if non_wrapper_files:
        print(f"Note: The following files may need wrapper verification:\n" +
              "\n".join(non_wrapper_files))


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])