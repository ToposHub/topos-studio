"""B15/B16/B17/B18/B19 纯算法核心回归（各手册条款 oracle）。

- B15：remap_sample 手册 oracle（40→240、100→None、180→30、歧义报错）、
  帧域→样本域 spans（23.976 绝对边界）、事务校验（保护/删除冲突拒绝）。
- B16：accept_block_reply 三元身份拒绝矩阵；隔离登记/解除。
- B17：MTC 完整序列组装 + 错序不发布；反馈去环（回声/重复/容量）；
  SyncState 机（acquiring→locked→freewheel→lost→恢复）。
- B18：chna UID 校验（含 numUIDs>numTracks 合法语义）+ chunk 往返。
- B19：环回延迟（233 样本手册 oracle）、带限探针电平、patch 冲突。
"""
from __future__ import annotations

from collections import OrderedDict
from fractions import Fraction

import numpy as np
import pytest

from src.features.edit.core.pro_audio_oracles import (
    BlockReply,
    MtcAssembler,
    QuarantineRegistry,
    SyncState,
    SyncStateMachine,
    accept_block_reply,
    bandlimited_probe,
    estimate_roundtrip_samples,
    pack_chna_chunk,
    parse_chna_chunk,
    should_apply,
    validate_patch_claims,
    validate_track_uids,
)
from src.features.edit.core.reconform import ReconformChange, ShotRef
from src.features.edit.core.reconform_transaction import (
    KeptSpan,
    plan_kept_spans,
    remap_sample,
    validate_reconform_transaction,
)


# ─────────────────────────────────────────────────────────────────────────────
# B15
# ─────────────────────────────────────────────────────────────────────────────

class TestReconformRemap:
    def test_manual_oracle(self) -> None:
        spans = (KeptSpan(0, 100, 200), KeptSpan(150, 300, 0))
        assert remap_sample(40, spans) == 240
        assert remap_sample(100, spans) is None   # 删除区段不吸附
        assert remap_sample(180, spans) == 30
        with pytest.raises(ValueError, match="ambiguous"):
            remap_sample(160, (KeptSpan(0, 200, 0), KeptSpan(100, 300, 500)))

    def test_frame_to_sample_spans(self) -> None:
        """keep/move/delete → spans；23.976 下帧边界精确闭合。"""
        fps = Fraction(24000, 1001)
        shot = ShotRef(shot_id="A", source_tc_frames=0, duration_frames=24)
        changes = (
            ReconformChange(kind="keep", old_shot=shot, old_start=0,
                            new_start=48),
            ReconformChange(kind="delete", old_start=24),
            ReconformChange(kind="move", old_shot=shot, old_start=48,
                            new_start=0),
        )
        spans = plan_kept_spans(changes, sample_rate=48000, fps=fps)
        assert len(spans) == 2
        # 23.976 @48k 每帧精确 2002 样本 → 24 帧 = 48048（整数边界）
        assert spans[0].old_start == 0
        assert spans[0].old_end == 48048
        assert spans[0].new_start == 48048 * 2  # new_start=48 帧
        # 删除区段 [48048, 96096) 无 span → remap None
        assert remap_sample(60000, spans) is None
        # move 段映射连续
        assert remap_sample(96096, spans) == 0

    def test_transaction_rejects_protected_split(self) -> None:
        spans = (KeptSpan(0, 1000, 0), KeptSpan(1000, 2000, 1000))
        # 保护区间 [800, 1200) 会被 span 边界切碎 → 拒绝
        errors = validate_reconform_transaction(
            spans, protected_ranges=[(800, 1200)])
        assert any("protected" in e for e in errors)
        # 删除与保留重叠 → 拒绝
        errors = validate_reconform_transaction(spans,
                                                deleted_ranges=[(900, 1100)])
        assert any("deleted" in e for e in errors)
        # 无冲突 → 通过
        assert validate_reconform_transaction(
            spans, protected_ranges=[(0, 1000)]) == []


# ─────────────────────────────────────────────────────────────────────────────
# B16
# ─────────────────────────────────────────────────────────────────────────────

class TestHostBlockReply:
    def test_manual_oracle_matrix(self) -> None:
        assert accept_block_reply(BlockReply(3, 10, 256), 3, 10, 256)
        assert not accept_block_reply(BlockReply(2, 10, 256), 3, 10, 256)
        assert not accept_block_reply(BlockReply(3, 9, 256), 3, 10, 256)
        assert not accept_block_reply(BlockReply(3, 10, 255), 3, 10, 256)

    def test_quarantine_registry(self) -> None:
        reg = QuarantineRegistry()
        reg.quarantine("fp-1", "scan timeout")
        assert reg.is_quarantined("fp-1")
        assert not reg.release("fp-other")
        assert reg.release("fp-1")
        assert not reg.is_quarantined("fp-1")
        # 容量上限淘汰最旧
        reg = QuarantineRegistry(max_entries=2)
        reg.quarantine("a", "x")
        reg.quarantine("b", "x")
        reg.quarantine("c", "x")
        assert not reg.is_quarantined("a")
        assert reg.is_quarantined("b") and reg.is_quarantined("c")


# ─────────────────────────────────────────────────────────────────────────────
# B17
# ─────────────────────────────────────────────────────────────────────────────

class TestMtcAssembler:
    def _feed_position(self, assembler: MtcAssembler, hours: int,
                       minutes: int, seconds: int, frames: int,
                       fps_code: int = 0):
        out = None
        quarters = [
            frames & 0x0F, (frames >> 4) & 0x0F,
            seconds & 0x0F, (seconds >> 4) & 0x0F,
            minutes & 0x0F, (minutes >> 4) & 0x0F,
            (hours & 0x1F) | (fps_code << 5), (hours >> 5) & 0x0F,
        ]
        for i, nibble in enumerate(quarters):
            out = assembler.feed_quarter_frame((i << 4) | nibble)
        return out

    def test_complete_sequence_publishes_position(self) -> None:
        asm = MtcAssembler()
        result = self._feed_position(asm, hours=1, minutes=2, seconds=3,
                                     frames=20, fps_code=0)
        assert result is not None
        assert result["position_frames"] == (3600 + 120 + 3) * 24 + 20
        assert result["fps_code"] == 0

    def test_out_of_order_does_not_publish(self) -> None:
        asm = MtcAssembler()
        # 顺序 0, 2（跳过 1）
        assert asm.feed_quarter_frame(0x00) is None
        assert asm.feed_quarter_frame((2 << 4) | 0x0) is None  # resync
        # 重发完整序列后恢复
        result = self._feed_position(asm, 0, 0, 0, 0)
        assert result is not None

    def test_duplicate_index_resyncs(self) -> None:
        asm = MtcAssembler()
        assert asm.feed_quarter_frame(0x01) is None
        assert asm.feed_quarter_frame(0x01) is None  # 重复 0
        result = self._feed_position(asm, 0, 0, 0, 10)
        assert result is not None and result["frames"] == 10


class TestFeedbackDedup:
    def test_manual_oracle(self) -> None:
        seen: OrderedDict = OrderedDict()
        assert should_apply("desk", 41, "app", seen, now=100.0)
        assert not should_apply("desk", 41, "app", seen, now=101.0)
        assert not should_apply("app", 42, "app", seen, now=101.0)

    def test_window_expiry_allows_new_event(self) -> None:
        seen: OrderedDict = OrderedDict()
        assert should_apply("desk", 41, "app", seen, now=100.0)
        assert not should_apply("desk", 41, "app", seen, now=120.0)
        assert should_apply("desk", 41, "app", seen, now=200.0)  # 窗外合法

    def test_capacity_bound(self) -> None:
        seen: OrderedDict = OrderedDict()
        for i in range(300):
            should_apply("desk", i, "app", seen, now=100.0)
        assert len(seen) <= 256


class TestSyncStateMachine:
    def test_acquiring_then_locked(self) -> None:
        sm = SyncStateMachine(lock_after_s=2.0, freewheel_after_s=0.5,
                              lost_after_s=5.0)
        assert sm.observe(0.0) == SyncState.ACQUIRING
        assert sm.observe(1.0) == SyncState.ACQUIRING
        assert sm.observe(2.5) == SyncState.LOCKED

    def test_freewheel_then_lost_then_reacquire(self) -> None:
        sm = SyncStateMachine(lock_after_s=0.5, freewheel_after_s=0.5,
                              lost_after_s=5.0)
        sm.observe(0.0)
        sm.observe(1.0)  # locked
        assert sm.poll(1.3) == SyncState.LOCKED       # 断流 <0.5s
        assert sm.poll(2.0) == SyncState.FREEWHEEL    # 1s 无观测
        assert sm.poll(10.0) == SyncState.LOST        # 9s 无观测
        # 恢复观测 → 重新 acquiring（poll 只处理无观测超时降级）
        assert sm.observe(12.0) == SyncState.ACQUIRING


# ─────────────────────────────────────────────────────────────────────────────
# B18
# ─────────────────────────────────────────────────────────────────────────────

class TestChna:
    def test_manual_oracle(self) -> None:
        validate_track_uids([("ATU_00000001", 0), ("ATU_00000002", 1)], 2)
        # 不同时间的 UID 可复用同一 PCM 轨（numUIDs > numTracks 合法）
        validate_track_uids([("ATU_00000001", 0), ("ATU_00000002", 0)], 2)
        with pytest.raises(ValueError, match="duplicate"):
            validate_track_uids([("ATU_00000001", 0), ("ATU_00000001", 1)], 2)
        with pytest.raises(ValueError, match="shape"):
            validate_track_uids([("ATU_ZZZZ", 0)], 2)
        with pytest.raises(ValueError, match="range"):
            validate_track_uids([("ATU_00000001", 2)], 2)
        with pytest.raises(ValueError, match="count"):
            validate_track_uids([("ATU_00000001", 0)], 0)

    def test_chna_chunk_roundtrip(self) -> None:
        uids = [("ATU_00000001", 0), ("ATU_00000002", 1),
                ("ATU_00000003", 0)]  # numUIDs(3) > numTracks(2) 合法
        payload = pack_chna_chunk(2, uids)
        num_tracks, parsed = parse_chna_chunk(payload)
        assert num_tracks == 2
        assert parsed == uids  # 磁盘 trackIndex 1 起 → 解析回 0 起

    def test_chna_truncated_rejected(self) -> None:
        payload = pack_chna_chunk(1, [("ATU_00000001", 0)])
        with pytest.raises(ValueError, match="truncated"):
            parse_chna_chunk(payload[:-8])


# ─────────────────────────────────────────────────────────────────────────────
# B19
# ─────────────────────────────────────────────────────────────────────────────

class TestRoundtripEstimation:
    def test_manual_oracle(self) -> None:
        sent = np.random.default_rng(19).normal(size=127)
        returned = np.r_[np.zeros(233), sent, np.zeros(100)]
        assert estimate_roundtrip_samples(sent, returned) == 233

    def test_probe_is_bandlimited_and_low_level(self) -> None:
        probe = bandlimited_probe(48000, 0.05, seed=7)
        assert float(np.max(np.abs(probe))) <= 0.051
        spec = np.abs(np.fft.rfft(probe.astype(np.float64)))
        freqs = np.fft.rfftfreq(len(probe), d=1.0 / 48000)
        out_of_band = float(np.max(spec[(freqs < 150) | (freqs > 8500)]))
        in_band = float(np.max(spec[(freqs > 300) & (freqs < 7500)]))
        assert out_of_band < in_band * 0.05

    def test_polarity_inverted_detected(self) -> None:
        sent = bandlimited_probe(48000, 0.02, seed=3)
        returned = np.r_[np.zeros(500), -sent]  # 极性反转
        lag = estimate_roundtrip_samples(sent, returned)
        assert lag == 500
        # 极性检查由消费方在 lag 处比较符号
        assert returned[lag:lag + sent.size][0] < 0 < sent[0]

    def test_patch_conflicts(self) -> None:
        claims = {"program": (0, 1), "cue1": (2,), "insert": (3,)}
        assert validate_patch_claims(claims) == []
        claims["cue2"] = (1, 2)
        conflicts = validate_patch_claims(claims)
        assert len(conflicts) == 2
        assert "channel 1" in conflicts[0]
