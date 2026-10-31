#!/usr/bin/env python
"""R12: verify MG resources actually land inside a built wheel.

Builds the project wheel into a temp directory and inspects the archive:

  * every bundled MG preset folder ships its code/manifest AND preview.gif
  * the HTML template packages ship manifest.json + template.html + preview.gif
  * locale JSON files for teaching_animation are included
  * no absolute machine paths leaked into any shipped text resource

Exit 0 when the release package is complete, 1 otherwise.  Skips (exit 0)
with a message when the ``build`` module is unavailable.

Usage:
  scripts/verify_packaged_resources.py [--keep <dir>]
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_ABSOLUTE_PATH = re.compile(r"/Users/|/home/|/tmp/|C:\\\\")

# Only these text resources are decoded and scanned.  Binary assets (GIF,
# ZIP, fonts) never go through the text scan — they are covered by the
# structural checks above instead.
_TEXT_SUFFIXES = (".json", ".html", ".md", ".py", ".txt")
# Resource directories whose text content may embed generated paths.
# NOTE the parentheses at the call site: `a and b or c` would scan every
# file under /templates/ regardless of extension and skip /locales/ text.
_RESOURCE_DIRS = ("/presets/", "/templates/", "/locales/")


def is_scannable_text_resource(name: str) -> bool:
    """True when a wheel entry is text AND belongs to a resource directory."""
    return name.endswith(_TEXT_SUFFIXES) and any(d in name for d in _RESOURCE_DIRS)


def find_absolute_path_leaks(text: str) -> list[str]:
    """Machine-absolute path patterns found in the given resource text."""
    return _ABSOLUTE_PATH.findall(text)


def _build_wheel(outdir: Path) -> Path | None:
    try:
        import build  # noqa: F401
    except ImportError:
        return None
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(outdir), "--no-isolation"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(result.stdout[-2000:])
        print(result.stderr[-2000:])
        raise SystemExit("wheel build failed")
    wheels = sorted(outdir.glob("*.whl"))
    return wheels[-1] if wheels else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", type=Path, default=None,
                        help="keep the built wheel in this directory")
    args = parser.parse_args()

    outdir = args.keep or Path(tempfile.mkdtemp(prefix="topos_wheel_"))
    outdir.mkdir(parents=True, exist_ok=True)
    wheel = _build_wheel(outdir)
    if wheel is None:
        print("SKIP: python 'build' module unavailable (pip install build)")
        return 0
    print(f"wheel: {wheel.name}")

    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()

    def _exists(pattern: str) -> int:
        return sum(1 for name in names if re.search(pattern, name))

    failures: list[str] = []

    presets = [n for n in names if "/presets/library/" in n and n.endswith("build.py")]
    preset_gifs = [n for n in names if "/presets/library/" in n and n.endswith("preview.gif")]
    if len(presets) < 13:
        failures.append(f"bundled presets in wheel: {len(presets)} < 13")
    if len(preset_gifs) != len(presets):
        failures.append(
            f"preset previews mismatched: {len(preset_gifs)} gifs for {len(presets)} presets")

    manifests = [n for n in names if "/templates/motion_graphic/" in n and n.endswith("manifest.json")]
    templates = [n for n in names if "/templates/motion_graphic/" in n and n.endswith("template.html")]
    template_gifs = [n for n in names if "/templates/motion_graphic/" in n and n.endswith("preview.gif")]
    if not manifests or len(manifests) != len(templates) or len(manifests) != len(template_gifs):
        failures.append(
            f"template packages incomplete: {len(manifests)} manifests, "
            f"{len(templates)} html, {len(template_gifs)} gifs")

    locales = [n for n in names if "/teaching_animation/locales/" in n and n.endswith(".json")]
    if {"en_US", "zh_CN"} - {Path(n).stem for n in locales}:
        failures.append(f"locale files missing: {sorted(locales)}")
    # R-11: the shared i18n tables must ship too, or every feature's
    # shared-namespace translations fall back to English on a clean machine.
    shared = [n for n in names if "/shared/i18n/locales/" in n and n.endswith(".json")]
    if {"en_US", "zh_CN"} - {Path(n).stem for n in shared}:
        failures.append(f"shared i18n locale files missing: {sorted(shared)}")

    # Absolute machine paths must never ship inside text resources.
    leaked: list[str] = []
    with zipfile.ZipFile(wheel) as archive:
        for name in names:
            if not is_scannable_text_resource(name):
                continue
            try:
                text = archive.read(name).decode("utf-8", errors="ignore")
            except (OSError, KeyError):
                continue
            if find_absolute_path_leaks(text):
                leaked.append(name)
    if leaked:
        failures.append(f"absolute machine paths shipped in: {leaked[:5]}")

    scanned = sum(1 for name in names if is_scannable_text_resource(name))
    print(f"  text resources scanned for path leaks: {scanned}")

    print(f"  presets: {len(presets)} (gifs {len(preset_gifs)})")
    print(f"  template packages: {len(manifests)} (html {len(templates)}, gifs {len(template_gifs)})")
    print(f"  locales: {[Path(n).name for n in locales]}")
    print(f"  wheel kept in: {outdir}")
    if failures:
        for failure in failures:
            print(f"  FAIL: {failure}")
        return 1
    print("packaged MG resources complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
