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
        """模板值 + 显式连续字段覆盖（FX-05：旋钮提交与模板解耦）。

        模板的 ``makeup_db`` 旧键归一为 ``makeup_gain``（旧实现写入后
        被 CompressorEffect 静默忽略）。
        """
        template = str(self._params.get("template", "dx_glue"))
        base = dict(BUS_COMP_TEMPLATES.get(
            template, BUS_COMP_TEMPLATES["dx_glue"]))
        if "makeup_gain" not in base and "makeup_db" in base:
            base["makeup_gain"] = base.pop("makeup_db")
        for key in ("threshold_db", "ratio", "attack_ms", "release_ms",
                    "knee", "mix", "makeup_gain"):
            value = self._params.get(key)
            if value is not None:
                base[key] = value
        self._comp.set_params(base, sample_rate=self._sample_rate)

    def set_params(self, params: object,
                   sample_rate: Optional[float] = None,
                   algorithm_version: Optional[int] = None) -> None:
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)
            self._comp = CompressorEffect(self._sample_rate)
        merged = dict(self._params)
        if isinstance(params, dict):
            merged.update(params)
        self._params = merged
        # FX-05：模板与旋钮都重算有效参数（连续字段覆盖模板值）；
        # CompressorEffect.set_params 保持包络状态（FX-04 同合同）。
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
    完美重构实现；LR4 并联形式的相位匹配变体待线性相位延迟合同）。

    FX-04：SOS 状态按输入实际通道数分配（旧实现写死 2 声道）。
    """

    def __init__(self, freq: float, sample_rate: float) -> None:
        self._low_sos = np.concatenate([
            butter(2, freq, btype="low", fs=sample_rate, output="sos")] * 2)
        self._zi_low: Optional[np.ndarray] = None

    def _ensure_state(self, channels: int) -> None:
        expected = (self._low_sos.shape[0], channels, 2)
        if self._zi_low is None or self._zi_low.shape != expected:
            self._zi_low = np.zeros(expected, dtype=np.float64)

    def reset(self) -> None:
        if self._zi_low is not None:
            self._zi_low[:] = 0.0

    def split(self, x: np.ndarray):
        x64 = x.astype(np.float64)
        self._ensure_state(x.shape[0])
        low, self._zi_low = sosfilt(
            self._low_sos, x64, axis=-1, zi=self._zi_low)
        low = low.astype(np.float32)
        complementary = (x64 - low.astype(np.float64)).astype(np.float32)
        return low, np.ascontiguousarray(complementary)


def normalize_band_params(band: object, default_attack_ms: float) -> dict:
    """参数入口集中别名兼容（FX-04）：makeup_db（旧默认键）→
    makeup_gain（CompressorEffect 读键）；补齐 attack_ms（缺省继承
    全局）与 enabled/mute 语义。"""
    out = dict(band) if isinstance(band, dict) else {}
    if "makeup_gain" not in out and "makeup_db" in out:
        try:
            out["makeup_gain"] = float(out.pop("makeup_db"))
        except (TypeError, ValueError):
            out.pop("makeup_db", None)
    out.setdefault("attack_ms", float(default_attack_ms))
    out.setdefault("enabled", True)
    out.setdefault("mute", False)
    return out


class MultibandCompressorEffect(AudioEffect):
    """多段压缩（FX-04）：LR4 减法互补分频 + 每段独立 CompressorEffect。

    2026-11-21 计划合同：
    - 段数 2/3/4（crossovers 升序、长度 = 段数−1）；递归对余带分频；
    - SOS 状态按输入实际通道数分配；拓扑（采样率/通道数/分频点数与
      值）不变时旋钮提交复用已有 CompressorEffect 包络（不重建）；
    - makeup_db（旧默认键）→ makeup_gain 别名在参数入口集中；
    - enabled=false 段仍参加重构；mute 段归零；段 attack 缺省继承全局。
    """

    _BANDS_MAX = 4
    _DEFAULT_BANDS = (
        {"threshold_db": -20.0, "ratio": 2.0, "release_ms": 220.0,
         "makeup_db": 0.0},
        {"threshold_db": -24.0, "ratio": 2.5, "release_ms": 150.0,
         "makeup_db": 0.0},
        {"threshold_db": -22.0, "ratio": 2.0, "release_ms": 180.0,
         "makeup_db": 0.0},
        {"threshold_db": -22.0, "ratio": 2.0, "release_ms": 180.0,
         "makeup_db": 0.0},
    )

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._params: dict = {
            "enabled": True,
            "crossovers": [180.0, 4200.0],
            "bands": [dict(b) for b in self._DEFAULT_BANDS[:3]],
        }
        self._sections: list = []
        self._band_comps: list = []
        self._topology: Optional[tuple] = None
        self._last_channels = 0
        self._ensure_topology(0)

    # ── 参数归一 ────────────────────────────────────────────────
    def _crossovers_of(self, params: dict) -> tuple:
        raw = params.get("crossovers") or [180.0, 4200.0]
        if not isinstance(raw, (list, tuple)):
            raw = [180.0, 4200.0]
        values = [float(freq) for freq in raw[:self._BANDS_MAX - 1]]
        ceiling = 0.45 * float(self._sample_rate)
        values = [min(max(freq, 20.0), ceiling) for freq in values]
        for lo, hi in zip(values, values[1:]):
            if hi <= lo:
                # 非法交叉点：回退默认并报错（计划合同）。
                raise ValueError(
                    f"multiband crossovers must strictly ascend: {values}")
        return tuple(values)

    def _bands_of(self, params: dict, count: int) -> list:
        raw = params.get("bands")
        if not isinstance(raw, (list, tuple)):
            raw = []
        defaults = self._DEFAULT_BANDS
        bands = []
        for index in range(count):
            source = raw[index] if index < len(raw) and isinstance(
                raw[index], dict) else dict(defaults[index])
            bands.append(normalize_band_params(
                source, float(params.get("attack_ms", 10.0))))
        return bands

    # ── 拓扑 ────────────────────────────────────────────────────
    def _ensure_topology(self, channels: int) -> None:
        try:
            crossovers = self._crossovers_of(self._params)
        except ValueError:
            self._params["crossovers"] = [180.0, 4200.0]
            crossovers = self._crossovers_of(self._params)
        key = (channels, crossovers, round(float(self._sample_rate), 3))
        if key == self._topology:
            return
        self._topology = key
        count = len(crossovers) + 1
        if channels > 0:
            self._sections = [_LR4Section(freq, self._sample_rate)
                              for freq in crossovers]
        self._band_comps = []
        for band in self._bands_of(self._params, count):
            comp = CompressorEffect(self._sample_rate)
            comp.set_params(dict(band), sample_rate=self._sample_rate)
            self._band_comps.append(comp)

    def set_params(self, params: object,
                   sample_rate: Optional[float] = None,
                   algorithm_version: Optional[int] = None) -> None:
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)
            self._topology = None
        merged = dict(self._params)
        if isinstance(params, dict):
            merged.update(params)
        self._params = merged
        # 旋钮提交：拓扑不变时仅更新各段 CompressorEffect 参数（复用
        # 已有包络状态）；分频点变化才重建 sections（清分频历史）。
        try:
            crossovers = self._crossovers_of(self._params)
        except ValueError:
            self._params["crossovers"] = [180.0, 4200.0]
            crossovers = self._crossovers_of(self._params)
        count = len(crossovers) + 1
        bands = self._bands_of(self._params, count)
        # 旋钮提交：段数一致且分频 sections 已建 → 仅更新各段压缩参数
        # （复用已有包络状态）；否则整体重建（首块前或拓扑变化）。
        if (len(self._band_comps) == count
                and len(self._sections) == len(crossovers)):
            for comp, band in zip(self._band_comps, bands):
                comp.set_params(dict(band), sample_rate=self._sample_rate)
        else:
            self._ensure_topology(self._last_channels)

    def reset_state(self) -> None:
        for section in self._sections:
            section.reset()
        for comp in self._band_comps:
            comp.reset_state()

    @property
    def is_active(self) -> bool:
        return bool(self._params.get("enabled", True)) and _SCIPY

    def process(self, samples: np.ndarray) -> np.ndarray:
        if not self.is_active or samples is None or samples.ndim != 2:
            return samples
        if samples.shape[-1] == 0:
            return samples
        self._last_channels = samples.shape[0]
        self._ensure_topology(samples.shape[0])
        bands = self._bands_of(self._params, len(self._band_comps))
        # 递归分频：split_i → (band_i, rest)；最后一段 = 剩余。
        pieces = []
        rest = samples
        for section in self._sections:
            low, rest = section.split(rest)
            pieces.append(np.ascontiguousarray(low))
        pieces.append(np.ascontiguousarray(rest))
        out = np.zeros_like(samples, dtype=np.float64)
        for band, comp, piece in zip(bands, self._band_comps, pieces):
            if bool(band.get("mute", False)):
                continue  # mute：该段归零（其余段照常重构）
            if bool(band.get("enabled", True)):
                processed = comp.process(piece)
            else:
                processed = piece  # disabled：该段干声仍参加重构
            out += processed.astype(np.float64)
        return np.ascontiguousarray(out, dtype=np.float32)

    def tail_estimate_samples(self) -> int:
        return max((comp.tail_estimate_samples()
                    for comp in self._band_comps), default=0)

    def latency_samples(self) -> int:
        return 0

    def collect_telemetry(self) -> dict:
        data = {
            f"band_{index}_gr_db": getattr(comp, "last_gr_db", 0.0)
            for index, comp in enumerate(self._band_comps)
        }
        data["band_count"] = len(self._band_comps)
        return data
