#!/usr/bin/env python3
"""Generate a small, reproducible Topos test-media corpus from real sources.

.. deprecated:: 2026-09-27（C02）
   规范真实媒体语料已迁移到 ``tools/generate_topos_baseline_corpus.py``：
   2K/4K × 六档 ProRes + TOPOS、487 帧 @25fps、带 pcm_s24le 音轨，命名
   ``{2K,4K}_topos_{422_LT,422_Proxy,422,422_HQ,4444,4444_XQ}.mov``，指纹
   冻结在 ``tests/test_videos/topos_prores_baseline_manifest.json``。
   本脚本输出的 96 帧旧命名（``2K_topos_proxy.mov`` 等）与在库语料冲突，
   真实媒体测试已全部改用规范名（tests/real_media.py 统一映射）。
   保留本脚本仅供离线小语料实验，请勿再用于 tests/test_videos。

The generated files are intentionally local test assets (``tests/test_videos``
is git-ignored).  The source is decoded one frame at a time and fed directly
to the product ``ToposVideoEncoder`` planar path, so this exercises the same
rate-control, rANS2 and MOV mux path used by the application.

Default corpus:

* 2K and 4K, 96 frames at 25 fps (3.84 seconds);
* 422 Proxy/LT/Standard/HQ from the matching ProRes 422 HQ source;
* 4444 from DNxHR 444 10-bit and 4444 XQ from DNxHR 444 12-bit;
* compatibility copies ``2K_topos.mov`` and ``4K_topos.mov`` point to the
  newly generated HQ files because older tools use those canonical paths.

Usage::

    .venv/bin/python tools/generate_topos_test_media.py
    .venv/bin/python tools/generate_topos_test_media.py --frames 120
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable, Sequence

import av
import numpy as np

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.shared.codec.color_metadata_config import ColorMetadataConfig  # noqa: E402
from src.shared.codec.topos_binding import (  # noqa: E402
    ToposCodec,
    ToposDecoder,
    ToposMovieFile,
)
from src.shared.export.topos_encoder import ToposVideoEncoder  # noqa: E402


FPS = Fraction(25, 1)


@dataclass(frozen=True)
class Case:
    name: str
    width: int
    height: int
    source_422: str
    source_444_10: str
    source_444_12: str


CASES: tuple[Case, ...] = (
    Case(
        "2K", 1920, 1080,
        "tests/test_videos/2K_prores422HQ.mov",
        "tests/test_videos/2K_DNxHR 444 10bit.mov",
        "tests/test_videos/2K_DNxHR 444 12bit.mov",
    ),
    Case(
        "4K", 3840, 2160,
        "tests/test_videos/4K_prores422HQ.mov",
        "tests/test_videos/4K_DNxHR 444 10bit.mov",
        "tests/test_videos/4K_DNxHR 444 12bit.mov",
    ),
)


TIERS: tuple[tuple[str, str, str], ...] = (
    ("proxy", "yuv422p10le", "422"),
    ("lt", "yuv422p10le", "422"),
    ("standard", "yuv422p10le", "422"),
    ("hq", "yuv422p10le", "422"),
    ("4444", "yuv444p10le", "444"),
    ("4444xq", "yuv444p12le", "444"),
)

EXPECTED = {
    "proxy": (3, 0, 10),
    "lt": (3, 0, 10),
    "standard": (3, 0, 10),
    "hq": (3, 0, 10),
    "4444": (5, 1, 10),
    "4444xq": (6, 1, 12),
}

#: --alpha 模式下携带 alpha 平面的档位（T-07/T-10 alpha 链路验证）。
#: 只覆盖 4444（yuv444p10le → topos_yuva444p10a16，alpha mode1 无损）；
#: 4444xq 的 12-bit alpha 变体如需支持须先核对 _PLANAR_FMT_4 白名单。
ALPHA_TIERS = ("4444",)
ALPHA_PIX_FMT = "topos_yuva444p10a16"
#: alpha mode1 = 无损 16-bit（topos_encoder 的 a16 映射）。
ALPHA_EXPECTED_MODE = 1


@dataclass
class PlanarFrame:
    """Minimal product-frame protocol consumed by ToposVideoEncoder."""

    planes: tuple[np.ndarray, ...]
    pixel_format: str
    width: int
    height: int
    frame_index: int
    is_planar: bool = True


def _plane_array(plane: av.VideoPlane) -> np.ndarray:
    """Copy one PyAV plane into a tightly packed little-endian uint16 array."""
    arr = np.ndarray(
        (plane.height, plane.width),
        dtype="<u2",
        buffer=memoryview(plane),
        strides=(plane.line_size, 2),
    )
    return np.ascontiguousarray(arr)


def _decode_frames(
    source: Path,
    pixel_format: str,
    width: int,
    height: int,
    count: int,
) -> Iterable[PlanarFrame]:
    """Decode ``count`` frames while retaining only the current frame."""
    with av.open(str(source)) as container:
        stream = container.streams.video[0]
        for index, frame in enumerate(container.decode(stream)):
            if index >= count:
                break
            if frame.format.name != pixel_format:
                frame = frame.reformat(format=pixel_format)
            expected_widths = (
                (width, (width + 1) // 2, (width + 1) // 2)
                if pixel_format.startswith("yuv422")
                else (width, width, width)
            )
            if len(frame.planes) < 3:
                raise RuntimeError(
                    f"{source}: {frame.format.name} has fewer than three planes"
                )
            planes = tuple(_plane_array(p) for p in frame.planes[:3])
            expected_shapes = tuple((height, w) for w in expected_widths)
            if tuple(p.shape for p in planes) != expected_shapes:
                raise RuntimeError(
                    f"{source}: decoded plane geometry {tuple(p.shape for p in planes)} "
                    f"!= {expected_shapes} ({pixel_format})"
                )
            yield PlanarFrame(planes, pixel_format, width, height, index)


def _synthetic_alpha_plane(width: int, height: int, frame_index: int) -> np.ndarray:
    """确定性合成 alpha 平面（uint16 全量程）。

    水平斜坡 × 棋盘 + 随帧漂移的条带：同时覆盖部分透明、全透明与
    全不透明区域，且逐帧变化（可验证 alpha 的时间维 roundtrip）。
    源素材（DNxHR 444）不带 alpha，管线验证只需要语义正确的 alpha，
    不需要内容真实感。
    """
    ys, xs = np.mgrid[0:height, 0:width]
    ramp = (xs.astype(np.uint32) * 65535) // max(1, width - 1)
    checker = np.where(((xs >> 5) + (ys >> 5)) % 2 == 0, 65535, 8192)
    drift = np.where(((ys + frame_index * 8) % 128) < 64, 1, 0)
    alpha = (ramp // 2) + ((checker - 8192) // 2) * (1 + drift)
    return np.clip(alpha, 0, 65535).astype("<u2")


def _encoder_config(
    case: Case, tier: str, pixel_format: str, alpha: bool = False,
) -> SimpleNamespace:
    if alpha:
        if tier not in ALPHA_TIERS:
            raise ValueError(f"tier {tier} 不支持 --alpha（仅 {ALPHA_TIERS}）")
        pixel_format = ALPHA_PIX_FMT
    return SimpleNamespace(
        width=case.width,
        height=case.height,
        fps=FPS,
        pix_fmt=pixel_format,
        profile=tier,
        crf=None,
        has_alpha=alpha,
        entropy_mode="rans2",
        aq_mode="off",
        rdo_mode="off",
        gop="intra",
        color_metadata=ColorMetadataConfig.sdr(),
    )


def _output_path(out_dir: Path, case: Case, tier: str) -> Path:
    return out_dir / f"{case.name}_topos_{tier}.mov"


def _remove_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _encode_group(
    case: Case,
    source: Path,
    pixel_format: str,
    tiers: Sequence[str],
    frames: int,
    out_dir: Path,
    threads: int,
    replace: bool,
    alpha: bool = False,
) -> list[Path]:
    """Decode one source once and fan each planar frame into its tier encoders."""
    outputs = [_output_path(out_dir, case, tier) for tier in tiers]
    if not replace and any(path.exists() for path in outputs):
        existing = [path.name for path in outputs if path.exists()]
        raise RuntimeError(
            f"输出已存在（使用默认替换或删除后重试）: {', '.join(existing)}"
        )
    for path in outputs:
        _remove_if_present(path)
        _remove_if_present(Path(str(path) + ".topos-tmp"))

    encoders: list[ToposVideoEncoder] = []
    started = time.perf_counter()
    try:
        for tier in tiers:
            encoder = ToposVideoEncoder(
                str(_output_path(out_dir, case, tier)),
                _encoder_config(case, tier, pixel_format, alpha=alpha),
                slice_threads=threads,
                slice_rows=16,
            )
            encoder.open()
            encoders.append(encoder)

        decoded = 0
        for frame in _decode_frames(source, pixel_format, case.width, case.height, frames):
            alpha_plane = (
                _synthetic_alpha_plane(case.width, case.height, frame.frame_index)
                if alpha else None
            )
            for encoder, tier in zip(encoders, tiers):
                if alpha_plane is not None and tier in ALPHA_TIERS:
                    encoder.encode_frame(PlanarFrame(
                        planes=frame.planes + (alpha_plane,),
                        pixel_format=ALPHA_PIX_FMT,
                        width=case.width,
                        height=case.height,
                        frame_index=frame.frame_index,
                    ))
                else:
                    encoder.encode_frame(frame)
            decoded += 1
            if decoded % 16 == 0 or decoded == frames:
                print(
                    f"  {case.name} {pixel_format} "
                    f"{decoded}/{frames} 帧",
                    flush=True,
                )
        if decoded != frames:
            raise RuntimeError(
                f"{source}: 仅解码 {decoded} 帧，要求 {frames} 帧"
            )
        for encoder in encoders:
            encoder.close()
    except Exception:
        for encoder in reversed(encoders):
            try:
                if encoder.is_open:
                    encoder.abort()
            except Exception:
                pass
        for path in outputs:
            _remove_if_present(path)
            _remove_if_present(Path(str(path) + ".topos-tmp"))
        raise

    elapsed = time.perf_counter() - started
    print(
        f"  完成 {case.name} {','.join(tiers)}：{frames / elapsed:.1f} 帧/秒 "
        f"（{elapsed:.2f}s，源 {source.name}）",
        flush=True,
    )
    return outputs


def _validate(path: Path, tier: str, case: Case, frames: int, codec: ToposCodec,
              alpha: bool = False) -> dict:
    """Validate MOV metadata and decode both ends of each output."""
    expected_profile, expected_pf, expected_bd = EXPECTED[tier]
    expected_alpha = ALPHA_EXPECTED_MODE if (alpha and tier in ALPHA_TIERS) else 0
    movie = ToposMovieFile(codec, str(path))
    try:
        info = movie.info()
        checks = {
            "dimensions": (int(info.width), int(info.height)) == (case.width, case.height),
            "sample_count": int(info.sample_count) == frames,
            "profile": int(info.profile) == expected_profile,
            "pixel_format": int(info.pixel_format) == expected_pf,
            "bit_depth": int(info.bit_depth) == expected_bd,
            "bitstream_major": int(info.bitstream_major) == 7,
            # T-07 关口：alpha 档必须真的带 alpha（此前 4444 档 alpha_mode=0 缺口）
            "alpha_mode": int(getattr(info, "alpha_mode", 0) or 0) == expected_alpha,
        }
        first_buf, first_size = movie.packet_into(0)
        with ToposDecoder(codec) as decoder:
            frame_info = decoder.prepare(first_buf, first_size)
            planes = []
            for plane_index in range(int(frame_info.plane_count)):
                pw, ph = codec.plane_geometry(frame_info, plane_index)
                planes.append(np.empty((ph, pw), dtype="<u2"))
            views = decoder.make_views(planes)
            decoder.decode_views(first_buf, views, int(frame_info.plane_count), first_size)
            # ``packet_into`` reuses an internal MOV buffer.  Decode the first
            # packet before fetching the last one, otherwise the second read
            # would overwrite ``first_buf`` while it is still referenced.
            last_buf, last_size = movie.packet_into(frames - 1)
            decoder.decode_views(last_buf, views, int(frame_info.plane_count), last_size)
        checks["decode_first_last"] = True
        if not all(checks.values()):
            raise RuntimeError(f"验证失败 {path.name}: {checks}")
        return {
            "path": str(path.relative_to(REPO)),
            "tier": tier,
            "resolution": [case.width, case.height],
            "frames": int(info.sample_count),
            "fps": [FPS.numerator, FPS.denominator],
            "profile": int(info.profile),
            "pixel_format": int(info.pixel_format),
            "bit_depth": int(info.bit_depth),
            "bitstream_major": int(info.bitstream_major),
            "alpha_mode": int(getattr(info, "alpha_mode", 0) or 0),
            "bytes": path.stat().st_size,
            "mbps": path.stat().st_size * 8.0 / (frames / float(FPS)) / 1e6,
            "first_packet_bytes": int(first_size),
            "last_packet_bytes": int(last_size),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "checks": checks,
        }
    finally:
        movie.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--frames", type=int, default=96,
                        help="每个输出的帧数（默认 96 = 3.84 秒@25fps）")
    parser.add_argument("--threads", type=int, default=16,
                        help="Topos slice threads（默认 16）")
    parser.add_argument("--output-dir", type=Path,
                        default=REPO / "tests" / "test_videos")
    parser.add_argument("--no-replace", action="store_true",
                        help="输出已存在时失败，而不是替换")
    parser.add_argument("--alpha", action="store_true",
                        help=f"为 {ALPHA_TIERS} 档编码 alpha 平面"
                             f"（{ALPHA_PIX_FMT}，alpha mode{ALPHA_EXPECTED_MODE}）")
    parser.add_argument("--only-tier", action="append", default=[],
                        help="只生成指定档位（可重复）；缺省生成全部档位")
    args = parser.parse_args()
    if args.frames <= 0 or args.threads <= 0:
        parser.error("--frames 和 --threads 必须为正数")

    out_dir = args.output_dir if args.output_dir.is_absolute() else REPO / args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    replace = not args.no_replace
    only_tiers = set(args.only_tier) if args.only_tier else None

    for case in CASES:
        sources = [
            REPO / case.source_422,
            REPO / case.source_444_10,
            REPO / case.source_444_12,
        ]
        missing = [str(path) for path in sources if not path.is_file()]
        if missing:
            raise SystemExit("找不到源素材：\n" + "\n".join(missing))

    print(
        f"生成 Topos 测试素材：{args.frames} 帧 @ {FPS}，输出目录 {out_dir}",
        flush=True,
    )
    generated: list[Path] = []
    for case in CASES:
        if only_tiers is None or only_tiers & {"proxy", "lt", "standard", "hq"}:
            generated += _encode_group(
                case,
                REPO / case.source_422,
                "yuv422p10le",
                tuple(t for t in ("proxy", "lt", "standard", "hq")
                      if only_tiers is None or t in only_tiers),
                args.frames,
                out_dir,
                args.threads,
                replace,
            )
        if only_tiers is None or "4444" in only_tiers:
            generated += _encode_group(
                case,
                REPO / case.source_444_10,
                "yuv444p10le",
                ("4444",),
                args.frames,
                out_dir,
                args.threads,
                replace,
                alpha=args.alpha,
            )
        if only_tiers is None or "4444xq" in only_tiers:
            generated += _encode_group(
                case,
                REPO / case.source_444_12,
                "yuv444p12le",
                ("4444xq",),
                args.frames,
                out_dir,
                args.threads,
                replace,
            )

    # Remove stale legacy Topos assets only after the replacement corpus is valid.
    codec = ToposCodec()
    assets = []
    for path in generated:
        tier = path.stem.rsplit("_", 1)[-1]
        case = next(c for c in CASES if path.name.startswith(c.name + "_"))
        entry = _validate(path, tier, case, args.frames, codec,
                          alpha=args.alpha)
        assets.append(entry)
        print(
            f"  验证通过 {path.name}: {entry['bytes'] / 1e6:.2f} MB, "
            f"{entry['mbps']:.1f} Mbps, profile={entry['profile']}, "
            f"p{entry['pixel_format']} {entry['bit_depth']}bit",
            flush=True,
        )

    aliases = []
    for case in CASES:
        source = _output_path(out_dir, case, "hq")
        alias = out_dir / f"{case.name}_topos.mov"
        _remove_if_present(alias)
        shutil.copy2(source, alias)
        aliases.append({
            "path": str(alias.relative_to(REPO)),
            "points_to": str(source.relative_to(REPO)),
            "sha256": hashlib.sha256(alias.read_bytes()).hexdigest(),
            "bytes": alias.stat().st_size,
        })

    removed = []
    for name in (
        "4K_prores422HQ.topos",
        "2K_h264.topos",
    ):
        stale = out_dir / name
        if stale.exists() or stale.is_symlink():
            _remove_if_present(stale)
            removed.append(str(stale.relative_to(REPO)))

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator": str(Path(__file__).relative_to(REPO)),
        "frames": args.frames,
        "fps": [FPS.numerator, FPS.denominator],
        "threads": args.threads,
        "encoder": "ToposVideoEncoder",
        "entropy_mode": "rans2",
        "container": "MOV",
        "sources": [
            {
                "resolution": case.name,
                "422": case.source_422,
                "444_10bit": case.source_444_10,
                "444_12bit": case.source_444_12,
            }
            for case in CASES
        ],
        "assets": assets,
        "compatibility_aliases": aliases,
        "removed_legacy_assets": removed,
    }
    manifest_path = out_dir / "topos_test_media_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"\n生成完成：{len(assets)} 个分档 + {len(aliases)} 个兼容别名", flush=True)
    print(f"清单：{manifest_path}", flush=True)
    if removed:
        print("已删除旧素材：" + ", ".join(removed), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
