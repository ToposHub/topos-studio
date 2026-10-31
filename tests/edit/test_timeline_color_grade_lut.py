"""Exactness checks for the timeline export's cached 8-bit grade LUT path."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from src.features.edit.core.timeline_export import TimelineExporter
from src.features.color.core.the_decompiler import GradingMath


def _exporter_for_grade(grading_params: dict) -> tuple[TimelineExporter, object]:
    stack = SimpleNamespace(
        grading_params=grading_params,
        curves_data={},
        hsl_curve_data={},
        mixer_params={},
        lut_path=None,
        soft_clip_params={"enabled": False},
        layers=[],
        active_layer_index=None,
    )
    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._project = SimpleNamespace(color_stacks={"stack": stack})
    exporter._color_grade_lut_cache = {}
    layer = SimpleNamespace(
        clip=SimpleNamespace(color_stack_id="stack"),
        clip_id="clip",
        source_frame=0,
    )
    return exporter, layer


def _reference_bgr(frame: np.ndarray, grading_params: dict) -> np.ndarray:
    rgb = frame[..., ::-1].astype(np.float32) / 255.0
    graded = GradingMath.apply_grading(
        rgb.reshape(-1, 3), grading_params,
    ).reshape(rgb.shape)
    return np.rint(
        np.clip(graded[..., ::-1], 0.0, 1.0) * 255.0,
    ).astype(np.uint8)


def test_cached_lut_matches_float_grade_exactly_for_real_project_wheels() -> None:
    """The project uses independent Lift/Gain wheels; every 8-bit code must match."""
    project_grades = (
        {
            "lift": [250.67947667772825, 0.7041904101271046, 0],
            "gamma": [0, 0, 0],
            "gain": [0, 0, 0],
            "exposure": 0.0,
            "temp": 0.0,
            "tint": 0.0,
            "contrast": 0.0,
            "shadow": 0.0,
            "highlights": 0.0,
            "saturation": 0.0,
            "vibrance": 0.0,
            "hue_shift": 0.0,
        },
        {
            "lift": [0, 0, 0],
            "gamma": [0, 0, 0],
            "gain": [49.360623248919055, 0.375193068518081, 0],
            "exposure": 0.0,
            "temp": 0.0,
            "tint": 0.0,
            "contrast": 0.0,
            "shadow": 0.0,
            "highlights": 0.0,
            "saturation": 0.0,
            "vibrance": 0.0,
            "hue_shift": 0.0,
        },
    )
    all_codes = np.arange(256, dtype=np.uint8)
    ramp = np.stack((all_codes, all_codes[::-1], all_codes), axis=-1)[None]
    random = np.random.default_rng(42).integers(
        0, 256, size=(32, 256, 3), dtype=np.uint8,
    )
    frame = np.concatenate((ramp, random), axis=0)

    for grading_params in project_grades:
        exporter, layer = _exporter_for_grade(grading_params)
        expected = _reference_bgr(frame, grading_params)
        actual = exporter._apply_color_grade(frame, layer)

        assert actual.dtype == np.uint8
        np.testing.assert_array_equal(actual, expected)
        assert len(exporter._color_grade_lut_cache) == 1


def test_cross_channel_controls_keep_float_grade_path() -> None:
    grading_params = {
        "lift": [4.0, 0.0, -3.0],
        "gamma": [0.0, 0.0, 0.0],
        "gain": [0.0, 0.0, 0.0],
        "highlights": 2.0,
        "vibrance": 0.0,
        "hue_shift": 0.0,
        "saturation": 0.0,
    }
    exporter, layer = _exporter_for_grade(grading_params)
    frame = np.random.default_rng(7).integers(
        0, 256, size=(8, 16, 3), dtype=np.uint8,
    )

    actual = exporter._apply_color_grade(frame, layer)

    np.testing.assert_array_equal(actual, _reference_bgr(frame, grading_params))
    assert not exporter._color_grade_lut_cache


# ======================================================================
# 2026-09-17 任务③：saturation 分解快路（LUT + cv2.transform 矩阵）
# ======================================================================

def test_decomposed_fastpath_matches_float_path_within_1lsb():
    """saturation 线性分解（LUT 前缀 + 3×3 矩阵）与通用浮点路径
    逐像素 |diff| ≤ 1 LSB。"""
    ex = TimelineExporter.__new__(TimelineExporter)
    ex._color_grade_lut_cache = {}
    params = {
        "exposure": 0.25, "contrast": 0.15, "saturation": 0.2,
        "gain": (0.05, 0.0, -0.05), "lift": (0.0, 0.02, 0.0),
        "gamma": (0.0, 0.0, -0.03), "shadow": 5.0, "temp": 3.0,
    }
    decomposed = ex._get_decomposed_grade_fastpath(params)
    assert decomposed is not None
    lut, sat_matrix = decomposed
    assert lut is not None and sat_matrix is not None

    rng = np.random.default_rng(7)
    frame = rng.integers(0, 256, (36, 64, 3), dtype=np.uint8)

    import cv2
    # The prefix stays floating point until saturation has consumed HDR and
    # negative responses. Quantize only at the delivery boundary.
    fast = np.rint(np.clip(
        cv2.transform(cv2.LUT(frame, lut), sat_matrix), 0.0, 255.0
    )).astype(np.uint8)

    rgb = frame[..., :3][..., ::-1].astype(np.float32) / 255.0
    graded = GradingMath.apply_grading(
        rgb.reshape(-1, 3), params).reshape(rgb.shape)
    reference = frame.copy()
    reference[..., :3] = np.rint(
        np.clip(graded[..., ::-1], 0.0, 1.0) * 255.0).astype(np.uint8)

    diff = np.abs(fast.astype(np.int16) - reference.astype(np.int16))
    assert int(diff.max()) <= 1


def test_decomposed_fastpath_declines_nonlinear_coupled_params():
    ex = TimelineExporter.__new__(TimelineExporter)
    ex._color_grade_lut_cache = {}
    assert ex._get_decomposed_grade_fastpath({"vibrance": 10.0}) is None
    assert ex._get_decomposed_grade_fastpath({"highlights": 5.0}) is None
    assert ex._get_decomposed_grade_fastpath({"hue_shift": 3.0}) is None
    # 纯 saturation 极端值仍在快路内
    result = ex._get_decomposed_grade_fastpath({"saturation": -100.0})
    assert result is not None and result[1] is not None
