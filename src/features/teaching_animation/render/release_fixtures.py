"""Deterministic S0-S3 release fixtures for cross-layer acceptance gates.

The fixtures intentionally keep the scene surface small.  They exercise the
failure-prone product combinations (locale, alpha, missing asset and effect
fallback) without depending on a machine-local media file or browser cache.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from typing import Any

from ..core.contracts import AnimationFrame, RenderContext, RenderEvidence, RenderedFrame
from ..core.model import SCENE_SCHEMA_VERSION, SceneAssetRef, SceneElement, SceneTransform, TeachingSceneDocument
from .effect_capabilities import RenderProfile
from .release_manifest import ReleaseManifest, build_release_manifest
from .svg_renderer import SvgSceneRenderer
from .v2_adapter import build_svg_render_plan


@dataclass(frozen=True)
class ReleaseFixtureResult:
    """Two deterministic render passes and their provenance manifest."""

    fixture_id: str
    scene: TeachingSceneDocument
    context: RenderContext
    frames: tuple[int, ...]
    first: tuple[RenderedFrame, ...]
    second: tuple[RenderedFrame, ...]
    manifest: ReleaseManifest

    @property
    def strict_consistent(self) -> bool:
        return len(self.first) == len(self.second) and all(
            left.frame == right.frame
            and left.width == right.width
            and left.height == right.height
            and left.pixel_format == right.pixel_format
            and left.data == right.data
            for left, right in zip(self.first, self.second)
        )


def _title(element_id: str, content: Any) -> SceneElement:
    return SceneElement(
        element_id=element_id,
        type="text",
        transform=SceneTransform(x=8.0, y=8.0, width=110.0, height=18.0),
        style={"color": "#f9fafb", "font_size": 12.0},
        content=content,
    )


def build_s0_s3_fixtures() -> tuple[tuple[str, TeachingSceneDocument, RenderContext], ...]:
    """Return the fixed acceptance scenes in S0, S1, S2, S3 order."""
    common = dict(width=128, height=72, fps_num=24, fps_den=1, duration_frames=6)
    s0 = TeachingSceneDocument(
        scene_id="release-s0-basic-v1",
        schema_version=SCENE_SCHEMA_VERSION,
        name="S0 Basic",
        background={"color": "#111827"},
        elements=(_title("s0-title", "Motion Graphic"),),
        animations=(),
        **common,
    )
    s1 = replace(
        s0,
        scene_id="release-s1-cjk-v1",
        name="S1 CJK",
        elements=(_title("s1-title", {"locales": {"en-US": "Teaching", "zh-CN": "教学动画"}}),),
    )
    s2 = replace(
        s0,
        scene_id="release-s2-transparent-v1",
        name="S2 Transparent",
        background={},
    )
    s3 = replace(
        s0,
        scene_id="release-s3-fallback-v1",
        name="S3 Missing Asset + Fallback",
        elements=(
            _title("s3-title", "Fallback"),
            SceneElement(
                element_id="s3-missing-image",
                type="image",
                z_index=1,
                transform=SceneTransform(x=8.0, y=34.0, width=40.0, height=28.0),
                asset_ref="missing-image",
                style={"effects": [{"effect_type": "glow", "params": {}}]},
            ),
        ),
        asset_refs=(SceneAssetRef("missing-image", path="fixtures/not-present.png"),),
    )
    return (
        ("S0", s0, RenderContext(width=128, height=72, render_profile="preview", locale="en-US")),
        ("S1", s1, RenderContext(width=128, height=72, render_profile="preview", locale="zh-CN")),
        ("S2", s2, RenderContext(width=128, height=72, render_profile="preview", transparent=True)),
        (
            "S3",
            s3,
            RenderContext(
                width=128,
                height=72,
                render_profile="preview",
                strict_assets=False,
                locale="en-US",
            ),
        ),
    )


def _render_once(
    scene: TeachingSceneDocument,
    context: RenderContext,
    frames: tuple[int, ...],
) -> tuple[RenderedFrame, ...]:
    renderer = SvgSceneRenderer()
    renderer.prepare(scene, context)
    try:
        return tuple(renderer.render_frame(AnimationFrame(frame, scene.fps)) for frame in frames)
    finally:
        renderer.cleanup()


def render_release_fixture(
    fixture_id: str,
    scene: TeachingSceneDocument,
    context: RenderContext,
    frames: tuple[int, ...] = (0, 3, 5),
) -> ReleaseFixtureResult:
    """Render one fixture twice and build its release provenance manifest."""
    first = _render_once(scene, context, frames)
    second = _render_once(scene, context, frames)
    plan = build_svg_render_plan(scene, RenderProfile.preview())
    fallback = tuple(
        item for item in plan.cache_key_inputs.get("effect_fallback", "").split(",") if item
    )
    evidence = tuple(
        RenderEvidence(
            renderer_id="teaching-svg",
            renderer_build="teaching-animation-svg/0.2",
            frame_hash=hashlib.sha256(frame.data).hexdigest(),
            capability_fallback=fallback,
            diagnostics={"fixture_id": fixture_id, "frame": frame.frame},
        )
        for frame in first
    )
    return ReleaseFixtureResult(
        fixture_id=fixture_id,
        scene=scene,
        context=context,
        frames=frames,
        first=first,
        second=second,
        manifest=build_release_manifest(scene, context, evidence),
    )


__all__ = [
    "ReleaseFixtureResult",
    "build_s0_s3_fixtures",
    "render_release_fixture",
]
