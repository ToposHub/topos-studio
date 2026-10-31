#!/usr/bin/env python3
"""用 12-bit 444 灰阶梯度探查 VideoToolbox ProRes 的码值映射。"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()
    output = args.out_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    width = height = 128
    count = 2
    source = output / "source_12bit.yuv"
    packed = output / "source_p416le.raw"
    movie = output / "prores_4444xq.mov"
    decoded = output / "decoded_12bit.yuv"
    report = output / "vt_12bit_ramp.json"
    if any(path.exists() for path in (source, packed, movie, decoded, report)):
        raise FileExistsError("输出已存在，拒绝覆盖原始探针")

    y = np.tile(np.linspace(256, 3760, width, dtype="<u2"), (height, 1))
    uv = np.full((height, width), 2048, dtype="<u2")
    with source.open("wb") as target:
        for _ in range(count):
            for plane in (y, uv, uv):
                target.write(plane.tobytes())
    ffmpeg = args.ffmpeg
    raw = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "rawvideo"]
    _run(raw + ["-pixel_format", "yuv444p12le", "-video_size",
                f"{width}x{height}", "-framerate", "25", "-i", str(source),
                "-pix_fmt", "p416le", "-f", "rawvideo", "-y", str(packed)])
    _run(raw + ["-pixel_format", "p416le", "-video_size",
                f"{width}x{height}", "-framerate", "25", "-i", str(packed),
                "-c:v", "prores_videotoolbox", "-allow_sw", "1",
                "-profile:v", "5", "-pix_fmt", "p416le",
                "-color_primaries", "bt709", "-color_trc", "bt709",
                "-colorspace", "bt709", "-color_range", "tv",
                "-y", str(movie)])
    _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(movie),
          "-pix_fmt", "yuv444p12le", "-f", "rawvideo", "-y", str(decoded)])
    original = np.fromfile(source, dtype="<u2").reshape(count, 3, height, width)
    actual = np.fromfile(decoded, dtype="<u2").reshape(count, 3, height, width)
    difference = actual.astype(np.int32) - original.astype(np.int32)
    result = {
        "status": "synthetic_mapping_probe_no_quality_verdict",
        "measurement_script_sha256": _sha256(Path(__file__)),
        "ffmpeg_version": subprocess.run(
            [ffmpeg, "-version"], check=True, capture_output=True,
            text=True).stdout.splitlines()[0],
        "format": "yuv444p12le", "prores_input": "p416le",
        "profile": "4444xq", "allow_software_fallback": True,
        "hardware_acceleration_confirmed": False,
        "width": width, "height": height, "frames": count,
        "source_sha256": _sha256(source), "movie_sha256": _sha256(movie),
        "decoded_sha256": _sha256(decoded),
        "per_channel": [
            {"mean_error_codes": float(difference[:, index].mean()),
             "mae_codes": float(np.abs(difference[:, index]).mean()),
             "max_abs_codes": int(np.abs(difference[:, index]).max())}
            for index in range(3)
        ],
    }
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
