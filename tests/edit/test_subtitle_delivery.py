"""P1-21 序列级字幕交付（多语言轨批量导出 + manifest）测试。"""

from __future__ import annotations

import json
import os

import pytest

from src.features.edit.core.captions.delivery import (
    STATUS_SKIPPED_EMPTY,
    STATUS_SKIPPED_ROLE,
    STATUS_WRITTEN,
    delivery_file_name,
    export_subtitle_tracks,
    list_subtitle_tracks,
)
from src.features.edit.core.captions.model import CaptionTrackMetadata
from src.features.edit.core.timeline_v2 import (
    EditTrack,
    Rational,
    SubtitleClip,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _sub_track(
    track_id: str,
    cues_spec,
    language: str = "",
    role: str = "primary",
    label: str = "",
    name: str = "",
) -> EditTrack:
    clips = tuple(
        SubtitleClip(
            clip_id=f"{track_id}-c{i}", track_id=track_id,
            timeline_start=start, timeline_duration=dur, text=text,
        )
        for i, (start, dur, text) in enumerate(cues_spec)
    )
    return EditTrack(
        track_id=track_id, name=name, track_type="subtitle",
        subtitle_clips=clips,
        caption_metadata=CaptionTrackMetadata(
            language=language, role=role, label=label,
        ),
    )


def _sequence(*tracks: EditTrack, name: str = "Movie") -> TimelineSequence:
    return TimelineSequence(
        timeline_id="seq-1", name=name, fps=Rational(24, 1),
        subtitle_tracks=tracks,
    )


class TestListAndNaming:
    def test_summaries_default_language_role_and_label(self):
        track = _sub_track("s1", [(0, 24, "hi")])
        (summary,) = list_subtitle_tracks(_sequence(track))
        assert summary.language == "und"
        assert summary.role == "primary"
        assert summary.label == "s1"  # label→name→track_id 回退链
        assert summary.cue_count == 1

    def test_summaries_use_metadata(self):
        track = _sub_track(
            "s1", [(0, 24, "hi")], language="zh-Hans", role="secondary",
            label="简体中文", name="旧名",
        )
        (summary,) = list_subtitle_tracks(_sequence(track))
        assert summary.language == "zh-Hans"
        assert summary.role == "secondary"
        assert summary.label == "简体中文"

    def test_file_name_language_convention(self):
        from src.features.edit.core.captions.delivery import SubtitleTrackSummary

        zh = SubtitleTrackSummary(
            track_id="s1", label="简体", language="zh-Hans",
            role="primary", cue_count=3,
        )
        und = SubtitleTrackSummary(
            track_id="s2", label="注释", language="und",
            role="secondary", cue_count=1,
        )
        assert delivery_file_name(zh, "My Film", "srt") == "My_Film.zh-Hans.srt"
        assert delivery_file_name(und, "My Film", "vtt") == "My_Film_注释.vtt"


class TestExportSubtitleTracks:
    def test_multi_track_export_with_manifest(self, tmp_path):
        zh = _sub_track("s1", [(0, 24, "你好"), (48, 24, "世界")],
                        language="zh-Hans")
        en = _sub_track("s2", [(0, 24, "hello")], language="en",
                        role="secondary")
        empty = _sub_track("s3", [], language="fr", role="forced")

        report = export_subtitle_tracks(
            _sequence(zh, en, empty), output_dir=tmp_path
        )
        assert report.passed
        assert [f.file_name for f in report.written] == [
            "Movie.zh-Hans.srt", "Movie.en.srt",
        ]
        # 空轨道跳过且不落盘
        assert [f.status for f in report.files] == [
            STATUS_WRITTEN, STATUS_WRITTEN, STATUS_SKIPPED_EMPTY,
        ]
        assert not (tmp_path / "Movie.fr.srt").exists()

        # SRT 内容完整（cue 计数 + 顺序）
        zh_text = (tmp_path / "Movie.zh-Hans.srt").read_text(encoding="utf-8")
        assert "1\n00:00:00,000 --> 00:00:01,000\n你好" in zh_text
        assert "2\n00:00:02,000 --> 00:00:03,000\n世界" in zh_text

        # manifest 机器可读
        manifest = json.loads(
            (tmp_path / "subtitle_manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["schema"] == "topos.subtitle_delivery.v1"
        assert manifest["sequence_name"] == "Movie"
        written = [f for f in manifest["files"] if f["status"] == "written"]
        assert {f["language"] for f in written} == {"zh-Hans", "en"}
        assert report.manifest_path.endswith("subtitle_manifest.json")

    def test_role_filter_skips_secondary(self, tmp_path):
        zh = _sub_track("s1", [(0, 24, "你好")], language="zh-Hans")
        commentary = _sub_track(
            "s2", [(0, 24, "note")], language="en", role="secondary",
        )
        report = export_subtitle_tracks(
            _sequence(zh, commentary), output_dir=tmp_path,
            include_roles=("primary", "forced"),
        )
        assert [f.status for f in report.files] == [
            STATUS_WRITTEN, STATUS_SKIPPED_ROLE,
        ]
        assert len(report.written) == 1
        assert not (tmp_path / "Movie.en.srt").exists()

    def test_same_language_tracks_get_dedupe_suffix(self, tmp_path):
        a = _sub_track("s1", [(0, 24, "one")], language="en", label="EN-1")
        b = _sub_track("s2", [(48, 24, "two")], language="en", label="EN-2")
        report = export_subtitle_tracks(
            _sequence(a, b), output_dir=tmp_path
        )
        assert [f.file_name for f in report.written] == [
            "Movie.en.srt", "Movie.en_2.srt",
        ]

    def test_ttml_batch_carries_xml_lang_per_file(self, tmp_path):
        zh = _sub_track("s1", [(0, 24, "你好")], language="zh-Hans")
        ar = _sub_track("s2", [(0, 24, "مرحبا")], language="ar",
                        role="secondary")
        report = export_subtitle_tracks(
            _sequence(zh, ar), output_dir=tmp_path, format_name="ttml",
        )
        assert report.format_name == "ttml"
        assert (tmp_path / "Movie.zh-Hans.ttml").read_text(
            encoding="utf-8"
        ).count('xml:lang="zh-Hans"') == 1
        assert (tmp_path / "Movie.ar.ttml").read_text(
            encoding="utf-8"
        ).count('xml:lang="ar"') == 1

    def test_track_ids_subset(self, tmp_path):
        a = _sub_track("s1", [(0, 24, "one")], language="ja")
        b = _sub_track("s2", [(0, 24, "two")], language="ko")
        report = export_subtitle_tracks(
            _sequence(a, b), output_dir=tmp_path, track_ids=["s2"],
        )
        assert [f.file_name for f in report.files] == ["Movie.ko.srt"]

    def test_invalid_format_and_no_tracks_raise(self, tmp_path):
        track = _sub_track("s1", [(0, 24, "x")])
        with pytest.raises(ValueError):
            export_subtitle_tracks(
                _sequence(track), output_dir=tmp_path, format_name="docx"
            )
        with pytest.raises(ValueError):
            export_subtitle_tracks(
                TimelineSequence(timeline_id="e", name="Empty",
                                 fps=Rational(24, 1)),
                output_dir=tmp_path,
            )

    def test_include_empty_writes_file(self, tmp_path):
        empty = _sub_track("s1", [], language="de")
        report = export_subtitle_tracks(
            _sequence(empty), output_dir=tmp_path, include_empty=True
        )
        assert len(report.written) == 1
        assert (tmp_path / "Movie.de.srt").read_text(encoding="utf-8") == ""


class TestDeliveryDialog:
    def test_defaults_and_selection(self, qapp):
        from src.features.edit.core.captions.delivery import SubtitleTrackSummary
        from src.features.edit.ui.subtitle_delivery_dialog import (
            DELIVERY_FORMATS,
            SubtitleDeliveryDialog,
        )

        tracks = [
            SubtitleTrackSummary("s1", "简体", "zh-Hans", "primary", 3),
            SubtitleTrackSummary("s2", "注释", "en", "secondary", 1),
            SubtitleTrackSummary("s3", "空轨", "fr", "forced", 0),
        ]
        dlg = SubtitleDeliveryDialog(tracks)
        # 非空轨道默认勾选，空轨道默认不勾
        assert dlg.values() == ("srt", ("s1", "s2"))
        # 切换格式
        dlg._format_combo.setCurrentIndex(DELIVERY_FORMATS.index("ttml"))
        assert dlg.values()[0] == "ttml"
        # 取消勾选 s1 → 只剩 s2
        dlg._track_boxes[0][1].setChecked(False)
        assert dlg.values() == ("ttml", ("s2",))
