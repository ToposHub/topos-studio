#!/usr/bin/env python3
"""Generate a fixed SVG preview/export consistency and provenance manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from dataclasses import replace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.features.teaching_animation.core.contracts import FrameContext, ProxyTier, RenderContext
from src.features.teaching_animation.core.html_layer import build_html_layer
from src.features.teaching_animation.render.benchmark import (
    BenchmarkFixture,
    build_reference_scene,
    measure_segments,
)
from src.features.teaching_animation.render.consistency import ComparisonMode
from src.features.teaching_animation.render.release_manifest import (
    build_release_manifest,
    compare_rendered_frame_sequences,
)
from src.features.teaching_animation.render.v2_adapter import SvgSceneRendererV2


def _render(scene, frames: tuple[int, ...], proxy: ProxyTier):
    renderer = SvgSceneRendererV2()
    session = renderer.prepare(scene, FrameContext.for_scene(scene, frames[0], proxy_tier=proxy))
    results = []
    try:
        for frame in frames:
            results.append(renderer.render(
                session, FrameContext.for_scene(scene, frame, proxy_tier=proxy)))
    finally:
        renderer.close(session)
    return tuple(results)


def _manifest_context(scene, profile: str) -> RenderContext:
    return RenderContext(
        fps=scene.fps,
        width=scene.width,
        height=scene.height,
        backend="svg",
        render_profile=profile,
        renderer_version="teaching-animation-svg/0.2",
    )


def _render_html(scene, frames: tuple[int, ...]):
    from src.features.teaching_animation.render.html_renderer_service import HtmlRendererService
    from src.features.teaching_animation.render.html_renderer_spike.playwright_backend import (
        PlaywrightChromiumBackend,
    )
    from src.features.teaching_animation.render.html_scene_renderer_v2 import HtmlSceneRendererV2

    probe = PlaywrightChromiumBackend()
    if not probe.is_available():
        reason = f"pinned Chromium unavailable ({probe.backend_build})"
        probe.close()
        return None, reason
    probe.close()
    html = (
        "<html><head><style>html,body{margin:0;background:#111827}"
        ".box{position:absolute;left:8%;top:20%;width:36%;height:45%;"
        "background:#2563eb;animation:slide 1s linear infinite}"
        ".label{position:absolute;left:8%;top:72%;color:#f9fafb;font-size:32px}"
        "@keyframes slide{0%{transform:translateX(0)}100%{transform:translateX(80px)}}"
        "</style></head><body><div class='box'></div><div class='label'>Benchmark</div></body></html>"
    )
    layer = build_html_layer(
        layer_id="release-html",
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
    rendered = []
    try:
        session = renderer.prepare(
            html_scene,
            FrameContext.for_scene(html_scene, frames[0], proxy_tier=ProxyTier.FINAL),
        )
        try:
            for frame in frames:
                rendered.append(renderer.render(
                    session,
                    FrameContext.for_scene(html_scene, frame, proxy_tier=ProxyTier.FINAL),
                ))
        finally:
            renderer.close(session)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return None, f"HTML capture failed: {exc}"
    finally:
        service.close()
    return (html_scene, tuple(rendered)), ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--height", type=int, default=180)
    parser.add_argument("--frames", type=int, default=24)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--include-html", action="store_true")
    parser.add_argument("--include-gpu", action="store_true")
    args = parser.parse_args()
    if min(args.width, args.height, args.frames) < 1:
        parser.error("--width, --height and --frames must be positive")

    fixture = BenchmarkFixture(
        fixture_id="teaching-animation/release-s0-svg-v1",
        width=args.width,
        height=args.height,
        duration_frames=max(args.frames, 3),
    )
    scene = build_reference_scene(fixture)
    selected = (0, fixture.duration_frames // 2, fixture.duration_frames - 1)
    preview = _render(scene, selected, ProxyTier.PREVIEW)
    export = _render(scene, selected, ProxyTier.FINAL)
    consistency = compare_rendered_frame_sequences(
        tuple(item.frame for item in preview),
        tuple(item.frame for item in export),
        mode=ComparisonMode.STRICT,
    )
    release_passed = all(result.passed for result in consistency)
    payload = {
        "schema_version": 1,
        "fixture": fixture.to_dict() | {"fixture_hash": fixture.fixture_hash},
        "frames": list(selected),
        "consistency": [
            {
                "passed": result.passed,
                "issues": [issue.__dict__ for issue in result.issues],
                "mean_abs_diff": result.mean_abs_diff,
                "max_abs_diff": result.max_abs_diff,
            }
            for result in consistency
        ],
        "preview": build_release_manifest(
            scene, _manifest_context(scene, "preview"),
            tuple(item.evidence for item in preview),
        ).to_dict(),
        "export": build_release_manifest(
            scene, _manifest_context(scene, "final"),
            tuple(item.evidence for item in export),
        ).to_dict(),
    }
    if args.include_html:
        html_result, reason = _render_html(scene, selected)
        if html_result is None:
            payload["html"] = {"status": "skipped", "reason": reason}
        else:
            html_scene, first = html_result
            second_result, second_reason = _render_html(scene, selected)
            if second_result is None:
                payload["html"] = {"status": "skipped", "reason": second_reason}
            else:
                _second_scene, second = second_result
                html_consistency = compare_rendered_frame_sequences(
                    tuple(item.frame for item in first),
                    tuple(item.frame for item in second),
                    mode=ComparisonMode.STRICT,
                )
                html_context = RenderContext(
                    fps=scene.fps,
                    width=scene.width,
                    height=scene.height,
                    backend="html",
                    render_profile="final",
                    renderer_version="teaching-animation-html/real",
                )
                payload["html"] = {
                    "status": "passed" if all(item.passed for item in html_consistency) else "failed",
                    "consistency": [
                        {"passed": item.passed, "issues": [issue.__dict__ for issue in item.issues]}
                        for item in html_consistency
                    ],
                    "manifest": build_release_manifest(
                        html_scene, html_context, tuple(item.evidence for item in first)
                    ).to_dict(),
                }
                release_passed = release_passed and all(item.passed for item in html_consistency)
    if args.include_gpu:
        gpu_context = _manifest_context(scene, "preview")
        gpu_segment = measure_segments(
            scene, gpu_context, (selected[0],), names=("gpu_effect",), iterations=1)[0]
        payload["gpu"] = gpu_segment.to_dict()
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if release_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
