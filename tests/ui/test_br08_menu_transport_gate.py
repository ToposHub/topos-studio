"""BR08：菜单期间 transport 门的生产 feed 验收。

对应《Gallery/Viewport 后端复查整改计划》BR08 子项"菜单期间 transport
门"（2026-11-10）：``set_menu_open`` 此前只有域件无生产接线——本轮把
Qt 菜单（QMenu Show/Hide）与原生菜单栏（MenuBarAboutToBeShown/Hidden）
生命周期经 app 级事件过滤馈入协调器；嵌套子菜单收起时以
activePopupWidget 判定整体是否仍打开。

其余 BR08 子项（Color Enhanced VFR PTS 帧钟、Source 预算执行通道、
其他禁用服务的执行端）属未完成范围，能力位保持真实禁用并在完成
报告中声明（docs/plans/2026-11-10-...md）。
"""
from __future__ import annotations

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication, QMenu

from src.features.edit.ui.viewport_playback_coordinator import (
    MenuTransportGate,
    install_menu_transport_gate,
)


class _RecordingCoordinator:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    def set_menu_open(self, open_: bool) -> None:
        self.calls.append(bool(open_))


def _menu_with_event(etype: QEvent.Type) -> tuple[QMenu, QEvent]:
    return QMenu(), QEvent(etype)


class TestMenuTransportGate:
    def test_qmenu_show_and_hide_feed_gate(self, qapp):
        coordinator = _RecordingCoordinator()
        gate = MenuTransportGate(coordinator)
        menu = QMenu()
        qapp.installEventFilter(gate)
        try:
            menu.show()
            assert coordinator.calls[-1] is True     # 打开菜单 → 门关
            menu.hide()
            assert coordinator.calls[-1] is False    # 收起 → 门开
        finally:
            qapp.removeEventFilter(gate)
            menu.deleteLater()

    def test_native_menubar_events_feed_gate_when_available(self, qapp):
        """PyQt6 绑定暴露原生菜单栏事件时生效（本版本通常缺省）。"""
        from src.features.edit.ui.viewport_playback_coordinator import (
            _MENU_BAR_EVENT_TYPES,
        )
        if not _MENU_BAR_EVENT_TYPES:
            pytest.skip("PyQt6 绑定未暴露 MenuBarAboutToBeShown/Hidden")
        coordinator = _RecordingCoordinator()
        gate = MenuTransportGate(coordinator)
        for etype, expected in zip(
                _MENU_BAR_EVENT_TYPES, (True, False)):
            assert gate.eventFilter(qapp, QEvent(etype)) is False
            assert coordinator.calls[-1] is expected

    def test_unrelated_objects_ignored(self, qapp):
        from PyQt6.QtWidgets import QWidget

        coordinator = _RecordingCoordinator()
        gate = MenuTransportGate(coordinator)
        widget = QWidget()
        try:
            assert gate.eventFilter(widget, QEvent(QEvent.Type.Show)) is False
            assert coordinator.calls == []          # 非 QMenu 的 Show 不动门
        finally:
            widget.deleteLater()

    def test_install_helper_installs_app_filter(self, qapp):
        coordinator = _RecordingCoordinator()
        gate = install_menu_transport_gate(coordinator)
        menu = QMenu()
        try:
            menu.show()
            assert coordinator.calls[-1] is True
            menu.hide()
            assert coordinator.calls[-1] is False
        finally:
            menu.deleteLater()
            QApplication.instance().removeEventFilter(gate)
