"""Preview artifacts and storyboard sampling for Motion Graphic plans (MG-03).

MG-03 closes the "confirm before you can see" gap.  Before this, the host
only rendered a 64×36 first-frame check internally; the user was asked to
confirm a clip they had never seen play.  This module defines the artifacts a
preview pass produces — storyboard stills, first/middle/last keyframes, a
low-resolution playable video — plus the deterministic frame sampling so the
same plan always yields the same storyboard.

The render itself runs on the existing render worker / job registry (wired up
in B2 / MG-16).  Here we define:

* :class:`StoryboardFrame` — one sampled still (frame index + label + render
  profile + artifact reference);
* :class:`MotionGraphicPreview` — the bundle returned to the card / approval
  scope, including a ``preview_hash`` bound to the plan's ``plan_hash``;
* :func:`sample_storyboard_frames` — deterministic sampling that picks
  first / middle / last plus N evenly spaced beats;
* :class:`SceneDiffSummary` — a compact description of the
  :class:`~src.features.teaching_animation.ui.editor_controller.SceneDiff`
  produced by the dry run, plus a timeline-diff summary (new clip / track /
  duration) so the card can show "this will add 1 clip of 360 frames".
  Note: instances are *not* hashable despite ``@dataclass(frozen=True)`` —
  ``timeline_insertion`` is a Mapping, so ``hash()`` raises ``TypeError``;
  identity across passes is established through the preview hash instead.

Design rules:

* Qt-free, import-cheap; the host adapts these artifacts to widgets;
* the preview is hash-bound to the plan — a stale plan never reuses an old
  preview artifact silently;
* sampling is deterministic so two preview passes over the same plan produce
  the same storyboard, which is what the audit log needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping, Sequence

from ..core.model import TeachingSceneDocument


# --------------------------------------------------------------------------- #
# Render profile
# --------------------------------------------------------------------------- #


#: Default preview resolutions by tier.  The card always renders the
#: ``storyboard`` tier first; the ``lowres`` tier is produced on demand when
#: the user clicks play.
PREVIEW_RESOLUTIONS: dict[str, tuple[int, int]] = {
    "storyboard": (640, 360),
    "lowres": (1280, 720),
    "preview_full": (1920, 1080),
}


@dataclass(frozen=True)
class RenderProfile:
    """Describes how a preview frame / video was rendered.

    Bound into the preview hash so a re-render at a different resolution is
    not confused with a stale preview.
    """

    width: int
    height: int
    fps: float
    tier: str = "storyboard"           # storyboard / lowres / preview_full
    transparent: bool = False
    backend: str = "svg"               # svg / html / moderngl / webgl

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "tier": self.tier,
            "transparent": self.transparent,
            "backend": self.backend,
        }


# --------------------------------------------------------------------------- #
# Storyboard frame
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StoryboardFrame:
    """One sampled still from the proposed scene.

    ``artifact_ref`` is an opaque string the host's artifact store resolves to
    a path / texture / URL; the model stays free of absolute paths.
    """

    frame_index: int
    label: str
    time_seconds: float
    render_profile: RenderProfile
    artifact_ref: str | None = None
    render_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_index": self.frame_index,
            "label": self.label,
            "time_seconds": self.time_seconds,
            "render_profile": self.render_profile.to_dict(),
            "artifact_ref": self.artifact_ref,
            "render_error": self.render_error,
        }


# --------------------------------------------------------------------------- #
# Scene diff summary
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SceneDiffSummary:
    """Compact description of what the plan changes vs the base scene.

    Built from the :class:`SceneDiff` produced by the dry run plus the
    timeline insertion info.  The card renders this as "新增 4 个元素, 1 段
    动画, 时长 15 秒, 插入第 0 帧".

    Not hashable: despite ``frozen=True`` the ``timeline_insertion`` Mapping
    makes ``hash(summary)`` raise ``TypeError`` (AUD-27001).  Use
    ``to_dict()``-based canonical hashing for identity checks.
    """

    added_element_ids: tuple[str, ...] = ()
    changed_element_ids: tuple[str, ...] = ()
    removed_element_ids: tuple[str, ...] = ()
    animation_changed: bool = False
    duration_changed: bool = False
    base_duration_frames: int = 0
    proposed_duration_frames: int = 0
    timeline_insertion: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "added_element_ids": list(self.added_element_ids),
            "changed_element_ids": list(self.changed_element_ids),
            "removed_element_ids": list(self.removed_element_ids),
            "animation_changed": self.animation_changed,
            "duration_changed": self.duration_changed,
            "base_duration_frames": self.base_duration_frames,
            "proposed_duration_frames": self.proposed_duration_frames,
            "timeline_insertion": dict(self.timeline_insertion),
        }

    def summary_lines(self) -> tuple[str, ...]:
        lines: list[str] = []
        if self.added_element_ids:
            lines.append(f"新增 {len(self.added_element_ids)} 个元素")
        if self.changed_element_ids:
            lines.append(f"修改 {len(self.changed_element_ids)} 个元素")
        if self.removed_element_ids:
            lines.append(f"删除 {len(self.removed_element_ids)} 个元素")
        if self.animation_changed:
            lines.append("动画有变化")
        if self.duration_changed and self.base_duration_frames != self.proposed_duration_frames:
            lines.append(
                f"时长 {self.base_duration_frames} → {self.proposed_duration_frames} 帧"
            )
        insertion = dict(self.timeline_insertion)
        if insertion:
            track = insertion.get("track_label") or insertion.get("track_id") or "Graphics"
            frame = insertion.get("insert_frame", 0)
            duration = insertion.get("duration_frames") or self.proposed_duration_frames
            lines.append(f"插入 {track} 轨第 {frame} 帧，长约 {duration} 帧")
        return tuple(lines)


# --------------------------------------------------------------------------- #
# Preview bundle
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MotionGraphicPreview:
    """The preview bundle shown on the card and bound to the approval scope.

    Attributes:
        preview_id: Stable identifier.
        plan_hash: The plan this preview was rendered from.  When the plan
            changes, the preview is stale and must be regenerated.
        scene_hash: Scene hash at render time (matches the plan).
        storyboard: Sampled stills, always populated for a successful preview.
        video_artifact_ref: Opaque reference to the lowres playable video, or
            ``None`` until the user clicks play.
        render_profile: Profile used for the storyboard stills.
        video_profile: Profile used for the lowres video, if any.
        diff_summary: Compact description of what the preview is showing.
        preview_hash: sha256 of the preview payload; bound into the approval
            scope so "the preview the user saw" == "the plan being confirmed".
        render_errors: Per-frame errors; if any *storyboard* frame errored,
            the preview is considered failed and confirmation is blocked.
        completed_at: Wall clock for ordering.
    """

    preview_id: str
    plan_hash: str
    scene_hash: str
    storyboard: tuple[StoryboardFrame, ...]
    render_profile: RenderProfile
    preview_hash: str
    diff_summary: SceneDiffSummary = field(default_factory=SceneDiffSummary)
    video_artifact_ref: str | None = None
    video_profile: RenderProfile | None = None
    render_errors: tuple[str, ...] = ()
    completed_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.preview_id:
            raise ValueError("MotionGraphicPreview.preview_id is required")
        if not self.plan_hash:
            raise ValueError("MotionGraphicPreview.plan_hash is required")
        if not self.scene_hash:
            raise ValueError("MotionGraphicPreview.scene_hash is required")
        if not self.preview_hash:
            raise ValueError("MotionGraphicPreview.preview_hash is required")

    @property
    def is_failed(self) -> bool:
        """A preview with no storyboard or all-error frames is failed."""
        if not self.storyboard:
            return True
        return all(frame.render_error is not None for frame in self.storyboard)

    @property
    def is_playable(self) -> bool:
        return self.video_artifact_ref is not None and not self.is_failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "preview_id": self.preview_id,
            "plan_hash": self.plan_hash,
            "scene_hash": self.scene_hash,
            "storyboard": [frame.to_dict() for frame in self.storyboard],
            "render_profile": self.render_profile.to_dict(),
            "video_artifact_ref": self.video_artifact_ref,
            "video_profile": self.video_profile.to_dict() if self.video_profile else None,
            "diff_summary": self.diff_summary.to_dict(),
            "preview_hash": self.preview_hash,
            "render_errors": list(self.render_errors),
            "completed_at": self.completed_at,
        }


# --------------------------------------------------------------------------- #
# Frame sampling
# --------------------------------------------------------------------------- #


def sample_storyboard_frames(
    scene: TeachingSceneDocument,
    *,
    count: int = 5,
    fps: float | None = None,
) -> tuple[int, ...]:
    """Return ``count`` deterministic frame indices spanning the scene.

    Always includes frame 0 (first), the last frame and (for count >= 3) the
    middle frame; remaining slots are evenly spaced.  The result is sorted and
    de-duplicated so the caller can render in order.

    The sampling is a pure function of ``(duration_frames, count)`` so two
    preview passes over the same plan always pick the same storyboard — which
    is what the preview-hash binding needs to stay stable.
    """

    duration = max(1, int(scene.duration_frames))
    if count <= 0:
        return ()
    if count == 1:
        return (0,)

    frames: set[int] = {0, duration - 1}
    if count >= 3:
        frames.add(duration // 2)
    # Evenly spaced interior samples.
    needed = max(0, count - len(frames))
    if needed > 0:
        step = duration / (needed + 1)
        for i in range(1, needed + 1):
            frames.add(min(duration - 1, max(0, int(round(i * step)))))
    ordered = sorted(frames)[:count]
    # If dedup shrank the set below count (very short scenes), pad by repeating
    # neighbours deterministically.
    while len(ordered) < count and ordered:
        ordered.append(ordered[-1])
    return tuple(ordered)[:count]


# --------------------------------------------------------------------------- #
# Preview hash + builder
# --------------------------------------------------------------------------- #


def compute_preview_hash(
    *,
    plan_hash: str,
    scene_hash: str,
    render_profile: RenderProfile,
    storyboard_indices: Sequence[int],
    diff_summary: SceneDiffSummary,
) -> str:
    """Stable sha256 for a preview bundle, used by the approval scope."""
    payload = {
        "plan_hash": plan_hash,
        "scene_hash": scene_hash,
        "render_profile": render_profile.to_dict(),
        "storyboard_indices": list(storyboard_indices),
        "diff_summary": diff_summary.to_dict(),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_preview(
    *,
    plan_hash: str,
    scene_hash: str,
    scene: TeachingSceneDocument,
    storyboard: Sequence[StoryboardFrame],
    render_profile: RenderProfile,
    diff_summary: SceneDiffSummary | None = None,
    video_artifact_ref: str | None = None,
    video_profile: RenderProfile | None = None,
    render_errors: Sequence[str] | None = None,
    completed_at: float = 0.0,
    preview_id: str | None = None,
) -> MotionGraphicPreview:
    """Assemble a :class:`MotionGraphicPreview` from rendered frames.

    The storyboard is expected to already be rendered by the caller (the host
    render worker).  This helper just hashes, validates and stamps the bundle
    so the card / approval scope get a consistent object.
    """

    import uuid

    diff = diff_summary or SceneDiffSummary(
        proposed_duration_frames=scene.duration_frames,
    )
    indices = tuple(frame.frame_index for frame in storyboard)
    preview_hash = compute_preview_hash(
        plan_hash=plan_hash,
        scene_hash=scene_hash,
        render_profile=render_profile,
        storyboard_indices=indices,
        diff_summary=diff,
    )
    return MotionGraphicPreview(
        preview_id=preview_id or f"mg-preview-{uuid.uuid4().hex}",
        plan_hash=plan_hash,
        scene_hash=scene_hash,
        storyboard=tuple(storyboard),
        render_profile=render_profile,
        preview_hash=preview_hash,
        diff_summary=diff,
        video_artifact_ref=video_artifact_ref,
        video_profile=video_profile,
        render_errors=tuple(render_errors or ()),
        completed_at=completed_at,
    )


def summarize_scene_diff(
    *,
    proposed_scene: TeachingSceneDocument,
    base_scene: TeachingSceneDocument | None = None,
    changed_element_ids: Sequence[str] = (),
    animation_changed: bool = False,
    timeline_track_id: str | None = None,
    timeline_track_label: str | None = None,
    timeline_insert_frame: int | None = None,
) -> SceneDiffSummary:
    """Build a :class:`SceneDiffSummary` from a base scene and the dry-run output.

    The base scene for a *new* Motion Graphic clip is empty (or a placeholder),
    so every element in ``proposed_scene`` counts as "added".  For scene-edit
    flows the caller passes the actual base scene.
    """

    proposed_ids = {element.element_id for element in proposed_scene.elements}
    if base_scene is None:
        added = tuple(sorted(proposed_ids))
        changed: tuple[str, ...] = ()
        removed: tuple[str, ...] = ()
        base_duration = 0
    else:
        base_ids = {element.element_id for element in base_scene.elements}
        added = tuple(sorted(proposed_ids - base_ids))
        changed = tuple(sorted(set(changed_element_ids) & (proposed_ids & base_ids)))
        removed = tuple(sorted(base_ids - proposed_ids))
        base_duration = base_scene.duration_frames

    insertion: dict[str, Any] = {}
    if timeline_track_id is not None or timeline_track_label is not None:
        insertion = {
            "track_id": timeline_track_id,
            "track_label": timeline_track_label,
            "insert_frame": timeline_insert_frame,
            "duration_frames": proposed_scene.duration_frames,
        }

    return SceneDiffSummary(
        added_element_ids=added,
        changed_element_ids=changed,
        removed_element_ids=removed,
        animation_changed=animation_changed,
        duration_changed=(base_duration != proposed_scene.duration_frames),
        base_duration_frames=base_duration,
        proposed_duration_frames=proposed_scene.duration_frames,
        timeline_insertion=insertion,
    )


__all__ = [
    "MotionGraphicPreview",
    "RenderProfile",
    "SceneDiffSummary",
    "StoryboardFrame",
    "PREVIEW_RESOLUTIONS",
    "build_preview",
    "compute_preview_hash",
    "sample_storyboard_frames",
    "summarize_scene_diff",
]
