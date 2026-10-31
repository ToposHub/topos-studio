"""Unit tests for the wheel resource scan in scripts/verify_packaged_resources.py.

The old scan condition suffered an operator-precedence bug
(``endswith(...) and "/presets/" in name or "/templates/" in name``): every
file under /templates/ was decoded regardless of extension while /locales/
text was never checked.  These tests pin the corrected predicate and the
leak detector with positive and negative cases, independent of wheel builds.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify_packaged_resources.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("verify_packaged_resources", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("verify_packaged_resources", module)
    spec.loader.exec_module(module)
    return module


mod = _load_module()


# -- what gets scanned ---------------------------------------------------------

def test_text_resources_in_all_resource_dirs_are_scannable():
    for name in (
        "topos/presets/library/charts/pie/manifest.json",
        "topos/presets/library/charts/pie/build.py",
        "topos/templates/motion_graphic/particle/template.html",
        "topos/features/teaching_animation/locales/en_US.json",
        "topos/shared/i18n/locales/zh_CN.json",
        "topos/templates/motion_graphic/particle/README.md",
    ):
        assert mod.is_scannable_text_resource(name), name


def test_binaries_and_non_resource_text_are_not_scanned():
    for name in (
        "topos/templates/motion_graphic/particle/preview.gif",   # binary in resource dir
        "topos/presets/library/charts/pie/preview.gif",
        "topos/core/renderer.py",          # code but not a packaged resource dir
        "topos-0.1.dist-info/METADATA",    # metadata, extension not in text set
    ):
        assert not mod.is_scannable_text_resource(name), name


def test_templates_dir_only_scans_text_extensions():
    # The precedence bug scanned ANY extension under /templates/.
    assert not mod.is_scannable_text_resource("topos/templates/x/icon.png")
    assert mod.is_scannable_text_resource("topos/templates/x/icon.svg.txt")


# -- what counts as a leak ------------------------------------------------------

def test_leak_detector_flags_machine_absolute_paths():
    text = 'preview = "/Users/testuser/dev/preview.gif"'
    assert mod.find_absolute_path_leaks(text) == ["/Users/"]
    assert mod.find_absolute_path_leaks("path = '/tmp/build/x'") == ["/tmp/"]
    assert mod.find_absolute_path_leaks(r'root = "C:\\Dev"') == ["C:\\\\"]
    assert mod.find_absolute_path_leaks("home = '/home/alice'") == ["/home/"]


def test_leak_detector_allows_portable_relative_paths():
    for text in (
        '"src": "presets/library/pie/preview.gif"',
        '"home": "Library/Application Support"',
        '"path": "C:relative/paths"',      # not the C:\\ absolute pattern
        '"/usr/local/share/icons"',        # system path, not a per-machine path
    ):
        assert mod.find_absolute_path_leaks(text) == [], text
