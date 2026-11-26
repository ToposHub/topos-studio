"""The Chromogen panel writes ordered, transactional look patches."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtWidgets import QFileDialog, QGraphicsView, QLabel, QScrollArea, QSplitter, QToolButton, QWidget

from src.features.color.core.chromogen import (
    CHROMOGEN_KEY, CHROMOGEN_MAX_STAGES, CHROMOGEN_PRESETS, STAGE_SPECS, apply_chromogen_linear, chromogen_hue,
    new_chromogen_stage,
)
from src.features.color.ui.chromogen_panel import ChromogenPanel
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import DARK_THEME
from src.shared.ui.widgets.chevron_combo import ChevronComboBox

if TYPE_CHECKING:
    from pytestqt.qtbot import QtBot


def _show_panel(qtbot: QtBot) -> ChromogenPanel:
    panel = ChromogenPanel()
    qtbot.addWidget(panel)
    panel.resize(720, 340)
    panel.show()
    return panel


def _stages(panel: ChromogenPanel) -> list[dict]:
    state = panel.get_params()[CHROMOGEN_KEY]
    return state["stages"] if state is not None else []


def _state(*kinds: str) -> dict:
    return {"version": 1, "enabled": True, "amount": 1.0,
            "stages": [new_chromogen_stage(kind) for kind in kinds]}


def test_insert_move_duplicate_and_delete_are_one_gesture_each(qtbot: QtBot) -> None:
    panel = _show_panel(qtbot)
    events = []
    panel.interaction_started.connect(lambda: events.append("start"))
    panel.changed.connect(lambda _: events.append("patch"))
    panel.interaction_ended.connect(lambda: events.append("end"))

    panel.add_stage("colour_saturation")
    panel.add_stage("sector_brightness")
    assert [stage["kind"] for stage in _stages(panel)] == ["colour_saturation", "sector_brightness"]
    selected = _stages(panel)[1]
    panel.up_button.click()
    assert _stages(panel)[0] == selected
    panel.duplicate_button.click()
    duplicate = _stages(panel)[1]
    assert duplicate["id"] != selected["id"]
    assert duplicate["params"] == selected["params"]
    panel.delete_button.click()
    assert _stages(panel)[0] == selected
    assert len(_stages(panel)) == 2
    assert events == ["start", "patch", "end"] * 5


def test_reorder_look_bypass_and_stage_bypass_are_independent(qtbot: QtBot) -> None:
    panel = _show_panel(qtbot)
    changes = []
    panel.changed.connect(changes.append)
    state = _state("contrast_boost", "highlight_bleach", "neutral_tint")
    ids = [stage["id"] for stage in state["stages"]]
    panel.set_params({CHROMOGEN_KEY: state})
    assert panel.stage_list.ids() == ids
    assert not changes

    panel.stage_list.order_changed.emit(ids[::-1])
    assert panel.stage_list.ids() == ids[::-1]
    panel.stage_list.item(0).setCheckState(Qt.CheckState.Unchecked)
    panel.bypass_button.setChecked(True)
    look = panel.get_params()[CHROMOGEN_KEY]
    assert not look["enabled"]
    assert [stage["enabled"] for stage in look["stages"]] == [False, True, True]
    panel.bypass_button.setChecked(False)
    assert panel.get_params()[CHROMOGEN_KEY]["enabled"]
    assert not _stages(panel)[0]["enabled"]
    assert changes[-1][CHROMOGEN_KEY]["stages"][0]["id"] == ids[-1]


def test_active_drag_discarded_on_hydration_with_no_stale_write(qtbot: QtBot) -> None:
    panel = _show_panel(qtbot)
    panel.set_params({CHROMOGEN_KEY: _state("colour_saturation")})
    events = []
    panel.interaction_started.connect(lambda: events.append("start"))
    panel.changed.connect(lambda _: events.append("patch"))
    panel.interaction_ended.connect(lambda: events.append("end"))
    panel._publish_timer.setInterval(1000)
    slider = panel._amount.slider
    press = slider._handle_rect().center()
    move = press - QPoint(19, 0)
    qtbot.mousePress(slider, Qt.MouseButton.LeftButton, pos=press)
    qtbot.mouseMove(slider, pos=move)
    assert panel.get_params()[CHROMOGEN_KEY]["amount"] < 1
    assert events == ["start"]

    next_state = _state("highlight_bleach")
    next_state["amount"] = 0.35
    panel.set_params({CHROMOGEN_KEY: next_state})
    qtbot.mouseMove(slider, pos=move - QPoint(17, 0))
    qtbot.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=move - QPoint(17, 0))
    panel.flush_pending_changes()
    assert panel.get_params()[CHROMOGEN_KEY]["amount"] == pytest.approx(0.35)
    assert events == ["start", "end"]


def test_control_patch_and_reset_keep_the_selected_stage_id(qtbot: QtBot) -> None:
    panel = _show_panel(qtbot)
    panel.set_params({CHROMOGEN_KEY: _state("colour_saturation")})
    stage = _stages(panel)[0]
    spec = STAGE_SPECS[stage["kind"]].controls[0]
    changes = []
    panel.changed.connect(changes.append)
    value = spec.maximum if spec.default != spec.maximum else spec.minimum
    panel._rows[spec.key].spin.setValue(value)
    assert changes[-1][CHROMOGEN_KEY]["stages"][0]["params"][spec.key] == pytest.approx(value)
    panel.reset_stage_button.click()
    reset = _stages(panel)[0]
    assert reset["id"] == stage["id"]
    assert reset["params"][spec.key] == pytest.approx(spec.default)


def test_preset_round_trip_and_append_limit(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    panel = _show_panel(qtbot)
    source = _state("highlight_bleach", "neutral_tint")
    panel.set_params({CHROMOGEN_KEY: source})

    path = tmp_path / "Chromogen.json"
    suggested_names = []

    def accept(dialog: QFileDialog) -> int:
        assert dialog.defaultSuffix() == "json"
        return 1

    monkeypatch.setattr(QFileDialog, "selectFile", lambda _, name: suggested_names.append(name))
    monkeypatch.setattr(QFileDialog, "exec", accept)
    monkeypatch.setattr(QFileDialog, "selectedFiles", lambda _: [str(path)])
    panel._export_dialog()
    assert suggested_names == ["Look Dev.json"]
    assert [s["kind"] for s in json.loads(path.read_text())["stages"]] == ["highlight_bleach", "neutral_tint"]

    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_: (str(path), ""))
    panel._import_dialog(append=True)
    stages = _stages(panel)
    assert [stage["kind"] for stage in stages] == ["highlight_bleach", "neutral_tint"] * 2
    assert len({stage["id"] for stage in stages}) == 4
    panel._import_dialog(append=False)
    assert len(_stages(panel)) == 2
    assert not (set(panel.stage_list.ids()) & {stage["id"] for stage in source["stages"]})

    full = _state(*["colour_saturation"] * CHROMOGEN_MAX_STAGES)
    panel.set_params({CHROMOGEN_KEY: full})
    assert not panel.add_button.isEnabled()
    assert not panel.apply_preset(source, append=True)
    assert len(_stages(panel)) == CHROMOGEN_MAX_STAGES


def test_quick_preset_choice_replaces_the_look_and_reflects_hydration(qtbot: QtBot) -> None:
    panel = _show_panel(qtbot)
    panel.set_params({CHROMOGEN_KEY: _state("sector_brightness")})
    changes = []
    panel.changed.connect(changes.append)
    assert panel.preset.currentIndex() == -1

    index = panel.preset.findData("Warm Silver")
    panel.preset.activated.emit(index)
    assert len(changes) == 1
    assert panel.preset.currentData() == "Warm Silver"
    assert [stage["kind"] for stage in _stages(panel)] == [
        stage["kind"] for stage in CHROMOGEN_PRESETS["Warm Silver"]["stages"]
    ]
    panel.preset.activated.emit(index)
    assert len(changes) == 1

    panel._amount.spin.setValue(0.5)
    assert panel.preset.currentIndex() == -1
    panel.set_params(changes[0])
    assert panel.preset.currentData() == "Warm Silver"
    assert len(changes) == 2


@pytest.mark.parametrize(("locale", "title", "export_tooltip", "scope"), (
    ("en_US", "Look Dev", "Export Look Dev LUT", "Look Dev Scope"),
    ("zh_CN", "风格设计", "导出风格设计 LUT", "风格设计图形"),
))
def test_look_dev_display_name(
    qtbot: QtBot, locale: str, title: str, export_tooltip: str, scope: str,
) -> None:
    original_locale = I18N.get_current_locale()
    try:
        assert I18N.load_locale(locale)
        panel = _show_panel(qtbot)
        assert I18N.tr("chromogen.title") == title
        assert any(label.text() == title for label in panel.findChildren(QLabel))
        assert any(button.toolTip() == export_tooltip for button in panel.findChildren(QToolButton))
        assert panel.scope.accessibleName() == scope
    finally:
        I18N.load_locale(original_locale)


def test_unknown_version_remains_intact_until_a_supported_look_is_loaded(qtbot: QtBot) -> None:
    panel = _show_panel(qtbot)
    incoming = {"version": 10, "foreign": [42], "stages": []}
    changes = []
    panel.changed.connect(changes.append)
    panel.set_params({CHROMOGEN_KEY: incoming})
    assert not panel.add_button.isEnabled()
    assert not panel.preset.isEnabled()
    assert panel.get_params() == {CHROMOGEN_KEY: incoming}
    panel.add_stage("colour_saturation")
    assert not changes
    panel.set_params({CHROMOGEN_KEY: None})
    assert panel.add_button.isEnabled()
    assert panel.preset.isEnabled()
    assert not changes


def test_scope_projections_and_picked_pixel(qtbot: QtBot) -> None:
    panel = _show_panel(qtbot)
    source = _state("sector_brightness")
    panel.set_params({CHROMOGEN_KEY: source})
    picked_color = (0.4, 0.19, 0.08)
    panel.set_picked_color(picked_color)
    assert panel.scope._picked_color == picked_color
    assert panel.scope._picked_after.shape == (3,)

    for mode in ("radial", "eab", "rgb", "response"):
        panel.projection.setCurrentIndex(panel.projection.findData(mode))
        assert panel.scope._projection == mode
        assert not panel.scope.grab().isNull()

    key, x_axis, _, values = panel.scope._response
    assert key == "response_ev" and x_axis == "hue_axis"
    assert values.shape == (73, 3)
    assert np.isfinite(values).all()
    panel.gamut.setCurrentIndex(panel.gamut.findData("rec2020"))
    assert panel.scope._gamut == "rec2020"

    panel.projection.setCurrentIndex(panel.projection.findData("radial"))
    center = panel.scope.rect().center()
    qtbot.mousePress(panel.scope, Qt.MouseButton.LeftButton, pos=center)
    qtbot.mouseRelease(panel.scope, Qt.MouseButton.LeftButton, pos=center + QPoint(12, 8))
    assert panel.scope._pan.x() == pytest.approx(12)
    panel.projection.setCurrentIndex(panel.projection.findData("rgb"))
    before = panel.scope._yaw
    qtbot.mousePress(panel.scope, Qt.MouseButton.LeftButton, pos=center)
    qtbot.mouseRelease(panel.scope, Qt.MouseButton.LeftButton, pos=center + QPoint(12, 8))
    assert panel.scope._yaw != before


def test_aligned_header_and_footer_fit_narrow_layout(qtbot: QtBot) -> None:
    panel = _show_panel(qtbot)
    panel.set_params({CHROMOGEN_KEY: _state("colour_crosstalk")})
    assert all(isinstance(combo, ChevronComboBox) for combo in (panel.preset, panel.projection, panel.gamut))
    splitter = panel.findChild(QSplitter)
    assert splitter.handleWidth() == 1
    amount = panel._amount
    foot_controls = (panel.up_button, panel.duplicate_button, panel.delete_button, panel.projection, panel.gamut, amount)

    def top_in_panel(widget: QWidget) -> int:
        return widget.mapTo(panel, QPoint()).y()

    for width in (720, 420):
        panel.resize(width, 218)
        qtbot.waitUntil(lambda: splitter.width() == panel.width() and splitter.height() == 188)
        centers = [control.mapTo(panel, control.rect().center()).y() for control in (
            splitter.widget(0).findChild(QLabel), panel.preset, panel.stage_title,
        )]
        assert max(centers) - min(centers) <= 1
        assert len({(top_in_panel(widget), widget.height()) for widget in foot_controls}) == 1
        for widget in foot_controls:
            assert widget.parentWidget().rect().contains(widget.geometry())
        seams = [splitter.widget(index).findChildren(
            QWidget, "colorParameterDivider", Qt.FindChildOption.FindDirectChildrenOnly,
        ) for index in range(3)]
        assert all(len(lines) == 2 for lines in seams)
        assert all(len({top_in_panel(divider) for divider in band}) == 1 for band in zip(*seams))

    scroller = panel.findChild(QScrollArea)
    assert top_in_panel(scroller) + scroller.height() < top_in_panel(amount)
    assert amount.slider.isVisible()
    assert all(row.slider.isHidden() for row in panel._rows.values())
    qtbot.waitUntil(lambda: scroller.verticalScrollBar().maximum() > 0)
    footer_position = amount.pos()
    scroller.verticalScrollBar().setValue(scroller.verticalScrollBar().maximum())
    assert amount.pos() == footer_position


@pytest.mark.parametrize("locale", ("en_US", "zh_CN"))
def test_parameter_names_fit_the_compact_column(qtbot: QtBot, locale: str) -> None:
    original_locale = I18N.get_current_locale()
    try:
        assert I18N.load_locale(locale)
        panel = _show_panel(qtbot)
        panel.setStyleSheet(DARK_THEME)
        panel.set_params({CHROMOGEN_KEY: _state(*STAGE_SPECS)})
        panel.resize(420, 210)
        column = panel.findChild(QSplitter).widget(2)
        viewport = column.findChild(QScrollArea).viewport()
        width = column.width()

        def right_edge(widget: QWidget) -> int:
            return widget.mapTo(panel, widget.rect().topRight()).x()

        for index in range(len(STAGE_SPECS)):
            panel.stage_list.setCurrentRow(index)
            qtbot.waitUntil(lambda: all(
                row.spin.x() > row.label.geometry().right()
                and viewport.rect().contains(row.spin.mapTo(viewport, row.spin.rect().topRight()).x(), 0)
                and right_edge(row.spin) == right_edge(panel._amount.spin)
                for row in panel._rows.values()
            ))
            for row in panel._rows.values():
                label = row.label
                assert label.width() >= label.fontMetrics().horizontalAdvance(label.text())
                assert label.alignment() & Qt.AlignmentFlag.AlignRight
                assert row.spin.x() - label.geometry().right() == row.layout().spacing() + 1
            assert column.width() == width
    finally:
        I18N.load_locale(original_locale)


def test_stage_sliders_appear_when_parameter_column_is_widened(qtbot: QtBot) -> None:
    panel = _show_panel(qtbot)
    panel.resize(1080, 340)
    panel.set_params({CHROMOGEN_KEY: _state("contrast_boost")})
    splitter = panel.findChild(QSplitter)
    rows = list(panel._rows.values())
    assert all(row.slider.isHidden() for row in rows)
    assert panel._amount.slider.isVisible()

    panel.resize(1560, 340)
    qtbot.waitUntil(lambda: all(row.slider.isVisible() for row in rows))
    boost = panel._rows["contrast"]
    boost.slider.setValue(round(0.4 * boost.scale))
    panel.resize(1080, 340)
    qtbot.waitUntil(lambda: all(row.slider.isHidden() for row in rows))
    assert boost.spin.value() == pytest.approx(0.4)

    splitter.setSizes([150, 530, 370])
    qtbot.waitUntil(lambda: all(row.slider.isVisible() for row in rows))
    splitter.setSizes([150, 730, 160])
    qtbot.waitUntil(lambda: all(row.slider.isHidden() for row in rows))
    assert panel._amount.slider.isVisible()


def test_picker_cancels_when_panel_is_hidden(qtbot: QtBot) -> None:
    panel = _show_panel(qtbot)
    requests = []
    panel.pick_requested.connect(requests.append)
    panel.pick_button.setChecked(True)
    panel.hide()
    assert requests == [True, False]


def test_sector_picker_authors_only_center_hue(qtbot: QtBot) -> None:
    panel = _show_panel(qtbot)
    panel.set_params({CHROMOGEN_KEY: _state("sector_saturation", "contrast_boost")})
    initial = _stages(panel)
    spec = next(control for control in STAGE_SPECS["sector_saturation"].controls if control.key == "hue")
    hue = 160.0 if spec.default != 160.0 else 280.0
    requests = []
    patches = []
    panel.pick_hue_requested.connect(requests.append)
    panel.changed.connect(patches.append)
    panel.hue_pick_button.setChecked(True)
    panel.set_picked_hue(hue)
    assert len(patches) == 1
    expected_hue = (hue - spec.minimum) % 360.0 + spec.minimum
    assert _stages(panel)[0]["params"] == {**initial[0]["params"], "hue": expected_hue}
    assert _stages(panel)[1] == initial[1]
    panel.stage_list.setCurrentRow(1)
    assert panel.hue_pick_button.isHidden()
    assert requests == [True, False]


def test_stage_renaming_preserves_stage_kind_and_parameters(qtbot: QtBot) -> None:
    panel = _show_panel(qtbot)
    panel.set_params({CHROMOGEN_KEY: _state("neutral_tint")})
    stage = _stages(panel)[0]
    patches = []
    panel.changed.connect(patches.append)
    panel.stage_list.currentItem().setText("Dusk tint")
    assert _stages(panel)[0] == {**stage, "name": "Dusk tint"}
    assert len(patches) == 1
    assert panel.stage_list.item(0).text() == "Dusk tint"
    assert panel.stage_title.text() == I18N.tr("chromogen.stage.neutral_tint", STAGE_SPECS["neutral_tint"].label)


def test_viewer_hue_pick_uses_the_input_of_the_selected_stage(qtbot: QtBot) -> None:
    panel = _show_panel(qtbot)
    source = _state("neutral_tint", "sector_saturation", "contrast_boost")
    source["stages"][0]["params"].update(amount=1.5, hue=70.0)
    panel.set_params({CHROMOGEN_KEY: source})
    panel.stage_list.setCurrentRow(1)
    panel.hue_pick_button.setChecked(True)
    rgb = (0.14, 0.19, 0.20)
    prefix = {**source, "stages": source["stages"][:1]}
    expected = chromogen_hue(apply_chromogen_linear(np.array([rgb], dtype=np.float32), prefix))[0]
    assert abs(float(expected - chromogen_hue(np.array([rgb], dtype=np.float32))[0])) > 0.5
    panel.set_picked_color(rgb)
    assert _stages(panel)[1]["params"]["hue"] == pytest.approx(float(expected))
    assert _stages(panel)[0] == source["stages"][0]
    assert _stages(panel)[2] == source["stages"][2]


def test_switching_viewer_pick_modes_keeps_new_mode_and_drops_pending_click(qtbot: QtBot) -> None:
    from src.app.controllers.chromogen_picker import ChromogenPicker

    panel = _show_panel(qtbot)
    panel.add_stage("sector_saturation")

    class Viewer(QGraphicsView):
        def accepts_warper_picker_at(self, position: QPointF) -> bool:
            return True

    viewer = Viewer()
    qtbot.addWidget(viewer)
    viewer.setSceneRect(0, 0, 100, 100)
    viewer.resize(200, 160)
    viewer.show()
    picker = ChromogenPicker(viewer, panel, refresh=lambda: None, available=lambda: True,
                             disarm_peers=lambda: None)
    center = viewer.mapFromScene(QPointF(50, 50))
    panel.pick_button.setChecked(True)
    key = picker.request("node", ("source",), (1,))
    qtbot.mouseClick(viewer.viewport(), Qt.MouseButton.LeftButton, pos=center)
    assert picker._click is not None
    panel.hue_pick_button.setChecked(True)
    assert picker._click is None
    assert panel.hue_pick_button.isChecked()
    assert not panel.pick_button.isChecked()
    pixel = np.array([[[0.6, 0.1, 0.3]]], dtype=np.float32)
    picker.accept((key, (0, 0, 100, 100), False, pixel))
    assert _stages(panel)[0]["params"]["hue"] == 0.0
    qtbot.mouseClick(viewer.viewport(), Qt.MouseButton.LeftButton, pos=center)
    assert _stages(panel)[0]["params"]["hue"] == pytest.approx(float(chromogen_hue(pixel)[0, 0]))
    assert not panel.hue_pick_button.isChecked()
