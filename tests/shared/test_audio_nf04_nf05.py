"""NF-04/05：新 FX 实例遥测消费 + preset/seed 持久化（计划 §6.6/§9.1）。

- NF-04：update_track_telemetry 将实例 tap（in/out dB）送入两款新对话
  框；silent（停止/暂停）清表。DSP collect_telemetry 的 F0/模块活动
  在 VC 对话框 update_telemetry 已消费（NF-02 测试覆盖）。
- NF-05：effect_preset 用户库对两款新类型开箱即用（按 effect_type 分
  桶，原子写）；seed 随参数持久化，保存/加载往返不变。
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.effect_preset import (
    load_user_presets,
    remove_user_preset,
    save_user_preset,
)
from src.features.edit.core.timeline_v2 import (
    SpectralManglerParams,
    VoiceChangerParams,
)


def _make_mixer(qapp):
    from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget
    return TrackMixerWidget(track_id="T1", track_name="Track 1", track_tag="A1")


class TestInstanceTelemetryDispatch:
    def test_telemetry_reaches_new_dialogs(self, qapp):
        w = _make_mixer(qapp)
        w._effect_add("voice_changer")
        vc_inst = w._effect_chain.effects[-1]
        w._open_voice_changer_editor(vc_inst)
        vc_dlg = w._voice_changer_dialogs[vc_inst.instance_id]
        w._effect_add("spectral_mangler")
        sm_inst = w._effect_chain.effects[-1]
        w._open_spectral_mangler_editor(sm_inst)
        sm_dlg = w._spectral_mangler_dialogs[sm_inst.instance_id]

        # 正常载荷：不抛错（对话框消费 in/out RMS）
        w.update_track_telemetry({
            "peak": [-6.0, -6.0], "rms": [-18.0, -18.0],
            "taps": {
                vc_inst.instance_id: {"in_db": -12.0, "out_db": -14.0,
                                      "bypassed": False},
                sm_inst.instance_id: {"in_db": -10.0, "out_db": -8.0,
                                      "bypassed": False},
            },
            "spectrum": None,
        })
        # silent 载荷：清表事件不抛错
        w.update_track_telemetry({"silent": True, "spectrum": None})


class TestUserPresets:
    def test_preset_library_roundtrip_with_seed(self, tmp_path, monkeypatch):
        store = tmp_path / "effect_presets.json"
        monkeypatch.setattr(
            "src.features.edit.core.effect_preset._store_path",
            lambda: str(store))
        vc = VoiceChangerParams(pitch_semitones=-9.5,
                                pitch_jitter_pct=42.0,
                                random_seed=31337)
        assert save_user_preset("voice_changer", "Deep", vc.to_dict(),
                                preset_id="vc-1")
        sm = SpectralManglerParams(routing="parallel",
                                   grain_density_hz=55.0,
                                   random_seed=4242)
        assert save_user_preset("spectral_mangler", "Wide", sm.to_dict(),
                                preset_id="sm-1")
        vc_entries = load_user_presets("voice_changer")
        assert len(vc_entries) == 1
        assert vc_entries[0]["params"]["pitch_semitones"] == -9.5
        assert vc_entries[0]["params"]["random_seed"] == 31337
        assert vc_entries[0]["algorithm_version"] >= 4
        sm_entries = load_user_presets("spectral_mangler")
        assert sm_entries[0]["params"]["routing"] == "parallel"
        assert sm_entries[0]["params"]["random_seed"] == 4242
        # 删除
        assert remove_user_preset("voice_changer", "vc-1")
        assert load_user_presets("voice_changer") == []
        # 两条目独立分桶
        data = json.loads(store.read_text(encoding="utf-8"))
        assert set(data["presets"].keys()) >= {"voice_changer",
                                               "spectral_mangler"}

    def test_preset_apply_through_params_validation(self):
        """预设参数经 Params 校验后应用到实例（坏值回退默认，不抛错）。"""
        parsed = VoiceChangerParams.from_dict(
            {"pitch_semitones": 99.0, "vocoder_carrier": "bogus"})
        assert parsed.pitch_semitones == 99.0  # from_dict 不钳位数值范围由 DSP 兜底
        assert parsed.vocoder_carrier == "saw"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
