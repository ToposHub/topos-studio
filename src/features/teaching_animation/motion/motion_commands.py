"""W1 / TM-02: high-level motion commands — the AI-facing motion facade.

These commands let the AI (and the editor) drive animation through *named,
parameterized intent* — "pop these four cards with a 12-frame stagger" — and
compile that intent down to the keyframe tracks the existing renderer already
reads.  AI never writes low-level tracks by hand (plan §W1: "不允许 AI 直接写
低层 track"); it emits these commands, which are schema-validated, undoable,
and produce a readable SceneDiff.

The stagger structure is persisted in ``scene.metadata`` as a versioned
``MotionPlan`` so a later interval change *ripples* through every element
automatically (the S1 gate: "改前段时长后后续自动重排").

Implements the W1 subset of TM-02: ``apply_motion_preset``, ``stagger_elements``
and ``set_beat_timing`` (here ``SetStaggerIntervalCommand``).  Other TM-02
commands (apply_template, set_transition, replace_asset_slot, set_brand_theme)
land in later waves.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Mapping
import uuid

from ..core.model import SceneAnimation, TeachingSceneDocument
from .presets import MotionPresetRegistry, create_default_preset_registry, stagger_index
from .spring import SpringConfig, measure_spring
from src.shared.keyframe import Keyframe, KeyframeTrack, InterpolationType


# Wide track bounds so compiled curves (e.g. spring overshoot, slide offsets)
# are never clipped by the keyframe track; the evaluator applies the semantic
# clamp for the target property afterwards (opacity -> [0,1], scale -> >=0).
_TRACK_MIN = -1.0e9
_TRACK_MAX = 1.0e9

_MOTION_PLANS_KEY = "motion_plans"
_PLAN_SCHEMA_VERSION = 1


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def compile_preset_to_track(
    preset_fn,
    *,
    param_name: str,
    start_frame: int,
    end_frame: int,
    fps: float,
    sample_step: int = 1,
) -> KeyframeTrack:
    """Sample a ``MotionFn(frame, fps)`` into a deterministic ``KeyframeTrack``.

    Sampling is per-frame (or every ``sample_step``) over ``[start, end]``; this
    is the bridge from the motion language to the renderer's existing keyframe
    path, so preview and export read identical values.  Collinear samples are
    coalesced to keep the track compact without changing its shape.
    """
    if end_frame <= start_frame:
        raise ValueError("end_frame must exceed start_frame")
    if sample_step < 1:
        raise ValueError("sample_step must be positive")
    if fps <= 0.0:
        raise ValueError("fps must be positive")
    keyframes: list[Keyframe] = []
    for frame in range(start_frame, end_frame + 1, sample_step):
        value = float(preset_fn(frame, fps))
        keyframes.append(Keyframe(frame=frame, value=value, interpolation=InterpolationType.LINEAR))
    keyframes = _coalesce_collinear(keyframes)
    if len(keyframes) == 1:  # ensure at least two points for interpolation
        keyframes.append(Keyframe(frame=keyframes[0].frame + 1, value=keyframes[0].value,
                                  interpolation=InterpolationType.LINEAR))
    return KeyframeTrack(
        param_name=param_name, keyframes=keyframes,
        min_val=_TRACK_MIN, max_val=_TRACK_MAX,
        pre_extrap="constant", post_extrap="constant",
    )


def _coalesce_collinear(keyframes: list[Keyframe], tolerance: float = 1e-4) -> list[Keyframe]:
    """Drop interior keyframes that lie on the segment between their neighbours."""
    if len(keyframes) < 3:
        return keyframes
    kept: list[Keyframe] = [keyframes[0]]
    for index in range(1, len(keyframes) - 1):
        prev, curr, nxt = kept[-1], keyframes[index], keyframes[index + 1]
        span = nxt.frame - prev.frame
        if span == 0:
            continue
        expected_y = prev.value + (nxt.value - prev.value) * (curr.frame - prev.frame) / span
        if abs(expected_y - curr.value) > tolerance:
            kept.append(curr)
    kept.append(keyframes[-1])
    return kept


# ---------------------------------------------------------------------------
# Persisted plan (enables ripple editing)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StaggerPlan:
    """Serializable record of one stagger assignment, stored in scene metadata."""

    plan_id: str
    schema_version: int = _PLAN_SCHEMA_VERSION
    preset_id: str = "pop"
    param_name: str = "scale_x"
    interval: int = 12
    delay: int = 0
    duration: int = 18
    anchor_frame: int = 0
    element_ids: tuple[str, ...] = ()
    params: Mapping[str, Any] = field(default_factory=dict)
    animation_ids: tuple[str, ...] = ()
    # Serializable motion semantics of the compiled track (spring kind,
    # physical params, settle frame, overshoot policy) so editors can explain
    # the baked keyframes and re-derive them after a save/reopen round-trip.
    motion: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id, "schema_version": self.schema_version,
            "preset_id": self.preset_id, "param_name": self.param_name,
            "interval": self.interval, "delay": self.delay, "duration": self.duration,
            "anchor_frame": self.anchor_frame, "element_ids": list(self.element_ids),
            "params": copy.deepcopy(dict(self.params)),
            "animation_ids": list(self.animation_ids),
            "motion": copy.deepcopy(dict(self.motion)),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StaggerPlan":
        return cls(
            plan_id=str(data.get("plan_id", _new_id("plan"))),
            schema_version=int(data.get("schema_version", _PLAN_SCHEMA_VERSION)),
            preset_id=str(data.get("preset_id", "pop")),
            param_name=str(data.get("param_name", "scale_x")),
            interval=int(data.get("interval", 12)),
            delay=int(data.get("delay", 0)),
            duration=int(data.get("duration", 18)),
            anchor_frame=int(data.get("anchor_frame", 0)),
            element_ids=tuple(str(x) for x in data.get("element_ids", ())),
            params=copy.deepcopy(dict(data.get("params", {}))),
            animation_ids=tuple(str(x) for x in data.get("animation_ids", ())),
            motion=copy.deepcopy(dict(data.get("motion", {}))),
        )

    def starts(self) -> tuple[int, ...]:
        """Per-element start frame; later elements shift when interval changes."""
        return tuple(
            self.anchor_frame + stagger_index(index, interval=self.interval, delay=self.delay)
            for index in range(len(self.element_ids))
        )


def read_motion_plans(scene: TeachingSceneDocument) -> dict[str, StaggerPlan]:
    """Public read access to the persisted motion plans (editor display)."""
    return _read_plans(scene)


def _read_plans(scene: TeachingSceneDocument) -> dict[str, StaggerPlan]:
    raw = scene.metadata.get(_MOTION_PLANS_KEY, {}) if isinstance(scene.metadata, Mapping) else {}
    if not isinstance(raw, Mapping):
        return {}
    return {pid: StaggerPlan.from_dict(item) for pid, item in raw.items() if isinstance(item, Mapping)}


def _write_plan(scene: TeachingSceneDocument, plan: StaggerPlan) -> TeachingSceneDocument:
    plans = _read_plans(scene)
    plans[plan.plan_id] = plan
    new_metadata = {**dict(scene.metadata), _MOTION_PLANS_KEY: {pid: p.to_dict() for pid, p in plans.items()}}
    from dataclasses import replace
    return replace(scene, metadata=new_metadata)


def _remove_plan(scene: TeachingSceneDocument, plan_id: str) -> tuple[TeachingSceneDocument, StaggerPlan | None]:
    plans = _read_plans(scene)
    plan = plans.pop(plan_id, None)
    new_metadata = {**dict(scene.metadata)}
    if plans:
        new_metadata[_MOTION_PLANS_KEY] = {pid: p.to_dict() for pid, p in plans.items()}
    else:
        new_metadata.pop(_MOTION_PLANS_KEY, None)
    from dataclasses import replace
    return replace(scene, metadata=new_metadata), plan


def _strip_animations(scene: TeachingSceneDocument, animation_ids: tuple[str, ...]) -> TeachingSceneDocument:
    if not animation_ids:
        return scene
    drop = set(animation_ids)
    from dataclasses import replace
    return replace(scene, animations=tuple(a for a in scene.animations if a.animation_id not in drop))


_SPRING_PRESET_DEFAULTS: dict[str, tuple[SpringConfig, bool]] = {
    "pop": (SpringConfig(mass=1.0, damping=12.0, stiffness=180.0), True),
    "overshoot": (SpringConfig(mass=1.0, damping=7.0, stiffness=170.0), True),
}


def motion_semantics(preset_id: str, params: Mapping[str, Any], fps: float) -> dict[str, Any]:
    """Describe the motion source behind a compiled track, JSON-safe.

    Spring presets record their physical config, settle frame (relative to the
    per-element start) and overshoot policy, so the baked LINEAR keyframes stay
    explainable and recompilable after save/reopen.  Non-spring presets record
    only their kind.  A ``spring_config`` param is normalized to plain numbers
    (a SpringConfig object itself is not JSON-serializable, which previously
    made these params unable to round-trip through scene metadata).
    """
    defaults = _SPRING_PRESET_DEFAULTS.get(preset_id)
    if defaults is None:
        return {"kind": "easing", "preset_id": preset_id}
    base_config, base_overshoot = defaults
    raw_config = params.get("spring_config", base_config)
    # AUD-28113: the compiler (presets._pop_preset/_overshoot_preset) funnels
    # every ``spring_config`` through ``_coerce_spring_config``, whose partial
    # dict defaults are mass=1/damping=10/stiffness=100 — not this preset's
    # base.  Reuse the exact same coercion here so the persisted motion
    # semantics (and settle_frame) always describe the actually compiled
    # track; ripple recompiles and save/reopen then stay consistent.
    from .presets import _coerce_spring_config
    coerced_config = _coerce_spring_config(raw_config)
    config = coerced_config if coerced_config is not None else base_config
    overshoot = bool(params.get("overshoot", base_overshoot)) if preset_id == "pop" else True
    return {
        "kind": "spring",
        "preset_id": preset_id,
        "mass": config.mass,
        "damping": config.damping,
        "stiffness": config.stiffness,
        "overshoot": overshoot,
        "settle_frame": measure_spring(fps, config),
    }


def _build_plan_animations(plan: StaggerPlan, scene: TeachingSceneDocument,
                           registry: MotionPresetRegistry) -> tuple[list[SceneAnimation], list[str], StaggerPlan]:
    """Compile a plan into SceneAnimations; returns (animations, ids, updated_plan)."""
    animations: list[SceneAnimation] = []
    ids: list[str] = []
    starts = plan.starts()
    motion = motion_semantics(plan.preset_id, plan.params, scene.fps)
    for offset, element_id in enumerate(plan.element_ids):
        if scene.element_by_id(element_id) is None:
            raise ValueError(f"Element not found: {element_id}")
        start = starts[offset]
        preset_fn = registry.build(plan.preset_id, start=start, **dict(plan.params))
        track = compile_preset_to_track(
            preset_fn, param_name=plan.param_name,
            start_frame=start, end_frame=start + plan.duration, fps=scene.fps,
        )
        animation_id = _new_id(f"motion_{plan.preset_id}")
        animations.append(SceneAnimation(
            animation_id=animation_id, element_id=element_id,
            property=plan.param_name, track=track,
        ))
        ids.append(animation_id)
    updated = StaggerPlan(**{**plan.to_dict(), "animation_ids": ids, "motion": motion})
    return animations, ids, updated


def _json_safe_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize plan params for metadata persistence (SpringConfig → dict)."""
    safe: dict[str, Any] = {}
    for key, value in params.items():
        if isinstance(value, SpringConfig):
            safe[key] = {"mass": value.mass, "damping": value.damping, "stiffness": value.stiffness}
        else:
            safe[key] = value
    return safe


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ApplyMotionPresetCommand:
    """Apply one motion preset to one element's property (undoable)."""

    element_id: str
    preset_id: str
    param_name: str
    start_frame: int = 0
    end_frame: int | None = None
    params: Mapping[str, Any] = field(default_factory=dict)
    registry: MotionPresetRegistry | None = None
    command_id: str = field(default_factory=lambda: _new_id("apply_motion"))
    label: str = "Apply Motion Preset"
    _animation_id: str | None = None

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._animation_id is not None:
            # AUD-28116：同一命令实例重复 apply 会生成第二个动画并把 undo
            # 令牌覆盖为新 id——先前动画从此无法被 undo 摘除（孤儿）。命令
            # 语义是一次 apply 对应一次 undo；redo（SceneUndoStack 在 undo
            # 后重放同一实例）必须先经 undo 清令牌。
            raise RuntimeError(
                f"apply_motion_preset already applied "
                f"(animation {self._animation_id}); undo first or build a new command")
        if scene.element_by_id(self.element_id) is None:
            raise ValueError(f"Element not found: {self.element_id}")
        registry = self.registry or create_default_preset_registry()
        end = self.end_frame if self.end_frame is not None else scene.duration_frames
        preset_fn = registry.build(self.preset_id, start=self.start_frame, **dict(self.params))
        track = compile_preset_to_track(
            preset_fn, param_name=self.param_name,
            start_frame=self.start_frame, end_frame=end, fps=scene.fps,
        )
        animation_id = _new_id(f"motion_{self.preset_id}")
        object.__setattr__(self, "_animation_id", animation_id)
        animation = SceneAnimation(
            animation_id=animation_id, element_id=self.element_id,
            property=self.param_name, track=track,
        )
        return scene.with_animation_added(animation)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._animation_id is None:
            raise RuntimeError("apply_motion_preset has not been applied")
        from dataclasses import replace
        updated = replace(scene, animations=tuple(
            a for a in scene.animations if a.animation_id != self._animation_id
        ))
        # AUD-28116：undo 后清令牌，同一实例的 redo apply 才能重新写入。
        object.__setattr__(self, "_animation_id", None)
        return updated


@dataclass(frozen=True)
class StaggerElementsCommand:
    """Apply a preset to several elements with a fixed stagger (undoable, ripple-ready)."""

    element_ids: tuple[str, ...]
    preset_id: str
    param_name: str
    interval: int = 12
    duration: int = 18
    delay: int = 0
    anchor_frame: int = 0
    params: Mapping[str, Any] = field(default_factory=dict)
    registry: MotionPresetRegistry | None = None
    plan_id: str | None = None
    command_id: str = field(default_factory=lambda: _new_id("stagger"))
    label: str = "Stagger Elements"
    _plan_id: str | None = None

    def __post_init__(self) -> None:
        if not self.element_ids:
            raise ValueError("element_ids cannot be empty")
        if self.interval < 0:
            raise ValueError("interval must be non-negative")
        if self.duration <= 0:
            raise ValueError("duration must be positive")

    def _build_plan(self, animation_ids: tuple[str, ...]) -> StaggerPlan:
        return StaggerPlan(
            plan_id=self.plan_id or _new_id("stagger"),
            preset_id=self.preset_id, param_name=self.param_name,
            interval=self.interval, delay=self.delay, duration=self.duration,
            anchor_frame=self.anchor_frame, element_ids=self.element_ids,
            params=_json_safe_params(self.params), animation_ids=animation_ids,
        )

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._plan_id is not None:
            # AUD-28116：重复 apply 会写出第二套 plan + 动画并覆盖 undo
            # 令牌，先前的 plan/animations 从此无人回收（孤儿）。与
            # ApplyMotionPresetCommand 同规则：拒绝重复 apply，redo 先 undo。
            raise RuntimeError(
                f"stagger_elements already applied (plan {self._plan_id}); "
                "undo first or build a new command")
        registry = self.registry or create_default_preset_registry()
        plan = self._build_plan(animation_ids=())
        # The compiled plan carries the animation ids AND the motion semantics
        # (spring config / settle frame); rebuilding it here would drop them.
        animations, _ids, final_plan = _build_plan_animations(plan, scene, registry)
        object.__setattr__(self, "_plan_id", final_plan.plan_id)
        updated = scene
        for animation in animations:
            updated = updated.with_animation_added(animation)
        return _write_plan(updated, final_plan)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._plan_id is None:
            raise RuntimeError("stagger_elements has not been applied")
        plans = _read_plans(scene)
        plan = plans.get(self._plan_id)
        ids = plan.animation_ids if plan is not None else ()
        updated = _strip_animations(scene, ids)
        updated, _ = _remove_plan(updated, self._plan_id)
        # AUD-28116：undo 后清令牌，同一实例的 redo apply 才能重新写入。
        object.__setattr__(self, "_plan_id", None)
        return updated


@dataclass(frozen=True)
class SetStaggerIntervalCommand:
    """Change a stagger's interval; every element re-ripples (undoable).

    This is the W1 ``set_beat_timing``: editing the high-level plan recomputes
    every per-element animation from the new interval, so "card 3 should appear
    8 frames later" is a one-parameter edit, not a per-keyframe rewrite.
    """

    plan_id: str
    interval: int
    registry: MotionPresetRegistry | None = None
    command_id: str = field(default_factory=lambda: _new_id("set_interval"))
    label: str = "Set Stagger Interval"
    _previous_plan: StaggerPlan | None = None

    def __post_init__(self) -> None:
        if self.interval < 0:
            raise ValueError("interval must be non-negative")

    def apply(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous_plan is not None:
            # AUD-28116：重复 apply 会把 undo 基线（_previous_plan）覆盖为
            # 中间态——undo 只能回到上一次 apply 前而非最初的 plan，多次
            # undo / redo 语义被破坏。与其它 motion 命令同规则：拒绝重复
            # apply，redo 先经 undo 清令牌。
            raise RuntimeError(
                f"set_stagger_interval already applied (plan {self.plan_id}); "
                "undo first or build a new command")
        plans = _read_plans(scene)
        plan = plans.get(self.plan_id)
        if plan is None:
            raise ValueError(f"Motion plan not found: {self.plan_id}")
        object.__setattr__(self, "_previous_plan", plan)
        registry = self.registry or create_default_preset_registry()
        # Remove the plan's old animations, then recompile at the new interval.
        updated = _strip_animations(scene, plan.animation_ids)
        from dataclasses import replace
        new_plan = replace(plan, interval=self.interval, animation_ids=())
        animations, ids, _ = _build_plan_animations(new_plan, updated, registry)
        recompiled = replace(new_plan, animation_ids=tuple(ids))
        for animation in animations:
            updated = updated.with_animation_added(animation)
        return _write_plan(updated, recompiled)

    def undo(self, scene: TeachingSceneDocument) -> TeachingSceneDocument:
        if self._previous_plan is None:
            raise RuntimeError("set_stagger_interval has not been applied")
        registry = self.registry or create_default_preset_registry()
        plans = _read_plans(scene)
        current = plans.get(self.plan_id)
        updated = _strip_animations(scene, current.animation_ids if current else ())
        previous = self._previous_plan
        animations, ids, _ = _build_plan_animations(
            StaggerPlan(**{**previous.to_dict(), "animation_ids": ()}), updated, registry
        )
        restored_plan = type(previous)(**{**previous.to_dict(), "animation_ids": ids})
        for animation in animations:
            updated = updated.with_animation_added(animation)
        updated = _write_plan(updated, restored_plan)
        # AUD-28116：undo 后清令牌，同一实例的 redo apply 才能重新写入。
        object.__setattr__(self, "_previous_plan", None)
        return updated


def scene_diff(before: TeachingSceneDocument, after: TeachingSceneDocument) -> dict[str, Any]:
    """Readable structural diff for AI/editor feedback (a light SceneDiff)."""
    before_anim_ids = {a.animation_id for a in before.animations}
    after_anim_ids = {a.animation_id for a in after.animations}
    return {
        "added_animations": sorted(after_anim_ids - before_anim_ids),
        "removed_animations": sorted(before_anim_ids - after_anim_ids),
        "duration_changed": before.duration_frames != after.duration_frames,
        "metadata_changed": dict(before.metadata) != dict(after.metadata),
    }


__all__ = [
    "ApplyMotionPresetCommand",
    "SetStaggerIntervalCommand",
    "StaggerElementsCommand",
    "StaggerPlan",
    "compile_preset_to_track",
    "motion_semantics",
    "read_motion_plans",
    "scene_diff",
]
