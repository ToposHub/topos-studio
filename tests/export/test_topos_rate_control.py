"""P5-T6：topos_rate_control 专测。

ToposRateFeedback（P4 帧级码控）公共 API 覆盖：
- 构造参数校验（target/预算成对/正数）
- seed / note / anchored / stats / telemetry
- active / over_budget 属性
- 切换帧检测（reseed_hint）、qp 钳位、确定性（spec §11.1）

纯逻辑模块，无 I/O、无真实编码器。
"""

from __future__ import annotations

import pytest

from src.shared.export.topos_rate_control import ToposRateFeedback


class TestConstruction:
    def test_rejects_nonpositive_target(self) -> None:
        with pytest.raises(ValueError, match="target_bytes"):
            ToposRateFeedback(target_bytes=0)

    def test_rejects_negative_target(self) -> None:
        with pytest.raises(ValueError):
            ToposRateFeedback(target_bytes=-100)

    def test_rejects_half_configured_budget(self) -> None:
        with pytest.raises(ValueError, match="预算模式"):
            ToposRateFeedback(target_bytes=1000, total_budget_bytes=10000)

    @pytest.mark.parametrize("budget,frames", [
        (0, 10), (-1, 10), (10000, 0), (10000, -5),
    ])
    def test_rejects_nonpositive_budget_args(
            self, budget: int, frames: int) -> None:
        with pytest.raises(ValueError):
            ToposRateFeedback(target_bytes=1000,
                              total_budget_bytes=budget,
                              total_frames=frames)

    def test_window_floored_to_two(self) -> None:
        fb = ToposRateFeedback(target_bytes=100, window=0)
        assert fb.window == 2

    def test_fresh_instance_not_active(self) -> None:
        fb = ToposRateFeedback(target_bytes=100)
        assert fb.active is False
        assert fb.over_budget is False


class TestSeedAndNote:
    def test_seed_activates_and_accounts_first_packet(self) -> None:
        fb = ToposRateFeedback(target_bytes=1000)
        fb.seed(qp_used=30, packet_size=1200)
        assert fb.active is True
        assert fb.qp == 30
        assert fb.frames == 1
        assert fb.total == 1200

    def test_seed_clamps_qp_into_bounds(self) -> None:
        fb = ToposRateFeedback(target_bytes=100, qp_min=5, qp_max=50)
        fb.seed(qp_used=200, packet_size=100)
        assert fb.qp == 50
        fb2 = ToposRateFeedback(target_bytes=100, qp_min=5, qp_max=50)
        fb2.seed(qp_used=1, packet_size=100)
        assert fb2.qp == 5

    def test_note_before_seed_raises(self) -> None:
        fb = ToposRateFeedback(target_bytes=100)
        with pytest.raises(RuntimeError, match="未定标"):
            fb.note(100)

    def test_note_returns_qp_within_bounds(self) -> None:
        fb = ToposRateFeedback(target_bytes=1000, qp_min=0, qp_max=95)
        fb.seed(20, 1000)
        sizes = [50, 5_000_000, 1, 1000, 999, 12345] * 6
        for size in sizes:
            qp = fb.note(size)
            assert isinstance(qp, int)
            assert 0 <= qp <= 95

    def test_oversized_frame_raises_next_qp(self) -> None:
        fb = ToposRateFeedback(target_bytes=1000)
        fb.seed(20, 1000)
        qp = fb.note(8 * 1000)  # 严重超支 → 码控升 qp 压码
        assert qp > 20

    def test_undersized_frame_lowers_next_qp(self) -> None:
        fb = ToposRateFeedback(target_bytes=1000)
        fb.seed(40, 1000)
        qp = fb.note(125)  # 8 倍欠支 → 降 qp 提质量
        assert qp < 40

    def test_deterministic_qp_sequence(self) -> None:
        """spec §11.1：同帧字节序列 → 同 qp 序列 → 同累计字节。"""
        sizes = [900, 1100, 100_000, 500, 1000, 2500, 25, 1000] * 4

        def run() -> tuple:
            fb = ToposRateFeedback(target_bytes=1000)
            fb.seed(24, 1000)
            qps = [fb.note(s) for s in sizes]
            return tuple(qps), fb.total, fb.frames

        assert run() == run()


class TestCutDetectionAndAnchored:
    def _warmed_up(self) -> ToposRateFeedback:
        fb = ToposRateFeedback(target_bytes=1000)
        fb.seed(20, 1000)
        for _ in range(6):
            fb.note(1000)
        return fb

    def test_cut_frame_sets_reseed_hint(self) -> None:
        fb = self._warmed_up()
        assert fb.reseed_hint is False
        fb.note(1000 * 5)  # > cut_factor(1.8) × 窗口中位数
        assert fb.last_frame_was_cut is True
        assert fb.reseed_hint is True
        # 遥测帧 index 采用"已入账帧数"（1 基）：切帧即最后一帧
        assert fb.telemetry()["cut_frames"] == [fb.frames]

    def test_normal_frames_do_not_flag_cut(self) -> None:
        fb = self._warmed_up()
        fb.note(1200)  # 1.2× < 1.8×，非切换帧
        assert fb.last_frame_was_cut is False
        assert fb.reseed_hint is False

    def test_anchored_resets_hint_and_keeps_history(self) -> None:
        fb = self._warmed_up()
        fb.note(1000 * 5)
        assert fb.reseed_hint is True
        frames_before = fb.frames
        total_before = fb.total

        fb.anchored(qp_used=33, packet_size=990)

        assert fb.qp == 33
        assert fb.reseed_hint is False
        # 与 seed 的区别：不清预算累计/帧数
        assert fb.frames == frames_before + 1
        assert fb.total == total_before + 990

    def test_overshoot_beyond_lend_cap_sets_hint(self) -> None:
        """单帧超支 > lend_cap_factor × target = 码控失配 → 强修正信号。"""
        fb = ToposRateFeedback(target_bytes=1000, lend_cap_factor=3.0)
        fb.seed(20, 1000)
        fb.note(1000)  # 正常帧，清掉 seed 阶段状态
        fb.reseed_hint = False
        fb.note(5000)  # 超支 4000 > 3×1000 封顶；窗口 <5 帧避免命中切换路径
        assert fb.reseed_hint is True


class TestBudgetMode:
    def test_over_budget_flag_flips(self) -> None:
        fb = ToposRateFeedback(target_bytes=500, total_budget_bytes=1000,
                               total_frames=10)
        fb.seed(20, 900)
        assert fb.over_budget is False
        fb.note(200)  # total=1100 > 1000
        assert fb.over_budget is True

    def test_budget_target_refreshes_after_seed(self) -> None:
        """逐帧目标 = 剩余预算均摊 ×0.97（P1 预算模式）。"""
        fb = ToposRateFeedback(target_bytes=1000, total_budget_bytes=10000,
                               total_frames=10)
        fb.seed(20, 5000)
        # 剩余 5000 × 0.97 // 9 帧 = 538
        assert fb.target == 538.0

    def test_budget_target_never_below_one(self) -> None:
        fb = ToposRateFeedback(target_bytes=1000, total_budget_bytes=100,
                               total_frames=100)
        fb.seed(20, 99)
        assert fb.target == 1.0


class TestStatsAndTelemetry:
    def test_stats_zero_frames(self) -> None:
        fb = ToposRateFeedback(target_bytes=1000)
        assert fb.stats() == (0, 0, 0.0)

    def test_stats_tracks_frames_and_total(self) -> None:
        fb = ToposRateFeedback(target_bytes=1000)
        fb.seed(20, 800)
        fb.note(1200)
        frames, total, _ = fb.stats()
        assert frames == 2
        assert total == 2000

    def test_stats_avg_deviation_near_zero_at_target(self) -> None:
        fb = ToposRateFeedback(target_bytes=1000)
        fb.seed(20, 1000)
        for _ in range(20):
            fb.note(1000)
        _, _, avg_dev = fb.stats()
        assert abs(avg_dev) < 1e-9

    def test_telemetry_empty(self) -> None:
        t = ToposRateFeedback(target_bytes=1000).telemetry()
        assert t["frames_recorded"] == 0
        assert t["qp_min"] is None and t["qp_max"] is None
        assert t["qp_last"] is None
        assert t["bytes_max_frame"] is None
        assert t["cut_frames"] == []
        assert t["per_frame"] == []

    def test_telemetry_records_per_frame(self) -> None:
        fb = ToposRateFeedback(target_bytes=1000)
        fb.seed(20, 1000)
        fb.note(1000)
        fb.note(1000)
        t = fb.telemetry()
        assert t["frames_recorded"] == 3
        assert t["bytes_total"] == 3000
        assert len(t["per_frame"]) == 3
        # index 为已入账帧数（1 基）；qp 记的是该帧编码时使用的 qp
        assert t["per_frame"][0] == (1, 20, 1000, 0)
        assert t["qp_min"] == min(rec[1] for rec in t["per_frame"])
        assert t["qp_last"] == fb.qp
        assert t["debt_bytes"] >= 0
        assert t["reseed_hint"] is fb.reseed_hint
