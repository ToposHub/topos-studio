"""Asynchronous UI transport for the existing Files HTTP API.

No directory scanning, database access or file mutations live here. The legacy
service remains authoritative, including its locked-directory checks.
"""
from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest


@dataclass
class _MetadataJob:
    """一次元数据写入的请求身份；逐路径结果汇总后才发布成功/失败。"""

    paths: list[str]
    patch: dict[str, Any]
    remaining: int = 0
    failures: list[str] = field(default_factory=list)
    # [FR-11] 服务端落库后的最终 user 快照：path → {tags/rating/...}
    snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class _PreviewRead:
    path: str
    data: bytearray = field(default_factory=bytearray)
    oversized: bool = False


@dataclass
class _ThumbnailRead:
    path: str
    version: int
    generation: int
    data: bytearray = field(default_factory=bytearray)
    oversized: bool = False


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
    # [FR-11] 服务确认后的最终 user 快照：UI 一律按此回显（不自行推算）
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
    compare_image_failed = pyqtSignal(str, str, str)    # path, message, session
    compare_set_finished = pyqtSignal(str)              # session
    strip_loaded = pyqtSignal(str, bytes)
    strip_failed = pyqtSignal(str)
    plugin_launch_finished = pyqtSignal(str, bool, str)

    MAX_THUMBNAIL_CONCURRENCY = 4
    MAX_THUMBNAIL_QUEUE = 128
    MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024
    MAX_TREE_CONCURRENCY = 4
    MAX_TREE_QUEUE = 128
    MAX_PREVIEW_BYTES = 64 * 1024 * 1024

    def __init__(
        self, server_url: Callable[[], str], token: str | None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._server_url = server_url
        self._token = token
        self._network = QNetworkAccessManager(self)
        self._reply: QNetworkReply | None = None
        self._generation = 0
        self._closed = False
        self._thumbnail_queue: deque[tuple[str, int]] = deque()
        self._thumbnail_attempted: set[tuple[str, int]] = set()
        self._thumbnail_replies: dict[QNetworkReply, _ThumbnailRead] = {}
        self._thumbnail_versions: dict[str, int] = {}
        self._tree_queue: deque[str] = deque()
        self._tree_replies: dict[QNetworkReply, str] = {}
        self._tree_pending_paths: set[str] = set()
        self._metadata_replies: dict[QNetworkReply, _MetadataJob] = {}
        self._preview_reply: QNetworkReply | None = None
        self._preview_state: _PreviewRead | None = None
        # [FR-15/17] 比较会话化：session_id → 剩余队列 / 在途 (reply, state)
        self._compare_sessions: dict[str, deque[str]] = {}
        self._compare_replies: dict[str, tuple[QNetworkReply, _PreviewRead]] = {}
        self._strip_replies: dict[QNetworkReply, str] = {}
        # [FR-09] 搜索/集合请求也纳入在途表：切目录后旧结果不得覆盖新视图
        self._search_reply: QNetworkReply | None = None
        self._collection_reply: QNetworkReply | None = None
        # [FR-16] 多任务并行轮询：job_id → {attempts, reply}；单一槽位会让
        # 第二个任务覆盖第一个的轮询，前任务失去完成/失败反馈
        self._task_polls: dict[str, dict[str, Any]] = {}
        self._task_poll_timer: QTimer | None = None
        self._scan_poll_timer: QTimer | None = None
        self._plugin_replies: dict[QNetworkReply, str] = {}

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

    def load_tree(self, path: str) -> None:
        """Load one expanded node, independently of content navigation.

        Concurrent requests for the same node share its final loaded/failed
        signal. No filesystem scanning or recursive prefetch occurs here.
        """
        if self._closed or path in self._tree_pending_paths:
            return
        if len(self._tree_queue) >= self.MAX_TREE_QUEUE:
            self.tree_failed.emit(path, "目录树读取队列已满，请稍后重试。")
            return
        self._tree_pending_paths.add(path)
        self._tree_queue.append(path)
        self._pump_tree()

    def _pump_tree(self) -> None:
        while not self._closed and self._tree_queue and len(self._tree_replies) < self.MAX_TREE_CONCURRENCY:
            path = self._tree_queue.popleft()
            request = self._make_request("/api/tree", {"path": path})
            if request is None:
                self._tree_pending_paths.discard(path)
                self.tree_failed.emit(path, "素材服务尚未就绪或地址无效。")
                continue
            reply = self._network.get(request)
            self._tree_replies[reply] = path
            reply.finished.connect(lambda r=reply, p=path: self._finish_tree(r, p))

    def _finish_tree(self, reply: QNetworkReply, path: str) -> None:
        self._tree_replies.pop(reply, None)
        self._tree_pending_paths.discard(path)
        if self._closed:
            reply.deleteLater()
            return
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        raw = bytes(reply.readAll())
        error = reply.error()
        error_text = reply.errorString()
        reply.deleteLater()
        try:
            entries: Any = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            entries = None
        if status != 200 or error != QNetworkReply.NetworkError.NoError:
            self.tree_failed.emit(path, error_text if error != QNetworkReply.NetworkError.NoError else "目录树请求失败。")
        elif not isinstance(entries, list) or any(
            not isinstance(entry, dict)
            or any(not isinstance(entry.get(key), str) or not entry[key] for key in ("name", "path"))
            for entry in entries
        ):
            self.tree_failed.emit(path, "素材服务返回的目录树格式无效。")
        else:
            self.tree_loaded.emit(path, entries)
        self._pump_tree()

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

    def load_directory(self, path: str) -> None:
        if self._closed:
            return
        self.cancel()
        generation = self._generation
        self.loading.emit(path)
        base = self._server_url()
        if not base or not self._token:
            self.failed.emit(path, "素材服务尚未就绪，请稍后重试或打开完整 Files。")
            return
        url = QUrl(base)
        # Credentials belong only to the local Files runtime.
        if url.scheme() != "http" or url.host() not in ("127.0.0.1", "localhost", "::1"):
            self.failed.emit(path, "素材服务地址无效。")
            return
        url.setPath("/api/list")
        # QUrlQuery treats '+' and existing %XX as query syntax. Encode the
        # filesystem path as a component so Flask receives it exactly once.
        url.setQuery(urlencode({"path": path}, quote_via=quote))
        request = QNetworkRequest(url)
        request.setRawHeader(b"X-Auth-Token", self._token.encode("utf-8"))
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.ManualRedirectPolicy,
        )
        request.setTransferTimeout(15000)
        reply = self._network.get(request)
        self._reply = reply
        reply.finished.connect(lambda: self._finish(reply, path, generation))

    def _finish(self, reply: QNetworkReply, path: str, generation: int) -> None:
        if generation != self._generation:
            reply.deleteLater()
            return
        self._reply = None
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        raw = bytes(reply.readAll())
        error = reply.error()
        error_text = reply.errorString()
        reply.deleteLater()
        try:
            payload: Any = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            self.failed.emit(path, "素材服务没有返回有效数据。" if error == QNetworkReply.NetworkError.NoError else error_text)
            return
        if not isinstance(payload, dict):
            self.failed.emit(path, "素材服务返回的目录格式无效。")
            return
        if status != 200 or error != QNetworkReply.NetworkError.NoError or payload.get("error"):
            message = "目录已锁定，请在完整 Files 中解锁后重试。" if payload.get("locked") else str(payload.get("error") or error_text)
            self.failed.emit(path, message)
            return
        entries = payload.get("files")
        if not isinstance(entries, list) or any(
            not isinstance(entry, dict)
            or any(not isinstance(entry.get(key), str) for key in ("name", "path", "type"))
            for entry in entries
        ):
            self.failed.emit(path, "素材服务返回的文件条目无效。")
            return
        self.loaded.emit(path, entries)

    # -- 元数据写入（U05） ------------------------------------------------
    def patch_metadata(self, paths: list[str], patch: dict[str, Any],
                       tags_op: str = "replace") -> None:
        """只提交用户改过的字段；单文件走 update_meta，多文件走 batch/meta。

        [FR-11] tags_op 显式语义（replace/add/remove/clear），不再让服务
        端"追加"与 UI"替换"显示各说各话。响应确认后才发 metadata_applied，
        失败逐条汇总：不写"已保存"的假状态；成功时随 metadata_snapshot
        发布服务端最终 user 快照。
        """
        if self._closed or not paths or not patch:
            return
        allowed = ("rating", "tags", "color", "flag", "comments")
        batchable = {key: value for key, value in patch.items() if key in allowed}
        tags_payload = dict(patch)
        if "tags" in batchable or tags_op == "clear":
            tags_payload["tags_op"] = tags_op
        requests: list[tuple[str, dict[str, Any]]] = []
        if len(paths) == 1:
            requests.append(("/api/update_meta", {"path": paths[0], **tags_payload}))
        elif batchable:
            # B06：comments 已并入 batch/meta（字段级 patch，一次请求一套语义）
            requests.append(("/api/batch/meta", {"files": paths, **tags_payload}))
        if not requests:
            return
        job = _MetadataJob(paths=list(paths), patch=dict(patch), remaining=len(requests))
        for endpoint, payload in requests:
            request = self._make_post(endpoint)
            if request is None:
                job.remaining -= 1
                job.failures.append("素材服务尚未就绪，无法保存标记。")
                continue
            reply = self._network.post(request, json.dumps(payload).encode("utf-8"))
            self._metadata_replies[reply] = job
            reply.finished.connect(lambda r=reply, j=job: self._finish_metadata(r, j))
        if job.remaining == 0:
            self._publish_metadata(job)

    def _finish_metadata(self, reply: QNetworkReply, job: _MetadataJob) -> None:
        self._metadata_replies.pop(reply, None)
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        raw = bytes(reply.readAll())
        error = reply.error()
        reply.deleteLater()
        job.remaining -= 1
        if self._closed:
            return
        payload: Any = None
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            payload = None
        success = (status == 200 and error == QNetworkReply.NetworkError.NoError
                   and isinstance(payload, dict) and payload.get("success") is not False)
        if not success:
            message = ""
            if isinstance(payload, dict):
                message = str(payload.get("message") or payload.get("error") or "")
            job.failures.append(message or "保存失败（服务未确认）")
        elif isinstance(payload, dict):
            # [FR-11] 收集服务端最终快照：单文件 user 字段 / 批量 results 列表
            user = payload.get("user")
            if isinstance(user, dict):
                job.snapshots[str(job.paths[0]) if len(job.paths) == 1 else ""] = \
                    dict(user)  # 单请求路径在 job.paths 内
            for item in payload.get("results") or []:
                if isinstance(item, dict) and item.get("path"):
                    snapshot = item.get("user")
                    job.snapshots[str(item["path"])] = dict(snapshot) if isinstance(snapshot, dict) else {}
        if job.remaining <= 0:
            self._publish_metadata(job)

    def _publish_metadata(self, job: _MetadataJob) -> None:
        if job.failures:
            self.metadata_failed.emit(list(job.paths), dict(job.patch),
                                     "；".join(sorted(set(job.failures))))
        else:
            self.metadata_applied.emit(list(job.paths), dict(job.patch), "已保存")
            if job.snapshots:
                self.metadata_snapshot.emit(dict(job.snapshots))

    # -- 文件任务（B07） ---------------------------------------------------
    TASK_POLL_INTERVAL_MS = 400
    TASK_POLL_MAX_ATTEMPTS = 300

    def run_task(
        self, operation: str, targets: list[str], *, policy: str = "error",
        options: dict[str, Any] | None = None,
    ) -> None:
        """提交文件任务并轮询到终态：mkdir/rename/copy/move/trash/restore/purge。

        不在这里做成功判断——逐项结果由 /api/tasks/status 的 items 决定。
        """
        if self._closed or not targets:
            return
        request = self._make_post("/api/tasks/start")
        if request is None:
            self.task_failed.emit("素材服务尚未就绪，无法执行文件操作。")
            return
        payload = {"operation": operation, "targets": targets,
                   "policy": policy, "options": dict(options or {})}
        reply = self._network.post(request, json.dumps(payload).encode("utf-8"))
        reply.finished.connect(lambda r=reply: self._finish_task_start(r))

    def _finish_task_start(self, reply: QNetworkReply) -> None:
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        raw = bytes(reply.readAll())
        error = reply.error()
        reply.deleteLater()
        if self._closed:
            return
        try:
            payload: Any = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            payload = None
        if status != 200 or error != QNetworkReply.NetworkError.NoError \
                or not isinstance(payload, dict) or not payload.get("success"):
            message = payload.get("message") if isinstance(payload, dict) else None
            self.task_failed.emit(str(message or reply.errorString() or "文件操作未能提交"))
            return
        job = payload.get("job") or {}
        job_id = str(job.get("job_id") or "")
        if not job_id:
            self.task_failed.emit("素材服务未返回任务编号")
            return
        self.task_started.emit(dict(job))
        self._start_task_poll(job_id)

    def _start_task_poll(self, job_id: str) -> None:
        """[FR-16] 按 job_id 注册轮询：多个活动任务各自独立，互不覆盖。"""
        self._task_polls[job_id] = {"attempts": 0, "reply": None}
        if self._task_poll_timer is None:
            self._task_poll_timer = QTimer(self)
            self._task_poll_timer.timeout.connect(self._poll_task_once)
        self._task_poll_timer.start(self.TASK_POLL_INTERVAL_MS)
        self._poll_task_once()

    def _poll_task_once(self) -> None:
        if self._closed or not self._task_polls:
            return
        for job_id in list(self._task_polls):
            state = self._task_polls.get(job_id)
            if state is None or state["reply"] is not None:
                continue  # 该任务上次轮询未回：让 HTTP 自己返回，不堆叠
            state["attempts"] += 1
            if state["attempts"] > self.TASK_POLL_MAX_ATTEMPTS:
                self._stop_task_poll(job_id)
                self.task_failed.emit(
                    f"文件任务 {job_id[:8]} 超时，请稍后在完整 Files 中查看结果。")
                continue
            request = self._make_request("/api/tasks/status", {"job_id": job_id})
            if request is None:
                self._stop_task_poll(job_id)
                continue
            reply = self._network.get(request)
            state["reply"] = reply
            reply.finished.connect(lambda r=reply, j=job_id: self._finish_task_poll(r, j))
        if not self._task_polls and self._task_poll_timer is not None:
            self._task_poll_timer.stop()

    def _finish_task_poll(self, reply: QNetworkReply, job_id: str) -> None:
        state = self._task_polls.get(job_id)
        if state is not None and state["reply"] is reply:
            state["reply"] = None
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        raw = bytes(reply.readAll())
        error = reply.error()
        reply.deleteLater()
        if self._closed:
            return
        if state is None:
            return  # 已停止跟踪（超时/关闭）：丢弃
        try:
            payload: Any = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return
        job = payload.get("job") if isinstance(payload, dict) else None
        if status != 200 or error != QNetworkReply.NetworkError.NoError or not isinstance(job, dict):
            self._stop_task_poll(job_id)
            self.task_failed.emit("无法读取文件任务进度。")
            return
        if str(job.get("job_id") or "") != job_id:
            return
        if job.get("status") in ("completed", "failed", "cancelled"):
            self._stop_task_poll(job_id)
            self.task_finished.emit(dict(job))
            return
        self.task_started.emit(dict(job))  # 复用为进度快照，UI 只读 completed/total

    def cancel_task(self, job_id: str) -> None:
        if self._closed or not job_id:
            return
        request = self._make_post("/api/tasks/cancel")
        if request is None:
            return
        reply = self._network.post(request, json.dumps({"job_id": job_id}).encode("utf-8"))
        reply.finished.connect(lambda r=reply: r.deleteLater())

    def active_task_jobs(self) -> list[str]:
        """[FR-16] 当前仍在轮询的任务 id（任务列表/取消入口用）。"""
        return list(self._task_polls)

    def _stop_task_poll(self, job_id: str | None = None) -> None:
        """停止指定任务的轮询；None = 全部（关闭时）。"""
        targets = list(self._task_polls) if job_id is None else [job_id]
        for target in targets:
            state = self._task_polls.pop(target, None)
            if state is not None and state["reply"] is not None:
                state["reply"].abort()
                state["reply"].deleteLater()
        if not self._task_polls and self._task_poll_timer is not None:
            self._task_poll_timer.stop()

    # -- 常用位置 / 扫描状态（F11 / B10） ---------------------------------
    def load_libraries(self) -> None:
        if self._closed:
            return
        request = self._make_request("/api/library/list", {})
        if request is None:
            return
        reply = self._network.get(request)
        reply.finished.connect(lambda r=reply: self._finish_libraries(r))

    def _finish_libraries(self, reply: QNetworkReply) -> None:
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        raw = bytes(reply.readAll())
        error = reply.error()
        reply.deleteLater()
        if self._closed or status != 200 or error != QNetworkReply.NetworkError.NoError:
            return
        try:
            payload: Any = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return
        if isinstance(payload, dict):
            payload = payload.get("libraries") or payload.get("items") or []
        if isinstance(payload, list):
            self.libraries_loaded.emit(payload)

    def pin_library(self, path: str) -> None:
        self._post_library("/api/library/add", {"path": path}, "已固定为常用位置")

    def unpin_library(self, path: str) -> None:
        self._post_library("/api/library/unpin", {"path": path}, "已取消固定")

    def _post_library(self, endpoint: str, payload: dict[str, Any], ok_message: str) -> None:
        request = self._make_post(endpoint)
        if request is None:
            self.library_changed.emit(False, "素材服务尚未就绪")
            return
        reply = self._network.post(request, json.dumps(payload).encode("utf-8"))
        reply.finished.connect(lambda r=reply: self._finish_library_change(r, ok_message))

    def _finish_library_change(self, reply: QNetworkReply, ok_message: str) -> None:
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        raw = bytes(reply.readAll())
        error = reply.error()
        reply.deleteLater()
        if self._closed:
            return
        try:
            payload: Any = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            payload = None
        ok = (status == 200 and error == QNetworkReply.NetworkError.NoError
              and isinstance(payload, dict) and payload.get("success") is not False)
        message = ok_message if ok else str(
            (payload or {}).get("message") if isinstance(payload, dict) else "操作失败")
        self.library_changed.emit(bool(ok), message)
        if ok:
            self.load_libraries()

    def load_scan_status(self) -> None:
        if self._closed:
            return
        request = self._make_request("/api/scan/status", {})
        if request is None:
            return
        reply = self._network.get(request)
        reply.finished.connect(lambda r=reply: self._finish_scan_status(r))

    def _finish_scan_status(self, reply: QNetworkReply) -> None:
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        raw = bytes(reply.readAll())
        error = reply.error()
        reply.deleteLater()
        if self._closed:
            return
        try:
            payload: Any = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return
        if status != 200 or error != QNetworkReply.NetworkError.NoError or not isinstance(payload, dict):
            self.scan_failed.emit("无法读取索引状态")
            return
        self.scan_status_loaded.emit(dict(payload))
        self._sync_scan_poll(bool(payload.get("running")))

    def _sync_scan_poll(self, running: bool) -> None:
        if running and self._scan_poll_timer is None:
            self._scan_poll_timer = QTimer(self)
            self._scan_poll_timer.timeout.connect(self.load_scan_status)
            self._scan_poll_timer.start(1000)
        elif not running and self._scan_poll_timer is not None:
            self._scan_poll_timer.stop()
            self._scan_poll_timer = None

    def start_scan(self) -> None:
        request = self._make_post("/api/scan/start")
        if request is None:
            self.scan_failed.emit("素材服务尚未就绪")
            return
        reply = self._network.post(request, b"{}")
        reply.finished.connect(lambda r=reply: self._finish_scan_action(r, "索引已开始"))

    def cancel_scan(self) -> None:
        request = self._make_post("/api/scan/cancel")
        if request is None:
            return
        reply = self._network.post(request, b"{}")
        reply.finished.connect(lambda r=reply: self._finish_scan_action(r, "已请求取消索引"))

    def _finish_scan_action(self, reply: QNetworkReply, ok_message: str) -> None:
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        error = reply.error()
        reply.deleteLater()
        if self._closed:
            return
        if status not in (200, 409) or error != QNetworkReply.NetworkError.NoError:
            self.scan_failed.emit("索引操作失败")
            return
        if status == 409:
            self.scan_failed.emit("索引已在运行，或当前没有可取消的任务")
        self.load_scan_status()

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

    # -- 批量重命名（F13） -------------------------------------------------
    def preview_batch_rename(self, files: list[str], mode: str, params: dict[str, Any],
                             session_id: str = "") -> None:
        """先预览再执行：预览不落盘，冲突由服务标记。

        [FR-13] session_id 贯穿请求/响应：多个非模态窗口共享广播信号，
        没有会话身份就会收到彼此的预览/执行结果。
        """
        request = self._make_post("/api/batch/rename/preview")
        if request is None:
            self.batch_rename_preview_failed.emit("素材服务尚未就绪", session_id)
            return
        payload = {"files": list(files), "mode": mode, "params": dict(params or {})}
        reply = self._network.post(request, json.dumps(payload).encode("utf-8"))
        reply.finished.connect(
            lambda r=reply, s=session_id: self._finish_batch_rename_preview(r, s))

    def _finish_batch_rename_preview(self, reply: QNetworkReply, session_id: str) -> None:
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        raw = bytes(reply.readAll())
        error = reply.error()
        reply.deleteLater()
        if self._closed:
            return
        try:
            payload: Any = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            payload = None
        if status != 200 or error != QNetworkReply.NetworkError.NoError or not isinstance(payload, dict):
            message = (payload or {}).get("message") if isinstance(payload, dict) else None
            self.batch_rename_preview_failed.emit(str(message or "批量改名预览失败"),
                                                  session_id)
            return
        payload["session_id"] = session_id
        self.batch_rename_preview_ready.emit(dict(payload))

    def execute_batch_rename(self, files: list[str], mode: str, params: dict[str, Any],
                             session_id: str = "") -> None:
        request = self._make_post("/api/batch/rename")
        if request is None:
            self.batch_rename_finished.emit({"success": False,
                                             "message": "素材服务尚未就绪",
                                             "session_id": session_id})
            return
        payload = {"files": list(files), "mode": mode, "params": dict(params or {})}
        reply = self._network.post(request, json.dumps(payload).encode("utf-8"))
        reply.finished.connect(
            lambda r=reply, s=session_id: self._finish_batch_rename(r, s))

    def _finish_batch_rename(self, reply: QNetworkReply, session_id: str = "") -> None:
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        raw = bytes(reply.readAll())
        error = reply.error()
        reply.deleteLater()
        if self._closed:
            return
        try:
            payload: Any = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            payload = None
        if not isinstance(payload, dict):
            payload = {"success": False, "message": "批量改名失败"}
        if (status != 200 or error != QNetworkReply.NetworkError.NoError
                or payload.get("success") is not True):
            payload = {"success": False,
                       "message": str(payload.get("message") or "批量改名失败")}
        payload["session_id"] = session_id
        self.batch_rename_finished.emit(dict(payload))

    # -- 并排比较（F15 / FR-15/17） ----------------------------------------
    def load_compare_images(self, paths: list[str], session_id: str = "") -> None:
        """[FR-15] 会话隔离：每个比较窗口一个独立队列与在途请求。

        此前全局单队列 + 单 reply——两个窗口互相吞请求，关闭也不取消。
        """
        queue = deque(list(paths)[:4])
        if not queue:
            self.compare_set_finished.emit(session_id)
            return
        self._compare_sessions[session_id] = queue
        self._pump_compare(session_id)

    def cancel_compare(self, session_id: str = "") -> None:
        """[FR-17] 关闭窗口即取消该会话在途请求，清空剩余队列。"""
        self._compare_sessions.pop(session_id, None)
        entry = self._compare_replies.pop(session_id, None)
        if entry is not None:
            reply, _state = entry
            reply.abort()
            reply.deleteLater()

    def _pump_compare(self, session_id: str) -> None:
        queue = self._compare_sessions.get(session_id)
        if queue is None:
            return  # 会话已取消/关闭
        if session_id in self._compare_replies:
            return  # 该会话上一张未传完
        if not queue:
            self._compare_sessions.pop(session_id, None)
            self.compare_set_finished.emit(session_id)
            return
        path = queue.popleft()
        request = self._make_request("/api/file", {"path": path})
        if request is None:
            self.compare_image_failed.emit(path, "素材服务尚未就绪", session_id)
            QTimer.singleShot(0, lambda s=session_id: self._pump_compare(s))
            return
        reply = self._network.get(request)
        reply.setReadBufferSize(self.MAX_PREVIEW_BYTES + 1)
        state = _PreviewRead(path=path)
        self._compare_replies[session_id] = (reply, state)
        reply.readyRead.connect(lambda r=reply, st=state: self._read_preview(r, st))
        reply.finished.connect(
            lambda r=reply, st=state, s=session_id: self._finish_compare(r, st, s))

    def _finish_compare(self, reply: QNetworkReply, state: _PreviewRead,
                        session_id: str) -> None:
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        error = reply.error()
        data = bytes(state.data)
        reply.deleteLater()
        self._compare_replies.pop(session_id, None)
        if self._closed:
            return
        if session_id not in self._compare_sessions:
            return  # [FR-17] 会话已关闭：结果一律丢弃
        if state.oversized:
            self.compare_image_failed.emit(state.path, "文件过大，无法比较", session_id)
        elif status == 200 and error == QNetworkReply.NetworkError.NoError and data:
            self.compare_image_loaded.emit(state.path, data, session_id)
        else:
            self.compare_image_failed.emit(state.path, "读取失败", session_id)
        self._pump_compare(session_id)

    # -- 悬停胶片条（F14） -------------------------------------------------
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

    # -- 集合浏览（B11） ---------------------------------------------------
    def load_collections(self) -> None:
        if self._closed:
            return
        request = self._make_request("/api/collections", {})
        if request is None:
            return
        reply = self._network.get(request)
        reply.finished.connect(lambda r=reply: self._finish_collections(r))

    def _finish_collections(self, reply: QNetworkReply) -> None:
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        raw = bytes(reply.readAll())
        error = reply.error()
        reply.deleteLater()
        if self._closed or status != 200 or error != QNetworkReply.NetworkError.NoError:
            return
        try:
            payload: Any = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return
        items = payload.get("collections") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            items = payload if isinstance(payload, list) else None
        if isinstance(items, list):
            self.collections_loaded.emit(items)

    def load_collection(self, collection_id: int) -> None:
        """集合成员与 /api/list 同构；不构建第二套查询语义。"""
        if self._closed:
            return
        # [FR-09] 取消上一个集合请求：连续切换集合时旧响应直接作废
        stale, self._collection_reply = self._collection_reply, None
        if stale is not None:
            stale.abort()
            stale.deleteLater()
        request = self._make_request(f"/api/collections/{int(collection_id)}/items", {})
        if request is None:
            self.collection_failed.emit("素材服务尚未就绪，无法读取集合。")
            return
        reply = self._network.get(request)
        self._collection_reply = reply
        reply.finished.connect(lambda r=reply: self._finish_collection(r))

    def _finish_collection(self, reply: QNetworkReply) -> None:
        if reply is not self._collection_reply:
            reply.deleteLater()
            return  # 已被更新的集合请求/导航作废：一律丢弃
        self._collection_reply = None
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        raw = bytes(reply.readAll())
        error = reply.error()
        reply.deleteLater()
        if self._closed:
            return
        try:
            payload: Any = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            payload = None
        if status != 200 or error != QNetworkReply.NetworkError.NoError \
                or not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
            message = payload.get("error") if isinstance(payload, dict) else None
            self.collection_failed.emit(str(message or "读取集合失败"))
            return
        self.collection_loaded.emit(dict(payload))

    # -- 全库搜索（B05） ---------------------------------------------------
    def search_library(self, query: str, *, limit: int = 200, offset: int = 0) -> None:
        """跨目录搜索：只读索引，不扫盘；结果显式带 total/truncated。

        [FR-09] 与目录列表同一套失效规则：新请求作废旧搜索；切目录
        （load_directory→cancel）也一并作废，晚到的旧响应绝不覆盖新视图。
        """
        if self._closed:
            return
        stale, self._search_reply = self._search_reply, None
        if stale is not None:
            stale.abort()
            stale.deleteLater()
        request = self._make_request("/api/search", {
            "q": str(query or ""), "limit": int(limit), "offset": int(offset)})
        if request is None:
            self.search_failed.emit("素材服务尚未就绪，无法搜索素材库。")
            return
        reply = self._network.get(request)
        self._search_reply = reply
        reply.finished.connect(lambda r=reply: self._finish_search(r))

    def _finish_search(self, reply: QNetworkReply) -> None:
        if reply is not self._search_reply:
            reply.deleteLater()
            return  # [FR-09] 已被更新的搜索/导航作废：一律丢弃
        self._search_reply = None
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        raw = bytes(reply.readAll())
        error = reply.error()
        reply.deleteLater()
        if self._closed:
            return
        try:
            payload: Any = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            payload = None
        if status != 200 or error != QNetworkReply.NetworkError.NoError or not isinstance(payload, dict):
            message = payload.get("error") if isinstance(payload, dict) else None
            self.search_failed.emit(str(message or reply.errorString() or "搜索失败"))
            return
        files = payload.get("files")
        if not isinstance(files, list):
            self.search_failed.emit("素材服务返回的搜索结果格式无效。")
            return
        self.search_loaded.emit(dict(payload))

    # -- 能力清单（B01） ---------------------------------------------------
    def load_capabilities(self) -> None:
        request = self._make_request("/api/capabilities", {})
        if request is None:
            return
        reply = self._network.get(request)
        reply.finished.connect(lambda r=reply: self._finish_capabilities(r))

    def _finish_capabilities(self, reply: QNetworkReply) -> None:
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        raw = bytes(reply.readAll())
        error = reply.error()
        reply.deleteLater()
        if self._closed or status != 200 or error != QNetworkReply.NetworkError.NoError:
            return
        try:
            payload: Any = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return
        items = payload.get("capabilities") if isinstance(payload, dict) else None
        if isinstance(items, list):
            self.capabilities_loaded.emit(items)

    # -- 原生预览取数（B04 / U03） ----------------------------------------
    def load_preview(self, path: str) -> None:
        """只取字节；解码在 UI 侧工作线程完成，主线程不做同步解码。"""
        self.cancel_preview()
        request = self._make_request("/api/file", {"path": path})
        if request is None:
            self.preview_failed.emit(path, "素材服务尚未就绪，无法预览。")
            return
        reply = self._network.get(request)
        reply.setReadBufferSize(self.MAX_PREVIEW_BYTES + 1)
        state = _PreviewRead(path=path)
        self._preview_reply = reply
        self._preview_state = state
        reply.metaDataChanged.connect(lambda r=reply, st=state: self._check_preview_size(r, st))
        reply.readyRead.connect(lambda r=reply, st=state: self._read_current_preview(r, st))
        reply.finished.connect(lambda r=reply, st=state: self._finish_preview(r, st))

    def _check_preview_size(self, reply: QNetworkReply, state: _PreviewRead) -> None:
        if self._closed or reply is not self._preview_reply or state is not self._preview_state:
            return
        size = reply.header(QNetworkRequest.KnownHeaders.ContentLengthHeader)
        if isinstance(size, int) and size > self.MAX_PREVIEW_BYTES:
            state.oversized = True
            reply.abort()

    def _read_current_preview(self, reply: QNetworkReply, state: _PreviewRead) -> None:
        if not self._closed and reply is self._preview_reply and state is self._preview_state:
            self._read_preview(reply, state)

    def _read_preview(self, reply: QNetworkReply, state: _PreviewRead) -> None:
        if state.oversized:
            return
        chunk = reply.read(self.MAX_PREVIEW_BYTES + 1 - len(state.data))
        if chunk:
            state.data.extend(bytes(chunk))
        if len(state.data) > self.MAX_PREVIEW_BYTES:
            state.oversized = True
            reply.abort()

    def _finish_preview(self, reply: QNetworkReply, state: _PreviewRead) -> None:
        # 同路径重开也必须按请求身份丢弃旧 fetch，不能让旧字节成为新解码任务。
        if self._closed or reply is not self._preview_reply or state is not self._preview_state:
            reply.deleteLater()
            return
        self._read_preview(reply, state)
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        error = reply.error()
        error_text = reply.errorString()
        if reply is self._preview_reply:
            self._preview_reply = None
            self._preview_state = None
        reply.deleteLater()
        if self._closed:
            return
        if state.oversized:
            self.preview_failed.emit(state.path, "文件过大，请在完整 Files 中预览。")
        elif (status == 200 and error == QNetworkReply.NetworkError.NoError and state.data):
            self.preview_loaded.emit(state.path, bytes(state.data))
        elif status == 423:
            self.preview_failed.emit(state.path, "目录已锁定，请在完整 Files 中解锁后预览。")
        elif status == 204:
            self.preview_failed.emit(state.path, "该文件没有可预览内容。")
        else:
            self.preview_failed.emit(state.path, error_text or "预览请求失败。")

    def cancel_preview(self) -> None:
        reply, self._preview_reply = self._preview_reply, None
        self._preview_state = None
        if reply is not None:
            reply.abort()

    def cancel(self) -> None:
        self._generation += 1
        reply, self._reply = self._reply, None
        thumbnails = list(self._thumbnail_replies)
        self._thumbnail_replies.clear()
        self._thumbnail_queue.clear()
        self._thumbnail_attempted.clear()
        self._thumbnail_versions.clear()
        # [FR-09] 目录切换同样作废在途搜索/集合请求，防晚到旧响应覆盖新目录
        for attr in ("_search_reply", "_collection_reply"):
            stale = getattr(self, attr, None)
            if stale is not None:
                stale.abort()
                stale.deleteLater()
                setattr(self, attr, None)
        if reply is not None:
            reply.abort()
        for thumbnail in thumbnails:
            thumbnail.abort()

    def shutdown(self) -> None:
        # [FR-17] 先标记关闭，再取消全部在途请求：关闭后的完成回调
        # 一律静默丢弃（compare/metadata 此前只清引用不 abort，请求仍
        # 会跑完并试图发信号）
        self._closed = True
        for reply in list(self._plugin_replies):
            reply.abort()
            reply.deleteLater()
        self._plugin_replies.clear()
        for session_id in list(self._compare_sessions):
            self.cancel_compare(session_id)
        self._compare_sessions.clear()
        for reply, _state in list(self._compare_replies.values()):
            reply.abort()
            reply.deleteLater()
        self._compare_replies.clear()
        for reply in list(self._strip_replies):
            reply.abort()
        self._strip_replies.clear()
        self._stop_task_poll()
        self._sync_scan_poll(False)
        self.cancel()
        self.cancel_preview()
        for reply in list(self._metadata_replies):
            reply.abort()
            reply.deleteLater()
        self._metadata_replies.clear()
        trees = list(self._tree_replies)
        self._tree_queue.clear()
        self._tree_replies.clear()
        self._tree_pending_paths.clear()
        for tree in trees:
            tree.abort()
