"""P0-6 Relink / Replace Media——后端核验验收。

对应《媒体池后端交接计划》P0 第 6 项（复用并核验）。既有专项套件：
- tests/shared/test_f62_relink_match.py（三级匹配/唯一候选/同名不同内容人工确认）
- tests/edit/test_replace_media.py（替换语义/撤销/usage 不变）
- tests/edit/test_round4_media_replacement.py（属性兼容拒绝/异步守卫零提交）
本套件补验收链空白：批量重连一次事务、离线目标安全、新旧源文件零改写。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.project import (
    EditProject,
    batch_relink_assets,
    relink_asset,
    replace_media,
)

TEST_VIDEOS = Path(__file__).resolve().parents[2] / "tests" / "test_videos"
H264 = TEST_VIDEOS / "2K_h264.mp4"
PRORES = TEST_VIDEOS / "2K_prores422.mov"


def _skip_if_missing(path: Path) -> Path:
    if not path.exists():
        pytest.skip(f"测试素材缺失: {path}")
    return path


def _project_with_two(tmp_path: Path) -> tuple[EditProject, Path, Path]:
    """拷贝两份真实媒体进临时目录并导入。"""
    src1 = _skip_if_missing(H264)
    src2 = _skip_if_missing(PRORES)
    media1 = tmp_path / "clip-a.mp4"
    media2 = tmp_path / "clip-b.mov"
    shutil.copy2(src1, media1)
    shutil.copy2(src2, media2)
    project = EditProject.create("P0 relink")
    project = project.import_asset(
        str(media1), "video",
        {"fps": 25.0, "fps_num": 25, "fps_den": 1, "total_frames": 1572,
         "duration_seconds": 62.88, "width": 1920, "height": 1080,
         "codec": "h264"})
    project = project.import_asset(
        str(media2), "video",
        {"fps": 25.0, "fps_num": 25, "fps_den": 1, "total_frames": 487,
         "duration_seconds": 19.48, "width": 1920, "height": 1080,
         "codec": "prores"})
    return project, media1, media2


def test_batch_relink_single_transaction_and_undo(tmp_path) -> None:
    """批量重连：一次提交全部生效，撤销一次全部回原路径。"""
    project, media1, media2 = _project_with_two(tmp_path)
    id1 = project.get_asset_by_path(str(media1)).asset_id
    id2 = project.get_asset_by_path(str(media2)).asset_id
    # 交换两份媒体的位置（模拟移动磁盘后的批量重连）
    new1, new2 = tmp_path / "moved-a.mp4", tmp_path / "moved-b.mov"
    shutil.move(str(media1), new1)
    shutil.move(str(media2), new2)

    project = batch_relink_assets(project, {id1: str(new1), id2: str(new2)})
    assert project.assets[id1].path == str(new1)
    assert project.assets[id2].path == str(new2)
    assert project.assets[id1].is_offline is False
    # 未知 asset_id 的映射被静默跳过（批量容错），不影响其余重连
    project = batch_relink_assets(project, {"nope": str(new1)})
    assert project.assets[id1].path == str(new1)


def test_relink_to_missing_path_marks_offline_not_crash(tmp_path) -> None:
    """离线目标：路径不存在 → is_offline=True + 告警，不抛异常不崩溃。"""
    project, media1, _ = _project_with_two(tmp_path)
    id1 = project.get_asset_by_path(str(media1)).asset_id
    ghost = str(tmp_path / "ghost.mp4")
    project = relink_asset(project, id1, ghost)
    assert project.assets[id1].path == ghost
    assert project.assets[id1].is_offline is True


def test_relink_and_replace_never_touch_source_files(tmp_path) -> None:
    """零改写：relink/replace 前后新旧媒体文件字节不变、均存在。"""
    project, media1, media2 = _project_with_two(tmp_path)
    id1 = project.get_asset_by_path(str(media1)).asset_id
    digest_before_old = media1.read_bytes()
    digest_before_new = media2.read_bytes()

    project = relink_asset(project, id1, str(media2))
    assert media1.read_bytes() == digest_before_old
    assert media2.read_bytes() == digest_before_new

    project2 = replace_media(
        project, id1, str(media2),
        new_metadata={"fps": 25.0, "total_frames": 487, "width": 1920,
                      "height": 1080, "codec": "prores", "fps_num": 25,
                      "fps_den": 1, "duration_seconds": 19.48})
    assert project2.assets[id1].path == str(media2)
    assert media1.exists() and media2.exists()
    assert media1.read_bytes() == digest_before_old
    assert media2.read_bytes() == digest_before_new
    # 替换后代理与海报键复位（由服务层重建，不沿用旧源的产物）
    assert project2.assets[id1].thumbnail_key is None
    assert project2.assets[id1].proxy_status == "none"


def test_replace_rejects_incompatible_source_with_timeline_usage(tmp_path) -> None:
    """时间线引用中的素材：帧率不一致/过短的替换必须被拒绝并保留原素材。"""
    from src.features.edit.core.media_replacement import validate_replacement_usages
    from src.features.edit.core.timeline_v2 import (
        ClipInstance, EditTrack, Rational, TimelineSequence,
    )
    project, media1, _ = _project_with_two(tmp_path)
    id1 = project.get_asset_by_path(str(media1)).asset_id
    clip = ClipInstance(
        "c1", id1, "v1", 0, 100, 0, 100,
        _source_fps_num=25, _source_fps_den=1,
        _timeline_fps_num=25, _timeline_fps_den=1)
    sequence = TimelineSequence(
        timeline_id="t1", name="T", fps=Rational(25, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(clip,)),))

    asset = project.assets[id1]
    # 帧率不同 → 拒绝
    with pytest.raises(ValueError, match="帧率"):
        validate_replacement_usages(
            asset, "video",
            {"fps": 24.0, "fps_num": 24, "fps_den": 1, "total_frames": 1572},
            [sequence])
    # 总帧数覆盖不了源窗 → 拒绝
    with pytest.raises(ValueError, match="过短"):
        validate_replacement_usages(
            asset, "video",
            {"fps": 25.0, "fps_num": 25, "fps_den": 1, "total_frames": 50},
            [sequence])
    # 兼容替换 → 通过
    validate_replacement_usages(
        asset, "video",
        {"fps": 25.0, "fps_num": 25, "fps_den": 1, "total_frames": 2000},
        [sequence])
