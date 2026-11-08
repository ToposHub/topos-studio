"""Clip param / keyframe / effect-stack commands (P6 split, moved verbatim).

Extracted from commands/__init__.py: params_keyfx cluster (clip param
updates, param keyframe add / remove / update / interpolation, and clip
effect stack add / remove / param-update / enable / reorder). The shared
track-lock guard assert_track_editable lives in _support.py; the cluster
needed no private helpers of its own. Class sources are byte-identical to
the original module; to_dict/from_dict keys are a persistence contract —
do not rename.
"""


from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
import logging
import uuid

from src.features.edit.core.timeline_v2 import TimelineSequence, ClipInstance

from ._support import assert_track_editable

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from src.shared.keyframe import InterpolationType

logger = logging.getLogger(__name__)

# ================================================================
# UpdateClipParamCommand
# ================================================================

from src.features.edit.core.clip_params import TransformParams, CropParams, SpeedParams, RawParams


@dataclass(frozen=True)
class UpdateClipParamCommand:
    """
    Command to update a clip parameter.

    Supports updating transform, crop, speed, opacity, and other clip parameters.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track containing the clip
        clip_id: Clip ID to update
        param_type: Parameter type ('transform', 'crop', 'speed', 'opacity', 'is_enabled')
        new_value: New parameter value
        old_value: Old parameter value (for undo)
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    param_type: str  # 'transform', 'crop', 'speed', 'opacity', 'is_enabled'
    new_value: Any  # TransformParams, CropParams, SpeedParams, float, bool
    old_value: Any | None = None  # Set during apply or provided

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: update clip parameter.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with clip parameter updated
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # R01/RI-03：属性面板 Transform/Opacity/RAW 等经此命令直写模型，
        # 此前无轨锁守卫（审计实测锁轨 X 55→77 仍生效）。菜单/快捷键已
        # 有守卫，领域层统一拒绝保证 AI/脚本/API 入口同策略。
        assert_track_editable(timeline, self.track_id, "Update clip param")

        # N11/T3-03：调用方未携带 old_value 时（facade 缺命令时的回退
        # 路径、脚本直用），在 apply 时刻从被替换前的 clip 捕获真实旧
        # 值。此前 undo 在 old_value 为 None 时读"当前值"——apply 之后
        # 当前值已是新值，Undo 变 no-op，历史弹掉而内容不恢复。
        if self.old_value is None:
            if self.param_type == "transform":
                object.__setattr__(self, "old_value", clip.transform)
            elif self.param_type == "crop":
                object.__setattr__(self, "old_value", clip.crop)
            elif self.param_type == "speed":
                object.__setattr__(self, "old_value", clip.speed)
            elif self.param_type == "opacity":
                object.__setattr__(self, "old_value", clip.opacity)
            elif self.param_type == "is_enabled":
                object.__setattr__(self, "old_value", clip.is_enabled)
            elif self.param_type == "raw":
                object.__setattr__(self, "old_value", clip.raw)
            elif self.param_type in ("audio_sample_offset",
                                     "source_sample_offset"):
                # M14.3 采样级 UI：clip 级子帧偏移（@48k 参考率样本）
                object.__setattr__(self, "old_value",
                                   int(getattr(clip, self.param_type, 0) or 0))
            elif self.param_type in ("effect", "remove_effect"):
                object.__setattr__(self, "old_value", tuple(clip.effects))

        # All branches update via replace(): only the named parameter changes
        # and every other clip field (fades/color/blend/gain/freeze/fps
        # metadata/animated params...) survives the update.
        if self.param_type == "transform":
            if not isinstance(self.new_value, TransformParams):
                raise ValueError("new_value must be TransformParams for transform update")
            updated_clip = replace(clip, transform=self.new_value)
        elif self.param_type == "crop":
            if not isinstance(self.new_value, CropParams):
                raise ValueError("new_value must be CropParams for crop update")
            updated_clip = replace(clip, crop=self.new_value)
        elif self.param_type == "speed":
            if not isinstance(self.new_value, SpeedParams):
                raise ValueError("new_value must be SpeedParams for speed update")
            # RI-04/R15：时长统一走 timeline_duration_for_source（fps 元
            # 数据 + 速度点积分）；fade 随新时长钳制。UI 正常路径已改道
            # ChangeClipSpeedCommand，此处为 API/脚本直用时的同语义兜底。
            from src.features.edit.core.timeline_ops import (
                timeline_duration_for_source,
            )

            seq_fps = getattr(timeline, "fps", None)
            seq_fps_num = seq_fps.num if hasattr(seq_fps, "num") else None
            seq_fps_den = seq_fps.den if hasattr(seq_fps, "den") else 1
            new_timeline_duration = timeline_duration_for_source(
                clip.source_duration,
                self.new_value,
                clip._source_fps_num,
                clip._source_fps_den,
                clip._timeline_fps_num or seq_fps_num,
                clip._timeline_fps_den or seq_fps_den,
            )
            updated_clip = replace(
                clip,
                speed=self.new_value,
                timeline_duration=new_timeline_duration,
                fade_in_duration=min(
                    clip.fade_in_duration, new_timeline_duration
                ),
                fade_out_duration=min(
                    clip.fade_out_duration, new_timeline_duration
                ),
            )
        elif self.param_type == "raw":
            # TRAW（批 2）：非破坏 RAW 参数（RawParams 或 None=重置）
            if self.new_value is not None and not isinstance(self.new_value, RawParams):
                raise ValueError("new_value must be RawParams for raw update")
            updated_clip = replace(clip, raw=self.new_value)
        elif self.param_type == "opacity":
            if not isinstance(self.new_value, (int, float)):
                raise ValueError("new_value must be float for opacity update")
            updated_clip = replace(clip, opacity=float(self.new_value))
        elif self.param_type == "is_enabled":
            if not isinstance(self.new_value, bool):
                raise ValueError("new_value must be bool for is_enabled update")
            updated_clip = replace(clip, is_enabled=self.new_value)
        elif self.param_type in ("audio_sample_offset",
                                 "source_sample_offset"):
            # M14.3 采样级 UI：子帧偏移整数样本（≥0；@48k 参考率，
            # 语义见 core/sample_time.py——不做帧进位归一化）
            if not isinstance(self.new_value, int) or self.new_value < 0:
                raise ValueError(
                    f"new_value must be a non-negative int for "
                    f"{self.param_type} update")
            updated_clip = replace(
                clip, **{self.param_type: self.new_value})
        elif self.param_type == "effect":
            # AI plan set_effect（T-P2-20/W06 回放修复）：dict = 按类型
            # upsert 单个效果；tuple/list = 整体替换（undo 路径用）。
            if isinstance(self.new_value, dict):
                from src.features.edit.core.effect_instance import (
                    EffectInstance,
                )
                etype = str(self.new_value.get("effect_type", ""))
                if not etype:
                    raise ValueError("effect update requires effect_type")
                params = dict(self.new_value.get("parameters") or {})
                existing = next(
                    (e for e in clip.effects if e.effect_type == etype), None)
                if existing is not None:
                    new_effect = EffectInstance(
                        effect_id=existing.effect_id, effect_type=etype,
                        params=params, is_enabled=existing.is_enabled)
                    updated_clip = clip.with_effect_updated(
                        existing.effect_id, new_effect)
                else:
                    new_effect = EffectInstance(effect_type=etype,
                                                params=params)
                    updated_clip = clip.with_effect_added(new_effect)
            elif isinstance(self.new_value, (tuple, list)):
                updated_clip = replace(clip, effects=tuple(self.new_value))
            else:
                raise ValueError(
                    "effect new_value must be descriptor dict or effects tuple")
        elif self.param_type == "remove_effect":
            # AI plan remove_effect：new_value = effect_type 字符串；
            # 目标不存在时为幂等 no-op。
            etype = str(self.new_value)
            target = next(
                (e for e in clip.effects if e.effect_type == etype), None)
            updated_clip = (clip.with_effect_removed(target.effect_id)
                            if target is not None else clip)
        else:
            raise ValueError(f"Unknown param_type: {self.param_type}")

        # Check for overlap with other clips (only for speed which changes duration)
        if self.param_type == "speed":
            other_clips = [c for c in track.clips if c.clip_id != self.clip_id]
            for existing in other_clips:
                if (updated_clip.timeline_start < existing.timeline_end and
                    updated_clip.timeline_end > existing.timeline_start):
                    raise ValueError(
                        f"Updated clip would overlap with {existing.clip_id}"
                    )

        new_track = track.with_clip_updated(updated_clip)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(
            f"UpdateClipParamCommand applied: {self.clip_id} "
            f"{self.param_type} updated"
        )
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: restore original parameter.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with original parameter restored
        """
        if self.old_value is None:
            # Get current value from timeline to restore
            track = timeline.get_track_by_id(self.track_id)
            if track is None:
                raise ValueError(f"Track not found: {self.track_id}")
            clip = track.get_clip_by_id(self.clip_id)
            if clip is None:
                raise ValueError(f"Clip not found: {self.clip_id}")
            # Get current value as old_value for redo
            if self.param_type == "transform":
                old_value = clip.transform
            elif self.param_type == "crop":
                old_value = clip.crop
            elif self.param_type == "speed":
                old_value = clip.speed
            elif self.param_type == "opacity":
                old_value = clip.opacity
            elif self.param_type == "is_enabled":
                old_value = clip.is_enabled
            elif self.param_type in ("audio_sample_offset",
                                     "source_sample_offset"):
                # M14.3 采样级 UI：clip 级子帧偏移（@48k 参考率样本）
                old_value = int(getattr(clip, self.param_type, 0) or 0)
            elif self.param_type in ("effect", "remove_effect"):
                # undo 捕获整个效果列表：undo 命令以 tuple new_value 走
                # "effect" 分支整体还原（"remove_effect" 的 undo 同样
                # 经 "effect" 整体替换路径）。
                old_value = tuple(clip.effects)
            else:
                raise ValueError(f"Unknown param_type: {self.param_type}")
        else:
            old_value = self.old_value

        # Create undo command
        undo_param_type = ("effect" if self.param_type == "remove_effect"
                           else self.param_type)
        undo_command = UpdateClipParamCommand(
            command_id=self.command_id,
            label=f"Undo {self.label}",
            track_id=self.track_id,
            clip_id=self.clip_id,
            param_type=undo_param_type,
            new_value=old_value,
            old_value=self.new_value,
        )

        return undo_command.apply(timeline)

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        old_value_dict = None
        if self.old_value is not None:
            if hasattr(self.old_value, 'to_dict'):
                old_value_dict = self.old_value.to_dict()
            else:
                old_value_dict = self.old_value

        new_value_dict = None
        if hasattr(self.new_value, 'to_dict'):
            new_value_dict = self.new_value.to_dict()
        else:
            new_value_dict = self.new_value

        return {
            "command_id": self.command_id,
            "command_type": "UpdateClipParamCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "param_type": self.param_type,
            "new_value": new_value_dict,
            "old_value": old_value_dict,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> UpdateClipParamCommand:
        """Deserialize command."""
        param_type = data.get("param_type", "")
        new_value_data = data.get("new_value")
        old_value_data = data.get("old_value")

        # Deserialize values based on param_type
        if param_type == "transform":
            new_value = TransformParams.from_dict(new_value_data) if new_value_data else None
            old_value = TransformParams.from_dict(old_value_data) if old_value_data else None
        elif param_type == "crop":
            new_value = CropParams.from_dict(new_value_data) if new_value_data else None
            old_value = CropParams.from_dict(old_value_data) if old_value_data else None
        elif param_type == "speed":
            new_value = SpeedParams.from_dict(new_value_data) if new_value_data else None
            old_value = SpeedParams.from_dict(old_value_data) if old_value_data else None
        elif param_type in ("opacity", "is_enabled"):
            new_value = new_value_data
            old_value = old_value_data
        else:
            new_value = new_value_data
            old_value = old_value_data

        return UpdateClipParamCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Update Clip Parameter"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            param_type=param_type,
            new_value=new_value,
            old_value=old_value,
        )

    @staticmethod
    def create_transform(
        track_id: str,
        clip_id: str,
        new_transform: TransformParams,
        old_transform: TransformParams | None = None
    ) -> UpdateClipParamCommand:
        """Factory method for transform update."""
        return UpdateClipParamCommand(
            command_id=str(uuid.uuid4()),
            label=f"Update Transform for {clip_id}",
            track_id=track_id,
            clip_id=clip_id,
            param_type="transform",
            new_value=new_transform,
            old_value=old_transform,
        )

    @staticmethod
    def create_crop(
        track_id: str,
        clip_id: str,
        new_crop: CropParams,
        old_crop: CropParams | None = None
    ) -> UpdateClipParamCommand:
        """Factory method for crop update."""
        return UpdateClipParamCommand(
            command_id=str(uuid.uuid4()),
            label=f"Update Crop for {clip_id}",
            track_id=track_id,
            clip_id=clip_id,
            param_type="crop",
            new_value=new_crop,
            old_value=old_crop,
        )

    @staticmethod
    def create_speed(
        track_id: str,
        clip_id: str,
        new_speed: SpeedParams,
        old_speed: SpeedParams | None = None
    ) -> UpdateClipParamCommand:
        """Factory method for speed update."""
        return UpdateClipParamCommand(
            command_id=str(uuid.uuid4()),
            label=f"Update Speed for {clip_id}",
            track_id=track_id,
            clip_id=clip_id,
            param_type="speed",
            new_value=new_speed,
            old_value=old_speed,
        )

    @staticmethod
    def create_opacity(
        track_id: str,
        clip_id: str,
        new_opacity: float,
        old_opacity: float | None = None
    ) -> UpdateClipParamCommand:
        """Factory method for opacity update."""
        return UpdateClipParamCommand(
            command_id=str(uuid.uuid4()),
            label=f"Update Opacity for {clip_id}",
            track_id=track_id,
            clip_id=clip_id,
            param_type="opacity",
            new_value=new_opacity,
            old_value=old_opacity,
        )


# ================================================================
# Keyframe Commands (P5.3)
# ================================================================

@dataclass(frozen=True)
class AddKeyframeCommand:
    """
    Command to add a keyframe to a clip parameter (P5.3).

    Supported parameters:
    - opacity
    - transform.translate_x/y
    - transform.scale_x/y
    - effect float parameters

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track containing the clip
        clip_id: Clip ID
        param_name: Parameter path (e.g., 'opacity', 'transform.translate_x')
        frame: Frame number for keyframe
        value: Value at keyframe
        interpolation: Interpolation type (linear, ease_in, ease_out, bezier)
        effect_id: Optional effect ID (for effect parameters)
        old_clip: Old clip state (for undo)
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    param_name: str
    frame: int
    value: float
    interpolation: str = "linear"
    effect_id: str | None = None  # For effect parameters
    _old_clip: ClipInstance | None = None  # Set during apply

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: add keyframe.

        When ``self.effect_id`` is set, the keyframe is added to that
        effect's ``animated_params`` (via ``EffectInstance.with_keyframe_added``
        + ``ClipInstance.with_effect_updated``) instead of the clip's own
        ``animated_params``. This is what makes per-effect parameter
        animation work — the clip-level path only covers opacity /
        transform.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with keyframe added
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # C06（第六轮审计）：关键帧/效果写入口统一轨锁守卫——锁轨下
        # 这些命令此前可绕锁直接写入（revision 递增、内容被改）。
        assert_track_editable(timeline, self.track_id, "Add keyframe")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # Import InterpolationType from shared module (Phase 2: Modularization)
        from src.shared.keyframe import InterpolationType
        interp = InterpolationType(self.interpolation)

        if self.effect_id is not None:
            new_clip = self._apply_to_effect(clip, interp)
        else:
            new_clip = self._apply_to_clip(clip, interp)

        new_track = track.with_clip_updated(new_clip)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(
            f"AddKeyframeCommand applied: {self.clip_id} "
            f"param={self.param_name}, frame={self.frame}, value={self.value}"
            f"{f', effect={self.effect_id}' if self.effect_id else ''}"
        )

        return new_timeline

    def _apply_to_clip(self, clip: ClipInstance, interp) -> ClipInstance:
        """Clip-level keyframe (opacity / transform.*)."""
        # C01（第六轮审计）：值域走 clip_params 的权威 schema。此前命令层
        # 写成 transform.position*（实际名称是 translate_*），translate/
        # rotation 落默认 [0,100]——X=-250 求值为 0、rotation=-45 变 0。
        from src.features.edit.core.clip_params import clip_param_range

        min_val, max_val = clip_param_range(self.param_name)

        return clip.with_keyframe_added(
            self.param_name,
            self.frame,
            self.value,
            interp,
            min_val,
            max_val
        )

    def _apply_to_effect(self, clip: ClipInstance, interp) -> ClipInstance:
        """Effect-level keyframe (per-effect parameter animation).

        Looks up the ``ParamDef`` from the effect catalog to get the
        correct min/max range, then delegates to
        ``EffectInstance.with_keyframe_added`` and writes the new effect
        back via ``ClipInstance.with_effect_updated``.
        """
        effect = clip.get_effect_by_id(self.effect_id)
        if effect is None:
            raise ValueError(f"Effect not found: {self.effect_id}")

        min_val, max_val = self._effect_param_range(effect.effect_type)

        new_effect = effect.with_keyframe_added(
            self.param_name,
            self.frame,
            self.value,
            interp,
            min_val,
            max_val,
        )
        return clip.with_effect_updated(self.effect_id, new_effect)

    def _effect_param_range(self, effect_type: str) -> tuple:
        """Look up a parameter's (min, max) from the effect catalog.

        Falls back to (0.0, 100.0) when the spec or param is unknown so
        the keyframe is still recorded (better than crashing); the
        rendered value may be clamped oddly but the data is preserved.
        """
        try:
            from src.features.shared.effects import get_effect_catalog
            spec = get_effect_catalog().get_spec(effect_type)
            if spec is not None:
                for pdef in spec.param_defs:
                    if pdef.name == self.param_name:
                        return (float(pdef.min_val), float(pdef.max_val))
        except Exception:  # noqa: BLE001 — never crash the command
            pass
        return (0.0, 100.0)

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: remove keyframe.

        Args:
            timeline: Current timeline

        Returns:
            Timeline without the keyframe
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        if self.effect_id is not None:
            effect = clip.get_effect_by_id(self.effect_id)
            if effect is not None:
                new_effect = effect.with_keyframe_removed(
                    self.param_name, self.frame)
                new_clip = clip.with_effect_updated(self.effect_id, new_effect)
            else:
                new_clip = clip
        else:
            new_clip = clip.with_keyframe_removed(self.param_name, self.frame)

        new_track = track.with_clip_updated(new_clip)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(
            f"AddKeyframeCommand undone: {self.clip_id} "
            f"param={self.param_name}, frame={self.frame}"
        )

        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "AddKeyframeCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "param_name": self.param_name,
            "frame": self.frame,
            "value": self.value,
            "interpolation": self.interpolation,
            "effect_id": self.effect_id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> AddKeyframeCommand:
        """Deserialize command."""
        return AddKeyframeCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Add Keyframe"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            param_name=data.get("param_name", ""),
            frame=data.get("frame", 0),
            value=data.get("value", 0.0),
            interpolation=data.get("interpolation", "linear"),
            effect_id=data.get("effect_id"),
        )

    @staticmethod
    def create(
        track_id: str,
        clip_id: str,
        param_name: str,
        frame: int,
        value: float,
        interpolation: str = "linear",
        effect_id: str | None = None
    ) -> AddKeyframeCommand:
        """Factory method to create command."""
        return AddKeyframeCommand(
            command_id=str(uuid.uuid4()),
            label=f"Add Keyframe at F{frame}",
            track_id=track_id,
            clip_id=clip_id,
            param_name=param_name,
            frame=frame,
            value=value,
            interpolation=interpolation,
            effect_id=effect_id,
        )


@dataclass(frozen=True)
class MoveKeyframeCommand:
    """B12：完整关键帧时间移动——整 key 复制只改 frame。

    既有 Remove+Add 桥只携带 value/interpolation，丢 tangent_mode、
    handle_left/right、legacy bezier_in/out 与未知扩展字段，移动时间
    等于改写曲线形状且 undo 无法完整还原。本命令按**整 Keyframe** 移动：
    复制源 key 仅替换 frame；目的帧已有关键帧时显式拒绝（冲突规则
    可 review，不静默覆盖）；undo 以 apply 期完整快照精确还原。

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track containing the clip
        clip_id: Clip ID
        param_name: Parameter path
        from_frame: 源帧号
        to_frame: 目的帧号（不得与现有其他关键帧重合）
        effect_id: Optional effect ID (for effect parameters)
        _moved_key: apply 期捕获的完整源 key 快照（to_dict，undo 用）
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    param_name: str
    from_frame: int
    to_frame: int
    effect_id: str | None = None
    _moved_key: dict[str, Any] | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        assert_track_editable(timeline, self.track_id, "Move keyframe")
        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")
        if self.from_frame == self.to_frame:
            return timeline

        anim_param = self._resolve_anim_param(clip)
        if anim_param is None or not anim_param.keyframes:
            raise ValueError(
                f"No keyframes for param {self.param_name!r} at "
                f"frame {self.from_frame}")
        source = anim_param.keyframes.get_keyframe_at(self.from_frame)
        if source is None:
            raise ValueError(
                f"No keyframe at frame {self.from_frame}")
        if anim_param.keyframes.get_keyframe_at(self.to_frame) is not None:
            raise ValueError(
                f"Keyframe already exists at frame {self.to_frame}")

        object.__setattr__(self, "_moved_key", source.to_dict())
        moved = replace(source, frame=self.to_frame)
        new_clip = self._write_moved(clip, anim_param, moved)
        new_track = track.with_clip_updated(new_clip)
        return timeline.with_track_updated(new_track)

    def _resolve_anim_param(self, clip: ClipInstance):
        if self.effect_id is not None:
            effect = clip.get_effect_by_id(self.effect_id)
            if effect is None:
                raise ValueError(f"Effect not found: {self.effect_id}")
            return effect.animated_params.get(self.param_name)
        return clip.animated_params.get(self.param_name)

    def _write_moved(
        self, clip: ClipInstance, anim_param, moved_key,
    ) -> ClipInstance:
        from dataclasses import replace as _dc_replace

        old_track = anim_param.keyframes
        new_keys = [k for k in old_track.keyframes
                    if k.frame != self.from_frame]
        new_keys.append(moved_key)
        new_keys.sort()
        new_anim = _dc_replace(
            anim_param,
            keyframes=_dc_replace(old_track, keyframes=new_keys),
        )
        if self.effect_id is not None:
            effect = clip.get_effect_by_id(self.effect_id)
            new_effect = _dc_replace(
                effect,
                animated_params={
                    **effect.animated_params, self.param_name: new_anim},
            )
            return clip.with_effect_updated(self.effect_id, new_effect)
        return _dc_replace(
            clip, animated_params={
                **clip.animated_params, self.param_name: new_anim})

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._moved_key is None:
            raise ValueError("Cannot undo: command was never applied")
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")
        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        from src.shared.keyframe import Keyframe

        anim_param = self._resolve_anim_param(clip)
        if anim_param is None or not anim_param.keyframes:
            raise ValueError(
                f"No keyframes for param {self.param_name!r} on undo")
        restored = Keyframe.from_dict(self._moved_key)
        if anim_param.keyframes.get_keyframe_at(restored.frame) is not None:
            raise ValueError(
                f"Keyframe already exists at frame {restored.frame}")
        # 以快照整 key 还原（含 tangent/handles/extras），先移除目的帧
        undo_cmd = MoveKeyframeCommand(
            command_id=self.command_id, label=self.label,
            track_id=self.track_id, clip_id=self.clip_id,
            param_name=self.param_name,
            from_frame=self.to_frame, to_frame=restored.frame,
            effect_id=self.effect_id, _moved_key=self._moved_key,
        )
        new_clip = undo_cmd._write_moved(clip, anim_param, restored)
        new_track = track.with_clip_updated(new_clip)
        return timeline.with_track_updated(new_track)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "MoveKeyframeCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "param_name": self.param_name,
            "from_frame": self.from_frame,
            "to_frame": self.to_frame,
            "effect_id": self.effect_id,
            "_moved_key": self._moved_key,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "MoveKeyframeCommand":
        return MoveKeyframeCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Move Keyframe"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            param_name=data.get("param_name", ""),
            from_frame=data.get("from_frame", 0),
            to_frame=data.get("to_frame", 0),
            effect_id=data.get("effect_id"),
            _moved_key=data.get("_moved_key"),
        )

    @staticmethod
    def create(
        track_id: str,
        clip_id: str,
        param_name: str,
        from_frame: int,
        to_frame: int,
        effect_id: str | None = None,
    ) -> "MoveKeyframeCommand":
        return MoveKeyframeCommand(
            command_id=str(uuid.uuid4()),
            label=f"Move keyframe F{from_frame}→F{to_frame}",
            track_id=track_id,
            clip_id=clip_id,
            param_name=param_name,
            from_frame=int(from_frame),
            to_frame=int(to_frame),
            effect_id=effect_id,
        )


@dataclass(frozen=True)
class RemoveKeyframeCommand:
    """
    Command to remove a keyframe from a clip parameter (P5.3).

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track containing the clip
        clip_id: Clip ID
        param_name: Parameter path
        frame: Frame number of keyframe to remove
        effect_id: Optional effect ID (for effect parameters)
        _old_value: Old keyframe value (for undo)
        _old_interpolation: Old interpolation type (for undo)
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    param_name: str
    frame: int
    effect_id: str | None = None  # For effect parameters
    _old_value: float | None = None  # Set during apply
    _old_interpolation: str | None = None  # Set during apply

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: remove keyframe.

        When ``self.effect_id`` is set, the keyframe is removed from the
        effect's ``animated_params`` (mirroring AddKeyframeCommand).

        Args:
            timeline: Current timeline

        Returns:
            Timeline with keyframe removed
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # C06（第六轮审计）：关键帧/效果写入口统一轨锁守卫——锁轨下
        # 这些命令此前可绕锁直接写入（revision 递增、内容被改）。
        assert_track_editable(timeline, self.track_id, "Remove keyframe")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # Store old keyframe data for undo (read before removing).
        old_value, old_interpolation = self._read_old_keyframe(clip)
        object.__setattr__(self, "_old_value", old_value)
        object.__setattr__(self, "_old_interpolation", old_interpolation)

        if self.effect_id is not None:
            effect = clip.get_effect_by_id(self.effect_id)
            if effect is not None:
                new_effect = effect.with_keyframe_removed(
                    self.param_name, self.frame)
                new_clip = clip.with_effect_updated(
                    self.effect_id, new_effect)
            else:
                new_clip = clip
        else:
            new_clip = clip.with_keyframe_removed(self.param_name, self.frame)

        new_track = track.with_clip_updated(new_clip)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(
            f"RemoveKeyframeCommand applied: {self.clip_id} "
            f"param={self.param_name}, frame={self.frame}"
            f"{f', effect={self.effect_id}' if self.effect_id else ''}"
        )

        return new_timeline

    def _read_old_keyframe(self, clip: ClipInstance) -> tuple:
        """Read the keyframe being removed (for undo).

        Looks in the effect's animated_params when ``effect_id`` is set,
        otherwise in the clip's animated_params.
        """
        if self.effect_id is not None:
            effect = clip.get_effect_by_id(self.effect_id)
            if effect is None or self.param_name not in effect.animated_params:
                return (None, None)
            anim_param = effect.animated_params[self.param_name]
        else:
            if self.param_name not in clip.animated_params:
                return (None, None)
            anim_param = clip.animated_params[self.param_name]

        kf = (anim_param.keyframes.get_keyframe_at(self.frame)
              if anim_param.keyframes else None)
        if kf:
            return (kf.value, kf.interpolation.value)
        return (None, None)

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: restore keyframe.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with keyframe restored
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        if self._old_value is None:
            raise ValueError("Cannot undo: old keyframe value not stored")

        from src.shared.keyframe import InterpolationType
        interp = InterpolationType(self._old_interpolation or "linear")

        if self.effect_id is not None:
            effect = clip.get_effect_by_id(self.effect_id)
            if effect is None:
                raise ValueError(
                    f"Effect not found for undo: {self.effect_id}")
            min_val, max_val = self._effect_param_range(effect.effect_type)
            new_effect = effect.with_keyframe_added(
                self.param_name, self.frame, self._old_value,
                interp, min_val, max_val,
            )
            new_clip = clip.with_effect_updated(self.effect_id, new_effect)
        else:
            # C01：与 AddKeyframeCommand._apply_to_clip 同一权威值域
            # （此前 undo 恢复关键帧时复制了同样的错误 [0,100] 范围）。
            from src.features.edit.core.clip_params import clip_param_range

            min_val, max_val = clip_param_range(self.param_name)
            new_clip = clip.with_keyframe_added(
                self.param_name, self.frame, self._old_value,
                interp, min_val, max_val,
            )

        new_track = track.with_clip_updated(new_clip)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(
            f"RemoveKeyframeCommand undone: {self.clip_id} "
            f"param={self.param_name}, frame={self.frame}"
        )

        return new_timeline

    def _effect_param_range(self, effect_type: str) -> tuple:
        """Look up a parameter's (min, max) from the effect catalog."""
        try:
            from src.features.shared.effects import get_effect_catalog
            spec = get_effect_catalog().get_spec(effect_type)
            if spec is not None:
                for pdef in spec.param_defs:
                    if pdef.name == self.param_name:
                        return (float(pdef.min_val), float(pdef.max_val))
        except Exception:  # noqa: BLE001
            pass
        return (0.0, 100.0)

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "RemoveKeyframeCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "param_name": self.param_name,
            "frame": self.frame,
            "effect_id": self.effect_id,
            "old_value": self._old_value,
            "old_interpolation": self._old_interpolation,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> RemoveKeyframeCommand:
        """Deserialize command."""
        return RemoveKeyframeCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Remove Keyframe"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            param_name=data.get("param_name", ""),
            frame=data.get("frame", 0),
            effect_id=data.get("effect_id"),
            _old_value=data.get("old_value"),
            _old_interpolation=data.get("old_interpolation"),
        )

    @staticmethod
    def create(
        track_id: str,
        clip_id: str,
        param_name: str,
        frame: int,
        effect_id: str | None = None,
    ) -> RemoveKeyframeCommand:
        """Factory method to create command."""
        return RemoveKeyframeCommand(
            command_id=str(uuid.uuid4()),
            label=f"Remove Keyframe at F{frame}",
            track_id=track_id,
            clip_id=clip_id,
            param_name=param_name,
            frame=frame,
            effect_id=effect_id,
        )


@dataclass(frozen=True)
class UpdateKeyframeCommand:
    """
    Command to update a keyframe value (P5.3).

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track containing the clip
        clip_id: Clip ID
        param_name: Parameter path
        frame: Frame number of keyframe
        new_value: New value
        old_value: Old value (for undo)
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    param_name: str
    frame: int
    new_value: float
    old_value: float | None = None  # Set during apply

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: update keyframe value.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with keyframe updated
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # C06（第六轮审计）：关键帧/效果写入口统一轨锁守卫——锁轨下
        # 这些命令此前可绕锁直接写入（revision 递增、内容被改）。
        assert_track_editable(timeline, self.track_id, "Update keyframe")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        new_clip = clip.with_keyframe_updated(self.param_name, self.frame, self.new_value)

        new_track = track.with_clip_updated(new_clip)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(
            f"UpdateKeyframeCommand applied: {self.clip_id} "
            f"param={self.param_name}, frame={self.frame}, value={self.new_value}"
        )

        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: restore old keyframe value.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with old keyframe value
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        if self.old_value is None:
            raise ValueError("Cannot undo: old value not stored")

        new_clip = clip.with_keyframe_updated(self.param_name, self.frame, self.old_value)

        new_track = track.with_clip_updated(new_clip)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(
            f"UpdateKeyframeCommand undone: {self.clip_id} "
            f"param={self.param_name}, frame={self.frame}"
        )

        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "UpdateKeyframeCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "param_name": self.param_name,
            "frame": self.frame,
            "new_value": self.new_value,
            "old_value": self.old_value,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> UpdateKeyframeCommand:
        """Deserialize command."""
        return UpdateKeyframeCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Update Keyframe"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            param_name=data.get("param_name", ""),
            frame=data.get("frame", 0),
            new_value=data.get("new_value", 0.0),
            old_value=data.get("old_value"),
        )

    @staticmethod
    def create(
        track_id: str,
        clip_id: str,
        param_name: str,
        frame: int,
        new_value: float,
        old_value: float | None = None
    ) -> UpdateKeyframeCommand:
        """Factory method to create command."""
        return UpdateKeyframeCommand(
            command_id=str(uuid.uuid4()),
            label=f"Update Keyframe F{frame} to {new_value}",
            track_id=track_id,
            clip_id=clip_id,
            param_name=param_name,
            frame=frame,
            new_value=new_value,
            old_value=old_value,
        )


# ================================================================
# SetKeyframeInterpolationCommand (easing curve switch)
# ================================================================


@dataclass(frozen=True)
class SetKeyframeInterpolationCommand:
    """Change the easing curve (interpolation) of an existing keyframe.

    Lets a user switch a keyframe between linear / ease_in / ease_out /
    ease_in_out / bezier / hold without altering its value or frame. The
    value is read from the current keyframe and preserved.
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    param_name: str
    frame: int
    new_interpolation: str  # InterpolationType value name (e.g. "ease_in_out")
    _old_interpolation: str | None = None

    def _resolve_type(self, name: str) -> InterpolationType:
        """Map a string name to an InterpolationType member."""
        from src.shared.keyframe import InterpolationType as IT
        for member in IT:
            if member.value == name or member.name.lower() == name.lower():
                return member
        # Default fallback
        return IT.LINEAR

    def apply(self, timeline: TimelineSequence):
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # C06（第六轮审计）：关键帧/效果写入口统一轨锁守卫——锁轨下
        # 这些命令此前可绕锁直接写入（revision 递增、内容被改）。
        assert_track_editable(timeline, self.track_id, "Set keyframe interpolation")
        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        anim_param = clip.animated_params.get(self.param_name)
        if anim_param is None or anim_param.keyframes is None:
            raise ValueError(f"No animated param/keyframes: {self.param_name}")
        kf = anim_param.keyframes.get_keyframe_at(self.frame)
        if kf is None:
            raise ValueError(f"No keyframe at frame {self.frame}")

        old_interp_name = kf.interpolation.value if hasattr(kf.interpolation, "value") else str(kf.interpolation)
        new_interp = self._resolve_type(self.new_interpolation)
        new_clip = clip.with_keyframe_updated(
            self.param_name, self.frame, kf.value, interpolation=new_interp
        )
        new_timeline = timeline.with_track_updated(track.with_clip_updated(new_clip))

        updated_cmd = SetKeyframeInterpolationCommand(
            command_id=self.command_id,
            label=self.label,
            track_id=self.track_id,
            clip_id=self.clip_id,
            param_name=self.param_name,
            frame=self.frame,
            new_interpolation=self.new_interpolation,
            _old_interpolation=old_interp_name,
        )
        logger.debug(
            f"SetKeyframeInterpolationCommand applied: {self.clip_id} "
            f"frame {self.frame} → {self.new_interpolation}"
        )
        return new_timeline, updated_cmd

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        if self._old_interpolation is None:
            return timeline
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            return timeline
        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            return timeline
        anim_param = clip.animated_params.get(self.param_name)
        if anim_param is None or anim_param.keyframes is None:
            return timeline
        kf = anim_param.keyframes.get_keyframe_at(self.frame)
        if kf is None:
            return timeline
        old_interp = self._resolve_type(self._old_interpolation)
        new_clip = clip.with_keyframe_updated(
            self.param_name, self.frame, kf.value, interpolation=old_interp
        )
        return timeline.with_track_updated(track.with_clip_updated(new_clip))

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": "SetKeyframeInterpolationCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "param_name": self.param_name,
            "frame": self.frame,
            "new_interpolation": self.new_interpolation,
            "old_interpolation": self._old_interpolation,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SetKeyframeInterpolationCommand:
        return SetKeyframeInterpolationCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Set Keyframe Interpolation"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            param_name=data.get("param_name", ""),
            frame=data.get("frame", 0),
            new_interpolation=data.get("new_interpolation", "linear"),
            _old_interpolation=data.get("old_interpolation"),
        )

    @staticmethod
    def create(
        track_id: str, clip_id: str, param_name: str, frame: int, interpolation: str
    ) -> SetKeyframeInterpolationCommand:
        return SetKeyframeInterpolationCommand(
            command_id=str(uuid.uuid4()),
            label=f"Set Easing F{frame}: {interpolation}",
            track_id=track_id,
            clip_id=clip_id,
            param_name=param_name,
            frame=frame,
            new_interpolation=interpolation,
        )


# ================================================================
# Effect Commands (P5.2)
# ================================================================

from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.effect_registry import (
    GENERATOR_EFFECT_TYPES,
    get_effect_registry,
)


@dataclass(frozen=True)
class AddEffectCommand:
    """
    Command to add an effect to a clip.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track containing the clip
        clip_id: Clip ID to add effect to
        effect_type: Effect type identifier (e.g., 'gaussian_blur')
        _effect_id: ID of created effect (set during apply)
        _added_effect: Added effect instance (for undo)
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    effect_type: str
    _effect_id: str | None = None
    _added_effect: EffectInstance | None = None
    _plugin_identity: dict[str, Any] | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: add effect to clip.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with effect added to clip
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # C06（第六轮审计）：关键帧/效果写入口统一轨锁守卫——锁轨下
        # 这些命令此前可绕锁直接写入（revision 递增、内容被改）。
        assert_track_editable(timeline, self.track_id, "Add effect")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # Get effect definition and create instance
        registry = get_effect_registry()
        effect_def = registry.get_definition(self.effect_type)
        if effect_def is None:
            raise ValueError(f"Effect type not found: {self.effect_type}")

        # Create effect instance with order based on existing effects
        existing_effects = clip.effects if isinstance(clip.effects, tuple) else tuple(clip.effects) if clip.effects else ()
        order = len(existing_effects)
        effect_instance = effect_def.create_instance(order)

        # If the caller pre-generated an effect_id (e.g. the timeline
        # drag-drop path, which needs the id before apply so it can scroll
        # the properties panel to the new effect), override the instance's
        # auto-generated id with it. Keeps undo/redo consistent: the same
        # id is reused across the command's lifetime.
        if self._effect_id:
            effect_instance = effect_instance.with_effect_id(self._effect_id)
        if self._plugin_identity is not None:
            effect_instance = effect_instance.with_plugin_identity(
                dict(self._plugin_identity)
            )

        # Add effect to clip
        updated_clip = clip.with_effect_added(effect_instance)

        # Update track and timeline
        new_track = track.with_clip_updated(updated_clip)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(
            f"AddEffectCommand applied: {self.effect_type} to clip {self.clip_id}"
        )

        # AUD-16211: 持久化生成的 effect 实例（frozen dataclass，用本模块
        # 通用的 object.__setattr__ 回写，保持 bare-timeline 返回契约）。
        # 旧实现不持久化，undo 走"最后一个同类型效果"回退查找——同片段有
        # 多个同类型效果、或 add 与 undo 之间 reorder/删除过效果时会移除
        # 错误目标。回写后 undo/redo 均按精确 effect_id 移除。
        object.__setattr__(self, "_effect_id", effect_instance.effect_id)
        object.__setattr__(self, "_added_effect", effect_instance)
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: remove the effect.

        Args:
            timeline: Current timeline

        Returns:
            Timeline without the effect
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # Find the effect to remove
        # The effect_id should be stored in _effect_id or we can find by type
        if self._effect_id:
            effect_id = self._effect_id
        else:
            # Find the last effect of this type
            effects = clip.effects if isinstance(clip.effects, tuple) else tuple(clip.effects) if clip.effects else ()
            effect_id = None
            for effect in reversed(effects):
                if isinstance(effect, EffectInstance) and effect.effect_type == self.effect_type:
                    effect_id = effect.effect_id
                    break
                elif isinstance(effect, dict) and effect.get("effect_type") == self.effect_type:
                    effect_id = effect.get("effect_id")
                    break

            if effect_id is None:
                logger.warning("AddEffectCommand undo: cannot find effect to remove")
                return timeline

        # Remove effect
        updated_clip = clip.with_effect_removed(effect_id)
        new_track = track.with_clip_updated(updated_clip)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(f"AddEffectCommand undone: removed {self.effect_type}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "AddEffectCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "effect_type": self.effect_type,
            "effect_id": self._effect_id,
            "added_effect": self._added_effect.to_dict() if self._added_effect else None,
            "plugin_identity": self._plugin_identity,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> AddEffectCommand:
        """Deserialize command."""
        added_effect_data = data.get("added_effect")
        added_effect = EffectInstance.from_dict(added_effect_data) if added_effect_data else None

        return AddEffectCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Add Effect"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            effect_type=data.get("effect_type", ""),
            _effect_id=data.get("effect_id"),
            _added_effect=added_effect,
            _plugin_identity=data.get("plugin_identity"),
        )

    @staticmethod
    def create(
        track_id: str,
        clip_id: str,
        effect_type: str,
        effect_id: str | None = None,
        plugin_identity: dict[str, Any] | None = None,
    ) -> AddEffectCommand:
        """Factory method to create command.

        Args:
            effect_id: Optional pre-generated effect id. When provided,
                ``apply()`` uses this id instead of the auto-generated one,
                so callers (e.g. timeline drag-drop) can know the id before
                the command runs and scroll/select the resulting UI section.
                When omitted, a new uuid is generated at apply time.

        Raises:
            ValueError: if ``effect_type`` is a generator (FX-C06). Generators
                (adjustment_clip / solid_color) are clip TEMPLATES that must be
                dragged onto the timeline to create a new clip — attaching one
                as an EffectInstance produces a useless instance that the
                renderer silently drops. Reject early with a readable error.
        """
        if effect_type in GENERATOR_EFFECT_TYPES:
            raise ValueError(
                f"Cannot attach generator '{effect_type}' as an effect. "
                f"Generators create new clips — drag them onto the timeline "
                f"instead of double-clicking to attach."
            )
        if plugin_identity is None and effect_type.startswith("plugin:"):
            try:
                from src.features.shared.effects import get_effect_catalog
                from src.features.shared.effects.plugin_identity import (
                    build_plugin_identity,
                )
                spec = get_effect_catalog().get_spec(effect_type)
                if spec is not None:
                    plugin_identity = build_plugin_identity(spec).to_dict()
            except Exception:  # noqa: BLE001 — command creation remains robust
                logger.warning(
                    "Could not snapshot plugin identity for %s",
                    effect_type,
                    exc_info=True,
                )
        return AddEffectCommand(
            command_id=str(uuid.uuid4()),
            label=f"Add {effect_type} Effect",
            track_id=track_id,
            clip_id=clip_id,
            effect_type=effect_type,
            _effect_id=effect_id,
            _plugin_identity=plugin_identity,
        )


@dataclass(frozen=True)
class RemoveEffectCommand:
    """
    Command to remove an effect from a clip.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track containing the clip
        clip_id: Clip ID to remove effect from
        effect_id: Effect ID to remove
        _removed_effect: Removed effect instance (for undo)
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    effect_id: str
    _removed_effect: EffectInstance | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: remove effect from clip.

        Args:
            timeline: Current timeline

        Returns:
            Timeline without the effect
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # C06（第六轮审计）：关键帧/效果写入口统一轨锁守卫——锁轨下
        # 这些命令此前可绕锁直接写入（revision 递增、内容被改）。
        assert_track_editable(timeline, self.track_id, "Remove effect")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # Get the effect being removed (for undo)
        removed_effect = clip.get_effect_by_id(self.effect_id)
        if removed_effect is None:
            raise ValueError(f"Effect not found: {self.effect_id}")

        # Remove effect from clip
        updated_clip = clip.with_effect_removed(self.effect_id)

        # Update track and timeline
        new_track = track.with_clip_updated(updated_clip)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(
            f"RemoveEffectCommand applied: {self.effect_id} from clip {self.clip_id}"
        )

        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: restore the effect.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with effect restored
        """
        if self._removed_effect is None:
            raise ValueError("Cannot undo: removed effect not stored")

        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # Add effect back
        updated_clip = clip.with_effect_added(self._removed_effect)
        new_track = track.with_clip_updated(updated_clip)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(f"RemoveEffectCommand undone: restored {self.effect_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "RemoveEffectCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "effect_id": self.effect_id,
            "removed_effect": self._removed_effect.to_dict() if self._removed_effect else None,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> RemoveEffectCommand:
        """Deserialize command."""
        removed_effect_data = data.get("removed_effect")
        removed_effect = EffectInstance.from_dict(removed_effect_data) if removed_effect_data else None

        return RemoveEffectCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Remove Effect"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            effect_id=data.get("effect_id", ""),
            _removed_effect=removed_effect,
        )

    @staticmethod
    def create(
        track_id: str,
        clip_id: str,
        effect_id: str
    ) -> RemoveEffectCommand:
        """Factory method to create command."""
        return RemoveEffectCommand(
            command_id=str(uuid.uuid4()),
            label=f"Remove Effect {effect_id}",
            track_id=track_id,
            clip_id=clip_id,
            effect_id=effect_id,
        )


@dataclass(frozen=True)
class UpdateEffectParamCommand:
    """
    Command to update an effect parameter.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track containing the clip
        clip_id: Clip ID containing the effect
        effect_id: Effect ID to update
        param_name: Parameter name to update
        new_value: New parameter value
        old_value: Old parameter value (for undo)
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    effect_id: str
    param_name: str
    new_value: Any
    old_value: Any | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: update effect parameter.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with effect parameter updated
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # C06（第六轮审计）：关键帧/效果写入口统一轨锁守卫——锁轨下
        # 这些命令此前可绕锁直接写入（revision 递增、内容被改）。
        assert_track_editable(timeline, self.track_id, "Update effect parameter")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # Get the effect
        effect = clip.get_effect_by_id(self.effect_id)
        if effect is None:
            raise ValueError(f"Effect not found: {self.effect_id}")

        # Update the effect parameter
        if isinstance(effect, EffectInstance):
            updated_effect = effect.with_param(self.param_name, self.new_value)
        elif isinstance(effect, dict):
            updated_params = dict(effect.get("params", {}))
            updated_params[self.param_name] = self.new_value
            updated_effect = dict(effect)
            updated_effect["params"] = updated_params
        else:
            raise ValueError(f"Unknown effect type: {type(effect)}")

        # Update clip with new effect
        updated_clip = clip.with_effect_updated(self.effect_id, updated_effect)

        # Update track and timeline
        new_track = track.with_clip_updated(updated_clip)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(
            f"UpdateEffectParamCommand applied: {self.effect_id}.{self.param_name} = {self.new_value}"
        )
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: restore original parameter value.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with original parameter value
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # Get the effect
        effect = clip.get_effect_by_id(self.effect_id)
        if effect is None:
            raise ValueError(f"Effect not found: {self.effect_id}")

        # Restore old value
        if self.old_value is None:
            # Try to get current value
            if isinstance(effect, EffectInstance):
                current_value = effect.params.get(self.param_name)
            elif isinstance(effect, dict):
                current_value = effect.get("params", {}).get(self.param_name)
            else:
                current_value = None

            if current_value is None:
                raise ValueError("Cannot undo: old_value not stored")
            old_value = current_value
        else:
            old_value = self.old_value

        # Update the effect parameter with old value
        if isinstance(effect, EffectInstance):
            updated_effect = effect.with_param(self.param_name, old_value)
        elif isinstance(effect, dict):
            updated_params = dict(effect.get("params", {}))
            updated_params[self.param_name] = old_value
            updated_effect = dict(effect)
            updated_effect["params"] = updated_params
        else:
            raise ValueError(f"Unknown effect type: {type(effect)}")

        # Update clip with restored effect
        updated_clip = clip.with_effect_updated(self.effect_id, updated_effect)
        new_track = track.with_clip_updated(updated_clip)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(f"UpdateEffectParamCommand undone: {self.effect_id}.{self.param_name}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "UpdateEffectParamCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "effect_id": self.effect_id,
            "param_name": self.param_name,
            "new_value": self.new_value,
            "old_value": self.old_value,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> UpdateEffectParamCommand:
        """Deserialize command."""
        return UpdateEffectParamCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Update Effect Parameter"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            effect_id=data.get("effect_id", ""),
            param_name=data.get("param_name", ""),
            new_value=data.get("new_value"),
            old_value=data.get("old_value"),
        )

    @staticmethod
    def create(
        track_id: str,
        clip_id: str,
        effect_id: str,
        param_name: str,
        new_value: Any,
        old_value: Any | None = None
    ) -> UpdateEffectParamCommand:
        """Factory method to create command."""
        return UpdateEffectParamCommand(
            command_id=str(uuid.uuid4()),
            label=f"Update {param_name}",
            track_id=track_id,
            clip_id=clip_id,
            effect_id=effect_id,
            param_name=param_name,
            new_value=new_value,
            old_value=old_value,
        )


@dataclass(frozen=True)
class SetEffectEnabledCommand:
    """
    Command to enable/disable an effect.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track containing the clip
        clip_id: Clip ID containing the effect
        effect_id: Effect ID to toggle
        new_enabled: New enabled state
        old_enabled: Old enabled state (for undo)
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    effect_id: str
    new_enabled: bool
    old_enabled: bool | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: toggle effect enabled state.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with effect enabled state changed
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # C06（第六轮审计）：关键帧/效果写入口统一轨锁守卫——锁轨下
        # 这些命令此前可绕锁直接写入（revision 递增、内容被改）。
        assert_track_editable(timeline, self.track_id, "Toggle effect enabled")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # Get the effect
        effect = clip.get_effect_by_id(self.effect_id)
        if effect is None:
            raise ValueError(f"Effect not found: {self.effect_id}")

        # Update enabled state
        if isinstance(effect, EffectInstance):
            updated_effect = effect.with_enabled(self.new_enabled)
        elif isinstance(effect, dict):
            updated_effect = dict(effect)
            updated_effect["is_enabled"] = self.new_enabled
        else:
            raise ValueError(f"Unknown effect type: {type(effect)}")

        # Update clip
        updated_clip = clip.with_effect_updated(self.effect_id, updated_effect)
        new_track = track.with_clip_updated(updated_clip)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(
            f"SetEffectEnabledCommand applied: {self.effect_id} enabled={self.new_enabled}"
        )
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: restore original enabled state.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with original enabled state
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        # Get the effect
        effect = clip.get_effect_by_id(self.effect_id)
        if effect is None:
            raise ValueError(f"Effect not found: {self.effect_id}")

        # Restore old enabled state
        if self.old_enabled is None:
            # Get current state
            if isinstance(effect, EffectInstance):
                current_enabled = effect.is_enabled
            elif isinstance(effect, dict):
                current_enabled = effect.get("is_enabled", True)
            else:
                current_enabled = True

            old_enabled = not current_enabled  # Toggle back
        else:
            old_enabled = self.old_enabled

        # Update enabled state
        if isinstance(effect, EffectInstance):
            updated_effect = effect.with_enabled(old_enabled)
        elif isinstance(effect, dict):
            updated_effect = dict(effect)
            updated_effect["is_enabled"] = old_enabled
        else:
            raise ValueError(f"Unknown effect type: {type(effect)}")

        # Update clip
        updated_clip = clip.with_effect_updated(self.effect_id, updated_effect)
        new_track = track.with_clip_updated(updated_clip)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(f"SetEffectEnabledCommand undone: {self.effect_id}")
        return new_timeline

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "SetEffectEnabledCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "effect_id": self.effect_id,
            "new_enabled": self.new_enabled,
            "old_enabled": self.old_enabled,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> SetEffectEnabledCommand:
        """Deserialize command."""
        return SetEffectEnabledCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Toggle Effect"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            effect_id=data.get("effect_id", ""),
            new_enabled=data.get("new_enabled", True),
            old_enabled=data.get("old_enabled"),
        )

    @staticmethod
    def create(
        track_id: str,
        clip_id: str,
        effect_id: str,
        new_enabled: bool,
        old_enabled: bool | None = None
    ) -> SetEffectEnabledCommand:
        """Factory method to create command."""
        return SetEffectEnabledCommand(
            command_id=str(uuid.uuid4()),
            label=f"Toggle Effect {effect_id}",
            track_id=track_id,
            clip_id=clip_id,
            effect_id=effect_id,
            new_enabled=new_enabled,
            old_enabled=old_enabled,
        )


@dataclass(frozen=True)
class ReorderEffectCommand:
    """
    Command to reorder effects in a clip's effect stack.

    Attributes:
        command_id: Unique identifier
        label: Human-readable label
        track_id: Track containing the clip
        clip_id: Clip ID containing the effects
        effect_id: Effect ID to reorder
        new_order: New order position
        old_order: Old order position (for undo)
    """

    command_id: str
    label: str
    track_id: str
    clip_id: str
    effect_id: str
    new_order: int
    old_order: int | None = None

    def apply(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Apply the command: reorder effect in stack.

        FX-H04 fix: order is defined strictly by ARRAY POSITION
        (0..n-1), never by a stored ``order`` field that can drift out of
        sync. The reorder pops the effect from its current position and
        re-inserts it at ``new_order``, then renumbers every effect 0..n-1
        so the stack is always compact. ``old_order`` is recorded the first
        time apply runs (when it is still None) so undo always has a valid
        target.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with effect reordered
        """
        track = timeline.get_track_by_id(self.track_id)
        if track is None:
            raise ValueError(f"Track not found: {self.track_id}")

        # C06（第六轮审计）：关键帧/效果写入口统一轨锁守卫——锁轨下
        # 这些命令此前可绕锁直接写入（revision 递增、内容被改）。
        assert_track_editable(timeline, self.track_id, "Reorder effects")

        clip = track.get_clip_by_id(self.clip_id)
        if clip is None:
            raise ValueError(f"Clip not found: {self.clip_id}")

        effects = clip.effects if isinstance(clip.effects, tuple) else tuple(clip.effects) if clip.effects else ()

        # Locate the effect by id and capture its current array index as
        # old_order the first time apply runs. This is the single source of
        # truth for undo — callers no longer need to pass it.
        current_index = -1
        for i, effect in enumerate(effects):
            eid = effect.effect_id if isinstance(effect, EffectInstance) else effect.get("effect_id")
            if eid == self.effect_id:
                current_index = i
                break
        if current_index == -1:
            raise ValueError(f"Effect not found: {self.effect_id}")

        if self.old_order is None:
            # object.__setattr__ because the command is frozen. Done once;
            # subsequent apply (redo) keeps the recorded old_order.
            object.__setattr__(self, "old_order", current_index)

        # Pop-and-reinsert by array position, then renumber 0..n-1.
        new_order = max(0, min(self.new_order, len(effects) - 1))
        moving = effects[current_index]
        reordered = list(effects)
        del reordered[current_index]
        reordered.insert(new_order, moving)

        new_effects = tuple(
            (e.with_order(i) if isinstance(e, EffectInstance) else e)
            for i, e in enumerate(reordered)
        )

        updated_clip = clip.with_effects(new_effects)
        new_track = track.with_clip_updated(updated_clip)
        new_timeline = timeline.with_track_updated(new_track)

        logger.debug(
            f"ReorderEffectCommand applied: {self.effect_id} "
            f"{current_index} -> {new_order}"
        )
        return new_timeline

    def undo(self, timeline: TimelineSequence) -> TimelineSequence:
        """
        Undo the command: restore original order.

        FX-H04 fix: ``apply`` records ``old_order`` on first run, so undo
        simply builds the reverse command (new_order := old_order) and
        applies it. The previous implementation raised when old_order was
        None — now that can only happen if apply never ran, which is a
        genuine error worth surfacing.

        Args:
            timeline: Current timeline

        Returns:
            Timeline with original effect order
        """
        if self.old_order is None:
            raise ValueError("Cannot undo: old_order not stored (apply never ran)")

        # Reverse command. Pass old_order=self.new_order so the reverse
        # command's apply() doesn't overwrite the recorded value.
        undo_command = ReorderEffectCommand(
            command_id=self.command_id,
            label=f"Undo {self.label}",
            track_id=self.track_id,
            clip_id=self.clip_id,
            effect_id=self.effect_id,
            new_order=self.old_order,
            old_order=self.new_order,
        )

        return undo_command.apply(timeline)

    def to_dict(self) -> dict[str, Any]:
        """Serialize command."""
        return {
            "command_id": self.command_id,
            "command_type": "ReorderEffectCommand",
            "label": self.label,
            "track_id": self.track_id,
            "clip_id": self.clip_id,
            "effect_id": self.effect_id,
            "new_order": self.new_order,
            "old_order": self.old_order,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ReorderEffectCommand:
        """Deserialize command."""
        return ReorderEffectCommand(
            command_id=data.get("command_id", str(uuid.uuid4())),
            label=data.get("label", "Reorder Effect"),
            track_id=data.get("track_id", ""),
            clip_id=data.get("clip_id", ""),
            effect_id=data.get("effect_id", ""),
            new_order=data.get("new_order", 0),
            old_order=data.get("old_order"),
        )

    @staticmethod
    def create(
        track_id: str,
        clip_id: str,
        effect_id: str,
        new_order: int,
        old_order: int | None = None,
        timeline: TimelineSequence | None = None,
    ) -> ReorderEffectCommand:
        """Factory method to create a reorder command.

        FX-H04 fix: callers normally do NOT know ``old_order``. If it is
        omitted and a ``timeline`` is supplied, derive it from the
        effect's current array position so the command is always undoable.
        If neither is given the command is still created; ``apply`` will
        record old_order on first run (redo-safe).
        """
        resolved_old = old_order
        if resolved_old is None and timeline is not None:
            track = timeline.get_track_by_id(track_id)
            if track is not None:
                clip = track.get_clip_by_id(clip_id)
                if clip is not None:
                    effects = clip.effects if isinstance(clip.effects, tuple) else tuple(clip.effects or ())
                    for i, effect in enumerate(effects):
                        eid = effect.effect_id if isinstance(effect, EffectInstance) else effect.get("effect_id")
                        if eid == effect_id:
                            resolved_old = i
                            break
        return ReorderEffectCommand(
            command_id=str(uuid.uuid4()),
            label=f"Reorder Effect to {new_order}",
            track_id=track_id,
            clip_id=clip_id,
            effect_id=effect_id,
            new_order=new_order,
            old_order=resolved_old,
        )
