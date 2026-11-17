"""Owned, non-modal effect windows whose UI values survive close/reopen."""
from __future__ import annotations

import copy
from typing import Any

from PyQt6.QtCore import QObject, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QHideEvent
from PyQt6.QtWidgets import QDialog, QLabel, QScrollArea, QVBoxLayout, QWidget

from .catalog import EffectInstance, create_panel, get_effect


def stop_panel_timers(panel: QWidget) -> None:
    """Reference panels have simulated spectra/meters; never run them as telemetry."""
    for timer in panel.findChildren(QTimer):
        timer.stop()


class EffectParameterWindow(QDialog):
    """Closing hides this cached panel; removal/disposal destroys it explicitly."""

    def __init__(self, instance: EffectInstance, owner_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.instance = instance
        descriptor = get_effect(instance.effect_id)
        self.setWindowTitle(f"{descriptor.name_zh} — {owner_name or instance.owner_id}")
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setStyleSheet("QDialog { background:#27272B; color:#E4E4EA; } QLabel#MixerEffectStatus { background:#222226; color:#AEB5C0; border:none; padding:8px 12px; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        status = QLabel("UI 参数面板 · 音频处理未连接 · 表计/频谱非实时测量", self)
        status.setObjectName("MixerEffectStatus")
        layout.addWidget(status)
        self.panel = create_panel(instance.effect_id, owner_name or instance.owner_id)
        stop_panel_timers(self.panel)
        restore = getattr(self.panel, "set_state", None)
        getter = getattr(self.panel, "get_state", None)
        self.initial_state: dict[str, Any] = copy.deepcopy(getter()) if callable(getter) else {}
        if callable(restore) and callable(getter):
            state = copy.deepcopy(instance.parameters) if instance.parameters else copy.deepcopy(self.initial_state)
            if "enabled" in state:
                state["enabled"] = instance.enabled
            restore(state)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll_area.setWidget(self.panel)
        layout.addWidget(self.scroll_area, 1)
        self.resize(min(1180, max(900, self.panel.width())), min(850, max(620, self.panel.height() + 36)))
        # Wrapping in a scroll area keeps large reference panels usable on laptops.
        self.setMinimumSize(640, 420)

    def capture_state(self) -> dict[str, Any]:
        getter = getattr(self.panel, "get_state", None)
        if callable(getter):
            state = getter()
            if not isinstance(state, dict):
                raise TypeError("Panel get_state must return a dictionary")
            self.instance.parameters = copy.deepcopy(state)
        return copy.deepcopy(self.instance.parameters)

    def hideEvent(self, event: QHideEvent) -> None:
        stop_panel_timers(self.panel)
        self.capture_state()
        super().hideEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        stop_panel_timers(self.panel)
        self.capture_state()
        super().closeEvent(event)


class EffectWindowManager(QObject):
    instanceAboutToChange = pyqtSignal(object)
    instanceChanged = pyqtSignal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._parent_widget = parent
        self._windows: dict[tuple[str, str], EffectParameterWindow] = {}
        self._synchronizing: set[tuple[str, str]] = set()

    def open(self, instance: EffectInstance, owner_name: str = "") -> EffectParameterWindow:
        descriptor = get_effect(instance.effect_id)
        if not instance.instance_id or not instance.owner_id:
            raise ValueError("Effect window requires instance and owner identities")
        key = (instance.owner_id, instance.instance_id)
        window = self._windows.get(key)
        if window is not None and window.instance.effect_id != descriptor.effect_id:
            raise ValueError("An instance identity cannot change effect type")
        if window is None:
            window = EffectParameterWindow(instance, owner_name, self._parent_widget)
            self._windows[key] = window
            state_changed = getattr(window.panel, "stateChanged", None)
            if state_changed is not None:
                state_changed.connect(lambda state, win=window: self._state_changed(win, state))
        else:
            self._synchronize_window(window, instance)
        window.show()
        stop_panel_timers(window.panel)
        window.raise_()
        window.activateWindow()
        return window

    def _synchronize_window(self, window: EffectParameterWindow, instance: EffectInstance) -> None:
        """Rebind snapshot-replaced objects; restore only genuinely changed values."""
        window.instance = instance
        getter = getattr(window.panel, "get_state", None)
        setter = getattr(window.panel, "set_state", None)
        if callable(getter) and callable(setter):
            target = copy.deepcopy(instance.parameters or window.initial_state)
            if "enabled" in target:
                target["enabled"] = instance.enabled
            if getter() != target:
                key = (instance.owner_id, instance.instance_id)
                self._synchronizing.add(key)
                try:
                    setter(target)
                finally:
                    self._synchronizing.discard(key)
        stop_panel_timers(window.panel)

    def reconcile(self, instances: list[EffectInstance]) -> None:
        """Follow current local session objects after commit/undo; close removed UIs."""
        current: dict[tuple[str, str], EffectInstance] = {}
        for instance in instances:
            key = (instance.owner_id, instance.instance_id)
            if key in current:
                raise ValueError("Duplicate owner/instance identity in effect session")
            current[key] = instance
        for key in list(self._windows):
            instance = current.get(key)
            window = self._windows[key]
            if instance is None or instance.effect_id != window.instance.effect_id:
                self.close_instance(key[1], key[0])
            else:
                self._synchronize_window(window, instance)

    def _state_changed(self, window: EffectParameterWindow, state: dict[str, Any]) -> None:
        key = (window.instance.owner_id, window.instance.instance_id)
        if key in self._synchronizing or self._windows.get(key) is not window:
            return
        if window.instance.parameters == state and window.instance.enabled == state.get("enabled", window.instance.enabled):
            return
        self.instanceAboutToChange.emit(window.instance)
        window.instance.parameters = copy.deepcopy(state)
        if isinstance(state.get("enabled"), bool):
            window.instance.enabled = state["enabled"]
        stop_panel_timers(window.panel)
        self.instanceChanged.emit(window.instance)

    def set_enabled(self, instance: EffectInstance, enabled: bool) -> None:
        """Caller owns the undo transaction for this programmatic rack operation."""
        instance.enabled = enabled
        if instance.parameters:
            instance.parameters["enabled"] = enabled
        window = self.window_for(instance)
        if window is not None:
            self._synchronize_window(window, instance)
            instance.parameters = window.capture_state()

    def close_instance(self, instance_id: str, owner_id: str | None = None) -> None:
        for key in list(self._windows):
            if key[1] == instance_id and (owner_id is None or key[0] == owner_id):
                window = self._windows.pop(key)
                stop_panel_timers(window.panel)
                window.close()
                window.deleteLater()

    def pause_all(self) -> None:
        for window in self._windows.values():
            stop_panel_timers(window.panel)
            window.hide()

    def dispose_all(self) -> None:
        for owner_id, instance_id in list(self._windows):
            self.close_instance(instance_id, owner_id)

    def window_for(self, instance: EffectInstance) -> EffectParameterWindow | None:
        return self._windows.get((instance.owner_id, instance.instance_id))
