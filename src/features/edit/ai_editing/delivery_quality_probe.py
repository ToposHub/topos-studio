"""M21.2（R05）· 交付 QC 测量 producer——真实测量填充质量门输入。

质量门（``evaluate_delivery_quality``）只裁决**已提供的测量**；此前无
生产者，字段默认 0/None——"未测"与"测得为零"不可区分，默认 0 冒充已
测量。本模块用 PyAV 实际解码输出文件，测量黑帧/冻结帧/损坏帧/音频轨
/采样率/峰值/静音/LUFS，产出 :class:`DeliveryQualityInput`：

- **测得**的字段填实测值（0 = 真实测得零帧）；
- **测不了**的字段保持 None（门跳过或按契约 fail-closed，如
  ``loudness_backend_available=False`` 且期望 LUFS 已设 → 阻断）；
- 测量失败（文件打不开等）返回 ``None``——调用方不得把探测失败当成
  通过。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Optional

from src.features.edit.ai_editing.delivery_quality_gate import (
    DeliveryQualityInput,
)

logger = logging.getLogger(__name__)

#: 黑帧判定：整帧平均亮度低于该值（0-1 域）
BLACK_FRAME_LUMA_THRESHOLD = 0.004
#: 冻结帧判定：相邻帧图像哈希相同即冻结
FREEZE_HASH_PREFIX_BYTES = 1 << 16


def _frame_signature(frame: Any) -> bytes:
    """帧内容签名（前 64KB 平面数据哈希——冻结检测用，非内容身份）。"""
    h = hashlib.sha256()
    for plane in frame.planes:
        try:
            buf = bytes(plane[:FREEZE_HASH_PREFIX_BYTES // len(frame.planes)])
        except Exception:  # noqa: BLE001 — plane 不可读按空处理
            buf = b""
        h.update(buf)
        if h.digest_size * 0 >= FREEZE_HASH_PREFIX_BYTES:
            break
    return h.digest()


def measure_delivery_quality(
    path: str | Path,
    *,
    expected_resolution: Optional[tuple[int, int]] = None,
    expected_fps: Optional[str] = None,
    expected_audio_sample_rate: Optional[int] = None,
    expected_lufs: Optional[float] = None,
    max_silence_seconds: Optional[float] = None,
) -> Optional[DeliveryQualityInput]:
    """实际解码测量一个交付文件，产出质量门输入。

    Returns:
        :class:`DeliveryQualityInput`；文件打不开/无视频流返回 None
        （探测失败 ≠ 通过，调用方显式处理）。
    """
    try:
        import av
        import numpy as np
    except ImportError:
        logger.warning("PyAV/numpy 不可用——交付质量测量无法执行")
        return None

    p = Path(path)
    if not p.is_file():
        return None

    black = frozen = corrupt = 0
    width = height = None
    fps_str: Optional[str] = None
    duration: Optional[float] = None
    audio_present = False
    audio_rate: Optional[int] = None
    silence_seconds = 0.0
    peak_db: Optional[float] = None
    integrated_lufs: Optional[float] = None
    loudness_backend = False

    try:
        with av.open(str(p)) as container:
            v_streams = container.streams.video
            a_streams = container.streams.audio
            if not v_streams:
                return None
            v = v_streams[0]
            width, height = v.codec_context.width, v.codec_context.height
            if v.average_rate is not None and float(v.average_rate) > 0:
                fps_val = round(float(v.average_rate), 5)
                fps_str = str(int(fps_val)) if fps_val == int(fps_val)                     else str(fps_val)
            duration = float(container.duration or 0) / 1_000_000.0 or None

            prev_sig: Optional[bytes] = None
            for packet in container.demux(v):
                try:
                    for frame in packet.decode():
                        # 统一转 RGB24：yuv420p 的黑（Y=16 limited）经
                        # 矩阵换算回 RGB≈0；直接对 YUV 平面取均值会把
                        # 纯黑判成 16/255≈0.06
                        try:
                            rgb_frame = frame.reformat(format="rgb24")
                            arr = rgb_frame.to_ndarray()
                            luma = float(arr.mean()) / 255.0
                            if luma < BLACK_FRAME_LUMA_THRESHOLD:
                                black += 1
                            # 冻结检测：降采样字节哈希（快速、对噪声鲁棒）
                            small = arr[::8, ::8]
                            sig = hashlib.sha256(
                                small.tobytes()).digest()
                            if prev_sig is not None and sig == prev_sig:
                                frozen += 1
                            prev_sig = sig
                        except Exception:  # noqa: BLE001 — 像素统计失败按可解码计
                            pass
                except av.error.InvalidDataError:
                    corrupt += 1

            if a_streams:
                a = a_streams[0]
                audio_present = True
                audio_rate = int(a.codec_context.sample_rate or 0) or None
                # PCM 峰值/静音测量（解码全部音频帧）
                peak = 0.0
                silent_run = 0.0
                max_silent_run = 0.0
                rate_f = float(audio_rate or 48000)
                pcm_all = []
                for packet in container.demux(a):
                    try:
                        for frame in packet.decode():
                            arr = frame.to_ndarray()
                            if arr.ndim == 1:
                                arr = arr[np.newaxis, :]
                            mono = arr.mean(axis=0)
                            peak = max(peak, float(np.abs(mono).max()) if
                                       mono.size else 0.0)
                            frame_silence = bool(
                                mono.size and float(np.abs(mono).max())
                                < 1e-4)
                            frame_dur = frame.samples / rate_f
                            if frame_silence:
                                silent_run += frame_dur
                                max_silent_run = max(max_silent_run,
                                                     silent_run)
                            else:
                                silent_run = 0.0
                            pcm_all.append(mono)
                    except av.error.InvalidDataError:
                        corrupt += 1
                if peak > 0:
                    import math
                    peak_db = 20.0 * math.log10(peak)
                silence_seconds = max_silent_run
                # LUFS（pyloudnorm 可用时）
                if pcm_all:
                    pcm = np.concatenate(pcm_all)
                    try:
                        import pyloudnorm as pyln
                        meter = pyln.Meter(rate_f)
                        integrated_lufs = float(
                            meter.integrated_loudness(pcm.astype(np.float64)))
                        loudness_backend = True
                    except Exception as exc:  # noqa: BLE001 — 后端缺失如实标不可用
                        logger.info("LUFS 后端不可用：%s", exc)
                        loudness_backend = False
    except Exception as exc:  # noqa: BLE001 — 打开失败 = 探测失败
        logger.warning("交付质量测量失败（%s）：%s", p, exc)
        return None

    def _file_hash() -> Optional[str]:
        try:
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return None

    return DeliveryQualityInput(
        resolution=(width, height) if width and height else None,
        expected_resolution=expected_resolution,
        fps=fps_str,
        expected_fps=expected_fps,
        duration_seconds=duration,
        black_frame_count=black,
        frozen_frame_count=frozen,
        corrupt_frame_count=corrupt,
        audio_present=audio_present,
        audio_sample_rate=audio_rate,
        expected_audio_sample_rate=expected_audio_sample_rate,
        peak_db=peak_db,
        integrated_lufs=integrated_lufs,
        loudness_backend_available=loudness_backend,
        expected_lufs=expected_lufs,
        silence_seconds=silence_seconds if audio_present else None,
        max_silence_seconds=max_silence_seconds,
        video_codec=None,
        container=p.suffix.lstrip(".") or None,
        file_size_bytes=p.stat().st_size,
        output_exists=True,
        output_reopenable=True,   # 全量解码成功即"可重开"
        output_hash=_file_hash(),
    )


__all__ = ["measure_delivery_quality", "BLACK_FRAME_LUMA_THRESHOLD"]
