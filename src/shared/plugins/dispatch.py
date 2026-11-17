"""F4：权威复核与调度的原子边界——同一把锁持有 revision/epoch/token。

此前 revalidate（worker）→dispatch（GUI）之间是开放窗口：停用/切库/
撤权落在窗口里时，dispatch 侧逐项复查仍可能与状态变更交错（复查通过
→变更落地→提交发出）。DTO 票据（revalidated 标记/弱引用表）防伪不能
代替这个原子边界——票据只证明"曾经复核过"，不证明"提交时仍然成立"。

``DispatchAuthority`` 的契约：

* **check_and_submit**：在**持有权威锁**的临界区内完成全部复核（身份/
  取消/期限/库代数/epoch）并调用提交——复核与提交之间不可能插入任何
  经本边界的状态变更；
* **mutate / invalidate**：停用、切库、撤权等状态变更经同一把锁串行
  ——要么完整发生在提交之前（提交侧拒绝 `AUTHORITY_EPOCH_MOVED`/
  对应错误），要么完整发生在提交之后（变更对下一次调用生效）；
* epoch 单调递增：任何经边界的变更都推进 epoch；调用在 prepare 时冻结
  epoch，提交时在锁内比对——票据之外的事实判定。

锁纪律：临界区内只做判定与提交（提交方须为非阻塞或快速返回的 Qt 线
程安全调用）；慢 I/O 的变更经 ``mutate`` 在调用方线程执行（如 worker），
期间提交侧短暂等待——顺序性优先于微秒级延迟。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class AuthorityRejected(Exception):
    """边界内复核失败——复核与提交原子地不成立。"""

    def __init__(self, error_code: str, message: str, *, retriable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.retriable = retriable


@dataclass(frozen=True)
class _EpochState:
    epoch: int = 0


class DispatchAuthority:
    """持有 epoch（状态代数）与锁的唯一权威；复核+提交在此原子完成。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._epoch = 0
        self._last_mutation = ""

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    @property
    def last_mutation(self) -> str:
        """最近一次经边界的变更标签（诊断用）。"""
        with self._lock:
            return self._last_mutation

    # ------------------------------------------------------------ 变更侧
    def mutate(self, label: str, work: Callable[[], T]) -> T:
        """状态变更（停用/切库/撤权…）：与提交串行，落地即推进 epoch。"""
        with self._lock:
            self._epoch += 1
            self._last_mutation = str(label)
            logger.debug("[AUTHORITY] mutate %s -> epoch %s", label, self._epoch)
            return work()

    def invalidate(self, label: str) -> None:
        """无操作体的变更宣告（如切库由宿主外部完成，仅需作废在途）。"""
        with self._lock:
            self._epoch += 1
            self._last_mutation = str(label)
            logger.debug("[AUTHORITY] invalidate %s -> epoch %s", label, self._epoch)

    # ------------------------------------------------------------ 提交侧
    def check_and_submit(self, frozen_epoch: int,
                         recheck: Callable[[], None],
                         submit: Callable[[], T]) -> T:
        """锁内复核 + 提交。

        ``recheck`` 抛 ``AuthorityRejected``/``CommandRejected`` 时提交
        不会发生；epoch 与 ``frozen_epoch`` 不一致直接拒绝（经边界的
        变更已发生）。任何其他异常同样阻止提交（不吞错）。
        """
        with self._lock:
            if int(frozen_epoch) != self._epoch:
                raise AuthorityRejected(
                    "AUTHORITY_EPOCH_MOVED",
                    f"插件状态在复核与提交之间已变化（{self._last_mutation or '状态变更'}），请重新发起。",
                    retriable=True)
            recheck()          # 全部业务复核（身份/取消/期限/库代数/可启动）
            return submit()    # 复核通过后立即提交——窗口内插不进变更


__all__ = ["AuthorityRejected", "DispatchAuthority"]
