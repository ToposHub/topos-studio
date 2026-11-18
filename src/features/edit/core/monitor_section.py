"""P1 · 输入监听通道条（2026-11-19 计划第二阶段 · 监控与录音分离）。

录音链路的监听控制层——**只作用于监听输出**（``on_monitor_block`` 转发
的设备块），录制的 take 保持监听前信号（监听状态对录音/导出零副作用）：

- 独立监听音量（dB）、Mono 折叠、DIM（默认 −20 dB 衰减）、Mute；
- PFL/AFL 语义位（pre/post fader listen）——v1 由调用方在 tap 点选择，
  本类只携带标志与电平表读数；
- 参考校准（``reference_db``）：监听链路的 0 dB 参考标注，供声压级
  对齐诊断（不改数字增益）。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

import numpy as np

#: DIM 默认衰减（dB；控制室惯例 −20）。
DEFAULT_DIM_DB = -20.0


@dataclass(frozen=True)
class MonitorState:
    """监听通道条的纯数据状态（可序列化快照）。"""

    volume_db: float = 0.0
    mono: bool = False
    dim: bool = False
    muted: bool = False
    pfl: bool = False          # pre-fader listen（tap 点由调用方选择）
    afl: bool = False          # after-fader listen
    solo_safe: bool = False    # solo-safe（不被他轨 solo 静音）
    reference_db: float = 0.0  # 监听参考校准标注（SPL 对齐诊断用）

    def with_volume_db(self, value: float) -> "MonitorState":
        return replace(self, volume_db=float(value))

    def linear_gain(self) -> float:
        if self.muted:
            return 0.0
        gain_db = self.volume_db + (DEFAULT_DIM_DB if self.dim else 0.0)
        return 10.0 ** (gain_db / 20.0)

    def to_dict(self) -> dict:
        return {
            "volume_db": self.volume_db, "mono": self.mono, "dim": self.dim,
            "muted": self.muted, "pfl": self.pfl, "afl": self.afl,
            "solo_safe": self.solo_safe, "reference_db": self.reference_db,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MonitorState":
        fields = {f: data.get(f, getattr(cls(), f))
                  for f in ("volume_db", "mono", "dim", "muted",
                            "pfl", "afl", "solo_safe", "reference_db")}
        return cls(**fields)


class MonitorSection:
    """监听通道条：对监听块应用 Mono/DIM/音量/静音（不改录音路径）。"""

    def __init__(self, state: Optional[MonitorState] = None) -> None:
        self._state = state or MonitorState()

    @property
    def state(self) -> MonitorState:
        return self._state

    def set_state(self, state: MonitorState) -> None:
        self._state = state

    def process(self, block: np.ndarray) -> np.ndarray:
        """应用监听矩阵（返回新数组；输入不变）。

        ``block``: 监听域 planar float (channels, n)。Mono 折叠先做
        （声道合并听感），再统一增益——顺序与控制室通道条一致。
        """
        if block is None or block.size == 0:
            return block
        out = block
        if self._state.mono and out.shape[0] > 1:
            mono = out.mean(axis=0, keepdims=True)
            out = np.repeat(mono, out.shape[0], axis=0)
        gain = self._state.linear_gain()
        if gain == 0.0:
            return np.zeros_like(out)
        if gain != 1.0:
            out = out * np.float32(gain)
        return np.ascontiguousarray(out, dtype=np.float32)
