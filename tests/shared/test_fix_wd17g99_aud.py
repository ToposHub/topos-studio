"""WD17-G99 修复验证：batch-378/379/380/382 shared-ui（audio_mixer 后半 +
column_grid / HUD / 曲线控件）审计发现。

覆盖（每条含正例与反例）：
- AUD-37812  删除 Send 后槽位控件从 UI 与槽位列表同步移除，提交的
             payload 不再包含该 send（UI 与模型状态一致）
- AUD-37814  Spill 交互接线：Bus 列头点击触发 spill()/spill_requested，
             Master 列头不触发（提示而非静默）
- AUD-37901  balance 拖动期不再经 spinbox 直连泄露 balance_changed：
             拖动只发 preview，松手恰好一次 commit；spinbox 视觉同步
- AUD-37902  删除效果器实例关闭其打开中的编辑对话框；已删实例的后续
             编辑被守卫丢弃，不再发出无变化链的重复提交
- AUD-37903  EQ 后端探测 fail-closed：capabilities 导入失败时降级警示
             （橙色 badge）出现而非伪装"可用"
- AUD-37904  _sync_dialog_toggle 程序化 setChecked 加信号锁，
             不再级联触发对话框处理器（伪 toggled）
- AUD-37905  右键 tag 标签时菜单按 sender() 映射全局坐标，
             不再偏移到 _track_label 原点
- AUD-38001  apply_columns 不再"每次执行都写 QSettings"：列数不变
             （resize 风暴重放）零写入，变化恰好写一次，滑块全程
             拖动的写盘次数以列数范围宽度为上界
- AUD-38002  EffectDegradationPanel.refresh_settings 加信号锁，
             程序化 setChecked 不触发 _on_global_toggle 伪信号
- AUD-38003  HUD 关闭按钮走 hide_hud：_visible 复位、定时器停、
             visibility_changed 发射
- AUD-38004  on_playback_started 对 source_manager 私有属性 getattr 防御，
             缺属性按"无该源"处理不再 AttributeError
- AUD-38211  左键点击空白处（未加点）interaction_started 在 release 得到
             配对 interaction_ended
- AUD-38212  set_lut(emit_changed=True) 的 changed 信号在 blockSignals
             恢复后发射，真正送达接收方
- AUD-38213  mouseDoubleClickEvent 检查按钮：右键双击不再重置曲线
- AUD-38214  set_lut 支持 (256, 3) 形状（文档承诺补齐），旧工程恢复不丢曲线
- AUD-38215  calculate_curve 裸 except 收窄为 (ValueError, IndexError)
             并记日志，降级路径不变

UI 测试用 QT_QPA_PLATFORM=offscreen，不依赖真实音频设备。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QCheckBox, QLabel, QListWidget, QMenu, QPushButton, QWidget


# ────────────────────────── 测试工具 ──────────────────────────


def _mouse_event(
    event_type: QEvent.Type,
    pos: QPointF,
    button: Qt.MouseButton = Qt.MouseButton.LeftButton,
) -> QMouseEvent:
    """构造本地坐标鼠标事件（offscreen 测试用）。"""
    return QMouseEvent(
        event_type, pos, pos, button, button, Qt.KeyboardModifier.NoModifier
    )


# ────────────────────────── AUD-37812: 删除 Send 移除槽位控件 ──────────────────────────


def _make_routing_dialog(qapp, with_send: bool = True):
    from src.shared.ui.audio_mixer.routing_dialog import RoutingDialog
    from src.features.edit.core.timeline_v2 import Send

    sends = (Send(target_bus_id="B1", level=1.0, enabled=True),) if with_send else ()
    dlg = RoutingDialog(
        track_id="T1", track_name="Track 1",
        output_bus_id=None, sends=sends,
        bus_tracks=[("B1", "Bus 1"), ("B2", "Bus 2")],
    )
    return dlg


class TestAud37812RemoveSendSlot:
    def test_remove_slot_removes_widget_and_commits(self, qapp):
        """点击删除：槽位控件离开布局与槽位列表，payload 不含该 send。"""
        dlg = _make_routing_dialog(qapp, with_send=True)
        assert len(dlg._send_slots) == 1
        slot = dlg._send_slots[0]
        committed: list[dict] = []
        dlg.routing_committed.connect(lambda _tid, p: committed.append(p))

        slot._on_remove()

        # UI 与模型状态一致：槽位从列表移除、从布局分离
        assert slot not in dlg._send_slots
        assert slot.parent() is None
        # 提交的 payload 已不含被删 send
        assert len(committed) == 1
        assert committed[0]["sends"] == []

    def test_remove_one_of_two_keeps_other(self, qapp):
        """多 send 场景：只删点中的那个，其余保留。"""
        dlg = _make_routing_dialog(qapp, with_send=True)
        from src.features.edit.core.timeline_v2 import Send
        dlg._add_send_slot(Send(target_bus_id="B2", level=0.5, enabled=True))
        assert len(dlg._send_slots) == 2
        committed: list[dict] = []
        dlg.routing_committed.connect(lambda _tid, p: committed.append(p))

        dlg._send_slots[1]._on_remove()

        assert len(dlg._send_slots) == 1
        assert len(committed) == 1
        assert [s.target_bus_id for s in committed[0]["sends"]] == ["B1"]

    def test_without_removal_slot_stays(self, qapp):
        """反例：不点删除时槽位保留、无提交。"""
        dlg = _make_routing_dialog(qapp, with_send=True)
        committed: list[dict] = []
        dlg.routing_committed.connect(lambda _tid, p: committed.append(p))
        assert len(dlg._send_slots) == 1
        assert committed == []


# ────────────────────────── AUD-37814: Spill 交互接线 ──────────────────────────


def _make_matrix_dialog(qapp):
    from src.shared.ui.audio_mixer.routing_matrix_dialog import RoutingMatrixDialog

    tracks = [
        ("S1", "Src 1", False),
        ("S2", "Src 2", False),
        ("B1", "Bus 1", True),
        ("B2", "Bus 2", True),
    ]
    routing = {
        "S1": {"output_bus_id": "B1", "sends": [], "is_bus": False},
        "S2": {"output_bus_id": None, "sends": [("B2", 0.5)], "is_bus": False},
        "B1": {"output_bus_id": None, "sends": [], "is_bus": True},
        "B2": {"output_bus_id": None, "sends": [], "is_bus": True},
    }
    return RoutingMatrixDialog(tracks=tracks, routing=routing)


class TestAud37814SpillWiring:
    def test_bus_header_click_triggers_spill(self, qapp):
        """点击 Bus 列头 → spill()/spill_requested 可由用户触发。"""
        dlg = _make_matrix_dialog(qapp)
        requested: list[str] = []
        dlg.spill_requested.connect(requested.append)

        # 列 0=Master，列 1=B1
        dlg._table.horizontalHeader().sectionClicked.emit(1)

        assert requested == ["B1"]
        assert dlg._spilled_bus == "B1"
        # 标签显示上游轨道（S1 经 output 路由到 B1）
        assert "Src 1" in dlg._spill_label.text()

    def test_master_header_click_does_not_spill(self, qapp):
        """反例：Master 列头不触发 spill（显示提示而非静默/误触发）。"""
        dlg = _make_matrix_dialog(qapp)
        requested: list[str] = []
        dlg.spill_requested.connect(requested.append)

        dlg._table.horizontalHeader().sectionClicked.emit(0)

        assert requested == []
        assert dlg._spilled_bus is None

    def test_spill_via_header_matches_direct_call(self, qapp):
        """经表头的 spill 与直接调用 spill() 行为一致。"""
        dlg = _make_matrix_dialog(qapp)
        dlg._table.horizontalHeader().sectionClicked.emit(2)  # B2：S2 经 send 上游
        assert dlg._spilled_bus == "B2"
        assert "Src 2" in dlg._spill_label.text()


# ────────────────────────── AUD-37901: balance 拖动期 commit 泄露 ──────────────────────────


def _make_mixer(qapp):
    from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget

    return TrackMixerWidget(track_id="T1", track_name="Track 1", track_tag="A1")


class TestAud37901BalanceCommitLeak:
    def test_drag_emits_preview_only_then_single_commit(self, qapp):
        """拖动期只发 balance_preview；松手恰好一次 balance_changed。"""
        w = _make_mixer(qapp)
        previews: list[int] = []
        commits: list[int] = []
        w.balance_preview.connect(lambda _tid, v: previews.append(v))
        w.balance_changed.connect(lambda _tid, v: commits.append(v))

        w._on_balance_pressed()
        for v in (10, 20, 30):
            w._on_balance_changed(v)
        # 拖动期间：无 commit 泄露（旧行为：spinbox 直连每次都灌 commit）
        assert commits == []
        assert previews == [10, 20, 30]
        # spinbox 视觉同步跟随拖动值
        assert w._balance_spin.value() == 30

        w._on_balance_released()
        assert commits == [30]  # 松手恰好一次提交

    def test_nondrag_change_commits_directly(self, qapp):
        """反例：非拖动变化（键盘）仍直接提交一次。"""
        w = _make_mixer(qapp)
        commits: list[int] = []
        w.balance_changed.connect(lambda _tid, v: commits.append(v))

        w._on_balance_changed(5)
        assert commits == [5]

    def test_spin_keyboard_input_still_commits_once(self, qapp):
        """spinbox 键盘输入路径不受影响：一次提交、推子视觉同步、无重入。"""
        w = _make_mixer(qapp)
        commits: list[int] = []
        w.balance_changed.connect(lambda _tid, v: commits.append(v))

        w._balance_spin.setValue(7)
        assert commits == [7]
        assert w._balance_fader.value() == 7


# ────────────────────────── AUD-37902: 删除实例处理打开中的对话框 ──────────────────────────


class _FakeEffectDialog(QWidget):
    """模拟效果器编辑对话框：记录 deleteLater 调用。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.delete_later_count = 0
        # 模拟对话框的启用开关（AUD-37904 复用同一控件）
        self._enable_toggle = QCheckBox(self)
        self._enable_toggle.setChecked(False)
        self.toggle_signals = 0
        self._enable_toggle.toggled.connect(lambda _c: self._on_toggle())

    def _on_toggle(self) -> None:
        self.toggle_signals += 1

    def deleteLater(self) -> None:  # noqa: N802 (Qt 命名)
        self.delete_later_count += 1
        super().deleteLater()


class TestAud37902DialogCleanupOnRemove:
    def test_remove_closes_open_dialog(self, qapp):
        """删除实例时关闭（deleteLater）并从对话框字典移除。"""
        w = _make_mixer(qapp)
        w._effect_add("limiter")
        assert w._effect_chain is not None
        inst = w._effect_chain.effects[0]
        dlg = _FakeEffectDialog()
        dlg.show()
        w._limiter_dialogs[inst.instance_id] = dlg

        w._effect_remove(inst)

        assert inst.instance_id not in w._limiter_dialogs
        assert dlg.delete_later_count == 1

    def test_edits_from_removed_instance_are_dropped(self, qapp):
        """已删实例的对话框编辑被守卫丢弃：不再发出无变化链的重复提交。"""
        from src.features.edit.core.timeline_v2 import LimiterParams

        w = _make_mixer(qapp)
        w._effect_add("limiter")
        inst = w._effect_chain.effects[0]
        committed: list = []
        w.track_effects_committed.connect(lambda _tid, c: committed.append(c))

        w._effect_remove(inst)
        assert len(committed) == 1  # 删除本身的提交
        committed.clear()

        # 删除前已打开的对话框继续编辑：守卫拦截，不产生新提交
        w._on_limiter_edited(inst, LimiterParams.default(), commit=True)
        assert committed == []

    def test_edits_from_live_instance_still_commit(self, qapp):
        """反例：存活实例的编辑照常提交。"""
        from src.features.edit.core.timeline_v2 import LimiterParams

        w = _make_mixer(qapp)
        w._effect_add("limiter")
        inst = w._effect_chain.effects[0]
        committed: list = []
        w.track_effects_committed.connect(lambda _tid, c: committed.append(c))

        w._on_limiter_edited(inst, LimiterParams.default(), commit=True)
        assert len(committed) == 1
        assert committed[0].effects[0].instance_id == inst.instance_id


# ────────────────────────── AUD-37903: EQ 后端探测 fail-closed ──────────────────────────


def _eq_chain():
    """构造含启用 EQ 胶囊实例（panel="eq"）的最小 chain 桩。"""
    inst = SimpleNamespace(
        effect_type="strip_eq", params={"enabled": True},
        panel="eq", instance_id="eq-1", order=0,
    )
    return SimpleNamespace(effects=(inst,))


class TestAud37903FailClosedProbe:
    def test_import_failure_shows_degraded_badge(self, qapp):
        """capabilities 导入失败 → 按不可用处理：橙色降级 badge 出现。"""
        w = _make_mixer(qapp)
        w._effect_chain = _eq_chain()

        with patch.dict(sys.modules, {"src.features.edit.core.audio_capabilities": None}):
            w._update_badges_style()  # 不抛 ImportError

        style = w._eq_badge.styleSheet()
        assert "#664400" in style  # 降级告警色（fail-closed）

    def test_backend_ok_shows_cyan(self, qapp):
        """反例：后端可用 → 青色激活样式（非降级）。"""
        w = _make_mixer(qapp)
        w._effect_chain = _eq_chain()

        with patch(
            "src.features.edit.core.audio_capabilities.is_scipy_available_for_dsp",
            return_value=True,
        ):
            w._update_badges_style()

        style = w._eq_badge.styleSheet()
        assert "#664400" not in style
        assert "#00d2c1" in style

    def test_backend_unavailable_shows_degraded_badge(self, qapp):
        """SciPy 探测返回 False → 降级告警。"""
        w = _make_mixer(qapp)
        w._effect_chain = _eq_chain()

        with patch(
            "src.features.edit.core.audio_capabilities.is_scipy_available_for_dsp",
            return_value=False,
        ):
            w._update_badges_style()

        assert "#664400" in w._eq_badge.styleSheet()


# ────────────────────────── AUD-37904: _sync_dialog_toggle 信号锁 ──────────────────────────


class TestAud37904SyncDialogToggle:
    def test_programmatic_set_checked_does_not_fire_toggled(self, qapp):
        """同步开关时对话框处理器不被触发（伪 toggled 被 blockSignals 吞）。"""
        w = _make_mixer(qapp)
        dlg = _FakeEffectDialog()
        dlg.show()
        w._limiter_dialogs["lim-1"] = dlg

        w._sync_dialog_toggle("lim-1", True)

        assert dlg._enable_toggle.isChecked() is True  # 视觉已同步
        assert dlg.toggle_signals == 0                 # 但未触发对话框处理器

    def test_missing_instance_is_noop(self, qapp):
        """反例：未打开对话框的实例为 no-op。"""
        w = _make_mixer(qapp)
        w._sync_dialog_toggle("nope", True)  # 不抛异常


# ────────────────────────── AUD-37905: 右键菜单坐标映射 ──────────────────────────


class TestAud37905ContextMenuOrigin:
    def test_tag_label_menu_uses_sender_origin(self, qapp):
        """右键 tag 标签：菜单按 tag 标签自身坐标映射，不偏移到 track_label。"""
        w = _make_mixer(qapp)
        w.resize(120, 420)
        w.show()
        captured: list[QPoint] = []

        with patch.object(
            QMenu, "exec",
            lambda self, pos, *a, **k: (captured.append(QPoint(pos)), 0)[1],
        ):
            w._track_tag_label.customContextMenuRequested.emit(QPoint(3, 4))

        tag_global = w._track_tag_label.mapToGlobal(QPoint(3, 4))
        label_global = w._track_label.mapToGlobal(QPoint(3, 4))
        # 两个标签位置不同（tag 在顶部、label 在列表下方），保证测试有区分度
        assert tag_global != label_global
        assert captured == [tag_global]

    def test_track_label_menu_still_correct(self, qapp):
        """反例：右键 track_label 仍按其自身坐标映射。"""
        w = _make_mixer(qapp)
        w.resize(120, 420)
        w.show()
        captured: list[QPoint] = []

        with patch.object(
            QMenu, "exec",
            lambda self, pos, *a, **k: (captured.append(QPoint(pos)), 0)[1],
        ):
            w._track_label.customContextMenuRequested.emit(QPoint(5, 6))

        assert captured == [w._track_label.mapToGlobal(QPoint(5, 6))]


# ────────────────────────── AUD-38001: QSettings 写入去抖 ──────────────────────────


class TestAud38001SettingsDebounce:
    def test_same_columns_resize_storm_no_write(self, qapp):
        """resize 风暴中 columns 不变：完全不触碰 QSettings。"""
        from src.shared.ui.column_grid import ColumnGridController

        with patch("PyQt6.QtCore.QSettings") as QS:
            QS.return_value.value.return_value = 4
            view = QListWidget()
            view.resize(600, 400)
            ctrl = ColumnGridController(view, settings_key="g99_storm")
            QS.reset_mock()

            for _ in range(5):
                ctrl.apply_columns(ctrl.columns)  # 模拟 0ms retile 重放

            QS.assert_not_called()  # 一次 QSettings 实例化/写入都没有

    def test_change_writes_exactly_once_per_change(self, qapp):
        """列数变化各写一次（值正确）；同值重放不追加写入。"""
        from src.shared.ui.column_grid import ColumnGridController

        with patch("PyQt6.QtCore.QSettings") as QS:
            QS.return_value.value.return_value = 4
            view = QListWidget()
            view.resize(600, 400)
            ctrl = ColumnGridController(view, settings_key="g99_change")

            QS.reset_mock()
            ctrl.apply_columns(6)
            ctrl.apply_columns(6)  # 同值重放：不追加
            ctrl.apply_columns(7)

            setValue = QS.return_value.setValue
            assert setValue.call_count == 2
            assert setValue.call_args_list[0].args == ("g99_change", 6)
            assert setValue.call_args_list[1].args == ("g99_change", 7)
            assert ctrl._persisted_columns == 7

    def test_slider_drag_bounded_by_range_width(self, qapp):
        """滑块全程拖动（1..8）的写盘次数以范围宽度为上界，而非每 tick 多次。"""
        from src.shared.ui.column_grid import ColumnGridController
        from PyQt6.QtWidgets import QSlider

        with patch("PyQt6.QtCore.QSettings") as QS:
            QS.return_value.value.return_value = 4
            view = QListWidget()
            view.resize(600, 400)
            slider = QSlider(Qt.Orientation.Horizontal)
            ctrl = ColumnGridController(view, slider=slider, settings_key="g99_slider")

            QS.reset_mock()
            # 模拟整段拖动 + 缩放风暴交替
            for c in range(1, 9):
                slider.setValue(c)
                for _ in range(3):
                    ctrl.apply_columns(ctrl.columns)  # retile 重放同值

            assert QS.return_value.setValue.call_count <= 8
            assert ctrl._persisted_columns == 8


# ────────────────────────── AUD-38002: refresh_settings 信号锁 ──────────────────────────


class TestAud38002RefreshSettingsLock:
    @pytest.fixture(autouse=True)
    def _preserve_degradation_state(self):
        """保存/恢复全局降级开关，避免污染其他测试。"""
        from src.shared.effects.preview_quality import (
            get_effect_preview_quality_manager,
        )
        mgr = get_effect_preview_quality_manager()
        original = mgr.is_degradation_enabled()
        yield
        mgr.set_degradation_enabled(original)

    def test_refresh_does_not_fire_pseudo_signal(self, qapp):
        """状态翻转后 refresh_settings：不回写 manager、不外发 changed。"""
        from src.shared.ui.effect_degradation_indicator import EffectDegradationPanel
        from src.shared.effects.preview_quality import (
            get_effect_preview_quality_manager,
        )

        panel = EffectDegradationPanel()
        mgr = get_effect_preview_quality_manager()

        # 外部（偏好设置持久化恢复）翻转状态：refresh 的 setChecked 必然
        # 改变复选框状态 —— 未加锁时必然发射 stateChanged 伪信号
        mgr.set_degradation_enabled(not mgr.is_degradation_enabled())
        expected = mgr.is_degradation_enabled()

        emitted: list[dict] = []
        panel.degradation_settings_changed.connect(emitted.append)

        with patch.object(mgr, "set_degradation_enabled") as sw:
            panel.refresh_settings()
            sw.assert_not_called()

        assert emitted == []
        assert panel._enable_checkbox.isChecked() == expected


# ────────────────────────── AUD-38003: HUD 关闭按钮走 hide_hud ──────────────────────────


class TestAud38003CloseButtonRouting:
    def test_close_button_hides_with_state_sync(self, qapp):
        """点 X：_visible 复位、定时器停、visibility_changed 发射 False。"""
        from src.shared.ui.playback_hud import PlaybackHUDPanel

        panel = PlaybackHUDPanel()
        visibility: list[bool] = []
        panel.visibility_changed.connect(visibility.append)

        panel.show_hud()
        assert panel.is_visible() is True
        assert panel._update_timer.isActive()

        close_btns = [b for b in panel.findChildren(QPushButton) if b.text() == "X"]
        assert len(close_btns) == 1
        close_btns[0].click()

        assert visibility == [True, False]
        assert panel.is_visible() is False
        assert not panel._update_timer.isActive()
        assert not panel.isVisible()

    def test_toggle_after_close_works(self, qapp):
        """关闭后再 toggle：语义正确（旧行为 _visible 残留 True 会反转出错）。"""
        from src.shared.ui.playback_hud import PlaybackHUDPanel

        panel = PlaybackHUDPanel()
        close_btns = [b for b in panel.findChildren(QPushButton) if b.text() == "X"]
        panel.show_hud()
        close_btns[0].click()

        panel.toggle_visibility()
        assert panel.is_visible() is True  # 从"已隐藏"toggle → 显示


# ────────────────────────── AUD-38004: source_manager 防御访问 ──────────────────────────


def _make_hud_controller(qapp):
    from src.shared.ui.playback_hud_integration import PlaybackHUDController

    return PlaybackHUDController(main_window=None, parent_widget=None,
                                 auto_create_dock=False)


class TestAud38004DefensiveSourceManager:
    def test_source_manager_without_private_attrs(self, qapp):
        """缺私有属性的 session：默认 master_only/unknown，不抛 AttributeError。"""
        ctrl = _make_hud_controller(qapp)
        session = SimpleNamespace(source_manager=SimpleNamespace())
        ctrl.connect_session(session)

        ctrl.on_playback_started("/test/v.mp4", None, None)

        info = ctrl.model.pipeline_info
        assert info.source_type == "master_only"
        assert info.frame_path == "unknown"

    def test_disk_proxy_detected(self, qapp):
        """_disk_proxy_source 存在 → disk_proxy。"""
        ctrl = _make_hud_controller(qapp)
        sm = SimpleNamespace(
            _disk_proxy_source=object(), _proxy_source=None, _master_source=None,
        )
        ctrl.connect_session(SimpleNamespace(source_manager=sm))

        ctrl.on_playback_started("/test/v.mp4", None, None)
        assert ctrl.model.pipeline_info.source_type == "disk_proxy"

    def test_memory_proxy_and_planar_frame_path(self, qapp):
        """proxy != master → memory_proxy；is_planar_mode() → planar 路径。"""
        ctrl = _make_hud_controller(qapp)
        proxy = SimpleNamespace(
            _disk_proxy_source=None,
            _proxy_source=SimpleNamespace(is_planar_mode=lambda: True),
            _master_source=object(),
        )
        # proxy._proxy_source 与 _master_source 不同对象
        ctrl.connect_session(SimpleNamespace(source_manager=proxy))

        ctrl.on_playback_started("/test/v.mp4", None, None)
        info = ctrl.model.pipeline_info
        assert info.source_type == "memory_proxy"
        assert info.frame_path == "planar_yuv_r8_upload"

    def test_session_without_source_manager(self, qapp):
        """连 source_manager 属性都没有的 session：安全跳过源类型探测。"""
        ctrl = _make_hud_controller(qapp)
        ctrl.connect_session(SimpleNamespace())

        ctrl.on_playback_started("/test/v.mp4", None, None)  # 不抛
        # 无 source_manager → 探测整段跳过，源类型保持模型默认
        assert ctrl.model.pipeline_info.source_type == "unknown"


# ────────────────────────── AUD-38211: interaction 配对 ──────────────────────────


def _make_curves(qapp):
    from src.shared.ui.widgets.curves_widget import CurvesWidget

    w = CurvesWidget()
    w.resize(400, 300)
    return w


class TestAud38211InteractionPairing:
    def test_left_click_empty_area_pairs_ended(self, qapp):
        """左键点击空白处（未加点）：started 在 release 得到配对 ended。"""
        w = _make_curves(qapp)
        started = ended = 0
        w.interaction_started.connect(lambda: None)
        counts = {"s": 0, "e": 0}
        w.interaction_started.connect(lambda: counts.__setitem__("s", counts["s"] + 1))
        w.interaction_ended.connect(lambda: counts.__setitem__("e", counts["e"] + 1))

        # (2,2) 在 draw rect（margin=15）之外且远离任何控制点
        w.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, QPointF(2, 2)))
        assert counts == {"s": 1, "e": 0}
        assert len(w.curves["M"]) == 2  # 未加点

        w.mouseReleaseEvent(_mouse_event(QEvent.Type.MouseButtonRelease, QPointF(2, 2)))
        assert counts == {"s": 1, "e": 1}  # 配对补齐（旧行为：ended 永不发射）

    def test_click_on_point_still_pairs(self, qapp):
        """反例：命中/新增控制点的点击，release 配对不变。"""
        w = _make_curves(qapp)
        counts = {"s": 0, "e": 0}
        w.interaction_started.connect(lambda: counts.__setitem__("s", counts["s"] + 1))
        w.interaction_ended.connect(lambda: counts.__setitem__("e", counts["e"] + 1))

        # 命中白点 (255,0) → widget 坐标
        wx, wy = w.to_widget(255, 0)
        w.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, QPointF(wx, wy)))
        w.mouseReleaseEvent(_mouse_event(QEvent.Type.MouseButtonRelease, QPointF(wx, wy)))
        assert counts == {"s": 1, "e": 1}

    def test_add_point_inside_rect_pairs(self, qapp):
        """反例：空白处加点（rect 内）同样配对。"""
        w = _make_curves(qapp)
        counts = {"s": 0, "e": 0}
        w.interaction_started.connect(lambda: counts.__setitem__("s", counts["s"] + 1))
        w.interaction_ended.connect(lambda: counts.__setitem__("e", counts["e"] + 1))

        w.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, QPointF(200, 150)))
        assert len(w.curves["M"]) == 3  # 已加点
        w.mouseReleaseEvent(_mouse_event(QEvent.Type.MouseButtonRelease, QPointF(200, 150)))
        assert counts == {"s": 1, "e": 1}


# ────────────────────────── AUD-38212: set_lut 的 changed 送达 ──────────────────────────


class TestAud38212SetLutEmit:
    def test_emit_changed_true_reaches_receiver(self, qapp):
        """emit_changed=True：changed 信号同步送达接收方（恰好一次）。"""
        import numpy as np

        w = _make_curves(qapp)
        received: list = []
        w.changed.connect(lambda lut, data: received.append((lut, data)))

        lut = np.linspace(0.0, 1.0, 256).astype(np.float32).reshape(256, 1)
        w.set_lut(lut, emit_changed=True)

        assert len(received) == 1
        lut_out, data = received[0]
        assert lut_out.shape == (256, 1, 3)

    def test_emit_changed_false_silent(self, qapp):
        """反例：emit_changed=False 不发射（静默恢复路径不变）。"""
        import numpy as np

        w = _make_curves(qapp)
        received: list = []
        w.changed.connect(lambda lut, data: received.append((lut, data)))

        # 反恒等（首值 1.0 → y_ui = 0），确保可观测到恢复效果
        lut = np.linspace(1.0, 0.0, 256).astype(np.float32).reshape(256, 1)
        w.set_lut(lut, emit_changed=False)

        assert received == []
        # 但曲线确实已恢复（M 通道非默认对角线）
        assert w.curves["M"][0].y() == 0.0


# ────────────────────────── AUD-38213: 双击按钮检查 ──────────────────────────


class TestAud38213DoubleClickButton:
    def test_right_double_click_does_not_reset(self, qapp):
        """右键双击不重置曲线（旧行为：意外重置当前通道）。"""
        from PyQt6.QtCore import QPointF as PF

        w = _make_curves(qapp)
        # 先加一个控制点构造非默认曲线
        w.curves["R"] = [PF(0, 255), PF(128, 128), PF(255, 0)]
        assert len(w.curves["R"]) == 3

        w.mouseDoubleClickEvent(_mouse_event(
            QEvent.Type.MouseButtonDblClick, QPointF(100, 100),
            button=Qt.MouseButton.RightButton,
        ))

        assert len(w.curves["R"]) == 3  # 未被重置

    def test_middle_double_click_does_not_reset(self, qapp):
        """中键双击同样不重置。"""
        from PyQt6.QtCore import QPointF as PF

        w = _make_curves(qapp)
        w.curves["G"] = [PF(0, 255), PF(64, 200), PF(255, 0)]

        w.mouseDoubleClickEvent(_mouse_event(
            QEvent.Type.MouseButtonDblClick, QPointF(100, 100),
            button=Qt.MouseButton.MiddleButton,
        ))

        assert len(w.curves["G"]) == 3

    def test_left_double_click_still_resets(self, qapp):
        """反例：左键双击照常重置当前通道。"""
        from PyQt6.QtCore import QPointF as PF

        w = _make_curves(qapp)
        w.set_active_channel("R")  # 双击重置的是"当前通道"
        w.curves["R"] = [PF(0, 255), PF(128, 128), PF(255, 0)]

        w.mouseDoubleClickEvent(_mouse_event(
            QEvent.Type.MouseButtonDblClick, QPointF(100, 100),
            button=Qt.MouseButton.LeftButton,
        ))

        assert len(w.curves["R"]) == 2  # 重置为默认两端点
        assert w.curves["R"] == [PF(0, 255), PF(255, 0)]


# ────────────────────────── AUD-38214: (256, 3) 形状支持 ──────────────────────────


class TestAud38214LutShape256x3:
    def test_2d_256x3_lut_restores_channels(self, qapp):
        """文档声明的 (256, 3) 形状：控制点按各通道恢复，不再静默丢失。"""
        import numpy as np

        w = _make_curves(qapp)
        # 反恒等曲线：v = 1 - x/255 → y_ui = x（与默认对角线相反）
        x = np.arange(256, dtype=np.float32) / 255.0
        lut = np.stack([1.0 - x, x, 1.0 - x], axis=1).astype(np.float32)
        assert lut.shape == (256, 3)

        w.set_lut(lut, emit_changed=False)

        # 列 0 → R 通道：反恒等 → 点 (0,0),(255,255)
        assert w.curves["R"][0].x() == 0.0 and w.curves["R"][0].y() == 0.0
        assert w.curves["R"][-1].x() == 255.0 and w.curves["R"][-1].y() == 255.0
        # 列 1 → G 通道：恒等 → 与默认对角线一致
        assert w.curves["G"][0].y() == 255.0
        assert w.curves["G"][-1].y() == 0.0

    def test_3d_256x1x3_lut_still_works(self, qapp):
        """反例：既有 (256,1,3) 形状行为不变。"""
        import numpy as np

        w = _make_curves(qapp)
        x = np.arange(256, dtype=np.float32) / 255.0
        lut = np.stack([1.0 - x, x, 1.0 - x], axis=1).reshape(256, 1, 3)

        w.set_lut(lut, emit_changed=False)
        assert w.curves["R"][0].y() == 0.0
        assert w.curves["R"][-1].y() == 255.0

    def test_256x1_lut_still_works(self, qapp):
        """反例：既有 (256,1) 形状行为不变（仅 M 通道恢复）。"""
        import numpy as np

        w = _make_curves(qapp)
        lut = (1.0 - np.arange(256, dtype=np.float32) / 255.0).reshape(256, 1)

        w.set_lut(lut, emit_changed=False)
        assert w.curves["M"][0].y() == 0.0
        # RGB 保持默认
        assert w.curves["R"][0].y() == 255.0


# ────────────────────────── AUD-38215: 裸 except 收窄 ──────────────────────────


class TestAud38215BareExceptNarrowed:
    def test_degenerate_points_fall_back_to_linear(self, qapp, caplog):
        """重复 x 控制点（PCHIP ValueError）：降级线性插值而非崩溃，并留痕。"""
        import logging
        import numpy as np
        from PyQt6.QtCore import QPointF as PF
        from src.shared.ui.widgets.curves_widget import HAS_SCIPY

        w = _make_curves(qapp)
        pts = [PF(0, 255), PF(0, 100), PF(255, 0)]  # x=0 重复

        with caplog.at_level(logging.WARNING, logger="src.shared.ui.widgets.curves_widget"):
            vals = w.calculate_curve(pts)

        assert len(vals) == 256
        expected = np.interp(np.arange(256), [0, 0, 255], [255, 100, 0])
        assert np.allclose(vals, expected)
        # 收窄的 except 命中时留下日志（scipy 可用才会走异常降级路径）
        if HAS_SCIPY:
            assert any("AUD-38215" in r.message for r in caplog.records)

    def test_programming_errors_not_swallowed(self, qapp):
        """收窄为 (ValueError, IndexError)：try 区内的编程错误照常抛出。"""
        from PyQt6.QtCore import QPointF as PF
        import src.shared.ui.widgets.curves_widget as cw

        if not cw.HAS_SCIPY:
            pytest.skip("scipy 不可用：异常收窄验证依赖 PCHIP 主路径")

        w = _make_curves(qapp)
        pts = [PF(0, 255), PF(64, 128), PF(255, 0)]  # 合法控制点，走 PCHIP 主路径

        with patch.object(cw, "PchipInterpolator", side_effect=AttributeError("boom")):
            # 旧裸 except 会吞掉该 AttributeError 并静默降级；
            # 收窄后 (ValueError, IndexError) 不含 AttributeError → 穿透
            with pytest.raises(AttributeError, match="boom"):
                w.calculate_curve(pts)

    def test_normal_points_primary_path(self, qapp, caplog):
        """反例：正常控制点走主插值路径（过控制点、无降级日志）。"""
        import logging
        import numpy as np
        from PyQt6.QtCore import QPointF as PF

        w = _make_curves(qapp)
        pts = [PF(0, 255), PF(128, 0), PF(255, 255)]  # V 形

        with caplog.at_level(logging.WARNING, logger="src.shared.ui.widgets.curves_widget"):
            vals = w.calculate_curve(pts)

        assert len(vals) == 256
        # 插值过控制点
        assert np.isclose(vals[0], 255.0)
        assert np.isclose(vals[128], 0.0)
        assert np.isclose(vals[255], 255.0)
        # 主路径不触发降级日志
        assert not any("AUD-38215" in r.message for r in caplog.records)
