"""
Realtime Playback Core Types
专业 NLE 实时播放内核核心类型定义。

对应规格书 §5。
所有数据结构都是纯 dataclass / enum，不依赖 Qt，方便在测试/benchmark 中独立使用。

设计要点（来自规格书）：
- PlaybackMode 区分 realtime / paused / scrub / step / export，决定能否 master fallback。
- SourceKind 必须显式标记帧来源，禁止隐藏 master fallback。
- FrameIdentity 的 cache key 必须包含 clip_id，避免同素材多 clip 共用 decode cursor。
- FrameHandle.exact=False 不能用于 paused/step/export；realtime miss 应 hold 而非展示错帧。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Tuple


# =============================================================================
# PlaybackMode  (规格书 §5.1)
# =============================================================================


class PlaybackMode(Enum):
    """播放模式，决定 source 策略与 master 允许性。"""

    REALTIME = "realtime"  # 实时播放，禁止 master fallback
    PAUSED = "paused"      # 暂停，允许 master exact
    SCRUB = "scrub"        # 拖拽 scrub，可短 timeout / 低质占位
    STEP = "step"          # 逐帧，必须 exact
    EXPORT = "export"      # 导出，允许 master + 等待

    @property
    def is_realtime(self) -> bool:
        """是否处于实时播放模式（热路径严格策略生效）。"""
        return self is PlaybackMode.REALTIME

    @property
    def allows_master(self) -> bool:
        """该模式是否允许 master fallback / 直接 master 解码。

        REALTIME 严格禁止，其它模式允许。
        """
        return self is not PlaybackMode.REALTIME

    @property
    def allows_blocking(self) -> bool:
        """该模式是否允许同步等待解码（rebuffer）。"""
        return self in (PlaybackMode.PAUSED, PlaybackMode.STEP, PlaybackMode.EXPORT)


# =============================================================================
# SourceKind  (规格书 §5.2)
# =============================================================================


class SourceKind(Enum):
    """帧来源类型，必须显式记录用于 trace 与统计。"""

    DISK_PROXY = "disk_proxy"        # 磁盘代理文件（专业 NLE 主路径）
    RENDER_CACHE = "render_cache"    # 渲染缓存（复杂效果/comp clip）
    MEMORY_PROXY = "memory_proxy"    # 内存降级源（仅过渡，不是专业代理）
    MASTER = "master"                # 原始母版（仅 paused/step/export）
    PLACEHOLDER = "placeholder"      # 占位（缩略图/上一帧）
    NONE = "none"                    # 无可用源

    @property
    def is_realtime_ready(self) -> bool:
        """是否可作为 realtime 主路径 source。

        memory_proxy 仅过渡，不计为 ready；master 不算 ready。
        """
        return self in (SourceKind.DISK_PROXY, SourceKind.RENDER_CACHE)

    @property
    def is_master(self) -> bool:
        return self is SourceKind.MASTER


# =============================================================================
# FrameIdentity  (规格书 §5.3)
# =============================================================================


@dataclass(frozen=True)
class FrameIdentity:
    """
    帧的身份标识。

    cache key 必须包含 clip_id，避免同一 source path 的多个 timeline clip
    共用一个 decode cursor（规格书 §5.3 / §10.1）。
    代理文件可按 source_path 共享，但 display decode stream 不能共享 cursor。

    Attributes:
        timeline_id: 时间线/工程标识
        clip_id: timeline clip 唯一 id
        asset_id: 素材池中的资产 id
        source_path: 源文件绝对路径
        source_frame: 源视频帧号（0-based）
        source_kind: 帧来源类型
        proxy_key: 磁盘代理 key（可空）
        render_cache_key: 渲染缓存 key（可空）
    """

    timeline_id: str
    clip_id: str
    asset_id: str
    source_path: str
    source_frame: int
    source_kind: SourceKind
    proxy_key: str | None = None
    render_cache_key: str | None = None


# =============================================================================
# FrameHandle  (规格书 §5.4)
# =============================================================================


@dataclass
class FrameHandle:
    """
    单层帧句柄。

    生命周期：decode worker 产出 -> ring buffer 缓存 -> GpuFramePool 上传 -> compositor 消费。

    Attributes:
        identity: 帧身份
        frame_index: 源帧号（冗余于 identity.source_frame，便于 ring buffer 查询）
        width / height: 帧尺寸
        pixel_format: 像素格式（如 "yuv420p", "yuv422p10le", "rgb24"）
        bit_depth: 位深 (8/10/12/16)
        color_space: 颜色空间标记（可空）
        pts_seconds: 源 PTS（秒）
        source_kind: 实际来源
        payload: 帧数据（GPU texture id / planar planes / cpu ndarray / placeholder）
        payload_type: payload 类型标记
        exact: 是否精确帧（false 不能用于 paused/step/export）
        decode_ms: 解码耗时（用于 trace）
    """

    identity: FrameIdentity
    frame_index: int
    width: int
    height: int
    pixel_format: str
    bit_depth: int
    color_space: str | None
    pts_seconds: float
    source_kind: SourceKind
    payload: object
    payload_type: str  # "gpu_texture" | "planar_yuv" | "cpu_ndarray" | "placeholder"
    exact: bool = True
    decode_ms: float = 0.0

    def is_gpu_payload(self) -> bool:
        return self.payload_type == "gpu_texture"

    def is_planar(self) -> bool:
        return self.payload_type == "planar_yuv"

    def is_cpu_payload(self) -> bool:
        return self.payload_type == "cpu_ndarray"

    def is_placeholder(self) -> bool:
        return self.payload_type == "placeholder" or self.source_kind is SourceKind.PLACEHOLDER


# =============================================================================
# LayerFrameBundle  (规格书 §5.5)
# =============================================================================


@dataclass
class LayerFrameBundle:
    """
    单个 timeline frame 的多层帧 bundle。

    complete=True 才能提交 GPU compositor。
    complete=False 时 engine 选择 hold/rebuffer/placeholder。

    Attributes:
        timeline_frame: 时间线帧号
        generation_id: 调度 generation（seek 后递增，旧 generation 结果必须丢弃）
        layers: 各 layer 的 FrameHandle（按 track_index 排序）
        deadline_ns: 截止时间戳（用于 rebuffer 决策）
        complete: 是否所有 layer 都 ready
        miss_reason: 未 complete 时的原因（可空）
    """

    timeline_frame: int
    generation_id: int
    layers: Tuple[FrameHandle, ...]
    deadline_ns: int
    complete: bool
    miss_reason: str | None = None

    @property
    def layer_count(self) -> int:
        return len(self.layers)

    def is_stale(self, current_generation_id: int) -> bool:
        """bundle 是否过期（generation 不匹配）。"""
        return self.generation_id != current_generation_id


# =============================================================================
# DecodeMiss / SourceDecision  (规格书 §6)
# =============================================================================


@dataclass(frozen=True)
class DecodeMiss:
    """解码 miss 事件，用于 policy 决定 rebuffer/placeholder。"""

    timeline_frame: int
    clip_id: str
    asset_id: str
    source_frame: int
    source_kind: SourceKind
    reason: str
    generation_id: int = 0


# =============================================================================
# MissReason  (缺陷计划 RP-P1-05：统一 miss 语义)
# =============================================================================


class MissReason(Enum):
    """miss 归一词表（规格书 P1-05）。

    五类核心语义 + engine 侧来源标记。policy 按类别决定
    hold / rebuffer / 警告，禁止"假装成功"（详见 policy.should_rebuffer）。
    """

    DECODE_MISS = "decode_miss"                # 解码未就绪/解码器跟不上（瞬态）
    GPU_BUSY = "gpu_busy"                      # GPU lane/lease/slot 饱和（瞬态）
    CACHE_WARMING = "cache_warming"            # ring/缓存仍在填充（瞬态）
    PROXY_PENDING = "proxy_pending"            # 磁盘代理生成中（等待无意义→rebuffer）
    UNSUPPORTED_EFFECT = "unsupported_effect"  # 无实时路径的效果（hold+警告，不rebuffer）
    BACK_PRESSURE = "back_pressure"            # 播放预算 drop（瞬态）
    DELEGATE_MISS = "delegate_miss"            # delegate 未归因失败（hold-first 兜底）


def classify_miss_reason(reason: str | None) -> MissReason:
    """把自由文本 miss reason 归一为 MissReason。

    生产方（scheduler miss_reason、decode 失败串、engine 通道）各自输出
    语义化字符串；policy 只信任归一化后的类别。未知字符串归为
    DELEGATE_MISS（瞬态 hold-first），绝不默认立即 rebuffer。
    """
    if not reason:
        return MissReason.DELEGATE_MISS
    text = reason.strip().lower()
    # 精确命中优先（包括嵌套串里的完整类别词）。
    for member in MissReason:
        if member.value in text:
            return member
    # 已知生产方的短语（TimelineDecodeScheduler.request_bundle、renderer
    # GPU 结果 drop 阶段、frame provider 解码失败串等）。
    if "proxy" in text and ("generating" in text or "pending" in text or "building" in text):
        return MissReason.PROXY_PENDING
    if "gpu" in text and (
        "busy" in text
        or "lane" in text
        or "timeout" in text
        or "slot" in text
        or "result_drop" in text
    ):
        return MissReason.GPU_BUSY
    if (
        "not ready" in text
        or "warming" in text
        or "prefetch" in text
        or "no layers" in text
        or "source_frame" in text
        or "stream" in text
    ):
        return MissReason.CACHE_WARMING
    if "unsupported" in text or "no realtime" in text or "effect" in text:
        return MissReason.UNSUPPORTED_EFFECT
    if "back_pressure" in text or "budget" in text:
        return MissReason.BACK_PRESSURE
    if (
        "decode" in text
        or "decoder" in text
        or "no frame" in text
        or "open failed" in text
        or "deadline" in text
    ):
        return MissReason.DECODE_MISS
    return MissReason.DELEGATE_MISS


@dataclass(frozen=True)
class SourceDecision:
    """
    RealtimePlaybackPolicy 的单 layer 决策结果。

    所有字段必须可 trace，禁止隐藏 master fallback。
    """

    source_kind: SourceKind
    source_path: str | None
    proxy_info: object | None
    render_cache_info: object | None
    allow_blocking_decode: bool
    allow_master_fallback: bool
    max_wait_ms: int
    reason: str
