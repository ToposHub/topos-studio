"""R06-B: bake 结果资源会话（内容寻址 + R09 工程闭包）。

结构与语义照 ``src/shared/color/lut_resource_registry.py``（R02/R09 已
验收的范本）：

- **进程单例** ``get_bake_result_session()``：staging 目录 + 原子落盘
  （temp + ``os.replace``，失败无半条目）+ 工程加载/关闭生命周期。
- **内容寻址**：``bake_<sha32>``——同一字节结果跨任务去重；元数据携带
  spec/双 revision/栈/后端（版本化可追溯）。
- **R09 闭包**：``collect_graph_bake_closure`` 递归遍历（含 compound
  children）全部 ``params.resources.bake_ref``；
  ``ensure_graph_bake_closure`` 保存前把图中引用全部落进包索引——缺失
  不静默移除、不阻断保存（missing 列表交预检/浏览器显示可修复状态）。
- **释放路径**：``release(uri)`` 删除暂存文件（取消/失败/过期丢弃）；
  ``reset()`` 关工程清空（目录级释放）。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading
from typing import Any, Iterable

from src.shared.project.resource_resolver import ResourceEntry


def bake_resource_id(sha256_hex: str) -> str:
    return f"bake_{sha256_hex[:32]}"


class BakeResultSession:
    """bake 结果的暂存/注册/持久化（RLock；落盘可由后台线程执行）。"""

    KIND = "generated"
    ROLES = ("bake",)

    def __init__(self, staging_root: str | None = None) -> None:
        self._lock = threading.RLock()
        self._staging_root = staging_root or tempfile.mkdtemp(
            prefix="topos-bake-results-")
        self._entries: dict[str, ResourceEntry] = {}
        self._staged_sources: dict[str, str] = {}

    # ── 工程生命周期 ───────────────────────────────────────────────
    def bind_project(self, resolver: Any) -> None:
        """工程加载：合并包内资源索引（全部 kind，防保存丢条目）。"""
        with self._lock:
            self._clear_staging()
            self._entries.clear()
            index = getattr(resolver, "index", None) if resolver is not None else None
            if index is not None:
                for entry in getattr(index, "entries", {}).values():
                    if isinstance(entry, ResourceEntry):
                        self._entries[entry.resource_id] = entry

    def reset(self) -> None:
        """工程关闭/换会话：清条目并释放暂存目录（结果文件生命周期终点）。"""
        with self._lock:
            self._entries.clear()
            self._clear_staging()

    def _clear_staging(self) -> None:
        self._staged_sources.clear()
        shutil.rmtree(self._staging_root, ignore_errors=True)
        try:
            os.makedirs(self._staging_root, exist_ok=True)
        except OSError:
            pass

    # ── 注册（worker 成功后由 service 调用） ──────────────────────
    def register_result(self, payload: bytes, format_hint: str,
                        metadata: dict[str, Any]) -> ResourceEntry:
        """内容寻址注册：原子暂存 → 条目（重复字节幂等返回既有条目）。"""
        sha = hashlib.sha256(payload).hexdigest()
        with self._lock:
            existing = self._by_sha(sha)
            if existing is not None:
                return existing
            os.makedirs(self._staging_root, exist_ok=True)
            suffix = f".{format_hint or 'bin'}"
            destination = os.path.join(self._staging_root, f"{sha}{suffix}")
            if not os.path.exists(destination):
                fd, temp = tempfile.mkstemp(dir=self._staging_root,
                                            suffix=".part")
                try:
                    with os.fdopen(fd, "wb") as target:
                        target.write(payload)
                    os.replace(temp, destination)
                except OSError:
                    try:
                        os.unlink(temp)
                    except OSError:
                        pass
                    raise
            archive_path = f"resources/bake/{sha}{suffix}"
            entry = ResourceEntry(
                resource_id=bake_resource_id(sha),
                kind=self.KIND,
                archive_path=archive_path,
                size_bytes=len(payload),
                sha256=sha,
                original_filename=f"bake-{sha[:12]}{suffix}",
                mime="application/x-topos-bake+npz"
                if format_hint == "npz" else "application/octet-stream",
                roles=self.ROLES,
                metadata=dict(metadata))
            self._entries[entry.resource_id] = entry
            self._staged_sources[entry.archive_path] = destination
            return entry

    def _by_sha(self, sha256_hex: str) -> ResourceEntry | None:
        for entry in self._entries.values():
            if entry.sha256 == sha256_hex:
                return entry
        return None

    # ── 查询/释放/持久化 ───────────────────────────────────────────
    def entries(self) -> list[ResourceEntry]:
        with self._lock:
            return [self._entries[rid] for rid in sorted(self._entries)]

    def status_of(self, reference: str) -> str:
        """引用状态：registered / unregistered（fail-closed 投影）。"""
        rid = _resource_id_of(reference)
        with self._lock:
            entry = self._entries.get(rid)
        if entry is None:
            return "unregistered"
        if entry.archive_path in self._staged_sources:
            local = self._staged_sources[entry.archive_path]
            if not os.path.exists(local):
                return "missing"
        return "registered"

    def release(self, reference: str) -> bool:
        """释放单条暂存结果文件（取消/失败/过期丢弃路径）。"""
        rid = _resource_id_of(reference)
        with self._lock:
            entry = self._entries.get(rid)
            if entry is None:
                return False
            local = self._staged_sources.pop(entry.archive_path, None)
            self._entries.pop(rid, None)
        if local:
            try:
                os.unlink(local)
            except OSError:
                pass
        return True

    def save_payload(self) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
        """生成包载荷：资源索引 + 新增文件源（archive_path → 本机路径）。

        索引形状与 LUT 会话一致（``ResourceIndex.to_dict``——resources
        为条目列表，load 侧 ``from_dict`` 才能还原）。
        """
        from src.shared.project.resource_resolver import ResourceIndex
        with self._lock:
            if not self._entries:
                return None, None
            index = ResourceIndex(entries=dict(self._entries))
            return index.to_dict(), (dict(self._staged_sources) or None)


def _resource_id_of(reference: str) -> str:
    """topos-res://<id> 或裸 id → id。"""
    if reference.startswith("topos-res://"):
        return reference[len("topos-res://"):]
    return reference


# ── R09 图闭包 ────────────────────────────────────────────────────

def collect_graph_bake_closure(graph: Any) -> list[str]:
    """递归收集图内全部 bake 引用（含 compound children）。"""

    from collections.abc import Mapping as _Mapping

    def visit(container: Any) -> None:
        for node in getattr(container, "nodes", ()) or ():
            params = getattr(node, "params", None) or {}
            resources = params.get("resources") \
                if isinstance(params, _Mapping) else None
            if isinstance(resources, _Mapping):
                ref = resources.get("bake_ref")
                if isinstance(ref, str) and ref:
                    found.add(ref)
            children = getattr(node, "compound_children", None)
            if children is not None:
                visit(children)

    found: set[str] = set()
    visit(graph)
    return sorted(found)


def ensure_graph_bake_closure(
    graphs: Iterable[Any], session: BakeResultSession,
) -> dict[str, Any]:
    """保存前确保图中 bake 引用全部在会话内（缺失保留引用，不静默删）。"""
    refs: set[str] = set()
    for graph in graphs:
        refs.update(collect_graph_bake_closure(graph))
    missing: list[str] = []
    for ref in sorted(refs):
        if session.status_of(ref) == "unregistered":
            missing.append(ref)
    return {"refs": sorted(refs), "missing": missing}


_session: BakeResultSession | None = None
_session_lock = threading.Lock()


def get_bake_result_session() -> BakeResultSession:
    """进程级单例（宿主装配与测试注入共用）。"""
    global _session
    with _session_lock:
        if _session is None:
            _session = BakeResultSession()
        return _session
