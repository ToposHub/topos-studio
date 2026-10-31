"""MT-11: scene-owned audio plan (mixing delegated to Edit/Export).

A Motion Graphic scene may carry narration/music/sound cues whose timing
belongs to the scene's time system, so independent editor, Clip and export all
keep them in sync.  The MG module does NOT build an audio engine: it produces an
``AudioRenderPlan`` (start frame, duration, gain envelope, ducking intent) that
the existing Edit/Export audio services consume.

Audio that cannot be mixed must be diagnosed (blocked or explicit silent
output) rather than silently dropped — see the plan's release gate §7.2.10.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping


class DuckingIntent(str, Enum):
    """How an audio clip interacts with the mix around it."""

    NONE = "none"
    DUCK_OTHERS = "duck_others"     # lower other clips while this plays
    SIDECHAIN = "sidechain"         # sidechain compression target


@dataclass(frozen=True)
class AudioClip:
    """One scene-owned audio reference with timing + mix intent."""

    clip_id: str
    asset_ref: str                  # SceneAssetRef.asset_id
    duration_frames: int
    start_frame: int = 0            # scene-local
    gain: float = 1.0               # linear 0..N (1.0 = unity)
    fade_in_frames: int = 0
    fade_out_frames: int = 0
    loop: bool = False
    ducking: DuckingIntent = DuckingIntent.NONE
    duck_amount: float = 0.5        # gain applied to ducked others (0..1)
    source_start_frame: int = 0     # seek offset after a scene/timeline trim

    def __post_init__(self) -> None:
        if not self.clip_id:
            raise ValueError("clip_id cannot be empty")
        if not self.asset_ref:
            raise ValueError("asset_ref cannot be empty")
        if self.start_frame < 0 or self.duration_frames <= 0:
            raise ValueError("audio range invalid")
        if self.source_start_frame < 0:
            raise ValueError("source_start_frame must be non-negative")
        if self.gain < 0.0:
            raise ValueError("gain must be non-negative")
        if self.fade_in_frames < 0 or self.fade_out_frames < 0:
            raise ValueError("fade frames must be non-negative")
        if self.fade_in_frames + self.fade_out_frames > self.duration_frames:
            raise ValueError("fade in+out cannot exceed clip duration")
        if not 0.0 <= self.duck_amount <= 1.0:
            raise ValueError("duck_amount must be in [0,1]")

    @property
    def end_frame(self) -> int:
        return self.start_frame + self.duration_frames

    def gain_at(self, local_frame: int) -> float:
        """Linear gain envelope at a clip-local frame (fade in/out applied)."""
        if local_frame < 0 or local_frame >= self.duration_frames:
            return 0.0
        env = self.gain
        if self.fade_in_frames and local_frame < self.fade_in_frames:
            env *= local_frame / self.fade_in_frames
        remaining = self.duration_frames - local_frame
        if self.fade_out_frames and remaining <= self.fade_out_frames:
            env *= max(0.0, remaining / self.fade_out_frames)
        return env

    def to_dict(self) -> dict[str, Any]:
        return {
            "clip_id": self.clip_id, "asset_ref": self.asset_ref,
            "start_frame": self.start_frame, "duration_frames": self.duration_frames,
            "gain": self.gain, "fade_in_frames": self.fade_in_frames,
            "fade_out_frames": self.fade_out_frames, "loop": self.loop,
            "ducking": self.ducking.value, "duck_amount": self.duck_amount,
            "source_start_frame": self.source_start_frame,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AudioClip":
        try:
            ducking = DuckingIntent(data.get("ducking", "none"))
        except ValueError:
            ducking = DuckingIntent.NONE
        return cls(
            clip_id=str(data.get("clip_id", "")), asset_ref=str(data.get("asset_ref", "")),
            start_frame=int(data.get("start_frame", 0)), duration_frames=int(data.get("duration_frames", 1)),
            gain=float(data.get("gain", 1.0)),
            fade_in_frames=int(data.get("fade_in_frames", 0)),
            fade_out_frames=int(data.get("fade_out_frames", 0)),
            loop=bool(data.get("loop", False)), ducking=ducking,
            duck_amount=float(data.get("duck_amount", 0.5)),
            source_start_frame=int(data.get("source_start_frame", 0)),
        )


@dataclass(frozen=True)
class AudioRenderPlan:
    """The mix plan handed to Edit/Export; MG does not run the audio engine."""

    clips: tuple[AudioClip, ...] = ()
    sample_rate: int = 48000
    master_gain: float = 1.0

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if self.master_gain < 0.0:
            raise ValueError("master_gain must be non-negative")
        ids = [c.clip_id for c in self.clips]
        if len(ids) != len(set(ids)):
            raise ValueError("AudioClip ids must be unique")

    @property
    def end_frame(self) -> int:
        return max((c.end_frame for c in self.clips), default=0)

    def active_clips(self, frame: int) -> tuple[AudioClip, ...]:
        return tuple(c for c in self.clips if c.start_frame <= frame < c.end_frame)

    def to_timeline(
        self,
        *,
        timeline_start: int,
        scene_in_frame: int = 0,
        scene_out_frame: int | None = None,
    ) -> "AudioRenderPlan":
        """Map scene-local audio to a timeline clip without losing seek offsets."""
        if timeline_start < 0 or scene_in_frame < 0:
            raise ValueError("timeline_start and scene_in_frame must be non-negative")
        end = scene_out_frame if scene_out_frame is not None else None
        if end is not None and end <= scene_in_frame:
            raise ValueError("scene_out_frame must exceed scene_in_frame")
        mapped: list[AudioClip] = []
        for clip in self.clips:
            overlap_start = max(clip.start_frame, scene_in_frame)
            overlap_end = clip.end_frame if end is None else min(clip.end_frame, end)
            if overlap_end <= overlap_start:
                continue
            skipped = overlap_start - clip.start_frame
            duration = overlap_end - overlap_start
            # AUD-27309: each fade is individually clamped to the trimmed
            # duration, but the sum may still exceed it (e.g. 100-frame clip
            # with 50+50 fades trimmed to 60 frames).  ``dataclasses.replace``
            # re-runs ``__post_init__``, which would raise for such a perfectly
            # legal source clip — jointly constrain the pair instead.
            fade_in = max(0, min(clip.fade_in_frames - skipped, duration))
            fade_out = max(0, min(clip.fade_out_frames, duration))
            if fade_in + fade_out > duration:
                fade_in = min(fade_in, duration // 2)
                fade_out = min(fade_out, duration - fade_in)
            mapped.append(replace(
                clip,
                start_frame=timeline_start + overlap_start - scene_in_frame,
                duration_frames=duration,
                source_start_frame=clip.source_start_frame + skipped,
                fade_in_frames=fade_in,
                fade_out_frames=fade_out,
            ))
        return replace(self, clips=tuple(mapped))

    def to_dict(self) -> dict[str, Any]:
        return {
            "clips": [c.to_dict() for c in self.clips],
            "sample_rate": self.sample_rate, "master_gain": self.master_gain,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AudioRenderPlan":
        return cls(
            clips=tuple(AudioClip.from_dict(c) for c in data.get("clips", []) if isinstance(c, Mapping)),
            sample_rate=int(data.get("sample_rate", 48000)),
            master_gain=float(data.get("master_gain", 1.0)),
        )


@dataclass(frozen=True)
class AudioDiagnostic:
    """Result of preflighting an AudioRenderPlan against available assets."""

    missing_assets: tuple[str, ...] = ()
    unsupported_mix: tuple[str, ...] = ()   # clip_ids the mixer cannot handle
    out_of_range: tuple[str, ...] = ()      # clip_ids outside the scene range
    silent_fallback: bool = False           # True if output must be explicit silence

    @property
    def blocking(self) -> bool:
        return bool(self.missing_assets or self.unsupported_mix or self.out_of_range)


def preflight_audio(
    plan: AudioRenderPlan,
    available_asset_ids: frozenset[str],
    *,
    mixer_supports_ducking: bool = True,
    scene_duration_frames: int | None = None,
) -> AudioDiagnostic:
    """Preflight a plan: missing assets / unsupported ducking become diagnostics."""
    missing = tuple(c.clip_id for c in plan.clips if c.asset_ref not in available_asset_ids)
    unsupported = ()
    if not mixer_supports_ducking:
        unsupported = tuple(c.clip_id for c in plan.clips if c.ducking != DuckingIntent.NONE)
    out_of_range = ()
    if scene_duration_frames is not None:
        if scene_duration_frames <= 0:
            raise ValueError("scene_duration_frames must be positive")
        out_of_range = tuple(
            c.clip_id for c in plan.clips
            if c.start_frame < 0 or c.end_frame > scene_duration_frames
        )
    return AudioDiagnostic(
        missing_assets=missing,
        unsupported_mix=unsupported,
        out_of_range=out_of_range,
        silent_fallback=bool(plan.clips) and len(missing) == len(plan.clips),
    )


_AUDIO_PLAN_KEY = "audio_render_plan"


def read_audio_render_plan(metadata: Mapping[str, Any] | None) -> AudioRenderPlan:
    """Read a scene-owned plan from additive scene metadata."""
    if not isinstance(metadata, Mapping):
        return AudioRenderPlan()
    raw = metadata.get(_AUDIO_PLAN_KEY, {})
    return AudioRenderPlan.from_dict(raw) if isinstance(raw, Mapping) else AudioRenderPlan()


def write_audio_render_plan_into(
    metadata: Mapping[str, Any] | None,
    plan: AudioRenderPlan,
) -> dict[str, Any]:
    """Return detached metadata with the versioned plan written."""
    updated = dict(metadata or {})
    updated[_AUDIO_PLAN_KEY] = plan.to_dict()
    return updated


def audio_plan_for_scene(scene: Any) -> AudioRenderPlan:
    """Resolve the scene-owned plan without coupling hosts to metadata keys."""
    return read_audio_render_plan(getattr(scene, "metadata", None))


__all__ = [
    "AudioClip", "AudioDiagnostic", "AudioRenderPlan", "DuckingIntent", "audio_plan_for_scene",
    "preflight_audio", "read_audio_render_plan", "write_audio_render_plan_into",
]
