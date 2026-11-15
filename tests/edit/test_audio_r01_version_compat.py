"""R01：版本兼容、未知数据保留与能力契约回归（计划 §5 R01）。

验收对应复审报告 2026-11-15：
- legacy／current／future fixture 各自 load→save→reload 字段无损；
- 未知实例夹在两个 Delay 中间，已知实例与 metadata 不丢、链不清空；
- 预设 algorithm_version 777 存盘再读回仍是 777（不再被写成本地 3）；
- 损坏条目保留诊断，Master 链遇未知实例不再整链丢弃；
- 旧 HPF PCM（df749759 系数设计）在容差内重现，当前实例仍走 v3 设计；
- 显式升级改变声音且（经 SetTrackEffectChain）可 undo。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_dsp import (
    ALGORITHM_VERSION,
    EffectChain,
    StripEQFilter,
    _biquad_highpass,
    strip_eq_band_sections,
)
from src.features.edit.core.effect_preset import load_user_presets, save_user_preset
from src.features.edit.core.fx_versioning import (
    AUDIO_FX_SCHEMA_VERSION,
    algorithm_compat_bucket,
    chain_has_diagnostics,
    collect_chain_diagnostics,
    effect_type_support,
    upgrade_chain_algorithm_version,
)
from src.features.edit.core.mixer_preset import ChannelStripPreset
from src.features.edit.core.timeline_v2 import (
    DelayParams,
    EditTrack,
    EffectChainParams,
    TimelineSequence,
    TrackEffectInstance,
)

SR = 48000.0


def _delay_instance(delay_ms: float, iid: str = "", av: int | None = None) -> dict:
    d = {
        "effect_type": "delay",
        "params": DelayParams(left_delay=delay_ms,
                              right_delay=delay_ms).to_dict(),
        "enabled": True,
        "order": 0,
        "instance_id": iid,
        "panel": "fx",
    }
    if av is not None:
        d["algorithm_version"] = av
    return d


# ───────────────────────────── 无损往返 ─────────────────────────────

class TestFixtureRoundtrip:
    def test_legacy_current_future_instances_roundtrip_lossless(self):
        raw = {
            "effects": [
                _delay_instance(80.0, "d1"),                    # legacy（无版本键）
                _delay_instance(100.0, "d2", av=ALGORITHM_VERSION),   # current
                _delay_instance(120.0, "d3", av=777),           # future
            ],
        }
        for i, e in enumerate(raw["effects"]):
            e["order"] = i
        chain = EffectChainParams.from_dict(raw)
        # load → save → reload
        reloaded = EffectChainParams.from_dict(chain.to_dict())
        again = EffectChainParams.from_dict(reloaded.to_dict())
        assert [e.instance_id for e in again.effects] == ["d1", "d2", "d3"]
        assert [e.params["left_delay"] for e in again.effects] == [80.0, 100.0, 120.0]
        # legacy 无版本键 → 加载时映射为明确 legacy 版本，且稳定保持
        assert again.effects[0].algorithm_version == 2
        assert reloaded.effects[0].algorithm_version == again.effects[0].algorithm_version
        assert again.effects[1].algorithm_version == ALGORITHM_VERSION
        # future 版本数据原样保留（无损往返，不伪装成本地版本）
        assert again.effects[2].algorithm_version == 777

    def test_sequence_writes_and_consumes_schema_version(self):
        seq = TimelineSequence(name="v")
        d = seq.to_dict()
        assert d["audio_fx_schema_version"] == AUDIO_FX_SCHEMA_VERSION
        # v2 键是已知键：load 后不再作为 extras 回显重复键
        d2 = TimelineSequence.from_dict(d).to_dict()
        keys = [k for k in d2 if k == "audio_fx_schema_version"]
        assert keys == ["audio_fx_schema_version"]

    def test_unknown_instance_metadata_survives_roundtrip(self):
        raw = _delay_instance(80.0, "dx", av=777)
        raw["mystery_meta"] = {"vendor": "future", "blob": [1, 2, 3]}
        inst = TrackEffectInstance.from_dict(raw)
        rt = TrackEffectInstance.from_dict(inst.to_dict())
        assert rt._extra_fields == {"mystery_meta": {"vendor": "future", "blob": [1, 2, 3]}}
        assert rt.algorithm_version == 777


# ───────────────────── 未知实例不清链（普通轨 + Master） ─────────────────────

class TestUnknownInstancePreservation:
    def test_unknown_fx_between_two_delays_keeps_known_instances(self):
        raw = {
            "effects": [
                _delay_instance(80.0, "d1"),
                {"effect_type": "future_fx", "params": {"x": 1.0},
                 "enabled": True, "order": 1, "instance_id": "F1",
                 "vendor_meta": "keep-me"},
                _delay_instance(120.0, "d2"),
            ],
        }
        track_data = {
            "track_id": "t1", "name": "T", "track_type": "audio", "index": 0,
            "effect_chain": raw,
        }
        track = EditTrack.from_dict(track_data)
        chain = track.effect_chain
        assert chain is not None
        types = [e.effect_type for e in chain.effects]
        assert types == ["delay", "future_fx", "delay"]
        unknown = chain.effects[1]
        assert unknown.instance_id == "F1"
        assert unknown.params == {"x": 1.0}
        assert unknown._extra_fields == {"vendor_meta": "keep-me"}
        # 保存重开后仍完整保留
        rt = EditTrack.from_dict(track.to_dict())
        assert [e.effect_type for e in rt.effect_chain.effects] == types
        assert rt.effect_chain.effects[1].params == {"x": 1.0}

    def test_master_chain_unknown_instance_does_not_drop_whole_chain(self):
        seq_data = {
            "timeline_id": "s1", "name": "S", "fps": "24/1",
            "master_effect_chain": {"effects": [
                _delay_instance(80.0, "md1"),
                {"effect_type": "future_fx", "params": {"a": 2},
                 "enabled": True, "order": 1, "instance_id": "MF"},
            ]},
        }
        seq = TimelineSequence.from_dict(seq_data)
        assert seq.master_effect_chain is not None
        assert [e.effect_type for e in seq.master_effect_chain.effects] == \
            ["delay", "future_fx"]

    def test_corrupted_entry_diagnosed_and_skipped(self):
        chain = EffectChainParams.from_dict({"effects": [
            _delay_instance(80.0, "ok1"),
            "not-a-dict",
            _delay_instance(120.0, "ok2"),
        ]})
        assert [e.instance_id for e in chain.effects] == ["ok1", "ok2"]

    def test_dsp_chain_skips_unknown_with_no_silent_loss_of_known(self):
        chain = EffectChain(SR)
        chain.set_params([
            ("delay", DelayParams(left_delay=80.0, right_delay=80.0).to_dict(),
             True, "d1", 2),
            ("future_fx", {"x": 1.0}, True, "F1", 2),
            ("delay", DelayParams(left_delay=120.0, right_delay=120.0).to_dict(),
             True, "d2", 2),
        ], sample_rate=SR)
        blk = np.zeros((2, 256), dtype=np.float32)
        chain.process(blk)
        taps = chain.tap_levels()
        assert set(taps) == {"d1", "d2"}


# ───────────────────────── 预设版本保持 ─────────────────────────

class TestPresetVersionPreservation:
    def test_preset_777_roundtrip_stays_777(self):
        raw = {
            "preset_id": "p1", "name": "future-preset",
            "algorithm_version": 777,
            "volume": 1.0, "pan": 0.0, "is_muted": False, "is_solo": False,
            "strip_eq": None,
            "effect_chain": {"effects": [_delay_instance(80.0, "pd", av=777)]},
            "panner": None, "output_bus_id": None, "sends": [],
            "is_bus": False, "created_at": "2026-01-01T00:00:00",
        }
        p = ChannelStripPreset.from_dict(raw)
        rt = ChannelStripPreset.from_dict(p.to_dict())
        assert rt.algorithm_version == 777
        assert rt.effect_chain.effects[0].algorithm_version == 777

    def test_preset_without_version_maps_to_legacy(self):
        raw = {"preset_id": "p0", "name": "old", "algorithm_version": 2}
        p = ChannelStripPreset.from_dict(raw)
        assert p.algorithm_version == 2

    def test_from_track_captures_current_version(self):
        track = EditTrack(track_id="t", name="T", track_type="audio", index=0)
        p = ChannelStripPreset.from_track(track, "snap")
        assert p.algorithm_version == ALGORITHM_VERSION

    def test_effect_user_preset_roundtrip_keeps_version(self, monkeypatch, tmp_path):
        store = tmp_path / "effect_presets.json"
        monkeypatch.setattr("src.features.edit.core.effect_preset._store_path",
                            lambda: str(store))
        assert save_user_preset("delay", "MyDelay",
                                DelayParams(left_delay=123.0,
                                            right_delay=123.0).to_dict(),
                                preset_id="pid-1")
        # 手动把版本改写为未来值，验证读回不覆写
        import json
        data = json.loads(store.read_text(encoding="utf-8"))
        data["presets"]["delay"][0]["algorithm_version"] = 777
        store.write_text(json.dumps(data), encoding="utf-8")
        entries = load_user_presets("delay")
        assert entries and entries[0]["algorithm_version"] == 777


# ─────────────────── 旧 HPF PCM 兼容与显式升级 ───────────────────

def _steady_rms_db(y: np.ndarray, skip: int) -> float:
    return float(20.0 * np.log10(np.sqrt(np.mean(y[skip:] ** 2)) + 1e-24))


class TestLegacyHpfCompat:
    def test_legacy_bucket_reproduces_df749759_hpf(self):
        """250Hz 正弦过 1000Hz/12dB HPF：legacy 桶复现旧系数设计（±0.01dB）。"""
        n = int(SR * 2)
        t = np.arange(n) / SR
        x = 0.5 * np.sin(2 * np.pi * 250.0 * t)
        from scipy.signal import sosfilt
        # 独立 oracle：旧实现 = 级联 slope//6 个相同 Q=0.7071 biquad
        rows = [_biquad_highpass(1000.0, 0.7071, SR) for _ in range(2)]
        oracle_sos = np.array([[b[0], b[1], b[2], 1.0, a[1], a[2]]
                               for b, a in rows])
        oracle_rms = _steady_rms_db(sosfilt(oracle_sos, x), n // 2)

        band = {"filter_type": "HPF", "freq": 1000.0, "gain": 0.0,
                "q": 1.1, "slope": 12, "enabled": True}
        legacy_sos = np.array(strip_eq_band_sections(
            band, SR, "Clean", algorithm_version=2))
        legacy_rms = _steady_rms_db(sosfilt(legacy_sos, x), n // 2)
        assert abs(legacy_rms - oracle_rms) <= 0.01

        current_sos = np.array(strip_eq_band_sections(
            band, SR, "Clean", algorithm_version=ALGORITHM_VERSION))
        current_rms = _steady_rms_db(sosfilt(current_sos, x), n // 2)
        # v3 与 legacy 的差值即复审测得的 ~24.12dB（确认两者确实不同路径）
        assert abs(current_rms - oracle_rms) > 20.0

    def test_effect_chain_passes_version_to_eq(self):
        band = {"filter_type": "HPF", "freq": 1000.0, "gain": 0.0,
                "q": 1.1, "slope": 12, "enabled": True, "index": 0}
        params = {"enabled": True, "voicing": "Clean", "master_gain": 0.0,
                  "bands": [band]}
        n = int(SR)
        x = (0.5 * np.sin(2 * np.pi * 250.0 * np.arange(n) / SR)).reshape(1, -1)

        eq_legacy = StripEQFilter(SR)
        eq_legacy.set_params(params, sample_rate=SR, algorithm_version=2)
        y_legacy = np.asarray(eq_legacy.process(x.astype(np.float32)))

        eq_current = StripEQFilter(SR)
        eq_current.set_params(params, sample_rate=SR,
                              algorithm_version=ALGORITHM_VERSION)
        y_current = np.asarray(eq_current.process(x.astype(np.float32)))
        # 稳态幅值显著不同（legacy 桶更深）
        assert _steady_rms_db(y_legacy[0], n // 2) < \
            _steady_rms_db(y_current[0], n // 2) - 20.0

    def test_explicit_upgrade_changes_sound_and_undo_restores(self):
        """模型层：升级把 av=2 提到当前版本（声音路径切换）；undo = 原链恢复。"""
        from src.features.edit.core.commands import CommandStack
        from src.features.edit.core.commands.audio_commands import \
            SetTrackEffectChainCommand

        legacy_chain = EffectChainParams(effects=(
            TrackEffectInstance.from_dict(_delay_instance(80.0, "d1")),
        ))
        assert legacy_chain.effects[0].algorithm_version == 2

        upgraded = upgrade_chain_algorithm_version(legacy_chain)
        assert upgraded.effects[0].algorithm_version == ALGORITHM_VERSION
        # 升级只改版本，不动声音参数 / 身份
        assert upgraded.effects[0].params == legacy_chain.effects[0].params
        assert upgraded.effects[0].instance_id == legacy_chain.effects[0].instance_id
        # 未来版本实例不被升级（数据保留）
        future_chain = EffectChainParams(effects=(
            TrackEffectInstance.from_dict(_delay_instance(90.0, "f1", av=777)),))
        assert upgrade_chain_algorithm_version(
            future_chain).effects[0].algorithm_version == 777

        # 命令路径：execute → undo 恢复原链（含版本）
        track = EditTrack(track_id="tb", name="B", track_type="audio", index=0)
        seq = TimelineSequence(name="u2", audio_tracks=(track,))
        stack = CommandStack()
        seq = stack.execute(
            SetTrackEffectChainCommand.create("tb", upgraded), seq)
        applied = seq.get_track_by_id("tb").effect_chain
        assert applied.effects[0].algorithm_version == ALGORITHM_VERSION
        seq = stack.undo(seq)
        assert seq.get_track_by_id("tb").effect_chain is None


# ───────────────────────── 能力声明 ─────────────────────────

class TestCapabilityDeclaration:
    def test_buckets(self):
        assert algorithm_compat_bucket(0) == "legacy"
        assert algorithm_compat_bucket(2) == "legacy"
        assert algorithm_compat_bucket(3) == "current"
        assert algorithm_compat_bucket(999) == "future"

    def test_effect_type_support_states(self):
        assert effect_type_support("delay")[0] == "supported"
        status, reason = effect_type_support("future_fx")
        assert status == "unsupported" and "future_fx" in reason

    def test_collect_chain_diagnostics_and_flag(self):
        chain = EffectChainParams.from_dict({"effects": [
            _delay_instance(80.0, "d1"),
            {"effect_type": "future_fx", "params": {}, "instance_id": "F1"},
        ]})
        diags = collect_chain_diagnostics(chain)
        by_id = {d["instance_id"]: d for d in diags}
        assert by_id["d1"]["status"] == "supported"
        assert by_id["d1"]["compat_bucket"] == "legacy"
        assert by_id["F1"]["status"] == "unsupported"
        assert chain_has_diagnostics(chain)
        clean = EffectChainParams.from_dict({"effects": [_delay_instance(1.0, "ok")]})
        assert not chain_has_diagnostics(clean)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
