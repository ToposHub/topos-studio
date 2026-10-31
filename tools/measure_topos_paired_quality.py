#!/usr/bin/env python3
"""同一无损 planar 输入同时编码 TOPOS/ProRes，再对源逐帧评分。

这是 Q01 的最小可复现测量路径。当前支持无 Alpha 的 10-bit 422 与
12-bit 444；12-bit ProRes 需 VideoToolbox 的 p416le 入口，且输入转换
必须逐字节往返一致。RAW、half-float、Alpha 使用后续专属原生域工具。报告仅是
测量结果，不自动宣称优于 ProRes。输入须是紧密排列、little-endian u16
的 Y、U、V 平面逐帧串接，码值 LSB 对齐。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import av
import numpy as np

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.shared.codec.color_metadata_config import ColorMetadataConfig  # noqa: E402
from src.shared.codec.topos_binding import (  # noqa: E402
    ToposCodec, ToposDecoder, ToposMovieFile,
)
from src.shared.export.topos_encoder import ToposVideoEncoder  # noqa: E402

TIERS = {
    "proxy": ("yuv422p10le", 0),
    "lt": ("yuv422p10le", 1),
    "standard": ("yuv422p10le", 2),
    "hq": ("yuv422p10le", 3),
    "4444": ("yuv444p12le", 4),
    "4444xq": ("yuv444p12le", 5),
}


@dataclass(frozen=True)
class PlanarFrame:
    planes: tuple[np.ndarray, np.ndarray, np.ndarray]
    pixel_format: str
    width: int
    height: int
    frame_index: int
    is_planar: bool = True


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _geometry(width: int, height: int, pix_fmt: str) -> tuple[tuple[int, int], ...]:
    cw = (width + 1) // 2 if "422" in pix_fmt else width
    return ((height, width), (height, cw), (height, cw))


def _raw_frames(path: Path, width: int, height: int, pix_fmt: str,
                count: int) -> Iterator[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    shapes = _geometry(width, height, pix_fmt)
    frame_bytes = sum(h * w * 2 for h, w in shapes)
    if path.stat().st_size != frame_bytes * count:
        raise ValueError(
            f"原始平面大小 {path.stat().st_size} != {frame_bytes}×{count}"
        )
    with path.open("rb") as source:
        for _ in range(count):
            planes = []
            for h, w in shapes:
                buf = source.read(h * w * 2)
                if len(buf) != h * w * 2:
                    raise EOFError("原始平面提前结束")
                planes.append(np.frombuffer(buf, dtype="<u2").reshape(h, w))
            yield tuple(planes)


def _validate_source_domain(path: Path, width: int, height: int,
                            pix_fmt: str, count: int, bits: int) -> None:
    """逐帧逐平面核对原始输入位深，防止两侧各自静默截断。"""
    limit = (1 << bits) - 1
    for frame_index, planes in enumerate(
        _raw_frames(path, width, height, pix_fmt, count)
    ):
        for plane_index, plane in enumerate(planes):
            maximum = int(plane.max(initial=0))
            if maximum > limit:
                raise ValueError(
                    f"源帧 {frame_index} 平面 {plane_index} 最大码值 "
                    f"{maximum} 超出 {bits}-bit 范围 0..{limit}"
                )


def _av_plane(plane: av.video.plane.VideoPlane) -> np.ndarray:
    return np.ascontiguousarray(np.ndarray(
        (plane.height, plane.width), dtype="<u2",
        buffer=memoryview(plane), strides=(plane.line_size, 2),
    ))


def _prores_frames(path: Path, pix_fmt: str) -> Iterator[tuple[np.ndarray, ...]]:
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        for frame in container.decode(stream):
            # ProRes 4444 必带 opaque Alpha，颜色三平面仍是原生 12-bit。
            acceptable = {pix_fmt}
            if pix_fmt == "yuv444p12le":
                acceptable.add("yuva444p12le")
            if frame.format.name not in acceptable:
                raise ValueError(
                    f"ProRes 解码格式 {frame.format.name} 不匹配输入 {pix_fmt}"
                )
            yield tuple(_av_plane(p) for p in frame.planes[:3])


def _topos_frames(codec: ToposCodec, path: Path) -> Iterator[tuple[np.ndarray, ...]]:
    movie = ToposMovieFile(codec, str(path))
    decoder = ToposDecoder(codec)
    try:
        for index in range(int(movie.info().sample_count)):
            packet, size = movie.packet_into(index)
            info = decoder.prepare(packet, size)
            planes = []
            for channel in range(3):
                width, height = codec.plane_geometry(info, channel)
                planes.append(np.empty((height, width), dtype="<u2"))
            views = decoder.make_views(planes)
            decoder.decode_views(packet, views, 3, size)
            yield tuple(planes)
    finally:
        decoder.close()
        movie.close()


def _frame_error(reference: tuple[np.ndarray, ...],
                 candidate: tuple[np.ndarray, ...], bits: int) -> dict:
    channels: dict[str, dict[str, float]] = {}
    for name, expected, actual in zip("yuv", reference, candidate):
        if expected.shape != actual.shape:
            raise ValueError(f"{name} 平面形状不一致: {actual.shape} != {expected.shape}")
        delta = actual.astype(np.int32) - expected.astype(np.int32)
        mse = float(np.mean(np.square(delta.astype(np.float64))))
        channels[name] = {
            "psnr_db": math.inf if mse == 0 else 10 * math.log10(
                ((1 << bits) - 1) ** 2 / mse),
            "mae_codes": float(np.mean(np.abs(delta))),
            "max_abs_codes": int(np.max(np.abs(delta))),
        }
    return channels


def _video_payload_bytes(path: Path) -> int:
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        return sum(packet.buffer_size for packet in container.demux(stream)
                   if packet.dts is not None)


def _encode_topos(source: Path, dest: Path, width: int, height: int,
                  count: int, fps: int, tier: str, pix_fmt: str,
                  threads: int, crf: int | None,
                  bitrate_bps: int | None) -> float:
    config = SimpleNamespace(
        width=width, height=height, fps=Fraction(fps, 1),
        pix_fmt=pix_fmt, profile=tier, crf=crf, bitrate=bitrate_bps,
        has_alpha=False,
        entropy_mode="rans2", aq_mode="off", rdo_mode="off", gop="intra",
        color_metadata=ColorMetadataConfig.sdr(),
    )
    encoder = ToposVideoEncoder(str(dest), config, slice_threads=threads)
    started = time.perf_counter()
    try:
        encoder.open()
        for index, planes in enumerate(_raw_frames(
            source, width, height, pix_fmt, count,
        )):
            encoder.encode_frame(PlanarFrame(
                planes, pix_fmt, width, height, index,
            ))
        encoder.close()
    except BaseException:
        if encoder.is_open:
            encoder.abort()
        raise
    return time.perf_counter() - started


def _encode_prores(ffmpeg: str, source: Path, dest: Path, width: int,
                   height: int, count: int, fps: int, pix_fmt: str,
                   profile: int, encoder: str,
                   allow_sw: bool) -> tuple[float, list[str]]:
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
        "-pixel_format", pix_fmt, "-video_size", f"{width}x{height}",
        "-framerate", str(fps), "-i", str(source), "-frames:v", str(count),
        "-c:v", encoder,
    ]
    if encoder == "prores_videotoolbox" and allow_sw:
        command.extend(["-allow_sw", "1"])
    command.extend([
        "-profile:v", str(profile), "-pix_fmt", pix_fmt,
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-colorspace", "bt709", "-color_range", "tv", "-y", str(dest),
    ])
    started = time.perf_counter()
    subprocess.run(command, check=True)
    return time.perf_counter() - started, command


def _prepare_vt_12bit_input(ffmpeg: str, source: Path, output: Path,
                            width: int, height: int, count: int,
                            fps: int) -> dict:
    """显式冻结 p416 输入并逐字节验证 12→16→12 转换没有改变源。"""
    packed = output / "prores_input_p416le.raw"
    restored = output / "prores_input_roundtrip_12bit.raw"
    base = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "rawvideo"]
    forward = base + [
        "-pixel_format", "yuv444p12le", "-video_size", f"{width}x{height}",
        "-framerate", str(fps), "-i", str(source), "-frames:v", str(count),
        "-pix_fmt", "p416le", "-f", "rawvideo", "-y", str(packed),
    ]
    reverse = base + [
        "-pixel_format", "p416le", "-video_size", f"{width}x{height}",
        "-framerate", str(fps), "-i", str(packed), "-frames:v", str(count),
        "-pix_fmt", "yuv444p12le", "-f", "rawvideo", "-y", str(restored),
    ]
    subprocess.run(forward, check=True)
    subprocess.run(reverse, check=True)
    try:
        if _sha256(restored) != _sha256(source):
            raise ValueError("12-bit planar ↔ p416le 转换非逐字节一致；拒绝比较")
    finally:
        restored.unlink(missing_ok=True)
    return {
        "source_format": "yuv444p12le", "encoder_input_format": "p416le",
        "encoder_input_sha256": _sha256(packed),
        "roundtrip_bit_exact": True,
        "forward_command": forward, "reverse_command": reverse,
    }


def _require_encoder_input_format(ffmpeg: str, encoder: str,
                                  pixel_format: str) -> None:
    """拒绝 FFmpeg 静默插入位深/色度转换造成的伪同规格比较。"""
    probe = subprocess.run(
        [ffmpeg, "-hide_banner", "-h", f"encoder={encoder}"],
        capture_output=True, text=True, check=True,
    )
    for line in (probe.stdout + probe.stderr).splitlines():
        if "Supported pixel formats:" in line:
            formats = line.split("Supported pixel formats:", 1)[1].split()
            if pixel_format in formats:
                return
            raise ValueError(
                f"{encoder} 不接受原生 {pixel_format} 输入（只接受 {formats}）；"
                "FFmpeg 会自动转换位深，不能作为同规格 ProRes 画质对照"
            )
    raise ValueError(f"无法确认 {encoder} 支持的输入像素格式；拒绝测量")


def run(args: argparse.Namespace) -> dict:
    if args.width <= 0 or args.height <= 0 or args.frames <= 0 or args.fps <= 0:
        raise ValueError("宽高、帧数和帧率必须为正数")
    if args.threads <= 0:
        raise ValueError("线程数必须为正数")
    if args.topos_crf is not None and not 0 <= args.topos_crf <= 63:
        raise ValueError("--topos-crf 必须在 0..63")
    if args.topos_crf is not None and args.match_prores_bitrate:
        raise ValueError("固定 QP 与 ProRes 目标码率互斥")
    if args.source_origin == "decoded-lossy-source" and args.source_parent is None:
        raise ValueError("有损源解码得到的原始平面必须提供 --source-parent")
    pix_fmt, profile = TIERS[args.tier]
    bits = 10 if "10le" in pix_fmt else 12
    ffmpeg = shutil.which(args.ffmpeg)
    if ffmpeg is None:
        raise FileNotFoundError(f"FFmpeg 不存在: {args.ffmpeg}")
    if bits == 12 and args.prores_encoder != "prores_videotoolbox":
        _require_encoder_input_format(ffmpeg, args.prores_encoder, pix_fmt)
    elif bits == 12:
        _require_encoder_input_format(ffmpeg, args.prores_encoder, "p416le")
    else:
        _require_encoder_input_format(ffmpeg, args.prores_encoder, pix_fmt)
    output = args.out_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    top_path = output / f"topos_{args.tier}.mov"
    pro_path = output / f"prores_{args.tier}.mov"
    report_path = output / f"paired_{args.tier}.json"
    if any(p.exists() for p in (top_path, pro_path, report_path)):
        raise FileExistsError("输出已存在；请指定新的 --out-dir，避免覆盖既有证据")
    source = args.source_raw.resolve(strict=True)
    source_sha = _sha256(source)
    parent = args.source_parent.resolve(strict=True) if args.source_parent else None
    _validate_source_domain(source, args.width, args.height, pix_fmt,
                            args.frames, bits)
    pro_source = source
    pro_pix_fmt = pix_fmt
    input_conversion = None
    if bits == 12 and args.prores_encoder == "prores_videotoolbox":
        input_conversion = _prepare_vt_12bit_input(
            ffmpeg, source, output, args.width, args.height,
            args.frames, args.fps,
        )
        pro_source = output / "prores_input_p416le.raw"
        pro_pix_fmt = "p416le"
    pro_elapsed, pro_cmd = _encode_prores(
        ffmpeg, pro_source, pro_path, args.width, args.height, args.frames,
        args.fps, pro_pix_fmt, profile, args.prores_encoder, args.allow_sw,
    )
    pro_bytes = _video_payload_bytes(pro_path)
    bitrate_bps = (round(pro_bytes * 8 * args.fps / args.frames)
                   if args.match_prores_bitrate else None)
    top_elapsed = _encode_topos(
        source, top_path, args.width, args.height, args.frames,
        args.fps, args.tier, pix_fmt, args.threads, args.topos_crf,
        bitrate_bps,
    )
    codec = ToposCodec()
    with ToposMovieFile(codec, str(top_path)) as movie:
        if int(movie.info().sample_count) != args.frames:
            raise ValueError("TOPOS 帧数与源不一致")
    measurements = []
    raw = _raw_frames(source, args.width, args.height, pix_fmt, args.frames)
    top = _topos_frames(codec, top_path)
    pro = _prores_frames(pro_path, pix_fmt)
    try:
        for index in range(args.frames):
            reference = next(raw)
            measurements.append({
                "frame": index,
                "topos": _frame_error(reference, next(top), bits),
                "prores": _frame_error(reference, next(pro), bits),
            })
        if next(top, None) is not None or next(pro, None) is not None:
            raise ValueError("输出帧数超过源")
    finally:
        top.close()
        pro.close()
    top_bytes = _video_payload_bytes(top_path)
    lib_path = Path(codec._lib._name).resolve(strict=True)
    report = {
        "status": "measurement_only_no_release_verdict",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "measurement_script_sha256": _sha256(Path(__file__)),
        "source_origin": args.source_origin,
        "source": str(source), "source_sha256": source_sha,
        "source_parent": (str(parent) if parent else None),
        "source_parent_sha256": (_sha256(parent) if parent else None),
        "format": pix_fmt, "tier": args.tier, "width": args.width,
        "height": args.height, "fps": args.fps, "frames": args.frames,
        "native_library": str(lib_path),
        "native_library_sha256": _sha256(lib_path),
        "topos": {"path": str(top_path), "sha256": _sha256(top_path),
                  "video_bytes": top_bytes, "encode_wall_s": top_elapsed,
                  "slice_threads": args.threads,
                  "crf_override": args.topos_crf,
                  "bitrate_target_bps": bitrate_bps,
                  "encoder": "ToposVideoEncoder"},
        "prores": {"path": str(pro_path), "sha256": _sha256(pro_path),
                   "video_bytes": pro_bytes, "encode_wall_s": pro_elapsed,
                   "encoder": f"ffmpeg/{args.prores_encoder}",
                   "allow_software_fallback": args.allow_sw,
                   "hardware_acceleration_confirmed": False,
                   "opaque_alpha_emitted": bits == 12,
                   "alpha_note": ("VideoToolbox 4444 解码为 yuva444p12le；"
                                  "输入无 alpha，TOPOS 侧无 alpha，载荷比较"
                                  "包含 ProRes 的 opaque alpha 成本"
                                  if bits == 12 else None),
                   "input_conversion": input_conversion,
                   "command": pro_cmd},
        "topos_over_prores_video_bytes": top_bytes / pro_bytes,
        "rate_matched_2pct": abs(top_bytes / pro_bytes - 1.0) <= 0.02,
        "per_frame": measurements,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2)
                           + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-raw", required=True, type=Path)
    parser.add_argument("--source-origin", required=True,
                        choices=("lossless-original", "decoded-lossy-source"))
    parser.add_argument("--source-parent", type=Path,
                        help="原始平面由有损素材解码时记录该素材完整指纹")
    parser.add_argument("--tier", required=True, choices=sorted(TIERS))
    parser.add_argument("--width", required=True, type=int)
    parser.add_argument("--height", required=True, type=int)
    parser.add_argument("--frames", required=True, type=int)
    parser.add_argument("--fps", type=int, default=25)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--topos-crf", type=int,
                        help="固定 QP 的实验点；省略时为产品默认码控")
    parser.add_argument("--match-prores-bitrate", action="store_true",
                        help="按 ProRes 实际视频载荷设 TOPOS 目标码率")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--prores-encoder", default="prores_ks",
                        choices=("prores_ks", "prores_videotoolbox"))
    parser.add_argument("--allow-sw", action="store_true",
                        help="VideoToolbox 不可用硬件时允许软件回退；报告不宣称硬件")
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    report = run(args)
    print(f"{report['tier']}: TOPOS/ProRes 视频载荷比 "
          f"{report['topos_over_prores_video_bytes']:.3f}; "
          f"逐帧报告 {args.out_dir / ('paired_' + args.tier + '.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
