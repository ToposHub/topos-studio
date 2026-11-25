"""Exactness checks for the timeline export's cached 8-bit grade LUT path."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

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
    assert ex._get_decomposed_grade_fastpath({"mid_detail": 40.0}) is None
    # 纯 saturation 极端值仍在快路内
    result = ex._get_decomposed_grade_fastpath({"saturation": -100.0})
    assert result is not None and result[1] is not None


@pytest.mark.parametrize("version", [0, 1])
def test_midtone_detail_uses_full_frames_in_preview_and_delivery(version: int) -> None:
    from src.features.edit.core.timeline_preview_renderer import PreviewRenderWorker
    from src.shared.project.color_stack import ColorStackData

    params = {"mid_detail": 40.0, "primary_calibration_version": version}
    exporter, layer = _exporter_for_grade(params)
    exporter._project.color_stacks["stack"] = ColorStackData(
        stack_id="stack", grading_params=params)
    worker = PreviewRenderWorker.__new__(PreviewRenderWorker)
    worker._asset_resolver = SimpleNamespace(_project=exporter._project)
    worker._timeline_sequence = SimpleNamespace(timeline_id="timeline")
    frame = np.full((16, 512, 4), 102, dtype=np.uint8)
    frame[:, 256:, :3] = 153
    frame[..., 3] = 203

    delivered = exporter._apply_color_grade(frame, layer)
    preview = worker._apply_preview_color_grade_unstabilized(frame, layer)
    np.testing.assert_array_equal(delivered, preview)
    np.testing.assert_array_equal(delivered[..., 3], frame[..., 3])
    np.testing.assert_array_equal(delivered[:, 200, :3], frame[:, 200, :3])
    assert np.all(delivered[:, 255, :3] < frame[:, 255, :3])
    assert np.all(delivered[:, 256, :3] > frame[:, 256, :3])
    assert not exporter._color_grade_lut_cache


def test_measured_chain_without_a_main_stack_uses_same_cpu_grade(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.edit.core import color_chain
    from src.features.edit.core.timeline_preview_renderer import PreviewRenderWorker

    params = {"primary_calibration_version": 1, "offset": (0.0, 0.0, 0.02)}
    monkeypatch.setattr(color_chain, "chain_extra_params", lambda *args, **kwargs: [
        {**params, "_chain_segment": "pre"},
    ])
    exporter, layer = _exporter_for_grade(params)
    layer.clip.color_stack_id = None
    worker = PreviewRenderWorker.__new__(PreviewRenderWorker)
    worker._asset_resolver = SimpleNamespace(_project=exporter._project)
    worker._timeline_sequence = SimpleNamespace(timeline_id="timeline")
    frame = np.array([[[62, 98, 127], [177, 120, 33]]], dtype=np.uint8)
    expected = _reference_bgr(frame, params)
    assert not np.array_equal(expected, frame)

    preview = worker._apply_preview_color_grade_unstabilized(frame, layer)
    exported = exporter._apply_color_grade_unstabilized(frame, layer)
    np.testing.assert_array_equal(preview, expected)
    np.testing.assert_array_equal(exported, expected)
    assert exporter._get_decomposed_grade_fastpath(params) is None
    with pytest.raises(ValueError, match="CPU compositor"):
        exporter._gpu_grade_uniforms(layer)


@pytest.mark.parametrize("version", [0, 1, 2])
def test_curves_only_stack_reaches_preview_and_export(version: int) -> None:
    from src.features.color.core.color_graph_cpu import apply_curves_cpu
    from src.features.color.core.layer import build_curves_lut_from_data
    from src.features.edit.core.timeline_preview_renderer import PreviewRenderWorker
    from src.shared.project.color_stack import ColorStackData

    points = {"M": [(0, 255), (128, 80), (255, 0)]}
    stack = ColorStackData.create().with_curves(points).with_curves_edit(
        {"calibration_version": version}
    )
    exporter, layer = _exporter_for_grade({})
    exporter._project.color_stacks["stack"] = stack
    worker = PreviewRenderWorker.__new__(PreviewRenderWorker)
    worker._asset_resolver = SimpleNamespace(_project=exporter._project)
    worker._timeline_sequence = SimpleNamespace(timeline_id="timeline")
    working_bgr = np.array([[[0.15, 0.09, 0.02], [0.22, 0.33, 1.4]]], dtype=np.float32)

    lut = build_curves_lut_from_data(points, calibration_version=version)
    expected = apply_curves_cpu(working_bgr[..., ::-1], lut)[..., ::-1]
    assert not np.array_equal(working_bgr, expected)
    np.testing.assert_allclose(
        worker._apply_preview_color_grade_unstabilized(working_bgr, layer), expected)
    np.testing.assert_allclose(
        exporter._apply_color_grade_unstabilized(working_bgr, layer), expected)


@pytest.mark.parametrize("dtype", [np.uint8, np.uint16])
def test_legacy_flat_lut_keeps_its_saved_strength(tmp_path: Path, dtype: type) -> None:
    from src.features.color.core.delivery_matrix import report_from_color_stacks
    from src.features.edit.core.timeline_preview_renderer import PreviewRenderWorker
    from src.shared.project.color_stack import ColorStackData

    cube = tmp_path / "black.cube"
    cube.write_text("LUT_3D_SIZE 2\n" + "0 0 0\n" * 8, encoding="utf-8")
    stack = ColorStackData(stack_id="stack", lut_path=str(cube), lut_intensity=0.5)
    exporter, layer = _exporter_for_grade({})
    exporter._project.color_stacks["stack"] = stack
    worker = PreviewRenderWorker.__new__(PreviewRenderWorker)
    worker._asset_resolver = SimpleNamespace(_project=exporter._project)
    worker._timeline_sequence = SimpleNamespace(timeline_id="timeline")
    frame = np.array([[[52, 124, 222]]], dtype=dtype)
    assert ("lut", "full") in {
        (row.operation, row.delivery) for row in report_from_color_stacks((stack,), "video").rows
    }
    np.testing.assert_array_equal(exporter._apply_color_grade_unstabilized(frame, layer), frame // 2)
    np.testing.assert_array_equal(worker._apply_preview_color_grade_unstabilized(frame, layer), frame // 2)
    cube.unlink()
    with pytest.raises(ValueError, match="color graph cpu failed"):
        exporter._apply_color_grade_unstabilized(frame, layer)


def test_rec709_measured_grade_through_tiff_delivery(tmp_path: Path) -> None:
    import cv2

    from src.features.edit.core.project import EditProject, MediaAsset
    from src.features.edit.core.timeline_export import (
        ExportCodec, TimelineExportConfig, create_timeline_exporter,
    )
    from src.features.edit.core.timeline_preview_renderer import AssetResolver, PreviewRenderWorker
    from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
    from src.shared.media.metadata import Rational
    from src.shared.project.color_stack import ColorStackData

    codes = np.empty((64, 128, 3), dtype=np.uint16)
    codes[:, :64] = [14900, 36200, 44600]
    codes[:, 64:] = [37500, 15000, 41100]
    source = tmp_path / "source.tif"
    assert cv2.imwrite(str(source), codes)
    asset = MediaAsset("asset", str(source), "image", "SDR plate", {})
    stack = ColorStackData(stack_id="stack", grading_params={
        "primary_calibration_version": 2, "saturation": 16.0,
    })
    project = EditProject("parity", "Parity", assets={asset.asset_id: asset},
                          color_stacks={stack.stack_id: stack}).with_color_management({
        "color.timeline_space": "rec709-g24", "color.output_space": "rec709-g24",
    })
    clip = ClipInstance("clip", asset.asset_id, "v0", 0, 1, 0, 1, color_stack_id=stack.stack_id)
    sequence = TimelineSequence(
        timeline_id="timeline", name="Parity", width=128, height=64, fps=Rational(24, 1),
        video_tracks=(EditTrack("v0", "V1", "video", 0, clips=(clip,)),),
    )
    _, layer = _exporter_for_grade({})
    layer.clip = clip
    worker = PreviewRenderWorker.__new__(PreviewRenderWorker)
    worker._asset_resolver = AssetResolver(project)
    worker._timeline_sequence = sequence
    preview = worker._apply_preview_color_grade_unstabilized(codes, layer)
    outcome = create_timeline_exporter(project, sequence).export_timeline(TimelineExportConfig(
        output_path=str(tmp_path / "output"), codec=ExportCodec.TIFF_SEQUENCE, bit_depth=16,
        timeline_id=sequence.timeline_id, include_audio=False, burn_subtitles=False,
    ))
    assert outcome.success, outcome.cause
    delivered = cv2.imread(str(next(tmp_path.glob("output*.tif"))), cv2.IMREAD_UNCHANGED)
    luma = codes @ np.array([0.09, 0.70, 0.21])
    expected = np.rint(codes + 0.16 * (codes - luma[..., None]))
    np.testing.assert_allclose(preview, expected, atol=1, rtol=0)
    np.testing.assert_allclose(delivered, expected, atol=1, rtol=0)
