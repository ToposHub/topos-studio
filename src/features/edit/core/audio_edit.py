"""Non-destructive per-clip audio edit stack (P0-3 of the Clip Editor roadmap).

``ClipInstance.audio_edit`` holds a versioned, serializable structure::

    {
        "version": 1,
        "processing_rate": 48000,
        "steps": [ {step...}, ... ],
        "markers": [...],       # 预留（P1-5 标记点）
        "envelope": [...],      # 预留（P1-4 包络）
    }

Each step is a pure-data dict with a stable ``step_id``, a ``processor_id``
from the registry below, an ``algorithm_version`` for the DSP it was authored
against, user ``params`` (half-open sample range + target channels + gains),
an ``enabled`` flag, and any unknown fields preserved verbatim for forward
compatibility.

Coordinate contract (roadmap §3):

- Step ranges are half-open ``[start, end)`` integers in **clip-local output
  samples at ``processing_rate``** — the same domain the render path sees when
  it processes a timeline block (mix_samples / export / stems all position
  blocks by clip-local samples).
- V1 processors are all length-preserving; a step that would change length
  must additionally persist an input/output mapping and is rejected by this
  registry with an explicit diagnostic until those land (P2).

Render order guarantee: source decode → trim/binding/retime → **this stack**
→ native clip gain / envelope / fades → Clip FX → Track FX → Master.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

from src.features.edit.core.sample_time import AUDIO_REFERENCE_SAMPLE_RATE

AUDIO_EDIT_SCHEMA_VERSION = 1
PROCESSING_RATE = AUDIO_REFERENCE_SAMPLE_RATE

# 选区处理边缘的干湿过渡（2–5 ms 区间取 3 ms；防爆音）。
EDGE_FADE_SECONDS = 0.003

# V1 处理器注册表：全部保长。algorithm_version 语义见各实现。
PROCESSORS: dict[str, dict[str, Any]] = {
    "range_gain": {
        "algorithm_version": 1,
        "length_preserving": True,
        "description": "Apply a linear gain (dB) to a half-open sample range on target channels.",
    },
    "mute": {
        "algorithm_version": 1,
        "length_preserving": True,
        "description": "Zero gain over a range with short edge fades to the original.",
    },
    "normalize": {
        "algorithm_version": 1,
        "length_preserving": True,
        "description": "range_gain with the gain pre-computed from the measured sample peak.",
    },
}
KNOWN_PROCESSOR_IDS = frozenset(PROCESSORS)


def new_audio_edit(steps: Optional[list[dict]] = None, **extra: Any) -> dict:
    """构造一个最小合法的 audio_edit 结构（未知顶层字段保留）。"""
    edit: dict[str, Any] = {
        "version": AUDIO_EDIT_SCHEMA_VERSION,
        "processing_rate": PROCESSING_RATE,
        "steps": list(steps or []),
    }
    edit.update(extra)
    return edit


def make_step(
    processor_id: str,
    start: int,
    end: int,
    channels: tuple[int, ...] | list[int],
    *,
    gain_db: float = 0.0,
    enabled: bool = True,
    label: str = "",
    step_id: str = "",
    **params: Any,
) -> dict:
    """构造一个 step（半开区间 + 目标声道；gain 语义随处理器）。"""
    import uuid

    step: dict[str, Any] = {
        "step_id": step_id or str(uuid.uuid4()),
        "processor_id": processor_id,
        "algorithm_version": PROCESSORS[processor_id]["algorithm_version"],
        "enabled": bool(enabled),
        "params": {
            "start": int(start),
            "end": int(end),
            "channels": [int(c) for c in channels],
            **({"gain_db": float(gain_db)} if processor_id != "mute" else {}),
            **({"label": label} if label else {}),
            **params,
        },
    }
    return step


def validate_audio_edit(
    audio_edit: Any,
    *,
    clip_output_samples: int | None = None,
    channel_count: int | None = None,
) -> list[str]:
    """返回逐条诊断（空列表 = 可安全应用/交付）。

    未知处理器/未知版本/坐标越界都会阻断，而不是静默跳过——这是
    「重开后可重建、未知 processor 阻断应用和交付」的合同（§3）。
    """
    if audio_edit is None:
        return []
    if not isinstance(audio_edit, dict):
        return ["audio_edit must be a dict"]
    diagnostics: list[str] = []
    version = audio_edit.get("version")
    if not isinstance(version, int) or version < 1:
        diagnostics.append(f"audio_edit.version must be a positive int, got {version!r}")
    elif version > AUDIO_EDIT_SCHEMA_VERSION:
        diagnostics.append(
            f"audio_edit.version {version} is newer than supported "
            f"{AUDIO_EDIT_SCHEMA_VERSION}")
    rate = audio_edit.get("processing_rate", PROCESSING_RATE)
    if rate != PROCESSING_RATE:
        diagnostics.append(
            f"processing_rate {rate!r} != reference {PROCESSING_RATE}")
    steps = audio_edit.get("steps", [])
    if not isinstance(steps, list):
        diagnostics.append("audio_edit.steps must be a list")
        return diagnostics
    seen_ids: set[str] = set()
    for index, step in enumerate(steps):
        where = f"step[{index}]"
        if not isinstance(step, dict):
            diagnostics.append(f"{where}: must be a dict")
            continue
        step_id = step.get("step_id")
        if not isinstance(step_id, str) or not step_id:
            diagnostics.append(f"{where}: missing step_id")
        elif step_id in seen_ids:
            diagnostics.append(f"{where}: duplicate step_id {step_id!r}")
        else:
            seen_ids.add(step_id)
        processor_id = step.get("processor_id")
        if processor_id not in KNOWN_PROCESSOR_IDS:
            diagnostics.append(
                f"{where}: unknown processor {processor_id!r} — "
                "application and delivery are blocked")
            continue
        params = step.get("params", {})
        if not isinstance(params, dict):
            diagnostics.append(f"{where}: params must be a dict")
            continue
        start, end = params.get("start"), params.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            diagnostics.append(f"{where}: start/end must be ints")
            continue
        if not 0 <= start < end:
            diagnostics.append(f"{where}: range [{start}, {end}) is not half-open valid")
        if clip_output_samples is not None and end > clip_output_samples:
            diagnostics.append(
                f"{where}: range end {end} exceeds clip output samples {clip_output_samples}")
        channels = params.get("channels")
        if (not isinstance(channels, list) or not channels
                or not all(isinstance(c, int) and c >= 0 for c in channels)):
            diagnostics.append(f"{where}: channels must be a non-empty list of ints")
        elif channel_count is not None and any(c >= channel_count for c in channels):
            diagnostics.append(
                f"{where}: channel index out of range for {channel_count}-channel clip")
        gain_db = params.get("gain_db")
        if processor_id != "mute" and not isinstance(gain_db, (int, float)):
            diagnostics.append(f"{where}: gain_db must be numeric for {processor_id}")
    return diagnostics


def steps_signature(audio_edit: Any) -> tuple:
    """供音频图快照/重建签名使用的稳定元组（enabled 步骤的有序指纹）。"""
    if not isinstance(audio_edit, dict):
        return ()
    signature: list[tuple] = []
    for step in audio_edit.get("steps", []) or []:
        if not isinstance(step, dict) or not step.get("enabled", True):
            continue
        params = step.get("params", {}) if isinstance(step.get("params"), dict) else {}
        signature.append((
            step.get("step_id"), step.get("processor_id"),
            step.get("algorithm_version"),
            params.get("start"), params.get("end"),
            tuple(params.get("channels", ())),
            params.get("gain_db"),
        ))
    return tuple(signature)


def edge_fade_samples(sample_rate: int) -> int:
    return max(1, int(round(EDGE_FADE_SECONDS * sample_rate)))


def _range_gain_envelope(
    block_length: int, block_start: int, start: int, end: int, fade: int,
) -> np.ndarray | None:
    """构造块内 gain 包络（选区内 1.0，边缘 3 ms 线性过渡；None=不涉及）。"""
    if block_length <= 0:
        return None
    block_end = block_start + block_length
    if block_end <= start or block_start >= end:
        return None
    env = np.zeros(block_length, dtype=np.float32)
    lo = max(start, block_start)
    hi = min(end, block_end)
    # 完整包含时无需淡入（块整体在选区内 → 全 1）。
    env[lo - block_start:hi - block_start] = 1.0
    # 边缘过渡：按绝对样本位置计算斜坡（任意分块与整段逐样本一致——
    # 不能用块内 linspace，其端点随块长变化）。
    if fade > 0:
        for edge, rising in ((start, True), (end, False)):
            ramp_start = edge if rising else edge - fade
            ramp_end = ramp_start + fade
            overlap_lo = max(ramp_start, block_start)
            overlap_hi = min(ramp_end, block_end)
            if overlap_hi <= overlap_lo:
                continue
            positions = np.arange(overlap_lo, overlap_hi, dtype=np.float64)
            progress = (positions - ramp_start) / fade
            values = (progress if rising else (1.0 - progress)).astype(np.float32)
            env[overlap_lo - block_start:overlap_hi - block_start] = np.minimum(
                env[overlap_lo - block_start:overlap_hi - block_start], values)
    return env


@dataclass(frozen=True)
class AudioEditBlockContext:
    """渲染块上下文：块在片段输出域的起始样本与块长（processing_rate）。"""

    block_start_sample: int
    sample_rate: int = PROCESSING_RATE


def apply_clip_audio_edit(
    samples: np.ndarray,
    block_start_sample: int,
    audio_edit: Any,
    *,
    sample_rate: int = PROCESSING_RATE,
) -> np.ndarray:
    """对片段输出域的一个块应用处理栈（纯函数；原地不修改输入）。

    ``samples``: planar float [channels, n]，块首对应片段输出样本
    ``block_start_sample``（processing_rate 域）。调用点位于 clip gain 之后、
    Clip FX 之前（播放/成片/Stem 三处共用本函数保证一致）。
    未知处理器存在时直接原样返回（调用方应先经 validate 阻断）。
    """
    if audio_edit is None or not isinstance(audio_edit, dict):
        return samples
    steps = audio_edit.get("steps") or []
    if not steps:
        return samples
    fade = edge_fade_samples(sample_rate)
    out: np.ndarray | None = None
    for step in steps:
        if not isinstance(step, dict) or not step.get("enabled", True):
            continue
        processor_id = step.get("processor_id")
        if processor_id not in KNOWN_PROCESSOR_IDS:
            return samples  # 未知处理器：保持原样（上游 validate 负责阻断）
        params = step.get("params", {})
        start, end = params.get("start", 0), params.get("end", 0)
        channels = params.get("channels") or []
        gain_linear = 0.0 if processor_id == "mute" else 10.0 ** (
            float(params.get("gain_db", 0.0)) / 20.0)
        if processor_id == "normalize":
            # normalize 的增益在提交时已按实测 peak 预计算并持久化。
            gain_linear = 10.0 ** (float(params.get("gain_db", 0.0)) / 20.0)
        envelope = _range_gain_envelope(
            samples.shape[1], block_start_sample, int(start), int(end), fade)
        if envelope is None:
            continue
        if out is None:
            out = samples.copy()
        for channel in channels:
            if isinstance(channel, int) and 0 <= channel < out.shape[0]:
                out[channel] = out[channel] * (envelope * gain_linear +
                                               (1.0 - envelope))
    return out if out is not None else samples


def apply_audio_edit_to_pcm(pcm: np.ndarray, audio_edit: Any,
                            *, sample_rate: int = PROCESSING_RATE) -> np.ndarray:
    """离线整段应用（Clip Editor 预览/试听共用）。"""
    return apply_clip_audio_edit(pcm, 0, audio_edit, sample_rate=sample_rate)


def copy_audio_edit(audio_edit: Any) -> Any:
    """深拷贝（命令构造前避免共享可变 dict）。"""
    return copy.deepcopy(audio_edit) if isinstance(audio_edit, dict) else audio_edit


def replace_step_enabled(audio_edit: Any, step_id: str, enabled: bool) -> Any:
    """返回旁路状态更新后的新 audio_edit（不修改原 dict）。"""
    if not isinstance(audio_edit, dict):
        return audio_edit
    updated = copy.deepcopy(audio_edit)
    for step in updated.get("steps", []):
        if isinstance(step, dict) and step.get("step_id") == step_id:
            step["enabled"] = bool(enabled)
    return updated


def remove_step(audio_edit: Any, step_id: str) -> Any:
    """返回移除某 step 后的新 audio_edit；steps 清空时返回 None。"""
    if not isinstance(audio_edit, dict):
        return audio_edit
    updated = copy.deepcopy(audio_edit)
    updated["steps"] = [step for step in updated.get("steps", [])
                        if not (isinstance(step, dict) and step.get("step_id") == step_id)]
    return updated if updated["steps"] else None


def append_step(audio_edit: Any, step: dict) -> dict:
    """追加一个 step（audio_edit 为空时建立最小结构）。"""
    base = copy_audio_edit(audio_edit) if isinstance(audio_edit, dict) else new_audio_edit()
    base.setdefault("steps", []).append(step)
    return base


def db_to_linear(gain_db: float) -> float:
    return 10.0 ** (gain_db / 20.0)


def linear_to_db(linear: float) -> float:
    return 20.0 * math.log10(max(1e-12, linear))
