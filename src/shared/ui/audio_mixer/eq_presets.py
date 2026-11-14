"""参数化 EQ 预设（CW-A1 重构）。

预设是完整的 StripEQParams dict（enabled / voicing / master_gain / bands），
由 ParametricEQDialog 的预设下拉直接载入。频段结构与
timeline_v2.default_eq_bands_* 一致。
"""

from __future__ import annotations

from typing import Any


def _flat_bands() -> list[dict]:
    from src.features.edit.core.timeline_v2 import default_eq_bands_flat
    return [dict(b) for b in default_eq_bands_flat()]


PARAMETRIC_EQ_PRESETS: list[dict[str, Any]] = [
    {
        "name": "Vocal Presence",
        "params": {
            "enabled": True, "voicing": "Clean", "master_gain": 0.0,
            "bands": [
                {"index": 1, "enabled": True,  "solo": False, "filter_type": "HPF",        "range_key": "L",  "freq": 65.0,    "gain": 0.0,  "q": 1.1, "slope": 12},
                {"index": 2, "enabled": True,  "solo": False, "filter_type": "Low Shelf",  "range_key": "L",  "freq": 110.0,   "gain": -4.5, "q": 1.4, "slope": 12},
                {"index": 3, "enabled": True,  "solo": False, "filter_type": "Bell",       "range_key": "ML", "freq": 320.0,   "gain": -2.2, "q": 2.1, "slope": 12},
                {"index": 4, "enabled": True,  "solo": False, "filter_type": "Bell",       "range_key": "MH", "freq": 2400.0,  "gain": 3.4,  "q": 1.4, "slope": 12},
                {"index": 5, "enabled": True,  "solo": False, "filter_type": "High Shelf", "range_key": "H",  "freq": 8200.0,  "gain": 2.0,  "q": 1.2, "slope": 12},
                {"index": 6, "enabled": False, "solo": False, "filter_type": "LPF",        "range_key": "H",  "freq": 19500.0, "gain": 0.0,  "q": 1.1, "slope": 12},
            ],
        },
    },
    {
        "name": "Flat Neutral",
        "params": {"enabled": True, "voicing": "Clean", "master_gain": 0.0, "bands": _flat_bands()},
    },
    {
        "name": "Dialogue De-Mud",
        "params": {
            "enabled": True, "voicing": "Clean", "master_gain": 0.5,
            "bands": [
                {"index": 1, "enabled": True,  "solo": False, "filter_type": "HPF",        "range_key": "L",  "freq": 90.0,    "gain": 0.0,  "q": 1.1, "slope": 18},
                {"index": 2, "enabled": True,  "solo": False, "filter_type": "Bell",       "range_key": "L",  "freq": 230.0,   "gain": -3.5, "q": 2.4, "slope": 12},
                {"index": 3, "enabled": False, "solo": False, "filter_type": "Bell",       "range_key": "ML", "freq": 550.0,   "gain": 0.0,  "q": 1.1, "slope": 12},
                {"index": 4, "enabled": True,  "solo": False, "filter_type": "Bell",       "range_key": "MH", "freq": 3400.0,  "gain": 2.6,  "q": 1.6, "slope": 12},
                {"index": 5, "enabled": True,  "solo": False, "filter_type": "High Shelf", "range_key": "H",  "freq": 9000.0,  "gain": 1.5,  "q": 1.1, "slope": 12},
                {"index": 6, "enabled": False, "solo": False, "filter_type": "LPF",        "range_key": "H",  "freq": 18500.0, "gain": 0.0,  "q": 1.1, "slope": 12},
            ],
        },
    },
    {
        "name": "Radio / Lo-Fi",
        "params": {
            "enabled": True, "voicing": "Punch", "master_gain": 2.0,
            "bands": [
                {"index": 1, "enabled": True,  "solo": False, "filter_type": "HPF",        "range_key": "L",  "freq": 360.0,   "gain": 0.0,  "q": 1.1, "slope": 24},
                {"index": 2, "enabled": False, "solo": False, "filter_type": "Low Shelf",  "range_key": "L",  "freq": 80.0,    "gain": 0.0,  "q": 1.1, "slope": 12},
                {"index": 3, "enabled": True,  "solo": False, "filter_type": "Bell",       "range_key": "ML", "freq": 1200.0,  "gain": 5.0,  "q": 2.0, "slope": 12},
                {"index": 4, "enabled": False, "solo": False, "filter_type": "Bell",       "range_key": "MH", "freq": 2500.0,  "gain": 0.0,  "q": 1.1, "slope": 12},
                {"index": 5, "enabled": False, "solo": False, "filter_type": "High Shelf", "range_key": "H",  "freq": 6000.0,  "gain": 0.0,  "q": 1.1, "slope": 12},
                {"index": 6, "enabled": True,  "solo": False, "filter_type": "LPF",        "range_key": "H",  "freq": 3500.0,  "gain": 0.0,  "q": 1.1, "slope": 24},
            ],
        },
    },
]
