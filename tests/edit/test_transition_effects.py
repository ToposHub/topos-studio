from __future__ import annotations

import numpy as np

from src.features.edit.core.transition_effects import (
    apply_full_frame_transition,
    apply_transition_effect,
    apply_transition_motion_blur,
)


def test_motion_blur_transition_is_inert_at_endpoints_and_changes_midpoint():
    frame = np.zeros((32, 48, 4), dtype=np.uint8)
    frame[:, 12:16, :3] = (20, 120, 240)
    frame[..., 3] = 177

    start = apply_transition_motion_blur(frame, 0.0, "motion_blur_left")
    middle = apply_transition_motion_blur(frame, 0.5, "motion_blur_left")
    end = apply_transition_motion_blur(frame, 1.0, "motion_blur_left")

    assert np.array_equal(start, frame)
    assert np.array_equal(end, frame)
    assert not np.array_equal(middle[..., :3], frame[..., :3])
    assert np.array_equal(middle[..., 3], frame[..., 3])


def test_whip_pan_accepts_float_frames_and_bad_parameters():
    frame = np.linspace(0.0, 1.0, 24 * 40 * 3, dtype=np.float32).reshape(24, 40, 3)
    output = apply_transition_motion_blur(
        frame,
        0.5,
        "whip_pan_right",
        {"blur_radius": "not-a-number", "shutter": 2.0},
    )

    assert output.shape == frame.shape
    assert output.dtype == frame.dtype
    assert np.isfinite(output).all()


def test_radial_and_rgb_split_transitions_change_only_the_source_colour_data():
    frame = np.zeros((36, 60, 4), dtype=np.uint8)
    frame[8:28, 18:42, :3] = (30, 130, 240)
    frame[..., 3] = 211

    for transition_type in (
        "zoom_blur_in", "zoom_blur_out", "spin_blur_cw", "spin_blur_ccw",
        "rgb_split_left", "rgb_split_right",
    ):
        output = apply_transition_effect(frame, 0.5, transition_type)
        assert output.shape == frame.shape
        assert output.dtype == frame.dtype
        assert not np.array_equal(output[..., :3], frame[..., :3])
        assert np.array_equal(output[..., 3], frame[..., 3])


def test_full_frame_blur_dissolves_have_no_geometric_mask_boundary():
    first = np.zeros((40, 64, 4), dtype=np.uint8)
    second = np.zeros_like(first)
    first[..., :3] = (20, 80, 220)
    second[..., :3] = (220, 80, 20)
    first[..., 3] = 177
    second[..., 3] = 211

    for transition_type in (
        "blur_dissolve", "defocus_dissolve", "film_dissolve",
    ):
        start = apply_full_frame_transition(first, second, 0.0, transition_type)
        middle = apply_full_frame_transition(first, second, 0.5, transition_type)
        end = apply_full_frame_transition(first, second, 1.0, transition_type)

        assert np.array_equal(start, first)
        assert np.array_equal(end, second)
        assert not np.array_equal(middle[..., :3], first[..., :3])
        assert middle.shape == first.shape
        assert middle.dtype == first.dtype


def test_smooth_cut_is_content_driven_and_preserves_endpoints():
    first = np.zeros((48, 72, 4), dtype=np.uint8)
    second = np.zeros_like(first)
    first[12:32, 18:38, :3] = (30, 160, 240)
    second[12:32, 28:48, :3] = (30, 160, 240)
    first[..., 3] = 190
    second[..., 3] = 190

    start = apply_full_frame_transition(first, second, 0.0, "smooth_cut")
    middle = apply_full_frame_transition(first, second, 0.5, "smooth_cut")
    end = apply_full_frame_transition(first, second, 1.0, "smooth_cut")

    assert np.array_equal(start, first)
    assert np.array_equal(end, second)
    assert middle.shape == first.shape
    assert middle.dtype == first.dtype
    assert np.array_equal(middle[..., 3], first[..., 3])
