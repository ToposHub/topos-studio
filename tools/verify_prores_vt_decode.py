#!/usr/bin/env python3
"""用 AVFoundation 独立解码 ProRes，与 FFmpeg 12-bit 444 输出交叉核验。

macOS/PyObjC 专用。VideoToolbox 以 16-bit bi-planar CVPixelBuffer 交付，
按高 12 位转回原生码值；报告两个解码器逐平面的绝对差，不判定画质优劣。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import numpy as np

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.measure_topos_paired_quality import _prores_frames, _sha256  # noqa: E402


def _read_plane(buffer: Any, index: int, quartz: Any) -> np.ndarray:
    height = quartz.CVPixelBufferGetHeightOfPlane(buffer, index)
    row_bytes = quartz.CVPixelBufferGetBytesPerRowOfPlane(buffer, index)
    pointer = quartz.CVPixelBufferGetBaseAddressOfPlane(buffer, index)
    data = b"".join(
        b"".join(pointer[y * row_bytes:(y + 1) * row_bytes])
        for y in range(height)
    )
    return np.frombuffer(data, dtype="<u2").reshape(height, row_bytes // 2)


def _apple_frames(path: Path, count: int) -> Iterator[tuple[np.ndarray, ...]]:
    import AVFoundation
    import CoreMedia
    import Foundation
    import Quartz

    asset = AVFoundation.AVURLAsset.URLAssetWithURL_options_(
        Foundation.NSURL.fileURLWithPath_(str(path)), None,
    )
    tracks = asset.tracksWithMediaType_(AVFoundation.AVMediaTypeVideo)
    if len(tracks) != 1:
        raise ValueError(f"期望一条视频轨，得到 {len(tracks)}")
    reader, error = AVFoundation.AVAssetReader.assetReaderWithAsset_error_(
        asset, None,
    )
    if reader is None:
        raise RuntimeError(f"AVAssetReader 创建失败: {error}")
    pixel_format = Quartz.kCVPixelFormatType_444YpCbCr16BiPlanarVideoRange
    output = AVFoundation.AVAssetReaderTrackOutput.alloc().initWithTrack_outputSettings_(
        tracks[0], {Quartz.kCVPixelBufferPixelFormatTypeKey: pixel_format},
    )
    if not reader.canAddOutput_(output):
        raise RuntimeError("AVAssetReader 不接受 16-bit 444 输出设置")
    reader.addOutput_(output)
    if not reader.startReading():
        raise RuntimeError(f"AVAssetReader 启动失败: {reader.error()}")
    for index in range(count):
        sample = output.copyNextSampleBuffer()
        if sample is None:
            raise EOFError(f"AVFoundation 仅解码 {index} 帧: {reader.error()}")
        pixel_buffer = CoreMedia.CMSampleBufferGetImageBuffer(sample)
        if Quartz.CVPixelBufferGetPixelFormatType(pixel_buffer) != pixel_format:
            raise ValueError("AVFoundation 输出像素格式不是要求的 16-bit 444")
        if Quartz.CVPixelBufferGetPlaneCount(pixel_buffer) != 2:
            raise ValueError("AVFoundation 输出平面数不是 2")
        lock_flags = Quartz.kCVPixelBufferLock_ReadOnly
        if Quartz.CVPixelBufferLockBaseAddress(pixel_buffer, lock_flags) != 0:
            raise RuntimeError("CVPixelBuffer 加锁失败")
        try:
            y = _read_plane(pixel_buffer, 0, Quartz)
            uv = _read_plane(pixel_buffer, 1, Quartz)
        finally:
            Quartz.CVPixelBufferUnlockBaseAddress(pixel_buffer, lock_flags)
        width = Quartz.CVPixelBufferGetWidth(pixel_buffer)
        y = y[:, :width]
        uv = uv[:, :width * 2]
        yield tuple(
            (plane >> 4).astype("<u2", copy=False)
            for plane in (y, uv[:, 0::2], uv[:, 1::2])
        )


def verify(path: Path, frames: int) -> dict:
    path = path.resolve(strict=True)
    results = []
    apple = _apple_frames(path, frames)
    ffmpeg = _prores_frames(path, "yuv444p12le")
    try:
        for index in range(frames):
            native_planes = next(apple)
            ffmpeg_planes = next(ffmpeg)
            per_plane = {}
            for name, native, decoded in zip("yuv", native_planes, ffmpeg_planes):
                if native.shape != decoded.shape:
                    raise ValueError(f"帧 {index} {name} 形状不一致")
                delta = np.abs(native.astype(np.int32) - decoded.astype(np.int32))
                per_plane[name] = {
                    "mae_codes": float(delta.mean()),
                    "max_abs_codes": int(delta.max()),
                    "bit_exact": bool(np.array_equal(native, decoded)),
                }
            results.append({"frame": index, "planes": per_plane})
    finally:
        apple.close()
        ffmpeg.close()
    return {
        "status": "independent_decoder_agreement_only",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "file": str(path), "file_sha256": _sha256(path),
        "frames": frames, "apple_output": "p416le high 12 bits",
        "ffmpeg_output": "yuva444p12le first three planes",
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--frames", type=int, default=3)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.frames <= 0:
        parser.error("--frames 必须为正")
    if args.out.exists():
        raise FileExistsError(f"拒绝覆盖现有报告: {args.out}")
    result = verify(args.video, args.frames)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(f"交叉解码 {args.frames} 帧，报告 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
