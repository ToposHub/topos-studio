"""Measure completed GL graph frames; excludes decode, presentation and encoding."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import resource
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def measure(width: int, height: int, seconds: float) -> dict[str, Any]:
    import moderngl
    import numpy as np
    from src.features.color.core.color_graph_migration import migrate_legacy_stack
    from src.features.color.core.color_graph_texture import ColorGraphTextureRenderer
    from src.features.color.core.grade_keyframes import GradeKeyframe, GradeKeyframeTrack
    from src.features.color.core.layer import Layer
    from src.shared.project.color_stack import ColorStackData

    layers = [Layer("Animated exposure"), Layer("Animated contrast")]
    for layer, param, start, end in ((layers[0], "exposure", 0.0, 30.0),
                                     (layers[1], "contrast", 0.0, 30.0)):
        layer.grade_keyframes[param] = GradeKeyframeTrack(param, [GradeKeyframe(0, start), GradeKeyframe(1499, end)])
    stack = ColorStackData.create(asset_id="benchmark", name="Animation benchmark").with_layers(
        [layer.to_dict(None) for layer in layers])
    graph = migrate_legacy_stack(stack)
    ctx = moderngl.create_standalone_context(require=330)
    renderer = ColorGraphTextureRenderer(ctx)
    source = ctx.texture((width, height), 3, np.full((height, width, 3), 0.25, "f4").tobytes(), dtype="f4")
    samples: list[float] = []
    try:
        for frame in range(3):
            renderer.render(source, graph, frame)
            ctx.finish()
        started = time.perf_counter()
        while time.perf_counter() - started < seconds:
            before = time.perf_counter()
            renderer.render(source, graph, len(samples) % 1500)
            ctx.finish()
            samples.append(time.perf_counter() - before)
        elapsed = time.perf_counter() - started
        return {
            "scope": "completed GL graph frames only; no decode/presentation/encode",
            "renderer": ctx.info.get("GL_RENDERER"), "size": [width, height],
            "animated_correctors": 2, "dtype": "f4", "elapsed_seconds": round(elapsed, 3),
            "frames": len(samples), "fps": round(len(samples) / elapsed, 3),
            "p50_ms": round(float(np.percentile(samples, 50)) * 1000, 3),
            "p95_ms": round(float(np.percentile(samples, 95)) * 1000, 3),
            "over_60fps_budget_frames": sum(sample > 1 / 60 for sample in samples),
            "output_slots": len(renderer._outputs), "plan_cache_entries": len(renderer.plan_cache),
            "published_leases": len(renderer.executor._published),
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        }
    finally:
        renderer.release()
        source.release()
        ctx.release()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=3840)
    parser.add_argument("--height", type=int, default=2160)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.width, args.height, args.seconds) <= 0:
        parser.error("size and duration must be positive")
    # The context is created, used and released on the same worker thread.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="KeyframeBenchmark") as worker:
        result = worker.submit(measure, args.width, args.height, args.seconds).result()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
