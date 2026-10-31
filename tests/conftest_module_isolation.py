"""
Pytest fixtures for testing module isolation.

These fixtures temporarily hide optional feature modules from disk and/or
set feature flag environment variables to verify that the application
degrades gracefully when modules are missing.
"""
import os
import shutil
import sys
from pathlib import Path
from typing import List

import pytest


# Base directory for feature modules
_FEATURES_DIR = Path(__file__).resolve().parent.parent / "src" / "features"


def _hide_module_dirs(module_names: List[str]):
    """
    Temporarily rename feature module directories so they cannot be found
    by the import system.

    Returns a cleanup callable that restores all directories, even if an
    earlier restore step fails.
    """
    renames: List[tuple] = []  # (hidden_path, original_path)

    for name in module_names:
        original = _FEATURES_DIR / name
        hidden = _FEATURES_DIR / f"_{name}_hidden"
        if original.is_dir():
            shutil.move(str(original), str(hidden))
            renames.append((hidden, original))
        else:
            # Directory already absent; nothing to restore.
            renames.append((None, None))

    def _restore():
        errors: List[Exception] = []
        for hidden, original in renames:
            if hidden is None:
                continue
            try:
                if hidden.is_dir():
                    # If original somehow re-appeared, remove the hidden copy
                    # to avoid clobbering developer work.
                    if original.is_dir():
                        shutil.rmtree(str(hidden))
                    else:
                        shutil.move(str(hidden), str(original))
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise RuntimeError(
                f"Errors during module directory restore: {errors}"
            )

    return _restore


@pytest.fixture
def hide_modules():
    """
    Fixture that temporarily hides feature module directories from disk.

    Usage::

        def test_something(hide_modules):
            hide_modules(["edit", "composite", "deliver"])
            # ... test code runs with those dirs missing ...
            # directories are restored automatically after the test

    The fixture also clears cached ``sys.modules`` entries for the hidden
    modules so that subsequent imports truly fail.

    Cleanup runs via a pytest finalizer, guaranteeing execution even if
    the test crashes.
    """
    _cleanup = None
    _purged_modules = None

    def _hide(module_names: List[str]):
        nonlocal _cleanup, _purged_modules
        # Restore any previous hide before applying a new one.
        if _cleanup is not None:
            _cleanup()
        if _purged_modules:
            for name, module in _purged_modules.items():
                sys.modules.setdefault(name, module)

        _cleanup = _hide_module_dirs(module_names)

        # Purge cached imports so that ``import src.features.edit`` will
        # re-resolve from disk and fail.
        _purged_modules = _purge_module_cache(module_names)

    yield _hide

    # Finalizer: always restore.
    if _cleanup is not None:
        _cleanup()
    if _purged_modules:
        for name, module in _purged_modules.items():
            sys.modules.setdefault(name, module)


def _purge_module_cache(module_names: List[str]):
    """Remove cached sys.modules entries and return them for later restore."""
    prefixes = [f"src.features.{name}" for name in module_names]
    to_remove = [
        mod for mod in sys.modules
        if any(mod == p or mod.startswith(p + ".") for p in prefixes)
    ]
    removed = {}
    for mod in to_remove:
        removed_module = sys.modules.pop(mod, None)
        if removed_module is not None:
            removed[mod] = removed_module
    return removed


@pytest.fixture
def color_only_env(monkeypatch):
    """
    Set TOPOS_DISABLE_FEATURES for every non-color optional feature without
    touching the filesystem.  Only feature flags are affected.

    teaching_animation / ai_workflow 后来加入默认开启集（DEFAULT_FEATURE_FLAGS），
    color-only 场景必须一并禁用，否则注册器会照常加载它们。

    The environment is automatically restored via monkeypatch when the
    test finishes.
    """
    _ALL_OPTIONAL = ("edit,composite,deliver,teaching_animation,ai_workflow")
    monkeypatch.setenv("TOPOS_DISABLE_FEATURES", _ALL_OPTIONAL)
    # Clear any per-feature overrides so the bulk disable takes effect.
    for feat in ("EDIT", "COMPOSITE", "DELIVER", "TEACHING_ANIMATION", "AI_WORKFLOW"):
        monkeypatch.delenv(f"TOPOS_FEATURE_{feat}", raising=False)
    yield


@pytest.fixture
def color_only_physical(hide_modules, monkeypatch):
    """
    Physically hide edit, composite, and deliver module directories *and*
    disable every non-color optional feature flag.  This simulates a
    color-only build where the optional modules are not shipped at all.
    """
    modules_to_hide = ["edit", "composite", "deliver"]
    hide_modules(modules_to_hide)
    monkeypatch.setenv(
        "TOPOS_DISABLE_FEATURES",
        "edit,composite,deliver,teaching_animation,ai_workflow",
    )
    for feat in ("EDIT", "COMPOSITE", "DELIVER", "TEACHING_ANIMATION", "AI_WORKFLOW"):
        monkeypatch.delenv(f"TOPOS_FEATURE_{feat}", raising=False)
    yield
