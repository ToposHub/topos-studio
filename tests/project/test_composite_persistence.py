"""Round-trip persistence for a standalone image's composite graph.

Regression for the broken single-asset composite round-trip: ``composite/graphs.json``
must be an id-keyed mapping ``{composition_id: graph}`` (what the reader in
``topos_project_read_service`` expects), NOT the raw graph dict. A user's edits
on image A must survive a save → reload so per-image composite isolation works.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.color_node import COLORNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.transform_node import TransformNode
from src.shared.project.save_router import SaveRouter
from src.shared.project.topos_project import ToposProject
from src.shared.project.topos_project_io import ToposProjectIO


def _graph_with_user_edits() -> NodeGraph:
    """A non-default graph: COLOR -> Transform(scale_x=1.5) -> Output."""
    g = NodeGraph()
    color = COLORNode(); color.position = (100, 200)
    xform = TransformNode(); xform.position = (300, 200)
    xform.set_parameter("scale_x", 1.5)
    out = OutputNode(); out.position = (500, 200)
    g.add_node(color); g.add_node(xform); g.add_node(out)
    g.connect(color.id, "result", xform.id, "image")
    g.connect(xform.id, "result", out.id, "image")
    return g


def test_standalone_composite_graph_round_trips():
    """A graph saved as {composite_id: graph} restores with nodes, connections,
    and the user's parameter edit intact."""
    router = SaveRouter()
    graph_data = _graph_with_user_edits().to_dict()
    composite_id = "standalone_composite"

    with tempfile.TemporaryDirectory() as tmpdir:
        image_path = os.path.join(tmpdir, "image.png")
        project_path = os.path.join(tmpdir, "image.topos")
        open(image_path, "wb").close()

        project = ToposProject.create_single_asset(
            name="Image", asset_path=image_path, media_type="image")

        # RC3 format: id-keyed mapping (what _save_single_asset_project now writes).
        assert ToposProjectIO.save(
            project=project,
            filepath=project_path,
            full_compositions_data={composite_id: graph_data},
        )

        result = router.load_single_asset_data(project_path)
        assert result is not None
        restored = result.get("composite_graph")
        assert restored is not None, "composite graph was not restored"

        # Topology preserved
        assert len(restored.get("nodes", [])) == 3
        assert len(restored.get("connections", [])) == 2

        # The user's edit survived the round-trip
        def params_of(node_dict):
            return node_dict.get("parameters", {})

        xform = next(
            n for n in restored["nodes"]
            if params_of(n).get("scale_x") is not None)
        assert params_of(xform)["scale_x"] == 1.5


def test_raw_graph_dict_round_trip_was_broken():
    """Document the OLD broken behaviour: writing the raw graph dict (not
    id-keyed) yields a malformed restore. This is what RC3 fixed — the raw dict
    is NOT a valid compositions mapping, so the reader finds no real graph."""
    router = SaveRouter()
    graph_data = _graph_with_user_edits().to_dict()

    with tempfile.TemporaryDirectory() as tmpdir:
        image_path = os.path.join(tmpdir, "image.png")
        project_path = os.path.join(tmpdir, "image.topos")
        open(image_path, "wb").close()

        project = ToposProject.create_single_asset(
            name="Image", asset_path=image_path, media_type="image")

        # OLD (broken) format: raw graph dict passed straight through.
        ToposProjectIO.save(
            project=project,
            filepath=project_path,
            full_compositions_data=graph_data,
        )

        result = router.load_single_asset_data(project_path)
        restored = (result or {}).get("composite_graph")
        # The reader takes the first dict value of compositions; with the raw
        # graph dict that is a stray entry (e.g. the schema version), NOT a graph
        # with nodes — so the restore is not a usable composite graph.
        is_real_graph = bool(restored and restored.get("nodes"))
        assert is_real_graph is False, (
            "raw-dict format unexpectedly yielded a valid graph — reader changed?")
