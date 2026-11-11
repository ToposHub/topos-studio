# -*- coding: utf-8 -*-
"""CW-B00 冻结参考行为:三份真实素材 fixture 的确定性 probe/manifest/锚点帧。

对计划 docs/plans/2026-11-12-color-warper-backend-plan.md §5.4 的落实:
- 记录实际 codec、分辨率、帧率/timebase、时长、pix_fmt、颜色元数据、文件 sha256;
- 全量顺序解码,冻结首帧/中段帧/尾帧(帧索引、PTS、时间域、解码器、整帧像素 hash);
- 冻结一段连续播放区间(中段起 50 帧)的逐帧 PTS 与像素 hash;
- 输出 960px 宽 PNG 锚点图供人工比对(仅展示用,不是色彩基准)。

确定性说明:像素 hash 依赖 PyAV/FFmpeg 的 swscale 版本,manifest 记录构建信息;
后续阶段复用同一 manifest,不把不同解码版本差异算作 Warper 误差。

用法:
    .venv/bin/python scripts/probe_warper_fixtures_b00.py [--skip-siblings]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

import av
import numpy as np

REPO = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO / "tests" / "test_videos"
OUT_DIR = REPO / "docs" / "validation" / "color_warper_b00"
FRAMES_DIR = OUT_DIR / "frames"

CORE_FIXTURES = [
    "2K_h264.mp4",
    "2K_prores422.mov",
    "2K_DNxHR HQX 10bit.mov",  # 文件名含空格:仅经 pathlib 传参,不经 shell
]

INTERVAL_FRAMES = 50
ANCHOR_PNG_WIDTH = 960


def _file_sha256(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


# FFmpeg libavutil pixfmt.h 中的颜色枚举(PYAV 18 直接返回 int)。
_COLOR_SPACES = {0: "RGB", 1: "BT709", 2: "unspecified", 4: "FCC", 5: "BT470BG",
                 6: "SMPTE170M", 7: "SMPTE240M", 8: "YCGCO", 9: "BT2020NCL", 10: "BT2020CL"}
_COLOR_TRANSFERS = {1: "BT709", 2: "unspecified", 4: "BT470M", 6: "SMPTE170M",
                    7: "SMPTE240M", 8: "LINEAR", 13: "IEC61966_2_1(sRGB)",
                    16: "SMPTE2084(PQ)", 18: "ARIB_STD_B67(HLG)"}
_COLOR_PRIMARIES = {1: "BT709", 2: "unspecified", 4: "BT470M", 5: "BT470BG",
                    6: "SMPTE170M", 7: "SMPTE240M", 8: "FILM", 9: "BT2020"}
_COLOR_RANGES = {0: "unspecified", 1: "MPEG(limited)", 2: "JPEG(full)"}


def _enum_name(value: object, table: dict[int, str] | None = None) -> str:
    """PyAV 枚举可能以 int 形式返回;记录 int 与名字,便于跨版本比对。"""
    if isinstance(value, int):
        label = (table or {}).get(value)
        return f"{value}({label})" if label else str(value)
    name = getattr(value, "name", None)
    return f"{value}({name})" if name else str(value)


def _stream_info(stream: av.video.stream.VideoStream, container: av.container.InputContainer) -> dict:
    context = stream.codec_context
    average_rate = stream.average_rate
    return {
        "codec": context.name,
        "width": stream.width,
        "height": stream.height,
        "pix_fmt": context.pix_fmt,
        "bits_per_coded_sample": context.bits_per_coded_sample or None,
        "r_frame_rate": str(stream.base_rate),
        "average_rate": str(average_rate) if average_rate else None,
        "time_base": str(stream.time_base),
        "start_time": float(stream.start_time) if stream.start_time is not None else None,
        "duration_seconds": float(stream.duration * stream.time_base) if stream.duration else None,
        "nb_frames_metadata": stream.metadata.get("nb_frames"),
        "color_space": _enum_name(context.colorspace, _COLOR_SPACES),
        "color_transfer": _enum_name(context.color_trc, _COLOR_TRANSFERS),
        "color_primaries": _enum_name(context.color_primaries, _COLOR_PRIMARIES),
        "color_range": _enum_name(context.color_range, _COLOR_RANGES),
        "container_format": container.format.name or container.format.long_name,
        "container_metadata": dict(container.metadata),
    }


def _frame_record(frame: av.VideoFrame, index: int) -> dict:
    rgb = frame.reformat(format="rgb24")
    plane = rgb.planes[0]
    pixels = bytes(plane)
    return {
        "frame_index": index,
        "pts": frame.pts,
        "pts_seconds": float(frame.pts * frame.time_base) if frame.pts is not None else None,
        "time_base": str(frame.time_base),
        "pixel_sha256_rgb24": hashlib.sha256(pixels).hexdigest(),
        "width": frame.width,
        "height": frame.height,
    }


def _save_anchor_png(frame: av.VideoFrame, name: str) -> str:
    from PIL import Image

    image = frame.reformat(format="rgb24").to_image()
    if image.width > ANCHOR_PNG_WIDTH:
        height = round(image.height * ANCHOR_PNG_WIDTH / image.width)
        image = image.resize((ANCHOR_PNG_WIDTH, height), Image.LANCZOS)
    target = FRAMES_DIR / name
    image.save(target, format="PNG", optimize=True)
    return target.name


def probe_fixture(filename: str, with_hash: bool = True) -> dict:
    path = FIXTURE_DIR / filename
    if not path.exists():
        return {"file": filename, "error": "missing"}
    record: dict = {
        "file": filename,
        "file_bytes": path.stat().st_size,
    }
    if with_hash:
        record["file_sha256"] = _file_sha256(path)

    with av.open(str(path)) as container:
        video = container.streams.video[0]
        video.thread_type = "AUTO"
        record["stream"] = _stream_info(video, container)
        audio = container.streams.audio
        record["audio_streams"] = [
            {"codec": s.codec_context.name, "layout": str(s.codec_context.layout),
             "sample_rate": s.codec_context.sample_rate}
            for s in audio
        ]

        # 全量顺序解码:确定性拿到真实帧数与每帧 PTS。
        pts_series: list[int] = []
        first = last = None
        decoded = 0
        for frame in container.decode(video):
            pts_series.append(frame.pts if frame.pts is not None else -1)
            last = frame
            if first is None:
                first = frame
            decoded += 1

        record["decoded_frame_count"] = decoded
        record["pts_monotonic"] = all(b > a for a, b in zip(pts_series, pts_series[1:]))
        if not decoded:
            return record

        record["first_frame"] = _frame_record(first, 0)
        record["last_frame"] = _frame_record(last, decoded - 1)

        # 二次轻量定位中段帧与连续区间:重开容器顺序跳帧,避免 seek 不确定性。
        mid_index = decoded // 2
        middle = None
        interval: list[dict] = []
        with av.open(str(path)) as second:
            stream2 = second.streams.video[0]
            stream2.thread_type = "AUTO"
            for index, frame in enumerate(second.decode(stream2)):
                if index == mid_index:
                    middle = frame
                    record["mid_frame"] = _frame_record(frame, index)
                elif mid_index < index <= mid_index + INTERVAL_FRAMES:
                    interval.append(_frame_record(frame, index))
                elif index > mid_index + INTERVAL_FRAMES:
                    break
        record["playback_interval"] = {
            "note": f"中段帧之后连续 {len(interval)} 帧顺序解码",
            "frames": interval,
        }
        if middle is not None:
            record["anchor_pngs"] = {
                "first": _save_anchor_png(first, f"{path.stem}_first.png"),
                "mid": _save_anchor_png(middle, f"{path.stem}_mid.png"),
                "last": _save_anchor_png(last, f"{path.stem}_last.png"),
            }
    return record


def probe_siblings() -> list[dict]:
    """轻量记录其余素材(不做解码/hash),供 B08 性能阶段选材。"""
    siblings = []
    for entry in sorted(FIXTURE_DIR.iterdir()):
        if not entry.is_file() or entry.suffix.lower() not in {".mov", ".mp4"}:
            continue
        if entry.name in CORE_FIXTURES:
            continue
        try:
            with av.open(str(entry)) as container:
                video = container.streams.video[0]
                if video.codec_context is None:
                    siblings.append({"file": entry.name, "error": "no_codec_context"})
                    continue
                siblings.append({
                    "file": entry.name,
                    "file_bytes": entry.stat().st_size,
                    "codec": video.codec_context.name,
                    "size": f"{video.codec_context.coded_width}x{video.codec_context.coded_height}",
                    "pix_fmt": video.codec_context.pix_fmt,
                    "average_rate": str(video.average_rate) if video.average_rate else None,
                    "duration_seconds": float(video.duration * video.time_base) if video.duration else None,
                })
        except OSError:
            siblings.append({"file": entry.name, "error": "probe_failed"})
    return siblings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-siblings", action="store_true", help="跳过其余素材轻量清单")
    parser.add_argument("--skip-hash", action="store_true", help="跳过大文件 sha256(快速重跑)")
    args = parser.parse_args()

    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {
        "stage": "CW-B00",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "decoder": {
            "pyav": av.__version__,
            "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
            "python": sys.version.split()[0],
            "note": "pixel_sha256_rgb24 基于 rgb24 swscale 重排,跨解码器版本不保证逐位一致;跨版本差异须先比对本 manifest 再归因。",
        },
        "fixtures": [],
    }

    for name in CORE_FIXTURES:
        print(f"probing {name} ...", flush=True)
        manifest["fixtures"].append(probe_fixture(name, with_hash=not args.skip_hash))

    if not args.skip_siblings:
        print("probing siblings ...", flush=True)
        manifest["sibling_assets"] = probe_siblings()

    target = OUT_DIR / "fixtures_manifest.json"
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"manifest -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
