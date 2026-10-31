"""
Color Stack Data Module - 调色栈数据模型 (P9.1)

每个调色栈包含完整的调色参数，可以被多个 ClipInstance 引用。
同一素材的不同 clip 可以选择独立调色（各自 color_stack_id）
或共享素材调色（color_shared=True，使用 asset 的默认调色栈）。

契约（P0-COLOR-02/03 统一后）：
- ``layers`` / ``active_layer_index`` / ``raw_params`` 是**权威完整状态**
  （完整 Layer 快照，含蒙版/限定器/变形器/效果/插件/混合/烘焙等）；
  顶层平铺键（grading_params 等）保留为向后兼容的精简视图。
- 读写双方约定：持久化边界（保存/切换 flush）必须刷新 layers 快照；
  读取时 layers 非空则按完整恢复，否则回退平铺键（旧工程）。
- 未知键在读端忽略；schema_version 缺失视为 1（无 layers 的旧格式）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, ClassVar
import copy
import uuid
import time

from src.shared.extra_fields import preserves_extras, split_extras

# 调色栈序列化 schema 版本：
# v1 - 仅平铺精简键（grading/curves/hsl/mixer/soft_clip/lut/trackings）
# v2 - 新增完整 Layer 快照（layers/active_layer_index/raw_params）
# v3 - 平铺视图新增曲线面板侧栏键（curves_soft_clip / curves_edit，见
#      src/features/color/core/curves_soft_clip.py）。权威状态始终是
#      Layer 快照内的同名字段；平铺键只服务旧格式回退与面板直读。
#      读端不分支版本号（缺失键 = 默认恒等），写端恒写当前版本。
COLOR_STACK_SCHEMA_VERSION = 3


@dataclass(frozen=True)
@preserves_extras
class ColorStackData:
    """
    调色栈数据 (P9.1)。

    每个调色栈包含完整的调色参数，可以被多个 ClipInstance 引用。
    同一素材的不同 clip 可以选择独立调色（各自 color_stack_id）
    或共享素材调色（color_shared=True，使用 asset 的默认调色栈）。

    Attributes:
        stack_id: 调色栈唯一标识符（UUID）
        asset_id: 关联素材 ID（可选，用于素材默认调色栈）
        name: 调色栈名称（用于 UI 显示）
        grading_params: 调色参数字典（精简兼容视图）
        curves_data: 曲线数据
        hsl_curve_data: HSL 曲线数据
        mixer_params: 颜色混合参数
        soft_clip_params: Soft Clip 参数
        lut_path: 当前使用的 LUT 路径
        lut_intensity: LUT 强度（0.0-1.0）
        created_at: 创建时间戳
        modified_at: 最后修改时间戳
        layers: 完整 Layer 快照（权威状态；空 = 旧格式/未捕获）
        active_layer_index: 活动图层索引（与 layers 配套）
        raw_params: RAW 解码参数（engine 级状态）
    """
    stack_id: str
    asset_id: str | None = None
    name: str = "Default"
    grading_params: dict[str, Any] = field(default_factory=lambda: {
        "lift": (0, 0, 0),
        "gamma": (0, 0, 0),
        "gain": (0, 0, 0),
        "exposure": 0,
        "temp": 0,
        "tint": 0,
        "contrast": 0,
        "shadow": 0,
        "highlights": 0,
        "saturation": 0,
        "vibrance": 0,
        "mid_detail": 0,
        "sharpness": 0,
        "hue_shift": 0,
        "lift_saturation": 0,
        "gamma_saturation": 0,
        "gain_saturation": 0
    })
    curves_data: dict[str, Any] = field(default_factory=dict)
    hsl_curve_data: dict[str, Any] = field(default_factory=dict)
    mixer_params: dict[str, Any] = field(default_factory=dict)
    soft_clip_params: dict[str, Any] = field(default_factory=lambda: {
        "enabled": False,
        "high": 1.0,
        "white_point": 1.1,
        "shoulder_strength": 1.0,
        "low": 0.0,
        "black_point": -0.1,
        "toe_strength": 1.0,
        "exposure": 0.0,
    })
    # Schema v3：曲线面板侧栏的平铺精简键（曲线工具四参数柔化裁切 +
    # Edit 强度）。{} = 恒等默认（旧工程缺失键视觉不变）；语义模型见
    # src/features/color/core/curves_soft_clip.py。
    curves_soft_clip: dict[str, Any] = field(default_factory=dict)
    curves_edit: dict[str, Any] = field(default_factory=dict)
    lut_path: str | None = None
    lut_intensity: float = 1.0
    created_at: float = 0.0
    modified_at: float = 0.0
    # Schema v2: ownership fields
    scope: str = "clip_local"
    source_kind: str = "timeline_clip"
    timeline_id: str | None = None
    clip_id: str | None = None
    # 蒙版跟踪数据（TrackingStore 序列化，含 schema_version；旧工程无此键时为 {}）
    trackings: dict[str, Any] = field(default_factory=dict)
    # Schema v2: 完整 Layer 快照（权威状态）
    layers: tuple[dict[str, Any], ...] = ()
    active_layer_index: int = 0
    raw_params: dict[str, Any] = field(default_factory=dict)
    # P1-COLOR-13: per-clip 视频降噪参数（VideoDenoiseParams.to_dict；
    # None = 该 clip 未启用，切 clip 时据此重置引擎会话防串扰）
    video_denoise: dict[str, Any] | None = None
    # DESK-04A：未来 schema 未知键保留（load→save 不丢）
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    # to_dict 全部键（含条件写出的 layers/active_layer_index/raw_params/
    # video_denoise）+ from_dict 消费键——两者一致，无额外 legacy 迁移键
    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "schema_version", "stack_id", "asset_id", "name",
        "grading_params", "curves_data", "hsl_curve_data", "mixer_params",
        "soft_clip_params", "curves_soft_clip", "curves_edit",
        "lut_path", "lut_intensity",
        "created_at", "modified_at", "scope", "source_kind",
        "timeline_id", "clip_id", "trackings",
        "layers", "active_layer_index", "raw_params", "video_denoise",
    })

    def __post_init__(self) -> None:
        """验证调色栈数据有效性"""
        if not self.stack_id:
            raise ValueError("stack_id cannot be empty")
        if self.lut_intensity < 0.0 or self.lut_intensity > 1.0:
            raise ValueError("lut_intensity must be between 0.0 and 1.0")
        if self.layers and not isinstance(self.layers, tuple):
            object.__setattr__(self, "layers", tuple(self.layers))

    @staticmethod
    def create(asset_id: str | None = None, name: str = "Default") -> ColorStackData:
        """
        创建新的调色栈。

        Args:
            asset_id: 关联素材 ID（可选）
            name: 调色栈名称

        Returns:
            新的 ColorStackData 实例
        """
        now = time.time()
        return ColorStackData(
            stack_id=str(uuid.uuid4()),
            asset_id=asset_id,
            name=name,
            created_at=now,
            modified_at=now,
        )

    @staticmethod
    def create_standalone_image(asset_id: str, name: str = "Image Grade") -> ColorStackData:
        """
        创建 standalone image 调色栈 (schema v2)。

        Args:
            asset_id: 关联素材 ID
            name: 调色栈名称

        Returns:
            新的 ColorStackData 实例
        """
        now = time.time()
        return ColorStackData(
            stack_id=str(uuid.uuid4()),
            asset_id=asset_id,
            name=name,
            scope="asset_default",
            source_kind="standalone_image",
            timeline_id=None,
            clip_id=None,
            created_at=now,
            modified_at=now,
        )

    @staticmethod
    def create_timeline_clip(
        asset_id: str,
        timeline_id: str,
        clip_id: str,
        name: str = "Clip Grade",
    ) -> ColorStackData:
        """
        创建 timeline clip 调色栈 (schema v2)。

        Args:
            asset_id: 关联素材 ID
            timeline_id: 关联时间线 ID
            clip_id: 关联 Clip ID

        Returns:
            新的 ColorStackData 实例
        """
        now = time.time()
        return ColorStackData(
            stack_id=str(uuid.uuid4()),
            asset_id=asset_id,
            name=name,
            scope="clip_local",
            source_kind="timeline_clip",
            timeline_id=timeline_id,
            clip_id=clip_id,
            created_at=now,
            modified_at=now,
        )

    # with_* 工厂统一走 dataclasses.replace：新增字段自动随所有更新携带，
    # 避免逐字段手写构造漏带（历史上 layers 这类新字段最容易这样丢）。
    #
    # 【快照冻结契约】with_* 是"提交边界"：入参容器一律 deepcopy，与
    # 调用方的活对象彻底脱钩。Layer.to_dict 对 grading_params/
    # qualifier_params/soft_clip_params 等可变容器是按引用放进快照的，
    # 而 image_engine 的 set_qualifier_params/set_soft_clip_params 会
    # 对活动层 dict 做原地 update()——不冻结的话，提交后的"不可变"
    # 工程快照与撤销基线（_old_layers）会随面板拖动被回溯改写，qualifier/
    # soft_clip 的撤销因此静默失效（HistoryManager._serialize_layers 的
    # deepcopy 是同一问题的另一处补丁）。

    def with_grading_params(self, params: dict[str, Any]) -> ColorStackData:
        """创建新实例，更新调色参数"""
        return replace(self, grading_params=copy.deepcopy(params), modified_at=time.time())

    def with_curves(self, curves: dict[str, Any]) -> ColorStackData:
        """创建新实例，更新曲线数据"""
        return replace(self, curves_data=copy.deepcopy(curves), modified_at=time.time())

    def with_hsl_curves(self, hsl_curves: dict[str, Any]) -> ColorStackData:
        """创建新实例，更新 HSL 曲线数据"""
        return replace(self, hsl_curve_data=copy.deepcopy(hsl_curves), modified_at=time.time())

    def with_mixer_params(self, mixer_params: dict[str, Any]) -> ColorStackData:
        """创建新实例，更新颜色混合参数"""
        return replace(self, mixer_params=copy.deepcopy(mixer_params), modified_at=time.time())

    def with_soft_clip_params(self, soft_clip_params: dict[str, Any]) -> ColorStackData:
        """创建新实例，更新 Soft Clip 参数"""
        return replace(self, soft_clip_params=copy.deepcopy(soft_clip_params), modified_at=time.time())

    def with_curves_soft_clip(self, curves_soft_clip: dict[str, Any]) -> ColorStackData:
        """创建新实例，更新曲线面板四参数柔化裁切（schema v3 平铺键）"""
        return replace(self, curves_soft_clip=copy.deepcopy(curves_soft_clip), modified_at=time.time())

    def with_curves_edit(self, curves_edit: dict[str, Any]) -> ColorStackData:
        """创建新实例，更新曲线面板 Edit 强度（schema v3 平铺键）"""
        return replace(self, curves_edit=copy.deepcopy(curves_edit), modified_at=time.time())

    def with_lut(self, lut_path: str | None, intensity: float = 1.0) -> ColorStackData:
        """创建新实例，更新 LUT"""
        return replace(
            self, lut_path=lut_path, lut_intensity=intensity, modified_at=time.time()
        )

    def with_name(self, name: str) -> ColorStackData:
        """创建新实例，更新名称（不更新 modified_at：重命名不是内容修改）"""
        return replace(self, name=name)

    def with_video_denoise(
            self, video_denoise: dict[str, Any] | None) -> ColorStackData:
        """创建新实例，更新 per-clip 视频降噪参数（None = 清除）。"""
        return replace(
            self, video_denoise=video_denoise, modified_at=time.time())

    def with_trackings(self, trackings: dict[str, Any]) -> ColorStackData:
        """创建新实例，更新蒙版跟踪数据（TrackingStore 序列化字典）"""
        return replace(self, trackings=copy.deepcopy(trackings), modified_at=time.time())

    def with_layers(
        self,
        layers: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        active_layer_index: int = 0,
        raw_params: dict[str, Any] | None = None,
    ) -> ColorStackData:
        """创建新实例，刷新完整 Layer 快照（权威状态）。

        持久化边界（保存 / 片段切换 flush）调用；平铺精简键应同时从
        快照派生，保证两套视图一致。快照 deepcopy 冻结（见上方类注释）。
        """
        return replace(
            self,
            layers=tuple(copy.deepcopy(layers)),
            active_layer_index=int(active_layer_index),
            raw_params=copy.deepcopy(raw_params) if raw_params is not None else self.raw_params,
            modified_at=time.time(),
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化到字典。

        layers/raw_params 非空才写（保持旧工程输出形状稳定）；
        schema_version 恒写，读端按版本走迁移分支。
        """
        data: dict[str, Any] = {
            "schema_version": COLOR_STACK_SCHEMA_VERSION,
            "stack_id": self.stack_id,
            "asset_id": self.asset_id,
            "name": self.name,
            "grading_params": self.grading_params,
            "curves_data": self.curves_data,
            "hsl_curve_data": self.hsl_curve_data,
            "mixer_params": self.mixer_params,
            "soft_clip_params": self.soft_clip_params,
            "lut_path": self.lut_path,
            "lut_intensity": self.lut_intensity,
            "created_at": self.created_at,
            "modified_at": self.modified_at,
            "scope": self.scope,
            "source_kind": self.source_kind,
            "timeline_id": self.timeline_id,
            "clip_id": self.clip_id,
            "trackings": self.trackings,
        }
        # v3 侧栏键：非空才写（未触碰侧栏的旧工程输出形状保持 v2 稳定）
        if self.curves_soft_clip:
            data["curves_soft_clip"] = self.curves_soft_clip
        if self.curves_edit:
            data["curves_edit"] = self.curves_edit
        if self.layers:
            data["layers"] = [dict(layer) for layer in self.layers]
            data["active_layer_index"] = self.active_layer_index
        if self.raw_params:
            data["raw_params"] = self.raw_params
        if self.video_denoise:
            data["video_denoise"] = self.video_denoise
        # DESK-04A：未知键原样回写（不覆盖已知键——extras 只含未知键）
        data.update(self._extra_fields)
        return data

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ColorStackData:
        """从字典反序列化。

        未知键忽略；schema_version 缺失按 v1（无 layers）处理。
        """
        layers = data.get("layers") or ()
        raw_params = data.get("raw_params") or {}
        return ColorStackData(
            stack_id=data.get("stack_id", str(uuid.uuid4())),
            asset_id=data.get("asset_id"),
            name=data.get("name", "Default"),
            grading_params=data.get("grading_params", {}),
            curves_data=data.get("curves_data", {}),
            hsl_curve_data=data.get("hsl_curve_data", {}),
            mixer_params=data.get("mixer_params", {}),
            soft_clip_params=data.get("soft_clip_params", {}),
            curves_soft_clip=(
                data.get("curves_soft_clip")
                if isinstance(data.get("curves_soft_clip"), dict) else {}),
            curves_edit=(
                data.get("curves_edit")
                if isinstance(data.get("curves_edit"), dict) else {}),
            lut_path=data.get("lut_path"),
            lut_intensity=data.get("lut_intensity", 1.0),
            created_at=data.get("created_at", 0.0),
            modified_at=data.get("modified_at", 0.0),
            scope=data.get("scope", "clip_local"),
            source_kind=data.get("source_kind", "timeline_clip"),
            timeline_id=data.get("timeline_id"),
            clip_id=data.get("clip_id"),
            trackings=data.get("trackings", {}),
            layers=tuple(layers) if isinstance(layers, (list, tuple)) else (),
            active_layer_index=int(data.get("active_layer_index", 0) or 0),
            raw_params=raw_params if isinstance(raw_params, dict) else {},
            video_denoise=(
                data.get("video_denoise")
                if isinstance(data.get("video_denoise"), dict) else None),
            _extra_fields=split_extras(data, ColorStackData._KNOWN_KEYS),
        )

    def __hash__(self) -> int:
        """哈希值，基于 stack_id"""
        return hash(self.stack_id)

    def __eq__(self, other: object) -> bool:
        """相等比较，基于 stack_id"""
        if not isinstance(other, ColorStackData):
            return NotImplemented
        return self.stack_id == other.stack_id

    def __repr__(self) -> str:
        """详细表示"""
        return (
            f"ColorStackData(stack_id={self.stack_id!r}, "
            f"asset_id={self.asset_id!r}, "
            f"name={self.name!r})"
        )
