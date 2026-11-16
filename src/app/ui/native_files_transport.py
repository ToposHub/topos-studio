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

from PyQt6.QtCore import QObject, QUrl, pyqtSignal
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest


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

    MAX_THUMBNAIL_CONCURRENCY = 4
    MAX_THUMBNAIL_QUEUE = 128
    MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024

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

    def cancel(self) -> None:
        self._generation += 1
        reply, self._reply = self._reply, None
        thumbnails = list(self._thumbnail_replies)
        self._thumbnail_replies.clear()
        self._thumbnail_queue.clear()
        self._thumbnail_attempted.clear()
        self._thumbnail_versions.clear()
        if reply is not None:
            reply.abort()
        for thumbnail in thumbnails:
            thumbnail.abort()

    def shutdown(self) -> None:
        self._closed = True
        self.cancel()
