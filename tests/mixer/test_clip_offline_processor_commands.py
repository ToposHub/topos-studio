"""Offline selection processing uses one project command and temporary delta monitoring."""
from __future__ import annotations

import numpy as np

from src.features.edit.core.audio_edit import render_audio_edit_full
from src.features.edit.core.timeline_v2 import ClipInstance
from src.features.mixer.ui.project_clip_editor import ProjectClipEditor

RATE = 48000


def setup_editor(qapp):
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
    panel.canvas._edit_channel = 0
    return editor, panel, pcm


def test_apply_commits_once_and_preview_delta_are_temporary(qapp) -> None:
    editor, panel, pcm = setup_editor(qapp)
    committed = []
    editor.audio_edit_committed.connect(lambda *args: committed.append(args))
    auditioned = []
    panel.start_band_preview = lambda start, samples: auditioned.append(samples) or True
    editor._on_offline_processor_requested("de_hum", "preview")
    editor._on_offline_processor_requested("de_hum", "delta")
    assert len(auditioned) == 2
    assert not committed
    np.testing.assert_allclose(auditioned[0] + auditioned[1], pcm, atol=1e-7)
    editor._on_offline_processor_requested("de_hum", "apply")
    assert len(committed) == 1
    clip_id, edit, _label = committed[0]
    assert clip_id == "c1"
    assert edit["steps"][0]["params"]["channels"] == [0]
    out = render_audio_edit_full(pcm, edit)
    np.testing.assert_array_equal(out, auditioned[0])
    np.testing.assert_array_equal(out[1], pcm[1])
    np.testing.assert_array_equal(out[:, :RATE // 2], pcm[:, :RATE // 2])
    editor.deleteLater()


def test_locked_or_changed_selection_cannot_commit(qapp) -> None:
    editor, panel, _pcm = setup_editor(qapp)
    queued = []
    editor._submit_analysis = lambda job, publish, **kwargs: queued.append(kwargs["current"])
    editor._on_offline_processor_requested("de_click", "apply")
    assert len(queued) == 1 and queued[0]()
    panel.canvas.setSelection(0.2, 0.8)
    assert not queued[0]()
    editor._locked = True
    editor._on_offline_processor_requested("de_click", "apply")
    assert len(queued) == 1
    editor.deleteLater()
