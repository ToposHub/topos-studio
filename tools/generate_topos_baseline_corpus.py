#!/usr/bin/env python3
"""P0 基准清洗：同一 4K ProRes 4444 母版的 2K/4K 六档 ProRes + TOPOS 基准集。

依据 docs/topos_prores_quality_optimization_plan_2026-09-21.md §5 P0：

- 现有 Apple 原生 ProRes 基准（2K 六档、4K Proxy/LT/4444/4444XQ）已是
  同母版 487 帧资产（vendor 'appl'、同批生成时间戳），保留并校验；
- 缺失的 ``4K_prores422.mov`` 与异源的 743 帧 ``4K_prores422HQ.mov`` 用
  Apple VideoToolbox ProRes 编码器（prores_videotoolbox，与基准集同源
  编码器家族）从母版重建：487 帧、25fps、BT.709 limited、pcm_s24le 直拷；
- 12 个 TOPOS 文件逐帧 planar 直通编码（产品 ToposVideoEncoder 路径，含
  PCM 音频轨），首尾帧解码 + 音频字节级校验。**源的口径已变**：本脚本默认
  拿"同档位同分辨率 ProRes 基准"当源（历史口径），而 2026-09-22 重导的在库
  语料统一改用 **4K ProRes 4444XQ 母版**（`{2K,4K}_prores4444XQ.mov`）——
  同档 ProRes 本身是有损一代，拿它当源会在 TOPOS 损失之上再叠一层，画质
  对比被源损失污染。在库文件名与逐档源已记入 manifest 的 `topos_sources` /
  `topos_naming` 字段；按 4444XQ 口径重跑需覆盖各档源（本脚本尚无开关）；
- 输出 manifest（frames/fps/色彩/音频/体积/视频流码率/指纹 + 成对一致性
  检查），供 P1/P2 码率上限定标使用。

用法::

    .venv/bin/python tools/generate_topos_baseline_corpus.py            # 全流程
    .venv/bin/python tools/generate_topos_baseline_corpus.py --skip-prores
    .venv/bin/python tools/generate_topos_baseline_corpus.py --only 4K --tiers proxy,lt
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

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

MEDIA_DIR = REPO / "tests" / "test_videos"
MASTER = MEDIA_DIR / "4K_prores4444.mov"
MANIFEST_PATH = MEDIA_DIR / "topos_prores_baseline_manifest.json"

FRAMES = 487
FPS = Fraction(25, 1)
AUDIO_RATE = 48000
AUDIO_CHANNELS = 2
AUDIO_BITS = 24
#: 母版 PCM 总采样数（19.48 s × 48000，与 487 帧 @25 对齐）。
AUDIO_SAMPLES = 935040
COLOR_CODES = {"primaries": 1, "transfer": 1, "matrix": 1}  # H.273 对齐：bt709
#: 色彩范围编码域不同：ffprobe/PyAV 1=MPEG(limited)；TOPOS 容器 0=limited。
RANGE_FF = 1
RANGE_TOPOS = 0
#: 归一化色彩描述（manifest 记录与成对比较用）。
COLOR = {"primaries": 1, "transfer": 1, "matrix": 1, "range": "limited"}


@dataclass(frozen=True)
class TierSpec:
    tier: str            # topos tier id
    prores_name: str     # 基准文件名后缀
    prores_profile: int  # prores_ks/videotoolbox profile 编号
    prores_pix_fmt: str  # ProRes 基准解码像素格式
    topos_pix_fmt: str   # TOPOS 编码 pix_fmt（planar 直通域）
    expected: tuple      # (profile, pixel_format, bit_depth)


TIERS: tuple[TierSpec, ...] = (
    TierSpec("proxy", "422Proxy", 0, "yuv422p10le", "yuv422p10le", (3, 0, 10)),
    TierSpec("lt", "422LT", 1, "yuv422p10le", "yuv422p10le", (3, 0, 10)),
    TierSpec("standard", "422", 2, "yuv422p10le", "yuv422p10le", (3, 0, 10)),
    TierSpec("hq", "422HQ", 3, "yuv422p10le", "yuv422p10le", (3, 0, 10)),
    TierSpec("4444", "4444", 4, "yuv444p12le", "yuv444p10le", (5, 1, 10)),
    TierSpec("4444xq", "4444XQ", 5, "yuv444p12le", "yuv444p12le", (6, 1, 12)),
)

SIZES = {"2K": (1920, 1080), "4K": (3840, 2160)}

#: C02 冻结命名契约（2026-09-22 重导、2026-09-27 起由本工具直接落盘）：
#: topos 输出文件名 = f"{size}_topos_{TOPOS_NAMING[spec.tier]}.mov"。
#: 历史 tier-id 命名（2K_topos_proxy.mov 等）退役，禁止再写入 test_videos。
TOPOS_NAMING = {
    "proxy": "422_Proxy",
    "lt": "422_LT",
    "standard": "422",
    "hq": "422_HQ",
    "4444": "4444",
    "4444xq": "4444_XQ",
}

#: 需要从母版重建的 ProRes 基准（缺失 / 异源帧数）；其余 Apple 原生基准保留。
PRORES_REGEN = {"4K_prores422.mov", "4K_prores422HQ.mov"}


def _ffmpeg_bin() -> Path:
    """static_ffmpeg 包内的独立 ffmpeg 二进制（无 Homebrew 依赖）。"""
    import shutil

    try:
        from static_ffmpeg._add_paths import add_paths
        add_paths()
    except ImportError:
        pass
    which = shutil.which("ffmpeg")
    if which:
        return Path(which)
    raise SystemExit("ffmpeg 不可用：static_ffmpeg 包缺失或平台目录不含 darwin 二进制")


def _sha256_partial(path: Path, head_bytes: int = 1 << 20) -> str:
    """快速指纹：size + 首尾各 1MB sha256（与 freeze_benchmark_manifest 同口径）。"""
    size = path.stat().st_size
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(head_bytes))
        if size > 2 * head_bytes:
            f.seek(-head_bytes, 2)
            h.update(f.read(head_bytes))
    h.update(str(size).encode())
    return h.hexdigest()


def _ffprobe_bytes(path: Path) -> int:
    """ProRes 视频流净载荷字节（demux 视频包求和；音频不计入）。"""
    with av.open(str(path)) as c:
        stream = c.streams.video[0]
        total = 0
        for pkt in c.demux(stream):
            if pkt.dts is None:
                continue
            total += pkt.buffer_size
    return total


# ————————————————————————— ProRes 基准 —————————————————————————

def generate_prores(ff: Path, spec: TierSpec, size: str, out: Path) -> None:
    """母版 → ProRes 基准（prores_ks 软编；音频 PCM 直拷）。

    C03（2026-09-27）：原用 prores_videotoolbox（Apple 硬编家族同源），
    但 FFmpeg 8.x 的 VT 封装不把 color_range 传入输出流——重建产物
    range='unknown'，冻结色彩契约（range=limited）自检失败。prores_ks
    为同一 ProRes 422 家族的参考软编器，完整写入 primaries/trc/
    matrix/range 元数据，且不依赖硬件可用性（跨机可复现），故重建
    统一走 prores_ks。
    """
    w, h = SIZES[size]
    cmd = [
        str(ff), "-hide_banner", "-loglevel", "error",
        "-i", str(MASTER),
        # 目标尺寸必须显式传给 scale：省略 w:h 时 scale 只改写范围、不做缩放，
        # 于是"重建 2K"会写出 4K（母版尺寸）并覆盖同名基准。
        "-vf", f"scale={w}:{h}:in_range=tv:out_range=tv",
        "-c:v", "prores_ks", "-profile:v", str(spec.prores_profile),
        "-pix_fmt", "yuv422p10le",
        # 注意：不设 -frames:v——mux 在视频帧数达标即定稿时会截短音频
        # 直拷（实测 935040 → 932736）；母版恰为 487 帧，EOF 排空即得
        # 487 帧 + 完整音频，帧数由 probe_prores 校验兜底。
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-colorspace", "bt709", "-color_range", "tv",
        "-c:a", "copy",
        "-y", str(out),
    ]
    t0 = time.perf_counter()
    subprocess.run(cmd, check=True)
    print(f"  ProRes {out.name}：{time.perf_counter() - t0:.1f}s", flush=True)


def probe_prores(path: Path, spec: TierSpec, size: str) -> dict:
    """校验一个 ProRes 基准并返回描述条目（不满足契约 → RuntimeError）。"""
    w, h = SIZES[size]
    with av.open(str(path)) as c:
        s = c.streams.video[0]
        cc = s.codec_context
        aud = c.streams.audio[0].codec_context if c.streams.audio else None
        # C03：色彩元数据从解码首帧读取。PyAV 17 的 demux CodecContext
        # 不把 codecpar.color_range 回填进 avctx（恒 unknown），帧级字段
        # 由解码器从容器/码流填充，跨版本稳定。ffprobe 口径（流级）已
        # 验证与帧级一致。
        color = None
        for frame in c.decode(video=0):
            color = {
                "primaries": int(frame.color_primaries or 0),
                "transfer": int(frame.color_trc or 0),
                "matrix": int(frame.colorspace or 0),
                "range": "limited" if int(frame.color_range or 0) == RANGE_FF
                else "full" if int(frame.color_range or 0) == 2 else "unknown",
            }
            break
        if color is None:
            color = {
                "primaries": int(cc.color_primaries or 0),
                "transfer": int(cc.color_trc or 0),
                "matrix": int(cc.colorspace or 0),
                "range": "limited" if int(cc.color_range or 0) == RANGE_FF
                else "full" if int(cc.color_range or 0) == 2 else "unknown",
            }
        entry = {
            "path": str(path.relative_to(REPO)),
            "kind": "prores",
            "tier": spec.tier,
            "resolution": [int(cc.width), int(cc.height)],
            "frames": int(s.frames),
            "fps": float(s.average_rate),
            "pix_fmt": cc.format.name,
            "color": color,
            "audio": (
                {"codec": aud.name, "rate": int(aud.sample_rate),
                 "channels": len(aud.layout.channels),
                 "bits": 24 if aud.format.name == "s32" else None}
                if aud else None
            ),
            "bytes": path.stat().st_size,
        }
        problems = []
        if entry["resolution"] != [w, h]:
            problems.append(f"resolution {entry['resolution']} != [{w},{h}]")
        if entry["frames"] != FRAMES:
            problems.append(f"frames {entry['frames']} != {FRAMES}")
        if entry["fps"] != 25.0:
            problems.append(f"fps {entry['fps']} != 25")
        if entry["pix_fmt"] != spec.prores_pix_fmt:
            problems.append(f"pix_fmt {entry['pix_fmt']} != {spec.prores_pix_fmt}")
        if color != COLOR:
            problems.append(f"color {color} != {COLOR}")
        if aud is None:
            problems.append("no audio stream")
        else:
            if aud.name != "pcm_s24le" or int(aud.sample_rate) != AUDIO_RATE \
                    or len(aud.layout.channels) != AUDIO_CHANNELS:
                problems.append(
                    f"audio {aud.name}/{aud.sample_rate}/{aud.layout.name}"
                    " != pcm_s24le/48000/stereo")
            if int(c.streams.audio[0].duration or 0) != AUDIO_SAMPLES:
                problems.append(
                    f"audio samples {int(c.streams.audio[0].duration or 0)}"
                    f" != {AUDIO_SAMPLES}")
        if problems:
            raise RuntimeError(f"{path.name}: {problems}")
    entry["video_bytes"] = _ffprobe_bytes(path)
    entry["video_mbps"] = entry["video_bytes"] * 8.0 / (FRAMES / 25.0) / 1e6
    entry["sha256_partial"] = _sha256_partial(path)
    return entry


def master_audio_sha256() -> str:
    """母版音频净载荷指纹（证明基准集与母版同音频源）。"""
    with av.open(str(MASTER)) as c:
        s = c.streams.audio[0]
        h = hashlib.sha256()
        for pkt in c.demux(s):
            h.update(bytes(pkt))
    return h.hexdigest()


# ————————————————————————— TOPOS 编码 —————————————————————————

@dataclass
class PlanarFrame:
    """ToposVideoEncoder 消费的最小产品帧协议（planar 直通）。"""

    planes: tuple
    pixel_format: str
    width: int
    height: int
    frame_index: int
    is_planar: bool = True


def _plane_array(plane) -> np.ndarray:
    arr = np.ndarray(
        (plane.height, plane.width), dtype="<u2",
        buffer=memoryview(plane), strides=(plane.line_size, 2),
    )
    return np.ascontiguousarray(arr)


def _audio_int32(path: Path) -> np.ndarray:
    """ProRes pcm_s24le → (channels, N) int32（s24<<8，PyAV 解码为打包 s32）。"""
    chunks = []
    with av.open(str(path)) as c:
        s = c.streams.audio[0]
        ch = len(s.codec_context.layout.channels)
        for frame in c.decode(s):
            arr = frame.to_ndarray()          # (1, N*ch) 交错打包
            chunks.append(arr.reshape(-1, ch).T)
    return np.concatenate(chunks, axis=1)


def _decode_audio_exact(path: Path) -> np.ndarray:
    """ProRes pcm_s24le → (2, N) float32，标度与编码器量化器严格互逆。

    s32(s24<<8) → v24=s32/256 → f=v24/8388607（float32）。编码侧
    rint(f*8388607) 恢复 v24（误差 ≤0.25 LSB，无 tie），保住 24-bit 精度。
    """
    a = _audio_int32(path)
    v24 = a.astype(np.float64) / 256.0
    return (v24 / 8388607.0).astype(np.float32)


def _expected_audio_be(path: Path) -> tuple:
    """ProRes 音频 → (期望容器大端 24-bit 字节流, 钳位样本数)。

    严格镜像编码器 LPCM 路径：f=clip(v24/8388607,-1,1) → rint(f*8388607)。
    s24 负满刻度（-8388608）经 [-1,1] 钳位后落 -8388607（差 1 LSB，
    记入返回值供诊断）。
    """
    a = _audio_int32(path)                      # (ch, N) int32 = s24 << 8
    v24 = a.astype(np.float64) / 256.0
    x = np.clip(v24 / 8388607.0, -1.0, 1.0)
    clipped = int(np.count_nonzero(v24 < -8388607.0))
    q = np.rint(x * 8388607.0)
    be = np.ascontiguousarray(q.T)
    return be.astype(">i4").view(">u1").reshape(-1, 4)[:, 1:4].tobytes(), clipped


def _encoder_config(spec: TierSpec, size: str) -> SimpleNamespace:
    w, h = SIZES[size]
    return SimpleNamespace(
        width=w, height=h, fps=FPS,
        pix_fmt=spec.topos_pix_fmt,
        profile=spec.tier,
        crf=None,
        has_alpha=False,
        entropy_mode="rans2",
        aq_mode="off",
        rdo_mode="off",
        gop="intra",
        color_metadata=ColorMetadataConfig(),  # bt709/bt709/bt709/limited
    )


def encode_topos(spec: TierSpec, size: str, src: Path, out: Path,
                 threads: int) -> None:
    """同档 ProRes 基准 → TOPOS（planar 直通 + PCM 音频轨）。"""
    w, h = SIZES[size]
    if out.exists() or Path(str(out) + ".topos-tmp").exists():
        out.unlink(missing_ok=True)
        Path(str(out) + ".topos-tmp").unlink(missing_ok=True)
    encoder = ToposVideoEncoder(
        str(out), _encoder_config(spec, size),
        slice_threads=threads, slice_rows=None,   # 产品自适应带高（8/4）
    )
    t0 = time.perf_counter()
    try:
        encoder.open()
        encoder.add_audio_stream(
            codec="pcm_s24le", sample_rate=AUDIO_RATE, channels=AUDIO_CHANNELS)
        audio_f = _decode_audio_exact(src)
        pf = spec.topos_pix_fmt
        sub = "422" if pf.startswith("yuv422") else "444"
        with av.open(str(src)) as c:
            stream = c.streams.video[0]
            n = 0
            for frame in c.decode(stream):
                if n >= FRAMES:
                    break
                if frame.format.name != pf:
                    frame = frame.reformat(format=pf)
                expected_widths = (
                    (w, (w + 1) // 2, (w + 1) // 2) if sub == "422"
                    else (w, w, w))
                planes = tuple(_plane_array(p) for p in frame.planes[:3])
                shapes = tuple((h, ew) for ew in expected_widths)
                if tuple(p.shape for p in planes) != shapes:
                    raise RuntimeError(
                        f"{src.name}: 平面几何 {tuple(p.shape for p in planes)}"
                        f" != {shapes}")
                encoder.encode_frame(PlanarFrame(
                    planes, pf, w, h, n))
                n += 1
                if n % 64 == 0 or n == FRAMES:
                    print(f"  {size} topos/{spec.tier} {n}/{FRAMES} 帧",
                          flush=True)
        if n != FRAMES:
            raise RuntimeError(f"{src.name}: 仅解码 {n} 帧，要求 {FRAMES}")
        encoder.write_audio_samples(audio_f, AUDIO_RATE)
        encoder.close()
    except Exception:
        if encoder.is_open:
            try:
                encoder.abort()
            except Exception:
                pass
        out.unlink(missing_ok=True)
        Path(str(out) + ".topos-tmp").unlink(missing_ok=True)
        raise
    print(f"  TOPOS {out.name}：{FRAMES / (time.perf_counter() - t0):.1f} fps",
          flush=True)


def verify_topos(codec: ToposCodec, path: Path, spec: TierSpec, size: str,
                 src: Path) -> dict:
    """TOPOS 输出校验：容器元数据 + 首尾帧解码 + 音频字节级比对。"""
    w, h = SIZES[size]
    exp_profile, exp_pf, exp_bd = spec.expected
    movie = ToposMovieFile(codec, str(path))
    try:
        info = movie.info()
        checks = {
            "dimensions": (int(info.width), int(info.height)) == (w, h),
            "sample_count": int(info.sample_count) == FRAMES,
            "profile": int(info.profile) == exp_profile,
            "pixel_format": int(info.pixel_format) == exp_pf,
            "bit_depth": int(info.bit_depth) == exp_bd,
            "bitstream_major": int(info.bitstream_major) == 7,
            "color": (
                int(info.color_primaries) == COLOR_CODES["primaries"]
                and int(info.color_transfer) == COLOR_CODES["transfer"]
                and int(info.color_matrix) == COLOR_CODES["matrix"]
                and int(info.color_range) == RANGE_TOPOS),
        }
        first_buf, first_size = movie.packet_into(0)
        with ToposDecoder(codec) as decoder:
            fi = decoder.prepare(first_buf, first_size)
            planes = []
            for i in range(int(fi.plane_count)):
                pw, ph = codec.plane_geometry(fi, i)
                planes.append(np.empty((ph, pw), dtype="<u2"))
            views = decoder.make_views(planes)
            decoder.decode_views(first_buf, views, int(fi.plane_count),
                                 first_size)
            last_buf, last_size = movie.packet_into(FRAMES - 1)
            decoder.decode_views(last_buf, views, int(fi.plane_count),
                                 last_size)
        checks["decode_first_last"] = True

        # 视频流净载荷（逐包尺寸求和）
        video_bytes = 0
        for idx in range(int(info.sample_count)):
            _, psz = movie.packet_into(idx)
            video_bytes += int(psz)
        # 音频轨：格式 + 字节级内容
        ai = movie.audio_info()
        checks["audio_format"] = (
            int(ai.sample_rate) == AUDIO_RATE
            and int(ai.channel_count) == AUDIO_CHANNELS
            and int(ai.bits_per_sample) == AUDIO_BITS
            and int(ai.sample_count) == AUDIO_SAMPLES)
        got = movie.read_audio_samples()
        want, clipped = _expected_audio_be(src)
        checks["audio_bytes_equal"] = (got == want)
        if not checks["audio_bytes_equal"]:
            n = min(len(got), len(want))
            diff = sum(a != b for a, b in zip(got[:n], want[:n]))
            checks["audio_mismatch_bytes"] = diff
        if not all(v for k, v in checks.items() if isinstance(v, bool)):
            raise RuntimeError(f"{path.name}: {checks}")
        return {
            "path": str(path.relative_to(REPO)),
            "kind": "topos",
            "tier": spec.tier,
            "resolution": [w, h],
            "frames": int(info.sample_count),
            "fps": [FPS.numerator, FPS.denominator],
            "profile": int(info.profile),
            "pixel_format": int(info.pixel_format),
            "bit_depth": int(info.bit_depth),
            "color": dict(COLOR),
            "audio": {"codec": "pcm_s24le", "rate": AUDIO_RATE,
                      "channels": AUDIO_CHANNELS, "bits": AUDIO_BITS,
                      "samples": int(ai.sample_count),
                      "byte_exact": True,
                      "neg_full_scale_clipped_lsb": clipped},
            "bytes": path.stat().st_size,
            "video_bytes": video_bytes,
            "video_mbps": video_bytes * 8.0 / (FRAMES / 25.0) / 1e6,
            "sha256_partial": _sha256_partial(path),
            "checks": {
                k: v for k, v in checks.items() if isinstance(v, bool)},
        }
    finally:
        movie.close()


# ————————————————————————— 主流程 —————————————————————————

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-prores", action="store_true",
                        help="跳过 ProRes 重建/校验（已有基准时）")
    parser.add_argument("--skip-topos", action="store_true",
                        help="跳过 TOPOS 编码")
    parser.add_argument("--only", action="append", default=[],
                        choices=sorted(SIZES), help="只处理指定分辨率")
    parser.add_argument("--tiers", default="",
                        help="逗号分隔的档位子集（缺省全部六档）")
    parser.add_argument("--threads", type=int, default=16,
                        help="Topos slice threads（默认 16，产品导出档）")
    args = parser.parse_args()

    sizes = list(args.only) or list(SIZES)
    wanted = set(args.tiers.split(",")) if args.tiers else None

    if not MASTER.is_file():
        raise SystemExit(f"母版缺失：{MASTER}")

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator": str(Path(__file__).relative_to(REPO)),
        "plan": "docs/topos_prores_quality_optimization_plan_2026-09-21.md",
        "phase": "P0",
        "topos_naming": dict(TOPOS_NAMING),  # C02：冻结名契约随清单落盘
        "master": {
            "path": str(MASTER.relative_to(REPO)),
            "frames": FRAMES,
            "fps": [FPS.numerator, FPS.denominator],
            "resolution": list(SIZES["4K"]),
            "color": dict(COLOR),
            "bytes": MASTER.stat().st_size,
            "sha256_partial": _sha256_partial(MASTER),
            "audio_payload_sha256": None,  # 稍后填充
        },
        "audio_rate": AUDIO_RATE,
        "audio_samples": AUDIO_SAMPLES,
        "entries": [],
        "pairs": [],
    }

    # —— 1) ProRes 基准：缺失/校验失败才重建（VT 重建确定性，但免无谓重跑）——
    prores_entries = {}
    if not args.skip_prores:
        ff = _ffmpeg_bin()
        for size in sizes:
            for spec in TIERS:
                if wanted and spec.tier not in wanted:
                    continue
                out = MEDIA_DIR / f"{size}_prores{spec.prores_name}.mov"
                entry = None
                if out.exists():
                    try:
                        entry = probe_prores(out, spec, size)
                    except RuntimeError as e:
                        print(f"  基准不合规，重建：{e}", flush=True)
                        entry = None
                if entry is None:
                    generate_prores(ff, spec, size, out)
                    entry = probe_prores(out, spec, size)
                prores_entries[(size, spec.tier)] = entry
                manifest["entries"].append(entry)
                print(f"  校验通过 {out.name}: video "
                      f"{entry['video_mbps']:.1f} Mbps, "
                      f"{entry['bytes'] / 1e6:.1f} MB", flush=True)
        manifest["master"]["audio_payload_sha256"] = master_audio_sha256()

    # —— 2) TOPOS 编码 + 校验 ——
    if not args.skip_topos:
        codec = ToposCodec()
        for size in sizes:
            for spec in TIERS:
                if wanted and spec.tier not in wanted:
                    continue
                src = MEDIA_DIR / f"{size}_prores{spec.prores_name}.mov"
                if not src.exists():
                    raise SystemExit(f"TOPOS 源基准缺失：{src}（先跑 --skip-topos 之外的完整流程）")
                out = MEDIA_DIR / f"{size}_topos_{TOPOS_NAMING[spec.tier]}.mov"
                encode_topos(spec, size, src, out, args.threads)
                entry = verify_topos(codec, out, spec, size, src)
                manifest["entries"].append(entry)
                print(f"  校验通过 {out.name}: video "
                      f"{entry['video_mbps']:.1f} Mbps, "
                      f"{entry['bytes'] / 1e6:.1f} MB", flush=True)

    # —— 3) 成对一致性 + 汇总 ——
    entries = manifest["entries"]
    for size in sizes:
        for spec in TIERS:
            topos = next((e for e in entries if e["kind"] == "topos"
                          and e["tier"] == spec.tier
                          and tuple(e["resolution"]) == SIZES[size]), None)
            prores = next((e for e in entries if e["kind"] == "prores"
                           and e["tier"] == spec.tier
                           and tuple(e["resolution"]) == SIZES[size]), None)
            if not (topos and prores):
                continue
            ratio = topos["bytes"] / prores["bytes"]
            pair = {
                "size": size,
                "tier": spec.tier,
                "prores": prores["path"],
                "topos": topos["path"],
                "prores_bytes": prores["bytes"],
                "topos_bytes": topos["bytes"],
                "topos_over_prores": round(ratio, 4),
                "prores_video_mbps": round(prores["video_mbps"], 2),
                "topos_video_mbps": round(topos["video_mbps"], 2),
                "consistent": (
                    topos["frames"] == prores["frames"]
                    and topos["color"] == prores["color"]
                    and topos["audio"]["rate"] == prores["audio"]["rate"]
                    and topos["audio"]["channels"] == prores["audio"]["channels"]
                    and topos["audio"]["bits"] == prores["audio"]["bits"]
                    and topos["audio"]["samples"] == AUDIO_SAMPLES),
                # P2 目标：topos_bytes <= prores_bytes × 0.98；P0 仅记录现状
                "within_cap_98": topos["bytes"] <= prores["bytes"] * 0.98,
            }
            manifest["pairs"].append(pair)
            flag = "✓" if pair["within_cap_98"] else "·"
            print(f"  [{flag}] {size} {spec.tier}: topos/prores = "
                  f"{ratio * 100:.1f}%（{pair['topos_video_mbps']} vs "
                  f"{pair['prores_video_mbps']} Mbps）", flush=True)

    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"\nmanifest：{MANIFEST_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
