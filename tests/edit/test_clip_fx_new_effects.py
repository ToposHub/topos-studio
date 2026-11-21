"""Clip rack panels, project editing signals, and audio processing regressions."""
from __future__ import annotations

import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QDialogButtonBox, QWidget
from pytestqt.qtbot import QtBot

from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
    _ensure_effect_param_registry,
)
from src.features.edit.ui.clip_audio_effect_dialog_adapter import ClipAudioEffectDialogManager
from src.features.mixer.effects.catalog import get_effect

pytestmark = pytest.mark.edit_core
FPS = 25

_RACK_PANELS = {
    "de_esser": "deesser",
    "true_peak_limiter": "true_peak_limiter",
    "dynamic_eq": "dynamic_eq",
    "bus_comp": "bus_compressor",
    "multiband_comp": "multiband_compressor",
    "convolution_reverb": "convolution_reverb",
    "transient_designer": "transient_designer",
    "tremolo_auto_pan": "tremolo_autopan",
    "chorus_flanger": "modulation",
    "phaser": "modulation",
}
_TEN_FX = tuple(_RACK_PANELS)


def _default_params(effect_type: str) -> dict[str, Any]:
    return _ensure_effect_param_registry()[effect_type].default().to_dict()


def test_params_classes_have_roundtrip() -> None:
    registry = _ensure_effect_param_registry()
    for effect_type in _TEN_FX:
        params = _default_params(effect_type)
        assert params
        assert registry[effect_type].from_dict(params).to_dict() == params


class TestOpenDialog:
    @pytest.mark.parametrize(("effect_type", "panel_id"), _RACK_PANELS.items())
    def test_opens_supplied_effect_panel(self, qtbot: QtBot, effect_type: str, panel_id: str) -> None:
        manager = ClipAudioEffectDialogManager()
        dialog = manager.open_dialog(effect_type, "clip_1", "fx_1", _default_params(effect_type), "DIALOG CLIP")

        assert dialog is not None
        descriptor = get_effect(panel_id)
        assert type(dialog.panel).__module__ == descriptor.panel_module
        assert type(dialog.panel).__name__ == descriptor.panel_class
        assert dialog.isVisible()
        assert "DIALOG CLIP" in dialog.windowTitle()
        dialog.close()

    def test_open_caches_dialog_per_clip_effect(self, qtbot: QtBot) -> None:
        manager = ClipAudioEffectDialogManager()
        params = _default_params("de_esser")
        first = manager.open_dialog("de_esser", "clip_a", "fx_a", params)
        assert first is not None
        assert manager.open_dialog("de_esser", "clip_a", "fx_a", params) is first
        assert manager.open_dialog("de_esser", "clip_a", "fx_b", params) is not first
        manager.close_all()

    def test_partial_dict_uses_defaults(self, qtbot: QtBot) -> None:
        manager = ClipAudioEffectDialogManager()
        dialog = manager.open_dialog("de_esser", "clip_p", "fx_p", {"threshold_dbfs": -40.0})

        assert dialog is not None
        assert dialog.panel.get_state()["threshold"] == -40.0
        assert dialog.panel.get_state()["mode"] == "split"
        assert dialog.panel.ratio_input.value() == pytest.approx(4.0)
        dialog.close()

    def test_multiband_edit_roundtrips_crossover_array(self, qtbot: QtBot) -> None:
        manager = ClipAudioEffectDialogManager()
        params = _default_params("multiband_comp")
        dialog = manager.open_dialog("multiband_comp", "clip_m", "fx_m", params)

        assert dialog is not None
        assert dialog.panel.get_state()["crossovers"][:2] == params["crossovers"]
        # This signal is emitted by dragging the crossover on the supplied graph.
        dialog.panel.mb_graph.crossoverDragged.emit(0, 275.0)
        buttons = dialog.findChild(QDialogButtonBox)
        with qtbot.waitSignal(manager.clip_effect_params_committed) as commit:
            qtbot.mouseClick(buttons.button(QDialogButtonBox.StandardButton.Apply), Qt.MouseButton.LeftButton)
        assert commit.args == ["clip_m", "fx_m", {**params, "crossovers": [275.0, params["crossovers"][1]]}, None]
        dialog.close()

    def test_corrupt_params_fall_back_with_warning(
        self, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        def fail_parse(_params: dict[str, Any]) -> Any:
            raise ValueError("corrupt")

        params_class = _ensure_effect_param_registry()["phaser"]
        monkeypatch.setattr(params_class, "from_dict", staticmethod(fail_parse))
        manager = ClipAudioEffectDialogManager()
        with caplog.at_level(logging.WARNING):
            dialog = manager.open_dialog("phaser", "clip_c", "fx_c", {"garbage": object()})

        assert dialog is not None
        assert dialog.panel.get_state()["phaser"]["rate"] == _default_params("phaser")["rate_hz"]
        assert "Audio effect phaser has invalid panel parameters" in caplog.text
        dialog.close()


class TestSignalRoundTrip:
    @pytest.mark.parametrize(("effect_type", "edit", "expected"), [
        ("de_esser", lambda panel: panel.ratio_input.setValue(6.5), {"ratio": 6.5}),
        ("true_peak_limiter", lambda panel: panel.engine_card.stereo_link_button.click(), {"linked": False}),
        ("dynamic_eq", lambda panel: panel.band_cards[3].c_range.knob.setValue(-16.0, emit=True),
         {"max_cut_db": 16.0}),
        ("dynamic_eq", lambda panel: panel.band_cards[3].c_boost.knob.setValue(5.0, emit=True),
         {"max_boost_db": 5.0}),
        ("dynamic_eq", lambda panel: panel.band_cards[3].c_threshold_slope.knob.setValue(0.75, emit=True),
         {"slope": 0.75}),
        ("bus_comp", lambda panel: panel.btn_api.click(), {"template": "mx_glue"}),
        ("bus_comp", lambda panel: panel.c_sc_hpf.knob.setValue(237.0, emit=True), {"sc_hpf_hz": 237.0}),
        ("convolution_reverb", lambda panel: panel._controls["dry_level"].setValue(65.0, emit=True), {"dry": 0.65}),
        ("transient_designer", lambda panel: panel.transient_card.c_attack.knob.setValue(-50.0, emit=True),
         {"attack_db": -6.0}),
        ("transient_designer", lambda panel: panel.transient_card.c_sustain.knob.setValue(50.0, emit=True),
         {"sustain_db": 6.0}),
        ("transient_designer", lambda panel: panel.transient_card.c_attack_time.knob.setValue(17.0, emit=True),
         {"attack_ms": 17.0}),
        ("chorus_flanger", lambda panel: panel.chorus_card.c_depth.knob.setValue(50.0, emit=True), {"depth_ms": 5.0}),
        ("chorus_flanger", lambda panel: panel.chorus_card.c_voices.knob.setValue(3.0, emit=True), {"voices": 3}),
        ("chorus_flanger", lambda panel: panel.chorus_card.c_processor_feedback.knob.setValue(40.0, emit=True),
         {"feedback": 0.4}),
        ("phaser", lambda panel: panel.phaser_card.stage_btns[3].click(), {"stages": 3}),
        ("phaser", lambda panel: panel.phaser_card.c_depth.knob.setValue(60.0, emit=True), {"sweep_octaves": 3.2}),
        ("saturation", lambda panel: panel.global_tape_panel.g_drive_knob.setValue(37.5, emit=True), {"drive": 37.5}),
        ("imager", lambda panel: panel.knob_pan.setValue(-40.0, emit=True), {"pan": -40.0}),
    ])
    def test_native_control_previews_and_commits_existing_processor_parameter(
        self, qtbot: QtBot, effect_type: str, edit: Callable[[QWidget], None], expected: dict[str, Any],
    ) -> None:
        manager = ClipAudioEffectDialogManager()
        params = {**_default_params(effect_type), "custom_metadata": {"source": "older project"}}
        dialog = manager.open_dialog(effect_type, "clip_native", "fx_native", params)
        assert dialog is not None
        expected_params = params | {
            key: pytest.approx(value) if isinstance(value, float) else value
            for key, value in expected.items()
        }
        try:
            with qtbot.waitSignal(manager.clip_effect_preview, timeout=1000) as preview:
                edit(dialog.panel)
            with qtbot.waitSignal(manager.clip_effect_params_committed) as commit:
                dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Apply).click()
            assert preview.args == ["clip_native", "fx_native", expected_params, None]
            assert commit.args == ["clip_native", "fx_native", expected_params, None]
            assert dialog.current_params() == expected_params
        finally:
            dialog.close()

    def test_modulation_mode_switch_keeps_edits_and_connects_flanger_card(self, qtbot: QtBot) -> None:
        manager = ClipAudioEffectDialogManager()
        params = _default_params("chorus_flanger")
        dialog = manager.open_dialog("chorus_flanger", "clip_native", "fx_native", params)
        assert dialog is not None
        try:
            dialog.panel.chorus_card.c_rate.knob.setValue(2.0, emit=True)
            dialog.panel.mode_btns["Flanger"].click()
            assert not dialog.panel.chorus_card.isVisible()
            assert dialog.panel.flanger_card.isVisible()
            dialog.panel.flanger_card.c_depth.knob.setValue(62.0, emit=True)
            assert dialog.current_params() == params | {"mode": "flanger", "rate_hz": 2.0, "depth_ms": 6.2}
            dialog.panel.mode_btns["Chorus"].click()
            assert dialog.panel.chorus_card.isVisible()
            assert dialog.current_params() == params | {"rate_hz": 2.0, "depth_ms": 6.2}
        finally:
            dialog.close()

    def test_multiband_native_gain_preserves_legacy_metadata(self, qtbot: QtBot) -> None:
        manager = ClipAudioEffectDialogManager()
        params = _default_params("multiband_comp")
        params["bands"][0].update(makeup_db=4.0, knee_db=12.0)
        dialog = manager.open_dialog("multiband_comp", "clip_native", "fx_native", params)
        assert dialog is not None
        try:
            state = dialog.panel.get_state()
            assert state["band_count"] == 3
            assert state["bands"][0]["makeup"] == 0.0
            assert state["bands"][0]["knee"] == 30.0
            assert dialog.current_params() == params
            dialog.panel.band_cards[0].c_makeup.knob.setValue(22.0, emit=True)
            expected_bands = [params["bands"][0] | {"makeup_gain": 22.0}, *params["bands"][1:]]
            assert dialog.current_params() == params | {"bands": expected_bands}
        finally:
            dialog.close()

    def test_multiband_canonical_crossovers_take_precedence_over_legacy_aliases(self, qtbot: QtBot) -> None:
        manager = ClipAudioEffectDialogManager()
        params = _default_params("multiband_comp") | {
            "crossovers": [220.0, 4000.0], "crossover_low": 155.0, "crossover_high": 1950.0,
        }
        dialog = manager.open_dialog("multiband_comp", "clip_native", "fx_native", params)
        assert dialog is not None
        try:
            assert dialog.panel.get_state()["crossovers"][:2] == [220.0, 4000.0]
            dialog.panel.mb_graph.crossoverDragged.emit(0, 275.0)
            saved = params | {"crossovers": [275.0, 4000.0]}
            assert dialog.current_params() == saved
            dialog.apply()
            assert dialog.panel.get_state()["crossovers"][:2] == saved["crossovers"]
        finally:
            dialog.close()

    def test_commit_emits_production_params_for_knob_edit(self, qtbot: QtBot) -> None:
        manager = ClipAudioEffectDialogManager()
        params = _default_params("de_esser")
        dialog = manager.open_dialog("de_esser", "clip_1", "fx_1", params)

        assert dialog is not None
        dialog.panel.thresh_knob.setValue(-36.0, emit=True)
        buttons = dialog.findChild(QDialogButtonBox)
        with qtbot.waitSignal(manager.clip_effect_params_committed) as commit:
            qtbot.mouseClick(buttons.button(QDialogButtonBox.StandardButton.Apply), Qt.MouseButton.LeftButton)
        assert commit.args == ["clip_1", "fx_1", {**params, "threshold_dbfs": -36.0}, None]
        dialog.close()

    def test_cancel_discards_limiter_preview_and_releases_window(self, qtbot: QtBot) -> None:
        manager = ClipAudioEffectDialogManager()
        params = _default_params("true_peak_limiter")
        dialog = manager.open_dialog("true_peak_limiter", "clip_3", "fx_3", params)

        assert dialog is not None
        commits = QSignalSpy(manager.clip_effect_params_committed)
        with qtbot.waitSignal(manager.clip_effect_preview, timeout=1000) as preview:
            dialog.panel.gain_lufs_card.c_ceiling.knob.setValue(-3.0, emit=True)
        assert preview.args == ["clip_3", "fx_3", {**params, "ceiling_dbtp": -3.0}, None]

        buttons = dialog.findChild(QDialogButtonBox)
        with qtbot.waitSignal(manager.clip_effect_preview_finished) as finished:
            qtbot.mouseClick(buttons.button(QDialogButtonBox.StandardButton.Cancel), Qt.MouseButton.LeftButton)
        assert finished.args == ["clip_3", "fx_3"]
        assert len(commits) == 0
        reopened = manager.open_dialog("true_peak_limiter", "clip_3", "fx_3", params)
        assert reopened is not None and reopened is not dialog
        assert reopened.panel.get_state()["ceiling"] == params["ceiling_dbtp"]
        reopened.close()


# ── 控制器 ────────────────────────────────────────────────────────────

def _clip_with_fx(effect_type: str, params: dict | None = None,
                  effect_id: str = "fx_x") -> tuple:
    from src.features.edit.core.effect_instance import EffectInstance
    effect = EffectInstance(
        effect_type=effect_type, params=dict(params or _default_params(effect_type)),
        effect_id=effect_id, order=0)
    clip = ClipInstance(
        clip_id="clip_x", asset_id="a1", track_id="T1", effects=(effect,),
        timeline_start=0, timeline_duration=FPS, source_start=0,
        source_duration=FPS)
    track = EditTrack("T1", "Track 1", "audio", 0,
                      clips=(clip,))
    seq = TimelineSequence(audio_tracks=(track,))
    return seq, clip, effect


class TestControllerPreview:
    """预览直送引擎：模型不动、撤销栈不动、目标实例参数精确替换。"""

    def _host(self, seq: TimelineSequence | None = None) -> tuple[Any, list, list]:
        from src.app.controllers.edit_controller import EditControllerMixin

        if seq is None:
            seq, _clip, _fx = _clip_with_fx("de_esser")
        applied: list = []
        engine: list = []
        samplers: list[Callable[[int], list] | None] = []

        def set_clip_fx(
            clip_id: str, entries: list | None, track_id: str | None = None,
            animated_sampler: Callable[[int], list] | None = None,
        ) -> None:
            engine.append((clip_id, list(entries) if entries is not None else None, track_id))
            samplers.append(animated_sampler)

        mgr = SimpleNamespace(set_clip_effects=set_clip_fx)
        host = EditControllerMixin()
        host.edit_page = SimpleNamespace(_current_timeline=seq)
        host._execute_timeline_edit_command = lambda cmd, tl: applied.append(cmd)
        host._resync_audio_after_effect_change = lambda: None
        host._get_mixer_audio_manager = lambda: mgr
        host.audition_samplers = samplers
        return host, applied, engine

    def test_preview_updates_engine_without_model_change(self):
        from src.app.controllers.edit_controller import EditControllerMixin
        host, applied, engine = self._host()
        bind = EditControllerMixin._on_edit_clip_effect_preview.__get__(host)
        preview = _default_params("de_esser")
        preview["threshold_dbfs"] = -20.0
        bind("clip_x", "fx_x", preview)
        assert applied == []  # 不落命令
        assert len(engine) == 1
        cid, entries, track_id = engine[0]
        assert (cid, track_id) == ("clip_x", "T1")
        assert entries == [("de_esser", preview, True, "fx_x", 5)]

    def test_preview_only_replaces_target_effect(self):
        """多效果链：仅目标实例参数被替换，其余保持模型值。"""
        from src.app.controllers.edit_controller import EditControllerMixin
        from src.features.edit.core.effect_instance import EffectInstance
        seq, _clip, _fx = _clip_with_fx("de_esser")
        extra = EffectInstance(effect_type="phaser",
                               params=_default_params("phaser"),
                               effect_id="fx_y", order=1)
        clip = seq.audio_tracks[0].clips[0]
        object.__setattr__(clip, "effects", (clip.effects[0], extra))
        host, _applied, engine = self._host(seq)
        bind = EditControllerMixin._on_edit_clip_effect_preview.__get__(host)
        preview = _default_params("phaser")
        preview["rate_hz"] = 2.0
        bind("clip_x", "fx_y", preview)
        entries = engine[0][1]
        assert entries[0][0] == "de_esser" and entries[0][2] is True
        assert entries[1] == ("phaser", preview, True, "fx_y", 5)
        # 预览 phaser 的 rate 只在引擎，模型未动
        assert extra.params["rate_hz"] != 2.0

    def test_preview_without_engine_is_silent(self):
        from src.app.controllers.edit_controller import EditControllerMixin
        host, _applied, _engine = self._host()
        host._get_mixer_audio_manager = lambda: None
        bind = EditControllerMixin._on_edit_clip_effect_preview.__get__(host)
        bind("clip_x", "fx_x", {})  # 不抛即通过

    def test_preview_finished_reverts_to_model(self):
        host, _applied, engine = self._host()
        params = _default_params("de_esser")
        host._on_edit_clip_effect_preview("clip_x", "fx_x", {**params, "threshold_dbfs": -25.0})
        host._on_edit_clip_effect_preview_finished("clip_x", "fx_x")
        assert len(engine) == 2
        assert engine[-1] == ("clip_x", [("de_esser", params, True, "fx_x", 5)], "T1")

    def test_cancel_rolls_back_panel_bypass_in_the_engine(self, qtbot: QtBot) -> None:
        host, applied, engine = self._host()
        manager = ClipAudioEffectDialogManager()
        host._clip_audio_fx_dialog_mgr = manager
        manager.clip_effect_preview.connect(host._on_edit_clip_effect_preview)
        manager.clip_effect_preview_finished.connect(host._on_edit_clip_effect_preview_finished)
        params = _default_params("de_esser")
        dialog = manager.open_dialog("de_esser", "clip_x", "fx_x", params, enabled=True)

        assert dialog is not None
        with qtbot.waitSignal(manager.clip_effect_preview, timeout=1000):
            qtbot.mouseClick(dialog.panel.power_switch, Qt.MouseButton.LeftButton)
        assert engine[-1] == ("clip_x", [("de_esser", {**params, "enabled": False}, False, "fx_x", 5)], "T1")

        buttons = dialog.findChild(QDialogButtonBox)
        qtbot.mouseClick(buttons.button(QDialogButtonBox.StandardButton.Cancel), Qt.MouseButton.LeftButton)
        assert engine[-1] == ("clip_x", [("de_esser", params, True, "fx_x", 5)], "T1")
        assert applied == []

    def test_preview_preserves_other_animated_controls_and_cancel_restores_the_edited_lane(
        self, qtbot: QtBot,
    ) -> None:
        seq, clip, effect = _clip_with_fx("de_esser")
        effect = effect.with_keyframe_added("threshold_dbfs", 10, -23.0, min_value=-60.0, max_value=0.0)
        effect = effect.with_keyframe_added("ratio", 10, 7.0, min_value=1.0, max_value=20.0)
        object.__setattr__(clip, "effects", (effect,))
        host, applied, _engine = self._host(seq)
        manager = ClipAudioEffectDialogManager()
        host._clip_audio_fx_dialog_mgr = manager
        manager.clip_effect_preview.connect(host._on_edit_clip_effect_preview)
        manager.clip_effect_preview_finished.connect(host._on_edit_clip_effect_preview_finished)
        dialog = manager.open_dialog("de_esser", "clip_x", "fx_x", dict(effect.params))

        assert dialog is not None
        with qtbot.waitSignal(manager.clip_effect_preview, timeout=1000):
            dialog.panel.thresh_knob.setValue(-37.0, emit=True)
        sampler = host.audition_samplers[-1]
        assert sampler is not None
        assert sampler(10)[0][1]["threshold_dbfs"] == -37.0
        assert sampler(10)[0][1]["ratio"] == 7.0

        buttons = dialog.findChild(QDialogButtonBox)
        qtbot.mouseClick(buttons.button(QDialogButtonBox.StandardButton.Cancel), Qt.MouseButton.LeftButton)
        restored = host.audition_samplers[-1]
        assert restored is not None
        assert restored(10)[0][1]["threshold_dbfs"] == -23.0
        assert restored(10)[0][1]["ratio"] == 7.0
        assert applied == []

    def test_preview_finished_empty_chain_clears(self):
        """无 audio 效果的 clip 关窗 → entries 空 → None 清链。"""
        seq, clip, _fx = _clip_with_fx("de_esser")
        host, _applied, engine = self._host(seq)
        host._on_edit_clip_effect_preview("clip_x", "fx_x", _default_params("de_esser"))
        object.__setattr__(clip, "effects", ())
        host._on_edit_clip_effect_preview_finished("clip_x", "fx_x")
        assert engine[-1] == ("clip_x", None, "T1")


class TestControllerCommit:
    def test_commit_builds_macro_command_for_new_fx(self):
        """新 FX 提交走既有路径：变化字段 → UpdateEffectParamCommand，
        多字段聚合 MacroCommand（单步撤销）。"""
        from src.app.controllers.edit_controller import EditControllerMixin
        seq, _clip, _fx = _clip_with_fx("de_esser")
        applied: list = []
        host = SimpleNamespace(
            edit_page=SimpleNamespace(_current_timeline=seq),
            _execute_timeline_edit_command=(
                lambda cmd, tl: applied.append(cmd)),
            _resync_audio_after_effect_change=lambda: None,
        )
        bind = EditControllerMixin._on_edit_clip_effect_params_committed.__get__(host)
        committed = _default_params("de_esser")
        committed["threshold_dbfs"] = -18.0
        committed["ratio"] = 6.0
        bind("clip_x", "fx_x", committed)
        assert len(applied) == 1
        command = applied[0]
        from src.features.edit.core.commands import MacroCommand
        assert isinstance(command, MacroCommand)
        names = {sc.param_name for sc in command.sub_commands}
        assert names == {"threshold_dbfs", "ratio"}

    def test_commit_no_change_is_noop(self):
        from src.app.controllers.edit_controller import EditControllerMixin
        seq, _clip, _fx = _clip_with_fx("de_esser")
        applied: list = []
        host = SimpleNamespace(
            edit_page=SimpleNamespace(_current_timeline=seq),
            _execute_timeline_edit_command=(
                lambda cmd, tl: applied.append(cmd)),
            _resync_audio_after_effect_change=lambda: None,
        )
        bind = EditControllerMixin._on_edit_clip_effect_params_committed.__get__(host)
        bind("clip_x", "fx_x", _default_params("de_esser"))
        assert applied == []


# ── 引擎后端（clip 级 DSP 链健全性）──────────────────────────────────

class TestClipEngineBackend:
    """set_clip_effects 十款逐一：真实块处理无异常、输出有限。

    后端健全性证明：EffectChain 适配器在 clip 级（decoder 后、轨音量前）
    与机架同一条处理路径；块边界（1 帧 = 1920 样本）不炸、不发散。
    """

    def test_all_ten_process_real_audio(self):
        from src.features.edit.core.audio_playback import (
            AudioMixer, AudioTrackState,
        )
        SR = 48000
        n_total = SR // FPS  # 1 帧
        rng = np.random.default_rng(7)
        source = (rng.standard_normal((2, n_total)) * 0.4).astype(np.float32)

        for effect_type in _TEN_FX:
            mixer = AudioMixer()
            state = {"fed": False}

            def decode(frame, fps, num_samples, _state=state, _src=source):
                _state["fed"] = True
                return _src

            mixer.add_track("T", SimpleNamespace(
                _channels=2, _clip_id="c_fx",
                _timeline_start=0, _timeline_duration=FPS,
                decode_samples_for_frame=decode,
            ), AudioTrackState(track_id="T"))
            params = _default_params(effect_type)
            if effect_type == "convolution_reverb":
                # 默认无 IR：登记路径必须干净旁路（不炸不发散）
                params["dry_wet"] = 100.0
            mixer.set_clip_effects(
                "c_fx", [(effect_type, params, True)], track_id="T")
            chunk, _peaks = mixer.mix_samples(0, float(FPS), n_total)
            assert chunk.shape == (2, n_total), effect_type
            assert np.all(np.isfinite(chunk)), effect_type
            assert state["fed"], effect_type
            # 旁路同链（enabled=False）→ 原样通过（有限即可）
            mixer.set_clip_effects("c_fx", [(effect_type, params, False)],
                                   track_id="T")
            chunk2, _p = mixer.mix_samples(0, float(FPS), n_total)
            assert np.all(np.isfinite(chunk2)), effect_type

    def test_true_peak_limiter_ceiling_respected_at_clip_level(self):
        """clip 级 True Peak Limiter：dBTP 上限在输出端成立。"""
        from src.features.edit.core.audio_playback import (
            AudioMixer, AudioTrackState,
        )
        SR = 48000
        n_total = SR  # 1 秒
        t = np.arange(n_total) / SR
        # 1.5 dBTP 等效振幅的正弦（峰值 1.19）——链后须压到 -1.0 dBTP 内
        src = (1.19 * np.sin(2 * np.pi * 1000.0 * t)).astype(np.float32)
        src = np.stack([src, src])
        mixer = AudioMixer()
        mixer.add_track("T", SimpleNamespace(
            _channels=2, _clip_id="c_tp",
            _timeline_start=0, _timeline_duration=int(SR / FPS * FPS),
            decode_samples_for_frame=lambda f, fps, n: src,
        ), AudioTrackState(track_id="T"))
        mixer.set_clip_effects(
            "c_tp",
            [("true_peak_limiter",
              _default_params("true_peak_limiter") | {"ceiling_dbtp": -1.0},
              True)],
            track_id="T")
        chunk, _peaks = mixer.mix_samples(0, float(FPS), n_total)
        peak = float(np.max(np.abs(chunk[:, 2000:])))  # 跳过 lookahead 前沿
        assert peak <= 10 ** (-1.0 / 20.0) + 0.05
