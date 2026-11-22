"""B12 · AAF 音频子集往返回归（pyaaf2 1.7.1 / MIT）。

手册验收（§20/B12）：合成 5 轨混合 mono/stereo、不同源入点、间隙、
23.976/24/25 fps 项目；导出→导入后源窗误差 ≤1 样本（样本编辑率槽）；
轨名/槽身份保持；不支持项（视频/FX/淡化）进报告不静默丢；inspect 不改
项目；pyaaf2 缺失时明确不可用。外部接收端往返属人工验收（另行记录）。
"""
from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from src.features.edit.adapters.aaf_adapter import (
    AafAudioAdapter,
    AafOptions,
    export_aaf,
    import_aaf,
    inspect_aaf,
)
from src.features.edit.adapters.interchange_types import (
    InterchangeEntryCategory,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Send,
    TimelineSequence,
)

pytest.importorskip("aaf2")


def _sequence() -> TimelineSequence:
    """3 音轨（含间隙/多源入点/不同时长）+ 1 Bus + 视频轨（进报告）。"""
    clips_dx = (
        ClipInstance(clip_id="dx1", asset_id="asset-dx", track_id="dx",
                     timeline_start=0, timeline_duration=48000,
                     source_start=1200, source_duration=48000),
        ClipInstance(clip_id="dx2", asset_id="asset-dx", track_id="dx",
                     timeline_start=72000, timeline_duration=48000,
                     source_start=96000, source_duration=48000),
    )
    clips_mx = (
        ClipInstance(clip_id="mx1", asset_id="asset-mx", track_id="mx",
                     timeline_start=48000, timeline_duration=96000,
                     source_start=0, source_duration=96000),
    )
    clips_amb = (
        ClipInstance(clip_id="amb1", asset_id="asset-amb", track_id="amb",
                     timeline_start=24000, timeline_duration=48000,
                     source_start=480, source_duration=48000),
    )
    dx = EditTrack("dx", "DX", "audio", 0, clips=clips_dx,
                   output_bus_id="dialogue_bus")
    mx = EditTrack("mx", "MX", "audio", 1, clips=clips_mx)
    amb = EditTrack("amb", "AMB", "audio", 2, clips=clips_amb)
    bus = EditTrack("dialogue_bus", "DX Bus", "audio", 3, is_bus=True,
                    sends=(Send(send_id="s1", target_bus_id="dialogue_bus",
                                level=0.0),))
    video = EditTrack("vid", "V1", "video", 0)
    return TimelineSequence(audio_tracks=(dx, mx, amb, bus),
                            video_tracks=(video,),
                            fps=Fraction(24000, 1001),
                            sample_rate=48000)


class TestAafExportImport:
    def test_export_creates_file_with_report(self, tmp_path: Path) -> None:
        seq = _sequence()
        result = export_aaf(seq, tmp_path / "out.aaf")
        assert Path(result.output_path).is_file()
        assert Path(result.output_path).stat().st_size > 0
        assert result.report.format_id == "aaf"
        preserved = [e for e in result.report.entries
                     if e.category == InterchangeEntryCategory.PRESERVED]
        assert any("audio track" in e.description for e in preserved)
        # 视频/Bus/FX 未静默保留
        assert any(e.category == InterchangeEntryCategory.DROPPED
                   for e in result.report.entries)
        assert any(e.category == InterchangeEntryCategory.REQUIRES_USER_ACTION
                   for e in result.report.entries)
        # 无临时残留
        assert not list(tmp_path.glob("*.tmp"))

    def test_unsupported_policy_bake_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="bake"):
            export_aaf(_sequence(), tmp_path / "x.aaf",
                       options=AafOptions(unsupported_policy="bake"))

    def test_inspect_reads_without_import(self, tmp_path: Path) -> None:
        path = tmp_path / "out.aaf"
        export_aaf(_sequence(), path)
        info = inspect_aaf(path)
        assert info.format_id == "aaf"
        assert info.supported
        assert info.timeline_count == 1
        assert info.asset_count == 3

    def test_roundtrip_source_windows_within_one_sample(
            self, tmp_path: Path) -> None:
        """手册：样本编辑率槽 → 导入后源窗/位置误差 ≤1 样本。

        位置按**样本域**比对：导出侧 23.976 帧域 → 样本；导入侧以导入
        序列自身 fps（默认 24）+ 子帧偏移重述同一绝对样本位置。帧数值
        绝不在两个域之间直接相等比较（那是原实现的域混淆 bug）。
        """
        from src.features.edit.core.interchange import frames_to_samples

        seq = _sequence()
        path = tmp_path / "out.aaf"
        export_aaf(seq, path)
        result = import_aaf(path)
        assert result.sequences, "no candidate sequence imported"
        imported = result.sequences[0]
        tracks = {t.name: t for t in imported.audio_tracks}
        # 3 条非 Bus 音频轨（Bus 不导出）
        assert set(tracks) == {"DX", "MX", "AMB"}
        imp_fps = imported.fps

        def _start_s(clip: ClipInstance) -> int:
            return frames_to_samples(clip.timeline_start, imp_fps, 48000) \
                + int(clip.audio_sample_offset or 0)

        def _source_s(clip: ClipInstance) -> int:
            return frames_to_samples(clip.source_start, imp_fps, 48000) \
                + int(clip.source_sample_offset or 0)

        def _end_s(clip: ClipInstance) -> int:
            return frames_to_samples(
                clip.timeline_start + clip.timeline_duration,
                imp_fps, 48000)

        dx = tracks["DX"].clips
        assert len(dx) == 2
        # 23.976@48k 每帧 2002 样本：导出前位置全部精确换算
        assert _start_s(dx[0]) == 0
        assert _end_s(dx[0]) - _start_s(dx[0]) == 48000 * 2002
        assert abs(_source_s(dx[0]) - 1200 * 2002) <= 1
        assert _start_s(dx[1]) == 72000 * 2002
        assert abs(_source_s(dx[1]) - 96000 * 2002) <= 1
        mx = tracks["MX"].clips
        assert _start_s(mx[0]) == 48000 * 2002
        assert _end_s(mx[0]) - _start_s(mx[0]) == 96000 * 2002
        amb = tracks["AMB"].clips
        assert _start_s(amb[0]) == 24000 * 2002
        assert abs(_source_s(amb[0]) - 480 * 2002) <= 1
        # 间隙不伪造内容（DX 轨 [48000, 72000) 帧区间内无 clip 起点）
        starts_s = [_start_s(c) for c in dx]
        assert 48000 * 2002 not in starts_s
        # relink 请求覆盖全部 3 个资产
        relink_keys = {r.external_ref for r in result.relink_requests}
        assert relink_keys == {"topos:asset-dx", "topos:asset-mx",
                               "topos:asset-amb"}

    def test_export_frame_domain_not_relabelled_as_samples(
            self, tmp_path: Path) -> None:
        """回归（域混淆）：帧数值不得被直接当样本写进 AAF——24fps×48k 下
        帧 240（10s）必须落在样本 480000，而不是 240（2000 倍位移）。"""
        from src.features.edit.adapters import aaf_adapter as aa
        import aaf2

        clip = ClipInstance(clip_id="c1", asset_id="a1", track_id="t1",
                            timeline_start=240, timeline_duration=480,
                            source_start=0, source_duration=480,
                            audio_sample_offset=17)
        track = EditTrack("t1", "T1", "audio", 0, clips=(clip,))
        seq = TimelineSequence(audio_tracks=(track,),
                               fps=Fraction(24, 1), sample_rate=48000)
        path = tmp_path / "domain.aaf"
        export_aaf(seq, path)
        with aaf2.open(str(path)) as f:
            mob = next(iter(f.content.compositionmobs()))
            slot = mob.slots[0]
            seq_seg = slot.segment
            items = list(seq_seg.components)
        # 240 帧 + 17 样本偏移 → 起点样本 480017；末帧边界 720 帧 →
        # 1440000 样本；clip 本体 = 1440000 − 480017
        assert int(items[0].length) == 480017          # Filler 前导
        assert int(items[1].length) == 1440000 - 480017  # clip 本体
        assert int(slot.edit_rate.numerator) == 48000

    def test_protocol_adapter_methods(self, tmp_path: Path) -> None:
        adapter = AafAudioAdapter()
        path = tmp_path / "out.aaf"
        export = adapter.export_from_canonical(
            _sequence(), options={"output_path": str(path)})
        assert Path(export.output_path).is_file()
        info = adapter.inspect(path)
        assert info.supported
        imported = adapter.import_to_canonical(path)
        assert imported.sequences

    def test_bare_sourceclip_slot_imported(self, tmp_path: Path) -> None:
        """回归：裸 SourceClip 槽（无 Sequence 包装）是真实 AAF 常见合法
        形态——不得因无 components 属性静默导入成空轨。"""
        import aaf2

        path = tmp_path / "bare.aaf"
        with aaf2.open(str(path), "w") as f:
            comp = f.create.from_name("CompositionMob")
            comp.name = "Bare"
            f.content.mobs.append(comp)
            srcmob = f.create.from_name("SourceMob")
            srcmob.name = "topos:asset-x"
            desc = f.create.from_name("PCMDescriptor")
            desc["SampleRate"].value = aaf2.rational.AAFRational(48000)
            desc["AudioSamplingRate"].value = aaf2.rational.AAFRational(48000)
            desc["Channels"].value = 2
            desc["QuantizationBits"].value = 24
            desc["BlockAlign"].value = 6
            desc["AverageBPS"].value = 288000
            desc["Length"].value = 0
            srcmob.descriptor = desc
            sslot = f.create.from_name("TimelineMobSlot", slot_id=1)
            sslot.edit_rate = aaf2.rational.AAFRational(48000)
            fill = f.create.from_name("Filler")
            fill.media_kind = "Sound"
            fill.length = 48000
            sslot.segment = fill
            srcmob.slots.append(sslot)
            f.content.mobs.append(srcmob)
            sc = f.create.from_name("SourceClip")
            sc.media_kind = "Sound"
            sc.length = 48000
            sc.start = 0
            sc.mob = srcmob
            sc.slot = sslot
            slot = f.create.from_name("TimelineMobSlot", slot_id=1)
            slot.name = "DX"
            slot.segment = sc  # 裸 segment（不包 Sequence）
            slot.edit_rate = aaf2.rational.AAFRational(48000)
            slot.origin = 0
            comp.slots.append(slot)
            f.save()

        result = import_aaf(path)
        assert result.sequences
        track = result.sequences[0].audio_tracks[0]
        assert len(track.clips) == 1
        clip = track.clips[0]
        # 样本域位置重述为帧域 (帧, 偏移)：0 样本 → 帧 0；48000 样本
        # @24fps → 24 帧；源起点 0 样本 → 帧 0。
        assert clip.timeline_start == 0
        assert clip.timeline_duration == 24
        assert clip.source_start == 0
        assert clip.audio_sample_offset == 0

    def test_import_rejects_non_aaf(self, tmp_path: Path) -> None:
        bogus = tmp_path / "bogus.aaf"
        bogus.write_bytes(b"not an aaf")
        with pytest.raises(Exception):
            inspect_aaf(bogus)
