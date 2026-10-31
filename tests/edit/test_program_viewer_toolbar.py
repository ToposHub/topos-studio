"""Program Viewer toolbar layout and preview-quality menu tests."""

from __future__ import annotations


def test_program_toolbar_separates_menus_from_right_aligned_tools(qapp, widget_shelter) -> None:
    """Navigation menus stay left while editing and display controls stay right."""
    from src.features.edit.ui.program_viewer import ProgramViewerBar

    toolbar = ProgramViewerBar(widget_shelter)
    toolbar.resize(900, 28)
    toolbar.show()
    qapp.processEvents()

    assert toolbar.view_menu_btn.x() < toolbar.guides_menu_btn.x()
    assert toolbar.guides_menu_btn.x() < toolbar.transform_btn.x()
    assert toolbar.transform_btn.x() < toolbar.quality_menu_btn.x()
    assert toolbar.quality_menu_btn.x() < toolbar.zoom_label.x()
    assert not hasattr(toolbar, "safe90_btn")
    assert not hasattr(toolbar, "safe100_btn")


def test_program_toolbar_preview_quality_is_a_checkable_menu(qapp, widget_shelter) -> None:
    """Choosing a menu item updates the label and emits its proxy scale."""
    from src.features.edit.ui.program_viewer import ProgramViewerBar

    toolbar = ProgramViewerBar(widget_shelter)
    emitted: list[float] = []
    toolbar.preview_quality_changed.connect(emitted.append)

    half_resolution = next(
        action for action in toolbar.quality_menu.actions()
        if float(action.data()) == 0.5
    )
    half_resolution.trigger()

    assert toolbar.quality_menu_btn.text() == "1/2"
    assert emitted == [0.5]
    assert half_resolution.isChecked()
    assert sum(action.isChecked() for action in toolbar.quality_menu.actions()) == 1


def test_program_toolbar_timeline_selector_is_centered_in_toolbar_row(qapp, widget_shelter) -> None:
    """The timeline selector belongs to the toolbar and stays horizontally centered."""
    from types import SimpleNamespace
    from src.features.edit.ui.program_viewer import ProgramViewerBar

    toolbar = ProgramViewerBar(widget_shelter)
    toolbar.resize(900, 28)
    toolbar.set_timeline_choices(
        {"timeline-1": SimpleNamespace(name="Timeline 1")}, "timeline-1"
    )
    toolbar.show()
    qapp.processEvents()

    selector = toolbar._timeline_selector
    assert selector.parentWidget() is toolbar
    assert selector.geometry().center().x() == toolbar.rect().center().x()
    assert selector.y() == 0
    assert selector.width() < 132


def test_program_widget_routes_guides_to_the_viewer(qapp) -> None:
    """The Guides popup selection must update the Program monitor overlay."""
    from src.features.edit.ui.program_viewer import ProgramViewerWidget

    widget = ProgramViewerWidget()
    widget.get_toolbar().guide_changed.emit(
        "safe_area", "title", {"inset": 0.1, "secondary": 0.0})

    assert widget.get_viewer()._active_guide == {
        "type": "safe_area",
        "id": "title",
        "params": {"inset": 0.1, "secondary": 0.0},
    }

    widget.get_toolbar().guide_changed.emit("aspect_ratio", "default", {})

    assert widget.get_viewer()._active_guide is None


def test_program_widget_timeline_selector_lists_and_switches_timelines(qapp) -> None:
    """The monitor selector shows the active timeline and emits its ID."""
    from types import SimpleNamespace
    from src.features.edit.ui.program_viewer import ProgramViewerWidget

    widget = ProgramViewerWidget()
    switched: list[str] = []
    widget.timeline_switch_requested.connect(switched.append)
    widget.set_timeline_choices(
        {
            "timeline-1": SimpleNamespace(name="Timeline 1"),
            "timeline-2": SimpleNamespace(name="Timeline 2"),
        },
        "timeline-2",
    )

    selector = widget.get_toolbar()._timeline_selector
    assert not selector.isHidden()
    assert [selector.itemText(i) for i in range(selector.count())] == [
        "Timeline 1", "Timeline 2"
    ]
    assert selector.currentData() == "timeline-2"

    selector.setCurrentIndex(0)
    assert switched == ["timeline-1"]


def test_program_timeline_popup_width_fits_long_names(qapp, widget_shelter) -> None:
    """The compact closed selector still gets a wide popup for long names."""
    from types import SimpleNamespace
    from src.features.edit.ui.program_viewer import ProgramViewerBar

    toolbar = ProgramViewerBar(widget_shelter)
    toolbar.set_timeline_choices(
        {"timeline-1": SimpleNamespace(name="A very long timeline name")},
        "timeline-1",
    )
    selector = toolbar._timeline_selector

    # W07：不变量是弹窗宽度必须容纳最长名（Windows 字体度量下
    # longest+36 恰可等于紧凑控件宽度——严格 > 是平台脆断言）
    from PyQt6.QtGui import QFontMetrics
    metrics = QFontMetrics(selector.font())
    longest = max(
        metrics.horizontalAdvance(selector.itemText(i))
        for i in range(selector.count()))
    assert selector._preferred_popup_width() >= max(selector.width(),
                                                   longest + 30)


def test_guides_panel_includes_black_matte_and_export_options(qapp) -> None:
    """An aspect guide carries the requested black-matte output settings."""
    from src.features.color.ui.viewer_guides import (
        BROADCAST_FILM_GUIDES,
        ViewerGuidesPanel,
    )

    panel = ViewerGuidesPanel()
    selected: list[tuple[str, str, dict]] = []
    panel.guide_selected.connect(
        lambda guide_type, guide_id, params: selected.append(
            (guide_type, guide_id, params)))
    panel._black_fill_check.setChecked(True)
    panel._burn_in_check.setChecked(True)

    cinema_guide = next(guide for guide in BROADCAST_FILM_GUIDES if guide.id == "2.35")
    panel._on_item_clicked(cinema_guide.id, cinema_guide)

    assert selected == [
        ("aspect_ratio", "2.35", {
            "ratio": 2.35,
            "fill_black": True,
            "burn_in": True,
        })
    ]


def test_guides_panel_includes_the_common_239_cinema_ratio(qapp) -> None:
    """2.39:1 is available as a dedicated cinema guide, not an approximation."""
    from src.features.color.ui.viewer_guides import BROADCAST_FILM_GUIDES

    guide = next(guide for guide in BROADCAST_FILM_GUIDES if guide.id == "2.39")

    assert guide.label == "2.39"
    assert guide.value == 2.39


def test_black_matte_checkbox_refreshes_the_active_guide_immediately(qapp) -> None:
    """Changing a matte option must not require selecting the ratio again."""
    from src.features.color.ui.viewer_guides import (
        BROADCAST_FILM_GUIDES,
        ViewerGuidesPanel,
    )

    panel = ViewerGuidesPanel()
    selected: list[tuple[str, str, dict]] = []
    panel.guide_selected.connect(
        lambda guide_type, guide_id, params: selected.append(
            (guide_type, guide_id, params)))
    cinema_guide = next(guide for guide in BROADCAST_FILM_GUIDES if guide.id == "2.39")
    panel._on_item_clicked(cinema_guide.id, cinema_guide)
    selected.clear()

    panel._black_fill_check.setChecked(True)

    assert selected == [
        ("aspect_ratio", "2.39", {
            "ratio": 2.39,
            "fill_black": True,
            "burn_in": False,
        })
    ]


def test_black_matte_covers_fractional_display_rect_edges(qapp) -> None:
    """Opaque mattes must not leave anti-aliased source pixels at their edge."""
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QColor, QImage, QPainter
    from src.features.edit.ui.program_viewer import ProgramViewer

    viewer = ProgramViewer()
    canvas = QImage(120, 100, QImage.Format.Format_ARGB32)
    canvas.fill(QColor("#ffffff"))
    image_rect = QRectF(10.5, 10.5, 100.0, 75.0)

    painter = QPainter(canvas)
    painter.setClipRect(image_rect)
    viewer._paint_aspect_ratio_guide(painter, image_rect, 2.35, True)
    painter.end()

    assert canvas.pixelColor(10, 10) == QColor("#000000")
    assert canvas.pixelColor(10, 26) == QColor("#000000")
    assert canvas.pixelColor(10, 27) == QColor("#ffffff")
    assert canvas.pixelColor(10, 70) == QColor("#000000")
    assert canvas.pixelColor(9, 27) == QColor("#ffffff")
    assert canvas.pixelColor(111, 27) == QColor("#ffffff")


def test_vertical_matte_covers_the_fractional_top_edge(qapp) -> None:
    """Side mattes extend through the top edge when no top bar exists."""
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QColor, QImage, QPainter
    from src.features.edit.ui.program_viewer import ProgramViewer

    viewer = ProgramViewer()
    canvas = QImage(120, 100, QImage.Format.Format_ARGB32)
    canvas.fill(QColor("#ffffff"))
    image_rect = QRectF(10.5, 10.5, 100.0, 75.0)

    painter = QPainter(canvas)
    painter.setClipRect(image_rect)
    viewer._paint_aspect_ratio_guide(painter, image_rect, 0.5, True)
    painter.end()

    assert canvas.pixelColor(10, 10) == QColor("#000000")
    assert canvas.pixelColor(41, 10) == QColor("#000000")
    assert canvas.pixelColor(42, 10) == QColor("#ffffff")
    assert canvas.pixelColor(10, 85) == QColor("#000000")
