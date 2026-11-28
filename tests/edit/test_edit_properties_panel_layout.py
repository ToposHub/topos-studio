"""Regression tests for the Edit Properties dock sizing contract."""

from __future__ import annotations

from unittest.mock import MagicMock

from PyQt6.QtCore import QByteArray, Qt
from PyQt6.QtWidgets import QDockWidget, QMainWindow, QWidget

from src.app.controllers.dock_layout_controller import (
    DockLayoutMixin,
    _AI_ASSISTANT_DOCK_LAYOUT_MIGRATION_KEY,
    _AI_ASSISTANT_DOCK_LAYOUT_MIGRATION_VERSION,
    _COLOR_GRADING_DOCK_LAYOUT_MIGRATION_KEY,
    _COLOR_GRADING_DOCK_LAYOUT_MIGRATION_VERSION,
    _EDIT_TIMELINE_DOCK_LAYOUT_MIGRATION_KEY,
    _EDIT_TIMELINE_DOCK_LAYOUT_MIGRATION_VERSION,
    _EDIT_PROPERTIES_DOCK_LAYOUT_MIGRATION_KEY,
    _EDIT_PROPERTIES_DOCK_LAYOUT_MIGRATION_VERSION,
    _EDIT_PROPERTIES_DOCK_TARGET_WIDTH,
)
from src.features.edit.ui.edit_properties_panel import (
    EditPropertiesPanel,
    KeyframeIndicatorButton,
    _VALUE_FIXED_WIDTH,
)
from src.features.edit.core.timeline_v2 import TransitionInstance


def _make_mock_clip() -> MagicMock:
    """Build a minimal mock clip that drives set_clip to render full sections."""
    clip = MagicMock()
    clip.clip_id = "test-clip"
    clip.transform = MagicMock()
    clip.crop = MagicMock()
    clip.crop.left = clip.crop.right = clip.crop.top = clip.crop.bottom = 0.0
    clip.crop.feather = 0.0
    clip.opacity = 1.0
    clip.speed = MagicMock()
    clip.speed.speed = 1.0
    clip.speed.method = "linear"
    clip.effects = []
    clip.asset_id = "asset-1"
    clip.name = "Test"
    clip.timeline_start = 0
    clip.timeline_end = 100
    return clip


def _expand_transform_section(panel) -> None:
    """Force-expand the Transform section (defaults to collapsed now).

    Layout/width assertions need the section's content widget to be visible
    and properly laid out; a collapsed section returns bogus geometry.
    """
    section = panel._sections.get("clip:test-clip:transform")
    if section is not None and getattr(section, "_is_collapsed", False):
        section.toggle_collapse()


def test_properties_panel_does_not_mutate_parent_dock_width(qapp) -> None:
    """Playback/layout resizes must not lock the user-controlled dock splitter."""
    dock = QDockWidget()
    panel = EditPropertiesPanel()
    dock.setWidget(panel)
    dock.setMaximumWidth(16777215)

    panel.resize(420, 480)
    qapp.processEvents()

    assert dock.maximumWidth() == 16777215
    assert panel.minimumWidth() == 280


def test_transition_properties_panel_builds_float_parameter_row(qapp) -> None:
    """Selecting a transition must build the new softness control safely."""
    panel = EditPropertiesPanel()
    transition = TransitionInstance(
        transition_id="transition-test",
        transition_type="iris_circle",
        track_id="v1",
        left_clip_id="left",
        right_clip_id="right",
        center_frame=20,
        duration=12,
        params={"softness": 0.08},
    )

    panel.set_transition(transition)

    assert "_build_transition_float_row" in EditPropertiesPanel.__dict__
    assert "transition:transition-test" in panel._sections


class _LayoutConfig:
    def __init__(self, data: dict | None = None) -> None:
        self.data = data or {}
        self.saved = False

    def get(self, key: str):
        return self.data.get(key)

    def set(self, key: str, value, auto_save: bool = True) -> None:
        self.data[key] = value

    def save_config(self) -> None:
        self.saved = True


class _EditLayoutMigrationHost:
    """Small DockLayoutMixin-shaped host for migration behavior tests."""

    def __init__(self, config: _LayoutConfig) -> None:
        self.config_manager = config
        self.current_page = "Edit"
        self.dock_properties = MagicMock()
        self.dock_properties.isFloating.return_value = False
        self.page_layouts: dict[str, QByteArray] = {}
        self.resizeDocks = MagicMock()
        self._save_current_layout_to_store = MagicMock()

    def dockWidgetArea(self, dock) -> Qt.DockWidgetArea:
        assert dock is self.dock_properties
        return Qt.DockWidgetArea.RightDockWidgetArea

    def saveState(self) -> QByteArray:
        return QByteArray(b"corrected-edit-layout")


class _AiAssistantLayoutMigrationHost(_EditLayoutMigrationHost):
    """Minimal host for the AI assistant tab-group migration."""

    def __init__(self, config: _LayoutConfig, properties_dock, assistant_dock) -> None:
        super().__init__(config)
        self.dock_properties = properties_dock
        self.dock_ai_edit_assistant = assistant_dock
        self.tabifyDockWidget = MagicMock()

    def dockWidgetArea(self, dock) -> Qt.DockWidgetArea:
        assert dock in (self.dock_properties, self.dock_ai_edit_assistant)
        return Qt.DockWidgetArea.RightDockWidgetArea


class _TimelineLayoutMigrationHost(_EditLayoutMigrationHost):
    """Minimal host for Timeline dock height migration behavior."""

    def __init__(self, config: _LayoutConfig, timeline_dock) -> None:
        super().__init__(config)
        self.dock_edit_timeline = timeline_dock
        self.dock_program_viewer = MagicMock()
        self.dock_program_viewer.isFloating.return_value = False
        self.dock_program_viewer.height.return_value = 300
        self.dock_program_viewer.minimumHeight.return_value = 150

    def dockWidgetArea(self, dock) -> Qt.DockWidgetArea:
        if dock is self.dock_edit_timeline:
            return Qt.DockWidgetArea.BottomDockWidgetArea
        assert dock is self.dock_program_viewer
        return Qt.DockWidgetArea.TopDockWidgetArea


class _ColorGradingLayoutMigrationHost(_EditLayoutMigrationHost):
    """Minimal host for Color Wheel dock height migration behavior."""

    def __init__(self, config: _LayoutConfig, grading_dock) -> None:
        super().__init__(config)
        self.current_page = "Color"
        self.dock_grading = grading_dock
        self.all_docks = [grading_dock]
        self.rect = MagicMock()
        self.dock_viewport = MagicMock()
        self.dock_viewport.isFloating.return_value = False
        self.dock_viewport.isHidden.return_value = False
        self.dock_viewport.height.return_value = 300
        self.dock_viewport.minimumHeight.return_value = 150
        self._save_all_docked_sizes = MagicMock()

    def dockWidgetArea(self, dock) -> Qt.DockWidgetArea:
        if dock is self.dock_grading:
            return Qt.DockWidgetArea.BottomDockWidgetArea
        assert dock is self.dock_viewport
        return Qt.DockWidgetArea.TopDockWidgetArea


def test_legacy_edit_properties_width_migrates_once() -> None:
    """The correction must persist once without becoming a splitter lock."""
    config = _LayoutConfig({"page_layouts": {"Edit": "legacy-layout"}})
    host = _EditLayoutMigrationHost(config)

    DockLayoutMixin._migrate_edit_properties_dock_width(host)

    host.resizeDocks.assert_called_once_with(
        [host.dock_properties],
        [_EDIT_PROPERTIES_DOCK_TARGET_WIDTH],
        Qt.Orientation.Horizontal,
    )
    assert config.data[_EDIT_PROPERTIES_DOCK_LAYOUT_MIGRATION_KEY] == \
        _EDIT_PROPERTIES_DOCK_LAYOUT_MIGRATION_VERSION
    assert config.data["page_layouts"]["Edit"] == "636f727265637465642d656469742d6c61796f7574"
    assert host.page_layouts["Edit"] == QByteArray(b"corrected-edit-layout")
    assert config.saved is True

    DockLayoutMixin._migrate_edit_properties_dock_width(host)
    host.resizeDocks.assert_called_once()


def test_legacy_edit_properties_width_skips_non_edit_or_floating_docks() -> None:
    """The migration must not disturb other pages or a floating panel."""
    config = _LayoutConfig()
    host = _EditLayoutMigrationHost(config)
    host.current_page = "Color"

    DockLayoutMixin._migrate_edit_properties_dock_width(host)
    host.resizeDocks.assert_not_called()
    assert _EDIT_PROPERTIES_DOCK_LAYOUT_MIGRATION_KEY not in config.data

    host.current_page = "Edit"
    host.dock_properties.isFloating.return_value = True
    DockLayoutMixin._migrate_edit_properties_dock_width(host)
    host.resizeDocks.assert_not_called()
    assert _EDIT_PROPERTIES_DOCK_LAYOUT_MIGRATION_KEY not in config.data


def test_legacy_ai_assistant_layout_merges_right_side_tabs_once() -> None:
    """Old nested-dock state is migrated into one stable right-side tab stack."""
    config = _LayoutConfig({"page_layouts": {"Edit": "legacy-layout"}})
    properties_dock = MagicMock()
    assistant_dock = MagicMock()
    properties_dock.isFloating.return_value = False
    assistant_dock.isFloating.return_value = False
    host = _AiAssistantLayoutMigrationHost(
        config, properties_dock, assistant_dock
    )

    DockLayoutMixin._migrate_ai_assistant_dock_layout(host)

    host.tabifyDockWidget.assert_called_once_with(properties_dock, assistant_dock)
    assert config.data[_AI_ASSISTANT_DOCK_LAYOUT_MIGRATION_KEY] == \
        _AI_ASSISTANT_DOCK_LAYOUT_MIGRATION_VERSION
    assert config.data["page_layouts"]["Edit"] == \
        "636f727265637465642d656469742d6c61796f7574"
    assert host.page_layouts["Edit"] == QByteArray(b"corrected-edit-layout")
    assert config.saved is True

    DockLayoutMixin._migrate_ai_assistant_dock_layout(host)
    host.tabifyDockWidget.assert_called_once()


def test_ai_assistant_migration_keeps_the_shared_tab_width_stable(qapp) -> None:
    """Showing the repaired AI tab repeatedly must not widen the right stack."""
    from src.features.edit.ui.ai_edit_assistant_panel import AiEditAssistantPanel

    window = QMainWindow()
    window.setCentralWidget(QWidget())
    properties_dock = QDockWidget("Properties", window)
    properties_dock.setObjectName("dockProperties")
    properties_dock.setMinimumWidth(280)
    properties_dock.setWidget(QWidget())
    assistant_dock = AiEditAssistantPanel(window)
    window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, properties_dock)
    window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, assistant_dock)
    window.tabifyDockWidget(properties_dock, assistant_dock)
    window.resize(1200, 800)
    window.show()
    qapp.processEvents()

    # This mirrors the default Edit-layout rebuild: re-adding Properties
    # breaks the previously hidden assistant's tab relationship.
    assistant_dock.hide()
    window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, properties_dock)
    qapp.processEvents()

    config = _LayoutConfig({"page_layouts": {"Edit": "legacy-layout"}})
    window.config_manager = config
    window.current_page = "Edit"
    window.dock_properties = properties_dock
    window.dock_ai_edit_assistant = assistant_dock
    window.page_layouts = {}
    window._save_current_layout_to_store = MagicMock()

    DockLayoutMixin._migrate_ai_assistant_dock_layout(window)
    assistant_dock.show()
    qapp.processEvents()

    assert assistant_dock in window.tabifiedDockWidgets(properties_dock)
    shared_width = assistant_dock.width()
    for _ in range(3):
        assistant_dock.hide()
        qapp.processEvents()
        assistant_dock.show()
        qapp.processEvents()
        assert assistant_dock in window.tabifiedDockWidgets(properties_dock)
        assert assistant_dock.width() == shared_width

    window.close()


def test_legacy_timeline_layout_resizes_both_vertical_docks_once() -> None:
    """An oversized Qt splitter state must set both ends of its splitter."""
    timeline_dock = MagicMock()
    timeline_dock.isFloating.return_value = False
    timeline_dock.objectName.return_value = "dockEditTimeline"
    timeline_dock.height.return_value = 1000
    config = _LayoutConfig({
        "page_layouts": {"Edit": "legacy-layout"},
        "dock_dockEditTimeline_docked_size": "2104x281",
    })
    host = _TimelineLayoutMigrationHost(config, timeline_dock)

    DockLayoutMixin._migrate_edit_timeline_dock_height(host)

    host.resizeDocks.assert_called_once_with(
        [host.dock_program_viewer, timeline_dock],
        [1019, 281],
        Qt.Orientation.Vertical,
    )
    assert config.data[_EDIT_TIMELINE_DOCK_LAYOUT_MIGRATION_KEY] == \
        _EDIT_TIMELINE_DOCK_LAYOUT_MIGRATION_VERSION
    assert config.data["page_layouts"]["Edit"] == \
        "636f727265637465642d656469742d6c61796f7574"
    assert host.page_layouts["Edit"] == QByteArray(b"corrected-edit-layout")
    assert config.saved is True

    DockLayoutMixin._migrate_edit_timeline_dock_height(host)
    host.resizeDocks.assert_called_once()


def test_timeline_layout_migration_preserves_a_normal_or_manual_height() -> None:
    """A state that agrees with the recorded height must not be reset."""
    timeline_dock = MagicMock()
    timeline_dock.isFloating.return_value = False
    timeline_dock.objectName.return_value = "dockEditTimeline"
    timeline_dock.height.return_value = 400
    config = _LayoutConfig({"dock_dockEditTimeline_docked_size": "2104x281"})
    host = _TimelineLayoutMigrationHost(config, timeline_dock)

    DockLayoutMixin._migrate_edit_timeline_dock_height(host)

    host.resizeDocks.assert_not_called()
    assert _EDIT_TIMELINE_DOCK_LAYOUT_MIGRATION_KEY not in config.data


def test_timeline_layout_migration_replaces_a_corrupted_giant_saved_height(qapp) -> None:
    """A previous failed repair must not make the oversized state permanent."""
    window = QMainWindow()
    window.setCentralWidget(QWidget())
    program_dock = QDockWidget("Program", window)
    program_dock.setObjectName("dockProgramViewer")
    program_dock.setWidget(QWidget())
    program_dock.widget().setMinimumHeight(150)
    timeline_dock = QDockWidget("Timeline", window)
    timeline_dock.setObjectName("dockEditTimeline")
    timeline_dock.setWidget(QWidget())
    window.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, program_dock)
    window.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, timeline_dock)
    window.resize(1400, 1400)
    window.show()
    qapp.processEvents()
    window.resizeDocks([program_dock, timeline_dock], [300, 1000], Qt.Orientation.Vertical)
    qapp.processEvents()

    config = _LayoutConfig({
        "page_layouts": {"Edit": "legacy-layout"},
        "dock_dockEditTimeline_docked_size": "1240x938",
    })
    window.config_manager = config
    window.current_page = "Edit"
    window.dock_edit_timeline = timeline_dock
    window.dock_program_viewer = program_dock
    window.page_layouts = {}
    window._save_current_layout_to_store = MagicMock()

    DockLayoutMixin._migrate_edit_timeline_dock_height(window)
    qapp.processEvents()

    assert abs(timeline_dock.height() - 420) <= 5
    assert program_dock.height() > timeline_dock.height()
    assert config.data[_EDIT_TIMELINE_DOCK_LAYOUT_MIGRATION_KEY] == \
        _EDIT_TIMELINE_DOCK_LAYOUT_MIGRATION_VERSION
    window.close()


def test_unbalanced_color_tools_height_resizes_both_vertical_docks_once() -> None:
    """Restore the default height while retaining the space for the viewport."""
    grading_dock = MagicMock()
    grading_dock.isFloating.return_value = False
    grading_dock.isHidden.return_value = False
    grading_dock.objectName.return_value = "dockGrading"
    grading_dock.height.return_value = 1000
    grading_dock.tab_height.return_value = 24
    config = _LayoutConfig({
        "page_layouts": {"Color": "legacy-layout"},
        "dock_dockGrading_docked_size": "713x257",
    })
    host = _ColorGradingLayoutMigrationHost(config, grading_dock)

    DockLayoutMixin._migrate_color_grading_dock_height(host)

    host.resizeDocks.assert_called_once_with(
        [host.dock_viewport, grading_dock],
        [964, 360],
        Qt.Orientation.Vertical,
    )
    assert config.data[_COLOR_GRADING_DOCK_LAYOUT_MIGRATION_KEY] == \
        _COLOR_GRADING_DOCK_LAYOUT_MIGRATION_VERSION
    assert config.data["page_layouts"]["Color"] == \
        "636f727265637465642d656469742d6c61796f7574"


def test_color_wheel_release_without_press_is_safe(qapp) -> None:
    """Dock changes may send release first; it must never abort the process."""
    from src.features.color.ui.color_wheel import ColorWheel

    wheel = ColorWheel()
    wheel._wheel_canvas.mouseReleaseEvent(None)


def test_inspector_controls_fit_between_equal_side_margins(qapp) -> None:
    from PyQt6.QtCore import QPoint
    from PyQt6.QtWidgets import QCheckBox, QComboBox, QPushButton
    from src.features.composite.nodes.base_node import ParamDef
    from src.features.edit.core.effect_instance import ParamSchema
    from src.features.edit.core.timeline_v2 import EditTrack
    from src.shared.ui.style_sheet import C, DARK_THEME
    from src.shared.ui.widgets.chevron_combo import ChevronComboBox
    from src.shared.ui.widgets.topos_controls import ToposClickableSlider

    panel = EditPropertiesPanel()
    old_style = qapp.styleSheet()
    try:
        qapp.setStyleSheet(DARK_THEME)
        panel.set_clip(_make_mock_clip(), media_type="video")
        extra = panel._add_section("controls", "Extra controls", {}, {})
        definition = ParamDef(name="exposure_compensation", ptype="float", min_val=0.0,
                              max_val=8.0, step=0.1, default=1.0)
        track = EditTrack(track_id="audio", name="Audio 1", track_type="audio", index=0)
        for extra_row in (
            panel._create_effect_param_widget_from_pdef(definition, 1.0, "fx"),
            panel._create_effect_param_widget("gain", ParamSchema(label="Gain"), 1.0, "fx"),
            panel._build_transition_int_row(extra, "duration", 4, definition, "transition"),
            panel._build_transition_float_row(extra, "softness", 1.0, definition, "transition"),
            panel._build_track_volume_row(track, definition),
            panel._build_track_pan_row(track, definition),
        ):
            extra.content_layout().addWidget(extra_row)
        button_row = panel._create_effect_channel_group(None, "fx", {})
        extra.content_layout().addWidget(button_row)
        first_button = button_row.findChildren(QPushButton)[0]
        path_row = panel._build_info_row(
            "Path", "Project Library / Media References / Footage Review / Timeline Stills"
        )
        extra.content_layout().addWidget(path_row)
        path_value = path_row.layout().itemAt(1).widget()
        for section in panel._sections.values():
            if section._is_collapsed:
                section.toggle_collapse()
        panel.show()

        raw = panel._sections["clip:test-clip:raw"]._param_widgets["wb_gain_r"]["slider"]
        for width, height in ((280, 350), (330, 900), (400, 900), (520, 900)):
            panel.resize(width, height)
            qapp.processEvents()
            raw_x = raw.mapTo(panel, QPoint()).x()
            row = raw.parentWidget()
            label = row.layout().itemAt(0).widget()
            expected_width = row.width() - label.width() - _VALUE_FIXED_WIDTH - 12 - KeyframeIndicatorButton.BUTTON_SIZE
            assert raw.width() == expected_width
            for slider in panel.findChildren(ToposClickableSlider):
                assert (slider.mapTo(panel, QPoint()).x(), slider.width()) == (raw_x, raw.width())
                assert slider.isVisible()
            assert first_button.mapTo(panel, QPoint()).x() == raw_x
            assert C.TEXT_DIM in label.styleSheet() and "10px" in label.styleSheet()
            combos = panel.findChildren(QComboBox)
            assert len(combos) >= 5
            assert len({(combo.mapTo(panel, QPoint()).x(), combo.width()) for combo in combos}) == 1
            assert all(isinstance(combo, ChevronComboBox) for combo in combos)
            assert all(combo.mapTo(panel, QPoint()).x() == raw_x for combo in combos)
            keyframe = panel._keyframe_buttons["transform.rotation"]
            right = keyframe.mapTo(panel, QPoint(keyframe.width(), 0)).x()
            assert all(combo.mapTo(panel, QPoint(combo.width(), 0)).x() == right for combo in combos)
            for section in panel._sections.values():
                reset = section._reset_btn
                if reset.isVisible():
                    assert reset.mapTo(panel, QPoint(reset.width(), 0)).x() == right
            assert right == row.mapTo(panel, QPoint(row.width(), 0)).x()
            viewport = panel._scroll.viewport()
            viewport_left = viewport.mapTo(panel, QPoint()).x()
            row_left = row.mapTo(panel, QPoint()).x()
            assert viewport_left + viewport.width() - right == row_left - viewport_left
            assert path_value.height() >= path_value.heightForWidth(path_value.width())
            checkboxes = [box for box in panel.findChildren(QCheckBox)
                          if box.parentWidget().objectName() == "editPropertiesRow"]
            assert checkboxes and all(box.mapTo(panel, QPoint()).x() == raw_x for box in checkboxes)

        transform = panel._sections["clip:test-clip:transform"]
        for key in ("translate_x", "translate_y", "scale_x", "scale_y", "pivot_x", "pivot_y"):
            assert transform._param_widgets[key].get("slider") is None
        assert {"transform.translate_x", "transform.translate_y", "transform.scale_x",
                "transform.scale_y", "transform.rotation"} <= panel._keyframe_buttons.keys()
        for combo in panel.findChildren(QComboBox):
            assert combo.sizeAdjustPolicy() == QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
    finally:
        panel.close()
        qapp.setStyleSheet(old_style)


def test_dragvalue_stays_fixed_width_regardless_of_panel_width(qapp) -> None:
    """The value box never stretches with the dock — only the slider does."""
    from PyQt6.QtWidgets import QMainWindow, QDockWidget, QWidget
    from PyQt6.QtCore import Qt

    def dragvalue_width_at(dock_w: int) -> int:
        mw = QMainWindow()
        mw.setCentralWidget(QWidget())
        panel = EditPropertiesPanel()
        dock = QDockWidget("Properties", mw)
        dock.setWidget(panel)
        mw.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        mw.resize(1400, 800)
        mw.show()
        panel.set_clip(_make_mock_clip())
        # Native sections (incl. Transform) default to collapsed; expand the
        # Transform section so layout-dependent assertions measure the real
        # laid-out widget tree, not a hidden collapsed stub.
        _expand_transform_section(panel)
        for _ in range(5):
            qapp.processEvents()
        mw.resizeDocks([dock], [dock_w], Qt.Orientation.Horizontal)
        for _ in range(5):
            qapp.processEvents()
        section = panel._sections["clip:test-clip:transform"]
        w = section._param_widgets["rotation"]["widget"].width()
        mw.close()
        return w

    narrow = dragvalue_width_at(320)
    wide = dragvalue_width_at(520)
    assert narrow == wide == _VALUE_FIXED_WIDTH, (
        f"DragValue must stay fixed at {_VALUE_FIXED_WIDTH}px in narrow "
        f"({narrow}) and wide ({wide}) docks"
    )


def test_paired_controls_start_at_the_same_left_edge_as_single_controls(qapp) -> None:
    from PyQt6.QtCore import QPoint

    panel = EditPropertiesPanel()
    panel.set_clip(_make_mock_clip())
    _expand_transform_section(panel)
    panel.show()
    for width in (280, 350, 500):
        panel.resize(width, 500)
        qapp.processEvents()
        params = panel._sections["clip:test-clip:transform"]._param_widgets
        slider_x = params["rotation"]["slider"].mapTo(panel, QPoint()).x()
        for axis in ("translate", "scale", "pivot"):
            first = params[f"{axis}_x"]["widget"]
            second = params[f"{axis}_y"]["widget"]
            assert first.mapTo(panel, QPoint()).x() == slider_x
            assert second.mapTo(panel, QPoint(second.width(), 0)).x() <= panel.width()
    panel.close()


def test_panel_uses_default_size_hint_like_composite(qapp) -> None:
    """No hardcoded sizeHint override — let Qt negotiate width from content.

    The old QSize(100, 116) override was a dock-width guard that backfired into
    a 'fixed width' feel. It must stay removed so the panel behaves like the
    Composite page's properties panel.
    """
    assert "sizeHint" not in EditPropertiesPanel.__dict__
    assert "minimumSizeHint" not in EditPropertiesPanel.__dict__


def _make_clip_with_transform(transform) -> MagicMock:
    """Mock clip whose transform is a real TransformParams (for field access)."""
    clip = _make_mock_clip()
    clip.transform = transform
    return clip


def test_transform_section_has_pivot_controls(qapp) -> None:
    """The Transform section must expose pivot_mode + pivot_x/y rows.

    Regression for the missing center-point setting: the panel previously built
    only translate/scale/rotation and silently dropped pivot_mode/pivot_x/
    pivot_y even though TransformParams (and the renderer) fully support them.
    """
    from src.features.edit.core.clip_params import TransformParams

    panel = EditPropertiesPanel()
    panel.set_clip(_make_clip_with_transform(TransformParams()))
    _expand_transform_section(panel)

    section = panel._sections["clip:test-clip:transform"]
    widgets = section._param_widgets
    # All three pivot params must be wired to a widget.
    assert "pivot_mode" in widgets, "pivot_mode combo row missing"
    assert "pivot_x" in widgets, "pivot_x numeric row missing"
    assert "pivot_y" in widgets, "pivot_y numeric row missing"


def test_pivot_xy_disabled_in_center_mode(qapp) -> None:
    """pivot_x/y row is disabled when pivot_mode == 'center' (anchor = image centre)."""
    from src.features.edit.core.clip_params import TransformParams

    panel = EditPropertiesPanel()
    # center mode is the default.
    panel.set_clip(_make_clip_with_transform(TransformParams(pivot_mode="center")))
    _expand_transform_section(panel)

    section = panel._sections["clip:test-clip:transform"]
    px_widget = section._param_widgets["pivot_x"]["widget"]
    # The whole XY pair row is disabled (Qt propagates to the DragValue child);
    # use isEnabledTo so we observe the effective enabled state, not the
    # child's local flag.
    assert not px_widget.isEnabledTo(panel), "pivot_x must be disabled in center mode"


def test_pivot_xy_enabled_in_custom_mode(qapp) -> None:
    """pivot_x/y row is editable when pivot_mode == 'custom'."""
    from src.features.edit.core.clip_params import TransformParams

    panel = EditPropertiesPanel()
    panel.set_clip(_make_clip_with_transform(
        TransformParams(pivot_mode="custom", pivot_x=400.0, pivot_y=300.0)))
    _expand_transform_section(panel)

    section = panel._sections["clip:test-clip:transform"]
    px_widget = section._param_widgets["pivot_x"]["widget"]
    assert px_widget.isEnabledTo(panel), "pivot_x must be enabled in custom mode"


def test_pivot_mode_combo_toggles_xy_enabled(qapp) -> None:
    """Switching pivot_mode center→custom enables pivot_x/y live."""
    from src.features.edit.core.clip_params import TransformParams

    panel = EditPropertiesPanel()
    panel.set_clip(_make_clip_with_transform(TransformParams(pivot_mode="center")))
    _expand_transform_section(panel)

    section = panel._sections["clip:test-clip:transform"]
    combo = section._param_widgets["pivot_mode"]["widget"]
    px_widget = section._param_widgets["pivot_x"]["widget"]

    # custom is index 1 in _TRANSFORM_PIVOT_MODES = ["center", "custom"].
    combo.setCurrentIndex(1)
    assert px_widget.isEnabledTo(panel), "pivot_x should become editable after switching to custom"

    # Back to center (index 0).
    combo.setCurrentIndex(0)
    assert not px_widget.isEnabledTo(panel), "pivot_x should disable again in center mode"
