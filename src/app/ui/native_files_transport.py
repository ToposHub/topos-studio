"""Asynchronous UI transport for the native Files panel.

D1 换轨：目录/树/搜索/集合/元数据/任务/库/扫描/批改名走 **C2 FilesFacade**
（同进程、有界 worker、代际作废），预览/比较为本地直读；这些路由不再依赖
旧 HTTP 服务。缩略图/胶片条走共享解码器与有界本地任务；工具由原生命令宿主启动。

目录结果先进入 GUI 线程队列，再核对导航代际，避免已入队的旧请求回写面板。
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from PyQt6.QtCore import QObject, QTimer, Qt, pyqtSignal

from src.app.ui.native_files_locations import mounted_data_volumes
from src.features.files.contracts import (
    FilesErrorCode,
    QueryRequest,
    identity_key,
    is_valid_folder_name,
)
from src.features.files.repository import FilesDataLocator
from src.features.files.repository.catalog import FilesCatalog, nfc
from src.features.files.services.facade import FacadeOutcome, FilesFacade, read_file_bytes
from src.features.files.transactions import CancelToken
from src.shared.i18n.i18n import I18N
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
    # Apply pagination after the panel has installed the page's rows.
    page_meta = pyqtSignal(str, int, bool)
    thumbnail_loaded = pyqtSignal(str, bytes)
    thumbnail_failed = pyqtSignal(str)
    thumbnail_pending = pyqtSignal(str)
    tree_loaded = pyqtSignal(str, list)
    tree_failed = pyqtSignal(str, str)
    volumes_loaded = pyqtSignal(tuple)
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
    folder_created = pyqtSignal(str, str, str)
    search_loaded = pyqtSignal(dict)
    search_failed = pyqtSignal(str)
    collections_loaded = pyqtSignal(list)
    collection_loaded = pyqtSignal(dict)
    collection_failed = pyqtSignal(str)
    collection_changed = pyqtSignal(bool, str)  # D2 集合写操作反馈
    trash_loaded = pyqtSignal(list)             # D2 回收站清单
    trash_failed = pyqtSignal(str)
    recovery_loaded = pyqtSignal(list)          # D5' 上次运行遗留的未终态任务
    recovery_applied = pyqtSignal(dict)         # D5' 恢复判定结果
    libraries_loaded = pyqtSignal(list)
    library_changed = pyqtSignal(bool, str)
    library_registry_changed = pyqtSignal()
    scan_status_loaded = pyqtSignal(dict)
    scan_failed = pyqtSignal(str)
    ai_library_results = pyqtSignal(str, list, str)
    ai_library_search_note = pyqtSignal(str, str)
    ai_library_embedding_consent_changed = pyqtSignal()
    ai_library_copy_finished = pyqtSignal(str)
    ai_library_index_status = pyqtSignal(dict)
    ai_library_settings_loaded = pyqtSignal(dict)
    ai_library_settings_saved = pyqtSignal(str)
    ai_analysis_marks_loaded = pyqtSignal(str, list)
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
    _thumbnail_done = pyqtSignal(str, object)
    _strip_done = pyqtSignal(str, object)
    _tree_done = pyqtSignal(object, str)
    _volumes_done = pyqtSignal(object)
    _directory_done = pyqtSignal(int, str, object)
    _ai_library_index_done = pyqtSignal(object)
    _ai_library_settings_done = pyqtSignal(object, object, object)
    _ai_analysis_marks_done = pyqtSignal(object, str, int)

    MAX_THUMBNAIL_CONCURRENCY = 4
    MAX_THUMBNAIL_QUEUE = 128
    MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024
    MAX_TREE_QUEUE = 64
    MAX_PREVIEW_BYTES = 64 * 1024 * 1024

    def __init__(
        self, server_url: Callable[[], str], token: str | None,
        parent: QObject | None = None, *, facade: FilesFacade | None = None,
    ) -> None:
        super().__init__(parent)
        # 数据面 runtime（懒构造：首用时建库/开连接，构造失败可诊断）
        self._facade: FilesFacade | None = facade
        self._owned_catalog: FilesCatalog | None = None
        self._facade_error: str | None = None
        self._media_cancel = threading.Event()
        self._drained = threading.Event()
        self._thumbnail_done.connect(self._finish_local_thumbnail, Qt.ConnectionType.QueuedConnection)
        self._strip_done.connect(self._finish_local_strip, Qt.ConnectionType.QueuedConnection)
        self._tree_done.connect(self._finish_tree, Qt.ConnectionType.QueuedConnection)
        self._volumes_done.connect(self._finish_volumes, Qt.ConnectionType.QueuedConnection)
        self._directory_done.connect(self._finish_directory, Qt.ConnectionType.QueuedConnection)
        self._ai_library_index_done.connect(self._finish_ai_library_index, Qt.ConnectionType.QueuedConnection)
        self._ai_library_settings_done.connect(self._finish_ai_library_settings, Qt.ConnectionType.QueuedConnection)
        self._ai_analysis_marks_done.connect(self._finish_ai_analysis_marks, Qt.ConnectionType.QueuedConnection)
        self._generation = 0        # HTTP 缩略图代际（沿用原语义）
        self._nav_generation = 0    # 数据面导航代际：cancel() 后迟到结果作废
        self._preview_seq = 0       # 预览请求身份：同路径重开/取消都作废旧读取
        self._closed = False
        self._running_jobs: dict[str, CancelToken] = {}
        self._scan_cancel: CancelToken | None = None
        self._ai_library_index_cancel: CancelToken | None = None
        self._ai_library_index_state: dict[str, Any] = {}
        self._ai_library_settings_request_id = ""
        self._ai_analysis_marks_request_id = ""
        self._ai_library_search_cancel: CancelToken | None = None
        self._ai_analysis_marks_cancel: CancelToken | None = None
        self._scan_state: dict[str, Any] = {"running": False, "phase": "空闲"}
        self._thumbnail_queue: deque[tuple[str, int]] = deque()
        self._thumbnail_dispatch_paused = False
        self._thumbnail_attempted: set[tuple[str, int]] = set()
        self._thumbnail_replies: dict[str, _ThumbnailRead] = {}
        self._thumbnail_versions: dict[str, int] = {}
        self._tree_pending_paths: dict[str, str] = {}
        self._volumes_pending = False
        # [FR-15/17] 比较会话化：session_id → 剩余队列 / 在途标记
        self._compare_sessions: dict[str, deque[str]] = {}
        self._compare_busy: set[str] = set()
        self._compare_seq = 0
        self._rename_plan_ids: dict[str, str] = {}  # [FR-13] 会话 → 已确认计划
        self._strip_replies: dict[str, str] = {}

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
            self._facade = FilesFacade(catalog)
            self._owned_catalog = catalog
        except Exception as exc:  # CatalogSchemaMismatch 等：如实上报
            self._facade_error = f"{exc.__class__.__name__}: {exc}"
        return self._facade

    def library_revision(self) -> int | None:
        """[F3] 当前素材库 revision（facade 未就绪返回 None，不冒充 0）。"""
        facade = self._facade
        if facade is None:
            return None
        try:
            return int(facade.revision)
        except Exception:
            return None

    def _runtime_error_message(self) -> str:
        return f"素材数据不可用：{self._facade_error or '未知错误'}"

    def launch_plugin(self, plugin_id: str, endpoint: str, payload: dict[str, Any]) -> bool:
        """Retired HTTP entry: tools must use the registered native command."""
        if not self._closed:
            self.plugin_launch_finished.emit(plugin_id, False, "请通过原生插件命令打开工具。")
        return False

    # ------------------------------------------------------------ AI 素材库
    def search_ai_library(
        self, request_id: str, query: str, library_id: str | None = None, *,
        folder: str | None = None, kinds: tuple[str, ...] | None = None, recursive: bool = True,
        only_unanalyzed: bool = False, semantic: bool = True,
    ) -> None:
        if self._closed:
            return
        if self._ai_library_search_cancel is not None:
            self._ai_library_search_cancel.request()
        facade = self._runtime()
        if facade is None:
            self.ai_library_results.emit(request_id, [], self._runtime_error_message())
            return
        cancel = self._ai_library_search_cancel = CancelToken()

        def work() -> tuple[list[dict[str, Any]], str]:
            from src.app.services.ai_library_search import search_library

            return search_library(facade, query, library_id=library_id, folder=folder, kinds=kinds,
                                  recursive=recursive, only_unanalyzed=only_unanalyzed,
                                  semantic=semantic, allow_remote=semantic, cancel=cancel)

        def deliver(outcome: FacadeOutcome) -> None:
            if not self._closed and not cancel.requested:
                if outcome.error is not None:
                    self.ai_library_results.emit(request_id, [], str(outcome.error.message))
                else:
                    hits, note = outcome.result
                    self.ai_library_results.emit(request_id, hits, "")
                    self.ai_library_search_note.emit(request_id, note)

        facade.run(key="ai-library-search", request_id=request_id, listener=deliver, work=work)

    def load_ai_analysis_marks(self, path: str, paths: list[str]) -> None:
        """Resolve card badges independently of directory and thumbnail delivery."""
        if self._closed:
            return
        if self._ai_analysis_marks_cancel is not None:
            self._ai_analysis_marks_cancel.request()
        request_id = self._ai_analysis_marks_request_id = uuid.uuid4().hex
        generation = self._nav_generation
        candidates = tuple(dict.fromkeys(paths))
        facade = self._runtime() if candidates else None
        if facade is None:
            self.ai_analysis_marks_loaded.emit(path, [])
            return
        cancel = self._ai_analysis_marks_cancel = CancelToken()

        def deliver(outcome: FacadeOutcome) -> None:
            if not self._closed and not cancel.requested:
                self._ai_analysis_marks_done.emit(outcome, path, generation)

        facade.run(key="ai-library-marks", request_id=request_id, listener=deliver,
                   work=lambda: facade.ai_library_analyzed_paths(candidates, cancel=cancel))

    def _finish_ai_analysis_marks(self, outcome: FacadeOutcome, path: str, generation: int) -> None:
        if (self._closed or generation != self._nav_generation
                or outcome.request_id != self._ai_analysis_marks_request_id):
            return
        self.ai_analysis_marks_loaded.emit(path, [] if outcome.error else list(outcome.result))

    def copy_to_ai_library(self, sources: list[str], destination: str) -> None:
        facade = self._runtime()
        if facade is None:
            self.ai_library_copy_finished.emit(self._runtime_error_message())
            return

        def deliver(outcome: FacadeOutcome) -> None:
            if self._closed:
                return
            if outcome.error is not None:
                self.ai_library_copy_finished.emit(str(outcome.error.message))
                self.load_libraries()
                self.library_registry_changed.emit()
                return
            items = outcome.result.items
            copied = sum(item.ok for item in items)
            message = f"已复制 {copied} 项，失败 {len(items) - copied} 项。"
            if copied != len(items):
                failed = next(item for item in items if not item.ok)
                message += f" {failed.message or failed.error_code}"
            self.ai_library_copy_finished.emit(message)
            self.load_libraries()
            self.library_registry_changed.emit()

        facade.run(key="ai-library-copy", request_id=uuid.uuid4().hex,
                   work=lambda: facade.copy_to_ai_library(sources, destination), listener=deliver)

    def start_ai_library_index(self, library_id: str | None = None, *,
                               folder: str | None = None, force: bool = False,
                               only_unanalyzed: bool = False, kinds: tuple[str, ...] | None = None,
                               recursive: bool = True) -> None:
        if self._ai_library_index_cancel is not None:
            return
        if force and only_unanalyzed:
            self._ai_library_index_state = {"error": I18N.tr(
                "files.ai_library.conflicting_scan_options", "强制重分析与仅分析待处理素材不能同时启用。")}
            self.ai_library_index_status.emit(dict(self._ai_library_index_state))
            return
        facade = self._runtime()
        if facade is None:
            self.ai_library_index_status.emit({"error": self._runtime_error_message()})
            return
        cancel = CancelToken()
        self._ai_library_index_cancel = cancel
        self._ai_library_index_state = {"running": True, "done": 0, "total": 0}
        self.ai_library_index_status.emit(dict(self._ai_library_index_state))

        def work() -> dict[str, Any]:
            from contextlib import ExitStack

            from src.app.services.ai_library_analysis import (
                LibraryAnalyzer, MediaRoute, active_media_routes, configured_library_routes,
            )

            libraries = facade.libraries.list_libraries()
            if not libraries:
                raise ValueError("先添加一个素材目录。")
            if library_id is None and folder is not None:
                raise ValueError("请先选择子目录所属的素材库。")
            roots = {record.library_id: record.root for record in libraries
                     if library_id is None or record.library_id == library_id}
            policies = {owner_id: facade.ai_library_analysis_policy(owner_id, folder=folder) for owner_id in roots}
            if library_id is not None and library_id not in policies:
                raise ValueError("该素材库已移除，请重新选择。")
            policies = {
                owner_id: replace(
                    policy, kinds=tuple(kind for kind in policy.kinds if kinds is None or kind in kinds),
                    recursive=policy.recursive and recursive,
                )
                for owner_id, policy in policies.items()
            }
            explicit_routes: dict[str, tuple[MediaRoute, ...]] = {}
            for owner_id, policy in policies.items():
                if not policy.kinds or policy.mode != "profiles" or policy.models is None:
                    continue
                routes, missing = configured_library_routes(policy)
                if missing:
                    labels = {"image": "图片", "video": "视频", "audio": "音频"}
                    return {"error": I18N.tr(
                        "files.ai_library.model_setup_required", "请先配置用于分析{kinds}的模型。",
                        kinds="、".join(I18N.tr(f"files.native.kind.{kind}", labels[kind]) for kind in missing)),
                        "model_setup_required": True, "kind": missing[0]}
                blocked = [route.profile.display_name for route in routes if route.requires_upload and (
                    not route.profile.allows_media_upload or route.approval not in policy.approved_routes
                )]
                if blocked:
                    return {"error": I18N.tr(
                        "files.ai_library.model_upload_required", "此目录尚未授权向以下模型发送素材：{models}",
                        models="、".join(dict.fromkeys(blocked)))}
                explicit_routes[owner_id] = routes

            analyzer = LibraryAnalyzer(cancel=cancel)
            last_update = 0.0
            policy_messages: list[str] = []

            def additional_analyzers(owner_id: str) -> tuple[LibraryAnalyzer, ...]:
                # Libraries are scanned sequentially. Release the previous
                # directory's MCP process before opening the next one.
                sessions.close()
                policy = policies.get(owner_id)
                if policy is None:
                    return ()
                if policy.mode != "profiles":
                    return ()
                profiles = []
                routes = explicit_routes.get(owner_id)
                if routes is None:
                    routes = tuple(active_media_routes().values())
                if policy.models is None and not routes:
                    policy_messages.append("有目录选择了已绑定模型，但视觉和语音转文字均未绑定可用配置。")
                for route in routes:
                    if route.requires_upload and (
                        not route.profile.allows_media_upload or route.approval not in policy.approved_routes
                    ):
                        policy_messages.append(f"{route.profile.display_name} 未获得此目录的当前上传授权。")
                        continue
                    analyzer = LibraryAnalyzer(
                        cancel=cancel, mode=route.capability, route=route,
                        read_policy=lambda: facade.ai_library_analysis_policy(owner_id, folder=folder),
                        scope_path=folder or roots[owner_id],
                    )
                    sessions.callback(analyzer.close)
                    profiles.append(analyzer)
                return tuple(profiles)

            def progress(done: int, total: int) -> None:
                nonlocal last_update
                now = time.monotonic()
                if not self._closed and (now - last_update >= 0.15 or done == total):
                    last_update = now
                    self.ai_library_index_status.emit({"running": True, "done": done, "total": total})

            with ExitStack() as sessions:
                per_library_scope = {owner_id: (policy.kinds, policy.recursive) for owner_id, policy in policies.items()}
                report = facade.build_ai_library(
                    analyzer, revision=analyzer.revision, cancel=cancel, progress=progress,
                    additional_analyzers=additional_analyzers, library_id=library_id,
                    folder=folder, kinds=kinds, recursive=recursive, force=force,
                    per_library_scope=per_library_scope, only_unanalyzed=only_unanalyzed,
                )
            messages = {item["message"] for item in report["warnings"]}
            report["warnings"].extend(
                {"message": message} for message in dict.fromkeys(policy_messages) if message not in messages
            )
            if not cancel.requested and not report.get("error"):
                embedding_report = self._build_library_embeddings(
                    facade, library_id, folder=folder, kinds=kinds, recursive=recursive, cancel=cancel,
                    per_library_scope=per_library_scope, explicit=False,
                )
                if embedding_report:
                    report["vectors"] = embedding_report
            return report

        facade.run(key="ai-library-index", request_id=uuid.uuid4().hex, work=work,
                   listener=lambda outcome: self._ai_library_index_done.emit(outcome) if not self._closed else None)

    def _build_library_embeddings(
        self, facade: FilesFacade, library_id: str | None, *, folder: str | None,
        kinds: tuple[str, ...] | None, recursive: bool, cancel: CancelToken, explicit: bool,
        per_library_scope: dict[str, tuple[tuple[str, ...], bool]] | None = None,
        approval: str | None = None, expected_scope_stamp: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        from src.app.services.ai_library_embeddings import active_library_embedding_route, library_embedder
        from src.shared.ai_models.generation.media_inference import MediaInferenceError

        try:
            route = active_library_embedding_route()
        except MediaInferenceError as exc:
            return {"error": str(exc)}
        if route is None:
            return {"model_setup_required": True, "error": I18N.tr(
                "files.ai_library.vector_setup_required", "请先在首选项中配置并设为默认向量模型。")} if explicit else {}
        if approval is not None:
            if library_id is None or not route.requires_upload or route.approval != approval:
                return {"error": I18N.tr("files.ai_library.vector_route_changed", "向量模型已更换，请刷新后重新确认。")}
            facade.set_ai_library_embedding_consent(library_id, approval, folder=folder,
                                                     expected_scope_stamp=expected_scope_stamp)
        totals: dict[str, Any] = {"embedded": 0, "skipped": 0, "pending": 0, "cancelled": False}
        processed_scope = False
        for library in facade.libraries.list_libraries():
            if cancel.requested or (library_id is not None and library.library_id != library_id):
                continue
            owner = library.library_id
            if per_library_scope is not None and owner not in per_library_scope:
                continue
            owner_kinds, owner_recursive = (per_library_scope or {}).get(owner, (kinds, recursive))
            if owner_kinds == ():
                continue
            embedder = library_embedder(facade, owner, folder=folder, route=route)
            if embedder is None or not embedder.is_authorized():
                continue
            processed_scope = True
            last_update = 0.0

            def progress(done: int, total: int) -> None:
                nonlocal last_update
                now = time.monotonic()
                if not self._closed and (now - last_update >= 0.15 or done == total):
                    last_update = now
                    self.ai_library_index_status.emit({
                        "running": True, "phase": "vectors", "done": done, "total": total,
                    })

            try:
                current = facade.build_ai_library_embeddings(
                    embedder, model_key=embedder.revision, library_id=owner, folder=folder,
                    kinds=owner_kinds, recursive=owner_recursive, cancel=cancel, progress=progress,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                totals["error"] = str(exc)
                break
            for key in ("embedded", "skipped", "pending"):
                totals[key] += current.get(key, 0)
            if current.get("error"):
                totals["error"] = current["error"]
                break
        totals["cancelled"] = cancel.requested
        if not processed_scope and not cancel.requested:
            return {"error": I18N.tr("files.ai_library.vector_scope_required",
                                     "请先选择素材库或文件夹并授权向量服务。")} if explicit else {}
        return totals

    def start_ai_library_embeddings(
        self, library_id: str | None, *, folder: str | None, kinds: tuple[str, ...], recursive: bool,
        approval: str | None = None, expected_scope_stamp: tuple[str, str] | None = None,
    ) -> None:
        if self._closed or self._ai_library_index_cancel is not None:
            return
        facade = self._runtime()
        if facade is None:
            self.ai_library_index_status.emit({"error": self._runtime_error_message()})
            return
        cancel = self._ai_library_index_cancel = CancelToken()
        self._ai_library_index_state = {"running": True, "phase": "vectors", "done": 0, "total": 0}
        self.ai_library_index_status.emit(dict(self._ai_library_index_state))

        def work() -> dict[str, Any]:
            return {"vectors": self._build_library_embeddings(
                facade, library_id, folder=folder, kinds=kinds, recursive=recursive, cancel=cancel,
                approval=approval, expected_scope_stamp=expected_scope_stamp, explicit=True,
            )}

        facade.run(key="ai-library-index", request_id=uuid.uuid4().hex, work=work,
                   listener=lambda outcome: self._ai_library_index_done.emit(outcome) if not self._closed else None)

    def revoke_ai_library_embedding_consent(self, library_id: str, *, folder: str | None) -> None:
        facade = self._runtime()
        if facade is None:
            self.ai_library_settings_loaded.emit({"error": self._runtime_error_message()})
            return

        def deliver(outcome: FacadeOutcome) -> None:
            if self._closed:
                return
            if outcome.error is not None:
                self.ai_library_settings_loaded.emit({"error": str(outcome.error.message), "library_id": library_id,
                                                      "folder": folder})
            else:
                self.ai_library_embedding_consent_changed.emit()

        facade.run(key=f"ai-library-vector-consent:{library_id}", request_id=uuid.uuid4().hex,
                   work=lambda: facade.set_ai_library_embedding_consent(library_id, None, folder=folder),
                   listener=deliver)

    def _finish_ai_library_index(self, outcome: FacadeOutcome) -> None:
        self._ai_library_index_cancel = None
        if self._closed:
            return
        self._ai_library_index_state = ({"error": str(outcome.error.message)}
                                        if outcome.error is not None else dict(outcome.result))
        self.ai_library_index_status.emit(dict(self._ai_library_index_state))

    def stop_ai_library_index(self) -> None:
        if self._ai_library_index_cancel is not None:
            self._ai_library_index_cancel.request()

    def ai_library_index_state(self) -> dict[str, Any]:
        return dict(self._ai_library_index_state)

    def load_ai_library_settings(self, library_id: str | None = None, *, folder: str | None = None) -> None:
        request_id = self._ai_library_settings_request_id = uuid.uuid4().hex
        facade = self._runtime()
        if facade is None:
            self.ai_library_settings_loaded.emit({"error": self._runtime_error_message(),
                                                  "library_id": library_id, "folder": folder})
            return

        def work() -> dict[str, Any]:
            from pathlib import Path

            from src.app.services.ai_library_analysis import active_media_routes, library_model_options
            from src.app.services.ai_library_embeddings import active_library_embedding_route
            from src.shared.ai_models.generation.media_inference import MediaInferenceError

            if folder is not None and library_id is None:
                raise ValueError("请先选择子目录所属的素材库。")
            libraries = facade.ai_library_analysis_policies()
            scope_library, scope_folder = library_id, folder
            if folder is not None:
                requested = next((row for row in libraries if row["library_id"] == library_id), None)
                target = Path(nfc(folder))
                if requested is not None and target.is_relative_to(Path(requested["path"])):
                    owners = (row for row in libraries if target.is_relative_to(Path(row["path"])))
                    owner = max(owners, key=lambda row: len(Path(row["path"]).parts))
                    scope_library = owner["library_id"]
                    scope_folder = None if target == Path(owner["path"]) else folder
            stamp = facade.ai_library_scope_stamp(scope_library, folder=scope_folder) if scope_library else None
            policy = facade.ai_library_analysis_policy(scope_library, folder=scope_folder) if scope_library else None
            route_error = ""
            try:
                route = active_library_embedding_route()
            except MediaInferenceError as exc:
                route, route_error = None, str(exc)
            consent = (facade.ai_library_embedding_consent(scope_library, folder=scope_folder)
                       if scope_library else None)
            return {"libraries": libraries,
                    "routes": [route.summary() for route in active_media_routes().values()],
                    "models": library_model_options(), "scope_policy": policy.to_dict() if policy else None,
                    "scope_stamp": stamp, "scope_library": scope_library, "scope_folder": scope_folder,
                    "embedding_route": route.summary() if route else None,
                    "embedding_error": route_error, "embedding_consent": consent}

        def deliver(outcome: FacadeOutcome) -> None:
            if not self._closed:
                self._ai_library_settings_done.emit(outcome, library_id, folder)

        facade.run(key="ai-library-settings", request_id=request_id, work=work, listener=deliver)

    def _finish_ai_library_settings(self, outcome: FacadeOutcome, library_id: str | None, folder: str | None) -> None:
        if self._closed or outcome.request_id != self._ai_library_settings_request_id:
            return
        snapshot = {"error": str(outcome.error.message)} if outcome.error else outcome.result
        self.ai_library_settings_loaded.emit({**snapshot, "library_id": library_id, "folder": folder})

    def save_ai_library_settings(
        self, library_id: str, mode: str, approved_routes: tuple[str, ...], *,
        folder: str | None = None, models: dict[str, str] | None = None,
        kinds: tuple[str, ...] = ("image", "video", "audio"), recursive: bool = True,
        expected_scope_stamp: tuple[str, str] | None = None,
    ) -> None:
        facade = self._runtime()
        if facade is None:
            self.ai_library_settings_saved.emit(self._runtime_error_message())
            return

        choices = tuple(models.items()) if models is not None else None

        def work() -> None:
            from src.features.files.services.ai_library import LibraryAnalysisPolicy

            policy = LibraryAnalysisPolicy(mode, approved_routes, choices, kinds, recursive)
            facade.set_ai_library_analysis_policy(
                library_id, policy, folder=folder, expected_scope_stamp=expected_scope_stamp,
            )

        def deliver(outcome: FacadeOutcome) -> None:
            if not self._closed:
                self.ai_library_settings_saved.emit(str(outcome.error.message) if outcome.error else "")

        facade.run(key=f"ai-library-policy:{library_id}:{folder or ''}", request_id=uuid.uuid4().hex,
                   work=work, listener=deliver)

    # ------------------------------------------------------------ 目录树（facade）
    def load_volumes(self) -> None:
        if self._closed or self._volumes_pending:
            return
        facade = self._runtime()
        if facade is None:
            return
        self._volumes_pending = True
        facade.run(key="mounted-volumes", request_id=uuid.uuid4().hex, work=mounted_data_volumes,
                   listener=lambda outcome: self._volumes_done.emit(outcome) if not self._closed else None)

    def _finish_volumes(self, outcome: FacadeOutcome) -> None:
        self._volumes_pending = False
        if not self._closed and outcome.error is None:
            self.volumes_loaded.emit(outcome.result)

    def invalidate_tree_root(self, root: str) -> None:
        prefix = root.rstrip("/") + "/"
        for path in tuple(self._tree_pending_paths):
            if path == root or path.startswith(prefix):
                del self._tree_pending_paths[path]

    def reload_tree(self, path: str) -> None:
        self.invalidate_tree_path(path)
        self.load_tree(path)

    def invalidate_tree_path(self, path: str) -> None:
        self._tree_pending_paths.pop(path, None)

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
        request_id = uuid.uuid4().hex
        self._tree_pending_paths[path] = request_id
        facade.tree(path, request_id=request_id,
                    listener=lambda outcome, p=path: self._tree_done.emit(outcome, p)
                    if not self._closed else None)

    def _finish_tree(self, outcome: FacadeOutcome, path: str) -> None:
        if self._closed or self._tree_pending_paths.get(path) != outcome.request_id:
            return
        self._tree_pending_paths.pop(path)
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

    # ------------------------------------------------------------ 本地缩略图
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

    def set_thumbnail_dispatch_paused(self, paused: bool) -> None:
        self._thumbnail_dispatch_paused = paused
        if not paused:
            self._pump_thumbnails()

    def _pump_thumbnails(self) -> None:
        if self._thumbnail_dispatch_paused:
            return
        while (not self._closed and self._thumbnail_queue
               and len(self._thumbnail_replies) < self.MAX_THUMBNAIL_CONCURRENCY):
            path, version = self._thumbnail_queue.popleft()
            if self._thumbnail_versions.get(path) != version:
                continue
            facade = self._runtime()
            if facade is None:
                self.thumbnail_failed.emit(path)
                continue
            request_id = uuid.uuid4().hex
            state = _ThumbnailRead(path, version, self._generation)
            self._thumbnail_replies[request_id] = state
            cancel = self._media_cancel

            def deliver(outcome: Any, key: str = request_id) -> None:
                if not self._closed:
                    self._thumbnail_done.emit(key, outcome)

            facade.run(key=f"thumbnail:{request_id}", request_id=request_id,
                       work=lambda p=path, v=version, token=cancel, runtime=facade:
                           runtime.thumbnail_bytes(p, v, cancel=token),
                       listener=deliver)

    def _finish_local_thumbnail(self, request_id: str, outcome: Any) -> None:
        state = self._thumbnail_replies.pop(request_id, None)
        if self._closed or state is None or state.generation != self._generation:
            return
        if self._thumbnail_versions.get(state.path) == state.version:
            raw = outcome.result
            if outcome.error is None and isinstance(raw, bytes) and 0 < len(raw) <= self.MAX_THUMBNAIL_BYTES:
                self.thumbnail_loaded.emit(state.path, raw)
            else:
                self.thumbnail_failed.emit(state.path)
        self._pump_thumbnails()

    # ------------------------------------------------------------ 目录内容（facade）
    PAGE_SIZE = 500  # 目录分页大小（D3）；测试可实例级收紧

    def load_directory(self, path: str, offset: int = 0) -> None:
        if self._closed:
            return
        if offset <= 0:
            self.cancel()  # 首页导航才作废旧上下文；续页共享当前代际
        generation = self._nav_generation
        if offset <= 0:
            self.loading.emit(path)
        facade = self._runtime()
        if facade is None:
            self.failed.emit(path, self._runtime_error_message())
            return
        request = QueryRequest(request_id=uuid.uuid4().hex, path=path)
        limit = int(self.PAGE_SIZE)

        def _work():
            record = facade.libraries.library_for_path(request.path)
            return facade.query.list_directory(
                request, library_id=record.library_id if record else "",
                limit=limit, offset=int(offset))

        def _deliver(outcome: FacadeOutcome) -> None:
            if not self._closed:
                self._directory_done.emit(generation, path, outcome)

        facade.run(key=f"list:{identity_key(path)}", request_id=request.request_id,
                   work=_work, listener=_deliver)

    def _finish_directory(self, generation: int, path: str, outcome: FacadeOutcome) -> None:
        # The request can become stale after the worker posts its Qt event.
        if self._closed or generation != self._nav_generation:
            return
        if outcome.error is not None:
            self.failed.emit(path, _dir_error_message(outcome.error))
            return
        result = outcome.result
        self.loaded.emit(path, list(result.entries))
        if not self._closed and generation == self._nav_generation:
            self.page_meta.emit(path, result.total, result.truncated)

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
                if result.accepted:
                    items.extend(result.items)
                else:
                    items.extend(ItemResult(path, ok=False, error_code=result.error_code,
                                            message=result.message) for path in group)
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
        if job.snapshots:
            self.metadata_snapshot.emit(dict(job.snapshots))
        if job.failures:
            self.metadata_failed.emit(list(job.paths), dict(job.patch),
                                      "；".join(sorted(set(job.failures))))
        else:
            self.metadata_applied.emit(list(job.paths), dict(job.patch), "已保存")

    # ------------------------------------------------------------ 文件任务（facade）
    def create_folder(self, request_id: str, parent: str, name: str, *, cancel: CancelToken | None = None) -> None:
        """Run a standard mkdir and send its result to the requesting picker."""
        name = name.strip()
        if not os.path.isabs(parent) or not is_valid_folder_name(name):
            message = I18N.tr("files.add_library.invalid_name", "请输入有效的单级文件夹名称。")
            self.folder_created.emit(request_id, "", message)
            return

        def complete(report: dict[str, Any], error: str) -> None:
            successes = report.get("successes", ())
            failures = report.get("failures", ())
            destination = successes[0]["to"] if successes else ""
            self.folder_created.emit(request_id, destination, error or (failures[0]["error"] if failures else ""))

        self.run_task("mkdir", [parent], options={"name": name}, cancel=cancel, on_complete=complete)

    def run_task(
        self, operation: str, targets: list[str], *,
        policy: str = "error",
        options: dict[str, Any] | None = None,
        cancel: CancelToken | None = None,
        on_complete: Callable[[dict[str, Any], str], None] | None = None,
    ) -> None:
        """提交文件任务：mkdir/rename/copy/move/trash/restore/purge。

        成功判断不在这里——逐项结果由服务 items 决定；进度经回调推进
        task_started（复用为进度快照，UI 只读 completed/total）。
        """
        if self._closed or not targets:
            if on_complete is not None:
                on_complete({}, self._runtime_error_message())
            return

        def fail(message: str) -> None:
            self.task_failed.emit(message)
            if on_complete is not None:
                on_complete({}, message)

        facade = self._runtime()
        if facade is None:
            fail(self._runtime_error_message())
            return
        options = dict(options or {})
        targets = list(targets)
        if operation == "mkdir" and options.get("name"):
            # UI 语义：目标目录 + 新名字 → 服务接收完整路径
            name = str(options.pop("name"))
            if name in {".", ".."} or any(char in name for char in "/\\:\0"):
                fail("新文件夹名称不能包含路径。")
                return
            targets = [os.path.join(targets[0], name)]
            options["parents"] = False
        job_id = f"task-{operation}-{uuid.uuid4().hex[:10]}"
        cancel = cancel if cancel is not None else CancelToken()
        self._running_jobs[job_id] = cancel
        # 首发（completed=0）附完整调用参数：任务列表据此提供"重试失败项"
        self.task_started.emit({
            "job_id": job_id, "operation": operation,
            "total": len(targets), "completed": 0,
            "targets": list(targets), "policy": str(policy),
            "options": dict(options or {})})

        def _progress(done: int) -> None:
            if not self._closed:
                self.task_started.emit({"job_id": job_id, "operation": operation,
                                        "total": len(targets), "completed": done})

        def _work() -> list[ItemResult]:
            groups: dict[str, list[str]] = {}
            trash_owners = {str(record.id): record.library_id for record in facade.tasks.list_trash()} if operation in {"restore", "purge"} else {}
            for raw in targets:
                record = facade.libraries.library_for_path(raw)
                library_id = trash_owners.get(str(raw), "") if operation in {"restore", "purge"} else record.library_id if record else ""
                groups.setdefault(library_id, []).append(raw if operation in {"mkdir", "copy", "move"} else nfc(raw))
            items: list[ItemResult] = []
            for library_id, group in groups.items():
                offset = len(items)
                result = facade.tasks.start_task(
                    library_id, operation, group, options=options, policy=policy,
                    job_id=job_id, cancel=cancel,
                    progress=lambda done, _total, base=offset: _progress(base + done))
                if result.accepted:
                    items.extend(result.items)
                else:
                    items.extend(ItemResult(path, ok=False, error_code=result.error_code,
                                            message=result.message) for path in group)
            return items

        def _deliver(outcome) -> None:
            self._running_jobs.pop(job_id, None)
            if self._closed:
                return
            if outcome.error is not None:
                fail(str(outcome.error.message or "文件操作未能提交"))
                return
            items = list(outcome.result)
            successes = [{"path": item.path, "to": item.result or ""}
                         for item in items if item.ok]
            failures = [{"path": item.path,
                         "error": str(item.message or item.error_code or "失败"),
                         "code": str(item.error_code or "")}
                        for item in items if not item.ok]
            cancelled = any(item.error_code == "CANCELLED" for item in items)
            report = {
                "job_id": job_id, "operation": operation,
                "total": len(items), "completed": len(items),
                "successes": successes, "failures": failures,
                "cancelled": cancelled}
            if operation == "move" and options.get("cut_token"):
                report["cut_token"] = options["cut_token"]
            self.task_finished.emit(report)
            if on_complete is not None:
                on_complete(report, "")

        facade.run(key=f"task:{job_id}", request_id=job_id,
                   work=_work, listener=_deliver)

    def cancel_task(self, job_id: str) -> None:
        """请求取消：协作式（条目/块边界生效）；终态仍由 task_finished 报告。"""
        if self._closed or not job_id:
            return
        token = self._running_jobs.get(job_id)
        if token is not None:
            token.request()

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
        self.register_library(path, "")

    def register_library(self, directory: str, name: str) -> None:
        self._library_change(
            lambda facade: facade.register_library(directory, name),
            I18N.tr("files.ai_library.library_added", "已添加素材库。"))

    def rename_library(self, library_id: str, name: str) -> None:
        self._library_change(
            lambda facade: "" if facade.libraries.rename(library_id, name) else
            I18N.tr("files.ai_library.library_missing", "该素材库已移除，请重新选择。"),
            I18N.tr("files.ai_library.library_renamed", "已更新素材库名称。"))

    def remove_library(self, library_id: str) -> None:
        self._library_change(
            lambda facade: "" if facade.libraries.unpin(library_id) else
            I18N.tr("files.ai_library.library_missing", "该素材库已移除，请重新选择。"),
            I18N.tr("files.ai_library.library_removed", "已移除素材库。"))

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
                self.library_registry_changed.emit()

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
        for token in tuple(self._running_jobs.values()):
            token.request()
        self._running_jobs.clear()

    def retry_thumbnail(self, path: str, version: int = 0) -> None:
        if self._closed:
            return
        self.release_thumbnail(path)
        self.thumbnail_retry_done.emit(path, True)
        self.request_thumbnail(path, version)

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

    # ------------------------------------------------------------ 本地悬停胶片条
    def request_strip(self, path: str, duration: float = 0.0) -> None:
        from src.features.files.services.media import thumbnail_bytes
        if self._closed or path in self._strip_replies.values():
            return
        if len(self._strip_replies) >= 2:
            self.strip_failed.emit(path)
            return
        facade = self._runtime()
        if facade is None:
            self.strip_failed.emit(path)
            return
        request_id = uuid.uuid4().hex
        self._strip_replies[request_id] = path
        cancel = self._media_cancel

        def deliver(outcome: Any) -> None:
            if not self._closed:
                self._strip_done.emit(request_id, outcome)

        facade.run(key=f"strip:{request_id}", request_id=request_id,
                   work=lambda: thumbnail_bytes(path, strip=True, cancel=cancel),
                   listener=deliver)

    def _finish_local_strip(self, request_id: str, outcome: Any) -> None:
        path = self._strip_replies.pop(request_id, "")
        if self._closed or not path:
            return
        if outcome.error is None and isinstance(outcome.result, bytes) and outcome.result:
            self.strip_loaded.emit(path, outcome.result)
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

    # ------------------------------------------------------------ 集合写入（D2）
    def create_collection(self, name: str, paths: list[str]) -> None:
        """从所选新建集合并加入成员；归属库按首个成员路径解析。"""
        facade = self._runtime()
        if facade is None or not str(name or "").strip():
            self.collection_changed.emit(False, self._runtime_error_message()
                                         if facade is None else "集合名不能为空")
            return
        clean = str(name).strip()
        members = [nfc(path) for path in paths]

        def _work(facade: FilesFacade):
            record = facade.libraries.library_for_path(members[0]) \
                if members else None
            library_id = record.library_id if record else ""
            created = facade.collections.create_collection(library_id, clean)
            if not created.ok:
                return created
            return facade.collections.add_members(
                int(created.message), members) if members else created

        self._collection_write(_work, f"已创建集合「{clean}」")

    def rename_collection(self, collection_id: int, name: str) -> None:
        self._collection_write(
            lambda facade: facade.collections.rename_collection(
                int(collection_id), str(name or "")),
            "集合已改名")

    def add_collection_members(self, collection_id: int,
                               paths: list[str]) -> None:
        members = [nfc(path) for path in paths]
        if not members:
            self.collection_changed.emit(False, "先选择要加入集合的素材")
            return
        self._collection_write(
            lambda facade: facade.collections.add_members(
                int(collection_id), members),
            f"已加入 {len(members)} 项")

    def remove_collection_members(self, collection_id: int,
                                  paths: list[str]) -> None:
        members = [nfc(path) for path in paths]
        if not members:
            self.collection_changed.emit(False, "先选择要移除的成员")
            return
        self._collection_write(
            lambda facade: facade.collections.remove_members(
                int(collection_id), members),
            f"已移除 {len(members)} 项")

    def delete_collection(self, collection_id: int) -> None:
        self._collection_write(
            lambda facade: facade.collections.delete_collection(
                int(collection_id)),
            "集合已删除")

    def _collection_write(self, work: Callable[[FilesFacade], Any],
                          ok_message: str) -> None:
        facade = self._runtime()
        if facade is None:
            self.collection_changed.emit(False, self._runtime_error_message())
            return
        request_id = uuid.uuid4().hex

        def _deliver(outcome) -> None:
            if self._closed:
                return
            if outcome.error is not None:
                self.collection_changed.emit(
                    False, str(outcome.error.message or "集合操作失败"))
                return
            result = outcome.result
            if not result.ok:
                self.collection_changed.emit(
                    False, str(result.message or result.error_code or "集合操作失败"))
                return
            self.collection_changed.emit(True, ok_message)
            self.load_collections()

        facade.run(key=f"collection-write:{request_id}", request_id=request_id,
                   work=lambda: work(facade), listener=_deliver)

    # ------------------------------------------------------------ 中断恢复（D5'/B3）
    def load_recoveries(self) -> None:
        """上个进程遗留的未终态任务清单（job_log ACTIVE 态）。"""
        if self._closed:
            return
        facade = self._runtime()
        if facade is None:
            return

        def _deliver(outcome) -> None:
            if self._closed or outcome.error is not None:
                return
            import json as _json
            records = []
            for row in outcome.result:
                try:
                    args = _json.loads(row.get("args") or "{}")
                except ValueError:
                    args = {}
                records.append({
                    "job_id": str(row.get("id") or ""),
                    "operation": str(row.get("op") or ""),
                    "state": str(row.get("state") or ""),
                    "source": str(args.get("source") or ""),
                })
            if records:
                self.recovery_loaded.emit(records)

        facade.run(key="recovery", request_id=uuid.uuid4().hex,
                   work=lambda: facade.tasks.pending_recoveries(),
                   listener=_deliver)

    def recover_job(self, job_id: str) -> None:
        """按磁盘证据分类恢复一个未终态任务（无法判定则保持待恢复）。"""
        if self._closed or not job_id:
            return
        facade = self._runtime()
        if facade is None:
            return

        def _deliver(outcome) -> None:
            if self._closed:
                return
            if outcome.error is not None:
                self.recovery_applied.emit({
                    "job_id": str(job_id), "state": "failed",
                    "classification": str(outcome.error.message or "恢复失败"),
                    "action": "none"})
                return
            verdict = dict(outcome.result.items[0].result or {})
            verdict.setdefault("job_id", str(job_id))
            self.recovery_applied.emit(verdict)

        facade.run(key=f"recover:{job_id}", request_id=uuid.uuid4().hex,
                   work=lambda: facade.tasks.recover(str(job_id)),
                   listener=_deliver)

    # ------------------------------------------------------------ 回收站（D2）
    def load_trash(self) -> None:
        """回收站清单（全库）；恢复/彻底删除走 run_task 的逐项结果。"""
        if self._closed:
            return
        facade = self._runtime()
        if facade is None:
            self.trash_failed.emit(self._runtime_error_message())
            return

        def _deliver(outcome) -> None:
            if self._closed or outcome.error is not None:
                if outcome.error is not None:
                    self.trash_failed.emit(
                        str(outcome.error.message or "回收站读取失败"))
                return
            self.trash_loaded.emit([
                {"id": record.id, "library_id": record.library_id,
                 "original_path": record.original_path,
                 "item_name": record.item_name, "is_dir": record.is_dir,
                 "deleted_at": record.deleted_at, "expire_at": record.expire_at}
                for record in outcome.result])

        facade.run(key="trash", request_id=uuid.uuid4().hex,
                   work=lambda: facade.tasks.list_trash(), listener=_deliver)

    # ------------------------------------------------------------ 全库搜索（facade）
    def search_library(self, query: str, limit: int = 200, offset: int = 0) -> None:
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

        request_id = uuid.uuid4().hex
        if path.lower().endswith(".toos"):
            from src.features.files.services.media import thumbnail_bytes
            facade.run(key="preview:" + identity_key(path), request_id=request_id,
                       work=lambda: thumbnail_bytes(path), listener=_deliver)
        else:
            facade.read_file(path, request_id=request_id, listener=_deliver,
                             max_bytes=self.MAX_PREVIEW_BYTES)

    def cancel_preview(self) -> None:
        self._preview_seq += 1

    # ------------------------------------------------------------ 取消/关闭
    def cancel(self) -> None:
        self._generation += 1
        self._nav_generation += 1
        if self._ai_analysis_marks_cancel is not None:
            self._ai_analysis_marks_cancel.request()
        self._media_cancel.set()
        self._media_cancel = threading.Event()
        self._thumbnail_replies.clear()
        self._strip_replies.clear()
        self._thumbnail_queue.clear()
        self._thumbnail_attempted.clear()
        self._thumbnail_versions.clear()

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop_ai_library_index()
        if self._ai_library_search_cancel is not None:
            self._ai_library_search_cancel.request()
        for session_id in list(self._compare_sessions):
            self.cancel_compare(session_id)
        self._compare_sessions.clear()
        self._compare_busy.clear()
        if self._scan_cancel is not None:
            self._scan_cancel.request()
        self.cancel()
        self._media_cancel.set()
        self.cancel_preview()
        self._tree_pending_paths.clear()
        facade, self._facade = self._facade, None
        if facade is not None:
            facade.shutdown()
            catalog, self._owned_catalog = self._owned_catalog, None
            drained = self._drained

            def close_catalog() -> None:
                try:
                    if catalog is not None:
                        catalog.close()
                finally:
                    drained.set()

            facade.when_idle(close_catalog)
        else:
            self._drained.set()

    def wait_for_shutdown(self, timeout: float) -> bool:
        """Allow process exit to wait after the GUI event loop has stopped."""
        return self._drained.wait(timeout)
