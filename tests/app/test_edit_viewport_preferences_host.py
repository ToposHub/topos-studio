"""Versioned Viewport preferences wired through the real dock host methods."""

from __future__ import annotations

from typing import Any, Callable
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QMainWindow

from src.app.controllers.dock_layout_controller import DockLayoutMixin


class _Config:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = dict(data)
        self.save_config = MagicMock()

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any, auto_save: bool = True) -> None:
        assert auto_save is False
        self.data[key] = value


class _Splitter(QObject):
    splitterMoved = pyqtSignal(int, int)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self.current_sizes = [400, 600]

    def sizes(self) -> list[int]:
        return list(self.current_sizes)


class _Viewport(QObject):
    mode_changed = pyqtSignal(bool)
    active_monitor_changed = pyqtSignal(str)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self.dual_view = False
        self.active_monitor = "program"
        self._deliver_mode = False
        self.splitter = _Splitter(self)
        self.restored_sizes: list[int] | None = None
        self.focus_requests: list[bool] = []

    def restore_splitter_sizes(self, sizes: list[int]) -> None:
        self.restored_sizes = sizes
        self.splitter.current_sizes = list(sizes)

    def set_dual_view(self, enabled: bool) -> None:
        self.dual_view = enabled
        self.mode_changed.emit(enabled)

    def show_source(self, focus: bool = True) -> None:
        self.focus_requests.append(focus)
        self.active_monitor = "source"
        self.active_monitor_changed.emit("source")

    def show_program(self, focus: bool = True) -> None:
        self.focus_requests.append(focus)
        self.active_monitor = "program"
        self.active_monitor_changed.emit("program")


class _Host(DockLayoutMixin, QMainWindow):
    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__()
        self.current_page = "Edit"
        self.config_manager = _Config(data)
        self.edit_viewport = _Viewport(self)
        self._setup_edit_viewport_preferences()


@pytest.fixture
def make_host(qtbot: Any) -> Callable[[dict[str, Any]], _Host]:
    def create(data: dict[str, Any]) -> _Host:
        host = _Host(data)
        qtbot.addWidget(host)
        return host

    return create


def test_migrates_legacy_preferences_and_keeps_other_config(make_host: Any) -> None:
    host = make_host({
        "edit_viewport_dual_view": True,
        "edit_viewport_splitter_sizes": [300, 700],
        "page_layouts": {"Color": "unchanged"},
    })
    assert host.edit_viewport.dual_view
    assert host.edit_viewport.restored_sizes == [300, 700]
    assert host.edit_viewport.active_monitor == "program"
    assert host.edit_viewport.focus_requests == [False]
    assert host.config_manager.data["edit_viewport_preferences"] == {
        "version": 1, "dual_view": True,
        "splitter_sizes": [300, 700], "single_view_target": "timeline",
    }
    assert host.config_manager.data["page_layouts"] == {"Color": "unchanged"}
    host.config_manager.save_config.assert_not_called()
    assert host._edit_viewport_preferences_timer.isActive()
    assert host._edit_viewport_preferences_timer.interval() == 300


def test_versioned_preferences_win_and_restore_source_without_focus(make_host: Any) -> None:
    raw = {
        "version": 1, "dual_view": False,
        "splitter_sizes": [200, 800], "single_view_target": "source",
    }
    host = make_host({
        "edit_viewport_preferences": raw,
        "edit_viewport_dual_view": True,
    })
    assert not host.edit_viewport.dual_view
    assert host.edit_viewport.active_monitor == "source"
    assert host.edit_viewport.restored_sizes == [200, 800]
    assert host.edit_viewport.focus_requests == [False]
    assert not host._edit_viewport_preferences_timer.isActive()


def test_invalid_versioned_config_normalizes_with_safe_defaults(make_host: Any) -> None:
    host = make_host({"edit_viewport_preferences": {
        "version": 99, "dual_view": True, "single_view_target": "source",
    }})
    assert not host.edit_viewport.dual_view
    assert host.edit_viewport.active_monitor == "program"
    assert host.config_manager.data["edit_viewport_preferences"] == {
        "version": 1, "dual_view": False,
        "splitter_sizes": None, "single_view_target": "timeline",
    }


def test_active_monitor_signal_saves_target_with_debounce(make_host: Any, qtbot: Any) -> None:
    host = make_host({"edit_viewport_preferences": {
        "version": 1, "dual_view": False,
        "splitter_sizes": [400, 600], "single_view_target": "timeline",
    }})
    host.edit_viewport.show_source(focus=False)
    assert host.config_manager.data["edit_viewport_preferences"]["single_view_target"] == "source"
    host.edit_viewport.show_program(focus=False)
    assert host.config_manager.data["edit_viewport_preferences"]["single_view_target"] == "timeline"
    host.config_manager.save_config.assert_not_called()
    qtbot.waitUntil(lambda: host.config_manager.save_config.call_count == 1, timeout=1500)
    assert not host._edit_viewport_preferences_timer.isActive()


def test_single_view_does_not_overwrite_last_valid_dual_sizes(make_host: Any) -> None:
    host = make_host({"edit_viewport_preferences": {
        "version": 1, "dual_view": True,
        "splitter_sizes": [400, 600], "single_view_target": "timeline",
    }})
    viewport = host.edit_viewport
    viewport.splitter.current_sizes = [350, 650]
    viewport.splitter.splitterMoved.emit(350, 1)
    viewport.splitter.current_sizes = [0, 1000]
    viewport.set_dual_view(False)
    viewport.show_source(focus=False)
    stored = host.config_manager.data["edit_viewport_preferences"]
    assert stored["splitter_sizes"] == [350, 650]
    assert stored["dual_view"] is False
    viewport.set_dual_view(True)  # transient invalid sizes must not erase them either
    assert host.config_manager.data["edit_viewport_preferences"]["splitter_sizes"] == [350, 650]


@pytest.mark.parametrize("page,borrowed", [("Deliver", False), ("Edit", True)])
def test_deliver_changes_are_not_saved(make_host: Any, page: str, borrowed: bool) -> None:
    raw = {
        "version": 1, "dual_view": True,
        "splitter_sizes": [400, 600], "single_view_target": "timeline",
    }
    host = make_host({"edit_viewport_preferences": raw})
    host.current_page = page
    host.edit_viewport._deliver_mode = borrowed
    host.edit_viewport.show_source(focus=False)
    host.edit_viewport.set_dual_view(False)
    host.edit_viewport.splitter.splitterMoved.emit(0, 1)
    assert host.config_manager.data["edit_viewport_preferences"] == raw
    assert not host._edit_viewport_preferences_timer.isActive()
