"""动感弹跳标题 — kinetic words popping and sliding in sequence."""

from src.features.teaching_animation.presets.builders import (
    glow_effect, key, scene_document, shape_element, text_element, track,
)

PRESET = {
    "preset_id": "kinetic_pop_title",
    "name": "动感弹跳标题",
    "name_i18n": {"en_US": "Kinetic Pop Title"},
    "category": "text",
    "description": "主标题回弹缩放 + 副词滑入 + 说明文字淡入的三段式排版",
    "description_i18n": {"en_US": "Three-beat layout: main title pops in, subtitle slides, caption fades"},
    "tags": ["text", "title", "kinetic", "pop", "文字", "标题", "动效"],
}


def build() -> dict:
    return scene_document([
        text_element("word_intro", 960, 300, "INTRODUCING", size=44, weight="300",
                     color="#f9a8d4", anchor="middle", z=10, letter_spacing=18.0,
                     tracks=(track("opacity", key(0, 0.0), key(8, 1.0)),
                             track("y", key(0, 330.0), key(14, 300.0, "ease_out")))),
        text_element("word_main", 960, 400, "COLOR GRADE", size=150, weight="800",
                     color="#fef3c7", anchor="middle", z=11,
                     effects=(glow_effect("#f59e0b", radius=22.0, opacity=0.5),),
                     tracks=(
                         track("scale_x", key(4, 0.5), key(18, 1.06, "ease_out"),
                               key(26, 1.0, "ease_in_out")),
                         track("scale_y", key(4, 0.5), key(18, 1.06, "ease_out"),
                               key(26, 1.0, "ease_in_out")),
                         track("rotation", key(4, -7.0), key(22, 0.0, "ease_out")),
                     )),
        text_element("word_sub", 960, 590, "IN ONE CLICK", size=64, weight="600",
                     color="#fdba74", anchor="middle", z=12,
                     tracks=(track("x", key(10, 780.0), key(26, 960.0, "ease_out")),
                             track("opacity", key(10, 0.0), key(18, 1.0)))),
        shape_element("dot_left", 620, 616, 18, 18, kind="circle", z=13, fill="#f472b6",
                      tracks=(track("opacity", key(22, 0.0), key(30, 1.0)),)),
        shape_element("dot_right", 1282, 616, 18, 18, kind="circle", z=13, fill="#f472b6",
                      tracks=(track("opacity", key(24, 0.0), key(32, 1.0)),)),
        text_element("caption", 960, 700, "topos-color.example · 2026", size=26,
                     color="#94a3b8", anchor="middle", z=14,
                     tracks=(track("opacity", key(30, 0.0), key(42, 1.0)),)),
    ], name="Kinetic Pop Title", duration_frames=60).to_dict()
