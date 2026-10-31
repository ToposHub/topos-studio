#!/usr/bin/env python3
"""Compare A/B export outputs: probe metadata + per-frame MAD (0-255 scale).

Topos (TPIC) outputs must be decoded with the native ToposMediaSource —
FFmpeg cannot decode TPIC and PyAV open/decode segfaults on it (documented
in the master plan §20.9 / P2-14 notes). Per-plane MAD is reported on the
8-bit scale (10-bit values / 4) for those outputs.
"""

from __future__ import annotations

import sys
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEST_ROOT))

import numpy as np  # noqa: E402

CASES = ["h264", "prores_422", "dnxhr_lb", "topos_proxy"]
TOPOS_CASES = {"topos_proxy"}


def decode_ffmpeg(path: Path, limit: int = 48):
    import av

    frames = []
    pix_fmt = codec = None
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        codec = stream.codec_context.name if stream.codec_context else None
        pix_fmt = stream.codec_context.pix_fmt if stream.codec_context else None
        for frame in container.decode(stream):
            frames.append(frame.to_ndarray(format="rgb24"))
            if len(frames) >= limit:
                break
    return codec, pix_fmt, frames


def decode_topos(path: Path, limit: int = 48):
    from src.shared.media.topos_source import ToposMediaSource

    source = ToposMediaSource(str(path))
    source.open()
    planes_per_frame = []
    try:
        for i in range(limit):
            frame = source.read_frame(i)
            if frame is None:
                break
            planes_per_frame.append(
                [p.astype(np.int16) for p in frame.planes])
    finally:
        source.close()
    return "topos", "yuv422p10le", planes_per_frame


def main() -> int:
    base_dir = TEST_ROOT / "bench_out" / "ab_baseline"
    dev_dir = TEST_ROOT / "bench_out" / "ab_dev"
    overall_ok = True
    for name in CASES:
        b = base_dir / f"{name}.mov"
        d = dev_dir / f"{name}.mov"
        if not b.exists() or not d.exists():
            print(f"{name}: MISSING output")
            overall_ok = False
            continue
        decoder = decode_topos if name in TOPOS_CASES else decode_ffmpeg
        bcodec, bpix, bframes = decoder(b)
        dcodec, dpix, dframes = decoder(d)
        n = min(len(bframes), len(dframes))
        if name in TOPOS_CASES:
            mads = [
                float(np.mean([
                    np.abs(bf[p].astype(np.int16)
                           - df[p].astype(np.int16)).mean() / 4.0
                    for p in range(3)]))
                for bf, df in zip(bframes[:n], dframes[:n])]
        else:
            mads = [float(np.abs(bf.astype(np.int16) - df.astype(np.int16)
                                 ).mean())
                    for bf, df in zip(bframes[:n], dframes[:n])]
        mad = float(np.mean(mads))
        mad_max = float(np.max(mads))
        same_frames = len(bframes) == len(dframes)
        ok = same_frames and mad < 4.0
        overall_ok = overall_ok and ok
        print(f"{name}: frames b={len(bframes)} d={len(dframes)} "
              f"pix b={bpix} d={dpix} codec b={bcodec} d={dcodec} "
              f"MAD={mad:.3f} max={mad_max:.3f} -> {'OK' if ok else 'CHECK'}")
    print("VERDICT:", "OK" if overall_ok else "NEEDS_REVIEW")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
