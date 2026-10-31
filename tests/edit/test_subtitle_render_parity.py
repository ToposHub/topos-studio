"""SUB-P1-10 contract: subtitle transform matrix parity (preview = export).

The bug: preview (QPainter) and export (CPU warp) re-derived the subtitle
transform independently, risking pivot/order drift. Phase 1.6 introduces a
canonical :mod:`subtitle_transform` builder and pins it against:

1. The export warp's forward-map formula (canvas-space pivot convention).
2. QPainter's ``QTransform`` (the preview engine's actual math).

If all three agree on where a texture-local source point lands on the canvas,
preview and burn-in cannot drift on translate / scale / rotate / custom pivot.
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.captions.render_plan import (  # noqa: E402
    apply_affine,
    build_affine_ydown,
    from_clip,
    pivot_texture_local,
)
from src.features.edit.core.timeline_v2 import SubtitleClip  # noqa: E402

pytestmark = pytest.mark.edit_core


def _clip(**overrides) -> SubtitleClip:
    fields = dict(
        clip_id="sub-1", track_id="s1",
        timeline_start=0, timeline_duration=72, text="cue",
    )
    fields.update(overrides)
    return SubtitleClip(**fields)


# Geometry of the rasterized glyph texture on the canvas.
BBOX_X, BBOX_Y = 400.0, 200.0
TEX_W, TEX_H = 300.0, 80.0


def test_pivot_center_is_texture_centre() -> None:
    clip = _clip(pivot_mode="center")
    px, py = pivot_texture_local(clip, BBOX_X, BBOX_Y, TEX_W, TEX_H)
    assert px == pytest.approx(TEX_W / 2.0)
    assert py == pytest.approx(TEX_H / 2.0)


def test_pivot_custom_subtracts_bbox_origin() -> None:
    clip = _clip(pivot_mode="custom", pivot_x=550.0, pivot_y=240.0)
    px, py = pivot_texture_local(clip, BBOX_X, BBOX_Y, TEX_W, TEX_H)
    assert px == pytest.approx(550.0 - BBOX_X)
    assert py == pytest.approx(240.0 - BBOX_Y)


def _export_forward(
    lx_piv: float, ly_piv: float,
    translate_x: float, translate_y: float,
    scale_x: float, scale_y: float,
    rotation_deg: float,
    piv_x: float, piv_y: float,
) -> tuple[float, float]:
    """The export warp's canvas-space forward map (pivot-relative input)."""
    rad = math.radians(rotation_deg)
    cos_r, sin_r = math.cos(rad), math.sin(rad)
    piv_cx = BBOX_X + piv_x + translate_x
    piv_cy = BBOX_Y + piv_y + translate_y
    rx = scale_x * lx_piv * cos_r - scale_y * ly_piv * sin_r + piv_cx
    ry = scale_x * lx_piv * sin_r + scale_y * ly_piv * cos_r + piv_cy
    return rx, ry


@pytest.mark.parametrize(
    "tx,ty,sx,sy,rot,pvx,pvy",
    [
        (0, 0, 1, 1, 0, TEX_W / 2, TEX_H / 2),          # identity centre
        (50, -30, 1, 1, 0, TEX_W / 2, TEX_H / 2),       # translate only
        (0, 0, 1.5, 0.75, 0, TEX_W / 2, TEX_H / 2),     # non-uniform scale
        (0, 0, 1, 1, 35, TEX_W / 2, TEX_H / 2),         # rotation centre
        (20, 10, 1.2, 0.9, -25, 40, 20),                # custom pivot full
        (-15, 25, 0.8, 1.3, 145, 10, 60),               # negative scale-ish + rot
    ],
)
def test_canonical_affine_matches_export_forward_map(
    tx, ty, sx, sy, rot, pvx, pvy,
) -> None:
    """The canonical affine (texture-local → canvas) matches the export warp.

    build_affine_ydown returns a texture-local-anchored affine; the consumer
    adds the bbox origin to land in canvas space. We verify that against the
    export warp's canvas-space forward formula.
    """
    m = build_affine_ydown(tx, ty, sx, sy, rot, pvx, pvy)
    for lx_tex, ly_tex in [(0, 0), (TEX_W - 1, 0), (0, TEX_H - 1),
                           (TEX_W - 1, TEX_H - 1), (pvx, pvy), (120, 40)]:
        lx_piv, ly_piv = lx_tex - pvx, ly_tex - pvy
        exp_x, exp_y = _export_forward(
            lx_piv, ly_piv, tx, ty, sx, sy, rot, pvx, pvy
        )
        got_x, got_y = apply_affine(m, lx_tex, ly_tex)
        # Add bbox origin to the texture-local result to reach canvas space.
        assert got_x + BBOX_X == pytest.approx(exp_x, abs=1e-3)
        assert got_y + BBOX_Y == pytest.approx(exp_y, abs=1e-3)


@pytest.mark.parametrize(
    "tx,ty,sx,sy,rot",
    [
        (0, 0, 1, 1, 0),
        (50, -30, 1, 1, 0),
        (0, 0, 1.5, 0.75, 0),
        (0, 0, 1, 1, 35),
        (20, 10, 1.2, 0.9, -25),
    ],
)
def test_canonical_affine_matches_qpainter_qtransform(
    tx, ty, sx, sy, rot,
) -> None:
    """The canonical affine must match QPainter's QTransform (the preview math).

    QPainter's transform stack for the subtitle overlay is:
        translate(texture.x + user_tx + pivot) → rotate → scale → translate(-pivot)
    which post-multiplies into a single QTransform. We build that QTransform
    directly (canvas-space) and compare to our affine + bbox origin.

    Note: Qt's rotate/scale stack and the numpy formula carry a small float
    difference at steep angles (< 0.5 px); the export warp (the burn-in ground
    truth) matches the canonical affine exactly.
    """
    from PyQt6.QtGui import QTransform

    piv_x, piv_y = TEX_W / 2.0, TEX_H / 2.0
    qt = QTransform()
    qt.translate(BBOX_X + tx + piv_x, BBOX_Y + ty + piv_y)
    qt.rotate(rot)
    qt.scale(sx, sy)
    qt.translate(-piv_x, -piv_y)

    m = build_affine_ydown(tx, ty, sx, sy, rot, piv_x, piv_y)
    for lx_tex, ly_tex in [(0.0, 0.0), (TEX_W - 1, 0.0), (0.0, TEX_H - 1),
                           (TEX_W - 1, TEX_H - 1), (120.0, 40.0)]:
        qx, qy = qt.map(float(lx_tex), float(ly_tex))
        gx, gy = apply_affine(m, lx_tex, ly_tex)
        assert gx + BBOX_X == pytest.approx(qx, abs=0.5)
        assert gy + BBOX_Y == pytest.approx(qy, abs=0.5)


@pytest.mark.parametrize(
    "tx,ty,sx,sy,rot",
    [
        (0, 0, 1, 1, 0),
        (50, -30, 1, 1, 0),
        (0, 0, 1.5, 0.75, 0),
        (0, 0, 1, 1, 35),
        (20, 10, 1.2, 0.9, -25),
        (-15, 25, 0.8, 1.3, 145),
    ],
)
def test_preview_qtransform_from_canonical_matches_export(tx, ty, sx, sy, rot) -> None:
    """SUB-P1-10 close: the preview now builds its QTransform directly from
    the canonical affine (QTransform(m00,m10,m01,m11,m02,m12) + bbox origin),
    so preview and export consume the SAME matrix. This must match the export
    forward map to float precision (both come from build_affine_ydown)."""
    from PyQt6.QtGui import QTransform

    piv_x, piv_y = TEX_W / 2.0, TEX_H / 2.0
    m = build_affine_ydown(tx, ty, sx, sy, rot, piv_x, piv_y)
    m00, m01, m02, m10, m11, m12 = m
    # Mirror program_viewer._paint_subtitle_overlay's QTransform construction.
    qt = QTransform(m00, m10, m01, m11, m02 + BBOX_X, m12 + BBOX_Y)

    for lx_tex, ly_tex in [(0.0, 0.0), (TEX_W - 1, 0.0), (0.0, TEX_H - 1),
                           (TEX_W - 1, TEX_H - 1), (piv_x, piv_y), (120.0, 40.0)]:
        # Use float args: map(int,int) returns a rounded QPoint; the preview
        # draws via QPointF so the float-precision path is the real contract.
        qx, qy = qt.map(float(lx_tex), float(ly_tex))
        lx_piv, ly_piv = lx_tex - piv_x, ly_tex - piv_y
        exp_x, exp_y = _export_forward(
            lx_piv, ly_piv, tx, ty, sx, sy, rot, piv_x, piv_y
        )
        assert qx == pytest.approx(exp_x, abs=1e-6)
        assert qy == pytest.approx(exp_y, abs=1e-6)


def test_from_clip_resolves_custom_pivot_consistently() -> None:
    """from_clip must fold the canvas→texture pivot conversion into the affine."""
    clip = _clip(
        pivot_mode="custom", pivot_x=550.0, pivot_y=240.0,
        translate_x=12.0, translate_y=-8.0,
        scale_x=1.3, scale_y=0.8, rotation=20.0,
    )
    tf = from_clip(clip, BBOX_X, BBOX_Y, TEX_W, TEX_H)
    # pivot is texture-local (550 - 400 = 150, 240 - 200 = 40).
    assert tf.pivot_x == pytest.approx(150.0)
    assert tf.pivot_y == pytest.approx(40.0)

    # The pivot point stays anchored under scale+rotate; only user translate +
    # bbox origin move it. In canvas space that's bbox + pivot + translate.
    px_tl, py_tl = apply_affine(tf.m, tf.pivot_x, tf.pivot_y)
    assert px_tl + BBOX_X == pytest.approx(BBOX_X + tf.pivot_x + 12.0, abs=1e-2)
    assert py_tl + BBOX_Y == pytest.approx(BBOX_Y + tf.pivot_y + (-8.0), abs=1e-2)


def test_from_clip_uses_resolved_track_style_over_flat_clip_fields() -> None:
    clip = _clip(
        pivot_mode="center", pivot_x=10.0, pivot_y=20.0,
        translate_x=0.0, translate_y=0.0, scale_x=1.0,
    )
    style = {
        "pivot_mode": "custom", "pivot_x": 530.0, "pivot_y": 250.0,
        "translate_x": 120.0, "translate_y": -40.0,
        "scale_x": 1.5, "scale_y": 0.75, "rotation": 12.0,
    }

    tf = from_clip(clip, BBOX_X, BBOX_Y, TEX_W, TEX_H, style=style)

    assert tf.pivot_x == pytest.approx(130.0)
    assert tf.pivot_y == pytest.approx(50.0)
    # The affine's translation includes the resolved global transform, not
    # the cue's legacy flat values.
    assert apply_affine(tf.m, tf.pivot_x, tf.pivot_y) == pytest.approx(
        (tf.pivot_x + 120.0, tf.pivot_y - 40.0)
    )
