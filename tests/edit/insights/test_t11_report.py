"""T11 · 镜头/对白/marker 总表与三格式一致导出测试。"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from tests.edit.insights import expected as exp
from tests.edit.insights import synthetic as syn

from src.features.edit.insights.capture import capture_readonly_state
from src.features.edit.insights.dto import (
    ClipEntry,
    CueEntry,
    FpsSpec,
    InsightSnapshot,
    ProjectEntry,
    SequenceEntry,
    TrackEntry,
)
from src.features.edit.insights.report import (
    REPORT_SCHEMA,
    build_report,
    render_html,
    report_to_dict,
    write_csv,
    write_html,
    write_json,
)


pytestmark = [pytest.mark.edit_core]


@pytest.fixture(scope="module")
def report():
    st = syn.build_synthetic_state()
    snapshot = capture_readonly_state(st.state, capture_id="cap-t11", captured_at=0.0)
    return build_report(snapshot)


def test_row_counts_and_unloaded_exclusion(report):
    # 手算：clip 行 = S1 4视频+1音频 + S2 1视频 = 6；对白 3；marker 2
    assert len(report.clips) == 6
    assert len(report.dialogues) == 3
    assert len(report.markers) == 2
    assert report.excluded_unloaded == (syn.SEQ_UNLOADED,)
    assert report.total_rows == 11
    # 未加载序列不出现任何行
    for row in report.clips + report.dialogues + report.markers:
        assert row.sequence_id != syn.SEQ_UNLOADED


def test_intervals_and_timecode_display(report):
    by_id = {row.clip_id: row for row in report.clips}
    a1 = by_id[syn.CLIP_A1]
    assert (a1.timeline_start, a1.timeline_end) == (0, 120)          # 半开
    assert (a1.source_start, a1.source_end) == (24, 144)             # 非零入点
    assert a1.tc_in == "00:00:00:00" and a1.tc_out == "00:00:05:00"  # 24fps 手算
    ntsc1 = by_id[syn.CLIP_NTSC1]
    assert ntsc1.tc_in == "00:00:00;00" and ntsc1.tc_out == exp.TC_S2_FRAME100  # DF
    # 复杂变速只展示记录区间 + 口径标记，不推导
    b2 = by_id[syn.CLIP_B2]
    assert b2.source_mapping == "unknown"
    assert (b2.source_start, b2.source_end) == (0, 60)


def test_filename_and_displayname_separate(report):
    by_id = {row.clip_id: row for row in report.clips}
    a1 = by_id[syn.CLIP_A1]
    assert a1.asset_display_name == "CamA_Take3.mov"
    assert a1.file_basename == "CamA_Take3.mov"
    # 源路径（含目录）绝不出现在任何行
    dump = json.dumps(report_to_dict(report), ensure_ascii=False)
    assert "/Volumes" not in dump


def test_multitrack_overlap_rows_independent(report):
    """A1 音频 [0,200) 与 V1 视频轨时间重叠：两行独立，时长不可相加。"""
    sfx = [r for r in report.clips if r.clip_id == syn.CLIP_SFX][0]
    v_first = [r for r in report.clips if r.clip_id == syn.CLIP_A1][0]
    assert sfx.track_type == "audio" and v_first.track_type == "video"
    assert sfx.timeline_start < v_first.timeline_end  # 时间重叠
    # 行相加 ≠ 序列时长（460）：禁止把行和当节目时长
    total = sum(r.timeline_end - r.timeline_start for r in report.clips
                if r.sequence_id == syn.SEQ_MAIN)
    assert total == 580  # 手算 120+80+120+60+200
    seq_dur = [s.duration_frames for s in report.sequences
               if s.sequence_id == syn.SEQ_MAIN][0]
    assert seq_dur == exp.S1_DURATION and total != seq_dur


def _read_csv_rows(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def test_three_formats_row_consistency(report, tmp_path):
    write_csv(report, str(tmp_path / "r.csv"))
    write_json(report, str(tmp_path / "r.json"))
    write_html(report, str(tmp_path / "r.html"))

    csv_rows = _read_csv_rows(str(tmp_path / "r.csv"))
    payload = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    html_text = (tmp_path / "r.html").read_text(encoding="utf-8")

    # 行数一致：CSV = JSON 各段之和 = 总行数
    assert len(csv_rows) == report.total_rows
    assert len(payload["clips"]) == len(report.clips)
    assert len(payload["dialogues"]) == len(report.dialogues)
    assert len(payload["markers"]) == len(report.markers)

    # ID/区间一致：CSV 的 (section, entity_id, frame_start, frame_end) 集合
    # == JSON 的同字段集合
    def key_csv(row):
        return (row["section"], row["entity_id"], row["frame_start"], row["frame_end"])

    def key_json(section, row):
        start = row.get("timeline_start", row.get("frame"))
        end = row.get("timeline_end", row.get("frame"))
        return (section, row.get("clip_id", row.get("cue_id", row.get("marker_id"))),
                str(start), str(end))

    csv_keys = {key_csv(r) for r in csv_rows}
    json_keys = set()
    for row in payload["clips"]:
        json_keys.add(key_json("clip", row))
    for row in payload["dialogues"]:
        json_keys.add(key_json("dialogue", row))
    for row in payload["markers"]:
        json_keys.add(key_json("marker", row))
    assert csv_keys == json_keys

    # HTML 静态：无脚本标签；每个 clip id 出现
    assert "<script" not in html_text.lower()
    assert syn.CLIP_A1 in html_text and syn.CUE_ZH in html_text


def test_csv_chinese_multiline_quotes_roundtrip(report, tmp_path):
    path = str(tmp_path / "r.csv")
    write_csv(report, path)
    rows = _read_csv_rows(path)
    quote_rows = [r for r in rows if r["entity_id"] == syn.CUE_QUOTE]
    assert len(quote_rows) == 1
    # 原文保真：引号/换行/中文逗号全部回读一致
    assert quote_rows[0]["text"] == syn.CUE_TEXT_QUOTE


def test_json_preserves_original_text(report, tmp_path):
    path = str(tmp_path / "r.json")
    write_json(report, path)
    payload = json.loads(path and (tmp_path / "r.json").read_text(encoding="utf-8"))
    assert payload["schema"] == REPORT_SCHEMA
    quote = [d for d in payload["dialogues"] if d["cue_id"] == syn.CUE_QUOTE][0]
    assert quote["text"] == syn.CUE_TEXT_QUOTE
    assert payload["excluded_unloaded"] == [syn.SEQ_UNLOADED]
    # fps 有理数与 DF 标志进入 JSON
    ntsc_meta = [s for s in payload["sequences"] if s["sequence_id"] == syn.SEQ_NTSC][0]
    assert ntsc_meta["fps"] == {"num": 30000, "den": 1001, "drop_frame": True}


def test_html_escapes_dangerous_text():
    """构造带 <script>/引号的行：HTML 必须转义，不得出现可执行内容。"""
    hostile_text = '<script>alert("x")</script>\n中文"引号"'
    hostile = InsightSnapshot(
        schema_version="1",
        capture_id="cap-x",
        captured_at=0.0,
        project=ProjectEntry(project_id="p", name="P", revision=0),
        sequences=(
            SequenceEntry(
                sequence_id="s1", name="S1", fps=FpsSpec(24, 1, False),
                width=1920, height=1080, sample_rate=48000, revision=0,
                load_state="loaded", duration_frames=10,
                tracks=(
                    TrackEntry(
                        track_id="t1", sequence_id="s1", name="SUB",
                        track_type="subtitle", index=0, is_enabled=True,
                        cues=(CueEntry(
                            clip_id="c1", sequence_id="s1", track_id="t1",
                            text=hostile_text, timeline_start=0, timeline_end=5,
                        ),),
                    ),
                ),
            ),
        ),
        assets=(),
    )
    out = render_html(build_report(hostile))
    assert "<script>alert" not in out          # 原文不可执行
    assert "&lt;script&gt;" in out             # 转义后可见
    assert hostile_text not in out


def test_scale_report_counts():
    from tests.edit.insights import scale_fixture

    sc = scale_fixture.build_scale_state(1_000)
    snapshot = capture_readonly_state(sc.state, capture_id="cap-t11-scale")
    rep = build_report(snapshot)
    # 手算：1000 clips + 10 cues + 10 markers = 1020 行
    assert len(rep.clips) == 1_000
    assert len(rep.dialogues) == 10
    assert len(rep.markers) == 10
    assert rep.total_rows == 1_020


def test_audio_cue_csv_reuses_interchange_cue_sheet(tmp_path):
    """T11 验收项：音频 cue 复用既有 CueSheet（不重写算法）。

    手算：S1 有音频轨 A1（SFX [0,200)）→ 1 个 CSV；S2 无音频轨 → 跳过；
    S3 未加载 → 跳过。
    """
    from src.features.edit.insights.audio_cues import export_audio_cue_csvs

    st = syn.build_synthetic_state()
    snapshot = capture_readonly_state(st.state, capture_id="cap-cue", captured_at=0.0)
    written = export_audio_cue_csvs(lambda: st.state, snapshot, tmp_path)
    assert len(written) == 1
    text = Path(written[0]).read_text(encoding="utf-8")
    assert "Topos Color Cue Sheet" in text          # 既有 CueSheet 输出格式
    assert "ambience" in text or "sfx" in text.lower()
    assert syn.CLIP_SFX in text                      # A1 音频 clip 条目

    # 宿主序列缺失（捕获后卸载）→ 跳过不伪造
    class EmptyState:
        project = st.project
        sequences = {}

    assert export_audio_cue_csvs(lambda: EmptyState(), snapshot, tmp_path) == []
