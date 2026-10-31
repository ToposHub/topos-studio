"""
Side-chain Ducker / Auto-Ducking（AM-08 阶段 4.3，对话修复/ducking）。

专业影视后期常用：当对话/旁白出现时，自动压低背景音乐（ducking），
对话结束平滑恢复。本模块实现基于 side-chain 的自动 ducking。

核心算法（side-chain compressor）：
- side-chain 输入（speech）的包络驱动 gain reduction。
- 当 speech 超过 threshold 时，music 按 ratio 衰减（duck）。
- attack/release 控制压低/恢复速度，避免突变（click）。
- 连续状态（跨块），与 CompressorEffect 一致的包络跟随。

用法（auto-ducking）：
    ducker = SideChainDucker(sample_rate)
    ducker.set_params(target_duck_db=-6.0, threshold_db=-30.0, attack_ms=20, release_ms=500)
    for each block:
        music_out = ducker.process(music_block, sidechain=speech_block)

设计原则（计划表 4.3）：
- speech 出现期间 music 按目标 duck dB 衰减。
- 离线/实时一致（纯 DSP，无随机性）。
- AI 失败不阻塞播放（AI dialogue isolation 是可选增强，本模块是确定性 DSP）。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

import numpy as np


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Side-chain Ducker 参数
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DuckerParams:
    """Side-chain ducker 参数（不可变）。

    Attributes:
        enabled: 是否启用
        threshold_db: side-chain（speech）触发阈值（dB）。speech 超过此值时开始 duck。
        target_duck_db: 目标衰减量（dB，负值）。duck 触发时 music 衰减此值。
            典型：-3 (轻度) / -6 (标准) / -12 (深度)。
        attack_ms: duck 启动时间（ms，speech 出现时压低速度）。典型 5-50ms。
        release_ms: duck 释放时间（ms，speech 结束后恢复速度）。典型 200-1000ms。
        sidechain_source: side-chain 来源标识（如 "track:t1" 或 "bus:dialogue"）。
            引擎据此路由 side-chain 信号。
        hold_ms: 保持时间（ms）：speech 低于阈值后，保持 duck 状态多久再开始释放。
            避免短暂停顿导致的"呼吸感"。
    """
    enabled: bool = True
    threshold_db: float = -30.0
    target_duck_db: float = -6.0
    attack_ms: float = 20.0
    release_ms: float = 500.0
    sidechain_source: str = ""
    hold_ms: float = 100.0

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "threshold_db": self.threshold_db,
            "target_duck_db": self.target_duck_db,
            "attack_ms": self.attack_ms,
            "release_ms": self.release_ms,
            "sidechain_source": self.sidechain_source,
            "hold_ms": self.hold_ms,
        }

    @staticmethod
    def from_dict(data: dict) -> "DuckerParams":
        return DuckerParams(
            enabled=bool(data.get("enabled", True)),
            threshold_db=float(data.get("threshold_db", -30.0)),
            target_duck_db=float(data.get("target_duck_db", -6.0)),
            attack_ms=float(data.get("attack_ms", 20.0)),
            release_ms=float(data.get("release_ms", 500.0)),
            sidechain_source=str(data.get("sidechain_source", "")),
            hold_ms=float(data.get("hold_ms", 100.0)),
        )

    @staticmethod
    def default() -> "DuckerParams":
        return DuckerParams()


# ─────────────────────────────────────────────────────────────────────────────
# SideChainDucker：连续状态 side-chain 压缩器
# ─────────────────────────────────────────────────────────────────────────────

class SideChainDucker:
    """Side-chain ducker（自动 ducking，AM-08 阶段 4.3）。

    用 side-chain 信号（speech）的包络驱动 music 的 gain reduction。
    与 CompressorEffect 的区别：
    - side-chain 输入独立于被处理信号（不是自驱动）。
    - 语义是"duck"（speech 出现 → music 衰减），不是"压缩动态"。
    - 增益范围固定为 [target_duck_db, 0]（不放大，只衰减）。

    连续状态：包络跨块累积，与实时播放逐块一致。
    """

    def __init__(self, sample_rate: float = 48000.0) -> None:
        self._sample_rate = float(sample_rate)
        self._params: DuckerParams = DuckerParams.default()
        # 连续状态
        self._gain_db: float = 0.0  # 当前增益（0=不衰减，负=衰减）
        self._hold_counter: float = 0.0  # 保持剩余样本数
        self._attack_coeff: float = self._time_to_coeff(self._params.attack_ms)
        self._release_coeff: float = self._time_to_coeff(self._params.release_ms)
        self._hold_samples: int = self._ms_to_samples(self._params.hold_ms)

    @staticmethod
    def _time_to_coeff(time_ms: float, sample_rate: float = 48000.0) -> float:
        """由时间常数计算每采样指数平滑系数（与 CompressorEffect 一致）。"""
        if time_ms <= 0.0 or sample_rate <= 0.0:
            return 0.0
        return float(np.exp(-1.0 / (time_ms * 0.001 * sample_rate)))

    def _ms_to_samples(self, ms: float) -> int:
        """ms → 样本数。"""
        return int(ms * 0.001 * self._sample_rate)

    def set_params(self, params: Optional[DuckerParams], sample_rate: Optional[float] = None) -> None:
        """更新 ducker 参数。

        Args:
            params: DuckerParams，None=禁用
            sample_rate: 可选新采样率
        """
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)
            self._gain_db = 0.0
            self._hold_counter = 0.0

        if params is None:
            self._params = DuckerParams(enabled=False)
        else:
            self._params = params
        self._attack_coeff = self._time_to_coeff(self._params.attack_ms, self._sample_rate)
        self._release_coeff = self._time_to_coeff(self._params.release_ms, self._sample_rate)
        self._hold_samples = self._ms_to_samples(self._params.hold_ms)

    def process(self, samples: np.ndarray, sidechain: Optional[np.ndarray] = None) -> np.ndarray:
        """对 music 应用 side-chain ducking。

        Args:
            samples: music PCM (channels, n)，被处理信号。
            sidechain: speech PCM (channels, n) 或 None。None=无 duck（直通）。

        Returns:
            ducked music PCM，同 shape。
        """
        if samples.ndim != 2 or samples.shape[1] < 1:
            return samples
        if not self._params.enabled:
            # AUD-18213：duck 中途禁用不得直通。上一块可能仍以 -6dB 输出，
            # 直接 0dB 旁路会产生瞬时增益阶跃（click）。先把残余增益按
            # release 时间常数平滑释放回 0，释放完成后（≤-1e-6dB，听感
            # 不可闻）再真正旁路。
            if self._gain_db < -1e-6:
                return self._smooth_release_to_zero(samples)
            self._gain_db = 0.0
            self._hold_counter = 0.0
            return samples
        if sidechain is None or sidechain.ndim != 2 or sidechain.shape[1] == 0:
            # 无 side-chain 信号：平滑释放回 0（避免突变）
            return self._smooth_release_to_zero(samples)

        n = samples.shape[1]
        out = samples.astype(np.float64, copy=True)

        # side-chain 包络：取 max abs（声道联动），转 dB
        sc_peak = np.max(np.abs(sidechain.astype(np.float64)), axis=0)  # (n,)
        sc_db = 20.0 * np.log10(np.maximum(sc_peak, 1e-9))

        threshold = self._params.threshold_db
        target_duck = self._params.target_duck_db  # 负值
        att_c = self._attack_coeff
        rel_c = self._release_coeff

        gain_db = self._gain_db
        hold_counter = self._hold_counter

        # AUD-18215：逐样本 Python 循环改为按阈值穿越分段（run）的向量化
        # 计算。增益递推是线性指数平滑，段内可用累积乘法一次算出——
        # release/无信号段与逐样本循环位级一致；attack 段因原循环每次
        # "加 target 再减 target"的两次舍入无法闭式重放，与逐样本实现
        # 差 ≤1 ulp（~1e-15 相对误差，远低于 int16 量化步长与既有
        # parity 容差 1e-4）。段数 = 阈值穿越次数 + 1，正常语音/音乐
        # 边界稀疏，消除每秒 48k 次纯 Python 迭代。
        gains = np.empty(n, dtype=np.float64)
        above = sc_db > threshold
        change_points = np.flatnonzero(above[1:] != above[:-1]) + 1
        boundaries = np.concatenate(([0], change_points, [n]))
        for run_start, run_end in zip(boundaries[:-1], boundaries[1:]):
            run_start = int(run_start)
            run_end = int(run_end)
            length = run_end - run_start
            if above[run_start]:
                # speech 激活段：gain = target + (gain - target) * att_c^k，
                # hold 计数器每样本重置为 hold_samples。首样本即完成一次
                # attack 乘法（与逐样本循环的"先乘后存"顺序一致）。
                seq = np.empty(length, dtype=np.float64)
                seq[0] = (gain_db - target_duck) * att_c
                if length > 1:
                    seq[1:] = att_c
                    np.multiply.accumulate(seq, out=seq)
                gains[run_start:run_end] = target_duck + seq
                gain_db = float(gains[run_end - 1])
                hold_counter = float(self._hold_samples)
            else:
                # speech 低于阈值段：先按 hold 计数保持当前 duck，hold
                # 耗尽后按 release 系数向 0 释放。
                held = min(length, max(0, math.ceil(hold_counter)))
                if held > 0:
                    gains[run_start:run_start + held] = gain_db
                    hold_counter -= float(held)
                release_len = length - held
                if release_len > 0:
                    release_start = run_start + held
                    if gain_db < 0.0:
                        seq = np.empty(release_len, dtype=np.float64)
                        seq[0] = gain_db * rel_c
                        if release_len > 1:
                            seq[1:] = rel_c
                            np.multiply.accumulate(seq, out=seq)
                        gains[release_start:run_end] = seq
                        gain_db = float(seq[-1])
                    else:
                        # gain_db >= 0（=0）：0 * rel_c 恒为 0，整段保持
                        gains[release_start:run_end] = 0.0
                        gain_db = 0.0

        self._gain_db = gain_db
        self._hold_counter = hold_counter

        # 应用增益（线性）
        gain_linear = 10.0 ** (gains / 20.0)
        out *= gain_linear[np.newaxis, :]

        return out.astype(samples.dtype, copy=False)

    def _smooth_release_to_zero(self, samples: np.ndarray) -> np.ndarray:
        """无 side-chain 信号时，平滑释放到 0 dB（避免突变 click）。"""
        n = samples.shape[1]
        out = samples.astype(np.float64, copy=True)
        rel_c = self._release_coeff
        gain_db = self._gain_db
        gains = np.empty(n, dtype=np.float64)
        # AUD-18215：与 process() 主路径同款向量化（累积乘法保持逐样本
        # 循环的浮点乘法顺序，位级一致）。
        if gain_db < 0.0 and n > 0:
            seq = np.empty(n, dtype=np.float64)
            seq[0] = gain_db * rel_c
            if n > 1:
                seq[1:] = rel_c
                np.multiply.accumulate(seq, out=seq)
            gains[:] = seq
            self._gain_db = float(seq[-1])
        else:
            # gain_db 已在 0（或块为空）：0 * rel_c 恒为 0，保持不变
            gains[:] = gain_db if gain_db > 0.0 else 0.0
        gain_linear = 10.0 ** (gains / 20.0)
        out *= gain_linear[np.newaxis, :]
        return out.astype(samples.dtype, copy=False)

    def reset_state(self) -> None:
        """重置状态（seek/暂停后调用）。"""
        self._gain_db = 0.0
        self._hold_counter = 0.0

    @property
    def is_active(self) -> bool:
        return self._params.enabled

    @property
    def current_gain_db(self) -> float:
        """当前增益（dB，0=不衰减，负=正在 duck）。供 UI meter 显示。"""
        return self._gain_db


# ─────────────────────────────────────────────────────────────────────────────
# 便捷函数：auto-duck 整段 music
# ─────────────────────────────────────────────────────────────────────────────

def auto_duck(
    music: np.ndarray,
    sidechain: np.ndarray,
    params: DuckerParams,
    sample_rate: float = 48000.0,
) -> np.ndarray:
    """对整段 music 应用 auto-ducking（离线便捷函数）。

    Args:
        music: music PCM (channels, n)
        sidechain: speech PCM (channels, n)，长度需 >= music
        params: DuckerParams
        sample_rate: 采样率

    Returns:
        ducked music PCM
    """
    ducker = SideChainDucker(sample_rate)
    ducker.set_params(params)
    # 对齐长度
    n = music.shape[-1] if music.ndim == 2 else len(music)
    sc = sidechain
    if sc.ndim == 2:
        sc = sc[:, :n]
        sc_len = sc.shape[1]
    else:
        sc = sc[:n]
        sc_len = len(sc)
    if sc_len < n:
        # AUD-18212：sidechain 短于 music 时 process() 的逐样本包络
        # 跟随会越界（IndexError）。音乐长于语音是 ducking 常态（配乐
        # 贯穿、对白稀疏）——以静音补齐：补齐段 speech 远低于阈值，
        # 增益按 release 平滑恢复，语义即"该区间无对白、不 duck"。
        logger.warning(
            "auto_duck: sidechain (%d samples) shorter than music "
            "(%d samples); padding with silence",
            sc_len, n,
        )
        if sc.ndim == 2:
            pad = np.zeros((sc.shape[0], n - sc_len), dtype=sc.dtype)
            sc = np.concatenate([sc, pad], axis=1)
        else:
            pad = np.zeros(n - sc_len, dtype=sc.dtype)
            sc = np.concatenate([sc, pad])
    return ducker.process(music, sidechain=sc)
