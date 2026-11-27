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
            mins=[], maxs=[], rmss=[], report={})
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
                mins=[], maxs=[], rmss=[], report={})

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
            return ClipPreviewResult(key, _pcm(), RATE, 1.0, [], [], [])

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


def test_live_envelope_scales_display_only_until_processed_preview_arrives(qapp) -> None:
    from src.features.edit.core.audio_edit import envelope_from_canvas_points, set_envelope

    editor, panel = _panel(qapp)
    panel.set_source_mode(False)
    panel._duration = 1.0

    def curve(level: float) -> dict:
        return set_envelope(None, envelope_from_canvas_points(
            [{"t": 0.0, "db": -6.0}, {"t": 1.0, "db": level}], 1.0, RATE,
        ))

    panel.sync_audio_edit(curve(-6.0), RATE)
    panel.attach_pcm(_pcm(), RATE)
    panel._refresh_stack_preview()
    canvas = panel.canvas
    baked_pcm = panel._pcm
    assert canvas._waveform_envelope_override() is None
    canvas._env_points[1]["db"] = -18.0  # Mouse drag updates this temporary projection.
    live = canvas._waveform_envelope_override()
    assert live is not None
    ratio = canvas._waveform_envelope_ratio(np.array([0.0, 0.25, 0.5, 1.0]), live)
    np.testing.assert_allclose(ratio, 10 ** (np.array([0.0, -1.875, -6.0, -12.0]) / 20.0))
    assert panel._pcm is baked_pcm

    panel.sync_audio_edit(curve(-18.0), RATE)
    assert panel._pcm is not baked_pcm
    assert canvas._waveform_envelope_override() is None
    panel.btn_bypass.setChecked(True)
    assert canvas._waveform_envelope_override() is None
    assert canvas._waveform_baked_envelope is None
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


def test_hidden_spectrum_skips_fft_and_reuses_the_published_audition(qtbot, monkeypatch) -> None:
    from src.features.mixer.ui import project_clip_editor

    panel = project_clip_editor._TimelineClipPanel()
    qtbot.addWidget(panel)
    panel.resize(1100, 600)
    panel.show()
    real_analyze = project_clip_editor.analyze_spectrogram
    analyses = []

    def analyze(*args, **kwargs):
        analyses.append(args[0])
        return real_analyze(*args, **kwargs)

    monkeypatch.setattr(project_clip_editor, "analyze_spectrogram", analyze)
    panel._duration = 1.0
    panel.sync_audio_edit(_mute_edit(RATE))
    panel.attach_pcm(_pcm(), RATE)
    panel._refresh_stack_preview()
    try:
        qtbot.waitUntil(lambda: panel._last_preview is not None, timeout=4000)
        audition = panel._pcm
        assert np.max(np.abs(audition[:, 500:-500])) == 0.0
        assert panel.btn_play.isEnabled()
        assert panel.canvas._spectrogram is None and not analyses

        panel.btn_spectral.setChecked(True)
        qtbot.waitUntil(lambda: panel.canvas._spectrogram is not None, timeout=4000)
        overview = panel.canvas._spectrogram
        assert len(analyses) == 1 and analyses[0] is audition
        panel.btn_spectral.setChecked(False)
        panel.btn_spectral.setChecked(True)
        panel._request_spectrum()
        assert panel.canvas._spectrogram is overview
        assert len(analyses) == 1
        assert panel._pcm is audition

        panel.btn_spectral.setChecked(False)
        panel.btn_bypass.setChecked(True)
        qtbot.waitUntil(lambda: panel._last_preview.key == panel._preview_request_key, timeout=4000)
        assert panel.canvas._spectrogram is None and len(analyses) == 1
        np.testing.assert_array_equal(panel._pcm, _pcm())
    finally:
        panel.shutdown_preview()


@pytest.mark.parametrize("rate", [44100, 48000])
def test_selection_processor_auditions_only_the_selected_range(qapp, monkeypatch, rate: int) -> None:
    from src.features.edit.core.effect_registry import create_effect_instance
    from src.features.edit.core.timeline_v2 import ClipInstance
    from src.features.mixer.ui import project_clip_editor

    editor, panel = _panel(qapp)
    saturation = create_effect_instance("saturation").with_params({"drive": 36.0})
    editor._clip = ClipInstance(clip_id="selection", asset_id="audio", track_id="track",
                                timeline_duration=24, source_duration=24, effects=(saturation,))
    t = np.arange(rate) / rate
    pcm = np.asarray([.2 * np.sin(2 * np.pi * 200 * t), .12 * np.sin(2 * np.pi * 720 * t)], dtype=np.float32)
    panel.load_audio_data(rate, 1., [], [], [])
    panel.set_clip_gain_fade((-12.0, 0, 0, 24000, None, "linear", "linear", 24.0, 24))
    panel.set_clip_effects(editor._clip, 24.0)
    panel.attach_pcm(pcm, rate)
    panel._refresh_stack_preview()
    dry = panel._pcm
    panel.canvas.setSelection(.25, .625)
    heard: list[tuple[int, np.ndarray]] = []
    committed = []

    def audition(start: int, buffer: np.ndarray) -> bool:
        heard.append((start, buffer))
        return True

    monkeypatch.setattr(panel, "start_band_preview", audition)
    actual_render = project_clip_editor.render_pre_render_steps
    rendered = []

    def render(*args, **kwargs):
        rendered.append(args[1])
        return actual_render(*args, **kwargs)

    monkeypatch.setattr(project_clip_editor, "render_pre_render_steps", render)
    editor.audio_edit_committed.connect(lambda *args: committed.append(args))
    editor.open_selection_effect("doubler")
    dialog = editor._selection_effect_dialog
    assert dialog.preview.isEnabled()
    dialog.preview.click()
    assert dialog.status.text() == ""
    dialog.delta.click()
    assert len(heard) == 2 and not committed
    dialog.apply.click()
    assert len(committed) == 1 and committed[0][0] == "selection"
    assert len(rendered) == 1  # a single candidate render across all three actions
    assert editor._selection_effect_dialog is None
    panel.sync_audio_edit(committed[0][1], RATE)
    wet = panel._pcm
    start, stop = round(rate * .25), round(rate * .625)
    assert heard[0][0] == heard[1][0] == start
    assert heard[0][1].flags.c_contiguous
    np.testing.assert_array_equal(heard[0][1], wet[:, start:stop])
    np.testing.assert_array_equal(heard[1][1], dry[:, start:stop] - wet[:, start:stop])
    assert np.max(np.abs(heard[1][1])) > .001
    editor.close()


@pytest.mark.parametrize("change", ["parameter", "range", "close"])
def test_selection_effect_cancels_stale_apply(qtbot, monkeypatch, change: str) -> None:
    from threading import Event

    from src.features.edit.core.timeline_v2 import ClipInstance
    from src.features.mixer.ui import project_clip_editor

    editor, panel = _panel(qtbot)
    editor._clip = ClipInstance(clip_id="selection", asset_id="audio", track_id="track",
                                timeline_duration=24, source_duration=24)
    panel.load_audio_data(RATE, 1., [], [], [])
    panel.attach_pcm(_pcm(), RATE)
    panel._preview_offline = False
    started, cancelled_in_worker, release = Event(), Event(), Event()

    def render(pcm, _candidate, *, sample_rate, cancel_check):
        started.set()
        while not release.wait(.01):
            if cancel_check():
                cancelled_in_worker.set()
                break
        return pcm

    monkeypatch.setattr(project_clip_editor, "render_pre_render_steps", render)
    committed = []
    editor.audio_edit_committed.connect(lambda *args: committed.append(args))
    editor.open_selection_effect("pitch_correction")
    dialog = editor._selection_effect_dialog
    try:
        dialog.apply.click()
        qtbot.waitUntil(started.is_set, timeout=4000)
        assert dialog.stop.isEnabled()
        if change == "parameter":
            edit = dialog.retune.lineEdit()
            edit.setFocus()
            edit.selectAll()
            qtbot.keyClicks(edit, "125")
            assert dialog.retune.value() == 45.0  # the draft changes before the spin value settles
        elif change == "range":
            panel.canvas.setSelection(.2, .6)
        else:
            dialog.close()
        qtbot.waitUntil(cancelled_in_worker.is_set, timeout=4000)
        assert not committed
    finally:
        release.set()
        editor.close()


def test_frequency_band_audition_handles_audio_shorter_than_filter_padding() -> None:
    from src.features.mixer.ui.clip_analysis import build_band_preview

    pcm = _pcm(samples=16)
    output = build_band_preview(pcm, RATE, 1, 15, 300., 1000.)
    assert output.shape == (2, 14)
    assert np.isfinite(output).all()


def test_loudness_fields_clear_when_the_new_preview_has_no_measurement(qapp, monkeypatch) -> None:
    editor, panel = _panel(qapp)
    report = {"integrated_lufs": -22.0, "true_peak_dbtp": -1.5}
    monkeypatch.setattr(panel, "_measure_loudness", lambda *args: dict(report))
    panel._duration = 1.0
    panel.attach_pcm(_pcm(), RATE)
    panel._refresh_stack_preview()
    assert panel.lufs_val_lbl.text() == "-22.0 LUFS"
    assert panel.true_peak_lbl.text() == "-1.5 dBTP"

    report.clear()
    panel.attach_pcm(_pcm(.12), RATE)
    panel._refresh_stack_preview()
    panel._set_target_lufs(-18)
    assert panel.lufs_val_lbl.text() == panel.true_peak_lbl.text() == "—"
    editor.close()


def test_audition_is_ready_before_background_loudness_scan(qtbot, monkeypatch) -> None:
    import threading

    from src.features.mixer.ui.project_clip_editor import _TimelineClipPanel

    panel = _TimelineClipPanel()
    qtbot.addWidget(panel)
    panel.resize(1100, 600)
    panel.show()
    started, finish = threading.Event(), threading.Event()

    def measure(pcm: np.ndarray, _rate: int, _cancelled) -> dict:
        assert np.max(np.abs(pcm[:, 500:-500])) == 0.0
        started.set()
        assert finish.wait(3)
        return {"integrated_lufs": -31.0, "true_peak_dbtp": -13.0}

    monkeypatch.setattr(panel, "_measure_loudness", measure)
    panel._duration = 1.0
    panel.sync_audio_edit(_mute_edit(RATE))
    panel.attach_pcm(_pcm(), RATE)
    panel._refresh_stack_preview()
    try:
        qtbot.waitUntil(started.is_set, timeout=3000)
        assert panel.btn_play.isEnabled()
        assert panel.canvas._waveform_samples is panel._pcm
        assert panel.lufs_val_lbl.text() == "—"
        finish.set()
        qtbot.waitUntil(lambda: panel.lufs_val_lbl.text() == "-31.0 LUFS", timeout=3000)
        assert panel.true_peak_lbl.text() == "-13.0 dBTP"
    finally:
        finish.set()
        panel.shutdown_preview()


def test_switching_back_to_cached_audition_clears_preview_failure(qapp, monkeypatch) -> None:
    editor, panel = _panel(qapp)
    panel._duration = 1.0
    panel.sync_audio_edit(_mute_edit(RATE))
    panel.attach_pcm(_pcm(), RATE)
    panel._refresh_stack_preview()
    good = panel._pcm

    def fail(_inputs: dict) -> None:
        raise RuntimeError("preview processing failed")

    monkeypatch.setattr(panel, "_compute_preview", fail)
    panel.btn_bypass.setChecked(True)
    assert panel._preview_failure_note and editor._status.text()
    panel.btn_bypass.setChecked(False)
    assert panel._pcm is good
    assert panel._preview_failure_note == editor._status.text() == ""
    editor.close()


def test_failed_analysis_for_a_stale_selection_leaves_current_status(qtbot, monkeypatch) -> None:
    import threading

    from src.features.mixer.ui.project_clip_editor import ProjectClipEditor

    editor = ProjectClipEditor()
    qtbot.addWidget(editor)
    editor._ensure_panel()
    started, finish = threading.Event(), threading.Event()
    still_current = True
    messages: list[str] = []
    monkeypatch.setattr(editor, "_report_analysis_failure", messages.append)

    def fail(_cancelled) -> None:
        started.set()
        assert finish.wait(3)
        raise RuntimeError("failed after selection changed")

    try:
        editor._submit_analysis(
            fail, lambda _result: None, current=lambda: still_current, busy_message="Processing selection…",
        )
        qtbot.waitUntil(started.is_set, timeout=3000)
        assert editor._status.accessibleName() == "Processing selection…"
        still_current = False
        finish.set()
        qtbot.waitUntil(lambda: editor._analysis_callback is None, timeout=3000)
        assert messages == []
        assert editor._status.accessibleName() == ""
    finally:
        finish.set()
        editor.close()
