"""
Clip Parameters Module
剪辑参数数据模型。

## 聧责

- TransformParams: 变换参数（位置、缩放、旋转、锚点）
- CropParams: 裁切参数（边缘裁切和羽化）
- SpeedParams: 速度参数（播放速度、反转、插值方式）

## 设计原则

- 不可变数据类（frozen=True）确保线程安全
- 完整类型注解
- 不依赖 PyQt，纯 Python 数据模型
- 参数验证在 __post_init__ 中进行

## 参考

- docs/edit_page_timeline_architecture.md P0.B ADR
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, ClassVar, TYPE_CHECKING

from src.shared.extra_fields import preserves_extras, split_extras

# AUD-16109: 注解引用 "np.ndarray" 而 numpy 原先仅在函数体内局部导入，
# typing.get_type_hints() 解析字符串注解时会 NameError——改为模块级
# TYPE_CHECKING 导入供注解解析使用（运行时零开销，不引入硬依赖）。
if TYPE_CHECKING:  # pragma: no cover
    import numpy as np


def _require_finite(name: str, value: float) -> None:
    """AUD-16103: NaN/Inf 与任何数比较均为 False，可绕过全部基于
    ``<``/``>`` 的范围校验（Python ``json.loads`` 默认还接受 ``NaN``
    字面量）。所有 float 参数字段统一在此拦截非有限值。
    """
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")


class ClipBlendMode(str, Enum):
    """A1: 时间线片段图层混合模式（16 种标准模式）。

    语义为图层混合（与下方已合成结果混合），与
    timeline_render_plan.BlendMode 的转场混合语义不同，勿混用。
    数值即 GLSL/NumPy 两侧共享的 mode 编号（uniform int）。
    """

    NORMAL = "normal"            # 0
    MULTIPLY = "multiply"        # 1
    SCREEN = "screen"            # 2
    OVERLAY = "overlay"          # 3
    DARKEN = "darken"            # 4
    LIGHTEN = "lighten"          # 5
    COLOR_DODGE = "color_dodge"  # 6
    COLOR_BURN = "color_burn"    # 7
    HARD_LIGHT = "hard_light"    # 8
    SOFT_LIGHT = "soft_light"    # 9
    DIFFERENCE = "difference"    # 10
    EXCLUSION = "exclusion"      # 11
    HUE = "hue"                  # 12
    SATURATION = "saturation"    # 13
    COLOR = "color"              # 14
    LUMINOSITY = "luminosity"    # 15

    @property
    def code(self) -> int:
        """共享 GLSL / NumPy 分派用的整数编号。"""
        return _CLIP_BLEND_CODES[self]


#: 枚举 → 整数编号（定义顺序即编号；新枚举成员追加到末尾保持稳定）
_CLIP_BLEND_CODES: dict[ClipBlendMode, int] = {
    mode: index for index, mode in enumerate(ClipBlendMode)
}


@dataclass(frozen=True)
@preserves_extras
class TransformParams:
    """
    变换参数（像素空间模型，与合成页 TransformNode 完全一致）。

    NUKE/Fusion 约定：translate/scale/rotation 都在像素空间，pivot 决定
    旋转/缩放的中心。这样剪辑页与合成页的 transform 语义、单位、范围
    完全统一，手柄控制器可以直接复用 composite 的 viewer_handles 几何
    数学，无需归一化↔像素桥接层。

    Attributes:
        translate_x: X 平移（像素，正值向右）
        translate_y: Y 平移（像素，正值向下，Y-down）
        scale_x: X 缩放（1.0 = 100%）
        scale_y: Y 缩放（1.0 = 100%）
        rotation: 旋转角度（度，正值顺时针）
        pivot_mode: 锚点模式（"center" 用画面中心，"custom" 用 pivot_x/y）
        pivot_x: 自定义锚点 X（像素，pivot_mode="custom" 时生效）
        pivot_y: 自定义锚点 Y（像素，pivot_mode="custom" 时生效）
    """

    translate_x: float = 0.0
    translate_y: float = 0.0
    scale_x: float = 1.0
    scale_y: float = 1.0
    rotation: float = 0.0
    pivot_mode: str = "center"
    pivot_x: float = 0.0
    pivot_y: float = 0.0
    # DESK-04A：未来 schema 未知键保留（load→save 不丢）
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    # to_dict 全部键 + legacy 迁移键（position/anchor 已迁移，不算未知）
    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "translate_x", "translate_y", "scale_x", "scale_y", "rotation",
        "pivot_mode", "pivot_x", "pivot_y",
        "position_x", "position_y", "anchor_x", "anchor_y",
    })

    def __post_init__(self) -> None:
        """验证变换参数有效性"""
        for float_name in (
            "translate_x", "translate_y", "scale_x", "scale_y",
            "rotation", "pivot_x", "pivot_y",
        ):
            _require_finite(float_name, float(getattr(self, float_name)))
        if self.scale_x < 0.0 or self.scale_y < 0.0:
            raise ValueError(
                f"Scale cannot be negative: scale_x={self.scale_x}, scale_y={self.scale_y}"
            )
        if self.scale_x > 5.0 or self.scale_y > 5.0:
            raise ValueError(
                f"Scale too large: scale_x={self.scale_x}, scale_y={self.scale_y} (max 5.0)"
            )
        if self.pivot_mode not in ("center", "custom"):
            raise ValueError(f"Invalid pivot_mode: {self.pivot_mode}")

    # ==================== 工厂方法 ====================

    def with_translate(self, x: float, y: float) -> TransformParams:
        """创建新实例，修改平移（像素）。"""
        return TransformParams(
            translate_x=x, translate_y=y,
            scale_x=self.scale_x, scale_y=self.scale_y,
            rotation=self.rotation,
            pivot_mode=self.pivot_mode, pivot_x=self.pivot_x, pivot_y=self.pivot_y,
        )

    def with_scale(self, scale_x: float, scale_y: float) -> TransformParams:
        """创建新实例，修改缩放。"""
        return TransformParams(
            translate_x=self.translate_x, translate_y=self.translate_y,
            scale_x=scale_x, scale_y=scale_y,
            rotation=self.rotation,
            pivot_mode=self.pivot_mode, pivot_x=self.pivot_x, pivot_y=self.pivot_y,
        )

    def with_scale_uniform(self, scale: float) -> TransformParams:
        """创建新实例，使用统一缩放。"""
        return self.with_scale(scale, scale)

    def with_rotation(self, degrees: float) -> TransformParams:
        """创建新实例，修改旋转角度。"""
        return TransformParams(
            translate_x=self.translate_x, translate_y=self.translate_y,
            scale_x=self.scale_x, scale_y=self.scale_y,
            rotation=degrees,
            pivot_mode=self.pivot_mode, pivot_x=self.pivot_x, pivot_y=self.pivot_y,
        )

    def with_pivot(self, mode: str, x: float = 0.0, y: float = 0.0) -> TransformParams:
        """创建新实例，修改锚点模式与位置。"""
        return TransformParams(
            translate_x=self.translate_x, translate_y=self.translate_y,
            scale_x=self.scale_x, scale_y=self.scale_y,
            rotation=self.rotation,
            pivot_mode=mode, pivot_x=x, pivot_y=y,
        )

    def with_pivot_mode(self, mode: str) -> TransformParams:
        """创建新实例，仅修改锚点模式（保留 pivot_x/y 不变）。"""
        return TransformParams(
            translate_x=self.translate_x, translate_y=self.translate_y,
            scale_x=self.scale_x, scale_y=self.scale_y,
            rotation=self.rotation,
            pivot_mode=mode, pivot_x=self.pivot_x, pivot_y=self.pivot_y,
        )

    # ==================== 序列化 ====================

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式，用于序列化。"""
        return {
            "translate_x": self.translate_x,
            "translate_y": self.translate_y,
            "scale_x": self.scale_x,
            "scale_y": self.scale_y,
            "rotation": self.rotation,
            "pivot_mode": self.pivot_mode,
            "pivot_x": self.pivot_x,
            "pivot_y": self.pivot_y,
            **self._extra_fields,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> TransformParams:
        """从字典创建实例。

        兼容旧版归一化模型（position_x/y、anchor_x/y）：检测到旧键时按
        参考分辨率 1920×1080 换算成像素，anchor 在中心 (0.5,0.5) 的迁成
        pivot_mode="center"，否则迁成 custom + 像素 pivot。
        """
        extras = split_extras(data, TransformParams._KNOWN_KEYS)
        # 旧版归一化数据兼容：检测 position_x/anchor_x 键。
        has_legacy = "position_x" in data or "anchor_x" in data
        if has_legacy:
            REF_W, REF_H = 1920.0, 1080.0
            translate_x = float(data.get("position_x", 0.0)) * REF_W
            translate_y = float(data.get("position_y", 0.0)) * REF_H
            anchor_x = float(data.get("anchor_x", 0.5))
            anchor_y = float(data.get("anchor_y", 0.5))
            # 中心锚点（默认值）→ center 模式；否则 custom + 像素 pivot。
            if abs(anchor_x - 0.5) < 0.01 and abs(anchor_y - 0.5) < 0.01:
                return TransformParams(
                    translate_x=translate_x, translate_y=translate_y,
                    scale_x=float(data.get("scale_x", 1.0)),
                    scale_y=float(data.get("scale_y", 1.0)),
                    rotation=float(data.get("rotation", 0.0)),
                    pivot_mode="center", pivot_x=0.0, pivot_y=0.0,
                    _extra_fields=extras,
                )
            return TransformParams(
                translate_x=translate_x, translate_y=translate_y,
                scale_x=float(data.get("scale_x", 1.0)),
                scale_y=float(data.get("scale_y", 1.0)),
                rotation=float(data.get("rotation", 0.0)),
                pivot_mode="custom",
                pivot_x=anchor_x * REF_W, pivot_y=anchor_y * REF_H,
                _extra_fields=extras,
            )
        # 新版像素模型。
        return TransformParams(
            translate_x=float(data.get("translate_x", 0.0)),
            translate_y=float(data.get("translate_y", 0.0)),
            scale_x=float(data.get("scale_x", 1.0)),
            scale_y=float(data.get("scale_y", 1.0)),
            rotation=float(data.get("rotation", 0.0)),
            pivot_mode=data.get("pivot_mode", "center"),
            pivot_x=float(data.get("pivot_x", 0.0)),
            pivot_y=float(data.get("pivot_y", 0.0)),
            _extra_fields=extras,
        )


# C01（第六轮审计）：clip 级可动画参数的权威值域——单一来源，供关键帧
# add/remove/undo/UI 共用。此前命令层写成 transform.position*（实际名称
# 是 translate_*），translate/rotation 落默认 [0,100]：X=-250 求值为 0、
# Y=500 变 100、rotation=-45 变 0。
# 语义与 TransformParams 一致：translate/pivot 像素无实际上限（±8K 余量），
# scale 遵循模型校验 [0, 5]，rotation 角度（±10 圈余量）。
CLIP_PARAM_RANGES: dict[str, tuple[float, float]] = {
    "opacity": (0.0, 1.0),
    "transform.translate_x": (-8192.0, 8192.0),
    "transform.translate_y": (-8192.0, 8192.0),
    "transform.scale_x": (0.0, 5.0),
    "transform.scale_y": (0.0, 5.0),
    "transform.rotation": (-3600.0, 3600.0),
    "transform.pivot_x": (-8192.0, 8192.0),
    "transform.pivot_y": (-8192.0, 8192.0),
}


def clip_param_range(param_name: str) -> tuple[float, float]:
    """clip 级可动画参数的 (min, max)。

    未知 transform.* 子参数回落 translate 的宽值域（像素语义），其余
    未知参数保持旧默认 [0, 100]（效果参数经 effect catalog 查询，不经此）。
    """
    if param_name in CLIP_PARAM_RANGES:
        return CLIP_PARAM_RANGES[param_name]
    if param_name.startswith("transform."):
        return (-8192.0, 8192.0)
    return (0.0, 100.0)


@dataclass(frozen=True)
@preserves_extras
class CropParams:
    """
    裁切参数。

    定义剪辑的边缘裁切，使用归一化值。

    Attributes:
        left: 左裁切（归一化，0.0 = 无裁切，0.1 = 裁去 10%）
        right: 右裁切（归一化）
        top: 上裁切（归一化）
        bottom: 下裁切（归一化）
        feather: 边缘羽化（像素）
    """

    left: float = 0.0
    right: float = 0.0
    top: float = 0.0
    bottom: float = 0.0
    feather: float = 0.0
    # DESK-04A：未知字段保留
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)
    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "left", "right", "top", "bottom", "feather",
    })

    def __post_init__(self) -> None:
        """验证裁切参数有效性"""
        for float_name in ("left", "right", "top", "bottom", "feather"):
            _require_finite(float_name, float(getattr(self, float_name)))
        # 单边裁切不能超过 50%
        if self.left < 0.0 or self.left > 0.5:
            raise ValueError(f"left crop must be between 0.0 and 0.5: {self.left}")
        if self.right < 0.0 or self.right > 0.5:
            raise ValueError(f"right crop must be between 0.0 and 0.5: {self.right}")
        if self.top < 0.0 or self.top > 0.5:
            raise ValueError(f"top crop must be between 0.0 and 0.5: {self.top}")
        if self.bottom < 0.0 or self.bottom > 0.5:
            raise ValueError(f"bottom crop must be between 0.0 and 0.5: {self.bottom}")
        # 羽化不能为负
        if self.feather < 0.0:
            raise ValueError(f"feather cannot be negative: {self.feather}")
        # 总裁切不能超过 100%（留有安全边界）
        if self.left + self.right > 0.9:
            raise ValueError(
                f"Total horizontal crop too large: {self.left + self.right} (max 0.9)"
            )
        if self.top + self.bottom > 0.9:
            raise ValueError(
                f"Total vertical crop too large: {self.top + self.bottom} (max 0.9)"
            )

    # ==================== 计算属性 ====================

    @property
    def total_horizontal_crop(self) -> float:
        """
        水平总裁切。

        Returns:
            左裁切 + 右裁切
        """
        return self.left + self.right

    @property
    def total_vertical_crop(self) -> float:
        """
        垂直总裁切。

        Returns:
            上裁切 + 下裁切
        """
        return self.top + self.bottom

    @property
    def remaining_horizontal(self) -> float:
        """
        水平剩余比例。

        Returns:
            1.0 - 总裁切
        """
        return 1.0 - self.total_horizontal_crop

    @property
    def remaining_vertical(self) -> float:
        """
        垂直剩余比例。

        Returns:
            1.0 - 总裁切
        """
        return 1.0 - self.total_vertical_crop

    # ==================== 工厂方法 ====================

    def with_crop(
        self,
        left: float | None = None,
        right: float | None = None,
        top: float | None = None,
        bottom: float | None = None,
    ) -> CropParams:
        """
        创建新实例，修改裁切值。

        Args:
            left: 新的左裁切（None 表示不变）
            right: 新的右裁切（None 表示不变）
            top: 新的上裁切（None 表示不变）
            bottom: 新的下裁切（None 表示不变）

        Returns:
            新的 CropParams 实例
        """
        return CropParams(
            left=left if left is not None else self.left,
            right=right if right is not None else self.right,
            top=top if top is not None else self.top,
            bottom=bottom if bottom is not None else self.bottom,
            feather=self.feather,
        )

    def with_feather(self, feather: float) -> CropParams:
        """
        创建新实例，修改羽化值。

        Args:
            feather: 新的羽化值（像素）

        Returns:
            新的 CropParams 实例
        """
        return CropParams(
            left=self.left,
            right=self.right,
            top=self.top,
            bottom=self.bottom,
            feather=feather,
        )

    # ==================== 序列化 ====================

    def to_dict(self) -> dict[str, Any]:
        """
        转换为字典格式，用于序列化。

        Returns:
            参数字典
        """
        return {
            "left": self.left,
            "right": self.right,
            "top": self.top,
            "bottom": self.bottom,
            "feather": self.feather,
            **self._extra_fields,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> CropParams:
        """
        从字典创建实例。

        Args:
            data: 参数字典

        Returns:
            CropParams 实例
        """
        return CropParams(
            # AUD-16104: 反序列化统一强转，拦截 JSON 中的字符串/整型混用
            left=float(data.get("left", 0.0)),
            right=float(data.get("right", 0.0)),
            top=float(data.get("top", 0.0)),
            bottom=float(data.get("bottom", 0.0)),
            feather=float(data.get("feather", 0.0)),
            _extra_fields=split_extras(data, CropParams._KNOWN_KEYS),
        )


@dataclass(frozen=True)
class SpeedPoint:
    """A retime-control boundary expressed in source-relative frames.

    ``source_offset`` is measured in the clip's playback direction.  A point
    starts a new constant-speed segment; its ``speed`` applies until the next
    point (or the end of the clip).  Keeping the point in source coordinates
    makes it stable when another segment is retimed and the timeline duration
    changes.
    """

    source_offset: int
    speed: float
    # A3.1: 斜坡插值。linear = 段内恒速（既有行为，逐帧一致）；
    # smooth = 本段速度从前段速度 smoothstep(3t²-2t³) 渐变到本段速度。
    easing: str = "linear"

    def __post_init__(self) -> None:
        _require_finite("speed-point speed", self.speed)
        if self.source_offset <= 0:
            raise ValueError("speed-point source_offset must be positive")
        if not 0.1 <= self.speed <= 8.0:
            raise ValueError(f"speed-point speed must be between 0.1 and 8.0: {self.speed}")
        if self.easing not in ("linear", "smooth"):
            raise ValueError(f"Invalid easing: {self.easing}, must be linear or smooth")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_offset": self.source_offset,
            "speed": self.speed,
            "easing": self.easing,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "SpeedPoint":
        return SpeedPoint(
            source_offset=int(data["source_offset"]),
            speed=float(data["speed"]),
            easing=str(data.get("easing", "linear")),
        )


@dataclass(frozen=True)
@preserves_extras
class SpeedParams:
    """
    速度参数。

    定义剪辑的播放速度和变速方式。

    Attributes:
        speed: 播放速度（1.0 = 正常，2.0 = 两倍速，0.5 = 半速）
        reverse: 是否反向播放
        interpolation: 插值方式（nearest, frame_blend, optical_flow）
        speed_points: 达芬奇式速度点；每个点开始一个新的恒定速度段
    """

    speed: float = 1.0
    reverse: bool = False
    interpolation: str = "nearest"  # nearest, frame_blend, optical_flow
    speed_points: tuple[SpeedPoint, ...] = ()
    # N01/C3-01：窗口切片的曲线控制域。speed_points 定义在"曲线坐标"上：
    # curve_offset = 当前窗口起点在曲线上的位置；curve_length = 曲线域
    # 总长（None = 曲线域就是当前 source_duration，legacy 语义）。trim/
    # split/overwrite 等窗口切片只移动窗口、绝不重排点位或重积分曲线——
    # 切分前后的保留帧逐帧一致（此前按缩短后的 source_duration 重新归
    # 一化 smooth 曲线/丢 step 点位，切点前后内容都会变）。反向片的
    # 点位同样按源序存储，reverse 只在输出端翻转。
    curve_offset: int = 0
    curve_length: int | None = None
    # P1-08: 音频保调变速。True 时离线导出走 WSOLA（audio_timestretch）；
    # 实时路径尚未接入（capability registry 标注 PARTIAL）。
    preserve_pitch: bool = False
    # Edit content v4: exact time origin in the immutable full speed curve.
    # None retains the v1-v3 integer-window origin.
    curve_time_offset: float | None = None
    # DESK-04A：未知字段保留
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)
    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "speed", "reverse", "interpolation", "speed_points",
        "curve_offset", "curve_length", "curve_time_offset", "preserve_pitch",
    })

    def __post_init__(self) -> None:
        """验证速度参数有效性"""
        _require_finite("speed", self.speed)
        # 与 Resolve 的 Retime Controls 预设范围一致：10% 到 800%。
        if self.speed < 0.1 or self.speed > 8.0:
            raise ValueError(f"speed must be between 0.1 and 8.0: {self.speed}")
        # 插值方式必须有效
        valid_interpolations = ("nearest", "frame_blend", "optical_flow")
        if self.interpolation not in valid_interpolations:
            raise ValueError(
                f"Invalid interpolation: {self.interpolation}, "
                f"must be one of {valid_interpolations}"
            )
        if self.curve_time_offset is not None:
            _require_finite("curve_time_offset", self.curve_time_offset)
        offsets = [point.source_offset for point in self.speed_points]
        if offsets != sorted(offsets) or len(offsets) != len(set(offsets)):
            raise ValueError("speed_points must be strictly sorted by source_offset")

    # ==================== 计算属性 ====================
    # AUD-16107: is_normal_speed / effective_duration_factor 已删除——
    # 全仓无调用方，且二者忽略 speed_points（变速剪辑下语义错误）。
    # 时长换算一律走 timeline_duration_for_source_duration()。

    @property
    def is_slow_motion(self) -> bool:
        """
        是否为慢动作。

        Returns:
            speed < 1.0
        """
        return self.speed < 1.0

    @property
    def is_fast_forward(self) -> bool:
        """
        是否为快进。

        Returns:
            speed > 1.0
        """
        return self.speed > 1.0

    @property
    def is_variable_speed(self) -> bool:
        """Whether this clip contains Resolve-style speed points."""
        return bool(self.speed_points)

    def _curve_window(self, source_duration: int) -> tuple[int, int]:
        """当前窗口 ``(w0, w1)``（曲线坐标）；legacy 域 = [0, duration)。"""
        # Handles may extend before the authored curve. Keep its coordinate
        # system fixed so adding handles cannot change the smooth LUT grid.
        w0 = int(self.curve_offset)
        return w0, w0 + max(1, int(source_duration))

    def _curve_length(self, source_duration: int) -> int:
        if self.curve_length is not None and self.curve_length > 0:
            return int(self.curve_length)
        return max(1, int(source_duration))

    def _full_curve_segments(
        self, curve_length: int,
    ) -> tuple[tuple[int, int, float], ...]:
        """整条曲线（域 [0, curve_length)）的 ``(start, end, speed)`` 段。"""
        valid_points = tuple(
            point for point in self.speed_points
            if 0 < point.source_offset < curve_length
        )
        starts = (0,) + tuple(point.source_offset for point in valid_points)
        speeds = (self.speed,) + tuple(point.speed for point in valid_points)
        ends = tuple(point.source_offset for point in valid_points) + (curve_length,)
        return tuple(zip(starts, ends, speeds))

    def _segments(self, source_duration: int) -> tuple[tuple[int, int, float], ...]:
        """Return ``(source_start, source_end, speed)`` segments.

        N01/C3-01：段按**曲线域**构建后裁剪到当前窗口。窗口起点落在
        某段中间时该段以原速继续（step 精确；smooth 由斜坡查表另行保
        留曲线形状）。legacy（curve_offset=0/curve_length=None）时与
        旧实现逐段相等。
        """
        curve_length = self._curve_length(source_duration)
        w0, w1 = self._curve_window(source_duration)
        window_segments = []
        if w0 < 0:
            window_segments.append((w0, min(0, w1), self.speed))
        for start, end, segment_speed in self._full_curve_segments(curve_length):
            lo, hi = max(start, w0), min(end, w1)
            if lo < hi:
                window_segments.append((lo, hi, segment_speed))
        if w1 > curve_length:
            tail_speed = self._full_curve_segments(curve_length)[-1][2]
            window_segments.append((max(w0, curve_length), w1, tail_speed))
        if not window_segments:
            window_segments = [(w0, w1, self.speed)]
        return tuple(window_segments)

    def sliced_by_window(
        self, start_delta: int, new_source_duration: int,
        previous_source_duration: int | None = None,
        timeline_offset: float | None = None,
    ) -> "SpeedParams":
        """N01/C3-01：窗口切片——起点平移 start_delta（源坐标）并改窗口长。

        点位保持在曲线坐标上不动；只有窗口移动。legacy（curve_length
        为 None）切片时以 ``previous_source_duration`` 物化原域长——域
        必须**先于**切片确定，否则缩短窗口会把曲线域一起截短、切点前
        的保留帧也变。越界窗口保持原域与积分网格，用首/尾速恒速延伸。
        """
        new_offset = int(self.curve_offset) + int(start_delta)
        if self.curve_length is not None:
            new_length = int(self.curve_length)
        else:
            new_length = max(1, int(
                previous_source_duration
                if previous_source_duration is not None
                else new_source_duration))
        time_origin = None
        if timeline_offset is not None:
            old_duration = previous_source_duration or new_source_duration
            time_origin = self._window_time_origin(old_duration) + timeline_offset
        return SpeedParams(
            speed=self.speed,
            reverse=self.reverse,
            interpolation=self.interpolation,
            speed_points=self.speed_points,
            curve_offset=new_offset,
            curve_length=new_length,
            preserve_pitch=self.preserve_pitch,
            curve_time_offset=time_origin,
        )

    def _window_time_origin(self, source_duration: int) -> float:
        """Time of this window in the full curve, before frame quantization."""
        if self.curve_time_offset is not None:
            return self.curve_time_offset
        return self._curve_position_time(self.curve_offset, source_duration)

    def _curve_position_time(self, position: float, source_duration: int) -> float:
        """Inverse of the immutable curve, including constant-speed handles."""
        if position < 0:
            return position / self.speed
        length = self._curve_length(source_duration)
        if self._has_smooth_ramp(source_duration):
            import numpy as np

            ns, nt, _start, total = self._ramp_lookup_tables(source_duration)
            if position <= length:
                return float(np.interp(position / length, ns, nt)) * total
            tail_speed = self._full_curve_segments(length)[-1][2]
            return total + (position - length) / tail_speed
        elapsed = 0.0
        segments = self._full_curve_segments(length)
        for start, end, speed in segments:
            elapsed += max(0, min(position, end) - start) / speed
            if position <= end:
                return elapsed
        return elapsed + (position - length) / segments[-1][2]

    def source_position_at_time(self, timeline_frame: float, source_duration: int) -> float:
        """Unquantized playback-direction source position, allowing handles.

        Curve slices keep the full curve's clock; beyond the authored curve,
        the first/last speeds extend constantly. Edge constraints and blend
        sampling share these semantics with committed window edits.
        """
        return (self._curve_position_at_time(timeline_frame, source_duration)
                - self.curve_offset)

    def _curve_position_at_time(self, timeline_frame: float, source_duration: int) -> float:
        """Continuous position in the authored domain, before window subtraction."""
        length = self._curve_length(source_duration)
        origin = self._window_time_origin(source_duration)
        domain_time = origin + float(timeline_frame)
        if domain_time < 0:
            return domain_time * self.speed
        if self._has_smooth_ramp(source_duration):
            import numpy as np
            ns, nt, _start, total = self._ramp_lookup_tables(source_duration)
            if domain_time <= total:
                return float(np.interp(domain_time / max(total, 1e-9), nt, ns)) * length
            last_speed = self._full_curve_segments(length)[-1][2]
            return length + (domain_time - total) * last_speed
        for start, end, speed in self._full_curve_segments(length):
            segment_duration = (end - start) / speed
            if domain_time <= segment_duration:
                return start + domain_time * speed
            domain_time -= segment_duration
        last_speed = self._full_curve_segments(length)[-1][2]
        return length + domain_time * last_speed

    def timeline_duration_for_source_duration(self, source_duration: int) -> int:
        """Return the exact timeline length implied by all speed segments.

        A3.1: 存在 smooth 斜坡时用速度曲线积分的总长（首尾帧锚定不变，
        中间帧按渐变速度重分布）。
        """
        if source_duration <= 0:
            raise ValueError("source_duration must be positive")
        if self.curve_length is not None:
            _, end = self._curve_window(source_duration)
            return max(1, round(self._curve_position_time(end, source_duration)
                                - self._window_time_origin(source_duration)))
        if self._has_smooth_ramp(source_duration):
            import numpy as np
            ns, nt, start_time, total = self._ramp_lookup_tables(source_duration)
            _, w1 = self._curve_window(source_duration)
            end_time = float(np.interp(w1 / self._curve_length(source_duration), ns, nt)) * total
            return max(1, round(end_time - start_time))
        # N01/C3-01：窗口段（曲线坐标）积分——legacy 全窗时与旧公式
        # 逐段相等；窗口切片时长 = 原曲线在窗口内的精确积分。
        return max(
            1,
            sum(max(1, round((end - start) / segment_speed))
                for start, end, segment_speed in self._segments(source_duration)),
        )

    def speed_at_source_offset(self, source_offset: int, source_duration: int) -> float:
        """Return the speed of the segment containing ``source_offset``."""
        w0, _w1 = self._curve_window(source_duration)
        curve_pos = w0 + max(0, min(int(source_offset), source_duration - 1))
        curve_length = self._curve_length(source_duration)
        for start, end, segment_speed in self._full_curve_segments(curve_length):
            if start <= curve_pos < end:
                return segment_speed
        return self.speed

    # ==================== A3.1: 平滑速度斜坡 ====================

    def _has_smooth_ramp(self, source_duration: int) -> bool:
        """任一 smooth 坡段与当前窗口相交时启用斜坡积分路径。

        N01/C3-01：判据是**坡段**（[point, 下一点/域尾)）与窗口相交，
        不是点位落在窗口内——右半切片的窗口起点常处于坡中（定义该坡
        的点在窗口后方），此时必须仍走查表路径才能与切分前逐帧一致。
        """
        # A sliced constant-looking window still uses the full curve's
        # quantizer. Switching it to the step path would change round→floor
        # before a ramp, including frames preceding the cut.
        length = self._curve_length(source_duration)
        return any(point.easing == "smooth" and point.source_offset < length
                   for point in self.speed_points)

    def _ramp_source_curve(
        self, source_duration: int, samples: int = 0,
        window_start: int | None = None,
        window_end: int | None = None,
    ) -> tuple["np.ndarray", "np.ndarray"]:
        """A3.1: 采样源域速度曲线并对时间轴积分（确定性查表基础）。

        速度曲线定义（源域）：段 [start, end) 的目标速度 v_i；其起点速度
        点 easing="smooth" 时段内速度从 v_{i-1} 经 smoothstep(3t²-2t³)
        渐变到 v_i，linear 段恒速（与旧行为逐帧一致）。

        Returns:
            (cum_source, cum_timeline)：cum_source[k] 为第 k 个采样点的源
            位置（均匀采样 0..source_duration）；cum_timeline[k] 为播放到
            该源位置所需的 timeline 帧数（浮点）。
        """
        import numpy as np

        w0, w1 = self._curve_window(source_duration)
        curve_length = self._curve_length(source_duration)
        if window_start is not None and window_end is not None:
            # N01/C3-01：显式窗口（曲线坐标）——按整域密度采样该窗口，
            # 输出为窗口坐标 [0, window_end-window_start)。
            w0, w1 = int(window_start), int(window_end)
            source_duration = max(1, w1 - w0)
        if samples <= 0:
            samples = max(2, curve_length * 4 + 1)
        # N01/C3-01：采样沿曲线域进行（坡形按**原始段边界**平滑，与切分
        # 前完全一致），输出为窗口坐标 [0, source_duration)。
        xs = np.linspace(0.0, float(source_duration), samples)
        segments = self._full_curve_segments(curve_length)
        speed_at = np.empty(samples, dtype=np.float64)
        seg_idx = 0
        for i, x in enumerate(xs):
            xi = min(int(w0 + x), curve_length - 1)
            while seg_idx + 1 < len(segments) and xi >= segments[seg_idx + 1][0]:
                seg_idx += 1
            start, end, target_speed = segments[seg_idx]
            if seg_idx == 0:
                speed_at[i] = target_speed
                continue
            easing = "linear"
            for point in self.speed_points:
                if point.source_offset == start:
                    easing = point.easing
                    break
            if easing == "smooth" and end > start:
                prev_speed = segments[seg_idx - 1][2]
                t = (w0 + x - start) / float(end - start)
                sstep = t * t * (3.0 - 2.0 * t)
                speed_at[i] = prev_speed + (target_speed - prev_speed) * sstep
            else:
                speed_at[i] = target_speed
        # timeline 位置 = ∫ dx / v(x)（v 单位：源帧 / 时间线帧）
        dt_source = np.diff(xs)
        avg_speed = 0.5 * (speed_at[1:] + speed_at[:-1])
        cum_timeline = np.concatenate(
            ([0.0], np.cumsum(dt_source / np.maximum(avg_speed, 1e-9))))
        return xs, cum_timeline

    def _ramp_lookup_tables(
        self, source_duration: int,
    ) -> tuple["np.ndarray", "np.ndarray", float, float]:
        """A3.1: 斜坡映射用的归一化查表。

        Returns:
            (norm_source, norm_timeline, scale)：norm_timeline 为归一化
            timeline 位置（0..1），norm_source 为对应的归一化源位置；
            scale = 斜坡路径的总 timeline 长度（浮点帧）。

        N01/C3-01：曲线表始终按**整个曲线域**采样（密度 = 域长×4），
        再切出当前窗口——切片窗口与切分前的表同源同密度，保留帧不再
        因窗口变短而改变采样分辨率（此前按窗口长重采样使 smooth 切分
        切点前 ±1 帧漂移）。
        """
        import numpy as np

        # N01/C3-01：整域表 + 窗口起点偏移 S——窗口内第 t 帧对应域时间
        # S + t，与切分前同一张表同一坐标系，切点落在整数速度域位置时
        # 精确续接（此前切片表归一化重采样引入 ±2 帧插值漂移）。
        curve_length = self._curve_length(source_duration)
        w0, _w1 = self._curve_window(source_duration)
        xs_full, cum_full = self._ramp_source_curve(
            curve_length, window_start=0, window_end=curve_length)
        total = float(cum_full[-1])
        norm_source = xs_full / float(max(1, curve_length))
        norm_timeline = cum_full / max(total, 1e-9)
        # S = 窗口起点（曲线位置 w0）对应的域时间线时间；T = 域总时长
        start_norm_source = w0 / float(max(1, curve_length))
        start_time_norm = float(np.interp(
            start_norm_source, norm_source, norm_timeline))
        if self.curve_time_offset is not None:
            start_time = self.curve_time_offset
        elif w0 < 0:
            start_time = w0 / self.speed
        elif w0 > curve_length:
            tail_speed = self._full_curve_segments(curve_length)[-1][2]
            start_time = total + (w0 - curve_length) / tail_speed
        else:
            start_time = start_time_norm * total
        return norm_source, norm_timeline, start_time, total

    # ==================== 帧转换 ====================

    def timeline_to_source_frames(
        self, timeline_frame: int | float, source_duration: int
    ) -> tuple[int, int]:
        """
        将时间线帧转换为源帧范围。

        对于变速播放，一个时间线帧可能对应多个源帧（frame_blend）。

        Args:
            timeline_frame: 时间线帧号（相对于剪辑起始）
            source_duration: 源素材总帧数

        Returns:
            (起始源帧, 结束源帧)，对于 nearest 模式两者相同

        Examples:
            >>> params = SpeedParams(speed=2.0)  # 两倍速
            >>> params.timeline_to_source_frames(10, 100)
            (20, 20)  # 第10帧对应第20帧

            >>> params = SpeedParams(speed=0.5)  # 半速（慢动作）
            >>> params.timeline_to_source_frames(10, 100)
            (5, 5)  # 第10帧对应第5帧
        """
        # N02/C3-02：接受连续 timeline 位置（float）——混合帧率折算后的
        # 等效位置不得先 int(round) 再乘段速度（量化误差会被 8x 级速度
        # 放大成 max4 源帧差）；沿段积分保持连续，最后统一量化。
        remaining = max(0.0, float(timeline_frame))
        source_frame = source_duration - 1
        if self.curve_length is not None:
            # Slices and extensions share the authored curve's clock. Handles
            # lie outside that domain and continue at its first/last speed.
            import math

            exact = self.source_position_at_time(remaining, source_duration)
            if self._has_smooth_ramp(source_duration):
                source_frame = round(round(exact + self.curve_offset, 9)) - self.curve_offset
            else:
                source_frame = math.floor(exact)
            source_frame = max(0, min(source_frame, source_duration - 1))
            if self.reverse:
                source_frame = source_duration - 1 - source_frame
            return source_frame, source_frame
        # A3.1: smooth 斜坡 → 查表映射（归一化位置双线性插值，确定性）
        if self._has_smooth_ramp(source_duration):
            import numpy as np

            ns, nt, start_time, total = self._ramp_lookup_tables(source_duration)
            # 窗口内第 remaining 帧的域时间 = S + remaining；整域表插值
            domain_time = min(max(start_time + remaining, 0.0), total)
            pos = domain_time / max(total, 1e-9)
            source_norm = float(np.interp(pos, nt, ns))
            curve_pos = round(round(source_norm * self._curve_length(source_duration), 9))
            w0, _w1 = self._curve_window(source_duration)
            source_frame = int(min(
                source_duration - 1, max(0, curve_pos - w0)))
            if self.reverse:
                source_frame = source_duration - 1 - source_frame
            return (source_frame, source_frame)
        # N01/C3-01：段为曲线坐标；输出换回窗口坐标。
        # N02/C3-02（含 K06 方向）：段边界用精确浮点长度 (end-start)/v
        # ——逐段 round 会在段边界量化时间轴，使"加入一个同速中性点"
        # 就改变保留帧（8x 实测 75/100 帧 max 差 4）；整数段长下与
        # legacy 逐帧一致，非整数段长只在边界 ≤1 帧内更精确。最终源帧
        # 仍按 legacy 向下截断。
        w0, _w1 = self._curve_window(source_duration)
        if self.curve_time_offset is not None:
            remaining += self.curve_time_offset
        segments = (self._full_curve_segments(self._curve_length(source_duration))
                    if self.curve_time_offset is not None
                    else self._segments(source_duration))
        for start, end, segment_speed in segments:
            exact_length = (end - start) / segment_speed
            if remaining < exact_length or (
                    exact_length <= 0 and remaining <= 0):
                source_frame = int(
                    start + remaining * segment_speed) - w0
                source_frame = max(0, min(source_frame, end - w0 - 1))
                break
            remaining -= exact_length

        # 确保帧号在有效范围内
        source_frame = max(0, min(source_frame, source_duration - 1))
        if self.reverse:
            source_frame = source_duration - 1 - source_frame

        if self.interpolation == "nearest":
            return (source_frame, source_frame)
        else:
            # frame_blend 或 optical_flow 可能需要帧范围
            # 第一版简化处理，返回单帧
            return (source_frame, source_frame)

    def timeline_to_source_frame_pair(
        self, timeline_frame: int | float, source_duration: int,
    ) -> tuple[int, int, float]:
        """A3.2: 时间线帧 → 双源帧采样（帧混合）。

        Returns:
            (frame_a, frame_b, weight)：weight ∈ [0,1] 为 frame_b 的混合
            权重。整速段（速度对齐帧边界）权重恒 0；慢速/斜坡路径权重=
            精确源位置的小数部分。nearest 语义 = weight 0 只取 frame_a。
        """
        import numpy as np  # 斜坡路径查表需要；恒速路径不触发

        # 复用内部浮点映射：ramp 路径查表插值；恒速路径解析式
        domain_exact: float | None = None
        if self.curve_length is not None:
            domain_exact = self._curve_position_at_time(max(0.0, float(timeline_frame)), source_duration)
            exact = domain_exact - self.curve_offset
        elif self._has_smooth_ramp(source_duration):
            ns, nt, start_time, total = self._ramp_lookup_tables(source_duration)
            remaining = max(0.0, float(timeline_frame))
            domain_time = min(max(start_time + remaining, 0.0), total)
            pos = domain_time / max(total, 1e-9)
            domain_exact = float(np.interp(pos, nt, ns)) * float(self._curve_length(source_duration))
            exact = domain_exact - self.curve_offset
        else:
            exact = 0.0
            remaining = max(0.0, float(timeline_frame))
            w0, _w1 = self._curve_window(source_duration)
            if self.curve_time_offset is not None:
                remaining += self.curve_time_offset
            segments = (self._full_curve_segments(self._curve_length(source_duration))
                        if self.curve_time_offset is not None
                        else self._segments(source_duration))
            for start, end, segment_speed in segments:
                exact_length = (end - start) / segment_speed
                if remaining < exact_length or (
                        exact_length <= 0 and remaining <= 0):
                    exact = start + (remaining * segment_speed) - w0
                    break
                remaining -= exact_length
            else:
                if remaining > 0:
                    exact = source_duration - 1

        import math
        lower = min(0, -self.curve_offset) if self.curve_length is not None else 0
        upper = (max(source_duration - 1, self.curve_length - self.curve_offset - 1)
                 if self.curve_length is not None else source_duration - 1)
        # Fractional blend weight belongs to the immutable domain. Subtracting
        # an integer window origin first can lose low float bits on extension.
        domain_floor = math.floor(domain_exact) if domain_exact is not None else None
        floor = domain_floor - self.curve_offset if domain_floor is not None else math.floor(exact)
        a = int(min(max(lower, floor), upper))
        frac = domain_exact - domain_floor if domain_exact is not None else exact - floor
        b = min(a + 1, upper)
        weight = min(max(frac, 0.0), 1.0)
        if self.reverse:
            a, b = source_duration - 1 - a, source_duration - 1 - b
            a, b = max(a, b), min(a, b)
        return (a, b, weight)

    def source_to_timeline_frame(self, source_frame: int) -> int:
        """
        将源帧转换为时间线帧。

        Args:
            source_frame: 源帧号

        Returns:
            时间线帧号

        Examples:
            >>> params = SpeedParams(speed=2.0)
            >>> params.source_to_timeline_frame(20)
            10  # 第20帧对应时间线第10帧
        """
        return self.source_to_timeline_frame_for_source_duration(
            source_frame,
            max(source_frame + 1, 1),
        )

    def source_to_timeline_frame_for_source_duration(
        self, source_frame: int, source_duration: int,
    ) -> int:
        """Inverse mapping with the clip's complete source duration available."""
        if source_frame < 0:
            raise ValueError("source_frame cannot be negative")
        offset = max(0, min(int(source_frame), source_duration - 1))
        if self.reverse:
            offset = source_duration - 1 - offset
        if self.curve_length is not None:
            elapsed = self._curve_position_time(self.curve_offset + offset, source_duration)
            duration = self.timeline_duration_for_source_duration(source_duration)
            return int(min(max(0.0, elapsed - self._window_time_origin(source_duration)), duration - 1))
        # A3.1: smooth 斜坡 → 归一化表反查（二分，确定性）
        if self._has_smooth_ramp(source_duration):
            import numpy as np

            ns, nt, start_time, total = self._ramp_lookup_tables(source_duration)
            w0, _w1 = self._curve_window(source_duration)
            src_norm = (w0 + offset) / float(
                max(1, self._curve_length(source_duration)))
            tl_norm = float(np.interp(src_norm, ns, nt))
            domain_time = tl_norm * total - start_time
            tl_frames = self.timeline_duration_for_source_duration(source_duration)
            return int(min(max(domain_time, 0.0), tl_frames - 1))
        elapsed = 0.0
        w0, _w1 = self._curve_window(source_duration)
        curve_pos = w0 + offset
        segments = (self._full_curve_segments(self._curve_length(source_duration))
                    if self.curve_time_offset is not None
                    else self._segments(source_duration))
        for start, end, segment_speed in segments:
            if start <= curve_pos < end:
                return int(max(0.0, elapsed + (curve_pos - start) / segment_speed
                               - (self.curve_time_offset or 0.0)))
            elapsed += (end - start) / segment_speed
        return int(elapsed)

    # ==================== 工厂方法 ====================

    def _reanchor_edited_curve(self, updated: "SpeedParams") -> "SpeedParams":
        """Keep a sliced window's exact source entry when its curve changes.

        The stored clock belongs to the old integral. Reusing its numeric time
        after editing speed or control points moves the source In. Carry its
        continuous source coordinate into the new integral instead, retaining
        the fractional phase that an integer curve_offset alone cannot express.
        """
        if (self.curve_time_offset is None or self.curve_length is None
                or (self.speed == updated.speed and self.speed_points == updated.speed_points)):
            return updated
        source_entry = self._curve_position_at_time(0.0, self.curve_length)
        return replace(updated, curve_time_offset=updated._curve_position_time(
            source_entry, self.curve_length))

    def with_speed(self, speed: float) -> SpeedParams:
        """
        创建新实例，修改速度。

        Args:
            speed: 新的速度值

        Returns:
            新的 SpeedParams 实例
        """
        return self._reanchor_edited_curve(SpeedParams(
            speed=speed,
            reverse=self.reverse,
            interpolation=self.interpolation,
            speed_points=self.speed_points,
            curve_offset=self.curve_offset,
            curve_length=self.curve_length,
            curve_time_offset=self.curve_time_offset,
            preserve_pitch=self.preserve_pitch,
        ))

    def with_reverse(self, reverse: bool) -> SpeedParams:
        """
        创建新实例，修改反向状态。

        Args:
            reverse: 是否反向播放

        Returns:
            新的 SpeedParams 实例
        """
        return SpeedParams(
            speed=self.speed,
            reverse=reverse,
            interpolation=self.interpolation,
            speed_points=self.speed_points,
            curve_offset=self.curve_offset,
            curve_length=self.curve_length,
            curve_time_offset=self.curve_time_offset,
            preserve_pitch=self.preserve_pitch,
        )

    def with_preserve_pitch(self, preserve_pitch: bool) -> SpeedParams:
        """
        创建新实例，修改音频保调变速（P1-08）。

        Args:
            preserve_pitch: True 时离线导出走 WSOLA 保调伸缩

        Returns:
            新的 SpeedParams 实例
        """
        return SpeedParams(
            speed=self.speed,
            reverse=self.reverse,
            interpolation=self.interpolation,
            speed_points=self.speed_points,
            curve_offset=self.curve_offset,
            curve_length=self.curve_length,
            curve_time_offset=self.curve_time_offset,
            preserve_pitch=preserve_pitch,
        )

    def with_interpolation(self, interpolation: str) -> SpeedParams:
        """
        创建新实例，修改插值方式。

        Args:
            interpolation: 新的插值方式

        Returns:
            新的 SpeedParams 实例
        """
        return SpeedParams(
            speed=self.speed,
            reverse=self.reverse,
            interpolation=interpolation,
            speed_points=self.speed_points,
            curve_offset=self.curve_offset,
            curve_length=self.curve_length,
            curve_time_offset=self.curve_time_offset,
            preserve_pitch=self.preserve_pitch,
        )

    def with_speed_point(self, source_offset: int, source_duration: int) -> "SpeedParams":
        """Split the current segment at a source-relative speed point.

        N01/C3-01：``source_offset`` 是窗口坐标；切片 clip 的点位存于曲
        线坐标，此处换算后存储。
        """
        offset = int(source_offset)
        if not 0 < offset < source_duration:
            raise ValueError("speed point must be inside the clip")
        curve_pos = self.curve_offset + offset
        if any(point.source_offset == curve_pos for point in self.speed_points):
            return self
        point = SpeedPoint(
            curve_pos, self.speed_at_source_offset(offset, source_duration))
        points = tuple(sorted((*self.speed_points, point), key=lambda item: item.source_offset))
        return self._reanchor_edited_curve(SpeedParams(
            self.speed, self.reverse, self.interpolation, points,
            curve_offset=self.curve_offset,
            curve_length=self.curve_length,
            curve_time_offset=self.curve_time_offset,
            preserve_pitch=self.preserve_pitch,
        ))

    def with_speed_point_speed(self, source_offset: int, speed: float) -> "SpeedParams":
        """Change the segment beginning at a previously added speed point.

        N01/C3-01：``source_offset`` 为窗口坐标，匹配时换算曲线坐标。
        """
        offset = int(source_offset)
        if offset == 0 and self.curve_offset == 0:
            return self.with_speed(speed)
        curve_pos = self.curve_offset + offset
        found = False
        points: list[SpeedPoint] = []
        for point in self.speed_points:
            if point.source_offset == curve_pos:
                points.append(SpeedPoint(curve_pos, float(speed), point.easing))
                found = True
            else:
                points.append(point)
        if not found:
            raise ValueError("speed point does not exist")
        return self._reanchor_edited_curve(SpeedParams(
            self.speed, self.reverse, self.interpolation, tuple(points),
            curve_offset=self.curve_offset,
            curve_length=self.curve_length,
            curve_time_offset=self.curve_time_offset,
            preserve_pitch=self.preserve_pitch,
        ))

    def with_point_easing(self, source_offset: int, easing: str) -> "SpeedParams":
        """A3.1: 修改某速度点的斜坡插值（linear | smooth）。

        N01/C3-01：``source_offset`` 为窗口坐标，匹配时换算曲线坐标。
        """
        offset = int(source_offset)
        curve_pos = self.curve_offset + offset
        found = False
        points: list[SpeedPoint] = []
        for point in self.speed_points:
            if point.source_offset == curve_pos:
                points.append(SpeedPoint(point.source_offset, point.speed, easing))
                found = True
            else:
                points.append(point)
        if not found:
            raise ValueError("speed point does not exist")
        return self._reanchor_edited_curve(SpeedParams(
            self.speed, self.reverse, self.interpolation, tuple(points),
            curve_offset=self.curve_offset,
            curve_length=self.curve_length,
            curve_time_offset=self.curve_time_offset,
            preserve_pitch=self.preserve_pitch,
        ))

    def without_speed_points(self) -> "SpeedParams":
        """Keep the base speed and remove all retime-control boundaries."""
        return SpeedParams(
            self.speed, self.reverse, self.interpolation,
            preserve_pitch=self.preserve_pitch,
        )

    # ==================== 序列化 ====================

    def to_dict(self) -> dict[str, Any]:
        """
        转换为字典格式，用于序列化。

        Returns:
            参数字典
        """
        return {
            "speed": self.speed,
            "reverse": self.reverse,
            "interpolation": self.interpolation,
            "speed_points": [point.to_dict() for point in self.speed_points],
            "curve_offset": self.curve_offset,
            "curve_length": self.curve_length,
            "curve_time_offset": self.curve_time_offset,
            "preserve_pitch": self.preserve_pitch,
            **self._extra_fields,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SpeedParams:
        """
        从字典创建实例。

        Args:
            data: 参数字典

        Returns:
            SpeedParams 实例
        """
        return SpeedParams(
            # AUD-16104: 反序列化统一强转（float/bool），与字段契约对齐
            speed=float(data.get("speed", 1.0)),
            reverse=bool(data.get("reverse", False)),
            interpolation=data.get("interpolation", "nearest"),
            speed_points=tuple(
                SpeedPoint.from_dict(point)
                for point in data.get("speed_points", ())
            ),
            curve_offset=int(data.get("curve_offset", 0) or 0),
            curve_length=(
                int(data["curve_length"])
                if data.get("curve_length") is not None else None
            ),
            preserve_pitch=bool(data.get("preserve_pitch", False)),
            curve_time_offset=(float(data["curve_time_offset"])
                               if data.get("curve_time_offset") is not None else None),
            _extra_fields=split_extras(data, SpeedParams._KNOWN_KEYS),
        )


@dataclass(frozen=True)
class RawParams:
    """
    TRAW（Topos RAW）非破坏 RAW 参数（批 2）。

    WB gain 为线性域乘性系数（debayer 后、归一化前应用）；exposure_stops
    为 EI/曝光补偿（+1 = ×2 线性）；display_ootf 为 v1 预览 OOTF 开关
    （gamma 2.2，仅查看语义——导出/调色管线应显式关闭）。

    Attributes:
        wb_gain_r: 红通道白平衡增益 [0.25, 4.0]
        wb_gain_g: 绿通道白平衡增益 [0.25, 4.0]
        wb_gain_b: 蓝通道白平衡增益 [0.25, 4.0]
        exposure_stops: 曝光补偿（stops）[-4.0, +4.0]
        display_ootf: 预览 OOTF 开关
    """

    wb_gain_r: float = 1.0
    wb_gain_g: float = 1.0
    wb_gain_b: float = 1.0
    exposure_stops: float = 0.0
    display_ootf: bool = True
    # RC8：black level（码值域，debayer 前减除；默认 0 = 不减除）。
    # TRWM 携带源值（RC1），此处为非破坏偏移语义的基座。
    black_level: float = 0.0
    # RC3：IDT 目标色空间（camera_gamut_matrices 预设 id；"native" =
    # 不变换——相机原生 RGB 直出）。用户显式选择优先于 TRWM 携带 id。
    color_space: str = "native"
    # RC3：查看伽马（"preview-2.2" 预览 OOTF / "srgb" / "linear"）。
    gamma: str = "preview-2.2"

    MIN_GAIN = 0.25
    MAX_GAIN = 4.0
    MIN_STOPS = -4.0
    MAX_STOPS = 4.0

    def __post_init__(self) -> None:
        for name in ("wb_gain_r", "wb_gain_g", "wb_gain_b",
                     "exposure_stops", "black_level"):
            _require_finite(name, float(getattr(self, name)))
        for name in ("wb_gain_r", "wb_gain_g", "wb_gain_b"):
            v = getattr(self, name)
            if not (self.MIN_GAIN <= v <= self.MAX_GAIN):
                raise ValueError(
                    f"{name} must be within [{self.MIN_GAIN}, {self.MAX_GAIN}]: {v}")
        if not (self.MIN_STOPS <= self.exposure_stops <= self.MAX_STOPS):
            raise ValueError(
                f"exposure_stops must be within [{self.MIN_STOPS}, "
                f"{self.MAX_STOPS}]: {self.exposure_stops}")
        if self.gamma not in ("preview-2.2", "srgb", "linear"):
            raise ValueError(f"gamma must be preview-2.2/srgb/linear: {self.gamma!r}")
        if not isinstance(self.color_space, str) or not self.color_space:
            raise ValueError(f"color_space must be a non-empty string: {self.color_space!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "wb_gain_r": self.wb_gain_r,
            "wb_gain_g": self.wb_gain_g,
            "wb_gain_b": self.wb_gain_b,
            "exposure_stops": self.exposure_stops,
            "display_ootf": self.display_ootf,
            "black_level": self.black_level,
            "color_space": self.color_space,
            "gamma": self.gamma,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "RawParams":
        return RawParams(
            wb_gain_r=float(data.get("wb_gain_r", 1.0)),
            wb_gain_g=float(data.get("wb_gain_g", 1.0)),
            wb_gain_b=float(data.get("wb_gain_b", 1.0)),
            exposure_stops=float(data.get("exposure_stops", 0.0)),
            display_ootf=bool(data.get("display_ootf", True)),
            black_level=float(data.get("black_level", 0.0)),
            color_space=str(data.get("color_space", "native")),
            gamma=str(data.get("gamma", "preview-2.2")),
        )
