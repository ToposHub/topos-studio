"""H01 冻结基线：用真历史提交的 audio_dsp 渲染 EQ PCM 作真值。

用法（仓库根目录）：
    .venv/bin/python docs/audit/2026-11-15-audio-fx-post-integration/h01/freeze-historical-baseline.py

对每个关键历史提交（v2 引入 302b88b6、EQ-02 76ad5657、v3 e2131e65），
加载该提交的 ``audio_dsp.py`` 为独立模块，渲染：
  - HPF 1000Hz / 12dB / 250Hz 正弦（复审 §5 H01 已复现案例）
  - High Shelf 3kHz / +9dB / British voicing / 250Hz 正弦（EQ-02 敏感案例）
  - Delay 200ms @ FB 30%，块间改 delay 120→160ms（v3→v4 xfade 边界）
并把稳态 RMS 与前 N 个采样写入 baseline JSON，供回归比对。
不修改用户工程/预设；只生成临时合成音频。
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parent / "baseline-historical.json"
SR = 48000
COMMITS = ["302b88b6", "76ad5657", "1016a3cd", "e2131e65", "d9ba26c9"]


def load_historical(commit: str) -> types.ModuleType:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    src = subprocess.run(
        ["git", "-C", str(REPO), "show", f"{commit}:src/features/edit/core/audio_dsp.py"],
        capture_output=True, text=True, check=True).stdout
    mod = types.ModuleType(f"audio_dsp_{commit}")
    mod.__file__ = f"<git:{commit}:audio_dsp.py>"
    sys.modules[mod.__name__] = mod
    exec(compile(src, mod.__file__, "exec"), mod.__dict__)
    return mod


def rms_db(x: np.ndarray, skip: int) -> float:
    seg = np.asarray(x, dtype=np.float64)[:, skip:]
    return float(20.0 * np.log10(max(float(np.sqrt((seg ** 2).mean())), 1e-12)))


def render_eq(mod, band: dict, voicing: str, freq_hz: float) -> dict:
    """250Hz 正弦过单个 EQ 频段，返回稳态 RMS 与首 8 采样。"""
    t = np.arange(int(SR)) / SR
    sig = (0.5 * np.sin(2 * np.pi * freq_hz * t)).astype(np.float64)
    x = np.stack([sig, sig])
    fx = mod.StripEQFilter(float(SR))
    p = mod.StripEQParams.from_dict({
        "enabled": True, "voicing": voicing, "master_gain": 0.0,
        "bands": [dict(band, enabled=True, index=0)]})
    fx.set_params(p)
    y = fx.process(x)
    return {"rms_db": round(rms_db(y, SR // 2), 6),
            "head": [round(float(v), 7) for v in y[0, :8]]}


def render_delay(mod) -> dict:
    """块间改 delay：120ms→160ms，FB 30%，看切换语义（v4=xfade）。"""
    t = np.arange(1024) / SR
    pulse = np.zeros((2, 1024), dtype=np.float64)
    pulse[:, :8] = np.hanning(8)
    out = np.zeros((2, 0), dtype=np.float64)
    fx = mod.DelayEffect(float(SR))
    p = {"enabled": True, "left_delay_ms": 120.0, "right_delay_ms": 120.0,
         "feedback": 0.3, "mix": 0.5}
    fx.set_params(p)
    # 10 块 120ms → 3 块 160ms → 10 块 120ms
    blocks = [120.0] * 10 + [160.0] * 3 + [120.0] * 10
    for i, ms in enumerate(blocks):
        x = pulse if i == 0 else np.zeros((2, 1024), dtype=np.float64)
        if i == 10:
            fx.set_params({"enabled": True, "left_delay_ms": ms,
                           "right_delay_ms": ms, "feedback": 0.3, "mix": 0.5})
        if i == 13:
            fx.set_params({"enabled": True, "left_delay_ms": 120.0,
                           "right_delay_ms": 120.0, "feedback": 0.3,
                           "mix": 0.5})
        out = np.concatenate([out, fx.process(x)], axis=-1)
    return {"rms_db": round(rms_db(out, 0), 6),
            "tail_head": [round(float(v), 7) for v in out[0, 10240:10248]]}


def main() -> None:
    baseline = {}
    hpf = {"filter_type": "HPF", "freq": 1000.0, "gain": 0.0,
           "q": 1.1, "slope": 12}
    shelf = {"filter_type": "High Shelf", "freq": 3000.0, "gain": 9.0,
             "q": 1.1, "slope": 12}
    for commit in COMMITS:
        mod = load_historical(commit)
        baseline[commit] = {
            "hpf_clean_250hz": render_eq(mod, hpf, "Clean", 250.0),
            "shelf_british_250hz": render_eq(mod, shelf, "British", 250.0),
            "delay_switch": render_delay(mod),
        }
        print(commit, json.dumps(baseline[commit], ensure_ascii=False))
    OUT.write_text(json.dumps(baseline, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
