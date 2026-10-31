#!/usr/bin/env python3
"""Regenerate the packaged woff2 delivery fonts from their TTF sources.

The HTML font lock inlines fonts as base64 woff2 ``data:`` URIs (much smaller
than TTF).  The TTFs are the source of truth (hashed for ``font_set_hash``);
this script produces the woff2 files the lock actually embeds.  Re-run whenever
``src/assets/fonts/*.ttf`` changes.

Requires ``fonttools`` + ``brotli`` (already project dependencies)::

    .venv/bin/python scripts/build_font_woff2.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from fontTools.ttLib import TTFont

FONTS_DIR = Path(__file__).resolve().parent.parent / "src" / "assets" / "fonts"
SOURCES = ["DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSansMono.ttf"]


def main() -> int:
    if not FONTS_DIR.is_dir():
        print(f"fonts dir not found: {FONTS_DIR}", file=sys.stderr)
        return 1
    for src_name in SOURCES:
        src = FONTS_DIR / src_name
        if not src.is_file():
            print(f"missing source TTF: {src}", file=sys.stderr)
            return 1
        dst = src.with_suffix(".woff2")
        font = TTFont(src)
        font.flavor = "woff2"
        font.save(dst)
        before, after = src.stat().st_size, dst.stat().st_size
        print(f"{src_name}: {before} -> {dst.name}: {after} ({100 * after // before}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
