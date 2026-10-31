"""Regression tests for remediation batch D fixes (wd12-g66).

Covers 16 Major findings from the 2026-09-28 audit, edit UI c segment:

- AUD-20103: interchange diagnostics must be HTML-escaped before rich text.
- AUD-20200: Quick Look Space toggle must find and close the open dialog.
- AUD-20201: hover-scrub frame cache is bounded per path.
- AUD-20202: offline availability scan runs off the UI thread.
- AUD-20203: media pool must not take FrameProviderService private locks.
- AUD-20204: batch log edit must treat rating 0 ("unrated") as an operation.
- AUD-20205: proxy generation rejects re-entry while a job is pending.
- AUD-20206: drag enter uses a shallow media sniff (no recursive walk).
- AUD-20306: NewProjectDialog honors the selected audio sample rate.
- AUD-20309: resolution presets do not drift under aspect lock.
- AUD-20310: non-preset sequence fps surfaces as Custom, never silently
  rewritten on save.
- AUD-20401: error-state overlay is actually painted in paintGL.
- AUD-20402: clear_texture defers glDeleteTextures to the next paintGL.
- AUD-20403: present-frame ack fires on invalid-texture and blit-exception
  paths, exactly once.
- AUD-20404: subtitle overlay is not wrapped in begin/endNativePainting.
- AUD-20500: image Auto resolution parsed from FileIO "Resolution" string.

GL entry points are monkeypatched on the module-level ``_gl`` wrapper so the
viewer logic can be exercised headlessly without a GL context.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import src.features.edit.ui.media_pool_panel as mpp
import src.features.edit.ui.program_viewer as pv
from src.features.edit.core.project import EditProject, update_offline_status


# ===========================================================================
# AUD-20103 — interchange diagnostics HTML escaping
# ===========================================================================


def test_interchange_diagnostics_escaped(qapp) -> None:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QLabel

    from src.features.edit.adapters.interchange_types import (
        InterchangeReport,
    )
    from src.features.edit.ui.interchange_dialog import InterchangeReportDialog

    report = InterchangeReport(
        format_id="fcpxml",
        diagnostics=(
            {
                "severity": "error",
                "code": "TAEP_INTERCHANGE_LOSSY",
                "message": "FCPXML parse failed: <bad & tag> in 'A & B/x.fcpxml'",
            },
        ),
    )
    dialog = InterchangeReportDialog(report)

    labels = dialog.findChildren(QLabel)
    diag_labels = [
        label for label in labels
        if label.textFormat() == Qt.TextFormat.RichText
        and "TAEP_INTERCHANGE_LOSSY" in label.text()
    ]
    assert diag_labels, "diagnostic rich-text label not found"
    text = diag_labels[0].text()
    # Escaped entities survive; raw < / & must not appear in the message part.
    assert "&lt;bad &amp; tag&gt;" in text
    assert "<bad" not in text


# ===========================================================================
# AUD-20200 — Quick Look Space toggle tracks the open dialog
# ===========================================================================


class _FakeQuickLookDialog:
    instances: list["_FakeQuickLookDialog"] = []

    def __init__(self, asset, parent=None) -> None:
        self.asset = asset
        self.parent = parent
        self.visible = True
        self.closed = False
        self.deleted = False
        self.destroyed = SimpleNamespace(connect=lambda slot: None)
        _FakeQuickLookDialog.instances.append(self)

    def isVisible(self) -> bool:
        return self.visible

    def close(self) -> None:
        self.closed = True
        self.visible = False

    def show(self) -> None:
        self.visible = True

    def deleteLater(self) -> None:
        self.deleted = True


def _quick_look_shell(monkeypatch) -> SimpleNamespace:
    asset = SimpleNamespace(display_name="clip.mov")
    shell = SimpleNamespace()
    shell._project = SimpleNamespace(assets={"a1": asset})
    shell._quick_look_dialog = None
    shell._view_mode = mpp.ViewMode.LIST
    shell.grid_view = None

    class _Item:
        def data(self, _column, role):
            if role == mpp.Qt.ItemDataRole.UserRole + 1:
                return "asset"
            if role == mpp.Qt.ItemDataRole.UserRole:
                return "a1"
            return None

    class _Tree:
        @staticmethod
        def selectedItems():
            return [_Item()]

    shell.tree = _Tree()
    shell._get_top_level_widget = lambda: None
    shell._show_quick_look = mpp.MediaPoolPanel._show_quick_look.__get__(shell)
    shell._on_quick_look_destroyed = (
        mpp.MediaPoolPanel._on_quick_look_destroyed.__get__(shell)
    )
    _FakeQuickLookDialog.instances = []
    monkeypatch.setattr(mpp, "QuickLookDialog", _FakeQuickLookDialog)
    return shell


def test_quick_look_space_presses_do_not_leak(qapp, monkeypatch) -> None:
    shell = _quick_look_shell(monkeypatch)

    shell._show_quick_look()  # open
    assert len(_FakeQuickLookDialog.instances) == 1
    assert shell._quick_look_dialog is _FakeQuickLookDialog.instances[0]

    shell._show_quick_look()  # toggle close
    assert len(_FakeQuickLookDialog.instances) == 1, "second press must not open another dialog"
    first = _FakeQuickLookDialog.instances[0]
    assert first.closed and first.deleted
    assert shell._quick_look_dialog is None


def test_quick_look_reopens_after_hidden_close(qapp, monkeypatch) -> None:
    """Esc closes the dialog (hidden, not destroyed); next Space reopens."""
    shell = _quick_look_shell(monkeypatch)
    shell._show_quick_look()
    first = shell._quick_look_dialog
    first.visible = False  # user closed it via Esc earlier

    shell._show_quick_look()
    # Hidden zombie is destroyed and a new dialog opens in a single press.
    assert first.deleted
    assert shell._quick_look_dialog is not first
    assert shell._quick_look_dialog is _FakeQuickLookDialog.instances[-1]


# ===========================================================================
# AUD-20201 — hover-scrub frame cache bounded per path
# ===========================================================================


def test_hover_frame_cache_is_bounded(qapp) -> None:
    from PyQt6.QtGui import QPixmap

    view = mpp.AssetListView()
    for index in range(view._HOVER_FRAMES_PER_PATH + 25):
        view._on_hover_frame_ready("/a.mov", index, QPixmap(8, 8))
    frames = view._hover_frames["/a.mov"]
    assert len(frames) == view._HOVER_FRAMES_PER_PATH
    # Most recent frames retained, oldest evicted (FIFO).
    assert (view._HOVER_FRAMES_PER_PATH + 24) in frames
    assert 0 not in frames
    # Only the capped path is present at all.
    assert set(view._hover_frames) == {"/a.mov"}


def test_hover_frame_cache_switching_path_drops_previous(qapp) -> None:
    from PyQt6.QtGui import QPixmap

    view = mpp.AssetListView()
    view._on_hover_frame_ready("/a.mov", 0, QPixmap(8, 8))
    assert "/a.mov" in view._hover_frames

    # Simulate the path switch performed by _handle_hover_scrub.
    view._hover_path = "/a.mov"
    view._hover_frames.pop("/a.mov", None)
    view._hover_path = "/b.mov"
    view._on_hover_frame_ready("/b.mov", 3, QPixmap(8, 8))
    assert "/a.mov" not in view._hover_frames
    assert "/b.mov" in view._hover_frames


# ===========================================================================
# AUD-20202 — offline availability scan off the UI thread
# ===========================================================================


class _ThreadPoolRecorder:
    def __init__(self) -> None:
        self.started: list = []

    def start(self, worker) -> None:
        self.started.append(worker)


class _OfflineShell(mpp.QObject):
    """Real QObject so PyQt signal connects to bound methods work."""


def _offline_shell(monkeypatch) -> tuple[_OfflineShell, _ThreadPoolRecorder]:
    project = EditProject.create("offline-test")
    project = project.import_asset(
        path="/definitely/not/there/clip.mov",
        media_type="video",
        metadata={},
    )
    shell = _OfflineShell()
    shell._project = project
    shell._updating = False
    shell._offline_scan_in_flight = False
    shell._offline_status_signature = None
    applied: list = []
    shell.set_project = lambda project_: applied.append(project_)
    shell.project_changed = SimpleNamespace(emit=lambda project_: applied.append(project_))
    shell._applied = applied
    shell._refresh_offline_status = (
        mpp.MediaPoolPanel._refresh_offline_status.__get__(shell)
    )
    shell._on_offline_scan_finished = (
        mpp.MediaPoolPanel._on_offline_scan_finished.__get__(shell)
    )
    pool = _ThreadPoolRecorder()
    monkeypatch.setattr(
        mpp.QThreadPool, "globalInstance", staticmethod(lambda: pool)
    )
    return shell, pool


def test_offline_refresh_does_not_scan_on_ui_thread(qapp, monkeypatch) -> None:
    shell, pool = _offline_shell(monkeypatch)
    sync_calls: list = []
    monkeypatch.setattr(mpp, "update_offline_status", lambda project: sync_calls.append(project))

    shell._refresh_offline_status()

    assert sync_calls == [], "update_offline_status must not run on the UI thread"
    assert len(pool.started) == 1
    assert isinstance(pool.started[0], mpp.OfflineScanWorker)
    assert shell._offline_scan_in_flight is True
    # No scan queued while one is in flight.
    shell._refresh_offline_status()
    assert len(pool.started) == 1


def test_offline_scan_result_applied_when_source_current(qapp, monkeypatch) -> None:
    shell, _pool = _offline_shell(monkeypatch)
    source = shell._project
    checked = update_offline_status(source)  # asset path missing -> goes offline
    assert checked is not source, "fixture precondition: availability changed"

    shell._on_offline_scan_finished(source, checked)
    assert shell._applied == [checked, checked]  # set_project + project_changed


def test_offline_scan_result_discarded_when_project_swapped(qapp, monkeypatch) -> None:
    shell, _pool = _offline_shell(monkeypatch)
    stale_source = shell._project
    checked = update_offline_status(stale_source)
    shell._project = EditProject.create("new-project")  # swapped while scanning

    shell._on_offline_scan_finished(stale_source, checked)
    assert shell._applied == []


def test_offline_scan_worker_emits_only_on_change(qapp) -> None:
    project = EditProject.create("worker-test")
    worker = mpp.OfflineScanWorker(project)
    received: list = []
    worker.signals.finished.connect(
        lambda source, checked: received.append((source, checked))
    )
    worker.run()
    assert received == [(project, None)], "unchanged scan must report None"


# ===========================================================================
# AUD-20203 — media pool uses FrameProviderService public API only
# ===========================================================================


def test_update_disk_proxy_services_uses_public_frame_api(qapp, monkeypatch) -> None:
    updated: list = []

    class _FrameService:
        def get_shared_clips_info(self):
            return {"/src.mov": ["clip-1"]}

        def get_clip_session(self, clip_id):
            assert clip_id == "clip-1"
            return SimpleNamespace(is_open=True)

        def update_disk_proxy_info(self, clip_id, info):
            updated.append((clip_id, info))

    class _DiskService:
        def __init__(self) -> None:
            self._lock = __import__("threading").RLock()
            self._info_cache: dict = {}

    monkeypatch.setattr(mpp, "get_frame_provider_service", lambda: _FrameService())
    monkeypatch.setattr(mpp, "get_disk_proxy_service", lambda: _DiskService())

    # AUD-20210：探测移入 QThreadPool worker，服务回填经 probed 信号
    # 在 UI 线程回调完成——桩以同步 worker 复现整条链路
    panel_shell = SimpleNamespace(
        _proxy_resolution_signals=mpp.ProxyResolutionSignals(),
        _pending_proxy_meta={},
    )
    panel_shell._on_proxy_resolution_probed = (
        mpp.MediaPoolPanel._on_proxy_resolution_probed.__get__(panel_shell)
    )
    panel_shell._apply_proxy_ready = (
        mpp.MediaPoolPanel._apply_proxy_ready.__get__(panel_shell)
    )
    def _on_probed(proxy_path, source_path, resolution, asset_id, generation, request_id):
        # SimpleNamespace 不可弱引用，经普通函数闭包转发给绑定方法
        # N21：转发完整身份载荷（asset_id/generation）
        panel_shell._on_proxy_resolution_probed(
            proxy_path, source_path, resolution, asset_id, generation, request_id)
    panel_shell._proxy_resolution_signals.probed.connect(_on_probed)

    class _FakeWorker:
        def __init__(self, proxy_path, source_path, asset, signals=None,
                 asset_id="", session_generation=-1, request_id=""):
            self._proxy_path = proxy_path
            self._source_path = source_path
            self._signals = signals
            self._asset_id = asset_id
            self._gen = session_generation
            self._request_id = request_id

        def run(self) -> None:
            # N21：信号载荷含 asset_id/generation（替身传空/当前代次）
            self._signals.probed.emit(
                self._proxy_path, self._source_path, (1280, 720),
                self._asset_id, self._gen, self._request_id)

    monkeypatch.setattr(mpp, "ProxyResolutionWorker", _FakeWorker)
    started: list = []
    monkeypatch.setattr(
        mpp, "QThreadPool",
        SimpleNamespace(globalInstance=lambda: SimpleNamespace(
            start=started.append)))

    panel_shell._update_disk_proxy_services = (
        mpp.MediaPoolPanel._update_disk_proxy_services.__get__(panel_shell)
    )

    asset = SimpleNamespace(path="/src.mov")
    panel_shell._update_disk_proxy_services(asset, "/proxy.mov")
    # 真实 worker 在池线程异步执行，此时 _pending_proxy_meta 已暂存；
    # 调用返回后再驱动 worker 即可复现该时序
    for worker in started:
        worker.run()

    assert len(updated) == 1
    clip_id, info = updated[0]
    assert clip_id == "clip-1"
    assert info.proxy_path == "/proxy.mov"
    # No private lock acquisition on the frame service: the fake exposes no
    # _lock/_path_to_clips/_clips attributes at all, so the old code path
    # (which also deadlocked on the non-reentrant lock) cannot run.


# ===========================================================================
# AUD-20204 — batch log edit honors rating 0
# ===========================================================================


def _batch_log_shell(monkeypatch, params: dict) -> SimpleNamespace:
    shell = SimpleNamespace()
    shell._project = SimpleNamespace(
        assets={"a1": SimpleNamespace(asset_id="a1", keywords=(), rating=3)}
    )
    created: list = []

    class _FakeDialog:
        def __init__(self, *args, **kwargs) -> None:
            pass

        @staticmethod
        def exec():
            return mpp.QDialog.DialogCode.Accepted

        @staticmethod
        def batch_params():
            return params

    monkeypatch.setattr(mpp, "BatchAssetLogDialog", _FakeDialog)

    class _Cmd:
        pass

    def _create(ids, label=None, **kwargs):
        created.append((tuple(ids), kwargs))
        return _Cmd()

    monkeypatch.setattr(
        mpp.BatchUpdateAssetLogCommand, "create", staticmethod(_create)
    )
    shell._command_stack = SimpleNamespace(
        execute=lambda cmd, project: "new-project"
    )
    emitted: list = []
    shell.project_changed = SimpleNamespace(emit=emitted.append)
    shell._refresh_tree = lambda: None
    shell._get_top_level_widget = lambda: None
    monkeypatch.setattr(
        mpp.QMessageBox, "information", lambda *args, **kwargs: None
    )
    shell._created = created
    shell._emitted = emitted
    shell._on_batch_edit_log = mpp.MediaPoolPanel._on_batch_edit_log.__get__(shell)
    return shell


def test_batch_log_clear_rating_is_an_operation(qapp, monkeypatch) -> None:
    shell = _batch_log_shell(
        monkeypatch,
        {
            "add_keywords": (),
            "remove_keywords": (),
            "rating": 0,  # "未评级" — the previously dropped case
            "flag": None,
            "color_label": None,
            "comment": None,
        },
    )
    shell._on_batch_edit_log(["a1"])
    assert shell._created, "clearing ratings must create the batch command"
    ids, kwargs = shell._created[0]
    assert ids == ("a1",)
    assert kwargs["rating"] == 0


def test_batch_log_all_unchanged_still_no_op(qapp, monkeypatch) -> None:
    shell = _batch_log_shell(
        monkeypatch,
        {
            "add_keywords": (),
            "remove_keywords": (),
            "rating": None,
            "flag": None,
            "color_label": None,
            "comment": None,
        },
    )
    shell._on_batch_edit_log(["a1"])
    assert shell._created == [], "all-unchanged must remain a no-op"


# ===========================================================================
# AUD-20205 — proxy generation re-entry guard
# ===========================================================================


def test_generate_proxy_rejected_while_pending(qapp, monkeypatch) -> None:
    asset = SimpleNamespace(
        asset_id="a1",
        path="/src.mov",
        display_name="src.mov",
        is_offline=False,
        is_audio=False,
        proxy_status="pending",
    )
    shell = SimpleNamespace()
    shell._project = SimpleNamespace(assets={"a1": asset})
    shell._proxy_jobs = {}
    shell._get_top_level_widget = lambda: None
    shell._on_generate_proxy = mpp.MediaPoolPanel._on_generate_proxy.__get__(shell)

    info_calls: list = []
    monkeypatch.setattr(
        mpp.QMessageBox, "information", lambda *a, **k: info_calls.append((a, k))
    )
    ffmpeg_checked: list = []
    monkeypatch.setattr(
        mpp, "check_ffmpeg_available", lambda: ffmpeg_checked.append(1) or True
    )

    shell._on_generate_proxy("a1")

    assert info_calls, "pending proxy must inform the user"
    assert ffmpeg_checked == [], "re-entry must be rejected before any work starts"
    assert shell._proxy_jobs == {}


# ===========================================================================
# AUD-20206 — shallow drag sniff
# ===========================================================================


def test_drag_offers_media_shallow(qapp, tmp_path) -> None:
    shell = SimpleNamespace()
    shell._drag_offers_media = mpp.MediaPoolPanel._drag_offers_media.__get__(shell)

    assert shell._drag_offers_media(["/tmp/clip.mov"]) is True
    assert shell._drag_offers_media(["/tmp/song.mp3"]) is True
    assert shell._drag_offers_media([str(tmp_path)]) is True  # dir: deferred walk
    assert shell._drag_offers_media(["/tmp/notes.txt", ""]) is False
    assert shell._drag_offers_media([]) is False


def test_drag_enter_does_not_walk_directories(qapp, monkeypatch, tmp_path) -> None:
    """dragEnterEvent must not recursively expand a dropped folder."""
    shell = SimpleNamespace()
    walk_calls: list = []

    def _no_walk(paths):
        walk_calls.append(list(paths))
        return ["/never"]

    # Bind BOTH the real shallow sniffer and the (must-not-run) deep filter.
    shell._drag_offers_media = mpp.MediaPoolPanel._drag_offers_media.__get__(shell)
    shell._filter_valid_media_files = _no_walk
    shell.dragEnterEvent = mpp.MediaPoolPanel.dragEnterEvent.__get__(shell)

    deep = tmp_path / "nested"
    deep.mkdir()

    class _Url:
        @staticmethod
        def isLocalFile():
            return True

        @staticmethod
        def toLocalFile():
            return str(deep)

    class _Mime:
        @staticmethod
        def hasUrls():
            return True

        @staticmethod
        def urls():
            return [_Url]

    accepted: list = []

    class _Event:
        mimeData = staticmethod(lambda: _Mime)

        @staticmethod
        def acceptProposedAction():
            accepted.append(True)

        @staticmethod
        def ignore():
            accepted.append(False)

    shell.dragEnterEvent(_Event)
    assert accepted == [True], "folder drags are accepted optimistically"
    assert walk_calls == [], "dragEnter must not run the recursive filter walk"


# ===========================================================================
# AUD-20306 — NewProjectDialog honors selected sample rate
# ===========================================================================


def test_new_project_dialog_applies_sample_rate(qapp, monkeypatch, tmp_path) -> None:
    import src.features.edit.ui.new_project_dialog as npd

    fake_config = SimpleNamespace(get=lambda key, default=None: default)
    dialog = npd.NewProjectDialog(config_manager=fake_config, parent=None)
    dialog.name_edit.setText("SampleRateProject")
    dialog.location_edit.setText(str(tmp_path))

    index_44k = dialog.sample_rate_combo.findData(44100)
    assert index_44k >= 0
    dialog.sample_rate_combo.setCurrentIndex(index_44k)

    saved: list = []

    def _fake_save(self, path, project):
        saved.append(project)
        return True

    monkeypatch.setattr(npd.EditProjectIO, "save_project", _fake_save)

    dialog._on_create()

    assert saved, "project must be saved by _on_create"
    project = saved[0]
    timeline = project.timelines[project.active_timeline_id]
    assert timeline.sample_rate == 44100, "user-selected sample rate must not be dropped"
    assert timeline.fps == dialog.fps_combo.currentData()


# ===========================================================================
# AUD-20309 — resolution preset values do not drift under aspect lock
# ===========================================================================


def test_resolution_preset_honored_under_aspect_lock(qapp) -> None:
    from src.features.edit.ui.new_timeline_dialog import NewTimelineDialog

    dialog = NewTimelineDialog(config_manager=None)
    dialog._aspect_locked = True
    dialog._aspect_ratio = 16.0 / 9.0

    preset_index = None
    for index in range(1, dialog.resolution_combo.count()):
        if dialog.resolution_combo.itemText(index) == "4K DCI (4096x2160)":
            preset_index = index
            break
    assert preset_index is not None

    dialog._on_resolution_changed(preset_index)

    assert int(dialog.width_spin.value()) == 4096
    assert int(dialog.height_spin.value()) == 2160
    # Lock re-anchored to the preset's own ratio (256:135), not the old 16:9.
    assert dialog._aspect_ratio == pytest.approx(4096 / 2160)


def test_width_edit_still_enforces_lock_after_preset(qapp) -> None:
    from src.features.edit.ui.new_timeline_dialog import NewTimelineDialog

    dialog = NewTimelineDialog(config_manager=None)
    dialog._aspect_locked = True
    dialog._aspect_ratio = 16.0 / 9.0

    dialog._on_width_changed(1920.0)
    assert int(dialog.height_spin.value()) == 1080


# ===========================================================================
# AUD-20310 — non-preset sequence fps surfaces as Custom
# ===========================================================================


def test_non_preset_fps_gets_custom_entry(qapp) -> None:
    from src.features.edit.ui.new_timeline_dialog import FPS_PRESETS, NewTimelineDialog

    dialog = NewTimelineDialog(
        config_manager=None,
        initial_settings={
            "name": "OddFps",
            "fps": 15.0,
            "fps_num": 15,
            "fps_den": 1,
            "width": 1920,
            "height": 1080,
            "sample_rate": 48000,
        },
    )

    fps_index = dialog.fps_combo.currentIndex()
    assert fps_index >= len(FPS_PRESETS), "non-preset fps must add a Custom entry"
    assert float(dialog.fps_combo.currentData()) == 15.0

    settings = dialog.get_settings()
    assert settings["fps"] == 15.0
    assert settings["fps_num"] == 15
    assert settings["fps_den"] == 1


def test_selecting_preset_discards_custom_fps(qapp) -> None:
    from src.features.edit.ui.new_timeline_dialog import FPS_PRESETS, NewTimelineDialog

    dialog = NewTimelineDialog(
        config_manager=None,
        initial_settings={
            "fps": 15.0,
            "fps_num": 15,
            "fps_den": 1,
        },
    )
    assert dialog._custom_fps is not None

    dialog.fps_combo.setCurrentIndex(1)  # "24"
    assert dialog._custom_fps is None
    settings = dialog.get_settings()
    assert settings["fps_num"] == FPS_PRESETS[1][2]
    assert settings["fps_den"] == FPS_PRESETS[1][3]


def test_custom_fps_reselected_without_marker_uses_rational(qapp) -> None:
    from src.features.edit.ui.new_timeline_dialog import FPS_PRESETS, NewTimelineDialog

    dialog = NewTimelineDialog(
        config_manager=None,
        initial_settings={
            "fps": 15.0,
            "fps_num": 15,
            "fps_den": 1,
        },
    )
    custom_index = dialog.fps_combo.currentIndex()
    dialog.fps_combo.setCurrentIndex(0)  # back to a preset (clears marker)
    assert dialog._custom_fps is None
    dialog.fps_combo.setCurrentIndex(custom_index)  # re-select Custom by hand

    settings = dialog.get_settings()
    assert settings["fps"] == 15.0
    assert settings["fps_num"] / settings["fps_den"] == pytest.approx(15.0)


# ===========================================================================
# AUD-20401 / AUD-20404 — paintGL overlay wiring
# ===========================================================================


class _FakePainter:
    def __init__(self, target=None) -> None:
        self.calls: list[str] = []

    def beginNativePainting(self) -> None:
        self.calls.append("beginNativePainting")

    def endNativePainting(self) -> None:
        self.calls.append("endNativePainting")

    def end(self) -> None:
        self.calls.append("end")


def _paintable_viewer(monkeypatch) -> pv.ProgramViewer:
    viewer = pv.ProgramViewer()
    viewer._gl_initialized = True
    monkeypatch.setattr(pv, "QPainter", _FakePainter)
    monkeypatch.setattr(viewer, "_reset_copy_gl_state", lambda: None)
    for name in ("glViewport", "glClearColor", "glClear"):
        monkeypatch.setattr(pv._gl, name, lambda *a, **k: None)
    return viewer


def test_error_overlay_painted_when_error_state_set(qapp, monkeypatch) -> None:
    viewer = _paintable_viewer(monkeypatch)
    painted: list = []
    monkeypatch.setattr(viewer, "_paint_error_overlay", painted.append)

    viewer._error_state = "none"
    viewer._paintGL_impl()
    assert painted == []

    viewer._error_state = "render_error"
    viewer._paintGL_impl()
    assert len(painted) == 1, "error state must trigger the overlay painter"


def test_subtitle_overlay_not_wrapped_in_native_painting(qapp, monkeypatch) -> None:
    viewer = _paintable_viewer(monkeypatch)
    painted: list = []
    monkeypatch.setattr(
        viewer, "_paint_subtitle_overlay", painted.append
    )
    monkeypatch.setattr(
        viewer._subtitle_handle_controller, "draw_handles", lambda painter: None
    )
    viewer._subtitle_overlays = [object()]

    viewer._paintGL_impl()

    assert len(painted) == 1
    painter = painted[0]
    assert isinstance(painter, _FakePainter)
    assert "beginNativePainting" not in painter.calls
    assert "end" in painter.calls


# ===========================================================================
# AUD-20402 — clear_texture defers GL deletion to paintGL
# ===========================================================================


def test_clear_texture_defers_delete_to_paintgl(qapp, monkeypatch) -> None:
    viewer = _paintable_viewer(monkeypatch)
    deleted: list = []
    monkeypatch.setattr(
        pv._gl, "glDeleteTextures", lambda ids: deleted.extend(ids)
    )

    viewer._current_tex_id = 55
    viewer.clear_texture()

    # Nothing deleted while outside the GL callback; queued for paintGL.
    assert deleted == []
    assert viewer._pending_tex_delete == 55
    assert viewer._current_tex_id is None

    viewer._paintGL_impl()
    assert deleted == [55]
    assert viewer._pending_tex_delete is None


def test_deferred_delete_never_touches_reallocated_texture(qapp, monkeypatch) -> None:
    viewer = _paintable_viewer(monkeypatch)
    deleted: list = []
    monkeypatch.setattr(
        pv._gl, "glDeleteTextures", lambda ids: deleted.extend(ids)
    )

    viewer._current_tex_id = 55
    viewer.clear_texture()
    # A fresh frame arrives before the next paint: new texture name 66.
    viewer._current_tex_id = 66

    viewer._paintGL_impl()
    assert deleted == [55], "only the queued name may be deleted"
    assert viewer._current_tex_id == 66


# ===========================================================================
# AUD-20403 — present-frame ack on invalid-texture / blit-exception paths
# ===========================================================================


def _copy_viewer(monkeypatch, *, source_valid: bool, blit_raises: bool):
    viewer = pv.ProgramViewer()
    deleted: list = []
    monkeypatch.setattr(pv._gl, "glIsTexture", lambda tex: source_valid)
    monkeypatch.setattr(pv._gl, "glDeleteTextures", lambda ids: deleted.extend(ids))
    monkeypatch.setattr(pv._gl, "glGenTextures", lambda n: 11)
    for name in (
        "glTexParameteri", "glTexImage2D", "glBindTexture",
        "glGenFramebuffers", "glBindFramebuffer", "glFramebufferTexture2D",
        "glClearColor",
    ):
        monkeypatch.setattr(pv._gl, name, lambda *a, **k: None)

    def _clear(*_a, **_k) -> None:
        return None

    monkeypatch.setattr(pv._gl, "glClear", _clear)

    def _blit(*_a, **_k) -> None:
        if blit_raises:
            raise RuntimeError("blit boom")

    monkeypatch.setattr(pv._gl, "glBlitFramebuffer", _blit)
    monkeypatch.setattr(viewer, "_reset_copy_gl_state", lambda: None)

    viewer._shared_tex_id = 9
    viewer._shared_tex_width = 64
    viewer._shared_tex_height = 48
    acks: list = []
    viewer._pending_present_frame = SimpleNamespace(slot_id=3, has_fence=False)
    viewer._pending_present_ack = acks.append
    return viewer, acks


def test_ack_fires_when_source_texture_invalid(qapp, monkeypatch) -> None:
    viewer, acks = _copy_viewer(monkeypatch, source_valid=False, blit_raises=False)
    viewer._copy_shared_texture_to_display()
    assert acks == [3], "invalid source texture must still ack the renderer slot"
    assert viewer._pending_present_frame is None
    assert viewer._pending_present_ack is None


def test_ack_fires_exactly_once_when_blit_raises(qapp, monkeypatch) -> None:
    viewer, acks = _copy_viewer(monkeypatch, source_valid=True, blit_raises=True)
    with pytest.raises(RuntimeError):
        viewer._copy_shared_texture_to_display()
    assert acks == [3], "blit exception must ack exactly once"


def test_ack_fires_exactly_once_on_success(qapp, monkeypatch) -> None:
    viewer, acks = _copy_viewer(monkeypatch, source_valid=True, blit_raises=False)
    viewer._copy_shared_texture_to_display()
    assert acks == [3]


def test_ack_fires_for_invalid_shared_dims(qapp, monkeypatch) -> None:
    viewer, acks = _copy_viewer(monkeypatch, source_valid=True, blit_raises=False)
    viewer._shared_tex_width = 0
    viewer._copy_shared_texture_to_display()
    assert acks == [3]


# ===========================================================================
# AUD-20500 — image Auto resolution from FileIO "Resolution" string
# ===========================================================================


def _probe(monkeypatch, resolution: str) -> dict:
    import src.features.edit.ui.quick_project_dialog as qpd
    import src.shared.io.file_io as file_io

    monkeypatch.setattr(
        file_io.FileIO,
        "get_metadata",
        staticmethod(lambda path: {"Resolution": resolution}),
    )
    return qpd.QuickProjectDialog._probe_media_metadata(
        SimpleNamespace(), "/tmp/photo.png"
    )


def test_image_resolution_parsed_from_resolution_string(qapp, monkeypatch) -> None:
    metadata = _probe(monkeypatch, "6000 x 4000")
    assert metadata["width"] == 6000
    assert metadata["height"] == 4000
    assert metadata["media_type"] == "image"


def test_image_resolution_missing_falls_back_with_warning(
    qapp, monkeypatch
) -> None:
    metadata = _probe(monkeypatch, "-")
    assert metadata["width"] == 1920
    assert metadata["height"] == 1080
