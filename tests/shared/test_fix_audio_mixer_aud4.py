"""
批 A（AUD-37803/37808/37809/37900）修复回归测试。

覆盖：
1. AUD-37803: PresetDialog Apply 按钮的 clicked(bool) 不再把 False 当
   QListWidgetItem 传入槽（原先必崩 AttributeError）。
2. AUD-37808: RoutingDialog 初始装载（程序化 setCurrentIndex）不发射
   routing_committed（原先打开对话框即以空 sends 提交，消费端按替换
   语义清空该轨全部 Send）；用户交互后才发射。
3. AUD-37809: 空 target_bus_id 的 Send 不再被构造（Qt 槽内未捕获
   ValueError 会 abort）：占位项/目标 Bus 已删除时 collect() 返回 None；
   无 Bus 时添加按钮禁用且 _on_add_send 有守卫；EditTrack.from_dict
   加载侧过滤脏数据。
4. AUD-37900: Limiter 参数编辑走 preview/committed 双路（committed 进
   命令栈），与 compressor 等 7 类效果器一致。

UI 测试用 QT_QPA_PLATFORM=offscreen，不依赖真实音频设备。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# ────────────────────────── AUD-37803: PresetDialog ──────────────────────────


class TestPresetDialogApplyButton:
    """Apply 按钮 clicked(bool) 与带参槽的参数类型不匹配（必崩路径）。"""

    def _make_dialog(self, qapp):
        """构造一个不读用户磁盘预设文件的 PresetDialog。"""
        from src.shared.ui.audio_mixer.preset_dialog import PresetDialog
        with patch("src.shared.ui.audio_mixer.preset_dialog.load_presets",
                   return_value=[]):
            dlg = PresetDialog(track_id="t1", track_name="Track 1")
        return dlg

    def test_apply_button_clicked_does_not_crash(self, qapp):
        """clicked 信号携带的 checked=False 不得被当作 item 使用。"""
        from src.features.edit.core.mixer_preset import ChannelStripPreset
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QListWidgetItem

        dlg = self._make_dialog(qapp)
        preset = ChannelStripPreset(name="P1")
        item = QListWidgetItem("P1")
        item.setData(Qt.ItemDataRole.UserRole, preset)
        dlg._list.addItem(item)
        dlg._list.setCurrentItem(item)

        received = []
        dlg.apply_preset.connect(lambda tid, p: received.append((tid, p)))

        # 真实 clicked 信号签名是 clicked(bool)。修复前：False 被当作 item
        # 传入 → 'bool' object has no attribute 'data'（AttributeError）。
        dlg._apply_btn.clicked.emit(False)

        assert received == [("t1", preset)]

    def test_apply_button_no_selection_is_noop(self, qapp):
        """无选中项时点击 Apply 安静返回（不崩、不发信号）。"""
        dlg = self._make_dialog(qapp)
        received = []
        dlg.apply_preset.connect(lambda tid, p: received.append((tid, p)))
        dlg._apply_btn.clicked.emit(False)
        assert received == []


# ────────────────────────── AUD-37808: RoutingDialog ──────────────────────────


class TestRoutingDialogNoCommitOnInit:
    """打开对话框不得触发 committed（初始装载加状态锁）。"""

    def _make_dialog(self, qapp):
        from src.shared.ui.audio_mixer.routing_dialog import RoutingDialog
        from src.features.edit.core.timeline_v2 import Send

        dialog = RoutingDialog(
            track_id="t1", track_name="Track 1",
            output_bus_id="busA",  # 路由到 Bus → 装载时 combo 索引变化
            sends=(Send(target_bus_id="busB", level=0.5),),
            bus_tracks=[("busA", "Bus A"), ("busB", "Bus B")],
        )
        return dialog

    def test_construction_emits_no_committed(self, qapp):
        """构造（含程序化回填）期间不得发射 routing_committed。"""
        dialog = self._make_dialog(qapp)
        commits = []
        dialog.routing_committed.connect(lambda tid, p: commits.append((tid, p)))
        # 构造早于连接无法直接捕获，这里验证装载完成后的最终状态正确、
        # 且 loading 锁已复位（后续用户交互正常提交）。
        assert dialog._loading is False
        assert len(dialog._send_slots) == 1
        payload = dialog._collect()
        assert payload["output_bus_id"] == "busA"
        assert [s.target_bus_id for s in payload["sends"]] == ["busB"]

    def test_init_does_not_clear_sends(self, qapp):
        """回归复现原缺陷：构造期间 combo 变化不得以空 sends 提交。

        修复前 _load_initial_state 的 setCurrentIndex 触发 _on_committed，
        在 _send_slots 尚空时 emit（本测试用一个后置连接监听装载后状态，
        并直接验证装载路径不会再走提交分支）。
        """
        dialog = self._make_dialog(qapp)
        # 初始装载完成后：sends 完整保留（未被空列表替换的起点状态）
        assert dialog._collect()["sends"], "初始 sends 应完整加载"

        # 模拟"重新装载"（如另一个轨道复用构造路径）：再次进入 loading
        # 状态并程序化变更 UI，不应发射 committed。
        commits = []
        dialog.routing_committed.connect(lambda tid, p: commits.append((tid, p)))
        dialog._loading = True
        dialog._load_initial_state()
        dialog._output_combo.setCurrentIndex(0)  # Master
        dialog._loading = False
        assert commits == [], "程序化装载期不得发射 committed"

    def test_user_interaction_commits(self, qapp):
        """用户交互（改输出目标）正常发射 committed 且 payload 正确。"""
        dialog = self._make_dialog(qapp)
        commits = []
        dialog.routing_committed.connect(lambda tid, p: commits.append((tid, p)))
        dialog._output_combo.setCurrentIndex(0)  # 用户改选 Master
        assert len(commits) == 1
        tid, payload = commits[0]
        assert tid == "t1"
        assert payload["output_bus_id"] is None
        assert [s.target_bus_id for s in payload["sends"]] == ["busB"]


# ────────────────────────── AUD-37809: 空 target 防御 ──────────────────────────


class TestSendSlotEmptyTarget:
    """Send 空 target_bus_id 的三条崩溃路径（构造前过滤）。"""

    def test_collect_placeholder_returns_none(self, qapp):
        """用户选回"(选择 Bus)"占位项 → collect 返回 None 而非抛 ValueError。"""
        from src.shared.ui.audio_mixer.routing_dialog import _SendSlotWidget
        from src.features.edit.core.timeline_v2 import Send

        send = Send(target_bus_id="busA", level=0.7, enabled=True)
        slot = _SendSlotWidget(send, [("busA", "Bus A"), ("busB", "Bus B")])
        slot._target_combo.setCurrentIndex(0)  # 占位项（data=""）
        assert slot.is_target_empty is True
        assert slot.collect() is None  # 修复前：Send(target_bus_id="") → ValueError

    def test_collect_deleted_bus_target_returns_none(self, qapp):
        """既有 send 的目标 Bus 已被删除（无匹配选项，停在占位项）→ 不崩。"""
        from src.shared.ui.audio_mixer.routing_dialog import _SendSlotWidget
        from src.features.edit.core.timeline_v2 import Send

        send = Send(target_bus_id="ghostBus", level=0.5, enabled=True)
        slot = _SendSlotWidget(send, [("busA", "Bus A")])  # 选项不含 ghostBus
        assert slot.is_target_empty is True
        assert slot.collect() is None

    def test_collect_valid_target_still_works(self, qapp):
        """正常目标不受影响（仍构造 Send）。"""
        from src.shared.ui.audio_mixer.routing_dialog import _SendSlotWidget
        from src.features.edit.core.timeline_v2 import Send

        send = Send(target_bus_id="busA", level=0.7, enabled=True)
        slot = _SendSlotWidget(send, [("busA", "Bus A")])
        collected = slot.collect()
        assert collected is not None
        assert collected.target_bus_id == "busA"
        assert abs(collected.level - 0.7) < 1e-6

    def test_dialog_collect_skips_empty_target_slot(self, qapp):
        """对话框 _collect 整体收集时跳过空目标 slot（无未捕获异常）。"""
        from src.shared.ui.audio_mixer.routing_dialog import RoutingDialog
        from src.features.edit.core.timeline_v2 import Send

        dialog = RoutingDialog(
            track_id="t1", track_name="Track 1",
            output_bus_id=None,
            sends=(Send(target_bus_id="ghostBus", level=0.5),),
            bus_tracks=[("busA", "Bus A")],
        )
        payload = dialog._collect()
        assert payload["sends"] == []  # 空 target 被过滤而非崩溃

    def test_empty_target_shows_warning(self, qapp):
        """空目标时告警区明示（不允许无声跳过）。"""
        from src.shared.ui.audio_mixer.routing_dialog import RoutingDialog
        from src.features.edit.core.timeline_v2 import Send

        dialog = RoutingDialog(
            track_id="t1", track_name="Track 1",
            output_bus_id=None,
            sends=(Send(target_bus_id="ghostBus", level=0.5),),
            bus_tracks=[("busA", "Bus A")],
        )
        assert dialog._warning_label.isVisibleTo(dialog), \
            "空目标 send 应在告警区显示提示（不允许无声跳过）"

    def test_add_send_without_bus_tracks_is_guarded(self, qapp):
        """无 Bus 轨时：添加按钮禁用 + _on_add_send 兜底提示（不构造空 Send）。"""
        from src.shared.ui.audio_mixer.routing_dialog import RoutingDialog

        dialog = RoutingDialog(
            track_id="t1", track_name="Track 1",
            output_bus_id=None, sends=(), bus_tracks=[],
        )
        assert dialog._add_send_btn.isEnabled() is False
        with patch("src.shared.ui.audio_mixer.routing_dialog.QMessageBox.information") as info:
            dialog._on_add_send()  # 修复前：Send(target_bus_id="") → ValueError
        assert info.called
        assert dialog._send_slots == []


class TestEditTrackFromDictDefensive:
    """timeline_v2 加载侧防御：空 target 的脏 send 不阻断反序列化。"""

    def test_from_dict_filters_empty_target_sends(self):
        from src.features.edit.core.timeline_v2 import EditTrack

        track = EditTrack.from_dict({
            "track_id": "t1",
            "track_type": "audio",
            "sends": [
                {"send_id": "s1", "target_bus_id": "", "level": 0.5},   # 脏数据
                {"send_id": "s2", "target_bus_id": "busA", "level": 0.7},  # 正常
                {"send_id": "s3"},                                       # 缺 target
                None,                                                    # 非 dict
                "junk",                                                  # 非 dict
            ],
        })
        # 修复前：第一条就让 Send 构造抛 ValueError → 整个 EditTrack 加载失败
        assert [s.send_id for s in track.sends] == ["s2"]
        assert track.sends[0].target_bus_id == "busA"

    def test_from_dict_none_sends_field(self):
        """"sends": null 也不崩。"""
        from src.features.edit.core.timeline_v2 import EditTrack

        track = EditTrack.from_dict({"track_id": "t1", "track_type": "audio", "sends": None})
        assert track.sends == ()

    def test_send_still_rejects_empty_target(self):
        """Send 构造校验本身保留（.clinerules：构造前校验必填字段）。"""
        from src.features.edit.core.timeline_v2 import Send

        with pytest.raises(ValueError):
            Send(target_bus_id="")


# ────────────────────────── AUD-37900: Limiter 命令栈路径 ──────────────────────────


class TestLimiterCommandPath:
    """Limiter 参数编辑必须走 preview/committed 双路（committed 进命令栈）。"""

    def _make_widget(self, qapp):
        from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget
        from src.features.edit.core.timeline_v2 import (
            EffectChainParams, LimiterParams, TrackEffectInstance,
        )

        w = TrackMixerWidget(track_id="t1", track_name="Track 1")
        inst = TrackEffectInstance(
            effect_type="limiter",
            params=LimiterParams.default().to_dict(),
            enabled=True, order=0, panel="fx",
        )
        w._effect_chain = EffectChainParams(effects=(inst,))
        return w, inst

    def test_open_editor_connects_both_signals(self, qapp):
        """接线修复：limiter_preview 与 limiter_committed 均被连接。"""
        from PyQt6.QtCore import QObject, pyqtSignal
        from src.features.edit.core.timeline_v2 import LimiterParams

        # 用真实 QObject 派生的替身（信号机制可用）
        class FakeDlg(QObject):
            limiter_preview = pyqtSignal(str, object)
            limiter_committed = pyqtSignal(str, object)

            def __init__(self, *, track_id, track_name, instance_id,
                         initial_params=None, parent=None):
                super().__init__()
                self._track_id = track_id

            def isVisible(self):
                return False

            def show(self):
                pass

        w, inst = self._make_widget(qapp)
        with patch("src.shared.ui.audio_mixer.limiter_dialog.LimiterDialog", FakeDlg):
            w._open_limiter_editor(inst)

        dlg = w._limiter_dialogs[inst.instance_id]
        previews, commits = [], []
        w.track_effects_preview.connect(lambda tid, c: previews.append(c))
        w.track_effects_committed.connect(lambda tid, c: commits.append(c))

        new_params = LimiterParams(threshold_db=-6.0, release_ms=120.0)
        dlg.limiter_preview.emit("t1", new_params)
        assert len(previews) == 1 and commits == []
        assert previews[0].effects[0].params["threshold_db"] == -6.0

        dlg.limiter_committed.emit("t1", new_params)
        assert len(commits) == 1, "committed 必须走 track_effects_committed（命令栈）"

    def test_on_limiter_edited_commit_true_emits_committed(self, qapp):
        """commit=True → track_effects_committed（修复前只发 preview）。"""
        from src.features.edit.core.timeline_v2 import LimiterParams

        w, inst = self._make_widget(qapp)
        previews, commits = [], []
        w.track_effects_preview.connect(lambda tid, c: previews.append(c))
        w.track_effects_committed.connect(lambda tid, c: commits.append(c))

        new_params = LimiterParams(threshold_db=-10.0, release_ms=200.0, enabled=False)
        w._on_limiter_edited(inst, new_params, commit=True)

        assert commits and not previews
        chain = commits[0]
        edited = chain.effects[0]
        assert edited.params["threshold_db"] == -10.0
        assert edited.params["enabled"] is False

    def test_on_limiter_edited_commit_false_emits_preview(self, qapp):
        """commit=False → track_effects_preview（实时听感）。"""
        from src.features.edit.core.timeline_v2 import LimiterParams

        w, inst = self._make_widget(qapp)
        previews, commits = [], []
        w.track_effects_preview.connect(lambda tid, c: previews.append(c))
        w.track_effects_committed.connect(lambda tid, c: commits.append(c))

        w._on_limiter_edited(inst, LimiterParams(threshold_db=-2.0), commit=False)

        assert previews and not commits

    def test_on_limiter_edited_default_commit_is_true(self, qapp):
        """默认 commit=True（与 compressor 一致的签名缺省）。"""
        from src.features.edit.core.timeline_v2 import LimiterParams

        w, inst = self._make_widget(qapp)
        commits = []
        w.track_effects_committed.connect(lambda tid, c: commits.append(c))
        w._on_limiter_edited(inst, LimiterParams())  # 两参调用兼容
        assert commits
