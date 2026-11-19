"""
Timeline v2 Data Models Module
剪辑时间线数据模型 v2。

## 聧责

- TimelineSequence: 时间线序列（视频轨、音频轨、标记）
- EditTrack: 编辑轨道（剪辑实例列表）
- ClipInstance: 剪辑实例（素材引用、时间映射、变换参数）
- TransitionInstance: 转场实例（两个剪辑间的转场效果）
- TimelineMarker: 时间线标记
- Keyframe: 关键帧

## 设计原则

- 不可变数据类（frozen=True）确保线程安全
- 完整类型注解
- 不依赖 PyQt，纯 Python 数据模型
- 参数验证在 __post_init__ 中进行
- 同轨剪辑不允许重叠

## P5.3 - Keyframe Animation

- ClipInstance.animated_params: Dict of parameter keyframes
- Supported: opacity, transform.translate_x/y, transform.scale_x/y, transform.rotation

## 参考

- docs/edit_page_timeline_architecture.md P0.B ADR
- src/shared/media/timeline.py 现有模型（作为参考，was src/core/media/timeline.py）
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Collection, Optional, Dict, ClassVar, Literal
from enum import Enum
import uuid

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from fractions import Fraction

logger = logging.getLogger(__name__)

from src.shared.media.metadata import Rational
from src.shared.extra_fields import preserves_extras, split_extras
# R01：FX schema 版本常量（fx_versioning 只在本模块内 lazy import，无环）
from src.features.edit.core.fx_versioning import AUDIO_FX_SCHEMA_VERSION
from src.features.edit.core.clip_params import (
    ClipBlendMode,
    CropParams,
    RawParams,
    SpeedParams,
    TransformParams,
)
from src.features.edit.core.effect_instance import AnimatableParam  # P5.3: Import AnimatableParam
from src.features.edit.core.transition_catalog import TRANSITION_TYPE_OPTIONS
from src.features.edit.core.audio_transition_catalog import (
    AUDIO_TRANSITION_TYPE_OPTIONS,
)
# SUB-P2-01/10: layered caption model (style/track/cue metadata + schema).
from src.features.edit.core.captions.model import (
    CaptionCueMetadata,
    CaptionStyle,
    CaptionTrackMetadata,
)
from src.features.edit.core.captions.graphics import (
    CaptionGraphicBinding,
    CaptionGraphicTemplate,
    CaptionGraphicTemplateRegistry,
    CaptionTrackGraphicStyle,
    migrate_caption_graphics_block,
)

# Import InterpolationType from shared module (Phase 2: Modularization)
from src.shared.keyframe import InterpolationType, KeyframeTrack, Keyframe


# 字幕字号的唯一默认策略：0 表示按输出画布高度自适应。
# 1080p 下约为 59px（文档中可近似写作 60px），4K 会按比例放大。
SUBTITLE_AUTO_FONT_SIZE_RATIO: float = 0.055
SUBTITLE_DEFAULT_CANVAS_HEIGHT: int = 1080


def effective_subtitle_font_size(font_size: int | None,
                                 canvas_height: int = SUBTITLE_DEFAULT_CANVAS_HEIGHT) -> int:
    """Return the concrete pixel size used to render a subtitle.

    ``SubtitleClip.font_size == 0`` is an intentional auto-fit sentinel. UI,
    preview and export must all resolve it through this function instead of
    showing/using different hard-coded defaults.
    """
    requested = int(font_size or 0)
    if requested > 0:
        return requested
    return max(12, int(max(1, int(canvas_height)) * SUBTITLE_AUTO_FONT_SIZE_RATIO))


# ================================================================
# GradeMode Enum - 调色模式 (P9.1)
# ================================================================

class GradeMode(Enum):
    """
    调色模式枚举。

    同一素材多次出现在时间线时的调色语义：
    - SHARED: 同一素材所有 clip 共用一个调色（多个 clip 引用同一个 asset default stack）
    - LOCAL: 同一素材不同 clip 独立调色（每个 clip 有自己的 local stack）

    操作行为：
    - 第一次从 clip 进入 Color：默认创建 clip_local stack
    - 右键 clip：Use Shared Asset Grade / Make Local Grade Copy
    - Media Pool asset 右键：Open Asset Grade in Color

    参考：
    - docs/library_media_pool_project_architecture_plan_2026-05-19.md 方案 8
    """

    SHARED = "shared"  # 共享素材调色
    LOCAL = "local"    # 独立剪辑调色

    @property
    def is_shared(self) -> bool:
        """是否为共享调色模式"""
        return self == GradeMode.SHARED

    @property
    def is_local(self) -> bool:
        """是否为独立调色模式"""
        return self == GradeMode.LOCAL


MIN_SPEED: float = 0.1
MAX_SPEED: float = 8.0

# Timeline format-settings keys used by the Media Pool card preview.  The
# preview is intentionally represented by a stable asset/frame reference;
# decoded QPixmaps stay in ThumbnailProvider's cache and are not serialized.
MEDIA_POOL_PREVIEW_ASSET_ID_KEY = "media_pool_preview_asset_id"
MEDIA_POOL_PREVIEW_FRAME_KEY = "media_pool_preview_frame"


def _parse_blend_mode(value: Any) -> ClipBlendMode:
    """A1: 容错解析混合模式字符串（缺失/非法 → NORMAL）。"""
    if isinstance(value, ClipBlendMode):
        return value
    try:
        return ClipBlendMode(str(value))
    except (ValueError, TypeError):
        return ClipBlendMode.NORMAL


@dataclass(frozen=True)
class VcaGroup:
    """P1 · VCA 组：增益/静音控制层（不改路由与 Send tap）。

    - 成员轨的输出电平乘以组增益（dB，可正可负）；任一所属组静音即
    静音（多组相乘语义）；
    - 路由（output_bus_id/sends/pre-post tap）完全不变——VCA 只作用于
    成员轨的推子级增益；
    - 快照：``(gain_db, muted)`` 二元组按 vca_id 收集/重放（见
    ``TimelineSequence.vca_snapshot``）。
    """
    vca_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    member_track_ids: tuple = ()
    gain_db: float = 0.0
    muted: bool = False

    def with_gain_db(self, gain_db: float) -> "VcaGroup":
        return replace(self, gain_db=float(gain_db))

    def with_muted(self, muted: bool) -> "VcaGroup":
        return replace(self, muted=bool(muted))

    def linear_gain(self) -> float:
        """组增益的线性倍率（静音=0）。"""
        return 0.0 if self.muted else 10.0 ** (self.gain_db / 20.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "vca_id": self.vca_id,
            "name": self.name,
            "member_track_ids": list(self.member_track_ids),
            "gain_db": self.gain_db,
            "muted": self.muted,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VcaGroup":
        return cls(
            vca_id=str(data.get("vca_id") or uuid.uuid4()),
            name=str(data.get("name", "") or ""),
            member_track_ids=tuple(
                str(tid) for tid in data.get("member_track_ids", ()) or ()),
            gain_db=float(data.get("gain_db", 0.0) or 0.0),
            muted=bool(data.get("muted", False)),
        )


@dataclass(frozen=True)
class TimelineMarker:
    """时间线标记。"""
    marker_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    frame: int = 0
    name: str = ""
    color: str = "#ff0000"
    comment: str = ""
    # Optional clip scope.  ``None`` keeps the marker timeline-scoped and
    # renders it on the ruler; a clip/track pair renders it above that clip.
    clip_id: str | None = None
    track_id: str | None = None
    # C5: marker kind. "standard"（默认）或 "chapter"（专业交付章节，可导出
    # YouTube 章节文本 / FFMetadata）。旧工程无该字段 → standard，兼容。
    kind: str = "standard"

    def __post_init__(self) -> None:
        if not self.marker_id:
            raise ValueError("marker_id cannot be empty")
        if self.frame < 0:
            raise ValueError(f"frame cannot be negative: {self.frame}")
        if not self.color.startswith("#") or len(self.color) != 7:
            raise ValueError(f"Invalid color format: {self.color}")
        if self.kind not in ("standard", "chapter"):
            raise ValueError(f"Invalid marker kind: {self.kind}")

    def with_kind(self, kind: str) -> "TimelineMarker":
        """返回指定 kind 的新标记（frozen 模型，禁止原地修改）。"""
        return TimelineMarker(
            marker_id=self.marker_id, frame=self.frame, name=self.name,
            color=self.color, comment=self.comment,
            clip_id=self.clip_id, track_id=self.track_id, kind=kind,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "marker_id": self.marker_id,
            "frame": self.frame,
            "name": self.name,
            "color": self.color,
            "comment": self.comment,
            "clip_id": self.clip_id,
            "track_id": self.track_id,
            "kind": self.kind,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> TimelineMarker:
        return TimelineMarker(
            marker_id=data.get("marker_id", str(uuid.uuid4())),
            frame=data["frame"], name=data.get("name", ""),
            color=data.get("color", "#ff0000"), comment=data.get("comment", ""),
            clip_id=data.get("clip_id"), track_id=data.get("track_id"),
            kind=data.get("kind", "standard"),
        )


def _repair_clip_animated_range(param_name: str, anim_param) -> Any:
    """R11（第七轮审计）：已知 clip 动画参数的值域迁移到权威 schema。

    C01 之前持久化的 AnimatableParam/KeyframeTrack 带旧默认值域
    （translate/rotation 常见 [0,100]），负平移/大角度关键帧求值被钳到
    0。求值钳制走 KeyframeTrack 的 min_val/max_val，因此 param 与内部
    track 两层都要修；关键帧 raw、base_value 与未知参数名原样保留。
    """
    from dataclasses import replace as _dc_replace

    if not (param_name == "opacity" or param_name.startswith("transform.")):
        return anim_param  # 未知/效果参数保持持久化值域

    from src.features.edit.core.clip_params import clip_param_range

    lo, hi = clip_param_range(param_name)
    track = anim_param.keyframes
    if track is not None and (track.min_val != lo or track.max_val != hi):
        track = _dc_replace(track, min_val=lo, max_val=hi)
    if anim_param.min_value == lo and anim_param.max_value == hi:
        return anim_param if anim_param.keyframes is track else _dc_replace(
            anim_param, keyframes=track)
    return _dc_replace(
        anim_param, min_value=lo, max_value=hi, keyframes=track)


def _int_or_default(value, default: int | None) -> int | None:
    """F3.3：采样级字段的防御解析（None/缺省→default；非负 int 直通；
    非法值回退 default——不静默接受浮点样本语义）。"""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if value >= 0 else default


# W05（契约 D4）：clip 彩色旗标白名单——UI 菜单与命令校验共用同一
# 真值来源；时间线面板的 Flags 菜单从这里取色，未知/历史值读取保留、
# 绘制 fallback 默认色。
CLIP_FLAG_COLORS: tuple[str, ...] = (
    "#4BA6DD",  # Blue
    "#46C7CA",  # Cyan
    "#72B66B",  # Green
    "#E4C454",  # Yellow
    "#E06B66",  # Red
    "#D88BB7",  # Pink
    "#A38CCC",  # Purple
)


@dataclass(frozen=True)
class ClipInstance:
    """
    剪辑实例。Timeline clip 通过 asset_id 引用素材。

    时间映射：
    - timeline_start + timeline_duration: 时间线位置和时长
    - source_start + source_duration: 素材内使用范围
    - speed: 播放速度

    P5.3 - Keyframe Animation:
    - animated_params: Dict[str, AnimatableParam] for animated parameters
    - Supported: opacity, transform.translate_x/y, transform.scale_x/y, transform.rotation

    P9.1 - Color Page Linkage:
    - color_stack_id: 调色栈引用 ID，用于 Edit → Color 页面联动
    - color_shared: 是否共享素材调色（False 为独立调色）

    P9.2 - Edit → Composite Page Linkage:
    - composite_id: 合成引用 ID，用于 Edit → Composite 页面联动
    - 如果设置了 composite_id，composite graph 输出作为 clip 的渲染源
    """
    clip_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    asset_id: str = ""
    track_id: str = ""
    timeline_start: int = 0
    timeline_duration: int = 0
    source_start: int = 0
    source_duration: int = 0
    speed: SpeedParams | float = SpeedParams()  # Support both types for flexibility
    linked_clip_id: str | None = None
    transform: TransformParams = field(default_factory=TransformParams)
    crop: CropParams = field(default_factory=CropParams)
    opacity: float = 1.0
    animated_params: Dict[str, AnimatableParam] = field(default_factory=dict)  # P5.3
    # R02：未来 schema 写入的未知字段在此保留（与 CaptionCueMetadata.extra
    # 同构），to_dict 原样回写——高版本工程经低版本读写不丢新字段。
    _extra_fields: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    effects: tuple[Any, ...] = ()
    is_enabled: bool = True
    # Label color for clip identification (hex color string or None for default)
    label_color: str | None = None
    # W05：clip 级彩色旗标（接线计划契约 D4）。"#RRGGBB" 白名单色或
    # None=无旗标。独立于 MediaAsset.flag（素材级 reject 标记）与
    # label_color（片段配色）；序列化非默认才消费，未知值读取保留、
    # UI 绘制 fallback 默认色。split/复制/Replace 经字段保留自动继承。
    flag_color: str | None = None
    # Audio timeline display preference. False keeps stereo source peaks
    # mixed into one compact envelope; True shows separate L/R envelopes.
    waveform_show_channels: bool = False
    # Phase 1.2: Fade in/out duration (frames)
    fade_in_duration: int = 0  # 淡入时长（帧）
    fade_out_duration: int = 0  # 淡出时长（帧）
    # B16：淡化曲线形状（沿 fade_in/out_duration 的归一化进度定义）。
    # None = legacy 默认（视频 linear / 音频 equal_power，旧工程逐帧/
    # 逐样本不变）；显式 "linear" | "equal_power" | "smooth" 视听统一。
    # split/复制经 replace 自动继承；序列化非默认才写出，未知历史值
    # 读取保留（消费者按 legacy 默认解释，不丢字段）。
    fade_curve: str | None = None
    # R09：两端**独立**曲线（Resolve 契约：In smooth / Out linear 可共存）。
    # None = 回落共享 ``fade_curve``（B16 旧字段）→ legacy 默认；白名单同
    # fade_curve。单端修改不触碰另一端与共享字段。
    fade_curve_in: str | None = None
    fade_curve_out: str | None = None
    # F3.3 · 采样级音频语义（有理数时钟见 core/sample_time.py）：
    # - audio_sample_offset: 时间线侧子帧偏移（clip 帧起点后延迟的样本数
    #   @48k 参考率，r ≥ 0；不做帧进位归一化——非整数帧率下任何进位都
    #   产生有理数残差错位，位置由 (帧, 偏移) 二元组精确表示）
    # - source_sample_offset: 源侧子帧修剪（入点后跳过的源样本数）
    # - fade_in/out_samples: 采样域淡化（设置时覆盖帧域 fade；None=沿用帧）
    # 旧工程无这些字段 → 默认 0/None（迁移零改动；to_dict 只在非默认时
    # 写出，旧版读新工程经 _extra_fields 保留不丢）。
    audio_sample_offset: int = 0
    source_sample_offset: int = 0
    fade_in_samples: int | None = None
    fade_out_samples: int | None = None
    # P0-3：Clip Editor 非破坏性处理栈（版本化 dict；None = 无编辑）。
    # 结构/校验/渲染见 core/audio_edit.py；未知 processor 由该模块的
    # validate 阻断应用与交付（模型层只做类型校验，载入不抛）。
    # to_dict 非默认才写出（旧工程序列化字节不变）。
    audio_edit: dict | None = None
    # P9.1: Color page linkage
    color_stack_id: str | None = None  # 调色栈引用 ID
    color_shared: bool = False  # 是否共享素材调色
    # P9.2: Composite page linkage
    composite_id: str | None = None  # 合成引用 ID
    composite_bypassed: bool = False  # 合成旁路（临时关闭合成效果）
    # Render Cache linkage
    render_cache_id: str | None = None  # 渲染缓存 manifest ID
    # A2: 冻结帧。None = 正常播放；否则该片段全程显示此源帧号
    # （播放/擦洗/导出统一经 timeline_to_source_frame 短路返回）。
    freeze_source_frame: int | None = None
    # A1: 图层混合模式（与下方已合成结果混合；normal = 纯 alpha over）
    blend_mode: ClipBlendMode = ClipBlendMode.NORMAL
    # C3: 片段级音频增益（dB，[-48, +12]；0 = 无增益）。混音顺序：
    # clip gain → clip 包络 → track automation → track volume → bus。
    audio_gain_db: float = 0.0
    # TRAW（批 2）：非破坏 RAW 参数（WB gain / EI / 预览 OOTF）。
    # None = 未调整（debayer 走单位增益默认）；仅 RAW 素材消费。
    raw: "RawParams | None" = None
    # F4.1: 片段微调栈（组 look 之上的单镜头微调；None = 无）。
    # 顺序见 core/color_chain.py：pre_group → 主栈 → trim → post_group。
    # split/复制经 dataclass replace 自动保留（timeline_ops 全字段保留契约）。
    color_trim_stack_id: str | None = None
    # FPS metadata for precise frame mapping (Phase 10 integration)
    _source_fps_num: int | None = None
    _source_fps_den: int | None = None
    _timeline_fps_num: int | None = None
    _timeline_fps_den: int | None = None
    _source_pts_index: tuple[float, ...] | None = None
    # Edit content v4: exact CFR sampling phase, in source-frame units.
    _source_frame_phase_num: int = 0
    _source_frame_phase_den: int = 1

    def __post_init__(self) -> None:
        if self._source_frame_phase_den <= 0:
            raise ValueError("source frame phase denominator must be positive")
        if not self.clip_id:
            raise ValueError("clip_id cannot be empty")
        if not self.asset_id:
            raise ValueError("asset_id cannot be empty")
        if not self.track_id:
            raise ValueError("track_id cannot be empty")
        if self.timeline_start < 0:
            raise ValueError(f"timeline_start cannot be negative: {self.timeline_start}")
        if self.timeline_duration <= 0:
            raise ValueError(f"timeline_duration must be positive: {self.timeline_duration}")
        if self.source_start < 0:
            raise ValueError(f"source_start cannot be negative: {self.source_start}")
        if self.source_duration <= 0:
            raise ValueError(f"source_duration must be positive: {self.source_duration}")
        # speed can be SpeedParams or float
        if isinstance(self.speed, SpeedParams):
            # SpeedParams handles its own validation
            pass
        elif isinstance(self.speed, (int, float)):
            if self.speed < MIN_SPEED or self.speed > MAX_SPEED:
                raise ValueError(f"speed must be between {MIN_SPEED} and {MAX_SPEED}")
        else:
            raise ValueError(f"speed must be SpeedParams or float, got {type(self.speed)}")
        if self.opacity < 0.0 or self.opacity > 1.0:
            raise ValueError("opacity must be between 0.0 and 1.0")
        # A2: freeze frame validation
        if self.freeze_source_frame is not None and self.freeze_source_frame < 0:
            raise ValueError(
                f"freeze_source_frame cannot be negative: {self.freeze_source_frame}")
        # A1: blend mode normalization (invalid/foreign values → NORMAL)
        if not isinstance(self.blend_mode, ClipBlendMode):
            object.__setattr__(self, "blend_mode", _parse_blend_mode(self.blend_mode))
        # C3: clip gain 范围校验
        if not -48.0 <= self.audio_gain_db <= 12.0:
            raise ValueError(
                f"audio_gain_db must be within [-48, +12]: {self.audio_gain_db}")
        # Phase 1.2: fade validation
        if self.fade_in_duration < 0:
            raise ValueError(f"fade_in_duration cannot be negative: {self.fade_in_duration}")
        # F3.3：采样级字段防御（命令层有 fps 感知的精确上限，这里只挡
        # 负值与离谱大值；偏移语义声明见 sample_time.py）
        for _name in ("audio_sample_offset", "source_sample_offset"):
            if not 0 <= getattr(self, _name) <= 48000 * 600:
                raise ValueError(f"{_name} out of range: {getattr(self, _name)}")
        for _name in ("fade_in_samples", "fade_out_samples"):
            _v = getattr(self, _name)
            if _v is not None and not 0 <= _v <= 48000 * 600:
                raise ValueError(f"{_name} out of range: {_v}")
        if self.fade_out_duration < 0:
            raise ValueError(f"fade_out_duration cannot be negative: {self.fade_out_duration}")
        # B16：fade_curve 白名单（None=legacy 默认；未知历史值读取保留
        # 由 from_dict 负责，构造路径严格校验新写）。R09：两端独立字段
        # 同一白名单。
        for _curve_field in ("fade_curve", "fade_curve_in", "fade_curve_out"):
            _curve = getattr(self, _curve_field)
            if _curve is not None and _curve not in (
                    "linear", "equal_power", "smooth"):
                raise ValueError(
                    f"{_curve_field} must be linear|equal_power|smooth or "
                    f"None: {_curve!r}")
        # fade duration cannot exceed clip duration
        if self.fade_in_duration > self.timeline_duration:
            raise ValueError(f"fade_in_duration ({self.fade_in_duration}) exceeds clip duration ({self.timeline_duration})")
        if self.fade_out_duration > self.timeline_duration:
            raise ValueError(f"fade_out_duration ({self.fade_out_duration}) exceeds clip duration ({self.timeline_duration})")
        # Fade-in and fade-out occupy the same clip time span from opposite
        # sides.  Keep legacy/project data loadable, but normalize an overlap
        # at the model boundary so the two ramps can meet at most at one point.
        if self.fade_in_duration + self.fade_out_duration > self.timeline_duration:
            object.__setattr__(
                self,
                "fade_out_duration",
                max(0, self.timeline_duration - self.fade_in_duration),
            )

    @property
    def timeline_end(self) -> int:
        return self.timeline_start + self.timeline_duration

    @property
    def source_end(self) -> int:
        return self.source_start + self.source_duration - 1

    def effective_speed(self) -> float:
        """
        获取有效速度值。

        Returns:
            如果 speed 是 SpeedParams，返回其 speed 属性；
            否则返回 speed 的浮点值。
        """
        if isinstance(self.speed, SpeedParams):
            return self.speed.speed
        return float(self.speed)

    def timeline_to_source_frame(self, timeline_frame: int) -> int:
        """
        将时间线帧转换为素材帧。

        当 fps 元数据可用时，委托给 timeline_frame_mapper 进行精确的有理数帧映射；
        否则回退到旧的 inline 公式。

        Args:
            timeline_frame: 时间线帧号

        Returns:
            素材帧号

        Raises:
            ValueError: 如果帧号超出剪辑范围
        """
        if not (self.timeline_start <= timeline_frame < self.timeline_end):
            raise ValueError(f"Timeline frame {timeline_frame} outside clip range")

        # A2: 冻结帧——任意 timeline 帧都映射到同一源帧（播放/擦洗/导出
        # 三路共用此方法，无需各自特判）。
        if self.freeze_source_frame is not None:
            return self.freeze_source_frame

        # Motion Graphic clips reference a scene document instead of a
        # source movie.  Shift-drag retiming changes only the timeline span;
        # map that span across the authored scene so a longer clip continues
        # to play the whole deck instead of holding its last frame.  Keep the
        # check is kept local to this model method so ordinary media clips
        # retain the existing frame-mapper path.
        # Keep this hot-path check allocation-free; the host adapter accepts
        # these two stable virtual prefixes and importing it per frame would
        # add avoidable work to realtime playback.
        asset_id = str(self.asset_id or "")
        is_motion_graphic = asset_id.startswith((
            "teaching_animation:",
            "motion_graphic:",
        ))
        if is_motion_graphic and self.timeline_duration != self.source_duration:
            timeline_span = max(1, self.timeline_duration - 1)
            source_span = max(0, self.source_duration - 1)
            relative = max(0, timeline_frame - self.timeline_start)
            mapped = (relative * source_span + timeline_span // 2) // timeline_span
            return self.source_start + min(source_span, mapped)

        # The rational mapper currently models a single constant speed.  A
        # retime-control clip owns a piecewise mapper in SpeedParams, which is
        # also used by preview and export, so keep that path authoritative.
        has_speed_points = (
            isinstance(self.speed, SpeedParams) and self.speed.is_variable_speed
        )
        # Use precise frame mapper when fps metadata is available.
        if (
            not has_speed_points
            and self._source_fps_num is not None
            and self._timeline_fps_num is not None
        ):
            from fractions import Fraction
            from src.features.edit.core.timeline_frame_mapper import map_frame, FrameMapRequest

            spd = self.effective_speed()
            # C04：reverse 显式取自 SpeedParams.reverse；legacy 数值负速
            # 度同样折算为 reverse + 正速度，mapper 不再从正速度猜方向。
            reverse = spd < 0
            if isinstance(self.speed, SpeedParams) and self.speed.reverse:
                reverse = True
            req = FrameMapRequest(
                timeline_frame=timeline_frame,
                timeline_fps_num=self._timeline_fps_num,
                timeline_fps_den=self._timeline_fps_den or 1,
                clip_timeline_start=self.timeline_start,
                clip_timeline_duration=self.timeline_duration,
                source_start_frame=self.source_start,
                source_duration_frames=self.source_duration,
                source_fps_num=self._source_fps_num,
                source_fps_den=self._source_fps_den or 1,
                speed=Fraction(abs(spd)).limit_denominator(10000),
                reverse=reverse,
                source_pts_index=self._source_pts_index,
                source_frame_phase=Fraction(self._source_frame_phase_num,
                                            self._source_frame_phase_den),
            )
            result = map_frame(req)
            if result is not None:
                return result.source_frame
            # Fallback through to old formula if mapper returns None

        relative = timeline_frame - self.timeline_start
        if isinstance(self.speed, SpeedParams):
            # R06：有变速点时把实际时间线帧按 fps 比例折算进 SpeedParams
            # 的 speed 域（30fps 源/24fps 序列第 71 帧应播到源 ~117 而非
            # 82——旧路径把时间线帧当 speed 域帧）。
            # N02/C3-02：等效位置保持连续——先 int(round) 再交给
            # SpeedParams 乘段速度，量化误差会被高倍速段放大（30/24
            # fps 8x 加中性速度点实测 75/100 帧 max 差 4）。段积分在
            # SpeedParams 内保持 float，最终源帧统一量化。
            ratio = self._source_over_timeline_fps_ratio()
            equiv = (
                relative * float(ratio) if ratio != 1 else relative
            )
            if not has_speed_points and self._source_frame_phase_num:
                equiv += (self._source_frame_phase_num / self._source_frame_phase_den
                          / self.effective_speed())
            start, _ = self.speed.timeline_to_source_frames(
                equiv, self.source_duration
            )
            return self.source_start + start
        return self.source_start + int(relative * float(self.speed)
                                       + self._source_frame_phase_num / self._source_frame_phase_den)

    def timeline_to_source_frame_pair(self, timeline_frame: int) -> tuple[int, int, float]:
        """Absolute timeline frame → clip-source-relative blend samples.

        Preview/export use this entry point so FPS and the retained sampling
        phase are applied before interpolation. The third value weights b.
        """
        from fractions import Fraction
        import math

        if not self.timeline_start <= timeline_frame < self.timeline_end:
            raise ValueError(f"Timeline frame {timeline_frame} outside clip range")
        if self.freeze_source_frame is not None:
            relative = self.freeze_source_frame - self.source_start
            return relative, relative, 0.0
        local = timeline_frame - self.timeline_start
        if isinstance(self.speed, SpeedParams) and self.speed.speed_points:
            return self.speed.timeline_to_source_frame_pair(
                local * float(self._source_over_timeline_fps_ratio()), self.source_duration)
        position = (local * self.source_frames_per_timeline_frame()
                    + Fraction(self._source_frame_phase_num, self._source_frame_phase_den))
        floor = math.floor(position)
        weight = float(position - floor)
        # A fractional entry may blend the preceding source handle even
        # though nearest sampling starts on the following integer frame.
        a = max(-self.source_start, min(floor, self.source_duration - 1))
        b = max(-self.source_start, min(floor + 1, self.source_duration - 1))
        if self.is_reverse_playback():
            a, b = self.source_duration - 1 - a, self.source_duration - 1 - b
        return a, b, weight

    def source_to_timeline_frame(self, source_frame: int) -> int:
        """
        将素材帧转换为时间线帧。

        Args:
            source_frame: 素材帧号

        Returns:
            时间线帧号

        Raises:
            ValueError: 如果帧号超出素材范围
        """
        if not (self.source_start <= source_frame <= self.source_end):
            raise ValueError(f"Source frame {source_frame} outside clip range")
        source_relative = source_frame - self.source_start
        effective_speed = self.effective_speed()
        if self._source_frame_phase_num and not (
                isinstance(self.speed, SpeedParams) and self.speed.speed_points):
            from fractions import Fraction
            playback = (self.source_duration - 1 - source_relative
                        if self.is_reverse_playback() else source_relative)
            phase = Fraction(self._source_frame_phase_num, self._source_frame_phase_den)
            return self.timeline_start + max(0, int(
                (playback - phase) / self.source_frames_per_timeline_frame()))
        if isinstance(self.speed, SpeedParams):
            # R11：逆映射同样按 fps 比例折回实际时间线域（30fps 源 160
            # 应落 24fps 序列 48，旧公式 60；字幕/源标记定位依赖此映射）
            t_equiv = self.speed.source_to_timeline_frame_for_source_duration(
                source_relative,
                self.source_duration,
            )
            ratio = self._source_over_timeline_fps_ratio()
            if ratio == 1:
                return self.timeline_start + t_equiv
            return self.timeline_start + int(round(t_equiv / ratio))
        factor = self.source_frames_per_timeline_frame()
        if factor == abs(effective_speed):
            # 无 fps 元数据：维持 legacy 公式（含数值负速的反向语义）
            return self.timeline_start + int(source_relative / effective_speed)
        return self.timeline_start + int(source_relative / float(factor))

    def clip_local_to_source_frame(self, clip_local_frame: int) -> int:
        """
        将剪辑本地帧转换为素材帧。

        clip_local_frame 是相对于剪辑起始的帧号（0 = 剪辑第一帧）。

        Args:
            clip_local_frame: 剪辑本地帧号（相对于 timeline_start）

        Returns:
            素材帧号

        Raises:
            ValueError: 如果帧号超出剪辑范围
        """
        timeline_frame = self.timeline_start + clip_local_frame
        return self.timeline_to_source_frame(timeline_frame)

    def source_to_clip_local_frame(self, source_frame: int) -> int:
        """
        将素材帧转换为剪辑本地帧。

        返回相对于剪辑起始的帧号（0 = 剪辑第一帧）。

        Args:
            source_frame: 素材帧号

        Returns:
            剪辑本地帧号

        Raises:
            ValueError: 如果帧号超出素材范围
        """
        timeline_frame = self.source_to_timeline_frame(source_frame)
        return timeline_frame - self.timeline_start

    def with_timeline_start(self, new_start: int) -> ClipInstance:
        return replace(self, timeline_start=new_start)

    def with_temporal_shift(self, delta: int) -> ClipInstance:
        """C13：clip 在时间线上整体平移 delta 帧时同步平移动画关键帧。

        绝对时间域的 clip animated_params 与 effect animated_params 一起
        移动，保持 clip-local 曲线不变；delta=0 原样返回。marker/转场由
        各自的事务路径处理（C11/C16）。
        """
        if delta == 0:
            return self
        from src.features.edit.core.effect_instance import AnimatableParam

        def _shift_animatable(param: Any) -> Any:
            if isinstance(param, AnimatableParam):
                return param.shifted(delta)
            return param

        new_animated = {
            name: _shift_animatable(param)
            for name, param in self.animated_params.items()
        }
        new_effects = tuple(
            effect.with_animated_params_shifted(delta)
            if hasattr(effect, "with_animated_params_shifted")
            else effect
            for effect in self.effects
        )
        return replace(self, animated_params=new_animated, effects=new_effects)

    def with_animation_time_remapped(
        self, old_duration: int, new_duration: int
    ) -> ClipInstance:
        """F2.4：变速重锚时按比例重映射 clip/effect 动画关键帧（绝对帧域）。

        关键帧在 clip 时间 span 内的相对位置保持：f' = start +
        (f - start) * new/old（Fraction 精确，经 shifted/time_scaled 组合，
        handle 增量同比例缩放）。降速多帧合并沿用 time_scaled 的
        "保留后值"去重；时长不变或无动画原样返回。
        """
        if old_duration <= 0 or new_duration == old_duration:
            return self
        if not self.animated_params and not self.effects:
            return self
        from fractions import Fraction

        from src.features.edit.core.effect_instance import AnimatableParam

        anchor = self.timeline_start
        ratio = Fraction(new_duration, old_duration)

        def _remap(param: Any) -> Any:
            if isinstance(param, AnimatableParam) and param.is_animated():
                return (
                    param.shifted(-anchor)
                    .time_scaled(ratio)
                    .shifted(anchor)
                )
            return param

        new_animated = {
            name: _remap(param) for name, param in self.animated_params.items()
        }
        new_effects = tuple(
            effect.with_animated_params_remapped(anchor, ratio)
            if hasattr(effect, "with_animated_params_remapped")
            else effect
            for effect in self.effects
        )
        return replace(self, animated_params=new_animated, effects=new_effects)

    def with_timeline_duration(self, new_duration: int) -> ClipInstance:
        # C10：时长变化时把淡入/淡出钳制到新时长内，修剪/变速缩短不再
        # 因 fade > duration 在 __post_init__ 抛错（审计实测 100 帧淡出
        # 40 的片段裁到 20 抛 ValueError）。
        return replace(
            self,
            timeline_duration=new_duration,
            fade_in_duration=min(self.fade_in_duration, new_duration),
            fade_out_duration=min(self.fade_out_duration, new_duration),
        )

    def with_source_range(self, new_start: int | None = None, new_duration: int | None = None) -> ClipInstance:
        actual_start = new_start if new_start is not None else self.source_start
        actual_duration = new_duration if new_duration is not None else self.source_duration
        if isinstance(self.speed, SpeedParams):
            new_timeline_duration = self.speed.timeline_duration_for_source_duration(
                actual_duration
            )
        else:
            new_timeline_duration = int(actual_duration / self.effective_speed())
        return replace(
            self,
            timeline_duration=new_timeline_duration,
            source_start=actual_start,
            source_duration=actual_duration,
        )

    def with_speed(self, new_speed: float) -> ClipInstance:
        new_timeline_duration = int(self.source_duration / new_speed)
        return replace(
            self,
            timeline_duration=new_timeline_duration,
            speed=new_speed,
        )

    def source_frames_per_timeline_frame(self) -> "Fraction":
        """C06/INT-12：每时间线帧消耗的源帧数（速度 × 源/时间线 fps 比）。

        fps 元数据缺失时退化为纯速度（legacy 1:1）。trim 边界换算统一
        使用本系数，鼠标修剪不再把 timeline 帧数当素材帧数。
        """
        from fractions import Fraction

        factor = Fraction(abs(self.effective_speed())).limit_denominator(100000)
        if self._source_fps_num and self._timeline_fps_num:
            source_fps = Fraction(self._source_fps_num, self._source_fps_den or 1)
            timeline_fps = Fraction(self._timeline_fps_num, self._timeline_fps_den or 1)
            if source_fps > 0 and timeline_fps > 0:
                factor *= source_fps / timeline_fps
        return factor

    def source_fps_fraction(self) -> "Fraction | None":
        """R06（第七轮审计）：素材自身帧率；元数据缺失返回 None。

        音频导出按源帧位置取源时必须用源时钟换算（调用方缺失时退化
        用时间线帧率），公开访问器避免各路径直接摸私有字段。
        """
        from fractions import Fraction

        if self._source_fps_num and self._source_fps_num > 0:
            return Fraction(self._source_fps_num, self._source_fps_den or 1)
        return None

    def is_reverse_playback(self) -> bool:
        """C05/C06：剪辑是否反向播放（SpeedParams.reverse 或负速度）。"""
        if isinstance(self.speed, SpeedParams):
            return bool(self.speed.reverse) or self.speed.speed < 0
        return float(self.speed) < 0

    def _source_over_timeline_fps_ratio(self) -> "Fraction":
        """R06/R11：source_fps / timeline_fps（缺元数据 = 1）。

        SpeedParams 分段映射的域是"speed 调整后的时间线帧"（不含 fps）。
        带 fps 元数据时，实际时间线帧 T 对应 speed 域 t = T × ratio，
        反向 t/ratio。正反映射与时长积分共用同一比例，不再分叉。
        """
        from fractions import Fraction

        if self._source_fps_num and self._timeline_fps_num:
            source_fps = Fraction(self._source_fps_num, self._source_fps_den or 1)
            timeline_fps = Fraction(
                self._timeline_fps_num, self._timeline_fps_den or 1
            )
            if source_fps > 0 and timeline_fps > 0:
                return source_fps / timeline_fps
        return Fraction(1)

    def source_window_mapping_updates(
        self, source_start: int, source_duration: int, timeline_offset: int,
    ) -> dict[str, Any]:
        """Preserve the original sampling clock when a source window is sliced.

        Source windows use integer frames; a cut may land between them.  CFR
        keeps a rational source phase, variable speed keeps the unrounded time
        origin in its original curve domain.  Translation/slip do not call this.
        """
        from fractions import Fraction

        updates: dict[str, Any] = {}
        if isinstance(self.speed, SpeedParams) and self.speed.speed_points:
            playback_offset = (
                self.source_start + self.source_duration - source_start - source_duration
                if self.is_reverse_playback() else source_start - self.source_start
            )
            updates["speed"] = self.speed.sliced_by_window(
                playback_offset, source_duration,
                previous_source_duration=self.source_duration,
                timeline_offset=float(timeline_offset * self._source_over_timeline_fps_ratio()),
            )
        else:
            phase = Fraction(self._source_frame_phase_num, self._source_frame_phase_den)
            phase += timeline_offset * self.source_frames_per_timeline_frame()
            if self.is_reverse_playback():
                phase += source_start + source_duration - self.source_start - self.source_duration
            else:
                phase += self.source_start - source_start
            updates["_source_frame_phase_num"] = phase.numerator
            updates["_source_frame_phase_den"] = phase.denominator
        return updates

    def with_left_edge_source_values(self, new_timeline_start: int) -> tuple[int, int]:
        """C06：时间线左缘移动后的 (new_source_start, new_source_duration)。

        方向感知：正向左修吃源入点（start += Δsrc）；反向左修吃最先播放
        的高端帧（start 不变、duration 收缩）。延伸（负 delta）对称。
        """
        delta = new_timeline_start - self.timeline_start
        if self.freeze_source_frame is None and 0 <= delta < self.timeline_duration:
            source = self.timeline_to_source_frame(new_timeline_start)
            if self.is_reverse_playback():
                return self.source_start, source - self.source_start + 1
            return source, self.source_end - source + 1
        if isinstance(self.speed, SpeedParams) and self.speed.speed_points:
            ratio = float(self._source_over_timeline_fps_ratio())
            src_delta = int(round(self.speed.source_position_at_time(delta * ratio, self.source_duration)
                                  - self.speed.source_position_at_time(0, self.source_duration)))
        else:
            src_delta = int(round(delta * float(self.source_frames_per_timeline_frame())))
        new_duration = self.source_duration - src_delta
        if self.is_reverse_playback():
            return self.source_start, max(1, new_duration)
        return self.source_start + src_delta, max(1, new_duration)

    def with_right_edge_source_values(self, new_timeline_duration: int) -> tuple[int, int]:
        """C06：时间线右缘移动后的 (new_source_start, new_source_duration)。

        方向感知：正向右修扩/缩源出点（start 不变）；反向右修吃最后播放
        的低端帧（start += Δsrc、duration 收缩）。
        """
        delta = new_timeline_duration - self.timeline_duration
        if self.freeze_source_frame is None and 0 < new_timeline_duration < self.timeline_duration:
            cut = self.timeline_to_source_frame(self.timeline_start + new_timeline_duration)
            last = self.timeline_to_source_frame(self.timeline_start + new_timeline_duration - 1)
            blend_handles = (last,)
            if isinstance(self.speed, SpeedParams) and self.speed.interpolation == "frame_blend":
                a, b, weight = self.timeline_to_source_frame_pair(
                    self.timeline_start + new_timeline_duration - 1)
                blend_handles = (self.source_start + a, self.source_start + b) if weight else (last,)
            if self.is_reverse_playback():
                start = min(cut + 1, *blend_handles)
                return start, self.source_end - start + 1
            end = max(cut - 1, *blend_handles)
            return self.source_start, end - self.source_start + 1
        if isinstance(self.speed, SpeedParams) and self.speed.speed_points:
            ratio = float(self._source_over_timeline_fps_ratio())
            src_delta = int(round(self.speed.source_position_at_time(new_timeline_duration * ratio, self.source_duration)
                                  - self.speed.source_position_at_time(self.timeline_duration * ratio, self.source_duration)))
        else:
            src_delta = int(round(delta * float(self.source_frames_per_timeline_frame())))
        if self.is_reverse_playback():
            # 反向右缘 = 源区间低端：收缩（delta<0）时低端上移 start -= src_delta
            # 且 duration 同步收缩；延伸（delta>0）对称。
            return self.source_start - src_delta, max(1, self.source_duration + src_delta)
        return self.source_start, max(1, self.source_duration + src_delta)

    def with_trim_left(self, new_timeline_start: int, new_source_start: int) -> ClipInstance:
        """Trim from left side: adjust start position.

        C10：fade envelope 随保留内容钳制——左修主要影响淡入，但极端
        缩短下淡出同样不能超过新时长。
        """
        timeline_delta = new_timeline_start - self.timeline_start
        source_delta = new_source_start - self.source_start
        new_timeline_duration = self.timeline_duration - timeline_delta
        new_source_duration = self.source_duration - source_delta
        if new_timeline_duration <= 0 or new_source_duration <= 0:
            raise ValueError("Trim would result in zero or negative duration")
        return replace(
            self,
            timeline_start=new_timeline_start,
            timeline_duration=new_timeline_duration,
            source_start=new_source_start,
            source_duration=new_source_duration,
            fade_in_duration=min(self.fade_in_duration, new_timeline_duration),
            fade_out_duration=min(self.fade_out_duration, new_timeline_duration),
            **self.source_window_mapping_updates(new_source_start, new_source_duration, timeline_delta),
        )

    def with_trim_right(self, new_timeline_duration: int, new_source_duration: int) -> ClipInstance:
        """Trim from right side: adjust duration.

        C10：同 with_trim_left——右修缩短时淡出（以及淡入）钳制到新时长。
        """
        if new_timeline_duration <= 0 or new_source_duration <= 0:
            raise ValueError("Trim would result in zero or negative duration")
        return replace(
            self,
            timeline_duration=new_timeline_duration,
            source_duration=new_source_duration,
            fade_in_duration=min(self.fade_in_duration, new_timeline_duration),
            fade_out_duration=min(self.fade_out_duration, new_timeline_duration),
            **self.source_window_mapping_updates(self.source_start, new_source_duration, 0),
        )

    def with_track(self, track_id: str) -> ClipInstance:
        return replace(self, track_id=track_id)

    def with_freeze_frame(self, frame: int | None) -> ClipInstance:
        """A2: 返回设置/清除冻结帧的新实例（frozen 模型，禁止原地修改）。

        Args:
            frame: 冻结的源帧号；None = 取消冻结、恢复正常播放。
        """
        return replace(self, freeze_source_frame=frame)

    def with_blend_mode(self, mode: "ClipBlendMode | str") -> ClipInstance:
        """A1: 返回指定混合模式的新实例（接受枚举或字符串，非法归一 NORMAL）。"""
        resolved = _parse_blend_mode(mode)
        return replace(self, blend_mode=resolved)

    def with_fade_curve(self, curve: "str | None") -> ClipInstance:
        """B16：设置淡化曲线形状（None=legacy 默认；白名单校验）。

        R05：这是**用户显式写**——无论设置已知曲线还是重置 None，
        都丢弃加载时保留的未支持原值（``_extra_fields["fade_curve"]``），
        用户意图优先于前向兼容回写。
        """
        return self.with_fade_curve_state(curve, None)

    @property
    def fade_curve_raw(self) -> Any:
        """R05：加载时保留的未支持曲线原值（无则 None）。

        ``fade_curve`` 为 None 且 ``_extra_fields`` 存有原始未支持值
        时返回原值——渲染按 legacy 默认降级，保存时原样回写。
        """
        if self.fade_curve is not None:
            return None
        return self._extra_fields.get("fade_curve")

    def with_fade_curve_state(
        self, curve: "str | None", raw_unknown: Any,
    ) -> ClipInstance:
        """R05：完整淡化曲线状态恢复（已知值 + 未支持原值）。

        undo/加载路径用：``curve=None, raw_unknown=<原值>`` 恢复
        "未支持原值 + legacy 降级渲染"状态；``raw_unknown=None`` 表示
        用户显式写（清空原值）。
        """
        if curve is not None and curve not in (
                "linear", "equal_power", "smooth"):
            raise ValueError(
                f"fade_curve must be linear|equal_power|smooth or None: "
                f"{curve!r}")
        clip = replace(self, fade_curve=curve)
        extras = {
            key: value for key, value in self._extra_fields.items()
            if key != "fade_curve"
        }
        if curve is None and raw_unknown is not None:
            extras["fade_curve"] = raw_unknown
        object.__setattr__(clip, "_extra_fields", extras)
        return clip

    # ── R09：两端独立曲线 ────────────────────────────────────────────

    @property
    def effective_fade_curve_in(self) -> "str | None":
        """淡入端生效曲线：独立字段 → 共享 fade_curve（B16）→ None。"""
        return self.fade_curve_in if self.fade_curve_in is not None \
            else self.fade_curve

    @property
    def effective_fade_curve_out(self) -> "str | None":
        """淡出端生效曲线：独立字段 → 共享 fade_curve（B16）→ None。"""
        return self.fade_curve_out if self.fade_curve_out is not None \
            else self.fade_curve

    def with_fade_end_curve(self, end: str, curve: "str | None",
                            ) -> ClipInstance:
        """R09：用户显式写单端曲线（另一端与共享字段不动；清该端原值）。"""
        return self.with_fade_end_curve_state(end, curve, None)

    def fade_end_curve_raw(self, end: str) -> Any:
        """R09：该端加载时保留的未支持原值（无则 None）。"""
        if end not in ("in", "out"):
            raise ValueError(f"end must be 'in' or 'out': {end!r}")
        field_name = f"fade_curve_{end}"
        if getattr(self, field_name) is not None:
            return None
        return self._extra_fields.get(field_name)

    def with_fade_end_curve_state(
        self, end: str, curve: "str | None", raw_unknown: Any,
    ) -> ClipInstance:
        """R09：单端完整状态恢复（undo/加载路径；另一端不受影响）。"""
        if end not in ("in", "out"):
            raise ValueError(f"end must be 'in' or 'out': {end!r}")
        if curve is not None and curve not in (
                "linear", "equal_power", "smooth"):
            raise ValueError(
                f"fade_curve_{end} must be linear|equal_power|smooth or "
                f"None: {curve!r}")
        field_name = f"fade_curve_{end}"
        clip = replace(self, **{field_name: curve})
        extras = {
            key: value for key, value in self._extra_fields.items()
            if key != field_name
        }
        if curve is None and raw_unknown is not None:
            extras[field_name] = raw_unknown
        object.__setattr__(clip, "_extra_fields", extras)
        return clip

    def with_audio_gain_db(self, gain_db: float) -> ClipInstance:
        """C3: 返回设置片段级音频增益的新实例（clamp [-48, +12] dB）。"""
        clamped = max(-48.0, min(12.0, float(gain_db)))
        return replace(self, audio_gain_db=clamped)

    def with_transform(self, new_transform: TransformParams) -> ClipInstance:
        """Create new instance with updated transform parameters."""
        return replace(self, transform=new_transform)

    def with_crop(self, new_crop: CropParams) -> ClipInstance:
        """Create new instance with updated crop parameters."""
        return replace(self, crop=new_crop)

    def with_opacity(self, new_opacity: float) -> ClipInstance:
        """Create new instance with updated opacity."""
        return replace(self, opacity=new_opacity)

    def with_speed_params(self, new_speed: SpeedParams) -> ClipInstance:
        """Create new instance with updated SpeedParams (affects timeline duration).

        R05：变速显著缩短时间线时长时，fade envelope 先行钳制到新时长，
        否则 replace() 在 __post_init__ 因 fade > duration 抛 ValueError
        （复查实测 fade40 片段 5x 变速崩溃）。
        """
        new_timeline_duration = new_speed.timeline_duration_for_source_duration(
            self.source_duration
        )
        return replace(
            self,
            timeline_duration=new_timeline_duration,
            speed=new_speed,
            fade_in_duration=min(self.fade_in_duration, new_timeline_duration),
            fade_out_duration=min(self.fade_out_duration, new_timeline_duration),
        )

    def with_enabled(self, is_enabled: bool) -> ClipInstance:
        """Create new instance with updated enabled state."""
        return replace(self, is_enabled=is_enabled)

    def with_linked_clip_id(self, linked_clip_id: str | None) -> ClipInstance:
        """Create new instance with updated linked_clip_id."""
        return replace(self, linked_clip_id=linked_clip_id)

    def with_fade_in(self, new_fade_in: int) -> ClipInstance:
        """
        Create new instance with updated fade in duration (Phase 1.2).

        Args:
            new_fade_in: New fade in duration in frames (must be >= 0 and <= timeline_duration)

        Returns:
            New ClipInstance with updated fade_in_duration
        """
        # The in-ramp cannot cross the existing out-ramp.
        clamped_fade_in = max(
            0,
            min(new_fade_in, self.timeline_duration - self.fade_out_duration),
        )
        return replace(self, fade_in_duration=clamped_fade_in)

    def with_fade_out(self, new_fade_out: int) -> ClipInstance:
        """
        Create new instance with updated fade out duration (Phase 1.2).

        Args:
            new_fade_out: New fade out duration in frames (must be >= 0 and <= timeline_duration)

        Returns:
            New ClipInstance with updated fade_out_duration
        """
        # The out-ramp cannot cross the existing in-ramp.
        clamped_fade_out = max(
            0,
            min(new_fade_out, self.timeline_duration - self.fade_in_duration),
        )
        return replace(self, fade_out_duration=clamped_fade_out)

    def with_fade(self, new_fade_in: int, new_fade_out: int) -> ClipInstance:
        """
        Create new instance with updated fade in/out durations (Phase 1.2).

        Args:
            new_fade_in: New fade in duration in frames
            new_fade_out: New fade out duration in frames

        Returns:
            New ClipInstance with updated fade durations
        """
        clamped_fade_in = max(0, min(new_fade_in, self.timeline_duration))
        clamped_fade_out = max(
            0,
            min(new_fade_out, self.timeline_duration - clamped_fade_in),
        )
        return replace(
            self,
            fade_in_duration=clamped_fade_in,
            fade_out_duration=clamped_fade_out,
        )

    def with_composite_id(self, composite_id: str | None) -> ClipInstance:
        """
        Create new instance with updated composite reference (P9.2).

        Args:
            composite_id: Composite graph ID to link, or None to unlink

        Returns:
            New ClipInstance with updated composite_id
        """
        return replace(self, composite_id=composite_id)

    def with_render_cache_id(self, render_cache_id: str | None) -> ClipInstance:
        """
        Create new instance with updated render cache reference.

        Args:
            render_cache_id: Render cache manifest ID to link, or None to clear

        Returns:
            New ClipInstance with updated render_cache_id
        """
        return replace(self, render_cache_id=render_cache_id)

    def with_label_color(self, label_color: str | None) -> ClipInstance:
        """
        Create new instance with updated label color.

        Args:
            label_color: Hex color string (e.g. '#FF0000') or None for default

        Returns:
            New ClipInstance with updated label_color
        """
        return replace(self, label_color=label_color)

    def with_flag_color(self, flag_color: str | None) -> ClipInstance:
        """W05：返回更新了彩色旗标的新实例（None = 清除，契约 D4）。"""
        return replace(self, flag_color=flag_color)

    def with_waveform_show_channels(self, show_channels: bool) -> ClipInstance:
        """Create a new clip with mono or separate-channel waveform display."""
        return replace(self, waveform_show_channels=bool(show_channels))

    def with_composite_bypassed(self, bypassed: bool) -> ClipInstance:
        """
        Create new instance with updated composite bypass state.

        Args:
            bypassed: True to bypass composite, False to enable

        Returns:
            New ClipInstance with updated composite_bypassed
        """
        return replace(self, composite_bypassed=bypassed)

    def with_asset_id(self, asset_id: str) -> ClipInstance:
        """
        Create new instance with updated asset reference.

        Used when upgrading a normal clip to a Comp Clip - the asset_id
        changes from the original source asset to the comp_clip asset.

        Args:
            asset_id: New asset ID to reference

        Returns:
            New ClipInstance with updated asset_id
        """
        return replace(self, asset_id=asset_id)

    def with_color_stack(self, color_stack_id: str | None, color_shared: bool = False) -> ClipInstance:
        """
        Create new instance with updated color stack reference (P9.1).

        Args:
            color_stack_id: Color stack ID to link, or None to unlink
            color_shared: Whether to share color with asset's default stack

        Returns:
            New ClipInstance with updated color_stack_id and color_shared
        """
        return replace(
            self,
            color_stack_id=color_stack_id,
            color_shared=color_shared,
        )

    # ==================== Keyframe Operations (P5.3) ====================

    def with_color_trim_stack(self, stack_id: str | None) -> "ClipInstance":
        """F4.1：设置/清除片段微调栈引用。"""
        return replace(self, color_trim_stack_id=stack_id or None)

    def get_param_at_frame(self, param_name: str, frame: int) -> Any:
        """
        Get parameter value at specific frame (P5.3).

        Supported parameters:
        - 'opacity': Clip opacity
        - 'transform.translate_x': X translate (pixels)
        - 'transform.translate_y': Y translate (pixels)
        - 'transform.scale_x': X scale
        - 'transform.scale_y': Y scale
        - 'transform.rotation': rotation (degrees)

        Args:
            param_name: Parameter path (e.g., 'opacity', 'transform.translate_x')
            frame: Timeline frame number

        Returns:
            Parameter value at frame (interpolated if animated)
        """
        if param_name in self.animated_params:
            return self.animated_params[param_name].get_value_at_frame(frame)

        # Get static value from transform or opacity
        if param_name == "opacity":
            return self.opacity
        elif param_name.startswith("transform."):
            attr = param_name.split(".", 1)[1]
            return getattr(self.transform, attr, 0.0)

        return 0.0

    def is_param_animated(self, param_name: str) -> bool:
        """Check if parameter has keyframes (P5.3)."""
        if param_name not in self.animated_params:
            return False
        return self.animated_params[param_name].is_animated()

    def has_keyframe_at(self, param_name: str, frame: int) -> bool:
        """Check if there's a keyframe at specific frame (P5.3)."""
        if param_name not in self.animated_params:
            return False
        return self.animated_params[param_name].has_keyframe_at(frame)

    def with_keyframe_added(
        self,
        param_name: str,
        frame: int,
        value: float,
        interpolation: InterpolationType = InterpolationType.LINEAR,
        min_value: float = 0.0,
        max_value: float = 1.0,
    ) -> ClipInstance:
        """
        Create new instance with keyframe added (P5.3).

        Args:
            param_name: Parameter path (e.g., 'opacity', 'transform.translate_x')
            frame: Frame number
            value: Value at this frame
            interpolation: Interpolation type
            min_value: Minimum value (for new AnimatableParam)
            max_value: Maximum value (for new AnimatableParam)

        Returns:
            New ClipInstance with keyframe added
        """
        if param_name in self.animated_params:
            # R11：旧工程的参数可能仍带 [0,100] 旧值域——先迁到权威
            # schema 再打点，否则新增负关键帧继续被钳 0。
            existing = _repair_clip_animated_range(
                param_name, self.animated_params[param_name])
            anim_param = existing.add_keyframe(frame, value, interpolation)
        else:
            # Get current static value as base
            if param_name == "opacity":
                base_value = self.opacity
            elif param_name.startswith("transform."):
                attr = param_name.split(".", 1)[1]
                base_value = getattr(self.transform, attr, value)
            else:
                base_value = value

            anim_param = AnimatableParam(
                base_value=base_value,
                min_value=min_value,
                max_value=max_value,
            ).add_keyframe(frame, value, interpolation)

        new_animated = dict(self.animated_params)
        new_animated[param_name] = anim_param

        return replace(self, animated_params=new_animated)

    def with_keyframe_removed(self, param_name: str, frame: int) -> ClipInstance:
        """
        Create new instance with keyframe removed (P5.3).

        Args:
            param_name: Parameter path
            frame: Frame number

        Returns:
            New ClipInstance with keyframe removed
        """
        if param_name not in self.animated_params:
            return self

        anim_param = self.animated_params[param_name].remove_keyframe(frame)

        new_animated = dict(self.animated_params)

        if not anim_param.is_animated():
            del new_animated[param_name]
        else:
            new_animated[param_name] = anim_param

        return replace(self, animated_params=new_animated)

    def with_keyframe_updated(
        self, param_name: str, frame: int, value: float,
        interpolation: Optional["InterpolationType"] = None,
    ) -> ClipInstance:
        """
        Create new instance with keyframe value updated (P5.3).

        Args:
            param_name: Parameter path
            frame: Frame number
            value: New value
            interpolation: Optional new easing curve (InterpolationType). If
                None the keyframe keeps its current interpolation.

        Returns:
            New ClipInstance with keyframe updated
        """
        if param_name not in self.animated_params:
            return self

        anim_param = self.animated_params[param_name].update_keyframe(frame, value, interpolation)

        new_animated = dict(self.animated_params)
        new_animated[param_name] = anim_param

        return replace(self, animated_params=new_animated)

    # ==================== Effect Operations ====================

    def with_effects(self, effects: tuple) -> ClipInstance:
        """Create a new instance with the effects tuple replaced.

        FX-C01 fix: must carry EVERY field, not a hand-picked subset.
        The previous hand-written constructor dropped ``label_color``,
        fade in/out, ``color_stack_id``/``color_shared``, ``composite_id``/
        ``composite_bypassed``, ``render_cache_id`` and all the FPS/PTS
        mapping fields whenever any effect was added/updated/removed —
        so adding a blur could silently kill the fade or detach the
        color stack. ``dataclasses.replace`` is the only safe way to
        swap a single field on a frozen dataclass without enumerating
        (and forgetting) fields by hand.
        """
        return dataclasses.replace(self, effects=effects)

    def with_effect_added(self, effect: Any) -> ClipInstance:
        """Create new instance with an effect added."""
        # Import EffectInstance for type checking
        from src.features.edit.core.effect_instance import EffectInstance

        # Determine the order for new effect
        existing_effects = self.effects if isinstance(self.effects, tuple) else tuple(self.effects) if self.effects else ()
        new_order = len(existing_effects)

        # If effect is EffectInstance, update its order
        if isinstance(effect, EffectInstance):
            effect_with_order = effect.with_order(new_order)
            new_effects = existing_effects + (effect_with_order,)
        else:
            # For dict-based effects, just append
            new_effects = existing_effects + (effect,)

        return self.with_effects(new_effects)

    def with_effect_updated(self, effect_id: str, new_effect: Any) -> ClipInstance:
        """Create new instance with an effect updated by ID."""
        existing_effects = self.effects if isinstance(self.effects, tuple) else tuple(self.effects) if self.effects else ()

        new_effects = []
        for effect in existing_effects:
            # Check if this is the effect to update
            if isinstance(effect, dict):
                if effect.get("effect_id") == effect_id:
                    new_effects.append(new_effect)
                else:
                    new_effects.append(effect)
            else:
                # EffectInstance or other object with effect_id attribute
                if hasattr(effect, 'effect_id') and effect.effect_id == effect_id:
                    new_effects.append(new_effect)
                else:
                    new_effects.append(effect)

        return self.with_effects(tuple(new_effects))

    def with_effect_removed(self, effect_id: str) -> ClipInstance:
        """Create new instance with an effect removed by ID."""
        existing_effects = self.effects if isinstance(self.effects, tuple) else tuple(self.effects) if self.effects else ()

        new_effects = []
        for effect in existing_effects:
            keep = True
            if isinstance(effect, dict):
                if effect.get("effect_id") == effect_id:
                    keep = False
            elif hasattr(effect, 'effect_id'):
                if effect.effect_id == effect_id:
                    keep = False

            if keep:
                new_effects.append(effect)

        return self.with_effects(tuple(new_effects))

    def get_effect_by_id(self, effect_id: str) -> Optional[Any]:
        """Get effect by ID."""
        effects = self.effects if isinstance(self.effects, tuple) else tuple(self.effects) if self.effects else ()

        for effect in effects:
            if isinstance(effect, dict):
                if effect.get("effect_id") == effect_id:
                    return effect
            elif hasattr(effect, 'effect_id'):
                if effect.effect_id == effect_id:
                    return effect

        return None

    def _to_dict_known(self) -> dict[str, Any]:
        # Serialize effects (handle both dict and EffectInstance)
        effects_list = []
        for effect in self.effects:
            if isinstance(effect, dict):
                effects_list.append(effect)
            elif hasattr(effect, 'to_dict'):
                effects_list.append(effect.to_dict())
            else:
                effects_list.append(effect)

        # Serialize animated_params (P5.3)
        animated_params_dict = {
            name: param.to_dict()
            for name, param in self.animated_params.items()
        }

        # Serialize speed (handle both SpeedParams and float)
        speed_value = self.speed
        if isinstance(speed_value, SpeedParams):
            speed_value = speed_value.to_dict()

        return {
            "clip_id": self.clip_id,
            "asset_id": self.asset_id,
            "track_id": self.track_id,
            "timeline_start": self.timeline_start,
            "timeline_duration": self.timeline_duration,
            "source_start": self.source_start,
            "source_duration": self.source_duration,
            "speed": speed_value,
            "linked_clip_id": self.linked_clip_id,
            "transform": self.transform.to_dict(),
            "crop": self.crop.to_dict(),
            "opacity": self.opacity,
            "animated_params": animated_params_dict,
            "effects": effects_list,
            "is_enabled": self.is_enabled,
            "label_color": self.label_color,
            # W05：与 label_color 同型直出；旧工程缺该键读取 → None（契约 D4）
            "flag_color": self.flag_color,
            "waveform_show_channels": self.waveform_show_channels,
            # Phase 1.2: Fade in/out
            "fade_in_duration": self.fade_in_duration,
            "fade_out_duration": self.fade_out_duration,
            # B16：淡化曲线——非默认才写出（旧工程序列化字节不变）
            "fade_curve": self.fade_curve,
            # R09：两端独立曲线（None 不改变 B16 共享字段语义）
            "fade_curve_in": self.fade_curve_in,
            "fade_curve_out": self.fade_curve_out,
            # F3.3：采样级字段——非默认才写出（旧工程序列化字节不变；
            # 字段语义声明见 sample_time.py；旧版读新工程保留于 _extra_fields）
            "audio_sample_offset": self.audio_sample_offset or None,
            "source_sample_offset": self.source_sample_offset or None,
            "fade_in_samples": self.fade_in_samples,
            "fade_out_samples": self.fade_out_samples,
            # P0-3：非破坏性处理栈——非默认才写出（旧工程字节不变）。
            "audio_edit": self.audio_edit,
            # F4.1: 片段微调栈（非默认才写 = 旧工程字节不变）
            "color_trim_stack_id": self.color_trim_stack_id,
            # P9.1: Color page linkage
            "color_stack_id": self.color_stack_id,
            "color_shared": self.color_shared,
            # P9.2: Composite page linkage
            "composite_id": self.composite_id,
            "composite_bypassed": self.composite_bypassed,
            # Render Cache linkage
            "render_cache_id": self.render_cache_id,
            # A2: 冻结帧
            "freeze_source_frame": self.freeze_source_frame,
            # A1: 图层混合模式（写字符串值）
            "blend_mode": self.blend_mode.value if isinstance(self.blend_mode, ClipBlendMode) else str(self.blend_mode),
            # C3: 片段级音频增益
            "audio_gain_db": self.audio_gain_db,
            # TRAW（批 2）：RAW 参数
            "raw": self.raw.to_dict() if self.raw is not None else None,
            # FPS metadata for precise frame mapping
            "source_fps_num": self._source_fps_num,
            "source_fps_den": self._source_fps_den,
            "timeline_fps_num": self._timeline_fps_num,
            "timeline_fps_den": self._timeline_fps_den,
            "source_pts_index": list(self._source_pts_index) if self._source_pts_index else None,
            "source_frame_phase_num": self._source_frame_phase_num,
            "source_frame_phase_den": self._source_frame_phase_den,
        }

    @staticmethod
    def _from_dict_known(data: dict[str, Any]) -> ClipInstance:
        # Deserialize speed
        speed_data = data.get("speed", 1.0)
        if isinstance(speed_data, dict):
            speed = SpeedParams.from_dict(speed_data)
        else:
            speed = speed_data

        # Deserialize effects
        effects_data = data.get("effects", [])
        effects_list = []
        for effect_data in effects_data:
            if isinstance(effect_data, dict):
                # Check if it's EffectInstance format
                if "effect_type" in effect_data:
                    from src.features.edit.core.effect_instance import (
                        EffectInstance,
                    )
                    from src.features.edit.core.effect_registry import (
                        NON_ATTACHABLE_VIDEO_EFFECT_TYPES,
                    )
                    instance = EffectInstance.from_dict(effect_data)
                    # T-P1-20：legacy ghost FX（transform/crop/opacity/
                    # vignette）从未接渲染后端——旧工程载入时剔除并告警，
                    # 而不是留下零视觉效果、导出被 preflight 阻断的实例。
                    # 剔除视觉无损（这些实例从未参与渲染）。
                    if instance.effect_type in NON_ATTACHABLE_VIDEO_EFFECT_TYPES:
                        logger.warning(
                            "ClipInstance.from_dict: 剔除 legacy ghost 效果 "
                            "「%s」（clip=%s）——该效果从未接渲染后端，"
                            "真实能力为 clip-native 参数",
                            instance.effect_type,
                            data.get("clip_id", "?"),
                        )
                        continue
                    effects_list.append(instance)
                else:
                    effects_list.append(effect_data)
            else:
                effects_list.append(effect_data)

        # Deserialize animated_params (P5.3)
        animated_params_data = data.get("animated_params", {})
        animated_params = {
            name: AnimatableParam.from_dict(param_data)
            for name, param_data in animated_params_data.items()
        }

        return ClipInstance(
            clip_id=data.get("clip_id", str(uuid.uuid4())),
            asset_id=data["asset_id"],
            track_id=data["track_id"],
            timeline_start=data["timeline_start"],
            timeline_duration=data["timeline_duration"],
            source_start=data["source_start"],
            source_duration=data["source_duration"],
            speed=speed,
            linked_clip_id=data.get("linked_clip_id"),
            transform=TransformParams.from_dict(data.get("transform", {})),
            crop=CropParams.from_dict(data.get("crop", {})),
            opacity=data.get("opacity", 1.0),
            animated_params=animated_params,
            effects=tuple(effects_list),
            is_enabled=data.get("is_enabled", True),
            label_color=data.get("label_color"),
            # W05：旧工程缺键 → None；未知颜色值保留（UI fallback 绘制）
            flag_color=data.get("flag_color"),
            waveform_show_channels=bool(data.get("waveform_show_channels", False)),
            # Phase 1.2: Fade in/out
            fade_in_duration=data.get("fade_in_duration", 0),
            fade_out_duration=data.get("fade_out_duration", 0),
            # B16：曲线缺键 → None（legacy）。构造校验拒绝未知新写；
            # 损坏/未来值清洗为 None（freeze_source_frame 同先例），
            # 不让坏字段阻断整个工程加载。R09：两端独立字段同策略。
            fade_curve=(
                data.get("fade_curve")
                if data.get("fade_curve") in (
                    "linear", "equal_power", "smooth") else None),
            fade_curve_in=(
                data.get("fade_curve_in")
                if data.get("fade_curve_in") in (
                    "linear", "equal_power", "smooth") else None),
            fade_curve_out=(
                data.get("fade_curve_out")
                if data.get("fade_curve_out") in (
                    "linear", "equal_power", "smooth") else None),
            # F3.3：采样级字段（缺省=旧工程；负值/非 int 防御回退默认，
            # 不静默舍入语义——非法载荷回退 0/None 并保持可检测）
            audio_sample_offset=_int_or_default(
                data.get("audio_sample_offset"), 0),
            source_sample_offset=_int_or_default(
                data.get("source_sample_offset"), 0),
            fade_in_samples=_int_or_default(
                data.get("fade_in_samples"), None),
            fade_out_samples=_int_or_default(
                data.get("fade_out_samples"), None),
            # P0-3：处理栈（缺失/非 dict → None；未知 processor 不在载入时
            # 拒绝，由 audio_edit.validate 在应用/交付处逐条阻断诊断）。
            audio_edit=(
                dict(_raw_audio_edit)
                if isinstance((_raw_audio_edit := data.get("audio_edit")), dict)
                else None),
            color_trim_stack_id=data.get("color_trim_stack_id") or None,
            # P9.1: Color page linkage
            color_stack_id=data.get("color_stack_id"),
            color_shared=data.get("color_shared", False),
            # P9.2: Composite page linkage
            composite_id=data.get("composite_id"),
            composite_bypassed=data.get("composite_bypassed", False),
            # Render Cache linkage
            render_cache_id=data.get("render_cache_id"),
            # A2: 冻结帧（旧工程缺失 → None，正常播放）。负值/坏值清洗
            # 为 None——损坏数据不该让整个工程加载失败。
            freeze_source_frame=(
                int(_raw_freeze)
                if isinstance((_raw_freeze := data.get("freeze_source_frame")), (int, float))
                and _raw_freeze >= 0 else None),
            # TRAW（批 2）：RAW 参数（缺失 → None）
            raw=(
                RawParams.from_dict(data["raw"])
                if isinstance(data.get("raw"), dict) else None),
            # A1: 混合模式（缺失/非法 → NORMAL，旧工程兼容）
            blend_mode=_parse_blend_mode(data.get("blend_mode")),
            # C3: 片段级音频增益（旧工程缺失 → 0；超范围 clamp 而非拒载）
            audio_gain_db=max(-48.0, min(12.0, float(
                data.get("audio_gain_db", 0.0) or 0.0))),
            # FPS metadata for precise frame mapping
            _source_fps_num=data.get("source_fps_num"),
            _source_fps_den=data.get("source_fps_den"),
            _timeline_fps_num=data.get("timeline_fps_num"),
            _timeline_fps_den=data.get("timeline_fps_den"),
            _source_pts_index=tuple(data["source_pts_index"]) if data.get("source_pts_index") else None,
            _source_frame_phase_num=int(data.get("source_frame_phase_num", 0)),
            _source_frame_phase_den=int(data.get("source_frame_phase_den", 1)),
        )

    def to_dict(self) -> dict[str, Any]:
        """R02：序列化已知字段后回写未知扩展字段（已知键优先）。

        R05：``fade_curve`` 未被用户改写（None）且 extras 保留着加载
        时的未支持原值时，**原样回写原值**——新版工程在旧程序中
        打开→保存不丢曲线定义（渲染仍按 legacy 降级，不冒充已支持）。
        """
        result = self._to_dict_known()
        # R05/R09：未支持原值回写（fade_curve* 为 None 且 extras 存有
        # 原始未支持值时写出原值；用户改写后 extras 已清）
        for curve_key in ("fade_curve", "fade_curve_in", "fade_curve_out"):
            if getattr(self, curve_key) is None and \
                    curve_key in self._extra_fields:
                result[curve_key] = self._extra_fields[curve_key]
        for key, value in self._extra_fields.items():
            result.setdefault(key, value)
        return result

    _KNOWN_CLIP_KEYS = frozenset({
        "clip_id", "asset_id", "track_id", "timeline_start",
        "timeline_duration", "source_start", "source_duration", "speed",
        "linked_clip_id", "transform", "crop", "opacity", "animated_params",
        "effects", "is_enabled", "label_color", "waveform_show_channels",
        "fade_in_duration", "fade_out_duration", "color_stack_id",
        "color_shared", "composite_id", "composite_bypassed",
        "render_cache_id", "freeze_source_frame", "blend_mode",
        "audio_gain_db", "raw", "source_fps_num", "source_fps_den",
        "timeline_fps_num", "timeline_fps_den", "source_pts_index",
        "source_frame_phase_num", "source_frame_phase_den", "audio_edit",
        # R05 注意：fade_curve 有意**不**列入——未支持原值经
        # _extra_fields 保留才能在打开→保存往返中不丢（from_dict 会
        # 把已知值/None 从 extras 剔除，维持不变式）。
    })

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ClipInstance:
        """R02：反序列化 + 保留未识别键（未来 schema 字段 roundtrip 不丢）。"""
        clip = ClipInstance._from_dict_known(data)
        # R11（第七轮审计）：已知 clip 动画参数值域加载即迁移到权威
        # schema——旧工程 [0,100] 域下负平移/旋转关键帧曾全部钳 0。
        repaired = {}
        for name, param in clip.animated_params.items():
            if name == "opacity" or name.startswith("transform."):
                repaired[name] = _repair_clip_animated_range(name, param)
            else:
                repaired[name] = param
        if repaired != clip.animated_params:
            from dataclasses import replace as _dc_replace

            clip = _dc_replace(clip, animated_params=repaired)
        extras = {
            key: value for key, value in data.items()
            if key not in ClipInstance._KNOWN_CLIP_KEYS
        }
        # R05：未支持曲线原值保留在 extras（不在 _KNOWN_CLIP_KEYS 是
        # 有意的——原值要能随工程往返）。已知值/None 不入 extras，
        # 保持"extras 含 fade_curve* ⟺ 未支持原值待回写"的不变式。
        # R09：两端独立字段同策略。
        for _curve_key in ("fade_curve", "fade_curve_in", "fade_curve_out"):
            raw_curve = data.get(_curve_key)
            if raw_curve is None or raw_curve in (
                    "linear", "equal_power", "smooth"):
                extras.pop(_curve_key, None)
            else:
                logger.warning(
                    "ClipInstance.from_dict: 未支持 %s %r（clip=%s）"
                    "——渲染按 legacy 默认降级，原值保留待保存回写",
                    _curve_key, raw_curve, data.get("clip_id", "?"),
                )
        if extras:
            object.__setattr__(clip, "_extra_fields", extras)
        return clip

    def __hash__(self) -> int:
        return hash(self.clip_id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ClipInstance):
            return NotImplemented
        return self.clip_id == other.clip_id


@dataclass(frozen=True)
class TransitionInstance:
    """转场实例。

    A transition normally blends two adjacent clips.  At the beginning or
    end of a clip it may also be one-sided: ``right_clip_id`` only means a
    fade-in from the sequence background, while ``left_clip_id`` only means a
    fade-out to the sequence background.
    """
    transition_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    transition_type: str = "cross_dissolve"
    track_id: str = ""
    left_clip_id: str = ""
    right_clip_id: str = ""
    center_frame: int = 0
    duration: int = 0
    alignment: str = "centered"
    params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # AUD-19104：构造即复制 params dict（frozen 模型不内嵌可变外部引用）。
        object.__setattr__(self, "params", dict(self.params))
        if not self.transition_id:
            raise ValueError("transition_id cannot be empty")
        # Audio transitions (audio:* namespace) live alongside video ones on the
        # same TransitionInstance — only the renderer namespace separates them.
        if (
            self.transition_type not in TRANSITION_TYPE_OPTIONS
            and self.transition_type not in AUDIO_TRANSITION_TYPE_OPTIONS
        ):
            # C2: 用户自定义转场（user_*，已注册的变体类型）合法
            from src.features.edit.core.custom_transitions import (
                resolve_user_transition,
            )
            if resolve_user_transition(self.transition_type) is None:
                raise ValueError(
                    f"Invalid transition_type: {self.transition_type}")
        if not self.track_id:
            raise ValueError("track_id cannot be empty")
        if not self.left_clip_id and not self.right_clip_id:
            raise ValueError("at least one transition clip must be provided")
        if self.duration <= 0:
            raise ValueError(f"duration must be positive: {self.duration}")
        valid_align = ("centered", "start_on_cut", "end_on_cut")
        if self.alignment not in valid_align:
            raise ValueError(f"Invalid alignment: {self.alignment}")

    @property
    def is_one_sided(self) -> bool:
        """Whether this is a clip-edge transition rather than a cut blend."""
        return not (self.left_clip_id and self.right_clip_id)

    @property
    def is_audio(self) -> bool:
        """Whether this transition lives in the ``audio:`` namespace.

        Consumers that only handle video (e.g. :mod:`timeline_render_plan`) use
        this to skip audio transitions; the audio mixer/exporter use it to pick
        them up.
        """
        return self.transition_type in AUDIO_TRANSITION_TYPE_OPTIONS

    @property
    def start_frame(self) -> int:
        if self.alignment == "centered":
            return self.center_frame - self.duration // 2
        elif self.alignment == "start_on_cut":
            return self.center_frame
        else:
            return self.center_frame - self.duration

    @property
    def end_frame(self) -> int:
        return self.start_frame + self.duration

    def to_dict(self) -> dict[str, Any]:
        return {"transition_id": self.transition_id, "transition_type": self.transition_type,
            "track_id": self.track_id, "left_clip_id": self.left_clip_id,
            "right_clip_id": self.right_clip_id, "center_frame": self.center_frame,
            "duration": self.duration, "alignment": self.alignment, "params": self.params}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> TransitionInstance:
        return TransitionInstance(
            transition_id=data.get("transition_id", str(uuid.uuid4())),
            transition_type=data.get("transition_type", "cross_dissolve"),
            track_id=data["track_id"], left_clip_id=data["left_clip_id"],
            right_clip_id=data["right_clip_id"], center_frame=data["center_frame"],
            duration=data["duration"], alignment=data.get("alignment", "centered"),
            params=data.get("params", {}))

    def __hash__(self) -> int:
        return hash(self.transition_id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TransitionInstance):
            return NotImplemented
        return self.transition_id == other.transition_id


# ─────────────────────────────────────────────────────────────────────────────
# Subtitle Clip (P10.3 - 字幕轨道)
# ─────────────────────────────────────────────────────────────────────────────
# 字幕片段：独立于 ClipInstance（无需 asset_id/source_start/crop 等视频字段），
# 直接存储文字、字体、变换、颜色等参数。字段命名与 TextNode/TransformNode
# 严格对齐，使得 viewer_handles 的控制手柄可以零改动复用（手柄函数按
# translate_x/scale_x/rotation/pivot_* 等键名读取参数）。
#
# 渲染统一走 subtitle_renderer.render_subtitle_to_rgba()，提取自
# TextNode._render_text_pixels，输出 bbox 大小的 RGBA float32 buffer。
# ─────────────────────────────────────────────────────────────────────────────

# 字重/对齐枚举值（与 text_node.py 同步）
_SUBTITLE_FONT_WEIGHTS: tuple[str, ...] = ("light", "normal", "demibold", "bold", "black")
_SUBTITLE_HALIGNS: tuple[str, ...] = ("left", "center", "right")
_SUBTITLE_VALIGNS: tuple[str, ...] = ("top", "middle", "bottom")
_SUBTITLE_PIVOT_MODES: tuple[str, ...] = ("center", "custom")


@dataclass(frozen=True)
@preserves_extras
class SubtitleClip:
    """
    字幕片段。时间线字幕轨道上的一个文字片段。

    时间映射：
    - timeline_start + timeline_duration: 时间线位置和时长

    参数（与 TextNode 同名同语义，保证手柄/UI 复用）：
    - 文字：text / font_family / font_size / font_weight / italic
    - 排版：halign / valign / letter_spacing / line_spacing
    - 变换：translate_x/y / scale_x/y / rotation / pivot_mode/x/y
    - 颜色：fill_r/g/b/a（HDR float）
    - 描边：outline_enable / outline_width / outline_r/g/b/a
    - 阴影：shadow_enable / shadow_offset_x/y / shadow_blur / shadow_r/g/b/a
    """
    # ── Identity & timing ──
    clip_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    track_id: str = ""
    timeline_start: int = 0
    timeline_duration: int = 72  # 默认 3s @ 24fps，实际由偏好设置决定
    # ── Content ──
    text: str = ""
    # ── Font ──
    font_family: str = "Arial"
    font_size: int = 0  # 0 = 自适应（按画布高度比例，1080p≈60px）
    font_weight: str = "normal"
    italic: bool = False
    # ── Layout ──
    halign: str = "center"
    valign: str = "bottom"
    letter_spacing: float = 0.0
    line_spacing: float = 1.0
    # ── Transform (pixel space, mirrors TransformNode so handles work unchanged) ──
    translate_x: float = 0.0
    translate_y: float = 0.0
    scale_x: float = 1.0
    scale_y: float = 1.0
    rotation: float = 0.0
    pivot_mode: str = "center"
    pivot_x: float = 0.0
    pivot_y: float = 0.0
    # ── Fill color (HDR float, unclamped) ──
    fill_r: float = 1.0
    fill_g: float = 1.0
    fill_b: float = 1.0
    fill_a: float = 1.0
    # ── Outline ──
    outline_enable: bool = False
    outline_width: float = 4.0
    outline_r: float = 0.0
    outline_g: float = 0.0
    outline_b: float = 0.0
    outline_a: float = 1.0
    # ── Drop shadow ──
    shadow_enable: bool = False
    shadow_offset_x: float = 8.0
    shadow_offset_y: float = 8.0
    shadow_blur: float = 8.0
    shadow_r: float = 0.0
    shadow_g: float = 0.0
    shadow_b: float = 0.0
    shadow_a: float = 1.0
    # ── State ──
    is_enabled: bool = True
    label_color: str | None = None
    # P0 display policy: native subtitle, bound graphic, both (diagnostic),
    # or disabled while retaining the source cue for delivery.
    display_mode: str = "native"
    # M13.7：cue 动画声明（None = 无动画；{"roll": True} /
    # {"fade_in_frames": n, "fade_out_frames": n}——渲染侧
    # subtitle_renderer.apply_cue_animation 消费，随 to_dict/from_dict
    # 稀疏往返，旧工程无键 = None）。
    animation: dict | None = None
    # SUB-P2-01/06: Phase 2A layered caption model.
    #   * style_override: per-cue CaptionStyle override (None = fully inherit
    #     the track's default_style). The 34 flat style fields above remain the
    #     render-time source of truth (resolver flattens track⊕override→flat);
    #     style_override is the *intent* layer that survives preset edits.
    #   * caption_metadata: per-cue review/provenance metadata (speaker/status/
    #     note/locked/source_span/...). Never affects pixels; drives QC +
    #     collaboration (SUB-P2-12).
    # Both default to "unset" so a legacy cue behaves exactly as before.
    style_override: Optional[CaptionStyle] = None
    caption_metadata: CaptionCueMetadata = field(default_factory=CaptionCueMetadata)
    # DESK-04A：未来 schema 未知键保留（load→save 不丢；与 ClipInstance.
    # _extra_fields / captions preserve_unknown_keys 同契约，走共享基础设施）
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    # to_dict 全部键（含条件写出的 animation 与 caption schema 版本戳）+
    # from_dict 消费键。legacy 字幕形状（v0/v1）由 captions.schema 的
    # upgrade_captions_in_project 在反序列化前迁移，不产生本层迁移键。
    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "clip_id", "track_id", "timeline_start", "timeline_duration",
        "text", "font_family", "font_size", "font_weight", "italic",
        "halign", "valign", "letter_spacing", "line_spacing",
        "translate_x", "translate_y", "scale_x", "scale_y", "rotation",
        "pivot_mode", "pivot_x", "pivot_y",
        "fill_r", "fill_g", "fill_b", "fill_a",
        "outline_enable", "outline_width",
        "outline_r", "outline_g", "outline_b", "outline_a",
        "shadow_enable", "shadow_offset_x", "shadow_offset_y", "shadow_blur",
        "shadow_r", "shadow_g", "shadow_b", "shadow_a",
        "is_enabled", "label_color", "display_mode", "animation",
        "caption_schema_version", "style_override", "caption_metadata",
    })

    def __post_init__(self) -> None:
        if not self.clip_id:
            raise ValueError("clip_id cannot be empty")
        if not self.track_id:
            raise ValueError("track_id cannot be empty")
        if self.timeline_start < 0:
            raise ValueError(f"timeline_start cannot be negative: {self.timeline_start}")
        if self.timeline_duration <= 0:
            raise ValueError(f"timeline_duration must be positive: {self.timeline_duration}")
        if self.font_weight not in _SUBTITLE_FONT_WEIGHTS:
            raise ValueError(f"Invalid font_weight: {self.font_weight}")
        if self.halign not in _SUBTITLE_HALIGNS:
            raise ValueError(f"Invalid halign: {self.halign}")
        if self.valign not in _SUBTITLE_VALIGNS:
            raise ValueError(f"Invalid valign: {self.valign}")
        if self.pivot_mode not in _SUBTITLE_PIVOT_MODES:
            raise ValueError(f"Invalid pivot_mode: {self.pivot_mode}")
        if self.display_mode not in ("native", "graphic", "both", "disabled"):
            raise ValueError(f"Invalid subtitle display_mode: {self.display_mode}")
        if self.font_size < 0:
            raise ValueError(f"font_size must be >= 0: {self.font_size}")
        # SUB-P2-01: validate Phase 2A layered fields.
        if self.style_override is not None and not isinstance(self.style_override, CaptionStyle):
            raise TypeError(
                f"style_override must be a CaptionStyle or None, got "
                f"{type(self.style_override).__name__}"
            )
        if not isinstance(self.caption_metadata, CaptionCueMetadata):
            raise TypeError(
                f"caption_metadata must be a CaptionCueMetadata, got "
                f"{type(self.caption_metadata).__name__}"
            )

    @property
    def timeline_end(self) -> int:
        return self.timeline_start + self.timeline_duration

    # ── Generic with_* helpers (used by commands & handle controller) ──

    def with_timeline_start(self, start: int) -> SubtitleClip:
        if start < 0:
            raise ValueError(f"timeline_start cannot be negative: {start}")
        return self._replace(timeline_start=start)

    def with_timeline_duration(self, duration: int) -> SubtitleClip:
        if duration <= 0:
            raise ValueError(f"timeline_duration must be positive: {duration}")
        return self._replace(timeline_duration=duration)

    def with_trim_left(self, delta: int) -> SubtitleClip:
        """Trim from left edge (shift start without moving end visually)."""
        new_start = self.timeline_start + delta
        new_duration = self.timeline_duration - delta
        if new_start < 0 or new_duration <= 0:
            raise ValueError(f"Invalid trim left: start={new_start}, duration={new_duration}")
        return self._replace(timeline_start=new_start, timeline_duration=new_duration)

    def with_trim_right(self, delta: int) -> SubtitleClip:
        """Trim from right edge (extend/shrink duration)."""
        new_duration = self.timeline_duration + delta
        if new_duration <= 0:
            raise ValueError(f"Invalid trim right: duration={new_duration}")
        return self._replace(timeline_duration=new_duration)

    def with_track(self, track_id: str) -> SubtitleClip:
        return self._replace(track_id=track_id)

    def with_label_color(self, color: str | None) -> SubtitleClip:
        return self._replace(label_color=color)

    def with_text(self, text: str) -> SubtitleClip:
        return self._replace(text=text)

    def with_params(self, **kwargs: Any) -> SubtitleClip:
        """Update multiple subtitle parameters atomically (used by handle drag merge)."""
        return self._replace(**kwargs)

    def _replace(self, **kwargs: Any) -> SubtitleClip:
        """Create a new SubtitleClip with updated fields (frozen-safe)."""
        from dataclasses import replace as _dc_replace
        return _dc_replace(self, **kwargs)

    # ── Phase 2A layered-caption helpers ──

    def resolved_style(self, track_default: CaptionStyle) -> CaptionStyle:
        """Resolve this cue's effective style: override ⊕ track default.

        Returns a *fully-populated* CaptionStyle (no None among fields the
        override or default set). Fields neither sets remain None here; the
        renderer's resolver fills legacy defaults at flatten time. Use this
        when you need the merged intent (e.g. workspace display, export
        manifest) rather than for pixel rendering.
        """
        if self.style_override is None:
            return track_default
        return self.style_override.merge_over(track_default)

    def with_style_override(self, style: Optional[CaptionStyle]) -> SubtitleClip:
        """Return a copy with a new per-cue style override (None = inherit)."""
        return self._replace(style_override=style)

    def with_caption_metadata(self, metadata: CaptionCueMetadata) -> SubtitleClip:
        """Return a copy with updated review/provenance metadata."""
        return self._replace(caption_metadata=metadata)

    def is_edit_locked(self) -> bool:
        """True if this cue's review status forbids editing (SUB-P2-12)."""
        return self.caption_metadata.is_edit_locked()

    def active_word_index(self, timeline_frame: int) -> int | None:
        """Return the aligned word active at ``timeline_frame``.

        The helper keeps frame ownership in the immutable caption model; the
        preview/export layers can use it for a live highlight without
        reimplementing half-open timing rules.
        """
        from src.features.edit.core.captions.model import active_caption_word
        return active_caption_word(
            self.caption_metadata.word_timings, int(timeline_frame)
        )

    # ── Serialization ──

    def to_dict(self) -> dict[str, Any]:
        return {
            "clip_id": self.clip_id,
            "track_id": self.track_id,
            "timeline_start": self.timeline_start,
            "timeline_duration": self.timeline_duration,
            "text": self.text,
            "font_family": self.font_family,
            "font_size": self.font_size,
            "font_weight": self.font_weight,
            "italic": self.italic,
            "halign": self.halign,
            "valign": self.valign,
            "letter_spacing": self.letter_spacing,
            "line_spacing": self.line_spacing,
            "translate_x": self.translate_x,
            "translate_y": self.translate_y,
            "scale_x": self.scale_x,
            "scale_y": self.scale_y,
            "rotation": self.rotation,
            "pivot_mode": self.pivot_mode,
            "pivot_x": self.pivot_x,
            "pivot_y": self.pivot_y,
            "fill_r": self.fill_r, "fill_g": self.fill_g,
            "fill_b": self.fill_b, "fill_a": self.fill_a,
            "outline_enable": self.outline_enable,
            "outline_width": self.outline_width,
            "outline_r": self.outline_r, "outline_g": self.outline_g,
            "outline_b": self.outline_b, "outline_a": self.outline_a,
            "shadow_enable": self.shadow_enable,
            "shadow_offset_x": self.shadow_offset_x,
            "shadow_offset_y": self.shadow_offset_y,
            "shadow_blur": self.shadow_blur,
            "shadow_r": self.shadow_r, "shadow_g": self.shadow_g,
            "shadow_b": self.shadow_b, "shadow_a": self.shadow_a,
            "is_enabled": self.is_enabled,
            "label_color": self.label_color,
            "display_mode": self.display_mode,
            # M13.7：动画声明稀疏写出（None 不落键——旧工程形状不变）
            **({"animation": dict(self.animation)}
               if self.animation else {}),
            # SUB-P2-01/10: Phase 2A layered fields. Flat style fields above
            # remain (dual-write) so legacy readers keep working; the layered
            # block is the *intent* source for preset/inheritance. Stamping the
            # caption schema version lets future loaders migrate explicitly.
            "caption_schema_version": 2,
            "style_override": (self.style_override.to_dict()
                               if self.style_override is not None else None),
            "caption_metadata": self.caption_metadata.to_dict(),
            **self._extra_fields,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SubtitleClip:
        return SubtitleClip(
            clip_id=data.get("clip_id", str(uuid.uuid4())),
            track_id=data.get("track_id", ""),
            timeline_start=data.get("timeline_start", 0),
            timeline_duration=data.get("timeline_duration", 72),
            text=data.get("text", ""),
            font_family=data.get("font_family", "Arial"),
            # Keep missing legacy fields aligned with SubtitleClip's 0 =
            # auto-fit sentinel; the renderer resolves the actual pixel size
            # from the timeline canvas height.
            font_size=data.get("font_size", 0),
            font_weight=data.get("font_weight", "normal"),
            italic=data.get("italic", False),
            halign=data.get("halign", "center"),
            valign=data.get("valign", "bottom"),
            letter_spacing=data.get("letter_spacing", 0.0),
            line_spacing=data.get("line_spacing", 1.0),
            translate_x=data.get("translate_x", 0.0),
            translate_y=data.get("translate_y", 0.0),
            scale_x=data.get("scale_x", 1.0),
            scale_y=data.get("scale_y", 1.0),
            rotation=data.get("rotation", 0.0),
            pivot_mode=data.get("pivot_mode", "center"),
            pivot_x=data.get("pivot_x", 0.0),
            pivot_y=data.get("pivot_y", 0.0),
            fill_r=data.get("fill_r", 1.0), fill_g=data.get("fill_g", 1.0),
            fill_b=data.get("fill_b", 1.0), fill_a=data.get("fill_a", 1.0),
            outline_enable=data.get("outline_enable", False),
            outline_width=data.get("outline_width", 4.0),
            outline_r=data.get("outline_r", 0.0), outline_g=data.get("outline_g", 0.0),
            outline_b=data.get("outline_b", 0.0), outline_a=data.get("outline_a", 1.0),
            shadow_enable=data.get("shadow_enable", False),
            shadow_offset_x=data.get("shadow_offset_x", 8.0),
            shadow_offset_y=data.get("shadow_offset_y", 8.0),
            shadow_blur=data.get("shadow_blur", 8.0),
            shadow_r=data.get("shadow_r", 0.0), shadow_g=data.get("shadow_g", 0.0),
            shadow_b=data.get("shadow_b", 0.0), shadow_a=data.get("shadow_a", 1.0),
            is_enabled=data.get("is_enabled", True),
            label_color=data.get("label_color"),
            display_mode=str(data.get("display_mode", "native") or "native"),
            animation=(dict(data["animation"])
                       if isinstance(data.get("animation"), dict)
                       else None),
            # SUB-P2-01/10: Phase 2A layered fields. Absent on legacy clips →
            # defaults (style_override=None, empty metadata), so v1 projects
            # load losslessly.
            style_override=(CaptionStyle.from_dict(data["style_override"])
                            if data.get("style_override") else None),
            caption_metadata=CaptionCueMetadata.from_dict(data.get("caption_metadata")),
            _extra_fields=split_extras(data, SubtitleClip._KNOWN_KEYS),
        )

    def __hash__(self) -> int:
        return hash(self.clip_id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SubtitleClip):
            return NotImplemented
        return self.clip_id == other.clip_id


@dataclass(frozen=True)
class Send:
    """音频发送（W24/MX-14）— 把一条 strip 的信号发送到 Bus/Aux。

    Attributes:
        send_id: 唯一标识
        target_bus_id: 目标 Bus 轨的 track_id
        level: 发送电平（0.0-1.0，线性增益）
        enabled: 是否启用
        tap: 取样位置（MX-14，Send 模型 v2）。"post_fader"=轨链输出端
            （音量/pan/EQ/FX 之后，默认——旧工程即此行为，声音不变）；
            "pre_fader"=clip 求和后、音量/EQ/FX 之前（不随推子变化）。
        muted: 独立静音（MX-14）——只断开该 send 的贡献，不影响主输出，
            也不清空 send 配置（区别于 enabled=False 的移除语义）。
    """
    send_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    target_bus_id: str = ""
    level: float = 1.0
    enabled: bool = True
    tap: str = "post_fader"
    muted: bool = False

    def __post_init__(self) -> None:
        if not self.target_bus_id:
            raise ValueError("target_bus_id cannot be empty")
        if self.level < 0.0 or self.level > 1.0:
            raise ValueError(f"level must be in [0.0, 1.0]: {self.level}")
        if self.tap not in _SEND_TAPS:
            raise ValueError(f"tap must be one of {_SEND_TAPS}: {self.tap}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "send_id": self.send_id,
            "target_bus_id": self.target_bus_id,
            "level": self.level,
            "enabled": self.enabled,
            "tap": self.tap,
            "muted": self.muted,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Send":
        # MX-14: 未知 tap 值降级为默认 post_fader（保持旧声音，不阻断加载）
        tap = str(data.get("tap", "post_fader") or "post_fader")
        if tap not in _SEND_TAPS:
            logger.warning(
                "Send.from_dict: unknown tap %r → post_fader (default)", tap)
            tap = "post_fader"
        return Send(
            send_id=data.get("send_id", str(uuid.uuid4())),
            target_bus_id=data.get("target_bus_id", ""),
            level=float(data.get("level", 1.0)),
            enabled=bool(data.get("enabled", True)),
            tap=tap,
            muted=bool(data.get("muted", False)),
        )


# MX-14: Send 取样位置合法集合（版本化契约；默认 post_fader=旧行为）
_SEND_TAPS = ("post_fader", "pre_fader")


# ── 参数化 EQ 频段模型（CW-A1 重构：6 频段全参量）──────────────────────────────

_EQ_FILTER_TYPES = ("HPF", "Low Shelf", "Bell", "High Shelf", "Notch", "LPF")
_EQ_VOICINGS = ("Clean", "British", "Warm", "Punch")
_EQ_SLOPES = (6, 12, 18, 24)
_EQ_RANGE_KEYS = ("L", "ML", "MH", "H")


def auto_range_key(freq: float) -> str:
    """根据频率自动匹配最合适的旋钮精细调节区间（与 EQ UI 一致）。"""
    if freq < 350.0:
        return "L"
    elif freq < 1400.0:
        return "ML"
    elif freq < 7500.0:
        return "MH"
    return "H"


def default_eq_bands_demo() -> tuple[dict, ...]:
    """6 频段演示布局（Band 6 LPF 默认旁路）。"""
    return (
        {"index": 1, "enabled": True,  "solo": False, "filter_type": "HPF",        "range_key": "L",  "freq": 65.0,    "gain": 0.0,  "q": 1.1, "slope": 12},
        {"index": 2, "enabled": True,  "solo": False, "filter_type": "Low Shelf",  "range_key": "L",  "freq": 110.0,   "gain": -4.5, "q": 1.4, "slope": 12},
        {"index": 3, "enabled": True,  "solo": False, "filter_type": "Bell",       "range_key": "ML", "freq": 320.0,   "gain": -2.2, "q": 2.1, "slope": 12},
        {"index": 4, "enabled": True,  "solo": False, "filter_type": "Bell",       "range_key": "MH", "freq": 2400.0,  "gain": 3.4,  "q": 1.4, "slope": 12},
        {"index": 5, "enabled": True,  "solo": False, "filter_type": "High Shelf", "range_key": "H",  "freq": 8200.0,  "gain": 2.0,  "q": 1.2, "slope": 12},
        {"index": 6, "enabled": False, "solo": False, "filter_type": "LPF",        "range_key": "H",  "freq": 19500.0, "gain": 0.0,  "q": 1.1, "slope": 12},
    )


def default_eq_bands_flat() -> tuple[dict, ...]:
    """6 频段平直布局（全部 0 dB，仅 HPF/LPF 旁路）。"""
    return (
        {"index": 1, "enabled": False, "solo": False, "filter_type": "HPF",        "range_key": "L",  "freq": 70.0,    "gain": 0.0, "q": 1.1, "slope": 12},
        {"index": 2, "enabled": True,  "solo": False, "filter_type": "Low Shelf",  "range_key": "L",  "freq": 80.0,    "gain": 0.0, "q": 1.1, "slope": 12},
        {"index": 3, "enabled": True,  "solo": False, "filter_type": "Bell",       "range_key": "ML", "freq": 250.0,   "gain": 0.0, "q": 1.1, "slope": 12},
        {"index": 4, "enabled": True,  "solo": False, "filter_type": "Bell",       "range_key": "MH", "freq": 1500.0,  "gain": 0.0, "q": 1.1, "slope": 12},
        {"index": 5, "enabled": True,  "solo": False, "filter_type": "High Shelf", "range_key": "H",  "freq": 6000.0,  "gain": 0.0, "q": 1.2, "slope": 12},
        {"index": 6, "enabled": False, "solo": False, "filter_type": "LPF",        "range_key": "H",  "freq": 20000.0, "gain": 0.0, "q": 1.1, "slope": 12},
    )


def _normalize_eq_band(band: Any, idx: int) -> dict[str, Any]:
    """校验并归一化单个 EQ 频段 dict（范围钳位 + 缺失字段补默认）。

    全新开始语义：形状不符（如旧图形 EQ 的 float 列表元素）或数值异常时，
    该频段回退到平直默认，绝不抛异常阻断工程加载。
    """
    if not isinstance(band, dict):
        return dict(default_eq_bands_flat()[idx])
    ftype = str(band.get("filter_type", "Bell"))
    if ftype not in _EQ_FILTER_TYPES:
        ftype = "Bell"
    try:
        freq = float(band.get("freq", 1000.0))
    except (TypeError, ValueError):
        freq = 1000.0
    freq = max(20.0, min(22000.0, freq))
    try:
        gain = float(band.get("gain", 0.0))
    except (TypeError, ValueError):
        gain = 0.0
    gain = max(-20.0, min(20.0, gain))
    try:
        q = float(band.get("q", 1.1))
    except (TypeError, ValueError):
        q = 1.1
    q = max(0.3, min(10.3, q))
    try:
        slope = int(band.get("slope", 12))
    except (TypeError, ValueError):
        slope = 12
    if slope not in _EQ_SLOPES:
        slope = 12
    range_key = str(band.get("range_key", "") or "")
    if range_key not in _EQ_RANGE_KEYS:
        range_key = auto_range_key(freq)
    return {
        "index": idx + 1,
        "enabled": bool(band.get("enabled", True)),
        "solo": bool(band.get("solo", False)),
        "filter_type": ftype,
        "range_key": range_key,
        "freq": freq,
        "gain": gain,
        "q": q,
        "slope": slope,
    }


def preserves_param_extras(cls, extra_known: tuple = ()):
    """H01：效果参数 dataclass 的**参数内部未知字段**保留装饰器。

    复审 H01-4：实例 ``params`` dict 里的未来/未知键不能因
    ``XParams.from_dict(...).to_dict()`` 往返而丢失（与
    ``TrackEffectInstance._extra_fields`` 同契约，作用在参数层）。

    机制：
    - 已知键 = dataclass 字段名 ∪ ``cls.default().to_dict()`` 键 ∪
      ``extra_known``（legacy 别名）；
    - ``from_dict`` 把未知键存到实例 ``_extra_fields``（类级默认 {}，
      frozen 实例经 ``object.__setattr__``）；
    - ``to_dict`` 在末尾合并 ``_extra_fields``；
    - ``@preserves_extras`` 使 ``with_*``/``copy`` 派生方法携带 extras。

    相等性不含 extras（非 dataclass 字段）；``dataclasses.replace`` 不
    携带（参数对象一律经 from_dict 重建，关键往返路径已覆盖）。
    """
    import dataclasses as _dc
    import functools as _ft

    orig_from_dict = cls.from_dict
    orig_to_dict = cls.to_dict
    known_cache: list = []

    def _known() -> frozenset:
        if not known_cache:
            keys = {f.name for f in _dc.fields(cls)}
            try:
                probe = cls.default() if hasattr(cls, "default") else None
            except Exception:
                probe = None
            if probe is not None:
                keys.update(orig_to_dict(probe).keys())
            keys.update(extra_known)
            known_cache.append(frozenset(keys))
        return known_cache[0]

    @_ft.wraps(orig_from_dict)
    def from_dict(data):
        inst = orig_from_dict(data)
        extras = split_extras(data or {}, _known())
        if extras:
            object.__setattr__(inst, "_extra_fields", extras)
        return inst

    @_ft.wraps(orig_to_dict)
    def to_dict(self):
        out = orig_to_dict(self)
        extras = getattr(self, "_extra_fields", None)
        if extras:
            out.update(extras)
        return out

    cls.from_dict = staticmethod(from_dict)
    cls.to_dict = to_dict
    cls._extra_fields = {}
    return preserves_extras(cls)


@dataclass(frozen=True)
@preserves_param_extras
class StripEQParams:
    """轨道级参数化 EQ（6 频段全参量，CW-A1 重构）。

    每个频段独立选择滤波器形状（HPF / Low Shelf / Bell / High Shelf /
    Notch / LPF），并可调频率 / 增益 / Q（剪切滤波器另有 6..24 dB/oct 斜率）。
    voicing 是调音台染色特性（按比例缩放 Q），master_gain 为整体输出增益。

    Attributes:
        enabled: EQ 总开关（False=旁路，信号直通）
        voicing: 染色特性（"Clean" / "British" / "Warm" / "Punch"，Q 缩放系数）
        master_gain: 整体输出增益（-20.0..+20.0 dB）
        bands: 6 个频段 dict（见 default_eq_bands_demo），逐段归一化校验
    """
    enabled: bool = True
    voicing: str = "Clean"
    master_gain: float = 0.0
    bands: tuple = field(default_factory=default_eq_bands_flat)

    def __post_init__(self) -> None:
        if self.voicing not in _EQ_VOICINGS:
            raise ValueError(f"voicing must be one of {_EQ_VOICINGS}: {self.voicing}")
        if not (-20.0 <= self.master_gain <= 20.0):
            raise ValueError(f"master_gain must be in [-20, 20]: {self.master_gain}")
        if not self.bands:
            raise ValueError("bands cannot be empty")
        # 频段逐个校验（bands 必须是 dict 序列；构造方负责先归一化）
        normalized = tuple(_normalize_eq_band(b, i) for i, b in enumerate(self.bands))
        object.__setattr__(self, "bands", normalized)

    @staticmethod
    def default() -> "StripEQParams":
        """平直响应（全部频段 0 dB，启用）。"""
        return StripEQParams(bands=default_eq_bands_flat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "voicing": self.voicing,
            "master_gain": self.master_gain,
            "bands": [dict(b) for b in self.bands],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "StripEQParams":
        bands_data = data.get("bands", [])
        # 全新开始：旧图形 EQ（bands 为 float 列表）→ 整体落平直默认
        if not isinstance(bands_data, (list, tuple)) or not bands_data or \
                not all(isinstance(b, dict) for b in bands_data):
            bands_data = default_eq_bands_flat()
        # 不足 6 段时用平直默认补齐（频段身份 = 下标，不可缺位）
        flat = default_eq_bands_flat()
        bands = tuple(
            dict(bands_data[i]) if i < len(bands_data) else dict(flat[i])
            for i in range(len(flat))
        )
        voicing = str(data.get("voicing", "Clean"))
        if voicing not in _EQ_VOICINGS:
            voicing = "Clean"
        try:
            master_gain = float(data.get("master_gain", 0.0) or 0.0)
        except (TypeError, ValueError):
            master_gain = 0.0
        return StripEQParams(
            enabled=bool(data.get("enabled", True)),
            voicing=voicing,
            master_gain=max(-20.0, min(20.0, master_gain)),
            bands=bands,
        )


# ── FX 机架数据模型（W26）─────────────────────────────────────────────────────

@dataclass(frozen=True)
@preserves_param_extras
class LimiterParams:
    """限幅器参数（FX 机架效果器之一，CW-A1 重构）。

    Attributes:
        threshold_db: 阈值（dB），[-30, 0]，信号超过此值被限制
        attack_ms: 启动时间（ms），[0.7, 30]，增益下降速度
        hold_ms: 保持时间（ms），[0, 4000]，达到限制后增益的驻留时间
        release_ms: 释放时间（ms），[50, 4000]，增益恢复速度
        enabled: 是否启用（False=旁路直通）
    """
    threshold_db: float = -6.0
    attack_ms: float = 0.71
    hold_ms: float = 0.0
    release_ms: float = 93.0
    enabled: bool = True

    def __post_init__(self) -> None:
        if not (-30.0 <= self.threshold_db <= 0.0):
            raise ValueError(f"threshold_db must be in [-30, 0]: {self.threshold_db}")
        if not (0.7 <= self.attack_ms <= 30.0):
            raise ValueError(f"attack_ms must be in [0.7, 30]: {self.attack_ms}")
        if not (0.0 <= self.hold_ms <= 4000.0):
            raise ValueError(f"hold_ms must be in [0, 4000]: {self.hold_ms}")
        if not (50.0 <= self.release_ms <= 4000.0):
            raise ValueError(f"release_ms must be in [50, 4000]: {self.release_ms}")

    @staticmethod
    def default() -> "LimiterParams":
        return LimiterParams()

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold_db": self.threshold_db,
            "attack_ms": self.attack_ms,
            "hold_ms": self.hold_ms,
            "release_ms": self.release_ms,
            "enabled": self.enabled,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "LimiterParams":
        return LimiterParams(
            threshold_db=float(data.get("threshold_db", -6.0)),
            attack_ms=float(data.get("attack_ms", 0.71)),
            hold_ms=float(data.get("hold_ms", 0.0)),
            release_ms=float(data.get("release_ms", 93.0)),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass(frozen=True)
class DynamicsParams:
    """Dynamics 完整单元参数（Da Vinci Fairlight 语义，CW-A1）。

    单一效果器实例携带 Expander/Gate → Compressor（含 Make Up）→ Limiter
    三个子模块的完整状态——clip 级 Dynamics 是一条 EffectInstance，
    不在属性面板拆分为三张卡片。轨级 Dynamics 仍以 panel="dynamics"
    三实例存储（DSP 遥测按实例定位）；两种形态共用同一面板 UI。

    Attributes:
        enabled: 总电源（False=三模块全部旁路直通）
        makeup: 全局 Make Up 补偿增益（dB），[0, 24]（写入 comp.makeup_gain）
        eg: ExpanderParams.to_dict() 子状态
        comp: CompressorParams.to_dict() 子状态（makeup_gain 与 makeup 同步）
        lim: LimiterParams.to_dict() 子状态
    """
    enabled: bool = True
    makeup: float = 0.0
    eg: dict[str, Any] = field(default_factory=lambda: ExpanderParams.default().to_dict())
    comp: dict[str, Any] = field(default_factory=lambda: CompressorParams.default().to_dict())
    lim: dict[str, Any] = field(default_factory=lambda: LimiterParams.default().to_dict())

    def __post_init__(self) -> None:
        if not (0.0 <= self.makeup <= 24.0):
            raise ValueError(f"makeup must be in [0, 24]: {self.makeup}")
        # 子状态统一经各自 from_dict 归一化（缺键/非法值落默认，AUD 容错语义）
        object.__setattr__(self, "eg", ExpanderParams.from_dict(dict(self.eg or {})).to_dict())
        comp = CompressorParams.from_dict(dict(self.comp or {})).to_dict()
        # 单一真源：makeup 顶层字段与 comp.makeup_gain 保持一致（顶层优先）
        comp["makeup_gain"] = self.makeup
        object.__setattr__(self, "comp", comp)
        object.__setattr__(self, "lim", LimiterParams.from_dict(dict(self.lim or {})).to_dict())

    @staticmethod
    def default() -> "DynamicsParams":
        return DynamicsParams()

    def to_dict(self) -> dict[str, Any]:
        comp = dict(self.comp)
        comp["makeup_gain"] = self.makeup  # 单一真源：makeup 写入 comp
        return {
            "enabled": self.enabled,
            "makeup": self.makeup,
            "eg": dict(self.eg),
            "comp": comp,
            "lim": dict(self.lim),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "DynamicsParams":
        data = dict(data or {})
        comp = dict(data.get("comp") or {})
        makeup = float(data.get("makeup", comp.get("makeup_gain", 0.0)))
        return DynamicsParams(
            enabled=bool(data.get("enabled", True)),
            makeup=max(0.0, min(24.0, makeup)),
            eg=dict(data.get("eg") or {}),
            comp=comp,
            lim=dict(data.get("lim") or {}),
        )


@dataclass(frozen=True)
@preserves_param_extras
class CompressorParams:
    """压缩器参数（FX 机架效果器之一，CW-A1 重构：并行混合 + 保持 + 侧链）。

    feed-forward 峰值压缩拓扑；knee 为百分比软拐点宽度（内部映射 0..24 dB）。
    mix 是并行压缩（Dry/Wet 混合）；sc_* 是侧链（外部检源路由与监听）。

    Attributes:
        threshold_db: 阈值（dB），[-60, 0]，信号超过此值开始压缩
        ratio: 压缩比，[1.2, 20.0]（1.2:1 起压，20:1 近限制）
        attack_ms: 启动时间（ms），[0.7, 1000]，包络跟随上升速度
        hold_ms: 保持时间（ms），[0, 4000]，压缩增益的驻留时间
        release_ms: 释放时间（ms），[50, 5000]，包络跟随下降速度
        knee: 软拐点宽度（0..100 %，内部映射 0..24 dB，0=硬拐点）
        mix: 压缩湿信号比例（0..100 %，100=经典串行压缩）
        makeup_gain: 输出补偿增益（dB），[0, +24]（Dynamics 面板 MakeUp 推子）
        sc_on: 侧链开关（False=内部检源）
        sc_listen: 侧链监听（True=输出检源信号用于 audition）
        sc_source: 侧链源（"internal" 或任意轨道 track_id）
        enabled: 是否启用（False=旁路直通）
    """
    threshold_db: float = -15.0
    ratio: float = 2.5
    attack_ms: float = 1.4
    hold_ms: float = 0.0
    release_ms: float = 93.0
    knee: float = 15.0
    mix: float = 100.0
    makeup_gain: float = 0.0
    sc_on: bool = False
    sc_listen: bool = False
    sc_source: str = "internal"
    enabled: bool = True
    # R09：Make Up 位置（三实例 DY 面板形态）。None = legacy（v3 语义：
    # makeup 在压缩器内部、Limiter 之前——旧工程声音不变）；True = 组后
    # 增益（整个 Dynamics 组（EG→Comp→Lim）处理完后再乘，与复合
    # DynamicsEffect 的放置一致——两形态同参数不再产生不同限幅声音）。
    makeup_post: Optional[bool] = None

    def __post_init__(self) -> None:
        if not (-60.0 <= self.threshold_db <= 0.0):
            raise ValueError(f"threshold_db must be in [-60, 0]: {self.threshold_db}")
        if not (1.2 <= self.ratio <= 20.0):
            raise ValueError(f"ratio must be in [1.2, 20]: {self.ratio}")
        if not (0.7 <= self.attack_ms <= 1000.0):
            raise ValueError(f"attack_ms must be in [0.7, 1000]: {self.attack_ms}")
        if not (0.0 <= self.hold_ms <= 4000.0):
            raise ValueError(f"hold_ms must be in [0, 4000]: {self.hold_ms}")
        if not (50.0 <= self.release_ms <= 5000.0):
            raise ValueError(f"release_ms must be in [50, 5000]: {self.release_ms}")
        if not (0.0 <= self.knee <= 100.0):
            raise ValueError(f"knee must be in [0, 100]: {self.knee}")
        if not (0.0 <= self.mix <= 100.0):
            raise ValueError(f"mix must be in [0, 100]: {self.mix}")
        if not (0.0 <= self.makeup_gain <= 24.0):
            raise ValueError(f"makeup_gain must be in [0, 24]: {self.makeup_gain}")

    @staticmethod
    def default() -> "CompressorParams":
        return CompressorParams()

    def to_dict(self) -> dict[str, Any]:
        result = {
            "threshold_db": self.threshold_db,
            "ratio": self.ratio,
            "attack_ms": self.attack_ms,
            "hold_ms": self.hold_ms,
            "release_ms": self.release_ms,
            "knee": self.knee,
            "mix": self.mix,
            "makeup_gain": self.makeup_gain,
            "sc_on": self.sc_on,
            "sc_listen": self.sc_listen,
            "sc_source": self.sc_source,
            "enabled": self.enabled,
        }
        # R09：makeup_post 仅在显式设置时持久化（None=legacy 不写键，
        # 旧工程往返不出现新字段）
        if self.makeup_post is not None:
            result["makeup_post"] = bool(self.makeup_post)
        return result

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "CompressorParams":
        return CompressorParams(
            threshold_db=float(data.get("threshold_db", -15.0)),
            ratio=float(data.get("ratio", 2.5)),
            attack_ms=float(data.get("attack_ms", 1.4)),
            hold_ms=float(data.get("hold_ms", 0.0)),
            release_ms=float(data.get("release_ms", 93.0)),
            knee=float(data.get("knee", 15.0)),
            mix=float(data.get("mix", 100.0)),
            makeup_gain=float(data.get("makeup_gain", 0.0)),
            sc_on=bool(data.get("sc_on", False)),
            sc_listen=bool(data.get("sc_listen", False)),
            sc_source=str(data.get("sc_source", "internal") or "internal"),
            enabled=bool(data.get("enabled", True)),
            makeup_post=(None if data.get("makeup_post") is None
                         else bool(data.get("makeup_post"))),
        )


@dataclass(frozen=True)
@preserves_param_extras
class ReverbParams:
    """声学房间混响器参数（FX 机架效果器之一，CW-A1 重构）。

    物理房间模型：H/L/W 三维尺寸驱动早反射簇的时空分布，RT60 驱动晚期
    FDN 尾音，Brightness/Modulation 控制高频阻尼与梳状微调制；ER/Reverb
    两级独立音色染色（lo/hi shelf）；输出级提供 Direct/ER/Reverb 三路
    电平与 Dry/Wet 交叉淡化。

    Attributes:
        height/length/width: 房间尺寸（m），早反射跨距与扩散的驱动量
        pre_delay: 预延迟（ms，-200..200；负值有效钳为 0）
        reverb_time: 混响时间 RT60（ms，100..4000）
        distance: 声源到听音点距离（m，0..30），驱动直达/ER 附加延迟
        brightness: 高频亮度（%，6..100），映射晚期阻尼频率
        modulation: 梳状微调制深度（%，0..100）
        er_lo_freq/er_hi_freq: 早反射染色 shelf 频率（Hz）
        er_lo_gain/er_hi_gain: 早反射染色增益（dB，-18..+6）
        rev_lo_freq/rev_hi_freq: 晚期混响染色 shelf 频率（Hz）
        rev_lo_gain/rev_hi_gain: 晚期混响染色增益（dB，-18..+6）
        dry_wet: 干湿交叉淡化（%，0=全干 100=全湿）
        direct_db/er_db/reverb_db: 三路输出电平（dB，-60..+6，-60≈-inf）
        enabled: 是否启用（False=旁路直通）
    """
    height: float = 10.0
    length: float = 30.0
    width: float = 40.0
    pre_delay: float = 42.0
    reverb_time: float = 2730.0
    distance: float = 19.87
    brightness: float = 50.0
    modulation: float = 2.0
    er_lo_freq: float = 300.0
    er_hi_freq: float = 10000.0
    er_lo_gain: float = 0.0
    er_hi_gain: float = -6.1
    rev_lo_freq: float = 291.0
    rev_hi_freq: float = 11100.0
    rev_lo_gain: float = 1.1
    rev_hi_gain: float = -2.2
    dry_wet: float = 100.0
    direct_db: float = 0.0
    er_db: float = 0.0
    reverb_db: float = 0.0
    enabled: bool = True

    def __post_init__(self) -> None:
        for name in ("height",):
            v = getattr(self, name)
            if not (2.0 <= v <= 30.0):
                raise ValueError(f"{name} must be in [2, 30]: {v}")
        for name in ("length", "width"):
            v = getattr(self, name)
            if not (3.0 <= v <= 80.0):
                raise ValueError(f"{name} must be in [3, 80]: {v}")
        if not (-200.0 <= self.pre_delay <= 200.0):
            raise ValueError(f"pre_delay must be in [-200, 200]: {self.pre_delay}")
        if not (100.0 <= self.reverb_time <= 4000.0):
            raise ValueError(f"reverb_time must be in [100, 4000]: {self.reverb_time}")
        if not (0.0 <= self.distance <= 30.0):
            raise ValueError(f"distance must be in [0, 30]: {self.distance}")
        if not (6.0 <= self.brightness <= 100.0):
            raise ValueError(f"brightness must be in [6, 100]: {self.brightness}")
        if not (0.0 <= self.modulation <= 100.0):
            raise ValueError(f"modulation must be in [0, 100]: {self.modulation}")
        if not (150.0 <= self.er_lo_freq <= 500.0):
            raise ValueError(f"er_lo_freq must be in [150, 500]: {self.er_lo_freq}")
        if not (1000.0 <= self.er_hi_freq <= 16000.0):
            raise ValueError(f"er_hi_freq must be in [1000, 16000]: {self.er_hi_freq}")
        if not (150.0 <= self.rev_lo_freq <= 500.0):
            raise ValueError(f"rev_lo_freq must be in [150, 500]: {self.rev_lo_freq}")
        if not (1000.0 <= self.rev_hi_freq <= 16000.0):
            raise ValueError(f"rev_hi_freq must be in [1000, 16000]: {self.rev_hi_freq}")
        for name in ("er_lo_gain", "er_hi_gain", "rev_lo_gain", "rev_hi_gain"):
            v = getattr(self, name)
            if not (-18.0 <= v <= 6.0):
                raise ValueError(f"{name} must be in [-18, 6]: {v}")
        if not (0.0 <= self.dry_wet <= 100.0):
            raise ValueError(f"dry_wet must be in [0, 100]: {self.dry_wet}")
        for name in ("direct_db", "er_db", "reverb_db"):
            v = getattr(self, name)
            if not (-60.0 <= v <= 6.0):
                raise ValueError(f"{name} must be in [-60, 6]: {v}")

    @staticmethod
    def default() -> "ReverbParams":
        return ReverbParams()

    def to_dict(self) -> dict[str, Any]:
        return {
            "height": self.height, "length": self.length, "width": self.width,
            "pre_delay": self.pre_delay, "reverb_time": self.reverb_time,
            "distance": self.distance, "brightness": self.brightness,
            "modulation": self.modulation,
            "er_lo_freq": self.er_lo_freq, "er_hi_freq": self.er_hi_freq,
            "er_lo_gain": self.er_lo_gain, "er_hi_gain": self.er_hi_gain,
            "rev_lo_freq": self.rev_lo_freq, "rev_hi_freq": self.rev_hi_freq,
            "rev_lo_gain": self.rev_lo_gain, "rev_hi_gain": self.rev_hi_gain,
            "dry_wet": self.dry_wet,
            "direct_db": self.direct_db, "er_db": self.er_db,
            "reverb_db": self.reverb_db,
            "enabled": self.enabled,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ReverbParams":
        def _f(key: str, default: float) -> float:
            try:
                return float(data.get(key, default))
            except (TypeError, ValueError):
                return default

        return ReverbParams(
            height=_f("height", 10.0), length=_f("length", 30.0),
            width=_f("width", 40.0), pre_delay=_f("pre_delay", 42.0),
            reverb_time=_f("reverb_time", 2730.0), distance=_f("distance", 19.87),
            brightness=_f("brightness", 50.0), modulation=_f("modulation", 2.0),
            er_lo_freq=_f("er_lo_freq", 300.0), er_hi_freq=_f("er_hi_freq", 10000.0),
            er_lo_gain=_f("er_lo_gain", 0.0), er_hi_gain=_f("er_hi_gain", -6.1),
            rev_lo_freq=_f("rev_lo_freq", 291.0), rev_hi_freq=_f("rev_hi_freq", 11100.0),
            rev_lo_gain=_f("rev_lo_gain", 1.1), rev_hi_gain=_f("rev_hi_gain", -2.2),
            dry_wet=_f("dry_wet", 100.0),
            direct_db=_f("direct_db", 0.0), er_db=_f("er_db", 0.0),
            reverb_db=_f("reverb_db", 0.0),
            enabled=bool(data.get("enabled", True)),
        )


# DL-01：Delay 输入选择矩阵（真实实现的能力面）。旧菜单的 LCR/Quad/5.0/5.1/7.1
# 从未有真实多声道 Delay 实现（DSP 一直按立体声处理）——from_dict 迁移到
# "Stereo"，不改变声音，同时移除假能力声明。
_DELAY_INPUT_FORMATS = ("Stereo", "Mono", "Left", "Right")
_DELAY_LEGACY_INPUT_FORMATS = ("LCR", "Quad", "5.0", "5.1", "7.1")


@dataclass(frozen=True)
@preserves_param_extras
class DelayParams:
    """立体声双线延迟器参数（FX 机架效果器之一，CW-A1 重构）。

    L/R 独立延迟线（反馈支持负号=相位反转），stereo_blend 交叉反馈矩阵，
    high_ratio 控制反馈回路高频阻尼，输入级 low/high cut 滤波，
    输出级 dry/wet 与电平。

    Attributes:
        low_cut: 输入低切（Hz，20..5000）
        high_cut: 输入高切（Hz，100..20000）
        left_delay/right_delay: 声道延迟时间（ms，0..1200）
        left_feedback/right_feedback: 声道反馈量（%，-99..+99，负值翻相）
        high_ratio: 反馈高频阻尼比（%，0..100，越大高频衰减越快）
        stereo_blend: 交叉反馈比例（%，0=独立 100=完全交叉）
        dry_wet: 湿信号比例（%，0..100）
        level_db: 输出电平（dB，-24..+6）
        link_lr: UI 联动开关（仅界面便利，DSP 不读）
        input_format: 输入格式（"Stereo"/"Mono"/"Left"/"Right"，DL-01 选择矩阵；
            "Mono" = (L+R)/2 平均驱动双线；"Left"/"Right" = 单声道内容驱动双线）
        enabled: 是否启用（False=旁路直通）
    """
    low_cut: float = 108.0
    high_cut: float = 12500.0
    left_delay: float = 121.0
    left_feedback: float = 16.0
    right_delay: float = 135.0
    right_feedback: float = -7.0
    high_ratio: float = 60.0
    stereo_blend: float = 15.0
    dry_wet: float = 30.0
    level_db: float = 0.0
    link_lr: bool = False
    input_format: str = "Stereo"
    enabled: bool = True

    def __post_init__(self) -> None:
        if not (20.0 <= self.low_cut <= 5000.0):
            raise ValueError(f"low_cut must be in [20, 5000]: {self.low_cut}")
        if not (100.0 <= self.high_cut <= 20000.0):
            raise ValueError(f"high_cut must be in [100, 20000]: {self.high_cut}")
        if not (0.0 <= self.left_delay <= 1200.0):
            raise ValueError(f"left_delay must be in [0, 1200]: {self.left_delay}")
        if not (-99.0 <= self.left_feedback <= 99.0):
            raise ValueError(f"left_feedback must be in [-99, 99]: {self.left_feedback}")
        if not (0.0 <= self.right_delay <= 1200.0):
            raise ValueError(f"right_delay must be in [0, 1200]: {self.right_delay}")
        if not (-99.0 <= self.right_feedback <= 99.0):
            raise ValueError(f"right_feedback must be in [-99, 99]: {self.right_feedback}")
        if not (0.0 <= self.high_ratio <= 100.0):
            raise ValueError(f"high_ratio must be in [0, 100]: {self.high_ratio}")
        if not (0.0 <= self.stereo_blend <= 100.0):
            raise ValueError(f"stereo_blend must be in [0, 100]: {self.stereo_blend}")
        if not (0.0 <= self.dry_wet <= 100.0):
            raise ValueError(f"dry_wet must be in [0, 100]: {self.dry_wet}")
        if not (-24.0 <= self.level_db <= 6.0):
            raise ValueError(f"level_db must be in [-24, 6]: {self.level_db}")
        if self.input_format not in _DELAY_INPUT_FORMATS:
            raise ValueError(f"invalid input_format: {self.input_format}")

    @staticmethod
    def default() -> "DelayParams":
        return DelayParams()

    def to_dict(self) -> dict[str, Any]:
        return {
            "low_cut": self.low_cut, "high_cut": self.high_cut,
            "left_delay": self.left_delay, "left_feedback": self.left_feedback,
            "right_delay": self.right_delay, "right_feedback": self.right_feedback,
            "high_ratio": self.high_ratio, "stereo_blend": self.stereo_blend,
            "dry_wet": self.dry_wet, "level_db": self.level_db,
            "link_lr": self.link_lr, "input_format": self.input_format,
            "enabled": self.enabled,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "DelayParams":
        def _f(key: str, default: float) -> float:
            try:
                return float(data.get(key, default))
            except (TypeError, ValueError):
                return default

        fmt = str(data.get("input_format", "Stereo") or "Stereo")
        # DL-01：旧菜单从未有真实多声道 Delay 实现（DSP 一直按立体声处理），
        # LCR/Quad/5.0/5.1/7.1 枚举迁移到 Stereo（不改变声音，移除假能力声明）
        if fmt in _DELAY_LEGACY_INPUT_FORMATS:
            fmt = "Stereo"
        if fmt not in _DELAY_INPUT_FORMATS:
            fmt = "Stereo"
        return DelayParams(
            low_cut=_f("low_cut", 108.0), high_cut=_f("high_cut", 12500.0),
            left_delay=_f("left_delay", 121.0), left_feedback=_f("left_feedback", 16.0),
            right_delay=_f("right_delay", 135.0), right_feedback=_f("right_feedback", -7.0),
            high_ratio=_f("high_ratio", 60.0), stereo_blend=_f("stereo_blend", 15.0),
            dry_wet=_f("dry_wet", 30.0), level_db=_f("level_db", 0.0),
            link_lr=bool(data.get("link_lr", False)),
            input_format=fmt,
            enabled=bool(data.get("enabled", True)),
        )


@dataclass(frozen=True)
@preserves_param_extras
class SaturationParams:
    """谐波失真饱和器参数（FX 机架效果器之一）。

    sigmoid WaveShaper，移植自 audio_pro 的 Saturation。

    Attributes:
        drive: 驱动量（0..100，0=无失真直通，100=强过载）
        enabled: 是否启用（False=旁路直通）
    """
    drive: float = 0.0
    enabled: bool = True

    def __post_init__(self) -> None:
        if not (0.0 <= self.drive <= 100.0):
            raise ValueError(f"drive must be in [0, 100]: {self.drive}")

    @staticmethod
    def default() -> "SaturationParams":
        return SaturationParams()

    def to_dict(self) -> dict[str, Any]:
        return {
            "drive": self.drive,
            "enabled": self.enabled,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SaturationParams":
        return SaturationParams(
            drive=float(data.get("drive", 0.0)),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass(frozen=True)
@preserves_param_extras
class ImagerParams:
    """立体声成像器参数（FX 机架效果器之一）。

    Mid/Side 处理：width 控制立体声宽度（Side 信号增益）。
    Pan 控制：M/S 处理后线性声像。
    移植自 audio_pro 的 Imager 参数。

    Attributes:
        pan: 声像（-100..100，-100=全左, 0=中, 100=全右）
        width: 立体声宽度（0..200，0=mono, 100=原始, 200=超宽）
        enabled: 是否启用（False=旁路直通）
    """
    pan: float = 0.0
    width: float = 100.0
    enabled: bool = True

    def __post_init__(self) -> None:
        if not (-100.0 <= self.pan <= 100.0):
            raise ValueError(f"pan must be in [-100, 100]: {self.pan}")
        if not (0.0 <= self.width <= 200.0):
            raise ValueError(f"width must be in [0, 200]: {self.width}")

    @staticmethod
    def default() -> "ImagerParams":
        return ImagerParams()

    def to_dict(self) -> dict[str, Any]:
        return {
            "pan": self.pan,
            "width": self.width,
            "enabled": self.enabled,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ImagerParams":
        return ImagerParams(
            pan=float(data.get("pan", 0.0)),
            width=float(data.get("width", 100.0)),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass(frozen=True)
@preserves_param_extras
class ExpanderParams:
    """扩展器/门限参数（FX 机架效果器之一，CW-A1 重构）。

    向下扩展器：低于阈值的信号按 1:ratio 下扩，range_db 封顶最大衰减。
    mode="Gate" 时按高比率陡降门限处理（ratio 锁定为门限陡降）。

    兼容：``from_dict`` 接受旧 noise_gate 键（threshold_db/attack_ms/
    release_ms），自动以 Gate 模式无损承接（CW-A1 全新开始策略）。

    Attributes:
        mode: "Expander"（比率下扩）或 "Gate"（高比率门）
        threshold_db: 阈值（dB），[-50, 0]，低于此值开始下扩
        range_db: 最大衰减深度（dB），[0, 60]（Gate 模式即门深）
        ratio: 扩展比（1:x），[1.1, 3.0]
        attack_ms: 开门时间（ms），[0.5, 100]
        hold_ms: 保持时间（ms），[0, 4000]
        release_ms: 关门时间（ms），[50, 4000]
        enabled: 是否启用（False=旁路直通）
    """
    mode: str = "Expander"
    threshold_db: float = -35.0
    range_db: float = 18.0
    ratio: float = 1.8
    attack_ms: float = 1.4
    hold_ms: float = 0.0
    release_ms: float = 93.0
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.mode not in ("Expander", "Gate"):
            raise ValueError(f"mode must be Expander|Gate: {self.mode}")
        if not (-50.0 <= self.threshold_db <= 0.0):
            raise ValueError(f"threshold_db must be in [-50, 0]: {self.threshold_db}")
        if not (0.0 <= self.range_db <= 60.0):
            raise ValueError(f"range_db must be in [0, 60]: {self.range_db}")
        if not (1.1 <= self.ratio <= 3.0):
            raise ValueError(f"ratio must be in [1.1, 3.0]: {self.ratio}")
        if not (0.5 <= self.attack_ms <= 100.0):
            raise ValueError(f"attack_ms must be in [0.5, 100]: {self.attack_ms}")
        if not (0.0 <= self.hold_ms <= 4000.0):
            raise ValueError(f"hold_ms must be in [0, 4000]: {self.hold_ms}")
        if not (50.0 <= self.release_ms <= 4000.0):
            raise ValueError(f"release_ms must be in [50, 4000]: {self.release_ms}")

    @staticmethod
    def default() -> "ExpanderParams":
        return ExpanderParams()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "threshold_db": self.threshold_db,
            "range_db": self.range_db,
            "ratio": self.ratio,
            "attack_ms": self.attack_ms,
            "hold_ms": self.hold_ms,
            "release_ms": self.release_ms,
            "enabled": self.enabled,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ExpanderParams":
        # 旧 noise_gate 键无损承接：无 mode/range 键但带 release_ms 键 → Gate
        legacy_gate = ("mode" not in data and "range_db" not in data
                       and "range" not in data and "release_ms" in data)
        mode = str(data.get("mode", "Gate" if legacy_gate else "Expander"))
        if mode not in ("Expander", "Gate"):
            mode = "Gate" if legacy_gate else "Expander"
        ratio = float(data.get("ratio", 1.8))
        if legacy_gate:
            ratio = 2.5

        def _f(key: str, default: float, lo: float, hi: float) -> float:
            try:
                return max(lo, min(hi, float(data.get(key, default))))
            except (TypeError, ValueError):
                return default

        return ExpanderParams(
            mode=mode,
            threshold_db=_f("threshold_db", -35.0, -50.0, 0.0),
            range_db=_f("range_db", _f("range", 18.0, 0.0, 60.0), 0.0, 60.0),
            ratio=max(1.1, min(3.0, ratio)),
            attack_ms=_f("attack_ms", 1.4, 0.5, 100.0),
            hold_ms=_f("hold_ms", 0.0, 0.0, 4000.0),
            release_ms=_f("release_ms", 93.0, 50.0, 4000.0),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass(frozen=True)
class SpatialPannerParams:
    """空间声像器参数（轨级属性，非链效果；CW-A1 重构）。

    lr（左右）不在此存储——``EditTrack.pan`` 是唯一真源（保持 pan 自动化
    兼容）。立体声域实现：spread/rotate/divergence/boom 真实生效；
    fb/elevation/format/space_mode 完整持久化并参与可视化，待多声道
    引擎后启用。

    Attributes:
        enabled: 声像器总开关（False=回退普通 pan/balance）
        space_mode: 空间视图模式（"2D" / "3D"）
        format: 扬声器制式（"2.0"/"5.1"/"7.1"/"7.1.4"，立体声域暂可视化）
        fb: 前后定位（-100..100，100=正前）
        elevation: 高度（0..100，0=地面 100=天花板，3D）
        rotate: 声场旋转角（0..360 deg）
        spread: 声场弥散宽度（0=PNT 点声源 .. 100=FULL）
        spread_auto: Auto Spread 收敛（越靠边界越向点声源收敛）
        divergence: 全场弥散度（0..100，对侧串音）
        div_mode: 弥散模式（"2D" / "3D"）
        boom_on: LFE 低频发送开关
        boom_route: 低频发送路由（"Pre" 推子前 / "Post" 推子后 / "Only" 仅低频）
        boom_gain: 低频发送增益（dB，-60≈-inf .. +10）
    """
    enabled: bool = True
    space_mode: str = "2D"
    format: str = "2.0"
    fb: float = 100.0
    elevation: float = 0.0
    rotate: float = 0.0
    spread: float = 100.0
    spread_auto: bool = False
    divergence: float = 0.0
    div_mode: str = "2D"
    boom_on: bool = False
    boom_route: str = "Pre"
    boom_gain: float = -60.0

    def __post_init__(self) -> None:
        if self.space_mode not in ("2D", "3D"):
            raise ValueError(f"space_mode must be 2D|3D: {self.space_mode}")
        if self.format not in ("2.0", "5.1", "7.1", "7.1.4"):
            raise ValueError(f"invalid format: {self.format}")
        if not (-100.0 <= self.fb <= 100.0):
            raise ValueError(f"fb must be in [-100, 100]: {self.fb}")
        if not (0.0 <= self.elevation <= 100.0):
            raise ValueError(f"elevation must be in [0, 100]: {self.elevation}")
        if not (0.0 <= self.rotate <= 360.0):
            raise ValueError(f"rotate must be in [0, 360]: {self.rotate}")
        if not (0.0 <= self.spread <= 100.0):
            raise ValueError(f"spread must be in [0, 100]: {self.spread}")
        if not (0.0 <= self.divergence <= 100.0):
            raise ValueError(f"divergence must be in [0, 100]: {self.divergence}")
        if self.div_mode not in ("2D", "3D"):
            raise ValueError(f"div_mode must be 2D|3D: {self.div_mode}")
        if self.boom_route not in ("Pre", "Post", "Only"):
            raise ValueError(f"boom_route must be Pre|Post|Only: {self.boom_route}")
        if not (-60.0 <= self.boom_gain <= 10.0):
            raise ValueError(f"boom_gain must be in [-60, 10]: {self.boom_gain}")

    @staticmethod
    def default() -> "SpatialPannerParams":
        return SpatialPannerParams()

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "space_mode": self.space_mode,
            "format": self.format,
            "fb": self.fb,
            "elevation": self.elevation,
            "rotate": self.rotate,
            "spread": self.spread,
            "spread_auto": self.spread_auto,
            "divergence": self.divergence,
            "div_mode": self.div_mode,
            "boom_on": self.boom_on,
            "boom_route": self.boom_route,
            "boom_gain": self.boom_gain,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SpatialPannerParams":
        def _f(key: str, default: float, lo: float, hi: float) -> float:
            try:
                return max(lo, min(hi, float(data.get(key, default))))
            except (TypeError, ValueError):
                return default

        space_mode = str(data.get("space_mode", "2D") or "2D")
        if space_mode not in ("2D", "3D"):
            space_mode = "2D"
        fmt = str(data.get("format", "2.0") or "2.0")
        if fmt not in ("2.0", "5.1", "7.1", "7.1.4"):
            fmt = "2.0"
        div_mode = str(data.get("div_mode", "2D") or "2D")
        if div_mode not in ("2D", "3D"):
            div_mode = "2D"
        boom_route = str(data.get("boom_route", "Pre") or "Pre")
        if boom_route not in ("Pre", "Post", "Only"):
            boom_route = "Pre"
        return SpatialPannerParams(
            enabled=bool(data.get("enabled", True)),
            space_mode=space_mode,
            format=fmt,
            fb=_f("fb", 100.0, -100.0, 100.0),
            elevation=_f("elevation", 0.0, 0.0, 100.0),
            rotate=_f("rotate", 0.0, 0.0, 360.0),
            spread=_f("spread", 100.0, 0.0, 100.0),
            spread_auto=bool(data.get("spread_auto", False)),
            divergence=_f("divergence", 0.0, 0.0, 100.0),
            div_mode=div_mode,
            boom_on=bool(data.get("boom_on", False)),
            boom_route=boom_route,
            boom_gain=_f("boom_gain", -60.0, -60.0, 10.0),
        )


# 效果器类型 → 参数 dataclass 的注册（供反序列化 / UI 工厂使用）
# 延迟填充避免前向引用问题
_EFFECT_PARAM_REGISTRY: dict = {}


def _ensure_effect_param_registry() -> dict:
    if not _EFFECT_PARAM_REGISTRY:
        _EFFECT_PARAM_REGISTRY["strip_eq"] = StripEQParams
        _EFFECT_PARAM_REGISTRY["limiter"] = LimiterParams
        _EFFECT_PARAM_REGISTRY["compressor"] = CompressorParams
        _EFFECT_PARAM_REGISTRY["reverb"] = ReverbParams
        _EFFECT_PARAM_REGISTRY["delay"] = DelayParams
        _EFFECT_PARAM_REGISTRY["saturation"] = SaturationParams
        _EFFECT_PARAM_REGISTRY["imager"] = ImagerParams
        _EFFECT_PARAM_REGISTRY["expander"] = ExpanderParams
        # CW-A1: NoiseGate 由 Expander(Gate) 无损承接（from_dict 兼容旧键）
        _EFFECT_PARAM_REGISTRY["noise_gate"] = ExpanderParams
        # Dynamics 完整单元（EG→Comp→Lim 单实例，clip 级不拆分）
        _EFFECT_PARAM_REGISTRY["dynamics"] = DynamicsParams
        # NF-01（2026-11-15 计划 §6）：两款新复合 FX
        _EFFECT_PARAM_REGISTRY["voice_changer"] = VoiceChangerParams
        _EFFECT_PARAM_REGISTRY["spectral_mangler"] = SpectralManglerParams
        # P1（2026-11-19 计划第三阶段 #1-#7）：新 FX 参数载体
        # （注册后 fx_versioning 判 supported；validate_effect_params 可归一）
        _EFFECT_PARAM_REGISTRY["de_esser"] = DeEsserParams
        _EFFECT_PARAM_REGISTRY["true_peak_limiter"] = TruePeakLimiterParams
        _EFFECT_PARAM_REGISTRY["dynamic_eq"] = DynamicEqParams
        _EFFECT_PARAM_REGISTRY["bus_comp"] = BusCompParams
        _EFFECT_PARAM_REGISTRY["multiband_comp"] = MultibandCompParams
        _EFFECT_PARAM_REGISTRY["convolution_reverb"] = ConvolutionReverbParams
        _EFFECT_PARAM_REGISTRY["transient_designer"] = TransientDesignerParams
        _EFFECT_PARAM_REGISTRY["tremolo_auto_pan"] = TremoloAutoPanParams
        _EFFECT_PARAM_REGISTRY["chorus_flanger"] = ChorusFlangerParams
        _EFFECT_PARAM_REGISTRY["phaser"] = PhaserParams
    return _EFFECT_PARAM_REGISTRY


def _deep_copy_automation(automation: dict) -> dict:
    """AUD-19104：深拷贝 {param: KeyframeTrack} 映射。

    KeyframeTrack 是可变 dataclass（add_keyframe 原位写入）。frozen 模型的
    派生快照若共享同一 dict / 同一批 KeyframeTrack 实例，任何原位修改都会
    同时改写历史快照且不触发 revision bump（破坏 TAEP 乐观锁与 undo 一致
    性）。轨级与 Master automation 的所有快照路径统一走本深拷贝。

    注意同时保留 pre_extrap / post_extrap（外推策略是 authoritative 字段）。
    """
    return {
        param: KeyframeTrack(
            param_name=track.param_name,
            keyframes=list(track.keyframes),
            min_val=track.min_val,
            max_val=track.max_val,
            pre_extrap=track.pre_extrap,
            post_extrap=track.post_extrap,
        )
        for param, track in automation.items()
    }


# NF-01（2026-11-15 计划 §7.1/§8.1）：两款新复合 FX 的扁平参数 schema。
# UI 保留嵌套结构，模型层只存这份扁平键（双向 adapter 在对话框侧维护
# 唯一映射表）；自动化键沿用 fx/{instance_id}/{param} 顶层键。

_VC_FORMANT_MODES = ("natural", "crisp")
_VC_TRACKING_MODES = ("vocal", "wide")
_VC_CARRIERS = ("saw", "pulse", "choir", "noise")


def _seed_of(d: dict) -> int:
    """H12：seed 宽容解析（坏字符串/浮点 → 0，不再抛 ValueError）。"""
    try:
        return int(d.get("random_seed", 0) or 0) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
@preserves_param_extras
class VoiceChangerParams:
    """Voice Changer（Formant → Pitch → Vocoder → OutputGain）参数。

    默认 = 原型 ``default_voice_neutral()``（中性：Formant 恒等、
    Pitch/Vocoder 关闭）。``default_*_demo()`` 是试听预设，不在新建实例
    时自动加载。random_seed 为持久元数据（uint32；复制实例继承，Random
    换新 seed 一次提交），不进连续自动化 lane。
    """

    enabled: bool = True
    output_gain_db: float = 0.0
    # Formant
    formant_enabled: bool = True
    formant_mode: str = "natural"
    formant_shift_st: float = 0.0
    formant_throat_pct: float = 0.0
    formant_gender_pct: float = 0.0
    formant_preserve_pct: float = 90.0
    formant_timbre_pct: float = 0.0
    formant_mix_pct: float = 100.0
    # Pitch
    pitch_enabled: bool = False
    pitch_tracking: str = "vocal"
    pitch_semitones: float = 0.0
    pitch_fine_cents: float = 0.0
    pitch_formant_link_pct: float = 0.0
    pitch_glide_ms: float = 18.0
    pitch_jitter_pct: float = 0.0
    pitch_mix_pct: float = 100.0
    # Vocoder
    vocoder_enabled: bool = False
    vocoder_carrier: str = "saw"
    vocoder_bands: int = 16
    vocoder_carrier_hz: float = 115.0
    vocoder_formant_st: float = 0.0
    vocoder_unvoiced_pct: float = 72.0
    vocoder_release_ms: float = 24.0
    vocoder_mix_pct: float = 100.0
    # 元数据
    random_seed: int = 0

    def __post_init__(self) -> None:
        if self.formant_mode not in _VC_FORMANT_MODES:
            raise ValueError(f"formant_mode must be in {_VC_FORMANT_MODES}")
        if self.pitch_tracking not in _VC_TRACKING_MODES:
            raise ValueError(f"pitch_tracking must be in {_VC_TRACKING_MODES}")
        if self.vocoder_carrier not in _VC_CARRIERS:
            raise ValueError(f"vocoder_carrier must be in {_VC_CARRIERS}")
        object.__setattr__(self, "random_seed",
                           int(self.random_seed) & 0xFFFFFFFF)

    @staticmethod
    def default() -> "VoiceChangerParams":
        return VoiceChangerParams()

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "output_gain_db": self.output_gain_db,
            "formant_enabled": self.formant_enabled,
            "formant_mode": self.formant_mode,
            "formant_shift_st": self.formant_shift_st,
            "formant_throat_pct": self.formant_throat_pct,
            "formant_gender_pct": self.formant_gender_pct,
            "formant_preserve_pct": self.formant_preserve_pct,
            "formant_timbre_pct": self.formant_timbre_pct,
            "formant_mix_pct": self.formant_mix_pct,
            "pitch_enabled": self.pitch_enabled,
            "pitch_tracking": self.pitch_tracking,
            "pitch_semitones": self.pitch_semitones,
            "pitch_fine_cents": self.pitch_fine_cents,
            "pitch_formant_link_pct": self.pitch_formant_link_pct,
            "pitch_glide_ms": self.pitch_glide_ms,
            "pitch_jitter_pct": self.pitch_jitter_pct,
            "pitch_mix_pct": self.pitch_mix_pct,
            "vocoder_enabled": self.vocoder_enabled,
            "vocoder_carrier": self.vocoder_carrier,
            "vocoder_bands": self.vocoder_bands,
            "vocoder_carrier_hz": self.vocoder_carrier_hz,
            "vocoder_formant_st": self.vocoder_formant_st,
            "vocoder_unvoiced_pct": self.vocoder_unvoiced_pct,
            "vocoder_release_ms": self.vocoder_release_ms,
            "vocoder_mix_pct": self.vocoder_mix_pct,
            "random_seed": self.random_seed,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "VoiceChangerParams":
        d = data or {}

        def _b(key: str, default: bool) -> bool:
            return bool(d.get(key, default))

        def _f(key: str, default: float) -> float:
            # H12：非有限值（NaN/±Inf）→ 默认（修前 NaN 增益穿透 DSP
            # 产生整块非有限输出；数值范围由 DSP np.clip 统一兜底）
            try:
                v = float(d.get(key, default))
            except (TypeError, ValueError):
                return default
            import math as _math
            return v if _math.isfinite(v) else default

        def _s(key: str, default: str, allowed: tuple) -> str:
            v = str(d.get(key, default) or default)
            return v if v in allowed else default

        def _i(key: str, default: int, lo: int, hi: int) -> int:
            try:
                return max(lo, min(hi, int(d.get(key, default))))
            except (TypeError, ValueError):
                return default

        return VoiceChangerParams(
            enabled=_b("enabled", True),
            output_gain_db=_f("output_gain_db", 0.0),
            formant_enabled=_b("formant_enabled", True),
            formant_mode=_s("formant_mode", "natural", _VC_FORMANT_MODES),
            formant_shift_st=_f("formant_shift_st", 0.0),
            formant_throat_pct=_f("formant_throat_pct", 0.0),
            formant_gender_pct=_f("formant_gender_pct", 0.0),
            formant_preserve_pct=_f("formant_preserve_pct", 90.0),
            formant_timbre_pct=_f("formant_timbre_pct", 0.0),
            formant_mix_pct=_f("formant_mix_pct", 100.0),
            pitch_enabled=_b("pitch_enabled", False),
            pitch_tracking=_s("pitch_tracking", "vocal", _VC_TRACKING_MODES),
            pitch_semitones=_f("pitch_semitones", 0.0),
            pitch_fine_cents=_f("pitch_fine_cents", 0.0),
            pitch_formant_link_pct=_f("pitch_formant_link_pct", 0.0),
            pitch_glide_ms=_f("pitch_glide_ms", 18.0),
            pitch_jitter_pct=_f("pitch_jitter_pct", 0.0),
            pitch_mix_pct=_f("pitch_mix_pct", 100.0),
            vocoder_enabled=_b("vocoder_enabled", False),
            vocoder_carrier=_s("vocoder_carrier", "saw", _VC_CARRIERS),
            vocoder_bands=_i("vocoder_bands", 16, 8, 32),
            vocoder_carrier_hz=_f("vocoder_carrier_hz", 115.0),
            vocoder_formant_st=_f("vocoder_formant_st", 0.0),
            vocoder_unvoiced_pct=_f("vocoder_unvoiced_pct", 72.0),
            vocoder_release_ms=_f("vocoder_release_ms", 24.0),
            vocoder_mix_pct=_f("vocoder_mix_pct", 100.0),
            random_seed=_seed_of(d),
        )


_SM_ROUTINGS = ("ring_grain_dist", "grain_ring_dist", "dist_grain_ring",
                "parallel")
_SM_RING_MODES = ("sine", "dual", "fm_ring", "rectify")
_SM_GRAIN_MODES = ("cloud", "reverse", "scatter")
_SM_DIST_CURVES = ("tape", "fold", "hard_clip", "bitcrush")


@dataclass(frozen=True)
@preserves_param_extras
class SpectralManglerParams:
    """Spectral Mangler（Ring／Grain／Dist 三模块 + 拓扑）参数。

    默认 = 原型 ``default_mangler_init()``（三模块关闭、global_mix=100、
    gain=0；其余保留 demo 数值作为再次开模块的起点）。
    ``output_limiter_enabled`` 生产 Init=False（Demo 可开）。
    """

    enabled: bool = True
    routing: str = "ring_grain_dist"
    global_mix_pct: float = 100.0
    output_gain_db: float = 0.0
    output_limiter_enabled: bool = False
    # Ring
    ring_enabled: bool = False
    ring_mode: str = "fm_ring"
    ring_carrier_hz: float = 340.0
    ring_lfo_rate_hz: float = 2.4
    ring_lfo_depth_pct: float = 35.0
    ring_warp_pct: float = 28.0
    ring_spread_pct: float = 45.0
    ring_mix_pct: float = 65.0
    # Grain
    grain_enabled: bool = False
    grain_freeze: bool = False
    grain_mode: str = "cloud"
    grain_size_ms: float = 85.0
    grain_density_hz: float = 24.0
    grain_pitch_st: float = 7.0
    grain_pitch_jitter_pct: float = 22.0
    grain_position_pct: float = 30.0
    grain_spray_pct: float = 45.0
    grain_feedback_pct: float = 38.0
    grain_mix_pct: float = 70.0
    # Dist
    dist_enabled: bool = False
    dist_curve: str = "fold"
    dist_drive_db: float = 14.5
    dist_fold_pct: float = 42.0
    dist_bias_pct: float = 12.0
    dist_tone_hz: float = 5200.0
    dist_resonance_pct: float = 25.0
    dist_mix_pct: float = 80.0
    # 元数据
    random_seed: int = 0
    #: H09：Freeze 捕获资产引用（项目侧 id；空 = 无资产。raw PCM 不进
    #: 参数 JSON；automation kind = never）
    freeze_capture_id: str = ""

    def __post_init__(self) -> None:
        if self.routing not in _SM_ROUTINGS:
            raise ValueError(f"routing must be in {_SM_ROUTINGS}")
        if self.ring_mode not in _SM_RING_MODES:
            raise ValueError(f"ring_mode must be in {_SM_RING_MODES}")
        if self.grain_mode not in _SM_GRAIN_MODES:
            raise ValueError(f"grain_mode must be in {_SM_GRAIN_MODES}")
        if self.dist_curve not in _SM_DIST_CURVES:
            raise ValueError(f"dist_curve must be in {_SM_DIST_CURVES}")
        object.__setattr__(self, "random_seed",
                           int(self.random_seed) & 0xFFFFFFFF)

    @staticmethod
    def default() -> "SpectralManglerParams":
        return SpectralManglerParams()

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "routing": self.routing,
            "global_mix_pct": self.global_mix_pct,
            "output_gain_db": self.output_gain_db,
            "output_limiter_enabled": self.output_limiter_enabled,
            "ring_enabled": self.ring_enabled,
            "ring_mode": self.ring_mode,
            "ring_carrier_hz": self.ring_carrier_hz,
            "ring_lfo_rate_hz": self.ring_lfo_rate_hz,
            "ring_lfo_depth_pct": self.ring_lfo_depth_pct,
            "ring_warp_pct": self.ring_warp_pct,
            "ring_spread_pct": self.ring_spread_pct,
            "ring_mix_pct": self.ring_mix_pct,
            "grain_enabled": self.grain_enabled,
            "grain_freeze": self.grain_freeze,
            "grain_mode": self.grain_mode,
            "grain_size_ms": self.grain_size_ms,
            "grain_density_hz": self.grain_density_hz,
            "grain_pitch_st": self.grain_pitch_st,
            "grain_pitch_jitter_pct": self.grain_pitch_jitter_pct,
            "grain_position_pct": self.grain_position_pct,
            "grain_spray_pct": self.grain_spray_pct,
            "grain_feedback_pct": self.grain_feedback_pct,
            "grain_mix_pct": self.grain_mix_pct,
            "dist_enabled": self.dist_enabled,
            "dist_curve": self.dist_curve,
            "dist_drive_db": self.dist_drive_db,
            "dist_fold_pct": self.dist_fold_pct,
            "dist_bias_pct": self.dist_bias_pct,
            "dist_tone_hz": self.dist_tone_hz,
            "dist_resonance_pct": self.dist_resonance_pct,
            "dist_mix_pct": self.dist_mix_pct,
            "random_seed": self.random_seed,
            "freeze_capture_id": self.freeze_capture_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SpectralManglerParams":
        d = data or {}

        def _b(key: str, default: bool) -> bool:
            return bool(d.get(key, default))

        def _f(key: str, default: float) -> float:
            # H12：非有限值（NaN/±Inf）→ 默认（修前 NaN 增益穿透 DSP
            # 产生整块非有限输出；数值范围由 DSP np.clip 统一兜底）
            try:
                v = float(d.get(key, default))
            except (TypeError, ValueError):
                return default
            import math as _math
            return v if _math.isfinite(v) else default

        def _s(key: str, default: str, allowed: tuple) -> str:
            v = str(d.get(key, default) or default)
            return v if v in allowed else default

        return SpectralManglerParams(
            enabled=_b("enabled", True),
            routing=_s("routing", "ring_grain_dist", _SM_ROUTINGS),
            global_mix_pct=_f("global_mix_pct", 100.0),
            output_gain_db=_f("output_gain_db", 0.0),
            output_limiter_enabled=_b("output_limiter_enabled", False),
            ring_enabled=_b("ring_enabled", False),
            ring_mode=_s("ring_mode", "fm_ring", _SM_RING_MODES),
            ring_carrier_hz=_f("ring_carrier_hz", 340.0),
            ring_lfo_rate_hz=_f("ring_lfo_rate_hz", 2.4),
            ring_lfo_depth_pct=_f("ring_lfo_depth_pct", 35.0),
            ring_warp_pct=_f("ring_warp_pct", 28.0),
            ring_spread_pct=_f("ring_spread_pct", 45.0),
            ring_mix_pct=_f("ring_mix_pct", 65.0),
            grain_enabled=_b("grain_enabled", False),
            grain_freeze=_b("grain_freeze", False),
            grain_mode=_s("grain_mode", "cloud", _SM_GRAIN_MODES),
            grain_size_ms=_f("grain_size_ms", 85.0),
            grain_density_hz=_f("grain_density_hz", 24.0),
            grain_pitch_st=_f("grain_pitch_st", 7.0),
            grain_pitch_jitter_pct=_f("grain_pitch_jitter_pct", 22.0),
            grain_position_pct=_f("grain_position_pct", 30.0),
            grain_spray_pct=_f("grain_spray_pct", 45.0),
            grain_feedback_pct=_f("grain_feedback_pct", 38.0),
            grain_mix_pct=_f("grain_mix_pct", 70.0),
            dist_enabled=_b("dist_enabled", False),
            dist_curve=_s("dist_curve", "fold", _SM_DIST_CURVES),
            dist_drive_db=_f("dist_drive_db", 14.5),
            dist_fold_pct=_f("dist_fold_pct", 42.0),
            dist_bias_pct=_f("dist_bias_pct", 12.0),
            dist_tone_hz=_f("dist_tone_hz", 5200.0),
            dist_resonance_pct=_f("dist_resonance_pct", 25.0),
            dist_mix_pct=_f("dist_mix_pct", 80.0),
            random_seed=_seed_of(d),
            freeze_capture_id=str(d.get("freeze_capture_id", "") or ""),
        )


# ─────────────────────────────────────────────────────────────────────────────
# P1（2026-11-19 计划第三阶段 #1-#7）新 FX 参数载体（默认值 = DSP DEFAULTS）。
# 注册进 _EFFECT_PARAM_REGISTRY 后 fx_versioning 判 supported、
# validate_effect_params 可归一化、机架默认参数工厂可实例化。
# 范围守卫从宽（DSP 侧 set_params 有硬 clamp）；from_dict 对坏类型回默认。
# ─────────────────────────────────────────────────────────────────────────────

def _params_f(key: str, data: dict, default: float) -> float:
    try:
        return float(data.get(key, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class DeEsserParams:
    """De-Esser（齿音消除）：band-pass 检测 + 流式 RMS 包络。"""
    threshold_dbfs: float = -32.0
    ratio: float = 4.0
    frequency: float = 6500.0
    q: float = 2.0
    attack_ms: float = 5.0
    release_ms: float = 60.0
    max_reduction_db: float = 12.0
    mode: str = "split"  # "wide" | "split"（减法互补）
    listen: bool = False
    enabled: bool = True

    @staticmethod
    def default() -> "DeEsserParams":
        return DeEsserParams()

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold_dbfs": self.threshold_dbfs, "ratio": self.ratio,
            "frequency": self.frequency, "q": self.q,
            "attack_ms": self.attack_ms, "release_ms": self.release_ms,
            "max_reduction_db": self.max_reduction_db, "mode": self.mode,
            "listen": self.listen, "enabled": self.enabled,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "DeEsserParams":
        mode = str(data.get("mode", "split") or "split")
        if mode not in ("wide", "split"):
            mode = "split"
        return DeEsserParams(
            threshold_dbfs=_params_f("threshold_dbfs", data, -32.0),
            ratio=_params_f("ratio", data, 4.0),
            frequency=_params_f("frequency", data, 6500.0),
            q=_params_f("q", data, 2.0),
            attack_ms=_params_f("attack_ms", data, 5.0),
            release_ms=_params_f("release_ms", data, 60.0),
            max_reduction_db=_params_f("max_reduction_db", data, 12.0),
            mode=mode,
            listen=bool(data.get("listen", False)),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass(frozen=True)
class TruePeakLimiterParams:
    """True Peak Limiter：4× 过采样 + lookahead + 平滑 release（dBTP）。"""
    ceiling_dbtp: float = -1.0
    lookahead_ms: float = 5.0
    release_ms: float = 80.0
    linked: bool = True
    enabled: bool = True

    @staticmethod
    def default() -> "TruePeakLimiterParams":
        return TruePeakLimiterParams()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ceiling_dbtp": self.ceiling_dbtp,
            "lookahead_ms": self.lookahead_ms,
            "release_ms": self.release_ms,
            "linked": self.linked, "enabled": self.enabled,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "TruePeakLimiterParams":
        return TruePeakLimiterParams(
            ceiling_dbtp=_params_f("ceiling_dbtp", data, -1.0),
            lookahead_ms=_params_f("lookahead_ms", data, 5.0),
            release_ms=_params_f("release_ms", data, 80.0),
            linked=bool(data.get("linked", True)),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass(frozen=True)
class DynamicEqParams:
    """Dynamic EQ：StripEQ Bell × 阈值驱动的动态增益（关闭动态==EQ）。"""
    frequency: float = 3000.0
    q: float = 1.4
    gain_db: float = 0.0
    threshold_dbfs: float = -30.0
    max_cut_db: float = 9.0
    max_boost_db: float = 0.0
    attack_ms: float = 8.0
    release_ms: float = 120.0
    dynamic_enabled: bool = True
    slope: float = 0.5
    enabled: bool = True

    @staticmethod
    def default() -> "DynamicEqParams":
        return DynamicEqParams()

    def to_dict(self) -> dict[str, Any]:
        return {
            "frequency": self.frequency, "q": self.q, "gain_db": self.gain_db,
            "threshold_dbfs": self.threshold_dbfs,
            "max_cut_db": self.max_cut_db, "max_boost_db": self.max_boost_db,
            "attack_ms": self.attack_ms, "release_ms": self.release_ms,
            "dynamic_enabled": self.dynamic_enabled, "slope": self.slope,
            "enabled": self.enabled,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "DynamicEqParams":
        return DynamicEqParams(
            frequency=_params_f("frequency", data, 3000.0),
            q=_params_f("q", data, 1.4),
            gain_db=_params_f("gain_db", data, 0.0),
            threshold_dbfs=_params_f("threshold_dbfs", data, -30.0),
            max_cut_db=_params_f("max_cut_db", data, 9.0),
            max_boost_db=_params_f("max_boost_db", data, 0.0),
            attack_ms=_params_f("attack_ms", data, 8.0),
            release_ms=_params_f("release_ms", data, 120.0),
            dynamic_enabled=bool(data.get("dynamic_enabled", True)),
            slope=_params_f("slope", data, 0.5),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass(frozen=True)
class BusCompParams:
    """Bus 胶合压缩：CompressorEffect 核心 + 模板 + SC 高通。"""
    template: str = "dx_glue"  # BUS_COMP_TEMPLATES: dx_glue | mx_glue
    sc_hpf_hz: float = 120.0
    enabled: bool = True

    @staticmethod
    def default() -> "BusCompParams":
        return BusCompParams()

    def to_dict(self) -> dict[str, Any]:
        return {
            "template": self.template, "sc_hpf_hz": self.sc_hpf_hz,
            "enabled": self.enabled,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "BusCompParams":
        template = str(data.get("template", "dx_glue") or "dx_glue")
        if template not in ("dx_glue", "mx_glue"):
            template = "dx_glue"
        return BusCompParams(
            template=template,
            sc_hpf_hz=_params_f("sc_hpf_hz", data, 120.0),
            enabled=bool(data.get("enabled", True)),
        )


def _default_multiband_bands() -> tuple[dict[str, float], ...]:
    return (
        {"threshold_db": -20.0, "ratio": 2.0, "release_ms": 220.0, "makeup_db": 0.0},
        {"threshold_db": -24.0, "ratio": 2.5, "release_ms": 150.0, "makeup_db": 0.0},
        {"threshold_db": -22.0, "ratio": 2.0, "release_ms": 180.0, "makeup_db": 0.0},
    )


@dataclass(frozen=True)
class MultibandCompParams:
    """三段 Multiband 压缩：减法互补分频 + 每段独立包络。"""
    crossover_low: float = 180.0
    crossover_high: float = 4200.0
    band_low: dict[str, float] | None = None
    band_mid: dict[str, float] | None = None
    band_high: dict[str, float] | None = None
    attack_ms: float = 10.0
    enabled: bool = True

    @staticmethod
    def default() -> "MultibandCompParams":
        return MultibandCompParams()

    def _bands(self) -> tuple[dict[str, float], ...]:
        defaults = _default_multiband_bands()
        return tuple(
            dict(band) if isinstance(band, dict) else dict(default)
            for band, default in zip(
                (self.band_low, self.band_mid, self.band_high), defaults))

    def to_dict(self) -> dict[str, Any]:
        return {
            "crossovers": [self.crossover_low, self.crossover_high],
            "bands": [dict(b) for b in self._bands()],
            "attack_ms": self.attack_ms, "enabled": self.enabled,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "MultibandCompParams":
        crossovers = data.get("crossovers")
        if (not isinstance(crossovers, (list, tuple)) or len(crossovers) != 2):
            crossovers = (180.0, 4200.0)
        bands = data.get("bands")
        if not isinstance(bands, (list, tuple)) or len(bands) != 3:
            bands = _default_multiband_bands()
        band_fields = []
        for band in bands:
            band_fields.append(dict(band) if isinstance(band, dict) else {})
        while len(band_fields) < 3:
            band_fields.append({})
        return MultibandCompParams(
            crossover_low=_params_f("crossover_low", dict(data),
                                    float(crossovers[0])),
            crossover_high=_params_f("crossover_high", dict(data),
                                     float(crossovers[1])),
            band_low=band_fields[0], band_mid=band_fields[1],
            band_high=band_fields[2],
            attack_ms=_params_f("attack_ms", data, 10.0),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass(frozen=True)
class ConvolutionReverbParams:
    """卷积混响：分区 FFT 卷积；IR 经 ir_path 装载（UI 文件选择/序列化）。"""
    ir_path: str = ""
    wet: float = 0.35
    dry: float = 1.0
    true_stereo: bool = False
    enabled: bool = True

    @staticmethod
    def default() -> "ConvolutionReverbParams":
        return ConvolutionReverbParams()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ir_path": self.ir_path, "wet": self.wet, "dry": self.dry,
            "true_stereo": self.true_stereo, "enabled": self.enabled,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ConvolutionReverbParams":
        return ConvolutionReverbParams(
            ir_path=str(data.get("ir_path", "") or ""),
            wet=_params_f("wet", data, 0.35),
            dry=_params_f("dry", data, 1.0),
            true_stereo=bool(data.get("true_stereo", False)),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass(frozen=True)
class TransientDesignerParams:
    """瞬态塑形：dB 域 attack/sustain 量（3dB 膝）。"""
    attack_db: float = 6.0
    sustain_db: float = 0.0
    attack_ms: float = 8.0
    sustain_ms: float = 80.0
    enabled: bool = True

    @staticmethod
    def default() -> "TransientDesignerParams":
        return TransientDesignerParams()

    def to_dict(self) -> dict[str, Any]:
        return {
            "attack_db": self.attack_db, "sustain_db": self.sustain_db,
            "attack_ms": self.attack_ms, "sustain_ms": self.sustain_ms,
            "enabled": self.enabled,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "TransientDesignerParams":
        return TransientDesignerParams(
            attack_db=_params_f("attack_db", data, 6.0),
            sustain_db=_params_f("sustain_db", data, 0.0),
            attack_ms=_params_f("attack_ms", data, 8.0),
            sustain_ms=_params_f("sustain_ms", data, 80.0),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass(frozen=True)
class TremoloAutoPanParams:
    """Tremolo / Auto-Pan：等功率声像或幅度调制（相位连续 LFO）。"""
    mode: str = "tremolo"  # "tremolo" | "auto_pan"
    rate_hz: float = 4.0
    depth: float = 0.7
    shape: str = "sine"
    enabled: bool = True

    @staticmethod
    def default() -> "TremoloAutoPanParams":
        return TremoloAutoPanParams()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode, "rate_hz": self.rate_hz,
            "depth": self.depth, "shape": self.shape,
            "enabled": self.enabled,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "TremoloAutoPanParams":
        mode = str(data.get("mode", "tremolo") or "tremolo")
        if mode not in ("tremolo", "auto_pan"):
            mode = "tremolo"
        return TremoloAutoPanParams(
            mode=mode,
            rate_hz=_params_f("rate_hz", data, 4.0),
            depth=_params_f("depth", data, 0.7),
            shape=str(data.get("shape", "sine") or "sine"),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass(frozen=True)
class ChorusFlangerParams:
    """Chorus / Flanger：相位连续调制延迟线 + 反馈尾音预算。"""
    mode: str = "chorus"  # "chorus" | "flanger"
    rate_hz: float = 0.6
    depth_ms: float = 3.0
    base_ms: float = 18.0
    feedback: float = 0.0
    voices: int = 2
    mix: float = 0.5
    enabled: bool = True

    @staticmethod
    def default() -> "ChorusFlangerParams":
        return ChorusFlangerParams()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode, "rate_hz": self.rate_hz,
            "depth_ms": self.depth_ms, "base_ms": self.base_ms,
            "feedback": self.feedback, "voices": self.voices,
            "mix": self.mix, "enabled": self.enabled,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ChorusFlangerParams":
        mode = str(data.get("mode", "chorus") or "chorus")
        if mode not in ("chorus", "flanger"):
            mode = "chorus"
        try:
            voices = int(data.get("voices", 2))
        except (TypeError, ValueError):
            voices = 2
        voices = max(1, min(4, voices))
        return ChorusFlangerParams(
            mode=mode,
            rate_hz=_params_f("rate_hz", data, 0.6),
            depth_ms=_params_f("depth_ms", data, 3.0),
            base_ms=_params_f("base_ms", data, 18.0),
            feedback=_params_f("feedback", data, 0.0),
            voices=voices,
            mix=_params_f("mix", data, 0.5),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass(frozen=True)
class PhaserParams:
    """Phaser：全通级联 + 相位连续 LFO 扫频。"""
    rate_hz: float = 0.5
    stages: int = 4
    base_hz: float = 600.0
    sweep_octaves: float = 2.5
    mix: float = 0.5
    feedback: float = 0.3
    enabled: bool = True

    @staticmethod
    def default() -> "PhaserParams":
        return PhaserParams()

    def to_dict(self) -> dict[str, Any]:
        return {
            "rate_hz": self.rate_hz, "stages": self.stages,
            "base_hz": self.base_hz, "sweep_octaves": self.sweep_octaves,
            "mix": self.mix, "feedback": self.feedback,
            "enabled": self.enabled,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "PhaserParams":
        try:
            stages = int(data.get("stages", 4))
        except (TypeError, ValueError):
            stages = 4
        stages = max(1, min(12, stages))
        return PhaserParams(
            rate_hz=_params_f("rate_hz", data, 0.5),
            stages=stages,
            base_hz=_params_f("base_hz", data, 600.0),
            sweep_octaves=_params_f("sweep_octaves", data, 2.5),
            mix=_params_f("mix", data, 0.5),
            feedback=_params_f("feedback", data, 0.3),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass(frozen=True)
@preserves_extras
class TrackEffectInstance:
    """FX 机架链中的单个效果器条目。

    Attributes:
        effect_type: 效果器类型标识（"strip_eq" / "limiter" / ...，对应 AudioEffect.effect_type）
        params: 效果器参数（dict 形式，即对应 *Params.to_dict() 的结果）
        enabled: 该条目的 bypass 开关（False=旁路此效果器，链中跳过）
        order: 链中顺序（0=最前，越大越靠后；UI 拖拽调整）
        instance_id: 唯一标识（uuid，用于 UI 定位 / 命令栈 diff）
        panel: 由哪个 UI 面板管理（"fx"=FX 列表多实例；"dynamics"=DY 胶囊单例）。
            仅 UI 层使用，DSP（to_dsp_entries）不读此字段，所有实例照常处理。
            默认 "fx"，旧项目无此字段时回填 "fx"（向后兼容）。
        algorithm_version: 实例创建/最后编辑时的 DSP 算法版本（R01）。
            0 = 未标注（历史工程）→ 按 legacy 兼容桶渲染；原样存回不覆写。
            未来版本（> audio_dsp.ALGORITHM_VERSION）同样原样保留。
    """
    effect_type: str
    params: dict
    enabled: bool = True
    order: int = 0
    instance_id: str = ""
    panel: str = "fx"
    algorithm_version: int = 0
    # R01：未知顶层键 opaque 保留（DESK-04A 模式，load→save→reload 无损）
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.effect_type:
            raise ValueError("effect_type cannot be empty")
        # R01：未知 effect_type 不再抛错（单实例不得清整条链 / 阻断工程加载）。
        # 数据以 opaque payload 保留；DSP 侧 EffectChain.set_params 跳过并告警，
        # 能力判定见 fx_versioning.effect_type_support（预览/导出 preflight 共用）。
        registry = _ensure_effect_param_registry()
        if self.effect_type not in registry:
            logger.warning(
                "TrackEffectInstance: unknown effect_type %r preserved as "
                "opaque payload (rendering will skip it with a diagnostic)",
                self.effect_type,
            )
        # AUD-19104：构造即复制 params dict——frozen 模型不得内嵌调用方
        # 持有的可变引用（否则对 params 的原位编辑会穿透所有快照）。
        object.__setattr__(self, "params", dict(self.params))
        try:
            av = max(0, int(self.algorithm_version or 0))
        except (TypeError, ValueError):
            av = 0
        if av == 0:
            # 0 = 本构建新建实例（UI/命令构造走默认值）→ 标记当前算法版本。
            # 旧工程加载由 from_dict 显式给 LEGACY_ALGORITHM_VERSION，
            # 不会进入本分支——两类"未标注"语义由此区分（R01）。
            from src.features.edit.core.audio_dsp import ALGORITHM_VERSION
            av = int(ALGORITHM_VERSION)
        object.__setattr__(self, "algorithm_version", av)
        # instance_id 为空时自动生成（frozen dataclass 用 object.__setattr__）
        if not self.instance_id:
            import uuid as _uuid
            object.__setattr__(self, "instance_id", str(_uuid.uuid4()))

    def to_dict(self) -> dict[str, Any]:
        result = {
            "effect_type": self.effect_type,
            "params": dict(self.params),
            "enabled": self.enabled,
            "order": self.order,
            "instance_id": self.instance_id,
            "panel": self.panel,
            "algorithm_version": self.algorithm_version,
        }
        result.update(self._extra_fields)
        return result

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "TrackEffectInstance":
        known = frozenset({
            "effect_type", "params", "enabled", "order",
            "instance_id", "panel", "algorithm_version",
        })
        # H01：旧数据（无版本键）映射为 0 = 未标注（早于版本标注的工程，
        # era 跨 43dfb2e3..1016a3cd 有历史歧义）——按最旧可复现语义渲染
        # 并在诊断中说明；不再猜测成 v2，显式升级走 command。
        from src.features.edit.core.fx_versioning import (
            UNSPECIFIED_ALGORITHM_VERSION,
        )
        try:
            raw_av = int(data.get("algorithm_version",
                                  UNSPECIFIED_ALGORITHM_VERSION)
                         or UNSPECIFIED_ALGORITHM_VERSION)
        except (TypeError, ValueError):
            raw_av = UNSPECIFIED_ALGORITHM_VERSION
        inst = TrackEffectInstance(
            effect_type=str(data.get("effect_type", "")),
            params=dict(data.get("params", {}) or {}),
            enabled=bool(data.get("enabled", True)),
            order=int(data.get("order", 0)),
            instance_id=str(data.get("instance_id", "")),
            panel=str(data.get("panel", "fx")),
            # 占位 1 避开 post_init 的 0→current 归一化；数据路径的
            # 0 = 未标注（era 歧义）须原样保留，与 UI 新建实例区分（H01）。
            algorithm_version=max(1, raw_av),
            _extra_fields=split_extras(data, known),
        )
        if raw_av <= 0:
            object.__setattr__(inst, "algorithm_version", 0)
        return inst


@dataclass(frozen=True)
class EffectChainParams:
    """FX 机架效果器链参数（轨道级）。

    持有有序的效果器条目列表。None / 空表示该轨道未使用 FX 机架。
    向后兼容：旧项目的 strip_eq 字段加载时自动迁移为链中的 strip_eq 条目。
    """
    effects: tuple[TrackEffectInstance, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "effects": [e.to_dict() for e in self.effects],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "EffectChainParams":
        effects_data = data.get("effects", []) if data else []
        effects: list[TrackEffectInstance] = []
        # R01：逐条容错——未知 effect_type 已在 TrackEffectInstance 内保留为
        # opaque payload；此处只跳过结构性损坏（非 dict）条目并记录，
        # 单条脏数据不再让整个 EditTrack / TimelineSequence 反序列化失败。
        for e in effects_data:
            if not isinstance(e, dict):
                logger.warning(
                    "EffectChainParams.from_dict: skip corrupted (non-dict) "
                    "effect entry %r", e)
                continue
            effects.append(TrackEffectInstance.from_dict(e))
        return EffectChainParams(effects=tuple(effects))

    @staticmethod
    def from_strip_eq(eq_params: "StripEQParams") -> "EffectChainParams":
        """从旧 strip_eq 字段迁移（向后兼容已保存项目）。"""
        return EffectChainParams(effects=(
            TrackEffectInstance(
                effect_type="strip_eq",
                params=eq_params.to_dict(),
                enabled=eq_params.enabled,
                order=0,
            ),
        ))

    def to_dsp_entries(self) -> list:
        """转成 EffectChain.set_params 的条目列表（CW-A1/R01: 5 元组）。

        Returns:
            [(effect_type, params dict, enabled, instance_id, algorithm_version),
             ...] 按 order 排序。instance_id 供 GR 遥测 / 侧链检源按实例定位；
            algorithm_version（R01）供 DSP 选择 legacy/current 兼容路径。
            未知 effect_type 的实例照常返回——DSP 侧跳过并告警（不静默清链）。
        """
        return [
            (e.effect_type, dict(e.params) if e.params else None, e.enabled,
             e.instance_id, e.algorithm_version, e.panel)
            for e in sorted(self.effects, key=lambda x: x.order)
        ]


@dataclass(frozen=True)
class EditTrack:
    """编辑轨道。支持视频轨、音频轨、字幕轨。

    字幕轨（track_type="subtitle"）使用 subtitle_clips 而非 clips 字段，
    因为 SubtitleClip 是独立的数据类（无需 asset_id/source_start 等视频字段）。
    """
    track_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    track_type: str = "video"
    index: int = 0
    clips: tuple[ClipInstance, ...] = ()
    # P10.3: 字幕片段（仅 track_type="subtitle" 时使用）
    subtitle_clips: tuple[SubtitleClip, ...] = ()
    transitions: tuple[TransitionInstance, ...] = ()
    is_enabled: bool = True
    is_locked: bool = False
    # P1-05 Sync Lock：波纹/提取（Extract）操作是否带动本轨后续 clip。
    # True（默认）= 与其他轨保持同步（现行为）；False = 本轨忽略波纹位移，
    # 允许有意失步（音乐轨/参考轨常见）。与 is_locked（编辑锁）语义正交。
    is_sync_locked: bool = True
    is_visible: bool = True
    is_muted: bool = False
    is_solo: bool = False
    volume: float = 1.0  # 音量（0.0-2.0，默认 1.0）
    pan: float = 0.0  # 声相（-1.0 左, 0.0 中, 1.0 右）；空间声像器的 lr 真源
    # CW-A1: 空间声像器参数（SpatialPannerParams.to_dict() 形式；空 dict=未使用，
    # 回退普通 pan/balance）。lr 由 pan 字段唯一承载以保自动化兼容。
    panner: dict = field(default_factory=dict)
    # W6: 轨道级 Strip EQ 参数（专业混音 strip 标配，None=禁用/默认）
    strip_eq: Optional["StripEQParams"] = None
    # W26: FX 机架效果器链（None=未使用）。向后兼容：旧项目 strip_eq 加载时自动迁移。
    effect_chain: Optional["EffectChainParams"] = None
    # W16: 轨道颜色（hex 字符串如 "#ff5722"，None=用默认主题色）
    color: Optional[str] = None
    # W19: 混音自动化关键帧（key=参数名 "volume"/"pan"/"mute"，value=KeyframeTrack）
    # 注意：dict 内容可变（KeyframeTrack 是 mutable dataclass），但 EditTrack 引用 frozen。
    # 改 automation 走 with_automation_* 方法返回新 EditTrack（dict 浅拷贝）。
    automation: dict = field(default_factory=dict)
    # AM-07: 自动化模式（"off"/"read"/"write"/"touch"/"latch"）。
    # 引擎据此判断是否读取 automation 曲线。Off=用静态值，Read/Write/Touch/Latch=读取曲线。
    # Write/Touch/Latch 的录制采集留待完整实现，当前先支持模式持久化与读取判断。
    automation_mode: str = "off"
    # W24: Bus 路由字段
    is_bus: bool = False  # True=该轨是 Bus/Aux 轨（无 clips，只接收 send/routing）
    output_bus_id: Optional[str] = None  # 输出目标 Bus 的 track_id（None=直接输出到 Master）
    sends: tuple["Send", ...] = ()  # 发送列表（post-fader）
    # AM-10 阶段 3.5：Stem 分组标签（专业影视 D/M/E 交付）。
    # 空字符串=未分组；常用值："dialogue"/"music"/"effects"/"narration"/"ambience"。
    # Stem 导出按此标签聚合轨道为 Dialogue/Music/Effects stems。
    stem_group: str = ""
    # SUB-P2-01/06: Phase 2A track-level caption metadata (language/role/
    # default_style/safe_anchor). Only meaningful on subtitle tracks; None on
    # video/audio tracks. Serves as the inheritance root for cue styles.
    caption_metadata: Optional[CaptionTrackMetadata] = None
    # A graphics track deliberately remains a video track for compatibility;
    # the role only changes UI labels and caption-graphic resolution. Kept at
    # the end of the dataclass to preserve legacy positional construction of
    # EditTrack(track_id, name, track_type, index, ...).
    track_role: str = "standard"

    def __post_init__(self) -> None:
        if not self.track_id:
            raise ValueError("track_id cannot be empty")
        if self.track_type not in ("video", "audio", "subtitle"):
            raise ValueError(f"Invalid track_type: {self.track_type}")
        if self.track_role not in ("standard", "graphics"):
            raise ValueError(f"Invalid track_role: {self.track_role}")
        if self.track_role == "graphics" and self.track_type != "video":
            raise ValueError("Only video tracks can have track_role='graphics'")
        if self.index < 0:
            raise ValueError(f"Invalid track index: {self.index}")
        if self.volume < 0.0 or self.volume > 2.0:
            raise ValueError(f"volume must be between 0.0 and 2.0: {self.volume}")
        if self.pan < -1.0 or self.pan > 1.0:
            raise ValueError(f"pan must be between -1.0 and 1.0: {self.pan}")
        if self.track_type == "subtitle" and self.clips:
            raise ValueError(
                f"Subtitle track {self.track_id} cannot carry regular media clips"
            )
        # Validate clip overlap for regular clips
        sorted_clips = sorted(self.clips, key=lambda c: c.timeline_start)
        for i in range(len(sorted_clips) - 1):
            curr, next_ = sorted_clips[i], sorted_clips[i + 1]
            if curr.timeline_end > next_.timeline_start:
                raise ValueError(f"Clips overlap: {curr.clip_id} and {next_.clip_id}")
        # P10.3: Validate subtitle clip overlap
        sorted_sub = sorted(self.subtitle_clips, key=lambda c: c.timeline_start)
        for i in range(len(sorted_sub) - 1):
            curr, next_ = sorted_sub[i], sorted_sub[i + 1]
            if curr.timeline_end > next_.timeline_start:
                raise ValueError(f"Subtitle clips overlap: {curr.clip_id} and {next_.clip_id}")
        # SUB-P0-03: subtitle invariant enforcement.
        #   * subtitle_clips may only live on a subtitle track (never video/audio),
        #   * every subtitle clip's track_id must equal this track's id (so an
        #     Update can never silently orphan a clip from its owner),
        #   * subtitle clip ids are unique within the track.
        if self.subtitle_clips:
            if self.track_type != "subtitle":
                raise ValueError(
                    f"Track {self.track_id} ({self.track_type}) cannot carry "
                    f"subtitle_clips; only subtitle tracks may."
                )
            seen_sub_ids: set[str] = set()
            for sub in self.subtitle_clips:
                if sub.track_id != self.track_id:
                    raise ValueError(
                        f"Subtitle clip {sub.clip_id} track_id={sub.track_id!r} "
                        f"does not match owner track {self.track_id!r}"
                    )
                if sub.clip_id in seen_sub_ids:
                    raise ValueError(
                        f"Duplicate subtitle clip_id within track "
                        f"{self.track_id}: {sub.clip_id}"
                    )
                seen_sub_ids.add(sub.clip_id)

    @property
    def duration_frames(self) -> int:
        all_ends: list[int] = []
        for clip in self.clips:
            all_ends.append(clip.timeline_end)
        for sub in self.subtitle_clips:
            all_ends.append(sub.timeline_end)
        return max(all_ends) if all_ends else 0

    # ── Subtitle clip accessors (P10.3) ──

    def get_subtitle_clip_at(self, timeline_frame: int) -> Optional[SubtitleClip]:
        """Return the subtitle clip active at the given timeline frame, if any."""
        for sub in self.subtitle_clips:
            if sub.timeline_start <= timeline_frame < sub.timeline_end:
                return sub
        return None

    def get_subtitle_clip_by_id(self, clip_id: str) -> Optional[SubtitleClip]:
        for sub in self.subtitle_clips:
            if sub.clip_id == clip_id:
                return sub
        return None

    def get_adjacent_subtitle_clips(self, clip_id: str) -> tuple[Optional[SubtitleClip], Optional[SubtitleClip]]:
        clip = self.get_subtitle_clip_by_id(clip_id)
        if clip is None:
            return (None, None)
        sorted_subs = sorted(self.subtitle_clips, key=lambda c: c.timeline_start)
        prev, next_ = None, None
        for i, c in enumerate(sorted_subs):
            if c.clip_id == clip_id:
                if i > 0:
                    prev = sorted_subs[i - 1]
                if i < len(sorted_subs) - 1:
                    next_ = sorted_subs[i + 1]
                break
        return (prev, next_)

    def with_subtitle_clip_added(self, clip: SubtitleClip) -> EditTrack:
        for existing in self.subtitle_clips:
            if clip.timeline_start < existing.timeline_end and clip.timeline_end > existing.timeline_start:
                raise ValueError(f"Subtitle clip overlaps with {existing.clip_id}")
        new_sub = tuple(sorted(self.subtitle_clips + (clip,), key=lambda c: c.timeline_start))
        return self._rebuild(subtitle_clips=new_sub)

    def with_subtitle_clip_removed(self, clip_id: str) -> EditTrack:
        new_sub = tuple(c for c in self.subtitle_clips if c.clip_id != clip_id)
        return self._rebuild(subtitle_clips=new_sub)

    def with_subtitle_clip_updated(self, clip: SubtitleClip) -> EditTrack:
        other = tuple(c for c in self.subtitle_clips if c.clip_id != clip.clip_id)
        for existing in other:
            if clip.timeline_start < existing.timeline_end and clip.timeline_end > existing.timeline_start:
                raise ValueError(f"Updated subtitle clip overlaps with {existing.clip_id}")
        new_sub = tuple(sorted(other + (clip,), key=lambda c: c.timeline_start))
        return self._rebuild(subtitle_clips=new_sub)

    def _rebuild(self, *,
                 index: int | None = None,
                 clips: tuple[ClipInstance, ...] | None = None,
                 subtitle_clips: tuple[SubtitleClip, ...] | None = None,
                 transitions: tuple[TransitionInstance, ...] | None = None,
                 is_enabled: bool | None = None,
                 is_locked: bool | None = None,
                 is_sync_locked: bool | None = None,
                 is_visible: bool | None = None,
                 is_muted: bool | None = None,
                 is_solo: bool | None = None,
                 volume: float | None = None,
                 pan: float | None = None,
                 panner: Optional[dict] = None,
                 strip_eq: Optional["StripEQParams"] | None = None,
                 color: Optional[str] | None = None,
                 automation: Optional[dict] = None,
                 automation_mode: Optional[str] = None,
                 is_bus: bool | None = None,
                 output_bus_id: Optional[str] | None = None,
                 sends: tuple["Send", ...] | None = None,
                 effect_chain: Optional["EffectChainParams"] | None = None,
                 stem_group: str | None = None,
                 track_role: str | None = None,
                 caption_metadata: Optional[CaptionTrackMetadata] | Literal["ellipsis"] = "ellipsis",
                 ) -> EditTrack:
        """Internal immutable rebuilder — centralizes field passing to avoid
        forgetting new fields (like subtitle_clips) in every with_* method.

        注意：strip_eq/color/output_bus_id/effect_chain 参数 None 表示"不传"，
        显式传 None 需用对应 with_*。automation 参数 None 表示"不传"；要清空请传空 dict {}。
        automation_mode 参数 None 表示"不传"。stem_group 参数 None 表示"不传"。
        caption_metadata 参数用 "ellipsis" 哨兵表示"不传"；显式传 None 表示"清除"。
        """
        new_track = EditTrack(
            track_id=self.track_id, name=self.name, track_type=self.track_type,
            track_role=self.track_role if track_role is None else track_role,
            index=self.index if index is None else index,
            clips=self.clips if clips is None else clips,
            subtitle_clips=self.subtitle_clips if subtitle_clips is None else subtitle_clips,
            transitions=self.transitions if transitions is None else transitions,
            is_enabled=self.is_enabled if is_enabled is None else is_enabled,
            is_locked=self.is_locked if is_locked is None else is_locked,
            is_sync_locked=(
                self.is_sync_locked if is_sync_locked is None else is_sync_locked
            ),
            is_visible=self.is_visible if is_visible is None else is_visible,
            is_muted=self.is_muted if is_muted is None else is_muted,
            is_solo=self.is_solo if is_solo is None else is_solo,
            volume=self.volume if volume is None else volume,
            pan=self.pan if pan is None else pan,
            # panner 参数 None 表示"不传"；要清空请传空 dict {}
            panner=dict(self.panner) if panner is None else dict(panner),
            strip_eq=self.strip_eq if strip_eq is None else strip_eq,
            color=self.color if color is None else color,
            # AUD-19104：未显式更新时深拷贝（KeyframeTrack 可变），派生
            # 快照不再与旧快照共享同一批 automation 轨实例。
            automation=(
                _deep_copy_automation(self.automation)
                if automation is None else automation
            ),
            automation_mode=self.automation_mode if automation_mode is None else automation_mode,
            is_bus=self.is_bus if is_bus is None else is_bus,
            output_bus_id=self.output_bus_id if output_bus_id is None else output_bus_id,
            sends=self.sends if sends is None else sends,
            effect_chain=self.effect_chain if effect_chain is None else effect_chain,
            stem_group=self.stem_group if stem_group is None else stem_group,
            caption_metadata=(self.caption_metadata
                              if caption_metadata == "ellipsis" else caption_metadata),
            _extra_fields=dict(self._extra_fields),
        )
        # C12：clip 几何变化（显式传 clips 且未显式替换 transitions）时，
        # 转场自动重新锚定到引用片段的新边缘。显式传 transitions 的是
        # 精确快照恢复（C11 undo），不做推导。
        if clips is not None and transitions is None:
            return new_track.reanchor_transitions()
        return new_track

    def reanchor_transitions(self) -> "EditTrack":
        """C12：把轨道上的转场重新锚定到引用片段的当前边缘。

        移动/波纹/滚动/变速等时间变换此前只改 clips，转场
        ``center_frame`` 留在旧绝对时间，validate_timeline 报
        "not anchored" / "range exceeds"。规则：

        - 双边转场：两片仍相接（A.end == B.start）时 center 移到新切点，
          duration 按两侧可用长度收紧；两片被拉开（出现间隙）则按既定
          策略删除该切点转场（undo 由 C11 快照完整恢复）。
        - 单边入点转场（right-only）：center=right.timeline_start，
          duration 收紧到片段长度，alignment 归一 start_on_cut。
        - 单边出点转场（left-only）：center=left.timeline_end，
          duration 收紧到片段长度，alignment 归一 end_on_cut。
        - 引用片段缺失：原样保留（不删），由删除命令的快照恢复或
          validate 报告，避免分步 undo 中间态丢内容。
        """
        if not self.transitions:
            return self
        by_id = {c.clip_id: c for c in self.clips}
        new_transitions: list[TransitionInstance] = []
        changed = False
        for t in self.transitions:
            left = by_id.get(t.left_clip_id) if t.left_clip_id else None
            right = by_id.get(t.right_clip_id) if t.right_clip_id else None
            if left is not None and right is not None:
                if left.timeline_end != right.timeline_start:
                    # 分离策略：删除（时间变换把它变成了不可能的切点）
                    changed = True
                    continue
                center = left.timeline_end
                if t.alignment == "centered":
                    max_duration = 2 * min(
                        left.timeline_duration, right.timeline_duration
                    )
                else:
                    max_duration = (
                        left.timeline_duration + right.timeline_duration
                    )
                duration = max(1, min(t.duration, max_duration))
                updated = replace(t, center_frame=center, duration=duration)
            elif right is not None:
                duration = max(1, min(t.duration, right.timeline_duration))
                updated = replace(
                    t,
                    center_frame=right.timeline_start,
                    duration=duration,
                    alignment="start_on_cut",
                )
            elif left is not None:
                duration = max(1, min(t.duration, left.timeline_duration))
                updated = replace(
                    t,
                    center_frame=left.timeline_end,
                    duration=duration,
                    alignment="end_on_cut",
                )
            else:
                new_transitions.append(t)
                continue
            # TransitionInstance.__eq__ 只比较 transition_id（身份相等），
            # 变化检测必须逐字段比较
            if (
                updated.center_frame != t.center_frame
                or updated.duration != t.duration
                or updated.alignment != t.alignment
            ):
                changed = True
            new_transitions.append(updated)
        if not changed:
            return self
        return self._rebuild(transitions=tuple(new_transitions))

    def get_clip_at(self, timeline_frame: int) -> Optional[ClipInstance]:
        for clip in self.clips:
            if clip.timeline_start <= timeline_frame < clip.timeline_end:
                return clip
        return None

    def get_clip_by_id(self, clip_id: str) -> Optional[ClipInstance]:
        for clip in self.clips:
            if clip.clip_id == clip_id:
                return clip
        return None

    def get_adjacent_clips(self, clip_id: str) -> tuple[Optional[ClipInstance], Optional[ClipInstance]]:
        clip = self.get_clip_by_id(clip_id)
        if clip is None:
            return (None, None)
        sorted_clips = sorted(self.clips, key=lambda c: c.timeline_start)
        prev, next_ = None, None
        for i, c in enumerate(sorted_clips):
            if c.clip_id == clip_id:
                if i > 0:
                    prev = sorted_clips[i - 1]
                if i < len(sorted_clips) - 1:
                    next_ = sorted_clips[i + 1]
                break
        return (prev, next_)

    def with_clip_added(self, clip: ClipInstance) -> EditTrack:
        for existing in self.clips:
            if clip.timeline_start < existing.timeline_end and clip.timeline_end > existing.timeline_start:
                raise ValueError(f"Clip overlaps with {existing.clip_id}")
        new_clips = tuple(sorted(self.clips + (clip,), key=lambda c: c.timeline_start))
        return self._rebuild(clips=new_clips)

    def with_clip_removed(self, clip_id: str) -> EditTrack:
        new_clips = tuple(c for c in self.clips if c.clip_id != clip_id)
        new_trans = tuple(t for t in self.transitions if t.left_clip_id != clip_id and t.right_clip_id != clip_id)
        return self._rebuild(clips=new_clips, transitions=new_trans)

    def with_clip_updated(self, clip: ClipInstance) -> EditTrack:
        other_clips = tuple(c for c in self.clips if c.clip_id != clip.clip_id)
        for existing in other_clips:
            if clip.timeline_start < existing.timeline_end and clip.timeline_end > existing.timeline_start:
                raise ValueError(f"Updated clip overlaps with {existing.clip_id}")
        new_clips = tuple(sorted(other_clips + (clip,), key=lambda c: c.timeline_start))
        return self._rebuild(clips=new_clips)

    def with_transition_added(self, transition: TransitionInstance) -> EditTrack:
        """添加转场并验证其落在合法的切点或片段边缘。"""
        left = self.get_clip_by_id(transition.left_clip_id) if transition.left_clip_id else None
        right = self.get_clip_by_id(transition.right_clip_id) if transition.right_clip_id else None
        if not transition.left_clip_id and not transition.right_clip_id:
            raise ValueError("Transition must reference at least one clip")
        if transition.left_clip_id and left is None:
            raise ValueError(f"Transition left clip not found: {transition.left_clip_id}")
        if transition.right_clip_id and right is None:
            raise ValueError(f"Transition right clip not found: {transition.right_clip_id}")

        if left is not None and right is not None:
            _, next_ = self.get_adjacent_clips(transition.left_clip_id)
            if next_ is None or next_.clip_id != transition.right_clip_id:
                raise ValueError(f"Clips {transition.left_clip_id} and {transition.right_clip_id} are not adjacent")
            if transition.start_frame < left.timeline_start or transition.end_frame > right.timeline_end:
                raise ValueError("Transition range exceeds the adjacent clips")
        elif right is not None:
            if transition.alignment != "start_on_cut" or transition.center_frame != right.timeline_start:
                raise ValueError("A head transition must start at the clip beginning")
            if transition.start_frame < right.timeline_start or transition.end_frame > right.timeline_end:
                raise ValueError("Head transition range exceeds the clip")
        elif left is not None:
            if transition.alignment != "end_on_cut" or transition.center_frame != left.timeline_end:
                raise ValueError("A tail transition must end at the clip end")
            if transition.start_frame < left.timeline_start or transition.end_frame > left.timeline_end:
                raise ValueError("Tail transition range exceeds the clip")
        new_transitions = self.transitions + (transition,)
        return self._rebuild(transitions=new_transitions)

    def with_volume(self, new_volume: float) -> EditTrack:
        """创建新实例，修改音量。"""
        return self._rebuild(volume=new_volume)

    def with_pan(self, new_pan: float) -> EditTrack:
        """创建新实例，修改声相（-1.0 左, 0.0 中, 1.0 右）。"""
        return self._rebuild(pan=new_pan)

    def with_panner(self, new_panner: dict) -> EditTrack:
        """CW-A1: 创建新实例，修改空间声像器参数（空 dict=未使用）。"""
        return self._rebuild(panner=dict(new_panner or {}))

    def with_strip_eq(self, new_strip_eq: Optional["StripEQParams"]) -> EditTrack:
        """创建新实例，修改 Strip EQ 参数。传 None 表示禁用 EQ（旁路）。"""
        # AUD-19100：dataclasses.replace 自动携带全部字段（含 is_sync_locked/
        # track_role）。此前手工枚举字段构造会静默把 is_sync_locked 重置回
        # True、track_role 重置回 "standard"（重命名 graphics 轨会使其失去
        # track_role，字幕图形渲染链路失效）。None 经 replace 正常传递。
        return replace(self, strip_eq=new_strip_eq)

    def with_effect_chain(self, new_chain: Optional["EffectChainParams"]) -> EditTrack:
        """W26: 创建新实例，修改 FX 机架效果器链。传 None 表示清空链。"""
        # AUD-19100：同 with_strip_eq——replace 携带全部字段。
        return replace(self, effect_chain=new_chain)

    def with_visibility(self, visible: bool) -> EditTrack:
        """创建新实例，修改可见性。"""
        return self._rebuild(is_visible=visible)

    def with_muted(self, muted: bool) -> EditTrack:
        """创建新实例，修改静音状态。"""
        return self._rebuild(is_muted=muted)

    def with_solo(self, solo: bool) -> EditTrack:
        """创建新实例，修改独奏状态。"""
        return self._rebuild(is_solo=solo)

    def with_sync_locked(self, sync_locked: bool) -> EditTrack:
        """创建新实例，修改同步锁状态（P1-05）。"""
        return self._rebuild(is_sync_locked=sync_locked)

    def with_name(self, name: str) -> EditTrack:
        """创建新实例，修改轨道名称。"""
        # AUD-19100：replace 携带全部字段（含 effect_chain/is_sync_locked/
        # track_role），重命名不再清空轨道 FX 链、不再重置同步锁与角色。
        return replace(self, name=name)

    def with_color(self, color: Optional[str]) -> EditTrack:
        """W16: 创建新实例，修改轨道颜色（hex 字符串，None=默认）。"""
        # AUD-19100：replace 携带全部字段；None 经 replace 正常传递（清除颜色）。
        return replace(self, color=color)

    # ── W24: Bus 路由 ──

    def with_automation_mode(self, mode: str) -> EditTrack:
        """AM-07: 设置自动化模式（off/read/write/touch/latch）。"""
        return self._rebuild(automation_mode=str(mode))

    def with_stem_group(self, stem_group: str) -> EditTrack:
        """AM-10 阶段 3.5：设置 Stem 分组标签（dialogue/music/effects/narration/ambience）。

        空字符串=清除分组。Stem 导出按此标签聚合轨道。
        """
        return self._rebuild(stem_group=str(stem_group))

    def with_caption_metadata(
        self, metadata: Optional[CaptionTrackMetadata]
    ) -> EditTrack:
        """SUB-P2-01: 设置该字幕轨的语言/角色/默认样式/安全锚点。

        传 None 清除（回到无元数据状态）。默认样式的变更通过 resolve_style
        自动传播到所有未覆盖该字段的 cue。
        """
        return self._rebuild(caption_metadata=metadata)

    def with_output_bus(self, output_bus_id: Optional[str]) -> EditTrack:
        """W24: 设置该轨的输出目标 Bus（None=输出到 Master）。"""
        # AUD-19100：replace 携带全部字段；None 经 replace 正常传递。
        return replace(self, output_bus_id=output_bus_id)

    def with_sends(self, sends: tuple) -> EditTrack:
        """W24: 设置该轨的发送列表。"""
        return self._rebuild(sends=sends)

    def with_send_added(self, send: "Send") -> EditTrack:
        """W24: 添加一个发送。"""
        return self.with_sends(self.sends + (send,))

    def with_send_removed(self, send_id: str) -> EditTrack:
        """W24: 移除一个发送。"""
        return self.with_sends(tuple(s for s in self.sends if s.send_id != send_id))

    def with_send_updated(self, send: "Send") -> EditTrack:
        """W24: 更新一个发送。"""
        new_sends = tuple(send if s.send_id == send.send_id else s for s in self.sends)
        return self.with_sends(new_sends)

    # ── W19: Automation 访问器 ──

    # automation 参数名常量
    AUTOMATION_VOLUME = "volume"
    AUTOMATION_PAN = "pan"
    AUTOMATION_MUTE = "mute"


    def get_automation_value(self, param: str, frame: int, default: float) -> float:
        """查询某参数在指定帧的自动化值（线性插值）。

        若该参数无 automation，返回 default（即静态值）。
        """
        track = self.automation.get(param)
        if track is None or not track.keyframes:
            return default
        return track.get_value(frame, default)

    def has_automation(self, param: str) -> bool:
        """该参数是否有 automation 关键帧。"""
        track = self.automation.get(param)
        return track is not None and len(track.keyframes) > 0

    def _clone_automation(self) -> dict:
        """深拷贝 automation dict（含 KeyframeTrack）。

        AUD-19104：统一走模块级 ``_deep_copy_automation``（同时保留
        pre_extrap / post_extrap 外推字段，旧实现会静默丢弃）。
        """
        return _deep_copy_automation(self.automation)

    def with_automation_keyframe_added(
        self, param: str, keyframe: "Keyframe", min_val: float, max_val: float
    ) -> EditTrack:
        """添加一个 automation 关键帧。"""
        new_auto = self._clone_automation()
        track = new_auto.get(param)
        if track is None:
            track = KeyframeTrack(param_name=param, min_val=min_val, max_val=max_val)
            new_auto[param] = track
        track.add_keyframe(keyframe)
        return self._rebuild(automation=new_auto)

    def with_automation_keyframe_removed(self, param: str, frame: int) -> EditTrack:
        """移除某帧的 automation 关键帧。"""
        new_auto = self._clone_automation()
        track = new_auto.get(param)
        if track is not None:
            track.remove_keyframe(frame)
            if not track.keyframes:
                # 关键帧清空后移除该 param 的 track
                del new_auto[param]
        return self._rebuild(automation=new_auto)

    def with_automation_keyframe_updated(
        self, param: str, frame: int, new_keyframe: "Keyframe"
    ) -> EditTrack:
        """更新某帧的 automation 关键帧。"""
        new_auto = self._clone_automation()
        track = new_auto.get(param)
        if track is not None:
            # remove 旧的，add 新的（add 会处理同帧替换）
            track.remove_keyframe(frame)
            track.add_keyframe(new_keyframe)
        return self._rebuild(automation=new_auto)

    def with_automation_cleared(self, param: str) -> EditTrack:
        """清空某参数的所有 automation 关键帧。"""
        new_auto = self._clone_automation()
        new_auto.pop(param, None)
        return self._rebuild(automation=new_auto)

    # R28/F-R09：未知字段保留（与 ClipInstance._extra_fields 同策略）
    _extra_fields: Dict[str, Any] = field(
        default_factory=dict, repr=False, compare=False
    )

    def to_dict(self) -> dict[str, Any]:
        payload = {"track_id": self.track_id, "name": self.name, "track_type": self.track_type,
            "track_role": self.track_role,
            "index": self.index, "clips": [c.to_dict() for c in self.clips],
            "subtitle_clips": [c.to_dict() for c in self.subtitle_clips],
            "transitions": [t.to_dict() for t in self.transitions],
            "is_enabled": self.is_enabled, "is_locked": self.is_locked,
            "is_sync_locked": self.is_sync_locked,
            "is_visible": self.is_visible,
            "is_muted": self.is_muted, "is_solo": self.is_solo, "volume": self.volume,
            "pan": self.pan,
            "panner": dict(self.panner),
            "strip_eq": self.strip_eq.to_dict() if self.strip_eq is not None else None,
            "effect_chain": self.effect_chain.to_dict() if self.effect_chain is not None else None,
            "color": self.color,
            "automation": {k: v.to_dict() for k, v in self.automation.items()} if self.automation else {},
            "automation_mode": self.automation_mode,
            "is_bus": self.is_bus,
            "output_bus_id": self.output_bus_id,
            "sends": [s.to_dict() for s in self.sends],
            "stem_group": self.stem_group,
            # SUB-P2-01: track-level caption metadata (None on non-subtitle tracks).
            "caption_metadata": (self.caption_metadata.to_dict()
                                 if self.caption_metadata is not None else None),
        }
        # R28：未知字段随载荷保留（未来版本写入的字段 roundtrip 不丢）
        payload.update(self._extra_fields)
        return payload

    @staticmethod
    def from_dict(data: dict[str, Any]) -> EditTrack:
        strip_eq_data = data.get("strip_eq")
        # W26: 若项目无 effect_chain 但有 strip_eq，将其作为链中的 strip_eq 条目
        # （StripEQParams 现在是多频段形状，旧三段字段会被 from_dict 优雅降级为 10-band flat）
        chain_data = data.get("effect_chain")
        effect_chain: Optional[EffectChainParams] = None
        if chain_data:
            effect_chain = EffectChainParams.from_dict(chain_data)
        elif strip_eq_data:
            eq = StripEQParams.from_dict(strip_eq_data)
            effect_chain = EffectChainParams.from_strip_eq(eq)
        auto_data = data.get("automation") or {}
        automation: dict = {}
        for param, track_data in auto_data.items():
            try:
                automation[param] = KeyframeTrack.from_dict(track_data)
            except (TypeError, ValueError, KeyError, AttributeError) as exc:
                # AUD-19102：收窄异常类型并记录——宽泛 except Exception: pass
                # 会把音量/声像/静音关键帧的混音自动化无日志静默丢弃，且
                # 掩盖 from_dict 内部的编程错误。AttributeError 在内：
                # 非映射载荷（如字符串）访问 .get 即抛，是本解析器对脏
                # 数据的自有失败模式。
                logger.warning(
                    "EditTrack.from_dict: 丢弃损坏的 automation 轨 %r: %s",
                    param, exc,
                )
        # AUD-37809 防御：历史/损坏数据可能含空 target_bus_id 的 send
        # （Send 构造校验会抛 ValueError，单条脏数据会让整个 EditTrack
        # 反序列化失败）。加载侧过滤空目标项，其余正常载入。
        sends = tuple(
            Send.from_dict(s)
            for s in (data.get("sends") or ())
            if isinstance(s, dict) and s.get("target_bus_id")
        )
        # CW-A1: 空间声像器参数经 SpatialPannerParams 归一化；缺失/损坏 → 空 dict
        panner_data = data.get("panner")
        panner: dict = (
            SpatialPannerParams.from_dict(panner_data).to_dict()
            if isinstance(panner_data, dict) and panner_data else {}
        )
        track = EditTrack(
            track_id=data.get("track_id", str(uuid.uuid4())), name=data.get("name", ""),
            track_type=data.get("track_type", "video"), index=data.get("index", 0),
            track_role=str(data.get("track_role", "standard") or "standard"),
            clips=tuple(ClipInstance.from_dict(c) for c in data.get("clips", [])),
            subtitle_clips=tuple(SubtitleClip.from_dict(c) for c in data.get("subtitle_clips", [])),
            transitions=tuple(TransitionInstance.from_dict(t) for t in data.get("transitions", [])),
            is_enabled=data.get("is_enabled", True), is_locked=data.get("is_locked", False),
            is_sync_locked=data.get("is_sync_locked", True),
            is_visible=data.get("is_visible", True),
            is_muted=data.get("is_muted", False), is_solo=data.get("is_solo", False),
            volume=data.get("volume", 1.0), pan=data.get("pan", 0.0),
            panner=panner,
            strip_eq=StripEQParams.from_dict(strip_eq_data) if strip_eq_data else None,
            effect_chain=effect_chain,
            color=data.get("color"),
            automation=automation,
            automation_mode=str(data.get("automation_mode", "off") or "off"),
            is_bus=bool(data.get("is_bus", False)),
            output_bus_id=data.get("output_bus_id"),
            sends=sends,
            stem_group=str(data.get("stem_group", "") or ""),
            # SUB-P2-01: caption metadata absent on legacy tracks → None.
            caption_metadata=(CaptionTrackMetadata.from_dict(data["caption_metadata"])
                              if data.get("caption_metadata") else None),
        )
        # R28：未知键进 extras（已知键以本解析器为准）
        _KNOWN_TRACK_KEYS = frozenset({
            "track_id", "name", "track_type", "track_role", "index", "clips",
            "subtitle_clips", "transitions", "is_enabled", "is_locked",
            "is_sync_locked", "is_visible", "is_muted", "is_solo", "volume",
            "pan", "panner", "strip_eq", "effect_chain", "color", "automation",
            "automation_mode", "is_bus", "output_bus_id", "sends",
            "stem_group", "caption_metadata",
        })
        extras = {
            key: value for key, value in data.items()
            if key not in _KNOWN_TRACK_KEYS
        }
        if extras:
            object.__setattr__(track, "_extra_fields", extras)
        return track

    def __hash__(self) -> int:
        return hash(self.track_id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, EditTrack):
            return NotImplemented
        return self.track_id == other.track_id




# MX-10: FX 参数自动化命名空间。track.automation 的 key 采用
# "fx/{instance_id}/{param_key}"（模型 dict key 天然泛型，序列化/快照/
# 命令层零改动即支持）；块边界处由采样器按当前帧求值并覆盖链参数。
AUTOMATION_FX_PREFIX = "fx/"


def parse_fx_automation_key(key: str) -> Optional[tuple]:
    """解析 "fx/{instance_id}/{param_key}" → (instance_id, param_key)。

    非本命名空间（volume/pan/mute 或其他）返回 None。
    instance_id 含 "/" 时按第一个 "/" 拆分（instance_id 由 uuid 生成，
    不含 "/"；param_key 为 schema 键，同样不含 "/"）。
    """
    if not key.startswith(AUTOMATION_FX_PREFIX):
        return None
    rest = key[len(AUTOMATION_FX_PREFIX):]
    if "/" not in rest:
        return None
    iid, _, pkey = rest.partition("/")
    if not iid or not pkey:
        return None
    return iid, pkey


#: H12：参数自动化类别（automation_kind 真源）。
#: never      = 元数据/资源引用（seed、总开关）——不得进入数值 lane；
#: event      = 离散事件语义（routing/模式/曲线/Freeze）——fx_sampler 块
#:               边界重设天然支持（每块一次，无中间插值意义）；
#: continuous = 连续参数（浮点，单位各异；块边界求值 + DSP 侧平滑/淡化）。
FX_AUTOMATION_NEVER = frozenset({
    "random_seed", "enabled", "algorithm_version", "freeze_capture_id",
})
FX_AUTOMATION_EVENT = frozenset({
    "routing", "formant_mode", "pitch_tracking", "vocoder_carrier",
    "ring_mode", "grain_mode", "dist_curve", "grain_freeze",
})


def automation_param_kind(param_key: str) -> str:
    """H12：自动化 eligibility（UI 键列表与采样消费共用）。"""
    if param_key in FX_AUTOMATION_NEVER:
        return "never"
    if param_key in FX_AUTOMATION_EVENT:
        return "event"
    return "continuous"


def validate_effect_params(effect_type: str, params: dict) -> tuple:
    """H12：参数统一校验（预览/导出/保存前共用）。

    Returns:
        (cleaned_dict, diagnostics)：cleaned 为经 ``XParams.from_dict``
        归一化的 dict（非有限值/坏类型回退默认，未知键保留）；diagnostics
        为被替换字段列表 ``[(key, raw, replacement), ...]``。未知
        effect_type 返回 (原 dict, [])（opaque payload 原样，调用方走
        fx_versioning 诊断）。
    """
    registry = _ensure_effect_param_registry()
    cls = registry.get(effect_type)
    if cls is None or not isinstance(params, dict):
        return dict(params or {}), []
    try:
        cleaned_obj = cls.from_dict(params)
    except Exception:
        return dict(params or {}), [("__all__", "unparsable", "raw kept")]
    cleaned = cleaned_obj.to_dict()
    diags = []
    for key, raw in (params or {}).items():
        if key not in cleaned:
            continue
        try:
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                import math as _math
                if not _math.isfinite(float(raw)):
                    diags.append((key, raw, cleaned[key]))
                    continue
            if cleaned.get(key) != raw and str(cleaned.get(key)) != str(raw):
                # 类型/枚举/范围归一（int("3")→3 等静默归一不计；只报
                # 数值被替换与枚举回退）
                if isinstance(raw, (int, float)) and isinstance(
                        cleaned.get(key), (int, float)):
                    if float(raw) == float(cleaned[key]):
                        continue
                diags.append((key, raw, cleaned[key]))
        except Exception:
            pass
    return cleaned, diags


def build_track_fx_sampler(track: Any) -> Optional[Any]:
    """MX-10: 从 track.automation 的 fx/* 键构建 ``frame -> entries`` 采样器。

    返回 None 当：automation_mode == "off" / 无 fx 键 / 无效果链。
    采样器闭包捕获 track 快照（frozen dataclass）——链或曲线编辑产生新
    track 对象，由调用方（edit_page 同步 / renderer 节点构建）重建采样器。

    求值语义：块边界粒度（每块按起始帧求值一次，与 clip FX 的
    animated_sampler 同型）；keyframe 插值由 KeyframeTrack.get_value 完成；
    未知参数键（链参数里不存在）被忽略。
    """
    if str(getattr(track, "automation_mode", "off") or "off").lower() == "off":
        return None
    chain = getattr(track, "effect_chain", None)
    if chain is None:
        return None
    fx_tracks: dict = {}
    for key, kt in (getattr(track, "automation", None) or {}).items():
        parsed = parse_fx_automation_key(str(key))
        if parsed is not None and getattr(kt, "keyframes", None):
            fx_tracks[parsed] = kt
    if not fx_tracks:
        return None
    base_entries = chain.to_dsp_entries()

    def _sampler(frame: int) -> list:
        entries = []
        for entry in base_entries:
            etype, params, enabled, iid = entry[0], entry[1], entry[2], entry[3]
            av = entry[4] if len(entry) > 4 else 0
            p = dict(params) if params else {}
            for (eid, pkey), kt in fx_tracks.items():
                if eid == iid and pkey in p:
                    # H12：never 类参数（seed/enabled/版本）不进数值 lane
                    if automation_param_kind(pkey) == "never":
                        continue
                    try:
                        p[pkey] = kt.get_value(int(frame), p[pkey])
                    except (TypeError, ValueError, KeyError):
                        pass
            entries.append((etype, p, enabled, iid, av,
                            entry[5] if len(entry) > 5 else "fx"))
        return entries

    return _sampler

@dataclass(frozen=True)
class TimelineClipboardPayload:
    """Typed clipboard payload for timeline clip copy/paste operations.

    Replaces ad-hoc dict construction with a structured, typed container.
    """
    clips: tuple[dict[str, Any], ...] = ()
    anchor_frame: int = 0
    fps: float = 24.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "timeline_clips",
            "fps": self.fps,
            "anchor_frame": self.anchor_frame,
            "clips": list(self.clips),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> TimelineClipboardPayload:
        return TimelineClipboardPayload(
            clips=tuple(data.get("clips", [])),
            anchor_frame=data.get("anchor_frame", 0),
            fps=data.get("fps", 24.0),
        )

    @staticmethod
    def is_clipboard_dict(data: Any) -> bool:
        return isinstance(data, dict) and data.get("type") == "timeline_clips"


def resolve_timeline_in_out(
    range_start: int | None,
    range_end: int | None,
    duration_frames: int,
) -> tuple[int, int] | None:
    """Resolve persisted In/Out points to an inclusive, valid frame range.

    A single endpoint is a complete range for playback and export: an In point
    extends through the final timeline frame, while an Out point starts at
    frame zero. ``None`` means no selection or an invalid/empty timeline.
    """
    if duration_frames <= 0 or (range_start is None and range_end is None):
        return None
    final_frame = duration_frames - 1
    start = (
        min(final_frame, max(0, int(range_start)))
        if range_start is not None
        else 0
    )
    end = (
        min(final_frame, max(0, int(range_end)))
        if range_end is not None
        else final_frame
    )
    if end < start:
        return None
    return (start, end)


# An empty sequence still represents a real deliverable: it is a black canvas
# whose length can be chosen with the timeline range controls.  Keep this
# default in the shared model helpers so the Edit transport, range UI and
# Deliver exporter agree on the same duration without changing the semantic
# ``TimelineSequence.duration_frames`` property (which describes media only).
DEFAULT_EMPTY_TIMELINE_SECONDS: float = 10.0


def effective_timeline_duration_frames(
    timeline: Any,
    *,
    empty_duration_seconds: float = DEFAULT_EMPTY_TIMELINE_SECONDS,
) -> int:
    """Return the usable playback/export duration for a timeline.

    ``TimelineSequence.duration_frames`` intentionally reports only the end
    of actual clips, and is therefore zero for an empty sequence.  The UI and
    exporter need a slightly different concept: a timeline is also a black
    canvas, and persisted In/Out marks may extend past the last clip.  This
    helper folds those extents together and gives a new empty timeline a
    small, deterministic default canvas (10 seconds at its sequence FPS).

    The function is deliberately duck-typed so it can be used by controller
    tests and lightweight timeline-like objects without importing Qt.
    """
    duration = max(0, int(getattr(timeline, "duration_frames", 0) or 0))

    for track_group_name in ("video_tracks", "audio_tracks", "subtitle_tracks"):
        for track in getattr(timeline, track_group_name, ()) or ():
            duration = max(duration, int(getattr(track, "duration_frames", 0) or 0))
            for clip in getattr(track, "clips", ()) or ():
                duration = max(duration, int(getattr(clip, "timeline_end", 0) or 0))
            for subtitle in getattr(track, "subtitle_clips", ()) or ():
                duration = max(duration, int(getattr(subtitle, "timeline_end", 0) or 0))

    # A range is an explicit request for an output canvas.  The end point is
    # inclusive in the model, hence the +1 when converting to a duration.
    for field_name in ("range_start", "range_end"):
        value = getattr(timeline, field_name, None)
        if value is not None:
            duration = max(duration, int(value) + 1)

    fps_value = getattr(timeline, "fps", 24.0) or 24.0
    try:
        fps = float(fps_value.to_float() if hasattr(fps_value, "to_float") else fps_value)
    except (TypeError, ValueError):
        fps = 24.0
    fps = fps if fps > 0 else 24.0
    seconds = max(0.1, float(empty_duration_seconds))
    default_duration = max(1, int(round(fps * seconds)))

    # Do not silently add a ten-second black tail to a short media timeline:
    # exporters must stop at the last real clip unless the user explicitly
    # marked a later range endpoint.  For a genuinely empty sequence, on the
    # other hand, the default black canvas remains available even when an In
    # point was set near its beginning.
    has_media_extent = int(getattr(timeline, "duration_frames", 0) or 0) > 0
    if not has_media_extent:
        for track_group_name in ("video_tracks", "audio_tracks", "subtitle_tracks"):
            for track in getattr(timeline, track_group_name, ()) or ():
                if any(int(getattr(clip, "timeline_end", 0) or 0) > 0
                       for clip in (getattr(track, "clips", ()) or ())):
                    has_media_extent = True
                    break
                if any(int(getattr(subtitle, "timeline_end", 0) or 0) > 0
                       for subtitle in (getattr(track, "subtitle_clips", ()) or ())):
                    has_media_extent = True
                    break
            if has_media_extent:
                break

    if has_media_extent:
        return max(1, duration)
    return max(default_duration, duration)


@dataclass(frozen=True)
class TimelineSequence:
    """时间线序列。包含视频轨、音频轨、字幕轨、标记。"""
    timeline_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    fps: Rational = Rational(24, 1)
    width: int = 1920
    height: int = 1080
    sample_rate: int = 48000
    # Sequence-format values which do not affect frame placement directly.
    format_settings: dict[str, Any] = field(default_factory=dict)
    video_tracks: tuple[EditTrack, ...] = ()
    audio_tracks: tuple[EditTrack, ...] = ()
    # P10.3: 字幕轨（独立元组，渲染在最顶层视频调色之上）
    subtitle_tracks: tuple[EditTrack, ...] = ()
    # P0/P2/P4: caption graphics live beside the timeline tracks so the
    # complete binding graph round-trips with timeline_sequences.json.
    caption_graphic_bindings: tuple[CaptionGraphicBinding, ...] = ()
    caption_track_graphic_styles: tuple[CaptionTrackGraphicStyle, ...] = ()
    caption_graphics_extra: dict[str, Any] = field(default_factory=dict)
    markers: tuple[TimelineMarker, ...] = ()
    # P1 · VCA 组（增益/静音控制层；路由与 Send tap 不变）
    vcas: tuple[VcaGroup, ...] = ()
    # P4: In/Out range for range playback and export
    range_start: int | None = None
    range_end: int | None = None
    # W12: Master 总线音量/静音（持久化到项目文件，重开恢复）
    # master_volume 是线性增益，范围 0.0-2.0（支持 >0dB 正增益，与推子 dB 范围对齐）；
    # AudioMixer 在 mix_samples 末端应用后跑软限幅器保护削波。
    master_volume: float = 1.0   # 0.0-2.0（线性增益）
    master_muted: bool = False
    # §3.1: Master automation（volume/pan/mute 关键帧，与轨级 automation 同结构）。
    # key=参数名 "volume"/"pan"/"mute"，value=KeyframeTrack。
    # 引擎在 apply_master 阶段逐样本 ramp 应用（与轨级 automation 一致）。
    master_automation: dict = field(default_factory=dict)
    master_automation_mode: str = "off"  # off/read/write/touch/latch
    # MX-03: Master 条带处理（EQ/FX 与普通轨同一参数模型）。引擎在 master
    # 音量后、限幅器前应用（TRACK_CHAIN_ORDER 的 master 段）。立体声 Master
    # 不建模 Panner（无意义），能力矩阵见计划 §5 MX-03。
    master_strip_eq: Optional[StripEQParams] = None
    master_effect_chain: Optional[EffectChainParams] = None
    # TAEP P0: 单调递增的乐观锁 revision。每次经统一提交入口（command /
    # EditProjectService）成功落地一次原子变更后递增。它不是序列化 schema 版本，
    # 而是 timeline 内容版本，用于 AI plan 的 base_revision 一致性检查
    # （TAEP §6.2）。直接调用 _rebuild / with_* 不会自动 bump，bump 只发生在
    # 提交边界，避免 preview/模拟产生虚假 revision 噪音。
    revision: int = 0
    # R28/F-R09：未知字段保留（未来版本写入的序列级字段 roundtrip 不丢）
    _extra_fields: Dict[str, Any] = field(
        default_factory=dict, repr=False, compare=False
    )

    _KNOWN_SEQUENCE_KEYS = frozenset({
        "timeline_id", "name", "fps", "width", "height", "sample_rate",
        "format_settings", "video_tracks", "audio_tracks", "subtitle_tracks",
        "caption_graphics", "markers", "range_start", "range_end",
        "master_volume", "master_muted", "master_automation",
        "master_automation_mode", "master_strip_eq", "master_effect_chain",
        "revision", "audio_fx_schema_version",
    })

    def __post_init__(self) -> None:
        if not self.timeline_id:
            raise ValueError("timeline_id cannot be empty")
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"Invalid resolution: {self.width}x{self.height}")
        if self.sample_rate <= 0:
            raise ValueError(f"Invalid sample_rate: {self.sample_rate}")
        # fps can be Rational or int for convenience
        if hasattr(self.fps, 'num'):
            if self.fps.num <= 0 or self.fps.den <= 0:
                raise ValueError(f"Invalid fps: {self.fps}")
        elif isinstance(self.fps, (int, float)):
            if self.fps <= 0:
                raise ValueError(f"Invalid fps: {self.fps}")
            # AUD-19101：int/float fps 归一化为 Rational，模型内部单一类型
            # （to_dict / 逐帧换算只面对 Rational，不再 AttributeError 或
            # hasattr 双类型分支漂移）。float 先放大到 1e6 精度再约分，
            # 29.97 这类非整数帧率不丢精度。
            if isinstance(self.fps, float):
                normalized = Rational(int(round(self.fps * 1_000_000)), 1_000_000)
            else:
                normalized = Rational(int(self.fps), 1)
            object.__setattr__(self, 'fps', normalized)
        for collection_name, tracks, expected_type in (
            ("video_tracks", self.video_tracks, "video"),
            ("audio_tracks", self.audio_tracks, "audio"),
            ("subtitle_tracks", self.subtitle_tracks, "subtitle"),
        ):
            for track in tracks:
                if track.track_type != expected_type:
                    raise ValueError(
                        f"{collection_name} can only contain {expected_type} tracks; "
                        f"track {track.track_id} has type {track.track_type}"
                    )
        # SUB-P0-03: subtitle clip ids must be globally unique across the
        # whole sequence (a cue id is the lookup key for selection, export
        # and undo). EditTrack already enforces per-track uniqueness.
        seen_seq_sub_ids: set[str] = set()
        for sub_track in self.subtitle_tracks:
            for sub in sub_track.subtitle_clips:
                if sub.clip_id in seen_seq_sub_ids:
                    raise ValueError(
                        f"Duplicate subtitle clip_id across sequence: {sub.clip_id}"
                    )
                seen_seq_sub_ids.add(sub.clip_id)
        seen_binding_ids: set[str] = set()
        seen_graphic_clip_ids: set[str] = set()
        seen_caption_ids: set[str] = set()
        for binding in self.caption_graphic_bindings:
            if binding.binding_id in seen_binding_ids:
                raise ValueError(f"Duplicate caption graphic binding: {binding.binding_id}")
            if binding.graphic_clip_id in seen_graphic_clip_ids:
                raise ValueError(f"Duplicate graphic clip binding: {binding.graphic_clip_id}")
            if binding.caption_cue_id is not None and binding.materialization != "detached":
                if binding.caption_cue_id in seen_caption_ids:
                    raise ValueError(
                        f"Multiple active caption graphic bindings for cue: {binding.caption_cue_id}"
                    )
                if self.get_subtitle_clip_by_id(binding.caption_cue_id) is None:
                    raise ValueError(f"Caption binding source cue not found: {binding.caption_cue_id}")
                seen_caption_ids.add(binding.caption_cue_id)
            seen_binding_ids.add(binding.binding_id)
            seen_graphic_clip_ids.add(binding.graphic_clip_id)

    @property
    def duration_frames(self) -> int:
        all_tracks = self.video_tracks + self.audio_tracks + self.subtitle_tracks
        if not all_tracks:
            return 0
        return max(track.duration_frames for track in all_tracks)

    @property
    def all_tracks(self) -> tuple[EditTrack, ...]:
        return self.video_tracks + self.audio_tracks + self.subtitle_tracks

    def get_track_by_id(self, track_id: str) -> Optional[EditTrack]:
        for track in self.all_tracks:
            if track.track_id == track_id:
                return track
        return None

    def get_clip_by_id(self, clip_id: str) -> Optional[ClipInstance]:
        for track in self.all_tracks:
            clip = track.get_clip_by_id(clip_id)
            if clip is not None:
                return clip
        return None

    def get_reciprocal_linked_clip(
        self, clip: ClipInstance | str | None
    ) -> Optional[ClipInstance]:
        """Return a clip only when the link is a valid two-way relationship.

        ``linked_clip_id`` is persisted on both members of an A/V pair.  A
        stale one-way reference must never be treated as an active link by
        editing or UI code, otherwise a deleted/split clip can still appear
        linked while silently failing to move with its former partner.
        """
        if clip is None:
            return None
        source = self.get_clip_by_id(clip) if isinstance(clip, str) else clip
        if source is None or not source.linked_clip_id:
            return None
        linked = self.get_clip_by_id(source.linked_clip_id)
        if linked is None or linked.clip_id == source.clip_id:
            return None
        if linked.linked_clip_id != source.clip_id:
            return None
        source_track = self.get_track_by_id(source.track_id)
        linked_track = self.get_track_by_id(linked.track_id)
        if source_track is None or linked_track is None:
            return None
        if {source_track.track_type, linked_track.track_type} != {"video", "audio"}:
            return None
        return linked

    def repair_linked_clips(self) -> TimelineSequence:
        """Clear invalid persisted A/V links and preserve valid pairs.

        Older projects can contain one-way references produced by splitting
        or deleting a linked clip.  Repairing at the model boundary keeps the
        renderer, UI and command layer from interpreting those IDs
        differently.
        """
        clips = {
            clip.clip_id: clip
            for track in self.video_tracks + self.audio_tracks
            for clip in track.clips
        }
        track_types = {
            track.track_id: track.track_type
            for track in self.video_tracks + self.audio_tracks
        }
        updated_tracks: list[EditTrack] = []
        changed = False
        for track in self.video_tracks + self.audio_tracks:
            updated_clips: list[ClipInstance] = []
            for clip in track.clips:
                linked = clips.get(clip.linked_clip_id) if clip.linked_clip_id else None
                valid = (
                    linked is not None
                    and linked.clip_id != clip.clip_id
                    and linked.linked_clip_id == clip.clip_id
                    and {track.track_type, track_types.get(linked.track_id)} == {"video", "audio"}
                )
                updated = clip if valid or clip.linked_clip_id is None else clip.with_linked_clip_id(None)
                updated_clips.append(updated)
                changed = changed or updated.linked_clip_id != clip.linked_clip_id
            updated_tracks.append(
                track if all(new is old for new, old in zip(updated_clips, track.clips))
                else replace(track, clips=tuple(updated_clips))
            )
        if not changed:
            return self
        video_count = len(self.video_tracks)
        return self._rebuild(
            video_tracks=tuple(updated_tracks[:video_count]),
            audio_tracks=tuple(updated_tracks[video_count:]),
        )

    def get_subtitle_clip_by_id(self, clip_id: str) -> Optional[SubtitleClip]:
        """P10.3: 在所有字幕轨中查找字幕片段。"""
        for track in self.subtitle_tracks:
            clip = track.get_subtitle_clip_by_id(clip_id)
            if clip is not None:
                return clip
        return None

    def find_subtitle_clip_owner_track(self, clip_id: str) -> Optional[EditTrack]:
        """P10.3: 返回包含指定字幕片段的轨道。"""
        for track in self.subtitle_tracks:
            if track.get_subtitle_clip_by_id(clip_id) is not None:
                return track
        return None

    def get_active_subtitle_clips(self, timeline_frame: int) -> tuple[SubtitleClip, ...]:
        """P10.3: 返回在指定帧处于活跃状态的所有字幕片段（按轨道顺序）。

        用于预览渲染和导出烧录：遍历所有字幕轨，收集当前帧命中的字幕。
        """
        result: list[SubtitleClip] = []
        for track in self.subtitle_tracks:
            if not track.is_enabled or not track.is_visible:
                continue
            sub = track.get_subtitle_clip_at(timeline_frame)
            if sub is not None and sub.is_enabled and sub.display_mode in ("native", "both"):
                result.append(sub)
        return tuple(result)

    def get_graphics_track(self) -> Optional[EditTrack]:
        """Return the first compatible video track reserved for caption graphics."""
        return next(
            (track for track in self.video_tracks if track.track_role == "graphics"),
            None,
        )

    def get_caption_graphic_binding(self, cue_id: str) -> CaptionGraphicBinding | None:
        matches = tuple(
            binding for binding in self.caption_graphic_bindings
            if binding.caption_cue_id == cue_id
        )
        return (
            next((binding for binding in matches
                  if binding.materialization != "detached"), None)
            or (matches[0] if matches else None)
        )

    def get_caption_graphic_binding_by_clip(self, clip_id: str) -> CaptionGraphicBinding | None:
        return next(
            (binding for binding in self.caption_graphic_bindings
             if binding.graphic_clip_id == clip_id),
            None,
        )

    def get_caption_graphic_template_registry(self) -> CaptionGraphicTemplateRegistry:
        templates = {
            str(item.get("template_id")): CaptionGraphicTemplate.from_dict(item)
            for item in self.caption_graphics_extra.get("composite_templates", [])
            if isinstance(item, dict) and item.get("template_id")
        }
        return CaptionGraphicTemplateRegistry(templates)

    def with_caption_graphic_template(self, template: CaptionGraphicTemplate) -> TimelineSequence:
        values = [
            item for item in self.caption_graphics_extra.get("composite_templates", [])
            if not isinstance(item, dict) or item.get("template_id") != template.template_id
        ]
        values.append(template.to_dict())
        extra = dict(self.caption_graphics_extra)
        extra["composite_templates"] = values
        return self._rebuild(caption_graphics_extra=extra)

    def with_caption_graphic_binding_added(self, binding: CaptionGraphicBinding) -> TimelineSequence:
        if self.get_caption_graphic_binding_by_clip(binding.graphic_clip_id) is not None:
            raise ValueError(f"Graphic clip already bound: {binding.graphic_clip_id}")
        if binding.caption_cue_id is not None:
            existing_active = next(
                (
                    item for item in self.caption_graphic_bindings
                    if item.caption_cue_id == binding.caption_cue_id
                    and item.materialization != "detached"
                ),
                None,
            )
            if existing_active is not None and binding.materialization != "detached":
                raise ValueError(
                    f"Caption cue already has an active graphic binding: {binding.caption_cue_id}"
                )
        return self._rebuild(caption_graphic_bindings=self.caption_graphic_bindings + (binding,))

    def with_caption_graphic_binding_updated(self, binding: CaptionGraphicBinding) -> TimelineSequence:
        if not any(item.binding_id == binding.binding_id for item in self.caption_graphic_bindings):
            raise ValueError(f"Caption binding not found: {binding.binding_id}")
        return self._rebuild(caption_graphic_bindings=tuple(
            binding if item.binding_id == binding.binding_id else item
            for item in self.caption_graphic_bindings
        ))

    def with_caption_graphic_binding_removed(self, binding_id: str) -> TimelineSequence:
        return self._rebuild(caption_graphic_bindings=tuple(
            item for item in self.caption_graphic_bindings if item.binding_id != binding_id
        ))

    def with_caption_track_graphic_style(self, style: CaptionTrackGraphicStyle) -> TimelineSequence:
        styles = tuple(item for item in self.caption_track_graphic_styles if item.track_id != style.track_id)
        return self._rebuild(caption_track_graphic_styles=styles + (style,))

    def without_caption_track_graphic_style(self, track_id: str) -> TimelineSequence:
        return self._rebuild(caption_track_graphic_styles=tuple(
            item for item in self.caption_track_graphic_styles if item.track_id != track_id
        ))

    def sync_caption_graphic_for_cue(self, cue_id: str) -> TimelineSequence:
        """Synchronize the denormalized graphic clip range from its cue."""
        cue = self.get_subtitle_clip_by_id(cue_id)
        binding = self.get_caption_graphic_binding(cue_id)
        if cue is None or binding is None or not binding.sync_timing:
            return self
        clip = self.get_clip_by_id(binding.graphic_clip_id)
        if clip is None:
            return self
        updated = clip.with_timeline_start(cue.timeline_start).with_timeline_duration(cue.timeline_duration)
        return self.with_clip_updated(updated)

    def _rebuild(self, *,
                 video_tracks: tuple[EditTrack, ...] | None = None,
                 audio_tracks: tuple[EditTrack, ...] | None = None,
                 subtitle_tracks: tuple[EditTrack, ...] | None = None,
                 caption_graphic_bindings: tuple[CaptionGraphicBinding, ...] | None = None,
                 caption_track_graphic_styles: tuple[CaptionTrackGraphicStyle, ...] | None = None,
                 caption_graphics_extra: dict[str, Any] | None = None,
                 markers: tuple[TimelineMarker, ...] | None = None,
                 range_start: int | None = ...,
                 range_end: int | None = ...,
                 name: str | None = None,
                 master_volume: float | None = None,
                 master_muted: bool | None = None,
                 master_automation: dict | None = None,
                 master_automation_mode: str | None = None,
                 # MX-03: None=清除条带，... 哨兵=不传（与 range_start 同语义）
                 master_strip_eq: Any = ...,
                 master_effect_chain: Any = ...) -> TimelineSequence:
        """Internal immutable rebuilder — centralizes field passing.

        保留 revision 不变（bump 只发生在提交边界，见 ``with_revision_bumped``）。
        """
        return TimelineSequence(
            timeline_id=self.timeline_id,
            name=self.name if name is None else name,
            fps=self.fps,
            width=self.width, height=self.height,
            sample_rate=self.sample_rate,
            format_settings=dict(self.format_settings),
            video_tracks=self.video_tracks if video_tracks is None else video_tracks,
            audio_tracks=self.audio_tracks if audio_tracks is None else audio_tracks,
            subtitle_tracks=self.subtitle_tracks if subtitle_tracks is None else subtitle_tracks,
            caption_graphic_bindings=(self.caption_graphic_bindings if caption_graphic_bindings is None else caption_graphic_bindings),
            caption_track_graphic_styles=(self.caption_track_graphic_styles if caption_track_graphic_styles is None else caption_track_graphic_styles),
            caption_graphics_extra=dict(self.caption_graphics_extra if caption_graphics_extra is None else caption_graphics_extra),
            markers=self.markers if markers is None else markers,
            range_start=self.range_start if range_start is ... else range_start,
            range_end=self.range_end if range_end is ... else range_end,
            master_volume=self.master_volume if master_volume is None else master_volume,
            master_muted=self.master_muted if master_muted is None else master_muted,
            # AUD-19104：未显式更新时深拷贝——派生序列快照不再共享同一批
            # master KeyframeTrack 实例（可变），undo 前后快照互不影响。
            master_automation=(
                _deep_copy_automation(self.master_automation)
                if master_automation is None else master_automation
            ),
            master_automation_mode=self.master_automation_mode if master_automation_mode is None else master_automation_mode,
            master_strip_eq=(self.master_strip_eq if master_strip_eq is ... else master_strip_eq),
            master_effect_chain=(self.master_effect_chain if master_effect_chain is ... else master_effect_chain),
            revision=self.revision,
            _extra_fields=dict(self._extra_fields),
        )

    def with_revision_bumped(self) -> TimelineSequence:
        """TAEP P0: 返回 revision+1 的新实例。

        只应在**提交边界**调用（command 成功 apply 之后、写回 controller 之前），
        表示一次确定性的 timeline 内容变更已落地。preview / 模拟 / diff 不调用它，
        因此 proposed timeline 与 base 的 revision 相等，便于乐观锁比较。
        """
        return replace(self, revision=self.revision + 1)

    # ── P1 · VCA 组（增益/静音控制层） ─────────────────────────────

    def with_vca_group(self, group: VcaGroup) -> TimelineSequence:
        """替换/追加一个 VCA 组（按 vca_id 去重；frozen 模型）。"""
        updated = tuple(
            group if existing.vca_id == group.vca_id else existing
            for existing in self.vcas)
        if all(existing.vca_id != group.vca_id for existing in updated):
            updated = updated + (group,)
        return replace(self, vcas=updated)

    def vca_factor_for(self, track_id: str) -> float:
        """成员轨的 VCA 倍率（非成员=1；多组相乘，任一静音=0）。"""
        factor = 1.0
        for group in self.vcas:
            if track_id in group.member_track_ids:
                factor *= group.linear_gain()
        return factor

    def vca_snapshot(self) -> dict:
        """全部 VCA 组的 (gain_db, muted) 快照（按 vca_id）。"""
        return {group.vca_id: (group.gain_db, group.muted)
                for group in self.vcas}

    def apply_vca_snapshot(self, snapshot: dict) -> TimelineSequence:
        """按快照重放全部 VCA 组状态（缺项保持原值）。"""
        updated = tuple(
            group.with_gain_db(snapshot[group.vca_id][0]).with_muted(
                snapshot[group.vca_id][1])
            if group.vca_id in snapshot else group
            for group in self.vcas)
        return replace(self, vcas=updated)

    def with_track_updated(self, track: EditTrack) -> TimelineSequence:
        if track.track_type == "video":
            new_video = tuple(track if t.track_id == track.track_id else t for t in self.video_tracks)
            return self._rebuild(video_tracks=new_video)
        elif track.track_type == "audio":
            new_audio = tuple(track if t.track_id == track.track_id else t for t in self.audio_tracks)
            return self._rebuild(audio_tracks=new_audio)
        else:  # subtitle
            new_sub = tuple(track if t.track_id == track.track_id else t for t in self.subtitle_tracks)
            return self._rebuild(subtitle_tracks=new_sub)

    def with_track_added(self, track: EditTrack) -> TimelineSequence:
        """Add a new track to the timeline."""
        if track.track_type == "video":
            return self._rebuild(video_tracks=self.video_tracks + (track,))
        elif track.track_type == "audio":
            return self._rebuild(audio_tracks=self.audio_tracks + (track,))
        else:  # subtitle
            return self._rebuild(subtitle_tracks=self.subtitle_tracks + (track,))

    def with_track_removed(self, track_id: str) -> TimelineSequence:
        """W27: Remove a track from the timeline by ID."""
        for collection_attr in ("video_tracks", "audio_tracks", "subtitle_tracks"):
            tracks = getattr(self, collection_attr)
            if any(t.track_id == track_id for t in tracks):
                new_tracks = tuple(t for t in tracks if t.track_id != track_id)
                return self._rebuild(**{collection_attr: new_tracks})
        return self

    def with_clip_added(self, track_id: str, clip: ClipInstance) -> TimelineSequence:
        track = self.get_track_by_id(track_id)
        if track is None:
            raise ValueError(f"Track not found: {track_id}")
        clip_with_track = clip.with_track(track_id)
        new_track = track.with_clip_added(clip_with_track)
        return self.with_track_updated(new_track)

    def with_clip_removed(self, clip_id: str) -> TimelineSequence:
        for track in self.all_tracks:
            clip = track.get_clip_by_id(clip_id)
            if clip is not None:
                new_timeline = self
                if clip.linked_clip_id:
                    linked = self.get_clip_by_id(clip.linked_clip_id)
                    if linked is not None and linked.linked_clip_id == clip.clip_id:
                        new_timeline = self.with_clip_updated(
                            linked.with_linked_clip_id(None)
                        )
                        track = new_timeline.get_track_by_id(track.track_id) or track
                new_track = track.with_clip_removed(clip_id)
                return new_timeline.with_track_updated(new_track)
        return self

    def with_asset_clips_removed(
        self,
        asset_ids: Collection[str],
    ) -> TimelineSequence:
        """Remove every timeline clip that references one of ``asset_ids``.

        Media-pool assets are project-level objects, while full timeline
        sequences are kept in the controller cache.  Removing an asset must
        therefore have an explicit sequence-level operation; otherwise the
        renderer is left with a clip whose ``asset_id`` no longer resolves.

        The clip IDs are collected before applying removals so linked-clip
        cleanup and transition cleanup continue to use the normal immutable
        ``with_clip_removed`` path.
        """
        target_asset_ids = {asset_id for asset_id in asset_ids if asset_id}
        if not target_asset_ids:
            return self

        clip_ids = [
            clip.clip_id
            for track in self.all_tracks
            for clip in track.clips
            if getattr(clip, "asset_id", None) in target_asset_ids
        ]
        updated = self
        for clip_id in clip_ids:
            updated = updated.with_clip_removed(clip_id)
        return updated

    def with_clip_updated(
        self,
        clip: ClipInstance,
        *,
        preserve_waveform_display: bool = True,
    ) -> TimelineSequence:
        for track in self.all_tracks:
            existing = track.get_clip_by_id(clip.clip_id)
            if existing is not None:
                # A number of legacy clip-edit paths rebuild ClipInstance
                # manually and therefore omit newer visual-only fields. Keep
                # a user's stereo preference through those edits, while
                # allowing the dedicated display command to explicitly turn
                # it off.
                if (
                    preserve_waveform_display
                    and getattr(existing, "waveform_show_channels", False)
                    and not getattr(clip, "waveform_show_channels", False)
                ):
                    clip = clip.with_waveform_show_channels(True)
                new_track = track.with_clip_updated(clip)
                return self.with_track_updated(new_track)
        raise ValueError(f"Clip not found: {clip.clip_id}")

    # P10.3: 字幕片段的序列级不可变更新方法
    def with_subtitle_clip_added(self, track_id: str, clip: SubtitleClip) -> TimelineSequence:
        track = self.get_track_by_id(track_id)
        if track is None:
            raise ValueError(f"Subtitle track not found: {track_id}")
        if track.track_type != "subtitle":
            raise ValueError(f"Track {track_id} is not a subtitle track")
        clip_with_track = clip.with_track(track_id)
        new_track = track.with_subtitle_clip_added(clip_with_track)
        return self.with_track_updated(new_track)

    def with_subtitle_clip_removed(self, clip_id: str) -> TimelineSequence:
        for track in self.subtitle_tracks:
            if track.get_subtitle_clip_by_id(clip_id) is not None:
                new_track = track.with_subtitle_clip_removed(clip_id)
                return self.with_track_updated(new_track)
        return self

    def with_subtitle_clip_updated(self, clip: SubtitleClip) -> TimelineSequence:
        for track in self.subtitle_tracks:
            existing = track.get_subtitle_clip_by_id(clip.clip_id)
            if existing is not None:
                new_track = track.with_subtitle_clip_updated(clip)
                return self.with_track_updated(new_track)
        raise ValueError(f"Subtitle clip not found: {clip.clip_id}")

    def to_dict(self) -> dict[str, Any]:
        return {"timeline_id": self.timeline_id, "name": self.name,
            "fps": f"{self.fps.num}/{self.fps.den}", "width": self.width, "height": self.height,
            "sample_rate": self.sample_rate, "format_settings": dict(self.format_settings),
            "video_tracks": [t.to_dict() for t in self.video_tracks],
            "audio_tracks": [t.to_dict() for t in self.audio_tracks],
            "subtitle_tracks": [t.to_dict() for t in self.subtitle_tracks],
            "caption_graphics": {
                "caption_graphics_schema_version": 1,
                "caption_graphic_bindings": [b.to_dict() for b in self.caption_graphic_bindings],
                "caption_track_graphic_styles": [s.to_dict() for s in self.caption_track_graphic_styles],
                "composite_templates": [dict(item) for item in self.caption_graphics_extra.get("composite_templates", [])]
                if isinstance(self.caption_graphics_extra.get("composite_templates", []), list) else [],
                **{k: v for k, v in self.caption_graphics_extra.items() if k != "composite_templates"},
            },
            "markers": [m.to_dict() for m in self.markers],
            "vcas": [g.to_dict() for g in self.vcas],
            "range_start": self.range_start, "range_end": self.range_end,
            "master_volume": self.master_volume, "master_muted": self.master_muted,
            "master_automation": {k: v.to_dict() for k, v in self.master_automation.items()} if self.master_automation else {},
            "master_automation_mode": self.master_automation_mode,
            "master_strip_eq": self.master_strip_eq.to_dict() if self.master_strip_eq else None,
            "master_effect_chain": self.master_effect_chain.to_dict() if self.master_effect_chain else None,
            "revision": self.revision,
            # R01：FX 序列化 schema 版本（缺失 = legacy v1；from_dict 不回填改写）
            "audio_fx_schema_version": AUDIO_FX_SCHEMA_VERSION,
            # R28/F-R09：未知字段保留
            **self._extra_fields}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> TimelineSequence:
        fps = Rational.from_string(data.get("fps", "24/1"))
        # §3.1: master automation 反序列化
        master_auto = {}
        for param, track_data in (data.get("master_automation") or {}).items():
            try:
                master_auto[param] = KeyframeTrack.from_dict(track_data)
            except (TypeError, ValueError, KeyError, AttributeError) as exc:
                # AUD-19103：与轨级 automation（AUD-19102）同一修复模式。
                logger.warning(
                    "TimelineSequence.from_dict: 丢弃损坏的 master_automation"
                    " 轨 %r: %s",
                    param, exc,
                )
        graphics_block = migrate_caption_graphics_block(data.get("caption_graphics"))
        # MX-03: Master EQ/FX 反序列化（坏字段降级为 None，不阻断序列加载）
        master_eq = None
        eq_data = data.get("master_strip_eq")
        if isinstance(eq_data, dict) and eq_data:
            try:
                master_eq = StripEQParams.from_dict(eq_data)
            except (TypeError, ValueError, KeyError) as exc:
                logger.warning(
                    "TimelineSequence.from_dict: 丢弃损坏的 master_strip_eq: %s", exc)
        master_chain = None
        chain_data = data.get("master_effect_chain")
        if isinstance(chain_data, dict) and chain_data:
            try:
                master_chain = EffectChainParams.from_dict(chain_data)
            except (TypeError, ValueError, KeyError) as exc:
                logger.warning(
                    "TimelineSequence.from_dict: 丢弃损坏的 master_effect_chain: %s", exc)
        known_graphics = {
            "caption_graphics_schema_version", "caption_graphic_bindings",
            "caption_track_graphic_styles", "composite_templates",
        }
        subtitle_ids = {
            clip.clip_id
            for track in tuple(EditTrack.from_dict(t) for t in data.get("subtitle_tracks", []))
            for clip in track.subtitle_clips
        }
        parsed_bindings: list[CaptionGraphicBinding] = []
        orphaned_bindings: list[dict[str, Any]] = []
        seen_binding_ids: set[str] = set()
        seen_graphic_clip_ids: set[str] = set()
        for raw_binding in graphics_block.get("caption_graphic_bindings", []):
            if not isinstance(raw_binding, dict):
                continue
            try:
                binding = CaptionGraphicBinding.from_dict(raw_binding)
                invalid_source = (
                    binding.caption_cue_id is not None
                    and binding.caption_cue_id not in subtitle_ids
                )
                duplicate = (
                    binding.binding_id in seen_binding_ids
                    or binding.graphic_clip_id in seen_graphic_clip_ids
                )
                if invalid_source or duplicate:
                    orphaned_bindings.append(dict(raw_binding))
                    continue
                parsed_bindings.append(binding)
                seen_binding_ids.add(binding.binding_id)
                seen_graphic_clip_ids.add(binding.graphic_clip_id)
            except (TypeError, ValueError) as exc:
                orphaned = dict(raw_binding)
                orphaned["diagnostic"] = str(exc)
                orphaned_bindings.append(orphaned)
        timeline = TimelineSequence(
            timeline_id=data.get("timeline_id", str(uuid.uuid4())), name=data.get("name", ""),
            fps=fps, width=data.get("width", 1920), height=data.get("height", 1080),
            sample_rate=data.get("sample_rate", 48000),
            format_settings=dict(data.get("format_settings", {})),
            video_tracks=tuple(EditTrack.from_dict(t) for t in data.get("video_tracks", [])),
            audio_tracks=tuple(EditTrack.from_dict(t) for t in data.get("audio_tracks", [])),
            subtitle_tracks=tuple(EditTrack.from_dict(t) for t in data.get("subtitle_tracks", [])),
            caption_graphic_bindings=tuple(parsed_bindings),
            caption_track_graphic_styles=tuple(
                CaptionTrackGraphicStyle.from_dict(item)
                for item in graphics_block.get("caption_track_graphic_styles", [])
                if isinstance(item, dict)
            ),
            caption_graphics_extra={
                **{key: value for key, value in graphics_block.items() if key not in known_graphics},
                "composite_templates": list(graphics_block.get("composite_templates", [])),
                **({"orphaned_caption_graphic_bindings": orphaned_bindings}
                   if orphaned_bindings else {}),
            },
            markers=tuple(TimelineMarker.from_dict(m) for m in data.get("markers", [])),
            vcas=tuple(VcaGroup.from_dict(g) for g in data.get("vcas", [])),
            range_start=data.get("range_start"), range_end=data.get("range_end"),
            master_volume=float(data.get("master_volume", 1.0)),
            master_muted=bool(data.get("master_muted", False)),
            master_automation=master_auto,
            master_automation_mode=str(data.get("master_automation_mode", "off") or "off"),
            master_strip_eq=master_eq,
            master_effect_chain=master_chain,
            revision=int(data.get("revision", 0) or 0),
            # R28：未知键进 extras（caption_graphics 块的内部键除外——
            # 它们已由 migrate_caption_graphics_block 消化）
            **({"_extra_fields": {
                k: v for k, v in data.items()
                if k not in TimelineSequence._KNOWN_SEQUENCE_KEYS
                and k != "caption_graphics"
            }} if any(
                k not in TimelineSequence._KNOWN_SEQUENCE_KEYS
                and k != "caption_graphics"
                for k in data
            ) else {}),
        )
        return timeline.repair_linked_clips()

    def with_master_volume(self, volume: float) -> TimelineSequence:
        """W12: 创建新实例，修改 Master 总线音量（线性增益 0.0-2.0，支持 >0dB 正增益）。"""
        return self._rebuild(master_volume=max(0.0, min(2.0, volume)))

    def with_master_muted(self, muted: bool) -> TimelineSequence:
        """W12: 创建新实例，修改 Master 总线静音状态。"""
        return self._rebuild(master_muted=muted)

    def with_master_automation(self, automation: dict) -> TimelineSequence:
        """§3.1: 创建新实例，设置 Master automation（{param: KeyframeTrack}）。

        AUD-19104：深拷贝传入映射（外层 dict 与内层 KeyframeTrack 一并重建），
        调用方后续对原 dict / 轨实例的原位修改不会穿透本快照。
        """
        return self._rebuild(master_automation=_deep_copy_automation(automation))

    def with_master_automation_mode(self, mode: str) -> TimelineSequence:
        """§3.1: 创建新实例，设置 Master automation 模式（off/read/write/touch/latch）。"""
        return self._rebuild(master_automation_mode=str(mode))

    def with_master_strip_eq(self, params: Optional[StripEQParams]) -> TimelineSequence:
        """MX-03: 创建新实例，设置 Master Strip EQ（None=禁用）。"""
        return self._rebuild(master_strip_eq=params)

    def with_master_effect_chain(self, chain: Optional[EffectChainParams]) -> TimelineSequence:
        """MX-03: 创建新实例，设置 Master FX 机架链（None=清空）。"""
        return self._rebuild(master_effect_chain=chain)

    def with_range(self, range_start: int | None, range_end: int | None) -> TimelineSequence:
        """Create new instance with updated In/Out range for range playback/export."""
        return self._rebuild(range_start=range_start, range_end=range_end)

    def __hash__(self) -> int:
        return hash(self.timeline_id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TimelineSequence):
            return NotImplemented
        return self.timeline_id == other.timeline_id
