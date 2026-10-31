#!/usr/bin/env python3
"""FFmpeg 外部进程 bridge：在不复制 Topos codec 的前提下交换 GBR10 图像。

这是可验证的外部工具适配器，不是 FFmpeg 原生 demuxer/encoder 插件：
- ``encode``：FFmpeg 读取 PNG/EXR 等图片并输出 gbrp10le rawvideo，Topos C ABI
  编码并写入 `.toos`；
- ``decode``：Topos C ABI 解码 `.toos`，FFmpeg 把 gbrp10le rawvideo 写回 PNG/EXR。

v1 能力边界：GBR 4:4:4、10-bit、无 alpha、单帧。YUV 4:2:2、12-bit、Alpha
和 Topos MOV 不在这个 bridge 的隐式转换范围内，遇到时显式失败。
"""
from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
from pathlib import Path

import numpy as np

from src.shared.ffmpeg_locator import find_ffmpeg, find_ffprobe
from src.shared.codec.topos_binding import (
    TOPOS_CODEC_ABI_VERSION,
    ToposCodec,
    _CFrameConfig,
    load_library,
)
from src.shared.codec.topos_image_binding import ToposImageCodec


def _ffmpeg(args: list[str], input_bytes: bytes | None = None) -> bytes:
    command = [find_ffmpeg() or "ffmpeg", "-hide_banner", "-loglevel", "error", *args]
    proc = subprocess.run(
        command,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"FFmpeg 失败 ({proc.returncode}): {detail}")
    return proc.stdout


def _codec_config(width: int, height: int) -> _CFrameConfig:
    config = _CFrameConfig()
    config.struct_size = ctypes.sizeof(_CFrameConfig)
    config.abi_version = TOPOS_CODEC_ABI_VERSION
    config.visible_width = width
    config.visible_height = height
    config.profile = 5  # GBR 4:4:4 10-bit
    config.pixel_format = 2
    config.bit_depth = 10
    config.qmatrix_id = 1
    config.qp_base = 20
    config.color_range = 1
    config.color_primaries = 1
    config.color_transfer = 1
    config.color_matrix = 0  # GBR identity
    config.sar_num = 1
    config.sar_den = 1
    return config


def encode(input_path: Path, output_path: Path, library: str | None) -> dict:
    raw = _ffmpeg([
        "-i", str(input_path),
        "-frames:v", "1",
        "-f", "rawvideo",
        "-pix_fmt", "gbrp10le",
        "pipe:1",
    ])
    # ffprobe avoids trusting a caller-supplied size and keeps FFmpeg as the
    # external image reader of this adapter.
    probe = subprocess.run(
        [find_ffprobe() or "ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0",
         str(input_path)],
        capture_output=True, text=True, check=False,
    )
    if probe.returncode != 0:
        raise RuntimeError(f"ffprobe 失败: {probe.stderr.strip()}")
    width, height = (int(x) for x in probe.stdout.strip().split(",", 1))
    expected = 3 * width * height * np.dtype("<u2").itemsize
    if len(raw) != expected:
        raise RuntimeError(f"FFmpeg rawvideo 大小不匹配: {len(raw)} != {expected}")
    planes = np.frombuffer(raw, dtype="<u2").reshape(3, height, width)
    codec = ToposCodec(load_library(library) if library else None)
    packet = codec.encode(_codec_config(width, height), [planes[0], planes[1], planes[2]])
    image_codec = ToposImageCodec(library)
    file_bytes = image_codec.write_packet(str(output_path), packet, image_profile=1)
    return {"operation": "encode", "width": width, "height": height,
            "packet_bytes": len(packet), "file_bytes": file_bytes,
            "profile": "hq-gbr10"}


def decode(input_path: Path, output_path: Path, library: str | None) -> dict:
    image_codec = ToposImageCodec(library)
    info = image_codec.probe(str(input_path))
    if (info.pixel_format, info.bit_depth, info.has_alpha) != (2, 10, False):
        raise RuntimeError(
            "FFmpeg bridge v1 只支持 GBR 4:4:4 10-bit 无 alpha .toos "
            f"(got pf={info.pixel_format} bd={info.bit_depth} alpha={info.has_alpha})")
    planes, _, _ = image_codec.decode(str(input_path))
    raw = b"".join(np.ascontiguousarray(p, dtype="<u2").tobytes() for p in planes[:3])
    _ffmpeg([
        "-f", "rawvideo", "-pix_fmt", "gbrp10le",
        "-s:v", f"{info.visible_width}x{info.visible_height}",
        "-i", "pipe:0", "-frames:v", "1", "-y", str(output_path),
    ], input_bytes=raw)
    return {"operation": "decode", "width": int(info.visible_width),
            "height": int(info.visible_height), "pixel_format": "gbrp10le",
            "output": str(output_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("encode", "decode"))
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--library", help="libtopos_codec path")
    args = parser.parse_args()
    if not args.input.is_file():
        parser.error(f"input does not exist: {args.input}")
    result = (encode(args.input, args.output, args.library)
              if args.operation == "encode"
              else decode(args.input, args.output, args.library))
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
