"""B10 · 统一控制室监听服务（主计划 §20/B10）。

职责：在最终设备路由之前选择监听源（PFL/AFL/SIP/Program/Input）、应用
mono/DIM/Mute 与监听音量——**只影响监听输出**，绝不改写 Master/Stem/
录音源内容（监听隔离不变量由测试锁定）。

- PFL：轨道推子前 tap；AFL：推子后 tap；SIP 消费共享节目图已按路由
  可听闭包和 solo-safe 规则处理后的 Master。PFL/AFL 不修改节目可听集合。
- mono 经布局矩阵（stereo=(L+R)/2 双声道同值）；非立体声布局必须显式
  注册矩阵，缺失时诊断拒绝，不得对 5.1 简单 mean（Center/LFE 语义错）。
- DIM/Mute/音量切换用短增益斜坡（默认 5ms，可配置），斜坡包络由测试
  严格核对；零输入不产生非零输出。
- ``MonitorSessionState`` 是本机会话状态（不进工程 undo/交付图）；每轨
  ``solo_safe`` 是声音模型字段（SetTrackSoloSafeCommand 提交）。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, replace
from typing import Dict, List, Mapping, Optional, Tuple

import numpy as np

from src.features.edit.core.realtime_playback.audio_ring_buffer import AudioRingBuffer

logger = logging.getLogger(__name__)

MONITOR_SESSION_STATE_VERSION = 1

#: 监听源选择（source_tap 合法值）
MONITOR_SOURCES = ("program", "pfl", "afl", "sip", "input")

DEFAULT_DIM_DB = -20.0
DEFAULT_RAMP_MS = 5.0
MIN_MONITOR_GAIN_DB = -90.0
MAX_MONITOR_GAIN_DB = 0.0


@dataclass(frozen=True)
class ControlRoomPatch:
    """Map logical speaker slots to zero based channels of a local device."""

    device_id: bytes = b""
    channels: Tuple[int, ...] = ()
    device_channels: int = 2
    trim_db: Tuple[float, ...] = ()

    def __post_init__(self) -> None:
        if type(self.device_channels) is not int or not 1 <= self.device_channels <= 32:
            raise ValueError("output requires between 1 and 32 device channels")
        if self.channels and len(self.channels) not in (1, 2, 4, 6, 8):
            raise ValueError("speaker patch requires 1, 2, 4, 6 or 8 channels")
        if len(set(self.channels)) != len(self.channels) or any(
            type(channel) is not int or channel < 0 or channel >= self.device_channels
            for channel in self.channels
        ):
            raise ValueError("output channel numbers must be unique and in range")
        if self.trim_db and (len(self.trim_db) != len(self.channels) or any(
            not math.isfinite(value) or not -24.0 <= value <= 0.0
            for value in self.trim_db
        )):
            raise ValueError("speaker trims require one value per speaker from -24 to 0 dB")

    def apply(self, samples: np.ndarray) -> np.ndarray:
        if not self.channels:
            return samples
        if samples.shape[0] != len(self.channels):
            raise ValueError("monitor patch and rendered speaker count differ")
        output = np.zeros((self.device_channels, samples.shape[1]), dtype=np.float32)
        output[list(self.channels)] = samples
        for channel, trim in zip(self.channels, self.trim_db):
            if trim != 0.0:
                output[channel] *= np.float32(10.0 ** (trim / 20.0))
        return output

    @classmethod
    def load_local(cls) -> ControlRoomPatch:
        from PyQt6.QtCore import QSettings

        settings = QSettings("ToposColor", "ControlRoom")
        data = settings.value("speaker_patch_v1")
        if isinstance(data, dict):
            try:
                return cls(bytes.fromhex(data["device_id"]), tuple(data["channels"]),
                           data["device_channels"], tuple(data["trim_db"]))
            except (KeyError, TypeError, ValueError):
                logger.warning("Ignoring invalid local control room speaker patch")
        return cls()

    def save_local(self) -> None:
        from PyQt6.QtCore import QSettings

        QSettings("ToposColor", "ControlRoom").setValue("speaker_patch_v1", {
            "device_id": self.device_id.hex(), "channels": list(self.channels),
            "device_channels": self.device_channels, "trim_db": list(self.trim_db),
        })


class ControlRoomInput:
    """Bounded listen-only sample queue from a shared Qt capture source."""

    def __init__(self, sample_rate: int, channels: int) -> None:
        self.sample_rate = sample_rate
        self._ring = AudioRingBuffer(channels, max(256, sample_rate // 5))

    def push(self, pcm: np.ndarray) -> None:
        self._ring.write(pcm[:, -self._ring.capacity:])

    @property
    def available_samples(self) -> int:
        return self._ring.filled

    def read(self, samples: int, sample_rate: int) -> Optional[np.ndarray]:
        if sample_rate != self.sample_rate:
            return None
        pcm = self._ring.read(samples)
        if pcm.shape[-1] == samples:
            return pcm
        result = np.zeros((pcm.shape[0], samples), dtype=np.float32)
        result[:, :pcm.shape[-1]] = pcm
        return result

    def clear(self) -> None:
        self._ring.clear()


@dataclass(frozen=True)
class MonitorSessionState:
    """本机控制室会话状态（version=1；不序列化进工程）。"""

    source_tap: str = "program"
    gain_db: float = 0.0
    dim_db: float = DEFAULT_DIM_DB
    dim: bool = False
    muted: bool = False
    mono: bool = False
    mono_matrix_id: str = ""            # 布局矩阵注册键（空=仅 stereo 内置）
    selected_listen_ids: Tuple[str, ...] = ()   # PFL/AFL/SIP 目标 track_id
    output_patch_id: str = ""
    reference_profile_id: str = ""
    ramp_ms: float = DEFAULT_RAMP_MS

    def __post_init__(self) -> None:
        if self.source_tap not in MONITOR_SOURCES:
            raise ValueError(
                f"source_tap must be one of {MONITOR_SOURCES}: "
                f"{self.source_tap!r}")

        if not all(math.isfinite(float(value)) for value in
                   (self.gain_db, self.dim_db, self.ramp_ms)):
            raise ValueError("monitor gain and ramp must be finite")
        if self.ramp_ms < 0.0:
            raise ValueError("monitor ramp must be nonnegative")
        try:
            gain = 10.0 ** ((float(self.gain_db) +
                            (float(self.dim_db) if self.dim else 0.0)) / 20.0)
        except OverflowError as exc:
            raise ValueError("monitor gain is outside supported range") from exc
        if not math.isfinite(gain):
            raise ValueError("monitor gain must be finite")

    def target_gain(self) -> float:
        """目标线性增益（mute=0；dim 叠加 dim_db；与监听音量相乘）。"""
        if self.muted:
            return 0.0
        return 10.0 ** ((float(self.gain_db) +
                         (float(self.dim_db) if self.dim else 0.0)) / 20.0)


@dataclass
class _RampState:
    """增益斜坡的持续状态（跨块推进；seek 由 reset 重置）。"""
    last_gain: float = 1.0
    ramp_samples_left: int = 0
    ramp_from: float = 1.0


def stereo_mono_matrix(channels: int) -> np.ndarray:
    """stereo mono 矩阵（(C_out=2, C_in)：L/R 各取 0.5 等功率合成）。

    仅对 channels==2 合法；其他布局必须注册专用矩阵（B06 合同）。
    """
    if channels != 2:
        raise ValueError(f"stereo mono matrix only defined for 2ch, got {channels}")
    return np.full((2, 2), 0.5, dtype=np.float64)


class MonitorService:
    """控制室监听渲染（在设备路由前；状态本机持有，线程边界由调用方保证）。"""

    def __init__(self, sample_rate: float,
                 mono_matrices: Optional[Dict[str, np.ndarray]] = None) -> None:
        self._sample_rate = float(sample_rate)
        if not math.isfinite(self._sample_rate) or self._sample_rate <= 0.0:
            raise ValueError("monitor sample rate must be finite and positive")
        self._mono_matrices: Dict[str, np.ndarray] = {}
        for matrix_id, matrix in (mono_matrices or {}).items():
            self.register_mono_matrix(matrix_id, matrix)
        self._state = MonitorSessionState()
        self._ramp = _RampState()

    @property
    def state(self) -> MonitorSessionState:
        return self._state

    def set_state(self, state: MonitorSessionState) -> None:
        """更新会话状态；增益变化触发短斜坡（不占工程 undo）。"""
        if self._state.target_gain() != state.target_gain():
            ramp_samples = max(1, int(self._sample_rate
                                      * max(0.0, float(state.ramp_ms)) / 1000.0))
            self._ramp = _RampState(
                last_gain=self._ramp.last_gain, ramp_samples_left=ramp_samples,
                ramp_from=self._ramp.last_gain)
        self._state = state

    def reset(self) -> None:
        """seek/关闭工程：清斜坡状态（旧位置增益不得复活）。"""
        self._ramp = _RampState(last_gain=self._state.target_gain())

    def register_mono_matrix(self, matrix_id: str, matrix: np.ndarray) -> None:
        value = np.array(matrix, dtype=np.float64, copy=True)
        if not matrix_id or value.ndim != 2 or min(value.shape) < 1 or not np.isfinite(value).all():
            raise ValueError("mono matrix must be a finite nonempty 2D array with an id")
        value.setflags(write=False)
        self._mono_matrices[str(matrix_id)] = value

    # ── 监听源选择 ────────────────────────────────────────────────────

    def _select_tap_ids(
        self,
        program_taps: Mapping[str, np.ndarray],
        audible_solo: Optional[set],
        solo_safe: Tuple[str, ...],
    ) -> Tuple[str, ...]:
        """按 source_tap 选择参与求和的 tap 键。

        tap 键约定：``track:<id>:pre``（推子前）/``track:<id>:post``（推子
        后）/``master``（节目）/``input``（录音输入）。
        """
        source = self._state.source_tap
        if source == "program":
            return ("master",)
        if source == "input":
            return tuple(sorted(
                k for k in program_taps if k.startswith("input")))
        selected = tuple(dict.fromkeys(self._state.selected_listen_ids))
        if source == "pfl":
            return tuple(f"track:{tid}:pre" for tid in selected
                         if f"track:{tid}:pre" in program_taps)
        if source == "afl":
            return tuple(f"track:{tid}:post" for tid in selected
                         if f"track:{tid}:post" in program_taps)
        # SIP is the programme graph after solo/solo-safe evaluation. Summing
        # every post tap would double-count both source and bus, bypass master
        # processing and ignore send levels. Only the shared graph can produce
        # that signal; audible_solo/solo_safe are descriptive here, not a second
        # independent routing implementation.
        return ("master",)

    # ── 渲染 ──────────────────────────────────────────────────────────

    def render_block(
        self,
        program_taps: Mapping[str, np.ndarray],
        input_taps: Optional[Mapping[str, np.ndarray]] = None,
        *,
        audible_solo: Optional[set] = None,
        solo_safe: Tuple[str, ...] = (),
    ) -> np.ndarray:
        """渲染一个监听块；输入数组**只读**（返回新数组）。

        program_taps: {tap_key: (channels, n)}；input_taps 键约定
        ``input:<device_session_id>``。选中的 PFL/AFL tap 求和，Program/SIP
        保留共享 Master，随后经 mono 矩阵与增益斜坡输出。
        """
        taps: Dict[str, np.ndarray] = dict(program_taps)
        for key, buf in (input_taps or {}).items():
            taps[f"input:{key}"] = buf
        selected = self._select_tap_ids(taps, audible_solo, solo_safe)

        # 先收集选中的 tap（通道数取首个有效 tap；输出长度 = 最大宽度，
        # 不再随 tap 迭代顺序截长——长 tap 的尾部不得丢弃）。
        selected_bufs: List[Tuple[str, np.ndarray]] = []
        out_ch: Optional[int] = None
        n = 0
        for key in selected:
            buf = taps.get(key)
            if buf is None or buf.size == 0:
                continue
            if buf.ndim != 2 or buf.shape[0] < 1:
                raise ValueError(f"monitor tap {key!r} must be planar PCM")
            selected_bufs.append((key, buf))
            n = max(n, buf.shape[-1])
            if out_ch is None:
                out_ch = buf.shape[0]
        if not selected_bufs or n == 0:
            # No selected tap still consumes a complete device block. Keep
            # its layout/clock and advance any gain transition through silence.
            template = taps.get("master")
            if template is None:
                template = next(iter(taps.values()), np.zeros((2, 1), np.float32))
            out_ch, n = template.shape

        out = np.zeros((out_ch, n), dtype=np.float32)
        for key, buf in selected_bufs:
            if buf.shape[0] != out.shape[0]:
                # 布局不一致的 tap 不静默相加（B06 合同：显式矩阵处理）
                raise ValueError(
                    f"monitor tap {key!r} layout {buf.shape[0]} != "
                    f"{out.shape[0]}; explicit layout conversion required")
            width = min(out.shape[-1], buf.shape[-1])
            out[:, :width] += buf[:, :width]

        # mono（布局矩阵；缺矩阵显式拒绝，不猜）
        if self._state.mono:
            matrix_id = self._state.mono_matrix_id
            if matrix_id:
                matrix = self._mono_matrices.get(matrix_id)
                if matrix is None:
                    raise ValueError(
                        f"mono matrix {matrix_id!r} not registered")
                if matrix.shape[1] != out.shape[0]:
                    raise ValueError(
                        f"mono matrix {matrix_id!r} input channels "
                        f"{matrix.shape[1]} != tap {out.shape[0]}")
                out = (matrix @ out.astype(np.float64)).astype(np.float32)
            else:
                if out.shape[0] != 2:
                    raise ValueError(
                        f"mono fold requires a registered matrix for "
                        f"{out.shape[0]}ch (stereo builtin only)")
                out = (stereo_mono_matrix(2) @ out.astype(np.float64)).astype(
                    np.float32)

        # 增益（短斜坡：从上一块目标增益滑向本块目标增益）。
        # 斜坡位置按剩余总样本数定义（全局进度），不按块重锚——块长
        # 小于斜坡长度时（如 64 样本设备缓冲 vs 5ms 斜坡）不得把整段
        # 斜坡压缩进首块。
        target = float(self._state.target_gain())
        ramp = self._ramp
        if ramp.ramp_samples_left > 0 and ramp.ramp_from != target:
            total = int(ramp.ramp_samples_left)
            count = min(total, out.shape[-1])
            if count > 0:
                gains = (ramp.ramp_from + (target - ramp.ramp_from)
                         * np.arange(1, count + 1, dtype=np.float64) / total)
                envelope = np.full(out.shape[-1], target, dtype=np.float64)
                envelope[:count] = gains
                out = out * envelope[np.newaxis, :].astype(np.float32)
                ramp.ramp_samples_left = total - count
                ramp.last_gain = float(gains[-1])
                ramp.ramp_from = float(gains[-1])
            else:
                ramp.last_gain = target
        else:
            ramp.last_gain = target
            if target == 0.0:
                out = np.zeros_like(out)
            elif target != 1.0:
                out = out * np.float32(target)
        return np.ascontiguousarray(out, dtype=np.float32)


def render_speaker_block(
    service: MonitorService, state: MonitorSessionState, patch: ControlRoomPatch,
    device_channels: int, taps: Mapping[str, np.ndarray],
) -> np.ndarray:
    """Adapt canonical tap layouts to the local speaker patch and apply controls."""
    from src.features.edit.core.channel_layout import ChannelLayout, route_pcm

    layout = ChannelLayout.from_channel_count(len(patch.channels) or device_channels)
    if state.mono and layout.channel_count > 2:
        # Mono goes through the layout's explicit fold, including its LFE policy.
        target = ChannelLayout.STEREO if layout == ChannelLayout.QUAD else layout
        routed = {key: route_pcm(route_pcm(route_pcm(block, ChannelLayout.MONO), target), layout)
                  for key, block in taps.items()}
        state = replace(state, mono=False)
    else:
        routed = {key: route_pcm(block, layout) for key, block in taps.items()}
        if layout.channel_count == 1:
            state = replace(state, mono=False)
    service.set_state(state)
    return patch.apply(service.render_block(routed))


__all__ = [
    "DEFAULT_DIM_DB",
    "DEFAULT_RAMP_MS",
    "MONITOR_SESSION_STATE_VERSION",
    "MONITOR_SOURCES",
    "MonitorService",
    "MonitorSessionState",
    "stereo_mono_matrix",
]
