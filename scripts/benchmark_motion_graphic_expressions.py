#!/usr/bin/env python3
"""Measure safe expression evaluation and emit cross-process frame hashes."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.features.teaching_animation.core.contracts import AnimationFrame, RenderContext
from src.features.teaching_animation.core.model import (
    SceneElement,
    SceneExpression,
    SceneTransform,
    TeachingSceneDocument,
)
from src.features.teaching_animation.motion.expression import (
    ExpressionContext,
    ExpressionError,
    evaluate_expression,
    validate_expression,
)
from src.features.teaching_animation.render.svg_renderer import SvgSceneRenderer


def expression_scene() -> TeachingSceneDocument:
    element = SceneElement(
        element_id="expression-title",
        type="text",
        transform=SceneTransform(x=12.0, y=8.0, width=180.0, height=30.0),
        style={"color": "#f9fafb", "font_size": 18.0},
        content="Expression Golden",
    )
    return TeachingSceneDocument(
        scene_id="expression-cross-process-v1",
        schema_version=2,
        name="Expression Golden",
        width=240,
        height=120,
        fps_num=24,
        fps_den=1,
        duration_frames=24,
        background={"color": "#111827"},
        elements=(element,),
        animations=(),
        expressions=(SceneExpression(
            expression_id="title-x",
            element_id=element.element_id,
            property="x",
            source="12.0 + sin(frame / 3.0) * 8.0 + seed",
            seed=7,
        ),),
    )


def render_hashes(frames: tuple[int, ...]) -> dict[str, str]:
    scene = expression_scene()
    renderer = SvgSceneRenderer()
    renderer.prepare(scene, RenderContext(fps=scene.fps, width=scene.width, height=scene.height))
    try:
        import hashlib

        return {
            str(frame): hashlib.sha256(
                renderer.render_frame(AnimationFrame(frame, scene.fps)).data
            ).hexdigest()
            for frame in frames
        }
    finally:
        renderer.cleanup()


def expression_stats(frame: int) -> tuple[dict[str, object], ...]:
    sources = (
        "12.0 + sin(frame / 3.0) * 8.0 + seed",
        "+".join("1.0" for _ in range(250)),
    )
    stats: list[dict[str, object]] = []
    for source in sources:
        started = time.perf_counter()
        error = ""
        try:
            validate_expression(source)
            evaluate_expression(source, ExpressionContext(frame=frame, seed=7))
        except ExpressionError as exc:
            error = str(exc)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        try:
            node_count = sum(1 for _ in ast.walk(ast.parse(source, mode="eval")))
        except SyntaxError:
            node_count = -1
        stats.append({
            "source_length": len(source),
            "node_count": node_count,
            "elapsed_ms": round(elapsed_ms, 4),
            "within_budget": not bool(error),
            "budget_exceeded": "budget" in error or "range" in error,
            "error": error,
        })
    return tuple(stats)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", default="0,1,3,7,12,23")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()
    frames = tuple(int(item) for item in args.frames.split(",") if item.strip())
    if not frames or any(frame < 0 or frame >= 24 for frame in frames):
        parser.error("--frames must contain values in [0, 24)")
    payload: dict[str, object] = {
        "schema_version": 1,
        "scene_id": expression_scene().scene_id,
        "frames": list(frames),
        "frame_hashes": render_hashes(frames),
    }
    if args.stats:
        payload["expression_stats"] = list(expression_stats(frames[0]))
    encoded = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
