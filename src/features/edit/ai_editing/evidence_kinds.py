"""Extended evidence kinds + domain-aware project state snapshot (OS-04 / OS-05).

OS-05 asks the evidence store to carry more than transcript/shot/ocr/tag/audio.
The audit found that ``SegmentKind`` already has FACE/VISUAL/CHARACTER, but is
still missing COLOR / COMPOSITE_QUALITY / QUALITY / BRAND / BEAT / VOICE.

This module defines the **extended evidence vocabulary** as a standalone
frozenset (so it composes with the existing enum rather than monkey-patching
it) plus an :class:`EvidenceRecord` wrapper that carries the OS-05 metadata
(fingerprint / model version / confidence / expiry) uniformly.

OS-04 asks the project snapshot to cover effect / marker / selection / color
nodes / composite graph / delivery profile with per-domain fingerprints.  This
module defines :class:`DomainSnapshot` + :class:`ExtendedProjectSnapshot` as
an additive layer the host can produce alongside the existing
``snapshot_service.ProjectSnapshot``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import time
from typing import Any, Mapping


# --------------------------------------------------------------------------- #
# Extended evidence vocabulary (OS-05)
# --------------------------------------------------------------------------- #
#
# These compose with the existing ``SegmentKind`` enum.  The host registers
# analysis providers that emit these kinds; the semantic index and evidence
# policy pick them up.

# Existing kinds (mirrored for completeness):
EVIDENCE_TRANSCRIPT = "transcript"
EVIDENCE_SHOT = "shot"
EVIDENCE_TAG = "tag"
EVIDENCE_OCR = "ocr"
EVIDENCE_AUDIO = "audio"
EVIDENCE_FACE = "face"
EVIDENCE_VISUAL = "visual"
EVIDENCE_CHARACTER = "character"

# New kinds (OS-05 gap):
EVIDENCE_BEAT = "beat"                       # music beat markers (VE-06)
EVIDENCE_VOICE = "voice"                     # voice / speech detection vs non-speech
EVIDENCE_LOUDNESS = "loudness"               # per-segment loudness measurement
EVIDENCE_COLOR = "color"                     # shot-level color statistics (CG-02)
EVIDENCE_COMPOSITE_QUALITY = "composite_quality"  # alpha / key / spill quality (CP-02)
EVIDENCE_QUALITY = "quality"                  # technical quality (sharpness / noise / stability)
EVIDENCE_BRAND = "brand"                     # brand element detection (logo / product / brand color)

EXTENDED_EVIDENCE_KINDS: frozenset[str] = frozenset({
    EVIDENCE_TRANSCRIPT, EVIDENCE_SHOT, EVIDENCE_TAG, EVIDENCE_OCR, EVIDENCE_AUDIO,
    EVIDENCE_FACE, EVIDENCE_VISUAL, EVIDENCE_CHARACTER,
    EVIDENCE_BEAT, EVIDENCE_VOICE, EVIDENCE_LOUDNESS,
    EVIDENCE_COLOR, EVIDENCE_COMPOSITE_QUALITY, EVIDENCE_QUALITY, EVIDENCE_BRAND,
})

#: Evidence kinds newly added in OS-05 (not in the original SegmentKind).
NEW_EVIDENCE_KINDS: frozenset[str] = frozenset({
    EVIDENCE_BEAT, EVIDENCE_VOICE, EVIDENCE_LOUDNESS,
    EVIDENCE_COLOR, EVIDENCE_COMPOSITE_QUALITY, EVIDENCE_QUALITY, EVIDENCE_BRAND,
})


# --------------------------------------------------------------------------- #
# Evidence record (OS-05 immutable wrapper)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EvidenceRecord:
    """One immutable evidence artifact with OS-05 metadata.

    Wraps any evidence (transcript segment, shot cut, color measurement, …)
    with the common provenance + validity fields the policy and planner need.
    """

    evidence_id: str
    kind: str
    asset_id: str
    model_id: str | None = None
    model_version: str | None = None
    confidence: float | None = None
    source_fingerprint: str | None = None      # fingerprint of the asset at analysis time
    created_at: float = 0.0
    expires_at: float | None = None            # None = no explicit expiry
    payload: Mapping[str, Any] = field(default_factory=dict)
    summary: str = ""

    def __post_init__(self) -> None:
        if not self.evidence_id:
            raise ValueError("EvidenceRecord.evidence_id is required")
        if self.kind not in EXTENDED_EVIDENCE_KINDS:
            raise ValueError(
                f"Unknown evidence kind: {self.kind!r}; "
                f"expected one of {sorted(EXTENDED_EVIDENCE_KINDS)}"
            )
        if not self.asset_id:
            raise ValueError("EvidenceRecord.asset_id is required")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")

    def is_expired(self, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        current = now if now is not None else time.time()
        return current >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "asset_id": self.asset_id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "confidence": self.confidence,
            "source_fingerprint": self.source_fingerprint,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "payload": dict(self.payload),
            "summary": self.summary,
        }


# --------------------------------------------------------------------------- #
# Domain snapshot (OS-04)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DomainSnapshot:
    """One domain's contribution to the extended project snapshot.

    Each domain (editorial / color / composite / delivery) produces a summary
    with a revision + fingerprint so cross-domain stale detection works.
    """

    domain: str
    revision: int
    fingerprint: str
    summary: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "revision": self.revision,
            "fingerprint": self.fingerprint,
            "summary": dict(self.summary),
        }


@dataclass(frozen=True)
class ExtendedProjectSnapshot:
    """Additive snapshot covering effect / marker / selection / color / comp / delivery.

    Composes with the existing ``ProjectSnapshot`` (project / timeline / tracks
    / clips / assets).  The host builds both: the base snapshot for the common
    fields, this one for the domain-specific fields the audit flagged as
    missing.
    """

    project_id: str
    timeline_id: str | None
    base_revision: int
    domains: tuple[DomainSnapshot, ...] = ()
    selection: Mapping[str, Any] = field(default_factory=dict)
    markers: tuple[Mapping[str, Any], ...] = ()
    effects_summary: tuple[Mapping[str, Any], ...] = ()
    delivery_profile_id: str | None = None
    created_at: float = field(default_factory=time.time)

    def domain(self, name: str) -> DomainSnapshot | None:
        return next((d for d in self.domains if d.domain == name), None)

    def fingerprint(self) -> str:
        """Stable hash over all domain fingerprints + revision + selection."""
        payload = {
            "project_id": self.project_id,
            "timeline_id": self.timeline_id,
            "base_revision": self.base_revision,
            "domains": [
                {"domain": d.domain, "revision": d.revision, "fingerprint": d.fingerprint}
                for d in self.domains
            ],
            # AUD-13508：selection 必须参与指纹，否则选择状态变化不会被 stale
            # 检测捕获（docstring 一直声明包含 selection）。
            "selection": dict(self.selection),
            "delivery_profile_id": self.delivery_profile_id,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def stale_domains(self, other: "ExtendedProjectSnapshot") -> tuple[str, ...]:
        """Return domain names whose fingerprint changed vs ``other``."""
        mine = {d.domain: d.fingerprint for d in self.domains}
        theirs = {d.domain: d.fingerprint for d in other.domains}
        return tuple(
            name for name in mine
            if name in theirs and mine[name] != theirs[name]
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "timeline_id": self.timeline_id,
            "base_revision": self.base_revision,
            "domains": [d.to_dict() for d in self.domains],
            "selection": dict(self.selection),
            "markers": [dict(m) for m in self.markers],
            "effects_summary": [dict(e) for e in self.effects_summary],
            "delivery_profile_id": self.delivery_profile_id,
            "created_at": self.created_at,
            "fingerprint": self.fingerprint(),
        }


__all__ = [
    "EvidenceRecord",
    "DomainSnapshot",
    "ExtendedProjectSnapshot",
    "EXTENDED_EVIDENCE_KINDS",
    "NEW_EVIDENCE_KINDS",
    "EVIDENCE_TRANSCRIPT",
    "EVIDENCE_SHOT",
    "EVIDENCE_TAG",
    "EVIDENCE_OCR",
    "EVIDENCE_AUDIO",
    "EVIDENCE_FACE",
    "EVIDENCE_VISUAL",
    "EVIDENCE_CHARACTER",
    "EVIDENCE_BEAT",
    "EVIDENCE_VOICE",
    "EVIDENCE_LOUDNESS",
    "EVIDENCE_COLOR",
    "EVIDENCE_COMPOSITE_QUALITY",
    "EVIDENCE_QUALITY",
    "EVIDENCE_BRAND",
]
