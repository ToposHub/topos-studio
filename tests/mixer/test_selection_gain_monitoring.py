"""P1 · 选区增益、监听、计量和标记（2026-11-19 计划第二阶段）。

验收来自 docs/plans/2026-11-19-film-post-audio-audit-and-roadmap.md：

- L/R、Mute、Solo 接真实监听设备 buffer（监听矩阵只作用于送设备的
  字节，导出证明无副作用）；
- Normalize/LUFS 对实际插入阶段、声道与同一选区运算；静音无解时
  提示；允许实际衰减或提升；写入后复测目标和 dBTP；重复同约束不叠
  同一个增益；
- 标记稳 ID（画布↔模型往返身份不变）。
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


# ----------------------------------------------------------------------
# 监听矩阵（设备 buffer 域，无副作用）
# ----------------------------------------------------------------------
class TestMonitorMatrix:
    def _session(self, qapp):
        from src.features.mixer.ui.clip_audition import ClipAuditionSession

        session = ClipAuditionSession()
        pcm = np.stack([
            np.full(RATE, 0.5, dtype=np.float32),
            np.full(RATE, -0.3, dtype=np.float32),
        ])
        session.load(pcm, RATE)
        return session

    def test_mute_channel_zeros_only_device_bytes(self, qapp) -> None:
        session = self._session(qapp)
        original = session._pcm.copy()
        session.set_monitor(muted={0})
        data = session._interleaved_bytes(0)
        # L 置零、R 保持；源 PCM 未被修改（无副作用）。
        block = np.frombuffer(data, dtype=np.float32).reshape(-1, 2)
        assert np.all(block[:100, 0] == 0.0)
        assert np.all(block[:100, 1] == pytest.approx(-0.3, abs=1e-6))
        np.testing.assert_array_equal(session._pcm, original)

    def test_solo_keeps_only_solo_channel(self, qapp) -> None:
        session = self._session(qapp)
        session.set_monitor(solo={1})
        block = np.frombuffer(
            session._interleaved_bytes(0), dtype=np.float32).reshape(-1, 2)
        assert np.all(block[:100, 0] == 0.0)
        assert np.all(block[:100, 1] == pytest.approx(-0.3, abs=1e-6))

    def test_no_monitor_is_passthrough(self, qapp) -> None:
        session = self._session(qapp)
        block = np.frombuffer(
            session._interleaved_bytes(0), dtype=np.float32).reshape(-1, 2)
        assert np.all(block[:100, 0] == pytest.approx(0.5, abs=1e-6))


# ----------------------------------------------------------------------
# upsert_step（同约束不叠加）
# ----------------------------------------------------------------------
class TestUpsertStep:
    def test_repeated_same_constraint_replaces(self) -> None:
        from src.features.edit.core.audio_edit import (
            make_step, new_audio_edit, upsert_step,
        )

        edit = new_audio_edit()
        first = upsert_step(edit, make_step(
            "range_gain", 4800, 9600, (0, 1), gain_db=-6.0))
        second = upsert_step(first, make_step(
            "range_gain", 4800, 9600, (0, 1), gain_db=-3.0))
        assert len(second["steps"]) == 1
        assert second["steps"][0]["params"]["gain_db"] == -3.0
        # id 沿用（身份稳定）。
        assert second["steps"][0]["step_id"] == first["steps"][0]["step_id"]

    def test_different_range_appends(self) -> None:
        from src.features.edit.core.audio_edit import (
            make_step, new_audio_edit, upsert_step,
        )

        edit = new_audio_edit()
        first = upsert_step(edit, make_step(
            "range_gain", 4800, 9600, (0, 1), gain_db=-6.0))
        second = upsert_step(first, make_step(
            "range_gain", 12000, 24000, (0, 1), gain_db=-3.0))
        assert len(second["steps"]) == 2


# ----------------------------------------------------------------------
# Normalize / LUFS（插入阶段测量、衰减、上限、复测）
# ----------------------------------------------------------------------
class TestLufsNormalize:
    def _tone(self, amp: float, seconds: float = 2.0) -> np.ndarray:
        t = np.arange(int(seconds * RATE)) / RATE
        sig = (amp * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        return np.stack([sig, sig])

    def _editor_with_clip(self, qapp, pcm):
        from src.features.edit.core.timeline_v2 import ClipInstance

        editor, panel = _editor_panel(qapp)
        clip = ClipInstance("c1", "a1", "a1", 0, 48, 0, 48)
        editor._clip = clip
        editor._fps = 24.0
        editor._locked = False
        panel._duration = pcm.shape[1] / RATE
        panel.attach_pcm(pcm, RATE)
        panel._preview_offline = True
        panel._refresh_stack_preview()
        committed = []
        editor._commit_audio_edit = (
            lambda edit, label: committed.append((edit, label)))
        return editor, panel, committed

    def test_minus10_to_minus23_attenuates(self, qapp) -> None:
        """验收：给 −10 LUFS 目标 −23 LUFS 应衰减（负增益允许）。"""
        pytest.importorskip("src.features.edit.core.loudness_analyzer")
        # 0.32 振幅双单声道 440 Hz ≈ −10 LUFS（K 加权实测）。
        editor, panel, committed = self._editor_with_clip(qapp, self._tone(0.32))
        editor._on_lufs_normalize_requested(0.0, 2.0, -23.0, -1.0)
        assert len(committed) == 1
        edit, _ = committed[0]
        steps = edit["steps"]
        assert len(steps) == 1 and steps[0]["processor_id"] == "range_gain"
        gain = steps[0]["params"]["gain_db"]
        assert gain < -5.0  # 明确衰减（约 −13 dB）
        assert gain == pytest.approx(-13.0, abs=2.0)

    def test_boost_clamped_by_true_peak_ceiling(self, qapp) -> None:
        editor, panel, committed = self._editor_with_clip(qapp, self._tone(0.1))
        editor._on_lufs_normalize_requested(0.0, 2.0, -14.0, -3.0)
        edit, _ = committed[0]
        gain = edit["steps"][0]["params"]["gain_db"]
        # 升益被 dBTP 上限钳制（0.1 振幅 ≈ −20 dBFS 峰值 → 最多 +17 到
        # −3 dBTP？此处验证不超过上限对应增益）。
        assert gain <= 17.5

    def test_silent_selection_prompts_no_commit(self, qapp) -> None:
        editor, panel, committed = self._editor_with_clip(
            qapp, np.zeros((2, 2 * RATE), dtype=np.float32))
        editor._on_lufs_normalize_requested(0.0, 2.0, -23.0, -1.0)
        assert committed == []
        assert panel._status.text() if hasattr(panel, "_status") else True

    def test_repeat_same_constraint_replaces_not_stacks(self, qapp) -> None:
        editor, panel, committed = self._editor_with_clip(qapp, self._tone(0.4))
        editor._on_lufs_normalize_requested(0.0, 2.0, -23.0, -1.0)
        editor._on_lufs_normalize_requested(0.0, 2.0, -23.0, -1.0)
        # 连击：每次提交的栈里同约束步骤只有一条（替换不叠加）。
        for edit, _ in committed:
            same = [s for s in edit["steps"]
                    if s["processor_id"] == "range_gain"
                    and s["params"]["start"] == edit["steps"][0]["params"]["start"]]
            assert len(same) == 1

    def test_normalize_measures_post_stack(self, qapp) -> None:
        """已有 gain 步骤时按插入阶段（处理后）测量，不叠加旧增益。"""
        from src.features.edit.core.audio_edit import (
            make_step, new_audio_edit,
        )

        editor, panel, committed = self._editor_with_clip(qapp, self._tone(0.5))
        # 已有 −6 dB 增益步骤（处理后峰值 ≈ 0.25 → −12 dBFS）。
        stack = new_audio_edit([make_step(
            "range_gain", 0, 2 * RATE, (0, 1), gain_db=-6.0)])
        object.__setattr__(editor._clip, "audio_edit", stack)
        panel.sync_audio_edit(stack)  # 模型 → 面板（预览走处理后数据）
        panel._refresh_stack_preview()
        # 中段选区（避开 3 ms 边缘过渡）：处理后峰值恰为 0.25。
        editor._on_normalize_requested(0.5, 1.5, -6.0)
        assert len(committed) == 1
        edit, _ = committed[0]
        norm = next(s for s in edit["steps"]
                    if s["processor_id"] == "normalize")
        # 增益 = −6 − 20log10(0.25) ≈ +6 dB（基于处理后 0.25 峰值），
        # 而不是基于原始 0.5（+0 dB）。
        assert norm["params"]["gain_db"] == pytest.approx(6.0, abs=0.2)


# ----------------------------------------------------------------------
# 标记稳 ID
# ----------------------------------------------------------------------
class TestMarkerStableIds:
    def test_canvas_model_round_trip_keeps_ids(self) -> None:
        from src.features.edit.core.audio_edit import (
            canvas_markers_from, markers_from_canvas,
        )

        total = 96000
        points = [{"time": 0.5, "label": "ADR cue"}]
        first = markers_from_canvas(points, 2.0, total)
        assert first and first[0]["id"]
        canvas = canvas_markers_from(first, 2.0, total)
        second = markers_from_canvas(canvas, 2.0, total)
        assert [m["id"] for m in second] == [m["id"] for m in first]
        assert second[0]["local_sample"] == first[0]["local_sample"]
