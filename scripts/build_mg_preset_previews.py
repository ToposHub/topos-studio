#!/usr/bin/env python
"""Generate preview.gif files for the MG preset library (bundled root by default).

Every preset is also load-validated (schema v3) even when its GIF already
exists, so this doubles as a library health check.

Usage:
    .venv/bin/python scripts/build_mg_preset_previews.py [--force]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main() -> int:
    parser = argparse.ArgumentParser(description="Render MG preset preview GIFs")
    parser.add_argument("--force", action="store_true", help="regenerate existing GIFs")
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--max-frames", type=int, default=24)
    args = parser.parse_args()

    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)

    from src.features.teaching_animation.presets import (
        MgPresetLibrary,
        bundled_preset_root,
        load_preset_scene,
        render_preset_gif,
    )

    library = MgPresetLibrary([bundled_preset_root()])
    print(f"discovered {len(library.records())} presets "
          f"({len(library.categories())} categories), errors: {len(library.errors)}")
    for error in library.errors:
        print(f"  ! {error}")

    rendered = skipped = failed = 0
    for record in library.records():
        gif = record.folder / record.manifest.preview
        try:
            scene = load_preset_scene(record.manifest, record.folder)
        except Exception as exc:  # noqa: BLE001 — report and continue
            print(f"FAIL load   {record.preset_id}: {exc}")
            failed += 1
            continue
        if gif.is_file() and not args.force:
            skipped += 1
            continue
        try:
            render_preset_gif(scene, gif, width=args.width, max_frames=args.max_frames)
            rendered += 1
            print(f"ok   render {record.preset_id} -> {gif.name} "
                  f"({gif.stat().st_size // 1024} KiB)")
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL render {record.preset_id}: {exc}")
            failed += 1
    print(f"RESULT: {rendered} rendered · {skipped} already had GIF · {failed} failed")
    del app
    return 0 if failed == 0 and not library.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
