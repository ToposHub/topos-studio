"""Clip/Frame edits, correction lifecycle and channel filtering contracts."""
import copy

import numpy as np
import pytest

from src.features.color.tracking.tracking_evaluator import TrackingEvaluator, capture_base_geometry
from src.features.color.tracking.tracking_store import TrackingStore
from src.features.color.tracking.window_edit import edit_tracked_geometry, filter_motion_matrix
from src.shared.tracking.track_data import TrackingBinding
from src.shared.tracking.types import TrackStatus, TrackTransform

FRAME = (1000, 600)


def setup_binding(kind: str = "rect") -> tuple[TrackingBinding, TrackingEvaluator]:
    binding = TrackingBinding("b", "clip", "shape", source_frame_start=0,
                              source_frame_end=20, reference_frame=0)
    binding.base_geometry = capture_base_geometry({"type": kind, "center": [0.5, 0.5],
                                                 "size": [0.3, 0.3], "rotation": 0.0})
    for frame in (0, 10, 20):
        h = np.eye(3)
        h[0, 2] = frame * 10.0
        binding.apply_result(TrackTransform.from_matrix(frame, h, 1.0, TrackStatus.VALID))
    store = TrackingStore()
    store.add_binding(binding)
    return binding, TrackingEvaluator(store)


def test_repeated_frame_edits_merge_displayed_override() -> None:
    binding, evaluator = setup_binding("curve")
    binding.base_geometry["size"] = [0.0, 0.0]
    binding.base_geometry["points"] = [
        {"pos": [0.2, 0.2], "c1": [0.2, 0.2], "c2": [0.2, 0.2], "fi": 20.0},
        {"pos": [0.7, 0.7], "c1": [0.7, 0.7], "c2": [0.7, 0.7]},
    ]
    desired = evaluator.evaluate_geometry("clip", "shape", 10, FRAME)
    desired["points"][0]["pos"][0] += 0.05
    edit_tracked_geometry(binding, desired, 10, FRAME, "frame")
    edit_tracked_geometry(binding, {"center": [0.65, 0.55]}, 10, FRAME, "frame")
    result = evaluator.evaluate_geometry("clip", "shape", 10, FRAME)
    assert result["points"][0]["pos"] == pytest.approx([0.35, 0.2])
    assert result["points"][0]["fi"] == 20.0
    assert result["center"] == [0.65, 0.55]
    assert binding.base_geometry["points"][0]["pos"] == [0.2, 0.2]


def test_clip_edit_after_frame_edit_keeps_correction_local() -> None:
    binding, evaluator = setup_binding()
    edit_tracked_geometry(binding, {"center": [0.7, 0.5]}, 10, FRAME, "frame")
    edit_tracked_geometry(binding, {"center": [0.8, 0.5]}, 10, FRAME, "clip")
    assert evaluator.evaluate_geometry("clip", "shape", 10, FRAME)["center"] == pytest.approx([0.8, 0.5])
    assert evaluator.evaluate_geometry("clip", "shape", 0, FRAME)["center"] == pytest.approx([0.6, 0.5])
    binding.clear_manual_correction(10)
    assert evaluator.evaluate_geometry("clip", "shape", 10, FRAME)["center"] == pytest.approx([0.7, 0.5])
    assert "_frame_geometry" not in binding.base_geometry


def test_clear_results_preserves_or_removes_frame_geometry_with_manual() -> None:
    binding, evaluator = setup_binding()
    edit_tracked_geometry(binding, {"center": [0.7, 0.5]}, 10, FRAME, "frame")
    binding.clear_results(keep_manual=True)
    assert evaluator.evaluate_geometry("clip", "shape", 10, FRAME)["center"] == [0.7, 0.5]
    binding.clear_results(keep_manual=False)
    assert "_frame_geometry" not in binding.base_geometry
    assert evaluator.evaluate_geometry("clip", "shape", 10, FRAME)["center"] == [0.5, 0.5]
    binding.base_geometry["_frame_geometry"] = {"10": {"center": [0.9, 0.9]}}
    assert evaluator.evaluate_geometry("clip", "shape", 10, FRAME)["center"] == [0.5, 0.5]
    assert evaluator.evaluate_geometry("clip", "shape", 21, FRAME) is None


def test_invalid_edit_does_not_write_partial_override() -> None:
    binding, _ = setup_binding()
    before = copy.deepcopy(binding.to_dict())
    with pytest.raises(ValueError):
        edit_tracked_geometry(binding, {"center": [0.1, 0.1]}, 99, FRAME, "frame")
    assert binding.to_dict() == before


def test_channel_filter_preserves_pivot_and_local_scale_under_perspective() -> None:
    matrix = np.array([[1.2, -0.2, 60.0], [0.2, 1.2, -20.0], [0.0002, -0.0001, 1.0]])
    pivot = np.array([500.0, 300.0, 1.0])
    filtered = filter_motion_matrix(matrix, {"center": [0.5, 0.5]}, FRAME,
                                    {"tx": False, "ty": False})
    mapped = filtered @ pivot
    assert mapped[:2] / mapped[2] == pytest.approx(pivot[:2])
    def derivative(h: np.ndarray) -> np.ndarray:
        p = h @ pivot
        return (h[:2, :2] - np.outer(p[:2] / p[2], h[2, :2])) / p[2]
    assert derivative(filtered) == pytest.approx(derivative(matrix))
    assert filter_motion_matrix(matrix, {}, FRAME, {}) == pytest.approx(matrix)


def test_clip_point_edit_keeps_earlier_frame_point_correction_local() -> None:
    binding, evaluator = setup_binding("curve")
    binding.base_geometry["points"] = [
        {"pos": [0.2, 0.2], "c1": [0.2, 0.2], "c2": [0.2, 0.2], "type": "corner"},
        {"pos": [0.7, 0.7], "c1": [0.7, 0.7], "c2": [0.7, 0.7], "type": "corner"},
    ]
    desired = evaluator.evaluate_geometry("clip", "shape", 10, FRAME)
    desired["points"][0]["pos"][0] += 0.1
    edit_tracked_geometry(binding, desired, 10, FRAME, "frame")
    desired = evaluator.evaluate_geometry("clip", "shape", 10, FRAME)
    desired["points"][1]["pos"][1] += 0.05
    edit_tracked_geometry(binding, {"points": desired["points"]}, 10, FRAME, "clip")
    assert evaluator.evaluate_geometry("clip", "shape", 10, FRAME)["points"] == desired["points"]
    # The new clip-wide point edit affects the reference. The old local edit does not.
    reference = evaluator.evaluate_geometry("clip", "shape", 0, FRAME)
    assert reference["points"][0]["pos"] == pytest.approx([0.2, 0.2])
    assert reference["points"][1]["pos"] == pytest.approx([0.7, 0.75])


def test_failed_frame_edit_starts_from_displayed_reference() -> None:
    binding, evaluator = setup_binding()
    binding.transforms[10] = TrackTransform.from_matrix(
        10, np.array([[1.0, 0.0, 400.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]),
        0.0, TrackStatus.FAILED)
    edit_tracked_geometry(binding, {"center": [0.6, 0.5]}, 10, FRAME, "clip")
    assert binding.base_geometry["center"] == pytest.approx([0.6, 0.5])
    assert evaluator.evaluate_geometry("clip", "shape", 10, FRAME)["center"] == pytest.approx([0.6, 0.5])


def test_clip_topology_edit_preserves_other_exact_manual_shapes() -> None:
    binding, evaluator = setup_binding("curve")
    binding.base_geometry["points"] = [
        {"pos": p, "c1": p, "c2": p, "type": "corner"}
        for p in ([0.2, 0.2], [0.7, 0.7])]
    for frame in (10, 20):
        current = evaluator.evaluate_geometry("clip", "shape", frame, FRAME)
        current["points"][0]["fi"] = float(frame)
        edit_tracked_geometry(binding, current, frame, FRAME, "frame")
    other = copy.deepcopy(evaluator.evaluate_geometry("clip", "shape", 20, FRAME))
    desired = evaluator.evaluate_geometry("clip", "shape", 10, FRAME)
    desired["points"].append({"pos": [0.4, 0.7], "c1": [0.4, 0.7], "c2": [0.4, 0.7], "type": "corner"})
    edit_tracked_geometry(binding, desired, 10, FRAME, "clip")
    assert len(evaluator.evaluate_geometry("clip", "shape", 0, FRAME)["points"]) == 3
    assert evaluator.evaluate_geometry("clip", "shape", 10, FRAME) == desired
    assert evaluator.evaluate_geometry("clip", "shape", 20, FRAME) == other
