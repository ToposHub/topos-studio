"""Deterministic render adapters for preview, export and timeline consumers."""

from .asset_resolver import AssetIssue, AssetPreflightError, AssetPreflightResult, AssetResolver
from .evaluator import EvaluatedElement, EvaluatedScene, evaluate
from .frame_adapter import FrameAdapter, RgbaFrame
from .html_preview_renderer import HtmlPreviewRenderer
from .cache import RenderCacheManifest, SceneRenderCache, build_cache_manifest
from .benchmark import (
    BenchmarkFixture, BenchmarkReport, BenchmarkSegment, build_reference_scene,
    measure_segments, run_benchmark,
)
from .render_worker import RenderHandle, RenderProgress, RenderResult, RenderWorker
from .advanced import (
    AdvancedBackendCapabilities, ChartData, ModernGLEffectRegistry, WebGLPreviewRenderer,
    formula_to_plain_text, render_chart_svg, render_draw_svg, resolve_localized_text,
)
from .scene_renderer import CancellationToken, RenderCancelled
from .svg_renderer import SvgSceneRenderer, render_svg_demo
from .v2_adapter import (
    SvgRendererSession,
    SvgSceneRendererV2,
    build_svg_render_plan,
    frame_context_to_render_context,
)
from .html_renderer_service import HtmlRendererService  # noqa: F401 — re-export（tests.render.test_html_renderer_service 使用）
from .html_scene_renderer_v2 import HtmlRenderError, HtmlSceneRendererV2  # noqa: F401 — re-export
from .scene_renderer_router import (
    HtmlSceneRendererV1Adapter,
    backend_for_scene,
    get_shared_html_service,
    make_playwright_backend_factory,
    reset_shared_html_service,
    scene_has_html_layer,
    select_scene_renderer,
)
from .effect_capabilities import (
    BackendEffectCapability,
    BackendEffectSupport,
    DirtyRange,
    EffectCapabilityMatrix,
    EffectDeclaration,
    RenderProfile,
    RenderProfileName,
    ResolvedEffect,
    ResolvedEffectChain,
    dirty_range_for_element,
    read_effect_declarations,
    read_scene_gpu_effects,
    resolve_effect_chain,
    write_effect_declarations,
)
from .composite_effect_bridge import CompositeEffectBridge, CompositeEffectResult, dtype_for_color_pipeline
from .consistency import (
    ComparisonMode,
    ConsistencyIssue,
    ConsistencyResult,
    FrameSnapshot,
    compare_frames,
    golden_evidence,
)
from .release_manifest import ReleaseManifest, build_release_manifest, compare_rendered_frame_sequences
from .release_fixtures import ReleaseFixtureResult, build_s0_s3_fixtures, render_release_fixture

__all__ = [
    "AdvancedBackendCapabilities", "AssetIssue", "AssetPreflightError", "AssetPreflightResult", "AssetResolver",
    "BenchmarkFixture", "BenchmarkReport", "BenchmarkSegment",
    "BackendEffectCapability", "BackendEffectSupport", "CompositeEffectBridge", "CompositeEffectResult", "dtype_for_color_pipeline",
    "ChartData",
    "CancellationToken", "ComparisonMode", "ConsistencyIssue", "ConsistencyResult",
    "DirtyRange", "EffectCapabilityMatrix", "EffectDeclaration", "EvaluatedElement",
    "EvaluatedScene", "FrameAdapter", "FrameSnapshot",
    "HtmlPreviewRenderer", "HtmlSceneRendererV1Adapter", "RenderCacheManifest",
    "RenderCancelled", "RenderHandle",
    "RenderProfile", "RenderProfileName", "RenderProgress", "RenderResult", "RenderWorker", "ReleaseManifest",
    "ReleaseFixtureResult", "ResolvedEffect", "ResolvedEffectChain", "RgbaFrame", "SceneRenderCache",
    "ModernGLEffectRegistry", "SvgSceneRenderer", "SvgSceneRendererV2", "SvgRendererSession",
    "WebGLPreviewRenderer", "backend_for_scene", "build_cache_manifest", "build_svg_render_plan",
    "build_reference_scene", "build_release_manifest", "build_s0_s3_fixtures", "compare_frames", "compare_rendered_frame_sequences", "dirty_range_for_element", "evaluate", "formula_to_plain_text",
    "frame_context_to_render_context", "get_shared_html_service", "golden_evidence",
    "make_playwright_backend_factory", "render_chart_svg", "render_draw_svg", "render_svg_demo",
    "read_effect_declarations", "read_scene_gpu_effects", "reset_shared_html_service", "resolve_effect_chain", "resolve_localized_text",
    "measure_segments", "render_release_fixture", "run_benchmark", "scene_has_html_layer", "select_scene_renderer", "write_effect_declarations",
]
