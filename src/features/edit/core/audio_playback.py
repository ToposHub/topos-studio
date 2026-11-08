"""
Audio Playback Controller Module
音频播放控制器（与 EditPlaybackController 协同）。

## 职责

- AudioClock: 基于 audio samples 的精确时钟
- AudioTrackState: 单轨状态管理（volume/muted/solo）
- AudioDecoder: 使用 PyAV 解码音频轨道
- AudioMixer: 混合多轨音频（考虑 mute/solo/volume）
- AudioOutput: 音频输出（QtMultimedia QAudioSink）
- AudioPlaybackManager: 整合播放控制

## 设计原则

- Audio clock 主导，视频追 audio clock
- 使用 PyAV 进行精确音频解码
- 支持实时 mute/solo/volume 控制
- 与 EditPlaybackController 协同工作
- 完整类型注解
- 所有 PyQt 信号通过主线程

## 音视频同步策略

1. Audio Clock 作为主时钟：
   - audio_clock = samples_played / sample_rate (秒)
   - 视频帧 = audio_clock * timeline_fps

2. 视频追赶策略：
   - 计算 audio_clock 对应的理想视频帧
   - 如果视频落后，跳帧追赶
   - 如果视频超前，等待音频

3. 与 EditPlaybackController 协同：
   - AudioPlaybackManager 提供时钟源
   - EditPlaybackController.tick() 可获取 audio_clock
   - 或直接让 AudioPlaybackManager 控制帧同步

## 使用示例

```python
from src.features.edit.core.audio_playback import AudioPlaybackManager
from src.features.edit.core.edit_playback_controller import EditPlaybackController

# 创建音频播放控制器
audio_manager = AudioPlaybackManager()
audio_manager.set_sample_rate(48000)

# 创建视频播放控制器
video_controller = EditPlaybackController()
video_controller.set_fps(24.0)

# 协同设置
audio_manager.set_video_controller(video_controller)

# 播放控制
audio_manager.play(start_frame=0, timeline_fps=24.0)
audio_manager.pause()
audio_manager.seek(frame=100)

# 轨道控制
audio_manager.set_track_volume("track_001", 0.8)
audio_manager.set_track_mute("track_001", True)
audio_manager.set_track_solo("track_002", True)
```

## 依赖

- PyAV (av>=10.0.0): 音频解码
- PyQt6.QtMultimedia: 音频输出
"""

from __future__ import annotations

import logging
import math
import sys
import time
import threading
import queue
from dataclasses import dataclass
from itertools import count as _it_count
from typing import TYPE_CHECKING, Iterator, Optional, Callable, Dict, List, Tuple, Any
from enum import Enum
from pathlib import Path
from collections import deque

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from src.features.edit.core.channel_layout import ChannelLayout

import numpy as np

from PyQt6.QtCore import QObject, pyqtSignal, QThread, QMutex, QMutexLocker
from PyQt6.QtMultimedia import QAudioSink, QAudioFormat, QMediaDevices

from src.features.edit.core.playback_debug import playback_trace
from src.features.edit.core.audio_dsp import (
    StripEQFilter,
    EffectChain,
    # AUD-15506: mix 热路径依赖提升为模块级导入，消除逐块函数内 import 的
    # 查找/调用开销（channel_layout 无反向依赖；audio_dsp 本就是本模块依赖）。
    apply_clip_fade,
    pan_gains_for_channels,
    balance_gains,
)
from src.features.edit.core.channel_layout import adapt_channels
from src.features.edit.core.source_patch import SourcePatchSpec
from src.features.edit.core.timeline_v2 import StripEQParams, EffectChainParams
from src.features.edit.core.loudness_meter import LoudnessMeter

# PyAV for audio decoding
try:
    import av
    PYAV_AVAILABLE = True
except ImportError:
    PYAV_AVAILABLE = False
    av = None  # type: ignore

logger = logging.getLogger(__name__)

# B13：电平静音地板——与播放线程 dB 钳制（-100dB）同源。stop/pause
# 广播静音与迟到回调丢弃共用该值，不引入第二套"无声"语义。
LEVEL_SILENT_DB = -100.0


# =============================================================================
# Constants
# =============================================================================

# 默认采样率
DEFAULT_SAMPLE_RATE: int = 48000

# 默认音频缓冲区大小（毫秒）
DEFAULT_BUFFER_MS: int = 200

# 音频输出缓冲区帧数。保持在低延迟区间，避免 QAudioSink 内部排队
# 数百毫秒旧样本导致播放开始/seek 后声音明显落后画面。
AUDIO_OUTPUT_FRAMES: int = 4096
SCRUB_AUDIO_OUTPUT_FRAMES: int = 4096

# 音视频同步阈值（毫秒）
AV_SYNC_THRESHOLD_MS: float = 50.0  # 50ms 差异触发校正

# 最大追赶帧数
MAX_SYNC_CORRECTION_FRAMES: int = 5

# 音量范围
MIN_VOLUME: float = 0.0
MAX_VOLUME: float = 2.0

# 解码队列大小
DECODE_QUEUE_SIZE: int = 10


# =============================================================================
# Audio Resampling Utility
# =============================================================================

# PyAV 高质量重采样器缓存（按 (source_rate, target_rate, channels, instance_id) 键缓存）
# 注意：不能全局共享 resampler，因为每个解码器有独立的状态
_av_resampler_cache: Dict[Tuple[int, int, int, int], Any] = {}
# AUD-15501: 缓存容量上限（FIFO 驱逐最旧条目）。解码器 close() 会按 instance_id
# 前缀主动清理，上限兜底异常路径（close 未被调用/进程长会话）下的无界增长——
# 每个残留 AudioResampler 持有 FIR 历史。被驱逐实例下次 resample 重建，仅损失
# 滤波历史（一次 seek 级瞬态），不崩溃不泄漏。
_AV_RESAMPLER_CACHE_MAX: int = 64
# AUD-15501: 解码器实例 ID 用单调计数器分配。旧实现用 id(self)——CPython 在对象
# 释放后可复用地址，新解码器分到同地址会从缓存拿到旧实例的残留滤波状态，
# clip 开头混入上一个 clip 的滤波尾（可闻杂音）。计数器全进程单调不复用。
_decoder_instance_seq: Iterator[int] = _it_count(1)


def _get_av_resampler(source_rate: int, target_rate: int, channels: int, instance_id: int = 0) -> Any:
    """获取或创建 PyAV AudioResampler 实例（带缓存）。

    Args:
        instance_id: 用于区分不同解码器实例的 ID，避免状态污染
    """
    key = (source_rate, target_rate, channels, instance_id)
    if key not in _av_resampler_cache:
        if PYAV_AVAILABLE:
            layout = "stereo" if channels == 2 else "mono" if channels == 1 else f"{channels}c"
            _av_resampler_cache[key] = av.audio.resampler.AudioResampler(
                format="fltp",
                layout=layout,
                rate=target_rate,
            )
            # AUD-15501: 容量上限——超限时按插入序驱逐最旧条目（dict 保序）。
            while len(_av_resampler_cache) > _AV_RESAMPLER_CACHE_MAX:
                _av_resampler_cache.pop(next(iter(_av_resampler_cache)), None)
        else:
            return None
    return _av_resampler_cache[key]


def _resample_audio(
    samples: np.ndarray,
    source_rate: int,
    target_rate: int,
    instance_id: int = 0,
    flush: bool = False,
) -> np.ndarray:
    """
    使用 PyAV AudioResampler（多相 FIR 滤波器）对音频进行高质量重采样。

    如果 PyAV 不可用，退回到 scipy.signal.resample_poly；
    如果 scipy 也不可用，再退回线性插值（不推荐，仅应急）。

    Args:
        samples: 音频数组，形状为 (channels, num_samples)，float32 [-1, 1]
        source_rate: 源采样率（Hz）
        target_rate: 目标采样率（Hz）
        instance_id: 解码器实例 ID，用于隔离 resampler 状态
        flush: 是否 flush resampler（仅在 seek/结束时使用）

    Returns:
        重采样后的数组，形状为 (channels, new_num_samples)，float32 [-1, 1]
    """
    if source_rate == target_rate:
        return samples

    if samples.ndim == 1:
        samples = samples.reshape(1, -1)

    source_count = samples.shape[-1]
    if source_count == 0:
        return np.zeros((samples.shape[0], 0), dtype=np.float32)

    channels = samples.shape[0]

    # --- 路径 1: PyAV AudioResampler（最佳质量，多相 FIR） ---
    if PYAV_AVAILABLE:
        try:
            resampler = _get_av_resampler(source_rate, target_rate, channels, instance_id)
            if resampler is not None:
                layout = "stereo" if channels == 2 else "mono" if channels == 1 else f"{channels}c"
                # 构造一个 AudioFrame 来喂给 resampler。
                # 必须 ascontiguousarray：astype(order='K') 会保留源数组的
                # 非连续布局（如声道映射切片 .T 的产物），from_ndarray 拒收
                # 非 C-contiguous 输入并抛异常，导致整轨长期落到 scipy 兜底。
                frame = av.AudioFrame.from_ndarray(
                    np.ascontiguousarray(samples, dtype=np.float32),
                    format="fltp", layout=layout,
                )
                frame.sample_rate = source_rate
                out_frames = resampler.resample(frame)
                # 仅在 seek 或结束时 flush，避免每次调用都重置内部状态
                if flush:
                    out_frames += resampler.resample(None)
                    # PyAV 的 AudioResampler flush 后即终结：缓存实例继续喂帧
                    # 会抛 AVERROR_EOF（[Errno 541478725] End of file），
                    # 之后每次调用都落到 scipy resample_poly 兜底（每次
                    # 重算 FIR 滤波器，实测单轨 ~1ms vs PyAV ~0.1ms，多轨
                    # 混音因此掉到实时以下）。seek 后丢弃实例，下次重建。
                    _av_resampler_cache.pop(
                        (source_rate, target_rate, channels, instance_id), None)
                if out_frames:
                    arrays = [f.to_ndarray() for f in out_frames]
                    result = np.concatenate(arrays, axis=-1)
                    return result.astype(np.float32)
        except Exception as e:
            logger.debug(f"PyAV resampler failed ({e}), falling back to scipy")

    # --- 路径 2: scipy.signal.resample_poly（polyphase FIR，高质量） ---
    try:
        from scipy.signal import resample_poly
        from math import gcd
        g = gcd(target_rate, source_rate)
        up = target_rate // g
        down = source_rate // g
        result = np.empty((channels, 0), dtype=np.float32)
        parts = []
        for ch in range(channels):
            parts.append(resample_poly(samples[ch].astype(np.float64), up, down).astype(np.float32))
        result = np.stack(parts, axis=0)
        return result
    except ImportError:
        logger.warning("scipy not available, falling back to linear interpolation resampler")
    except Exception as e:
        logger.warning(f"scipy resample_poly failed ({e}), falling back to linear interpolation")

    # --- 路径 3: 线性插值（最低质量，仅应急） ---
    target_count = int(round(source_count * target_rate / source_rate))
    if target_count <= 0:
        return np.zeros((channels, 0), dtype=np.float32)

    target_indices = np.arange(target_count, dtype=np.float64) * (source_count - 1) / max(1, target_count - 1)
    result = np.empty((channels, target_count), dtype=np.float32)
    for ch in range(channels):
        result[ch] = np.interp(target_indices, np.arange(source_count, dtype=np.float64), samples[ch].astype(np.float64))

    return result


# =============================================================================
# Enums
# =============================================================================

class AudioPlaybackState(Enum):
    """音频播放状态。"""
    STOPPED = "stopped"
    PLAYING = "playing"
    PAUSED = "paused"


class AudioSyncMode(Enum):
    """音视频同步模式。"""
    AUDIO_MASTER = "audio_master"  # 音频主导，视频追音频
    VIDEO_MASTER = "video_master"  # 视频主导，音频追视频（静音模式）
    INDEPENDENT = "independent"    # 独立播放（无同步）


# =============================================================================
# Bus DAG 拓扑排序（AM-14）
# =============================================================================

def topological_sort_buses(
    bus_ids: list,
    routing: dict,
) -> tuple[list, list]:
    """AM-14: 对 Bus 节点做拓扑排序 + 环检测。

    支持多层 Bus→Bus 路由（此前实现硬编码两层，Bus 只能直接到 Master）。
    基于 `_track_routing` 构建有向图，用 Kahn 算法拓扑排序。

    Args:
        bus_ids: 参与排序的 Bus track_id 列表。
        routing: {track_id: {"output_bus_id": Optional[str], "sends": [...], "is_bus": bool}}。
            output_bus_id 与有效的 Bus send 都构成 Bus→Bus 信号边。虽然 send 是
            并行 tap，它仍要求源 Bus 先处理；否则 send 的信号会写入一个已经处理过
            的 Bus，造成当前 block 静默丢失。

    Returns:
        (sorted_bus_ids, cycle_bus_ids):
        - sorted_bus_ids: 拓扑序的 Bus 列表（上游在前）。环中的 Bus 不在此列表。
        - cycle_bus_ids: 检测到环的 Bus（从排序中排除，避免无限循环）。

    环处理：检测到环时，环中的 Bus 全部排除并返回，调用方应记录日志、
    不让运行图崩溃（计划表 1.3："错误包含 node IDs，运行图不崩溃"）。
    未知目标（output_bus_id 指向不存在的 Bus）视为指向 Master（不构成边）。
    """
    bus_set = set(bus_ids)
    # 构建邻接表：bus → 下游 bus。Bus output 与有效 send 都是同一 block 的
    # 信号依赖；二者都必须参与排序与环检测。
    downstream: dict = {bid: [] for bid in bus_ids}
    in_degree: dict = {bid: 0 for bid in bus_ids}
    for bid in bus_ids:
        r = routing.get(bid, {})
        if not isinstance(r, dict):
            continue
        targets: set = set()
        output_target = r.get("output_bus_id")
        if output_target in bus_set:
            targets.add(output_target)
        for send in r.get("sends", []) or []:
            if not isinstance(send, (tuple, list)) or len(send) < 2:
                continue
            send_target, send_level = send[0], send[1]
            if send_target in bus_set and float(send_level) > 0.0:
                targets.add(send_target)
        for target in targets:
            downstream[bid].append(target)
            in_degree[target] = in_degree.get(target, 0) + 1

    # Kahn 算法
    from collections import deque
    queue = deque(bid for bid in bus_ids if in_degree[bid] == 0)
    sorted_ids: list = []
    while queue:
        bid = queue.popleft()
        sorted_ids.append(bid)
        for nxt in downstream[bid]:
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)

    # 剩余 in_degree > 0 的节点在环中
    cycle_ids = [bid for bid in bus_ids if in_degree[bid] > 0]
    return sorted_ids, cycle_ids


def compute_audible_solo_closure(
    track_ids: list,
    routing: dict,
    solo_ids: list,
) -> set:
    """RMA-04: 计算 solo 时的可听节点闭包。

    旧 solo 逻辑（mix_samples/render_bus_stage）的错误：先剔除所有未 solo 的普通轨，
    再剔除所有未 solo 的 Bus。这破坏了可听闭包——
      - solo 一个普通轨（输出到 Bus B → Master）：B 未 solo 被剔除 → 该轨无声；
      - solo 一个 Bus B（接收普通轨 T）：T 未 solo 被剔除 → B 无输入，无声。

    正确语义：solo 某节点时，保留"该节点到 Master 的完整可听路径"。
      - solo 普通轨 → 保留该轨 + 其输出祖先 Bus 链（到 Master）；
      - solo Bus → 保留该 Bus + 其所有上游输入（让 Bus 收到信号）+ 下游到 Master。

    本函数返回"必须保持可听"的 track_id 集合（含普通轨与 Bus）。调用方据此
    判断"未在集合内的节点跳过"，替代旧的 `not state.solo` 单点判断。

    Args:
        track_ids: 所有参与判断的 track_id（普通轨 + Bus）。
        routing: {track_id: {"output_bus_id": Optional[str], "sends": [(bus_id, level), ...],
            "is_bus": bool}}。
        solo_ids: 当前处于 solo 态的 track_id 列表。

    Returns:
        audible: 可听节点集合。无 solo 时返回空集（调用方应据此放行全部节点）。
    """
    if not solo_ids:
        return set()
    solo_set = set(solo_ids)
    track_set = set(track_ids)
    audible: set = set()

    # 1) 下游闭包：每个 solo 节点沿 output_bus_id 走到 Master，路径上的 Bus 都要保留。
    #    （普通轨 solo → 其 output 链上的 Bus 必须保留；Bus solo → 其下游 Bus 链保留。）
    #    send 是并行 tap：solo 节点的 send 目标 Bus 也必须保留（否则 wet 信号丢失），
    #    并从该 send Bus 继续沿 output_bus_id 走到 Master。
    def _walk_downstream(node_id: str) -> None:
        cur = node_id
        seen: set = set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            audible.add(cur)
            r = routing.get(cur, {})
            if isinstance(r, dict):
                # send 目标（并行 tap）也要保留，并继续走其下游
                for send_bus_id, send_level in r.get("sends", []) or []:
                    if send_level > 0.0 and send_bus_id in track_set:
                        _walk_downstream(send_bus_id)
                target = r.get("output_bus_id")
            else:
                target = None
            if target is None or target not in track_set:
                break
            cur = target

    for s in solo_set:
        _walk_downstream(s)

    # 2) 上游闭包：solo 的 Bus 需要其上游输入（普通轨 + 上游 Bus）才能收到信号。
    #    反向遍历 routing：谁把 output 指向（或 send 到）该 solo Bus，谁就是上游。
    #    对这些上游再递归取上游，直到无上游。
    bus_targets = {nid for nid in track_ids
                   if (routing.get(nid, {}).get("is_bus") if isinstance(routing.get(nid), dict) else False)}
    # 反向邻接（仅 output_bus_id 构成串行上游；send 是并行 tap，上游送 solo Bus 也应保留）
    upstream_of: dict = {nid: [] for nid in track_ids}
    for nid in track_ids:
        r = routing.get(nid, {})
        if not isinstance(r, dict):
            continue
        out_target = r.get("output_bus_id")
        if out_target in track_set:
            upstream_of[out_target].append(nid)
        for send_bus_id, send_level in r.get("sends", []) or []:
            if send_bus_id in track_set and send_level > 0.0:
                upstream_of[send_bus_id].append(nid)

    def _walk_upstream(bus_id: str) -> None:
        stack = [bus_id]
        seen: set = set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            for up in upstream_of.get(cur, []):
                if up not in audible:
                    audible.add(up)
                    stack.append(up)

    # 只对*显式 solo* 的 Bus 取上游输入。普通轨 solo 时，它的下游 Bus
    # 只是信号必经路径，不能因此把同一 Bus 的其它输入也放进 solo；那会让
    # “solo 一条轨”意外听到同组的所有轨。Bus 的下游 Bus 同理只是输出路径，
    # 不应反向吸纳其其它输入。
    solo_buses = solo_set & bus_targets
    for bid in solo_buses:
        _walk_upstream(bid)

    return audible


def validate_routing_graph(
    bus_tracks: list,
    routing: dict,
) -> tuple[bool, list]:
    """AM-08 阶段 4.2：验证路由图无环、无未知目标（供 Routing UI 实时告警）。

    在 UI 提交路由变更前调用，避免创建会导致引擎崩溃的环。
    复用 topological_sort_buses 的环检测逻辑。

    Args:
        bus_tracks: 所有 Bus 轨道的 track_id 列表。
        routing: {track_id: {"output_bus_id": Optional[str], "sends": [...], "is_bus": bool}}。

    Returns:
        (is_valid, errors):
        - is_valid: True=无环无未知目标
        - errors: 问题描述列表（供 UI 显示），空列表=有效
    """
    errors: list = []
    bus_set = set(bus_tracks)

    # 1. 环检测
    sorted_ids, cycle_ids = topological_sort_buses(list(bus_set), routing)
    if cycle_ids:
        errors.append(
            f"检测到路由环（cycle）：{', '.join(sorted(cycle_ids))}。"
            f"请检查 Bus 的输出目标，避免 Bus→Bus 循环。"
        )

    # 2. 未知目标检测（output_bus_id 指向不存在的 Bus）
    for tid, r in routing.items():
        if not isinstance(r, dict):
            continue
        target = r.get("output_bus_id")
        if target is not None and target not in bus_set:
            # 目标可能是 Master（None 的语义），但非 None 且不在 bus_set = 未知
            errors.append(
                f"轨道 {tid} 的输出目标 '{target}' 不存在（已被删除或 ID 无效）。"
            )
        # sends 目标检测
        sends = r.get("sends", []) or []
        for s in sends:
            if isinstance(s, (list, tuple)) and len(s) >= 1:
                s_target = s[0]
                if s_target and s_target not in bus_set:
                    errors.append(
                        f"轨道 {tid} 的 Send 目标 '{s_target}' 不存在。"
                    )

    return (len(errors) == 0, errors)


# =============================================================================
# AudioTrackState
# =============================================================================

@dataclass
class AudioTrackState:
    """
    音频轨道状态。

    Attributes:
        track_id: 轨道 ID
        volume: 音量（0.0-2.0，默认 1.0）
        muted: 是否静音
        solo: 是否独奏
        pan: 声像（-1.0 左到 1.0 右，0.0 中）
        enabled: 是否启用
    """
    track_id: str
    volume: float = 1.0
    muted: bool = False
    solo: bool = False
    pan: float = 0.0
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.track_id:
            raise ValueError("track_id cannot be empty")
        if self.volume < MIN_VOLUME or self.volume > MAX_VOLUME:
            raise ValueError(f"volume must be between {MIN_VOLUME} and {MAX_VOLUME}: {self.volume}")
        if self.pan < -1.0 or self.pan > 1.0:
            raise ValueError(f"pan must be between -1.0 and 1.0: {self.pan}")

    def effective_volume(self) -> float:
        """
        计算有效音量。

        考虑 muted 和 solo 状态。

        Returns:
            有效音量（静音返回 0.0）
        """
        # 如果被静音，返回 0
        if self.muted:
            return 0.0

        # 如果有 solo 轨道且当前轨道不是 solo，返回 0
        # （这个逻辑需要在 AudioPlaybackManager 中处理全局 solo 状态）
        return self.volume

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "volume": self.volume,
            "muted": self.muted,
            "solo": self.solo,
            "pan": self.pan,
            "enabled": self.enabled,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> AudioTrackState:
        return AudioTrackState(
            track_id=data["track_id"],
            volume=data.get("volume", 1.0),
            muted=data.get("muted", False),
            solo=data.get("solo", False),
            pan=data.get("pan", 0.0),
            enabled=data.get("enabled", True),
        )


# =============================================================================
# TrackLevel（W9：每轨电平数据，含 peak + RMS）
# =============================================================================

class TrackLevel:
    """每轨电平测量数据（W9/W11）。

    包含 sample peak（峰值）、RMS（均方根，线性振幅）和 LUFS（响度，dB）。
    支持索引访问 [0]/[1]（返回 peak_l/peak_r）以向后兼容旧测试/调用方。

    Attributes:
        peak_l: 左声道 sample peak（0.0..1.0+，线性）
        peak_r: 右声道 sample peak
        rms_l: 左声道 RMS（root-mean-square，线性）
        rms_r: 右声道 RMS
        lufs: Momentary LUFS（EBU R128，立体声合并，dB）。默认 -70（无信号）
    """

    __slots__ = ("peak_l", "peak_r", "rms_l", "rms_r", "lufs")

    def __init__(
        self,
        peak_l: float = 0.0,
        peak_r: float = 0.0,
        rms_l: float = 0.0,
        rms_r: float = 0.0,
        lufs: float = -70.0,
    ) -> None:
        self.peak_l = float(peak_l)
        self.peak_r = float(peak_r)
        self.rms_l = float(rms_l)
        self.rms_r = float(rms_r)
        self.lufs = float(lufs)

    def __getitem__(self, index: int) -> float:
        """索引访问：[0]=peak_l, [1]=peak_r（向后兼容 (l, r) tuple）。"""
        if index == 0:
            return self.peak_l
        elif index == 1:
            return self.peak_r
        raise IndexError(f"TrackLevel index out of range: {index}")

    def __iter__(self) -> Iterator[float]:
        yield self.peak_l
        yield self.peak_r

    def __len__(self) -> int:
        return 2

    def __repr__(self) -> str:
        return (f"TrackLevel(peak_l={self.peak_l:.4f}, peak_r={self.peak_r:.4f}, "
                f"rms_l={self.rms_l:.4f}, rms_r={self.rms_r:.4f}, lufs={self.lufs:.2f})")


# =============================================================================
# AudioClock
# =============================================================================

@dataclass
class AudioClockState:
    """
    音频时钟状态。

    基于 audio samples 的精确时钟。

    Attributes:
        start_time: 启动时的 monotonic 时间（秒）
        pause_time: 暂停时的 monotonic 时间（秒）
        elapsed_at_pause: 暂停时的累计播放时间（秒）
        samples_played: 累计播放采样数
        current_frame: 当前对应的视频帧号
        state: 播放状态
        speed: 播放速度（1.0 = 正常）
    """
    start_time: float = 0.0
    pause_time: float = 0.0
    elapsed_at_pause: float = 0.0
    samples_played: int = 0
    current_frame: int = 0
    state: AudioPlaybackState = AudioPlaybackState.STOPPED
    speed: float = 1.0


class AudioClock:
    """
    音频时钟。

    基于 audio samples 提供精确的时间基准，
    用于音视频同步。

    Audio Clock = samples_played / sample_rate (秒)

    Methods:
        get_audio_time(): 获取当前音频时间（秒）
        get_frame_for_fps(fps): 根据 FPS 计算对应的视频帧
    """

    def __init__(self, sample_rate: int = DEFAULT_SAMPLE_RATE) -> None:
        """
        初始化音频时钟。

        Args:
            sample_rate: 采样率（Hz）
        """
        self._sample_rate: int = sample_rate
        self._state: AudioClockState = AudioClockState()
        self._mutex: QMutex = QMutex()

    @property
    def sample_rate(self) -> int:
        """采样率。"""
        return self._sample_rate

    def set_sample_rate(self, sample_rate: int) -> None:
        """
        设置采样率。

        Args:
            sample_rate: 采样率（Hz）
        """
        if sample_rate <= 0:
            raise ValueError(f"Invalid sample_rate: {sample_rate}")
        self._sample_rate = sample_rate

    @property
    def is_playing(self) -> bool:
        """是否正在播放。"""
        with QMutexLocker(self._mutex):
            return self._state.state == AudioPlaybackState.PLAYING

    def start(self, start_frame: int = 0, fps: float = 24.0) -> None:
        """
        启动时钟。

        Args:
            start_frame: 起始帧号
            fps: 时间线帧率
        """
        with QMutexLocker(self._mutex):
            self._state.start_time = time.monotonic()
            self._state.elapsed_at_pause = start_frame / fps
            self._state.samples_played = int(self._state.elapsed_at_pause * self._sample_rate)
            self._state.current_frame = start_frame
            self._state.state = AudioPlaybackState.PLAYING
            self._state.speed = 1.0

    def pause(self) -> None:
        """暂停时钟。"""
        with QMutexLocker(self._mutex):
            if self._state.state != AudioPlaybackState.PLAYING:
                return
            self._state.pause_time = time.monotonic()
            elapsed = self._state.pause_time - self._state.start_time
            self._state.elapsed_at_pause += elapsed
            self._state.state = AudioPlaybackState.PAUSED

    def resume(self) -> bool:
        """恢复时钟。

        Returns:
            时钟确为 PAUSED 态并被恢复时返回 True；其余状态为 no-op 并返回
            False（AUD-15515：调用方据此决定是否推进状态机/发信号）。
        """
        with QMutexLocker(self._mutex):
            if self._state.state != AudioPlaybackState.PAUSED:
                return False
            self._state.start_time = time.monotonic()
            self._state.state = AudioPlaybackState.PLAYING
            return True

    def stop(self) -> None:
        """停止时钟。"""
        with QMutexLocker(self._mutex):
            self._state.state = AudioPlaybackState.STOPPED
            self._state.samples_played = 0
            self._state.current_frame = 0
            self._state.elapsed_at_pause = 0.0

    def seek(self, frame: int, fps: float) -> None:
        """
        定位到指定帧。

        Args:
            frame: 目标帧号
            fps: 时间线帧率
        """
        with QMutexLocker(self._mutex):
            # 计算对应的音频时间
            audio_time = frame / fps
            self._state.elapsed_at_pause = audio_time
            self._state.samples_played = int(audio_time * self._sample_rate)
            self._state.current_frame = frame

            # 如果正在播放，重置起始时间
            if self._state.state == AudioPlaybackState.PLAYING:
                self._state.start_time = time.monotonic()

    def add_samples(self, samples: int) -> None:
        """
        添加播放的采样数。

        由音频输出线程调用，更新时钟。

        Args:
            samples: 本次播放的采样数
        """
        with QMutexLocker(self._mutex):
            self._state.samples_played += samples

    def get_audio_time(self) -> float:
        """
        获取当前音频时间（秒）。

        基于实际已播放的采样数（samples_played / sample_rate），
        而非 wall clock，从而避免音频硬件缓冲和线程调度导致的漂移。

        Returns:
            当前音频时间（基于 samples_played）
        """
        with QMutexLocker(self._mutex):
            # 始终使用 samples_played 计算时间，这是最准确的音频进度
            return self._state.samples_played / self._sample_rate

    def get_frame_for_fps(self, fps: float) -> int:
        """
        根据 FPS 计算对应的视频帧。

        Args:
            fps: 时间线帧率

        Returns:
            对应的视频帧号
        """
        audio_time = self.get_audio_time()
        return int(audio_time * fps)

    def get_samples_for_frames(self, frames: int, fps: float) -> int:
        """
        计算帧数对应的采样数。

        Args:
            frames: 帧数
            fps: 时间线帧率

        Returns:
            采样数
        """
        duration_seconds = frames / fps
        return int(duration_seconds * self._sample_rate)

    def get_state(self) -> AudioClockState:
        """
        获取时钟状态（副本）。

        Returns:
            AudioClockState 副本
        """
        with QMutexLocker(self._mutex):
            return AudioClockState(
                start_time=self._state.start_time,
                pause_time=self._state.pause_time,
                elapsed_at_pause=self._state.elapsed_at_pause,
                samples_played=self._state.samples_played,
                current_frame=self._state.current_frame,
                state=self._state.state,
                speed=self._state.speed,
            )


# =============================================================================
# AudioDecoder
# =============================================================================

class AudioDecoder:
    """
    音频解码器。

    使用 PyAV 解码音频轨道。

    Methods:
        open(path): 打开音频文件
        decode_frame(frame_number): 解码指定帧的音频
        decode_range(start_frame, end_frame, fps): 解码帧范围
        close(): 关闭解码器
    """

    def __init__(self) -> None:
        """
        初始化解码器。
        """
        # 生成唯一实例 ID，用于隔离 resampler 状态。
        # AUD-15501: 单调计数器（id(self) 在对象释放后地址可被复用，新解码器
        # 会从缓存拿到旧实例的残留滤波状态）。
        self._instance_id: int = next(_decoder_instance_seq)
        
        self._container: Optional[av.container.Container] = None
        self._audio_stream: Optional[av.AudioStream] = None
        self._path: Optional[Path] = None
        self._sample_rate: int = DEFAULT_SAMPLE_RATE
        self._channels: int = 2
        self._duration_samples: int = 0
        self._fps: float = 24.0

        # 解码位置缓存
        self._last_decoded_frame: int = -1
        self._decode_buffer: deque = deque(maxlen=DECODE_QUEUE_SIZE)

        # 素材偏移与重采样参数
        self._source_start: int = 0          # 素材在源文件中的起始帧（source 帧单位）
        self._source_fps: float = 24.0       # 源视频的帧率（用于帧↔时间换算）
        self._output_sample_rate: int = DEFAULT_SAMPLE_RATE  # 目标输出采样率
        self._timeline_start: int = 0        # clip 在时间线上的起始帧
        self._timeline_duration: int = 0     # clip 在时间线上的持续帧数（0 = 未知/不限）
        # RMA-09: 容器实际声道布局名（open 时从 stream.layout.name 读取）。
        self._source_layout_name: str = ""
        # P-clip-FX: 该 decoder 对应的 clip_id，由 add_track 注入。
        # mix_samples 据此反查 clip.effects 并应用 per-clip 效果链。
        # None（旧调用方）表示该 decoder 不应用 clip 级效果。
        self._clip_id: Optional[str] = None

        # RMA-03: clip 速度与淡入淡出（实时路径此前完全不处理，与离线导出漂移）。
        # speed: 播放速度倍率。F3.2 起两种策略：
        #   - resample 变调（默认）：按倍率调整每帧读取的源采样数；
        #   - preserve_pitch 保调：StreamingWsola 流式 WSOLA（与离线导出
        #     wsola_time_stretch 同 25ms/7.5ms 参数，预听/导出一致）。
        # fade: clip 淡入淡出元数据 dict {timeline_start, timeline_duration,
        #   fade_in_frames, fade_out_frames, fps}，mix_samples 解码后应用 apply_clip_fade。
        self._speed: float = 1.0
        self._preserve_pitch: bool = False
        self._stretcher: Optional[Any] = None
        self._fade: Optional[dict] = None
        # C3: 片段级增益（dB）
        self._gain_db: float = 0.0

        # 小数采样累积器（消除 int() 截断导致的累积漂移）
        self._fractional_offset: float = 0.0
        # F3.2: 保调路径的独立小数累积器（不与变调路径互串）与读前余量
        self._pp_fraction: float = 0.0
        self._pp_margin_pending: int = 0
        self._pp_flushed: bool = False
        # F3.3：子帧偏移块消费门（时间线侧/源侧；seek/重开重置 pending）
        from src.features.edit.core.sample_time import SampleSkipGate
        self._timeline_skip = SampleSkipGate()
        self._source_skip = SampleSkipGate()
        self._fade_offsets = (0, 0)

        # Stage B (规格书 §17.1): leftover sample buffer。
        # decode_next_samples 跨 packet 收集到 >= num_samples 后会截断；
        # 截断掉的尾部 samples 必须保留到下一次调用，否则每个视频帧会
        # 在 packet 边界（如 2048）丢掉 1920 之后的多余 samples，
        # 造成音波不连续 / 杂音 / 长期漂移。
        # 流程：先消费 remainder -> 不足再 demux 新 audio frames ->
        #       合并后超过 num_samples 时返回前 N 个，剩余保存到 remainder。
        self._sample_remainder: Optional[np.ndarray] = None

        # Sample-accurate seek: container.seek 只落到 packet 边界（可能早于
        # 目标时间），seek 后记录目标秒数，decode_next_samples 对首个解码帧
        # 用 pts 计算并丢弃目标时间之前的 pre-roll 样本，避免旧样本混入输出
        # 造成杂音/错位（尤其 scrub 音频预览共享 mixer 随机 seek 后恢复播放）。
        self._seek_target_seconds: Optional[float] = None

    @property
    def is_open(self) -> bool:
        """是否已打开。"""
        return self._container is not None

    @property
    def sample_rate(self) -> int:
        """采样率。"""
        return self._sample_rate

    @property
    def channels(self) -> int:
        """声道数。"""
        return self._channels

    @property
    def source_layout_name(self) -> str:
        """RMA-09: 容器实际声道布局名（如 "stereo"/"5.1(side)"），open 时读取。"""
        return self._source_layout_name

    @property
    def source_layout(self) -> Optional["ChannelLayout"]:
        """RMA-09: 解析后的 ChannelLayout，无法识别时返回 None（调用方应拒绝或要求映射）。

        PyAV 的 stream.layout.name 可能返回规范名（"stereo"/"5.1(side)"）或通用
        "N channels"。优先用规范名解析；失败时按声道数推断（1/2/6/8 标准）；
        仍无法识别（如 4 声道非标准）返回 None。
        """
        from src.features.edit.core.channel_layout import ChannelLayout
        # 1) 规范名解析（如 "5.1(side)"）
        if self._source_layout_name:
            try:
                return ChannelLayout.from_av_layout_name(self._source_layout_name)
            except (ValueError, Exception):
                pass
        # 2) 按声道数推断（标准 1/2/6/8）
        try:
            return ChannelLayout.from_channel_count(self._channels)
        except (ValueError, Exception):
            return None

    @property
    def duration_samples(self) -> int:
        """总采样数。"""
        return self._duration_samples

    def set_speed(self, speed: float) -> None:
        """RMA-03: 设置 clip 播放速度。

        F3.2：保调开启时倍率突变经流式伸缩器 set_speed（清对齐模板，
        已完成帧连续保留）；resample 变调策略只存倍率。
        """
        new_speed = max(0.01, float(speed)) if speed and speed > 0 else 1.0
        if self._stretcher is not None and abs(new_speed - self._speed) > 1e-9:
            try:
                self._stretcher.set_speed(new_speed)
            except ValueError:
                # 超出 WSOLA 支持范围（0.1–8.0）：本回调块回退变调策略
                self._preserve_pitch = False
                self._stretcher = None
        self._speed = new_speed

    def set_preserve_pitch(self, enabled: bool) -> None:
        """F3.2: 开关保调变速（流式 WSOLA；关闭/重开即重置状态）。"""
        enabled = bool(enabled)
        if enabled == self._preserve_pitch:
            return
        self._preserve_pitch = enabled
        self._stretcher = None  # 重开：全新时域状态
        self._pp_flushed = False
        self._pp_fraction = 0.0

    def set_gain_db(self, gain_db: float) -> None:
        """C3: 设置片段级增益（dB，[-48, +12]；0 = 无增益）。

        mix_samples 在 clip fade 之后、clip-FX/track 处理之前应用
        （混音顺序：clip gain → clip 包络 → track automation → volume）。
        """
        self._gain_db = max(-48.0, min(12.0, float(gain_db)))

    def set_fade(self, timeline_start: int, timeline_duration: int,
                 fade_in_frames: int = 0, fade_out_frames: int = 0,
                 fps: float = 24.0,
                 fade_in_samples: int | None = None,
                 fade_out_samples: int | None = None,
                 audio_sample_offset: int = 0,
                 source_sample_offset: int = 0) -> None:
        """RMA-03/F3.3: 设置 clip 淡入淡出与采样级偏移元数据。

        F3.3 采样域：``fade_in/out_samples`` 覆盖帧域淡化（None=沿用帧）；
        ``audio_sample_offset``/``source_sample_offset`` 经 SampleSkipGate
        在 clip 首块消费（时间线侧延迟入点/源侧子帧修剪），seek/重开时
        重置 pending。
        """
        from src.features.edit.core.sample_time import SampleSkipGate
        if fade_in_frames <= 0 and fade_out_frames <= 0 \
                and not fade_in_samples and not fade_out_samples:
            self._fade = None if (not audio_sample_offset
                                  and not source_sample_offset) else {
                "timeline_start": int(timeline_start),
                "timeline_duration": int(timeline_duration),
                "fade_in_frames": 0,
                "fade_out_frames": 0,
                "fps": float(fps),
            }
            self._timeline_skip = SampleSkipGate(audio_sample_offset)
            self._source_skip = SampleSkipGate(source_sample_offset)
            return
        self._fade = {
            "timeline_start": int(timeline_start),
            "timeline_duration": int(timeline_duration),
            "fade_in_frames": int(fade_in_frames),
            "fade_out_frames": int(fade_out_frames),
            "fps": float(fps),
            "fade_in_samples": int(fade_in_samples)
            if fade_in_samples else None,
            "fade_out_samples": int(fade_out_samples)
            if fade_out_samples else None,
            "audio_sample_offset": int(audio_sample_offset or 0),
        }
        self._timeline_skip = SampleSkipGate(audio_sample_offset)
        self._source_skip = SampleSkipGate(source_sample_offset)
        self._fade_offsets = (
            int(audio_sample_offset or 0), int(source_sample_offset or 0))

    def open(
        self,
        path: Path,
        timeline_fps: float = 24.0,
        source_start: int = 0,
        source_fps: float = 0.0,
        output_sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> bool:
        """
        打开音频文件。

        Args:
            path: 文件路径
            timeline_fps: 时间线帧率（用于帧号↔时间换算）
            source_start: 素材在源文件中的起始帧（0 = 从头开始）
            source_fps: 源视频的帧率（0 = 与 timeline_fps 相同）
            output_sample_rate: 目标输出采样率（Hz）

        Returns:
            是否成功打开
        """
        if not PYAV_AVAILABLE:
            logger.warning("PyAV not available, cannot decode audio")
            return False

        try:
            self._container = av.open(str(path))
            self._path = path
            self._fps = timeline_fps
            self._source_start = max(0, int(source_start))
            self._source_fps = float(source_fps) if source_fps > 0 else timeline_fps
            self._output_sample_rate = output_sample_rate if output_sample_rate > 0 else DEFAULT_SAMPLE_RATE

            # 查找音频流
            audio_streams = [s for s in self._container.streams if s.type == 'audio']
            if not audio_streams:
                logger.warning(f"No audio stream in {path}")
                # AUD-15504: 失败路径必须关闭已打开的容器——否则 self._container
                # 残留非 None，is_open 此后错误地返回 True，文件句柄只靠 GC
                # 释放（时机不确定），open()/close() 不对称。
                try:
                    self._container.close()
                except Exception as close_err:
                    logger.debug(f"Container close after open failure raised: {close_err}")
                self._container = None
                self._audio_stream = None
                return False

            self._audio_stream = audio_streams[0]  # type: ignore
            self._sample_rate = self._audio_stream.sample_rate or DEFAULT_SAMPLE_RATE  # type: ignore
            self._channels = self._audio_stream.channels or 2  # type: ignore
            # RMA-09: 记录容器实际声道布局名（如 "5.1(side)"），供 adapt_channels 用
            # 真实布局而非按声道数猜测。5.1(side) vs 5.1(back)、multiple mono 等会被
            # 正确解释，避免丢声道。属性 source_layout 返回 ChannelLayout 或 None。
            try:
                self._source_layout_name = str(self._audio_stream.layout.name)  # type: ignore
            except Exception:
                self._source_layout_name = ""

            # 计算总采样数
            if self._audio_stream.duration:  # type: ignore
                duration_time = float(self._audio_stream.duration) * float(self._audio_stream.time_base)  # type: ignore
                self._duration_samples = int(duration_time * self._sample_rate)
            else:
                # 使用容器时长估算
                if self._container.duration:
                    duration_time = float(self._container.duration) / 1_000_000  # 微秒转秒
                    self._duration_samples = int(duration_time * self._sample_rate)
                else:
                    self._duration_samples = 0

            logger.debug(f"AudioDecoder opened: {path}, rate={self._sample_rate}, channels={self._channels}")
            return True

        except Exception as e:
            logger.error(f"Failed to open audio file: {path}, error: {e}")
            # AUD-15504: 异常路径同样先关闭容器再置 None，保证
            # open() == False ⟹ is_open == False 且句柄即时释放。
            if self._container is not None:
                try:
                    self._container.close()
                except Exception as close_err:
                    logger.debug(f"Container close after open error raised: {close_err}")
            self._container = None
            self._audio_stream = None
            return False

    def close(self) -> None:
        """关闭解码器。"""
        if self._container:
            self._container.close()
            self._container = None
            self._audio_stream = None
            self._path = None
            self._decode_buffer.clear()
            # Stage B: close 时清空 leftover，避免下次复用解码器时残留旧 samples。
            self._sample_remainder = None
            self._seek_target_seconds = None
            logger.debug("AudioDecoder closed")
        # AUD-15501: 关闭时清理本实例在全局 resampler 缓存中的条目。
        # 长剪辑会话反复 add/remove clip 时，条目只增不减（每个残留
        # AudioResampler 持有 FIR 历史）；seek/结束 flush 的 pop 只覆盖单键。
        self._purge_resampler_cache_entries()

    def _purge_resampler_cache_entries(self) -> None:
        """AUD-15501: 按 instance_id 清理全局 resampler 缓存中本实例的条目。"""
        prefix = self._instance_id
        stale_keys = [k for k in _av_resampler_cache if k[3] == prefix]
        for k in stale_keys:
            _av_resampler_cache.pop(k, None)

    def seek_to_time(self, time_seconds: float) -> bool:
        """
        定位到指定时间（秒）。

        Args:
            time_seconds: 目标时间（秒）

        Returns:
            是否成功
        """
        if not self._container or not self._audio_stream:
            return False

        try:
            # PyAV seek 使用 time_base
            target_pts = int(time_seconds / float(self._audio_stream.time_base))  # type: ignore
            self._container.seek(target_pts, stream=self._audio_stream)  # type: ignore
            self._decode_buffer.clear()
            self._last_decoded_frame = -1
            # Stage B: seek 后清空 leftover，避免播放定位后混入旧 samples 造成杂音/漂移。
            self._sample_remainder = None
            # Sample-accurate: container.seek 落到 packet 边界（backward=True 时
            # pts <= target），记录目标时间，由 decode_next_samples 丢弃 pre-roll。
            self._seek_target_seconds = float(time_seconds)
            
            # seek 后 flush resampler，清空旧状态避免杂音
            if PYAV_AVAILABLE and self._sample_rate != self._output_sample_rate:
                try:
                    resampler = _get_av_resampler(
                        self._sample_rate, self._output_sample_rate, self._channels, self._instance_id
                    )
                    if resampler is not None:
                        resampler.resample(None)  # flush
                    # flush 后实例即终结（继续喂帧抛 AVERROR_EOF），必须从
                    # 缓存移除，否则 seek 后该解码器永久落入 scipy 慢路径
                    # （每次重算 FIR 滤波器，多轨混音掉到实时以下）。
                    _av_resampler_cache.pop(
                        (self._sample_rate, self._output_sample_rate,
                         self._channels, self._instance_id), None)
                except Exception:
                    pass
            
            return True
        except Exception as e:
            logger.error(f"Seek failed: {e}")
            return False

    def seek_to_frame(self, frame: int) -> bool:
        """
        定位到指定帧。

        Args:
            frame: 目标帧号（clip 本地帧偏移，0 = clip 起始帧）

        Returns:
            是否成功
        """
        # ``frame`` 是时间线中的 clip-local 帧，不是源素材帧。混帧率时
        # 直接把它加到 source_start 会把 24fps timeline 的第 24 帧错误
        # 定位成 25fps 素材的第 24 帧（而非 1 秒处），导致 A/V 偏移。
        source_start_seconds = self._source_start / max(self._source_fps, 0.001)
        timeline_seconds = frame / max(self._fps, 0.001)
        time_seconds = source_start_seconds + timeline_seconds
        return self.seek_to_time(time_seconds)

    def decode_next_samples(self, num_samples: int) -> Optional[np.ndarray]:
        """
        解码下一批采样（含 leftover buffer）。

        Stage B (规格书 §17.1) 修复：跨 packet 收集到 >= num_samples 后
        截断时，必须把多余 samples 保留到 ``self._sample_remainder``，下一次
        调用先消费这部分。否则每个视频帧会在 packet 边界（如 2048）丢掉
        1920 之后的多余 samples（典型 25fps/48kHz 丢 128/帧），造成杂音、
        音波断裂和长期漂移。

        流程：
            1. remainder 有数据时先消费 remainder。
            2. 不足再 demux/decode 新 audio frames。
            3. 合并后超过 num_samples：返回前 num_samples，剩余存 remainder。
            4. seek/close 时清空 remainder（见对应方法）。

        Args:
            num_samples: 需要的采样数

        Returns:
            音频采样数组 (channels, samples) 或 None
        """
        if not self._container or not self._audio_stream:
            return None

        try:
            collected_samples: List[np.ndarray] = []
            collected_count: int = 0

            # Step 1: 先消费上一次遗留的 remainder。
            if self._sample_remainder is not None:
                remainder = self._sample_remainder
                self._sample_remainder = None
                collected_samples.append(remainder)
                collected_count += remainder.shape[-1]

            # Step 2: remainder 不足时继续 demux 新 audio frames。
            if collected_count < num_samples:
                for packet in self._container.demux(self._audio_stream):  # type: ignore
                    for frame in packet.decode():  # type: ignore
                        # Sample-accurate seek：丢弃目标时间之前的 pre-roll 样本。
                        # container.seek 落点在 packet 边界，可能早于目标时间；
                        # 用帧 pts 精确计算应跳过的源样本数。
                        preroll_skip = 0
                        if self._seek_target_seconds is not None:
                            frame_start_seconds: Optional[float] = None
                            try:
                                if frame.pts is not None:  # type: ignore
                                    frame_start_seconds = (
                                        float(frame.pts) * float(self._audio_stream.time_base)  # type: ignore
                                    )
                            except Exception:
                                frame_start_seconds = None
                            if frame_start_seconds is not None:
                                skip = int(round(
                                    (self._seek_target_seconds - frame_start_seconds)
                                    * self._sample_rate
                                ))
                                if skip >= frame.samples:  # type: ignore
                                    # 整帧都在目标时间之前：丢弃整帧，继续对下一帧判断。
                                    continue
                                if skip > 0:
                                    preroll_skip = skip
                                # skip <= 0：落点已不早于目标，无需再丢。
                            # pts 不可用时保守放弃 skip（不能拿后续更晚的 pts 乱丢）。
                            self._seek_target_seconds = None
                        # 转换为 numpy 数组并归一化到 [-1.0, 1.0]
                        audio_array = frame.to_ndarray()  # type: ignore

                        # ---- 处理 packed (交错) 格式 → 平面格式 ----
                        # packed 格式 to_ndarray() 可能返回 (1, channels*samples) 或 1D 数组
                        # 需要解交错为 (channels, samples)
                        if hasattr(frame, 'format') and hasattr(frame.format, 'is_planar'):
                            if frame.format.is_planar is False:
                                try:
                                    n_channels = frame.layout.nb_channels if frame.layout else 0
                                    n_samples_per_ch = frame.samples
                                    if n_channels <= 0:
                                        n_channels = self._channels  # 退回到解码器记录的声道数
                                    total_expected = n_channels * n_samples_per_ch

                                    if audio_array.ndim == 2 and audio_array.shape[0] == 1:
                                        interleaved = audio_array[0]
                                        if interleaved.size >= total_expected and n_channels > 1:
                                            audio_array = interleaved[:total_expected].reshape(-1, n_channels).T
                                        # 如果 size 不匹配或单声道，保持原样
                                    elif audio_array.ndim == 1 and n_channels > 1:
                                        # PyAV 某些版本返回 1D 数组
                                        if audio_array.size >= total_expected:
                                            audio_array = audio_array[:total_expected].reshape(-1, n_channels).T
                                    # 如果已经是 (channels, samples) 形状则无需处理
                                except (AttributeError, ValueError) as e:
                                    logger.debug(f"Deinterleave skipped: {e}")
                                    # 解交错失败时保持 audio_array 原样，不会崩溃

                        if audio_array.dtype == np.float32:
                            pass  # 已经是 [-1.0, 1.0] 范围
                        elif audio_array.dtype == np.float64:
                            audio_array = audio_array.astype(np.float32)
                        elif audio_array.dtype == np.int16:
                            audio_array = audio_array.astype(np.float32) / 32768.0
                        elif audio_array.dtype == np.int32:
                            audio_array = audio_array.astype(np.float32) / 2147483648.0
                        elif audio_array.dtype == np.int8:
                            audio_array = audio_array.astype(np.float32) / 128.0
                        elif audio_array.dtype == np.uint8:
                            audio_array = (audio_array.astype(np.float32) - 128.0) / 128.0
                        else:
                            # 未知格式：尝试安全转换
                            audio_array = audio_array.astype(np.float32)
                            peak = np.max(np.abs(audio_array))
                            if peak > 1.0:
                                audio_array /= peak

                        # Sample-accurate seek：切掉帧头部 pre-roll（解交错/归一化之后，
                        # 保证切片在样本维度而非交错维度上）。
                        if preroll_skip > 0 and audio_array.shape[-1] > preroll_skip:
                            audio_array = audio_array[..., preroll_skip:]
                        elif preroll_skip > 0:
                            continue

                        collected_samples.append(audio_array)
                        collected_count += audio_array.shape[-1]

                        if collected_count >= num_samples:
                            break

                    if collected_count >= num_samples:
                        break

            if not collected_samples:
                return None

            # Step 3: 合并所有采样。
            result = np.concatenate(collected_samples, axis=-1)

            # Step 4: 超过 num_samples 时，保留尾部到 remainder 再截断。
            # 这是 Stage B 的核心修复：不再直接丢弃多余 samples。
            if result.shape[-1] > num_samples:
                excess_count = result.shape[-1] - num_samples
                self._sample_remainder = np.ascontiguousarray(
                    result[..., num_samples:].copy()
                )
                result = result[..., :num_samples]
                logger.debug(
                    f"decode_next_samples kept {excess_count} leftover samples "
                    f"for next call (requested={num_samples}, decoded={collected_count})"
                )

            return result

        except av.error.EOFError:
            # Normal: reached end of stream
            return None
        except Exception as e:
            logger.error(f"Decode failed: {e}")
            return None

    def decode_samples_for_frame(
        self,
        frame: int,
        fps: float,
        samples_per_frame: int,
    ) -> Optional[np.ndarray]:
        """
        解码指定帧对应的音频采样，并按输出采样率重采样。

        使用浮点累积器跟踪每帧应读取的精确采样数，消除 int() 截断
        在非整数帧率（如 29.97fps）下造成的累积漂移。

        Args:
            frame: 时间线帧号（绝对位置）
            fps: 时间线帧率
            samples_per_frame: 输出端每帧期望的采样数（output_sample_rate / fps）

        Returns:
            重采样后的音频数组 (channels, output_samples) 或 None
        """
        # 将 timeline 帧转换为 clip 本地帧偏移
        local_frame = frame - self._timeline_start

        # clip 尚未开始，返回 None（mixer 会跳过此轨道）
        if local_frame < 0:
            return None
        if self._timeline_duration > 0 and local_frame >= self._timeline_duration:
            return None

        # 检查是否需要 seek（seek 时重置小数累积器）
        expected_frame = self._last_decoded_frame + 1
        if self._last_decoded_frame < 0 or local_frame != expected_frame:
            # R06（第七轮审计）：定位时钟用调用方传入的 fps——导出路径
            # 传入的帧号是素材源帧（canonical 帧映射），按源帧率换算才
            # 与视频解码同刻；沿用 open() 的 timeline_fps 会让 50fps 源
            # 在 25fps 序列上 1 秒时间线定位到 2 秒源处。实时播放调用方
            # 传 timeline_fps（等于 open 值），行为不变。
            time_seconds = (
                self._source_start / max(self._source_fps, 0.001)
                + local_frame / max(float(fps), 0.001)
            )
            self.seek_to_time(time_seconds)
            self._fractional_offset = 0.0
            self._pp_fraction = 0.0
            # F3.2：seek/loop/暂停恢复 → 清流式伸缩器时域状态并重置读前余量
            if self._stretcher is not None:
                self._stretcher.reset()
                self._pp_margin_pending = \
                    self._stretcher.algorithmic_latency_samples
            self._pp_flushed = False
            if local_frame == 0:
                self._timeline_skip.reset(self._fade_offsets[0])
                self._source_skip.reset(self._fade_offsets[1])

        # 按输出端实际 chunk 时长读取源采样。非整数帧率下输出端会在
        # N/N+1 samples 间交替，这里跟随实际 num_samples，避免长时间漂移。
        exact_samples_per_frame = (
            samples_per_frame * self._sample_rate / max(1, self._output_sample_rate)
        )
        if exact_samples_per_frame <= 0:
            exact_samples_per_frame = self._sample_rate / max(fps, 1.0)

        speed = self._speed if self._speed > 0 else 1.0

        # F3.2: 保调变速（流式 WSOLA）。与离线导出同 25ms/7.5ms 参数
        # （预听/导出策略一致）；输出恰好 n 个输出采样（无截断丢弃）。
        # 注意：1x 也走伸缩器——1x 旁路会让 1x↔变速切换产生路径切换缝
        # （新伸缩器读前余量跳过 ~32ms 内容，实测可闻脉冲）；全速经同一
        # 状态机，倍率突变只动步进（平滑，见 test_speed_switch_keeps_streaming）。
        if self._preserve_pitch:
            result = self._decode_preserve_pitch(
                local_frame, samples_per_frame, speed,
                exact_samples_per_frame)
            if result is not None:
                self._last_decoded_frame = local_frame
            return result

        # RMA-03: clip 速度。resample（变调）策略——按 speed 倍率调整每帧读取的
        # 源采样数：speed>1 读更多源样本→播放更快/音调更高；speed<1 读更少→更慢/更低。
        # 这与离线导出 timeline_export 的 source_frame*=speed 语义一致（变调 resample）。
        exact_samples_per_frame *= speed

        # 累积小数部分，取整数部分作为本次实际读取的采样数
        self._fractional_offset += exact_samples_per_frame
        source_samples_per_frame = int(self._fractional_offset)
        self._fractional_offset -= source_samples_per_frame

        if source_samples_per_frame <= 0:
            source_samples_per_frame = max(1, samples_per_frame)  # safety fallback

        # 从源文件读取原始采样
        samples = self.decode_next_samples(source_samples_per_frame)
        if samples is not None:
            self._last_decoded_frame = local_frame

            # 若源采样率与输出采样率不同，执行重采样
            if self._sample_rate != self._output_sample_rate:
                samples = _resample_audio(
                    samples,
                    source_rate=self._sample_rate,
                    target_rate=self._output_sample_rate,
                    instance_id=self._instance_id,
                    flush=False,  # 正常播放不 flush，seek 时已单独处理
                )

        return self._apply_skip_gates(samples)

    def _apply_skip_gates(self, samples: Optional[np.ndarray]):
        """F3.3：子帧偏移门（源侧修剪 → 时间线侧延迟，串联一次消费）。"""
        if samples is None or samples.shape[-1] == 0:
            return samples
        samples = self._source_skip.feed(samples)
        return self._timeline_skip.feed(samples)

    def _decode_preserve_pitch(
        self,
        local_frame: int,
        samples_per_frame: int,
        speed: float,
        exact_source_per_output: float,
    ) -> Optional[np.ndarray]:
        """F3.2: 保调解码块——读 n*speed(+margin) 源 → 流式 WSOLA → 恰好 n 输出。

        有界：源读取按块（EOF 提前止），伸缩器内部 FIFO 有界；回调内无
        解码器分配（伸缩器按 clip 惰性建一次）。EOF 时 flush 伸缩器出尾
        块，其后本 clip 音频结束（返回 None）。
        """
        if self._stretcher is None:
            from src.features.edit.core.audio_timestretch import (
                StreamingWsola,
            )
            self._stretcher = StreamingWsola(
                channels=max(1, self._channels),
                sample_rate=float(self._output_sample_rate),
                speed=speed)
            self._pp_margin_pending = \
                self._stretcher.algorithmic_latency_samples

        n_out = max(1, int(samples_per_frame))
        # 源域读取量：输出 n × speed，经 src/out 采样率换算，外加读前余量
        target_src = n_out * speed * (
            self._sample_rate / max(1, self._output_sample_rate))
        self._pp_fraction += target_src
        read_src = int(self._pp_fraction)
        self._pp_fraction -= read_src
        margin = self._pp_margin_pending
        self._pp_margin_pending = 0
        need_src = read_src + margin

        pieces: list = []
        got = 0
        while got < need_src:
            chunk = self.decode_next_samples(need_src - got)
            if chunk is None or chunk.shape[-1] == 0:
                break  # clip 音频 EOF
            pieces.append(chunk)
            got += chunk.shape[-1]
        if not pieces:
            # 本块无新输入：可 emit 的只剩伸缩器内已结算样本
            out = self._stretcher.pull(n_out)
            if out.shape[-1] == 0 and self._pp_flushed:
                return None
            return out if out.shape[-1] else None
        samples = pieces[0] if len(pieces) == 1 else np.concatenate(pieces, axis=-1)
        if self._sample_rate != self._output_sample_rate:
            samples = _resample_audio(
                samples,
                source_rate=self._sample_rate,
                target_rate=self._output_sample_rate,
                instance_id=self._instance_id,
                flush=False,
            )
        samples = self._apply_skip_gates(samples)
        self._stretcher.push(samples)
        out = self._stretcher.pull(n_out)
        if out.shape[-1] < n_out and got < need_src:
            # EOF：flush 尾块补齐（一次性）
            if not self._pp_flushed:
                self._pp_flushed = True
                tail = self._stretcher.flush()
                if tail.shape[-1]:
                    out = np.concatenate([out, tail], axis=-1)
        return out if out.shape[-1] else None


# =============================================================================
# AudioMixer
# =============================================================================

class AudioMixer:
    """
    音频混合器。

    混合多个音频轨道，考虑 mute/solo/volume。

    Methods:
        add_track(track_id, decoder): 添加轨道
        remove_track(track_id): 移除轨道
        set_track_state(track_id, state): 设置轨道状态
        mix_samples(num_samples): 混合采样
        has_solo_tracks(): 是否有 solo 轨道
    """

    def __init__(self) -> None:
        """
        初始化混合器。
        """
        self._tracks: Dict[str, AudioTrackState] = {}
        self._decoders: Dict[str, Any] = {}
        self._mutex: QMutex = QMutex()
        # W7: 每轨一个 Strip EQ 滤波器实例（含 IIR 状态，跨样本块连续）
        self._eq_filters: Dict[str, StripEQFilter] = {}
        # W26: 每轨一个 FX 机架效果器链（EQ 之外的限幅器等，post-fader，在 strip EQ 之后）
        self._effect_chains: Dict[str, EffectChain] = {}
        # P-clip-FX: 每个音频 clip 一个独立效果器链（来自 clip.effects 中
        # 的 mixer audio 效果）。在 decoder 解码后、track volume/pan 之前
        # 应用（item-FX = pre-track-FX 语义）。key=clip_id。
        self._clip_effect_chains: Dict[str, EffectChain] = {}
        # D04（第六轮审计）：动画 FX 的逐帧参数采样器（frame -> entries）；
        # mix_samples 在处理该 clip 块前按当前帧更新链参数。
        self._clip_effect_samplers: Dict[str, Any] = {}
        # clip_id → track_id 反查表，用于 remove_track 时清理对应 clip 链。
        self._clip_to_track: Dict[str, str] = {}
        # W11: 每轨一个 LUFS 响度计（EBU R128，跨样本块连续）
        self._lufs_meters: Dict[str, Any] = {}
        # W21: 每轨 automation（{track_id: {param: KeyframeTrack}}）
        self._automation: Dict[str, Dict[str, Any]] = {}
        # AM-07: 每轨自动化模式（"off"/"read"/"write"/"touch"/"latch"）。
        # 引擎据此判断是否读取 automation 曲线。Off 模式用静态值（不读曲线）。
        # 默认 "off"（向后兼容：无模式时存在关键帧不读取，由调用方显式设为 read）。
        self._automation_modes: Dict[str, str] = {}
        # W24/W25: 每轨路由信息 {track_id: {"output_bus_id": str|None, "sends": [(bus_id, level), ...]}}
        self._track_routing: Dict[str, Dict[str, Any]] = {}
        self._sample_rate: float = 48000.0
        # A5: solo 计数器，避免 mix_samples 每帧 `any(t.solo for t in self._tracks.values())` 遍历。
        # 所有修改 _tracks 的入口（add/remove/set_track_state/update_track_solo）都负责维护它。
        self._solo_count: int = 0
        # Master 总线音量/静音（sample 级应用，对称于 track volume）。
        # master_volume 是线性增益，支持 >1.0 的正增益（mix_samples 末端软限幅器保护削波）。
        # 历史上 manager 曾用线程私有的 QAudioSink.set_volume（上限 1.0、且线程不可达），
        # 既不支持正增益也恒为 no-op；现统一在 sample 级处理。
        self._master_volume: float = 1.0
        self._master_muted: bool = False
        # §3.1: master automation（{param: KeyframeTrack}）+ 模式
        self._master_automation: dict = {}
        self._master_automation_mode: str = "off"
        # AM-09: 连续状态 Master 限幅器（替代无状态逐 buffer 峰值归一化）。
        # 跨 buffer 平滑增益包络，避免 audible pumping（block-boundary gain jump）。
        from src.features.edit.core.audio_dsp import MasterLimiter
        self._master_limiter = MasterLimiter(self._sample_rate, threshold=1.0)
        # AT (Audio Transition): 每轨的音频转场实例（仅 audio:* 类型）。
        # 存 TransitionInstance 列表；mix_samples 在解码每个 clip 时查询该 clip
        # 是否作为某转场的 left/right，并叠加 crossfade 包络。None/空=该轨无转场。
        self._audio_transitions: Dict[str, tuple] = {}
        # 混音 scratch 缓冲（mix_samples 热路径零分配）：
        # - _mix_acc_scratch: 每轨 LUFS 累积缓冲（key=track_id）
        # - _mix_pad_scratch: 每轨尾部补零缓冲（clip 结尾短块）
        # - _mix_bus_scratch: 每个 Bus 的输入累积缓冲（key=bus_id）
        # 缓冲按容量复用（≥请求宽度），每次使用前整块清零；调用方只持有
        # 本次调用内的视图，跨调用无引用（master 输出仍每次新建，因
        # scrub 路径的返回值会跨线程排队）。
        self._mix_acc_scratch: Dict[str, np.ndarray] = {}
        self._mix_pad_scratch: Dict[str, np.ndarray] = {}
        self._mix_bus_scratch: Dict[str, np.ndarray] = {}
        # W11: LUFS 按需计算——仅当存在 LUFS 模式的表（用户显式选择）时才
        # 跑 K-weighting/门控统计。实测 LUFS 占混音热路径近一半耗时
        # （8 轨 ~3.4ms/7.1ms），默认 peak/RMS 表场景完全不需要。
        # 由 AudioMixerPanel.lufs_metering_changed → manager → 此开关驱动。
        self._lufs_metering_enabled: bool = False
        # AUD-15506: solo 可听闭包缓存（None=失效）。solo/routing 只在
        # add/remove/set_track_state/update_track_solo/set_track_routing 变化，
        # 所有这些入口负责把缓存置回 None；mix_samples 持同一把锁读，一致。
        self._solo_closure_cache: Optional[set] = None
        # AUD-15506: AudioGraphRenderer._apply_declick_edges 惰性缓存。该类所在
        # 模块反向 import 本模块（topological_sort_buses），不能在模块级导入，
        # 首次用到时解析一次并缓存函数引用。
        self._declick_edges_fn: Optional[Callable[[Any, float], Any]] = None

    def set_lufs_metering(self, enabled: bool) -> None:
        """开/关 LUFS 响度计算（表模式驱动，线程安全）。"""
        with QMutexLocker(self._mutex):
            self._lufs_metering_enabled = bool(enabled)

    def _mix_scratch(
        self,
        store: Dict[str, np.ndarray],
        key: str,
        channels: int,
        num_samples: int,
    ) -> np.ndarray:
        """获取复用的零填充混音缓冲（宽度恰好 num_samples 的视图）。

        容量不足时重新分配；chunk 宽度在非整数帧率下交替 N/N+1，
        按容量（而非精确宽度）复用避免每个周期重新分配。
        """
        buf = store.get(key)
        if buf is None or buf.shape[0] != channels or buf.shape[-1] < num_samples:
            buf = np.zeros((channels, num_samples), dtype=np.float32)
            store[key] = buf
        else:
            buf.fill(0.0)
        if buf.shape[-1] == num_samples:
            return buf
        return buf[:, :num_samples]

    def _iter_decoders(self, decoders: Any) -> List[AudioDecoder]:
        """Return decoder objects while tolerating legacy single-decoder values."""
        if isinstance(decoders, list):
            return decoders
        if isinstance(decoders, tuple):
            return list(decoders)
        return [decoders]

    def add_track(
        self,
        track_id: str,
        decoder: AudioDecoder,
        state: Optional[AudioTrackState] = None,
        *,
        replace_existing: bool = True,
    ) -> None:
        """
        添加音频轨道。

        Args:
            track_id: 轨道 ID
            decoder: 解码器
            state: 轨道状态（可选）
            replace_existing: True 时替换原轨道解码器；False 时追加为同轨道的另一个 clip
        """
        # AUD-15505: decoder.close() 会 flush/关闭 PyAV 容器（慢盘/网络盘下耗时
        # 不可控），绝不能在 mixer `_mutex` 锁内执行——播放线程的 mix_samples
        # 整个解码+混音周期都持同一把锁，锁内 close 会把音频线程拖出 underrun。
        # 锁内只做字典摘除，close 统一移到锁外；被摘除的旧解码器不会再被新的
        # mix 周期引用（mix 每周期在锁内重新读 _decoders）。
        stale_decoders: List[AudioDecoder] = []
        with QMutexLocker(self._mutex):
            if replace_existing or track_id not in self._decoders:
                old_decoders = self._decoders.get(track_id)
                if old_decoders is not None and replace_existing:
                    stale_decoders = self._iter_decoders(old_decoders)
                self._decoders[track_id] = [decoder]
            else:
                decoders = self._decoders[track_id]
                if isinstance(decoders, list):
                    decoders.append(decoder)
                else:
                    self._decoders[track_id] = [decoders, decoder]

            # A5: 维护 solo 计数器（必须在写入 _tracks 前读取旧值做差分）
            if state is not None:
                self._apply_solo_delta(track_id, state.solo)
                self._tracks[track_id] = state
            elif track_id not in self._tracks:
                self._tracks[track_id] = AudioTrackState(track_id=track_id)
                # 新建默认 state（solo=False），计数器无需变化
            # AUD-15506: 轨道集/solo 变化 → 闭包缓存失效
            self._invalidate_solo_closure()

        # AUD-15505: 锁外关闭被替换的旧解码器（文件 IO）。
        for old_decoder in stale_decoders:
            old_decoder.close()

    def _invalidate_solo_closure(self) -> None:
        """AUD-15506: 拓扑/solo/路由变化后使 solo 闭包缓存失效。

        必须在持 mixer `_mutex` 期间调用（与 mix_samples 的读取互斥）。
        """
        self._solo_closure_cache = None

    def _apply_declick(self, gate: Any, sample_rate: float) -> Any:
        """AUD-15506: 应用 de-click 边沿（AudioGraphRenderer._apply_declick_edges）。

        该函数所在模块反向 import 本模块（topological_sort_buses），不能在
        模块级导入；首次调用解析一次并缓存函数引用，mix 热路径后续零开销。
        """
        fn = self._declick_edges_fn
        if fn is None:
            from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
            fn = AudioGraphRenderer._apply_declick_edges
            self._declick_edges_fn = fn
        return fn(gate, sample_rate)

    def remove_track(self, track_id: str) -> None:
        """
        移除音频轨道。

        Args:
            track_id: 轨道 ID
        """
        # AUD-15505: 同 add_track——close 是文件 IO，锁内只摘除、锁外关闭。
        stale_decoders: List[AudioDecoder] = []
        with QMutexLocker(self._mutex):
            if track_id in self._decoders:
                stale_decoders = self._iter_decoders(self._decoders[track_id])
                del self._decoders[track_id]
            if track_id in self._tracks:
                # A5: 移除前若是 solo 轨，计数器减 1
                if self._tracks[track_id].solo:
                    self._solo_count = max(0, self._solo_count - 1)
                del self._tracks[track_id]
            # W7: 同时清理 EQ 滤波器
            self._eq_filters.pop(track_id, None)
            # W26: 清理 FX 机架效果器链
            self._effect_chains.pop(track_id, None)
            # P-clip-FX: 清理该 track 下所有 clip 的 per-clip 链
            # （用 _clip_to_track 反查该 track 的 clip_id 集合）
            clip_ids_to_remove = [
                cid for cid, tid in self._clip_to_track.items() if tid == track_id
            ]
            for cid in clip_ids_to_remove:
                self._clip_effect_chains.pop(cid, None)
                self._clip_effect_samplers.pop(cid, None)
                self._clip_to_track.pop(cid, None)
            # W11: 清理 LUFS 响度计
            self._lufs_meters.pop(track_id, None)
            # 清理混音 scratch 缓冲（防轨道频繁增删时内存累积）
            self._mix_acc_scratch.pop(track_id, None)
            self._mix_pad_scratch.pop(track_id, None)
            self._mix_bus_scratch.pop(track_id, None)
            # W21: 清理 automation
            self._automation.pop(track_id, None)
            # W24: 清理路由
            self._track_routing.pop(track_id, None)
            # AT: 清理音频转场
            self._audio_transitions.pop(track_id, None)
            # AUD-15506: 拓扑（轨道集/路由）变化 → solo 闭包缓存失效
            self._solo_closure_cache = None

        # AUD-15505: 锁外关闭被移除轨道的解码器（文件 IO）。
        for decoder in stale_decoders:
            decoder.close()

    def register_bus_track(self, track_id: str, state: Optional[AudioTrackState] = None) -> None:
        """AM-02: 注册 Bus 轨到引擎（无 decoder）。

        Bus 轨没有 clips/decoder，但需要进入 `_tracks`，否则 mix_samples 阶段 2
        的 `self._tracks.get(bus_id)` 为 None → Bus 输入被丢弃（AM-02 根因）。

        与 add_track 的区别：不创建/注册 decoder，只写 `_tracks` + solo 计数器。
        Bus 的 EQ/FX/automation/routing 仍由各自的 set_track_* 方法单独同步。

        Args:
            track_id: Bus 轨 ID
            state: Bus 轨状态（volume/pan/muted/solo/enabled）；None 用默认 state
        """
        with QMutexLocker(self._mutex):
            bus_state = state if state is not None else AudioTrackState(track_id=track_id)
            # 维护 solo 计数器（必须在写入 _tracks 前做差分）
            self._apply_solo_delta(track_id, bus_state.solo)
            self._tracks[track_id] = bus_state
            # AUD-15506: 轨道集/solo 变化 → 闭包缓存失效
            self._invalidate_solo_closure()
            # 确保 Bus 不在 _decoders 中（Bus 不解码；阶段 1 遍历 _decoders 时跳过 Bus）
            # 不动 _decoders，保持普通轨与 Bus 的解耦。

    def unregister_bus_track(self, track_id: str) -> None:
        """AM-02: 注销 Bus 轨（与 register_bus_track 对称）。

        只清理 `_tracks` 及关联缓存，不涉及 decoder（Bus 无 decoder）。
        """
        with QMutexLocker(self._mutex):
            if track_id in self._tracks:
                if self._tracks[track_id].solo:
                    self._solo_count = max(0, self._solo_count - 1)
                del self._tracks[track_id]
            self._eq_filters.pop(track_id, None)
            self._effect_chains.pop(track_id, None)
            self._lufs_meters.pop(track_id, None)
            self._automation.pop(track_id, None)
            self._track_routing.pop(track_id, None)
            # AUD-15506: 轨道集/路由变化 → 闭包缓存失效
            self._invalidate_solo_closure()

    def _apply_solo_delta(self, track_id: str, new_solo: bool) -> None:
        """A5: 根据「即将写入的 solo 值」与「当前 _tracks 中的 solo 值」的差分更新 _solo_count。

        必须在已持锁、且**写入 _tracks 之前**调用，否则旧值已被覆盖、差分恒为 0。
        """
        old_state = self._tracks.get(track_id)
        old_solo = old_state.solo if old_state is not None else False
        if new_solo and not old_solo:
            self._solo_count += 1
        elif not new_solo and old_solo:
            self._solo_count = max(0, self._solo_count - 1)

    def set_track_state(self, track_id: str, state: AudioTrackState) -> None:
        """
        设置轨道状态。

        Args:
            track_id: 轨道 ID
            state: 新状态
        """
        with QMutexLocker(self._mutex):
            # A5: 写入前做差分（否则旧值已被覆盖）
            self._apply_solo_delta(track_id, state.solo)
            self._tracks[track_id] = state
            # AUD-15506: solo 可能翻转 → 闭包缓存失效
            self._invalidate_solo_closure()

    # ── W18: 原子更新方法（在单次锁内完成读-改-写，消除竞态）──
    #
    # 这些方法在单次 QMutexLocker 内完成「读旧 state → 构造新 state → 写入 _tracks」，
    # 并返回新 state 给调用方（AudioPlaybackManager），由 manager 在自己的锁外更新
    # `_track_states` 缓存并发信号。这样整个调用链只有 mixer 内部 1 次加锁，
    # 消除了「get→构造→update→写缓存」三段分别加锁导致的读-改-写竞态。

    def update_track_volume(self, track_id: str, volume: float) -> Optional[AudioTrackState]:
        """原子地更新某轨音量（单次加锁，避免与播放线程竞态）。

        Returns:
            构造好的新 AudioTrackState，或 None（track_id 不存在）。
        """
        with QMutexLocker(self._mutex):
            state = self._tracks.get(track_id)
            if state is None:
                return None
            new_state = AudioTrackState(
                track_id=track_id,
                volume=max(MIN_VOLUME, min(MAX_VOLUME, volume)),
                muted=state.muted, solo=state.solo,
                pan=state.pan, enabled=state.enabled,
            )
            self._tracks[track_id] = new_state
            return new_state

    def update_track_pan(self, track_id: str, pan: float) -> Optional[AudioTrackState]:
        """原子地更新某轨声像。返回新 state 或 None。"""
        with QMutexLocker(self._mutex):
            state = self._tracks.get(track_id)
            if state is None:
                return None
            new_state = AudioTrackState(
                track_id=track_id, volume=state.volume,
                muted=state.muted, solo=state.solo,
                pan=max(-1.0, min(1.0, pan)), enabled=state.enabled,
            )
            self._tracks[track_id] = new_state
            return new_state

    def update_track_mute(self, track_id: str, muted: bool) -> Optional[AudioTrackState]:
        """原子地更新某轨静音状态。返回新 state 或 None。"""
        with QMutexLocker(self._mutex):
            state = self._tracks.get(track_id)
            if state is None:
                return None
            new_state = AudioTrackState(
                track_id=track_id, volume=state.volume,
                muted=muted, solo=state.solo,
                pan=state.pan, enabled=state.enabled,
            )
            self._tracks[track_id] = new_state
            return new_state

    def update_track_solo(self, track_id: str, solo: bool) -> Optional[AudioTrackState]:
        """原子地更新某轨独奏状态。返回新 state 或 None。

        同时维护 _solo_count 计数器（A5），避免 mix_samples 每帧遍历全表。
        """
        with QMutexLocker(self._mutex):
            state = self._tracks.get(track_id)
            if state is None:
                return None
            # 同步 _solo_count 计数器（仅当 solo 状态真正翻转时增减）
            if solo and not state.solo:
                self._solo_count += 1
            elif not solo and state.solo:
                self._solo_count = max(0, self._solo_count - 1)
            new_state = AudioTrackState(
                track_id=track_id, volume=state.volume,
                muted=state.muted, solo=solo,
                pan=state.pan, enabled=state.enabled,
            )
            self._tracks[track_id] = new_state
            # AUD-15506: solo 翻转 → 闭包缓存失效
            self._invalidate_solo_closure()
            return new_state

    def get_track_state(self, track_id: str) -> Optional[AudioTrackState]:
        """
        获取轨道状态。

        Args:
            track_id: 轨道 ID

        Returns:
            轨道状态或 None
        """
        with QMutexLocker(self._mutex):
            return self._tracks.get(track_id)

    def set_master_volume(self, volume: float) -> None:
        """设置 Master 总线音量（线性增益）。线程安全。

        支持 >1.0 的正增益（如 +6dB ≈ 2.0）：mix_samples 末端在应用 master 增益后
        跑软限幅器，对超阈值部分平滑缩放，避免硬削波。

        Args:
            volume: 线性增益（>= 0.0；典型 0.0-2.0，与推子 dB 范围对齐）
        """
        # PyQt6's QMutexLocker wrapper can race during construction when many
        # Python threads enter this setter simultaneously. Lock the underlying
        # QMutex explicitly so overload resolution never observes a locker.
        self._mutex.lock()
        try:
            self._master_volume = max(0.0, float(volume))
        finally:
            self._mutex.unlock()

    def set_master_muted(self, muted: bool) -> None:
        """设置 Master 总线静音状态。线程安全。"""
        with QMutexLocker(self._mutex):
            self._master_muted = bool(muted)

    def set_master_automation(self, automation: dict) -> None:
        """§3.1: 设置 Master automation（{param: KeyframeTrack}）。线程安全。"""
        with QMutexLocker(self._mutex):
            self._master_automation = dict(automation or {})

    def set_master_automation_mode(self, mode: str) -> None:
        """§3.1: 设置 Master automation 模式（off/read/write/touch/latch）。线程安全。"""
        with QMutexLocker(self._mutex):
            self._master_automation_mode = str(mode or "off").lower()

    def set_sample_rate(self, sample_rate: float) -> None:
        """更新混合器采样率（影响 EQ 滤波器系数）。线程安全。"""
        with QMutexLocker(self._mutex):
            self._sample_rate = float(sample_rate)
            # 采样率变化时重置所有 EQ 滤波器（系数会在下次 set_params 时按新采样率重算）
            for flt in self._eq_filters.values():
                flt.set_params(flt._params, sample_rate=self._sample_rate)
            # W26: 同步更新 FX 机架效果器链
            for chain in self._effect_chains.values():
                chain.set_params(chain._last_entries, sample_rate=self._sample_rate)
            # P-clip-FX: 同步更新 per-clip 效果器链
            for clip_chain in self._clip_effect_chains.values():
                clip_chain.set_params(clip_chain._last_entries, sample_rate=self._sample_rate)

    def set_decoders_output_rate(self, sample_rate: int) -> None:
        """AUD-15509: 设备格式回退后同步所有解码器的重采样目标率。

        ``set_sample_rate`` 只更新 EQ/FX 系数；解码器的 ``_output_sample_rate``
        仍是 add_track 时的旧率——解码输出按旧率重采样后被新率设备消费，
        音调/速度整体漂移（48k→44.1k 约 -8.6%），EQ 中心频率整体偏移。
        此处统一改写目标率并丢弃旧率的 resampler 缓存实例（其 FIR 历史对应
        旧率信号，键含旧 target_rate，留着只会等容量上限驱逐）。线程安全。
        """
        if sample_rate <= 0:
            return
        with QMutexLocker(self._mutex):
            for decoders in self._decoders.values():
                for decoder in self._iter_decoders(decoders):
                    decoder._output_sample_rate = int(sample_rate)
                    decoder._purge_resampler_cache_entries()

    def reset_dsp_states(self) -> None:
        """复位所有效果器/滤波器的内部状态（seek 后调用）。线程安全。

        EQ/FX/限幅器/混响等都是有状态 DSP（IIR zi、延迟线、包络）。
        跳转播放位置后旧状态对应的是跳转前的信号位置——混响尾、压缩
        包络、滤波瞬态都会错位，产生爆音或抽吸。seek 时统一清零，
        各效果器从零状态重新收敛。
        """
        with QMutexLocker(self._mutex):
            for flt in self._eq_filters.values():
                flt.reset_state()
            for chain in self._effect_chains.values():
                chain.reset_state()
            for clip_chain in self._clip_effect_chains.values():
                clip_chain.reset_state()
            for meter in self._lufs_meters.values():
                try:
                    meter.reset()
                except Exception:
                    pass
            if self._master_limiter is not None:
                self._master_limiter.reset()

    def set_track_strip_eq(self, track_id: str, params: Optional[StripEQParams]) -> None:
        """设置某轨的 Strip EQ 参数（W7）。线程安全。

        Args:
            track_id: 轨道 ID
            params: StripEQParams，None 表示禁用 EQ（旁路）
        """
        with QMutexLocker(self._mutex):
            if track_id not in self._eq_filters:
                self._eq_filters[track_id] = StripEQFilter(self._sample_rate)
            self._eq_filters[track_id].set_params(params, sample_rate=self._sample_rate)

    def set_track_effect_chain(self, track_id: str, chain_params: Optional[EffectChainParams]) -> None:
        """设置某轨的 FX 机架效果器链（W26）。线程安全。

        在 strip EQ 之后应用（post-fader，post-EQ）。

        Args:
            track_id: 轨道 ID
            chain_params: EffectChainParams，None 表示清空链
        """
        entries = chain_params.to_dsp_entries() if chain_params is not None else None
        with QMutexLocker(self._mutex):
            if track_id not in self._effect_chains:
                self._effect_chains[track_id] = EffectChain(self._sample_rate)
            self._effect_chains[track_id].set_params(entries, sample_rate=self._sample_rate)

    def set_clip_effects(
        self,
        clip_id: str,
        entries: Optional[list],
        track_id: Optional[str] = None,
        animated_sampler: Optional[Any] = None,
    ) -> None:
        """设置某音频 clip 的 per-clip 效果器链（P-clip-FX）。线程安全。

        在 decoder 解码后、track volume/pan 之前应用（item-FX 语义，
        与 Reaper item FX / Pro Tools clip effects 一致）。

        entries 复用 EffectChain 的输入格式：
            [(effect_type: str, params: dict|None, enabled: bool), ...] 按 order 排序；
            None 或空列表表示清空该 clip 的链。

        调用方负责把 clip.effects 中属于 mixer audio 的 EffectInstance 过滤
        并转成此格式（见 edit_page._sync_audio_tracks）。

        Args:
            clip_id: clip ID（decoder._clip_id）
            track_id: 该 clip 所属 track ID（用于 remove_track 反查清理）
            animated_sampler: D04——``frame -> entries`` 采样器；提供时
                mix_samples 在处理该 clip 的每个块前按当前帧更新链参数
                （animated_params 关键帧真实作用于预览声音）。None=静态。
        """
        with QMutexLocker(self._mutex):
            if track_id is not None:
                self._clip_to_track[clip_id] = track_id
            if not entries:
                # 清空：直接移除缓存的链（下次该 clip 无效果处理）
                self._clip_effect_chains.pop(clip_id, None)
                self._clip_effect_samplers.pop(clip_id, None)
                return
            if clip_id not in self._clip_effect_chains:
                self._clip_effect_chains[clip_id] = EffectChain(self._sample_rate)
            self._clip_effect_chains[clip_id].set_params(entries, sample_rate=self._sample_rate)
            if animated_sampler is not None:
                self._clip_effect_samplers[clip_id] = animated_sampler
            else:
                self._clip_effect_samplers.pop(clip_id, None)

    def set_audio_transitions(self, track_id: str, transitions: Optional[list]) -> None:
        """注册某音频轨的转场实例（AT — Audio Transitions）。线程安全。

        仅消费 ``audio:`` 命名空间的 :class:`TransitionInstance`（视频转场
        不影响音频）。``mix_samples`` 在解码每个 clip 时查询该 clip 是否作为
        某转场的 left/right，并在转场跨度内叠加 crossfade 包络。

        Args:
            track_id: 音频轨道 ID
            transitions: ``track.transitions`` 全量列表（调用方传整条 track 的
                transitions 即可，本方法会过滤只留 audio:* 实例）；``None``/空
                表示该轨无音频转场。
        """
        with QMutexLocker(self._mutex):
            if not transitions:
                self._audio_transitions.pop(track_id, None)
                return
            from src.features.edit.core.audio_transition_catalog import is_audio_transition
            audio_only = tuple(
                t for t in transitions
                if getattr(t, "is_audio", False) or is_audio_transition(getattr(t, "transition_type", ""))
            )
            if audio_only:
                self._audio_transitions[track_id] = audio_only
            else:
                self._audio_transitions.pop(track_id, None)

    def set_track_automation(self, track_id: str, automation: Dict[str, Any]) -> None:
        """设置某轨的 automation 数据（W21）。线程安全。

        Args:
            track_id: 轨道 ID
            automation: {param: KeyframeTrack}（param="volume"/"pan"/"mute"）
        """
        with QMutexLocker(self._mutex):
            self._automation[track_id] = dict(automation) if automation else {}

    def set_track_automation_mode(self, track_id: str, mode: str) -> None:
        """AM-07: 设置某轨的自动化模式（off/read/write/touch/latch）。线程安全。

        引擎据此判断是否读取 automation 曲线：
        - "off"：不读取（用静态 volume/pan/mute 值）
        - "read"/"write"/"touch"/"latch"：读取曲线覆盖静态值
          （write/touch/latch 的录制采集留待完整实现，当前先支持模式持久化与读取判断）
        """
        with QMutexLocker(self._mutex):
            normalized = str(mode).lower().strip()
            if normalized not in ("off", "read", "write", "touch", "latch"):
                normalized = "off"
            self._automation_modes[track_id] = normalized

    def _apply_audio_crossfade(
        self,
        samples: np.ndarray,
        track_id: str,
        clip_id: str,
        current_frame: int,
        fps: float,
    ) -> np.ndarray:
        """AT: 若该 clip 参与某音频转场，叠加 crossfade 包络。

        对该 clip 在本块覆盖的样本，按其角色（left=淡出 / right=淡入）和
        转场曲线乘上交叉淡化增益。clip 不在任何转场中、或本块完全在跨度外时，
        原样返回。在调用方（mix_samples）的 _mutex 下访问，故无需再加锁。
        """
        transitions = self._audio_transitions.get(track_id)
        if not transitions or not clip_id or samples.shape[-1] == 0:
            return samples
        from src.features.edit.core.audio_transition_catalog import audio_curve_for
        from src.features.edit.core.audio_dsp import apply_crossfade_block
        out = samples
        for tr in transitions:
            role: Optional[str] = None
            if tr.left_clip_id == clip_id:
                role = "left"
            elif tr.right_clip_id == clip_id:
                role = "right"
            else:
                continue
            curve = audio_curve_for(tr.transition_type)
            if curve is None:
                continue
            try:
                out = apply_crossfade_block(
                    out,
                    transition_start_frame=tr.start_frame,
                    transition_duration_frames=tr.duration,
                    current_frame=current_frame,
                    role=role,
                    curve=curve,
                    fps=fps if fps > 0 else 24.0,
                    sample_rate=float(self._sample_rate),
                )
            except Exception as e:  # pragma: no cover - defensive, never break playback
                logger.debug(f"audio crossfade apply failed: {e}")
        return out

    def set_track_routing(self, track_id: str, output_bus_id: Optional[str],
                          sends: list, is_bus: bool = False) -> None:
        """设置某轨的路由信息（W24/W25）。线程安全。

        Args:
            track_id: 轨道 ID
            output_bus_id: 输出目标 Bus 的 track_id（None=直接到 Master）
            sends: [(target_bus_id, level), ...] 发送列表
            is_bus: 该轨是否是 Bus 轨（Bus 轨无 decoder，只接收 send/routing）
        """
        with QMutexLocker(self._mutex):
            self._track_routing[track_id] = {
                "output_bus_id": output_bus_id,
                "sends": list(sends) if sends else [],
                "is_bus": bool(is_bus),
            }
            # AUD-15506: 路由变化 → 闭包缓存失效
            self._invalidate_solo_closure()

    def has_solo_tracks(self) -> bool:
        """
        是否有 solo 轨道。

        Returns:
            是否有任何轨道处于 solo 状态
        """
        with QMutexLocker(self._mutex):
            # A5: 用计数器替代 any() 遍历，O(1) 查询
            return self._solo_count > 0

    def mix_samples(
        self,
        current_frame: int,
        fps: float,
        num_samples: int,
        output_channels: int = 2,
    ) -> tuple:
        """
        混合指定帧的音频采样。

        Args:
            current_frame: 当前帧号
            fps: 帧率
            num_samples: 需要的采样数
            output_channels: 输出声道数

        Returns:
            (mixed_output, per_track_peaks)
            - mixed_output: 混合后的音频数组 (channels, samples)
            - per_track_peaks: {track_id: TrackLevel} 每轨电平数据，其中 TrackLevel
              是 dict {"peak_l", "peak_r", "rms_l", "rms_r"}（线性振幅）。
              已应用该轨 volume/pan/EQ，未经母带软限幅，供 mixer 电平表使用。
              被跳过的轨（muted/非 solo）不出现。

              注意：为向后兼容，TrackLevel 同时支持索引访问 [0]/[1]（返回 peak_l/peak_r）。
        """
        with QMutexLocker(self._mutex):
            # A5: 用计数器替代 any() 遍历，避免每帧扫描全表
            has_solo = self._solo_count > 0

            # RMA-04: 计算 solo 可听闭包。旧逻辑"solo 普通轨→剔除未 solo 的 Bus"
            # 会把该轨必经的 Bus 也跳过（无声）；"solo Bus→剔除未 solo 普通轨"会让
            # Bus 收不到上游输入。闭包保证 solo 节点到 Master 的完整可听路径。
            # AUD-15506: 闭包只在拓扑/solo 变化时重算（所有变更入口置缓存失效），
            # 播放中的每个 ~40ms 混音块直接复用，不再逐块做图遍历+集合分配。
            audible_solo: set = set()
            if has_solo:
                audible_solo = self._solo_closure_cache
                if audible_solo is None:
                    solo_ids = [tid for tid, st in self._tracks.items() if st.solo]
                    all_track_ids = list(self._tracks.keys())
                    audible_solo = compute_audible_solo_closure(
                        all_track_ids, self._track_routing, solo_ids,
                    )
                    self._solo_closure_cache = audible_solo

            # 输出缓冲区（Master）
            output = np.zeros((output_channels, num_samples), dtype=np.float32)
            # 每轨电平（线性振幅），按 track_id 累计
            per_track_peaks: Dict[str, "TrackLevel"] = {}
            # W25: Bus 输入缓冲区（{bus_id: ndarray}），阶段 1 累积，阶段 2 处理
            bus_inputs: Dict[str, np.ndarray] = {}
            # W25/AM-14: 注册 Bus 集合。提前计算一次，阶段 1 的路由目标校验
            # （AUD-15402）与阶段 2 的拓扑收集共用；整段调用持锁，两处一致。
            all_registered_buses = [
                tid for tid in self._tracks
                if isinstance(self._track_routing.get(tid), dict)
                and self._track_routing[tid].get("is_bus")
            ]
            registered_bus_set = set(all_registered_buses)

            # 遍历所有普通音频轨（非 Bus）
            for track_id, decoders in self._decoders.items():
                state = self._tracks.get(track_id)
                if not state or not state.enabled:
                    continue
                # W25: 跳过 Bus 轨（它们在阶段 2 处理，不在阶段 1 解码 clips）
                routing = self._track_routing.get(track_id, {})
                if routing.get("is_bus", False):
                    continue

                # 计算有效音量
                if state.muted:
                    continue

                # RMA-04: solo 用可听闭包判断。旧逻辑 `not state.solo` 会把 solo 普通
                # 轨的上游输入（喂给 solo Bus 的普通轨）错误剔除。闭包保留这些上游。
                if has_solo and track_id not in audible_solo:
                    continue

                volume = state.volume
                pan = state.pan
                # AM-07/§3.2: automation 模式判断 + 逐样本 ramp（替代每帧单标量）。
                # Off 模式不读取曲线（用静态值）。Read/Write/Touch/Latch 读取曲线。
                # §3.2: 当一个 block 内 automation 快速变化时，逐样本 ramp 避免阶跃
                # click，斜坡段误差 < 0.1dB（计划表验收）。
                auto_mode = self._automation_modes.get(track_id, "off")
                auto = self._automation.get(track_id) if auto_mode != "off" else None
                # 逐样本 ramp（None=该参数无曲线，用标量）
                volume_ramp = None
                pan_ramp = None
                mute_gate = None
                if auto:
                    vol_track = auto.get("volume")
                    if vol_track is not None and vol_track.keyframes:
                        volume_ramp = np.array(
                            vol_track.get_value_ramp(
                                float(current_frame), fps, num_samples,
                                float(self._sample_rate), volume,
                            ),
                            dtype=np.float32,
                        )
                    pan_track = auto.get("pan")
                    if pan_track is not None and pan_track.keyframes:
                        pan_ramp = np.array(
                            pan_track.get_value_ramp(
                                float(current_frame), fps, num_samples,
                                float(self._sample_rate), pan,
                            ),
                            dtype=np.float32,
                        )
                    mute_track = auto.get("mute")
                    if mute_track is not None and mute_track.keyframes:
                        # RMA-07: 普通轨 mute 也必须逐样本门控。旧逻辑只读
                        # block 起点，自动化切点落在 block 内时会提前/延后最多一个
                        # 音频块，并产生阶跃 click；Bus/Master 已使用同一语义。
                        # AUD-15506: de-click 函数经 mixer 惰性缓存，不再逐块 import。
                        mute_ramp = np.array(
                            mute_track.get_value_ramp(
                                float(current_frame), fps, num_samples,
                                float(self._sample_rate), 0.0,
                            ),
                            dtype=np.float32,
                        )
                        mute_gate = (mute_ramp <= 0.5).astype(np.float32)
                        mute_gate = self._apply_declick(
                            mute_gate, float(self._sample_rate),
                        )
                        if not np.any(mute_gate):
                            continue
                # 累计该轨电平（跨多个 decoder/clip：peak 取 max，RMS 用平方和均值）
                track_peak_l = 0.0
                track_peak_r = 0.0
                # RMS 用平方和累计（最后除以总样本数）
                track_sum_sq_l = 0.0
                track_sum_sq_r = 0.0
                track_sample_count = 0
                # W11: 该轨总输出缓冲（供 LUFS 测量）
                # A3: 懒分配——仅在该轨确实解码出样本时才创建，避免无 clip 轨
                # 每帧分配 num_samples × output_channels × float32 的空缓冲。
                track_output_acc: Optional[np.ndarray] = None

                for decoder in self._iter_decoders(decoders):
                    # 解码音频
                    samples = decoder.decode_samples_for_frame(
                        current_frame, fps, num_samples
                    )

                    if samples is None:
                        continue

                    # A3: 首次拿到样本时才分配该轨累积缓冲（复用 scratch，零分配）
                    if track_output_acc is None:
                        track_output_acc = self._mix_scratch(
                            self._mix_acc_scratch, track_id, output_channels, num_samples
                        )

                    # F2.1 · Source Patching：素材级源声道映射先于 downmix 选道
                    # （5.1 只送 L/R、单选 C 做 ADR 对轨等）。select_channels
                    # 返回同一对象 = 无映射或解码布局与映射基线不符（退直通）。
                    _src_layout = getattr(decoder, "source_layout", None)
                    _patch_map = getattr(decoder, "_source_channel_map", None)
                    if _patch_map is not None:
                        _selected = SourcePatchSpec(
                            audio_channel_map=_patch_map,
                        ).select_channels(samples)
                        if _selected is not samples:
                            samples = _selected
                            # 选道后的布局是自定义的，不能再声明容器布局
                            _src_layout = None

                    # AM-08 阶段 4.1 / RMA-09: 声道映射用标准 downmix（替代 naive 截断）。
                    # RMA-09: 用 decoder 的真实容器布局（如 5.1(side)），而非按声道数猜测。
                    if samples.shape[0] != output_channels:
                        try:
                            # AUD-15506: adapt_channels 已提升为模块级导入
                            samples = adapt_channels(
                                samples, output_channels,
                                src_layout=_src_layout,
                            )
                        except ValueError:
                            # 非标准声道数（如 4 声道）：回退安全截断避免阻断播放
                            if samples.shape[0] == 1:
                                samples = np.repeat(samples, output_channels, axis=0)
                            elif samples.shape[0] > output_channels:
                                samples = samples[:output_channels]
                            elif samples.shape[0] < output_channels:
                                # 审计修复：1<src<out（如 4ch 源→6ch 输出）
                                # 旧代码两分支都不命中，样本宽度保持错误。
                                # 与导出侧回退一致：首声道广播到目标声道。
                                samples = np.repeat(
                                    samples[:1], output_channels, axis=0)

                    # RMA-03: clip 淡入/淡出（实时路径此前完全不处理，与离线导出漂移）。
                    # 在声道扩展后、clip-FX/track 处理前应用，作用于该 clip 原始信号。
                    # apply_clip_fade 按 current_frame 在 clip 时间线范围内的位置算包络。
                    fade = getattr(decoder, "_fade", None)
                    if fade is not None and samples.shape[-1] > 0:
                        # AUD-15506: apply_clip_fade 已提升为模块级导入
                        try:
                            samples = apply_clip_fade(
                                samples,
                                clip_timeline_start=fade["timeline_start"],
                                clip_timeline_duration=fade["timeline_duration"],
                                current_frame=current_frame,
                                fade_in_frames=fade["fade_in_frames"],
                                fade_out_frames=fade["fade_out_frames"],
                                fps=fade["fps"],
                                sample_rate=float(self._sample_rate),
                                fade_in_samples_override=fade.get(
                                    "fade_in_samples"),
                                fade_out_samples_override=fade.get(
                                    "fade_out_samples"),
                                audio_sample_offset=int(fade.get(
                                    "audio_sample_offset", 0) or 0),
                            )
                        except Exception as e:
                            logger.debug(f"clip fade apply failed: {e}")

                    # C3: 片段级增益（clip gain → track volume 之前应用）。
                    # dB → 线性：10^(dB/20)；0 dB = 恒等。
                    gain_db = getattr(decoder, "_gain_db", 0.0) or 0.0
                    if abs(gain_db) > 1e-9 and samples.shape[-1] > 0:
                        samples = samples * (10.0 ** (gain_db / 20.0))

                    # AT (Audio Transition): 若该 clip 参与音频转场，叠加 crossfade
                    # 包络。与 clip fade 同级（源电平调整），在 clip-FX/volume 之前。
                    clip_id = getattr(decoder, "_clip_id", None)
                    if clip_id is not None:
                        samples = self._apply_audio_crossfade(
                            samples, track_id, clip_id, current_frame, fps
                        )
                        # P-clip-FX: 应用该 clip 的 per-clip 效果器链
                        # （item-FX 语义，pre-fader / pre-track-FX）。
                        # 在声道扩展之后、track volume/pan 之前处理，保证效果器
                        # 拿到的是该 clip 自己的原始信号（未被轨道音量缩放）。
                        clip_chain = self._clip_effect_chains.get(clip_id)
                        if clip_chain is not None:
                            # R08（第七轮审计）：动画求值先于 is_active 判断
                            # ——静态参数中性（如 drive=0）的链必须在关键帧
                            # 激活时被采样唤醒；先判 active 会永远跳过采样
                            #（0→非0 动画实时听不到，active↔inactive 交替
                            # 也无法再次激活）。无采样器的静态链不进此分支。
                            fx_sampler = self._clip_effect_samplers.get(clip_id)
                            if fx_sampler is not None:
                                # D04：动画 FX 按当前帧求值更新链参数
                                #（EffectChain.set_params 复用实例，DSP 状态
                                # 连续；失败沿用上一帧参数，不阻断混音）。
                                try:
                                    clip_chain.set_params(
                                        fx_sampler(current_frame),
                                        sample_rate=self._sample_rate,
                                    )
                                except Exception:  # noqa: BLE001
                                    pass
                            if clip_chain.is_active:
                                samples = clip_chain.process(samples)

                    # 应用音量和声像（§3.2: 逐样本 ramp 替代每帧单标量）
                    # 注：in-place 分支仅用于独占数组（base is None，即
                    # decode/resample/concatenate 的产物）；视图走拷贝乘，
                    # 避免污染 decoder 缓冲（多声道截取 samples[:ch] 等）。
                    if volume_ramp is not None:
                        n = samples.shape[-1]
                        vr = volume_ramp[:n]
                        samples = samples * vr[np.newaxis, :]
                    elif volume == 1.0:
                        # 默认音量（绝大多数轨）：跳过整段乘法与分配。
                        pass
                    elif samples.base is None:
                        # 独占数据：原位乘，零分配。
                        samples *= volume
                    else:
                        samples = samples * volume

                    # 应用声像（RMA-08: 按源声道数选算法）。
                    # mono 源用 equal-power pan（center 恒功率 -3dB/侧），stereo 源用
                    # balance（unity-center，保持原响度与声像），surround 用 trim。
                    # §3.2: pan ramp 时逐样本计算 left/right gain。
                    # AUD-15506: pan_gains_for_channels 已提升为模块级导入。
                    src_ch = int(getattr(decoder, "_channels", 2) or 2)
                    if pan_ramp is not None:
                        n = samples.shape[-1]
                        pr = np.clip(pan_ramp[:n], -1.0, 1.0)
                        if src_ch <= 1:
                            # mono: equal-power（cos/sin）
                            theta = (pr + 1.0) * 0.5 * (np.pi / 2.0)
                            left_gains = np.cos(theta).astype(np.float32)
                            right_gains = np.sin(theta).astype(np.float32)
                        else:
                            # stereo/surround: balance（unity-center）
                            left_gains = (1.0 - np.maximum(0.0, pr)).astype(np.float32)
                            right_gains = (1.0 + np.minimum(0.0, pr)).astype(np.float32)
                        samples[0] *= left_gains
                        if samples.shape[0] > 1:
                            samples[1] *= right_gains
                    else:
                        left_gain, right_gain = pan_gains_for_channels(pan, src_ch)
                        samples[0] *= left_gain
                        if samples.shape[0] > 1:
                            samples[1] *= right_gain

                    # W7: 应用 Strip EQ（post-fader，在 volume/pan 之后、累计 peak 之前）
                    # EQ 滤波器实例在 self._eq_filters 中按 track_id 缓存
                    eq_filter = self._eq_filters.get(track_id)
                    if eq_filter is not None and eq_filter.is_active:
                        samples = eq_filter.process(samples)

                    # W26: 应用 FX 机架效果器链（post-EQ，在 strip EQ 之后）
                    chain = self._effect_chains.get(track_id)
                    if chain is not None and chain.is_active:
                        samples = chain.process(samples)

                    # 将 mute 放在 strip 输出端，确保自动化切点同时门控 EQ/FX
                    # 的输出；这样不会让通道被静音后残留 effect tail 泄到 Master。
                    if mute_gate is not None:
                        samples = samples * mute_gate[np.newaxis, :samples.shape[-1]]

                    # 累计该轨电平（在补零/截取前，避免 padding 拉低 peak/RMS）
                    if samples.shape[0] >= 2 and samples.shape[-1] > 0:
                        track_peak_l = max(track_peak_l, float(np.max(np.abs(samples[0]))))
                        track_peak_r = max(track_peak_r, float(np.max(np.abs(samples[1]))))
                        # A2: RMS 平方和用 np.dot 替代 np.sum(x**2)，避免创建临时平方数组
                        track_sum_sq_l += float(np.dot(samples[0], samples[0]))
                        track_sum_sq_r += float(np.dot(samples[1], samples[1]))
                        track_sample_count += samples.shape[-1]
                    elif samples.shape[-1] > 0:
                        p = float(np.max(np.abs(samples[0])))
                        track_peak_l = max(track_peak_l, p)
                        track_peak_r = max(track_peak_r, p)
                        sq = float(np.dot(samples[0], samples[0]))
                        track_sum_sq_l += sq
                        track_sum_sq_r += sq
                        track_sample_count += samples.shape[-1]

                    # 混合到输出
                    # A2: 补零用 scratch 缓冲复用（替代每轨每帧一次 np.zeros
                    # 大数组分配）；截取分支是视图，天然零拷贝。
                    cur_len = samples.shape[-1]
                    if cur_len < num_samples:
                        padded = self._mix_scratch(
                            self._mix_pad_scratch, track_id, output_channels, num_samples
                        )
                        padded[:, :cur_len] = samples
                        samples = padded
                    elif cur_len > num_samples:
                        # 截取
                        samples = samples[..., :num_samples]

                    # W11: 累积到该轨输出（供 LUFS 测量）
                    track_output_acc[:, :samples.shape[-1]] += samples
                    # W25: 路由到目标（Bus 或 Master）+ 处理 sends
                    target_bus = routing.get("output_bus_id")
                    if target_bus is not None and target_bus in registered_bus_set:
                        # 路由到 Bus
                        if target_bus not in bus_inputs:
                            bus_inputs[target_bus] = self._mix_scratch(
                                self._mix_bus_scratch, target_bus,
                                output_channels, num_samples,
                            )
                        bus_inputs[target_bus][:, :samples.shape[-1]] += samples
                    elif target_bus is not None:
                        # AUD-15402: output_bus_id 指向未注册 bus（如 bus 已删除但
                        # 轨的路由未清理）。旧行为为未知目标也建缓冲——样本累积进
                        # 永不被消费的孤儿缓冲，整轨音频静默丢失。与 Bus 阶段及
                        # topological_sort_buses 的"未知目标视为 Master"语义统一：
                        # 兜底进 master 并告警。
                        logger.warning(
                            "audio track %s routes to unknown bus %s; falling back to master",
                            track_id, target_bus,
                        )
                        output += samples
                    else:
                        # 直接到 Master
                        output += samples
                    # 处理 sends（post-fader，并行发送到多个 Bus）
                    for send_bus_id, send_level in routing.get("sends", []):
                        if send_level <= 0.0:
                            continue
                        if send_bus_id not in registered_bus_set:
                            # AUD-15402: send 指向未注册 bus——并行 tap 落空，
                            # 跳过并告警（与 Bus 阶段 send 只认已注册目标一致）。
                            logger.warning(
                                "audio track %s send targets unknown bus %s; send skipped",
                                track_id, send_bus_id,
                            )
                            continue
                        if send_bus_id not in bus_inputs:
                            bus_inputs[send_bus_id] = self._mix_scratch(
                                self._mix_bus_scratch, send_bus_id,
                                output_channels, num_samples,
                            )
                        bus_inputs[send_bus_id][:, :samples.shape[-1]] += samples * send_level

                if track_peak_l > 0.0 or track_peak_r > 0.0:
                    # 计算 RMS（均方根）
                    if track_sample_count > 0:
                        rms_l = float(np.sqrt(track_sum_sq_l / track_sample_count))
                        rms_r = float(np.sqrt(track_sum_sq_r / track_sample_count))
                    else:
                        rms_l = rms_r = 0.0
                    # W11: 喂入 LUFS 响度计（用该轨完整输出，K-weighting 后计算）
                    # A3: track_output_acc 在该轨无样本时为 None，跳过 LUFS（peak 已为 0，
                    # 此分支仅在 peak>0 时进入，理论上 acc 不会是 None，这里做防御）。
                    # W11-gate: 无 LUFS 消费者时跳过 K-weighting（近一半混音耗时）。
                    lufs_m = -70.0
                    if (
                        self._lufs_metering_enabled
                        and track_output_acc is not None
                    ):
                        try:
                            if track_id not in self._lufs_meters:
                                self._lufs_meters[track_id] = LoudnessMeter(self._sample_rate, output_channels)
                            lm = self._lufs_meters[track_id]
                            lm.process(track_output_acc)
                            lufs_m = lm.momentary_lufs
                        except Exception:
                            pass
                    per_track_peaks[track_id] = TrackLevel(
                        peak_l=track_peak_l, peak_r=track_peak_r,
                        rms_l=rms_l, rms_r=rms_r, lufs=lufs_m,
                    )

            # W25/AM-14: 阶段 2 — 按拓扑序处理 Bus 轨（支持多层 Bus→Bus）。
            # 此前实现硬编码两层（Bus 直接到 Master）。现在用 DAG 拓扑排序，
            # 上游 Bus 处理后按自身 routing 输出到下游 Bus 或 Master。
            # 所有已注册的 Bus（含仅接收上游 Bus 输出、无普通轨直连的）都参与。
            # 确保每个注册 Bus 都有 buffer（无输入的 Bus buffer 为静音，正确）
            # （注册 Bus 集合 all_registered_buses 已在阶段 1 前计算，AUD-15402）
            for bid in all_registered_buses:
                if bid not in bus_inputs:
                    bus_inputs[bid] = self._mix_scratch(
                        self._mix_bus_scratch, bid, output_channels, num_samples
                    )
            sorted_buses, cycle_buses = topological_sort_buses(all_registered_buses, self._track_routing)
            if cycle_buses:
                logger.warning(
                    "audio bus routing cycle detected, skipping: %s", cycle_buses,
                )
            for bus_id in sorted_buses:
                bus_input = bus_inputs.get(bus_id)
                if bus_input is None:
                    continue
                bus_state = self._tracks.get(bus_id)
                if bus_state is None or not bus_state.enabled or bus_state.muted:
                    continue
                # RMA-04: solo 用可听闭包判断。旧逻辑 `not bus_state.solo` 会把 solo
                # 普通轨必经的 Bus（该 Bus 未 solo）错误剔除，导致 solo 普通轨无声。
                if has_solo and bus_id not in audible_solo:
                    continue
                # RMA-06: Bus automation（volume/pan/mute）逐样本求值。
                # 此前 Bus 阶段只读静态 bus_state.volume/pan，automation 被无声忽略。
                bus_volume = bus_state.volume
                bus_pan = bus_state.pan
                bus_auto_mode = self._automation_modes.get(bus_id, "off")
                bus_auto = self._automation.get(bus_id) if bus_auto_mode != "off" else None
                bus_vol_ramp = None
                bus_pan_ramp = None
                if bus_auto:
                    bvt = bus_auto.get("volume")
                    if bvt is not None and getattr(bvt, "keyframes", None):
                        bus_vol_ramp = np.array(
                            bvt.get_value_ramp(
                                float(current_frame), fps, num_samples,
                                float(self._sample_rate), bus_volume,
                            ),
                            dtype=np.float32,
                        )
                    bpt = bus_auto.get("pan")
                    if bpt is not None and getattr(bpt, "keyframes", None):
                        bus_pan_ramp = np.array(
                            bpt.get_value_ramp(
                                float(current_frame), fps, num_samples,
                                float(self._sample_rate), bus_pan,
                            ),
                            dtype=np.float32,
                        )
                    bmt = bus_auto.get("mute")
                    if bmt is not None and getattr(bmt, "keyframes", None):
                        # RMA-07: 逐样本 mute 门控 + de-click
                        # AUD-15506: de-click 函数经 mixer 惰性缓存。
                        bmute_ramp = np.array(
                            bmt.get_value_ramp(
                                float(current_frame), fps, num_samples,
                                float(self._sample_rate), 0.0,
                            ),
                            dtype=np.float32,
                        )
                        bgate = (bmute_ramp <= 0.5).astype(np.float32)
                        bgate = self._apply_declick(bgate, float(self._sample_rate))
                        bus_input = bus_input * bgate[np.newaxis, :]
                # volume（automation ramp 或静态）
                if bus_vol_ramp is not None:
                    bus_samples = bus_input * bus_vol_ramp[np.newaxis, :]
                else:
                    bus_samples = bus_input * bus_volume
                # pan（RMA-08: Bus 输入是已混的立体声，用 balance unity-center，
                # 保持 center 响度与声像；不再用 equal-power 把整体降 -3dB）
                # AUD-15506: balance_gains 已提升为模块级导入。
                if bus_pan_ramp is not None and bus_samples.shape[0] >= 2:
                    bus_samples = bus_samples.copy()
                    for bi in range(min(len(bus_pan_ramp), bus_samples.shape[-1])):
                        lg, rg = balance_gains(float(bus_pan_ramp[bi]))
                        bus_samples[0, bi] *= lg
                        bus_samples[1, bi] *= rg
                else:
                    lg, rg = balance_gains(bus_pan)
                    bus_samples[0] *= lg
                    if bus_samples.shape[0] > 1:
                        bus_samples[1] *= rg
                # EQ
                bus_eq = self._eq_filters.get(bus_id)
                if bus_eq is not None and bus_eq.is_active:
                    bus_samples = bus_eq.process(bus_samples)
                # W26: FX 机架效果器链（post-EQ）
                bus_chain = self._effect_chains.get(bus_id)
                if bus_chain is not None and bus_chain.is_active:
                    bus_samples = bus_chain.process(bus_samples)
                # AM-14: Bus 处理后按自身 routing 输出（Bus→Bus 或 →Master）
                bus_routing = self._track_routing.get(bus_id, {})
                bus_target = bus_routing.get("output_bus_id") if isinstance(bus_routing, dict) else None
                if bus_target is not None and bus_target in bus_inputs:
                    # 输出到下游 Bus（多层路由）
                    bus_inputs[bus_target][:, :bus_samples.shape[-1]] += bus_samples
                else:
                    if bus_target is not None:
                        # AUD-15402: Bus 输出指向未注册 bus——兜底进 master 并告警
                        logger.warning(
                            "audio bus %s routes to unknown bus %s; falling back to master",
                            bus_id, bus_target,
                        )
                    # 输出到 Master（output_bus_id=None 或指向非 Bus）
                    output += bus_samples
                # RMA-06: Bus sends（Bus 也可 send 到其他 Bus，并行 tap）。
                # 此前实时 Bus 阶段只处理 output_bus_id，sends 被无声忽略。
                if isinstance(bus_routing, dict):
                    for send_bus_id, send_level in bus_routing.get("sends", []) or []:
                        if send_level <= 0.0:
                            continue
                        if send_bus_id in bus_inputs:
                            bus_inputs[send_bus_id][:, :bus_samples.shape[-1]] += bus_samples * send_level
                        elif send_bus_id not in registered_bus_set:
                            # AUD-15402: Bus send 指向未注册 bus——并行 tap 落空，告警
                            logger.warning(
                                "audio bus %s send targets unknown bus %s; send skipped",
                                bus_id, send_bus_id,
                            )
                # per-track peak（Bus 电平）
                if bus_samples.shape[0] >= 2:
                    bl = float(np.max(np.abs(bus_samples[0])))
                    br = float(np.max(np.abs(bus_samples[1])))
                else:
                    bl = br = float(np.max(np.abs(bus_samples[0])))
                if bl > 0.0 or br > 0.0:
                    rms_bl = float(np.sqrt(np.mean(bus_samples[0] ** 2))) if bus_samples.shape[-1] > 0 else 0.0
                    rms_br = float(np.sqrt(np.mean(bus_samples[1] ** 2))) if bus_samples.shape[0] > 1 and bus_samples.shape[-1] > 0 else rms_bl
                    per_track_peaks[bus_id] = TrackLevel(
                        peak_l=bl, peak_r=br, rms_l=rms_bl, rms_r=rms_br, lufs=-70.0,
                    )

            # Master 总线增益 + 静音（在软限幅之前应用，让正增益真正放大信号）
            # §3.1: master automation（volume ramp + mute 门控），与轨级 automation 一致。
            # Off 模式用静态 master_volume/muted；Read/etc 读取曲线逐样本 ramp。
            # RMA-07: master mute automation 此前只读 block 起点（整 block 单值），
            # 改为逐样本门控（>0.5 静音 + 边界 de-click），与导出 apply_master 一致。
            master_auto_mode = self._master_automation_mode
            master_auto = self._master_automation if master_auto_mode != "off" else None
            static_muted = self._master_muted
            if static_muted:
                output *= 0.0
            else:
                # mute automation 逐样本门控
                if master_auto:
                    mute_track = master_auto.get("mute")
                    if mute_track is not None and getattr(mute_track, "keyframes", None):
                        # AUD-15506: de-click 函数经 mixer 惰性缓存。
                        mute_ramp = np.array(
                            mute_track.get_value_ramp(
                                float(current_frame), fps, output.shape[-1],
                                float(self._sample_rate), 0.0,
                            ),
                            dtype=output.dtype,
                        )
                        gate = (mute_ramp <= 0.5).astype(output.dtype)
                        gate = self._apply_declick(gate, float(self._sample_rate))
                        output = output * gate[np.newaxis, :]
                # volume automation ramp（逐样本）或静态增益
                vol_ramp = None
                if master_auto:
                    vol_track = master_auto.get("volume")
                    if vol_track is not None and getattr(vol_track, "keyframes", None):
                        vol_ramp = np.array(
                            vol_track.get_value_ramp(
                                float(current_frame), fps, output.shape[-1],
                                float(self._sample_rate), self._master_volume,
                            ),
                            dtype=output.dtype,
                        )
                if vol_ramp is not None:
                    output = output * vol_ramp[np.newaxis, :]
                elif self._master_volume != 1.0:
                    output *= self._master_volume

            # AM-09: 连续状态 Master 限幅器（替代无状态逐 buffer 峰值归一化）。
            # 跨 buffer 平滑增益包络，避免相邻 buffer 缩放比例不同造成的 pumping。
            # 在 master 增益之后跑，保护正增益（master_volume > 1.0）导致的削波。
            output = self._master_limiter.process(output)

            return output, per_track_peaks


# =============================================================================
# AudioOutput (QtMultimedia)
# =============================================================================

class AudioOutput(QObject):
    """
    音频输出。

    使用 QtMultimedia QAudioSink 输出音频。

    Signals:
        samples_played: 已播放采样数
        buffer_low: 缓冲区低水位
    """

    samples_played = pyqtSignal(int)
    buffer_low = pyqtSignal()

    def __init__(
        self,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = 2,
        buffer_frames: int = AUDIO_OUTPUT_FRAMES,
        parent: Optional[QObject] = None,
    ) -> None:
        """
        初始化音频输出。

        Args:
            sample_rate: 采样率
            channels: 声道数
            parent: QObject 父对象
        """
        super().__init__(parent)

        self._sample_rate: int = sample_rate
        self._channels: int = channels
        self._buffer_frames: int = max(256, int(buffer_frames))

        # 创建音频格式
        self._format: QAudioFormat = QAudioFormat()
        self._format.setSampleRate(sample_rate)
        self._format.setChannelCount(channels)
        self._format.setSampleFormat(QAudioFormat.SampleFormat.Float)

        # 创建音频输出
        self._audio_sink: Optional[QAudioSink] = None
        self._io_device: Optional[Any] = None

        # 缓冲区
        self._buffer: bytearray = bytearray()
        self._buffer_mutex: QMutex = QMutex()

        # 状态
        self._is_playing: bool = False
        self._samples_written: int = 0

        logger.debug(f"AudioOutput created: rate={sample_rate}, channels={channels}")

    def start(self) -> bool:
        """
        启动音频输出。

        Returns:
            是否成功启动
        """
        try:
            # 获取默认音频设备
            device = QMediaDevices.defaultAudioOutput()
            playback_trace(
                "audio_output.start",
                sample_rate=self._format.sampleRate(),
                channels=self._format.channelCount(),
                sample_format=self._format.sampleFormat(),
                device=device.description(),
            )
            if not device.isFormatSupported(self._format):
                # 尝试最接近的格式
                nearest = device.preferredFormat()
                if nearest.sampleRate() <= 0 or nearest.channelCount() <= 0:
                    logger.warning("Audio format not supported")
                    return False
                self._format = nearest
                self._sample_rate = self._format.sampleRate()
                self._channels = self._format.channelCount()

            # 创建音频 sink
            self._audio_sink = QAudioSink(device, self._format, self)
            self._audio_sink.setBufferSize(self._buffer_frames * self.bytes_per_frame())

            # 启动
            self._io_device = self._audio_sink.start()
            if self._io_device is None:
                playback_trace("audio_output.start_failed", reason="no_io_device")
                logger.warning("Failed to start audio sink")
                return False

            self._is_playing = True
            self._samples_written = 0

            logger.debug("AudioOutput started")
            return True

        except Exception as e:
            playback_trace("audio_output.start_failed", reason=e)
            logger.error(f"Failed to start audio output: {e}")
            return False

    def stop(self) -> None:
        """停止音频输出。"""
        if self._audio_sink:
            try:
                self._audio_sink.stop()
            except RuntimeError:
                pass
            self._audio_sink = None
            self._io_device = None

        self._is_playing = False
        self._buffer.clear()
        logger.debug("AudioOutput stopped")

    def suspend(self) -> None:
        """暂停音频输出。"""
        if self._audio_sink:
            try:
                self._audio_sink.suspend()
            except RuntimeError:
                self._audio_sink = None
                self._io_device = None
            self._is_playing = False
            logger.debug("AudioOutput suspended")

    def resume(self) -> None:
        """恢复音频输出。"""
        if self._audio_sink:
            try:
                self._audio_sink.resume()
                self._is_playing = True
                logger.debug("AudioOutput resumed")
            except RuntimeError:
                self._audio_sink = None
                self._io_device = None
                self._is_playing = False

    def reset_buffer(self, keep_suspended: bool = False) -> None:
        """
        清空设备端已经排队的旧样本。

        视频缓冲结束后如果音频发生 seek，QAudioSink 里可能还残留 seek
        之前写入的样本。恢复播放前清空这些样本，避免旧声音先播放导致
        杂音和明显 A/V 偏移。
        """
        if self._audio_sink is None:
            self._buffer.clear()
            self._samples_written = 0
            self._is_playing = False
            return

        should_suspend = keep_suspended or not self._is_playing
        try:
            self._audio_sink.stop()
            self._io_device = self._audio_sink.start()
            self._buffer.clear()
            self._samples_written = 0
            self._is_playing = self._io_device is not None
            if should_suspend and self._audio_sink is not None:
                self._audio_sink.suspend()
                self._is_playing = False
            playback_trace(
                "audio_output.reset_buffer",
                keep_suspended=keep_suspended,
                restarted=self._io_device is not None,
            )
        except Exception as exc:
            playback_trace("audio_output.reset_buffer_failed", error=exc)
            logger.debug("AudioOutput reset_buffer failed: %s", exc)

    def write_samples(self, samples: np.ndarray) -> int:
        """
        写入音频采样。

        Args:
            samples: 音频数组 (channels, samples)

        Returns:
            写入的采样数
        """
        if not self._is_playing or self._io_device is None:
            return 0

        try:
            data, bytes_per_sample, num_samples = self._samples_to_output_bytes(samples)

            # 写入
            written = self._io_device.write(data)

            if written > 0:
                self._samples_written += written // (self._channels * bytes_per_sample)
                self.samples_played.emit(self._samples_written)

            return written // (self._channels * bytes_per_sample)

        except RuntimeError as e:
            self._is_playing = False
            self._io_device = None
            playback_trace("audio_output.write_device_deleted", error=e)
            logger.debug("Audio output device disappeared while writing: %s", e)
            return 0
        except Exception as e:
            logger.error(f"Failed to write audio samples: {e}")
            return 0

    def bytes_per_sample(self) -> int:
        """Return bytes per one channel sample for the active output format."""
        sample_format = self._format.sampleFormat()
        if sample_format == QAudioFormat.SampleFormat.Int16:
            return 2
        if sample_format == QAudioFormat.SampleFormat.UInt8:
            return 1
        return 4

    def bytes_per_frame(self) -> int:
        """Return bytes for one interleaved audio sample frame."""
        return self._channels * self.bytes_per_sample()

    def bytes_free(self) -> int:
        """Return writable bytes currently available in QAudioSink."""
        if self._audio_sink is None or not self._is_playing:
            return 0
        try:
            return int(self._audio_sink.bytesFree())
        except RuntimeError:
            self._audio_sink = None
            self._io_device = None
            self._is_playing = False
            return 0
        except Exception:
            return 0

    def _samples_to_output_bytes(self, samples: np.ndarray) -> tuple[bytes, int, int]:
        """
        Convert normalized float samples into the active QAudioFormat bytes.

        Args:
            samples: (channels, samples) float-like array in [-1, 1].

        Returns:
            (data, bytes_per_sample, num_samples)
        """
        if samples.ndim == 1:
            samples = samples.reshape(1, -1)
        # AM-08 阶段 4.1：输出阶段声道映射（标准 downmix 替代截断）。
        # mix_samples 已做 downmix，此处通常是冗余保护，但保持一致。
        if samples.shape[0] != self._channels:
            try:
                from src.features.edit.core.channel_layout import adapt_channels
                samples = adapt_channels(samples, self._channels)
            except ValueError:
                if samples.shape[0] == 1:
                    samples = np.repeat(samples, self._channels, axis=0)
                else:
                    samples = samples[:self._channels]

        num_samples = samples.shape[-1]
        interleaved = np.clip(samples.T.reshape(-1), -1.0, 1.0)
        sample_format = self._format.sampleFormat()

        if sample_format == QAudioFormat.SampleFormat.Int16:
            pcm = interleaved.astype(np.float64) * 32767.0
            return pcm.astype(np.int16).tobytes(), 2, num_samples
        if sample_format == QAudioFormat.SampleFormat.Int32:
            pcm = interleaved.astype(np.float64) * 2147483647.0
            return pcm.astype(np.int32).tobytes(), 4, num_samples
        if sample_format == QAudioFormat.SampleFormat.UInt8:
            return ((interleaved + 1.0) * 127.5).astype(np.uint8).tobytes(), 1, num_samples

        return interleaved.astype(np.float32).tobytes(), 4, num_samples

    def get_buffer_size_ms(self) -> int:
        """
        获取缓冲区大小（毫秒）。

        Returns:
            缓冲区大小（ms）
        """
        if self._audio_sink is None:
            return 0

        # QAudioSink.bufferSize() 返回 bytes
        buffer_bytes = self._audio_sink.bufferSize()
        samples_in_buffer = buffer_bytes // self.bytes_per_frame()
        buffer_ms = int(samples_in_buffer * 1000 / self._sample_rate)

        return buffer_ms

    def get_processed_samples(self) -> int:
        """
        获取已处理的采样数。

        Returns:
            已处理采样数
        """
        return self._samples_written

    def get_volume(self) -> float:
        """
        获取音量。

        Returns:
            音量（0.0-1.0）
        """
        if self._audio_sink:
            return self._audio_sink.volume()
        return 0.0

    def set_volume(self, volume: float) -> None:
        """
        设置音量。

        Args:
            volume: 音量（0.0-1.0）
        """
        if self._audio_sink:
            self._audio_sink.setVolume(max(0.0, min(1.0, volume)))

    @property
    def is_playing(self) -> bool:
        """是否正在播放。"""
        return self._is_playing


# =============================================================================
# AudioPlaybackThread
# =============================================================================

class AudioPlaybackThread(QThread):
    """
    音频播放线程。

    在后台解码和输出音频，更新时钟。

    Signals:
        frame_sync: 请求视频帧同步 (frame_number)
        sync_error: 同步误差通知 (error_ms)
        output_failed: 音频输出失败（启动失败/设备中途丢失），reason 字符串；
            manager 收到后统一走 stop() + state_changed（AUD-15508）
    """

    frame_sync = pyqtSignal(int)
    sync_error = pyqtSignal(float)
    audio_level = pyqtSignal(list)  # [left_db, right_db]
    audio_track_levels = pyqtSignal(dict)  # {track_id: [left_db, right_db]} per-track 电平
    output_failed = pyqtSignal(str)  # AUD-15508

    def __init__(self, parent: Optional[QObject] = None) -> None:
        """
        初始化播放线程。
        """
        super().__init__(parent)

        self._clock: Optional[AudioClock] = None
        self._mixer: Optional[AudioMixer] = None
        self._output: Optional[AudioOutput] = None
        self._fps: float = 24.0
        self._samples_per_frame: int = 0
        self._samples_per_frame_exact: float = 0.0
        self._sample_rate: int = DEFAULT_SAMPLE_RATE
        self._channels: int = 2

        self._running: bool = False
        self._paused: bool = False
        self._mutex: QMutex = QMutex()
        # AUD-15508: 输出失败只上报一次（启动失败/设备中途丢失共用）
        self._output_failure_reported: bool = False

        self._command_queue: queue.Queue = queue.Queue()
        self._last_sync_frame: int = 0
        self._next_audio_frame: int = 0
        self._first_write_logged: bool = False
        self._write_blocked_logged: bool = False

        # Mixer per-track 电平：由 prefill/direct-mix 路径写入最新峰值，
        # 主循环按节流频率 emit 出去（供 AudioMixerPanel 各轨电平表）。
        self._latest_per_track_peaks: Dict[str, tuple] = {}
        self._last_track_level_emit_ms: float = 0.0

        # 小数帧累积器，消除 int() 截断导致的累积漂移
        self._fractional_frame: float = 0.0
        self._sample_chunk_fraction: float = 0.0
        # 实时性隔离：播放期间收紧的 GIL switch interval 原值（退出时恢复）
        self._switch_interval_saved: Optional[float] = None

        # Final-5 (FINAL-P0-06): AudioRingBuffer 接入 QAudioSink 输出路径。
        # 解码+混音结果先写入 ring，输出阶段从 ring 读，解耦"按需 seek 解码"
        # 与"QAudioSink 反压"。seek 时 ring.clear()+apply_fade_in() 避免残留。
        # ring 容量约 0.5s（48kHz stereo），既能缓冲抖动又不引入明显延迟。
        from src.features.edit.core.realtime_playback.audio_ring_buffer import (
            AudioRingBuffer,
        )
        self._ring = AudioRingBuffer(channels=2, capacity=24000)
        # 预填充目标水位（samples）：低于此值时主动解码补 ring。
        self._ring_prefill_target: int = 9600  # ~200ms@48kHz
        # 上一次输出的 audio frame，用于 AVSyncController 计算 drift。
        self._last_output_audio_frame: int = -1
        # Final-5: seek 后首次 read 时对 ring 内容 apply_fade_in，避免爆音。
        self._pending_fade_in: bool = False
        # Independent write cursor for ring prefill/preroll.  The output cursor
        # (_next_audio_frame) advances only after samples are actually written
        # to the device; prefilled ring data must still move forward in time.
        self._ring_write_frame_float: float = 0.0
        # 整数帧写游标：每次 mix_samples 后严格 +1。不能用
        # int(_ring_write_frame_float) 代替——非整数帧率（29.97）下浮点游标
        # 累积误差会使 floor 滞后一帧，导致同一帧被重复混音/解码器 seek 错位。
        self._ring_write_frame_int: int = 0
        # prefill 每次混音样本数的分数累积器（与输出端 _sample_chunk_fraction
        # 独立）：非整数帧率下交替产出 N/N+1 样本，且不再把 n clip 到 needed，
        # 避免尾部小 n 对同一整数帧再次 mix_samples 触发解码器 seek 丢 remainder。
        self._prefill_chunk_fraction: float = 0.0
        self._prerolled_frame: Optional[int] = None
        self._prerolled_fps: float = 0.0

    def _enqueue_command(self, command: Tuple[str, Any]) -> None:
        """Queue a control command, coalescing stale positioning requests."""
        if command[0] not in {"seek", "preroll"}:
            self._command_queue.put(command)
            return

        retained: list[Tuple[str, Any]] = []
        try:
            while True:
                queued = self._command_queue.get_nowait()
                if queued[0] not in {"seek", "preroll"}:
                    retained.append(queued)
        except queue.Empty:
            pass

        for queued in retained:
            self._command_queue.put(queued)
        self._command_queue.put(command)

    def setup(
        self,
        clock: AudioClock,
        mixer: AudioMixer,
        output: Optional[AudioOutput],
        fps: float,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = 2,
        start_frame: int = 0,
        start_paused: bool = False,
    ) -> None:
        """
        设置播放组件。

        Args:
            clock: 音频时钟
            mixer: 音频混合器
            output: 音频输出
            fps: 时间线帧率
        """
        self._clock = clock
        self._mixer = mixer
        self._output = output
        self._fps = fps
        self._samples_per_frame_exact = clock.sample_rate / fps
        self._samples_per_frame = max(1, int(self._samples_per_frame_exact))
        self._sample_rate = sample_rate
        self._channels = channels
        self._next_audio_frame = max(0, int(start_frame))
        self._last_sync_frame = self._next_audio_frame
        self._ring_write_frame_float = float(self._next_audio_frame)
        self._ring_write_frame_int = int(self._next_audio_frame)
        self._paused = bool(start_paused)
        self._fractional_frame = 0.0  # 重置小数累积器
        self._sample_chunk_fraction = 0.0
        self._prefill_chunk_fraction = 0.0

    def _apply_realtime_isolation(self) -> None:
        """A/V 同压下的实时性隔离（OS 调度 + GIL 车队缓解）。

        组合播放（视频解码/合成 + 多轨音频）时主线程与渲染线程的 Python
        段会形成 GIL 车队，音频输出线程按默认优先级/切换间隔拿不到足够
        的调度与 GIL 时隙，表现为音频时钟 <0.9x 实时（纯音频场景无此问题）。
        两层缓解：
        1. OS 层：TimeCriticalPriority（Qt 在 Apple 平台映射到最高 QoS 类），
           主线程饱和时本线程仍能被及时调度；
        2. GIL 层：播放期间收紧 sys.setswitchinterval（5ms→1ms），缩短等待
           线程拿到 GIL 的最坏时延；线程退出时恢复原值。

        可用 TOPOS_AUDIO_REALTIME_ISOLATION=0 关闭（A/B 消融用），
        TOPOS_AUDIO_SWITCH_INTERVAL_MS 覆盖收紧值（毫秒）。
        """
        import os
        if os.environ.get("TOPOS_AUDIO_REALTIME_ISOLATION", "1") == "0":
            return
        try:
            self.setPriority(QThread.ThreadPriority.TimeCriticalPriority)
        except Exception:
            pass
        try:
            prev = sys.getswitchinterval()
            tight_ms = float(os.environ.get("TOPOS_AUDIO_SWITCH_INTERVAL_MS", "1.0"))
            tight = max(0.0001, tight_ms / 1000.0)
            if tight < prev:
                sys.setswitchinterval(tight)
                self._switch_interval_saved = prev
        except Exception:
            pass

    def _restore_realtime_isolation(self) -> None:
        """恢复播放期间收紧的 GIL switch interval（幂等）。"""
        if self._switch_interval_saved is not None:
            try:
                sys.setswitchinterval(self._switch_interval_saved)
            except Exception:
                pass
            self._switch_interval_saved = None

    def run(self) -> None:
        """
        线程主循环。

        解码、混合、输出音频。
        """
        self._running = True
        logger.debug("AudioPlaybackThread started")
        self._apply_realtime_isolation()

        try:
            if self._output is None:
                self._output = AudioOutput(sample_rate=self._sample_rate, channels=self._channels)

            if not self._output.start():
                playback_trace("audio_thread.error", reason="output_start_failed")
                # AUD-15508: manager 在 play() 时已 emit state_changed("playing")。
                # 旧代码只置 _running=False 静默退出：时钟停走、frame_sync 停发、
                # UI 仍显示"播放中"且无任何信号。上报 manager 统一 stop() +
                # state_changed("stopped")，UI 恢复一致。
                self._output_failure_reported = True
                self.output_failed.emit("output_start_failed")
                self._running = False

            # 检测设备格式回退后的采样率变化，确保整条链路采样率一致
            if self._running and self._output is not None:
                device_rate = self._output._sample_rate
                device_channels = self._output._channels
                if device_rate != self._sample_rate or device_channels != self._channels:
                    logger.warning(
                        f"Audio device format fallback: rate {self._sample_rate}→{device_rate}, "
                        f"channels {self._channels}→{device_channels}. Syncing pipeline."
                    )
                    self._sample_rate = device_rate
                    self._channels = device_channels
                    if self._clock:
                        self._clock.set_sample_rate(device_rate)
                    self._samples_per_frame_exact = device_rate / self._fps
                    self._samples_per_frame = max(1, int(self._samples_per_frame_exact))
                    # AUD-15509: 只同步时钟/帧率不够——解码器在 add_track 时已按
                    # 旧 _output_sample_rate 重采样到旧率，AudioMixer 的 EQ/FX
                    # 系数也仍按旧率计算：48k 内容被 44.1k 设备消费整体 ~8.6%
                    # 减速降调，EQ 中心频率整体偏移。同步 mixer 系数、全部解码器
                    # 的重采样目标率，并按新声道数重建 ring（固定 2 声道的 ring
                    # 写单声道数据依赖广播语义，读回声道数与设备不一致）。
                    if self._mixer is not None:
                        self._mixer.set_sample_rate(device_rate)
                        self._mixer.set_decoders_output_rate(device_rate)
                    if self._ring is not None and self._ring.channels != max(1, int(device_channels)):
                        from src.features.edit.core.realtime_playback.audio_ring_buffer import (
                            AudioRingBuffer,
                        )
                        self._ring = AudioRingBuffer(
                            channels=max(1, int(device_channels)),
                            capacity=self._ring.capacity,
                        )
                    playback_trace(
                        "audio_thread.format_fallback_sync",
                        new_rate=device_rate,
                        new_channels=device_channels,
                        samples_per_frame=self._samples_per_frame,
                    )

            if self._running and self._paused and self._output is not None:
                self._output.suspend()

            while self._running:
                # 处理命令
                try:
                    cmd = self._command_queue.get(timeout=0.01)
                    self._process_command(cmd)
                except queue.Empty:
                    pass

                # 如果暂停，等待
                if self._paused:
                    self.msleep(10)
                    continue

                # 播放音频
                if self._clock and self._mixer and self._output:
                    if self._output.is_playing:
                        # AUD-15510: _play_cycle（含直混兜底 mix_samples）外层兜底
                        # 保护——mix/写路径的未预期异常若冲出 run() 会杀死整个
                        # 播放线程且状态残留为 playing。记录并跳过本 chunk，
                        # 线程继续服务后续周期（与 _prefill_ring 的保护同级）。
                        try:
                            self._play_cycle()
                        except Exception as e:
                            logger.error(
                                f"_play_cycle failed, skipping chunk: {e}",
                                exc_info=True,
                            )
                            self.msleep(5)
                    elif self._clock.is_playing:
                        # AUD-15508: 设备中途丢失（write_samples/bytes_free 捕获
                        # RuntimeError 后置 _is_playing=False）。旧代码让线程静默
                        # 空转：时钟停走、frame_sync 停发、UI 仍显示"播放中"。
                        # 上报一次并退出循环，manager 统一恢复状态。
                        if not self._output_failure_reported:
                            self._output_failure_reported = True
                            playback_trace("audio_thread.error", reason="output_device_lost")
                            self.output_failed.emit("output_device_lost")
                        self._running = False

            if self._output:
                self._output.stop()
        finally:
            self._restore_realtime_isolation()
            logger.debug("AudioPlaybackThread stopped")

    def _process_command(self, cmd: Tuple[str, Any]) -> None:
        """
        处理命令。

        Args:
            cmd: (command_type, data)
        """
        cmd_type, data = cmd

        if cmd_type == "stop":
            self._running = False
        elif cmd_type == "pause":
            self._paused = True
            if self._output:
                self._output.suspend()
        elif cmd_type == "resume":
            self._paused = False
            if self._output:
                self._output.resume()
        elif cmd_type == "seek":
            # Seek 命令：data = (frame, fps)
            frame, fps = data
            self._seek_audio_thread_position(int(frame), float(fps))
        elif cmd_type == "preroll":
            frame, fps, target_samples = data
            self._paused = True
            self._seek_audio_thread_position(
                int(frame),
                float(fps),
                keep_suspended=True,
            )
            playback_trace(
                "audio_thread.preroll_start",
                frame=frame,
                fps=fps,
                target_samples=target_samples,
            )
            filled = self._prefill_ring_to(int(target_samples))
            self._prerolled_frame = int(frame)
            self._prerolled_fps = float(fps)
            playback_trace(
                "audio_thread.preroll_done",
                frame=frame,
                filled=filled,
                target_samples=target_samples,
            )

    def _seek_audio_thread_position(
        self,
        frame: int,
        fps: float,
        *,
        keep_suspended: Optional[bool] = None,
    ) -> None:
        """Reset thread-side audio cursors and clear queued output/ring samples."""
        self._fps = fps
        if self._clock:
            self._samples_per_frame_exact = self._clock.sample_rate / fps
            self._samples_per_frame = max(1, int(self._samples_per_frame_exact))
        self._next_audio_frame = max(0, int(frame))
        self._last_sync_frame = self._next_audio_frame
        self._ring_write_frame_float = float(self._next_audio_frame)
        self._ring_write_frame_int = int(self._next_audio_frame)
        self._prerolled_frame = None
        self._prerolled_fps = 0.0
        self._write_blocked_logged = False
        self._fractional_frame = 0.0
        self._sample_chunk_fraction = 0.0
        self._prefill_chunk_fraction = 0.0
        if self._output:
            self._output.reset_buffer(
                keep_suspended=self._paused if keep_suspended is None else keep_suspended
            )
        try:
            self._ring.clear()
        except Exception:
            pass
        # 有状态 DSP（混响尾/延迟线/压缩包络/EQ zi）对应跳转前的信号位置，
        # 保留会在新位置产生错位瞬态——seek 时统一复位。
        if self._mixer is not None:
            try:
                self._mixer.reset_dsp_states()
            except Exception:
                logger.debug("Failed to reset DSP states on seek", exc_info=True)
        self._pending_fade_in = True

    def _next_chunk_samples(self) -> int:
        """Return the next output chunk sample count for the current FPS."""
        exact = self._samples_per_frame_exact or float(self._samples_per_frame)
        if exact <= 0:
            return max(1, self._samples_per_frame)
        self._sample_chunk_fraction += exact
        chunk_samples = int(self._sample_chunk_fraction)
        self._sample_chunk_fraction -= chunk_samples
        return max(1, chunk_samples)

    def _play_cycle(self) -> None:
        """
        一个播放周期（Final-5: 经 AudioRingBuffer 解耦）。

        流程：
        1. 若 ring 水位低于 prefill 目标，主动 mix_samples 写入 ring
           （解码+混音仍由 mixer 按需 seek，但写入 ring 后输出侧不再
           直接承担解码延迟）。
        2. 从 ring 读 chunk_samples，write 到 QAudioSink（批量：单次
           循环最多写 3 个 chunk）。
        3. seek 后首次 read 对 ring 内容 apply_fade_in 防爆音。
        4. bytes_free 反压仍以 QAudioSink 为准（双层缓冲水位对齐）。

        批量写的原因：线程主循环每轮先 queue.get(timeout=0.01) 轮询再写
        一个 40ms chunk。GIL 争用严重时（视频+8 音轨实测）线程重新获取
        GIL 的唤醒延迟被拉到数十毫秒，循环跌到 ~20 次/s，音频时钟掉到
        0.8x 实时。一次唤醒写最多 3 个 chunk，让每次 GIL 获取做更多
        工作，恢复 1.0x 节奏（QAudioSink 的 bytes_free 仍限制水位上限）。
        """
        if self._samples_per_frame <= 0:
            self.msleep(5)
            return

        # Final-5: 预填充 ring（水位不足时主动解码写入）。
        # 用 mixer 当前帧推进，避免重复解码同一帧。
        try:
            self._prefill_ring()
        except Exception as e:
            logger.debug(f"_prefill_ring failed: {e}")

        for _ in range(3):
            if not self._write_next_output_chunk():
                break

    def _write_next_output_chunk(self) -> bool:
        """写一个输出 chunk；被反压/写失败时返回 False。"""
        current_frame = self._next_audio_frame
        chunk_fraction_before = self._sample_chunk_fraction
        chunk_samples = self._next_chunk_samples()
        chunk_bytes = chunk_samples * self._output.bytes_per_frame()
        bytes_free = self._output.bytes_free()
        if bytes_free < chunk_bytes:
            self._sample_chunk_fraction = chunk_fraction_before
            # AUD-15511: 日志只去重一次；退避睡眠必须在每次反压轮次执行——
            # 旧实现把 msleep(5) 放进"仅首次"守卫内，首个阻塞周期之后的连续
            # 阻塞不再退避，靠主循环 queue.get(timeout=0.01) 兜底，一旦循环
            # 结构变化就退化为忙等烧 CPU。
            if not self._write_blocked_logged:
                self._write_blocked_logged = True
                playback_trace(
                    "audio_thread.output_wait",
                    frame=current_frame,
                    bytes_free=bytes_free,
                    chunk_bytes=chunk_bytes,
                )
            self.msleep(5)
            return False
        self._write_blocked_logged = False

        # Final-5: 从 ring 读取输出样本（而不是直接调 mix_samples）。
        # ring 水位不足时退化为直接 mix（兼容初始化/极快 seek 场景）。
        used_direct_mix = False
        ring_filled = self._ring.filled
        if ring_filled < chunk_samples:
            # ring 水位不足（underrun 边界）：直接 mix 兜底，避免静音断音。
            # AUD-15510: 与 _prefill_ring 同级保护——mix_samples 内任何未预期
            # 异常（畸形 sends 的 float() TypeError、DSP 插件抛错）冲出 run()
            # 会杀死整个播放线程且状态残留为 playing。捕获、记录并跳过本 chunk。
            try:
                samples, _direct_per_track = self._mixer.mix_samples(
                    current_frame,
                    self._fps,
                    chunk_samples,
                    self._output._channels,
                )
            except Exception as e:
                logger.error(f"direct-mix fallback failed, skipping chunk: {e}", exc_info=True)
                self._sample_chunk_fraction = chunk_fraction_before
                self.msleep(5)
                return False
            # 直接 mix 路径可获得 per-track 峰值，更新最新缓存（供主循环 emit）
            if _direct_per_track:
                self._latest_per_track_peaks = _direct_per_track
            # ring 中的残留旧样本已被本分支跳过，若不清空会在后续周期被
            # 先于新数据消费（重复播放旧段）。清空后把直混结果写入 ring，统一
            # 经 peek→write→commit 消费：写失败（written==0）时样本留在 ring，
            # 下一轮重试而非永久丢失（AUD-15512：直混分支同样先 clear 再写，
            # 旧实现失败即丢整块）。
            try:
                self._ring.clear()
            except Exception:
                pass
            if self._pending_fade_in and samples is not None and samples.size > 0:
                # 对直接 mix 的结果做简单线性 fade-in（64 样本）。
                try:
                    import numpy as _np
                    fade_n = min(64, samples.shape[-1])
                    fade = _np.linspace(0.0, 1.0, fade_n, dtype=_np.float32)
                    samples[..., :fade_n] *= fade
                except Exception:
                    pass
                self._pending_fade_in = False
            if samples is not None and samples.size > 0:
                self._ring.write(samples)
            used_direct_mix = True

        # Apply fade before consuming; applying it after read only fades
        # samples that will be played later and leaves the first block raw.
        if self._pending_fade_in:
            try:
                self._ring.apply_fade_in(fade_samples=min(64, chunk_samples))
            except Exception:
                pass
            self._pending_fade_in = False

        # AUD-15512: 先 peek 拷贝、写设备成功后再按实际写入量提交消费。
        # 旧实现先 ring.read(chunk_samples) 消费再写：write_samples 返回 0
        # （设备瞬断、缓冲意外满）时样本已从 ring 拿走且只回滚了
        # _sample_chunk_fraction——这块音频永久丢失（可闻跳变/卡顿）；部分
        # 写入的尾差同样不回补。peek 不消费，失败时数据原样留在 ring 头部。
        samples = self._ring.peek(chunk_samples)
        if samples is None or samples.size == 0:
            self._sample_chunk_fraction = chunk_fraction_before
            return False

        # 输出
        written = self._output.write_samples(samples)
        if written > 0:
            # AUD-15512: 写入成功后才提交 ring 消费，且只消费实际写入的量——
            # 部分写入的余量留在 ring 头部由下一轮 peek 补写，不再丢失尾差。
            self._ring.read(written)
        if written > 0 and not self._first_write_logged:
            self._first_write_logged = True
            playback_trace(
                "audio_thread.first_write",
                frame=current_frame,
                samples=written,
                peak=float(np.max(np.abs(samples))) if samples is not None and samples.size else 0.0,
            )
        elif written == 0:
            self._sample_chunk_fraction = chunk_fraction_before
            if not self._write_blocked_logged:
                self._write_blocked_logged = True
                playback_trace(
                    "audio_thread.write_zero",
                    frame=current_frame,
                    tracks=len(getattr(self._mixer, "_decoders", {}) or {}),
                    bytes_free=bytes_free,
                    chunk_bytes=chunk_bytes,
                    sample_shape=getattr(samples, "shape", None),
                )
            self.msleep(5)
            return False

        # 计算电平并发射
        # A4: master 电平 emit 与 per-track 共用同一节流时间戳（30ms ≈ 33Hz），
        # 避免每帧都 emit（播放线程 ~24-60Hz，master 表不需要更高刷新率）。
        if samples is not None and samples.size > 0:
            try:
                import numpy as _np
                now_ms = time.perf_counter() * 1000.0
                if now_ms - self._last_track_level_emit_ms >= 30.0:
                    if samples.ndim == 2 and samples.shape[0] >= 2:
                        left_peak = float(_np.max(_np.abs(samples[0])))
                        right_peak = float(_np.max(_np.abs(samples[1])))
                    elif samples.ndim == 2:
                        left_peak = right_peak = float(_np.max(_np.abs(samples[0])))
                    else:
                        left_peak = right_peak = float(_np.max(_np.abs(samples)))

                    # A4: 向量化 dB 转换——左右峰值打包成数组一次 log10，替代 2 次 Python log10
                    peaks = _np.array([left_peak, right_peak], dtype=_np.float64)
                    # 避免 log10(0)：用 clip 到 1e-10
                    peaks_db = 20.0 * _np.log10(_np.maximum(peaks, 1e-10))
                    peaks_db = _np.maximum(peaks_db, -100.0)
                    self.audio_level.emit([float(peaks_db[0]), float(peaks_db[1])])
            except Exception:
                pass

        # 发射 per-track 电平（节流 ~33Hz，避免 UI 过载）
        try:
            now_ms = time.perf_counter() * 1000.0
            if (self._latest_per_track_peaks
                    and now_ms - self._last_track_level_emit_ms >= 30.0):
                self._last_track_level_emit_ms = now_ms
                track_levels: Dict[str, dict] = {}
                # A4: 批量向量化 dB 转换——先把所有轨的 peak/rms 线性值收集到数组，
                # 一次 log10 完成全部转换，替代每轨 4 次 Python 层 log10 调用。
                track_ids_ordered = list(self._latest_per_track_peaks.keys())
                if track_ids_ordered:
                    lin_arr = np.empty((len(track_ids_ordered), 4), dtype=np.float64)
                    lufs_arr = np.empty((len(track_ids_ordered),), dtype=np.float64)
                    for i, tid in enumerate(track_ids_ordered):
                        lvl = self._latest_per_track_peaks[tid]
                        lin_arr[i, 0] = lvl.peak_l
                        lin_arr[i, 1] = lvl.peak_r
                        lin_arr[i, 2] = lvl.rms_l
                        lin_arr[i, 3] = lvl.rms_r
                        lufs_arr[i] = lvl.lufs
                    # 一次向量化 log10（替代 len×4 次 Python log10）
                    db_arr = 20.0 * np.log10(np.maximum(lin_arr, 1e-10))
                    db_arr = np.maximum(db_arr, -100.0)
                    for i, tid in enumerate(track_ids_ordered):
                        track_levels[tid] = {
                            "peak": [float(db_arr[i, 0]), float(db_arr[i, 1])],
                            "rms": [float(db_arr[i, 2]), float(db_arr[i, 3])],
                            # W11: LUFS（立体声合并值），直接是 dB，无需转换
                            "lufs": [float(lufs_arr[i]), float(lufs_arr[i])],
                        }
                # 清空缓存：电平表靠下次填充刷新，无信号时由 UI 侧衰减归零
                self._latest_per_track_peaks = {}
                if track_levels:
                    self.audio_track_levels.emit(track_levels)
        except Exception:
            pass

        # 更新时钟
        if written > 0:
            self._clock.add_samples(written)
            if used_direct_mix:
                self._advance_ring_write_cursor_from_frame(current_frame, written)
            # 使用小数累积器计算帧推进，避免 int(round()) 截断导致的累积漂移
            exact_frames = written / max(1.0, self._samples_per_frame_exact)
            self._fractional_frame += exact_frames
            frames_to_advance = int(self._fractional_frame)
            self._fractional_frame -= frames_to_advance
            if frames_to_advance > 0:
                self._next_audio_frame += frames_to_advance

        # 同步帧
        sync_frame = self._next_audio_frame
        if sync_frame != self._last_sync_frame:
            self._last_sync_frame = sync_frame
            self.frame_sync.emit(sync_frame)
        return True

    def _prefill_ring(self) -> None:
        """
        Final-5: ring 水位不足时主动解码+混音写入 ring。

        每次 prefill 用严格递增的整数帧游标推进 mixer，写入若干整帧样本到
        ring；ring 达到目标水位即返回。

        历史问题（已移除的“电平采样兜底”）：ring 满且电平缓存为空时额外
        调一次 mix_samples 只采电平——该调用会消耗有状态解码器（同一整数帧
        被二次混音/触发 seek 丢 remainder），是非整数帧率下音频错位的主要
        来源之一。稳态播放时 ring 每周期被输出消费，prefill 会持续运行并
        刷新 per-track 峰值，无需额外兜底。
        """
        if self._mixer is None or self._output is None:
            return
        self._prefill_ring_to(self._ring_prefill_target)

    def _prefill_ring_to(self, target: int) -> int:
        """Fill the audio ring up to target samples using a continuous write cursor.

        关键约束（断裂修复）：
        1. 混音帧号用显式整数游标 ``_ring_write_frame_int``，每次 mix 后 +1；
           不用 int(浮点游标)（非整数帧率下会滞后一帧导致同帧重复混音）。
        2. 每次混音样本数用 ``_prefill_chunk_fraction`` 分数累积器产出整帧
           对应的 N/N+1 样本，且 **不 clip 到 needed**：允许水位略超 target
           （ring 容量远大于 target），避免尾部极小 n 对同一帧再次 mix。
        """
        if self._mixer is None:
            return int(getattr(self._ring, "filled", 0))

        target = max(0, min(int(target), int(getattr(self._ring, "capacity", target))))
        # 防御性同步：整数游标不落后于浮点游标（外部只设浮点游标的旧路径兼容）。
        self._ring_write_frame_int = max(
            self._ring_write_frame_int,
            int(math.floor(self._ring_write_frame_float + 1e-9)),
        )
        exact = max(1.0, float(self._samples_per_frame_exact or self._samples_per_frame))
        # 安全守卫：单次 prefill 最多补到 target 所需帧数 + 少量余量。
        max_iterations = max(8, int(target / exact) + 8)
        iterations = 0

        while self._ring.filled < target:
            iterations += 1
            if iterations > max_iterations:
                break
            # 整帧样本数：分数累积器交替产出 N/N+1（非整数帧率下与输出端一致）。
            self._prefill_chunk_fraction += exact
            n = int(self._prefill_chunk_fraction)
            self._prefill_chunk_fraction -= n
            n = max(1, n)
            current_frame = int(self._ring_write_frame_int)
            samples, per_track = self._mixer.mix_samples(
                current_frame,
                self._fps,
                n,
                self._channels,
            )
            if samples is None or samples.size == 0:
                break
            # prefill 是稳态播放路径：缓存最新 per-track 峰值，供主循环节流 emit。
            if per_track:
                self._latest_per_track_peaks = per_track
            written = self._ring.write(samples)
            if written <= 0:
                break
            # 每次 mix_samples 消耗了一个整帧（无论实际产出多少样本）：
            # 整数游标 +1，浮点游标对齐到同一位置。
            self._ring_write_frame_int = current_frame + 1
            self._ring_write_frame_float = float(self._ring_write_frame_int)

        return int(self._ring.filled)

    def _advance_ring_write_cursor(self, samples: int) -> None:
        exact = max(1.0, float(self._samples_per_frame_exact or self._samples_per_frame))
        self._ring_write_frame_float += max(0, int(samples)) / exact
        # 整数游标不落后于浮点游标（向上取整：已消费的帧不再重复混音）。
        self._ring_write_frame_int = max(
            self._ring_write_frame_int,
            int(math.ceil(self._ring_write_frame_float - 1e-9)),
        )

    def _advance_ring_write_cursor_from_frame(self, frame: int, samples: int) -> None:
        exact = max(1.0, float(self._samples_per_frame_exact or self._samples_per_frame))
        consumed_end = float(frame) + max(0, int(samples)) / exact
        self._ring_write_frame_float = max(float(self._ring_write_frame_float), consumed_end)
        self._ring_write_frame_int = max(
            self._ring_write_frame_int,
            int(math.ceil(consumed_end - 1e-9)),
        )

    def stop(self) -> None:
        """停止线程。"""
        self._enqueue_command(("stop", None))
        self._running = False

    def pause(self) -> None:
        """暂停线程。"""
        self._enqueue_command(("pause", None))

    def resume(self) -> None:
        """恢复线程。"""
        self._enqueue_command(("resume", None))

    def seek(self, frame: int, fps: float) -> None:
        """
        定位线程。

        Args:
            frame: 目标帧
            fps: 帧率
        """
        self._enqueue_command(("seek", (frame, fps)))

    def preroll(self, frame: int, fps: float, target_samples: int) -> None:
        """Prepare ring-buffered audio for a paused playback start."""
        self._enqueue_command(("preroll", (frame, fps, target_samples)))


# =============================================================================
# AudioScrubPreviewWorker
# =============================================================================

class AudioScrubPreviewWorker(QThread):
    """
    Background mixer for audible scrub preview.

    It never touches QAudioSink and never advances AudioClock.  The worker only
    mixes a short PCM block for the latest requested frame and emits it back to
    the manager thread for best-effort output.
    """

    samples_ready = pyqtSignal(int, int, object)  # frame, generation, np.ndarray

    def __init__(
        self,
        mixer: AudioMixer,
        parent: Optional[QObject] = None,
        is_playback_active: Optional[Callable[[], bool]] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("AudioScrubPreviewWorker")
        self._mixer = mixer
        self._queue: queue.Queue = queue.Queue(maxsize=1)
        self._stop_flag = False
        self._stop_event = threading.Event()
        self._latest_generation = 0
        self._latest_lock = threading.Lock()
        # AUD-15513: 播放启动与排队预览之间的竞态守卫。worker 在破坏性混音
        # （reset_dsp_states + 解码器 seek）前查询该回调；时钟 is_playing
        # 由 QMutex 保护，可从 worker 线程安全轮询。
        self._is_playback_active = is_playback_active

    def cancel_pending(self) -> None:
        """使所有已排队、尚未混音的预览任务失效（AUD-15513）。

        播放启动（play/resume）时调用：递增内部 generation 使队内旧任务
        过期，并清空队列。已在混音中的任务由 ``_is_playback_active``
        检查兜底。
        """
        with self._latest_lock:
            self._latest_generation += 1
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

    def request_preview(
        self,
        frame: int,
        fps: float,
        num_samples: int,
        output_channels: int,
        generation: int,
    ) -> None:
        """Replace any queued preview with the newest scrub target."""
        with self._latest_lock:
            self._latest_generation = int(generation)

        task = (
            int(frame),
            float(fps),
            max(1, int(num_samples)),
            max(1, int(output_channels)),
            int(generation),
        )
        dropped = 0
        try:
            while True:
                self._queue.get_nowait()
                dropped += 1
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(task)
        except queue.Full:
            dropped += 1
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            self._queue.put_nowait(task)

        playback_trace(
            "audio_scrub_worker.enqueue",
            frame=frame,
            generation=generation,
            dropped=dropped,
            samples=num_samples,
        )

    def stop(self) -> None:
        """Stop the worker and wake it if it is waiting for a preview task."""
        self._stop_flag = True
        self._stop_event.set()
        self.requestInterruption()
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass

    def run(self) -> None:
        while not self._stop_flag and not self._stop_event.is_set():
            try:
                task = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if task is None:
                continue

            frame, fps, num_samples, output_channels, generation = task
            with self._latest_lock:
                latest_generation = self._latest_generation
            if int(generation) != int(latest_generation):
                playback_trace(
                    "audio_scrub_worker.drop_stale_before_mix",
                    frame=frame,
                    generation=generation,
                    latest_generation=latest_generation,
                )
                continue

            # AUD-15513: 入队时未播放的预览可能在用户按下 Play 后才被混音。
            # 此处（reset_dsp_states/解码器 seek 之前）检查时钟态，播放已
            # 启动则丢弃任务，避免清掉播放中的混响尾并扰动解码器位置。
            if self._is_playback_active is not None and self._is_playback_active():
                playback_trace(
                    "audio_scrub_worker.drop_playback_active",
                    frame=frame,
                    generation=generation,
                )
                continue

            t0 = time.perf_counter()
            playback_trace(
                "audio_scrub_worker.mix_start",
                frame=frame,
                generation=generation,
                samples=num_samples,
            )
            try:
                # 每次预览都在任意位置渲染：复位有状态 DSP，避免上一预览的
                # 混响尾/包络/EQ 状态串到本预览（播放暂停期，无并发混音）。
                self._mixer.reset_dsp_states()
                samples, _scrub_per_track = self._mixer.mix_samples(
                    frame,
                    fps,
                    num_samples,
                    output_channels,
                )
                samples = self._apply_preview_fade(samples)
            except Exception as exc:
                playback_trace(
                    "audio_scrub_worker.mix_error",
                    frame=frame,
                    generation=generation,
                    error=exc,
                )
                continue

            with self._latest_lock:
                latest_generation = self._latest_generation
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            if int(generation) != int(latest_generation):
                playback_trace(
                    "audio_scrub_worker.drop_stale_after_mix",
                    frame=frame,
                    generation=generation,
                    latest_generation=latest_generation,
                    elapsed_ms=elapsed_ms,
                )
                continue

            playback_trace(
                "audio_scrub_worker.mix_done",
                frame=frame,
                generation=generation,
                elapsed_ms=elapsed_ms,
                sample_shape=getattr(samples, "shape", None),
            )
            self.samples_ready.emit(int(frame), int(generation), samples)

    def _apply_preview_fade(self, samples: np.ndarray) -> np.ndarray:
        if samples is None or samples.size == 0:
            return np.zeros((2, 0), dtype=np.float32)
        faded = np.array(samples, dtype=np.float32, copy=True)
        if faded.ndim == 1:
            faded = faded.reshape(1, -1)
        n = faded.shape[-1]
        if n <= 0:
            return faded
        fade_n = min(64, max(1, n // 8))
        fade_in = np.linspace(0.0, 1.0, fade_n, dtype=np.float32)
        fade_out = np.linspace(1.0, 0.0, fade_n, dtype=np.float32)
        faded[..., :fade_n] *= fade_in
        faded[..., -fade_n:] *= fade_out
        return faded


# =============================================================================
# AudioPlaybackManager
# =============================================================================

class AudioPlaybackManager(QObject):
    """
    音频播放管理器。

    整合时钟、解码器、混合器、输出，
    与 EditPlaybackController 协同工作。

    Signals:
        state_changed: 播放状态变化 (playing/paused/stopped)
        frame_changed: 当前帧变化（用于同步视频）
        track_state_changed: 轨道状态变化 (track_id)
        sync_error: 同步误差 (error_ms)
        end_reached: 播放到达终点

    Attributes:
        _clock: 音频时钟
        _mixer: 音频混合器
        _output: 音频输出
        _playback_thread: 播放线程
        _track_states: 轨道状态字典
        _video_controller: 关联的视频控制器
        _timeline_fps: 时间线帧率
        _timeline_duration: 时间线总帧数
        _loop_enabled: 是否循环播放
        _sync_mode: 同步模式
    """

    # Signals
    state_changed = pyqtSignal(str)         # playing/paused/stopped
    frame_changed = pyqtSignal(int)         # 当前帧变化
    track_state_changed = pyqtSignal(str)   # track_id
    sync_error = pyqtSignal(float)          # 同步误差 ms
    end_reached = pyqtSignal()              # 播放到达终点
    audio_level_changed = pyqtSignal(list)  # [left_db, right_db] 电平
    audio_track_levels_changed = pyqtSignal(dict)  # {track_id: [l_db, r_db]} per-track 电平

    def __init__(self, parent: Optional[QObject] = None) -> None:
        """
        初始化音频播放管理器。

        Args:
            parent: QObject 父对象
        """
        super().__init__(parent)

        # 组件
        self._clock: AudioClock = AudioClock()
        self._mixer: AudioMixer = AudioMixer()
        self._output: Optional[AudioOutput] = None
        self._playback_thread: Optional[AudioPlaybackThread] = None
        self._scrub_preview_worker: Optional[AudioScrubPreviewWorker] = None
        self._scrub_preview_output: Optional[AudioOutput] = None

        # 轨道状态
        self._track_states: Dict[str, AudioTrackState] = {}
        self._mutex: QMutex = QMutex()

        # 时间线参数
        self._timeline_fps: float = 24.0
        self._timeline_duration: int = 0
        self._sample_rate: int = DEFAULT_SAMPLE_RATE
        self._loop_enabled: bool = False

        # 视频控制器关联
        self._video_controller: Optional[Any] = None  # EditPlaybackController
        self._sync_mode: AudioSyncMode = AudioSyncMode.AUDIO_MASTER
        self._prepared_frame: Optional[int] = None
        self._prepared_fps: float = 0.0
        self._audio_preroll_ms: int = 120
        self._audio_scrub_preview_ms: int = 72
        self._scrub_preview_generation: int = 0

        logger.debug("AudioPlaybackManager initialized")

    # ==================== Configuration ====================

    def prewarm_output(self) -> None:
        """
        Keep for API compatibility.

        AudioOutput is now created inside AudioPlaybackThread so QAudioSink and
        its QIODevice stay on the same thread that writes samples.
        """
        logger.debug("AudioOutput pre-warm skipped; output is thread-owned")

    def set_sample_rate(self, sample_rate: int) -> None:
        """
        设置采样率。

        Args:
            sample_rate: 采样率（Hz）
        """
        if sample_rate <= 0:
            logger.warning(f"Invalid sample_rate: {sample_rate}")
            return

        self._sample_rate = sample_rate
        self._clock.set_sample_rate(sample_rate)

        if self._playback_thread is not None:
            logger.debug("Sample rate changed while playback thread exists; restart required")

        logger.debug(f"Sample rate set to {sample_rate}")

    def set_timeline_fps(self, fps: float) -> None:
        """
        设置时间线帧率。

        Args:
            fps: 帧率
        """
        if fps <= 0:
            logger.warning(f"Invalid fps: {fps}")
            return

        self._timeline_fps = fps
        logger.debug(f"Timeline FPS set to {fps}")

    def set_timeline_duration(self, duration: int) -> None:
        """
        设置时间线总帧数。

        Args:
            duration: 总帧数
        """
        self._timeline_duration = max(0, duration)
        logger.debug(f"Timeline duration set to {duration}")

    def set_loop(self, enabled: bool) -> None:
        """
        设置循环播放。

        Args:
            enabled: 是否循环
        """
        self._loop_enabled = enabled
        logger.debug(f"Loop mode: {enabled}")

    def set_lufs_metering(self, enabled: bool) -> None:
        """开/关混音器 LUFS 计算（由 AudioMixerPanel 表模式驱动）。

        无 LUFS 模式的表时关闭可省掉每 chunk 每轨的 K-weighting
        （占混音热路径近一半耗时），详见 AudioMixer.set_lufs_metering。
        """
        if self._mixer is not None:
            self._mixer.set_lufs_metering(enabled)

    def set_sync_mode(self, mode: AudioSyncMode) -> None:
        """
        设置同步模式。

        Args:
            mode: AudioSyncMode
        """
        self._sync_mode = mode
        logger.debug(f"Sync mode set to {mode.value}")

    def set_video_controller(self, controller: Any) -> None:
        """
        设置关联的视频控制器。

        Args:
            controller: EditPlaybackController 实例
        """
        self._video_controller = controller

        # 连接信号
        if controller:
            # 当视频帧变化时，音频可以同步
            if hasattr(controller, 'frame_changed'):
                controller.frame_changed.connect(self._on_video_frame_changed)

            logger.debug("Video controller linked")

    def set_master_volume(self, volume: float) -> None:
        """设置 Master 总线音量（线性增益）。

        转发到 AudioMixer 在 sample 级应用（mix_samples 末端、软限幅之前），
        避免依赖线程私有的 QAudioSink（其 _output 在本 manager 中恒为 None，
        且 QAudioSink.setVolume 上限 1.0 无法支持 >0dB 正增益）。

        Args:
            volume: 线性增益（>= 0.0；支持 >1.0 正增益，配合软限幅保护削波）
        """
        if self._mixer is not None:
            self._mixer.set_master_volume(volume)
        # 兼容：若未来恢复线程外 AudioOutput，顺带调（当前 _output 恒 None，跳过）
        if self._output:
            self._output.set_volume(max(0.0, min(1.0, volume)))

    def set_master_muted(self, muted: bool) -> None:
        """设置 Master 总线静音。转发到 AudioMixer。"""
        if self._mixer is not None:
            self._mixer.set_master_muted(muted)

    # ==================== Track Management ====================

    def add_track(
        self,
        track_id: str,
        asset_path: Path,
        initial_state: Optional[AudioTrackState] = None,
        source_start: int = 0,
        source_fps: float = 0.0,
        timeline_start: int = 0,
        timeline_duration: int = 0,
        replace_existing: bool = True,
        clip_id: Optional[str] = None,
        speed: float = 1.0,
        fade_in_frames: int = 0,
        fade_out_frames: int = 0,
        gain_db: float = 0.0,
        source_channel_map: Optional[tuple] = None,
        preserve_pitch: bool = False,
        fade_in_samples: Optional[int] = None,
        fade_out_samples: Optional[int] = None,
        audio_sample_offset: int = 0,
        source_sample_offset: int = 0,
    ) -> bool:
        """
        添加音频轨道。

        Args:
            track_id: 轨道 ID
            asset_path: 素材路径
            initial_state: 初始状态（可选）
            source_start: 素材在源文件中的起始帧（0 = 从头开始）
            source_fps: 源视频的帧率（0 = 与 timeline_fps 相同）
            timeline_start: clip 在时间线上的起始帧
            timeline_duration: clip 的持续帧数（0 = 未知/不限）
            replace_existing: True 替换轨道已有 clip；False 追加同轨道 clip
            clip_id: 该 decoder 对应的 clip ID（P-clip-FX，用于 per-clip
                效果链反查）。None 时该 decoder 不应用 clip 级效果。
            speed: RMA-03 clip 播放速度（默认 1.0）。
            preserve_pitch: F3.2 保调变速（流式 WSOLA；与离线导出同参数。
                倒放/速度曲线等不支持组合由调用方过滤后置 False，本层只认
                常量速度）。
            fade_in_frames/fade_out_frames: RMA-03 clip 淡入/淡出帧数（默认 0=无）。
            source_channel_map: F2.1 素材级源声道映射
                （((src, dst), ...) 对，None=直通）。来自 EditProject
                的 Source Patch 覆盖，mix_samples 解码后据此选道。

        Returns:
            是否成功添加
        """
        # 创建解码器
        decoder = AudioDecoder()
        if not decoder.open(
            asset_path,
            self._timeline_fps,
            source_start=source_start,
            source_fps=source_fps,
            output_sample_rate=self._sample_rate,
        ):
            logger.warning(f"Failed to open audio asset: {asset_path}")
            return False

        # 设置 clip 在时间线上的起始帧
        decoder._timeline_start = timeline_start
        decoder._timeline_duration = max(0, int(timeline_duration))
        # P-clip-FX: 记录 decoder ↔ clip 映射，供 mix_samples 反查 clip.effects
        decoder._clip_id = clip_id
        # F2.1: 素材级源声道映射（Source Patching），mix_samples 解码后选道
        decoder._source_channel_map = source_channel_map
        # RMA-03: clip 速度与淡入淡出（实时路径此前忽略，与离线导出漂移）
        decoder.set_speed(speed)
        # F3.2: 保调变速（流式 WSOLA）——离线请求由调用方用
        # preserve_pitch_request 同口径解析（倒放/曲线回退变调并留痕）。
        decoder.set_preserve_pitch(bool(preserve_pitch))
        # F3.3: 采样域淡化/子帧偏移（预览/成片同语义）
        if fade_in_samples or fade_out_samples \
                or audio_sample_offset or source_sample_offset:
            decoder.set_fade(
                timeline_start, timeline_duration,
                fade_in_frames=fade_in_frames,
                fade_out_frames=fade_out_frames,
                fps=self._timeline_fps,
                fade_in_samples=fade_in_samples,
                fade_out_samples=fade_out_samples,
                audio_sample_offset=audio_sample_offset,
                source_sample_offset=source_sample_offset,
            )
        decoder.set_fade(timeline_start, timeline_duration,
                         fade_in_frames=fade_in_frames,
                         fade_out_frames=fade_out_frames,
                         fps=self._timeline_fps)
        # C3: 片段级增益
        decoder.set_gain_db(gain_db)

        # 创建状态
        state = initial_state or AudioTrackState(track_id=track_id)

        # 添加到混合器
        self._mixer.add_track(
            track_id,
            decoder,
            state,
            replace_existing=replace_existing,
        )

        # 保存状态
        with QMutexLocker(self._mutex):
            self._track_states[track_id] = state

        # 发射信号
        self.track_state_changed.emit(track_id)

        logger.debug(f"Audio track added: {track_id}")
        return True

    def remove_track(self, track_id: str) -> None:
        """
        移除音频轨道。

        Args:
            track_id: 轨道 ID
        """
        self._mixer.remove_track(track_id)

        with QMutexLocker(self._mutex):
            if track_id in self._track_states:
                del self._track_states[track_id]

        self.track_state_changed.emit(track_id)
        logger.debug(f"Audio track removed: {track_id}")

    def register_bus_track(self, track_id: str,
                           state: Optional[AudioTrackState] = None) -> None:
        """AM-02: 注册 Bus 轨（无 decoder）到引擎。

        Bus 轨即使没有 clips 也必须进入引擎的 track state 表，否则其接收的
        路由输入会被丢弃。见 AudioMixer.register_bus_track。
        """
        bus_state = state if state is not None else AudioTrackState(track_id=track_id)
        self._mixer.register_bus_track(track_id, bus_state)
        with QMutexLocker(self._mutex):
            self._track_states[track_id] = bus_state
        self.track_state_changed.emit(track_id)
        logger.debug(f"Audio bus track registered: {track_id}")

    def unregister_bus_track(self, track_id: str) -> None:
        """AM-02: 注销 Bus 轨。"""
        self._mixer.unregister_bus_track(track_id)
        with QMutexLocker(self._mutex):
            if track_id in self._track_states:
                del self._track_states[track_id]
        self.track_state_changed.emit(track_id)
        logger.debug(f"Audio bus track unregistered: {track_id}")

    def quiesce_for_timeline_rebuild(self) -> None:
        """
        Stop thread-owned audio output before mutating the mixer graph.

        Timeline edits such as split/move can replace decoder sets while the
        playback thread is still pre-filling or writing to QAudioSink.  Stop the
        worker first so decoder close/reopen and QIODevice teardown cannot race
        with output writes.  Unlike stop(), this does not emit frame_changed(0).
        """
        self._clock.stop()

        if self._playback_thread:
            self._playback_thread.stop()
            self._playback_thread.wait(1000)
            if self._playback_thread.isRunning():
                logger.warning("Audio playback thread did not stop before timeline rebuild")
                self._playback_thread.wait()
            self._playback_thread = None

        self._prepared_frame = None
        self._prepared_fps = 0.0
        self._stop_scrub_preview_output()
        self.state_changed.emit(AudioPlaybackState.STOPPED.value)
        playback_trace("audio_manager.quiesce_for_timeline_rebuild")

    def get_track_state(self, track_id: str) -> Optional[AudioTrackState]:
        """
        获取轨道状态。

        Args:
            track_id: 轨道 ID

        Returns:
            轨道状态或 None
        """
        return self._mixer.get_track_state(track_id)

    def set_track_volume(self, track_id: str, volume: float) -> None:
        """
        设置轨道音量。

        Args:
            track_id: 轨道 ID
            volume: 音量（0.0-2.0）
        """
        # A1: mixer.update_track_* 在单次加锁内完成「读旧→构造→写入 _tracks」并返回新 state，
        # manager 直接写入 _track_states 缓存并发信号。整条链只有 mixer 内部 1 次加锁，
        # 消除了原来的「get→构造→update→写 _track_states」三段加锁导致的读-改-写竞态和冗余分配
        # （原代码构造的 new_state 根本没用上，update_track_volume 内部又重新读取构造）。
        new_state = self._mixer.update_track_volume(track_id, volume)
        if new_state is not None:
            with QMutexLocker(self._mutex):
                self._track_states[track_id] = new_state
            self.track_state_changed.emit(track_id)

    def set_track_mute(self, track_id: str, muted: bool) -> None:
        """
        设置轨道静音。

        Args:
            track_id: 轨道 ID
            muted: 是否静音
        """
        new_state = self._mixer.update_track_mute(track_id, muted)
        if new_state is not None:
            with QMutexLocker(self._mutex):
                self._track_states[track_id] = new_state
            self.track_state_changed.emit(track_id)

    def set_track_solo(self, track_id: str, solo: bool) -> None:
        """
        设置轨道独奏。

        Args:
            track_id: 轨道 ID
            solo: 是否独奏
        """
        new_state = self._mixer.update_track_solo(track_id, solo)
        if new_state is not None:
            with QMutexLocker(self._mutex):
                self._track_states[track_id] = new_state
            self.track_state_changed.emit(track_id)

    def set_track_pan(self, track_id: str, pan: float) -> None:
        """
        设置轨道声像。

        Args:
            track_id: 轨道 ID
            pan: 声像（-1.0 左到 1.0 右）
        """
        new_state = self._mixer.update_track_pan(track_id, pan)
        if new_state is not None:
            with QMutexLocker(self._mutex):
                self._track_states[track_id] = new_state
            self.track_state_changed.emit(track_id)

    def set_track_strip_eq(self, track_id: str, params: Optional[StripEQParams]) -> None:
        """设置某轨的 Strip EQ 参数（W7）。转发到 AudioMixer。

        Args:
            track_id: 轨道 ID
            params: StripEQParams，None 表示禁用 EQ
        """
        self._mixer.set_track_strip_eq(track_id, params)

    def set_track_effect_chain(self, track_id: str, chain_params: Optional[EffectChainParams]) -> None:
        """设置某轨的 FX 机架效果器链（W26）。转发到 AudioMixer。

        Args:
            track_id: 轨道 ID
            chain_params: EffectChainParams，None 表示清空链
        """
        self._mixer.set_track_effect_chain(track_id, chain_params)

    def set_clip_effects(
        self,
        clip_id: str,
        entries: Optional[list],
        track_id: Optional[str] = None,
        animated_sampler: Optional[Any] = None,
    ) -> None:
        """设置某音频 clip 的 per-clip 效果器链（P-clip-FX）。转发到 AudioMixer。

        Args:
            clip_id: clip ID
            entries: [(effect_type, params_dict, enabled), ...]，None/空清空
            track_id: 所属 track ID（用于清理反查）
            animated_sampler: D04 动画 FX 逐帧采样器（None=静态）
        """
        self._mixer.set_clip_effects(
            clip_id, entries, track_id=track_id,
            animated_sampler=animated_sampler)

    def set_audio_transitions(self, track_id: str, transitions: Optional[list]) -> None:
        """AT: 注册某音频轨的转场实例。转发到 AudioMixer。

        Args:
            track_id: 音频轨道 ID
            transitions: ``track.transitions`` 全量列表（本方法只消费 audio:*
                实例）；None/空表示该轨无音频转场。
        """
        self._mixer.set_audio_transitions(track_id, transitions)

    def set_track_automation(self, track_id: str, automation: Dict[str, Any]) -> None:
        """W21: 设置某轨的 automation 数据。转发到 AudioMixer。"""
        self._mixer.set_track_automation(track_id, automation)

    def set_track_automation_mode(self, track_id: str, mode: str) -> None:
        """AM-07: 设置某轨的自动化模式。转发到 AudioMixer。"""
        self._mixer.set_track_automation_mode(track_id, mode)

    def set_track_routing(self, track_id: str, output_bus_id: Optional[str],
                          sends: list, is_bus: bool = False) -> None:
        """W24/W25: 设置某轨的路由信息。转发到 AudioMixer。"""
        self._mixer.set_track_routing(track_id, output_bus_id, sends, is_bus=is_bus)

    def get_all_track_ids(self) -> List[str]:
        """
        获取所有轨道 ID。

        Returns:
            轨道 ID 列表
        """
        with QMutexLocker(self._mutex):
            return list(self._track_states.keys())

    # ==================== Playback Control ====================

    def _clamp_frame(self, frame: int) -> int:
        if self._timeline_duration <= 0:
            return 0
        return max(0, min(int(frame), self._timeline_duration - 1))

    def _create_playback_thread(self, start_frame: int, *, start_paused: bool) -> None:
        """Create the thread-owned audio output path."""
        self._playback_thread = AudioPlaybackThread(self)
        self._playback_thread.setup(
            self._clock,
            self._mixer,
            None,
            self._timeline_fps,
            sample_rate=self._sample_rate,
            channels=2,
            start_frame=start_frame,
            start_paused=start_paused,
        )
        self._playback_thread.frame_sync.connect(self._on_frame_sync)
        self._playback_thread.sync_error.connect(self._on_sync_error)
        self._playback_thread.audio_level.connect(self._on_audio_level)
        self._playback_thread.audio_track_levels.connect(self._on_audio_track_levels)
        # AUD-15508: 播放线程上报输出失败（启动失败/设备中途丢失）。
        # 信号经队列投递回 manager 线程，stop() 在此执行不会与播放线程死锁。
        self._playback_thread.output_failed.connect(self._on_output_failed)
        self._playback_thread.start()

    def _on_output_failed(self, reason: str) -> None:
        """AUD-15508: 播放线程上报音频输出失败（启动失败/设备中途丢失）。

        统一恢复路径：旧实现下 sink 启动失败或运行中设备拔出只会让线程静默
        退出/空转——时钟停走、frame_sync 停发、视频冻结，而 UI 仍显示
        "播放中"（play() 已 emit state_changed("playing")）。此处统一走
        stop()：时钟/线程/输出归零并发射 state_changed("stopped")，
        UI 状态与实际一致。设备热插拔自动重建（QMediaDevices 监听）不在
        本条范围。
        """
        playback_trace("audio_manager.output_failed", reason=reason)
        logger.warning(f"Audio output failed during playback ({reason}); stopping playback")
        self.stop()

    def _ensure_scrub_preview_worker(self) -> AudioScrubPreviewWorker:
        worker = self._scrub_preview_worker
        if worker is not None:
            return worker

        worker = AudioScrubPreviewWorker(
            self._mixer,
            self,
            is_playback_active=lambda: self._clock.is_playing,
        )
        worker.samples_ready.connect(self._on_scrub_preview_samples_ready)
        worker.start()
        self._scrub_preview_worker = worker
        return worker

    def _invalidate_scrub_previews(self) -> None:
        """AUD-15513: 播放启动时使排队的 scrub 预览任务全部失效。

        ``preview_scrub_audio`` 只在入队时检查 ``is_playing``，暂停期入队的
        预览任务在 Play 按下后仍会通过 generation 校验并被混音（复位 DSP、
        seek 解码器），破坏正在开始的播放。此处递增 manager 侧 generation
        （丢弃尚未 emit 的结果）并让 worker 丢弃队列中的过期任务。
        """
        self._scrub_preview_generation += 1
        worker = self._scrub_preview_worker
        if worker is not None:
            worker.cancel_pending()
        playback_trace(
            "audio_manager.scrub_preview_invalidated",
            generation=self._scrub_preview_generation,
        )

    def _ensure_scrub_preview_output(self) -> Optional[AudioOutput]:
        output = self._scrub_preview_output
        if output is None:
            output = AudioOutput(
                sample_rate=self._sample_rate,
                channels=2,
                buffer_frames=SCRUB_AUDIO_OUTPUT_FRAMES,
                parent=self,
            )
            self._scrub_preview_output = output
        if not output.is_playing and not output.start():
            return None
        return output

    def _stop_scrub_preview_output(self) -> None:
        if self._scrub_preview_output is not None:
            self._scrub_preview_output.stop()

    def preview_scrub_audio(self, frame: int, duration_ms: Optional[int] = None) -> bool:
        """
        Request a short audible scrub preview without blocking UI/video scrub.

        Decode/mix happens on AudioScrubPreviewWorker.  Output is best-effort:
        if the tiny preview buffer is full, the block is dropped instead of
        queueing old audio behind the playhead.
        """
        if self._timeline_duration == 0 or self._clock.is_playing:
            return False

        frame = self._clamp_frame(frame)
        duration_ms = (
            self._audio_scrub_preview_ms
            if duration_ms is None
            else max(1, int(duration_ms))
        )
        preview_samples = max(256, int(self._sample_rate * duration_ms / 1000.0))
        self._scrub_preview_generation += 1
        generation = int(self._scrub_preview_generation)
        worker = self._ensure_scrub_preview_worker()
        worker.request_preview(
            frame,
            self._timeline_fps,
            preview_samples,
            2,
            generation,
        )
        self._prepared_frame = None
        self._prepared_fps = 0.0
        playback_trace(
            "audio_manager.scrub_preview_request",
            frame=frame,
            generation=generation,
            duration_ms=duration_ms,
            samples=preview_samples,
        )
        return True

    def _on_scrub_preview_samples_ready(
        self,
        frame: int,
        generation: int,
        samples: object,
    ) -> None:
        if self._clock.is_playing:
            playback_trace(
                "audio_manager.scrub_preview_drop_playing",
                frame=frame,
                generation=generation,
            )
            return
        if int(generation) != int(self._scrub_preview_generation):
            playback_trace(
                "audio_manager.scrub_preview_drop_stale",
                frame=frame,
                generation=generation,
                latest_generation=self._scrub_preview_generation,
            )
            return

        output = self._ensure_scrub_preview_output()
        if output is None:
            playback_trace("audio_manager.scrub_preview_output_unavailable", frame=frame)
            return

        sample_array = np.asarray(samples, dtype=np.float32)
        bytes_needed = sample_array.shape[-1] * output.bytes_per_frame()
        bytes_free = output.bytes_free()
        if bytes_free < bytes_needed:
            playback_trace(
                "audio_manager.scrub_preview_drop_output_full",
                frame=frame,
                generation=generation,
                bytes_free=bytes_free,
                bytes_needed=bytes_needed,
            )
            return

        written = output.write_samples(sample_array)
        playback_trace(
            "audio_manager.scrub_preview_written",
            frame=frame,
            generation=generation,
            samples=sample_array.shape[-1],
            written=written,
        )

    def prepare_for_playback(self, frame: int, preroll_ms: Optional[int] = None) -> bool:
        """
        Warm the thread-owned audio output and ring buffer while transport is paused.

        This is used after scrub/seek so pressing Play can resume a prepared
        QAudioSink + ring instead of cold-starting decode/mix/output.
        """
        if self._timeline_duration == 0:
            return False
        if self._clock.is_playing:
            self.seek(frame)
            return False

        frame = self._clamp_frame(frame)
        preroll_ms = self._audio_preroll_ms if preroll_ms is None else max(0, int(preroll_ms))
        target_samples = max(
            self._clock.get_samples_for_frames(1, self._timeline_fps),
            int(self._sample_rate * preroll_ms / 1000.0),
        )

        self._clock.seek(frame, self._timeline_fps)
        if self._playback_thread is None:
            self._create_playback_thread(frame, start_paused=True)
        self._playback_thread.preroll(frame, self._timeline_fps, target_samples)
        self._prepared_frame = frame
        self._prepared_fps = self._timeline_fps
        self.frame_changed.emit(frame)
        playback_trace(
            "audio_manager.prepare_for_playback",
            frame=frame,
            fps=self._timeline_fps,
            preroll_ms=preroll_ms,
            target_samples=target_samples,
        )
        return True

    def play(self, start_frame: int = 0) -> None:
        """
        开始播放。

        Args:
            start_frame: 起始帧号
        """
        if self._timeline_duration == 0:
            # 空时间线点播放是正常的用户操作（例如刚加载空工程），不是错误。
            # 用 debug 级别避免控制台噪音。
            logger.debug("Cannot play: timeline is empty")
            return

        self._stop_scrub_preview_output()
        # AUD-15513: 使暂停期排队的 scrub 预览任务失效，避免其随后扰动
        # 解码器位置并复位正在播放的 DSP 状态。
        self._invalidate_scrub_previews()
        start_frame = self._clamp_frame(start_frame)
        prepared = (
            self._playback_thread is not None
            and self._prepared_frame == start_frame
            and abs(self._prepared_fps - self._timeline_fps) < 0.0001
        )

        # 启动时钟
        self._clock.start(start_frame, self._timeline_fps)

        # 创建并启动播放线程
        if self._playback_thread is None:
            self._create_playback_thread(start_frame, start_paused=False)
        elif prepared:
            self._playback_thread.resume()
            playback_trace("audio_manager.play_from_preroll", start_frame=start_frame)
        else:
            self._playback_thread.seek(start_frame, self._timeline_fps)
            self._playback_thread.resume()
        self._prepared_frame = None
        self._prepared_fps = 0.0

        # 发射状态变化
        self.state_changed.emit(AudioPlaybackState.PLAYING.value)
        playback_trace(
            "audio_manager.play",
            start_frame=start_frame,
            fps=self._timeline_fps,
            duration=self._timeline_duration,
            tracks=len(self.get_all_track_ids()),
        )

        logger.debug(f"Audio playback started: frame={start_frame}")

    def pause(self) -> None:
        """
        暂停播放。
        """
        if not self._clock.is_playing:
            return

        # 暂停时钟
        self._clock.pause()

        # 暂停播放线程
        if self._playback_thread:
            self._playback_thread.pause()

        # 发射状态变化
        self.state_changed.emit(AudioPlaybackState.PAUSED.value)
        # B13：暂停的瞬时电平 = 静音（与播放线程 -100dB 钳制地板一致）；
        # peak-hold 衰减由各电平表自行处理，这里只提供真实状态 feed
        self._emit_silent_levels()
        playback_trace("audio_manager.pause", frame=self.get_current_frame())

        logger.debug("Audio playback paused")

    def resume(self) -> None:
        """
        恢复播放。

        仅当时钟处于 PAUSED 态才恢复（AUD-15515）。停止态/从未播放时调用
        与 ``pause()`` 的守卫对称：时钟不动、线程不恢复、不误发
        ``state_changed("playing")``，避免 UI 显示"播放中"而实际无声。
        """
        # 恢复时钟；非 PAUSED 态时为 no-op（返回 False）
        if not self._clock.resume():
            logger.debug("Audio resume ignored: clock is not paused")
            return

        # AUD-15513: 暂停期排队的 scrub 预览任务在恢复播放后同样失效。
        self._invalidate_scrub_previews()

        # 恢复播放线程
        if self._playback_thread:
            self._playback_thread.resume()

        # 发射状态变化
        self.state_changed.emit(AudioPlaybackState.PLAYING.value)

        logger.debug("Audio playback resumed")

    def stop(self) -> None:
        """
        停止播放。
        """
        # 停止时钟
        self._clock.stop()

        # 停止播放线程
        if self._playback_thread:
            self._playback_thread.stop()
            # AUD-15514: stop() 常由 UI 线程（含 _on_frame_sync 播放到终点）
            # 触发，线程可能正在 mix_samples 内做 PyAV demux（慢盘/网络素材
            # 单块解码可达秒级）。与 quiesce_for_timeline_rebuild 一致采用
            # 有界 wait + 告警的两段式，常见路径不再无界阻塞 UI。
            self._playback_thread.wait(1000)
            if self._playback_thread.isRunning():
                logger.warning(
                    "Audio playback thread did not stop promptly at stop(); "
                    "waiting for in-flight decode to finish"
                )
                self._playback_thread.wait()
            self._playback_thread = None
        self._prepared_frame = None
        self._prepared_fps = 0.0
        self._stop_scrub_preview_output()

        # 停止输出
        self._output = None

        # 发射状态变化
        self.state_changed.emit(AudioPlaybackState.STOPPED.value)
        # B13：停止（含 EOF→stop）后瞬时电平归静音——不伪造音频、不改
        # 表控件，只把真实生命周期状态 feed 给订阅者；迟到的线程回调
        # 由 _on_audio_* 的播放态守卫丢弃
        self._emit_silent_levels()
        self.frame_changed.emit(0)
        playback_trace("audio_manager.stop")

        logger.debug("Audio playback stopped")

    def _emit_silent_levels(self) -> None:
        """B13：向全部电平订阅者广播静音（master + per-track）。

        master 用双声道 [silent, silent]；per-track 用最近一次 feed 出现
        的轨集合（各表按既有衰减/peak-hold 规则回落）。静音地板与播放
        线程的 dB 钳制一致（-100dB），不引入第二套"无声"语义。
        """
        silent = [LEVEL_SILENT_DB, LEVEL_SILENT_DB]
        self.audio_level_changed.emit(list(silent))
        track_ids = getattr(self, "_last_track_level_ids", None) or ()
        if track_ids:
            self.audio_track_levels_changed.emit({
                track_id: list(silent) for track_id in track_ids})

    def seek(self, frame: int) -> None:
        """
        定位到指定帧。

        Args:
            frame: 目标帧号
        """
        # Clamp 到有效范围
        frame = self._clamp_frame(frame)

        # 定位时钟
        self._clock.seek(frame, self._timeline_fps)

        # 定位播放线程
        if self._playback_thread:
            self._playback_thread.seek(frame, self._timeline_fps)
        self._prepared_frame = None
        self._prepared_fps = 0.0

        # Do not seek decoders from the UI/main thread.  AudioPlaybackThread
        # owns the live decode cadence; the next mix_samples() call will seek
        # each decoder under the mixer lock when it observes the new frame.
        playback_trace("audio_manager.seek_deferred_to_audio_thread", frame=frame)

        # 发射帧变化
        self.frame_changed.emit(frame)

        logger.debug(f"Audio seek to frame={frame}")

    # ==================== Sync Methods ====================

    def get_current_frame(self) -> int:
        """
        获取当前帧号（基于 audio clock）。

        Returns:
            当前帧号
        """
        return self._clock.get_frame_for_fps(self._timeline_fps)

    def get_audio_time(self) -> float:
        """
        获取当前音频时间（秒）。

        Returns:
            当前时间（秒）
        """
        return self._clock.get_audio_time()

    def ring_stats(self) -> dict:
        """
        Final-5 (FINAL-P0-06): 暴露 AudioRingBuffer 统计（underrun/overflow/filled）。

        用于 telemetry / UI 显示音频缓冲健康度。playback thread 未启动时
        返回空 dict（无 ring 实例可用）。
        """
        thread = getattr(self, "_playback_thread", None)
        if thread is None:
            return {}
        ring = getattr(thread, "_ring", None)
        if ring is None:
            return {}
        try:
            return ring.stats()
        except Exception:
            return {}

    def get_video_sync_frame(self) -> int:
        """
        获取视频同步帧。

        基于 audio clock 计算视频应该显示的帧。

        Returns:
            视频同步帧号
        """
        return self._clock.get_frame_for_fps(self._timeline_fps)

    def sync_video_to_audio(self) -> None:
        """
        同步视频到音频。

        如果视频落后，发送同步信号。
        """
        if not self._video_controller:
            return

        # 获取 audio frame
        audio_frame = self.get_current_frame()

        # 获取 video frame
        video_frame = 0
        if hasattr(self._video_controller, 'current_frame'):
            video_frame = self._video_controller.current_frame

        # 计算差异
        diff = audio_frame - video_frame
        if abs(diff) > MAX_SYNC_CORRECTION_FRAMES:
            # 同步误差过大，跳帧
            self.sync_error.emit(abs(diff) * 1000.0 / self._timeline_fps)

            # 发送同步帧
            self.frame_changed.emit(audio_frame)

    # ==================== Signal Handlers ====================

    def _on_frame_sync(self, frame: int) -> None:
        """
        处理播放线程的帧同步信号。

        Args:
            frame: 当前帧
        """
        # 检查边界
        if frame >= self._timeline_duration - 1:
            if self._loop_enabled:
                # 循环
                self.seek(0)
                self.play(0)
            else:
                # 到达终点
                self.stop()
                self.end_reached.emit()
            return

        # 同步视频
        if self._sync_mode == AudioSyncMode.AUDIO_MASTER:
            self.frame_changed.emit(frame)

            # 如果有视频控制器，同步视频帧
            if self._video_controller:
                # 直接更新视频帧（通过信号）
                if hasattr(self._video_controller, 'frame_changed'):
                    # 不直接 emit，而是让视频控制器自己处理
                    pass

    def _on_sync_error(self, error_ms: float) -> None:
        """
        处理同步误差信号。

        Args:
            error_ms: 误差（毫秒）
        """
        self.sync_error.emit(error_ms)

    def _on_audio_level(self, levels: list) -> None:
        """
        处理音频电平信号。

        Args:
            levels: [left_db, right_db] 电平值

        B13③：stop/pause 已广播静音后，跨线程排队中的迟到线程回调
        不得把表"回亮"——非播放态一律丢弃。
        """
        if not self._clock.is_playing:
            return
        self.audio_level_changed.emit(levels)

    def _on_audio_track_levels(self, levels: dict) -> None:
        """
        处理 per-track 音频电平信号。

        Args:
            levels: {track_id: [left_db, right_db]} 每轨电平

        B13：记录最近出现的轨集合（stop/pause 广播静音时按同一集合
        逐轨归零）；非播放态丢弃迟到回调（见 _on_audio_level）。
        """
        if not self._clock.is_playing:
            return
        if levels:
            self._last_track_level_ids = list(levels.keys())
        self.audio_track_levels_changed.emit(levels)

    def _on_video_frame_changed(self, frame: int) -> None:
        """
        处理视频帧变化信号。

        用于 VIDEO_MASTER 模式，音频追视频。

        Args:
            frame: 视频帧号
        """
        if self._sync_mode == AudioSyncMode.VIDEO_MASTER:
            # 音频追视频：定位音频时钟
            self.seek(frame)

    # ==================== Utility Methods ====================

    def get_state(self) -> AudioPlaybackState:
        """
        获取播放状态。

        Returns:
            AudioPlaybackState
        """
        if self._clock.is_playing:
            return AudioPlaybackState.PLAYING
        elif self._clock.get_state().elapsed_at_pause > 0:
            return AudioPlaybackState.PAUSED
        else:
            return AudioPlaybackState.STOPPED

    def get_progress(self) -> float:
        """
        获取播放进度。

        Returns:
            0.0 - 1.0
        """
        if self._timeline_duration <= 0:
            return 0.0
        current_frame = self.get_current_frame()
        # AUD-15517: 单帧时间线（duration == 1）时分母为 0，UI 进度更新
        # 直接抛 ZeroDivisionError；钳到 1 保证有定义。
        denominator = max(1, self._timeline_duration - 1)
        return current_frame / denominator

    def cleanup(self) -> None:
        """
        清理资源。

        停止播放，释放所有解码器。
        """
        try:
            self.stop()
        finally:
            # This must run even if the normal playback thread or a decoder
            # raises during stop().  The manager is a Qt parent of this
            # QThread; allowing parent destruction to race this cleanup makes
            # QThread::~QThread() abort the process on macOS.
            self._stop_scrub_preview_worker()
            if self._scrub_preview_output is not None:
                self._scrub_preview_output.stop()
                self._scrub_preview_output = None

        # 清理所有解码器
        for track_id in list(self._mixer._decoders.keys()):
            self._mixer.remove_track(track_id)

        logger.debug("AudioPlaybackManager cleanup complete")

    def _stop_scrub_preview_worker(self) -> None:
        """Join the audible-scrub QThread before releasing the manager.

        ``AudioScrubPreviewWorker`` owns a custom ``run()`` loop rather than a
        Qt event loop, so ``quit()`` alone cannot stop it.  A normal stop is
        expected to finish within the short queue timeout.  If a decoder is
        still mixing, wait for that cooperative operation to return rather
        than terminating a thread while it may hold an AudioMixer/PyAV lock.
        The unbounded final wait is intentional: returning while the QThread
        is still running is exactly what causes Qt's fatal destructor warning.
        """
        worker = self._scrub_preview_worker
        if worker is None:
            return

        worker.stop()
        if worker.isRunning() and not worker.wait(3000):
            logger.warning(
                "Audio scrub preview worker is still mixing; waiting for "
                "cooperative shutdown"
            )
            worker.wait()

        if worker.isRunning():
            # Defensive assertion/logging for unusual Qt backends.  Keep the
            # reference so the parent cannot silently destroy a live thread.
            logger.error(
                "Audio scrub preview worker is still running after join"
            )
            return

        self._scrub_preview_worker = None

    def __del__(self) -> None:
        try:
            self.cleanup()
        except Exception:
            pass


# =============================================================================
# Integration Helper
# =============================================================================

class AudioVideoSynchronizer:
    """
    音视频同步器。

    协调 AudioPlaybackManager 和 EditPlaybackController。

    Usage:
        synchronizer = AudioVideoSynchronizer(audio_manager, video_controller)
        synchronizer.setup()
        synchronizer.play(start_frame=0)
        synchronizer.pause()
        synchronizer.seek(frame=100)
    """

    def __init__(
        self,
        audio_manager: AudioPlaybackManager,
        video_controller: Any,  # EditPlaybackController
    ) -> None:
        """
        初始化同步器。

        Args:
            audio_manager: 音频播放管理器
            video_controller: 视频播放控制器
        """
        self._audio = audio_manager
        self._video = video_controller
        self._sync_mode: AudioSyncMode = AudioSyncMode.AUDIO_MASTER

    def setup(self) -> None:
        """
        设置同步。

        连接信号，配置同步模式。
        """
        # 音频主导时，音频帧变化触发视频同步
        if self._sync_mode == AudioSyncMode.AUDIO_MASTER:
            self._audio.frame_changed.connect(self._sync_video_to_audio)

        # 视频主导时，视频帧变化触发音频同步
        elif self._sync_mode == AudioSyncMode.VIDEO_MASTER:
            if hasattr(self._video, 'frame_changed'):
                self._video.frame_changed.connect(self._audio.seek)

        logger.debug("AudioVideoSynchronizer setup complete")

    def set_sync_mode(self, mode: AudioSyncMode) -> None:
        """
        设置同步模式。

        Args:
            mode: AudioSyncMode
        """
        self._sync_mode = mode
        self.setup()

    def play(self, start_frame: int = 0) -> None:
        """
        播放。

        同时启动音频和视频。

        Args:
            start_frame: 起始帧
        """
        # 先启动音频（时钟源）
        self._audio.play(start_frame)

        # 同步视频位置
        if self._sync_mode == AudioSyncMode.AUDIO_MASTER:
            # 视频会在收到音频帧信号后同步
            pass
        else:
            # 视频主导：启动视频播放
            if hasattr(self._video, 'play'):
                self._video.play()

    def pause(self) -> None:
        """
        暂停。

        同时暂停音频和视频。
        """
        self._audio.pause()
        if hasattr(self._video, 'pause'):
            self._video.pause()

    def stop(self) -> None:
        """
        停止。

        同时停止音频和视频。
        """
        self._audio.stop()
        if hasattr(self._video, 'stop'):
            self._video.stop()

    def seek(self, frame: int) -> None:
        """
        定位。

        同时定位音频和视频。

        Args:
            frame: 目标帧
        """
        # 先定位音频（时钟源）
        self._audio.seek(frame)

        # 定位视频
        if hasattr(self._video, 'seek'):
            self._video.seek(frame)

    def _sync_video_to_audio(self, frame: int) -> None:
        """
        同步视频到音频帧。

        Args:
            frame: 音频帧号
        """
        if self._sync_mode != AudioSyncMode.AUDIO_MASTER:
            return

        if hasattr(self._video, 'seek'):
            self._video.seek(frame)


# Q01/F1.2 根治：音频解码公共入口加进程级 in-flight 计数，使 ffmpeg_source
# 的常驻关闭线程只在全局解码空闲窗口执行容器关闭（打断 libav↔GIL 锁序反转）。
from src.shared.media.av_concurrency import av_operation_decorator as _av_op_dec  # noqa: E402

for _av_entry in (
    "open", "seek_to_time", "seek_to_frame",
    "decode_next_samples", "decode_samples_for_frame",
):
    setattr(AudioDecoder, _av_entry,
            _av_op_dec(AudioDecoder.__dict__[_av_entry]))
