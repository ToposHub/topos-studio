"""
Audio Interchange 测试（AM-08 阶段 4.5）。

验证（计划表 4.5）：
- BWF timecode：SMPTE 时间码转换正确（HH:MM:SS:FF）。
- CSV cue sheet：从 sequence 生成 cue 清单，列含 track/clip/timecode/时长。
- AAF/OMF/XML 能力评估：列出可用格式与许可状态。
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.interchange import (
    frames_to_timecode,
    timecode_to_frames,
    frames_to_samples,
    samples_to_frames,
    parse_timecode,
    CueEntry,
    CueSheet,
    build_cue_sheet_from_sequence,
    INTERCHANGE_CAPABILITIES,
    get_available_formats,
    evaluate_format,
)
from src.features.edit.core.timeline_v2 import (
    EditTrack, TimelineSequence, ClipInstance,
)


FPS = 24.0


def _make_clip(track_id: str, start: int, duration: int, asset_id: str = "a1") -> ClipInstance:
    return ClipInstance(
        clip_id=f"{track_id}_{start}", asset_id=asset_id, track_id=track_id,
        source_start=0, source_duration=duration,
        timeline_start=start, timeline_duration=duration,
    )


def _make_sequence_with_clips():
    """构造含 2 轨 + 多 clip 的 sequence。"""
    t1 = EditTrack(
        track_id="t1", track_type="audio", index=0, name="Dialogue",
        clips=(_make_clip("t1", 0, 48), _make_clip("t1", 72, 24)),
    )
    t2 = EditTrack(
        track_id="t2", track_type="audio", index=1, name="Music",
        clips=(_make_clip("t2", 0, 96, "a2"),),
    )
    return TimelineSequence(audio_tracks=(t1, t2), fps=FPS)


class TestTimecode:
    """SMPTE 时间码转换。"""

    def test_zero_frame(self):
        assert frames_to_timecode(0, 24.0) == "00:00:00:00"

    def test_one_second(self):
        """24 帧后 = 1 秒。"""
        assert frames_to_timecode(24, 24.0) == "00:00:01:00"

    def test_one_minute(self):
        """24*60=1440 帧 = 1 分钟。"""
        assert frames_to_timecode(1440, 24.0) == "00:01:00:00"

    def test_one_hour(self):
        """24*3600=86400 帧 = 1 小时。"""
        assert frames_to_timecode(86400, 24.0) == "01:00:00:00"

    def test_frames_field(self):
        """帧字段正确（0-23）。"""
        assert frames_to_timecode(5, 24.0) == "00:00:00:05"
        assert frames_to_timecode(23, 24.0) == "00:00:00:23"

    def test_25fps(self):
        """25 fps 转换。"""
        assert frames_to_timecode(25, 25.0) == "00:00:01:00"

    def test_roundtrip(self):
        """时间码往返（frame → tc → frame）。"""
        for f in [0, 1, 24, 100, 1440, 86400, 12345]:
            tc = frames_to_timecode(f, 24.0)
            back = timecode_to_frames(tc, 24.0)
            assert back == f, f"往返失败: {f} → {tc} → {back}"

    def test_drop_frame_2997(self):
        """29.97 drop-frame 生成（分号分隔）。"""
        tc = frames_to_timecode(0, 29.97, drop_frame=True)
        assert ";" in tc  # drop-frame 用分号

    def test_invalid_fps(self):
        assert frames_to_timecode(100, 0) == "00:00:00:00"


class TestDropFrameTimecode:
    """P0-05：SMPTE ST 12-1 drop-frame 双向转换。

    参考值（30000/1001 DF）：
    - 第 1 分钟第一帧 F=1800 → 00:01:00;02
    - 第 2 分钟第一帧 F=3598 → 00:02:00;04
    - 第 10 分钟第一帧 F=17982 → 00:10:00;00（整十分钟不丢帧号）
    - 每小时 107892 帧
    """

    def test_minute_boundaries_2997(self):
        assert frames_to_timecode(0, 29.97, drop_frame=True) == "00:00:00;00"
        assert frames_to_timecode(1799, 29.97, drop_frame=True) == "00:00:59;29"
        assert frames_to_timecode(1800, 29.97, drop_frame=True) == "00:01:00;02"
        assert frames_to_timecode(3597, 29.97, drop_frame=True) == "00:01:59;29"
        assert frames_to_timecode(3598, 29.97, drop_frame=True) == "00:02:00;02"

    def test_ten_minute_boundary_2997(self):
        # 第 9 分钟结束 → 第 10 分钟（完整分钟，不丢帧号）
        assert frames_to_timecode(17981, 29.97, drop_frame=True) == "00:09:59;29"
        assert frames_to_timecode(17982, 29.97, drop_frame=True) == "00:10:00;00"
        assert frames_to_timecode(17983, 29.97, drop_frame=True) == "00:10:00;01"
        # 第 11 分钟恢复丢帧号
        assert frames_to_timecode(19782, 29.97, drop_frame=True) == "00:11:00;02"

    def test_hour_boundaries_2997(self):
        # 1 小时 = 107892 帧
        assert frames_to_timecode(107892, 29.97, drop_frame=True) == "01:00:00;00"
        # 10 小时整
        assert frames_to_timecode(1078920, 29.97, drop_frame=True) == "10:00:00;00"

    def test_roundtrip_exhaustive_boundaries_2997(self):
        boundaries = [0, 1, 1798, 1799, 1800, 1801, 3597, 3598, 5396, 5397,
                      17981, 17982, 17983, 19780, 19782, 107891, 107892,
                      107893, 215784, 2589408]  # 24h+
        for f in boundaries:
            tc = frames_to_timecode(f, 29.97, drop_frame=True)
            back = timecode_to_frames(tc, 29.97, drop_frame=True)
            assert back == f, f"DF 往返失败: {f} → {tc} → {back}"

    def test_roundtrip_long_duration_2997(self):
        # 逐分钟扫描 3 小时，覆盖每个丢/不丢分钟的边界
        frames_per_10min = 17982
        for f in range(0, 3 * 107892, frames_per_10min // 6):
            tc = frames_to_timecode(f, 29.97, drop_frame=True)
            back = timecode_to_frames(tc, 29.97, drop_frame=True)
            assert back == f, f"DF 往返失败: {f} → {tc} → {back}"

    def test_df_5994(self):
        # 60000/1001：名义 60fps，每分钟丢 4 帧
        assert frames_to_timecode(0, 59.94, drop_frame=True) == "00:00:00;00"
        assert frames_to_timecode(3599, 59.94, drop_frame=True) == "00:00:59;59"
        assert frames_to_timecode(3600, 59.94, drop_frame=True) == "00:01:00;04"
        # 10 分钟块 = 36000 - 9*4 = 35964 帧
        assert frames_to_timecode(35964, 59.94, drop_frame=True) == "00:10:00;00"
        # 1 小时 = 215784 帧
        assert frames_to_timecode(215784, 59.94, drop_frame=True) == "01:00:00;00"

    def test_roundtrip_5994(self):
        for f in [0, 3600, 3596, 35964, 71928, 215784, 1234567]:
            tc = frames_to_timecode(f, 59.94, drop_frame=True)
            back = timecode_to_frames(tc, 59.94, drop_frame=True)
            assert back == f, f"59.94 DF 往返失败: {f} → {tc} → {back}"

    def test_non_displayable_df_timecode_rejected(self):
        from pytest import raises
        # 00:01:00;00 与 00:01:00;01 是被丢弃的编号，不可解析
        with raises(ValueError, match="[Nn]on-displayable"):
            timecode_to_frames("00:01:00;00", 29.97, drop_frame=True)
        with raises(ValueError, match="[Nn]on-displayable"):
            timecode_to_frames("00:01:00;01", 29.97, drop_frame=True)
        # 整 10 分钟处 ff=0/1 合法
        assert timecode_to_frames("00:10:00;00", 29.97, drop_frame=True) == 17982
        assert timecode_to_frames("00:00:00;00", 29.97, drop_frame=True) == 0

    def test_df_with_non_ntsc_fps_rejected(self):
        from pytest import raises
        with raises(ValueError, match="30000/1001"):
            frames_to_timecode(100, 24.0, drop_frame=True)
        with raises(ValueError, match="30000/1001"):
            timecode_to_frames("00:00:00:00", 25.0, drop_frame=True)

    def test_rational_fps_exact(self):
        from src.shared.media.metadata import Rational
        fps = Rational(30000, 1001)
        tc = frames_to_timecode(1800, fps, drop_frame=True)  # type: ignore[arg-type]
        assert tc == "00:01:00;02"
        assert timecode_to_frames(tc, fps, drop_frame=True) == 1800  # type: ignore[arg-type]

    def test_out_of_range_fields_rejected(self):
        from pytest import raises
        with raises(ValueError, match="out of range"):
            timecode_to_frames("00:00:60:00", 24.0)
        with raises(ValueError, match="out of range"):
            timecode_to_frames("00:60:00:00", 24.0)
        with raises(ValueError, match="out of range"):
            timecode_to_frames("00:00:00:24", 24.0)
        with raises(ValueError, match="Invalid timecode"):
            timecode_to_frames("00:00:01", 24.0)
        with raises(ValueError, match="Invalid timecode"):
            timecode_to_frames("00:00:01:00:00", 24.0)

    def test_empty_timecode_returns_zero(self):
        assert timecode_to_frames("", 24.0) == 0


class TestRationalSampleConversion:
    """P0-05：Rational 帧率的帧/样本精确转换。"""

    def test_rational_frames_to_samples_exact(self):
        from src.shared.media.metadata import Rational
        # 24fps = 2000 样本/帧
        assert frames_to_samples(48, Rational(24, 1), 48000) == 96000  # type: ignore[arg-type]
        # 30000/1001: 1 帧 = 48000*1001/30000 = 1601.6 → 1601
        assert frames_to_samples(1, Rational(30000, 1001), 48000) == 1601  # type: ignore[arg-type]
        # 1001 帧后无累积漂移：1001*1001*48000/30000 = 1603201.6 → 1603201
        assert frames_to_samples(1001, Rational(30000, 1001), 48000) == 1603201  # type: ignore[arg-type]

    def test_samples_to_frames_inverse(self):
        from src.shared.media.metadata import Rational
        assert samples_to_frames(96000, Rational(24, 1), 48000) == 48  # type: ignore[arg-type]
        assert samples_to_frames(2000, 24.0, 48000) == 1
        assert samples_to_frames(0, 24.0, 48000) == 0


class TestFramesToSamples:
    """帧→采样转换。"""

    def test_basic(self):
        """24fps, 48kHz：1 帧 = 2000 样本。"""
        assert frames_to_samples(1, 24.0, 48000) == 2000

    def test_zero(self):
        assert frames_to_samples(0, 24.0, 48000) == 0

    def test_multiple_frames(self):
        """48 帧（2秒）= 96000 样本。"""
        assert frames_to_samples(48, 24.0, 48000) == 96000


class TestCueSheet:
    """CSV cue sheet 生成。"""

    def test_build_from_sequence(self):
        """从 sequence 构建 cue sheet。"""
        seq = _make_sequence_with_clips()
        sheet = build_cue_sheet_from_sequence(seq, project_name="Test")
        assert sheet.project_name == "Test"
        assert sheet.fps == FPS
        # 3 个 clip（t1 两个 + t2 一个）
        assert len(sheet.entries) == 3

    def test_entries_have_timecode(self):
        """entries 含 SMPTE timecode。"""
        seq = _make_sequence_with_clips()
        sheet = build_cue_sheet_from_sequence(seq)
        for e in sheet.entries:
            assert ":" in e.timeline_start_tc
            assert ":" in e.timeline_end_tc

    def test_entries_track_names(self):
        """entries 含轨道名。"""
        seq = _make_sequence_with_clips()
        sheet = build_cue_sheet_from_sequence(seq)
        track_names = {e.track_name for e in sheet.entries}
        assert "Dialogue" in track_names
        assert "Music" in track_names

    def test_entries_sorted_by_time(self):
        """entries 按开始时间排序。"""
        seq = _make_sequence_with_clips()
        sheet = build_cue_sheet_from_sequence(seq)
        starts = [timecode_to_frames(e.timeline_start_tc, FPS) for e in sheet.entries]
        assert starts == sorted(starts)

    def test_csv_export(self, tmp_path):
        """CSV 文件写入。"""
        seq = _make_sequence_with_clips()
        sheet = build_cue_sheet_from_sequence(seq, project_name="MyProject")
        path = str(tmp_path / "cue.csv")
        ok = sheet.to_csv(path)
        assert ok
        assert Path(path).exists()
        content = Path(path).read_text(encoding="utf-8")
        assert "MyProject" in content
        assert "Dialogue" in content
        assert "Music" in content
        assert "Start TC" in content  # 列头

    def test_csv_columns(self, tmp_path):
        """CSV 含所有必需列。"""
        seq = _make_sequence_with_clips()
        sheet = build_cue_sheet_from_sequence(seq)
        path = str(tmp_path / "cols.csv")
        sheet.to_csv(path)
        with open(path, encoding="utf-8") as f:
            reader = csv.reader(f)
            rows = list(reader)
        # 找到列头行
        header = None
        for r in rows:
            if r and r[0] == "Track":
                header = r
                break
        assert header is not None
        assert "Start TC" in header
        assert "End TC" in header
        assert "Duration TC" in header

    def test_total_duration_in_sheet(self):
        """cue sheet 含总时长 timecode。"""
        seq = _make_sequence_with_clips()
        sheet = build_cue_sheet_from_sequence(seq)
        # 96 帧总时长（t2 的 clip）
        assert "00:00:04:" in sheet.total_duration_tc  # 96/24=4秒


class TestInterchangeCapabilities:
    """互操作能力评估。"""

    def test_bwf_available(self):
        """BWF 已实现。"""
        bwf = evaluate_format("BWF (Broadcast Wave)")
        assert bwf is not None
        assert bwf.available is True
        assert bwf.priority == "high"

    def test_csv_available(self):
        """F1.3/R4：CSV cue sheet 无生产入口——与注册表 DORMANT 对齐为 False。"""
        csv_fmt = evaluate_format("CSV Cue Sheet")
        assert csv_fmt is not None
        assert csv_fmt.available is False
        assert "注册表" in csv_fmt.notes

    def test_stems_available(self):
        """Stems 已实现。"""
        stems = evaluate_format("Audio Stems")
        assert stems is not None
        assert stems.available is True

    def test_aaf_evaluated(self):
        """AAF 已评估（可用性 + 许可记录）。"""
        aaf = evaluate_format("AAF")
        assert aaf is not None
        assert "aaf2" in aaf.license or "SDK" in aaf.license
        assert aaf.priority == "low"

    def test_omf_evaluated(self):
        """OMF 已评估。"""
        omf = evaluate_format("OMF")
        assert omf is not None
        assert omf.priority == "low"

    def test_fcpxml_evaluated(self):
        """Q03（第六轮审计）：FCPXML 受控子集已接生产入口，静态表与注册表一致。"""
        xml = evaluate_format("Final Cut Pro XML (FCPXML)")
        assert xml is not None
        assert xml.priority == "medium"
        assert xml.available, "capability_registry 已 PARTIAL，静态表不得再声明不可用"

    def test_get_available_formats(self):
        """get_available_formats 返回已实现格式。"""
        available = get_available_formats()
        names = {f.name for f in available}
        assert len(available) >= 3
        assert all(f.available for f in available)

    def test_all_formats_have_license_info(self):
        """所有格式记录许可信息（计划表要求评估库与许可）。"""
        for fmt in INTERCHANGE_CAPABILITIES:
            assert fmt.license, f"{fmt.name} 缺许可信息"
            assert fmt.notes, f"{fmt.name} 缺评估说明"

    def test_evaluate_unknown_returns_none(self):
        """未知格式返回 None。"""
        assert evaluate_format("nonexistent") is None


class TestCueEntryDataclass:
    """CueEntry 数据类。"""

    def test_construction(self):
        e = CueEntry(
            track_name="T1", clip_name="clip1",
            timeline_start_tc="00:00:01:00", timeline_end_tc="00:00:02:00",
            duration_tc="00:00:01:00", duration_seconds=1.0,
            asset_name="asset1",
        )
        assert e.track_name == "T1"
        assert e.channels == 2  # 默认 stereo
        assert e.sample_rate == 48000


class TestAafConditionalGate:
    """F6.3 · AAF 条件延期声明——不冒充可重建时间线。"""

    def test_aaf_declared_unavailable_with_actionable_advice(self):
        from src.features.edit.core.interchange import evaluate_format

        aaf = evaluate_format("AAF")
        assert aaf is not None and aaf.available is False
        # 可执行交接建议（不是空白拒绝）
        assert "Stems" in aaf.notes and "routing.json" in aaf.notes
        assert "FCPXML" in aaf.notes  # 外部转换路径
        assert "不冒充" in aaf.notes or "不以" in aaf.notes

    def test_aaf_not_in_available_formats(self):
        from src.features.edit.core.interchange import get_available_formats

        assert all("AAF" not in f.name for f in get_available_formats())
