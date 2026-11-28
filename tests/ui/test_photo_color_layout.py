"""Photo Color factory tabs, responsive grouping and legacy layouts."""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDockWidget, QMainWindow, QTabBar, QTabWidget, QWidget

from src.app.controllers.dock_layout_controller import DockLayoutMixin
from src.features.color.ui.panel_chrome import COLOR_TOOL_DOCK_MAX_HEIGHT, ColorToolDockWidget
from src.shared.i18n.i18n import I18N


class _Config(dict):
    def set(self, key: str, value: object, *, auto_save: bool = True) -> None:
        self[key] = value


class _Window(QMainWindow, DockLayoutMixin):
    _active_work_mode = "image"
    current_page = "Color"

    def __init__(self) -> None:
        super().__init__()
        self.setDockNestingEnabled(True)
        self.setTabPosition(Qt.DockWidgetArea.AllDockWidgetAreas, QTabWidget.TabPosition.North)
        self.setCentralWidget(QWidget())
        self.centralWidget().hide()
        self.config_manager = _Config()
        self.all_docks = []
        for name in ("presets", "lut", "plugin", "viewport", "layers", "info", "crop", "raw",
                     "grading", "base_grade", "chromogen", "denoise", "deblur", "curves",
                     "warper", "mixer", "qualifier", "masks", "effects", "analysis", "grain"):
            dock_type = QDockWidget if name in ("presets", "lut", "plugin", "viewport", "layers", "info") else ColorToolDockWidget
            dock = dock_type(name, self)
            dock.setWidget(QWidget())
            if name == "viewport":
                self.viewport_container = dock.widget()
            dock.setObjectName(name)
            self.all_docks.append(dock)
            setattr(self, f"dock_{name}", dock)

    def _photo_color_available_width(self, width: int) -> int:
        return width

    def _perform_update_all_titlebars(self) -> None:
        self._style_dock_tab_bars()


def test_photo_color_defaults_and_adapts(qtbot) -> None:
    host = _Window()
    qtbot.addWidget(host)
    host.resize(1720, 900)
    status = host.statusBar()
    host.apply_default_layout()
    host.show()
    qtbot.waitUntil(lambda: not host.dock_analysis.visibleRegion().isEmpty())
    top = Qt.DockWidgetArea.TopDockWidgetArea
    assert all(host.dockWidgetArea(dock) == top for dock in host.all_docks)
    assert set(host.tabifiedDockWidgets(host.dock_presets)) == {host.dock_lut, host.dock_plugin}
    assert host.tabifiedDockWidgets(host.dock_layers) == []
    assert host.tabifiedDockWidgets(host.dock_info) == [host.dock_crop]
    assert host.dock_info.geometry().bottom() < host.dock_layers.geometry().top()
    assert host.dock_info.x() == host.dock_layers.x()
    host._style_dock_tab_bars(only_changed=True)
    details_tabs = next(bar for bar in host.findChildren(QTabBar)
                        if bar.isVisible() and {bar.tabText(i) for i in range(bar.count())} == {"info", "crop"})
    assert not details_tabs.property("_topos_dock_tabs_compact")
    assert host._builtin_page_layouts() == [(I18N.tr("workspace.horizontal_color", "Landscape Color"), "factory")]
    assert {host.dock_raw, host.dock_base_grade, host.dock_chromogen} <= set(host.tabifiedDockWidgets(host.dock_grading))
    assert host.dock_grain in host.tabifiedDockWidgets(host.dock_analysis)
    assert host.dock_crop.maximumHeight() > COLOR_TOOL_DOCK_MAX_HEIGHT
    assert host.dock_grading.maximumHeight() <= COLOR_TOOL_DOCK_MAX_HEIGHT
    assert status.y() - host.dock_grading.geometry().bottom() <= 2
    host.dock_crop.hide()

    host.resize(1160, 680)
    host._fit_photo_color_layout(1160)
    qtbot.waitUntil(lambda: not host.dock_layers.visibleRegion().isEmpty())
    assert {host.dock_layers, host.dock_info} <= set(host.tabifiedDockWidgets(host.dock_analysis))
    assert {host.dock_deblur, host.dock_grain} <= set(host.tabifiedDockWidgets(host.dock_presets))
    assert host.dock_curves in host.tabifiedDockWidgets(host.dock_grading)
    assert host.dock_crop.isHidden()
    assert host.dock_deblur.maximumHeight() > COLOR_TOOL_DOCK_MAX_HEIGHT
    assert host.dock_grain.maximumHeight() > COLOR_TOOL_DOCK_MAX_HEIGHT
    assert host.dock_crop.maximumHeight() <= COLOR_TOOL_DOCK_MAX_HEIGHT
    assert host.dock_presets.minimumWidth() > sum(
        host.dock_presets.fontMetrics().horizontalAdvance(dock.windowTitle())
        for dock in (host.dock_presets, *host.tabifiedDockWidgets(host.dock_presets))
    )

    host.resize(1160, 900)
    host._fit_photo_color_layout(1160)
    qtbot.waitUntil(lambda: host.dock_info.x() == host.dock_layers.x()
                    and host.dock_info.geometry().bottom() < host.dock_layers.y())
    assert host.dock_info in host.tabifiedDockWidgets(host.dock_analysis)
    assert host.tabifiedDockWidgets(host.dock_layers) == []

    host.resize(1720, 900)
    host._fit_photo_color_layout(1720)
    host.layout().activate()
    host._fit_photo_color_layout(1720)
    assert host.dock_info not in host.tabifiedDockWidgets(host.dock_layers)
    assert host.dock_deblur in host.tabifiedDockWidgets(host.dock_grading)
    assert host.dock_grain in host.tabifiedDockWidgets(host.dock_analysis)
    assert host.dock_curves not in host.tabifiedDockWidgets(host.dock_grading)
    assert host.dock_crop.isHidden()

    host.config_manager.set(host._active_page_layout_key("Color"), {"name": "Saved layout"})
    host._fit_photo_color_layout(1160)
    assert host.dock_info not in host.tabifiedDockWidgets(host.dock_layers)
    assert host.dock_curves not in host.tabifiedDockWidgets(host.dock_grading)


def test_photo_color_migrates_legacy_factory_layout(qtbot) -> None:
    host = _Window()
    qtbot.addWidget(host)
    host.resize(1600, 900)
    resources, right, primary, secondary, analysis = host._photo_color_groups()
    left_area, right_area = Qt.DockWidgetArea.LeftDockWidgetArea, Qt.DockWidgetArea.RightDockWidgetArea
    top, bottom = Qt.DockWidgetArea.TopDockWidgetArea, Qt.DockWidgetArea.BottomDockWidgetArea
    for area, group in ((left_area, resources), (top, (host.dock_viewport,)), (right_area, right[:1]),
                        (right_area, right[1:]), (bottom, primary), (bottom, secondary), (bottom, analysis)):
        for dock in group:
            host.addDockWidget(area, dock)
            dock.show()
            if dock is not group[0]:
                host.tabifyDockWidget(group[0], dock)
    host.dock_crop.hide()
    host._migrate_photo_color_layout()
    assert host.dockWidgetArea(host.dock_presets) == top
    assert host.dockWidgetArea(host.dock_layers) == top
    assert not host.tabifiedDockWidgets(host.dock_layers)
    assert not host.dock_info.isHidden()
    assert host.dock_crop.isHidden()
