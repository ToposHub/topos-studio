"""H12：参数合法性、自动化 eligibility 与校验 API（复审 §5 H12）。

修前复现：
- ``VoiceChangerParams.from_dict`` 保留 NaN 增益 → 处理 1920×2 产生
  3840 个非有限输出；
- 坏 seed 字符串直接 ValueError；
- 仅按 int/float 判自动化会让 seed 等元数据进入曲线 lane。

修后契约：
- 两款新 FX 的 ``from_dict`` 浮点字段非有限值（NaN/±Inf）→ 默认，
  seed 宽容解析（坏值 → 0）；数值范围由 DSP ``np.clip`` 统一兜底；
- ``automation_param_kind``：never（seed/enabled/version）不进数值
  lane（采样消费端强制跳过）；event（routing/模式/曲线/Freeze）与
  continuous 分类供 UI 白名单共用；
- ``validate_effect_params``：预览/导出/保存前统一校验，返回净化
  dict + 逐字段诊断（原始 payload 经 extras 保留）。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    EffectChainParams,
    SpectralManglerParams,
    TrackEffectInstance,
    VoiceChangerParams,
    automation_param_kind,
    build_track_fx_sampler,
    validate_effect_params,
)
from src.shared.keyframe import Keyframe, KeyframeTrack

pytestmark = pytest.mark.edit_core


class TestSanitization:
    def test_nan_inf_fall_back_to_defaults(self):
        p = VoiceChangerParams.from_dict({
            "output_gain_db": float("nan"),
            "pitch_semitones": float("inf"),
            "formant_shift_st": float("-inf"),
        })
        assert p.output_gain_db == 0.0
        assert p.pitch_semitones == 0.0
        assert p.formant_shift_st == 0.0
        q = SpectralManglerParams.from_dict({
            "ring_carrier_hz": float("nan"),
            "grain_density_hz": float("inf"),
        })
        assert q.ring_carrier_hz == 340.0
        assert q.grain_density_hz == 24.0

    def test_bad_seed_string_no_exception(self):
        p = VoiceChangerParams.from_dict({"random_seed": "not-a-seed"})
        assert p.random_seed == 0
        q = SpectralManglerParams.from_dict({"random_seed": 3.9})
        assert isinstance(q.random_seed, int)

    def test_nan_never_reaches_dsp(self):
        from src.features.edit.core.audio_dsp import EffectChain
        chain = EffectChain(48000.0)
        chain.set_params([("voice_changer",
                           VoiceChangerParams.from_dict(
                               {"output_gain_db": float("nan")}).to_dict(),
                           True, "V", 4)], sample_rate=48000.0)
        y = np.asarray(chain.process(
            np.ones((2, 1920), dtype=np.float32) * 0.3))
        assert np.isfinite(y).all()

    def test_field_matrix(self):
        """全字段 NaN/Inf/错类型矩阵：from_dict 后 to_dict 全有限。"""
        base = VoiceChangerParams().to_dict()
        polluted = {k: (float("nan") if isinstance(v, float) else
                        ("garbage" if isinstance(v, str) else -1))
                    for k, v in base.items()}
        polluted["random_seed"] = "?"
        p = VoiceChangerParams.from_dict(polluted)
        for k, v in p.to_dict().items():
            if isinstance(v, float):
                assert math.isfinite(v), k


class TestAutomationEligibility:
    def test_kinds(self):
        assert automation_param_kind("random_seed") == "never"
        assert automation_param_kind("enabled") == "never"
        assert automation_param_kind("algorithm_version") == "never"
        assert automation_param_kind("routing") == "event"
        assert automation_param_kind("grain_mode") == "event"
        assert automation_param_kind("output_gain_db") == "continuous"
        assert automation_param_kind("pitch_semitones") == "continuous"

    def test_never_lane_skipped_in_sampler(self):
        d = VoiceChangerParams().to_dict()
        inst = TrackEffectInstance("voice_changer", d, instance_id="V")
        c_seed = KeyframeTrack(param_name="fx/V/random_seed",
                               min_val=0, max_val=2 ** 31)
        c_seed.add_keyframe(Keyframe(frame=0, value=12345))
        c_gain = KeyframeTrack(param_name="fx/V/output_gain_db",
                               min_val=-20, max_val=20)
        c_gain.add_keyframe(Keyframe(frame=0, value=-6.0))
        track = EditTrack("T", "T", "audio", 0,
                          clips=(ClipInstance("c", "a", "T", 0, 10, 0, 10),),
                          effect_chain=EffectChainParams(effects=(inst,)),
                          automation_mode="read",
                          automation={"fx/V/random_seed": c_seed,
                                      "fx/V/output_gain_db": c_gain})
        entry = build_track_fx_sampler(track)(0)[0]
        assert entry[1]["random_seed"] == 0        # never：曲线被跳过
        assert entry[1]["output_gain_db"] == -6.0  # continuous：应用


class TestValidateEffectParams:
    def test_nan_reported_and_cleaned(self):
        cleaned, diags = validate_effect_params("voice_changer", {
            "output_gain_db": float("nan"),
            "vocoder_carrier": "bogus",
            "future_x": 5,
        })
        assert math.isfinite(cleaned["output_gain_db"])
        assert cleaned["vocoder_carrier"] == "saw"
        assert cleaned["future_x"] == 5           # 未知键原样保留
        keys = {d[0] for d in diags}
        assert "output_gain_db" in keys
        assert "vocoder_carrier" in keys

    def test_clean_params_no_diagnostics(self):
        base = VoiceChangerParams(pitch_semitones=3.0).to_dict()
        cleaned, diags = validate_effect_params("voice_changer", base)
        assert diags == []
        assert cleaned["pitch_semitones"] == 3.0

    def test_unknown_type_opaque_passthrough(self):
        cleaned, diags = validate_effect_params(
            "mystery_fx", {"whatever": float("nan")})
        assert list(cleaned.keys()) == ["whatever"]
        assert math.isnan(cleaned["whatever"])  # 原样保留（NaN != NaN）
        assert diags == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
