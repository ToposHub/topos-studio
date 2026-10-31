"""Auditable cross-layer render manifest and frame consistency helpers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping, Sequence

from ..core.contracts import RenderContext, RenderEvidence, RenderedFrame
from ..core.model import TeachingSceneDocument
from .cache import build_cache_manifest
from .consistency import ComparisonMode, ConsistencyResult, FrameSnapshot, compare_frames


@dataclass(frozen=True)
class ReleaseManifest:
    """Provenance for one backend/profile across a fixed set of frames."""

    schema_version: int
    scene_id: str
    scene_hash: str
    backend: str
    renderer_ids: tuple[str, ...]
    renderer_build: str
    render_profile: str
    width: int
    height: int
    dpr: float
    alpha_mode: str
    working_color_space: str
    bit_depth: int
    font_set_hash: str
    asset_hashes: tuple[tuple[str, str], ...]
    declared_asset_ids: tuple[str, ...]
    capability_fallback: tuple[str, ...]
    frame_evidence: tuple[Mapping[str, Any], ...]

    @property
    def manifest_hash(self) -> str:
        payload = json.dumps(self.to_dict(include_hash=False), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "scene_id": self.scene_id,
            "scene_hash": self.scene_hash,
            "backend": self.backend,
            "renderer_ids": list(self.renderer_ids),
            "renderer_build": self.renderer_build,
            "render_profile": self.render_profile,
            "surface": {
                "width": self.width,
                "height": self.height,
                "dpr": self.dpr,
                "alpha_mode": self.alpha_mode,
                "working_color_space": self.working_color_space,
                "bit_depth": self.bit_depth,
            },
            "font_set_hash": self.font_set_hash,
            "asset_hashes": [list(item) for item in self.asset_hashes],
            "declared_asset_ids": list(self.declared_asset_ids),
            "capability_fallback": list(self.capability_fallback),
            "frame_evidence": [dict(item) for item in self.frame_evidence],
        }
        if include_hash:
            payload["manifest_hash"] = self.manifest_hash
        return payload


def build_release_manifest(
    scene: TeachingSceneDocument,
    context: RenderContext,
    evidence: Sequence[RenderEvidence],
) -> ReleaseManifest:
    """Build a manifest whose cache identity and frame evidence agree."""
    renderer_build = evidence[0].renderer_build if evidence else context.renderer_version
    cache_manifest = build_cache_manifest(scene, 0, context, renderer_build=renderer_build)
    renderer_ids = tuple(sorted({item.renderer_id for item in evidence}))
    fallbacks = tuple(sorted({item for record in evidence for item in record.capability_fallback}))
    return ReleaseManifest(
        schema_version=1,
        scene_id=scene.scene_id,
        scene_hash=cache_manifest.scene_hash,
        backend=context.backend,
        renderer_ids=renderer_ids,
        renderer_build=renderer_build,
        render_profile=context.render_profile,
        width=context.width,
        height=context.height,
        dpr=context.dpr,
        alpha_mode="premult" if context.transparent else "straight",
        working_color_space=context.working_color_space,
        bit_depth=context.bit_depth,
        font_set_hash=cache_manifest.font_set_hash,
        asset_hashes=cache_manifest.asset_hashes,
        declared_asset_ids=cache_manifest.declared_asset_ids,
        capability_fallback=fallbacks,
        frame_evidence=tuple(item.to_dict() for item in evidence),
    )


def compare_rendered_frame_sequences(
    preview: Sequence[RenderedFrame],
    export: Sequence[RenderedFrame],
    *,
    mode: ComparisonMode = ComparisonMode.STRICT,
    perceptual_tolerance: float = 2.0,
    alpha_tolerance: int = 0,
) -> tuple[ConsistencyResult, ...]:
    """Compare same-index frame sequences and fail on length mismatch."""
    if len(preview) != len(export):
        raise ValueError("preview and export frame sequences must have equal length")
    results: list[ConsistencyResult] = []
    for left, right in zip(preview, export):
        results.append(compare_frames(
            FrameSnapshot(left.width, left.height, left.data),
            FrameSnapshot(right.width, right.height, right.data),
            mode=mode,
            perceptual_tolerance=perceptual_tolerance,
            alpha_tolerance=alpha_tolerance,
        ))
    return tuple(results)


__all__ = [
    "ReleaseManifest", "build_release_manifest", "compare_rendered_frame_sequences",
]
