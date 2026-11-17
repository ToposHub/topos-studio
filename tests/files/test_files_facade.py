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
