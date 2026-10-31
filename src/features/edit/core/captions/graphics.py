"""字幕图形片段的领域模型、模板注册表与解析服务。

字幕仍然是唯一的语义事实源；本模块只保存绑定关系和图形模板引用。
图形片段本身复用时间线的普通 ``ClipInstance``，由绑定通过 clip id 解析
回字幕 cue，避免复制一份无法追踪的文本和时间码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping
import uuid


DISPLAY_MODES = frozenset({"native", "graphic", "both", "disabled"})
MATERIALIZATIONS = frozenset({"live", "cached", "detached"})


def _copy_dict(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class CaptionGraphicBinding:
    """One cue-to-graphic relationship.

    ``caption_cue_id`` is optional for future track-template materialization;
    single-cue clips always set it. Text and timing are intentionally absent:
    they are resolved from the source ``SubtitleClip`` at render/commit time.
    """

    binding_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    caption_track_id: str = ""
    caption_cue_id: str | None = None
    graphic_clip_id: str = ""
    composite_id: str | None = None
    template_id: str = "fade"
    display_mode: str = "graphic"
    sync_text: bool = True
    sync_timing: bool = True
    sync_style: bool = False
    materialization: str = "live"
    template_version: str = "1"
    parameters: dict[str, Any] = field(default_factory=dict)
    # Snapshots are only fallbacks for disabled sync flags; CaptionCue remains
    # the source of truth whenever the corresponding flag is enabled.
    text_snapshot: str | None = None
    style_snapshot: dict[str, Any] = field(default_factory=dict)
    source_snapshot: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.binding_id or not self.caption_track_id or not self.graphic_clip_id:
            raise ValueError("CaptionGraphicBinding ids cannot be empty")
        if self.display_mode not in DISPLAY_MODES:
            raise ValueError(f"Invalid graphic display mode: {self.display_mode}")
        if self.materialization not in MATERIALIZATIONS:
            raise ValueError(f"Invalid graphic materialization: {self.materialization}")

    def with_sync(self, *, text: bool | None = None, timing: bool | None = None,
                  style: bool | None = None) -> "CaptionGraphicBinding":
        return self._replace(
            sync_text=self.sync_text if text is None else bool(text),
            sync_timing=self.sync_timing if timing is None else bool(timing),
            sync_style=self.sync_style if style is None else bool(style),
        )

    def detached(self, snapshot: Mapping[str, Any] | None = None) -> "CaptionGraphicBinding":
        return self._replace(
            materialization="detached", sync_text=False, sync_timing=False,
            sync_style=False, source_snapshot=_copy_dict(snapshot),
        )

    def _replace(self, **changes: Any) -> "CaptionGraphicBinding":
        values = {
            "binding_id": self.binding_id,
            "caption_track_id": self.caption_track_id,
            "caption_cue_id": self.caption_cue_id,
            "graphic_clip_id": self.graphic_clip_id,
            "composite_id": self.composite_id,
            "template_id": self.template_id,
            "display_mode": self.display_mode,
            "sync_text": self.sync_text,
            "sync_timing": self.sync_timing,
            "sync_style": self.sync_style,
            "materialization": self.materialization,
            "template_version": self.template_version,
            "parameters": dict(self.parameters),
            "text_snapshot": self.text_snapshot,
            "style_snapshot": dict(self.style_snapshot),
            "source_snapshot": dict(self.source_snapshot),
            "extra": dict(self.extra),
        }
        values.update(changes)
        return CaptionGraphicBinding(**values)

    def to_dict(self) -> dict[str, Any]:
        value = {
            "binding_id": self.binding_id,
            "caption_track_id": self.caption_track_id,
            "caption_cue_id": self.caption_cue_id,
            "graphic_clip_id": self.graphic_clip_id,
            "composite_id": self.composite_id,
            "template_id": self.template_id,
            "display_mode": self.display_mode,
            "sync_text": self.sync_text,
            "sync_timing": self.sync_timing,
            "sync_style": self.sync_style,
            "materialization": self.materialization,
            "template_version": self.template_version,
            "parameters": dict(self.parameters),
            "text_snapshot": self.text_snapshot,
            "style_snapshot": dict(self.style_snapshot),
            "source_snapshot": dict(self.source_snapshot),
        }
        value.update(self.extra)
        return value

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "CaptionGraphicBinding":
        known = {
            "binding_id", "caption_track_id", "caption_cue_id", "graphic_clip_id",
            "composite_id", "template_id", "display_mode", "sync_text",
            "sync_timing", "sync_style", "materialization", "template_version",
            "parameters", "text_snapshot", "style_snapshot",
            "source_snapshot",
        }
        return CaptionGraphicBinding(
            binding_id=str(data.get("binding_id") or uuid.uuid4()),
            caption_track_id=str(data.get("caption_track_id") or ""),
            caption_cue_id=(str(data["caption_cue_id"]) if data.get("caption_cue_id") is not None else None),
            graphic_clip_id=str(data.get("graphic_clip_id") or ""),
            composite_id=(str(data["composite_id"]) if data.get("composite_id") else None),
            template_id=str(data.get("template_id") or "fade"),
            display_mode=str(data.get("display_mode") or "graphic"),
            sync_text=bool(data.get("sync_text", True)),
            sync_timing=bool(data.get("sync_timing", True)),
            sync_style=bool(data.get("sync_style", False)),
            materialization=str(data.get("materialization") or "live"),
            template_version=str(data.get("template_version") or "1"),
            parameters=_copy_dict(data.get("parameters")),
            text_snapshot=(str(data["text_snapshot"]) if data.get("text_snapshot") is not None else None),
            style_snapshot=_copy_dict(data.get("style_snapshot")),
            source_snapshot=_copy_dict(data.get("source_snapshot")),
            extra={key: value for key, value in data.items() if key not in known},
        )


@dataclass(frozen=True)
class GraphicClip:
    """Serializable semantic view of a materialized graphic clip.

    The timeline stores the runtime instance as ``ClipInstance`` so it can
    reuse all video-track operations. This lightweight contract is useful to
    project/Composite integrations that must not depend on that implementation
    detail.
    """

    clip_id: str
    track_id: str
    timeline_start: int
    timeline_duration: int
    source_caption_id: str | None = None
    template_id: str = "fade"
    composite_id: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.clip_id or not self.track_id:
            raise ValueError("GraphicClip ids cannot be empty")
        if self.timeline_start < 0 or self.timeline_duration <= 0:
            raise ValueError("GraphicClip has an invalid time range")

    @property
    def timeline_end(self) -> int:
        return self.timeline_start + self.timeline_duration

    def to_dict(self) -> dict[str, Any]:
        return {
            "clip_id": self.clip_id, "track_id": self.track_id,
            "timeline_start": self.timeline_start,
            "timeline_duration": self.timeline_duration,
            "source_caption_id": self.source_caption_id,
            "template_id": self.template_id,
            "composite_id": self.composite_id,
            "parameters": dict(self.parameters),
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "GraphicClip":
        return GraphicClip(
            clip_id=str(data.get("clip_id") or ""),
            track_id=str(data.get("track_id") or ""),
            timeline_start=int(data.get("timeline_start", 0)),
            timeline_duration=int(data.get("timeline_duration", 1)),
            source_caption_id=(str(data["source_caption_id"]) if data.get("source_caption_id") else None),
            template_id=str(data.get("template_id") or "fade"),
            composite_id=(str(data["composite_id"]) if data.get("composite_id") else None),
            parameters=_copy_dict(data.get("parameters")),
        )

@dataclass(frozen=True)
class CaptionTrackGraphicStyle:
    """Lazy track-level template reference; no per-cue graphic clips required."""

    track_id: str = ""
    template_id: str = "fade"
    template_version: str = "1"
    parameters: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    fallback_to_native: bool = True
    extra: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        value = {
            "track_id": self.track_id,
            "template_id": self.template_id,
            "template_version": self.template_version,
            "parameters": dict(self.parameters),
            "enabled": self.enabled,
            "fallback_to_native": self.fallback_to_native,
        }
        value.update(self.extra)
        return value

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "CaptionTrackGraphicStyle":
        known = {"track_id", "template_id", "template_version", "parameters", "enabled", "fallback_to_native"}
        return CaptionTrackGraphicStyle(
            track_id=str(data.get("track_id") or ""),
            template_id=str(data.get("template_id") or "fade"),
            template_version=str(data.get("template_version") or "1"),
            parameters=_copy_dict(data.get("parameters")),
            enabled=bool(data.get("enabled", True)),
            fallback_to_native=bool(data.get("fallback_to_native", True)),
            extra={key: value for key, value in data.items() if key not in known},
        )


@dataclass(frozen=True)
class CaptionGraphicTemplate:
    """Versioned template descriptor exposed to Edit and Composite pages."""

    template_id: str
    version: str = "1"
    effect: str = "fade"
    defaults: dict[str, Any] = field(default_factory=dict)
    published_parameters: tuple[str, ...] = ()
    compatible_effects: tuple[str, ...] = ("opacity", "transform", "crop", "blend")
    is_published: bool = True
    # Kept after the original constructor fields so integrations which create
    # templates positionally retain their pre-existing argument order.
    label: str = ""
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "version": self.version,
            "effect": self.effect,
            "label": self.label,
            "description": self.description,
            "defaults": dict(self.defaults),
            "published_parameters": list(self.published_parameters),
            "compatible_effects": list(self.compatible_effects),
            "is_published": self.is_published,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "CaptionGraphicTemplate":
        return CaptionGraphicTemplate(
            template_id=str(data.get("template_id") or ""),
            version=str(data.get("version") or "1"),
            effect=str(data.get("effect") or "fade"),
            label=str(data.get("label") or ""),
            description=str(data.get("description") or ""),
            defaults=_copy_dict(data.get("defaults")),
            published_parameters=tuple(str(v) for v in (data.get("published_parameters") or ())),
            compatible_effects=tuple(str(v) for v in (data.get("compatible_effects") or ("opacity", "transform", "crop", "blend"))),
            is_published=bool(data.get("is_published", True)),
        )


BUILTIN_CAPTION_GRAPHIC_TEMPLATES: dict[str, CaptionGraphicTemplate] = {
    "fade": CaptionGraphicTemplate(
        "fade", effect="fade", label="淡入淡出",
        description="克制的淡入淡出，适合对话和纪录片字幕。",
        defaults={"in_frames": 8, "out_frames": 8},
        published_parameters=("in_frames", "out_frames"),
    ),
    "typewriter": CaptionGraphicTemplate(
        "typewriter", effect="typewriter", label="打字机",
        description="从左至右逐步显现文字，再平滑淡出。",
        defaults={"in_frames": 12, "out_frames": 6},
        published_parameters=("in_frames", "out_frames"),
    ),
    "scale_pop": CaptionGraphicTemplate(
        "scale_pop", effect="scale_pop", label="弹跳缩放",
        description="带受控回弹的缩放出现，适合社交媒体字幕和标注。",
        defaults={"in_frames": 8, "out_frames": 8, "overshoot": 0.12},
        published_parameters=("in_frames", "out_frames", "overshoot"),
    ),
    "slide_up": CaptionGraphicTemplate(
        "slide_up", effect="slide_up", label="向上滑入",
        description="从字幕下方上滑至停留位置。",
        defaults={"in_frames": 10, "out_frames": 8, "distance": 1.0},
        published_parameters=("in_frames", "out_frames", "distance"),
    ),
    "slide_down": CaptionGraphicTemplate(
        "slide_down", effect="slide_down", label="向下滑入",
        description="从字幕上方下滑至停留位置。",
        defaults={"in_frames": 10, "out_frames": 8, "distance": 1.0},
        published_parameters=("in_frames", "out_frames", "distance"),
    ),
    "slide_left": CaptionGraphicTemplate(
        "slide_left", effect="slide_left", label="从左滑入",
        description="从左侧进入、向右侧离开。",
        defaults={"in_frames": 10, "out_frames": 8, "distance": 1.0},
        published_parameters=("in_frames", "out_frames", "distance"),
    ),
    "slide_right": CaptionGraphicTemplate(
        "slide_right", effect="slide_right", label="从右滑入",
        description="从右侧进入、向左侧离开。",
        defaults={"in_frames": 10, "out_frames": 8, "distance": 1.0},
        published_parameters=("in_frames", "out_frames", "distance"),
    ),
    "wipe_reveal": CaptionGraphicTemplate(
        "wipe_reveal", effect="wipe_reveal", label="擦除显现",
        description="干净的方向性遮罩显现，不改变原有文字样式。",
        defaults={"in_frames": 12, "out_frames": 6},
        published_parameters=("in_frames", "out_frames"),
    ),
}


class CaptionGraphicTemplateRegistry:
    """Small provider-neutral registry used by render, Composite and AI."""

    def __init__(self, templates: Mapping[str, CaptionGraphicTemplate] | None = None) -> None:
        self._templates = dict(BUILTIN_CAPTION_GRAPHIC_TEMPLATES)
        if templates:
            self._templates.update(templates)

    def get(self, template_id: str, version: str | None = None) -> CaptionGraphicTemplate | None:
        template = self._templates.get(template_id)
        if template is None:
            return None
        if not template.is_published:
            return None
        if version is not None and template.version != version:
            return None
        return template

    def register(self, template: CaptionGraphicTemplate) -> None:
        if not template.template_id:
            raise ValueError("template_id cannot be empty")
        self._templates[template.template_id] = template

    def publish(self, template: CaptionGraphicTemplate) -> CaptionGraphicTemplate:
        """Validate and register a published template definition."""
        if not template.is_published:
            raise ValueError(f"Template is not publishable: {template.template_id}")
        if not template.effect:
            raise ValueError(f"Template effect cannot be empty: {template.template_id}")
        self.register(template)
        return template

    def diagnose(self, template_id: str, version: str | None = None) -> str | None:
        """Return a user-facing missing/version diagnostic, or ``None``."""
        template = self._templates.get(template_id)
        if template is None:
            return f"Missing caption graphic template: {template_id}"
        if version is not None and template.version != version:
            return (
                f"Caption graphic template {template_id} is version "
                f"{template.version}, requested {version}"
            )
        if not template.is_published:
            return f"Caption graphic template is unpublished: {template_id}@{template.version}"
        return None

    def all(self) -> tuple[CaptionGraphicTemplate, ...]:
        return tuple(self._templates.values())


DEFAULT_CAPTION_GRAPHIC_TEMPLATES = CaptionGraphicTemplateRegistry()


def migrate_caption_graphics_block(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize and preserve a serialized caption-graphics block."""
    raw = _copy_dict(data)
    version = int(raw.get("caption_graphics_schema_version", 1) or 1)
    if version > 1:
        raise ValueError(f"Caption graphics schema {version} is newer than supported 1")
    raw["caption_graphics_schema_version"] = 1
    raw.setdefault("caption_graphic_bindings", [])
    raw.setdefault("caption_track_graphic_styles", [])
    raw.setdefault("composite_templates", [])
    return raw


def template_parameters(template: CaptionGraphicTemplate, overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    values = dict(template.defaults)
    values.update(_copy_dict(overrides))
    return values


@dataclass(frozen=True)
class CaptionGraphicAnimationState:
    """Per-frame graphic-caption state shared by preview and export.

    ``translate_*`` are canvas pixels, while ``reveal`` is a 0..1 crop of the
    compact glyph texture.  Keeping this a pure value avoids separate preview
    and export easing implementations drifting apart.
    """

    opacity: float = 1.0
    translate_x: float = 0.0
    translate_y: float = 0.0
    scale: float = 1.0
    reveal: float = 1.0


_CAPTION_GRAPHIC_EFFECTS = frozenset({
    "fade", "typewriter", "scale_pop", "slide_up", "slide_down",
    "slide_left", "slide_right", "wipe_reveal",
})


def _ease_out_cubic(value: float) -> float:
    value = max(0.0, min(1.0, float(value)))
    return 1.0 - (1.0 - value) ** 3


def caption_graphic_animation_state(
    effect: str,
    local_frame: int,
    duration: int,
    params: Mapping[str, Any],
    *,
    content_width: float = 0.0,
    content_height: float = 0.0,
) -> CaptionGraphicAnimationState:
    """Return the canonical animation state for a subtitle graphic frame.

    The spatial distance is intentionally expressed in glyph-size multiples,
    not screen pixels.  A subtitle therefore travels the same visual amount at
    1080p and 4K, while the editor's ``distance`` control remains intuitive.
    Unknown effect ids fail safely to a static caption for forward compatibility.
    """
    if effect not in _CAPTION_GRAPHIC_EFFECTS:
        return CaptionGraphicAnimationState()

    duration = max(1, int(duration))
    local = max(0, min(int(local_frame), duration - 1))
    in_frames = max(0, int(params.get("in_frames", 0) or 0))
    out_frames = max(0, int(params.get("out_frames", 0) or 0))
    distance = max(0.0, float(params.get("distance", 1.0) or 0.0))
    overshoot = max(0.0, min(0.5, float(params.get("overshoot", 0.12) or 0.0)))

    in_progress = 1.0
    if in_frames > 0:
        in_progress = _ease_out_cubic((local + 1) / in_frames)
    remaining = duration - local
    out_progress = 1.0
    if out_frames > 0:
        out_progress = _ease_out_cubic(remaining / out_frames)

    opacity = 1.0
    translate_x = 0.0
    translate_y = 0.0
    scale = 1.0
    reveal = 1.0

    is_entering = in_frames > 0 and local < in_frames
    is_exiting = out_frames > 0 and remaining <= out_frames

    if effect == "fade":
        if is_entering:
            opacity = min(opacity, in_progress)
        if is_exiting:
            opacity = min(opacity, out_progress)
    elif effect in {"typewriter", "wipe_reveal"}:
        if is_entering:
            reveal = min(reveal, in_progress)
        if is_exiting:
            reveal = min(reveal, out_progress)
            # A disappearing glyph should not leave a hard last-frame pop.
            opacity = min(opacity, out_progress)
    elif effect == "scale_pop":
        if is_entering:
            opacity = min(opacity, in_progress)
            # The sinusoid returns to zero at both ends, so the final resting
            # scale is exactly 1.0 rather than permanently enlarged.
            scale = 0.72 + 0.28 * in_progress + overshoot * (
                4.0 * in_progress * (1.0 - in_progress)
            )
        if is_exiting:
            opacity = min(opacity, out_progress)
            scale = min(scale, 0.72 + 0.28 * out_progress)
    else:
        horizontal = max(24.0, float(content_width) * 1.15) * distance
        vertical = max(24.0, float(content_height) * 1.15) * distance
        if is_entering:
            opacity = min(opacity, in_progress)
            travel = 1.0 - in_progress
            if effect == "slide_up":
                translate_y += vertical * travel
            elif effect == "slide_down":
                translate_y -= vertical * travel
            elif effect == "slide_left":
                translate_x -= horizontal * travel
            elif effect == "slide_right":
                translate_x += horizontal * travel
        if is_exiting:
            opacity = min(opacity, out_progress)
            travel = 1.0 - out_progress
            if effect == "slide_up":
                translate_y -= vertical * travel
            elif effect == "slide_down":
                translate_y += vertical * travel
            elif effect == "slide_left":
                translate_x += horizontal * travel
            elif effect == "slide_right":
                translate_x -= horizontal * travel

    return CaptionGraphicAnimationState(
        opacity=max(0.0, min(1.0, opacity)),
        translate_x=translate_x,
        translate_y=translate_y,
        scale=max(0.01, scale),
        reveal=max(0.0, min(1.0, reveal)),
    )


def animation_opacity(effect: str, local_frame: int, duration: int, params: Mapping[str, Any]) -> float:
    """Compatibility accessor for consumers that only need alpha."""
    return caption_graphic_animation_state(
        effect, local_frame, duration, params
    ).opacity


def renderable_graphic_for_cue(timeline: Any, cue: Any, *, frame: int | None = None) -> tuple[CaptionGraphicBinding, CaptionGraphicTemplate, dict[str, Any]] | None:
    """Resolve a cue binding or lazy track style, with missing-template status."""
    binding = next((item for item in timeline.caption_graphic_bindings
                    if item.caption_cue_id == cue.clip_id), None)
    if binding is not None:
        template = timeline.get_caption_graphic_template_registry().get(
            binding.template_id, binding.template_version
        )
        if template is None:
            return None
        return binding, template, template_parameters(template, binding.parameters)
    track_style = next((item for item in timeline.caption_track_graphic_styles
                        if item.track_id == cue.track_id and item.enabled), None)
    if track_style is None:
        return None
    template = timeline.get_caption_graphic_template_registry().get(
        track_style.template_id, track_style.template_version
    )
    if template is None:
        return None
    synthetic = CaptionGraphicBinding(
        binding_id=f"track-style:{track_style.track_id}",
        caption_track_id=track_style.track_id,
        caption_cue_id=cue.clip_id,
        graphic_clip_id=f"track-style:{cue.clip_id}",
        template_id=track_style.template_id,
        template_version=track_style.template_version,
        parameters=dict(track_style.parameters),
    )
    return synthetic, template, template_parameters(template, track_style.parameters)


def cue_snapshot_for_graphic(cue: Any) -> dict[str, Any]:
    """Capture the source values needed when a binding disables sync flags."""
    from src.features.edit.core.captions.model import STYLE_FIELDS

    return {
        "text": str(getattr(cue, "text", "")),
        "style_fields": {field: getattr(cue, field) for field in STYLE_FIELDS},
        "style_override": (
            cue.style_override.to_dict()
            if getattr(cue, "style_override", None) is not None else None
        ),
    }


def clone_caption_graphic_binding(
    binding: CaptionGraphicBinding,
    graphic_clip_id: str,
    cue: Any | None = None,
) -> CaptionGraphicBinding:
    """Create an independent detached binding for copy/paste."""
    snapshot = (
        cue_snapshot_for_graphic(cue)
        if cue is not None else dict(binding.source_snapshot)
    )
    extra = dict(binding.extra)
    extra["copied_from_binding_id"] = binding.binding_id
    return binding._replace(
        binding_id=str(uuid.uuid4()),
        graphic_clip_id=graphic_clip_id,
        materialization="detached",
        sync_text=False,
        sync_timing=False,
        sync_style=False,
        source_snapshot=snapshot,
        extra=extra,
    )


def render_snapshot_for_binding(cue: Any, binding: CaptionGraphicBinding) -> dict[str, Any] | None:
    """Resolve the immutable fallback values used by a graphic binding."""
    if binding.materialization == "detached":
        return dict(binding.source_snapshot) or None
    snapshot: dict[str, Any] = {}
    if not binding.sync_text and binding.text_snapshot is not None:
        snapshot["text"] = binding.text_snapshot
    if not binding.sync_style and binding.style_snapshot:
        style_snapshot = dict(binding.style_snapshot)
        style_snapshot.pop("text", None)
        snapshot.update(style_snapshot)
    return snapshot or None


@dataclass(frozen=True)
class CaptionCompositeInput:
    """Controlled Composite input; no graph JSON may replace the source cue."""

    caption_cue_id: str
    text: str
    start: int
    end: int
    word_timings: tuple[dict[str, Any], ...]
    language: str
    style: dict[str, Any]


def build_composite_input(timeline: Any, cue_id: str) -> CaptionCompositeInput:
    cue = timeline.get_subtitle_clip_by_id(cue_id)
    if cue is None:
        raise ValueError(f"Subtitle clip not found: {cue_id}")
    track = timeline.get_track_by_id(cue.track_id)
    metadata = getattr(track, "caption_metadata", None)
    from src.features.edit.core.captions.model import (
        EMPTY_STYLE,
        clip_style_base,
        resolve_style,
    )
    default_style = (
        metadata.default_style
        if metadata is not None and metadata.default_style is not None
        else EMPTY_STYLE
    )
    style = resolve_style(default_style, cue.style_override, base=clip_style_base(cue))
    return CaptionCompositeInput(
        caption_cue_id=cue.clip_id,
        text=cue.text,
        start=cue.timeline_start,
        end=cue.timeline_end,
        word_timings=tuple(item.to_dict() for item in cue.caption_metadata.word_timings),
        language=str(getattr(metadata, "language", "") or ""),
        style=style,
    )


def render_caption_graphic_rgba(
    cue: Any,
    track: Any,
    template: CaptionGraphicTemplate,
    parameters: Mapping[str, Any],
    width: int,
    height: int,
    local_frame: int,
    source_snapshot: Mapping[str, Any] | None = None,
) -> Any:
    """Render a graphic caption through the canonical subtitle rasterizer.

    The function is intentionally imported by preview/export workers only;
    callers remain responsible for keeping GPU work on the existing render
    thread. The templates here are alpha/mask operations, so preview and
    export share the exact same CPU semantics.
    """
    from src.features.edit.core.captions.model import (
        CaptionStyle,
        EMPTY_STYLE,
        STYLE_FIELDS,
        clip_style_base,
        resolve_style,
    )
    from src.features.edit.core.captions.render_plan import pivot_texture_local
    from src.features.edit.core.subtitle_renderer import (
        _resolved_value,
        _warp_rgba_to_canvas,
        get_subtitle_texture_cached,
    )

    if source_snapshot:
        snapshot_fields = source_snapshot.get("style_fields")
        updates = {
            key: snapshot_fields[key]
            for key in STYLE_FIELDS
            if isinstance(snapshot_fields, Mapping) and key in snapshot_fields
        }
        if "text" in source_snapshot:
            updates["text"] = str(source_snapshot["text"])
        if "style_override" in source_snapshot:
            raw_override = source_snapshot.get("style_override")
            updates["style_override"] = (
                CaptionStyle.from_dict(raw_override)
                if isinstance(raw_override, Mapping) else None
            )
        if updates:
            cue = cue.with_params(**updates)

    default_style = getattr(getattr(track, "caption_metadata", None), "default_style", EMPTY_STYLE)
    style = resolve_style(default_style, getattr(cue, "style_override", None), base=clip_style_base(cue))
    # Keep the glyph texture compact until the final warp.  The old path first
    # allocated a full-canvas subtitle image and then copied it again solely to
    # animate alpha; at 4K that meant tens of MiB per animated cue per frame.
    # Reusing the compact glyph cache keeps animation cost proportional to the
    # subtitle itself, not the project frame size.
    texture = get_subtitle_texture_cached(cue, int(width), int(height), style=style)
    duration = max(1, int(cue.timeline_duration))
    animation = caption_graphic_animation_state(
        template.effect,
        int(local_frame),
        duration,
        parameters,
        content_width=texture.width,
        content_height=texture.height,
    )
    glyph_rgba = texture.rgba
    if animation.reveal < 1.0:
        # Typewriter and wipe are texture-local masks.  Cropping the compact
        # glyph image (rather than the whole canvas) fixes the historical bug
        # where captions positioned away from x=0 appeared all at once.
        glyph_rgba = texture.rgba.copy()
        revealed_columns = int(round(texture.width * animation.reveal))
        glyph_rgba[:, max(0, revealed_columns):, :] = 0.0

    pivot_x, pivot_y = pivot_texture_local(
        cue, texture.x, texture.y, texture.width, texture.height, style=style
    )
    rgba = _warp_rgba_to_canvas(
        glyph_rgba,
        texture.width,
        texture.height,
        int(width),
        int(height),
        texture.x,
        texture.y,
        _resolved_value(cue, style, "scale_x") * animation.scale,
        _resolved_value(cue, style, "scale_y") * animation.scale,
        _resolved_value(cue, style, "rotation"),
        pivot_x,
        pivot_y,
        _resolved_value(cue, style, "translate_x") + animation.translate_x,
        _resolved_value(cue, style, "translate_y") + animation.translate_y,
    )
    if animation.opacity < 1.0:
        rgba[..., 3] *= animation.opacity
    return rgba


__all__ = [
    "DISPLAY_MODES", "MATERIALIZATIONS", "CaptionGraphicBinding",
    "GraphicClip",
    "CaptionTrackGraphicStyle", "CaptionGraphicTemplate",
    "CaptionGraphicTemplateRegistry", "DEFAULT_CAPTION_GRAPHIC_TEMPLATES",
    "BUILTIN_CAPTION_GRAPHIC_TEMPLATES", "CaptionCompositeInput",
    "build_composite_input", "migrate_caption_graphics_block",
    "template_parameters", "CaptionGraphicAnimationState",
    "caption_graphic_animation_state", "animation_opacity", "renderable_graphic_for_cue",
    "cue_snapshot_for_graphic", "clone_caption_graphic_binding",
    "render_snapshot_for_binding",
    "render_caption_graphic_rgba",
]


# ──────────────────────────────────────────────────────────────────────────
# C1: cue 级入场/出场动画（扩展 word-level 枚举到 cue 级）
# ──────────────────────────────────────────────────────────────────────────

#: cue 级动画枚举（与既有 graphic 模板 effect 命名保持同词汇）
CUE_ANIMATIONS = frozenset({
    "none", "fade", "slide_up", "slide_left", "scale_pop", "typewriter",
})

#: 统一 ease-out（cubic out）：t → 1-(1-t)^3，入场/出场共用
_EASE_OUT = "(1-(1-t)^3) 的闭式在下方内联实现"


def _ease_out(t: float) -> float:
    t = max(0.0, min(1.0, float(t)))
    return 1.0 - (1.0 - t) ** 3


def cue_animation_envelope(
    local_frame: int,
    duration: int,
    *,
    in_animation: str = "none",
    out_animation: str = "none",
    in_frames: int = 12,
    out_frames: int = 8,
) -> tuple[float, float, float, float]:
    """cue 级动画包络：返回 ``(opacity, translate_x_ratio, translate_y_ratio, scale)``。

    AUD-15900: X/Y 两个轴向的位移比分别返回——旧实现把两轴相加成单一
    标量（声称 translate_y_ratio），``in="slide_left" + out="slide_up"``
    等异轴组合被错误地叠加成单轴偏移；方向已对齐同文件规范实现
    ``caption_graphic_animation_state`` 的 Y-down 惯例（slide_up 从下方
    进入 = 正 y 位移）。

    时间函数统一 ease-out；in/out 动画可独立指定：
    - fade: 仅 opacity 0→1 / 1→0
    - slide_up / slide_left: 位移比 1→0（进入方向，进入/出场对称反向）
    - scale_pop: scale 0.8→1（带 ease-out），末段对称
    - typewriter: 仅作用于文本逐字显示（消费端按 progress 裁剪），
      此处 opacity 恒 1
    - none: 恒定 (1, 0, 0, 1)

    纯函数、确定性，CPU/GPU 渲染均可消费。
    """
    duration = max(1, int(duration))
    local = max(0, min(int(local_frame), duration))
    in_frames = max(0, int(in_frames))
    out_frames = max(0, int(out_frames))

    opacity = 1.0
    translate_y = 0.0
    translate_x = 0.0
    scale = 1.0

    if in_animation != "none" and in_frames > 0 and local < in_frames:
        t = _ease_out((local + 1) / in_frames)
        if in_animation == "fade":
            opacity = t
        elif in_animation == "slide_up":
            opacity = t
            # Y-down：从下方进入（正 y 偏移随进度归零）
            translate_y = 1.0 - t
        elif in_animation == "slide_left":
            opacity = t
            translate_x = -(1.0 - t)
        elif in_animation == "scale_pop":
            opacity = t
            scale = 0.8 + 0.2 * t

    remaining = duration - local
    if out_animation != "none" and out_frames > 0 and remaining < out_frames:
        t = _ease_out(remaining / out_frames)
        if out_animation == "fade":
            opacity = min(opacity, t)
        elif out_animation == "slide_up":
            opacity = min(opacity, t)
            # Y-down：向上方退出（负 y 偏移）
            translate_y = -(1.0 - t)
        elif out_animation == "slide_left":
            opacity = min(opacity, t)
            translate_x = 1.0 - t
        elif out_animation == "scale_pop":
            opacity = min(opacity, t)
            scale = min(scale, 0.8 + 0.2 * t)

    return (opacity, translate_x, translate_y, scale)
