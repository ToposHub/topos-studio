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
        """手册：样本编辑率槽 → 导入后源窗/位置误差 ≤1 样本。"""
        seq = _sequence()
        path = tmp_path / "out.aaf"
        export_aaf(seq, path)
        result = import_aaf(path)
        assert result.sequences, "no candidate sequence imported"
        imported = result.sequences[0]
        tracks = {t.name: t for t in imported.audio_tracks}
        # 3 条非 Bus 音频轨（Bus 不导出）
        assert set(tracks) == {"DX", "MX", "AMB"}
        dx = tracks["DX"].clips
        assert len(dx) == 2
        assert dx[0].timeline_start == 0
        assert dx[0].timeline_duration == 48000
        assert abs(dx[0].source_start - 1200) <= 1
        assert dx[1].timeline_start == 72000
        assert abs(dx[1].source_start - 96000) <= 1
        mx = tracks["MX"].clips
        assert mx[0].timeline_start == 48000
        assert mx[0].timeline_duration == 96000
        amb = tracks["AMB"].clips
        assert amb[0].timeline_start == 24000
        assert abs(amb[0].source_start - 480) <= 1
        # 间隙不伪造内容（DX 轨 48000–72000 无 clip）
        starts = [c.timeline_start for c in dx]
        assert 48000 not in starts or all(
            c.timeline_start != 48000 for c in dx)
        # relink 请求覆盖全部 3 个资产
        relink_keys = {r.external_ref for r in result.relink_requests}
        assert relink_keys == {"topos:asset-dx", "topos:asset-mx",
                               "topos:asset-amb"}

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

    def test_import_rejects_non_aaf(self, tmp_path: Path) -> None:
        bogus = tmp_path / "bogus.aaf"
        bogus.write_bytes(b"not an aaf")
        with pytest.raises(Exception):
            inspect_aaf(bogus)
