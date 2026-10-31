"""T20 · 运行时组件测试：取消/守卫/worker/闸门/临时产物/索引失效。"""

from __future__ import annotations

import threading
import time

import pytest

from tests.edit.insights import synthetic as syn

from src.features.edit.insights.capture import capture_readonly_state
from src.features.edit.insights.index import SearchIndexCache, build_search_rows
from src.features.edit.insights.runtime import (
    BoundedWorker,
    CancelToken,
    CancelledError,
    CaptureGuard,
    InsightsConfig,
    TempArtifactManager,
    ThumbnailGate,
)


pytestmark = [pytest.mark.edit_core]


# ── CancelToken / CaptureGuard ──────────────────────────────────


def test_cancel_token_cooperative():
    token = CancelToken()
    assert not token.cancelled
    token.raise_if_cancelled()  # 未取消不抛
    token.cancel()
    assert token.cancelled
    with pytest.raises(CancelledError):
        token.raise_if_cancelled()


def test_capture_guard_stale_rejected():
    guard = CaptureGuard()
    guard.publish("cap-1")
    assert guard.is_current("cap-1")
    guard.publish("cap-2")  # refresh
    assert not guard.is_current("cap-1")  # 旧捕获结果拒收
    assert guard.is_current("cap-2")


# ── BoundedWorker ────────────────────────────────────────────────


def test_worker_executes_and_returns():
    worker = BoundedWorker()
    try:
        results: list = []
        done = threading.Event()

        def on_done(result):
            results.append(result)
            done.set()

        worker.submit(
            lambda token: 40 + 2, capture_id="cap-1", on_done=on_done
        )
        assert done.wait(timeout=5)
        assert results[0].ok and results[0].value == 42
    finally:
        worker.shutdown()


def test_worker_cancellation_at_checkpoint():
    worker = BoundedWorker()
    try:
        results: list = []
        done = threading.Event()

        def long_job(token):
            for _ in range(1000):
                token.raise_if_cancelled()
                time.sleep(0.005)
            return "finished"

        job_id = worker.submit(
            long_job, capture_id="cap-1",
            on_done=lambda r: (results.append(r), done.set()),
        )
        time.sleep(0.05)  # 让作业进入循环
        worker.cancel(job_id)
        assert done.wait(timeout=5)
        assert results[0].cancelled and results[0].value is None
    finally:
        worker.shutdown()


def test_worker_job_exception_does_not_kill_worker():
    worker = BoundedWorker()
    try:
        results: list = []
        done1, done2 = threading.Event(), threading.Event()

        def boom(token):
            raise ValueError("job error")

        worker.submit(boom, capture_id="cap-1", on_done=lambda r: (results.append(r), done1.set()))
        assert done1.wait(timeout=5)
        assert not results[0].ok and results[0].error == "job error"

        worker.submit(lambda t: "ok", capture_id="cap-1", on_done=lambda r: (results.append(r), done2.set()))
        assert done2.wait(timeout=5)
        assert results[1].ok and results[1].value == "ok"
    finally:
        worker.shutdown()


def test_worker_shutdown_releases_thread():
    worker = BoundedWorker()
    thread = worker._threads[0]
    worker.shutdown(timeout=5)
    assert not thread.is_alive()
    with pytest.raises(RuntimeError):
        worker.submit(lambda t: 1, capture_id="cap")


# ── ThumbnailGate ────────────────────────────────────────────────


def test_gate_limits_inflight_to_two():
    gate = ThumbnailGate(inflight_limit=2, queue_limit=8)
    assert gate.acquire(timeout=0.5)
    assert gate.acquire(timeout=0.5)
    assert gate.inflight == 2
    # 第三个拿不到名额（超时拒绝）
    assert not gate.acquire(timeout=0.1)
    gate.release()
    assert gate.acquire(timeout=0.5)  # 释放后可再入


def test_gate_pause_blocks_new_but_keeps_inflight():
    gate = ThumbnailGate(inflight_limit=2, queue_limit=8)
    assert gate.acquire(timeout=0.5)
    gate.pause()
    assert gate.paused
    assert not gate.acquire(timeout=0.2)  # 暂停期不派发
    assert gate.inflight == 1             # 已在途不受影响
    gate.release()
    gate.resume()
    assert gate.acquire(timeout=1.0)


def test_gate_queue_overflow_rejects():
    gate = ThumbnailGate(inflight_limit=1, queue_limit=1)
    assert gate.acquire(timeout=0.1)      # 占满在途
    results: list[bool] = []
    threads = [
        threading.Thread(target=lambda: results.append(gate.acquire(timeout=0.05)))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # queue_limit=1：至多 1 个排队等待，其余立即拒绝
    assert results.count(False) >= 3


# ── TempArtifactManager ──────────────────────────────────────────


def test_temp_artifacts_publish_and_cleanup(tmp_path):
    mgr = TempArtifactManager(tmp_path, task_id="t1")
    final = tmp_path / "out" / "report.csv"
    final.parent.mkdir()
    final.write_text("旧产物", encoding="utf-8")       # 已存在的旧产物
    temp = mgr.temp_path("report.csv")
    temp.write_text("新产物", encoding="utf-8")
    mgr.publish("report.csv", final)
    assert final.read_text(encoding="utf-8") == "新产物"  # 原子替换
    assert not temp.exists()                              # 临时文件已移走
    mgr.cleanup()
    assert not mgr.task_dir.exists()                      # 只删自己的目录
    assert final.exists()                                 # 已发布产物不受影响


def test_temp_publish_missing_temp_fails_and_keeps_old(tmp_path):
    mgr = TempArtifactManager(tmp_path, task_id="t2")
    final = tmp_path / "keep.txt"
    final.write_text("旧", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        mgr.publish("never-written", final)
    assert final.read_text(encoding="utf-8") == "旧"      # 失败不覆盖旧产物


# ── 索引缓存失效 ─────────────────────────────────────────────────


def test_index_cache_hits_same_content_and_invalidates_new_capture():
    st = syn.build_synthetic_state()
    snap1 = capture_readonly_state(st.state, capture_id="cap-a", captured_at=1.0)
    cache = SearchIndexCache(capacity=4)
    rows1 = cache.rows_for(snap1)
    assert cache.stats["misses"] == 1
    # 同一快照再次取 → 命中
    assert cache.rows_for(snap1) is rows1
    assert cache.stats["hits"] == 1
    # 内容相同的重捕获（revision 变化，content_hash 相同）→ 新 key miss
    # 但行集重建后与旧行集相等（内容一致）
    from tests.edit.insights.synthetic import build_bumped_state

    bumped = build_bumped_state(st)
    snap2 = capture_readonly_state(bumped.state, capture_id="cap-b", captured_at=2.0)
    rows2 = cache.rows_for(snap2)
    assert cache.stats["misses"] == 2
    assert rows2 == rows1  # 内容相同 → 行集一致
    # 内容真实变化 → 行集不同（新 capture_id + 真实改名字段）
    from dataclasses import replace as dc_replace

    changed = dc_replace(
        snap1,
        capture_id="cap-x",  # 新捕获身份（key 必须不同）
        sequences=tuple(
            dc_replace(s, name=s.name + "改") if s.sequence_id == syn.SEQ_MAIN else s
            for s in snap1.sequences
        ),
    )
    rows3 = cache.rows_for(changed)
    assert cache.stats["misses"] == 3
    assert rows3 != rows1


def test_index_rows_are_column_pruned():
    st = syn.build_synthetic_state()
    snap = capture_readonly_state(st.state, capture_id="cap-x", captured_at=0.0)
    rows = build_search_rows(snap)
    # 手算：6 clip + 3 cue + 2 marker + 3 asset = 14 行
    assert len(rows) == 14
    # 行内无路径/无宿主对象引用（列裁剪）
    for row in rows:
        assert isinstance(row.label, str)
        assert "/Volumes" not in row.label
        assert not hasattr(row, "metadata")


def test_index_capacity_evicts_oldest():
    st = syn.build_synthetic_state()
    cache = SearchIndexCache(capacity=2)
    for i in range(3):
        snap = capture_readonly_state(st.state, capture_id=f"cap-{i}", captured_at=float(i))
        cache.rows_for(snap)
    assert len(cache) == 2
    assert cache.stats["evictions"] == 1


def test_search_over_index_rows_consistent_with_unified_search():
    """索引行扫描与 unified_search 同命中集（列裁剪不丢结果）。"""
    from src.features.edit.insights.search import SearchQuery, unified_search

    st = syn.build_synthetic_state()
    snap = capture_readonly_state(st.state, capture_id="cap-i", captured_at=0.0)
    rows = build_search_rows(snap)
    needle = "你好"
    index_hits = {
        (r.sequence_id, r.entity_id)
        for r in rows
        if needle in r.label or any(needle in t for t in r.extra_texts)
    }
    direct = unified_search(snap, SearchQuery(text=needle))
    assert index_hits == {(h.sequence_id, h.entity_id) for h in direct.hits}
