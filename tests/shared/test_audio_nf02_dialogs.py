"""NF-02：两款新 FX 对话框与 Mixer/clip 接入回归（计划 §6.2–§6.3）。

- 用户原型 UI 生产接入：demo=False 不启动假数据定时器（频谱/电平保持
  地板，由 update_telemetry 注入）；视觉风格保留原作。
- 扁平 Params ↔ 嵌套 UI state 的唯一映射表往返（含枚举 id 稳定化）。
- 手势：widget stateChanged → 节流 preview + 停手 400ms 单次 committed；
  程序化 set_current_params 零提交。
- Mixer：默认参数/显示名/对话框字典/分发/R06 外部回填闭环。
- clip：注册表指向真实对话框类。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.timeline_v2 import (
    SpectralManglerParams,
    VoiceChangerParams,
)


def _make_mixer(qapp):
    from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget
    return TrackMixerWidget(track_id="T1", track_name="Track 1", track_tag="A1")


class TestMappingTables:
    def test_voice_roundtrip_flat_nested(self):
        from src.shared.ui.audio_mixer.voice_changer_dialog import (
            params_from_ui_state,
            ui_state_from_params,
        )
        p = VoiceChangerParams(pitch_enabled=True, pitch_semitones=-7.3,
                               formant_shift_st=4.2, formant_mode="crisp",
                               vocoder_carrier="choir", vocoder_bands=24,
                               pitch_jitter_pct=33.0, random_seed=99)
        state = ui_state_from_params(p.to_dict())
        assert state["pitch"]["semitones"] == -7.3
        assert state["formant"]["mode"] == "Crisp"
        assert state["vocoder"]["carrier"] == "Choir"
        rt = params_from_ui_state(state)
        expected = p.to_dict()
        for key in expected:
            if key == "random_seed":
                continue  # seed 为元数据，不入 UI state
            assert rt[key] == expected[key], key

    def test_mangler_roundtrip_flat_nested(self):
        from src.shared.ui.audio_mixer.spectral_mangler_dialog import (
            params_from_ui_state,
            ui_state_from_params,
        )
        p = SpectralManglerParams(routing="parallel",
                                  output_limiter_enabled=True,
                                  ring_mode="rectify",
                                  grain_mode="reverse",
                                  dist_curve="bitcrush",
                                  grain_feedback_pct=77.0,
                                  global_mix_pct=64.0)
        state = ui_state_from_params(p.to_dict())
        assert state["routing"] == "Parallel Blend"
        assert state["limiter"] is True
        assert state["ring"]["mode"] == "Rectify"
        assert state["grain"]["mode"] == "Reverse"
        assert state["dist"]["curve"] == "BitCrush"
        rt = params_from_ui_state(state)
        expected = p.to_dict()
        for key in expected:
            if key == "random_seed":
                continue
            assert rt[key] == expected[key], key

    def test_missing_ui_fields_fall_back_to_defaults(self):
        """不完整的 UI state（如旧 bank）经 Params 校验回退默认，不抛错。"""
        from src.shared.ui.audio_mixer.voice_changer_dialog import (
            params_from_ui_state,
        )
        rt = params_from_ui_state({"enabled": True, "pitch": {"semitones": 5.0}})
        parsed = VoiceChangerParams.from_dict(rt)
        assert parsed.pitch_semitones == 5.0
        assert parsed.pitch_enabled is False  # 缺字段 → 默认


class TestDialogLifecycle:
    def _make_vc(self, qapp):
        from src.shared.ui.audio_mixer.voice_changer_dialog import (
            VoiceChangerDialog,
        )
        return VoiceChangerDialog(
            track_id="T1", track_name="Track 1", instance_id="VC1",
            initial_params=VoiceChangerParams(
                pitch_semitones=3.0).to_dict())

    def _make_sm(self, qapp):
        from src.shared.ui.audio_mixer.spectral_mangler_dialog import (
            SpectralManglerDialog,
        )
        return SpectralManglerDialog(
            track_id="T1", track_name="Track 1", instance_id="SM1",
            initial_params=SpectralManglerParams(
                ring_enabled=True).to_dict())

    def test_demo_timer_not_started(self, qapp):
        dlg = self._make_vc(qapp)
        assert not dlg._widget._demo_timer.isActive()
        sm = self._make_sm(qapp)
        assert not sm._widget._demo_timer.isActive()

    def test_gesture_preview_then_single_commit(self, qapp):
        dlg = self._make_vc(qapp)
        previews: list = []
        commits: list = []
        dlg.voice_changer_preview.connect(lambda tid, p: previews.append(p))
        dlg.voice_changer_committed.connect(lambda tid, p: commits.append(p))
        # 模拟连续拖动（多次 stateChanged）后停手
        state = dlg._widget.get_state()
        state["pitch"]["semitones"] = 8.0
        dlg._widget.set_state(state)  # set_state 本身不 emit——手动触发
        for _ in range(5):
            dlg._on_widget_state(state)
        assert commits == []  # 拖动中不提交
        dlg._commit_timer.stop()
        dlg._emit_commit()
        assert len(commits) == 1
        assert commits[0]["pitch_semitones"] == 8.0

    def test_set_current_params_is_silent(self, qapp):
        dlg = self._make_vc(qapp)
        commits: list = []
        dlg.voice_changer_committed.connect(lambda tid, p: commits.append(p))
        dlg.set_current_params(VoiceChangerParams(
            formant_shift_st=-6.0).to_dict())
        assert commits == []
        assert dlg._widget.get_state()["formant"]["shift"] == -6.0

    def test_backfill_then_edit_uses_new_values(self, qapp):
        """回填后继续编辑：提交的是回填后的新参数（R06 语义）。"""
        dlg = self._make_vc(qapp)
        dlg.set_current_params(VoiceChangerParams(
            pitch_semitones=11.0).to_dict())
        commits: list = []
        dlg.voice_changer_committed.connect(lambda tid, p: commits.append(p))
        dlg._emit_commit()
        assert commits and commits[0]["pitch_semitones"] == 11.0

    def test_telemetry_entry_points(self, qapp):
        dlg = self._make_vc(qapp)
        dlg.update_telemetry({
            "f0_in_hz": 150.0, "f0_out_hz": 300.0, "confidence": 0.8,
            "in_rms": 0.1, "out_rms": 0.08,
            "formant_active": True, "pitch_active": True,
            "vocoder_active": False,
        })
        # 无异常即通过（画布由原型实现;CLR 不伪造断言留给 NF-04）


class TestMixerIntegration:
    def test_default_params_and_names(self):
        from src.shared.ui.audio_mixer.track_mixer_widget import (
            _effect_default_params,
            _effect_display_name,
        )
        vc = _effect_default_params("voice_changer")
        assert vc == VoiceChangerParams.default().to_dict()
        sm = _effect_default_params("spectral_mangler")
        assert sm == SpectralManglerParams.default().to_dict()
        assert _effect_display_name("voice_changer") == "Voice"
        assert _effect_display_name("spectral_mangler") == "Mangler"

    def test_dialog_dict_and_dispatch(self, qapp):
        from src.shared.ui.audio_mixer.track_mixer_widget import (
            TrackMixerWidget,
        )
        w = _make_mixer(qapp)
        for attr in ("_voice_changer_dialogs", "_spectral_mangler_dialogs"):
            assert attr in TrackMixerWidget._EFFECT_DIALOG_ATTRS
            assert attr in TrackMixerWidget._EFFECT_DIALOG_TYPES
        # 实例编辑路径（直接调打开器）
        w._effect_add("voice_changer")
        inst = w._effect_chain.effects[-1]
        assert inst.effect_type == "voice_changer"
        w._open_voice_changer_editor(inst)
        dlg = w._voice_changer_dialogs.get(inst.instance_id)
        assert dlg is not None and dlg.isVisible()
        # 提交路径（守卫 + 链更新）
        commits: list = []
        w.track_effects_committed.connect(lambda tid, c: commits.append(c))
        dlg.set_current_params(VoiceChangerParams(
            pitch_semitones=6.0).to_dict())
        dlg._emit_commit()
        assert commits and commits[-1].effects[-1].params["pitch_semitones"] == 6.0

    def test_external_backfill_reaches_dialog(self, qapp):
        """R06 闭环：外部 set_effect_chain 同 id 改参 → 对话框回填。"""
        w = _make_mixer(qapp)
        w._effect_add("spectral_mangler")
        inst = w._effect_chain.effects[-1]
        w._open_spectral_mangler_editor(inst)
        dlg = w._spectral_mangler_dialogs[inst.instance_id]
        new_params = SpectralManglerParams(
            routing="grain_ring_dist", grain_enabled=True).to_dict()
        from src.features.edit.core.timeline_v2 import (
            EffectChainParams, TrackEffectInstance,
        )
        w.set_effect_chain(EffectChainParams(effects=(
            TrackEffectInstance(effect_type="spectral_mangler",
                                instance_id=inst.instance_id,
                                params=new_params),)))
        state = dlg._widget.get_state()
        assert state["routing"] == "Grain → Ring → Dist"
        assert state["grain"]["enabled"] is True

    def test_clip_registry_uses_real_dialogs(self):
        from src.features.edit.ui.clip_audio_effect_dialog_adapter import (
            _ensure_dialog_registry,
        )
        from src.shared.ui.audio_mixer.voice_changer_dialog import (
            VoiceChangerDialog,
        )
        from src.shared.ui.audio_mixer.spectral_mangler_dialog import (
            SpectralManglerDialog,
        )
        reg = _ensure_dialog_registry()
        assert reg["voice_changer"][0] is VoiceChangerDialog
        assert reg["spectral_mangler"][0] is SpectralManglerDialog


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
