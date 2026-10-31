"""Aurora 渐变背景 — layered drifting radial glows on a deep gradient."""

from src.features.teaching_animation.presets.builders import (
    key, radial_gradient, linear_gradient, scene_document, shape_element, track,
)

PRESET = {
    "preset_id": "aurora_glow_bg",
    "name": "Aurora 渐变背景",
    "name_i18n": {"en_US": "Aurora Gradient Background"},
    "category": "backgrounds",
    "description": "深色渐变底 + 三团缓慢漂移的极光辉斑，适合做标题背景",
    "description_i18n": {"en_US": "Dark gradient base with three slowly drifting aurora glows, ideal behind titles"},
    "tags": ["background", "gradient", "aurora", "背景", "渐变"],
}


def build() -> dict:
    elements = [
        shape_element("bg_plate", 0, 0, 1920, 1080, kind="rect", z=0,
                      fill="#070b1a",
                      gradient=linear_gradient((0.0, "#060a18"), (1.0, "#101a3c"), angle=90.0)),
        shape_element("glow_teal", 240, 180, 900, 760, kind="ellipse", z=1,
                      fill="#14b8a6", fill_opacity=0.55,
                      gradient=radial_gradient((0.0, "#14b8a6"), (1.0, "#0b1220"),
                                               opacities=(0.5, 0.0)),
                      tracks=(
                          track("x", key(0, 240.0), key(48, 340.0), key(96, 240.0)),
                          track("opacity", key(0, 0.75), key(48, 1.0), key(96, 0.75)),
                      )),
        shape_element("glow_indigo", 880, 320, 980, 720, kind="ellipse", z=2,
                      fill="#6366f1", fill_opacity=0.5,
                      gradient=radial_gradient((0.0, "#6366f1"), (1.0, "#0b1220"),
                                               opacities=(0.45, 0.0)),
                      tracks=(
                          track("x", key(0, 880.0), key(48, 780.0), key(96, 880.0)),
                          track("y", key(0, 320.0), key(48, 280.0), key(96, 320.0)),
                      )),
        shape_element("glow_violet", 520, 560, 820, 640, kind="ellipse", z=3,
                      fill="#a855f7", fill_opacity=0.45,
                      gradient=radial_gradient((0.0, "#a855f7"), (1.0, "#0b1220"),
                                               opacities=(0.4, 0.0)),
                      tracks=(
                          track("y", key(0, 560.0), key(48, 620.0), key(96, 560.0)),
                          track("opacity", key(0, 0.9), key(48, 0.6), key(96, 0.9)),
                      )),
    ]
    return scene_document(elements, name="Aurora Glow Background", duration_frames=96).to_dict()
