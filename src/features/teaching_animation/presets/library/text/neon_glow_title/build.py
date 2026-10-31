"""霓虹发光标题 — glowing title with breathing underline."""

from src.features.teaching_animation.presets.builders import (
    glow_effect, key, linear_gradient, scene_document, shape_element, text_element, track,
)

PRESET = {
    "preset_id": "neon_glow_title",
    "name": "霓虹发光标题",
    "name_i18n": {"en_US": "Neon Glow Title"},
    "category": "text",
    "description": "青色霓虹描边发光 + 呼吸下划线，暗背景大标题",
    "description_i18n": {"en_US": "Cyan neon stroke glow with a breathing underline for big dark-scene titles"},
    "tags": ["text", "title", "neon", "glow", "文字", "标题", "发光"],
}


def build() -> dict:
    elements = [
        text_element("title", 960, 400, "TOPOS COLOR", size=170, weight="800",
                     color="#e0fbff", anchor="middle", z=10, letter_spacing=6.0,
                     effects=(glow_effect("#22d3ee", radius=34.0, opacity=0.95),
                              glow_effect("#0ea5e9", radius=10.0, opacity=0.9)),
                     tracks=(
                         track("scale_x", key(0, 0.94), key(30, 1.0, "ease_out")),
                         track("scale_y", key(0, 0.94), key(30, 1.0, "ease_out")),
                         track("opacity", key(0, 0.0), key(8, 1.0)),
                     )),
        text_element("subtitle", 960, 620, "MOTION GRAPHIC EDITOR", size=40,
                     weight="300", color="#7dd3fc", anchor="middle", z=11,
                     letter_spacing=14.0,
                     tracks=(track("opacity", key(10, 0.0), key(24, 1.0, "ease_out")),)),
        shape_element("underline", 660, 704, 600, 8, kind="rect", z=12,
                      fill="#22d3ee", corner_radius=4.0,
                      gradient=linear_gradient((0.0, "#0ea5e9"), (0.5, "#67e8f9"), (1.0, "#0ea5e9")),
                      tracks=(
                          track("width", key(14, 0.0), key(34, 600.0, "ease_out")),
                          track("opacity", key(0, 0.6), key(36, 1.0), key(72, 0.6)),
                      )),
    ]
    return scene_document(elements, name="Neon Glow Title", duration_frames=72).to_dict()
