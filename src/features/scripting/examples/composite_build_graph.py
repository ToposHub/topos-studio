# Composite: build a node graph from scratch (C4 NODE_OPS)
#
# Builds  constant -> transform -> blur -> output  in ONE plan, so the
# whole topology change lands as a single undo group. Run this in the
# scripting panel with a composite page open.

result = topos.composite.apply({
    "plan_id": "script-build-graph",
    "operations": [
        {"op": "add_node", "node_type": "constant", "node_id": "src",
         "position": [120, 300],
         "parameters": {"width": 1920, "height": 1080}},
        {"op": "add_node", "node_type": "transform", "node_id": "xf",
         "position": [420, 300]},
        {"op": "add_node", "node_type": "blur", "node_id": "bl",
         "position": [720, 300]},
        {"op": "connect", "from_node": "src", "from_port": "result",
         "to_node": "xf", "to_port": "image"},
        {"op": "connect", "from_node": "xf", "from_port": "result",
         "to_node": "bl", "to_port": "image"},
        {"op": "connect", "from_node": "bl", "from_port": "result",
         "to_node": "output-1", "to_port": "image"},
    ],
})

if result["applied"]:
    topos.print(f"graph built; generation {result['generation_before']}"
                f" -> {result['generation_after']}")
else:
    topos.print(f"plan rejected: {result['error']}")
    for err in result["report"]["errors"]:
        topos.print(f"  - {err['message']}")
