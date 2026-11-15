"""R06：编辑器回填、实例身份与提交守卫回归（计划 §5 R06，2026-11-15 复审）。

复审确认的问题：
- 已开 Delay 窗在外部 set_effect_chain 同 id 改 800ms 后仍显示 100ms；
  旧窗再提交把旧参数写回（覆盖外部新值并进命令栈）。
- 外部清空链后 EQ 胶囊窗保留旧值；迟到提交把已移除的 strip_eq 实例
  重新追加（复活已删效果）。
- 8 类 FX 列表对话框字典在外部链变化时完全不被通知。

修复契约：同 id 同类型 → set_current_params 静默回填；实例移除/链清空
→ 关闭窗口；绑定过实例 id 的 EQ 窗迟到提交被拒绝。
"""

from __future__ import annotations

import pytest

from PyQt6.QtWidgets import QWidget

from src.features.edit.core.timeline_v2 import (
    DelayParams,
    EffectChainParams,
    StripEQParams,
    TrackEffectInstance,
)

pytestmark = pytest.mark.edit_core


class _FakeEffectDialog(QWidget):
    """模拟效果对话框：记录 set_current_params 与 deleteLater。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.backfilled: list = []
        self.delete_later_count = 0

    def set_current_params(self, params):
        self.backfilled.append(params)

    def deleteLater(self):  # noqa: N802 (Qt 命名)
        self.delete_later_count += 1
        super().deleteLater()


def _make_mixer(qapp):
    from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget
    return TrackMixerWidget(track_id="T1", track_name="Track 1", track_tag="A1")


def _delay_instance(delay_ms: float, iid: str) -> TrackEffectInstance:
    return TrackEffectInstance(
        effect_type="delay", instance_id=iid,
        params=DelayParams(left_delay=delay_ms,
                           right_delay=delay_ms).to_dict())


class TestExternalBackfill:
    def test_same_id_param_change_backfills_open_delay_dialog(self, qapp):
        """外部同 id 改 100ms → 800ms：已开窗口被回填新参数。"""
        w = _make_mixer(qapp)
        w.set_effect_chain(EffectChainParams(
            effects=(_delay_instance(100.0, "D1"),)))
        dlg = _FakeEffectDialog()
        dlg.show()
        if not hasattr(w, "_delay_dialogs"):
            w._delay_dialogs = {}
        w._delay_dialogs["D1"] = dlg

        w.set_effect_chain(EffectChainParams(
            effects=(_delay_instance(800.0, "D1"),)))

        assert len(dlg.backfilled) == 1
        assert dlg.backfilled[0]["left_delay"] == 800.0
        assert "D1" in w._delay_dialogs

    def test_removed_instance_closes_open_dialog(self, qapp):
        """外部删除实例：已开窗口被关闭并注销（旧窗不能复活效果）。"""
        w = _make_mixer(qapp)
        w.set_effect_chain(EffectChainParams(
            effects=(_delay_instance(100.0, "D1"),
                     _delay_instance(120.0, "D2"))))
        dlg = _FakeEffectDialog()
        dlg.show()
        if not hasattr(w, "_delay_dialogs"):
            w._delay_dialogs = {}
        w._delay_dialogs["D2"] = dlg

        w.set_effect_chain(EffectChainParams(
            effects=(_delay_instance(100.0, "D1"),)))  # D2 被外部移除

        assert "D2" not in w._delay_dialogs
        assert dlg.delete_later_count >= 1

    def test_cleared_chain_closes_all_open_dialogs(self, qapp):
        w = _make_mixer(qapp)
        w.set_effect_chain(EffectChainParams(
            effects=(_delay_instance(100.0, "D1"),)))
        dlg = _FakeEffectDialog()
        dlg.show()
        if not hasattr(w, "_delay_dialogs"):
            w._delay_dialogs = {}
        w._delay_dialogs["D1"] = dlg

        w.set_effect_chain(None)

        assert w._delay_dialogs == {}
        assert dlg.delete_later_count >= 1

    def test_type_change_closes_dialog(self, qapp):
        """同 id 类型变化（delay→reverb 实例替换）：旧类型窗口关闭。"""
        w = _make_mixer(qapp)
        w.set_effect_chain(EffectChainParams(
            effects=(_delay_instance(100.0, "X1"),)))
        dlg = _FakeEffectDialog()
        dlg.show()
        if not hasattr(w, "_delay_dialogs"):
            w._delay_dialogs = {}
        w._delay_dialogs["X1"] = dlg

        w.set_effect_chain(EffectChainParams(effects=(
            TrackEffectInstance(effect_type="reverb", instance_id="X1",
                                params={}),
        )))

        assert "X1" not in w._delay_dialogs
        assert dlg.delete_later_count >= 1

    def test_other_instance_dialog_untouched(self, qapp):
        """只回填/关闭相关实例：其他实例窗口不被误关。"""
        w = _make_mixer(qapp)
        w.set_effect_chain(EffectChainParams(
            effects=(_delay_instance(100.0, "D1"),
                     _delay_instance(120.0, "D2"))))
        dlg1 = _FakeEffectDialog()
        dlg2 = _FakeEffectDialog()
        dlg1.show()
        dlg2.show()
        if not hasattr(w, "_delay_dialogs"):
            w._delay_dialogs = {}
        w._delay_dialogs["D1"] = dlg1
        if not hasattr(w, "_delay_dialogs"):
            w._delay_dialogs = {}
        w._delay_dialogs["D2"] = dlg2

        w.set_effect_chain(EffectChainParams(
            effects=(_delay_instance(300.0, "D1"),)))  # D2 移除

        assert "D1" in w._delay_dialogs
        assert dlg1.backfilled and dlg1.backfilled[0]["left_delay"] == 300.0
        assert "D2" not in w._delay_dialogs
        assert dlg2.delete_later_count >= 1


class TestEqResurrectionGuard:
    def test_eq_dialog_does_not_resurrect_removed_instance(self, qapp):
        """外部移除 panel=eq 实例后，绑定过该实例的窗口迟到提交被拒绝
        （不重新追加 strip_eq 实例、不发 committed）。"""
        from src.features.edit.core.timeline_v2 import StripEQParams
        w = _make_mixer(qapp)
        eq_inst = TrackEffectInstance(
            effect_type="strip_eq", instance_id="EQ1", panel="eq",
            params=StripEQParams.default().to_dict())
        w.set_effect_chain(EffectChainParams(effects=(eq_inst,)))

        # 模拟已开 EQ 窗（绑定实例 id）
        dlg = _FakeEffectDialog()
        dlg._instance_id = "EQ1"
        dlg.show()
        w._eq_dialog = dlg

        committed: list = []
        w.track_effects_committed.connect(lambda tid, c: committed.append(c))

        # 外部移除 EQ 实例（回填路径会关窗；这里直接构造迟到提交竞态）
        w.set_effect_chain(None)
        committed.clear()

        # 窗口关闭前的迟到提交（对话框仍持有旧参数）
        w._on_eq_committed_from_badge("T1", StripEQParams(master_gain=9.0))
        assert committed == [], "removed EQ instance resurrected by stale dialog"
        assert w._effect_chain is None

    def test_eq_dialog_fresh_create_still_works(self, qapp):
        """反例：窗口从未绑定实例 id（首次新建路径）→ 照常创建。"""
        from src.features.edit.core.timeline_v2 import StripEQParams
        w = _make_mixer(qapp)
        dlg = _FakeEffectDialog()
        dlg.show()
        w._eq_dialog = dlg  # 无 _instance_id

        committed: list = []
        w.track_effects_committed.connect(lambda tid, c: committed.append(c))
        w._on_eq_committed_from_badge("T1", StripEQParams(master_gain=3.0))
        assert len(committed) == 1
        assert w._effect_chain is not None
        assert w._effect_chain.effects[0].panel == "eq"

    def test_eq_dialog_closes_when_instance_removed(self, qapp):
        """外部移除 EQ 实例（链保留其他效果）：EQ 窗被关闭。"""
        from src.features.edit.core.timeline_v2 import StripEQParams
        w = _make_mixer(qapp)
        eq_inst = TrackEffectInstance(
            effect_type="strip_eq", instance_id="EQ1", panel="eq",
            params=StripEQParams.default().to_dict())
        w.set_effect_chain(EffectChainParams(
            effects=(eq_inst, _delay_instance(100.0, "D1"))))
        dlg = _FakeEffectDialog()
        dlg._instance_id = "EQ1"
        dlg.show()
        w._eq_dialog = dlg
        closed = []
        dlg.close = lambda: closed.append(True)  # 记录关闭（避免真关）

        w.set_effect_chain(EffectChainParams(
            effects=(_delay_instance(100.0, "D1"),)))
        assert closed, "EQ dialog not closed after instance removal"


class TestDialogIdentityPerInstance:
    def test_dialogs_keyed_by_instance_id_two_same_type(self, qapp):
        """两个同类 Delay 实例：窗口字典按 instance_id 区分，回填各归各。"""
        w = _make_mixer(qapp)
        w.set_effect_chain(EffectChainParams(
            effects=(_delay_instance(100.0, "D1"),
                     _delay_instance(200.0, "D2"))))
        dlg1 = _FakeEffectDialog()
        dlg2 = _FakeEffectDialog()
        dlg1.show()
        dlg2.show()
        if not hasattr(w, "_delay_dialogs"):
            w._delay_dialogs = {}
        w._delay_dialogs["D1"] = dlg1
        if not hasattr(w, "_delay_dialogs"):
            w._delay_dialogs = {}
        w._delay_dialogs["D2"] = dlg2

        w.set_effect_chain(EffectChainParams(
            effects=(_delay_instance(111.0, "D1"),
                    _delay_instance(222.0, "D2"))))

        assert dlg1.backfilled[0]["left_delay"] == 111.0
        assert dlg2.backfilled[0]["left_delay"] == 222.0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
