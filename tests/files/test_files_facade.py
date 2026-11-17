"""C2：类型化异步 facade（有界 worker / 代际丢弃 / 错误通道）。"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from src.features.files.contracts import QueryRequest
from src.features.files.repository import FilesCatalog, FilesDataLocator
from src.features.files.services.facade import (
    FacadeError,
    FacadeOutcome,
    FilesFacade,
    query_result_of,
)


@pytest.fixture()
def env(tmp_path):
    catalog = FilesCatalog(FilesDataLocator.for_config_root(tmp_path))
    library = catalog.register_library(tmp_path / "lib")
    lib = tmp_path / "lib"
    lib.mkdir()
    for name in ("a.png", "b.png", "needle.png"):
        (lib / name).write_bytes(b"x")
    yield tmp_path, catalog, library, lib
    catalog.close()


class Collector:
    """线程安全收集回调结果。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.outcomes: list[FacadeOutcome] = []
        self.event = threading.Event()

    def __call__(self, outcome: FacadeOutcome) -> None:
        with self.lock:
            self.outcomes.append(outcome)
        self.event.set()

    def wait(self, count: int = 1, timeout: float = 5.0) -> list[FacadeOutcome]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if len(self.outcomes) >= count:
                    return list(self.outcomes)
            time.sleep(0.01)
        raise AssertionError("回调超时未送达")

    @property
    def count(self) -> int:
        with self.lock:
            return len(self.outcomes)


def test_list_delivers_typed_result_with_identity(env):
    tmp, catalog, library, lib = env
    facade = FilesFacade(catalog)
    try:
        collector = Collector()
        facade.list_directory(
            QueryRequest(request_id="req-1", path=str(lib)),
            library_id=library.library_id, listener=collector)
        outcomes = collector.wait(1)
        result = query_result_of(outcomes[0])
        assert result.request_id == "req-1" and result.total == 3
        assert outcomes[0].generation == 1
        assert result.revision == catalog.revision
    finally:
        facade.shutdown()


def test_stale_result_is_dropped_never_overrides_new(env):
    """同一路径连发两次：慢的旧结果被丢弃，只有最新代际送达。"""
    tmp, catalog, library, lib = env
    facade = FilesFacade(catalog, max_workers=2)
    try:
        gate = threading.Event()
        original = facade.query.list_directory
        calls: list[str] = []

        def _slow_then_fast(request: QueryRequest, **kwargs):
            calls.append(request.request_id)
            if request.request_id == "slow":
                gate.wait(5.0)  # 旧请求压住，直到新请求完成
            return original(request, **kwargs)

        facade.query.list_directory = _slow_then_fast  # type: ignore[method-assign]
        slow = Collector()
        fast = Collector()
        facade.list_directory(QueryRequest(request_id="slow", path=str(lib)),
                              library_id=library.library_id, listener=slow)
        time.sleep(0.05)  # 确保 slow 先被 worker 取走
        facade.list_directory(QueryRequest(request_id="fast", path=str(lib)),
                              library_id=library.library_id, listener=fast)
        fast.wait(1)
        gate.set()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and slow.count == 0:
            time.sleep(0.01)
        assert slow.count == 0, "旧代际结果不得送达（迟到覆盖是缺陷）"
        assert facade.dropped_results == 1
        assert query_result_of(fast.outcomes[0]).request_id == "fast"
    finally:
        facade.shutdown()


def test_errors_are_delivered_not_swallowed(env):
    tmp, catalog, library, lib = env
    facade = FilesFacade(catalog)
    try:
        collector = Collector()
        facade.list_directory(
            QueryRequest(request_id="err", path=str(tmp / "ghost")),
            library_id=library.library_id, listener=collector)
        outcome = collector.wait(1)[0]
        assert outcome.error is not None and outcome.error.code == "not_found"
        with pytest.raises(Exception):
            query_result_of(outcome)
    finally:
        facade.shutdown()


def test_bounded_workers_queue_instead_of_reject(env):
    tmp, catalog, library, lib = env
    facade = FilesFacade(catalog, max_workers=1)
    try:
        collectors = [Collector() for _ in range(3)]
        for index, collector in enumerate(collectors):
            facade.scan_library(library.library_id,
                                request_id=f"scan-{index}", listener=collector)
        for index, collector in enumerate(collectors):
            outcomes = collector.wait(1)
            assert outcomes[0].ok, f"scan-{index} 被拒绝/失败"
    finally:
        facade.shutdown()


def test_unexpected_exception_becomes_diagnosable_error(env):
    tmp, catalog, library, lib = env
    facade = FilesFacade(catalog)
    try:
        def _explode():
            raise RuntimeError("worker 内意外崩溃")

        collector = Collector()
        facade._submit(key="boom", request_id="boom", work=_explode,
                       listener=collector, wrap=lambda value: value)
        outcome = collector.wait(1)[0]
        assert outcome.error is not None
        assert outcome.error.code == "service_unavailable"
        assert "worker 内意外崩溃" in outcome.error.message
    finally:
        facade.shutdown()


def test_shutdown_blocks_new_submissions(env):
    tmp, catalog, library, lib = env
    facade = FilesFacade(catalog)
    facade.shutdown()
    with pytest.raises(FacadeError):
        facade.list_directory(
            QueryRequest(request_id="late", path=str(lib)),
            library_id=library.library_id, listener=lambda outcome: None)


# ---------------------------------------------------------------- D1 扩展
def test_tree_delivers_children_and_same_path_supersedes(env):
    tmp, catalog, library, lib = env
    (lib / "sub1").mkdir()
    (lib / "sub2").mkdir()
    (lib / "f.png").write_bytes(b"x")  # 文件不进树
    facade = FilesFacade(catalog, max_workers=1)
    try:
        first, second = Collector(), Collector()
        facade.tree(str(lib), request_id="t1", listener=first)
        facade.tree(str(lib), request_id="t2", listener=second)
        outcomes = second.wait(1)
        names = sorted(item["name"] for item in outcomes[0].result)
        assert names == ["sub1", "sub2"]
        assert first.count == 0  # 同路径旧请求被作废（不送达）
        assert facade.dropped_results >= 1
    finally:
        facade.shutdown()


def test_read_file_local_bytes_size_cap_and_missing(env):
    tmp, catalog, library, lib = env
    target = lib / "f.bin"
    target.write_bytes(b"payload")
    facade = FilesFacade(catalog, max_workers=1)
    try:
        ok = Collector()
        facade.read_file(str(target), request_id="r1", listener=ok, max_bytes=64)
        outcome = ok.wait(1)[0]
        assert outcome.ok and outcome.result == b"payload"

        oversize = Collector()
        facade.read_file(str(target), request_id="r2", listener=oversize,
                         max_bytes=4)
        error = oversize.wait(1)[0].error
        assert error is not None and error.code == "unsupported"
        assert "预览上限" in error.message

        missing = Collector()
        facade.read_file(str(lib / "nope.bin"), request_id="r3",
                         listener=missing, max_bytes=64)
        assert missing.wait(1)[0].error.code == "not_found"
    finally:
        facade.shutdown()


def test_collection_members_hydrate_disk_truth_keep_offline(env):
    tmp, catalog, library, lib = env
    lib_id = library.library_id
    from src.features.files.contracts import Availability
    cid = catalog.create_collection(lib_id, "选集")
    keep = lib / "keep.png"
    keep.write_bytes(b"x")
    gone = lib / "gone.png"
    gone.write_bytes(b"x")
    catalog.add_to_collection(cid, str(keep))
    catalog.add_to_collection(cid, str(gone))
    gone.unlink()  # 成员已从磁盘消失
    facade = FilesFacade(catalog, max_workers=1)
    try:
        collector = Collector()
        facade.collection_members(cid, request_id="c1", listener=collector)
        result = query_result_of(collector.wait(1)[0])
        by_path = {entry.path: entry for entry in result.entries}
        assert by_path[str(keep)].availability == Availability.ONLINE
        assert by_path[str(gone)].availability == Availability.OFFLINE  # 不静默丢行
        assert result.path == f"collection://{cid}"
    finally:
        facade.shutdown()


def test_batch_rename_preview_execute_and_plan_errors(env):
    tmp, catalog, library, lib = env
    lib_id = library.library_id
    (lib / "one.png").write_bytes(b"a")
    (lib / "two.png").write_bytes(b"b")
    facade = FilesFacade(catalog, max_workers=1)
    try:
        preview = Collector()
        facade.batch_rename_preview(
            lib_id, [str(lib / "one.png"), str(lib / "two.png")],
            "template", {"template": "{name}_{seq:2}.{ext}"},
            request_id="br1", listener=preview)
        plan = preview.wait(1)[0].result
        assert [item.proposed_name for item in plan.items] == [
            "one_01.png", "two_02.png"]

        executed = Collector()
        facade.batch_rename_execute(plan.plan_id, lib_id,
                                    request_id="br2", listener=executed)
        outcomes = executed.wait(1)[0].result
        assert all(o.ok for o in outcomes)
        assert (lib / "one_01.png").exists() and (lib / "two_02.png").exists()

        bad_mode = Collector()
        facade.batch_rename_preview(
            lib_id, [str(lib / "one_01.png")], "nope", {},
            request_id="br3", listener=bad_mode)
        error = bad_mode.wait(1)[0].error
        assert error is not None
        assert error.details.get("plan_error") == "BAD_MODE"
    finally:
        facade.shutdown()


def test_scan_libraries_aggregate_with_progress(env):
    tmp, catalog, library, lib = env
    lib2 = tmp / "lib2"
    lib2.mkdir()
    second = catalog.register_library(lib2)
    (lib / "s1.png").write_bytes(b"x")
    (lib2 / "s2.png").write_bytes(b"x")
    progress: list[tuple[int, int]] = []
    facade = FilesFacade(catalog, max_workers=1)
    try:
        collector = Collector()
        facade.scan_libraries(
            [library.library_id, second.library_id],
            request_id="scan-all", listener=collector,
            progress=lambda done, total: progress.append((done, total)))
        outcome = collector.wait(1)[0]
        assert outcome.ok
        assert len(outcome.result.items) == 2  # 每库一项
        assert progress and progress[-1][0] == progress[-1][1]
        assert catalog.asset(library.library_id, str(lib / "s1.png")) is not None
        assert catalog.asset(second.library_id, str(lib2 / "s2.png")) is not None
    finally:
        facade.shutdown()


def test_run_generic_submission_keeps_identity_semantics(env):
    tmp, catalog, library, lib = env
    facade = FilesFacade(catalog, max_workers=1)
    try:
        collector = Collector()
        facade.run("libraries", "libs-1",
                   lambda: facade.libraries.list_libraries(), collector)
        outcome = collector.wait(1)[0]
        assert outcome.ok
        assert [record.library_id for record in outcome.result] == [
            library.library_id]
    finally:
        facade.shutdown()
