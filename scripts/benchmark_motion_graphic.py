#!/usr/bin/env python3
"""Run and persist the deterministic Teaching Animation performance baseline."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.features.teaching_animation.core.contracts import RenderContext
from src.features.teaching_animation.render.benchmark import (
    BenchmarkFixture,
    build_reference_scene,
    measure_segments,
    run_benchmark,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--frames", type=int, default=24)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--profile", choices=("draft", "interactive", "preview", "final"), default="preview")
    parser.add_argument("--dpr", type=float, default=1.0)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument(
        "--segment",
        choices=("qt_rasterize", "html_capture", "gpu_effect", "export_encode", "all"),
        help="also measure optional backend stages",
    )
    parser.add_argument("--enforce", action="store_true", help="exit non-zero when the current target gate fails")
    args = parser.parse_args()
    if args.frames < 1:
        parser.error("--frames must be positive")
    if args.width < 1 or args.height < 1:
        parser.error("--width and --height must be positive")
    if args.dpr <= 0.0:
        parser.error("--dpr must be positive")

    fixture = BenchmarkFixture(
        width=args.width,
        height=args.height,
        duration_frames=max(args.frames + 1, 240),
    )
    scene = build_reference_scene(fixture)
    context = RenderContext(
        fps=fixture.fps,
        width=fixture.width,
        height=fixture.height,
        render_profile=args.profile,
        dpr=args.dpr,
    )
    frames = tuple(range(args.frames))
    report = run_benchmark(scene, context, frames, fixture=fixture, iterations=args.iterations)
    if args.segment is not None:
        names = (
            ("qt_rasterize", "html_capture", "gpu_effect", "export_encode")
            if args.segment == "all" else (args.segment,)
        )
        report = replace(report, segments=measure_segments(
            scene, context, frames, names=names, iterations=args.iterations))
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n"
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if report.gate.passed or not args.enforce else 1


if __name__ == "__main__":
    raise SystemExit(main())
