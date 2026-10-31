"""等距立方体 — three gradient faces floating with a soft ground shadow."""

from src.features.teaching_animation.presets.builders import (
    key, linear_gradient, radial_gradient, scene_document, shape_element, track,
)

PRESET = {
    "preset_id": "isometric_cube",
    "name": "等距 3D 立方体",
    "name_i18n": {"en_US": "Isometric 3D Cube"},
    "category": "shapes3d",
    "description": "三面渐变的 2.5D 立方体上下悬浮，地面投影随呼吸缩放",
    "description_i18n": {"en_US": "A 2.5D cube with gradient faces hovers above a breathing ground shadow"},
    "tags": ["3d", "isometric", "cube", "shape", "3D", "立方体"],
}

# Cube centre (960, 470), half-width 300, iso half-height 150.
_TOP = "M 0 0 L 300 150 L 0 300 L -300 150 Z"
_LEFT = "M -300 150 L 0 300 L 0 640 L -300 490 Z"
_RIGHT = "M 300 150 L 0 300 L 0 640 L 300 490 Z"
_FLOAT = (key(0, 0.0), key(24, -16.0, "ease_in_out"), key(48, 6.0, "ease_in_out"),
          key(72, 0.0, "ease_in_out"))


def build() -> dict:
    y_track = track("y", *_FLOAT)
    return scene_document([
        shape_element("ground_shadow", 700, 1075, 520, 90, kind="ellipse", z=0,
                      fill="#000000", fill_opacity=0.45,
                      gradient=radial_gradient((0.0, "#000000"), (1.0, "#000000"),
                                               opacities=(0.5, 0.0)),
                      tracks=(
                          track("scale_x", key(0, 1.0), key(24, 0.9, "ease_in_out"),
                                key(48, 1.05, "ease_in_out"), key(72, 1.0)),
                          track("opacity", key(0, 0.5), key(24, 0.35), key(48, 0.55),
                                key(72, 0.5)),
                      )),
        shape_element("face_left", 960, 170, 600, 490, kind="path", z=2,
                      fill="#2563eb", d=_LEFT,
                      gradient=linear_gradient((0.0, "#1d4ed8"), (1.0, "#1e3a8a")),
                      tracks=(y_track,)),
        shape_element("face_right", 960, 170, 600, 490, kind="path", z=3,
                      fill="#3b82f6", d=_RIGHT,
                      gradient=linear_gradient((0.0, "#3b82f6"), (1.0, "#1e40af")),
                      tracks=(y_track,)),
        shape_element("face_top", 960, 170, 600, 300, kind="path", z=4,
                      fill="#93c5fd", d=_TOP,
                      gradient=linear_gradient((0.0, "#bfdbfe"), (1.0, "#60a5fa")),
                      tracks=(y_track,)),
    ], name="Isometric 3D Cube", duration_frames=72).to_dict()
