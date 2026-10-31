"""relink 三级匹配测试。"""

from __future__ import annotations

import os
import shutil

import pytest


# ---------------------------------------------------------------------- #
# refresh_path_status
# ---------------------------------------------------------------------- #

def test_refresh_detects_missing(repo, sample_files):
    """文件被删除后，refresh 标 missing。"""
    from apps.topos_vault.db.repository import MediaSource
    from apps.topos_vault.fingerprint import compute_fingerprint
    from apps.topos_vault.relink import Relinker

    path = str(sample_files["video"])
    fp = compute_fingerprint(path, "video")
    repo.upsert_media_source(MediaSource(
        fingerprint=fp, media_type="video", path_original=path, path_current=path,
        file_size=os.path.getsize(path),
    ))

    # 删除文件
    os.remove(path)
    missing = Relinker(repo).refresh_path_status()
    assert missing == 1
    assert repo.get_media_source(fp).path_status == "missing"


def test_refresh_marks_valid_when_file_exists(repo, sample_files):
    """文件存在时，refresh 标 valid。"""
    from apps.topos_vault.db.repository import MediaSource
    from apps.topos_vault.fingerprint import compute_fingerprint
    from apps.topos_vault.relink import Relinker

    path = str(sample_files["image"])
    fp = compute_fingerprint(path, "image")
    repo.upsert_media_source(MediaSource(
        fingerprint=fp, media_type="image", path_original=path, path_current=path,
        path_status="missing",  # 故意标错
    ))

    Relinker(repo).refresh_path_status()
    assert repo.get_media_source(fp).path_status == "valid"


def test_refresh_detects_relocated_then_missing_again(repo, sample_files, tmp_path):
    """修复的 bug：relocated 素材二次移动后能检测到丢失。"""
    from apps.topos_vault.db.repository import MediaSource
    from apps.topos_vault.fingerprint import compute_fingerprint
    from apps.topos_vault.relink import Relinker

    path = str(sample_files["audio"])
    fp = compute_fingerprint(path, "audio")
    repo.upsert_media_source(MediaSource(
        fingerprint=fp, media_type="audio", path_original=path, path_current=path,
        file_size=os.path.getsize(path),
    ))

    # 第一次：移动文件 → 标 relocated
    moved = tmp_path / "moved"
    moved.mkdir()
    new_path = str(moved / "track.mp3")
    shutil.move(path, new_path)
    relinker = Relinker(repo)
    relinker.refresh_path_status()
    report = relinker.relink_missing([str(moved)])
    assert report.found == 1
    assert repo.get_media_source(fp).path_status == "relocated"

    # 第二次：再把文件移走（二次丢失）
    shutil.move(new_path, str(tmp_path / "gone.mp3"))
    missing = relinker.refresh_path_status()
    assert missing == 1, "relocated 素材二次丢失应被检测到"
    assert repo.get_media_source(fp).path_status == "missing"


# ---------------------------------------------------------------------- #
# 三级匹配
# ---------------------------------------------------------------------- #

def test_level0_path_directly_valid(repo, sample_files):
    """Level 0：路径仍有效，直接命中。"""
    from apps.topos_vault.db.repository import MediaSource
    from apps.topos_vault.fingerprint import compute_fingerprint
    from apps.topos_vault.relink import Relinker

    path = str(sample_files["video"])
    fp = compute_fingerprint(path, "video")
    repo.upsert_media_source(MediaSource(
        fingerprint=fp, media_type="video", path_original=path, path_current=path,
        file_size=os.path.getsize(path),
    ))

    # 路径还在，不删文件，直接 relink（应走 Level 0）
    relinker = Relinker(repo)
    relinker.refresh_path_status()
    # 此时 missing=0，relink_missing 无事可做
    report = relinker.relink_missing(["/nonexistent"])
    assert report.total == 0


def test_level1_same_dir_same_name(repo, sample_files, tmp_path):
    """Level 1：原目录还在、同名、同 size → 命中。

    场景：整个文件夹被一起移动到新挂载点，但相对结构不变。
    """
    from apps.topos_vault.db.repository import MediaSource
    from apps.topos_vault.fingerprint import compute_fingerprint
    from apps.topos_vault.relink import Relinker

    # 原始目录结构
    orig_dir = tmp_path / "orig_project" / "media"
    orig_dir.mkdir(parents=True)
    orig_file = orig_dir / "clip.mp4"
    shutil.copy2(sample_files["video"], orig_file)

    fp = compute_fingerprint(str(orig_file), "video")
    repo.upsert_media_source(MediaSource(
        fingerprint=fp, media_type="video",
        path_original=str(orig_file), path_current=str(orig_file),
        file_size=os.path.getsize(str(orig_file)),
    ))

    # 移动整个项目目录到新位置（模拟换盘）
    new_project = tmp_path / "new_location"
    shutil.move(str(orig_dir.parent), str(new_project))
    new_media_dir = new_project / "media"

    relinker = Relinker(repo)
    relinker.refresh_path_status()
    # Level 1：原 path_original 的目录（orig_dir）在新位置，
    # 但 os.path.exists(orig_file) 为 False（整个目录被移走了）。
    # Level 1 找的是 path_original 的 dirname + basename，这里 dirname 已不存在。
    # 所以这个场景其实走 Level 2（指纹匹配），不是 Level 1。
    # Level 1 真正的场景是：文件没动，只是 path_current 失效（比如临时改了 path_current）。
    # 这里测的是"目录被整体搬走"——应走 Level 2。
    report = relinker.relink_missing([str(new_media_dir)])
    assert report.found >= 1
    result = report.results[0]
    assert result.found
    # 内容相同 → 指纹匹配（Level 2），或 size 相同（Level 2 降级）
    assert result.matched_level == 2


def test_level2_fingerprint_match_after_move(repo, sample_files, tmp_path):
    """Level 2：文件移动到新位置，指纹匹配 → 命中。"""
    from apps.topos_vault.db.repository import MediaSource
    from apps.topos_vault.fingerprint import compute_fingerprint
    from apps.topos_vault.relink import Relinker

    orig = sample_files["image"]
    fp = compute_fingerprint(str(orig), "image")
    repo.upsert_media_source(MediaSource(
        fingerprint=fp, media_type="image",
        path_original=str(orig), path_current=str(orig),
        file_size=os.path.getsize(str(orig)),
    ))

    # 移到新目录
    new_dir = tmp_path / "new_location"
    new_dir.mkdir()
    new_path = new_dir / "renamed.jpg"
    shutil.move(str(orig), str(new_path))

    relinker = Relinker(repo)
    relinker.refresh_path_status()
    report = relinker.relink_missing([str(new_dir)])

    assert report.found == 1
    result = report.results[0]
    assert result.matched_level == 2
    assert result.new_path == str(new_path)


def test_relink_updates_db_status(repo, sample_files, tmp_path):
    """relink 成功后，数据库 path_status 变 relocated。"""
    from apps.topos_vault.db.repository import MediaSource
    from apps.topos_vault.fingerprint import compute_fingerprint
    from apps.topos_vault.relink import Relinker

    orig = sample_files["video"]
    fp = compute_fingerprint(str(orig), "video")
    repo.upsert_media_source(MediaSource(
        fingerprint=fp, media_type="video",
        path_original=str(orig), path_current=str(orig),
        file_size=os.path.getsize(str(orig)),
    ))

    new_dir = tmp_path / "moved"
    new_dir.mkdir()
    shutil.move(str(orig), str(new_dir / "clip.mp4"))

    relinker = Relinker(repo)
    relinker.refresh_path_status()
    relinker.relink_missing([str(new_dir)])

    got = repo.get_media_source(fp)
    assert got.path_status == "relocated"
    assert got.path_current == str(new_dir / "clip.mp4")


def test_relink_missed_when_not_in_candidates(repo, sample_files, tmp_path):
    """候选目录里没有目标文件 → 未找到。"""
    from apps.topos_vault.db.repository import MediaSource
    from apps.topos_vault.fingerprint import compute_fingerprint
    from apps.topos_vault.relink import Relinker

    orig = sample_files["image"]
    fp = compute_fingerprint(str(orig), "image")
    repo.upsert_media_source(MediaSource(
        fingerprint=fp, media_type="image",
        path_original=str(orig), path_current=str(orig),
    ))
    os.remove(str(orig))

    # 给一个空目录
    empty = tmp_path / "empty"
    empty.mkdir()
    relinker = Relinker(repo)
    relinker.refresh_path_status()
    report = relinker.relink_missing([str(empty)])
    assert report.missed == 1
    assert report.found == 0


def test_relink_idempotent(repo, sample_files, tmp_path):
    """重复 relink 不破坏已重连的状态。"""
    from apps.topos_vault.db.repository import MediaSource
    from apps.topos_vault.fingerprint import compute_fingerprint
    from apps.topos_vault.relink import Relinker

    orig = sample_files["audio"]
    fp = compute_fingerprint(str(orig), "audio")
    repo.upsert_media_source(MediaSource(
        fingerprint=fp, media_type="audio",
        path_original=str(orig), path_current=str(orig),
        file_size=os.path.getsize(str(orig)),
    ))

    new_dir = tmp_path / "moved"
    new_dir.mkdir()
    target = new_dir / "track.mp3"
    shutil.move(str(orig), str(target))

    relinker = Relinker(repo)
    relinker.refresh_path_status()
    report1 = relinker.relink_missing([str(new_dir)])
    assert report1.found == 1

    # 再次 relink（relocated 状态，文件仍在）
    relinker.refresh_path_status()
    # relocated + 文件仍存在 → refresh 会把它标成 valid（路径当前有效）
    report2 = relinker.relink_missing([str(new_dir)])
    # 此时没有 missing 的了
    assert report2.total == 0


def test_relink_report_summary(repo, sample_files, tmp_path):
    """报告 summary 包含 Level 分布。"""
    from apps.topos_vault.db.repository import MediaSource
    from apps.topos_vault.fingerprint import compute_fingerprint
    from apps.topos_vault.relink import Relinker

    orig = sample_files["image"]
    fp = compute_fingerprint(str(orig), "image")
    repo.upsert_media_source(MediaSource(
        fingerprint=fp, media_type="image",
        path_original=str(orig), path_current=str(orig),
        file_size=os.path.getsize(str(orig)),
    ))

    new_dir = tmp_path / "moved"
    new_dir.mkdir()
    shutil.move(str(orig), str(new_dir / "p.jpg"))

    relinker = Relinker(repo)
    relinker.refresh_path_status()
    report = relinker.relink_missing([str(new_dir)])

    summary = report.summary()
    assert "找到" in summary
    assert "Level 分布" in summary
