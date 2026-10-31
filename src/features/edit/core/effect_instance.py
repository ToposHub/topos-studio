"""
Effect Instance Module - Effect data model for clip effects.

This module defines the data structure for effects applied to clips.
Effects are stacked on clips and processed in order during rendering.

Design Principles:
- Immutable data class (frozen=True) for thread safety
- Complete type annotations
- Independent of PyQt (pure Python data model)
- Parameter validation in __post_init__

Reference:
- docs/edit_page_timeline_architecture.md P0.B ADR

P5.3 - Keyframe Animation:
- AnimatableParam: Wrapper for animated parameters
- EffectInstance.animated_params: Dict of parameter keyframes
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, ClassVar
import uuid

# Import KeyframeTrack from shared module (Phase 2: Modularization)
from src.shared.keyframe import Keyframe, KeyframeTrack, InterpolationType

from src.shared.extra_fields import preserves_extras, split_extras


# ================================================================
# AnimatableParam - Wrapper for animated parameters (P5.3)
# ================================================================

# AUD-16801：frozen=True 落实模块头声明"Immutable data class (frozen=True)
# for thread safety"的不可变保证——所有变更走 add/remove/update_keyframe
# 的 copy-on-write（返回新实例），直接字段赋值会在赋值点抛 FrozenInstanceError。
@dataclass(frozen=True)
class AnimatableParam:
    """
    Animatable parameter wrapper.

    Wraps a single parameter that can be animated with keyframes.
    Stores both the base (static) value and keyframe track.

    P5.3 First version supports:
    - opacity (clip level)
    - transform.translate_x/y (clip level)
    - transform.scale_x/y (clip level)
    - effect float parameters

    Attributes:
        base_value: The static value when not animated
        keyframes: Optional KeyframeTrack for animation
        min_value: Minimum allowed value
        max_value: Maximum allowed value
    """

    base_value: float
    keyframes: Optional[KeyframeTrack] = None
    min_value: float = 0.0
    max_value: float = 1.0

    def is_animated(self) -> bool:
        """Check if this parameter has keyframes."""
        return self.keyframes is not None and len(self.keyframes.keyframes) > 0

    def shifted(self, delta: int) -> "AnimatableParam":
        """C13：关键帧随 clip 时间变换平移（copy-on-write）。"""
        from dataclasses import replace as _dc_replace

        if not self.is_animated() or delta == 0:
            return self
        return _dc_replace(self, keyframes=self.keyframes.shifted(delta))

    def time_scaled(self, factor) -> "AnimatableParam":
        """FE-B01：序列改 FPS 时缩放关键帧帧号（copy-on-write）。"""
        from dataclasses import replace as _dc_replace

        if not self.is_animated():
            return self
        return _dc_replace(self, keyframes=self.keyframes.time_scaled(factor))

    def get_value_at_frame(self, frame: int) -> float:
        """
        Get parameter value at specific frame.

        If animated, returns interpolated value from keyframes.
        Otherwise returns base_value.

        Args:
            frame: Timeline frame number

        Returns:
            Parameter value at frame
        """
        if self.is_animated():
            return self.keyframes.get_value(frame, self.base_value)
        return self.base_value

    def add_keyframe(
        self,
        frame: int,
        value: float,
        interpolation: InterpolationType = InterpolationType.LINEAR
    ) -> AnimatableParam:
        """
        Add a keyframe at specified frame.

        Args:
            frame: Frame number
            value: Value at this frame
            interpolation: Interpolation type

        Returns:
            New AnimatableParam with keyframe added
        """
        if self.keyframes is None:
            # Create new track
            track = KeyframeTrack(
                param_name="",
                min_val=self.min_value,
                max_val=self.max_value,
            )
        else:
            # AUD-16800：copy-on-write——KeyframeTrack.add_keyframe 原地
            # append/sort 共享列表，直接复用旧 track 会污染 undo/历史中
            # 仍引用同一 track 的旧 EffectInstance（新旧实例关键帧互相
            # 泄漏），且 UI 线程 sort 与渲染线程遍历在共享 list 上竞态。
            # 与 remove_keyframe/update_keyframe 一致：先复制再修改。
            track = KeyframeTrack(
                param_name=self.keyframes.param_name,
                keyframes=list(self.keyframes.keyframes),
                min_val=self.keyframes.min_val,
                max_val=self.keyframes.max_val,
                pre_extrap=self.keyframes.pre_extrap,
                post_extrap=self.keyframes.post_extrap,
            )

        new_kf = Keyframe(frame, value, interpolation)
        track.add_keyframe(new_kf)

        return AnimatableParam(
            base_value=self.base_value,
            keyframes=track,
            min_value=self.min_value,
            max_value=self.max_value,
        )

    def remove_keyframe(self, frame: int) -> AnimatableParam:
        """
        Remove keyframe at specified frame.

        Args:
            frame: Frame number

        Returns:
            New AnimatableParam with keyframe removed
        """
        if self.keyframes is None:
            return self

        new_track = KeyframeTrack(
            param_name=self.keyframes.param_name,
            keyframes=[kf for kf in self.keyframes.keyframes if kf.frame != frame],
            min_val=self.keyframes.min_val,
            max_val=self.keyframes.max_val,
        )

        # If no keyframes left, clear animation
        if len(new_track.keyframes) == 0:
            return AnimatableParam(
                base_value=self.base_value,
                keyframes=None,
                min_value=self.min_value,
                max_value=self.max_value,
            )

        return AnimatableParam(
            base_value=self.base_value,
            keyframes=new_track,
            min_value=self.min_value,
            max_value=self.max_value,
        )

    def update_keyframe(
        self, frame: int, value: float, interpolation: Optional[InterpolationType] = None
    ) -> AnimatableParam:
        """
        Update keyframe value (and optionally interpolation) at specified frame.

        Args:
            frame: Frame number
            value: New value
            interpolation: Optional new InterpolationType (easing curve). If
                None, the keyframe keeps its current interpolation.

        Returns:
            New AnimatableParam with keyframe updated
        """
        if self.keyframes is None:
            return self

        new_keyframes = []
        for kf in self.keyframes.keyframes:
            if kf.frame == frame:
                new_interp = interpolation if interpolation is not None else kf.interpolation
                new_keyframes.append(Keyframe(frame, value, new_interp))
            else:
                new_keyframes.append(kf)

        new_track = KeyframeTrack(
            param_name=self.keyframes.param_name,
            keyframes=new_keyframes,
            min_val=self.keyframes.min_val,
            max_val=self.keyframes.max_val,
        )

        return AnimatableParam(
            base_value=self.base_value,
            keyframes=new_track,
            min_value=self.min_value,
            max_value=self.max_value,
        )

    def update_keyframe_interpolation(
        self, frame: int, interpolation: InterpolationType
    ) -> AnimatableParam:
        """Change only the easing curve of the keyframe at ``frame``.

        Convenience wrapper around :meth:`update_keyframe` that preserves the
        existing value. Used when a user picks a different easing (e.g.
        linear → ease_in_out) for an existing keyframe.
        """
        if self.keyframes is None:
            return self
        for kf in self.keyframes.keyframes:
            if kf.frame == frame:
                return self.update_keyframe(frame, kf.value, interpolation)
        return self

    def has_keyframe_at(self, frame: int) -> bool:
        """Check if there's a keyframe at specific frame."""
        if self.keyframes is None:
            return False
        return self.keyframes.get_keyframe_at(frame) is not None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "base_value": self.base_value,
            "keyframes": self.keyframes.to_dict() if self.keyframes else None,
            "min_value": self.min_value,
            "max_value": self.max_value,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> AnimatableParam:
        """Deserialize from dictionary."""
        keyframes_data = data.get("keyframes")
        keyframes = KeyframeTrack.from_dict(keyframes_data) if keyframes_data else None

        return AnimatableParam(
            base_value=data.get("base_value", 0.0),
            keyframes=keyframes,
            min_value=data.get("min_value", 0.0),
            max_value=data.get("max_value", 1.0),
        )


@dataclass(frozen=True)
@preserves_extras
class EffectInstance:
    """
    Effect instance data model.

    Represents a single effect applied to a clip. Effects are processed
    in order (by the `order` field) during rendering.

    P5.3 - Keyframe Animation Support:
    - animated_params: Dict of parameter name -> AnimatableParam
    - with_keyframe_added/removed: Factory methods for keyframe operations
    - get_param_at_frame: Get interpolated value at specific frame

    Attributes:
        effect_id: Unique identifier for this effect instance
        effect_type: Type identifier (e.g., 'blur', 'sharpen', 'vignette')
        params: Effect parameters (dict of param name -> value, static values)
        animated_params: Animated parameters with keyframes (P5.3)
        is_enabled: Whether the effect is active
        order: Processing order (0 = first applied, higher = later)

    Example:
        >>> blur = EffectInstance(
        ...     effect_type='gaussian_blur',
        ...     params={'intensity': 50.0, 'radius': 5.0}
        ... )
        >>> blur.effect_id  # Auto-generated UUID
        >>> blur.params['intensity']
        50.0
    """

    effect_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    effect_type: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    animated_params: dict[str, AnimatableParam] = field(default_factory=dict)  # P5.3
    is_enabled: bool = True
    order: int = 0
    plugin_identity: Optional[dict[str, Any]] = None  # FX-M07: stable plugin identity snapshot
    # DESK-04A：未来 schema 未知键保留（load→save 不丢）
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    # to_dict 全部键（含条件写出的 plugin_identity）+ from_dict 消费键；
    # legacy 迁移（intensity）发生在 params 内层字典，不产生顶层迁移键
    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset({
        "effect_id", "effect_type", "params", "animated_params",
        "is_enabled", "order", "plugin_identity",
    })

    def with_animated_params_remapped(self, anchor: int, ratio) -> "EffectInstance":
        """F2.4：效果参数关键帧按 (anchor, ratio) 重映射（变速重锚）。"""
        if not self.animated_params:
            return self
        from dataclasses import replace as _dc_replace

        return _dc_replace(
            self,
            animated_params={
                name: (
                    param.shifted(-anchor).time_scaled(ratio).shifted(anchor)
                    if hasattr(param, "time_scaled") and param.is_animated()
                    else param
                )
                for name, param in self.animated_params.items()
            },
        )

    def with_animated_params_shifted(self, delta: int) -> "EffectInstance":
        """C13：效果参数关键帧随 clip 时间变换平移（copy-on-write）。"""
        if delta == 0 or not self.animated_params:
            return self
        from dataclasses import replace as _dc_replace

        return _dc_replace(
            self,
            animated_params={
                name: param.shifted(delta) if hasattr(param, "shifted") else param
                for name, param in self.animated_params.items()
            },
        )

    def with_animated_params_scaled(self, factor) -> "EffectInstance":
        """FE-B01：效果参数关键帧随序列 FPS 变化缩放帧号。"""
        if not self.animated_params:
            return self
        from dataclasses import replace as _dc_replace

        return _dc_replace(
            self,
            animated_params={
                name: param.time_scaled(factor)
                if hasattr(param, "time_scaled") else param
                for name, param in self.animated_params.items()
            },
        )

    def __post_init__(self) -> None:
        """Validate effect parameters."""
        if not self.effect_id:
            raise ValueError("effect_id cannot be empty")
        if not self.effect_type:
            raise ValueError("effect_type cannot be empty")
        if self.order < 0:
            raise ValueError(f"order cannot be negative: {self.order}")

    # ==================== Keyframe Methods (P5.3) ====================

    def get_param_at_frame(self, param_name: str, frame: int) -> Any:
        """
        Get parameter value at specific frame.

        If parameter is animated, returns interpolated value.
        Otherwise returns static params value.

        Args:
            param_name: Parameter name
            frame: Timeline frame number

        Returns:
            Parameter value at frame
        """
        if param_name in self.animated_params:
            return self.animated_params[param_name].get_value_at_frame(frame)
        return self.params.get(param_name, 0.0)

    def is_param_animated(self, param_name: str) -> bool:
        """Check if parameter has keyframes."""
        if param_name not in self.animated_params:
            return False
        return self.animated_params[param_name].is_animated()

    def has_keyframe_at(self, param_name: str, frame: int) -> bool:
        """Check if there's a keyframe at specific frame."""
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
        max_value: float = 100.0,
    ) -> EffectInstance:
        """
        Create new instance with keyframe added.

        Args:
            param_name: Parameter name
            frame: Frame number
            value: Value at this frame
            interpolation: Interpolation type
            min_value: Minimum value (for new AnimatableParam)
            max_value: Maximum value (for new AnimatableParam)

        Returns:
            New EffectInstance with keyframe added
        """
        if param_name in self.animated_params:
            anim_param = self.animated_params[param_name].add_keyframe(frame, value, interpolation)
        else:
            # Create new AnimatableParam
            base_value = self.params.get(param_name, value)
            anim_param = AnimatableParam(
                base_value=base_value,
                min_value=min_value,
                max_value=max_value,
            ).add_keyframe(frame, value, interpolation)

        new_animated = dict(self.animated_params)
        new_animated[param_name] = anim_param

        return EffectInstance(
            effect_id=self.effect_id,
            effect_type=self.effect_type,
            params=self.params,
            animated_params=new_animated,
            is_enabled=self.is_enabled,
            order=self.order,
            plugin_identity=self.plugin_identity,
        )

    def with_keyframe_removed(self, param_name: str, frame: int) -> EffectInstance:
        """
        Create new instance with keyframe removed.

        Args:
            param_name: Parameter name
            frame: Frame number

        Returns:
            New EffectInstance with keyframe removed
        """
        if param_name not in self.animated_params:
            return self

        anim_param = self.animated_params[param_name].remove_keyframe(frame)

        new_animated = dict(self.animated_params)

        if not anim_param.is_animated():
            # Remove from animated_params if no keyframes left
            del new_animated[param_name]
        else:
            new_animated[param_name] = anim_param

        return EffectInstance(
            effect_id=self.effect_id,
            effect_type=self.effect_type,
            params=self.params,
            animated_params=new_animated,
            is_enabled=self.is_enabled,
            order=self.order,
            plugin_identity=self.plugin_identity,
        )

    def with_keyframe_updated(self, param_name: str, frame: int, value: float) -> EffectInstance:
        """
        Create new instance with keyframe value updated.

        Args:
            param_name: Parameter name
            frame: Frame number
            value: New value

        Returns:
            New EffectInstance with keyframe updated
        """
        if param_name not in self.animated_params:
            return self

        anim_param = self.animated_params[param_name].update_keyframe(frame, value)

        new_animated = dict(self.animated_params)
        new_animated[param_name] = anim_param

        return EffectInstance(
            effect_id=self.effect_id,
            effect_type=self.effect_type,
            params=self.params,
            animated_params=new_animated,
            is_enabled=self.is_enabled,
            order=self.order,
            plugin_identity=self.plugin_identity,
        )

    # ==================== Factory Methods ====================

    def with_param(self, param_name: str, value: Any) -> EffectInstance:
        """
        Create new instance with updated parameter.

        Args:
            param_name: Parameter name to update
            value: New parameter value

        Returns:
            New EffectInstance with updated param
        """
        new_params = dict(self.params)
        new_params[param_name] = value
        return EffectInstance(
            effect_id=self.effect_id,
            effect_type=self.effect_type,
            params=new_params,
            animated_params=self.animated_params,
            is_enabled=self.is_enabled,
            order=self.order,
            plugin_identity=self.plugin_identity,
        )

    def with_params(self, params: dict[str, Any]) -> EffectInstance:
        """
        Create new instance with all parameters replaced.

        Args:
            params: New parameters dict

        Returns:
            New EffectInstance with new params
        """
        return EffectInstance(
            effect_id=self.effect_id,
            effect_type=self.effect_type,
            params=params,
            animated_params=self.animated_params,
            is_enabled=self.is_enabled,
            order=self.order,
            plugin_identity=self.plugin_identity,
        )

    def with_enabled(self, enabled: bool) -> EffectInstance:
        """
        Create new instance with enabled state changed.

        Args:
            enabled: New enabled state

        Returns:
            New EffectInstance with new enabled state
        """
        return EffectInstance(
            effect_id=self.effect_id,
            effect_type=self.effect_type,
            params=self.params,
            animated_params=self.animated_params,
            is_enabled=enabled,
            order=self.order,
            plugin_identity=self.plugin_identity,
        )

    def with_effect_id(self, effect_id: str) -> EffectInstance:
        """Create a new instance carrying a different effect_id.

        Used by ``AddEffectCommand`` when a caller pre-generates the id
        (e.g. the timeline drag-drop path needs the id *before* apply so it
        can scroll the properties panel to the new effect's section). All
        other fields are preserved.
        """
        return EffectInstance(
            effect_id=effect_id,
            effect_type=self.effect_type,
            params=self.params,
            animated_params=self.animated_params,
            is_enabled=self.is_enabled,
            order=self.order,
            plugin_identity=self.plugin_identity,
        )

    def with_order(self, order: int) -> EffectInstance:
        """
        Create new instance with new processing order.

        Args:
            order: New order value

        Returns:
            New EffectInstance with new order
        """
        return EffectInstance(
            effect_id=self.effect_id,
            effect_type=self.effect_type,
            params=self.params,
            animated_params=self.animated_params,
            is_enabled=self.is_enabled,
            order=order,
            plugin_identity=self.plugin_identity,
        )

    def with_plugin_identity(self, identity: Optional[dict[str, Any]]) -> EffectInstance:
        """Create new instance with updated plugin identity (FX-M07).

        Used by the migration command to backfill identity on legacy
        projects, and by the re-bind flow when a plugin is updated.

        Args:
            identity: New plugin_identity dict, or None to clear.

        Returns:
            New EffectInstance with the given identity.
        """
        return EffectInstance(
            effect_id=self.effect_id,
            effect_type=self.effect_type,
            params=self.params,
            animated_params=self.animated_params,
            is_enabled=self.is_enabled,
            order=self.order,
            plugin_identity=identity,
        )

    # ==================== Serialization ====================

    def to_dict(self) -> dict[str, Any]:
        """
        Convert to dictionary for serialization.

        Returns:
            Dict representation of effect
        """
        animated_params_dict = {
            name: param.to_dict()
            for name, param in self.animated_params.items()
        }
        result = {
            "effect_id": self.effect_id,
            "effect_type": self.effect_type,
            "params": self.params,
            "animated_params": animated_params_dict,
            "is_enabled": self.is_enabled,
            "order": self.order,
        }
        # FX-M07: include plugin identity only when present (backward compat)
        if self.plugin_identity is not None:
            result["plugin_identity"] = self.plugin_identity
        # DESK-04A：未知键原样回写
        result.update(self._extra_fields)
        return result

    @staticmethod
    def from_dict(data: dict[str, Any]) -> EffectInstance:
        """
        Create instance from dictionary.

        Args:
            data: Dict representation

        Returns:
            EffectInstance
        """
        animated_params_data = data.get("animated_params", {})
        animated_params = {
            name: AnimatableParam.from_dict(param_data)
            for name, param_data in animated_params_data.items()
        }
        effect_type = data.get("effect_type", "")
        params = dict(data.get("params", {}))

        # ── Legacy effect_type migration ─────────────────────────────
        # Older projects stored effects under hardcoded Edit-only type
        # ids (gaussian_blur) with a different parameter schema
        # (intensity 0-100). The unified EffectCatalog now exposes the
        # composite node's spec ("blur") with the composite parameter
        # set (blur_type / radius_x / radius_y / mix / ...). Map the old
        # ids here so legacy projects load with a sensible result:
        #   gaussian_blur → blur, blur_type=gaussian, mix = intensity/100
        # Sharpen kept its effect_type ("sharpen"), but the parameter
        # set changed (intensity → amount); map the value through.
        if effect_type == "gaussian_blur":
            effect_type = "blur"
            intensity = float(params.pop("intensity", 0.0) or 0.0)
            params.setdefault("blur_type", "gaussian")
            params.setdefault("mix", max(0.0, min(1.0, intensity / 100.0)))
            # Legacy "radius" already matches the new schema's radius.
        elif effect_type == "sharpen" and "intensity" in params:
            # Old schema had intensity(0-100) + radius; new schema uses
            # amount(0-10). Map intensity → amount at a conservative scale.
            intensity = float(params.pop("intensity", 0.0) or 0.0)
            params.setdefault("amount", max(0.0, min(10.0, intensity / 10.0)))

        return EffectInstance(
            effect_id=data.get("effect_id", str(uuid.uuid4())),
            effect_type=effect_type,
            params=params,
            animated_params=animated_params,
            is_enabled=data.get("is_enabled", True),
            order=data.get("order", 0),
            plugin_identity=data.get("plugin_identity", None),  # FX-M07
            _extra_fields=split_extras(data, EffectInstance._KNOWN_KEYS),
        )

    # ==================== Comparison ====================

    def __hash__(self) -> int:
        return hash(self.effect_id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, EffectInstance):
            return NotImplemented
        return self.effect_id == other.effect_id


@dataclass(frozen=True)
class EffectDefinition:
    """
    Effect definition - describes an effect type and its parameters.

    This is the "blueprint" for creating EffectInstance objects.
    It defines what parameters an effect has and their default values.

    Attributes:
        effect_type: Unique type identifier
        name: Human-readable name (for UI display)
        category: Category for grouping (e.g., 'blur', 'color', 'transform')
        description: Short description of what the effect does
        params_schema: Schema for parameters (param_name -> ParamSchema)
        icon: Optional icon name for UI

    Example:
        >>> blur_def = EffectDefinition(
        ...     effect_type='gaussian_blur',
        ...     name='Gaussian Blur',
        ...     category='blur',
        ...     params_schema={
        ...         'intensity': ParamSchema(
        ...             type='float',
        ...             default=0.0,
        ...             min_value=0.0,
        ...             max_value=100.0,
        ...             label='Intensity'
        ...         )
        ...     }
        ... )
    """

    effect_type: str
    name: str = ""
    category: str = ""
    description: str = ""
    params_schema: dict[str, ParamSchema] = field(default_factory=dict)
    icon: str = ""

    def __post_init__(self) -> None:
        """Validate effect definition."""
        if not self.effect_type:
            raise ValueError("effect_type cannot be empty")
        if not self.name:
            raise ValueError("name cannot be empty")

    def create_instance(self, order: int = 0) -> EffectInstance:
        """
        Create an EffectInstance from this definition.

        Args:
            order: Processing order for the instance

        Returns:
            EffectInstance with default params
        """
        default_params = {
            name: schema.default
            for name, schema in self.params_schema.items()
        }
        return EffectInstance(
            effect_type=self.effect_type,
            params=default_params,
            is_enabled=True,
            order=order,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize definition."""
        return {
            "effect_type": self.effect_type,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "params_schema": {
                name: schema.to_dict()
                for name, schema in self.params_schema.items()
            },
            "icon": self.icon,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> EffectDefinition:
        """Deserialize definition."""
        params_schema_data = data.get("params_schema", {})
        params_schema = {
            name: ParamSchema.from_dict(schema_data)
            for name, schema_data in params_schema_data.items()
        }
        return EffectDefinition(
            effect_type=data.get("effect_type", ""),
            name=data.get("name", ""),
            category=data.get("category", ""),
            description=data.get("description", ""),
            params_schema=params_schema,
            icon=data.get("icon", ""),
        )


@dataclass(frozen=True)
class ParamSchema:
    """
    Parameter schema - describes a single effect parameter.

    Attributes:
        type: Parameter type ('float', 'int', 'bool', 'color', 'choice')
        default: Default value
        min_value: Minimum value (for float/int)
        max_value: Maximum value (for float/int)
        step: Step value for UI slider (for float/int)
        label: Human-readable label
        choices: Available choices (for 'choice' type)
        unit: Unit suffix (e.g., '%', 'px')
    """

    type: str = "float"
    default: Any = 0.0
    min_value: float = 0.0
    max_value: float = 100.0
    step: float = 1.0
    label: str = ""
    choices: tuple[str, ...] = ()
    unit: str = ""

    def __post_init__(self) -> None:
        """Validate schema."""
        valid_types = ("float", "int", "bool", "color", "choice", "string")
        if self.type not in valid_types:
            raise ValueError(f"Invalid param type: {self.type}")

        if self.type == "choice" and not self.choices:
            raise ValueError("choice type requires choices")

    def to_dict(self) -> dict[str, Any]:
        """Serialize schema."""
        return {
            "type": self.type,
            "default": self.default,
            "min_value": self.min_value,
            "max_value": self.max_value,
            "step": self.step,
            "label": self.label,
            "choices": list(self.choices),
            "unit": self.unit,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ParamSchema:
        """Deserialize schema."""
        return ParamSchema(
            type=data.get("type", "float"),
            default=data.get("default", 0.0),
            min_value=data.get("min_value", 0.0),
            max_value=data.get("max_value", 100.0),
            step=data.get("step", 1.0),
            label=data.get("label", ""),
            choices=tuple(data.get("choices", [])),
            unit=data.get("unit", ""),
        )