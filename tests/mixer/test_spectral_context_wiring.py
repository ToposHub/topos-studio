"""P1-3 · 语图右键菜单的生产接线（2026-11-21 计划）。

验收来自 docs/plans/2026-11-21-mixer-clip-editor-backend-handoff.md：

- 频谱 heal / attenuate 分别接修复处理器（spectral_repair 预渲染步骤，
  笔迹承载选区时间与频带）；
- 仅试听该频段（solo_band）走带通试听：不入工程，选区外静音；
- 每条命令按实际能力拒绝：锁轨、无选区、PCM 未就绪均有明确提示
  （不再静默无效果）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

RATE = 48000


def _editor_with_clip(qapp, pcm=None, locked=False):
    from src.features.edit.core.timeline_v2 import ClipInstance
    from src.features.mixer.ui.project_clip_editor import ProjectClipEditor

    if pcm is None:
        t = np.arange(2 * RATE) / RATE
        pcm = np.stack([
            (0.4 * np.sin(2 * np.pi * 2000.0 * t)).astype(np.float32)] * 2)
    editor = ProjectClipEditor()
    panel = editor._ensure_panel()
    clip = ClipInstance("c1", "a1", "a1", 0, 48, 0, 48)
    editor._clip = clip
    editor._fps = 24.0
    editor._locked = locked
    panel._duration = pcm.shape[1] / RATE
    panel.attach_pcm(pcm, RATE)
    panel._preview_offline = True
    panel._model_audio_edit = None
    committed = []
    editor._commit_audio_edit = (
        lambda edit, label: committed.append((edit, label)))
    messages = []
    original = panel.set_status_message

    def _capture(text, *args, **kwargs):
        messages.append(str(text))
        return original(text, *args, **kwargs)

    panel.set_status_message = _capture
    return editor, panel, committed, messages


class TestSpectralContextMenuWiring:
    def test_canvas_right_click_reaches_spectral_menu(self, qapp, qtbot, monkeypatch) -> None:
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QMenu

        editor, panel, committed, _messages = _editor_with_clip(qapp)
        qtbot.addWidget(editor)
        editor._empty.hide()
        editor._scroll.show()
        editor.resize(1320, 700)
        panel.set_stack_editable_hint(True)
        panel._refresh_stack_preview()
        panel.btn_spectral.setChecked(True)
        panel.canvas.setSelection(0.5, 1.0, 1800.0, 2200.0)
        panel.canvas._is_2d_spectral_sel = True
        editor.show()

        def choose_attenuation(menu: QMenu, *_args: object) -> None:
            action = next(action for action in menu.actions() if "-12 dB" in action.text())
            assert action.isEnabled()
            action.trigger()

        monkeypatch.setattr(QMenu, "exec", choose_attenuation)
        _, spectrum, _ = panel.canvas._wave_and_spec_rects()
        qtbot.mouseClick(panel.canvas, Qt.MouseButton.RightButton, pos=spectrum.center().toPoint())
        assert len(committed) == 1
        assert committed[0][0]["steps"][0]["params"]["mode"] == "attenuate"

    def test_heal_commits_spectral_repair_step(self, qapp) -> None:
        editor, panel, committed, _messages = _editor_with_clip(qapp)
        editor._on_spectral_repair_requested(0.5, 1.0, 1800.0, 2200.0, "heal")
        assert len(committed) == 1
        edit, _ = committed[0]
        steps = edit["steps"]
        assert len(steps) == 1 and steps[0]["processor_id"] == "spectral_repair"
        params = steps[0]["params"]
        assert params["mode"] == "heal"
        # 选区换算到输出域：0.5–1.0 s → 24000–48000。
        assert params["start"] == 24000 and params["end"] == 48000
        stroke = params["strokes"][0]
        assert stroke["sample"] == 36000
        assert stroke["hz"] == pytest.approx((1800.0 * 2200.0) ** 0.5)
        assert stroke["time_radius"] == 12000

    def test_attenuate_commits_attenuate_mode(self, qapp) -> None:
        editor, panel, committed, _messages = _editor_with_clip(qapp)
        editor._on_spectral_repair_requested(0.5, 1.0, 1800.0, 2200.0,
                                             "attenuate")
        edit, _ = committed[0]
        assert edit["steps"][0]["params"]["mode"] == "attenuate"
        assert edit["steps"][0]["params"]["amount_db"] == pytest.approx(12.0)

    def test_locked_track_is_rejected_with_message(self, qapp) -> None:
        editor, panel, committed, messages = _editor_with_clip(qapp, locked=True)
        editor._on_spectral_repair_requested(0.5, 1.0, 1800.0, 2200.0, "heal")
        assert committed == []
        assert any("locked" in m.lower() for m in messages)

    def test_no_selection_is_rejected(self, qapp) -> None:
        editor, panel, committed, messages = _editor_with_clip(qapp)
        editor._on_spectral_repair_requested(0.0, 0.0, 1800.0, 2200.0, "heal")
        assert committed == []
        assert messages

    def test_solo_band_auditions_without_commit(self, qapp, monkeypatch) -> None:
        pytest.importorskip("scipy.signal")
        editor, panel, committed, _messages = _editor_with_clip(qapp)
        loaded = []
        played = []
        monkeypatch.setattr(
            panel._audition, "load",
            lambda pcm, rate: loaded.append((pcm, rate)))
        monkeypatch.setattr(
            panel._audition, "play", lambda start=None: played.append(start) or True)
        editor._on_spectral_repair_requested(
            0.5, 1.0, 1800.0, 2200.0, "solo_band")
        assert committed == []  # 不入工程
        assert loaded and played == [0]
        filtered = loaded[0][0]
        assert panel._band_preview[0] == 24000
        assert filtered.shape[1] == 24000
        assert not np.any(filtered[:, (0, -1)])
        assert float(np.abs(filtered[:, 5000:15000]).max()) > 0.01
        panel._on_audition_playing_changed(False)
        assert panel._band_preview is None
        assert loaded[-1][0] is panel._pcm
