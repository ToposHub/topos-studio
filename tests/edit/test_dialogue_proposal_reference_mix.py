"""B13/B20 核心回归：对白候选闭环 与 参考mix对齐/匹配增益。

- B13：候选往返、revision/指纹校验、低置信度对齐不自动应用、对齐写
  既有偏移字段（右移 audio_sample_offset / 左移 source_sample_offset）、
  单命令单 undo；对齐 oracle ±37/±239 样本；RoomTone 引用不含 PCM。
- B20：延迟 1001 样本 reference 对齐 ≤1 样本；-6dB 副本匹配增益；
  静音/非有限测量禁用；提升上限拒绝；MixVersion 冻结登记。
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from src.features.edit.core.dialogue_proposal import (
    ALIGNMENT_AUTO_APPLY_CONFIDENCE,
    ApplyDialogueProposalCommand,
    DialogueProposal,
    ProposalOperation,
    RecorderSourceIdentity,
    RoomToneRef,
    apply_alignment_to_clip,
    validate_proposal,
)
from src.features.edit.core.dialogue_tools import estimate_alignment_offset
from src.features.edit.core.reference_mix import (
    ComparisonState,
    MixVersion,
    ReferenceMixRef,
    align_reference_offset,
    comparison_gain,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)


# ─────────────────────────────────────────────────────────────────────────────
# B13
# ─────────────────────────────────────────────────────────────────────────────

class TestDialogueProposal:
    def test_roundtrip(self) -> None:
        proposal = DialogueProposal(
            proposal_id="p1", project_revision=7,
            source_fingerprints=("fp-a", "fp-b"),
            operations=(ProposalOperation(
                op_type="align", track_id="adr", clip_id="c1",
                offset_samples=-37, confidence=0.9),),
            warnings=("low energy tail",),
        )
        restored = DialogueProposal.from_dict(proposal.to_dict())
        assert restored == proposal

    def test_revision_mismatch_rejected(self) -> None:
        proposal = DialogueProposal(proposal_id="p", project_revision=3,
                                    source_fingerprints=("fp",),
                                    operations=())
        errors = validate_proposal(proposal, current_revision=4,
                                   current_fingerprints=("fp",))
        assert errors and "revision" in errors[0]

    def test_fingerprint_change_rejected(self) -> None:
        proposal = DialogueProposal(proposal_id="p", project_revision=3,
                                    source_fingerprints=("fp-old",),
                                    operations=())
        errors = validate_proposal(proposal, current_revision=3,
                                   current_fingerprints=("fp-new",))
        assert any("fingerprint" in e for e in errors)

    def test_low_confidence_alignment_not_auto_applied(self) -> None:
        proposal = DialogueProposal(
            proposal_id="p", project_revision=1, source_fingerprints=("fp",),
            operations=(
                ProposalOperation(op_type="align", track_id="adr",
                                  clip_id="good", offset_samples=10,
                                  confidence=0.9),
                ProposalOperation(op_type="align", track_id="adr",
                                  clip_id="bad", offset_samples=999,
                                  confidence=0.2),
            ))
        errors = validate_proposal(proposal, current_revision=1,
                                   current_fingerprints=("fp",))
        assert any("bad" in e and "confidence" in e for e in errors)
        cmd, skipped = ApplyDialogueProposalCommand.create(proposal, "adr")
        assert dict(cmd.align_offsets) == {"good": 10}
        assert skipped and "bad" in skipped[0]

    def test_alignment_oracle_offsets(self) -> None:
        """手册 oracle：同源延迟 ±37 / ±239 样本，估计误差 ≤1 样本。

        estimate_alignment_offset 要求参考严格长于 take（滑动搜索窗）；
        参考取 1.5s、take 取其中 1s 的延迟片段。返回语义 "+ = 右移"：
        take 晚 37 → 应左移 37 → offset = −37。
        """
        rng = np.random.default_rng(13)
        rate = 48000
        source = rng.standard_normal(int(rate * 1.5))
        for delay in (37, 239):
            take = np.r_[np.zeros(delay),
                         source[:rate - delay]]
            ref = source[:int(rate * 1.5)]
            est = estimate_alignment_offset(ref, take, rate,
                                            max_offset_ms=100.0)
            assert est is not None
            # take 内容相对参考晚 delay 样本 → 右移修正为负
            assert abs(est.offset_samples + delay) <= 1, (delay, est)
            # 噪声 fixture 的相关置信度约 0.8（幅值受重叠比例影响）——
            # 断言达到自动应用门槛，不是虚假峰
            assert est.confidence >= ALIGNMENT_AUTO_APPLY_CONFIDENCE

    def test_silence_and_pure_tone_rejected_auto_align(self) -> None:
        rate = 48000
        silence = np.zeros(rate, dtype=np.float64)
        est = estimate_alignment_offset(silence, silence.copy(), rate)
        assert est is None  # 全静音不出建议
        t = np.arange(rate) / rate
        tone = np.sin(2 * np.pi * 1000.0 * t)
        est = estimate_alignment_offset(tone, np.r_[np.zeros(37), tone[:-37]],
                                        rate, max_offset_ms=100.0)
        # 纯周期音即使出候选也是低置信/歧义——不满足自动门槛
        assert est is None or est.confidence < ALIGNMENT_AUTO_APPLY_CONFIDENCE

    def test_apply_alignment_both_directions_with_undo(self) -> None:
        clip_a = ClipInstance(clip_id="a", asset_id="x", track_id="adr",
                              timeline_start=0, timeline_duration=1000,
                              source_start=0, source_duration=1000)
        clip_b = ClipInstance(clip_id="b", asset_id="x", track_id="adr",
                              timeline_start=2000, timeline_duration=1000,
                              source_start=0, source_duration=1000)
        track = EditTrack("adr", "ADR", "audio", 0, clips=(clip_a, clip_b))
        timeline = TimelineSequence(audio_tracks=(track,))
        # clip_a 右移 37（延迟字段）；clip_b 左移 239（源侧修剪）
        proposal = DialogueProposal(
            proposal_id="p", project_revision=timeline.revision,
            source_fingerprints=("fp",),
            operations=(
                ProposalOperation(op_type="align", track_id="adr",
                                  clip_id="a", offset_samples=37,
                                  confidence=0.95),
                ProposalOperation(op_type="align", track_id="adr",
                                  clip_id="b", offset_samples=-239,
                                  confidence=0.95),
            ))
        cmd, _skipped = ApplyDialogueProposalCommand.create(proposal, "adr")
        new_timeline, applied = cmd.apply(timeline)
        nt = new_timeline.get_track_by_id("adr")
        by_id = {c.clip_id: c for c in nt.clips}
        assert by_id["a"].audio_sample_offset == 37
        assert by_id["b"].source_sample_offset == 239
        restored = applied.undo(new_timeline)
        rt = restored.get_track_by_id("adr")
        by_id = {c.clip_id: c for c in rt.clips}
        assert by_id["a"].audio_sample_offset == 0
        assert by_id["b"].source_sample_offset == 0

    def test_room_tone_ref_no_pcm_and_identity_unknown_preserved(self) -> None:
        ref = RoomToneRef(asset_id="tone", source_start_sample=0,
                          source_end_sample=48000)
        assert not hasattr(ref, "pcm")
        identity = RecorderSourceIdentity.from_dict({"asset_id": "rec1"})
        assert identity.bwf_time_reference is None      # unknown 不缺省
        assert identity.recorded_date == ""             # 不缺省为今天
        full = RecorderSourceIdentity(
            asset_id="rec1", bwf_time_reference=480000, scene="SC1",
            take="T3", recorded_date="2026-11-22", source_rate=48000)
        assert full.to_dict()["bwf_time_reference"] == 480000


# ─────────────────────────────────────────────────────────────────────────────
# B20
# ─────────────────────────────────────────────────────────────────────────────

class TestReferenceMix:
    def test_manual_comparison_gain_oracle(self) -> None:
        assert math.isclose(comparison_gain(-20.0, -23.0),
                            10.0 ** (-3.0 / 20.0))
        with pytest.raises(ValueError):
            comparison_gain(float("nan"), -23.0)
        with pytest.raises(ValueError):
            comparison_gain(-30.0, -14.0)  # 需要 +16dB 超上限

    def test_delayed_reference_aligned_within_one_sample(self) -> None:
        """手册 fixture：延迟 1001 样本 reference；对齐误差 ≤1 样本。"""
        rng = np.random.default_rng(19)
        n = 48000
        prog = rng.standard_normal(n).astype(np.float64) * 0.2
        delayed = np.r_[np.zeros(1001), prog[:n - 1001]]
        lag, confidence = align_reference_offset(prog, delayed,
                                                 max_offset_samples=48000)
        assert abs(lag - 1001) <= 1
        assert confidence > 0.9

    def test_attenuated_copy_match_gain(self) -> None:
        """整体 -6dB 副本：比较增益应恢复 0dB 差（峰值域 oracle）。"""
        rng = np.random.default_rng(20)
        prog = rng.standard_normal((2, 48000)).astype(np.float64) * 0.3
        ref = prog * 10 ** (-6.0 / 20.0)
        # 用未加权的 RMS 能量差作为"测量"（B05 完整 LUFS 口径另测）：
        # match_gain_db = 20log10(rms_prog / rms_ref) ≈ +6dB
        rms_prog = float(np.sqrt(np.mean(prog ** 2)))
        rms_ref = float(np.sqrt(np.mean(ref ** 2)))
        match_db = 20.0 * math.log10(rms_prog / rms_ref)
        assert match_db == pytest.approx(6.0, abs=0.05)
        gain = comparison_gain(-26.0, -20.0)  # LUFS 差 +6dB 的线性倍率
        assert float(np.max(np.abs(ref * gain))) == pytest.approx(
            float(np.max(np.abs(prog))), rel=1e-3)

    def test_reference_isolation_state_not_in_project(self) -> None:
        """ComparisonState 是监听会话态：可变、不进 undo/序列化。"""
        state = ComparisonState(active_source="ref-1", match_gain_db=-6.0)
        state.active_source = ""  # A/B 切换不改工程
        assert not hasattr(state, "to_dict") or state.measurement_key == ""
        ref = ReferenceMixRef(version_id="ref-1", asset_id="a1")
        assert ref.provenance == ""  # 出处字段显式（审计）

    def test_mix_version_freeze_registration(self) -> None:
        version = MixVersion(version_id="v1", source_revision=42,
                             render_spec_hash="spec-abc", asset_id="print-1",
                             measured_report_ref="qc-1")
        restored = MixVersion.from_dict(version.to_dict())
        assert restored.source_revision == 42
        assert restored.render_spec_hash == "spec-abc"
        with pytest.raises(ValueError):
            ReferenceMixRef(version_id="", asset_id="a")
        with pytest.raises(ValueError):
            ReferenceMixRef(version_id="v", asset_id="")
