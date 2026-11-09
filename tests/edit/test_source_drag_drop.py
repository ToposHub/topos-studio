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


# ── I04：insert 意图一致性 + 事务回滚 + 身份校验 ────────────────────


def _fingerprint_payload(view, asset_id, in_frame, out_frame, intent="av"):
    from src.features.edit.core.source_time_domain import (
        media_fingerprint_for_path,
    )
    payload = build_source_drag_payload(
        asset_id, in_frame, out_frame, intent,
        media_fingerprint=media_fingerprint_for_path("/tmp/src-1.mov"))
    return payload


def test_i04_insert_video_intent_is_video_only(qtbot, qapp):
    """P1 复现修复：insert 模式下 video 意图不得再插出配对音频。"""
    view, _ = _setup(qtbot, qapp)
    view.set_edit_mode("insert")
    payload = build_source_drag_payload("src-1", 0, 19, "video")
    _drag_and_drop(view, _mime(payload.encode()))

    timeline = view.get_timeline()
    assert len(timeline.video_tracks[0].clips) == 1
    assert not timeline.audio_tracks[0].clips


def test_i04_insert_audio_intent_single_audio_clip(qtbot, qapp):
    """P1 复现修复：insert 模式下 audio 意图恰好一条音频 clip。"""
    view, _ = _setup(qtbot, qapp)
    view.set_edit_mode("insert")
    payload = build_source_drag_payload("src-1", 0, 19, "audio")
    _drag_and_drop(view, _mime(payload.encode()), track_id="A1")

    timeline = view.get_timeline()
    assert not timeline.video_tracks[0].clips
    assert len(timeline.audio_tracks[0].clips) == 1


def test_i04_insert_av_intent_keeps_linked_pair(qtbot, qapp):
    view, _ = _setup(qtbot, qapp)
    view.set_edit_mode("insert")
    payload = build_source_drag_payload("src-1", 0, 19, "av")
    _drag_and_drop(view, _mime(payload.encode()))

    timeline = view.get_timeline()
    assert len(timeline.video_tracks[0].clips) == 1
    assert len(timeline.audio_tracks[0].clips) == 1


def test_i04_failed_insert_after_track_creation_rolls_back(qtbot, qapp,
                                                            monkeypatch):
    """P2 复现修复：建轨后插入失败 → 回滚建轨，零孤立轨零 Undo 垃圾。"""
    from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence

    # 无音频轨的时间线：audio 意图 drop 到空白处会先建轨
    project = EditProject.create("i04")
    asset = _asset()
    project.assets[asset.asset_id] = asset
    view = TimelineView()
    qtbot.addWidget(view)
    view.resize(1200, 650)
    view.set_project(project)
    view.load_timeline(TimelineSequence(
        "i04", "I04", width=64, height=48, fps=Rational(24, 1),
        video_tracks=(EditTrack("V1", "V1", "video", 0),),
        audio_tracks=(),
    ))
    view.show()
    qapp.processEvents()

    monkeypatch.setattr(view, "insert_clip", lambda **_kw: False)
    view.set_edit_mode("insert")
    payload = build_source_drag_payload("src-1", 0, 9, "audio")

    # 音频区（无轨）投放 → create_track 路径
    audio_area = view._layout_engine._area.audio_area
    assert audio_area.height() > 0
    x = view._layout_engine.frame_to_x(20)
    scene_pos = QPointF(x, audio_area.center().y())
    point = view.mapFromScene(scene_pos)
    mime = _mime(payload.encode())
    enter = QDragEnterEvent(point, Qt.DropAction.CopyAction, mime,
                            Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier)
    view.dragEnterEvent(enter)
    assert enter.isAccepted()
    drop = QDropEvent(scene_pos, Qt.DropAction.CopyAction, mime,
                      Qt.MouseButton.LeftButton,
                      Qt.KeyboardModifier.NoModifier)
    view.dropEvent(drop)

    timeline = view.get_timeline()
    assert not timeline.audio_tracks          # 孤立轨已回滚
    assert not view._command_stack._undo_stack  # 不留 Undo 垃圾
    assert not timeline.video_tracks[0].clips


def test_i04_fingerprint_mismatch_rejected(qtbot, qapp):
    """跨工程同名 ID：载荷指纹与当前项目素材不符 → 整单拒绝。"""
    view, project = _setup(qtbot, qapp)
    assert project.assets["src-1"].path == "/tmp/src-1.mov"
    payload = build_source_drag_payload(
        "src-1", 0, 9, media_fingerprint="0123456789abcdef")
    mime = _mime(payload.encode())  # 持有引用
    event = QDragEnterEvent(
        _point(view, "V1", 20), Qt.DropAction.CopyAction, mime,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view.dragEnterEvent(event)
    assert not event.isAccepted()
    assert view._drag_source_payload is None


def test_i04_matching_fingerprint_accepted(qtbot, qapp):
    from src.features.edit.core.source_time_domain import (
        media_fingerprint_for_path,
    )
    view, _ = _setup(qtbot, qapp)
    payload = build_source_drag_payload(
        "src-1", 0, 9, media_fingerprint=media_fingerprint_for_path(
            "/tmp/src-1.mov"))
    assert _enter(view, _mime(payload.encode())).isAccepted()


def test_i04_range_past_end_rejected(qtbot, qapp):
    """越界范围按可信 metadata（total=240）拒绝，不信载荷自报。"""
    view, _ = _setup(qtbot, qapp)
    payload = build_source_drag_payload("src-1", 0, 999)
    event = _enter(view, _mime(payload.encode()))
    assert not event.isAccepted()
    assert view._drag_source_payload is None


def test_i04_audio_intent_on_silent_asset_rejected(qtbot, qapp):
    from src.features.edit.core.timeline_v2 import TimelineSequence

    view, project = _setup(qtbot, qapp)
    silent = MediaAsset(
        asset_id="silent-1", path="/tmp/silent.mov", media_type="video",
        display_name="silent.mov",
        metadata={"fps": 24.0, "total_frames": 240, "width": 640,
                  "height": 480})  # 无 audio_info
    project.assets[silent.asset_id] = silent
    payload = build_source_drag_payload("silent-1", 0, 9, "audio")
    event = _enter(view, _mime(payload.encode()))
    assert not event.isAccepted()


def test_i04_payload_fps_override_ignored(qtbot, qapp):
    """载荷自报 fps 谎报 25：换算仍按素材 fps 24（25fps 时间线 → ×25/24）。"""
    from src.features.edit.core.timeline_v2 import ClipInstance

    view, _ = _setup(qtbot, qapp, timeline_fps=(25, 1))
    seed = ClipInstance(
        clip_id="seed", asset_id="src-1", track_id="V1",
        timeline_start=0, timeline_duration=8,
        source_start=0, source_duration=8)
    current = view.get_timeline()
    view.load_timeline(type(current)(
        "i04", "I04", width=64, height=48, fps=current.fps,
        video_tracks=(EditTrack("V1", "V1", "video", 0, clips=(seed,)),),
        audio_tracks=(EditTrack("A1", "A1", "audio", 0),)))
    # 载荷 fps 谎报 25/1，素材真实 fps 24（见 _asset()）
    payload = build_source_drag_payload(
        "src-1", 0, 47, source_fps=(25, 1))
    _drag_and_drop(view, _mime(payload.encode()), frame=40)

    clips = view.get_timeline().video_tracks[0].clips
    clip = [c for c in clips if c.clip_id != "seed"][0]
    assert clip.source_duration == 48
    assert clip.timeline_duration == 50          # 48 × 25/24（非 48×25/25）
