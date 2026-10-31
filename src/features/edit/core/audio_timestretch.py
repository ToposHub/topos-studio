"""音频保调变速（WSOLA）—— P1-08 离线实现。

背景（计划表 P1-08）：当前两条变速路径均为"变调 resample"——实时
（audio_playback.Decoder 按 speed 缩放每帧读取的源采样数）与离线导出
（timeline_export 按 speed 平移 source_frame）都会随速度改变音调。
专业剪辑要求 0.5x/1.5x 下人声音调保持。

本模块实现 WSOLA（Waveform-Similarity Overlap-Add）时间伸缩：

- 按帧重叠相加（Hann 窗，50% overlap）重建波形；
- 每帧在容差范围内搜索与"自然延续模板"相关性最佳的对齐点，
  消除重叠相加的相位不连续（咔哒声）；
- 多声道共用同一组对齐偏移（相似度在声道混合单声道上计算），
  保证立体声像不漂移；
- 输出时长 = 输入时长 / speed（speed>1 更短）。

定位与边界：
- 离线路径：``wsola_time_stretch``（导出/Stem 预解码整段伸缩）；
- 实时路径：``StreamingWsola``（F3.2 流式状态机，音频回调内逐块
  push/pull；有界缓冲，seek/loop/暂停 reset，倍率突变清对齐状态）；
  两者共用同一组帧长/搜索容差参数——预听/导出策略一致；
- speed == 1 直接返回副本；输入过短（不足两帧）原样返回；
- 速度范围沿用 SpeedParams 的 0.1–8.0，越界抛 ValueError。
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# 典型语音/音乐 WSOLA 参数：25ms 分析帧、7.5ms 搜索容差
DEFAULT_FRAME_MS = 25.0
DEFAULT_SEARCH_MS = 7.5

MIN_SPEED = 0.1
MAX_SPEED = 8.0


def _best_offset(
    reference: np.ndarray,
    template: np.ndarray,
    nominal: int,
    delta: int,
    max_start: int,
) -> int:
    """在 [nominal-delta, nominal+delta] 内找与模板相关性最佳的对齐点。

    reference: 单声道参考信号；template: 上一帧的自然延续段（长度 H）。
    返回相对 nominal 的整数偏移 k（帧起点 = nominal + k）。
    """
    lo = max(0, nominal - delta)
    hi = min(max_start, nominal + delta)
    if hi <= lo:
        return 0
    # valid 模式相关：corr[i] = sum(reference[lo+i : lo+i+H] * template)
    corr = np.correlate(reference[lo : hi + template.shape[-1]], template, mode="valid")
    if corr.size == 0:
        return 0
    return int(lo + int(np.argmax(corr))) - nominal


def wsola_time_stretch(
    samples: np.ndarray,
    speed: float,
    sample_rate: float,
    *,
    frame_ms: float = DEFAULT_FRAME_MS,
    search_ms: float = DEFAULT_SEARCH_MS,
) -> np.ndarray:
    """WSOLA 保调时间伸缩（离线、确定性）。

    Args:
        samples: (channels, n) 或 (n,) 波形，float/int 均可。
        speed: 播放速度（>1 更快/更短，<1 更慢/更长），范围 [0.1, 8.0]。
        sample_rate: 采样率（决定帧/容差的绝对尺寸）。
        frame_ms: 分析帧长（毫秒）。
        search_ms: 对齐搜索容差（毫秒）。

    Returns:
        与输入同布局（含声道维）的伸缩结果，时长 ≈ n / speed。
        speed==1 或输入过短时返回输入副本。
    """
    speed = float(speed)
    if speed < MIN_SPEED or speed > MAX_SPEED:
        raise ValueError(f"speed must be between {MIN_SPEED} and {MAX_SPEED}: {speed}")
    x = np.asarray(samples, dtype=np.float32)
    mono_input = x.ndim == 1
    if mono_input:
        x = x[np.newaxis, :]
    if x.ndim != 2:
        raise ValueError(f"expected 1D or 2D samples, got shape {x.shape}")
    if x.shape[0] == 0 or x.shape[1] == 0:
        return x.copy()
    if abs(speed - 1.0) < 1e-9:
        return x[0].copy() if mono_input else x.copy()

    sample_rate = float(sample_rate)
    n = x.shape[1]
    frame_len = max(64, int(round(sample_rate * frame_ms / 1000.0)))
    if frame_len % 2:
        frame_len += 1
    hop = frame_len // 2  # 输出重叠相加步进（50% overlap）
    delta = max(1, int(round(sample_rate * search_ms / 1000.0)))

    # 输入过短（不足一个分析帧 + 搜索窗）：原样返回，避免退化
    if n < frame_len + hop + delta:
        return x[0].copy() if mono_input else x.copy()

    # 相似度参考：声道混合单声道（多声道共用同一对齐，保立体声像）
    reference = x.mean(axis=0) if x.shape[0] > 1 else x[0]

    stretch = 1.0 / speed
    out_len = int(round(n * stretch)) + frame_len
    out = np.zeros((x.shape[0], out_len), dtype=np.float32)
    norm = np.zeros(out_len, dtype=np.float32)
    window = np.hanning(frame_len).astype(np.float32)

    # 分析位置按 hop * speed 前进：speed>1 时每次在源上跳更远
    ana_hop = hop * speed
    pos_ana = 0.0
    out_pos = 0
    template: Optional[np.ndarray] = None

    while True:
        nominal = int(round(pos_ana))
        if nominal + frame_len + hop >= n or out_pos + frame_len > out_len:
            break
        if template is None:
            k = 0
        else:
            k = _best_offset(
                reference, template, nominal, delta, n - frame_len
            )
        start = max(0, min(nominal + k, n - frame_len))
        seg = x[:, start : start + frame_len]
        out[:, out_pos : out_pos + frame_len] += seg * window[np.newaxis, :]
        norm[out_pos : out_pos + frame_len] += window
        # 下一帧模板 = 本帧在源中的自然延续（相隔 hop，与速度无关）
        t_start = start + hop
        template = reference[t_start : t_start + hop]
        pos_ana += ana_hop
        out_pos += hop

    norm[norm < 1e-6] = 1.0
    result = out / norm[np.newaxis, :]
    target_len = max(1, int(round(n * stretch)))
    result = result[:, :target_len]
    if mono_input:
        result = result[0]
    return result


class StreamingWsola:
    """F3.2 · 流式 WSOLA 保调变速状态机（实时音频回调用）。

    与离线 ``wsola_time_stretch`` 同参数（25ms 帧 / 7.5ms 搜索 /
    50% Hann 重叠 / 多声道共用对齐偏移），输出按块 pull：

    - 有界内存：输入 FIFO 只保留 ``[pos_ana - delta, …]`` 窗口；输出
      OLA 区在 emit 后裁剪。内部峰值 ≈ 一个 push 块 + delta + 4*hop。
    - 算法延迟 ≈ (delta + 2*hop)/sr ≈ 32ms @48k：首块由调用方多推
      margin 补偿（推 n*speed + margin 后立即可拉 n）。
    - 状态清理：``reset()``（seek/loop/暂停）全清；``set_speed()``
      （倍率突变）只更新步进、保留对齐模板（实测清模板产生可闻脉冲，
      违背"切换无爆音"验收——语义偏离已在方法 docstring 与台账登记）。
    - 回调安全：pull/push 内不做解码、无无界分配（增长按帧粒度，
      裁剪在 emit 后）。

    License：自研 numpy WSOLA，无第三方许可约束。
    """

    def __init__(
        self,
        channels: int = 1,
        sample_rate: float = 48000.0,
        speed: float = 1.0,
        *,
        frame_ms: float = DEFAULT_FRAME_MS,
        search_ms: float = DEFAULT_SEARCH_MS,
    ) -> None:
        speed = float(speed)
        if speed < MIN_SPEED or speed > MAX_SPEED:
            raise ValueError(
                f"speed must be between {MIN_SPEED} and {MAX_SPEED}: {speed}")
        self._channels = max(1, int(channels))
        self._speed = speed
        self._frame_len = max(64, int(round(float(sample_rate) * frame_ms / 1000.0)))
        if self._frame_len % 2:
            self._frame_len += 1
        self._hop = self._frame_len // 2
        self._delta = max(1, int(round(float(sample_rate) * search_ms / 1000.0)))
        self._window = np.hanning(self._frame_len).astype(np.float32)

        self.reset()

    # ── 参数 ─────────────────────────────────────────────────────────

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def algorithmic_latency_samples(self) -> int:
        """首输出就绪前需预推的输入样本数（读前补偿量）。"""
        return self._delta + 2 * self._hop

    # ── 状态管理 ──────────────────────────────────────────────────────

    def reset(self) -> None:
        """全清时域状态（seek / loop / 暂停后恢复）。"""
        self._in = np.zeros((self._channels, 0), dtype=np.float32)
        self._in_mono = np.zeros(0, dtype=np.float32)
        self._in_origin = 0        # _in[0] 的绝对输入索引
        self._pos_ana = 0.0        # 绝对分析位置
        self._out = np.zeros((self._channels, 0), dtype=np.float32)
        self._norm = np.zeros(0, dtype=np.float32)
        self._out_origin = 0       # _out[0] 的绝对输出索引
        self._out_write = 0        # 下一帧的绝对输出落点
        self._emitted = 0          # 已 emit 的绝对输出索引
        self._template: Optional[np.ndarray] = None
        self._total_in = 0

    def set_speed(self, speed: float) -> None:
        """倍率突变：更新步进并**保留**对齐模板。

        计划原文"倍率突变清理时域状态"——实测清模板会让下一帧失去对齐
        搜索（k=0 任意相位），OLA 重叠区产生可闻脉冲（见
        test_speed_switch_no_click），与验收"切换无异常脉冲/爆音"冲突。
        因此取可实现语义：对齐模板与已完成帧保留连续（输出无洞），
        只有新步进生效；seek/loop/暂停仍经 ``reset()`` 全清时域状态。
        """
        speed = float(speed)
        if speed < MIN_SPEED or speed > MAX_SPEED:
            raise ValueError(
                f"speed must be between {MIN_SPEED} and {MAX_SPEED}: {speed}")
        if abs(speed - self._speed) < 1e-9:
            return
        self._speed = speed

    # ── 流式接口 ──────────────────────────────────────────────────────

    def push(self, chunk: np.ndarray) -> None:
        """追加输入块（(channels, n) 或 (n,)；声道数须与构造一致）。"""
        x = np.asarray(chunk, dtype=np.float32)
        if x.ndim == 1:
            x = x[np.newaxis, :]
        if x.ndim != 2 or x.shape[0] != self._channels or x.shape[1] == 0:
            raise ValueError(
                f"expected ({self._channels}, n) chunk, got {x.shape}")
        self._trim_input()
        self._in = np.concatenate([self._in, x], axis=1)
        mono = x.mean(axis=0) if self._channels > 1 else x[0]
        self._in_mono = np.concatenate([self._in_mono, mono])
        self._total_in += x.shape[1]

    def available(self) -> int:
        """当前可 emit 的输出样本数（不产生新解码）。"""
        self._refill(final=False)
        return max(0, self._out_write - self._emitted)

    def pull(self, n: int) -> np.ndarray:
        """取出至多 n 个已定结算的输出样本（不足时返回实际数量）。"""
        n = max(0, int(n))
        if n == 0:
            return np.zeros((self._channels, 0), dtype=np.float32)
        self._refill(final=False)
        take = min(n, self._out_write - self._emitted)
        if take <= 0:
            return np.zeros((self._channels, 0), dtype=np.float32)
        start = self._emitted - self._out_origin
        seg = self._out[:, start:start + take]
        norm = self._norm[start:start + take]
        norm = np.where(norm < 1e-6, 1.0, norm)
        result = (seg / norm[np.newaxis, :]).copy()
        self._emitted += take
        self._trim_output()
        return result

    def flush(self) -> np.ndarray:
        """输入流结束：以收缩搜索放置剩余帧并 emit 全部（含未定结算尾）。"""
        self._refill(final=True)
        start = self._emitted - self._out_origin
        if start >= self._out.shape[1]:
            return np.zeros((self._channels, 0), dtype=np.float32)
        seg = self._out[:, start:]
        norm = np.where(self._norm[start:] < 1e-6, 1.0, self._norm[start:])
        result = (seg / norm[np.newaxis, :]).copy()
        self._emitted = self._out_origin + self._out.shape[1]
        self._trim_output()
        return result

    def pending_input(self) -> int:
        """内部滞留的未消费输入样本数（诊断/内存断言用）。"""
        return int(self._in.shape[1])

    # ── 内部 ──────────────────────────────────────────────────────────

    def _have_input(self) -> int:
        """绝对输入索引空间内的可用末尾（= 已推入的绝对总量）。"""
        return self._in_origin + self._in.shape[1]

    def _trim_input(self) -> None:
        keep_from = max(0, int(self._pos_ana) - self._delta)
        drop = keep_from - self._in_origin
        if drop > 0:
            self._in = self._in[:, drop:]
            self._in_mono = self._in_mono[drop:]
            self._in_origin = keep_from

    def _trim_output(self) -> None:
        drop = self._emitted - self._out_origin
        if drop > 0:
            self._out = self._out[:, drop:]
            self._norm = self._norm[drop:]
            self._out_origin = self._emitted

    def _ensure_out_capacity(self, end_abs: int) -> None:
        need = end_abs - self._out_origin
        if need > self._out.shape[1]:
            grow = need - self._out.shape[1]
            self._out = np.concatenate([
                self._out,
                np.zeros((self._channels, grow), dtype=np.float32),
            ], axis=1)
            self._norm = np.concatenate([
                self._norm, np.zeros(grow, dtype=np.float32)])

    def _refill(self, final: bool) -> None:
        """按当前输入尽量放置 OLA 帧（纯内存运算，无解码）。"""
        while True:
            nominal = int(self._pos_ana)
            if self._template is None:
                need = nominal + self._frame_len
            else:
                need = nominal + self._delta + 2 * self._hop
            have = self._have_input()
            if final and have < need:
                # 尾块：收缩需求继续放置，直到连一个完整帧都不够
                if have < nominal + self._frame_len:
                    break
                need = have
            elif have < need:
                break
            self._place_frame(nominal, final=final)

    def _place_frame(self, nominal: int, final: bool) -> None:
        n_avail = self._have_input()
        max_start = max(0, n_avail - self._frame_len)
        if self._template is None:
            k = 0
        else:
            lo = max(0, nominal - self._delta)
            hi = min(max_start, nominal + self._delta)
            if hi <= lo:
                k = 0
            else:
                base = lo - self._in_origin
                ref = self._in_mono[base:]
                corr = np.correlate(
                    ref[: hi - lo + self._template.shape[-1]],
                    self._template, mode="valid")
                k = (int(lo + int(np.argmax(corr))) - nominal) if corr.size else 0
        start = max(0, min(nominal + k, max_start))
        s0 = start - self._in_origin
        seg = self._in[:, s0:s0 + self._frame_len]
        w = self._out_write - self._out_origin
        self._ensure_out_capacity(self._out_write + self._frame_len)
        self._out[:, w:w + self._frame_len] += seg * self._window[np.newaxis, :]
        self._norm[w:w + self._frame_len] += self._window
        t0 = s0 + self._hop
        self._template = self._in_mono[t0:t0 + self._hop].copy()
        self._pos_ana += self._hop * self._speed
        self._out_write += self._hop
