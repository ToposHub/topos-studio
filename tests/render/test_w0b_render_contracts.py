"""W0-B: frozen render contracts + schema v2 migration + V2 byte-equivalence.

Covers the W0-B gate (plan §5.1.1):
* L0 — contract round-trip with unknown-field preservation; schema v2 migration;
* L1 — FrameContext / CanvasSpace / RenderSurface validation (DPR, alpha, bounds);
* L3 — the SVG renderer behind the new V2 Protocol produces byte-identical RGBA
  to the legacy path (preview/export no blocking regression), at 1x and 2x DPR
  and with transparent alpha.
"""

from __future__ import annotations

import copy
import pytest

from src.features.teaching_animation.core.contracts import (
    AlphaMode,
    Capability,
    CanvasSpace,
    ColorSpace,
    FrameContext,
    ProxyTier,
    RenderEvidence,
    RenderPass,
    RenderPlan,
    RenderSurface,
)
from src.features.teaching_animation.core.model import (
    SCENE_SCHEMA_VERSION,
    SceneAnimation,
    SceneElement,
    SceneTransform,
    TeachingSceneDocument,
)
from src.features.teaching_animation.core.migration import (
    SceneMigrationError,
    migrate_scene_json,
)
from src.features.teaching_animation.render.frame_adapter import FrameAdapter
from src.features.teaching_animation.render.svg_renderer import SvgSceneRenderer
from src.features.teaching_animation.render.v2_adapter import (
    SvgSceneRendererV2,
    frame_context_to_render_context,
)
from src.shared.keyframe import Keyframe, KeyframeTrack, InterpolationType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _track(param: str, pairs: list[tuple[int, float]]) -> KeyframeTrack:
    keyframes = [Keyframe(frame=f, value=v, interpolation=InterpolationType.LINEAR) for f, v in pairs]
    return KeyframeTrack(param_name=param, keyframes=keyframes)


def _scene() -> TeachingSceneDocument:
    title = SceneElement(
        element_id="title",
        type="text",
        transform=SceneTransform(x=40.0, y=40.0, width=400.0, height=80.0),
        style={"color": "#f9fafb", "font_size": 36.0},
        content="Step 1",
    )
    card = SceneElement(
        element_id="card",
        type="shape",
        z_index=1,
        transform=SceneTransform(x=40.0, y=140.0, width=200.0, height=120.0),
        style={"fill": "#3b82f6"},
        content={"kind": "rect"},
    )
    return TeachingSceneDocument(
        scene_id="scene-w0b",
        schema_version=SCENE_SCHEMA_VERSION,
        name="W0B Test",
        width=320,
        height=200,
        fps_num=24,
        fps_den=1,
        duration_frames=36,
        background={"color": "#0b1220"},
        elements=(title, card),
        animations=(SceneAnimation("fade", "title", "opacity", _track("opacity", [(0, 0.0), (12, 1.0)])),),
    )


# ---------------------------------------------------------------------------
# L0: contract round-trip
# ---------------------------------------------------------------------------

class TestCanvasSpaceRoundTrip:
    def test_round_trip_preserves_all_fields(self) -> None:
        canvas = CanvasSpace(design_width=1920.0, design_height=1080.0, origin=(10.0, 20.0), safe_area=(80.0, 60.0, 80.0, 60.0))
        restored = CanvasSpace.from_dict(canvas.to_dict())
        assert restored == canvas

    def test_unknown_fields_do_not_crash(self) -> None:
        data = {"design_width": 100.0, "design_height": 100.0, "future_anchor": {"x": 1}}
        canvas = CanvasSpace.from_dict(data)
        assert canvas.design_width == 100.0

    def test_from_dimensions(self) -> None:
        canvas = CanvasSpace.from_dimensions(1920, 1080)
        assert canvas.design_width == 1920.0
        assert canvas.safe_area == (0.0, 0.0, 0.0, 0.0)

    def test_zero_dimension_rejected(self) -> None:
        with pytest.raises(ValueError):
            CanvasSpace(design_width=0.0, design_height=100.0)

    def test_oversized_safe_area_rejected(self) -> None:
        with pytest.raises(ValueError):
            CanvasSpace(design_width=100.0, design_height=100.0, safe_area=(60.0, 0.0, 60.0, 0.0))


class TestRenderSurfaceRoundTrip:
    def test_round_trip(self) -> None:
        surface = RenderSurface(pixel_width=3840, pixel_height=2160, dpr=2.0,
                                colorspace=ColorSpace.LINEAR, alpha_mode=AlphaMode.PREMULT,
                                proxy_tier=ProxyTier.INTERACTIVE)
        restored = RenderSurface.from_dict(surface.to_dict())
        assert restored == surface

    def test_invalid_dpr_rejected(self) -> None:
        with pytest.raises(ValueError):
            RenderSurface(pixel_width=10, pixel_height=10, dpr=0.0)

    def test_bad_enum_falls_back_to_default(self) -> None:
        restored = RenderSurface.from_dict({"pixel_width": 10, "pixel_height": 10, "colorspace": "weird"})
        assert restored.colorspace == ColorSpace.SRGB


class TestFrameContextRoundTrip:
    def test_round_trip(self) -> None:
        ctx = FrameContext(
            frame=12, fps_num=24, fps_den=1,
            canvas=CanvasSpace.from_dimensions(320, 200),
            surface=RenderSurface(320, 200, 1.0),
            revision="rev-1", render_profile_hash="profile-abc",
        )
        restored = FrameContext.from_dict(ctx.to_dict())
        assert restored == ctx

    def test_fps_is_rational(self) -> None:
        ctx = FrameContext(frame=5, fps_num=30000, fps_den=1001,
                           canvas=CanvasSpace.from_dimensions(10, 10),
                           surface=RenderSurface(10, 10))
        assert ctx.fps == pytest.approx(29.97, rel=1e-3)
        assert ctx.time_seconds == pytest.approx(5 * 1001 / 30000)

    def test_for_scene_builds_surface_and_canvas(self) -> None:
        scene = _scene()
        ctx = FrameContext.for_scene(scene, frame=0, dpr=2.0, transparent=True)
        assert ctx.canvas.design_width == 320.0
        assert ctx.surface.pixel_width == 640  # 320 * 2.0 DPR
        assert ctx.surface.pixel_height == 400
        assert ctx.surface.dpr == 2.0
        assert ctx.surface.alpha_mode == AlphaMode.PREMULT
        assert ctx.fps_num == 24 and ctx.fps_den == 1

    def test_negative_frame_rejected(self) -> None:
        with pytest.raises(ValueError):
            FrameContext(frame=-1, fps_num=24, fps_den=1,
                         canvas=CanvasSpace.from_dimensions(10, 10),
                         surface=RenderSurface(10, 10))


class TestRenderPlanEvidence:
    def test_render_plan_round_trip(self) -> None:
        plan = RenderPlan(
            passes=(RenderPass(stage="source", target="title", capability=Capability.SVG_RASTER),),
            required_capabilities=(Capability.SVG_RASTER,),
            cache_key_inputs={"scene": "hash1"},
            fail_policy="export_block",
        )
        data = plan.to_dict()
        assert data["passes"][0]["capability"] == "svg_raster"
        assert data["fail_policy"] == "export_block"

    def test_evidence_round_trip(self) -> None:
        ev = RenderEvidence(renderer_id="r", renderer_build="b", frame_hash="deadbeef",
                            capability_fallback=("gpu_effect",), render_duration_ms=3.5)
        data = ev.to_dict()
        assert data["capability_fallback"] == ["gpu_effect"]
        assert data["frame_hash"] == "deadbeef"


# ---------------------------------------------------------------------------
# L0: schema v2 migration
# ---------------------------------------------------------------------------

class TestSchemaV2Migration:
    def test_current_version_is_three(self) -> None:
        assert SCENE_SCHEMA_VERSION == 3

    def test_v1_scene_migrates_to_v3_additively(self) -> None:
        v1 = {
            "schema_version": 1,
            "scene_id": "s1", "name": "S",
            "width": 100, "height": 100, "fps_num": 24, "fps_den": 1,
            "duration_frames": 10,
            "background": {"color": "#000"},
            "elements": [{"element_id": "e1", "type": "text"}],
            "animations": [], "markers": [], "asset_refs": [],
        }
        migrated = migrate_scene_json(v1)
        assert migrated["schema_version"] == 3
        assert migrated["canvas"] == {}      # additive hook, derived on read
        assert migrated["cues"] == []
        # No semantic change: original fields preserved.
        for key in ("scene_id", "name", "width", "height", "fps_num", "duration_frames", "elements"):
            assert key in migrated

    def test_v0_legacy_migrates_through_v1_to_v3(self) -> None:
        legacy = {"scene_id": "legacy", "name": "L", "fps": 30.0, "duration_seconds": 1.0,
                  "layers": [{"type": "text", "content": "hi"}], "unknown_future": {"x": 1}}
        migrated = migrate_scene_json(legacy)
        assert migrated["schema_version"] == 3
        assert migrated["fps_num"] == 30
        assert migrated["duration_frames"] == 30
        assert migrated["unknown_future"] == {"x": 1}
        assert migrated["canvas"] == {}

    def test_v2_scene_migrates_to_v3_with_default_layout(self) -> None:
        v2 = {
            "schema_version": 2,
            "scene_id": "s2", "name": "S2",
            "width": 100, "height": 100, "fps_num": 24, "fps_den": 1,
            "duration_frames": 10,
            "background": {"color": "#000"},
            "elements": [
                {"element_id": "e1", "type": "text", "custom_future": {"keep": 1}},
            ],
            "animations": [], "markers": [], "asset_refs": [],
        }
        migrated = migrate_scene_json(v2)
        assert migrated["schema_version"] == 3
        # Every element gains the additive layout mapping; unknown element
        # fields keep round-tripping untouched.
        assert migrated["elements"][0]["layout"] == {}
        assert migrated["elements"][0]["custom_future"] == {"keep": 1}

    def test_v3_frame_layout_validates_and_round_trips(self) -> None:
        from src.features.teaching_animation.core.model import SceneElement, SceneTransform
        from src.features.teaching_animation.core.validator import validate_scene_json

        scene = _scene()
        frame = SceneElement(
            "frame1", "frame", transform=SceneTransform(width=400.0, height=200.0),
            style={"fill": "#1e293b"},
            layout={"mode": "flex-row", "gap": 12.0, "justify": "between"},
        )
        scene = scene.with_element_added(frame)
        serialized = scene.to_dict()
        assert serialized["schema_version"] == 3
        result = validate_scene_json(serialized)
        assert result.is_valid, result.errors
        restored = TeachingSceneDocument.from_dict(migrate_scene_json(serialized))
        # 规格裁决（P1-RENDER-01）：v3 文档物化时，空 page_id 的元素归入
        # 默认页 'page_1'（v3 要求每个元素归属页面；默认页由
        # TeachingSceneDocument 构造给出）。其余字段逐位保持不变。
        expected = SceneElement(
            "frame1", "frame", transform=SceneTransform(width=400.0, height=200.0),
            style={"fill": "#1e293b"},
            layout={"mode": "flex-row", "gap": 12.0, "justify": "between"},
            page_id="page_1",
        )
        assert restored.element_by_id("frame1") == expected

    def test_v3_invalid_layout_flags_validator(self) -> None:
        from src.features.teaching_animation.core.validator import validate_scene_json

        data = _scene().to_dict()
        data["elements"].append({"element_id": "bad", "type": "frame", "layout": {"mode": "diagonal"}})
        result = validate_scene_json(data)
        assert any(issue.code == "enum" and issue.path.endswith("layout.mode") for issue in result.errors)

    def test_unknown_fields_preserved(self) -> None:
        data = {"schema_version": 2, "scene_id": "s", "name": "n", "width": 10, "height": 10,
                "fps_num": 24, "fps_den": 1, "duration_frames": 5, "background": {},
                "elements": [], "animations": [], "markers": [], "asset_refs": [],
                "future_field": [1, 2, 3]}
        migrated = migrate_scene_json(copy.deepcopy(data))
        assert migrated["future_field"] == [1, 2, 3]

    def test_future_version_rejected(self) -> None:
        with pytest.raises(SceneMigrationError):
            migrate_scene_json({"schema_version": 99})

    def test_v2_round_trip_through_model(self) -> None:
        scene = _scene()
        serialized = scene.to_dict()
        assert serialized["schema_version"] == 3
        assert serialized["canvas"] == {}
        restored = TeachingSceneDocument.from_dict(serialized)
        assert restored == scene
        # canvas is editable and survives round-trip when populated.
        canvas_scene = TeachingSceneDocument.from_dict({**serialized, "canvas": {"design_width": 320.0, "design_height": 200.0}})
        assert canvas_scene.canvas == {"design_width": 320.0, "design_height": 200.0}


# ---------------------------------------------------------------------------
# L3: V2 Protocol byte-equivalence (the no-blocking-regression gate)
# ---------------------------------------------------------------------------

class TestV2ByteEquivalence:
    @pytest.mark.parametrize("dpr,transparent", [(1.0, False), (2.0, False), (1.0, True), (2.0, True)])
    def test_v2_matches_legacy_rgba_byte_for_byte(self, dpr: float, transparent: bool) -> None:
        scene = _scene()
        for frame in (0, 1, 12, 35):
            # Legacy path: SvgSceneRenderer -> FrameAdapter.
            from src.features.teaching_animation.core.contracts import AnimationFrame, RenderContext
            legacy_ctx = RenderContext(
                fps=scene.fps,
                width=int(round(scene.width * dpr)),
                height=int(round(scene.height * dpr)),
                renderer_version="legacy", backend="svg",
                transparent=transparent,
            )
            legacy_renderer = SvgSceneRenderer()
            legacy_renderer.prepare(scene, legacy_ctx)
            try:
                legacy_rgba = FrameAdapter.from_rendered_frame(
                    legacy_renderer.render_frame(AnimationFrame(frame, scene.fps))
                )
            finally:
                legacy_renderer.cleanup()

            # V2 path: frozen FrameContext -> SvgSceneRendererV2.
            alpha = AlphaMode.PREMULT if transparent else AlphaMode.STRAIGHT
            fc = FrameContext(
                frame=frame, fps_num=scene.fps_num, fps_den=scene.fps_den,
                canvas=CanvasSpace.from_dimensions(scene.width, scene.height),
                surface=RenderSurface(
                    pixel_width=int(round(scene.width * dpr)),
                    pixel_height=int(round(scene.height * dpr)),
                    dpr=dpr, alpha_mode=alpha,
                ),
            )
            v2 = SvgSceneRendererV2()
            session = v2.prepare(scene, fc)
            try:
                result = v2.render(session, fc)
            finally:
                v2.close(session)

            assert result.frame.pixel_format == "rgba8"
            assert result.frame.width == legacy_rgba.width
            assert result.frame.height == legacy_rgba.height
            assert bytes(result.frame.data) == bytes(legacy_rgba.data), (
                f"V2 vs legacy RGBA mismatch at frame={frame} dpr={dpr} transparent={transparent}"
            )
            # Evidence is populated and deterministic in shape.
            assert result.evidence.renderer_id == "teaching-svg"
            assert result.evidence.frame_hash
            assert "plan_stages" in result.evidence.diagnostics

    def test_render_plan_declares_svg_capability(self) -> None:
        scene = _scene()
        from src.features.teaching_animation.render.v2_adapter import build_svg_render_plan
        plan = build_svg_render_plan(scene)
        assert Capability.SVG_RASTER in plan.required_capabilities
        # one composite + one pass per visible element
        stages = [p.stage for p in plan.passes]
        assert stages[0] == "composite"
        assert stages.count("source") == len(scene.elements)

    def test_frame_context_to_render_context_maps_alpha(self) -> None:
        fc = FrameContext(
            frame=0, fps_num=24, fps_den=1,
            canvas=CanvasSpace.from_dimensions(10, 10),
            surface=RenderSurface(20, 20, dpr=2.0, alpha_mode=AlphaMode.PREMULT),
        )
        rc = frame_context_to_render_context(fc)
        assert rc.width == 20 and rc.height == 20
        assert rc.fps == 24.0
        assert rc.transparent is True

    def test_close_is_idempotent(self) -> None:
        scene = _scene()
        fc = FrameContext.for_scene(scene, frame=0)
        v2 = SvgSceneRendererV2()
        session = v2.prepare(scene, fc)
        v2.close(session)
        v2.close(session)  # must not raise
