"""
ToposProjectController - 统一工程状态控制器。

统一管理 Color/Edit/Composite/Deliver 页面的 dirty 状态，
提供集中的 is_dirty() 检查接口，协调 SaveRouter 进行保存，
并支持基于 dirty 状态的自动保存机制。

设计原则：
- 统一 dirty 状态入口，聚合 PageContextManager
- 与 SaveRouter 协调，save 后自动清除 dirty
- 自动保存定时器，仅在 dirty 时触发
- 信号驱动，不直接依赖 UI 组件
- 线程安全：dirty 状态字典由内部可重入锁保护（AUD-37009），
  状态变更通过 Qt 信号广播

参考文档：
- docs/library_media_pool_project_architecture_plan_2026-05-19.md
- AGENTS.md 统一工程包架构方案 P1
"""

from __future__ import annotations

import logging
import threading
import time  # AUD-37013: 提升自 4 处函数内重复局部 import
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

logger = logging.getLogger(__name__)


# ================================================================
# 自动保存间隔约束（AUD-37008）
# ================================================================

#: 自动保存间隔下界（分钟）。更小的间隔（尤其 0）会让 QTimer 以事件
#: 循环频率触发保存风暴。from_dict 恢复持久化数据时做钳制；
#: start_auto_save / set_auto_save_interval 对显式传入值做校验。
MIN_AUTO_SAVE_INTERVAL_MINUTES = 1

#: 默认自动保存间隔（分钟）。
DEFAULT_AUTO_SAVE_INTERVAL_MINUTES = 5


def _clamp_auto_save_interval_ms(value: Any) -> int:
    """将持久化来源的间隔值规整为安全的毫秒数。

    AUD-37008：历史数据可能存有 0/负数/非整数（毫秒→分钟整除截断后
    可能为 0），直接喂给 QTimer.start(0) 会形成每个事件循环 tick 都
    触发的保存风暴。此处统一钳制到下界。
    """
    default_ms = DEFAULT_AUTO_SAVE_INTERVAL_MINUTES * 60 * 1000
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return default_ms
    minimum_ms = MIN_AUTO_SAVE_INTERVAL_MINUTES * 60 * 1000
    return max(ms, minimum_ms)


def _validate_auto_save_interval_minutes(interval_minutes: Any) -> int:
    """校验并返回分钟间隔；非法值抛 ValueError（显式契约）。"""
    if not isinstance(interval_minutes, int) or isinstance(interval_minutes, bool):
        raise ValueError(
            f"interval_minutes must be an int, got {type(interval_minutes).__name__}"
        )
    if interval_minutes < MIN_AUTO_SAVE_INTERVAL_MINUTES:
        raise ValueError(
            f"interval_minutes must be >= {MIN_AUTO_SAVE_INTERVAL_MINUTES}, "
            f"got {interval_minutes}"
        )
    return interval_minutes


# ================================================================
# PageType - 页面类型枚举（从 main_window.py 移入）
# ================================================================

class PageType(Enum):
    """页面类型枚举。"""
    LIBRARY = "Library"
    EDIT = "Edit"
    COLOR = "Color"
    COMPOSITE = "Composite"
    DELIVER = "Deliver"


# ================================================================
# DirtyState - Dirty 状态数据类
# ================================================================

@dataclass(frozen=True)
class DirtyState:
    """
    Dirty 状态记录。

    记录单个页面的 dirty 状态及其原因。

    Attributes:
        page: 页面类型
        is_dirty: 是否有未保存的更改
        reason: dirty 原因（可选）
        timestamp: 状态变更时间戳
    """
    page: PageType
    is_dirty: bool
    reason: str | None = None
    timestamp: float = 0.0


# ================================================================
# DirtyStateChanged - 状态变更事件
# ================================================================

@dataclass(frozen=True)
class DirtyStateChanged:
    """
    Dirty 状态变更事件。

    用于通知状态变更的详细信息。

    Attributes:
        page: 页面类型
        old_state: 旧状态
        new_state: 新状态
        reason: 变更原因
    """
    page: PageType
    old_state: bool
    new_state: bool
    reason: str | None = None


# ================================================================
# ToposProjectController - 统一工程状态控制器
# ================================================================

class ToposProjectController(QObject):
    """
    统一工程状态控制器。

    统一管理 Color/Edit/Composite/Deliver 页面的 dirty 状态，
    提供集中的 is_dirty() 检查接口，协调 SaveRouter 进行保存。

    ## 核心功能

    1. **统一 dirty 状态管理**
       - `is_dirty()` / `is_page_dirty(page)` - 检查 dirty 状态
       - `mark_dirty(page, reason)` - 标记页面 dirty
       - `clear_dirty(page)` / `clear_all_dirty()` - 清除 dirty

    2. **自动保存机制**
       - 定时器检查 dirty 状态
       - 仅在 dirty 时触发保存
       - 可配置保存间隔

    3. **与 SaveRouter 协调**
       - 保存成功后自动清除 dirty
       - 保存失败保留 dirty 状态

    4. **信号驱动**
       - `dirty_changed` - dirty 状态变更信号
       - `auto_save_triggered` - 自动保存触发信号
       - `save_completed` - 保存完成信号

    ## 使用方法

    ```python
    # 创建控制器
    controller = ToposProjectController()

    # 设置保存回调
    controller.set_save_callback(save_router.save)

    # 各页面修改时标记 dirty
    controller.mark_dirty(PageType.COLOR, "参数调整")
    controller.mark_dirty(PageType.EDIT, "添加剪辑")

    # 检查 dirty 状态
    if controller.is_dirty():
        # 有未保存的更改

    # 保存并清除 dirty
    controller.save()

    # 启动自动保存
    controller.start_auto_save(interval_minutes=5)
    ```

    ## 线程安全

    dirty 状态字典与保存时间戳由内部可重入锁（``threading.RLock``）
    保护（AUD-37009）：任意线程调用 mark_dirty/clear_dirty 等方法都是
    原子的，快照类方法（to_dict/get_all_dirty_states）在并发变更下也
    不会抛错。状态变更仍通过 Qt 信号广播；使用 QueuedConnection 可将
    槽安全地投递到 UI 线程执行。
    """

    # ==================== 信号定义 ====================

    # Dirty 状态变更信号
    dirty_changed = pyqtSignal(object)  # DirtyStateChanged

    # 自动保存触发信号
    auto_save_triggered = pyqtSignal()

    # 保存完成信号（参数：是否成功）
    save_completed = pyqtSignal(bool)

    # 全局 dirty 状态变更信号（任何页面 dirty 变化）
    global_dirty_changed = pyqtSignal(bool)

    # ==================== 构造函数 ====================

    def __init__(self, parent: QObject | None = None) -> None:
        """
        初始化 ToposProjectController。

        Args:
            parent: 父 QObject（可选）
        """
        super().__init__(parent)

        # AUD-37009: guards _page_dirty_states / _last_save_timestamp.
        # Reentrant so signal slots (direct connection) may call back into
        # the controller while a transition holds the lock.
        self._state_lock = threading.RLock()

        # 页面 dirty 状态字典
        self._page_dirty_states: dict[PageType, DirtyState] = {
            PageType.LIBRARY: DirtyState(page=PageType.LIBRARY, is_dirty=False),
            PageType.EDIT: DirtyState(page=PageType.EDIT, is_dirty=False),
            PageType.COLOR: DirtyState(page=PageType.COLOR, is_dirty=False),
            PageType.COMPOSITE: DirtyState(page=PageType.COMPOSITE, is_dirty=False),
            PageType.DELIVER: DirtyState(page=PageType.DELIVER, is_dirty=False),
        }

        # 保存回调函数
        self._save_callback: Callable[[], bool] | None = None

        # 自动保存定时器
        self._auto_save_timer: QTimer | None = None
        self._auto_save_interval_ms: int = 5 * 60 * 1000  # 默认 5 分钟

        # 上次保存时间戳
        self._last_save_timestamp: float = 0.0

        # 自动保存是否启用
        self._auto_save_enabled: bool = False

        logger.info("ToposProjectController initialized")

    # ==================== Dirty 状态管理 ====================

    def is_dirty(self) -> bool:
        """
        检查是否有任何页面 dirty。

        Returns:
            True 如果任何页面有未保存的更改
        """
        with self._state_lock:
            return any(state.is_dirty for state in self._page_dirty_states.values())

    def is_page_dirty(self, page: PageType) -> bool:
        """
        检查指定页面是否 dirty。

        Args:
            page: 页面类型

        Returns:
            True 如果该页面有未保存的更改
        """
        with self._state_lock:
            state = self._page_dirty_states.get(page)
            return state.is_dirty if state else False

    def mark_dirty(self, page: PageType, reason: str | None = None) -> None:
        """
        标记页面 dirty。

        Args:
            page: 页面类型
            reason: dirty 原因（可选，用于日志和调试）
        """
        with self._state_lock:
            old_state = self._page_dirty_states.get(page)
            if old_state and old_state.is_dirty:
                # 已经 dirty，更新原因和时间戳
                new_state = DirtyState(
                    page=page,
                    is_dirty=True,
                    reason=reason,
                    timestamp=time.time(),
                )
                self._page_dirty_states[page] = new_state
                logger.debug(f"Page {page.value} already dirty, updated: {reason}")
                return

            # 创建新的 dirty 状态
            new_state = DirtyState(
                page=page,
                is_dirty=True,
                reason=reason,
                timestamp=time.time(),
            )
            self._page_dirty_states[page] = new_state

            # 发射状态变更信号
            event = DirtyStateChanged(
                page=page,
                old_state=False,
                new_state=True,
                reason=reason,
            )
            self.dirty_changed.emit(event)

            # 发射全局 dirty 变化信号
            self.global_dirty_changed.emit(True)

        logger.info(f"Page {page.value} marked dirty: {reason}")

    def clear_dirty(self, page: PageType) -> None:
        """
        清除页面 dirty 状态。

        Args:
            page: 页面类型
        """
        with self._state_lock:
            old_state = self._page_dirty_states.get(page)
            if not old_state or not old_state.is_dirty:
                # 已经是 clean 状态
                return

            # 创建新的 clean 状态
            new_state = DirtyState(
                page=page,
                is_dirty=False,
                reason=None,
                timestamp=time.time(),
            )
            self._page_dirty_states[page] = new_state

            # 发射状态变更信号
            event = DirtyStateChanged(
                page=page,
                old_state=True,
                new_state=False,
                reason="saved",
            )
            self.dirty_changed.emit(event)

            # 检查是否所有页面都 clean，发射全局 dirty 变化信号
            if not self.is_dirty():
                self.global_dirty_changed.emit(False)

        logger.info(f"Page {page.value} cleared dirty")

    def clear_all_dirty(self, reason: str = "saved") -> None:
        """
        清除所有页面的 dirty 状态。

        Args:
            reason: 清除原因（默认 "saved"）
        """
        cleared_pages: list[PageType] = []

        with self._state_lock:
            for page, old_state in self._page_dirty_states.items():
                if old_state.is_dirty:
                    new_state = DirtyState(
                        page=page,
                        is_dirty=False,
                        reason=None,
                        timestamp=time.time(),
                    )
                    self._page_dirty_states[page] = new_state

                    event = DirtyStateChanged(
                        page=page,
                        old_state=True,
                        new_state=False,
                        reason=reason,
                    )
                    self.dirty_changed.emit(event)
                    cleared_pages.append(page)

            if cleared_pages:
                self.global_dirty_changed.emit(False)

        if cleared_pages:
            logger.info(f"All dirty pages cleared: {[p.value for p in cleared_pages]}")

    def get_dirty_pages(self) -> list[PageType]:
        """
        获取所有 dirty 的页面列表。

        Returns:
            dirty 页面列表
        """
        with self._state_lock:
            return [page for page, state in self._page_dirty_states.items() if state.is_dirty]

    def get_dirty_state(self, page: PageType) -> DirtyState | None:
        """
        获取页面的完整 dirty 状态。

        Args:
            page: 页面类型

        Returns:
            DirtyState 实例或 None
        """
        with self._state_lock:
            return self._page_dirty_states.get(page)

    def get_all_dirty_states(self) -> dict[PageType, DirtyState]:
        """
        获取所有页面的 dirty 状态字典。

        Returns:
            页面 dirty 状态字典
        """
        with self._state_lock:
            return dict(self._page_dirty_states)

    # ==================== 保存协调 ====================

    def set_save_callback(self, callback: Callable[[], bool]) -> None:
        """
        设置保存回调函数。

        Args:
            callback: 保存函数，返回 True 表示成功
        """
        self._save_callback = callback
        logger.debug("Save callback set")

    def save(self) -> bool:
        """
        执行保存操作。

        调用保存回调，成功后清除所有 dirty 状态。

        Returns:
            True 如果保存成功
        """
        # AUD-37009: state check under the lock; the (potentially slow)
        # save callback runs WITHOUT holding the lock so concurrent
        # dirty tracking is not blocked for the duration of the IO.
        with self._state_lock:
            if self._save_callback is None:
                logger.warning("No save callback set")
                return False

            # 检查是否有 dirty 状态
            if not self.is_dirty():
                logger.debug("No dirty state, skip save")
                return True

        logger.info("Executing save...")

        try:
            success = self._save_callback()
        except Exception as e:
            logger.error(f"Save exception: {e}")
            self.save_completed.emit(False)
            return False

        if success:
            with self._state_lock:
                # 清除所有 dirty 状态
                self.clear_all_dirty(reason="saved")

                # 更新保存时间戳
                self._last_save_timestamp = time.time()

            # 发射保存完成信号
            self.save_completed.emit(True)

            logger.info("Save completed successfully")
            return True
        else:
            # 保存失败，保留 dirty 状态
            self.save_completed.emit(False)
            logger.warning("Save failed, dirty state preserved")
            return False

    def save_page(self, page: PageType) -> bool:
        """
        保存指定页面。

        回调契约（AUD-37012）：当前保存回调是全量保存 —— ``save()`` 成功后
        清除*所有*页面的 dirty 状态（见 ``clear_all_dirty``）。因此本方法
        仅把「该页是否 dirty」用作短路门槛，实际执行一次全量保存；不存在
        单独清除该页 dirty 的分支。若未来引入页面级持久化，回调需接收
        page 参数并只清除对应页面的 dirty。

        Args:
            page: 页面类型

        Returns:
            True 如果保存成功
        """
        if not self.is_page_dirty(page):
            logger.debug(f"Page {page.value} not dirty, skip save")
            return True

        return self.save()

    # ==================== 自动保存机制 ====================

    def start_auto_save(self, interval_minutes: int = DEFAULT_AUTO_SAVE_INTERVAL_MINUTES) -> None:
        """
        启动自动保存。

        Args:
            interval_minutes: 保存间隔（分钟），必须 >= 1

        Raises:
            ValueError: interval_minutes 非整数或低于下界（AUD-37008，
                防止 0 间隔定时器在每个事件循环 tick 触发保存风暴）
        """
        interval_minutes = _validate_auto_save_interval_minutes(interval_minutes)
        self._auto_save_interval_ms = interval_minutes * 60 * 1000
        self._auto_save_enabled = True

        if self._auto_save_timer is None:
            self._auto_save_timer = QTimer(self)
            self._auto_save_timer.timeout.connect(self._on_auto_save_timer)

        self._auto_save_timer.start(self._auto_save_interval_ms)
        logger.info(f"Auto-save started, interval: {interval_minutes} minutes")

    def stop_auto_save(self) -> None:
        """
        停止自动保存。
        """
        self._auto_save_enabled = False

        if self._auto_save_timer is not None:
            self._auto_save_timer.stop()

        logger.info("Auto-save stopped")

    def set_auto_save_interval(self, interval_minutes: int) -> None:
        """
        设置自动保存间隔。

        Args:
            interval_minutes: 保存间隔（分钟），必须 >= 1

        Raises:
            ValueError: interval_minutes 非整数或低于下界（AUD-37008）
        """
        interval_minutes = _validate_auto_save_interval_minutes(interval_minutes)
        self._auto_save_interval_ms = interval_minutes * 60 * 1000

        if self._auto_save_timer is not None and self._auto_save_enabled:
            self._auto_save_timer.setInterval(self._auto_save_interval_ms)

        logger.debug(f"Auto-save interval set: {interval_minutes} minutes")

    def is_auto_save_enabled(self) -> bool:
        """
        检查自动保存是否启用。

        Returns:
            True 如果自动保存已启用
        """
        return self._auto_save_enabled

    def get_last_save_timestamp(self) -> float:
        """
        获取上次保存时间戳。

        Returns:
            时间戳（秒）
        """
        with self._state_lock:
            return self._last_save_timestamp

    def _on_auto_save_timer(self) -> None:
        """
        自动保存定时器回调。

        仅在 dirty 时触发保存。

        AUD-37010: 信号与保存二选一 —— 存在 ``auto_save_triggered`` 消费者
        时只广播触发信号，由消费者执行保存（信号存在的自然意图就是让消费
        端接管，避免"一次触发两次保存"）；无人监听时兜底执行同步 ``save()``，
        自动保存不会因为没人接信号而静默失效。
        """
        if not self.is_dirty():
            logger.debug("Auto-save timer: no dirty state, skip")
            return

        logger.info("Auto-save timer triggered, dirty state detected")

        if self.receivers(self.auto_save_triggered) > 0:
            # 发射自动保存触发信号（消费者负责保存）
            self.auto_save_triggered.emit()
        else:
            # 无消费者：兜底执行保存
            self.save()

    # ==================== 状态序列化 ====================

    def to_dict(self) -> dict[str, Any]:
        """
        序列化状态为字典。

        用于保存控制器状态到配置或工程文件。

        Returns:
            状态字典
        """
        with self._state_lock:
            return {
                "page_dirty_states": {
                    page.value: {
                        "is_dirty": state.is_dirty,
                        "reason": state.reason,
                        "timestamp": state.timestamp,
                    }
                    for page, state in self._page_dirty_states.items()
                },
                "auto_save_enabled": self._auto_save_enabled,
                "auto_save_interval_ms": self._auto_save_interval_ms,
                "last_save_timestamp": self._last_save_timestamp,
            }

    def from_dict(self, data: dict[str, Any]) -> None:
        """
        从字典恢复状态。

        Args:
            data: 状态字典
        """
        with self._state_lock:
            page_states = data.get("page_dirty_states", {})
            for page_str, state_data in page_states.items():
                try:
                    page = PageType(page_str)
                    self._page_dirty_states[page] = DirtyState(
                        page=page,
                        is_dirty=state_data.get("is_dirty", False),
                        reason=state_data.get("reason"),
                        timestamp=state_data.get("timestamp", 0.0),
                    )
                except ValueError:
                    logger.warning(f"Unknown page type: {page_str}")

            self._auto_save_enabled = data.get("auto_save_enabled", False)
            # AUD-37008: persisted values are untrusted external data — coerce to
            # int and clamp to the minimum interval before any timer is started.
            self._auto_save_interval_ms = _clamp_auto_save_interval_ms(
                data.get("auto_save_interval_ms", 5 * 60 * 1000)
            )
            self._last_save_timestamp = data.get("last_save_timestamp", 0.0)

            # 如果启用了自动保存，启动定时器
            if self._auto_save_enabled:
                self.start_auto_save(self._auto_save_interval_ms // 60000)

    # ==================== 辅助方法 ====================

    def reset(self) -> None:
        """
        重置所有状态。

        清除所有 dirty 状态，停止自动保存。
        """
        self.clear_all_dirty(reason="reset")
        self.stop_auto_save()
        with self._state_lock:
            self._last_save_timestamp = 0.0
        logger.info("ToposProjectController reset")

    def get_dirty_summary(self) -> str:
        """
        获取 dirty 状态摘要。

        Returns:
            状态摘要字符串
        """
        with self._state_lock:
            dirty_pages = self.get_dirty_pages()
            if not dirty_pages:
                return "No unsaved changes"

            summaries: list[str] = []
            for page in dirty_pages:
                state = self._page_dirty_states[page]
                reason = state.reason or "unknown"
                summaries.append(f"{page.value}: {reason}")

        return "Unsaved: " + ", ".join(summaries)