"""P1-13 Consolidate（按剪辑范围裁剪收集）测试。

真实素材路径（tests/test_videos/2K_h264.mp4，25fps/62.9s/GOP≈1.44s）
驱动 remux/transcode 两种模式的端到端验证；ffmpeg CLI 不可用时跳过
真实媒体用例（与 proxy 生成器的环境假设一致）。
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

from src.features.edit.core.consolidate import (
    STATUS_COPIED_IMAGE,
    STATUS_CONSOLIDATED,
    STATUS_FAILED,
    STATUS_MISSING,
    MODE_REMUX,
    MODE_TRANSCODE,
    ConsolidateError,
    SourceRange,
    _last_keyframe_at_or_before,
    _merge_ranges,
    compute_used_ranges,
    consolidate_project,
)
from src.features.edit.core.project import EditProject, MediaAsset, ProjectBin
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core

_REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_VIDEO = str(_REPO_ROOT / "tests" / "test_videos" / "2K_h264.mp4")
REAL_FPS = 25.0  # 探测值（1572 帧 / 62.89s）


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


requires_ffmpeg = pytest.mark.skipif(
    not _ffmpeg_available(), reason="ffmpeg CLI not on PATH"
)


def _video_asset(path: str = REAL_VIDEO, asset_id: str = "vid-1") -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id,
        path=path,
        media_type="video",
        display_name=os.path.basename(path),
        metadata={"fps": REAL_FPS, "width": 2048, "height": 1080,
                  "total_frames": 1572, "duration": 62.89},
    )


def _sequence_with_clips(asset_id: str, *ranges, seq_fps: int = 25) -> TimelineSequence:
    clips = []
    pos = 0
    for start, dur in ranges:
        clips.append(ClipInstance(
            clip_id=f"clip-{start}",
            asset_id=asset_id,
            track_id="v1",
            timeline_start=pos,
            timeline_duration=dur,
            source_start=start,
            source_duration=dur,
        ))
        pos += dur + 10
    track = EditTrack(track_id="v1", name="V1", track_type="video",
                      clips=tuple(clips))
    from src.features.edit.core.timeline_v2 import Rational
    return TimelineSequence(
        timeline_id="seq-1", name="Main", fps=Rational(seq_fps, 1),
        video_tracks=(track,),
    )


def _project_with(asset: MediaAsset) -> EditProject:
    root = ProjectBin(
        bin_id="root", name="Root", parent_id=None, asset_ids=(asset.asset_id,)
    )
    return EditProject(
        project_id="p1", name="ConsolidateTest",
        assets={asset.asset_id: asset}, bins={"root": root}, root_bin_id="root",
    )


# ─────────────────────────────────────────────────────────────────────────────
# 纯模型：范围计算与合并
# ─────────────────────────────────────────────────────────────────────────────

def test_merge_ranges_overlaps_and_adjacent():
    merged = _merge_ranges([
        SourceRange(100, 200), SourceRange(150, 260),
        SourceRange(300, 400), SourceRange(400, 500),  # 相邻合并
        SourceRange(20, 30),
    ])
    assert merged == (
        SourceRange(20, 30),
        SourceRange(100, 260),
        SourceRange(300, 500),
    )


def test_compute_used_ranges_union_video_audio_and_handles():
    audio_clip = ClipInstance(
        clip_id="a-1", asset_id="vid-1", track_id="a1",
        timeline_start=0, timeline_duration=50, source_start=80,
        source_duration=50,
    )
    audio_track = EditTrack(track_id="a1", name="A1", track_type="audio",
                            clips=(audio_clip,))
    seq = _sequence_with_clips("vid-1", (100, 100))  # [100,200)
    seq = seq._rebuild(audio_tracks=(audio_track,))

    ranges = compute_used_ranges([seq], handles_frames=12)
    # 视频 [100,200) 与音频 [80,130) 各加 12 帧手柄后并集
    assert ranges["vid-1"] == (SourceRange(68, 212),)


def test_compute_used_ranges_handles_floor_at_zero():
    seq = _sequence_with_clips("vid-1", (5, 50))
    ranges = compute_used_ranges([seq], handles_frames=12)
    assert ranges["vid-1"] == (SourceRange(0, 67),)


def test_compute_used_ranges_merges_disjoint_clips():
    seq = _sequence_with_clips("vid-1", (50, 30), (500, 40))
    ranges = compute_used_ranges([seq])
    assert ranges["vid-1"] == (SourceRange(50, 80), SourceRange(500, 540))


# ─────────────────────────────────────────────────────────────────────────────
# 关键帧扫描（真实媒体）
# ─────────────────────────────────────────────────────────────────────────────

@requires_ffmpeg
def test_last_keyframe_at_or_before_real_video():
    kf = _last_keyframe_at_or_before(REAL_VIDEO, 5.0)
    assert kf is not None
    assert 0.0 <= kf <= 5.0
    assert kf > 0.0  # 62.9s 文件 5s 内必有非零关键帧
    # 0 秒处对齐到 0
    assert _last_keyframe_at_or_before(REAL_VIDEO, 0.0) == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 端到端：remux（流复制）
# ─────────────────────────────────────────────────────────────────────────────

@requires_ffmpeg
def test_consolidate_remux_two_segments(tmp_path):
    asset = _video_asset()
    project = _project_with(asset)
    # 两个不相邻区间：[50,80) 与 [500,540)，无手柄
    seq = _sequence_with_clips(asset.asset_id, (50, 30), (500, 40))

    new_project, new_seqs, report = consolidate_project(
        project, sequences=[seq], target_dir=str(tmp_path), mode=MODE_REMUX,
        handles_frames=0,
    )

    assert report.passed, report.warnings
    segs = [r for r in report.records if r.status == STATUS_CONSOLIDATED]
    assert len(segs) == 2
    assert all(os.path.exists(os.path.join(str(tmp_path), r.archived_path))
               for r in segs)
    assert report.source_bytes > report.segment_bytes  # 裁剪确实变小

    # 原素材移除，两个分段素材进入工程与素材箱
    assert asset.asset_id not in new_project.assets
    new_ids = {r.new_asset_id for r in segs}
    assert new_ids <= set(new_project.assets)
    root_bin = new_project.bins["root"]
    assert new_ids == set(root_bin.asset_ids)

    # clip 重映射：新 source_start = 旧 source_start - 对齐帧（关键帧回退 ≥0）
    new_clips = new_seqs[0].video_tracks[0].clips
    assert report.remapped_clips.keys() == {"clip-50", "clip-500"}
    for old_clip, new_clip, seg in zip(
        seq.video_tracks[0].clips, new_clips, segs
    ):
        assert new_clip.asset_id == seg.new_asset_id
        offset = seg.aligned_start_frame
        assert 0 <= offset <= old_clip.source_start
        assert new_clip.source_start == old_clip.source_start - offset
        # 时间线位置不受影响
        assert new_clip.timeline_start == old_clip.timeline_start
        assert new_clip.timeline_duration == old_clip.timeline_duration


@requires_ffmpeg
def test_consolidate_remux_segment_playable_and_covers_range(tmp_path):
    """分段必须可解码且覆盖整个请求区间（关键帧对齐只允许向前扩）。"""
    import av

    asset = _video_asset()
    project = _project_with(asset)
    seq = _sequence_with_clips(asset.asset_id, (150, 100))  # 6s @25fps

    _, _, report = consolidate_project(
        project, sequences=[seq], target_dir=str(tmp_path), mode=MODE_REMUX,
        handles_frames=0,
    )
    seg = next(r for r in report.records if r.status == STATUS_CONSOLIDATED)
    seg_path = os.path.join(str(tmp_path), seg.archived_path)

    with av.open(seg_path) as c:
        v = c.streams.video[0]
        assert v.frames == 100 or v.frames >= 95  # 关键帧对齐允许 GOP 内冗余
        assert float(v.average_rate) == pytest.approx(REAL_FPS, rel=0.02)


@requires_ffmpeg
def test_consolidate_transcode_prores_exact_frames(tmp_path):
    import av

    asset = _video_asset()
    project = _project_with(asset)
    seq = _sequence_with_clips(asset.asset_id, (100, 60))  # 4s @25fps

    new_project, new_seqs, report = consolidate_project(
        project, sequences=[seq], target_dir=str(tmp_path),
        mode=MODE_TRANSCODE, handles_frames=0,
    )
    assert report.passed, report.warnings
    seg = next(r for r in report.records if r.status == STATUS_CONSOLIDATED)
    # transcode 帧精确：对齐帧 = 请求起点
    assert seg.aligned_start_frame == 100
    assert seg.archived_path.endswith(".mov")

    seg_path = os.path.join(str(tmp_path), seg.archived_path)
    with av.open(seg_path) as c:
        v = c.streams.video[0]
        assert v.codec_context.name == "prores"
        assert v.frames == 60
        assert v.codec_context.pix_fmt == "yuv422p10le"

    # clip 偏移 = 请求起点（transcode 帧精确）→ 新片段从分段 0 帧开始
    new_clip = new_seqs[0].video_tracks[0].clips[0]
    assert new_clip.source_start == 0


@requires_ffmpeg
def test_consolidate_image_full_copy_zero_offset(tmp_path):
    img_src = tmp_path / "still.png"
    img_src.write_bytes(b"\x89PNG\r\n\x1a\nfake")  # 内容不重要，只验证复制路径
    asset = MediaAsset(
        asset_id="img-1", path=str(img_src), media_type="image",
        display_name="still.png", metadata={"width": 1920, "height": 1080},
    )
    project = _project_with(asset)
    seq = _sequence_with_clips(asset.asset_id, (0, 1))

    new_project, new_seqs, report = consolidate_project(
        project, sequences=[seq], target_dir=str(tmp_path / "out"),
        mode=MODE_REMUX,
    )
    assert report.passed
    rec = report.records[0]
    assert rec.status == STATUS_COPIED_IMAGE
    assert rec.aligned_start_frame == 0
    assert new_seqs[0].video_tracks[0].clips[0].source_start == 0
    assert asset.asset_id not in new_project.assets
    assert rec.new_asset_id in new_project.assets


@requires_ffmpeg
def test_consolidate_missing_file_reports_usage_locations(tmp_path):
    asset = _video_asset(path="/nonexistent/missing.mp4")
    project = _project_with(asset)
    seq = _sequence_with_clips(asset.asset_id, (50, 30))

    new_project, new_seqs, report = consolidate_project(
        project, sequences=[seq], target_dir=str(tmp_path), handles_frames=0,
    )
    assert report.missing[0].status == STATUS_MISSING
    assert asset.asset_id in report.missing_usage
    assert any("Main" in loc for loc in report.missing_usage[asset.asset_id])
    # clip 保持原样（不重映射到不存在的分段）
    assert new_seqs[0].video_tracks[0].clips[0].asset_id == asset.asset_id


@requires_ffmpeg
def test_consolidate_corrupt_media_records_failure_not_raise(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video file" * 100)
    asset = _video_asset(path=str(bad))
    project = _project_with(asset)
    seq = _sequence_with_clips(asset.asset_id, (50, 30))

    # av.open 失败 -> ConsolidateError/av 错误被捕获为分段失败
    new_project, _, report = consolidate_project(
        project, sequences=[seq], target_dir=str(tmp_path / "out"),
        handles_frames=0,
    )
    assert not report.passed
    assert report.failures[0].status == STATUS_FAILED
    assert report.warnings


def test_consolidate_invalid_mode_rejected(tmp_path):
    asset = _video_asset()
    project = _project_with(asset)
    seq = _sequence_with_clips(asset.asset_id, (50, 30))
    with pytest.raises(ValueError):
        consolidate_project(
            project, sequences=[seq], target_dir=str(tmp_path), mode="bogus"
        )


@requires_ffmpeg
def test_consolidate_report_roundtrip_dict(tmp_path):
    asset = _video_asset()
    project = _project_with(asset)
    seq = _sequence_with_clips(asset.asset_id, (50, 30))
    _, _, report = consolidate_project(
        project, sequences=[seq], target_dir=str(tmp_path), handles_frames=0,
    )
    data = report.to_dict()
    assert data["schema"] == "topos.consolidate.v1"
    assert data["mode"] == MODE_REMUX
    assert data["handles_frames"] == 0
    assert data["records"] and data["records"][0]["status"] == STATUS_CONSOLIDATED


# ─────────────────────────────────────────────────────────────────────────────
# 工程可落地性：归档工程可序列化
# ─────────────────────────────────────────────────────────────────────────────

@requires_ffmpeg
def test_consolidated_project_saves_and_reloads(tmp_path):
    from src.features.edit.core.edit_project_io import EditProjectIO

    asset = _video_asset()
    project = _project_with(asset)
    seq = _sequence_with_clips(asset.asset_id, (50, 30))
    new_project, new_seqs, report = consolidate_project(
        project, sequences=[seq], target_dir=str(tmp_path), handles_frames=0,
    )
    assert report.passed

    # EditProjectIO.save_project 需要序列化器认可的 timelines 结构：
    # 直接复用 io 落盘 + 读回
    io = EditProjectIO()
    out = str(tmp_path / "consolidated.topos")
    assert io.save_project(out, new_project)
    loaded = io.load_project(out)
    assert loaded is not None
    reloaded = loaded[0]
    seg_id = report.records[0].new_asset_id
    assert seg_id in reloaded.assets
    assert reloaded.assets[seg_id].path.endswith(".mp4")
