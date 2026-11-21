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
- Length-changing processors (``time_stretch`` / ``delete_range``) additionally
  persist an explicit input/output mapping (``output_length`` etc.); their
  downstream coordinates belong to the previous step's output domain, which
  ``output_length_after_steps`` walks step by step.  A pre-render step may not
  follow a length-changing step (diagnosed) — inline steps are always
  expressed in the FINAL output domain.
- ``pre_render`` processors apply as a group over the whole clip before the
  inline steps — their STFT/stretch semantics cannot survive block splitting,
  so they are baked into a cached base PCM (``pcm_render_cache``) that the
  three render paths slice per block.

Render order guarantee: source decode → trim/binding/retime → **this stack**
→ native clip gain / envelope / fades → Clip FX → Track FX → Master.
"""
from __future__ import annotations

import copy
import math
from collections.abc import Callable
from fractions import Fraction
from typing import Any, Optional

import numpy as np

from src.features.edit.core.sample_time import (
    AUDIO_REFERENCE_SAMPLE_RATE,
    reference_position_to_rate,
)

AUDIO_EDIT_SCHEMA_VERSION = 1
PROCESSING_RATE = AUDIO_REFERENCE_SAMPLE_RATE

# 选区处理边缘的干湿过渡（2–5 ms 区间取 3 ms；防爆音）。
EDGE_FADE_SECONDS = 0.003

# P0-1 · 稳定操作身份：同一操作（处理器+operation+半开区间+声道）重复
# 提交按 upsert_step 替换而非叠加。三类操作互不替换（并存）：
# 手动选区增益 / 峰值标准化 / LUFS 响度匹配各自维护自己的系数。
OPERATION_SELECTION_GAIN = "selection_gain"
OPERATION_NORMALIZE_PEAK = "normalize_peak"
OPERATION_LOUDNESS_MATCH = "loudness_match"

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
    "channel_polarity": {
        "algorithm_version": 1,
        "length_preserving": True,
        "description": "Multiply selected channels by −1 (polarity invert) over a range.",
    },
    "spectral_denoise": {
        "algorithm_version": 1,
        "length_preserving": True,
        # §3.3：STFT 类处理器不能按块保持整段语义，走预渲染 PCM 缓存。
        "pre_render": True,
        "description": (
            "Soft spectral-subtraction denoise over a range using a captured "
            "noise print (pre-rendered)."
        ),
    },
    "time_stretch": {
        "algorithm_version": 1,
        "length_preserving": False,
        "pre_render": True,
        "description": (
            "Speed (and optional pitch) change over a range; records the "
            "input/output mapping (pre-rendered)."
        ),
    },
    "delete_range": {
        "algorithm_version": 1,
        "length_preserving": False,
        "pre_render": True,
        "description": (
            "Remove a half-open sample span and splice the sides with a short "
            "crossfade (pre-rendered)."
        ),
    },
    "spectral_repair": {
        "algorithm_version": 1,
        "length_preserving": True,
        "pre_render": True,
        "description": (
            "Brush/selection spectral repair: attenuate the masked time-freq "
            "region or heal it from neighbouring frames (pre-rendered)."
        ),
    },
    # FX-20（2026-11-21 计划）：固定音程双声（长度保持，预渲染）。
    "doubler": {
        "algorithm_version": 1,
        "length_preserving": True,
        "pre_render": True,
        "description": (
            "Fixed-interval doubled voices (pitch/detune/delay/pan/gain "
            "per voice, length preserving, pre-rendered)."
        ),
    },
    # FX-08（2026-11-21 计划）：自动音高校正（长度保持，预渲染）。
    "pitch_correction": {
        "algorithm_version": 1,
        "length_preserving": True,
        "pre_render": True,
        "description": (
            "Monophonic automatic pitch correction: F0 tracking, scale "
            "quantization, stable-note time-preserving rendering."
        ),
    },
}
KNOWN_PROCESSOR_IDS = frozenset(PROCESSORS)
# 预渲染处理器：作为一组在基底 PCM 中烘焙（先于 inline 步骤），缓存按
# 其有序签名索引（pcm_render_cache）。
PRE_RENDER_PROCESSORS = frozenset(
    pid for pid, meta in PROCESSORS.items() if meta.get("pre_render"))
# 改长度处理器（§3/§6）：必须记录输入/输出映射；下游坐标域属于上一步输出。
LENGTH_CHANGING_PROCESSORS = frozenset(
    pid for pid, meta in PROCESSORS.items()
    if meta.get("pre_render") and not meta.get("length_preserving", True))


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
    # §3/§6：逐 step 推进坐标域长度——改长度步骤之后的步骤属于上一步输出域；
    # inline 步骤（非预渲染）一律在最终输出域校验（最后一轮通过）。
    domain_length = clip_output_samples
    final_length: int | None = clip_output_samples
    seen_length_step = False
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
        is_length_step = processor_id in LENGTH_CHANGING_PROCESSORS
        if is_length_step:
            if not step.get("enabled", True):
                continue  # 旁路的改长度步骤不推进域（等价不存在）
            if seen_length_step and processor_id == "time_stretch":
                diagnostics.append(
                    f"{where}: only one enabled time_stretch step is supported")
            seen_length_step = True
        else:
            # 预渲染步骤不得跟在改长度步骤之后（其坐标域属于上一步输出，
            # 与“inline 在最终域”的规则交织无法稳定表达；v1 明确拒绝）。
            if processor_id in PRE_RENDER_PROCESSORS and seen_length_step:
                diagnostics.append(
                    f"{where}: pre-render step {processor_id!r} cannot follow "
                    "a length-changing step — clear or reorder the stack")
        start, end = params.get("start"), params.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            diagnostics.append(f"{where}: start/end must be ints")
            continue
        if not 0 <= start < end:
            diagnostics.append(f"{where}: range [{start}, {end}) is not half-open valid")
        # inline 步骤在最终域校验（延迟到 final_length 已知后再补查）。
        bound = domain_length if is_length_step or seen_length_step else None
        if bound is not None and end > bound:
            diagnostics.append(
                f"{where}: range end {end} exceeds current output samples {bound}")
        channels = params.get("channels")
        if (not isinstance(channels, list) or not channels
                or not all(isinstance(c, int) and c >= 0 for c in channels)):
            diagnostics.append(f"{where}: channels must be a non-empty list of ints")
        elif channel_count is not None and any(c >= channel_count for c in channels):
            diagnostics.append(
                f"{where}: channel index out of range for {channel_count}-channel clip")
        gain_db = params.get("gain_db")
        if (processor_id not in ("mute", "channel_polarity", "spectral_denoise",
                                 "time_stretch", "delete_range",
                                 "spectral_repair")
                and not isinstance(gain_db, (int, float))):
            diagnostics.append(f"{where}: gain_db must be numeric for {processor_id}")
        if processor_id in PRE_RENDER_PROCESSORS:
            # 预渲染步骤（含改长度步骤）按“当前坐标域长度”校验区间与映射。
            diagnostics.extend(_validate_pre_render_step(
                where, processor_id, params,
                rate=audio_edit.get("processing_rate", PROCESSING_RATE),
                clip_output_samples=domain_length))
        if is_length_step:
            domain_length = _advance_domain_length(processor_id, params, domain_length)
            if domain_length is not None:
                if domain_length <= 0:
                    diagnostics.append(
                        f"{where}: step would empty the clip (output {domain_length})")
                    domain_length = 1
                final_length = domain_length
    if final_length is not None:
        for index, step in enumerate(steps):
            if not isinstance(step, dict) or step.get("processor_id", "") in (
                    LENGTH_CHANGING_PROCESSORS) or not step.get("enabled", True):
                continue
            end = (step.get("params", {}) or {}).get("end")
            if isinstance(end, int) and end > final_length:
                diagnostics.append(
                    f"step[{index}]: inline range end {end} exceeds final "
                    f"output samples {final_length}")
    envelope = audio_edit.get("envelope", [])
    if envelope is not None:
        if not isinstance(envelope, list):
            diagnostics.append("audio_edit.envelope must be a list")
        else:
            seen_samples: set[int] = set()
            for index, node in enumerate(envelope):
                where = f"envelope[{index}]"
                if not isinstance(node, dict):
                    diagnostics.append(f"{where}: must be a dict")
                    continue
                sample = node.get("local_sample")
                if not isinstance(sample, int) or sample < 0:
                    diagnostics.append(f"{where}: local_sample must be a non-negative int")
                elif final_length is not None and sample > final_length:
                    diagnostics.append(
                        f"{where}: local_sample {sample} exceeds final output "
                        f"samples {final_length}")
                elif sample in seen_samples:
                    diagnostics.append(f"{where}: duplicate local_sample {sample}")
                else:
                    seen_samples.add(sample)
                if not isinstance(node.get("gain_db"), (int, float)):
                    diagnostics.append(f"{where}: gain_db must be numeric")
                if node.get("interpolation", "linear") != "linear":
                    diagnostics.append(
                        f"{where}: unsupported interpolation {node.get('interpolation')!r}")
    markers = audio_edit.get("markers", [])
    if markers is not None:
        if not isinstance(markers, list):
            diagnostics.append("audio_edit.markers must be a list")
        else:
            marker_ids: set[str] = set()
            for index, marker in enumerate(markers):
                where = f"markers[{index}]"
                if not isinstance(marker, dict):
                    diagnostics.append(f"{where}: must be a dict")
                    continue
                marker_id = marker.get("id")
                if not isinstance(marker_id, str) or not marker_id:
                    diagnostics.append(f"{where}: missing id")
                elif marker_id in marker_ids:
                    diagnostics.append(f"{where}: duplicate marker id")
                else:
                    marker_ids.add(marker_id)
                if not isinstance(marker.get("local_sample"), int) or marker["local_sample"] < 0:
                    diagnostics.append(f"{where}: local_sample must be a non-negative int")
    return diagnostics


def _validate_pre_render_step(
    where: str,
    processor_id: str,
    params: dict,
    *,
    rate: int,
    clip_output_samples: int | None,
) -> list[str]:
    """预渲染处理器的逐条诊断（值域/区间/映射一致性/依赖 SciPy）。"""
    diagnostics: list[str] = []
    if processor_id == "time_stretch":
        ratio_pct = params.get("ratio_pct")
        if (not isinstance(ratio_pct, (int, float))
                or not 25.0 <= float(ratio_pct) <= 400.0):
            diagnostics.append(
                f"{where}: ratio_pct {ratio_pct!r} must be within [25, 400]")
            return diagnostics
        semitones = params.get("pitch_semitones", 0.0)
        if (not isinstance(semitones, (int, float))
                or not -24.0 <= float(semitones) <= 24.0):
            diagnostics.append(
                f"{where}: pitch_semitones {semitones!r} must be within [±24]")
            return diagnostics
        output_length = params.get("output_length")
        if not isinstance(output_length, int) or output_length <= 0:
            diagnostics.append(
                f"{where}: time_stretch requires a positive int output_length")
            return diagnostics
        if clip_output_samples is not None:
            start, end = params.get("start"), params.get("end")
            if isinstance(start, int) and isinstance(end, int) and end > start:
                # output_length 是“本步骤后的定义域长度”（域级映射）。
                segment = _time_stretch_output_length(
                    end - start, float(ratio_pct))
                expected = clip_output_samples - (end - start) + segment
                if abs(output_length - expected) > 1:
                    diagnostics.append(
                        f"{where}: output_length {output_length} does not match "
                        f"ratio mapping (expected ≈{expected})")
        return diagnostics
    if processor_id == "delete_range":
        output_length = params.get("output_length")
        if not isinstance(output_length, int) or output_length < 0:
            diagnostics.append(
                f"{where}: delete_range requires a non-negative int output_length")
            return diagnostics
        if clip_output_samples is not None:
            start, end = params.get("start"), params.get("end")
            if isinstance(start, int) and isinstance(end, int) and end > start:
                expected = clip_output_samples - (end - start)
                if output_length != expected:
                    diagnostics.append(
                        f"{where}: output_length {output_length} does not match "
                        f"splice mapping (expected {expected})")
        return diagnostics
    if processor_id == "spectral_repair":
        from src.features.edit.core.audio_repair import SUPPORTED_MODES, SUPPORTED_NFFT
        amount = params.get("amount_db", 24.0)
        if (not isinstance(amount, (int, float))
                or not 0.0 <= float(amount) <= 48.0):
            diagnostics.append(
                f"{where}: amount_db {amount!r} must be within [0, 48]")
        if params.get("mode", "attenuate") not in SUPPORTED_MODES:
            diagnostics.append(
                f"{where}: mode {params.get('mode')!r} must be one of "
                f"{SUPPORTED_MODES}")
        if params.get("nfft", 2048) not in SUPPORTED_NFFT:
            diagnostics.append(
                f"{where}: nfft {params.get('nfft')!r} must be one of "
                f"{SUPPORTED_NFFT}")
        strokes = params.get("strokes")
        if not isinstance(strokes, list) or not strokes:
            diagnostics.append(f"{where}: spectral_repair requires strokes")
            return diagnostics
        nyquist = rate / 2.0
        for index, stroke in enumerate(strokes):
            if not isinstance(stroke, dict):
                diagnostics.append(f"{where}: strokes[{index}] must be a dict")
                continue
            sample = stroke.get("sample")
            hz = stroke.get("hz")
            radius = stroke.get("time_radius")
            band = stroke.get("bandwidth_oct")
            if not isinstance(sample, int) or not 0 <= sample:
                diagnostics.append(
                    f"{where}: strokes[{index}].sample must be a non-negative int")
            elif clip_output_samples is not None and sample > clip_output_samples:
                diagnostics.append(
                    f"{where}: strokes[{index}].sample {sample} exceeds domain "
                    f"{clip_output_samples}")
            if (not isinstance(hz, (int, float)) or not 0.0 < float(hz) <= nyquist):
                diagnostics.append(
                    f"{where}: strokes[{index}].hz must be within (0, Nyquist]")
            if (not isinstance(radius, (int, float)) or float(radius) <= 0.0):
                diagnostics.append(
                    f"{where}: strokes[{index}].time_radius must be positive")
            if (not isinstance(band, (int, float)) or float(band) <= 0.0):
                diagnostics.append(
                    f"{where}: strokes[{index}].bandwidth_oct must be positive")
        return diagnostics
    if processor_id != "spectral_denoise":
        return diagnostics
    from src.features.edit.core.audio_denoise import MIN_NOISE_SECONDS, SUPPORTED_NFFT
    nfft = params.get("nfft", 2048)
    if nfft not in SUPPORTED_NFFT:
        diagnostics.append(f"{where}: nfft {nfft!r} must be one of {SUPPORTED_NFFT}")
        nfft = 2048
    reduction_db = params.get("reduction_db", 12.0)
    if not isinstance(reduction_db, (int, float)) or not 0.0 <= float(reduction_db) <= 48.0:
        diagnostics.append(
            f"{where}: reduction_db {reduction_db!r} must be within [0, 48]")
    noise_start, noise_end = params.get("noise_start"), params.get("noise_end")
    if (not isinstance(noise_start, int) or not isinstance(noise_end, int)
            or not 0 <= noise_start < noise_end):
        diagnostics.append(
            f"{where}: noise range [{noise_start!r}, {noise_end!r}) is not half-open valid")
        return diagnostics
    if clip_output_samples is not None and noise_end > clip_output_samples:
        diagnostics.append(
            f"{where}: noise end {noise_end} exceeds clip output samples "
            f"{clip_output_samples}")
    # 采集时长与 DSP 同一门槛（≥ max(0.25 s, nfft + 5 hops)）。
    minimum = max(round(MIN_NOISE_SECONDS * rate), int(nfft) + 5 * (int(nfft) // 4))
    if noise_end - noise_start < minimum:
        diagnostics.append(
            f"{where}: noise print needs ≥ {minimum} samples "
            f"(≥ {MIN_NOISE_SECONDS:.2f} s) at {rate} Hz")
    return diagnostics


def _time_stretch_output_length(segment_samples: int, ratio_pct: float) -> int:
    """§6 长度合同：``output = round(N / (ratio_pct / 100))``。"""
    return max(1, int(round(segment_samples / (float(ratio_pct) / 100.0))))


# 公共别名（编辑器提交侧与 DSP 共用同一合同）。
time_stretch_output_length = _time_stretch_output_length


def _advance_domain_length(
    processor_id: str, params: dict, domain_length: int | None,
) -> int | None:
    """改长度步骤后的输出域长度（映射推进；无法判定时保持原值）。"""
    if domain_length is None:
        return None
    start, end = params.get("start"), params.get("end")
    if not isinstance(start, int) or not isinstance(end, int) or end <= start:
        return domain_length
    if processor_id == "time_stretch":
        return (domain_length - (end - start)
                + _time_stretch_output_length(
                    end - start, float(params.get("ratio_pct", 100.0))))
    if processor_id == "delete_range":
        return max(0, domain_length - (end - start))
    return domain_length


def output_length_after_steps(audio_edit: Any, total_samples: int) -> int:
    """整栈后的片段输出长度（inline 步骤保长；逐 pre-render 步骤推进）。"""
    length = int(total_samples)
    if not isinstance(audio_edit, dict):
        return length
    for step in audio_edit.get("steps", []) or []:
        if not isinstance(step, dict) or not step.get("enabled", True):
            continue
        if step.get("processor_id") not in LENGTH_CHANGING_PROCESSORS:
            continue
        params = step.get("params", {}) if isinstance(step.get("params"), dict) else {}
        start, end = params.get("start"), params.get("end")
        if not isinstance(start, int) or not isinstance(end, int) or end <= start:
            continue
        if end > length:
            end = length  # 防御：越界步骤按钳制后的映射推进
        if step.get("processor_id") == "time_stretch":
            length = (length - (end - start)
                      + _time_stretch_output_length(
                          end - start, float(params.get("ratio_pct", 100.0))))
        else:  # delete_range
            length = max(0, length - (end - start))
    return length


def remap_position_through_step(
    position: int, start: int, end: int, segment_output: int,
) -> int:
    """把输入域位置映射到改长度步骤的输出域（A/B 试听与重定位共用）。

    ``[start, end)`` 变为 ``[start, start + segment_output)``；区间前不动、
    区间内线性、区间后平移 ``segment_output - (end - start)``。
    """
    if position <= start:
        return position
    if position >= end:
        return position - (end - start) + segment_output
    span = max(1, end - start)
    return start + int(round((position - start) * segment_output / span))


def invert_position_through_step(
    position: "int | Fraction", start: int, end: int, segment_output: int,
) -> Fraction:
    """改长度步骤输出域位置 → 输入域位置（精确有理数，不取整）。

    与 ``remap_position_through_step`` 互逆（delete 的输出侧无缝，输入侧
    被删区间不产生输出位置——逆映射天然无塌缩歧义）。前缀烘焙时把
    最终域存储的 inline 步骤区间折回其机架位置所在的域。
    """
    pos = Fraction(int(position))
    if pos <= start:
        return pos
    if segment_output <= 0:  # delete_range：输出 ≥ start 一律来自输入 ≥ end
        return pos + (end - start)
    new_end = start + segment_output
    if pos >= new_end:
        return pos - segment_output + (end - start)
    span = max(1, end - start)
    return start + (pos - start) * span / segment_output


def _step_segment_output(processor_id: str, params: dict) -> int:
    """改长度步骤作用段的输出长度（delete=0；stretch=合同长度）。"""
    start, end = params.get("start"), params.get("end")
    if not isinstance(start, int) or not isinstance(end, int) or end <= start:
        return max(0, end - start) if isinstance(end, int) and isinstance(start, int) else 0
    if processor_id == "time_stretch":
        return _time_stretch_output_length(
            end - start, float(params.get("ratio_pct", 100.0)))
    return 0  # delete_range


def _map_processing_range(
    params: dict, map_range: Callable[[int, int], tuple[int, int]],
) -> bool:
    """Move the output-domain range and keep insertion-domain DSP anchors.

    Captured noise intervals and brush strokes refer to the samples seen by
    that processor, before any later speed or length changes. Older projects
    already store those anchors in that domain.
    """
    lo, hi = params.get("start"), params.get("end")
    if isinstance(lo, int) and isinstance(hi, int) and hi > lo:
        lo, hi = map_range(lo, hi)
        if hi <= lo:
            return False
        params["start"], params["end"] = lo, hi
    return True


def remap_audio_edit_for_length_steps(
    audio_edit: Any, length_steps: list[dict],
) -> tuple[Any, list[str]]:
    """把 inline 步骤/包络/标记重定位到新增改长度步骤后的输出域。

    返回 ``(新 audio_edit, warnings)``；塌缩（end ≤ start）的 inline 步骤
    被移除并在 warnings 里点名——「不可重映射时显示冲突」的 v1 语义。
    """
    if not length_steps or not isinstance(audio_edit, dict):
        return audio_edit, []
    warnings: list[str] = []
    updated = copy.deepcopy(audio_edit)

    def _shift(position: int) -> int:
        for step in length_steps:
            if not isinstance(step, dict):
                continue
            params = step.get("params", {}) if isinstance(step.get("params"), dict) else {}
            start, end = params.get("start"), params.get("end")
            if isinstance(start, int) and isinstance(end, int) and end > start:
                position = remap_position_through_step(
                    position, start, end,
                    _step_segment_output(step.get("processor_id", ""), params))
        return position

    kept_steps = []
    for step in updated.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        processor_id = step.get("processor_id", "")
        if processor_id in LENGTH_CHANGING_PROCESSORS:
            kept_steps.append(step)
            continue
        params = step.get("params", {})
        if isinstance(params, dict) and not _map_processing_range(params, lambda lo, hi: (_shift(lo), _shift(hi))):
            warnings.append(f"step {step.get('step_id')} collapsed inside an edit and was removed")
            continue
        kept_steps.append(step)
    updated["steps"] = kept_steps
    if updated.get("envelope"):
        for node in updated["envelope"]:
            if isinstance(node, dict) and isinstance(node.get("local_sample"), int):
                node["local_sample"] = _shift(node["local_sample"])
        updated["envelope"].sort(key=lambda n: n.get("local_sample", 0))
        # 重定位后同 sample 唯一：后写覆盖前写。
        deduped: list[dict] = []
        for node in updated["envelope"]:
            if deduped and deduped[-1].get("local_sample") == node.get("local_sample"):
                deduped[-1] = node
            else:
                deduped.append(node)
        updated["envelope"] = deduped
    if updated.get("markers"):
        for marker in updated["markers"]:
            if isinstance(marker, dict) and isinstance(marker.get("local_sample"), int):
                marker["local_sample"] = _shift(marker["local_sample"])
        updated["markers"].sort(key=lambda m: m.get("local_sample", 0))
    return updated, warnings


def unmap_audio_edit_for_removed_length_steps(
    audio_edit: Any, removed_steps: list[dict],
) -> tuple[Any, list[str]]:
    """P1-3 · 反向编辑：被移除改长度步骤的坐标逆映射。

    旁路/删除/替换/清空改长度步骤时，inline 步骤/包络/标记的持久化
    坐标在**旧最终输出域**——经被移除步骤的逆映射（按机架逆序逐个，
    ``invert_position_through_step``）折回新输出域。区间端点落入
    delete_range 删除区间的节点/步骤不可映射（塌缩）→ 移除并点名
    （「不可重映射时显示冲突」的语义）。
    """
    if not removed_steps or not isinstance(audio_edit, dict):
        return audio_edit, []
    warnings: list[str] = []
    updated = copy.deepcopy(audio_edit)

    def _unmap_position(position: int, step: dict) -> int:
        params = step.get("params", {}) if isinstance(
            step.get("params"), dict) else {}
        start, end = int(params.get("start", 0)), int(params.get("end", 0))
        seg = _step_segment_output(step.get("processor_id", ""), params)
        # delete_range 的输出域无孔：落入删除区间的输入位置不存在——
        # 逆映射天然无歧义（见 invert_position_through_step 契约）。
        return int(invert_position_through_step(position, start, end, seg))

    for step in reversed(removed_steps):
        params = step.get("params", {}) if isinstance(
            step.get("params"), dict) else {}
        start, end = int(params.get("start", 0)), int(params.get("end", 0))
        processor = step.get("processor_id", "")
        kept_steps = []
        for existing in updated.get("steps", []) or []:
            if not isinstance(existing, dict):
                continue
            if existing.get("processor_id") in LENGTH_CHANGING_PROCESSORS:
                kept_steps.append(existing)  # 其余改长度步骤保留原坐标
                continue
            e_params = (existing.get("params", {})
                        if isinstance(existing.get("params"), dict) else {})
            if not _map_processing_range(
                e_params, lambda lo, hi: (_unmap_position(lo, step), _unmap_position(hi, step)),
            ):
                warnings.append(f"step {existing.get('step_id')} collapsed when the {processor} step was removed")
                continue
            kept_steps.append(existing)
        updated["steps"] = kept_steps
        if updated.get("envelope"):
            kept_nodes = []
            for node in updated["envelope"]:
                if not isinstance(node, dict):
                    continue
                mapped = _unmap_position(int(node.get("local_sample", 0)), step)
                if kept_nodes and mapped <= kept_nodes[-1].get("local_sample", -1):
                    warnings.append(
                        f"envelope node {node.get('id')} collapsed when the "
                        f"{processor} step was removed and was dropped")
                    continue
                node["local_sample"] = mapped
                kept_nodes.append(node)
            updated["envelope"] = kept_nodes
        if updated.get("markers"):
            for marker in updated["markers"]:
                if isinstance(marker, dict):
                    marker["local_sample"] = _unmap_position(
                        int(marker.get("local_sample", 0)), step)
            updated["markers"].sort(key=lambda m: m.get("local_sample", 0))
    return updated, warnings


def requires_pre_render(audio_edit: Any) -> bool:
    """enabled 步骤里是否含预渲染处理器（决定渲染路径走缓存基底）。"""
    if not isinstance(audio_edit, dict):
        return False
    for step in audio_edit.get("steps", []) or []:
        if (isinstance(step, dict) and step.get("enabled", True)
                and step.get("processor_id") in PRE_RENDER_PROCESSORS):
            return True
    return False


def pre_render_steps(audio_edit: Any) -> list[dict]:
    """按栈序取出 enabled 的预渲染步骤（整段烘焙的输入）。"""
    if not isinstance(audio_edit, dict):
        return []
    return [
        step for step in audio_edit.get("steps", []) or []
        if (isinstance(step, dict) and step.get("enabled", True)
            and step.get("processor_id") in PRE_RENDER_PROCESSORS)
    ]


def last_pre_render_step_index(audio_edit: Any) -> int:
    """最后一个 **enabled** 预渲染步骤的下标（无则 −1）。"""
    if not isinstance(audio_edit, dict):
        return -1
    last = -1
    for index, step in enumerate(audio_edit.get("steps", []) or []):
        if (isinstance(step, dict) and step.get("enabled", True)
                and step.get("processor_id") in PRE_RENDER_PROCESSORS):
            last = index
    return last


def render_prefix_steps(audio_edit: Any) -> list[dict]:
    """P0 · 缓存前缀：截至最后一个 enabled 预渲染步骤的全部 enabled 步骤。

    用户机架顺序即处理顺序——前缀里混排的 inline 步骤（Gain→Denoise 与
    Denoise→Gain 非通勤）必须一并烘焙进缓存基底，不能在实时块上重放
    （那等于把机架顺序重排为"全部预渲染在前"）。后缀（最后一个预渲染
    步骤之后的步骤）才走实时块。
    """
    if not isinstance(audio_edit, dict):
        return []
    last = last_pre_render_step_index(audio_edit)
    if last < 0:
        return []
    return [
        step for step in (audio_edit.get("steps", []) or [])[:last + 1]
        if isinstance(step, dict) and step.get("enabled", True)
    ]


def suffix_audio_edit(audio_edit: Any) -> Any:
    """P0 · 缓存后缀：最后一个 enabled 预渲染步骤**之后**的步骤 + 包络/标记。

    基底在场时实时块只应用后缀（前缀已烘焙）；预渲染缺席的回退仍走
    ``inline_audio_edit``（能放的 inline 都放，降噪缺席但可听）。
    """
    if not isinstance(audio_edit, dict):
        return audio_edit
    last = last_pre_render_step_index(audio_edit)
    if last < 0:
        return audio_edit
    updated = copy.deepcopy(audio_edit)
    updated["steps"] = list(updated.get("steps", []))[last + 1:]
    return updated


def inline_audio_edit(audio_edit: Any) -> Any:
    """去掉预渲染步骤的 audio_edit 副本（块路径在缓存基底上应用的部分）。

    包络/标记保留（块求值器只处理 inline 步骤 + 包络）；无步骤且无包络时
    返回仅含空 steps 的结构，``apply_clip_audio_edit`` 自然恒等。
    """
    if not isinstance(audio_edit, dict):
        return audio_edit
    updated = copy.deepcopy(audio_edit)
    updated["steps"] = [
        step for step in updated.get("steps", []) or []
        if not (isinstance(step, dict)
                and step.get("processor_id") in PRE_RENDER_PROCESSORS)
    ]
    return updated


def step_identity_key(step: Any) -> tuple | None:
    """P0-1 · 步骤的操作身份键（处理器+操作+半开区间+声道集合）。

    ``None`` 表示无法解析（非 dict 步骤）。gain_db/label 不参与身份——
    它们是同一次操作的参数而非身份。
    """
    if not isinstance(step, dict):
        return None
    params = step.get("params", {}) if isinstance(step.get("params"), dict) else {}
    return (step.get("processor_id"), params.get("operation"),
            params.get("start"), params.get("end"),
            tuple(params.get("channels") or ()))


def upsert_step(audio_edit: Any, step: dict) -> dict:
    """P1 · 同约束幂等插入：替换处理器、操作身份、区间与声道相同的步骤。

    Normalize/LUFS 连击同一选区不得叠加同一个增益——测量基于插入阶段
    的实际数据，重复提交应以**替换**语义保持单一增益。无匹配则按
    ``append_step`` 追加（机架顺序保留）。
    """
    if not isinstance(audio_edit, dict):
        return append_step(audio_edit, step)
    identity = step_identity_key(step)
    updated = copy.deepcopy(audio_edit)
    for existing in updated.get("steps", []) or []:
        if step_identity_key(existing) == identity:
            replacement = dict(step)
            replacement["step_id"] = existing.get("step_id") or step.get("step_id")
            steps = list(updated.get("steps", []))
            steps[steps.index(existing)] = replacement
            updated["steps"] = steps
            return updated
    return append_step(updated, step)


def tag_legacy_measurement_operations(
    audio_edit: Any, *, legacy_labels: "tuple[str, ...]" = (),
) -> Any:
    """P0-1 · 旧工程迁移：为无操作身份的历史测量步骤补上稳定身份。

    识别规则（只点名测量类步骤，其他历史 ``range_gain`` 原样保留）：

    - ``normalize`` 步骤且无 ``operation`` → ``normalize_peak``；
    - ``range_gain`` 步骤且无 ``operation``、label 命中
      ``legacy_labels``（响度匹配的历史标签，含本地化变体）→
      ``loudness_match``。

    返回迁移后的深拷贝；无匹配步骤时返回原对象语义的拷贝。
    """
    if not isinstance(audio_edit, dict):
        return audio_edit
    updated = copy.deepcopy(audio_edit)
    for step in updated.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        processor_id = step.get("processor_id")
        params = step.get("params", {})
        if not isinstance(params, dict) or params.get("operation"):
            continue
        if processor_id == "normalize":
            params["operation"] = OPERATION_NORMALIZE_PEAK
        elif (processor_id == "range_gain"
                and str(params.get("label", "")) in legacy_labels):
            params["operation"] = OPERATION_LOUDNESS_MATCH
    return updated


def find_measurement_step_index(
    audio_edit: Any, *, processor_id: str, operation: str,
    start: int, end: int, channels: "tuple[int, ...] | list[int]",
    legacy_labels: "tuple[str, ...]" = (),
) -> int:
    """P0-1 · 定位将被替换的既有步骤下标（无匹配 = 追加位置 = 栈末）。

    先按与 ``upsert_step`` 一致的身份键匹配（含 ``operation``）；未命中
    再按迁移规则识别旧工程无身份的步骤（normalize 无 operation /
    range_gain+legacy label），替换语义对旧步骤同样生效。
    """
    if not isinstance(audio_edit, dict):
        return 0
    steps = audio_edit.get("steps", []) or []
    target = (processor_id, operation, int(start), int(end), tuple(channels))
    legacy: dict[str, set[str] | bool] = {}
    if processor_id == "normalize":
        legacy["normalize"] = True
    if processor_id == "range_gain" and operation == OPERATION_LOUDNESS_MATCH:
        legacy["range_gain_labels"] = set(legacy_labels)
    for index, existing in enumerate(steps):
        key = step_identity_key(existing)
        if key is None:
            continue
        if key[:2] != target[:2]:
            continue
        if (key[2], key[3], tuple(key[4])) != (target[2], target[3], target[4]):
            continue
        if key[1] == operation:
            return index
        # 身份键 operation 缺失 → 迁移识别（见 tag_legacy…）。
        if key[1] is None:
            pid = key[0]
            if (pid == "normalize" and "normalize" in legacy
                    and legacy["normalize"]):
                return index
            labels = legacy.get("range_gain_labels")
            if (pid == "range_gain" and labels is not None):
                params = existing.get("params", {}) if isinstance(
                    existing.get("params"), dict) else {}
                if str(params.get("label", "")) in labels:
                    return index
    return len(steps)


def _enabled_length_maps(steps: list, from_index: int) -> list[tuple[int, int, int]]:
    """``steps[from_index:]`` 中 enabled 改长度步骤的 forward map（按机架序）。

    供测量基准渲染把最终输出域的区间/坐标折回插入点域——与
    ``render_pre_render_steps._later_length_maps`` 同一构造，只是作用域
    覆盖整个栈（改长度步骤全部是预渲染步骤，均在机架前缀内）。
    """
    maps: list[tuple[int, int, int]] = []
    for step in steps[from_index:]:
        if not isinstance(step, dict) or not step.get("enabled", True):
            continue
        params = step.get("params", {}) if isinstance(
            step.get("params"), dict) else {}
        pid = step.get("processor_id")
        if pid == "time_stretch":
            s, e = int(params.get("start", 0)), int(params.get("end", 0))
            maps.append((s, e, _time_stretch_output_length(
                max(0, e - s), float(params.get("ratio_pct", 100.0)))))
        elif pid == "delete_range":
            maps.append((int(params.get("start", 0)), int(params.get("end", 0)), 0))
    return maps


def _invert_range_through_maps(
    start: int, end: int, maps: list[tuple[int, int, int]],
) -> tuple[int, int]:
    """最终输出域半开区间 → 经改长度步骤逆映射（反向逐个，floor/ceil）。"""
    lo, hi = Fraction(int(start)), Fraction(int(end))
    for s, e, seg in reversed(maps):
        lo = invert_position_through_step(lo, s, e, seg)
        hi = invert_position_through_step(hi, s, e, seg)
    return max(0, math.floor(lo)), max(1, math.ceil(hi))


def map_selection_to_insertion(
    audio_edit: Any, start: int, end: int, insertion_index: int,
) -> tuple[int, int]:
    """P0-1 · 最终输出域选区 → 插入点域选区（供测量基准取段）。"""
    steps = (audio_edit.get("steps", []) or []) if isinstance(audio_edit, dict) else []
    maps = _enabled_length_maps(steps, insertion_index)
    if not maps:
        return int(start), int(end)
    return _invert_range_through_maps(int(start), int(end), maps)


def length_step_sample_maps(audio_edit: Any) -> tuple[tuple[int, int, int], ...]:
    """Enabled length steps as (input start, input end, output length)."""
    steps = (audio_edit.get("steps", []) or []) if isinstance(audio_edit, dict) else []
    return tuple(_enabled_length_maps(steps, 0))


def render_to_step_insertion(
    pcm: np.ndarray,
    audio_edit: Any,
    *,
    insertion_index: int,
    sample_rate: int = PROCESSING_RATE,
    progress: Any = None,
    cancel_check: Any = None,
) -> np.ndarray:
    """P0-1 · 渲染到目标步骤插入点的 PCM（替换时移除被替换系数）。

    按机架顺序应用 ``insertion_index`` 之前的全部 enabled 步骤；inline
    步骤的持久化区间在最终输出域，经 ``_enabled_length_maps`` 折回其
    机架位置所在域后应用（与 ``render_pre_render_steps`` 前缀烘焙同一
    语义，但折叠覆盖整个栈——改长度步骤均为预渲染步骤）。包络/标记
    在全部步骤之后应用，不属于插入点基准。不含被替换步骤本身。
    """
    if not isinstance(audio_edit, dict):
        return pcm
    steps = audio_edit.get("steps", []) or []
    prefix = steps[:max(0, int(insertion_index))]
    if not prefix:
        return pcm
    from src.features.edit.core.audio_denoise import (
        denoise_spectral_subtraction, scipy_available,
    )
    out = pcm
    for index, step in enumerate(prefix):
        if cancel_check is not None and cancel_check():
            return out
        if not isinstance(step, dict) or not step.get("enabled", True):
            continue
        processor_id = step.get("processor_id")
        params = step.get("params", {}) if isinstance(
            step.get("params"), dict) else {}
        length_maps = _enabled_length_maps(steps, index + 1)
        if length_maps and processor_id not in LENGTH_CHANGING_PROCESSORS:
            params = dict(params)
            if not _map_processing_range(
                params, lambda lo, hi: _invert_range_through_maps(lo, hi, length_maps),
            ):
                continue
        if processor_id in PRE_RENDER_PROCESSORS:
            if not scipy_available():
                raise RuntimeError(
                    "SciPy DSP backend unavailable for pre-render steps")
            if processor_id == "spectral_denoise":
                def _progress(fraction: float, _params=params) -> bool:
                    if progress is not None:
                        progress(float(fraction))
                    return not (cancel_check is not None and cancel_check())

                out = denoise_spectral_subtraction(
                    out,
                    sample_rate,
                    (int(params.get("noise_start", 0)), int(params.get("noise_end", 0))),
                    (int(params.get("start", 0)), int(params.get("end", 0))),
                    float(params.get("reduction_db", 12.0)),
                    nfft=int(params.get("nfft", 2048)),
                    link_stereo=bool(params.get("link_stereo", True)),
                    # P1-1 · 预渲染分支消费请求的声道（未选通道不变）。
                    channels=params.get("channels") or None,
                    progress=_progress,
                )
            elif processor_id == "time_stretch":
                out = _render_time_stretch_step(
                    out, params, sample_rate,
                    progress=progress, cancel_check=cancel_check)
            elif processor_id == "delete_range":
                out = _render_delete_range_step(out, params, sample_rate)
            elif processor_id == "pitch_correction":
                from src.features.edit.core.audio_effects.pitch_correction                     import render_pitch_correction
                out = render_pitch_correction(
                    out,
                    sample_rate,
                    ref_a4_hz=float(params.get("ref_a4_hz", 440.0)),
                    strength=float(params.get("strength", 100.0)),
                    retune_ms=float(params.get("retune_ms", 45.0)),
                    bypass_notes=tuple(params.get("bypass_notes") or ()),
                )
            elif processor_id == "doubler":
                from src.features.edit.core.audio_effects.doubler import (
                    doubles_from_params,
                    render_doubler,
                )
                out = render_doubler(
                    out,
                    sample_rate,
                    doubles_from_params(params),
                    dry_gain_db=float(params.get("dry_gain_db", 0.0)),
                    wet_mix_pct=float(params.get("wet_mix_pct", 100.0)),
                    output_gain_db=float(params.get("output_gain_db", 0.0)),
                )
            elif processor_id == "spectral_repair":
                from src.features.edit.core.audio_repair import repair_spectral
                out = repair_spectral(
                    out,
                    sample_rate,
                    list(params.get("strokes", []) or []),
                    (int(params.get("start", 0)), int(params.get("end", 0))),
                    amount_db=float(params.get("amount_db", 24.0)),
                    mode=str(params.get("mode", "attenuate")),
                    nfft=int(params.get("nfft", 2048)),
                    link_stereo=bool(params.get("link_stereo", True)),
                    # P1-1 · 预渲染分支消费请求的声道（未选通道不变）。
                    channels=params.get("channels") or None,
                    progress=progress,
                )
            else:
                raise RuntimeError(
                    f"pre-render processor {processor_id!r} has no renderer")
            continue
        if processor_id not in KNOWN_PROCESSOR_IDS:
            continue  # 未知处理器：保持原样（上游 validate 负责阻断）
        mapped = {**step, "params": params}
        temp = {"version": AUDIO_EDIT_SCHEMA_VERSION,
                "processing_rate": PROCESSING_RATE, "steps": [mapped]}
        out = apply_clip_audio_edit(out, 0, temp, sample_rate=sample_rate)
    return out


def render_audio_edit_full(
    pcm: np.ndarray,
    audio_edit: Any,
    *,
    sample_rate: int = PROCESSING_RATE,
    progress: Any = None,
    cancel_check: Any = None,
) -> np.ndarray:
    """P0-1 · 全栈整段渲染（前缀烘焙 + 后缀 inline + 包络），复测共用。

    与编辑器预览 ``_compute_preview`` 同一语义：预渲染步骤整段烘焙成
    基底，后缀 inline 步骤与包络在其上应用。测量候选步骤的**最终输出
    读数**时使用（插入点目标见 ``render_to_step_insertion``）。
    """
    if not isinstance(audio_edit, dict):
        return pcm
    steps = audio_edit.get("steps", []) or []
    if not steps and not envelope_nodes(audio_edit):
        return pcm
    base = pcm
    if requires_pre_render(audio_edit):
        base = render_pre_render_steps(
            pcm, audio_edit, sample_rate=sample_rate,
            progress=progress, cancel_check=cancel_check)
    return apply_audio_edit_to_pcm(
        base, suffix_audio_edit(audio_edit), sample_rate=sample_rate)


def replace_processor_step(audio_edit: Any, processor_id: str, step: dict) -> dict:
    """用 ``step`` 替换该处理器的既有步骤（没有则追加；其余步骤保持原序）。

    降噪等“重调参数”类提交的幂等入口——重复点击不会堆叠同处理器步骤。
    """
    base = copy_audio_edit(audio_edit) if isinstance(audio_edit, dict) else new_audio_edit()
    steps = [
        existing for existing in base.get("steps", []) or []
        if not (isinstance(existing, dict)
                and existing.get("processor_id") == processor_id)
    ]
    steps.append(step)
    base["steps"] = steps
    return base


def render_pre_render_steps(
    pcm: np.ndarray,
    audio_edit: Any,
    *,
    sample_rate: int = PROCESSING_RATE,
    progress: Any = None,
    cancel_check: Any = None,
) -> np.ndarray:
    """整段应用缓存前缀（缓存构建/编辑器预览共用；不修改输入）。

    P0 · 机架顺序即处理顺序：前缀 = 截至最后一个 enabled 预渲染步骤的
    全部 enabled 步骤（``render_prefix_steps``）。inline 步骤的持久化区间
    在最终输出域——若其后还有改长度步骤，先经 ``invert_position_through_step``
    逐个折回当前域再整段应用。SciPy 缺席时显式抛错——静默旁路会破坏
    「试听 == 播放 == 导出」合同。``progress``: Callable[[float], None]；
    ``cancel_check``: Callable[[], bool]。
    """
    index = last_pre_render_step_index(audio_edit)
    if index < 0:
        return pcm
    return render_to_step_insertion(
        pcm, audio_edit, insertion_index=index + 1, sample_rate=sample_rate,
        progress=progress, cancel_check=cancel_check)


def _render_time_stretch_step(
    pcm: np.ndarray, params: dict, sample_rate: int, *,
    progress: Any = None, cancel_check: Any = None,
) -> np.ndarray:
    """选区变速/音高：两侧上下文原样拼接（§6：映射计入最终长度）。"""
    from src.features.edit.core.audio_timepitch import (
        pitch_shift, retime_nopreserve, retime_preserve,
    )
    start = max(0, int(params.get("start", 0)))
    end = min(pcm.shape[1], int(params.get("end", pcm.shape[1])))
    if end <= start:
        return pcm
    ratio_pct = float(params.get("ratio_pct", 100.0))
    semitones = float(params.get("pitch_semitones", 0.0))
    preserve = bool(params.get("preserve_pitch", True))
    segment = pcm[:, start:end]
    if abs(semitones) > 1e-6:
        segment = pitch_shift(segment, sample_rate, semitones)
    if abs(ratio_pct - 100.0) > 1e-6:
        segment = (retime_preserve if preserve else retime_nopreserve)(
            segment, sample_rate, ratio_pct)
    if cancel_check is not None and cancel_check():
        return pcm
    if progress is not None:
        progress(1.0)
    # 契约长度为准（§6）：实际 DSP 输出与 round(N/ratio) 允许 ≤2 样本差，
    # 超出的部分补零/截断——缓存基底长度与 output_length 映射保持精确一致。
    contract = _time_stretch_output_length(end - start, ratio_pct)
    if abs(segment.shape[1] - contract) > 2:
        raise RuntimeError(
            f"time_stretch produced {segment.shape[1]} samples, contract is "
            f"{contract}")
    if segment.shape[1] < contract:
        segment = np.concatenate([
            segment,
            np.zeros((segment.shape[0], contract - segment.shape[1]),
                     dtype=np.float32)], axis=1)
    elif segment.shape[1] > contract:
        segment = segment[:, :contract]
    return np.ascontiguousarray(np.concatenate(
        [pcm[:, :start], segment, pcm[:, end:]], axis=1), dtype=np.float32)


def _render_delete_range_step(
    pcm: np.ndarray, params: dict, sample_rate: int,
) -> np.ndarray:
    """内部删除：两侧拼接 + 接缝 3 ms 淡出/淡入（长度恰为 N − (end−start)）。

    保长优先于重叠交叉淡化（重叠会把映射长度缩短一个 fade 段）；接缝
    两侧同时做短斜坡，消除拼接点阶跃（§3「防爆音」的硬要求）。
    """
    start = max(0, int(params.get("start", 0)))
    end = min(pcm.shape[1], int(params.get("end", 0)))
    if end <= start:
        return pcm
    prefix = np.array(pcm[:, :start], dtype=np.float32, copy=True)
    suffix = np.array(pcm[:, end:], dtype=np.float32, copy=True)
    fade = min(edge_fade_samples(sample_rate),
               prefix.shape[1], suffix.shape[1])
    if fade > 0:
        fall = np.linspace(1.0, 0.0, fade, dtype=np.float32)
        rise = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        prefix[:, prefix.shape[1] - fade:] *= fall
        suffix[:, :fade] *= rise
    return np.ascontiguousarray(
        np.concatenate([prefix, suffix], axis=1), dtype=np.float32)


def _slice_pre_render_base(
    base: np.ndarray, block_start: int, count: int,
) -> np.ndarray:
    """从整段基底切出块（尾端不足补零——帧↔样本舍入会多出少量样本）。"""
    lo = max(0, int(block_start))
    hi = min(base.shape[1], lo + max(0, count))
    if hi <= lo:
        return np.zeros((base.shape[0], max(0, count)), dtype=np.float32)
    block = base[:, lo:hi]
    if hi - lo < count:
        block = np.concatenate([
            block,
            np.zeros((base.shape[0], count - (hi - lo)), dtype=np.float32),
        ], axis=1)
    return block


def _edit_processing_rate(audio_edit: Any) -> int:
    """栈声明的持久化坐标率（validate 强制 48000；防御缺省）。"""
    if isinstance(audio_edit, dict):
        try:
            return int(audio_edit.get("processing_rate", PROCESSING_RATE))
        except (TypeError, ValueError):
            return PROCESSING_RATE
    return PROCESSING_RATE


def _envelope_nodes_at_rate(
    nodes: list[dict], processing_rate: int, consumption_rate: int,
) -> list[dict]:
    """包络节点 48k 参考域 → 消费率域（仅换算 local_sample，dB 不动）。"""
    if processing_rate == consumption_rate:
        return nodes
    converted: list[dict] = []
    for node in nodes:
        scaled = dict(node)
        scaled["local_sample"] = reference_position_to_rate(
            int(node["local_sample"]), consumption_rate)
        converted.append(scaled)
    return converted


def _slice_pre_render_base_rated(
    base: np.ndarray, block_start: int, count: int,
    processing_rate: int, consumption_rate: int,
) -> np.ndarray:
    """跨率基底切块：块位置/长度在消费率域，基底在 48k 参考域。

    同率直接整数切片；跨率按精确有理数边界切出覆盖段后线性重采样
    到块长（绝对位置对齐——任意分块与整段一致）。基底域内的"参考
    端点 + 1 样本"钳制保证与同率路径相同的边界归属（≤1 输出样本）。
    """
    if processing_rate == consumption_rate or count <= 0:
        return _slice_pre_render_base(base, block_start, count)
    # 块 [block_start, block_start+count) 消费率样本 → 基底参考率域窗口
    lo_f = Fraction(int(block_start)) * processing_rate / int(consumption_rate)
    hi_f = Fraction(int(block_start) + int(count)) * processing_rate / int(consumption_rate)
    lo = max(0, math.floor(lo_f))
    hi = min(base.shape[1], math.ceil(hi_f) + 1)  # +1：interp 右端闭钳制
    if hi <= lo:
        return np.zeros((base.shape[0], max(0, count)), dtype=np.float32)
    segment = base[:, lo:hi]
    seg_positions = np.arange(lo, lo + segment.shape[1], dtype=np.float64)
    # 输出样本在基底域的绝对有理位置（浮点承载，53 位尾数足够）。
    out_positions = (
        np.arange(int(block_start), int(block_start) + int(count), dtype=np.float64)
        * (float(processing_rate) / float(consumption_rate))
    )
    out = np.empty((base.shape[0], int(count)), dtype=np.float32)
    beyond = out_positions > (base.shape[1] - 1)
    for ch in range(base.shape[0]):
        out[ch] = np.interp(out_positions, seg_positions, segment[ch])
    # 基底末尾之后补零（EOF 外静音），不钳制末样本值。
    if beyond.any():
        out[:, beyond] = 0.0
    return out


def apply_clip_audio_edit_block(
    samples: np.ndarray,
    block_start_sample: int,
    audio_edit: Any,
    *,
    sample_rate: int = PROCESSING_RATE,
    pre_render_base: np.ndarray | None = None,
) -> np.ndarray:
    """三条渲染路径共用的块入口：预渲染基底 + inline 步骤/包络。

    - ``pre_render_base`` 提供时（缓存命中）：以基底切块替换解码块，再应用
      inline 步骤 + 包络（预渲染步骤已烘焙在基底里）。
    - 缺席时（缓存构建中）：临时回退为 inline-only——构建完成由引擎签名
      变化触发重建接入降噪基底，栈状态可见（排队/计算/已提交）。
    """
    if audio_edit is None or not isinstance(audio_edit, dict):
        return samples
    if requires_pre_render(audio_edit):
        if pre_render_base is not None and samples.shape[-1] > 0:
            # P0：基底在场——前缀（含混排的 inline 步骤）已烘焙，实时块
            # 只应用后缀。
            audio_edit = suffix_audio_edit(audio_edit)
            processing_rate = _edit_processing_rate(audio_edit)
            block = _slice_pre_render_base_rated(
                pre_render_base, block_start_sample, samples.shape[-1],
                processing_rate, int(sample_rate))
            if block.shape[0] != samples.shape[0]:
                # 解码层 >2ch 已混到 stereo；基底与输出声道数不一致时按
                # 导出/播放的同一保守策略（首声道广播/截断）对齐。
                if block.shape[0] == 1:
                    block = np.repeat(block, samples.shape[0], axis=0)
                else:
                    block = block[:samples.shape[0]]
            samples = np.ascontiguousarray(block, dtype=np.float32)
    return apply_clip_audio_edit(
        samples, block_start_sample, audio_edit, sample_rate=sample_rate)


def clip_block_from_pre_render(
    audio_edit: Any,
    pre_render_base: np.ndarray | None,
    block_start_sample: int,
    count: int,
    *,
    sample_rate: int,
    channels: int,
) -> Optional[np.ndarray]:
    """P0 · 预渲染成品作为独立源的统一块供给（实时/成片/Stem 共用）。

    源解码负责输入；缓存成品负责**有效输出范围**——time_stretch 等
    改长度步骤烘焙出的成品比源长时，源 EOF 后的块由本函数直接供出，
    块存在性不再由源解码决定（修复「50% 播放生成 2 s 缓存、实时第二
    秒仍全静音」）。先查渲染状态（栈是否需要预渲染 + 基底是否就绪），
    再按当前 revision 成品切块；返回 None 表示无成品可用（调用方保持
    原 continue 语义）。返回块已应用 inline 步骤/包络，与
    ``apply_clip_audio_edit_block`` 对已解码块的处理完全一致。
    """
    if (pre_render_base is None or count <= 0
            or not isinstance(audio_edit, dict)
            or not requires_pre_render(audio_edit)):
        return None
    zeros = np.zeros((max(1, int(channels)), int(count)), dtype=np.float32)
    return apply_clip_audio_edit_block(
        zeros, block_start_sample, audio_edit,
        sample_rate=int(sample_rate), pre_render_base=pre_render_base)


def steps_signature(audio_edit: Any) -> tuple:
    """供音频图快照/重建签名使用的稳定元组（enabled 步骤 + 包络）。"""
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
            # §3.3/§6：任何参数变化（reduction_db / ratio_pct / output_length…）
            # 都必须扰动签名——否则引擎不重建、缓存 key 不变。
            tuple(sorted((key, repr(value))
                         for key, value in params.items())),
        ))
    signature.extend(envelope_signature(audio_edit))
    return tuple(signature)


# ----------------------------------------------------------------------
# 片段音量包络（P1-4）：节点 {id, local_sample, gain_db, interpolation}
# ----------------------------------------------------------------------
def envelope_nodes(audio_edit: Any) -> list[dict]:
    """读取包络节点（按 local_sample 升序；非法项剔除）。"""
    if not isinstance(audio_edit, dict):
        return []
    nodes = []
    for node in audio_edit.get("envelope", []) or []:
        if (isinstance(node, dict) and isinstance(node.get("local_sample"), int)
                and isinstance(node.get("gain_db"), (int, float))):
            nodes.append(node)
    nodes.sort(key=lambda n: n["local_sample"])
    return nodes


def envelope_signature(audio_edit: Any) -> list[tuple]:
    return [
        (n.get("id"), n.get("local_sample"), round(float(n.get("gain_db", 0.0)), 4),
         n.get("interpolation", "linear"))
        for n in envelope_nodes(audio_edit)
    ]


def set_envelope(audio_edit: Any, nodes: list[dict] | None) -> dict:
    """写入包络节点（None/空 → 移除键；返回新 audio_edit，不改原对象）。"""
    updated = copy_audio_edit(audio_edit) if isinstance(audio_edit, dict) else new_audio_edit()
    if nodes:
        updated["envelope"] = sorted(nodes, key=lambda n: n["local_sample"])
    else:
        updated.pop("envelope", None)
    return updated


def envelope_from_canvas_points(
    points: list[dict], duration_seconds: float, output_total_samples: int,
) -> list[dict]:
    """画布包络点（{"t": 秒, "db": dB}）→ 模型节点（片段输出样本域）。"""
    import uuid

    if not points or duration_seconds <= 0.0 or output_total_samples <= 0:
        return []
    nodes = []
    for point in points:
        t = float(point.get("t", 0.0))
        db = float(point.get("db", 0.0))
        fraction = max(0.0, min(1.0, t / duration_seconds))
        nodes.append({
            "id": point.get("id") or str(uuid.uuid4()),
            "local_sample": int(round(fraction * output_total_samples)),
            "gain_db": round(db, 3),
            "interpolation": "linear",
        })
    nodes.sort(key=lambda n: n["local_sample"])
    # 同 sample 唯一：后写覆盖前写。
    deduped: list[dict] = []
    for node in nodes:
        if deduped and deduped[-1]["local_sample"] == node["local_sample"]:
            deduped[-1] = node
        else:
            deduped.append(node)
    return deduped


def canvas_points_from_envelope(
    nodes: list[dict], duration_seconds: float, output_total_samples: int,
) -> list[dict]:
    """模型节点 → 画布点（供编辑器装载；域转换与写入方向互逆）。"""
    if not nodes or duration_seconds <= 0.0 or output_total_samples <= 0:
        return []
    return [
        {
            "t": max(0.0, min(duration_seconds,
                              node["local_sample"] / output_total_samples * duration_seconds)),
            "db": float(node.get("gain_db", 0.0)),
        }
        for node in nodes
    ]


def evaluate_envelope_gain(
    block_start_sample: int, count: int, nodes: list[dict],
) -> Optional[np.ndarray]:
    """包络节点的块求值：dB 分段线性 → 线性增益包络。

    首节点前/末节点后沿用边界增益（计划 §4.3）。实时与离线共用本函数；
    返回 None 表示无包络（恒等）。
    """
    if count <= 0 or not nodes:
        return None
    samples = np.arange(block_start_sample, block_start_sample + count,
                        dtype=np.float64)
    node_x = np.array([n["local_sample"] for n in nodes], dtype=np.float64)
    node_db = np.array([float(n.get("gain_db", 0.0)) for n in nodes],
                       dtype=np.float64)
    db = np.interp(samples, node_x, node_db)
    return (10.0 ** (db / 20.0)).astype(np.float32)


def edge_fade_samples(sample_rate: int) -> int:
    return max(1, int(round(EDGE_FADE_SECONDS * sample_rate)))


def selection_measurement_envelope(
    length: int, start: int, end: int, *, sample_rate: int,
) -> "np.ndarray | None":
    """P0-1 · 候选测量步骤的 unity 增益包络（None = 区间外不涉及）。

    测量基准必须乘以该包络再取峰值/响度：步骤边缘的 3 ms 过渡不达
    全增益，直接量基准会把过渡区样本误当峰值目标（重复标准化落点
    漂移的来源之一）。乘包络后 ``gain = target − 测量值`` 与最终渲染
    逐样本一致。
    """
    return _range_gain_envelope(
        int(length), 0, int(start), int(end), edge_fade_samples(int(sample_rate)))


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


def apply_clip_audio_edit(
    samples: np.ndarray,
    block_start_sample: int,
    audio_edit: Any,
    *,
    sample_rate: int = PROCESSING_RATE,
) -> np.ndarray:
    """对片段输出域的一个块应用处理栈（纯函数；原地不修改输入）。

    ``samples``: planar float [channels, n]，块首对应片段输出样本
    ``block_start_sample``（``sample_rate`` 消费率域）。栈内持久化坐标
    （step 区间 / envelope 节点）以 48000 参考率存储，本函数内部经
    有理数换算映射到消费率域——48/44.1/96 kHz 图同一编辑在时间轴上
    落点一致（边界偏差 ≤1 输出样本）。调用点位于 clip gain 之后、
    Clip FX 之前（播放/成片/Stem 三处共用本函数保证一致）。
    未知处理器存在时直接原样返回（调用方应先经 validate 阻断）。
    """
    if audio_edit is None or not isinstance(audio_edit, dict):
        return samples
    steps = audio_edit.get("steps") or []
    envelope = envelope_nodes(audio_edit)
    if not steps and not envelope:
        return samples
    consumption_rate = int(sample_rate)
    processing_rate = _edit_processing_rate(audio_edit)
    rate_mismatch = processing_rate != consumption_rate
    envelope = _envelope_nodes_at_rate(
        envelope, processing_rate, consumption_rate)
    fade = edge_fade_samples(sample_rate)
    out: np.ndarray | None = None
    for step in steps:
        if not isinstance(step, dict) or not step.get("enabled", True):
            continue
        processor_id = step.get("processor_id")
        if processor_id not in KNOWN_PROCESSOR_IDS:
            return samples  # 未知处理器：保持原样（上游 validate 负责阻断）
        if processor_id in PRE_RENDER_PROCESSORS:
            # 预渲染处理器在基底 PCM 中烘焙（apply_clip_audio_edit_block
            # 负责 inline/基底分派）；直接走块路径的旧调用方同样安全跳过。
            continue
        params = step.get("params", {})
        start, end = params.get("start", 0), params.get("end", 0)
        if rate_mismatch:
            # 48k 参考域区间 → 消费率域（有理换算，round 半偶）。
            start = reference_position_to_rate(int(start), consumption_rate)
            end = reference_position_to_rate(int(end), consumption_rate)
        channels = params.get("channels") or []
        gain_linear = 0.0 if processor_id == "mute" else 10.0 ** (
            float(params.get("gain_db", 0.0)) / 20.0)
        if processor_id == "normalize":
            # normalize 的增益在提交时已按实测 peak 预计算并持久化。
            gain_linear = 10.0 ** (float(params.get("gain_db", 0.0)) / 20.0)
        elif processor_id == "channel_polarity":
            # 反极性 = 对选定通道乘 −1（与 range 包络同路径，边缘同样有短过渡）。
            gain_linear = -1.0
        range_env = _range_gain_envelope(
            samples.shape[1], block_start_sample, int(start), int(end), fade)
        if range_env is None:
            continue
        if out is None:
            out = samples.copy()
        for channel in channels:
            if isinstance(channel, int) and 0 <= channel < out.shape[0]:
                out[channel] = out[channel] * (range_env * gain_linear +
                                               (1.0 - range_env))
    # P1-4：片段音量包络（与 clip gain / fade 相乘的顺序合同不变）。
    if envelope:
        gain_env = evaluate_envelope_gain(
            block_start_sample, samples.shape[1], envelope)
        if gain_env is not None:
            if out is None:
                out = samples.copy()
            out = out * gain_env
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
    """Remove a step while preserving other clip audio edit data."""
    if not isinstance(audio_edit, dict):
        return audio_edit
    remaining = [step for step in audio_edit.get("steps", [])
                 if not (isinstance(step, dict) and step.get("step_id") == step_id)]
    return copy_audio_edit({**audio_edit, "steps": remaining}) if remaining else clear_steps(audio_edit)


def clear_steps(audio_edit: Any) -> Any:
    """Clear the processor rack, keeping envelopes, markers and extra fields."""
    if not isinstance(audio_edit, dict):
        return audio_edit
    updated = {**audio_edit, "steps": []}
    return None if updated == new_audio_edit() else copy_audio_edit(updated)


def append_step(audio_edit: Any, step: dict) -> dict:
    """追加一个 step（audio_edit 为空时建立最小结构）。"""
    base = copy_audio_edit(audio_edit) if isinstance(audio_edit, dict) else new_audio_edit()
    base.setdefault("steps", []).append(step)
    return base


def db_to_linear(gain_db: float) -> float:
    return 10.0 ** (gain_db / 20.0)


def linear_to_db(linear: float) -> float:
    return 20.0 * math.log10(max(1e-12, linear))


# ----------------------------------------------------------------------
# 片段标记点（P1-5）：随片段移动，局部样本域；复制片段时重新分配 ID。
# ----------------------------------------------------------------------
def marker_list(audio_edit: Any) -> list[dict]:
    """读取标记点（按 local_sample 升序）。"""
    if not isinstance(audio_edit, dict):
        return []
    markers = []
    for marker in audio_edit.get("markers", []) or []:
        if (isinstance(marker, dict) and isinstance(marker.get("local_sample"), int)
                and marker.get("local_sample") >= 0):
            markers.append(marker)
    markers.sort(key=lambda m: m["local_sample"])
    return markers


def set_markers(audio_edit: Any, markers: list[dict] | None) -> Any:
    """写入标记点（None/空 → 移除键；无原栈时建立最小结构）。"""
    updated = copy_audio_edit(audio_edit) if isinstance(audio_edit, dict) else new_audio_edit()
    if markers:
        updated["markers"] = sorted(markers, key=lambda m: m["local_sample"])
    else:
        updated.pop("markers", None)
    return updated


def markers_from_canvas(
    points: list[dict], duration_seconds: float, output_total_samples: int,
) -> list[dict]:
    """画布标记（{"time": 秒, "label": str}）→ 模型标记（含稳定 ID）。"""
    import uuid

    if not points or duration_seconds <= 0.0 or output_total_samples <= 0:
        return []
    markers = []
    for point in points:
        t = float(point.get("time", 0.0))
        fraction = max(0.0, min(1.0, t / duration_seconds))
        markers.append({
            "id": point.get("id") or str(uuid.uuid4()),
            "local_sample": int(round(fraction * output_total_samples)),
            "name": str(point.get("label", "") or ""),
            "color": point.get("color"),
        })
    markers.sort(key=lambda m: m["local_sample"])
    return markers


def canvas_markers_from(
    markers: list[dict], duration_seconds: float, output_total_samples: int,
) -> list[dict]:
    """模型标记 → 画布标记（时间/标签）。

    P1 · 稳 ID：``id`` 随标记往返——下一次 ``markers_from_canvas`` 按
    同一 id 写回（拖动/改名不换身份，引用与自动化锚点不漂移）。
    """
    if not markers or duration_seconds <= 0.0 or output_total_samples <= 0:
        return []
    return [
        {
            "id": m.get("id"),
            "time": max(0.0, min(duration_seconds,
                                 m["local_sample"] / output_total_samples * duration_seconds)),
            "label": str(m.get("name", "") or ""),
        }
        for m in markers
    ]


def regenerate_marker_ids(audio_edit: Any) -> Any:
    """复制片段时重新分配 marker ID（内容保持同一源位置）。"""
    if not isinstance(audio_edit, dict) or not audio_edit.get("markers"):
        return audio_edit
    import uuid

    updated = copy_audio_edit(audio_edit)
    updated["markers"] = [
        {**marker, "id": str(uuid.uuid4())} for marker in updated["markers"]
    ]
    return updated
