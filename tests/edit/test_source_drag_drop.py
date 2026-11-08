"""VP-B08：Source 拖放视图接线——载荷优先、三种意图、锁轨零写入、undo。

契约：source-clip MIME 优先于通用 media-pool asset MIME 解析；av/video/
audio 三种意图分别落成对/只视频/只音频；源区间 Out 包含并走跨帧率换算；
锁轨拒绝零写入；单命令 undo 还原轨道。
"""

from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtCore import QByteArray, QMimeData, QPoint, QPointF, Qt, QUrl
from PyQt6.QtGui import QDragEnterEvent, QDropEvent

from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.source_drag_payload import (
    MIME_TYPE_SOURCE_CLIP,
    build_source_drag_payload,
)
from src.features.edit.core.timeline_v2 import EditTrack, Rational
from src.features.edit.ui.timeline_view import TimelineView

pytestmark = pytest.mark.edit_ui

MIME_TYPE_MEDIA_POOL_ASSETS = "application/x-topos-media-pool-assets"


def _asset(asset_id: str = "src-1", fps: float = 24.0,
           channels: int = 2) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id,
        path=f"/tmp/{asset_id}.mov",
        media_type="video",
        display_name=f"{asset_id}.mov",
        metadata={
            "fps": fps, "total_frames": 240, "width": 640, "height": 480,
            "audio_info": {"channels": channels, "channel_layout": "stereo"},
        },
    )


def _setup(qtbot: Any, qapp: Any = None, *, timeline_fps: tuple[int, int] = (24, 1),
           locked: bool = False) -> tuple[TimelineView, EditProject]:
    from src.features.edit.core.timeline_v2 import TimelineSequence

    project = EditProject.create("b08")
    asset = _asset()
    project.assets[asset.asset_id] = asset
    view = TimelineView()
    qtbot.addWidget(view)
    view.resize(1200, 650)
    view.set_project(project)
    view.load_timeline(TimelineSequence(
        "b08", "B08", width=64, height=48, fps=Rational(*timeline_fps),
        video_tracks=(EditTrack("V1", "V1", "video", 0, is_locked=locked),),
        audio_tracks=(EditTrack("A1", "A1", "audio", 0, is_locked=locked),),
    ))
    view.show()
    if qapp is not None:
        qapp.processEvents()
    return view, project


def _scene_point(view: TimelineView, track_id: str, frame: int = 40):
    rect = view._layout_engine.track_rect(track_id)
    x = view._layout_engine.frame_to_x(frame)
    return QPointF(x, rect.center().y())


def _point(view: TimelineView, track_id: str, frame: int = 40):
    return view.mapFromScene(_scene_point(view, track_id, frame))


def _mime(payload_bytes: bytes, *, with_assets_mime: bool = False) -> QMimeData:
    mime = QMimeData()
    mime.setData(MIME_TYPE_SOURCE_CLIP, QByteArray(payload_bytes))
    if with_assets_mime:
        import json as _json

        mime.setData(
            MIME_TYPE_MEDIA_POOL_ASSETS,
            QByteArray(_json.dumps(["src-1"]).encode()))
        mime.setUrls([QUrl.fromLocalFile("/tmp/src-1.mov")])
    return mime


def _enter(view: TimelineView, mime: QMimeData, track_id: str = "V1",
           frame: int = 40) -> QDragEnterEvent:
    event = QDragEnterEvent(
        _point(view, track_id, frame), Qt.DropAction.CopyAction, mime,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view.dragEnterEvent(event)
    return event


def _drop(view: TimelineView, mime: QMimeData, track_id: str = "V1",
          frame: int = 40) -> QDropEvent:
    event = QDropEvent(
        _scene_point(view, track_id, frame), Qt.DropAction.CopyAction,
        mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view.dropEvent(event)
    return event


def _drag_and_drop(view: TimelineView, mime: QMimeData,
                   track_id: str = "V1", frame: int = 40) -> QDropEvent:
    assert _enter(view, mime, track_id, frame).isAccepted()
    return _drop(view, mime, track_id, frame)


# ── 拖入：载荷优先 ─────────────────────────────────────────────────


def test_b08_drag_enter_prefers_source_payload_over_assets_mime(qtbot, qapp):
    view, _ = _setup(qtbot, qapp)
    payload = build_source_drag_payload("src-1", 10, 49, "video")
    mime = _mime(payload.encode(), with_assets_mime=True)

    assert _enter(view, mime).isAccepted()
    assert view._drag_source_payload is not None
    assert view._drag_source_payload.stream_intent == "video"
    assert view._drag_file_paths == []          # URI fallback 不再抢路由


def test_b08_drag_enter_unknown_asset_rejected(qtbot, qapp):
    view, _ = _setup(qtbot, qapp)
    payload = build_source_drag_payload("not-in-project", 0, 9)
    mime = _mime(payload.encode())  # 持有引用：事件不接管 QMimeData 所有权
    event = QDragEnterEvent(
        QPoint(200, 60), Qt.DropAction.CopyAction, mime,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view.dragEnterEvent(event)
    assert not event.isAccepted()


# ── 三种意图 + 源区间 ──────────────────────────────────────────────


def test_b08_av_drop_inserts_window_with_linked_audio(qtbot, qapp):
    view, _ = _setup(qtbot, qapp)
    payload = build_source_drag_payload("src-1", 20, 59)  # 40 帧，Out 包含
    _drag_and_drop(view, _mime(payload.encode()))

    timeline = view.get_timeline()
    video = [c for c in timeline.video_tracks[0].clips]
    audio = [c for c in timeline.audio_tracks[0].clips]
    assert len(video) == 1 and len(audio) == 1
    clip = video[0]
    assert clip.source_start == 20
    assert clip.source_duration == 40
    assert clip.timeline_duration == 40            # 24→24 同帧率
    assert audio[0].timeline_start == clip.timeline_start
    assert audio[0].linked_clip_id == clip.clip_id
    # 单步 undo 还原两条轨
    view.undo()
    timeline = view.get_timeline()
    assert not timeline.video_tracks[0].clips
    assert not timeline.audio_tracks[0].clips


def test_b08_video_intent_drops_video_only(qtbot, qapp):
    view, _ = _setup(qtbot, qapp)
    payload = build_source_drag_payload("src-1", 5, 24, "video")
    _drag_and_drop(view, _mime(payload.encode()))

    timeline = view.get_timeline()
    assert len(timeline.video_tracks[0].clips) == 1
    assert not timeline.audio_tracks[0].clips


def test_b08_audio_intent_drops_on_audio_track(qtbot, qapp):
    view, _ = _setup(qtbot, qapp)
    payload = build_source_drag_payload("src-1", 0, 19, "audio")
    _drag_and_drop(view, _mime(payload.encode()))

    timeline = view.get_timeline()
    assert not timeline.video_tracks[0].clips
    assert len(timeline.audio_tracks[0].clips) == 1
    clip = timeline.audio_tracks[0].clips[0]
    assert clip.source_start == 0 and clip.source_duration == 20


def test_b08_cross_fps_duration_converted(qtbot, qapp):
    # 预置片段使时间线非空——空时间线会被首素材 conform 成同帧率（既有
    # 产品规则）；非空 25fps 时间线才做 24fps 源窗口的跨帧率换算。
    from src.features.edit.core.timeline_v2 import ClipInstance

    view, _ = _setup(qtbot, qapp, timeline_fps=(25, 1))
    seed = ClipInstance(
        clip_id="seed", asset_id="src-1", track_id="V1",
        timeline_start=0, timeline_duration=8,
        source_start=0, source_duration=8)
    current = view.get_timeline()
    view.load_timeline(type(current)(
        "b08", "B08", width=64, height=48, fps=current.fps,
        video_tracks=(EditTrack("V1", "V1", "video", 0, clips=(seed,)),),
        audio_tracks=(EditTrack("A1", "A1", "audio", 0),)))
    payload = build_source_drag_payload(
        "src-1", 0, 47, source_fps=(24, 1))        # 48 帧窗口
    _drag_and_drop(view, _mime(payload.encode()), frame=40)

    clips = view.get_timeline().video_tracks[0].clips
    assert len(clips) == 2                          # seed + 新窗口片段
    clip = [c for c in clips if c.clip_id != "seed"][0]
    assert clip.source_duration == 48               # 源域不变
    assert clip.timeline_duration == 50             # 48 × 25/24


def test_b08_locked_track_rejects_zero_write(qtbot, qapp):
    view, _ = _setup(qtbot, qapp, locked=True)
    payload = build_source_drag_payload("src-1", 0, 9)
    messages: list[str] = []
    view.status_message_requested.connect(messages.append)

    event = _drag_and_drop(view, _mime(payload.encode()))

    assert not event.isAccepted()
    assert any("locked" in m.lower() for m in messages)
    # 零写入：无命令、无片段、revision 不动（时间线对象可能因既有的
    # preview-source conform 被 copy-on-write 替换，那不是本路径的写入）
    assert not view._command_stack._undo_stack
    timeline = view.get_timeline()
    assert not timeline.video_tracks[0].clips
    assert not timeline.audio_tracks[0].clips
    assert timeline.revision == 0
