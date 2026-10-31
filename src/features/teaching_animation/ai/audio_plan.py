# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""Audio planning for Motion Graphic clips (MG-15).

MG-15 models the audio side of a clip — narration, music, SFX, ducking and
beat markers — as a typed :class:`AudioPlan` that the planner can attach to a
proposal.  Audio is optional but, when present, must declare its source,
duration and mix strategy so the export quality gate can refuse a silent or
over-loud export.

The plan stays Qt-free and stores only ids + opaque refs; the actual TTS /
music generation happens through the provider adapters wired up in later
batches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping

from .asset_provenance import (
    ALL_LICENSE_STATES,
    LICENSE_MISSING,
    LICENSE_RESTRICTED,
)


# --------------------------------------------------------------------------- #
# Audio kinds
# --------------------------------------------------------------------------- #


KIND_NARRATION = "narration"
KIND_MUSIC = "music"
KIND_SFX = "sfx"
KIND_AMBIENCE = "ambience"

SUPPORTED_AUDIO_KINDS: frozenset[str] = frozenset({
    KIND_NARRATION, KIND_MUSIC, KIND_SFX, KIND_AMBIENCE,
})


# License states that block an export.  Kept in sync with the asset-side
# quality gate (quality_gate.py blocks {"missing", "restricted"}) so the audio
# plan cannot pass tracks the asset pipeline would refuse (MG-18 alignment).
BLOCKING_LICENSE_STATES: frozenset[str] = frozenset({
    LICENSE_MISSING, LICENSE_RESTRICTED,
})


# --------------------------------------------------------------------------- #
# Tracks
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AudioTrack:
    """One audio layer in the plan."""

    track_id: str
    kind: str
    label: str
    asset_id: str | None = None
    start_seconds: float = 0.0
    duration_seconds: float | None = None
    gain_db: float = 0.0
    ducking_target: str | None = None    # track_id this track ducks under
    ducking_depth_db: float = 0.0
    license_state: str = "unknown"
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.track_id:
            raise ValueError("AudioTrack.track_id is required")
        if self.kind not in SUPPORTED_AUDIO_KINDS:
            raise ValueError(f"Unsupported audio kind: {self.kind!r}")
        if self.duration_seconds is not None and self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        if self.start_seconds < 0:
            raise ValueError("start_seconds cannot be negative")
        if self.license_state not in ALL_LICENSE_STATES:
            raise ValueError(
                f"Unknown license_state: {self.license_state!r}; "
                f"expected one of {sorted(ALL_LICENSE_STATES)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "kind": self.kind,
            "label": self.label,
            "asset_id": self.asset_id,
            "start_seconds": self.start_seconds,
            "duration_seconds": self.duration_seconds,
            "gain_db": self.gain_db,
            "ducking_target": self.ducking_target,
            "ducking_depth_db": self.ducking_depth_db,
            "license_state": self.license_state,
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True)
class BeatMarker:
    """A music beat marker used to sync visual cuts."""

    frame: int
    strength: float = 1.0
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame": self.frame,
            "strength": self.strength,
            "label": self.label,
        }


@dataclass(frozen=True)
class SubtitleMapping:
    """Maps a subtitle cue to a narration segment + frame range."""

    subtitle_id: str
    narration_track_id: str | None
    start_frame: int
    end_frame: int
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "subtitle_id": self.subtitle_id,
            "narration_track_id": self.narration_track_id,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "text": self.text,
        }


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AudioPlan:
    """The full audio layer of a Motion Graphic clip."""

    tracks: tuple[AudioTrack, ...] = ()
    beat_markers: tuple[BeatMarker, ...] = ()
    subtitle_mappings: tuple[SubtitleMapping, ...] = ()
    target_loudness_lufs: float | None = None
    narration_text: str = ""
    notes: str = ""

    @property
    def has_audio(self) -> bool:
        return bool(self.tracks)

    @property
    def duration_seconds(self) -> float:
        end = 0.0
        for track in self.tracks:
            duration = track.duration_seconds or 0.0
            end = max(end, track.start_seconds + duration)
        return end

    def narration_track(self) -> AudioTrack | None:
        return next((t for t in self.tracks if t.kind == KIND_NARRATION), None)

    def music_track(self) -> AudioTrack | None:
        return next((t for t in self.tracks if t.kind == KIND_MUSIC), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tracks": [track.to_dict() for track in self.tracks],
            "beat_markers": [marker.to_dict() for marker in self.beat_markers],
            "subtitle_mappings": [mapping.to_dict() for mapping in self.subtitle_mappings],
            "target_loudness_lufs": self.target_loudness_lufs,
            "narration_text": self.narration_text,
            "notes": self.notes,
        }

    def plan_hash(self) -> str:
        payload = self.to_dict()
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def issues(self, *, clip_duration_seconds: float) -> tuple[str, ...]:
        """Return any audio issues that would block export (MG-18 link)."""
        issues: list[str] = []
        for track in self.tracks:
            end = track.start_seconds + (track.duration_seconds or 0.0)
            if end > clip_duration_seconds + 0.1:
                issues.append(
                    f"音频轨 {track.track_id} 延伸超过片段时长 ({end:.2f}s > {clip_duration_seconds:.2f}s)"
                )
            if track.license_state in BLOCKING_LICENSE_STATES:
                issues.append(
                    f"音频轨 {track.track_id} 许可状态为 {track.license_state}，不可用于导出"
                )
        if self.target_loudness_lufs is not None and abs(self.target_loudness_lufs) > 30:
            issues.append("目标响度异常")
        return tuple(issues)


__all__ = [
    "AudioPlan",
    "AudioTrack",
    "BeatMarker",
    "SubtitleMapping",
    "SUPPORTED_AUDIO_KINDS",
    "BLOCKING_LICENSE_STATES",
    "KIND_NARRATION",
    "KIND_MUSIC",
    "KIND_SFX",
    "KIND_AMBIENCE",
]
