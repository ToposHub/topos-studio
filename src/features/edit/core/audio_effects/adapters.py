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
        self._uses_legacy = False
        self._async_resources_enabled = False

    def set_params(self, params: object, sample_rate: Optional[float] = None,
                   algorithm_version: Optional[int] = None) -> None:
        if sample_rate is not None:
            self._sample_rate = float(sample_rate)
        legacy_factory = getattr(self, "_legacy_impl_factory", None)
        use_legacy = (callable(legacy_factory) and algorithm_version is not None
                      and int(algorithm_version) <= 4)
        if use_legacy != self._uses_legacy:
            self.release_resources()
            factory = legacy_factory if use_legacy else self._impl_factory
            self._impl = factory(self._sample_rate)
            self._uses_legacy = use_legacy
            if self._async_resources_enabled:
                self.enable_async_resources()
        self._impl.set_params(params, sample_rate=sample_rate,
                              algorithm_version=algorithm_version)

    def enable_async_resources(self) -> None:
        """Allow resource-owning implementations to prepare outside callbacks."""
        self._async_resources_enabled = True
        enable = getattr(self._impl, "enable_async_resources", None)
        if callable(enable):
            enable()

    def release_resources(self) -> None:
        """Cancel resource jobs when a chain discards this instance."""
        release = getattr(self._impl, "release_resources", None)
        if callable(release):
            release()

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
        # Analysis is optional for DSP implementations. Stable instance IDs
        # enable chain taps for every adapter, including processors without an
        # analysis publisher (e.g. convolution and modulation effects).
        collect = getattr(self._impl, "collect_telemetry", None)
        return dict(collect()) if callable(collect) else {}


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


class TruePeakLimiterAdapter(_CompositeAdapterBase):
    """P1 · True Peak Limiter（Master/交付；旧 Limiter 语义不变）。"""

    effect_type = "true_peak_limiter"

    def _impl_factory(self, sr):  # noqa: N802（基类约定）
        from src.features.edit.core.audio_effects.true_peak_limiter import (
            TruePeakLimiterEffect,
        )
        return TruePeakLimiterEffect(sr)

    def _legacy_impl_factory(self, sr: float) -> AudioEffect:
        from src.features.edit.core.audio_effects.legacy_v4.true_peak_limiter import (
            TruePeakLimiterEffect,
        )
        return TruePeakLimiterEffect(sr)


class DynamicEqAdapter(_CompositeAdapterBase):
    """P1 · Dynamic EQ（对白/音乐避让、轨道与 Bus）。"""

    effect_type = "dynamic_eq"

    @property
    def sc_source(self) -> str:
        return self._impl.sc_source

    def process(self, samples: np.ndarray,
                sidechain: Optional[np.ndarray] = None) -> np.ndarray:
        return self._impl.process(samples, sidechain=sidechain)

    def _impl_factory(self, sr):  # noqa: N802（基类约定）
        from src.features.edit.core.audio_effects.dynamic_eq import (
            DynamicEqEffect,
        )
        return DynamicEqEffect(sr)


class BusCompAdapter(_CompositeAdapterBase):
    """P1 · Bus 压缩（CompressorEffect 核心 + 胶合模板 + SC 高通）。"""

    effect_type = "bus_comp"

    def _impl_factory(self, sr):  # noqa: N802（基类约定）
        from src.features.edit.core.audio_effects.bus_multiband import (
            BusCompEffect,
        )
        return BusCompEffect(sr)


class MultibandCompAdapter(_CompositeAdapterBase):
    """P1 · 多段压缩（LR4 相位对齐分频 + 每段独立 envelope）。"""

    effect_type = "multiband_comp"

    def _impl_factory(self, sr):  # noqa: N802（基类约定）
        from src.features.edit.core.audio_effects.bus_multiband import (
            MultibandCompressorEffect,
        )
        return MultibandCompressorEffect(sr)


class ConvolutionReverbAdapter(_CompositeAdapterBase):
    """P1 · 卷积混响（Room Match / ADR / 环境 / FX Bus）。"""

    effect_type = "convolution_reverb"

    def _impl_factory(self, sr):  # noqa: N802（基类约定）
        from src.features.edit.core.audio_effects.convolution_reverb import (
            ConvolutionReverbEffect,
        )
        return ConvolutionReverbEffect(sr)


class ModulationAdapterBase(_CompositeAdapterBase):
    """调制类共用适配（#7 批次）。"""

    effect_type = ""
    _factory = None

    def _impl_factory(self, sr):  # noqa: N802（基类约定）
        return self._factory(sr)


class TransientDesignerAdapter(_CompositeAdapterBase):
    effect_type = "transient_designer"

    @property
    def sc_source(self) -> str:
        return self._impl.sc_source

    def process(self, samples: np.ndarray,
                sidechain: Optional[np.ndarray] = None) -> np.ndarray:
        return self._impl.process(samples, sidechain=sidechain)

    def _impl_factory(self, sr):  # noqa: N802（基类约定）
        from src.features.edit.core.audio_effects.modulation_tools import (
            TransientDesignerEffect,
        )
        return TransientDesignerEffect(sr)


class TremoloAutoPanAdapter(_CompositeAdapterBase):
    effect_type = "tremolo_auto_pan"

    def _impl_factory(self, sr):  # noqa: N802（基类约定）
        from src.features.edit.core.audio_effects.modulation_tools import (
            TremoloAutoPanEffect,
        )
        return TremoloAutoPanEffect(sr)


class ChorusFlangerAdapter(_CompositeAdapterBase):
    effect_type = "chorus_flanger"

    def _impl_factory(self, sr):  # noqa: N802（基类约定）
        from src.features.edit.core.audio_effects.modulation_tools import (
            ChorusFlangerEffect,
        )
        return ChorusFlangerEffect(sr)

    def _legacy_impl_factory(self, sr: float) -> AudioEffect:
        from src.features.edit.core.audio_effects.legacy_v4.modulation_tools import (
            ChorusFlangerEffect,
        )
        return ChorusFlangerEffect(sr)


class PhaserAdapter(_CompositeAdapterBase):
    effect_type = "phaser"

    def _impl_factory(self, sr):  # noqa: N802（基类约定）
        from src.features.edit.core.audio_effects.modulation_tools import (
            PhaserEffect,
        )
        return PhaserEffect(sr)

    def _legacy_impl_factory(self, sr: float) -> AudioEffect:
        from src.features.edit.core.audio_effects.legacy_v4.modulation_tools import (
            PhaserEffect,
        )
        return PhaserEffect(sr)


class VoiceChangerAdapter(_CompositeAdapterBase):
    effect_type = "voice_changer"

    def _impl_factory(self, sr):  # noqa: N802
        from src.features.edit.core.audio_effects.voice_changer import (
            VoiceChangerEffect,
        )
        return VoiceChangerEffect(sr)
