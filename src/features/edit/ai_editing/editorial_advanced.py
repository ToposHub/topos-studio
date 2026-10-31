"""Video-editing agent remaining gaps (VE-02/05/06/07/09/10/11/13).

Fills the remaining VE-domain gaps the audit found:

* VE-02: transcript revision history + silence/filler/chapter annotations
* VE-05: multicam sync evidence + active-angle plan
* VE-06: beat grid + audio-driven edit planning
* VE-07: B-roll search candidates with license + brand checks
* VE-09: effect eligibility + risk catalog
* VE-10: AI/human transaction conflict + rebase suggestion
* VE-11: master → linked version derivation
* VE-13: long-form layered analysis strategy
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


# --------------------------------------------------------------------------- #
# VE-02: Transcript revision + annotations
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TranscriptRevision:
    """Versioned, editable transcript with provenance (VE-02)."""

    revision_id: str
    asset_id: str
    revision: int
    word_count: int
    speaker_count: int
    language: str = "zh"
    silence_segments: tuple[Mapping[str, Any], ...] = ()   # {"start": float, "end": float}
    filler_words: tuple[str, ...] = ()
    chapters: tuple[Mapping[str, Any], ...] = ()            # {"title": str, "start": float, "end": float}
    speech_rate_wpm: float | None = None
    user_verified: bool = False
    source_provider: str | None = None
    source_model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "asset_id": self.asset_id,
            "revision": self.revision,
            "word_count": self.word_count,
            "speaker_count": self.speaker_count,
            "language": self.language,
            "silence_segments": [dict(s) for s in self.silence_segments],
            "filler_words": list(self.filler_words),
            "chapters": [dict(c) for c in self.chapters],
            "speech_rate_wpm": self.speech_rate_wpm,
            "user_verified": self.user_verified,
            "source_provider": self.source_provider,
            "source_model": self.source_model,
        }


# --------------------------------------------------------------------------- #
# VE-05: Multicam sync evidence + active-angle plan
# --------------------------------------------------------------------------- #


SYNC_TIMECODE = "timecode"
SYNC_WAVEFORM = "waveform"
SYNC_FACE = "face"
SYNC_SLATE = "slate"

SUPPORTED_SYNC_METHODS: frozenset[str] = frozenset({
    SYNC_TIMECODE, SYNC_WAVEFORM, SYNC_FACE, SYNC_SLATE,
})


@dataclass(frozen=True)
class MulticamSyncEvidence:
    """Evidence that a group of angles are synchronized (VE-05)."""

    group_id: str
    angle_clip_ids: tuple[str, ...]
    sync_method: str
    confidence: float
    offset_map: Mapping[str, float] = field(default_factory=dict)  # clip_id → offset seconds

    def __post_init__(self) -> None:
        if self.sync_method not in SUPPORTED_SYNC_METHODS:
            raise ValueError(f"Unknown sync method: {self.sync_method!r}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")


@dataclass(frozen=True)
class ActiveAnglePlan:
    """A proposed active-angle switching sequence for a multicam group (VE-05)."""

    group_id: str
    cuts: tuple[Mapping[str, Any], ...]    # {"frame": int, "angle_clip_id": str, "reason": str}
    j_cut_frames: int = 0                  # audio leads video by N frames
    l_cut_frames: int = 0
    confidence: float = 0.8

    @property
    def needs_confirmation(self) -> bool:
        return self.confidence < 0.75


# --------------------------------------------------------------------------- #
# VE-06: Beat grid + audio-driven planning
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BeatGrid:
    """Detected music beats for rhythm-driven editing (VE-06)."""

    asset_id: str
    bpm: float
    beat_frames: tuple[int, ...]
    downbeat_frames: tuple[int, ...] = ()
    confidence: float = 0.8

    @property
    def beat_count(self) -> int:
        return len(self.beat_frames)

    def beats_in_range(self, start_frame: int, end_frame: int) -> tuple[int, ...]:
        return tuple(f for f in self.beat_frames if start_frame <= f < end_frame)


@dataclass(frozen=True)
class AudioDrivenEditPlan:
    """Proposed cuts aligned to beats / speech / silence (VE-06)."""

    beat_grid_id: str | None
    speech_rhythm_alignment: bool
    proposed_cuts: tuple[Mapping[str, Any], ...] = ()  # {"frame": int, "reason": str, "confidence": float}
    ducking_rules: tuple[Mapping[str, Any], ...] = ()
    target_loudness_lufs: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "beat_grid_id": self.beat_grid_id,
            "speech_rhythm_alignment": self.speech_rhythm_alignment,
            "proposed_cuts": [dict(c) for c in self.proposed_cuts],
            "ducking_rules": [dict(r) for r in self.ducking_rules],
            "target_loudness_lufs": self.target_loudness_lufs,
        }


# --------------------------------------------------------------------------- #
# VE-07: B-roll search candidates
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BRollCandidate:
    """One B-roll suggestion with provenance + license + brand checks (VE-07)."""

    asset_id: str
    source_clip_id: str | None             # from current project, or None = external library
    in_frame: int
    out_frame: int
    reason: str
    confidence: float
    license_state: str = "unknown"         # ok / missing / restricted / unknown
    brand_compliant: bool = True
    is_suggestion: bool = True             # False = auto-insert (only when high confidence + licensed)

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")

    @property
    def may_auto_insert(self) -> bool:
        return (
            self.confidence >= 0.85
            and self.license_state == "ok"
            and self.brand_compliant
            and not self.is_suggestion
        )


# --------------------------------------------------------------------------- #
# VE-09: Effect eligibility + risk catalog
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EffectEligibility:
    """Whether a given effect is appropriate for a given clip (VE-09)."""

    effect_id: str
    clip_id: str
    eligible: bool
    reason: str = ""
    parameter_ranges: Mapping[str, tuple[float, float]] = field(default_factory=dict)
    preview_supported: bool = True
    export_supported: bool = True
    render_cost_factor: float = 1.0         # 1.0 = baseline; >1 = more expensive
    quality_risk: str = "low"               # low / medium / high
    reversible: bool = True


@dataclass(frozen=True)
class EffectCatalogEntry:
    """One effect in the AI-facing effect catalog (VE-09)."""

    effect_id: str
    label: str
    category: str                           # transition / speed / stabilize / denoise / reframe / style
    description: str = ""
    eligibility_checks: tuple[str, ...] = ()  # function names that assess eligibility
    default_parameter_ranges: Mapping[str, tuple[float, float]] = field(default_factory=dict)
    preview_backends: tuple[str, ...] = ("cpu",)
    export_backends: tuple[str, ...] = ("cpu", "gpu")
    render_cost_factor: float = 1.0
    quality_risk: str = "low"
    reversible: bool = True


# --------------------------------------------------------------------------- #
# VE-10: AI/human transaction conflict resolution
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TransactionConflict:
    """One conflict between an AI plan and a human edit (VE-10)."""

    conflict_id: str
    domain: str
    clip_id: str
    ai_plan_ref: str
    human_change: str                       # what the user did
    conflict_type: str                      # overlap / overwrite / delete_target / param_mismatch
    severity: str = "warning"

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflict_id": self.conflict_id,
            "domain": self.domain,
            "clip_id": self.clip_id,
            "ai_plan_ref": self.ai_plan_ref,
            "human_change": self.human_change,
            "conflict_type": self.conflict_type,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class RebaseSuggestion:
    """Suggested partial replan after a human edit (VE-10)."""

    stale_plan_id: str
    affected_clip_ids: tuple[str, ...]
    recompute_domains: tuple[str, ...]
    preserve_clip_ids: tuple[str, ...]      # human-locked clips to keep
    suggested_action: str = "replan_affected"  # replan_affected / full_replan / accept_conflict


def detect_conflicts(
    ai_target_clip_ids: Iterable[str],
    human_changed_clip_ids: Iterable[str],
    *,
    domain: str = "editorial",
) -> tuple[TransactionConflict, ...]:
    """Detect where an AI plan and a human edit touch the same clips (VE-10)."""
    ai_set = set(ai_target_clip_ids)
    conflicts: list[TransactionConflict] = []
    for clip_id in human_changed_clip_ids:
        if clip_id in ai_set:
            conflicts.append(TransactionConflict(
                conflict_id=f"conflict-{clip_id}",
                domain=domain,
                clip_id=clip_id,
                ai_plan_ref="",
                human_change="edited",
                conflict_type="overwrite",
            ))
    return tuple(conflicts)


# --------------------------------------------------------------------------- #
# VE-11: Master → linked version derivation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LinkedVersion:
    """One derived version from a master editorial plan (VE-11)."""

    version_id: str
    master_plan_id: str
    canvas_aspect: str
    duration_target_seconds: float
    language: str = "zh"
    reframe_strategy: str = "center_crop"   # center_crop / tracking / auto_reframe
    subtitle_strategy: str = "burn_in"      # burn_in / soft / none
    cta_override: str | None = None
    derivation_notes: str = ""


@dataclass(frozen=True)
class VersionDerivationPlan:
    """Plan to produce multiple platform versions from one master (VE-11)."""

    master_plan_id: str
    versions: tuple[LinkedVersion, ...]

    @property
    def version_count(self) -> int:
        return len(self.versions)

    def aspects_covered(self) -> tuple[str, ...]:
        seen: list[str] = []
        for v in self.versions:
            if v.canvas_aspect not in seen:
                seen.append(v.canvas_aspect)
        return tuple(seen)


# --------------------------------------------------------------------------- #
# VE-13: Long-form layered analysis strategy
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HierarchicalSummary:
    """One level of a multi-level asset summary for long-form content (VE-13)."""

    level: str                # project / chapter / scene / shot
    label: str
    start_seconds: float
    duration_seconds: float
    key_points: tuple[str, ...] = ()
    asset_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class LongFormAnalysisStrategy:
    """Layered analysis + retrieval plan for long-form content (VE-13)."""

    asset_id: str
    total_duration_seconds: float
    summaries: tuple[HierarchicalSummary, ...] = ()
    proxy_first: bool = True
    cache_ttl_seconds: float = 3600.0
    eviction_policy: str = "lru"            # lru / ttl / budget

    @property
    def hierarchy_depth(self) -> int:
        return len({s.level for s in self.summaries})

    def summaries_at(self, level: str) -> tuple[HierarchicalSummary, ...]:
        return tuple(s for s in self.summaries if s.level == level)


__all__ = [
    # VE-02
    "TranscriptRevision",
    # VE-05
    "MulticamSyncEvidence",
    "ActiveAnglePlan",
    "SUPPORTED_SYNC_METHODS",
    "SYNC_TIMECODE",
    "SYNC_WAVEFORM",
    "SYNC_FACE",
    "SYNC_SLATE",
    # VE-06
    "BeatGrid",
    "AudioDrivenEditPlan",
    # VE-07
    "BRollCandidate",
    # VE-09
    "EffectEligibility",
    "EffectCatalogEntry",
    # VE-10
    "TransactionConflict",
    "RebaseSuggestion",
    "detect_conflicts",
    # VE-11
    "LinkedVersion",
    "VersionDerivationPlan",
    # VE-13
    "HierarchicalSummary",
    "LongFormAnalysisStrategy",
]
