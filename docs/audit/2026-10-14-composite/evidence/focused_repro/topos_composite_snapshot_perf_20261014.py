"""Read-only Composite snapshot benchmark, run from repository root.

Initial 2026-10-14 results were collected while the root agent ran pytest on
the same computer; rerun with competing suites stopped for final reporting.
No GPU is involved. The benchmark measures NodeGraph.snapshot() only.
"""
import json
import statistics
import time
from pathlib import Path

from src.features.composite.core.node_engine import NodeGraph, NODE_REGISTRY


def main() -> None:
    results = []
    for count, keys, expression in [
        (10, 0, False), (50, 0, False), (50, 120, False),
        (50, 120, True), (100, 120, False),
    ]:
        graph = NodeGraph()
        for index in range(count):
            node = NODE_REGISTRY["transform"](f"t{index}")
            graph.add_node(node)
            if keys:
                for parameter in ["translate_x", "translate_y", "rotation",
                                  "scale_x", "scale_y"]:
                    node.keyframe_tracks[parameter] = {
                        "param_name": parameter, "value_type": "float",
                        "min_val": -10000, "max_val": 10000,
                        "keyframes": [
                            {"frame": key * 4, "value": key * 0.1,
                             "interp": "linear"}
                            for key in range(keys)
                        ],
                    }
            if expression:
                node.set_expression("center_x", "sin(frame*0.1)*10")
        samples = []
        for frame in range(12):
            started = time.perf_counter()
            graph.snapshot(frame_number=frame)
            samples.append((time.perf_counter() - started) * 1000.0)
        measured = samples[2:]
        results.append({
            "nodes": count, "tracks": count * 5 if keys else 0,
            "keys_per_track": keys, "expression": expression,
            "median_ms": round(statistics.median(measured), 3),
            "max_ms": round(max(measured), 3), "samples_ms": measured,
        })
    output = {
        "benchmark": "NodeGraph.snapshot(), 2 warmups + 10 measured frames",
        "content": "Synthetic Transform graphs; no source decode or GPU",
        "results": results,
    }
    print(json.dumps(output, indent=2))
    Path("/tmp/topos_composite_snapshot_perf_20261014_rerun.json").write_text(
        json.dumps(output, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
