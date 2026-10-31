"""C5: 章节标记测试。

覆盖：
- TimelineMarker.kind 模型（默认 standard、校验、with_kind、序列化容错）
- 命令透传（AddMarkerCommand / EditMarkerCommand new_kind、undo、序列化往返）
- 章节导出纯函数（YouTube 时间码进位 / 0:00 自动补行 / FFMetadata 结构）
- add_chapter_marker_at_playhead 交互（时间线级、kind=chapter、可撤销）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.chapter_export import (  # noqa: E402
    chapter_markers,
    format_ffmetadata,
    format_timestamp_hms,
    format_youtube_chapters,
)
from src.features.edit.core.commands import (  # noqa: E402
    AddMarkerCommand,
    EditMarkerCommand,
)
from src.features.edit.core.timeline_v2 import (  # noqa: E402
    Rational,
    TimelineMarker,
    TimelineSequence,
)

try:
    from PyQt6.QtWidgets import QApplication  # noqa: F401

    HAS_QT = True
except Exception:  # noqa: BLE001
    HAS_QT = False

pytestmark = pytest.mark.skipif(not HAS_QT, reason="PyQt6 unavailable")

FPS = 24.0


def _marker(frame: int, kind: str = "standard", name: str = "") -> TimelineMarker:
    return TimelineMarker(frame=frame, name=name, kind=kind)


def _sequence(*markers: TimelineMarker, duration: int = 0) -> TimelineSequence:
    return TimelineSequence(markers=tuple(markers), fps=Rational(24, 1))


# ----------------------------------------------------------------------
# 模型
# ----------------------------------------------------------------------

class TestMarkerKindModel:
    def test_default_kind_is_standard(self) -> None:
        assert _marker(0).kind == "standard"

    def test_invalid_kind_rejected(self) -> None:
        with pytest.raises(ValueError, match="kind"):
            TimelineMarker(frame=0, kind="bogus")

    def test_with_kind_returns_new_instance(self) -> None:
        m = _marker(12)
        chapter = m.with_kind("chapter")
        assert chapter is not m
        assert chapter.kind == "chapter"
        assert chapter.frame == m.frame == 12
        assert m.kind == "standard"  # 原 marker 不变

    def test_serialization_roundtrip(self) -> None:
        m = _marker(48, kind="chapter", name="Intro")
        restored = TimelineMarker.from_dict(m.to_dict())
        assert restored.kind == "chapter"
        assert restored.name == "Intro"

    def test_old_dict_without_kind_loads_as_standard(self) -> None:
        data = {"frame": 10}  # 旧工程：无 kind 字段
        marker = TimelineMarker.from_dict(data)
        assert marker.kind == "standard"


# ----------------------------------------------------------------------
# 命令
# ----------------------------------------------------------------------

class TestMarkerKindCommands:
    def test_add_chapter_command_and_undo(self) -> None:
        seq = _sequence()
        cmd = AddMarkerCommand.create(_marker(24, kind="chapter"))
        new_seq = cmd.apply(seq)
        assert len(new_seq.markers) == 1
        assert new_seq.markers[0].kind == "chapter"
        undone = cmd.undo(new_seq)
        assert undone.markers == ()

    def test_edit_kind_via_command(self) -> None:
        seq = _sequence(_marker(10, name="A"))
        cmd = EditMarkerCommand.create(
            seq.markers[0].marker_id, new_kind="chapter")
        new_seq = cmd.apply(seq)
        assert new_seq.markers[0].kind == "chapter"
        undone = cmd.undo(new_seq)
        assert undone.markers[0].kind == "standard"

    def test_edit_kind_command_serialization_roundtrip(self) -> None:
        cmd = EditMarkerCommand.create("mid", new_kind="chapter")
        restored = EditMarkerCommand.from_dict(cmd.to_dict())
        assert restored.new_kind == "chapter"


# ----------------------------------------------------------------------
# 导出纯函数
# ----------------------------------------------------------------------

class TestChapterExport:
    def test_chapter_markers_filters_and_sorts(self) -> None:
        markers = [
            _marker(96, kind="chapter", name="B"),
            _marker(10),  # standard → 排除
            _marker(24, kind="chapter", name="A"),
        ]
        chapters = chapter_markers(markers)
        assert [m.name for m in chapters] == ["A", "B"]

    def test_timestamp_formats(self) -> None:
        assert format_timestamp_hms(0, FPS) == "0:00:00"
        assert format_timestamp_hms(int(FPS * 65), FPS) == "0:01:05"
        # ≥1h 进位
        assert format_timestamp_hms(int(FPS * 3661), FPS) == "1:01:01"

    def test_youtube_format_with_auto_first(self) -> None:
        markers = [
            _marker(int(FPS * 30), kind="chapter", name="Opening"),
            _marker(int(FPS * 90), kind="chapter", name="Talk"),
        ]
        text = format_youtube_chapters(markers, FPS)
        lines = text.strip().splitlines()
        assert lines[0] == "0:00 Chapters"  # 自动补 0:00
        assert lines[1] == "0:00:30 Opening"
        assert lines[2] == "0:01:30 Talk"

    def test_youtube_format_without_auto_first(self) -> None:
        markers = [_marker(0, kind="chapter", name="Start")]
        text = format_youtube_chapters(markers, FPS, auto_first=False)
        assert text.strip() == "0:00:00 Start"

    def test_youtube_empty_returns_empty(self) -> None:
        assert format_youtube_chapters([], FPS) == ""
        assert format_youtube_chapters([_marker(5)], FPS) == ""

    def test_ffmetadata_structure(self) -> None:
        markers = [
            _marker(0, kind="chapter", name="Start"),
            _marker(48, kind="chapter", name="Next"),
        ]
        text = format_ffmetadata(markers, FPS, duration_frames=240)
        assert text.startswith(";FFMETADATA1")
        assert "[CHAPTER]" in text
        # TIMEBASE=1/1000 + 毫秒 START/END（NTSC 帧率无取整漂移）
        assert "TIMEBASE=1/1000" in text
        assert "START=0" in text
        assert "END=2000" in text  # 48 帧 @24fps = 2000ms
        assert "title=Next" in text
        # 最后一章 END = duration_frames（240 帧 = 10000ms）
        assert "START=2000" in text
        assert "END=10000" in text

    def test_ffmetadata_escapes_reserved_characters(self) -> None:
        markers = [_marker(0, kind="chapter", name="Part 1 = Intro; #2")]
        text = format_ffmetadata(markers, FPS)
        assert "title=Part 1 \\= Intro\\; \\#2" in text

    def test_youtube_no_duplicate_zero_timestamp(self) -> None:
        # 首章落在帧 1..fps-1：时间码同为 0:00，不应再补第二条 0:00
        markers = [_marker(12, kind="chapter", name="First")]
        text = format_youtube_chapters(markers, FPS)
        assert text.count("0:00") == 1
        assert text.strip().endswith("0:00 First")

    def test_ffmetadata_empty_returns_empty(self) -> None:
        assert format_ffmetadata([], FPS) == ""


# ----------------------------------------------------------------------
# 交互（TimelineView）
# ----------------------------------------------------------------------

class TestAddChapterInteraction:
    def _view_with_timeline(self):
        from src.features.edit.ui.timeline_view import TimelineView

        view = TimelineView()
        view.load_timeline(TimelineSequence(fps=Rational(24, 1)))
        view._move_playhead_to(48)
        return view

    def test_add_chapter_creates_timeline_scoped_chapter(self, qapp) -> None:
        view = self._view_with_timeline()
        view.add_chapter_marker_at_playhead()
        markers = view._timeline.markers
        assert len(markers) == 1
        assert markers[0].kind == "chapter"
        assert markers[0].frame == 48
        assert markers[0].clip_id is None

    def test_add_chapter_is_undoable(self, qapp) -> None:
        view = self._view_with_timeline()
        view.add_chapter_marker_at_playhead()
        assert len(view._timeline.markers) == 1
        undone = view._command_stack.undo(view._timeline)
        assert undone.markers == ()
