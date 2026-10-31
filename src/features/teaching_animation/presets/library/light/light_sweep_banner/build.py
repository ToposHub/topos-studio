"""扫光横幅 — diagonal light beam sweeping across a dark banner."""

from src.features.teaching_animation.presets.builders import (
    blur_effect, key, linear_gradient, scene_document, shape_element, text_element, track,
)

PRESET = {
    "preset_id": "light_sweep_banner",
    "name": "扫光横幅",
    "name_i18n": {"en_US": "Light Sweep Banner"},
    "category": "light",
    "description": "深色横幅上一道倾斜高光从左到右扫过，突出标题文字",
    "description_i18n": {"en_US": "A tilted highlight sweeps across a dark banner to emphasize a title"},
    "tags": ["light", "sweep", "banner", "光效", "扫光"],
}


def build() -> dict:
    return scene_document([
        shape_element("panel", 160, 340, 1600, 400, kind="rect", z=0,
                      fill="#0f172a", corner_radius=24.0,
                      gradient=linear_gradient((0.0, "#111c3a"), (1.0, "#0b1122"), angle=90.0),
                      stroke="#1e3a5f", stroke_width=2.0),
        text_element("title", 960, 448, "WEEKLY RELEASE", size=96, weight="800",
                     color="#f1f5f9", anchor="middle", z=5,
                     tracks=(track("opacity", key(0, 0.0), key(10, 1.0)),)),
        text_element("subtitle", 960, 580, "v2.4 · 现已发布", size=34,
                     color="#7dd3fc", anchor="middle", z=6,
                     tracks=(track("opacity", key(6, 0.0), key(18, 1.0, "ease_out")),)),
        shape_element("beam", -300, 300, 520, 480, kind="rect", z=8,
                      fill="#e0f2fe", fill_opacity=0.55, rotation=-18.0,
                      gradient=linear_gradient((0.0, "#e0f2fe"), (0.5, "#ffffff"),
                                               (1.0, "#e0f2fe"), angle=0.0,
                                               opacities=(0.0, 0.85, 0.0)),
                      effects=(blur_effect(10.0),),
                      tracks=(
                          track("x", key(6, -420.0), key(60, 1980.0, "ease_in_out")),
                          track("opacity", key(0, 0.0), key(6, 0.0), key(14, 1.0),
                                key(52, 1.0), key(60, 0.0)),
                      )),
    ], name="Light Sweep Banner", duration_frames=72).to_dict()
