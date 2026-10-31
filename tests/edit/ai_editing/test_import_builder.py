"""P1-16 — ImportResult → EditProject 落地构建器测试。

验收：
- 在线素材（文件存在）经 import_asset 落池并去重；
- 缺失媒体生成离线素材（is_offline + external_ref 元数据），按路径去重；
- clip 的外部引用 id（FCPXML asset id / OTIO target_url）重映射为工程内
  真实 asset_id，placement 不变；无法解析的 clip 保留原值并被报告；
- TimelineSequenceRef upsert（fps Rational → float），新时间线设为 active；
- OTIO 风格（无 asset 描述）从 clips 推导素材。
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.adapters.fcpxml_adapter import FCPXMLAdapter
from src.features.edit.adapters.import_builder import (
    build_project_from_import_result,
    collect_asset_rows,
    url_to_local_path,
)
from src.features.edit.adapters.interchange_types import ImportResult
from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.media.metadata import Rational

_REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_VIDEO = _REPO_ROOT / "tests" / "test_videos" / "2K_h264.mp4"
MISSING_VIDEO = "/nonexistent/path/missing_clip.mp4"


def _fcpxml_fixture() -> str:
    """在线（真实素材，file URL 带空格需 percent-encode）+ 缺失素材的 FCPXML。"""
    # W07 Windows 修复：str(Path) 在 Windows 带反斜杠，quote 后成
    # %5C 逃逸的伪 file URL（adapter 不识别→在线素材误判离线）。
    # Path.as_uri() 产生规范 file:///C:/... 形式（含空格正确编码）。
    real_url = REAL_VIDEO.as_uri()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.9">
  <resources>
    <format id="r1" name="1920x1080" frameDuration="1001/24000s" width="1920" height="1080"/>
    <asset id="r2" name="real clip" hasVideo="1" start="0s" duration="7200s">
      <media-rep kind="original" src="{real_url}"/>
    </asset>
    <asset id="r3" name="gone clip" hasVideo="1" start="0s" duration="3600s">
      <media-rep kind="original" src="file://{MISSING_VIDEO}"/>
    </asset>
  </resources>
  <library>
    <event name="E"><project name="P"><sequence id="seq1" name="Main">
      <spine>
        <asset-clip name="c0" ref="r2" offset="0s" duration="1200s" tcStart="0s"/>
        <asset-clip name="c1" ref="r3" offset="1200s" duration="600s" tcStart="240s"/>
      </spine>
    </sequence></project></event>
  </library>
</fcpxml>
"""


@pytest.fixture
def fcpxml_result(tmp_path):
    p = tmp_path / "build.fcpxml"
    p.write_text(_fcpxml_fixture(), encoding="utf-8")
    return FCPXMLAdapter().import_to_canonical(p)


# ================================================================
# url_to_local_path
# ================================================================


def test_url_to_local_path_variants():
    assert url_to_local_path("") == ""
    assert url_to_local_path("/plain/path.mov") == "/plain/path.mov"
    # 空格 percent-encoding 解码（W07：返回原生分隔符路径）
    assert url_to_local_path("file:///a%20b/c.mov") == os.path.normpath(
        "/a b/c.mov")
    # Windows 盘符
    assert url_to_local_path("file:///C:/media/x.mp4") == os.path.normpath(
        "C:/media/x.mp4")  # W07 原生分隔符


# ================================================================
# FCPXML → 工程落地
# ================================================================


def test_build_online_and_offline_assets(fcpxml_result):
    project = EditProject.create(name="Interchange Host")
    new_project, sequences, summary = build_project_from_import_result(
        project, fcpxml_result, probe_metadata=lambda p: {"probed": True}
    )

    assert summary.asset_count == 2
    assert len(summary.imported_asset_ids) == 1
    assert len(summary.offline_assets) == 1

    # 在线素材：真实文件存在，带探测元数据，加入 root bin
    online = new_project.get_asset_by_path(str(REAL_VIDEO))
    assert online is not None
    assert online.metadata.get("probed") is True
    assert not online.is_offline
    root_bin = new_project.bins[new_project.root_bin_id]
    assert online.asset_id in root_bin.asset_ids

    # 离线素材：is_offline + external_ref 供后续 relink
    offline_id, offline_path = summary.offline_assets[0]
    offline = new_project.assets[offline_id]
    assert offline.path == os.path.normpath(MISSING_VIDEO)  # W07 原生分隔符
    assert offline.is_offline is True
    assert offline.metadata.get("source_kind") == "interchange"
    assert offline.metadata.get("external_ref") == "r3"
    assert offline.asset_id in root_bin.asset_ids


def test_clip_asset_ids_remapped(fcpxml_result):
    project = EditProject.create(name="Interchange Host")
    new_project, sequences, summary = build_project_from_import_result(
        project, fcpxml_result
    )
    assert summary.sequence_ids and not summary.unmapped_clip_ids

    seq = sequences[summary.sequence_ids[0]]
    clips = seq.video_tracks[0].clips
    assert len(clips) == 2
    # 引用已重映射到工程内真实 asset_id
    assert clips[0].asset_id in new_project.assets
    assert clips[1].asset_id in new_project.assets
    assert clips[0].asset_id == new_project.get_asset_by_path(str(REAL_VIDEO)).asset_id
    # placement 保持 adapter 输出不变
    adapter_clips = fcpxml_result.sequences[0].video_tracks[0].clips
    assert clips[0].timeline_start == adapter_clips[0].timeline_start
    assert clips[0].timeline_duration == adapter_clips[0].timeline_duration
    assert clips[1].source_start == adapter_clips[1].source_start


def test_timeline_ref_upserted_and_active(fcpxml_result):
    project = EditProject.create(name="Interchange Host")
    new_project, sequences, summary = build_project_from_import_result(
        project, fcpxml_result
    )
    tid = summary.sequence_ids[0]
    ref = new_project.timelines.get(tid)
    assert ref is not None
    # Rational 24000/1001 → float
    assert ref.fps == pytest.approx(23.976023976, abs=1e-4)
    assert new_project.active_timeline_id == tid


def test_reimport_reuses_assets_and_dedupes_timeline(fcpxml_result):
    project = EditProject.create(name="Interchange Host")
    p1, s1, sum1 = build_project_from_import_result(project, fcpxml_result)
    p2, s2, sum2 = build_project_from_import_result(p1, fcpxml_result)

    # 素材按路径去重：在线 + 离线都复用
    assert len(sum2.imported_asset_ids) == 0
    assert len(sum2.reused_asset_ids) == 2
    assert len(sum2.offline_assets) == 0
    assert len(p2.assets) == 2
    # timeline_id 冲突时加后缀，不覆盖已有时间线
    assert len(s2) == 1
    assert set(s2.keys()) != set(s1.keys())
    assert len(p2.timelines) == 2


# ================================================================
# OTIO 风格（无 asset 描述，clip.asset_id = target_url）
# ================================================================


def _otio_style_result() -> ImportResult:
    # W07 Windows 修复：str(Path) 在 Windows 带反斜杠，quote 后成
    # %5C 逃逸的伪 file URL（adapter 不识别→在线素材误判离线）。
    # Path.as_uri() 产生规范 file:///C:/... 形式（含空格正确编码）。
    real_url = REAL_VIDEO.as_uri()
    missing_url = f"file://{MISSING_VIDEO}"
    c0 = ClipInstance(
        clip_id="otio-0", asset_id=real_url, track_id="v1",
        timeline_start=0, timeline_duration=100, source_start=0, source_duration=100,
    )
    c1 = ClipInstance(
        clip_id="otio-1", asset_id=missing_url, track_id="v1",
        timeline_start=100, timeline_duration=50, source_start=10, source_duration=50,
    )
    v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=(c0, c1))
    seq = TimelineSequence(
        timeline_id="otio-seq", name="OTIO Import", fps=Rational(25, 1),
        width=1920, height=1080, sample_rate=48000, video_tracks=(v1,),
    )
    return ImportResult(sequences=(seq,), assets=())


def test_otio_style_derives_assets_from_clips():
    result = _otio_style_result()
    project = EditProject.create(name="OTIO Host")
    new_project, sequences, summary = build_project_from_import_result(
        project, result, probe_metadata=lambda p: {}
    )
    assert summary.asset_count == 2
    assert len(summary.imported_asset_ids) == 1  # 真实文件在线导入
    assert len(summary.offline_assets) == 1

    seq = sequences[summary.sequence_ids[0]]
    clips = seq.video_tracks[0].clips
    assert clips[0].asset_id == new_project.get_asset_by_path(str(REAL_VIDEO)).asset_id
    assert clips[1].asset_id == summary.offline_assets[0][0]
    assert new_project.timelines[summary.sequence_ids[0]].fps == pytest.approx(25.0)


# ================================================================
# 未映射 clip 与预览行
# ================================================================


def test_unmapped_clip_kept_and_reported():
    ghost = ClipInstance(
        clip_id="ghost-clip", asset_id="no-such-ref", track_id="v1",
        timeline_start=0, timeline_duration=30, source_start=0, source_duration=30,
    )
    v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=(ghost,))
    seq = TimelineSequence(
        timeline_id="g-seq", name="Ghost", fps=Rational(24, 1),
        video_tracks=(v1,),
    )
    # desc 清单存在但不含 ghost 引用 → clip 无法映射
    result = ImportResult(
        sequences=(seq,),
        assets=(
            {
                "asset_id": "imp-r9", "name": "other", "media_type": "video",
                "path": str(REAL_VIDEO),
            },
        ),
    )
    project = EditProject.create(name="Ghost Host")
    new_project, sequences, summary = build_project_from_import_result(
        project, result
    )
    # 未映射 clip 保留原 asset_id，不崩溃，并在摘要中报告
    kept = sequences["g-seq"].video_tracks[0].clips[0]
    assert kept.asset_id == "no-such-ref"
    assert summary.unmapped_clip_ids == ("ghost-clip",)


def test_collect_asset_rows_for_preview(fcpxml_result):
    rows = collect_asset_rows(fcpxml_result)
    assert len(rows) == 2
    by_exists = {exists for _, _, exists in rows}
    assert by_exists == {True, False}
    names = {name for name, _, _ in rows}
    assert "real clip" in names and "gone clip" in names


# ================================================================
# AUD-12614：单素材探测失败不中断整批导入
# ================================================================


def test_corrupt_asset_probe_failure_degrades_not_aborts(fcpxml_result):
    """probe 对某个损坏素材抛错时，该素材降级为空元数据、整批导入继续。

    历史实现 metadata=probe(local) 无防护——单个损坏文件使整个
    build_project_from_import_result 中断，其余素材与时间线全部丢失。
    """
    project = EditProject.create(name="Probe Fail Host")

    def _flaky_probe(path):
        if str(REAL_VIDEO) in str(path):
            raise RuntimeError("probe: corrupt file")
        return {"width": 1920, "height": 1080}

    new_project, sequences, summary = build_project_from_import_result(
        project, fcpxml_result, probe_metadata=_flaky_probe)

    # 在线素材仍被导入（元数据降级为空 dict），离线素材照旧
    assert len(summary.imported_asset_ids) + len(summary.reused_asset_ids) >= 1
    online = new_project.get_asset_by_path(str(REAL_VIDEO))
    assert online is not None
    assert online.metadata == {}
    # 时间线照常构建，clip 全部映射成功
    assert summary.sequence_ids and not summary.unmapped_clip_ids
    seq = sequences[summary.sequence_ids[0]]
    assert all(clip.asset_id for clip in seq.video_tracks[0].clips)
