"""
Edit Project Data Models Module
编辑项目数据模型（P2）。

## 职责

- MediaAsset: 项目媒体素材
- ProjectBin: 项目素材箱
- TimelineSequenceRef: 时间线引用（简化版）
- EditProject: 编辑项目

## 设计原则

- 不可变数据类（frozen=True）确保线程安全
- 完整类型注解
- 验证逻辑在 __post_init__ 中
- 工厂方法返回新实例（不修改原实例）

## 素材导入流程

1. 用户导入文件 -> EditProject.import_asset()
2. 去重检查（同一路径不重复导入）
3. 探测 metadata：
   - 图片：使用 FileIO.get_metadata()
   - 视频：使用 MediaSourceFactory.probe_video_metadata()
4. 生成 asset_id 和 thumbnail_key
5. 返回新的 EditProject 实例

## Timeline 管理

P2 只支持创建 timeline 引用（TimelineSequenceRef），
完整 TimelineSequence 模型在 P3 实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Optional, ClassVar
import uuid
import time
import os
import logging

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from src.features.edit.core.timeline_v2 import TimelineSequence

from src.shared.extra_fields import preserves_extras

logger = logging.getLogger(__name__)


def _fps_to_float(value: Any, default: float = 24.0) -> float:
    """Normalize timeline/media fps values to a positive float."""
    if value is None:
        return default
    if hasattr(value, "to_fps"):
        value = value.to_fps()
    elif hasattr(value, "to_float"):
        value = value.to_float()
    elif hasattr(value, "num") and hasattr(value, "den"):
        den = getattr(value, "den", 1) or 1
        value = getattr(value, "num", default) / den
    try:
        fps = float(value)
    except (TypeError, ValueError):
        return default
    return fps if fps > 0.0 else default


# ================================================================
# MediaAsset - 媒体素材
# ================================================================

# P1-12 素材日志字段的合法取值
ASSET_FLAG_VALUES: tuple[str, ...] = ("none", "reject")
ASSET_COLOR_LABELS: tuple[str, ...] = (
    "none", "blue", "cyan", "green", "yellow", "orange", "red", "purple", "pink",
)

@dataclass(frozen=True)
@preserves_extras
class MediaAsset:
    """
    项目媒体素材。

    每个导入的素材生成唯一 asset_id，Timeline clip 通过 asset_id 引用，
    不直接依赖文件路径（支持素材重定位、代理、离线管理）。

    Attributes:
        asset_id: 唯一标识符（UUID）
        path: 源媒体文件路径（可变，通过 asset_id 稳定引用）
        media_type: 媒体类型（image, video, audio, subtitle, image_sequence）
        display_name: 显示名称（用户可修改）
        metadata: 缓存元数据（width/height/fps/duration/codec/pixel_format/audio_info）
        thumbnail_key: 缩略图缓存键（指向 thumbnail service）
        proxy_status: 代理状态（none, queued, ready, failed）
        color_metadata: 颜色元数据（色彩空间、白平衡等）
        created_at: 创建时间戳（Unix time）
        reel: 卷号（影视后期制作标识）
        scene: 场景号
        shot: 镜头号
        take: 拍摄次号
    """
    asset_id: str
    path: str
    media_type: str  # image, video, audio, subtitle, image_sequence
    display_name: str
    metadata: dict[str, Any]
    thumbnail_key: str | None = None
    proxy_path: str | None = None  # 代理文件路径（P6.3）
    proxy_status: str = "none"  # none, pending, ready, failed
    is_offline: bool = False  # 是否离线（源文件不存在）
    color_metadata: dict[str, Any] | None = None
    created_at: float = 0.0
    # 影视后期制作元数据
    reel: str = ""
    scene: str = ""
    shot: str = ""
    take: str = ""
    # P1-12 素材日志字段（媒体池专业日志能力，参照 Resolve Media Pool）
    keywords: tuple[str, ...] = ()  # 关键词（可多个，用于过滤与 Smart Bin）
    rating: int = 0  # 评级 0-5（0 = 未评级）
    flag: str = "none"  # 旗标（none / reject）
    color_label: str = "none"  # 颜色标签（见 ASSET_COLOR_LABELS）
    comment: str = ""  # 注释
    # DESK-01B：内嵌资源身份（手机自包含包）。resource_uri 是权威引用
    # （topos-res://…，见 docs/adr/0005 §3）；path_original 保留外部原
    # 路径提示（重链接/溯源用）。加载时外部路径离线 → path 被物化为
    # 解包缓存内的 seekable 文件（本机实现细节，保存侧守卫不落盘）。
    resource_uri: str | None = None
    path_original: str | None = None
    # DESK-04A：未来 schema 未知键保留（load→save 不丢）。本类无类级
    # to_dict/from_dict——序列化经 serialization._asset_to_dict /
    # _dict_to_asset 与 edit_project_io.convert_*，读取侧用 split_extras
    # 捕获、写出侧回写。
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    # 序列化层写出的全部键（serialization._asset_to_dict，含条件写出的
    # resource_uri/path_original）+ 读取侧消费键——两者一致，无 legacy 迁移键
    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "asset_id", "path", "media_type", "display_name", "metadata",
        "thumbnail_key", "proxy_path", "proxy_status", "is_offline",
        "color_metadata", "created_at", "reel", "scene", "shot", "take",
        "keywords", "rating", "flag", "color_label", "comment",
        "resource_uri", "path_original",
    })

    def __post_init__(self) -> None:
        """验证素材数据有效性"""
        if not self.asset_id:
            raise ValueError("asset_id cannot be empty")
        if not isinstance(self.keywords, tuple):
            raise ValueError("keywords must be a tuple of str")
        if any(not isinstance(k, str) for k in self.keywords):
            raise ValueError("keywords must be a tuple of str")
        if self.rating < 0 or self.rating > 5:
            raise ValueError(f"rating must be 0-5: {self.rating}")
        if self.flag not in ASSET_FLAG_VALUES:
            raise ValueError(f"Invalid flag: {self.flag}")
        if self.color_label not in ASSET_COLOR_LABELS:
            raise ValueError(f"Invalid color_label: {self.color_label}")
        # comp_clip / nested_sequence / adjustment / solid / motion_graphic /
        # placeholder（B7 占位符片段）都是无磁盘文件的虚拟/生成器素材，
        # 允许空 path。
        _VIRTUAL_TYPES = (
            "comp_clip", "nested_sequence", "adjustment", "solid", "motion_graphic",
            "placeholder",
        )
        if not self.path and self.media_type not in _VIRTUAL_TYPES:
            raise ValueError("path cannot be empty")
        if self.media_type not in (
            "image", "video", "audio", "subtitle", "image_sequence", "model",
            "comp_clip", "nested_sequence", "adjustment", "solid", "motion_graphic",
            "placeholder",
        ):
            raise ValueError(f"Invalid media_type: {self.media_type}")
        if self.proxy_status not in ("none", "pending", "ready", "failed"):
            raise ValueError(f"Invalid proxy_status: {self.proxy_status}")

    # ==================== 工厂方法 ====================
    # 所有 with_* 使用 dataclasses.replace 派生新实例，避免手动逐字段
    # 复制在新增字段时遗漏（frozen dataclass 的 replace 语义完全等价）。

    def with_display_name(self, name: str) -> MediaAsset:
        """创建新实例，修改显示名称"""
        return replace(self, display_name=name)

    def with_proxy_status(self, status: str) -> MediaAsset:
        """创建新实例，修改代理状态"""
        return replace(self, proxy_status=status)

    def with_proxy_path(self, proxy_path: str | None) -> MediaAsset:
        """创建新实例，修改代理路径"""
        return replace(self, proxy_path=proxy_path)

    def with_offline_status(self, is_offline: bool) -> MediaAsset:
        """创建新实例，修改离线状态"""
        return replace(self, is_offline=is_offline)

    def with_thumbnail_key(self, key: str | None) -> MediaAsset:
        """创建新实例，修改缩略图键"""
        return replace(self, thumbnail_key=key)

    def with_path(self, new_path: str) -> MediaAsset:
        """创建新实例，修改路径（用于素材重定位）"""
        return replace(self, path=new_path)

    def with_production_info(
        self,
        reel: str | None = None,
        scene: str | None = None,
        shot: str | None = None,
        take: str | None = None,
    ) -> MediaAsset:
        """创建新实例，修改影视制作元数据"""
        return replace(
            self,
            reel=reel if reel is not None else self.reel,
            scene=scene if scene is not None else self.scene,
            shot=shot if shot is not None else self.shot,
            take=take if take is not None else self.take,
        )

    def with_log_fields(
        self,
        keywords: tuple[str, ...] | None = None,
        rating: int | None = None,
        flag: str | None = None,
        color_label: str | None = None,
        comment: str | None = None,
    ) -> MediaAsset:
        """创建新实例，修改素材日志字段（P1-12）。

        任何为 None 的参数保持原值不变。
        """
        return replace(
            self,
            keywords=tuple(keywords) if keywords is not None else self.keywords,
            rating=rating if rating is not None else self.rating,
            flag=flag if flag is not None else self.flag,
            color_label=color_label if color_label is not None else self.color_label,
            comment=comment if comment is not None else self.comment,
        )

    @property
    def has_proxy(self) -> bool:
        """是否有可用的代理"""
        return self.proxy_status == "ready" and self.proxy_path is not None

    @property
    def effective_path(self) -> str:
        """获取有效播放路径（优先使用代理，否则使用源文件）"""
        if self.has_proxy and not self.is_offline:
            return self.proxy_path
        return self.path

    # ==================== 计算属性 ====================

    @property
    def duration_seconds(self) -> float | None:
        """获取素材时长（秒），图片返回 None"""
        if self.media_type == "image":
            return None
        return self.metadata.get("duration_seconds")

    @property
    def fps(self) -> float | None:
        """获取帧率，图片返回 None"""
        if self.media_type == "image":
            return None
        return self.metadata.get("fps")

    @property
    def total_frames(self) -> int | None:
        """获取总帧数，图片返回 None"""
        if self.media_type == "image":
            return None
        return self.metadata.get("total_frames")

    @property
    def width(self) -> int | None:
        """获取宽度"""
        return self.metadata.get("width")

    @property
    def height(self) -> int | None:
        """获取高度"""
        return self.metadata.get("height")

    @property
    def resolution_str(self) -> str:
        """获取分辨率字符串"""
        w = self.width
        h = self.height
        if w and h:
            return f"{w}x{h}"
        return "-"

    @property
    def is_video(self) -> bool:
        """是否为视频"""
        return self.media_type == "video"

    @property
    def is_image(self) -> bool:
        """是否为图片"""
        return self.media_type == "image"

    @property
    def is_model3d(self) -> bool:
        """是否为可拖入 Composite 的 3D 模型素材。"""
        return self.media_type == "model"

    @property
    def is_audio(self) -> bool:
        """是否为音频"""
        return self.media_type == "audio"

    @property
    def is_subtitle(self) -> bool:
        """是否为字幕文件素材"""
        return self.media_type == "subtitle"

    @property
    def is_comp_clip(self) -> bool:
        """是否为 Comp Clip"""
        return self.media_type == "comp_clip"

    @property
    def is_motion_graphic(self) -> bool:
        """是否为 Motion Graphic 虚拟素材。"""
        return self.media_type == "motion_graphic"

    @property
    def is_nested_sequence(self) -> bool:
        """是否为嵌套序列（作为 clip 嵌入的子序列）"""
        return self.media_type == "nested_sequence"

    @property
    def is_adjustment(self) -> bool:
        """是否为调节片段（Adjustment Clip，无源画面，把效果链作用于下方轨道）"""
        return self.media_type == "adjustment"

    @property
    def is_generator(self) -> bool:
        """是否为生成器类素材（无源媒体、程序化生成画面）"""
        return self.media_type in ("adjustment", "solid", "motion_graphic")

    @property
    def is_virtual(self) -> bool:
        """Whether the asset is backed by a host-side virtual URI."""
        return self.media_type in (
            "comp_clip", "nested_sequence", "adjustment", "solid", "motion_graphic",
        )

    @property
    def solid_color(self) -> tuple[float, float, float] | None:
        """纯色片段的 RGB（0.0~1.0）。非 solid 类型返回 None。

        存储在 ``metadata["color"]`` 里（与 Solid Color 工厂一致），
        缺失时回退为黑色。
        """
        if self.media_type != "solid":
            return None
        c = self.metadata.get("color", [0.0, 0.0, 0.0])
        if not isinstance(c, (list, tuple)) or len(c) < 3:
            return (0.0, 0.6, 0.8)
        return (float(c[0]), float(c[1]), float(c[2]))

    # Default generator parameters (mirrors Composite ConstantNode defaults).
    # No type annotation — this is a plain class variable, NOT a dataclass field.
    _GENERATOR_DEFAULTS = {
        "mode": "solid",
        "color_r": 0.0, "color_g": 0.6, "color_b": 0.8, "color_a": 1.0,
        "color2_r": 1.0, "color2_g": 1.0, "color2_b": 1.0, "color2_a": 1.0,
        "checker_size": 64,
        "start_x": 0.0, "start_y": 0.5, "end_x": 1.0, "end_y": 0.5,
        "center_x": 0.5, "center_y": 0.5, "radial_radius": 0.5,
        "feather": 0.0,
    }

    @property
    def generator_params(self) -> dict | None:
        """Solid 生成器的完整参数集（模式、颜色、棋盘格/渐变/径向设置）。

        非 solid 类型返回 None。向后兼容：若 metadata 中仅有旧版
        ``color`` 字段，则自动构造 mode="solid" 的完整参数。
        """
        if self.media_type != "solid":
            return None
        params = dict(self._GENERATOR_DEFAULTS)
        # Legacy path: only metadata["color"] exists
        c = self.metadata.get("color")
        if isinstance(c, (list, tuple)) and len(c) >= 3:
            params["color_r"] = float(c[0])
            params["color_g"] = float(c[1])
            params["color_b"] = float(c[2])
        # New path: metadata["generator_params"] stores the full dict
        gp = self.metadata.get("generator_params")
        if isinstance(gp, dict):
            for k, v in gp.items():
                if k in params:
                    params[k] = v
        return params

    @property
    def nested_timeline_id(self) -> str | None:
        """嵌套序列 asset 引用的子序列 timeline_id（仅 nested_sequence 有效）。

        存储约定：asset.path = topos://sequence/<timeline_id>，
        metadata["timeline_id"] 作为冗余/权威来源。
        """
        if self.media_type != "nested_sequence":
            return None
        tid = self.metadata.get("timeline_id")
        if isinstance(tid, str) and tid:
            return tid
        # 从虚拟 URI 回退解析
        if self.path.startswith("topos://sequence/"):
            return self.path[len("topos://sequence/"):]
        return None

    # is_offline 已在 dataclass 字段中定义，不需要 property

    @property
    def codec(self) -> str:
        """获取编码格式"""
        return self.metadata.get("codec_name", "") or self.metadata.get("codec", "")

    @property
    def codec_str(self) -> str:
        """获取编码格式字符串（用于显示）"""
        codec = self.codec
        if codec:
            return codec.upper()
        return "-"

    @property
    def audio_channels(self) -> int | None:
        """获取音频声道数"""
        audio_info = self.metadata.get("audio_info", {})
        if isinstance(audio_info, dict):
            return audio_info.get("channels")
        return None

    @property
    def audio_channel_layout(self) -> str | None:
        """§4.1: 获取音频声道布局（'mono'/'stereo'/'5.1'/'7.1'）。"""
        audio_info = self.metadata.get("audio_info", {})
        if isinstance(audio_info, dict):
            return audio_info.get("channel_layout")
        return None

    @property
    def audio_channels_str(self) -> str:
        """获取音频声道字符串"""
        channels = self.audio_channels
        if channels:
            return f"{channels}ch"
        return "-"

    @property
    def production_info_str(self) -> str:
        """获取影视制作信息字符串（Scene/Shot/Take）"""
        parts = []
        if self.scene:
            parts.append(f"S{self.scene}")
        if self.shot:
            parts.append(f"Sh{self.shot}")
        if self.take:
            parts.append(f"T{self.take}")
        return "/".join(parts) if parts else "-"

    def __hash__(self) -> int:
        """哈希值，基于 asset_id"""
        return hash(self.asset_id)

    def __eq__(self, other: object) -> bool:
        """相等比较，基于 asset_id"""
        if not isinstance(other, MediaAsset):
            return NotImplemented
        return self.asset_id == other.asset_id

    def __repr__(self) -> str:
        """详细表示"""
        return (
            f"MediaAsset(asset_id={self.asset_id!r}, "
            f"path={self.path!r}, "
            f"media_type={self.media_type!r}, "
            f"display_name={self.display_name!r})"
        )


# ================================================================
# ProjectBin - 项目素材箱
# ================================================================

@dataclass(frozen=True)
@preserves_extras
class ProjectBin:
    """
    项目素材箱（文件夹）。

    支持层级结构，每个 bin 可以包含素材和其他 timeline 引用。
    第一版先实现一级 bin，数据模型预留层级扩展。

    Attributes:
        bin_id: 唯一标识符（UUID）
        name: 素材箱名称
        parent_id: 父素材箱 ID（None 表示根级）
        asset_ids: 包含的素材 ID 元组
        timeline_ids: 包含的时间线 ID 元组
    """
    bin_id: str
    name: str
    parent_id: str | None
    asset_ids: tuple[str, ...] = ()
    timeline_ids: tuple[str, ...] = ()
    # DESK-04A：未来 schema 未知键保留（load→save 不丢；经序列化层往返）
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    # serialization._bin_to_dict 写出 / _dict_to_bin 消费的全部键
    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "bin_id", "name", "parent_id", "asset_ids", "timeline_ids",
    })

    def __post_init__(self) -> None:
        """验证素材箱数据有效性"""
        if not self.bin_id:
            raise ValueError("bin_id cannot be empty")
        if not self.name:
            raise ValueError("name cannot be empty")

    # ==================== 工厂方法 ====================

    def with_name(self, name: str) -> ProjectBin:
        """创建新实例，修改名称"""
        return ProjectBin(
            bin_id=self.bin_id,
            name=name,
            parent_id=self.parent_id,
            asset_ids=self.asset_ids,
            timeline_ids=self.timeline_ids,
        )

    def with_asset_added(self, asset_id: str) -> ProjectBin:
        """创建新实例，添加素材"""
        if asset_id in self.asset_ids:
            return self  # 已存在，不重复添加
        return ProjectBin(
            bin_id=self.bin_id,
            name=self.name,
            parent_id=self.parent_id,
            asset_ids=self.asset_ids + (asset_id,),
            timeline_ids=self.timeline_ids,
        )

    def with_asset_removed(self, asset_id: str) -> ProjectBin:
        """创建新实例，移除素材"""
        new_ids = tuple(id for id in self.asset_ids if id != asset_id)
        return ProjectBin(
            bin_id=self.bin_id,
            name=self.name,
            parent_id=self.parent_id,
            asset_ids=new_ids,
            timeline_ids=self.timeline_ids,
        )

    def with_timeline_added(self, timeline_id: str) -> ProjectBin:
        """创建新实例，添加时间线引用"""
        if timeline_id in self.timeline_ids:
            return self
        return ProjectBin(
            bin_id=self.bin_id,
            name=self.name,
            parent_id=self.parent_id,
            asset_ids=self.asset_ids,
            timeline_ids=self.timeline_ids + (timeline_id,),
        )

    def with_timeline_removed(self, timeline_id: str) -> ProjectBin:
        """创建新实例，移除时间线引用"""
        new_ids = tuple(id for id in self.timeline_ids if id != timeline_id)
        return ProjectBin(
            bin_id=self.bin_id,
            name=self.name,
            parent_id=self.parent_id,
            asset_ids=self.asset_ids,
            timeline_ids=new_ids,
        )

    def with_parent(self, parent_id: str) -> ProjectBin:
        """创建新实例，修改父 bin（用于拖拽改变层级）"""
        return ProjectBin(
            bin_id=self.bin_id,
            name=self.name,
            parent_id=parent_id,
            asset_ids=self.asset_ids,
            timeline_ids=self.timeline_ids,
        )

    def __hash__(self) -> int:
        """哈希值，基于 bin_id"""
        return hash(self.bin_id)

    def __eq__(self, other: object) -> bool:
        """相等比较，基于 bin_id"""
        if not isinstance(other, ProjectBin):
            return NotImplemented
        return self.bin_id == other.bin_id

    def __repr__(self) -> str:
        """详细表示"""
        return (
            f"ProjectBin(bin_id={self.bin_id!r}, "
            f"name={self.name!r}, "
            f"asset_count={len(self.asset_ids)}, "
            f"timeline_count={len(self.timeline_ids)})"
        )


# ================================================================
# SmartBin - 智能素材箱（自动分类）
# ================================================================

# SmartBin 预定义类型常量
SMART_BIN_ALL_VIDEO = "all_video"
SMART_BIN_ALL_AUDIO = "all_audio"
SMART_BIN_ALL_STILLS = "all_stills"
SMART_BIN_OFFLINE_MEDIA = "offline_media"
SMART_BIN_RECENT_IMPORTS = "recent_imports"


@dataclass(frozen=True)
@preserves_extras
class SmartBin:
    """
    智能素材箱（自动分类）。

    Smart Bin 根据预定义的过滤条件自动归类素材，不需要用户手动管理。
    当项目素材变化时，Smart Bin 自动更新。

    预定义 Smart Bins:
    - All Video: 所有视频素材
    - All Audio: 所有音频素材
    - All Stills: 所有图片素材
    - Offline Media: 离线素材（文件不存在）
    - Recent Imports: 最近导入的素材

    Attributes:
        bin_id: 唯一标识符（使用预定义常量）
        name: 显示名称
        filter_type: 过滤类型（media_type, offline, recent）
        filter_params: 过滤参数字典
    """
    bin_id: str
    name: str
    filter_type: str  # media_type, offline, recent, custom
    filter_params: dict[str, Any] = field(default_factory=dict)
    # DESK-04A：未来 schema 未知键保留（load→save 不丢；经序列化层往返）
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    # serialization._smart_bin_to_dict 写出 / _dict_to_smart_bin 消费的全部键
    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "bin_id", "name", "filter_type", "filter_params",
    })

    def __post_init__(self) -> None:
        """验证智能素材箱数据有效性"""
        if not self.bin_id:
            raise ValueError("bin_id cannot be empty")
        if not self.name:
            raise ValueError("name cannot be empty")
        if self.filter_type not in ("media_type", "offline", "recent", "custom"):
            raise ValueError(f"Invalid filter_type: {self.filter_type}")

    # ==================== 工厂方法 ====================

    @staticmethod
    def create_all_video() -> SmartBin:
        """创建 All Video Smart Bin"""
        return SmartBin(
            bin_id=SMART_BIN_ALL_VIDEO,
            name="All Video",
            filter_type="media_type",
            filter_params={"media_type": "video"},
        )

    @staticmethod
    def create_all_audio() -> SmartBin:
        """创建 All Audio Smart Bin"""
        return SmartBin(
            bin_id=SMART_BIN_ALL_AUDIO,
            name="All Audio",
            filter_type="media_type",
            filter_params={"media_type": "audio"},
        )

    @staticmethod
    def create_all_stills() -> SmartBin:
        """创建 All Stills Smart Bin"""
        return SmartBin(
            bin_id=SMART_BIN_ALL_STILLS,
            name="All Stills",
            filter_type="media_type",
            filter_params={"media_type": "image"},
        )

    @staticmethod
    def create_offline_media() -> SmartBin:
        """创建 Offline Media Smart Bin"""
        return SmartBin(
            bin_id=SMART_BIN_OFFLINE_MEDIA,
            name="Offline Media",
            filter_type="offline",
            filter_params={},
        )

    @staticmethod
    def create_offline() -> SmartBin:
        """创建 Offline Media Smart Bin（别名，兼容 create_offline_media）"""
        return SmartBin.create_offline_media()

    @staticmethod
    def create_recent_imports(hours: int = 24) -> SmartBin:
        """创建 Recent Imports Smart Bin"""
        return SmartBin(
            bin_id=SMART_BIN_RECENT_IMPORTS,
            name="Recent Imports",
            filter_type="recent",
            filter_params={"hours": hours},
        )

    # ==================== 过滤方法 ====================

    def get_assets(self, project: EditProject) -> list[MediaAsset]:
        """
        根据过滤条件获取匹配的素材。

        Args:
            project: EditProject 实例

        Returns:
            匹配的素材列表
        """
        assets: list[MediaAsset] = []

        if self.filter_type == "media_type":
            target_type = self.filter_params.get("media_type", "")
            for asset in project.assets.values():
                if asset.media_type == target_type:
                    assets.append(asset)

        elif self.filter_type == "offline":
            # 优先使用 is_offline 字段，回退到文件系统检查
            for asset in project.assets.values():
                if asset.is_virtual:
                    continue  # Virtual assets don't have real file paths
                if asset.is_offline or not os.path.exists(asset.path):
                    assets.append(asset)

        elif self.filter_type == "recent":
            hours = self.filter_params.get("hours", 24)
            cutoff_time = time.time() - hours * 3600
            for asset in project.assets.values():
                if asset.created_at >= cutoff_time:
                    assets.append(asset)

        elif self.filter_type == "custom":
            # 自定义过滤条件
            for asset in project.assets.values():
                if self._matches_custom_filter(asset):
                    assets.append(asset)

        return assets

    def _matches_custom_filter(self, asset: MediaAsset) -> bool:
        """检查素材是否匹配自定义过滤条件。

        支持的条件（P1-12 扩展）：
        - media_type / min_width / max_width / min_fps / max_fps / codec（原有）
        - keywords: list[str]，素材关键词与列表任一匹配即命中；
          require_all_keywords=True 时要求全部包含
        - min_rating: 素材评级 >= 该值
        - flag: 精确匹配（如 "reject"）
        - color_label: 精确匹配颜色标签
        - comment_contains: 注释子串匹配（大小写不敏感）
        - offline: True/False 精确匹配离线状态
        - proxy_status: 精确匹配代理状态
        """
        # 检查媒体类型
        if "media_type" in self.filter_params:
            if asset.media_type != self.filter_params["media_type"]:
                return False

        # 检查分辨率范围
        if "min_width" in self.filter_params:
            if asset.width and asset.width < self.filter_params["min_width"]:
                return False
        if "max_width" in self.filter_params:
            if asset.width and asset.width > self.filter_params["max_width"]:
                return False

        # 检查帧率范围
        if "min_fps" in self.filter_params:
            if asset.fps and asset.fps < self.filter_params["min_fps"]:
                return False
        if "max_fps" in self.filter_params:
            if asset.fps and asset.fps > self.filter_params["max_fps"]:
                return False

        # 检查编码格式
        if "codec" in self.filter_params:
            codec = asset.metadata.get("codec", "")
            if codec.lower() != self.filter_params["codec"].lower():
                return False

        # P1-12：关键词（任一命中；require_all_keywords=True 时全部包含）
        keywords = self.filter_params.get("keywords")
        if keywords:
            require_all = bool(self.filter_params.get("require_all_keywords", False))
            if require_all:
                if not set(keywords).issubset(asset.keywords):
                    return False
            elif not set(keywords) & set(asset.keywords):
                return False

        # P1-12：最低评级
        min_rating = self.filter_params.get("min_rating")
        if isinstance(min_rating, int) and asset.rating < min_rating:
            return False

        # P1-12：旗标 / 颜色标签精确匹配
        flag = self.filter_params.get("flag")
        if isinstance(flag, str) and asset.flag != flag:
            return False
        color_label = self.filter_params.get("color_label")
        if isinstance(color_label, str) and asset.color_label != color_label:
            return False

        # P1-12：注释子串
        comment_contains = self.filter_params.get("comment_contains")
        if isinstance(comment_contains, str) and comment_contains:
            if comment_contains.lower() not in asset.comment.lower():
                return False

        # P1-12：离线状态 / 代理状态精确匹配
        offline = self.filter_params.get("offline")
        if isinstance(offline, bool) and asset.is_offline != offline:
            return False
        proxy_status = self.filter_params.get("proxy_status")
        if isinstance(proxy_status, str) and asset.proxy_status != proxy_status:
            return False

        return True

    def __hash__(self) -> int:
        """哈希值，基于 bin_id"""
        return hash(self.bin_id)

    def __eq__(self, other: object) -> bool:
        """相等比较，基于 bin_id"""
        if not isinstance(other, SmartBin):
            return NotImplemented
        return self.bin_id == other.bin_id

    def __repr__(self) -> str:
        """详细表示"""
        return (
            f"SmartBin(bin_id={self.bin_id!r}, "
            f"name={self.name!r}, "
            f"filter_type={self.filter_type!r})"
        )


# ================================================================
# TimelineSequenceRef - 时间线引用（P2 简化版）
# ================================================================

@dataclass(frozen=True)
@preserves_extras
class TimelineSequenceRef:
    """
    时间线引用（简化版）。

    P2 只支持创建 timeline 引用，完整 TimelineSequence 模型在 P3 实现。
    此引用包含基本设置信息，便于项目级管理。

    Attributes:
        timeline_id: 唯一标识符（UUID）
        name: 时间线名称
        fps: 帧率（float，如 24.0, 23.976）
        width: 视频宽度
        height: 视频高度
        sample_rate: 音频采样率（默认 48000）
        format_settings: 其余序列格式设置（像素宽高比、场序、显示格式等）
        created_at: 创建时间戳
    """
    timeline_id: str
    name: str
    fps: float = 24.0
    width: int = 1920
    height: int = 1080
    sample_rate: int = 48000
    format_settings: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    # DESK-04A：未来 schema 未知键保留（load→save 不丢；经序列化层往返）
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    # serialization._timeline_ref_to_dict 写出 / _dict_to_timeline_ref 消费的全部键
    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "timeline_id", "name", "fps", "width", "height",
        "sample_rate", "format_settings", "created_at",
    })

    def __post_init__(self) -> None:
        """验证时间线引用数据有效性"""
        if not self.timeline_id:
            raise ValueError("timeline_id cannot be empty")
        if not self.name:
            raise ValueError("name cannot be empty")
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"Invalid resolution: {self.width}x{self.height}")
        if self.fps <= 0:
            raise ValueError(f"Invalid fps: {self.fps}")
        if self.sample_rate <= 0:
            raise ValueError(f"Invalid sample_rate: {self.sample_rate}")

    # ==================== 工厂方法 ====================

    def with_name(self, name: str) -> TimelineSequenceRef:
        """创建新实例，修改名称"""
        return TimelineSequenceRef(
            timeline_id=self.timeline_id,
            name=name,
            fps=self.fps,
            width=self.width,
            height=self.height,
            sample_rate=self.sample_rate,
            format_settings=dict(self.format_settings),
            created_at=self.created_at,
        )

    def with_resolution(self, width: int, height: int) -> TimelineSequenceRef:
        """创建新实例，修改分辨率"""
        return TimelineSequenceRef(
            timeline_id=self.timeline_id,
            name=self.name,
            fps=self.fps,
            width=width,
            height=height,
            sample_rate=self.sample_rate,
            format_settings=dict(self.format_settings),
            created_at=self.created_at,
        )

    def with_fps(self, fps: float) -> TimelineSequenceRef:
        """创建新实例，修改帧率"""
        return TimelineSequenceRef(
            timeline_id=self.timeline_id,
            name=self.name,
            fps=fps,
            width=self.width,
            height=self.height,
            sample_rate=self.sample_rate,
            format_settings=dict(self.format_settings),
            created_at=self.created_at,
        )

    @property
    def resolution_str(self) -> str:
        """分辨率字符串"""
        return f"{self.width}x{self.height}"

    def __hash__(self) -> int:
        """哈希值，基于 timeline_id"""
        return hash(self.timeline_id)

    def __eq__(self, other: object) -> bool:
        """相等比较，基于 timeline_id"""
        if not isinstance(other, TimelineSequenceRef):
            return NotImplemented
        return self.timeline_id == other.timeline_id

    def __repr__(self) -> str:
        """详细表示"""
        return (
            f"TimelineSequenceRef(timeline_id={self.timeline_id!r}, "
            f"name={self.name!r}, "
            f"fps={self.fps}, "
            f"resolution={self.resolution_str})"
        )


# ================================================================
# ColorStackData - 调色栈数据 (P9.1)
# ================================================================
# P0-COLOR-03 契约统一：与 shared/project/color_stack.py 的重复定义已合并，
# 此处 re-export 保持既有导入路径（from ...edit.core.project import
# ColorStackData）不变。schema v2 起调色栈携带完整 Layer 快照，
# 字段与版本策略见 shared 定义处的契约说明。
from src.shared.project.color_stack import ColorStackData  # noqa: F401

# ================================================================
# EditProject - 编辑项目
# ================================================================

@dataclass(frozen=True)
class EditProject:
    """
    编辑项目。

    管理项目级素材池、素材箱、时间线引用、合成图。

    Attributes:
        project_id: 项目唯一标识符
        name: 项目名称
        assets: 素材字典 {asset_id: MediaAsset}
        bins: 素材箱字典 {bin_id: ProjectBin}
        timelines: 时间线字典 {timeline_id: TimelineSequenceRef}
        compositions: 合成图字典 {composite_id: CompositeGraphData} (P9.2)
        root_bin_id: 根素材箱 ID
        active_timeline_id: 当前活动时间线 ID
        created_at: 创建时间戳
        modified_at: 最后修改时间戳

    约定：
    - 至少有一个 root bin，用于存放未分类素材
    - 新导入的素材默认放入 root bin
    - 可以创建多个 bin 组织素材
    - 同一素材可以出现在多个 bin（引用而非复制）
    """
    project_id: str
    name: str
    assets: dict[str, MediaAsset] = field(default_factory=dict)
    bins: dict[str, ProjectBin] = field(default_factory=dict)
    timelines: dict[str, TimelineSequenceRef] = field(default_factory=dict)
    compositions: dict[str, Any] = field(default_factory=dict)  # P9.2: {composite_id: CompositeGraphData}
    color_stacks: dict[str, ColorStackData] = field(default_factory=dict)  # P9.1: {stack_id: ColorStackData}
    comp_clips: dict[str, Any] = field(default_factory=dict)  # {comp_clip_id: CompClipData.to_dict()}
    # P1-12: 用户自建 Smart Bin（已保存搜索），重开工程后保持稳定。
    # 内置 Smart Bin（All Video 等）不存这里，由 get_default_smart_bins() 动态给出。
    smart_bins: dict[str, SmartBin] = field(default_factory=dict)
    render_cache_manifests: dict[str, Any] = field(default_factory=dict)  # {cache_id: RenderCacheManifest.to_dict()}
    render_cache_settings: Any = None  # RenderCacheSettings (None = defaults)
    # P1-14: 项目级代理策略（ProxyPolicySettings，None = 默认策略）
    proxy_policy: Any = None
    # F2.7: 素材级解释覆盖（{asset_id: InterpretationOverride}，None 字段
    # = 按探测；fps 覆盖不改写原始探测 metadata）
    interpretation_overrides: dict[str, Any] = field(default_factory=dict)
    # F2.10: 外录音频源级绑定（{video_asset_id: AudioSyncBinding}）
    audio_sync_bindings: dict[str, Any] = field(default_factory=dict)
    # M03.4: 同步诊断持久化（{asset_key: SyncDiagnosticRecord dict}）——
    # 置信度/歧义/拒绝原因随工程往返（拒绝的同步也留存，审计事实）。
    sync_diagnostics: dict[str, Any] = field(default_factory=dict)
    # F2.1: 素材级 Source Patching 覆盖（{asset_id: SourcePatchSpec}）——
    # 源 V/A 流开关与源声道→目标轨映射；无覆盖 = 双流全开直通（现状）。
    source_patch_overrides: dict[str, Any] = field(default_factory=dict)
    # F4.1: 场景/机位调色组（{group_id: ColorGroupData}）——组前/组后
    # 处理栈 + 跨时间线成员引用；组表整体经 SetColorGroupsCommand
    # 受限事务写回（冲突拒绝不半写）。旧工程无此键 = 空组表。
    color_groups: dict[str, Any] = field(default_factory=dict)
    # F4.2: 栈级命名版本与共享锁（{stack_id: {"active","locked",
    # "versions"}}；命令见 core/color_versions.py）。旧工程无键=空表。
    color_stack_versions: dict[str, Any] = field(default_factory=dict)
    # F8.1: 工程级命名快照（{name: {"meta", "payload"}}；命令见
    # core/project_snapshots.py）。旧工程无键 = 空表。
    project_snapshots: dict[str, Any] = field(default_factory=dict)
    root_bin_id: str = ""
    active_timeline_id: str | None = None
    active_comp_clip_id: str | None = None
    created_at: float = 0.0
    modified_at: float = 0.0
    # M14.3b: 工程级采样率（F3.3 采样域参考率；0/缺省 = 48000 由
    # project_sample_rate.project_sample_rate() 访问器解析——字段本身
    # 保持可选，旧工程无键即默认，不猜）
    project_sample_rate: int = 0
    # TAEP P0: 项目级单调递增 revision（乐观锁）。每次经统一提交入口
    # （EditProjectService）成功落地一次原子 timeline 变更后递增。它不等于
    # .topos schema 版本，而是“工程逻辑版本”，用于 AI plan 的 base_revision
    # 一致性比较（TAEP §6.2）。手动修改、脚本、AI 提交、XML 导入都必须走
    # 同一入口才能保证 revision 正确。
    revision: int = 0

    def __post_init__(self) -> None:
        """验证项目数据有效性"""
        if not self.project_id:
            raise ValueError("project_id cannot be empty")
        if not self.name:
            raise ValueError("name cannot be empty")

        # 兼容旧 schema：如果有素材但没有 root_bin_id（旧版本工程文件
        # 序列化时没存 root_bin_id），自动补建一个 Root bin 并把所有现
        # 有 asset 挂进去，而不是只打 warning 让数据处于不一致状态。
        # EditProject 是 frozen dataclass，在 __post_init__ 里改字段必须
        # 用 object.__setattr__ 绕过冻结检查。
        if self.assets and not self.root_bin_id:
            root_bin_id = str(uuid.uuid4())
            new_bins = dict(self.bins)
            new_bins[root_bin_id] = ProjectBin(
                bin_id=root_bin_id,
                name="Root",
                parent_id=None,
                asset_ids=tuple(self.assets.keys()),
            )
            object.__setattr__(self, "bins", new_bins)
            object.__setattr__(self, "root_bin_id", root_bin_id)
            logger.info(
                "EditProject had assets but no root_bin_id — auto-created "
                "Root bin %s with %d asset(s)",
                root_bin_id,
                len(self.assets),
            )

    # ==================== 静态工厂方法 ====================

    @staticmethod
    def create(name: str = "Untitled Project") -> EditProject:
        """
        创建新的编辑项目。

        自动创建 root bin 用于存放未分类素材。

        Args:
            name: 项目名称

        Returns:
            新的 EditProject 实例
        """
        now = time.time()
        project_id = str(uuid.uuid4())
        root_bin_id = str(uuid.uuid4())
        root_bin = ProjectBin(
            bin_id=root_bin_id,
            name="Root",
            parent_id=None,
        )
        return EditProject(
            project_id=project_id,
            name=name,
            assets={},
            bins={root_bin_id: root_bin},
            timelines={},
            compositions={},
            color_stacks={},
            comp_clips={},
            render_cache_manifests={},
            render_cache_settings=None,
            proxy_policy=None,
            root_bin_id=root_bin_id,
            active_timeline_id=None,
            created_at=now,
            modified_at=now,
        )

    @staticmethod
    def create_image_project(
        name: str,
        image_path: str,
        metadata: dict[str, Any] | None = None,
    ) -> EditProject:
        """Create the canonical single-image form of an EditProject.

        Image work is not a second document type.  It is an EditProject with
        one image asset, no timelines, and one asset-owned color stack.  The
        same object can therefore move from Image mode into a timeline or
        Composite without copying state between project systems.
        """
        project = EditProject.create(name=name).import_asset(
            path=image_path,
            media_type="image",
            metadata=metadata or {},
            display_name=os.path.basename(image_path),
        )
        asset = project.get_asset_by_path(image_path)
        if asset is None:
            raise RuntimeError(f"Failed to add image asset: {image_path}")
        stack = ColorStackData.create_standalone_image(asset.asset_id)
        return replace(
            project,
            color_stacks={stack.stack_id: stack},
            modified_at=time.time(),
        )

    @staticmethod
    def create_quick_project(
        name: str,
        media_path: str,
        fps: float = 24.0,
        width: int = 1920,
        height: int = 1080,
        timeline_name: str = "Main Timeline",
    ) -> tuple[EditProject, str, str]:
        """
        快速创建项目（从单个媒体文件）。

        创建包含单个素材和默认时间线的项目，适用于：
        - Library 双击 MOV 快速开始编辑
        - 从单个视频/图片快速创建剪辑项目

        Args:
            name: 项目名称
            media_path: 媒体文件路径
            fps: 帧率（默认 24.0）
            width: 视频宽度（默认 1920）
            height: 视频高度（默认 1080）
            timeline_name: 时间线名称（默认 "Main Timeline")

        Returns:
            Tuple of (EditProject 实例, asset_id, timeline_id)

        Raises:
            ValueError: 如果媒体路径无效
        """
        # 判断媒体类型
        from src.shared.media.format_registry import VIDEO_EXTENSIONS, IMAGE_EXTENSIONS
        video_extensions = tuple(VIDEO_EXTENSIONS)
        image_extensions = tuple(IMAGE_EXTENSIONS)

        media_type = "video"
        if media_path.lower().endswith(video_extensions):
            media_type = "video"
        elif media_path.lower().endswith(image_extensions):
            media_type = "image"
        else:
            media_type = "video"  # 默认

        # 探测媒体元数据
        metadata: dict[str, Any] = {}
        if media_type == "video":
            try:
                from src.shared.media.factory import MediaSourceFactory
                video_meta = MediaSourceFactory.probe_video_metadata(media_path)

                if video_meta:
                    video_fps = float(video_meta.fps)
                    pixel_format = getattr(video_meta, "pixel_format", None)
                    if not isinstance(pixel_format, str):
                        pixel_format = getattr(video_meta, "pix_fmt", "")
                    if not isinstance(pixel_format, str):
                        pixel_format = ""
                    metadata = {
                        "width": video_meta.width,
                        "height": video_meta.height,
                        "fps": video_fps,
                        "duration_seconds": video_meta.duration_seconds,
                        "total_frames": video_meta.total_frames,
                        "codec_name": video_meta.codec_name or "",
                        "codec": video_meta.codec_name or "",
                        "codec_profile": getattr(video_meta, "codec_profile", None),
                        "pixel_format": pixel_format,
                    }

                    # 使用视频的实际分辨率和帧率（如果未指定）
                    if width == 1920 and height == 1080:
                        width = video_meta.width
                        height = video_meta.height
                    if fps == 24.0:
                        fps = video_fps

                    logger.debug(
                        f"Quick project using video metadata: {width}x{height}, {fps}fps"
                    )
            except Exception as e:
                logger.warning(f"Failed to probe video metadata: {e}")
                metadata = {"width": width, "height": height, "fps": fps}
        else:
            # 图片元数据
            try:
                from src.shared.io.file_io import FileIO
                file_meta = FileIO.get_metadata(media_path)

                if file_meta:
                    metadata = {
                        "width": file_meta.get("width", width),
                        "height": file_meta.get("height", height),
                    }

                    # 使用图片的实际分辨率
                    if width == 1920 and height == 1080:
                        width = file_meta.get("width", width)
                        height = file_meta.get("height", height)

                    logger.debug(f"Quick project using image metadata: {width}x{height}")
            except Exception as e:
                logger.warning(f"Failed to probe image metadata: {e}")
                metadata = {"width": width, "height": height}

        # 创建项目
        project = EditProject.create(name)

        # 导入素材
        display_name = os.path.basename(media_path)
        project = project.import_asset(
            path=media_path,
            media_type=media_type,
            metadata=metadata,
            display_name=display_name,
        )

        # 获取导入的 asset_id
        asset_id = None
        for aid, asset in project.assets.items():
            if asset.path == media_path:
                asset_id = aid
                break

        if asset_id is None:
            raise ValueError(f"Failed to import asset: {media_path}")

        # 创建默认时间线
        project = project.create_timeline(
            name=timeline_name,
            fps=fps,
            width=width,
            height=height,
        )

        timeline_id = project.active_timeline_id

        logger.info(
            f"Quick project created: {name} from {media_path}, "
            f"asset_id={asset_id}, timeline_id={timeline_id}"
        )

        return project, asset_id, timeline_id

    # ==================== 素材管理 ====================

    def get_asset_by_id(self, asset_id: str) -> MediaAsset | None:
        """根据 ID 获取素材"""
        return self.assets.get(asset_id)

    def get_asset_by_path(self, path: str) -> MediaAsset | None:
        """根据路径获取素材（用于去重检查）"""
        for asset in self.assets.values():
            if asset.path == path:
                return asset
        return None

    def has_asset_path(self, path: str) -> bool:
        """检查路径是否已导入（用于去重）"""
        return self.get_asset_by_path(path) is not None

    def import_asset(
        self,
        path: str,
        media_type: str,
        metadata: dict[str, Any],
        display_name: str | None = None,
        thumbnail_key: str | None = None,
        color_metadata: dict[str, Any] | None = None,
    ) -> EditProject:
        """
        导入素材到项目。

        去重检查：同一路径不重复导入。

        Args:
            path: 源媒体文件路径
            media_type: 媒体类型（image, video, audio, image_sequence）
            metadata: 元数据字典
            display_name: 显示名称（默认使用文件名）
            thumbnail_key: 缩略图缓存键
            color_metadata: 颜色元数据

        Returns:
            新的 EditProject 实例（包含新素材）

        Raises:
            ValueError: 如果素材已存在或参数无效
        """
        # 去重检查
        existing = self.get_asset_by_path(path)
        if existing is not None:
            logger.info(f"Asset already exists: {path}, asset_id={existing.asset_id}")
            return self  # 不重复导入，返回原实例

        # 生成素材 ID
        asset_id = str(uuid.uuid4())

        # 显示名称默认使用文件名
        if display_name is None:
            display_name = os.path.basename(path)

        # 创建素材实例
        now = time.time()
        asset = MediaAsset(
            asset_id=asset_id,
            path=path,
            media_type=media_type,
            display_name=display_name,
            metadata=metadata,
            thumbnail_key=thumbnail_key,
            proxy_status="none",
            color_metadata=color_metadata,
            created_at=now,
        )

        # 添加到 assets 字典
        new_assets = dict(self.assets)
        new_assets[asset_id] = asset

        # 添加到 root bin
        root_bin = self.bins.get(self.root_bin_id)
        if root_bin is None:
            # 创建 root bin
            root_bin_id = str(uuid.uuid4())
            root_bin = ProjectBin(
                bin_id=root_bin_id,
                name="Root",
                parent_id=None,
            )
            new_bins = dict(self.bins)
            new_bins[root_bin_id] = root_bin
            new_root_bin_id = root_bin_id
        else:
            new_bins = dict(self.bins)
            new_bins[self.root_bin_id] = root_bin.with_asset_added(asset_id)
            new_root_bin_id = self.root_bin_id

        return replace(self, 
            assets=new_assets,
            bins=new_bins,
            root_bin_id=new_root_bin_id,
            modified_at=now,
        )

    def remove_asset(self, asset_id: str) -> EditProject:
        """
        移除素材。

        从所有 bin 中移除该素材的引用。

        Args:
            asset_id: 要移除的素材 ID

        Returns:
            新的 EditProject 实例
        """
        if asset_id not in self.assets:
            return self

        # 移除素材
        new_assets = dict(self.assets)
        del new_assets[asset_id]

        # 从所有 bin 中移除引用
        new_bins = dict(self.bins)
        for bin_id, bin_obj in self.bins.items():
            if asset_id in bin_obj.asset_ids:
                new_bins[bin_id] = bin_obj.with_asset_removed(asset_id)

        now = time.time()
        return replace(self, 
            assets=new_assets,
            bins=new_bins,
            modified_at=now,
        )

    # ==================== Subclip 管理 (Scene Cut P6) ====================

    def import_subclip(
        self,
        asset: MediaAsset,
        bin_id: str | None = None,
    ) -> EditProject:
        """
        导入子片段素材到项目。

        子片段是虚拟素材，引用原素材文件但定义特定 source range。
        使用 metadata.is_subclip=True 标识。

        Args:
            asset: 子片段 MediaAsset（由 create_subclip_asset 创建）
            bin_id: 目标素材箱 ID（None 使用 root bin）

        Returns:
            新的 EditProject 实例（包含子片段）

        Raises:
            ValueError: 如果素材不是有效的子片段
        """
        # 验证是子片段
        is_subclip = asset.metadata.get("is_subclip", False)
        if not is_subclip:
            raise ValueError(f"Asset {asset.asset_id} is not a subclip")

        # 添加到 assets 字典
        new_assets = dict(self.assets)
        new_assets[asset.asset_id] = asset

        # 确定目标 bin（AUD-17103：新建的 root bin 必须保留到最终
        # new_bins，不得被二次 dict(self.bins) 覆盖丢弃）
        new_bins = dict(self.bins)
        new_root_bin_id = self.root_bin_id
        target_bin_id = bin_id or self.root_bin_id
        if target_bin_id is None or target_bin_id not in new_bins:
            # 使用 root bin 或创建
            if self.root_bin_id and self.root_bin_id in new_bins:
                target_bin_id = self.root_bin_id
            else:
                # 创建 root bin
                root_bin_id = str(uuid.uuid4())
                root_bin = ProjectBin(
                    bin_id=root_bin_id,
                    name="Root",
                    parent_id=None,
                )
                new_bins[root_bin_id] = root_bin
                new_root_bin_id = root_bin_id
                target_bin_id = root_bin_id

        # 添加到目标 bin（从 new_bins 取，覆盖"目标 bin 为新建 root"的情形）
        target_bin = new_bins.get(target_bin_id)
        if target_bin is not None:
            new_bins[target_bin_id] = target_bin.with_asset_added(asset.asset_id)

        now = time.time()
        return replace(self, 
            revision=self.revision,  # FE-B10：保留 revision，不在此处 bump
            assets=new_assets,
            bins=new_bins,
            root_bin_id=new_root_bin_id,
            modified_at=now,
        )

    def with_subclips_added(
        self,
        subclip_assets: tuple[MediaAsset, ...],
        bin_id: str | None = None,
        remove_existing_ids: tuple[str, ...] = (),
    ) -> EditProject:
        """
        批量添加子片段素材到项目。

        这是 add_subclips_to_project 的简化版本，
        直接接受已创建的 MediaAsset 实例列表。

        Args:
            subclip_assets: 子片段 MediaAsset 元组
            bin_id: 目标素材箱 ID（None 使用 root bin）
            remove_existing_ids: 要先移除的素材 ID 元组（用于 replace 策略）

        Returns:
            新的 EditProject 实例
        """
        if not subclip_assets and not remove_existing_ids:
            return self

        # 先移除已有的（如果指定）
        project = self
        for asset_id in remove_existing_ids:
            if asset_id in project.assets:
                project = project.remove_asset(asset_id)

        # 添加每个子片段
        for asset in subclip_assets:
            project = project.import_subclip(asset, bin_id)

        return project

    def get_subclips_for_parent(
        self,
        parent_asset_id: str,
    ) -> list[MediaAsset]:
        """
        获取指定父素材的所有子片段。

        Args:
            parent_asset_id: 父素材 ID

        Returns:
            子片段 MediaAsset 列表
        """
        subclips: list[MediaAsset] = []
        for asset in self.assets.values():
            is_subclip = asset.metadata.get("is_subclip", False)
            if is_subclip:
                asset_parent_id = asset.metadata.get("parent_asset_id", "")
                if asset_parent_id == parent_asset_id:
                    subclips.append(asset)
        return subclips

    def get_subclips_for_analysis(
        self,
        analysis_id: str,
    ) -> list[MediaAsset]:
        """
        获取指定分析 ID 的所有子片段。

        用于撤销/清理操作。

        Args:
            analysis_id: Scene cut 分析 ID

        Returns:
            子片段 MediaAsset 列表
        """
        subclips: list[MediaAsset] = []
        for asset in self.assets.values():
            is_subclip = asset.metadata.get("is_subclip", False)
            if is_subclip:
                asset_analysis_id = asset.metadata.get("scene_cut_analysis_id", "")
                if asset_analysis_id == analysis_id:
                    subclips.append(asset)
        return subclips

    def remove_subclips_for_analysis(
        self,
        analysis_id: str,
    ) -> EditProject:
        """
        移除指定分析 ID 的所有子片段。

        用于撤销 "Add Cuts to Media Pool" 操作。

        Args:
            analysis_id: Scene cut 分析 ID

        Returns:
            新的 EditProject 实例
        """
        subclips = self.get_subclips_for_analysis(analysis_id)
        project = self
        for subclip in subclips:
            project = project.remove_asset(subclip.asset_id)
        return project

    def find_bin_for_asset(
        self,
        asset_id: str,
    ) -> ProjectBin | None:
        """
        查找包含指定素材的素材箱。

        Args:
            asset_id: 素材 ID

        Returns:
            找到的 ProjectBin，或 None
        """
        for bin_obj in self.bins.values():
            if asset_id in bin_obj.asset_ids:
                return bin_obj
        return None

    def get_unfiled_asset_ids(self) -> list[str]:
        """Return asset_ids that are *not* referenced by any user-created bin.

        "Un-filed" 的定义：素材没有被任何用户创建的 bin（即非 root bin）
        引用。root bin 是隐式容器（import_asset 总会把素材放进去），所以
        不能用 root bin 的 asset_ids 来判断。

        这样媒体池 root 层级就能像 Finder 一样：素材拖进某个文件夹后，
        从 root 视图消失（但它仍可能被多个 bin 引用，引用语义不变）。

        Returns:
            未归类素材的 asset_id 列表（保持 ``self.assets`` 的插入顺序）。
        """
        filed_ids: set[str] = set()
        for bin_id, bin_obj in self.bins.items():
            if bin_id == self.root_bin_id:
                continue  # root bin 是隐式容器，不算"归类"
            filed_ids.update(bin_obj.asset_ids)
        return [aid for aid in self.assets.keys() if aid not in filed_ids]

    def get_unfiled_timeline_ids(self) -> list[str]:
        """Return timeline_ids not referenced by any user-created bin.

        与 :meth:`get_unfiled_asset_ids` 对称：时间线只要被某个非 root bin
        引用，就算"已归类"，不再在 root 层级显示。
        """
        filed_ids: set[str] = set()
        for bin_id, bin_obj in self.bins.items():
            if bin_id == self.root_bin_id:
                continue
            filed_ids.update(bin_obj.timeline_ids)
        return [tid for tid in self.timelines.keys() if tid not in filed_ids]

    # ==================== Bin 管理 ====================

    def get_bin_by_id(self, bin_id: str) -> ProjectBin | None:
        """根据 ID 获取素材箱"""
        return self.bins.get(bin_id)

    def create_bin(self, name: str, parent_id: str | None = None) -> EditProject:
        """
        创建新的素材箱。

        Args:
            name: 素材箱名称
            parent_id: 父素材箱 ID（None 表示根级）

        Returns:
            新的 EditProject 实例
        """
        bin_id = str(uuid.uuid4())
        new_bin = ProjectBin(
            bin_id=bin_id,
            name=name,
            parent_id=parent_id,
        )

        new_bins = dict(self.bins)
        new_bins[bin_id] = new_bin

        now = time.time()
        return replace(self, 
            bins=new_bins,
            modified_at=now,
        )

    def move_asset_to_bin(self, asset_id: str, bin_id: str) -> EditProject:
        """
        将素材移动到指定素材箱。

        注意：素材只是引用，可以从 root bin 移动到其他 bin，
        也可以同时存在于多个 bin。

        Args:
            asset_id: 素材 ID
            bin_id: 目标素材箱 ID

        Returns:
            新的 EditProject 实例

        Raises:
            ValueError: 如果素材或素材箱不存在
        """
        if asset_id not in self.assets:
            raise ValueError(f"Asset not found: {asset_id}")
        if bin_id not in self.bins:
            raise ValueError(f"Bin not found: {bin_id}")

        # 添加到目标 bin
        target_bin = self.bins[bin_id]
        new_bins = dict(self.bins)
        new_bins[bin_id] = target_bin.with_asset_added(asset_id)

        now = time.time()
        return replace(self, 
            bins=new_bins,
            modified_at=now,
        )

    def move_timeline_to_bin(self, timeline_id: str, bin_id: str) -> EditProject:
        """
        将时间线加入指定素材箱。

        与素材一样，时间线只是引用——同一个时间线可以出现在多个 bin 中。
        时间线本身仍然存储在 ``self.timelines`` 顶层字典里，bin 通过
        ``timeline_ids`` 元组引用它。

        Args:
            timeline_id: 时间线 ID
            bin_id: 目标素材箱 ID

        Returns:
            新的 EditProject 实例

        Raises:
            ValueError: 如果时间线或素材箱不存在
        """
        if timeline_id not in self.timelines:
            raise ValueError(f"Timeline not found: {timeline_id}")
        if bin_id not in self.bins:
            raise ValueError(f"Bin not found: {bin_id}")

        target_bin = self.bins[bin_id]
        new_bins = dict(self.bins)
        new_bins[bin_id] = target_bin.with_timeline_added(timeline_id)

        now = time.time()
        return replace(self, 
            bins=new_bins,
            modified_at=now,
        )

    def remove_asset_from_bin(self, asset_id: str, bin_id: str) -> EditProject:
        """从指定 bin 移除素材引用（拖出 bin 回到根级别）。

        素材本身保留在项目中（仍在 root bin 隐式存在），只是不再被该
        bin 引用。这是 ``move_asset_to_bin`` 的逆操作。

        Args:
            asset_id: 素材 ID
            bin_id: 要从中移除引用的 bin ID

        Returns:
            新的 EditProject 实例

        Raises:
            ValueError: 如果素材或 bin 不存在
        """
        if asset_id not in self.assets:
            raise ValueError(f"Asset not found: {asset_id}")
        if bin_id not in self.bins:
            raise ValueError(f"Bin not found: {bin_id}")

        target_bin = self.bins[bin_id]
        new_bins = dict(self.bins)
        new_bins[bin_id] = target_bin.with_asset_removed(asset_id)

        now = time.time()
        return replace(self, 
            bins=new_bins,
            modified_at=now,
        )

    def remove_timeline_from_bin(self, timeline_id: str, bin_id: str) -> EditProject:
        """从指定 bin 移除时间线引用（拖出 bin 回到根级别）。"""
        if timeline_id not in self.timelines:
            raise ValueError(f"Timeline not found: {timeline_id}")
        if bin_id not in self.bins:
            raise ValueError(f"Bin not found: {bin_id}")

        target_bin = self.bins[bin_id]
        new_bins = dict(self.bins)
        new_bins[bin_id] = target_bin.with_timeline_removed(timeline_id)

        now = time.time()
        return replace(self, 
            bins=new_bins,
            modified_at=now,
        )

    def rename_bin(self, bin_id: str, new_name: str) -> EditProject:
        """
        重命名素材箱。

        Args:
            bin_id: 素材箱 ID
            new_name: 新名称

        Returns:
            新的 EditProject 实例

        Raises:
            ValueError: 如果素材箱不存在
        """
        if bin_id not in self.bins:
            raise ValueError(f"Bin not found: {bin_id}")

        bin_obj = self.bins[bin_id]
        new_bin = bin_obj.with_name(new_name)

        new_bins = dict(self.bins)
        new_bins[bin_id] = new_bin

        now = time.time()
        return replace(self, 
            bins=new_bins,
            modified_at=now,
        )

    def rename_asset(self, asset_id: str, new_display_name: str) -> EditProject:
        """
        重命名素材的显示名称（不改动磁盘文件）。

        Args:
            asset_id: 素材 ID
            new_display_name: 新的显示名称

        Returns:
            新的 EditProject 实例

        Raises:
            ValueError: 如果素材不存在
        """
        if asset_id not in self.assets:
            raise ValueError(f"Asset not found: {asset_id}")

        asset = self.assets[asset_id]
        new_asset = asset.with_display_name(new_display_name)

        new_assets = dict(self.assets)
        new_assets[asset_id] = new_asset

        now = time.time()
        return replace(self, 
            assets=new_assets,
            modified_at=now,
        )

    def reparent_bin(self, bin_id: str, new_parent_id: str) -> EditProject:
        """改变 bin 的父级（拖拽 bin 进另一个 bin）。

        包含循环检测：禁止把 bin 移入自己或自己的任何子孙，否则会形成环。

        Args:
            bin_id: 要移动的 bin ID
            new_parent_id: 新父 bin ID（root_bin_id 表示移到根级别）

        Returns:
            新的 EditProject 实例

        Raises:
            ValueError: 如果 bin 不存在，或检测到循环（移入自身/子孙）
        """
        if bin_id not in self.bins:
            raise ValueError(f"Bin not found: {bin_id}")
        if new_parent_id != self.root_bin_id and new_parent_id not in self.bins:
            raise ValueError(f"Parent bin not found: {new_parent_id}")

        bin_obj = self.bins[bin_id]
        if bin_obj.parent_id == new_parent_id:
            return self  # No change needed.

        # Cycle detection: new_parent must not be bin_id itself nor any of
        # bin_id's descendants. Walk the descendant subtree of bin_id.
        if new_parent_id == bin_id:
            raise ValueError("Cannot move a bin into itself")
        descendants: set[str] = set()
        stack = [bin_id]
        while stack:
            current = stack.pop()
            for bid, b in self.bins.items():
                if b.parent_id == current and bid not in descendants:
                    descendants.add(bid)
                    stack.append(bid)
        if new_parent_id in descendants:
            raise ValueError(
                "Cannot move a bin into one of its own descendants (cycle)"
            )

        new_bin = bin_obj.with_parent(new_parent_id)
        new_bins = dict(self.bins)
        new_bins[bin_id] = new_bin

        now = time.time()
        return replace(self, 
            bins=new_bins,
            modified_at=now,
        )

    def duplicate_asset(self, asset_id: str, bin_id: str | None = None) -> tuple[EditProject, str]:
        """创建素材副本。

        普通素材副本共享源文件；Motion Graphic 副本则克隆场景/HTML
        身份，确保媒体池中的两个素材可以独立编辑。所有副本都是全新的
        MediaAsset，不继承原 asset 的 timeline 引用。

        Args:
            asset_id: 要复制的素材 ID
            bin_id: 放置副本的 bin ID。None 表示当前查看的 bin；root bin
                表示放到根级别（不显式归档）。

        Returns:
            (新项目, 新 asset_id)

        Raises:
            ValueError: 如果素材不存在
        """
        if asset_id not in self.assets:
            raise ValueError(f"Asset not found: {asset_id}")
        src = self.assets[asset_id]
        now = time.time()

        # Media-pool duplication has different semantics from timeline copy:
        # a Motion Graphic asset must receive its own scene/HTML identity so
        # editing one pool item never mutates the other.  Keep the human name
        # deterministic and numbered instead of creating duplicate labels.
        motion_graphic_scene = None
        if str(getattr(src, "media_type", "")) == "motion_graphic":
            metadata = dict(getattr(src, "metadata", {}) or {})
            scene_id = str(metadata.get("scene_id", "") or "")
            if scene_id:
                try:
                    from src.features.teaching_animation.scene_store import get_shared_scene_store
                    motion_graphic_scene = get_shared_scene_store().get(scene_id)
                except (ImportError, RuntimeError):
                    motion_graphic_scene = None
            if motion_graphic_scene is not None:
                copied_scene_id = f"scene_{uuid.uuid4().hex}"
                base_name = str(getattr(src, "display_name", "Motion Graphic Clip") or "Motion Graphic Clip")
                used_names = {
                    str(getattr(asset, "display_name", "") or "")
                    for asset in self.assets.values()
                }
                suffix = 2
                copied_name = f"{base_name} {suffix}"
                while copied_name in used_names:
                    suffix += 1
                    copied_name = f"{base_name} {suffix}"
                from src.features.teaching_animation.scene_store import get_shared_scene_store
                from dataclasses import replace as replace_scene
                copied_scene = replace_scene(
                    motion_graphic_scene,
                    scene_id=copied_scene_id,
                    name=copied_name,
                )
                get_shared_scene_store().register(copied_scene)
                new_asset_id = f"teaching_animation:{copied_scene_id}"
                metadata.update({
                    "scene_id": copied_scene_id,
                    "width": int(getattr(copied_scene, "width", metadata.get("width", 1920)) or 1920),
                    "height": int(getattr(copied_scene, "height", metadata.get("height", 1080)) or 1080),
                    "fps": float(getattr(copied_scene, "fps", metadata.get("fps", 25.0)) or 25.0),
                    "duration_frames": int(getattr(copied_scene, "duration_frames", metadata.get("duration_frames", 1)) or 1),
                    "duration_seconds": float(getattr(copied_scene, "duration_frames", 1) or 1)
                    / max(1.0, float(getattr(copied_scene, "fps", 25.0) or 25.0)),
                    "render_cache_id": f"scene:{copied_scene_id}",
                })
                src_path = f"topos://motion_graphic/{copied_scene_id}"
                src_name = copied_name
            else:
                # A project restored without its scene store can still be
                # duplicated safely by assigning a new virtual identity; the
                # editor will recreate the empty scene from the copied
                # metadata instead of accidentally sharing the old scene.
                copied_scene_id = f"scene_{uuid.uuid4().hex}"
                new_asset_id = f"teaching_animation:{copied_scene_id}"
                metadata = dict(getattr(src, "metadata", {}) or {})
                metadata["scene_id"] = copied_scene_id
                metadata["render_cache_id"] = f"scene:{copied_scene_id}"
                src_path = f"topos://motion_graphic/{copied_scene_id}"
                base_name = str(getattr(src, "display_name", "Motion Graphic Clip") or "Motion Graphic Clip")
                used_names = {
                    str(getattr(asset, "display_name", "") or "")
                    for asset in self.assets.values()
                }
                suffix = 2
                src_name = f"{base_name} {suffix}"
                while src_name in used_names:
                    suffix += 1
                    src_name = f"{base_name} {suffix}"
        else:
            new_asset_id = str(uuid.uuid4())
            metadata = dict(src.metadata)
            src_path = src.path
            src_name = src.display_name

        new_asset = MediaAsset(
            asset_id=new_asset_id,
            path=src_path,
            media_type=src.media_type,
            display_name=src_name,
            metadata=metadata,
            thumbnail_key=(
                f"motion_graphic:{metadata.get('scene_id')}"
                if str(getattr(src, "media_type", "")) == "motion_graphic"
                else src.thumbnail_key
            ),
            proxy_status="none",  # Don't inherit proxy; regenerate if needed.
            is_offline=src.is_offline,  # AUD-17106：离线状态如实继承
            color_metadata=dict(src.color_metadata) if src.color_metadata else None,
            created_at=now,
            reel=src.reel,
            scene=src.scene,
            shot=src.shot,
            take=src.take,
            # AUD-17106：素材日志字段（P1-12）随副本继承，不再静默丢失
            keywords=tuple(src.keywords),
            rating=src.rating,
            flag=src.flag,
            color_label=src.color_label,
            comment=src.comment,
        )
        new_assets = dict(self.assets)
        new_assets[new_asset_id] = new_asset

        # Decide which bin to register the copy in.
        target_bin_id = bin_id if bin_id is not None else self.root_bin_id
        new_bins = dict(self.bins)
        if target_bin_id in new_bins:
            new_bins[target_bin_id] = new_bins[target_bin_id].with_asset_added(new_asset_id)

        new_project = replace(self, 
            assets=new_assets,
            bins=new_bins,
            modified_at=now,
        )
        return new_project, new_asset_id

    def update_asset_production_info(
        self,
        asset_id: str,
        reel: str | None = None,
        scene: str | None = None,
        shot: str | None = None,
        take: str | None = None,
    ) -> EditProject:
        """更新素材的影视制作元数据（Reel/Scene/Shot/Take）。

        任何为 None 的参数保持原值不变。
        """
        if asset_id not in self.assets:
            raise ValueError(f"Asset not found: {asset_id}")
        asset = self.assets[asset_id]
        new_asset = asset.with_production_info(reel=reel, scene=scene, shot=shot, take=take)
        new_assets = dict(self.assets)
        new_assets[asset_id] = new_asset
        now = time.time()
        return replace(self, 
            assets=new_assets,
            modified_at=now,
        )

    def update_asset_log_info(
        self,
        asset_id: str,
        *,
        keywords: tuple[str, ...] | None = None,
        rating: int | None = None,
        flag: str | None = None,
        color_label: str | None = None,
        comment: str | None = None,
    ) -> EditProject:
        """更新素材的日志字段（P1-12：关键词/评级/旗标/颜色标签/注释）。

        任何为 None 的参数保持原值不变。
        """
        if asset_id not in self.assets:
            raise ValueError(f"Asset not found: {asset_id}")
        asset = self.assets[asset_id]
        new_asset = asset.with_log_fields(
            keywords=keywords,
            rating=rating,
            flag=flag,
            color_label=color_label,
            comment=comment,
        )
        new_assets = dict(self.assets)
        new_assets[asset_id] = new_asset
        now = time.time()
        return replace(self, 
            assets=new_assets,
            modified_at=now,
        )

    def remove_bin(self, bin_id: str) -> EditProject:
        """
        移除素材箱。

        不能移除 root bin。素材箱中的素材引用会被移除，
        但素材本身保留在其他素材箱或 root bin 中；被删 bin 的子 bin
        上移一级（挂到被删 bin 的父级），parent_id 不留悬挂引用。

        Args:
            bin_id: 要移除的素材箱 ID

        Returns:
            新的 EditProject 实例

        Raises:
            ValueError: 如果试图移除 root bin
        """
        if bin_id == self.root_bin_id:
            raise ValueError("Cannot remove root bin")

        if bin_id not in self.bins:
            return self

        # AUD-17102：子 bin 上移一级，避免 parent_id 指向已删除的 bin
        removed_parent = self.bins[bin_id].parent_id
        new_bins = dict(self.bins)
        del new_bins[bin_id]
        for child_id, child_bin in self.bins.items():
            if child_bin.parent_id == bin_id:
                new_bins[child_id] = child_bin.with_parent(removed_parent)

        now = time.time()
        return replace(self, 
            bins=new_bins,
            modified_at=now,
        )

    # ==================== Smart Bin 管理（P1-12 用户自建已保存搜索） ====================

    def add_smart_bin(self, smart_bin: SmartBin) -> EditProject:
        """添加用户自建 Smart Bin。

        bin_id 冲突时抛 ValueError，避免静默覆盖既有搜索。
        """
        if smart_bin.bin_id in self.smart_bins:
            raise ValueError(f"Smart bin already exists: {smart_bin.bin_id}")
        new_bins = dict(self.smart_bins)
        new_bins[smart_bin.bin_id] = smart_bin
        return self._with_smart_bins(new_bins)

    def remove_smart_bin(self, bin_id: str) -> EditProject:
        """移除用户自建 Smart Bin（不存在的 id 静默忽略，幂等）。"""
        if bin_id not in self.smart_bins:
            return self
        new_bins = dict(self.smart_bins)
        del new_bins[bin_id]
        return self._with_smart_bins(new_bins)

    def rename_smart_bin(self, bin_id: str, new_name: str) -> EditProject:
        """重命名用户自建 Smart Bin。"""
        if bin_id not in self.smart_bins:
            raise ValueError(f"Smart bin not found: {bin_id}")
        old = self.smart_bins[bin_id]
        new_bins = dict(self.smart_bins)
        new_bins[bin_id] = replace(old, name=new_name)
        return self._with_smart_bins(new_bins)

    def _with_smart_bins(self, new_smart_bins: dict[str, SmartBin]) -> EditProject:
        """以新的 smart_bins 字典派生工程（其余字段保持不变）。"""
        return replace(self, 
            smart_bins=new_smart_bins,
            modified_at=time.time(),
        )

    # ==================== Timeline 管理 ====================

    def get_timeline_by_id(self, timeline_id: str) -> TimelineSequenceRef | None:
        """根据 ID 获取时间线"""
        return self.timelines.get(timeline_id)

    def create_timeline(
        self,
        name: str,
        fps: float = 24.0,
        width: int = 1920,
        height: int = 1080,
    ) -> EditProject:
        """
        创建新的时间线。

        Args:
            name: 时间线名称
            fps: 帧率
            width: 视频宽度
            height: 视频高度

        Returns:
            新的 EditProject 实例，新创建的时间线为 active_timeline_id
        """
        timeline_id = str(uuid.uuid4())
        now = time.time()
        new_timeline = TimelineSequenceRef(
            timeline_id=timeline_id,
            name=name,
            fps=fps,
            width=width,
            height=height,
            sample_rate=48000,
            created_at=now,
        )

        new_timelines = dict(self.timelines)
        new_timelines[timeline_id] = new_timeline

        # N27/R25：创建是内容变更，但 revision 的发布只在统一提交边界
        # （commit adapter / ProjectCommandStack）单调 +1——纯 helper 保
        # 留当前 revision，此前 fresh 构造丢字段使"新建时间线"把高版本
        # 工程归 0。
        return replace(
            self,
            timelines=new_timelines,
            active_timeline_id=timeline_id,  # 设置为活动时间线
            modified_at=now,
        )

    def with_timeline_ref_added(
        self,
        ref: TimelineSequenceRef,
        set_active: bool = False,
    ) -> EditProject:
        """注册一条时间线引用而不切换 active（P1-02 嵌套序列创建用）。

        与 :meth:`create_timeline` 不同，本方法不生成新 id、不激活。
        """
        new_timelines = dict(self.timelines)
        new_timelines[ref.timeline_id] = ref
        return replace(
            self,
            timelines=new_timelines,
            active_timeline_id=(
                ref.timeline_id if set_active else self.active_timeline_id
            ),
        )

    def with_media_asset_added(self, asset: MediaAsset) -> EditProject:
        """加入一个已构造好的 MediaAsset（虚拟素材，P1-02 嵌套序列用）。"""
        new_assets = dict(self.assets)
        new_assets[asset.asset_id] = asset
        return replace(self, assets=new_assets)

    def with_source_patch_override(self, asset_id: str, spec) -> "EditProject":
        """F2.1：设置/清除素材级 Source Patch 覆盖（None = 删除覆盖）。

        spec 为 ``SourcePatchSpec``（不可变）；经 UpdateSourcePatchCommand
        提交以获得 Undo/持久化，生产代码勿直接调用本方法改工程。
        """
        new_overrides = dict(self.source_patch_overrides)
        if spec is None:
            new_overrides.pop(asset_id, None)
        else:
            new_overrides[asset_id] = spec
        return replace(self, source_patch_overrides=new_overrides)

    def with_interpretation_override(self, asset_id: str, override) -> "EditProject":
        """F2.7：设置/清除素材级解释覆盖（None = 删除覆盖）。

        同时清该素材代理状态（代理按探测参数生成，解释变更不重生成会
        串片——与 relink 同语义）；override 为 InterpretationOverride。
        """
        new_overrides = dict(self.interpretation_overrides)
        if override is None:
            new_overrides.pop(asset_id, None)
        else:
            new_overrides[asset_id] = override
        new_assets = dict(self.assets)
        asset = new_assets.get(asset_id)
        if asset is not None and (
                getattr(asset, "proxy_status", "none") != "none"
                or getattr(asset, "proxy_path", None)):
            from dataclasses import replace as _dc_replace
            new_assets[asset_id] = _dc_replace(
                asset, proxy_path=None, proxy_status="none")
        return replace(self, interpretation_overrides=new_overrides,
                       assets=new_assets)

    def with_project_snapshots(self, table: dict) -> "EditProject":
        """F8.1：整体替换工程快照表。"""
        from dataclasses import replace as _replace
        return _replace(self, project_snapshots=dict(table))

    def with_color_groups(self, groups: dict) -> "EditProject":
        """F4.1：整体替换调色组表（命令层保证事务性；纯模型替换）。"""
        from dataclasses import replace as _replace
        return _replace(self, color_groups=dict(groups))

    def with_color_stack_versions(self, table: dict) -> "EditProject":
        """F4.2：整体替换版本/锁表（命令层保证事务性）。"""
        from dataclasses import replace as _replace
        return _replace(self, color_stack_versions=dict(table))

    def with_color_stacks(self, stacks: dict) -> "EditProject":
        """F4.2：整体替换调色栈表（版本激活等命令的写回点）。"""
        from dataclasses import replace as _replace
        return _replace(self, color_stacks=dict(stacks))

    def with_audio_sync_binding(self, asset_id: str, binding) -> "EditProject":
        """F2.10：设置/清除视频素材的外录音频绑定（None = 解绑）。

        binding 为 AudioSyncBinding；WAV 素材必须存在且 media_type=audio。
        """
        new_bindings = dict(self.audio_sync_bindings)
        if binding is None:
            new_bindings.pop(asset_id, None)
        else:
            wav = self.assets.get(binding.wav_asset_id)
            if wav is None:
                raise ValueError(
                    f"WAV asset not found: {binding.wav_asset_id}")
            if getattr(wav, "media_type", "") != "audio":
                raise ValueError(
                    f"bound asset is not audio: {binding.wav_asset_id}")
            binding.validate()
            new_bindings[asset_id] = binding
        return replace(self, audio_sync_bindings=new_bindings)

    def set_active_timeline(self, timeline_id: str | None) -> EditProject:
        """
        设置活动时间线。

        Args:
            timeline_id: 时间线 ID（None 表示清除）

        Returns:
            新的 EditProject 实例

        Raises:
            ValueError: 如果时间线不存在
        """
        if timeline_id is not None and timeline_id not in self.timelines:
            raise ValueError(f"Timeline not found: {timeline_id}")

        # N27/R25：纯导航不重置 revision——此前 fresh 构造丢 revision 字
        # 段（切时间线/新建后 7→0，Relink UI 仍 7，旧 AI plan 的
        # base_revision 失配窗口重开）。导航无内容变更，revision 保持。
        return replace(
            self,
            active_timeline_id=timeline_id,
            modified_at=time.time(),
        )

    def set_active_comp_clip(self, comp_clip_id: str | None) -> EditProject:
        """
        设置活动 Comp Clip（记住当前打开的合成，供下次打开工程恢复）。

        Args:
            comp_clip_id: Comp Clip ID（None 表示清除）

        Returns:
            新的 EditProject 实例

        Raises:
            ValueError: 如果 Comp Clip 不存在
        """
        if comp_clip_id is not None and comp_clip_id not in self.comp_clips:
            raise ValueError(f"Comp clip not found: {comp_clip_id}")
        if comp_clip_id == self.active_comp_clip_id:
            return self

        now = time.time()
        return replace(self, 
            active_comp_clip_id=comp_clip_id,
            modified_at=now,
        )

    def remove_timeline(self, timeline_id: str) -> EditProject:
        """
        移除时间线。

        同时清理各素材箱对这条时间线的引用（timeline_ids），避免
        悬挂 ID 随工程持久化（与 remove_asset 清理 bin 引用对称）。

        Args:
            timeline_id: 要移除的时间线 ID

        Returns:
            新的 EditProject 实例
        """
        if timeline_id not in self.timelines:
            return self

        new_timelines = dict(self.timelines)
        del new_timelines[timeline_id]

        # AUD-17101：从所有 bin 中移除该时间线引用，不留悬挂 ID
        new_bins = dict(self.bins)
        for bin_id, bin_obj in self.bins.items():
            if timeline_id in bin_obj.timeline_ids:
                new_bins[bin_id] = bin_obj.with_timeline_removed(timeline_id)

        # 如果移除的是活动时间线，清除 active_timeline_id
        new_active_id = self.active_timeline_id
        if self.active_timeline_id == timeline_id:
            new_active_id = None

        now = time.time()
        return replace(self, 
            bins=new_bins,
            timelines=new_timelines,
            active_timeline_id=new_active_id,
            modified_at=now,
        )

    # ==================== Composition 管理 (P9.2) ====================

    def get_composition_by_id(self, composite_id: str) -> Any | None:
        """根据 ID 获取合成图数据"""
        return self.compositions.get(composite_id)

    def create_composition(
        self,
        composite_id: str,
        source_clip_id: str,
        source_asset_id: str,
        source_asset_path: str,
        name: str = "",
        width: int = 1920,
        height: int = 1080,
        color_stack_id: str | None = None,
    ) -> EditProject:
        """
        创建新的合成图 (P9.2)。

        为 clip 创建关联的合成图数据，用于 Edit → Composite 页面联动。

        Args:
            composite_id: 合成图唯一 ID
            source_clip_id: 来源 clip ID
            source_asset_id: 来源素材 ID
            source_asset_path: 来源素材路径
            name: 合成名称（默认为 clip 名称）
            width: 输出宽度
            height: 输出高度
            color_stack_id: clip 调色栈 ID；有值时默认图插入 Topos Color 节点

        Returns:
            新的 EditProject 实例
        """
        from src.features.edit.core.composite_graph_data import CompositeGraphData

        # 创建默认合成图
        comp_data = CompositeGraphData.create_default_for_clip(
            clip_id=source_clip_id,
            asset_id=source_asset_id,
            asset_path=source_asset_path,
            width=width,
            height=height,
            name=name,
            composite_id=composite_id,
            color_stack_id=color_stack_id,
        )

        new_compositions = dict(self.compositions)
        new_compositions[composite_id] = comp_data.to_dict()

        now = time.time()
        return replace(self, 
            compositions=new_compositions,
            modified_at=now,
        )

    def update_composition(
        self,
        composite_id: str,
        nodes: list[Any] | None = None,
        connections: list[Any] | None = None,
        name: str | None = None,
    ) -> EditProject:
        """
        更新合成图数据 (P9.2)。

        Args:
            composite_id: 合成图 ID
            nodes: 节点数据列表（可选）
            connections: 连接数据列表（可选）
            name: 合成名称（可选）

        Returns:
            新的 EditProject 实例

        Raises:
            ValueError: 如果合成图不存在
        """

        if composite_id not in self.compositions:
            raise ValueError(f"Composition not found: {composite_id}")

        comp_dict = dict(self.compositions[composite_id])

        if nodes is not None:
            comp_dict["nodes"] = nodes
        if connections is not None:
            comp_dict["connections"] = connections
        if name is not None:
            comp_dict["name"] = name
        comp_dict["modified_at"] = time.time()

        new_compositions = dict(self.compositions)
        new_compositions[composite_id] = comp_dict

        now = time.time()
        return replace(self, 
            compositions=new_compositions,
            modified_at=now,
        )

    def remove_composition(self, composite_id: str) -> EditProject:
        """
        移除合成图 (P9.2)。

        Args:
            composite_id: 要移除的合成图 ID

        Returns:
            新的 EditProject 实例
        """
        if composite_id not in self.compositions:
            return self

        new_compositions = dict(self.compositions)
        del new_compositions[composite_id]

        now = time.time()
        return replace(self, 
            compositions=new_compositions,
            modified_at=now,
        )

    @property
    def composition_count(self) -> int:
        """合成图数量"""
        return len(self.compositions)

    # ==================== Color Stack 管理 (P9.1) ====================

    def get_color_stack_by_id(self, stack_id: str) -> ColorStackData | None:
        """根据 ID 获取调色栈"""
        return self.color_stacks.get(stack_id)

    def get_color_stack_for_asset(self, asset_id: str) -> ColorStackData | None:
        """获取素材的默认调色栈"""
        for stack in self.color_stacks.values():
            if stack.asset_id == asset_id:
                return stack
        return None

    def create_color_stack(
        self,
        asset_id: str | None = None,
        name: str = "Default",
    ) -> tuple[EditProject, str]:
        """
        创建新的调色栈 (P9.1)。

        Args:
            asset_id: 关联素材 ID（可选，用于素材默认调色栈）
            name: 调色栈名称

        Returns:
            Tuple of (新的 EditProject 实例, stack_id)
        """
        stack = ColorStackData.create(asset_id=asset_id, name=name)
        new_stacks = dict(self.color_stacks)
        new_stacks[stack.stack_id] = stack

        now = time.time()
        return replace(self, 
            color_stacks=new_stacks,
            modified_at=now,
        ), stack.stack_id

    def update_color_stack(
        self,
        stack_id: str,
        grading_params: dict[str, Any] | None = None,
        curves_data: dict[str, Any] | None = None,
        hsl_curve_data: dict[str, Any] | None = None,
        mixer_params: dict[str, Any] | None = None,
        soft_clip_params: dict[str, Any] | None = None,
        lut_path: str | None = None,
        lut_intensity: float | None = None,
        layers: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
        active_layer_index: int | None = None,
        raw_params: dict[str, Any] | None = None,
        trackings: dict[str, Any] | None = None,
        video_denoise: dict[str, Any] | None = None,
        video_stabilization: dict[str, Any] | None = None,
    ) -> EditProject:
        """
        更新调色栈参数 (P9.1)。

        Args:
            stack_id: 调色栈 ID
            grading_params: 调色参数（可选）
            curves_data: 曲线数据（可选）
            hsl_curve_data: HSL 曲线数据（可选）
            mixer_params: 颜色混合参数（可选）
            soft_clip_params: Soft Clip 参数（可选）
            lut_path: LUT 路径（可选）
            lut_intensity: LUT 强度（可选）
            layers: 完整 Layer 快照（可选；持久化边界刷新权威状态）
            active_layer_index: 活动图层索引（可选；与 layers 配套）
            raw_params: RAW 解码参数（可选；与 layers 配套）
            trackings: 跟踪数据（可选；P1-COLOR-16 分类粘贴通道）

        Returns:
            新的 EditProject 实例
        """
        stack = self.color_stacks.get(stack_id)
        if stack is None:
            logger.warning(f"Color stack not found: {stack_id}")
            return self

        # 根据传入参数更新
        if grading_params is not None:
            stack = stack.with_grading_params(grading_params)
        if curves_data is not None:
            stack = stack.with_curves(curves_data)
        if hsl_curve_data is not None:
            stack = stack.with_hsl_curves(hsl_curve_data)
        if mixer_params is not None:
            stack = stack.with_mixer_params(mixer_params)
        if soft_clip_params is not None:
            stack = stack.with_soft_clip_params(soft_clip_params)
        if lut_path is not None or lut_intensity is not None:
            intensity = lut_intensity if lut_intensity is not None else stack.lut_intensity
            stack = stack.with_lut(lut_path, intensity)
        if layers is not None or raw_params is not None or active_layer_index is not None:
            stack = stack.with_layers(
                layers if layers is not None else stack.layers,
                active_layer_index=(
                    stack.active_layer_index
                    if active_layer_index is None
                    else active_layer_index
                ),
                raw_params=raw_params,
            )
        if trackings is not None:
            stack = stack.with_trackings(trackings)
        if video_stabilization is not None:
            stack = stack.with_video_stabilization(video_stabilization)
        if video_denoise is not None:
            # 空 dict 归一为 None（undo 清除通道传 {}）：
            # None = 该 clip 未启用降噪
            stack = stack.with_video_denoise(video_denoise or None)

        new_stacks = dict(self.color_stacks)
        new_stacks[stack_id] = stack

        now = time.time()
        from dataclasses import replace as _replace
        return _replace(
            self,
            color_stacks=new_stacks,
            modified_at=now,
        )

    def delete_color_stack(self, stack_id: str) -> EditProject:
        """
        删除调色栈 (P9.1)。

        Args:
            stack_id: 调色栈 ID

        Returns:
            新的 EditProject 实例
        """
        if stack_id not in self.color_stacks:
            logger.warning(f"Color stack not found: {stack_id}")
            return self

        new_stacks = dict(self.color_stacks)
        del new_stacks[stack_id]

        now = time.time()
        from dataclasses import replace as _replace
        return _replace(
            self,
            color_stacks=new_stacks,
            modified_at=now,
        )

    @property
    def color_stack_count(self) -> int:
        """调色栈数量"""
        return len(self.color_stacks)

    # ==================== Render Cache 管理 ====================

    def get_render_cache_for_clip(
        self,
        timeline_id: str,
        clip_id: str,
        render_fingerprint: str | None = None,
    ) -> Any | None:
        """
        Get the render cache manifest for a specific clip.

        Args:
            timeline_id: Timeline ID
            clip_id: Clip ID
            render_fingerprint: Optional semantic dependency identity. When
                supplied, only that exact fingerprint is returned so undo/redo
                can recover a historical valid variant.

        Returns:
            RenderCacheManifest if found, None otherwise
        """
        from src.features.edit.core.render_cache import RenderCacheManifest
        # Newer renders are appended with a fresh cache id. A fingerprint-
        # qualified lookup walks the same history and returns the newest
        # matching semantic variant; an unqualified lookup preserves the
        # legacy "latest manifest" behavior.
        for manifest_dict in reversed(tuple(self.render_cache_manifests.values())):
            manifest = RenderCacheManifest.from_dict(manifest_dict) if isinstance(manifest_dict, dict) else manifest_dict
            if (isinstance(manifest, RenderCacheManifest)
                    and manifest.timeline_id == timeline_id
                    and manifest.clip_id == clip_id):
                if render_fingerprint is not None and (
                    manifest.render_fingerprint != str(render_fingerprint)
                ):
                    continue
                return manifest
        return None

    def set_render_cache_manifest(self, manifest: Any) -> EditProject:
        """
        Store or update a render cache manifest.

        Args:
            manifest: RenderCacheManifest instance

        Returns:
            New EditProject with updated manifests
        """
        from src.features.edit.core.render_cache import RenderCacheManifest, RenderCacheStatus
        if isinstance(manifest, RenderCacheManifest):
            manifest_dict = manifest.to_dict()
            cache_id = manifest.cache_id
        elif isinstance(manifest, dict):
            manifest_dict = manifest
            cache_id = manifest.get("cache_id", "")
        else:
            return self

        new_manifests = dict(self.render_cache_manifests)
        # Fingerprinted manifests are immutable semantic variants. Keep
        # completed variants so undo/redo can select the old result again.
        # Legacy entries and non-terminal entries are still removed when a
        # new fingerprinted render is published; they cannot participate in
        # semantic reuse and an orphaned RENDERING entry could block a new
        # render after a crash. A legacy write preserves the historical
        # one-current-manifest behavior.
        try:
            manifest_timeline_id = str(manifest_dict.get("timeline_id", "") or "")
            manifest_clip_id = str(manifest_dict.get("clip_id", "") or "")
            manifest_fingerprint = str(manifest_dict.get("render_fingerprint", "") or "")
            if manifest_timeline_id and manifest_clip_id:
                for old_cache_id, old_value in list(new_manifests.items()):
                    if str(old_cache_id) == str(cache_id):
                        continue
                    old_manifest = (
                        RenderCacheManifest.from_dict(old_value)
                        if isinstance(old_value, dict) else old_value
                    )
                    if not (
                        isinstance(old_manifest, RenderCacheManifest)
                        and str(old_manifest.timeline_id) == manifest_timeline_id
                        and str(old_manifest.clip_id) == manifest_clip_id
                    ):
                        continue
                    keep_historical_variant = bool(
                        manifest_fingerprint
                        and old_manifest.render_fingerprint
                        and old_manifest.status == RenderCacheStatus.VALID
                    )
                    if not keep_historical_variant:
                        del new_manifests[old_cache_id]
        except (TypeError, ValueError):
            # Cache bookkeeping must never make a project mutation fail; an
            # unstructured legacy entry is simply preserved.
            pass
        new_manifests[cache_id] = manifest_dict

        now = time.time()
        return replace(self, 
            render_cache_manifests=new_manifests,
            modified_at=now,
        )

    def prune_render_cache_variants(
        self,
        timeline_id: str,
        clip_id: str,
        max_variants: int = 8,
    ) -> tuple[EditProject, tuple[str, ...]]:
        """Bound historical semantic variants and report dropped file paths.

        The immutable project model cannot delete files itself. This method
        returns both the pruned project and paths that the host controller may
        remove after checking whether another manifest still references them.
        The newest variants are retained, including the just-published
        RENDERING/VALID entry.
        """
        from src.features.edit.core.render_cache import RenderCacheManifest

        limit = max(1, int(max_variants))
        entries: list[tuple[str, RenderCacheManifest]] = []
        for cache_id, raw in self.render_cache_manifests.items():
            manifest = RenderCacheManifest.from_dict(raw) if isinstance(raw, dict) else raw
            if (
                isinstance(manifest, RenderCacheManifest)
                and str(manifest.timeline_id) == str(timeline_id)
                and str(manifest.clip_id) == str(clip_id)
            ):
                entries.append((str(cache_id), manifest))
        if len(entries) <= limit:
            return self, ()

        remove_entries = entries[:-limit]
        new_manifests = dict(self.render_cache_manifests)
        removed_paths = tuple(
            sorted({str(manifest.path or "") for _, manifest in remove_entries if manifest.path})
        )
        for cache_id, _manifest in remove_entries:
            new_manifests.pop(cache_id, None)
        return replace(
            self,
            render_cache_manifests=new_manifests,
            modified_at=time.time(),
        ), removed_paths

    def invalidate_render_cache_for_color_stack(self, stack_id: str) -> EditProject:
        """Invalidate all caches that reference the given color stack.

        Finds all clips whose composite graph uses this color stack
        and marks their caches as stale.
        """
        from src.features.edit.core.render_cache import RenderCacheManifest

        affected_clip_ids = set()
        for graph_id, graph_data in self.compositions.items():
            if not isinstance(graph_data, dict):
                continue
            for node in graph_data.get("nodes", []):
                if node.get("type") == "topos_color" and node.get("data", {}).get("color_stack_id") == stack_id:
                    # Find clips that use this graph
                    for cache_id, m_dict in self.render_cache_manifests.items():
                        m = RenderCacheManifest.from_dict(m_dict) if isinstance(m_dict, dict) else m_dict
                        if isinstance(m, RenderCacheManifest) and m.composite_id == graph_id:
                            affected_clip_ids.add((m.timeline_id, m.clip_id))

        result = self
        for timeline_id, clip_id in affected_clip_ids:
            result = result.invalidate_render_cache_for_clip(timeline_id, clip_id)
        return result

    def invalidate_render_cache_for_composite(self, composite_id: str) -> EditProject:
        """Invalidate all caches that use the given composite graph."""
        from src.features.edit.core.render_cache import RenderCacheManifest, RenderCacheStatus

        new_manifests = dict(self.render_cache_manifests)

        for cache_id, manifest_dict in list(new_manifests.items()):
            manifest = RenderCacheManifest.from_dict(manifest_dict) if isinstance(manifest_dict, dict) else manifest_dict
            if (
                isinstance(manifest, RenderCacheManifest)
                and manifest.composite_id == composite_id
                and not (
                    manifest.status == RenderCacheStatus.VALID
                    and manifest.render_fingerprint
                )
            ):
                stale = RenderCacheManifest(
                    cache_id=manifest.cache_id,
                    timeline_id=manifest.timeline_id,
                    clip_id=manifest.clip_id,
                    composite_id=manifest.composite_id,
                    color_stack_revision=manifest.color_stack_revision,
                    composite_graph_revision=manifest.composite_graph_revision,
                    source_asset_revision=manifest.source_asset_revision,
                    effect_signature=manifest.effect_signature,
                    render_fingerprint=manifest.render_fingerprint,
                    timeline_fps=manifest.timeline_fps,
                    start_frame=manifest.start_frame,
                    duration_frames=manifest.duration_frames,
                    path=manifest.path,
                    codec=manifest.codec,
                    width=manifest.width,
                    height=manifest.height,
                    status=RenderCacheStatus.STALE,
                    container=manifest.container,
                    render_kind=manifest.render_kind,
                    scene_id=manifest.scene_id,
                )
                new_manifests[cache_id] = stale.to_dict()

        now = time.time()
        return replace(self, 
            render_cache_manifests=new_manifests,
            modified_at=now,
        )

    def invalidate_render_cache_for_asset(self, asset_id: str) -> EditProject:
        """Invalidate all caches for clips that reference the given asset."""
        from src.features.edit.core.render_cache import RenderCacheManifest

        affected_clips = set()
        for cc_dict in self.comp_clips.values():
            cc = cc_dict
            if isinstance(cc_dict, dict):
                from src.features.edit.core.comp_clip_data import CompClipData
                cc = CompClipData.from_dict(cc_dict)
            if hasattr(cc, 'source_asset_id') and cc.source_asset_id == asset_id:
                for m_dict in self.render_cache_manifests.values():
                    m = RenderCacheManifest.from_dict(m_dict) if isinstance(m_dict, dict) else m_dict
                    if isinstance(m, RenderCacheManifest):
                        affected_clips.add((m.timeline_id, m.clip_id))

        result = self
        for timeline_id, clip_id in affected_clips:
            result = result.invalidate_render_cache_for_clip(timeline_id, clip_id)
        return result

    def invalidate_render_cache_for_timeline(self, timeline_id: str) -> EditProject:
        """Invalidate all caches on the given timeline."""
        from src.features.edit.core.render_cache import RenderCacheManifest, RenderCacheStatus

        new_manifests = dict(self.render_cache_manifests)

        for cache_id, manifest_dict in list(new_manifests.items()):
            manifest = RenderCacheManifest.from_dict(manifest_dict) if isinstance(manifest_dict, dict) else manifest_dict
            if (
                isinstance(manifest, RenderCacheManifest)
                and manifest.timeline_id == timeline_id
                and not (
                    manifest.status == RenderCacheStatus.VALID
                    and manifest.render_fingerprint
                )
            ):
                stale = RenderCacheManifest(
                    cache_id=manifest.cache_id,
                    timeline_id=manifest.timeline_id,
                    clip_id=manifest.clip_id,
                    composite_id=manifest.composite_id,
                    color_stack_revision=manifest.color_stack_revision,
                    composite_graph_revision=manifest.composite_graph_revision,
                    source_asset_revision=manifest.source_asset_revision,
                    effect_signature=manifest.effect_signature,
                    render_fingerprint=manifest.render_fingerprint,
                    timeline_fps=manifest.timeline_fps,
                    start_frame=manifest.start_frame,
                    duration_frames=manifest.duration_frames,
                    path=manifest.path,
                    codec=manifest.codec,
                    width=manifest.width,
                    height=manifest.height,
                    status=RenderCacheStatus.STALE,
                    container=manifest.container,
                    render_kind=manifest.render_kind,
                    scene_id=manifest.scene_id,
                )
                new_manifests[cache_id] = stale.to_dict()

        now = time.time()
        return replace(self, 
            render_cache_manifests=new_manifests,
            modified_at=now,
        )

    def invalidate_render_cache_for_clip(
        self,
        timeline_id_or_clip_id: str,
        clip_id: str | None = None,
    ) -> EditProject:
        """Mark matching render-cache manifests STALE.

        FX-H02: effect mutations (add/remove/reorder/param/keyframe/toggle)
        must invalidate the affected clip's cache so the user never sees a
        stale pre-effect frame. The whole-timeline invalidator above is too
        coarse — it would throw away every clip's cache when one effect
        changes. This scoped helper flips only the matching clip's manifests
        to STALE.

        Args:
            timeline_id_or_clip_id: clip ID for the modern one-argument API,
                or timeline ID for the legacy two-argument API.
            clip_id: optional clip ID used with the legacy timeline-scoped API.

        Returns:
            New EditProject with the matching manifests marked STALE.
        """
        from src.features.edit.core.render_cache import RenderCacheManifest, RenderCacheStatus

        timeline_id = timeline_id_or_clip_id if clip_id is not None else None
        target_clip_id = clip_id or timeline_id_or_clip_id
        new_manifests = dict(self.render_cache_manifests)
        changed = False
        for cache_id, manifest_dict in list(new_manifests.items()):
            manifest = RenderCacheManifest.from_dict(manifest_dict) if isinstance(manifest_dict, dict) else manifest_dict
            if (isinstance(manifest, RenderCacheManifest)
                    and manifest.clip_id == target_clip_id
                    and (timeline_id is None or manifest.timeline_id == timeline_id)
                    and manifest.status == RenderCacheStatus.VALID
                    and not manifest.render_fingerprint):
                stale = RenderCacheManifest(
                    cache_id=manifest.cache_id,
                    timeline_id=manifest.timeline_id,
                    clip_id=manifest.clip_id,
                    composite_id=manifest.composite_id,
                    color_stack_revision=manifest.color_stack_revision,
                    composite_graph_revision=manifest.composite_graph_revision,
                    source_asset_revision=manifest.source_asset_revision,
                    effect_signature=manifest.effect_signature,
                    render_fingerprint=manifest.render_fingerprint,
                    timeline_fps=manifest.timeline_fps,
                    start_frame=manifest.start_frame,
                    duration_frames=manifest.duration_frames,
                    path=manifest.path,
                    codec=manifest.codec,
                    width=manifest.width,
                    height=manifest.height,
                    status=RenderCacheStatus.STALE,
                    container=manifest.container,
                    render_kind=manifest.render_kind,
                    scene_id=manifest.scene_id,
                )
                new_manifests[cache_id] = stale.to_dict()
                changed = True

        if not changed:
            return self  # no manifest matched — avoid a needless new project

        now = time.time()
        return replace(self, 
            render_cache_manifests=new_manifests,
            modified_at=now,
        )

    def get_render_cache_settings(self) -> Any:
        """Get render cache settings, creating defaults if needed."""
        from src.features.edit.core.render_cache import RenderCacheSettings
        if self.render_cache_settings is None:
            return RenderCacheSettings()
        if isinstance(self.render_cache_settings, dict):
            return RenderCacheSettings.from_dict(self.render_cache_settings)
        return self.render_cache_settings

    def get_proxy_policy(self) -> Any:
        """Get the project proxy policy, creating defaults if needed."""
        from src.features.edit.core.proxy_policy import ProxyPolicySettings
        if self.proxy_policy is None:
            return ProxyPolicySettings()
        if isinstance(self.proxy_policy, dict):
            return ProxyPolicySettings.from_dict(self.proxy_policy)
        return self.proxy_policy

    def with_proxy_policy(self, policy: Any) -> "EditProject":
        """Return a copy with the given proxy policy (P1-14)."""
        import time as _time
        return replace(self, proxy_policy=policy, modified_at=_time.time())

    # ==================== Comp Clip 管理 ====================

    def get_comp_clip_by_id(self, comp_clip_id: str) -> Any | None:
        """根据 ID 获取 Comp Clip 数据"""
        return self.comp_clips.get(comp_clip_id)

    def get_comp_clip_by_asset_id(self, asset_id: str) -> Any | None:
        """根据 asset_id 获取 Comp Clip 数据"""
        from src.features.edit.core.comp_clip_data import CompClipData
        for cc_dict in self.comp_clips.values():
            cc = CompClipData.from_dict(cc_dict) if isinstance(cc_dict, dict) else cc_dict
            if isinstance(cc, CompClipData) and cc.asset_id == asset_id:
                return cc
        return None

    def get_comp_clip_by_graph_id(self, graph_id: str) -> Any | None:
        """根据 graph_id 获取 Comp Clip 数据"""
        from src.features.edit.core.comp_clip_data import CompClipData
        for cc_dict in self.comp_clips.values():
            cc = CompClipData.from_dict(cc_dict) if isinstance(cc_dict, dict) else cc_dict
            if isinstance(cc, CompClipData) and cc.graph_id == graph_id:
                return cc
        return None

    # ==================== 生成器（Generators）工厂 ====================
    #
    # 调节片段（Adjustment Clip）和纯色片段（Solid Color）是无磁盘文件
    # 的虚拟素材 —— 它们复用 MediaAsset 的虚拟素材机制（与 comp_clip /
    # nested_sequence 一致：path 为 topos:// scheme，__post_init__ 允许空
    # path）。生成器只产出一个 MediaAsset，不需要 graph/comp_clip 字典；
    # 其"内容"完全由渲染层程序化生成。
    # =================================================================

    def create_motion_graphic_asset(
        self,
        scene: Any,
        name: str | None = None,
    ) -> tuple["EditProject", str]:
        """Register a Teaching Scene as a virtual Media Pool asset.

        Motion Graphic clips are authored scene documents, not prematurely
        rendered video files.  The asset therefore keeps a stable reference
        to the scene and its render identity in metadata while the timeline
        and preview renderer remain free to render/cache frames on demand.

        The stable id intentionally uses the legacy ``teaching_animation:``
        prefix.  Existing timeline clips, project payloads and integrations
        already understand that reference, so introducing the MediaAsset does
        not invalidate older projects.
        """
        scene_id = str(getattr(scene, "scene_id", "") or "")
        if not scene_id:
            raise ValueError("scene.scene_id cannot be empty")
        fps = _fps_to_float(getattr(scene, "fps", 25.0), default=25.0)
        duration_frames = max(1, int(getattr(scene, "duration_frames", 1) or 1))
        width = max(1, int(getattr(scene, "width", 1920) or 1920))
        height = max(1, int(getattr(scene, "height", 1080) or 1080))
        duration_seconds = duration_frames / fps
        asset_id = f"teaching_animation:{scene_id}"
        existing = self.assets.get(asset_id)
        display_name = name or (
            str(getattr(scene, "name", "") or "").strip()
            or "Motion Graphic Clip"
        )
        # Persist the page count alongside the virtual asset.  The scene store
        # is normally available to the editor, but projects opened offline
        # still need the 0.5s-per-page minimum for Shift retiming.
        page_count = 1
        html_layer = getattr(scene, "html_layer", None)
        layer_metadata = getattr(html_layer, "metadata", {}) or {}
        try:
            page_count = max(1, int(layer_metadata.get("page_count", 1) or 1))
        except (TypeError, ValueError):
            page_count = 1
        if page_count == 1 and html_layer is not None:
            try:
                from src.features.teaching_animation.core.html_page_deck import detect_page_count
                page_count = max(1, int(detect_page_count(getattr(html_layer, "sanitized_html", ""))))
            except Exception:  # pragma: no cover - optional HTML layer
                page_count = 1
        metadata = {
            "source_kind": "ai_motion_graphic",
            "scene_id": scene_id,
            "scene_revision": int(getattr(scene, "revision", 0) or 0),
            "scene_schema_version": int(getattr(scene, "schema_version", 1) or 1),
            "width": width,
            "height": height,
            "fps": fps,
            "duration_frames": duration_frames,
            "duration_seconds": duration_seconds,
            "page_count": page_count,
            "render_mode": "on_demand_cache",
            "render_cache_id": f"scene:{scene_id}",
        }
        asset = MediaAsset(
            asset_id=asset_id,
            path=f"topos://motion_graphic/{scene_id}",
            media_type="motion_graphic",
            display_name=(existing.display_name if existing and not name else display_name),
            metadata=metadata,
            thumbnail_key=f"motion_graphic:{scene_id}",
            created_at=existing.created_at if existing else time.time(),
        )
        new_assets = dict(self.assets)
        new_assets[asset_id] = asset

        # Keep the asset visible in the root bin, without duplicating an
        # existing reference when a scene is regenerated or edited.
        new_bins = dict(self.bins)
        root_bin = self.bins.get(self.root_bin_id)
        if root_bin is not None:
            new_bins[self.root_bin_id] = root_bin.with_asset_added(asset_id)

        return replace(
            self,
            assets=new_assets,
            bins=new_bins,
            modified_at=time.time(),
        ), asset_id

    def create_adjustment_clip(self, name: str = "Adjustment Clip") -> tuple["EditProject", str]:
        """创建一个调节片段（Adjustment Clip）虚拟素材。

        调节片段本身无画面，其效果链（挂在 clip.effects 上）会作用于
        时间线上该片段下方所有视频轨道在此时段的合成画面 —— 与 DaVinci
        Resolve 的 Adjustment Clip 语义一致。

        Args:
            name: 素材显示名

        Returns:
            (new EditProject, adjustment_asset_id) — asset_id 用于构造
            ``ClipInstance(asset_id=...)`` 落到时间线上。
        """
        now = time.time()
        asset_id = str(uuid.uuid4())
        asset = MediaAsset(
            asset_id=asset_id,
            path=f"topos://adjustment/{asset_id}",
            media_type="adjustment",
            display_name=name,
            metadata={
                "source_kind": "adjustment",
            },
            created_at=now,
        )
        new_assets = dict(self.assets)
        new_assets[asset_id] = asset

        # 加入 root bin，让素材库可见（与 comp_clip 一致）
        new_bins = dict(self.bins)
        root_bin = self.bins.get(self.root_bin_id)
        if root_bin is not None:
            new_bins[self.root_bin_id] = root_bin.with_asset_added(asset_id)

        return replace(self, 
            assets=new_assets,
            bins=new_bins,
            modified_at=now,
        ), asset_id

    def _next_solid_color_name(self) -> str:
        """Generate the next available 'Solid Color NN' display name."""
        import re
        max_num = 0
        for asset in self.assets.values():
            if getattr(asset, "media_type", None) != "solid":
                continue
            dn = getattr(asset, "display_name", "") or ""
            m = re.match(r"Solid Color\s+(\d+)", dn)
            if m:
                max_num = max(max_num, int(m.group(1)))
        return f"Solid Color {max_num + 1:02d}"

    def create_solid_color_asset(
        self,
        name: Optional[str] = None,
        color_rgb: tuple[float, float, float] = (0.0, 0.6, 0.8),
    ) -> tuple["EditProject", str]:
        """创建一个纯色片段（Solid Color）虚拟素材。

        纯色片段生成一张指定颜色的画面，作为普通视频层参与合成（支持
        opacity、可被调节片段作用）。

        Args:
            name: 素材显示名（None 时自动编号，如 "Solid Color 01"）
            color_rgb: RGB 三元组（0.0~1.0）

        Returns:
            (new EditProject, solid_asset_id)
        """
        if name is None:
            name = self._next_solid_color_name()
        now = time.time()
        asset_id = str(uuid.uuid4())
        asset = MediaAsset(
            asset_id=asset_id,
            path=f"topos://solid/{asset_id}",
            media_type="solid",
            display_name=name,
            metadata={
                "source_kind": "solid",
                "color": [float(color_rgb[0]), float(color_rgb[1]), float(color_rgb[2])],
            },
            created_at=now,
        )
        new_assets = dict(self.assets)
        new_assets[asset_id] = asset

        new_bins = dict(self.bins)
        root_bin = self.bins.get(self.root_bin_id)
        if root_bin is not None:
            new_bins[self.root_bin_id] = root_bin.with_asset_added(asset_id)

        return replace(self, 
            assets=new_assets,
            bins=new_bins,
            modified_at=now,
        ), asset_id

    def create_empty_comp_clip(
        self,
        name: str = "Comp Clip",
        width: int = 1920,
        height: int = 1080,
        fps: float = 24.0,
        duration_frames: int = 240,
        background_color: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0),
    ) -> tuple[EditProject, str, str, str]:
        """
        创建空白 Comp Clip。

        同时创建：MediaAsset(comp_clip) + CompClipData + CompositeGraphData

        Args:
            name: Comp Clip 名称
            width: 输出宽度
            height: 输出高度
            fps: 帧率
            duration_frames: 时长帧数
            background_color: Constant 节点默认背景色 (R, G, B, A)

        Returns:
            Tuple of (new EditProject, comp_clip_id, comp_asset_id, graph_id)
        """
        from src.features.edit.core.comp_clip_data import CompClipData
        from src.features.edit.core.composite_graph_data import CompositeGraphData

        now = time.time()
        comp_clip_id = str(uuid.uuid4())
        comp_asset_id = str(uuid.uuid4())
        graph_id = str(uuid.uuid4())
        fps_value = _fps_to_float(fps)

        # 创建 MediaAsset
        comp_asset = MediaAsset(
            asset_id=comp_asset_id,
            path=f"topos://comp_clip/{comp_asset_id}",
            media_type="comp_clip",
            display_name=name,
            metadata={
                "comp_clip_id": comp_clip_id,
                "graph_id": graph_id,
                "width": width,
                "height": height,
                "fps": fps_value,
                "duration_frames": duration_frames,
                # MediaAsset.duration_seconds/total_frames 属性读这两个键
                # （timeline_view._get_asset_duration_frames 依赖它们决定
                # clip 时长）。此前只写 duration_frames，属性读到 None，
                # comp clip 拖上时间线时长度回退到默认 250 帧（10 秒）
                # ——用户设置的时长被静默丢弃。
                "total_frames": duration_frames,
                "duration_seconds": duration_frames / fps_value if fps_value else None,
                "source_kind": "empty_comp_clip",
            },
            created_at=now,
        )

        # 创建 CompClipData
        comp_clip = CompClipData.create_empty(
            asset_id=comp_asset_id,
            graph_id=graph_id,
            name=name,
            width=width,
            height=height,
            fps=fps_value,
            duration_frames=duration_frames,
        )

        # 创建默认 CompositeGraphData
        comp_graph = CompositeGraphData.create_default_empty_comp_clip(
            composite_id=graph_id,
            comp_clip_id=comp_clip_id,
            comp_asset_id=comp_asset_id,
            name=name,
            width=width,
            height=height,
            fps=fps_value,
            duration_frames=duration_frames,
            background_color=background_color,
        )

        # 更新所有字典
        new_assets = dict(self.assets)
        new_assets[comp_asset_id] = comp_asset

        new_compositions = dict(self.compositions)
        new_compositions[graph_id] = comp_graph.to_dict()

        new_comp_clips = dict(self.comp_clips)
        new_comp_clips[comp_clip_id] = comp_clip.to_dict()

        # 添加到 root bin
        root_bin = self.bins.get(self.root_bin_id)
        new_bins = dict(self.bins)
        if root_bin is not None:
            new_bins[self.root_bin_id] = root_bin.with_asset_added(comp_asset_id)

        return replace(self, 
            assets=new_assets,
            bins=new_bins,
            compositions=new_compositions,
            comp_clips=new_comp_clips,
            active_comp_clip_id=comp_clip_id,
            modified_at=now,
        ), comp_clip_id, comp_asset_id, graph_id

    def next_comp_clip_name(self) -> str:
        """Return the next user-facing name for a timeline-created Comp Clip.

        Comp Clips are project objects, so their names must be allocated from
        the project snapshot rather than from a UI counter.  This keeps the
        numbering stable across page switches and avoids duplicate names after
        save/load or undo/redo.
        """
        existing_names: set[str] = set()

        for value in self.comp_clips.values():
            name = getattr(value, "name", None)
            if name is None and isinstance(value, dict):
                name = value.get("name")
            if name:
                existing_names.add(str(name).strip())

        # Include media assets and graph names as a compatibility fallback for
        # projects written by older versions where comp_clips was incomplete.
        for asset in self.assets.values():
            if getattr(asset, "media_type", "") == "comp_clip":
                name = str(getattr(asset, "display_name", "") or "").strip()
                if name:
                    existing_names.add(name)
        for value in self.compositions.values():
            name = getattr(value, "name", None)
            if name is None and isinstance(value, dict):
                name = value.get("name")
            if name:
                existing_names.add(str(name).strip())

        index = 1
        while f"Comp Clip {index}" in existing_names:
            index += 1
        return f"Comp Clip {index}"

    def create_comp_clip_from_timeline_clip(
        self,
        timeline_id: str,
        clip_id: str,
        timeline=None,
        name: str | None = None,
    ) -> tuple[EditProject, str, str, str]:
        """
        Explicitly create a Comp Clip from a timeline clip (Phase 2).

        Creates: MediaAsset(comp_clip) + CompClipData + CompositeGraphData.
        Uses real metadata from the timeline clip and source asset.

        Args:
            timeline_id: Timeline ID
            clip_id: Clip ID to upgrade
            timeline: TimelineSequence for resolving real clip metadata
            name: Comp Clip name (defaults to source asset name)

        Returns:
            Tuple of (new EditProject, comp_clip_id, comp_asset_id, graph_id)
        """
        from src.features.edit.core.comp_clip_data import CompClipData
        from src.features.edit.core.composite_graph_data import CompositeGraphData

        now = time.time()
        comp_clip_id = str(uuid.uuid4())
        comp_asset_id = str(uuid.uuid4())
        graph_id = str(uuid.uuid4())

        # Resolve real clip metadata from timeline
        original_asset_id = ""
        original_asset_path = ""
        clip_duration = 240
        width = 1920
        height = 1080
        fps = 24.0
        color_stack_id = ""

        resolved_clip = None
        if timeline is not None:
            fps = _fps_to_float(getattr(timeline, 'fps', 24.0))
            if hasattr(timeline, 'width') and timeline.width:
                width = timeline.width
                height = timeline.height

            for t in timeline.all_tracks:
                c = t.get_clip_by_id(clip_id)
                if c:
                    resolved_clip = c
                    original_asset_id = getattr(c, 'asset_id', '') or ""
                    clip_duration = getattr(c, 'timeline_duration', 240) or 240
                    color_stack_id = getattr(c, 'color_stack_id', '') or ""
                    break

            # Get source asset metadata for resolution
            if original_asset_id:
                source_asset = self.assets.get(original_asset_id)
                if source_asset:
                    original_asset_path = source_asset.path or ""
                    if not (hasattr(timeline, 'width') and timeline.width):
                        if source_asset.width:
                            width = source_asset.width
                        if source_asset.height:
                            height = source_asset.height
                    # Do not derive the timeline label from the source file.
                    # When no explicit name is supplied, the project-level
                    # Comp Clip allocator below produces ``Comp Clip N``.

        if timeline is not None and resolved_clip is None:
            raise ValueError(f"Cannot create Comp Clip: clip not found in timeline: {clip_id}")

        if not original_asset_id:
            raise ValueError(f"Cannot create Comp Clip: source asset unresolved for clip: {clip_id}")

        # 创建 MediaAsset
        display_name = name or self.next_comp_clip_name()
        comp_asset = MediaAsset(
            asset_id=comp_asset_id,
            path=f"topos://comp_clip/{comp_asset_id}",
            media_type="comp_clip",
            display_name=display_name,
            metadata={
                "comp_clip_id": comp_clip_id,
                "graph_id": graph_id,
                "width": width,
                "height": height,
                "fps": fps,
                "duration_frames": clip_duration,
                "source_kind": "timeline_clip_upgrade",
                "original_asset_id": original_asset_id,
                "original_asset_path": original_asset_path,
                "original_timeline_id": timeline_id,
                "original_clip_id": clip_id,
            },
            created_at=now,
        )

        # 创建 CompClipData
        comp_clip = CompClipData.create_from_timeline_clip(
            asset_id=comp_asset_id,
            graph_id=graph_id,
            name=display_name,
            width=width,
            height=height,
            fps=fps,
            duration_frames=clip_duration,
            original_asset_id=original_asset_id,
            original_timeline_id=timeline_id,
            original_clip_id=clip_id,
        )

        # 创建默认 CompositeGraphData
        comp_graph = CompositeGraphData.create_default_for_clip(
            clip_id=clip_id,
            asset_id=original_asset_id,
            asset_path=original_asset_path,
            width=width,
            height=height,
            name=display_name,
            composite_id=graph_id,
            comp_clip_id=comp_clip_id,
            comp_asset_id=comp_asset_id,
            timeline_id=timeline_id,
            fps=fps,
            duration_frames=clip_duration,
            color_stack_id=color_stack_id,
        )

        # 更新所有字典
        new_assets = dict(self.assets)
        new_assets[comp_asset_id] = comp_asset

        new_compositions = dict(self.compositions)
        new_compositions[graph_id] = comp_graph.to_dict()

        new_comp_clips = dict(self.comp_clips)
        new_comp_clips[comp_clip_id] = comp_clip.to_dict()

        # 添加到 root bin
        root_bin = self.bins.get(self.root_bin_id)
        new_bins = dict(self.bins)
        if root_bin is not None:
            new_bins[self.root_bin_id] = root_bin.with_asset_added(comp_asset_id)

        return replace(self, 
            assets=new_assets,
            bins=new_bins,
            compositions=new_compositions,
            comp_clips=new_comp_clips,
            active_comp_clip_id=comp_clip_id,
            modified_at=now,
        ), comp_clip_id, comp_asset_id, graph_id

    def _legacy_promote_composition_to_comp_clip(
        self,
        composite_id: str,
        timeline_id: str,
        clip_id: str,
        name: str | None = None,
        original_asset_id: str | None = None,
        original_asset_path: str | None = None,
        width: int | None = None,
        height: int | None = None,
        fps: float | None = None,
        duration_frames: int | None = None,
    ) -> tuple[EditProject, str, str]:
        """
        DEPRECATED: Legacy temporary composition promotion.

        DO NOT CALL FROM UI / PRODUCTION CODE.
        This method is retained ONLY for backward compatibility with existing tests.

        Use create_comp_clip_from_timeline_clip() for explicit Comp Clip creation.
            name: Optional display name for the Comp Clip
            original_asset_id: Real source asset ID from clip.asset_id
            original_asset_path: Real source asset path (optional)
            width: Output width (defaults to composition or 1920)
            height: Output height (defaults to composition or 1080)
            fps: Frame rate (defaults to 24.0)
            duration_frames: Duration in frames (defaults to 240)

        Returns:
            Tuple of (new EditProject, comp_clip_id, comp_asset_id)

        Raises:
            ValueError: If the composition does not exist
        """
        from src.features.edit.core.comp_clip_data import CompClipData
        from src.features.edit.core.composite_graph_data import CompositeGraphData

        # (a) Find the composition by composite_id -- must exist
        if composite_id not in self.compositions:
            raise ValueError(f"Composition not found: {composite_id}")

        comp_dict = self.compositions[composite_id]
        comp_data = CompositeGraphData.from_dict(comp_dict) if isinstance(comp_dict, dict) else comp_dict

        # (d) Read real original_asset_id (prefer argument, then existing data)
        real_asset_id = original_asset_id or comp_data.source_asset_id or ""

        # (e) Read real duration
        real_duration = duration_frames if duration_frames is not None else comp_data.duration_frames or 240

        # (f) Read resolution/fps
        res_w = width or comp_data.width or 1920
        res_h = height or comp_data.height or 1080
        real_fps = _fps_to_float(fps if fps is not None else comp_data.fps or 24.0)

        now = time.time()
        comp_clip_id = str(uuid.uuid4())
        comp_asset_id = str(uuid.uuid4())

        # (j) Keep existing graph_id (= composite_id) -- do NOT create a new one
        graph_id = composite_id

        display_name = name or comp_data.name or "Comp Clip"

        # (g) Create a new MediaAsset(media_type="comp_clip")
        comp_asset = MediaAsset(
            asset_id=comp_asset_id,
            path=f"topos://comp_clip/{comp_asset_id}",
            media_type="comp_clip",
            display_name=display_name,
            metadata={
                "comp_clip_id": comp_clip_id,
                "graph_id": graph_id,
                "width": res_w,
                "height": res_h,
                "fps": real_fps,
                "duration_frames": real_duration,
                "source_kind": "timeline_clip_upgrade",
                "original_asset_id": real_asset_id,
                "original_timeline_id": timeline_id,
                "original_clip_id": clip_id,
            },
            created_at=now,
        )

        # (h) Create CompClipData linking to the composition
        comp_clip = CompClipData.create_from_timeline_clip(
            asset_id=comp_asset_id,
            graph_id=graph_id,
            name=display_name,
            width=res_w,
            height=res_h,
            fps=real_fps,
            duration_frames=real_duration,
            original_asset_id=real_asset_id,
            original_timeline_id=timeline_id,
            original_clip_id=clip_id,
        )

        # (i) Update the CompositeGraphData with comp_clip_id, comp_asset_id,
        #     source_kind="timeline_clip_upgrade", source_refs
        #     Keep existing graph_id and nodes/connections unchanged.
        promoted_comp = CompositeGraphData(
            composite_id=graph_id,
            source_clip_id=clip_id,
            source_asset_id=real_asset_id,
            source_asset_path=original_asset_path or comp_data.source_asset_path or "",
            name=display_name,
            nodes=comp_data.nodes,
            connections=comp_data.connections,
            viewer_state=comp_data.viewer_state,
            viewport_state=comp_data.viewport_state,
            backdrops=comp_data.backdrops,
            sticky_notes=comp_data.sticky_notes,
            groups=comp_data.groups,
            created_at=comp_data.created_at,
            modified_at=now,
            width=res_w,
            height=res_h,
            comp_clip_id=comp_clip_id,
            comp_asset_id=comp_asset_id,
            source_kind="timeline_clip_upgrade",
            timeline_id=timeline_id,
            clip_id=clip_id,
            source_refs={
                "original_asset_id": real_asset_id,
                "original_clip_id": clip_id,
                "original_timeline_id": timeline_id,
            },
            fps=real_fps,
            duration_frames=real_duration,
        )

        # Update all dictionaries
        new_assets = dict(self.assets)
        new_assets[comp_asset_id] = comp_asset

        new_compositions = dict(self.compositions)
        new_compositions[graph_id] = promoted_comp.to_dict()

        new_comp_clips = dict(self.comp_clips)
        new_comp_clips[comp_clip_id] = comp_clip.to_dict()

        # Add to root bin
        root_bin = self.bins.get(self.root_bin_id)
        new_bins = dict(self.bins)
        if root_bin is not None:
            new_bins[self.root_bin_id] = root_bin.with_asset_added(comp_asset_id)

        # (k) Return (new_project, comp_clip_id, comp_asset_id)
        return replace(self, 
            assets=new_assets,
            bins=new_bins,
            compositions=new_compositions,
            comp_clips=new_comp_clips,
            active_comp_clip_id=comp_clip_id,
            modified_at=now,
        ), comp_clip_id, comp_asset_id

    def update_comp_clip(
        self,
        comp_clip_id: str,
        name: str | None = None,
        width: int | None = None,
        height: int | None = None,
        fps: float | None = None,
        duration_frames: int | None = None,
        background_color: tuple[float, float, float, float] | None = None,
    ) -> EditProject:
        """更新 Comp Clip 数据。

        同步更新 CompClipData、关联 MediaAsset metadata 以及
        CompositeGraphData（图级分辨率 + 默认 Constant 节点的
        width/height/color_* 参数；不影响用户已添加的其它节点）。

        Args:
            comp_clip_id: Comp Clip ID
            name: 新名称（可选）
            width: 新宽度（可选）
            height: 新高度（可选）
            fps: 新帧率（可选）
            duration_frames: 新时长帧数（可选）
            background_color: 新背景色 (R, G, B, A)（可选）

        Returns:
            新的 EditProject 实例
        """
        import dataclasses as _dc
        from src.features.edit.core.comp_clip_data import CompClipData
        from src.features.edit.core.composite_graph_data import (
            PROJECT_BACKGROUND_PARAM,
            CONSTANT_COLOR_SPACE_PARAM,
        )

        if comp_clip_id not in self.comp_clips:
            logger.warning(f"Comp clip not found: {comp_clip_id}")
            return self

        cc_dict = self.comp_clips[comp_clip_id]
        cc = CompClipData.from_dict(cc_dict) if isinstance(cc_dict, dict) else cc_dict

        # 1. 更新 CompClipData（frozen，用 with_/replace 重建）
        if duration_frames is not None:
            cc = cc.with_duration(duration_frames)
        # CompClipData 没有 with_width/height/fps，用 replace 批量替换
        replace_fields: dict[str, Any] = {}
        if name is not None:
            replace_fields["name"] = name
        if width is not None:
            replace_fields["width"] = width
        if height is not None:
            replace_fields["height"] = height
        if fps is not None:
            replace_fields["fps"] = _fps_to_float(fps)
        if replace_fields:
            cc = _dc.replace(cc, **replace_fields)

        new_comp_clips = dict(self.comp_clips)
        new_comp_clips[comp_clip_id] = cc.to_dict() if hasattr(cc, 'to_dict') else cc

        # 2. 更新关联 MediaAsset metadata
        new_assets = dict(self.assets)
        comp_asset_id = cc.asset_id
        if comp_asset_id and comp_asset_id in new_assets:
            asset = new_assets[comp_asset_id]
            new_meta = dict(asset.metadata)
            if name is not None:
                new_meta["display_name"] = name
            if width is not None:
                new_meta["width"] = width
            if height is not None:
                new_meta["height"] = height
            if fps is not None:
                new_meta["fps"] = _fps_to_float(fps)
            if duration_frames is not None:
                new_meta["duration_frames"] = duration_frames
            new_assets[comp_asset_id] = _dc.replace(
                asset,
                metadata=new_meta,
                display_name=name if name is not None else asset.display_name,
            )

        # 3. 更新 CompositeGraphData（图级分辨率 + Constant 节点参数）
        new_compositions = dict(self.compositions)
        graph_id = cc.graph_id
        if graph_id and graph_id in new_compositions:
            comp_dict = dict(new_compositions[graph_id])
            if width is not None:
                comp_dict["width"] = width
            if height is not None:
                comp_dict["height"] = height
            if name is not None:
                comp_dict["name"] = name

            # 同步更新背景 Constant 节点的 width/height/color_*（若存在）
            # 以及 Output 节点的 format_mode/width/height（分辨率权威源）。
            # 新图用私有标记区分背景 Constant；旧空白 Comp Clip 没有标记
            # 时回退到第一个 Constant，避免把用户新增的 Constant 一并改色。
            nodes = list(comp_dict.get("nodes", []))
            constant_nodes = [
                node for node in nodes if node.get("type") == "constant"
            ]
            background_nodes = [
                node for node in constant_nodes
                if bool(node.get("parameters", {}).get(
                    PROJECT_BACKGROUND_PARAM, False))
            ]
            if not background_nodes and comp_dict.get(
                    "source_kind") == "empty_comp_clip":
                background_nodes = constant_nodes[:1]
            background_node_ids = {
                node.get("id") or node.get("node_id")
                for node in background_nodes
            }
            # 快照不可变隔离：节点 dict 可能与旧工程快照（undo 历史）共享，
            # 修改前必须逐节点 copy-on-write 重建，禁止原地改写共享 dict。
            new_nodes: list[Any] = []
            for node in nodes:
                if node.get("type") == "constant":
                    params = dict(node.get("parameters", {}))
                    if width is not None:
                        params["width"] = width
                    if height is not None:
                        params["height"] = height
                    node_id = node.get("id") or node.get("node_id")
                    if (background_color is not None
                            and (not background_node_ids
                                 or node_id in background_node_ids)):
                        bg_r, bg_g, bg_b, bg_a = background_color
                        params["color_r"] = float(bg_r)
                        params["color_g"] = float(bg_g)
                        params["color_b"] = float(bg_b)
                        params["color_a"] = float(bg_a)
                        params[CONSTANT_COLOR_SPACE_PARAM] = "linear"
                    node = {**node, "parameters": params}
                elif node.get("type") == "output":
                    if width is not None or height is not None:
                        # Output node owns the authoritative resolution.
                        params = dict(node.get("parameters", {}))
                        params["format_mode"] = "custom"
                        if width is not None:
                            params["width"] = width
                        if height is not None:
                            params["height"] = height
                        node = {**node, "parameters": params}
                new_nodes.append(node)
            comp_dict["nodes"] = new_nodes
            comp_dict["modified_at"] = time.time()
            new_compositions[graph_id] = comp_dict

        now = time.time()
        return replace(self, 
            assets=new_assets,
            compositions=new_compositions,
            comp_clips=new_comp_clips,
            modified_at=now,
        )

    def remove_comp_clip(self, comp_clip_id: str) -> EditProject:
        """
        移除 Comp Clip。

        同时删除 MediaAsset、CompClipData、CompositeGraphData。
        注意：如果 Comp Clip 正被时间线使用，应先检查。

        Args:
            comp_clip_id: 要移除的 Comp Clip ID
        """
        from src.features.edit.core.comp_clip_data import CompClipData

        if comp_clip_id not in self.comp_clips:
            return self

        cc_dict = self.comp_clips[comp_clip_id]
        cc = CompClipData.from_dict(cc_dict) if isinstance(cc_dict, dict) else cc_dict

        # 删除关联数据
        new_assets = dict(self.assets)
        new_compositions = dict(self.compositions)
        new_comp_clips = dict(self.comp_clips)
        new_bins = dict(self.bins)

        if isinstance(cc, CompClipData):
            # 删除 MediaAsset
            new_assets.pop(cc.asset_id, None)
            # 删除 CompositeGraphData
            new_compositions.pop(cc.graph_id, None)
            # 从 bins 中移除
            for bin_id, bin_obj in self.bins.items():
                if cc.asset_id in bin_obj.asset_ids:
                    new_bins[bin_id] = bin_obj.with_asset_removed(cc.asset_id)

        del new_comp_clips[comp_clip_id]

        now = time.time()
        return replace(self, 
            assets=new_assets,
            bins=new_bins,
            compositions=new_compositions,
            comp_clips=new_comp_clips,
            active_comp_clip_id=self.active_comp_clip_id if self.active_comp_clip_id != comp_clip_id else None,
            modified_at=now,
        )

    @property
    def comp_clip_count(self) -> int:
        """Comp Clip 数量"""
        return len(self.comp_clips)

    # ==================== Comp Clip Reset Methods (Phase 6) ====================

    def reset_clip_composite(
        self,
        timeline_id: str,
        clip_id: str,
        timeline: 'TimelineSequence | None' = None,
    ) -> 'EditProject | tuple[EditProject, TimelineSequence]':
        """
        Reset Clip Composite (Phase 6): Restore a timeline-upgraded Comp Clip.

        For timeline clips that were upgraded to Comp Clips, this:
        1. Restores clip.asset_id to original_asset_id
        2. Sets clip.composite_id = None
        3. Removes Comp Clip asset/media if no other references

        Args:
            timeline_id: Timeline ID containing the clip
            clip_id: Clip ID to reset
            timeline: TimelineSequence containing the clip (required, as project
                      stores TimelineSequenceRef not full TimelineSequence)

        Returns:
            早退路径（timeline 为 None / clip 不存在 / 无 original_asset_id）
            返回 ``self``（原实例不变）；成功路径返回
            ``(新 EditProject, 更新后的 TimelineSequence)`` 元组。
            调用方必须先判别返回形状再使用（tests/edit/test_comp_clip_reset.py
            锁定了该双形状契约，AUD-17105 注解据此如实标注）。
        """
        from src.features.edit.core.comp_clip_data import CompClipData

        if timeline is None:
            return self

        clip = timeline.get_clip_by_id(clip_id)
        if clip is None:
            return self

        # Find the CompClipData associated with this clip's composite_id
        comp_clip_to_remove = None
        comp_clip_dict_key = None  # Key in comp_clips dict (may differ from CompClipData.comp_clip_id)
        original_asset_id = None

        if clip.composite_id:
            # Find CompClipData by graph_id
            for cc_id, cc_dict in self.comp_clips.items():
                cc = CompClipData.from_dict(cc_dict) if isinstance(cc_dict, dict) else cc_dict
                if isinstance(cc, CompClipData) and cc.graph_id == clip.composite_id:
                    comp_clip_to_remove = cc
                    comp_clip_dict_key = cc_id
                    original_asset_id = cc.original_asset_id
                    break

        # Build new clip with restored asset_id
        if not original_asset_id:
            return self

        from src.features.edit.core.timeline_v2 import ClipInstance
        clip_dict = {
            'clip_id': clip.clip_id,
            'asset_id': original_asset_id,
            'track_id': clip.track_id,
            'timeline_start': clip.timeline_start,
            'timeline_duration': clip.timeline_duration,
            'source_start': clip.source_start,
            'source_duration': clip.source_duration,
            'speed': clip.speed,
            'linked_clip_id': clip.linked_clip_id,
            'transform': clip.transform,
            'crop': clip.crop,
            'opacity': clip.opacity,
            'animated_params': clip.animated_params,
            'effects': clip.effects,
            'is_enabled': clip.is_enabled,
            'fade_in_duration': clip.fade_in_duration,
            'fade_out_duration': clip.fade_out_duration,
            'color_stack_id': clip.color_stack_id,
            'color_shared': clip.color_shared,
            'composite_id': None,  # Clear composite reference
        }
        restored_clip = ClipInstance(**clip_dict)

        # Remove Comp Clip if no other references
        new_comp_clips = dict(self.comp_clips)
        new_compositions = dict(self.compositions)
        new_assets = dict(self.assets)
        new_bins = dict(self.bins)

        if comp_clip_to_remove is not None:
            # Check if any other clips reference this comp clip's asset
            other_refs = False
            for t in timeline.all_tracks:
                for c in t.clips:
                    if c.clip_id != clip_id and c.asset_id == comp_clip_to_remove.asset_id:
                        other_refs = True
                        break

            if not other_refs:
                # Safe to remove the Comp Clip entirely
                new_comp_clips.pop(comp_clip_dict_key, None)
                new_compositions.pop(comp_clip_to_remove.graph_id, None)
                new_assets.pop(comp_clip_to_remove.asset_id, None)
                # Remove from bins
                for bin_id, bin_obj in self.bins.items():
                    if comp_clip_to_remove.asset_id in bin_obj.asset_ids:
                        new_bins[bin_id] = bin_obj.with_asset_removed(comp_clip_to_remove.asset_id)

        now = time.time()
        return replace(self, 
            assets=new_assets,
            bins=new_bins,
            compositions=new_compositions,
            comp_clips=new_comp_clips,
            modified_at=now,
        ), timeline.with_clip_updated(restored_clip)

    def reset_comp_clip_graph(
        self,
        comp_clip_id: str,
    ) -> EditProject:
        """
        Reset Comp Clip Graph (Phase 6): Reset graph to default.

        For media pool Comp Clips, this:
        1. Does NOT delete the MediaAsset
        2. Resets graph to default: TimelineClipInput -> Output (for timeline source)
           or Constant -> Output (for empty)
        3. Clears render cache

        Args:
            comp_clip_id: CompClipData ID to reset

        Returns:
            New EditProject with reset graph
        """
        from src.features.edit.core.comp_clip_data import CompClipData
        from src.features.edit.core.composite_graph_data import CompositeGraphData

        if comp_clip_id not in self.comp_clips:
            return self

        cc_dict = self.comp_clips[comp_clip_id]
        cc = CompClipData.from_dict(cc_dict) if isinstance(cc_dict, dict) else cc_dict

        if not isinstance(cc, CompClipData):
            return self

        # Reset the graph based on source kind
        if cc.is_from_timeline_clip and cc.original_clip_id:
            # Phase 4: TimelineClipInput -> Topos Color -> Output
            # Try to find the color_stack_id from the timeline clip
            reset_color_stack_id = ""
            reset_timeline_id = cc.original_timeline_id or ""
            if reset_timeline_id:
                timeline = self.timelines.get(reset_timeline_id)
                if timeline:
                    for track in timeline.all_tracks:
                        clip = track.get_clip_by_id(cc.original_clip_id)
                        if clip:
                            reset_color_stack_id = getattr(clip, 'color_stack_id', '') or ""
                            break

            new_graph = CompositeGraphData.create_default_for_clip(
                clip_id=cc.original_clip_id,
                asset_id=cc.original_asset_id or "",
                asset_path="",
                width=cc.width,
                height=cc.height,
                name=cc.name,
                composite_id=cc.graph_id,
                comp_clip_id=cc.comp_clip_id,
                comp_asset_id=cc.asset_id,
                timeline_id=reset_timeline_id,
                fps=cc.fps,
                duration_frames=cc.duration_frames,
                color_stack_id=reset_color_stack_id,
            )
        else:
            # Constant -> Output
            new_graph = CompositeGraphData.create_default_empty_comp_clip(
                composite_id=cc.graph_id,
                comp_clip_id=cc.comp_clip_id,
                comp_asset_id=cc.asset_id,
                name=cc.name,
                width=cc.width,
                height=cc.height,
                fps=cc.fps,
                duration_frames=cc.duration_frames,
            )

        # Update compositions
        new_compositions = dict(self.compositions)
        new_compositions[cc.graph_id] = new_graph.to_dict()

        now = time.time()
        return replace(self, 
            compositions=new_compositions,
            modified_at=now,
        )

    # ==================== ProjectDocument Protocol ====================

    @property
    def document_type(self) -> str:
        """ProjectDocument protocol: document type identifier."""
        return "edit_project"

    @property
    def dirty(self) -> bool:
        """ProjectDocument protocol: whether the document has unsaved changes.

        Since EditProject is immutable, dirty is tracked via modified_at vs created_at.
        A project is considered dirty if it was modified after creation.
        """
        return self.modified_at > self.created_at

    # ==================== 项目属性 ====================

    @property
    def asset_count(self) -> int:
        """素材数量"""
        return len(self.assets)

    @property
    def bin_count(self) -> int:
        """素材箱数量"""
        return len(self.bins)

    @property
    def timeline_count(self) -> int:
        """时间线数量"""
        return len(self.timelines)

    @property
    def active_timeline(self) -> TimelineSequenceRef | None:
        """当前活动时间线"""
        if self.active_timeline_id is None:
            return None
        return self.timelines.get(self.active_timeline_id)

    @property
    def root_bin(self) -> ProjectBin | None:
        """根素材箱"""
        return self.bins.get(self.root_bin_id)

    @property
    def video_assets(self) -> list[MediaAsset]:
        """所有视频素材"""
        return [a for a in self.assets.values() if a.is_video]

    @property
    def image_assets(self) -> list[MediaAsset]:
        """所有图片素材"""
        return [a for a in self.assets.values() if a.is_image]

    @property
    def audio_assets(self) -> list[MediaAsset]:
        """所有音频素材"""
        return [a for a in self.assets.values() if a.is_audio]

    @property
    def offline_assets(self) -> list[MediaAsset]:
        """所有离线素材（文件不存在）"""
        return [a for a in self.assets.values() if not a.is_virtual and not os.path.exists(a.path)]

    @property
    def recent_assets(self) -> list[MediaAsset]:
        """最近24小时导入的素材"""
        cutoff_time = time.time() - 24 * 3600
        return [a for a in self.assets.values() if a.created_at >= cutoff_time]

    # ==================== SmartBin 方法 ====================

    @staticmethod
    def get_default_smart_bins() -> list[SmartBin]:
        """
        获取预定义的 Smart Bins。

        Returns:
            预定义 SmartBin 列表
        """
        return [
            SmartBin.create_all_video(),
            SmartBin.create_all_audio(),
            SmartBin.create_all_stills(),
            SmartBin.create_offline_media(),
            SmartBin.create_recent_imports(),
        ]

    def get_smart_bin_assets(self, smart_bin_id: str) -> list[MediaAsset]:
        """
        根据 Smart Bin ID 获取匹配的素材。

        Args:
            smart_bin_id: Smart Bin ID（使用预定义常量）

        Returns:
            匹配的素材列表
        """
        smart_bin = None
        for sb in self.get_default_smart_bins():
            if sb.bin_id == smart_bin_id:
                smart_bin = sb
                break

        # P1-12: 用户自建 Smart Bin（已保存搜索）
        if smart_bin is None:
            smart_bin = self.smart_bins.get(smart_bin_id)

        if smart_bin is None:
            return []

        return smart_bin.get_assets(self)

    def __hash__(self) -> int:
        """哈希值，基于 project_id"""
        return hash(self.project_id)

    def __eq__(self, other: object) -> bool:
        """相等比较，基于 project_id"""
        if not isinstance(other, EditProject):
            return NotImplemented
        return self.project_id == other.project_id

    def __repr__(self) -> str:
        """详细表示"""
        return (
            f"EditProject(project_id={self.project_id!r}, "
            f"name={self.name!r}, "
            f"assets={self.asset_count}, "
            f"bins={self.bin_count}, "
            f"timelines={self.timeline_count})"
        )


# ================================================================
# 辅助函数：素材元数据探测
# ================================================================

def _probe_audio_info(path: str) -> Optional[dict[str, Any]]:
    """
    探测音频流信息（内部辅助函数）。

    使用 PyAV 打开文件并探测音频流元数据：
    - channels: 声道数 (1=mono, 2=stereo, etc.)
    - sample_rate: 采样率 (Hz)
    - codec: 音频编码器名称

    Args:
        path: 媒体文件路径

    Returns:
        audio_info 字典，或 None（无音频流）
    """
    try:
        import av

        container = None
        try:
            container = av.open(path)
            audio_stream = None

            # 查找音频流
            for stream in container.streams:
                if stream.type == 'audio':
                    audio_stream = stream
                    break

            if audio_stream is None:
                return None

            # 提取音频信息
            channels = audio_stream.channels if hasattr(audio_stream, 'channels') else 2
            sample_rate = audio_stream.rate if hasattr(audio_stream, 'rate') else 48000
            codec_name = audio_stream.codec.name if audio_stream.codec else 'unknown'
            # §4.1: 提取声道布局（PyAV layout，如 'stereo'/'5.1'/'7.1'/'mono'）
            channel_layout = None
            try:
                if hasattr(audio_stream, 'layout'):
                    channel_layout = str(audio_stream.layout.name)
            except Exception:
                channel_layout = None

            # 时长（秒）：音频素材的 duration_seconds 由 MediaAsset.duration_seconds
            # 直接读取；缺失时时间线 clip 会退化为 5 秒图像默认值，音频播放
            # 5 秒后被当作"到达终点"停止。优先 stream.duration，回退 container。
            duration_seconds: float | None = None
            try:
                if audio_stream.duration is not None and audio_stream.time_base:
                    duration_seconds = float(audio_stream.duration * audio_stream.time_base)
            except Exception:
                duration_seconds = None
            if duration_seconds is None:
                try:
                    if container.duration is not None:
                        duration_seconds = float(container.duration / av.time_base)
                except Exception:
                    duration_seconds = None
            if duration_seconds is not None and duration_seconds < 0:
                duration_seconds = None

            return {
                "channels": channels,
                "sample_rate": sample_rate,
                "codec": codec_name,
                "channel_layout": channel_layout,  # §4.1: 声道布局（供 downmix 决策）
                "duration_seconds": duration_seconds,
            }
        finally:
            # AUD-17107：探测中途抛异常（如损坏文件上 audio_stream.codec
            # 抛错）也保证关闭容器，不泄漏文件句柄/解复用器
            if container is not None:
                container.close()

    except Exception as e:
        logger.warning(f"Failed to probe audio info: {path}, {e}")
        return None


def probe_media_metadata(path: str) -> dict[str, Any]:
    """
    探测媒体文件元数据。

    根据文件类型使用不同探测方法：
    - 图片：使用 FileIO.get_metadata()
    - 视频：使用 MediaSourceFactory.probe_video_metadata() + PyAV 音频探测
    - 音频：基本信息

    Args:
        path: 媒体文件路径

    Returns:
        元数据字典
    """
    from src.shared.io.file_io import FileIO

    # 检查文件是否存在
    if not os.path.exists(path):
        return {}

    # 获取文件扩展名
    ext = os.path.splitext(path)[1].lower()

    # 图片扩展名列表 (from unified registry)
    from src.shared.media.format_registry import IMAGE_EXTENSIONS as _img_exts
    image_extensions = list(_img_exts)

    # 视频扩展名列表 (from unified registry)
    from src.shared.media.format_registry import VIDEO_EXTENSIONS as _video_exts
    video_extensions = list(_video_exts)

    # 音频扩展名列表
    from src.shared.media.format_registry import AUDIO_EXTENSIONS as _audio_exts
    audio_extensions = list(_audio_exts)

    metadata: dict[str, Any] = {}

    if ext in image_extensions:
        # .toos 单图特化：走 topos image 探测链（档位/色彩/位深/像素格式
        # 齐全；FileIO/exiftool 路线对 TPIM 产不出 codec 信息）。探测失败
        # 回退 FileIO 通用路线（错误信息不丢）。
        from src.shared.media.format_registry import is_toos_image
        if is_toos_image(path):
            try:
                from src.shared.media.factory import MediaSourceFactory
                toos_meta = MediaSourceFactory.probe_video_metadata(path)
            except Exception as e:  # noqa: BLE001 — 探测失败降级，不阻断导入
                logger.warning(f"Failed to probe .toos metadata: {path}, {e}")
                toos_meta = None
            if toos_meta is not None:
                metadata = {
                    "width": toos_meta.width,
                    "height": toos_meta.height,
                    "fps": None,           # 静态图（避免显示 1 fps）
                    "total_frames": 1,
                    "codec": toos_meta.codec_name,
                    "codec_profile": toos_meta.codec_profile,
                    "pixel_format": toos_meta.pixel_format,
                    "bit_depth": toos_meta.bit_depth,
                    "has_alpha": toos_meta.has_alpha,
                    "field_order": toos_meta.field_order,
                    "rotation": toos_meta.rotation,
                    "format": "TOOS",
                    "file_size": os.path.getsize(path),
                }
                if getattr(toos_meta, "color_primaries", None):
                    metadata["color_primaries"] = toos_meta.color_primaries
                if getattr(toos_meta, "color_transfer", None):
                    metadata["color_transfer"] = toos_meta.color_transfer
                if getattr(toos_meta, "color_matrix", None):
                    metadata["color_matrix"] = toos_meta.color_matrix
                if getattr(toos_meta, "color_range", None):
                    metadata["color_range"] = toos_meta.color_range
                try:
                    if toos_meta.is_hdr:
                        metadata["is_hdr"] = True
                except Exception:
                    pass
                return metadata
        # 图片：使用 FileIO
        try:
            metadata = FileIO.get_metadata(path)
            # 转换格式为统一结构
            result = {
                "width": None,
                "height": None,
                "format": ext[1:].upper(),
                "file_size": os.path.getsize(path),
            }
            # 解析分辨率
            resolution = metadata.get("Resolution", "")
            if resolution and resolution != "-":
                parts = resolution.split(" x ")
                if len(parts) == 2:
                    try:
                        result["width"] = int(parts[0])
                        result["height"] = int(parts[1])
                    except ValueError:
                        pass
            # 添加其他元数据
            result["camera"] = metadata.get("Camera", "")
            result["lens"] = metadata.get("Lens", "")
            result["iso"] = metadata.get("ISO", "")
            result["shutter"] = metadata.get("Shutter", "")
            result["aperture"] = metadata.get("Aperture", "")
            result["date"] = metadata.get("Date", "")
            metadata = result
        except Exception as e:
            logger.warning(f"Failed to probe image metadata: {path}, {e}")
            metadata = {"format": ext[1:].upper(), "error": str(e)}

    elif ext in video_extensions:
        # 视频：使用 MediaSourceFactory + PyAV 音频探测
        try:
            from src.shared.media.factory import MediaSourceFactory
            video_meta = MediaSourceFactory.probe_video_metadata(path)
            if video_meta is not None:
                source_fps = video_meta.fps
                metadata = {
                    "width": video_meta.width,
                    "height": video_meta.height,
                    "fps": float(source_fps) if source_fps else None,
                    "fps_num": getattr(source_fps, "num", None),
                    "fps_den": getattr(source_fps, "den", None),
                    "total_frames": video_meta.total_frames,
                    "duration_seconds": video_meta.total_frames / float(source_fps) if source_fps and video_meta.total_frames else None,
                    "codec": video_meta.codec_name,
                    # 档位/profile（topos 源 = 'Topos 422/4444/4444 XQ'；
                    # FFmpeg 源 = 'HQ'/'Standard' 等）——Info 面板显示
                    # 'Topos 422'/'ProRes 422 HQ' 的数据源，缺失会退化为
                    # 仅 codec 名。
                    "codec_profile": video_meta.codec_profile,
                    "pixel_format": video_meta.pixel_format,
                    "bit_depth": video_meta.bit_depth,
                    # Info 面板 Scan/Timecode/Rotation/Alpha 行同源字段
                    # （此前探测到但被丢弃，面板恒显示 '—'）。
                    "field_order": video_meta.field_order,
                    "timecode": video_meta.timecode,
                    "rotation": video_meta.rotation,
                    "has_alpha": video_meta.has_alpha,
                    "format": ext[1:].upper(),
                    "file_size": os.path.getsize(path),
                }
                # T-P0-10：持久化安全的 media identity（backend + 内容签名）。
                # 只存可重建字段（size/mtime_ns），不存机器绝对 dylib 路径——
                # 工程 reload 时可据此判断 metadata 是否仍可信。
                try:
                    probe_result = MediaSourceFactory.probe_video_metadata_cached(path)
                    metadata["media_backend"] = probe_result.backend
                    if probe_result.signature is not None:
                        metadata["media_identity"] = {
                            "size_bytes": probe_result.signature[0],
                            "mtime_ns": probe_result.signature[1],
                            "fingerprint": "size+mtime_ns",
                        }
                except Exception:  # noqa: BLE001 — identity 记录失败不阻断导入
                    pass

                # Persist source color metadata (primaries/transfer/matrix/range
                # + HDR flag) so the export/encoder layer can tag the output
                # stream correctly and the IDT can decode with the right
                # transfer function. Previously these were probed but dropped.
                if getattr(video_meta, "color_primaries", None):
                    metadata["color_primaries"] = video_meta.color_primaries
                if getattr(video_meta, "color_transfer", None):
                    metadata["color_transfer"] = video_meta.color_transfer
                if getattr(video_meta, "color_matrix", None):
                    metadata["color_matrix"] = video_meta.color_matrix
                if getattr(video_meta, "color_range", None):
                    metadata["color_range"] = video_meta.color_range
                try:
                    if video_meta.is_hdr:
                        metadata["is_hdr"] = True
                except Exception:
                    pass
                
                # F5.4：VFR PTS 索引构建——逐包 demux 抽取秒表；仅 VFR
                # 素材写入 metadata（CFR 不写，插入路径零开销走 CFR 数学）。
                try:
                    from src.shared.media.ffmpeg_source import (
                        extract_pts_index_seconds, looks_vfr,
                    )
                    pts_seconds = extract_pts_index_seconds(path)
                    if pts_seconds and looks_vfr(pts_seconds):
                        metadata["pts_index"] = pts_seconds
                        metadata["is_vfr"] = True
                except Exception as e:  # noqa: BLE001 — 索引失败不阻断导入
                    logger.warning(f"VFR PTS index build failed for {path}: {e}")

                # 探测音频流信息（修复 V-05/V-09 问题）
                audio_info = _probe_audio_info(path)
                if audio_info:
                    metadata["audio_info"] = audio_info
                    logger.debug(f"Video has audio: {path}, channels={audio_info.get('channels')}, sample_rate={audio_info.get('sample_rate')}")
            else:
                metadata = {"format": ext[1:].upper(), "error": "probe failed"}
        except Exception as e:
            logger.warning(f"Failed to probe video metadata: {path}, {e}")
            metadata = {"format": ext[1:].upper(), "error": str(e)}

    elif ext in audio_extensions:
        # 音频：探测音频流信息
        audio_info = _probe_audio_info(path)
        metadata = {
            "format": ext[1:].upper(),
            "file_size": os.path.getsize(path),
            "audio": True,
        }
        if audio_info:
            metadata["audio_info"] = audio_info
            # duration_seconds 是 MediaAsset.duration_seconds / 时间线 clip
            # 时长的数据源；缺失会让音频 clip 退化为 5 秒默认值并在 5 秒
            # 后停止播放。
            if audio_info.get("duration_seconds") is not None:
                metadata["duration_seconds"] = audio_info["duration_seconds"]

    else:
        # 未知格式
        metadata = {
            "format": ext[1:].upper() if ext else "unknown",
            "file_size": os.path.getsize(path),
        }

    return metadata


def get_media_type(path: str) -> str:
    """
    根据文件路径判断媒体类型。

    Args:
        path: 文件路径

    Returns:
        媒体类型（image, video, audio, subtitle, image_sequence）
    """
    from src.shared.media.format_registry import get_media_type as _get_type
    result = _get_type(path)
    if result == 'unknown':
        return "image"  # 默认作为图片处理
    return result


# ================================================================
# P6.3 Offline/Relink/Proxy 功能
# ================================================================

def check_media_availability(project: EditProject) -> dict[str, bool]:
    """
    检查项目中所有素材的可用性。

    Args:
        project: EditProject 实例

    Returns:
        {asset_id: is_available} 映射
    """
    availability: dict[str, bool] = {}
    for asset_id, asset in project.assets.items():
        # AUD-17104：与 get_offline_assets/update_offline_status 的 is_virtual
        # 语义对齐；placeholder（path 为空串的虚拟素材）同样没有宿主文件，
        # 不得被 os.path.exists 误判为离线。
        if asset.is_virtual or asset.media_type == "placeholder":
            availability[asset_id] = True  # 虚拟/占位素材没有真实文件路径
        else:
            availability[asset_id] = os.path.exists(asset.path)
    return availability


def get_offline_assets(project: EditProject) -> list[MediaAsset]:
    """
    获取项目中所有离线素材。

    Args:
        project: EditProject 实例

    Returns:
        离线素材列表
    """
    offline_assets: list[MediaAsset] = []
    for asset in project.assets.values():
        if asset.is_virtual:
            continue  # Virtual assets don't have real file paths
        if not os.path.exists(asset.path):
            offline_assets.append(asset)
    return offline_assets


def update_offline_status(project: EditProject) -> EditProject:
    """
    更新项目中所有素材的离线状态。

    遍历所有素材，检查文件是否存在，更新 is_offline 标记。

    Args:
        project: EditProject 实例

    Returns:
        新的 EditProject 实例（包含更新的离线状态）
    """
    new_assets: dict[str, MediaAsset] = {}
    changed = False

    for asset_id, asset in project.assets.items():
        if asset.is_virtual:
            new_assets[asset_id] = asset
            continue  # Virtual assets are always "available"
        is_offline = not os.path.exists(asset.path)
        if asset.is_offline != is_offline:
            new_assets[asset_id] = asset.with_offline_status(is_offline)
            changed = True
        else:
            new_assets[asset_id] = asset

    if not changed:
        return project

    now = time.time()

    return replace(project, 
        revision=project.revision,  # FE-B10：保留 revision，不在此处 bump
        assets=new_assets,
        modified_at=now,
    )


def relink_asset(project: EditProject, asset_id: str, new_path: str) -> EditProject:
    """
    重链接素材到新路径。

    Args:
        project: EditProject 实例
        asset_id: 素材 ID
        new_path: 新路径

    Returns:
        新的 EditProject 实例

    Raises:
        ValueError: 如果素材不存在
    """
    if asset_id not in project.assets:
        raise ValueError(f"Asset not found: {asset_id}")

    asset = project.assets[asset_id]

    # 验证新路径是否存在
    if not os.path.exists(new_path):
        logger.warning(f"Relink path does not exist: {new_path}")

    # 创建新素材实例（更新路径和离线状态）
    is_offline = not os.path.exists(new_path)
    new_asset = asset.with_path(new_path).with_offline_status(is_offline)

    # 更新素材字典
    new_assets = dict(project.assets)
    new_assets[asset_id] = new_asset

    now = time.time()
    return replace(project, 
        revision=project.revision,  # FE-B10：纯 helper 保留 revision，bump 只在统一提交边界
        
        assets=new_assets,
        modified_at=now,
    )


def replace_media(
    project: EditProject,
    asset_id: str,
    new_path: str,
    *,
    new_metadata: dict[str, Any] | None = None,
    new_media_type: str | None = None,
    color_metadata: dict[str, Any] | None = None,
) -> EditProject:
    """替换素材源文件（P1-13 Replace Media）。

    与 relink_asset 的区别：replace 是有意的换源操作——
    - asset_id 不变，因此所有时间线剪辑的 asset_id/入出点/特效引用
      全部自然保留（“替换素材不丢剪辑参数”）；
    - 旧代理失效，代理路径与状态被清除；
    - display_name 若与旧文件名一致（未被用户重命名过）则跟随新文件名；
    - 可选传入重新探测的 metadata（分辨率/帧率/时长等），未传时保留
      旧 metadata（调用方负责在 UI 层重新探测）。

    Args:
        project: 工程实例
        asset_id: 素材 ID
        new_path: 新源文件路径（必须存在）
        new_metadata: 重新探测的元数据（可选）

    Returns:
        新的 EditProject 实例

    Raises:
        ValueError: 素材不存在或新路径不存在
    """
    if asset_id not in project.assets:
        raise ValueError(f"Asset not found: {asset_id}")
    if not os.path.exists(new_path):
        raise ValueError(f"Replace target does not exist: {new_path}")

    asset = project.assets[asset_id]

    old_basename = os.path.basename(asset.path) if asset.path else ""
    new_basename = os.path.basename(new_path)
    display_name = asset.display_name
    if not old_basename or display_name == old_basename:
        # 用户没有改过名字 → 跟随新文件名；改过 → 保留用户命名
        display_name = new_basename

    # F-U04：探测到的类型/fps 随换源更新（video→jpg 不再保留旧 video
    # 与旧 fps；未提供时维持旧行为——调用方负责探测）
    effective_media_type = new_media_type or asset.media_type
    effective_metadata = (
        dict(new_metadata) if new_metadata is not None else asset.metadata
    )
    # N22/RF06：缩略图键指向旧源缓存，换源即失效；提供新色彩元数据
    # （调用方与导入同口径提取）时随换源刷新——此前成功换 PNG 仍保留
    # 旧 HDR 色彩与旧缩略图。
    effective_color = (
        dict(color_metadata)
        if color_metadata is not None else asset.color_metadata
    )
    new_asset = replace(
        asset,
        path=new_path,
        is_offline=False,
        proxy_path=None,
        proxy_status="none",
        display_name=display_name,
        media_type=effective_media_type,
        metadata=effective_metadata,
        thumbnail_key=None,
        color_metadata=effective_color,
    )

    new_assets = dict(project.assets)
    new_assets[asset_id] = new_asset

    logger.info(
        "Replace media: asset %s %r -> %r (clips keep parameters via stable asset_id)",
        asset_id, asset.path, new_path,
    )
    now = time.time()
    return replace(project, 
        revision=project.revision,  # FE-B10：纯 helper 保留 revision，bump 只在统一提交边界
        
        assets=new_assets,
        modified_at=now,
    )


def batch_relink_assets(
    project: EditProject,
    relink_map: dict[str, str]
) -> EditProject:
    """
    批量重链接素材。

    Args:
        project: EditProject 实例
        relink_map: {asset_id: new_path} 映射

    Returns:
        新的 EditProject 实例
    """
    now = time.time()
    new_assets: dict[str, MediaAsset] = dict(project.assets)

    for asset_id, new_path in relink_map.items():
        if asset_id in new_assets:
            asset = new_assets[asset_id]
            is_offline = not os.path.exists(new_path)
            new_assets[asset_id] = asset.with_path(new_path).with_offline_status(is_offline)

    return replace(project, 
        revision=project.revision,  # FE-B10：纯 helper 保留 revision，bump 只在统一提交边界
        
        assets=new_assets,
        modified_at=now,
    )


def update_asset_proxy(
    project: EditProject,
    asset_id: str,
    proxy_path: str | None,
    proxy_status: str
) -> EditProject:
    """
    更新素材的代理信息。

    Args:
        project: EditProject 实例
        asset_id: 素材 ID
        proxy_path: 代理文件路径
        proxy_status: 代理状态（none, pending, ready, failed）

    Returns:
        新的 EditProject 实例
    """
    if asset_id not in project.assets:
        raise ValueError(f"Asset not found: {asset_id}")

    asset = project.assets[asset_id]

    # 创建新素材实例
    new_asset = asset.with_proxy_path(proxy_path).with_proxy_status(proxy_status)

    # 更新素材字典
    new_assets = dict(project.assets)
    new_assets[asset_id] = new_asset

    now = time.time()
    return replace(project, 
        revision=project.revision,  # FE-B10：纯 helper 保留 revision，bump 只在统一提交边界
        
        assets=new_assets,
        modified_at=now,
    )


# ================================================================
# B7: 占位符片段（Placeholder Clip）
# ================================================================

def create_placeholder_asset(
    project: "EditProject",
    name: str,
    *,
    width: int = 1920,
    height: int = 1080,
    fps: float = 24.0,
    duration_frames: int = 96,
    note: str = "",
) -> tuple["EditProject", "MediaAsset"]:
    """创建占位符素材并加入工程（B7 粗剪定结构工作流）。

    占位素材无磁盘文件（path 为空），元数据记录目标分辨率/帧率/时长/
    备注（如"此处待补：航拍空镜"）。渲染走专用静态帧（彩条+斜纹），
    不触发解码器；导出默认被 preflight 拦截并列出全部占位片段。

    Args:
        project: 目标工程
        name: 显示名称（如"航拍空镜 [待补]"）
        width/height/fps: 目标规格（对齐序列设置）
        duration_frames: 建议时长（帧）
        note: 备注说明

    Returns:
        (新工程, 新占位素材)
    """
    clean = (name or "").strip() or "Placeholder"
    asset = MediaAsset(
        asset_id=str(uuid.uuid4()),
        path="",
        media_type="placeholder",
        display_name=clean,
        metadata={
            "placeholder": True,
            "width": int(width),
            "height": int(height),
            "fps": float(fps),
            "duration_frames": max(1, int(duration_frames)),
            "note": str(note or ""),
        },
    )
    return project.with_media_asset_added(asset), asset


def list_placeholder_clips(
    project: "EditProject",
    timeline: "Any",
) -> "list[tuple[Any, MediaAsset]]":
    """枚举时间线上全部占位片段（B7 preflight/标记列表用）。

    Returns:
        [(clip, asset), ...] 按轨道与时间位置排序。
    """
    results: "list[tuple[Any, MediaAsset]]" = []
    if project is None or timeline is None:
        return results
    for track in timeline.all_tracks:
        for clip in track.clips:
            asset = project.assets.get(clip.asset_id)
            if asset is not None and asset.media_type == "placeholder":
                results.append((clip, asset))
    results.sort(key=lambda item: (item[0].timeline_start, item[0].track_id))
    return results
