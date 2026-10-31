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
        self.dock_viewport = MagicMock()
        self.dock_viewport.isFloating.return_value = False
        self.dock_viewport.height.return_value = 300
        self.dock_viewport.minimumHeight.return_value = 150

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

    assert timeline_dock.height() <= 320
    assert program_dock.height() > timeline_dock.height()
    assert config.data[_EDIT_TIMELINE_DOCK_LAYOUT_MIGRATION_KEY] == \
        _EDIT_TIMELINE_DOCK_LAYOUT_MIGRATION_VERSION
    window.close()


def test_oversized_color_wheel_layout_resizes_both_vertical_docks_once() -> None:
    """Color Wheel must not retain a stale bottom-splitter proportion."""
    grading_dock = MagicMock()
    grading_dock.isFloating.return_value = False
    grading_dock.objectName.return_value = "dockGrading"
    grading_dock.height.return_value = 1000
    config = _LayoutConfig({
        "page_layouts": {"Color": "legacy-layout"},
        "dock_dockGrading_docked_size": "713x257",
    })
    host = _ColorGradingLayoutMigrationHost(config, grading_dock)

    DockLayoutMixin._migrate_color_grading_dock_height(host)

    host.resizeDocks.assert_called_once_with(
        [host.dock_viewport, grading_dock],
        [1043, 257],
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


def test_numeric_sliders_always_visible_between_label_and_dragvalue(qapp) -> None:
    """Numeric rows must be ``[label][slider stretch][DragValue fixed]``.

    Sliders stay visible at every panel width — they stretch to fill the row's
    middle so the DragValue pins to the right edge.  Hiding them (an earlier
    attempt) collapsed every row to ``[label][DragValue] + whitespace``, which
    is the "everything left-aligned" look the Composite page does not have.
    """
    from PyQt6.QtWidgets import QMainWindow, QDockWidget, QWidget
    from PyQt6.QtCore import Qt

    mw = QMainWindow()
    mw.setCentralWidget(QWidget())
    panel = EditPropertiesPanel()
    dock = QDockWidget("Properties", mw)
    dock.setWidget(panel)
    mw.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
    mw.resize(1200, 800)
    mw.show()
    panel.set_clip(_make_mock_clip())
    _expand_transform_section(panel)
    for _ in range(5):
        qapp.processEvents()

    transform = panel._sections["clip:test-clip:transform"]
    assert transform._slidable_sliders, "Transform section should own sliders"

    # Every numeric-row slider must be visible regardless of width.
    assert all(s.isVisible() for s in transform._slidable_sliders)

    # Row 0 is now the Translate X/Y pair (composite TransformNode parity):
    # [label] [x-slider stretch=1] [x] [x-DragValue] [y-slider] [y] [y-DragValue]
    content = transform._content_widget
    row0 = content.layout().itemAt(0).widget().layout()
    types = [type(row0.itemAt(j).widget()).__name__ for j in range(row0.count())
             if row0.itemAt(j) and row0.itemAt(j).widget()]
    # The first slider (x axis) sits right after the label and takes the slack.
    assert types[0] == "QLabel", "row must start with the param label"
    assert types[1] == "ToposClickableSlider", "x-axis slider must follow the label"
    assert "DragValue" in types, "row must contain DragValue boxes"
    assert row0.stretch(1) == 1, "x-slider must take the slack via stretch=1"


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
        content = panel._sections["clip:test-clip:transform"]._content_widget
        # Row 0 is the Translate X/Y pair; find its first DragValue (x axis).
        row0 = content.layout().itemAt(0).widget().layout()
        drag = next(row0.itemAt(j).widget() for j in range(row0.count())
                    if row0.itemAt(j) and row0.itemAt(j).widget()
                    and type(row0.itemAt(j).widget()).__name__ == "DragValue")
        w = drag.width()
        mw.close()
        return w

    narrow = dragvalue_width_at(320)
    wide = dragvalue_width_at(520)
    assert narrow == wide == _VALUE_FIXED_WIDTH, (
        f"DragValue must stay fixed at {_VALUE_FIXED_WIDTH}px in narrow "
        f"({narrow}) and wide ({wide}) docks"
    )


def test_no_right_gap_dragvalue_reaches_panel_edge(qapp) -> None:
    """DragValue's right edge must align with the row's right edge (no gap).

    Regression for the 'large empty area on the right' bug: an earlier version
    pinned the scroll area's inner content widget to ``QSizePolicy.Maximum``
    horizontally, which froze the content at its natural sizeHint and left the
    section narrower than the dock — so the value box never reached the right
    edge. With a resizable content widget (composite parity) the gap is 0.
    """
    from PyQt6.QtWidgets import QMainWindow, QDockWidget, QWidget
    from PyQt6.QtCore import Qt

    for dock_w in (350, 500):
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

        content = panel._sections["clip:test-clip:transform"]._content_widget
        row0 = content.layout().itemAt(0).widget()
        rl = row0.layout()
        # Row 0 is the Translate X/Y pair; the LAST DragValue (y axis) sits at
        # the row's right edge (the pair fills the row like composite).
        drags = [rl.itemAt(j).widget() for j in range(rl.count())
                 if rl.itemAt(j) and rl.itemAt(j).widget()
                 and type(rl.itemAt(j).widget()).__name__ == "DragValue"]
        assert drags, "Translate row must contain DragValue boxes"
        # G16（第六轮审计）：Translate/Scale 每轴现在带 ◆ 关键帧按钮——
        # 行尾可能是 y 轴 ◆ 而不是 DragValue。"无右侧死空白"的语义是
        # 行内最后一个控件到达右缘（DragValue 或 KeyframeIndicatorButton）。
        widgets = [rl.itemAt(j).widget() for j in range(rl.count())
                   if rl.itemAt(j) and rl.itemAt(j).widget()]
        assert widgets, "Translate row must have widgets"
        last = widgets[-1]
        assert type(last).__name__ in ("DragValue", "KeyframeIndicatorButton"), (
            f"row-right widget should be a DragValue or its ◆ button, "
            f"got {type(last).__name__}"
        )
        gap = row0.width() - (last.pos().x() + last.width())
        assert gap <= 2, (
            f"Row-right control must reach the right edge at dock_w={dock_w}, "
            f"but had a {gap}px gap (row={row0.width()}, last_right="
            f"{last.pos().x() + last.width()})"
        )
        mw.close()


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
