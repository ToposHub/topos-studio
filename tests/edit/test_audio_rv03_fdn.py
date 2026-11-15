"""RV-03 回归：校准 8 线正交 FDN 晚期网络（阶段H）。

验收（计划 §10 RV-03）：
- 0.5/1/2/4s 目标 RT60 中低频误差 ≤10%（T30 能量衰减测量）；
- 长尾无周期啸叫或能量增长（正交 Householder → 谱半径 ≤ max|g_i|<1）；
- mod=0 无调制（同输入两次处理逐样本一致）；mod>0 输出不同；
- 预览/导出种子和时间轴一致（确定性——无随机源，两个新实例输出相同）；
- 多声道不是 L/R 交替复制（L/R 去相关：不等但能量同量级）。
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_dsp import ReverbEffect

SR = 48000.0


def _reverb(rt60_ms, mod=0.0, brightness=50.0):
    rv = ReverbEffect(SR)
    p = rv._default_params_dict()
    p.update({
        "enabled": True, "dry_wet": 100.0, "direct_db": -100.0,
        "er_db": -100.0, "reverb_db": 0.0,
        "pre_delay": 0.0, "distance": 0.0,
        "reverb_time": rt60_ms, "modulation": mod, "brightness": brightness,
        "rev_lo_gain": 0.0, "rev_hi_gain": 0.0,  # 中性染色
    })
    rv.set_params(p)
    return rv


def _t30_seconds(y, sr=SR):
    """中低频段（100–1000Hz）能量包络 T30（RV-03 验收口径：
    "0.5/1/2/4s 目标 RT60 在所定义中低频误差 ≤10%"——阻尼拐角以下
    |H|≈1，包络按 g_i 严格衰减；高频更快衰减属频谱倾斜非校准误差）。"""
    w = y.sum(axis=0)
    spec = np.fft.rfft(w)
    freqs = np.fft.rfftfreq(len(w), 1.0 / sr)
    spec[(freqs < 100.0) | (freqs > 1000.0)] = 0.0
    w = np.abs(np.fft.irfft(spec, len(w)))
    win = int(0.05 * sr)
    env = np.convolve(w ** 2, np.ones(win) / win, mode="same")
    edb = 10.0 * np.log10(np.maximum(env, 1e-16))
    pk = int(np.argmax(edb))
    t5 = t35 = None
    for i in range(pk, len(env) - 1):
        if t5 is None and edb[i] < edb[pk] - 5.0:
            t5 = i
        if t5 is not None and edb[i] < edb[pk] - 35.0:
            t35 = i
            break
    if t5 is None or t35 is None:
        return None
    return (t35 - t5) / sr * 2.0


def _ir(rv, seconds):
    n = int(seconds * SR)
    imp = np.zeros((2, n), dtype=np.float32)
    imp[:, 100] = 1.0
    return rv.process(imp)


class TestRv03Rt60Calibration:
    @pytest.mark.parametrize("target_s", [0.5, 1.0, 2.0, 4.0])
    def test_rt60_within_10_percent(self, target_s):
        """目标 RT60（中低频宽带测量；中性染色）误差 ≤10%。"""
        rv = _reverb(target_s * 1000.0)
        y = _ir(rv, target_s * 2.2)
        measured = _t30_seconds(y)
        assert measured is not None, "decay envelope did not cross -35dB"
        assert measured == pytest.approx(target_s, rel=0.10), (
            f"RT60 target {target_s}s measured {measured:.3f}s")

    def test_long_tail_finite_and_decaying(self):
        """RT60=4s 强噪声突发 + 长静音：finite、单调衰减、无自激增长。"""
        rv = _reverb(4000.0)
        rng = np.random.default_rng(3)
        n_burst = int(0.5 * SR)
        burst = (0.8 * rng.standard_normal(n_burst)).astype(np.float32)
        x = np.zeros((2, int(SR * 6)), dtype=np.float32)
        x[:, :n_burst] = np.stack([burst] * 2)
        y = rv.process(x)
        assert np.all(np.isfinite(y))
        # 分段 RMS 单调下降（衰减方向正确，无能量增长/自激）
        segs = [float(np.sqrt(np.mean(y[:, int(t * SR):int((t + 1) * SR)] ** 2)))
                for t in range(1, 6)]
        assert all(segs[i] > segs[i + 1] for i in range(len(segs) - 1)), segs
        assert segs[-1] > 0.0  # RT60=4s：6s 时仍有残余（-30dB 以下量级）


class TestRv03Modulation:
    def test_mod_zero_is_deterministic_and_unmodulated(self):
        """mod=0：同输入两次处理逐样本一致（无时变）。"""
        rng = np.random.default_rng(7)
        x = np.stack([(0.3 * rng.standard_normal(int(SR // 2)))
                      .astype(np.float32)] * 2)
        a = _reverb(1500.0, mod=0.0).process(x.copy())
        b = _reverb(1500.0, mod=0.0).process(x.copy())
        np.testing.assert_array_equal(a, b)

    def test_mod_nonzero_changes_output(self):
        rng = np.random.default_rng(7)
        x = np.stack([(0.3 * rng.standard_normal(int(SR // 2)))
                      .astype(np.float32)] * 2)
        a = _reverb(1500.0, mod=0.0).process(x.copy())
        b = _reverb(1500.0, mod=30.0).process(x.copy())
        assert not np.allclose(a, b)

    def test_deterministic_across_instances(self):
        """确定性：两个新实例（同参数/同输入）输出逐样本一致
        （预览/导出种子与时间轴一致的基础）。"""
        rng = np.random.default_rng(11)
        x = np.stack([(0.3 * rng.standard_normal(int(SR // 2)))
                      .astype(np.float32)] * 2)
        a = _reverb(2000.0, mod=10.0).process(x.copy())
        b = _reverb(2000.0, mod=10.0).process(x.copy())
        np.testing.assert_array_equal(a, b)


class TestRv03StereoDecorrelation:
    def test_lr_not_alternating_copies(self):
        """L/R 去相关：不相等但能量同量级（≤3dB 差）。"""
        rv = _reverb(2000.0)
        rng = np.random.default_rng(5)
        x = np.stack([(0.3 * rng.standard_normal(int(SR)))
                      .astype(np.float32)] * 2)
        y = rv.process(x.copy())
        assert not np.allclose(y[0], y[1])
        e_l = float(np.sqrt(np.mean(y[0] ** 2)))
        e_r = float(np.sqrt(np.mean(y[1] ** 2)))
        assert abs(20.0 * np.log10(max(e_l, 1e-12) / max(e_r, 1e-12))) < 3.0


class TestRv03HotUpdateRegression:
    def test_rt60_drag_keeps_buffers(self):
        """RV-04 锁：RT60 拖动就地更新增益，网络缓冲不清零。"""
        rv = _reverb(1000.0)
        x = np.zeros((2, 480), dtype=np.float32)
        x[:, 0] = 0.9
        rv.process(x)
        fdn_obj = rv._fdn8
        bufs_before = [b.copy() for b in rv._fdn8._bufs]
        g_before = rv._fdn8._g[0]
        rv.set_params({**rv._params, "reverb_time": 3000.0})
        assert rv._fdn8 is fdn_obj
        for before, after in zip(bufs_before, rv._fdn8._bufs):
            assert np.array_equal(before, after)
        # RT60 变长 → 增益升高（就地更新生效）
        assert rv._fdn8._g[0] > g_before

    def test_brightness_drag_updates_damping(self):
        rv = _reverb(2000.0, brightness=20.0)
        damp2_dark = rv._fdn8._damp2
        rv.set_params({**rv._params, "brightness": 100.0})
        # 更亮 → 阻尼低通输入权重更大（反馈极点更小，高频衰减少）
        assert rv._fdn8._damp2 > damp2_dark
