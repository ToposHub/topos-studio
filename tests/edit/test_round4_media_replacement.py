"""Fourth audit: source replacement preflight, async races and metadata identity."""
from __future__ import annotations

from dataclasses import replace
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from PyQt6.QtCore import QObject, QTimer, QThreadPool
import pytest

from src.features.edit.core.commands import ProjectCommandStack
from src.features.edit.core.media_replacement import (
    merge_replacement_metadata, validate_replacement_usages,
)
from src.features.edit.core.project import EditProject, MediaAsset, TimelineSequenceRef
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui import media_pool_panel as pool

pytestmark = pytest.mark.edit_core


def _asset() -> MediaAsset:
    return MediaAsset("a", "/old.mov", "video", "old.mov", metadata={
        "fps": 24.0, "fps_num": 24, "fps_den": 1, "total_frames": 100,
        "audio_info": {"channels": 2}, "color_transfer": "pq", "is_hdr": True,
        "multicam_group": {"id": "G"}, "future": {"flag": True}},
        color_metadata={"color_transfer": "pq"}, thumbnail_key="old-thumb")


def _sequence(track_type: str = "video") -> TimelineSequence:
    clip = ClipInstance("c", "a", "v", 0, 50, 10, 50,
                        _source_fps_num=24, _source_fps_den=1,
                        _timeline_fps_num=24, _timeline_fps_den=1)
    track = EditTrack("v", "V", track_type, 0, clips=(clip,))
    seq = TimelineSequence("tl", "Timeline", fps=24.0)
    return seq.with_track_added(track)


def test_metadata_discards_old_stream_fields_and_retains_extensions() -> None:
    old = _asset()
    merged = merge_replacement_metadata(old.metadata, {"width": 2, "height": 2, "format": "PNG"})
    for key in ("fps", "total_frames", "audio_info", "color_transfer", "is_hdr"):
        assert key not in merged
    assert merged["multicam_group"] == {"id": "G"}
    merged["future"]["flag"] = False
    assert old.metadata["future"]["flag"] is True


@pytest.mark.parametrize("media_type,metadata,message", [
    ("image", {"width": 2, "height": 2}, "不同类型"),
    ("video", {"fps": 48.0, "total_frames": 100}, "帧率"),
    ("video", {"fps": 24.0, "total_frames": 59}, "过短"),
    ("video", {"fps": 24.0}, "总帧数"),
])
def test_referenced_replace_refuses_incompatible_source(media_type, metadata, message) -> None:
    with pytest.raises(ValueError, match=message):
        validate_replacement_usages(_asset(), media_type, metadata, [_sequence()])


def test_valid_same_fps_replace_and_unused_cross_type() -> None:
    validate_replacement_usages(_asset(), "video", {"fps": 24.0, "total_frames": 60}, [_sequence()])
    validate_replacement_usages(_asset(), "image", {"width": 2, "height": 2}, [])
    with pytest.raises(ValueError, match="没有可用音频"):
        validate_replacement_usages(_asset(), "video", {"fps": 24.0, "total_frames": 100}, [_sequence("audio")])


class _Host(QObject):
    _on_replace_media = pool.MediaPoolPanel._on_replace_media
    _on_replacement_probed = pool.MediaPoolPanel._on_replacement_probed

    def __init__(self) -> None:
        super().__init__()
        asset = _asset()
        self._project = EditProject("p", "P", assets={"a": asset}, revision=7)
        self._proxy_session_generation = 3
        self._replacement_requests = {}
        self._replacement_workers = {}
        self._command_stack = ProjectCommandStack()
        self._collect_timeline_sequences = lambda: []
        self._get_top_level_widget = lambda: None
        self._show_replacement_error = Mock()
        self._invalidate_proxy_job = Mock()
        self.project_changed = SimpleNamespace(emit=Mock())
        self._refresh_tree = Mock()


def test_replace_probe_keeps_ui_responsive_and_commits_full_undo(qtbot, monkeypatch, tmp_path) -> None:
    host = _Host()
    new_path = str(tmp_path / "new.png")
    (tmp_path / "new.png").write_bytes(b"png")
    monkeypatch.setattr(pool.QFileDialog, "getOpenFileName", lambda *args: (new_path, ""))
    monkeypatch.setattr(pool.QMessageBox, "question", lambda *args: pool.QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(pool, "get_media_type", lambda path: "image")
    gate = threading.Event()
    worker_thread: list[int] = []
    def probe(path):
        worker_thread.append(threading.get_ident())
        gate.wait(3)
        return {"width": 2, "height": 2, "format": "PNG"}
    monkeypatch.setattr(pool, "probe_media_metadata", probe)
    host._on_replace_media("a")
    ticks: list[bool] = []
    QTimer.singleShot(0, lambda: ticks.append(True))
    qtbot.waitUntil(lambda: bool(ticks))
    assert host._project.revision == 7 and not host._command_stack.can_undo()
    gate.set()
    qtbot.waitUntil(lambda: host._project.revision == 8)
    assert worker_thread != [threading.get_ident()]
    new_asset = host._project.assets["a"]
    assert new_asset.media_type == "image"
    assert "fps" not in new_asset.metadata and "audio_info" not in new_asset.metadata
    assert new_asset.color_metadata == {}
    assert new_asset.metadata["multicam_group"] == {"id": "G"}
    restored = host._command_stack.undo(host._project)
    assert restored.assets["a"] == _asset()


def test_old_replacement_callback_does_not_consume_new_request(tmp_path) -> None:
    host = _Host()
    old_asset = host._project.assets["a"]
    host._replacement_requests["a"] = ("new", "p", old_asset, "/new.png", 3)
    host._on_replacement_probed("a", "old", ("image", {"width": 2, "height": 2}, ""))
    assert host._replacement_requests["a"][0] == "new"
    assert host._project.revision == 7
    # A relink/replace/user-log change made during the worker invalidates its snapshot.
    host._project = replace(host._project, assets={"a": replace(old_asset, path="/different.mov")})
    host._on_replacement_probed("a", "new", ("image", {"width": 2, "height": 2}, ""))
    assert host._project.assets["a"].path == "/different.mov"
    assert not host._command_stack.can_undo()
    host._show_replacement_error.assert_not_called()


def test_replace_snapshots_nested_payload_before_background_probe(qapp, monkeypatch, tmp_path) -> None:
    host = _Host()
    new_path = str(tmp_path / "new.png")
    monkeypatch.setattr(pool.QFileDialog, "getOpenFileName", lambda *args: (new_path, ""))
    monkeypatch.setattr(pool.QMessageBox, "question", lambda *args: pool.QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(pool.QThreadPool, "globalInstance", lambda: SimpleNamespace(start=Mock()))
    host._on_replace_media("a")
    request_id = host._replacement_requests["a"][0]
    # Other integration surfaces can still mutate a nested dictionary on a
    # frozen MediaAsset. Completion must compare against the captured value.
    host._project.assets["a"].metadata["future"]["flag"] = False
    host._on_replacement_probed("a", request_id, ("image", {"width": 2, "height": 2}, ""))
    assert host._project.assets["a"].path == "/old.mov"
    assert host._project.revision == 7
    assert not host._command_stack.can_undo()
    host._show_replacement_error.assert_not_called()


def test_project_change_during_replace_confirmation_does_not_start_worker(qapp, monkeypatch) -> None:
    host = _Host()
    monkeypatch.setattr(pool.QFileDialog, "getOpenFileName", lambda *args: ("/new.png", ""))
    def confirm_after_project_changed(*_args):
        host._project = replace(host._project, project_id="another-project")
        return pool.QMessageBox.StandardButton.Yes
    monkeypatch.setattr(pool.QMessageBox, "question", confirm_after_project_changed)
    start = Mock()
    monkeypatch.setattr(pool.QThreadPool, "globalInstance", lambda: SimpleNamespace(start=start))
    host._on_replace_media("a")
    start.assert_not_called()
    assert host._replacement_requests == {}
    assert host._project.revision == 7


def test_failed_probe_and_partial_snapshots_are_zero_commit() -> None:
    host = _Host()
    old_asset = host._project.assets["a"]
    host._replacement_requests["a"] = ("bad", "p", old_asset, "/bad.mov", 3)
    host._on_replacement_probed("a", "bad", ("video", {"error": "bad container"}, "bad container"))
    assert host._project.revision == 7 and not host._command_stack.can_undo()
    assert host._show_replacement_error.call_count == 1
    host._project = replace(host._project, timelines={"missing": TimelineSequenceRef("missing", "Missing")})
    host._replacement_requests["a"] = ("partial", "p", old_asset, "/good.mov", 3)
    host._on_replacement_probed("a", "partial", ("video", {"fps": 24, "total_frames": 200}, ""))
    assert host._project.revision == 7 and not host._command_stack.can_undo()
    assert "全部时间线" in host._show_replacement_error.call_args.args[1]


@pytest.mark.parametrize("media_type,metadata", [
    ("audio", {"audio": True, "format": "WAV"}),
    ("image", {"format": "PNG", "width": None, "height": None}),
])
def test_extension_only_probe_cannot_replace_source(qapp, monkeypatch, media_type, metadata) -> None:
    monkeypatch.setattr(pool, "get_media_type", lambda path: media_type)
    monkeypatch.setattr(pool, "probe_media_metadata", lambda path: metadata)
    worker = pool.ReplacementProbeWorker("a", "request", "/bad")
    results = []
    worker.signals.finished.connect(lambda _a, _token, result: results.append(result))
    worker.run()
    assert results[0][2]


def test_proxy_same_identity_requeue_keeps_new_pending(tmp_path) -> None:
    source = tmp_path / "source.mov"
    source.write_bytes(b"source")
    stat = source.stat()
    proxy = str(tmp_path / "proxy.mov")
    host = SimpleNamespace(_proxy_session_generation=3, _pending_proxy_meta={
        proxy: (str(source), stat.st_mtime, stat.st_size, "a", 3, "new")},
        _apply_proxy_ready=Mock())
    pool.MediaPoolPanel._on_proxy_resolution_probed(host, proxy, str(source), (111, 222), "a", 3, "old")
    assert proxy in host._pending_proxy_meta
    host._apply_proxy_ready.assert_not_called()
    pool.MediaPoolPanel._on_proxy_resolution_probed(host, proxy, str(source), (640, 360), "a", 3, "new")
    assert proxy not in host._pending_proxy_meta
    assert host._apply_proxy_ready.call_args.args[-1] == (640, 360)


def test_proxy_inplace_source_change_rejects_result(tmp_path) -> None:
    source = tmp_path / "source.mov"
    source.write_bytes(b"source")
    stat = source.stat()
    proxy = "/proxy.mov"
    host = SimpleNamespace(_proxy_session_generation=3, _pending_proxy_meta={
        proxy: (str(source), stat.st_mtime, stat.st_size, "a", 3, "request")},
        _apply_proxy_ready=Mock())
    source.write_bytes(b"different source bytes")
    pool.MediaPoolPanel._on_proxy_resolution_probed(host, proxy, str(source), (640, 360), "a", 3, "request")
    host._apply_proxy_ready.assert_not_called()


def test_proxy_encode_rejects_source_replaced_in_place(tmp_path) -> None:
    source = tmp_path / "source.mov"
    source.write_bytes(b"original")
    stat = source.stat()
    asset = replace(_asset(), path=str(source), proxy_status="pending")
    host = SimpleNamespace(
        _project=EditProject("p", "P", assets={"a": asset}),
        _proxy_jobs={"a": str(source)},
        _proxy_job_signatures={"a": (stat.st_size, stat.st_mtime_ns)},
        _proxy_job_token_valid=lambda *_args: True,
        _on_proxy_error=Mock(), _invalidate_proxy_job=Mock(),
        project_changed=SimpleNamespace(emit=Mock()),
        _update_disk_proxy_services=Mock())
    source.write_bytes(b"replaced source")
    pool.MediaPoolPanel._on_proxy_finished(host, "a", "/proxy.mov", 1)
    host._on_proxy_error.assert_called_once()
    assert "源文件发生变化" in host._on_proxy_error.call_args.args[1]
    host._update_disk_proxy_services.assert_not_called()
    host.project_changed.emit.assert_not_called()


def test_replacement_source_changed_after_probe_is_zero_commit(tmp_path) -> None:
    host = _Host()
    new = tmp_path / "new.png"
    new.write_bytes(b"first")
    stat = new.stat()
    host._replacement_requests["a"] = ("request", "p", _asset(), str(new), 3)
    new.write_bytes(b"changed")
    host._on_replacement_probed("a", "request", (
        "image", {"width": 2, "height": 2}, "", (stat.st_size, stat.st_mtime_ns)))
    assert host._project.revision == 7 and not host._command_stack.can_undo()
    assert "发生变化" in host._show_replacement_error.call_args.args[1]


def test_proxy_asset_invalidation_clears_only_its_pending_probe() -> None:
    host = SimpleNamespace(
        _proxy_jobs={"a": "/a.mov", "b": "/b.mov"},
        _proxy_job_tokens={"a": 1, "b": 2},
        _proxy_job_generations={"a": 3, "b": 3},
        _pending_proxy_meta={
            "/pa.mov": ("/a.mov", 0, 0, "a", 3, "old"),
            "/pb.mov": ("/b.mov", 0, 0, "b", 3, "other"),
        })
    pool.MediaPoolPanel._invalidate_proxy_job(host, "a")
    assert set(host._pending_proxy_meta) == {"/pb.mov"}
    assert set(host._proxy_jobs) == {"b"}


@pytest.mark.parametrize("metadata", [
    {"fps_num": 24, "fps_den": "0"},
    {"fps_num": float("nan"), "fps_den": 1},
    {"fps_num": -24, "fps_den": 1},
    {"fps_num": 0, "fps_den": 1},
    {"fps_num": 24, "fps_den": 1.5},
    {"fps_num": 24},
    {"fps": float("nan")},
    {"fps": float("inf")},
    {"fps": 0},
    {"fps": -24},
    {"fps": {}},
])
def test_invalid_external_fps_becomes_actionable_value_error(metadata) -> None:
    with pytest.raises(ValueError, match="帧率无效"):
        validate_replacement_usages(_asset(), "video", metadata, [_sequence()])


def test_legacy_audio_source_uses_own_sequence_frame_base() -> None:
    audio_asset = replace(_asset(), media_type="audio", metadata={"duration_seconds": 2.0})
    audio_clip = ClipInstance("c", "a", "a", 0, 80, 0, 80)
    sequence = TimelineSequence("48fps", "T", fps=48.0, audio_tracks=(
        EditTrack("a", "A", "audio", 0, clips=(audio_clip,)),))
    validate_replacement_usages(audio_asset, "audio", {"duration_seconds": 2.0}, [sequence])


@pytest.mark.parametrize("frames", [float("inf"), float("nan"), {}, "bad", -1, 0, 1.5])
def test_invalid_frame_count_is_value_error(frames) -> None:
    with pytest.raises(ValueError, match="有效总帧数"):
        validate_replacement_usages(_asset(), "video", {"fps": 24, "total_frames": frames}, [_sequence()])
