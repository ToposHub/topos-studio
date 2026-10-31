"""Focused checks for the identity single-layer timeline export fast path."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from src.features.edit.core.timeline_export import TimelineExporter


def _exporter(frame: np.ndarray) -> TimelineExporter:
    exporter = TimelineExporter.__new__(TimelineExporter)
    exporter._project = SimpleNamespace(get_asset_by_id=lambda _asset_id: None)
    exporter._export_has_alpha = False
    exporter._gpu_render_composite = lambda *_args, **_kwargs: None
    exporter._get_output_resolution = lambda _config: (frame.shape[1], frame.shape[0])
    exporter._get_layer_frame = lambda _layer: frame
    exporter._apply_crop_cpu = lambda data, _layer: data
    exporter._apply_video_denoise_export = lambda data, _layer: data
    exporter._apply_color_grade = lambda data, _layer: data
    exporter._apply_clip_effects_cpu = lambda data, _layer, _frame: data
    exporter._resize_frame = lambda data, _width, _height: data
    return exporter


def _layer(*, opacity: float = 1.0, transform=None):
    return SimpleNamespace(
        clip_id="clip",
        asset_id="asset",
        source_frame=0,
        track_index=0,
        opacity=opacity,
        crop=(0.0, 0.0, 0.0, 0.0, 0.0),
        transform=transform or (0.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0),
        clip=SimpleNamespace(effects=(), color_stack_id=None, blend_mode="normal"),
    )


def test_single_opaque_identity_layer_skips_float32_composite() -> None:
    frame = np.random.default_rng(12).integers(
        0, 256, size=(36, 64, 3), dtype=np.uint8,
    )
    exporter = _exporter(frame)
    exporter._normalise_bgr_frame = lambda _frame: (_ for _ in ()).throw(
        AssertionError("identity single layer should not allocate float32 composite"),
    )
    exporter._quantize_export_frame = lambda _frame: (_ for _ in ()).throw(
        AssertionError("identity single layer should preserve source samples"),
    )

    result = exporter._composite_layers((_layer(),), (), object(), frame_idx=0)

    assert result is frame


def test_partial_opacity_keeps_general_float_composite() -> None:
    frame = np.full((8, 16, 3), 101, dtype=np.uint8)
    exporter = _exporter(frame)
    normalised: list[np.ndarray] = []

    def _normalise(data: np.ndarray) -> np.ndarray:
        result = TimelineExporter._normalise_bgr_frame(data)
        normalised.append(result)
        return result

    exporter._normalise_bgr_frame = _normalise
    exporter._quantize_export_frame = lambda data: (
        TimelineExporter._quantize_export_frame(exporter, data)
    )

    result = exporter._composite_layers(
        (_layer(opacity=0.5),), (), object(), frame_idx=0,
    )

    assert normalised
    assert result is not frame
    assert result.shape == frame.shape
    assert result.dtype == np.uint8
    assert np.all(result < frame)


def test_cpu_export_fallback_applies_clip_transform_after_resize() -> None:
    frame = np.full((8, 16, 3), 255, dtype=np.uint8)
    transform = (0.0, 0.0, 0.5, 0.5, 0.0, "center", 0.0, 0.0)
    layer = _layer(transform=transform)
    exporter = _exporter(frame)
    exporter._get_sequence_resolution = lambda: (16, 8)

    from src.features.edit.core.adjustment_composite import apply_transform_cpu
    expected = apply_transform_cpu(frame, transform, 16, 8, 16, 8)
    result = exporter._composite_layers((layer,), (), object(), frame_idx=0)

    np.testing.assert_array_equal(result, expected)
    assert np.count_nonzero(result) < np.count_nonzero(frame)


def test_cpu_export_applies_crop_matte_after_clip_transform() -> None:
    frame = np.arange(8 * 16 * 3, dtype=np.uint8).reshape(8, 16, 3)
    transform = (4.0, 0.0, 1.0, 1.0, 0.0, "center", 0.0, 0.0)
    layer = _layer(transform=transform)
    layer.crop = (0.25, 0.0, 0.0, 0.0, 0.0)
    exporter = _exporter(frame)
    exporter._get_sequence_resolution = lambda: (16, 8)

    crop_inputs: list[np.ndarray] = []

    def record_post_transform_crop(data: np.ndarray, _layer) -> np.ndarray:
        crop_inputs.append(data.copy())
        return data

    exporter._apply_crop_cpu = record_post_transform_crop

    from src.features.edit.core.adjustment_composite import apply_transform_cpu
    from src.features.edit.core.crop_cpu import apply_crop_cpu
    source_cropped = apply_crop_cpu(frame, layer.crop)
    expected = apply_transform_cpu(source_cropped, transform, 16, 8, 16, 8)
    exporter._composite_layers((layer,), (), object(), frame_idx=0)

    assert len(crop_inputs) == 1
    np.testing.assert_array_equal(crop_inputs[0], expected)


def test_cpu_export_keeps_lower_track_visible_around_transformed_upper_layer() -> None:
    lower = np.full((8, 16, 3), (20, 40, 60), dtype=np.uint8)
    upper = np.full((8, 16, 3), (200, 160, 120), dtype=np.uint8)
    exporter = _exporter(lower)
    exporter._get_sequence_resolution = lambda: (16, 8)
    exporter._get_layer_frame = lambda layer: (
        lower if layer.track_index == 0 else upper
    )

    upper_layer = _layer(transform=(0.0, 0.0, 0.5, 0.5, 0.0, "center", 0.0, 0.0))
    upper_layer.track_index = 1
    result = exporter._composite_layers(
        (_layer(), upper_layer), (), object(), frame_idx=0,
    )

    # The transformed upper clip occupies the centre; its transparent warp
    # border must reveal the lower track instead of becoming opaque black.
    np.testing.assert_array_equal(result[0, 0], lower[0, 0])
    assert np.all(result[4, 8] == upper[4, 8])
