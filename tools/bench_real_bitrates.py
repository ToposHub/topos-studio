#!/usr/bin/env python3
"""Measure current TOPOS and ProRes profile bitrates on the same 2K source.

The 422 tiers use the 2K ProRes 422 HQ fixture.  The 444 tiers use the 4K
ProRes 4444 fixture downscaled to 1920x1080 so every row has the same frame
geometry, frame count, and frame rate.
"""
from __future__ import annotations

import argparse
import gc
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
W, H, FPS = 1920, 1080, 25
TIERS = [
    ("proxy", 3, 0, 10, "422", "proxy"),
    ("lt", 3, 0, 10, "422", "lt"),
    ("standard", 3, 0, 10, "422", "standard"),
    ("hq", 3, 0, 10, "422", "hq"),
    ("pro444", 5, 1, 10, "444", "4444"),
    ("extreme", 6, 1, 12, "444", "4444xq"),
]


def run(cmd: list[str]) -> None:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(f"command failed: {' '.join(cmd)}\n{p.stderr[-2000:]}")


def raw_source(domain: str, depth: int, n: int, out: Path) -> Path:
    src = (ROOT / "tests/test_videos/2K_prores422HQ.mov" if domain == "422"
           else ROOT / "tests/test_videos/4K_prores4444.mov")
    pix = f"yuv{domain}p{depth}le"
    vf = [] if domain == "422" else ["-vf", "scale=1920:1080:flags=lanczos"]
    if out.exists():
        expected = n * W * H * 4 if domain == "422" else n * W * H * 6
        if out.stat().st_size == expected:
            return out
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src),
         "-frames:v", str(n), *vf, "-pix_fmt", pix, "-f", "rawvideo", str(out)])
    return out


def load_frames(raw: Path, domain: str, n: int) -> list[list[np.ndarray]]:
    buf = np.fromfile(raw, dtype="<u2")
    ysz = W * H
    cw = W // 2 if domain == "422" else W
    csz = cw * H
    per = ysz + 2 * csz
    return [[buf[o:o + ysz].reshape(H, W),
             buf[o + ysz:o + ysz + csz].reshape(H, cw),
             buf[o + ysz + csz:o + per].reshape(H, cw)]
            for o in range(0, n * per, per)]


def ffprobe(path: Path) -> dict:
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries",
                        "stream=codec_name,profile,pix_fmt,width,height,nb_frames,bit_rate",
                        "-of", "json", str(path)], check=True,
                       capture_output=True, text=True)
    streams = json.loads(p.stdout).get("streams", [])
    return streams[0] if streams else {}


def rates(path: Path, n: int) -> dict:
    size = path.stat().st_size
    duration = n / FPS
    return {
        "bytes": size,
        "mib": round(size / (1024 * 1024), 4),
        "mbps": round(size * 8 / duration / 1_000_000, 4),
        "bpp": round(size * 8 / (n * W * H), 6),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=96)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--lib", required=True)
    args = ap.parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT))
    import src.shared.codec.topos_binding as tb
    from src.shared.codec.topos_profiles import TOPOS_PROFILE_TIERS

    codec = tb.ToposCodec(tb.load_library(args.lib))
    codec.set_slice_threads(16)
    result = {"width": W, "height": H, "fps": FPS, "frames": args.frames,
              "source": "tests/test_videos/2K_prores422HQ.mov for 422; "
                        "tests/test_videos/4K_prores4444.mov downscaled for 444",
              "library": args.lib, "tiers": []}

    frames_by_domain = {}
    for domain, depth in (("422", 10), ("444", 10), ("444", 12)):
        raw = raw_source(domain, depth, args.frames,
                         out / f"raw_{domain}_{depth}.yuv")
        frames_by_domain[(domain, depth)] = load_frames(raw, domain, args.frames)

    for tier_id, profile, pf, depth, domain, prores_profile in TIERS:
        tier = TOPOS_PROFILE_TIERS[tier_id]
        frames = frames_by_domain[(domain, depth)]
        cfg = codec.movie_config(W, H, qp=24, profile=profile,
                                 bit_depth=depth, pixel_format=pf)
        fc = tb._CFrameConfig()
        import ctypes
        fc.struct_size = ctypes.sizeof(tb._CFrameConfig)
        fc.abi_version = tb.TOPOS_CODEC_ABI_VERSION
        fc.visible_width, fc.visible_height = W, H
        fc.profile, fc.pixel_format, fc.bit_depth = profile, pf, depth
        fc.qmatrix_id, fc.qp_base = 1, 24
        fc.color_range = fc.color_primaries = fc.color_transfer = 1
        fc.color_matrix = 1
        fc.sar_num = fc.sar_den = 1
        target = tier.frame_target_bytes(W, H)
        topos_path = out / f"topos_{tier_id}.mov"
        with tb.ToposMuxFile(codec, str(topos_path), cfg) as mux:
            packet_bytes = 0
            qps = []
            for i, planes in enumerate(frames):
                pkt, _stats, qp = codec.encode_sized(fc, planes, target)
                packet_bytes += len(pkt)
                qps.append(qp)
                mux.add_packet(pkt, pts=i, dur=1)
            mux.finish()

        pix = f"yuv{domain}p{depth}le"
        prores_path = out / f"prores_{prores_profile}.mov"
        if not prores_path.exists():
            run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "rawvideo",
                 "-pix_fmt", pix, "-s", f"{W}x{H}", "-r", str(FPS),
                 "-i", str(out / f"raw_{domain}_{depth}.yuv"),
                 "-c:v", "prores_ks", "-profile:v", prores_profile,
                 "-threads", "16", "-f", "mov", str(prores_path)])

        row = {
            "id": tier_id,
            "domain": f"{domain}p{depth}",
            "topos_target_bpp": tier.target_bpp,
            "topos_qp_min": min(qps),
            "topos_qp_max": max(qps),
            "topos_packet": {"bytes": packet_bytes,
                              "mib": round(packet_bytes / (1024 * 1024), 4),
                              "mbps": round(packet_bytes * 8 / (args.frames / FPS) / 1e6, 4),
                              "bpp": round(packet_bytes * 8 / (args.frames * W * H), 6)},
            "topos_mov": rates(topos_path, args.frames),
            "prores_profile": prores_profile,
            "prores_mov": rates(prores_path, args.frames),
            "prores_probe": ffprobe(prores_path),
        }
        result["tiers"].append(row)
        print(f"{tier_id:8s} TOPOS {row['topos_mov']['mbps']:8.3f} Mbps "
              f"({row['topos_mov']['bpp']:.4f} bpp) | "
              f"ProRes {row['prores_mov']['mbps']:8.3f} Mbps "
              f"({row['prores_mov']['bpp']:.4f} bpp)")

    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(f"[result] {out / 'result.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
