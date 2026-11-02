"""
Timeline Export Module
时间线导出控制器。

## 聧责

- TimelineExporter: 时间线导出控制器
- 支持多种来源：Current Color Image / Current Composite Output / Current Edit Timeline
- 使用 TimelineRenderPlan 渲染帧，不走 UI viewer
- 支持 range、resolution、fps、codec、audio 配置

## 设计原则

- 纯渲染管线，不依赖 UI 线程
- 使用 FFmpegVideoEncoder 编码输出
- 使用 VideoFrameProviderService 获取帧
- 完整类型注解
- 进度通知信号

## 导出流程

1. 用户配置导出参数（source, range, resolution, codec）
2. TimelineExporter 初始化渲染管线
3. 使用 TimelineRenderPlan 计算每帧的渲染内容
4. 使用 PreviewRenderWorker 合成帧
5. 使用 FFmpegVideoEncoder 编码输出
6. 进度通知 UI

## 架构

    ┌──────────────────────────────────────────────────────┐
    │ DeliverPage (UI)                                     │
    │ - 配置导出参数                                         │
    │ - 监听 progress 信号                                  │
    └──────────────────────────────────────────────────────┘
                            │ export_requested(config)
                            ▼
    ┌──────────────────────────────────────────────────────┐
    │ TimelineExporter                                     │
    │ - 持有 EditProject, TimelineSequence                │
    │ - 持有 VideoFrameProviderService                    │
    │ - 持有 TimelineRenderPlan                           │
    │ - 持有 FFmpegVideoEncoder                           │
    └──────────────────────────────────────────────────────┘
                            │
            ┌───────────────┼───────────────┐
            ▼               ▼               ▼
    RenderPlan       FrameProvider     VideoEncoder
    (计算每帧内容)    (获取原始帧)      (编码输出)
"""

from __future__ import annotations

import dataclasses
import logging
import math
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

from src.features.edit.core.proxy_policy import export_source_path
from src.features.color.core.curves_soft_clip import (
    curves_edit_is_default,
    curves_soft_clip_is_identity,
)
from src.features.shared.effects.effect_tiers import effective_denoise_radius
from src.features.edit.core.transition_catalog import (
    COMPOSITE_TRANSITION_TYPES,
    FULL_FRAME_TRANSITION_TYPES,
    LIB1_TRANSITION_TYPES,
    RESAMPLE_TRANSITION_TYPES,
    SPATIAL_TRANSITION_TYPES,
    TRANSITION_EFFECT_TYPES,
)
from src.features.edit.core.transition_effects import (
    apply_full_frame_transition,
    apply_transition_effect,
)
from src.features.edit.core.transition_mask import compute_transition_mask
from src.features.edit.core.timeline_v2 import (
    effective_timeline_duration_frames,
    resolve_timeline_in_out,
)
from src.shared.export.gpu_trail import ExportGpuTrail, ExportGpuTrailSnapshot
from src.shared.export.export_stage_telemetry import ExportTelemetryRecorder
from src.features.edit.core.timeline_render_plan import (
    build_direct_topos_delivery_plan,
)

if TYPE_CHECKING:
    import numpy as np
    from src.shared.media.frame import DecodedFrame
    from src.shared.export.video_encoder import FFmpegVideoEncoder, FFmpegEncoderConfig
    from src.shared.export.video_encoder import ColorMetadataConfig
    from src.shared.video.frame_provider_service import VideoFrameProviderService
    from src.features.edit.core.timeline_v2 import TimelineSequence
    from src.features.edit.core.timeline_render_plan import DirectToposDeliveryPlan
    from src.features.edit.core.timeline_render_plan import TimelineRenderPlan, ClipLayerInfo, TransitionBlendInfo
    from src.features.edit.core.project import EditProject
    from src.features.edit.core.nested_sequence import NestedAudioDiagnostic, SequenceResolver

logger = logging.getLogger(__name__)


class ExportGradeDegradedError(RuntimeError):
    """N03 · 导出调色降级硬门——视觉能力缺失默认拒绝成片。

    keyer 失败（无限 matte=整帧误调）与 qualifier 后处理不支持
    （丢弃=视觉差异）不再"告警后继续"；仅在导出器显式
    ``allow_degraded_grade=True`` 时保留降级路径（声明式取舍）。
    """


#: F2.8：optical_flow 未实现的显式告警去重（clip_id 集合）
_OPTICAL_FLOW_WARNED: set = set()

# P2-17：导出 OETF（_linear_bgr_to_export_signal → transform_color）实现
# 身份。进入导出帧级缓存 SegmentRenderKey.color_management——色彩变换
# 实现变化而图数据不变时，键必须变化以避免命中旧实现渲染的帧。
EXPORT_OETF_VERSION = "export_oetf_v1"

# 复验 P0-03：Topos planar 帧识别（与 topos_source._pixel_format_name 的
# 命名一致：yuv422/444/gbr p10/p12le + topos_yuva*/topos_gbrap* 带独立
# alpha 位深后缀）。命中 = provider 已给出精确满刻度平面，导出链直通。
import re as _re_mod

import atexit as _atexit_mod

# 批 4：+16（视频/图片 16-bit 档；profile 3/5 合法，4444xq/profile6 仍 12-bit）
_TOPOS_PLANAR_RE = _re_mod.compile(
    r'^(yuv(422|444)|gbr)p(10|12|16)le$'
    r'|^topos_(yuva(422|444)p(10|12|16)|gbrap(10|12|16))a(8|10|12|16)$')
_TOPOS_COLOR_DEPTH_RE = _re_mod.compile(r'p(10|12|16)(?:le|a)')
_TOPOS_ALPHA_DEPTH_RE = _re_mod.compile(r'a(\d+)$')

# —— N03：后台 manifest 写线程登记表 ——————————————————————
# _write_success_manifest_async 的 daemon 线程会跨导出调用存活（等待
# 交付定稿 + 全量 SHA-256 可能达数秒）。没有登记/排空机制时，线程会
# 泄漏进后续无关测试与会话收尾（实测：与 pytest 卸载阶段的 GC 竞争
# 产生 SIGSEGV，且与后续编码并发污染进程级 native 状态）。daemon +
# fire-and-forget 语义下导出器自身无法可靠追踪线程终点，因此用模块
# 级登记表 + 显式排空 API 收口。
_MANIFEST_WRITER_THREADS: "set[threading.Thread]" = set()
_MANIFEST_WRITERS_LOCK = threading.Lock()
_MANIFEST_WRITERS_ATEXIT_DONE = False


def wait_for_manifest_writers(timeout: float = 60.0) -> bool:
    """等待全部后台 manifest 写线程结束；False=超时仍有存活。

    测试（每例后排空）与应用关停（atexit 窗口）共用。幂等、线程安全；
    join 期间新线程仍可登记（deadline 统一裁决）。
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        with _MANIFEST_WRITERS_LOCK:
            pending = [t for t in _MANIFEST_WRITER_THREADS if t.is_alive()]
        if not pending:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return False
        for t in pending:
            t.join(timeout=min(remaining, 5.0))


def _drain_manifest_writers_atexit() -> None:
    # 解释器拆除（daemon 线程被硬杀、GC 收对象）之前给 manifest 写线程
    # 一个收敛窗口；超时则维持既有 daemon 语义（sidecar 原子替换）。
    wait_for_manifest_writers(timeout=30.0)


def _register_manifest_writer(thread: "threading.Thread") -> None:
    global _MANIFEST_WRITERS_ATEXIT_DONE
    with _MANIFEST_WRITERS_LOCK:
        _MANIFEST_WRITER_THREADS.add(thread)
        register_atexit = not _MANIFEST_WRITERS_ATEXIT_DONE
        _MANIFEST_WRITERS_ATEXIT_DONE = True
    if register_atexit:
        _atexit_mod.register(_drain_manifest_writers_atexit)


def _unregister_manifest_writer(thread: "threading.Thread") -> None:
    with _MANIFEST_WRITERS_LOCK:
        _MANIFEST_WRITER_THREADS.discard(thread)


@dataclass(frozen=True)
class _ManifestSnapshot:
    """AUD-18402：manifest 写入所需导出状态的一次性快照。

    后台 manifest 线程与 individual-clips 子导出循环并发，快照在导出
    线程于 ``_write_success_manifest_async`` 入口捕获，后台线程只消费
    快照、绝不回读 exporter 的可变成员。
    """
    project: Any
    sequence: Any
    qc_report: Any
    preflight_result: Any
    loudness_report: Any
    effect_export_skips: Tuple[str, ...]
    stage_telemetry: Optional[Dict[str, Any]] = None


def _is_topos_planar_format(pixel_format: str) -> bool:
    return _TOPOS_PLANAR_RE.match(str(pixel_format or '').strip().lower()) is not None


def _topos_planar_depths(pixel_format: str) -> Tuple[int, Optional[int]]:
    """Topos planar 名 → (颜色位深, alpha 位深|None)。alpha 后缀不污染
    颜色位深（复验 P0-03：`_bit_depth_for_pixel_format` 曾把 a12/a16
    误认为颜色位深）。"""
    s = str(pixel_format or '').strip().lower()
    m = _TOPOS_COLOR_DEPTH_RE.search(s)
    color = int(m.group(1)) if m else 10
    ma = _TOPOS_ALPHA_DEPTH_RE.search(s)
    alpha = int(ma.group(1)) if ma else None
    return color, alpha


def _topos_timeline_cpu_threads(
    cpu_count: Optional[int] = None,
) -> Tuple[int, Optional[int]]:
    """Split TOPOS timeline CPU workers between encoder and intra decoder.

    On systems with at least 12 logical CPUs, reserving about one third (up
    to eight workers) for the parallel source decoder avoids running its
    automatic slice pool on top of the TOPOS native encoder's full pool. On
    smaller systems retain the existing encoder cap and FFmpeg auto policy.
    """
    available = max(1, int(cpu_count or os.cpu_count() or 1))
    if available < 12:
        return min(16, available), None
    decode_threads = min(8, max(4, available // 3))
    encode_threads = min(16, max(1, available - decode_threads))
    return encode_threads, decode_threads


def apply_program_guide_matte_bgr(
    frame_bgr_u8: "np.ndarray",
    ratio: float,
) -> "np.ndarray":
    """Return a copy of *frame_bgr_u8* with an opaque aspect-ratio matte.

    The output resolution is intentionally preserved.  A wider target ratio
    creates top/bottom cinema bars; a taller target ratio creates side bars.
    """
    height, width = frame_bgr_u8.shape[:2]
    if width <= 0 or height <= 0 or ratio <= 0.0:
        return frame_bgr_u8

    frame_ratio = width / height
    if abs(frame_ratio - ratio) < 1e-9:
        return frame_bgr_u8

    matte = frame_bgr_u8.copy()
    if ratio > frame_ratio:
        content_height = max(1, min(height, int(round(width / ratio))))
        top = (height - content_height) // 2
        bottom = height - top - content_height
        if top > 0:
            matte[:top, ...] = 0
        if bottom > 0:
            matte[height - bottom:, ...] = 0
    else:
        content_width = max(1, min(width, int(round(height * ratio))))
        left = (width - content_width) // 2
        right = width - left - content_width
        if left > 0:
            matte[:, :left, ...] = 0
        if right > 0:
            matte[:, width - right:, ...] = 0
    return matte


# =============================================================================
# 导出配置
# =============================================================================


class ExportSource(Enum):
    """导出来源类型"""

    COLOR_IMAGE = "color_image"       # 当前 Color 页面图像
    COMPOSITE_OUTPUT = "composite"    # 当前 Composite 输出
    TIMELINE = "timeline"             # 当前 Edit Timeline


class ExportRangeType(Enum):
    """导出范围类型"""

    ENTIRE = "entire"          # 整个时间线
    IN_OUT = "in_out"         # In/Out 点范围
    SELECTED = "selected"     # 选中的 clip
    CUSTOM = "custom"         # 自定义帧范围


class ExportRenderMode(Enum):
    """Whether delivery produces one timeline file or one file per clip."""

    SINGLE_CLIP = "single_clip"
    INDIVIDUAL_CLIPS = "individual_clips"


@dataclass(frozen=True)
class _IndividualClipExport:
    """One isolated render plan produced by Individual Clips delivery."""

    output_path: str
    start_frame: int
    end_frame: int
    sequence: Any


class ExportCodec(Enum):
    """导出编码器类型"""

    # H.264 / H.265
    H264 = "h264"
    HEVC = "hevc"
    # Palette-based animated image
    GIF = "gif"

    # Apple ProRes 全 Profile
    PRORES_PROXY = "prores_proxy"      # Offline editing
    PRORES_LT = "prores_lt"            # Rough cuts
    PRORES_422 = "prores_422"          # General editing
    PRORES_422HQ = "prores_422hq"      # Final cuts, broadcast
    PRORES_4444 = "prores_4444"        # VFX, compositing (with alpha)
    PRORES_4444XQ = "prores_4444xq"    # Maximum quality (12-bit + alpha)

    # Avid DNxHR 全 Profile
    DNXHR_LB = "dnxhr_lb"              # Low Bandwidth - Offline editing (8-bit)
    DNXHR_SQ = "dnxhr_sq"              # Standard Quality - General editing (8-bit)
    DNXHR_HQ = "dnxhr_hq"              # High Quality - Final cuts (8-bit)
    DNXHR_HQX = "dnxhr_hqx"            # High Quality Extended - Broadcast (10-bit)
    DNXHR_444 = "dnxhr_444"            # DNxHR 444 - VFX, compositing (10-bit + alpha)

    # Topos Video Codec（自研，阶段 8；前四档 = profile3 质量预设；
    # R4.6：4444/4444XQ 为独立 bitstream profile 5/6（ADR-C023 原名
    # Pro444/Extreme），进入导出枚举）
    TOPOS_PROXY = "topos_proxy"        # 离线代理、远程剪辑
    TOPOS_LT = "topos_lt"              # 轻量中间缓存、粗剪
    TOPOS = "topos"                    # Standard：常规剪辑和渲染缓存
    TOPOS_HQ = "topos_hq"              # 调色、中间母版
    TOPOS_4444 = "topos_4444"          # 4444：VFX、动态图形（4:4:4 10-bit）
    TOPOS_4444XQ = "topos_4444xq"      # 4444 XQ：高宽容度 HDR（4:4:4 12-bit）
    # v1.8（ADR-C056）：唯一帧间档（微 GOP IP-2，载体 V7-R3）。无 Alpha、
    # 固定锚 qp——能力收缩见 topos_profiles lp 档声明。
    TOPOS_LP = "topos_lp"              # 422 LP：实时粗剪缓存、快速转码
    # v1.9（P2 重标定）：旧 Proxy 低码率档独立保留——低带宽远程预览/
    # 网络流转码（非 ProRes 对标档；不做画质对标验收）
    TOPOS_NETPROXY = "topos_netproxy"  # 422 Net Proxy：网络代理档
    # M4-R7/R8（ADR-C058）：视频 RAW——CFA 相位平面 RAW→RAW 直出（帧内
    # 全 I，位深×比率 12 档锚与图片线共享；比率选择经 codec_profile 携带
    # 注册表 id 'raw12-4' 等，qp 由此换算 crf）
    TOPOS_RAW = "topos_raw"            # Topos RAW：相机 RAW CFA 中间片

    # 图像序列
    PNG_SEQUENCE = "png_sequence"
    TIFF_SEQUENCE = "tiff_sequence"
    TOPOS_IMAGE_SEQUENCE = "topos_image_sequence"


@dataclass(frozen=True)
class AudioStemExportTrack:
    """导出容器追加音轨（stems 角色轨）声明（v1.6 M-B8，ADR-C054 §4）。

    语义：轨 0 = 主混音（既有 audio_* 配置，旧 reader 的兼容视图）；
    本声明序列表逐条映射为追加轨 1..N——按 ``stem_group``（EditTrack
    的 D/M/E 角色标签）过滤渲染独立母带，每轨独立 codec/声道/格式，
    轨名（容器 ©nam）承载角色标签供 Resolve/Premiere 显示。

    Attributes:
        stem_group: 参与该轨的 EditTrack.stem_group 标签（非空；
            "dialogue"/"music"/"effects"/…）
        name: 容器轨名；空则回落 stem_group 首字母大写
        codec: 宿主音频 codec 名（ToposVideoEncoder._AUDIO_CODEC_MAP 档：
            aac / pcm_s16le / pcm_s24le / pcm_s32le / pcm_f32le）
        channels: 声道数（1/2/6/8；1 = mono，广播对白轨惯例）
        channel_layout: 显式布局名（P1-09 单一真相；空回落 channels）
        bitrate: AAC 码率 kbps（lpcm 档忽略）
    """
    stem_group: str = ""
    name: str = ""
    codec: str = "pcm_s24le"
    channels: int = 2
    channel_layout: str = ""
    bitrate: Optional[int] = None


def resolve_stem_track_ids(
    sequence: "TimelineSequence",
    stem_group: str,
) -> Tuple[int, ...]:
    """stem_group → 参与该角色母带渲染的音轨 track_id 元组（M-B8）。

    口径与 `_render_audio_master` 的轨收集一致：普通轨按 stem_group
    匹配且需有 clip，且须通过渲染闸门（可见/启用/未静音 + solo 可听
    闭包）——声明（首帧前）与渲染（音频线程）必须同一过滤，否则
    "已声明但渲染为空"的轨会令 finish fail-fast 使整个导出失败
    （回归：角色轨全部静音时导出必败）。Bus 轨不在此列（母带渲染
    保留完整 Bus/Master 链，被排除轨贡献恒零——等价于 solo 该组，
    与 stem_export 设计一致）。
    传入序列应为展开后的音频序列（与渲染同源）。
    """
    group = str(stem_group or "").strip()
    if not group:
        return ()
    tracks = list(getattr(sequence, "audio_tracks", ()) or ())
    from src.features.edit.core.timeline_render_plan import is_track_renderable
    has_solo = any(bool(getattr(t, "is_solo", False)) for t in tracks)
    audible_solo = set()
    if has_solo:
        # RMA-04 可听闭包：与 _render_audio_master 同一计算
        # （拓扑口径，与采样率无关）
        from src.features.edit.core.audio_graph_renderer import (
            AudioGraphRenderer,
        )
        renderer = AudioGraphRenderer(48000.0)
        renderer.configure_from_sequence(sequence)
        audible_solo = renderer.compute_audible_solo_closure()
    return tuple(
        t.track_id
        for t in tracks
        if not getattr(t, "is_bus", False)
        and (getattr(t, "stem_group", "") or "") == group
        and len(getattr(t, "clips", ()) or ()) > 0
        and is_track_renderable(t, has_solo)
        and (not has_solo or t.track_id in audible_solo)
    )


def auto_detect_stem_groups(sequence: "TimelineSequence") -> Tuple[str, ...]:
    """检测序列中已标注的 stem_group 标签（声明序去重；UI 列举用）。"""
    groups: "list[str]" = []
    for t in (getattr(sequence, "audio_tracks", ()) or ()):
        g = str(getattr(t, "stem_group", "") or "").strip()
        if g and g not in groups:
            groups.append(g)
    return tuple(groups)


@dataclass
class TimelineExportConfig:
    """
    时间线导出配置。

    Attributes:
        source: 导出来源类型
        timeline_id: 时间线 ID（仅用于 Timeline 来源）
        range_type: 导出范围类型
        start_frame: 自定义起始帧（仅用于 CUSTOM 范围）
        end_frame: 自定义结束帧（仅用于 CUSTOM 范围）
        resolution: 输出分辨率 (width, height)，None 表示使用原始分辨率
        fps: 输出帧率，None 表示使用原始帧率
        codec: 编码器类型
        codec_profile: 编码器 profile（如 ProRes 的 hq、lt）
        quality: 质量/比特率设置（CRF 数值或 bps）
        quality_mode: quality 的解释方式（auto / vbr / cbr / crf）
        include_audio: 是否包含音频
        output_path: 输出文件路径
    """

    source: ExportSource = ExportSource.TIMELINE
    timeline_id: Optional[str] = None
    range_type: ExportRangeType = ExportRangeType.ENTIRE
    start_frame: int = 0
    end_frame: int = -1  # -1 表示到末尾
    resolution: Optional[Tuple[int, int]] = None
    fps: Optional[float] = None
    codec: ExportCodec = ExportCodec.PRORES_422
    codec_profile: Optional[str] = None
    quality: Optional[int] = None  # CRF 或目标码率（bps）
    quality_mode: str = "auto"  # "auto" | "vbr" | "cbr" | "crf"
    # GIF palette size.  Kept on the timeline config so Quick Export and
    # Deliver jobs share the same validated export contract.
    gif_color_count: int = 256
    # P8-Topos：显式 Alpha 输出开关（Topos 等自带 Alpha 语义的 codec；
    # yuva* pix_fmt 的 codec（ProRes 4444/DNxHR 444）由 pix_fmt 隐含）。
    has_alpha: bool = False
    # R7：Topos pix_fmt 显式覆盖（空 = codec_map 档位默认；444 档下
    # yuv444p*/gbrp* 及 a{N} 变体——ADR-C020 遗留收口）。Topos codec
    # 由编码器构造期单一解析表校验（O1）；HEVC（2026-09-16）限
    # yuv420p/yuv420p10le 交付位深档。
    pixel_format: str = ""
    # R7：Topos Alpha 预算超硬上限策略（record/error/continue；UI 授权
    # 开关透传，ADR-C014 C-114）。
    alpha_budget_policy: str = "record"
    include_audio: bool = True
    audio_codec: str = "aac"
    audio_bitrate: int = 192       # kbps
    audio_sample_rate: int = 48000
    audio_channels: int = 2
    # P1-09：显式音频布局（"stereo"/"5.1(side)"/"7.1"…）。设置后是导出声道数
    # 的单一真相（覆盖 audio_channels）；未知布局名在导出入口显式报错。
    audio_channel_layout: str = ""
    # v1.6 M-B8：stems 追加音轨声明（空 = 单音轨行为逐字节不变）。
    # 仅 topos MOV 容器生效；编码器不支持时整体降级单轨并记录警告。
    audio_stems: Tuple[AudioStemExportTrack, ...] = ()
    output_path: str = ""
    selected_clip_ids: Optional[List[str]] = None
    # Resolve-compatible individual-clips delivery controls.  These are plain
    # strings at this boundary so queued jobs can stay schema-lightweight.
    render_mode: str = ExportRenderMode.SINGLE_CLIP.value
    filename_use: str = "timeline"  # "custom" | "timeline" | "source"
    custom_name: str = ""
    file_suffix: str = ""
    unique_filenames: bool = True
    unique_filename_position: str = "suffix"  # "prefix" | "suffix"
    unique_filename_digits: int = 4
    add_source_frame_count: bool = False
    #: Extra frames rendered at both head and tail of each independent clip.
    #: Available source media and the timeline start clamp this value safely.
    frame_handles: int = 0
    # R15/R17（第七轮审计）：图像序列（PNG/TIFF）作业配置——此前编码器
    # 读不到位深/帧号/覆盖策略（正式 TIFF 永远 rgb24、帧号固定 6 位从 0
    # 起、同名旧序列被静默覆盖且取消会误删旧文件）。bit_depth>=16 时
    # 渲染管线走高精度 uint16 路径（真实 16-bit 交付）。
    bit_depth: int = 8  # PNG/TIFF 序列：8 | 16
    image_sequence_digits: int = 6  # 帧号 padding 位数
    image_sequence_start_index: int = 0  # 首帧号（如 1001）
    image_sequence_overwrite: bool = False  # 同名序列覆盖授权（默认拒绝）
    # P10.3: 是否把字幕轨烧录到输出视频（默认 True，符合"导出即烧录"诉求）
    burn_subtitles: bool = True
    # AM-10 阶段 3.4：离线响度交付。
    # loudness_target_lufs：目标 LUFS（如 -23 EBU R128 / -24 ATSC A/85 / -14 流媒体）。
    #   提供时：导出完成后扫描 master PCM 生成 LoudnessReport，并按增益 normalize（含安全限幅）。
    #   None：不进行响度分析/normalize（保持原样导出）。
    # loudness_report_path：报告写入路径（.json + 同名 .txt），None=不写报告文件
    #   （仅当 loudness_target_lufs 提供时才生成报告内容）。
    loudness_target_lufs: Optional[float] = None
    loudness_report_path: Optional[str] = None
    # T-P1-70 步骤 4：AI 导出溯源——发起导出的 AI 计划/任务 ID（None =
    # 非 AI 发起，如 UI/Deliver 直接触发）。随交付 manifest 归档，供审计
    # 回放把交付物关联回发起它的 AI 计划与导出 job。
    ai_plan_id: Optional[str] = None
    ai_job_id: Optional[str] = None

    def validate(self) -> List[str]:
        """
        验证配置有效性。

        Returns:
            错误消息列表，空列表表示配置有效
        """
        errors: List[str] = []

        if self.source == ExportSource.TIMELINE and not self.timeline_id:
            errors.append("Timeline source requires timeline_id")

        if self.range_type == ExportRangeType.CUSTOM:
            # AUD-18405：负值 start_frame 会渲染 -50.. 的前导黑帧。
            if self.start_frame < 0:
                errors.append("start_frame must be >= 0")
            if self.end_frame >= 0 and self.end_frame < self.start_frame:
                errors.append("end_frame must be >= start_frame")

        if self.resolution:
            w, h = self.resolution
            if w <= 0 or h <= 0:
                errors.append("Invalid resolution")

        if self.fps is not None:
            # fps can be a float or a Rational(num, den); normalize before compare.
            fps_value = float(self.fps.to_fps()) if hasattr(self.fps, "to_fps") else float(self.fps)
            if fps_value <= 0:
                errors.append("fps must be positive")

        # R17（第七轮审计）：图像序列位深只支持 8/16；帧号 padding 与
        # 起始号域校验（编码器据此生成 <base>_%0Nd 文件名）。
        if self.codec in (ExportCodec.PNG_SEQUENCE, ExportCodec.TIFF_SEQUENCE):
            if self.bit_depth not in (8, 16):
                errors.append("image sequence bit_depth must be 8 or 16")
            if not 1 <= self.image_sequence_digits <= 10:
                errors.append("image_sequence_digits must be 1..10")
            if self.image_sequence_start_index < 0:
                errors.append("image_sequence_start_index must be >= 0")

        if self.codec == ExportCodec.GIF:
            if not 2 <= int(self.gif_color_count) <= 256:
                errors.append("GIF color count must be between 2 and 256")
            if self.fps is not None:
                fps_value = float(self.fps.to_fps()) if hasattr(self.fps, "to_fps") else float(self.fps)
                if fps_value > 100.0:
                    errors.append("GIF frame rate cannot exceed 100 fps")

        if not self.output_path:
            errors.append("output_path is required")

        if self.render_mode not in {mode.value for mode in ExportRenderMode}:
            errors.append(f"Unknown render_mode: {self.render_mode}")
        if self.filename_use not in {"custom", "timeline", "source"}:
            errors.append(f"Unknown filename_use: {self.filename_use}")
        if self.unique_filename_position not in {"prefix", "suffix"}:
            errors.append("unique_filename_position must be 'prefix' or 'suffix'")
        if not 2 <= self.unique_filename_digits <= 10:
            errors.append("unique_filename_digits must be between 2 and 10")
        if self.frame_handles < 0:
            errors.append("frame_handles cannot be negative")

        # 检查输出路径扩展名与编码器匹配
        if self.output_path:
            ext = os.path.splitext(self.output_path)[1].lower()
            if self.codec in (ExportCodec.PNG_SEQUENCE, ExportCodec.TIFF_SEQUENCE,
                              ExportCodec.TOPOS_IMAGE_SEQUENCE):
                # G14（第六轮审计）：PNG/TIFF 标准序列已有专用编码器
                # （StandardImageSequenceEncoder），不再 fail-fast 拒绝——
                # 旧 AUD-18404 拒绝文本描述的"唯一序列实现"已不成立。
                # 图序列输出到目录（或图像扩展名基路径）
                if not os.path.isdir(self.output_path) and ext not in ('', '.png', '.tiff', '.tif'):
                    errors.append("Image sequence output requires directory or image extension")
                if self.codec == ExportCodec.PNG_SEQUENCE and ext == '.tif':
                    errors.append("PNG sequence output cannot use a .tif path")
                if self.codec == ExportCodec.TIFF_SEQUENCE and ext == '.png':
                    errors.append("TIFF sequence output cannot use a .png path")
            else:
                # 视频文件
                video_extensions = ('.mov', '.mp4', '.mkv', '.avi', '.webm', '.mxf', '.gif')
                if ext not in video_extensions:
                    errors.append(f"Video codec {self.codec} requires video file extension")
                elif self.codec == ExportCodec.GIF and ext != '.gif':
                    errors.append("GIF codec requires a .gif output extension")

        # v1.6 M-B8：stems 追加轨声明校验（fail-fast 于配置层，先于编码器）
        seen_groups = set()
        for stem in self.audio_stems or ():
            if not str(stem.stem_group or "").strip():
                errors.append("audio_stems: stem_group 不能为空")
            if stem.stem_group in seen_groups:
                errors.append(f"audio_stems: stem_group {stem.stem_group!r} 重复")
            seen_groups.add(stem.stem_group)
            if stem.channels not in (1, 2, 6, 8):
                errors.append(
                    f"audio_stems[{stem.stem_group}]: 声道数 {stem.channels}"
                    " 不支持（1/2/6/8）")

        return errors

    def requires_scipy_backend(self, sequence: Optional["TimelineSequence"] = None) -> bool:
        """判断本次导出是否依赖 scipy DSP 后端（EQ / LUFS）。

        RMA-01：导出 preflight 用。两条触发路径——
        (1) 用户显式指定 loudness_target_lufs（响度归一化）；
        (2) 工程内任一音频轨道/Bus 启用了 Strip EQ（sequence 提供）。
        两条任一命中即需要 scipy；scipy 缺失时这些处理会被静默旁路，
        导致产出文件与用户预期不符（无 EQ / 未归一化）。
        """
        if self.loudness_target_lufs is not None:
            return True
        if sequence is not None:
            for track in sequence.audio_tracks:
                eq = getattr(track, "strip_eq", None)
                if eq is not None and getattr(eq, "enabled", False):
                    return True
        return False


# =============================================================================
# 导出 preflight（RMA-01：缺 DSP 后端不得静默产出未处理文件）
# =============================================================================


@dataclass(frozen=True)
class ExportPreflightResult:
    """导出前置检查结果（RMA-01）。

    Attributes:
        ok: 是否允许继续导出（无阻断性缺口）。
        blocking: 阻断性错误列表（后端缺失导致产出不可信时非空）。
        degraded: 降级提示列表（非阻断，但有组件被旁路）。
        blockers: 结构化阻断项（效果或 NestedAudioDiagnostic）；``blocking``
            是其消息视图，机器消费方（AI/Deliver/CLI）读 ``blockers``。
    """
    ok: bool
    blocking: tuple = ()
    degraded: tuple = ()
    blockers: tuple = ()  # Items implement to_dict() for manifest serialization.

    @property
    def blocking_messages(self) -> List[str]:
        return list(self.blocking)

    @property
    def degraded_messages(self) -> List[str]:
        return list(self.degraded)


@dataclass(frozen=True)
class ExportEffectBlocker:
    """T-P1-20：preflight 结构化阻断项（effect ID 级错误码）。

``code`` 词表：``effect_not_exportable`` / ``effect_unknown`` /
``plugin_preview_only`` / ``plugin_identity_<status>`` /
``catalog_unavailable``。禁止通用 "unsupported" 掩盖具体后端缺失。
    """
    effect_type: str
    code: str
    message: str
    clip_id: str = ""
    effect_id: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {
            "effect_type": self.effect_type,
            "code": self.code,
            "message": self.message,
            "clip_id": self.clip_id,
            "effect_id": self.effect_id,
        }


@dataclass(frozen=True)
class ExportOutcome:
    """导出结果的结构化语义（P0-05）：成功 / 用户取消 / 失败 三态显式化。

    此前 ``export_timeline`` 返回裸 bool，异常路径与用户取消统一返回
    False，worker 侧把一切 False 报成 ``EXPORT_CANCELLED``——真正的
    codec/磁盘/渲染失败被掩盖。现在：

    - ``success=True``：导出完成，产物已定稿；
    - ``cancelled=True``（且 success=False）：仅用户主动取消走这里；
    - 其余 ``success=False``：失败，``error_code``/``cause`` 携带结构化
      错误（异常类型链、编码器诊断），UI/job 层不得再映射为取消。

    ``__bool__`` 等价 ``success``——历史 ``if not outcome:`` 布尔调用点
    语义不变（成功为真值）。
    """
    success: bool
    cancelled: bool = False
    error_code: str = ""
    cause: str = ""
    #: P1-02：请求了音频但编码器容器不含音轨（GIF/图片序列；topos
    #: 容器 v1.1 起带单音轨，不再触发）——时间线音频被显式跳过而非
    #: 静默丢弃；作业结果/UI 据此提示。
    audio_omitted: bool = False
    #: W6-02：硬件编码探针失败回退 CPU 的结构化诊断
    #: （encoder_hw_unavailable:<hwaccel>[:<detail>]，空串 = 未发生回退）。
    #: 回退不阻塞导出，成功 outcome 也可携带；作业结果/UI 据此提示。
    hw_fallback_reason: str = ""
    #: T-P0-20：统一导出阶段 telemetry（topos-export-stage-telemetry/1 的
    #: JSON dict：阶段统计、backend、fallback 根因、full_gpu 判定）。
    stage_telemetry: Optional[Dict[str, Any]] = None
    #: T-P1-10：交付路径标记（如 ``topos_direct_planar_cpu_encode``）；
    #: 空串 = 常规渲染管线。诚实标注编码后端（不虚报 GPU）。
    delivery_path: str = ""

    @classmethod
    def ok(
        cls,
        audio_omitted: bool = False,
        hw_fallback_reason: str = "",
    ) -> "ExportOutcome":
        return cls(
            success=True,
            audio_omitted=audio_omitted,
            hw_fallback_reason=hw_fallback_reason,
        )

    @classmethod
    def user_cancelled(cls) -> "ExportOutcome":
        return cls(success=False, cancelled=True)

    @classmethod
    def failed(cls, error_code: str, cause: str) -> "ExportOutcome":
        return cls(success=False, cancelled=False,
                   error_code=error_code, cause=cause)

    def __bool__(self) -> bool:
        return self.success


def preflight_audio_export(
    config: "TimelineExportConfig",
    sequence: Optional["TimelineSequence"] = None,
) -> ExportPreflightResult:
    """导出前检查音频后端能力（RMA-01）。

    当 scipy 缺失且本次导出依赖 EQ/LUFS 时，返回 ok=False 并给出结构化阻断原因，
    调用方应据此阻止导出或提示用户修复，而不是静默产出未处理文件。
    """
    from src.features.edit.core.audio_capabilities import get_audio_capabilities

    blocking: List[str] = []
    degraded: List[str] = []

    # T-P0-31：能力探测惰性化——仅当本次导出真的声明依赖 scipy DSP
    # （loudness_target 或启用 EQ）时才探测。不依赖 EQ/LUFS 的导出此前
    # 也强制 import scipy.signal（冷进程 ~2.8s 死开销，48 帧短任务的
    # FPS 被固定成本淹没）。RMA-01 语义不变：loudness_target 即使
    # include_audio=False 也依旧阻断（用户显式要求不得静默丢弃）。
    if not config.requires_scipy_backend(sequence):
        return ExportPreflightResult(ok=True)
    caps = get_audio_capabilities(check_output_device=False)
    if config.requires_scipy_backend(sequence) and not caps.eq_lufs_available:
        needs = []
        if config.loudness_target_lufs is not None:
            needs.append(f"响度归一化 → {config.loudness_target_lufs} LUFS")
        if sequence is not None:
            eq_tracks = [
                getattr(t, "name", t.track_id) for t in sequence.audio_tracks
                if getattr(getattr(t, "strip_eq", None), "enabled", False)
            ]
            if eq_tracks:
                needs.append("Strip EQ（轨道: %s）" % ", ".join(eq_tracks))
        blocking.append(
            "SciPy DSP 后端不可用，但本次导出依赖：%s。"
            "这些处理会被静默跳过，产出文件将不含 EQ / 不做响度归一化，"
            "不可作为专业交付。请先安装 scipy（pip install scipy）再导出，"
            "或在导出设置中关闭 EQ/响度归一化。"
            % "；".join(needs)
        )
    return ExportPreflightResult(ok=not blocking, blocking=tuple(blocking), degraded=tuple(degraded))


def preflight_video_effects_export(
    sequence: Optional["TimelineSequence"],
) -> ExportPreflightResult:
    """FX-M05: export preflight for video effects (never silently drop).

    Scans every clip's ENABLED effect chain on the timeline. An effect with
    no export backend, or an unsafe plugin identity state, blocks export so
    the deliverable cannot silently omit processing visible in preview.

    T-P1-20：错误码按 effect ID 返回（``ExportEffectBlocker``）；能力判定
    走统一 :func:`resolve_execution_capability`；能力模块不可导入时
    fail-closed（``catalog_unavailable``），不再静默放行。

    Args:
        sequence: the timeline to scan. None → ok (nothing to check).

    Returns:
        ``ExportPreflightResult`` with structured blockers + 消息视图。
    """
    if sequence is None:
        return ExportPreflightResult(ok=True)
    try:
        from src.features.shared.effects import (
            get_effect_catalog, resolve_execution_capability,
        )
    except Exception as exc:  # noqa: BLE001 — 无法验证 = 不可信，阻断而非放行
        message = (f"效果能力目录不可用，无法验证导出后端（{exc}）；"
                   "为避免静默产出缺效果文件已阻断导出。")
        return ExportPreflightResult(
            ok=False,
            blocking=(message,),
            blockers=(ExportEffectBlocker(
                effect_type="", code="catalog_unavailable",
                message=message),
            ),
        )

    catalog = get_effect_catalog()
    # T-P1-22：音频 DSP（strip_eq/limiter/...）由 AudioGraphRenderer 消费、
    # preflight_audio_export 管辖——视频效果 preflight 不得把它们当
    # "不可渲染"阻断（统一契约的视频轴不含音频 DSP 后端）。
    from src.features.edit.core.effect_registry import (
        AUDIO_MIXER_EFFECT_TYPES,
    )

    blockers: List[ExportEffectBlocker] = []
    seen: set[str] = set()
    for track in sequence.all_tracks:
        for clip in track.clips:
            for effect in (getattr(clip, "effects", ()) or ()):
                if not getattr(effect, "is_enabled", True):
                    continue
                effect_type = getattr(effect, "effect_type", "")
                if not effect_type:
                    continue
                if effect_type in AUDIO_MIXER_EFFECT_TYPES:
                    continue
                clip_id = str(getattr(clip, "clip_id", "") or "")
                effect_id = str(getattr(effect, "effect_id", "") or "")
                if effect_type.startswith("plugin:"):
                    from src.features.shared.effects.plugin_identity import (
                        resolve_plugin_status,
                    )
                    report = resolve_plugin_status(
                        getattr(effect, "plugin_identity", None),
                        effect_type,
                        catalog_spec=catalog.get_spec(effect_type),
                    )
                    if report.export_blocked:
                        blockers.append(ExportEffectBlocker(
                            effect_type=effect_type,
                            code=f"plugin_identity_{report.status.value}",
                            message=(
                                f"片段「{clip_id}」效果「{effect_id}」"
                                f"插件状态为 {report.status.value}: "
                                f"{report.detail}"
                            ),
                            clip_id=clip_id, effect_id=effect_id,
                        ))
                if effect_type in seen:
                    continue
                seen.add(effect_type)
                cap = resolve_execution_capability(
                    effect_type,
                    catalog_known=catalog.is_known(effect_type),
                )
                if not cap.exportable:
                    code, reason_msg = cap.delivery_restriction()
                    blocker_code = (
                        "effect_unknown" if cap.unsupported
                        else ("plugin_preview_only" if cap.preview_only
                              else "effect_not_exportable"))
                    blockers.append(ExportEffectBlocker(
                        effect_type=effect_type,
                        code=blocker_code,
                        message=(
                            f"效果「{effect_type}」{reason_msg}（{code}）；"
                            "继续导出会静默丢失该效果。"
                            "请移除/禁用该效果，或安装支持它的导出后端。"
                        ),
                        clip_id=clip_id, effect_id=effect_id,
                    ))
    return ExportPreflightResult(
        ok=not blockers,
        blocking=tuple(b.message for b in blockers),
        degraded=(),
        blockers=tuple(blockers),
    )


def preflight_caption_graphics_export(
    sequence: Optional["TimelineSequence"],
    project: Optional["EditProject"] = None,
) -> ExportPreflightResult:
    """Validate caption-graphic bindings before a video deliverable starts."""
    if sequence is None:
        return ExportPreflightResult(ok=True)
    blocking: list[str] = []
    degraded: list[str] = []
    registry = sequence.get_caption_graphic_template_registry()
    for binding in getattr(sequence, "caption_graphic_bindings", ()):
        cue = sequence.get_subtitle_clip_by_id(binding.caption_cue_id) if binding.caption_cue_id else None
        if cue is None:
            blocking.append(f"字幕图形绑定 {binding.binding_id} 缺少来源 cue {binding.caption_cue_id}")
        clip = sequence.get_clip_by_id(binding.graphic_clip_id)
        if clip is None:
            blocking.append(f"字幕图形绑定 {binding.binding_id} 缺少图形片段 {binding.graphic_clip_id}")
        else:
            track = sequence.get_track_by_id(clip.track_id)
            if track is None or track.track_type != "video" or track.track_role != "graphics":
                blocking.append(f"字幕图形片段 {clip.clip_id} 不在有效 graphics 视频轨道上")
        if registry.get(binding.template_id, binding.template_version) is None:
            blocking.append(
                f"字幕图形绑定 {binding.binding_id} 缺少模板 "
                f"{binding.template_id}@{binding.template_version}"
            )
        if binding.materialization == "detached" and not binding.source_snapshot:
            degraded.append(f"脱离绑定 {binding.binding_id} 没有视觉快照，将使用当前 cue 作为回退")
        if binding.composite_id and project is not None:
            get_composition = getattr(project, "get_composition_by_id", None)
            composition = (
                get_composition(binding.composite_id)
                if callable(get_composition) else None
            )
            if callable(get_composition) and composition is None:
                blocking.append(
                    f"字幕图形绑定 {binding.binding_id} 缺少 Composite 实例 {binding.composite_id}"
                )
            elif composition is not None:
                try:
                    from src.features.composite.core.node_engine import NODE_REGISTRY
                    raw_nodes = (
                        composition.get("nodes", [])
                        if isinstance(composition, dict)
                        else getattr(composition, "nodes", [])
                    )
                    if isinstance(raw_nodes, dict):
                        raw_nodes = list(raw_nodes.values())
                    for raw_node in raw_nodes or []:
                        node_type = (
                            raw_node.get("node_type", "")
                            if isinstance(raw_node, dict)
                            else getattr(raw_node, "node_type", "")
                        )
                        node_cls = NODE_REGISTRY.get(node_type)
                        if node_cls is None:
                            blocking.append(
                                f"字幕图形绑定 {binding.binding_id} 使用未知 Composite 节点 "
                                f"{node_type or '<empty>'}"
                            )
                        elif bool(getattr(node_cls, "IS_PLACEHOLDER", False)):
                            blocking.append(
                                f"字幕图形绑定 {binding.binding_id} 使用未完成 Composite 节点 "
                                f"{node_type}"
                            )
                except Exception as exc:
                    blocking.append(
                        f"字幕图形绑定 {binding.binding_id} 的 Composite 图诊断失败: {exc}"
                    )
    return ExportPreflightResult(
        ok=not blocking, blocking=tuple(blocking), degraded=tuple(degraded)
    )


def preflight_fonts_export(
    sequence: Optional["TimelineSequence"],
) -> ExportPreflightResult:
    """导出前检查字幕字体可用性（SUB-P2-04/11）。

    遍历所有可见字幕轨的启用 cue，按 **解析后** 的 font_family（track-default
    ⊕ cue-override，与渲染/烧录实际使用的字体一致）分组，调用 font_resolver
    的 preflight_fonts。结果按 severity 映射：``error``（无替代字体）→ blocking
    阻断导出；``warning``（有替代）→ degraded 降级提示。无字幕或缺字幕均不阻断
    正常流程；``sequence`` 为 None 时返回 ok。
    """
    if sequence is None:
        return ExportPreflightResult(ok=True)
    try:
        from src.features.edit.core.captions.font_resolver import preflight_fonts
        from src.features.edit.core.captions.model import (
            resolve_style, EMPTY_STYLE, clip_style_base,
        )
    except Exception:
        logger.exception("Failed to import font preflight; skipping")
        return ExportPreflightResult(ok=True)

    # Group clip ids by the resolved font family (matches what the renderer
    # actually rasterizes — not the legacy flat field).
    family_to_clips: dict[str, list[str]] = {}
    try:
        tracks = sequence.subtitle_tracks
    except Exception:
        tracks = ()
    for track in tracks:
        if not getattr(track, "is_enabled", True) or not getattr(track, "is_visible", True):
            continue
        track_default = (
            track.caption_metadata.default_style
            if getattr(track, "caption_metadata", None) is not None
            and getattr(track.caption_metadata, "default_style", None) is not None
            else EMPTY_STYLE
        )
        for clip in getattr(track, "subtitle_clips", ()):
            if not getattr(clip, "is_enabled", True):
                continue
            try:
                family = resolve_style(
                    track_default, getattr(clip, "style_override", None),
                    base=clip_style_base(clip),
                ).get("font_family", "")
            except Exception:
                family = getattr(clip, "font_family", "") or ""
            family_to_clips.setdefault(family or "", []).append(
                getattr(clip, "clip_id", "")
            )
    family_map = {k: tuple(v) for k, v in family_to_clips.items()}
    issues = preflight_fonts(family_map)

    blocking: List[str] = []
    degraded: List[str] = []
    for issue in issues:
        msg = f"{issue.message} Affected cues: {', '.join(issue.affected_clips) or '(none)'}"
        if issue.severity == "error":
            blocking.append(msg)
        else:
            degraded.append(msg)
    return ExportPreflightResult(
        ok=not blocking, blocking=tuple(blocking), degraded=tuple(degraded),
    )


# =============================================================================
# CPU effect implementations (Phase 4 export path).
#
# Pure-numpy equivalents of the GPU shaders, used by the export pipeline
# (which has no GL context). Each handler takes a BGR uint8 frame + a
# params dict (already resolved at the current frame) and returns a new
# BGR uint8 frame. Quality is tuned for export (full-kernel, no preview
# downscaling) rather than real-time playback.
# =============================================================================

_CV2_GAUSSIAN_WARMUP_LOCK = threading.Lock()
_CV2_GAUSSIAN_WARMED_UP: set[tuple[Any, ...]] = set()

def _cpu_blur(frame_bgr_u8, params: dict):
    """CPU counterpart of the Composite Blur node.

    The CPU path is used for export and software preview fallback. It mirrors
    the shared GPU schema (type, anisotropic radii, channel mask and mix) and
    uses OpenCV equivalents for the five variants. ``frame`` is normally BGR
    but BGRA is accepted so alpha-channel selection remains meaningful.
    """
    import cv2
    import numpy as np
    from src.features.shared.effects.effect_pass_runner import _pack_blur_params

    if frame_bgr_u8 is None or getattr(frame_bgr_u8, "ndim", 0) != 3:
        return frame_bgr_u8
    try:
        uniforms = _pack_blur_params(params)
    except (TypeError, ValueError):
        logger.exception("Failed to pack CPU blur parameters")
        return frame_bgr_u8

    blur_type = params.get("blur_type", "box")
    if blur_type == "directional":
        blur_type = "motion"
    if blur_type not in {"box", "gaussian", "bilateral", "motion", "bokeh"}:
        blur_type = "box"

    radius_x = max(0.0, float(uniforms["u_radius_x"]))
    radius_y = max(0.0, float(uniforms["u_radius_y"]))
    if blur_type not in {"box", "gaussian"}:
        radius_x = radius_y = max(0.0, float(uniforms["u_radius"]))
    mix = max(0.0, min(1.0, float(uniforms["u_mix"])))
    if max(radius_x, radius_y) < 0.5 or mix <= 0.001:
        return frame_bgr_u8

    source = frame_bgr_u8
    color = source[:, :, :3]
    max_radius = max(radius_x, radius_y)

    if blur_type in {"box", "gaussian"}:
        kx = max(1, min(511, int(round(radius_x)) * 2 + 1))
        ky = max(1, min(511, int(round(radius_y)) * 2 + 1))
        edge_policy = int(uniforms["u_edge_policy"])
        border_type = {
            0: cv2.BORDER_REPLICATE,
            1: cv2.BORDER_WRAP,
            2: cv2.BORDER_REFLECT,
        }.get(edge_policy, cv2.BORDER_REPLICATE)

        def warm_gaussian_path(image, border: int) -> None:
            """Prime OpenCV's shape-specific Gaussian dispatch once."""
            if blur_type != "gaussian":
                return
            global _CV2_GAUSSIAN_WARMED_UP
            signature = (
                *image.shape, image.dtype.str, kx, ky, int(border),
            )
            if signature in _CV2_GAUSSIAN_WARMED_UP:
                return
            # On this macOS OpenCV build, the first GaussianBlur dispatch for
            # a new image geometry can ignore its border argument. A matching
            # throwaway call initializes that optimized path; cache each
            # geometry/kernel/border combination to avoid per-frame overhead.
            with _CV2_GAUSSIAN_WARMUP_LOCK:
                if signature not in _CV2_GAUSSIAN_WARMED_UP:
                    cv2.GaussianBlur(
                        np.zeros_like(image), (kx, ky),
                        sigmaX=0, sigmaY=0, borderType=border,
                    )
                    _CV2_GAUSSIAN_WARMED_UP.add(signature)

        def blur_with_policy(image):
            if blur_type == "gaussian":
                apply_filter = lambda value, border: cv2.GaussianBlur(
                    value, (kx, ky), sigmaX=0, sigmaY=0,
                    borderType=border,
                )
            else:
                apply_filter = lambda value, border: cv2.blur(
                    value, (kx, ky), borderType=border)
            if edge_policy == 1:
                # OpenCV's box filter rejects BORDER_WRAP. Explicitly wrap
                # the source neighborhood so both box and Gaussian Blur use
                # the same periodic edge contract as the GPU shaders.
                pad_y, pad_x = ky // 2, kx // 2
                pad_width = ((pad_y, pad_y), (pad_x, pad_x))
                if image.ndim == 3:
                    pad_width += ((0, 0),)
                padded = np.pad(image, pad_width, mode="wrap")
                warm_gaussian_path(padded, cv2.BORDER_REPLICATE)
                filtered = apply_filter(padded, cv2.BORDER_REPLICATE)
                height, width = image.shape[:2]
                return filtered[pad_y:pad_y + height,
                                pad_x:pad_x + width]
            warm_gaussian_path(image, border_type)
            return apply_filter(image, border_type)

        blurred_color = blur_with_policy(color)
    elif blur_type == "bilateral":
        diameter = max(3, min(21, int(round(max_radius)) * 2 + 1))
        preserve = max(0.0, min(1.0, float(params.get("edge_preserve", 0.5))))
        sigma_color = max(5.0, 80.0 * (1.0 - preserve))
        blurred_color = cv2.bilateralFilter(
            color, diameter, sigma_color, max(1.0, max_radius),
        )
    elif blur_type == "motion":
        # The shader scales streak length by radius * shutter; mirror that
        # here so CPU export and GPU preview agree on blur strength.
        shutter = max(0.0, min(2.0, float(params.get("shutter", 1.0))))
        size = max(3, min(511, int(round(max_radius * shutter)) * 2 + 1))
        kernel = np.zeros((size, size), dtype=np.float32)
        center = (size - 1) / 2.0
        angle = np.deg2rad(float(params.get("angle", 0.0)))
        dx = np.cos(angle) * center
        dy = np.sin(angle) * center
        cv2.line(
            kernel,
            (int(round(center - dx)), int(round(center - dy))),
            (int(round(center + dx)), int(round(center + dy))),
            1.0,
            1,
        )
        kernel /= max(float(kernel.sum()), 1.0)
        blurred_color = cv2.filter2D(color, -1, kernel)
    else:  # bokeh
        stretch = max(0.25, 1.0 + float(params.get("anamorphic", 0.0)) * 1.5)
        radius_w = max(1, int(round(max_radius * stretch)))
        radius_h = max(1, int(round(max_radius / stretch)))
        width = min(511, radius_w * 2 + 1)
        height = min(511, radius_h * 2 + 1)
        kernel = np.zeros((height, width), dtype=np.float32)
        cx, cy = (width - 1) // 2, (height - 1) // 2
        blades = uniforms.get("u_aperture_blades", 0)
        if blades <= 0:
            cv2.ellipse(kernel, (cx, cy), (radius_w, radius_h), 0, 0, 360, 1.0, -1)
        else:
            points = []
            for i in range(blades):
                angle = 2.0 * np.pi * i / blades - np.pi / 2.0
                points.append((
                    int(round(cx + radius_w * np.cos(angle))),
                    int(round(cy + radius_h * np.sin(angle))),
                ))
            cv2.fillPoly(kernel, [np.asarray(points, dtype=np.int32)], 1.0)
        kernel /= max(float(kernel.sum()), 1.0)
        blurred_color = cv2.filter2D(color, -1, kernel)

    # ParamDef channel names are logical RGB, while the CPU frame is BGR.
    mask = uniforms.get("u_channel_mask", (1.0, 1.0, 1.0, 0.0))
    out = source.copy()
    for logical_index, bgr_index in enumerate((2, 1, 0)):
        if mask[logical_index] > 0.5:
            blended = color[:, :, bgr_index].astype(np.float32)
            filtered = blurred_color[:, :, bgr_index].astype(np.float32)
            out[:, :, bgr_index] = np.clip(
                blended + (filtered - blended) * mix,
                np.iinfo(source.dtype).min if np.issubdtype(source.dtype, np.integer) else -np.inf,
                np.iinfo(source.dtype).max if np.issubdtype(source.dtype, np.integer) else np.inf,
            ).astype(source.dtype)

    if source.shape[2] >= 4 and mask[3] > 0.5:
        alpha = source[:, :, 3]
        if blur_type == "bilateral":
            alpha_blurred = cv2.bilateralFilter(
                alpha, max(3, min(21, int(round(max_radius)) * 2 + 1)),
                80.0, max(1.0, max_radius),
            )
        elif blur_type in {"box", "gaussian"}:
            alpha_blurred = blur_with_policy(alpha)
        else:
            alpha_blurred = cv2.filter2D(alpha, -1, kernel)
        out[:, :, 3] = np.clip(
            alpha.astype(np.float32)
            + (alpha_blurred.astype(np.float32) - alpha) * mix,
            np.iinfo(source.dtype).min if np.issubdtype(source.dtype, np.integer) else -np.inf,
            np.iinfo(source.dtype).max if np.issubdtype(source.dtype, np.integer) else np.inf,
        ).astype(source.dtype)
    return out


def _effect_full_scale(frame: "np.ndarray") -> float:
    """Return the full-scale code value for the frame's dtype (AUD-18400).

    Integer frames normalize by their own range (uint8 → 255, uint16 → 65535);
    float frames are already scene-referred and get no extra scaling.  The
    sharpen/denoise/edge_detect handlers previously hardcoded ``/255`` both
    ways, saturating every uint16 delivery frame to white.
    """
    import numpy as np

    if np.issubdtype(frame.dtype, np.integer):
        return float(np.iinfo(frame.dtype).max)
    return 1.0


def _effect_quantize(
    normalized: "np.ndarray",
    dtype: "np.dtype[Any]",
    full_scale: float,
) -> "np.ndarray":
    """Quantize a normalized effect result back to the input frame's dtype.

    Integer targets clip to the code range and round-trip through
    ``full_scale`` (mirroring the ``_cpu_glow`` reference pattern); float
    targets keep the computed values without an extra clamp so HDR input
    is not squashed.
    """
    import numpy as np

    if np.issubdtype(dtype, np.integer):
        return np.rint(
            np.clip(normalized, 0.0, 1.0) * full_scale,
        ).astype(dtype)
    return normalized.astype(dtype)


def _cpu_sharpen(frame_bgr_u8, params: dict):
    """Apply the timeline's thresholded unsharp mask on the CPU."""
    import cv2
    import numpy as np

    amount = float(params.get("amount", 0.0))
    radius = float(params.get("radius", 1.0))
    if amount < 0.01 or radius < 0.1:
        return frame_bgr_u8
    k = max(1, int(round(radius * 2)) | 1)
    # AUD-18400：按帧 dtype 满刻度归一（uint8/255、uint16/65535；float
    # 不额外缩放），旧的硬编码 /255 往返使高位深导出帧整帧饱和。
    full_scale = _effect_full_scale(frame_bgr_u8)
    source = frame_bgr_u8.astype(np.float32) / np.float32(full_scale)
    blurred = cv2.GaussianBlur(source, (k, k), sigmaX=radius * 0.5)
    diff = source - blurred
    diff_luma = np.sum(
        diff * np.array((0.0722, 0.7152, 0.2126), dtype=np.float32),
        axis=2,
    )
    threshold = max(0.0, float(params.get("threshold", 0.0)))
    if threshold > 0.0:
        t = np.clip(np.abs(diff_luma) / threshold, 0.0, 1.0)
        diff *= (t * t * (3.0 - 2.0 * t))[..., None]

    channels = str(params.get("channels", "all"))
    if channels == "luminance":
        weights = np.array((0.0722, 0.7152, 0.2126), dtype=np.float32)
        result = source + diff_luma[..., None] * amount * weights
    else:
        # "all" also affects alpha in the RGBA shader; timeline export
        # currently passes opaque BGR frames, so all and rgb share RGB math.
        result = source + diff * amount
    return _effect_quantize(result, frame_bgr_u8.dtype, full_scale)


def _cpu_glow(frame_bgr_u8, params: dict):
    """Bloom/glow: bright-pass → blur → screen-blend back."""
    import cv2
    import numpy as np
    threshold = float(params.get("threshold", 0.7))
    radius = float(params.get("radius", 10.0))
    intensity = float(params.get("intensity", 1.0))
    mix = float(params.get("mix", 1.0))
    if radius < 0.5 or intensity < 0.001 or mix < 0.001:
        return frame_bgr_u8
    # Work in the source depth.  The old uint8-only bright pass made an
    # otherwise 10-bit export lose precision whenever a glow was enabled.
    max_value = (
        float(np.iinfo(frame_bgr_u8.dtype).max)
        if np.issubdtype(frame_bgr_u8.dtype, np.integer) else 1.0
    )
    threshold_code = max(0.0, min(1.0, threshold)) * max_value
    bright = np.where(frame_bgr_u8 >= threshold_code, frame_bgr_u8, 0)
    quality = str(params.get("quality", "normal"))
    sample_count = {"fast": 8, "normal": 16, "high": 32}.get(quality, 16)
    k = sample_count * 2 + 1
    bloom = cv2.GaussianBlur(bright, (k, k), sigmaX=radius * 0.5)
    tint = np.array((
        float(params.get("tint_b", 0.7)),
        float(params.get("tint_g", 0.85)),
        float(params.get("tint_r", 1.0)),
    ), dtype=np.float32)
    bloom = np.clip(
        bloom.astype(np.float32) * intensity * tint[None, None, :],
        0.0, max_value,
    )
    # Screen blend in normalized float avoids uint16 intermediate overflow.
    source_f = frame_bgr_u8.astype(np.float32) / max_value
    bloom_f = bloom / max_value
    screen = 1.0 - (1.0 - source_f) * (1.0 - bloom_f)
    screen = np.rint(np.clip(screen, 0.0, 1.0) * max_value).astype(frame_bgr_u8.dtype)
    if mix >= 0.999:
        return screen
    return cv2.addWeighted(frame_bgr_u8, 1.0 - mix, screen, mix, 0)


def _cpu_erode_dilate(frame_bgr_u8, params: dict):
    """Apply the clip morphology filter while preserving its selected channels."""
    import cv2
    import numpy as np

    if frame_bgr_u8 is None or getattr(frame_bgr_u8, "ndim", 0) != 3:
        return frame_bgr_u8
    channels = str(params.get("channels", "alpha"))
    if channels not in {"alpha", "all"}:
        raise ValueError(f"Unsupported morphology channel mode: {channels}")
    operation = str(params.get("operation", "erode"))
    if operation not in {"erode", "dilate"}:
        raise ValueError(f"Unsupported morphology operation: {operation}")
    size = int(params.get("size", 3))
    iterations = int(params.get("iterations", 1))
    if not 1 <= size <= 50 or not 1 <= iterations <= 10:
        raise ValueError("Morphology size/iterations are outside the node schema")
    radius = size // 2
    if radius == 0:
        return frame_bgr_u8
    edge_mode = str(params.get("edge_mode", "extend"))
    if edge_mode not in {"extend", "reflect", "wrap"}:
        raise ValueError(f"Unsupported morphology edge mode: {edge_mode}")

    kernel_size = radius * 2 + 1
    kernel = np.ones((kernel_size, kernel_size), dtype=np.uint8)
    morphology = cv2.erode if operation == "erode" else cv2.dilate
    border_type = {
        "extend": cv2.BORDER_REPLICATE,
        "reflect": cv2.BORDER_REFLECT,
    }.get(edge_mode)
    source = frame_bgr_u8
    alpha_only = channels == "alpha"
    if alpha_only:
        if source.shape[2] < 4:
            # Timeline video is usually opaque BGR, so alpha-only morphology
            # has no channel to change on this delivery path.
            return frame_bgr_u8
        source = source[:, :, 3]
    if edge_mode == "wrap":
        # cv2 morphology does not accept BORDER_WRAP. Padding by the complete
        # repeated-kernel radius gives the cropped region periodic neighbors.
        pad = radius * iterations
        padding = ((pad, pad), (pad, pad)) + ((0, 0),) * (source.ndim - 2)
        source = np.pad(source, padding, mode="wrap")
        border_type = cv2.BORDER_REPLICATE
    result = morphology(
        source, kernel, iterations=iterations, borderType=border_type)
    if edge_mode == "wrap":
        pad = radius * iterations
        height, width = frame_bgr_u8.shape[:2]
        result = result[pad:pad + height, pad:pad + width]
    if alpha_only:
        output = frame_bgr_u8.copy()
        output[:, :, 3] = result
        return output
    return result


def _cpu_edge_detect(frame_bgr_u8, params: dict):
    """Apply the Edge Detect node's Sobel/Laplacian/Prewitt output on CPU.

    The node's Canny option is the shader's double-threshold Sobel variant
    (without hysteresis). Keeping that exact behavior here lets Metal decline
    unsupported variants without dropping the effect during export.
    """
    import cv2
    import numpy as np

    if frame_bgr_u8 is None or getattr(frame_bgr_u8, "ndim", 0) != 3:
        return frame_bgr_u8
    method = str(params.get("method", "sobel"))
    direction = str(params.get("direction", "both"))
    output_mode = str(params.get("output_mode", "edges"))
    channel = str(params.get("channel", "luminance"))
    if method not in {"sobel", "laplacian", "prewitt", "canny"}:
        raise ValueError(f"Unsupported edge method: {method}")
    if direction not in {"both", "horizontal", "vertical"}:
        raise ValueError(f"Unsupported edge direction: {direction}")
    if output_mode not in {"edges", "gradient", "direction"}:
        raise ValueError(f"Unsupported edge output mode: {output_mode}")
    if channel not in {"luminance", "r", "g", "b", "a"}:
        raise ValueError(f"Unsupported edge channel: {channel}")

    # GPU uniforms are IEEE-754 float32. Quantize controls once here so the
    # CPU fallback follows the same equality/step boundary at thresholds.
    threshold = float(np.float32(params.get("threshold", 0.1)))
    threshold_high = float(np.float32(params.get("threshold_high", 0.3)))
    output_strength = float(np.float32(params.get("output_strength", 1.0)))
    kernel_size = int(params.get("kernel_size", 3))
    preblur = float(np.float32(params.get("preblur", 0.0)))
    if not all(np.isfinite(v) for v in (
            threshold, threshold_high, output_strength, preblur)):
        raise ValueError("Edge Detect parameters must be finite")
    if kernel_size not in {3, 5, 7} or preblur < 0.0:
        raise ValueError("Edge Detect kernel/preblur is outside the node schema")

    # AUD-18400：按帧 dtype 满刻度归一（uint8/255、uint16/65535；float
    # 不额外缩放），旧的硬编码 /255 使高位深导出帧的梯度整体坍缩。
    full_scale = _effect_full_scale(frame_bgr_u8)
    source = frame_bgr_u8.astype(np.float32) / np.float32(full_scale)
    if channel == "luminance":
        scalar = (source[..., 2] * 0.2126 + source[..., 1] * 0.7152
                  + source[..., 0] * 0.0722)
    elif channel == "r":
        scalar = source[..., 2]
    elif channel == "g":
        scalar = source[..., 1]
    elif channel == "b":
        scalar = source[..., 0]
    else:
        scalar = (source[..., 3] if source.shape[2] >= 4
                  else np.ones(source.shape[:2], dtype=np.float32))

    height, width = scalar.shape

    def sample_shifted(image, dx: float, dy: float):
        map_x, map_y = np.meshgrid(
            np.arange(width, dtype=np.float32) + dx,
            np.arange(height, dtype=np.float32) + dy,
        )
        return cv2.remap(
            image, map_x, map_y, cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REPLICATE,
        )

    if preblur > 0.0:
        blurred = scalar * 4.0
        blurred += sample_shifted(scalar, preblur, 0.0) * 2.0
        blurred += sample_shifted(scalar, -preblur, 0.0) * 2.0
        blurred += sample_shifted(scalar, 0.0, preblur) * 2.0
        blurred += sample_shifted(scalar, 0.0, -preblur) * 2.0
        blurred += sample_shifted(scalar, preblur, preblur)
        blurred += sample_shifted(scalar, -preblur, -preblur)
        blurred += sample_shifted(scalar, preblur, -preblur)
        blurred += sample_shifted(scalar, -preblur, preblur)
        scalar = blurred / 16.0

    padded_3 = np.pad(scalar, 1, mode="edge")
    tl = padded_3[:-2, :-2]
    tm = padded_3[:-2, 1:-1]
    tr = padded_3[:-2, 2:]
    ml = padded_3[1:-1, :-2]
    center = padded_3[1:-1, 1:-1]
    mr = padded_3[1:-1, 2:]
    bl = padded_3[2:, :-2]
    bm = padded_3[2:, 1:-1]
    br = padded_3[2:, 2:]
    use_laplacian = method == "laplacian"
    use_sobel = method in {"sobel", "canny"}
    if kernel_size == 5 and use_sobel:
        padded_5 = np.pad(scalar, 2, mode="edge")
        values = [
            padded_5[2 + y:2 + y + height, 2 + x:2 + x + width]
            for y in range(-2, 3) for x in range(-2, 3)
        ]
        gx = (-2.0 * values[0] - values[1] + values[3] + 2.0 * values[4]
              - 4.0 * values[5] - 2.0 * values[6] + 2.0 * values[8]
              + 4.0 * values[9] - 6.0 * values[10] - 3.0 * values[11]
              + 3.0 * values[13] + 6.0 * values[14]
              - 4.0 * values[15] - 2.0 * values[16] + 2.0 * values[18]
              + 4.0 * values[19] - 2.0 * values[20] - values[21]
              + values[23] + 2.0 * values[24]) / 12.0
        gy = (-2.0 * values[0] - 4.0 * values[5] - 6.0 * values[10]
              - 4.0 * values[15] - 2.0 * values[20]
              - values[1] - 2.0 * values[6] - 3.0 * values[11]
              - 2.0 * values[16] - values[21]
              + values[3] + 2.0 * values[8] + 3.0 * values[13]
              + 2.0 * values[18] + values[23]
              + 2.0 * values[4] + 4.0 * values[9] + 6.0 * values[14]
              + 4.0 * values[19] + 2.0 * values[24]) / 12.0
    elif method == "prewitt" and kernel_size != 5:
        gx = -tl + tr - ml + mr - bl + br
        gy = -tl - tm - tr + bl + bm + br
    else:
        # Sobel 3x3; the catalog shader also uses this for Canny and the
        # kernel_size=5 Prewitt branch.
        gx = -tl + tr - 2.0 * ml + 2.0 * mr - bl + br
        gy = -tl - 2.0 * tm - tr + bl + 2.0 * bm + br

    if use_laplacian:
        if kernel_size == 5:
            lap = (tm + bm + ml + mr + 0.5 * (tl + tr + bl + br)
                   - 8.0 * center)
        else:
            lap = tm + bm + ml + mr - 4.0 * center
        magnitude = np.abs(lap)
    else:
        if direction == "horizontal":
            magnitude = np.abs(gx)
        elif direction == "vertical":
            magnitude = np.abs(gy)
        else:
            magnitude = np.hypot(gx, gy)
    magnitude = magnitude / 4.0

    if output_mode == "edges":
        # A one-ULP Metal FMA difference must not flip a Sobel pixel that is
        # exactly on the user threshold. The shared tolerance is far below
        # the control's 0.01 step and keeps CPU/Metal binary output stable.
        output = (magnitude + np.float32(1.0e-6) >= threshold).astype(np.float32)
        if method == "canny":
            output = np.where(
                magnitude < threshold, 0.0,
                np.where(magnitude < threshold_high, 0.5, 1.0),
            )
        output *= output_strength
    elif output_mode == "gradient":
        output = magnitude * output_strength
    elif use_laplacian:
        output = np.full_like(magnitude, 0.5)
    else:
        angle = np.arctan2(gy, gx)
        angle = np.where((gx * gx + gy * gy) > 1.0e-12, angle, 0.0)
        output = (angle + np.float32(3.14159)) / np.float32(2.0 * 3.14159)
    if bool(params.get("invert", False)):
        output = 1.0 - output

    gray = _effect_quantize(output, frame_bgr_u8.dtype, full_scale)
    result = np.repeat(gray[:, :, None], frame_bgr_u8.shape[2], axis=2)
    if frame_bgr_u8.shape[2] >= 4:
        result[:, :, 3] = frame_bgr_u8[:, :, 3]
    return result


def _cpu_denoise(
    frame_bgr_u8: "np.ndarray",
    params: dict[str, Any],
) -> "np.ndarray":
    """Apply the timeline Denoise node's bilateral filter on CPU.

    This mirrors the GLSL node's spatial/range weights and its luminance-only
    and chroma-only reconstruction rules, providing a complete fallback for
    non-Metal export backends and unsupported frame types.
    """
    import numpy as np

    if frame_bgr_u8 is None or getattr(frame_bgr_u8, "ndim", 0) != 3:
        return frame_bgr_u8
    if frame_bgr_u8.shape[2] not in (3, 4):
        raise ValueError("Denoise expects BGR or BGRA input")
    raw_strength = float(params.get("strength", 0.5))
    raw_radius = float(params.get("radius", 3))
    if (not np.isfinite(raw_strength) or not np.isfinite(raw_radius)
            or not raw_radius.is_integer()
            or not 0.0 <= raw_strength <= 1.0
            or not 1 <= int(raw_radius) <= 10):
        raise ValueError("Denoise strength/radius is outside the node schema")
    strength = float(np.float32(raw_strength))
    # T-P1-21 batch 2: quality tier → effective kernel radius, same
    # contract as the GLSL path (effect_tiers.effective_denoise_radius).
    radius = effective_denoise_radius(int(raw_radius),
                                      str(params.get("quality", "normal")))
    if strength < 0.001:
        return frame_bgr_u8

    height, width = frame_bgr_u8.shape[:2]
    # AUD-18400：按帧 dtype 满刻度归一（uint8/255、uint16/65535；float
    # 不额外缩放），旧的硬编码 /255 往返使高位深导出帧整帧饱和。
    full_scale = _effect_full_scale(frame_bgr_u8)
    center = frame_bgr_u8[:, :, (2, 1, 0)].astype(np.float32) / np.float32(full_scale)
    alpha = (frame_bgr_u8[:, :, 3].astype(np.float32) / np.float32(full_scale)
             if frame_bgr_u8.shape[2] == 4 else None)
    luminance_only = bool(params.get("luminance_only", False))
    chroma_only = bool(params.get("chroma_only", False))
    luma_coefficients = np.asarray((0.2126, 0.7152, 0.0722), dtype=np.float32)
    center_luma = np.sum(center * luma_coefficients, axis=2)
    center_chroma = center - center_luma[:, :, None]

    sigma_spatial = max(float(radius) * 0.4, 1.0)
    sigma_range = max(0.1, 0.15 * (1.0 - strength * 0.5))
    radius = min(radius, 10)
    padded = np.pad(
        center, ((radius, radius), (radius, radius), (0, 0)), mode="edge")
    padded_alpha = (np.pad(alpha, ((radius, radius), (radius, radius)),
                           mode="edge") if alpha is not None else None)
    total = np.zeros_like(center)
    total_alpha = np.zeros((height, width), dtype=np.float32) if alpha is not None else None
    weight_sum = np.zeros((height, width), dtype=np.float32)
    denominator = np.float32(2.0 * sigma_range * sigma_range)
    for dy in range(-radius, radius + 1):
        y0 = radius + dy
        for dx in range(-radius, radius + 1):
            x0 = radius + dx
            sample = padded[y0:y0 + height, x0:x0 + width]
            if luminance_only:
                sample_luma = np.sum(sample * luma_coefficients, axis=2)
                difference = center_luma - sample_luma
                range_diff_sq = difference * difference
            elif chroma_only:
                sample_luma = np.sum(sample * luma_coefficients, axis=2)
                sample_chroma = sample - sample_luma[:, :, None]
                difference = center_chroma - sample_chroma
                range_diff_sq = np.sum(difference * difference, axis=2)
            else:
                difference = center - sample
                range_diff_sq = np.sum(difference * difference, axis=2)
            spatial_weight = np.float32(np.exp(
                -(dx * dx + dy * dy) / (2.0 * sigma_spatial * sigma_spatial)))
            range_weight = np.exp(-range_diff_sq / denominator)
            weight = spatial_weight * range_weight
            total += sample * weight[:, :, None]
            if total_alpha is not None and padded_alpha is not None:
                total_alpha += padded_alpha[y0:y0 + height, x0:x0 + width] * weight
            weight_sum += weight

    filtered = total / np.maximum(weight_sum[:, :, None], np.float32(0.0001))
    result = center * np.float32(1.0 - strength) + filtered * np.float32(strength)
    if luminance_only:
        filtered_luma = np.sum(filtered * luma_coefficients, axis=2)
        result_luma = (center_luma * np.float32(1.0 - strength)
                       + filtered_luma * np.float32(strength))
        result = result_luma[:, :, None] + center_chroma
    elif chroma_only:
        filtered_luma = np.sum(filtered * luma_coefficients, axis=2)
        filtered_chroma = filtered - filtered_luma[:, :, None]
        result_chroma = (center_chroma * np.float32(1.0 - strength)
                         + filtered_chroma * np.float32(strength))
        result = center_luma[:, :, None] + result_chroma

    bgr = _effect_quantize(result[:, :, ::-1], frame_bgr_u8.dtype, full_scale)
    if alpha is None:
        return bgr
    if luminance_only or chroma_only or total_alpha is None:
        out_alpha = frame_bgr_u8[:, :, 3]
    else:
        filtered_alpha = total_alpha / np.maximum(weight_sum, np.float32(0.0001))
        out_alpha = _effect_quantize(
            alpha * np.float32(1.0 - strength)
            + filtered_alpha * np.float32(strength),
            frame_bgr_u8.dtype, full_scale,
        )
    return np.dstack((bgr, out_alpha))


def _cpu_defocus(frame_bgr_u8, params: dict):
    """CPU reference for the timeline Defocus node's focal-radius shader.

    ``separable`` mirrors the node's horizontal, variable-sigma Gaussian pass
    and all three edge policies. ``mipmap`` builds a clamped Gaussian pyramid
    and samples its fractional LOD, matching the shader's preview intent on
    export backends without mipmapped source textures.
    """
    import cv2
    import numpy as np

    if frame_bgr_u8 is None or getattr(frame_bgr_u8, "ndim", 0) != 3:
        return frame_bgr_u8
    if frame_bgr_u8.shape[2] not in (3, 4):
        raise ValueError("Defocus expects BGR or BGRA input")
    dtype = frame_bgr_u8.dtype
    if not np.issubdtype(dtype, np.integer):
        raise ValueError("Defocus CPU reference expects integer image data")
    max_code = float(np.iinfo(dtype).max)
    try:
        mode = str(params.get("mode", "separable"))
        focal_x = float(params.get("focal_x", 0.5))
        focal_y = float(params.get("focal_y", 0.5))
        focal_size = float(params.get("focal_size", 0.3))
        max_radius = float(params.get("max_radius", 20.0))
        edge_policy = str(params.get("edge_policy", "clamp"))
        process_alpha = bool(params.get("process_alpha", True))
    except (TypeError, ValueError) as exc:
        raise ValueError("Defocus parameters are invalid") from exc
    if (mode not in {"separable", "mipmap"}
            or edge_policy not in {"clamp", "wrap", "mirror"}
            or not all(np.isfinite(v) for v in
                       (focal_x, focal_y, focal_size, max_radius))
            or not 0.0 <= focal_x <= 1.0
            or not 0.0 <= focal_y <= 1.0
            or not 0.01 <= focal_size <= 1.0
            or not 0.0 <= max_radius <= 100.0):
        raise ValueError("Defocus parameters are outside the node schema")
    if max_radius < 0.1:
        return frame_bgr_u8

    height, width = frame_bgr_u8.shape[:2]
    y, x = np.mgrid[0:height, 0:width].astype(np.float32)
    u = (x + np.float32(0.5)) / np.float32(width)
    v = (y + np.float32(0.5)) / np.float32(height)
    dx = np.abs(u - np.float32(focal_x))
    dy = np.abs(v - np.float32(focal_y))
    distance = np.sqrt(dx * dx + dy * dy)
    dist_factor = distance / np.maximum(np.float32(focal_size), np.float32(0.01))
    local_radius = np.float32(max_radius) * np.minimum(dist_factor * dist_factor, 1.0)
    active = local_radius >= np.float32(0.5)

    rgb = frame_bgr_u8[:, :, :3][:, :, ::-1].astype(np.float32) / max_code
    has_alpha = frame_bgr_u8.shape[2] == 4
    alpha = (frame_bgr_u8[:, :, 3:4].astype(np.float32) / max_code
             if has_alpha else None)
    channels = np.concatenate((rgb, alpha), axis=2) if process_alpha and alpha is not None else rgb

    if mode == "separable":
        pad_mode = {"clamp": "edge", "wrap": "wrap", "mirror": "symmetric"}[edge_policy]
        padded = np.pad(channels, ((0, 0), (15, 15), (0, 0)), mode=pad_mode)
        sigma = np.maximum(local_radius * np.float32(0.5), np.float32(0.1))
        denominator = np.float32(2.0) * sigma * sigma
        total = np.zeros_like(channels)
        weight_sum = np.zeros((height, width), dtype=np.float32)
        for offset in range(-15, 16):
            sample = padded[:, 15 + offset:15 + offset + width]
            weight = np.exp(np.float32(-(offset * offset)) / denominator)
            total += sample * weight[:, :, None]
            weight_sum += weight
        filtered = total / np.maximum(weight_sum[:, :, None], np.float32(1.0e-4))
    else:
        # textureLod uses the normalized pixel center and a trilinear blend
        # between adjacent mip levels. Recreate that sampling on CPU with a
        # lazily-built, clamped pyramid.
        lod = np.log2(local_radius + np.float32(1.0))
        pyramid = [channels]
        while (len(pyramid) < 12
               and min(pyramid[-1].shape[:2]) > 1
               and len(pyramid) <= int(np.ceil(float(lod.max())))):
            prev = pyramid[-1]
            padded = np.pad(
                prev,
                ((0, prev.shape[0] % 2), (0, prev.shape[1] % 2), (0, 0)),
                mode="edge",
            )
            next_level = (
                padded[0::2, 0::2] + padded[0::2, 1::2]
                + padded[1::2, 0::2] + padded[1::2, 1::2]
            ) * np.float32(0.25)
            pyramid.append(
                np.rint(np.clip(next_level, 0.0, 1.0) * max_code)
                / np.float32(max_code))
        lod = np.minimum(lod, float(len(pyramid) - 1))
        lod_lo = np.floor(lod).astype(np.int32)
        lod_hi = np.minimum(lod_lo + 1, len(pyramid) - 1)
        frac = (lod - lod_lo).astype(np.float32)
        map_x = np.empty((height, width), dtype=np.float32)
        map_y = np.empty((height, width), dtype=np.float32)
        filtered = np.zeros_like(channels)
        for level, mip in enumerate(pyramid):
            map_x[:] = (x + 0.5) * np.float32(mip.shape[1] / width) - 0.5
            map_y[:] = (y + 0.5) * np.float32(mip.shape[0] / height) - 0.5
            sampled = cv2.remap(
                mip, map_x, map_y, cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REPLICATE,
            )
            low_mask = lod_lo == level
            high_mask = lod_hi == level
            if np.any(low_mask):
                filtered[low_mask] += sampled[low_mask] * (1.0 - frac[low_mask, None])
            if np.any(high_mask):
                filtered[high_mask] += sampled[high_mask] * frac[high_mask, None]

    output = channels.copy()
    output[active] = filtered[active]
    out_rgb = np.rint(np.clip(output[:, :, :3][:, :, ::-1], 0.0, 1.0) * max_code).astype(dtype)
    if not has_alpha:
        return out_rgb
    out_alpha = (np.rint(np.clip(output[:, :, 3], 0.0, 1.0) * max_code).astype(dtype)
                 if process_alpha else frame_bgr_u8[:, :, 3])
    return np.dstack((out_rgb, out_alpha))


# Registry: effect_type → CPU handler. Effects not listed here passthrough
# (logged as a warning) so export never crashes on an unsupported filter.
_CPU_EFFECT_HANDLERS = {
    "blur": _cpu_blur,
    "sharpen": _cpu_sharpen,
    "glow": _cpu_glow,
    "erode_dilate": _cpu_erode_dilate,
    "edge_detect": _cpu_edge_detect,
    "denoise": _cpu_denoise,
    "defocus": _cpu_defocus,
}


# =============================================================================
# TimelineExporter
# =============================================================================


def expand_sequence_audio_for_render(
    sequence: "TimelineSequence",
    sequence_resolver: Optional["SequenceResolver"],
    project: Optional["EditProject"],
    *,
    strict: bool = False,
    diagnostics: Optional[list["NestedAudioDiagnostic"]] = None,
    frame_range: Optional[tuple[int, int]] = None,
) -> "TimelineSequence":
    """音频渲染用的展开序列：嵌套子序列音频轨平移进父坐标（P1-02）。

    无嵌套贡献时原样返回 ``sequence``（零开销路径）；有则返回
    ``audio_tracks`` 替换后的副本（其余字段/master 不变）。实时播放的
    EditPage 音频同步走同一个 ``flatten_nested_audio_tracks``。
    """
    from src.features.edit.core.nested_sequence import flatten_nested_audio_tracks

    resolver = sequence_resolver if callable(sequence_resolver) else None
    asset_lookup = (
        (lambda aid: project.assets.get(aid))
        if project is not None and getattr(project, "assets", None) is not None
        else None
    )
    expanded = flatten_nested_audio_tracks(
        sequence, resolver, asset_lookup, strict=strict,
        diagnostics=diagnostics, frame_range=frame_range,
    )
    if expanded == tuple(sequence.audio_tracks or ()):
        return sequence
    from dataclasses import replace as _dc_replace

    return _dc_replace(sequence, audio_tracks=expanded)


class _PassthroughFormatSpec:
    """单条平面直通目标的格式契约（fail-closed 门的数据化形态）。

    Attributes:
        codec: 编码器 codec 名（``encoder_config.codec``）。
        target_pix_fmt: 编码器像素格式（``encoder_config.pix_fmt``）。
        source_pix_fmt: 源平面帧像素格式（帧级与元数据级双查）。
        source_bit_depth: 源位深（同时约束 plane_infos 与 dtype）。
        chroma_subsampling: ``"422"``（色度全高）或 ``"420"``（色度半高）。
        requires_high_precision: 是否要求 ``_use_high_precision_export``
            （TOPOS 10bit 导出语义；FFmpeg 8bit 直通不要求）。
        untagged_siting_codecs: 源未标注 chroma siting 时可按 left(0)
            缺省的 codec 集合（与既有 ProRes 豁免同构：h264/hevc 同属
            MPEG 谱系 4:2:0，untagged 约定与 FFmpeg 编码器隐式 siting 0
            一致，且解码后的采样网格与目标 codec 无关，故跨编码器互认）。
        scalar_delivery: 是否支持 8-bit 标量交付（YUV 域 resize 与
            opacity/fade 围绕 limited-range 中性点的线性缩放）。仅 4:2:0
            SDR 交付验证过；422 规格保持 False。
        downconvert_source_bit_depth: 非 0 时源平面在 YUV 域按右移降位到
            该位深（limited-range 锚点恰好整除：64→16、940→235、960→240，
            舍入为 round-half-up），交付 dtype/pix_fmt/plane_infos 随之
            改写为目标域。仅 ``scalar_delivery`` 同为 True 的规格允许。
    """

    __slots__ = (
        "codec", "target_pix_fmt", "source_pix_fmt", "source_bit_depth",
        "chroma_subsampling", "requires_high_precision",
        "untagged_siting_codecs", "scalar_delivery",
        "downconvert_source_bit_depth",
    )

    def __init__(
        self,
        *,
        codec: str,
        target_pix_fmt: str,
        source_pix_fmt: str,
        source_bit_depth: int,
        chroma_subsampling: str,
        requires_high_precision: bool,
        untagged_siting_codecs: tuple = (),
        scalar_delivery: bool = False,
        downconvert_source_bit_depth: int = 0,
    ) -> None:
        self.codec = codec
        self.target_pix_fmt = target_pix_fmt
        self.source_pix_fmt = source_pix_fmt
        self.source_bit_depth = source_bit_depth
        self.chroma_subsampling = chroma_subsampling
        self.requires_high_precision = requires_high_precision
        self.untagged_siting_codecs = tuple(untagged_siting_codecs)
        self.scalar_delivery = bool(scalar_delivery)
        self.downconvert_source_bit_depth = int(downconvert_source_bit_depth)


#: 平面直通白名单。TOPOS yuv422p10le 是既有已验证路径；h264/hevc
#: yuv420p 是 2026-09-15 导出提速计划加入的 FFmpeg 硬编直通（解码平面
#: → 编码器同格式直填，消除 BGR 往返；hevc 8-bit 档 2026-09-16 扩展，
#: AMF/NVENC/VideoToolbox 的 hevc+yuv420p 输入与 h264 同构，libx265
#: 由 profile=main 表达 8-bit 档）。untagged 豁免跨 h264/hevc 互认
#: （2026-09-17：两者 untagged 4:2:0 约定同为 left(0)，采样网格与目标
#: codec 无关）。10-bit 源 → 8-bit 档走 YUV 域右移降位（>>2，limited
#: range 锚点精确整除），不引入 RGB 往返。扩展此表必须同时：
#: 1. 确认 `_encode_frame` 白名单与 `_convert_planar_frame` 对该格式零转换；
#: 2. 在 `tests/edit/test_timeline_export_planar_passthrough.py` 加正/负用例；
#: 3. 补端到端导出回读验证（色彩/帧数）。
_PASSTHROUGH_FORMAT_SPECS = (
    _PassthroughFormatSpec(
        codec="topos",
        target_pix_fmt="yuv422p10le",
        source_pix_fmt="yuv422p10le",
        source_bit_depth=10,
        chroma_subsampling="422",
        requires_high_precision=True,
    ),
    _PassthroughFormatSpec(
        codec="h264",
        target_pix_fmt="yuv420p",
        source_pix_fmt="yuv420p",
        source_bit_depth=8,
        chroma_subsampling="420",
        requires_high_precision=False,
        untagged_siting_codecs=("h264", "hevc"),
        scalar_delivery=True,
    ),
    # Topos 422 10-bit 中间片 → h264 8-bit 4:2:0 交付：YUV 域一次明确
    # 转换（>>2 降位 + 色度半高），替代旧链 planar→BGR float→swscale
    # 往返（2026-09-17 实测该往返 ~70ms/帧，主导 Topos 源导出全档 ~11fps）。
    _PassthroughFormatSpec(
        codec="h264",
        target_pix_fmt="yuv420p",
        source_pix_fmt="yuv422p10le",
        source_bit_depth=10,
        chroma_subsampling="422",
        requires_high_precision=False,
        scalar_delivery=True,
        downconvert_source_bit_depth=8,
    ),
    _PassthroughFormatSpec(
        codec="hevc",
        target_pix_fmt="yuv420p",
        source_pix_fmt="yuv420p",
        source_bit_depth=8,
        chroma_subsampling="420",
        requires_high_precision=False,
        untagged_siting_codecs=("h264", "hevc"),
        scalar_delivery=True,
    ),
    # 同 h264：Topos 422 10-bit 源的 8-bit 4:2:0 交付档。
    _PassthroughFormatSpec(
        codec="hevc",
        target_pix_fmt="yuv420p",
        source_pix_fmt="yuv422p10le",
        source_bit_depth=10,
        chroma_subsampling="422",
        requires_high_precision=False,
        scalar_delivery=True,
        downconvert_source_bit_depth=8,
    ),
    _PassthroughFormatSpec(
        codec="hevc",
        target_pix_fmt="yuv420p",
        source_pix_fmt="yuv420p10le",
        source_bit_depth=10,
        chroma_subsampling="420",
        requires_high_precision=False,
        untagged_siting_codecs=("h264", "hevc"),
        scalar_delivery=True,
        downconvert_source_bit_depth=8,
    ),
    # Topos 422 10-bit 源 → ProRes 422 10-bit（standard/hq）：同格式平面
    # 直填（编码器 `_convert_planar_frame` 零转换路径），无 RGB 往返。
    _PassthroughFormatSpec(
        codec="prores",
        target_pix_fmt="yuv422p10le",
        source_pix_fmt="yuv422p10le",
        source_bit_depth=10,
        chroma_subsampling="422",
        requires_high_precision=True,
        untagged_siting_codecs=("prores", "h264", "hevc"),
    ),
    # Topos 422 10-bit 源 → 8-bit 422 交付（prores proxy/lt、dnxhr lb/sq/hq）：
    # YUV 域 >>2 降位，色度几何不变。
    _PassthroughFormatSpec(
        codec="prores",
        target_pix_fmt="yuv422p",
        source_pix_fmt="yuv422p10le",
        source_bit_depth=10,
        chroma_subsampling="422",
        requires_high_precision=False,
        downconvert_source_bit_depth=8,
    ),
    _PassthroughFormatSpec(
        codec="dnxhr",
        target_pix_fmt="yuv422p",
        source_pix_fmt="yuv422p10le",
        source_bit_depth=10,
        chroma_subsampling="422",
        requires_high_precision=False,
        downconvert_source_bit_depth=8,
    ),
    # DNxHR HQX 是 10-bit 422：同格式平面直填。
    _PassthroughFormatSpec(
        codec="dnxhr",
        target_pix_fmt="yuv422p10le",
        source_pix_fmt="yuv422p10le",
        source_bit_depth=10,
        chroma_subsampling="422",
        requires_high_precision=True,
    ),
    # ── 10-bit 4:2:0 源（4K_H265/手机 HDR/HEVC 中间片常见）──
    # h264 8-bit 420 交付：>>2 降位，色度几何不变（420→420 无重采样）。
    _PassthroughFormatSpec(
        codec="h264",
        target_pix_fmt="yuv420p",
        source_pix_fmt="yuv420p10le",
        source_bit_depth=10,
        chroma_subsampling="420",
        requires_high_precision=False,
        untagged_siting_codecs=("h264", "hevc"),
        scalar_delivery=True,
        downconvert_source_bit_depth=8,
    ),
    # ProRes 422 10-bit（standard/hq）：色度 420→422 垂直上采样，位深不变。
    _PassthroughFormatSpec(
        codec="prores",
        target_pix_fmt="yuv422p10le",
        source_pix_fmt="yuv420p10le",
        source_bit_depth=10,
        chroma_subsampling="420",
        requires_high_precision=True,
        untagged_siting_codecs=("h264", "hevc"),
    ),
    # ProRes/DNxHR 8-bit 422：降位 + 色度上采样，一次 YUV 域完成。
    _PassthroughFormatSpec(
        codec="prores",
        target_pix_fmt="yuv422p",
        source_pix_fmt="yuv420p10le",
        source_bit_depth=10,
        chroma_subsampling="420",
        requires_high_precision=False,
        untagged_siting_codecs=("h264", "hevc"),
        downconvert_source_bit_depth=8,
    ),
    _PassthroughFormatSpec(
        codec="dnxhr",
        target_pix_fmt="yuv422p",
        source_pix_fmt="yuv420p10le",
        source_bit_depth=10,
        chroma_subsampling="420",
        requires_high_precision=False,
        untagged_siting_codecs=("h264", "hevc"),
        downconvert_source_bit_depth=8,
    ),
    # DNxHR HQX 10-bit 422：色度上采样，位深不变。
    _PassthroughFormatSpec(
        codec="dnxhr",
        target_pix_fmt="yuv422p10le",
        source_pix_fmt="yuv420p10le",
        source_bit_depth=10,
        chroma_subsampling="420",
        requires_high_precision=True,
        untagged_siting_codecs=("h264", "hevc"),
    ),
    # Topos 422 中间片：色度上采样到容器 422 契约，位深不变。
    _PassthroughFormatSpec(
        codec="topos",
        target_pix_fmt="yuv422p10le",
        source_pix_fmt="yuv420p10le",
        source_bit_depth=10,
        chroma_subsampling="420",
        requires_high_precision=True,
        untagged_siting_codecs=("h264", "hevc"),
    ),
)


#: OpenGL 导出 GPU 效果链放行的 dispatch_key 集合（pass2.5 经
#: EffectPassRunner 执行；与 ``_gpu_metal_effect_instance_uniforms``
#: 支持的效果类型一一对应，CPU 处理器表 `_CPU_EFFECT_HANDLERS` 亦覆盖
#: 同集——拒绝路径的 CPU 回退语义完整）。扩集必须同时：Metal 校验器
#: 支持该类型、runner 有执行路径、CPU 有回退处理器。
_GPU_OPENGL_EFFECT_DISPATCH_KEYS = frozenset({
    "blur", "sharpen", "glow", "erode_dilate",
    "edge_detect", "denoise", "defocus",
})

#: T-P2-20/W06：声明 @export_support opengl 的插件（catalog 落为
#: plugin_capabilities 的 "export_opengl"）经 DCTL_PROCESS 单通道着色器
#: 进入 GL 导出链。 runners 自行打包插件 uniforms，不走内置参数门。
_PLUGIN_OPENGL_DISPATCH_KEYS = frozenset({"plugin"})


def _spec_allows_opengl_export(spec: Any) -> bool:
    """GL 导出链放行判定：内置白名单或声明 export_opengl 的插件规格。"""
    dispatch_key = str(getattr(spec, "dispatch_key", ""))
    if dispatch_key in _GPU_OPENGL_EFFECT_DISPATCH_KEYS:
        return True
    if dispatch_key.startswith("plugin:"):
        return "export_opengl" in tuple(
            getattr(spec, "plugin_capabilities", ()) or ())
    return False


def _passthrough_specs_for(encoder_config: Any) -> tuple:
    """按编码器配置列出直通规格候选；白名单外返回空元组（fail-closed）。

    同一 (codec, pix_fmt) 可有多条源域规格（如 hevc yuv420p 同时接受
    8-bit 同格式与 10-bit 降位源）；帧级选择由调用方按解码帧
    pixel_format 完成。
    """
    codec = str(getattr(encoder_config, "codec", "")).lower()
    pix_fmt = str(getattr(encoder_config, "pix_fmt", "")).lower()
    if not codec or not pix_fmt:
        return ()
    return tuple(
        spec for spec in _PASSTHROUGH_FORMAT_SPECS
        if spec.codec == codec and spec.target_pix_fmt == pix_fmt
    )


def _chroma_domain_of(pix_fmt: str) -> str:
    """像素格式的色度子采样域（"420"/"422"/"444"），直通门与交付段共用。"""
    lower = str(pix_fmt or "").lower()
    if "420" in lower:
        return "420"
    if "422" in lower:
        return "422"
    return "444"


def _normalize_hevc_profile(profile: Any, pix_fmt: str) -> Optional[str]:
    """HEVC profile 与交付位深对齐（main=8bit yuv420p / main10=10bit）。

    交付位深以 pix_fmt 为单一真相源；UI 的 profile 选择含 "auto"（=
    未指定），deliver 预设固定 profile="main" 而历史 codec_map 默认
    10-bit——两处来源可能互相矛盾。profile 缺省/"auto"/main↔main10 与
    位深冲突时按位深归一，避免无效 profile 名（"auto"）或 8bit 配
    main10 的矛盾组合传给 avcodec（setter 失败仅告警，流内 profile 与
    容器标签会不一致）。显式选择且不冲突的值原样保留。
    """
    name = str(profile or "").strip().lower()
    if pix_fmt == "yuv420p10le":
        return "main10" if name in (
            "", "auto", "none", "main") else name or None
    if pix_fmt == "yuv420p":
        return "main" if name in (
            "", "auto", "none", "main10") else name or None
    return name or None


@dataclasses.dataclass(frozen=True)
class NativeGpuBridgeEligibility:
    """``can_use_native_gpu_bridge`` 的判定结果（T-P0-30）。

    ``reason_code`` 词表（fail-closed，任一不满足即不可选）：
    ``unsupported_encoder_format`` / ``subtitles_requested`` /
    ``alpha_requested`` / ``no_video_clips`` / ``transitions_present`` /
    ``non_video_clip`` / ``clip_effects_present`` /
    ``non_identity_transform`` / ``non_identity_crop`` /
    ``non_identity_opacity`` / ``fades_present`` /
    ``topos_422_to_nv12_not_implemented`` /
    ``source_pixel_format_unsupported:<fmt>`` /
    ``source_pixel_format_unknown`` /
    ``source_to_surface_not_supported:<fmt>:<surface>`` /
    ``videotoolbox_unavailable``。
    """

    eligible: bool
    reason_code: str = ""
    required_output_surface: str = ""


#: 首版 native VT bridge 支持的源像素格式 → 允许的输出 surface。
#: T-P0-31（TOPOS 422 R16 → NV12/P010 GPU 转换）落地前，4:2:2 源没有
#: GPU 路径——必须在此提前拒绝，而不是运行中途把 CPU frame 送进 bridge。
_NATIVE_BRIDGE_SOURCE_FORMATS: Dict[str, frozenset] = {
    "yuv420p": frozenset({"nv12"}),
    "nv12": frozenset({"nv12"}),
    "yuv420p10le": frozenset({"p010"}),
    "p010": frozenset({"p010"}),
}

#: TOPOS 4:2:2 家族像素格式（无法被首版 GPU bridge 消费）。
_TOPOS_422_PIXEL_FORMATS = frozenset({
    "yuv422p", "yuv422p10le", "yuv422p12le", "yuv422p16le",
})

#: 解码∥编码流水线的默认档位白名单（T-P1-10 直通交付）。实测依据
#: （2026-09-19，topos HQ 素材，96 帧 ×6 稳态中位）：编码耗时接近解码
#: 耗时的重档（standard/hq/4444/4444xq）重叠有净收益——hq 2K 87.5→120.0
#: fps（+37%）、4K 27.2→40.6 fps（+49%）；轻档（proxy/lt）编码过轻、
#: 解码主导，2K proxy 119.2→111.1 fps（−7%）→ 默认串行。
_TOPOS_DIRECT_PIPELINE_TIERS = frozenset({
    "standard", "hq", "4444", "4444xq",
})


def can_use_native_gpu_bridge(
    sequence: Any,
    project: Any,
    encoder_config: Any,
    snapshot: Any = None,
    *,
    export_config: Any = None,
) -> NativeGpuBridgeEligibility:
    """纯函数：判定时间线 + 编码配置能否走 native VT GPU bridge。

    T-P0-30 步骤 1/2：在 encoder 创建**之前**做全量前置协商——检查所有
    clip 的源像素格式/位深、效果/调色/合成、几何、透明度/淡入淡出、转场、
    字幕/alpha、输出 codec/pix_fmt；不得只检查"单层"。任何不满足都返回
    稳定 ``reason_code``，调用方改走普通 encoder 并记录
    ``bridge_not_selected:<reason>``。

    Args:
        sequence: 时间线序列（video_tracks/transitions）。
        project: 编辑工程（get_asset_by_id 解析 clip 源元数据）。
        encoder_config: ``FFmpegEncoderConfig``（codec/pix_fmt/宽高）。
        snapshot: 可选 ``RuntimeCapabilitySnapshot``；提供时要求
            ``encode.videotoolbox`` 为 AVAILABLE。
        export_config: 可选 ``TimelineExportConfig``（字幕/alpha 检查）。
    """
    codec = str(getattr(encoder_config, "codec", "")).lower()
    pixel_format = str(getattr(encoder_config, "pix_fmt", "")).lower()
    surface = {
        ("h264", "yuv420p"): "nv12",
        ("hevc", "yuv420p10le"): "p010",
    }.get((codec, pixel_format))
    if surface is None:
        return NativeGpuBridgeEligibility(
            False, "unsupported_encoder_format")
    width = int(getattr(encoder_config, "width", 0) or 0)
    height = int(getattr(encoder_config, "height", 0) or 0)
    if width <= 0 or height <= 0:
        return NativeGpuBridgeEligibility(False, "invalid_output_dimensions")
    if width % 2 or height % 2:
        # NV12/P010 为 4:2:0：奇数尺寸没有定义的 surface 布局。
        return NativeGpuBridgeEligibility(
            False, "odd_resolution_not_supported")

    if export_config is not None:
        if bool(getattr(export_config, "burn_subtitles", False)):
            # burn_subtitles 是全局开关：时间线没有字幕内容时烧录为无操作，
            # 不构成 bridge 阻断（与交付层"空轨不算"的语义一致）。
            has_subtitle_content = any(
                bool(getattr(t, "is_enabled", True))
                and any(getattr(c, "is_enabled", True)
                        for c in getattr(t, "clips", ()) or ())
                for t in getattr(sequence, "subtitle_tracks", ()) or ()
            )
            if has_subtitle_content:
                return NativeGpuBridgeEligibility(
                    False, "subtitles_requested")
        if bool(getattr(export_config, "has_alpha", False)):
            return NativeGpuBridgeEligibility(False, "alpha_requested")

    if snapshot is not None:
        vt = None
        try:
            vt = snapshot.entry("encode.videotoolbox")
        except Exception:  # noqa: BLE001 — 兼容对象缺 entry() 时跳过快照门
            vt = None
        if vt is not None:
            state = getattr(getattr(vt, "state", None), "value", "")
            if state and state != "AVAILABLE":
                return NativeGpuBridgeEligibility(
                    False, "videotoolbox_unavailable")

    video_tracks = [
        track for track in getattr(sequence, "video_tracks", ()) or ()
        if bool(getattr(track, "is_enabled", True))
    ]
    clips = [
        clip for track in video_tracks
        for clip in getattr(track, "clips", ()) or ()
        if bool(getattr(clip, "is_enabled", True))
    ]
    if not video_tracks or not clips:
        return NativeGpuBridgeEligibility(False, "no_video_clips")

    for track in video_tracks:
        if getattr(track, "transitions", ()):
            return NativeGpuBridgeEligibility(False, "transitions_present")

    try:
        from src.features.edit.core.clip_params import CropParams, TransformParams

        identity_transform = TransformParams()
        identity_crop = CropParams()
        for clip in clips:
            asset = project.get_asset_by_id(clip.asset_id)
            if asset is None or str(getattr(asset, "media_type", "")) != "video":
                return NativeGpuBridgeEligibility(False, "non_video_clip")
            if (getattr(clip, "effects", ())
                    or getattr(clip, "color_stack_id", None)
                    or getattr(clip, "composite_id", None)):
                return NativeGpuBridgeEligibility(
                    False, "clip_effects_present")
            if getattr(clip, "transform", None) != identity_transform:
                return NativeGpuBridgeEligibility(
                    False, "non_identity_transform")
            if getattr(clip, "crop", None) != identity_crop:
                return NativeGpuBridgeEligibility(False, "non_identity_crop")
            opacity = getattr(clip, "opacity", 1.0)
            try:
                non_identity_opacity = abs(float(opacity) - 1.0) > 1e-6
            except (TypeError, ValueError):
                non_identity_opacity = True
            if non_identity_opacity:
                return NativeGpuBridgeEligibility(False, "non_identity_opacity")
            if (float(getattr(clip, "fade_in_duration", 0.0) or 0.0) > 0.0
                    or float(getattr(clip, "fade_out_duration", 0.0) or 0.0) > 0.0):
                return NativeGpuBridgeEligibility(False, "fades_present")
            metadata = getattr(asset, "metadata", {}) or {}
            source_fmt = str(
                metadata.get("pixel_format", "") or "").strip().lower()
            if not source_fmt:
                # T-P0-10 之后 import 即持久化 pixel_format；缺失按未知
                # fail-closed，不得猜测。
                return NativeGpuBridgeEligibility(
                    False, "source_pixel_format_unknown")
            if source_fmt in _TOPOS_422_PIXEL_FORMATS:
                # T-P0-31：422 R16 texture → NV12/P010 surface 的 GPU
                # 降采样 kernel 已落地（timeline_metal_export_renderer
                # timeline_fused_export_topos_texture_{yuv420,p010}_surface，
                # Metal surface 路径、零 CPU 回读）。其余 surface 形态仍无
                # 定义的 422 转换路径，保持 fail-closed。
                if surface not in ("nv12", "p010"):
                    return NativeGpuBridgeEligibility(
                        False, "topos_422_surface_format_unsupported")
                # 422 → nv12/p010 有专属 kernel：跳过通用 source→surface 映射。
                continue
            allowed = _NATIVE_BRIDGE_SOURCE_FORMATS.get(source_fmt)
            if allowed is None:
                return NativeGpuBridgeEligibility(
                    False, f"source_pixel_format_unsupported:{source_fmt}")
            if surface not in allowed:
                return NativeGpuBridgeEligibility(
                    False,
                    f"source_to_surface_not_supported:{source_fmt}:{surface}")
    except NativeGpuBridgeEligibility:
        raise
    except Exception:
        # 几何/资产结构未知 → fail-closed
        return NativeGpuBridgeEligibility(False, "timeline_structure_unknown")

    return NativeGpuBridgeEligibility(
        True, reason_code="", required_output_surface=surface)


class _ExternalGpuSurfaceEncoder:
    """Duck-typed video encoder for an explicitly injected native GPU bridge.

    The bridge owns the output container and native compression session.  This
    adapter exists so TimelineExporter can retain its normal open/close and
    audio-capability checks without opening a second host-visible FFmpeg
    container for the same output path.
    """

    supports_audio = True
    hw_fallback_reason = ""
    hw_encoder_name = "h264_videotoolbox"

    def __init__(self, bridge: Any, config: Any) -> None:
        self._bridge = bridge
        self._config = config
        self._is_open = False

    @property
    def is_open(self) -> bool:
        return self._is_open

    def open(self) -> None:
        self._is_open = True

    def encode_frame(self, frame: Any) -> None:
        # The normal TimelineExporter GPU branch submits GpuEncodeSurface
        # directly. This method is only a safety net for an unexpected CPU
        # fallback, which must never silently encode through this bridge.
        from src.shared.export.gpu_encode_surface import GpuEncodeSurface

        if not isinstance(frame, GpuEncodeSurface):
            raise RuntimeError(
                "native GPU bridge cannot consume a CPU/host-visible frame"
            )
        frame.submit()

    def add_audio_stream(
        self,
        codec: str = "aac",
        sample_rate: int = 48000,
        channels: int = 2,
        bitrate: int | None = None,
    ) -> None:
        self._bridge.add_audio_stream(
            codec=codec,
            sample_rate=sample_rate,
            channels=channels,
            bitrate=bitrate,
        )

    def write_audio_samples(self, samples: Any, sample_rate: int = 48000) -> None:
        self._bridge.write_audio_samples(samples, sample_rate=sample_rate)

    def close(self) -> None:
        self._is_open = False

    def abort(self) -> None:
        self._is_open = False


class TimelineExporter:
    """
    时间线导出控制器。

    负责：
    1. 解析导出配置
    2. 初始化渲染管线
    3. 使用 TimelineRenderPlan 计算每帧内容
    4. 合成帧（多轨叠加、转场混合）
    5. 使用 FFmpegVideoEncoder 编码输出
    6. 进度通知

    使用方式：
    ```python
    exporter = TimelineExporter(
        project=edit_project,
        sequence=timeline_sequence,
        frame_provider=video_frame_provider,
    )

    # 连接进度信号
    exporter.progress_changed.connect(on_progress)

    # 执行导出
    config = TimelineExportConfig(
        source=ExportSource.TIMELINE,
        range_type=ExportRangeType.ENTIRE,
        codec=ExportCodec.PRORES_422,
        output_path='/path/to/output.mov',
    )

    success = exporter.export_timeline(config)
    ```
    """

    def __init__(
        self,
        project: "EditProject",
        sequence: "TimelineSequence",
        frame_provider: "VideoFrameProviderService",
        progress_callback: Optional[Callable[[int, int, float], None]] = None,
        sequence_resolver: Optional[Callable[[str, "TimelineSequence"]] ] = None,
        gpu_encode_surface_provider: Optional[Any] = None,
        allow_degraded_grade: bool = False,
    ) -> None:
        """
        初始化导出器。

        Args:
            project: EditProject 实例
            sequence: TimelineSequence 实例
            frame_provider: VideoFrameProviderService 实例
            progress_callback: 进度回调函数 (current_frame, total_frames, progress_percent)
            sequence_resolver: P1-02 嵌套序列解析器 ``timeline_id -> TimelineSequence``；
                未提供时嵌套片段导出降级为跳帧并记录警告
            allow_degraded_grade: N03——keyer 失败/qualifier 后处理不支持
                时允许降级继续（默认 False=拒绝导出，视觉错误不标成功）
            gpu_encode_surface_provider: Optional native GPU encoder surface
                provider. If absent, the established host-visible encoder path
                remains active. The provider must own the native encoder ABI;
                this exporter only submits an already-rendered GPU surface. A
                provider with ``close_on_export=True`` is closed after the
                encoder flushes, which is the ownership mode used by the
                macOS VideoToolbox export bridge.
        """
        self._project = project
        self._sequence = sequence
        self._frame_provider = frame_provider
        self._progress_callback = progress_callback
        # N03：降级硬门开关（默认拒绝——视觉错误不标成功）
        self.allow_degraded_grade = bool(allow_degraded_grade)
        # P1-02: 嵌套序列（递归防环栈 + 子序列解析）
        self._sequence_resolver = sequence_resolver
        self._gpu_encode_surface_provider = gpu_encode_surface_provider
        self._nested_render_stack: set = set()
        self._active_export_config: Optional[TimelineExportConfig] = None

        # CompositeFrameRenderer for comp_clip assets (lazy-initialized)
        self._comp_frame_renderer = None

        # 渲染计划
        self._render_plan: Optional["TimelineRenderPlan"] = None

        # 编码器
        self._encoder: Optional["FFmpegVideoEncoder"] = None

        # 导出状态
        self._is_exporting: bool = False
        self._cancel_requested: bool = False
        self._cancel_pending_before_start: bool = False
        # Pause is cooperative: the current frame is allowed to finish, then
        # the render loop waits without consuming more frames.  An Event is
        # used instead of polling so a paused export does not burn a CPU core.
        self._pause_requested: bool = False
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._current_frame: int = 0
        self._export_start_frame: int = 0
        self._total_frames: int = 0

        # AM-10 阶段 3.4：最近一次导出的响度报告（供调用方/UI 审计）
        self._last_loudness_report: Optional[Any] = None

        # RMA-01：最近一次导出 preflight 结果（供 UI 展示阻断/降级原因）
        self._last_preflight_result: Optional[ExportPreflightResult] = None
        # P1-19：最近一次交付 QC 报告（供 UI/manifest 归档）
        self._last_qc_report: Optional[Any] = None
        # T-P1-70 步骤 5：导出中被跳过的效果（无交付实现、逐帧静默丢失）。
        # 只记日志不进结果会让交付不可审计——逐项进入 manifest degradations。
        self._effect_export_skips: list[str] = []

        # P1-18：最近一次导出的 manifest（供 UI 审计）
        self._last_export_manifest: Optional[Dict[str, Any]] = None
        # Manifest generation hashes every referenced source for auditability.
        # Keep that disk-bound work off the export critical path; the worker is
        # daemonized so a finished video is never held hostage by a multi-GB
        # source hash (the sidecar is replaced atomically when it completes).
        self._manifest_lock = threading.Lock()
        # T-P1-70：交付定稿事件——native bridge 的 muxer 在 export 的
        # finally 里关闭；后台 manifest 线程必须等它收尾后才能对输出
        # 文件做 media probe，否则读到未定稿容器（InvalidData 竞态）。
        self._delivery_finalized = threading.Event()

        # 打开的 clip 会话
        self._open_clips: "OrderedDict[str, str]" = OrderedDict()  # session_key(clip_id) -> clip_session_id，LRU
        # P0-EXPORT-AUDIO：后台音频渲染线程状态（与视频编码并行）
        self._audio_render_thread: Optional[threading.Thread] = None
        self._audio_master_pcm: Optional["np.ndarray"] = None
        self._audio_render_error: Optional[BaseException] = None
        # v1.6 M-B8：stems 追加轨（声明序 [(AudioStemExportTrack, 轨索引)]）
        # 与各角色母带 PCM（与 _audio_stem_tracks 等长；None = 该组无可渲染轨）
        self._audio_stem_tracks: "list" = []
        self._audio_stem_pcms: "list" = []
        # Metal export overlaps the next VT decode with this frame's Metal
        # composition/readback and hardware encode. The decode service keeps
        # its per-session lock, so the prefetch worker never shares decoder
        # state concurrently with the render thread.
        self._metal_prefetch_executor: Optional[ThreadPoolExecutor] = None
        self._metal_prefetch_futures: Dict[Tuple[str, int], Future[Any]] = {}
        self._metal_prefetch_enabled = str(
            os.getenv("TOPOS_EXPORT_METAL_PREFETCH", "1") or "1"
        ).strip().lower() not in ("0", "false", "off", "no")
        # 针对部分 10/12-bit HEVC 的 CPU 导出兼容解码源。它们明确请求
        # rgb24，避免某些 PyAV 版本的 YUV → rgb48 色度转换错误。
        self._export_rgb8_sources: Dict[str, Any] = {}
        # 高位深交付（ProRes / HEVC Main10 / DNxHR HQX）不应走上面的
        # rgb24 兼容分支。这里保留解码器原生 YUV 平面，在 CPU 合成前转换为
        # 16-bit BGR；预览和 8-bit 交付仍保持原有的快速路径。
        self._export_high_depth_sources: Dict[str, Any] = {}
        self._use_high_precision_export: bool = False

        # P1-COLOR-13: 导出 GPU 降噪桥（headless context，惰性创建；
        # 无 clip 启用降噪的导出完全不触碰 GL）
        self._denoise_bridge: Optional[Any] = None

        # P0-GPU-1: 导出 GPU 合成渲染器（headless context，惰性创建；
        # 单层直通场景走 GPU，不适用/失败原样回落 CPU 链）
        self._gpu_export_renderer: Optional[Any] = None
        self._gpu_trail: Optional[ExportGpuTrail] = None
        # T-P0-20：统一阶段 telemetry（每次 export_timeline 重建）。
        self._export_telemetry: Optional[ExportTelemetryRecorder] = None
        # T-P0-30：native bridge 选择状态（selected/reason_code/surface）。
        self._bridge_selection: Dict[str, Any] = {"selected": False,
                                                  "reason_code": "not_evaluated",
                                                  "output_surface": ""}
        self._gpu_ineligible_clip_ids: set = set()
        self._gpu_renderer_recreates = 0
        # Stable 8-bit per-channel grade LUTs avoid allocating and evaluating
        # several full-frame float32 arrays for ordinary primary grades.
        self._color_grade_lut_cache: Dict[Tuple[Any, ...], Any] = {}
        # 开关：TOPOS_EXPORT_GPU_RENDER 显式控制；未设置时测试环境
        # （pytest）默认 CPU 口径保证确定性，生产默认启用。显式 =1 视为
        # 用户强制 GPU 合成，CPU/GPU 自适应跳过策略（见
        # _adaptive_gpu_composite_skip_reason）让位。
        _gpu_env = str(os.getenv("TOPOS_EXPORT_GPU_RENDER", "") or "").strip().lower()
        if _gpu_env:
            self._gpu_render_disabled = _gpu_env in ("0", "false", "off", "no")
            self._gpu_render_forced = _gpu_env in ("1", "true", "on", "yes")
        else:
            self._gpu_render_disabled = "PYTEST_CURRENT_TEST" in os.environ
            self._gpu_render_forced = False

        self._timeline_graphics_backend_reason = ""
        self._timeline_graphics_backend = self._select_timeline_graphics_backend()
        # An explicitly injected native bridge is an opt-in request for the
        # Metal surface path.  Keep the shipping auto preference unchanged for
        # ordinary exporters, but do not silently select OpenGL after the
        # caller has supplied a Metal-only encoder bridge.
        if getattr(self._gpu_encode_surface_provider, "handles_timeline_video", False):
            self._timeline_graphics_backend = "metal"
        self._metal_fallback_warnings: set[str] = set()
        logger.info("Timeline export graphics backend: %s", self._timeline_graphics_backend)

        logger.info(
            f"TimelineExporter initialized: "
            f"timeline={sequence.name}, "
            f"duration={effective_timeline_duration_frames(sequence)} frames"
        )

    @property
    def gpu_trail_snapshot(self) -> Optional[ExportGpuTrailSnapshot]:
        """Return the latest export evidence without changing its ownership."""
        trail = getattr(self, "_gpu_trail", None)
        return trail.snapshot() if trail is not None else None

    @property
    def export_stage_telemetry_snapshot(self) -> Optional[Dict[str, Any]]:
        """T-P0-20：最近一次导出的统一阶段 telemetry（JSON-safe dict）。"""
        recorder = getattr(self, "_export_telemetry", None)
        return recorder.snapshot().to_dict() if recorder is not None else None

    @property
    def bridge_selection(self) -> Dict[str, Any]:
        """T-P0-30：native bridge 选择状态（与运行中 GPU 失败显式区分）。"""
        return dict(getattr(self, "_bridge_selection", {}))

    def _record_gpu_stage(
        self,
        name: str,
        backend: str,
        *,
        gpu_submit_ms: Optional[float] = None,
        cpu_upload_bytes: int = 0,
        cpu_download_bytes: int = 0,
        wall_ms: Optional[float] = None,
    ) -> None:
        trail = getattr(self, "_gpu_trail", None)
        if trail is not None:
            trail.record_stage_sample(
                name=name,
                backend=backend,
                gpu_submit_ms=gpu_submit_ms,
                cpu_upload_bytes=cpu_upload_bytes,
                cpu_download_bytes=cpu_download_bytes,
            )
        # T-P0-20：同一证据写入统一阶段 telemetry（阶段词表映射：trail 的
        # 'yuv_conversion' 等诊断段不在规范词表内时忽略）。
        recorder = getattr(self, "_export_telemetry", None)
        if recorder is not None:
            try:
                recorder.record_stage_sample(
                    name,
                    backend,
                    gpu_submit_ms=gpu_submit_ms,
                    cpu_upload_bytes=cpu_upload_bytes,
                    cpu_download_bytes=cpu_download_bytes,
                    wall_ms=wall_ms,
                )
            except ValueError:
                pass  # 诊断段名不在规范词表——legacy trail 仍保留

    def _record_gpu_fallback(self, reason: str) -> None:
        trail = getattr(self, "_gpu_trail", None)
        if trail is not None:
            trail.set_fallback_reason(reason)
        # T-P0-20：根因 first-wins——首个 fallback 是根因，后续症状不覆盖。
        recorder = getattr(self, "_export_telemetry", None)
        if recorder is not None:
            recorder.set_fallback_root_cause(
                reason,
                frame_index=int(getattr(self, "_current_frame", 0) or 0),
            )

    # =========================================================================
    # 公共接口
    # =========================================================================

    def export_timeline(
        self,
        config: TimelineExportConfig,
    ) -> ExportOutcome:
        """导出时间线（T-P0-20：结束后附统一阶段 telemetry）。

        Args:
            config: 导出配置

        Returns:
            :class:`ExportOutcome`：成功 / 用户取消 / 失败三态显式
            （P0-05）；``stage_telemetry`` 携带统一阶段统计、backend、
            fallback 根因与 full_gpu 判定（JSON-safe）。
        """
        errors = config.validate()
        if errors:
            raise ValueError(f"Invalid config: {errors}")
        outcome = self._export_timeline_dispatch(config)
        recorder = getattr(self, "_export_telemetry", None)
        if recorder is not None and outcome.stage_telemetry is None:
            outcome = dataclasses.replace(
                outcome, stage_telemetry=recorder.snapshot().to_dict())
        return outcome

    def _export_timeline_dispatch(
        self,
        config: TimelineExportConfig,
    ) -> ExportOutcome:
        """
        导出时间线（原实现；``export_timeline`` 负责 telemetry 附加）。

        Args:
            config: 导出配置

        Returns:
            :class:`ExportOutcome`：成功 / 用户取消 / 失败三态显式
            （P0-05）。``__bool__`` 等价 ``success``，历史布尔调用点语义
            不变；需要区分取消与失败时必须读结构化字段。

        Raises:
            ValueError: 配置无效
        """
        # 验证配置
        errors = config.validate()
        if errors:
            raise ValueError(f"Invalid config: {errors}")

        self._active_export_config = config
        self._gpu_trail = ExportGpuTrail(
            f"timeline:{config.output_path}:{time.monotonic_ns()}"
        )
        # T-P0-20：统一阶段 telemetry（root-cause first-wins、full_gpu 判定）。
        self._export_telemetry = ExportTelemetryRecorder(
            f"timeline:{config.output_path}:{time.monotonic_ns()}",
            pipeline="timeline",
        )
        # T-P1-70：每次导出重置被跳过效果记录（逐项进 manifest degradations）。
        # setdefault：__new__ 直构的 exporter（单测桩）没有 __init__ 布下
        # 的属性时也不崩。
        self.__dict__.setdefault("_effect_export_skips", []).clear()
        self.__dict__.setdefault(
            "_delivery_finalized", threading.Event()).clear()

        # Individual clips uses exactly the same render, audio and subtitle
        # pipeline as a normal delivery.  It merely expands the timeline into
        # one bounded single-clip export per enabled video clip.
        if config.render_mode == ExportRenderMode.INDIVIDUAL_CLIPS.value:
            return self._export_individual_clips(config)

        # Validate the exact audible export window before creating any encoder,
        # including direct delivery. Preview may omit unsupported nests; audio
        # delivery must surface their identities instead of succeeding silently.
        self._audio_render_sequence = self._sequence
        if config.include_audio and config.codec != ExportCodec.GIF:
            from src.features.edit.core.nested_sequence import NestedAudioExpansionError

            try:
                self._audio_render_sequence = expand_sequence_audio_for_render(
                    self._sequence, self._sequence_resolver, self._project,
                    strict=True, frame_range=self._calculate_frame_range(config),
                )
            except NestedAudioExpansionError as exc:
                self._last_preflight_result = ExportPreflightResult(
                    ok=False, blocking=tuple(item.message for item in exc.diagnostics),
                    blockers=exc.diagnostics,
                )
                raise ValueError(f"Nested audio preflight failed: {exc}") from exc

        # RMA-01: 音频后端 preflight —— scipy 缺失且本次导出依赖 EQ/响度归一化时，
        # 不得静默产出未处理文件。返回结构化错误，调用方据此阻止导出或提示用户。
        preflight = (
            ExportPreflightResult(ok=True)
            if config.codec == ExportCodec.GIF
            else preflight_audio_export(config, self._audio_render_sequence)
        )
        if not preflight.ok:
            msg = "; ".join(preflight.blocking_messages)
            logger.error("Export blocked by audio preflight: %s", msg)
            self._last_preflight_result = preflight
            raise ValueError(f"Audio preflight failed: {msg}")
        self._last_preflight_result = preflight

        # SUB-P2-04/11: 字体 preflight —— 烧录字幕时缺关键字体不得静默用替代/
        # 空字形产出（error 阻断）；有替代字体则降级提示并继续。结果合并进
        # _last_preflight_result，供 UI/manifest 审计。
        if config.burn_subtitles:
            font_pf = preflight_fonts_export(self._sequence)
            if not font_pf.ok:
                msg = "; ".join(font_pf.blocking_messages)
                logger.error("Export blocked by font preflight: %s", msg)
                self._last_preflight_result = ExportPreflightResult(
                    ok=False,
                    blocking=preflight.blocking + font_pf.blocking,
                    degraded=preflight.degraded + font_pf.degraded,
                )
                raise ValueError(f"Font preflight failed: {msg}")
            if font_pf.degraded:
                self._last_preflight_result = ExportPreflightResult(
                    ok=True,
                    blocking=preflight.blocking,
                    degraded=preflight.degraded + font_pf.degraded,
                )
                for d in font_pf.degraded_messages:
                    logger.warning("Font preflight warning: %s", d)

        # FX-M05/FX-M07: video effects and plugin identity preflight.
        # Unsupported/missing/schema-incompatible effects block export so the
        # deliverable can never silently omit a visible preview effect.
        vfx_pf = preflight_video_effects_export(self._sequence)
        if not vfx_pf.ok:
            self._last_preflight_result = ExportPreflightResult(
                ok=False,
                blocking=self._last_preflight_result.blocking + vfx_pf.blocking,
                degraded=self._last_preflight_result.degraded + vfx_pf.degraded,
            )
            msg = "; ".join(vfx_pf.blocking_messages)
            logger.error("Export blocked by video effect preflight: %s", msg)
            raise ValueError(f"Video effect preflight failed: {msg}")
        if vfx_pf.degraded:
            self._last_preflight_result = ExportPreflightResult(
                ok=True,
                blocking=self._last_preflight_result.blocking,
                degraded=self._last_preflight_result.degraded + vfx_pf.degraded,
            )
            for d in vfx_pf.degraded_messages:
                logger.warning("Video effect preflight warning: %s", d)

        caption_graphics_pf = preflight_caption_graphics_export(
            self._sequence, self._project
        )
        if not caption_graphics_pf.ok:
            self._last_preflight_result = ExportPreflightResult(
                ok=False,
                blocking=self._last_preflight_result.blocking + caption_graphics_pf.blocking,
                degraded=self._last_preflight_result.degraded + caption_graphics_pf.degraded,
            )
            msg = "; ".join(caption_graphics_pf.blocking_messages)
            logger.error("Export blocked by caption graphics preflight: %s", msg)
            raise ValueError(f"Caption graphics preflight failed: {msg}")
        if caption_graphics_pf.degraded:
            self._last_preflight_result = ExportPreflightResult(
                ok=True,
                blocking=self._last_preflight_result.blocking,
                degraded=self._last_preflight_result.degraded + caption_graphics_pf.degraded,
            )
            for d in caption_graphics_pf.degraded_messages:
                logger.warning("Caption graphics preflight warning: %s", d)

        # RMA-05: 路由图校验门禁——非法路由（环/未知 output/send 目标）不得静默
        # 回退 Master 并产出文件。导入旧工程/命令/撤销路径可绕过 UI 校验，导致
        # 声音错误落到 Master。在导出初始化前阻断，避免产出错误文件。
        if config.include_audio and config.codec != ExportCodec.GIF:
            from src.features.edit.core.audio_graph_snapshot import build_audio_graph_snapshot
            snapshot = build_audio_graph_snapshot(self._sequence)
            if not snapshot.is_valid:
                msg = "; ".join(snapshot.validation_errors)
                logger.error("Export blocked by invalid audio routing graph: %s", msg)
                raise ValueError(f"Invalid audio routing graph: {msg}")

        # P1-19: 交付质量门——离线素材等 BLOCKING 问题不得产出可交付文件。
        # WARNING（音画时长差/字幕越界/空时间线/帧率不一致）记录并随报告归档，
        # 不阻断。QC 自身异常绝不阻塞导出（报告降级为空）。
        try:
            from src.features.edit.core.delivery_qc import run_delivery_qc
            qc_report = run_delivery_qc(self._sequence, self._project)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Delivery QC skipped due to internal error: %s", exc)
            qc_report = None
        self._last_qc_report = qc_report
        if qc_report is not None and not qc_report.passed:
            msg = "; ".join(qc_report.blocking_messages)
            logger.error("Export blocked by delivery QC: %s", msg)
            raise ValueError(f"Delivery QC failed: {msg}")
        if qc_report is not None:
            for warning_message in qc_report.warning_messages:
                logger.warning("Delivery QC: %s", warning_message)

        # 初始化导出状态
        # AUD-18411: clear the previous run's cancel state *before* flipping
        # `_is_exporting` — at this point a late cancel_export() still goes
        # through the `_cancel_pending_before_start` channel, so nothing can
        # be lost here, and a stale flag from an earlier cancelled run cannot
        # leak into this run.
        self._cancel_requested = False
        self._is_exporting = True
        # Preserve a cancel request that arrived in the small window between
        # worker creation and export_timeline().  Normal runs start clean;
        # cancellation during a run already sets _cancel_requested directly.
        # AUD-18411: merge instead of overwrite — a cancel_export() arriving
        # between `_is_exporting = True` and this line sets _cancel_requested
        # directly (the `not _is_exporting` branch is skipped), so overwriting
        # it with the pending flag could swallow that request. OR-merge keeps
        # both channels.
        self._cancel_requested = (
            self._cancel_requested or self._cancel_pending_before_start
        )
        self._cancel_pending_before_start = False
        self._current_frame = 0

        # 计算帧范围
        start_frame, end_frame = self._calculate_frame_range(config)
        self._export_start_frame = start_frame

        self._total_frames = end_frame - start_frame

        # AUD-18405：非空时间线上 CUSTOM 范围解析为空（start ≥ 时长）时，
        # 此前 range() 为空循环 → 编码器 close 出 0 帧文件仍按成功交付并
        # 写成功 manifest（进度百分比还为负）。空时间线（duration=0）的
        # 空画布交付是 QC 认定的合法路径，保持既有行为。
        if (config.range_type == ExportRangeType.CUSTOM
                and end_frame <= start_frame
                and effective_timeline_duration_frames(self._sequence) > 0):
            self._is_exporting = False
            logger.error(
                "Export custom frame range is empty: start=%s end=%s "
                "against timeline duration=%s",
                config.start_frame, config.end_frame,
                effective_timeline_duration_frames(self._sequence),
            )
            return ExportOutcome.failed(
                error_code="EXPORT_EMPTY_FRAME_RANGE",
                cause=(
                    f"custom frame range [{config.start_frame}, "
                    f"{config.end_frame}] contains no frames within "
                    "the timeline duration"),
            )

        logger.info(
            f"Starting export: range=[{start_frame}, {end_frame}), "
            f"total_frames={self._total_frames}, "
            f"codec={config.codec}"
        )

        try:
            # 创建渲染计划
            # NOTE: TimelineRenderPlan 仅在 TYPE_CHECKING 块中导入（类型注解用），
            # 此处运行时实际调用，需显式导入。
            from src.features.edit.core.timeline_render_plan import TimelineRenderPlan
            self._render_plan = TimelineRenderPlan(self._sequence)

            # 创建编码器配置
            encoder_config = self._create_encoder_config(config)

            # T-P1-10：TOPOS identity 直通快路径——严格 eligibility 判定，
            # 任一不满足立刻走普通 render path 并记录原因。
            if str(getattr(encoder_config, "codec", "")).lower() == "topos":
                direct_plan, direct_reason = build_direct_topos_delivery_plan(
                    self._sequence, self._project, config, encoder_config,
                    start_frame, end_frame,
                )
                if direct_plan is not None:
                    outcome = self._export_direct_topos(
                        direct_plan, config, encoder_config)
                    if outcome is not None:
                        return outcome
                else:
                    logger.info(
                        "Direct TOPOS delivery not eligible: %s",
                        direct_reason,
                    )
                    recorder = getattr(self, "_export_telemetry", None)
                    if recorder is not None:
                        recorder.set_delivery_path(
                            f"topos_direct_not_eligible:{direct_reason}")

            self._use_high_precision_export = self._pixel_format_is_high_depth(
                encoder_config.pix_fmt
            )
            # R2（审计 P0-1）：Alpha 开关贯通渲染侧——合成缓冲/帧提取/字幕
            # 烧录/编码分派按 4 通道工作；未勾选时严格保持既有 3 通道行为
            # （零回归风险）。内部合成约定 straight alpha。
            self._export_has_alpha = bool(encoder_config.has_alpha)
            # R2（审计 P1-2）：导出色彩元数据缓存——comp clip 场景线性帧的
            # 显式 OETF 转换依据（每帧重复解析素材元数据太浪费）。
            self._export_color_metadata = self._resolve_export_color_metadata()

            # 创建编码器
            self._encoder = self._create_encoder(config.output_path, encoder_config)

            # 打开编码器
            self._encoder.open()

            # 音频准备：在视频循环前添加音频流（如有音频轨道）。
            # P1-02: 判定基于**展开后**的音频轨——纯嵌套音频（父序列自身
            # 无音频轨）此前被这里短路：不加音频流、也不进入音频渲染。
            # Already expanded strictly before encoder creation; video-only
            # exports deliberately keep the original sequence.
            has_audio = (
                config.include_audio
                and any(len(t.clips) > 0
                        for t in self._audio_render_sequence.audio_tracks)
            )
            # P1-02：无音轨编码器（GIF/图片序列；topos 容器 v1.1 起已带
            # 单音轨，不再触发）显式跳过音频——此前音频仍被完整渲染/混音
            # 后由编码器静默丢弃。跳过 + 结果标注 audio_omitted（能力、
            # 作业结果、日志三处一致）。
            self._audio_omitted = False
            if has_audio and not getattr(self._encoder, 'supports_audio', True):
                if config.codec == ExportCodec.GIF:
                    logger.warning(
                        "Animated GIF has no audio track; timeline audio was omitted"
                    )
                else:
                    logger.warning(
                        "目标编码器不含音轨（图片序列）：时间线音频已跳过"
                        "（audio_omitted）；最终交付需 sidecar 音轨或第二阶段 mux"
                    )
                self._audio_omitted = True
                has_audio = False
            if has_audio:
                try:
                    # P1-09：声道数与混音路径同源（显式布局单一真相）
                    from src.features.edit.core.channel_layout import (
                        resolve_export_channels,
                    )

                    self._encoder.add_audio_stream(
                        codec=config.audio_codec,
                        sample_rate=config.audio_sample_rate,
                        channels=resolve_export_channels(
                            config.audio_channels, config.audio_channel_layout
                        ),
                        bitrate=config.audio_bitrate,
                    )
                except Exception as e:
                    logger.warning(f"Failed to add audio stream: {e}")
                    has_audio = False

            # v1.6 M-B8：stems 追加轨声明（须在首帧编码前；编码器不支持/
            # 声明失败整体降级单轨——轨 0 主混音不变）
            self._audio_stem_tracks = []
            self._audio_stem_pcms = []
            if has_audio and config.audio_stems:
                self._audio_stem_tracks = self._declare_audio_stems(config)

            # 音频渲染与视频编码并行（P0-EXPORT-AUDIO）：解码/混音/Loudness
            # 在独立线程执行，master PCM 就绪后由导出线程在视频循环结束处
            # 统一写入编码器——容器 mux 仍保持单线程语义，不引入并发写。
            if has_audio:
                self._audio_master_pcm = None
                self._audio_render_error = None
                self._audio_render_thread = threading.Thread(
                    target=self._audio_export_worker,
                    args=(start_frame, end_frame, config),
                    name="topos-export-audio",
                    daemon=True,
                )
                self._audio_render_thread.start()

            # 逐帧渲染并编码 —— 阶段 3b：渲染 ∥ 编码流水线（渲染生产者线程
            # 与编码消费线程重叠；GPU 降噪等不适用场景自动回退串行）
            if self._pipeline_eligible(start_frame, end_frame, config):
                self._render_encode_pipeline(start_frame, end_frame, config)
            else:
                for frame_idx in range(start_frame, end_frame):
                    if not self._wait_if_paused():
                        break
                    # 检查取消请求
                    if self._cancel_requested:
                        logger.info("Export cancelled by user")
                        break

                    # 渲染帧
                    frame_data = self._render_frame(frame_idx, config)

                    if frame_data is not None:
                        # 编码帧
                        self._encode_frame(frame_data)
                    else:
                        logger.warning(f"Frame {frame_idx} render returned None")

                    # 更新进度
                    self._current_frame = frame_idx - start_frame + 1
                    self._notify_progress()

            # 音频写入（在视频帧全部编码后）。渲染已与视频并行完成，这里
            # 只汇合线程并串行写编码器。
            # P1-01：用户取消后立即停止——跳过写入（音频渲染线程内部也
            # 检查取消并提前收尾）。
            if self._audio_render_thread is not None:
                self._join_audio_render_thread()
            if self._audio_render_error is not None:
                audio_error = self._audio_render_error
                self._audio_render_error = None
                self._audio_master_pcm = None
                raise audio_error
            audio_pcm = self._audio_master_pcm
            self._audio_master_pcm = None
            if audio_pcm is not None and has_audio and not self._cancel_requested:
                try:
                    self._encoder.write_audio_samples(
                        audio_pcm, config.audio_sample_rate,
                    )
                    logger.info("Audio export complete (parallel render)")
                except Exception as e:
                    logger.warning(f"Failed to write audio: {e}")
                # v1.6 M-B8：主混音落轨 0 后，各角色母带串行写追加轨
                if not self._cancel_requested:
                    self._write_stem_tracks(config)

            # 关闭编码器。用户取消时优先 abort()（支持该方法的编码器——
            # 如 ToposVideoEncoder——丢弃未定稿数据并删除临时文件，
            # 取消不会留下被误识别为成功的输出文件）。
            if self._encoder:
                if self._cancel_requested and hasattr(self._encoder, 'abort'):
                    try:
                        self._encoder.abort()
                    except Exception:
                        logger.exception("Encoder abort failed on cancel")
                else:
                    # T-P0-20：mux（定稿封装）阶段 wall 计时。
                    _mux_started = time.perf_counter()
                    try:
                        self._encoder.close()
                    finally:
                        _recorder = getattr(self, "_export_telemetry", None)
                        if _recorder is not None:
                            _recorder.record_stage_sample(
                                "mux", "container_writer",
                                wall_ms=(time.perf_counter() - _mux_started)
                                * 1000.0,
                            )

            # 清理 clip 会话
            self._cleanup_clips()

            # P0-05：取消与成功显式分型（不再共用 False）
            if self._cancel_requested:
                logger.info("Export cancelled: frames_encoded=%s",
                            self._current_frame)
                return ExportOutcome.user_cancelled()

            logger.info(
                f"Export complete: success=True, "
                f"frames_encoded={self._current_frame}"
            )

            # P1-18: 交付 manifest 归档——源 hash/revision/能力快照/QC/preflight。
            # 源素材可能是数 GB，完整 SHA-256 必须在后台生成，不能把磁盘
            # 扫描时间算进视频导出关键路径；sidecar 仍会在后台原子替换完成。
            self._write_success_manifest_async(config)

            return ExportOutcome.ok(
                audio_omitted=self._audio_omitted,
                hw_fallback_reason=getattr(
                    self._encoder, "hw_fallback_reason", ""
                ) or "",
            )

        except Exception as e:
            logger.exception("Export failed")
            # 异常路径同样汇合音频线程，避免泄漏到下一次导出
            self._join_audio_render_thread()
            self._audio_master_pcm = None
            self._audio_render_error = None
            # 清理资源（异常路径同样先 abort：失败不得留下伪成功文件）
            if self._encoder:
                try:
                    if hasattr(self._encoder, 'abort'):
                        self._encoder.abort()
                    else:
                        self._encoder.close()
                except Exception:
                    pass
            self._cleanup_clips()
            # P0-05：异常是失败不是取消——结构化错误码 + 类型链 cause，
            # worker/UI 层据此与 EXPORT_CANCELLED 区分
            return ExportOutcome.failed(
                error_code="EXPORT_FAILED",
                cause=f"{type(e).__name__}: {e}",
            )

        finally:
            self._is_exporting = False
            gpu_surface_provider = getattr(
                self, "_gpu_encode_surface_provider", None
            )
            if getattr(gpu_surface_provider, "close_on_export", False):
                try:
                    gpu_surface_provider.close()
                except Exception:
                    logger.exception("GPU encode surface provider close failed")
            self._encoder = None
            self._use_high_precision_export = False
            self._export_has_alpha = False
            self._export_color_metadata = None
            # T-P1-70：交付定稿——native muxer 已收尾，后台 manifest
            # 线程可以安全 probe 输出文件。
            self._delivery_finalized.set()
            # P1-COLOR-13: 释放导出降噪桥的 headless GL context
            if self._denoise_bridge is not None:
                try:
                    self._denoise_bridge.release()
                except Exception:
                    pass
                self._denoise_bridge = None
            # P0-GPU-1: 释放导出 GPU 合成渲染器（串行模式创建线程=导出
            # 线程；流水线模式已在生产者线程 finally 释放，此处幂等空操作）
            self._release_gpu_export_renderer()

    def _write_success_manifest(
        self,
        config: TimelineExportConfig,
        *,
        hash_assets: bool = True,
        manifest_state: Optional["_ManifestSnapshot"] = None,
    ) -> None:
        """P1-18: 成功导出后写交付 manifest（任何失败只记日志，不影响导出）。

        AUD-18402：后台 manifest 线程必须经 ``manifest_state`` 传入在导出
        线程一次性捕获的状态快照——individual-clips 子导出循环会在后台
        哈希期间改写 ``self._sequence`` / ``_effect_export_skips`` /
        ``_last_preflight_result`` / ``_last_qc_report``，直接读 self 会把
        clip N+1 的状态嵌进 clip N 的交付审计 sidecar。
        """
        state = manifest_state if manifest_state is not None \
            else self._snapshot_manifest_state()
        try:
            from src.features.edit.core.export_manifest import (
                build_export_manifest,
                manifest_path_for,
                write_manifest_file,
            )
            # R16（第七轮审计）：图像序列的 output 快照目标必须是实际帧
            # 目录——基路径不是输出文件（单文件哈希只会报不存在）。
            output_path = config.output_path
            codec_value = str(
                getattr(getattr(config, "codec", None), "value",
                        getattr(config, "codec", "")) or "").lower()
            if codec_value in ("png_sequence", "tiff_sequence"):
                seq_info = getattr(
                    getattr(self, "_encoder", None), "sequence_info", None)
                if callable(seq_info):
                    output_path = seq_info().get("directory") or output_path
            manifest = build_export_manifest(
                state.project,
                state.sequence,
                config,
                qc_report=state.qc_report,
                preflight_result=state.preflight_result,
                loudness_report=state.loudness_report,
                output_path=output_path,
                hash_assets=hash_assets,
                stage_telemetry=state.stage_telemetry,
                effect_export_skips=state.effect_export_skips,
                probe_media=hash_assets and codec_value not in (
                    "png_sequence", "tiff_sequence"),
            )
            write_manifest_file(manifest_path_for(config.output_path), manifest)
            self._last_export_manifest = manifest
            logger.info(
                "Export manifest written: %s", manifest_path_for(config.output_path)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Export manifest write failed: %s", exc)

    def _snapshot_manifest_state(self) -> "_ManifestSnapshot":
        """在调用线程一次性捕获 manifest 所需的全部可变导出状态。

        ``_export_telemetry`` 快照同样在此捕获（后台线程读 self 属性时
        individual-clips 可能已重建 telemetry 实例）。
        """
        return _ManifestSnapshot(
            project=self._project,
            sequence=self._sequence,
            qc_report=getattr(self, "_last_qc_report", None),
            preflight_result=getattr(self, "_last_preflight_result", None),
            loudness_report=getattr(self, "_last_loudness_report", None),
            effect_export_skips=tuple(
                self.__dict__.get("_effect_export_skips", ()) or ()),
            stage_telemetry=self.export_stage_telemetry_snapshot,
        )

    def _write_success_manifest_async(self, config: TimelineExportConfig) -> None:
        """Write the success manifest without blocking video delivery completion.

        ``build_export_manifest`` intentionally computes full SHA-256 hashes
        for auditability.  A source such as the 2.7 GB Topos test master takes
        several seconds to scan, while it contributes no video pixels.  Keep
        the existing synchronous ``_write_success_manifest`` API for callers
        and tests that explicitly request it, but run it on a serialized
        daemon worker for normal exports.  The manifest writer uses an atomic
        replace, so readers see either the previous complete sidecar or the
        new complete one.

        AUD-18402：① 全部所需状态在导出线程入口一次性快照（后台线程不再
        读 self 的可变成员——individual-clips 子导出会并发改写它们）；
        ② cheap 侧写同样纳入 ``_manifest_lock``，与持锁全量写互斥，连续
        两次导出同一路径时不会出现锁外写与持锁写交错。
        """
        # 在导出线程捕获快照（cheap 写与后台全量写共用）。
        manifest_state = self._snapshot_manifest_state()
        # Emit a cheap, immediately available sidecar first.  This preserves
        # the manifest contract even when a CLI process exits before a daemon
        # thread can finish scanning a multi-GB source.
        with self._manifest_lock:
            self._write_success_manifest(
                config, hash_assets=False, manifest_state=manifest_state)

        def write_in_background() -> None:
            try:
                with self._manifest_lock:
                    # T-P1-70：先等交付定稿（native muxer 收尾），否则 media
                    # probe 会读到未定稿容器。上限 5 分钟防悬挂（超时后 probe
                    # 失败会如实记录，不伪造）。
                    self._delivery_finalized.wait(timeout=300.0)
                    self._write_success_manifest(
                        config, hash_assets=True, manifest_state=manifest_state)
            finally:
                _unregister_manifest_writer(thread)

        thread = threading.Thread(
            target=write_in_background,
            name="export-manifest-writer",
            daemon=True,
        )
        _register_manifest_writer(thread)
        thread.start()

    def _export_individual_clips(
        self, config: TimelineExportConfig) -> ExportOutcome:
        """Render every enabled timeline video clip to an independently named file.

        A child export keeps its original timeline frame range, which means
        color, compositing, subtitles and audio are evaluated by the same
        code path as a single-clip delivery.  Encoding starts at frame zero
        for each child output, as users expect from independent media files.
        """
        exports = self._build_individual_clip_exports(config)
        if not exports:
            raise ValueError("Individual Clips requires at least one enabled video clip in the selected range")

        total_frames = sum(item.end_frame - item.start_frame for item in exports)
        completed_frames = 0
        original_sequence = self._sequence
        self._individual_progress_total = total_frames
        self._individual_progress_offset = 0
        any_audio_omitted = False
        hw_fallback_reasons: list[str] = []
        try:
            for item in exports:
                if self._cancel_requested:
                    return ExportOutcome.user_cancelled()
                self._individual_progress_offset = completed_frames
                # Isolate the active video clip. Without this, overlapping
                # video tracks would leak into every individual output, and
                # handle frames before/after a trimmed clip would be black.
                self._sequence = item.sequence
                child_config = replace(
                    config,
                    render_mode=ExportRenderMode.SINGLE_CLIP.value,
                    range_type=ExportRangeType.CUSTOM,
                    start_frame=item.start_frame,
                    end_frame=item.end_frame,
                    output_path=item.output_path,
                )
                child = self.export_timeline(child_config)
                any_audio_omitted = any_audio_omitted or child.audio_omitted
                child_hw_reason = getattr(child, "hw_fallback_reason", "")
                if child_hw_reason:
                    hw_fallback_reasons.append(child_hw_reason)
                if not child:
                    # P0-05：保留子导出的取消/失败区分（取消向上传播，
                    # 失败包装为 INDIVIDUAL_CLIP_FAILED 并携带子 cause）
                    if child.cancelled:
                        return ExportOutcome.user_cancelled()
                    return ExportOutcome.failed(
                        error_code="INDIVIDUAL_CLIP_FAILED",
                        cause=f"{item.output_path}: "
                              f"{child.error_code or 'unknown'} {child.cause}".strip(),
                    )
                completed_frames += item.end_frame - item.start_frame
            return ExportOutcome.ok(
                audio_omitted=any_audio_omitted,
                hw_fallback_reason="; ".join(dict.fromkeys(hw_fallback_reasons)),
            )
        finally:
            self._sequence = original_sequence
            self._individual_progress_total = 0
            self._individual_progress_offset = 0

    def _build_individual_clip_exports(
        self,
        config: TimelineExportConfig,
    ) -> List[_IndividualClipExport]:
        """Build isolated individual-clip renders in deterministic track order.

        Resolve's unique-name convention uses the video track number and the
        clip's ordinal in that track (for example ``V2-0025``).  We preserve
        that convention and apply a final collision guard even when users
        deliberately disable the visible unique-name option, so no export can
        silently overwrite a prior clip.
        """
        range_start, range_end = self._calculate_frame_range(config)
        extension = os.path.splitext(config.output_path)[1]
        output_dir = os.path.dirname(config.output_path) or "."
        reserved_paths: set[str] = set()
        exports: List[_IndividualClipExport] = []

        for track_number, track in enumerate(self._sequence.video_tracks, start=1):
            clips = sorted(
                (clip for clip in track.clips if getattr(clip, "is_enabled", True)),
                key=lambda clip: (clip.timeline_start, clip.clip_id),
            )
            for clip_number, clip in enumerate(clips, start=1):
                content_start = max(range_start, int(clip.timeline_start))
                content_end = min(range_end, int(clip.timeline_end))
                if content_end <= content_start:
                    continue
                start, end, isolated_sequence = self._prepare_individual_clip_sequence(
                    track, clip, content_start, content_end, config.frame_handles
                )
                stem = self._individual_clip_stem(
                    config, clip, track_number, clip_number
                )
                output_path = self._deduplicate_individual_path(
                    os.path.join(output_dir, stem + extension), reserved_paths
                )
                reserved_paths.add(os.path.normcase(os.path.abspath(output_path)))
                exports.append(_IndividualClipExport(
                    output_path=output_path,
                    start_frame=start,
                    end_frame=end,
                    sequence=isolated_sequence,
                ))
        return exports

    def _prepare_individual_clip_sequence(
        self,
        track: Any,
        clip: Any,
        content_start: int,
        content_end: int,
        requested_handles: int,
    ) -> Tuple[int, int, Any]:
        """Isolate one clip and extend it with safely available frame handles.

        Handles are source-media frames rendered before and after the edit,
        not black timeline padding.  They are clipped by the timeline origin,
        source in/out, and the media's known duration.  A partial custom
        range remains exact (no speculative handles) because it does not
        necessarily describe a complete editorial clip.
        """
        # Lightweight test/dummy sequences deliberately do not implement the
        # immutable model API.  They exercise naming only, so avoid trying to
        # dataclass-replace their simple clip objects.
        rebuild_track = getattr(track, "_rebuild", None)
        rebuild_sequence = getattr(self._sequence, "_rebuild", None)
        if not callable(rebuild_track) or not callable(rebuild_sequence):
            return content_start, content_end, self._sequence

        full_clip_selected = (
            content_start == int(clip.timeline_start)
            and content_end == int(clip.timeline_end)
        )
        head_handles = tail_handles = 0
        if requested_handles > 0 and full_clip_selected:
            head_handles, tail_handles = self._available_clip_handles(
                clip, requested_handles
            )

        start = max(0, content_start - head_handles)
        end = content_end + tail_handles
        extended_clip = self._extend_clip_for_handles(
            clip, start, end, head_handles, tail_handles
        )

        # Keep captions aligned to the original timeline, but remove every
        # other video layer and every transition.  Audio is reduced to clips
        # explicitly linked to this video instance: otherwise music, dialog
        # or an overlapping edit on a different track would leak into every
        # exported "individual" file.
        isolated_track = rebuild_track(clips=(extended_clip,), transitions=())
        isolated_audio_tracks = self._linked_audio_tracks_for_clip(
            clip, start, end, head_handles, tail_handles
        )
        isolated_sequence = rebuild_sequence(
            video_tracks=(isolated_track,),
            audio_tracks=isolated_audio_tracks,
        )
        return start, end, isolated_sequence

    def _linked_audio_tracks_for_clip(
        self,
        video_clip: Any,
        start: int,
        end: int,
        head_handles: int,
        tail_handles: int,
    ) -> tuple[Any, ...]:
        """Return only audio instances paired with *video_clip*.

        Linked A/V clips are created atomically by the timeline editor.  We
        retain those clips (and any bus tracks needed to route them), while
        intentionally excluding unlinked timeline music and narration.  This
        matches the expectation that an individual clip is self-contained,
        rather than a series of overlapping timeline mixdowns.
        """
        linked_id = str(getattr(video_clip, "linked_clip_id", "") or "")
        video_id = str(getattr(video_clip, "clip_id", "") or "")
        isolated: list[Any] = []
        for audio_track in getattr(self._sequence, "audio_tracks", ()):
            if getattr(audio_track, "is_bus", False):
                isolated.append(audio_track)
                continue
            linked_clips = tuple(
                audio_clip
                for audio_clip in getattr(audio_track, "clips", ())
                if (
                    (linked_id and str(getattr(audio_clip, "clip_id", "")) == linked_id)
                    or str(getattr(audio_clip, "linked_clip_id", "")) == video_id
                )
            )
            if not linked_clips:
                continue
            rebuild = getattr(audio_track, "_rebuild", None)
            if not callable(rebuild):
                continue
            extended = tuple(
                self._extend_clip_for_handles(
                    audio_clip, start, end, head_handles, tail_handles
                )
                for audio_clip in linked_clips
            )
            isolated.append(rebuild(clips=extended, transitions=()))
        return tuple(isolated)

    def _available_clip_handles(self, clip: Any, requested: int) -> Tuple[int, int]:
        """Return available timeline-frame handles at the head and tail."""
        source_start = max(0, int(getattr(clip, "source_start", 0) or 0))
        source_duration = max(1, int(getattr(clip, "source_duration", 0) or 0))
        source_end = source_start + source_duration
        source_total = self._source_frame_count_for_clip(clip)
        source_after = max(0, source_total - source_end) if source_total else 0
        speed = abs(float(getattr(clip, "effective_speed", lambda: 1.0)()))
        speed = speed if speed > 0.0 else 1.0

        # For reverse clips, timeline head points toward the source tail.
        if getattr(getattr(clip, "speed", None), "reverse", False):
            head_available, tail_available = source_after, source_start
        else:
            head_available, tail_available = source_start, source_after
        head = min(int(getattr(clip, "timeline_start", 0) or 0), requested)
        head = self._timeline_handles_for_source_frames(head, head_available, speed)
        tail = self._timeline_handles_for_source_frames(requested, tail_available, speed)
        return head, tail

    @staticmethod
    def _timeline_handles_for_source_frames(
        requested: int,
        available_source_frames: int,
        speed: float,
    ) -> int:
        """Clamp requested timeline frames to the available source media."""
        handles = max(0, int(requested))
        while handles > 0 and int(handles * speed) > available_source_frames:
            handles -= 1
        return handles

    def _source_frame_count_for_clip(self, clip: Any) -> int:
        """Best-effort total source length from imported media metadata."""
        asset = None
        if self._project is not None:
            getter = getattr(self._project, "get_asset_by_id", None)
            if callable(getter):
                asset = getter(getattr(clip, "asset_id", ""))
            elif hasattr(self._project, "assets"):
                asset = self._project.assets.get(getattr(clip, "asset_id", ""))
        metadata = getattr(asset, "metadata", {}) if asset is not None else {}
        if not isinstance(metadata, dict):
            return 0
        for key in ("frame_count", "duration_frames", "frames", "nb_frames"):
            try:
                value = int(metadata.get(key, 0) or 0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return 0

    @staticmethod
    def _extend_clip_for_handles(
        clip: Any,
        start: int,
        end: int,
        head_handles: int,
        tail_handles: int,
    ) -> Any:
        """Return a clip instance whose source mapping includes its handles."""
        speed = abs(float(getattr(clip, "effective_speed", lambda: 1.0)()))
        speed = speed if speed > 0.0 else 1.0
        head_source = int(head_handles * speed)
        tail_source = int(tail_handles * speed)
        reverse = bool(getattr(getattr(clip, "speed", None), "reverse", False))
        source_start = int(getattr(clip, "source_start", 0) or 0)
        if reverse:
            source_start -= tail_source
        else:
            source_start -= head_source
        return replace(
            clip,
            timeline_start=start,
            timeline_duration=end - start,
            source_start=max(0, source_start),
            source_duration=(
                int(getattr(clip, "source_duration", 0) or 0)
                + head_source
                + tail_source
            ),
        )

    def _individual_clip_stem(
        self,
        config: TimelineExportConfig,
        clip: Any,
        track_number: int,
        clip_number: int,
    ) -> str:
        """Build one safe basename from Resolve-style naming controls."""
        source_name = self._source_name_for_clip(clip)
        if config.filename_use == "source":
            base = source_name
        elif config.filename_use == "custom":
            base = self._expand_individual_name_template(config.custom_name, clip, source_name)
        else:
            base = self._sequence.name or "timeline"
        base = self._safe_filename_stem(base or source_name or "clip")

        if config.unique_filenames:
            unique_id = (
                f"V{track_number}-{clip_number:0{config.unique_filename_digits}d}"
            )
            base = (
                f"{unique_id}_{base}"
                if config.unique_filename_position == "prefix"
                else f"{base}_{unique_id}"
            )
        if config.add_source_frame_count:
            source_count = max(1, int(getattr(clip, "source_duration", 0) or 0))
            base = f"{base}_{source_count}fr"
        if config.file_suffix:
            base = f"{base}_{self._safe_filename_stem(config.file_suffix)}"
        return base

    def _source_name_for_clip(self, clip: Any) -> str:
        """Return a source-media basename, falling back safely for generators."""
        asset = None
        if self._project is not None:
            get_asset = getattr(self._project, "get_asset_by_id", None)
            if callable(get_asset):
                asset = get_asset(clip.asset_id)
            elif hasattr(self._project, "assets"):
                asset = self._project.assets.get(clip.asset_id)
        if asset is not None:
            path = str(getattr(asset, "path", "") or "")
            if path:
                return os.path.splitext(os.path.basename(path))[0]
            display_name = str(getattr(asset, "display_name", "") or "")
            if display_name:
                return os.path.splitext(display_name)[0]
        return str(getattr(clip, "asset_id", "") or getattr(clip, "clip_id", "clip"))

    def _expand_individual_name_template(
        self,
        template: str,
        clip: Any,
        source_name: str,
    ) -> str:
        """Expand the small, documented metadata subset useful for clips."""
        values = {
            "timeline": self._sequence.name or "timeline",
            "source": source_name,
            "clip": str(getattr(clip, "clip_id", "clip")),
            "asset": str(getattr(clip, "asset_id", "asset")),
        }
        result = template or "{source}"
        for key, value in values.items():
            result = result.replace("{" + key + "}", value)
        return result

    @staticmethod
    def _safe_filename_stem(value: str) -> str:
        """Keep generated names in the chosen output directory."""
        import re

        cleaned = re.sub(r'[\\/:*?"<>|]+', "_", str(value)).strip(" ._")
        return cleaned or "clip"

    @staticmethod
    def _deduplicate_individual_path(path: str, reserved_paths: set[str]) -> str:
        """Never let two individual outputs overwrite each other."""
        candidate = path
        stem, extension = os.path.splitext(path)
        suffix = 2
        while os.path.normcase(os.path.abspath(candidate)) in reserved_paths or os.path.exists(candidate):
            candidate = f"{stem}_{suffix}{extension}"
            suffix += 1
        return candidate

    def cancel_export(self) -> None:
        """
        取消当前导出。

        设置取消标志，导出循环会在下一帧检查并停止。
        """
        self._cancel_requested = True
        if not self._is_exporting:
            self._cancel_pending_before_start = True
        # A paused export must wake up so cancellation is not stuck behind
        # the pause gate.
        self._resume_event.set()

    def pause_export(self) -> None:
        """Pause after the current frame and wait for :meth:`resume_export`."""
        # Accept the request before export_timeline() flips _is_exporting so
        # a user can pause immediately after starting a job.
        self._pause_requested = True
        self._resume_event.clear()

    def resume_export(self) -> None:
        """Resume a previously paused export."""
        self._pause_requested = False
        self._resume_event.set()

    def _wait_if_paused(self) -> bool:
        """Wait while paused and return whether rendering may continue."""
        while self._pause_requested and not self._cancel_requested:
            self._resume_event.wait(timeout=0.25)
        return not self._cancel_requested

    @property
    def last_loudness_report(self) -> Optional[Any]:
        """AM-10 阶段 3.4：返回最近一次导出的响度报告（LoudnessReport），无则 None。"""
        return self._last_loudness_report

    @property
    def last_preflight_result(self) -> Optional[ExportPreflightResult]:
        """RMA-01：返回最近一次导出 preflight 结果，无则 None。"""
        return self._last_preflight_result

    @property
    def last_qc_report(self) -> Optional[Any]:
        """P1-19：返回最近一次交付 QC 报告（DeliveryQCReport），无则 None。"""
        return self._last_qc_report

    @property
    def last_export_manifest(self) -> Optional[Dict[str, Any]]:
        """P1-18：返回最近一次成功导出的 manifest（dict），无则 None。"""
        return self._last_export_manifest

    def is_exporting(self) -> bool:
        """是否正在导出"""
        return self._is_exporting

    def get_progress(self) -> Tuple[int, int, float]:
        """
        获取当前进度。

        Returns:
            (current_frame, total_frames, progress_percent)
        """
        percent = 0.0 if self._total_frames == 0 else self._current_frame / self._total_frames
        return (self._current_frame, self._total_frames, percent)

    # =========================================================================
    # 内部方法
    # =========================================================================

    def _calculate_frame_range(
        self,
        config: TimelineExportConfig,
    ) -> Tuple[int, int]:
        """
        计算导出帧范围。

        Args:
            config: 导出配置

        Returns:
            (start_frame, end_frame) tuple
        """
        duration = effective_timeline_duration_frames(self._sequence)

        if config.range_type == ExportRangeType.ENTIRE:
            # 整个时间线
            return (0, duration)

        elif config.range_type == ExportRangeType.IN_OUT:
            # In/Out 点范围。单端点也表示有效选区：仅 In = In 到末尾；
            # 仅 Out = 开头到 Out。模型中的 Out 点为包含端，导出使用右开区间。
            resolved_range = resolve_timeline_in_out(
                getattr(self._sequence, "range_start", None),
                getattr(self._sequence, "range_end", None),
                duration,
            )
            if resolved_range is None:
                return (0, duration)
            start, end = resolved_range
            return (start, end + 1)

        elif config.range_type == ExportRangeType.SELECTED:
            # 选中的 clip — compute range from selected clip bounds
            if config.selected_clip_ids and hasattr(self._sequence, 'all_tracks'):
                min_start = None
                max_end = None
                for track in self._sequence.all_tracks:
                    for clip in track.clips:
                        if clip.clip_id in config.selected_clip_ids:
                            clip_start = clip.timeline_start
                            clip_end = clip.timeline_start + clip.timeline_duration
                            if min_start is None or clip_start < min_start:
                                min_start = clip_start
                            if max_end is None or clip_end > max_end:
                                max_end = clip_end
                if min_start is not None and max_end is not None:
                    return (min_start, max_end)
            # Fallback: no selection, export entire timeline
            return (0, duration)

        elif config.range_type == ExportRangeType.CUSTOM:
            # 自定义范围
            start = config.start_frame
            end = config.end_frame if config.end_frame >= 0 else duration
            return (start, min(end, duration))

        return (0, duration)

    def _direct_audio_prepare(self, encoder: Any, plan: Any,
                              config: TimelineExportConfig) -> bool:
        """直通路径音频准备（v1.1）：展开音轨 → 判定 → 声明音频流 +
        启动并行母带渲染线程（与普通路径同机制、同取消语义）。

        返回 has_audio；声明失败降级为无音频（警告，不阻断导出）。
        """
        from src.features.edit.core.timeline_export import (
            expand_sequence_audio_for_render as _expand,
        )

        if not config.include_audio:
            self._audio_render_sequence = self._sequence
            return False
        self._audio_render_sequence = _expand(
            self._sequence, self._sequence_resolver, self._project,
            strict=True,
            frame_range=(plan.timeline_start, plan.timeline_start + plan.frames),
        )
        has_audio = (
            config.include_audio
            and any(len(t.clips) > 0
                    for t in self._audio_render_sequence.audio_tracks)
        )
        if not has_audio:
            return False
        try:
            from src.features.edit.core.channel_layout import (
                resolve_export_channels,
            )

            encoder.add_audio_stream(
                codec=config.audio_codec,
                sample_rate=config.audio_sample_rate,
                channels=resolve_export_channels(
                    config.audio_channels, config.audio_channel_layout
                ),
                bitrate=config.audio_bitrate,
            )
        except Exception as e:
            logger.warning(
                "Direct TOPOS delivery: audio stream unavailable, "
                "continuing video-only: %s", e)
            return False
        # v1.6 M-B8：stems 追加轨声明（失败/不支持降级单轨，警告记录）
        self._audio_stem_tracks = []
        self._audio_stem_pcms = []
        if config.audio_stems:
            self._audio_stem_tracks = self._declare_audio_stems(config, encoder)
        self._audio_master_pcm = None
        self._audio_render_error = None
        self._audio_render_thread = threading.Thread(
            target=self._audio_export_worker,
            args=(plan.timeline_start, plan.timeline_start + plan.frames,
                  config, encoder),
            name="topos-direct-export-audio",
            daemon=True,
        )
        self._audio_render_thread.start()
        return True

    def _direct_audio_finish(self, encoder: Any, plan: Any,
                             config: TimelineExportConfig,
                             has_audio: bool) -> None:
        """直通路径音频收尾：汇合渲染线程并串行喂入编码器（帧循环后）。

        渲染异常向上抛（导出失败，与普通路径同语义）；写入失败降级警告
        （视频交付不受损）。
        """
        if not has_audio:
            return
        self._join_audio_render_thread()
        if self._audio_render_error is not None:
            audio_error = self._audio_render_error
            self._audio_render_error = None
            self._audio_master_pcm = None
            raise audio_error
        audio_pcm = self._audio_master_pcm
        self._audio_master_pcm = None
        if audio_pcm is None:
            return
        try:
            encoder.write_audio_samples(audio_pcm, config.audio_sample_rate)
            logger.info("Direct TOPOS delivery: audio written")
            # v1.6 M-B8：主混音落轨 0 后，各角色母带串行写追加轨
            self._write_stem_tracks(config, encoder)
        except Exception as e:
            logger.warning(
                "Direct TOPOS delivery: failed to write audio: %s", e)

    def _render_and_write_audio(
        self,
        start_frame: int,
        end_frame: int,
        config: TimelineExportConfig,
    ) -> None:
        """串行兼容入口：渲染 master PCM 并写入编码器（旧语义不变）。"""
        output = self._render_audio_master(start_frame, end_frame, config)
        if output is None or self._encoder is None:
            return
        try:
            self._encoder.write_audio_samples(output, config.audio_sample_rate)
            logger.info("Audio export complete")
        except Exception as e:
            logger.warning(f"Failed to write audio: {e}")

    def _join_audio_render_thread(self) -> None:
        """汇合后台音频渲染线程（正常/取消/异常路径都必须调用，防泄漏）。"""
        thread = getattr(self, "_audio_render_thread", None)
        if thread is None:
            return
        self._audio_render_thread = None
        if thread.is_alive():
            thread.join()

    def _audio_export_worker(
        self,
        start_frame: int,
        end_frame: int,
        config: TimelineExportConfig,
        encoder: Any = None,
    ) -> None:
        """后台音频渲染线程目标：只产出 master PCM，不触碰编码器/容器。

        失败语义与串行路径一致：渲染异常在导出线程重新抛出（导出失败），
        而不是被吞掉伪装成功。encoder：直通路径的局部编码器（守卫用，
        不写帧——写入仍在导出线程串行完成）。
        """
        try:
            self._audio_master_pcm = self._render_audio_master(
                start_frame, end_frame, config, encoder=encoder,
            )
            # v1.6 M-B8：主混音之后逐角色渲染 stems 母带（同线程串行，
            # 解码器各自独立打开；失败语义随异常统一上抛）
            self._audio_stem_pcms = self._render_stem_masters(
                start_frame, end_frame, config, encoder=encoder,
            )
        except BaseException as exc:  # noqa: BLE001 — 主线程统一裁决
            self._audio_master_pcm = None
            self._audio_stem_pcms = []
            self._audio_render_error = exc

    def _render_audio_master(
        self,
        start_frame: int,
        end_frame: int,
        config: TimelineExportConfig,
        encoder: Any = None,
        only_track_ids: Optional[frozenset] = None,
        channels_override: Optional[int] = None,
    ) -> Optional["np.ndarray"]:
        """
        渲染音频 master PCM（解码 + 混音 + Bus/Master 阶段 + 响度处理）。

        从 _render_and_write_audio 拆出的纯渲染部分——不写编码器，因此
        可以在独立线程与视频编码并行（P0-EXPORT-AUDIO）。用户取消时
        提前返回 None（P1-01 语义：取消后不再为音频做剩余工作）。

        Args:
            start_frame: 起始帧
            end_frame: 结束帧
            config: 导出配置
            encoder: 编码器存在性守卫（直通路径的局部编码器不入
                self._encoder；默认回退读 self._encoder）
            only_track_ids: v1.6 M-B8 stems——只混入这些 track_id 的普通轨
                （Bus/Master 链完整保留，被排除轨贡献恒零 = solo 该组语义；
                None = 全部轨，主混音行为不变）。loudness 归一仅主混音做。
            channels_override: 该次渲染的输出声道数覆盖（stems 轨独立
                声道数，如 mono 对白轨）；None 用 config 布局解析。

        Returns:
            (channels, total_samples) float32 PCM；无可渲染音轨/取消时 None
        """
        audio_sequence = expand_sequence_audio_for_render(
            self._sequence, self._sequence_resolver, self._project,
            strict=True, frame_range=(start_frame, end_frame),
        )
        if encoder is None:
            encoder = self._encoder
        if encoder is None:
            return None

        try:
            from src.features.edit.core.audio_playback import AudioDecoder
            from src.features.edit.core.timeline_render_plan import is_track_renderable
            import numpy as np
        except ImportError:
            logger.warning("Audio modules not available, skipping audio export")
            return

        fps = self._get_sequence_fps()
        sample_rate = config.audio_sample_rate
        # P1-09：显式布局是导出声道数的单一真相（空串回落 audio_channels）
        from src.features.edit.core.channel_layout import resolve_export_channels

        channels = int(channels_override) if channels_override else resolve_export_channels(
            config.audio_channels, config.audio_channel_layout
        )
        samples_per_frame = int(sample_rate / fps) if fps > 0 else 0
        # P1-15: 总样本数与逐帧定位走精确有理数换算（float 路径长时长会漂移）
        from src.features.edit.core.interchange import frames_to_samples

        fps_rational = self._get_sequence_fps_rational()
        total_samples = (
            frames_to_samples(end_frame - start_frame, fps_rational, sample_rate)
            if fps > 0
            else 0
        )

        if total_samples <= 0:
            return

        # D06（第六轮审计）：逐帧样本边界用精确有理数换算的相邻差。
        # 固定 floor(sr/fps) 块长在 30000/1001 这类非整数帧率下每帧
        # 少解 0.6/1.2 个样本，输出缓冲按精确边界落位时留下零样本洞
        # （实测 300 帧 29.97fps 每声道 180 个额外零样本）。
        frame_count = max(0, int(end_frame) - int(start_frame))
        frame_sample_bounds = [
            frames_to_samples(f, fps_rational, sample_rate)
            for f in range(frame_count + 1)
        ]

        # AM-03: 用共享 AudioGraphRenderer 处理节点，与实时 mix_samples 节点顺序一致。
        # 此前导出仅做 volume/pan + 硬削波，缺失 EQ/FX/automation/Bus/master/软限幅。
        from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
        from src.features.edit.core.audio_dsp import EffectChain

        # P1-02: 嵌套序列音频展开——嵌套片段引用的子序列音频轨平移进父
        # 坐标作为虚拟轨（轨参数/Bus 拓扑保留）。与实时播放共用同一纯函数。
        # The strict expansion above also protects independent master calls;
        # a caller-provided preview cache cannot bypass delivery validation.
        renderer = AudioGraphRenderer(float(sample_rate))
        renderer.configure_from_sequence(audio_sequence)

        # 收集可见的音频轨道（含 Bus 轨——Bus 无 clips 但需参与 bus stage）
        has_solo = any(t.is_solo for t in audio_sequence.audio_tracks)
        # RMA-04: 计算 solo 可听闭包（替代 is_track_renderable 的旧 solo 分支）。
        # 旧逻辑把 solo 普通轨必经的 Bus、solo Bus 的上游普通轨错误剔除。
        audible_solo = renderer.compute_audible_solo_closure() if has_solo else set()
        audio_tracks = []
        bus_tracks = []
        for track in audio_sequence.audio_tracks:
            if not is_track_renderable(track, has_solo):
                continue
            # RMA-04: solo 闭包过滤——可见/启用/未静音之外，还要在可听闭包内。
            if has_solo and track.track_id not in audible_solo and not getattr(track, "is_bus", False):
                continue
            if getattr(track, "is_bus", False):
                bus_tracks.append(track)
            elif len(track.clips) > 0:
                # v1.6 M-B8：stems 角色过滤（None = 主混音全轨）
                if only_track_ids is not None and track.track_id not in only_track_ids:
                    continue
                audio_tracks.append(track)

        if not audio_tracks and not bus_tracks:
            logger.info("No renderable audio tracks, skipping audio export")
            return

        if only_track_ids is not None:
            logger.info(
                "Rendering audio stem: %d tracks (%d total), %d samples",
                len(audio_tracks), len(only_track_ids), total_samples,
            )

        logger.info(
            f"Rendering audio: {len(audio_tracks)} tracks + {len(bus_tracks)} buses, "
            f"{total_samples} samples, {sample_rate}Hz"
        )

        # 混音输出缓冲区（master）+ bus 输入缓冲
        output = np.zeros((channels, total_samples), dtype=np.float32)
        bus_inputs: dict = {}

        # 为每个 clip 打开 AudioDecoder 并解码
        decoders: Dict[str, AudioDecoder] = {}

        try:
            for track in audio_tracks:
                # 为该 clip 准备 per-clip FX 链（pre-fader）
                clip_fx_cache: dict = {}
                # D04：动画 FX 的逐帧参数采样器缓存（None=静态，无逐帧更新）
                _clip_fx_cache_fx: dict = {}

                for clip in track.clips:
                    clip_start = clip.timeline_start
                    clip_end = clip.timeline_start + clip.timeline_duration

                    overlap_start = max(clip_start, start_frame)
                    overlap_end = min(clip_end, end_frame)

                    if overlap_start >= overlap_end:
                        continue

                    # A2: 冻结段无对应音频进度——导出混音跳过冻结 clip
                    if getattr(clip, "freeze_source_frame", None) is not None:
                        continue

                    try:
                        # 获取 asset 路径
                        asset = self._project.assets.get(clip.asset_id) if self._project else None
                        if asset is None:
                            continue
                        # P1-02: 嵌套虚拟素材不直接解码——其音频已由
                        # expand_sequence_audio_for_render 展开成虚拟轨。
                        if getattr(asset, "is_nested_sequence", False):
                            continue

                        # P1-14: 导出取源走项目代理策略（默认强制 master 原片）
                        asset_path = export_source_path(self._project, asset)
                        if not asset_path:
                            continue
                        # F2.10/M03（S05）：外录绑定——音频消费切到 WAV 源。
                        # 1x 直读按偏移折算源帧；retime/保调片段同样使用
                        # WAV（偏移经 reader 的 source_offset_seconds 平移
                        # 源窗，与实时预览同口径——此前变速/保调静默回退
                        # 机内声，同一片段预览/成片音源不一致）。仅 WAV
                        # 素材缺失/不可读才回退机内声（留警告，非静默）。
                        from src.features.edit.core.audio_sync import (
                            binding_for_asset,
                            bound_audio_frame,
                        )
                        _binding = binding_for_asset(
                            getattr(self._project, "audio_sync_bindings", None)
                            or {}, clip.asset_id)
                        if _binding is not None:
                            _wav = self._project.assets.get(
                                _binding.wav_asset_id)
                            _wav_path = getattr(_wav, "path", "") \
                                if _wav is not None else ""
                            if _wav_path:
                                asset_path = _wav_path
                                _audio_offset = _binding.offset_seconds
                            else:
                                logger.warning(
                                    "clip %s: bound WAV asset unreadable "
                                    "(%s) — falling back to camera audio",
                                    clip.clip_id, _binding.wav_asset_id)
                                _binding = None
                        if _binding is not None:
                            _audio_offset = _binding.offset_seconds
                        else:
                            _audio_offset = 0.0

                        # 创建或复用 decoder（绑定源独立缓存键防串音）
                        _decoder_key = (
                            clip.asset_id, asset_path) if _binding \
                            else clip.asset_id
                        if _decoder_key not in decoders:
                            decoder = AudioDecoder()
                            # 传入 timeline_fps 与 output_sample_rate，保证音视频同步
                            decoder.open(asset_path, timeline_fps=fps, output_sample_rate=sample_rate)
                            decoders[_decoder_key] = decoder
                        else:
                            decoder = decoders[_decoder_key]

                        # 构建 per-clip FX 链（与实时 _sync_clip_audio_effects 一致）。
                        # D04（第六轮审计）：animated_params 由 clip_fx_entries 按
                        # 绝对时间线帧求值覆盖——旧代码只 set_params 一次静态值，
                        # 关键帧对导出声音零影响。
                        from src.features.edit.core.audio_retime import (
                            animated_fx_sampler,
                            clip_fx_entries as _clip_fx_entries,
                        )

                        clip_effects = sorted(
                            getattr(clip, 'effects', ()) or (),
                            key=lambda ef: getattr(ef, 'order', 0),
                        )
                        clip_chain = clip_fx_cache.get(clip.clip_id)
                        if clip_chain is None and clip_effects:
                            static_entries = _clip_fx_entries(clip_effects, None)
                            if static_entries:
                                chain = EffectChain(float(sample_rate))
                                chain.set_params(
                                    static_entries, sample_rate=float(sample_rate))
                                clip_fx_cache[clip.clip_id] = chain
                                clip_chain = chain
                        fx_sampler = _clip_fx_cache_fx.get(clip.clip_id)
                        if fx_sampler is None and clip_effects:
                            fx_sampler = animated_fx_sampler(clip_effects)
                            if fx_sampler is not None:
                                _clip_fx_cache_fx[clip.clip_id] = fx_sampler

                        # P1-08/R05（第七轮审计）：preserve-pitch 离线路径
                        # 改走共享 PreservedPitchReader——旧 helper 运行时
                        # np 未定义被 except 静默吞掉，实际回退变调；Stem
                        # 路径则明确回退变调，两者听感不一致。倒放/速度
                        # 曲线等离线不支持组合在此显式留痕回退变调。
                        preserve_reader = None
                        from src.features.edit.core.audio_retime import (
                            ClipRetimeReader,
                            PreservedPitchReader,
                            clip_source_fps as _clip_source_fps,
                            needs_retime as _needs_retime,
                            preserve_pitch_request as _pp_request,
                        )
                        _pp_requested, _pp_speed = _pp_request(clip)
                        if _pp_requested and _pp_speed is None:
                            logger.warning(
                                "clip %s preserve-pitch 不支持倒放/速度曲线，回退变调导出",
                                clip.clip_id,
                            )
                        elif _pp_requested:
                            _pp_reader = PreservedPitchReader(
                                decoder, clip, float(fps),
                                int(samples_per_frame), float(sample_rate),
                                cancel_check=lambda: self._cancel_requested,
                                source_offset_seconds=_audio_offset,
                            )
                            if _pp_reader.available:
                                preserve_reader = _pp_reader
                            else:
                                logger.warning(
                                    "clip %s preserve-pitch 预解码失败，回退变调路径",
                                    clip.clip_id,
                                )

                        # D01（第六轮审计）：非保调 retime（reverse/速度曲线/普通
                        # 变速）改走共享 ClipRetimeReader——canonical
                        # timeline_to_source_frame_pair 映射 + 样本域线性重采样，
                        # 与视频读源同源。旧公式 source_start + d*scalar_speed
                        # 使倒放/曲线音频与 1x 完全相同、2x 不变调。
                        retime_reader = None
                        if preserve_reader is None and _needs_retime(clip):
                            reader = ClipRetimeReader(
                                decoder, clip, float(fps),
                                int(samples_per_frame),
                                cancel_check=lambda: self._cancel_requested,
                                sample_rate=float(sample_rate),
                                source_offset_seconds=_audio_offset,
                            )
                            if reader.available:
                                retime_reader = reader
                            else:
                                logger.warning(
                                    "clip %s retime 窗口不可用，回退旧读源公式",
                                    clip.clip_id,
                                )

                        # F3.3 · 子帧偏移门：时间线侧延迟 + 源侧子帧修剪，
                        # 逐帧块循环消费（与实时 decoder._apply_skip_gates
                        # 同语义——预览/成片同刻；仅在 clip 真实入点起作用）。
                        from src.features.edit.core.sample_time import (
                            SampleSkipGate as _ClipSkipGate,
                        )
                        _offset_total = int(getattr(
                            clip, "audio_sample_offset", 0) or 0) + int(
                            getattr(clip, "source_sample_offset", 0) or 0)
                        _clip_skip_gate = _ClipSkipGate(_offset_total)

                        # R06：1x 直读的定位时钟用素材源帧率（元数据缺失
                        # 退化用序列 fps，行为与旧版一致）。
                        source_fps_for_clip = _clip_source_fps(clip, float(fps))

                        # 逐帧解码
                        for frame_idx in range(overlap_start, overlap_end):
                            # 取消语义（P1-01）：导出取消后不再为音频解码/混音
                            if self._cancel_requested:
                                return None
                            # D06：逐帧块长用精确相邻样本边界差（非整数 fps 时
                            # floor(sr/fps) 固定块长会在每帧边界留下零样本洞）。
                            rel = frame_idx - start_frame
                            out_start = frame_sample_bounds[rel]
                            out_end = frame_sample_bounds[rel + 1]
                            block_len = out_end - out_start
                            if block_len <= 0:
                                continue

                            if preserve_reader is not None:
                                samples = preserve_reader.read_frame(
                                    frame_idx, block_len)
                                if samples is None:
                                    continue
                            elif retime_reader is not None:
                                samples = retime_reader.read_frame(
                                    frame_idx, block_len)
                                if samples is None:
                                    continue
                            else:
                                # 1x 直读：块长用精确边界差（D06），源帧沿用
                                # canonical 映射（含混合 fps/相位修正）。
                                # R06：解码定位用源帧率时钟。
                                source_frame = clip.timeline_to_source_frame(frame_idx)
                                if _audio_offset:
                                    source_frame = bound_audio_frame(
                                        source_frame, _audio_offset,
                                        source_fps_for_clip)
                                samples = decoder.decode_samples_for_frame(
                                    source_frame, source_fps_for_clip, block_len,
                                )

                            if samples is None:
                                continue
                            if samples.ndim == 1:
                                samples = samples.reshape(1, -1)
                            samples = _clip_skip_gate.feed(samples)
                            if samples.shape[-1] == 0:
                                continue
                            if samples.shape[-1] < out_end - out_start:
                                # R04（第七轮审计）：解码器返回短块（AAC 帧
                                # 粒度/音频流先于视频结束）时补零到精确块长。
                                # 旧的 += 广播异常会被外层 except 捕获后跳过
                                # 整个 clip 余下音频——成片从首个短块起整段
                                # 静音仍标 done。
                                samples = np.pad(
                                    samples,
                                    ((0, 0),
                                     (0, out_end - out_start - samples.shape[-1])),
                                )
                            actual = min(samples.shape[-1], out_end - out_start)

                            # D04：动画 FX 参数按当前帧求值更新链（静态链不更新）。
                            if clip_chain is not None and fx_sampler is not None:
                                try:
                                    clip_chain.set_params(
                                        fx_sampler(frame_idx),
                                        sample_rate=float(sample_rate),
                                    )
                                except Exception:  # noqa: BLE001 — 参数更新失败沿用上一帧
                                    logger.debug(
                                        "clip %s animated fx update failed at %d",
                                        clip.clip_id, frame_idx, exc_info=True,
                                    )

                            # AM-08 阶段 4.1：声道映射用标准 downmix（替代 naive 截断）。
                            # 此前 samples[:channels] 直接丢弃 Center/LFE/Surround，
                            # 现用 ChannelLayout.downmix 保留全部声道贡献（ITUR BS.775）。
                            # P1-09：传入 decoder 的容器布局（素材真相），5.1(side)
                            # 等素材不再被按声道数误判；未知布局回退必留警告日志。
                            from src.features.edit.core.channel_layout import (
                                adapt_samples_for_delivery,
                            )
                            from src.features.edit.core.source_patch import (
                                patch_for_asset,
                            )
                            if samples.ndim == 1:
                                samples = np.broadcast_to(
                                    samples.reshape(1, -1),
                                    (1, samples.shape[0]),
                                )
                            # F2.1 · Source Patching：素材级源声道映射先于交付
                            # downmix 选道（与预览/Stem 同语义）。映射存在但未
                            # 生效（解码声道数与映射基线不符）时留警告并直通。
                            _src_layout = getattr(decoder, "source_layout", None)
                            _payload = samples[:, :actual]
                            # F3.4 修复：getattr 防御——duck-typed 工程
                            # （测试替身/旧版工程对象）无 source_patch_overrides
                            # 时此前直接 AttributeError，外层 except 吞掉后
                            # 整 clip 静音（stems 全静且彼此相等）。
                            _patch = patch_for_asset(
                                getattr(self._project,
                                        "source_patch_overrides", None) or {},
                                clip.asset_id)
                            _selected = _patch.select_channels(_payload)
                            if _selected is not _payload:
                                _payload = _selected
                                # 选道后的布局是自定义的，不能再声明容器布局
                                _src_layout = None
                            elif _patch.audio_channel_map:
                                logger.warning(
                                    "clip %s source patch map %s skipped: "
                                    "decoded %d channels",
                                    clip.clip_id, _patch.audio_channel_map,
                                    _payload.shape[0])
                            samples = adapt_samples_for_delivery(
                                _payload, channels,
                                src_layout=_src_layout,
                                context=f"clip {clip.clip_id}",
                            )

                            # C3: 片段级增益（clip gain → renderer 节点链之前）
                            _clip_gain_db = float(
                                getattr(clip, "audio_gain_db", 0.0) or 0.0)
                            if abs(_clip_gain_db) > 1e-9:
                                samples = samples * (10.0 ** (_clip_gain_db / 20.0))

                            # AM-03/AM-06: 用共享 renderer 处理节点（clip 淡化/automation/clip-FX/volume/pan/EQ/FX）
                            clip_fade = {
                                "timeline_start": clip.timeline_start,
                                "timeline_duration": clip.timeline_duration,
                                "fade_in_frames": int(getattr(clip, "fade_in_duration", 0) or 0),
                                "fade_out_frames": int(getattr(clip, "fade_out_duration", 0) or 0),
                                "fps": fps,
                                # F3.3：采样域淡化/子帧偏移（预览/成片同键）
                                "fade_in_samples": getattr(
                                    clip, "fade_in_samples", None),
                                "fade_out_samples": getattr(
                                    clip, "fade_out_samples", None),
                                "audio_sample_offset": int(getattr(
                                    clip, "audio_sample_offset", 0) or 0),
                            }
                            # AT: 若该 clip 参与音频转场，附加 crossfade 参数供 renderer
                            # 在 clip fade 之后叠加。一个 clip 理论上可能在多个转场中
                            # 出现（一左一右），这里收集所有命中实例。
                            if getattr(track, "transitions", None):
                                from src.features.edit.core.audio_transition_catalog import (
                                    audio_curve_for,
                                )
                                xfades = []
                                for tr in track.transitions:
                                    role = None
                                    if tr.left_clip_id == clip.clip_id:
                                        role = "left"
                                    elif tr.right_clip_id == clip.clip_id:
                                        role = "right"
                                    if role is None:
                                        continue
                                    curve = audio_curve_for(tr.transition_type)
                                    if curve is None:
                                        continue
                                    xfades.append({
                                        "role": role,
                                        "curve": curve,
                                        "start_frame": tr.start_frame,
                                        "duration": tr.duration,
                                    })
                                if xfades:
                                    clip_fade["crossfade"] = xfades
                            processed = renderer.render_track_block(
                                track.track_id, samples, frame_idx,
                                clip_fx_chain=clip_chain,
                                clip_fade=clip_fade,
                                fps=fps,
                            )
                            if processed is None:
                                continue  # automation mute

                            # 路由到该帧对应的 output 位置（bus_inputs 或 master）。
                            # 直接累积到全局缓冲的 [out_start:out_end] 切片，避免每帧
                            # 分配全长度缓冲（O(total_samples) per frame 的低效）。
                            node = renderer.get_node(track.track_id)
                            target = node.output_bus_id if node else None
                            if target is not None:
                                if target not in bus_inputs:
                                    bus_inputs[target] = np.zeros((channels, total_samples), dtype=np.float32)
                                bus_inputs[target][:, out_start:out_end] += processed[:channels, :actual]
                            else:
                                output[:, out_start:out_end] += processed[:channels, :actual]
                            # sends（并行）
                            if node is not None:
                                for send_bus_id, send_level in node.sends:
                                    if send_level <= 0.0:
                                        continue
                                    if send_bus_id not in bus_inputs:
                                        bus_inputs[send_bus_id] = np.zeros((channels, total_samples), dtype=np.float32)
                                    bus_inputs[send_bus_id][:, out_start:out_end] += processed[:channels, :actual] * send_level

                    except Exception as e:
                        logger.warning(f"Failed to decode audio for clip {clip.clip_id}: {e}")
                        continue

        finally:
            # D07（第六轮审计）：取消（循环内 return None）与异常路径
            # 同样关闭全部 decoder——旧代码取消时提前退出绕过此处。
            for d in decoders.values():
                try:
                    d.close()
                except Exception:
                    pass

        # AM-03/AM-14: Bus 阶段（拓扑序处理，支持多层 Bus→Bus）
        # RMA-04: 传入可听闭包，让 solo 普通轨必经的 Bus 也参与处理。
        # RMA-06: 传入 start_frame/fps 让 Bus automation（volume/pan/mute）逐样本求值。
        renderer.render_bus_stage(bus_inputs, output, has_solo=has_solo,
                                  audible_solo=audible_solo,
                                  start_frame=start_frame, fps=fps)

        # AM-03: Master 阶段（volume/mute + 软限幅，替代此前的硬削波 np.clip）
        # §3.1: 传入 start_frame/fps 供 master automation ramp
        output = renderer.apply_master(output, start_frame=start_frame, fps=fps)

        # AM-10 阶段 3.4：离线响度交付。
        # 若指定 loudness_target_lufs，则扫描 master PCM：
        # 1) 生成 LoudnessReport（integrated/true-peak/range/normalize 增益/警告）。
        # 2) 应用 normalize 增益（再次过 master limiter 保证不削波），再写入编码器。
        # 3) 报告写入 loudness_report_path（.json + .txt），与导出文件一起保存。
        # 设计原则（计划表 3.4）：用户显式指定 target 才 normalize，不破坏性改源。
        # v1.6 M-B8：normalize 增益按主混音测量，对角色轨无意义——stems
        # 渲染（only_track_ids 非 None）跳过响度处理。
        if config.loudness_target_lufs is not None and only_track_ids is None:
            from src.features.edit.core.loudness_analyzer import (
                LoudnessAnalysisCancelled,
                analyze_loudness,
            )

            # N23/RF07：取消谓词传入两次响度扫描——R27 只修了 analyzer
            # 函数本身，导出调用方此前不传 progress，首次 cancel 后仍跑
            # 第二次扫描、写报告并发布 last_report。
            def _loudness_alive(_progress: float) -> bool:
                return not self._cancel_requested

            try:
                report = analyze_loudness(
                    output, float(sample_rate),
                    target_lufs=float(config.loudness_target_lufs),
                    progress=_loudness_alive,
                )
                gain_db = report.normalize_gain_db
                if gain_db is not None and np.isfinite(gain_db):
                    # 应用 normalize 增益（线性 scale）
                    scale = float(10.0 ** (gain_db / 20.0))
                    output = output * scale
                    # 安全限幅：normalize 后峰值可能超 1.0，过 master limiter
                    output = renderer.apply_master(output, skip_volume=True)
                    logger.info(
                        f"Loudness normalize: {report.integrated_lufs:.1f} → "
                        f"{config.loudness_target_lufs:.1f} LUFS "
                        f"(gain {gain_db:+.1f} dB)"
                    )

                # N23/RF07：normalize 后、第二次扫描与报告发布前再核验
                # 取消——取消期间不得发布任何结果
                if self._cancel_requested:
                    raise LoudnessAnalysisCancelled()

                # 复测 normalize 后的实际响度（写入报告供审计）
                post_report = analyze_loudness(
                    output, float(sample_rate),
                    target_lufs=float(config.loudness_target_lufs),
                    progress=_loudness_alive,
                )

                # 写报告文件
                if config.loudness_report_path:
                    self._write_loudness_report(
                        config.loudness_report_path,
                        report, post_report, sample_rate,
                    )

                self._last_loudness_report = post_report
            except LoudnessAnalysisCancelled:
                # 显式取消向上传播（主循环的 cancel 检查裁决为
                # user_cancelled），不被下面的宽泛 except 吞成"分析失败
                # 继续交付"
                logger.info("Loudness scan cancelled by user")
                raise
            except Exception as e:
                logger.warning(f"Loudness analysis failed: {e}")

        # 写入编码器的职责已上移到调用方（串行 wrapper 或导出主线程），
        # 本方法只负责产出 master PCM。
        return output

    def _render_stem_masters(
        self,
        start_frame: int,
        end_frame: int,
        config: TimelineExportConfig,
        encoder: Any = None,
    ) -> "list":
        """逐角色渲染 stems 母带（v1.6 M-B8）：复用 `_render_audio_master`
        按 `only_track_ids` 过滤（同解码/混音/Bus/Master 链，仅轨集合不同）。

        返回与 `self._audio_stem_tracks` 等长的 PCM 列表；某组无可渲染轨
        时对应元素为 None（该轨跳过写入，其余轨不受影响）。
        """
        stems = getattr(self, "_audio_stem_tracks", None) or []
        if not stems:
            return []
        from src.features.edit.core.channel_layout import resolve_export_channels

        audio_sequence = expand_sequence_audio_for_render(
            self._sequence, self._sequence_resolver, self._project,
            strict=True, frame_range=(start_frame, end_frame),
        )
        results: "list" = []
        for stem, _track_idx in stems:
            if self._cancel_requested:
                return results + [None] * (len(stems) - len(results))
            track_ids = resolve_stem_track_ids(audio_sequence, stem.stem_group)
            if not track_ids:
                logger.info(
                    "Stem '%s'（%s）无可渲染音轨，该容器轨跳过",
                    stem.name or stem.stem_group, stem.stem_group)
                results.append(None)
                continue
            pcm = self._render_audio_master(
                start_frame, end_frame, config, encoder=encoder,
                only_track_ids=frozenset(track_ids),
                channels_override=resolve_export_channels(
                    stem.channels, stem.channel_layout),
            )
            results.append(pcm)
        return results

    def _declare_audio_stems(self, config: TimelineExportConfig,
                             encoder: Any = None) -> "list":
        """声明 stems 追加轨（v1.6）。编码器不支持/声明失败时整体降级
        单轨（返回空列表并记录警告）——部分轨丢失属静默交付缺陷，
        all-or-nothing。返回 [(AudioStemExportTrack, 轨索引)]。

        无可渲染音轨的组在声明前剔除：容器 v1.6 语义"声明即须有样本"
        （finish fail-fast），空组若占声明位会令导出失败；剔除留
        info 日志。
        """
        encoder = encoder if encoder is not None else self._encoder
        declare = getattr(encoder, "add_audio_track_stream", None)
        if not callable(declare):
            if config.audio_stems:
                logger.warning(
                    "目标编码器不支持 stems 多音轨：audio_stems 已降级为"
                    "单音轨导出（轨 0 主混音不变）")
            return []
        from src.features.edit.core.channel_layout import resolve_export_channels

        audio_sequence = getattr(self, "_audio_render_sequence", None)
        if audio_sequence is None:
            audio_sequence = expand_sequence_audio_for_render(
                self._sequence, self._sequence_resolver,
                self._project if self._project else None,
                strict=True, frame_range=self._calculate_frame_range(config),
            )
        declared: "list" = []
        try:
            for stem in config.audio_stems or ():
                if not resolve_stem_track_ids(audio_sequence, stem.stem_group):
                    logger.info(
                        "Stem '%s'（%s）无可渲染音轨，跳过声明",
                        stem.name or stem.stem_group, stem.stem_group)
                    continue
                name = str(stem.name or "").strip() or stem.stem_group.capitalize()
                idx = declare(
                    name=name,
                    codec=stem.codec,
                    sample_rate=config.audio_sample_rate,
                    channels=resolve_export_channels(
                        stem.channels, stem.channel_layout),
                    bitrate=stem.bitrate,
                )
                declared.append((stem, int(idx)))
        except Exception as e:
            logger.warning("stems 追加轨声明失败，已降级单音轨导出: %s", e)
            return []
        return declared

    def _write_stem_tracks(self, config: TimelineExportConfig,
                           encoder: Any = None) -> None:
        """把各角色母带 PCM 串行写入容器追加轨（主混音写入之后调用）。

        单轨写入失败降级警告——视频与主混音交付不受损（与音频写入
        既有降级语义一致）。
        """
        encoder = encoder if encoder is not None else self._encoder
        write = getattr(encoder, "write_audio_track_samples", None)
        if not callable(write):
            return
        stems = getattr(self, "_audio_stem_tracks", None) or []
        pcms = getattr(self, "_audio_stem_pcms", None) or []
        for (stem, idx), pcm in zip(stems, pcms):
            if pcm is None:
                continue
            try:
                write(idx, pcm, config.audio_sample_rate)
                logger.info("Stem 轨已写入：'%s' → 容器追加轨 %d",
                            stem.name or stem.stem_group, idx)
            except Exception as e:
                logger.warning(
                    "Stem 轨 '%s' 写入失败（其余轨交付不受损）: %s",
                    stem.name or stem.stem_group, e)
        self._audio_stem_pcms = []

    def _write_loudness_report(
        self, path: str, pre_report: Any, post_report: Any, sample_rate: int,
    ) -> None:
        """写入响度报告文件（JSON + 人类可读 TXT，AM-10 阶段 3.4）。

        Args:
            path: 报告路径（.json，同名 .txt 一并生成）
            pre_report: normalize 前的 LoudnessReport
            post_report: normalize 后的 LoudnessReport（写入文件的实际响度）
            sample_rate: 采样率
        """
        import json
        try:
            data = {
                "schema": "topos.loudness_report.v1",
                "sample_rate": int(sample_rate),
                "pre_normalize": pre_report.to_dict(),
                "post_normalize": post_report.to_dict(),
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            # 同名 .txt（人类可读）
            txt_path = os.path.splitext(path)[0] + ".txt"
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write("=== Pre-normalize ===\n")
                f.write(pre_report.to_text())
                f.write("\n\n=== Post-normalize (written to file) ===\n")
                f.write(post_report.to_text())
                f.write("\n")
            logger.info(f"Loudness report written: {path}")
        except Exception as e:
            logger.warning(f"Failed to write loudness report: {e}")

    def _resolve_export_color_metadata(self) -> "ColorMetadataConfig":
        """Derive the encoder color metadata for the export.

        Walks the timeline's video tracks and uses the first clip's
        persisted color metadata (primaries/transfer/matrix/range, captured
        at import in step 3) to tag the output stream. Falls back to
        ``ColorMetadataConfig.sdr()`` (standard BT.709 SDR) when no source
        metadata is available.

        G1（2026-09-21 复查补）：.toos 静图/序列素材入轨为 image 资产，
        此前 `is_video` 过滤把它们全部跳过——浮点/linear 序列导出永远
        静默落 SDR。现在 video 轨上任意媒体类型的色彩标签均参与。
        """
        from src.shared.export.video_encoder import ColorMetadataConfig

        try:
            for track in self._sequence.all_tracks:
                track_type: str = (
                    getattr(track, "track_type", None) or getattr(track, "kind", "")
                )
                if track_type != "video":
                    continue
                for clip in track.clips:
                    asset = self._project.assets.get(getattr(clip, "asset_id", ""))
                    if asset is None:
                        continue
                    meta = getattr(asset, "metadata", {}) or {}
                    cm = {
                        k: meta.get(k)
                        for k in ("color_primaries", "color_transfer",
                                  "color_matrix", "color_range")
                        if meta.get(k)
                    }
                    if cm.get("color_primaries") and cm.get("color_transfer"):
                        # RC6：RAW 域 transfer 标签（TRAW-LOG0）只描述源
                        # CFA 数据的编码域。经 CFA 交付出口（debayer→IDT
                        # gamut→工作空间）或普通渲染链后的输出已是显示域
                        # 画面——RAW 标签不得透传（Topos 编码器非 raw 档
                        # 会显式拒绝），回退标准 SDR。
                        transfer_l = str(cm["color_transfer"]).lower()
                        if transfer_l in ("traw-log0", "topos-traw-log0"):
                            logger.debug(
                                "export color: RAW-domain transfer %r not "
                                "propagated to re-encoded output (RC6); "
                                "falling back to SDR", cm["color_transfer"])
                            return ColorMetadataConfig.sdr()
                        # G1：GBR（H.273 matrix 0 = identity）是 RGB 平面
                        # 内容的标签——再编码 YUV/GBR 输出时编码器矩阵码
                        # 表无 'gbr'（ValueError）。GBR 平面本身矩阵恒等，
                        # 按 bt709 近似标注（与 G4 代理链同口径）。
                        matrix = str(cm.get("color_matrix", "bt709")).lower()
                        if matrix in ("gbr", "gbrp", "identity", "rgb", "0"):
                            matrix = "bt709"
                        return ColorMetadataConfig(
                            primaries=cm["color_primaries"],
                            transfer=cm["color_transfer"],
                            matrix=matrix,
                            range=cm.get("color_range", "limited"),
                        )
                    break  # first clip had no color metadata
                break
        except Exception as e:
            logger.debug(f"export color metadata resolve failed: {e}")
        return ColorMetadataConfig.sdr()

    def _create_encoder_config(
        self,
        config: TimelineExportConfig,
    ) -> "FFmpegEncoderConfig":
        """
        创建编码器配置。

        Args:
            config: 导出配置

        Returns:
            FFmpegEncoderConfig 实例
        """
        from src.shared.export.video_encoder import FFmpegEncoderConfig
        from src.shared.media.metadata import Rational

        # 确定分辨率（TimelineSequence 的 width/height 是直接字段，无 settings 属性）
        if config.resolution:
            width, height = config.resolution
        else:
            width, height = self._get_sequence_resolution()

        # 确定帧率
        # P1-15: 未显式覆盖时直接使用时间线精确 Rational——毫秒量化
        # （23.976 ≠ 24000/1001）会在长时长导出中造成系统性 A/V 漂移。
        if config.fps:
            fps = Rational.from_float(float(config.fps))
        else:
            fps = self._get_sequence_fps_rational()

        # 确定编码器和像素格式
        codec, pix_fmt, profile = self._get_codec_settings(config)
        # HEVC：profile 与交付位深对齐（main/main10 由 pix_fmt 决定，
        # "auto"/冲突值归一，显式不冲突值保留）
        resolved_profile = config.codec_profile or profile
        if codec == "hevc":
            resolved_profile = _normalize_hevc_profile(
                resolved_profile, pix_fmt)
        if config.codec == ExportCodec.TOPOS_RAW:
            # M4-R8：编码器档位解析域是视频 tier id——比率档 id
            # （'raw12-4'…）只用于 pix_fmt/qp 换算，不得透传
            resolved_profile = "raw"

        # 色彩元数据：优先从时间线首个视频片段的源元数据推导（步骤3已持久化），
        # 否则回退到标准 SDR (BT.709 / limited)。这样 Rec.709 源导出仍标 Rec.709，
        # 而不是被硬编码成同样的 SDR。
        color_metadata = self._resolve_export_color_metadata()

        # 构造 kwargs，按需添加 CRF / VBR / CBR 码率控制
        encoder_kwargs = dict(
            codec=codec,
            width=width,
            height=height,
            fps=fps,
            pix_fmt=pix_fmt,
            profile=resolved_profile,
            color_metadata=color_metadata,
            # threads=0（FFmpeg 自动 = 全核）：导出是独占式批处理，
            # libx264/x265/dnxhd 的线程扩展显著（4K 实测 threads=4 → 0
            # 各有 2-2.6×）；prores_ks 收益小但无回退。
            threads=0,
            # 后台线程转换帧格式，与编码重叠（4K 实测 DNxHR 12→20fps）
            pipelined_conversion=True,
            # 3 路帧级并行转换：4K 10bit 单帧转换 ~45ms 是端到端地板
            # （AMF 编码仅 13ms），帧级并行把它推到 max(≈15, 编码) 段
            # （2026-09-12 分环节实测）；in-flight 内存上界 ~4 帧
            convert_workers=3,
            # Alpha：显式开关（Topos）或带 alpha 的 planar pix_fmt 隐含。
            # Topos 的注册名是 topos_yuva*/topos_gbrap*，不能只检查
            # FFmpeg 的 yuva* 前缀，否则显式 Alpha pix_fmt 会在配置层被
            # 错当作 opaque，进而阻断直通或静默丢掉第 4 平面。
            has_alpha=bool(getattr(config, 'has_alpha', False))
                      or pix_fmt.startswith((
                          'yuva', 'topos_yuva', 'topos_gbrap')),
            gif_color_count=int(getattr(config, 'gif_color_count', 256)),
        )
        if codec == 'gif':
            # GIF is opaque in the first implementation; its palette is
            # controlled by GifVideoEncoder rather than FFmpeg quality flags.
            encoder_kwargs['has_alpha'] = False
        if codec == 'topos':
            # R7：Alpha 预算超限策略透传（FFmpegEncoderConfig 字段，仅
            # Topos encoder 消费；域外值在 encoder 构造期显式报错）
            encoder_kwargs['alpha_budget_policy'] = str(
                getattr(config, 'alpha_budget_policy', 'record') or 'record')
            # v1.8（ADR-C056）lp 帧间档约束前置（编码器构造期同规则
            # fail-fast，双保险；这里先给导出语境的可读错误）：
            if profile == 'lp':
                if encoder_kwargs['has_alpha']:
                    raise ValueError(
                        "Topos 422 LP（帧间档）不支持 Alpha——请关闭 Alpha "
                        "输出，或改用支持 Alpha 的档位（4444/4444 XQ）")
                if config.include_audio:
                    # GOP 分段并行（ADR-C050）与音轨声明互斥（ADR-C053
                    # 共存路线未落地）。带音频导出显式单 GOP 保音频可用；
                    # 分段收益集中在 ≤2K 摇移类素材，静音时间线（include_
                    # audio=False）不受影响。
                    encoder_kwargs['gop_size'] = 0

        # H.264/HEVC 支持 CRF、VBR 目标码率或 CBR 码率控制；Topos 的
        # quality 按 CRF 语义映射为 codec QP（0–63，档位默认码控的覆盖）；
        # 其他 codec 忽略 quality。
        if codec in ('h264', 'hevc', 'topos') and config.quality is not None:
            quality_mode = str(config.quality_mode or "auto").lower()
            if codec == 'topos':
                if quality_mode in ('crf', 'auto'):
                    encoder_kwargs['crf'] = int(config.quality)
            elif quality_mode in ("vbr", "bitrate"):
                # "bitrate" is the legacy name for the former target-bitrate
                # mode. Keep it as VBR when loading old queue entries.
                encoder_kwargs['bitrate'] = int(config.quality)
            elif quality_mode == "cbr":
                target_bitrate = int(config.quality)
                encoder_kwargs['bitrate'] = target_bitrate
                encoder_kwargs['min_bitrate'] = target_bitrate
                encoder_kwargs['max_bitrate'] = target_bitrate
                # A 2x VBV buffer is a conventional starting point for CBR.
                encoder_kwargs['buffer_size'] = target_bitrate * 2
            else:
                # Legacy auto-with-a-value and explicit CRF mode use CRF.
                encoder_kwargs['crf'] = int(config.quality)

        if codec == 'topos' and profile == 'raw':
            # M4-R7/R8：RAW 比率锚表（与图片线 12 档共享）——quality 显式
            # 给了 crf 时尊重调用方；否则 codec_profile 携带的注册表比率档
            # 定锚，缺省 raw12-4（家族默认 4:1）。编码器对 raw + 无 qp 显式
            # 报错（比率标签语义，qp20 近损默认不适用），此处兜底换算。
            if encoder_kwargs.get('crf') is None:
                from src.shared.codec.topos_profiles import (
                    get_topos_image_tier,
                    get_topos_image_tier_or_none,
                )
                raw_tier = get_topos_image_tier_or_none(
                    getattr(config, 'codec_profile', None))
                if raw_tier is None or raw_tier.family != 'raw':
                    raw_tier = get_topos_image_tier('raw12-4')
                encoder_kwargs['crf'] = int(raw_tier.qp)

        return FFmpegEncoderConfig(**encoder_kwargs)

    def _get_codec_settings(
        self,
        config: TimelineExportConfig,
    ) -> Tuple[str, str, Optional[str]]:
        """
        获取编码器设置。

        Args:
            config: 导出配置

        Returns:
            (codec_name, pix_fmt, profile) tuple
        """
        codec_map = {
            # H.264 / H.265
            ExportCodec.H264: ("h264", "yuv420p", None),
            ExportCodec.HEVC: ("hevc", "yuv420p10le", None),
            ExportCodec.GIF: ("gif", "rgb24", None),

            # Apple ProRes 全 Profile（codec=prores, profile 区分）
            ExportCodec.PRORES_PROXY: ("prores", "yuv422p", "proxy"),
            ExportCodec.PRORES_LT: ("prores", "yuv422p", "lt"),
            ExportCodec.PRORES_422: ("prores", "yuv422p10le", "standard"),
            ExportCodec.PRORES_422HQ: ("prores", "yuv422p10le", "hq"),
            ExportCodec.PRORES_4444: ("prores", "yuva444p10le", "4444"),
            ExportCodec.PRORES_4444XQ: ("prores", "yuva444p12le", "4444xq"),

            # Avid DNxHR 全 Profile（codec=dnxhr, profile 区分）
            ExportCodec.DNXHR_LB: ("dnxhr", "yuv422p", "lb"),
            ExportCodec.DNXHR_SQ: ("dnxhr", "yuv422p", "sq"),
            ExportCodec.DNXHR_HQ: ("dnxhr", "yuv422p", "hq"),
            ExportCodec.DNXHR_HQX: ("dnxhr", "yuv422p10le", "hqx"),
            ExportCodec.DNXHR_444: ("dnxhr", "yuva444p10le", "444"),

            # Topos Video Codec（自研；profile=档位 id，编码器按档位定标）
            ExportCodec.TOPOS_PROXY: ("topos", "yuv422p10le", "proxy"),
            ExportCodec.TOPOS_LT: ("topos", "yuv422p10le", "lt"),
            ExportCodec.TOPOS: ("topos", "yuv422p10le", "standard"),
            ExportCodec.TOPOS_HQ: ("topos", "yuv422p10le", "hq"),
            # R4.6：444 档默认 pix_fmt（tier 格式交叉要求 pf≠0；用户可在
            # 导出设置改 gbrp* / a{N} 变体，encoder 配置期校验）
            ExportCodec.TOPOS_4444: ("topos", "yuv444p10le", "4444"),
            ExportCodec.TOPOS_4444XQ: ("topos", "yuv444p12le", "4444xq"),
            # v1.8（ADR-C056）：lp 帧间档——profile=档位 id；编码器据
            # tier 强制 gop='ip2'，此处只负责像素格式（422 10-bit 唯一域）
            ExportCodec.TOPOS_LP: ("topos", "yuv422p10le", "lp"),
            # v1.9（P2）：网络代理档（旧 Proxy 低码率 0.29108 bpp）
            ExportCodec.TOPOS_NETPROXY: ("topos", "yuv422p10le", "netproxy"),
            # M4-R7/R8：视频 RAW——pix_fmt 由所选比率档（codec_profile 携带
            # 注册表 id）权威决定，此处 'raw' = 视频 tier id（单一 RAW tier）
            ExportCodec.TOPOS_RAW: ("topos", "topos_cfa12le", "raw"),
        }

        # G14：PNG/TIFF 序列走 StandardImageSequenceEncoder（BGR 帧直接
        # 落盘）；渲染端保持 8/16-bit RGB 输出，编码器在 _create_encoder
        # 显式选择。这里的描述值仅供共享配置校验路径（pix_fmt 语义为
        # 渲染目标而非编码器参数）。
        if config.codec in (ExportCodec.PNG_SEQUENCE, ExportCodec.TIFF_SEQUENCE):
            return (
                "png_sequence" if config.codec == ExportCodec.PNG_SEQUENCE
                else "tiff_sequence",
                "rgb48le" if getattr(config, "bit_depth", 8) >= 16 else "rgb24",
                "standard",
            )

        if config.codec == ExportCodec.TOPOS_IMAGE_SEQUENCE:
            # M2-F8：``codec_profile`` 携带图片档位注册表 id（'422-high'/
            # 'float-ultra'…）→ pix_fmt/qp 由注册表权威（单一注册表取名，
            # E3）；未携带（历史作业/直配）保持默认 422 intra 档。
            from src.shared.codec.topos_profiles import (
                get_topos_image_tier_or_none,
            )
            image_tier = get_topos_image_tier_or_none(
                getattr(config, 'codec_profile', None))
            if image_tier is not None:
                return ("topos_image_sequence", image_tier.pix_fmt,
                        image_tier.tier_id)
            # The image-sequence encoder is selected explicitly in
            # ``_create_encoder``.  These values describe its default 422
            # intra profile and keep the shared config validation path typed.
            return ("topos_image_sequence", "yuv422p10le", "standard")

        if config.codec == ExportCodec.TOPOS_RAW:
            # M4-R8：``codec_profile`` 携带 RAW 比率档注册表 id（'raw12-4'/
            # 'raw16-2'…，与图片线 12 档共享）→ pix_fmt 由注册表权威（E3，
            # 位深含在比率档里）；未携带（Quick Export/直配）走默认 12-bit。
            # 视频 tier 恒为 'raw'（单一 RAW tier，编码器据其冻结载体域）。
            from src.shared.codec.topos_profiles import (
                get_topos_image_tier_or_none,
            )
            raw_tier = get_topos_image_tier_or_none(
                getattr(config, 'codec_profile', None))
            if raw_tier is not None and raw_tier.family == 'raw':
                return ("topos", raw_tier.pix_fmt, "raw")
            explicit = str(getattr(config, 'pixel_format', '') or '').strip().lower()
            if explicit in ('topos_cfa12le', 'topos_cfa16le'):
                return ("topos", explicit, "raw")
            return ("topos", "topos_cfa12le", "raw")

        entry = codec_map.get(config.codec, ("prores", "yuv422p10le", "hq"))
        # R7：Topos 档位 pix_fmt 显式覆盖（UI 选择器 → VideoConfig → 此处；
        # 域外名字不在 UI 域内，编码器构造期仍经单一解析表兜底拒绝）
        if entry[0] == "topos" and getattr(config, "pixel_format", ""):
            entry = (entry[0], str(config.pixel_format), entry[2])
        # HEVC 8-bit 交付档（2026-09-16 平面直通扩展）：pixel_format 显式
        # 覆盖仅在 HEVC 编码域内透传（yuv420p=8bit / yuv420p10le=10bit）；
        # 未设置时保持 codec_map 的 10-bit 默认——直配 TimelineExportConfig
        # 的既有调用方行为不变，8-bit 是 deliver UI/预设的显式选择。
        if entry[0] == "hevc":
            hevc_override = str(
                getattr(config, "pixel_format", "") or "").strip().lower()
            if hevc_override in ("yuv420p", "yuv420p10le"):
                entry = (entry[0], hevc_override, entry[2])
        return entry

    def _create_encoder(
        self,
        output_path: str,
        config: "FFmpegEncoderConfig",
    ) -> "FFmpegVideoEncoder":
        """
        创建视频编码器。

        Args:
            output_path: 输出路径
            config: 编码器配置

        Returns:
            FFmpegVideoEncoder 实例（codec='topos' 时为 ToposVideoEncoder，
            duck-type 同接口）
        """
        maybe_bridge = getattr(self, "_maybe_create_default_native_gpu_bridge", None)
        if callable(maybe_bridge):
            maybe_bridge(output_path, config)
        gpu_bridge = getattr(self, "_gpu_encode_surface_provider", None)
        if getattr(gpu_bridge, "handles_timeline_video", False):
            codec = str(getattr(config, "codec", "")).lower()
            pixel_format = str(getattr(config, "pix_fmt", "")).lower()
            if (codec, pixel_format) not in {
                ("h264", "yuv420p"),
                ("hevc", "yuv420p10le"),
            }:
                raise ValueError(
                    "native VideoToolbox timeline bridge supports only "
                    "H.264 yuv420p or HEVC yuv420p10le"
                )
            bridge_path = str(getattr(gpu_bridge, "output_path", ""))
            if bridge_path and os.path.abspath(bridge_path) != os.path.abspath(output_path):
                raise ValueError(
                    "native GPU bridge output path does not match timeline export"
                )
            bridge_width = int(getattr(gpu_bridge, "width", 0) or 0)
            bridge_height = int(getattr(gpu_bridge, "height", 0) or 0)
            if bridge_width != int(getattr(config, "width", 0)) or bridge_height != int(
                getattr(config, "height", 0)
            ):
                raise ValueError(
                    "native GPU bridge dimensions do not match timeline export"
                )
            return _ExternalGpuSurfaceEncoder(gpu_bridge, config)
        if str(config.codec).lower() == 'topos_image_sequence':
            import os as _os
            from src.shared.export.topos_encoder import ToposImageSequenceEncoder
            return ToposImageSequenceEncoder(
                output_path, config,
                slice_threads=min(16, _os.cpu_count() or 1))
        if str(config.codec).lower() in ('png_sequence', 'tiff_sequence'):
            # G14（第六轮审计）：标准 PNG/TIFF 图像序列交付——外部合成
            # 软件可直接回导（此前明确拒绝并要求改用 Topos 专有序列）。
            from src.shared.export.image_sequence_encoder import (
                StandardImageSequenceEncoder,
            )
            return StandardImageSequenceEncoder(output_path, config)
        if str(config.codec).lower() == 'gif':
            from src.shared.export.gif_encoder import GifVideoEncoder
            return GifVideoEncoder(output_path, config)
        if str(config.codec).lower() == 'topos':
            from src.shared.export.topos_encoder import ToposVideoEncoder
            # TOPOS timeline also runs the high-depth source decoder on the
            # render producer. Reserve part of a large CPU budget for that
            # FFmpeg pool; smaller hosts keep the existing full encoder cap.
            encoder_threads, _decode_threads = _topos_timeline_cpu_threads()
            # v1.5 tmcd：序列起始时码透传（有则写、无则不写——零字节变化；
            # 帧率不在标称白名单或分量非法时编码器侧降级为不写）
            seq = getattr(self, '_sequence', None)
            start_tc = str(
                (getattr(seq, 'format_settings', {}) or {})
                .get('start_timecode', '') or '') if seq is not None else ''
            return ToposVideoEncoder(
                output_path, config,
                slice_threads=encoder_threads,
                start_timecode=start_tc)

        from src.shared.export.video_encoder import FFmpegVideoEncoder

        return FFmpegVideoEncoder(output_path, config)

    def _maybe_create_default_native_gpu_bridge(
        self,
        output_path: str,
        config: "FFmpegEncoderConfig",
    ) -> None:
        """Opt into the native VT bridge for the proven minimal timeline.

        T-P0-30：前置协商（:func:`can_use_native_gpu_bridge`）在 encoder
        创建之前完成——不满足时记录 ``bridge_not_selected:<reason>`` 并留在
        普通 encoder 路径；bridge 初始化失败时清理半成品输出（此刻尚未写
        帧，允许重新选择 CPU 路径）。TOPOS 422 源因此绝不会在运行中途把
        CPU frame 送进 GPU bridge。

        ``TOPOS_EXPORT_NATIVE_VT=1`` enables the macOS default path only for
        unmodified clips whose source pixel format has a GPU path to the
        selected surface, H.264/NV12 or HEVC/P010 output, and no
        subtitle/alpha path.
        """
        if getattr(self, "_gpu_encode_surface_provider", None) is not None:
            return
        if str(os.getenv("TOPOS_EXPORT_NATIVE_VT", "")).lower() not in {
            "1", "true", "yes", "on",
        }:
            return
        eligibility = can_use_native_gpu_bridge(
            self._sequence,
            self._project,
            config,
            snapshot=getattr(self, "_runtime_capability_snapshot", None),
            export_config=self._active_export_config,
        )
        if not eligibility.eligible:
            self._note_bridge_not_selected(eligibility.reason_code)
            return
        codec = str(getattr(config, "codec", "")).lower()
        surface = eligibility.required_output_surface
        bridge_pixel_format = surface

        video_tracks_present = any(
            bool(getattr(track, "is_enabled", True))
            for track in getattr(self._sequence, "video_tracks", ()) or ()
        )
        if not video_tracks_present:
            self._note_bridge_not_selected("no_video_clips")
            return
        # AUD-18406：native VT bridge 的 fps 参数是整数 timescale
        # （``max(1, int(round(fps)))``），非整数帧率时间线（23.976/29.97）
        # 会被取整封装，与按精确 Rational 渲染的音频随片长累积漂移。
        # bridge 支持分数 timescale 前对非整数 fps 显式拒绝并记录
        # reason_code，留在普通 encoder 路径（其 fps 为精确 Rational）。
        bridge_fps = getattr(config, "fps", None)
        fps_num = int(getattr(bridge_fps, "num", 0) or 0)
        fps_den = int(getattr(bridge_fps, "den", 0) or 0)
        if fps_num > 0 and fps_den > 0:
            fps_is_integer = fps_num % fps_den == 0
        else:
            try:
                fps_value = float(bridge_fps)
                fps_is_integer = abs(fps_value - round(fps_value)) <= 1e-9
            except (TypeError, ValueError):
                fps_is_integer = True
        if not fps_is_integer:
            self._note_bridge_not_selected("non_integer_fps")
            return
        try:
            from src.shared.gpu.metal.context import MetalComputeContext
            from src.shared.media.macos_vt.gpu_export_bridge import (
                VideoToolboxGpuExportBridge,
            )

            metal_context = MetalComputeContext()
            bridge = VideoToolboxGpuExportBridge(
                metal_context,
                output_path,
                width=int(getattr(config, "width", 0)),
                height=int(getattr(config, "height", 0)),
                fps=max(1, int(round(float(getattr(config, "fps", 25.0))))),
                codec=codec,
                pixel_format=bridge_pixel_format,
            )
        except Exception as exc:
            # T-P0-30 步骤 5：bridge 初始化失败 → 清理半成品（session/mux/
            # temp 输出），此刻尚未写任何帧，允许普通 encoder 重新选择。
            logger.warning(
                "Native VT bridge initialization failed before any frame "
                "was written; cleaning up and using the normal encoder [%s]",
                exc,
            )
            try:
                provider = getattr(self, "_gpu_encode_surface_provider", None)
                if provider is not None:
                    provider.close()
            except Exception:  # noqa: BLE001 — 清理失败不掩盖原始错误
                pass
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except OSError:
                logger.warning(
                    "Could not remove partial bridge output %s", output_path)
            self._note_bridge_init_failed(str(exc))
            return
        self._gpu_encode_surface_provider = bridge
        self._timeline_graphics_backend = "metal"
        self._bridge_selection = {
            "selected": True, "reason_code": "",
            "output_surface": surface,
        }
        logger.info("Native VT bridge selected by TOPOS_EXPORT_NATIVE_VT")

    def _note_bridge_not_selected(self, reason_code: str) -> None:
        """记录"未选择 bridge"（区别于运行中 GPU 失败——T-P0-30 完成定义）。"""
        self._bridge_selection = {
            "selected": False, "reason_code": reason_code,
            "output_surface": "",
        }
        logger.info("Native VT bridge not selected: %s", reason_code)
        recorder = getattr(self, "_export_telemetry", None)
        if recorder is not None:
            recorder.set_fallback_root_cause(
                f"bridge_not_selected:{reason_code}",
                stage="encode_submit",
                origin="native_vt_bridge",
            )

    def _note_bridge_init_failed(self, detail: str) -> None:
        """记录 bridge 初始化失败（已清理，尚未写帧，回退普通 encoder）。"""
        self._bridge_selection = {
            "selected": False, "reason_code": "bridge_init_failed",
            "output_surface": "",
        }
        # T-P0-31：bridge 选择时曾把渲染侧切到 metal（surface 路径）；
        # 初始化失败必须整体回退——否则宿主编码器会收到 planar/Metal 帧
        # 而直接崩溃（而非走诚实的 CPU 编码回退）。
        self._gpu_encode_surface_provider = None
        self._timeline_graphics_backend = self._select_timeline_graphics_backend()
        recorder = getattr(self, "_export_telemetry", None)
        if recorder is not None:
            recorder.set_fallback_root_cause(
                f"bridge_init_failed:{detail}",
                stage="encode_submit",
                origin="native_vt_bridge",
            )

    def _export_direct_topos(
        self,
        plan: Any,
        config: TimelineExportConfig,
        encoder_config: "FFmpegEncoderConfig",
    ) -> ExportOutcome:
        """T-P1-10：单一 TOPOS clip → TOPOS 输出的 planar 直通。

        ``ToposMediaSource.read_frame`` 的 planar 帧直接交给
        ``ToposVideoEncoder``——不经过 render plan/provider/BGR48/resize；
        等比例低分辨率时间线由 native decoder 直接输出目标平面。
        编码器仍是 CPU 路径：telemetry 标记
        ``delivery_path=topos_direct_planar_cpu_encode``，不虚报 GPU。
        帧循环默认按档位门控走解码∥编码流水线（重档
        ``_direct_topos_pipeline_loop``、轻档串行，
        TOPOS_EXPORT_DIRECT_PIPELINE=1/0 强制覆盖）；取消走
        encoder.abort()：不留目标文件或 ``.topos-tmp`` 残留。
        """
        from src.shared.export.topos_encoder import ToposVideoEncoder
        from src.shared.media.topos_source import ToposMediaSource

        # M4-R2：RAW→RAW 直通与 planar 直通分面标记（基准报告的 trail 列
        # 需要区分交付面；两者同为 CPU 编码面，不虚报 GPU）。
        is_raw_direct = str(getattr(plan, "source_pixel_format", "") or ""
                            ).startswith("topos_cfa")
        delivery_path = ("topos_direct_raw_cpu_encode" if is_raw_direct
                         else "topos_direct_planar_cpu_encode")
        recorder = getattr(self, "_export_telemetry", None)
        if recorder is not None:
            recorder.set_delivery_path(delivery_path)
        # legacy trail 显式标注 CPU 编码面——基准报告的 trail 列不得把
        # "无 fallback"误显示为 full_gpu（T-P1-10 步骤 4：不虚报 GPU）。
        legacy_trail = getattr(self, "_gpu_trail", None)
        if legacy_trail is not None:
            legacy_trail.record_stage_sample(
                name="encode_submit", backend="cpu_encoder",
                gpu_submit_ms=None,
            )
            legacy_trail.set_fallback_reason(delivery_path)
        encoder_threads, decode_threads = _topos_timeline_cpu_threads()
        # 4K 全分辨率/重编码时，编码器与解码器共享 native slice pool，
        # 使用拆分后的解码配额可避免互相争抢；但 4K→2K 原生降采样时
        # 编码几乎为空，瓶颈完全在解码，保留 decoder 的自动高并行度更快。
        source_pixels = int(getattr(plan, "width", 0)) * int(
            getattr(plan, "height", 0))
        target_pixels = int(getattr(plan, "target_width", 0)) * int(
            getattr(plan, "target_height", 0))
        requires_conversion = bool(
            getattr(plan, "requires_planar_conversion", False))
        split_decode_workers = (
            decode_threads
            if (source_pixels >= 3840 * 2160
                and not requires_conversion
                and not (bool(getattr(plan, "decoder_resize", False))
                         and target_pixels < 3840 * 2160))
            else None
        )
        logger.info(
            "Direct TOPOS delivery engaged: %s frames from %s "
            "(delivery_path=%s)",
            plan.frames, plan.asset_path, delivery_path,
        )

        self._is_exporting = True
        source: Optional[ToposMediaSource] = None
        encoder = ToposVideoEncoder(
            config.output_path, encoder_config,
            slice_threads=encoder_threads)
        try:
            encoder.open()

            # v1.1 音频轨（plan M-A6）：include_audio 不再拦截直通——
            # 与普通路径同构：声明音频流 + 并行渲染母带 + 帧循环后喂入。
            # v1.1 音频轨（plan M-A6）：include_audio 不再拦截直通——
            # 与普通路径同构：声明音频流 + 并行渲染母带 + 帧循环后喂入。
            direct_has_audio = self._direct_audio_prepare(
                encoder, plan, config)
            decode_target_size = None
            if bool(getattr(plan, "decoder_resize", False)):
                decode_target_size = (
                    int(getattr(plan, "target_width", 0)),
                    int(getattr(plan, "target_height", 0)),
                )
            drop_source_alpha = (
                getattr(plan, "source_alpha_depth", None) is not None
                and getattr(plan, "target_alpha_depth", None) is None
            )
            source = ToposMediaSource(
                plan.asset_path,
                target_size=decode_target_size,
                decode_workers=split_decode_workers,
                decode_role="export",
                drop_alpha=drop_source_alpha,
            )
            source.open()
            # RC1：RAW→RAW 直出透传源 TRWM（as-shot 基线随流走——导出物
            # 重导入不丢 RAW 参数）；非 RAW 或源无 trwm 时不写（字节不变）
            source_trwm = getattr(source, "raw_meta", None)
            if source_trwm is not None:
                from src.shared.codec.topos_trwm import encode_trwm

                encoder.set_raw_meta(encode_trwm(
                    **{k: source_trwm[k] for k in (
                        "wb_gain_r", "wb_gain_g", "wb_gain_b",
                        "exposure_stops", "black_level", "cfa_layout",
                        "sensor_matrix_id")}))
            # 解码∥编码流水线门控（2026-09-19 实测定标）：收益与"编码
            # 耗时 ≈ 解码耗时"正相关——hq 档 2K +37% / 4K +49%（稳态）；
            # proxy/lt 档编码过轻、解码主导，重叠换不来新容量（topos
            # native 调用共用进程级线程池），交接开销反而 −7%（2K）。
            # 故轻档默认串行、重档默认流水线；有额外平面转换时，即使是
            # proxy/lt 也默认开启流水线，因为转换后的 CPU 工作可与 native
            # 编码重叠。TOPOS_EXPORT_DIRECT_PIPELINE=1/0 仍可强制覆盖。
            pipeline_env = str(os.environ.get(
                "TOPOS_EXPORT_DIRECT_PIPELINE", "")).strip().lower()
            if pipeline_env in ("0", "off", "false"):
                use_pipeline = False
            elif pipeline_env in ("1", "on", "true"):
                use_pipeline = plan.frames > 1
            else:
                tier_id = str(getattr(
                    encoder_config, "profile", "") or "").strip().lower()
                use_pipeline = (
                    (tier_id in _TOPOS_DIRECT_PIPELINE_TIERS
                     or bool(getattr(
                         plan, "requires_planar_conversion", False)))
                    and plan.frames > 1)
            if use_pipeline:
                logger.info(
                    "Direct TOPOS decode-encode pipeline engaged (depth=3)")
                cancelled = self._direct_topos_pipeline_loop(
                    plan, source, encoder, recorder)
            else:
                cancelled = self._direct_topos_serial_loop(
                    plan, source, encoder, recorder)
            if cancelled:
                # 统一取消收尾：源与编码器都不留运行态（原串行路径
                # pause-cancel 分支未关源，属遗漏——此处一并补齐）。
                self._join_audio_render_thread()
                if source is not None:
                    try:
                        source.close()
                    except Exception:  # noqa: BLE001
                        pass
                    source = None
                encoder.abort()
                self._cleanup_clips()
                return dataclasses.replace(
                    ExportOutcome.user_cancelled(),
                    delivery_path=delivery_path,
                )
            source.close()
            source = None
            # v1.1：音频母带写入（并行渲染已在帧循环期间完成/取消）
            self._direct_audio_finish(encoder, plan, config,
                                      direct_has_audio)
            mux_started = time.perf_counter()
            encoder.close()
            if recorder is not None:
                recorder.record_stage_sample(
                    "mux", "container_writer",
                    wall_ms=(time.perf_counter() - mux_started) * 1000.0,
                )
                # T-P1-11 步骤 5：native 编码阶段统计入报告（worker CPU
                # 累计与墙钟分桶显式分组，topos_binding 分组口径）。计数器
                # 仅在 TOPOS_CODEC_PROFILE=1 时由 native 累加——未开启时
                # 保持 None（零值 ≠ 测量）；收集失败不掩盖导出成功。
                try:
                    from src.shared.codec.topos_binding import (
                        ToposCodec as _ToposCodec,
                        group_native_encode_stage_stats as _group_stats,
                    )

                    _grouped = _group_stats(
                        _ToposCodec().encode_stage_stats_get())
                    if (_grouped["worker_cpu_ns_total"] > 0
                            or _grouped["wall_ns_total"] > 0):
                        recorder.set_native_encode_stage_stats(_grouped)
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "native encode stage stats unavailable", exc_info=True)
            self._cleanup_clips()
            logger.info(
                "Direct TOPOS delivery complete: frames_encoded=%s",
                self._current_frame,
            )
            self._write_success_manifest_async(config)
            return dataclasses.replace(
                ExportOutcome.ok(
                    hw_fallback_reason="",
                ),
                delivery_path=delivery_path,
            )
        except Exception as e:  # noqa: BLE001 — 与主路径同语义：结构化失败
            logger.exception("Direct TOPOS delivery failed")
            # AUD-18401：与主路径异常分支（3073-3076）和取消分支（5123）
            # 同语义——先汇合音频渲染线程并清共享音频状态，否则旧线程跨
            # 导出存活，下一次导出期间写入 _audio_master_pcm /
            # _audio_render_error（过期 PCM 覆盖新导出 / 注入过期错误）。
            self._join_audio_render_thread()
            self._audio_master_pcm = None
            self._audio_render_error = None
            if source is not None:
                try:
                    source.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                if hasattr(encoder, "abort"):
                    encoder.abort()
                else:
                    encoder.close()
            except Exception:  # noqa: BLE001 — 清理失败不掩盖原始错误
                pass
            self._cleanup_clips()
            return dataclasses.replace(
                ExportOutcome.failed(
                    error_code="EXPORT_FAILED",
                    cause=f"{type(e).__name__}: {e}",
                ),
                delivery_path=delivery_path,
            )

    def _direct_topos_serial_loop(
        self,
        plan: "DirectToposDeliveryPlan",
        source: Any,
        encoder: Any,
        recorder: Any,
    ) -> bool:
        """串行帧循环（逐帧 read_frame → encode_frame，原行为保持）。

        Returns:
            True = 用户取消（收尾由调用方统一处理）；False = 正常完成。
        """
        # Native 4:4:4→4:2:2 can write into caller-owned U/V buffers.  Reuse
        # them across serial frames so a 4K conversion does not allocate two
        # large chroma planes on every iteration.  An empty holder is kept for
        # the first frame; old DLLs and the NumPy fallback leave it untouched.
        conversion_buffers: Optional[list] = (
            [] if getattr(plan, "requires_planar_conversion", False) else None
        )
        if plan.frames > 0:
            # Direct delivery is a contiguous source range.  Position once,
            # then use the sequential API so ToposMediaSource can use its
            # bounded export read-ahead; explicit read-ahead=0 still follows
            # the same single-frame decode path.
            source.seek_frame(plan.source_start)
        for i in range(plan.frames):
            if not self._wait_if_paused():
                logger.info(
                    "Direct TOPOS delivery cancelled while paused at "
                    "frame %s", i)
                return True
            if self._cancel_requested:
                logger.info(
                    "Direct TOPOS delivery cancelled at frame %s", i)
                return True
            started = time.perf_counter()
            decoded = source.read_next_frame()
            decode_ms = (time.perf_counter() - started) * 1000.0
            planes = getattr(decoded, "planes", None)
            if decoded is None or not planes or len(planes) < 3:
                raise RuntimeError(
                    f"direct TOPOS source read failed at frame {i}")
            if recorder is not None:
                recorder.record_stage_sample(
                    "decode", "topos_native",
                    wall_ms=decode_ms, frame_index=i,
                )
            started = time.perf_counter()
            encoder.encode_frame(
                self._prepare_direct_topos_frame(
                    plan, decoded, getattr(source, "_codec", None),
                    conversion_buffers=conversion_buffers))
            if recorder is not None:
                recorder.record_stage_sample(
                    "encode", "topos",
                    wall_ms=(time.perf_counter() - started) * 1000.0,
                    frame_index=i,
                )
            self._current_frame = i + 1
            self._notify_progress()
        return False

    def _direct_topos_pipeline_loop(
        self,
        plan: "DirectToposDeliveryPlan",
        source: Any,
        encoder: Any,
        recorder: Any,
    ) -> bool:
        """解码（生产者线程）∥ 编码（导出线程）流水线（直通交付）。

        正确性前提（设计复核）：
        - ``_ToposPlanePool`` 引用计数判定空闲（``_entry_free``）——
          消费线程持有 frame i 平面期间，生产者解码 i+1 只会新分配
          或复用已释放项，格式契约明确"不允许覆盖仍在使用的帧"；
        - ``ToposVideoEncoder.encode_frame`` 同步消费平面（planar 为
          校验+紧化拷贝，返回即可释放 DecodedFrame）；
        - 需要平面转换时，生产者完成 decode + planar prepare，消费者只做
          native encode，使转换 CPU 与编码 CPU 重叠；单生产者 FIFO +
          单消费者 → 帧序/进度与串行逐帧一致；
        - 解码与编码均为 ctypes native 调用（释放 GIL）→ 真并行。

        收尾无死锁：生产者的有界入队在 stop 置位后退化为丢弃退出；
        消费者 ``done ∧ empty`` 轮询 + join 后才关源。生产者异常记入
        holder，join 后原样重抛（与串行同语义）。

        Returns:
            True = 用户取消；False = 正常完成。
        """
        import queue as _queue

        q: "_queue.Queue" = _queue.Queue(maxsize=3)
        stop = threading.Event()
        producer_done = threading.Event()
        producer_error: list = []
        prepare_in_producer = bool(
            getattr(plan, "requires_planar_conversion", False))
        conversion_buffer_q: Optional["_queue.Queue"] = None
        if prepare_in_producer:
            # The FIFO has the same capacity as the frame queue.  A holder is
            # returned only after encode_frame has synchronously consumed its
            # converted planes, so no queued frame can be overwritten.
            conversion_buffer_q = _queue.Queue(maxsize=3)
            for _ in range(3):
                conversion_buffer_q.put([])

        def enqueue(item: tuple) -> bool:
            """有界入队；消费侧退出（stop）→ 返回 False 丢弃剩余帧。"""
            while not stop.is_set():
                try:
                    q.put(item, timeout=0.25)
                    return True
                except _queue.Full:
                    continue
            return False

        def produce() -> None:
            try:
                if plan.frames > 0:
                    source.seek_frame(plan.source_start)
                for i in range(plan.frames):
                    if stop.is_set() or self._cancel_requested:
                        break
                    if not self._wait_if_paused():
                        break
                    conversion_buffers = None
                    if conversion_buffer_q is not None:
                        while not stop.is_set():
                            try:
                                conversion_buffers = conversion_buffer_q.get(
                                    timeout=0.25)
                                break
                            except _queue.Empty:
                                continue
                        if conversion_buffers is None:
                            break
                    started = time.perf_counter()
                    try:
                        decoded = source.read_next_frame()
                        decode_ms = (time.perf_counter() - started) * 1000.0
                        planes = getattr(decoded, "planes", None)
                        if decoded is None or not planes or len(planes) < 3:
                            raise RuntimeError(
                                f"direct TOPOS source read failed at frame {i}")
                        prepare_ms = 0.0
                        if prepare_in_producer:
                            started = time.perf_counter()
                            decoded = self._prepare_direct_topos_frame(
                                plan, decoded, getattr(source, "_codec", None),
                                conversion_buffers=conversion_buffers)
                            prepare_ms = (time.perf_counter() - started) * 1000.0
                        if not enqueue((
                                i, decoded, decode_ms, prepare_ms,
                                conversion_buffers)):
                            if conversion_buffers is not None:
                                conversion_buffer_q.put(conversion_buffers)
                            break
                        # Ownership transfers to the queue item and returns
                        # from the consumer after synchronous encoding.
                        conversion_buffers = None
                    except Exception:
                        if conversion_buffers is not None:
                            conversion_buffer_q.put(conversion_buffers)
                        raise
            except Exception as e:  # noqa: BLE001 —— 消费侧重抛，串行同语义
                producer_error.append(e)
            finally:
                producer_done.set()

        producer = threading.Thread(
            target=produce, name="topos-direct-decode-pipeline",
            daemon=True)
        producer.start()
        cancelled = False
        try:
            while not producer_done.is_set() or not q.empty():
                try:
                    item = q.get(timeout=0.1)
                except _queue.Empty:
                    continue
                if not self._wait_if_paused():
                    logger.info(
                        "Direct TOPOS delivery cancelled while paused at "
                        "frame %s", self._current_frame)
                    cancelled = True
                    break
                if self._cancel_requested:
                    logger.info(
                        "Direct TOPOS delivery cancelled at frame %s",
                        self._current_frame)
                    cancelled = True
                    break
                i, decoded, decode_ms, prepare_ms, conversion_buffers = item
                try:
                    if recorder is not None:
                        recorder.record_stage_sample(
                            "decode", "topos_native",
                            wall_ms=decode_ms, frame_index=i,
                        )
                    started = time.perf_counter()
                    encoder.encode_frame(
                        decoded if prepare_in_producer else
                        self._prepare_direct_topos_frame(
                            plan, decoded, getattr(source, "_codec", None)))
                    if recorder is not None:
                        recorder.record_stage_sample(
                            "encode", "topos",
                            wall_ms=((time.perf_counter() - started) * 1000.0
                                     + prepare_ms),
                            frame_index=i,
                        )
                    self._current_frame = i + 1
                    self._notify_progress()
                finally:
                    if conversion_buffers is not None:
                        conversion_buffer_q.put(conversion_buffers)
        finally:
            stop.set()
            producer.join()
        if producer_error:
            raise producer_error[0]
        return cancelled

    @staticmethod
    def _prepare_direct_topos_frame(
        plan: "DirectToposDeliveryPlan",
        decoded: Any,
        native_codec: Any = None,
        *,
        conversion_buffers: Optional[list] = None,
    ) -> Any:
        """Apply the minimal integer-domain conversion for direct delivery.

        Topos' public timeline config can express Alpha as ``has_alpha=True``
        alongside a bare color ``pix_fmt``.  The encoder then uses its default
        Alpha depth (currently a12), while imported files may carry a16/a10/a8.
        For YUV 4:2:2↔4:4:4 or 10/12/16-bit changes, reshape only the affected
        integer planes and keep the path out of the RGB/GPU render round-trip.
        """
        source_color_depth = int(getattr(plan, "source_bit_depth", 0) or 0)
        target_color_depth = int(
            getattr(plan, "target_bit_depth", source_color_depth)
            or source_color_depth)
        source_color_layout = str(
            getattr(plan, "source_color_layout", "") or "")
        target_color_layout = str(
            getattr(plan, "target_color_layout", "") or "")
        layout_changed = source_color_layout != target_color_layout
        source_alpha_depth = getattr(plan, "source_alpha_depth", None)
        target_alpha_depth = getattr(plan, "target_alpha_depth", None)
        color_depth_changed = source_color_depth != target_color_depth
        alpha_depth_changed = (
            source_alpha_depth is not None
            and target_alpha_depth is not None
            and int(source_alpha_depth) != int(target_alpha_depth)
        )
        if (not layout_changed and not color_depth_changed
                and not alpha_depth_changed):
            return decoded

        planes = getattr(decoded, "planes", None)
        if not planes or len(planes) < 3:
            raise RuntimeError(
                "direct TOPOS planar conversion requires three color planes")

        import numpy as np

        def convert_depth(plane: Any, source_depth: int,
                         target_depth: int) -> Any:
            """Convert an integer full-scale plane without RGB round-trip."""
            array = np.asarray(plane)
            if array.ndim != 2:
                raise RuntimeError(
                    "direct TOPOS planar conversion received non-2D plane")
            if source_depth == target_depth:
                return array
            if source_depth < target_depth:
                # Topos integer planes use the conventional left-shifted
                # container representation for an up-conversion (10→12:
                # x<<2), preserving limited-range code anchors exactly.
                converted = np.left_shift(
                    array.astype(np.uint32), target_depth - source_depth)
            else:
                # Down-conversion uses round-half-up, matching the existing
                # planar delivery path and avoiding systematic darkening.
                # Keep the common uint16 path in the target container: the
                # old implementation widened the whole 4K Alpha plane to
                # uint32, added the rounding bias, then narrowed it again.
                # That created multiple 32 MB temporaries per 4K frame.
                shift = source_depth - target_depth
                converted = np.right_shift(array, shift)
                remainder = np.bitwise_and(array, (1 << shift) - 1)
                np.add(
                    converted,
                    remainder >= (1 << (shift - 1)),
                    out=converted,
                    casting="unsafe",
                )
                # 16-bit full scale rounds to 4096 when reduced to 12-bit;
                # clamp the exact opaque endpoint back to the target domain.
                np.minimum(
                    converted, (1 << target_depth) - 1, out=converted)
                return np.ascontiguousarray(converted.astype(
                    "<u2", copy=False))
            # Up-conversion keeps the existing wide intermediate so malformed
            # source samples still get clamped instead of wrapping uint16.
            converted = np.minimum(converted, (1 << target_depth) - 1)
            return np.ascontiguousarray(converted.astype("<u2", copy=False))

        converted_colors = tuple(
            convert_depth(plane, source_color_depth, target_color_depth)
            for plane in planes[:3]
        )
        if layout_changed:
            if {source_color_layout, target_color_layout} != {
                    "yuv422", "yuv444"}:
                raise RuntimeError(
                    "direct TOPOS planar conversion only supports YUV "
                    "4:2:2 <-> 4:4:4 chroma resampling")
            y_width = int(converted_colors[0].shape[1])
            if source_color_layout == "yuv422":
                # Topos 422 is horizontally co-sited: duplicate each chroma
                # sample across its two covered luma columns.  Odd widths are
                # clipped to the luma width after the final sample repeat.
                converted_colors = (
                    converted_colors[0],
                    np.ascontiguousarray(
                        np.repeat(converted_colors[1], 2, axis=1)
                        [:, :y_width]),
                    np.ascontiguousarray(
                        np.repeat(converted_colors[2], 2, axis=1)
                        [:, :y_width]),
                )
            else:
                # 4:4:4 -> 4:2:2 uses the same horizontal box average as the
                # existing Topos packed-color conversion, including edge
                # replication for an odd luma width.  Prefer the native
                # row-parallel helper when the decoder binding exposes it;
                # old libraries transparently retain the numpy reference.
                native_chroma = None
                if callable(getattr(native_codec,
                                    "convert_yuv444_to_422", None)):
                    try:
                        reusable = (
                            conversion_buffers
                            if conversion_buffers is not None
                            and len(conversion_buffers) == 2
                            else None
                        )
                        native_chroma = native_codec.convert_yuv444_to_422(
                            converted_colors[1], converted_colors[2],
                            bit_depth=target_color_depth,
                            max_workers=max(1, min(
                                16, os.cpu_count() or 1)),
                            planes_out=reusable)
                        if conversion_buffers is not None:
                            conversion_buffers[:] = native_chroma
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "native 444->422 conversion unavailable; "
                            "using numpy fallback: %s", exc)
                if native_chroma is not None:
                    converted_colors = (
                        converted_colors[0], native_chroma[0], native_chroma[1])
                else:
                    reusable_chroma = (
                        conversion_buffers
                        if conversion_buffers is not None
                        and len(conversion_buffers) == 2
                        else None
                    )
                    chroma = []
                    for plane_index, plane in enumerate(converted_colors[1:3]):
                        width = int(plane.shape[1])
                        full_pairs = width // 2
                        output_shape = (
                            int(plane.shape[0]), (width + 1) // 2)
                        output = None
                        if reusable_chroma is not None:
                            candidate = reusable_chroma[plane_index]
                            if (
                                candidate.shape == output_shape
                                and candidate.dtype == np.dtype("<u2")
                                and candidate.flags.c_contiguous
                            ):
                                output = candidate
                        if output is None:
                            output = np.empty(output_shape, dtype="<u2")
                        if target_color_depth < 16:
                            # 10/12-bit samples sum to at most 8190, so
                            # uint16 is safe here. Reuse one target-sized
                            # buffer for sum -> round-half-up -> divide.
                            if full_pairs:
                                left = plane[:, :full_pairs * 2:2]
                                right = plane[:, 1:full_pairs * 2:2]
                                np.add(left, right, out=output[:, :full_pairs])
                                np.add(
                                    output[:, :full_pairs], 1,
                                    out=output[:, :full_pairs],
                                )
                                np.right_shift(
                                    output[:, :full_pairs],
                                    1,
                                    out=output[:, :full_pairs],
                                )
                            if width & 1:
                                output[:, -1] = plane[:, -1]
                        else:
                            if full_pairs:
                                pair_sum = (
                                    plane[:, :full_pairs * 2:2].astype(np.uint32)
                                    + plane[:, 1:full_pairs * 2:2].astype(
                                        np.uint32)
                                )
                                output[:, :full_pairs] = (
                                    (pair_sum + 1) >> 1).astype(
                                        "<u2", copy=False)
                            if width & 1:
                                output[:, -1] = plane[:, -1]
                        chroma.append(output)
                    if conversion_buffers is not None:
                        conversion_buffers[:] = chroma
                    converted_colors = (
                        converted_colors[0], chroma[0], chroma[1])
        converted_alpha = None
        if len(planes) >= 4 and target_alpha_depth is not None:
            if alpha_depth_changed:
                converted_alpha = convert_depth(
                    planes[3], int(source_alpha_depth), int(target_alpha_depth))
            else:
                converted_alpha = planes[3]

        output_planes = converted_colors
        if converted_alpha is not None:
            output_planes += (converted_alpha,)

        frame_infos = getattr(decoded, "plane_infos", None)
        if frame_infos:
            frame_infos = tuple(
                replace(
                    info,
                    bit_depth=(
                        int(target_alpha_depth)
                        if index == 3 and target_alpha_depth is not None
                        else (int(target_color_depth) if index < 3
                              else getattr(info, "bit_depth", 0))),
                    dtype=(np.dtype("<u2")
                           if index < 3 or index == 3 and alpha_depth_changed
                           else getattr(info, "dtype", np.dtype("<u2"))),
                    stride=(
                        int((converted_alpha if index == 3
                             else converted_colors[index]).shape[1]) * 2
                        if index < 3 or index == 3 and alpha_depth_changed
                        else getattr(info, "stride", 0)),
                )
                for index, info in enumerate(frame_infos[:len(output_planes)])
            )

        pixel_format = str(getattr(decoded, "pixel_format", "") or "")
        import re as _re

        if layout_changed:
            target_chroma = "422" if target_color_layout == "yuv422" else "444"
            pixel_format = _re.sub(
                r"(yuva?|yuv)(?:422|444)",
                lambda match: f"{match.group(1)}{target_chroma}",
                pixel_format,
                count=1,
            )
        if color_depth_changed:
            pixel_format = _re.sub(
                r"p(?:10|12|16)(?=le|a)",
                f"p{target_color_depth}", pixel_format, count=1)
        if target_alpha_depth is not None:
            base, marker, old_depth = pixel_format.rpartition("a")
            if marker and old_depth.isdigit():
                pixel_format = f"{base}a{int(target_alpha_depth)}"
        elif source_alpha_depth is not None:
            # Canonical non-alpha planar name: this also removes the
            # ``topos_yuva...aN`` marker after an explicit Alpha drop.
            target_chroma = "422" if target_color_layout == "yuv422" else "444"
            pixel_format = f"yuv{target_chroma}p{int(target_color_depth)}le"
        return replace(
            decoded,
            planes=output_planes,
            pixel_format=pixel_format,
            plane_infos=frame_infos,
        )

    def _pipeline_eligible(
        self, start_frame: int, end_frame: int, config: TimelineExportConfig
    ) -> bool:
        """阶段 3b：渲染 ∥ 编码流水线适用性。

        GPU 降噪桥（P1-COLOR-13 headless GL 上下文）在渲染线程惰性创建、
        循环结束后于导出线程释放——线程亲和不确定，启用降噪的导出回退
        串行（正确性优先）。仅扫描渲染计划元数据，不触碰像素。
        """
        if self._render_plan is None:
            return False
        try:
            # F04（2026-09-27）：降噪是 per-clip 属性——按与导出区间重叠
            # 的 clip 判定。历史逐帧 get_frame_plan 预检查会为 90k 帧构造
            # 90k 份 FrameRenderPlan（4096 帧的 plan 缓存在真正渲染前已
            # 全部淘汰），首帧等待随片长线性膨胀。语义保留：任一重叠
            # clip 启用降噪 → 回退串行（正确性优先）。
            # N08（2026-09-27 复验）：只扫"会被渲染"的轨道/片段——复用
            # is_track_renderable（可见/启用/非静音/solo 语义）与
            # clip.is_enabled，与渲染计划逐帧过滤同口径。禁用轨道上的
            # 降噪 clip 不会进渲染，不应把可流水线的导出打回串行。
            from src.features.edit.core.timeline_render_plan import (
                is_track_renderable,
            )
            sequence = self._render_plan.sequence
            tracks = [t for t in getattr(sequence, "video_tracks", ()) or ()]
            has_solo = any(bool(getattr(t, "is_solo", False)) for t in tracks)
            for track in tracks:
                if not is_track_renderable(track, has_solo):
                    continue
                for clip in getattr(track, "clips", ()) or ():
                    if not bool(getattr(clip, "is_enabled", True)):
                        continue
                    cs = int(getattr(clip, "timeline_start", 0) or 0)
                    ce = cs + int(getattr(clip, "timeline_duration", 0) or 0)
                    if ce <= start_frame or cs >= end_frame:
                        continue  # 与导出区间无重叠
                    if self._get_clip_video_denoise(
                        _ClipOnlyLayer(clip)
                    ) is not None:
                        return False
        except Exception:
            return False
        return True

    def _render_encode_pipeline(
        self, start_frame: int, end_frame: int, config: TimelineExportConfig
    ) -> None:
        """阶段 3b：渲染（生产者线程）∥ 编码（当前导出线程）。

        - 有界队列深度 3（4K 帧 ~24MB/帧 → 预读上限 ~72MB）
        - 编码为 ctypes 原生调用（释放 GIL），与生产者的解码/合成重叠
        - 无死锁收尾：生产者以 producer_done 事件收尾（队列外标志），
          消费者轮询 `done ∧ empty`；stop 置位后生产者的有界入队退化为
          丢帧退出——不存在互相等待的路径
        - 生产者异常：记入 holder，消费侧 join 后原样重抛（与串行同语义）
        - 进度/帧序/取消行为与串行路径逐项一致
        """
        import queue as _queue
        import threading as _threading

        q: "_queue.Queue" = _queue.Queue(maxsize=3)
        stop = _threading.Event()
        producer_done = _threading.Event()
        producer_error: list = []

        def enqueue(item: tuple) -> bool:
            """有界入队；消费侧退出（stop）→ 返回 False 丢弃剩余帧。"""
            while not stop.is_set():
                try:
                    q.put(item, timeout=0.25)
                    return True
                except _queue.Full:
                    continue
            return False

        def produce() -> None:
            try:
                for fi in range(start_frame, end_frame):
                    if stop.is_set() or self._cancel_requested:
                        break
                    if not self._wait_if_paused():
                        break
                    if not enqueue((fi, self._render_frame(fi, config))):
                        break
            except Exception as e:  # noqa: BLE001 —— 消费侧重抛，串行同语义
                producer_error.append(e)
            finally:
                # P0-GPU-1: GPU 合成 context 由本生产者线程创建，
                # 必须在创建线程释放（standalone context 线程亲和）
                self._release_gpu_export_renderer()
                producer_done.set()

        producer = _threading.Thread(
            target=produce, name="export-render-pipeline", daemon=True)
        producer.start()
        try:
            while not producer_done.is_set() or not q.empty():
                try:
                    item = q.get(timeout=0.1)
                except _queue.Empty:
                    continue
                frame_idx, frame_data = item
                if not self._wait_if_paused():
                    logger.info("Export cancelled while paused")
                    stop.set()
                    break
                if self._cancel_requested:
                    logger.info("Export cancelled by user")
                    stop.set()
                    break  # 丢弃已预读帧
                if frame_data is not None:
                    self._encode_frame(frame_data)
                else:
                    logger.warning(f"Frame {frame_idx} render returned None")
                self._current_frame = frame_idx - start_frame + 1
                self._notify_progress()
        finally:
            stop.set()
            producer.join()
        if producer_error:
            raise producer_error[0]

    def _render_frame(
        self,
        frame_idx: int,
        config: TimelineExportConfig,
    ) -> Optional[Any]:
        """渲染单帧（T-P0-20：render 阶段 wall 计时后委托实现）。"""
        started = time.perf_counter()
        result = self._render_frame_impl(frame_idx, config)
        recorder = getattr(self, "_export_telemetry", None)
        if recorder is not None:
            recorder.record_stage_sample(
                "render",
                str(self._timeline_graphics_backend or "cpu"),
                wall_ms=(time.perf_counter() - started) * 1000.0,
                frame_index=frame_idx,
            )
        return result

    def _render_frame_impl(
        self,
        frame_idx: int,
        config: TimelineExportConfig,
    ) -> Optional[Any]:
        """
        渲染单帧。

        使用 TimelineRenderPlan 计算帧内容，
        合成多轨叠加和转场混合。

        Args:
            frame_idx: 时间线帧号
            config: 导出配置

        Returns:
            合成帧（BGR ndarray）或满足严格直通条件的 planar DecodedFrame，或 None
        """
        if self._render_plan is None:
            return None

        # P1-02：记录活动导出配置，供嵌套序列递归合成复用（字幕烧录等
        # 交付语义与父时间线一致）。
        self._active_export_config = config

        # 获取帧渲染计划
        frame_plan = self._render_plan.get_frame_plan(frame_idx)

        # TOPOS 高精度、严格无效果的单视频层可保留源 YUV 平面直达编码器。
        # 这仍然走同一个有界 render→encode 队列，只略过无意义的
        # YUV→RGB→GPU upload/readback→YUV 往返。
        passthrough = self._try_render_planar_passthrough(
            frame_plan, frame_idx, config)
        if passthrough is not None:
            return passthrough

        if not frame_plan.has_content:
            # 无内容，返回黑帧
            width, height = self._get_output_resolution(config)
            black = self._create_black_frame(width, height)
            # P10.3: 即使无视频内容，字幕也需要烧录（纯字幕视频）
            if config.burn_subtitles:
                black = self._burn_in_subtitles(black, frame_idx, width, height)
            return self._apply_program_guide_matte(black)

        # 合成层
        composite = self._composite_layers(
            frame_plan.layers, frame_plan.transitions, config, frame_idx)

        # P10.3: 字幕烧录（视频合成完成后，把字幕叠在顶上）
        if composite is not None and config.burn_subtitles:
            width, height = self._get_output_resolution(config)
            composite = self._burn_in_subtitles(composite, frame_idx, width, height)

        if composite is not None:
            composite = self._apply_program_guide_matte(composite)
        return composite

    @staticmethod
    def _rational_pair(value: Any) -> Optional[Tuple[int, int]]:
        """Return a normalized numerator/denominator pair for exact FPS checks."""
        try:
            num = int(getattr(value, "num", getattr(value, "numerator", 0)))
            den = int(getattr(value, "den", getattr(value, "denominator", 0)))
        except (TypeError, ValueError):
            return None
        if num <= 0 or den <= 0:
            return None
        from math import gcd

        divisor = gcd(num, den)
        return num // divisor, den // divisor

    def _try_render_planar_passthrough(
        self,
        frame_plan: Any,
        frame_idx: int,
        config: TimelineExportConfig,
    ) -> Optional[Any]:
        """Return a planar delivery frame when timeline semantics are identity.

        The encoder already accepts validated YUV planes for the target
        formats in :func:`_passthrough_specs_for` (TOPOS yuv422p10le, and
        since the 2026-09-15 export-speed plan, FFmpeg h264/hevc yuv420p;
        since 2026-09-17 also 10-bit sources downconverted to the 8-bit
        tier in the YUV domain). 8-bit 4:2:0 SDR delivery may also resize
        the source Y/U/V planes directly when the only timeline operation
        is the requested output scaling. This fail-closed gate keeps every
        other edit on the normal compositor.
        """
        import numpy as np

        # An explicit native surface bridge must receive the Metal-rendered
        # CVPixelBuffer, never the older host-visible planar passthrough. The
        # latter is still valid for the ordinary FFmpeg/Topos encoders.
        if getattr(
            getattr(self, "_gpu_encode_surface_provider", None),
            "handles_timeline_video",
            False,
        ):
            return None
        if getattr(self, "_export_has_alpha", False):
            return None
        if bool(getattr(config, "burn_subtitles", False)):
            # burn_subtitles 默认为 True（惰性语义：有字幕才烧）。直通
            # 排除条件与 _burn_in_subtitles 的实际改帧判据逐帧一致——
            # 该帧无活跃字幕时烧录是空操作，直通与合成路径逐像素等价。
            # 查询异常按 fail-closed 拒绝直通。
            try:
                if tuple(self._sequence.get_active_subtitle_clips(frame_idx)):
                    return None
            except Exception:
                return None

        encoder = getattr(self, "_encoder", None)
        encoder_config = getattr(encoder, "_config", None)
        spec_candidates = _passthrough_specs_for(encoder_config)
        if not spec_candidates:
            return None
        if not getattr(self, "_use_high_precision_export", False):
            # 高精度关闭时只保留非高精度规格（帧级按 pixel_format 精选
            # 之前先按精度过滤，避免无谓解码）。
            spec_candidates = tuple(
                spec for spec in spec_candidates
                if not spec.requires_high_precision)
            if not spec_candidates:
                return None
        try:
            output_siting = int(getattr(
                getattr(encoder, "_movie_cfg", None), "chroma_siting", 0))
        except (TypeError, ValueError, OverflowError):
            return None
        # Current TOPOS 4:2:2 export uses a co-sited horizontal chroma phase.
        # Reject other target phases before opening/reading the source frame.
        if output_siting not in (0, 2):
            return None

        layers = tuple(getattr(frame_plan, "layers", ()) or ())
        if (not getattr(frame_plan, "has_content", False)
                or len(layers) != 1
                or tuple(getattr(frame_plan, "transitions", ()) or ())):
            return None

        layer = layers[0]
        from src.shared.tracking.stabilization_state import stabilization_matrix
        if stabilization_matrix(self._clip_stabilization_state(layer), int(layer.source_frame)) is not None:
            return None
        clip = getattr(layer, "clip", None)
        if clip is None:
            return None

        # All visible per-clip geometry/opacity state is already evaluated in
        # ClipLayerInfo; exclude unbaked edits and time-varying source mapping.
        from src.features.edit.core.adjustment_composite import (
            transform_is_identity,
        )
        from src.features.edit.core.crop_cpu import crop_is_identity

        blend_mode = getattr(clip, "blend_mode", "normal")
        blend_name = str(getattr(blend_mode, "value", blend_mode))
        animated_params = getattr(clip, "animated_params", {}) or {}
        opacity = float(getattr(layer, "opacity", 1.0))
        if not math.isfinite(opacity):
            return None
        opacity = max(0.0, min(1.0, opacity))
        # 解码前先用候选集的乐观标量交付标志做排除（帧级规格在解码后
        # 按 pixel_format 精选；下方交付段有 scalar_delivery 一致性守卫）。
        supports_scalar_delivery = any(
            spec.scalar_delivery for spec in spec_candidates)
        if not supports_scalar_delivery and (
                animated_params
                or float(getattr(clip, "opacity", 1.0)) != 1.0
                or opacity != 1.0
                or int(getattr(clip, "fade_in_duration", 0) or 0) != 0
                or int(getattr(clip, "fade_out_duration", 0) or 0) != 0):
            return None
        if (not bool(getattr(clip, "is_enabled", False))
                or getattr(clip, "effects", ())
                or getattr(clip, "composite_id", None)
                or getattr(clip, "render_cache_id", None)
                or getattr(clip, "raw", None) is not None
                or getattr(clip, "freeze_source_frame", None) is not None
                or (supports_scalar_delivery
                    and any(str(name) != "opacity"
                            for name in animated_params))
                or blend_name != "normal"
                or not crop_is_identity(
                    tuple(getattr(layer, "crop", ()) or ()))
                or not transform_is_identity(
                    tuple(getattr(layer, "transform", ()) or ()))):
            return None

        speed = getattr(clip, "speed", 1.0)
        if isinstance(speed, (int, float)):
            normal_speed = float(speed) == 1.0
        else:
            normal_speed = (
                float(getattr(speed, "speed", 1.0)) == 1.0
                and not bool(getattr(speed, "reverse", False))
                and str(getattr(speed, "interpolation", "nearest")) == "nearest"
                and not tuple(getattr(speed, "speed_points", ()) or ())
            )
        if (not normal_speed
                or getattr(clip, "_source_pts_index", None) is not None):
            return None

        clip_start = int(getattr(clip, "timeline_start", 0))
        expected_source_frame = (
            int(getattr(clip, "source_start", 0))
            + int(frame_idx) - clip_start
        )
        if (int(getattr(layer, "source_frame", -1)) != expected_source_frame
                or not (clip_start <= frame_idx
                        < int(getattr(clip, "timeline_end", 0)))):
            return None

        if (self._get_clip_color_grade(layer) is not None
                or self._get_clip_video_denoise(layer) is not None):
            return None

        asset = self._project.get_asset_by_id(layer.asset_id) \
            if self._project is not None else None
        if asset is None or str(getattr(asset, "media_type", "")) != "video":
            return None

        output_width, output_height = self._get_output_resolution(config)
        sequence_width, sequence_height = self._get_sequence_resolution()
        resize_delivery = (
            supports_scalar_delivery
            and (output_width, output_height) != (sequence_width, sequence_height)
            and output_width % 2 == 0
            and output_height % 2 == 0
            and sequence_width % 2 == 0
            and sequence_height % 2 == 0
        )
        if ((not resize_delivery
             and (output_width, output_height) != (sequence_width, sequence_height))
                or (int(getattr(encoder_config, "width", 0)),
                    int(getattr(encoder_config, "height", 0)))
                != (output_width, output_height)):
            return None

        # The separate matte pass changes pixels after compositing; bypass it
        # only when the active program guide is not configured to burn in.
        settings = dict(getattr(self._sequence, "format_settings", {}) or {})
        guide = settings.get("program_guide")
        if isinstance(guide, dict) and guide.get("type") == "aspect_ratio":
            guide_params = guide.get("params", {})
            if (isinstance(guide_params, dict)
                    and bool(guide_params.get("fill_black", False))
                    and bool(guide_params.get("burn_in", False))):
                return None

        source_path = export_source_path(self._project, asset)
        if not source_path:
            return None

        # TOPOS clips are already delivered by ToposMediaSource as validated
        # planar frames.  Do not send them through the FFmpeg high-depth probe:
        # PyAV cannot decode the custom TPIC payload and, more importantly,
        # the container metadata does not distinguish the Proxy/LT/Standard/
        # HQ rate tiers well enough to prove that a raw bitstream copy is
        # semantically identical to the requested output tier.  Falling back
        # to the normal compositor is therefore both safer and faster than
        # retrying a doomed source open on every frame.
        asset_metadata = getattr(asset, "metadata", None)
        if isinstance(asset_metadata, dict):
            # 导入路径持久化的键是 "codec"（probe_media_metadata），
            # "codec_name" 兼容旧工程/手工构造的 asset。
            asset_codec_name = str(
                asset_metadata.get("codec_name")
                or asset_metadata.get("codec") or "")
        else:
            asset_codec_name = getattr(asset_metadata, "codec_name", "")
        session_key = self._layer_session_key(layer)
        if str(asset_codec_name or "").lower() == "topos":
            # Topos TPIC 中间片：FFmpeg 无法解码（打开必败），改走
            # provider 既有会话的原生解码平面（ToposMediaSource 契约：
            # planar uint16 + 逐平面 bit_depth/stride + 帧级 color/siting
            # 声明，与 T-03 渲染缓存直通同一解码契约）。此处交付的是
            # 解码后的像素平面而非容器位流拷贝——档位/格式语义由编码器
            # 按目标 spec 重新定标，早先"档位元数据不足以证明位流等价"
            # 的顾虑只适用于 raw copy，不适用于本路径。
            decoded = self._request_video_decoded_frame(
                source_path, expected_source_frame, layer.asset_id,
                session_key=session_key)
        else:
            request_high_depth = self._request_high_depth_decoded_frame
            try:
                decoded = request_high_depth(
                    source_path, expected_source_frame, layer.asset_id,
                    session_key=session_key)
            except TypeError as error:
                # Keep lightweight providers and older integrations compatible
                # while the production decoder uses the per-layer session key.
                if "session_key" not in str(error):
                    raise
                decoded = request_high_depth(
                    source_path, expected_source_frame, layer.asset_id)
        if decoded is None:
            return None

        metadata = getattr(decoded, "metadata", None)
        pixel_format = str(getattr(decoded, "pixel_format", "") or "").lower()
        spec = next(
            (spec for spec in spec_candidates
             if spec.source_pix_fmt == pixel_format),
            None)
        if (spec is None
                or metadata is None
                or str(getattr(metadata, "pixel_format", "") or "").lower()
                != pixel_format
                or int(getattr(metadata, "bit_depth", 0) or 0)
                != spec.source_bit_depth
                or bool(getattr(metadata, "has_alpha", False))
                or int(getattr(metadata, "rotation", 0) or 0) != 0
                or getattr(metadata, "field_order", None) not in
                (None, "", "progressive")
                or bool(getattr(decoded, "is_proxy", False))):
            return None
        if resize_delivery and not spec.scalar_delivery:
            # 帧级精选出的规格不支持标量交付时，放弃此前按候选集乐观
            # 判定的 resize 通道（当前白名单内该组合不存在，纯防御）。
            return None

        # The passthrough copies chroma samples without resampling, so the
        # source and container must describe the same sample location.
        # PyAV/FFmpeg currently omits this tag for ProRes 4:2:2; that codec's
        # conventional 4:2:2 layout is left-sited, matching TOPOS' default.
        # H.264 4:2:0 leaves the tag unsignaled in practice; its MPEG-2
        # style convention is likewise left-sited, matching both x264 and
        # the FFmpeg encoder's implicit siting (0). Other untagged formats
        # are ambiguous and must use the normal render path. Explicit
        # per-frame metadata takes precedence over stream tags.
        frame_extra = getattr(decoded, "extra", None) or {}
        source_siting = frame_extra.get(
            "chroma_siting",
            frame_extra.get(
                "topos_chroma_siting",
                getattr(metadata, "chroma_siting", None)))
        if source_siting is None:
            source_codec = str(getattr(metadata, "codec_name", "") or "").lower()
            if source_codec == "prores" and pixel_format == "yuv422p10le":
                source_siting = 0
            elif (source_codec in spec.untagged_siting_codecs
                    and pixel_format == spec.source_pix_fmt):
                source_siting = 0
            else:
                return None
        try:
            source_siting = int(source_siting)
        except (TypeError, ValueError, OverflowError):
            return None
        siting_matches = source_siting == output_siting
        if (not siting_matches
                and _chroma_domain_of(spec.target_pix_fmt) == "422"
                and source_siting in (0, 2) and output_siting in (0, 2)):
            # 4:2:2 交付无垂直色度子采样；left(0) 与 top-left(2) 共享同一
            # 水平色度相位（豁免按交付目标域判定——420 源上采样到 422 后
            # 同样适用，2026-09-17 扩展）。
            siting_matches = True
        if not siting_matches:
            return None

        source_fps = self._rational_pair(getattr(metadata, "fps", None))
        sequence_fps = self._rational_pair(getattr(self._sequence, "fps", None))
        output_fps = self._rational_pair(getattr(encoder_config, "fps", None))
        if (source_fps is None or sequence_fps is None
                or source_fps != sequence_fps or output_fps != sequence_fps):
            return None

        sample_aspect_ratio = getattr(metadata, "sample_aspect_ratio", None)
        if sample_aspect_ratio is not None:
            if self._rational_pair(sample_aspect_ratio) != (1, 1):
                return None

        source_color = {
            "primaries": getattr(metadata, "color_primaries", None),
            "transfer": getattr(metadata, "color_transfer", None),
            "matrix": getattr(metadata, "color_matrix", None),
            "range": getattr(metadata, "color_range", None),
        }
        output_color = getattr(encoder_config, "color_metadata", None)
        if output_color is None:
            return None
        for key, value in source_color.items():
            target = getattr(output_color, key, None)
            if (not value or not target
                    or str(value).lower() != str(target).lower()):
                return None

        source_width, source_height = (
            (sequence_width, sequence_height)
            if resize_delivery else (output_width, output_height)
        )
        planes = tuple(getattr(decoded, "planes", ()) or ())
        plane_infos = tuple(getattr(decoded, "plane_infos", ()) or ())
        if (getattr(decoded, "data", None) is not None
                or len(planes) != 3 or len(plane_infos) != 3
                or int(getattr(decoded, "width", 0) or 0) != source_width
                or int(getattr(decoded, "height", 0) or 0) != source_height
                or int(getattr(metadata, "width", 0) or 0) != source_width
                or int(getattr(metadata, "height", 0) or 0) != source_height):
            return None

        chroma_width = (source_width + 1) // 2
        if spec.chroma_subsampling == "422":
            chroma_height = source_height
        else:  # "420"
            chroma_height = (source_height + 1) // 2
        expected_shapes = (
            (source_height, source_width),
            (chroma_height, chroma_width),
            (chroma_height, chroma_width),
        )
        plane_dtype = np.uint16 if spec.source_bit_depth > 8 else np.uint8
        for plane, info, shape in zip(planes, plane_infos, expected_shapes):
            if (not isinstance(plane, np.ndarray)
                    or plane.dtype != plane_dtype
                    or plane.shape != shape
                    or not plane.flags.c_contiguous
                    or int(getattr(info, "width", 0)) != shape[1]
                    or int(getattr(info, "height", 0)) != shape[0]
                    or int(getattr(info, "bit_depth", 0)) != spec.source_bit_depth
                    or int(getattr(info, "stride", 0)) != plane.strides[0]):
                return None

        output_metadata = metadata
        output_plane_infos = plane_infos
        output_pixel_format = pixel_format
        # 10-bit 源 → 8-bit 档：YUV 域右移降位（round-half-up）。limited
        # range 锚点精确整除（Y 64→16、940→235；UV 64→16、960→240，因
        # (940-64)/(235-16)=(960-64)/(240-16)=4），全程不引入 RGB 往返。
        downconvert_depth = int(
            getattr(spec, "downconvert_source_bit_depth", 0) or 0)
        effective_bit_depth = spec.source_bit_depth
        if downconvert_depth:
            shift = spec.source_bit_depth - downconvert_depth
            half = 1 << (shift - 1)
            planes = tuple(
                ((plane + half) >> shift).astype(np.uint8)
                for plane in planes)
            effective_bit_depth = downconvert_depth
        # 目标与源的色度子采样域不同（如 420 源 → 422 交付）时也必须
        # 重排平面：交付平面几何必须与编码器目标一致。
        chroma_reshape_needed = (
            _chroma_domain_of(spec.target_pix_fmt)
            != _chroma_domain_of(pixel_format))
        if (resize_delivery or opacity < 1.0 - 1e-6
                or downconvert_depth or chroma_reshape_needed):
            # Match the established RGB scaler's half-pixel bilinear phase,
            # but keep the data in the encoder's limited-range YUV domain.
            # Opacity/fade is applied around the limited-range black/neutral
            # point, which is the YUV equivalent of fading RGB to black. This
            # removes both YUV→BGR and BGR→YUV conversions for the common
            # ungraded 8-bit 4:2:0 SDR delivery case (H.264/HEVC).
            import cv2

            # 色度目标几何按编码器目标的子采样域（420 半高 / 422 全高）——
            # 源 spec 的 chroma_subsampling 只描述解码平面验证形状。
            # 此前硬编码 420 半高，DNxHR/ProRes 8-bit 422 交付降位后
            # 色度平面字节数对不上（编码器 "got N bytes; need M" 失败）。
            target_pix_lower = str(spec.target_pix_fmt or "").lower()
            if "420" in target_pix_lower:
                target_chroma = (output_width // 2, output_height // 2)
            elif "422" in target_pix_lower:
                target_chroma = (output_width // 2, output_height)
            else:  # 444/无子采样：色度全分辨率
                target_chroma = (output_width, output_height)

            def resize_and_fade(
                plane: np.ndarray,
                target_size: Tuple[int, int],
                neutral: float,
                low: float,
                high: float,
            ) -> np.ndarray:
                if opacity >= 1.0 - 1e-6:
                    resized = plane
                    if (plane.shape[1], plane.shape[0]) != target_size:
                        resized = cv2.resize(
                            plane, target_size, interpolation=cv2.INTER_LINEAR)
                    return np.ascontiguousarray(resized)
                resized = plane.astype(np.float32)
                if (plane.shape[1], plane.shape[0]) != target_size:
                    resized = cv2.resize(
                        resized, target_size, interpolation=cv2.INTER_LINEAR)
                faded = neutral + (
                    resized - neutral) * opacity
                return np.rint(np.clip(faded, low, high)).astype(np.uint8)

            planes = (
                resize_and_fade(
                    planes[0], (output_width, output_height),
                    16.0, 16.0, 235.0),
                resize_and_fade(
                    planes[1], target_chroma, 128.0, 16.0, 240.0),
                resize_and_fade(
                    planes[2], target_chroma, 128.0, 16.0, 240.0),
            )
            from dataclasses import replace as dataclass_replace
            from src.shared.media.frame import PlaneInfo

            output_plane_infos = tuple(
                PlaneInfo.create(
                    index,
                    int(plane.shape[1]),
                    int(plane.shape[0]),
                    str(getattr(info, "component", ("Y", "U", "V")[index])),
                    plane.dtype,
                    effective_bit_depth,
                )
                for index, (plane, info) in enumerate(
                    zip(planes, plane_infos))
            )
            try:
                replace_fields: dict = {
                    "width": output_width, "height": output_height}
                if downconvert_depth:
                    # 元数据随交付域改写（与 plane_infos/pixel_format 一致），
                    # 避免下游按 metadata.bit_depth 判读时拿到残留的 10-bit。
                    replace_fields["bit_depth"] = downconvert_depth
                    replace_fields["pixel_format"] = spec.target_pix_fmt
                output_metadata = dataclass_replace(
                    metadata, **replace_fields)
            except (TypeError, ValueError):
                # Lightweight metadata test doubles may not be dataclasses;
                # frame dimensions and plane descriptors remain authoritative.
                output_metadata = metadata

        # 交付平面被重排（降位/色度域变化）后，帧声明的像素格式随目标
        # spec 改写——编码器据此走同格式直填零转换路径。
        if downconvert_depth or chroma_reshape_needed:
            output_pixel_format = spec.target_pix_fmt

        # Rebase source metadata onto output time. ToposVideoEncoder currently
        # assigns mux PTS in submission order, but keeping the DecodedFrame
        # contract correct also protects future encoder implementations.
        from src.shared.media.frame import DecodedFrame

        output_index = int(frame_idx) - int(
            getattr(self, "_export_start_frame", 0))
        output_fps_float = self._get_sequence_fps()
        return DecodedFrame(
            frame_index=output_index,
            pts=output_index,
            time_seconds=(output_index / output_fps_float
                          if output_fps_float > 0.0 else 0.0),
            planes=planes,
            pixel_format=output_pixel_format,
            width=output_width,
            height=output_height,
            metadata=output_metadata,
            plane_infos=output_plane_infos,
            is_proxy=False,
            extra=getattr(decoded, "extra", None),
        )

    def _apply_program_guide_matte(self, frame_bgr_u8: "np.ndarray") -> "np.ndarray":
        """Burn the configured Program Viewer cinema matte into an export frame."""
        settings = dict(getattr(self._sequence, "format_settings", {}) or {})
        guide = settings.get("program_guide")
        if not isinstance(guide, dict) or guide.get("type") != "aspect_ratio":
            return frame_bgr_u8

        params = guide.get("params", {})
        if not isinstance(params, dict):
            return frame_bgr_u8
        if not bool(params.get("fill_black", False)) or not bool(params.get("burn_in", False)):
            return frame_bgr_u8

        try:
            ratio = float(params.get("ratio", 0.0))
        except (TypeError, ValueError):
            return frame_bgr_u8
        if frame_bgr_u8.ndim == 3 and frame_bgr_u8.shape[-1] == 4:
            # R2：Alpha 导出——遮版仅作用于颜色通道（黑色条带上保留内容
            # alpha；该组合为预览导出特性，语义已在 ADR-C013 记录）
            out = frame_bgr_u8.copy()
            out[..., :3] = apply_program_guide_matte_bgr(frame_bgr_u8[..., :3], ratio)
            return out
        return apply_program_guide_matte_bgr(frame_bgr_u8, ratio)

    def _burn_in_subtitles(
        self,
        bgr_u8: "np.ndarray",
        frame_idx: int,
        width: int,
        height: int,
    ) -> "np.ndarray":
        """把当前帧的活跃字幕烧录到 BGR 8/16-bit 帧上。

        Args:
            bgr_u8: 输入帧 (h, w, 3) BGR 或 (h, w, 4) BGRA（R2：Alpha 导出
                传入 4 通道，字幕 over 尊重底图 alpha）
            frame_idx: 时间线帧号
            width, height: 输出分辨率

        Returns:
            合成后的帧（与输入同通道数/位深/尺寸）。
        """
        try:
            subs = self._sequence.get_active_subtitle_clips(frame_idx)
        except Exception:
            logger.exception("Failed to query active subtitle clips")
            return bgr_u8
        if not subs:
            return bgr_u8

        try:
            import numpy as np
            from src.features.edit.core.subtitle_renderer import (
                render_subtitle_clip_to_rgba_cached as _render_cached,
                composite_over,
            )
            from src.features.edit.core.captions.model import (
                resolve_style, EMPTY_STYLE, clip_style_base,
            )
        except Exception:
            logger.exception("Failed to import subtitle_renderer for burn-in")
            return bgr_u8

        h, w = bgr_u8.shape[:2]
        # 输入可能是 width/height 参数不匹配 bgr_u8 尺寸；以实际 buffer 为准。
        if w <= 0 or h <= 0:
            return bgr_u8

        # BGR 8/16-bit → straight-alpha RGBA float32（用作 over 合成的 bottom）
        # RGB 直接归一化到 [0,1]；4ch 输入用真实 alpha（R2），3ch 输入
        # alpha = 1（完全不透明背景）
        max_value = (
            float(np.iinfo(bgr_u8.dtype).max)
            if np.issubdtype(bgr_u8.dtype, np.integer) else 1.0
        )
        has_alpha_channel = bgr_u8.ndim == 3 and bgr_u8.shape[-1] == 4
        rgb = bgr_u8[..., :3][..., ::-1].astype(np.float32) / max_value  # BGR → RGB
        if has_alpha_channel:
            bottom_alpha = self._normalise_alpha_channel(bgr_u8[..., 3:4])
        else:
            bottom_alpha = np.ones((h, w, 1), dtype=np.float32)
        bg_rgba = np.concatenate([rgb, bottom_alpha], axis=-1)

        # 对每个字幕片段渲染全画布 RGBA，依次 over 合成（按轨道顺序）
        # 使用 cached 版本：同一字幕在它的 timeline_duration 内每帧都重复渲染，
        # 缓存按内容签名（排除变换）避免重复光栅化，导出长视频不会卡。
        for clip in subs:
            try:
                # SUB-P2-01/06: resolve layered style (track-default ⊕
                # cue-override) so captions render with their true style, not
                # the legacy flat fields. The track is local here; its
                # default_style lives under caption_metadata.
                track = self._sequence.get_track_by_id(clip.track_id) \
                    if hasattr(self._sequence, "get_track_by_id") else None
                track_default = (
                    track.caption_metadata.default_style
                    if track is not None and getattr(track, "caption_metadata", None) is not None
                    and track.caption_metadata.default_style is not None
                    else EMPTY_STYLE
                )
                style = resolve_style(track_default, getattr(clip, "style_override", None),
                                      base=clip_style_base(clip))
                track_style = next((item for item in getattr(self._sequence, "caption_track_graphic_styles", ())
                                    if item.track_id == clip.track_id and item.enabled), None)
                if track_style is not None:
                    from src.features.edit.core.captions.graphics import (
                        render_caption_graphic_rgba,
                        template_parameters,
                    )
                    template = self._sequence.get_caption_graphic_template_registry().get(
                        track_style.template_id, track_style.template_version
                    )
                    if template is None:
                        if not track_style.fallback_to_native:
                            logger.error("Caption track template missing: %s@%s", track_style.template_id, track_style.template_version)
                            continue
                        sub_rgba = _render_cached(clip, w, h, style=style)
                    else:
                        sub_rgba = render_caption_graphic_rgba(
                            clip, track, template,
                            template_parameters(template, track_style.parameters),
                            w, h, frame_idx - clip.timeline_start,
                        )
                else:
                    sub_rgba = _render_cached(clip, w, h, style=style)
            except Exception:
                logger.exception(
                    f"Subtitle burn-in render failed for clip {clip.clip_id}"
                )
                continue
            if sub_rgba is None:
                continue
            # 尺寸匹配（subtitle_renderer 保证与请求的 w/h 一致）
            if sub_rgba.shape[:2] != (h, w):
                continue
            bg_rgba = composite_over(sub_rgba, bg_rgba)

        # RGBA → BGR，并恢复输入位深与通道数。字幕的 alpha 合成在 float32
        # 中完成，避免高位深底图先降到 8-bit 才烧录文字。
        rgb_out = np.clip(bg_rgba[..., :3], 0.0, 1.0)
        if has_alpha_channel:
            a_out = np.clip(bg_rgba[..., 3:4], 0.0, 1.0)
            out = np.rint(
                np.concatenate([rgb_out[..., ::-1], a_out], axis=-1) * max_value
            ).astype(bgr_u8.dtype)
            return out
        bgr_out = np.rint(rgb_out * max_value).astype(bgr_u8.dtype)
        bgr_out = bgr_out[..., ::-1]  # RGB → BGR
        return bgr_out


    def _get_sequence_resolution(self) -> Tuple[int, int]:
        """
        获取时间线原始分辨率。

        TimelineSequence 的 width/height 是直接字段（不是 settings.width）。
        """
        width = int(getattr(self._sequence, 'width', 0) or 0)
        height = int(getattr(self._sequence, 'height', 0) or 0)
        if width <= 0 or height <= 0:
            # 兜底：从第一个视频 clip 的 asset 推断
            for track in getattr(self._sequence, 'video_tracks', ()) or ():
                for clip in track.clips:
                    asset = self._project.get_asset_by_id(clip.asset_id) if self._project else None
                    if asset is not None:
                        md = getattr(asset, 'metadata', None) or {}
                        w = int(md.get('width', 0) or 0)
                        h = int(md.get('height', 0) or 0)
                        if w > 0 and h > 0:
                            return (w, h)
            # 最终兜底
            return (1920, 1080)
        return (width, height)

    def _get_sequence_fps(self) -> float:
        """
        获取时间线帧率（float）。

        TimelineSequence.fps 是 Rational，需调用 to_fps() 转 float。
        """
        seq_fps = getattr(self._sequence, 'fps', None)
        if seq_fps is None:
            return 24.0
        if hasattr(seq_fps, 'to_fps'):
            try:
                return float(seq_fps.to_fps())
            except Exception:
                return 24.0
        try:
            return float(seq_fps)
        except Exception:
            return 24.0

    def _get_sequence_fps_rational(self):
        """
        获取时间线帧率的精确 Rational（P1-15 时间基准契约）。

        导出中的帧→样本定位必须走精确有理数运算：float(24000/1001) 的
        逐帧 ``int(n / fps * sample_rate)`` 会在长时长下产生 ±1 样本抖动，
        毫秒量化（23.976 ≠ 24000/1001）更会造成系统性 A/V 漂移。
        无 Rational 时从 float 构造（保持既有毫秒精度语义）。
        """
        from src.shared.media.metadata import Rational

        seq_fps = getattr(self._sequence, "fps", None)
        if seq_fps is not None and hasattr(seq_fps, "num") and hasattr(seq_fps, "den"):
            return seq_fps
        return Rational(int(round(self._get_sequence_fps() * 1000)), 1000)

    def _get_output_resolution(
        self,
        config: TimelineExportConfig,
    ) -> Tuple[int, int]:
        """获取输出分辨率"""
        if config.resolution:
            return config.resolution
        # 使用时间线分辨率（TimelineSequence 的 width/height 是直接字段）
        return self._get_sequence_resolution()

    def _create_black_frame(
        self,
        width: int,
        height: int,
    ) -> "np.ndarray":
        """创建黑色帧，按当前交付位深选择 8/16-bit 工作缓冲。

        R2：Alpha 导出下"无内容"= 全透明黑（straight alpha 约定——空时间线
        区域完全透明，而非不透明黑）。
        """
        import numpy as np
        dtype = np.uint16 if getattr(self, "_use_high_precision_export", False) else np.uint8
        channels = 4 if getattr(self, "_export_has_alpha", False) else 3
        return np.zeros((height, width, channels), dtype=dtype)

    @staticmethod
    def _normalise_alpha_channel(alpha: "np.ndarray") -> "np.ndarray":
        """uint8/uint16/float alpha 平面 → float32 [0,1]。

        R2 修复隐藏 bug：此前硬编码 ``/255``，uint16 层的 alpha 被压到近 0
        （错 257 倍）。
        """
        import numpy as np

        if np.issubdtype(alpha.dtype, np.integer):
            return alpha.astype(np.float32) / float(np.iinfo(alpha.dtype).max)
        return np.clip(alpha.astype(np.float32), 0.0, 1.0)

    @staticmethod
    def _unpremultiply_rgba(pm: "np.ndarray") -> "np.ndarray":
        """预乘 RGBA float → straight RGBA（零 alpha 像素 RGB 归零，无除零）。"""
        import numpy as np

        a = pm[..., 3:4]
        safe = np.maximum(a, 1e-6)
        rgb = np.where(a > 1e-6, pm[..., :3] / safe, 0.0)
        return np.concatenate([rgb, a], axis=-1).astype(np.float32)

    @staticmethod
    def _pixel_format_is_high_depth(pixel_format: str) -> bool:
        """Return whether an encoder pixel format can retain more than 8 bits."""
        lower = (pixel_format or "").lower()
        return any(token in lower for token in ("10", "12", "14", "16", "48", "64"))

    @staticmethod
    def _normalise_bgr_frame(frame: "np.ndarray") -> "np.ndarray":
        """Convert BGR integer/float samples to a float32 buffer.

        复验 P1-04：浮点输入**不再无条件裁到 [0,1]**——普通 float 层的
        场景线性工作值 >1.0（HDR 高光）此前在进合成前就被削平。整数输入
        按满刻度归一化天然落在 [0,1]；浮点 4 通道的 alpha 仍裁 [0,1]
        （coverage 语义域）。RGB >1 值沿合成链保留，量化时按导出传输
        OETF 编码（`_quantize_export_frame`）或 SDR 可见裁剪。
        """
        import numpy as np

        if np.issubdtype(frame.dtype, np.integer):
            return frame.astype(np.float32) / float(np.iinfo(frame.dtype).max)
        out = frame.astype(np.float32)
        if out.ndim == 3 and out.shape[-1] == 4:
            out[..., 3] = np.clip(out[..., 3], 0.0, 1.0)
        return out

    def _quantize_export_frame(self, frame: "np.ndarray") -> "np.ndarray":
        """Quantize the float compositor only at the selected delivery depth.

        复验 P1-04：HDR 导出（PQ/HLG 传输）的浮点线性值先经显式 OETF
        （与 Comp Clip 路径同一范式）再量化；SDR 传输无 >1 表示域，
        高光在量化边界可见裁剪（显式、非无条件静默）。
        """
        import numpy as np

        f = frame
        if np.issubdtype(f.dtype, np.floating) and f.ndim == 3:
            # 复验 P1-04：只有 **>1.0 的浮点值**（场景线性 HDR 高光）才需要
            # OETF——整数/信号域源归一化后 ≤1，已是编码信号，再走 OETF
            # 会二次编码（HDR 信号域 240/255 被压暗）。SDR 传输无 >1 表示
            # 域，超出高光在量化边界可见裁剪（一次性警告）。
            rgb = f[..., :3]
            over = float(np.max(rgb)) if rgb.size else 0.0
            if over > 1.0 + 1e-6:
                meta = getattr(self, "_export_color_metadata", None)
                transfer = str(
                    getattr(meta, "transfer", "bt709") or "bt709").lower()
                if transfer in ("pq", "smpte2084", "hlg", "arib-std-b67"):
                    sig = self._linear_bgr_to_export_signal(
                        f,
                        transfer=transfer,
                        primaries=str(getattr(meta, "primaries", "bt709")
                                      or "bt709"),
                        color_range=str(getattr(meta, "range", "limited")
                                        or "limited"),
                    )
                    if sig is not None:
                        # OETF 只作用于 RGB；alpha（若有）原样保留
                        if f.shape[-1] == 4:
                            f = np.concatenate(
                                [sig, np.clip(f[..., 3:4], 0.0, 1.0)],
                                axis=-1)
                        else:
                            f = sig
                    else:
                        logger.warning(
                            "HDR OETF 转换失败——高光按 SDR 边界裁剪（显式降级）")
                elif not getattr(self, "_sdr_clip_warned", False):
                    self._sdr_clip_warned = True
                    logger.warning(
                        "导出传输为 SDR（%s）但合成值最大 %.3f >1.0——高光将"
                        "在量化边界裁剪；如需 HDR 高光请在时间线色彩设置选择"
                        " PQ/HLG 传输", transfer, over)
        if getattr(self, "_use_high_precision_export", False):
            return np.rint(np.clip(f, 0.0, 1.0) * 65535.0).astype(np.uint16)
        return np.rint(np.clip(f, 0.0, 1.0) * 255.0).astype(np.uint8)

    def _composite_layers(
        self,
        layers: Tuple["ClipLayerInfo", ...],
        transitions: Tuple["TransitionBlendInfo", ...],
        config: TimelineExportConfig,
        frame_idx: int = 0,
    ) -> Optional["np.ndarray"]:
        """
        合成多个层。

        Args:
            layers: ClipLayerInfo 列表
            transitions: TransitionBlendInfo 列表
            config: 导出配置
            frame_idx: 当前时间线帧号（用于 effect 关键帧插值）

        Returns:
            合成后的帧数据（BGR uint8）
        """
        import numpy as np
        from src.features.edit.core.crop_cpu import apply_crop_cpu, crop_is_identity

        if not layers:
            return None

        # 输出分辨率
        width, height = self._get_output_resolution(config)

        # O3：保守遮挡裁剪——与预览/GPU 路径共享同一份判定语义
        # （timeline_occlusion.analyze_visible_layers）。被确定性遮挡的
        # 下层不再进入取帧/解码和 CPU/GPU/Metal 合成队列；任何不确定
        # （Alpha、转场、效果、几何）都 fail-closed 保留全部层。
        try:
            from src.features.edit.core.timeline_occlusion import (
                analyze_visible_layers,
            )
            decision = analyze_visible_layers(
                tuple(layers), tuple(transitions), int(frame_idx), (width, height),
                mode="export",
                facts_of=self._occlusion_facts_for_layer,
                coordinate_size=self._get_sequence_resolution(),
            )
            if decision.culled:
                layers = decision.render_layers
        except Exception as occlusion_error:  # noqa: BLE001 — 裁剪失败不阻断导出
            logger.warning(
                "Export occlusion analysis failed, keeping all layers: %s",
                occlusion_error,
            )

        # 按 track_index 排序（从下到上）
        sorted_layers = sorted(layers, key=lambda l: l.track_index)

        # R2（P0-1）：Alpha 导出 → 合成缓冲为 4 通道（预乘内部表示，
        # straight 对外），Porter-Duff over 语义
        export_alpha = bool(getattr(self, "_export_has_alpha", False))

        # 支持时直接在 GPU 上把最终 RGB 转成编码器需要的 YUV420P，
        # 避免 BGR 中间帧回读与 CPU 色彩转换。其它格式沿用 BGR 路径。
        gpu_yuv420p = self._gpu_yuv420_output_allowed(
            config, width, height, frame_idx=frame_idx)
        gpu_yuv422p10 = self._gpu_yuv422p10_output_allowed(
            config, width, height, frame_idx=frame_idx)
        gpu_frame = self._gpu_render_composite(
            sorted_layers, transitions, width, height,
            output_yuv420p=gpu_yuv420p,
            output_yuv422p10=gpu_yuv422p10,
            frame_idx=frame_idx)
        if gpu_frame is not None:
            return gpu_frame

        # 创建合成缓冲区（float32）
        composite: Optional[np.ndarray] = None

        # 转场映射
        transition_map: Dict[str, "TransitionBlendInfo"] = {}
        for trans in transitions:
            if trans.left_clip_id:
                transition_map[trans.left_clip_id] = trans
            if trans.right_clip_id:
                transition_map[trans.right_clip_id] = trans

        # 两源转场预合成：空间转场使用 mask；全画面转场使用内容驱动的
        # 双源合成，结果作为单层注入。
        wipe_composited: Dict[str, np.ndarray] = {}  # transition_id -> float32 [0-1] frame
        wipe_skip_ids: set[str] = set()
        wipe_inject: Dict[str, str] = {}  # left_clip_id -> transition_id
        for trans in transitions:
            if trans.transition_type not in COMPOSITE_TRANSITION_TYPES:
                continue
            left_layer = next((l for l in sorted_layers if l.clip_id == trans.left_clip_id), None)
            right_layer = next((l for l in sorted_layers if l.clip_id == trans.right_clip_id), None)
            if left_layer is None and right_layer is None:
                continue
            left_frame = self._get_layer_frame(left_layer) if left_layer is not None else None
            right_frame = self._get_layer_frame(right_layer) if right_layer is not None else None
            if left_frame is None and right_frame is None:
                continue
            if left_frame is not None and left_layer is not None:
                left_frame = self._prepare_transition_side_frame(
                    left_layer, left_frame, trans, frame_idx, width, height)
                left_f = self._normalise_bgr_frame(left_frame)
            else:
                left_f = None
            if right_frame is not None and right_layer is not None:
                right_frame = self._prepare_transition_side_frame(
                    right_layer, right_frame, trans, frame_idx, width, height)
                right_f = self._normalise_bgr_frame(right_frame)
            else:
                right_f = None
            template = left_f if left_f is not None else right_f
            if template is None:
                continue
            if left_f is None:
                left_f = np.zeros_like(template)
            if right_f is None:
                right_f = np.zeros_like(template)
            # 对齐尺寸/通道
            if left_f.shape != right_f.shape:
                import cv2
                if left_f.ndim == 2:
                    left_f = cv2.cvtColor(left_f, cv2.COLOR_GRAY2BGR) if left_f.max() > 1.5 else left_f[:, :, None]
                if right_f.ndim == 2:
                    right_f = right_f[:, :, None]
                right_f = cv2.resize(right_f, (left_f.shape[1], left_f.shape[0]), interpolation=cv2.INTER_AREA)
            # LIB1/X09（L03/L05）：双源重采样合成；user_* 按 base 判定。
            from src.features.edit.core.transition_catalog import (
                resolve_transition_base_type,
            )
            if resolve_transition_base_type(trans.transition_type) \
                    in RESAMPLE_TRANSITION_TYPES:
                # LIB1 双源复合家族（X09 + L14/L15）：统一分派器路由。
                from src.features.edit.transition_packs.library_v1.resample_cores import (
                    composite_lib1_transition,
                )
                wipe_composited[trans.transition_id] = composite_lib1_transition(
                    resolve_transition_base_type(trans.transition_type),
                    left_f,
                    right_f,
                    trans.blend_factor,
                    dict(trans.transition.params or {}),
                )
            elif trans.transition_type in FULL_FRAME_TRANSITION_TYPES:
                wipe_composited[trans.transition_id] = apply_full_frame_transition(
                    left_f,
                    right_f,
                    trans.blend_factor,
                    trans.transition_type,
                    trans.transition.params,
                    first_opacity=trans.left_opacity,
                    second_opacity=trans.right_opacity,
                )
            else:
                mask = compute_transition_mask(
                    left_f.shape[1], left_f.shape[0], trans.blend_factor,
                    trans.transition_type, trans.transition.params,
                )
                mask_c = mask[:, :, None] if left_f.ndim == 3 else mask
                # Apply each source opacity before the spatial wipe. Multiplying
                # both opacities would attenuate the pre-composite twice.
                blended = (
                    left_f * (1.0 - mask_c) * max(0.0, min(1.0, trans.left_opacity))
                    + right_f * mask_c * max(0.0, min(1.0, trans.right_opacity))
                )
                wipe_composited[trans.transition_id] = blended
            if trans.left_clip_id:
                wipe_skip_ids.add(trans.left_clip_id)
            if trans.right_clip_id:
                wipe_skip_ids.add(trans.right_clip_id)
            inject_id = trans.left_clip_id or trans.right_clip_id
            if inject_id:
                wipe_inject[inject_id] = trans.transition_id

        # 遍历层进行合成
        for layer in sorted_layers:
            # Wipe 的两个原始层已预合成，跳过；在 left 层位置注入预合成结果
            if layer.clip_id in wipe_skip_ids:
                if layer.clip_id in wipe_inject:
                    layer_float = wipe_composited[wipe_inject[layer.clip_id]]
                    if export_alpha and layer_float.ndim == 3 and layer_float.shape[2] == 4:
                        # R2：wipe 预合成为 straight RGBA → 转预乘后按既有
                        # opacity 注入语义合成（对预乘缓冲的 dissolve）
                        pm_rgb = layer_float[..., :3] * layer_float[..., 3:4]
                        injected = np.concatenate(
                            [pm_rgb, layer_float[..., 3:4]], axis=-1)
                        op = float(layer.opacity)
                        if composite is None:
                            composite = injected * op
                        else:
                            composite = composite * (1.0 - op) + injected * op
                    elif composite is None:
                        composite = layer_float.copy()
                    else:
                        composite = composite * (1.0 - layer.opacity) + layer_float * layer.opacity
                continue

            # 获取层帧数据
            layer_frame = self._get_layer_frame(layer)
            # A3.2: frame_blend 双源帧加权（慢速/斜坡消频闪）
            if layer_frame is not None:
                from src.features.edit.core.timeline_frame_mapper import (
                    _apply_frame_blend_layer,
                )
                layer_frame = _apply_frame_blend_layer(
                    layer, layer_frame, frame_idx, self._get_layer_frame)
                # F2.8：optical_flow 未实现——显式告警后按 nearest，
                # 不得静默降级（与预览 GPU 门控同语义）。遮挡分析等
                # 消费方以最小 ClipLayerInfo 契约喂层（无 clip 对象），
                # 速度插值告警按缺席=无告警处理。
                _clip_info = getattr(layer, "clip", None)
                _speed = getattr(_clip_info, "speed", None)
                if _clip_info is not None \
                        and getattr(_speed, "interpolation", "") == "optical_flow" \
                        and _clip_info.clip_id not in _OPTICAL_FLOW_WARNED:
                    _OPTICAL_FLOW_WARNED.add(_clip_info.clip_id)
                    logger.warning(
                        "clip %s: optical_flow 插值未实现，导出按 "
                        "nearest 采样（F2.8 显式告知）",
                        _clip_info.clip_id)

            if layer_frame is None:
                # FX-C04: Adjustment Clips (and other no-source layers) must
                # not be silently dropped on export. When the layer is an
                # adjustment, apply its effect chain to the accumulated
                # composite (everything composited below it) — the export
                # mirror of the preview post-composite pass. Scope is correct
                # because the loop processes layers bottom→top.
                _asset = self._project.get_asset_by_id(layer.asset_id)
                if _asset is not None and getattr(_asset, "media_type", "") == "adjustment":
                    if composite is not None:
                        # T-P1-22：非空 ColorStack 必须影响下方轨道——
                        # 此前 adjustment 层的 grade 被静默丢弃。
                        adjusted = self._apply_color_grade(composite, layer)
                        adjusted = self._apply_clip_effects_cpu(
                            adjusted, layer, frame_idx)
                        from src.features.edit.core.adjustment_composite import (
                            apply_transform_cpu,
                            blend_adjustment_cpu,
                        )
                        adjusted = apply_transform_cpu(
                            adjusted,
                            getattr(layer, "transform", None),
                            width,
                            height,
                        )
                        composite = blend_adjustment_cpu(
                            composite,
                            adjusted,
                            getattr(layer, "transform", None),
                            getattr(layer, "opacity", 1.0),
                        )
                    continue
                logger.warning(f"Failed to get frame for layer {layer.clip_id}")
                continue

            # P1-COLOR-13: per-clip GPU 降噪（调色之前，与预览管线顺序
            # 一致：denoise → grade）
            layer_frame = self._apply_video_denoise_export(layer_frame, layer)

            # 应用 clip 的 ColorStack 调色（CPU 纯 numpy，worker 线程安全）
            # 对齐 TimelinePreviewRenderer._apply_preview_color_grade 的实现，
            # 保证导出与实时预览的 primary grade 像素级一致。
            layer_frame = self._apply_color_grade(layer_frame, layer)

            # FX-M06: Apply edge crop (after grade, before effects)
            crop = tuple(getattr(layer, "crop", ()) or ())
            if not crop_is_identity(crop):
                layer_frame = apply_crop_cpu(layer_frame, crop)

            # Phase 4: 应用 clip 的视频滤镜链（CPU numpy 实现）。
            # 与实时预览的 GPU EffectPassRunner 等价但走 CPU 路径（导出
            # 管线无 moderngl.Context）。目前支持 blur/sharpen/glow 三种
            # 高频效果；其余效果 passthrough（仅记 warning），保证导出
            # 不会因不支持的效果而崩溃。
            layer_frame = self._apply_clip_effects_cpu(
                layer_frame, layer, frame_idx)

            # 获取有效 opacity（考虑转场）
            effective_opacity = layer.opacity
            if layer.clip_id in transition_map:
                trans_info = transition_map[layer.clip_id]
                if layer.clip_id == trans_info.left_clip_id:
                    effective_opacity = trans_info.left_opacity
                elif layer.clip_id == trans_info.right_clip_id:
                    effective_opacity = trans_info.right_opacity

            # 调整帧尺寸
            layer_frame = self._resize_frame(layer_frame, width, height)

            # CPU fallback is also responsible for clip geometry that the
            # Metal path declines. Match preview by transforming the
            # viewport-sized layer before it enters the layer composite.
            transform = tuple(getattr(layer, "transform", ()) or ())
            if transform:
                from src.features.edit.core.adjustment_composite import (
                    apply_transform_cpu,
                    transform_is_identity,
                )
                if not transform_is_identity(transform):
                    coordinate_w, coordinate_h = self._get_sequence_resolution()
                    # A transformed BGR layer needs transparent pixels outside
                    # its footprint when it is composited over an existing
                    # lower track. ``warpAffine`` can only preserve that
                    # coverage when the input carries an alpha channel;
                    # otherwise its black border becomes opaque foreground.
                    if (composite is not None
                            and layer_frame.ndim == 3
                            and layer_frame.shape[2] == 3):
                        alpha_value = (
                            np.iinfo(layer_frame.dtype).max
                            if np.issubdtype(layer_frame.dtype, np.integer)
                            else 1.0
                        )
                        alpha = np.full(
                            layer_frame.shape[:2] + (1,),
                            alpha_value,
                            dtype=layer_frame.dtype,
                        )
                        layer_frame = np.concatenate(
                            [layer_frame, alpha], axis=2)
                    layer_frame = apply_transform_cpu(
                        layer_frame,
                        transform,
                        width,
                        height,
                        coordinate_w,
                        coordinate_h,
                    )

            # Crop is also a viewport-space matte. Apply it again after clip
            # geometry so export matches preview when crop and transform are
            # both active. The source-space crop above the effects stage keeps
            # its existing filtering semantics.
            layer_frame = self._apply_crop_cpu(layer_frame, layer)

            # A single opaque layer with no transition is already the final
            # composite. Preserve its packed pixels instead of expanding a
            # 1080p frame to float32, multiplying by 1.0, then quantizing it
            # back to the same 8/16-bit values.
            if (not export_alpha
                    and len(sorted_layers) == 1
                    and not transitions
                    and abs(float(effective_opacity) - 1.0) <= 1e-6
                    and layer_frame.ndim == 3
                    and layer_frame.shape[2] == 3
                    and layer_frame.dtype in (np.uint8, np.uint16)
                    and layer_frame.shape[:2] == (height, width)):
                from src.features.edit.core.adjustment_composite import (
                    transform_is_identity,
                )

                if transform_is_identity(getattr(layer, "transform", None)):
                    return np.ascontiguousarray(layer_frame)

            # 转换为 float32 [0-1]，同时保留透明度。
            if layer_frame.ndim == 3 and layer_frame.shape[2] == 4:
                layer_float = self._normalise_bgr_frame(layer_frame[..., :3])
                # R2 修复：alpha 归一化按 dtype 选满刻度（此前硬编码 /255）
                layer_alpha = self._normalise_alpha_channel(layer_frame[..., 3:4])
            else:
                layer_float = self._normalise_bgr_frame(layer_frame)
                layer_alpha = 1.0
            src_a = np.clip(float(effective_opacity) * layer_alpha, 0.0, 1.0)

            # 合成
            if export_alpha:
                # R2（P0-1）：Porter-Duff over（自下而上，层永远盖在已累积
                # 内容之上）；内部预乘表示避免逐像素除法。层输入约定
                # straight alpha（RGBA 解码帧 / teaching animation）。
                if composite is None:
                    composite = np.zeros((height, width, 4), dtype=np.float32)
                one_minus = 1.0 - src_a
                composite[..., :3] = (
                    layer_float * src_a + composite[..., :3] * one_minus)
                composite[..., 3:4] = src_a + composite[..., 3:4] * one_minus
            elif composite is None:
                # 第一层
                composite = layer_float * src_a
            else:
                # A1: 非 normal 图层混合模式 → 共享 NumPy 公式（与预览
                # CPU/GPU、导出 GPU 三路同源）；normal 走既有 alpha over。
                _blend = getattr(getattr(layer, "clip", None), "blend_mode", None)
                _code = int(getattr(_blend, "code", 0) or 0)
                if _code != 0:
                    from src.features.edit.core.blend_modes import blend_rgb
                    # blend_rgb 的 HSL 系公式按 RGB 加权，而本路径的
                    # composite/layer_float 是 BGR——翻转后混合再翻回
                    # （与 blend_frames_u8 / GPU swizzle 同一约定）。
                    blended = blend_rgb(
                        composite[..., ::-1], layer_float[..., ::-1], _code,
                    )[..., ::-1]
                    composite = blended * src_a + composite * (1.0 - src_a)
                else:
                    # Alpha blend（不透明底 lerp——既有行为）
                    composite = composite * (1.0 - src_a) + layer_float * src_a

        # 处理 Dip To Color 转场
        for trans in transitions:
            if trans.transition_type == "dip_to_color" and composite is not None:
                color_factor = trans.get_color_factor()
                if color_factor > 0.0:
                    # 颜色混合
                    color_rgb = np.array(trans.blend_color, dtype=np.float32)
                    color_bgr = np.array([color_rgb[2], color_rgb[1], color_rgb[0]])
                    if export_alpha and composite.ndim == 3 and composite.shape[2] == 4:
                        # R2：对预乘缓冲的 dip = 以 color_factor 为 alpha 的
                        # over（颜色不透明地盖上去）
                        out_a = color_factor + composite[..., 3:4] * (1.0 - color_factor)
                        pm = color_bgr * color_factor + composite[..., :3] * (1.0 - color_factor)
                        composite = np.concatenate([pm, out_a], axis=-1)
                    else:
                        color_layer = np.ones((height, width, 3)) * color_bgr
                        composite = composite * (1.0 - color_factor) + color_layer * color_factor

        if composite is None:
            return self._create_black_frame(width, height)

        # 只在合成结束时量化。高位深交付保留 uint16 full-range RGB，
        # FFmpegVideoEncoder 会再转换到 yuv420/422p10le。
        if export_alpha and composite.ndim == 3 and composite.shape[2] == 4:
            # 预乘 → straight（编码器/字幕/遮版的工作约定）
            composite = self._unpremultiply_rgba(composite)
        return self._quantize_export_frame(composite)

    def _prepare_transition_side_frame(
        self,
        layer: "ClipLayerInfo",
        frame: "np.ndarray",
        trans: "TransitionBlendInfo",
        frame_idx: int,
        width: int,
        height: int,
    ) -> "np.ndarray":
        """AUD-18403：转场侧帧按主循环同序补齐完整处理链。

        此前双源预合成只做 denoise → grade → resize，frame-blend /
        crop / clip 效果链 / transform 在转场期间被静默丢弃（wipe 双侧
        clip 进 wipe_skip_ids 后主循环 ``continue``），且与 Metal 路径
        （side_properties 显式下发 effects/transform/crop）不一致。
        现与 _composite_layers 主循环同序：
        frame_blend → denoise → grade → 源空间 crop → clip 效果链 →
        resize → transform → 视口 crop → 转场源效果。
        """
        from src.features.edit.core.crop_cpu import apply_crop_cpu, crop_is_identity
        from src.features.edit.core.timeline_frame_mapper import (
            _apply_frame_blend_layer,
        )

        # A3.2: frame_blend 双源帧加权（主循环同序）
        frame = _apply_frame_blend_layer(
            layer, frame, frame_idx, self._get_layer_frame)
        # P1-COLOR-13: per-clip GPU 降噪（调色之前）
        frame = self._apply_video_denoise_export(frame, layer)
        # clip ColorStack primary grade
        frame = self._apply_color_grade(frame, layer)
        # FX-M06: 源空间 crop（grade 后、效果前）
        crop = tuple(getattr(layer, "crop", ()) or ())
        if not crop_is_identity(crop):
            frame = apply_crop_cpu(frame, crop)
        # Phase 4: clip 视频滤镜链（CPU numpy）
        frame = self._apply_clip_effects_cpu(frame, layer, frame_idx)
        frame = self._resize_frame(frame, width, height)
        # 几何 transform（Metal wipe 侧同样按不透明整帧处理，不加 alpha）
        transform = tuple(getattr(layer, "transform", ()) or ())
        if transform:
            from src.features.edit.core.adjustment_composite import (
                apply_transform_cpu,
                transform_is_identity,
            )
            if not transform_is_identity(transform):
                coordinate_w, coordinate_h = self._get_sequence_resolution()
                frame = apply_transform_cpu(
                    frame, transform, width, height,
                    coordinate_w, coordinate_h)
        # 视口空间 crop matte（transform 后，与主循环一致）
        frame = self._apply_crop_cpu(frame, layer)
        if trans.transition_type in TRANSITION_EFFECT_TYPES:
            frame = apply_transition_effect(
                frame,
                trans.blend_factor,
                trans.transition_type,
                trans.transition.params,
            )
        return frame

    # =================================================================
    # P0-GPU-1: 单层 GPU 直通合成（headless GL，失败回落 CPU）
    # =================================================================

    _GPU_CHROMA_DIV_BY_FORMAT: Dict[str, Tuple[int, int]] = {
        "yuv444p": (1, 1),
        "yuv444p10le": (1, 1),
        "yuv444p12le": (1, 1),
        "yuv422p": (2, 1),
        "yuv422p10le": (2, 1),
        "yuv422p12le": (2, 1),
        "yuv420p": (2, 2),
        "yuv420p10le": (2, 2),
    }

    def _release_gpu_export_renderer(self) -> None:
        """释放 GPU 合成渲染器（幂等；须在创建线程调用——串行=导出线程，
        流水线=render 生产者线程，两处 finally 均满足该契约）。"""
        renderer = self._gpu_export_renderer
        if renderer is None:
            return
        try:
            renderer.release()
        except Exception:
            pass
        self._gpu_export_renderer = None

    def _decoded_frame_is_high_depth(self, decoded_frame: Any) -> bool:
        """镜像 _get_video_frame 的高位深判定（格式名或 data itemsize）。"""
        pixel_format = str(getattr(decoded_frame, "pixel_format", "") or "")
        if self._pixel_format_is_high_depth(pixel_format):
            return True
        data = getattr(decoded_frame, "data", None)
        dtype = getattr(data, "dtype", None)
        return dtype is not None and getattr(dtype, "itemsize", 1) > 1

    def _gpu_yuv420_output_allowed(
        self, config: TimelineExportConfig, width: int, height: int,
        frame_idx: int = 0,
    ) -> bool:
        """Gate direct GPU RGB→YUV to a format/color contract we can match.

        The current shader implements 8-bit BT.709 limited-range 4:2:0. HDR,
        alternate matrices/ranges, odd dimensions, alpha, subtitle/matte
        overlays, and non-matching encoders retain the established path.

        2026-09-17：burn_subtitles 从硬拒绝改为与平面直通门一致的逐帧
        判据（惰性语义——默认 True 但仅当前帧确有活跃字幕时才需要
        回落 BGR；GPU YUV 直出与"无字幕帧"的 BGR 路径逐像素等价）。
        此前一刀切导致所有默认配置的 GPU 合成导出永远拿不到 YUV 直读，
        白付 BGR 回读 + 编码侧 CPU 色彩转换。
        """
        if (width <= 0 or height <= 0 or width % 2 or height % 2
                or bool(getattr(self, "_export_has_alpha", False))):
            return False
        if bool(getattr(config, "burn_subtitles", False)):
            try:
                if tuple(self._sequence.get_active_subtitle_clips(frame_idx)):
                    return False
            except Exception:
                return False
        encoder_config = getattr(getattr(self, "_encoder", None), "_config", None)
        codec_pair = (
            str(getattr(encoder_config, "codec", "")).lower(),
            str(getattr(encoder_config, "pix_fmt", "")).lower(),
        )
        if codec_pair not in {
            ("h264", "yuv420p"),
            ("hevc", "yuv420p"),
            ("hevc", "yuv420p10le"),
        }:
            return False
        color = getattr(self, "_export_color_metadata", None)
        if color is None or any(
            str(getattr(color, name, "")).lower() != expected
            for name, expected in (
                ("primaries", "bt709"), ("transfer", "bt709"),
                ("matrix", "bt709"), ("range", "limited"),
            )
        ):
            return False
        # The post-composite matte modifies pixels, so keep those exports on
        # the existing BGR route. A monitor-only program guide remains safe.
        settings = dict(getattr(self._sequence, "format_settings", {}) or {})
        guide = settings.get("program_guide")
        if isinstance(guide, dict) and guide.get("type") == "aspect_ratio":
            params = guide.get("params", {})
            if isinstance(params, dict) and params.get("fill_black") \
                    and params.get("burn_in"):
                return False
        return True

    def _gpu_yuv422p10_output_allowed(
        self, config: TimelineExportConfig, width: int, height: int,
        frame_idx: int = 0,
    ) -> bool:
        """Allow Metal to emit native Topos ``yuv422p10le`` planes.

        The finalizer matches the Topos encoder's BT.709 limited-range
        coefficients and 10-bit nominal code values. Unsupported metadata,
        alpha, subtitles, or odd dimensions keep the existing CPU-safe path.

        AUD-18407：burn_subtitles 从一刀切拒绝改为与 yuv420 门
        （2026-09-17）相同的逐帧惰性判据——默认 ``burn_subtitles=True``
        但当前帧无活跃字幕时仍走 GPU 直出；有活跃字幕或查询异常才
        fail-closed 回落 BGR。此前一刀切使默认配置下 TOPOS 10-bit 的
        Metal 原生 YUV 直出永不生效，每帧白付 BGR 回读 + 编码侧转换。
        """
        if (width <= 0 or height <= 0 or width % 2
                or bool(getattr(self, "_export_has_alpha", False))):
            return False
        if bool(getattr(config, "burn_subtitles", False)):
            try:
                if tuple(self._sequence.get_active_subtitle_clips(frame_idx)):
                    return False
            except Exception:
                return False
        encoder_config = getattr(getattr(self, "_encoder", None), "_config", None)
        if (str(getattr(encoder_config, "codec", "")).lower() != "topos"
                or str(getattr(encoder_config, "pix_fmt", "")).lower()
                != "yuv422p10le"):
            return False
        color = getattr(self, "_export_color_metadata", None)
        if color is None or any(
            str(getattr(color, name, "")).lower() != expected
            for name, expected in (
                ("primaries", "bt709"), ("transfer", "bt709"),
                ("matrix", "bt709"), ("range", "limited"),
            )
        ):
            return False
        return True

    def _adaptive_gpu_composite_skip_reason(
        self,
        sorted_layers: List["ClipLayerInfo"],
        transitions: Tuple["TransitionBlendInfo", ...],
        graphics_backend: str,
    ) -> Optional[str]:
        """主机可见源帧的 CPU/GPU 自适应选路（2026-09-16 提速计划）。

        真机基线（docs/windows_timeline_gpu_export_optimization_plan
        §11.1/§11.5）：主机可见帧走 OpenGL 上传→合成→读回的固定往返
        开销常超过轻量合成本身的 CPU 开销（合成路径 ~27fps vs 直通
        139fps，差距主要是逐帧 host↔GPU 拷贝而非 GPU 算力）。因此仅当
        帧内确有值得 GPU 承担的逐像素工作（多层/转场/调色/特效/降噪/
        几何变换/混合模式/帧混合变速），或源已是 native GPU surface
        （Metal，无上传读回）时才启用 GPU 合成；"直通形状但源契约不满
        足"的单层帧（如 NV12 源、fps 不匹配）直接走 CPU 合成。
        显式 TOPOS_EXPORT_GPU_RENDER=1 视为用户强制 GPU，本策略让位。

        Returns:
            应跳过 GPU 合成时返回原因字符串（记录进 GPU trail），
            应尝试 GPU 时返回 None。
        """
        if getattr(self, "_gpu_render_forced", False):
            return None
        if graphics_backend != "opengl":
            # Metal 源帧 native GPU 驻留（无上传/读回开销）；d3d11 在
            # 调用方已 fail-closed 回 CPU，不会携带该后端到达这里。
            return None
        if not sorted_layers or len(sorted_layers) > 1 or transitions:
            return None
        layer = sorted_layers[0]
        clip = getattr(layer, "clip", None)
        if clip is None:
            return None
        try:
            from src.features.edit.core.adjustment_composite import (
                transform_is_identity,
            )
            from src.features.edit.core.crop_cpu import crop_is_identity

            blend_mode = getattr(clip, "blend_mode", "normal")
            blend_name = str(getattr(blend_mode, "value", blend_mode))
            speed = getattr(clip, "speed", 1.0)
            frame_blend_retime = (
                str(getattr(speed, "interpolation", "nearest")) == "frame_blend"
                and getattr(clip, "freeze_source_frame", None) is None)
            has_pixel_work = bool(
                tuple(getattr(clip, "effects", ()) or ())
                or blend_name != "normal"
                or frame_blend_retime
                or not crop_is_identity(
                    tuple(getattr(layer, "crop", ()) or ()))
                or not transform_is_identity(
                    tuple(getattr(layer, "transform", ()) or ()))
                or self._get_clip_color_grade(layer) is not None
                or self._get_clip_video_denoise(layer) is not None)
        except Exception:  # noqa: BLE001 — 判定异常按"可能有重活"保留 GPU
            return None
        if has_pixel_work:
            return None
        return "adaptive_policy:cpu_preferred_single_layer_trivial_composite"

    @staticmethod
    def _gpu_render_output_kwargs(
        graphics_backend: str,
        *,
        output_u16: bool,
        output_yuv420p: bool,
        output_yuv422p10: bool,
        include_dip: bool = False,
        dip_color: Optional[Tuple[float, float, float]] = None,
        dip_factor: float = 0.0,
    ) -> Dict[str, Any]:
        """GPU 渲染入口 kwargs（T-P1-21 门控单点）。

        ``output_yuv422p10`` 是 Metal 渲染器独有的原生平面直出参数——
        OpenGL 渲染器（render_planar/render_layer_stack/render_packed_bgr）
        签名没有它，GL 走 BGR u8/u16 回读 + encoder sws 转换。此前不分
        后端一律传入，macOS auto→opengl 下 TOPOS 10-bit 导出每帧
        TypeError→整帧回落 CPU float 管线（grade 实测 1.4fps，且
        forced_gpu 与 cpu_render 同速——GPU 链从未生效）。
        """
        kwargs: Dict[str, Any] = {"output_u16": output_u16}
        if include_dip:
            kwargs["dip_color"] = dip_color
            kwargs["dip_factor"] = dip_factor
        if output_yuv420p:
            kwargs["output_yuv420p"] = True
        if output_yuv422p10 and graphics_backend == "metal":
            kwargs["output_yuv422p10"] = True
        return kwargs

    def _acquire_gpu_encode_surface(
        self, *, pixel_format: str, frame_idx: int,
        width: int, height: int,
        color_range: str = "limited", color_matrix: str = "bt709",
        duration_seconds: Optional[Any] = None,
    ) -> Optional[Any]:
        """Acquire one NV12/P010 encode surface with bounded starvation retry.

        T-P0-31：VT 完成回调持续归还槽位；ring 临时满员时短暂重试而非
        立即回落 CPU 帧格式——bridge 激活时下游会拒收 CPU 帧并终止整次
        导出。重试窗口 0.5s 后仍失败则返回 None（调用方走既有回退记录）。
        """
        provider = getattr(self, "_gpu_encode_surface_provider", None)
        if provider is None:
            return None
        deadline = time.monotonic() + 0.5
        last_error: Optional[Exception] = None
        while True:
            try:
                return provider.acquire(
                    width=int(width),
                    height=int(height),
                    pixel_format=pixel_format,
                    frame_index=int(frame_idx),
                    pts=int(frame_idx),
                    duration_seconds=duration_seconds,
                    color_range=color_range,
                    color_matrix=color_matrix,
                )
            except Exception as error:  # noqa: BLE001 — 满员/瞬时导入失败重试
                last_error = error
            if time.monotonic() >= deadline:
                self._record_gpu_fallback(
                    f"gpu_encode_surface_acquire_failed:{last_error}")
                return None
            time.sleep(0.005)

    def _gpu_render_composite(
        self,
        sorted_layers: List["ClipLayerInfo"],
        transitions: Tuple["TransitionBlendInfo", ...],
        width: int,
        height: int,
        output_yuv420p: bool = False,
        output_yuv422p10: bool = False,
        frame_idx: int = 0,
    ) -> Optional[Any]:
        """Render supported timeline layers on the selected GPU or return None.

        Metal covers packed/planar video and image layers with primary grade,
        scale, opacity, normal blending, cross-dissolve, dip-to-color and
        every catalogued two-source transition. Procedural masks and
        transition-specific source effects stay GPU-resident until the final
        readback. It reads back only the final BGR frame,
        native YUV422P10 planes for high-depth Topos delivery, or supported
        H.264 YUV420P planes.
        Bounded Blur (Box/Gaussian/Bilateral), Sharpen, Glow, Erode/Dilate,
        Sobel Edge Detect, Denoise and either Defocus mode may run as an
        ordered Metal chain using GPU-resident BGR intermediates. Clip crop
        mattes and affine transforms are also evaluated in the Metal stack
        compositor, including on the two sides of spatial transitions.
        Other clip effect types/parameters, clip-level temporal denoise,
        frame-blend retiming and alpha retain the CPU compositor. Native
        Topos 10-bit exports can read back YUV422P10 planes directly; the CPU
        then only forwards those planes to the Topos encoder. OpenGL keeps its
        broader existing stack path.
        """
        from src.shared.tracking.stabilization_state import stabilization_matrix
        if any(stabilization_matrix(self._clip_stabilization_state(layer),
                                    int(getattr(layer, 'source_frame', 0))) is not None
               for layer in sorted_layers):
            self._record_gpu_fallback('video_stabilization_source_space')
            return None
        import numpy as np

        if self._gpu_render_disabled:
            return None
        graphics_backend = getattr(self, "_timeline_graphics_backend", "opengl")
        if graphics_backend == "d3d11":
            # The D3D11 selector is intentionally fail-closed until the
            # native decoder -> render-plan -> encoder bridge is connected to
            # this exporter.  Never route a selected D3D11 job through the
            # OpenGL compositor and report it as a D3D11 result.
            reason = "not_yet_implemented:d3d11_timeline_composite_integration"
            self._timeline_graphics_backend_reason = reason
            logger.warning("D3D11 composite path unavailable; CPU fallback: %s", reason)
            return None
        adaptive_skip = self._adaptive_gpu_composite_skip_reason(
            sorted_layers, transitions, graphics_backend)
        if adaptive_skip is not None:
            # 主机可见单层轻量帧：GPU 上传/读回往返不划算，直接 CPU 合成
            # （原因进 GPU trail；直通门在此前已尝试并放行给了合成路径）。
            self._record_gpu_fallback(adaptive_skip)
            logger.debug(
                "GPU composite skipped by adaptive policy; CPU compositor: %s",
                adaptive_skip)
            return None
        if getattr(self, "_export_has_alpha", False):
            if graphics_backend == "metal":
                self._warn_metal_fallback("alpha_delivery")
            return None
        if not sorted_layers:
            return None
        metal_gpu_frames: Dict[int, Any] = {}
        renderer = getattr(self, "_gpu_export_renderer", None)
        if renderer is not None and bool(getattr(renderer, "failed", False)):
            # A failed headless context is permanent for this export. Avoid
            # fetching/copying every source frame for a GPU attempt that will
            # immediately fall back to the CPU compositor.
            return None
        if any(layer.clip_id in self._gpu_ineligible_clip_ids
               for layer in sorted_layers):
            return None

        try:
            from src.features.edit.core.crop_cpu import crop_is_identity
            from src.features.edit.core.transition_mask_gpu import (
                build_gpu_transition_mask_spec,
            )
            from src.features.edit.core.custom_transitions import (
                is_user_transition_spatial,
            )
            from src.features.edit.core.timeline_gpu_export_renderer import (
                GpuWipeElement, GpuYuv420Output, GpuYuv422P10Output,
            )

            if self._project is None:
                return None

            if graphics_backend != "metal":
                # OpenGL 渲染器不实现 clip 几何（blend pass 逐像素 1:1，
                # 元组里的 transform/crop 槽位仅 Metal 消费）。此前非恒等
                # transform/crop 在 OpenGL 下被静默丢弃（实测 2x scale 的
                # GPU 输出与 CPU 合成 MAD 13.7/255），fail-closed 回落
                # CPU 合成（2026-09-17 复查修复）。
                from src.features.edit.core.adjustment_composite import (
                    transform_is_identity,
                )
                for layer in sorted_layers:
                    # 空 transform（测试替身/无属性）= 无几何可丢，视为
                    # 恒等；transform_is_identity 对短元组返回 False。
                    layer_transform = tuple(
                        getattr(layer, "transform", ()) or ())
                    if layer_transform and not transform_is_identity(
                            layer_transform):
                        return None
                    if not crop_is_identity(
                            tuple(getattr(layer, "crop", ()) or ())):
                        return None

            def _is_gpu_two_source_transition(transition: Any) -> bool:
                transition_type = str(
                    getattr(transition, "transition_type", ""))
                return (
                    transition_type in SPATIAL_TRANSITION_TYPES
                    or transition_type in FULL_FRAME_TRANSITION_TYPES
                    or transition_type in TRANSITION_EFFECT_TYPES
                    or is_user_transition_spatial(transition_type) is True
                )

            safe_single_source_transitions = {
                "cross_dissolve", "dip_to_color",
            }
            if any(
                str(getattr(trans, "transition_type", ""))
                not in safe_single_source_transitions
                and not _is_gpu_two_source_transition(trans)
                for trans in transitions
            ):
                if graphics_backend == "metal":
                    self._warn_metal_fallback("unknown_transition")
                else:
                    logger.info(
                        "GPU timeline export does not cover unknown transition; "
                        "using the CPU compositor",
                    )
                return None

            # 转场分区（镜像 _composite_layers 的 wipe 预合成映射）
            transition_map: Dict[str, "TransitionBlendInfo"] = {}
            spatial_skip: set = set()
            spatial_inject: Dict[str, "TransitionBlendInfo"] = {}
            layer_ids = {layer.clip_id for layer in sorted_layers}
            for trans in transitions:
                if trans.left_clip_id:
                    transition_map[trans.left_clip_id] = trans
                if trans.right_clip_id:
                    transition_map[trans.right_clip_id] = trans
                if not _is_gpu_two_source_transition(trans):
                    continue
                if trans.left_clip_id:
                    spatial_skip.add(trans.left_clip_id)
                if trans.right_clip_id:
                    spatial_skip.add(trans.right_clip_id)
                # A one-sided transition is still a valid GPU two-source
                # pass: the missing side is rendered as black by the stack
                # renderer. Inject into whichever side actually exists.
                inject_id = next(
                    (clip_id for clip_id in (
                        trans.left_clip_id, trans.right_clip_id)
                     if clip_id in layer_ids),
                    None,
                )
                if inject_id:
                    spatial_inject[inject_id] = trans

            resolved_source_cache: Dict[Tuple[Any, ...], Any] = {}
            resolved_layer_effects: Dict[
                str, Optional[Tuple[Dict[str, float], ...]]
            ] = {}

            def _resolve_layer_source(layer, apply_effective_opacity):
                """解析单层源与 opacity；不适用返回 None。"""
                clip = getattr(layer, "clip", None)
                if clip is None:
                    return None
                if apply_effective_opacity:
                    effects = getattr(clip, "effects", ()) or ()
                    active_effects = [
                        effect for effect in effects
                        if getattr(effect, "is_enabled", True)
                    ]
                    if active_effects:
                        if graphics_backend == "metal":
                            supported, metal_effects = (
                                self._gpu_metal_effect_chain_uniforms(
                                    layer, frame_idx)
                            )
                            if not supported:
                                self._warn_metal_fallback(
                                    "unsupported_clip_effect_chain")
                                return None
                            resolved_layer_effects[layer.clip_id] = metal_effects
                        else:
                            # 2026-09-17 导出提速：OpenGL 后端放行经
                            # fail-closed 校验的效果链（首版 blur），
                            # 由导出渲染器 pass2.5（EffectPassRunner，
                            # 预览同款 GLSL）执行；不满足 → 整帧回落 CPU。
                            supported, effect_chain = (
                                self._gpu_opengl_effect_chain(
                                    layer, frame_idx)
                            )
                            if not supported:
                                return None
                            resolved_layer_effects[layer.clip_id] = effect_chain
                    crop = tuple(getattr(layer, "crop", ()) or ())
                    if (graphics_backend == "metal"
                            and not crop_is_identity(crop)
                            and (len(crop) < 5 or not all(
                                math.isfinite(float(value))
                                for value in crop[:5]))):
                        self._warn_metal_fallback("invalid_crop_matte")
                        return None
                    if graphics_backend == "metal":
                        if (getattr(clip, "composite_id", None)
                                and not bool(getattr(
                                    clip, "composite_bypassed", False))):
                            self._warn_metal_fallback("composite_clip")
                            return None
                    speed = getattr(clip, "speed", None)
                    if (getattr(speed, "interpolation", "") == "frame_blend"
                            and getattr(clip, "freeze_source_frame", None) is None):
                        try:
                            frame_a, frame_b, weight = (
                                clip.timeline_to_source_frame_pair(int(frame_idx)))
                        except Exception:
                            frame_a, frame_b, weight = 0, 1, 1.0
                        if float(weight) > 0.0 and frame_b != frame_a:
                            if graphics_backend == "metal":
                                self._warn_metal_fallback("frame_blend_retime")
                            else:
                                logger.info(
                                    "GPU timeline export does not cover frame-blend retiming; "
                                    "using the CPU compositor",
                                )
                            return None
                if self._get_clip_video_denoise(layer) is not None:
                    if graphics_backend == "metal":
                        self._warn_metal_fallback("video_denoise")
                    return None
                asset = self._project.get_asset_by_id(layer.asset_id)
                if asset is None:
                    return None
                media_type = str(getattr(asset, "media_type", "") or "")
                if media_type not in ("video", "image"):
                    return None
                effective_path = getattr(asset, "effective_path", None)
                asset_path = (
                    effective_path
                    if isinstance(effective_path, str) and effective_path
                    else str(getattr(asset, "path", "") or "")
                )
                if not asset_path:
                    return None
                source_key = (
                    media_type, str(getattr(layer, "asset_id", "")),
                    asset_path, int(layer.source_frame),
                    bool(getattr(self, "_use_high_precision_export", False)),
                )
                if graphics_backend == "metal" and source_key in resolved_source_cache:
                    return resolved_source_cache[source_key]
                if media_type == "video":
                    layer_session_key = self._layer_session_key(layer)
                    if graphics_backend == "metal":
                        decoded = self._request_video_metal_frame(
                            asset_path, int(layer.source_frame), layer.asset_id,
                            session_key=layer_session_key)
                    else:
                        decoded = self._request_video_decoded_frame(
                            asset_path, int(layer.source_frame), layer.asset_id,
                            session_key=layer_session_key)
                    if decoded is None:
                        return None
                    from src.shared.media.gpu_frame import GpuDecodedFrame
                    if isinstance(decoded, GpuDecodedFrame):
                        metal_gpu_frames[id(decoded)] = decoded
                        resolved_source_cache[source_key] = decoded
                        return decoded
                    source = self._gpu_planar_source_from_decoded(decoded)
                    if source is None and \
                            getattr(self, "_use_high_precision_export",
                                    False) \
                            and self._decoded_frame_is_high_depth(decoded):
                        # Keep the optional session-key extension compatible
                        # with lightweight provider/test doubles that still
                        # expose the historical three-argument callable.
                        request_high_depth = (
                            self._request_high_depth_decoded_frame)
                        session_key = self._layer_session_key(layer)
                        try:
                            decoded_hd = request_high_depth(
                                asset_path, int(layer.source_frame),
                                layer.asset_id, session_key=session_key)
                        except TypeError as error:
                            if "session_key" not in str(error):
                                raise
                            decoded_hd = request_high_depth(
                                asset_path, int(layer.source_frame),
                                layer.asset_id)
                        if decoded_hd is not None:
                            source = self._gpu_planar_source_from_decoded(
                                decoded_hd)
                    if source is not None:
                        if graphics_backend == "metal":
                            resolved_source_cache[source_key] = source
                        return source
                    packed = getattr(decoded, "data", None)
                    packed_format = str(
                        getattr(decoded, "pixel_format", "") or ""
                    ).strip().lower()
                    if packed_format in ("bgr24", "rgb24") \
                            and packed is not None and packed.ndim == 3 \
                            and packed.shape[2] == 3 \
                            and packed.dtype == np.uint8:
                        # VideoFrameProviderService's normal FFmpeg master
                        # decoder emits packed ``rgb24`` frames. The GPU
                        # renderer's packed path expects BGR because its
                        # texture swizzle restores RGB before grading. Accept
                        # both valid layouts and adapt RGB once here instead
                        # of silently routing every ordinary H.264 clip to
                        # the much slower CPU grade/composite path.
                        if packed_format == "rgb24":
                            packed = np.ascontiguousarray(packed[..., ::-1])
                        if graphics_backend == "metal":
                            resolved_source_cache[source_key] = packed
                        return packed
                    self._gpu_ineligible_clip_ids.add(layer.clip_id)
                    return None
                frame = self._get_image_frame(asset_path)
                if frame is None or frame.ndim != 3 \
                        or frame.shape[2] != 3 or frame.dtype != np.uint8:
                    return None
                if graphics_backend == "metal":
                    resolved_source_cache[source_key] = frame
                return frame

            layer_elements: list = []
            for layer in sorted_layers:
                if layer.clip_id in spatial_skip:
                    if layer.clip_id not in spatial_inject:
                        continue
                    trans = spatial_inject[layer.clip_id]
                    left_layer = next(
                        (l for l in sorted_layers
                         if l.clip_id == trans.left_clip_id), None)
                    right_layer = next(
                        (l for l in sorted_layers
                         if l.clip_id == trans.right_clip_id), None)
                    if graphics_backend != "metal":
                        # wipe 双源 pass 的 side effects 槽位仅 Metal 填充
                        # （side_properties 按 backend 选择），OpenGL 侧层
                        # 的 clip 效果没有 GPU 通道——静默丢弃即输出错误，
                        # fail-closed 整帧回落 CPU（侧层解析在
                        # apply_effective_opacity=False 下不会自行拒绝）。
                        for side_layer in (left_layer, right_layer):
                            if side_layer is None:
                                continue
                            side_effects = getattr(
                                getattr(side_layer, "clip", None),
                                "effects", ()) or ()
                            if any(getattr(effect, "is_enabled", True)
                                   for effect in side_effects):
                                return None
                    sides = []
                    for side_layer in (left_layer, right_layer):
                        if side_layer is None:
                            sides.append(None)
                            continue
                        # Spatial sides use the same Metal-capable effect,
                        # crop, transform, composite and retime validation as
                        # regular layers. Their opacity is carried separately
                        # by TransitionBlendInfo and applied in the wipe pass.
                        source = _resolve_layer_source(
                            side_layer, graphics_backend == "metal")
                        if source is None:
                            return None
                        sides.append(source)
                    # All supported two-source transitions use the same
                    # procedural GPU payload. The old ndarray mask remains
                    # only as a compatibility field for callers that do not
                    # have a GPU transition spec.
                    mask = None
                    mask_spec = None
                    try:
                        mask_spec = build_gpu_transition_mask_spec(
                            trans.transition_type,
                            float(trans.blend_factor),
                            getattr(trans.transition, "params", None),
                        )
                    except Exception:
                        return None
                    if mask_spec is None:
                        self._warn_metal_fallback("unsupported_transition_mask")
                        return None
                    if graphics_backend == "metal" and str(
                            trans.transition_type) in LIB1_TRANSITION_TYPES:
                        # LIB1（L03）：MSL kernel 未实现 kind=5/resample——
                        # 显式回退 CPU 合成，禁静默错渲染（也不替换为
                        # Cross Dissolve）。
                        self._warn_metal_fallback("lib1_transition_metal_unimplemented")
                        return None
                    side_properties: list[dict[str, Any]] = []
                    for side_layer in (left_layer, right_layer):
                        if side_layer is None:
                            side_properties.append({})
                            continue
                        side_transform = tuple(
                            getattr(side_layer, "transform", ()) or ())
                        side_crop = tuple(
                            getattr(side_layer, "crop", ()) or ())
                        side_properties.append({
                            "effects": resolved_layer_effects.get(
                                side_layer.clip_id)
                                if graphics_backend == "metal" else None,
                            "transform": side_transform or None
                                if graphics_backend == "metal" else None,
                            "transform_dims": self._get_sequence_resolution()
                                if graphics_backend == "metal" else None,
                            "crop": side_crop or None
                                if graphics_backend == "metal" else None,
                        })
                    layer_elements.append(GpuWipeElement(
                        left=sides[0],
                        left_grade=(self._gpu_grade_uniforms(left_layer)
                                    if left_layer is not None else None),
                        right=sides[1],
                        right_grade=(self._gpu_grade_uniforms(right_layer)
                                     if right_layer is not None else None),
                        mask=(np.ascontiguousarray(mask, dtype=np.float32)
                              if mask is not None else None),
                        left_opacity=float(trans.left_opacity),
                        right_opacity=float(trans.right_opacity),
                        opacity=float(getattr(layer, "opacity", 1.0)),
                        left_effects=side_properties[0].get("effects"),
                        right_effects=side_properties[1].get("effects"),
                        left_transform=side_properties[0].get("transform"),
                        right_transform=side_properties[1].get("transform"),
                        left_transform_dims=side_properties[0].get(
                            "transform_dims"),
                        right_transform_dims=side_properties[1].get(
                            "transform_dims"),
                        left_crop=side_properties[0].get("crop"),
                        right_crop=side_properties[1].get("crop"),
                        mask_spec=mask_spec,
                        transition_type=str(trans.transition_type),
                    ))
                    continue

                source = _resolve_layer_source(layer, True)
                if source is None:
                    return None
                effective_opacity = float(getattr(layer, "opacity", 1.0))
                if layer.clip_id in transition_map:
                    trans_info = transition_map[layer.clip_id]
                    if layer.clip_id == trans_info.left_clip_id:
                        effective_opacity = trans_info.left_opacity
                    elif layer.clip_id == trans_info.right_clip_id:
                        effective_opacity = trans_info.right_opacity
                blend_mode = getattr(layer, "get_blend_mode", None)
                blend_code = 0
                clip_blend = getattr(getattr(layer, "clip", None), "blend_mode", None)
                if clip_blend is not None:
                    blend_code = int(getattr(clip_blend, "code", 0))
                _ = blend_mode  # 转场语义 BlendMode 不在此使用
                transform = tuple(getattr(layer, "transform", ()) or ())
                if graphics_backend == "metal":
                    coordinate_width, coordinate_height = (
                        self._get_sequence_resolution())
                    crop = tuple(getattr(layer, "crop", ()) or ())
                else:
                    coordinate_width, coordinate_height = width, height
                    crop = ()
                layer_elements.append((
                    source, self._gpu_grade_uniforms(layer),
                    max(0.0, min(1.0, effective_opacity)), blend_code,
                    resolved_layer_effects.get(layer.clip_id), transform,
                    (coordinate_width, coordinate_height), crop))

            # dip_to_color：末端颜色 over（多个激活 dip 保守回落 CPU）
            dip_color = None
            dip_factor = 0.0
            for trans in transitions:
                if trans.transition_type != "dip_to_color":
                    continue
                try:
                    factor = float(trans.get_color_factor())
                except Exception:
                    return None
                if factor <= 0.0:
                    continue
                if dip_color is not None:
                    return None
                dip_color = tuple(
                    float(c) for c in trans.blend_color)
                dip_factor = max(0.0, min(1.0, factor))

            renderer = self._gpu_export_renderer
            if renderer is None:
                if graphics_backend == "metal":
                    from src.features.edit.core.timeline_metal_export_renderer import (
                        TimelineMetalExportRenderer,
                    )
                    trail = getattr(self, "_gpu_trail", None)
                    renderer = (
                        TimelineMetalExportRenderer(trail)
                        if trail is not None
                        else TimelineMetalExportRenderer()
                    )
                else:
                    from src.features.edit.core.timeline_gpu_export_renderer import (
                        TimelineGpuExportRenderer,
                    )
                    renderer = TimelineGpuExportRenderer()
                self._gpu_export_renderer = renderer
            if renderer.failed:
                return None

            # P010 is a native 10-bit YUV target written directly into the
            # VideoToolbox surface; it must not also request the packed uint16
            # BGR output mode, which is a mutually exclusive CPU-delivery
            # format. Topos yuv422p10 keeps the high-precision flag.
            output_u16 = bool(
                getattr(self, "_use_high_precision_export", False)
                and not output_yuv420p
            )
            single_layer = (
                layer_elements[0]
                if len(layer_elements) == 1
                and isinstance(layer_elements[0], tuple)
                else None
            )
            if single_layer is not None:
                source, grade_uniforms, opacity, blend_code = single_layer[:4]
                effect_params = (
                    single_layer[4] if len(single_layer) > 4 else None)
            else:
                blend_code = -1
                effect_params = None
            has_active_dip = dip_color is not None and dip_factor > 0.0
            single_geometry_active = False
            if single_layer is not None and graphics_backend == "metal":
                from src.features.edit.core.adjustment_composite import (
                    transform_is_identity,
                )
                from src.features.edit.core.crop_cpu import crop_is_identity
                single_transform = tuple(single_layer[5] or ())
                single_geometry_active = (
                    bool(single_transform)
                    and not transform_is_identity(single_transform)
                    or not crop_is_identity(single_layer[7]))
            # CPU composition does not apply a clip blend mode to the first
            # layer: there is no accumulated image to blend against yet. Keep
            # that exact semantic for a one-layer GPU export, including clips
            # whose stored blend mode is non-normal.
            if (single_layer is not None and not has_active_dip
                    and effect_params is None and not single_geometry_active):
                if isinstance(source, np.ndarray):
                    render = renderer.render_packed_bgr
                elif graphics_backend == "metal" and id(source) in metal_gpu_frames:
                    render = renderer.render_metal_texture_frame
                else:
                    render = renderer.render_planar
                kwargs = self._gpu_render_output_kwargs(
                    graphics_backend,
                    output_u16=output_u16,
                    output_yuv420p=output_yuv420p,
                    output_yuv422p10=output_yuv422p10,
                )
                output_surface = None
                if (
                    output_yuv420p
                    and graphics_backend == "metal"
                    and render.__name__ == "render_metal_texture_frame"
                    and getattr(self, "_gpu_encode_surface_provider", None) is not None
                ):
                    source_frame = source
                    output_surface = self._acquire_gpu_encode_surface(
                        pixel_format=(
                            "p010"
                            if str(getattr(
                                getattr(
                                    getattr(self, "_encoder", None),
                                    "_config", None,
                                ),
                                "codec", "",
                            )).lower() == "hevc"
                            else "nv12"
                        ),
                        frame_idx=int(frame_idx),
                        width=width,
                        height=height,
                        color_range=str(
                            getattr(source_frame, "color_range", "limited")),
                        color_matrix=str(
                            getattr(source_frame, "color_matrix", "bt709")),
                        duration_seconds=getattr(
                            source_frame, "duration_seconds", None),
                    )
                    if output_surface is not None:
                        kwargs["output_surface"] = output_surface
                result = render(
                    source, grade_uniforms, width, height, opacity, **kwargs)
                if output_surface is not None and result is not output_surface:
                    output_surface.release()
            else:
                # The stack shader always evaluates its blend operation
                # against the cleared black accumulator. For a lone source
                # layer that would differ from CPU's first-layer behavior;
                # force normal blending before applying the terminal dip pass.
                render_layers = layer_elements
                if single_layer is not None:
                    render_layers = [(
                        source, grade_uniforms, opacity, 0,
                        *single_layer[4:])]
                kwargs = self._gpu_render_output_kwargs(
                    graphics_backend,
                    output_u16=output_u16,
                    output_yuv420p=output_yuv420p,
                    output_yuv422p10=output_yuv422p10,
                    include_dip=True,
                    dip_color=dip_color,
                    dip_factor=dip_factor,
                )
                stack_output_surface = None
                if (
                    output_yuv420p
                    and graphics_backend == "metal"
                    and getattr(self, "_gpu_encode_surface_provider", None) is not None
                ):
                    try:
                        stack_pixel_format = (
                            "p010"
                            if str(getattr(
                                getattr(
                                    getattr(self, "_encoder", None),
                                    "_config", None,
                                ),
                                "codec", "",
                            )).lower() == "hevc"
                            else "nv12"
                        )
                        stack_output_surface = (
                            self._acquire_gpu_encode_surface(
                                pixel_format=stack_pixel_format,
                                frame_idx=int(frame_idx),
                                width=int(width),
                                height=int(height),
                            )
                        )
                        if stack_output_surface is not None:
                            kwargs["output_surface"] = stack_output_surface
                    except Exception as error:
                        self._record_gpu_fallback(
                            f"gpu_encode_surface_acquire_failed:{error}")
                        stack_output_surface = None
                result = renderer.render_layer_stack(
                    render_layers, width, height, **kwargs)
                if stack_output_surface is not None and result is not stack_output_surface:
                    stack_output_surface.release()
            if isinstance(result, GpuYuv420Output):
                from src.shared.media.frame import DecodedFrame

                fps = self._get_sequence_fps()
                return DecodedFrame(
                    frame_index=int(frame_idx),
                    pts=int(frame_idx),
                    time_seconds=(float(frame_idx) / fps if fps > 0 else 0.0),
                    planes=result.planes,
                    pixel_format="yuv420p",
                    width=width,
                    height=height,
                )
            if isinstance(result, GpuYuv422P10Output):
                from src.shared.media.frame import DecodedFrame

                fps = self._get_sequence_fps()
                return DecodedFrame(
                    frame_index=int(frame_idx),
                    pts=int(frame_idx),
                    time_seconds=(float(frame_idx) / fps if fps > 0 else 0.0),
                    planes=result.planes,
                    pixel_format="yuv422p10le",
                    width=width,
                    height=height,
                )
            renderer_errors = getattr(
                renderer, "metal_errors", getattr(renderer, "gl_errors", 0))
            if result is None and renderer_errors:
                # GL 上下文可能已失效（同线程其它 context 被释放的罕见
                # 时序——moderngl 不做按调用 make-current）——丢弃重建自愈；
                # 每导出上限 2 次防抖动，失败帧本帧回落 CPU
                if self._gpu_renderer_recreates < 2:
                    self._gpu_renderer_recreates += 1
                    try:
                        renderer.release()
                    except Exception:
                        pass
                    self._gpu_export_renderer = None
            return result
        except Exception as error:
            logger.warning(
                f"GPU export composite unavailable ({error}); CPU fallback")
            return None
        finally:
            if metal_gpu_frames:
                from src.shared.media.gpu_frame import safe_release_gpu_frame
                for frame in metal_gpu_frames.values():
                    try:
                        safe_release_gpu_frame(frame)
                    except Exception:
                        logger.debug(
                            "Could not release Metal export source frame",
                            exc_info=True,
                        )

    def _select_timeline_graphics_backend(self) -> str:
        """Resolve the persisted timeline graphics choice for this export."""
        requested = "auto"
        try:
            from src.shared.config.config_manager import ConfigManager
            requested = ConfigManager().get_backend_selection().graphics.value
        except Exception as error:  # corrupted/absent settings keep safe default
            logger.warning("Could not read graphics backend preference: %s", error)
        env_choice = str(os.getenv("TOPOS_BACKEND_GRAPHICS", "") or "").strip().lower()
        if env_choice:
            requested = env_choice.removeprefix("force_")
        if requested == "opengl":
            return "opengl"
        if requested == "metal":
            return "metal"
        if requested == "d3d11":
            try:
                from src.shared.media.windows_hw.d3d11_export_capability import (
                    probe_d3d11_export_capability,
                )
                capability = probe_d3d11_export_capability()
                if capability.ready:
                    return "d3d11"
                self._timeline_graphics_backend_reason = capability.reason
                logger.warning(
                    "D3D11 export requested but unavailable; falling back to OpenGL: %s",
                    capability.reason,
                )
            except (ImportError, OSError, RuntimeError, TypeError, ValueError) as error:
                self._timeline_graphics_backend_reason = f"d3d11_probe_failed:{error}"
                logger.warning(
                    "D3D11 export probe failed; falling back to OpenGL: %s", error
                )
            return "opengl"
        # Auto enables D3D11 only after the complete native gate passes.  The
        # normal development/compatibility route remains OpenGL when the
        # native bridge or real T0 evidence is absent.
        try:
            from src.shared.media.windows_hw.d3d11_export_capability import (
                probe_d3d11_export_capability,
            )
            capability = probe_d3d11_export_capability()
            if capability.ready:
                return "d3d11"
            self._timeline_graphics_backend_reason = capability.reason
        except (ImportError, OSError, RuntimeError, TypeError, ValueError) as error:
            self._timeline_graphics_backend_reason = f"d3d11_probe_failed:{error}"
        return "opengl"

    def _warn_metal_fallback(self, reason: str) -> None:
        """Log each known GPU coverage fallback once per export."""
        if reason in self._metal_fallback_warnings:
            return
        self._metal_fallback_warnings.add(reason)
        logger.info(
            "Timeline GPU path does not cover %s; using the CPU compositor",
            reason,
        )

    def _gpu_metal_effect_uniforms(
        self,
        layer: "ClipLayerInfo",
        frame_idx: int,
    ) -> Tuple[bool, Optional[Dict[str, float]]]:
        """Resolve one clip effect using the native-Metal parameter subset."""
        clip = getattr(layer, "clip", None)
        effects = tuple(getattr(clip, "effects", ()) or ())
        active = [e for e in effects if getattr(e, "is_enabled", True)]
        if not active:
            return True, None
        if len(active) != 1:
            return False, None
        return self._gpu_metal_effect_instance_uniforms(active[0], frame_idx)

    def _gpu_metal_effect_chain_uniforms(
        self,
        layer: "ClipLayerInfo",
        frame_idx: int,
    ) -> Tuple[bool, Optional[Tuple[Dict[str, float], ...]]]:
        """Resolve an ordered chain, declining the whole clip on any gap."""
        clip = getattr(layer, "clip", None)
        effects = tuple(getattr(clip, "effects", ()) or ())
        active = sorted(
            (effect for effect in effects
             if getattr(effect, "is_enabled", True)),
            key=lambda effect: getattr(effect, "order", 0),
        )
        if not active:
            return True, None
        resolved: list[Dict[str, float]] = []
        for effect in active:
            supported, uniforms = self._gpu_metal_effect_instance_uniforms(
                effect, frame_idx)
            if not supported:
                return False, None
            if uniforms is not None:
                resolved.append(uniforms)
        return True, tuple(resolved) or None

    def _gpu_opengl_effect_chain(
        self,
        layer: "ClipLayerInfo",
        frame_idx: int,
    ) -> Tuple[bool, Optional[Tuple[Tuple[Any, Dict[str, Any]], ...]]]:
        """解析 OpenGL 导出 GPU 效果链（fail-closed；与 Metal 门同构）。

        返回 (supported, chain)，chain 元素为 (EffectSpec, 原始采样参数
        dict)——参数打包由 EffectPassRunner 内部完成（与预览同一套）。
        参数安全性判定直接复用 ``_gpu_metal_effect_instance_uniforms``：
        MSL 路径"能否原生执行"的门与 GLSL 变体着色器能理解的参数集
        一致（变体白名单、edge_policy、finite 校验、半径上限、部分
        通道掩码拒绝），复用它避免两套门随时间漂移。unsupported 的
        uniform dict 在 GL 侧被忽略（runner 自行打包）。

        放行范围：EffectPassRunner 有专属执行路径的全部 7 种内置效果
        （blur/sharpen/glow/erode_dilate/edge_detect/denoise/defocus，
        2026-09-17 从 blur 扩展）；白名单外一律整帧回落 CPU。
        """
        clip = getattr(layer, "clip", None)
        effects = tuple(getattr(clip, "effects", ()) or ())
        active = sorted(
            (effect for effect in effects
             if getattr(effect, "is_enabled", True)),
            key=lambda effect: getattr(effect, "order", 0),
        )
        if not active:
            return True, None
        from src.features.shared.effects.effect_catalog import (
            get_effect_catalog,
        )

        catalog = get_effect_catalog()
        resolved: list[Tuple[Any, Dict[str, Any]]] = []
        for effect in active:
            effect_type = str(getattr(effect, "effect_type", ""))
            spec = catalog.get_spec(effect_type)
            if spec is None or not _spec_allows_opengl_export(spec):
                return False, None
            params = self._sample_effect_params(effect, frame_idx)
            if bool(params.get("channel_a", False)):
                # GL 导出中间链是 RGB（无 alpha 通道），alpha 掩码在
                # 此处无对应语义（CPU BGRA 链才有）→ 拒绝。
                return False, None
            if spec.dispatch_key.startswith("plugin:"):
                # 插件 uniforms 由 runner 通用打包（逐参数无内置门）；
                # 能力声明本身即门槛（export_support 头声明）。
                resolved.append((spec, dict(params)))
                continue
            supported, _uniforms = (
                self._gpu_metal_effect_instance_uniforms(effect, frame_idx))
            if not supported:
                return False, None
            if _uniforms is None:
                # 门判定为逐像素 no-op（半径≈0 / mix≈0 / 掩码全关）
                continue
            resolved.append((spec, dict(params)))
        return True, tuple(resolved) or None

    def _gpu_metal_effect_instance_uniforms(
        self,
        effect: Any,
        frame_idx: int,
    ) -> Tuple[bool, Optional[Dict[str, float]]]:
        """Resolve effect parameters implemented by the native Metal path.

        Supported parameters are effect-specific and paired with CPU
        fallbacks. Every built-in Blur variant is implemented natively;
        runtime gates keep unsupported inputs on the CPU path.
        """
        effect_type = str(getattr(effect, "effect_type", ""))
        params = self._sample_effect_params(effect, frame_idx)
        if effect_type == "blur":
            from src.features.shared.effects.effect_pass_runner import (
                _pack_blur_params,
            )

            blur_type = str(params.get("blur_type", "box"))
            if blur_type == "directional":
                blur_type = "motion"
            if blur_type not in {
                    "box", "gaussian", "bilateral", "motion", "bokeh"}:
                return False, None
            try:
                packed = _pack_blur_params(params)
                channel_mask = tuple(packed["u_channel_mask"])
                raw_edge_policy = params.get("edge_policy", "clamp")
                edge_policy_labels = ("clamp", "wrap", "mirror")
                if isinstance(raw_edge_policy, str):
                    if raw_edge_policy in edge_policy_labels:
                        edge_policy = edge_policy_labels.index(raw_edge_policy)
                    elif raw_edge_policy.isdigit() and int(raw_edge_policy) in (0, 1, 2):
                        edge_policy = int(raw_edge_policy)
                    else:
                        return False, None
                elif (isinstance(raw_edge_policy, (int, float))
                      and math.isfinite(float(raw_edge_policy))
                      and float(raw_edge_policy).is_integer()
                      and int(raw_edge_policy) in (0, 1, 2)):
                    edge_policy = int(raw_edge_policy)
                else:
                    return False, None
                radius_x = max(0.0, float(packed["u_radius_x"]))
                radius_y = max(0.0, float(packed["u_radius_y"]))
                mix = max(0.0, min(1.0, float(packed["u_mix"])))
            except (TypeError, ValueError, KeyError):
                return False, None
            if (edge_policy not in (0, 1, 2)
                    or not all(math.isfinite(value) for value in
                               (radius_x, radius_y, mix, *channel_mask))):
                return False, None
            if not any(value >= 0.5 for value in channel_mask[:3]):
                return True, None
            if blur_type in {"motion", "bokeh"}:
                try:
                    radius = max(0.0, float(packed["u_radius"]))
                    quality = str(params.get("quality", "normal"))
                    sample_count = {"fast": 8, "normal": 16, "high": 32}.get(
                        quality)
                    if blur_type == "motion":
                        angle = float(params.get("angle", 0.0))
                        shutter = float(params.get("shutter", 1.0))
                        motion_source = str(params.get("motion_source", "angle"))
                    else:
                        anamorphic = float(params.get("anamorphic", 0.0))
                        highlight_gain = float(params.get("highlight_gain", 0.0))
                        raw_blades = params.get("aperture_blades", "circle")
                        if isinstance(raw_blades, str):
                            blades = 0 if raw_blades == "circle" else int(raw_blades)
                        else:
                            blades = int(raw_blades)
                except (TypeError, ValueError, OverflowError):
                    return False, None
                common = (radius, mix, *channel_mask)
                if (sample_count is None
                        or not all(math.isfinite(value) for value in common)
                        or radius > 100.0
                        or not 0.0 <= mix <= 1.0):
                    return False, None
                if radius < 0.5 or mix <= 0.001:
                    return True, None
                if blur_type == "motion":
                    if (not math.isfinite(angle) or not math.isfinite(shutter)
                            or not 0.0 <= shutter <= 2.0
                            or motion_source != "angle"):
                        return False, None
                    return True, {
                        "kind": 10.0, "half_x": radius,
                        "half_y": shutter, "strength": mix,
                        "sigma_x": math.radians(angle), "sigma_y": shutter,
                        "edge_policy": float(edge_policy),
                        "sample_count": float(sample_count),
                        "channel_r": float(channel_mask[0] >= 0.5),
                        "channel_g": float(channel_mask[1] >= 0.5),
                        "channel_b": float(channel_mask[2] >= 0.5),
                    }
                if (not math.isfinite(anamorphic)
                        or not math.isfinite(highlight_gain)
                        or anamorphic < -1.0 or anamorphic > 1.0
                        or highlight_gain < 0.0 or highlight_gain > 4.0
                        or blades not in (0, 5, 6, 8)):
                    return False, None
                return True, {
                    "kind": 11.0, "half_x": radius,
                    "half_y": float(blades), "strength": mix,
                    "anamorphic": anamorphic,
                    "highlight_gain": highlight_gain,
                    "edge_policy": float(edge_policy),
                    "sample_count": float(sample_count),
                    "aperture_blades": float(blades),
                    "channel_r": float(channel_mask[0] >= 0.5),
                    "channel_g": float(channel_mask[1] >= 0.5),
                    "channel_b": float(channel_mask[2] >= 0.5),
                }
            if blur_type == "bilateral":
                try:
                    radius = max(0.0, float(packed["u_radius"]))
                    edge_preserve = float(params.get("edge_preserve", 0.5))
                except (TypeError, ValueError):
                    return False, None
                if not all(math.isfinite(value) for value in
                           (radius, edge_preserve)):
                    return False, None
                if radius < 0.5 or mix <= 0.001:
                    return True, None
                edge_preserve = max(0.0, min(1.0, edge_preserve))
                diameter = max(3, min(21, int(round(radius)) * 2 + 1))
                half_size = diameter // 2
                spatial_sigma = max(1.0, radius)
                sigma_color = max(5.0, 80.0 * (1.0 - edge_preserve))
                return True, {
                    "kind": 9.0, "half_x": float(half_size),
                    "half_y": float(half_size), "strength": mix,
                    "sigma_x": spatial_sigma, "sigma_y": sigma_color / 255.0,
                    "spatial_sigma": spatial_sigma,
                    "range_sigma": sigma_color / 255.0,
                    "channel_r": float(channel_mask[0] >= 0.5),
                    "channel_g": float(channel_mask[1] >= 0.5),
                    "channel_b": float(channel_mask[2] >= 0.5),
                }
            if any(value < 0.5 for value in channel_mask[:3]):
                return False, None
            if max(radius_x, radius_y) < 0.5 or mix <= 0.001:
                return True, None
            rounded_x, rounded_y = int(round(radius_x)), int(round(radius_y))
            half_x, half_y = rounded_x, rounded_y
            # The MSL implementation has statically bounded 33-tap axes;
            # larger radii are explicitly declined before buffer allocation.
            if max(half_x, half_y) > 16:
                return False, None
            if half_x == 0 and half_y == 0:
                return True, None
            kind = 1 if blur_type == "box" else 2
            sigma_x = (0.3 * (rounded_x - 1) + 0.8) if kind == 2 else 1.0
            sigma_y = (0.3 * (rounded_y - 1) + 0.8) if kind == 2 else 1.0
            return True, {
                "kind": float(kind), "half_x": float(half_x),
                "half_y": float(half_y), "strength": float(mix),
                "sigma_x": float(sigma_x), "sigma_y": float(sigma_y),
                "edge_policy": float(edge_policy),
            }

        if effect_type == "sharpen":
            try:
                amount = float(params.get("amount", 0.0))
                radius = float(params.get("radius", 1.0))
                threshold = float(params.get("threshold", 0.0))
                channels = str(params.get("channels", "all"))
            except (TypeError, ValueError):
                return False, None
            if not all(math.isfinite(value) for value in
                       (amount, radius, threshold)):
                return False, None
            if channels not in {"all", "rgb", "luminance"}:
                return False, None
            if amount < 0.01 or radius < 0.1:
                return True, None
            kernel_size = max(1, int(round(radius * 2.0)) | 1)
            half_size = kernel_size // 2
            if half_size > 16:
                return False, None
            return True, {
                "kind": 3.0, "half_x": float(half_size),
                "half_y": float(half_size), "strength": float(amount),
                "sigma_x": float(radius * 0.5),
                "sigma_y": float(radius * 0.5),
                "threshold": max(0.0, threshold),
                "channels": float(("all", "rgb", "luminance").index(channels)),
            }

        if effect_type == "glow":
            try:
                threshold = float(params.get("threshold", 0.7))
                radius = float(params.get("radius", 10.0))
                intensity = float(params.get("intensity", 1.0))
                mix = float(params.get("mix", 1.0))
                tint_r = float(params.get("tint_r", 1.0))
                tint_g = float(params.get("tint_g", 0.85))
                tint_b = float(params.get("tint_b", 0.7))
                quality = str(params.get("quality", "normal"))
            except (TypeError, ValueError):
                return False, None
            if not all(math.isfinite(value) for value in
                       (threshold, radius, intensity, mix,
                        tint_r, tint_g, tint_b)):
                return False, None
            if radius < 0.5 or intensity < 0.001 or mix < 0.001:
                return True, None
            if not 0.0 <= mix <= 1.0:
                return False, None
            sample_count = {"fast": 8, "normal": 16, "high": 32}.get(
                quality)
            if sample_count is None:
                return False, None
            threshold = max(0.0, min(1.0, threshold))
            return True, {
                "kind": 4.0, "half_x": float(sample_count),
                "half_y": float(sample_count), "strength": float(mix),
                "sigma_x": float(radius * 0.5),
                "sigma_y": float(radius * 0.5),
                "threshold": threshold, "intensity": float(intensity),
                "tint_r": tint_r, "tint_g": tint_g, "tint_b": tint_b,
            }

        if effect_type == "erode_dilate":
            try:
                operation = str(params.get("operation", "erode"))
                edge_mode = str(params.get("edge_mode", "extend"))
                channels = str(params.get("channels", "alpha"))
                raw_size = float(params.get("size", 3))
                raw_iterations = float(params.get("iterations", 1))
            except (TypeError, ValueError):
                return False, None
            if (operation not in {"erode", "dilate"}
                    or edge_mode not in {"extend", "reflect", "wrap"}
                    or channels not in {"alpha", "all"}
                    or not math.isfinite(raw_size)
                    or not math.isfinite(raw_iterations)
                    or not raw_size.is_integer()
                    or not raw_iterations.is_integer()):
                return False, None
            size = int(raw_size)
            iterations = int(raw_iterations)
            if not 1 <= size <= 50 or not 1 <= iterations <= 10:
                return False, None
            # Timeline export frames are opaque BGR, so alpha-only morphology
            # is a semantic identity. The native Metal path supports the RGB
            # operation for all declared border modes and a bounded radius.
            if channels == "alpha" or size == 1:
                return True, None
            half_size = (size // 2) * iterations
            return True, {
                "kind": 5.0, "half_x": float(half_size),
                "half_y": float(half_size), "strength": 1.0,
                "sigma_x": 1.0, "sigma_y": 1.0,
                "operation": float(operation == "dilate"),
                "edge_policy": float(("extend", "wrap", "reflect").index(edge_mode)),
            }

        if effect_type == "edge_detect":
            try:
                method = str(params.get("method", "sobel"))
                direction = str(params.get("direction", "both"))
                output_mode = str(params.get("output_mode", "edges"))
                channel = str(params.get("channel", "luminance"))
                threshold = float(params.get("threshold", 0.1))
                threshold_high = float(params.get("threshold_high", 0.3))
                output_strength = float(params.get("output_strength", 1.0))
                raw_kernel_size = float(params.get("kernel_size", 3))
                preblur = float(params.get("preblur", 0.0))
            except (TypeError, ValueError):
                return False, None
            if (method not in {"sobel", "laplacian", "prewitt", "canny"}
                    or direction not in {"both", "horizontal", "vertical"}
                    or output_mode not in {"edges", "gradient", "direction"}
                    or channel not in {"luminance", "r", "g", "b", "a"}
                    or not all(math.isfinite(value) for value in (
                        threshold, threshold_high, output_strength,
                        raw_kernel_size, preblur,
                    ))):
                return False, None
            if (method not in {"sobel", "canny", "prewitt", "laplacian"}
                    or raw_kernel_size not in (3.0, 5.0, 7.0)
                    or not 0.0 <= preblur <= 10.0
                    or not 0.0 <= threshold <= 1.0
                    or not 0.0 <= threshold_high <= 1.0
                    or not 0.0 <= output_strength <= 10.0):
                return False, None
            return True, {
                "kind": 6.0, "half_x": raw_kernel_size, "half_y": 1.0,
                "strength": output_strength,
                "sigma_x": threshold, "sigma_y": threshold_high,
                "direction": float(
                    ("both", "horizontal", "vertical").index(direction)),
                "output_mode": float(
                    ("edges", "gradient", "direction").index(output_mode)),
                "invert": float(bool(params.get("invert", False))),
                "channel": float({
                    "luminance": 0, "a": 1, "r": 2, "g": 3, "b": 4,
                }[channel]),
                "method": float({
                    "sobel": 0, "canny": 1,
                    "prewitt": 2, "laplacian": 3,
                }[method]),
                "preblur": preblur,
            }

        if effect_type == "denoise":
            try:
                strength = float(params.get("strength", 0.5))
                raw_radius = float(params.get("radius", 3))
                luminance_only = bool(params.get("luminance_only", False))
                chroma_only = bool(params.get("chroma_only", False))
            except (TypeError, ValueError):
                return False, None
            if (not math.isfinite(strength)
                    or not math.isfinite(raw_radius)
                    or not raw_radius.is_integer()
                    or not 0.0 <= strength <= 1.0
                    or not 1 <= int(raw_radius) <= 10):
                return False, None
            if strength < 0.001:
                return True, None
            radius = int(raw_radius)
            spatial_sigma = max(float(radius) * 0.4, 1.0)
            range_sigma = max(0.1, 0.15 * (1.0 - strength * 0.5))
            return True, {
                "kind": 7.0, "half_x": float(radius),
                "half_y": float(radius), "strength": strength,
                "sigma_x": spatial_sigma, "sigma_y": range_sigma,
                "spatial_sigma": spatial_sigma,
                "range_sigma": range_sigma,
                "luminance_only": float(luminance_only),
                "chroma_only": float(chroma_only),
            }

        if effect_type == "defocus":
            try:
                mode = str(params.get("mode", "separable"))
                focal_x = float(params.get("focal_x", 0.5))
                focal_y = float(params.get("focal_y", 0.5))
                focal_size = float(params.get("focal_size", 0.3))
                max_radius = float(params.get("max_radius", 20.0))
                process_alpha = bool(params.get("process_alpha", True))
                raw_edge_policy = params.get("edge_policy", "clamp")
            except (TypeError, ValueError):
                return False, None
            if (mode not in {"separable", "mipmap"}
                    or not all(math.isfinite(value) for value in (
                        focal_x, focal_y, focal_size, max_radius,
                    ))
                    or not 0.0 <= focal_x <= 1.0
                    or not 0.0 <= focal_y <= 1.0
                    or not 0.01 <= focal_size <= 1.0
                    or not 0.0 <= max_radius <= 100.0):
                return False, None
            edge_labels = ("clamp", "wrap", "mirror")
            if isinstance(raw_edge_policy, str):
                if raw_edge_policy in edge_labels:
                    edge_policy = edge_labels.index(raw_edge_policy)
                elif raw_edge_policy.isdigit() and int(raw_edge_policy) in (0, 1, 2):
                    edge_policy = int(raw_edge_policy)
                else:
                    return False, None
            elif (isinstance(raw_edge_policy, (int, float))
                  and math.isfinite(float(raw_edge_policy))
                  and float(raw_edge_policy).is_integer()
                  and int(raw_edge_policy) in (0, 1, 2)):
                edge_policy = int(raw_edge_policy)
            else:
                return False, None
            if max_radius < 0.1:
                return True, None
            return True, {
                "kind": 8.0, "half_x": 15.0, "half_y": 0.0,
                "strength": 1.0,
                "sigma_x": focal_x, "sigma_y": focal_y,
                "focal_x": focal_x, "focal_y": focal_y,
                "focal_size": focal_size, "max_radius": max_radius,
                "edge_policy": float(edge_policy),
                "process_alpha": float(process_alpha),
                "mode": float(mode == "mipmap"),
            }

        return False, None

    def _gpu_plane_depth(
        self,
        index: int,
        plane: "np.ndarray",
        fmt_lower: str,
        plane_infos: Any,
    ) -> int:
        """镜像 _planes_to_bgr_float._plane_depth 的位深权威顺序。"""
        pis = plane_infos or ()
        if index < len(pis):
            bit_depth = int(getattr(pis[index], "bit_depth", 0) or 0)
            if bit_depth > 0:
                return bit_depth
        if _is_topos_planar_format(fmt_lower):
            color, _alpha = _topos_planar_depths(fmt_lower)
            return color
        return self._bit_depth_for_pixel_format(fmt_lower, plane.dtype)

    def _gpu_planar_source_from_decoded(self, decoded_frame: Any) -> Optional[Any]:
        """DecodedFrame → GpuYuvSource（仅 planar YUV 无 alpha；否则 None）。

        range/matrix 消费与 _planes_to_bgr_float 同源（H.273 元数据，
        缺省 limited/bt709）。
        """
        import numpy as np

        planes = getattr(decoded_frame, "planes", None) or ()
        fmt_lower = str(
            getattr(decoded_frame, "pixel_format", "") or "").strip().lower()
        if len(planes) != 3 or fmt_lower not in self._GPU_CHROMA_DIV_BY_FORMAT:
            return None
        y_plane, u_plane, v_plane = planes
        if y_plane.ndim != 2 or u_plane.ndim != 2 or v_plane.ndim != 2:
            return None
        if y_plane.dtype not in (np.uint8, np.uint16):
            return None
        chroma_x, chroma_y = self._GPU_CHROMA_DIV_BY_FORMAT[fmt_lower]
        metadata = getattr(decoded_frame, "metadata", None)
        full_range = str(
            getattr(metadata, "color_range", "limited") or "limited"
        ).lower() == "full"
        matrix = str(
            getattr(metadata, "color_matrix", "bt709") or "bt709"
        ).lower()
        if matrix in ("bt2020nc", "bt2020c", "bt2020"):
            kr, kb = 0.2627, 0.0593
        elif matrix in ("smpte170m", "bt470bg", "bt470m", "fcc"):
            kr, kb = 0.2990, 0.1140
        else:
            kr, kb = 0.2126, 0.0722
        from src.features.edit.core.timeline_gpu_export_renderer import (
            GpuYuvSource,
        )
        return GpuYuvSource(
            planes=(y_plane, u_plane, v_plane),
            luma_bits=self._gpu_plane_depth(
                0, y_plane, fmt_lower,
                getattr(decoded_frame, "plane_infos", None)),
            chroma_x_div=chroma_x,
            chroma_y_div=chroma_y,
            full_range=full_range,
            kr=kr,
            kb=kb,
        )

    def _gpu_grade_uniforms(
        self,
        layer: "ClipLayerInfo",
    ) -> Optional[Dict[str, Any]]:
        """层调色参数 → shader uniform（关键帧按 source_frame 求值，
        镜像 _apply_color_grade 的参数解析路径）。"""
        grade = self._get_clip_color_grade(layer)
        if grade is None:
            return None
        grading_params = grade["grading_params"]
        if grade.get("grade_keyframes"):
            try:
                from src.features.color.core.grade_keyframes import (
                    GradeKeyframeTrack, evaluate_grade_keyframes,
                )
                tracks = {
                    str(name): GradeKeyframeTrack.from_dict(track)
                    for name, track in grade["grade_keyframes"].items()
                }
                grading_params = evaluate_grade_keyframes(
                    tracks, int(layer.source_frame), grading_params)
            except Exception as error:
                logger.warning(
                    f"Export GPU grade keyframe eval failed for clip "
                    f"{layer.clip_id}: {error}"
                )
        from src.features.edit.core.timeline_gpu_export_renderer import (
            grade_params_to_uniforms,
        )
        main = grade_params_to_uniforms(grading_params)
        # M05（S08）/F4.1：链段作为 grade pass 序列返回，渲染端按序逐段
        # 过 shader（mid FBO 乒乓）——pre 段在主栈**之前**，trim/post 段
        # 在主栈之后（pre→main→trim→post；未标记载荷按主栈之后处理）。
        # 高级标记段不进 GPU 序列（primary 域不可表示）。
        extras = [
            e for e in (grade.get("chain_extra_params") or ())
            if isinstance(e, dict)
            and not e.get("_unsupported_advanced", False)]
        pre_chain = [
            grade_params_to_uniforms({
                k: v for k, v in extra.items()
                if k not in ("_unsupported_advanced", "_chain_segment")})
            for extra in extras if extra.get("_chain_segment") == "pre"]
        after_chain = [
            grade_params_to_uniforms({
                k: v for k, v in extra.items()
                if k not in ("_unsupported_advanced", "_chain_segment")})
            for extra in extras if extra.get("_chain_segment") != "pre"]
        chain = pre_chain + [main] + after_chain
        return chain if len(chain) > 1 else main

    # =================================================================
    # 调色应用（CPU 纯 numpy，与 TimelinePreviewRenderer 对齐）
    # =================================================================

    def _occlusion_facts_for_layer(self, layer: "ClipLayerInfo"):
        """收集单层遮挡分析所需的静态事实（O3 共享模块契约）。

        与预览侧 ``TimelinePreviewRenderer._occlusion_facts_for_layer``
        同语义；素材类型/像素格式来自 EditProject 资产元数据，效果链
        只统计启用中的效果。任何失败都返回 None（fail-closed）。
        """
        try:
            from src.features.edit.core.timeline_occlusion import (
                LayerOcclusionFacts,
                effects_background_independent,
                normalize_blend_mode,
                source_alpha_known_opaque,
            )
            asset_id = getattr(layer, "asset_id", "") or ""
            asset = (
                self._project.get_asset_by_id(asset_id)
                if self._project is not None
                else None
            )
            metadata = getattr(asset, "metadata", {}) or {}
            clip = getattr(layer, "clip", None)
            effects = tuple(getattr(clip, "effects", ()) or ()) if clip is not None else ()
            return LayerOcclusionFacts(
                asset_kind=str(getattr(asset, "media_type", "") or ""),
                source_alpha_known_opaque=source_alpha_known_opaque(
                    metadata.get("pixel_format")
                ),
                blend_mode=normalize_blend_mode(
                    getattr(clip, "blend_mode", "normal")
                ),
                # O4 复查对齐：与预览侧同口径——clip 上挂载的任何效果
                # （含禁用中的）都阻止该层成为遮挡层。预览 CPU 效果链
                # 目前不跳过禁用效果（存量行为），若导出按"仅启用中"
                # 统计会造成预览/导出裁剪决策分歧；统一按保守口径。
                has_effects=bool(effects),
                # O7：能力声明仅作为事实记录，第一阶段不改变裁剪行为。
                background_independent=effects_background_independent(effects),
            )
        except Exception:  # noqa: BLE001 — 事实收集失败按未知层处理
            return None

    def _get_clip_color_grade(
        self,
        layer: "ClipLayerInfo",
    ) -> Optional[Dict[str, Any]]:
        """
        返回该层 clip 的 ColorStack 调色参数；若无需调色则返回 None。

        与 TimelinePreviewRenderer._get_preview_color_grade 的实现保持一致，
        保证导出与实时预览的 primary grade 像素级对齐。

        参数来源：EditProject.color_stacks[clip.color_stack_id] -> ColorStackData。
        """
        clip = getattr(layer, "clip", None)
        stack_id = getattr(clip, "color_stack_id", None)
        if not stack_id:
            return None

        project = self._project
        stacks = getattr(project, "color_stacks", {}) if project is not None else {}
        stack = stacks.get(stack_id)
        if stack is None:
            return None

        grading_params = dict(getattr(stack, "grading_params", {}) or {})
        wheel_defaults = {
            "lift": (0.0, 0.0, 0.0),
            "gamma": (0.0, 0.0, 0.0),
            "gain": (0.0, 0.0, 0.0),
        }
        scalar_defaults = {
            "exposure": 0.0,
            "temp": 0.0,
            "tint": 0.0,
            "contrast": 0.0,
            "shadow": 0.0,
            "highlights": 0.0,
            "saturation": 0.0,
            "vibrance": 0.0,
            "mid_detail": 0.0,
            "sharpness": 0.0,
            "hue_shift": 0.0,
        }

        def changed(value: Any, default: Any) -> bool:
            try:
                if isinstance(default, tuple):
                    values = tuple(float(item) for item in value)
                    return len(values) != len(default) or any(
                        abs(item - expected) > 1e-6
                        for item, expected in zip(values, default)
                    )
                return abs(float(value) - float(default)) > 1e-6
            except (TypeError, ValueError):
                return value != default

        basic_active = any(
            changed(grading_params.get(name, default), default)
            for name, default in {**wheel_defaults, **scalar_defaults}.items()
        )
        advanced_active = bool(
            getattr(stack, "curves_data", None)
            or getattr(stack, "hsl_curve_data", None)
            or getattr(stack, "mixer_params", None)
            or getattr(stack, "lut_path", None)
            or bool((getattr(stack, "soft_clip_params", {}) or {}).get("enabled", False))
            or not curves_soft_clip_is_identity(
                getattr(stack, "curves_soft_clip", None))
            or not curves_edit_is_default(getattr(stack, "curves_edit", None))
        )
        # P1-COLOR-03: 提取调色关键帧轨道（活动层或首个含轨道的层）。
        # 帧求值在 _apply_color_grade 按 layer.source_frame 进行，与
        # Color 页 GPU 预览共用 evaluate_grade_keyframes，帧值一致。
        grade_keyframes = None
        layers_snapshot = list(getattr(stack, "layers", None) or ())
        active_index = getattr(stack, "active_layer_index", None)
        ordered_layers = []
        if isinstance(active_index, int) and 0 <= active_index < len(layers_snapshot):
            ordered_layers.append(layers_snapshot[active_index])
        ordered_layers.extend(
            layer_dict for i, layer_dict in enumerate(layers_snapshot)
            if i != active_index
        )
        for layer_dict in ordered_layers:
            if isinstance(layer_dict, dict) and layer_dict.get("grade_keyframes"):
                grade_keyframes = layer_dict["grade_keyframes"]
                break

        # M05.2：layer 级已应用 LUT（active_lut_path）同样属高级段——
        # 不进 advanced 检测会静默无效果。
        for layer_dict in ordered_layers:
            if isinstance(layer_dict, dict) and layer_dict.get("active_lut_path"):
                advanced_active = True
                break

        # M06（S11）：qualifier 属 primary 域外的 matte 限定——纳入
        # advanced 检测（此前只开 qualifier 的栈 grade 返回 None，
        # 静默无效果）；参数随 grade 字典透传，渲染端按帧求值后显式
        # 告警丢弃（CPU primary 导出不渲染 matte，不静默）。
        qualifier_params = None
        for layer_dict in ordered_layers:
            if not isinstance(layer_dict, dict):
                continue
            qp = layer_dict.get("qualifier_params") or None
            if qp and qp.get("enabled"):
                qualifier_params = dict(qp)
                advanced_active = True
                break

        # F4.1: 调色链段（与预览快照同键；解析单点 core/color_chain）
        from src.features.edit.core.color_chain import (
            chain_extra_params, extras_active,
        )
        # 测试替身可能只装配 _project（无 _sequence）——防御 getattr
        timeline_id = str(
            getattr(getattr(self, "_sequence", None),
                    "timeline_id", "") or "")
        extras = chain_extra_params(project, timeline_id, clip)
        extras_on = extras_active(extras)

        if not basic_active and not advanced_active and not grade_keyframes \
                and not extras_on:
            return None

        # M05.2b/N03：曲线载荷——curves_data（M/R/G/B 控制点）随 grade 透传，
        # CPU 导出经 build_curves_lut_from_data 逐像素应用（既有权威
        # 实现，与 Color 页 GPU 同一控制点语义）。
        # N03 余项：**逐层载荷**替换首取扁平化——旧实现按序首取首个含
        # curves/hsl/soft_clip/LUT 的层（多层数据静默只剩首个，层身份/
        # opacity/mask/plugin/次序全部丢失）。现每层独立入
        # ``layers``（原始索引+active 标记+全量高级载荷），消费方按层
        # 组合；扁平兼容键仅在**恰好一层**含该类内容时等价填充，多层
        # 并存即 has_advanced_operations=True（交由降级硬门裁决——CPU
        # primary 不做多层高级组合，不静默取首层冒充完整）。
        curves_data = None
        hsl_curve_lut = None
        soft_clip_params = None
        curves_soft_clip_state = None
        curves_edit_state = None
        lut_path = str(getattr(stack, "lut_path", None) or "") or None
        lut_intensity = 1.0
        layers_payload: list = []
        advanced_layer_count = 0
        for layer_index, layer_dict in enumerate(layers_snapshot):
            if not isinstance(layer_dict, dict):
                continue
            layer_curves = layer_dict.get("curves_data") or None
            layer_hsl = layer_dict.get("hsl_curve_lut")
            layer_soft = (layer_dict.get("soft_clip_params") or None) \
                if (layer_dict.get("soft_clip_params") or {}).get(
                    "enabled") else None
            # 曲线面板侧栏状态：非恒等/非默认才入载荷（与 soft_clip 的
            # enabled 门控同口径）
            layer_csc = layer_dict.get("curves_soft_clip")
            layer_csc = None if curves_soft_clip_is_identity(layer_csc) \
                else layer_csc
            layer_cedit = layer_dict.get("curves_edit")
            layer_cedit = None if curves_edit_is_default(layer_cedit) \
                else layer_cedit
            layer_lut = layer_dict.get("active_lut_path") or None
            layer_qp = None
            qp = layer_dict.get("qualifier_params") or None
            if qp and qp.get("enabled"):
                layer_qp = dict(qp)
            layer_masks = layer_dict.get("mask_shapes") or None
            layer_plugins = layer_dict.get("plugins") or None
            is_advanced = bool(
                layer_curves or layer_hsl is not None or layer_soft
                or layer_csc or layer_cedit
                or layer_lut or layer_qp or layer_masks or layer_plugins)
            if is_advanced:
                advanced_layer_count += 1
            layers_payload.append({
                "index": layer_index,
                "active": layer_index == active_index,
                "opacity": layer_dict.get("opacity", 1.0),
                "grading_params": dict(
                    layer_dict.get("grading_params") or {}),
                "curves_data": dict(layer_curves) if layer_curves else None,
                "hsl_curve_lut": layer_hsl,
                "soft_clip_params": layer_soft,
                "curves_soft_clip": layer_csc,
                "curves_edit": layer_cedit,
                "lut_path": str(layer_lut) if layer_lut else None,
                "lut_intensity": float(
                    layer_dict.get("lut_intensity", 1.0) or 1.0),
                "qualifier_params": layer_qp,
                "mask_shapes": layer_masks,
                "plugins": layer_plugins,
            })
        if advanced_layer_count > 1:
            advanced_active = True  # 多层高级组合超 CPU 扁平能力→硬门
        # 单层兼容填充（与旧首取语义等价：唯一含内容的层即首取层）
        for entry in layers_payload:
            if curves_data is None and entry["curves_data"]:
                curves_data = dict(entry["curves_data"])
            if hsl_curve_lut is None and entry["hsl_curve_lut"] is not None:
                hsl = entry["hsl_curve_lut"]
                hsl_curve_lut = np.asarray(hsl, dtype=np.float32) \
                    if not isinstance(hsl, np.ndarray) else hsl
            if soft_clip_params is None and entry["soft_clip_params"]:
                soft_clip_params = dict(entry["soft_clip_params"])
            if curves_soft_clip_state is None and entry["curves_soft_clip"]:
                curves_soft_clip_state = dict(entry["curves_soft_clip"])
            if curves_edit_state is None and entry["curves_edit"]:
                curves_edit_state = dict(entry["curves_edit"])
            if lut_path is None and entry["lut_path"]:
                lut_path = entry["lut_path"]
                lut_intensity = entry["lut_intensity"]
        return {
            "stack_id": str(stack_id),
            "grading_params": grading_params,
            "has_advanced_operations": advanced_active,
            "grade_keyframes": grade_keyframes,
            "chain_extra_params": extras,
            "qualifier_params": qualifier_params,
            "curves_data": curves_data,
            "hsl_curve_lut": hsl_curve_lut,
            "soft_clip_params": soft_clip_params,
            "curves_soft_clip": curves_soft_clip_state,
            "curves_edit": curves_edit_state,
            "lut_path": lut_path,
            "lut_intensity": lut_intensity,
            # N03：逐层全量载荷（原始索引/active/opacity/高级内容/mask/
            # plugin/次序保留——替换首取扁平化的消费通道）
            "layers": tuple(layers_payload),
        }

    def _get_clip_video_denoise(
        self,
        layer: "ClipLayerInfo",
    ) -> Optional[Dict[str, Any]]:
        """返回该层 clip 栈上的 per-clip 视频降噪参数（未启用返回 None）。"""
        clip = getattr(layer, "clip", None)
        stack_id = getattr(clip, "color_stack_id", None)
        if not stack_id:
            return None
        project = self._project
        stacks = getattr(project, "color_stacks", {}) if project is not None else {}
        stack = stacks.get(stack_id)
        if stack is None:
            return None
        state = getattr(stack, "video_denoise", None)
        if isinstance(state, dict) and state:
            return state
        return None

    def _apply_video_denoise_export(
        self,
        frame_data: "np.ndarray",
        layer: "ClipLayerInfo",
    ) -> "np.ndarray":
        """P1-COLOR-13: 对 clip 帧应用导出档 GPU 降噪（headless 桥）。

        复用 Color 页 VideoDenoisePipeline 保证导出与预览像素一致；
        GL 不可用/异常时直通原帧。调用点在调色之前，与预览管线
        顺序一致（denoise → grade）。
        """
        try:
            if frame_data is None:
                return frame_data
            params = self._get_clip_video_denoise(layer)
            if not params:
                return frame_data
            if self._denoise_bridge is None:
                from src.features.color.core.export_denoise_bridge import (
                    ExportVideoDenoiseBridge,
                )

                self._denoise_bridge = ExportVideoDenoiseBridge()
            if frame_data.ndim == 3 and frame_data.shape[2] == 4:
                # R2：降噪桥按 3 通道纹理设计——Alpha 导出下仅对颜色通道
                # 降噪，alpha 原样保留（降噪不改变遮罩）
                import numpy as np

                denoised = self._denoise_bridge.process_frame(
                    frame_data[..., :3],
                    getattr(layer, "clip_id", ""),
                    int(getattr(layer, "source_frame", 0)),
                    params,
                )
                return np.concatenate(
                    [denoised, frame_data[..., 3:4]], axis=-1)
            return self._denoise_bridge.process_frame(
                frame_data,
                getattr(layer, "clip_id", ""),
                int(getattr(layer, "source_frame", 0)),
                params,
            )
        except Exception as error:
            logger.warning(
                f"Export video denoise failed for clip "
                f"{getattr(layer, 'clip_id', '?')}: {error}"
            )
            return frame_data

    # =================================================================
    # Clip effects (CPU numpy) — Phase 4
    #
    # Metal handles bounded subsets of the built-in timeline effects before
    # reaching this CPU path. This registry remains the fallback for CPU
    # effects and unsupported Metal parameters;
    # export preflight blocks effects with no implementation so they are
    # never silently omitted.
    # =================================================================

    def _apply_clip_effects_cpu(
        self,
        frame_data: "np.ndarray",
        layer: "ClipLayerInfo",
        frame_idx: int,
    ) -> "np.ndarray":
        """Apply a clip's video-filter chain to one BGR uint8 frame.

        Iterates the clip's effects in ``order`` (matching the GPU path),
        resolves each parameter at ``frame_idx`` (handles keyframe
        interpolation via EffectInstance.get_param_at_frame), and
        dispatches to the matching CPU implementation.

        Disabled effects are skipped. Unknown effects passthrough.
        """
        clip = getattr(layer, "clip", None)
        if clip is None:
            return frame_data
        effects = getattr(clip, "effects", ()) or ()
        if not effects:
            return frame_data

        result = frame_data
        for effect in sorted(effects, key=lambda e: getattr(e, "order", 0)):
            if not getattr(effect, "is_enabled", True):
                continue
            effect_type = getattr(effect, "effect_type", "")
            params = self._sample_effect_params(effect, frame_idx)
            handler = _CPU_EFFECT_HANDLERS.get(effect_type)
            if handler is None:
                # setdefault：__new__ 直构的 exporter（单测）无该属性也不崩
                skips = self.__dict__.setdefault("_effect_export_skips", [])
                if effect_type not in skips:
                    skips.append(effect_type)
                logger.warning(
                    "TimelineExporter: effect '%s' has no CPU export "
                    "implementation; skipping (GPU preview still applies it)",
                    effect_type,
                )
                continue
            try:
                result = handler(result, params)
            except Exception as exc:  # noqa: BLE001 — one bad effect mustn't kill export
                # AUD-18409：运行期失败不再只留运行日志——帧保持未处理
                # 交付却按成功收尾，违反"不得静默缺效果"。逐项（带原因）
                # 记入 _effect_export_skips，进入 manifest degradations，
                # 交付审计可见。
                skip_entry = f"{effect_type} (runtime_failure: {exc})"
                skips = self.__dict__.setdefault("_effect_export_skips", [])
                if skip_entry not in skips:
                    skips.append(skip_entry)
                logger.warning(
                    "TimelineExporter: CPU effect '%s' failed (%s); "
                    "frame left unfiltered",
                    effect_type, exc,
                )
        return result

    def _apply_crop_cpu(
        self,
        frame_data: "np.ndarray",
        layer: "ClipLayerInfo",
    ) -> "np.ndarray":
        """Apply a clip's CropParams matte to an export frame (FX-M06).

        Mirrors the CPU-preview and GPU paths but for the export pipeline.
        Uses the shared ``crop_render`` math so export, CPU preview and GPU
        preview agree on crop geometry. Identity crops return the frame
        unchanged (no allocation).
        """
        crop_tuple = getattr(layer, "crop", None)
        if not crop_tuple:
            return frame_data
        from src.features.edit.core.clip_params import CropParams
        from src.features.edit.core import crop_render
        try:
            crop = CropParams(
                left=float(crop_tuple[0]), right=float(crop_tuple[1]),
                top=float(crop_tuple[2]), bottom=float(crop_tuple[3]),
                feather=float(crop_tuple[4]),
            )
        except (TypeError, ValueError, IndexError):
            return frame_data
        if crop_render.crop_is_identity(crop):
            return frame_data
        if frame_data.ndim == 3 and frame_data.shape[-1] == 4:
            # 复验 P1-01：四通道裁剪——①方法内此前无 numpy 导入（运行时
            # NameError 直接失败）；②固定 /255 + uint8 不支持 RGBA16；
            # ③预乘式裁剪结果被下游 straight 合成消费。改为 straight-alpha
            # 语义（仅缩放 alpha）+ dtype 满刻度缩放。
            rect = crop_render.crop_rect_for_size(
                crop, frame_data.shape[1], frame_data.shape[0])
            return crop_render.apply_crop_straight_alpha(frame_data, rect)
        return crop_render.apply_crop_bgr(frame_data, crop)

    @staticmethod
    def _sample_effect_params(effect, frame_idx: int) -> dict:
        """Resolve an effect's parameter values at ``frame_idx``.

        Uses ``get_param_at_frame`` when available (handles keyframe
        interpolation); falls back to the static ``params`` dict.
        """
        get_at_frame = getattr(effect, "get_param_at_frame", None)
        params_dict = getattr(effect, "params", {}) or {}
        if get_at_frame is None:
            return dict(params_dict)
        out = {}
        for name in params_dict:
            try:
                out[name] = get_at_frame(name, frame_idx)
            except Exception:  # noqa: BLE001
                out[name] = params_dict[name]
        return out

    def _clip_stabilization_state(self, layer: "ClipLayerInfo") -> dict:
        from src.features.shared.effects.video_stabilization import clip_stabilization_state
        return clip_stabilization_state(getattr(self, '_project', None), getattr(layer, 'clip', None))

    def _apply_color_grade(self, frame_data: "np.ndarray", layer: "ClipLayerInfo") -> "np.ndarray":
        from src.features.shared.effects.video_stabilization import apply_stabilization_cpu
        graded = self._apply_color_grade_unstabilized(frame_data, layer)
        return apply_stabilization_cpu(graded, self._clip_stabilization_state(layer),
                                       int(getattr(layer, 'source_frame', 0)))

    def _apply_color_grade_unstabilized(
        self,
        frame_data: "np.ndarray",
        layer: "ClipLayerInfo",
    ) -> "np.ndarray":
        """
        对 BGR uint8 帧应用 clip 的 ColorStack primary grade（CPU 纯 numpy）。

        等价于 TimelinePreviewRenderer._apply_preview_color_grade。
        只跑 primary grade（lift/gamma/gain/exposure/temp/.../hue_shift），
        不处理 curves/hsl_curve/mixer/soft_clip/lut（与预览 CPU fallback 同等降级）。
        """
        grade = self._get_clip_color_grade(layer)
        if grade is None:
            return frame_data

        # P1-COLOR-03: 有关键帧轨道时按当前导出帧求值（与预览同一函数）
        grading_params = grade["grading_params"]
        if grade.get("grade_keyframes"):
            try:
                from src.features.color.core.grade_keyframes import (
                    GradeKeyframeTrack, evaluate_grade_keyframes,
                )
                tracks = {
                    str(name): GradeKeyframeTrack.from_dict(track)
                    for name, track in grade["grade_keyframes"].items()
                }
                grading_params = evaluate_grade_keyframes(
                    tracks, int(layer.source_frame), grading_params)
            except Exception as error:
                logger.warning(
                    f"Export grade keyframe eval failed for clip "
                    f"{layer.clip_id}: {error}"
                )

        # M06（S11）：qualifier.<field> 轨道按导出帧求值（与 Color 引擎
        # evaluate_qualifier_keyframes 同函数——此前只在 Color 预览求值，
        # 导出侧轨道被基础白名单静默忽略）。CPU primary 导出不渲染
        # matte：求值后显式告警丢弃，不静默。
        qualifier_params = grade.get("qualifier_params")
        if qualifier_params and grade.get("grade_keyframes"):
            # 轨道载荷可能是序列化 dict（grade 字典来自 ColorStackData
            # layers 快照）或对象（测试替身）——统一成对象后求值
            from types import SimpleNamespace as _NS
            from src.features.color.core.grade_keyframes import (
                GradeKeyframeTrack, evaluate_qualifier_keyframes,
            )
            qp_tracks = {}
            for name, track in dict(grade["grade_keyframes"]).items():
                if not str(name).startswith("qualifier."):
                    continue
                try:
                    qp_tracks[str(name)] = (
                        track if isinstance(track, GradeKeyframeTrack)
                        else GradeKeyframeTrack.from_dict(track))
                except Exception:  # noqa: BLE001 — 单轨畸形不阻断导出
                    continue
            if qp_tracks:
                _qp_layer = _NS(
                    qualifier_params=dict(qualifier_params),
                    grade_keyframes=qp_tracks)
                evaluate_qualifier_keyframes(
                    _qp_layer, int(layer.source_frame))
                qualifier_params = _qp_layer.qualifier_params
        allow_degraded = bool(getattr(self, "allow_degraded_grade", False))
        qualifier_matte = None
        if qualifier_params:
            # M06.2/N03：CPU（numpy）keyer——基础三带（hue/sat/lum）+
            # invert 像素级应用（keyer.glsl 的 numpy 移植）。
            # N03 硬门：keyer 计算失败（旧=回退无限 matte 继续成片——
            # 未限定整帧被误调=视觉错误标成功）与 matte 后处理
            # （clean/blur/denoise）不支持（旧=丢弃仅告警）均为**默认
            # 拒绝导出**；仅在导出器显式 ``allow_degraded_grade`` 时
            # 保留旧降级路径（显式声明的能力取舍，不再静默）。
            try:
                import numpy as _np_qp
                from src.features.color.core.qualifier_cpu import (
                    compute_qualifier_matte, qualifier_postprocess_active,
                )
                _src_rgb = frame_data[..., :3][..., ::-1]
                if _np_qp.issubdtype(_src_rgb.dtype, _np_qp.integer):
                    _max = float(_np_qp.iinfo(_src_rgb.dtype).max)
                    _src_rgb = _src_rgb.astype(_np_qp.float32) / _max
                else:
                    _src_rgb = _src_rgb.astype(_np_qp.float32)
                qualifier_matte = compute_qualifier_matte(
                    _src_rgb, qualifier_params)
                if qualifier_postprocess_active(qualifier_params):
                    if not allow_degraded:
                        raise ExportGradeDegradedError(
                            f"clip {layer.clip_id} qualifier matte 后处理"
                            "（clean/blur/denoise）CPU 导出不支持——默认"
                            "拒绝导出（allow_degraded_grade=True 可显式"
                            "接受降级）")
                    logger.warning(
                        "Export color grade: clip %s qualifier matte 后处理"
                        "（clean/blur/denoise）CPU 导出不支持——按基础"
                        " keyer 输出（后处理段丢弃，显式降级）", layer.clip_id)
            except ExportGradeDegradedError:
                raise
            except Exception:  # noqa: BLE001 — keyer 失败：默认拒绝
                if not allow_degraded:
                    raise ExportGradeDegradedError(
                        f"clip {layer.clip_id} CPU qualifier keyer 失败——"
                        "默认拒绝导出（失败继续=无限 matte 整帧误调；"
                        "allow_degraded_grade=True 可显式接受降级）"
                    ) from None
                qualifier_matte = None
                logger.warning(
                    "Export color grade: clip %s CPU qualifier keyer 失败"
                    "——该段按无限定器输出（显式降级）", layer.clip_id,
                    exc_info=True)

        # N03：mask_shapes_cpu 导出消费——活动层的 mask 形状经
        # combine_masks_cpu 栅格化（解析近似），与 qualifier matte 同式
        # 限定整条 layer 调色（out = src*(1-m) + graded*m）。不可评估
        # 形状（curve/bitmap/brush）或组合为空 → 与降级硬门同口径
        # （默认拒绝，显式 allow_degraded_grade 才跳过）。
        mask_matte = None
        for entry in (grade.get("layers") or ()):
            if not entry.get("active"):
                continue
            shapes_raw = entry.get("mask_shapes")
            if not shapes_raw:
                break
            from src.features.color.core.layer import MaskGroup, MaskShape
            from src.features.color.core.mask_shapes_cpu import (
                combine_masks_cpu,
            )
            shapes = []
            for raw in shapes_raw:
                if isinstance(raw, (MaskShape, MaskGroup)):
                    shapes.append(raw)
                    continue
                if isinstance(raw, dict):
                    try:
                        shapes.append(MaskShape.from_dict(raw))
                        continue
                    except Exception as e:  # noqa: BLE001 — 形态异常按不支持
                        logger.debug(
                            "Export color grade: clip %s mask shape parse "
                            "failed (%s); treated as unsupported",
                            layer.clip_id, e)
            src_h_m, src_w_m = frame_data.shape[:2]
            mask_matte, mask_report = combine_masks_cpu(
                shapes, src_w_m, src_h_m)
            unsupported = bool(mask_report.unsupported) \
                or mask_report.empty
            if unsupported:
                if not allow_degraded:
                    raise ExportGradeDegradedError(
                        f"clip {layer.clip_id} 层 mask 含不可评估形状"
                        f"（{mask_report.unsupported or '空'}）——默认拒绝"
                        "导出（allow_degraded_grade=True 可显式接受跳过）")
                mask_matte = None
                logger.warning(
                    "Export color grade: clip %s mask 形状不可评估——"
                    "按无 mask 输出（显式降级）", layer.clip_id)
            break
        if mask_matte is not None:
            if qualifier_matte is not None:
                qualifier_matte = qualifier_matte * mask_matte
            else:
                qualifier_matte = mask_matte[..., None]  # (h,w,1) 权重

        try:
            import numpy as np
            from src.features.color.core.the_decompiler import GradingMath

            if frame_data.ndim != 3 or frame_data.shape[2] < 3:
                return frame_data
            if (frame_data.dtype == np.uint8
                    and frame_data.shape[2] == 3
                    and not grade.get("grade_keyframes")
                    # F4.1: 分解 LUT fast-path 只覆盖主参数——链段存在时
                    # 走全 float 顺序应用（否则组前/微调/组后被静默跳过）
                    and not (grade.get("chain_extra_params") or ())
                    # M06.2: qualifier matte 需要全 float 路径（fast-path
                    # 只覆盖主参数，会静默跳过限定器）
                    and not grade.get("qualifier_params")
                    # M05.2b: 曲线同样在 fast-path 覆盖域外
                    and not grade.get("curves_data")
                    # M05.2c: HSL 曲线/soft-clip 同理；曲线面板四参数
                    # 柔化裁切与 Edit 强度（恒等/默认时 grade 不携带）
                    and grade.get("hsl_curve_lut") is None
                    and not grade.get("soft_clip_params")
                    and not grade.get("curves_soft_clip")
                    and not grade.get("curves_edit")):
                import cv2

                decomposed = self._get_decomposed_grade_fastpath(
                    grading_params)
                if decomposed is not None:
                    lut, sat_matrix = decomposed
                    graded_u8 = (
                        cv2.LUT(frame_data, lut) if lut is not None
                        else frame_data)
                    if sat_matrix is not None:
                        graded_u8 = cv2.transform(graded_u8, sat_matrix)
                        graded_u8 = np.rint(
                            np.clip(graded_u8, 0.0, 255.0)
                        ).astype(np.uint8)
                    return graded_u8
            # BGR 8/16-bit → RGB float32 [0,1]。调色始终在 float 中运行，
            # 因而 10-bit 交付不会在 grade 前被量化到 8-bit。
            max_value = (
                float(np.iinfo(frame_data.dtype).max)
                if np.issubdtype(frame_data.dtype, np.integer) else 1.0
            )
            rgb = frame_data[..., :3][..., ::-1].astype(np.float32) / max_value
            # M05（S08）：链段按 _chain_segment 标签分侧——pre 段在主栈
            # **之前**，trim/post 段在主栈之后（正确顺序
            # pre→main→trim→post；非线性参数非交换，此前整列后置使
            # contrast35+exposure20 单像素最大误差 0.0439084）。未标记
            # 的载荷按"主栈之后"处理（与旧行为一致，不猜段身份）。
            extras = [
                e for e in (grade.get("chain_extra_params") or ())
                if isinstance(e, dict)]
            pre_entries = [
                e for e in extras if e.get("_chain_segment") == "pre"]
            after_entries = [
                e for e in extras if e.get("_chain_segment") != "pre"]

            def _apply_entries(buf, entries):
                for extra in entries:
                    if extra.get("_unsupported_advanced", False):
                        logger.warning(
                            "Export color chain: clip %s 链段含 curves/LUT"
                            " 等高级操作，primary 导出跳过该段",
                            layer.clip_id)
                        continue
                    buf = GradingMath.apply_grading(
                        buf.reshape(-1, 3), extra).reshape(rgb.shape)
                return buf

            graded = _apply_entries(rgb, pre_entries)
            graded = GradingMath.apply_grading(
                graded.reshape(-1, 3),
                grading_params,
            ).reshape(rgb.shape)
            # M05.2b：RGB 曲线（GPU 合成序中 curves 在主 grade 之后，
            # 见 shader_compiler build_composition_shader 的段序）
            curves_data = grade.get("curves_data")
            if curves_data:
                try:
                    from src.features.color.core.layer import (
                        build_curves_lut_from_data,
                    )
                    # Edit 强度参与 LUT 合成（与 Color 页 GPU 同一语义）
                    lut = build_curves_lut_from_data(
                        curves_data,
                        (grade.get("curves_edit") or {}).get("amounts"),
                    )  # (256,1,3)
                    flat = graded.reshape(-1, 3)
                    idx = np.clip(
                        (flat * 255.0).round().astype(np.int64), 0, 255)
                    flat = np.stack([
                        lut[idx[:, 0], 0, 0],
                        lut[idx[:, 1], 0, 1],
                        lut[idx[:, 2], 0, 2],
                    ], axis=-1).astype(np.float32)
                    graded = flat.reshape(rgb.shape)
                except Exception:  # noqa: BLE001 — 曲线失败按 dropped 告警
                    logger.warning(
                        "Export color grade: clip %s 曲线应用失败——该段"
                        "按无曲线输出", layer.clip_id, exc_info=True)
            # M05.2c：HSL 曲线（GPU 合成序中在 RGB curves 之后）
            hsl_curve_lut = grade.get("hsl_curve_lut")
            if hsl_curve_lut is not None:
                try:
                    from src.features.color.core.advanced_grade_cpu import (
                        apply_hsl_curves_image,
                    )
                    graded = apply_hsl_curves_image(graded, hsl_curve_lut)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Export color grade: clip %s HSL 曲线应用失败"
                        "——该段按无曲线输出", layer.clip_id, exc_info=True)
            # M05.2c：soft-clip（GPU 合成序中在 HSL curves 之后）
            soft_clip_params = grade.get("soft_clip_params")
            if soft_clip_params:
                try:
                    from src.features.color.core.advanced_grade_cpu import (
                        apply_soft_clip_image,
                    )
                    graded = apply_soft_clip_image(graded, soft_clip_params)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Export color grade: clip %s soft-clip 应用失败"
                        "——该段按无 soft-clip 输出", layer.clip_id,
                        exc_info=True)
            # 曲线面板四参数柔化裁切（指数软膝；GPU 合成序中在 legacy
            # soft-clip 之后、mixer 之前）
            curves_soft_clip_params = grade.get("curves_soft_clip")
            if curves_soft_clip_params:
                try:
                    from src.features.color.core.advanced_grade_cpu import (
                        apply_curves_soft_clip_image,
                    )
                    graded = apply_curves_soft_clip_image(
                        graded, curves_soft_clip_params)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Export color grade: clip %s 曲线柔化裁切应用失败"
                        "——该段按无曲线柔化裁切输出", layer.clip_id,
                        exc_info=True)
            # M05.2：3D LUT 文件 CPU 应用（GPU 合成序中在 soft-clip 之后，
            # lut_exporter 同一数学）。交付语义（AUD-18409/M05.2）：声明的
            # LUT 缺失/损坏 = 内容错误 → fail-fast，不静默缺效果产出。
            lut_path = grade.get("lut_path")
            if lut_path:
                from src.features.color.core.lut_exporter import (
                    apply_lut_file_to_color,
                )
                lut_applied = apply_lut_file_to_color(
                    graded, lut_path, grade.get("lut_intensity", 1.0))
                if lut_applied is None:
                    raise ValueError(
                        f"LUT 文件缺失或不可解析：{lut_path}"
                        f"（clip {layer.clip_id}）——导出失败")
                graded = lut_applied
            # F4.1: 调色链段顺序应用（与预览 CPU 同语义；非线性参数
            # 无代数合并，顺序执行即精确组合）。链段含 curves/LUT 等
            # 高级标记段时 primary 导出跳过并告警（不静默丢段）。
            graded = _apply_entries(graded, after_entries)
            # M06.2：qualifier matte 限定整条 layer 调色（GPU 合成同式
            # out = src*(1-m) + graded*m；matte 在源图上计算）
            if qualifier_matte is not None:
                from src.features.color.core.qualifier_cpu import (
                    apply_qualifier_masked_grade,
                )
                graded = apply_qualifier_masked_grade(
                    rgb.astype(np.float32), graded, qualifier_matte)
            # RGB float32 → 输入的工作位深
            result = frame_data.copy()
            if np.issubdtype(frame_data.dtype, np.floating):
                result[..., :3] = graded[..., ::-1]
            else:
                result[..., :3] = np.rint(
                    np.clip(graded[..., ::-1], 0.0, 1.0) * max_value
                ).astype(frame_data.dtype)
            return result
        except Exception as error:
            # AUD-18409：调色运行期失败 fail-fast——预览已可见的调色在交付
            # 中缺失属内容错误（.clinerules "不得静默缺效果"），未调色帧
            # 直接进入交付物比导出失败更糟。结构化异常向上传播，由
            # export_timeline 的统一异常路径转为 EXPORT_FAILED。
            logger.error(
                "Export color grade failed for clip %s: %s",
                layer.clip_id, error,
            )
            raise ValueError(
                f"color grade failed for clip {layer.clip_id}: {error}"
            ) from error

    def _get_independent_grade_lut(
        self,
        grading_params: Dict[str, Any],
        *,
        floating: bool = False,
    ) -> Optional[Any]:
        """Return a proven independent BGR LUT, or decline to the full grade.

        ``floating`` retains unclipped code values for a subsequent saturation
        stage. Otherwise the table has the final uint8 delivery quantization.
        Dark-protection independence is checked before shadows, using the same
        prefix evaluator as GradingMath rather than sampled final responses.
        """
        import numpy as np

        coupled_controls = ("highlights", "vibrance", "hue_shift", "saturation")
        try:
            if any(abs(float(grading_params.get(name, 0.0) or 0.0)) > 1e-6
                   for name in coupled_controls):
                return None

            key_parts = []
            for name, value in sorted(grading_params.items()):
                if isinstance(value, (list, tuple, np.ndarray)):
                    stable_value = tuple(float(item) for item in value)
                else:
                    stable_value = float(value)
                key_parts.append((str(name), stable_value))
            cache_key = (floating, tuple(key_parts))
        except (TypeError, ValueError, OverflowError):
            return None

        cache = getattr(self, "_color_grade_lut_cache", None)
        if cache is None:
            cache = {}
            self._color_grade_lut_cache = cache
        # RG01/N25：缓存值可能是 ndarray（LUT）或 None（已证明不可分
        # 解的结论复用）。ndarray 与字符串哨兵做 != 会触发 NumPy 的
        # 元素级比较再求真值，直接抛 ValueError；用成员判定区分"缺
        # 失"与"缓存了 None"两种语义。
        if cache_key in cache:
            return cache[cache_key]

        from src.features.color.core.the_decompiler import GradingMath

        code_values = np.arange(256, dtype=np.float32) / 255.0

        # The protection gate runs before shadows. Its bound must therefore
        # use the canonical pre-protection prefix, rather than a response
        # already changed by protection or shadow expansion.
        prefix = GradingMath.apply_grading_prefix(
            np.repeat(code_values[:, None], 3, axis=1), grading_params)
        if not bool(np.all(np.isfinite(prefix))):
            cache[cache_key] = None
            return None
        if float(prefix.min()) < 0.0:
            minima = prefix.astype(np.float64).min(axis=0)
            maxima = prefix.astype(np.float64).max(axis=0)
            weights = np.array([0.299, 0.587, 0.114])
            bound = float(minima @ weights)
            tolerance = 1e-6 * (1.0 + float(np.abs(prefix).max()))
            if bound < tolerance:
                # Even with negative luma the protection is an identity if
                # no dark pixel can have a channel below -0.5 or RGB spread
                # above 0.15. Positive luma weights bound each dark channel
                # from above using the other channels' independent minima.
                dark_maxima = np.minimum(
                    maxima, (tolerance - bound + minima * weights) / weights)
                protection_is_identity = (
                    float(minima.min()) >= -0.5 + tolerance
                    and float(dark_maxima.max() - minima.min())
                    <= 0.15 - tolerance)
                if not protection_is_identity:
                    cache[cache_key] = None
                    return None

        def _channel_responses(context_value: float) -> np.ndarray:
            responses = np.empty((256, 3), dtype=np.float32)  # RGB 域
            for rgb_channel in range(3):
                samples = np.full(
                    (256, 3), context_value, dtype=np.float32)
                samples[:, rgb_channel] = code_values
                graded = GradingMath.apply_grading(
                    samples, grading_params)
                responses[:, rgb_channel] = graded[:, rgb_channel]
            return responses

        # Black/white contexts are a canary for changes to the grade pipeline;
        # the domain proof above, not these samples, establishes independence.
        responses = _channel_responses(0.0)
        cross_check = _channel_responses(1.0)
        if not np.array_equal(responses, cross_check):
            cache[cache_key] = None
            return None
        if not bool(np.all(np.isfinite(responses))):
            cache[cache_key] = None
            return None
        # The protection is either inactive or an identity over every u8
        # combination. Remaining controls are independent by construction.
        lut = np.empty((256, 1, 3),
                       dtype=np.float32 if floating else np.uint8)
        # Input and output frames are BGR, while GradingMath consumes RGB.
        for bgr_channel, rgb_channel in enumerate((2, 1, 0)):
            values = responses[:, rgb_channel] * 255.0
            lut[:, 0, bgr_channel] = (
                values if floating else np.rint(
                    np.clip(values, 0.0, 255.0)).astype(np.uint8))

        # Canary（非证明）：混合通道网格上 LUT 查表必须与 GradingMath
        # 完全一致（0 偏差——同一 F 值、同一量化）。任何偏差都说明上
        # 述证明的工程假设被打破，立即退回 float。
        levels = np.array(
            [0, 1, 2, 3, 7, 16, 64, 128, 255], dtype=np.float32
        ) / 255.0
        probe = np.stack(np.meshgrid(
            levels, levels, levels, indexing="ij"
        ), axis=-1).reshape(-1, 3)
        reference = GradingMath.apply_grading(probe, grading_params) * 255.0
        if not floating:
            reference = np.rint(np.clip(reference, 0.0, 255.0)).astype(np.uint8)
        probe_idx = np.clip(
            np.rint(probe * 255.0).astype(np.int32), 0, 255
        )
        fast_rgb = np.empty_like(reference)
        for rgb_channel in range(3):
            fast_rgb[:, rgb_channel] = lut[:, 0, 2 - rgb_channel][
                probe_idx[:, rgb_channel]
            ]
        if not np.array_equal(fast_rgb, reference):
            cache[cache_key] = None
            return None

        cache[cache_key] = lut
        return lut

    def _get_decomposed_grade_fastpath(
        self,
        grading_params: Dict[str, Any],
    ) -> Optional[Tuple[Optional[Any], Optional[Any]]]:
        """Return ``(channel LUT, saturation matrix)`` fast path or None.

        2026-09-17（任务③）：``saturation`` 是唯一的常用跨通道耦合项，
        且是线性映射 ``res' = (1+s)·res − s·gray``（gray 用 Rec.601 权重）
        ——可与逐通道 LUT 分解组合：前缀（exposure/wb/contrast/lift/
        gain/gamma/shadows，全部逐通道）走 256 项 LUT，saturation 走
        3×3 矩阵（cv2.transform）。highlights/vibrance/hue_shift 仍是
        非线性跨通道 → 返回 None 走通用浮点路径。

        饱和度存在时保留浮点 LUT 中的负值及超白响应，矩阵之后才在
        调用端量化。前缀先截断到 u8 会丢失随后去饱和可恢复的高光。
        """
        import numpy as np

        nonlinear_coupled = ("highlights", "vibrance", "hue_shift")
        try:
            if any(abs(float(grading_params.get(name, 0.0) or 0.0)) > 1e-6
                   for name in nonlinear_coupled):
                return None
            saturation = float(grading_params.get("saturation", 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            return None

        prefix_params = dict(grading_params)
        prefix_params["saturation"] = 0
        lut = self._get_independent_grade_lut(
            prefix_params, floating=abs(saturation) > 1e-6)
        if lut is None:
            return None
        if abs(saturation) <= 1e-6:
            return lut, None

        # BGR 帧序：Rec.601 亮度权重 (0.299, 0.587, 0.114) 需反转为
        # (B, G, R) = (0.114, 0.587, 0.299)。行和恒为 (1+s) − s·Σw = 1。
        s = saturation / 100.0
        weights = (0.114, 0.587, 0.299)
        matrix = np.empty((3, 3), dtype=np.float32)
        for out_channel in range(3):
            for in_channel in range(3):
                matrix[out_channel, in_channel] = (
                    (1.0 + s)
                    * (1.0 if out_channel == in_channel else 0.0)
                    - s * weights[in_channel])
        return lut, matrix

    def _get_layer_frame(
        self,
        layer: "ClipLayerInfo",
    ) -> Optional["np.ndarray"]:
        """
        获取单个层的帧数据。

        对于 comp_clip 素材，解析到其 original_asset_id 以获取源素材帧，
        不依赖 Composite 页面 UI 纹理。

        Args:
            layer: ClipLayerInfo

        Returns:
            帧数据（BGR uint8），或 None
        """
        import numpy as np

        # Motion Graphic clips point to a shared TeachingSceneDocument and do
        # not have a MediaAsset path.
        from src.features.teaching_animation.host_adapters import motion_graphic_scene_id
        scene_id = motion_graphic_scene_id(layer.asset_id)
        if scene_id is not None:
            from src.features.teaching_animation.host_adapters import render_teaching_scene_frame
            from src.features.teaching_animation.core.contracts import RenderContext
            width, height = self._get_sequence_resolution()
            try:
                rgba = render_teaching_scene_frame(
                    scene_id,
                    int(layer.source_frame),
                    context=RenderContext(
                        fps=float(getattr(self._sequence, "fps", 24.0) or 24.0),
                        width=max(1, int(width)),
                        height=max(1, int(height)),
                        transparent=True,
                    ),
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("Teaching Animation export frame unavailable for %s: %s", scene_id, exc)
                return None
            return np.frombuffer(rgba.data, dtype=np.uint8).reshape((rgba.height, rgba.width, 4))[..., [2, 1, 0, 3]].copy()

        # B7: 占位符片段走专用静态帧（彩条+斜纹），绝不触发解码器。
        # 三路（CPU 预览 / CPU 导出 / GPU 预览经 CPU 帧路径）共用同一帧。
        try:
            from src.features.edit.core.placeholder_frame import (
                is_placeholder_asset,
                render_placeholder_frame_for_asset,
            )
            asset_for_ph = (
                self._project.get_asset_by_id(layer.asset_id)
                if getattr(self, "_project", None) is not None
                else None
            )
            if asset_for_ph is not None and is_placeholder_asset(asset_for_ph):
                return render_placeholder_frame_for_asset(asset_for_ph, 1920, 1080)
        except (ImportError, AttributeError):
            pass

        sequence = getattr(self, "_sequence", None)
        binding = (
            sequence.get_caption_graphic_binding_by_clip(layer.clip_id)
            if sequence is not None and hasattr(sequence, "get_caption_graphic_binding_by_clip")
            else None
        )
        if binding is not None and isinstance(getattr(binding, "caption_cue_id", None), str) and binding.caption_cue_id:
            if binding.composite_id:
                composite_asset = self._project.get_asset_by_id(layer.asset_id)
                composite_frame = self._render_comp_clip_frame(layer, composite_asset)
                if composite_frame is not None:
                    return composite_frame
                logger.warning(
                    "Caption graphic Composite render failed; falling back to template "
                    "binding=%s composite_id=%s",
                    binding.binding_id, binding.composite_id,
                )
            cue = sequence.get_subtitle_clip_by_id(binding.caption_cue_id)
            if cue is not None:
                from src.features.edit.core.captions.graphics import (
                    render_caption_graphic_rgba,
                    render_snapshot_for_binding,
                    template_parameters,
                )
                template = sequence.get_caption_graphic_template_registry().get(
                    binding.template_id, binding.template_version
                )
                if template is None:
                    logger.error(
                        "Caption graphic template missing during export: %s@%s",
                        binding.template_id, binding.template_version,
                    )
                    return None
                track = sequence.get_track_by_id(cue.track_id)
                width, height = self._get_sequence_resolution()
                rgba = render_caption_graphic_rgba(
                    cue, track, template,
                    template_parameters(template, binding.parameters),
                    width, height, layer.source_frame,
                    render_snapshot_for_binding(cue, binding),
                )
                if rgba is None:
                    return None
                rgb = np.clip(rgba[..., :3], 0.0, 1.0)
                alpha = np.clip(rgba[..., 3:4], 0.0, 1.0)
                # Always retain a straight BGRA layer here.  Output alpha only
                # controls the final encoder surface; the CPU compositor still
                # needs this alpha for an ordinary opaque export, otherwise a
                # transparent caption canvas becomes an opaque black layer.
                return np.rint(np.concatenate(
                    [rgb[..., ::-1], alpha], axis=-1) * 255.0
                ).astype(np.uint8)

        # 获取素材路径
        asset = self._project.get_asset_by_id(layer.asset_id)
        if asset is None:
            logger.warning(f"Asset not found: {layer.asset_id}")
            return None

        # P1-14: 导出取源走项目代理策略（默认强制 master 原片）
        asset_path = export_source_path(self._project, asset)
        asset_type = asset.media_type

        composite_id = getattr(layer, "composite_id", None)
        if isinstance(composite_id, str) and composite_id:
            comp_result = self._render_comp_clip_frame(layer, asset)
            if comp_result is not None:
                return comp_result
            logger.warning(
                "Export composite-bound clip render failed; falling back to original source "
                f"clip={layer.clip_id}, composite_id={layer.composite_id}"
            )

        if asset_type == "comp_clip":
            # Comp Clip: try to render through the Composite graph first,
            # fall back to original source asset if graph data is unavailable.
            comp_result = self._render_comp_clip_frame(layer, asset)
            if comp_result is not None:
                return comp_result

            # Fallback: resolve to original source asset
            logger.debug(
                f"Export comp_clip '{asset.asset_id}' composite render failed, "
                f"falling back to original source"
            )
            if asset.is_comp_clip:
                orig_asset_id = asset.metadata.get('original_asset_id', '')
                if orig_asset_id:
                    orig_asset = self._project.get_asset_by_id(orig_asset_id)
                    if orig_asset is None:
                        logger.warning(
                            f"Comp clip original asset not found: {orig_asset_id}"
                        )
                        return None
                    effective_orig_path = getattr(orig_asset, 'effective_path', None)
                    orig_path = (
                        effective_orig_path
                        if isinstance(effective_orig_path, str) and effective_orig_path
                        else orig_asset.path
                    )
                    orig_type = orig_asset.media_type
                    if orig_type == "video":
                        return self._get_video_frame(
                            orig_path, layer.source_frame, orig_asset_id,
                            session_key=self._layer_session_key(layer),
                            raw_params=getattr(
                                getattr(layer, 'clip', None), 'raw', None),
                        )
                    elif orig_type == "image":
                        return self._get_image_frame(orig_path)
                    else:
                        logger.warning(
                            f"Comp clip original asset has unsupported type: {orig_type}"
                        )
                        return None
                else:
                    # Empty comp clip (no source) - skip this layer
                    logger.debug(
                        f"Comp clip '{asset.asset_id}' has no original source, "
                        f"skipping layer"
                    )
                    return None
            else:
                logger.warning("Asset type is comp_clip but is_comp_clip is False")
                return None

        elif asset_type == "video":
            return self._get_video_frame(
                asset_path, layer.source_frame, layer.asset_id,
                session_key=self._layer_session_key(layer),
                raw_params=getattr(getattr(layer, 'clip', None), 'raw', None),
            )
        elif asset_type == "image":
            return self._get_image_frame(asset_path)
        elif asset_type == "nested_sequence":
            # P1-02：嵌套序列——解析子序列并复用导出合成管线递归求值
            # （防循环 + 子序列帧钳制与预览路径语义一致）。
            return self._render_nested_sequence_layer(layer)
        elif asset_type == "solid":
            # 纯色背景：用 metadata["color"]（RGB 0~1）生成单色帧（BGR uint8）。
            rgb = asset.solid_color or (0.0, 0.0, 0.0)
            w = int(getattr(self._sequence, "width", 1920) or 1920)
            h = int(getattr(self._sequence, "height", 1080) or 1080)
            bgr = (
                int(round(rgb[2] * 255)),
                int(round(rgb[1] * 255)),
                int(round(rgb[0] * 255)),
            )
            return np.full((h, w, 3), bgr, dtype=np.uint8)
        elif asset_type == "adjustment":
            # T-P1-22：adjustment 层无源帧是契约而非缺失——效果/调色在
            # _composite_layers 的 FX-C04 恢复段作用到已合成画面。此前
            # 落入 else 每层每帧打 "Unsupported asset type: adjustment"。
            return None
        else:
            logger.warning(f"Unsupported asset type: {asset_type}")
            return None

    def _render_nested_sequence_layer(
        self,
        layer: "ClipLayerInfo",
    ) -> Optional["np.ndarray"]:
        """P1-02: 嵌套序列层——递归复用导出合成管线渲染子序列帧。

        与预览渲染器 ``_render_nested_sequence_frame`` 语义一致：帧号钳制到
        子序列范围、渲染栈防循环；合成走本导出器自己的
        ``_composite_layers``（色彩/转场/特效处理与顶层帧完全同构）。
        """
        asset = self._project.get_asset_by_id(layer.asset_id)
        child_tid = (
            getattr(asset, "nested_timeline_id", None) if asset is not None else None
        )
        if not child_tid:
            return None
        if child_tid in self._nested_render_stack:
            logger.warning("Export: circular nested sequence %s skipped", child_tid)
            return None
        if self._sequence_resolver is None:
            logger.warning(
                "Export: nested sequence %s has no sequence resolver; "
                "pass timeline_sequences to the exporter factory", child_tid,
            )
            return None
        child_seq = self._sequence_resolver(child_tid)
        if child_seq is None:
            logger.warning("Export: nested sequence %s unresolvable", child_tid)
            return None

        source_frame = int(layer.source_frame)
        duration = int(getattr(child_seq, "duration_frames", 0) or 0)
        if duration > 0:
            source_frame = max(0, min(source_frame, duration - 1))
        else:
            source_frame = max(0, source_frame)

        from src.features.edit.core.timeline_render_plan import TimelineRenderPlan
        plan = TimelineRenderPlan(child_seq)
        frame_plan = plan.get_frame_plan(source_frame)
        if not frame_plan.has_content:
            return None

        config = self._active_export_config or TimelineExportConfig()
        self._nested_render_stack.add(child_tid)
        try:
            return self._composite_layers(
                frame_plan.layers, frame_plan.transitions, config, source_frame
            )
        finally:
            self._nested_render_stack.discard(child_tid)

    def _export_session_cap(self) -> int:
        """导出视频解码会话的 LRU 上限（防多 clip 时间线内存失控）。"""
        try:
            return max(4, int(os.environ.get("TOPOS_EXPORT_SESSION_CACHE", "12")))
        except (TypeError, ValueError):
            return 12

    def _open_export_video_session(
        self, asset_path: str, session_key: str,
    ) -> Optional[str]:
        """打开/复用导出专用视频会话（clip 级解码游标 + LRU 上限）。

        会话按 session_key（默认 clip_id）分流：同一素材出现在多个
        timeline clip（多轨同用、转场两侧）时各自持有独立游标，避免
        asset 级共享游标在轨间来回 seek。会话数超上限时关闭最久未用的
        会话——请求方总是显式给 source_frame，被逐出后重新打开只多一次
        定位，不影响正确性。
        """
        existing = self._open_clips.get(session_key)
        if existing is not None:
            move_to_end = getattr(self._open_clips, "move_to_end", None)
            if callable(move_to_end):
                move_to_end(session_key)
            return existing
        try:
            # share_by_path=False：导出走独立 ClipSession，避免与
            # Color/Composite 播放共享缓存（导出大量 master 帧会撑爆播放缓存）
            clip_id = self._frame_provider.open_clip(
                asset_path, share_by_path=False,
            )
        except Exception as first_error:
            # 首开瞬时失败重试一次（实测：陈旧原生 codec 库会毒化进程内
            # 首次 open，第二次加载走回退即成功——失败被丢弃即表现为
            # 导出首帧黑帧）。立即重试成功则继续；仍失败才放弃本帧。
            try:
                clip_id = self._frame_provider.open_clip(
                    asset_path, share_by_path=False,
                )
                logger.warning(
                    "Export clip open retry succeeded for %s "
                    "(first failure: %s)", asset_path, first_error,
                )
            except Exception as e:
                logger.error(f"Failed to open clip {asset_path}: {e}")
                return None
        self._open_clips[session_key] = clip_id
        cap = self._export_session_cap()
        while len(self._open_clips) > cap:
            stale_key, stale_clip_id = next(iter(self._open_clips.items()))
            self._open_clips.pop(stale_key)
            try:
                self._frame_provider.close_clip(stale_clip_id)
                logger.debug(
                    "Evicted LRU export clip session %s (%s)",
                    stale_key, stale_clip_id,
                )
            except Exception as exc:
                logger.warning(f"Failed to close clip {stale_clip_id}: {exc}")
        logger.debug(
            f"Opened clip (export session): {asset_path} -> {clip_id} "
            f"(key={session_key})"
        )
        return clip_id

    @staticmethod
    def _layer_session_key(layer: Any) -> str:
        """导出解码会话键：优先 timeline clip 身份（同素材多 clip 分流）。"""
        return str(getattr(layer, "clip_id", "") or "") or str(
            getattr(layer, "asset_id", "") or ""
        )

    def _request_video_decoded_frame(
        self,
        asset_path: str,
        source_frame: int,
        asset_id: str,
        session_key: str = "",
    ) -> Optional[Any]:
        """打开/复用导出专用 clip 会话并取 master 帧（不做像素转换）。

        P0-GPU-1: 从 _get_video_frame 拆出，供 GPU 合成路径消费原始
        planar 帧（省去 CPU YUV→BGR 提取）；会话缓存与 CPU 路径共享，
        GPU 不适用回落时无需重复打开。
        """
        # 确保 clip 已打开
        clip_id = self._open_export_video_session(
            asset_path, session_key or asset_id,
        )
        if clip_id is None:
            return None

        # 获取帧（master 质量）
        try:
            decode_started = time.perf_counter()
            decoded_frame = self._frame_provider.request_frame(
                clip_id,
                source_frame,
                quality="master"
            )
            # T-P1-21 批次二：GL/多层路径此前不采样 decode 阶段——
            # telemetry 出现 stage_missing:decode，矩阵无法归因取帧成本。
            self._record_gpu_stage(
                "decode",
                "provider_session",
                wall_ms=(time.perf_counter() - decode_started) * 1000.0,
            )
            if decoded_frame is None:
                logger.warning(f"Failed to get frame {source_frame} from {asset_path}")
            return decoded_frame
        except Exception as e:
            logger.error(f"Failed to request frame {source_frame}: {e}")
            return None

    def _request_video_metal_frame(
        self,
        asset_path: str,
        source_frame: int,
        asset_id: str,
        session_key: str = "",
    ) -> Optional[Any]:
        """Request a VideoToolbox MTLTexture frame for Metal timeline export."""
        clip_id = self._open_export_video_session(
            asset_path, session_key or asset_id,
        )
        if clip_id is None:
            return None
        self._wait_for_metal_prefetch(clip_id)
        request_metal = getattr(self._frame_provider, "request_metal_frame", None)
        if not callable(request_metal):
            return self._request_video_decoded_frame(
                asset_path, source_frame, asset_id)
        decode_started = time.perf_counter()
        try:
            decoded = request_metal(
                clip_id, source_frame, quality="master")
            decode_wall_ms = (time.perf_counter() - decode_started) * 1000.0
            if decoded is not None:
                from src.shared.media.gpu_frame import GpuDecodedFrame
                if isinstance(decoded, GpuDecodedFrame):
                    self._record_gpu_stage(
                        "decode",
                        str(getattr(decoded, "source_kind", "videotoolbox")),
                        wall_ms=decode_wall_ms,
                    )
                    self._schedule_metal_prefetch(
                        clip_id, int(source_frame) + 1)
                else:
                    self._record_gpu_stage("decode", "cpu_fallback",
                                           wall_ms=decode_wall_ms)
                    self._record_gpu_fallback("metal_frame_not_available")
            if decoded is None:
                logger.warning(
                    "Failed to get Metal export frame %s from %s",
                    source_frame, asset_path,
                )
                self._record_gpu_stage("decode", "cpu_fallback",
                                       wall_ms=decode_wall_ms)
                self._record_gpu_fallback("metal_frame_not_available")
            return decoded
        except Exception as error:
            logger.error(
                "Failed to request Metal frame %s from %s: %s",
                source_frame, asset_path, error,
            )
            self._record_gpu_stage(
                "decode", "cpu_fallback",
                wall_ms=(time.perf_counter() - decode_started) * 1000.0)
            self._record_gpu_fallback("metal_frame_request_error")
            return None

    def _wait_for_metal_prefetch(self, clip_id: str) -> None:
        """Join queued read-ahead for this clip before issuing its next read."""
        pending = [
            (key, future) for key, future in
            getattr(self, "_metal_prefetch_futures", {}).items()
            if key[0] == clip_id
        ]
        for key, future in pending:
            self._metal_prefetch_futures.pop(key, None)
            try:
                future.result()
            except Exception as error:  # noqa: BLE001 — regular read can recover
                logger.debug("Metal VT prefetch failed clip=%s: %s", clip_id, error)

    def _schedule_metal_prefetch(self, clip_id: str, frame_index: int) -> None:
        """Start a bounded one-frame VT read-ahead for Metal timeline export."""
        if not getattr(self, "_metal_prefetch_enabled", False):
            return
        prefetch = getattr(self._frame_provider, "prefetch_vt_sequential", None)
        if not callable(prefetch):
            return
        executor = getattr(self, "_metal_prefetch_executor", None)
        if executor is None:
            executor = ThreadPoolExecutor(
                # Each decoder session has its own vt_lock/decode_lock. Allow
                # independent active tracks to prefetch concurrently; the
                # session locks still serialize seeks within one source.
                max_workers=4, thread_name_prefix="TimelineMetalPrefetch")
            self._metal_prefetch_executor = executor
        key = (clip_id, frame_index)
        if key not in self._metal_prefetch_futures:
            self._metal_prefetch_futures[key] = executor.submit(
                prefetch, clip_id, frame_index)

    def _get_video_frame(
        self,
        asset_path: str,
        source_frame: int,
        asset_id: str,
        session_key: str = "",
        raw_params: Any = None,
    ) -> Optional["np.ndarray"]:
        """
        获取视频帧。

        Args:
            asset_path: 视频文件路径
            source_frame: 源帧号
            asset_id: 素材 ID
            session_key: 解码会话键（默认按 asset_id 共享游标）
            raw_params: clip RawParams（CFA debayer 用户参数——导出与
                预览同优先级；非 CFA 格式忽略）

        Returns:
            帧数据（BGR uint8），或 None
        """
        decoded_frame = self._request_video_decoded_frame(
            asset_path, source_frame, asset_id,
            session_key=session_key or asset_id)
        if decoded_frame is None:
            return None

        try:
            pixel_format = str(getattr(decoded_frame, "pixel_format", "") or "")
            # 复验 P0-03：Topos planar 素材（TPIC 中间片再进时间线）直通——
            # FFmpeg 无法解码 TPIC，此前每帧仍尝试 FFmpegMediaSource 打开
            # （失败不缓存 → 逐帧重试 + 异常日志），随后落入忽略 range/matrix
            # 的低深度 fallback。现在 provider 已给出精确平面，直接走
            # 平面→BGR 转换（高精度导出 = 16-bit 路径，位深/range/matrix
            # 全部按帧声明消费）。TRAW CFA 同走直通（FFmpeg 双重试无效，
            # debayer 高低精度在 _debayer_cfa_to_bgr 内自决）。
            if _is_topos_planar_format(pixel_format) or 'cfa' in pixel_format.lower():
                return self._finish_source_frame(decoded_frame,
                                                 raw_params=raw_params)
            is_high_depth_decode = (
                self._pixel_format_is_high_depth(pixel_format)
                or getattr(getattr(decoded_frame, "data", None), "dtype", None) is not None
                and getattr(decoded_frame.data.dtype, "itemsize", 1) > 1
            )
            if is_high_depth_decode:
                if getattr(self, "_use_high_precision_export", False):
                    high_depth_frame = self._get_export_high_depth_frame(
                        asset_path, source_frame, asset_id,
                        session_key=session_key or asset_id,
                    )
                    if high_depth_frame is not None:
                        return high_depth_frame
                rgb8_frame = self._get_export_rgb8_frame(
                    asset_path, source_frame, asset_id,
                    session_key=session_key or asset_id,
                )
                if rgb8_frame is not None:
                    return rgb8_frame

            # 提取帧数据
            return self._finish_source_frame(decoded_frame,
                                             raw_params=raw_params)

        except Exception as e:
            logger.error(f"Failed to request frame {source_frame}: {e}")
            return None

    def _finish_source_frame(self, decoded_frame: Any,
                             raw_params: Any = None) -> Optional["np.ndarray"]:
        """提取帧数据 + premultiplied 语义归一（复验 P0-04）。

        Topos premultiplied 源的 RGB 已乘 alpha——CPU 合成按 straight 消费，
        必须先 unpremultiply；此前 `_extract_frame_data` 只返回裸 ndarray，
        标志在 `DecodedFrame.extra` 里被丢弃，透明边缘出现黑/亮边。
        """
        keep_alpha = bool(getattr(self, "_export_has_alpha", False))
        data = self._extract_frame_data(decoded_frame, keep_alpha=keep_alpha,
                                        raw_params=raw_params)
        if data is None or data.ndim != 3 or data.shape[-1] != 4:
            return data
        extra = getattr(decoded_frame, "extra", None) or {}
        if not bool(extra.get("topos_alpha_premultiplied", False)):
            return data
        import numpy as np

        scale = float(np.iinfo(data.dtype).max) \
            if np.issubdtype(data.dtype, np.integer) else 1.0
        f = data.astype(np.float32) / scale
        straight = self._unpremultiply_rgba(f)
        if np.issubdtype(data.dtype, np.integer):
            return np.rint(
                np.clip(straight, 0.0, 1.0) * scale).astype(data.dtype)
        return straight

    def _request_high_depth_decoded_frame(
        self, asset_path: str, source_frame: int, asset_id: str,
        session_key: str = "",
    ) -> Optional[Any]:
        """高精度导出的原生 YUV 平面解码源（会话缓存），GPU/CPU 共用。

        P0-GPU-2: 从 _get_export_high_depth_frame 拆出解码部分，供 GPU
        合成直接消费 planar 帧（省去 CPU bgr16 转换）；失败语义与原实现
        一致（告警 + None）。会话键与视频主会话一致（clip 级分流）。
        """
        source_key = session_key or asset_id
        source = self._export_high_depth_sources.get(source_key)
        if source is None:
            # T-P0-10：TOPOS (TPIC) 文件没有 FFmpeg 可解码的流——高位深直连
            # 的 FFmpegMediaSource 必定失败（无效 probe）。分流回主渲染路径
            # （provider → factory auto → ToposMediaSource）。
            try:
                from src.shared.media.topos_source import is_topos_movie
                if is_topos_movie(asset_path):
                    logger.debug(
                        "High-depth direct decode skipped for TOPOS asset "
                        "%s (native source handles the main path)",
                        asset_path,
                    )
                    return None
            except Exception:  # noqa: BLE001 — 嗅探失败按原路径处理
                pass
            try:
                from src.shared.media.ffmpeg_source import (
                    FFmpegMediaSource,
                    _is_intra_codec,
                )

                source = FFmpegMediaSource(
                    asset_path, preserve_planes=True, output_mode="planar_yuv",
                )
                source.open()
                encoder_config = getattr(
                    getattr(self, "_encoder", None), "_config", None)
                if (str(getattr(encoder_config, "codec", "")).lower()
                        == "topos"):
                    _encode_threads, decode_threads = \
                        _topos_timeline_cpu_threads()
                    stream = getattr(source, "_video_stream", None)
                    codec_context = getattr(stream, "codec_context", None)
                    codec_name = str(
                        getattr(codec_context, "name", "") or "").lower()
                    if (decode_threads is not None
                            and codec_context is not None
                            and _is_intra_codec(codec_name)):
                        try:
                            codec_context.thread_count = decode_threads
                        except Exception as exc:
                            logger.debug(
                                "Could not balance TOPOS timeline decoder pool: %s",
                                exc,
                            )
                self._export_high_depth_sources[source_key] = source
            except Exception as exc:
                logger.warning(
                    "Failed to open high-depth export decode source for %s: %s",
                    asset_path, exc,
                )
                return None
        try:
            decoded = source.read_frame(source_frame)
            if decoded is None:
                return None
            if len(getattr(decoded, "planes", None) or ()) < 3:
                return None
            return decoded
        except Exception as exc:
            logger.warning(
                "Failed to decode high-depth export frame %s from %s: %s",
                source_frame, asset_path, exc,
            )
            return None

    def _get_export_high_depth_frame(
        self, asset_path: str, source_frame: int, asset_id: str,
        session_key: str = "",
    ) -> Optional["np.ndarray"]:
        """Decode a source as native YUV planes and return full-range BGR16.

        PyAV's direct YUV→rgb48 reformat is unreliable for some 10-bit HEVC
        builds (it caused the magenta export regression).  Reading planes
        preserves every source code value, then this exporter applies the
        documented Y'CbCr matrix/range conversion itself.  This method is
        deliberately export-only: previews may keep their faster 8-bit path.
        """
        decoded = self._request_high_depth_decoded_frame(
            asset_path, source_frame, asset_id,
            session_key=session_key or asset_id)
        if decoded is None:
            return None
        return self._convert_yuv_planes_to_bgr16(
            getattr(decoded, "planes", None) or (),
            getattr(decoded, "pixel_format", ""),
            getattr(decoded, "metadata", None),
        )

    @staticmethod
    def _bit_depth_for_pixel_format(pixel_format: str, dtype: Any) -> int:
        """Infer usable sample depth from a planar FFmpeg/Topos pixel format.

        复验 P0-03：只认主位深（``p10``/``p12`` token），alpha 后缀
        ``a8/a10/a12/a16`` 不再被误认成颜色位深（此前 'a16' 会命中
        子串 '16' → 16-bit，把 10-bit 颜色平面按 65535 归一化压暗 64×）。
        """
        lower = (pixel_format or "").lower()
        m = _TOPOS_COLOR_DEPTH_RE.search(lower)
        if m:
            return int(m.group(1))
        for depth in (16, 14, 12, 10, 9):
            if f"p{depth}" in lower or f"{depth}le" in lower or f"{depth}be" in lower:
                return depth
        try:
            import numpy as np
            return int(np.dtype(dtype).itemsize * 8)
        except Exception:
            return 8

    def _planes_to_bgr_float(
        self,
        planes: tuple,
        pixel_format: str,
        metadata: Any = None,
        plane_infos: Any = None,
        keep_alpha: bool = False,
    ) -> Optional["np.ndarray"]:
        """planar YUV/GBR → float32 [0,1] BGR(A)（位深/range/matrix 全按声明）。

        复验 P0-03 统一核：
        - 逐平面位深以 PlaneInfo 为权威（alpha 独立），缺省回退格式名/容器；
          降 8-bit 用位深感知缩放（此前按 dtype 宽度右移，10-bit 满刻度
          2056 被压成 8）；
        - YUV 消费 H.273 range/matrix 元数据（此前低深度 fallback 用通用
          OpenCV 全范围转换，limited-range 10-bit 源系统性偏灰）；
        - GBR 平面序 = (G,B,R)（v1.4 恒等契约），直接重排不走红/蓝差。
        """
        try:
            import cv2
            import numpy as np

            y_plane, u_plane, v_plane = planes[0], planes[1], planes[2]
            if y_plane.ndim != 2 or u_plane.ndim != 2 or v_plane.ndim != 2:
                return None
            height, width = y_plane.shape

            fmt_lower = (pixel_format or "").lower()
            is_gbr = 'gbr' in fmt_lower
            fmt_color_depth: Optional[int] = None
            fmt_alpha_depth: Optional[int] = None
            if _is_topos_planar_format(fmt_lower):
                fmt_color_depth, fmt_alpha_depth = _topos_planar_depths(fmt_lower)

            def _plane_depth(i: int, plane: "np.ndarray") -> int:
                pis = plane_infos or ()
                if i < len(pis):
                    bd = int(getattr(pis[i], "bit_depth", 0) or 0)
                    if bd > 0:
                        return bd
                if i == 3 and fmt_alpha_depth:
                    return fmt_alpha_depth
                if fmt_color_depth:
                    return fmt_color_depth
                return self._bit_depth_for_pixel_format(fmt_lower, plane.dtype)

            def _norm(plane: "np.ndarray", depth: int) -> "np.ndarray":
                code_max = float((1 << depth) - 1)
                return plane.astype(np.float32) / code_max

            if is_gbr:
                # G,B,R 平面（v1.4）：满刻度直读，无 range/matrix
                g = _norm(y_plane, _plane_depth(0, y_plane))
                b = _norm(u_plane, _plane_depth(1, u_plane))
                r = _norm(v_plane, _plane_depth(2, v_plane))
                if u_plane.shape != (height, width):
                    b = cv2.resize(b, (width, height),
                                   interpolation=cv2.INTER_LINEAR)
                if v_plane.shape != (height, width):
                    r = cv2.resize(r, (width, height),
                                   interpolation=cv2.INTER_LINEAR)
                bgr = np.dstack((b, g, r)).astype(np.float32)
                color_range = "full"
            else:
                yd = _plane_depth(0, y_plane)
                cmax = float((1 << yd) - 1)
                scale_8 = float(1 << max(0, yd - 8))
                u_up = cv2.resize(u_plane, (width, height),
                                  interpolation=cv2.INTER_LINEAR)
                v_up = cv2.resize(v_plane, (width, height),
                                  interpolation=cv2.INTER_LINEAR)
                color_range = str(
                    getattr(metadata, "color_range", "limited") or "limited"
                ).lower()
                if color_range == "full":
                    y = y_plane.astype(np.float32) / cmax
                    cb = (u_up.astype(np.float32) - cmax * 0.5) / cmax
                    cr = (v_up.astype(np.float32) - cmax * 0.5) / cmax
                else:
                    y = (y_plane.astype(np.float32) - 16.0 * scale_8) / (219.0 * scale_8)
                    cb = (u_up.astype(np.float32) - 128.0 * scale_8) / (224.0 * scale_8)
                    cr = (v_up.astype(np.float32) - 128.0 * scale_8) / (224.0 * scale_8)
                matrix = str(
                    getattr(metadata, "color_matrix", "bt709") or "bt709"
                ).lower()
                if matrix in {"bt2020nc", "bt2020c", "bt2020"}:
                    kr, kb = 0.2627, 0.0593
                elif matrix in {"smpte170m", "bt470bg", "bt470m", "fcc"}:
                    kr, kb = 0.2990, 0.1140
                else:
                    kr, kb = 0.2126, 0.0722
                kg = 1.0 - kr - kb
                red = y + 2.0 * (1.0 - kr) * cr
                blue = y + 2.0 * (1.0 - kb) * cb
                green = y - 2.0 * kb * (1.0 - kb) / kg * cb \
                    - 2.0 * kr * (1.0 - kr) / kg * cr
                bgr = np.clip(np.dstack((blue, green, red)), 0.0, 1.0)

            if keep_alpha and len(planes) >= 4 and planes[3].ndim == 2:
                a_up = planes[3]
                if a_up.shape != (height, width):
                    a_up = cv2.resize(a_up, (width, height),
                                      interpolation=cv2.INTER_LINEAR)
                # alpha 独立位深满刻度（此前用颜色 code_max——a8 满刻度 255
                # 在 12-bit 颜色下被归一化成 255/4095 ≈ 0.062）
                a = np.clip(
                    _norm(a_up, _plane_depth(3, a_up)), 0.0, 1.0)
                bgr = np.concatenate([bgr, a[..., None]], axis=-1)
            return bgr.astype(np.float32)
        except Exception as exc:
            logger.warning("Planar YUV/GBR conversion failed: %s", exc)
            return None

    def _convert_yuv_planes_to_bgr16(
        self,
        planes: tuple,
        pixel_format: str,
        metadata: Any = None,
        plane_infos: Any = None,
        keep_alpha: Optional[bool] = None,
    ) -> Optional["np.ndarray"]:
        """Convert planar Y'CbCr/GBR to full-range BGR uint16 without 8-bit loss."""
        ka = bool(getattr(self, "_export_has_alpha", False)) \
            if keep_alpha is None else bool(keep_alpha)
        bgr = self._planes_to_bgr_float(
            planes, pixel_format, metadata, plane_infos, ka)
        if bgr is None:
            return None
        import numpy as np
        return np.rint(np.clip(bgr, 0.0, 1.0) * 65535.0).astype(np.uint16)

    def _get_export_rgb8_frame(
        self, asset_path: str, source_frame: int, asset_id: str,
        session_key: str = "",
    ) -> Optional["np.ndarray"]:
        """Decode a high-bit-depth asset through FFmpeg's reliable rgb24 path.

        High-bit-depth playback may legitimately retain ``rgb48le`` for GPU
        processing. CPU timeline export, however, feeds 8-bit BGR frames to
        its compositor. A dedicated export source keeps that conversion local
        to export and avoids changing the HDR/preview decode contract.
        """
        source_key = session_key or asset_id
        source = self._export_rgb8_sources.get(source_key)
        if source is None:
            try:
                from src.shared.media.ffmpeg_source import FFmpegMediaSource

                source = FFmpegMediaSource(asset_path, output_format="rgb24")
                source.open()
                self._export_rgb8_sources[source_key] = source
            except Exception as exc:
                logger.warning(
                    "Failed to open 8-bit export decode source for %s: %s",
                    asset_path, exc,
                )
                return None
        try:
            decoded = source.read_frame(source_frame)
            if decoded is not None:
                return self._extract_frame_data(
                    decoded,
                    keep_alpha=bool(getattr(self, "_export_has_alpha", False)),
                )
            return None
        except Exception as exc:
            logger.warning(
                "Failed to decode 8-bit export frame %s from %s: %s",
                source_frame, asset_path, exc,
            )
            return None

    _IMAGE_FRAME_CACHE_MAX = 8

    def _get_image_frame(
        self,
        asset_path: str,
    ) -> Optional["np.ndarray"]:
        """
        获取图片帧。

        AUD-18408：静态图层导出按导出器级 LRU 缓存解码帧——此前每个
        导出帧都对同一图片完整磁盘 imread+解码（转场预合成每帧还取左右
        两帧），长导出为同一张图做数千次重复 IO。命中返回缓存主本的
        ``copy()``（下游单层快速路径会原样交付该数组，绝不交出缓存本体，
        避免任何原地写污染后续帧）。缓存与 ``_open_clips`` 同生命周期，
        ``_cleanup_clips`` 释放；``__new__`` 直构的测试实例经 setdefault
        兜底。

        Args:
            asset_path: 图片文件路径

        Returns:
            帧数据（BGR uint8），或 None
        """
        alpha_mode = bool(getattr(self, "_export_has_alpha", False))
        cache_key = (str(asset_path), alpha_mode)
        cache: "OrderedDict[tuple[str, bool], np.ndarray]" = (
            self.__dict__.setdefault("_image_frame_cache", OrderedDict()))
        cached = cache.get(cache_key)
        if cached is not None:
            cache.move_to_end(cache_key)
            return cached.copy()
        try:
            import cv2
            import numpy as np
            # R2（P0-1）：Alpha 导出保留图片第 4 通道（cv2 原生 BGR 顺序 →
            # 直接得到 BGRA）；灰度/灰度+alpha 布局归一到 BGR/BGRA
            if alpha_mode:
                img = cv2.imread(asset_path, cv2.IMREAD_UNCHANGED)
                if img is not None:
                    if img.ndim == 2:
                        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
                    elif img.ndim == 3 and img.shape[2] == 2:
                        gray = np.repeat(img[:, :, :1], 3, axis=2)
                        img = np.concatenate([gray, img[:, :, 1:2]], axis=-1)
                    elif img.ndim == 3 and img.shape[2] == 1:
                        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
                if img is not None:
                    cache[cache_key] = img
                    while len(cache) > self._IMAGE_FRAME_CACHE_MAX:
                        cache.popitem(last=False)
                return img.copy() if img is not None else None
            # 使用 cv2.imread 加载图片
            img = cv2.imread(asset_path, cv2.IMREAD_COLOR)
            if img is None:
                logger.warning(f"Failed to load image {asset_path}")
                return None
            cache[cache_key] = img
            while len(cache) > self._IMAGE_FRAME_CACHE_MAX:
                cache.popitem(last=False)
            return img.copy()
        except Exception as e:
            logger.error(f"Failed to load image {asset_path}: {e}")
            return None

    def _debayer_cfa_to_bgr(
        self,
        decoded_frame: "DecodedFrame",
        planes,
        pixel_format: str,
        raw_params: Any = None,
    ) -> Optional["np.ndarray"]:
        """RC6：TRAW CFA 相位平面 → debayer → 工作空间 BGR（交付出口）。

        - 位深：plane_infos[0].bit_depth 优先，格式名兜底（cfa16→16）；
        - RAW 参数：三链同源解析（2026-09-21 复查收口）——用户 clip
          RawParams（面板 WB/EI/黑位/色彩空间/伽马）> 帧 TRWM as-shot
          基线 > 单位默认，与 GPU 预览/CPU 回退一致
          （camera_gamut_matrices.resolve_debayer_params）；
        - IDT gamut：用户 color_space > TRWM sensor_matrix_id → 预设
          （camera-native linear → BT.709 linear 工作空间），未知相机
          identity 并记一次日志（计划 RC2：不静默猜）；
        - 高精度导出（_use_high_precision_export）输出 16-bit BGR，
          否则 8-bit。
        """
        import numpy as np
        from src.shared.video.camera_gamut_matrices import resolve_debayer_params
        from src.shared.video.cfa_cpu_debayer import debayer_cfa_planes

        if planes is None or len(planes) != 4:
            logger.warning(
                "CFA export: expected 4 phase planes, got %s",
                None if planes is None else len(planes))
            return None
        infos = getattr(decoded_frame, 'plane_infos', None) or ()
        bit_depth = (
            int(getattr(infos[0], 'bit_depth', 0) or 0) if len(infos) else 0
        ) or (16 if 'cfa16' in str(pixel_format) else 12)
        fw = int(getattr(decoded_frame, 'width', 0) or 0)
        fh = int(getattr(decoded_frame, 'height', 0) or 0)
        if not fw or not fh:
            logger.warning("CFA export: frame size unknown")
            return None
        trwm = (getattr(decoded_frame, 'extra', None) or {}).get(
            'topos_trwm') or {}
        rp = resolve_debayer_params(raw_params, trwm)
        if not rp['explicit_gamut'] and not getattr(
                self, "_cfa_export_unknown_logged", False):
            logger.info(
                "TRAW CFA 导出：源未携带相机色空间（无 RAW 面板选择且 "
                "TRWM 无 sensor_matrix_id）——按 camera-native identity 出图")
            self._cfa_export_unknown_logged = True
        rgb = debayer_cfa_planes(
            planes, fw, fh, bit_depth=bit_depth,
            wb_gains=rp['wb_gains'],
            exposure_stops=rp['exposure_stops'],
            black_level=rp['black_level'],
            gamma_mode=rp['gamma_mode'],
            gamut_matrix=rp['matrix'])
        bgr = np.ascontiguousarray(rgb[..., ::-1])
        if getattr(self, "_use_high_precision_export", False):
            return np.rint(np.clip(bgr, 0.0, 1.0) * 65535.0).astype(np.uint16)
        return (np.clip(bgr, 0.0, 1.0) * 255.0).astype(np.uint8)

    def _extract_frame_data(
        self,
        decoded_frame: "DecodedFrame",
        keep_alpha: bool = False,
        raw_params: Any = None,
    ) -> Optional["np.ndarray"]:
        """
        从 DecodedFrame 提取 numpy 数组（BGR uint8）。

        处理打包格式（RGB/BGR/RGBA/BGRA）和平面格式（YUV420 等）。

        Args:
            decoded_frame: DecodedFrame 实例
            keep_alpha: R2（P0-1）——保留第 4 通道（输出 BGRA/BGR + A）；
                False 时保持既有 3 通道行为（丢弃 alpha）
            raw_params: clip RawParams（CFA debayer 用户参数——用户
                WB/EI/黑位/色彩空间/伽马随导出生效，与预览一致；
                None = 帧 TRWM as-shot 基线）

        Returns:
            numpy.ndarray (H, W, 3) BGR uint8 或 (H, W, 4) BGRA uint8，或 None
        """
        try:
            import numpy as np
            data = decoded_frame.data
            pixel_format = getattr(decoded_frame, 'pixel_format', 'bgr24')

            # ── 平面 YUV/GBR 格式（planes 非空）──
            planes = getattr(decoded_frame, 'planes', None) or ()
            if data is None and planes and len(planes) >= 3:
                # RAW/CFA（TRAW 相位平面）：RC6 CFA 交付出口——CPU debayer
                # → IDT gamut（用户 color_space > TRWM sensor_matrix_id /
                # camera_gamut_matrices 预设）→ 工作空间 BGR，与 R2 的
                # RAW→RAW 直出构成双路径（再编码档 / 保持 RAW）。此前显式
                # 拒绝（2026-09-13 复查：拒绝优于 YUV 乱渲静默垃圾）；
                # debayer 链就绪后升级为正常交付。查看伽马与预览同语义。
                if 'cfa' in str(pixel_format or '').lower():
                    return self._debayer_cfa_to_bgr(
                        decoded_frame, planes, pixel_format,
                        raw_params=raw_params)
                keep_alpha_planes = keep_alpha and len(planes) >= 4
                if getattr(self, "_use_high_precision_export", False):
                    # 复验 P0-03：Topos planar 高精度导出走 16-bit 路径
                    # （此前 Topos 源 FFmpeg 打开失败后落到 8-bit fallback）
                    return self._convert_yuv_planes_to_bgr16(
                        planes, pixel_format,
                        getattr(decoded_frame, 'metadata', None),
                        getattr(decoded_frame, 'plane_infos', None),
                        keep_alpha=keep_alpha_planes,
                    )
                return self._convert_yuv_planes_to_bgr(
                    planes, pixel_format,
                    keep_alpha=keep_alpha_planes,
                    metadata=getattr(decoded_frame, 'metadata', None),
                    plane_infos=getattr(decoded_frame, 'plane_infos', None),
                )

            if data is None:
                return None

            # ── 打包格式 ──
            if pixel_format in ('bgr24', 'bgra'):
                if data.ndim == 3 and data.shape[-1] == 4:
                    return data if keep_alpha else data[..., :3]
                return data

            elif pixel_format in ('rgb24', 'rgba'):
                if (keep_alpha and data.ndim == 3 and data.shape[-1] == 4):
                    # 复验 P0-02：RGBA→BGRA 只重排 RGB 三通道，Alpha 恒居末位。
                    # 此前 np.flip(..., axis=-1) 整轴翻转把 Alpha 翻到首通道
                    # （[10,20,30,40] → [40,30,20,10]，正确应为 [30,20,10,40]）。
                    return np.ascontiguousarray(data[..., [2, 1, 0, 3]])
                # RGB -> BGR（3 通道整轴翻转正确）
                return np.flip(data[..., :3], axis=-1)

            elif pixel_format in ('rgb48le', 'rgb48be', 'rgba64le', 'rgba64be'):
                # 高位深 RGB：FFmpegMediaSource 会将 10/12-bit 源转为
                # rgb48/rgba64 的 full-range uint16。导出编码器只接收
                # uint8 BGR；若直接透传，后续 /255 合成会导致通道饱和，
                # 并因 RGB/BGR 顺序错误产生洋红画面。
                rgb = data[..., :3]
                if np.issubdtype(rgb.dtype, np.integer):
                    max_value = np.iinfo(rgb.dtype).max
                    rgb_u8 = np.rint(
                        rgb.astype(np.float32) * (255.0 / max_value)
                    ).astype(np.uint8)
                else:
                    rgb_u8 = np.clip(rgb * 255.0, 0.0, 255.0).astype(np.uint8)
                bgr = np.flip(rgb_u8, axis=-1)
                if keep_alpha and data.ndim == 3 and data.shape[-1] == 4:
                    if np.issubdtype(data.dtype, np.integer):
                        amax = np.iinfo(data.dtype).max
                        a_u8 = np.rint(data[..., 3].astype(np.float32)
                                       * (255.0 / amax)).astype(np.uint8)
                    else:
                        a_u8 = np.clip(data[..., 3] * 255.0, 0.0, 255.0).astype(np.uint8)
                    return np.concatenate([bgr, a_u8[..., None]], axis=-1)
                return bgr

            elif pixel_format in ('yuv420p', 'yuv422p', 'yuv444p',
                                   'yuv420p10le', 'yuv422p10le', 'yuv444p10le'):
                # 单 ndarray 但内容是 YUV（可能是打包的）
                return self._convert_yuv_packed_to_bgr(data, pixel_format)

            else:
                # 兜底：假设已是 BGR-ish 3 通道
                if data.ndim == 3 and data.shape[-1] >= 3:
                    return data[..., :3]
                logger.warning(
                    f"Unknown pixel_format '{pixel_format}', shape={data.shape}; "
                    f"using raw data as BGR"
                )
                return data

        except Exception as e:
            logger.error(f"Failed to extract frame data: {e}")
            return None

    def _convert_yuv_planes_to_bgr(
        self,
        planes: tuple,
        pixel_format: str,
        keep_alpha: bool = False,
        metadata: Any = None,
        plane_infos: Any = None,
    ) -> Optional["np.ndarray"]:
        """将 YUV/GBR 平面 (Y, U, V[, A]) 转换为 BGR（可选 BGRA）uint8。

        复验 P0-03：与 16-bit 路径共用同一位深/range/matrix 感知内核
        （此前 dtype 宽度右移 + 通用 OpenCV 全范围转换，Topos 满刻度
        10-bit 平面与 limited-range FFmpeg 源都被系统性压暗/偏灰）。
        """
        bgr = self._planes_to_bgr_float(
            planes, pixel_format, metadata, plane_infos, keep_alpha)
        if bgr is None:
            return None
        import numpy as np
        return np.rint(np.clip(bgr, 0.0, 1.0) * 255.0).astype(np.uint8)

    def _convert_yuv_packed_to_bgr(
        self,
        data: "np.ndarray",
        pixel_format: str,
    ) -> Optional["np.ndarray"]:
        """将打包的 YUV ndarray 转换为 BGR uint8（兜底）。"""
        try:
            import numpy as np
            import cv2
            # 简化处理：如果是 2D（单平面 Y），无法转换
            if data.ndim == 2:
                logger.warning(f"Cannot convert 2D YUV data (fmt={pixel_format})")
                return None
            # 高位深 → 8-bit
            if data.dtype != np.uint8:
                data = np.clip(data // (1 << (data.itemsize * 8 - 8)), 0, 255).astype(np.uint8)
            # 假定最后一个维度是 interleaved YUV/UYVY 等；尝试 YUV2BGR
            try:
                return cv2.cvtColor(data, cv2.COLOR_YUV2BGR)
            except Exception:
                return data[..., :3] if data.shape[-1] >= 3 else data
        except Exception as e:
            logger.warning(f"YUV packed conversion failed: {e}")
            return data

    def _resize_frame(
        self,
        frame: "np.ndarray",
        target_width: int,
        target_height: int,
    ) -> "np.ndarray":
        """
        调整帧尺寸。

        Args:
            frame: 帧数据
            target_width: 目标宽度
            target_height: 目标高度

        Returns:
            调整后的帧
        """
        import cv2

        h, w = frame.shape[:2]

        if h == target_height and w == target_width:
            return frame

        return cv2.resize(frame, (target_width, target_height), interpolation=cv2.INTER_LINEAR)

    def _encode_frame(
        self,
        frame_data: Any,
    ) -> None:
        """编码帧（T-P0-20：encode 阶段 wall 计时后委托实现）。"""
        started = time.perf_counter()
        try:
            self._encode_frame_impl(frame_data)
        finally:
            recorder = getattr(self, "_export_telemetry", None)
            if recorder is not None:
                # T-P0-31：native bridge 的 surface 提交编码后端是
                # videotoolbox——按 FFmpeg codec 名标注会被误判为 CPU 编码
                # （encode_backend_cpu:h264 假阻断），trail 也不得这样虚标。
                from src.shared.export.gpu_encode_surface import (
                    GpuEncodeSurface as _Surface,
                )

                if isinstance(frame_data, _Surface):
                    backend = str(getattr(
                        frame_data.descriptor, "backend", "")) or "videotoolbox"
                else:
                    encoder_config = getattr(
                        getattr(self, "_encoder", None), "_config", None)
                    backend = str(
                        getattr(encoder_config, "codec", "") or "unknown").lower()
                recorder.record_stage_sample(
                    "encode",
                    backend,
                    wall_ms=(time.perf_counter() - started) * 1000.0,
                )

    def _encode_frame_impl(
        self,
        frame_data: Any,
    ) -> None:
        """
        编码帧。

        Args:
            frame_data: BGR ndarray，或严格校验通过的 TOPOS planar DecodedFrame
        """
        from src.shared.media.frame import DecodedFrame

        from src.shared.export.gpu_encode_surface import GpuEncodeSurface

        if isinstance(frame_data, GpuEncodeSurface):
            try:
                receipt = frame_data.submit()
            except Exception:
                frame_data.release()
                self._record_gpu_fallback("gpu_encoder_submit_failed")
                raise
            gpu_submit_ms = None
            if isinstance(receipt, dict):
                try:
                    gpu_submit_ms = float(receipt["gpu_submit_ms"])
                except (KeyError, TypeError, ValueError):
                    gpu_submit_ms = None
            self._record_gpu_stage(
                "encode_submit",
                frame_data.descriptor.backend,
                gpu_submit_ms=gpu_submit_ms,
            )
            return

        # The current FFmpeg/PyAV boundary consumes host-visible frames. Keep
        # this explicit in the trail until a VideoToolbox/Topos native surface
        # submitter replaces this method.
        self._record_gpu_stage("encode_submit", "cpu_encoder")
        self._record_gpu_fallback("cpu_encoder_surface_path")

        if isinstance(frame_data, DecodedFrame):
            encoder_config = getattr(self._encoder, "_config", None)
            encoder_codec = str(
                getattr(encoder_config, "codec", "")).lower()
            planar_topos = encoder_codec == "topos"
            frame_pix_fmt = str(
                getattr(frame_data, "pixel_format", "")).lower()
            # 8-bit 4:2:0 FFmpeg 直通交付（h264/hevc + yuv420p）：直通门
            # 与 GPU 直出共用该白名单（2026-09-16 起 hevc 与 h264 同构）。
            gpu_yuv420 = (
                encoder_codec in ("h264", "hevc")
                and str(getattr(encoder_config, "pix_fmt", "")).lower()
                == "yuv420p"
                and frame_pix_fmt == "yuv420p"
            )
            # ProRes/DNxHR 4:2:2 交付（2026-09-17 Topos 中间片直通扩展）：
            # 帧像素格式与编码目标同格式（10-bit 同格式直填或门内已降位），
            # 编码器 `_convert_planar_frame` 走字节级直填零转换路径。
            planar_ffmpeg_same = (
                encoder_codec in ("prores", "dnxhr")
                and frame_pix_fmt
                == str(getattr(encoder_config, "pix_fmt", "")).lower()
                and frame_pix_fmt in ("yuv422p10le", "yuv422p")
            )
            if (not frame_data.is_planar or frame_data.data is not None
                    or not (planar_topos or gpu_yuv420
                            or planar_ffmpeg_same)):
                raise TypeError(
                    "Planar timeline frames require a supported planar encoder"
                )
            if self._encoder:
                self._encoder.encode_frame(frame_data)
            return

        # 创建 DecodedFrame
        # 注意：frame_index/pts/time_seconds 是 DecodedFrame 的必填字段
        fps = self._get_sequence_fps()
        channels = frame_data.shape[2] if frame_data.ndim == 3 else 3
        # FFmpegVideoEncoder recognises bgr48le and creates an rgb48le
        # PyAV frame before converting to the selected 10-bit codec.
        # Labeling uint16 samples as bgr24 would silently truncate them.
        # R2：4 通道（Alpha 导出）→ bgra / bgra64le——两编码器均按
        # startswith('bgr') 分支显式处理第四通道。
        if frame_data.dtype.itemsize > 1:
            pixel_format = 'bgra64le' if channels == 4 else 'bgr48le'
        else:
            pixel_format = 'bgra' if channels == 4 else 'bgr24'
        decoded_frame = DecodedFrame(
            frame_index=self._current_frame,
            pts=self._current_frame,
            time_seconds=self._current_frame / fps if fps > 0 else 0.0,
            data=frame_data,
            pixel_format=pixel_format,
            width=frame_data.shape[1],
            height=frame_data.shape[0],
        )

        # 编码
        if self._encoder:
            self._encoder.encode_frame(decoded_frame)

    def _cleanup_clips(self) -> None:
        """清理打开的 clip 会话"""
        futures = tuple(getattr(self, "_metal_prefetch_futures", {}).values())
        if futures:
            for future in futures:
                try:
                    future.result()
                except Exception as error:  # noqa: BLE001 — close still proceeds
                    logger.debug("Metal VT prefetch stopped during cleanup: %s", error)
        getattr(self, "_metal_prefetch_futures", {}).clear()
        executor = getattr(self, "_metal_prefetch_executor", None)
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
            self._metal_prefetch_executor = None

        for asset_id, clip_id in self._open_clips.items():
            try:
                self._frame_provider.close_clip(clip_id)
                logger.debug(f"Closed clip: {clip_id}")
            except Exception as e:
                logger.warning(f"Failed to close clip {clip_id}: {e}")

        self._open_clips.clear()
        # AUD-18408：静态图层解码帧缓存随 clip 会话一起释放（下一次导出
        # 重新解码，_export_has_alpha 等模式变化不会命中陈旧条目）。
        self.__dict__.get("_image_frame_cache", OrderedDict()).clear()
        for source in self._export_rgb8_sources.values():
            try:
                source.close()
            except Exception as exc:
                logger.warning("Failed to close 8-bit export decode source: %s", exc)
        self._export_rgb8_sources.clear()
        for source in self._export_high_depth_sources.values():
            try:
                source.close()
            except Exception as exc:
                logger.warning("Failed to close high-depth export decode source: %s", exc)
        self._export_high_depth_sources.clear()

        # Cleanup CompositeFrameRenderer
        if self._comp_frame_renderer is not None:
            try:
                self._comp_frame_renderer.cleanup()
            except Exception as e:
                logger.warning(f"Failed to cleanup comp frame renderer: {e}")
            self._comp_frame_renderer = None

    def _get_comp_frame_renderer(self):
        """Lazy-initialize and return the CompositeFrameRenderer."""
        if self._comp_frame_renderer is None:
            from src.features.composite.render.composite_frame_renderer import CompositeFrameRenderer
            self._comp_frame_renderer = CompositeFrameRenderer()
        return self._comp_frame_renderer

    def _render_comp_clip_frame(self, layer: "ClipLayerInfo", asset) -> Optional["np.ndarray"]:
        """Render a comp_clip layer through the Composite graph.

        Loads the graph data from the project's compositions dict and
        renders it using CompositeFrameRenderer in export quality.

        Args:
            layer: ClipLayerInfo for the comp_clip layer
            asset: MediaAsset for the comp_clip

        Returns:
            BGR uint8 numpy array, or None if graph data unavailable
        """
        try:
            graph_id = getattr(layer, "composite_id", None) or ""
            if not graph_id:
                if not asset.is_comp_clip:
                    return None
                graph_id = asset.metadata.get('graph_id', '')
            if not graph_id:
                logger.debug(f"Comp clip '{asset.asset_id}' has no graph_id")
                return None

            # Load graph data from project compositions
            graph_data = self._project.compositions.get(graph_id)
            if graph_data is None:
                logger.debug(f"Graph data not found for graph_id='{graph_id}'")
                return None

            # P0-EXPORT-CACHE：comp-clip 导出帧级磁盘缓存（opt-in）。
            # 签名覆盖 graph 全量内容 + 帧 + 输出尺寸/位深/色彩元数据；
            # PNG 无损存储——命中与重渲逐位同质，绝不以画质换速度。
            segment_cache = None
            cache_key = None
            try:
                segment_cache = self._segment_cache_config()
                if segment_cache is not None:
                    cache_key = self._comp_frame_cache_key(
                        graph_id, graph_data, layer,
                    )
                    cached_frame = self._comp_frame_cache_get(
                        segment_cache, cache_key,
                    )
                    if cached_frame is not None:
                        return cached_frame
            except Exception as exc:
                logger.debug(f"Segment cache read skipped: {exc}")
                segment_cache = None
                cache_key = None

            # Render through CompositeFrameRenderer at export quality
            renderer = self._get_comp_frame_renderer()
            caption_input = None
            sequence = getattr(self, "_sequence", None)
            if sequence is not None and hasattr(sequence, "get_caption_graphic_binding_by_clip"):
                binding = sequence.get_caption_graphic_binding_by_clip(layer.clip_id)
                if binding is not None and binding.composite_id:
                    try:
                        from src.features.composite.adapters.caption_input import (
                            CaptionCompositeInputAdapter,
                        )
                        caption_input = CaptionCompositeInputAdapter.resolve(
                            sequence, binding.caption_cue_id)
                    except (ValueError, TypeError) as exc:
                        logger.warning("Caption Composite input unavailable: %s", exc)
            # clip_id / timeline_id 兜底链：graph_data 字段优先，缺则用 layer/sequence 字段。
            # 不兜底会导致 _build_timeline_source_context 在 clip_id 为空时静默返回 None。
            source_refs = graph_data.get('source_refs') or {}
            result = renderer.render_comp_clip_frame(
                graph_data=graph_data,
                timeline_id=(
                    graph_data.get('timeline_id')
                    or getattr(self._sequence, 'timeline_id', None)
                ),
                clip_id=(
                    graph_data.get('clip_id')
                    or graph_data.get('source_clip_id')
                    or source_refs.get('original_clip_id')
                    or layer.clip_id
                ),
                asset_id=(
                    graph_data.get('source_asset_id')
                    or source_refs.get('original_asset_id')
                    or layer.asset_id
                ),
                edit_project=self._project,
                timeline_sequences={self._sequence.timeline_id: self._sequence},
                timeline_frame=layer.source_frame,
                source_frame=layer.source_frame,
                quality="export",
                caption_input=caption_input,
            )

            if result is not None:
                # R2（审计 P1-2）：comp 帧渲染器回读的是**场景线性** BGR
                # float32——此前无条件 np.clip(result*255, 0, 255) 把 >1.0
                # 高光全部裁平。现在先经显式 OETF（LINEAR_SCENE → 导出
                # 传输信号，与 composite_render_output 同一范式），再按
                # 交付位深量化；转换失败才回退显式 clip（上方已记警告，
                # 不再是无条件静默）。
                import numpy as np

                meta = getattr(self, "_export_color_metadata", None)
                signal = self._linear_bgr_to_export_signal(
                    result,
                    transfer=str(getattr(meta, "transfer", "bt709") or "bt709"),
                    primaries=str(getattr(meta, "primaries", "bt709") or "bt709"),
                    color_range=str(getattr(meta, "range", "limited") or "limited"),
                )
                if signal is not None:
                    result = signal
                if getattr(self, "_use_high_precision_export", False):
                    frame = np.clip(result * 65535.0, 0, 65535).astype(np.uint16)
                else:
                    frame = np.clip(result * 255.0, 0, 255).astype(np.uint8)
                if segment_cache is not None and cache_key is not None:
                    try:
                        self._comp_frame_cache_put(
                            segment_cache, cache_key, frame,
                        )
                    except Exception as exc:
                        logger.debug(f"Segment cache write skipped: {exc}")
                return frame

            return None

        except Exception as e:
            logger.warning(f"Comp clip export render failed for layer {layer.clip_id}: {e}")
            return None

    def _segment_cache_config(self):
        """P0-EXPORT-CACHE：帧级导出缓存的 opt-in 配置（默认关闭）。

        需要同时设置 ``TOPOS_EXPORT_SEGMENT_CACHE=1`` 与
        ``TOPOS_EXPORT_SEGMENT_CACHE_DIR=<目录>``——存储位置必须由用户
        显式指定，绝不静默写盘。任何配置/初始化问题返回 None（直接回
        落实时渲染）。
        """
        enabled = os.environ.get(
            "TOPOS_EXPORT_SEGMENT_CACHE", "",
        ).strip().lower() in ("1", "true", "yes")
        if not enabled:
            return None
        root = os.environ.get("TOPOS_EXPORT_SEGMENT_CACHE_DIR", "").strip()
        if not root:
            return None
        cache = getattr(self, "_segment_cache", None)
        if cache is None or str(getattr(cache, "root", "")) != root:
            from src.shared.export.render_cache import DiskRenderCache

            cache = DiskRenderCache(root)
            self._segment_cache = cache
        return cache

    @staticmethod
    def _comp_media_versions(graph_data: Any) -> tuple:
        """P2-17 媒体版本：图引用的外部文件内容身份（path, size, mtime_ns）。

        走 collect_asset_dependencies 的只读路径扫描（媒体/LUT/HDRI/模型/
        材质纹理），逐路径 os.stat（µs 级，帧率无感）。文件缺失记
        (path, -1, -1)——缺失本身也是内容身份：之后文件出现/恢复会改变
        渲染结果，键必须随之变化。
        """
        import os as _os

        from src.features.composite.core.asset_dependency import (
            collect_asset_dependencies,
        )

        versions = []
        try:
            report = collect_asset_dependencies(graph_data, check_fs=False)
        except Exception:
            return (("collect_error", -1, -1),)
        for reference in report.references:
            path = reference.resolved_path or reference.stored_path
            if not path:
                continue
            try:
                stat = _os.stat(path)
                versions.append((str(path), int(stat.st_size), int(stat.st_mtime_ns)))
            except OSError:
                versions.append((str(path), -1, -1))
        return tuple(sorted(set(versions)))

    def _comp_frame_cache_key(self, graph_id: str, graph_data: Any, layer: "ClipLayerInfo"):
        """comp-clip 帧缓存签名（P2-17 规范化）：所有会改变像素的输入都在键内。

        - source_fingerprint：graph_id + 图 JSON 全量哈希（图结构/参数）
        - media_versions：图引用的外部文件 (path, size, mtime_ns)——素材
          被替换/重链接而图 JSON 不变时避免旧帧误命中
        - output_format：(宽, 高, fps_num, fps_den, 位深)——帧率与像素
          位深属于输出规格；图内时间域效果依赖 fps
        - grade：(transfer, primaries, matrix, range)——matrix 影响像素
          语义，此前未进键
        - color_management：OETF 实现版本（EXPORT_OETF_VERSION）
        - renderer_version：复合图渲染实现版本（COMPOSITE_GRAPH_RENDERER_
          VERSION）——渲染器实现变化而图不变时避免命中旧实现帧
        - effects/retime：此缓存阶段是"合成前单层帧"，clip 级效果/变速
          都在下游应用（见 export passthrough 判定），诚实取空值
        """
        import hashlib
        import json

        from src.features.composite.render.composite_frame_renderer import (
            COMPOSITE_GRAPH_RENDERER_VERSION,
        )
        from src.shared.export.render_cache import SegmentRenderKey

        payload = json.dumps(
            graph_data, sort_keys=True, ensure_ascii=False, default=repr,
        ).encode("utf-8")
        graph_hash = hashlib.sha256(payload).hexdigest()
        meta = getattr(self, "_export_color_metadata", None)
        width, height = self._get_sequence_resolution()
        fps_rational = self._get_sequence_fps_rational()
        fps_num = int(getattr(fps_rational, "num", 0) or 0)
        fps_den = int(getattr(fps_rational, "den", 0) or 0)
        if fps_num <= 0 or fps_den <= 0:
            fps_num, fps_den = int(round(self._get_sequence_fps() * 1000)), 1000
        precision = (
            "u16" if getattr(self, "_use_high_precision_export", False) else "u8"
        )
        return SegmentRenderKey(
            source_fingerprint=f"{graph_id}:{graph_hash}",
            in_frame=max(0, int(layer.source_frame)),
            out_frame=max(1, int(layer.source_frame) + 1),
            retime=(),
            layer_stack=(str(graph_id),),
            effects=(),
            grade=(
                str(getattr(meta, "transfer", "bt709") or "bt709"),
                str(getattr(meta, "primaries", "bt709") or "bt709"),
                str(getattr(meta, "matrix", "bt709") or "bt709"),
                str(getattr(meta, "range", "limited") or "limited"),
            ),
            color_management=(EXPORT_OETF_VERSION,),
            output_format=(int(width), int(height), fps_num, fps_den, precision),
            renderer_backend="composite_graph_cpu",
            renderer_version=COMPOSITE_GRAPH_RENDERER_VERSION,
            media_versions=self._comp_media_versions(graph_data),
        )

    def _comp_frame_cache_get(self, cache, key) -> Optional["np.ndarray"]:
        import cv2
        import numpy as np

        hit = cache.get(key)
        if hit is None:
            return None
        data, _metadata = hit
        frame = cv2.imdecode(
            np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED,
        )
        if frame is None or frame.ndim not in (2, 3):
            return None
        # imdecode 单通道会回读灰度；comp 帧 RGB(A) 至少 3 通道
        if frame.ndim == 2:
            return None
        return frame

    def _comp_frame_cache_put(self, cache, key, frame: "np.ndarray") -> None:
        import cv2

        ok, buf = cv2.imencode(".png", frame)
        if not ok:
            return
        cache.put(key, buf.tobytes(), {"format": "png_bgr"})

    @staticmethod
    def _linear_bgr_to_export_signal(
        linear_bgr: "np.ndarray",
        transfer: str = "bt709",
        primaries: str = "bt709",
        color_range: str = "limited",
    ) -> Optional["np.ndarray"]:
        """场景线性 BGR → 导出传输信号（R2/P1-2：显式 OETF）。

        镜像 composite_render_output 的既有范式：``transform_color``
        (LINEAR_SCENE → 输出 ColorSpaceDescriptor)。HDR 传输（PQ/HLG）
        把线性工作值按绝对刻度编码（PQ 的线性单位为 nits），SDR 传输
        （bt709 等）无 >1 表示域——超出的高光由调用侧量化边界可见地
        裁剪。转换失败返回 None（调用方回退显式 clip + 警告）。

        OETF 实现身份由 :data:`EXPORT_OETF_VERSION` 标识，并进导出帧
        缓存键（P2-17）：色彩变换实现变化而图不变时不得命中旧帧。
        """
        try:
            import numpy as np
            from src.shared.color.color_space_descriptor import (
                LINEAR_SCENE,
                ColorSpaceDescriptor,
            )
            from src.shared.color.color_transform_provider import transform_color
            from src.shared.color.types import (
                ColorPrimaries,
                ColorRange,
                ColorTransfer,
            )

            def _enum(cls, value, default):
                try:
                    return cls(str(value))
                except ValueError:
                    return default

            dst = ColorSpaceDescriptor(
                primaries=_enum(ColorPrimaries, primaries, ColorPrimaries.BT709),
                transfer=_enum(ColorTransfer, transfer, ColorTransfer.BT709),
                range=_enum(ColorRange, color_range, ColorRange.LIMITED),
            )
            rgb = np.ascontiguousarray(
                np.asarray(linear_bgr, dtype=np.float32)[..., :3][..., ::-1])
            enc = transform_color(rgb, LINEAR_SCENE, dst)
            return np.ascontiguousarray(enc[..., ::-1])
        except Exception as exc:
            logger.warning(
                "Linear→signal 显式色彩变换失败（%s）；回退显式 clip", exc)
            return None

    def _notify_progress(self) -> None:
        """通知进度更新"""
        if self._progress_callback:
            aggregate_total = int(getattr(self, "_individual_progress_total", 0) or 0)
            if aggregate_total > 0:
                current = min(
                    aggregate_total,
                    int(getattr(self, "_individual_progress_offset", 0)) + self._current_frame,
                )
                self._progress_callback(current, aggregate_total, current / aggregate_total)
                return
            percent = 0.0 if self._total_frames == 0 else self._current_frame / self._total_frames
            self._progress_callback(self._current_frame, self._total_frames, percent)


# =============================================================================
# 辅助函数
# =============================================================================


def create_timeline_exporter(
    project: "EditProject",
    sequence: "TimelineSequence",
    progress_callback: Optional[Callable[[int, int, float], None]] = None,
    timeline_sequences: Optional[Dict[str, "TimelineSequence"]] = None,
) -> TimelineExporter:
    """
    创建时间线导出器。

    工厂函数，自动获取 VideoFrameProviderService。

    Args:
        project: EditProject 实例
        sequence: TimelineSequence 实例
        progress_callback: 进度回调函数
        timeline_sequences: P1-02 全部时间线序列（嵌套序列导出解析用；
            None 时嵌套片段降级跳帧）

    Returns:
        TimelineExporter 实例
    """
    from src.shared.video import get_frame_provider_service

    frame_provider = get_frame_provider_service()

    sequence_resolver = (
        (lambda tid: timeline_sequences.get(tid))
        if timeline_sequences else None
    )

    return TimelineExporter(
        project=project,
        sequence=sequence,
        frame_provider=frame_provider,
        progress_callback=progress_callback,
        sequence_resolver=sequence_resolver,
    )


def estimate_export_duration(
    config: TimelineExportConfig,
    sequence: "TimelineSequence",
) -> float:
    """
    估算导出时长（秒）。

    Args:
        config: 导出配置
        sequence: TimelineSequence 实例

    Returns:
        估算时长（秒）
    """
    # 计算帧数
    duration = effective_timeline_duration_frames(sequence)
    if config.range_type == ExportRangeType.ENTIRE:
        frame_count = duration
    elif config.range_type == ExportRangeType.IN_OUT:
        resolved_range = resolve_timeline_in_out(
            sequence.range_start,
            sequence.range_end,
            duration,
        )
        frame_count = (
            resolved_range[1] - resolved_range[0] + 1
            if resolved_range is not None
            else duration
        )
    elif config.range_type == ExportRangeType.CUSTOM:
        end = config.end_frame if config.end_frame >= 0 else duration
        frame_count = end - config.start_frame
    else:
        frame_count = duration

    # 使用配置帧率或时间线帧率（sequence.fps 是 Rational，需转 float）
    if config.fps:
        fps_val = float(config.fps)
    else:
        seq_fps = sequence.fps
        fps_val = float(seq_fps.to_fps()) if hasattr(seq_fps, 'to_fps') else float(seq_fps)

    return frame_count / fps_val if fps_val > 0 else 0.0


def estimate_export_size(
    config: TimelineExportConfig,
    sequence: "TimelineSequence",
) -> int:
    """
    估算导出文件大小（字节）。

    Args:
        config: 导出配置
        sequence: TimelineSequence 实例

    Returns:
        估算大小（字节）
    """
    # 计算帧数
    duration = effective_timeline_duration_frames(sequence)
    if config.range_type == ExportRangeType.ENTIRE:
        frame_count = duration
    elif config.range_type == ExportRangeType.IN_OUT:
        resolved_range = resolve_timeline_in_out(
            sequence.range_start,
            sequence.range_end,
            duration,
        )
        frame_count = (
            resolved_range[1] - resolved_range[0] + 1
            if resolved_range is not None
            else duration
        )
    elif config.range_type == ExportRangeType.CUSTOM:
        end = config.end_frame if config.end_frame >= 0 else duration
        frame_count = end - config.start_frame
    else:
        frame_count = duration

    # 使用配置分辨率或时间线分辨率
    width, height = config.resolution if config.resolution else (1920, 1080)
    # sequence.fps 是 Rational，需转 float
    if config.fps:
        fps_val = float(config.fps)
    else:
        seq_fps = sequence.fps
        fps_val = float(seq_fps.to_fps()) if hasattr(seq_fps, 'to_fps') else float(seq_fps)

    # 根据编码器估算数据率
    codec_rates = {
        ExportCodec.H264: 12_000_000,            # ~12 Mbps
        ExportCodec.HEVC: 8_000_000,             # ~8 Mbps
        ExportCodec.GIF: 5_000_000,              # rough palette-animation estimate
        ExportCodec.PRORES_PROXY: 45_000_000,    # ~45 Mbps
        ExportCodec.PRORES_LT: 102_000_000,      # ~102 Mbps
        ExportCodec.PRORES_422: 147_000_000,     # ~147 Mbps
        ExportCodec.PRORES_422HQ: 220_000_000,   # ~220 Mbps
        ExportCodec.PRORES_4444: 330_000_000,    # ~330 Mbps
        ExportCodec.PRORES_4444XQ: 500_000_000,  # ~500 Mbps
        ExportCodec.DNXHR_LB: 36_000_000,        # ~36 Mbps
        ExportCodec.DNXHR_SQ: 156_000_000,       # ~156 Mbps (SQ, 1080p30)
        ExportCodec.DNXHR_HQ: 234_000_000,       # ~234 Mbps (HQ, 1080p30)
        ExportCodec.DNXHR_HQX: 312_000_000,      # ~312 Mbps (HQX 10-bit)
        ExportCodec.DNXHR_444: 468_000_000,      # ~468 Mbps (444 + alpha)
    }

    bitrate = codec_rates.get(config.codec, 100_000_000)

    # 估算大小
    duration_seconds = frame_count / fps_val if fps_val > 0 else 0.0
    size_bytes = int(bitrate * duration_seconds / 8)

    # 加上容器开销
    return int(size_bytes * 1.02)


class _ClipOnlyLayer:
    """F04：_pipeline_eligible 的轻量 layer 适配——_get_clip_video_denoise
    只读 ``clip`` 属性，避免为逐 clip 检查构造完整 ClipLayerInfo。"""

    __slots__ = ("clip",)

    def __init__(self, clip: Any) -> None:
        self.clip = clip
