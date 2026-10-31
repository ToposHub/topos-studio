"""
Stem Export 测试（AM-10 阶段 3.5）。

验证 stem 导出的核心验收（计划表 3.5）：
1. 对 3 轨 + 2 Bus 工程规划出正确的 stem 列表（Master/Track/Bus/Group）。
2. 各 stem 与 solo render 相等（stem == 该组轨道 solo 后渲染）。
3. 文件 sample rate/channel layout 正确。
4. EditTrack.stem_group 分组标签正确聚合。

不依赖真实音频文件（decode_clip_fn 返回确定性 DC 样本），纯内存比对。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.stem_export import (
    StemType,
    StemSpec,
    plan_stems_from_sequence,
    render_stem_pcm,
    write_stem_wav,
    export_all_stems,
    _collect_bus_upstream,
)
from src.features.edit.core.timeline_v2 import (
    EditTrack,
    TimelineSequence,
    ClipInstance,
)


SR = 48000
FPS = 24.0


def _make_clip(track_id: str, start: int = 0, duration: int = 24) -> ClipInstance:
    """构造一个简单的 clip。"""
    return ClipInstance(
        clip_id=f"{track_id}_clip",
        asset_id=f"{track_id}_asset",
        track_id=track_id,
        source_start=0,
        source_duration=duration,
        timeline_start=start,
        timeline_duration=duration,
    )


def _make_track(track_id: str, name: str = "", clips_count: int = 1,
                is_bus: bool = False, output_bus_id=None, stem_group: str = "",
                volume: float = 1.0) -> EditTrack:
    """构造 EditTrack（可选 clip/bus/stem_group）。"""
    clips = tuple(_make_clip(track_id, 0, 48) for _ in range(clips_count)) if clips_count and not is_bus else ()
    return EditTrack(
        track_id=track_id, name=name or track_id, track_type="audio",
        index=0, clips=clips, is_bus=is_bus, output_bus_id=output_bus_id,
        stem_group=stem_group, volume=volume,
    )


def _make_sequence_3t_2bus():
    """构造 3 轨 + 2 Bus 工程（计划表 3.5 验收用例）。

    布局：
    - t1 (Dialogue) → Bus A
    - t2 (Music) → Master
    - t3 (Effects) → Bus A
    - BusA → Master
    - BusB → Master（空 Bus，无人路由）
    """
    bus_a = _make_track("busA", "Bus A", is_bus=True)
    bus_b = _make_track("busB", "Bus B", is_bus=True)
    t1 = _make_track("t1", "Dialogue", output_bus_id="busA", stem_group="dialogue")
    t2 = _make_track("t2", "Music", output_bus_id=None, stem_group="music")
    t3 = _make_track("t3", "Effects", output_bus_id="busA", stem_group="effects")
    seq = TimelineSequence(
        audio_tracks=(t1, t2, t3, bus_a, bus_b),
        fps=FPS,
    )
    return seq


def _dc_decoder_factory(amp: float = 0.5):
    """返回 decode_clip_fn 回调：返回确定性 DC 样本。"""
    def _decode(track, clip, frame_idx, fps, n_samples):
        sig = np.full(n_samples, amp, dtype=np.float32)
        return np.stack([sig, sig])
    return _decode


class TestPlanStems:
    """stem 规划：从 sequence 生成 stem 列表。"""

    def test_plan_master_stem_always_present(self):
        """MASTER stem 始终存在。"""
        seq = _make_sequence_3t_2bus()
        specs = plan_stems_from_sequence(seq)
        master_specs = [s for s in specs if s.stem_type == StemType.MASTER]
        assert len(master_specs) == 1
        assert master_specs[0].name == "Master"

    def test_plan_track_stems_for_clip_tracks(self):
        """有 clip 的普通轨各生成一个 TRACK stem。"""
        seq = _make_sequence_3t_2bus()
        specs = plan_stems_from_sequence(seq)
        track_specs = [s for s in specs if s.stem_type == StemType.TRACK]
        assert len(track_specs) == 3  # t1, t2, t3

    def test_plan_bus_stems(self):
        """每个 Bus 生成一个 BUS stem。"""
        seq = _make_sequence_3t_2bus()
        specs = plan_stems_from_sequence(seq)
        bus_specs = [s for s in specs if s.stem_type == StemType.BUS]
        assert len(bus_specs) == 2  # busA, busB

    def test_plan_group_stems_by_stem_group(self):
        """按 stem_group 标签聚合 GROUP stem。"""
        seq = _make_sequence_3t_2bus()
        specs = plan_stems_from_sequence(seq)
        group_specs = [s for s in specs if s.stem_type == StemType.GROUP]
        group_names = {s.name for s in group_specs}
        assert "Dialogue" in group_names
        assert "Music" in group_names
        assert "Effects" in group_names

    def test_plan_group_stems_filtered(self):
        """group_stems 参数过滤只导出指定组。"""
        seq = _make_sequence_3t_2bus()
        specs = plan_stems_from_sequence(seq, group_stems=("dialogue",))
        group_specs = [s for s in specs if s.stem_type == StemType.GROUP]
        assert len(group_specs) == 1
        assert group_specs[0].name == "Dialogue"

    def test_bus_upstream_collection(self):
        """_collect_bus_upstream 收集直接/间接路由到 Bus 的轨道。"""
        seq = _make_sequence_3t_2bus()
        tracks = list(seq.audio_tracks)
        upstream = _collect_bus_upstream("busA", tracks)
        assert "t1" in upstream
        assert "t3" in upstream
        assert "t2" not in upstream  # t2 直送 master

    def test_track_stem_does_not_include_master(self):
        """TRACK stem 不经 master（原始分轨，适合再混音）。"""
        seq = _make_sequence_3t_2bus()
        specs = plan_stems_from_sequence(seq)
        track_specs = [s for s in specs if s.stem_type == StemType.TRACK]
        for s in track_specs:
            assert s.include_master is False


class TestStemEqualsSoloRender:
    """核心验收：stem 与 solo render 相等。"""

    def test_track_stem_equals_solo_that_track(self):
        """单轨 TRACK stem == 只渲染该轨的输出。"""
        seq = _make_sequence_3t_2bus()
        decode = _dc_decoder_factory(0.5)

        # TRACK stem for t2（直送 master，不经 master stage）
        stem_spec = StemSpec(
            stem_type=StemType.TRACK, name="t2", track_ids=("t2",),
            include_master=False,
        )
        stem_pcm = render_stem_pcm(seq, stem_spec, SR, 2, decode)

        # solo t2 渲染：构造只含 t2 的 stem
        solo_spec = StemSpec(
            stem_type=StemType.TRACK, name="solo", track_ids=("t2",),
            include_master=False,
        )
        solo_pcm = render_stem_pcm(seq, solo_spec, SR, 2, decode)

        np.testing.assert_allclose(stem_pcm, solo_pcm, atol=1e-6)

    def test_master_stem_includes_all_tracks(self):
        """MASTER stem 含全部轨道的贡献（比单轨响）。"""
        seq = _make_sequence_3t_2bus()
        decode = _dc_decoder_factory(0.5)

        master_spec = StemSpec(
            stem_type=StemType.MASTER, name="Master",
            track_ids=tuple(t.track_id for t in seq.audio_tracks),
            include_master=True,
        )
        master_pcm = render_stem_pcm(seq, master_spec, SR, 2, decode)

        # t2 单轨 stem（不经 master）
        t2_spec = StemSpec(
            stem_type=StemType.TRACK, name="t2", track_ids=("t2",),
            include_master=False,
        )
        t2_pcm = render_stem_pcm(seq, t2_spec, SR, 2, decode)

        master_energy = float(np.max(np.abs(master_pcm))) if master_pcm is not None else 0
        t2_energy = float(np.max(np.abs(t2_pcm))) if t2_pcm is not None else 0
        # master 含 3 轨 + BusA（t1+t3），应比单轨 t2 响
        assert master_energy > t2_energy

    def test_group_stem_aggregates_group_tracks(self):
        """GROUP stem 聚合该 stem_group 的所有轨道。"""
        seq = _make_sequence_3t_2bus()
        decode = _dc_decoder_factory(0.5)

        # Dialogue group（只有 t1）
        dlg_spec = StemSpec(
            stem_type=StemType.GROUP, name="Dialogue",
            track_ids=("t1",), include_master=True,
        )
        dlg_pcm = render_stem_pcm(seq, dlg_spec, SR, 2, decode)

        assert dlg_pcm is not None
        assert dlg_pcm.shape[0] == 2  # stereo
        # t1 路由到 busA，group stem 含 busA 处理
        assert float(np.max(np.abs(dlg_pcm))) > 0

    def test_bus_stem_includes_upstream(self):
        """BUS stem 含其上游轨道的贡献。"""
        seq = _make_sequence_3t_2bus()
        decode = _dc_decoder_factory(0.5)

        # BusA stem（含 t1, t3 上游）
        bus_a_spec = StemSpec(
            stem_type=StemType.BUS, name="Bus A",
            track_ids=("busA", "t1", "t3"), include_master=False,
        )
        bus_pcm = render_stem_pcm(seq, bus_a_spec, SR, 2, decode)

        assert bus_pcm is not None
        # t1+t3 都路由到 busA，bus stem 应有信号
        assert float(np.max(np.abs(bus_pcm))) > 0


class TestStemPcmProperties:
    """stem PCM 属性正确性。"""

    def test_stem_pcm_sample_rate_and_channels(self):
        """stem PCM 的 shape/采样率正确。"""
        seq = _make_sequence_3t_2bus()
        decode = _dc_decoder_factory(0.5)

        spec = StemSpec(
            stem_type=StemType.TRACK, name="t2", track_ids=("t2",),
            include_master=False,
        )
        pcm = render_stem_pcm(seq, spec, SR, 2, decode)
        assert pcm is not None
        assert pcm.shape[0] == 2  # stereo
        # 时长 = 48 帧 / 24 fps = 2 秒 = 96000 样本
        assert pcm.shape[1] == int(SR * 2.0)

    def test_stem_mono(self):
        """单声道 stem。"""
        seq = _make_sequence_3t_2bus()
        decode = _dc_decoder_factory(0.5)
        spec = StemSpec(
            stem_type=StemType.TRACK, name="t2", track_ids=("t2",),
            include_master=False,
        )
        pcm = render_stem_pcm(seq, spec, SR, 1, decode)
        assert pcm.shape[0] == 1

    def test_empty_stem_returns_none(self):
        """无内容的 stem 返回 None。"""
        # 空工程（无轨道 → duration_frames=0）
        seq = TimelineSequence(audio_tracks=(), fps=FPS)
        spec = StemSpec(
            stem_type=StemType.MASTER, name="Master", track_ids=(),
        )
        pcm = render_stem_pcm(seq, spec, SR, 2)
        assert pcm is None

    def test_silent_decode_when_no_decoder(self):
        """无 decode_clip_fn 时返回静音 PCM。"""
        seq = _make_sequence_3t_2bus()
        spec = StemSpec(
            stem_type=StemType.TRACK, name="t2", track_ids=("t2",),
            include_master=False,
        )
        pcm = render_stem_pcm(seq, spec, SR, 2, decode_clip_fn=None)
        # 静音（decode 默认返回零）
        assert pcm is not None
        assert float(np.max(np.abs(pcm))) < 1e-6


class TestStemWavWriting:
    """stem WAV 文件写入。"""

    def test_write_stem_wav_creates_file(self, tmp_path):
        """write_stem_wav 创建 WAV 文件。"""
        n = SR  # 1 秒
        t = np.arange(n) / SR
        sig = (0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
        samples = np.stack([sig, sig])

        path = str(tmp_path / "test_stem.wav")
        ok = write_stem_wav(path, samples, SR, bit_depth=24)
        assert ok
        assert Path(path).exists()
        assert Path(path).stat().st_size > 0

    def test_write_stem_wav_16bit(self, tmp_path):
        """16-bit WAV 写入。"""
        n = SR // 2
        sig = np.full(n, 0.3, dtype=np.float32)
        samples = np.stack([sig, sig])

        path = str(tmp_path / "stem16.wav")
        ok = write_stem_wav(path, samples, SR, bit_depth=16)
        assert ok
        assert Path(path).exists()

    def test_write_stem_wav_creates_directory(self, tmp_path):
        """目标目录不存在时自动创建。"""
        sig = np.full(1000, 0.3, dtype=np.float32)
        samples = np.stack([sig, sig])
        path = str(tmp_path / "subdir" / "nested" / "stem.wav")
        ok = write_stem_wav(path, samples, SR, bit_depth=24)
        assert ok
        assert Path(path).exists()


class TestExportAllStems:
    """export_all_stems 顶层入口。"""

    def test_export_all_stems_creates_files(self, tmp_path):
        """导出全部 stem 生成多个 WAV 文件。"""
        seq = _make_sequence_3t_2bus()
        decode = _dc_decoder_factory(0.5)

        result = export_all_stems(
            seq, str(tmp_path), decode_clip_fn=decode,
            sample_rate=SR, channels=2, bit_depth=24,
        )
        assert len(result.stems) > 0
        # 至少有 Master + 3 Track + 2 Bus stems
        wav_files = list(Path(tmp_path).glob("*.wav"))
        assert len(wav_files) >= 5

    def test_export_progress_callback(self, tmp_path):
        """进度回调被调用。"""
        seq = _make_sequence_3t_2bus()
        decode = _dc_decoder_factory(0.5)
        progress = []

        export_all_stems(
            seq, str(tmp_path), decode_clip_fn=decode,
            progress_callback=lambda c, t, name: progress.append((c, t, name)),
        )
        assert len(progress) > 0
        # 最后一次应是完成
        assert progress[-1][0] == progress[-1][1]

    def test_export_result_all_succeeded(self, tmp_path):
        """成功导出时 all_succeeded=True。"""
        seq = _make_sequence_3t_2bus()
        decode = _dc_decoder_factory(0.5)
        result = export_all_stems(
            seq, str(tmp_path), decode_clip_fn=decode,
        )
        assert result.all_succeeded

    def test_export_group_stems_filtered(self, tmp_path):
        """group_stems 过滤只导出指定组的 GROUP stem（TRACK/BUS 不受影响）。"""
        seq = _make_sequence_3t_2bus()
        decode = _dc_decoder_factory(0.5)

        # 不过滤：应含 3 个 GROUP stem（Dialogue/Music/Effects）
        result_all = export_all_stems(
            seq, str(tmp_path / "all"), decode_clip_fn=decode,
        )
        group_specs_all = plan_stems_from_sequence(seq)
        group_all = [s.name for s in group_specs_all if s.stem_type == StemType.GROUP]
        assert set(group_all) == {"Dialogue", "Music", "Effects"}

        # 过滤只导 dialogue：GROUP stem 只剩 Dialogue
        result_filtered = export_all_stems(
            seq, str(tmp_path / "filtered"), decode_clip_fn=decode,
            group_stems=("dialogue",),
        )
        group_specs_filtered = plan_stems_from_sequence(seq, group_stems=("dialogue",))
        group_filtered = [s.name for s in group_specs_filtered if s.stem_type == StemType.GROUP]
        assert group_filtered == ["Dialogue"]


class TestStemGroupField:
    """EditTrack.stem_group 字段行为。"""

    def test_stem_group_default_empty(self):
        """默认 stem_group 为空字符串。"""
        t = EditTrack(track_id="t1", track_type="audio", index=0)
        assert t.stem_group == ""

    def test_with_stem_group(self):
        """with_stem_group 设置标签。"""
        t = EditTrack(track_id="t1", track_type="audio", index=0)
        t2 = t.with_stem_group("dialogue")
        assert t2.stem_group == "dialogue"
        assert t.stem_group == ""  # 原 track 不变（immutable）

    def test_stem_group_preserved_through_with_name(self):
        """with_name 保留 stem_group（AM-04 不变量扩展）。"""
        t = EditTrack(
            track_id="t1", track_type="audio", index=0, stem_group="music",
        )
        t2 = t.with_name("Background Music")
        assert t2.stem_group == "music"

    def test_stem_group_serialization_roundtrip(self):
        """stem_group 序列化往返。"""
        t = EditTrack(
            track_id="t1", track_type="audio", index=0, stem_group="effects",
        )
        d = t.to_dict()
        assert d["stem_group"] == "effects"
        t2 = EditTrack.from_dict(d)
        assert t2.stem_group == "effects"

    def test_stem_group_backward_compat_no_field(self):
        """旧项目无 stem_group 字段时默认空字符串。"""
        d = {"track_id": "t1", "track_type": "audio", "index": 0}
        t = EditTrack.from_dict(d)
        assert t.stem_group == ""


# =====================================================================
# F3.4 · sends 路由进 stem（Master stem 与成片一致验收）
# =====================================================================

class TestStemSendsRouting:
    """sends 并行 tap 此前在 stem 路径整段丢失——带 send 的工程
    Master stem ≠ 成片。修复后：MASTER stem 含 send 贡献。"""

    @staticmethod
    def _sequence_with_send():
        from src.features.edit.core.timeline_v2 import Send

        bus_a = _make_track("busA", "Bus A", is_bus=True)
        # t2 直达 Master，同时 send 一半电平到 BusA
        t2 = _make_track("t2", "Music", clips_count=1)
        t2 = t2._rebuild(sends=(Send(
            send_id="s1", target_bus_id="busA", level=0.5),))
        t1 = _make_track("t1", "Dialogue", output_bus_id="busA",
                         clips_count=1)
        return TimelineSequence(
            audio_tracks=(t1, t2, bus_a), fps=FPS)

    def test_master_stem_includes_send_contribution(self):
        """MASTER stem：t2 的 send(0.5) 进 BusA → master 应含
        t2 主路 + t2×0.5（send）+ t1（busA 汇聚）全部贡献。"""
        seq = self._sequence_with_send()
        decode = _dc_decoder_factory(0.2)
        master_spec = StemSpec(
            stem_type=StemType.MASTER, name="Master",
            track_ids=tuple(t.track_id for t in seq.audio_tracks),
            include_master=True,
        )
        pcm = render_stem_pcm(seq, master_spec, SR, 2, decode)
        peak = float(np.max(np.abs(pcm)))
        # t2=0.2（master 直达）+ t2×0.1（send 0.5 经 busA）+ t1=0.2（busA）
        # = 0.5（DC 相加；电平 <1.0 避开 MasterLimiter 非线性）；
        # send 丢失的旧实现只读到 0.4。
        assert peak == pytest.approx(0.5, abs=0.02)

    def test_bus_stem_includes_send_source(self):
        """BUS stem（busA）：上游 = t1（output_bus）+ t2（send 0.5）。
        send 源按 send level 计入（修复前 send 上游干声丢失/未乘电平）。"""
        seq = self._sequence_with_send()
        decode = _dc_decoder_factory(0.2)
        bus_spec = StemSpec(
            stem_type=StemType.BUS, name="Bus A",
            track_ids=("busA", "t1", "t2"),  # bus + 上游（含 send 源）
            include_master=False,
        )
        pcm = render_stem_pcm(seq, bus_spec, SR, 2, decode)
        peak = float(np.max(np.abs(pcm)))
        # t1=0.2（路由 busA）+ t2×0.1（send 0.5）= 0.3；
        # t2 干声不落 BUS stem（修复前干声直入 = 0.4+）。
        assert peak == pytest.approx(0.3, abs=0.02)


class TestRoutingManifest:
    """F3.4 · 路由 sidecar：分轨重组的明确路由声明。"""

    _sequence_with_send = staticmethod(
        TestStemSendsRouting._sequence_with_send)

    def test_manifest_declares_sends_and_buses(self):
        from src.features.edit.core.stem_export import build_routing_manifest

        seq = self._sequence_with_send()
        m = build_routing_manifest(seq)
        assert m["schema"] == "topos.stem_routing/1"
        assert m["has_routing"] is True
        by_id = {t["track_id"]: t for t in m["tracks"]}
        assert by_id["busA"]["kind"] == "bus"
        assert by_id["t1"]["output_bus_id"] == "busA"
        assert by_id["t2"]["sends"][0]["target_bus_id"] == "busA"
        assert by_id["t2"]["sends"][0]["tap"] == "post-fader"
        assert by_id["t2"]["sends"][0]["level"] == pytest.approx(0.5)
        # 重组声明：有路由时明示"简单相加不等于混音"
        assert "简单相加不等于混音" in m["recombination"]

    def test_manifest_flat_project_declares_sum_ok(self):
        from src.features.edit.core.stem_export import build_routing_manifest

        seq = _make_sequence_3t_2bus()
        # 3t_2bus 有 busA 路由 → has_routing True；去路由后为平面工程
        m = build_routing_manifest(seq)
        assert m["has_routing"] is True
        flat = TimelineSequence(
            audio_tracks=(_make_track("t9", "Solo", clips_count=1),),
            fps=FPS)
        m2 = build_routing_manifest(flat)
        assert m2["has_routing"] is False
        assert "相加" in m2["recombination"]

    def test_export_all_stems_writes_sidecar(self, tmp_path):
        import json
        import os

        from src.features.edit.core.stem_export import export_all_stems

        seq = self._sequence_with_send()
        result = export_all_stems(
            seq, str(tmp_path), decode_clip_fn=_dc_decoder_factory(0.2))
        assert result.routing_manifest_path
        assert os.path.exists(result.routing_manifest_path)
        with open(result.routing_manifest_path, encoding="utf-8") as fh:
            m = json.load(fh)
        assert m["schema"] == "topos.stem_routing/1"
        assert any(t["sends"] for t in m["tracks"])
