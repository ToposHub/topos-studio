"""AudioEffect 适配层（NF/VC/SM → audio_dsp.EffectChain 工厂）。

audio_dsp 顶层不 import 本包（注册在 ``_ensure_effect_registry`` 惰性
进行），本模块顶层 import 已完成初始化的 audio_dsp——无环。
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from src.features.edit.core.audio_dsp import AudioEffect


class _CompositeAdapterBase(AudioEffect):
    """复合 FX 适配基类：委托给 audio_effects 实现，保留 AudioEffect 契约。"""

    _impl_factory = None  # 子类注入

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._impl = self._impl_factory(float(sample_rate))

    def set_params(self, params: object, sample_rate: Optional[float] = None,
                   algorithm_version: Optional[int] = None) -> None:
        if sample_rate is not None:
            self._sample_rate = float(sample_rate)
        self._impl.set_params(params, sample_rate=sample_rate,
                              algorithm_version=algorithm_version)

    def process(self, samples: np.ndarray) -> np.ndarray:
        return self._impl.process(samples)

    def reset_state(self) -> None:
        self._impl.reset_state()

    def tail_estimate_samples(self) -> int:
        return int(self._impl.tail_estimate_samples())

    @property
    def is_active(self) -> bool:
        return bool(self._impl.is_active)

    def latency_samples(self) -> int:
        return int(getattr(self._impl, "latency_samples", lambda: 0)())

    def take_freeze_capture(self) -> dict:
        """H09：透传捕获拉取（宿主落盘）。"""
        return self._impl.take_freeze_capture()

    def install_freeze_capture(self, capture: dict) -> bool:
        """H09：透传捕获装载（确定性重放）。"""
        return bool(self._impl.install_freeze_capture(capture))

    def collect_telemetry(self) -> dict:
        return dict(self._impl.collect_telemetry())


class SpectralManglerAdapter(_CompositeAdapterBase):
    effect_type = "spectral_mangler"

    def _impl_factory(self, sr):  # noqa: N802（基类约定）
        from src.features.edit.core.audio_effects.spectral_mangler import (
            SpectralManglerEffect,
        )
        return SpectralManglerEffect(sr)


class DeEsserAdapter(_CompositeAdapterBase):
    """P1 · De-Esser（对白片段/ADR/对白 Bus 的齿音抑制）。"""

    effect_type = "de_esser"

    def _impl_factory(self, sr):  # noqa: N802（基类约定）
        from src.features.edit.core.audio_effects.de_esser import (
            DeEsserEffect,
        )
        return DeEsserEffect(sr)


class VoiceChangerAdapter(_CompositeAdapterBase):
    effect_type = "voice_changer"

    def _impl_factory(self, sr):  # noqa: N802
        from src.features.edit.core.audio_effects.voice_changer import (
            VoiceChangerEffect,
        )
        return VoiceChangerEffect(sr)
