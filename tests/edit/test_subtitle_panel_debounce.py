"""SUB-P1-01 contract: subtitle panel debounce & merged commits.

The bug: ``textChanged`` emitted ``subtitle_param_changed`` per keystroke, so
typing 40 characters created 40 undo entries and 40 timeline commands; a color
pick emitted 4 separate channel commands.

The fix: text uses a 250 ms debounce + focus-out commit and emits one merged
``subtitle_params_changed``; the color picker emits one 4-field RGBA patch.

These tests drive the panel directly and capture signal emissions.
"""

from __future__ import annotations

from dataclasses import replace
from typing import List, Tuple

import pytest

from src.features.edit.core.timeline_v2 import SubtitleClip
from src.features.edit.core.timeline_v2 import effective_subtitle_font_size
from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel
from src.shared.ui.widgets import ChevronComboBox

pytestmark = pytest.mark.edit_core


def _clip(text: str = "hello", **overrides) -> SubtitleClip:
    fields = dict(
        clip_id="sub-1", track_id="s1",
        timeline_start=0, timeline_duration=72, text=text,
    )
    fields.update(overrides)
    return SubtitleClip(**fields)


class _ParamsRecorder:
    """Capture subtitle_params_changed emissions (clip_id, track_id, updates)."""

    def __init__(self, panel: EditPropertiesPanel) -> None:
        self.panel = panel
        self.emitted: List[Tuple[str, str, dict]] = []
        panel.subtitle_params_changed.connect(self._record)

    def _record(self, clip_id: str, track_id: str, updates: dict) -> None:
        # Copy to avoid mutation aliasing.
        self.emitted.append((clip_id, track_id, dict(updates)))


class _SingleParamRecorder:
    """Capture legacy single-field subtitle_param_changed emissions."""

    def __init__(self, panel: EditPropertiesPanel) -> None:
        self.panel = panel
        self.count = 0
        panel.subtitle_param_changed.connect(self._record)

    def _record(self, *_args) -> None:
        self.count += 1


def _set_subtitle(panel: EditPropertiesPanel, clip: SubtitleClip) -> None:
    """Build the subtitle sections without requiring a full controller."""
    panel._updating = True
    try:
        panel._current_subtitle_clip = clip
        # Clear previous sections.
        if hasattr(panel, "_sections"):
            panel._sections.clear()
        panel.set_subtitle_clip(clip, 24.0)
    finally:
        panel._updating = False


def test_text_typing_does_not_emit_per_keystroke(qapp) -> None:
    panel = EditPropertiesPanel()
    _set_subtitle(panel, _clip(text="hi"))
    params_rec = _ParamsRecorder(panel)
    single_rec = _SingleParamRecorder(panel)

    edit = getattr(panel, "_subtitle_text_edit", None)
    assert edit is not None, "subtitle text edit not built"

    # Simulate typing 5 characters without letting the debounce timer fire.
    for ch in "abcde":
        edit.blockSignals(True)
        edit.setPlainText(edit.toPlainText() + ch)
        edit.blockSignals(False)
        # Manually trigger textChanged as the real widget would.
        edit.textChanged.emit()

    # No commit yet (debounce window still open) and zero per-keystroke emits.
    assert params_rec.emitted == []
    assert single_rec.count == 0


def test_text_debounce_commits_one_merged_command(qapp) -> None:
    panel = EditPropertiesPanel()
    _set_subtitle(panel, _clip(text="hi"))
    rec = _ParamsRecorder(panel)

    edit = getattr(panel, "_subtitle_text_edit", None)
    assert edit is not None

    # Type several characters, then fire the debounce timer.
    for ch in "abcde":
        edit.blockSignals(True)
        edit.setPlainText(edit.toPlainText() + ch)
        edit.blockSignals(False)
        edit.textChanged.emit()

    # Force the debounce window to expire.
    panel._subtitle_text_timer.stop()
    panel._subtitle_text_timer.timeout.emit()

    # Exactly one merged commit carrying the final text.
    assert len(rec.emitted) == 1
    clip_id, track_id, updates = rec.emitted[0]
    assert clip_id == "sub-1"
    assert track_id == "s1"
    assert set(updates.keys()) == {"text"}
    assert updates["text"] == "hiabcde"


def test_text_focus_out_commits_immediately(qapp) -> None:
    panel = EditPropertiesPanel()
    _set_subtitle(panel, _clip(text=""))
    rec = _ParamsRecorder(panel)

    edit = getattr(panel, "_subtitle_text_edit", None)
    assert edit is not None

    edit.blockSignals(True)
    edit.setPlainText("done")
    edit.blockSignals(False)
    edit.textChanged.emit()

    # focus-out should commit right away, without waiting for the timer.
    edit.focusOutEvent(None)

    assert len(rec.emitted) == 1
    assert rec.emitted[0][2]["text"] == "done"


def test_text_unchanged_value_does_not_commit(qapp) -> None:
    panel = EditPropertiesPanel()
    _set_subtitle(panel, _clip(text="same"))
    rec = _ParamsRecorder(panel)

    edit = getattr(panel, "_subtitle_text_edit", None)
    assert edit is not None
    # "Type" the same value back.
    edit.blockSignals(True)
    edit.setPlainText("same")
    edit.blockSignals(False)
    edit.textChanged.emit()

    panel._subtitle_text_timer.stop()
    panel._subtitle_text_timer.timeout.emit()

    assert rec.emitted == []


# ── DragValue color session (SUB-P1-01) ────────────────────────────────────


def _fill_drag(panel: EditPropertiesPanel, key: str):
    """Return the DragValue widget for a fill channel, or None."""
    for sid, info in getattr(panel, "_sections", {}).items():
        widgets = getattr(panel._sections[sid], "_param_widgets", {}) \
            if hasattr(panel._sections[sid], "_param_widgets") else {}
        if key in widgets:
            return widgets[key]["widget"]
    return None


def test_dragvalue_drag_emits_one_merged_patch(qapp) -> None:
    """Dragging a fill channel emits one RGBA patch on release, not per tick."""
    panel = EditPropertiesPanel()
    _set_subtitle(panel, _clip(fill_r=0.2, fill_g=0.3, fill_b=0.4, fill_a=1.0))
    params_rec = _ParamsRecorder(panel)
    single_rec = _SingleParamRecorder(panel)

    r_drag = _fill_drag(panel, "fill_r")
    assert r_drag is not None, "fill_r DragValue not built"

    # Begin drag session, then several value ticks. setValue updates the
    # widget's internal value (so the release-time snapshot reads the final
    # value); valueChanged drives the panel handler as a real drag would.
    r_drag.interaction_started.emit()
    for v in (0.3, 0.4, 0.5, 0.6):
        r_drag.setValue(v)
        r_drag.valueChanged.emit(v)

    # No commit during the drag (session active).
    assert params_rec.emitted == []
    assert single_rec.count == 0

    # End drag → one merged patch with the final value.
    r_drag.interaction_ended.emit()
    assert len(params_rec.emitted) == 1
    _cid, _tid, updates = params_rec.emitted[0]
    assert updates == {"fill_r": 0.6}


def test_dragvalue_spinner_step_emits_single_field(qapp) -> None:
    """Outside a drag (e.g. a programmatic step), a single field is emitted."""
    panel = EditPropertiesPanel()
    _set_subtitle(panel, _clip(fill_r=0.5))
    params_rec = _ParamsRecorder(panel)
    single_rec = _SingleParamRecorder(panel)

    r_drag = _fill_drag(panel, "fill_r")
    assert r_drag is not None

    # valueChanged without an active session → single-field emit.
    r_drag.valueChanged.emit(0.7)
    assert single_rec.count == 1
    assert params_rec.emitted == []


def test_dragvalue_unchanged_drag_does_not_commit(qapp) -> None:
    """A drag that returns to the original value commits nothing."""
    panel = EditPropertiesPanel()
    _set_subtitle(panel, _clip(fill_r=0.5))
    params_rec = _ParamsRecorder(panel)

    r_drag = _fill_drag(panel, "fill_r")
    assert r_drag is not None

    r_drag.interaction_started.emit()
    r_drag.setValue(0.6)
    r_drag.valueChanged.emit(0.6)
    r_drag.setValue(0.5)
    r_drag.valueChanged.emit(0.5)  # back to start
    r_drag.interaction_ended.emit()

    assert params_rec.emitted == []


def test_handle_transform_sync_updates_widgets_without_emitting(qapp) -> None:
    """On-canvas handle drags update Transform fields without feedback loops."""
    panel = EditPropertiesPanel()
    clip = _clip()
    _set_subtitle(panel, clip)
    params_rec = _ParamsRecorder(panel)
    single_rec = _SingleParamRecorder(panel)
    updated = replace(
        clip,
        translate_x=120.0,
        translate_y=-35.0,
        scale_x=1.5,
        scale_y=0.8,
        rotation=42.0,
        pivot_mode="custom",
        pivot_x=320.0,
        pivot_y=240.0,
    )

    panel.update_subtitle_transform_values(updated)

    section = panel._sections["subtitle:sub-1:s1:transform"]
    assert section._param_widgets["translate_x"]["widget"].value() == 120.0
    assert section._param_widgets["translate_y"]["widget"].value() == -35.0
    assert section._param_widgets["scale_x"]["widget"].value() == 1.5
    assert section._param_widgets["scale_y"]["widget"].value() == 0.8
    assert section._param_widgets["rotation"]["widget"].value() == 42.0
    pivot = section._param_widgets["pivot_mode"]["widget"]
    assert pivot.currentText() == "custom"
    assert section._param_widgets["pivot_x"]["widget"].value() == 320.0
    assert section._param_widgets["pivot_y"]["widget"].value() == 240.0
    assert params_rec.emitted == []
    assert single_rec.count == 0


def test_auto_font_size_is_displayed_as_effective_pixels(qapp) -> None:
    panel = EditPropertiesPanel()
    _set_subtitle(panel, _clip(font_size=0))

    section = panel._sections["subtitle:sub-1:s1:font"]
    widget = section._param_widgets["font_size"]["widget"]
    assert widget.value() == effective_subtitle_font_size(0, 1080)


def test_rebuilding_subtitle_panel_closes_combo_popups_before_deletion(
    qapp, monkeypatch,
) -> None:
    """Avoid leaving a native QListView popup after its section is detached."""
    panel = EditPropertiesPanel()
    _set_subtitle(panel, _clip(clip_id="sub-1"))
    closed: list[ChevronComboBox] = []
    original_hide_popup = ChevronComboBox.hidePopup

    def _record_hide_popup(combo: ChevronComboBox) -> None:
        closed.append(combo)
        original_hide_popup(combo)

    monkeypatch.setattr(ChevronComboBox, "hidePopup", _record_hide_popup)
    panel.set_subtitle_clip(_clip(clip_id="sub-2"), 24.0)

    assert closed


def test_rebuilding_subtitle_panel_stops_previous_text_debounce_timer(qapp) -> None:
    """A timeout for the old cue must not fire after selection changes."""
    panel = EditPropertiesPanel()
    _set_subtitle(panel, _clip(clip_id="sub-1", text="first"))
    previous_timer = panel._subtitle_text_timer
    previous_timer.start()

    panel.set_subtitle_clip(_clip(clip_id="sub-2", text="second"), 24.0)

    assert not previous_timer.isActive()
    assert panel._subtitle_text_timer is not previous_timer
