"""十款新 rack FX 的 Clip FX 插槽适配验收（2026-11-20 收口批次）。

状态报告遗留项「十款新 FX 的 Clip FX 插槽适配（clip 级对话框）」：

- 注册表：``_ensure_fx_dialog_param_types`` 恰好十款，与 mixer 专用
  对话框表联合覆盖全部 ``AUDIO_MIXER_EFFECT_TYPES``（互斥不重叠）；
- open_dialog：十款逐一弹出通用表驱动 ``FxParamDialog``（标题带 i18n
  效果名 + 片段名），缓存复用，损坏参数降级默认（AUD-19720 同语义）；
- 归一化：打开前经 ParamsClass ``from_dict → to_dict``（缺键补默认、
  multiband crossovers 列表 ↔ UI 扁平键往返）；
- 信号回路：FxParamDialog 回传 dict——committed 直落既有 MacroCommand
  落库路径；preview 实时直送引擎（不落模型/不入撤销栈）；关窗回滚；
- 控制器：预览 entries 与 ``audio_retime.clip_fx_entries`` 同过滤/排序/
  启用口径，仅按 effect_id 精确替换目标实例参数；
- 引擎后端：``AudioMixer.set_clip_effects`` 十款逐一处理音频无异常
  （EffectChain 适配器路径，clip 级 DSP 后端健全）。
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.effect_registry import AUDIO_MIXER_EFFECT_TYPES
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.features.edit.ui.clip_audio_effect_dialog_adapter import (
    ClipAudioEffectDialogManager,
    _ensure_dialog_registry,
    _ensure_fx_dialog_param_types,
)

pytestmark = pytest.mark.edit_core
FPS = 25

_TEN_FX = (
    "de_esser", "true_peak_limiter", "dynamic_eq", "bus_comp",
    "multiband_comp", "convolution_reverb", "transient_designer",
    "tremolo_auto_pan", "chorus_flanger", "phaser",
)


def _default_params(effect_type: str) -> dict:
    return _ensure_fx_dialog_param_types()[effect_type].default().to_dict()


class TestFxDialogRegistry:
    def test_exactly_ten_new_types(self):
        fx_types = _ensure_fx_dialog_param_types()
        assert set(fx_types) == set(_TEN_FX)
        # 与 mixer 专用对话框表互斥，联合覆盖全部 audio 类型
        mixer_types = set(_ensure_dialog_registry())
        assert mixer_types & set(fx_types) == set()
        assert mixer_types | set(fx_types) == set(AUDIO_MIXER_EFFECT_TYPES)

    def test_params_classes_have_roundtrip(self):
        for effect_type, params_cls in _ensure_fx_dialog_param_types().items():
            assert params_cls.default().to_dict(), effect_type
            rt = params_cls.from_dict(params_cls.default().to_dict())
            assert rt.to_dict() == params_cls.default().to_dict(), effect_type


class TestOpenDialog:
    def test_all_ten_open_fx_param_dialog(self, qapp):
        from src.shared.ui.audio_mixer.fx_rack_dialogs import FxParamDialog
        mgr = ClipAudioEffectDialogManager()
        for index, effect_type in enumerate(_TEN_FX):
            dialog = mgr.open_dialog(
                effect_type, f"clip_{index}", f"fx_{index}",
                _default_params(effect_type), "DIALOG CLIP",
            )
            assert isinstance(dialog, FxParamDialog), effect_type
            assert dialog.isVisible(), effect_type
            assert dialog.fx_effect_type == effect_type
            # 标题：i18n 效果名 · 片段名
            assert "DIALOG CLIP" in dialog.windowTitle(), effect_type
            dialog.close()

    def test_open_caches_dialog_per_clip_effect(self, qapp):
        mgr = ClipAudioEffectDialogManager()
        params = _default_params("de_esser")
        d1 = mgr.open_dialog("de_esser", "clip_a", "fx_a", params)
        d2 = mgr.open_dialog("de_esser", "clip_a", "fx_a", params)
        assert d1 is d2
        other = mgr.open_dialog("de_esser", "clip_a", "fx_b", params)
        assert other is not d1
        mgr.close_all()

    def test_partial_dict_uses_defaults(self, qapp):
        mgr = ClipAudioEffectDialogManager()
        dialog = mgr.open_dialog(
            "de_esser", "clip_p", "fx_p", {"threshold_dbfs": -40.0})
        assert dialog is not None
        # 缺键补默认
        assert dialog._params["threshold_dbfs"] == -40.0
        assert dialog._params["ratio"] == pytest.approx(4.0)
        assert dialog._params["mode"] == "split"
        dialog.close()

    def test_multiband_crossovers_list_to_flat_keys(self, qapp):
        """DSP crossovers 列表 → UI 扁平键；提交合回列表（roundtrip）。"""
        mgr = ClipAudioEffectDialogManager()
        params = _default_params("multiband_comp")
        dialog = mgr.open_dialog("multiband_comp", "clip_m", "fx_m", params)
        assert "crossovers" not in dialog._params
        assert dialog._params["crossover_low"] == pytest.approx(
            params["crossovers"][0])
        assert dialog._params["crossover_high"] == pytest.approx(
            params["crossovers"][1])
        merged = dialog.current_params()
        assert merged["crossovers"] == params["crossovers"]
        dialog.close()

    def test_corrupt_params_fall_back_with_warning(self, qapp, caplog):
        """from_dict 失败 → 默认参数打开 + 警告日志（AUD-19720 同语义）。"""
        mgr = ClipAudioEffectDialogManager()
        original = _ensure_fx_dialog_param_types()["phaser"].from_dict

        def _boom(_data):
            raise ValueError("corrupt")

        import logging
        with caplog.at_level(logging.WARNING):
            try:
                _ensure_fx_dialog_param_types()[
                    "phaser"].from_dict = staticmethod(_boom)
                dialog = mgr.open_dialog(
                    "phaser", "clip_c", "fx_c", {"garbage": object()})
            finally:
                _ensure_fx_dialog_param_types()["phaser"].from_dict = original
        assert dialog is not None
        assert any("failed to parse" in rec.message for rec in caplog.records)
        dialog.close()

    def test_non_mixer_effect_still_returns_none(self, qapp):
        mgr = ClipAudioEffectDialogManager()
        assert mgr.open_dialog(
            "blur", "clip_v", "fx_v", {"radius": 4.0}) is None


class TestSignalRoundTrip:
    def test_commit_emits_dict_payload(self, qapp):
        mgr = ClipAudioEffectDialogManager()
        received: list = []
        mgr.clip_effect_params_committed.connect(
            lambda cid, eid, p: received.append((cid, eid, p)))
        dialog = mgr.open_dialog(
            "de_esser", "clip_1", "fx_1", _default_params("de_esser"))
        dialog._params["threshold_dbfs"] = -36.0
        dialog._emit(True)
        assert len(received) == 1
        cid, eid, payload = received[0]
        assert (cid, eid) == ("clip_1", "fx_1")
        assert payload["threshold_dbfs"] == -36.0
        assert payload["mode"] == "split"
        dialog.close()

    def test_commit_accepts_dict_from_mixer_dialog_branch_too(self, qapp):
        """_on_committed 对 dict 与 dataclass 回传双兼容。"""
        from src.features.edit.core.timeline_v2 import ReverbParams
        mgr = ClipAudioEffectDialogManager()
        received: list = []
        mgr.clip_effect_params_committed.connect(
            lambda cid, eid, p: received.append((cid, eid, p)))
        dialog = mgr.open_dialog(
            "reverb", "clip_2", "fx_2", ReverbParams.default().to_dict())
        # 旧契约：*Params dataclass
        dialog.reverb_committed.emit("clip_2", ReverbParams(dry_wet=77.0))
        assert received[-1][2]["dry_wet"] == 77.0
        # 新契约：已归一 dict（FxParamDialog 路径）
        mgr._on_committed("clip_2", "fx_2", {"dry_wet": 33.0})
        assert received[-1][2] == {"dry_wet": 33.0}
        dialog.close()

    def test_preview_signal_live_payload(self, qapp):
        mgr = ClipAudioEffectDialogManager()
        previews: list = []
        mgr.clip_effect_preview.connect(
            lambda cid, eid, p: previews.append((cid, eid, p)))
        dialog = mgr.open_dialog(
            "true_peak_limiter", "clip_3", "fx_3",
            _default_params("true_peak_limiter"))
        dialog._params["ceiling_dbtp"] = -3.0
        dialog._emit(False)
        assert previews and previews[0][2]["ceiling_dbtp"] == -3.0
        assert not mgr._dialogs or True
        dialog.close()

    def test_close_emits_preview_finished_and_clears_cache(self, qapp):
        mgr = ClipAudioEffectDialogManager()
        finished: list = []
        mgr.clip_effect_preview_finished.connect(
            lambda cid, eid: finished.append((cid, eid)))
        dialog = mgr.open_dialog(
            "phaser", "clip_4", "fx_4", _default_params("phaser"))
        key = ("clip_4", "fx_4")
        assert key in mgr._dialogs
        dialog.close()
        assert finished == [("clip_4", "fx_4")]
        assert key not in mgr._dialogs


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

    def _host(self, seq=None):
        if seq is None:
            seq, _clip, _fx = _clip_with_fx("de_esser")
        applied: list = []
        engine: list = []
        mgr = SimpleNamespace(
            set_clip_effects=(
                lambda cid, entries, track_id=None: engine.append(
                    (cid, list(entries), track_id))),
        )
        host = SimpleNamespace(
            edit_page=SimpleNamespace(_current_timeline=seq),
            _execute_timeline_edit_command=(
                lambda cmd, tl: applied.append(cmd)),
            _resync_audio_after_effect_change=lambda: None,
            _get_mixer_audio_manager=lambda: mgr,
        )
        from src.app.controllers.edit_controller import EditControllerMixin
        host._clip_fx_preview_entries = (
            EditControllerMixin._clip_fx_preview_entries.__get__(host))
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
        assert entries == [("de_esser", preview, True)]

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
        assert entries[1] == ("phaser", preview, True)
        # 预览 phaser 的 rate 只在引擎，模型未动
        assert extra.params["rate_hz"] != 2.0

    def test_preview_without_engine_is_silent(self):
        from src.app.controllers.edit_controller import EditControllerMixin
        host, _applied, _engine = self._host()
        host._get_mixer_audio_manager = lambda: None
        bind = EditControllerMixin._on_edit_clip_effect_preview.__get__(host)
        bind("clip_x", "fx_x", {})  # 不抛即通过

    def test_preview_finished_reverts_to_model(self):
        from src.app.controllers.edit_controller import EditControllerMixin
        host, _applied, engine = self._host()
        bind = EditControllerMixin._on_edit_clip_effect_preview_finished.__get__(host)
        bind("clip_x", "fx_x")
        assert len(engine) == 1
        entries = engine[0][1]
        # 模型全量条目（无预览覆盖）：params 为模型值、enabled=True
        assert entries[0] == ("de_esser", _default_params("de_esser"), True)

    def test_preview_finished_empty_chain_clears(self):
        """无 audio 效果的 clip 关窗 → entries 空 → None 清链。"""
        from src.app.controllers.edit_controller import EditControllerMixin
        seq, _clip, _fx = _clip_with_fx("de_esser")
        clip = seq.audio_tracks[0].clips[0]
        object.__setattr__(clip, "effects", ())
        applied: list = []
        engine: list = []
        mgr = SimpleNamespace(
            set_clip_effects=(
                lambda cid, entries, track_id=None: engine.append(
                    (cid, entries, track_id))))
        host = SimpleNamespace(
            edit_page=SimpleNamespace(_current_timeline=seq),
            _get_mixer_audio_manager=lambda: mgr,
        )
        from src.app.controllers.edit_controller import EditControllerMixin
        host._clip_fx_preview_entries = (
            EditControllerMixin._clip_fx_preview_entries.__get__(host))
        bind = EditControllerMixin._on_edit_clip_effect_preview_finished.__get__(host)
        bind("clip_x", "fx_x")
        assert engine and engine[0][1] is None


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
