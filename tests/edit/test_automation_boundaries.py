"""
自动化边界测试（RMA-07 阶段 B4）。

验收门槛（计划表 B4）：
- mute 关键帧在精确 sample 边界切换，并可选 1-5ms de-click ramp。
- 导出按 block 渲染，不能对整段 PCM 只评估一次（master 导出只读起始帧的 bug）。
- 中途 mute/ramp、Bus/Master 曲线在实时/离线准确且无 click。

核心 bug（RMA-07）：
- AudioGraphRenderer.apply_master 对整段 output 只读 start_frame 的 mute 值 →
  导出中途的 master mute keyframe 被整体忽略。
- 修复：mute automation 改为逐样本门控（get_value_ramp + >0.5 静音 + 边界 de-click）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer


SR = 48000.0
FPS = 24.0


def _keyframe_track(mute_keyframes):
    """构造一个含 mute keyframes 的 KeyframeTrack。"""
    from src.shared.keyframe import KeyframeTrack, Keyframe, InterpolationType
    track = KeyframeTrack(param_name="mute", min_val=0.0, max_val=1.0)
    for frame, value in mute_keyframes:
        track.add_keyframe(Keyframe(frame=frame, value=value,
                                    interpolation=InterpolationType.HOLD))
    return track


# ─────────────────────────────────────────────────────────────────────────────
# 1. apply_master：mute automation 逐样本门控（RMA-07 核心）
# ─────────────────────────────────────────────────────────────────────────────

class TestMasterMuteAutomationPerSample:
    """RMA-07: master mute automation 在导出全程逐样本评估，不再只读 start_frame。"""

    def _renderer_with_master_mute(self, mute_keyframes):
        """配置一个带 master mute automation 的 renderer。"""
        from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
        seq = TimelineSequence(audio_tracks=())
        renderer = AudioGraphRenderer(SR)
        renderer.configure_from_sequence(seq)
        renderer._master_automation = {"mute": _keyframe_track(mute_keyframes)}
        renderer._master_automation_mode = "read"
        return renderer

    def test_mute_mid_segment_is_silent(self):
        """10 秒素材，第 3 秒 mute、第 6 秒解除 → 3-6 秒静音，其余有声。

        帧 0-71 有声（0-3s），72-143 静音（3-6s），144-239 有声（6-10s）。
        旧 bug（只读 start_frame=0 → unmuted）会让整段有声；修复后 3-6s 静音。
        注意：第 0 帧需有 unmuted 关键帧，否则 KeyframeTrack 把首帧前的值当首帧值。
        """
        # fps=24: 3s=72帧, 6s=144帧；frame 0 = unmuted
        renderer = self._renderer_with_master_mute([(0, 0.0), (72, 1.0), (144, 0.0)])
        # 10 秒满幅 DC
        samples = np.full((2, int(SR * 10)), 0.5, dtype=np.float32)
        out = renderer.apply_master(samples, start_frame=0, fps=FPS)

        # 3-6 秒应静音
        seg_muted = out[:, int(SR * 4):int(SR * 5)]
        assert float(np.max(np.abs(seg_muted))) < 1e-3, \
            f"3-6s 应被 master mute automation 静音，peak={np.max(np.abs(seg_muted))}"
        # 0-3 秒有声
        seg_on = out[:, int(SR * 1):int(SR * 2)]
        assert float(np.max(np.abs(seg_on))) > 0.1, "0-3s 应有声"
        # 6-10 秒有声
        seg_on2 = out[:, int(SR * 8):int(SR * 9)]
        assert float(np.max(np.abs(seg_on2))) > 0.1, "6-10s 应有声（mute 解除）"

    def test_no_mute_automation_passes_full(self):
        """无 mute automation → 整段有声（与旧行为一致）。"""
        from src.features.edit.core.timeline_v2 import TimelineSequence
        seq = TimelineSequence(audio_tracks=())
        renderer = AudioGraphRenderer(SR)
        renderer.configure_from_sequence(seq)
        samples = np.full((2, int(SR * 2)), 0.5, dtype=np.float32)
        out = renderer.apply_master(samples, start_frame=0, fps=FPS)
        assert float(np.max(np.abs(out))) > 0.4

    def test_static_mute_silences_all(self):
        """静态 master_muted=True → 整段静音。"""
        from src.features.edit.core.timeline_v2 import TimelineSequence
        seq = TimelineSequence(audio_tracks=(), master_muted=True)
        renderer = AudioGraphRenderer(SR)
        renderer.configure_from_sequence(seq)
        samples = np.full((2, int(SR * 2)), 0.5, dtype=np.float32)
        out = renderer.apply_master(samples, start_frame=0, fps=FPS)
        assert float(np.max(np.abs(out))) < 1e-6

    def test_mute_boundary_no_click(self):
        """mute 边界（on↔off）处无超阈值 click（de-click ramp 平滑跳变）。

        检查 mute 起始帧附近的最大样本变化率受限（de-click ramp 使其平滑）。
        """
        # 1 秒 mute（24 帧）
        renderer = self._renderer_with_master_mute([(24, 1.0), (48, 0.0)])
        samples = np.full((2, int(SR * 3)), 0.5, dtype=np.float32)
        out = renderer.apply_master(samples, start_frame=0, fps=FPS)

        # 在 mute on 边界（24帧 = 1s = 48000 sample）附近，相邻样本差应被 ramp 限制。
        # 没有 de-click 时，边界处会从 0.5 瞬间跳到 0（差 0.5）；有 ramp 则渐变。
        boundary = int(SR * 1.0)  # 1s = mute on
        # 取边界前后各 50 样本的最大相邻差
        window = out[0, max(0, boundary - 50):boundary + 50]
        if len(window) > 1:
            max_jump = float(np.max(np.abs(np.diff(window))))
            # de-click ramp（1ms≈48 样本）使跳变分散，单步差应远小于 0.5
            assert max_jump < 0.3, f"mute 边界应有 de-click ramp，最大跳变 {max_jump}"


# ─────────────────────────────────────────────────────────────────────────────
# 2. _apply_declick_edges 单元测试
# ─────────────────────────────────────────────────────────────────────────────

class TestDeClickEdges:
    """RMA-07: _apply_declick_edges 在 0↔1 边界施加平滑 ramp。"""

    def test_no_change_passes_through(self):
        """全 1（无跳变）→ 原样返回。"""
        gate = np.ones(1000, dtype=np.float32)
        out = AudioGraphRenderer._apply_declick_edges(gate, SR)
        np.testing.assert_array_equal(out, gate)

    def test_abrupt_off_on_smoothed(self):
        """1→0→1 的跳变被平滑：边界附近不再是阶跃。"""
        gate = np.ones(3000, dtype=np.float32)
        gate[1000:2000] = 0.0  # 中段静音
        out = AudioGraphRenderer._apply_declick_edges(gate, SR)
        # 边界 1000（1→0）处：原 gate 从 1 跳到 0；平滑后应有渐变
        # 检查 1000 附近的最大相邻差 < 阶跃差（1.0）
        boundary = 1000
        window = out[max(0, boundary - 60):boundary + 60]
        max_jump = float(np.max(np.abs(np.diff(window))))
        assert max_jump < 0.5, f"边界应被平滑，最大跳变 {max_jump}（原阶跃 1.0）"

    def test_declick_preserves_steady_state(self):
        """de-click 只影响边界，远离边界的稳态区保持原值。"""
        gate = np.ones(5000, dtype=np.float32)
        gate[2000:3000] = 0.0
        out = AudioGraphRenderer._apply_declick_edges(gate, SR)
        # 稳态有声区（远离边界）
        assert abs(out[100] - 1.0) < 1e-5
        # 稳态静音区（中段，远离边界）
        assert abs(out[2500] - 0.0) < 1e-5


# ─────────────────────────────────────────────────────────────────────────────
# 3. volume automation 仍逐样本正确（回归保护）
# ─────────────────────────────────────────────────────────────────────────────

class TestMasterVolumeAutomationRegression:
    """RMA-07: volume automation 保持逐样本 ramp（修复 mute 时不能破坏 volume）。"""

    def test_volume_ramp_applied_across_segment(self):
        """volume automation 在整段逐样本应用（不是只读 start_frame）。"""
        from src.shared.keyframe import KeyframeTrack, Keyframe, InterpolationType
        from src.features.edit.core.timeline_v2 import TimelineSequence
        vol_track = KeyframeTrack(param_name="volume", min_val=0.0, max_val=2.0)
        vol_track.add_keyframe(Keyframe(frame=0, value=0.0,
                                        interpolation=InterpolationType.LINEAR))
        vol_track.add_keyframe(Keyframe(frame=240, value=1.0,
                                        interpolation=InterpolationType.LINEAR))
        seq = TimelineSequence(audio_tracks=())
        renderer = AudioGraphRenderer(SR)
        renderer.configure_from_sequence(seq)
        renderer._master_automation = {"volume": vol_track}
        renderer._master_automation_mode = "read"

        samples = np.full((2, int(SR * 10)), 0.5, dtype=np.float32)
        out = renderer.apply_master(samples, start_frame=0, fps=FPS)
        # 开头 volume≈0 → 接近静音；末尾 volume≈1 → 接近 0.5
        head = float(np.max(np.abs(out[:, :int(SR * 0.5)])))
        tail = float(np.max(np.abs(out[:, int(SR * 9):])))
        assert head < 0.05, f"开头 volume≈0 应静音，peak={head}"
        assert tail > 0.3, f"末尾 volume≈1 应有声，peak={tail}"
