"""P1（2026-11-19 计划第三阶段）新 FX 的 UI/注册层验收（边界 4 收口）。

- timeline_v2 参数注册表：10 款新 FX default→to_dict→from_dict 往返一致；
  fx_versioning 判 supported（修前 unsupported → 导出预检把实例当
  opaque payload 不渲染）；
- effect_registry：audio 类目注册 + 默认参数缓存 + AUDIO_MIXER_EFFECT_TYPES；
- 机架：添加菜单/插入集合/默认参数工厂覆盖 10 款；通用参数对话框
  构造、预览/提交信号、R06 回填（不发信号）；
- 卷积混响 ir_path 装载（缓存去重）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

NEW_FX = ("de_esser", "true_peak_limiter", "dynamic_eq", "bus_comp",
          "multiband_comp", "convolution_reverb", "transient_designer",
          "tremolo_auto_pan", "chorus_flanger", "phaser")

pytestmark = pytest.mark.edit_core


class TestParamRegistry:
    def test_roundtrip_and_support(self):
        from src.features.edit.core.timeline_v2 import _ensure_effect_param_registry
        from src.features.edit.core.fx_versioning import effect_type_support
        registry = _ensure_effect_param_registry()
        for effect_type in NEW_FX:
            cls = registry[effect_type]
            params = cls.default().to_dict()
            assert cls.from_dict(params).to_dict() == params, effect_type
            status, _reason = effect_type_support(effect_type)
            assert status == "supported", effect_type
            assert params.get("enabled") is True, effect_type

    def test_validate_effect_params_normalizes(self):
        from src.features.edit.core.timeline_v2 import validate_effect_params
        cleaned, diags = validate_effect_params(
            "de_esser", {"threshold_dbfs": "bad", "ratio": 6.0})
        assert cleaned["threshold_dbfs"] == -32.0  # 坏值回默认
        assert cleaned["ratio"] == 6.0
        assert any(d[0] == "threshold_dbfs" for d in diags)

    def test_multiband_nested_bands_roundtrip(self):
        from src.features.edit.core.timeline_v2 import MultibandCompParams
        params = MultibandCompParams.default()
        d = params.to_dict()
        assert d["crossovers"] == [180.0, 4200.0]
        assert len(d["bands"]) == 3 and d["bands"][1]["ratio"] == 2.5
        rt = MultibandCompParams.from_dict(d).to_dict()
        assert rt == d


class TestEffectRegistry:
    def test_audio_entries_and_defaults(self):
        from src.features.edit.core.effect_registry import (
            get_effect_registry, AUDIO_MIXER_EFFECT_TYPES,
            _get_audio_default_params,
        )
        registry = get_effect_registry()
        defaults = _get_audio_default_params()
        for effect_type in NEW_FX:
            definition = registry.get_definition(effect_type)
            assert definition is not None, effect_type
            assert definition.category == "audio", effect_type
            assert effect_type in AUDIO_MIXER_EFFECT_TYPES, effect_type
            assert defaults.get(effect_type), effect_type

    def test_mixer_default_params_factory(self):
        from src.shared.ui.audio_mixer.track_mixer_widget import (
            _effect_default_params, TrackMixerWidget,
        )
        insert_types = TrackMixerWidget._INSERT_EFFECT_TYPES
        for effect_type in NEW_FX:
            params = _effect_default_params(effect_type)
            assert params and params.get("enabled") is True, effect_type
            assert effect_type in insert_types, effect_type


class TestFxParamDialog:
    @pytest.fixture
    def dialog_env(self, qapp):
        from src.shared.ui.audio_mixer.fx_rack_dialogs import FxParamDialog
        return FxParamDialog

    @pytest.mark.parametrize("effect_type", list(NEW_FX))
    def test_constructs_and_emits(self, dialog_env, qtbot, effect_type):
        from src.features.edit.core.timeline_v2 import _ensure_effect_param_registry
        params = _ensure_effect_param_registry()[effect_type].default().to_dict()
        dlg = dialog_env("t1", "Dialogue", "inst-1", effect_type, params)
        assert dlg.windowTitle()
        received_preview: list[dict] = []
        received_commit: list[dict] = []
        dlg.fx_preview.connect(lambda _tid, p: received_preview.append(dict(p)))
        dlg.fx_committed.connect(lambda _tid, p: received_commit.append(dict(p)))
        dlg.set_current_params(params)  # R06 回填：不发信号
        assert not received_preview and not received_commit

        # 滑条拖动 → preview；松手 → committed
        from src.shared.ui.audio_mixer.fx_rack_dialogs import field_specs_for
        specs = field_specs_for(effect_type)
        float_specs = [s for s in specs if s.kind == "float"]
        if float_specs:
            spec = float_specs[0]
            slider = dlg._widgets[spec.key][0]
            slider.setValue(min(slider.maximum(), slider.value() + 100))
            assert received_preview, effect_type
            with qtbot.waitSignal(dlg.fx_committed, timeout=2000):
                slider.setSliderPosition(slider.maximum())
                slider.releaseKeyboard()
                slider.sliderReleased.emit()
            assert received_commit, effect_type

        # 枚举/开关直接提交
        combo_specs = [s for s in specs if s.kind == "combo"]
        if combo_specs:
            combo = dlg._widgets[combo_specs[0].key]
            combo.setCurrentIndex(1 if combo.count() > 1 else 0)
            assert received_commit, effect_type

    def test_add_effect_menu_contains_new_fx(self, qapp):
        from PyQt6.QtWidgets import QMenu
        from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget
        widget = TrackMixerWidget("t1", "Dialogue", "A1")
        menu = QMenu(widget)
        widget._populate_add_effect_menu(menu)
        texts = {action.text() for action in menu.actions()}
        for effect_type in NEW_FX:
            from src.shared.ui.audio_mixer.track_mixer_widget import (
                _effect_display_name,
            )
            assert _effect_display_name(effect_type) in texts, effect_type

    def test_generic_editor_roundtrip_through_chain(self, qapp):
        """通用对话框提交 → 机架链实例参数更新 → committed 信号。"""
        from dataclasses import replace as dc_replace
        from PyQt6.QtCore import Qt
        from src.features.edit.core.timeline_v2 import (
            EffectChainParams, TrackEffectInstance, _ensure_effect_param_registry,
        )
        from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget
        widget = TrackMixerWidget("t1", "Dialogue", "A1")
        inst = TrackEffectInstance(effect_type="de_esser", params={},
                                   instance_id="fx-1", order=1, panel="fx")
        widget._effect_chain = EffectChainParams(effects=(inst,))
        committed: list[EffectChainParams] = []
        widget.track_effects_committed.connect(
            lambda _tid, chain: committed.append(chain))
        params = _ensure_effect_param_registry()["de_esser"].default().to_dict()
        params["ratio"] = 8.0
        widget._on_fx_param_edited(inst, params, commit=True)
        assert committed
        updated = committed[-1].effects[0]
        assert updated.params["ratio"] == 8.0
        # 实例已删除 → 编辑被守卫丢弃
        widget._effect_chain = EffectChainParams(effects=())
        widget._on_fx_param_edited(inst, params, commit=True)
        assert len(committed) == 1


class TestConvolutionIrPath:
    def _make_wav(self, tmp_path: Path) -> str:
        import wave
        path = str(tmp_path / "ir.wav")
        rng = np.random.default_rng(7)
        pcm = (rng.standard_normal(4800) * 0.3).astype(np.float32)
        data = (np.clip(pcm, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        with wave.open(path, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(48000)
            wav.writeframes(data)
        return path

    def test_ir_path_loads_and_activates(self, tmp_path):
        from src.features.edit.core.audio_effects.convolution_reverb import (
            ConvolutionReverbEffect,
        )
        fx = ConvolutionReverbEffect(48000.0)
        assert fx.is_active is False  # 无 IR 不激活
        fx.set_params({"enabled": True, "ir_path": self._make_wav(tmp_path)})
        assert fx.is_active is True
        out = fx.process(np.eye(2, 64, dtype=np.float32) * 0.5)
        assert out.shape == (2, 64)

    def test_ir_path_cached(self, tmp_path):
        from src.features.edit.core.audio_effects.convolution_reverb import (
            ConvolutionReverbEffect,
        )
        fx = ConvolutionReverbEffect(48000.0)
        path = self._make_wav(tmp_path)
        fx.set_params({"ir_path": path})
        conv_before = fx._conv
        for _ in range(50):  # 自动化逐块 set_params：不得重复读盘
            fx.set_params({"ir_path": path})
        assert fx._conv is conv_before
