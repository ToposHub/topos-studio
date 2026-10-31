"""B2 visual-semantic contracts, shot aggregation and rebuildable cache.

The module deliberately stops at evidence.  Providers receive only keyframes
described by :class:`KeyframeManifest`; they cannot scan a timeline, write a
project, or turn OCR/captions into instructions.  Images and embeddings live
in an external cache and are always rebuildable from the manifest.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from src.features.edit.ai_editing.editorial_models import (
    FrameRange,
    SegmentKind,
    SemanticSegment,
)
from src.features.edit.ai_editing.keyframe_sampler import (
    FrameSamplerProtocol,
    KeyframeManifest,
    ShotSpan,
)


VISUAL_TAG = "tag"
VISUAL_CAPTION = "caption"
VISUAL_EMBEDDING = "embedding"
SUPPORTED_VISUAL_KINDS = frozenset({VISUAL_TAG, VISUAL_CAPTION, VISUAL_EMBEDDING})


@dataclass(frozen=True)
class VisualKeyframe:
    """A re-creatable keyframe passed to visual providers."""

    asset_id: str
    shot_index: int
    frame: int
    shot_range: FrameRange
    image: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.asset_id:
            raise ValueError("VisualKeyframe.asset_id is required")
        if self.shot_index < 0 or self.frame < 0:
            raise ValueError("shot_index/frame must be non-negative")
        if not self.shot_range.contains(self.frame):
            raise ValueError("keyframe must be inside its shot range")

    def to_dict(self) -> dict[str, Any]:
        """Serialize only rebuildable identity; never the image."""
        return {
            "asset_id": self.asset_id,
            "shot_index": self.shot_index,
            "frame": self.frame,
            "shot_range": self.shot_range.to_dict(),
        }


def keyframes_from_manifest(
    manifest: KeyframeManifest,
    shot_spans: Sequence[ShotSpan],
    sampler: FrameSamplerProtocol,
) -> tuple[VisualKeyframe, ...]:
    """Rebuild provider inputs from the manifest and a frame sampler."""
    by_index = {span.shot_index: span for span in shot_spans}
    if manifest.asset_id == "":
        raise ValueError("keyframe manifest asset_id is required")
    result: list[VisualKeyframe] = []
    for entry in manifest.entries:
        span = by_index.get(entry.shot_index)
        if span is None or not span.start_frame <= entry.frame < span.end_frame:
            raise ValueError(f"manifest frame {entry.frame} is outside shot {entry.shot_index}")
        result.append(VisualKeyframe(
            asset_id=manifest.asset_id,
            shot_index=entry.shot_index,
            frame=entry.frame,
            shot_range=FrameRange(span.start_frame, span.duration),
            image=sampler.sample(entry.frame),
        ))
    return tuple(result)


@dataclass(frozen=True)
class VisualObservation:
    """One provider result for one keyframe."""

    asset_id: str
    shot_index: int
    frame: int
    kind: str
    value: str = ""
    confidence: float = 1.0
    provenance: Mapping[str, Any] = field(default_factory=dict)
    field: str = ""
    embedding: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in SUPPORTED_VISUAL_KINDS:
            raise ValueError(f"unsupported visual kind: {self.kind!r}")
        if not self.asset_id or self.frame < 0 or self.shot_index < 0:
            raise ValueError("visual observation identity is invalid")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        if self.kind != VISUAL_EMBEDDING and not self.value.strip():
            raise ValueError("text visual observations require a value")
        if self.kind == VISUAL_EMBEDDING and not self.embedding:
            raise ValueError("embedding observations require a vector")

    @property
    def trust(self) -> str:
        """All model-produced visual text is untrusted agent input."""
        return "untrusted"


class VisualTagProvider(Protocol):
    provider_id: str
    model_version: str
    is_demo: bool

    def infer(self, keyframe: VisualKeyframe) -> tuple[VisualObservation, ...]: ...


class VisualCaptionProvider(Protocol):
    provider_id: str
    model_version: str
    is_demo: bool

    def infer(self, keyframe: VisualKeyframe) -> tuple[VisualObservation, ...]: ...


class EmbeddingProvider(Protocol):
    provider_id: str
    model_version: str
    is_demo: bool

    def infer(self, keyframe: VisualKeyframe) -> tuple[VisualObservation, ...]: ...


@dataclass(frozen=True)
class VisualEvidence:
    """Shot-level evidence returned to search and planners."""

    evidence_id: str
    asset_id: str
    shot_index: int
    shot_range: FrameRange
    kind: str
    value: str
    confidence: float
    source_frames: tuple[int, ...]
    provenance: Mapping[str, Any]
    trust: str = "untrusted"

    def to_segment(self) -> SemanticSegment:
        segment_kind = SegmentKind.TAG if self.kind == VISUAL_TAG else SegmentKind.VISUAL
        payload_key = "labels" if self.kind == VISUAL_TAG else "caption"
        return SemanticSegment(
            segment_id=self.evidence_id,
            asset_id=self.asset_id,
            source_range=self.shot_range,
            kind=segment_kind,
            payload={
                payload_key: [self.value] if self.kind == VISUAL_TAG else self.value,
                "source_frames": list(self.source_frames),
                "shot_index": self.shot_index,
                "trust": self.trust,
            },
            confidence=self.confidence,
            evidence_ref=self.evidence_id,
            provenance=dict(self.provenance),
        )


@dataclass(frozen=True)
class VisualConflict:
    asset_id: str
    shot_index: int
    field: str
    values: tuple[str, ...]
    source_frames: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "shot_index": self.shot_index,
            "field": self.field,
            "values": list(self.values),
            "source_frames": list(self.source_frames),
            "severity": "warning",
        }


@dataclass(frozen=True)
class ShotAggregation:
    evidence: tuple[VisualEvidence, ...]
    conflicts: tuple[VisualConflict, ...] = ()


def aggregate_shot_observations(
    results: Iterable[tuple[VisualKeyframe, Sequence[VisualObservation]]],
) -> ShotAggregation:
    """Aggregate head/mid/tail observations without letting one frame win.

    Duplicate values are merged with a confidence score that combines the
    maximum and mean confidence.  Different caption values are retained as
    separate evidence and reported as a conflict; no conclusion is silently
    overwritten by a later frame.
    """
    grouped: dict[tuple[str, int, str, str, str], list[VisualObservation]] = {}
    shot_ranges: dict[tuple[str, int], FrameRange] = {}
    for keyframe, observations in results:
        shot_key = (keyframe.asset_id, keyframe.shot_index)
        shot_ranges[shot_key] = keyframe.shot_range
        for obs in observations:
            if obs.asset_id != keyframe.asset_id or obs.shot_index != keyframe.shot_index:
                raise ValueError("provider observation does not match keyframe")
            if obs.kind == VISUAL_EMBEDDING:
                continue  # stored in the external vector cache, not as text evidence
            key = (obs.asset_id, obs.shot_index, obs.kind, obs.field or obs.kind, obs.value.strip())
            grouped.setdefault(key, []).append(obs)

    evidence: list[VisualEvidence] = []
    conflicts: list[VisualConflict] = []
    for (asset_id, shot_index, kind, field_name, value), items in sorted(grouped.items()):
        frames = tuple(sorted({item.frame for item in items}))
        confidence = (max(item.confidence for item in items) +
                      sum(item.confidence for item in items) / len(items)) / 2.0
        provenance = dict(items[0].provenance)
        provenance.update({
            "provider_frames": list(frames),
            "aggregation": "shot_max_mean",
            "model_versions": sorted({str(item.provenance.get("model_version", "")) for item in items}),
        })
        digest = hashlib.sha256(
            f"{asset_id}|{shot_index}|{kind}|{field_name}|{value}".encode("utf-8")
        ).hexdigest()[:20]
        evidence.append(VisualEvidence(
            evidence_id=f"visual-{digest}", asset_id=asset_id, shot_index=shot_index,
            shot_range=shot_ranges[(asset_id, shot_index)], kind=kind, value=value,
            confidence=min(1.0, confidence), source_frames=frames, provenance=provenance,
        ))

    by_field: dict[tuple[str, int, str, str], list[VisualEvidence]] = {}
    for item in evidence:
        if item.kind != VISUAL_CAPTION:
            continue
        key = (item.asset_id, item.shot_index, item.kind, "caption")
        by_field.setdefault(key, []).append(item)
    for (asset_id, shot_index, _kind, field_name), items in by_field.items():
        values = tuple(sorted({item.value for item in items}))
        if len(values) > 1:
            conflicts.append(VisualConflict(
                asset_id=asset_id, shot_index=shot_index, field=field_name,
                values=values,
                source_frames=tuple(sorted({f for item in items for f in item.source_frames})),
            ))
    return ShotAggregation(tuple(evidence), tuple(conflicts))


class DeterministicVisualTagProvider:
    """Small, explicitly demo-only provider for offline contract tests."""

    provider_id = "visual-tag-demo"
    model_version = "demo-v1"
    is_demo = True

    def infer(self, keyframe: VisualKeyframe) -> tuple[VisualObservation, ...]:
        label = "product" if hashlib.sha256(keyframe.asset_id.encode()).digest()[0] % 2 else "person"
        return (VisualObservation(
            asset_id=keyframe.asset_id, shot_index=keyframe.shot_index,
            frame=keyframe.frame, kind=VISUAL_TAG, value=label, confidence=0.78,
            field="tag", provenance={"provider_id": self.provider_id, "model_version": self.model_version},
        ),)


class DeterministicVisualCaptionProvider:
    provider_id = "visual-caption-demo"
    model_version = "demo-v1"
    is_demo = True

    def infer(self, keyframe: VisualKeyframe) -> tuple[VisualObservation, ...]:
        return (VisualObservation(
            asset_id=keyframe.asset_id, shot_index=keyframe.shot_index,
            frame=keyframe.frame, kind=VISUAL_CAPTION,
            value=f"frame {keyframe.frame} visual content", confidence=0.55,
            field="caption", provenance={"provider_id": self.provider_id, "model_version": self.model_version},
        ),)


class DeterministicEmbeddingProvider:
    provider_id = "visual-embedding-demo"
    model_version = "demo-v1"
    is_demo = True

    def infer(self, keyframe: VisualKeyframe) -> tuple[VisualObservation, ...]:
        digest = hashlib.sha256(
            f"{keyframe.asset_id}:{keyframe.shot_index}:{keyframe.frame}".encode()
        ).digest()
        values = tuple((byte / 127.5) - 1.0 for byte in digest[:8])
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return (VisualObservation(
            asset_id=keyframe.asset_id, shot_index=keyframe.shot_index,
            frame=keyframe.frame, kind=VISUAL_EMBEDDING,
            confidence=0.5, provenance={"provider_id": self.provider_id, "model_version": self.model_version},
            embedding=tuple(value / norm for value in values),
        ),)


class VisualCacheStore:
    """External, per-asset visual cache; project files are never touched."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def put(
        self,
        *,
        asset_id: str,
        keyframe_manifest: KeyframeManifest,
        evidence: Sequence[VisualEvidence],
        embeddings: Mapping[str, Sequence[float]] | None = None,
        fingerprint: Mapping[str, Any] | None = None,
    ) -> Path:
        safe = _safe_id(asset_id)
        self._root.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "asset_id": asset_id,
            "fingerprint": dict(fingerprint or {}),
            "keyframe_manifest": keyframe_manifest.to_dict(),
            "evidence": [
                {
                    "evidence_id": item.evidence_id,
                    "asset_id": item.asset_id,
                    "shot_index": item.shot_index,
                    "shot_range": item.shot_range.to_dict(),
                    "kind": item.kind,
                    "value": item.value,
                    "confidence": item.confidence,
                    "source_frames": list(item.source_frames),
                    "provenance": dict(item.provenance),
                    "trust": item.trust,
                }
                for item in evidence
            ],
            "embeddings": {
                str(k): [float(v) for v in values]
                for k, values in (embeddings or {}).items()
            },
        }
        target = self._root / f"{safe}.json"
        fd, temp_name = tempfile.mkstemp(prefix=f".{safe}.", suffix=".tmp", dir=self._root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.remove(temp_name)
        return target

    def load(self, asset_id: str) -> dict[str, Any] | None:
        path = self._root / f"{_safe_id(asset_id)}.json"
        if not path.is_file():
            return None
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict) or value.get("asset_id") != asset_id:
            return None
        return value

    def delete_asset(self, asset_id: str) -> bool:
        path = self._root / f"{_safe_id(asset_id)}.json"
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    def clear(self) -> None:
        for path in self._root.glob("*.json"):
            path.unlink(missing_ok=True)


def visual_context(evidence: Iterable[VisualEvidence]) -> tuple[dict[str, Any], ...]:
    """Wrap visual text as data; callers must not treat it as instructions."""
    return tuple({
        "type": "untrusted_visual_evidence",
        "asset_id": item.asset_id,
        "shot_index": item.shot_index,
        "source_frames": list(item.source_frames),
        "kind": item.kind,
        "text": item.value,
        "confidence": item.confidence,
        "provenance": dict(item.provenance),
    } for item in evidence)


def _safe_id(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value)
    return safe[:120] or "asset"


__all__ = [
    "EmbeddingProvider", "KeyframeManifest", "ShotAggregation", "VisualCacheStore",
    "VisualCaptionProvider", "VisualConflict", "VisualEvidence", "VisualKeyframe",
    "VisualObservation", "VisualTagProvider", "VISUAL_CAPTION", "VISUAL_EMBEDDING",
    "VISUAL_TAG", "DeterministicEmbeddingProvider", "DeterministicVisualCaptionProvider",
    "DeterministicVisualTagProvider", "aggregate_shot_observations", "keyframes_from_manifest",
    "visual_context",
]
