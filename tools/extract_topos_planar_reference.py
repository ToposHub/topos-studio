#!/usr/bin/env python3
"""从视频解码并冻结 paired-quality 使用的紧密排列 planar 原始帧。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import av
import numpy as np


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract(source: Path, output: Path, pixel_format: str,
            frames: int) -> dict:
    if frames <= 0:
        raise ValueError("--frames 必须为正")
    if output.exists():
        raise FileExistsError(f"输出已存在，拒绝覆盖: {output}")
    source = source.resolve(strict=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    dimensions: tuple[int, int] | None = None
    color: dict[str, int] | None = None
    try:
        with av.open(str(source)) as container, output.open("xb") as dest:
            stream = container.streams.video[0]
            for frame in container.decode(stream):
                if count == frames:
                    break
                if dimensions is None:
                    dimensions = (frame.width, frame.height)
                    color = {
                        "primaries": int(frame.color_primaries or 0),
                        "transfer": int(frame.color_trc or 0),
                        "matrix": int(frame.colorspace or 0),
                        "range": int(frame.color_range or 0),
                    }
                if (frame.width, frame.height) != dimensions:
                    raise ValueError("源视频中途改变几何；拒绝混合原始平面")
                if frame.format.name != pixel_format:
                    frame = frame.reformat(format=pixel_format)
                if len(frame.planes) < 3:
                    raise ValueError("输入不是三平面 YUV")
                for plane in frame.planes[:3]:
                    samples = np.ndarray(
                        (plane.height, plane.width), dtype="<u2",
                        buffer=memoryview(plane), strides=(plane.line_size, 2),
                    )
                    dest.write(np.ascontiguousarray(samples).tobytes())
                count += 1
        if count != frames:
            raise ValueError(f"源仅有 {count} 帧，要求 {frames} 帧")
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "command": " ".join(sys.argv),
        "source": str(source), "source_sha256": sha256_file(source),
        "source_color": color,
        "pixel_format": pixel_format,
        "width": dimensions[0], "height": dimensions[1], "frames": count,
        "av_version": av.__version__,
        "output": str(output.resolve()), "output_sha256": sha256_file(output),
        "output_bytes": output.stat().st_size,
        "source_is_lossless_original": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-video", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--pix-fmt", required=True,
                        choices=("yuv422p10le", "yuv444p10le", "yuv444p12le"))
    parser.add_argument("--frames", type=int, required=True)
    args = parser.parse_args()
    if args.manifest.exists():
        raise FileExistsError(f"清单已存在，拒绝覆盖: {args.manifest}")
    result = extract(args.source_video, args.out, args.pix_fmt, args.frames)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2)
                             + "\n", encoding="utf-8")
    print(f"冻结 {result['frames']} 帧 {result['pixel_format']} → "
          f"{result['output']} ({result['output_sha256']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
