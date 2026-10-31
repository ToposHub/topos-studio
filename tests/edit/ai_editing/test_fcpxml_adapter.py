"""TAEP Phase 7 — FCPXML adapter 测试。

验收（TAEP §12 P7）：
- inspect 报告版本/资源/不支持特性，不导入。
- import 受支持子集到 canonical TimelineSequence + MediaAsset 描述。
- export 从 canonical model 生成 FCPXML。
- round-trip：Topos → FCPXML → Topos 的 clip placement/时长/引用一致（受支持子集）。
- 不支持特性产出 InterchangeReport 的 dropped/requires_user_action 条目，不静默丢。
- 资产重连请求生成。
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.adapters.fcpxml_adapter import FCPXMLAdapter
from src.features.edit.adapters.interchange_types import (
    InterchangeEntryCategory,
)
from src.features.edit.core.clip_params import CropParams, SpeedParams, TransformParams
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineMarker, TimelineSequence
from src.shared.media.metadata import Rational


# ================================================================
# FCPXML fixture（手写最小合法 FCPXML）
# ================================================================


FCPXML_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.9">
  <resources>
    <format id="r1" name="1920x1080" frameDuration="1001/24000s" width="1920" height="1080"/>
    <asset id="r2" name="clip_a" hasVideo="1" start="0s" duration="7200s">
      <media-rep kind="original" src="file:///home/clip_a.mp4"/>
    </asset>
    <asset id="r3" name="clip_b" hasVideo="1" start="0s" duration="3600s"/>
  </resources>
  <library>
    <event name="Import Test">
      <project name="Test Project">
        <sequence id="seq1" name="Main">
          <spine>
            <asset-clip name="c0" ref="r2" offset="0s" duration="1200s" tcStart="0s"/>
            <asset-clip name="c1" ref="r3" offset="1200s" duration="600s" tcStart="240s"/>
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
"""


@pytest.fixture
def fcpxml_file(tmp_path):
    p = tmp_path / "test.fcpxml"
    p.write_text(FCPXML_FIXTURE, encoding="utf-8")
    return p


# ================================================================
# inspect
# ================================================================


def test_inspect_reports_version_and_assets(fcpxml_file):
    adapter = FCPXMLAdapter()
    inspection = adapter.inspect(fcpxml_file)
    assert inspection.format_id == "fcpxml"
    assert inspection.version == "1.9"
    assert inspection.supported is True
    assert inspection.asset_count == 2
    assert inspection.timeline_count == 1


def test_inspect_detects_unsupported_features(tmp_path):
    p = tmp_path / "has_multicam.fcpxml"
    p.write_text("""<?xml version="1.0"?><fcpxml version="1.9">
      <resources><format id="r1" frameDuration="1001/24000s" width="1920" height="1080"/></resources>
      <library><event><project><sequence><spine>
        <multicamClip name="mc"/>
      </spine></sequence></project></event></library></fcpxml>""", encoding="utf-8")
    inspection = FCPXMLAdapter().inspect(p)
    assert "multicam" in inspection.unsupported_features
    assert any(e.category == InterchangeEntryCategory.DROPPED for e in inspection.report.entries)


def test_inspect_parse_error_returns_unsupported(tmp_path):
    p = tmp_path / "broken.fcpxml"
    p.write_text("not xml <", encoding="utf-8")
    inspection = FCPXMLAdapter().inspect(p)
    assert inspection.supported is False
    assert inspection.report is not None
    assert inspection.report.has_errors


# ================================================================
# import
# ================================================================


def test_import_produces_sequence_and_assets(fcpxml_file):
    adapter = FCPXMLAdapter()
    result = adapter.import_to_canonical(fcpxml_file)
    assert len(result.sequences) == 1
    seq = result.sequences[0]
    # fps 从 frameDuration 解析为 Rational
    assert seq.fps == Rational(24000, 1001)
    assert seq.width == 1920 and seq.height == 1080
    # 2 video clips
    assert len(seq.video_tracks[0].clips) == 2
    c0 = seq.video_tracks[0].clips[0]
    c1 = seq.video_tracks[0].clips[1]
    # placement preserved
    assert c0.timeline_start == 0
    assert c1.timeline_start >= c0.timeline_start + c0.timeline_duration  # 相邻/不重叠
    # 资产描述
    assert len(result.assets) == 2
    # 资产重连请求（有 src 的）
    assert any(r.external_ref == "r2" for r in result.relink_requests)


def test_import_report_categories(fcpxml_file):
    result = FCPXMLAdapter().import_to_canonical(fcpxml_file)
    report = result.report
    assert report is not None
    # 有 preserved 条目（asset-clip）
    assert any(e.category == InterchangeEntryCategory.PRESERVED for e in report.entries)


# ================================================================
# export
# ================================================================


def _make_sequence_for_export() -> TimelineSequence:
    clip_a = ClipInstance(
        clip_id="c1", asset_id="a1", track_id="v1",
        timeline_start=0, timeline_duration=120,
        source_start=0, source_duration=120,
        speed=SpeedParams(), transform=TransformParams(), crop=CropParams(),
    )
    clip_b = ClipInstance(
        clip_id="c2", asset_id="a2", track_id="v1",
        timeline_start=120, timeline_duration=60,
        source_start=240, source_duration=60,
        speed=SpeedParams(), transform=TransformParams(), crop=CropParams(),
    )
    v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=(clip_a, clip_b))
    return TimelineSequence(
        timeline_id="t1", name="Export Test", fps=Rational(24, 1),
        width=1920, height=1080, sample_rate=48000,
        video_tracks=(v1,),
        markers=(TimelineMarker(marker_id="m1", frame=60, name="beat"),),
    )


def test_export_writes_valid_fcpxml(tmp_path):
    seq = _make_sequence_for_export()
    out = tmp_path / "out.fcpxml"
    adapter = FCPXMLAdapter()
    result = adapter.export_from_canonical(seq, options={"output_path": str(out)})
    assert out.exists()
    # 可被重新 parse
    tree = ET.parse(str(out))
    root = tree.getroot()
    assert root.tag == "fcpxml"
    # 含两个 asset-clip
    clips = root.findall(".//asset-clip")
    assert len(clips) == 2
    # 报告含 preserved
    assert any(e.category == InterchangeEntryCategory.PRESERVED for e in result.report.entries)


# ================================================================
# round-trip（TAEP §12 P7 核心验收）
# ================================================================


def test_round_trip_topos_to_fcpxml_to_topos(tmp_path):
    """Topos → FCPXML → Topos：clip placement/时长/引用一致（受支持子集）。"""
    seq = _make_sequence_for_export()
    out = tmp_path / "rt.fcpxml"
    adapter = FCPXMLAdapter()
    adapter.export_from_canonical(seq, options={"output_path": str(out)})

    # 重新导入
    result = adapter.import_to_canonical(out)
    assert len(result.sequences) == 1
    rt_seq = result.sequences[0]
    # 时长一致（fps round-trip：24/1）
    assert rt_seq.fps == Rational(24, 1)
    rt_clips = rt_seq.video_tracks[0].clips
    # clip 数量一致
    assert len(rt_clips) == 2
    # placement 一致（start/duration）
    assert rt_clips[0].timeline_start == 0
    assert rt_clips[0].timeline_duration == 120
    assert rt_clips[1].timeline_start == 120
    assert rt_clips[1].timeline_duration == 60
    # source start 一致
    assert rt_clips[1].source_start == 240


def test_export_subtitle_requires_user_action(tmp_path):
    """字幕导出首版无样式映射保证 -> requires_user_action。"""
    from src.features.edit.core.timeline_v2 import SubtitleClip
    seq = _make_sequence_for_export()
    # 加一条字幕轨
    sub = SubtitleClip(clip_id="s1", track_id="s1", timeline_start=0, timeline_duration=60, text="hello")
    s_track = EditTrack(track_id="s1", name="S1", track_type="subtitle", index=0, subtitle_clips=(sub,))
    seq = TimelineSequence(
        timeline_id="t1", name="Sub Test", fps=Rational(24, 1),
        width=1920, height=1080, sample_rate=48000,
        video_tracks=seq.video_tracks, subtitle_tracks=(s_track,),
    )
    out = tmp_path / "sub.fcpxml"
    adapter = FCPXMLAdapter()
    result = adapter.export_from_canonical(seq, options={"output_path": str(out)})
    assert any(
        e.category == InterchangeEntryCategory.REQUIRES_USER_ACTION and e.element == "subtitle"
        for e in result.report.entries
    )


def test_export_dropped_unsupported_transition(tmp_path):
    """不支持的 transition 类型 -> dropped 条目。"""
    from src.features.edit.core.timeline_v2 import TransitionInstance
    seq = _make_sequence_for_export()
    # dip_to_color 在受支持白名单内，但首版 export 可能标记——用 wipe 测
    trans = TransitionInstance(
        transition_id="tr1", transition_type="wipe", track_id="v1",
        left_clip_id="c1", right_clip_id="c2", center_frame=120, duration=12,
    )
    v1 = EditTrack(
        track_id="v1", name="V1", track_type="video", index=0,
        clips=seq.video_tracks[0].clips, transitions=(trans,),
    )
    seq = TimelineSequence(
        timeline_id="t1", name="Trans Test", fps=Rational(24, 1),
        width=1920, height=1080, sample_rate=48000, video_tracks=(v1,),
    )
    out = tmp_path / "trans.fcpxml"
    adapter = FCPXMLAdapter()
    result = adapter.export_from_canonical(seq, options={"output_path": str(out)})
    # wipe 不是 cross_dissolve -> dropped（首版只精确导出 cross_dissolve）
    assert any(
        e.category == InterchangeEntryCategory.DROPPED and e.element == "transition"
        for e in result.report.entries
    )


def test_export_asset_duration_reflects_clip_extents(tmp_path):
    """P1-16: <asset duration> 取该素材实际使用的最大源范围，不再硬编码 7200 帧。"""
    from src.features.edit.adapters.fcpxml_adapter import _frames_to_time

    seq = _make_sequence_for_export()
    # a1: 0+120=120 帧；a2: 240+60=300 帧
    out = tmp_path / "dur.fcpxml"
    FCPXMLAdapter().export_from_canonical(seq, options={"output_path": str(out)})
    root = ET.parse(str(out)).getroot()
    durations = {
        a.get("id"): a.get("duration")
        for a in root.findall(".//resources/asset")
    }
    fps = Rational(24, 1)
    assert durations["r2"] == _frames_to_time(120, fps)
    assert durations["r3"] == _frames_to_time(300, fps)


# ================================================================
# M24（R06）· 工程采样率随包往返（不被 48000 默认掩盖）
# ================================================================

def test_import_reads_audio_rate_44100(tmp_path):
    """format@audioRate="44100" → 导入序列 sample_rate=44100（此前硬编码
    48000，44.1k 工程的子帧偏移换算被默认值掩盖）。"""
    p = tmp_path / "sr441.fcpxml"
    p.write_text("""<?xml version="1.0"?><fcpxml version="1.9">
      <resources><format id="r1" frameDuration="1/24s" width="1920"
        height="1080" audioRate="44100"/></resources>
      <library><event><project><sequence><spine>
        <asset-clip name="c" ref="r2" offset="0s" duration="24/24s"
          start="0s"/>
      </spine></sequence></project></event></library></fcpxml>""", encoding="utf-8")
    result = FCPXMLAdapter().import_to_canonical(p)
    assert result.sequences
    assert result.sequences[0].sample_rate == 44100


def test_import_48k_form_normalized(tmp_path):
    p = tmp_path / "sr48k.fcpxml"
    p.write_text("""<?xml version="1.0"?><fcpxml version="1.9">
      <resources><format id="r1" frameDuration="1/24s" width="1920"
        height="1080" audioRate="48k"/></resources>
      <library><event><project><sequence><spine>
        <asset-clip name="c" ref="r2" offset="0s" duration="24/24s"
          start="0s"/>
      </spine></sequence></project></event></library></fcpxml>""", encoding="utf-8")
    result = FCPXMLAdapter().import_to_canonical(p)
    assert result.sequences[0].sample_rate == 48000


def test_import_invalid_audio_rate_falls_back_reported(tmp_path):
    p = tmp_path / "srbad.fcpxml"
    p.write_text("""<?xml version="1.0"?><fcpxml version="1.9">
      <resources><format id="r1" frameDuration="1/24s" width="1920"
        height="1080" audioRate="garbage"/></resources>
      <library><event><project><sequence><spine>
        <asset-clip name="c" ref="r2" offset="0s" duration="24/24s"
          start="0s"/>
      </spine></sequence></project></event></library></fcpxml>""", encoding="utf-8")
    result = FCPXMLAdapter().import_to_canonical(p)
    assert result.sequences[0].sample_rate == 48000
    assert any("audioRate" in d.get("message", "")
               for d in result.report.diagnostics)


def test_export_writes_audio_rate_from_sequence(tmp_path, fcpxml_file):
    """导出 format 带 audioRate——工程真实采样率随包往返。"""
    from src.features.edit.core.timeline_v2 import TimelineSequence
    out = tmp_path / "out.fcpxml"
    seq = TimelineSequence(timeline_id="t", name="T", fps=Rational(25, 1),
                           sample_rate=44100)
    FCPXMLAdapter().export_from_canonical(
        seq, options={"output_path": str(out)})
    fmt = ET.parse(out).getroot().find(".//format")
    assert fmt is not None
    assert fmt.get("audioRate") == "44100"


# ================================================================
# M24.2 · 子帧 offset 有理时基映射（导入/导出细则）
# ================================================================


def _write_ntsc_clip(tmp_path, offset_attr: str, audio_rate: str = "48000"):
    p = tmp_path / f"sub_{abs(hash((offset_attr, audio_rate)))}.fcpxml"
    p.write_text(f"""<?xml version="1.0"?><fcpxml version="1.9">
      <resources><format id="r1" frameDuration="1001/30000s" width="1920"
        height="1080" audioRate="{audio_rate}"/>
        <asset id="r2" name="c" hasVideo="1" hasAudio="1" start="0s"
          duration="7200s"/></resources>
      <library><event><project><sequence><spine>
        <asset-clip name="c" ref="r2" offset="{offset_attr}"
          duration="30000/30000s" start="0s"/>
      </spine></sequence></project></event></library></fcpxml>""", encoding="utf-8")
    return p


def test_subframe_decompose_compose_identity():
    """模型层：分解↔合成严格互逆（纯有理数，NTSC 非整帧率）。"""
    from fractions import Fraction

    from src.features.edit.core.sample_time import (
        TimelineSampleClock,
        compose_timeline_seconds,
        subframe_decompose,
    )
    clock = TimelineSampleClock(Rational(30000, 1001), 48000)
    for sec in (Fraction(0), Fraction(1, 2), Fraction(1001, 60000),
                Fraction(3600), Fraction(1001, 30000) * 7 + Fraction(1, 7)):
        frame, offset = subframe_decompose(clock, sec)
        assert 0 <= offset < clock.samples_per_frame
        assert compose_timeline_seconds(clock, frame, offset) == sec


def test_offset_to_reference_rate_exact_residue():
    """44.1k 声明率 → 48k 参考率：整数执行 + 精确残差（不静默丢）。"""
    from fractions import Fraction

    from src.features.edit.core.sample_time import offset_to_reference_rate
    # 100 样本@44100 = 4800000/44100 = 16000/147 = 108 + 124/147
    assert offset_to_reference_rate(Fraction(100), 44100) == (
        108, Fraction(124, 147))
    # 同率换算零残差
    assert offset_to_reference_rate(Fraction(800), 48000) == (800, Fraction(0))


def test_import_maps_subframe_offset_ntsc(tmp_path):
    """显式 offset 半帧（1001/60000s@NTSC）→ 参考率 800 样本 + 量化残差记账。"""
    p = _write_ntsc_clip(tmp_path, "1001/60000s")
    result = FCPXMLAdapter().import_to_canonical(p)
    clip = result.sequences[0].video_tracks[0].clips[0]
    assert clip.timeline_start == 0  # floor 半帧 = 0 帧
    # 残差 1001/60000s × 48000 = 800.8 样本 → 执行 800，残差 0.8 记账
    assert clip.audio_sample_offset == 800
    assert any("量化到 48k" in d.get("message", "")
               for d in result.report.diagnostics)
    assert any(e.category == InterchangeEntryCategory.CONVERTED
               and "子帧偏移" in e.description
               for e in result.report.entries)


def test_import_frame_aligned_offset_unchanged(tmp_path):
    """帧对齐 offset（1 帧 = 1001/30000s）→ 偏移 0，无新增条目。"""
    p = _write_ntsc_clip(tmp_path, "1001/30000s")
    result = FCPXMLAdapter().import_to_canonical(p)
    clip = result.sequences[0].video_tracks[0].clips[0]
    assert clip.timeline_start == 1
    assert clip.audio_sample_offset == 0
    assert not any("子帧" in d.get("message", "")
                   for d in result.report.diagnostics)


def test_export_emits_subframe_rational_seconds(tmp_path):
    """带子帧偏移的 clip：offset 合成精确有理秒（帧对齐 clip 输出不变）。"""
    sub_clip = ClipInstance(
        clip_id="c1", asset_id="a1", track_id="v1",
        timeline_start=1, timeline_duration=60,
        source_start=0, source_duration=60,
        speed=SpeedParams(), transform=TransformParams(), crop=CropParams(),
        audio_sample_offset=800,
    )
    aligned = ClipInstance(
        clip_id="c2", asset_id="a1", track_id="v1",
        timeline_start=61, timeline_duration=60,
        source_start=60, source_duration=60,
        speed=SpeedParams(), transform=TransformParams(), crop=CropParams(),
    )
    v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                   clips=(sub_clip, aligned))
    seq = TimelineSequence(timeline_id="t1", name="Sub", fps=Rational(30000, 1001),
                           sample_rate=48000, video_tracks=(v1,))
    out = tmp_path / "sub_out.fcpxml"
    FCPXMLAdapter().export_from_canonical(seq, options={"output_path": str(out)})
    clips = ET.parse(out).getroot().findall(".//asset-clip")
    # 1 帧起点(1001/30000s) + 800 样本(500/30000s) = 1501/30000s
    assert clips[0].get("offset") == "1501/30000s"
    # 帧对齐 clip：61 帧 = 61061/30000s（旧路径逐字节不变）
    assert clips[1].get("offset") == "61061/30000s"


def test_roundtrip_quantized_position_stable(tmp_path):
    """导出→重导入：量化后位置定点稳定（800 样本偏移往返不变）。"""
    sub_clip = ClipInstance(
        clip_id="c1", asset_id="a1", track_id="v1",
        timeline_start=1, timeline_duration=60,
        source_start=0, source_duration=60,
        speed=SpeedParams(), transform=TransformParams(), crop=CropParams(),
        audio_sample_offset=800,
    )
    v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                   clips=(sub_clip,))
    seq = TimelineSequence(timeline_id="t1", name="RT", fps=Rational(30000, 1001),
                           sample_rate=48000, video_tracks=(v1,))
    out = tmp_path / "rt.fcpxml"
    FCPXMLAdapter().export_from_canonical(seq, options={"output_path": str(out)})
    back = FCPXMLAdapter().import_to_canonical(out)
    clip = back.sequences[0].video_tracks[0].clips[0]
    assert clip.timeline_start == 1
    assert clip.audio_sample_offset == 800


# ================================================================
# M24.3 · 活动调色版本字段随包（note 标记双向）
# ================================================================


def test_export_writes_active_grade_note(tmp_path):
    """clip 引用栈且有活动版本 → asset-clip@note topos:grade= 标记+条目。"""
    clip = ClipInstance(
        clip_id="c1", asset_id="a1", track_id="v1",
        timeline_start=0, timeline_duration=24,
        source_start=0, source_duration=24,
        speed=SpeedParams(), transform=TransformParams(), crop=CropParams(),
        color_stack_id="s1",
    )
    v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                   clips=(clip,))
    seq = TimelineSequence(timeline_id="t1", name="Grade", fps=Rational(24, 1),
                           video_tracks=(v1,))
    out = tmp_path / "grade.fcpxml"
    result = FCPXMLAdapter().export_from_canonical(
        seq, options={"output_path": str(out),
                      "color_versions": {"s1": {"active": "v2-暖调"}}})
    el = ET.parse(out).getroot().find(".//asset-clip")
    assert el.get("note") == "topos:grade=v2-暖调"
    assert any("活动调色版本" in e.description
               for e in result.report.entries)


def test_export_without_versions_option_no_note(tmp_path):
    """未供版本表（或栈无活动版本）→ 不写 note（输出零变化）。"""
    clip = ClipInstance(
        clip_id="c1", asset_id="a1", track_id="v1",
        timeline_start=0, timeline_duration=24,
        source_start=0, source_duration=24,
        speed=SpeedParams(), transform=TransformParams(), crop=CropParams(),
        color_stack_id="s1",
    )
    v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                   clips=(clip,))
    seq = TimelineSequence(timeline_id="t1", name="G2", fps=Rational(24, 1),
                           video_tracks=(v1,))
    out = tmp_path / "g2.fcpxml"
    FCPXMLAdapter().export_from_canonical(seq, options={"output_path": str(out)})
    assert ET.parse(out).getroot().find(".//asset-clip").get("note") is None


def test_import_note_marker_into_extra_fields(tmp_path):
    """导入 note 标记 → clip._extra_fields + PRESERVED 条目（对拍声明）。"""
    p = tmp_path / "imp_grade.fcpxml"
    p.write_text("""<?xml version="1.0"?><fcpxml version="1.9">
      <resources><format id="r1" frameDuration="1/24s" width="1920"
        height="1080"/>
        <asset id="r2" name="c" hasVideo="1" start="0s" duration="7200s"/>
      </resources>
      <library><event><project><sequence><spine>
        <asset-clip name="c" ref="r2" offset="0s" duration="24/24s"
          start="0s" note="topos:grade=v2-暖调"/>
      </spine></sequence></project></event></library></fcpxml>""", encoding="utf-8")
    result = FCPXMLAdapter().import_to_canonical(p)
    clip = result.sequences[0].video_tracks[0].clips[0]
    assert clip._extra_fields.get("topos_grade_version") == "v2-暖调"
    assert any("活动调色版本" in e.description
               for e in result.report.entries)


def test_grade_marker_roundtrip(tmp_path):
    """导出→重导入：活动版本标记稳定往返。"""
    clip = ClipInstance(
        clip_id="c1", asset_id="a1", track_id="v1",
        timeline_start=0, timeline_duration=24,
        source_start=0, source_duration=24,
        speed=SpeedParams(), transform=TransformParams(), crop=CropParams(),
        color_stack_id="s9",
    )
    v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                   clips=(clip,))
    seq = TimelineSequence(timeline_id="t1", name="RT", fps=Rational(25, 1),
                           video_tracks=(v1,))
    out = tmp_path / "rt_grade.fcpxml"
    FCPXMLAdapter().export_from_canonical(
        seq, options={"output_path": str(out),
                      "color_versions": {"s9": {"active": "final"}}})
    back = FCPXMLAdapter().import_to_canonical(out)
    rt = back.sequences[0].video_tracks[0].clips[0]
    assert rt._extra_fields.get("topos_grade_version") == "final"
