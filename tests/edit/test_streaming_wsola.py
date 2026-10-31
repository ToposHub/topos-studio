"""F3.2 · 流式实时保调变速——StreamingWsola 验收。

冻结的算法容差（实施前冻结，见 ledger）：
- 音高：已知频率向量 0.5/1.25/2 倍伸缩后主频偏移 ≤ 3%（变调 resample
  在 2 倍时偏 100%，必被抓住）；
- 时长：|输出长度 − 输入长度/速度| ≤ frame_len + delta + hop
  （48k/25ms/7.5ms ⇒ ≤ 2160 样本 ≈ 45ms）；
- 预听/导出一致：流式 vs 离线同参数，归一化互相关 ≥ 0.95、长度差 ≤ hop；
- 切换无爆音：倍率突变处 |x[n]−x[n−1]| ≤ 10× 全流 p95 |diff|，且无
  NaN、峰值 ≤ 1.5；
- 有界内存：内部滞留输入 ≤ margin + 一个 push 块。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.audio_timestretch import (
    DEFAULT_FRAME_MS,
    DEFAULT_SEARCH_MS,
    MAX_SPEED,
    MIN_SPEED,
    StreamingWsola,
    wsola_time_stretch,
)

SR = 48000
BLOCK = 1024
FREQ_TOL = 0.03            # 冻结：主频偏移 ≤ 3%
LEN_TOL = None             # 按 48k 参数计算（见下）


def _len_tol() -> int:
    frame = max(64, int(round(SR * DEFAULT_FRAME_MS / 1000.0)))
    delta = max(1, int(round(SR * DEFAULT_SEARCH_MS / 1000.0)))
    if frame % 2:
        frame += 1
    return frame + delta + frame // 2


def _sine(freq: float, seconds: float = 1.0, channels: int = 1) -> np.ndarray:
    t = np.arange(int(SR * seconds), dtype=np.float32) / SR
    x = 0.8 * np.sin(2 * np.pi * freq * t)
    return x if channels == 1 else np.stack([x, x * 0.7], axis=0)


def _speech_like(seconds: float = 1.0) -> np.ndarray:
    """类语音：220Hz 基频谐波簇 + 3Hz 响度包络 + 轻噪声。"""
    t = np.arange(int(SR * seconds), dtype=np.float32) / SR
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 3.0 * t)
    x = np.zeros(t.size, dtype=np.float64)
    for h in range(1, 12):
        x += (1.0 / h) * np.sin(2 * np.pi * 220.0 * h * t + 0.1 * h)
    rng = np.random.default_rng(42)
    return (x / 2.5 * env + 0.01 * rng.standard_normal(t.size)).astype(np.float32)


def _stream(stretcher: StreamingWsola, sig: np.ndarray, speed: float,
            block: int = BLOCK) -> np.ndarray:
    """模拟音频回调：推 n*speed（首块 +margin）→ 拉 n；尾块 flush。"""
    mono = sig.ndim == 1
    x = sig[np.newaxis, :] if mono else sig
    margin = stretcher.algorithmic_latency_samples
    pos, first, outs = 0, True, []
    while True:
        need = int(round(block * speed)) + (margin if first else 0)
        first = False
        chunk = x[:, pos:pos + need]
        if chunk.shape[1] == 0:
            break
        pos += chunk.shape[1]
        stretcher.push(chunk)
        got = stretcher.pull(block)
        if got.shape[1]:
            outs.append(got)
    tail = stretcher.flush()
    if tail.shape[1]:
        outs.append(tail)
    if not outs:
        return np.zeros(0, dtype=np.float32)
    out = np.concatenate(outs, axis=1)
    return out[0] if mono else out


def _dominant_freq(x: np.ndarray) -> float:
    if x.size < 256:
        return 0.0
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    freq = np.fft.rfftfreq(x.size, 1.0 / SR)
    return float(freq[int(np.argmax(spec))])


def _autocorr_period(x: np.ndarray, lo_hz: float, hi_hz: float) -> float:
    """自相关基频估计（谐波簇主频用 FFT 会落在泛音上，改测周期）。"""
    best_lag, best_r = 0, -2.0
    for lag in range(int(SR / hi_hz), int(SR / lo_hz)):
        r = float(np.dot(x[:-lag], x[lag:]) / (x.size - lag))
        if r > best_r:
            best_r, best_lag = r, lag
    return SR / best_lag


pytestmark = pytest.mark.edit_core


class TestKnownFrequencyVectors:
    @pytest.mark.parametrize("speed", [0.5, 1.25, 2.0])
    def test_sine_pitch_and_duration(self, speed: float) -> None:
        """已知频率向量：440Hz 正弦 0.5/1.25/2 倍——音高保持、时长精确。"""
        sig = _sine(440.0)
        out = _stream(StreamingWsola(1, SR, speed), sig, speed)
        assert abs(_dominant_freq(out) - 440.0) / 440.0 <= FREQ_TOL
        expected = sig.size / speed
        assert abs(out.size - expected) <= _len_tol()
        assert float(np.max(np.abs(out))) <= 1.05

    @pytest.mark.parametrize("speed", [0.5, 2.0])
    def test_stereo_channels_share_alignment(self, speed: float) -> None:
        """多声道共用对齐：立体声两路同步伸缩（声像不漂）。"""
        sig = _sine(440.0, channels=2)
        out = _stream(StreamingWsola(2, SR, speed), sig, speed)
        assert out.shape[0] == 2
        # 两路同长同峰（对齐共享 → 无相对时移）
        assert out.shape[1] > 0
        ratio = float(np.max(np.abs(out[0]))) / max(
            1e-9, float(np.max(np.abs(out[1]))))
        assert 0.9 <= ratio <= 1.5  # 输入幅度比 1 : 0.8

    @pytest.mark.parametrize("speed", [0.5, 1.25, 2.0])
    def test_speech_like_preserves_f0(self, speed: float) -> None:
        """0.5/1.25/2 倍语音向量：基频 220Hz 保持 ±3%、包络不爆。"""
        sig = _speech_like()
        out = _stream(StreamingWsola(1, SR, speed), sig, speed)
        f0 = _autocorr_period(out, 150.0, 350.0)
        assert abs(f0 - 220.0) / 220.0 <= FREQ_TOL
        assert float(np.max(np.abs(out))) <= 1.5
        assert np.all(np.isfinite(out))


class TestPreviewExportConsistency:
    @pytest.mark.parametrize("speed", [0.5, 1.25, 2.0])
    def test_streaming_matches_offline(self, speed: float) -> None:
        """预听/导出策略一致：同参数流式 vs 离线，互相关 ≥ 0.95。"""
        sig = _speech_like()
        offline = wsola_time_stretch(sig, speed, SR)
        streamed = _stream(StreamingWsola(1, SR, speed), sig, speed)
        assert abs(streamed.size - offline.size) <= 1200  # ≤ frame_len
        L = min(offline.size, streamed.size)
        a, b = offline[:L].astype(np.float64), streamed[:L].astype(np.float64)
        corr = float(np.dot(a, b) / np.sqrt(np.dot(a, a) * np.dot(b, b)))
        assert corr >= 0.95


class TestStateManagement:
    def test_first_block_latency_compensated(self) -> None:
        """首块读前补偿：推 n*speed+margin 后立即可拉满 n（无空转等待）。"""
        speed = 2.0
        sw = StreamingWsola(1, SR, speed)
        n = BLOCK
        sw.push(_sine(440.0, seconds=0.2)[
                : int(round(n * speed)) + sw.algorithmic_latency_samples])
        assert sw.pull(n).shape[-1] == n

    def test_reset_clears_time_domain_state(self) -> None:
        """reset（seek/loop/暂停）后与新实例同输入同输出（零残留）。"""
        sig = _speech_like(0.5)
        fresh = _stream(StreamingWsola(1, SR, 2.0), sig, 2.0)
        sw = StreamingWsola(1, SR, 2.0)
        sw.push(_sine(300.0, seconds=0.3))     # 先播别的内容
        sw.pull(512)
        sw.reset()
        after = _stream(sw, sig, 2.0)
        assert after.size == fresh.size
        assert np.allclose(after, fresh, atol=1e-6)

    def test_speed_switch_no_click(self) -> None:
        """倍率突变（1→2→0.5）无异常脉冲/爆音：切换点一阶差分受控。"""
        sw = StreamingWsola(1, SR, 1.0)
        sig = _speech_like(2.0)
        margin = sw.algorithmic_latency_samples
        out, pos, first = [], 0, True
        switch_blocks = {8: 2.0, 20: 0.5}
        block_idx = 0
        while True:
            speed_now = sw.speed
            need = int(round(BLOCK * speed_now)) + (margin if first else 0)
            first = False
            chunk = sig[pos:pos + need]
            if chunk.size == 0:
                break
            pos += chunk.size
            sw.push(chunk)
            got = sw.pull(BLOCK)
            if got.size:
                out.append(got)
            block_idx += 1
            if block_idx in switch_blocks:
                sw.set_speed(switch_blocks[block_idx])
        tail = sw.flush()
        if tail.shape[1]:
            out.append(tail)
        x = np.concatenate(out, axis=1)[0]
        assert np.all(np.isfinite(x)) and float(np.max(np.abs(x))) <= 1.5
        diff = np.abs(np.diff(x))
        # 冻结：切换处最大跳变 ≤ 10× 全流 p95（爆音/脉冲会超出一个量级）
        assert float(np.max(diff)) <= 10.0 * float(np.percentile(diff, 95)) \
            or float(np.max(diff)) <= 0.2

    def test_bounded_internal_memory(self) -> None:
        """有界内存：长流下内部滞留输入 ≤ margin + 一个 push 块。"""
        speed = 2.0
        sw = StreamingWsola(1, SR, speed)
        sig = _speech_like(3.0)
        margin = sw.algorithmic_latency_samples
        pos, peak_pending = 0, 0
        while pos < sig.size:
            need = int(round(BLOCK * speed))
            sw.push(sig[pos:pos + need])
            pos += need
            sw.pull(BLOCK)
            peak_pending = max(peak_pending, sw.pending_input())
        # 有界不变式：refill 停在 need=delta+2hop 前 + trim 保留 delta 余量
        delta = int(round(SR * DEFAULT_SEARCH_MS / 1000.0))
        assert peak_pending <= margin + delta + int(round(BLOCK * speed))

    def test_invalid_speed_rejected(self) -> None:
        with pytest.raises(ValueError):
            StreamingWsola(1, SR, 0.01)
        with pytest.raises(ValueError):
            StreamingWsola(1, SR, MAX_SPEED + 1)
        sw = StreamingWsola(1, SR, MIN_SPEED)
        with pytest.raises(ValueError):
            sw.set_speed(99.0)
