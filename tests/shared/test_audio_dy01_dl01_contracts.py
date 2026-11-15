"""DY-01 / DL-01 契约回归（音频成熟化计划 阶段B/C）。

DY-01：Dynamics 总电源只翻转实例级旁路，模块旁路组合保持独立；
预设保留自身模块状态；Reset 用产品默认值（模块关闭）。
DL-01：Delay Link 开关一个手势恰好一次 committed；输入选择矩阵
为 Stereo/Mono/Left/Right（旧 LCR/Quad/5.x 迁移为 Stereo）。
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_dsp import DelayEffect
from src.features.edit.core.timeline_v2 import (
    DelayParams,
    EffectChainParams,
    TrackEffectInstance,
)


# ══════════════════════════ DY-01：Dynamics 对话框 ══════════════════════════


def _dyn_chain(comp_enabled=True, eg_enabled=False, lim_enabled=False,
               instance_enabled=True):
    """三实例 Dynamics 链：默认 [EG off, Comp on, Lim off]（§1.3 场景）。"""
    from src.features.edit.core.timeline_v2 import (
        CompressorParams, ExpanderParams, LimiterParams,
    )
    eg = ExpanderParams().to_dict()
    eg["enabled"] = eg_enabled
    comp = CompressorParams().to_dict()
    comp["enabled"] = comp_enabled
    lim = LimiterParams().to_dict()
    lim["enabled"] = lim_enabled
    return EffectChainParams(effects=(
        TrackEffectInstance(
            effect_type="expander", params=eg,
            enabled=instance_enabled, order=0, panel="dynamics",
        ),
        TrackEffectInstance(
            effect_type="compressor", params=comp,
            enabled=instance_enabled, order=1, panel="dynamics",
        ),
        TrackEffectInstance(
            effect_type="limiter", params=lim,
            enabled=instance_enabled, order=2, panel="dynamics",
        ),
    ))


class TestDy01MasterPowerIndependent:
    @pytest.fixture
    def dialog(self, qapp):
        from src.shared.ui.audio_mixer.dynamics_dialog import DynamicsDialog
        dlg = DynamicsDialog(track_id="t1", track_name="Voice", chain=_dyn_chain())
        return dlg

    def _module_flags(self, dlg):
        st = dlg._panel_state()
        return (bool(st["eg"].get("enabled")),
                bool(st["comp"].get("enabled")),
                bool(st["lim"].get("enabled")))

    def _instance_flags(self, dlg):
        return [bool(inst.enabled) for inst in dlg._chain.effects]

    def test_power_off_on_preserves_module_combination(self, dialog):
        """§1.3：模块 [False,True,False]，总电源关→开后仍为 [False,True,False]。"""
        assert self._module_flags(dialog) == (False, True, False)
        assert self._instance_flags(dialog) == [True, True, True]

        # PowerToggleSwitch.setChecked 默认不 emit（用户点击路径才发 toggled）
        dialog.power_switch.setChecked(False, emit=True)
        assert self._instance_flags(dialog) == [False, False, False]
        # 模块级组合不动
        assert self._module_flags(dialog) == (False, True, False)

        dialog.power_switch.setChecked(True, emit=True)
        assert self._instance_flags(dialog) == [True, True, True]
        assert self._module_flags(dialog) == (False, True, False), (
            "master off→on must not flip module bypass flags"
        )

    def test_flat_bypass_preset_keeps_modules_off(self, dialog):
        """§1.3：总电源开时应用 Flat Bypass，三模块 params.enabled 仍 False。"""
        flat_idx = next(i for i, p in enumerate(dialog._presets)
                        if p["name"] == "Flat Bypass")
        assert dialog._panel_state()["enabled"] is True
        dialog._on_preset_selected(flat_idx)
        assert self._module_flags(dialog) == (False, False, False), (
            "Flat Bypass preset must keep its own module off states"
        )

    def test_preset_keeps_own_module_states(self, dialog):
        """非对称预设（Music Ducking：EG off、Comp/Lim on）原样应用。"""
        duck_idx = next(i for i, p in enumerate(dialog._presets)
                        if p["name"] == "Music Ducking (Sidechain)")
        dialog._on_preset_selected(duck_idx)
        assert self._module_flags(dialog) == (False, True, True)

    def test_reset_disables_modules_keeps_master(self, dialog):
        """Reset 依产品默认值：模块参数回 Flat、模块开关关闭、总电源不变。"""
        dialog.power_switch.setChecked(True)
        assert dialog._panel_state()["enabled"] is True
        dialog.reset_dynamics()
        assert self._module_flags(dialog) == (False, False, False)
        assert dialog._panel_state()["enabled"] is True

    def test_single_commit_per_power_toggle(self, dialog):
        """一次总电源手势恰好一次 committed。"""
        committed = []
        dialog.dynamics_committed.connect(
            lambda _tid, chain: committed.append(chain))
        dialog.power_switch.setChecked(False, emit=True)
        assert len(committed) == 1


# ══════════════════════════ DL-01：Delay 对话框 ══════════════════════════


class TestDl01LinkSingleCommit:
    def _make_dialog(self, qapp):
        from src.shared.ui.audio_mixer.delay_dialog import DelayDialog
        dlg = DelayDialog(track_id="t1", track_name="Echo", instance_id="d1")
        return dlg

    def test_link_toggle_commits_exactly_once(self, qapp):
        """§1.3：Link 开一次实际发出恰好 1 条 committed。"""
        dlg = self._make_dialog(qapp)
        committed = []
        dlg.delay_committed.connect(lambda _tid, p: committed.append(p))
        dlg.btn_link.setChecked(True)
        dlg._on_link_toggled(True)
        assert len(committed) == 1, (
            f"Link toggle emitted {len(committed)} commits (double-commit bug)"
        )
        # Link 开启把右声道对齐左声道
        st = dlg._st()
        assert st.right_delay == pytest.approx(st.left_delay)
        assert st.right_feedback == pytest.approx(st.left_feedback)

    def test_input_format_menu_uses_real_matrix(self, qapp):
        """菜单枚举为 Stereo/Mono/Left/Right（移除从未实现的多声道假能力）。"""
        dlg = self._make_dialog(qapp)
        items = [dlg.format_combo.itemText(i)
                 for i in range(dlg.format_combo.count())]
        assert items == ["Stereo", "Mono", "Left", "Right"]

    def test_legacy_input_format_migrates_to_stereo(self, qapp):
        """旧工程 LCR/Quad/5.1 等枚举 → Stereo（声音不变，去掉假声明）。"""
        for legacy in ("LCR", "Quad", "5.0", "5.1", "7.1"):
            p = DelayParams.from_dict({"input_format": legacy})
            assert p.input_format == "Stereo"


# ══════════════════════════ DL-01：Delay DSP 输入选择 ══════════════════════════


class TestDl01DspInputSelection:
    SR = 48000.0

    def _delay(self, fmt):
        d = DelayEffect(self.SR)
        d.set_params({
            "left_delay": 20.0, "right_delay": 20.0,
            "left_feedback": 0.0, "right_feedback": 0.0,
            "dry_wet": 100.0, "level_db": 0.0,
            "input_format": fmt, "enabled": True,
        })
        return d

    def test_left_only_drives_both_lines_from_left(self):
        """Left：右声道内容不进延迟网络——L 冲激在 L/R 湿声都出现。"""
        d = self._delay("Left")
        x = np.zeros((2, 2048))
        x[0, 0] = 1.0  # 只有 L 有冲激
        y = d.process(x)
        peak_l = float(np.max(np.abs(y[0])))
        peak_r = float(np.max(np.abs(y[1])))
        assert peak_l > 0.05 and peak_r > 0.05  # 双线都由 L 驱动

        # R 冲激不进网络
        d2 = self._delay("Left")
        x2 = np.zeros((2, 2048))
        x2[1, 0] = 1.0
        y2 = d2.process(x2)
        assert float(np.max(np.abs(y2))) < 1e-9

    def test_right_only_drives_from_right(self):
        d = self._delay("Right")
        x = np.zeros((2, 2048))
        x[1, 0] = 1.0  # 只有 R 有冲激
        y = d.process(x)
        assert float(np.max(np.abs(y[0]))) > 0.05
        assert float(np.max(np.abs(y[1]))) > 0.05

    def test_mono_averages_channels(self):
        """Mono = (L+R)/2：反相 L/R 求平均为 0（单线无输出）。"""
        d = self._delay("Mono")
        n = 2048
        t = np.arange(n) / self.SR
        sig = np.sin(2.0 * np.pi * 1000.0 * t) * 0.5
        x = np.stack((sig, -sig))  # 反相 → mono 平均 = 0
        y = d.process(x)
        assert float(np.max(np.abs(y))) < 1e-6, (
            "Mono must be (L+R)/2 average (anti-phase cancels)"
        )
