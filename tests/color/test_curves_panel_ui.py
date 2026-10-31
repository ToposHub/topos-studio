"""UI composition, draft isolation and original curve interaction contracts."""

from PyQt6.QtCore import QPointF
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QBoxLayout

from src.features.color.core.workspace_presets import (
    ColorWorkspacePreset, builtin_color_workspaces, sanitize_preset,
)
from src.features.color.module import ColorModule
from src.features.color.ui.curves_panel import CurvesPanel
from src.shared.layout.store import LayoutStore
from src.shared.panels.registry import PanelRegistry
from src.shared.ui.widgets.curves_widget import CurvesWidget


def test_sidebar_drafts_do_not_modify_curve_data_or_emit_render_updates(qapp):
    canvas = CurvesWidget()
    before = canvas.get_curves_data()
    changed = QSignalSpy(canvas.changed)
    panel = CurvesPanel(canvas)
    panel.edit_rows["R"].slider.setValue(40)
    panel.soft_clip_rows["low_soft"].slider.setValue(32)
    panel.edit_buttons["B"].click()
    panel._reset_edit()
    panel._reset_soft_clip()
    QTest.qWait(30)  # Allow any unintended throttled render signal to fire.

    assert panel.curves_widget is canvas
    assert canvas.active_channel == "B"
    assert canvas.get_curves_data() == before
    assert len(changed) == 0


def test_soft_clip_defaults_match_mobile_identity_semantics(qapp):
    panel = CurvesPanel(CurvesWidget())
    for values in panel.get_ui_state()["soft_clip_channels"].values():
        assert values == {
            "low_clip": 0.0, "low_soft": 0.0, "high_clip": 1.0, "high_soft": 0.0,
        }
    # Slider bounds mirror the mobile SoftClipPanel: Low in [-1, High],
    # High in [Low, 2], knees inside the remaining gap.
    assert panel.soft_clip_rows["low_clip"].slider.minimum() == -100
    assert panel.soft_clip_rows["low_clip"].slider.maximum() == 100
    assert panel.soft_clip_rows["high_clip"].slider.minimum() == 0
    assert panel.soft_clip_rows["high_clip"].slider.maximum() == 200

    # Raising Low re-fits the knees into the shrunk gap.
    panel.soft_clip_rows["low_clip"].slider.setValue(80)
    values = panel.get_ui_state()["soft_clip_channels"]["R"]
    assert values["low_clip"] == 0.8
    panel.soft_clip_rows["low_soft"].slider.setValue(100)
    values = panel.get_ui_state()["soft_clip_channels"]["R"]
    assert values["low_soft"] == 0.2
    # The two knees share the gap exclusively.
    panel.soft_clip_rows["high_soft"].slider.setValue(100)
    values = panel.get_ui_state()["soft_clip_channels"]["R"]
    assert values["low_soft"] + values["high_soft"] <= 0.2 + 1e-9

    # Per-row reset returns that control to its identity default.
    panel._reset_soft_parameter("low_clip")
    assert panel.get_ui_state()["soft_clip_channels"]["R"]["low_clip"] == 0.0


def test_soft_clip_link_unlink_keeps_independent_channel_drafts(qapp):
    panel = CurvesPanel(CurvesWidget())
    panel.soft_clip_rows["low_soft"].slider.setValue(25)
    assert all(values["low_soft"] == 0.25 for values in panel.get_ui_state()["soft_clip_channels"].values())

    panel.soft_clip_link_btn.click()
    panel.soft_clip_buttons["G"].click()
    panel.soft_clip_rows["low_soft"].slider.setValue(70)
    panel.soft_clip_buttons["R"].click()
    assert panel.soft_clip_rows["low_soft"].value() == 0.25
    panel.soft_clip_buttons["G"].click()
    assert panel.soft_clip_rows["low_soft"].value() == 0.70

    panel.soft_clip_link_btn.click()
    assert all(values["low_soft"] == 0.70 for values in panel.get_ui_state()["soft_clip_channels"].values())
    state = panel.get_ui_state()
    state["soft_clip_channels"]["G"]["low_soft"] = 0.0
    assert panel.get_ui_state()["soft_clip_channels"]["G"]["low_soft"] == 0.70


def test_reparented_picker_and_curve_reset_keep_existing_signals(qapp):
    canvas = CurvesWidget()
    panel = CurvesPanel(canvas)
    toggled = QSignalSpy(canvas.picker_toggled)
    changed = QSignalSpy(canvas.changed)
    assert canvas.btn_container.isHidden()
    assert panel.picker_btn is canvas.picker_btn
    panel.picker_btn.click()
    assert list(toggled[0]) == [True]
    canvas.deactivate_picker()
    assert list(toggled[1]) == [False]

    canvas.curves["R"].insert(1, QPointF(128, 60))
    panel.reset_curves_btn.click()
    canvas.flush()
    assert len(changed) == 1
    assert canvas.get_curves_data()["R"] == [(0.0, 255.0), (255.0, 0.0)]


def test_responsive_layout_keeps_all_soft_clip_rows_accessible(qapp):
    panel = CurvesPanel(CurvesWidget())
    panel.resize(1000, 420)
    panel.show()
    qapp.processEvents()
    assert panel.body.direction() == QBoxLayout.Direction.LeftToRight
    panel.resize(480, 420)
    qapp.processEvents()
    assert panel.body.direction() == QBoxLayout.Direction.TopToBottom
    panel.sidebar_scroll.ensureWidgetVisible(panel.soft_clip_rows["high_soft"])
    qapp.processEvents()
    row = panel.soft_clip_rows["high_soft"]
    assert row.visibleRegion().boundingRect().height() > 0
    panel.close()


def test_soft_clip_has_no_standalone_panel_entry_and_hdr_uses_curves():
    registry = PanelRegistry()
    ColorModule().register_panels(registry)
    assert registry.get("color.soft_clip") is None
    assert registry.get("color.curves").dock_attr == "dock_curves"
    for product in ("color", "studio"):
        assert "color.soft_clip" not in LayoutStore.DEFAULT_PANELS[product]["Color"]
    hdr = next(p for p in builtin_color_workspaces() if p.preset_id == "builtin.hdr")
    assert "color.curves" in hdr.visible_panels
    assert "color.soft_clip" not in hdr.visible_panels


def test_saved_soft_clip_workspace_opens_combined_curves_palette():
    saved = ColorWorkspacePreset("user.old", "Old HDR", frozenset({"color.soft_clip", "color.raw"}))
    visible = sanitize_preset(saved, ["workspace.viewport", "color.curves", "color.raw"])
    assert "color.curves" in visible
    assert "color.raw" in visible
    assert "color.soft_clip" not in visible
