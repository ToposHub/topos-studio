"""Edit Viewport host entry points and legacy dock visibility migration."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QDockWidget, QMainWindow, QWidget
from pytestqt.qtbot import QtBot

from src.app.controllers.dock_layout_controller import DockLayoutMixin
from src.app.controllers.file_controller import FileOpsMixin
from src.features.edit.module import EditModule
from src.shared.panels.registry import PanelRegistry


@pytest.mark.parametrize(("page", "fitted"), [
    ("Edit", {"edit_viewport"}),
    ("Color", {"canvas"}),
    ("Composite", {"composite_viewer"}),
    ("Mixer", {"edit_viewport"}),
    ("Library", set()),
])
def test_project_window_resize_fits_active_viewers(qtbot: QtBot, page: str, fitted: set[str]) -> None:
    class Host(FileOpsMixin, QMainWindow):
        pass

    host = Host()
    qtbot.addWidget(host)
    host.current_page = page
    host.files_panel = None
    fit_calls = {name: MagicMock() for name in ("edit_viewport", "canvas", "composite_viewer")}
    for name, fit in fit_calls.items():
        setattr(host, name, SimpleNamespace(
            isVisible=lambda name=name: name in fitted,
            fit_visible_monitors=fit,
            fit_to_window=fit,
        ))
    host.resize(1000, 700)
    host.show()
    host.resize(1200, 720)
    qtbot.waitUntil(lambda: getattr(host, "_viewer_fit_resize_page", None) is None)
    assert {name for name, fit in fit_calls.items() if fit.called} == fitted


def test_edit_registers_a_single_viewport_panel() -> None:
    registry = PanelRegistry()
    EditModule.register_panels(SimpleNamespace(_available=True), registry)
    viewport = registry.get("edit.viewport")
    assert viewport.fallback_title == "Viewport"
    assert viewport.dock_attr == "dock_program_viewer"
    assert registry.get("edit.source_viewer") is None
    assert registry.get("edit.program_viewer") is None


def test_color_monitor_action_shares_one_viewer_and_releases_on_project_change(qapp) -> None:
    from PyQt6.QtWidgets import QLabel
    from src.features.color.ui.canvas import ViewportMenuBar
    from src.features.edit.ui.edit_page import EditPage
    from src.features.edit.ui.timeline_panel import TimelinePanel
    from src.features.edit.ui.viewport_scrubber import MonitorScrubber
    from src.app.main_window import MainWindow

    class Host(DockLayoutMixin, FileOpsMixin):
        pass

    host = Host()
    menu = ViewportMenuBar()
    slider = MonitorScrubber()
    slider.setRange(0, 199)
    dock = QDockWidget("Viewport")
    status = QLabel()
    controller = SimpleNamespace(current_frame=83, is_playing=False)
    panel = SimpleNamespace(_playback_controller=controller,
                            timeline_view=SimpleNamespace(get_playhead_frame=lambda: 4),
                            set_detached_program_playhead=MagicMock())
    panel.get_playhead_frame = lambda: TimelinePanel.get_playhead_frame(panel)
    panel.get_playback_controller = lambda: controller
    host.current_page = "Color"
    host.canvas = SimpleNamespace(viewport_menu=menu)
    host._current_edit_project = SimpleNamespace(project_id="A", active_timeline_id="timeline")
    host._current_timeline_sequences = {"timeline": object()}
    host.timeline_panel = panel
    host.edit_page = SimpleNamespace(_on_playhead_changed=MagicMock(), _scrub_active=False,
                                     shutdown_audio_playback=MagicMock())
    host.timeline_preview_renderer = MagicMock()
    host.edit_viewport = SimpleNamespace(program_slider=slider, set_program_only=MagicMock(),
                                         set_program_position=slider.setValue, timeline_monitor_status=status)
    host.dock_program_viewer = dock
    host.program_viewer_widget = MagicMock()
    host._ensure_program_monitor_output = MagicMock()
    host._submit_program_monitor_frame = MagicMock()
    target = object()
    host._color_timeline_clip_playback_context = {"clip": target}
    action = menu.timeline_composite_monitor_act
    action.triggered.connect(host.set_color_timeline_composite_monitor)
    menu.view_menu.aboutToShow.connect(host._sync_color_timeline_monitor_action)
    dock.visibilityChanged.connect(host._on_color_program_dock_visibility)
    page = SimpleNamespace(_is_active=False, _main_window=host)
    assert not action.isChecked() and not EditPage._preview_page_is_active(page)
    menu.view_menu.aboutToShow.emit()
    action.trigger()
    assert action.isChecked() and EditPage._preview_page_is_active(page)
    assert not dock.isHidden() and slider.value() == 83
    panel.set_detached_program_playhead.assert_called_with(host.edit_page._on_playhead_changed)
    host.timeline_preview_renderer.request_render.assert_called_once_with(83, request_kind="refresh")

    result = {"present_frame": SimpleNamespace(texture_id=1, slot_id=0)}
    host._on_timeline_preview_frame_ready(83, result)
    host.program_viewer_widget.set_frame.assert_called_once_with(83, result)
    assert host._color_program_output_kind == "gpu"
    host.timeline_preview_renderer.gpu_preview_ready = False
    host._on_timeline_preview_frame_ready(83, QImage(3, 2, QImage.Format.Format_RGB32))
    assert host._color_program_output_kind == "cpu" and "CPU" in status.text()

    next_project = SimpleNamespace(project_id="A", active_timeline_id="timeline")
    controller.current_frame = 91
    MainWindow._publish_color_graph_to_previews(host, next_project)
    host.timeline_preview_renderer.update_color_stacks.assert_called_once_with(next_project)
    host.timeline_preview_renderer.request_render.assert_called_with(91, request_kind="refresh")
    assert host._color_timeline_clip_playback_context["clip"] is target
    host._current_edit_project = SimpleNamespace(project_id="B", active_timeline_id="timeline")
    host._sync_color_timeline_monitor_action()
    assert not action.isChecked() and dock.isHidden() and dock.windowTitle() == "Viewport"
    panel.set_detached_program_playhead.assert_called_with(None)
    assert not EditPage._preview_page_is_active(page)
    assert not host.timeline_preview_renderer.shutdown.called
    host.timeline_preview_renderer.finish_scrub.assert_called_once_with(91, settle=False)
    assert host.program_viewer_widget.clear_texture.call_count == 2
    dock.close()
    menu.close()


def test_asset_double_click_opens_source_in_unified_viewport() -> None:
    asset = SimpleNamespace(asset_id="camera", display_name="Camera A.mov")
    viewport = SimpleNamespace(show_source=MagicMock())
    dock = SimpleNamespace(show=MagicMock(), raise_=MagicMock())
    source = SimpleNamespace(open_asset=MagicMock(), viewer=SimpleNamespace(setFocus=MagicMock()))
    host = SimpleNamespace(
        _current_edit_project=SimpleNamespace(assets={"camera": asset}),
        current_page="Edit", source_viewer_widget=source,
        edit_viewport=viewport, dock_program_viewer=dock,
        dock_source_viewer=None,
    )
    FileOpsMixin._on_asset_double_clicked(host, "camera")
    # R09：宿主把解释覆盖的生效帧率随打开传入（测试替身工程无覆盖 → None）
    source.open_asset.assert_called_once_with(asset, interpretation_fps=None)
    viewport.show_source.assert_called_once_with(focus=True)
    dock.show.assert_called_once_with()
    dock.raise_.assert_called_once_with()


def test_detached_program_uses_timeline_backpressure_without_painting_hidden_edit() -> None:
    from src.features.edit.ui.timeline_panel import TimelinePanel

    frames = []
    controller = SimpleNamespace(is_playing=False, current_frame=83, reanchor_to_frame=MagicMock())
    panel = SimpleNamespace(
        _playback_controller=controller, timeline_view=SimpleNamespace(isVisible=lambda: False),
        _should_defer_playback_tick=MagicMock(), playback_reanchored=MagicMock(),
        _update_timecode_display=MagicMock(), _set_view_playhead_from_controller=MagicMock())
    TimelinePanel.set_detached_program_playhead(panel, frames.append)
    TimelinePanel._on_controller_frame_changed(panel, 84)
    assert frames == [84]
    controller.is_playing = True
    TimelinePanel._on_controller_frame_changed(panel, 85)
    assert frames == [84]
    panel._should_defer_playback_tick.return_value = True
    TimelinePanel._on_timer_frame_ready(panel, 85)
    controller.reanchor_to_frame.assert_called_once_with(84, emit_frame_changed=False)
    panel._should_defer_playback_tick.return_value = False
    TimelinePanel._on_timer_frame_ready(panel, 86)
    assert frames == [84, 86]
    TimelinePanel.set_detached_program_playhead(panel, None)
    TimelinePanel._on_timer_frame_ready(panel, 87)
    assert frames == [84, 86]
    panel._set_view_playhead_from_controller.assert_not_called()
    panel._update_timecode_display.assert_not_called()


def test_color_monitor_reports_unavailable_preview_without_entering_mode(qapp) -> None:
    from PyQt6.QtGui import QAction

    action = QAction()
    action.setCheckable(True)
    status = MagicMock()
    host = SimpleNamespace(
        canvas=SimpleNamespace(viewport_menu=SimpleNamespace(timeline_composite_monitor_act=action)),
        _color_program_monitor_available=lambda: True, timeline_preview_renderer=None,
        _init_timeline_preview_renderer=MagicMock(), timeline_panel=MagicMock(), edit_viewport=MagicMock(),
        dock_program_viewer=MagicMock(), statusBar=lambda: status)
    DockLayoutMixin.set_color_timeline_composite_monitor(host, True)
    assert not action.isChecked() and not getattr(host, "_color_program_monitor_active", False)
    status.showMessage.assert_called_once()
    host.dock_program_viewer.show.assert_not_called()


def test_floating_and_redocking_color_program_keeps_session_until_actual_close(qapp) -> None:
    window = QMainWindow()
    dock = QDockWidget("Program", window)
    dock.setWidget(QWidget())
    window.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, dock)
    window.show()
    qapp.processEvents()
    close_monitor = MagicMock()
    host = SimpleNamespace(_color_program_monitor_active=True, _color_monitor_session=object(),
                           dock_program_viewer=dock, set_color_timeline_composite_monitor=close_monitor)
    dock.visibilityChanged.connect(lambda visible: DockLayoutMixin._on_color_program_dock_visibility(host, visible))
    dock.setFloating(True)
    qapp.processEvents()
    assert dock.isFloating() and not dock.isHidden()
    dock.setFloating(False)
    qapp.processEvents()
    assert not dock.isFloating() and not dock.isHidden()
    close_monitor.assert_not_called()
    # A queued hide belongs to its original owner even if the next session
    # happens to use the very same still-hidden dock.
    dock.hide()
    host._color_monitor_session = object()
    qapp.processEvents()
    close_monitor.assert_not_called()
    dock.show()
    dock.close()
    qapp.processEvents()
    close_monitor.assert_called_once_with(False)
    host._color_program_monitor_active = False
    window.close()


def test_project_replacement_retires_decode_provider_and_keeps_source_gpu_thread() -> None:
    old_renderer, old_worker, source_gpu = MagicMock(), MagicMock(), MagicMock()
    host = SimpleNamespace(
        timeline_preview_renderer=old_renderer, _source_decode_worker=old_worker,
        _source_gpu_renderer=source_gpu, _gpu_pipeline_initialized=True,
        program_viewer_widget=MagicMock(), source_viewer_widget=MagicMock(),
        _disconnect_timeline_preview_renderer_signals=MagicMock(), _current_edit_project=None,
        _setup_source_gpu_provider=MagicMock())
    host._release_timeline_preview_renderer = lambda: FileOpsMixin._release_timeline_preview_renderer(host)
    FileOpsMixin._init_timeline_preview_renderer(host)
    host._release_timeline_preview_renderer()
    host._disconnect_timeline_preview_renderer_signals.assert_called_once_with(old_renderer)
    host.program_viewer_widget.disable_native_viewport.assert_called_once_with()
    host.program_viewer_widget.clear_texture.assert_called_once_with()
    host.program_viewer_widget.set_realtime_mode.assert_called_once_with(False)
    host.source_viewer_widget.set_gpu_frame_provider.assert_called_once_with(None)
    old_worker.signals.cpu_fallback_requested.disconnect.assert_called_once_with(
        host.source_viewer_widget.request_cpu_fallback)
    old_renderer.deleteLater.assert_called_once_with()
    assert host.timeline_preview_renderer is None and host._gpu_decode_manager is None
    old_worker.stop.assert_called_once_with()
    old_renderer.shutdown.assert_called_once_with()
    assert not host._gpu_pipeline_initialized and host._source_decode_worker is None
    assert host._source_gpu_renderer is source_gpu
    new_manager = object()
    FileOpsMixin._init_source_gpu_pipeline(host, new_manager)
    host._setup_source_gpu_provider.assert_called_once_with(new_manager, source_gpu)


def test_quick_preview_keeps_current_monitor_and_focus() -> None:
    asset = SimpleNamespace(asset_id="camera", display_name="Camera A.mov")
    viewport = SimpleNamespace(show_source=MagicMock())
    source = SimpleNamespace(open_asset=MagicMock(), current_asset=None)
    host = SimpleNamespace(
        _current_edit_project=SimpleNamespace(assets={"camera": asset}),
        source_viewer_widget=source, edit_viewport=viewport,
    )
    FileOpsMixin._on_asset_selected(host, "camera")
    source.open_asset.assert_called_once_with(asset, interpretation_fps=None)
    viewport.show_source.assert_not_called()


def test_library_asset_opens_as_read_only_source_in_unified_viewport() -> None:
    asset = SimpleNamespace(asset_id="external", display_name="Library Clip")
    poster = QImage()
    viewport = SimpleNamespace(show_source=MagicMock())
    source = SimpleNamespace(open_asset=MagicMock(), viewer=SimpleNamespace(setFocus=MagicMock()))
    host = SimpleNamespace(
        current_page="Edit", source_viewer_widget=source, edit_viewport=viewport,
        dock_program_viewer=SimpleNamespace(show=MagicMock(), raise_=MagicMock()),
    )
    FileOpsMixin._on_library_source_preview(host, asset, poster)
    source.open_asset.assert_called_once_with(asset, preview_only=True, initial_preview=poster)
    viewport.show_source.assert_called_once_with(focus=True)


def test_source_only_legacy_layout_reveals_viewport_once(qapp) -> None:
    legacy = QMainWindow()
    for name, hidden in (("dockProgramViewer", True), ("dockSourceViewer", False)):
        dock = QDockWidget(name, legacy)
        dock.setObjectName(name)
        dock.setWidget(QWidget(dock))
        legacy.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, dock)
        dock.setVisible(not hidden)
    state = legacy.saveState()

    window = QMainWindow()
    viewport = QWidget()
    dock = QDockWidget("Viewport", window)
    dock.setObjectName("dockProgramViewer")
    dock.setWidget(viewport)
    window.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, dock)
    assert window.restoreState(state)
    assert dock.isHidden()

    data: dict[str, object] = {}
    config = SimpleNamespace(
        get=data.get, set=lambda key, value, auto_save=True: data.__setitem__(key, value),
        save_config=MagicMock(),
    )
    host = SimpleNamespace(
        current_page="Edit", edit_viewport=viewport, dock_program_viewer=dock,
        config_manager=config, page_layouts={}, saveState=window.saveState,
        _current_product_mode=lambda: "studio", _current_work_mode=lambda: "video",
        _save_current_layout_to_store=MagicMock(),
    )
    DockLayoutMixin._migrate_edit_viewport_visibility(host)
    assert not dock.isHidden()
    assert dock.widget() is viewport
    assert not host.page_layouts["Edit"].isEmpty()
    dock.hide()
    DockLayoutMixin._migrate_edit_viewport_visibility(host)
    assert dock.isHidden()
    config.save_config.assert_called_once_with()
    legacy.close()
    window.close()


def test_new_edit_renderer_receives_monitor_budget_before_first_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.app.controllers import file_controller

    host, renderer = MagicMock(), MagicMock()
    host.current_page = "Edit"
    host._current_edit_project.active_timeline_id = "timeline"
    host._current_timeline_sequences = {"timeline": object()}
    host.program_viewer_widget = None
    monkeypatch.setattr(file_controller, "create_timeline_preview_renderer", lambda **kwargs: renderer)
    observed: list[object] = []
    host._viewport_budget_executor.refresh.side_effect = lambda: observed.append(host.timeline_preview_renderer)
    host.edit_page.set_timeline.side_effect = lambda sequence: observed.append(sequence)

    FileOpsMixin._init_timeline_preview_renderer(host)
    assert observed == [renderer, host._current_timeline_sequences["timeline"]]
    host._request_current_timeline_preview_frame.assert_called_once_with()
