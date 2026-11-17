"""Asynchronous UI transport for the native Files panel.

D1 换轨：目录/树/搜索/集合/元数据/任务/库/扫描/批改名走 **C2 FilesFacade**
（同进程、有界 worker、代际作废），预览/比较为本地直读；这些路由不再依赖
旧 HTTP 服务。缩略图/胶片条（等 E 阶段媒体 provider）与插件四窗口拉起
（等 F/G 接管）暂保留旧 HTTP 桥。

信号契约与本类的公共方法签名保持不变；面板/控制器零改动。回调在 facade
worker 线程触发信号，Qt 按接收者亲和自动队列到 GUI 线程。
"""
from __future__ import annotations

import json
import os
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from src.features.files.contracts import (
    FilesErrorCode,
    QueryRequest,
    identity_key,
)
from src.features.files.repository import FilesDataLocator
from src.features.files.repository.catalog import FilesCatalog, nfc
from src.features.files.services.facade import FilesFacade, read_file_bytes
from src.features.files.transactions import CancelToken
from src.shared.plugins.services import ItemResult


@dataclass
class _MetadataJob:
    """一次元数据写入的请求身份；逐路径结果汇总后才发布成功/失败。"""

    paths: list[str]
    patch: dict[str, Any]
    failures: list[str] = field(default_factory=list)
    # 服务落库后的最终 user 快照：path → {tags/rating/...}
    snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class _ThumbnailRead:
    path: str
    version: int
    generation: int
    data: bytearray = field(default_factory=bytearray)
    oversized: bool = False


def _dir_error_message(error) -> str:
    if error.code == FilesErrorCode.NOT_FOUND:
        return "目录不存在或已被移动。"
    if error.code == FilesErrorCode.PERMISSION:
        return "没有访问权限。"
    return str(error.message or "读取目录失败。")


def _preview_error_message(error) -> str:
    if error.code == FilesErrorCode.NOT_FOUND:
        return "文件不存在或已被移动。"
    if error.code == FilesErrorCode.PERMISSION:
        return "没有读取权限。"
    return str(error.message or "预览读取失败。")


class NativeFilesTransport(QObject):
    loading = pyqtSignal(str)
    loaded = pyqtSignal(str, list)
    failed = pyqtSignal(str, str)
    thumbnail_loaded = pyqtSignal(str, bytes)
    thumbnail_failed = pyqtSignal(str)
    thumbnail_pending = pyqtSignal(str)
    tree_loaded = pyqtSignal(str, list)
    tree_failed = pyqtSignal(str, str)
    metadata_applied = pyqtSignal(list, dict, str)
    metadata_failed = pyqtSignal(list, dict, str)
    # 服务确认后的最终 user 快照：UI 一律按此回显（不自行推算）
    metadata_snapshot = pyqtSignal(dict)
    capabilities_loaded = pyqtSignal(list)
    preview_loaded = pyqtSignal(str, bytes)
    preview_failed = pyqtSignal(str, str)
    preview_cancelled = pyqtSignal(str)
    task_started = pyqtSignal(dict)
    task_finished = pyqtSignal(dict)
    task_failed = pyqtSignal(str)
    search_loaded = pyqtSignal(dict)
    search_failed = pyqtSignal(str)
    collections_loaded = pyqtSignal(list)
    collection_loaded = pyqtSignal(dict)
    collection_failed = pyqtSignal(str)
    libraries_loaded = pyqtSignal(list)
    library_changed = pyqtSignal(bool, str)
    scan_status_loaded = pyqtSignal(dict)
    scan_failed = pyqtSignal(str)
    thumbnail_retry_done = pyqtSignal(str, bool)
    batch_rename_preview_ready = pyqtSignal(dict)
    batch_rename_preview_failed = pyqtSignal(str, str)  # [FR-13] message, session_id
    batch_rename_finished = pyqtSignal(dict)
    compare_image_loaded = pyqtSignal(str, bytes, str)   # path, data, session
    compare_image_failed = pyqtSignal(str, str, str)     # path, message, session
    compare_set_finished = pyqtSignal(str)               # session
    strip_loaded = pyqtSignal(str, bytes)
    strip_failed = pyqtSignal(str)
    plugin_launch_finished = pyqtSignal(str, bool, str)

    MAX_THUMBNAIL_CONCURRENCY = 4
    MAX_THUMBNAIL_QUEUE = 128
    MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024
    MAX_TREE_QUEUE = 128
    MAX_PREVIEW_BYTES = 64 * 1024 * 1024

    def __init__(
        self, server_url: Callable[[], str], token: str | None,
        parent: QObject | None = None, *, facade: FilesFacade | None = None,
    ) -> None:
        super().__init__(parent)
        self._server_url = server_url
        self._token = token
        # 数据面 runtime（懒构造：首用时建库/开连接，构造失败可诊断）
        self._facade: FilesFacade | None = facade
        self._facade_error: str | None = None
        self._network = QNetworkAccessManager(self)  # 仅剩媒体桥/插件桥使用
        self._generation = 0        # HTTP 缩略图代际（沿用原语义）
        self._nav_generation = 0    # 数据面导航代际：cancel() 后迟到结果作废
        self._preview_seq = 0       # 预览请求身份：同路径重开/取消都作废旧读取
        self._closed = False
        self._running_jobs: set[str] = set()
        self._scan_cancel: CancelToken | None = None
        self._scan_state: dict[str, Any] = {"running": False, "phase": "空闲"}
        self._thumbnail_queue: deque[tuple[str, int]] = deque()
        self._thumbnail_attempted: set[tuple[str, int]] = set()
        self._thumbnail_replies: dict[QNetworkReply, _ThumbnailRead] = {}
        self._thumbnail_versions: dict[str, int] = {}
        self._tree_pending_paths: set[str] = set()
        # [FR-15/17] 比较会话化：session_id → 剩余队列 / 在途标记
        self._compare_sessions: dict[str, deque[str]] = {}
        self._compare_busy: set[str] = set()
        self._compare_seq = 0
        self._rename_plan_ids: dict[str, str] = {}  # [FR-13] 会话 → 已确认计划
        self._strip_replies: dict[QNetworkReply, str] = {}
        self._plugin_replies: dict[QNetworkReply, str] = {}

    # ------------------------------------------------------------ runtime
    def _runtime(self) -> FilesFacade | None:
        """懒构造 facade；失败只发生一次并可诊断（不重复砸盘）。"""
        if self._facade is not None:
            return self._facade
        if self._closed or self._facade_error is not None:
            return None
        try:
            locator = FilesDataLocator.default()
            locator.ensure_layout()
            catalog = FilesCatalog(locator)
            catalog.open()
            self._facade = FilesFacade(catalog)
        except Exception as exc:  # CatalogSchemaMismatch 等：如实上报
            self._facade_error = f"{exc.__class__.__name__}: {exc}"
        return self._facade

    def _runtime_error_message(self) -> str:
        return f"素材数据不可用：{self._facade_error or '未知错误'}"

    # ------------------------------------------------------------ 插件桥（F/G 前保留）
    def launch_plugin(self, plugin_id: str, endpoint: str, payload: dict[str, Any]) -> bool:
        """Open an existing tool; HTTP acknowledgement is not window-ready proof.

        [PR-05] 返回提交事实：True = HTTP 请求已发出；False = 同步拒绝
        （通道关闭/插件不受支持/服务未就绪/同插件在途）。同步拒绝仍发
        plugin_launch_finished 信号（在途去重除外），受理与否不撒谎。
        """
        if self._closed:
            return False
        if plugin_id not in ("format_factory", "image_factory", "shutdown_timer", "decode_benchmark") or endpoint != f"/api/plugin/{plugin_id}/launch":
            self.plugin_launch_finished.emit(plugin_id, False, "此插件没有受支持的原生启动接口。")
            return False
        if plugin_id in self._plugin_replies.values():
            return False
        request = self._make_post(endpoint)
        if request is None:
            self.plugin_launch_finished.emit(plugin_id, False, "素材服务尚未就绪，无法打开插件。")
            return False
        reply = self._network.post(request, json.dumps(payload).encode("utf-8"))
        self._plugin_replies[reply] = plugin_id
        reply.finished.connect(lambda r=reply: self._finish_plugin_launch(r))
        return True

    def _finish_plugin_launch(self, reply: QNetworkReply) -> None:
        plugin_id = self._plugin_replies.pop(reply, None)
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        error = reply.error()
        try:
            data = json.loads(bytes(reply.readAll()))
        except (ValueError, UnicodeError):
            data = None
        reply.deleteLater()
        if self._closed or plugin_id is None:
            return
        success = status == 200 and error == QNetworkReply.NetworkError.NoError and isinstance(data, dict) and data.get("status") == "ok"
        message = "已提交打开请求；工具窗口由插件创建。" if success else "插件未接受打开请求，请检查插件状态或重试。"
        self.plugin_launch_finished.emit(plugin_id, success, message)

    def _make_request(self, endpoint: str, query: dict[str, str | int]) -> QNetworkRequest | None:
        base = self._server_url()
        if self._closed or not base or not self._token:
            return None
        url = QUrl(base)
        if url.scheme() != "http" or url.host() not in ("127.0.0.1", "localhost", "::1"):
            return None
        url.setPath(endpoint)
        url.setQuery(urlencode(query, quote_via=quote))
        url.setFragment("")
        request = QNetworkRequest(url)
        request.setRawHeader(b"X-Auth-Token", self._token.encode("utf-8"))
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.ManualRedirectPolicy,
        )
        request.setTransferTimeout(15000)
        return request

    def _make_post(self, endpoint: str) -> QNetworkRequest | None:
        base = self._server_url()
        if self._closed or not base or not self._token:
            return None
        url = QUrl(base)
        if url.scheme() != "http" or url.host() not in ("127.0.0.1", "localhost", "::1"):
            return None
        url.setPath(endpoint)
        url.setQuery("")
        request = QNetworkRequest(url)
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        request.setRawHeader(b"X-Auth-Token", self._token.encode("utf-8"))
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.ManualRedirectPolicy,
        )
        request.setTransferTimeout(15000)
        return request

    # ------------------------------------------------------------ 目录树（facade）
    def load_tree(self, path: str) -> None:
        """Load one expanded node, independently of content navigation.

        Concurrent requests for the same node share its final loaded/failed
        signal. No filesystem scanning or recursive prefetch occurs here.
        Pending work is capped (在途集合封顶)；超出即时拒绝不排队。
        """
        if self._closed or path in self._tree_pending_paths:
            return
        if len(self._tree_pending_paths) >= self.MAX_TREE_QUEUE:
            self.tree_failed.emit(path, "目录树读取队列已满，请稍后重试。")
            return
        facade = self._runtime()
        if facade is None:
            self.tree_failed.emit(path, self._runtime_error_message())
            return
        self._tree_pending_paths.add(path)
        facade.tree(path, request_id=uuid.uuid4().hex,
                    listener=lambda outcome, p=path: self._finish_tree(outcome, p))

    def _finish_tree(self, outcome, path: str) -> None:
        self._tree_pending_paths.discard(path)
        if self._closed:
            return
        if outcome.error is not None:
            self.tree_failed.emit(path, str(outcome.error.message or "目录树读取失败。"))
            return
        entries = outcome.result
        if not isinstance(entries, list) or any(
            not isinstance(entry, dict)
            or not isinstance(entry.get("name"), str) or not entry["name"]
            or not isinstance(entry.get("path"), str) or not entry["path"]
            for entry in entries
        ):
            self.tree_failed.emit(path, "目录树数据无效。")
            return
        self.tree_loaded.emit(path, entries)

    # ------------------------------------------------------------ 缩略图桥（E 前保留 HTTP）
    def request_thumbnail(self, path: str, version: int = 0) -> None:
        """Request only painted items; one attempt per path/version/directory.

        The view owns decoded-image caching and bounded pending retries via
        release_thumbnail(). Genuine failures retain their fallback until a
        directory refresh (or thumbnail version change).
        """
        if self._closed:
            return
        key = (path, version)
        if key in self._thumbnail_attempted:
            return
        self._thumbnail_attempted.add(key)
        self._thumbnail_versions[path] = version
        if len(self._thumbnail_queue) >= self.MAX_THUMBNAIL_QUEUE:
            self.thumbnail_failed.emit(path)
            return
        self._thumbnail_queue.append(key)
        self._pump_thumbnails()

    def release_thumbnail(self, path: str) -> None:
        """Allow a completed thumbnail evicted from the view's cache to reload.

        Preserve deduplication for queued/in-flight work: evicting a different
        decoded image must never create duplicate network requests.
        """
        active = set(self._thumbnail_queue)
        active.update((state.path, state.version) for state in self._thumbnail_replies.values())
        self._thumbnail_attempted.difference_update(
            key for key in tuple(self._thumbnail_attempted)
            if key[0] == path and key not in active
        )

    def _pump_thumbnails(self) -> None:
        while self._thumbnail_queue and len(self._thumbnail_replies) < self.MAX_THUMBNAIL_CONCURRENCY:
            path, version = self._thumbnail_queue.popleft()
            if self._thumbnail_versions.get(path) != version:
                continue
            request = self._make_request("/api/thumb", {"path": path, "size": "small", "v": version})
            if request is None:
                self.thumbnail_failed.emit(path)
                continue
            reply = self._network.get(request)
            reply.setReadBufferSize(self.MAX_THUMBNAIL_BYTES + 1)
            state = _ThumbnailRead(path, version, self._generation)
            self._thumbnail_replies[reply] = state
            reply.metaDataChanged.connect(lambda r=reply, s=state: self._check_thumbnail_size(r, s))
            reply.readyRead.connect(lambda r=reply, s=state: self._read_thumbnail(r, s))
            reply.finished.connect(lambda r=reply, s=state: self._finish_thumbnail(r, s))

    def _check_thumbnail_size(self, reply: QNetworkReply, state: _ThumbnailRead) -> None:
        if state.generation != self._generation:
            return
        size = reply.header(QNetworkRequest.KnownHeaders.ContentLengthHeader)
        if isinstance(size, int) and size > self.MAX_THUMBNAIL_BYTES:
            state.oversized = True
            reply.abort()

    def _read_thumbnail(self, reply: QNetworkReply, state: _ThumbnailRead) -> None:
        if state.generation != self._generation or state.oversized:
            return
        chunk = reply.read(self.MAX_THUMBNAIL_BYTES + 1 - len(state.data))
        if chunk:
            state.data.extend(bytes(chunk))
        if len(state.data) > self.MAX_THUMBNAIL_BYTES:
            state.oversized = True
            reply.abort()

    def _finish_thumbnail(self, reply: QNetworkReply, state: _ThumbnailRead) -> None:
        if state.generation != self._generation:
            reply.deleteLater()
            return
        self._read_thumbnail(reply, state)
        self._thumbnail_replies.pop(reply, None)
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        content_type = str(reply.header(QNetworkRequest.KnownHeaders.ContentTypeHeader) or "").lower()
        disposition = bytes(reply.rawHeader(b"Content-Disposition")).decode("latin-1").lower()
        placeholder = "logo.webp" in disposition and state.path.replace("\\", "/").rsplit("/", 1)[-1].lower() != "logo.webp"
        success = (not state.oversized and status == 200
                   and reply.error() == QNetworkReply.NetworkError.NoError
                   and content_type.startswith("image/") and bool(state.data)
                   and not placeholder)
        pending = (not state.oversized
                   and reply.error() == QNetworkReply.NetworkError.NoError
                   and (status == 202 or (status == 200 and placeholder)))
        reply.deleteLater()
        if self._thumbnail_versions.get(state.path) == state.version:
            if success:
                self.thumbnail_loaded.emit(state.path, bytes(state.data))
            elif pending:
                self.thumbnail_pending.emit(state.path)
            else:
                self.thumbnail_failed.emit(state.path)
        self._pump_thumbnails()

    # ------------------------------------------------------------ 目录内容（facade）
    def load_directory(self, path: str) -> None:
        if self._closed:
            return
        self.cancel()
        generation = self._nav_generation
        self.loading.emit(path)
        facade = self._runtime()
        if facade is None:
            self.failed.emit(path, self._runtime_error_message())
            return
        request = QueryRequest(request_id=uuid.uuid4().hex, path=path)

        def _work():
            record = facade.libraries.library_for_path(request.path)
            return facade.query.list_directory(
                request, library_id=record.library_id if record else "")

        def _deliver(outcome) -> None:
            if self._closed or generation != self._nav_generation:
                return
            if outcome.error is not None:
                self.failed.emit(path, _dir_error_message(outcome.error))
                return
            self.loaded.emit(path, list(outcome.result.entries))

        facade.run(key=f"list:{identity_key(path)}", request_id=request.request_id,
                   work=_work, listener=_deliver)

    # ------------------------------------------------------------ 元数据写入（facade）
    def patch_metadata(self, paths: list[str], patch: dict[str, Any],
                       tags_op: str = "replace") -> None:
        """只提交用户改过的字段；服务确认后才发 metadata_applied。

        tags_op 显式语义（replace/add/remove/clear）；失败逐条汇总，不写
        "已保存"的假状态；成功时随 metadata_snapshot 发布落库最终快照。
        路径不在任何已固定库内时逐条失败并说明原因（不静默假装成功）。
        """
        if self._closed or not paths or not patch:
            return
        allowed = ("rating", "tags", "color", "flag", "comments")
        if not any(key in allowed for key in patch):
            return
        facade = self._runtime()
        if facade is None:
            self.metadata_failed.emit(list(paths), dict(patch),
                                      self._runtime_error_message())
            return
        job = _MetadataJob(paths=list(paths), patch=dict(patch))

        def _work() -> list[ItemResult]:
            groups: dict[str, list[str]] = {}
            for raw in paths:
                record = facade.libraries.library_for_path(raw)
                groups.setdefault(record.library_id if record else "", []).append(nfc(raw))
            items: list[ItemResult] = []
            for library_id, group in groups.items():
                if not library_id:
                    items.extend(
                        ItemResult(path, ok=False, error_code="NOT_FOUND",
                                   message="路径不在任何已固定库内；先固定常用位置并建立索引。")
                        for path in group)
                    continue
                result = facade.metadata.patch_metadata(
                    group, dict(patch), library_id=library_id, tags_op=tags_op)
                items.extend(result.items)
            return items

        def _deliver(outcome) -> None:
            if self._closed:
                return
            if outcome.error is not None:
                job.failures.append(str(outcome.error.message or "保存失败（服务未确认）"))
            else:
                for item in outcome.result:
                    if item.ok:
                        job.snapshots[item.path] = dict(item.result or {})
                    else:
                        job.failures.append(str(item.message or item.error_code or "保存失败"))
            self._publish_metadata(job)

        facade.run(key=f"meta:{uuid.uuid4().hex}", request_id=uuid.uuid4().hex,
                   work=_work, listener=_deliver)

    def _publish_metadata(self, job: _MetadataJob) -> None:
        if job.failures:
            self.metadata_failed.emit(list(job.paths), dict(job.patch),
                                      "；".join(sorted(set(job.failures))))
        else:
            self.metadata_applied.emit(list(job.paths), dict(job.patch), "已保存")
            if job.snapshots:
                self.metadata_snapshot.emit(dict(job.snapshots))

    # ------------------------------------------------------------ 文件任务（facade）
    def run_task(
        self, operation: str, targets: list[str], *,
        policy: str = "error",
        options: dict[str, Any] | None = None,
    ) -> None:
        """提交文件任务：mkdir/rename/copy/move/trash/restore/purge。

        成功判断不在这里——逐项结果由服务 items 决定；进度经回调推进
        task_started（复用为进度快照，UI 只读 completed/total）。
        """
        if self._closed or not targets:
            return
        facade = self._runtime()
        if facade is None:
            self.task_failed.emit(self._runtime_error_message())
            return
        options = dict(options or {})
        targets = list(targets)
        if operation == "mkdir" and options.get("name"):
            # UI 语义：目标目录 + 新名字 → 服务接收完整路径
            targets = [os.path.join(targets[0], str(options.pop("name")))]
        job_id = f"task-{operation}-{uuid.uuid4().hex[:10]}"
        self._running_jobs.add(job_id)
        self.task_started.emit({"job_id": job_id, "operation": operation,
                                "total": len(targets), "completed": 0})

        def _progress(done: int, total: int) -> None:
            if not self._closed:
                self.task_started.emit({"job_id": job_id, "operation": operation,
                                        "total": total, "completed": done})

        def _work() -> list[ItemResult]:
            groups: dict[str, list[str]] = {}
            for raw in targets:
                record = facade.libraries.library_for_path(raw)
                groups.setdefault(record.library_id if record else "", []).append(nfc(raw))
            items: list[ItemResult] = []
            for library_id, group in groups.items():
                result = facade.tasks.start_task(
                    library_id, operation, group, options=options, policy=policy,
                    job_id=job_id, progress=_progress)
                items.extend(result.items)
            return items

        def _deliver(outcome) -> None:
            self._running_jobs.discard(job_id)
            if self._closed:
                return
            if outcome.error is not None:
                self.task_failed.emit(str(outcome.error.message or "文件操作未能提交"))
                return
            items = list(outcome.result)
            successes = [{"path": item.path, "to": item.result or ""}
                         for item in items if item.ok]
            failures = [{"path": item.path,
                         "error": str(item.message or item.error_code or "失败"),
                         "code": str(item.error_code or "")}
                        for item in items if not item.ok]
            cancelled = any(item.error_code == "CANCELLED" for item in items)
            self.task_finished.emit({
                "job_id": job_id, "operation": operation,
                "total": len(items), "completed": len(successes),
                "successes": successes, "failures": failures,
                "cancelled": cancelled})

        facade.run(key=f"task:{job_id}", request_id=job_id,
                   work=_work, listener=_deliver)

    def cancel_task(self, job_id: str) -> None:
        """请求取消：协作式（条目/块边界生效）；终态仍由 task_finished 报告。"""
        if self._closed or not job_id:
            return
        facade = self._runtime()
        if facade is not None:
            facade.tasks.cancel_task(job_id)  # 结果不广播：进度/终态自然反映取消

    def active_task_jobs(self) -> list[str]:
        """[FR-16] 当前仍在执行的任务 id（任务列表/取消入口用）。"""
        return sorted(self._running_jobs)

    # ------------------------------------------------------------ 常用位置（facade）
    def load_libraries(self) -> None:
        if self._closed:
            return
        facade = self._runtime()
        if facade is None:
            return

        def _deliver(outcome) -> None:
            if self._closed or outcome.error is not None:
                return
            self.libraries_loaded.emit([
                {"library_id": record.library_id, "name": record.name,
                 "path": record.root}
                for record in outcome.result])

        facade.run(key="libraries", request_id=uuid.uuid4().hex,
                   work=lambda: facade.libraries.list_libraries(),
                   listener=_deliver)

    def pin_library(self, path: str) -> None:
        self._library_change(
            lambda facade: facade.libraries.register(path), "已固定为常用位置")

    def unpin_library(self, path: str) -> None:
        def _work(facade: FilesFacade) -> str:
            target = identity_key(path)
            match = next((record for record in facade.libraries.list_libraries()
                          if identity_key(record.root) == target), None)
            if match is None:
                return "该目录不在常用位置中"
            return "" if facade.libraries.unpin(match.library_id) else "取消固定失败"
        self._library_change(_work, "已取消固定")

    def _library_change(self, work: Callable[[FilesFacade], Any],
                        ok_message: str) -> None:
        facade = self._runtime()
        if facade is None:
            self.library_changed.emit(False, self._runtime_error_message())
            return

        def _deliver(outcome) -> None:
            if self._closed:
                return
            if outcome.error is not None:
                self.library_changed.emit(False, str(outcome.error.message or "操作失败"))
                return
            result = outcome.result
            ok = not (isinstance(result, str) and result)
            message = ok_message if ok else str(result or "操作失败")
            self.library_changed.emit(bool(ok), message)
            if ok:
                self.load_libraries()

        facade.run(key=f"library:{uuid.uuid4().hex}", request_id=uuid.uuid4().hex,
                   work=lambda: work(facade), listener=_deliver)

    # ------------------------------------------------------------ 索引（facade）
    def load_scan_status(self) -> None:
        if self._closed:
            return
        self.scan_status_loaded.emit(dict(self._scan_state))

    def start_scan(self) -> None:
        if self._closed:
            return
        facade = self._runtime()
        if facade is None:
            self.scan_failed.emit(self._runtime_error_message())
            return
        records = facade.libraries.list_libraries()
        if not records:
            self.scan_failed.emit("还没有固定的素材库；先固定常用位置再建立索引。")
            return
        cancel = CancelToken()
        self._scan_cancel = cancel
        self._scan_state = {"running": True, "phase": "扫描中", "scanned": 0, "total": 0}
        self.scan_status_loaded.emit(dict(self._scan_state))

        def _progress(done: int, total: int) -> None:
            if self._closed:
                return
            self._scan_state = {"running": True, "phase": "扫描中",
                                "scanned": int(done), "total": int(total) if total > 0 else 0}
            self.scan_status_loaded.emit(dict(self._scan_state))

        def _deliver(outcome) -> None:
            self._scan_cancel = None
            if self._closed:
                return
            if outcome.error is not None:
                self._scan_state = {"running": False, "phase": "失败"}
                self.scan_failed.emit(str(outcome.error.message or "索引失败"))
                return
            result = outcome.result
            scanned = sum(int(item.result.get("scanned", 0))
                          for item in result.items
                          if item.ok and isinstance(item.result, dict))
            cancelled = result.stage == "cancelled"
            self._scan_state = {
                "running": False,
                "phase": "已取消" if cancelled else "完成",
                "scanned": scanned,
                "last_result": {"added": scanned}}
            self.scan_status_loaded.emit(dict(self._scan_state))

        facade.scan_libraries([record.library_id for record in records],
                              request_id=uuid.uuid4().hex, listener=_deliver,
                              cancel=cancel, progress=_progress)

    def cancel_scan(self) -> None:
        if self._scan_cancel is not None:
            self._scan_cancel.request()

    def retry_thumbnail(self, path: str, version: int = 0) -> None:
        request = self._make_post("/api/thumb/retry")
        if request is None:
            self.thumbnail_retry_done.emit(path, False)
            return
        reply = self._network.post(request, json.dumps({"path": path}).encode("utf-8"))
        reply.finished.connect(lambda r=reply, p=path: self._finish_thumbnail_retry(r, p))

    def _finish_thumbnail_retry(self, reply: QNetworkReply, path: str) -> None:
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        error = reply.error()
        reply.deleteLater()
        if self._closed:
            return
        self.thumbnail_retry_done.emit(path, status == 200 and error == QNetworkReply.NetworkError.NoError)

    # ------------------------------------------------------------ 批量重命名（facade）
    def preview_batch_rename(self, files: list[str], mode: str, params: dict[str, Any],
                             session_id: str = "") -> None:
        """先预览再执行：预览不落盘，冲突由计划标记。

        [FR-13] session_id 贯穿请求/响应：多个非模态窗口共享广播信号，
        没有会话身份就会收到彼此的预览/执行结果。plan_id 由本类按会话
        暂存（对话框接线零改动）；执行侧 B6 指纹校验兜底防旧计划重放。
        """
        facade = self._runtime()
        if facade is None:
            self.batch_rename_preview_failed.emit(self._runtime_error_message(), session_id)
            return
        record = facade.libraries.library_for_path(files[0]) if files else None
        library_id = record.library_id if record else ""

        def _deliver(outcome) -> None:
            if self._closed:
                return
            if outcome.error is not None:
                self.batch_rename_preview_failed.emit(
                    str(outcome.error.message or "批量改名预览失败"), session_id)
                return
            plan = outcome.result
            self._rename_plan_ids[session_id] = plan.plan_id
            payload = {
                "session_id": session_id,
                "plan": [{"src_name": item.current_name,
                          "dst_name": item.proposed_name,
                          "conflict": bool(item.conflict)}
                         for item in plan.items],
                "errors": [item.reason for item in plan.items if item.reason and not item.conflict],
                "plan_id": plan.plan_id,
            }
            self.batch_rename_preview_ready.emit(payload)

        facade.batch_rename_preview(
            library_id, list(files), mode, dict(params or {}),
            request_id=session_id or uuid.uuid4().hex, listener=_deliver)

    def execute_batch_rename(self, files: list[str], mode: str, params: dict[str, Any],
                             session_id: str = "") -> None:
        """执行**已确认**的计划（plan_id 取自本会话最近一次预览）。"""
        facade = self._runtime()
        plan_id = self._rename_plan_ids.get(session_id, "")
        if facade is None or not plan_id:
            self.batch_rename_finished.emit({"success": False,
                                             "message": (self._runtime_error_message()
                                                         if facade is None else "请先预览确认后再执行"),
                                             "session_id": session_id})
            return
        self._rename_plan_ids.pop(session_id, None)
        record = facade.libraries.library_for_path(files[0]) if files else None
        library_id = record.library_id if record else ""

        def _deliver(outcome) -> None:
            if self._closed:
                return
            if outcome.error is not None:
                self.batch_rename_finished.emit({
                    "success": False,
                    "message": str(outcome.error.message or "批量改名失败"),
                    "session_id": session_id})
                return
            outcomes = list(outcome.result)
            errors = [f"{item.source.rsplit('/', 1)[-1]}: {item.message or item.code}"
                      for item in outcomes if not item.ok]
            self.batch_rename_finished.emit({
                "success": not errors,
                "processed": len(outcomes),
                "errors": errors,
                "message": "已执行" if not errors else "部分失败",
                "session_id": session_id})

        facade.batch_rename_execute(plan_id, library_id,
                                    request_id=session_id or uuid.uuid4().hex,
                                    listener=_deliver)

    # ------------------------------------------------------------ 并排比较（本地直读）
    def load_compare_images(self, paths: list[str], session_id: str = "") -> None:
        """[FR-15] 会话隔离：每个比较窗口一个独立队列与在途请求。"""
        queue = deque(list(paths)[:4])
        if not queue:
            self.compare_set_finished.emit(session_id)
            return
        self._compare_sessions[session_id] = queue
        self._pump_compare(session_id)

    def cancel_compare(self, session_id: str = "") -> None:
        """[FR-17] 关闭窗口即作废该会话：剩余队列清空，迟到结果丢弃。"""
        self._compare_sessions.pop(session_id, None)
        self._compare_busy.discard(session_id)

    def _pump_compare(self, session_id: str) -> None:
        queue = self._compare_sessions.get(session_id)
        if queue is None:
            return  # 会话已取消/关闭
        if session_id in self._compare_busy:
            return  # 该会话上一张未读完
        if not queue:
            self._compare_sessions.pop(session_id, None)
            self.compare_set_finished.emit(session_id)
            return
        path = queue.popleft()
        facade = self._runtime()
        if facade is None:
            self.compare_image_failed.emit(path, self._runtime_error_message(), session_id)
            QTimer.singleShot(0, lambda s=session_id: self._pump_compare(s))
            return
        self._compare_busy.add(session_id)
        self._compare_seq += 1
        seq = self._compare_seq

        def _deliver(outcome) -> None:
            self._compare_busy.discard(session_id)
            if self._closed or session_id not in self._compare_sessions:
                return  # [FR-17] 会话已关闭：结果一律丢弃
            if outcome.error is not None:
                self.compare_image_failed.emit(path, _preview_error_message(outcome), session_id)
            else:
                self.compare_image_loaded.emit(path, bytes(outcome.result), session_id)
            self._pump_compare(session_id)

        # 独立 key：两个比较窗口含同一文件时互不作废
        facade.run(key=f"compare:{session_id}:{seq}", request_id=uuid.uuid4().hex,
                   work=lambda: read_file_bytes(path, self.MAX_PREVIEW_BYTES),
                   listener=_deliver)

    # ------------------------------------------------------------ 悬停胶片条桥（E 前保留 HTTP）
    def request_strip(self, path: str, duration: float = 0.0) -> None:
        if self._closed:
            return
        request = self._make_request("/api/thumb/strip", {"path": path, "duration": max(0.0, float(duration or 0.0))})
        if request is None:
            self.strip_failed.emit(path)
            return
        reply = self._network.get(request)
        self._strip_replies[reply] = path
        reply.finished.connect(lambda r=reply: self._finish_strip(r))

    def _finish_strip(self, reply: QNetworkReply) -> None:
        path = self._strip_replies.pop(reply, "")
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        raw = bytes(reply.readAll())
        error = reply.error()
        reply.deleteLater()
        if self._closed or not path:
            return
        if status == 200 and error == QNetworkReply.NetworkError.NoError and raw:
            self.strip_loaded.emit(path, raw)
        else:
            self.strip_failed.emit(path)

    # ------------------------------------------------------------ 集合浏览（facade）
    def load_collections(self) -> None:
        if self._closed:
            return
        facade = self._runtime()
        if facade is None:
            return

        def _deliver(outcome) -> None:
            if self._closed or outcome.error is not None:
                return
            self.collections_loaded.emit(
                [dict(item.result) for item in outcome.result.items])

        facade.run(key="collections", request_id=uuid.uuid4().hex,
                   work=lambda: facade.collections.list_collections(),
                   listener=_deliver)

    def load_collection(self, collection_id: int) -> None:
        """集合成员与目录列表同构；不构建第二套查询语义。"""
        if self._closed:
            return
        # [FR-09] 连续切换集合时旧响应直接作废（facade 同 key 作废 + 代际门）
        generation = self._nav_generation
        facade = self._runtime()
        if facade is None:
            self.collection_failed.emit(self._runtime_error_message())
            return

        def _deliver(outcome) -> None:
            if self._closed or generation != self._nav_generation:
                return
            if outcome.error is not None:
                self.collection_failed.emit(str(outcome.error.message or "读取集合失败"))
                return
            result = outcome.result
            self.collection_loaded.emit({
                "files": list(result.entries),
                "total": result.total,
                "truncated": result.truncated})

        facade.collection_members(int(collection_id),
                                  request_id=uuid.uuid4().hex, listener=_deliver)

    # ------------------------------------------------------------ 全库搜索（facade）
    def search_library(self, query: str, *, limit: int = 200, offset: int = 0) -> None:
        """跨目录搜索：只读索引，不扫盘；结果显式带 total/truncated。

        [FR-09] 与目录列表同一套失效规则：新请求作废旧搜索；切目录
        （load_directory→cancel）也一并作废，晚到的旧响应绝不覆盖新视图。
        """
        if self._closed:
            return
        generation = self._nav_generation
        facade = self._runtime()
        if facade is None:
            self.search_failed.emit(self._runtime_error_message())
            return
        if not facade.libraries.list_libraries():
            self.search_failed.emit("还没有固定的素材库，无法全库搜索。")
            return

        def _deliver(outcome) -> None:
            if self._closed or generation != self._nav_generation:
                return
            if outcome.error is not None:
                self.search_failed.emit(str(outcome.error.message or "搜索失败"))
                return
            result = outcome.result
            self.search_loaded.emit({
                "query": query,
                "files": list(result.entries),
                "total": result.total,
                "truncated": result.truncated})

        facade.search(query, library_id="", request_id=uuid.uuid4().hex,
                      listener=_deliver, limit=int(limit), offset=int(offset))

    # ------------------------------------------------------------ 能力清单
    def load_capabilities(self) -> None:
        """数据面能力由本进程服务静态决定（无旧服务可用性参与）。"""
        if self._closed:
            return
        self.capabilities_loaded.emit([
            {"key": "metadata.patch", "available": True, "reason": ""},
            {"key": "files.task", "available": True, "reason": ""},
            {"key": "library.search", "available": True, "reason": ""},
        ])

    # ------------------------------------------------------------ 原生预览取数（本地直读）
    def load_preview(self, path: str) -> None:
        """只取字节；解码在 UI 侧工作线程完成，主线程不做同步解码。"""
        self.cancel_preview()
        if self._closed:
            return
        facade = self._runtime()
        if facade is None:
            self.preview_failed.emit(path, self._runtime_error_message())
            return
        seq = self._preview_seq

        def _deliver(outcome) -> None:
            # 同路径重开也必须按请求身份丢弃旧读取，不能让旧字节成为新解码任务。
            if self._closed or seq != self._preview_seq:
                return
            if outcome.error is not None:
                self.preview_failed.emit(path, _preview_error_message(outcome.error))
                return
            self.preview_loaded.emit(path, bytes(outcome.result))

        facade.read_file(path, request_id=uuid.uuid4().hex, listener=_deliver,
                         max_bytes=self.MAX_PREVIEW_BYTES)

    def cancel_preview(self) -> None:
        self._preview_seq += 1

    # ------------------------------------------------------------ 取消/关闭
    def cancel(self) -> None:
        self._generation += 1
        self._nav_generation += 1
        thumbnails = list(self._thumbnail_replies)
        self._thumbnail_replies.clear()
        self._thumbnail_queue.clear()
        self._thumbnail_attempted.clear()
        self._thumbnail_versions.clear()
        for thumbnail in thumbnails:
            thumbnail.abort()

    def shutdown(self) -> None:
        # [FR-17] 先标记关闭，再取消全部在途工作：关闭后的完成回调
        # 一律静默丢弃（比较/元数据/任务的迟到结果不再发信号）
        self._closed = True
        for reply in list(self._plugin_replies):
            reply.abort()
            reply.deleteLater()
        self._plugin_replies.clear()
        for session_id in list(self._compare_sessions):
            self.cancel_compare(session_id)
        self._compare_sessions.clear()
        self._compare_busy.clear()
        for reply in list(self._strip_replies):
            reply.abort()
        self._strip_replies.clear()
        if self._scan_cancel is not None:
            self._scan_cancel.request()
        facade, self._facade = self._facade, None
        if facade is not None:
            try:
                facade.shutdown()  # 取消未开始的工作并回收 worker
            except Exception:
                pass
        self.cancel()
        self.cancel_preview()
        self._tree_pending_paths.clear()
        for reply in list(self._thumbnail_replies):
            reply.abort()
