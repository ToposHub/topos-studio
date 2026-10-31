"""F3.3 · 采样级音频编辑与迁移——验收测试。

计划验收锚点：
- 48kHz 可移动/修剪一个 sample（nudge/slip 命令 + 渲染端 1 样本位移）；
- 非整数 fps 长序列无累积漂移（有理数时钟 vs 浮点逐步累积对照）；
- 旧工程迁移（无新字段→默认，字节级不写新键）、Undo、序列化精确往返、
  非法载荷防御回退（不静默舍入语义）；
- 采样域淡化在渲染端精确生效（预览 helper 与导出 renderer 同键）。
"""

from __future__ import annotations

import os
import sys
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.audio_dsp import (
    apply_clip_fade,
    linear_fade_envelope,
)
from src.features.edit.core.commands import UpdateClipSampleFieldsCommand
from src.features.edit.core.sample_time import (
    AUDIO_REFERENCE_SAMPLE_RATE,
    SampleSkipGate,
    TimelineSampleClock,
)
from src.features.edit.core.timeline_v2 import ClipInstance, Rational

pytestmark = pytest.mark.edit_core

SR = 48000


def _audio_seq(**clip_kw) -> object:
    from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence

    clip = ClipInstance(
        "c1", "a1", "t1", 0, 24, 0, 240,
        fade_in_duration=0, fade_out_duration=0, **clip_kw)
    track = EditTrack("t1", "A1", "audio", 0, clips=(clip,))
    return TimelineSequence(
        timeline_id="s", name="S", fps=Rational(24, 1),
        audio_tracks=(track,))


class TestRationalClock:
    def test_ntsc_frame_length_exact_rational(self) -> None:
        """29.97@48k：帧长 = 8008/5 样本（精确有理数，非 1601.6 浮点）。"""
        clock = TimelineSampleClock(Fraction(30000, 1001), SR)
        assert clock.samples_per_frame == Fraction(8008, 5)

    def test_long_sequence_no_accumulated_drift(self) -> None:
        """长序列（10 万帧）样本边界一次求值无漂移；浮点逐步累积对照
        演示误差（有理数路径必须与首尾闭合精确一致）。"""
        fps = Fraction(30000, 1001)
        clock = TimelineSampleClock(fps, SR)
        n = 100_000
        exact_end = clock.frame_start_sample(n)
        # 有理数闭合：边界差之和精确覆盖
        total = exact_end - clock.frame_start_sample(0)
        assert total == Fraction(SR, 1) * Fraction(n, 1) / fps
        # 浮点逐步累积（反例，float32——音频引擎常见精度）：长序列漂移
        # ≥1 样本；有理数路径与闭合精确一致（零漂移）。
        spf_float = np.float32(SR) / np.float32(30000 / 1001)
        acc = np.float32(0.0)
        for _ in range(n):
            acc = np.float32(acc + spf_float)
        assert abs(float(acc) - float(total)) > 0.5  # 浮点路径已漂移 ≥1 样本

    def test_block_bounds_close_exactly(self) -> None:
        """相邻样本边界差闭合：总样本 = 末-首，无孔洞/重叠（D06 同型）。"""
        clock = TimelineSampleClock(Fraction(30000, 1001), SR)
        bounds = clock.block_bounds(0, 240)
        assert len(bounds) == 241
        diffs = [b - a for a, b in zip(bounds, bounds[1:])]
        assert all(d >= 1 for d in diffs)
        assert sum(diffs) == bounds[-1]

    def test_position_exact_with_offset(self) -> None:
        """(帧, 整数偏移) 二元组精确表示任意子帧位置（含 1 样本）。"""
        clock = TimelineSampleClock(Fraction(24, 1), SR)
        assert clock.position_samples(10, 1) == Fraction(20001)


class TestClipFieldsAndMigration:
    def test_defaults_and_legacy_roundtrip(self) -> None:
        """旧工程（无新字段）→ 默认 0/None；序列化不写新键（字节兼容）。"""
        legacy = {
            "clip_id": "c", "asset_id": "a", "track_id": "t",
            "timeline_start": 0, "timeline_duration": 10,
            "source_start": 0, "source_duration": 10,
        }
        clip = ClipInstance.from_dict(legacy)
        assert clip.audio_sample_offset == 0
        assert clip.source_sample_offset == 0
        assert clip.fade_in_samples is None and clip.fade_out_samples is None
        out = clip.to_dict()
        assert out.get("audio_sample_offset") is None
        assert out.get("fade_in_samples") is None

    def test_exact_roundtrip_cross_platform(self) -> None:
        """新字段经 dict（JSON 语义）精确往返：整数样本无损失。"""
        clip = ClipInstance(
            "c", "a", "t", 0, 24, 0, 240,
            audio_sample_offset=1999, source_sample_offset=7,
            fade_in_samples=1001, fade_out_samples=2002)
        restored = ClipInstance.from_dict(clip.to_dict())
        assert restored == clip

    def test_invalid_payloads_fall_back_not_silently_rounded(self) -> None:
        """负值/浮点载荷回退默认（可检测），不静默接受伪样本语义。"""
        clip = ClipInstance.from_dict({
            "clip_id": "c", "asset_id": "a", "track_id": "t",
            "timeline_start": 0, "timeline_duration": 10,
            "source_start": 0, "source_duration": 10,
            "audio_sample_offset": -5,
            "fade_in_samples": 12.5,
        })
        assert clip.audio_sample_offset == 0
        assert clip.fade_in_samples is None


class TestSampleEditCommands:
    def test_move_by_one_sample_undo(self) -> None:
        """48kHz 可移动一个 sample：nudge 命令 + 单步 Undo。"""
        seq = _audio_seq()
        clip = seq.get_clip_by_id("c1")
        cmd = UpdateClipSampleFieldsCommand.create(
            "t1", clip, audio_sample_offset=1, label="Move 1 sample")
        applied, updated = cmd.apply(seq)
        assert applied.get_clip_by_id("c1").audio_sample_offset == 1
        undone = updated.undo(applied)
        assert undone.get_clip_by_id("c1").audio_sample_offset == 0

    def test_trim_in_by_one_sample(self) -> None:
        """源侧子帧修剪一个 sample（入点后跳 1 源样本）。"""
        seq = _audio_seq()
        clip = seq.get_clip_by_id("c1")
        cmd = UpdateClipSampleFieldsCommand.create(
            "t1", clip, source_sample_offset=1, label="Trim in 1 sample")
        applied, _ = cmd.apply(seq)
        assert applied.get_clip_by_id("c1").source_sample_offset == 1

    def test_fade_samples_set_and_clear(self) -> None:
        """采样域淡化设置/清除（None=回帧域）。"""
        seq = _audio_seq()
        clip = seq.get_clip_by_id("c1")
        cmd = UpdateClipSampleFieldsCommand.create(
            "t1", clip, fade_in_samples=480, fade_out_samples=960)
        applied, updated = cmd.apply(seq)
        got = applied.get_clip_by_id("c1")
        assert got.fade_in_samples == 480 and got.fade_out_samples == 960
        clear = UpdateClipSampleFieldsCommand.create(
            "t1", got, fade_in_samples=-1, fade_out_samples=-1)
        cleared, _ = clear.apply(applied)
        got2 = cleared.get_clip_by_id("c1")
        assert got2.fade_in_samples is None and got2.fade_out_samples is None

    def test_command_serializes_for_macro(self) -> None:
        """宏注册表可往返（_MACRO_SUBCOMMAND_TYPES 分发）。"""
        from src.features.edit.core.commands import _MACRO_SUBCOMMAND_TYPES

        seq = _audio_seq()
        clip = seq.get_clip_by_id("c1")
        cmd = UpdateClipSampleFieldsCommand.create(
            "t1", clip, audio_sample_offset=3)
        _, updated = cmd.apply(seq)
        payload = updated.to_dict()
        cls = _MACRO_SUBCOMMAND_TYPES[payload["command_type"]]
        restored = cls(payload)
        seq2, _ = restored.apply(seq)
        assert seq2.get_clip_by_id("c1").audio_sample_offset == 3


class TestRenderSemantics:
    def test_fade_boundary_exact_sample(self) -> None:
        """采样域淡化边界精确：第 N 样本包络可预测，N+1 起恒 1。"""
        x = np.ones((2, 2000), dtype=np.float32)
        out = apply_clip_fade(
            x.copy(), 0, 1, 0, 0, 0, 24.0, SR,
            envelope_fn=linear_fade_envelope,
            fade_in_samples_override=1000)
        assert out[0, 0] < 0.01
        assert abs(out[0, 999] - 0.999) < 2e-3
        assert abs(out[0, 1000] - 1.0) < 1e-6

    def test_fade_offset_shifts_with_content(self) -> None:
        """子帧偏移：淡化区随内容整体右移 offset 样本（预览/成片同刻）。"""
        x = np.ones((2, 2000), dtype=np.float32)
        kw = dict(envelope_fn=linear_fade_envelope,
                  fade_in_samples_override=1000)
        base = apply_clip_fade(x.copy(), 0, 1, 0, 0, 0, 24.0, SR, **kw)
        shifted = apply_clip_fade(
            x.copy(), 0, 1, 0, 0, 0, 24.0, SR,
            audio_sample_offset=500, **kw)
        assert abs(shifted[0, 500] - base[0, 0]) < 1e-5
        assert abs(shifted[0, 1500] - base[0, 1000]) < 1e-6

    def test_skip_gate_one_sample_move(self) -> None:
        """块消费门：移动 1 样本 = 首块少 1 样本内容（延迟入点语义）。"""
        gate = SampleSkipGate(1)
        block = np.ones((2, 10), dtype=np.float32)
        got = gate.feed(block)
        assert got.shape[-1] == 9
        assert gate.pending == 0
        # 后续块直通
        assert gate.feed(np.ones((2, 10), dtype=np.float32)).shape[-1] == 10

    def test_skip_gate_spans_blocks_and_zero_when_empty(self) -> None:
        gate = SampleSkipGate(15)
        b1 = gate.feed(np.ones((1, 10), dtype=np.float32))
        assert b1.shape[-1] == 0
        b2 = gate.feed(np.ones((1, 10), dtype=np.float32))
        assert b2.shape[-1] == 5
        assert gate.pending == 0

    def test_reference_rate_declared(self) -> None:
        """子帧偏移参考率声明（48k；块率不同时有理换算，见 sample_time）。"""
        assert AUDIO_REFERENCE_SAMPLE_RATE == 48000


# ================================================================
# M14.1 · split 采样分区契约（归属/取整/连续性）
# ================================================================

class TestSplitSamplePartition:
    def test_ntsc_split_total_samples_continuous(self):
        """NTSC（30000/1001）非整帧采样点 split：左右片样本数之和 ==
        原窗总样本数（不重读/漏样本），边界样本归左片。"""
        from src.features.edit.core.sample_time import (
            TimelineSampleClock, split_sample_partition,
        )
        from fractions import Fraction
        clock = TimelineSampleClock(Fraction(30000, 1001), 48000)
        left, right = split_sample_partition(
            clock, clip_timeline_start_frame=0,
            clip_timeline_duration_frames=100, split_timeline_frame=50)
        # NTSC 帧长 1601.6 样本：50 帧 = 80080 样本精确（50*8008/5）
        assert left == (0, 80080)
        # 100 帧 = 160160 样本；右片 = 剩余全部
        assert right == (80080, 160160 - 80080)
        assert left[1] + right[1] == 160160

    def test_split_point_inside_sample_floors_to_left(self):
        """切点落在样本内部（非整帧处）→ floor，边界样本归左片。"""
        from src.features.edit.core.sample_time import (
            TimelineSampleClock, split_sample_partition,
        )
        from fractions import Fraction
        # 30000/1001：帧起点样本位置 = 8008/5 * frame —— 帧号 ×5 不被
        # 8008 整除时产生 .2/.4/.6/.8 小数位
        clock = TimelineSampleClock(Fraction(30000, 1001), 48000)
        # 帧 51 起点 = 51*8008/5 = 81681.6 → 左片 floor 81681
        left, right = split_sample_partition(
            clock, 0, 100, 51)
        assert left == (0, 81681)
        assert right[0] == 81681  # 右片从下一样本起（连续）
        assert left[1] + right[1] == 160160

    def test_source_offset_stays_on_left_right_continues(self):
        """既有源侧子帧修剪 offset 归左片；右片 offset 连续推进。"""
        from src.features.edit.core.sample_time import (
            TimelineSampleClock, split_sample_partition,
        )
        clock = TimelineSampleClock(25, 48000)
        left, right = split_sample_partition(
            clock, 0, 100, 50, source_sample_offset=37)
        assert left == (37, 50 * 1920 - 37)
        assert right == (37 + left[1], 50 * 1920)

    def test_split_outside_clip_rejected(self):
        from src.features.edit.core.sample_time import (
            TimelineSampleClock, split_sample_partition,
        )
        clock = TimelineSampleClock(25, 48000)
        import pytest as _pytest
        with _pytest.raises(ValueError, match="严格位于"):
            split_sample_partition(clock, 0, 100, 100)
        with _pytest.raises(ValueError):
            split_sample_partition(clock, 0, 100, 0)


# ================================================================
# M14.3 采样级 UI：UpdateClipParamCommand 子帧偏移分支
# ================================================================

class TestSampleOffsetParamCommand:
    def _timeline(self):
        from src.features.edit.core.timeline_v2 import (
            ClipInstance, EditTrack, Rational, TimelineSequence,
        )
        clip = ClipInstance(
            clip_id="c1", asset_id="a", track_id="a1",
            timeline_start=0, timeline_duration=48,
            source_start=0, source_duration=48,
            audio_sample_offset=800, source_sample_offset=0)
        track = EditTrack(track_id="a1", name="A1", track_type="audio",
                          index=0, clips=(clip,))
        return TimelineSequence(audio_tracks=(track,), fps=Rational(24, 1))

    @staticmethod
    def _clip_of(seq):
        return seq.audio_tracks[0].clips[0]

    def test_apply_and_undo_roundtrip(self):
        from src.features.edit.core.commands import UpdateClipParamCommand
        seq = self._timeline()
        cmd = UpdateClipParamCommand(
            command_id="x", label="l", track_id="a1", clip_id="c1",
            param_type="audio_sample_offset", new_value=1600)
        seq2 = cmd.apply(seq)
        assert self._clip_of(seq2).audio_sample_offset == 1600
        # 其余字段原样保留（replace 语义）
        assert self._clip_of(seq2).source_sample_offset == 0
        seq3 = cmd.undo(seq2)
        assert self._clip_of(seq3).audio_sample_offset == 800

    def test_invalid_values_rejected(self):
        from src.features.edit.core.commands import UpdateClipParamCommand
        seq = self._timeline()
        for bad in (-1, 1.5, "800"):
            cmd = UpdateClipParamCommand(
                command_id="x", label="l", track_id="a1", clip_id="c1",
                param_type="source_sample_offset", new_value=bad)
            with pytest.raises(ValueError):
                cmd.apply(seq)

    def test_source_offset_branch(self):
        from src.features.edit.core.commands import UpdateClipParamCommand
        seq = self._timeline()
        cmd = UpdateClipParamCommand(
            command_id="x", label="l", track_id="a1", clip_id="c1",
            param_type="source_sample_offset", new_value=320)
        assert self._clip_of(cmd.apply(seq)).source_sample_offset == 320
