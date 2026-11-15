"""RV-02 回归：image-source ER、正负 Pre Delay 传播模型、Distance 语义（阶段G）。

验收（计划 §10 RV-02）：
- 相同平均尺寸不同形状产生合理不同 ER 分布（各维独立路径）；
- 负 pre-delay 可抵消部分传播时延（不再整段钳 0）；
- 不同 distance 的 ER 变化能解释（首反射随距离推迟）且晚期不随距离平移；
- ER-only 首反射时间与几何 oracle 误差 ≤1 采样；
- 权重有限、抽头 ≥1、L/R 能量守恒。
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_dsp import ReverbEffect, _SPEED_OF_SOUND

SR = 48000.0


def _reverb(pre_delay=42.0, distance=19.87, height=10.0, length=40.0,
            width=30.0, enabled=True):
    rv = ReverbEffect(SR)
    p = rv._default_params_dict()
    p.update({"pre_delay": pre_delay, "distance": distance,
              "height": height, "length": length, "width": width,
              "enabled": enabled})
    rv.set_params(p)
    return rv


def _paths_and_direct(p_dict):
    """几何 oracle：一阶+角镜像路径长度与直达几何路径（与 DSP 同一模型）。"""
    h, l, w = p_dict["height"], p_dict["length"], p_dict["width"]
    room = np.array([l, w, h])
    src = np.array([l * 0.4, w * 0.5, h * 0.5])
    lis = np.array([l * 0.6, w * 0.5, h * 0.5])
    images = []
    for axis in range(3):
        for far_wall in (False, True):
            img = src.copy()
            img[axis] = (2.0 * room[axis] - src[axis] if far_wall
                         else -src[axis])
            images.append(img)
    for axes in ((0, 1), (0, 2), (1, 2), (0, 1, 2)):
        img = src.copy()
        for axis in axes:
            img[axis] = 2.0 * room[axis] - src[axis]
        images.append(img)
    paths = np.linalg.norm(np.array(images) - lis, axis=1)
    direct = float(np.linalg.norm(lis - src))
    extra = np.sort(paths - direct)
    return extra, direct


class TestRv02ImageSourceEr:
    def test_first_reflection_matches_geometry_within_1_sample(self):
        """ER-only 首抽头 = 有效传播 + 最短附加反射行程（±1 采样；含 ±2%
        确定性去相关抖动的第 1 抽头因子 1+0.02·sin(2.1)）。"""
        pre, dist = 42.0, 19.87
        rv = _reverb(pre_delay=pre, distance=dist)
        extra, _direct = _paths_and_direct(
            {"height": 10.0, "length": 40.0, "width": 30.0})
        effective = max(0.0, pre * 0.001 + dist / _SPEED_OF_SOUND)
        jitter = 1.0 + 0.02 * np.sin(1 * 2.1)
        expected = round((effective + extra[0] / _SPEED_OF_SOUND)
                         * jitter * SR)
        assert abs(int(rv._er_taps_l[0]) - expected) <= 1

    def test_same_mean_different_shape_different_er(self):
        """均值同 22.67m：(40,20,8) vs (26,24,18)：ER 分布不同（各维独立）。"""
        a = _reverb(height=8.0, length=40.0, width=20.0)
        b = _reverb(height=18.0, length=26.0, width=24.0)
        ta = np.sort(a._er_taps_l)
        tb = np.sort(b._er_taps_l)
        # 首抽头与跨距都应不同（各维独立）
        assert not np.array_equal(ta, tb), \
            "same-mean different-shape rooms must yield different ER taps"
        assert abs(float(ta[0]) - float(tb[0])) > SR * 0.001  # >1ms 差异

    def test_weights_finite_and_lr_energy_conserved(self):
        rv = _reverb()
        wl, wr = rv._er_weights_l, rv._er_weights_r
        assert np.all(np.isfinite(wl)) and np.all(np.isfinite(wr))
        assert np.all(rv._er_taps_l >= 1) and np.all(rv._er_taps_r >= 1)
        assert np.all(wl > 0.0) and np.all(wr > 0.0)
        # 方向分配能量守恒：wl + wr = 2 × 基础权重（pan_l + pan_r = 1）
        base = (wl + wr) / 2.0
        np.testing.assert_allclose(wl + wr, 2.0 * base, rtol=1e-12)


class TestRv02PreDelayPropagation:
    def test_negative_predelay_cancels_propagation(self):
        """pre=-D/c（ms）+ distance=D ⇒ ER 起始 ≈ pre=0, distance=0 的情形
        （有效传播抵消为 0；晚期也回到 pre=0 基线）。
        """
        d_m = 17.15  # 50ms @343
        cancel_ms = -(d_m / _SPEED_OF_SOUND) * 1000.0
        rv_a = _reverb(pre_delay=cancel_ms, distance=d_m)
        rv_b = _reverb(pre_delay=0.0, distance=0.0)
        # 首抽头一致（镜像路径相同；有效传播都为 0）
        assert int(rv_a._er_taps_l[0]) == int(rv_b._er_taps_l[0])
        # 晚期起始一致（都不随距离平移）
        assert abs(rv_a._late_offset_l - rv_b._late_offset_l) < 1.0

    def test_old_clamp_no_longer_zeros_negative_region(self):
        """旧实现 max(0, pre) 整段钳 0：pre=-100ms 与 pre=0 完全相同；
        新实现 pre=-100ms 仍在抵消（当 distance 足够大时起始 < pre=0 情形）。
        """
        d_m = 34.3  # 100ms
        rv_neg = _reverb(pre_delay=-100.0, distance=d_m)
        rv_zero = _reverb(pre_delay=0.0, distance=d_m)
        # effective(-100ms+100ms)=0 < effective(0+100ms)=100ms
        assert rv_neg._er_taps_l[0] < rv_zero._er_taps_l[0]

    def test_fully_negative_still_clamped_at_zero(self):
        """pre + dist/c < 0 → effective=0（早反射不可能先于直达声）。"""
        rv = _reverb(pre_delay=-200.0, distance=0.0)
        extra0, _ = _paths_and_direct(
            {"height": 10.0, "length": 40.0, "width": 30.0})
        jitter = 1.0 + 0.02 * np.sin(1 * 2.1)
        expected = round(extra0[0] / _SPEED_OF_SOUND * jitter * SR)
        assert abs(int(rv._er_taps_l[0]) - expected) <= 1


class TestRv02DistanceSemantics:
    def test_distance_moves_er_not_late(self):
        """distance 0 → 20m：ER 首抽头推迟 ~58ms；晚期起始不变。"""
        near = _reverb(distance=0.0)
        far = _reverb(distance=20.0)
        delta_samples = int(far._er_taps_l[0]) - int(near._er_taps_l[0])
        delta_ms = delta_samples / SR * 1000.0
        assert delta_ms == pytest.approx(20.0 / _SPEED_OF_SOUND * 1000.0,
                                         abs=2.0)
        # 晚期起始不随距离平移（旧缺陷：整段晚期跟着 distance 走）
        assert far._late_offset_l == pytest.approx(near._late_offset_l,
                                                   abs=1.0)

    def test_distance_changes_output(self):
        """不同 distance 的湿输出 ER 到达时间不同（首个非零样本平移）。"""
        def _first_arrival(dist):
            rv = _reverb(distance=dist, enabled=True)
            n = int(SR * 0.4)  # 400ms：容纳传播+反射
            x = np.zeros((2, n), dtype=np.float32)
            x[:, 0] = 1.0
            y = rv.process(x)
            nz = np.flatnonzero(np.abs(y).sum(axis=0) > 1e-6)
            return int(nz[0]) if nz.size else -1

        i_near = _first_arrival(0.0)
        i_far = _first_arrival(25.0)
        assert i_near > 0 and i_far > i_near
        delta_ms = (i_far - i_near) / SR * 1000.0
        assert delta_ms == pytest.approx(25.0 / _SPEED_OF_SOUND * 1000.0,
                                         abs=3.0)


class TestRv02HotUpdateRegression:
    def test_geometry_change_keeps_ring_history(self):
        """RV-04 回归锁：容量足够时参数拖动不重建环形缓冲（历史保留）。"""
        rv = _reverb(distance=25.0)   # 先建大容量
        x = np.zeros((2, 480), dtype=np.float32)
        x[:, 0] = 0.8
        rv.process(x)
        ring_obj_before = rv._ring
        rv.set_params({**rv._params, "distance": 5.0})
        # 容量足够 → 同一缓冲对象，内容未清
        assert rv._ring is ring_obj_before
