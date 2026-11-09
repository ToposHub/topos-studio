"""I03（复查计划）：capability 宿主与 Match Frame 宿主执行——真实对象集成。

- 注册表前置条件按真实状态回填 EditViewport 动作（空项目/仅 Source/
  仅 Timeline/Deliver 的菜单状态可信）；
- Timeline→Source 复用 ``resolve_match_frame_at_playhead`` + 既有
  ``EditPage._on_clip_open_in_source`` 打开链；拒绝零写入回状态栏；
- Source→Timeline：唯一使用直接跳精确时间线帧（retimed 片段不再用朴素
  公式）；多处使用弹候选，取消不改变播放头；
- 无 handler 的键不执行（保持禁用语义）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from PyQt6.QtCore import QObject, pyqtSignal

from src.app.controllers.viewport_capability_host import (
    ViewportCapabilityHost,
)
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)
from src.features.edit.core.viewport_preferences import (  # noqa: F401  (回归 import 保护)
    parse_viewport_preferences,
)

pytestmark = pytest.mark.edit_ui


class _ViewportStub(QObject):
    capability_action_requested = pyqtSignal(str)
    capabilities_context_requested = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.refreshed: list[dict] = []

    def refresh_capabilities(self, caps: dict) -> None:
        self.refreshed.append(caps)


class _Selection:
    def __init__(self, ids: list[str]) -> None:
        self.selected_clip_ids = list(ids)


class _TimelineView:
    def __init__(self, sequence: TimelineSequence | None,
                 selection: _Selection | None = None) -> None:
        self._timeline_obj = sequence
        self._selection = selection or _Selection([])
        self._playhead_frame = 0
        self.set_playhead_calls: list[int] = []

    def get_timeline(self):
        return self._timeline_obj

    def set_playhead(self, frame: int) -> None:
        self._playhead_frame = int(frame)
        self.set_playhead_calls.append(int(frame))


class _TimelinePanel:
    def __init__(self, view: _TimelineView) -> None:
        self.timeline_view = view


class _EditPage:
    def __init__(self) -> None:
        self.opened: list[tuple[str, int]] = []

    def _on_clip_open_in_source(self, clip_id: str, frame: int) -> None:
        self.opened.append((clip_id, int(frame)))


class _SourceWidget:
    def __init__(self) -> None:
        self.current_asset: Any = None
        self.current_frame = 0


class _StatusBar:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def showMessage(self, text: str, timeout: int = 0) -> None:
        self.messages.append(text)


def _window(qtbot: Any, sequence: TimelineSequence | None = None,
            *, selection: list[str] | None = None,
            source_asset: Any = None, source_frame: int = 0,
            current_page: str = "Edit") -> SimpleNamespace:
    view = _TimelineView(sequence,
                         _Selection(selection) if selection else None)
    window = SimpleNamespace(
        edit_viewport=_ViewportStub(),
        timeline_panel=_TimelinePanel(view),
        source_viewer_widget=_SourceWidget(),
        edit_page=_EditPage(),
        current_page=current_page,
        _status=_StatusBar(),
    )
    window.source_viewer_widget.current_asset = source_asset
    window.source_viewer_widget.current_frame = source_frame

    def _statusBar():  # noqa: ANN202 — duck for MainWindow.statusBar()
        return window._status

    window.statusBar = _statusBar
    return window


def _clip(clip_id: str, asset_id: str, track_id: str, start: int,
          duration: int, source_start: int = 0, speed: float = 1.0
          ) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id, asset_id=asset_id, track_id=track_id,
        timeline_start=start, timeline_duration=duration,
        source_start=source_start, source_duration=max(1, int(duration)),
        speed=speed)


def _sequence(*clips: ClipInstance) -> TimelineSequence:
    video_ids = {c.track_id for c in clips if not c.track_id.startswith("A")}
    return TimelineSequence(
        "seq", "Seq", width=64, height=48, fps=Rational(24, 1),
        video_tracks=tuple(
            EditTrack(t, t, "video", index, clips=tuple(
                c for c in clips if c.track_id == t))
            for index, t in enumerate(sorted(video_ids)) or ()),
        audio_tracks=(),
    )


def _asset(asset_id: str) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id, path=f"/tmp/i03_{asset_id}.mov",
        media_type="video", display_name=f"{asset_id}.mov",
        metadata={"fps": 24.0, "total_frames": 240, "width": 640,
                  "height": 480})


# ── 注册表回填（真实状态 → 菜单状态可信） ─────────────────────────


def test_i03_refresh_feeds_registry_caps_to_viewport(qtbot, qapp):
    window = _window(qtbot, None, source_asset=None)
    host = ViewportCapabilityHost(window)
    caps = window.edit_viewport.refreshed[-1]
    assert caps["match_frame.source_to_timeline"].supported is False
    assert caps["match_frame.timeline_to_source"].supported is False
    # 状态变化（菜单展开请求）→ 重评
    window.edit_viewport.capabilities_context_requested.emit()
    assert len(window.edit_viewport.refreshed) >= 2


def test_i03_full_context_enables_match_frame(qtbot, qapp):
    seq = _sequence(_clip("c1", "a1", "V1", 0, 100))
    window = _window(qtbot, seq, source_asset=_asset("a1"))
    ViewportCapabilityHost(window)
    caps = window.edit_viewport.refreshed[-1]
    assert caps["match_frame.source_to_timeline"].supported is True
    assert caps["match_frame.timeline_to_source"].supported is True


def test_i03_deliver_disables_both_match_frame_directions(qtbot, qapp):
    seq = _sequence(_clip("c1", "a1", "V1", 0, 100))
    window = _window(qtbot, seq, source_asset=_asset("a1"),
                     current_page="Deliver")
    ViewportCapabilityHost(window)
    caps = window.edit_viewport.refreshed[-1]
    assert caps["match_frame.source_to_timeline"].supported is False
    assert caps["match_frame.timeline_to_source"].supported is False


# ── Timeline→Source：既有打开链 ────────────────────────────────────


def test_i03_timeline_match_frame_opens_source_at_authoritative_frame(
        qtbot, qapp):
    clip = _clip("c1", "a1", "V1", 0, 100, source_start=10)
    seq = _sequence(clip)
    window = _window(qtbot, seq, source_asset=None)
    host = ViewportCapabilityHost(window)
    window.timeline_panel.timeline_view._playhead_frame = 30

    host._on_action("timeline.match_frame")

    # 权威映射：源帧 = source_start + 30（非朴素播放头值）
    assert window.edit_page.opened == [("c1", 40)]


def test_i03_timeline_match_frame_empty_playhead_rejects_zero_write(
        qtbot, qapp):
    seq = _sequence(_clip("c1", "a1", "V1", 0, 100))
    window = _window(qtbot, seq)
    host = ViewportCapabilityHost(window)
    window.timeline_panel.timeline_view._playhead_frame = 150  # 无片段覆盖

    host._on_action("timeline.match_frame")

    assert window.edit_page.opened == []
    assert window._status.messages


# ── Source→Timeline：唯一使用直跳 / 多使用候选 ─────────────────────


def test_i03_source_match_frame_single_use_jumps_exact_frame(qtbot, qapp):
    # 2x retimed 片段：朴素公式 start + (frame − source_start) 会错帧
    clip = _clip("c1", "a1", "V1", 0, 100, source_start=0, speed=2.0)
    seq = _sequence(clip)
    window = _window(qtbot, seq, source_asset=_asset("a1"), source_frame=50)
    host = ViewportCapabilityHost(window)

    host._on_action("source.match_frame")

    # 权威反查：2x 变速下源帧 50 出现在时间线帧 25
    assert window.timeline_panel.timeline_view.set_playhead_calls == [25]


def test_i03_source_match_frame_multiple_uses_candidates(qtbot, qapp):
    seq = _sequence(
        _clip("c1", "a1", "V1", 0, 100, source_start=0),
        _clip("c2", "a1", "V1", 200, 100, source_start=0),
    )
    window = _window(qtbot, seq, source_asset=_asset("a1"), source_frame=42)
    host = ViewportCapabilityHost(window)

    host._on_action("source.match_frame")

    view = window.timeline_panel.timeline_view
    assert view.set_playhead_calls == []          # 不默认第一处
    assert host._candidate_menu is not None
    actions = host._candidate_menu.actions()
    assert len(actions) == 2

    actions[1].trigger()                          # 选第二处
    assert view.set_playhead_calls == [242]


def test_i03_source_match_frame_no_uses_status_only(qtbot, qapp):
    seq = _sequence(_clip("c1", "other", "V1", 0, 100))
    window = _window(qtbot, seq, source_asset=_asset("a1"), source_frame=42)
    host = ViewportCapabilityHost(window)

    host._on_action("source.match_frame")

    assert window.timeline_panel.timeline_view.set_playhead_calls == []
    assert window._status.messages


def test_i03_unknown_action_key_is_ignored(qtbot, qapp):
    window = _window(qtbot, None)
    host = ViewportCapabilityHost(window)
    host._on_action("proxy.prefer_proxy")  # 无 handler：不执行不崩溃
    assert window.edit_page.opened == []
