"""Offline selection processing uses one project command and temporary delta monitoring."""
from __future__ import annotations

import numpy as np
from PyQt6.QtWidgets import QApplication, QMenu

from src.features.edit.core.audio_edit import render_audio_edit_full
from src.features.edit.core.timeline_v2 import ClipInstance
from src.features.mixer.ui.project_clip_editor import ProjectClipEditor, _TimelineClipPanel
from src.features.mixer.ui.selection_effect_dialog import selection_effect_name

RATE = 48000


def setup_editor(qapp: QApplication) -> tuple[ProjectClipEditor, _TimelineClipPanel, np.ndarray]:
    editor = ProjectClipEditor()
    panel = editor._ensure_panel()
    panel._preview_offline = True
    editor._clip = ClipInstance("c1", "a1", "a1", 0, 48, 0, 48)
    editor._fps = 24.0
    t = np.arange(2 * RATE) / RATE
    signal = (0.2 * np.sin(2 * np.pi * 220 * t) + 0.08 * np.sin(2 * np.pi * 50 * t)).astype(np.float32)
    pcm = np.stack([signal, signal])
    panel._duration = 2.0
    panel.attach_pcm(pcm, RATE)
    panel.set_stack_editable_hint(True)
    panel.canvas.setSelection(0.5, 1.5)
    panel.canvas.setEditChannel(0)
    return editor, panel, pcm


def test_apply_commits_once_and_preview_delta_are_temporary(qapp) -> None:
    editor, panel, pcm = setup_editor(qapp)
    committed = []
    editor.audio_edit_committed.connect(lambda *args: committed.append(args))
    auditioned = []
    panel.start_band_preview = lambda start, samples: auditioned.append(samples) or True
    menu = QMenu(editor)
    editor._append_offline_processor_actions(menu)
    actions = menu.actions()[0].menu().actions()
    next(action for action in actions if action.text() == selection_effect_name("declick_dehum")).trigger()
    dialog = editor._selection_effect_dialog
    dialog.click_enabled.setChecked(False)
    dialog.hum_enabled.setChecked(True)
    dialog.preview.click()
    dialog.delta.click()
    assert len(auditioned) == 2
    assert not committed
    start, end = RATE // 2, 3 * RATE // 2
    np.testing.assert_allclose(auditioned[0] + auditioned[1], pcm[:, start:end], atol=1e-7)
    dialog.apply.click()
    assert len(committed) == 1
    clip_id, edit, _label = committed[0]
    assert clip_id == "c1"
    assert edit["steps"][0]["params"]["channels"] == [0]
    out = render_audio_edit_full(pcm, edit)
    np.testing.assert_array_equal(out[:, start:end], auditioned[0])
    np.testing.assert_array_equal(out[1], pcm[1])
    np.testing.assert_array_equal(out[:, :RATE // 2], pcm[:, :RATE // 2])
    editor.close()


def test_locked_or_draft_speed_selection_cannot_commit(qapp) -> None:
    editor, panel, _pcm = setup_editor(qapp)
    editor._locked = True
    editor.open_selection_effect("declick_dehum")
    assert editor._selection_effect_dialog is None
    editor._locked = False
    editor.open_selection_effect("declick_dehum")
    dialog = editor._selection_effect_dialog
    assert dialog.apply.isEnabled()
    panel.set_pending_timepitch((0, RATE, 125.0, 0.0, True))
    assert not dialog.apply.isEnabled()
    panel.set_pending_timepitch(None)
    assert dialog.apply.isEnabled()
    editor.close()
