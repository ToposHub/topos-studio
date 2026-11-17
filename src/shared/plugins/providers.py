"""F8：格式 provider 的真实执行边界——owner/优先级/冲突解释/缓存签名/
可回退错误类型 + Inspector 异步装配与世代失效。

manifest 已声明 ``FormatProviderDeclaration``（priority/formats/magic/
provides/cache_signature，PL-05），但没有运行时：注册无归属、多插件撞
型无解释、缓存无失效键、执行错误不分"可回退"类别（失败策略要求只有
明确错误类别才许回退，权限/不支持不得绕道另一后端）。

本模块的口径：

* **owner 归属**：注册携带 plugin_id；宿主停用/退役时
  ``unregister_owner`` 整体摘除（接 F6 生命周期）；
* **优先级与冲突解释**：同型竞争按 (priority, provider_id) 确定性排序，
  落选者以 ``ProviderConflict`` 可见（谁赢/为什么/差在哪）——不静默；
* **缓存签名**：``cache_key`` = 注册表 revision + 候选们的
  cache_signature——任一候选增删/改签名即失效；
* **错误类别**：``ProviderError.kind`` ∈ unsupported（不回退，终态）/
  transient（同 provider 重试）/ fallback（本 provider 失败，允许按序
  回退下一个）/ cancelled。**只有 fallback 类回退**——权限或不支持类
  错误绝不绕道其他 provider（失败策略原文）；
* **Inspector**：异步探测目标可由哪些 provider 处理；世代失效（晚到的
  旧代装配结果丢弃，不污染新请求）；transient 每 provider 有界重试一次。
"""
from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from src.shared.plugins.manifest import FORMAT_PROVIDES, FormatProviderDeclaration

PROVIDER_ERROR_KINDS = ("unsupported", "transient", "fallback", "cancelled")


def _normalize_target(target: str) -> str:
    """目标归一：接受扩展名（可带点）或文件名 → 小写裸扩展名。

    magic 前缀（不含点）原样通过；``MOV``/``.mov``/``clip.MOV`` 同义。
    """
    value = (target or "").lower().strip().lstrip(".")
    if "." in value:
        value = value.rsplit(".", 1)[-1]
    return value


class ProviderError(Exception):
    """provider 执行错误；``kind`` 决定回退语义（只有 fallback 回退）。"""

    def __init__(self, kind: str, message: str = "") -> None:
        if kind not in PROVIDER_ERROR_KINDS:
            raise ValueError(f"未知 provider 错误类别：{kind}")
        super().__init__(message or kind)
        self.kind = kind
        self.message = message or kind


@dataclass(frozen=True)
class ProviderConflict:
    """一次解析中每个落选者的可见解释。"""

    provider_id: str
    owner: str
    provides: str
    reason: str          # 为什么落选（优先级低/owner 已摘除/…）


@dataclass(frozen=True)
class _Entry:
    owner: str
    declaration: FormatProviderDeclaration
    executor: Callable[[str, str], Any]
    generation: int = 0


@dataclass(frozen=True)
class Resolution:
    """一次 (provides, 目标) 的解析结果：胜者 + 全部冲突解释。"""

    provides: str
    target: str                       # 扩展名（无点）或 magic 前缀
    provider_id: str
    owner: str
    conflicts: tuple[ProviderConflict, ...] = ()
    cache_key: str = ""


class ProviderRegistry:
    """owner 归属的 provider 注册表；确定性优先级 + 冲突解释 + 缓存签名。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: list[_Entry] = []
        self._revision = 0
        self._serial = itertools.count(1)

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    # ------------------------------------------------------------ 注册
    def register(self, owner: str, declaration: FormatProviderDeclaration,
                 executor: Callable[[str, str], Any]) -> None:
        """登记一个 provider（owner=plugin_id；同 owner 同 id 覆盖旧代）。"""
        with self._lock:
            self._entries = [
                entry for entry in self._entries
                if not (entry.owner == owner
                        and entry.declaration.provider_id == declaration.provider_id)]
            self._entries.append(_Entry(owner, declaration, executor,
                                        generation=next(self._serial)))
            self._revision += 1

    def unregister_owner(self, owner: str) -> int:
        """宿主停用/退役：整体摘除该 owner 的 provider（F6 接线点）。"""
        with self._lock:
            remaining = [entry for entry in self._entries if entry.owner != owner]
            removed = len(self._entries) - len(remaining)
            if removed:
                self._entries = remaining
                self._revision += 1
            return removed

    def owners(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted({entry.owner for entry in self._entries}))

    # ------------------------------------------------------------ 解析
    def _candidates(self, provides: str, target: str) -> list[_Entry]:
        target = _normalize_target(target)
        matched = []
        for entry in self._entries:
            declaration = entry.declaration
            if provides not in declaration.provides:
                continue
            formats = {fmt.lower().lstrip(".") for fmt in declaration.formats}
            magics = {magic.lower() for magic in declaration.magic}
            if target in formats or target in magics or not formats and not magics:
                matched.append(entry)
        # 确定性：priority 升序（小者优先），同优先级按注册代数（先来先服务）
        return sorted(matched, key=lambda entry: (entry.declaration.priority,
                                                  entry.generation))

    def resolve(self, provides: str, target: str) -> Resolution | None:
        """解析胜者 + 冲突解释 + 缓存签名；无候选返回 None。

        ``provides`` 不在声明集内直接 ValueError（调用方拼写错误不是
        "无 provider"）。
        """
        if provides not in FORMAT_PROVIDES:
            raise ValueError(f"未知 provides 类别：{provides}")
        with self._lock:
            candidates = self._candidates(provides, target)
            if not candidates:
                return None
            winner = candidates[0]
            conflicts = tuple(
                ProviderConflict(
                    provider_id=entry.declaration.provider_id,
                    owner=entry.owner,
                    provides=provides,
                    reason=(f"优先级 {entry.declaration.priority} 低于胜者 "
                            f"{winner.declaration.provider_id} 的 "
                            f"{winner.declaration.priority}"))
                for entry in candidates[1:])
            signatures = "|".join(
                f"{entry.owner}:{entry.declaration.provider_id}:"
                f"{entry.declaration.cache_signature or entry.declaration.version}"
                for entry in candidates)
            return Resolution(
                provides=provides, target=_normalize_target(target),
                provider_id=winner.declaration.provider_id,
                owner=winner.owner, conflicts=conflicts,
                cache_key=f"r{self._revision}:{signatures}")

    def run(self, provides: str, target: str,
            *, retry_transient: bool = True) -> tuple[Any, Resolution | None]:
        """按解析顺序执行；**只有 fallback 类错误回退**到下一个候选。

        unsupported = 终态（没有 provider 能处理，绝不绕道）；transient
        = 同 provider 重试一次；cancelled = 尊重取消，不回退。全部候选
        以 fallback 失败时抛出最后一个 fallback 错误。
        """
        if provides not in FORMAT_PROVIDES:
            raise ValueError(f"未知 provides 类别：{provides}")
        with self._lock:
            candidates = list(self._candidates(provides, target))
            signatures = "|".join(
                f"{entry.owner}:{entry.declaration.provider_id}:"
                f"{entry.declaration.cache_signature or entry.declaration.version}"
                for entry in candidates)
            resolution = (Resolution(
                provides=provides, target=_normalize_target(target),
                provider_id=candidates[0].declaration.provider_id,
                owner=candidates[0].owner,
                conflicts=tuple(ProviderConflict(
                    provider_id=e.declaration.provider_id, owner=e.owner,
                    provides=provides, reason="按序回退候选")
                    for e in candidates[1:]),
                cache_key=f"r{self._revision}:{signatures}")
                if candidates else None)
        last_error: ProviderError | None = None
        for entry in candidates:
            attempts = 2 if retry_transient else 1
            for attempt in range(attempts):
                try:
                    return entry.executor(provides, target), resolution
                except ProviderError as exc:
                    if exc.kind == "cancelled":
                        raise
                    if exc.kind == "transient" and attempt == 0:
                        continue          # 同 provider 有界重试一次
                    if exc.kind in ("fallback", "transient"):
                        # fallback：换下一个候选；重试后仍 transient 同理
                        # （该 provider 实际不可用，按序回退）
                        last_error = exc
                        break
                    raise                 # unsupported：终态，不绕道
        if last_error is not None:
            raise last_error
        raise ProviderError("unsupported", f"没有 provider 声明 {provides}: {target}")


class ProviderInspector:
    """异步装配：目标可由哪些 provider 处理；世代失效 + 有界重试。"""

    def __init__(self, registry: ProviderRegistry,
                 executor_provider: Callable[[], Any] | None = None) -> None:
        self._registry = registry
        self._lock = threading.Lock()
        self._generation = 0
        self._spawn = executor_provider or _default_worker

    def inspect(self, target: str, *,
                on_result: Callable[[int, list[dict[str, Any]]], None]) -> int:
        """发起异步装配；返回世代号。晚到的旧代结果**不回调**（丢弃）。

        ``on_result(generation, entries)`` 在 worker 线程触发——Qt 宿主
        自行 marshal；世代号供调用方比对（双保险）。
        """
        with self._lock:
            self._generation += 1
            generation = self._generation

        def work() -> None:
            entries: list[dict[str, Any]] = []
            seen: set[tuple[str, str]] = set()
            with self._registry._lock:   # 快照候选（不持锁执行）
                candidates = [
                    (provides, self._registry._candidates(provides, target))
                    for provides in FORMAT_PROVIDES]
            for provides, entries_for_kind in candidates:
                for entry in entries_for_kind:
                    key = (provides, entry.declaration.provider_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    record: dict[str, Any] = {
                        "provides": provides,
                        "provider_id": entry.declaration.provider_id,
                        "owner": entry.owner,
                        "priority": entry.declaration.priority,
                    }
                    # 能力探测：transient 重试一次；其余错误如实记录
                    try:
                        attempts = 2
                        for attempt in range(attempts):
                            try:
                                entry.executor(provides, target)
                                record["available"] = True
                                break
                            except ProviderError as exc:
                                if exc.kind == "transient" and attempt == 0:
                                    continue
                                raise
                    except ProviderError as exc:
                        record["available"] = False
                        record["error_kind"] = exc.kind
                        record["error"] = exc.message
                    except Exception as exc:  # noqa: BLE001 - 装配不因单 provider 崩
                        record["available"] = False
                        record["error_kind"] = "fallback"
                        record["error"] = f"{type(exc).__name__}: {exc}"
                    entries.append(record)
            with self._lock:
                stale = generation != self._generation
            if stale:
                return                   # 世代失效：晚到的旧代结果丢弃
            on_result(generation, entries)

        self._spawn(work)
        return generation

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation


def _default_worker(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True, name="provider-inspector").start()


__all__ = [
    "ProviderError", "ProviderConflict", "Resolution", "ProviderRegistry",
    "ProviderInspector", "FORMAT_PROVIDES",
]
