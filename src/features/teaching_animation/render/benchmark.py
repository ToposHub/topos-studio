"""Repeatable performance measurements for the Teaching Animation renderer.

The benchmark deliberately measures the existing host render path through
``RenderWorker``.  It does not use wall-clock time as scene input: the only
time-dependent values are measurements, while every frame and fixture input
is explicit and deterministic.  This keeps the report useful for comparing
renderer/cache changes without introducing a second rendering implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
import platform
import time
from typing import Any, Mapping, Sequence

from ..core.contracts import (
    AnimationFrame,
    FrameContext,
    ProxyTier,
    RenderContext,
)
from ..core.html_layer import build_html_layer
from ..core.model import SCENE_SCHEMA_VERSION, SceneAnimation, SceneElement, SceneTransform, TeachingSceneDocument
from src.shared.keyframe import Keyframe, KeyframeTrack
from .cache import SceneRenderCache, build_cache_manifest
from .composite_effect_bridge import CompositeEffectBridge
from .effect_capabilities import EffectDeclaration, RenderProfile
from .frame_adapter import FrameAdapter
from .perf_slo import (
    BenchmarkEnvironment,
    BenchmarkSample,
    PerfGate,
    PerfGateResult,
    PerfMetric,
    default_budgets,
)
from .render_worker import RenderWorker
from .svg_renderer import SvgSceneRenderer


@dataclass(frozen=True)
class BenchmarkFixture:
    """A named, deterministic scene shape used as performance evidence."""

    fixture_id: str = "teaching-animation/reference-svg-v1"
    width: int = 1920
    height: int = 1080
    fps: int = 24
    duration_frames: int = 240
    element_count: int = 16

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0 or self.fps <= 0 or self.duration_frames <= 0:
            raise ValueError("benchmark dimensions, fps and duration must be positive")
        if self.element_count < 1:
            raise ValueError("element_count must be positive")

    @property
    def fixture_hash(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "fixture_id": self.fixture_id,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "duration_frames": self.duration_frames,
            "element_count": self.element_count,
        }


@dataclass(frozen=True)
class BenchmarkReport:
    """Serializable benchmark evidence and the optional product-target gate."""

    schema_version: int
    fixture: BenchmarkFixture
    environment: BenchmarkEnvironment
    samples: tuple[BenchmarkSample, ...]
    series: Mapping[str, tuple[float, ...]]
    cache_stats: Mapping[str, int]
    output_hashes: tuple[tuple[str, str], ...]
    gate: PerfGateResult
    segments: tuple["BenchmarkSegment", ...] = ()
    memory_rss_start_bytes: int | None = None
    memory_rss_peak_bytes: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "fixture": self.fixture.to_dict() | {"fixture_hash": self.fixture.fixture_hash},
            "environment": self.environment.to_dict(),
            "samples": [sample.to_dict() for sample in self.samples],
            "series": {
                name: {
                    "values": list(values),
                    "p50": _percentile(values, 0.50),
                    "p95": _percentile(values, 0.95),
                }
                for name, values in sorted(self.series.items())
            },
            "cache_stats": dict(self.cache_stats),
            "output_hashes": [list(item) for item in self.output_hashes],
            "gate": {
                "passed": self.gate.passed,
                "violations": [
                    {
                        "metric": item.metric.value,
                        "budget": item.budget,
                        "measured": item.measured,
                        "profile": item.profile,
                        "fixture_id": item.fixture_id,
                        "exceeded_by": item.exceeded_by,
                    }
                    for item in self.gate.violations
                ],
                "notes": list(self.gate.notes),
            },
            "segments": [segment.to_dict() for segment in self.segments],
            "memory": {
                "rss_start_bytes": self.memory_rss_start_bytes,
                "rss_peak_bytes": self.memory_rss_peak_bytes,
            },
        }


@dataclass(frozen=True)
class BenchmarkSegment:
    """One optional backend-stage measurement or an explicit environment skip."""

    name: str
    status: str
    values: tuple[float, ...] = ()
    unit: str = "ms"
    reason: str = ""
    output_hashes: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "unit": self.unit,
            "values": list(self.values),
            "p50": _percentile(self.values, 0.50) if self.values else None,
            "p95": _percentile(self.values, 0.95) if self.values else None,
            "reason": self.reason,
            "output_hashes": [list(item) for item in self.output_hashes],
        }


def build_reference_scene(fixture: BenchmarkFixture) -> TeachingSceneDocument:
    """Build the stable reference scene without generated UUIDs."""
    if fixture.element_count < 1:
        raise ValueError("element_count must be positive")
    elements: list[SceneElement] = [
        SceneElement(
            element_id="title",
            type="text",
            transform=SceneTransform(x=80.0, y=60.0, width=1200.0, height=100.0),
            style={"color": "#f9fafb", "font_size": 48.0},
            content="Teaching Animation Benchmark",
        ),
    ]
    for index in range(fixture.element_count - 1):
        column = index % 4
        row = index // 4
        if index % 3 == 0:
            element_type = "shape"
            content: Any = {
                "kind": "rect" if index % 2 == 0 else "circle",
                "fill": "#2563eb" if index % 2 == 0 else "#f59e0b",
            }
            style: Mapping[str, Any] = {"stroke": "#e5e7eb", "stroke_width": 1.0}
        else:
            element_type = "text"
            content = f"Card {index + 1:02d}"
            style = {"color": "#d1d5db", "font_size": 24.0}
        elements.append(SceneElement(
            element_id=f"element-{index:02d}",
            type=element_type,
            z_index=index + 1,
            transform=SceneTransform(
                x=80.0 + column * 430.0,
                y=220.0 + row * 190.0,
                width=360.0,
                height=130.0,
            ),
            style=style,
            content=content,
        ))
    last_frame = max(0, fixture.duration_frames - 1)
    animations = (
        SceneAnimation(
            animation_id="title-slide",
            element_id="title",
            property="x",
            track=KeyframeTrack(
                param_name="x",
                keyframes=[Keyframe(0, 80.0), Keyframe(last_frame, 120.0)],
                min_val=0.0,
                max_val=float(fixture.width),
            ),
        ),
    )
    if fixture.element_count > 1:
        animations += (
            SceneAnimation(
                animation_id="first-card-opacity",
                element_id="element-00",
                property="opacity",
                track=KeyframeTrack(
                    param_name="opacity",
                    keyframes=[Keyframe(0, 0.75), Keyframe(last_frame, 1.0)],
                    min_val=0.0,
                    max_val=1.0,
                ),
            ),
        )
    return TeachingSceneDocument(
        scene_id=fixture.fixture_id,
        schema_version=SCENE_SCHEMA_VERSION,
        name="Teaching Animation Benchmark",
        width=fixture.width,
        height=fixture.height,
        fps_num=fixture.fps,
        fps_den=1,
        duration_frames=fixture.duration_frames,
        background={"color": "#111827"},
        elements=tuple(elements),
        animations=animations,
        metadata={"benchmark_fixture_hash": fixture.fixture_hash},
    )


def run_benchmark(
    scene: TeachingSceneDocument,
    context: RenderContext,
    frames: Sequence[int],
    *,
    fixture: BenchmarkFixture | None = None,
    iterations: int = 3,
) -> BenchmarkReport:
    """Measure cold, warm, interactive, export and single-element update paths."""
    frame_numbers = tuple(int(frame) for frame in frames)
    if not frame_numbers:
        raise ValueError("at least one frame is required")
    if any(frame < 0 or frame >= scene.duration_frames for frame in frame_numbers):
        raise ValueError("benchmark frames must be inside the scene duration")
    if iterations < 1:
        raise ValueError("iterations must be positive")
    fixture = fixture or BenchmarkFixture(
        fixture_id=scene.scene_id,
        width=scene.width,
        height=scene.height,
        fps=max(1, round(scene.fps)),
        duration_frames=scene.duration_frames,
        element_count=len(scene.elements),
    )
    if context.width != scene.width or context.height != scene.height:
        raise ValueError("render context dimensions must match the benchmark scene")

    manifest = build_cache_manifest(scene, frame_numbers[0], context)
    asset_hash = _hash_json(list(manifest.asset_hashes))
    environment = BenchmarkEnvironment(
        os=platform.platform(),
        cpu=platform.processor() or platform.machine(),
        gpu=os.environ.get("MOTION_GRAPHIC_GPU", "unknown"),
        driver=os.environ.get("MOTION_GRAPHIC_DRIVER", "unknown"),
        renderer_build=context.renderer_version,
        profile=context.render_profile,
        fixture_id=fixture.fixture_id,
        dpr=context.dpr,
        asset_hash=asset_hash,
        fixture_hash=fixture.fixture_hash,
    )

    series: dict[str, list[float]] = {
        "first_frame_ms": [],
        "warm_frame_ms": [],
        "interactive_frame_ms": [],
        "export_fps": [],
        "element_update_ms": [],
    }
    cache_totals = {"entries": 0, "hits": 0, "misses": 0, "invalidations": 0}
    memory_start = _process_rss_bytes()
    memory_peak = memory_start
    output_hashes: dict[str, str] = {}
    changed = _scene_with_single_element_change(scene)

    for _ in range(iterations):
        cache = SceneRenderCache(max_entries=max(64, len(frame_numbers) * 4))
        worker = RenderWorker(cache=cache)
        try:
            cache.clear()
            first_elapsed, first_frames = _measure_render(worker, scene, (frame_numbers[0],), context)
            series["first_frame_ms"].append(first_elapsed)
            _record_hash(output_hashes, "first", first_frames[0].data)

            warm_elapsed, warm_frames = _measure_render(worker, scene, (frame_numbers[0],), context)
            series["warm_frame_ms"].append(warm_elapsed)
            _record_hash(output_hashes, "warm", warm_frames[0].data)

            cache.clear()
            export_elapsed, export_frames = _measure_render(worker, scene, frame_numbers, context)
            series["export_fps"].append(len(frame_numbers) / max(export_elapsed / 1000.0, 1e-9))
            for frame in export_frames:
                _record_hash(output_hashes, f"frame_{frame.frame}", frame.data)

            cache.clear()
            interactive_elapsed: list[float] = []
            for frame_number in frame_numbers:
                elapsed, _ = _measure_render(worker, scene, (frame_number,), context)
                interactive_elapsed.append(elapsed)
            series["interactive_frame_ms"].append(_percentile(interactive_elapsed, 0.50))

            update_elapsed, _ = _measure_render(worker, changed, (frame_numbers[0],), context)
            series["element_update_ms"].append(update_elapsed)
            stats = cache.stats()
            for key in cache_totals:
                cache_totals[key] += stats[key]
            current_rss = _process_rss_bytes()
            if current_rss is not None:
                memory_peak = max(memory_peak or current_rss, current_rss)
        finally:
            worker.shutdown()

    samples = tuple(
        BenchmarkSample(metric, _percentile(tuple(series[name]), 0.50), environment)
        for name, metric in (
            ("first_frame_ms", PerfMetric.FIRST_FRAME_MS),
            ("interactive_frame_ms", PerfMetric.INTERACTIVE_FRAME_MS),
            ("export_fps", PerfMetric.EXPORT_FPS),
            ("element_update_ms", PerfMetric.ELEMENT_UPDATE_MS),
        )
    )
    gate = PerfGate(default_budgets()).evaluate(samples)
    return BenchmarkReport(
        schema_version=1,
        fixture=fixture,
        environment=environment,
        samples=samples,
        series={name: tuple(values) for name, values in series.items()},
        cache_stats=cache_totals,
        output_hashes=tuple(sorted(output_hashes.items())),
        gate=gate,
        memory_rss_start_bytes=memory_start,
        memory_rss_peak_bytes=memory_peak,
    )


def measure_segments(
    scene: TeachingSceneDocument,
    context: RenderContext,
    frames: Sequence[int],
    *,
    names: Sequence[str],
    iterations: int = 1,
) -> tuple[BenchmarkSegment, ...]:
    """Measure optional raster, browser and GPU stages with honest skip states."""
    if iterations < 1:
        raise ValueError("iterations must be positive")
    frame_numbers = tuple(int(frame) for frame in frames)
    if not frame_numbers:
        raise ValueError("at least one frame is required")
    allowed = {"qt_rasterize", "html_capture", "gpu_effect", "export_encode"}
    unknown = set(names) - allowed
    if unknown:
        raise ValueError(f"unknown benchmark segment(s): {sorted(unknown)}")
    result: list[BenchmarkSegment] = []
    for name in names:
        if name == "qt_rasterize":
            result.append(_measure_qt_rasterize(scene, context, frame_numbers, iterations))
        elif name == "html_capture":
            result.append(_measure_html_capture(scene, context, frame_numbers, iterations))
        elif name == "gpu_effect":
            result.append(_measure_gpu_effect(context, iterations))
        elif name == "export_encode":
            result.append(_measure_export_encode(scene, context, frame_numbers, iterations))
    return tuple(result)


def _measure_render(
    worker: RenderWorker,
    scene: TeachingSceneDocument,
    frames: Sequence[int],
    context: RenderContext,
) -> tuple[float, tuple[Any, ...]]:
    started = time.perf_counter()
    result = worker.render_sync(scene, frames, context)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return elapsed_ms, result.frames


def _scene_with_single_element_change(scene: TeachingSceneDocument) -> TeachingSceneDocument:
    element = scene.elements[0]
    changed = replace(element, content=f"{element.content} / updated")
    return scene.with_element_updated(changed)


def _measure_qt_rasterize(
    scene: TeachingSceneDocument,
    context: RenderContext,
    frames: tuple[int, ...],
    iterations: int,
) -> BenchmarkSegment:
    values: list[float] = []
    hashes: dict[str, str] = {}
    for _ in range(iterations):
        renderer = SvgSceneRenderer()
        try:
            renderer.prepare(scene, context)
            for frame_number in frames:
                svg = renderer.render_frame(AnimationFrame(frame_number, context.fps))
                started = time.perf_counter()
                rgba = FrameAdapter.from_rendered_frame(svg)
                values.append((time.perf_counter() - started) * 1000.0)
                _record_hash(hashes, f"frame_{frame_number}", rgba.data)
        except (ImportError, ModuleNotFoundError, RuntimeError, TypeError, ValueError) as exc:
            return BenchmarkSegment("qt_rasterize", "skipped", reason=f"Qt rasterizer unavailable: {exc}")
        finally:
            renderer.cleanup()
    return BenchmarkSegment("qt_rasterize", "passed", tuple(values), output_hashes=tuple(sorted(hashes.items())))


def _measure_html_capture(
    scene: TeachingSceneDocument,
    context: RenderContext,
    frames: tuple[int, ...],
    iterations: int,
) -> BenchmarkSegment:
    try:
        from .html_renderer_service import HtmlRendererService
        from .html_renderer_spike.playwright_backend import PlaywrightChromiumBackend
        probe = PlaywrightChromiumBackend()
        available = probe.is_available()
        build = probe.backend_build
        probe.close()
        if not available:
            return BenchmarkSegment(
                "html_capture", "skipped",
                reason=f"pinned Chromium unavailable ({build}); install the html-renderer extra",
            )
        from .html_scene_renderer_v2 import HtmlSceneRendererV2
        html = (
            "<html><head><style>html,body{margin:0;background:#111827}"
            ".box{position:absolute;left:8%;top:20%;width:36%;height:45%;"
            "background:#2563eb;animation:slide 1s linear infinite}"
            ".label{position:absolute;left:8%;top:72%;color:#f9fafb;font-size:32px}"
            "@keyframes slide{0%{transform:translateX(0)}100%{transform:translateX(80px)}}"
            "</style></head><body><div class='box'></div><div class='label'>Benchmark</div></body></html>"
        )
        layer = build_html_layer(
            layer_id="benchmark-html",
            sanitized_html=html,
            authored_source=html,
            viewport_width=scene.width,
            viewport_height=scene.height,
            fps_num=scene.fps_num,
            fps_den=scene.fps_den,
            duration_frames=scene.duration_frames,
            csp="default-src 'none'; style-src 'unsafe-inline'",
        )
        html_scene = replace(scene, html_layer=layer)
        service = HtmlRendererService(lambda: PlaywrightChromiumBackend(), pool_size=1)
        renderer = HtmlSceneRendererV2(service)
        values: list[float] = []
        hashes: dict[str, str] = {}
        proxy = ProxyTier(context.render_profile)
        try:
            for _ in range(iterations):
                session = renderer.prepare(html_scene, FrameContext.for_scene(
                    html_scene, frames[0], dpr=context.dpr, proxy_tier=proxy))
                try:
                    for frame_number in frames:
                        frame_context = FrameContext.for_scene(
                            html_scene, frame_number, dpr=context.dpr, proxy_tier=proxy)
                        started = time.perf_counter()
                        rendered = renderer.render(session, frame_context)
                        values.append((time.perf_counter() - started) * 1000.0)
                        _record_hash(hashes, f"frame_{frame_number}", rendered.frame.data)
                finally:
                    renderer.close(session)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return BenchmarkSegment("html_capture", "skipped", reason=f"HTML capture failed: {exc}")
        finally:
            service.close()
        return BenchmarkSegment("html_capture", "passed", tuple(values), output_hashes=tuple(sorted(hashes.items())))
    except (ImportError, ModuleNotFoundError, OSError, RuntimeError, TypeError, ValueError) as exc:
        return BenchmarkSegment("html_capture", "skipped", reason=f"HTML backend unavailable: {exc}")


def _measure_gpu_effect(context: RenderContext, iterations: int) -> BenchmarkSegment:
    try:
        import moderngl
        gl_context = moderngl.create_standalone_context()
    except (ImportError, ModuleNotFoundError, OSError, RuntimeError) as exc:
        return BenchmarkSegment("gpu_effect", "skipped", reason=f"ModernGL context unavailable: {exc}")
    # AUD-29805: every GL object created below must be owned by one try/finally
    # so an early failure (texture alloc/write, bridge construction) still
    # releases the context/texture instead of leaking them.
    source: Any = None
    bridge: CompositeEffectBridge | None = None
    values: list[float] = []
    try:
        source = gl_context.texture((32, 32), 4, dtype="f1")
        source.write(bytes([128, 64, 32, 255]) * (32 * 32))
        bridge = CompositeEffectBridge(gl_context)
        for _ in range(iterations):
            started = time.perf_counter()
            result = bridge.apply(
                source.glo,
                32,
                32,
                (EffectDeclaration("motion_blur", {"radius": 4.0, "angle": 45.0}),),
                profile=RenderProfile.preview(),
                dtype="f1",
            )
            values.append((time.perf_counter() - started) * 1000.0)
            if result.texture_id <= 0:
                return BenchmarkSegment("gpu_effect", "skipped", reason="effect pass returned no texture")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return BenchmarkSegment("gpu_effect", "skipped", reason=f"GPU effect failed: {exc}")
    finally:
        if bridge is not None:
            bridge.cleanup()
        if source is not None:
            source.release()
        gl_context.release()
    return BenchmarkSegment("gpu_effect", "passed", tuple(values))


def _measure_export_encode(
    scene: TeachingSceneDocument,
    context: RenderContext,
    frames: tuple[int, ...],
    iterations: int,
) -> BenchmarkSegment:
    """Measure PyAV mux/encode after the renderer has produced RGBA frames."""
    try:
        from io import BytesIO
        from fractions import Fraction
        import av
        import numpy as np
        rendered_frames = []
        renderer = SvgSceneRenderer()
        try:
            renderer.prepare(scene, context)
            for frame_number in frames:
                rendered = renderer.render_frame(AnimationFrame(frame_number, context.fps))
                rendered_frames.append(FrameAdapter.from_rendered_frame(rendered))
        finally:
            renderer.cleanup()
    except (ImportError, ModuleNotFoundError, OSError, RuntimeError, TypeError, ValueError) as exc:
        return BenchmarkSegment("export_encode", "skipped", reason=f"PyAV/RGBA input unavailable: {exc}")

    values: list[float] = []
    hashes: dict[str, str] = {}
    for _ in range(iterations):
        output = BytesIO()
        started = time.perf_counter()
        try:
            container = av.open(output, mode="w", format="mp4")
            stream = container.add_stream(
                "mpeg4", rate=Fraction(scene.fps_num, scene.fps_den))
            stream.width = scene.width
            stream.height = scene.height
            stream.pix_fmt = "yuv420p"
            for rgba in rendered_frames:
                array = np.frombuffer(rgba.data, dtype=np.uint8).reshape((rgba.height, rgba.width, 4))
                video_frame = av.VideoFrame.from_ndarray(array, format="rgba")
                for packet in stream.encode(video_frame):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
            container.close()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return BenchmarkSegment("export_encode", "skipped", reason=f"PyAV encode unavailable: {exc}")
        values.append((time.perf_counter() - started) * 1000.0)
        _record_hash(hashes, "encoded", output.getvalue())
    return BenchmarkSegment(
        "export_encode", "passed", tuple(values), output_hashes=tuple(sorted(hashes.items())))


def _record_hash(output: dict[str, str], key: str, data: bytes) -> None:
    digest = hashlib.sha256(data).hexdigest()
    previous = output.get(key)
    if previous is not None and previous != digest:
        raise RuntimeError(f"benchmark output changed between iterations: {key}")
    output[key] = digest


def _hash_json(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _process_rss_bytes() -> int | None:
    try:
        import psutil
        return int(psutil.Process(os.getpid()).memory_info().rss)
    except (ImportError, OSError):
        return None


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        raise ValueError("cannot calculate a percentile without values")
    if not 0.0 <= percentile <= 1.0:
        raise ValueError("percentile must be between 0 and 1")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = percentile * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


__all__ = [
    "BenchmarkFixture", "BenchmarkReport", "BenchmarkSegment", "build_reference_scene",
    "measure_segments", "run_benchmark",
]
