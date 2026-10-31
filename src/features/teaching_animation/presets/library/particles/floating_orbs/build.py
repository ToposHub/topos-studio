"""漂浮粒子 — soft glowing orbs drifting on looping sine-like paths."""

import random

from src.features.teaching_animation.presets.builders import (
    key, radial_gradient, scene_document, shape_element, track,
)

PRESET = {
    "preset_id": "floating_orbs",
    "name": "漂浮光球粒子",
    "name_i18n": {"en_US": "Floating Orbs Particles"},
    "category": "particles",
    "description": "18 个大小不一的柔光球缓慢上下漂浮 + 呼吸闪烁，循环播放",
    "description_i18n": {"en_US": "Eighteen soft glowing orbs drift up and down with breathing twinkle, looping"},
    "tags": ["particles", "orbs", "loop", "粒子", "漂浮"],
}

_COUNT = 18


def build() -> dict:
    rng = random.Random(20260821)
    elements = []
    for index in range(_COUNT):
        x = 80 + rng.random() * 1720
        y = 100 + rng.random() * 820
        size = 14 + rng.random() * 52
        phase = rng.randrange(0, 24)
        drift = 26.0 + rng.random() * 42.0
        color = rng.choice(["#38bdf8", "#22d3ee", "#a78bfa", "#f472b6", "#facc15"])
        # AUD-29200: the loop is a phase-shifted 96-frame triangle wave whose
        # extrema sit at 24+phase / 72+phase and whose wrap value is the
        # mid-swing point ``edge``, so every segment covers exactly
        # ``drift`` per 24 frames — the old tail key compressed 2*drift into
        # ``24 - phase`` frames (an ~8x speed rush before the loop point).
        edge = y + drift * phase / 24.0
        elements.append(shape_element(
            f"orb_{index}", x, y, size, size, kind="circle", z=index,
            fill=color, fill_opacity=0.85,
            gradient=radial_gradient((0.0, color), (1.0, color), opacities=(0.9, 0.0)),
            tracks=(
                track("y", key(0, edge),
                      key(24 + phase, y - drift, "ease_in_out"),
                      key(72 + phase, y + drift, "ease_in_out"),
                      key(96, edge, "ease_in_out")),
                track("opacity", key(0, 0.55), key(36, 1.0), key(72, 0.55), key(96, 0.55)),
            )))
    return scene_document(elements, name="Floating Orbs Particles",
                          duration_frames=96).to_dict()
