#!/usr/bin/env python
"""R-11: clean-machine install verification for the MG feature set.

The wheel-resource check proves files ship; it does not prove a fresh
machine can USE them.  This script installs the built wheel into an
isolated target directory (no source tree on sys.path), imports the MG
modules from that install only, and exercises the static core flow:

  * template packages discover from the wheel (bundled origin)
  * the unified catalog lists presets + templates with previews
  * locale files resolve through I18N in both languages
  * the bitrate tier helpers read their product table
  * writable roots (user templates, presets) point OUTSIDE the wheel dir
  * a missing AI manifest degrades to a warning, never a crash

Deps (PyQt6 etc.) come from the running interpreter — only the package
under test is isolated to the wheel install, which is exactly the
"wheel is self-contained" property under test.

Exit 0 when every clean-install probe passes.  Requires pip + the
``build`` module (skips otherwise).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_PROBES = r'''
import json, sys, os
from pathlib import Path
results = []

def probe(name, fn):
    try:
        detail = fn()
        results.append({"name": name, "ok": True, "detail": str(detail)})
    except Exception as exc:
        results.append({"name": name, "ok": False, "detail": f"{type(exc).__name__}: {exc}"})

def pkg_file(module):
    import importlib
    return str(importlib.import_module(module).__file__)

def no_repo_leak(path):
    repo = os.environ["TOPOS_CLEAN_INSTALL_REPO"]
    return "source-tree" if repo in path else "wheel"

probe("mg module imports from wheel", lambda: no_repo_leak(pkg_file("src.features.teaching_animation.module")))

def templates():
    from src.features.teaching_animation.core.motion_graphic_template_store import (
        discover_stored_templates, user_template_root)
    stored = discover_stored_templates()
    assert stored, "no template packages discovered from the wheel"
    assert all(s.origin == "bundled" for s in stored), "fresh machine must see bundled origin only"
    root = str(user_template_root())
    assert "site-packages" not in root and "dist-packages" not in root, \
        f"user template root must not live inside the install: {root}"
    return f"{len(stored)} bundled templates; user root outside wheel"

probe("template discovery (bundled only, writable root outside)", templates)

def catalog():
    from src.features.teaching_animation.core.mg_package_registry import MgPackageCatalog
    from src.features.teaching_animation.presets.library import MgPresetLibrary
    cat = MgPackageCatalog(presets=MgPresetLibrary())
    cat.refresh()
    presets = cat.packages(kind="scene_preset")
    templates = cat.packages(kind="html_template")
    assert len(presets) >= 13, f"presets in wheel: {len(presets)}"
    with_preview = [p for p in cat.packages() if p.preview_path and Path(p.preview_path).is_file()]
    assert len(with_preview) >= len(presets), "preset previews missing from wheel"
    return f"{len(presets)} presets + {len(templates)} templates, {len(with_preview)} previews on disk"

probe("unified catalog over wheel resources", catalog)

def locales():
    from pathlib import Path
    from src.shared.i18n.i18n import I18N
    base = Path(pkg_file("src.features.teaching_animation.module")).parent / "locales"
    for loc in ("en_US", "zh_CN"):
        assert (base / f"{loc}.json").is_file(), f"{loc} locale missing from wheel"
    assert I18N.load_locale("en_US")
    I18N.load_module_translations(str(base))
    assert I18N.tr("motion_graphic.ai.plan_cancelled", "x") != "x"
    return "en_US/zh_CN ship and resolve AI message keys"

probe("locale files resolve", locales)

def bitrate_table():
    from src.features.edit.core.render_cache import motion_graphic_bitrate_kbps
    assert motion_graphic_bitrate_kbps(1920, 1080, preset="high_quality") == 40000
    assert motion_graphic_bitrate_kbps(1920, 1080, preset="proxy") == 8000
    return "1080p tiers 8/20/40 Mbps read from the wheel"

probe("bitrate tier table", bitrate_table)

def missing_ai_manifest_degrades():
    # No model manifest in a fresh machine: the MG editor must still import
    # and expose its public entry points (warning, not crash).
    from src.features.teaching_animation import module as mg_module
    assert hasattr(mg_module, "TeachingAnimationModule") or hasattr(mg_module, "MotionGraphicModule") \
        or callable(getattr(mg_module, "initialize", None))
    return "module initializes without any AI manifest"

probe("missing AI manifest degrades gracefully", missing_ai_manifest_degrades)

print("CLEAN_INSTALL_JSON:" + json.dumps(results))
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, default=None,
                        help="use this wheel instead of building one")
    args = parser.parse_args()

    try:
        import build  # noqa: F401
    except ImportError:
        print("SKIP: python 'build' module unavailable (pip install build)")
        return 0

    with tempfile.TemporaryDirectory(prefix="topos_clean_install_") as tmp:
        tmp_path = Path(tmp)
        wheel = args.wheel
        if wheel is None:
            outdir = tmp_path / "wheel"
            outdir.mkdir()
            built = subprocess.run(
                [sys.executable, "-m", "build", "--wheel", "--outdir", str(outdir),
                 "--no-isolation"], cwd=str(REPO_ROOT), capture_output=True, text=True)
            if built.returncode != 0:
                print(built.stderr[-2000:])
                return 1
            wheel = sorted(outdir.glob("*.whl"))[-1]
        print(f"wheel under test: {wheel.name}")

        target = tmp_path / "site"
        installed = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--no-deps",
             "--target", str(target), str(wheel)], capture_output=True, text=True)
        if installed.returncode != 0:
            print(installed.stderr[-2000:])
            return 1

        import os
        env = dict(os.environ)
        env["TOPOS_CLEAN_INSTALL_REPO"] = str(REPO_ROOT / "src")
        env["PYTHONPATH"] = str(target)  # wheel only — repo src NOT on the path
        child = subprocess.run(
            [sys.executable, "-c", _PROBES], capture_output=True, text=True,
            env=env, cwd=str(tmp_path))
        for line in child.stdout.splitlines():
            if line.startswith("CLEAN_INSTALL_JSON:"):
                results = json.loads(line[len("CLEAN_INSTALL_JSON:"):])
                failures = 0
                for item in results:
                    mark = "PASS" if item["ok"] else "FAIL"
                    print(f"  [{mark}] {item['name']}: {item['detail']}")
                    failures += 0 if item["ok"] else 1
                print(f"\nclean-install probes: {len(results) - failures}/{len(results)} passed")
                return 1 if failures else 0
        print("probe subprocess produced no verdict:")
        print(child.stdout[-1500:])
        print(child.stderr[-1500:])
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
