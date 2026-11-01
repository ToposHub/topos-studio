"""Independent rectangle edge softness shares preview/export math and persistence."""
from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.layer import MaskShape
from src.features.color.core.mask_shapes_cpu import rasterize_shape_cpu
from src.features.color.core.window_softness import normalize_edge_softness, rasterize_rect_edge_softness


def test_legacy_shapes_keep_their_existing_feather_path():
    shape = MaskShape.from_dict({"type": "rect", "feather": 12})
    assert shape.edge_softness is None
    assert shape.feather == 12
    assert shape.to_dict()["shape_schema_version"] == 2
    assert shape.to_dict()["edge_softness"] is None


def test_shape_edge_roundtrip_and_future_payload_preserved():
    shape = MaskShape.from_dict({"type": "rect", "shape_schema_version": 3,
                                 "edge_softness": [1.25, 2, 3, 4], "future_extension": {"x": 1}})
    data = shape.to_dict()
    assert data["shape_schema_version"] == 3
    assert data["edge_softness"] == [1.25, 2, 3, 4]
    assert data["future_extension"] == {"x": 1}
    data["future_extension"]["x"] = 3
    assert shape.to_dict()["future_extension"] == {"x": 1}


@pytest.mark.parametrize("value", [[1], [1, 2, 3, float("nan")], [1, 2, 3, float("inf")],
                                    [1, 2, 3, -1], [1, 2, 3, 101], [True, 1, 2, 3], "1,2,3,4"])
def test_invalid_edges_are_rejected(value):
    with pytest.raises(ValueError):
        normalize_edge_softness(value)
    with pytest.raises(ValueError):
        MaskShape.from_dict({"type": "rect", "edge_softness": value})


def test_cache_keys_change_when_a_single_edge_changes():
    shape = MaskShape("rect")
    shape.edge_softness = [1, 2, 3, 4]
    full, partial = shape._get_render_cache_key(200, 100, 200), shape._get_shape_cache_key(200, 100, 200)
    shape.edge_softness[2] = 5
    assert shape._get_render_cache_key(200, 100, 200) != full
    assert shape._get_shape_cache_key(200, 100, 200) != partial


def test_zero_is_hard_and_each_edge_is_independent():
    hard = rasterize_rect_edge_softness(200, 100, [.5, .5], [.4, .4], 0, [0, 0, 0, 0])
    assert set(np.unique(hard)) == {0, 1}
    left_soft = rasterize_rect_edge_softness(200, 100, [.5, .5], [.4, .4], 0, [10, 0, 0, 0])
    assert 0 < left_soft[50, 55] < .5  # outside left edge only
    assert left_soft[50, 145] == 0    # right edge remains hard
    assert left_soft[25, 100] == 0    # top remains hard
    assert left_soft[50, 100] == 1


def test_softness_width_tracks_image_width_and_rotation_tracks_own_center():
    a = rasterize_rect_edge_softness(200, 100, [.3, .5], [.4, .4], 0, [10, 0, 0, 0])
    b = rasterize_rect_edge_softness(400, 200, [.3, .5], [.4, .4], 0, [10, 0, 0, 0])
    # Samples near the same normalized location differ only by half-pixel phase.
    assert a[50, 20] == pytest.approx(b[100, 40], abs=.03)
    rotated = rasterize_rect_edge_softness(200, 100, [.3, .5], [.4, .4], 90, [0, 0, 0, 0])
    assert rotated[50, 60] == 1
    assert rotated[50, 100] == 0
    assert rotated[15, 60] == 1  # long horizontal axis has become vertical


def test_preview_cpu_export_match_including_postprocess(qapp):
    from src.features.color.core.image_engine import ImageEngine
    shape = MaskShape("rect")
    shape.center = [.38, .6]
    shape.size = [.45, .4]
    shape.rotation = 31
    shape.edge_softness = [2, 5, 8, 1]
    shape.feather = 50  # independent edges replace global legacy feather
    shape.blur = 3
    shape.clean_black = .3
    shape.clean_white = .3
    shape.opacity = .7
    shape.invert = True
    cpu, reason = rasterize_shape_cpu(shape, 160, 90)
    assert reason is None
    # Rasterization is CPU-only; no constructor/GL context or worker is needed.
    engine = ImageEngine.__new__(ImageEngine)
    preview = ImageEngine._render_single_shape(engine, shape, 160, 90, 160)
    np.testing.assert_allclose(cpu, preview, atol=1e-7)
