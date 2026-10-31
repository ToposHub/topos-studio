"""pipeline 编排器测试。"""

from __future__ import annotations

import os

import pytest


def test_classify_media_type():
    """扩展名分类。"""
    from apps.topos_vault.pipeline.orchestrator import classify_media_type

    assert classify_media_type("/x/clip.mp4") == "video"
    assert classify_media_type("/x/clip.MOV") == "video"  # 大小写不敏感
    assert classify_media_type("/x/song.mp3") == "audio"
    assert classify_media_type("/x/photo.jpg") == "image"
    assert classify_media_type("/x/photo.CR2") == "image"
    assert classify_media_type("/x/doc.txt") is None
    assert classify_media_type("/x/noext") is None


def test_orchestrator_index_single_file(repo, sample_files):
    """单个文件打标。"""
    from apps.topos_vault.pipeline.orchestrator import Orchestrator

    orch = Orchestrator(repo)
    results = orch.index_file(str(sample_files["video"]))

    assert len(results) >= 1
    assert any(r.is_done for r in results)

    # 数据库里有记录了
    from apps.topos_vault.db.repository import VaultRepository
    r = VaultRepository()
    src = r.get_media_source_by_path(str(sample_files["video"]))
    assert src is not None
    assert src.media_type == "video"


def test_orchestrator_index_directory(repo, sample_files, tmp_path):
    """目录批量打标。"""
    from apps.topos_vault.pipeline.orchestrator import Orchestrator

    media_dir = sample_files["video"].parent  # tmp_path/media
    orch = Orchestrator(repo)
    report = orch.index_directory(str(media_dir))

    assert report.total >= 4  # video + audio + image + image2
    # 至少有一些成功
    assert report.done > 0


def test_orchestrator_idempotent(repo, sample_files):
    """重复 index 同一文件，第二次应跳过（已完成）。"""
    from apps.topos_vault.pipeline.orchestrator import Orchestrator

    orch = Orchestrator(repo)
    path = str(sample_files["image"])

    r1 = orch.index_file(path)
    assert any(res.is_done for res in r1)

    r2 = orch.index_file(path)
    # 第二次应该全部跳过（is_done=True）
    from apps.topos_vault.pipeline.steps import StepStatus
    assert all(res.status == StepStatus.SKIPPED for res in r2)


def test_orchestrator_nonexistent_file(repo):
    """不存在的文件 → 失败。"""
    from apps.topos_vault.pipeline.orchestrator import Orchestrator

    orch = Orchestrator(repo)
    results = orch.index_file("/nonexistent/file.mp4")
    assert any(r.is_failed for r in results)


def test_orchestrator_non_media_skipped(repo, tmp_path):
    """非素材文件被跳过。"""
    from apps.topos_vault.pipeline.orchestrator import Orchestrator

    txt = tmp_path / "notes.txt"
    txt.write_text("hello")
    orch = Orchestrator(repo)
    results = orch.index_file(str(txt), media_type="auto")

    from apps.topos_vault.pipeline.steps import StepStatus
    assert all(r.status == StepStatus.SKIPPED for r in results)


def test_orchestrator_progress_callback(repo, sample_files):
    """进度回调被调用。"""
    from apps.topos_vault.pipeline.orchestrator import Orchestrator, ProgressInfo

    orch = Orchestrator(repo)
    progress_log: list[ProgressInfo] = []

    def cb(info: ProgressInfo):
        progress_log.append(info)

    orch.index_file(str(sample_files["audio"]), progress=cb)
    assert len(progress_log) >= 1
    assert progress_log[-1].current_path == str(sample_files["audio"])


def test_orchestrator_add_step(repo):
    """动态添加 step。"""
    from apps.topos_vault.pipeline.orchestrator import Orchestrator
    from apps.topos_vault.pipeline.steps import PipelineStep

    orch = Orchestrator(repo)

    class DummyStep(PipelineStep):
        step_name = "dummy"
        applies_to = ("video",)

        def _run(self, source):
            return "dummy done"

    orch.add_step("video", DummyStep(repo))
    steps = orch.list_pipeline_steps("video")
    assert "ingest" in steps
    assert "dummy" in steps


def test_batch_report_summary(repo, sample_files):
    """批次报告 summary。"""
    from apps.topos_vault.pipeline.orchestrator import Orchestrator

    orch = Orchestrator(repo)
    report = orch.index_directory(str(sample_files["video"].parent))
    summary = report.summary()
    assert "成功" in summary
    assert "总计" in summary


def test_ingest_writes_fingerprint(repo, sample_files):
    """ingest 步骤写入真实指纹（非空、带前缀）。"""
    from apps.topos_vault.pipeline.orchestrator import Orchestrator

    orch = Orchestrator(repo)
    orch.index_file(str(sample_files["video"]))

    from apps.topos_vault.db.repository import VaultRepository
    from apps.topos_vault.fingerprint import parse_fingerprint_type

    r = VaultRepository()
    src = r.get_media_source_by_path(str(sample_files["video"]))
    assert src.fingerprint
    assert parse_fingerprint_type(src.fingerprint) in ("xxh3_3pt", "size")
