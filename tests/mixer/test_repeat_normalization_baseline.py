"""P0-1 · 重复标准化/响度匹配的基准、身份与实测复测。

验收来自 docs/plans/2026-11-21-mixer-clip-editor-backend-handoff.md：

- 重复提交（改目标）以**替换**语义保持单一系数：基准 = 渲染到目标
  步骤插入点（移除被替换系数），三份素材改第二次目标后均正确；
- 稳定操作身份：手动选区增益 / 峰值标准化 / LUFS 匹配三类并存、
  互不替换；旧工程无 operation 的响度步骤按迁移规则识别并沿用
  step ID 与排序；
- 峰值测量乘候选步骤的 unity 增益包络（边缘过渡不污染落点）；
- 提交后用同一渲染路径复测并回报实测值（LUFS 误差 ≤0.2 LU）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

RATE = 48000


def _editor_panel(qapp):
    from src.features.mixer.ui.project_clip_editor import ProjectClipEditor

    editor = ProjectClipEditor()
    panel = editor._ensure_panel()
    return editor, panel


def _editor_with_clip(qapp, pcm):
    from src.features.edit.core.timeline_v2 import ClipInstance

    editor, panel = _editor_panel(qapp)
    clip = ClipInstance("c1", "a1", "a1", 0, 48, 0, 48)
    editor._clip = clip
    editor._fps = 24.0
    editor._locked = False
    panel._duration = pcm.shape[1] / RATE
    panel.attach_pcm(pcm, RATE)
    panel._preview_offline = True
    panel.set_stack_editable_hint(True)
    committed = []
    editor._commit_audio_edit = (
        lambda edit, label: committed.append((edit, label)))
    return editor, panel, committed


def _apply_stack(editor, edit):
    """模拟命令落地：模型与面板同步新栈（预览走处理后数据）。"""
    object.__setattr__(editor._clip, "audio_edit", edit)
    editor._panel.sync_audio_edit(edit)


def _render(pcm, edit):
    from src.features.edit.core.audio_edit import render_audio_edit_full

    return render_audio_edit_full(pcm, edit, sample_rate=RATE)


def _peak_db(segment: np.ndarray) -> float:
    peak = float(np.abs(segment).max())
    assert peak > 1e-9
    return 20.0 * float(np.log10(peak))


def _constant(amp: float, seconds: float = 2.0) -> np.ndarray:
    sig = np.full(int(seconds * RATE), amp, dtype=np.float32)
    return np.stack([sig, sig])


def _tone(amp: float, seconds: float = 2.0) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    sig = (amp * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    return np.stack([sig, sig])


def _capture_status(panel) -> list[str]:
    """捕获状态行消息（info_label 只转发 set_status_message）。"""
    messages: list[str] = []
    original = panel.set_status_message

    def _capture(text, *_args, **_kwargs):
        messages.append(str(text))
        return original(text, *_args, **_kwargs)

    panel.set_status_message = _capture
    return messages


class TestRepeatedNormalize:
    def test_uses_rack_index_when_earlier_steps_are_bypassed(self, qapp) -> None:
        from src.features.edit.core.audio_edit import make_step, new_audio_edit

        pcm = _tone(0.25)
        editor, panel, committed = _editor_with_clip(qapp, pcm)
        steps = [
            make_step("range_gain", 0, 2 * RATE, (0, 1), gain_db=12.0, enabled=False),
            make_step("normalize", 0, 2 * RATE, (0, 1), gain_db=6.0),
        ]
        _apply_stack(editor, new_audio_edit(steps))
        editor._on_normalize_requested(0.0, 2.0, -3.0)
        candidate = committed[0][0]
        assert candidate["steps"][0] == steps[0]
        assert candidate["steps"][1]["step_id"] == steps[1]["step_id"]
        assert _peak_db(_render(pcm, candidate)) == pytest.approx(-3.0, abs=0.15)

    def test_new_request_cancels_analysis_before_it_can_write(self, qapp, qtbot, monkeypatch) -> None:
        import threading

        from src.features.mixer.ui import project_clip_editor

        editor, panel, committed = _editor_with_clip(qapp, _tone(0.25))
        qtbot.addWidget(editor)
        panel._preview_offline = False
        entered = threading.Event()
        cancellation_seen = threading.Event()
        original = project_clip_editor.measure_gain_edit

        def wait_on_first(request, cancelled):
            if request.target == -6.0:
                assert threading.current_thread() is not threading.main_thread()
                entered.set()
                while not cancelled():
                    cancellation_seen.wait(0.01)
                cancellation_seen.set()
                return None
            return original(request, cancelled)

        monkeypatch.setattr(project_clip_editor, "measure_gain_edit", wait_on_first)
        try:
            editor._on_normalize_requested(0.0, 2.0, -6.0)
            assert entered.wait(2.0)
            editor._on_normalize_requested(0.0, 2.0, -3.0)
            qtbot.waitUntil(lambda: bool(committed), timeout=5000)
            assert cancellation_seen.is_set()
            assert len(committed) == 1
            assert _peak_db(_render(panel._raw_pcm, committed[0][0])) == pytest.approx(-3.0, abs=0.15)
        finally:
            editor.close()

    def test_constant_retargets_exactly(self, qapp) -> None:
        """恒定 0.25 依次 −6→−3→−12 dBFS：每次落点误差 ≤0.15 dBFS。"""
        pcm = _constant(0.25)
        editor, panel, committed = _editor_with_clip(qapp, pcm)
        first_id = None
        for target in (-6.0, -3.0, -12.0):
            editor._on_normalize_requested(0.0, 2.0, target)
            assert len(committed) == 1  # 替换不叠加：始终只有一条
            edit, _ = committed.pop()
            steps = [s for s in edit["steps"]
                     if s["processor_id"] == "normalize"]
            assert len(steps) == 1
            assert steps[0]["params"]["operation"] == "normalize_peak"
            if first_id is None:
                first_id = steps[0]["step_id"]
            assert steps[0]["step_id"] == first_id  # 稳 ID
            _apply_stack(editor, edit)
            measured = _peak_db(_render(pcm, edit))
            assert measured == pytest.approx(target, abs=0.15)

    def test_sine_retargets_exactly(self, qapp) -> None:
        pcm = _tone(0.5)
        editor, panel, committed = _editor_with_clip(qapp, pcm)
        fade = 3 * RATE // 1000  # 3 ms 干湿过渡带（读数与复测同语义排除）
        for target in (-9.0, -4.5, -12.0):
            editor._on_normalize_requested(0.25, 1.75, target)
            edit, _ = committed.pop()
            _apply_stack(editor, edit)
            full = _render(pcm, edit)
            measured = _peak_db(full[:, 12000 + fade:84000 - fade])
            assert measured == pytest.approx(target, abs=0.15)

    def test_whole_clip_after_prior_gain_lands_exactly(self, qapp) -> None:
        """整片选区 + 既有整片 −6 dB 步骤：内部落点精确（插值点目标）。

        边缘 3 ms 是防爆音的干湿过渡带，同区间堆叠两个包络时过渡带
        样本可短暂超过内部电平——插入点目标语义只保证内部（选区主体）
        落点；全片段读数由复测回报呈现。
        """
        from src.features.edit.core.audio_edit import make_step, new_audio_edit

        pcm = _tone(0.5)
        editor, panel, committed = _editor_with_clip(qapp, pcm)
        stack = new_audio_edit([make_step(
            "range_gain", 0, 2 * RATE, (0, 1), gain_db=-6.0,
            operation="selection_gain")])
        _apply_stack(editor, stack)
        editor._on_normalize_requested(0.0, 2.0, -6.0)
        edit, _ = committed.pop()
        full = _render(pcm, edit)
        fade = 3 * RATE // 1000  # 3 ms 过渡带
        measured = _peak_db(full[:, fade:2 * RATE - fade])
        assert measured == pytest.approx(-6.0, abs=0.15)

    def test_normalize_measured_reported(self, qapp) -> None:
        pcm = _constant(0.25)
        editor, panel, committed = _editor_with_clip(qapp, pcm)
        messages = _capture_status(panel)
        editor._on_normalize_requested(0.0, 2.0, -6.0)
        assert committed
        assert any("-6" in m or "−6" in m for m in messages)


class TestRepeatedLoudnessMatch:
    def test_retarget_lands_within_lu(self, qapp) -> None:
        """−23 → −16 改目标：替换单一响度步骤，实测误差 ≤0.2 LU。"""
        from src.features.edit.core.loudness_analyzer import analyze_loudness

        pcm = _tone(0.4)
        editor, panel, committed = _editor_with_clip(qapp, pcm)
        first_id = None
        for target in (-23.0, -16.0):
            editor._on_lufs_normalize_requested(0.0, 2.0, target, -1.0)
            assert len(committed) == 1
            edit, _ = committed.pop()
            steps = [s for s in edit["steps"]
                     if s["processor_id"] == "range_gain"]
            assert len(steps) == 1
            assert steps[0]["params"]["operation"] == "loudness_match"
            if first_id is None:
                first_id = steps[0]["step_id"]
            assert steps[0]["step_id"] == first_id
            _apply_stack(editor, edit)
            full = _render(pcm, edit)
            report = analyze_loudness(full, float(RATE))
            assert float(report.integrated_lufs) == pytest.approx(
                target, abs=0.2)

    def test_legacy_step_migrated_and_replaced(self, qapp) -> None:
        """旧工程 range_gain + "Loudness match" 标签：迁移身份并替换。"""
        from src.features.edit.core.audio_edit import make_step, new_audio_edit

        pcm = _tone(0.4)
        editor, panel, committed = _editor_with_clip(qapp, pcm)
        legacy = new_audio_edit([make_step(
            "range_gain", 0, 2 * RATE, (0, 1), gain_db=-2.0,
            label="Loudness match")])  # 无 operation（旧工程格式）
        _apply_stack(editor, legacy)
        legacy_id = legacy["steps"][0]["step_id"]
        editor._on_lufs_normalize_requested(0.0, 2.0, -23.0, -1.0)
        edit, _ = committed.pop()
        steps = edit["steps"]
        assert len(steps) == 1  # 替换而非叠加
        assert steps[0]["params"]["operation"] == "loudness_match"
        assert steps[0]["step_id"] == legacy_id  # 沿用旧 step ID

    def test_legacy_normalize_migrated(self) -> None:
        from src.features.edit.core.audio_edit import (
            OPERATION_NORMALIZE_PEAK,
            find_measurement_step_index,
            make_step,
            new_audio_edit,
            tag_legacy_measurement_operations,
        )

        legacy = new_audio_edit([make_step(
            "normalize", 0, 48000, (0, 1), gain_db=3.0)])
        migrated = tag_legacy_measurement_operations(legacy)
        assert (migrated["steps"][0]["params"]["operation"]
                == OPERATION_NORMALIZE_PEAK)
        assert find_measurement_step_index(
            migrated, processor_id="normalize",
            operation=OPERATION_NORMALIZE_PEAK,
            start=0, end=48000, channels=(0, 1)) == 0
        # 其他历史 range_gain（无标签/其他标签）不迁移。
        other = new_audio_edit([make_step(
            "range_gain", 0, 48000, (0, 1), gain_db=1.0)])
        assert tag_legacy_measurement_operations(other) == other


class TestOperationCoexistence:
    def test_selection_gain_normalize_lufs_coexist(self, qapp) -> None:
        """同区间三类操作并存：手动增益不被响度匹配替换。"""
        pcm = _tone(0.5)
        editor, panel, committed = _editor_with_clip(qapp, pcm)
        editor._on_selection_gain_requested(0.0, 2.0, -6.0, -1)
        edit, _ = committed.pop()
        _apply_stack(editor, edit)
        editor._on_normalize_requested(0.0, 2.0, -6.0)
        edit, _ = committed.pop()
        _apply_stack(editor, edit)
        editor._on_lufs_normalize_requested(0.0, 2.0, -23.0, -1.0)
        edit, _ = committed.pop()
        kinds = sorted(
            (s["processor_id"], s["params"].get("operation"))
            for s in edit["steps"])
        assert kinds == [
            ("normalize", "normalize_peak"),
            ("range_gain", "loudness_match"),
            ("range_gain", "selection_gain"),
        ]


class TestInsertionPointBaseline:
    def test_render_to_insertion_removes_replaced_coefficient(self) -> None:
        """插入点基准：替换时移除被替换系数，保留更早步骤。"""
        from src.features.edit.core.audio_edit import (
            OPERATION_NORMALIZE_PEAK,
            find_measurement_step_index,
            make_step,
            new_audio_edit,
            render_to_step_insertion,
        )

        pcm = _constant(0.5)
        edit = new_audio_edit([
            make_step("range_gain", 0, RATE, (0, 1), gain_db=-6.0,
                      operation="selection_gain"),
            make_step("normalize", 0, RATE, (0, 1), gain_db=6.0,
                      operation=OPERATION_NORMALIZE_PEAK),
        ])
        idx = find_measurement_step_index(
            edit, processor_id="normalize", operation=OPERATION_NORMALIZE_PEAK,
            start=0, end=RATE, channels=(0, 1))
        assert idx == 1
        baseline = render_to_step_insertion(
            pcm, edit, insertion_index=idx, sample_rate=RATE)
        # 只有 selection_gain(−6 dB) 生效：normalize 系数被移除。
        assert float(np.abs(baseline[:, 400:44000]).max()) == pytest.approx(
            0.5 * 10 ** (-6 / 20), abs=1e-4)

    def test_selection_maps_through_length_step(self) -> None:
        """改长度步骤后的选区逆映射到插入点域。"""
        from src.features.edit.core.audio_edit import (
            map_selection_to_insertion, make_step, new_audio_edit,
        )

        # time_stretch 2×：[48000,96000) → 输出 [48000,72000)。
        edit = new_audio_edit([
            make_step("range_gain", 0, RATE, (0, 1), gain_db=-6.0,
                      operation="selection_gain"),
            make_step("time_stretch", 48000, 96000, (0, 1),
                      ratio_pct=200.0, output_length=24000),
        ])
        # 最终域 [48000, 72000) 对应插入点（步骤 1 前）域 [48000, 96000)。
        lo, hi = map_selection_to_insertion(edit, 48000, 72000, 1)
        assert lo == 48000 and hi == 96000

    def test_render_full_matches_preview_pipeline(self) -> None:
        """render_audio_edit_full 与预览管线同一语义（前缀+后缀+包络）。"""
        from src.features.edit.core.audio_edit import (
            apply_audio_edit_to_pcm, make_step, new_audio_edit,
            render_audio_edit_full, render_pre_render_steps,
            requires_pre_render, suffix_audio_edit,
        )

        pcm = _tone(0.5)
        edit = new_audio_edit([
            make_step("mute", 0, 24000, (0, 1)),
            make_step("normalize", 48000, 96000, (0, 1), gain_db=2.0,
                      operation="normalize_peak"),
        ])
        assert not requires_pre_render(edit)
        full = render_audio_edit_full(pcm, edit, sample_rate=RATE)
        ref = apply_audio_edit_to_pcm(
            pcm, suffix_audio_edit(edit), sample_rate=RATE)
        np.testing.assert_array_equal(full, ref)
        assert render_pre_render_steps(pcm, edit, sample_rate=RATE) is pcm


class TestVerifyReporting:
    def test_normalize_reports_measured_peak(self, qapp) -> None:
        pcm = _constant(0.25)
        editor, panel, committed = _editor_with_clip(qapp, pcm)
        messages = _capture_status(panel)
        editor._on_normalize_requested(0.0, 2.0, -6.0)
        edit, _ = committed.pop()
        assert any("-6" in m or "−6" in m for m in messages)

    def test_measurement_envelope_excludes_edge_ramp(self) -> None:
        from src.features.edit.core.audio_edit import (
            selection_measurement_envelope,
        )

        env = selection_measurement_envelope(
            RATE, 0, RATE, sample_rate=RATE)
        assert env is not None
        assert env[0] == pytest.approx(0.0)
        assert env[RATE // 2] == pytest.approx(1.0)
        assert env[RATE - 1] < 0.02  # 尾端过渡（半开区间不含 env=0 样本）
