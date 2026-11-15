"""H08：真正事件驱动的颗粒调度（复审 §5 H08）。

修前复现：
- 1 秒块 @80 grains/s 先触发 80 粒后渲染：64 池满丢 16、物理重叠 40；
- +12st/85ms/position0 读头 99.975% 被钳到同一点；Reverse seed19 需
  5.2617s 历史而容量 5s；
- 255/257 交替块时 FB95% 与 FB0 输出差 0；Cloud=Scatter 差 0。

修后契约（``_GrainModule`` H08）：
- 子块（≤256）交错"触发到边界→渲染"——池占用=真实重叠；触发时刻/
  随机消耗顺序块长无关（整块 == 任意分块，逐位）；
- 读头年龄上限覆盖 ring 容量与完整粒跨度；不足历史延后触发并计数
  （dropped_events），不读已覆盖数据；
- feedback = wet 经 D=size_n 样本延迟的环形回写（因果、有界、块无关）；
- Cloud（高斯年龄抖动+随机声道）/ Scatter（均匀撒布+交替声道+增益
  抖动）契约分化；历史容量 8s 覆盖最大读龄。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_effects.spectral_mangler import (
    _GrainModule,
    _HISTORY_SECONDS,
)
from src.features.edit.core.timeline_v2 import SpectralManglerParams

pytestmark = pytest.mark.edit_core
SR = 48000


def _noise(n: int, seed: int = 11) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal((2, n)) * 0.25


class TestEventScheduling:
    def test_1s_block_no_pool_overflow(self):
        """复审案例：1s 块 @80/s + 500ms 粒——真实重叠不超池（修前丢 16）。"""
        x = _noise(SR)
        p = SpectralManglerParams(
            grain_enabled=True, grain_density_hz=80.0, grain_size_ms=500.0,
            grain_mode="cloud", grain_position_pct=0.0, grain_spray_pct=0.0,
            grain_pitch_jitter_pct=0.0, grain_feedback_pct=0.0,
            random_seed=19)
        g = _GrainModule(SR)
        g.set_seed(19)
        g.process(x.copy(), p, 0)
        assert g.dropped_events == 0

    def test_whole_equals_arbitrary_blocks(self):
        x = _noise(SR * 2)
        p = SpectralManglerParams(grain_enabled=True, random_seed=19,
                                  grain_density_hz=50.0,
                                  grain_spray_pct=60.0,
                                  grain_pitch_jitter_pct=40.0)
        g1 = _GrainModule(SR)
        g1.set_seed(19)
        yw = g1.process(x.copy(), p, 0)
        g2 = _GrainModule(SR)
        g2.set_seed(19)
        parts = []
        pos = 0
        rng = np.random.default_rng(31)
        while pos < x.shape[-1]:
            m = min(int(rng.integers(1, 3000)), x.shape[-1] - pos)
            parts.append(g2.process(x[:, pos:pos + m].copy(), p, pos))
            pos += m
        np.testing.assert_allclose(yw, np.concatenate(parts, axis=-1),
                                   atol=1e-12)

    def test_read_positions_traverse_source(self):
        """读头实际遍历合法源（position>0 时读位分布在历史区间）。"""
        x = _noise(SR)
        p = SpectralManglerParams(grain_enabled=True, grain_density_hz=30.0,
                                  grain_size_ms=100.0,
                                  grain_position_pct=50.0,
                                  grain_spray_pct=80.0, random_seed=5)
        reads = []
        orig = _GrainModule._hist_read

        def spy(self_mod, ch, positions):
            reads.append(np.asarray(positions).copy())
            return orig(self_mod, ch, positions)

        _GrainModule._hist_read = spy
        try:
            g = _GrainModule(SR)
            g.set_seed(5)
            g.process(x.copy(), p, SR)  # abs 起 SR：历史从 0 起
        finally:
            _GrainModule._hist_read = orig
        allp = np.concatenate(reads)
        assert allp.size > 1000
        spread = float(allp.max() - allp.min())
        assert spread > 1000.0  # 非 99.975% 钳在同一点
        assert allp.min() >= 0.0

    def test_history_capacity_covers_max_read_age(self):
        """Reverse seed19 需 5.26s：容量 8s 下零丢弃。"""
        assert _HISTORY_SECONDS >= 8.0
        x = _noise(SR * 3)
        p = SpectralManglerParams(
            grain_enabled=True, grain_mode="reverse", random_seed=19,
            grain_size_ms=500.0, grain_density_hz=30.0,
            grain_position_pct=100.0, grain_spray_pct=100.0,
            grain_pitch_st=12.0)
        g = _GrainModule(SR)
        g.set_seed(19)
        y = g.process(x.copy(), p, 0)
        assert g.dropped_events == 0
        assert np.isfinite(y).all()


class TestFeedback:
    def test_fb_active_and_block_invariant(self):
        """FB95 vs FB0 输出不同（修前 255/257 交替下差 0）；且反馈路径
        本身块不变（样本域固定延迟）。"""
        x = _noise(SR * 2)
        base = dict(grain_enabled=True, grain_density_hz=30.0,
                    grain_mode="cloud", random_seed=7)
        outs = {}
        for tag, fb in (("fb95", 95.0), ("fb0", 0.0)):
            p = SpectralManglerParams(grain_feedback_pct=fb, **base)
            g = _GrainModule(SR)
            g.set_seed(7)
            outs[tag] = g.process(x.copy(), p, 0)
        assert np.abs(outs["fb95"] - outs["fb0"]).max() > 1e-3

        p95 = SpectralManglerParams(grain_feedback_pct=95.0, **base)
        g1 = _GrainModule(SR)
        g1.set_seed(7)
        yw = g1.process(x.copy(), p95, 0)
        g2 = _GrainModule(SR)
        g2.set_seed(7)
        parts, pos = [], 0
        for b in [255, 257] * 93 + [204]:
            parts.append(g2.process(x[:, pos:pos + b].copy(), p95, pos))
            pos += b
        np.testing.assert_allclose(yw[:, :pos],
                                   np.concatenate(parts, axis=-1),
                                   atol=1e-9)

    def test_fb_bounded(self):
        """95% 反馈长跑有界（归一环路稳定）。"""
        x = _noise(SR * 6)
        p = SpectralManglerParams(grain_enabled=True, grain_density_hz=40.0,
                                  grain_feedback_pct=95.0, random_seed=2,
                                  grain_mode="cloud")
        g = _GrainModule(SR)
        g.set_seed(2)
        y = g.process(x.copy(), p, 0)
        assert np.abs(y).max() < 8.1  # 输出 clip 契约 −8..8


class TestModesDiffer:
    def test_cloud_vs_scatter(self):
        x = _noise(SR * 2)
        base = dict(grain_enabled=True, random_seed=3, grain_spray_pct=70.0,
                    grain_density_hz=40.0)
        pc = SpectralManglerParams(grain_mode="cloud", **base)
        ps = SpectralManglerParams(grain_mode="scatter", **base)
        gc = _GrainModule(SR)
        gc.set_seed(3)
        gs = _GrainModule(SR)
        gs.set_seed(3)
        yc = gc.process(x.copy(), pc, 0)
        ys = gs.process(x.copy(), ps, 0)
        assert np.abs(yc - ys).max() > 1e-2  # 修前恒 0

    def test_same_seed_replay_identical(self):
        x = _noise(SR)
        p = SpectralManglerParams(grain_enabled=True, grain_mode="scatter",
                                  random_seed=42, grain_spray_pct=50.0)
        g1 = _GrainModule(SR)
        g1.set_seed(42)
        y1 = g1.process(x.copy(), p, 0)
        g2 = _GrainModule(SR)
        g2.set_seed(42)
        y2 = g2.process(x.copy(), p, 0)
        np.testing.assert_array_equal(y1, y2)

    def test_freeze_stops_history(self):
        x = _noise(SR)
        p = SpectralManglerParams(grain_enabled=True, grain_freeze=True,
                                  random_seed=8, grain_density_hz=50.0)
        g = _GrainModule(SR)
        g.set_seed(8)
        g.process(x[:, :SR // 2].copy(), p, 0)
        w = g._written
        g.process(x[:, SR // 2:].copy(), p, SR // 2)
        assert g._written == w  # freeze 停写


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
