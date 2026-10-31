"""彩带散落 — confetti rectangles tumbling down with staggered starts."""

import random

from src.features.teaching_animation.presets.builders import key, scene_document, shape_element, track

PRESET = {
    "preset_id": "confetti_burst",
    "name": "彩带散落粒子",
    "name_i18n": {"en_US": "Confetti Burst"},
    "category": "particles",
    "description": "24 片彩带旋转下落，色彩随机、错峰出场，庆祝/结尾场景",
    "description_i18n": {"en_US": "Twenty-four rotating ribbons fall with random colors and staggered entries"},
    "tags": ["particles", "confetti", "celebration", "粒子", "彩带"],
}

_COUNT = 24
_COLORS = ["#f87171", "#fb923c", "#facc15", "#4ade80", "#22d3ee", "#a78bfa", "#f472b6"]


def build() -> dict:
    rng = random.Random(7)
    elements = []
    for index in range(_COUNT):
        x = 60 + rng.random() * 1800
        fall = 520.0 + rng.random() * 360.0
        start = rng.randrange(0, 20)
        end = start + 34 + rng.randrange(0, 14)
        spin = 360.0 * rng.randrange(1, 3)
        color = rng.choice(_COLORS)
        elements.append(shape_element(
            f"confetti_{index}", x, -80.0, 16, 26, kind="rect", z=index,
            fill=color, corner_radius=3.0, rotation=rng.randrange(0, 90),
            tracks=(
                track("y", key(start, -80.0), key(end, fall, "ease_in")),
                track("rotation", key(start, 0.0), key(end, spin)),
                track("opacity", key(start, 0.0), key(start + 4, 1.0),
                      key(end - 6, 1.0), key(end, 0.0)),
            )))
    return scene_document(elements, name="Confetti Burst Particles",
                          duration_frames=72).to_dict()
