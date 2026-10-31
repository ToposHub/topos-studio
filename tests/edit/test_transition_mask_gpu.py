from __future__ import annotations

import numpy as np

from src.features.edit.core.timeline_metal_export_renderer import (
    TimelineMetalExportRenderer,
    _TRANSITION_MASK_PARAMS_DTYPE,
)
from src.features.edit.core.transition_mask_gpu import (
    GpuTransitionMaskSpec,
    build_gpu_transition_mask_spec,
)


def test_gpu_transition_spec_is_pixel_free_for_wipe_and_slide():
    wipe = build_gpu_transition_mask_spec(
        "wipe", 0.4, {"direction": "right", "softness": 0.1}
    )
    slide = build_gpu_transition_mask_spec("slide_up", 0.6, None)

    assert wipe is not None and wipe.kind == 0
    assert wipe.direction == 1
    assert wipe.softness == 0.1
    assert slide is not None and slide.direction == 2
    assert not hasattr(wipe, "mask")


def test_motion_blur_transition_reuses_directional_gpu_mask_geometry():
    motion = build_gpu_transition_mask_spec("motion_blur_left", 0.4, None)
    whip = build_gpu_transition_mask_spec("whip_pan_right", 0.6, None)

    assert (motion is not None and motion.kind == 0
            and motion.direction == 0 and motion.effect_mode == 1)
    assert (whip is not None and whip.kind == 0
            and whip.direction == 1 and whip.effect_mode == 2)


def test_radial_transition_reuses_centred_gpu_mask_geometry():
    zoom = build_gpu_transition_mask_spec("zoom_blur_in", 0.4, None)
    zoom_out = build_gpu_transition_mask_spec("zoom_blur_out", 0.4, None)
    spin = build_gpu_transition_mask_spec("spin_blur_ccw", 0.6, None)

    assert zoom is not None and zoom.kind == 1 and zoom.effect_mode == 3
    assert zoom_out is not None and zoom_out.direction == 1
    assert spin is not None and spin.kind == 1 and spin.effect_mode == 4
    assert spin.direction == 1


def test_full_frame_transition_spec_is_pixel_free():
    film = build_gpu_transition_mask_spec("film_dissolve", 0.5, None)
    smooth = build_gpu_transition_mask_spec("smooth_cut", 0.5, None)

    assert film is not None and film.kind == 4 and film.transition_mode == 2
    assert smooth is not None and smooth.kind == 4 and smooth.transition_mode == 3


def test_gpu_transition_spec_supports_polygon_and_rejects_unknown_shape():
    polygon = build_gpu_transition_mask_spec("iris_hexagon", 0.5, None)
    unknown = build_gpu_transition_mask_spec("not_a_transition", 0.5, None)

    assert polygon is not None and polygon.kind == 3
    assert len(polygon.vertices) == 6
    assert unknown is None


def test_metal_mask_params_are_small_control_data_not_a_pixel_mask():
    spec = GpuTransitionMaskSpec(
        kind=3,
        progress=0.5,
        vertices=((-1.0, -1.0), (1.0, -1.0), (0.0, 1.0)),
    )
    params = TimelineMetalExportRenderer._make_transition_mask_params(
        spec, 3840, 2160
    )

    assert params.dtype == _TRANSITION_MASK_PARAMS_DTYPE
    assert params.nbytes == _TRANSITION_MASK_PARAMS_DTYPE.itemsize
    assert params.nbytes < 1024
    assert np.allclose(params["vertices"][0, :3], spec.vertices)
    assert params["options"][0, 2] == 3
