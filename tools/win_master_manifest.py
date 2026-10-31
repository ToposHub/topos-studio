#!/usr/bin/env python3
"""W09：Windows 母版矩阵 —— tests/test_videos 指纹与属性登记。

对编码对照使用的母版计算 SHA-256，并用 PyAV 读取像素格式/几何/帧率/
帧数/色彩 metadata，输出 JSON manifest（供 RD 实验对拍与复现）。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import av

REPO = Path(__file__).resolve().parents[1]
TV = REPO / "tests" / "test_videos"

MASTERS = [
    "2K_prores422HQ.mov", "4K_prores422HQ.mov",
    "4K_prores4444.mov", "4K_prores4444XQ.mov",
    "2K_DNxHR 444 10bit.mov", "2K_DNxHR 444 12bit.mov",
    "4K_DNxHR 444 10bit.mov", "4K_DNxHR 444 12bit.mov",
]


def sha256(path: Path, block: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(block)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def probe(path: Path) -> dict:
    out = {"name": path.name, "bytes": path.stat().st_size,
           "sha256": sha256(path)}
    with av.open(str(path)) as c:
        s = c.streams.video[0]
        out.update({
            "width": s.codec_context.width, "height": s.codec_context.height,
            "pix_fmt": s.codec_context.format.name,
            "frames": s.frames, "duration_s": float(c.duration) / 1e6
            if c.duration else None,
            "avg_rate": str(s.average_rate),
            "codec": s.codec_context.name or s.codec.name,
        })
    return out


def main() -> int:
    rows = []
    for name in MASTERS:
        p = TV / name
        if not p.is_file():
            rows.append({"name": name, "missing": True})
            continue
        rows.append(probe(p))
        print(f"{name}: {rows[-1].get('pix_fmt')} "
              f"{rows[-1].get('width')}x{rows[-1].get('height')} "
              f"frames={rows[-1].get('frames')}", file=sys.stderr)
    out = Path("native/topos_codec/docs/windows/win_master_manifest_2026-09-28.json")
    out.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"[manifest] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
