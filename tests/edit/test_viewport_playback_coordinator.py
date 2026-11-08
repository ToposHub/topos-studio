"""VP-B01：Viewport 播放协调器——app 适配层把既有对象接到仲裁器。

缺口（计划 VP-B01）：仲裁器是纯领域件，真实互斥发生在
source_viewer.play/_start_source_audio 与 timeline controller.play 之间，
需要一层 QObject 适配：viewport 的 ``active_monitor_changed``（前端用
"program"/"source"）映射到领域 ID（"timeline"/"source"）后入仲裁器，
命令出仲裁器后调用真实控制器并把状态反馈回去。

契约：

- 前端 "program" ↔ 领域 "timeline" 的 ID 映射只在此层发生；
- Source 起播先暂停 Timeline（真实调用顺序可观测），播放头不动；
- ``install_viewport_playback_coordinator`` 单入口完成全部接线；
- 快照经 ``snapshot_changed`` 广播，拒绝原因经 ``status_message`` 回填；
- 宿主文本框焦点门在协调器层仍生效。
"""

from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from src.features.edit.core.viewport_playback_arbiter import (
    SOURCE,
    TIMELINE,
    ViewportPlaybackArbiter,
)
from src.features.edit.ui.viewport_playback_coordinator import (
    ViewportPlaybackCoordinator,
    install_viewport_playback_coordinator,
)

pytestmark = pytest.mark.edit_ui


class _FakeSourceWidget(QObject):
    """鸭子类型 SourceViewerWidget：play/pause/current_asset/is_playing。"""

    def __init__(self, parent: QObject | None = None,
                 log: list[tuple[str, str]] | None = None) -> None:
        super().__init__(parent)
        self._playing = False
        self._asset: Any = None
        self.calls: list[tuple[str, Any]] = []
        self.log = log if log is not None else []

    @property
    def current_asset(self) -> Any:
        return self._asset

    @property
    def is_playing(self) -> bool:
        return self._playing

    def play(self) -> None:
        self.calls.append(("play", None))
        self.log.append(("source", "play"))
        self._playing = True

    def pause(self) -> None:
        self.calls.append(("pause", None))
        self.log.append(("source", "pause"))
        self._playing = False


class _FakeTimelineController(QObject):
    """鸭子类型 EditPlaybackController。"""

    state_changed = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None,
                 log: list[tuple[str, str]] | None = None) -> None:
        super().__init__(parent)
        self._playing = False
        self.calls: list[tuple[str, Any]] = []
        self.log = log if log is not None else []

    @property
    def is_playing(self) -> bool:
        return self._playing

    def play(self) -> None:
        self.calls.append(("play", None))
        self.log.append(("timeline", "play"))
        self._playing = True
        self.state_changed.emit("playing")

    def pause(self) -> None:
        self.calls.append(("pause", None))
        self.log.append(("timeline", "pause"))
        self._playing = False
        self.state_changed.emit("paused")


class _FakeViewport(QObject):
    """鸭子类型 EditViewport（uncommitted 前端）：signal 名即合同。"""

    active_monitor_changed = pyqtSignal(str)
    mode_changed = pyqtSignal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._active = "program"
        self._dual = False

    @property
    def active_monitor(self) -> str:
        return self._active

    def _set_active(self, monitor: str) -> None:
        self._active = monitor
        self.active_monitor_changed.emit(monitor)


def _coordinator(qtbot: Any):
    source = _FakeSourceWidget()
    timeline = _FakeTimelineController()
    return ViewportPlaybackCoordinator(source, timeline), source, timeline


def test_b01_coordinator_maps_program_to_timeline(qtbot):
    coordinator, source, timeline = _coordinator(qtbot)
    viewport = _FakeViewport()
    coordinator.attach_viewport(viewport)

    viewport._set_active("source")
    assert coordinator.snapshot().active_monitor == SOURCE
    viewport._set_active("program")
    assert coordinator.snapshot().active_monitor == TIMELINE


def test_b01_coordinator_source_play_pauses_timeline_first(qtbot):
    log: list[tuple[str, str]] = []
    source = _FakeSourceWidget(log=log)
    timeline = _FakeTimelineController(log=log)
    coordinator = ViewportPlaybackCoordinator(source, timeline)
    coordinator.open_source_asset("asset-1", activate=False)
    coordinator.request_play_timeline()
    assert timeline.is_playing
    setup_len = len(log)

    coordinator.request_play_source()

    # 调用顺序：先 pause(timeline) 再 play(source)；播放头从不被触碰
    assert log[setup_len:] == [("timeline", "pause"), ("source", "play")]
    assert timeline.is_playing is False
    assert source.is_playing is True
    snap = coordinator.snapshot()
    assert snap.source_playback.playing is True
    assert snap.timeline_playback.playing is False


def test_b01_coordinator_feedback_from_controllers_updates_snapshot(qtbot):
    coordinator, source, timeline = _coordinator(qtbot)
    coordinator.open_source_asset("asset-1", activate=False)
    coordinator.request_play_timeline()

    # 控制器自发暂停（例如播放到末尾）也回填快照
    timeline.pause()

    assert coordinator.snapshot().timeline_playback.playing is False


def test_b01_coordinator_text_gate_blocks_play(qtbot):
    coordinator, source, timeline = _coordinator(qtbot)
    coordinator.open_source_asset("asset-1", activate=False)
    messages: list[str] = []
    coordinator.status_message.connect(messages.append)

    coordinator.set_text_input_active(True)
    coordinator.request_play_source()

    assert messages  # 拒绝原因回填宿主状态栏
    assert source.calls == []
    assert timeline.calls == []


def test_b01_coordinator_snapshot_signal_broadcast(qtbot):
    coordinator, source, timeline = _coordinator(qtbot)
    snapshots: list[Any] = []
    coordinator.snapshot_changed.connect(snapshots.append)

    coordinator.open_source_asset("asset-1", activate=True)

    assert snapshots
    assert snapshots[-1].source_asset_id == "asset-1"
    assert snapshots[-1].active_monitor == SOURCE


def test_b01_install_single_entry_wires_everything(qtbot):
    source = _FakeSourceWidget()
    timeline = _FakeTimelineController()
    viewport = _FakeViewport()
    timeline_panel = type("P", (), {"get_playback_controller":
                                    lambda self: timeline})()
    coordinator = install_viewport_playback_coordinator(
        viewport=viewport, source_widget=source,
        timeline_panel=timeline_panel)
    assert coordinator is not None
    assert coordinator is not None

    coordinator.open_source_asset("asset-1", activate=False)
    coordinator.request_play_timeline()
    coordinator.request_play_source()

    assert source.is_playing and not timeline.is_playing
