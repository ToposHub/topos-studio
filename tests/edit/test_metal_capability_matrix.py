"""Capability matrix of the Edit preview (plan 8.1, logic evidence).

One row per kind of frame, one column per configuration: the default
(``auto``), explicit Metal, explicit OpenGL, and Metal made unavailable.
Each cell states the backend that takes the frame — the single-clip Metal
preview, the Metal composite (plan 2.6), or GL — and, where it is GL under
Metal, why.  Nothing here needs a GPU: it is the decision logic, which is
the evidence a machine without Metal can give.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

pytest.importorskip("PyQt6.QtWidgets")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.edit_gpu_preview_renderer import (
    EditGpuPreviewRenderer,
    EditRenderTask,
)
from src.features.edit.core.metal_edit_compositor import (
    MetalCompositePlan,
    PlanRefusal,
    build_metal_composite_plan,
)
from src.shared.media.gpu_frame import GpuFrameRelease
from src.shared.media.frame import DecodedFrame, PlaneInfo

SINGLE = "metal_single"        # the fused one-pass kernel
COMPOSITE = "metal_composite"  # plan 2.6
GL = "gl"

DEFAULT = METAL = "all"       # metal_preview_mode() per column
OPENGL = "off"


def _frame() -> SimpleNamespace:
    return SimpleNamespace(
        pixel_buffer=object(), pixel_format="nv12", bit_depth=8, width=1920, height=1080,
        color_matrix="bt709", color_range="limited", color_primaries="bt709",
        color_transfer="bt709", release=lambda: None)


def _video(**info) -> dict:
    return {"frame_type": "vt_pixel_buffer", "vt_pixel_buffer": _frame(), "track_info": dict(info)}


def _planar() -> dict:
    y = np.zeros((8, 16), dtype=np.uint16)
    u = v = np.zeros((8, 8), dtype=np.uint16)
    planes = (y, u, v)
    infos = tuple(PlaneInfo.create(i, plane.shape[1], plane.shape[0], name, plane.dtype, 10)
                  for i, (plane, name) in enumerate(zip(planes, "YUV")))
    decoded = DecodedFrame(0, 0, 0.0, planes=planes, plane_infos=infos, pixel_format="yuv422p10le")
    return {"frame_type": "planar", "decoded_frame": decoded, "track_info": {}}


def _lut_grade() -> dict:
    """A serial one-Corrector graph whose only operation is a LUT."""
    from src.features.color.core.color_graph_service import ColorGraphData, ColorGraphService

    service = ColorGraphService(lut_reference_validator=lambda reference: True)
    inserted = service.apply_intent(ColorGraphData.create_serial(), "insert_corrector", {})
    assert inserted.ok
    node = next(n for n in inserted.graph.nodes if n.node_type == "corrector")
    applied = service.apply_intent(inserted.graph, "set_node_lut", {
        "node_id": node.node_id, "lut_ref": "topos-res://lut_" + "0" * 32, "strength": 0.8})
    assert applied.ok
    return {"color_graph": applied.graph, "source_frame": 0}


def _primary_grade(**overrides) -> dict:
    from src.features.color.core.color_graph_service import ColorGraphData, ColorGraphService

    inserted = ColorGraphService().apply_intent(ColorGraphData.create_serial(), "insert_corrector", {})
    assert inserted.ok
    graph = replace(inserted.graph, nodes=tuple(
        replace(node, params={"grade": {"exposure": 25.0, "saturation": -15.0}, **overrides})
        if node.node_type == "corrector" else node for node in inserted.graph.nodes))
    return {"color_graph": graph, "source_frame": 8}


def _rows() -> dict[str, EditRenderTask]:
    """One task per row of the matrix."""
    return {
        "untouched H.264 clip": EditRenderTask(1, 1, [_video()]),
        "H.264 clip with one LUT": EditRenderTask(1, 1, [_video(preview_grade=_lut_grade())]),
        "H.264 clip with primary grade": EditRenderTask(1, 1, [_video(preview_grade=_primary_grade())]),
        "cross dissolve (two opacities)": EditRenderTask(
            1, 1, [_video(opacity=0.35), _video(opacity=0.65)]),
        "moved and cropped clip": EditRenderTask(
            1, 1, [_video(transform=(40.0, -25.0, 0.8, 0.8, 0.0, "center", 0.0, 0.0),
                          crop=(0.1, 0.0, 0.0, 0.2, 40.0))]),
        "solid under a clip": EditRenderTask(1, 1, [
            {"frame_type": "solid", "track_info": {"is_solid": True, "solid_color": (1, 0, 0)}},
            _video()]),
        "dip to color": EditRenderTask(1, 1, [_video(opacity=0.5), _video()], transition_jobs=(
            (SimpleNamespace(transition_type="dip_to_color", get_color_factor=lambda: 0.6,
                             blend_color=(0.0, 0.0, 0.0)), None),)),
        "clip with a video effect": EditRenderTask(1, 1, [_video(clip_effects=(object(),))]),
        "DNxHR HQX (SDR 4:2:2 10-bit) clip": EditRenderTask(1, 1, [_planar()]),
        "unsupported planar clip": EditRenderTask(
            1, 1, [{"frame_type": "planar", "decoded_frame": object(), "track_info": {}}]),
        "comp clip (GPU handle)": EditRenderTask(
            1, 1, [{"frame_type": "comp_gpu_handle", "gpu_handle": object(), "track_info": {}}]),
        "spatial transition (wipe)": EditRenderTask(
            1, 1, [_video(), _video()],
            transition_jobs=((SimpleNamespace(transition_type="wipe"), object()),)),
        "adjustment clip above": EditRenderTask(1, 1, [_video()], adjustment_layers=[(object(), (), None)]),
        "spatial grade": EditRenderTask(
            1, 1, [_video(preview_grade=_primary_grade(grade={"mid_detail": 20.0}))]),
        "animated grade": EditRenderTask(
            1, 1, [_video(preview_grade=_primary_grade(grade_keyframes={"exposure": {"keyframes": [{}]}}))]),
    }


#: row → (default ``auto``, explicit Metal, explicit OpenGL); the fourth
#: column, Metal unavailable, is GL for every row.
EXPECTED: dict[str, tuple[str, str, str]] = {
    "untouched H.264 clip": (SINGLE, SINGLE, GL),
    "H.264 clip with one LUT": (SINGLE, SINGLE, GL),
    "H.264 clip with primary grade": (COMPOSITE, COMPOSITE, GL),
    "cross dissolve (two opacities)": (COMPOSITE, COMPOSITE, GL),
    "moved and cropped clip": (COMPOSITE, COMPOSITE, GL),
    "solid under a clip": (COMPOSITE, COMPOSITE, GL),
    "dip to color": (COMPOSITE, COMPOSITE, GL),
    "clip with a video effect": (GL, GL, GL),
    "DNxHR HQX (SDR 4:2:2 10-bit) clip": (COMPOSITE, COMPOSITE, GL),
    "unsupported planar clip": (GL, GL, GL),
    "comp clip (GPU handle)": (GL, GL, GL),
    "spatial transition (wipe)": (GL, GL, GL),
    "adjustment clip above": (GL, GL, GL),
    "spatial grade": (GL, GL, GL),
    "animated grade": (GL, GL, GL),
}


def _bare_renderer(mode: str) -> EditGpuPreviewRenderer:
    """The renderer as the tests of its GL fallback build it: no GL context."""
    from PyQt6.QtCore import QThread

    renderer = EditGpuPreviewRenderer.__new__(EditGpuPreviewRenderer)
    QThread.__init__(renderer)
    renderer._metal_preview_mode = mode
    renderer._composite_fbo = object()
    renderer._active_transform_coordinate_size = (1920, 1080)
    renderer._previous_gpu_frames = []
    renderer._note_zero_copy_import = lambda *args, **kwargs: None
    return renderer


def _decide(mode: str, name: str, *, device: bool = True) -> str:
    """Which Metal entry of the renderer takes the row ``name`` (``GL`` if none).

    Every call builds its own task: a frame that is taken belongs to the
    renderer from then on, as in production.  Without a device the Metal
    preview cannot be built at all, which is what fails in production.
    """
    task = _rows()[name]
    renderer = _bare_renderer(mode)
    taken: list[str] = []
    surface = (object(), GpuFrameRelease(release_callback=Mock()))

    def entry(backend: str):
        return lambda *args, **kwargs: taken.append(backend) or surface

    if device:
        renderer._metal_preview = SimpleNamespace(
            render=entry(SINGLE), render_composite=entry(COMPOSITE),
            last_gpu_time_ms=None, blit=lambda *args, **kwargs: None)
    else:
        import src.features.edit.core.metal_timeline_preview as preview_module

        class NoDevice(preview_module.MetalTimelinePreview):
            def __init__(self, *args, **kwargs) -> None:
                raise RuntimeError("Metal is unavailable")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(preview_module, "MetalTimelinePreview", NoDevice)
            return SINGLE if renderer._try_metal_preview(task, 1920, 1080) else GL
    if renderer._try_metal_preview(task, 1920, 1080) is False:
        return GL
    (backend,) = taken
    return backend


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_frame_takes_the_backend_the_matrix_states(name: str) -> None:
    default, metal, opengl = EXPECTED[name]
    assert _decide(DEFAULT, name) == default
    assert _decide(METAL, name) == metal
    assert _decide(OPENGL, name) == opengl
    # Metal made unavailable: every frame goes to GL, whatever the mode says.
    assert _decide(METAL, name, device=False) == GL


def test_the_default_column_is_what_an_untouched_installation_gets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.features.edit.core import metal_timeline_preview

    monkeypatch.setattr(metal_timeline_preview.sys, "platform", "darwin")
    for variable in ("TOPOS_BACKEND_GRAPHICS", "TOPOS_EDIT_METAL_PREVIEW", "TOPOS_METAL_DISABLE"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(
        "src.shared.config.config_manager.ConfigManager.get_backend_selection",
        lambda self: SimpleNamespace(graphics=SimpleNamespace(value="auto")))
    assert metal_timeline_preview.metal_preview_mode() == DEFAULT


def test_the_lut_diagnostic_mode_takes_only_a_single_lut() -> None:
    for name in EXPECTED:
        expected = SINGLE if name == "H.264 clip with one LUT" else GL
        assert _decide("lut", name) == expected, name


def test_every_gl_cell_of_the_metal_column_is_a_refusal_with_a_reason() -> None:
    reasons: dict[str, str] = {}
    for name, task in _rows().items():
        if EXPECTED[name][1] != GL:
            continue
        plan = build_metal_composite_plan(task)
        assert isinstance(plan, PlanRefusal), name
        reasons[name] = plan.reason
    assert reasons == {
        "clip with a video effect": "clip_effects",
        "unsupported planar clip": "source_unsupported:planar",
        "comp clip (GPU handle)": "source_unsupported:comp_gpu_handle",
        "spatial transition (wipe)": "transition_unsupported:wipe",
        "adjustment clip above": "adjustment_layers",
        "spatial grade": "grade_unsupported",
        "animated grade": "grade_unsupported",
    }


def test_the_rows_cover_both_metal_entries_and_every_refusal_kind() -> None:
    cells = [cell for row in EXPECTED.values() for cell in row]
    assert SINGLE in cells and COMPOSITE in cells
    assert isinstance(build_metal_composite_plan(_rows()["cross dissolve (two opacities)"]),
                      MetalCompositePlan)
    plan = build_metal_composite_plan(_rows()["H.264 clip with one LUT"])
    assert isinstance(plan, MetalCompositePlan) and plan.is_single_untouched_clip
    primary = build_metal_composite_plan(_rows()["H.264 clip with primary grade"])
    assert isinstance(primary, MetalCompositePlan) and not primary.is_single_untouched_clip
    assert primary.layers[0].correctors[0]["exposure"][0] == 25.0


def test_the_kill_switch_moves_the_whole_metal_column_to_gl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.features.edit.core import metal_timeline_preview

    monkeypatch.setattr(metal_timeline_preview.sys, "platform", "darwin")
    monkeypatch.setenv("TOPOS_BACKEND_GRAPHICS", "metal")
    monkeypatch.setenv("TOPOS_METAL_DISABLE", "edit_metal_preview,edit_metal_composite")
    # The switch is read where the session picks its preview mode.
    mode = metal_timeline_preview.metal_preview_mode()
    assert mode == OPENGL
    for name in EXPECTED:
        assert _decide(mode, name) == GL, name


def test_the_composite_switch_alone_leaves_the_single_clip_on_metal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.features.edit.core import metal_timeline_preview

    monkeypatch.setattr(metal_timeline_preview.sys, "platform", "darwin")
    monkeypatch.setenv("TOPOS_BACKEND_GRAPHICS", "metal")
    monkeypatch.setenv("TOPOS_METAL_DISABLE", "edit_metal_composite")
    mode = metal_timeline_preview.metal_preview_mode()
    assert mode == METAL
    for name, (_default, metal, _opengl) in EXPECTED.items():
        expected = GL if metal == COMPOSITE else metal
        assert _decide(mode, name) == expected, name


def test_a_dead_metal_device_ends_the_session_on_gl_and_says_why(caplog) -> None:
    renderer = _bare_renderer(METAL)
    renderer._metal_preview = None
    task = _rows()["untouched H.264 clip"]
    import src.features.edit.core.metal_timeline_preview as preview_module

    class NoDevice(preview_module.MetalTimelinePreview):
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("Metal is unavailable")

    caplog.set_level(logging.WARNING, logger="src.features.edit.core.edit_gpu_preview_renderer")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(preview_module, "MetalTimelinePreview", NoDevice)
        assert renderer._try_metal_preview(task, 1920, 1080) is False
    # The frame was not consumed, the session stays on GL, and it is said why.
    assert renderer._metal_preview_mode == OPENGL
    assert task.layers[0]["vt_pixel_buffer"] is not None
    assert any("Metal preview unavailable" in record.getMessage() for record in caplog.records)
