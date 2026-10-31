"""P1-11 tests for Composite viewer A/B comparison modes."""

from __future__ import annotations

import inspect


def test_viewer_compare_mode_round_trip(qapp) -> None:
    from src.features.composite.ui.composite_viewport import CompositeViewer

    viewer = CompositeViewer()
    viewer.set_split_compare_mode("difference")
    assert viewer._split_compare_mode == "difference"
    state = viewer.get_state()
    assert state["split_compare_mode"] == "difference"
    assert state["split_difference_scale"] == 4.0

    viewer.set_split_compare_mode("invalid")
    assert viewer._split_compare_mode == "wipe"
    viewer.set_state(state)
    assert viewer._split_compare_mode == "difference"
    viewer.close()


def test_viewer_compare_shader_has_display_matched_difference_heatmap() -> None:
    from src.features.composite.ui.composite_viewport import CompositeViewer

    source = inspect.getsource(CompositeViewer.initializeGL)
    assert "u_split_mode" in source
    assert "u_difference_scale" in source
    assert "before_rgb = process_display" in source
    assert "difference = abs(before_rgb - after_rgb)" in source


def test_viewer_can_clear_captured_compare_snapshot(qapp) -> None:
    from src.features.composite.ui.composite_viewport import CompositeViewer

    viewer = CompositeViewer()
    viewer._split_active = True
    viewer._secondary_tex_id = 42
    viewer.clear_split_snapshot()
    assert not viewer._split_active
    assert viewer._secondary_tex_id == 0
    viewer.close()
