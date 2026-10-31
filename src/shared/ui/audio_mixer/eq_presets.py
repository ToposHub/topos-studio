"""Graphic EQ 预设（移植自 audio_pro PresetManager.presets.eq）。

按 mode 分组的频段增益预设。每个预设是 {"name": str, "bands": list[float]}。
bands 长度需匹配对应 mode 的频段数（3 / 7 / 10 / 15）。

UI 用法：
    presets = EQ_PRESETS["10-band"]  # list[dict]
    for p in presets:
        menu.addAction(p["name"], ...)
    # 应用时：StripEQParams(mode="10-band", bands=tuple(p["bands"]))
"""

from __future__ import annotations

from typing import Any


# 预设定义：{mode: [{"name", "bands"}, ...]}
# bands 为各频段增益（dB），长度 = 该 mode 的频段数。
# 移植自 audio_pro/static/audio_player.js PresetManager.presets.eq。
EQ_PRESETS: dict[str, list[dict[str, Any]]] = {
    "3-band": [
        {"name": "平坦",     "bands": [0.0, 0.0, 0.0]},
        {"name": "低音增强", "bands": [6.0, 3.0, 0.0]},
        {"name": "人声突出", "bands": [-2.0, 4.0, 2.0]},
        {"name": "高音增强", "bands": [-2.0, 0.0, 6.0]},
        {"name": "电子音乐", "bands": [5.0, -2.0, 4.0]},
        {"name": "原声",     "bands": [3.0, 2.0, 3.0]},
        {"name": "摇滚",     "bands": [4.0, -1.0, 5.0]},
        {"name": "爵士",     "bands": [2.0, 3.0, 4.0]},
    ],
    "7-band": [
        {"name": "平坦",     "bands": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
        {"name": "低音增强", "bands": [8.0, 5.0, 2.0, 0.0, -1.0, -1.0, -2.0]},
        {"name": "人声突出", "bands": [-2.0, 0.0, 3.0, 5.0, 4.0, 2.0, 0.0]},
    ],
    "10-band": [
        {"name": "平坦",     "bands": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
        {"name": "低音增强", "bands": [8.0, 6.0, 4.0, 2.0, 0.0, -1.0, -1.0, -1.0, -2.0, -2.0]},
        {"name": "人声突出", "bands": [-3.0, -2.0, 0.0, 2.0, 4.0, 5.0, 4.0, 2.0, 0.0, -1.0]},
        {"name": "电子音乐", "bands": [6.0, 4.0, 0.0, -2.0, -1.0, 0.0, 2.0, 4.0, 5.0, 4.0]},
        {"name": "摇滚",     "bands": [5.0, 4.0, 2.0, -1.0, -2.0, 1.0, 3.0, 5.0, 5.0, 4.0]},
    ],
    "15-band": [
        {"name": "平坦",     "bands": [0.0]*15},
        {"name": "低音增强", "bands": [10.0, 8.0, 6.0, 4.0, 2.0, 0.0, -1.0, -1.0, -1.0, -1.0, -2.0, -2.0, -2.0, -2.0, -2.0]},
        {"name": "人声突出", "bands": [-4.0, -3.0, -2.0, -1.0, 1.0, 3.0, 5.0, 5.0, 5.0, 4.0, 3.0, 2.0, 1.0, 0.0, -1.0]},
        {"name": "电子音乐", "bands": [6.0, 5.0, 4.0, 2.0, 0.0, -2.0, -2.0, -1.0, 0.0, 1.0, 3.0, 4.0, 5.0, 5.0, 4.0]},
        {"name": "摇滚",     "bands": [5.0, 4.0, 3.0, 2.0, 1.0, -1.0, -2.0, -1.0, 1.0, 2.0, 3.0, 4.0, 5.0, 5.0, 4.0]},
        {"name": "爵士",     "bands": [3.0, 2.0, 2.0, 1.0, 2.0, 3.0, 3.0, 2.0, 2.0, 2.0, 3.0, 3.0, 4.0, 4.0, 3.0]},
        {"name": "原声",     "bands": [3.0, 2.0, 2.0, 2.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 2.0, 2.0, 2.0]},
    ],
}


def get_presets_for_mode(mode: str) -> list[dict[str, Any]]:
    """返回指定 mode 的预设列表。未知 mode 回退到 3-band。"""
    return EQ_PRESETS.get(mode, EQ_PRESETS["3-band"])


def get_preset_by_name(mode: str, name: str) -> list[float] | None:
    """按名称查找预设的 bands。找不到返回 None。"""
    for p in get_presets_for_mode(mode):
        if p["name"] == name:
            return list(p["bands"])
    return None
