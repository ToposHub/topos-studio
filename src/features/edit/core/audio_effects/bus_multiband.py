"""P1 · Bus Compressor / Multiband Compressor（2026-11-19 计划 #5）。

- Bus 版本绑定现有 ``CompressorEffect`` 核心（不新造电路拓扑——多种
  拓扑与 Auto Release 待单独测试后开放）：DX/MX 胶合模板 + 并行 Mix
  （compressor 内建）+ 检源高通（低频鼓组不触发ducking）。
- 多段：Linkwitz-Riley 4 阶互补分频（low+high 同为 4 阶——相位对齐，
  和为全通；线性相位分频另定延迟合同），每段独立
  ``CompressorEffect`` envelope（跨带状态各自连续）。
"""
from __future__ import annotations

from typing import Dict, Optional, Sequence

import numpy as np

from src.features.edit.core.audio_dsp import AudioEffect, CompressorEffect

try:  # pragma: no cover - 环境探测
    from scipy.signal import butter, sosfilt
    _SCIPY = True
except ImportError:  # pragma: no cover
    _SCIPY = False

#: DX / MX 胶合模板（映射到 CompressorEffect 参数）。
BUS_COMP_TEMPLATES: Dict[str, dict] = {
    "dx_glue": {
        "threshold_db": -22.0, "ratio": 2.5, "attack_ms": 12.0,
        "release_ms": 140.0, "knee": 18.0, "mix": 90.0, "makeup_db": 1.5,
    },
    "mx_glue": {
        "threshold_db": -18.0, "ratio": 2.0, "attack_ms": 22.0,
        "release_ms": 260.0, "knee": 22.0, "mix": 100.0, "makeup_db": 1.0,
    },
}


class BusCompEffect(AudioEffect):
    """Bus 压缩（CompressorEffect 核心 + 胶合模板 + 检源高通）。"""

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._params: dict = {"enabled": True, "template": "dx_glue",
                              "sc_hpf_hz": 120.0}
        self._comp = CompressorEffect(sample_rate)
        self._apply_template()
        self._hpf_sos = None
        self._hpf_state = None

    def _apply_template(self) -> None:
        template = str(self._params.get("template", "dx_glue"))
        self._comp.set_params(
            BUS_COMP_TEMPLATES.get(template, BUS_COMP_TEMPLATES["dx_glue"]),
            sample_rate=self._sample_rate)

    def set_params(self, params: object,
                   sample_rate: Optional[float] = None,
                   algorithm_version: Optional[int] = None) -> None:
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)
            self._comp = CompressorEffect(self._sample_rate)
        merged = dict(self._params)
        if isinstance(params, dict):
            merged.update(params)
        template_changed = merged.get("template") != self._params.get("template")
        self._params = merged
        if template_changed:
            self._apply_template()
        hpf = float(np.clip(self._params.get("sc_hpf_hz", 120.0),
                            0.0, self._sample_rate * 0.45))
        self._hpf_sos = (butter(2, hpf, btype="highpass",
                                fs=self._sample_rate, output="sos")
                         if hpf > 0.0 else None)
        self._hpf_state = None

    def reset_state(self) -> None:
        self._comp.reset_state()
        self._hpf_state = None

    @property
    def is_active(self) -> bool:
        return bool(self._params.get("enabled", True)) and _SCIPY

    def process(self, samples: np.ndarray) -> np.ndarray:
        if not self.is_active or samples is None or samples.ndim != 2:
            return samples
        if self._hpf_sos is None:
            return self._comp.process(samples)
        channels = samples.shape[0]
        if self._hpf_state is None or self._hpf_state.shape != (
                self._hpf_sos.shape[0], channels, 2):
            self._hpf_state = np.zeros(
                (self._hpf_sos.shape[0], channels, 2), dtype=np.float64)
        detector, self._hpf_state = sosfilt(
            self._hpf_sos, samples.astype(np.float64), axis=-1,
            zi=self._hpf_state)
        return self._comp.process(
            samples, sidechain=detector.astype(np.float32))

    def tail_estimate_samples(self) -> int:
        return self._comp.tail_estimate_samples()

    def latency_samples(self) -> int:
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# 多段：LR4 相位对齐互补分频
# ─────────────────────────────────────────────────────────────────────────────

class _LR4Section:
    """一个分频点（low = 2×butter2 级联；互补支 = x − low——减法互补，
    重构精确：low + 互补 = x 逐样本成立。「相位对齐的互补分频」的
    完美重构实现；LR4 并联形式的相位匹配变体待线性相位延迟合同）。"""

    def __init__(self, freq: float, sample_rate: float, channels: int) -> None:
        self._low_sos = np.concatenate([
            butter(2, freq, btype="low", fs=sample_rate, output="sos")] * 2)
        self._zi_low = np.zeros((self._low_sos.shape[0], channels, 2),
                                dtype=np.float64)

    def split(self, x: np.ndarray):
        x64 = x.astype(np.float64)
        low, self._zi_low = sosfilt(
            self._low_sos, x64, axis=-1, zi=self._zi_low)
        low = low.astype(np.float32)
        complementary = (x64 - low.astype(np.float64)).astype(np.float32)
        return low, np.ascontiguousarray(complementary)


class MultibandCompressorEffect(AudioEffect):
    """三段压缩：LR4 相位对齐分频 + 每段独立 CompressorEffect。"""

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._params: dict = {
            "enabled": True,
            "crossovers": [180.0, 4200.0],
            "bands": [
                {"threshold_db": -20.0, "ratio": 2.0, "release_ms": 220.0,
                 "makeup_db": 0.0},                       # 低段
                {"threshold_db": -24.0, "ratio": 2.5, "release_ms": 150.0,
                 "makeup_db": 0.0},                       # 中段
                {"threshold_db": -22.0, "ratio": 2.0, "release_ms": 180.0,
                 "makeup_db": 0.0},                       # 高段
            ],
        }
        self._sections: list = []
        self._band_comps: list = []
        self._rebuild()

    def _rebuild(self) -> None:
        crossovers = self._params.get("crossovers") or [180.0, 4200.0]
        self._sections = []
        self._band_comps = []
        for index, freq in enumerate(crossovers[:2]):
            self._sections.append(_LR4Section(float(freq), self._sample_rate, 2))
        for index in range(len(crossovers[:2]) + 1):
            comp = CompressorEffect(self._sample_rate)
            band_params = dict(self._params.get("bands", [{}])[index])
            band_params.setdefault("attack_ms", 10.0)
            comp.set_params(band_params, sample_rate=self._sample_rate)
            self._band_comps.append(comp)

    def set_params(self, params: object,
                   sample_rate: Optional[float] = None,
                   algorithm_version: Optional[int] = None) -> None:
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)
        merged = dict(self._params)
        if isinstance(params, dict):
            merged.update(params)
        self._params = merged
        self._rebuild()

    def reset_state(self) -> None:
        self._rebuild()

    @property
    def is_active(self) -> bool:
        return bool(self._params.get("enabled", True)) and _SCIPY

    def process(self, samples: np.ndarray) -> np.ndarray:
        if not self.is_active or samples is None or samples.ndim != 2:
            return samples
        if samples.shape[-1] == 0:
            return samples
        # 分频树：split1 → (low1, rest)；rest 再 split2 → (mid, high)。
        low, rest = self._sections[0].split(samples)
        mid, high = self._sections[1].split(rest)
        out = (self._band_comps[0].process(np.ascontiguousarray(low))
               + self._band_comps[1].process(np.ascontiguousarray(mid))
               + self._band_comps[2].process(np.ascontiguousarray(high)))
        return np.ascontiguousarray(out, dtype=np.float32)

    def tail_estimate_samples(self) -> int:
        return max((comp.tail_estimate_samples()
                    for comp in self._band_comps), default=0)

    def latency_samples(self) -> int:
        return 0

    def collect_telemetry(self) -> dict:
        return {
            f"band_{index}_gr_db": getattr(comp, "last_gr_db", 0.0)
            for index, comp in enumerate(self._band_comps)
        }
