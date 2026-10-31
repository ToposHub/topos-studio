"""Shader/uniform contract for the merge pipeline (S2-A remediation).

The bbox-mapping protocol is written in three places that used to drift
apart: the GLSL declarations in ``core/shaders.py``, the uniform dict in
``core/format.py`` (``MergeGeometry.to_uniform_dict``) and the guarded
uploads in ``evaluators/base_evaluator.py``.  These tests pin all three to
one contract:

  * every bbox uniform the Python side produces is declared in the shader;
  * the shader actually compiles and keeps those uniforms alive through
    linking (dead code would silently drop them) — real-GL smoke, skipped
    when no standalone context is available.
"""

from __future__ import annotations

import pytest

from src.features.composite.core.format import MergeGeometry
from src.features.composite.core.shaders import _BLEND_FRAGMENT

# The uniforms the merge evaluator produces for bbox mapping.  If a name
# changes here, the shader AND base_evaluator must change with it.
GEOMETRY_BBOX_UNIFORMS = (
    "u_output_resolution",
    "u_output_bbox",
    "u_bbox_a",
    "u_bbox_b",
    "u_bbox_mask",
    "u_resolution_a",
    "u_resolution_b",
    "u_resolution_mask",
)
# Set by base_evaluator from the geometry mismatch analysis, not by
# MergeGeometry.to_uniform_dict — still part of the shader contract.
EVALUATOR_BBOX_UNIFORMS = ("u_use_bbox_mapping",)
BBOX_UNIFORMS = GEOMETRY_BBOX_UNIFORMS + EVALUATOR_BBOX_UNIFORMS


def test_geometry_uniform_dict_and_shader_declare_the_same_bbox_names():
    from src.features.composite.core.format import BBox
    geometry = MergeGeometry(
        output_width=1920, output_height=1080,
        output_bbox=BBox(x=0, y=0, width=1920, height=1080),
        bbox_a=BBox(x=100, y=50, width=800, height=600),
        bbox_b=BBox(x=0, y=0, width=1920, height=1080),
        bbox_mask=None,
        resolution_a=(800, 600),
        resolution_b=(1920, 1080),
    )
    produced = set(geometry.to_uniform_dict())
    for name in GEOMETRY_BBOX_UNIFORMS:
        assert name in produced, f"MergeGeometry no longer produces {name}"
    for name in BBOX_UNIFORMS:
        assert name in _BLEND_FRAGMENT, (
            f"{name} is uploaded by the evaluator but missing from "
            "_BLEND_FRAGMENT — the merge contract drifted")


def test_sample_layer_maps_through_output_bbox_not_canvas_resolution():
    """sampleLayer must consume u_output_bbox (the render-target region), not
    u_output_resolution, so cropped output targets align with layer bboxes."""
    assert "sampleLayer(u_texture_a, v_uv, u_bbox_a, u_resolution_a, u_output_bbox)" \
        in _BLEND_FRAGMENT
    assert "u_output_resolution / tex_res" in _BLEND_FRAGMENT  # legacy path intact


def test_blend_shader_compiles_and_keeps_bbox_uniforms_alive():
    moderngl = pytest.importorskip("moderngl")
    try:
        ctx = moderngl.create_standalone_context()
    except Exception as exc:  # headless machine without GL
        pytest.skip(f"no standalone GL context: {exc}")
    from src.features.composite.core.shaders import _PASSTHROUGH_VERTEX
    program = ctx.program(vertex_shader=_PASSTHROUGH_VERTEX,
                          fragment_shader=_BLEND_FRAGMENT)
    linked = set(program)
    for name in BBOX_UNIFORMS:
        assert name in linked, (
            f"{name} was eliminated at link time — sampleLayer's bbox "
            "mapping lost its uniform")
    ctx.release()
