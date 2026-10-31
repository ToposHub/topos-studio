"""镜头光晕 — layered flare discs breathing along a diagonal axis."""

from src.features.teaching_animation.presets.builders import (
    blur_effect, key, radial_gradient, scene_document, shape_element, track,
)

PRESET = {
    "preset_id": "lens_flare_stack",
    "name": "镜头光晕",
    "name_i18n": {"en_US": "Lens Flare Stack"},
    "category": "light",
    "description": "主光斑 + 光环 + 小光点沿对角线排布，整体呼吸脉动",
    "description_i18n": {"en_US": "Main flare, ring and streak dots along a diagonal with a breathing pulse"},
    "tags": ["light", "flare", "glow", "光效", "光晕"],
}


def _disc_path(radius: float) -> str:
    """Circle centred on the local origin (AUD-28900).

    ``circle`` elements carry their centre at local (r, r), so the rendered
    ``translate(x y) ... scale(sx sy)`` pivots scaling on the bounding-box
    top-left and the breathing pulse drifts the flare discs diagonally.
    A path defined around (0, 0) makes scale/rotate pivot the disc centre:
    the element's (x, y) then positions the *centre* and the stack keeps a
    stable optical axis while pulsing.
    """
    return (f"M {-radius:.1f} 0 A {radius:.1f} {radius:.1f} 0 1 0 {radius:.1f} 0 "
            f"A {radius:.1f} {radius:.1f} 0 1 0 {-radius:.1f} 0 Z")


def build() -> dict:
    return scene_document([
        # (x, y) is the disc centre; width/height keep the 2r bbox for the
        # editor.  halo centre (880, 540), ring centre (890, 550) — co-axial
        # with the static "core" flare at (890, 550).
        shape_element("halo_big", 880, 540, 640, 640, kind="path", z=0,
                      fill="#fbbf24", fill_opacity=0.9,
                      gradient=radial_gradient((0.0, "#fde68a"), (0.55, "#f59e0b"),
                                               (1.0, "#0b1122"),
                                               opacities=(0.95, 0.35, 0.0)),
                      effects=(blur_effect(6.0),),
                      d=_disc_path(320.0),
                      tracks=(
                          track("scale_x", key(0, 1.0), key(26, 1.07, "ease_in_out"),
                                key(52, 0.98, "ease_in_out"), key(72, 1.0, "ease_in_out")),
                          track("scale_y", key(0, 1.0), key(26, 1.07, "ease_in_out"),
                                key(52, 0.98, "ease_in_out"), key(72, 1.0, "ease_in_out")),
                          track("opacity", key(0, 0.85), key(36, 1.0), key(72, 0.85)),
                      )),
        shape_element("ring_teal", 890, 550, 380, 380, kind="path", z=1,
                      fill="#0b1122", fill_opacity=0.0, stroke="#22d3ee",
                      stroke_width=5.0, d=_disc_path(190.0),
                      tracks=(track("scale_x", key(0, 0.9), key(36, 1.08, "ease_in_out"),
                                    key(72, 0.9)),
                              track("scale_y", key(0, 0.9), key(36, 1.08, "ease_in_out"),
                                    key(72, 0.9)))),
        shape_element("core", 806, 466, 168, 168, kind="circle", z=2,
                      fill="#fffbeb",
                      gradient=radial_gradient((0.0, "#ffffff"), (1.0, "#fde68a"),
                                               opacities=(1.0, 0.0)),
                      tracks=(track("opacity", key(0, 0.9), key(30, 1.0), key(60, 0.85),
                                    key(72, 0.9)),)),
        shape_element("spark_violet", 1150, 700, 120, 120, kind="circle", z=3,
                      fill="#a78bfa",
                      gradient=radial_gradient((0.0, "#c4b5fd"), (1.0, "#0b1122"),
                                               opacities=(0.9, 0.0)),
                      tracks=(track("opacity", key(8, 0.0), key(28, 0.95),
                                    key(56, 0.6), key(72, 0.9)))),
        shape_element("spark_pink", 1300, 560, 64, 64, kind="circle", z=4, fill="#f472b6",
                      gradient=radial_gradient((0.0, "#f9a8d4"), (1.0, "#0b1122"),
                                               opacities=(0.9, 0.0)),
                      tracks=(track("opacity", key(12, 0.0), key(32, 0.9), key(72, 0.8)))),
        shape_element("spark_cyan", 1000, 820, 44, 44, kind="circle", z=5, fill="#22d3ee",
                      gradient=radial_gradient((0.0, "#67e8f9"), (1.0, "#0b1122"),
                                               opacities=(0.9, 0.0)),
                      tracks=(track("opacity", key(16, 0.0), key(36, 0.9), key(72, 0.8)))),
    ], name="Lens Flare Stack", duration_frames=72).to_dict()
