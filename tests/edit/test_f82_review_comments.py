"""F8.2 · 带版本锚点的审片评论与 marker——验收测试。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.review_comments import (
    ReviewComment,
    comment_to_marker,
    export_comments,
    import_comments,
    relocate_comment,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _timeline(clips_spec):
    clips = tuple(
        ClipInstance(cid, aid, "v1", start, dur, src_start, dur)
        for cid, aid, start, dur, src_start in clips_spec)
    return TimelineSequence(
        timeline_id="tl1", name="TL", fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=clips),))


class TestCommentModel:
    def test_unicode_body_and_status_validation(self):
        c = ReviewComment(body="画面过曝 ☀️ 需压暗", author="审片·客户A")
        assert "☀️" in c.body
        with pytest.raises(ValueError):
            ReviewComment(status="bogus")

    def test_roundtrip(self):
        c = ReviewComment(body="b", version_hash="abc", source_frame=42)
        assert ReviewComment.from_dict(c.to_dict()) == c


class TestImportExport:
    def test_roundtrip_unicode_idempotent_reimport(self, tmp_path):
        comments = [
            ReviewComment(comment_id="c1", body="第一条 中文 ☀️",
                          author="A"),
            ReviewComment(comment_id="c2", body="second", author="B"),
        ]
        path = str(tmp_path / "review.json")
        assert export_comments(comments, path)
        merged, added = import_comments(path)
        assert added == 2 and len(merged) == 2
        # 重复导入幂等
        merged2, added2 = import_comments(path, existing=merged)
        assert added2 == 0 and len(merged2) == 2
        assert merged2[0].body == "第一条 中文 ☀️"

    def test_privacy_strip_authors(self, tmp_path):
        path = str(tmp_path / "review.json")
        export_comments(
            [ReviewComment(comment_id="c1", author="Secret Name")], path,
            strip_authors=True)
        with open(path, encoding="utf-8") as fh:
            import json
            payload = json.load(fh)
        assert payload["comments"][0]["author"] == ""

    def test_unknown_schema_rejected(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text('{"schema": "other"}')
        with pytest.raises(ValueError, match="schema"):
            import_comments(str(path))


class TestCommentToMarker:
    def test_status_colors(self):
        open_m = comment_to_marker(
            ReviewComment(body="过曝", status="open", timeline_frame=10))
        assert open_m["color"] == "#ff4444" and open_m["frame"] == 10
        resolved_m = comment_to_marker(
            ReviewComment(body="ok", status="resolved"))
        assert resolved_m["color"] == "#44cc44"

    def test_empty_body_no_marker(self):
        assert comment_to_marker(ReviewComment(body="")) is None


class TestRelocation:
    def test_exact_match_by_source_anchor(self):
        """换 fps/重排后：素材+源帧双匹配 → exact 迁移（置信 1.0）。"""
        old = _timeline([("c1", "a1", 0, 10, 0)])
        new = _timeline([("c9", "a1", 50, 10, 100)])  # 同素材新位置/新窗
        comment = ReviewComment(
            body="这里", timeline_frame=3, asset_id="a1", source_frame=102)
        result = relocate_comment(comment, new)
        assert result.confidence == 1.0 and result.reason == "exact"
        # 迁移到新时间线帧（源帧 102 落在 [100,110) → frame 52）
        assert result.comment.timeline_frame == 52
        assert result.comment.clip_id == "c9"

    def test_deleted_asset_stale_not_silently_moved(self):
        """删除镜头 → 失效（绝不静默挪到别的镜头）。"""
        new = _timeline([("cX", "aOTHER", 0, 10, 0)])
        comment = ReviewComment(
            body="这里", timeline_frame=3, asset_id="a1", source_frame=5)
        result = relocate_comment(comment, new)
        assert result.confidence == 0.0
        assert result.reason.startswith("stale")
        assert result.comment.timeline_frame == 3  # 原帧不动

    def test_pure_timeline_comment_frame_fallback(self):
        """无源锚的纯时间线评论：帧在界内保留（0.5 近似声明）。"""
        new = _timeline([("c1", "aX", 0, 100, 0)])
        comment = ReviewComment(body="x", timeline_frame=50)  # 无 asset 锚
        result = relocate_comment(comment, new)
        assert result.confidence == 0.5
        assert result.reason == "timeline_only"

    def test_fps_change_same_asset_source_anchor_exact(self):
        """改 fps：同素材+源帧在窗 → exact（源锚跨 fps 不变）。"""
        new = _timeline([("c1", "a1", 0, 20, 0)])  # 重排后的窗口
        comment = ReviewComment(
            body="x", timeline_frame=99, asset_id="a1", source_frame=5)
        result = relocate_comment(comment, new)
        assert result.confidence == 1.0 and result.reason == "exact"
        assert result.comment.timeline_frame == 5

    def test_source_out_of_window_stale(self):
        """源帧出窗（修剪掉）→ 失效不挪。"""
        new = _timeline([("c1", "a1", 0, 10, 100)])  # 窗 [100,110)
        comment = ReviewComment(
            body="x", timeline_frame=3, asset_id="a1", source_frame=55)
        result = relocate_comment(comment, new)
        assert result.confidence == 0.0
        assert result.reason.startswith("stale")


# ================================================================
# M15.2（R10）· 重复同源 occurrence 歧义
# ================================================================

class TestRepeatedSourceAmbiguity:
    @staticmethod
    def _clip(clip_id, start, source_offset=0):
        from types import SimpleNamespace as NS
        return NS(asset_id="camera", clip_id=clip_id,
                  timeline_start=start, timeline_duration=10,
                  timeline_to_source_frame=lambda f, o=source_offset: f - start + o)

    def test_original_clip_preferred_over_first_occurrence(self):
        """同源窗评论：原 clip_id 命中的 occurrence 优先（exact）——
        不跳到时间线更靠前的同源复制片段。"""
        comment = ReviewComment(
            comment_id="r", asset_id="camera", source_frame=5,
            clip_id="original", timeline_id="t", timeline_frame=105,
            version_hash="v")
        from types import SimpleNamespace as NS
        timeline = NS(video_tracks=[NS(clips=[
            self._clip("first", 0, source_offset=0),
            self._clip("original", 100, source_offset=0)])])
        result = relocate_comment(comment, timeline)
        assert result.reason == "exact"
        assert result.comment.clip_id == "original"
        assert result.comment.timeline_frame == 105

    def test_original_clip_gone_ambiguous_not_silent_first(self):
        """原 clip_id 已不在候选（被删，但同源复制存在）→ ambiguous
        交用户裁决，不静默挪到首个同源片段。"""
        comment = ReviewComment(
            comment_id="r2", asset_id="camera", source_frame=5,
            clip_id="deleted", timeline_id="t", timeline_frame=5,
            version_hash="v")
        from types import SimpleNamespace as NS
        timeline = NS(video_tracks=[NS(clips=[
            self._clip("first", 0),
            self._clip("second", 100)])])
        result = relocate_comment(comment, timeline)
        assert result.reason.startswith("ambiguous:multiple_occurrences")
        assert "first" in result.reason and "second" in result.reason
        assert result.confidence == 0.3
        # 评论不挪动
        assert result.comment.clip_id == "deleted"

    def test_single_remaining_occurrence_relocates_exact(self):
        comment = ReviewComment(
            comment_id="r3", asset_id="camera", source_frame=5,
            clip_id="deleted", timeline_id="t", timeline_frame=5,
            version_hash="v")
        from types import SimpleNamespace as NS
        timeline = NS(video_tracks=[NS(clips=[
            self._clip("only", 50)])])
        result = relocate_comment(comment, timeline)
        assert result.reason == "exact"
        assert result.comment.clip_id == "only"
        assert result.comment.timeline_frame == 55
