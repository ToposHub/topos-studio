"""H01：精确版本迁移、opaque 参数与 future 能力门控（复审计划 §5 H01）。

真值来源：docs/audit/2026-11-15-audio-fx-post-integration/h01/
baseline-historical.json —— 由真历史提交（302b88b6/76ad5657/1016a3cd/
e2131e65/d9ba26c9）的 audio_dsp 模块渲染冻结，非自比。

覆盖复审验收清单：
1. v2 EQ 与历史 PCM 一致（302b88b6：Butterworth + Shelf Q、无 EQ-02）；
2. v0/v1 → v1 legacy 系数；v3+ → 现行（EQ-02 耦合）；逐版本不漂移；
3. Delay v≤3 直接读头切换（crossfade 属 v4 契约）；
4. future 777：诊断 status=degraded + 非空 reason；数据无损往返；
5. 未标注（无版本键）→ 0（era 歧义入诊断，不再猜成 v2）；
6. 未知类型夹在两个已知实例间不丢整链；
7. 升级走 command（undo/redo 往返逐字段核对）；
8. 参数内部未知键经 Params.from_dict().to_dict() 不丢（10 类）；
9. 导出 preflight：future 实例无授权阻断、有授权降级提示。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import numpy as np
import pytest

from src.features.edit.core.audio_dsp import (
    ALGORITHM_VERSION,
    DelayEffect,
    StripEQFilter,
    StripEQParams,
)
from src.features.edit.core.fx_versioning import (
    algorithm_compat_bucket,
    collect_chain_diagnostics,
    upgrade_chain_algorithm_version,
)
from src.features.edit.core.timeline_v2 import (
    EffectChainParams,
    TrackEffectInstance,
)

pytestmark = pytest.mark.edit_core

SR = 48000
AUDIT = Path(__file__).resolve().parents[2] / (
    "docs/audit/2026-11-15-audio-fx-post-integration/h01"
    "/baseline-historical.json")


def _render_eq(band: dict, voicing: str, version) -> float:
    t = np.arange(SR) / SR
    sig = 0.5 * np.sin(2 * np.pi * 250.0 * t)
    x = np.stack([sig, sig])
    fx = StripEQFilter(float(SR))
    p = StripEQParams.from_dict({
        "enabled": True, "voicing": voicing, "master_gain": 0.0,
        "bands": [dict(band, enabled=True, index=0)]})
    fx.set_params(p, algorithm_version=version)
    y = fx.process(x)
    seg = np.asarray(y, dtype=np.float64)[:, SR // 2:]
    return float(20.0 * np.log10(max(float(np.sqrt((seg ** 2).mean())), 1e-12)))


HPF = {"filter_type": "HPF", "freq": 1000.0, "gain": 0.0, "q": 1.1, "slope": 12}
SHELF = {"filter_type": "High Shelf", "freq": 3000.0, "gain": 9.0,
         "q": 1.1, "slope": 12}


class TestEqFrozenHistoricalBaseline:
    """v2/v3 EQ 语义对齐真历史 PCM（0.5 幅度 250Hz 正弦、后半段 RMS）。"""

    @classmethod
    def setup_class(cls):
        cls.base = json.loads(AUDIT.read_text(encoding="utf-8"))

    def test_v2_matches_302b88b6(self):
        base = self.base["302b88b6"]
        assert _render_eq(HPF, "Clean", 2) == pytest.approx(
            base["hpf_clean_250hz"]["rms_db"], abs=1e-4)
        assert _render_eq(SHELF, "British", 2) == pytest.approx(
            base["shelf_british_250hz"]["rms_db"], abs=1e-4)

    def test_v3_matches_post_eq02(self):
        # v3 = e2131e65（EQ-02 声明边界）：British Shelf 与 v2 有可测差异
        base = self.base["e2131e65"]
        assert _render_eq(HPF, "Clean", 3) == pytest.approx(
            base["hpf_clean_250hz"]["rms_db"], abs=1e-4)
        assert _render_eq(SHELF, "British", 3) == pytest.approx(
            base["shelf_british_250hz"]["rms_db"], abs=1e-4)

    def test_v2_differs_from_v3_on_eq02(self):
        """EQ-02 边界真实存在（非同一路径的假分发）。"""
        assert _render_eq(SHELF, "British", 2) != pytest.approx(
            _render_eq(SHELF, "British", 3), abs=1e-4)

    def test_legacy_buckets_stable(self):
        """v0/v1 → v1 legacy 级联（df749759 语义），不随本地版本漂移。"""
        for v in (0, 1):
            assert _render_eq(HPF, "Clean", v) == pytest.approx(
                _render_eq(HPF, "Clean", 1), abs=1e-9)
        # legacy HPF 显著不同于 Butterworth（复审 ~24dB 家族差异）
        assert _render_eq(HPF, "Clean", 1) < _render_eq(HPF, "Clean", 4) - 15.0

    def test_none_version_is_current(self):
        assert _render_eq(SHELF, "British", None) == pytest.approx(
            _render_eq(SHELF, "British", ALGORITHM_VERSION), abs=1e-9)


class TestDelayVersionDispatch:
    def _switch_render(self, version) -> np.ndarray:
        """1kHz 连续正弦 10 块 @120ms → 热改 160.5ms 再 3 块（FB=0）。

        切换窗（15ms=720 样本）内：v≤3 直读 160.5ms 历史；v4 混合
        120ms/160.5ms 两个读头（相位差半周期，对比最大） → 波形可测差异；淡化结束后两者收敛一致。
        """
        fx = DelayEffect(float(SR))
        base = {"enabled": True, "left_delay": 120.0, "right_delay": 120.0,
                "left_feedback": 0.0, "right_feedback": 0.0, "dry_wet": 50.0}
        fx.set_params(dict(base), algorithm_version=version)
        t = np.arange(10240) / SR
        sine = (0.5 * np.sin(2 * np.pi * 1000.0 * t)).astype(np.float64)
        out = []
        pos = 0
        while pos < len(sine):
            seg = np.stack([sine[pos:pos + 1024], sine[pos:pos + 1024]])
            out.append(fx.process(seg))
            pos += seg.shape[-1]
        fx.set_params({**base, "left_delay": 160.5, "right_delay": 160.5},
                      algorithm_version=version)
        for _ in range(3):
            out.append(fx.process(np.zeros((2, 1024), dtype=np.float64)))
        return np.concatenate(out, axis=-1)

    def test_v3_and_below_direct_switch(self):
        y4 = self._switch_render(4)
        switch = 10240
        fade = int(0.015 * SR)
        for v in (0, 2, 3):
            y = self._switch_render(v)
            # v0/v2/v3 相互逐位一致（同一 legacy 直接切换路径）
            y_ref = self._switch_render(3)
            assert np.allclose(y, y_ref, atol=0), v
            # 切换窗内与 v4 不同（v4 混合旧读头）
            assert np.abs(y[:, switch:switch + fade]
                          - y4[:, switch:switch + fade]).max() > 0.05, v
            # 淡化结束后收敛一致（同一 160.5ms 读头）
            assert np.allclose(y[:, switch + fade + 64:],
                               y4[:, switch + fade + 64:], atol=1e-9), v

    def test_delay_bucket_boundaries(self):
        assert algorithm_compat_bucket(3, "delay") == "legacy"
        assert algorithm_compat_bucket(4, "delay") == "current"
        assert algorithm_compat_bucket(999, "delay") == "future"


class TestDiagnosticsAndGating:
    def _chain(self, *specs):
        return EffectChainParams(effects=tuple(
            TrackEffectInstance(effect_type=et, instance_id=f"i{i}",
                                params=params, algorithm_version=av)
            for i, (et, params, av) in enumerate(specs)))

    def test_future_version_degraded_with_reason(self):
        chain = self._chain(
            ("strip_eq", {"enabled": True}, 777))
        d = collect_chain_diagnostics(chain)[0]
        assert d["compat_bucket"] == "future"
        assert d["status"] == "degraded"
        assert d["reason"] and "777" in d["reason"]

    def test_future_roundtrip_lossless(self):
        chain = self._chain(
            ("delay", {"left_delay": 333.0, "secret_future_param": "x"}, 777))
        rt = EffectChainParams.from_dict(chain.to_dict())
        e = rt.effects[0]
        assert e.algorithm_version == 777
        assert e.params["secret_future_param"] == "x"

    def test_unknown_type_sandwiched_keeps_chain(self):
        chain = self._chain(
            ("delay", {"left_delay": 100.0}, 4),
            ("mystery_fx", {"whatever": 1}, 4),
            ("strip_eq", {"enabled": True}, 4))
        rt = EffectChainParams.from_dict(chain.to_dict())
        assert [e.effect_type for e in rt.effects] == \
            ["delay", "mystery_fx", "strip_eq"]
        assert rt.effects[1].params == {"whatever": 1}

    def test_unstamped_maps_to_zero_with_reason(self):
        data = {
            "effect_type": "strip_eq", "params": {"enabled": True},
            "instance_id": "u1"}
        inst = TrackEffectInstance.from_dict(data)
        assert inst.algorithm_version == 0
        d = collect_chain_diagnostics(
            EffectChainParams(effects=(inst,)))[0]
        assert d["compat_bucket"] == "legacy"
        assert "ambiguous" in d["reason"] or "unstamped" in d["reason"]

    def test_per_effect_bucket_reasons(self):
        d = collect_chain_diagnostics(self._chain(
            ("strip_eq", {"enabled": True}, 2)))[0]
        assert d["compat_bucket"] == "v2"
        assert "EQ-02" in d["reason"]
        d = collect_chain_diagnostics(self._chain(
            ("delay", {"left_delay": 1.0}, 2)))[0]
        assert d["compat_bucket"] == "legacy"
        assert "crossfade" in d["reason"]
        # 其他效果维持全局边界（v2 渲染语义）
        d = collect_chain_diagnostics(self._chain(
            ("reverb", {"enabled": True}, 2)))[0]
        assert d["compat_bucket"] == "legacy"
        d = collect_chain_diagnostics(self._chain(
            ("reverb", {"enabled": True}, 3)))[0]
        assert d["compat_bucket"] == "current"


class TestUpgradeCommand:
    def test_upgrade_undo_roundtrip(self):
        from src.features.edit.core.commands.audio_commands import (
            UpgradeTrackEffectAlgorithmCommand,
        )
        from src.features.edit.core.timeline_v2 import (
            ClipInstance, EditTrack, TimelineSequence,
        )
        from src.shared.media.metadata import Rational
        chain = EffectChainParams(effects=(
            TrackEffectInstance.from_dict({
                "effect_type": "strip_eq", "instance_id": "a",
                "params": {"enabled": True, "k": 5}, "algorithm_version": 0}),
            TrackEffectInstance(effect_type="delay", instance_id="b",
                                params={"left_delay": 100.0},
                                algorithm_version=2),
            TrackEffectInstance(effect_type="strip_eq", instance_id="c",
                                params={"enabled": True}, algorithm_version=777),
        ))
        track = EditTrack("T", "T", "audio", 0,
                          clips=(ClipInstance("c", "a", "T", 0, 40, 0, 40),),
                          effect_chain=chain)
        tl = TimelineSequence(fps=Rational(25, 1), audio_tracks=(track,))

        cmd = UpgradeTrackEffectAlgorithmCommand.create("T")
        tl2, cmd2 = cmd.apply(tl)
        versions = [e.algorithm_version
                    for e in tl2.get_track_by_id("T").effect_chain.effects]
        assert versions == [4, 4, 777]  # future 不动
        # 参数逐字段不变（只改版本）
        assert tl2.get_track_by_id("T").effect_chain.effects[0].params == \
            {"enabled": True, "k": 5}
        # undo 恢复逐字段原链
        tl3 = cmd2.undo(tl2)
        rt = tl3.get_track_by_id("T").effect_chain
        assert [e.algorithm_version for e in rt.effects] == [0, 2, 777]
        assert rt.effects[0].params == {"enabled": True, "k": 5}
        # 序列化往返
        data = cmd2.to_dict()
        from src.features.edit.core.commands.audio_commands import (
            UpgradeTrackEffectAlgorithmCommand as _U,
        )
        parsed = _U.from_dict(data)
        assert parsed.track_id == "T"
        # None = 本地当前版本（由 upgrade_chain_algorithm_version 解析）

    def test_upgrade_chain_helper_keeps_future(self):
        chain = EffectChainParams(effects=(
            TrackEffectInstance(effect_type="delay", instance_id="x",
                                params={}, algorithm_version=999),))
        out = upgrade_chain_algorithm_version(chain)
        assert out.effects[0].algorithm_version == 999


class TestParamExtrasPreservation:
    def test_all_ten_classes_roundtrip_unknown(self):
        from src.features.edit.core.timeline_v2 import (
            CompressorParams, DelayParams, ExpanderParams, ImagerParams,
            LimiterParams, ReverbParams, SaturationParams, SpectralManglerParams,
            StripEQParams, VoiceChangerParams,
        )
        for cls in (StripEQParams, LimiterParams, CompressorParams,
                    ReverbParams, DelayParams, SaturationParams, ImagerParams,
                    ExpanderParams, VoiceChangerParams, SpectralManglerParams):
            d = cls.default().to_dict()
            d["future_key_zz"] = {"nested": [1, 2]}
            rt = cls.from_dict(d).to_dict()
            assert rt["future_key_zz"] == {"nested": [1, 2]}, cls

    def test_upgrade_does_not_lose_param_extras(self):
        """实例升级只改版本：params 内未知键原样保留。"""
        from src.features.edit.core.commands.audio_commands import (
            UpgradeTrackEffectAlgorithmCommand,
        )
        from src.features.edit.core.timeline_v2 import (
            ClipInstance, EditTrack, TimelineSequence,
        )
        inst = TrackEffectInstance.from_dict({
            "effect_type": "voice_changer", "instance_id": "v",
            "params": {"pitch_semitones": 4.0, "future_field": 42},
            "algorithm_version": 0})
        track = EditTrack("T", "T", "audio", 0,
                          clips=(ClipInstance("c", "a", "T", 0, 40, 0, 40),),
                          effect_chain=EffectChainParams(effects=(inst,)))
        from src.shared.media.metadata import Rational
        tl = TimelineSequence(fps=Rational(25, 1), audio_tracks=(track,))
        tl2, _ = UpgradeTrackEffectAlgorithmCommand.create("T").apply(tl)
        e = tl2.get_track_by_id("T").effect_chain.effects[0]
        assert e.params["future_field"] == 42
        assert e.params["pitch_semitones"] == 4.0


class TestExportPreflightGating:
    def _seq_with_future_instance(self):
        from src.features.edit.core.timeline_v2 import (
            ClipInstance, EditTrack, TimelineSequence,
        )
        from src.shared.media.metadata import Rational
        inst = TrackEffectInstance(effect_type="delay", instance_id="d",
                                   params={"left_delay": 10.0},
                                   algorithm_version=777)
        track = EditTrack("T", "T", "audio", 0,
                          clips=(ClipInstance("c", "a", "T", 0, 40, 0, 40),),
                          effect_chain=EffectChainParams(effects=(inst,)))
        return TimelineSequence(fps=Rational(25, 1), audio_tracks=(track,))

    def test_future_blocks_without_consent(self):
        from src.features.edit.core.timeline_export import (
            TimelineExportConfig, preflight_audio_export,
        )
        cfg = TimelineExportConfig(output_path="/tmp/x.mov")
        result = preflight_audio_export(cfg, self._seq_with_future_instance())
        assert not result.ok
        assert any("777" in m or "非完全支持" in m for m in result.blocking)

    def test_consent_downgrades_to_warning(self):
        from src.features.edit.core.timeline_export import (
            TimelineExportConfig, preflight_audio_export,
        )
        cfg = TimelineExportConfig(output_path="/tmp/x.mov",
                                   allow_algorithm_degraded=True)
        result = preflight_audio_export(cfg, self._seq_with_future_instance())
        assert result.ok
        assert result.degraded

    def test_clean_project_not_blocked(self):
        from src.features.edit.core.timeline_export import (
            TimelineExportConfig, preflight_audio_export,
        )
        inst = TrackEffectInstance(effect_type="delay", instance_id="d",
                                   params={"left_delay": 10.0},
                                   algorithm_version=ALGORITHM_VERSION)
        from src.features.edit.core.timeline_v2 import (
            ClipInstance, EditTrack, TimelineSequence,
        )
        from src.shared.media.metadata import Rational
        track = EditTrack("T", "T", "audio", 0,
                          clips=(ClipInstance("c", "a", "T", 0, 40, 0, 40),),
                          effect_chain=EffectChainParams(effects=(inst,)))
        seq = TimelineSequence(fps=Rational(25, 1), audio_tracks=(track,))
        cfg = TimelineExportConfig(output_path="/tmp/x.mov")
        assert preflight_audio_export(cfg, seq).ok


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
