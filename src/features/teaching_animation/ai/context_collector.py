"""Project context collection for Motion Graphic planning (MG-12).

MG-12 stops the planner from inventing content in a vacuum.  Before the LLM
is asked for creative proposals, the host collects everything the plan should
respect: the current project / timeline revision, the selected assets, the
analysis evidence already on disk, the brand kit, existing subtitles / audio
tracks and the delivery profile.  The collector emits a
:class:`ProjectContextSnapshot` that the planner injects into the LLM prompt
*as evidence*, and a :meth:`ProjectContextSnapshot.fingerprint` that goes
into the approval scope so a stale context invalidates old plans.

Design rules:

* the snapshot is Qt-free and serializes to a JSON-safe dict (no secret /
  path / prompt); raw media bytes are never embedded — only ids, fingerprints
  and short labels;
* ``fingerprint`` is a sha256 over the *semantic* content (project id,
  timeline revision, asset fingerprints, brand hash, profile id) so two
  snapshots compare equal when nothing relevant changed;
* when the project / timeline / selection moves, the host builds a new
  snapshot and the approval layer's stale detection catches the drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Iterable, Mapping


# --------------------------------------------------------------------------- #
# Evidence entries
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AssetEvidence:
    """One piece of media the planner is allowed to see / use.

    ``fingerprint`` is a stable hash of the asset's identity (path or id +
    size + mtime rounded to the second) so context drift is detectable
    without embedding the bytes.  ``license_status`` is ``unknown`` until the
    asset provenance layer (MG-14) confirms it.
    """

    asset_id: str
    label: str
    kind: str                         # image / video / audio / font
    fingerprint: str
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    license_status: str = "unknown"   # ok / missing / unknown / restricted
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "label": self.label,
            "kind": self.kind,
            "fingerprint": self.fingerprint,
            "duration_seconds": self.duration_seconds,
            "width": self.width,
            "height": self.height,
            "license_status": self.license_status,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class TimelineContext:
    """What the planner knows about the target timeline."""

    timeline_id: str
    revision: int
    track_id: str | None
    track_label: str | None
    insert_frame: int
    available_tracks: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeline_id": self.timeline_id,
            "revision": self.revision,
            "track_id": self.track_id,
            "track_label": self.track_label,
            "insert_frame": self.insert_frame,
            "available_tracks": [dict(track) for track in self.available_tracks],
        }


@dataclass(frozen=True)
class BrandContext:
    """Brand tokens the planner should respect (MG-13 link)."""

    brand_id: str
    label: str
    palette: Mapping[str, str] = field(default_factory=dict)
    typography: Mapping[str, Any] = field(default_factory=dict)
    logo_policy: Mapping[str, Any] = field(default_factory=dict)
    forbidden: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "brand_id": self.brand_id,
            "label": self.label,
            "palette": dict(self.palette),
            "typography": dict(self.typography),
            "logo_policy": dict(self.logo_policy),
            "forbidden": list(self.forbidden),
        }

    def hash(self) -> str:
        payload = {
            "brand_id": self.brand_id,
            "palette": dict(self.palette),
            "typography": dict(self.typography),
            "logo_policy": dict(self.logo_policy),
            "forbidden": list(self.forbidden),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AnalysisEvidence:
    """Reference to existing analysis (ASR / shot / OCR) without raw payload."""

    evidence_id: str
    kind: str                         # asr / shot / ocr / audio / color
    source_asset_id: str
    model: str | None = None
    confidence: float | None = None
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "source_asset_id": self.source_asset_id,
            "model": self.model,
            "confidence": self.confidence,
            "summary": self.summary,
        }


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProjectContextSnapshot:
    """All evidence the planner is allowed to see in one immutable object."""

    project_id: str
    project_revision: int
    timeline: TimelineContext | None
    delivery_profile_id: str | None
    selected_assets: tuple[AssetEvidence, ...] = ()
    brand: BrandContext | None = None
    analysis: tuple[AnalysisEvidence, ...] = ()
    subtitle_tracks: tuple[Mapping[str, Any], ...] = ()
    audio_tracks: tuple[Mapping[str, Any], ...] = ()
    collected_at: float = 0.0
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_revision": self.project_revision,
            "timeline": self.timeline.to_dict() if self.timeline else None,
            "delivery_profile_id": self.delivery_profile_id,
            "selected_assets": [asset.to_dict() for asset in self.selected_assets],
            "brand": self.brand.to_dict() if self.brand else None,
            "analysis": [item.to_dict() for item in self.analysis],
            "subtitle_tracks": [dict(track) for track in self.subtitle_tracks],
            "audio_tracks": [dict(track) for track in self.audio_tracks],
            "collected_at": self.collected_at,
            "notes": self.notes,
        }

    def fingerprint(self) -> str:
        """Stable sha256 of the semantic content.

        The fingerprint excludes ``collected_at`` and ``notes`` so a
        re-collection at a different wall clock with identical content
        compares equal — that's what makes "did the context drift?" a
        reliable signal for the approval scope.
        """

        payload = {
            "project_id": self.project_id,
            "project_revision": self.project_revision,
            "timeline_id": self.timeline.timeline_id if self.timeline else None,
            "timeline_revision": self.timeline.revision if self.timeline else None,
            "track_id": self.timeline.track_id if self.timeline else None,
            "insert_frame": self.timeline.insert_frame if self.timeline else None,
            "delivery_profile_id": self.delivery_profile_id,
            "selected_assets": [asset.fingerprint for asset in self.selected_assets],
            "brand_hash": self.brand.hash() if self.brand else None,
            "analysis": [item.evidence_id for item in self.analysis],
            "subtitle_tracks": [dict(track) for track in self.subtitle_tracks],
            "audio_tracks": [dict(track) for track in self.audio_tracks],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Collector
# --------------------------------------------------------------------------- #


class ProjectContextCollector:
    """Build :class:`ProjectContextSnapshot` objects from host data.

    The collector is a thin pure-Python adapter; the host (the teaching
    animation module) supplies the raw dicts from the timeline / project /
    brand subsystems and the collector normalizes them into the typed
    snapshot.  Keeping the conversion in one place means every planner entry
    point (Assistant / Skill / Workflow) sees the same context shape.
    """

    def __init__(self, *, time_fn: Any = None) -> None:
        self._time_fn = time_fn or _default_time

    def collect(
        self,
        *,
        project_id: str,
        project_revision: int,
        timeline: Mapping[str, Any] | None,
        delivery_profile_id: str | None,
        selected_assets: Iterable[Mapping[str, Any]] = (),
        brand: Mapping[str, Any] | None = None,
        analysis: Iterable[Mapping[str, Any]] = (),
        subtitle_tracks: Iterable[Mapping[str, Any]] = (),
        audio_tracks: Iterable[Mapping[str, Any]] = (),
        notes: str = "",
    ) -> ProjectContextSnapshot:
        timeline_ctx = self._build_timeline(timeline) if timeline else None
        brand_ctx = self._build_brand(brand) if brand else None
        return ProjectContextSnapshot(
            project_id=project_id,
            project_revision=int(project_revision),
            timeline=timeline_ctx,
            delivery_profile_id=delivery_profile_id,
            selected_assets=tuple(self._build_asset(item) for item in selected_assets),
            brand=brand_ctx,
            analysis=tuple(self._build_analysis(item) for item in analysis),
            subtitle_tracks=tuple(dict(item) for item in subtitle_tracks),
            audio_tracks=tuple(dict(item) for item in audio_tracks),
            collected_at=self._time_fn(),
            notes=notes,
        )

    # ---------------- builders ---------------- #

    def _build_timeline(self, data: Mapping[str, Any]) -> TimelineContext:
        available = data.get("available_tracks", [])
        return TimelineContext(
            timeline_id=str(data.get("timeline_id", "")),
            revision=int(data.get("revision", 0)),
            track_id=data.get("track_id"),
            track_label=data.get("track_label"),
            insert_frame=int(data.get("insert_frame", 0)),
            available_tracks=tuple(dict(track) for track in available),
        )

    def _build_asset(self, data: Mapping[str, Any]) -> AssetEvidence:
        return AssetEvidence(
            asset_id=str(data.get("asset_id", "")),
            label=str(data.get("label", "")),
            kind=str(data.get("kind", "image")),
            fingerprint=str(data.get("fingerprint", "")),
            duration_seconds=data.get("duration_seconds"),
            width=data.get("width"),
            height=data.get("height"),
            license_status=str(data.get("license_status", "unknown")),
            notes=str(data.get("notes", "")),
        )

    def _build_brand(self, data: Mapping[str, Any]) -> BrandContext:
        forbidden = data.get("forbidden", [])
        return BrandContext(
            brand_id=str(data.get("brand_id", "")),
            label=str(data.get("label", "")),
            palette=dict(data.get("palette", {})),
            typography=dict(data.get("typography", {})),
            logo_policy=dict(data.get("logo_policy", {})),
            forbidden=tuple(str(item) for item in forbidden) if isinstance(forbidden, (list, tuple)) else (),
        )

    def _build_analysis(self, data: Mapping[str, Any]) -> AnalysisEvidence:
        return AnalysisEvidence(
            evidence_id=str(data.get("evidence_id", "")),
            kind=str(data.get("kind", "")),
            source_asset_id=str(data.get("source_asset_id", "")),
            model=data.get("model"),
            confidence=data.get("confidence"),
            summary=str(data.get("summary", "")),
        )


def _default_time() -> float:
    import time
    return time.time()


__all__ = [
    "AnalysisEvidence",
    "AssetEvidence",
    "BrandContext",
    "ProjectContextCollector",
    "ProjectContextSnapshot",
    "TimelineContext",
]
