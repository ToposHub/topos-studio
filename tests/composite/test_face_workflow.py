"""C3 face workflow tests — landmarks→skin-mask geometry, landmark
serialization, and the two node contracts.  No models are downloaded or
required: the detector seam is injected with synthetic landmarks.
"""

import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.format import BBox
from src.features.composite.core.node_capabilities import get_node_capability
from src.features.composite.core.node_engine import NODE_CATEGORIES, NODE_REGISTRY
from src.features.composite.face_workflow import (
    build_skin_mask,
    deserialize_landmarks,
    landmarks_at,
    serialize_landmarks,
)
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.face_smooth_node import FaceSmoothNode
from src.features.composite.nodes.face_track_node import (
    FaceTrackNode,
    set_face_detector,
)

pytestmark = pytest.mark.composite_core


def _synthetic_landmarks(count=468):
    """A plausible face: ellipse of points covering the frame centre."""
    angles = np.linspace(0, 2 * np.pi, count, endpoint=False)
    points = []
    for angle in angles:
        x = 0.5 + 0.3 * np.cos(angle)
        y = 0.5 + 0.42 * np.sin(angle)
        points.append((float(x), float(y)))
    return points


class TestSkinMask:
    def test_mask_shape_and_range(self):
        mask = build_skin_mask(_synthetic_landmarks(), 160, 120,
                               feather_px=0.0)
        assert mask.shape == (120, 160)
        assert mask.dtype == np.float32
        assert float(mask.max()) <= 1.0 and float(mask.min()) >= 0.0

    def test_skin_region_nonempty_exclusions_inside(self):
        mask = build_skin_mask(_synthetic_landmarks(), 160, 120,
                               feather_px=0.0)
        # Some skin pixels exist…
        assert float(mask.sum()) > 0.0
        # …and the strict exclusion zones (interp of table indices inside
        # our synthetic ellipse) carry no skin.
        from src.features.composite.face_workflow import (
            EXCLUSION_POLYGONS, _polygon_mask)
        exclusion = _polygon_mask(EXCLUSION_POLYGONS.values(),
                                  _synthetic_landmarks(), 160, 120)
        assert float(mask[exclusion > 0].sum()) == 0.0

    def test_feather_softens_edges(self):
        hard = build_skin_mask(_synthetic_landmarks(), 160, 120,
                               feather_px=0.0)
        soft = build_skin_mask(_synthetic_landmarks(), 160, 120,
                               feather_px=8.0)
        # Feathering produces intermediate values.
        assert float(((soft > 0.05) & (soft < 0.95)).sum()) > 0
        assert hard.min() >= 0.0

    def test_insufficient_landmarks_do_not_crash(self):
        mask = build_skin_mask([(0.1, 0.1), (0.2, 0.2)], 80, 60)
        assert float(mask.sum()) == 0.0


class TestLandmarkSerialization:
    def test_roundtrip(self):
        tracks = {1: [(0.1, 0.2), (0.3, 0.4)], 7: [(0.5, 0.5)]}
        blob = serialize_landmarks(tracks, confidences={7: 0.9})
        restored = deserialize_landmarks(blob)
        assert restored[1] == [(0.1, 0.2), (0.3, 0.4)]
        assert restored[7] == [(0.5, 0.5)]
        # Confidence round-trips in the JSON.
        payload = json.loads(blob)
        assert payload["7"][0][2] == 0.9

    def test_bad_json_is_tolerant(self):
        assert deserialize_landmarks("not json") == {}
        assert deserialize_landmarks("{}") == {}

    def test_nearest_frame_lookup(self):
        tracks = {10: [(0.1, 0.1)], 20: [(0.2, 0.2)]}
        assert landmarks_at(tracks, 10) == [(0.1, 0.1)]
        assert landmarks_at(tracks, 12) == [(0.1, 0.1)]  # nearest
        assert landmarks_at(tracks, 25) == [(0.2, 0.2)]
        assert landmarks_at({}, 5) is None


class TestFaceTrackNode:
    def test_registered(self):
        assert NODE_REGISTRY.get("face_track") is FaceTrackNode
        assert any(t == "face_track"
                   for t, _ in NODE_CATEGORIES.get("AI", []))

    def test_landmarks_survive_serialization(self, qapp):
        node = FaceTrackNode(node_id="ft")
        node.set_landmarks({3: [(0.2, 0.3), (0.4, 0.5)]})
        clone = FaceTrackNode.from_dict(node.to_dict())
        assert clone.get_landmarks()[3] == [(0.2, 0.3), (0.4, 0.5)]

    def test_injected_detector_runs_per_frame(self):
        node = FaceTrackNode(node_id="ft")
        calls = []

        def detector(frame, image_array):
            calls.append(frame)
            return ([(0.1, 0.1)] * 5, 0.95)

        set_face_detector(detector)
        try:
            assert node.detect_at(9) is True
            assert node.detection_state == "OK"
            assert calls == [9]
            assert node.get_landmarks()[9][0] == (0.1, 0.1)
        finally:
            set_face_detector(None)

    def test_no_detector_visible_state(self):
        node = FaceTrackNode(node_id="ft")
        set_face_detector(None)
        assert node.detect_at(1) is False
        assert node.detection_state == "NEEDS_DETECTOR"

    def test_passthrough_without_rerun(self):
        node = FaceTrackNode(node_id="ft")
        image = TextureResult(texture_id=4, width=32, height=24,
                              bbox=BBox.full_frame(32, 24))
        out = node._do_evaluate({"image": image}, object(), None, None)
        assert out["result"].texture_id == 4


class TestFaceSmoothNode:
    def _tex(self, tex_id=8):
        return TextureResult(texture_id=tex_id, width=64, height=48,
                             revision=1, bbox=BBox.full_frame(64, 48),
                             alpha_premultiplied=False,
                             color_space="linear")

    def test_registered_as_bilateral_marker(self):
        assert NODE_REGISTRY.get("face_smooth") is FaceSmoothNode
        cap = get_node_capability("face_smooth")
        # Reuses the validated bilateral denoise shader dispatch.
        assert cap.effect_type == "denoise"
        assert cap.tile_padding == 4

    def test_marker_contract_routes_mask(self):
        node = FaceSmoothNode(node_id="fs")
        node.set_parameter("strength", 0.8)
        out = node._do_evaluate({"image": self._tex(1),
                                 "mask": self._tex(2)},
                                object(), None, None)
        assert out["_effect_type"] == "denoise"
        assert out["_effect_mask"].texture_id == 2
        assert out["_effect_params"]["strength"] == 0.8

    def test_zero_strength_is_passthrough(self):
        node = FaceSmoothNode(node_id="fs")
        node.set_parameter("strength", 0.0)
        out = node._do_evaluate({"image": self._tex(1),
                                 "mask": self._tex(2)},
                                object(), None, None)
        assert out["result"].texture_id == 1

    def test_missing_mask_warns_but_flows(self):
        node = FaceSmoothNode(node_id="fs")
        out = node._do_evaluate({"image": self._tex(1)},
                                object(), None, None)
        assert out["result"].texture_id == 1
        assert "mask" in (node.error_message or "").lower()

    def test_missing_image_emits_nothing(self):
        node = FaceSmoothNode(node_id="fs")
        assert node._do_evaluate({}, object(), None, None) == {}

    def test_serialization_roundtrip(self, qapp):
        node = FaceSmoothNode(node_id="fs")
        node.set_parameter("strength", 0.45)
        node.set_parameter("radius", 8)
        clone = FaceSmoothNode.from_dict(node.to_dict())
        assert clone.parameters["strength"] == pytest.approx(0.45)
        assert clone.parameters["radius"] == 8
