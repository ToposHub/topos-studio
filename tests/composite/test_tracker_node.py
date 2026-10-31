"""A1: Composite tracker node — data model, registration, bake math.

The analyze worker path through the real Tracker2D needs the fixture
videos from tools/make_tracking_fixtures.py (and a working native codec);
here we verify the composite-side contracts that don't need media:
node behaviour, serialization, registration, and the page-side bake
arithmetic via a stubbed graph.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_capabilities import (
    get_node_capability, is_placeholder_node_type)
from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.tracker_node import (
    TRACKER_MODES, TrackerNode, decode_track_data, encode_track_data)

pytestmark = pytest.mark.composite_core


class TestTrackerNodeBasics:

    def test_registered_and_not_placeholder(self):
        from src.features.composite.core.node_engine import NODE_REGISTRY
        assert "tracker" in NODE_REGISTRY
        assert not is_placeholder_node_type("tracker")
        cap = get_node_capability("tracker")
        assert cap is not None
        assert cap.action_type == "passthrough"

    def test_passthrough_evaluation(self):
        node = TrackerNode(node_id="trk")
        image = _fake_texture(1, 64, 48)
        out = node._do_evaluate({"image": image}, ctx=None, fb_manager=None)
        assert out["image"] is image
        assert node._do_evaluate({}, None, None) == {}

    def test_track_data_round_trip(self):
        node = TrackerNode(node_id="trk")
        payload = {
            "media_path": "/shots/plate.exr",
            "width": 1920, "height": 1080,
            "reference_frame": 10, "mode": "trs",
            "transforms": {"10": {"tx": 0.0, "ty": 0.0, "rotation": 0.0,
                                  "scale_x": 1.0, "scale_y": 1.0,
                                  "status": "valid"}},
        }
        node.set_track_data(payload)
        # stored as a parameter → serialized with the graph
        data = node.to_dict()
        node2 = TrackerNode.from_dict(data)
        assert decode_track_data(node2.parameters["track_data"])["width"] == 1920

    def test_decode_tolerates_corruption(self):
        assert decode_track_data("not json {") is None
        assert decode_track_data("") is None
        assert decode_track_data(None) is None

    def test_modes_and_aliases(self):
        assert TRACKER_MODES == ["translation", "trs", "planar"]
        aliases = " ".join(TrackerNode.SEARCH_ALIASES)
        assert "跟踪" in aliases


def _fake_texture(tex_id, w, h):
    from src.features.composite.nodes.base_node import TextureResult
    return TextureResult(texture_id=tex_id, width=w, height=h)


class TestBakeMath:
    """The page's bake arithmetic, exercised with stubbed graph/nodes."""

    def test_bake_transform_tracks(self, monkeypatch):
        page = _StubPage()
        payload = {
            "width": 1000, "height": 500, "mode": "trs",
            "reference_frame": 0,
            "transforms": {
                "0": {"tx": 0.0, "ty": 0.0, "rotation": 0.0,
                      "scale_x": 1.0, "scale_y": 1.0, "status": "valid"},
                "5": {"tx": 50.0, "ty": -25.0, "rotation": 3.0,
                      "scale_x": 1.5, "scale_y": 1.25, "status": "valid"},
                "7": {"tx": 99.0, "ty": 0.0, "rotation": 0.0,
                      "scale_x": 1.0, "scale_y": 1.0,
                      "status": "failed"},  # skipped
            },
        }
        page._tracker.set_track_data(payload)
        page._on_tracker_bake_requested("trk", "transform")

        target = page._transform
        assert "translate_x" in target.keyframe_tracks
        tx = target.keyframe_tracks["translate_x"]
        values = {kf["frame"]: kf["value"] for kf in tx["keyframes"]}
        assert values == {0: 0.0, 5: 50.0}  # failed frame excluded
        rot = target.keyframe_tracks["rotation"]
        assert rot["keyframes"][1]["value"] == pytest.approx(3.0)
        # undo restores empty tracks
        page._undo_stack.undo()
        assert target.keyframe_tracks == {}

    def test_bake_cornerpin_corners(self, monkeypatch):
        page = _StubPage()
        # identity homography on a 1000x500 source
        ident = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        shift = [[1.0, 0.0, 10.0], [0.0, 1.0, 5.0], [0.0, 0.0, 1.0]]
        payload = {
            "width": 1000, "height": 500, "mode": "planar",
            "reference_frame": 0,
            "transforms": {
                "0": {"tx": 0.0, "ty": 0.0, "rotation": 0.0,
                      "scale_x": 1.0, "scale_y": 1.0,
                      "status": "valid", "matrix": shift},
                "1": {"tx": 0.0, "ty": 0.0, "rotation": 0.0,
                      "scale_x": 1.0, "scale_y": 1.0,
                      "status": "valid", "matrix": ident},
            },
        }
        page._tracker.set_track_data(payload)
        page._on_tracker_bake_requested("trk", "cornerpin")

        pin = page._cornerpin
        to1 = pin.keyframe_tracks["to1_x"]["keyframes"]
        values = {kf["frame"]: kf["value"] for kf in to1}
        # TL corner (0,0) shifted by (10,5) px → normalized /1000, /500
        assert values[0] == pytest.approx(10.0 / 1000.0)
        assert values[1] == pytest.approx(0.0)
        to2 = {kf["frame"]: kf["value"]
               for kf in pin.keyframe_tracks["to2_x"]["keyframes"]}
        assert to2[0] == pytest.approx((1000.0 + 10.0) / 1000.0)
        from1 = {kf["frame"]: kf["value"]
                 for kf in pin.keyframe_tracks["from1_x"]["keyframes"]}
        assert from1[0] == pytest.approx(0.0)


class _StubNode:
    def __init__(self, node_id, node_type):
        self.id = node_id
        self.node_type = node_type
        self.parameters = {}
        self.keyframe_tracks = {}
        self.param_revision = 0

    def set_parameter(self, name, value):
        self.parameters[name] = value
        self.param_revision += 1

    def get_track_data(self):
        from src.features.composite.nodes.tracker_node import decode_track_data
        return decode_track_data(self.parameters.get("track_data"))

    def set_keyframe_track(self, param_name, track):
        self.keyframe_tracks[param_name] = {
            "param_name": param_name,
            "keyframes": [kf.to_dict() for kf in track.keyframes],
            "min_val": track.min_val, "max_val": track.max_val,
        }

    def get_param_defs(self):
        # generous ranges so pixel values are not clamped
        class P:
            def __init__(self, lo, hi):
                self.min_val, self.max_val = lo, hi
        return {n: P(-1e6, 1e6) for n in
                ("translate_x", "translate_y", "rotation", "scale_x",
                 "scale_y", "to1_x", "to1_y", "to2_x", "to2_y",
                 "to3_x", "to3_y", "to4_x", "to4_y",
                 "from1_x", "from1_y", "from2_x", "from2_y",
                 "from3_x", "from3_y", "from4_x", "from4_y")}

    def bump_output_revision(self):
        pass


class _StubUndo:
    def __init__(self):
        self.stack = []

    def push(self, action):
        self.stack.append(action)

    def undo(self):
        action = self.stack.pop()
        action.undo_fn(action.data)


class _StubPage:
    """Minimal facade exposing the bake handler's dependencies."""

    def __init__(self):
        from src.features.composite.nodes.transform_node import TransformNode
        from src.features.composite.nodes.cornerpin_node import CornerPinNode
        self._tracker = TrackerNode(node_id="trk")
        self._transform = TransformNode(node_id="xf")
        self._cornerpin = CornerPinNode(node_id="pin")
        self._graph_nodes = {
            "trk": self._tracker, "xf": self._transform, "pin": self._cornerpin}
        self._node_graph = self
        self._graph_conns_out = {
            "trk": [_Conn("trk", "image", "xf", "input"),
                    _Conn("xf", "result", "pin", "input")],
        }
        self._undo_stack = _StubUndo()

        from src.features.composite.ui import composite_page as cp
        self._on_tracker_bake_requested = (
            cp.CompositePage._on_tracker_bake_requested.__get__(self))
        self._find_downstream_node = (
            cp.CompositePage._find_downstream_node.__get__(self))

    def get_node(self, node_id):
        return self._graph_nodes.get(node_id)

    def get_output_connections(self, node_id):
        return self._graph_conns_out.get(node_id, [])

    def _show_tracker_message(self, text):
        self.message = text

    def _emit_user_edit_once(self):
        pass

    def _refresh_properties_panel(self, node_id):
        pass

    def _evaluate_graph(self):
        pass


class _Conn:
    def __init__(self, src, src_port, dst, dst_port):
        self.src_node_id, self.src_port_name = src, src_port
        self.dst_node_id, self.dst_port_name = dst, dst_port


class TestImportFromColor:
    """A1.3: ColorFacade export → tracker import handler."""

    def test_facade_export_shape(self):
        """The boundary function serializes bindings into tracker payloads."""
        from types import SimpleNamespace

        class _Store:
            def all_bindings(self):
                return [SimpleNamespace(
                    binding_id="b1", shape_name="eye", clip_id="clip0",
                    reference_frame=3,
                    mode=SimpleNamespace(value="trs"),
                    transforms={4: SimpleNamespace(
                        translation=(2.0, -1.0), rotation=0.5,
                        scale=(1.1, 0.9), status=SimpleNamespace(value="valid"))},
                    manual_corrections={},
                )]

        from src.features.color.facade import ColorFacade
        facade = ColorFacade()
        facade._main_window = SimpleNamespace(engine=SimpleNamespace(
            tracking_store=_Store()))
        payloads = facade.export_tracking_bindings()
        assert len(payloads) == 1
        p = payloads[0]
        assert p["binding_id"] == "b1"
        assert p["mode"] == "trs"
        assert p["transforms"]["4"]["tx"] == pytest.approx(2.0)
        assert p["transforms"]["4"]["status"] == "valid"

    def test_facade_export_empty_when_no_store(self):
        from src.features.color.facade import ColorFacade
        assert ColorFacade().export_tracking_bindings() == []
