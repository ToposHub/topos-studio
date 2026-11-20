"""P0 · 重开/切换预览使用同一已处理数据（2026-11-19 计划第一阶段）。

验收来自 docs/plans/2026-11-19-film-post-audio-audit-and-roadmap.md：

- 复现：48 kHz、1 秒、振幅 0.25 的立体声 WAV 保存全段静音栈。重开时
  原代码首次试听仍为 0.25，切一次 A/B 才变为 0。修复后冷启加载静音
  和降噪，第一次试听即正确版本；
- 预览 worker 按 (spec, steps, view, fft, generation) 发布；旧回调
  （切片段/重连/退出）不误写；同 key 合并；失败清理；可结束。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_edit import make_step, new_audio_edit

RATE = 48000


def _panel(qapp):
    from src.features.mixer.ui.project_clip_editor import ProjectClipEditor

    editor = ProjectClipEditor()
    panel = editor._ensure_panel()
    panel._preview_offline = True  # 测试：同步计算直接投影
    return editor, panel


def _mute_edit(total: int) -> dict:
    return new_audio_edit([make_step("mute", 0, total, (0, 1))])


def _pcm(amp: float = 0.25, samples: int = RATE) -> np.ndarray:
    t = np.arange(samples) / RATE
    sig = (amp * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    return np.stack([sig, sig])


class TestReopenProcessedPreview:
    def test_mute_stack_first_audition_is_processed(self, qapp) -> None:
        """重开静音栈：首次装载即静音（原代码首次为 0.25 源音）。"""
        _editor, panel = _panel(qapp)
        panel._duration = 1.0
        panel.sync_audio_edit(_mute_edit(RATE))
        panel.attach_pcm(_pcm(), RATE)
        panel.sync_gain_fade(0.0, 0.2, 0.3, "power_v1:1.50000", "linear", locked=False)
        # attach_pcm 后不切 A/B、不重载——预览发布即为处理结果。
        panel._refresh_stack_preview()
        assert panel._pcm is not None
        assert (panel.canvas._fade_in_sec, panel.canvas._fade_out_sec) == (0.2, 0.3)
        # 静音含 3 ms 边缘过渡（防爆音合同）：内部为 0，整体峰值远低于
        # 原始 0.25（原 bug 首次预览即源音 0.25）。
        assert np.max(np.abs(panel._pcm[:, 500:-500])) == 0.0
        assert np.max(np.abs(panel._pcm)) < 0.25 * 0.9

    def test_denoise_stack_preview_matches_render_path(self, qapp) -> None:
        """降噪栈：预览 == 渲染管线输出（suffix 语义，无双重应用）。"""
        from src.features.edit.core.audio_edit import (
            apply_audio_edit_to_pcm, render_pre_render_steps, suffix_audio_edit,
        )

        rng = np.random.default_rng(20261119)
        t = np.arange(RATE // 2) / RATE
        tone = 0.25 * np.sin(2 * np.pi * 440.0 * t)
        pcm = np.stack([((tone + 0.02 * rng.standard_normal(RATE // 2))
                         .astype(np.float32))] * 2)
        total = RATE // 2
        edit = new_audio_edit([
            make_step("range_gain", 0, total, (0, 1), gain_db=-6.0),
            make_step("spectral_denoise", 0, total, (0, 1),
                      noise_start=0, noise_end=total // 2, reduction_db=12.0),
            make_step("mute", 0, 100, (0,)),
        ])
        _editor, panel = _panel(qapp)
        panel._duration = 0.5
        panel.sync_audio_edit(edit)
        panel.attach_pcm(pcm, RATE)
        panel._refresh_stack_preview()

        expected_base = render_pre_render_steps(pcm, edit, sample_rate=RATE)
        expected = apply_audio_edit_to_pcm(
            expected_base, suffix_audio_edit(edit), sample_rate=RATE)
        np.testing.assert_allclose(panel._pcm, expected, atol=1e-6)

    def test_bypass_toggle_restores_original(self, qapp) -> None:
        _editor, panel = _panel(qapp)
        panel._duration = 1.0
        panel.sync_audio_edit(_mute_edit(RATE))
        panel.attach_pcm(_pcm(), RATE)
        panel._refresh_stack_preview()
        assert np.max(np.abs(panel._pcm[:, 500:-500])) == 0.0
        # A/B 旁路：立即回到原始 PCM（临时监听状态）。
        panel.btn_bypass.setChecked(True)
        panel._refresh_stack_preview()
        assert np.max(np.abs(panel._pcm)) == pytest.approx(0.25, abs=0.01)
        panel.btn_bypass.setChecked(False)
        panel._refresh_stack_preview()
        assert np.max(np.abs(panel._pcm[:, 500:-500])) == 0.0


class TestPreviewGenerationGuard:
    def test_stale_result_does_not_overwrite(self, qapp) -> None:
        """切换片段（代际递增）后，旧预览发布被丢弃。"""
        from src.features.mixer.ui.clip_preview_worker import ClipPreviewResult

        _editor, panel = _panel(qapp)
        panel._duration = 1.0
        panel.sync_audio_edit(_mute_edit(RATE))
        panel.attach_pcm(_pcm(), RATE)
        stale_key = ("old-spec", (), None, 2048, panel._preview_generation, False)
        panel._preview_generation += 1  # 模拟切换到新片段
        stale = ClipPreviewResult(
            key=stale_key, edited=_pcm(0.9), sample_rate=RATE, duration=1.0,
            mins=np.zeros(8), maxs=np.zeros(8), rmss=np.zeros(8),
            spectrogram=[], report={})
        panel._on_preview_ready(stale)
        # 旧结果不误写：装载内容不是 0.9 的陈旧数据。
        assert panel._pcm is None or np.max(np.abs(panel._pcm)) != pytest.approx(0.9)

    def test_failure_keeps_last_good_preview(self, qapp) -> None:
        _editor, panel = _panel(qapp)
        panel._duration = 1.0
        panel.sync_audio_edit(_mute_edit(RATE))
        panel.attach_pcm(_pcm(), RATE)
        panel._refresh_stack_preview()
        good = panel._pcm
        # worker 失败：保留上一份有效预览，只留痕。
        panel._on_preview_failed(panel._preview_request_key, "boom")
        assert panel._pcm is good
        assert panel._preview_failure_note == "boom"
        assert "boom" in _editor._status.toolTip()


class TestPreviewWorker:
    def test_same_key_merged_and_finish_stops(self, qapp) -> None:
        from src.features.mixer.ui.clip_preview_worker import (
            ClipPreviewResult, ClipPreviewWorker,
        )

        worker = ClipPreviewWorker()
        runs = []

        def job():
            runs.append(1)
            return ClipPreviewResult(
                key=("k", (), None, 2048, 1, False), edited=_pcm(),
                sample_rate=RATE, duration=1.0,
                mins=np.zeros(8), maxs=np.zeros(8), rmss=np.zeros(8),
                spectrogram=[], report={})

        key = ("k", (), None, 2048, 1, False)
        worker.submit(key, job)
        worker.submit(key, job)   # 同 key 合并
        worker.submit(key, job)
        worker.finish()           # 可结束：停止前只跑一次
        worker.wait(3000)
        assert len(runs) <= 1
        assert worker.isRunning() is False

    def test_failed_job_reports_and_continues(self, qtbot) -> None:
        import threading

        from src.features.mixer.ui.clip_preview_worker import ClipPreviewWorker

        worker = ClipPreviewWorker()
        failures: list[tuple] = []
        worker.preview_failed.connect(lambda key, err: failures.append((key, err)))
        started = threading.Event()

        def bad():
            started.set()
            raise RuntimeError("dsp unavailable")

        worker.submit(("bad",), bad)
        assert started.wait(3.0)  # 任务已进入执行，finish 不再吞掉它
        qtbot.waitUntil(lambda: bool(failures))
        worker.finish()
        worker.wait(3000)
        assert failures and failures[0][0] == ("bad",)
        assert "dsp unavailable" in failures[0][1]

    def test_superseded_job_and_identical_inflight_requests(self, qtbot) -> None:
        import threading

        from src.features.mixer.ui.clip_preview_worker import ClipPreviewResult, ClipPreviewWorker

        worker = ClipPreviewWorker()
        entered = threading.Event()
        release = threading.Event()
        results = []
        calls = []
        worker.preview_ready.connect(lambda result: results.append(result.key))

        def result(key: tuple) -> ClipPreviewResult:
            return ClipPreviewResult(key, _pcm(), RATE, 1.0, [], [], [], [])

        def old_job() -> ClipPreviewResult:
            calls.append("old")
            entered.set()
            assert release.wait(3.0)
            return result(("old",))

        try:
            worker.submit(("old",), old_job)
            assert entered.wait(3.0)
            worker.submit(("old",), old_job)
            worker.submit(("new",), lambda: result(("new",)))
            release.set()
            qtbot.waitUntil(lambda: bool(results))
            assert calls == ["old"]
            assert results == [("new",)]
        finally:
            release.set()
            worker.finish()
            assert worker.wait(3000)


def test_preview_preserves_same_clip_selection_viewport_and_envelope(qapp) -> None:
    editor, panel = _panel(qapp)
    panel.load_audio_data(RATE, 1.0, [], [], [])
    panel.attach_pcm(_pcm(), RATE)
    panel.canvas.setSelection(0.2, 0.6)
    panel._sync_viewport(0.1, 0.8)
    panel.update_playhead(0.4)
    panel.canvas._env_points = [{"t": 0.3, "db": -4.0}]
    panel._refresh_stack_preview()
    assert panel.canvas.getSelection()[:2] == (0.2, 0.6)
    assert (panel.canvas._view_start, panel.canvas._view_end) == (0.1, 0.8)
    assert panel._playhead == 0.4
    assert panel.canvas._env_points == [{"t": 0.3, "db": -4.0}]
    editor.close()


def test_timeline_custom_fade_is_displayed_in_clip_editor(qapp) -> None:
    from src.features.edit.core.fade_curve_spec import fade_curve_from_midpoint
    from src.features.mixer.effects.panels.clip_editor import _display_fade_factor

    editor, panel = _panel(qapp)
    panel._duration = 1.0
    shape = fade_curve_from_midpoint(0.25)
    panel.sync_gain_fade(0.0, 0.2, 0.1, shape, "linear", locked=False)
    assert panel.canvas._fade_curve_type == shape
    assert panel.canvas._fade_out_curve_type == "Linear"
    assert _display_fade_factor(shape, 0.5) == pytest.approx(0.25)
    editor.close()
