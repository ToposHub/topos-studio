"""B08：投放探测/去重/取消契约测试（不启动时间线视图，不写磁盘）。"""
from __future__ import annotations

import threading

import pytest

from src.features.files.contracts import FilesErrorCode
from src.features.files.import_job import (
    DropContext,
    ImportCandidate,
    ImportProbeJob,
    ImportProbeResult,
    ImportProbeRunner,
    dedupe_paths,
    prepare_import,
)


def _run(job: ImportProbeJob) -> ImportProbeResult:
    captured: list[ImportProbeResult] = []
    job.signals.finished.connect(lambda _id, result: captured.append(result))
    job.run()
    assert captured, "探测任务必须发布结束信号，否则 UI 永远等不到结果"
    return captured[-1]


class _Asset:
    def __init__(self, path: str) -> None:
        self.path = path
        self.asset_id = "asset-" + path.rsplit("/", 1)[-1]


class _Project:
    """最小工程替身：按路径查找素材，import_asset 返回新对象。"""

    def __init__(self, assets: dict[str, _Asset] | None = None, fail_paths: set[str] | None = None) -> None:
        self.assets = {asset.asset_id: asset for asset in (assets or {}).values()}
        self._fail = fail_paths or set()
        self.imported: list[str] = []

    def get_asset_by_path(self, path: str) -> _Asset | None:
        return next((asset for asset in self.assets.values() if asset.path == path), None)

    def import_asset(self, path: str, media_type: str, metadata: dict) -> "_Project":
        if path in self._fail:
            raise RuntimeError("解码失败")
        self.imported.append(path)
        by_path = {asset.path: asset for asset in self.assets.values()}
        by_path[path] = _Asset(path)
        self.assets = {asset.asset_id: asset for asset in by_path.values()}
        return self


def test_probe_job_probes_off_thread_and_reports_metadata():
    probed: list[str] = []

    def probe(path: str) -> dict:
        probed.append(path)
        return {"duration": 3.0}

    job = ImportProbeJob(1, ["/m/a.mov", "/m/b.wav"], [], probe=probe,
                         classify=lambda path: "video" if path.endswith(".mov") else "audio")
    result = _run(job)
    assert {item.path for item in result.usable} == {"/m/a.mov", "/m/b.wav"}
    assert all(item.metadata["duration"] == 3.0 for item in result.usable)
    assert sorted(probed) == ["/m/a.mov", "/m/b.wav"]
    assert result.cancelled is False


def test_probe_failure_is_per_item_not_batch_wide():
    def probe(path: str) -> dict:
        if path.endswith(".broken"):
            raise OSError("无法读取")
        return {}

    job = ImportProbeJob(2, ["/m/good.mov", "/m/bad.broken"], [],
                         probe=probe, classify=lambda path: "video")
    result = _run(job)
    assert [item.path for item in result.usable] == ["/m/good.mov"]
    assert [item.path for item in result.failures] == ["/m/bad.broken"]
    assert result.failures[0].error.code == FilesErrorCode.UNSUPPORTED
    payload = result.to_job_result().to_dict()
    assert payload["failures"][0]["error"]["code"] == FilesErrorCode.UNSUPPORTED


def test_cancel_marks_result_cancelled_and_returns_no_candidates():
    job = ImportProbeJob(3, ["/m/a.mov"], [], probe=lambda path: {}, classify=lambda path: "video")
    job.cancel()
    result = _run(job)
    assert result.cancelled is True
    assert result.usable == ()


def test_cancel_during_probe_keeps_usable_prefix_only():
    release = threading.Event()

    def probe(path: str) -> dict:
        if path == "/m/slow.mov":
            release.wait(timeout=2)
        return {}

    job = ImportProbeJob(4, ["/m/slow.mov", "/m/next.mov"], [], probe=probe,
                         classify=lambda path: "video", max_workers=1)
    timer = threading.Timer(0.05, job.cancel)
    timer.start()
    timer2 = threading.Timer(0.2, release.set)
    timer2.start()
    try:
        result = _run(job)
    finally:
        timer.cancel()
        timer2.cancel()
        release.set()
    assert result.cancelled is True
    # 取消后不再继续收集：避免"半批"插入
    assert len(result.usable) <= 1


def test_runner_emits_finished_on_calling_thread(qtbot):
    runner = ImportProbeRunner()
    runner.finished.connect(lambda result: None)
    with qtbot.waitSignal(runner.finished, timeout=5000) as blocker:
        runner.start(["/m/a.mov"], [], probe=lambda path: {}, classify=lambda path: "video")
    assert blocker.args[0].job_id >= 1


def test_dedupe_paths_is_identity_based():
    assert dedupe_paths(["/m/A.mov", "/m/a.mov", "/m/B.mov", ""]) == ["/m/A.mov", "/m/B.mov"]


def test_prepare_import_reuses_existing_asset_and_skips_duplicate():
    existing = _Asset("/m/a.mov")
    project = _Project({"/m/a.mov": existing})
    plan = prepare_import(project, [
        ImportCandidate("/m/a.mov", "video", is_new=False),
        ImportCandidate("/m/b.mov", "video", is_new=True, metadata={"duration": 1.0}),
    ])
    assert [asset.path for asset in plan.assets] == ["/m/a.mov", "/m/b.mov"]
    assert plan.assets[0] is existing
    assert project.imported == ["/m/b.mov"]


def test_prepare_import_keeps_partial_success():
    project = _Project(fail_paths={"/m/bad.mov"})
    plan = prepare_import(project, [
        ImportCandidate("/m/ok.mov", "video", is_new=True),
        ImportCandidate("/m/bad.mov", "video", is_new=True),
    ])
    assert [asset.path for asset in plan.assets] == ["/m/ok.mov"]
    assert [item.path for item in plan.failures] == ["/m/bad.mov"]
    assert "解码失败" in plan.failures[0].error.message


def test_prepare_import_carries_probe_failure_without_importing():
    project = _Project()
    plan = prepare_import(project, [ImportCandidate(
        "/m/broken.mov", "video", is_new=True,
        error=__import__("src.features.files.contracts", fromlist=["FilesError"]).FilesError(
            "unsupported", "坏文件", False, "/m/broken.mov"))])
    assert plan.assets == ()
    assert project.imported == []
    assert len(plan.failures) == 1


def test_drop_context_describes_identity_and_target():
    context = DropContext(7, "proj-1", 3, 11, "track-2", False, "video", 240, 25.0)
    assert "proj-1@3" in context.describe()
    assert "track-2" in context.describe()
