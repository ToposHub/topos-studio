"""B11/B14 核心回归：采集时钟/Punch/恢复日志 与 comp 区域/编译计划。

按主计划 §20 手册验证条款：
- Punch 块边界切片：blocks×256、punch [250,780) → 恰好 530 样本
  arange(250,780)；通道互不串漏。
- CaptureStream：缺块/重复标记 discontinuity，不丢样本不虚报。
- ClockAnchor：±100ppm 漂移 fixture（压力 fixture，非硬件容差规范）
  校正后锚误差 ≤1 参考样本；漂移超 ±1000ppm 拒绝拟合。
- CaptureJournal：崩溃半行/缺段 → recoverable_prefix 只报连续已确认段。
- Comp：replace_region 保留左右残片（手册 oracle）；100 次随机替换后
  仍有序/不重叠；缺 take 区段 offline 不换音；编译 crossfade 边界。
"""
from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from src.features.edit.core.audio_comp import (
    AudioComp,
    CompRegion,
    compile_comp_plan,
    replace_region,
    validate_comp_regions,
)
from src.features.edit.core.capture_clock import (
    CaptureJournal,
    CaptureStream,
    ClockAnchor,
    PunchRange,
    apply_punch,
    punch_slice,
)


# ─────────────────────────────────────────────────────────────────────────────
# B11 · Punch
# ─────────────────────────────────────────────────────────────────────────────

class TestPunch:
    def test_manual_oracle_blocks_and_punch(self) -> None:
        """手册 oracle：256 样本块 × 4，punch [250, 780) → arange(250, 780)。"""
        blocks = [np.arange(i, i + 256, dtype=np.float32)[None, :]
                  for i in range(0, 1024, 256)]
        recorded = apply_punch([(i * 256, b) for i, b in enumerate(blocks)],
                               250, 780)
        assert recorded.shape == (1, 780 - 250)
        np.testing.assert_array_equal(recorded[0], np.arange(250, 780))

    def test_punch_channel_isolation(self) -> None:
        rng = np.random.default_rng(5)
        block = rng.standard_normal((8, 1024)).astype(np.float32)
        out = punch_slice(block, 0, 100, 600)
        assert out.shape == (8, 500)
        np.testing.assert_array_equal(out, block[:, 100:600])

    def test_punch_disjoint_blocks(self) -> None:
        block = np.arange(0, 256, dtype=np.float32)[None, :]
        assert punch_slice(block, 0, 300, 400).shape[-1] == 0
        with pytest.raises(ValueError):
            punch_slice(block, 0, 500, 500)  # 空 punch

    def test_punch_range_validation(self) -> None:
        with pytest.raises(ValueError):
            PunchRange(start=10, end=10)


class TestCaptureStream:
    def test_continuous_stream(self) -> None:
        stream = CaptureStream("dev1", expected_channels=2)
        pos = 0
        for n in (257, 733, 1024):  # 任意块长
            block = np.zeros((2, n), dtype=np.float32)
            stream.accept(block, first_device_sample=pos)
            pos += n
        assert stream.is_continuous
        assert stream.total_samples == 257 + 733 + 1024

    def test_gap_and_duplicate_flagged(self) -> None:
        stream = CaptureStream("dev1")
        stream.accept(np.zeros((2, 256), np.float32), first_device_sample=0)
        # 缺块：跳到 512
        b = stream.accept(np.zeros((2, 256), np.float32), first_device_sample=512)
        assert b.discontinuity
        assert stream.gaps == [(256, 512)]
        # 重复：回退到 700
        b = stream.accept(np.zeros((2, 256), np.float32), first_device_sample=700)
        assert b.discontinuity
        assert stream.duplicates == [(768, 700)]
        assert stream.discontinuities == 2

    def test_implicit_sequencing(self) -> None:
        stream = CaptureStream("dev1")
        stream.accept(np.zeros((2, 100), np.float32))
        block = stream.accept(np.zeros((2, 100), np.float32))
        assert block.first_device_sample == 100  # 累计推进


class TestClockAnchor:
    def test_no_drift_mapping(self) -> None:
        anchor = ClockAnchor(reference_rate=48000, device_rate=48000)
        anchor.add_anchor(480000, 480000)
        anchor.add_anchor(960000, 960000)
        assert anchor.estimate_drift() == pytest.approx(1.0, abs=1e-12)
        assert anchor.timeline_sample_for_device(1440000) == 1440000

    def test_plus_100ppm_drift_corrected(self) -> None:
        """设备 480048/参考 480000 比例：锚误差 ≤1 参考样本。"""
        anchor = ClockAnchor(reference_rate=480000, device_rate=480048)
        anchor.add_anchor(0, 0)
        anchor.add_anchor(480048, 480000)
        slope = anchor.estimate_drift()
        assert slope == pytest.approx(480000 / 480048, abs=1e-9)
        assert anchor.timeline_sample_for_device(480048) == pytest.approx(
            480000, abs=1)
        assert anchor.timeline_sample_for_device(960096) == pytest.approx(
            960000, abs=1)

    def test_extreme_drift_rejected(self) -> None:
        anchor = ClockAnchor(reference_rate=48000, device_rate=48000)
        anchor.add_anchor(0, 0)
        anchor.add_anchor(48000, 60000)  # +25% 偏差 → 非漂移，锚点污染
        with pytest.raises(ValueError, match="sanity"):
            anchor.estimate_drift()


class TestCaptureJournal:
    def _block(self, seq: int, first: int, n: int = 256,
               seed: int = 0) -> np.ndarray:
        rng = np.random.default_rng(seed + seq)
        return rng.standard_normal((2, n)).astype(np.float32)

    def test_recoverable_prefix(self, tmp_path: Path) -> None:
        journal = CaptureJournal(tmp_path / "journal.jsonl", fsync_every=4)
        stream = CaptureStream("dev1")
        pos = 0
        for seq in range(6):
            block = stream.accept(self._block(seq, pos),
                                  first_device_sample=pos)
            pos += block.sample_count
            journal.record_segment(block)
            journal.commit_segment(seq)
        journal.close()
        count, samples = journal.recoverable_prefix()
        assert count == 6
        assert samples == 6 * 256

    def test_crash_half_line_and_gap(self, tmp_path: Path) -> None:
        """崩溃残留（半行 JSON）与缺段 → 只报连续前缀。"""
        path = tmp_path / "journal.jsonl"
        records = []
        for seq in range(4):
            records.append(json.dumps({
                "sequence_no": seq, "first_device_sample": seq * 256,
                "sample_count": 256, "crc32": 0}))
        records.append('{"sequence_no": 4, "first_dev')  # 半行（崩溃残留）
        records.append(json.dumps({
            "sequence_no": 6, "first_device_sample": 1536,
            "sample_count": 256, "crc32": 0}))  # seq 5 缺失
        path.write_text("\n".join(records) + "\n", encoding="utf-8")
        journal = CaptureJournal(path)
        count, samples = journal.recoverable_prefix()
        assert count == 4
        assert samples == 4 * 256

    def test_fsync_throttling(self, tmp_path: Path) -> None:
        journal = CaptureJournal(tmp_path / "j.jsonl", fsync_every=2)
        stream = CaptureStream("dev1")
        for seq in range(5):
            block = stream.accept(self._block(seq, seq * 256),
                                  first_device_sample=seq * 256)
            journal.record_segment(block)
        # commit 0、1 → 第 2 次 commit 触发 fsync（计数清零）
        for seq in range(5):
            journal.commit_segment(seq)
        assert journal.segments()[0].fsynced
        assert journal.segments()[3].fsynced
        journal.close()


# ─────────────────────────────────────────────────────────────────────────────
# B14 · Comp 区域与编译
# ─────────────────────────────────────────────────────────────────────────────

class TestCompRegions:
    def test_replace_region_manual_oracle(self) -> None:
        got = replace_region(
            (CompRegion(0, 1000, "A", 100),),
            CompRegion(300, 600, "B", 20))
        assert got == (
            CompRegion(0, 300, "A", 100),
            CompRegion(300, 600, "B", 20),
            CompRegion(600, 1000, "A", 700),  # 右残片源偏移 +300
        )

    def test_replace_exact_cover_and_edges(self) -> None:
        base = (CompRegion(0, 500, "A", 0), CompRegion(500, 1000, "B", 0))
        # 完全覆盖 A
        got = replace_region(base, CompRegion(0, 500, "C", 10))
        assert got == (CompRegion(0, 500, "C", 10),
                       CompRegion(500, 1000, "B", 0))
        # 边界外替换（不相交）不变
        got = replace_region(base, CompRegion(2000, 3000, "C", 0))
        assert got == base + (CompRegion(2000, 3000, "C", 0),)

    def test_random_replacements_stay_ordered_non_overlapping(self) -> None:
        rng = np.random.default_rng(42)
        regions: tuple = ()
        for _ in range(100):
            start = int(rng.integers(0, 100_000))
            end = start + int(rng.integers(1, 5000))
            regions = replace_region(
                regions, CompRegion(start, end, "T", start % 997))
            errors = validate_comp_regions(regions)
            assert not errors, errors
        assert len(regions) > 0

    def test_empty_region_rejected(self) -> None:
        with pytest.raises(ValueError):
            CompRegion(10, 10, "A", 0)

    def test_compile_plan_offsets_and_fades(self) -> None:
        comp = AudioComp(
            comp_id="c1", track_id="dx",
            regions=(
                CompRegion(0, 300, "A", 0, fade_in=10, fade_out=10),
                CompRegion(300, 600, "B", 0, fade_in=10, fade_out=10),
                CompRegion(600, 1000, "A", 300, fade_in=10, fade_out=10),
            ))
        plan, warnings = compile_comp_plan(
            comp, {"A": 5000, "B": 5000}, max_crossfade=64)
        assert not warnings
        assert set(plan) == {"A", "B"}
        # A take 两区段；源偏移与区段窗一致；请求的 crossfade 保留
        # （切换边界交叉淡化受区段长度与 max_crossfade 限制）
        a_regions = plan["A"]
        assert a_regions[0] == CompRegion(0, 300, "A", 0,
                                          fade_in=10, fade_out=10)
        assert a_regions[1] == CompRegion(600, 1000, "A", 300,
                                          fade_in=10, fade_out=10)
        b_region = plan["B"][0]
        assert b_region.fade_in == 10 and b_region.fade_out == 10

    def test_compile_fade_clamped_by_region_length(self) -> None:
        comp = AudioComp(comp_id="c", track_id="t", regions=(
            CompRegion(0, 20, "A", 0, fade_in=64, fade_out=64),
        ))
        plan, _w = compile_comp_plan(comp, {"A": 5000}, max_crossfade=64)
        # 区段长 20 → fade 各收敛到 ≤20
        assert plan["A"][0].fade_in == 20
        assert plan["A"][0].fade_out == 20

    def test_missing_take_renders_offline_not_substituted(self) -> None:
        comp = AudioComp(comp_id="c", track_id="t", regions=(
            CompRegion(0, 100, "A", 0),
            CompRegion(100, 200, "ghost", 0),
        ))
        plan, warnings = compile_comp_plan(comp, {"A": 500})
        assert set(plan) == {"A"}
        assert any("missing" in w and "ghost" in w for w in warnings)

    def test_short_take_offline(self) -> None:
        comp = AudioComp(comp_id="c", track_id="t", regions=(
            CompRegion(0, 1000, "A", 900),  # 源只剩 100 < 1000
        ))
        plan, warnings = compile_comp_plan(comp, {"A": 1000})
        assert not plan
        assert any("exceeds take" in w for w in warnings)

    def test_overlapping_regions_rejected(self) -> None:
        comp = AudioComp(comp_id="c", track_id="t", regions=(
            CompRegion(0, 500, "A", 0),
            CompRegion(400, 600, "B", 0),
        ))
        with pytest.raises(ValueError, match="overlaps"):
            compile_comp_plan(comp, {"A": 5000, "B": 5000})
