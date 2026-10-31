"""GL 资源登记表（计划 P3-03）。

按资源组记录 GPU 资源的所有权归属，供诊断报告与 Phase 4+ backend 切换
（计划 §4.2 quiesce → release → rebuild 流程）使用。

线程契约（``.clinerules``：UI 线程绝不调用 GL）：

- ``register`` / ``unregister`` / ``snapshot`` 可在任意线程调用（内部有锁）。
- ``release_group`` / ``release_all`` 本身不做任何 GL 调用，只是回调登记时
  传入的 ``release_fn``。凡带 ``release_fn`` 的资源，其释放回调必须在拥有
  GL context 的渲染线程上执行——调用方负责线程正确性，登记表不做 GL 检查。
- ``release_fn=None`` 表示纯台账模式：资源由既有清理路径释放，登记表只
  记账（这是 P3-02/03 阶段 AsyncRenderer 的默认用法，行为不变约束）。

Deferred-release 协议（backend 切换用，计划 §4.2 quiesce → release →
rebuild）：

1. 资源创建时（渲染线程）``register`` 登记，可选传入 ``release_fn``；
   登记动作本身不释放任何东西。
2. 运行期资源正常轮转由既有路径负责， ``unregister`` 同步销账。
3. 仅当 backend 需要 quiesce/销毁时，才在**拥有 GL context 的渲染线程**
   上调用 ``release_group`` / ``release_all``，触发迟来的（deferred）
   ``release_fn`` 回调——即「注册时登记、切换时统一延迟释放」。
4. 回调按组内 LIFO 逆序执行（后创建的先释放，匹配 GL 依赖方向）。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# 计划 P3-03 点名的五个规范资源组。
GROUP_UPLOAD = "upload"
GROUP_FRAMEBUFFERS = "framebuffers"
GROUP_SHADERS = "shaders"
GROUP_SCOPE = "scope"
GROUP_DENOISE = "denoise"

# AsyncRenderer 生命周期内同样存在的辅助资源组（登记用于完整诊断）。
GROUP_PRESENT = "present"
GROUP_GRADING = "grading"
GROUP_MATTE = "matte"
GROUP_SCRATCH = "scratch"


@dataclass(frozen=True)
class ResourceRecord:
    """单个已登记资源的台账条目。"""

    group: str
    name: str
    owner: str
    detail: str = ""
    # 释放回调；None 表示纯台账（释放由登记表之外的既有路径负责）。
    release_fn: Optional[Callable[[], None]] = field(
        default=None, compare=False, repr=False
    )


class GLResourceRegistry:
    """记录「哪个 backend 拥有哪些 GL 资源」的台账，不做任何 GL 调用。"""

    def __init__(self, owner: str) -> None:
        """创建台账。

        Args:
            owner: 拥有者标识（backend id 或 renderer 名），写入每条
                ``ResourceRecord.owner`` 供跨 backend 诊断区分。
        """
        self._owner = owner
        self._lock = threading.Lock()
        self._records: Dict[str, Dict[str, ResourceRecord]] = {}

    @property
    def owner(self) -> str:
        """台账拥有者标识（构造时指定，不可变）。"""
        return self._owner

    def register(
        self,
        group: str,
        name: str,
        resource: Any = None,
        *,
        release_fn: Optional[Callable[[], None]] = None,
        detail: str = "",
    ) -> None:
        """登记（或覆盖登记）一个资源。同组同名以最后一次登记为准。"""
        if not group or not name:
            raise ValueError("register 需要 group 与 name")
        if detail == "" and resource is not None:
            detail = type(resource).__name__
        record = ResourceRecord(
            group=group,
            name=name,
            owner=self._owner,
            detail=detail,
            release_fn=release_fn,
        )
        with self._lock:
            self._records.setdefault(group, {})[name] = record

    def unregister(self, group: str, name: str) -> bool:
        """移除一条登记（资源已由既有路径释放时保持台账真实）。"""
        with self._lock:
            group_records = self._records.get(group)
            if group_records is None or name not in group_records:
                return False
            del group_records[name]
            if not group_records:
                del self._records[group]
            return True

    def get(self, group: str, name: str) -> Optional[ResourceRecord]:
        """查询单条登记；不存在返回 ``None``（任意线程可调）。"""
        with self._lock:
            return self._records.get(group, {}).get(name)

    def has_group(self, group: str) -> bool:
        """该组当前是否存在至少一条登记（任意线程可调）。"""
        with self._lock:
            return bool(self._records.get(group))

    def snapshot(self) -> Dict[str, List[str]]:
        """返回 {group: [name, ...]} 的只读快照（诊断报告用）。"""
        with self._lock:
            return {
                group: sorted(records)
                for group, records in self._records.items()
            }

    def record_count(self) -> int:
        """全部组的登记条目总数（任意线程可调）。"""
        with self._lock:
            return sum(len(records) for records in self._records.values())

    def release_group(self, group: str) -> List[str]:
        """释放一组资源：LIFO 顺序回调 release_fn，随后移除登记。

        返回实际触发过 release_fn 的资源名。release_fn=None 的条目只移除
        登记（纯台账）。回调异常被捕获并记日志，不中断同组其余释放；
        失败的条目会**回插**进台账（AUD-1300）——回调抛异常意味着资源
        在 GL 层并未真正释放，若登记也一并消失，泄漏资源就既无法重试
        也无法经 snapshot/release_all 诊断。重试时该组以回插顺序再次
        LIFO 释放。
        """
        with self._lock:
            group_records = self._records.pop(group, {})
        released: List[str] = []
        for name, record in reversed(list(group_records.items())):
            if record.release_fn is None:
                continue
            try:
                record.release_fn()
                released.append(name)
            except Exception:
                logger.exception(
                    "GL 资源释放回调失败：%s/%s（owner=%s，登记已保留待重试）",
                    group,
                    name,
                    self._owner,
                )
                with self._lock:
                    # 回调失败 ⇒ 资源未真正释放：条目必须留在台账里，
                    # 否则 deferred-release 同时失去释放与可观测性。
                    self._records.setdefault(group, {})[name] = record
        return released

    def release_all(self) -> Dict[str, int]:
        """按组释放全部资源，返回 {group: 触发过 release_fn 的条数}。

        回调失败的条目保留在台账中（见 ``release_group``）；再次调用
        ``release_all`` 可对其重试。
        """
        with self._lock:
            groups = list(self._records.keys())
        counts: Dict[str, int] = {}
        for group in groups:
            counts[group] = len(self.release_group(group))
        return counts

    def clear(self) -> None:
        """清空台账但不触发任何 release_fn（资源已由既有路径释放时用）。"""
        with self._lock:
            self._records.clear()
