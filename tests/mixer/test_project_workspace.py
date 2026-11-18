"""The Studio workspace edits the same tracks and timeline as Edit."""
from __future__ import annotations

import wave
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import QApplication, QDockWidget, QMainWindow, QMenu, QMessageBox, QTabBar, QWidget

from src.app.controllers.dock_layout_controller import DockLayoutMixin
from src.app.controllers.edit_controller import EditControllerMixin
from src.app.controllers.layer_controller import LayerOpsMixin
from src.app.controllers.menu_toolbar_controller import MenuToolbarMixin
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.selection import TimelineSelection
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.edit_effects_panel import EditEffectsPanel
from src.features.edit.ui.edit_viewport import EditViewport
from src.features.edit.ui.media_pool_panel import MediaPoolPanel, ViewMode
from src.features.edit.ui.program_viewer import ProgramViewerWidget
from src.features.edit.ui.source_viewer import SourceViewerWidget
from src.features.edit.ui.timeline_layout import TimelineDropTarget
from src.features.edit.ui.timeline_panel import TimelinePanel
from src.features.edit.ui.timeline_view import MIME_TYPE_MEDIA_POOL_ASSETS
from src.features.mixer.effects.panels.clip_editor import StudioAudioClipEditor
from src.features.mixer.ui.project_clip_editor import ProjectClipEditor
from src.features.mixer.ui.studio_console import StudioAudioMixerPanel
from src.shared.ui.audio_mixer.track_mixer_widget import db_to_linear


class ProjectHost(QMainWindow, DockLayoutMixin, EditControllerMixin, LayerOpsMixin, MenuToolbarMixin):
    def __init__(self) -> None:
        super().__init__()
        self.resize(1120, 720)
        self._edit_available = self._mixer_available = True
        self.current_page = "Edit"
        self.mixer_page = SimpleNamespace()
        self.audio_mixer_panel = StudioAudioMixerPanel()
        self.timeline_panel = TimelinePanel()
        self.program_viewer_widget = ProgramViewerWidget()
        self.edit_viewport = EditViewport(self.program_viewer_widget, SourceViewerWidget())
        self.edit_properties_panel = QWidget()
        self.media_pool_panel = MediaPoolPanel(self)
        self.media_pool_panel.hide()
        self.edit_effects_panel = EditEffectsPanel()
        self.status_bar = self.statusBar()
        self.edit_page = SimpleNamespace(_current_timeline=None, _audio_manager=None)
        self.dock_audio_mixer = self.make_dock("Audio Mixer", self.audio_mixer_panel, "editMixer")
        self.dock_program_viewer = self.make_dock("Viewport", self.edit_viewport, "programViewer")
        self.dock_edit_timeline = self.make_dock("Timeline", self.timeline_panel, "editTimeline")
        self.dock_properties = self.make_dock("Properties", self.edit_properties_panel, "editProperties")
        self.dock_edit_media_pool = self.make_dock("Media Pool", self.media_pool_panel, "editMediaPool")
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_edit_media_pool)
        self.dock_edit_effects = self.make_dock("Effects", self.edit_effects_panel, "editEffects")
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_edit_effects)
        self._create_audio_workspace_docks(self.make_dock)
        # 生产代码创建 Mixer dock 后默认隐藏（进入 Mixer 才显示）；不隐藏时
        # 顶部 dock 会挤压时间线视口，audio_only 场景重建被推迟。
        for dock_attr in ("dock_audio_viewer", "dock_audio_clip_editor",
                          "dock_audio_media_pool", "dock_audio_effects",
                          "dock_audio_console", "dock_audio_timeline"):
            getattr(self, dock_attr).hide()
        self.timeline_panel.set_audio_mixer_dock(self.dock_audio_mixer)

        panel = self.audio_mixer_panel
        panel.track_volume_changed.connect(self._on_mixer_volume_committed)
        panel.track_effects_committed.connect(self._on_mixer_effects_committed)
        panel.master_volume_changed.connect(self._on_mixer_master_volume)
        panel.track_output_requested.connect(self._on_mixer_output_bus_requested)
        panel.add_bus_requested.connect(self._on_mixer_add_bus_requested)
        self.timeline_panel.timeline_view.timeline_changed.connect(self.sync_timeline)

        split = QAction("Split", self)
        split.setShortcut(QKeySequence("Ctrl+Shift+X"))
        split.triggered.connect(self.on_timeline_split)
        self.menuBar().addMenu("Edit").addAction(split)
        self._module_actions = {"edit.timeline.split": split}
        self._scope_timeline_delete_shortcuts()

    def make_dock(self, title: str, widget: QWidget, name: str) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(name)
        dock.setWidget(widget)
        self.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, dock)
        return dock

    def sync_timeline(self, timeline: TimelineSequence) -> None:
        self.edit_page._current_timeline = timeline
        self._sync_audio_mixer_from_current_timeline()

    def show_mixer(self) -> None:
        self.current_page = "Mixer"
        self._attach_edit_panels_to_mixer()
        self.dock_audio_console.show()
        self.dock_audio_timeline.show()

    def switch_to_page(self, page_name: str) -> None:
        assert page_name == "Mixer"
        self.show_mixer()


@pytest.fixture
def project_host(qtbot) -> ProjectHost:
    host = ProjectHost()
    qtbot.addWidget(host)
    clip = ClipInstance(asset_id="audio", track_id="audio-track", timeline_duration=120, source_duration=120)
    track = EditTrack(
        track_id="audio-track", name="Dialogue", track_type="audio",
        volume=0.5, color="#4BA6DD", clips=(clip,),
    )
    picture = ClipInstance(asset_id="picture", track_id="video-track", timeline_duration=120, source_duration=120)
    video = EditTrack(track_id="video-track", name="Picture", track_type="video", clips=(picture,))
    sequence = TimelineSequence(audio_tracks=(track,), video_tracks=(video,))
    host.timeline_panel.timeline_view.load_timeline(sequence)
    host.sync_timeline(sequence)
    host.show()
    return host


def test_volume_and_insert_rack_share_commands_across_workspaces(project_host, qtbot) -> None:
    host = project_host
    panel = host.audio_mixer_panel
    strip = panel._track_mixers["audio-track"]
    view = host.timeline_panel.timeline_view
    strip._effect_add("delay")
    panel._master_mixer._effect_add("reverb")
    added = view.get_timeline()
    delay = added.audio_tracks[0].effect_chain.effects[0]
    master_chain = added.master_effect_chain

    host.edit_viewport.set_dual_view(True)
    host.show_mixer()
    assert panel.parentWidget() is host.dock_audio_console.widget()
    assert host.dock_audio_console.windowTitle() == host.dock_audio_mixer.windowTitle()
    assert host.timeline_panel.parentWidget() is host.dock_audio_timeline.widget()
    assert host.edit_viewport.parentWidget() is host.dock_audio_viewer.widget()
    assert host.edit_viewport.source_pane.isHidden()
    assert not host.edit_viewport.program_pane.isHidden()
    assert host.edit_properties_panel.parentWidget() is host.dock_properties
    assert strip._effect_list.count() == panel._master_mixer._effect_list.count() == 1
    assert strip._eq_badge.isHidden()
    assert strip.width() == panel._master_mixer.width() == 96
    assert strip.studio_fader.width() == 44
    assert strip.studio_meter.width() == 22
    assert strip.get_volume_linear() == pytest.approx(0.5)

    # The existing rack edits the same stable effect instance in Mixer.
    strip._effect_bypass(delay)
    bypassed = view.get_timeline().audio_tracks[0].effect_chain.effects[0]
    assert bypassed.instance_id == delay.instance_id
    assert bypassed.params["enabled"] is False
    assert view.get_timeline().master_effect_chain == master_chain

    revision = view.get_timeline().revision
    fader = strip.studio_fader
    point = QPoint(fader.width() // 2, fader.height() // 2)
    qtbot.mousePress(fader, Qt.MouseButton.LeftButton, pos=point)
    fader.setDb(-9.0)
    fader.setDb(-12.0)
    host._sync_audio_mixer_from_current_timeline()
    assert fader.db == -12.0
    assert view.get_timeline().revision == revision
    qtbot.mouseRelease(fader, Qt.MouseButton.LeftButton, pos=point)
    assert view.get_timeline().revision == revision + 1
    assert view.get_timeline().audio_tracks[0].volume == pytest.approx(db_to_linear(-12.0))
    assert host._edit_undo_redo_target() is view

    host._restore_edit_panels_from_mixer()
    host.current_page = "Edit"
    assert host.dock_edit_timeline.widget() is host.timeline_panel
    assert host.dock_audio_mixer.widget() is panel
    assert host.dock_program_viewer.widget() is host.edit_viewport
    assert not host.edit_viewport.source_pane.isHidden()
    assert host.dock_properties.widget() is host.edit_properties_panel
    assert strip._volume_spin.value() == -12.0
    assert strip.studio_fader.isVisibleTo(strip)
    assert strip._eq_badge.isHidden()
    view.undo()
    assert view.get_timeline().audio_tracks[0].volume == pytest.approx(0.5)


def test_split_shortcut_uses_shared_timeline_in_floating_mixer(project_host, qtbot) -> None:
    host = project_host
    view = host.timeline_panel.timeline_view
    host.show_mixer()
    dock = host.dock_audio_timeline
    dock.setFloating(True)
    dock.show()
    QApplication.setActiveWindow(dock)
    view.setFocus()
    picture = view.get_timeline().video_tracks[0].clips[0]
    assert not view._track_headers["video-track"].isVisible()
    assert not view._items_map[picture.clip_id].isVisible()
    assert view._track_headers["audio-track"].isVisible()
    assert view._layout_engine.area_layout().video_area.isEmpty()
    view.set_playhead(48)
    qtbot.keyClick(
        view, Qt.Key.Key_X,
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
    )
    qtbot.waitUntil(lambda: len(view.get_timeline().audio_tracks[0].clips) == 2)
    assert view.get_timeline().video_tracks[0].clips == (picture,)
    host._restore_edit_panels_from_mixer()
    assert host.dock_edit_timeline.widget().timeline_view is view
    view.undo()
    assert len(view.get_timeline().audio_tracks[0].clips) == 1
    assert view._track_headers["video-track"].isVisible()


def test_new_tracks_and_master_keep_the_compact_layout_and_real_levels(project_host) -> None:
    host = project_host
    host.show_mixer()
    panel = host.audio_mixer_panel
    master = panel._master_mixer
    host.sync_timeline(TimelineSequence(master_volume=0.4))
    assert not panel._track_mixers
    assert master.get_volume_linear() == pytest.approx(0.4)

    host.sync_timeline(TimelineSequence(audio_tracks=(EditTrack(track_id="new", track_type="audio"),)))
    strip = panel._track_mixers["new"]
    assert strip.width() == master.width() == 96
    levels = {"peak": [-8.0, -12.0], "gr": {"compressor": 3.0}}
    panel.update_per_track_levels({"new": levels})
    panel.update_per_track_levels({"new": levels})
    assert (strip.studio_meter.level_l, strip.studio_meter.level_r) == (-8.0, -12.0)
    assert strip.studio_meter.gr_db == -3.0
    panel.on_telemetry_reset(1)
    assert strip.studio_meter.level_l == -60.0
    assert strip.studio_peak.text() == "— dBFS"


def test_output_selector_uses_real_bus_ids_and_rejects_cycles(project_host) -> None:
    host = project_host
    panel = host.audio_mixer_panel
    view = host.timeline_panel.timeline_view
    panel.add_bus_requested.emit()
    panel.add_bus_requested.emit()
    first, second = [track for track in view.get_timeline().audio_tracks if track.is_bus]
    host.show_mixer()

    def choose(track_id: str, output_id: str | None) -> None:
        combo = panel._track_mixers[track_id].output_bus_combo
        index = combo.findData(output_id)
        assert index >= 0
        combo.setCurrentIndex(index)
        combo.activated.emit(index)

    choose("audio-track", first.track_id)
    assert view.get_timeline().get_track_by_id("audio-track").output_bus_id == first.track_id
    choose(second.track_id, first.track_id)
    revision = view.get_timeline().revision
    choose(first.track_id, second.track_id)
    assert view.get_timeline().revision == revision
    assert panel._track_mixers[first.track_id].output_bus_combo.currentData() is None

    host._restore_edit_panels_from_mixer()
    assert panel._track_mixers["audio-track"].output_bus_combo.currentData() == first.track_id
    view.undo()
    view.undo()
    assert panel._track_mixers["audio-track"].output_bus_combo.currentData() is None


def test_audio_clip_context_menu_opens_professional_dock_and_updates_timeline(
    project_host, qtbot, tmp_path, monkeypatch,
) -> None:
    host = project_host
    view = host.timeline_panel.timeline_view
    audio_path = tmp_path / "dialogue.wav"
    with wave.open(str(audio_path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(b"\xff\x1f\xff\x0f\x00\xe0\x00\xf0" * 120000)
    asset = MediaAsset(
        asset_id="audio", path=str(audio_path), media_type="audio", display_name="Dialogue",
        metadata={"audio_info": {"sample_rate": 48000, "channels": 2}},
    )
    host._current_edit_project = EditProject("project", "Dialogue", assets={asset.asset_id: asset})
    host.show_mixer()
    clip = view.get_timeline().audio_tracks[0].clips[0]
    item = view._items_map[clip.clip_id]

    def choose_editor(menu: QMenu, *_args: object) -> QAction:
        return next(action for action in menu.actions() if action.objectName() == "openProfessionalAudioEditor")

    monkeypatch.setattr(QMenu, "exec", choose_editor)
    click = view.mapFromScene(item.mapToScene(QPointF(50.0, 35.0)))
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.RightButton, pos=click)
    dock = host.dock_audio_clip_editor
    editor = dock.widget()
    assert dock.isVisible() and dock in host.tabifiedDockWidgets(host.dock_audio_timeline)
    assert editor.clip_id == clip.clip_id
    assert editor._panel is not None
    qtbot.waitUntil(lambda: editor._panel.peak_val_lbl.text() != "—", timeout=10000)
    # P1-6：噪声采集/降噪随真实 PCM 启用（预览态）；频谱修复笔仍待后端。
    assert editor._panel.btn_denoise.isEnabled()
    qtbot.waitUntil(lambda: editor._panel.btn_tool_brush.isEnabled(), timeout=10000)
    editor.float_requested.emit()
    assert dock.isFloating() and dock.titleBarWidget().height() > 0
    dock.move(-20000, -20000)
    view.request_audio_clip_editor(clip.clip_id)
    assert QApplication.primaryScreen().availableGeometry().intersects(dock.geometry())
    original_peak = editor._panel.peak_val_lbl.text()
    new_audio_path = tmp_path / "dialogue-relinked.wav"
    with wave.open(str(new_audio_path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(b"\xff\x3f\xff\x0f\x00\xc0\x00\xf0" * 120000)
    relinked_asset = replace(asset, path=str(new_audio_path))
    host._current_edit_project = replace(host._current_edit_project, assets={asset.asset_id: relinked_asset})
    host.media_pool_panel.project_changed.emit(host._current_edit_project)
    qtbot.waitUntil(
        lambda: editor._panel.peak_val_lbl.text() not in {"—", original_peak},
        timeout=10000,
    )
    editor._panel.gain_requested.emit(-6.0)
    editor._panel.fadeApplied.emit(0.5, 0.25)
    edited = view.get_timeline().get_clip_by_id(clip.clip_id)
    assert edited.audio_gain_db == -6.0
    assert (edited.fade_in_duration, edited.fade_out_duration) == (12, 6)
    editor.undo_requested.emit()
    assert view.get_timeline().get_clip_by_id(clip.clip_id).fade_in_duration == 0
    editor._panel._fade_curve_btns["S-Curve"].click()
    edited = view.get_timeline().get_clip_by_id(clip.clip_id)
    assert edited.effective_fade_curve_in == edited.effective_fade_curve_out == "smooth"
    editor.undo_requested.emit()
    assert view.get_timeline().get_clip_by_id(clip.clip_id).effective_fade_curve_in is None
    editor.redo_requested.emit()
    assert view.get_timeline().get_clip_by_id(clip.clip_id).effective_fade_curve_out == "smooth"
    editor.tab_requested.emit()
    assert not dock.isFloating()
    assert dock in host.tabifiedDockWidgets(host.dock_audio_timeline)
    host._restore_edit_panels_from_mixer()
    assert editor.clip_id is None


def test_double_click_and_tab_activation_follow_timeline_selection(project_host, qtbot, tmp_path) -> None:
    host = project_host
    view = host.timeline_panel.timeline_view
    path = tmp_path / "two-clips.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x00\x10\x00\xf0" * 120000)
    asset = MediaAsset(asset_id="audio", path=str(path), media_type="audio", display_name="Two clips", metadata={})
    host._current_edit_project = EditProject("project", "Clips", assets={asset.asset_id: asset})
    timeline = view.get_timeline()
    track = timeline.audio_tracks[0]
    first = replace(track.clips[0], timeline_duration=48, source_duration=48, fade_in_samples=960)
    second = replace(first, clip_id="second", timeline_start=48, source_start=48)
    timeline = timeline.with_track_updated(replace(track, clips=(first, second)))
    view.load_timeline(timeline)
    host.sync_timeline(timeline)
    host.show_mixer()

    item = view._items_map[first.clip_id]
    click = view.mapFromScene(item.mapToScene(QPointF(50.0, 26.0)))
    qtbot.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=click)
    dock = host.dock_audio_clip_editor
    editor = dock.widget()
    assert editor.clip_id == first.clip_id
    assert dock.titleBarWidget().height() == 0
    assert editor.findChild(QWidget, "ClipEditorHeader").height() == 30
    editor._panel.fi_pill.valueEdited.emit("0.030 s")
    edited = view.get_timeline().get_clip_by_id(first.clip_id)
    assert (edited.fade_in_samples, edited.fade_out_samples) == (1440, None)
    editor._panel.fi_pill.valueEdited.emit("nan")
    assert view.get_timeline().get_clip_by_id(first.clip_id).fade_in_samples == 1440
    editor.undo_requested.emit()
    assert view.get_timeline().get_clip_by_id(first.clip_id).fade_in_samples == 960

    editor._return_to_timeline()
    tabs = next(bar for bar in host.findChildren(QTabBar) if dock.windowTitle() in
                [bar.tabText(index) for index in range(bar.count())])
    clip_tab = next(index for index in range(tabs.count()) if tabs.tabText(index) == dock.windowTitle())
    assert tabs.currentIndex() != clip_tab
    view.set_selection(TimelineSelection().with_clip_selected(second.clip_id, track.track_id, timeline.timeline_id))
    assert editor.clip_id == first.clip_id
    qtbot.mouseClick(tabs, Qt.MouseButton.LeftButton, pos=tabs.tabRect(clip_tab).center())
    assert editor.clip_id == second.clip_id

    editor._return_to_timeline()
    view.set_selection(TimelineSelection())
    qtbot.mouseClick(tabs, Qt.MouseButton.LeftButton, pos=tabs.tabRect(clip_tab).center())
    assert editor.clip_id is None and editor._empty.isVisible()


@pytest.mark.parametrize("mode", [ViewMode.LIST, ViewMode.GRID])
def test_media_pool_audio_file_opens_original_editor_without_a_timeline(
    project_host, qtbot, tmp_path, monkeypatch, mode: ViewMode,
) -> None:
    host = project_host
    host.timeline_panel.clear()
    host.edit_page._current_timeline = None
    audio_path = tmp_path / "standalone.wav"
    with wave.open(str(audio_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(b"\xff\x1f\x00\xe0" * 24000)
    asset = MediaAsset(
        asset_id="standalone-audio", path=str(audio_path), media_type="audio",
        display_name="Standalone", metadata={"audio_info": {"sample_rate": 48000, "channels": 1}},
    )
    project = EditProject("project", "Source material", assets={asset.asset_id: asset})
    host._current_edit_project = project
    pool = host.media_pool_panel
    pool.resize(640, 400)
    pool.set_project(project)
    pool.set_view_mode(mode)
    pool.show()
    qtbot.waitUntil(lambda: pool.tree.topLevelItemCount() > 0)

    def open_audio_asset(menu: QMenu, *_args: object) -> QAction:
        action = next(action for action in menu.actions() if action.objectName() == "openProfessionalAudioEditor")
        action.trigger()
        return action

    monkeypatch.setattr(QMenu, "exec", open_audio_asset)
    if mode == ViewMode.LIST:
        item = pool.tree.topLevelItem(0)
        assert item.data(0, Qt.ItemDataRole.UserRole) == asset.asset_id
        pool._on_tree_context_menu(pool.tree.visualItemRect(item).center())
    else:
        index = pool.asset_model.index(pool.asset_model.row_for_asset(asset.asset_id), 0)
        pool._on_grid_context_menu(pool.grid_view.visualRect(index).center())

    assert host.current_page == "Mixer"
    dock = host.dock_audio_clip_editor
    editor = dock.widget()
    assert dock.isVisible()
    assert editor.clip_id is None
    assert isinstance(editor._panel, StudioAudioClipEditor)
    assert not editor._panel.gain_knob.isEnabled()
    qtbot.waitUntil(lambda: editor._panel.peak_val_lbl.text() != "—", timeout=10000)
    assert host.timeline_panel.timeline_view.get_timeline() is None
    assert host._current_edit_project is project
    host._clear_project_audio_clip_editor()
    assert dock.isHidden() and editor._asset is None


def test_mixer_track_header_record_button_follows_capture_state(project_host, qtbot, monkeypatch) -> None:
    from src.features.edit.core import recording_manager

    class _Recorder(recording_manager.RecordingManager):
        def start_recording(self, **_kwargs: object) -> bool:
            self._set_state(self.STATE_PRE_ROLL)
            self._set_state(self.STATE_RECORDING)
            return True

        def stop_recording(self) -> None:
            self._set_state(self.STATE_STOPPING)

    monkeypatch.setattr(recording_manager, "RecordingManager", _Recorder)
    host = project_host
    host._current_edit_project = EditProject("project", "Recording")
    host.edit_page._current_frame = 48
    view = host.timeline_panel.timeline_view
    panel = host.audio_mixer_panel
    panel.add_bus_requested.emit()
    host.show_mixer()
    header = view._track_headers["audio-track"]
    bus = next(track for track in view.get_timeline().audio_tracks if track.is_bus)
    assert not view._track_headers[bus.track_id]._show_record_button
    point = view.mapFromScene(header.mapToScene(header._get_record_button_rect().center()))
    assert header._get_button_at_pos(header._get_record_button_rect().center()) == "record"
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert header._recording_state == "recording"
    assert host._voiceover_sessions["audio-track"]["anchor_frame"] == 48

    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert header._recording_state == "stopping"
    recorder = host._mixer_timeline_recorders["audio-track"]
    assert not header._record_button_enabled()
    recorder._set_state(recorder.STATE_IDLE)
    assert header._recording_state == "idle"
    assert not host._mixer_timeline_recorders

    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    recorder = host._mixer_timeline_recorders["audio-track"]
    host._sync_mixer_timeline_recorders(TimelineSequence())
    assert recorder.state == recorder.STATE_STOPPING
    recorder._set_state(recorder.STATE_IDLE)
    assert not host._mixer_timeline_recorders


def test_audio_workspace_switches_ruler_to_sample_display(project_host, qtbot) -> None:
    """P2-8（P0）：音频工作区时间尺用采样域，返回 Edit 恢复帧域。"""
    host = project_host
    view = host.timeline_panel.timeline_view
    assert view._ruler_item._display_mode == "frames"
    host.show_mixer()
    assert view._audio_only
    assert view._ruler_item._display_mode == "samples"
    host._restore_edit_panels_from_mixer()
    host.current_page = "Edit"
    assert not view._audio_only
    assert view._ruler_item._display_mode == "frames"


def test_media_pool_and_effects_borrow_across_workspaces(project_host, qtbot, tmp_path) -> None:
    """P0-1：Media Pool / Effects 面板跨页借用 + 音频作用域可还原。"""
    host = project_host
    audio_path = tmp_path / "vo.wav"
    with wave.open(str(audio_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(b"\x10\x00" * 4800)
    asset = MediaAsset(
        asset_id="vo", path=str(audio_path), media_type="audio",
        display_name="VO", metadata={"audio_info": {"sample_rate": 48000, "channels": 1}},
    )
    project = EditProject("project", "Workspace", assets={asset.asset_id: asset})
    host._current_edit_project = project
    pool = host.media_pool_panel
    pool.set_project(project)
    effects = host.edit_effects_panel
    effects.tabs.setCurrentIndex(0)  # Video tab
    assert host.dock_audio_media_pool is not None and host.dock_audio_effects is not None

    host.show_mixer()
    assert pool.parentWidget() is host.dock_audio_media_pool.widget()
    assert effects.parentWidget() is host.dock_audio_effects.widget()
    # 音频作用域：默认 All Audio 智能箱 + Audio 标签页。
    assert pool._current_bin_id == "all_audio" and pool._current_bin_is_smart
    assert effects.tabs.currentIndex() == 3

    # 两次借还是同一实例，不重复连接/嵌套。
    host.show_mixer()
    assert pool.parentWidget() is host.dock_audio_media_pool.widget()
    assert effects.parentWidget() is host.dock_audio_effects.widget()
    assert effects.tabs.currentIndex() == 3

    host._restore_edit_panels_from_mixer()
    host.current_page = "Edit"
    assert host.dock_edit_media_pool.widget() is pool
    assert host.dock_edit_effects.widget() is effects
    # 作用域还原：回根 bin（非智能箱）与原标签页。
    assert pool._current_bin_id == "" and not pool._current_bin_is_smart
    assert effects.tabs.currentIndex() == 0
    # 再次借还后作用域不残留（保存状态被消费）。
    host.show_mixer()
    host._restore_edit_panels_from_mixer()
    assert pool._audio_workspace_saved is None
    assert effects._audio_workspace_tab_saved is None


def test_audio_workspace_media_drop_semantics(project_host, qtbot, monkeypatch) -> None:
    """P0-1：音频时间线拖放——纯音频/含音频视频落音轨，无音频拒绝，Bus 拒绝。"""
    import json

    from PyQt6.QtCore import QMimeData
    from PyQt6.QtGui import QDropEvent

    host = project_host
    host.show_mixer()
    view = host.timeline_panel.timeline_view
    assert view._audio_only

    def make_asset(asset_id: str, media_type: str, audio: bool) -> MediaAsset:
        metadata: dict = {"fps": 24.0, "duration_seconds": 2.0}
        if audio:
            metadata["audio_info"] = {"sample_rate": 48000, "channels": 2}
        return MediaAsset(
            asset_id=asset_id, path=f"/tmp/{asset_id}.bin",
            media_type=media_type, display_name=asset_id, metadata=metadata,
        )

    sound_video = make_asset("sound-video", "video", True)
    silent_video = make_asset("silent-video", "video", False)
    plain_audio = make_asset("plain-audio", "audio", True)

    assert view._audio_workspace_drag_media_type() is None  # 无拖放状态
    for assets, expected in (
        ([sound_video], "audio"),
        ([plain_audio], "audio"),
        ([plain_audio, sound_video], "audio"),
        ([silent_video], None),
        ([plain_audio, silent_video], None),
    ):
        view._drag_assets = list(assets)
        assert view._audio_workspace_drag_media_type() == expected
    view._drag_assets = []

    # 含音频流的视频按仅音频落显式音轨：不建视频轨、不建链接。
    timeline = view.get_timeline()
    audio_track = timeline.get_track_by_id("audio-track")
    video_clips_before = len(timeline.video_tracks[0].clips)
    assert view._add_asset_as_clip(sound_video, audio_track, 200, stream_intent="audio")
    updated = view.get_timeline()
    new_clips = [c for c in updated.get_track_by_id("audio-track").clips
                 if c.asset_id == "sound-video"]
    assert len(new_clips) == 1 and new_clips[0].timeline_start == 200
    assert len(updated.video_tracks) == 1
    assert len(updated.video_tracks[0].clips) == video_clips_before
    view.undo()
    assert all(c.asset_id != "sound-video"
               for c in view.get_timeline().get_track_by_id("audio-track").clips)

    # 无音频流的素材在提交前被拒并给出反馈（零写入）。
    messages: list[str] = []
    view.status_message_requested.connect(messages.append)
    view._drag_assets = [silent_video]
    view._drag_asset_ids = ["silent-video"]
    mime = QMimeData()
    mime.setData(MIME_TYPE_MEDIA_POOL_ASSETS, json.dumps(["silent-video"]).encode())
    event = QDropEvent(QPointF(300.0, 60.0), Qt.DropAction.CopyAction, mime,
                       Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    clips_before = len(view.get_timeline().get_track_by_id("audio-track").clips)
    view.dropEvent(event)
    assert any("no audio stream" in message for message in messages)
    assert len(view.get_timeline().get_track_by_id("audio-track").clips) == clips_before
    view._drag_assets = []
    view._drag_asset_ids = []

    # Bus 轨不是片段落点：hover 与提交共用同一拒绝。
    bus_target = TimelineDropTarget(
        frame=10, track_id="audio-track", track_type="audio", is_valid=True)
    monkeypatch.setattr(
        view._layout_engine, "resolve_drop_target",
        lambda *_args, **_kwargs: bus_target)
    bus_track = replace(audio_track, is_bus=True)
    real_get_track = TimelineSequence.get_track_by_id
    monkeypatch.setattr(
        TimelineSequence, "get_track_by_id",
        lambda self, tid: bus_track if tid == "audio-track" else real_get_track(self, tid))
    try:
        drop = view._resolve_media_drop_target(QPointF(300.0, 60.0), "audio", False)
        assert not drop.is_valid and "Bus" in drop.reason
    finally:
        monkeypatch.setattr(TimelineSequence, "get_track_by_id", real_get_track)
    drop = view._resolve_media_drop_target(QPointF(300.0, 60.0), "audio", False)
    assert drop.is_valid and drop.track_id == "audio-track"


def test_clip_editor_audio_edit_stack_commits_and_undoes(project_host, qtbot, tmp_path) -> None:
    """P0-3：静音选区 → 工程命令 → 机架/预览刷新 → 一次撤销还原。"""
    host = project_host
    view = host.timeline_panel.timeline_view
    audio_path = tmp_path / "stack.wav"
    with wave.open(str(audio_path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(b"\x30\x01\x30\x01" * 48000 * 5)  # 5 s 恒定电平
    asset = MediaAsset(
        asset_id="audio", path=str(audio_path), media_type="audio", display_name="Stack",
        metadata={"audio_info": {"sample_rate": 48000, "channels": 2},
                  "duration_seconds": 5.0, "fps": 24.0},
    )
    host._current_edit_project = EditProject("project", "Stack", assets={"audio": asset})
    host.show_mixer()
    clip_id = view.get_timeline().audio_tracks[0].clips[0].clip_id
    assert host._load_project_audio_clip_editor(clip_id)
    editor = host.dock_audio_clip_editor.widget()
    qtbot.waitUntil(lambda: editor._panel._pcm is not None, timeout=15000)
    panel = editor._panel

    # 选区中段静音（信号路径与「静音」按钮一致）。
    panel.canvas.setSelection(1.0, 4.0)
    assert panel._stop_button.isEnabled()
    silence_button = next(button for button in panel._selection_edit_buttons if button.text() == "静音")
    assert silence_button.isEnabled()
    silence_button.click()
    edited = view.get_timeline().get_clip_by_id(clip_id)
    assert edited.audio_edit is not None
    steps = edited.audio_edit["steps"]
    assert len(steps) == 1 and steps[0]["processor_id"] == "mute"
    assert steps[0]["params"]["start"] == 48000 and steps[0]["params"]["end"] == 192000
    # 命令落地后机架与预览刷新（预览经 worker 异步发布）：
    # 中段被静音，边缘保持 3 ms 过渡。
    assert len(panel._process_steps) == 1 and panel._process_steps[0]["type"] == "mute"
    raw_mid = float(panel._raw_pcm[0][96000])
    qtbot.waitUntil(lambda: abs(float(panel._pcm[0][96000])) < 1e-6, timeout=15000)
    edited_mid = float(panel._pcm[0][96000])
    assert raw_mid > 0.005 and abs(edited_mid) < 1e-6
    assert abs(float(panel._pcm[0][48144])) < float(panel._raw_pcm[0][48144])
    revision = view.get_timeline().revision

    # 旁路是命令（可撤销），A/B 开关只是监听状态。
    step_id = steps[0]["step_id"]
    panel.processStepToggled.emit(step_id, False)
    assert view.get_timeline().get_clip_by_id(clip_id).audio_edit["steps"][0]["enabled"] is False
    qtbot.waitUntil(
        lambda: abs(float(panel._pcm[0][96000]) - raw_mid) < 1e-6, timeout=15000)
    # 步骤在模型中已旁路 → 预览回原始；A/B 开关在此基础上只是监听态。
    panel.btn_bypass.setChecked(True)
    qtbot.waitUntil(
        lambda: abs(float(panel._pcm[0][96000]) - raw_mid) < 1e-6, timeout=15000)
    panel.btn_bypass.setChecked(False)
    qtbot.waitUntil(
        lambda: abs(float(panel._pcm[0][96000]) - raw_mid) < 1e-6, timeout=15000)

    view.undo()
    assert view.get_timeline().get_clip_by_id(clip_id).audio_edit["steps"][0]["enabled"] is True
    view.undo()
    assert view.get_timeline().get_clip_by_id(clip_id).audio_edit is None
    assert view.get_timeline().revision > revision  # 命令与撤销都入历史


def test_clip_editor_denoise_commits_pre_render_step(project_host, qtbot, tmp_path) -> None:
    """§3.3 + P1-6：采集噪声 → 降噪入栈（spectral_denoise 预渲染步骤）。

    入栈后：模型持步骤（幂等替换）、机架显示、预览 PCM 与缓存基底
    同语义（噪声区 RMS 显著下降）、撤销还原。
    """
    import numpy as np

    host = project_host
    view = host.timeline_panel.timeline_view
    rate = 48000
    rng = np.random.default_rng(20261118)
    noise = (rng.standard_normal((2, 4 * rate)) * 0.02).astype(np.float32)
    audio_path = tmp_path / "noisy.wav"
    interleaved = np.empty((noise.shape[1], 2), dtype="<i2")
    interleaved[:, 0] = (noise[0] * 32000).astype("<i2")
    interleaved[:, 1] = (noise[1] * 32000).astype("<i2")
    with wave.open(str(audio_path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(interleaved.tobytes())
    asset = MediaAsset(
        asset_id="audio", path=str(audio_path), media_type="audio", display_name="Noisy",
        metadata={"audio_info": {"sample_rate": rate, "channels": 2},
                  "duration_seconds": 4.0, "fps": 24.0},
    )
    host._current_edit_project = EditProject("project", "Noisy", assets={"audio": asset})
    host.show_mixer()
    clip_id = view.get_timeline().audio_tracks[0].clips[0].clip_id
    assert host._load_project_audio_clip_editor(clip_id)
    editor = host.dock_audio_clip_editor.widget()
    qtbot.waitUntil(lambda: editor._panel._pcm is not None, timeout=15000)
    panel = editor._panel

    # 采集 [0.05, 0.95) s 的纯噪声 → 降噪入栈选区 [1.0, 4.0) s。
    panel.canvas.setSelection(0.05, 0.95)
    panel.noisePrintCaptured.emit(0.05, 0.95)
    assert panel._noise_profile is not None and panel._noise_profile.is_valid
    panel.canvas.setSelection(1.0, 4.0)
    panel.denoiseRequested.emit(1.0, 4.0, 12.0)

    edited = view.get_timeline().get_clip_by_id(clip_id)
    assert edited.audio_edit is not None
    steps = edited.audio_edit["steps"]
    assert len(steps) == 1 and steps[0]["processor_id"] == "spectral_denoise"
    params = steps[0]["params"]
    assert params["start"] == 48000 and params["end"] == 192000
    assert params["noise_end"] > params["noise_start"] >= 0
    assert params["reduction_db"] == 12.0
    # 机架由模型驱动显示降噪步骤。
    assert len(panel._process_steps) == 1
    assert panel._process_steps[0]["type"] == "spectral_denoise"

    # 预览与缓存基底同语义：噪声区 RMS 显著下降（≥4 dB）。
    def _rms(data: np.ndarray) -> float:
        return float(np.sqrt(np.mean(np.square(data[0, 60000:180000].astype(np.float64)))))

    raw_rms = _rms(panel._raw_pcm)
    qtbot.waitUntil(
        lambda: _rms(panel._pcm) < raw_rms * 10 ** (-4.0 / 20.0), timeout=15000)
    edited_rms = _rms(panel._pcm)
    assert raw_rms > 0.005
    assert edited_rms < raw_rms * 10 ** (-4.0 / 20.0)

    # 重复提交是幂等替换（同一处理器只保留最新参数）。
    panel.denoiseRequested.emit(1.0, 4.0, 6.0)
    steps = view.get_timeline().get_clip_by_id(clip_id).audio_edit["steps"]
    assert len(steps) == 1 and steps[0]["params"]["reduction_db"] == 6.0

    view.undo()  # 撤销 6 dB 替换提交
    view.undo()  # 撤销首次入栈
    assert view.get_timeline().get_clip_by_id(clip_id).audio_edit is None


def test_clip_editor_timepitch_commit_changes_length_and_undoes(
        project_host, qtbot, tmp_path, monkeypatch) -> None:
    """§6.2：变速预览 → 提交（gap 模式）→ 片段变短 + 步骤入栈 → 一次撤销。"""
    import numpy as np

    host = project_host
    view = host.timeline_panel.timeline_view
    rate = 48000
    audio_path = tmp_path / "stretch.wav"
    t = np.arange(5 * rate) / rate
    tone = (0.2 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    interleaved = np.empty((tone.size, 2), dtype="<i2")
    interleaved[:, 0] = (tone * 32000).astype("<i2")
    interleaved[:, 1] = (tone * 32000).astype("<i2")
    with wave.open(str(audio_path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(interleaved.tobytes())
    asset = MediaAsset(
        asset_id="audio", path=str(audio_path), media_type="audio", display_name="Stretch",
        metadata={"audio_info": {"sample_rate": rate, "channels": 2},
                  "duration_seconds": 5.0, "fps": 24.0},
    )
    host._current_edit_project = EditProject("project", "Stretch", assets={"audio": asset})
    host.show_mixer()
    clip_id = view.get_timeline().audio_tracks[0].clips[0].clip_id
    assert host._load_project_audio_clip_editor(clip_id)
    editor = host.dock_audio_clip_editor.widget()
    qtbot.waitUntil(lambda: editor._panel._pcm is not None, timeout=15000)
    panel = editor._panel
    old_duration = view.get_timeline().get_clip_by_id(clip_id).timeline_duration

    # 对话框固定为“留空 + 独立音频编辑”（无链接视频时勾选不生效）。
    monkeypatch.setattr(
        type(editor), "_ask_length_change_options",
        lambda self, delta_frames, linked: (False, True))

    # 预览：150% 作用于 [1, 4) s → 待提交参数记录在面板上。
    panel.canvas.setSelection(1.0, 4.0)
    panel.stretch_knob.setValue(150.0, emit=False)
    assert panel._timepitch_button.isEnabled()
    panel._timepitch_button.click()
    qtbot.waitUntil(lambda: panel._timepitch_pcm is not None, timeout=10000)
    assert panel._pending_timepitch is not None
    assert panel._timepitch_commit_button.isEnabled()

    # 提交：改长度事务一次落库（片段时长 + audio_edit time_stretch）。
    panel._timepitch_commit_button.click()
    edited = view.get_timeline().get_clip_by_id(clip_id)
    assert edited.audio_edit is not None
    step = edited.audio_edit["steps"][0]
    assert step["processor_id"] == "time_stretch"
    assert step["params"]["ratio_pct"] == 150.0
    # 3 s 选区 → 2 s：片段时长按比例缩短（5 s → 4 s）。
    assert edited.timeline_duration < old_duration
    assert edited.timeline_duration == pytest.approx(old_duration * 4 / 5, abs=1)
    # 域级映射：240000 − 144000 + 96000。
    assert step["params"]["output_length"] == 192000
    assert panel._pending_timepitch is None
    qtbot.waitUntil(lambda: panel._raw_pcm is not None, timeout=10000)
    for change in (lambda: panel.processStepToggled.emit(step["step_id"], False),
                   lambda: panel.processStepRemoved.emit(step["step_id"]),
                   panel.stack_clear_requested.emit):
        change()
        assert view.get_timeline().get_clip_by_id(clip_id) == edited

    # 一次撤销还原长度与处理栈。
    view.undo()
    restored = view.get_timeline().get_clip_by_id(clip_id)
    assert restored.timeline_duration == old_duration
    assert restored.audio_edit is None


def test_clip_editor_delete_range_commits_and_undoes(
        project_host, qtbot, tmp_path, monkeypatch) -> None:
    """§3 控件映射：删除选区 → delete_range 步骤 + 长度事务。"""
    import numpy as np

    host = project_host
    view = host.timeline_panel.timeline_view
    rate = 48000
    audio_path = tmp_path / "delete.wav"
    t = np.arange(3 * rate) / rate
    tone = (0.2 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    interleaved = np.empty((tone.size, 2), dtype="<i2")
    interleaved[:, 0] = (tone * 32000).astype("<i2")
    interleaved[:, 1] = (tone * 32000).astype("<i2")
    with wave.open(str(audio_path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(interleaved.tobytes())
    asset = MediaAsset(
        asset_id="audio", path=str(audio_path), media_type="audio", display_name="Del",
        metadata={"audio_info": {"sample_rate": rate, "channels": 2},
                  "duration_seconds": 3.0, "fps": 24.0},
    )
    host._current_edit_project = EditProject("project", "Del", assets={"audio": asset})
    host.show_mixer()
    clip_id = view.get_timeline().audio_tracks[0].clips[0].clip_id
    assert host._load_project_audio_clip_editor(clip_id)
    editor = host.dock_audio_clip_editor.widget()
    qtbot.waitUntil(lambda: editor._panel._pcm is not None, timeout=15000)
    panel = editor._panel
    old_duration = view.get_timeline().get_clip_by_id(clip_id).timeline_duration

    monkeypatch.setattr(
        type(editor), "_ask_length_change_options",
        lambda self, delta_frames, linked: (False, True))
    panel.canvas.setSelection(1.0, 2.0)  # 删除中段 1 s
    panel.deleteRangeRequested.emit(1.0, 2.0)

    edited = view.get_timeline().get_clip_by_id(clip_id)
    assert edited.audio_edit is not None
    step = edited.audio_edit["steps"][0]
    assert step["processor_id"] == "delete_range"
    # 域级映射：240000（5 s 输出域）− 48000 = 192000。
    assert step["params"]["output_length"] == 192000
    # 显示域中段 1 s 被删：片段时长 −24 帧。
    assert edited.timeline_duration == old_duration - 24
    view.undo()
    restored = view.get_timeline().get_clip_by_id(clip_id)
    assert restored.timeline_duration == old_duration
    assert restored.audio_edit is None


def test_clip_editor_spectral_repair_commits_and_undoes(
        project_host, qtbot, tmp_path) -> None:
    """§5 画笔：频谱选区修复 → spectral_repair 预渲染步骤 → 撤销还原。"""
    import numpy as np

    host = project_host
    view = host.timeline_panel.timeline_view
    rate = 48000
    audio_path = tmp_path / "repair.wav"
    t = np.arange(2 * rate) / rate
    sig = (0.2 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    interfer = np.zeros_like(t, dtype=np.float32)
    interfer[int(0.5 * rate):int(0.58 * rate)] = \
        (0.3 * np.sin(2 * np.pi * 2000.0 * t[int(0.5 * rate):int(0.58 * rate)]))
    stereo = sig + interfer
    interleaved = np.empty((stereo.size, 2), dtype="<i2")
    interleaved[:, 0] = (stereo * 32000).astype("<i2")
    interleaved[:, 1] = (stereo * 32000).astype("<i2")
    with wave.open(str(audio_path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(interleaved.tobytes())
    asset = MediaAsset(
        asset_id="audio", path=str(audio_path), media_type="audio", display_name="Repair",
        metadata={"audio_info": {"sample_rate": rate, "channels": 2},
                  "duration_seconds": 2.0, "fps": 24.0},
    )
    host._current_edit_project = EditProject("project", "Repair", assets={"audio": asset})
    host.show_mixer()
    clip_id = view.get_timeline().audio_tracks[0].clips[0].clip_id
    assert host._load_project_audio_clip_editor(clip_id)
    editor = host.dock_audio_clip_editor.widget()
    qtbot.waitUntil(lambda: editor._panel._pcm is not None, timeout=15000)
    panel = editor._panel

    # 频谱选区修复（时间 × 频带）→ 修复按钮提交。
    panel.btn_tool_brush.click()
    assert panel.btn_spectral.isChecked() and panel.canvas._tool_mode == "brush"
    panel.canvas.setSelection(0.45, 0.62, 1200.0, 3200.0)
    repair_button = next(button for button in panel._selection_edit_buttons if button.text() == "修复")
    assert repair_button.isEnabled() and panel.btn_tool_brush.isEnabled()
    repair_button.click()
    edited = view.get_timeline().get_clip_by_id(clip_id)
    assert edited.audio_edit is not None
    step = edited.audio_edit["steps"][0]
    assert step["processor_id"] == "spectral_repair"
    params = step["params"]
    assert params["mode"] == "heal"
    assert params["strokes"] and params["strokes"][0]["hz"] > 1000.0
    assert params["strokes"][0]["time_radius"] > 0
    assert params["strokes"][0]["bandwidth_oct"] > 0.0
    # 机架显示修复步骤；一次撤销清栈。
    assert panel._process_steps[0]["type"] == "spectral_repair"
    view.undo()
    assert view.get_timeline().get_clip_by_id(clip_id).audio_edit is None


def test_timeline_shows_clip_volume_envelope_overlay(
        project_host, qtbot, tmp_path) -> None:
    """P1-4 余项：包络提交命令后，时间线音频片段叠加包络折线（模型驱动）。"""
    import numpy as np

    host = project_host
    view = host.timeline_panel.timeline_view
    rate = 48000
    audio_path = tmp_path / "env.wav"
    with wave.open(str(audio_path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x10\x01\x10\x01" * rate * 4)  # 4 s 恒定电平
    asset = MediaAsset(
        asset_id="audio", path=str(audio_path), media_type="audio", display_name="Env",
        metadata={"audio_info": {"sample_rate": rate, "channels": 2},
                  "duration_seconds": 4.0, "fps": 24.0},
    )
    host._current_edit_project = EditProject("project", "Env", assets={"audio": asset})
    host.show_mixer()
    clip_id = view.get_timeline().audio_tracks[0].clips[0].clip_id
    assert host._load_project_audio_clip_editor(clip_id)
    editor = host.dock_audio_clip_editor.widget()
    qtbot.waitUntil(lambda: editor._panel._pcm is not None, timeout=15000)
    panel = editor._panel

    item = view._items_map.get(clip_id)
    assert item is not None and item._envelope_nodes == []

    # 画布包络（拖拽结束/增删节点发一次）→ 命令 → 时间线叠加折线。
    panel.envelopeChanged.emit([
        {"t": 0.0, "db": 0.0}, {"t": 2.0, "db": -12.0}])
    qtbot.waitUntil(lambda: bool(view._items_map[clip_id]._envelope_nodes),
                    timeout=5000)
    nodes = view._items_map[clip_id]._envelope_nodes
    fractions = [round(fraction, 3) for fraction, _db in nodes]
    assert [round(db, 2) for _f, db in nodes] == [0.0, -12.0]
    assert fractions[0] == 0.0
    assert 0.3 <= fractions[1] <= 0.5  # 2 s / 4~5 s 显示域

    # 撤销后折线消失。
    view.undo()
    qtbot.waitUntil(
        lambda: not bool(view._items_map[clip_id]._envelope_nodes), timeout=5000)


def test_automation_lane_mode_commits_per_track(project_host, qtbot) -> None:
    """§7 P1：Automation Lane 模式按轨道入时间线历史（Read 实际回放）。"""
    host = project_host
    host.show_mixer()
    view = host.timeline_panel.timeline_view
    track_id = view.get_timeline().audio_tracks[0].track_id
    host._on_mixer_show_automation(track_id)
    lane = host._automation_lanes[track_id]
    try:
        assert lane._mode_combo.currentText() == "Off"  # 模型回填
        lane._mode_combo.setCurrentText("Read")
        track = view.get_timeline().get_track_by_id(track_id)
        assert track.automation_mode == "read"
        # 引擎 per-track 模式即时下发（Read 曲线回放的开关）。
        mgr = host._get_mixer_audio_manager()
        assert mgr is None or mgr._automation_modes.get(track_id) == "read"
        view.undo()
        assert view.get_timeline().get_track_by_id(
            track_id).automation_mode == "off"
    finally:
        lane.close()
        lane.deleteLater()
        host._automation_lanes.pop(track_id, None)


def test_channel_strip_shows_send_count(project_host, qtbot) -> None:
    """§7 P1 通道条：Send 徽标计数 + 点击入口（路由对话框信号）。"""
    from src.features.edit.core.timeline_v2 import Send

    host = project_host
    host.show_mixer()
    view = host.timeline_panel.timeline_view
    timeline = view.get_timeline()
    track = timeline.audio_tracks[0]
    host._sync_audio_mixer_from_timeline(timeline)
    strip = host.audio_mixer_panel._track_mixers[track.track_id]
    assert strip.sends_button.text() == "S"

    # 加一条活跃 send（目标占位 Bus ID；徽标显示活跃计数）。
    updated_timeline = timeline.with_track_updated(replace(track, sends=(
        Send(target_bus_id="bus-x", level=0.5),
        Send(target_bus_id="bus-mute", level=0.5, muted=True),
        Send(target_bus_id="bus-zero", level=0.0),
        Send(target_bus_id="bus-disabled", level=0.5, enabled=False),
    )))
    host._sync_audio_mixer_from_timeline(updated_timeline)
    strip = host.audio_mixer_panel._track_mixers[track.track_id]
    assert strip.sends_button.text().startswith("S1")
    assert strip.sends_button.toolTip().endswith("1/4")
    # 点击入口走 show_routing（与右键路由同一命令路径）。
    requested: list[str] = []
    strip.show_routing.connect(requested.append)
    strip.sends_button.click()
    assert requested == [track.track_id]


def test_linked_length_change_shows_independent_audio_option(qtbot, monkeypatch) -> None:
    editor = ProjectClipEditor()
    qtbot.addWidget(editor)

    def accept_gap(box: QMessageBox) -> int:
        assert box.checkBox() is not None and not box.checkBox().isChecked()
        box.checkBox().setChecked(True)
        button = next(button for button in box.buttons()
                      if box.buttonRole(button) == QMessageBox.ButtonRole.AcceptRole)
        button.click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", accept_gap)
    assert editor._ask_length_change_options(-12, linked=True) == (False, True)


def test_effect_panel_applies_spatial_panner_to_audio_track_only(project_host) -> None:
    host = project_host
    host.show_mixer()
    view = host.timeline_panel.timeline_view
    before = view.get_timeline()
    audio = before.audio_tracks[0]
    audio_clip = audio.clips[0]
    video_clip = before.video_tracks[0].clips[0]

    host._on_edit_effect_add_requested("spatial_panner", video_clip.clip_id)
    assert view.get_timeline() is before
    host._on_edit_effect_add_requested("gaussian_blur", audio_clip.clip_id)
    assert view.get_timeline() is before
    host._on_edit_effect_add_requested("spatial_panner", audio_clip.clip_id)
    with_panner = view.get_timeline()
    assert with_panner.audio_tracks[0].panner["enabled"] is True
    assert with_panner.get_clip_by_id(audio_clip.clip_id).effects == audio_clip.effects
    host._on_edit_effect_add_requested("spatial_panner", audio_clip.clip_id)
    assert view.get_timeline() is with_panner
    view.undo()
    assert not view.get_timeline().audio_tracks[0].panner


def test_insert_menu_exposes_existing_sound_design_effects(project_host) -> None:
    strip = project_host.audio_mixer_panel._track_mixers["audio-track"]
    menu = QMenu(strip)
    strip._populate_add_effect_menu(menu)
    by_id = {action.data(): action for action in menu.actions()}
    for effect_id in ("voice_changer", "spectral_mangler"):
        by_id[effect_id].trigger()
    track = project_host.timeline_panel.timeline_view.get_timeline().audio_tracks[0]
    assert [effect.effect_type for effect in track.effect_chain.effects] == ["voice_changer", "spectral_mangler"]


def test_recording_prep_popover_updates_settings_chain(project_host, qtbot, monkeypatch) -> None:
    """§7 P1：混音侧录音准备弹窗 → 设置链（ADR 面板缺席时兜底生效）。"""
    host = project_host
    host.show_mixer()
    view = host.timeline_panel.timeline_view
    track_id = view.get_timeline().audio_tracks[0].track_id
    strip = host.audio_mixer_panel._track_mixers[track_id]
    assert strip.record_prep_button.isVisible() or not strip.record_prep_button.isHidden()

    # 设备枚举固定（离屏环境无真实设备）。
    from src.features.edit.core import recording_manager
    monkeypatch.setattr(
        recording_manager.RecordingManager, "list_input_devices",
        classmethod(lambda cls: [("dev-1", "USB Mic"), ("dev-2", "Interface 2ch")]))

    # 弹窗自动接受并选择第二台设备 + 立体声 + 监听。
    from PyQt6.QtWidgets import QDialog
    accepted = {}

    def _auto_accept(dialog) -> int:
        from PyQt6.QtWidgets import QCheckBox, QComboBox, QDoubleSpinBox
        for box in dialog.findChildren(QComboBox):
            if box.count() >= 3 and box.itemData(1) == "dev-1":
                box.setCurrentIndex(2)  # dev-2
            elif box.count() == 2 and box.itemData(1) == 2:
                box.setCurrentIndex(1)  # stereo（index 1 的 data 是 2）
        for spin in dialog.findChildren(QDoubleSpinBox):
            spin.setValue(3.0)
        for check in dialog.findChildren(QCheckBox):
            check.setChecked(True)
        accepted["ran"] = True
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", lambda dialog: _auto_accept(dialog))
    host._on_mixer_record_prep(track_id)
    assert accepted.get("ran") is True
    settings = host._adr_settings_dict()
    assert settings["device_id"] == "dev-2"
    assert settings["channels"] == 2
    assert settings["pre_roll_seconds"] == 3.0
    assert settings["monitor"] is True


def test_mixer_workspace_controls_expose_accessible_names(project_host, qtbot, tmp_path) -> None:
    """§7 P2 可访问性：新增控件的屏幕阅读器名称与状态反馈非空。"""
    host = project_host
    host.show_mixer()
    view = host.timeline_panel.timeline_view
    track_id = view.get_timeline().audio_tracks[0].track_id
    strip = host.audio_mixer_panel._track_mixers[track_id]
    for name, widget in (
        ("sends", strip.sends_button),
        ("record prep", strip.record_prep_button),
        ("output routing", strip.output_bus_combo),
    ):
        assert widget.accessibleName(), f"{name} button lacks accessible name"
    # Clip Editor 的提交按钮同样可被读屏识别。
    clip_id = view.get_timeline().audio_tracks[0].clips[0].clip_id
    audio_path = tmp_path / "a11y.wav"
    with wave.open(str(audio_path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(b"\x10\x01\x10\x01" * 48000)
    asset = MediaAsset(
        asset_id="audio", path=str(audio_path), media_type="audio", display_name="A11y",
        metadata={"audio_info": {"sample_rate": 48000, "channels": 2},
                  "duration_seconds": 1.0, "fps": 24.0},
    )
    host._current_edit_project = EditProject("project", "A11y", assets={"audio": asset})
    assert host._load_project_audio_clip_editor(clip_id)
    editor = host.dock_audio_clip_editor.widget()
    qtbot.waitUntil(lambda: editor._panel._pcm is not None, timeout=15000)
    commit_button = editor._panel._timepitch_commit_button
    assert commit_button is not None and commit_button.accessibleName()
