"""P1-2 · 预览内容复用与变速预览的 worker 快照。

验收来自 docs/plans/2026-11-21-mixer-clip-editor-backend-handoff.md：

- FFT 切换优先复用已发布 PCM：内容键（源/栈/降噪/变速/gain-fade）未
  变时只重算频谱，不重跑基底与处理栈，不打断设备会话；
- 变速/变调预览请求在面板上快照化（不再在 UI 线程物化整段 PCM），
  与待提交参数共用同一份快照。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

RATE = 48000


def _panel_with_pcm(qapp):
    from src.features.edit.core.timeline_v2 import ClipInstance
    from src.features.mixer.ui.project_clip_editor import ProjectClipEditor

    editor = ProjectClipEditor()
    panel = editor._ensure_panel()
    clip = ClipInstance("c1", "a1", "a1", 0, 48, 0, 48)
    editor._clip = clip
    editor._fps = 24.0
    editor._locked = False
    t = np.arange(2 * RATE) / RATE
    pcm = np.stack([(0.4 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)] * 2)
    panel._duration = pcm.shape[1] / RATE
    panel.attach_pcm(pcm, RATE)
    panel._preview_offline = True
    panel._model_audio_edit = None
    return editor, panel


class TestFftReusesEditedPcm:
    def test_fft_change_skips_base_computation(self, qapp, monkeypatch) -> None:
        editor, panel = _panel_with_pcm(qapp)
        calls = []
        original = panel._compute_base_pcm

        def counting_base(inputs):
            calls.append(1)
            return original(inputs)

        monkeypatch.setattr(panel, "_compute_base_pcm", counting_base)
        panel._refresh_stack_preview()
        first_pcm = panel._pcm
        assert calls, "base should compute on first preview"
        base_calls = len(calls)
        # FFT 换档：内容键未变 → 复用 edited，不重跑基底。
        panel.fft_combo.setCurrentIndex(
            (panel.fft_combo.currentIndex() + 1) % panel.fft_combo.count())
        panel._refresh_stack_preview()
        assert len(calls) == base_calls
        # 复用已发布 PCM：内容恒等（新结果只差频谱矩阵）。
        np.testing.assert_array_equal(panel._pcm, first_pcm)

    def test_content_change_invalidates_reuse(self, qapp, monkeypatch) -> None:
        editor, panel = _panel_with_pcm(qapp)
        calls = []
        original = panel._compute_base_pcm

        def counting_base(inputs):
            calls.append(1)
            return original(inputs)

        monkeypatch.setattr(panel, "_compute_base_pcm", counting_base)
        panel._refresh_stack_preview()
        base_calls = len(calls)
        # 内容变化（选区增益栈）→ 重新计算基底。
        from src.features.edit.core.audio_edit import make_step, new_audio_edit

        panel.sync_audio_edit(new_audio_edit([make_step(
            "mute", 48000, 96000, (0, 1))]))
        assert len(calls) > base_calls


class TestTimepitchRequestSnapshot:
    def test_draft_uses_committed_stack_order_and_maps_bypass(self, qapp) -> None:
        from src.features.edit.core.audio_edit import (
            make_step, new_audio_edit, remap_audio_edit_for_length_steps, render_audio_edit_full,
        )
        from src.features.mixer.ui.clip_analysis import ClipAuditionCoordinates, make_timepitch_draft

        editor, panel = _panel_with_pcm(qapp)
        stack = new_audio_edit([make_step("range_gain", 12000, 60000, (0,), gain_db=-6.0)])
        panel.set_stack_editable_hint(True)
        panel.sync_audio_edit(stack)
        editor._on_time_pitch_requested(0.5, 1.5, 200.0, 0.0, False, "")
        request = panel._timepitch_request
        candidate, step = make_timepitch_draft(stack, request, panel._raw_pcm.shape[1], (0, 1))
        committed, _ = remap_audio_edit_for_length_steps(candidate, [step])
        expected = render_audio_edit_full(panel._raw_pcm, committed)
        np.testing.assert_array_equal(panel._pcm, expected)
        assert not panel.canvas.project_edits_enabled
        assert panel._audition_coordinates.map_to(ClipAuditionCoordinates(), 36000) == 48000
        panel.btn_bypass.setChecked(True)
        assert panel._duration == pytest.approx(2.0)
        np.testing.assert_array_equal(panel._pcm, panel._raw_pcm)

    def test_request_snapshot_drives_preview_and_pending(self, qapp) -> None:
        editor, panel = _panel_with_pcm(qapp)
        editor._on_time_pitch_requested(0.5, 1.5, 200.0, 0.0, False, "")
        # 请求快照与待提交参数一致（预览==提交输入）。
        assert panel._timepitch_request == panel._pending_timepitch
        lo, hi, ratio, _st, _p = panel._timepitch_request
        assert (lo, hi, ratio) == (24000, 72000, 200.0)
        # 预览长度按 2× 合同长度：1 s 选区 → 0.5 s，总 0.5+0.5+0.5=1.5 s。
        assert panel._pcm is not None
        assert panel._pcm.shape[1] == pytest.approx(
            24000 + 24000 + (96000 - 72000), abs=4)
        assert panel._duration == pytest.approx(1.5, abs=1e-3)
        # 清除预览回到原始长度。
        panel.clear_timepitch_preview()
        assert panel._timepitch_request is None
        assert panel._duration == pytest.approx(2.0, abs=1e-6)
