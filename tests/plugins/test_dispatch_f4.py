"""F4：权威复核与调度的原子边界——同一把锁持有 revision/epoch/token。

证明口径：
* 提交在锁内进行时，经边界的变更（停用/切库/撤权）必须等待——顺序性
  （要么整体在前使提交拒绝，要么整体在后）取代"窗口内交错"；
* epoch 推进后，过期调用在锁内拒绝且**零提交**；
* DTO 票据（revalidated 标记）不再单独放行——锁内必复核。
"""
from __future__ import annotations

import threading
import time

import pytest

from src.shared.plugins.dispatch import AuthorityRejected, DispatchAuthority


def test_mutate_serializes_with_in_flight_submit():
    """提交持锁期间，变更必须等待：顺序性证明。"""
    authority = DispatchAuthority()
    entered = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def submit():
        order.append("submit:enter")
        entered.set()
        release.wait(5)          # 模拟提交在途（锁内）
        order.append("submit:exit")
        return True

    def recheck():
        return None

    worker_result = {}

    def run_dispatch():
        worker_result["value"] = authority.check_and_submit(0, recheck, submit)

    thread = threading.Thread(target=run_dispatch)
    thread.start()
    assert entered.wait(5)
    mutate_done = threading.Event()

    def run_mutate():
        authority.mutate("disable:x", lambda: order.append("mutate"))
        mutate_done.set()

    mutator = threading.Thread(target=run_mutate)
    mutator.start()
    time.sleep(0.15)
    assert not mutate_done.is_set()      # 变更被提交的临界区挡住
    release.set()
    thread.join(timeout=5)
    mutator.join(timeout=5)
    assert mutate_done.is_set()
    assert worker_result["value"] is True
    # 顺序：提交完整结束后变更才落地
    assert order == ["submit:enter", "submit:exit", "mutate"]


def test_epoch_moved_rejects_without_submit():
    authority = DispatchAuthority()
    submit_calls: list[str] = []
    authority.invalidate("library-switch")   # 冻结 epoch=0 之后推进
    with pytest.raises(AuthorityRejected) as exc:
        authority.check_and_submit(
            0, lambda: None, lambda: submit_calls.append("called"))
    assert exc.value.error_code == "AUTHORITY_EPOCH_MOVED"
    assert exc.value.retriable
    assert submit_calls == []                # 过期调用零提交


def test_recheck_failure_blocks_submit_and_propagates():
    authority = DispatchAuthority()
    submit_calls: list[str] = []

    def bad_recheck():
        raise AuthorityRejected("COMMAND_NOT_APPLICABLE", "插件已停用")

    with pytest.raises(AuthorityRejected):
        authority.check_and_submit(0, bad_recheck,
                                   lambda: submit_calls.append("called"))
    assert submit_calls == []

    def broken_recheck():
        raise RuntimeError("boom")           # 非预期异常同样阻止提交（不吞错）

    with pytest.raises(RuntimeError):
        authority.check_and_submit(0, broken_recheck,
                                   lambda: submit_calls.append("called"))
    assert submit_calls == []


def test_same_epoch_check_passes_and_submits():
    authority = DispatchAuthority()
    seen: list[int] = []
    value = authority.check_and_submit(
        authority.epoch, lambda: seen.append(1), lambda: (seen.append(2), "ok")[1])
    assert value == "ok"
    assert seen == [1, 2]                    # 复核先于提交（锁内顺序）
