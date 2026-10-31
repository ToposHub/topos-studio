"""A5 ML-assisted Roto service tests — synthetic masks only: contour →
bezier conversion, the 16-point cap, IoA confidence propagation stop, and
keyframe application onto a real RotoShapeNode as plain roto data.
"""

import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.ml_roto import (
    MAX_POINTS,
    apply_keyframes,
    contour_to_bezier,
    intersection_over_area,
    mask_to_keyframe,
    mask_to_path,
    propagate,
)
from src.features.composite.nodes.roto_shape_node import RotoShapeNode

pytestmark = pytest.mark.composite_core


def _rectangle_mask(h=120, w=160, x0=30, y0=25, x1=110, y1=90):
    mask = np.zeros((h, w), dtype=np.float32)
    mask[y0:y1, x0:x1] = 1.0
    return mask


def _moving_rectangle_detector(x_start=20, x_end=110, frames=(0, 20),
                               h=120, w=160, drop_after=None):
    """Detector with ground-truth translation — IoA stays high until
    ``drop_after`` (inclusive) after which it reports no detection."""
    x0s, x1s = x_start, x_start + 70
    span = (frames[1] - frames[0]) or 1

    def detect(frame, previous_mask):
        if drop_after is not None and frame > drop_after:
            return None
        t = (frame - frames[0]) / span
        x0 = x0s + (x_end - 70 - x0s + 50) * t  # drift right
        mask = np.zeros((h, w), dtype=np.float32)
        x0 = int(round(x0))
        mask[30:90, x0:x0 + 70] = 1.0
        return mask

    return detect


class TestMaskToPath:
    def test_rectangle_gives_four_corners(self):
        built = mask_to_path(_rectangle_mask())
        assert built is not None
        path, decimated = built
        assert len(path) <= MAX_POINTS
        assert len(path) == 4
        assert decimated is False
        # Normalized coordinates inside [0, 1].
        for point in path:
            for key in ("pos", "c1", "c2"):
                x, y = point[key]
                assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0

    def test_handles_are_smooth_tangents(self):
        path = contour_to_bezier([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0),
                                  (0.0, 1.0)])
        corner = path[0]
        # Handles are offset from pos along the neighbour tangent.
        assert corner["c1"][0] < corner["pos"][0] or \
            corner["c2"][0] > corner["pos"][0]
        assert corner["c1"] != corner["pos"]
        assert corner["c2"] != corner["pos"]

    def test_blob_over_cap_adapts_or_decimates(self):
        rng = np.random.default_rng(0)
        mask = rng.random((200, 200)).astype(np.float32)
        mask = (mask > 0.5).astype(np.float32)  # very noisy → many points
        built = mask_to_path(mask)
        if built is None:
            pytest.skip("noise produced no contour")
        path, decimated = built
        assert len(path) <= MAX_POINTS

    def test_empty_mask_is_none(self):
        assert mask_to_path(np.zeros((50, 50), dtype=np.float32)) is None

    def test_keyframe_serializes_to_roto_points(self):
        keyframe = mask_to_keyframe(_rectangle_mask(), frame=7)
        assert keyframe is not None
        points = json.loads(keyframe.to_points_json())
        assert len(points) == 4
        for point in points:
            assert set(point) >= {"pos", "c1", "c2", "feather"}


class TestIoA:
    def test_identical_masks_are_one(self):
        mask = _rectangle_mask()
        assert intersection_over_area(mask, mask) == 1.0

    def test_disjoint_is_zero(self):
        a = _rectangle_mask(x0=0, y0=0, x1=40, y1=40)
        b = _rectangle_mask(x0=80, y0=60, x1=120, y1=100)
        assert intersection_over_area(a, b) == 0.0

    def test_partial_overlap(self):
        a = _rectangle_mask(x0=0, y0=0, x1=40, y1=40)
        b = _rectangle_mask(x0=20, y0=0, x1=60, y1=40)
        assert 0.4 < intersection_over_area(a, b) < 0.6


class TestPropagation:
    def test_walks_and_reports_confidence(self):
        detector = _moving_rectangle_detector()
        anchor = detector(0, None)
        frames = list(range(0, 21, 5))
        result = propagate(anchor, 0, detector, frames,
                           confidence_threshold=0.6)
        assert 0 in result.keyframes
        assert result.last_trusted_forward >= 0
        # Confidence recorded on propagated frames decays or holds.
        for frame, keyframe in result.keyframes.items():
            if frame != 0:
                assert 0.0 <= keyframe.ioa <= 1.0

    def test_stops_below_confidence_threshold(self):
        # A detector that jumps the object far away: IoA collapses.
        def detector(frame, previous):
            mask = np.zeros((120, 160), dtype=np.float32)
            x0 = 10 if frame < 5 else 130
            mask[30:90, x0:x0 + 20] = 1.0
            return mask

        result = propagate(detector(0, None), 0, detector,
                           list(range(0, 10)), confidence_threshold=0.6)
        # The walk must stop at the jump, not fabricate keyframes.
        trusted = [f for f in result.keyframes if f > 0]
        assert not trusted or max(trusted) < 5

    def test_no_detection_stops_direction(self):
        detector = _moving_rectangle_detector(drop_after=6)
        result = propagate(detector(0, None), 0, detector,
                           list(range(0, 20, 2)), confidence_threshold=0.6)
        assert result.last_trusted_forward <= 6
        assert "no detection" in result.stopped_reason or \
            result.last_trusted_forward == 6


class TestApplyKeyframes:
    def test_produces_plain_editable_roto_data(self, qapp):
        node = RotoShapeNode(node_id="r1")
        keyframe = mask_to_keyframe(_rectangle_mask(), frame=5)
        assert keyframe is not None
        written = apply_keyframes(node, [keyframe])
        assert written == 1
        # points JSON now describes the generated path…
        points = json.loads(node.parameters["points"])
        assert len(points) == 4
        # …and the animated channels exist as ordinary roto channels.
        defs = node.get_param_defs()
        assert "roto_point_0_x" in defs
        assert defs["roto_point_0_x"].animated is True
        # Track carries the keyframe at the generated frame.
        track = node.keyframe_tracks.get("roto_point_0_x")
        assert track is not None
        assert any(int(kf["frame"]) == 5 for kf in track["keyframes"])

    def test_multi_frame_keyframes_are_sorted(self, qapp):
        node = RotoShapeNode(node_id="r1")
        detector = _moving_rectangle_detector()
        result = propagate(detector(0, None), 0, detector,
                           list(range(0, 11, 5)), confidence_threshold=0.3)
        apply_keyframes(node, list(result.keyframes.values()))
        track = node.keyframe_tracks.get("roto_point_0_x")
        frames = [int(kf["frame"]) for kf in track["keyframes"]]
        assert frames == sorted(frames)

    def test_serialized_data_has_no_ai_dependency(self, qapp):
        node = RotoShapeNode(node_id="r1")
        keyframe = mask_to_keyframe(_rectangle_mask(), frame=3)
        apply_keyframes(node, [keyframe])
        data = node.to_dict()
        blob = json.dumps(data)
        assert "ai" not in blob.lower().replace("detail", "")
        # The serialized node round-trips as a plain RotoShape.
        clone = RotoShapeNode.from_dict(data)
        assert json.loads(clone.parameters["points"])
