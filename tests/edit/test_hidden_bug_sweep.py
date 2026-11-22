"""整体回扫（2026-11-22 第二轮）：隐藏缺陷修复的回归锁定。

每个测试对应一个已修复的缺陷（详见当轮审查记录）：

- monitor：5ms 增益斜坡按全局进度推进（块长 < 斜坡长不得压缩成单块）；
  混合长度 tap 输出取最大宽度（长 tap 尾部不得截断）。
- waveform pyramid：视窗完全越过 extent 末尾时解码窗不得越界 1 样本。
- render artifacts：注册表写前重读合并（跨实例互不覆盖删除）。
- reference mix：max_offset_samples=0 退化窗显式拒绝（不返回高置信错位）。
- capture clock：无拟合时按最新锚点换算（单锚不得退回过原点恒等）；
  日志复用（sequence 重启）不被旧会话同号段吞掉；恢复前缀切到最新会话。
- pro audio oracles：chna 记录按 EBU Tech 3285-s7 布局（UID 在字节 2..13）。
- AAF：帧域位置不得被直接当样本写（24fps×48k 下 2000 倍位移）——
  见 test_aaf_audio_subset.py::test_export_frame_domain_not_relabelled_as_samples。
- automation：trim 烘入保留贝塞尔手柄/外推策略（声音不变合同）。
- snapshot：stable_hash 超集补齐（panner/转场/master 条带/采样域淡化/
  子帧偏移/clip 启用/Bus 布局——变化必须改变 hash）。
- delivery：from_dict 不洗涤非法测量值；发布失败回滚零新增污染；
  同名 basename 冲突显式拒绝；None 限值不参与比较也不崩溃。
- PDC：pdc_out_edges_max_delay 公开访问器（导出间隙零喂预算依据）。
"""
from __future__ import annotations

import dataclasses
import math
from pathlib import Path

import numpy as np
import pytest

from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
from src.features.edit.core.audio_graph_snapshot import build_audio_graph_snapshot
from src.features.edit.core.capture_clock import (
    CaptureBlock,
    CaptureJournal,
    ClockAnchor,
)
from src.features.edit.core.delivery_preset import (
    DeliveryPreset,
    LoudnessTarget,
    evaluate_delivery,
)
from src.features.edit.core.delivery_profile import (
    DeliveryProfile,
    MeasurementResult,
    measurement_rejection,
    publish_delivery_atomic,
)
from src.features.edit.core.monitor_service import (
    MonitorService,
    MonitorSessionState,
)
from src.features.edit.core.pro_audio_oracles import pack_chna_chunk
from src.features.edit.core.reconform_transaction import (
    KeptSpan,
    validate_reconform_transaction,
)
from src.features.edit.core.reference_mix import align_reference_offset
from src.features.edit.core.render_artifacts import ArtifactRegistry, RenderArtifact
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.features.edit.core.waveform_pyramid import plan_decode_window
from tests.support.reference_project import build_reference_sequence


# ─────────────────────────────────────────────────────────────────────────────
# monitor · 斜坡全局进度 + 混合长度 tap
# ─────────────────────────────────────────────────────────────────────────────

class TestMonitorRampGlobalProgress:
    def test_small_blocks_do_not_compress_ramp(self) -> None:
        """64 样本块 × 若干：5ms(240 样本)斜坡按全局样本位置推进，
        首块末样本不得已到达目标（旧实现把整段斜坡压进首块）。"""
        service = MonitorService(48000.0)
        tap = np.ones((2, 64), dtype=np.float32)
        service.set_state(MonitorSessionState(gain_db=0.0))
        out1 = service.render_block({"master": tap})
        # 触发 1.0 → 0.1 的斜坡（240 样本 @48k = 5ms）
        service.set_state(MonitorSessionState(
            gain_db=20.0 * math.log10(0.1), ramp_ms=5.0))
        block = service.render_block({"master": tap})
        # 首块末样本 ≈ 1 − 0.9×64/240 ≈ 0.76（不得已是 0.1）
        assert float(block[0, -1]) > 0.5
        assert service._ramp.ramp_samples_left > 0
        # 继续喂 3 块（共 4×64=256 ≥ 240）→ 到达稳态 0.1
        for _ in range(3):
            block = service.render_block({"master": tap})
        assert float(block[0, -1]) == pytest.approx(0.1, abs=1e-3)
        assert service._ramp.ramp_samples_left == 0
        # 稳态后增益精确
        steady = service.render_block({"master": tap})
        assert float(steady[0, 10]) == pytest.approx(0.1, rel=1e-6)
        assert out1.shape == (2, 64)

    def test_mixed_length_taps_keep_longest_tail(self) -> None:
        """两 tap 一短一长：输出长度 = 最长（长 tap 尾部不得被截）。"""
        service = MonitorService(48000.0)
        service.set_state(MonitorSessionState(
            source_tap="afl", selected_listen_ids=("a", "b")))
        short = np.zeros((2, 100), dtype=np.float32)
        long = np.zeros((2, 300), dtype=np.float32)
        long[:, 299] = 1.0
        out = service.render_block({"track:a:post": short,
                                    "track:b:post": long})
        assert out.shape[-1] == 300
        assert float(out[0, 299]) == 1.0


# ─────────────────────────────────────────────────────────────────────────────
# waveform pyramid · 解码窗硬边界
# ─────────────────────────────────────────────────────────────────────────────

class TestDecodeWindowHardBoundary:
    def test_view_beyond_extent_clamps_inside(self) -> None:
        plan = plan_decode_window(0, 1000, 5000, 6000, 2)
        assert plan.window_start <= 999
        assert plan.window_end <= 1000
        assert plan.window_end > plan.window_start


# ─────────────────────────────────────────────────────────────────────────────
# render artifacts · 注册表合并写
# ─────────────────────────────────────────────────────────────────────────────

def _artifact(aid: str) -> RenderArtifact:
    return RenderArtifact(
        artifact_id=aid, kind="bounce", project_id="proj",
        sequence_id="seq", revision=1, sound_hash=f"hash-{aid}",
        sample_count=100)


class TestArtifactRegistryMerge:
    def test_second_writer_not_clobbered(self, tmp_path: Path) -> None:
        r1 = ArtifactRegistry(tmp_path)
        r1.register(_artifact("a"))
        r2 = ArtifactRegistry(tmp_path)
        r2.register(_artifact("b"))
        # r1 已加载：再写不得把 r2 的条目覆盖掉
        r1.register(_artifact("c"))
        r3 = ArtifactRegistry(tmp_path)
        ids = {a.artifact_id for a in r3.list()}
        assert ids == {"a", "b", "c"}

    def test_remove_keeps_other_writer_entries(self, tmp_path: Path) -> None:
        r1 = ArtifactRegistry(tmp_path)
        r1.register(_artifact("a"))
        r2 = ArtifactRegistry(tmp_path)
        r2.register(_artifact("b"))
        assert r1.remove("a") is True
        r3 = ArtifactRegistry(tmp_path)
        ids = {a.artifact_id for a in r3.list()}
        assert ids == {"b"}


# ─────────────────────────────────────────────────────────────────────────────
# reference mix · 退化窗口拒绝
# ─────────────────────────────────────────────────────────────────────────────

class TestAlignReferenceDegenerateWindow:
    def test_zero_window_rejected(self) -> None:
        rng = np.random.default_rng(11)
        prog = rng.normal(size=8000)
        ref = np.r_[prog[300:], np.zeros(300)]  # 参考早 300 样本
        with pytest.raises(ValueError, match="max_offset_samples"):
            align_reference_offset(prog, ref, max_offset_samples=0)

    def test_small_window_still_aligned(self) -> None:
        rng = np.random.default_rng(12)
        prog = rng.normal(size=8000)
        ref = np.r_[np.zeros(50), prog]  # 参考晚 50 样本
        offset, conf = align_reference_offset(prog, ref,
                                              max_offset_samples=50)
        assert offset == 50
        assert conf > 0.6


# ─────────────────────────────────────────────────────────────────────────────
# capture clock · 单锚回退 + 日志跨会话
# ─────────────────────────────────────────────────────────────────────────────

class TestClockAnchorFallback:
    def test_single_anchor_exact_at_anchor(self) -> None:
        anchor = ClockAnchor(48000, 48000)
        anchor.add_anchor(device_sample=5000, timeline_sample=9000)
        # 无拟合缓存（未 estimate_drift / 单锚早退）：按最新锚点换算
        assert anchor.timeline_sample_for_device(5000) == 9000
        assert anchor.timeline_sample_for_device(5001) == 9001

    def test_rejected_fit_falls_back_to_latest_anchor(self) -> None:
        anchor = ClockAnchor(48000, 48000)
        anchor.add_anchor(device_sample=0, timeline_sample=0)
        anchor.add_anchor(device_sample=48000, timeline_sample=47000)  # 带外
        with pytest.raises(ValueError, match="sanity"):
            anchor.estimate_drift()
        # 拒绝后不得退回过原点恒等（48000 ≠ 47000 锚点值）
        assert anchor.timeline_sample_for_device(48000) == 47000


class TestJournalCrossSession:
    def _block(self, seq: int, first: int) -> CaptureBlock:
        return CaptureBlock(device_session_id="dev", sequence_no=seq,
                            first_device_sample=first,
                            host_monotonic_ns=seq * 1_000_000,
                            pcm=np.zeros((1, 64), dtype=np.float32))

    def test_reused_journal_accepts_new_session(self, tmp_path: Path) -> None:
        path = tmp_path / "take.jsonl"
        j1 = CaptureJournal(path)
        for i in range(3):
            seg = j1.record_segment(self._block(i, i * 64))
            assert j1.commit_segment(seg.sequence_no)
        j1.close()
        # 崩溃恢复后同一日志路径开新会话（sequence 从 0 重启）
        j2 = CaptureJournal(path)
        seg = j2.record_segment(self._block(0, 0))
        assert j2.commit_segment(0) is True  # 不得被旧会话 seq0 吞掉
        j2.close()
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 4
        # 恢复前缀 = 最新会话的前缀（1 段 × 64 样本）
        j3 = CaptureJournal(path)
        count, samples = j3.recoverable_prefix()
        assert count == 1
        assert samples == 64


# ─────────────────────────────────────────────────────────────────────────────
# pro audio oracles · chna 磁盘布局
# ─────────────────────────────────────────────────────────────────────────────

class TestChnaDiskLayout:
    def test_uid_at_spec_offset(self) -> None:
        """EBU Tech 3285-s7：记录 = u16 trackIndex + UID ASCII @2..13 +
        u16 保留（旧实现把 UID 写在 4..15——与真实 ADM/BWF 工具不互操作）。"""
        payload = pack_chna_chunk(1, [("ATU_00000001", 0)])
        assert payload[10:22] == b"ATU_00000001"
        assert payload[8:10] == b"\x01\x00"   # trackIndex=1（1 起）
        assert payload[22:24] == b"\x00\x00"  # 保留字


# ─────────────────────────────────────────────────────────────────────────────
# automation · trim 烘入保留曲线形状
# ─────────────────────────────────────────────────────────────────────────────

class TestCoalescePreservesCurveShape:
    def _timeline(self):
        from src.shared.keyframe import (
            InterpolationType,
            Keyframe,
            KeyframeTrack,
        )
        from src.features.edit.core.automation_recorder import (
            CoalesceAutomationTrimCommand,
        )
        kf = Keyframe(frame=0, value=1.0,
                      interpolation=InterpolationType.BEZIER,
                      handle_right=(10.0, 0.6))
        curve = KeyframeTrack(param_name="volume", min_val=0.0, max_val=2.0,
                              keyframes=[kf],
                              pre_extrap="linear", post_extrap="linear")
        clip = ClipInstance(clip_id="c", asset_id="a", track_id="t",
                            timeline_start=0, timeline_duration=48,
                            source_start=0, source_duration=48)
        track = (EditTrack("t", "T", "audio", 0, clips=(clip,), volume=0.8)
                 ._rebuild(automation={"volume": curve})
                 .with_automation_trim("volume", 0.5))
        return TimelineSequence(audio_tracks=(track,)), \
            CoalesceAutomationTrimCommand

    def test_bezier_handles_and_extrapolation_kept(self) -> None:
        timeline, cmd_cls = self._timeline()
        track = timeline.get_track_by_id("t")
        frames = list(range(-10, 40))
        before = [track.get_automation_value("volume", f, track.volume)
                  for f in frames]
        new_tl, _ = cmd_cls.create("t").apply(timeline)
        nt = new_tl.get_track_by_id("t")
        after = [nt.get_automation_value("volume", f, nt.volume)
                 for f in frames]
        # 声音不变合同（旧实现在 BEZIER+linear 外推下 |Δ|≈0.36）
        for b, a in zip(before, after):
            assert math.isclose(b, a, rel_tol=1e-12, abs_tol=1e-12), (b, a)
        nc = nt.automation["volume"]
        assert nc.keyframes[0].handle_right == (10.0, 0.6)
        assert nc.pre_extrap == "linear" and nc.post_extrap == "linear"


# ─────────────────────────────────────────────────────────────────────────────
# snapshot · stable_hash 超集补齐
# ─────────────────────────────────────────────────────────────────────────────

class TestSnapshotSupersetFields:
    def test_track_panner_changes_hash(self) -> None:
        seq = build_reference_sequence()
        base = build_audio_graph_snapshot(seq).stable_hash
        track = seq.audio_tracks[0]
        mutated = seq.with_track_updated(dataclasses.replace(
            track, panner={"spread": 0.5, "rotate": 0.1}))
        assert build_audio_graph_snapshot(mutated).stable_hash != base

    def test_bus_layout_changes_hash(self) -> None:
        seq = build_reference_sequence()
        base = build_audio_graph_snapshot(seq).stable_hash
        bus = next(t for t in seq.audio_tracks if t.is_bus)
        mutated = seq.with_track_updated(
            dataclasses.replace(bus, bus_layout="5.1"))
        assert build_audio_graph_snapshot(mutated).stable_hash != base

    def test_master_strip_changes_hash(self) -> None:
        seq = build_reference_sequence()
        base = build_audio_graph_snapshot(seq).stable_hash
        mutated = dataclasses.replace(
            seq, master_strip_eq={"enabled": True, "low": 2.0})
        assert build_audio_graph_snapshot(mutated).stable_hash != base

    def test_clip_sample_fades_and_offsets_change_hash(self) -> None:
        seq = build_reference_sequence()
        base = build_audio_graph_snapshot(seq).stable_hash
        track = next(t for t in seq.audio_tracks if t.clips)
        clip = track.clips[0]
        mutated = seq.with_track_updated(dataclasses.replace(
            track, clips=(dataclasses.replace(clip, fade_in_samples=64),)))
        assert build_audio_graph_snapshot(mutated).stable_hash != base
        mutated2 = seq.with_track_updated(dataclasses.replace(
            track, clips=(dataclasses.replace(clip, audio_sample_offset=17),)))
        assert build_audio_graph_snapshot(mutated2).stable_hash != base
        mutated3 = seq.with_track_updated(dataclasses.replace(
            track, clips=(dataclasses.replace(clip, is_enabled=False),)))
        assert build_audio_graph_snapshot(mutated3).stable_hash != base


# ─────────────────────────────────────────────────────────────────────────────
# delivery · from_dict 忠实性 / 发布原子性 / None 限值
# ─────────────────────────────────────────────────────────────────────────────

def _profile() -> DeliveryProfile:
    return DeliveryProfile(
        profile_id="p", name="P", verification="verified",
        channel_order=("L", "R"), sample_rate=48000, bit_depth=24,
        target_lufs=-24.0,
        stems=(),
    )


class TestMeasurementFaithfulReload:
    def _result(self, **overrides) -> MeasurementResult:
        kw = dict(stem_id="FullMix", status="complete",
                  sample_rate=48000, channel_order=("L", "R"),
                  measurements={"integrated_lufs": -24.0,
                                "true_peak_dbtp": -2.0,
                                "peak_dbfs": -1.0})
        kw.update(overrides)
        return MeasurementResult(**kw)

    def test_bool_not_laundered_to_float(self) -> None:
        data = self._result(
            measurements={"integrated_lufs": -24.0, "true_peak_dbtp": True,
                          "peak_dbfs": -1.0}).to_dict()
        reloaded = MeasurementResult.from_dict(data)
        # 旧实现 float(True)=1.0 → 验收通过；忠实保留后必须因类型被拒
        rejection = measurement_rejection(reloaded, _profile())
        assert rejection is not None and "finite" in rejection

    def test_absurd_finite_value_rejected(self) -> None:
        result = self._result(
            measurements={"integrated_lufs": -24.0,
                          "true_peak_dbtp": 1e30, "peak_dbfs": -1.0})
        assert "plausible" in (measurement_rejection(result, _profile())
                               or "")


class TestPublishAtomicity:
    def test_same_basename_rejected(self, tmp_path: Path) -> None:
        d = tmp_path
        (d / "a").mkdir()
        (d / "b").mkdir()
        (d / "a" / "mix.wav").write_bytes(b"A")
        (d / "b" / "mix.wav").write_bytes(b"B")
        out = tmp_path / "out"
        with pytest.raises(ValueError, match="basename"):
            publish_delivery_atomic(
                {"FullMix": d / "a" / "mix.wav",
                 "MX": d / "b" / "mix.wav"}, out)

    def test_failure_rolls_back_new_files(self, tmp_path: Path) -> None:
        d = tmp_path / "src"
        d.mkdir()
        (d / "a.wav").write_bytes(b"A")
        (d / "b.wav").write_bytes(b"B")
        out = tmp_path / "out"
        out.mkdir()
        blocker = out / "b.wav"
        blocker.mkdir()  # os.replace 到目录名 → 失败（a.wav 已发布后）
        with pytest.raises(OSError):
            publish_delivery_atomic({"A": d / "a.wav", "B": d / "b.wav"},
                                    out)
        # 回滚：本次新发布的 a.wav 不得残留
        assert not (out / "a.wav").exists()
        assert blocker.is_dir()  # 调用方旧内容未被误删


class TestEvaluateDeliveryNoneLimits:
    def test_none_target_does_not_crash_or_flag(self) -> None:
        preset = DeliveryPreset(
            preset_id="x", name="X", standard="test", version="1",
            loudness=LoudnessTarget(target_lufs=None, tolerance_lu=1.0,
                                    max_true_peak_dbtp=-2.0),
            stems=("FullMix",),
        )
        findings, passed = evaluate_delivery(preset, {
            "FullMix": {"true_peak_dbtp": -2.5, "peak_dbfs": -1.0},
        })
        assert passed is True
        assert not any(f["check_id"].endswith(".loudness")
                       or f["check_id"].endswith(".measurement_missing")
                       for f in findings)


# ─────────────────────────────────────────────────────────────────────────────
# reconform · 保护区间判定（ containment 语义）
# ─────────────────────────────────────────────────────────────────────────────

class TestProtectedRangeContainment:
    def test_contained_range_passes(self) -> None:
        assert validate_reconform_transaction(
            (KeptSpan(0, 2000, 0),), protected_ranges=[(500, 1000)]) == []

    def test_partially_covered_rejected(self) -> None:
        errors = validate_reconform_transaction(
            (KeptSpan(300, 700, 300),), protected_ranges=[(0, 1000)])
        assert any("protected" in e for e in errors)


# ─────────────────────────────────────────────────────────────────────────────
# PDC · 出边最大延迟访问器（导出间隙零喂预算依据）
# ─────────────────────────────────────────────────────────────────────────────

class _DelayOnly:
    """声明 N 样本算法延迟的处理链桩（process 恒等）。"""

    def __init__(self, latency: int) -> None:
        self._latency = int(latency)

    def is_active(self) -> bool:
        return True

    def latency_samples(self) -> int:
        return self._latency

    def tail_estimate_samples(self) -> int:
        return 0

    def process(self, samples: np.ndarray,
                sidechain_by_id: object = None) -> np.ndarray:
        return samples

    def sidechain_sources(self) -> set:
        return set()

    def set_params(self, *args, **kwargs) -> None:
        return None

    def sidechain_map(self, taps):
        return {}

    def collect_gain_reduction(self):
        return {}

    def collect_detector_levels(self):
        return {}

    def set_spectrum_metering(self, _enabled: bool) -> None:
        return None


class TestPdcOutEdgesAccessor:
    def test_max_delay_across_main_and_send_edges(self) -> None:
        from src.features.edit.core.timeline_v2 import Send
        sequence = TimelineSequence(audio_tracks=(
            EditTrack("dry", "Dry", "audio", 0),
            EditTrack("wet", "Wet", "audio", 1, output_bus_id="bus1"),
            EditTrack("bus1", "Bus 1", "audio", 2, is_bus=True),
        ))
        renderer = AudioGraphRenderer(48000.0)
        renderer.configure_from_sequence(sequence)
        bus1 = renderer.get_node("bus1")
        assert bus1 is not None
        bus1.effect_chain = _DelayOnly(300)
        renderer._recompute_pdc()
        # dry 的主路边 = 300（对齐 wet 的最迟路径）；wet 主路边 = 0
        assert renderer.pdc_out_edges_max_delay("dry") == 300
        assert renderer.pdc_out_edges_max_delay("wet") == 0
        assert renderer.pdc_total_samples() == 300

    def test_send_edge_counts_toward_max(self) -> None:
        from src.features.edit.core.timeline_v2 import Send
        sequence = TimelineSequence(audio_tracks=(
            EditTrack("a", "A", "audio", 0),
            EditTrack("b", "B", "audio", 1, output_bus_id="bus1",
                      sends=(Send(send_id="s", target_bus_id="bus2",
                                  level=1.0, tap="post_fader"),)),
            EditTrack("bus1", "Bus 1", "audio", 2, is_bus=True),
            EditTrack("bus2", "Bus 2", "audio", 3, is_bus=True),
        ))
        renderer = AudioGraphRenderer(48000.0)
        renderer.configure_from_sequence(sequence)
        bus2 = renderer.get_node("bus2")
        assert bus2 is not None
        bus2.effect_chain = _DelayOnly(500)
        renderer._recompute_pdc()
        # a 的直达主路边需等 500（对齐经 bus2 的最迟路径）。
        # b 的 send 边延迟 = 0：send 内容随后被 bus2 自身链延迟 500，
        # 天然对齐（补偿只在汇合点间分配，不重复补偿）。
        assert renderer.pdc_out_edges_max_delay("a") == 500
        assert renderer.pdc_out_edges_max_delay("b") == 0
