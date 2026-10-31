# Composite: batch-update parameters across nodes (C4 NODE_OPS)
#
# One plan touches several nodes; validation is structured (unknown
# params / out-of-range values are rejected before anything mutates)
# and the whole batch is one undo group. Node ids come from a previous
# graph build (see composite_build_graph.py) or the snapshot API:
#   ids = [n["id"] for n in topos.composite.snapshot()["nodes"]]

result = topos.composite.apply({
    "plan_id": "script-batch-params",
    "operations": [
        # Single-parameter form.
        {"op": "set_param", "node_id": "xf", "param_name": "scale_x", "value": 1.25},
        # Batch form: several params on one node.
        {"op": "set_param", "node_id": "xf",
         "parameters": {"scale_y": 1.25, "rotation": -8.0}},
        # Per-node keyframes still work in the same plan.
        {"op": "add_keyframe", "node_id": "bl", "param_name": "radius",
         "frame": 0, "value": 8.0},
    ],
})

if result["applied"]:
    topos.print(f"params updated; generation {result['generation_before']}"
                f" -> {result['generation_after']}")
else:
    topos.print(f"plan rejected: {result['error']}")
    for err in result["report"]["errors"]:
        topos.print(f"  - {err['message']}")
