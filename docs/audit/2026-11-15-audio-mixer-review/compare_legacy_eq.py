"""Compare saved HPF parameters against the pre-maturity repository revision.

Loads one historical source file under a private module name, without changing
the working tree or replacing the production module. Requires git history.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from src.features.edit.core import audio_dsp
from src.features.edit.core.timeline_v2 import StripEQParams


def main() -> None:
    revision = "df749759"
    path = "src/features/edit/core/audio_dsp.py"
    source = subprocess.check_output(["git", "show", f"{revision}:{path}"],
                                      cwd=ROOT, text=True)
    legacy = ModuleType("topos_audit_legacy_audio_dsp")
    sys.modules[legacy.__name__] = legacy
    exec(compile(source, f"git:{revision}:{path}", "exec"), legacy.__dict__)
    sample_rate = 48000.0
    signal = np.stack([np.sin(2.0 * np.pi * 250.0 * np.arange(48000) / sample_rate)] * 2)
    params = StripEQParams(enabled=True, bands=({
        "enabled": True, "solo": False, "index": 1, "filter_type": "HPF",
        "freq": 1000.0, "gain": 0.0, "q": 0.7071, "slope": 12,
    },))
    levels = {}
    for name, module in ((revision, legacy), ("working_tree", audio_dsp)):
        effect = module.StripEQFilter(sample_rate)
        effect.set_params(params)
        output = effect.process(signal.copy())
        levels[name] = float(20.0 * np.log10(np.sqrt(np.mean(output[:, 24000:] ** 2))))
    print(json.dumps({"input_frequency_hz": 250.0, "input_peak": 1.0,
                      "sample_rate": sample_rate, "cutoff_hz": 1000.0,
                      "nominal_slope_db_oct": 12, "steady_rms_dbfs": levels,
                      "difference_db": levels["working_tree"] - levels[revision]},
                     indent=2))


if __name__ == "__main__":
    main()
