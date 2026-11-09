"""FileOpsMixin -- file open/save/video/events/backup handlers extracted from MainWindow."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict

from typing import Any, Optional

import cv2
import numpy as np
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QMainWindow

from PyQt6.QtWidgets import (QFileDialog, QMessageBox, QInputDialog,
                             QDialog)
from PyQt6.QtCore import QByteArray, QTimer, QObject, QEvent, QRunnable, QThreadPool, pyqtSignal

from src.features.color.core.layer import Layer
from src.features.edit.core.playback_debug import playback_trace
from src.features.edit.core.source_present_gate import SourcePresentGate
from src.shared.diagnostics import flash_diag as _flash_diag
from src.shared.io.file_io import FileIO
from src.shared.i18n.i18n import I18N
from src.shared.ui.shortcut_scope import active_motion_graphic_editor

# 视频文件扩展名判断统一走 format_registry（v2 GStreamer VideoDecoder 已移除）
from src.shared.media.format_registry import is_video as _is_video_file

from src.shared.video.video_playback import EnhancedVideoPlaybackEngine



def format_capability_report(report) -> tuple[str, str]:
    """DESK-07A：把 04B 能力分级报告格式化为 (状态栏文本, 详情文本)。"""
    grade_text = {
        "P": "部分内容受限（未识别对象已锁定，原样保留）",
        "B": "包由更高版本写入（不熟悉结构只读保留，保存不降级）",
        "X": "导出被阻断（存在缺失效果/必需素材/必需特性问题）",
    }
    status = f"工程兼容性：{grade_text.get(report.grade, report.grade)}"
    lines = []
    for finding in report.findings:
        lines.append(
            f"[{finding.severity}] {finding.object_kind} "
            f"{finding.object_ref}: {finding.detail}")
    return status, "\n".join(lines) or status


class _SourceViewerDecodeWorker:
    """
    Background decode worker for the Source Viewer.

    Source preview playback previously called ``EditGpuDecodeManager.get_frame``
    synchronously on the UI thread for every frame, which blocks the event
    loop (demux + VT decode + task submission) and makes playback stutter.
    This worker decodes on a dedicated thread with latest-wins coalescing:
    superseded frame requests are dropped instead of queued, so playback can
    never fall further behind than one decode.

    When the decoded frame cannot go through the GPU renderer, the worker
    emits ``cpu_fallback_requested(frame)`` so the widget runs its CPU path.
    """

    def __init__(self, gpu_decode_manager: object, gpu_renderer: object) -> None:
        from PyQt6.QtCore import pyqtSignal

        self._manager = gpu_decode_manager
        self._renderer = gpu_renderer
        # Signal emitter must be a QObject for queued cross-thread delivery.
        from PyQt6.QtCore import QObject

        class _Signals(QObject):
            cpu_fallback_requested = pyqtSignal(int)

        self.signals = _Signals()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        # (asset_path, frame, interactive, generation)
        self._request: Optional[tuple] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="SourceViewerDecodeWorker",
        )
        self._thread.start()

    def request(
        self,
        asset_path: str,
        frame: int,
        *,
        interactive: bool = False,
        generation: int = -1,
    ) -> None:
        """Queue a decode request; only the latest request is kept."""
        with self._cond:
            self._request = (asset_path, frame, interactive, generation)
            self._cond.notify()

    def stop(self) -> None:
        with self._cond:
            if not self._running:
                return
            self._running = False
            self._request = None
            self._cond.notify()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        from src.shared.media.gpu_frame import safe_release_gpu_frame
        from src.features.edit import facade as edit_facade

        while True:
            with self._cond:
                while self._running and self._request is None:
                    self._cond.wait(timeout=1.0)
                if not self._running:
                    return
                request = self._request
                self._request = None
            frame = -1
            # AUD-2803: 显式跟踪已解码帧的所有权。此前 create_edit_render_task
            # 构造/submit 自身抛异常时，已解码的 GPU/VT 帧既不释放也不提交
            # （持有 CVPixelBuffer/IOSurface 引用），仅降为 debug 日志——长
            # 时间源监视播放中反复触发会累积泄漏 GPU/IOSurface 资源。
            decoded_frame = None
            try:
                asset_path, frame, interactive, generation = request
                # Latest-wins: if a newer request arrived while we were
                # picking this one up, skip straight to it.
                with self._lock:
                    superseded = self._request is not None
                if superseded:
                    continue
                if interactive:
                    decoded_frame, frame_type = self._manager.get_frame_interactive(
                        asset_path,
                        frame,
                        is_stale=lambda p=asset_path, f=frame: self._is_superseded(p, f),
                    )
                else:
                    decoded_frame, frame_type = self._manager.get_frame(asset_path, frame)
                if decoded_frame is None:
                    # Interactive abort or decode failure: let the widget show
                    # its last good frame; do not force a CPU seek decode.
                    if not interactive:
                        self.signals.cpu_fallback_requested.emit(int(frame))
                    continue
                if frame_type == 'cpu':
                    safe_release_gpu_frame(decoded_frame)
                    decoded_frame = None
                    self.signals.cpu_fallback_requested.emit(int(frame))
                    continue

                frame_w = getattr(decoded_frame, 'width', 1920)
                frame_h = getattr(decoded_frame, 'height', 1080)
                layer_data = {
                    'frame_type': frame_type,
                    'track_info': {'opacity': 1.0, 'track_index': 0},
                }
                # Route the decoded frame to the key EditGpuPreviewRenderer's
                # _process_task dispatch expects for each frame_type.  The
                # mapping MUST mirror TimelinePreviewRenderer's layer builder
                # (timeline_preview_renderer.py ~L1505): 'vt_pixel_buffer'
                # frames zero-copy through the IOSurface GL bridge and are read
                # from the 'vt_pixel_buffer' key, NOT 'decoded_frame' — putting
                # them in the wrong key made the renderer skip the VT import
                # branch, fall through to "unsupported_type", and emit only
                # render_error (so frame_ready never fired and playback froze).
                if frame_type == 'gpu':
                    layer_data['gpu_frame'] = decoded_frame
                elif frame_type == 'vt_pixel_buffer':
                    layer_data['vt_pixel_buffer'] = decoded_frame
                else:  # 'planar' / 'vt_planar' → renderer's planar upload path
                    layer_data['decoded_frame'] = decoded_frame
                task = edit_facade.create_edit_render_task(
                    frame=frame,
                    # Widget generation at request time: lets the widget
                    # reject results superseded by a seek/asset switch even
                    # though frame numbers alone can be ambiguous.
                    generation_id=generation,
                    layers=[layer_data],
                    viewport_size=(frame_w, frame_h),
                    output_fbo_size=(frame_w, frame_h),
                )
                if not self._renderer.submit(task):
                    safe_release_gpu_frame(decoded_frame)
                    decoded_frame = None
                    self.signals.cpu_fallback_requested.emit(int(frame))
                else:
                    # 任务已被渲染队列接收，帧的所有权随之移交。
                    decoded_frame = None
            except Exception as e:
                # AUD-2803: 异常路径中若本 worker 仍持有已解码帧（构造/
                # 提交中途抛错），先释放再上报，避免泄漏。
                if decoded_frame is not None:
                    safe_release_gpu_frame(decoded_frame)
                    decoded_frame = None
                logger.debug(f"SourceViewerDecodeWorker error: {e}")
                self.signals.cpu_fallback_requested.emit(int(frame))

    def _is_superseded(self, asset_path: str, frame: int) -> bool:
        with self._lock:
            request = self._request
        if request is None:
            return False
        return request[:2] != (asset_path, frame)

# Conditional imports via edit facade -- avoid deep feature internals at module level
try:
    from src.features.edit.commands_facade import (
        get_project_io_classes,
        get_project_classes,
        get_timeline_classes,
        get_timeline_preview_renderer_factory,
    )
    _io = get_project_io_classes()
    _proj = get_project_classes()
    _tl = get_timeline_classes()
    _renderer_factory = get_timeline_preview_renderer_factory()

    if _io and _proj and _tl and _renderer_factory:
        EditProjectIO = _io["EditProjectIO"]
        EditProjectIOConfig = _io["EditProjectIOConfig"]
        UIState = _io["UIState"]
        EDIT_PROJECT_EXTENSION = _io["EDIT_PROJECT_EXTENSION"]
        EditProject = _proj["EditProject"]
        TimelineSequenceRef = _proj["TimelineSequenceRef"]
        EditTrack = _tl["EditTrack"]
        TimelineSequence = _tl["TimelineSequence"]
        create_timeline_preview_renderer = _renderer_factory
        from src.shared.media.metadata import Rational
        EDIT_MODULE_AVAILABLE = True
    else:
        raise ImportError("Edit facade returned None for required classes")
except ImportError:
    EditProjectIO = None
    EditProjectIOConfig = None
    UIState = None
    EDIT_PROJECT_EXTENSION = None
    EditProject = None
    TimelineSequenceRef = None
    EditTrack = None
    TimelineSequence = None
    create_timeline_preview_renderer = None
    Rational = None
    EDIT_MODULE_AVAILABLE = False

logger = logging.getLogger(__name__)

# P1-THREAD-01：stop() 超时后仍存活的 GPU/QThread 对象引用，推迟到进程
# 退出回收，防止 GC 在线程存活期间析构 QThread 触发进程 Abort。
_LEAKED_GPU_OBJECTS: list = []


def _stop_gpu_renderer_deferred(renderer, timeout_ms: int = 10000) -> None:
    """停止 GPU 渲染线程；超时则推迟销毁并显式上报（P1-THREAD-01）。

    ``stop()`` 返回 False 表示线程可能仍持有 GL 资源在运行，此时让对象
    被 GC 析构会直接 Abort 进程；把引用挂到进程级列表，销毁责任转移到
    进程退出，状态保持在日志中可检索。

    U06（第六轮审计）：``EditGpuPreviewRenderer.stop()`` 是旧契约——只置
    停止标志并返回 None。此前 None 被当作失败，每次关窗都误报"GPU 渲染
    线程未在 Nms 内退出"并把正常退出的 renderer 挂进泄漏表（审计实测
    进程退出码 0，线程实际已退出）。这里以线程实际存活状态为准。
    """
    try:
        try:
            stopped = renderer.stop(timeout_ms)
        except TypeError:
            # 不接受超时参数的简化 stop()
            stopped = renderer.stop()
    except Exception:
        logger.exception("renderer.stop() 抛异常，跳过推迟销毁")
        return
    if stopped is None:
        # 旧契约 stop()：无法从返回值判定，等待线程收尾并以真实状态为准。
        wait = getattr(renderer, "wait", None)
        if callable(wait):
            try:
                stopped = bool(wait(timeout_ms))
            except Exception:
                stopped = True  # 无法判定：不谎报泄漏，交由进程退出回收
        else:
            inner = getattr(renderer, "_thread", None)
            inner_wait = getattr(inner, "wait", None) if inner is not None else None
            if callable(inner_wait):
                try:
                    stopped = bool(inner_wait(timeout_ms))
                except Exception:
                    stopped = True
            else:
                stopped = True
    if not stopped:
        _LEAKED_GPU_OBJECTS.append(renderer)
        logger.error(
            "GPU 渲染线程未在 %dms 内退出，已推迟销毁（进程退出时回收）；"
            "请排查长命令或 GL 清理挂起", timeout_ms,
        )


class _MetadataLoader(QObject):
    """[R0-7] 文件元数据的线程池加载器 + LRU 缓存。

    FileIO.get_metadata 会同步调用 exiftool 子进程（timeout=2s），此前在
    文件加载路径于 UI 线程执行，exiftool 迟缓时每次打开文件冻结界面。
    结果经队列信号投递回 UI 线程刷新 info_panel；键含 mtime/size，
    文件变化自动失效。"""

    _CACHE_LIMIT = 128

    ready = pyqtSignal(str, dict)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._cache: "OrderedDict[tuple, dict]" = OrderedDict()
        self._inflight: set[str] = set()

    def request(self, path: str) -> None:
        try:
            key = (path, os.path.getmtime(path), os.path.getsize(path))
        except OSError:
            key = (path, None, None)

        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            self.ready.emit(path, cached)
            return
        if path in self._inflight:
            return

        self._inflight.add(path)
        worker = _MetadataWorker(path, key, self)
        worker.signals.ready.connect(self._on_ready)
        QThreadPool.globalInstance().start(worker)

    def _on_ready(self, path: str, key: tuple, metadata: dict) -> None:
        self._inflight.discard(path)
        self._cache[key] = metadata
        while len(self._cache) > self._CACHE_LIMIT:
            self._cache.popitem(last=False)
        self.ready.emit(path, metadata)


class _MetadataWorker(QRunnable):
    """在工作线程执行 FileIO.get_metadata（exiftool 子进程）。"""

    class _Signals(QObject):
        ready = pyqtSignal(str, tuple, dict)

    def __init__(self, path: str, key: tuple, parent_loader: _MetadataLoader) -> None:
        super().__init__()
        self._path = path
        self._key = key
        self.signals = self._Signals(parent_loader)

    def run(self) -> None:
        try:
            metadata = FileIO.get_metadata(self._path)
        except Exception:
            logger.debug("metadata load failed: %s", self._path, exc_info=True)
            metadata = {}
        self.signals.ready.emit(self._path, self._key, metadata)


class _InterchangeRowsWorker(QRunnable):
    """AUD-12613：工作线程执行 collect_asset_rows（逐素材磁盘探测）。"""

    class _Signals(QObject):
        ready = pyqtSignal(object, list)   # ImportResult, rows
        failed = pyqtSignal(object, str)   # ImportResult, error

    def __init__(self, result: Any, parent: QObject) -> None:
        super().__init__()
        self._result = result
        self.signals = self._Signals(parent)

    def run(self) -> None:
        from src.features.edit.adapters.import_builder import collect_asset_rows
        try:
            rows = collect_asset_rows(self._result)
        except Exception as exc:
            logger.exception("interchange asset-row probing failed (AUD-12613)")
            self.signals.failed.emit(self._result, str(exc))
            return
        self.signals.ready.emit(self._result, rows)


class _InterchangeBuildWorker(QRunnable):
    """AUD-12613：工作线程执行 build_project_from_import_result。

    逐素材 ``os.path.exists`` + ``probe_metadata``（ffprobe 子进程，百毫秒
    级/素材）——绝不留在 UI 槽内同步执行。构建仅读取 ``base`` 工程与
    ``result``（只读共享）；完成/失败经排队信号回 UI 线程应用。
    """

    class _Signals(QObject):
        ready = pyqtSignal(object, object, object, object)
        failed = pyqtSignal(object, str)

    def __init__(self, base: Any, result: Any, parent: QObject) -> None:
        super().__init__()
        self._base = base
        self._result = result
        self.signals = self._Signals(parent)

    def run(self) -> None:
        from src.features.edit.adapters.import_builder import (
            build_project_from_import_result,
        )
        try:
            new_project, sequences, summary = build_project_from_import_result(
                self._base, self._result)
        except Exception as exc:
            logger.exception("interchange import build failed (AUD-12613)")
            self.signals.failed.emit(self._result, str(exc))
            return
        self.signals.ready.emit(self._result, new_project, sequences, summary)


class _PanelResizeRelay(QObject):
    """[ARCH-3] 转发 FilesPanel 的 Resize 事件给回调，浮层播放器跟随面板尺寸。

    FileOpsMixin 混入 MainWindow，其 eventFilter 已被主窗口占用，因此用
    独立的 QObject 安装在 files_panel 上，避免两处 eventFilter 互相覆盖。"""

    def __init__(self, callback) -> None:
        super().__init__()
        self._callback = callback

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Resize:
            self._callback(event.size())
        return False


class FileOpsMixin:
    """Mixin providing file open/save, video, backup, event, and project management methods.

    Intended to be mixed into MainWindow so that all ``self.*`` attribute
    references resolve against the MainWindow instance.
    """

    # ------------------------------------------------------------------
    # File Open / Load
    # ------------------------------------------------------------------

    _metadata_loader: Optional[_MetadataLoader] = None

    def _request_metadata_async(self, path: str) -> None:
        """[R0-7] 异步获取文件元数据并回填 info_panel（见 _MetadataLoader）。"""
        if self._metadata_loader is None or self._metadata_loader.parent() is not self:
            self._metadata_loader = _MetadataLoader(self)
            self._metadata_loader.ready.connect(self._on_metadata_ready)
        self._metadata_loader.request(path)

    def _on_metadata_ready(self, path: str, metadata: dict) -> None:
        # 快速切换文件时只刷新当前文件的结果
        if getattr(self, "current_file_path", None) == path:
            self.info_panel.update_metadata(metadata)

    def _sync_mg_preset_project_root(self, project_filepath: str | None) -> None:
        """Point the MG preset library's project-local root at the open project.

        While a ``.topos`` file is open, ``<its directory>/mg_presets/`` joins
        the library scan roots (highest priority, receives new saves), so a
        preset collection travels with its project.  ``None`` detaches it.
        """
        try:
            from src.features.teaching_animation.presets.paths import set_active_project_file
            set_active_project_file(project_filepath)
        except ImportError:
            pass  # edit-only products ship without the teaching_animation package

    def new_project(self) -> None:
        """Create a new project through the single public lifecycle entry.

        Both image and video work use EditProject as the runtime document;
        the presence of timelines determines whether the project is an image
        project or a video editing project.
        """
        if getattr(self, "_current_edit_project", None) is not None:
            if not self.close_project():
                return
        elif getattr(self, "current_file_path", None):
            self.close_project()

        # Video work mode starts an empty timeline-capable document.  Image
        # work mode starts clean and receives its image asset through Open
        # Media, so each opened image can own its own project file.
        if getattr(self, "_edit_available", False) and self._get_work_mode() == "video":
            self._create_video_project()
            return

        # In Image mode a new project starts from a clean grading state.  A
        # source is still opened separately via Open Media, so this does not
        # invent a fake image asset on disk.
        self.on_adjustments_reset()
        self.status_bar.showMessage(
            I18N.tr("status.new_project", "New project ready. Open media to begin."),
            3000,
        )

    def open_project(self) -> None:
        """Open any .topos project and route it by its stored project mode."""
        last_path = (
            self.config_manager.get("last_project_path")
            or ""
        )
        filepath, _ = QFileDialog.getOpenFileName(
            self,
            I18N.tr("action.open_project", "Open Project..."),
            last_path,
            I18N.tr("project.file_filter", "Topos Projects (*.topos);;All Files (*)"),
        )
        if filepath:
            self._open_project_path(filepath)

    def _open_project_path(self, filepath: str) -> bool:
        """Open a project path, choosing image or edit hydration automatically."""
        if not filepath or not os.path.exists(filepath):
            return False
        if not self._check_project_matches_work_mode(filepath):
            return False
        if getattr(self, "_current_edit_project", None) is not None:
            if not self.close_project():
                return False

        previous_project = getattr(self, "_current_edit_project", None)

        mode = self._read_project_mode_from_manifest(filepath)
        if mode == "standalone_image":
            self._load_standalone_project(filepath)
        else:
            self._load_edit_project(filepath)

        loaded = getattr(self, "_current_edit_project", None) is not previous_project
        if loaded and mode != "standalone_image":
            # An edit project owns the timeline and must use the Edit save
            # route.  Project Manager can be opened while the last page is
            # Color/Library; leaving that page active makes Ctrl+S attempt a
            # color save, which can fail with the misleading "check project
            # path" message and leaves timeline edits out of the saved file.
            if getattr(self, "current_page", "") != "Edit":
                self.switch_to_page("Edit")
        if loaded:
            self._sync_mg_preset_project_root(filepath)

        return loaded

    def save_project_as(self) -> None:
        """Save the active image or edit project to a user-selected .topos path."""
        if getattr(self, "_current_edit_project", None) is not None:
            self._save_current_project_as()
            return

        if not getattr(self, "current_file_path", None):
            QMessageBox.warning(
                self,
                I18N.tr("warning.title", "Warning"),
                I18N.tr("project.no_project_message", "No project is open."),
            )
            return

        current_path = self.save_router.get_current_project_path()
        default_path = current_path or self.save_router._get_project_path_for_file(
            self.current_file_path
        )
        filepath, _ = QFileDialog.getSaveFileName(
            self,
            I18N.tr("action.save_project_as", "Save Project As..."),
            default_path or "",
            I18N.tr("project.file_filter", "Topos Projects (*.topos);;All Files (*)"),
        )
        if not filepath:
            return
        from src.shared.project.library import ensure_project_path
        filepath = ensure_project_path(filepath)

        self.save_router.set_project_path(filepath)
        if self.save_router.save():
            self._add_to_recent_projects(filepath)
            self._sync_mg_preset_project_root(filepath)
            self.config_manager.set("last_project_path", os.path.dirname(filepath))
            self.config_manager.save_config()
            self._clear_all_dirty_states()
            self.setWindowTitle(f"Topos - {os.path.basename(filepath)}")
            self.status_bar.showMessage(
                I18N.tr("status.project_saved_as", "Project saved as: {path}").format(
                    path=os.path.basename(filepath)
                ),
                3000,
            )
        else:
            self.save_router.clear_cache()
            QMessageBox.critical(
                self,
                I18N.tr("error.title", "Error"),
                I18N.tr("error.save_project", "Failed to save project."),
            )

    def export_handoff_to_mobile(self) -> None:
        """DESK-07B：把当前工程导出为自包含交接包（手机可独立打开）。"""
        source = (
            getattr(self, "_current_edit_project_filepath", None)
            or self.save_router.get_current_project_path()
        )
        if not source or not os.path.exists(source):
            QMessageBox.warning(
                self,
                I18N.tr("warning.title", "Warning"),
                I18N.tr(
                    "handoff.no_saved_project",
                    "请先保存工程——交接包从当前工程文件生成。"),
            )
            return
        default_name = (
            os.path.splitext(os.path.basename(source))[0] + "_handoff.topos")
        dest, _ = QFileDialog.getSaveFileName(
            self,
            I18N.tr("action.export_handoff", "导出交接包（移动端...）"),
            os.path.join(os.path.dirname(source), default_name),
            I18N.tr("project.file_filter", "Topos Projects (*.topos);;All Files (*)"),
        )
        if not dest:
            return
        from src.shared.project.library import ensure_project_path
        dest = ensure_project_path(dest)
        from src.shared.project.handoff import export_handoff_package
        try:
            self.status_bar.showMessage(
                I18N.tr("status.handoff_exporting", "正在生成交接包…"), 0)
            result = export_handoff_package(source, dest)
        except Exception as exc:  # noqa: BLE001 — UI 边界不外抛
            logger.exception("handoff export failed")
            QMessageBox.critical(
                self, I18N.tr("error.title", "Error"),
                I18N.tr("handoff.failed", "交接包导出失败：{err}").format(
                    err=exc))
            return
        finally:
            self.status_bar.clearMessage()
        if result.success:
            self.status_bar.showMessage(
                I18N.tr("status.handoff_done",
                        "交接包已生成：{path}").format(
                    path=os.path.basename(dest)) + f"（{result.summary}）",
                10000)
            QMessageBox.information(
                self,
                I18N.tr("status.handoff_done", "交接包已生成"),
                f"{dest}\n\n{result.summary}\n内嵌素材："
                f"{len(result.embedded_asset_ids)} 个",
            )
        else:
            problems = "\n".join(
                f"· {p.get('object_kind', '')} {p.get('object_id', '')}: "
                f"{p.get('detail', '')}"
                for p in result.problems)
            QMessageBox.warning(
                self,
                I18N.tr("handoff.failed_title", "交接包未生成"),
                (result.detail or result.summary)
                + (f"\n\n{problems}" if problems else ""),
            )

    def close_project(self) -> bool:
        """Close the active project and clear its media/session state."""
        # Cmd+W 是 ApplicationShortcut，Motion Graphic 编辑器激活时仍会触发。
        # 此时按 macOS 惯例应关闭最前面的窗口（编辑器），而不是把整个工程
        # 关掉。编辑器的 closeEvent 会自行处理未保存场景的询问。
        editor = active_motion_graphic_editor()
        if editor is not None and editor is not self:
            editor.close()
            return True

        if getattr(self, "_current_edit_project", None) is not None:
            return bool(self._close_current_project())

        # Image projects use the same EditProject state as video projects;
        # _close_current_project handles the document and this branch only
        # covers a media session created before a project was hydrated.
        if getattr(self, "current_file_path", None):
            self.unload_video(save_before_unload=False)
            # I05：无工程媒体会话关闭同样释放 Source（可复用边界）
            release = getattr(self, "_release_edit_source_viewer", None)
            if callable(release):
                release()
            self.current_file_path = None
            self.current_file_is_video = False
            self.save_router.clear_cache()
            # M19.2b：无已装配工程的媒体会话关闭同样清空进程级色彩
            # 覆盖（防御位——正常路径工程关闭时已清）。
            try:
                from src.shared.color.color_context_lifecycle import (
                    on_project_closed as _clear_color_context,
                )
                _clear_color_context()
            except ImportError:
                logger.debug(
                    "color_context_lifecycle unavailable on session close")
            self._sync_mg_preset_project_root(None)
            self.setWindowTitle(I18N.tr("app.brand", "Topos"))
            self.status_bar.showMessage(
                I18N.tr("status.project_closed", "Project closed."), 3000
            )
        return True

    def _confirm_project_replacement_for_image(self, path: str) -> bool:
        """AUD-2805: 图像加载将替换"异类"工程前先走关闭确认流程。

        替换条件（与 ``_ensure_image_project`` 一致）：活动工程含时间线，
        或不引用该图像素材。此前的路径下（Color 页从未加载素材 →
        ``current_file_path`` 为 None，load_image 的旧工程保存被跳过），
        未保存的时间线剪辑/工程修改既不触发保存也不弹关闭确认，直接被
        新图像工程覆盖，``_current_timeline_sequences`` 被清空。

        Returns:
            True 表示可以继续图像加载（无需替换，或替换已经过保存确认）；
            False 表示用户取消（或保存失败），调用方必须中止加载。
        """
        project = getattr(self, "_current_edit_project", None)
        if project is None:
            return True
        asset = project.get_asset_by_path(path) if hasattr(project, "get_asset_by_path") else None
        if not project.timelines and asset is not None:
            return True  # 同一图像自己的工程——不发生替换
        # 替换异类工程：经 _close_current_project() 走保存确认（Save/
        # Discard/Cancel），未保存修改不再静默丢失。
        return bool(self._close_current_project())

    def _ensure_image_project(self, path: str, project_path: str | None = None) -> bool:
        """Make the active runtime document the canonical image project.

        Returns False when replacing the active project was aborted (the
        unsaved-changes confirmation was cancelled), in which case the
        caller must not continue the image load (AUD-2805).
        """
        project = getattr(self, "_current_edit_project", None)
        asset = project.get_asset_by_path(path) if project is not None else None
        if project is None or project.timelines or asset is None:
            # AUD-2805: 替换一个已存在的异类工程（时间线工程，或不含该
            # 素材的其他图像工程）前必须先过保存确认。load_image 已在
            # 更早的位置调用过 _confirm_project_replacement_for_image，
            # 正常流程走到这里时 project 已为 None 或同素材工程，不会
            # 再弹一次窗；此处保留兜底以保护其它调用路径。
            if project is not None and not self._confirm_project_replacement_for_image(path):
                return False
            from src.features.edit.core.project import EditProject

            project = EditProject.create_image_project(
                name=os.path.splitext(os.path.basename(path))[0] or "Untitled Project",
                image_path=path,
            )
            self._current_edit_project = project
            self._current_timeline_sequences = {}
            if hasattr(self, "media_pool_panel"):
                self.media_pool_panel.set_project(project)
            if hasattr(self, "timeline_panel"):
                self.timeline_panel.set_project(project)
            self._setup_ai_editing()

        self._current_edit_project_filepath = project_path
        if project_path:
            self.save_router.set_project_path(project_path)
        self._sync_mg_preset_project_root(project_path)
        self.edit_project_dirty = project_path is None
        self.setWindowTitle(f"Topos - {project.name}" + (" *" if project_path is None else ""))
        return True

    def open_file(self) -> None:
        last_path = (self.config_manager.get("default_open_directory")
                     or self.config_manager.get("last_path") or "")
        # 添加视频格式支持
        video_formats = " *.mp4 *.mov *.avi *.mkv *.flv *.wmv *.webm *.m4v"
        filters = f"All Supported (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp *.arw *.cr2 *.nef *.dng *.orf *.raf *.psd *.exr *.hdr{video_formats});;Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp *.arw *.cr2 *.nef *.dng *.orf *.raf *.psd *.exr *.hdr);;Videos ({video_formats});;All Files (*)"
        path, _ = QFileDialog.getOpenFileName(self, "Open Media", last_path, filters)
        if path:
            self._open_media_path(path)

    def _open_media_path(self, path: str) -> None:
        """Open media, reusing its own project when one already exists."""
        if _is_video_file(path):
            if self.current_page in ("Color", "Library"):
                self._reject_standalone_video_for_color(path)
                return
            self.load_video(path)
            return

        # Directly opening an image must not create a fresh in-memory project
        # over an existing image's saved grade. Each image keeps its own
        # adjacent .topos project; a missing package starts a new one.
        project_path = self.save_router._get_project_path_for_file(path)
        if (
            os.path.exists(project_path)
            and self._read_project_mode_from_manifest(project_path) == "standalone_image"
        ):
            self._open_project_path(project_path)
        else:
            self.load_image(path)

    def _update_composite_for_standalone_image(self, image_path: str, restored_composite: bool = False) -> None:
        """Update Composite page graph for standalone image loading.

        When a standalone image is loaded (not a timeline clip), the
        Composite page should use an ImageInput-based graph instead of
        the default TimelineClipInput graph. This handles the case where
        the Composite page is already visible at app startup.

        Args:
            image_path: Path to the loaded image file.
        """
        if not getattr(self, '_composite_available', False) or getattr(self, 'composite_page', None) is None:
            return

        if hasattr(self.composite_page, "set_edit_project"):
            self.composite_page.set_edit_project(getattr(self, "_current_edit_project", None))

        # If a saved composite graph was just restored for this image, keep it —
        # don't overwrite the user's persisted work with a fresh default graph.
        if restored_composite:
            self._composite_graph_image_path = image_path
            return

        # Preserve the in-memory graph only if it already belongs to this same
        # image (e.g. re-loading it). Otherwise the graph is a previous image's
        # leftover and must be replaced with a fresh default — each image owns
        # its own composite, so never carry one image's nodes into another.
        # NOTE: do NOT guard on requires_color_output_input() — that returns True
        # for ANY ColorOutput graph (the COLORNode's node_type is "color_input"),
        # so once the first image is loaded it would short-circuit here forever
        # and image B would silently keep image A's nodes.
        if getattr(self, "_composite_graph_image_path", None) == image_path:
            return

        self.composite_page.switch_to_color_output_graph()
        self._composite_graph_image_path = image_path

    @staticmethod
    def _absolutize_composite_paths(graph_data: dict, project_dir: str) -> dict:
        """Convert relative image paths in ImageInputNodes back to absolute paths."""
        if not graph_data or not project_dir:
            return graph_data
        for node_data in graph_data.get("nodes", []):
            if node_data.get("type") == "image_input":
                params = node_data.get("parameters", {})
                fp = params.get("file_path", "")
                if fp and not os.path.isabs(fp):
                    abs_path = os.path.normpath(os.path.join(project_dir, fp))
                    if os.path.exists(abs_path):
                        params["file_path"] = abs_path
        return graph_data

    def load_image(
        self,
        path,
        *,
        create_standalone_session: bool = True,
        project_path: str | None = None,
    ):
        # 加载图像时，卸载当前视频（如果有）
        self.unload_video()
        if create_standalone_session and hasattr(self.canvas, "set_display_scene_size_override"):
            self.canvas.set_display_scene_size_override(None)

        # Clear SAM points and disable SAM mode when loading a new image
        if self.mask_panel.btn_sam.isChecked():
            self.mask_panel.btn_sam.setChecked(False)
        self.canvas.clear_sam_points()
        if hasattr(self.engine, 'reset_sam_points'):
            self.engine.reset_sam_points()

        if self.current_file_path:
            self.save_router.save()

        # AUD-2805: 打开一张"独立会话"图像可能替换当前打开的异类工程
        # （时间线工程，或不含该素材的其他图像工程）。此时 current_file_path
        # 为 None 的路径下（时间线素材从未加载进 Color），上面这行保存
        # 不会触发——未保存的工程修改必须经关闭确认流程处理，而不是被
        # 新图像工程静默覆盖。放在旧素材保存之后、新素材状态写入之前：
        # 用户取消时文档与面板状态均保持原样，加载就此中止。
        if create_standalone_session and not self._confirm_project_replacement_for_image(path):
            return

        # Check if original image exists
        image_exists = os.path.exists(path)
        full, high, low = FileIO.load_image(path) if image_exists else (None, None, None)

        # Offline Media Handling: Check if we can enter offline mode
        if full is None:
            # Try to load project data to check for baked caches
            topos_path = project_path or self.save_router._get_project_path_for_file(path)
            project_data = self.save_router.load_single_asset_data(topos_path) if topos_path else None

            if isinstance(project_data, dict):
                layers = project_data.get('layers') or []
            elif isinstance(project_data, list):
                layers = project_data
            else:
                layers = []

            # Check if any layer has baked cache
            has_baked_cache = any(
                getattr(layer, 'is_baked', False) and getattr(layer, 'baked_file_name', None)
                for layer in layers
            )

            if has_baked_cache and len(layers) > 0:
                # Enter offline mode - use baked cache as fallback
                logger.info(f"image not found: {path}")
                logger.info("offline mode with baked cache...")

                if self.engine.enter_offline_mode(layers):
                    self.current_file_path = None  # No valid source path
                    self.engine.layers = layers
                    self.engine.active_layer_index = 0

                    # Update UI for offline mode
                    self.layer_panel.update_layers(self.engine.layers, 0, is_offline_mode=True)
                    self.update_canvas(is_new_source=True)
                    self.canvas.fit_to_window()
                    self.engine.sync_all_resources()
                    self.update_from_layer(self.engine.get_active_layer())
                    if self.engine.get_active_layer():
                        self.mask_panel.update_shapes_list(self.engine.get_active_layer().mask_shapes)

                    # Restore AI plugin results from cached EXR files
                    self._restore_ai_plugin_results()

                    # Disable RAW panel in offline mode
                    self.raw_panel.reset_all()
                    self.raw_panel.set_enabled_for_file(False)

                    self.status_bar.showMessage(
                        I18N.tr("status.offline_mode", "⚠️ 离线缓存模式：找不到原始图片"),
                        5000)

                    self.request_render()
                    return
                else:
                    # Failed to enter offline mode (cache load error)
                    from PyQt6.QtWidgets import QMessageBox
                    QMessageBox.critical(self, I18N.tr("error.title", "错误"),
                        I18N.tr("error.offline_cache_failed",
                            f"无法加载离线缓存。\n原始图片不存在：{path}"))
                    return
            else:
                # No baked cache available - show normal error
                from PyQt6.QtWidgets import QMessageBox
                QMessageBox.critical(self, I18N.tr("error.title", "错误"),
                    I18N.tr("error.file_not_found", f"找不到文件：{path}"))
                return

        # Normal loading - image exists
        # Exit offline mode if previously in it
        if self.engine.is_offline_mode:
            self.engine.exit_offline_mode()

        self.current_file_path = path
        self.current_file_is_video = False
        # 切换到新图片时清除 save_router 的路径缓存。
        # 否则缓存还指向上一张图的 .topos，按 Cmd+S 会把当前图存到旧路径。
        self.save_router.clear_cache()
        if project_path:
            self.save_router.set_project_path(project_path)
        self._add_to_recent_files(path)

        if create_standalone_session:
            # AUD-2805：替换中止（用户取消保存确认）时不得继续加载。
            if not self._ensure_image_project(path, project_path=project_path):
                return

        # P9.1: Create ColorSession for single asset (only when loading standalone, not for timeline clips)
        if create_standalone_session:
            self._create_standalone_image_color_session(path, project_path=project_path)

        directory = os.path.dirname(path)
        self.config_manager.set("last_path", directory); self.config_manager.save_config()
        self.library_panel.set_root_path(directory)
        # [R0-7] 元数据（exiftool 子进程 ≤2s）挪出 UI 线程，异步回填 info_panel
        self._request_metadata_async(path)

        # Color management: auto-detect ICC profile and warn on mismatch
        _, self._source_color_space = self.color_manager.auto_assign_icc(path, full)
        warning = self.color_manager.check_color_space_mismatch(path)
        if warning:
            QMessageBox.warning(
                self,
                I18N.tr("color_mgmt.warning_title", "Color Space Notice"),
                I18N.tr("color_mgmt.mismatch", warning))

        # Load project data (Layers + RAW params)
        topos_path = project_path or self.save_router._get_project_path_for_file(path)
        project_data = self.save_router.load_single_asset_data(topos_path) if topos_path else None
        loaded_raw_params = None
        source_bake = None
        flip_state = None
        restored_composite_graph = False

        if isinstance(project_data, dict):
            self.engine.layers = project_data.get('layers') or [Layer("Layer 1")]
            loaded_raw_params = project_data.get('raw_params')
            source_bake = project_data.get('source_bake')
            flip_state = project_data.get('flip_state')
            # Restore composite graph if present
            composite_graph_data = project_data.get('composite_graph')
            if composite_graph_data and composite_graph_data.get("nodes"):
                restored_composite_graph = True
            if composite_graph_data and self._composite_available and self.composite_page:
                if self._composite_adapter:
                    if self.current_file_path:
                        project_dir = os.path.dirname(os.path.abspath(self.current_file_path))
                        self._composite_adapter.restore_graph(composite_graph_data, project_dir)
                    else:
                        self._composite_adapter.restore_graph(composite_graph_data, "")
                else:
                    try:
                        from src.features.composite.facade import get_node_graph_class
                        NodeGraph = get_node_graph_class()
                    except ImportError:
                        logger.warning("Composite module unavailable; skipping graph restore")
                        NodeGraph = None
                    if NodeGraph is not None:
                        if self.current_file_path:
                            project_dir = os.path.dirname(os.path.abspath(self.current_file_path))
                            self._absolutize_composite_paths(composite_graph_data, project_dir)
                        nodes_data = composite_graph_data.get("nodes", [])
                        if nodes_data:
                            restored_graph = NodeGraph.from_dict(composite_graph_data)
                            self.composite_page._node_graph = restored_graph
                            self.composite_page._node_editor.build_from_graph(restored_graph)
                            if hasattr(self.composite_page, '_undo_stack'):
                                self.composite_page._undo_stack.clear()

            # Restore composite viewer state if present
            composite_state_data = project_data.get('composite_state')
            if composite_state_data and self._composite_available and self.composite_viewer:
                if self._composite_adapter:
                    self._composite_adapter.restore_viewer_state(composite_state_data)
                else:
                    self.composite_viewer.set_state(composite_state_data)
        elif isinstance(project_data, list): # Backward compatibility
            self.engine.layers = project_data
        else:
            self.engine.layers = [Layer("Layer 1")]

        # Auto-switch Composite graph to ImageInput for standalone images
        # when no saved composite graph was restored above.
        if create_standalone_session:
            self._update_composite_for_standalone_image(path, restored_composite=restored_composite_graph)

        # Whole-frame video motion must never carry into still-image tools.
        self.engine.set_video_stabilization_state({})
        # Load original image first (sets img_source_full = original)
        self.engine.set_image(full, high, low, path=path)

        # Restore source bake (denoise/deblur result) if present
        if source_bake and source_bake.get("source_bake_file_name"):
            baked = FileIO.load_baked_layer(source_bake["source_bake_file_name"])
            if baked is not None:
                # load_baked_layer returns RGB, engine expects BGR
                baked_bgr = cv2.cvtColor(baked, cv2.COLOR_RGB2BGR)
                baked_high = FileIO._resize_to_fit(baked_bgr, FileIO.HIGH_RES_SIZE)
                baked_low = FileIO._resize_to_fit(baked_bgr, FileIO.LOW_RES_SIZE)
                # is_new_file=False preserves img_source_full as the true original
                self.engine.set_image(baked_bgr, baked_high, baked_low, is_new_file=False)
                self.engine.source_bake_file_name = source_bake["source_bake_file_name"]
                self.engine.source_bake_type = source_bake.get("source_bake_type")
                # Update panel baked states
                if source_bake.get("source_bake_type") == "denoise":
                    self.denoise_panel.set_baked_state(True)
                elif source_bake.get("source_bake_type") == "deblur":
                    self.deblur_panel.set_baked_state(True)
        self.layer_panel.set_source_bake_type(self.engine.source_bake_type)
        self.layer_panel.update_layers(self.engine.layers, 0, is_offline_mode=False)
        self.update_canvas(is_new_source=True); self.canvas.fit_to_window()

        # Restore flip state after image is loaded
        if flip_state:
            self.engine.set_flip_state(flip_state)

        self.engine.sync_all_resources(); self.update_from_layer(self.engine.get_active_layer())
        if self.engine.get_active_layer(): self.mask_panel.update_shapes_list(self.engine.get_active_layer().mask_shapes)

        # Cleanup orphaned baked cache files
        self._cleanup_baked_cache()

        # Restore AI plugin results from cached EXR files
        self._restore_ai_plugin_results()

        # RAW Panel handling
        # 扩展名清单引用引擎权威定义：此前这里硬编码 7 种、引擎按 12 种
        # （多 .sr2/.srf/.rw2/.pef/.x3f），打开这些文件时引擎按 RAW 处理、
        # RAW 面板却按"非 RAW"默认，状态机分叉。
        from src.features.color.core.image_engine import ImageEngine as _IE
        is_raw = os.path.splitext(path)[1].lower() in _IE.RAW_EXTENSIONS
        # 每次加载新图片时，先重置面板到初始状态，再针对文件类型进行个性化设置
        # 顺序不能颠倒，否则 reset_all 会覆盖针对文件类型的特定默认值（如曝光度）
        self.raw_panel.reset_all()
        self.raw_panel.set_enabled_for_file(is_raw)

        # If project had saved RAW params, restore them now (overriding defaults)
        if loaded_raw_params:
            self.raw_panel.set_params(loaded_raw_params)

        # Reset denoise/deblur baked state for new files without source bake
        if not source_bake:
            self.denoise_panel.set_baked_state(False)
            self.deblur_panel.set_baked_state(False)

        self.request_render()

        # 将当前文件添加到导出队列
        self.export_panel.add_current_file(path)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save_file(self) -> None:
        """
        统一保存入口 - 使用 SaveRouter。

        Cmd+S 的统一入口，按当前上下文智能路由：
        - 激活的 Motion Graphic 编辑器 → 保存该场景（见下方路由）
        - 有未保存过的工程（无 filepath）→ 统一另存为对话框
        - 否则 → save_router.save()，按 current_page 路由：
          - Library/Color: 保存当前工程的调色栈
          - Edit: 保存时间线与工程状态
          - Composite: 保存当前工程的合成数据
          - Deliver: 保存当前工程状态

        红线：所有保存都写入当前 .topos 工程包。
        """
        # Cmd+S 是 ApplicationShortcut（菜单命令需要跨 dock 生效），它会在
        # Motion Graphic 编辑器（modeless Tool 窗口）激活时仍然触发——若不
        # 在这里路由，用户在编辑器里按 Cmd+S 保存的是工程而非场景，编辑器
        # 自己的 Ctrl+S action 永远收不到这个键。见 shortcut_scope.py。
        editor = active_motion_graphic_editor()
        if editor is not None and editor is not self:
            editor.save()
            self.status_bar.showMessage(
                I18N.tr("status.mg_scene_saved", "Motion Graphic scene saved."), 3000
            )
            return

        # 智能路由：有工程但还没保存路径 → 走统一另存为流程
        # （save_router.save() 在 EditProject 无 filepath 时只会返回 False，体验差）
        if (getattr(self, '_current_edit_project', None) is not None
                and not getattr(self, '_current_edit_project_filepath', None)
                and hasattr(self, '_save_current_project')):
            self._save_current_project()
            return

        # 使用 SaveRouter 统一保存（按 current_page 路由）
        success = self.save_router.save()
        if success:
            # 获取保存的目标路径
            target = self.save_router.get_save_target()
            if target:
                self.library_panel.list_view.viewport().update()
                self.status_bar.showMessage(
                    f"Project saved: {os.path.basename(target.project_path)}", 3000
                )
                # 重置 dirty 标记
                self._clear_all_dirty_states()  # P0-5: Saved successfully, clear all dirty states
        else:
            # SaveRouter 失败，显示错误并保留 dirty 状态
            self.status_bar.showMessage(
                "Save failed. Please check project path and try again.", 5000
            )
            # 保持 dirty 状态，不换保存路径兜底

    # ------------------------------------------------------------------
    # Backup / Auto-save / Periodic save
    # ------------------------------------------------------------------

    def _start_backup_timer(self) -> None:
        """Start or stop the periodic auto-backup timer based on config."""
        interval_min = self.config_manager.get("auto_backup_interval") or 0
        self._backup_timer.stop()
        if interval_min > 0:
            self._backup_timer.start(interval_min * 60 * 1000)

    def _perform_auto_backup(self) -> None:
        """Periodic auto-backup callback."""
        if not self.current_file_path and not self._current_edit_project_filepath:
            return
        backup_dir = self.config_manager.get("backup_directory") or ""
        max_backups = self.config_manager.get("max_backup_count") or 5
        self.save_router.backup_project_async(
            backup_dir=backup_dir,
            max_backups=max_backups,
        )

    def _perform_periodic_save(self) -> None:
        """Periodic full-save callback (separate from 2s debounce autosave).

        AUD-2806: 全量保存是同步磁盘写（.topos 打包），QTimer 回调在 UI
        线程执行会周期性阻塞界面。有 edit 工程上下文时，仅在存在脏标记
        （edit/color/composite/deliver 任一）或工程未命名时才真正落盘，
        跳过无变化的周期；Library 单素材模式（无 edit 工程）保持旧行为。
        """
        if not ((self.current_file_path or self._current_edit_project_filepath) and self.engine):
            return
        if getattr(self, '_current_edit_project', None) is not None:
            has_dirty = any(
                getattr(self, flag, False)
                for flag in ('edit_project_dirty', 'color_session_dirty',
                             'composite_dirty', 'deliver_dirty')
            )
            if not has_dirty and self._current_edit_project_filepath:
                logger.debug("periodic save skipped: project clean")
                return
        self.save_router.save()

    # ------------------------------------------------------------------
    # Canvas / Undo-Redo action update
    # ------------------------------------------------------------------

    def update_canvas(self, is_new_source=False) -> None:
        img = self.engine.get_display_image()
        if img is not None:
            self.canvas.set_image(img, is_new_source=is_new_source)
            self.canvas.update_mask_overlay(self.engine.get_active_layer())
        self.update_undo_redo_actions()

    def update_undo_redo_actions(self) -> None:
        can_undo = self.engine.can_undo()
        can_redo = self.engine.can_redo()
        undo_label = None
        redo_label = None

        versions = getattr(self, "_clip_version_request_host", None)
        if versions is not None:
            if versions.history_target() is not None:
                can_undo, undo_label = True, versions.get_undo_label()
            if versions.history_target(redo=True) is not None:
                can_redo, redo_label = True, versions.get_redo_label()

        # E11: Edit 页激活时以时间线/媒体池命令栈为准——时间线编辑此前
        # 只进 TimelineView 独立命令栈，菜单 Undo 一直 disabled。
        if getattr(self, "_edit_available", False) and getattr(self, "current_page", None) == "Edit":
            panel = getattr(self, "timeline_panel", None)
            view = (
                panel.get_timeline_view()
                if panel is not None and hasattr(panel, "get_timeline_view")
                else getattr(panel, "timeline_view", None)
            )
            from PyQt6.QtWidgets import QApplication

            def _inside(widget, host) -> bool:
                while widget is not None:
                    if widget is host:
                        return True
                    widget = widget.parentWidget()
                return False

            focus = QApplication.focusWidget()
            media_pool = getattr(self, "media_pool_panel", None)
            if (media_pool is not None and focus is not None
                    and _inside(focus, media_pool)):
                can_undo = media_pool.can_undo()
                can_redo = media_pool.can_redo()
                undo_label = media_pool.get_undo_label()
                redo_label = media_pool.get_redo_label()
            elif view is not None:
                can_undo = view.can_undo()
                can_redo = view.can_redo()
                stack = getattr(view, "_command_stack", None)
                if stack is not None:
                    undo_label = stack.get_undo_label()
                    redo_label = stack.get_redo_label()

        if hasattr(self, 'undo_action'):
            self.undo_action.setEnabled(can_undo)
            self.redo_action.setEnabled(can_redo)

        if hasattr(self, 'undo_action_menu'):
            self.undo_action_menu.setEnabled(can_undo)
            self.redo_action_menu.setEnabled(can_redo)
            if undo_label:
                self.undo_action_menu.setText(f"Undo {undo_label}")
            if redo_label:
                self.redo_action_menu.setText(f"Redo {redo_label}")

    # ------------------------------------------------------------------
    # Script execution handlers
    # ------------------------------------------------------------------

    def _on_script_finished(self, success: bool, message: str) -> None:
        """Handle script execution completion — update UI to reflect engine state."""
        self.script_panel.set_running(False)
        self.script_panel.append_output(f"\n{message}\n")
        if success:
            self.script_panel.set_status("Completed", "#4CAF50")
        else:
            self.script_panel.set_status("Error", "#f44336")
        self.progress_bar.hide()
        self.status_bar.showMessage(message, 3000)
        # Refresh all UI panels to reflect any changes the script made
        self.layer_panel.update_layers(self.engine.layers, self.engine.active_layer_index)
        self.update_from_layer(self.engine.get_active_layer())
        self.request_render()

    def _on_script_error(self, error_message: str, line_number: int) -> None:
        """Highlight the error line in the script editor."""
        if line_number > 0:
            self.script_panel.highlight_line(line_number)
            self.script_panel.set_status(f"Error: line {line_number}", "#f44336")
        else:
            self.script_panel.set_status("Error", "#f44336")

    def _on_script_progress(self, current: int, total: int) -> None:
        """Update progress bar from script engine."""
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(current)
            self.progress_bar.show()

    def _on_script_status(self, message: str) -> None:
        """Update status bar message from script engine."""
        self.status_bar.showMessage(message, 0)

    def _on_action_registered(self, name: str) -> None:
        """Handle a new action being registered — add to Scripts menu."""
        if name not in self._script_action_items:
            self._scripts_no_actions_act.setVisible(False)
            act = self._scripts_menu.addAction(name)
            act.triggered.connect(lambda checked, n=name: self._run_registered_action(n))
            self._script_action_items[name] = act

    def _refresh_scripts_menu(self) -> None:
        """Sync the Scripts menu with the engine's registered actions."""
        registered = self.script_engine.get_registered_actions()
        # Remove actions no longer registered
        for name in list(self._script_action_items.keys()):
            if name not in registered:
                act = self._script_action_items.pop(name)
                self._scripts_menu.removeAction(act)
        # Add new actions
        for name in registered:
            if name not in self._script_action_items:
                self._scripts_no_actions_act.setVisible(False)
                act = self._scripts_menu.addAction(name)
                act.triggered.connect(lambda checked, n=name: self._run_registered_action(n))
                self._script_action_items[name] = act
        # Show/hide placeholder
        self._scripts_no_actions_act.setVisible(len(registered) == 0)

    def _run_registered_action(self, name: str) -> None:
        """Execute a registered script action."""
        self.script_panel.set_running(True)
        self.script_panel.append_output(f"\n--- Running action: {name} ---\n")
        self.script_engine.run_registered_action(name)

    # ------------------------------------------------------------------
    # Idle render
    # ------------------------------------------------------------------

    def perform_idle_render(self) -> None:
        self.engine.set_interaction_mode(False)

        # If we were just interacting with RAW, trigger full render if not already done
        if self.engine.current_raw_params.get('enabled', False):
             # This will trigger a full quality decode because is_interacting is now False
             self.engine.on_raw_params_changed(self.engine.current_raw_params)

        try:
            # frame_ready requests the single curve underlay only after the
            # final render is published. Requesting here would analyse the
            # previous preview, then repeat the work for the final image.
            self.on_render_finished(None, False)
        except Exception:
            logger.debug("Render finished callback failed")

    # ------------------------------------------------------------------
    # Recent files
    # ------------------------------------------------------------------

    def _add_to_recent_files(self, filepath: str) -> None:
        """Add a file to the recent files list and rebuild menu."""
        if not filepath:
            return
        recent = list(self.config_manager.get("recent_files") or [])
        max_count = self.config_manager.get("max_recent_files") or 10
        # Remove duplicate if present, then prepend
        abs_path = os.path.abspath(filepath)
        recent = [p for p in recent if os.path.abspath(p) != abs_path]
        recent.insert(0, filepath)
        recent = recent[:max_count]
        self.config_manager.set("recent_files", recent)
        self._rebuild_recent_menu()

    def _open_recent_file(self) -> None:
        """Open a file from the Recent Files menu."""
        action = self.sender()
        if not action:
            return
        filepath = action.data()
        if filepath and os.path.exists(filepath):
            self._open_media_path(filepath)

    # ------------------------------------------------------------------
    # Edit project helpers
    # ------------------------------------------------------------------

    def _get_current_edit_project(self) -> EditProject | None:
        """Get current EditProject from media pool panel."""
        return self._current_edit_project

    def _get_edit_ui_state(self) -> UIState | None:
        """Get current UI state for Edit project."""
        if self._current_edit_project is None:
            return None

        # Get UI state from panels
        ui_state = UIState(
            active_timeline_id=self._current_edit_project.active_timeline_id,
            playhead_frame=self.timeline_panel.get_playhead_frame() if hasattr(self.timeline_panel, 'get_playhead_frame') else 0,
            zoom_level=self.timeline_panel.get_zoom_level() if hasattr(self.timeline_panel, 'get_zoom_level') else 1.0,
            scroll_position=0,
            selected_clip_ids=self.timeline_panel.get_selected_clip_ids() if hasattr(self.timeline_panel, 'get_selected_clip_ids') else [],
            selected_track_ids=self.timeline_panel.get_selected_track_ids() if hasattr(self.timeline_panel, 'get_selected_track_ids') else [],
            expanded_bins=self.media_pool_panel.get_expanded_bins() if hasattr(self.media_pool_panel, 'get_expanded_bins') else [],
            view_mode=self.media_pool_panel.get_view_mode().name.lower() if hasattr(self.media_pool_panel, 'get_view_mode') else "list",
        )
        # SUB-P1-06: capture per-timeline layout (track heights/scroll/area
        # ratios) so reopening the project restores the arrangement.
        try:
            view = getattr(self.timeline_panel, 'timeline_view', None)
            if view is not None and getattr(view, '_timeline', None) is not None:
                ui_state.timeline_layout = view.layout_state()
        except Exception:
            logger.debug("Failed to capture timeline layout state", exc_info=True)
        # W07（接线计划 §6）：时间线展示偏好（缩略图/波形/zoom preset 与
        # 自定义 ppf+scroll/Position Lock/Tray/源目的轨 UI 路由）。
        try:
            export = getattr(self.timeline_panel, 'export_presentation_state', None)
            if callable(export):
                presentation = export()
                if presentation:
                    ui_state.timeline_presentation = presentation
        except Exception:
            logger.debug("Failed to capture timeline presentation state",
                         exc_info=True)
        return ui_state

    def _get_timeline_sequences(self) -> dict[str, TimelineSequence] | None:
        """Get current TimelineSequence dictionary."""
        sequences = dict(self._current_timeline_sequences or {})

        # The TimelineView owns the newest immutable snapshot immediately
        # after a command (split/trim/move).  Usually the timeline_changed
        # signal has already copied it into the controller cache, but taking
        # the active editor snapshot here closes that race for Ctrl+S and
        # background autosave without dropping other timelines.
        active_timeline = getattr(
            getattr(self, "edit_page", None),
            "_current_timeline",
            None,
        )
        active_id = getattr(active_timeline, "timeline_id", None)
        if active_timeline is not None and active_id:
            sequences[active_id] = active_timeline

        return sequences

    def _setup_save_router_callbacks(self) -> None:
        """
        设置 SaveRouter 回调。

        SaveRouter 使用回调模式获取状态，避免直接依赖 MainWindow 内部状态。
        """
        self.save_router.set_callbacks({
            "get_current_page": lambda: self.current_page,
            "get_current_file_path": lambda: self.current_file_path,
            "get_current_edit_project": lambda: self._current_edit_project,
            "get_edit_project_filepath": lambda: self._current_edit_project_filepath,
            "get_current_timeline_sequences": self._get_timeline_sequences,
            "get_current_asset_id": lambda: self._color_session_manager.get_current_asset_id(),
            "get_current_clip_id": lambda: self._color_session_manager.get_current_clip_id(),
            "get_current_stack_id": lambda: self._color_session_manager.get_current_color_stack_id(),
            "get_current_composite_id": lambda: getattr(self, '_current_composite_id', None),
            "get_current_timeline_id": lambda: self._color_session_manager.get_current_timeline_id() if self._color_session_manager.has_active_session() else (self._current_edit_project.active_timeline_id if self._current_edit_project else None),
            "get_engine": lambda: self.engine,
            "get_edit_project_io": lambda: self.edit_project_io,
            "get_work_mode": self._get_work_mode,
            "get_composite_graph_data": self._get_composite_graph_data,
            "get_composite_viewer_state": self._get_composite_state_data,
            "get_ui_state": self._get_edit_ui_state,
            "get_teaching_animation_payload": self._get_teaching_animation_payload,
            # P9.1: ColorSession 回调
            "get_current_color_session": lambda: self._color_session_manager.get_current_session(),
            # P0-4: EditProject 更新回调（保持内存数据一致性）
            "on_edit_project_updated": self._on_edit_project_updated_from_save_router,
            # P0-4: Composite ID 创建回调（绑定闭环）
            "on_composite_id_created": self._on_composite_id_created,
        })
        logger.debug("SaveRouter callbacks configured")

    def _get_teaching_animation_payload(self) -> dict[str, Any] | None:
        """Collect scene documents and stable host references for .topos IO."""
        try:
            from src.features.teaching_animation.core.model import (
                MotionGraphicClip, MotionGraphicNode, TeachingAnimationProjectPayload,
            )
            from src.features.teaching_animation.facade import serialize_project_payload
            from src.features.teaching_animation.host_adapters import motion_graphic_scene_id
            from src.features.teaching_animation.scene_store import get_shared_scene_store

            clips = []
            for sequence in (self._get_timeline_sequences() or {}).values():
                for track in getattr(sequence, "video_tracks", ()):
                    for clip in getattr(track, "clips", ()):
                        asset_id = str(getattr(clip, "asset_id", ""))
                        scene_id = motion_graphic_scene_id(asset_id)
                        if scene_id is None:
                            continue
                        source_start = int(getattr(clip, "source_start", 0))
                        source_duration = max(1, int(getattr(clip, "source_duration", 1)))
                        clips.append(MotionGraphicClip(
                            clip_id=str(clip.clip_id),
                            scene_id=scene_id,
                            timeline_start=int(clip.timeline_start),
                            timeline_duration=int(clip.timeline_duration),
                            scene_in_frame=source_start,
                            scene_out_frame=source_start + source_duration - 1,
                            composite_id=getattr(clip, "composite_id", None),
                            render_cache_id=getattr(clip, "render_cache_id", None),
                        ))

            nodes = []
            page = getattr(self, "composite_page", None)
            graph = getattr(page, "node_graph", None)
            for node in getattr(graph, "nodes", {}).values():
                if getattr(node, "node_type", "") != "motion_graphic":
                    continue
                scene_id = str(getattr(node, "parameters", {}).get("scene_id", ""))
                if scene_id:
                    nodes.append(MotionGraphicNode(node_id=str(node.id), scene_id=scene_id))

            metadata: dict[str, Any] = {}
            try:
                from src.features.teaching_animation.core.motion_graphic_generator_registry import (
                    get_motion_graphic_generator_registry,
                )
                templates = get_motion_graphic_generator_registry().list_project_templates()
                if templates:
                    metadata["motion_graphic_generators_v1"] = [
                        template.to_dict() for template in templates
                    ]
            except Exception:  # noqa: BLE001 — template palette is optional
                logger.debug("Motion Graphic generator templates not collected", exc_info=True)
            export_tasks = getattr(self, "_export_motion_graphic_task_memory", None)
            if callable(export_tasks):
                task_memory = export_tasks()
                if isinstance(task_memory, dict) and task_memory:
                    metadata["motion_graphic_task_memory_v1"] = task_memory

            payload = TeachingAnimationProjectPayload(
                scenes=get_shared_scene_store().scenes(),
                clips=tuple(clips), nodes=tuple(nodes), metadata=metadata,
            )
            return serialize_project_payload(payload)
        except Exception:  # noqa: BLE001
            logger.debug("Teaching Animation payload collection failed", exc_info=True)
            return None

    def _restore_teaching_animation_payload(self, data: dict[str, Any] | None) -> None:
        try:
            from src.features.teaching_animation.scene_store import get_shared_scene_store
            if data is None:
                get_shared_scene_store().replace_all(())
                try:
                    from src.features.teaching_animation.core.motion_graphic_generator_registry import (
                        get_motion_graphic_generator_registry,
                    )
                    from src.features.teaching_animation.core.motion_graphic_template_library import (
                        load_motion_graphic_template_library,
                    )
                    registry = get_motion_graphic_generator_registry()
                    registry.clear()
                    load_motion_graphic_template_library(registry)
                except Exception:
                    logger.debug("Motion Graphic generator palette clear skipped", exc_info=True)
                restore_tasks = getattr(self, "_restore_motion_graphic_task_memory", None)
                if callable(restore_tasks):
                    restore_tasks(None)
                return
            from src.features.teaching_animation.facade import deserialize_project_payload
            payload = deserialize_project_payload(data)
            get_shared_scene_store().replace_all(payload.scenes)
            try:
                from src.features.teaching_animation.core.html_template import HtmlTemplate
                from src.features.teaching_animation.core.motion_graphic_generator_registry import (
                    get_motion_graphic_generator_registry,
                )
                raw_templates = payload.metadata.get("motion_graphic_generators_v1", ())
                registry = get_motion_graphic_generator_registry()
                registry.clear()
                # Directory packages are application-owned, portable recipes;
                # restore them before the project-specific palette entries.
                # Their runtime preview paths stay out of project metadata.
                from src.features.teaching_animation.core.motion_graphic_template_library import (
                    load_motion_graphic_template_library,
                )
                load_motion_graphic_template_library(registry)
                for raw_template in raw_templates if isinstance(raw_templates, list) else ():
                    if isinstance(raw_template, dict):
                        template = HtmlTemplate.from_dict(raw_template)
                        if not registry.is_packaged(template.template_id, template.version):
                            registry.register(template)
            except Exception:  # noqa: BLE001 — older projects have no palette
                logger.debug("Motion Graphic generator templates not restored", exc_info=True)
            restore_tasks = getattr(self, "_restore_motion_graphic_task_memory", None)
            if callable(restore_tasks):
                metadata = payload.metadata if isinstance(payload.metadata, dict) else {}
                restore_tasks(metadata.get("motion_graphic_task_memory_v1"))
        except Exception:  # noqa: BLE001
            logger.debug("Teaching Animation payload restore failed", exc_info=True)

    def _get_ai_workflow_payload(self) -> dict[str, Any]:
        """Collect the active AI Workflow graph for .topos IO（plan §13）。

        始终返回有效 store dict（可为空）——空存储会覆盖工程里的旧图，
        保持"清空画布 → 保存"的工程边界语义；panel 未建（弹窗未开过）时
        返回当前已保存内容的空壳。
        """
        store: dict[str, Any] = {
            "schema": "topos.workflow-store/1",
            "workflows": [],
            "active_workflow_id": "",
        }
        try:
            panel = getattr(self, "ai_workflow_panel", None)
            graph = getattr(panel, "current_graph", None)
            if graph is None:
                return store
            entry = {
                "graph": graph.to_dict(),
                "skill_id": str(getattr(panel, "current_skill_id", "") or ""),
                "template_id": str(getattr(panel, "current_template_id", "") or ""),
            }
            store["workflows"] = [entry]
            store["active_workflow_id"] = graph.workflow_id
        except Exception:  # noqa: BLE001
            logger.debug("AI Workflow payload collection failed", exc_info=True)
        return store

    def _restore_ai_workflow_payload(self, data: dict[str, Any] | None) -> None:
        """Restore the saved workflow graph into the AI Workflow panel.

        ``data is None``（新工程/旧工程无该模块）→ 清空画布，不继承上一工程
        的图。panel 未建时暂存 payload，弹窗懒建后由 dock 装配消费。
        """
        try:
            if not isinstance(data, dict):
                data = None
            self._pending_ai_workflow_payload = data
            panel = getattr(self, "ai_workflow_panel", None)
            if panel is None:
                return
            load = getattr(panel, "load_graph", None)
            if not callable(load):
                return
            workflows = (data or {}).get("workflows") or []
            active_id = str((data or {}).get("active_workflow_id", "") or "")
            entry = None
            for item in workflows:
                if not isinstance(item, dict):
                    continue
                if not active_id or item.get("graph", {}).get("workflow_id") == active_id:
                    entry = item
                    break
            if entry is None:
                panel.clear_canvas()
                return
            from src.features.ai_workflow.core.workflow_models import (
                parse_workflow_dict,
            )
            graph = parse_workflow_dict(entry.get("graph") or {})
            load(graph, skill_id=str(entry.get("skill_id", "") or ""))
        except Exception:  # noqa: BLE001
            logger.debug("AI Workflow payload restore failed", exc_info=True)

    def _on_edit_project_updated_from_save_router(self, project: EditProject) -> None:
        """
        Handle EditProject update from SaveRouter.

        P0-4: 当 SaveRouter 更新 EditProject（如写入 color_stacks）后，
        通过此回调同步内存中的 _current_edit_project，保持数据一致性。

        Args:
            project: The updated EditProject instance
        """
        if project is None:
            logger.warning("Received None project from SaveRouter")
            return

        self._current_edit_project = project
        self.edit_project_dirty = True
        logger.debug(f"EditProject updated from SaveRouter: color_stacks count={len(project.color_stacks)}")

    def _on_composite_id_created(self, composite_id: str) -> None:
        """
        Handle composite_id auto-creation from SaveRouter.

        P0-4: 当 SaveRouter 保存 composite 时发现没有 composite_id，
        自动生成新 ID 后通过此回调同步到 UI context。

        Args:
            composite_id: 新创建的 composite ID
        """
        self._current_composite_id = composite_id
        logger.info(f"Composite ID created and bound: {composite_id}")

        context = self._page_context_manager.get_current_context()
        if context is not None:
            context.composite_id = composite_id

        clip_id = context.clip_id if context is not None else None
        if not clip_id and hasattr(self, '_color_session_manager'):
            clip_id = self._color_session_manager.get_current_clip_id()

        if not clip_id:
            logger.debug("Composite ID created without an active clip context")
            return

        try:
            updated_sequences = dict(self._current_timeline_sequences or {})
            for timeline_id, sequence in updated_sequences.items():
                for track in sequence.all_tracks:
                    clip = track.get_clip_by_id(clip_id)
                    if clip is None:
                        continue
                    updated_sequences[timeline_id] = sequence.with_clip_updated(
                        clip.with_composite_id(composite_id)
                    )
                    self._current_timeline_sequences = updated_sequences
                    self.edit_project_dirty = True
                    if hasattr(self, 'edit_properties_panel') and self.edit_properties_panel:
                        self.edit_properties_panel.update_composite_status(composite_id)
                    logger.info(f"Bound composite_id={composite_id} to clip={clip_id}")
                    return
        except Exception as e:
            logger.warning(f"Failed to bind composite_id to clip: {e}")

    def _on_media_pool_project_changed(self, project: EditProject) -> None:
        """
        Handle project_changed signal from MediaPoolPanel.

        This ensures MainWindow._current_edit_project stays synchronized
        when media_pool_panel creates or modifies the project (e.g., on import,
        batch-delete via Delete key, or undo/redo on the project command stack).

        For timeline changes specifically (add/remove of a timeline id), this
        also keeps ``_current_timeline_sequences`` in lock-step with
        ``project.timelines`` so the full TimelineSequence objects (with
        tracks/clips) survive undo/redo cycles.

        Args:
            project: The new/modified EditProject instance
        """
        previous_project = self._current_edit_project
        self._current_edit_project = project
        self.timeline_panel.set_project(project)
        self.edit_project_dirty = True

        # E11: 媒体池命令栈（bin 删除/撤销等）变化后刷新全局 Undo/Redo 菜单
        try:
            self.update_undo_redo_actions()
        except Exception:  # noqa: BLE001 菜单刷新失败不阻断工程同步
            logger.debug("update_undo_redo_actions after media pool change failed",
                         exc_info=True)

        # Sync _current_timeline_sequences with project.timelines so a
        # batch-delete (or its undo/redo) cannot leave the cache out of step.
        self._sync_timeline_sequences_after_project_change(previous_project, project)

        sequence_updates = self._consume_project_sequence_updates(project)

        # An asset deletion changes more than the project asset dictionary:
        # timeline clips are references into that dictionary, but the full
        # TimelineSequence objects live in the controller cache.  Keep both
        # sides in sync before handing the new project to the renderer.
        asset_timeline_changed = self._sync_asset_references_after_project_change(
            previous_project, project
        )

        if asset_timeline_changed or sequence_updates:
            self._refresh_active_timeline_after_asset_sync(project)

        # Composite graphs can also hold the deleted asset id.  Keep the
        # active Composite page on the sanitized project snapshot so it does
        # not continue rendering/persisting the pre-delete graph.
        self._sync_active_composite_after_project_change(
            previous_project, project
        )

        # If the active timeline was just restored by an undo, make sure the
        # timeline panel and renderer pick up its full sequence.
        if (
            previous_project is not None
            and previous_project.active_timeline_id != project.active_timeline_id
            and project.active_timeline_id is not None
        ):
            self._on_timeline_switched(project.active_timeline_id)

        self._publish_project_change_to_renderer(
            previous_project, project, sequence_changed=bool(sequence_updates))

        version_host = getattr(self, "_clip_version_request_host", None)
        if version_host is not None:
            version_host.on_project_changed(previous_project, project)

        # Update window title to show dirty state
        if self._current_edit_project_filepath:
            project_name = os.path.basename(self._current_edit_project_filepath)
            self.setWindowTitle(f"Topos - {project_name} *")
        else:
            self.setWindowTitle(f"Topos - {project.name} *")

        logger.debug(f"MediaPool project_changed synced to MainWindow: {project.name}")

    def _publish_project_change_to_renderer(
        self, previous: EditProject | None, current: EditProject, *,
        sequence_changed: bool = False,
    ) -> None:
        """Keep named version metadata from resetting preview decode state."""
        from dataclasses import fields

        renderer = getattr(self, "timeline_preview_renderer", None)
        if renderer is None:
            return
        ignored = {"color_stacks", "color_stack_versions", "revision", "modified_at"}

        def unchanged(left: object, right: object) -> bool:
            # Model equality can compare IDs only (MediaAsset/ColorStack).
            # Immutable payload identity is the safe unchanged-content proof.
            return (left is right or isinstance(left, (str, int, float, bool, type(None)))
                    and type(left) is type(right) and left == right)

        color_only = (previous is not None and not sequence_changed
                      and all(unchanged(getattr(previous, item.name), getattr(current, item.name))
                              for item in fields(current) if item.name not in ignored))
        if color_only:
            method_name = ("update_project_metadata" if previous.color_stacks is current.color_stacks
                           else "update_color_stacks")
            publish = getattr(renderer, method_name, None)
            if callable(publish):
                publish(current)
                return
        renderer.set_project(current)

    def _consume_project_sequence_updates(self, project: EditProject) -> bool:
        """Publish project transaction sequences on execute, Undo and Redo."""
        from dataclasses import replace

        panel = getattr(self, "media_pool_panel", None)
        getter = getattr(panel, "get_command_stack", None)
        stack = getter() if callable(getter) else None
        updates = getattr(stack, "last_sequence_updates", None)
        if not updates:
            return False
        sequences = dict(getattr(self, "_current_timeline_sequences", None) or {})
        for timeline_id, sequence in updates.items():
            if sequence is None:
                sequences.pop(timeline_id, None)
            elif timeline_id in project.timelines:
                current = sequences.get(timeline_id)
                apply_to = getattr(sequence, "apply_to", None)
                if callable(apply_to):
                    if current is None:
                        raise ValueError("Cannot publish a version binding without its timeline")
                    sequence = apply_to(current)
                revision = max(int(getattr(current, "revision", 0)),
                               int(getattr(sequence, "revision", 0))) + 1
                sequences[timeline_id] = replace(sequence, revision=revision)
        self._current_timeline_sequences = sequences
        stack.last_sequence_updates = {}
        return True

    def _sync_asset_references_after_project_change(
        self,
        previous_project: Optional[EditProject],
        current_project: EditProject,
    ) -> bool:
        """Keep cached timeline clips consistent with project asset changes.

        ``EditProject`` owns media-pool assets, while full timeline sequences
        are intentionally kept outside the project as controller-owned
        immutable values.  A project-level asset deletion therefore cannot
        clean timeline clips by itself.  If it did, the preview renderer
        would resolve a dangling ``asset_id`` and fail during compositing.

        The pre-delete sequences are retained in a small in-memory undo cache.
        When the project command stack restores the asset, the exact sequence
        snapshot is restored as well.  A monotonically increasing token keeps
        one batch delete grouped even when several assets share a timeline.
        """
        if previous_project is None:
            return False
        if previous_project.project_id != current_project.project_id:
            # Do not carry an undo snapshot from a different project into a
            # newly opened document, even if both happen to reuse an asset id.
            self._asset_timeline_snapshots_for_undo = {}
            self._asset_timeline_snapshot_counter = 0
            return False

        previous_asset_ids = set(previous_project.assets)
        current_asset_ids = set(current_project.assets)
        removed_asset_ids = previous_asset_ids - current_asset_ids
        added_asset_ids = current_asset_ids - previous_asset_ids
        if not removed_asset_ids and not added_asset_ids:
            return False

        sequences = dict(getattr(self, "_current_timeline_sequences", None) or {})
        changed = False

        if removed_asset_ids:
            affected_sequences: dict[str, Any] = {}
            affected_assets: set[str] = set()
            for timeline_id, sequence in sequences.items():
                referenced_assets = {
                    getattr(clip, "asset_id", None)
                    for track in sequence.all_tracks
                    for clip in getattr(track, "clips", ())
                    if getattr(clip, "asset_id", None) in removed_asset_ids
                }
                referenced_assets.discard(None)
                if not referenced_assets:
                    continue

                affected_sequences[timeline_id] = sequence
                affected_assets.update(referenced_assets)
                updated_sequence = sequence.with_asset_clips_removed(
                    removed_asset_ids
                )
                # TimelineSequence equality is intentionally identity-like
                # (timeline_id only), so compare object identity here rather
                # than ``!=`` when detecting immutable content changes.
                if updated_sequence is not sequence:
                    sequences[timeline_id] = updated_sequence
                    changed = True

            if affected_sequences:
                counter = int(
                    getattr(self, "_asset_timeline_snapshot_counter", 0)
                ) + 1
                self._asset_timeline_snapshot_counter = counter
                snapshot_cache = getattr(
                    self, "_asset_timeline_snapshots_for_undo", None
                )
                if snapshot_cache is None:
                    snapshot_cache = {}
                    self._asset_timeline_snapshots_for_undo = snapshot_cache
                snapshot_bundle = dict(affected_sequences)
                for asset_id in affected_assets:
                    snapshot_cache.setdefault(asset_id, []).append(
                        (counter, snapshot_bundle)
                    )

                logger.info(
                    "Removed %d timeline clip reference(s) for deleted asset(s): %s",
                    sum(
                        sum(
                            1
                            for clip in track.clips
                            if getattr(clip, "asset_id", None)
                            in removed_asset_ids
                        )
                        for sequence in affected_sequences.values()
                        for track in sequence.all_tracks
                    ),
                    ", ".join(sorted(affected_assets)),
                )

        if added_asset_ids:
            snapshot_cache = getattr(
                self, "_asset_timeline_snapshots_for_undo", None
            ) or {}
            candidates: list[tuple[int, dict[str, Any]]] = []
            for asset_id in added_asset_ids:
                candidates.extend(snapshot_cache.get(asset_id, ()))

            if candidates:
                counter, snapshot_bundle = max(candidates, key=lambda item: item[0])
                sequences.update(snapshot_bundle)
                changed = True

                # The same bundle is indexed by every asset in one batch.
                # Consume it from all indexes so a later undo cannot restore
                # the same sequence a second time.
                for asset_id, entries in list(snapshot_cache.items()):
                    remaining = [
                        entry for entry in entries if entry[0] != counter
                    ]
                    if remaining:
                        snapshot_cache[asset_id] = remaining
                    else:
                        snapshot_cache.pop(asset_id, None)

                logger.info(
                    "Restored timeline snapshots after asset undo: %s",
                    ", ".join(sorted(added_asset_ids)),
                )

        if changed:
            self._current_timeline_sequences = sequences
        return changed

    def _refresh_active_timeline_after_asset_sync(
        self,
        project: EditProject,
    ) -> None:
        """Publish a repaired active sequence to the timeline and preview UI."""
        active_timeline_id = project.active_timeline_id
        if not active_timeline_id:
            return
        sequence = (getattr(self, "_current_timeline_sequences", None) or {}).get(
            active_timeline_id
        )
        if sequence is None:
            return

        timeline_panel = getattr(self, "timeline_panel", None)
        if timeline_panel is not None:
            loader = getattr(timeline_panel, "load_timeline", None)
            if callable(loader):
                loader(sequence)

        edit_page = getattr(self, "edit_page", None)
        if edit_page is not None:
            setter = getattr(edit_page, "set_timeline", None)
            if callable(setter):
                setter(sequence)

        renderer = getattr(self, "timeline_preview_renderer", None)
        if renderer is not None:
            setter = getattr(renderer, "set_sequence", None)
            if callable(setter):
                setter(sequence)

    def _sync_active_composite_after_project_change(
        self,
        previous_project: Optional[EditProject],
        current_project: EditProject,
    ) -> None:
        """Refresh the active Composite graph after Media Pool mutations.

        The page owns a live ``NodeGraph`` in addition to the project's
        serialized composition dictionary.  Asset deletion sanitizes the
        latter in ``DeleteAssetCommand``; reload only when that active record
        actually changed, otherwise imports and unrelated Media Pool edits do
        not disturb the user's current graph or undo state.
        """
        page = getattr(self, "composite_page", None)
        if page is None:
            return

        setter = getattr(page, "set_edit_project", None)
        if callable(setter):
            setter(current_project)

        if previous_project is None:
            return
        if previous_project.project_id != current_project.project_id:
            return

        active_composite_id = (
            getattr(page, "_current_composite_id", "")
            or getattr(self, "_current_composite_id", "")
        )
        if not active_composite_id:
            return

        previous_compositions = previous_project.compositions
        current_compositions = current_project.compositions
        composition_removed = (
            active_composite_id in previous_compositions
            and active_composite_id not in current_compositions
        )
        composition_changed = (
            active_composite_id in previous_compositions
            and previous_compositions.get(active_composite_id)
            != current_compositions.get(active_composite_id)
        )

        if composition_removed:
            reset = getattr(page, "reset_to_empty_workspace", None)
            if callable(reset):
                reset()
            else:
                set_active = getattr(page, "set_composition_active", None)
                if callable(set_active):
                    set_active(False)
            self._current_composite_id = ""
            return

        if not composition_changed:
            return

        graph_data = current_compositions.get(active_composite_id)
        loader = getattr(page, "load_composite_data", None)
        if isinstance(graph_data, dict) and callable(loader):
            try:
                loader(graph_data)
            except Exception:
                logger.warning(
                    "Failed to reload sanitized Composite graph %s",
                    active_composite_id,
                    exc_info=True,
                )

    def _sync_timeline_sequences_after_project_change(
        self,
        previous_project: Optional[EditProject],
        current_project: EditProject,
    ) -> None:
        """Mirror ``project.timelines`` keys into ``_current_timeline_sequences``.

        - Newly added ids (e.g. after an undo restores a deleted timeline):
          try to re-use the snapshot the MediaPoolPanel cached at delete time
          so tracks/clips are preserved; only fall back to an empty sequence
          if no snapshot exists.
        - Removed ids (e.g. after a batch delete or redo): drop them from the
          cache so we don't keep stale TimelineSequence objects around.

        No-op when the timelines dict did not change.
        """
        if previous_project is None:
            return

        previous_ids = set(previous_project.timelines.keys())
        current_ids = set(current_project.timelines.keys())
        added = current_ids - previous_ids
        removed = previous_ids - current_ids
        if not added and not removed:
            return

        sequences = getattr(self, "_current_timeline_sequences", None)
        if sequences is None:
            return

        updated_sequences = dict(sequences)

        # Restore newly-appeared timelines from snapshots (best effort)
        for tid in added:
            if tid in updated_sequences:
                continue
            restored = None
            panel = getattr(self, "media_pool_panel", None)
            if panel is not None:
                consumer = getattr(panel, "consume_timeline_snapshot", None)
                if callable(consumer):
                    try:
                        restored = consumer(tid)
                    except Exception:
                        logger.exception(
                            "consume_timeline_snapshot failed for %s", tid
                        )
            if restored is not None:
                updated_sequences[tid] = restored
            else:
                # Snapshot miss — lazily build an empty sequence from the ref
                ref = current_project.timelines.get(tid)
                if ref is None:
                    continue
                builder = getattr(self, "_create_empty_timeline_sequence", None)
                if callable(builder):
                    try:
                        empty_seq = builder(ref)
                    except Exception:
                        logger.exception(
                            "_create_empty_timeline_sequence failed for %s", tid
                        )
                        empty_seq = None
                    if empty_seq is not None:
                        updated_sequences[tid] = empty_seq

        for tid in removed:
            updated_sequences.pop(tid, None)

        self._current_timeline_sequences = updated_sequences

    def _on_asset_double_clicked(self, asset_id: str) -> None:
        """
        Handle asset double-click from MediaPoolPanel.

        P3.1: Opens the asset in Source Viewer for preview.

        Args:
            asset_id: The asset ID to open
        """
        if not self._current_edit_project:
            logger.warning("Cannot open asset: No project loaded")
            return

        asset = self._current_edit_project.assets.get(asset_id)
        if not asset:
            logger.warning(f"Asset not found: {asset_id}")
            return

        if getattr(asset, "is_motion_graphic", False):
            # Motion Graphic is a scene-backed virtual asset.  Route the
            # double-click to the feature-owned editor instead of Source
            # Viewer, which cannot decode a ``topos://`` scene reference.
            registry = getattr(self, "feature_registry", None)
            module = (
                registry.get_module("teaching_animation")
                if registry is not None and hasattr(registry, "get_module")
                else None
            )
            opener = getattr(module, "open_motion_graphic_asset", None)
            if callable(opener) and opener(self, asset.asset_id):
                logger.debug("Opened Motion Graphic editor: %s", asset.asset_id)
            else:
                logger.warning(
                    "Motion Graphic editor unavailable for asset: %s",
                    asset.asset_id,
                )
            return

        # Open in Source Viewer
        self.source_viewer_widget.open_asset(asset)
        # I01（复查计划）：素材打开进入仲裁快照（内容同步；激活由
        # show_source 的 viewport 信号单独产生一次领域请求——一次交互
        # 恰好对应内容+激活两类请求，不重复）。
        coordinator = getattr(self, "_viewport_playback_coordinator", None)
        if coordinator is not None:
            try:
                coordinator.open_source_asset(
                    asset.asset_id, activate=False,
                    via="media_pool_double_click")
            except Exception:
                logger.debug("open_source_asset bookkeeping failed",
                             exc_info=True)

        # Switch to Edit page if not already there
        if self.current_page != "Edit":
            self.switch_to_page("Edit")

        viewport = getattr(self, "edit_viewport", None)
        if viewport is not None:
            viewport.show_source(focus=True)
            self.dock_program_viewer.show()
            self.dock_program_viewer.raise_()
        else:
            # Compatibility with lightweight hosts that still own two docks.
            source_dock = getattr(self, "dock_source_viewer", None)
            if source_dock is not None:
                source_dock.raise_()

        # A double-click is an explicit request to work with this source.
        # Move focus off the media pool so Space immediately controls Source.
        source_canvas = getattr(self.source_viewer_widget, "viewer", None)
        if source_canvas is not None:
            source_canvas.setFocus()

        logger.debug(f"Asset opened in Source Viewer: {asset.display_name}")

    def _on_asset_selected(self, asset_id: str) -> None:
        """Handle single-click asset selection from MediaPoolPanel.

        Loads the asset into the Source Viewer for quick preview without
        raising the dock or switching pages — the double-click handler
        (_on_asset_double_clicked) does the full "open + focus + switch".

        This mirrors Finder/NLE behavior where a single click shows a
        lightweight preview and a double-click fully opens the item.
        """
        if not self._current_edit_project:
            return
        asset = self._current_edit_project.assets.get(asset_id)
        if not asset:
            return
        if getattr(asset, "is_motion_graphic", False):
            return
        # Only load if the Source Viewer is not already showing this asset,
        # avoiding redundant decodes when re-selecting the same item.
        current = getattr(self.source_viewer_widget, "current_asset", None)
        if current is not None and getattr(current, "asset_id", None) == asset_id:
            return
        try:
            self.source_viewer_widget.open_asset(asset)
        except Exception:
            logger.exception("Failed to preview asset on select: %s", asset_id)
            return
        # I01：单击预览同样是内容入口——同步快照（不激活，不抢焦点）。
        coordinator = getattr(self, "_viewport_playback_coordinator", None)
        if coordinator is not None:
            try:
                coordinator.open_source_asset(
                    asset.asset_id, activate=False,
                    via="media_pool_single_click")
            except Exception:
                logger.debug("open_source_asset bookkeeping failed",
                             exc_info=True)

    # ------------------------------------------------------------------
    # Timeline preview renderer
    # ------------------------------------------------------------------

    def _init_timeline_preview_renderer(self) -> None:
        """
        Initialize or recreate TimelinePreviewRenderer.

        Creates the renderer when:
        - There is an active EditProject
        - There is an active TimelineSequence

        The renderer handles:
        - Playhead frame preview rendering
        - Cache management
        - Signal connections to ProgramViewer
        """
        # Shutdown existing renderer first
        if self.timeline_preview_renderer:
            old_renderer = self.timeline_preview_renderer
            self._disconnect_timeline_preview_renderer_signals(old_renderer)
            old_renderer.shutdown()
            old_renderer.deleteLater()
            self.timeline_preview_renderer = None

        # Check prerequisites
        if not self._current_edit_project:
            logger.debug("[TimelinePreviewRenderer] No EditProject, skipping init")
            return

        # Get active timeline sequence
        active_timeline_id = self._current_edit_project.active_timeline_id
        if not active_timeline_id:
            logger.debug("[TimelinePreviewRenderer] No active timeline, skipping init")
            return

        sequence = self._current_timeline_sequences.get(active_timeline_id)
        if not sequence:
            logger.warning(
                f"[TimelinePreviewRenderer] Timeline sequence not found: {active_timeline_id}"
            )
            return

        # NOTE: The canvas GL viewport's QOpenGLContext is managed by Qt's
        # internal framework.  Calling doneCurrent()/makeCurrent() on it
        # (which VideoAsyncRenderer does during shared-context creation)
        # causes a native segfault when the widget is not actively rendering.
        # For now, pass None so the preview renderer uses CPU-only mode initially.
        # GPU-accelerated preview is initialized after ProgramViewer's GL context
        # is ready, via the gl_initialized signal → _init_gpu_pipeline().
        gl_context = None

        preview_viewport_size = self._monitor_render_viewport_size((1280, 720))
        if self.program_viewer_widget:
            viewer = self.program_viewer_widget.get_viewer()
            if viewer is not None and viewer.width() > 0 and viewer.height() > 0:
                preview_viewport_size = self._monitor_render_viewport_size(
                    (viewer.width(), viewer.height())
                )

        # Create renderer using factory function
        try:
            self.timeline_preview_renderer = create_timeline_preview_renderer(
                project=self._current_edit_project,
                sequence=sequence,
                main_context=gl_context,
                config={
                    "viewport_size": preview_viewport_size,
                    # The current machines can decode 4K comfortably. Keep
                    # 4K timelines native and only reduce larger 6K/8K
                    # timelines to a 4K-class preview; scrubbing must not
                    # silently fall back to a blurry 720p frame.
                    "max_preview_edge": 3840,
                    "scrub_preview_edge": 3840,
                    "cache_size": 16,
                }
            )

            # P1-02: 注入嵌套序列子序列解析器（渲染嵌套片段时按 id 取子序列，
            # 子序列被编辑后父序列画面在下一次渲染即时反映）。
            if hasattr(self.timeline_preview_renderer, "set_sequence_resolver"):
                self.timeline_preview_renderer.set_sequence_resolver(
                    lambda tid: (self._current_timeline_sequences or {}).get(tid)
                )

            # P1-02: 同一解析器也注入 EditPage 音频同步——嵌套片段引用的子序列
            # 音频轨展开进父时间线实时混音（与导出共用 flatten 纯函数）。
            if hasattr(self.edit_page, "set_audio_sequence_resolver"):
                self.edit_page.set_audio_sequence_resolver(
                    lambda tid: (self._current_timeline_sequences or {}).get(tid)
                )

            # Connect signals
            self._connect_timeline_preview_renderer_signals()

            # Set timeline to EditPage
            self.edit_page.set_timeline(sequence)

            # Connect to ProgramViewer's GL initialized signal for GPU pipeline
            if self.program_viewer_widget:
                viewer = self.program_viewer_widget.get_viewer()
                if viewer is not None:
                    viewer.gl_initialized.connect(self._init_gpu_pipeline)
                    # If GL is already initialized, trigger immediately
                    if viewer._gl_initialized:
                        ctx = viewer.context()
                        if ctx is not None:
                            self._init_gpu_pipeline(ctx)

            self._request_current_timeline_preview_frame()

            logger.info(
                f"[TimelinePreviewRenderer] Initialized for timeline: {active_timeline_id}"
            )

        except Exception as e:
            logger.error(f"[TimelinePreviewRenderer] Failed to initialize: {e}")
            self.timeline_preview_renderer = None

    def _connect_timeline_preview_renderer_signals(self) -> None:
        """Connect TimelinePreviewRenderer signals to UI components."""
        if not self.timeline_preview_renderer:
            return

        # EditPage.timeline_frame_requested -> Renderer.request_render
        self.edit_page.timeline_frame_requested.connect(
            self.timeline_preview_renderer.request_render
        )
        if hasattr(self.edit_page, 'timeline_scrub_started') and hasattr(
            self.timeline_preview_renderer,
            'begin_scrub',
        ):
            self.edit_page.timeline_scrub_started.connect(
                self.timeline_preview_renderer.begin_scrub
            )
        if hasattr(self.edit_page, 'timeline_scrub_frame_requested') and hasattr(
            self.timeline_preview_renderer,
            'request_scrub_preview',
        ):
            self.edit_page.timeline_scrub_frame_requested.connect(
                self.timeline_preview_renderer.request_scrub_preview
            )
        if hasattr(self.edit_page, 'timeline_scrub_finished') and hasattr(
            self.timeline_preview_renderer,
            'finish_scrub',
        ):
            self.edit_page.timeline_scrub_finished.connect(
                self.timeline_preview_renderer.finish_scrub
            )

        # Renderer.frame_ready -> ProgramViewerWidget.set_frame
        self.timeline_preview_renderer.frame_ready.connect(
            self._on_timeline_preview_frame_ready
        )
        if hasattr(self.program_viewer_widget, "monitor_frame_ready"):
            try:
                self.program_viewer_widget.monitor_frame_ready.disconnect(
                    self._on_program_monitor_frame_ready
                )
            except (TypeError, RuntimeError):
                pass
            self.program_viewer_widget.monitor_frame_ready.connect(
                self._on_program_monitor_frame_ready
            )
        if hasattr(self.program_viewer_widget, "monitor_gpu_frame_ready"):
            try:
                self.program_viewer_widget.monitor_gpu_frame_ready.disconnect(
                    self._on_program_monitor_gpu_frame_ready
                )
            except (TypeError, RuntimeError):
                pass
            self.program_viewer_widget.monitor_gpu_frame_ready.connect(
                self._on_program_monitor_gpu_frame_ready
            )

        # Renderer.render_error -> log and clear viewer
        self.timeline_preview_renderer.render_error.connect(
            self._on_timeline_preview_render_error
        )

        # Final-4 (FINAL-P0-09): playback_mode_changed -> ProgramViewerWidget.set_realtime_mode。
        # REALTIME 播放时 Viewer 拒绝裸 tuple，强制走 GpuPresentFrame fence/slot 协议。
        if hasattr(self.timeline_preview_renderer, 'playback_mode_changed'):
            if hasattr(self.program_viewer_widget, 'set_realtime_mode'):
                self.timeline_preview_renderer.playback_mode_changed.connect(
                    self.program_viewer_widget.set_realtime_mode
                )

        # P8-02: ProgramViewerWidget.viewport_resized -> Renderer.set_viewport_size
        self.program_viewer_widget.viewport_resized.connect(
            self._on_program_viewer_viewport_resized
        )
        viewer = self.program_viewer_widget.get_viewer()
        if viewer is not None and viewer.width() > 0 and viewer.height() > 0:
            self._on_program_viewer_viewport_resized(viewer.width(), viewer.height())

        # Preview quality selector: ProgramViewerWidget -> Renderer
        if hasattr(self.program_viewer_widget, 'preview_quality_changed'):
            self.program_viewer_widget.preview_quality_changed.connect(
                self.timeline_preview_renderer.set_preview_quality
            )

        # P1-10: 自适应播放质量用户偏好（默认开启；关闭时 engine 钉在
        # NORMAL 档，负载不再降 decode scale）。
        if hasattr(self.timeline_preview_renderer, 'set_adaptive_playback_quality_enabled'):
            try:
                adaptive_enabled = bool(
                    self.config_manager.get("video_preview_adaptive_quality")
                )
            except Exception:
                adaptive_enabled = True
            self.timeline_preview_renderer.set_adaptive_playback_quality_enabled(
                adaptive_enabled
            )

        # 预览关滤波（P6 v2.1 后续）：默认关闭（保持滤波）。开关只作用于
        # 预览解码 request，交付/导出不受影响。
        if hasattr(self.timeline_preview_renderer, 'set_no_deblock'):
            try:
                no_deblock = bool(
                    self.config_manager.get("video_preview_no_deblock")
                )
            except Exception:
                no_deblock = False
            self.timeline_preview_renderer.set_no_deblock(no_deblock)

        # 注入播放控制器引用。渲染器只读取播放状态/方向，真正的帧推进
        # 仍由 EditPlaybackController + TimelinePanel 背压链路统一驱动。
        if hasattr(self, 'timeline_panel') and self.timeline_panel is not None:
            controller = self.timeline_panel.get_playback_controller()
            if controller is not None:
                self.timeline_preview_renderer.set_playback_controller(controller)
            if hasattr(self.timeline_panel, "set_preview_renderer"):
                self.timeline_panel.set_preview_renderer(self.timeline_preview_renderer)

        logger.debug("[TimelinePreviewRenderer] Signals connected")

    def _init_gpu_pipeline(self, gl_context: object) -> None:
        """
        Initialize the GPU pipeline after ProgramViewer's GL context is ready.

        Called when ProgramViewer emits gl_initialized signal.

        Args:
            gl_context: QOpenGLContext from ProgramViewer
        """
        from PyQt6.QtGui import QOpenGLContext

        if not isinstance(gl_context, QOpenGLContext):
            logger.warning("_init_gpu_pipeline: invalid context type")
            return

        if self.timeline_preview_renderer is None:
            logger.warning("_init_gpu_pipeline: no renderer available")
            return

        # Guard against duplicate initialization. ProgramViewer.gl_initialized
        # is documented to fire once, but if the viewer is rebuilt or the
        # signal is re-emitted, this handler would otherwise run again,
        # leak the previous GPU render thread (never stopped), and start a
        # second one sharing the same context. The flag makes the whole
        # pipeline idempotent.
        if getattr(self, '_gpu_pipeline_initialized', False):
            logger.debug("_init_gpu_pipeline: already initialized, skipping")
            return
        self._gpu_pipeline_initialized = True

        # AUD-2813: 渲染线程一旦 start()，装配中途抛异常时也必须有人负责
        # stop——否则该线程仅靠 Qt parent 保命不被 GC abort，却不在任何
        # 关停清单里（closeEvent 只停装配成功路径登记的 renderer）。
        gpu_renderer: Any = None
        try:
            from src.features.edit import facade as edit_facade

            # Initialize Source GPU renderer reference
            self._source_gpu_renderer = None

            # Check pipeline mode
            try:
                from src.shared.video.pipeline_decision import resolve_video_pipeline_mode
                decision = resolve_video_pipeline_mode(self.config_manager)
                if not decision.is_v3_active():
                    logger.info(f"GPU pipeline: {decision}, skipping GPU init")
                    return
            except Exception:
                pass  # Default to trying GPU

            # Create GPU decode manager
            gpu_decode_manager = edit_facade.create_edit_gpu_decode_manager()

            # Create GPU render thread with shared GL context
            gpu_renderer = edit_facade.create_edit_gpu_preview_renderer(
                main_context=gl_context,
                parent=self,
            )

            # Connect GPU renderer errors
            gpu_renderer.render_error.connect(
                lambda frame, msg: logger.warning(f"GPU render error frame={frame}: {msg}")
            )

            # Start the GPU render thread
            gpu_renderer.start()

            # Pass to TimelinePreviewRenderer (which also connects frame_ready signal)
            self.timeline_preview_renderer.set_gpu_renderer(
                gpu_decode_manager=gpu_decode_manager,
                gpu_renderer=gpu_renderer,
            )

            # 注入主 GL context 给 worker，用于渲染 comp_clip（合成片段嵌入时间线）。
            # worker 在 GUI 线程创建共享 context + offscreen surface 后转交自身线程，
            # 后续 _render_comp_clip_frame 渲染前后 makeCurrent/doneCurrent 它。
            self.timeline_preview_renderer.set_comp_gl_context(gl_context)

            # Store GPU decode manager for Source Viewer provider
            self._gpu_decode_manager = gpu_decode_manager

            # Wire GPU frame provider to Source Viewer.
            # This creates a dedicated GPU renderer sharing SourceViewer's GL context.
            self._init_source_gpu_pipeline(gpu_decode_manager)

            logger.info("GPU pipeline initialized for edit page")

        except Exception as e:
            logger.warning(f"GPU pipeline initialization failed, using CPU fallback: {e}")
            # AUD-2813: 装配失败也要停掉已启动的渲染线程（延迟停止 + 超
            # 时推迟销毁走项目统一的 _stop_gpu_renderer_deferred；stop 对
            # 未启动线程是安全的幂等操作），CPU 路径继续工作。
            if gpu_renderer is not None:
                _stop_gpu_renderer_deferred(gpu_renderer, 1000)
                gpu_renderer = None

    def _init_source_gpu_pipeline(self, gpu_decode_manager: object) -> None:
        """
        Initialize the Source Viewer GPU pipeline.

        Creates a dedicated EditGpuPreviewRenderer sharing SourceViewer's GL context.
        The renderer processes single-layer frames (YUV→RGB conversion) asynchronously.
        CPU fallback runs immediately; the GPU result arrives via signal and overwrites
        with higher quality (progressive refinement pattern).

        Args:
            gpu_decode_manager: EditGpuDecodeManager instance (shared with ProgramViewer)
        """
        if self.source_viewer_widget is None:
            return

        # Try to get SourceViewer's GL context immediately (already initialized)
        viewer = self.source_viewer_widget.viewer
        if viewer is None:
            return

        if viewer._gl_initialized:
            ctx = viewer.context()
            if ctx is not None:
                self._create_source_gpu_renderer(gpu_decode_manager, ctx)
        else:
            # Wait for SourceViewer's GL context to be initialized
            viewer.gl_initialized.connect(
                lambda ctx: self._create_source_gpu_renderer(gpu_decode_manager, ctx)
            )

    def _create_source_gpu_renderer(self, gpu_decode_manager: object, gl_context: object) -> None:
        """
        Create a dedicated GPU renderer for Source Viewer and wire the provider.

        Args:
            gpu_decode_manager: EditGpuDecodeManager instance
            gl_context: SourceViewer's QOpenGLContext
        """
        from PyQt6.QtGui import QOpenGLContext

        if not isinstance(gl_context, QOpenGLContext):
            return

        # Idempotency guard: if a Source GPU renderer is already running,
        # do not create a second one. The same viewer can emit
        # gl_initialized again (e.g. after a rebuild), and without this
        # guard we would leak the previous render thread and start a
        # duplicate sharing the same GL context.
        if getattr(self, '_source_gpu_renderer', None) is not None:
            logger.debug("_create_source_gpu_renderer: already created, skipping")
            return

        try:
            from src.features.edit import facade as edit_facade

            # Create dedicated renderer sharing SourceViewer's GL context
            source_renderer = edit_facade.create_edit_gpu_preview_renderer(
                main_context=gl_context,
                parent=self,
            )

            # Connect error logging.  Use WARNING (not debug) so a renderer
            # that accepts a task but cannot process it (e.g. unsupported
            # frame_type / present-pool exhausted) is visible by default — a
            # silent render_error previously masked the source-playback freeze
            # for several debugging rounds.
            source_renderer.render_error.connect(
                lambda frame, msg: logger.warning(f"Source GPU render error frame={frame}: {msg}")
            )

            # Connect frame_ready to SourceViewerWidget.set_gpu_frame()
            source_renderer.frame_ready.connect(self._on_source_gpu_frame_ready)

            # Connect present_frame_ready: carries the GpuPresentFrame
            # (slot_id) paired with the following frame_ready emission.  The
            # Source viewer must ack slots back to the renderer's
            # OutputSlotPool, otherwise the pool exhausts after one pass of
            # slots and playback freezes (unacked slots are never reused).
            source_renderer.present_frame_ready.connect(
                self._on_source_present_frame_ready
            )
            # VP-B02: present pairing goes through SourcePresentGate so every
            # dropped/displaced/mismatched descriptor reaches an explicit ack
            # (an unacked slot is never reused by the pool).
            self._source_present_gate = SourcePresentGate(
                ack_slot=source_renderer.ack_present_slot
            )

            # Start the render thread
            source_renderer.start()

            # Store reference for cleanup
            self._source_gpu_renderer = source_renderer

            # Wire the provider that submits tasks to this renderer
            self._setup_source_gpu_provider(gpu_decode_manager, source_renderer)

            logger.info("Source Viewer GPU renderer created and started")
        except Exception as e:
            logger.warning(f"Source GPU renderer creation failed: {e}")

    def _on_source_present_frame_ready(self, present: object) -> None:
        """Stash the present descriptor until the paired frame_ready arrives.

        The renderer emits present_frame_ready immediately before
        frame_ready for the same task on the same thread, so queued delivery
        preserves the pairing; the slot_id is matched by frame+generation.
        VP-B02: the gate releases (acks) any pending descriptor this one
        displaces — a dropped pending must never strand its slot.
        """
        gate = getattr(self, '_source_present_gate', None)
        if gate is None:  # renderer created before this wiring existed
            self._source_pending_present = present
            return
        gate.stash(present)

    def _on_source_gpu_frame_ready(self, frame: int, generation: int, tex_id: int, w: int, h: int) -> None:
        """Handle GPU-rendered frame for Source Viewer."""
        gate = getattr(self, '_source_present_gate', None)
        if gate is None:
            return
        # Resolve the present slot backing this frame (if any) so the widget
        # can ack superseded/discarded slots back to the renderer's pool.
        # Mismatched/late descriptors are acked inside the gate (VP-B02).
        present = gate.take(frame=frame, generation=generation)
        widget = self.source_viewer_widget
        if widget is None:
            if present is not None:
                gate.release(present)  # host gone: orphan slot still returns
            return
        slot_id = getattr(present, 'slot_id', -1) if present is not None else -1
        try:
            widget.set_gpu_frame(
                tex_id, w, h, frame=frame, generation=generation, slot_id=slot_id
            )
        except RuntimeError:
            # Widget's C++ side already destroyed (window-close race): give
            # the slot back instead of stranding it in the pool.
            gate.release_slot(slot_id)

    def _setup_source_gpu_provider(self, gpu_decode_manager: object, source_gpu_renderer: object) -> None:
        """
        Wire the GPU decode manager to the Source Viewer via async GPU renderer.

        The provider returns a dict:
        - {'submitted': True}: GPU render task submitted, CPU fallback should be skipped.
          GPU result arrives asynchronously via _on_source_gpu_frame_ready().
        - {'submitted': False}: GPU path unavailable, CPU fallback should run immediately.

        Args:
            gpu_decode_manager: EditGpuDecodeManager instance
            source_gpu_renderer: EditGpuPreviewRenderer for Source Viewer
        """
        if self.source_viewer_widget is None:
            return

        # Present-slot ack wiring: the widget acks slots it has consumed or
        # discarded so the renderer's OutputSlotPool keeps recycling.
        if hasattr(source_gpu_renderer, 'ack_present_slot'):
            self.source_viewer_widget.set_gpu_slot_ack_callback(
                source_gpu_renderer.ack_present_slot
            )

        # Reuse a single decode worker across renderer re-creation.
        worker = getattr(self, '_source_decode_worker', None)
        if worker is None:
            worker = _SourceViewerDecodeWorker(gpu_decode_manager, source_gpu_renderer)
            worker.start()
            self._source_decode_worker = worker
            # CPU fallback runs on the UI thread (QImage upload), requested
            # from the decode worker via queued signal.
            worker.signals.cpu_fallback_requested.connect(
                self.source_viewer_widget.request_cpu_fallback
            )

        def source_gpu_provider(asset_path: str, frame_index: int):
            """
            Hand the frame request to the background decode worker.

            Decode + GPU task submission happen off the UI thread so playback
            ticks never block the event loop.  The result arrives via the
            renderer's frame_ready signal; the widget's watchdog handles the
            timeout fallback.
            """
            try:
                worker.request(
                    asset_path,
                    frame_index,
                    generation=self.source_viewer_widget.gpu_generation,
                )
                return {'submitted': True}
            except Exception as e:
                logger.debug(f"Source GPU provider error: {e}")
                return {'submitted': False}

        self.source_viewer_widget.set_gpu_frame_provider(source_gpu_provider)
        logger.debug("Source Viewer GPU provider wired (async, submitted status)")

    def _disconnect_timeline_preview_renderer_signals(self, renderer: object) -> None:
        """Disconnect renderer signals before replacing the preview pipeline."""
        if renderer is None:
            return

        connections = [
            (self.edit_page.timeline_frame_requested, renderer.request_render),
            (renderer.frame_ready, self._on_timeline_preview_frame_ready),
            (renderer.render_error, self._on_timeline_preview_render_error),
            (self.program_viewer_widget.viewport_resized, self._on_program_viewer_viewport_resized),
        ]
        if hasattr(self.edit_page, 'timeline_scrub_started') and hasattr(renderer, 'begin_scrub'):
            connections.append((self.edit_page.timeline_scrub_started, renderer.begin_scrub))
        if hasattr(self.edit_page, 'timeline_scrub_frame_requested') and hasattr(renderer, 'request_scrub_preview'):
            connections.append((self.edit_page.timeline_scrub_frame_requested, renderer.request_scrub_preview))
        if hasattr(self.edit_page, 'timeline_scrub_finished') and hasattr(renderer, 'finish_scrub'):
            connections.append((self.edit_page.timeline_scrub_finished, renderer.finish_scrub))

        # Final-4: disconnect playback_mode_changed if it was connected.
        if (
            hasattr(renderer, 'playback_mode_changed')
            and hasattr(self.program_viewer_widget, 'set_realtime_mode')
        ):
            connections.append(
                (renderer.playback_mode_changed, self.program_viewer_widget.set_realtime_mode)
            )

        # Disconnect preview quality signal if it was connected
        if hasattr(self.program_viewer_widget, 'preview_quality_changed'):
            connections.append(
                (self.program_viewer_widget.preview_quality_changed, renderer.set_preview_quality)
            )

        for signal, slot in connections:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

        if hasattr(self, "timeline_panel") and self.timeline_panel is not None:
            current_renderer = getattr(self.timeline_panel, "_preview_renderer", None)
            if current_renderer is renderer and hasattr(self.timeline_panel, "set_preview_renderer"):
                self.timeline_panel.set_preview_renderer(None)

    def _request_current_timeline_preview_frame(self) -> None:
        """Render the frame currently under the playhead after timeline/model changes."""
        if self.timeline_preview_renderer is None:
            return

        frame = 0
        if hasattr(self, 'timeline_panel') and self.timeline_panel is not None:
            try:
                frame = self.timeline_panel.get_playhead_frame()
            except Exception:
                frame = 0
        elif hasattr(self, 'edit_page') and self.edit_page is not None:
            frame = int(getattr(self.edit_page, '_current_frame', 0) or 0)

        frame = max(0, int(frame))
        playback_trace("file_controller.request_current_preview", frame=frame)
        # P1-01: 剪辑命令/工程变更后的单帧刷新是合法旁路，显式标注通道，
        # 与"无主播放帧"（审计违规）在 trace 中可区分。
        self.timeline_preview_renderer.request_render(frame, request_kind="refresh")

    def _on_timeline_preview_frame_ready(self, frame: int, result: object) -> None:
        """
        Handle frame ready from TimelinePreviewRenderer.

        Args:
            frame: Frame number
            result: QImage or texture_id (MVP: QImage)
        """
        # Update ProgramViewerWidget
        result_type = type(result).__name__
        tex_id = width = height = None
        if isinstance(result, tuple) and len(result) == 3:
            tex_id, width, height = result
        scrub_active = bool(getattr(getattr(self, "edit_page", None), "_scrub_active", False))
        playback_trace(
            "file_controller.timeline_preview_frame_ready",
            frame=frame,
            result_type=result_type,
            tex_id=tex_id,
            width=width,
            height=height,
            scrub_active=scrub_active,
        )
        # Start external monitoring before ProgramViewer consumes the frame.
        # GPU results only reach DeckLink through ProgramViewer's PBO readback,
        # so readback must already be enabled when set_frame() triggers the GL copy.
        self._ensure_program_monitor_output()
        if self.program_viewer_widget:
            setattr(self.program_viewer_widget, "_timeline_scrub_active_for_trace", scrub_active)
            try:
                self.program_viewer_widget.set_frame(frame, result)
                offline_media = ()
                renderer = getattr(self, "timeline_preview_renderer", None)
                get_offline = getattr(renderer, "offline_media_for_frame", None)
                if callable(get_offline):
                    offline_media = get_offline(frame)
                set_offline = getattr(
                    self.program_viewer_widget, "set_offline_media", None
                )
                if callable(set_offline):
                    set_offline(offline_media)
            finally:
                setattr(self.program_viewer_widget, "_timeline_scrub_active_for_trace", False)
        self._submit_program_monitor_frame(frame, result)

        # Update timecode display
        if self._current_edit_project and hasattr(self._current_edit_project, 'fps'):
            fps = self._current_edit_project.fps
            timecode = self._frame_to_timecode(frame, fps)
            self.program_viewer_widget.set_timecode(timecode, fps)

        logger.debug(f"[TimelinePreviewRenderer] Frame {frame} ready")

    def _submit_program_monitor_frame(self, frame: int, result: object) -> None:
        """Submit Program Viewer frames to optional external monitoring output."""
        controller = self._ensure_program_monitor_output()
        if controller is None:
            self._decklink_monitor_debug_log(
                "timeline_submit_skipped",
                reason="no_controller",
                frame=frame,
                result_type=type(result).__name__,
                throttle=2.0,
            )
            return
        try:
            if isinstance(result, dict) and result.get("present_frame") is not None:
                present = result.get("present_frame")
                self._decklink_monitor_debug_log(
                    "timeline_present_waiting_for_readback",
                    frame=frame,
                    tex_id=getattr(present, "texture_id", 0),
                    input=f"{getattr(present, 'width', 0)}x{getattr(present, 'height', 0)}",
                    output=f"{controller.config.width}x{controller.config.height}",
                    fps=controller.config.fps,
                    mode=controller.config.mode,
                    backend=controller.config.backend,
                    throttle=1.0,
                )
                return
            width, height = self._decklink_result_size(result)
            submitted = controller.submit_frame(frame, result)
            self._decklink_monitor_debug_log(
                "timeline_submit",
                frame=frame,
                submitted=submitted,
                result_type=type(result).__name__,
                input=f"{width}x{height}" if width and height else "unknown",
                output=f"{controller.config.width}x{controller.config.height}",
                fps=controller.config.fps,
                mode=controller.config.mode,
                backend=controller.config.backend,
                throttle=1.0,
            )
        except Exception as exc:
            logger.debug("Program monitor output submit failed: %s", exc)

    def _submit_video_controller_monitor_frame(self, *, reason: str = "video") -> None:
        """Submit Color/Composite source video frames to external monitoring.

        Color and Composite viewports do not use ProgramViewer, so they do not
        emit monitor_frame_ready.  This path feeds the current decoded video
        frame to the same DeckLink controller with light throttling.
        """
        controller = self._ensure_program_monitor_output()
        video_controller = getattr(self, "video_controller", None)
        if controller is None:
            self._decklink_monitor_debug_log(
                "video_controller_submit_skipped",
                reason="no_controller",
                page=getattr(self, "current_page", None),
                throttle=2.0,
            )
            return
        if video_controller is None:
            self._decklink_monitor_debug_log(
                "video_controller_submit_skipped",
                reason="no_video_controller",
                page=getattr(self, "current_page", None),
                throttle=2.0,
            )
            return

        if (
            getattr(self, "current_page", None) == "Color"
            and not bool(getattr(video_controller, "is_playing", False))
        ):
            # A paused Color frame belongs to the ColorOutput path, not the
            # raw decoded-video path.  A thumbnail placeholder is already
            # letterboxed for the canvas; even a master frame here would be
            # ungraded.  Keep the previous monitor frame until ColorOutput is
            # ready, then submit that full-resolution graded texture.
            is_thumbnail = bool(
                getattr(video_controller, "_last_displayed_static_preview_is_thumbnail", False)
            )
            self._decklink_monitor_debug_log(
                "video_controller_submit_skipped",
                reason="color_thumbnail_placeholder" if is_thumbnail else "color_output_pending",
                page="Color",
                throttle=1.0,
            )
            return

        now = time.perf_counter()
        last_submit = float(getattr(self, "_decklink_video_monitor_last_submit", 0.0) or 0.0)
        min_interval = 1.0 / max(1.0, float(getattr(controller.config, "fps", 25.0) or 25.0))
        if now - last_submit < min_interval * 0.75:
            return

        displayed_frame = getattr(video_controller, "_last_displayed_frame_index", None)
        if displayed_frame is None:
            displayed_frame = getattr(video_controller, "current_frame", 0)
        frame_index = int(displayed_frame or 0)
        last_frame = getattr(self, "_decklink_video_monitor_last_frame", None)
        if last_frame == frame_index and reason == "playback":
            return

        payload = self._video_controller_monitor_payload(video_controller)
        if payload is None:
            self._decklink_monitor_debug_log(
                "video_controller_submit_skipped",
                reason="no_payload",
                frame=frame_index,
                page=getattr(self, "current_page", None),
                throttle=2.0,
            )
            return
        try:
            submit_payload = getattr(controller, "submit_video_frame", None)
            if callable(submit_payload):
                submitted = bool(submit_payload(frame_index, payload))
            else:
                image = self._monitor_payload_to_qimage(payload)
                submitted = bool(controller.submit_frame(frame_index, image)) if image is not None else False
            width, height = self._monitor_payload_size(payload)
            self._decklink_monitor_debug_log(
                "video_controller_submit",
                frame=frame_index,
                submitted=submitted,
                reason=reason,
                input=f"{width}x{height}" if width and height else "unknown",
                payload_type=type(payload).__name__,
                output=f"{controller.config.width}x{controller.config.height}",
                fps=controller.config.fps,
                mode=controller.config.mode,
                backend=controller.config.backend,
                page=getattr(self, "current_page", None),
                throttle=1.0,
            )
            if submitted:
                self._decklink_video_monitor_last_submit = now
                self._decklink_video_monitor_last_frame = frame_index
                playback_trace(
                    "decklink.video_controller_submit",
                    frame=frame_index,
                    reason=reason,
                    width=width,
                    height=height,
                    page=getattr(self, "current_page", None),
                )
        except Exception as exc:
            logger.debug("Video controller monitor output submit failed: %s", exc)

    def _schedule_video_controller_monitor_frame(self, *, reason: str = "video") -> None:
        """Coalesce source-frame monitor output away from the playback callback."""
        if bool(getattr(self, "_decklink_video_monitor_submit_pending", False)):
            return
        self._decklink_video_monitor_submit_pending = True

        def _submit_latest() -> None:
            self._decklink_video_monitor_submit_pending = False
            self._submit_video_controller_monitor_frame(reason=reason)

        QTimer.singleShot(1, _submit_latest)

    def _on_color_monitor_frame_ready(self, tex_id: int) -> None:
        """Submit Color page's final viewport render to external monitoring."""
        if getattr(self, "current_page", None) != "Color":
            return
        controller = self._ensure_program_monitor_output()
        if controller is None:
            return
        video_controller = getattr(self, "video_controller", None)
        if bool(getattr(video_controller, "is_playing", False)):
            if getattr(video_controller, "_last_submitted_video_reason", None) not in (None, "playback"):
                # A queued seek/static repaint may arrive after the user hits
                # Play.  Wait for the first actual playback frame instead of
                # flashing the old retained texture on the monitor.
                self._discard_color_monitor_pending_readback()
                return
            self._submit_color_monitor_pbo_readback(
                tex_id,
                controller,
                source="front_texture_playback",
                fit_mode="cover",
            )
            return
        if bool(getattr(video_controller, "_last_displayed_static_preview_is_thumbnail", False)):
            # Keep the last valid monitor frame until the original master frame
            # replaces the local thumbnail placeholder.
            return
        if not self._color_monitor_matches_current_video_frame(video_controller):
            # This frame_ready belongs to the departing clip.  Do not create a
            # ColorOutput request that would publish it after the new clip.
            return
        expected_frame = getattr(video_controller, "current_frame", None)
        expected_generation = getattr(video_controller, "_generation_id", None)
        target_key = (
            str(getattr(video_controller, "current_path", "") or ""),
            int(expected_generation or 0),
            int(expected_frame or 0),
        )
        if target_key != getattr(self, "_decklink_color_static_target", None):
            self._decklink_color_static_target = target_key
            self._decklink_color_output_last_request = 0.0
            self._discard_color_monitor_pending_readback()
        if self._request_color_monitor_output(controller, reason="static"):
            return

        now = time.perf_counter()
        last_submit = float(getattr(self, "_decklink_color_viewport_monitor_last_submit", 0.0) or 0.0)
        min_interval = 1.0 / max(1.0, float(getattr(controller.config, "fps", 25.0) or 25.0))
        if now - last_submit < min_interval * 0.75:
            return
        frame_array = self._grab_color_viewport_monitor_frame(controller)
        image = self._numpy_frame_to_monitor_qimage(frame_array)
        if image is None or image.isNull():
            self._decklink_monitor_debug_log(
                "color_viewport_submit_skipped",
                reason="no_qimage",
                tex_id=tex_id,
                throttle=2.0,
            )
            return
        frame_index = int(getattr(getattr(self, "video_controller", None), "current_frame", 0) or 0)
        try:
            submitted = controller.submit_frame(frame_index, image)
            self._decklink_monitor_debug_log(
                "color_viewport_submit",
                frame=frame_index,
                tex_id=tex_id,
                submitted=submitted,
                source="front_texture",
                input=f"{image.width()}x{image.height()}",
                output=f"{controller.config.width}x{controller.config.height}",
                fps=controller.config.fps,
                mode=controller.config.mode,
                backend=controller.config.backend,
                throttle=1.0,
            )
            if submitted:
                self._decklink_color_viewport_monitor_last_submit = now
        except Exception as exc:
            logger.debug("Color viewport monitor output submit failed: %s", exc)

    def _request_color_monitor_output(self, controller, *, reason: str) -> bool:
        """Request a stable full-frame ColorOutput for external monitoring."""
        engine = getattr(self, "engine", None)
        request = getattr(engine, "request_color_output", None)
        if engine is None or not callable(request):
            return False
        now = time.perf_counter()
        last_request = float(getattr(self, "_decklink_color_output_last_request", 0.0) or 0.0)
        min_interval = 1.0 / max(1.0, float(getattr(controller.config, "fps", 25.0) or 25.0))
        if now - last_request < min_interval * 0.75:
            return True
        self._decklink_color_output_last_request = now
        try:
            request(
                max_proxy_dim=max(int(controller.config.width), int(controller.config.height)),
                update_masks=False,
                target="monitor",
            )
            self._decklink_monitor_debug_log(
                "color_output_request",
                reason=reason,
                output=f"{controller.config.width}x{controller.config.height}",
                throttle=1.0,
            )
            return True
        except TypeError:
            try:
                request()
                return True
            except Exception as exc:
                logger.debug("Color monitor ColorOutput request failed: %s", exc)
        except Exception as exc:
            logger.debug("Color monitor ColorOutput request failed: %s", exc)
        return False

    def _on_color_output_monitor_ready(self, texture_id: int, width: int, height: int) -> None:
        """Submit stable ColorOutput texture to external monitoring."""
        if getattr(self, "current_page", None) != "Color":
            return
        video_controller = getattr(self, "video_controller", None)
        if (
            not bool(getattr(video_controller, "is_playing", False))
            and bool(getattr(video_controller, "_last_displayed_static_preview_is_thumbnail", False))
        ):
            return
        if (
            not bool(getattr(video_controller, "is_playing", False))
            and not self._color_monitor_matches_current_video_frame(video_controller)
        ):
            return
        controller = self._ensure_program_monitor_output()
        if controller is None:
            return
        if not bool(getattr(video_controller, "is_playing", False)):
            # A paused clip switch has only one frame to present.  PBO is a
            # one-frame-delayed readback, so it can publish the previous clip
            # indefinitely.  Read the just-rendered ColorOutput directly.
            self._discard_color_monitor_pending_readback()
            self._submit_color_monitor_direct_readback(
                texture_id,
                controller,
                int(width),
                int(height),
                source="monitor_color_output_static",
                fit_mode="cover",
            )
            return
        if getattr(video_controller, "_last_submitted_video_reason", None) not in (None, "playback"):
            self._discard_color_monitor_pending_readback()
            return
        self._submit_color_monitor_pbo_readback(
            texture_id,
            controller,
            width=int(width),
            height=int(height),
            source="monitor_color_output",
        )

    def _discard_color_monitor_pending_readback(self) -> None:
        """Forget a one-frame-delayed PBO result from a prior clip/state."""
        self._decklink_color_pbo_pending = None

    def _color_monitor_matches_current_video_frame(self, video_controller) -> bool:
        """Return whether renderer state belongs to the active video request."""
        renderer = getattr(getattr(self, "engine", None), "renderer", None)
        expected_frame = getattr(video_controller, "current_frame", None)
        expected_generation = getattr(video_controller, "_generation_id", None)
        rendered_frame = getattr(renderer, "current_video_frame_index", None)
        rendered_generation = getattr(renderer, "_video_generation_id", None)
        if (
            expected_frame is not None
            and rendered_frame is not None
            and int(rendered_frame) != int(expected_frame)
        ):
            return False
        if (
            expected_generation is not None
            and rendered_generation is not None
            and int(rendered_generation) != int(expected_generation)
        ):
            return False
        return True

    def _submit_color_monitor_pbo_readback(
        self,
        tex_id: int,
        controller,
        *,
        width: int | None = None,
        height: int | None = None,
        source: str = "front_texture",
        fit_mode: str = "contain",
    ) -> bool:
        """Submit Color renderer's front texture through async PBO readback."""
        engine = getattr(self, "engine", None)
        canvas = getattr(self, "canvas", None)
        gl_viewport = getattr(canvas, "gl_viewport", None)
        renderer = getattr(engine, "renderer", None)
        if engine is None or gl_viewport is None or renderer is None:
            return False
        if not tex_id:
            return False

        if width is None or height is None:
            size = getattr(renderer, "current_front_size", None) or getattr(engine, "viewport_size", None)
            if not size or len(size) != 2:
                return False
            width, height = int(size[0] or 0), int(size[1] or 0)
        width, height = int(width or 0), int(height or 0)
        if width <= 0 or height <= 0:
            return False

        now = time.perf_counter()
        last_issue = float(getattr(self, "_decklink_color_pbo_last_issue", 0.0) or 0.0)
        min_interval = 1.0 / max(1.0, float(getattr(controller.config, "fps", 25.0) or 25.0))
        should_issue = now - last_issue >= min_interval * 0.75

        gl_state = None
        # AUD-2802: 跟踪 makeCurrent 所有权，收尾补 doneCurrent（嵌套的
        # 直接回读复用外层上下文，不重复释放）。
        made_current = False
        try:
            made_current = self._make_gl_viewport_current(gl_viewport)
            from OpenGL import GL as gl

            gl_state = self._capture_gl_readback_state(gl)
            if not self._ensure_color_monitor_pbos(width, height):
                return self._submit_color_monitor_direct_readback(
                    tex_id,
                    controller,
                    width,
                    height,
                    source=f"{source}_pbo_init_fallback",
                    fit_mode=fit_mode,
                )
            drained = self._drain_color_monitor_pbo(controller)
            if not should_issue:
                if not drained:
                    self._submit_color_monitor_direct_readback(
                        tex_id,
                        controller,
                        width,
                        height,
                        source=f"{source}_throttle_fallback",
                        fit_mode=fit_mode,
                    )
                return True

            import ctypes

            pbos = getattr(self, "_decklink_color_pbos", None) or []
            fbo = int(getattr(self, "_decklink_color_read_fbo", 0) or 0)
            read_index = int(getattr(self, "_decklink_color_pbo_index", 0) or 0) % len(pbos)
            pbo = int(pbos[read_index])

            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, fbo)
            gl.glFramebufferTexture2D(
                gl.GL_READ_FRAMEBUFFER,
                gl.GL_COLOR_ATTACHMENT0,
                gl.GL_TEXTURE_2D,
                int(tex_id),
                0,
            )
            try:
                gl.glReadBuffer(gl.GL_COLOR_ATTACHMENT0)
            except Exception:
                pass
            status = gl.glCheckFramebufferStatus(gl.GL_READ_FRAMEBUFFER)
            if status != gl.GL_FRAMEBUFFER_COMPLETE:
                self._decklink_monitor_warning_log(
                    "color_pbo_framebuffer_incomplete",
                    status=status,
                    tex_id=tex_id,
                    input=f"{width}x{height}",
                    throttle=3.0,
                )
                self._decklink_monitor_debug_log(
                    "color_pbo_submit_skipped",
                    reason="framebuffer_incomplete",
                    status=status,
                    tex_id=tex_id,
                    throttle=2.0,
                )
                fallback = self._submit_color_monitor_direct_readback(
                    tex_id,
                    controller,
                    width,
                    height,
                    source=f"{source}_fbo_fallback",
                    fit_mode=fit_mode,
                )
                return drained or fallback

            gl.glBindBuffer(gl.GL_PIXEL_PACK_BUFFER, pbo)
            gl.glReadPixels(
                0,
                0,
                width,
                height,
                gl.GL_RGBA,
                gl.GL_UNSIGNED_BYTE,
                ctypes.c_void_p(0),
            )
            frame_index = self._color_monitor_frame_index()
            self._decklink_color_pbo_pending = (pbo, frame_index, width, height, fit_mode)
            self._decklink_color_pbo_index = (read_index + 1) % len(pbos)
            self._decklink_color_pbo_last_issue = now
            self._decklink_color_pbo_source = source
            if not drained:
                self._submit_color_monitor_direct_readback(
                    tex_id,
                    controller,
                    width,
                    height,
                    source=f"{source}_first_frame_fallback",
                    fit_mode=fit_mode,
                )
            return True
        except Exception as exc:
            self._decklink_monitor_warning_log(
                "color_pbo_submit_failed",
                error=exc,
                tex_id=tex_id,
                input=f"{width}x{height}",
                throttle=3.0,
            )
            self._decklink_monitor_debug_log(
                "color_pbo_submit_failed",
                error=exc,
                tex_id=tex_id,
                input=f"{width}x{height}",
                throttle=2.0,
            )
            logger.debug("Color monitor PBO readback failed: %s", exc)
            return False
        finally:
            try:
                from OpenGL import GL as gl

                self._restore_gl_readback_state(gl, gl_state)
            except Exception:
                pass
            self._release_gl_viewport_current(
                gl_viewport, made_current, source="color_pbo_readback")

    def _ensure_color_monitor_pbos(self, width: int, height: int) -> bool:
        size = (int(width), int(height))
        if getattr(self, "_decklink_color_pbo_size", None) == size and getattr(
            self, "_decklink_color_pbos", None
        ):
            return True

        try:
            from OpenGL import GL as gl

            old_pbos = getattr(self, "_decklink_color_pbos", None) or []
            if old_pbos:
                try:
                    gl.glDeleteBuffers(len(old_pbos), old_pbos)
                except Exception:
                    pass
            old_fbo = int(getattr(self, "_decklink_color_read_fbo", 0) or 0)
            if old_fbo:
                try:
                    gl.glDeleteFramebuffers(1, [old_fbo])
                except Exception:
                    pass

            pbos = self._gl_ids(gl.glGenBuffers(2))
            if len(pbos) < 2:
                return False

            byte_count = int(width) * int(height) * 4
            for pbo in pbos:
                gl.glBindBuffer(gl.GL_PIXEL_PACK_BUFFER, pbo)
                gl.glBufferData(gl.GL_PIXEL_PACK_BUFFER, byte_count, None, gl.GL_STREAM_READ)
            gl.glBindBuffer(gl.GL_PIXEL_PACK_BUFFER, 0)

            fbo = self._gl_id(gl.glGenFramebuffers(1))

            self._decklink_color_pbos = pbos
            self._decklink_color_pbo_size = size
            self._decklink_color_pbo_index = 0
            self._decklink_color_pbo_pending = None
            self._decklink_color_read_fbo = int(fbo)
            return True
        except Exception as exc:
            self._decklink_monitor_debug_log(
                "color_pbo_init_failed",
                error=exc,
                input=f"{width}x{height}",
                throttle=2.0,
            )
            logger.debug("Color monitor PBO allocation failed: %s", exc)
            self._decklink_color_pbos = []
            self._decklink_color_pbo_size = None
            self._decklink_color_pbo_pending = None
            return False

    def _drain_color_monitor_pbo(self, controller) -> bool:
        pending = getattr(self, "_decklink_color_pbo_pending", None)
        if not pending:
            return False
        try:
            from OpenGL import GL as gl
            from src.shared.video.monitor_output import MonitorRGBAFrame

            if len(pending) >= 5:
                pbo, frame_index, width, height, fit_mode = pending[:5]
            else:
                pbo, frame_index, width, height = pending
                fit_mode = "contain"
            byte_count = int(width) * int(height) * 4
            gl.glBindBuffer(gl.GL_PIXEL_PACK_BUFFER, int(pbo))
            raw = gl.glGetBufferSubData(gl.GL_PIXEL_PACK_BUFFER, 0, byte_count)
            if raw is None:
                return False
            payload = MonitorRGBAFrame(
                data=bytes(raw),
                width=int(width),
                height=int(height),
                is_gl_oriented=True,
                fit_mode=str(fit_mode or "contain"),
            )
            submit_payload = getattr(controller, "submit_video_frame", None)
            submitted = bool(submit_payload(int(frame_index), payload)) if callable(submit_payload) else False
            if submitted:
                self._decklink_color_viewport_monitor_last_submit = time.perf_counter()
                self._decklink_video_monitor_last_frame = int(frame_index)
            self._decklink_monitor_debug_log(
                "color_pbo_submit",
                frame=frame_index,
                submitted=submitted,
                source=getattr(self, "_decklink_color_pbo_source", "unknown"),
                input=f"{width}x{height}",
                output=f"{controller.config.width}x{controller.config.height}",
                fps=controller.config.fps,
                mode=controller.config.mode,
                throttle=1.0,
            )
            return submitted
        except Exception as exc:
            self._decklink_monitor_debug_log(
                "color_pbo_drain_failed",
                error=exc,
                throttle=2.0,
            )
            logger.debug("Color monitor PBO drain failed: %s", exc)
            return False
        finally:
            self._decklink_color_pbo_pending = None

    def _decklink_gpu_scaled_readback_enabled(self) -> bool:
        """N16：GPU 缩放回读开关（默认关——真实监看 A/B 验证后启用）。"""
        try:
            return bool(int(self.config_manager.get(
                "decklink_gpu_scaled_readback", 0) or 0))
        except Exception:  # noqa: BLE001 — 配置不可用按关
            return False

    def _gpu_scaled_readback(self, gl, tex_id: int,
                             src_w: int, src_h: int,
                             dst_w: int, dst_h: int):
        """N16：GPU 缩放回读（占位守卫——真实像素路径属 N18 监看域）。

        源为已绑定纹理（非 FBO）：正确缩放需采样渲染（shader）或双
        FBO blit，属真实监看 A/B 对拍域（设备验证后启用）；在此之前
        本方法恒返回 None（调用方回退既有整帧回读——开关即便误开也
        零行为变化，不冒称已省回读）。
        """
        return None

    def _submit_color_monitor_direct_readback(
        self,
        tex_id: int,
        controller,
        width: int,
        height: int,
        *,
        source: str,
        fit_mode: str = "contain",
    ) -> bool:
        """Synchronous ColorOutput readback fallback for first-frame monitor output."""
        now = time.perf_counter()
        min_interval = 1.0 / max(1.0, float(getattr(controller.config, "fps", 25.0) or 25.0))
        last_emit = float(getattr(self, "_decklink_color_direct_readback_last_emit", 0.0) or 0.0)
        if now - last_emit < min_interval * 0.75:
            return False

        engine = getattr(self, "engine", None)
        canvas = getattr(self, "canvas", None)
        gl_viewport = getattr(canvas, "gl_viewport", None)
        if engine is None or gl_viewport is None or not tex_id:
            return False

        gl_state = None
        # AUD-2802: 所有权跟踪——本方法既会被顶层信号槽直接调用（需要
        # makeCurrent/doneCurrent），也会被 PBO 回读在上下文已 current 时
        # 嵌套调用（此时不得释放外层的上下文）。
        made_current = False
        try:
            from OpenGL import GL as gl
            from src.shared.video.monitor_output import MonitorRGBAFrame

            made_current = self._make_gl_viewport_current(gl_viewport)
            gl_state = self._capture_gl_readback_state(gl)
            fbo = int(getattr(self, "_decklink_color_read_fbo", 0) or 0)
            if not fbo:
                fbo = self._gl_id(gl.glGenFramebuffers(1))
                self._decklink_color_read_fbo = fbo

            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, fbo)
            gl.glFramebufferTexture2D(
                gl.GL_READ_FRAMEBUFFER,
                gl.GL_COLOR_ATTACHMENT0,
                gl.GL_TEXTURE_2D,
                int(tex_id),
                0,
            )
            try:
                gl.glReadBuffer(gl.GL_COLOR_ATTACHMENT0)
            except Exception:
                pass
            status = gl.glCheckFramebufferStatus(gl.GL_READ_FRAMEBUFFER)
            if status != gl.GL_FRAMEBUFFER_COMPLETE:
                self._decklink_monitor_warning_log(
                    "color_direct_framebuffer_incomplete",
                    status=status,
                    tex_id=tex_id,
                    input=f"{width}x{height}",
                    throttle=3.0,
                )
                return False

            gl.glBindBuffer(gl.GL_PIXEL_PACK_BUFFER, 0)
            try:
                gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 1)
            except Exception:
                pass
            # N16：GPU blit 缩放回读（默认关，config
            # decklink_gpu_scaled_readback=1 显式开启）——监看目标小于
            # 源帧时经 glBlitFramebuffer 线性缩放后**只回读目标尺寸**
            # （替代整帧 glReadPixels 全量回读；像素正确性需真实监看
            # A/B 对拍（N18/设备域），默认关闭零行为变化）。
            scaled = None
            if self._decklink_gpu_scaled_readback_enabled():
                dst_w = int(getattr(controller.config, "width", 0) or 0)
                dst_h = int(getattr(controller.config, "height", 0) or 0)
                if 0 < dst_w < int(width) and 0 < dst_h < int(height):
                    scaled = self._gpu_scaled_readback(
                        gl, tex_id, int(width), int(height), dst_w, dst_h)
            if scaled is not None:
                raw_bytes, rd_w, rd_h = scaled
            else:
                raw = gl.glReadPixels(
                    0,
                    0,
                    int(width),
                    int(height),
                    gl.GL_RGBA,
                    gl.GL_UNSIGNED_BYTE,
                    outputType=bytes,
                )
                if raw is None:
                    self._decklink_monitor_warning_log(
                        "color_direct_readback_empty",
                        tex_id=tex_id,
                        input=f"{width}x{height}",
                        throttle=3.0,
                    )
                    return False
                raw_bytes = bytes(raw)
                rd_w, rd_h = int(width), int(height)
            byte_count = int(rd_w) * int(rd_h) * 4
            if len(raw_bytes) < byte_count:
                self._decklink_monitor_warning_log(
                    "color_direct_readback_short",
                    tex_id=tex_id,
                    bytes=len(raw_bytes),
                    expected=byte_count,
                    input=f"{rd_w}x{rd_h}",
                    throttle=3.0,
                )
                return False

            frame_index = self._color_monitor_frame_index()
            submit_payload = getattr(controller, "submit_video_frame", None)
            if not callable(submit_payload):
                return False
            payload = MonitorRGBAFrame(
                data=raw_bytes[:byte_count],
                width=int(rd_w),
                height=int(rd_h),
                is_gl_oriented=True,
                fit_mode=str(fit_mode or "contain"),
            )
            submitted = bool(submit_payload(int(frame_index), payload))
            self._decklink_monitor_debug_log(
                "color_direct_submit",
                frame=frame_index,
                submitted=submitted,
                source=source,
                input=f"{width}x{height}",
                output=f"{controller.config.width}x{controller.config.height}",
                fps=controller.config.fps,
                mode=controller.config.mode,
                throttle=1.0,
            )
            if submitted:
                self._decklink_color_direct_readback_last_emit = time.perf_counter()
                self._decklink_color_viewport_monitor_last_submit = self._decklink_color_direct_readback_last_emit
                self._decklink_video_monitor_last_frame = int(frame_index)
            return submitted
        except Exception as exc:
            self._decklink_monitor_warning_log(
                "color_direct_readback_failed",
                error=exc,
                tex_id=tex_id,
                input=f"{width}x{height}",
                throttle=3.0,
            )
            logger.debug("Color monitor direct readback failed: %s", exc)
            return False
        finally:
            try:
                from OpenGL import GL as gl

                self._restore_gl_readback_state(gl, gl_state)
            except Exception:
                pass
            self._release_gl_viewport_current(
                gl_viewport, made_current, source="color_direct_readback")

    def _capture_gl_readback_state(self, gl) -> tuple[int, int, int] | None:
        try:
            return (
                int(gl.glGetIntegerv(gl.GL_READ_FRAMEBUFFER_BINDING)),
                int(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING)),
                int(gl.glGetIntegerv(gl.GL_PIXEL_PACK_BUFFER_BINDING)),
            )
        except Exception:
            return None

    def _make_gl_viewport_current(self, gl_viewport) -> bool:
        """Make the Color viewport's GL context current, reporting ownership.

        AUD-2802: DeckLink 监视输出回读路径此前 makeCurrent() 后从不
        doneCurrent()，破坏 Qt 的 context-current 簿记。本 helper 记录
        所有权：只有真正取得上下文的调用方负责 doneCurrent；嵌套调用
        （PBO 回读内部再进直接回读）看到上下文已 current 时直接复用，
        不会把外层仍在使用的上下文释放掉。

        Returns:
            True 表示本次调用取得了上下文（调用方用完必须 doneCurrent）；
            False 表示上下文已由外层持有（嵌套调用，不得释放）。
        """
        try:
            from PyQt6.QtGui import QOpenGLContext

            widget_ctx = gl_viewport.context()
            if widget_ctx is not None and QOpenGLContext.currentContext() is widget_ctx:
                return False
        except Exception:
            logger.debug("GL context ownership probe failed", exc_info=True)
        gl_viewport.makeCurrent()
        return True

    def _release_gl_viewport_current(self, gl_viewport, made_current: bool, *, source: str) -> None:
        """Release a context acquired via ``_make_gl_viewport_current``."""
        if not made_current:
            return
        try:
            gl_viewport.doneCurrent()
        except Exception:
            logger.debug("GL readback doneCurrent failed (%s)", source, exc_info=True)

    def _restore_gl_readback_state(self, gl, state: tuple[int, int, int] | None) -> None:
        if state is None:
            gl.glBindBuffer(gl.GL_PIXEL_PACK_BUFFER, 0)
            return
        read_fbo, draw_fbo, pixel_pack = state
        gl.glBindBuffer(gl.GL_PIXEL_PACK_BUFFER, int(pixel_pack))
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, int(read_fbo))
        gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, int(draw_fbo))

    def _gl_id(self, value: object) -> int:
        """Normalize PyOpenGL id return values across scalar/list/ctypes variants."""
        if isinstance(value, (list, tuple)):
            return int(value[0])
        try:
            return int(value)
        except TypeError:
            return int(value[0])  # type: ignore[index]

    def _gl_ids(self, value: object) -> list[int]:
        if isinstance(value, int):
            return [int(value)]
        try:
            return [int(item) for item in value]  # type: ignore[union-attr]
        except TypeError:
            return [int(value)]

    def _color_monitor_frame_index(self) -> int:
        video_controller = getattr(self, "video_controller", None)
        displayed_frame = getattr(video_controller, "_last_displayed_frame_index", None)
        if displayed_frame is None:
            displayed_frame = getattr(video_controller, "current_frame", 0)
        return int(displayed_frame or 0)

    def _grab_color_viewport_monitor_frame(self, controller) -> np.ndarray | None:
        """Read back the currently displayed Color viewport texture."""
        engine = getattr(self, "engine", None)
        canvas = getattr(self, "canvas", None)
        gl_viewport = getattr(canvas, "gl_viewport", None)
        renderer = getattr(engine, "renderer", None)
        if engine is None or gl_viewport is None or renderer is None:
            return None
        if getattr(renderer, "current_front_tex_id", None) is None:
            return None

        made_current = False
        try:
            made_current = self._make_gl_viewport_current(gl_viewport)
        except Exception as exc:
            self._decklink_monitor_debug_log(
                "color_viewport_submit_skipped",
                reason="make_current_failed",
                error=exc,
                throttle=2.0,
            )
            return None

        # AUD-2802: 此前 makeCurrent 后没有任何 doneCurrent（连 finally
        # 都没有）；此处补齐，且只在本次调用真正取得上下文时释放。
        try:
            max_dim = max(int(controller.config.width), int(controller.config.height))
            grab = getattr(engine, "render_scope_view", None)
            frame = grab(max_dim=max_dim) if callable(grab) else None
            if frame is None:
                return None
            target_size = (int(controller.config.width), int(controller.config.height))
            height, width = frame.shape[:2]
            if (width, height) != target_size:
                interpolation = cv2.INTER_AREA if max(width, height) > max(target_size) else cv2.INTER_LINEAR
                frame = cv2.resize(frame, target_size, interpolation=interpolation)
            return frame
        except Exception as exc:
            self._decklink_monitor_debug_log(
                "color_viewport_submit_skipped",
                reason="front_texture_readback_failed",
                error=exc,
                throttle=2.0,
            )
            return None
        finally:
            self._release_gl_viewport_current(
                gl_viewport, made_current, source="color_viewport_grab")

    def _video_controller_monitor_payload(self, video_controller) -> object | None:
        """Return the latest source frame without converting it on the UI thread."""
        frame_array = getattr(video_controller, "_last_displayed_frame", None)
        decoded = getattr(video_controller, "_last_displayed_decoded_frame", None)
        is_playing = bool(getattr(video_controller, "is_playing", False))

        if decoded is not None and (is_playing or frame_array is None):
            return decoded

        if frame_array is None:
            frame_getter = getattr(video_controller, "_coordinator", None)
            frame_getter = getattr(frame_getter, "get_current_frame_data", None)
            if callable(frame_getter):
                try:
                    decoded = frame_getter(is_master=False)
                except TypeError:
                    decoded = frame_getter(False)
                except Exception:
                    decoded = None
                if decoded is not None:
                    return decoded

        if frame_array is None or not isinstance(frame_array, np.ndarray):
            return None
        if frame_array.ndim < 2:
            return None
        return frame_array

    def _monitor_payload_to_qimage(self, payload: object) -> QImage | None:
        if payload is None:
            return None
        if isinstance(payload, QImage):
            return payload
        if not isinstance(payload, np.ndarray):
            try:
                from src.shared.video.monitor_output import decoded_frame_to_bgr_array

                payload = decoded_frame_to_bgr_array(payload)
            except Exception:
                payload = None
        if payload is None or not isinstance(payload, np.ndarray):
            return None
        try:
            return self._numpy_frame_to_monitor_qimage(payload)
        except Exception as exc:
            logger.debug("Video controller monitor QImage conversion failed: %s", exc)
            return None

    @staticmethod
    def _monitor_payload_size(payload: object) -> tuple[int | None, int | None]:
        try:
            from src.shared.video.monitor_output import monitor_payload_size

            return monitor_payload_size(payload)
        except Exception:
            if isinstance(payload, QImage):
                return int(payload.width()), int(payload.height())
            if isinstance(payload, np.ndarray) and payload.ndim >= 2:
                return int(payload.shape[1]), int(payload.shape[0])
            return None, None

    def _numpy_frame_to_monitor_qimage(self, frame_array: object) -> QImage | None:
        if frame_array is None or not isinstance(frame_array, np.ndarray):
            return None
        if frame_array.ndim < 2:
            return None
        array = np.asarray(frame_array)
        if array.dtype != np.uint8:
            if np.issubdtype(array.dtype, np.floating):
                array = np.clip(array * 255.0, 0, 255).astype(np.uint8)
            else:
                array = np.clip(array, 0, 255).astype(np.uint8)
        if array.ndim == 2:
            array = cv2.cvtColor(array, cv2.COLOR_GRAY2RGB)
        elif array.shape[2] >= 4:
            array = cv2.cvtColor(array[:, :, :4], cv2.COLOR_BGRA2RGB)
        else:
            array = cv2.cvtColor(array[:, :, :3], cv2.COLOR_BGR2RGB)
        array = np.ascontiguousarray(array)
        height, width = array.shape[:2]
        bytes_per_line = int(array.strides[0])
        return QImage(
            array.data,
            width,
            height,
            bytes_per_line,
            QImage.Format.Format_RGB888,
        ).copy()

    def _on_program_monitor_frame_ready(self, frame: int, image) -> None:
        """Submit GPU PBO readback frames to external monitoring output."""
        controller = getattr(self, "_program_monitor_output", None)
        if controller is None:
            self._decklink_monitor_debug_log(
                "pbo_submit_skipped",
                reason="no_controller",
                frame=frame,
                throttle=2.0,
            )
            return
        try:
            submitted = controller.submit_frame(int(frame), image)
            width, height = self._decklink_result_size(image)
            self._decklink_monitor_debug_log(
                "pbo_submit",
                frame=frame,
                submitted=submitted,
                input=f"{width}x{height}" if width and height else "unknown",
                output=f"{controller.config.width}x{controller.config.height}",
                fps=controller.config.fps,
                mode=controller.config.mode,
                backend=controller.config.backend,
                throttle=1.0,
            )
        except Exception as exc:
            logger.debug("Program monitor output GPU readback submit failed: %s", exc)

    def _on_program_monitor_gpu_frame_ready(
        self,
        frame: int,
        texture_id: int,
        width: int,
        height: int,
        is_gl_oriented: bool,
    ) -> None:
        """Submit viewer-owned GPU texture metadata to native monitor output."""
        controller = getattr(self, "_program_monitor_output", None)
        if controller is None:
            self._decklink_monitor_debug_log(
                "gpu_submit_skipped",
                reason="no_controller",
                frame=frame,
                throttle=2.0,
            )
            return
        try:
            from src.shared.video.monitor_output import NativeGpuMonitorFrame

            submitted = controller.submit_gpu_frame(
                NativeGpuMonitorFrame(
                    frame_index=int(frame),
                    texture_id=int(texture_id),
                    width=int(width),
                    height=int(height),
                    is_gl_oriented=bool(is_gl_oriented),
                )
            )
            self._decklink_monitor_debug_log(
                "gpu_submit",
                frame=frame,
                submitted=submitted,
                texture_id=texture_id,
                input=f"{width}x{height}",
                output=f"{controller.config.width}x{controller.config.height}",
                fps=controller.config.fps,
                mode=controller.config.mode,
                backend=controller.config.backend,
                throttle=1.0,
            )
        except Exception as exc:
            logger.debug("Program monitor output native GPU submit failed: %s", exc)

    def _decklink_result_size(self, result: object) -> tuple[int | None, int | None]:
        if isinstance(result, QImage):
            return int(result.width()), int(result.height())
        if isinstance(result, tuple) and len(result) >= 3:
            try:
                return int(result[1]), int(result[2])
            except Exception:
                return None, None
        return None, None

    def _decklink_monitor_debug_log(
        self,
        event: str,
        *,
        throttle: float = 1.0,
        **fields,
    ) -> None:
        controller = getattr(self, "_program_monitor_output", None)
        config = getattr(controller, "config", None)
        if not bool(getattr(config, "debug", False)):
            return
        now = time.perf_counter()
        last_by_event = getattr(self, "_decklink_monitor_debug_last", None)
        if not isinstance(last_by_event, dict):
            last_by_event = {}
            self._decklink_monitor_debug_last = last_by_event
        last = float(last_by_event.get(event, 0.0) or 0.0)
        if now - last < throttle:
            return
        last_by_event[event] = now
        detail = " ".join(f"{key}={value}" for key, value in fields.items())
        logger.info("[DECKLINK_MONITOR_DEBUG] event=%s %s", event, detail)

    def _decklink_monitor_warning_log(
        self,
        event: str,
        *,
        throttle: float = 3.0,
        **fields,
    ) -> None:
        now = time.perf_counter()
        last_by_event = getattr(self, "_decklink_monitor_warning_last", None)
        if not isinstance(last_by_event, dict):
            last_by_event = {}
            self._decklink_monitor_warning_last = last_by_event
        last = float(last_by_event.get(event, 0.0) or 0.0)
        if now - last < throttle:
            return
        last_by_event[event] = now
        detail = " ".join(f"{key}={value}" for key, value in fields.items())
        logger.warning("[DECKLINK_MONITOR] event=%s %s", event, detail)

    def _ensure_program_monitor_output(self):
        """Create/start the optional DeckLink monitor output when configured."""
        try:
            from src.shared.video.monitor_output import (
                DeckLinkMonitorConfig,
                ProgramMonitorOutputController,
            )
        except Exception as exc:
            logger.debug("Program monitor output unavailable: %s", exc)
            return None

        config = DeckLinkMonitorConfig.from_config(getattr(self, "config_manager", None))
        existing = getattr(self, "_program_monitor_output", None)
        existing_config = getattr(existing, "config", None)

        # Image Grading mode has no video output path; force-disable the
        # DeckLink monitor in memory only (config value is preserved on disk
        # so switching back to video/all restores the user's setting).
        if config.enabled and self._get_work_mode() == "image":
            config = type(config)(**{**config.__dict__, "enabled": False})

        if not config.enabled:
            if existing is not None:
                try:
                    existing.stop()
                except Exception:
                    pass
                self._program_monitor_output = None
            self._set_program_monitor_output_modes(False, False)
            self._program_monitor_output_failed_config = None
            return None

        if getattr(self, "_program_monitor_output_failed_config", None) == config:
            self._set_program_monitor_output_modes(False, False)
            return None

        if existing is not None and existing_config != config:
            try:
                existing.stop()
            except Exception:
                pass
            existing = None
            self._program_monitor_output = None
            self._set_program_monitor_output_modes(False, False)
            self._program_monitor_output_failed_config = None

        if existing is None:
            existing = ProgramMonitorOutputController(config)
            self._program_monitor_output = existing
            if not existing.start():
                self._program_monitor_output_failed_config = config
                self._set_program_monitor_output_modes(False, False)
                return None
        elif not getattr(existing.backend, "is_running", False):
            if not existing.start():
                self._program_monitor_output_failed_config = config
                self._set_program_monitor_output_modes(False, False)
                return None
        self._program_monitor_output_failed_config = None
        native_backend = str(getattr(config, "backend", "ffmpeg")).lower() in {
            "native",
            "native_sdk",
            "sdk",
        }
        native_gpu_enabled = bool(
            native_backend
            and getattr(getattr(existing, "backend", None), "supports_gpu_texture", False)
        )
        self._set_program_monitor_output_modes(
            readback_enabled=not native_gpu_enabled,
            gpu_signal_enabled=native_gpu_enabled,
        )
        return existing

    def _set_program_monitor_output_modes(
        self,
        readback_enabled: bool,
        gpu_signal_enabled: bool,
    ) -> None:
        widget = getattr(self, "program_viewer_widget", None)
        readback_setter = getattr(widget, "set_monitor_readback_enabled", None)
        if callable(readback_setter):
            try:
                readback_setter(bool(readback_enabled))
            except Exception:
                pass
        gpu_setter = getattr(widget, "set_monitor_gpu_signal_enabled", None)
        if callable(gpu_setter):
            try:
                gpu_setter(bool(gpu_signal_enabled))
            except Exception:
                pass

    def _on_timeline_preview_render_error(self, frame: int, error_msg: str) -> None:
        """
        Handle render error from TimelinePreviewRenderer.

        Args:
            frame: Frame number where error occurred
            error_msg: Error message
        """
        logger.warning(f"[TimelinePreviewRenderer] Error at frame {frame}: {error_msg}")

        # Show an error overlay through the same ProgramViewer endpoint.
        # ProgramViewer preserves the last good frame for transient failures.
        if self.program_viewer_widget:
            offline_media = ()
            renderer = getattr(self, "timeline_preview_renderer", None)
            get_offline = getattr(renderer, "offline_media_for_frame", None)
            if callable(get_offline):
                offline_media = get_offline(frame)
            set_offline = getattr(
                self.program_viewer_widget, "set_offline_media", None
            )
            if offline_media and callable(set_offline):
                set_offline(offline_media)
            else:
                self.program_viewer_widget.set_error_state("render_error", error_msg)

    def _on_program_viewer_viewport_resized(self, width: int, height: int) -> None:
        """
        Handle viewport resize from ProgramViewerWidget (P8-02).

        Updates the renderer's viewport size to match the viewer.

        Args:
            width: New viewport width
            height: New viewport height
        """
        if self.timeline_preview_renderer and width > 0 and height > 0:
            viewport_size = self._monitor_render_viewport_size((width, height))
            self.timeline_preview_renderer.set_viewport_size(viewport_size)
            logger.debug(
                "[TimelinePreviewRenderer] Viewport size updated: ui=%sx%s render=%sx%s",
                width,
                height,
                viewport_size[0],
                viewport_size[1],
            )

    def _monitor_render_viewport_size(self, size: tuple[int, int]) -> tuple[int, int]:
        """Return a monitor-quality render size while preserving viewport aspect."""
        width = max(1, int(size[0]))
        height = max(1, int(size[1]))
        try:
            from src.shared.video.monitor_output import DeckLinkMonitorConfig

            config = DeckLinkMonitorConfig.from_config(getattr(self, "config_manager", None))
        except Exception:
            return width, height
        if not getattr(config, "enabled", False):
            return width, height
        target_w = max(1, int(getattr(config, "width", 0) or 0))
        target_h = max(1, int(getattr(config, "height", 0) or 0))
        scale = max(1.0, target_w / float(width), target_h / float(height))
        return max(1, int(round(width * scale))), max(1, int(round(height * scale)))

    def _frame_to_timecode(self, frame: int, fps: float) -> str:
        """
        Convert frame number to timecode string (HH:MM:SS:FF).

        Args:
            frame: Frame number
            fps: Frame rate

        Returns:
            Timecode string
        """
        total_seconds = frame / fps
        hours = int(total_seconds // 3600)
        minutes = int((total_seconds % 3600) // 60)
        seconds = int(total_seconds % 60)
        frames = int(frame % fps)

        return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"

    # ------------------------------------------------------------------
    # Auto-create / Ensure / New Edit Project
    # ------------------------------------------------------------------

    def _auto_create_edit_project(self, name: str = "Untitled Project") -> None:
        """
        Create an edit project without user interaction.

        Used when the user starts working (import/drag) without an explicit
        "New Project" action, and by the Project Manager's "New Project"
        flow. The project is unsaved — the first Ctrl+S will trigger a
        save-as dialog.

        No default timeline is pre-created. The media pool starts empty and
        the first timeline is lazily built when the user drags media onto
        the timeline view (``timeline_view._create_default_timeline``). The
        TimelinePreviewRenderer is likewise initialized lazily — when the
        first timeline arrives via ``_on_edit_timeline_changed``, the
        renderer is created on demand (see edit_controller.py).

        Args:
            name: Project name (default "Untitled Project")
        """
        # R-001: Delayed import for Edit module components via facade
        try:
            from src.features.edit.commands_facade import get_project_classes
            _proj = get_project_classes()
            if _proj is None:
                raise ImportError
            EditProject = _proj["EditProject"]
        except ImportError:
            raise RuntimeError("Edit feature is unavailable")
        import time as time_module

        project = EditProject.create(name=name)
        now = time_module.time()
        # timelines stays empty — created lazily on first media drop.
        project = EditProject(
            project_id=project.project_id,
            name=project.name,
            assets=project.assets,
            bins=project.bins,
            timelines=project.timelines,
            compositions=project.compositions,
            color_stacks=project.color_stacks,
            smart_bins=project.smart_bins,
            root_bin_id=project.root_bin_id,
            active_timeline_id=None,
            created_at=project.created_at,
            modified_at=now,
        )

        # Update state
        self._current_edit_project = project
        self._current_edit_project_filepath = None
        self._current_timeline_sequences = {}
        # M19.2b：新建空文档无作者色彩设定——清空上一工程的进程级
        # 覆盖层（本路径可能未经 close_project 直达，例如媒体池首次
        # 拖入时 _ensure_edit_project 自动建工程）。
        try:
            from src.shared.color.color_context_lifecycle import (
                on_project_closed as _clear_color_context,
            )
            _clear_color_context()
        except ImportError:
            logger.debug(
                "color_context_lifecycle unavailable; color overrides "
                "left as-is on auto-create",
            )

        # Update UI
        self.media_pool_panel.set_project(project)
        # Keep the timeline drag target on the same project immediately.
        # Without this, the first media-pool import creates a project but a
        # subsequent drag to the timeline is rejected as "no project set".
        # The new project intentionally has no persisted timeline yet. Show a
        # UI-only V1/A1 layout so the empty workspace already has real track
        # geometry for drag previews and drop hit-testing.
        clear_timeline = getattr(self.timeline_panel, "clear", None)
        if callable(clear_timeline):
            clear_timeline()
        self.timeline_panel.set_project(project)
        show_provisional = getattr(self.timeline_panel, "show_provisional_timeline", None)
        if callable(show_provisional):
            show_provisional()
        self.setWindowTitle(f"Topos - {name} *")
        # No timeline yet — the renderer will be initialized lazily when the
        # first timeline is created by a media drop.
        self._init_timeline_preview_renderer()

        logger.info(f"Auto-created edit project: {name}")

        # M1.2：重新装配 AI 子系统绑定到新工程（teardown 旧 + setup 新）
        self._setup_ai_editing()

    def _ensure_edit_project(self) -> bool:
        """
        Ensure an edit project exists. Auto-creates one if needed.

        Returns:
            True if a project now exists, False otherwise.
        """
        if self._current_edit_project is not None:
            return True
        self._auto_create_edit_project()
        return self._current_edit_project is not None

    def _create_video_project(self) -> None:
        """Create a new video project.

        The first timeline and its V1/A1 tracks are created lazily when the
        user drags the first media asset onto the timeline.
        """
        # Ask for project name
        name, ok = QInputDialog.getText(
            self,
            I18N.tr("dialog.new_project", "New Project"),
            I18N.tr("dialog.project_name", "Project Name:"),
            text="Untitled Project"
        )
        if not ok or not name:
            return

        name = name.strip()
        self._auto_create_edit_project(name=name)

        self._clear_all_dirty_states()  # Explicit creation, not dirty yet

        # Update status
        self.status_bar.showMessage(
            I18N.tr("status.new_project", f"New project created: {name}"),
            3000
        )

        logger.info(f"New edit project created: {name}; timeline will be created on first media drop")

    def _read_project_mode_from_manifest(self, filepath: str) -> str | None:
        """Lightweight read of a .topos archive's project_mode.

        Opens the zip and reads only ``manifest.json`` (no full extraction) so
        we can decide whether the project matches the active work mode before
        paying for the full load. Returns the project_mode string, or None if
        the archive is malformed / unreadable.
        """
        try:
            import zipfile
            import json as _json
            with zipfile.ZipFile(filepath, "r") as zf:
                with zf.open("manifest.json") as fh:
                    data = _json.load(fh)
            mode = data.get("project_mode", "standalone_image")
            # Legacy rename (mirrors ToposManifest.from_dict).
            if mode == "single_asset":
                mode = "standalone_image"
            return mode
        except Exception as exc:
            logger.debug("Could not read manifest from %s: %s", filepath, exc)
            return None

    def _report_content_migrations(self) -> None:
        """P2-13: 加载后展示内容 schema 迁移日志（状态栏 + 日志）。"""
        io = getattr(self, "edit_project_io", None)
        entries = getattr(io, "last_content_migrations", ()) if io else ()
        if not entries:
            return
        from src.features.edit.core.project_migration import (
            EDIT_CONTENT_SCHEMA_VERSION,
        )
        text = I18N.tr(
            "status.project_migrated",
            "工程已迁移到内容 schema v{version}",
        ).format(version=EDIT_CONTENT_SCHEMA_VERSION) + "：" + "；".join(entries)
        try:
            self.status_bar.showMessage(text, 8000)
        except Exception:  # noqa: BLE001 — 无状态栏环境（测试）仅记日志
            pass
        logger.info("project content migrations applied:\n%s", "\n".join(entries))

    def _report_package_compatibility(self) -> None:
        """DESK-07A：打开后展示包兼容分级与缺失资源结果（04B 分级）。

        E 级静默（无事不扰）；P/B 级状态栏 + 日志；X 级（导出被阻断）
        追加对话框——不阻止打开/编辑，但用户必须在导出前知情。
        """
        io = getattr(self, "edit_project_io", None)
        report = getattr(io, "get_last_capability_report", lambda: None)() \
            if io else None
        if report is None or report.grade == "E":
            return
        status, detail = format_capability_report(report)
        try:
            self.status_bar.showMessage(status, 10000)
        except Exception:  # noqa: BLE001 — 无状态栏环境（测试）仅记日志
            pass
        logger.warning("package compatibility %s:\n%s", report.grade, detail)
        if not report.export_allowed:
            try:
                from PyQt6.QtWidgets import QMessageBox
                QMessageBox.warning(
                    self,
                    I18N.tr("dialog.package_compat_title",
                            "工程兼容性受限"),
                    status + "\n\n" + detail,
                )
            except Exception:  # noqa: BLE001 — 无对话框环境（测试）
                pass

    def _load_standalone_project(self, filepath: str) -> None:
        """Hydrate the canonical image form of an EditProject."""
        try:
            result = self.edit_project_io.load_project(filepath)
        except Exception as exc:
            logger.exception("Failed to load image project: %s", filepath)
            QMessageBox.critical(
                self,
                I18N.tr("error.title", "Error"),
                I18N.tr("project.load_failed", "Failed to load project: {path}").format(
                    path=filepath
                ) + f"\n{exc}",
            )
            return

        if result is None:
            QMessageBox.critical(
                self,
                I18N.tr("error.title", "Error"),
                I18N.tr("project.load_failed", "Failed to load project: {path}").format(
                    path=filepath
                ),
            )
            return

        project, ui_state, timeline_sequences = result
        asset = next(
            (candidate for candidate in project.assets.values() if candidate.media_type == "image"),
            None,
        )
        if asset is None or project.timelines:
            QMessageBox.critical(
                self,
                I18N.tr("error.title", "Error"),
                I18N.tr("project.load_failed", "Not an image project: {path}").format(
                    path=filepath
                ),
            )
            return

        self._current_edit_project = project
        self._current_edit_project_filepath = filepath
        self._current_timeline_sequences = timeline_sequences or {}
        self.save_router.set_project_path(filepath)
        self._clear_all_dirty_states()
        if hasattr(self, "media_pool_panel"):
            self.media_pool_panel.set_project(project)
        if hasattr(self, "timeline_panel"):
            self.timeline_panel.set_project(project)

        # P2-13: 内容 schema 迁移提示
        self._report_content_migrations()

        self.load_image(asset.path, project_path=filepath)
        self._add_to_recent_projects(filepath)
        self.config_manager.set("last_project_path", os.path.dirname(filepath))
        self.config_manager.save_config()
        self.setWindowTitle(f"Topos - {project.name}")

    def _check_project_matches_work_mode(self, filepath: str) -> bool:
        """Return True if the project's type matches the active work mode.

        Shows a dialog telling the user to switch modes when there is a
        mismatch, and returns False (caller should abort the load).
        """
        project_mode = self._read_project_mode_from_manifest(filepath)
        if project_mode is None:
            # Unreadable manifest — let the normal load path surface the error.
            return True
        # standalone_image → image workflow; everything else needs Edit/timeline.
        expected = "image" if project_mode == "standalone_image" else "video"
        current = self._get_work_mode()
        if expected == current:
            return True
        # Mismatch: tell the user which mode to switch to.
        target_label = I18N.tr(
            "preferences.general.work_mode.image", "Image Grading"
        ) if expected == "image" else I18N.tr(
            "preferences.general.work_mode.video", "Video Grading"
        )
        QMessageBox.information(
            None,
            I18N.tr("project.mode_mismatch.title", "Wrong work mode for this project"),
            I18N.tr(
                "project.mode_mismatch.message",
                "This is a {project_kind} project but the app is in {current_mode} mode.\n\n"
                "Switch to {target_mode} (Preferences > General > Work Mode) and restart "
                "to open it.",
            ).format(
                project_kind=(
                    I18N.tr("project.kind.image", "image")
                    if project_mode == "standalone_image"
                    else I18N.tr("project.kind.video", "video")
                ),
                current_mode=(
                    I18N.tr("preferences.general.work_mode.image", "Image Grading")
                    if current == "image"
                    else I18N.tr("preferences.general.work_mode.video", "Video Grading")
                ),
                target_mode=target_label,
            ),
        )
        return False

    def _load_edit_project(self, filepath: str) -> None:
        """Load an Edit project from file."""
        # Work-mode mismatch: refuse to load a project whose type does not
        # match the active workflow, with a clear message telling the user to
        # switch modes (instead of silently failing or loading into the wrong UI).
        if not self._check_project_matches_work_mode(filepath):
            return
        # Guard: in Image Grading work mode the Edit feature is disabled, so
        # edit_project_io is None. Bail out defensively — recent project
        # menus/actions should be hidden, but this protects any stray caller.
        if getattr(self, "edit_project_io", None) is None:
            logger.warning("Cannot load Edit project (Edit feature disabled): %s", filepath)
            return
        # Check if autosave exists and prompt user. Keep the original path as
        # the live project path even when the recovery file is selected.
        original_filepath = filepath
        autosave_path = self.edit_project_io.get_autosave_filepath(filepath)
        load_filepath = filepath
        if autosave_path:
            reply = QMessageBox.question(
                self,
                I18N.tr("dialog.autosave_found", "Autosave Found"),
                I18N.tr(
                    "dialog.autosave_message",
                    f"An autosave file was found for this project.\n"
                    f"Do you want to restore from the autosave?\n\n"
                    f"Autosave: {autosave_path}\n"
                    f"Original: {filepath}"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                load_filepath = autosave_path
            else:
                # Choosing No means discard this recovery snapshot; otherwise
                # the same file would trigger the dialog on every next launch.
                self.edit_project_io.discard_autosave(original_filepath)

        # Load project
        result = self.edit_project_io.load_project(load_filepath)
        if result is None:
            QMessageBox.critical(
                self,
                I18N.tr("error.title", "Error"),
                I18N.tr("error.load_edit_project", f"Failed to load edit project: {filepath}")
            )
            return

        project, ui_state, timeline_sequences = result

        # P2-13: 内容 schema 迁移提示（旧工程升级为可见、可审计的信息）
        self._report_content_migrations()
        # DESK-07A：包兼容分级与缺失资源结果（E 级静默；X 级知情对话框）
        self._report_package_compatibility()

        # Update state
        self._current_edit_project = project
        self._current_edit_project_filepath = original_filepath
        self.edit_project_io.set_current_filepath(original_filepath)
        if load_filepath != original_filepath:
            self.edit_project_io._autosave_filepath = load_filepath
        self.save_router.set_project_path(original_filepath)
        self._current_timeline_sequences = timeline_sequences or {}
        self._clear_all_dirty_states()  # P0-5: Loaded project, clear all dirty states

        # Update Media Pool Panel
        self.media_pool_panel.set_project(project)
        self.timeline_panel.set_project(project)

        # Update Timeline Panel with timeline sequence (if available)
        active_sequence = None
        if timeline_sequences and project.active_timeline_id:
            active_sequence = timeline_sequences.get(project.active_timeline_id)
            if active_sequence:
                self.timeline_panel.set_timeline_sequence(active_sequence)
        if active_sequence is None:
            # Projects saved before their first media drop have no persisted
            # sequence. Keep the panel useful with the same UI-only V1/A1
            # layout used by a brand-new project.
            self.timeline_panel.clear()
            self.timeline_panel.show_provisional_timeline()

        # Restore UI state (if available)
        if ui_state:
            self._restore_edit_ui_state(ui_state)

        # Update window title
        self.setWindowTitle(f"Topos - {project.name}")

        # Add to recent projects
        self._add_to_recent_projects(filepath)
        self.config_manager.set("last_project_path", os.path.dirname(filepath))
        self.config_manager.save_config()

        # Update status
        self.status_bar.showMessage(
            I18N.tr("status.edit_project_loaded", f"Edit project loaded: {os.path.basename(filepath)}"),
            3000
        )

        logger.info(f"Edit project loaded: {filepath}")

        # Initialize TimelinePreviewRenderer (P2.1)
        self._init_timeline_preview_renderer()

        # M1.2：重新装配 AI 子系统绑定到新打开的工程（teardown 旧 + setup 新）
        self._setup_ai_editing()

    def _restore_edit_ui_state(self, ui_state: UIState) -> None:
        """Restore UI state after loading project."""
        # Restore playhead position
        if hasattr(self.timeline_panel, 'set_playhead_frame'):
            self.timeline_panel.set_playhead_frame(ui_state.playhead_frame)

        # Restore zoom level
        if hasattr(self.timeline_panel, 'set_zoom_level'):
            self.timeline_panel.set_zoom_level(ui_state.zoom_level)

        # SUB-P1-06: restore per-timeline layout (track heights/scroll/area
        # ratios). Runs after load_timeline so the engine knows the tracks.
        if getattr(ui_state, 'timeline_layout', None):
            try:
                view = getattr(self.timeline_panel, 'timeline_view', None)
                if view is not None:
                    view.set_layout_state(ui_state.timeline_layout)
            except Exception:
                logger.debug("Failed to restore timeline layout state", exc_info=True)

        # W07（接线计划 §6）：展示偏好恢复——layout 之后、选区/playhead
        # 同步之前；恢复零内容命令、零 revision（各 setter 内含状态锁）。
        if getattr(ui_state, 'timeline_presentation', None):
            try:
                apply = getattr(self.timeline_panel,
                                'apply_presentation_state', None)
                if callable(apply):
                    apply(ui_state.timeline_presentation)
            except Exception:
                logger.debug("Failed to restore timeline presentation state",
                             exc_info=True)

        # Restore expanded bins
        if hasattr(self.media_pool_panel, 'set_expanded_bins'):
            self.media_pool_panel.set_expanded_bins(ui_state.expanded_bins)

        # Restore view mode
        if hasattr(self.media_pool_panel, 'set_view_mode'):
            try:
                from src.features.edit.commands_facade import get_view_mode_enum
                ViewMode = get_view_mode_enum()
                if ViewMode is None:
                    raise ImportError
            except ImportError:
                logger.debug("Edit module unavailable; skipping view mode restore")
            else:
                mode = ViewMode[ui_state.view_mode.upper()] if isinstance(ui_state.view_mode, str) else ui_state.view_mode
                self.media_pool_panel.set_view_mode(mode)

    def _clear_all_dirty_states(self) -> None:
        """Clear all dirty state flags after successful save or project switch.

        P0-5 Fix: 统一清理所有 dirty 状态，确保保存成功后一致性。
        """
        self.edit_project_dirty = False
        # P9.1: Color session dirty（如果有）
        if hasattr(self, 'color_session_dirty'):
            self.color_session_dirty = False
        # Composite dirty（如果有）
        if hasattr(self, 'composite_dirty'):
            self.composite_dirty = False
        # Deliver dirty（如果有）
        if hasattr(self, 'deliver_dirty'):
            self.deliver_dirty = False

    # ------------------------------------------------------------------
    # Close-time save confirmation
    # ------------------------------------------------------------------

    def _has_unsaved_changes(self) -> bool:
        """Return True if the current project has unsaved changes.

        A project is considered unsaved when it exists but either has no
        file path yet (untitled/auto-created) or the dirty flag is set.
        """
        if not getattr(self, '_current_edit_project', None):
            return False
        if not getattr(self, '_current_edit_project_filepath', None):
            return True  # untitled project — never saved
        if getattr(self, 'edit_project_dirty', False):
            return True
        return False

    def _confirm_save_before_close(self) -> bool:
        """Prompt the user to save unsaved changes before the window closes.

        Returns True to proceed with closing, False to abort (user cancelled).
        Save → saves (may show "save as" for untitled); Discard → proceed;
        Cancel → abort close.
        """
        # AUD-2800: 记录用户是否明确选择 Discard。此前无论用户怎么选，
        # closeEvent 末尾的 _persist_active_project_before_close() 都会无
        # 条件 save_router.save()，把用户刚丢弃的脏状态写回主工程文件，
        # 「不保存并关闭」的语义失效。
        self._user_discarded_changes = False
        if not self._has_unsaved_changes():
            return True
        reply = QMessageBox.question(
            self,
            I18N.tr("dialog.unsaved_changes", "Unsaved Changes"),
            I18N.tr(
                "dialog.unsaved_changes_close_message",
                "The project has unsaved changes.\nDo you want to save before closing?"
            ),
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Save:
            try:
                self.save_file()
            except Exception as e:
                logger.error(f"Save before close failed: {e}")
                # If save failed, let the user decide again rather than
                # silently discarding.
                return False
            # If still unsaved after the save attempt (e.g. user cancelled
            # the "save as" dialog), abort the close.
            return not self._has_unsaved_changes()
        if reply == QMessageBox.StandardButton.Discard:
            # 用户明确丢弃未保存修改：关闭流程不得再把它们写回主工程。
            self._user_discarded_changes = True
            return True
        return False  # Cancel

    # ------------------------------------------------------------------
    # Quick Project
    # ------------------------------------------------------------------

    def _on_quick_project_requested(self, media_path: str) -> None:
        """
        处理 Quick Project 请求。

        弹出 QuickProjectDialog 让用户选择：
        - 创建 Quick Project（推荐）
        - 直接播放视频（传统调色模式）

        重要：dialog 先关闭，再创建项目。
        不在 dialog 内同步发射信号，避免重入。

        Args:
            media_path: 媒体文件路径
        """
        try:
            from src.features.edit.commands_facade import get_quick_project_dialog_class
            QuickProjectDialog = get_quick_project_dialog_class()
            if QuickProjectDialog is None:
                raise ImportError
        except ImportError:
            raise RuntimeError("Edit feature is unavailable")

        dialog = QuickProjectDialog(media_path, self.config_manager, self)
        result = dialog.exec()

        if result == QuickProjectDialog.DialogCode.Accepted:
            project_path = dialog.get_project_path()
            timeline_settings = dialog.get_timeline_settings()
            if project_path and timeline_settings:
                self._create_and_open_quick_project(
                    project_path, media_path, timeline_settings
                )

    def _create_and_open_quick_project(
        self,
        project_path: str,
        media_path: str,
        timeline_settings: dict,
    ):
        """
        创建并打开 Quick Project。

        Args:
            project_path: 项目文件路径
            media_path: 媒体文件路径
            timeline_settings: Timeline 设置字典
        """
        try:
            from src.features.edit.commands_facade import get_project_classes, get_project_io_classes
            _proj = get_project_classes()
            _io = get_project_io_classes()
            if _proj is None or _io is None:
                raise ImportError
            EditProject = _proj["EditProject"]
            EditProjectIO = _io["EditProjectIO"]
            EditProjectIOConfig = _io["EditProjectIOConfig"]
        except ImportError:
            raise RuntimeError("Edit feature is unavailable")

        try:
            # 获取项目名称
            project_name = os.path.splitext(os.path.basename(project_path))[0]

            # 获取 timeline 设置
            fps = timeline_settings.get("fps", 24.0)
            width = timeline_settings.get("width", 1920)
            height = timeline_settings.get("height", 1080)

            # 使用 EditProject.create_quick_project 创建项目
            project, asset_id, timeline_id = EditProject.create_quick_project(
                name=project_name,
                media_path=media_path,
                fps=fps,
                width=width,
                height=height,
            )

            # 设置 EditProject 为当前项目
            self._current_edit_project = project
            self._current_edit_project_filepath = project_path

            # 设置 MediaPoolPanel 的项目
            if hasattr(self, 'media_pool_panel') and self.media_pool_panel:
                self.media_pool_panel.set_project(project)

            # 设置 TimelinePanel 的项目
            if hasattr(self, 'timeline_panel') and self.timeline_panel:
                self.timeline_panel.set_project(project)

            # 保存项目到文件（一次性保存，不启动 autosave timer）
            io_config = EditProjectIOConfig(
                autosave_interval=0,
                backup_on_save=True,
            )
            edit_project_io = EditProjectIO(io_config)
            success = edit_project_io.save_project(project_path, project)

            if success:
                # The one-shot writer above only creates the archive. Bind the
                # MainWindow-owned IO manager too, otherwise its periodic
                # recovery autosave sees no current filepath and cannot
                # protect this newly created Quick Project.
                if getattr(self, "edit_project_io", None) is not None:
                    self.edit_project_io.set_current_filepath(project_path)
                self.save_router.set_project_path(project_path)
                # 添加到最近文件
                self._add_to_recent_files(project_path)

                # 延迟执行 UI 更新操作，避免在对话框创建流程中同步切页、
                # 初始化预览渲染器或触发 GL/Qt 状态重入。
                tid = timeline_id
                aid = asset_id
                auto_add = timeline_settings.get("auto_add_media", True)

                def _delayed_ui_setup() -> None:
                    try:
                        # 切换到新创建的 timeline
                        if hasattr(self.edit_page, 'switch_timeline'):
                            self.edit_page.switch_timeline(tid)

                        # 自动添加素材到 timeline
                        if auto_add and self.timeline_panel:
                            self._auto_add_media_to_timeline(project, aid, tid)

                        # 切换到 Edit 页面（直接设置页面索引，匹配 dev 分支行为：
                        # 避免触发 switch_to_page → enter_page_hook → _init_timeline_preview_renderer
                        # → VideoAsyncRenderer 在 Color 页面 GL 上下文仍活跃时做 doneCurrent/makeCurrent 导致崩溃）
                        self.pages.setCurrentIndex(1)  # Edit page

                    except Exception as e:
                        logger.error(f"Quick Project delayed UI setup failed: {e}")
                        self.status_bar.showMessage(
                            "项目已创建，请手动切换到 Edit 页面",
                            10000
                        )

                QTimer.singleShot(100, _delayed_ui_setup)

                # 更新状态栏
                self.status_bar.showMessage(
                    f"Quick Project 创建成功: {project_name}",
                    5000
                )

                logger.info(
                    f"Quick Project created and opened: {project_path}, "
                    f"asset={aid}, timeline={tid}"
                )
            else:
                self.status_bar.showMessage(
                    f"项目保存失败: {project_path}",
                    5000
                )
                logger.error(f"Failed to save Quick Project: {project_path}")

        except Exception as e:
            self.status_bar.showMessage(
                f"创建 Quick Project 失败: {str(e)}",
                5000
            )
            logger.error(f"Failed to create Quick Project: {e}")

    def _establish_composite_timeline_context(self, video_path: str) -> bool:
        """启动时为 Composite 页建立 timeline+clip 上下文（视频文件）。

        Composite 页默认图 ``TimelineClipInput → ToposColor → Output`` 依赖
        ``runtime_context`` + ``timeline_clip_gpu_provider``。但启动时停在
        Composite 页 + 视频文件，``load_video`` 只走 Color 管线，不建立
        EditProject/timeline/clip，导致 ``_runtime_context`` 始终为 None、
        图评估无输出（``graph produced no valid output``）。

        本方法补上缺失的 Edit 上下文：建立带 clip 的 timeline，然后走标准的
        ``Edit → Composite`` 上下文注入路径（``_handle_enter_composite_with_context``），
        让 Composite 页获得 runtime_context + provider。

        Args:
            video_path: 视频文件绝对路径。

        Returns:
            True 若上下文建立成功（``composite_page._runtime_context`` 非 None）。
        """
        from src.app.main_window import PageType

        # 幂等：若 Composite 已有 runtime_context（例如已被其他路径建立），跳过。
        if not self._composite_available or self.composite_page is None:
            return False
        if getattr(self.composite_page, "_runtime_context", None) is not None:
            logger.debug("[COMPOSITE-STARTUP] timeline context already established, skip")
            return True

        try:
            from src.features.edit.commands_facade import get_project_classes
            _proj = get_project_classes()
            if _proj is None:
                raise ImportError
            EditProject = _proj["EditProject"]
        except (ImportError, RuntimeError):
            logger.warning("[COMPOSITE-STARTUP] Edit feature unavailable, cannot build timeline context")
            return False

        try:
            # 1. 建立 quick project（含 asset + 空 timeline）
            project_name = os.path.splitext(os.path.basename(video_path))[0]
            project, asset_id, timeline_id = EditProject.create_quick_project(
                name=project_name,
                media_path=video_path,
            )

            # 2. 设置当前项目状态（与 _create_and_open_quick_project 一致）
            self._current_edit_project = project
            self._current_project_id = project.project_id
            self._current_edit_project_filepath = None
            if hasattr(self, 'media_pool_panel') and self.media_pool_panel:
                self.media_pool_panel.set_project(project)
            if hasattr(self, 'timeline_panel') and self.timeline_panel:
                self.timeline_panel.set_project(project)

            # 3. 实例化 timeline sequence（同步：_on_timeline_switched → _create_empty_timeline_sequence）
            if hasattr(self.edit_page, 'switch_timeline'):
                self.edit_page.switch_timeline(timeline_id)

            # 4. append clip 到 V1（同步执行，sequence 已就绪）
            self._auto_add_media_to_timeline(project, asset_id, timeline_id)

            # 5. 从 timeline sequence 取新建的 clip，构造 Edit→Composite PageContext。
            # 直接构造 context 走 _handle_enter_composite_with_context，比依赖 UI
            # selection 更可控（与 _auto_bind_composite_to_selected_clip 末段一致）。
            sequences = getattr(self, '_current_timeline_sequences', {}) or {}
            sequence = sequences.get(timeline_id)
            if sequence is None:
                logger.warning("[COMPOSITE-STARTUP] timeline sequence not found after switch")
                return False
            if not sequence.video_tracks:
                logger.warning("[COMPOSITE-STARTUP] no video track in timeline")
                return False
            v_track = sequence.video_tracks[0]
            if not v_track.clips:
                logger.warning("[COMPOSITE-STARTUP] no clip in video track after auto-add")
                return False
            clip = v_track.clips[-1]

            # 同步选中该 clip（供后续 _auto_bind_composite_to_selected_clip / playhead 查询使用）
            try:
                tl_view = self.timeline_panel.timeline_view
                if hasattr(tl_view, '_selection') and hasattr(tl_view, 'set_selection'):
                    sel = tl_view._selection.with_clip_selected(
                        clip.clip_id, v_track.track_id, timeline_id, replace=True)
                    tl_view.set_selection(sel)
            except Exception as e:
                logger.debug(f"[COMPOSITE-STARTUP] clip selection skipped: {e}")

            context = self._page_context_manager.create_context_for_edit_to_composite(
                clip_id=clip.clip_id,
                composite_id=getattr(clip, 'composite_id', None) or "",
                asset_id=clip.asset_id,
                timeline_id=timeline_id,
                timeline_frame=getattr(clip, 'timeline_start', 0),
                source_frame=getattr(clip, 'source_start', 0),
                clip_local_frame=0,
                color_stack_id=getattr(clip, 'color_stack_id', None),
            )
            context.source_page = PageType.EDIT
            context.target_page = PageType.COMPOSITE
            self._page_context_manager.set_current_context(context)

            # 6. 标准 Edit→Composite 上下文注入（建 runtime_context + provider）
            self._handle_enter_composite_with_context(context)

            success = getattr(self.composite_page, "_runtime_context", None) is not None
            if success:
                self._composite_timeline_context_established = True
                logger.info(
                    f"[COMPOSITE-STARTUP] timeline context established for {video_path} "
                    f"(clip={clip.clip_id[:8]}, timeline={timeline_id[:8]})"
                )
                # The Composite page now drives playback via the timeline clip frame
                # provider. Stop the legacy Color-page video controller so it does
                # not decode the same file concurrently and race the Composite
                # source context (mirrors page_controller's enter hook behavior).
                legacy_video_controller = getattr(self, "video_controller", None)
                if legacy_video_controller is not None and getattr(
                        legacy_video_controller, "is_playing", False):
                    try:
                        legacy_video_controller.stop_playback()
                    except (AttributeError, RuntimeError) as exc:
                        logger.warning(
                            "[COMPOSITE-STARTUP] failed to stop legacy video playback: %s",
                            exc,
                        )
            else:
                logger.warning("[COMPOSITE-STARTUP] context built but runtime_context still None")
            return success

        except Exception:
            logger.exception("[COMPOSITE-STARTUP] failed to establish timeline context")
            return False

    # ------------------------------------------------------------------
    # Video playback in panel
    # ------------------------------------------------------------------

    def play_video_in_panel(self, path) -> None:
        """Handle video playback from Files panel in embedded mode."""
        # 局部导入避免循环依赖；此前裸用名字，一旦调用必然 NameError
        from src.app.main_window import PLAYER_AVAILABLE

        if not PLAYER_AVAILABLE:
            # 内置播放器组件缺失：仅提示，绝不转交系统播放器
            logger.warning(f"内置播放器未加载，拒绝播放: {path}")
            self.status_bar.showMessage("内置播放器组件未加载，无法播放视频", 5000)
            return

        files_panel = self.files_panel
        try:
            # [ARCH-3] 路由决策统一到 plugins/video_pro/player_host.decide_route
            # （与独立模式 VideoProEngine 同一实现，消除双份复制漂移）
            from plugins.video_pro.player_host import decide_route, stop_other_players
            route = decide_route(path)

            if route == 'topos':
                from plugins.video_pro.topos_player import ToposPlayer
                player = getattr(self, '_topos_video_player', None)
                if player is None:
                    player = ToposPlayer(files_panel)
                    self._topos_video_player = player
                    player.play_failed.connect(
                        lambda msg: self.status_bar.showMessage(str(msg), 5000))
            elif route == 'libav':
                from plugins.video_pro.libav_player import LibavPlayer
                player = getattr(self, '_libav_video_player', None)
                if player is None:
                    player = LibavPlayer(files_panel)
                    self._libav_video_player = player
                    player.play_failed.connect(
                        lambda msg: self.status_bar.showMessage(str(msg), 5000))
            else:
                from plugins.video_pro.player_qt import UltimatePlayer
                player = getattr(self, 'video_player', None)
                # [CRASH-5] 持久复用单一实例。旧判断 "is None or not isVisible()"
                # 在播放器隐藏（停止/互斥/播完）时必然为真 → 每次播放都新建，
                # 旧实例（QMediaPlayer+AudioOutput+画布）永不清除。play_video()
                # 自身会完整重置状态，可直接复用。
                if player is None:
                    player = UltimatePlayer(files_panel)
                    self.video_player = player

            # 互斥：停掉并隐藏其他播放器（[ARCH-3] 停止逻辑与独立模式同一实现）
            stop_other_players(player, (
                getattr(self, 'video_player', None),
                getattr(self, '_topos_video_player', None),
                getattr(self, '_libav_video_player', None),
            ))

            player.setParent(files_panel)
            player.setGeometry(files_panel.rect())
            player.show()
            player.raise_()
            player.setFocus()
            player.play_video(path)
            # [ARCH-3] 浮层播放器跟随 FilesPanel 尺寸变化（此前 topos/libav
            # 不跟随——面板一变尺寸就露底或裁切；三引擎统一在此补齐）
            self._track_overlay_player(files_panel, player)
            self.status_bar.showMessage(f"正在播放: {os.path.basename(path)}", 3000)
        except Exception as e:
            # 播放器异常只记录并提示，不回退系统播放器（右键文件可手动选系统播放器）
            logger.error(f"player error: {e}")
            self.status_bar.showMessage(f"播放失败: {os.path.basename(path)}（可右键使用系统播放器打开）", 5000)

    def _track_overlay_player(self, files_panel, player) -> None:
        """[ARCH-3] 安装 FilesPanel 尺寸监听，让浮层播放器跟随面板 resize。"""
        if getattr(self, '_player_resize_relay', None) is None:
            self._player_resize_relay = _PanelResizeRelay(self._on_files_panel_resized)
            files_panel.installEventFilter(self._player_resize_relay)
        self._active_overlay_player = player

    def _on_files_panel_resized(self, size) -> None:
        player = getattr(self, '_active_overlay_player', None)
        if player is not None and player.isVisible():
            player.setGeometry(0, 0, size.width(), size.height())

    # ------------------------------------------------------------------
    # Load Video
    # ------------------------------------------------------------------

    def load_video(
        self,
        path: str,
        *,
        for_timeline_clip: bool = False,
        display_size: Optional[tuple[int, int]] = None,
    ):
        """
        加载视频文件进行调色

        Args:
            path: 视频文件路径
            for_timeline_clip: True when loading video source for a timeline clip
                (skips single asset session creation and rejection logic).
            display_size: Optional explicit source-display coordinate size.
                Timeline dimensions must not be passed here: Color displays
                and grades the source clip, while timeline composition belongs
                to the Edit Program viewer.
        """
        _flash_diag.event(
            'load_video', path=path, for_timeline_clip=for_timeline_clip,
            page=getattr(self, 'current_page', None),
        )
        # Image Grading mode has no Edit page, so a standalone video cannot be
        # opened by any path. Check this BEFORE the Color/Library guard so the
        # user gets the accurate "switch work mode" message rather than the
        # misleading "video requires a timeline" one.
        if not for_timeline_clip and self._get_work_mode() == "image":
            self._reject_video_in_image_mode(path)
            return
        if not for_timeline_clip and self.current_page in ("Color", "Library"):
            self._reject_standalone_video_for_color(path)
            return
        if not for_timeline_clip:
            self._clear_color_timeline_clip_playback_context()

        # 初始化视频控制器（如果尚未初始化）
        # 使用 EnhancedVideoPlaybackEngine 作为默认引擎
        # P0-1: 使用 resolve_video_pipeline_mode() 决策函数决定管线路径
        if self.video_controller is None:
            self.video_controller = EnhancedVideoPlaybackEngine(self.engine)
            # P0-1: 获取管线决策结果并启用对应路径
            pipeline_decision = self.video_controller.get_pipeline_decision()
            if pipeline_decision:
                logger.info(f"Video controller initialized: {pipeline_decision}")
                if pipeline_decision.is_v3_active():
                    self.video_controller.enable_dual_path(True)  # 启用 master/proxy 双路径架构
                else:
                    self.video_controller.enable_dual_path(False)  # 使用旧 v2 管线
            else:
                # 兼容旧逻辑（如果 get_pipeline_decision 未返回结果）
                if self.video_controller.is_v3_pipeline_enabled():
                    self.video_controller.enable_dual_path(True)
                else:
                    self.video_controller.enable_dual_path(False)
            # 连接视频控制器信号到 Canvas 的视频控制栏。Color 页时间线
            # clip 使用 clip-local 坐标显示，底层解码器仍使用 source 帧号。
            self.video_controller.frame_changed.connect(self._on_color_video_source_frame_changed)
            # 跟踪结果随帧应用：帧切换时重建受跟踪绑定影响的蒙版纹理
            self.video_controller.frame_changed.connect(self._on_color_tracking_frame_changed)
            self.video_controller.play_state_changed.connect(self._on_color_tracking_play_state_changed)
            self.video_controller.frame_ready.connect(self.on_video_frame_ready)  # 更新 Canvas
            self.video_controller.play_state_changed.connect(self.canvas.video_control_bar.on_play_state_changed)
            self.video_controller.play_state_changed.connect(self._on_video_play_state_changed)  # P0-5: debounce cleanup
            self.video_controller.play_state_changed.connect(self._on_scope_play_state_changed)
            # Color 页音频桥接：graded video_controller 是纯视频引擎，播放状态
            # 变化时镜像到 Edit 页的时间线音频（仅 Color timeline-clip 上下文生效）。
            self.video_controller.play_state_changed.connect(self._on_color_video_play_state_for_audio)
            self.video_controller.total_frames_changed.connect(self._on_color_video_total_frames_changed)

            # NOTE: Composite-specific signal connections (frame_changed → composite eval,
            # control bar updates) are made in _connect_video_control_bar() which runs
            # every time a video is loaded, ensuring they work even if the composite module
            # becomes available after the video controller was first created.

        if display_size is not None:
            if hasattr(self.video_controller, "set_display_scene_size"):
                self.video_controller.set_display_scene_size(display_size)
            if hasattr(self.canvas, "set_display_scene_size_override"):
                self.canvas.set_display_scene_size_override(display_size)
        else:
            if hasattr(self.video_controller, "clear_display_scene_size"):
                self.video_controller.clear_display_scene_size()
            if hasattr(self.canvas, "set_display_scene_size_override"):
                self.canvas.set_display_scene_size_override(None)

        # 停止现有的视频播放器（如果正在播放）
        # [ARCH-3] 此前只停 UltimatePlayer：若浮层是 topos/libav 播放器，
        # 加载调色后音频/解码线程仍在后台跑。三个持久实例统一停掉。
        for attr in ('video_player', '_topos_video_player', '_libav_video_player'):
            p = getattr(self, attr, None)
            if p is not None and p.isVisible():
                p.stop()
                p.hide()

        # [AUD-2804] 面板重置与 save_router 缓存清理移到加载成功之后：
        # 此前在尝试加载前即重置全部调色面板并清空路径缓存，新视频加载
        # 失败（损坏文件/解码器错误）后旧视频会话被"卸载"——未保存的面板
        # 级调整丢失、保存路由被破坏且无任何恢复。current_file_path 先行
        # 赋值保持加载过程中的信号路径语义，失败时按快照回滚。
        prev_file_path = self.current_file_path
        prev_is_video = bool(self.current_file_is_video)

        # 设置当前文件路径（视频项目会保存到 {视频名}.topos）
        self.current_file_path = path
        self.current_file_is_video = True

        # 加载视频
        if self.video_controller.load_video(path):
            self.engine.set_video_stabilization_state({})
            service = getattr(self, 'stabilization_service', None)
            if service is not None:
                self._cancel_stabilization()
                self._stabilizer_params_timer.stop()
            # 重置所有调色相关面板的参数（防止旧视频的调色应用到新视频）
            self.grading_panel.reset_all()
            if hasattr(self.hsl_curves_panel, 'reset_all_curves'):
                self.hsl_curves_panel.reset_all_curves()
            # 曲线工具（画布控制点 + 侧栏四参数/强度）整体回默认
            if hasattr(self, 'curves_widget'):
                self.curves_widget.reset(emit_changed=False)
            if hasattr(self, 'curves_panel'):
                self.curves_panel.set_layer_state(
                    {"curves_soft_clip": {}, "curves_edit": {}})
            if hasattr(self.effects_panel, 'reset_all'):
                self.effects_panel.reset_all()
            if hasattr(self.denoise_panel, 'reset_all'):
                self.denoise_panel.reset_all()

            # 切换到新视频时清除 save_router 的路径缓存（同 load_image）。
            self.save_router.clear_cache()
            self._add_to_recent_files(path)

            # A decoded frame's raster size can differ from its intended
            # display size (SAR/DAR).  Apply the same media presentation size
            # to Canvas that ProgramViewer uses, before fitting the Color view.
            get_display_size = getattr(self.video_controller, "get_media_display_size", None)
            media_display_size = get_display_size() if callable(get_display_size) else None
            if media_display_size and hasattr(self.canvas, "set_display_scene_size_override"):
                self.canvas.set_display_scene_size_override(media_display_size)
                refresh = getattr(self.video_controller, "refresh_current_frame_view", None)
                if callable(refresh):
                    refresh()
            # 显示 Canvas 上的视频控制栏
            self.canvas.video_control_bar.show_for_video()
            self._sync_color_timeline_clip_video_controls()

            # 显示 CompositeViewer 上的视频控制栏（如果可用）。
            # Composite 页启用 In/Out 范围控件（循环播放该区间）。
            if self._composite_available and self.composite_viewer:
                self.composite_viewer.video_control_bar.show_for_video_with_io()

            # 连接 Canvas 视频控制栏的信号到 video_controller。
            # load_video() can be called repeatedly without unload_video(); avoid
            # stacked connections where one click toggles play twice.
            self._connect_video_control_bar()

            # Standalone video grading is no longer a Color-page mode.  Timeline
            # clips must load only the source media here; their grade comes from
            # EditProject.color_stacks[clip.color_stack_id].
            if not for_timeline_clip:
                self._load_video_project(path)

            # 适配窗口大小
            self.canvas.fit_to_window()

            # 更新状态栏
            info = self.video_controller.get_video_info()
            if info:
                self.canvas.video_control_bar.set_frame_rate(info.get("fps", 24.0))
                self.status_bar.showMessage(
                    f"已加载视频: {os.path.basename(path)} ({info['width']}x{info['height']}, {info['fps']:.2f}fps, {info['total_frames']}帧)",
                    5000
                )

            # M4.2: 新素材不能复用旧素材的邻帧窗口（清掉渲染器侧缓存），
            # 并为首个暂停帧调度窗口获取。
            engine = getattr(self, 'engine', None)
            renderer = getattr(engine, 'renderer', None) if engine else None
            if renderer is not None and hasattr(renderer, 'set_video_denoise_window'):
                try:
                    renderer.set_video_denoise_window({})
                except Exception:
                    pass
            provider_obj = getattr(self, '_video_denoise_window_provider_obj', None)
            if provider_obj is not None:
                provider_obj.clear()
            self._schedule_video_denoise_window()
        else:
            # [AUD-2804] 加载失败：恢复旧会话状态（面板参数与 save_router
            # 缓存均未改动；current_file_path/is_video 按快照回滚——此前被
            # 无条件清空，且 current_file_is_video 残留 True 造成状态不一致）。
            self.current_file_path = prev_file_path
            self.current_file_is_video = prev_is_video
            self.status_bar.showMessage(f"加载视频失败: {os.path.basename(path)}", 5000)

    def _reject_standalone_video_for_color(self, path: str) -> None:
        """Reject standalone video opening in Color/Library pages.

        Video files must be added to an Edit timeline before they can be
        graded in Color.  Show an informational dialog explaining this.
        """
        QMessageBox.information(
            None,
            I18N.tr("color.video_requires_timeline.title", "Video grading uses the timeline"),
            I18N.tr(
                "color.video_requires_timeline.message",
                "Video files must be added to an Edit timeline before they can be graded in Color."
            ),
        )

    def _get_work_mode(self) -> str:
        """Current work mode (image/video/all). Delegates to feature_flags so
        the product-mode guard (only studio honors work_mode) stays consistent
        across the codebase. Defaults to 'all' on any error."""
        try:
            from src.shared.feature_flags import get_work_mode
            return get_work_mode(self.config_manager)
        except Exception:
            return "all"

    def _reject_video_in_image_mode(self, path: str) -> None:
        """Reject opening a video while in Image Grading work mode.

        Image mode has no Edit page, so videos cannot be graded at all. Point
        the user at Preferences to switch modes.
        """
        QMessageBox.information(
            None,
            I18N.tr(
                "color.video_blocked_in_image_mode.title",
                "Video not available in Image mode",
            ),
            I18N.tr(
                "color.video_blocked_in_image_mode.message",
                "Video files cannot be opened in Image Grading mode. "
                "Switch to Full or Video Grading mode "
                "(Preferences > General > Work Mode) and restart.",
            ),
        )

    def _on_library_video_selected(self, path: str) -> None:
        """Handle video_selected signal from Library panel.

        Rejects standalone video in Color/Library pages; otherwise loads normally.
        In Image Grading work mode videos are rejected entirely (no Edit page).
        """
        if self._get_work_mode() == "image":
            self._reject_video_in_image_mode(path)
            return
        if self.current_page in ("Color", "Library"):
            self._reject_standalone_video_for_color(path)
            return
        self.load_video(path)

    def _connect_video_control_bar(self) -> None:
        """Connect viewport video controls exactly once.
        
        Handles both Color page Canvas and Composite page CompositeViewer
        video control bars when composite module is available.
        """
        # Connect Color page Canvas video control bar
        bar = self.canvas.video_control_bar
        connections = (
            (bar.seek_requested, self.on_video_seek),
            (bar.scrub_started, self.on_video_scrub_started),
            (bar.scrub_moved, self.on_video_scrub_moved),
            (bar.scrub_finished, self.on_video_scrub_finished),
            (bar.play_toggled, self.on_video_play_toggled),
            (bar.next_frame, self.on_video_next_frame),
            (bar.prev_frame, self.on_video_prev_frame),
            (bar.first_frame, self.on_video_first_frame),
            (bar.last_frame, self.on_video_last_frame),
            (bar.loop_toggled, self.on_video_loop_toggled),
        )

        for signal, slot in connections:
            while True:
                try:
                    signal.disconnect(slot)
                except TypeError:
                    break
            signal.connect(slot)

        # When Color loaded a timeline video, Composite reuses the same source
        # video controller. Direct Edit -> Composite entries may not have this
        # controller; those are driven by the Edit timeline playback sync.
        if self.video_controller is not None and self._composite_available and self.composite_viewer:
            comp_bar = self.composite_viewer.video_control_bar
            comp_connections = (
                (comp_bar.seek_requested, self.on_video_seek),
                (comp_bar.scrub_started, self.on_video_scrub_started),
                (comp_bar.scrub_moved, self.on_video_scrub_moved),
                (comp_bar.scrub_finished, self.on_video_scrub_finished),
                (comp_bar.play_toggled, self.on_video_play_toggled),
                (comp_bar.next_frame, self.on_video_next_frame),
                (comp_bar.prev_frame, self.on_video_prev_frame),
                (comp_bar.first_frame, self.on_video_first_frame),
                (comp_bar.last_frame, self.on_video_last_frame),
                # In/Out 播放范围（仅 Composite 页启用 show_for_video_with_io 时可见）
                (comp_bar.mark_in_requested, self.on_composite_mark_in),
                (comp_bar.mark_out_requested, self.on_composite_mark_out),
                (comp_bar.clear_in_out_requested, self.on_composite_clear_in_out),
            )
            for signal, slot in comp_connections:
                while True:
                    try:
                        signal.disconnect(slot)
                    except TypeError:
                        break
                signal.connect(slot)

            video_connections = (
                (self.video_controller.frame_changed, self._on_video_frame_changed_for_composite),
                (self.video_controller.play_state_changed, self._on_video_play_state_for_composite),
            )
            for signal, slot in video_connections:
                while True:
                    try:
                        signal.disconnect(slot)
                    except TypeError:
                        break
                signal.connect(slot)

    def _get_timeline_composite_playback_controller(self):
        """Return Edit's playback clock for a timeline-backed Composite page."""
        if getattr(self, "current_page", None) != "Composite":
            return None
        page = getattr(self, "composite_page", None)
        if page is None or getattr(page, "_runtime_context", None) is None:
            return None
        panel = getattr(self, "timeline_panel", None)
        if panel is None:
            return None
        try:
            return panel.get_playback_controller()
        except (AttributeError, TypeError):
            return None

    def _color_timeline_preview_is_active(self) -> bool:
        return bool(
            getattr(self, "current_page", None) == "Color"
            and getattr(self, "_color_timeline_preview_renderer", None) is not None
            and self._active_color_timeline_clip_playback_context() is not None
        )

    def _color_timeline_playback_controller(self):
        if not self._color_timeline_preview_is_active():
            return None
        panel = getattr(self, "timeline_panel", None)
        if panel is None:
            return None
        try:
            return panel.get_playback_controller()
        except (AttributeError, TypeError):
            return None

    def _deactivate_color_timeline_preview(self) -> None:
        """Dispose Color's private timeline renderer before leaving the page.

        Its worker can otherwise emit into a viewport that Qt is in the middle
        of reparenting for Edit/Composite.  Disconnect first, then join the
        worker, so a late frame cannot touch the Color canvas.
        """
        renderer = getattr(self, "_color_timeline_preview_renderer", None)
        panel = getattr(self, "timeline_panel", None)
        controller = None
        if panel is not None:
            try:
                controller = panel.get_playback_controller()
            except (AttributeError, TypeError):
                pass
        slot = getattr(self, "_color_timeline_frame_slot", None)
        if controller is not None and slot is not None:
            try:
                controller.frame_changed.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        if renderer is not None:
            try:
                renderer.frame_ready.disconnect(self._on_color_timeline_preview_frame_ready)
            except (AttributeError, TypeError, RuntimeError):
                pass
            try:
                renderer.shutdown()
            except (AttributeError, RuntimeError):
                logger.debug("Failed to stop Color timeline preview", exc_info=True)
            try:
                renderer.deleteLater()
            except (AttributeError, RuntimeError):
                pass
        self._color_timeline_preview_renderer = None
        self._color_timeline_preview_id = None
        self._color_timeline_frame_slot = None
        end_preview = getattr(getattr(self, "canvas", None), "end_timeline_preview", None)
        if callable(end_preview):
            end_preview()

    def _activate_color_timeline_preview(self, context: object) -> bool:
        """Drive Color video preview from Edit's timeline render contract.

        Color's former source-local player had a second decode/cache epoch and
        could therefore show one cached source frame after Edit had already
        advanced to another clip.  A dedicated CPU timeline renderer shares
        Edit's playhead and render-plan rules while leaving Edit's realtime GPU
        renderer untouched.
        """
        if not callable(create_timeline_preview_renderer):
            return False
        timeline_id = getattr(context, "timeline_id", None)
        timeline = getattr(self, "_current_timeline_sequences", {}).get(timeline_id)
        project = getattr(self, "_current_edit_project", None)
        panel = getattr(self, "timeline_panel", None)
        if timeline is None or project is None or panel is None:
            return False
        try:
            controller = panel.get_playback_controller()
        except (AttributeError, TypeError):
            return False
        if controller is None:
            return False

        renderer = getattr(self, "_color_timeline_preview_renderer", None)
        if renderer is None or getattr(self, "_color_timeline_preview_id", None) != timeline_id:
            old_renderer = renderer
            if old_renderer is not None:
                old_slot = getattr(self, "_color_timeline_frame_slot", None)
                if old_slot is not None:
                    try:
                        controller.frame_changed.disconnect(old_slot)
                    except (TypeError, RuntimeError):
                        pass
                # Mirror _deactivate_color_timeline_preview: disconnect
                # frame_ready before shutdown so a frame still in flight from
                # the old worker cannot reach the shared Color canvas before
                # deleteLater() is processed on the event loop.
                try:
                    old_renderer.frame_ready.disconnect(self._on_color_timeline_preview_frame_ready)
                except (AttributeError, TypeError, RuntimeError):
                    pass
                try:
                    old_renderer.shutdown()
                    old_renderer.deleteLater()
                except (AttributeError, RuntimeError):
                    pass
            try:
                renderer = create_timeline_preview_renderer(
                    project=project,
                    sequence=timeline,
                    main_context=None,
                    config={"viewport_size": (1920, 1080), "cache_size": 16},
                )
            except Exception as exc:
                logger.warning("Unable to create Color timeline preview: %s", exc)
                return False
            renderer.frame_ready.connect(self._on_color_timeline_preview_frame_ready)
            renderer.set_playback_controller(controller)
            frame_slot = renderer.request_render
            controller.frame_changed.connect(frame_slot)
            self._color_timeline_preview_renderer = renderer
            self._color_timeline_preview_id = timeline_id
            self._color_timeline_frame_slot = frame_slot
        # The renderer owns these project/sequence objects already.  Calling
        # set_project()/set_sequence() for every clip card click resets its
        # worker generation while a previous request may still be completing;
        # on macOS that was the native-thread race behind the selection crash.
        # A clip selection only moves the shared timeline playhead.

        # Color loops the selected clip only on explicit user request. The
        # normal state is full-timeline playback through subsequent clips, so
        # each newly selected clip starts with loop off.  Composite keeps its
        # own default-ON loop because it owns a separate VideoControlBar
        # instance whose __init__ defaults to checked.
        bar = self.canvas.video_control_bar
        if bar.btn_loop.isChecked():
            # Toggling to False fires loop_toggled -> on_video_loop_toggled,
            # but _configure_color_timeline_playback_range below reapplies the
            # correct range afterwards.  Guarding the set avoids a redundant
            # signal when loop is already off and keeps the loop choice per-clip
            # rather than leaking across clips.
            bar.btn_loop.setChecked(False)
        self._configure_color_timeline_playback_range()
        begin_preview = getattr(self.canvas, "begin_timeline_preview", None)
        if callable(begin_preview):
            begin_preview()
        timeline_frame = int(getattr(context, "timeline_frame", 0) or 0)
        controller.seek(timeline_frame)
        return True

    def _configure_color_timeline_playback_range(self) -> None:
        """Apply Color loop state: full timeline normally, selected clip in loop."""
        controller = self._color_timeline_playback_controller()
        context = self._active_color_timeline_clip_playback_context()
        if controller is None or context is None:
            return
        clip = context["clip"]
        loop_current_clip = bool(self.canvas.video_control_bar.btn_loop.isChecked())
        if loop_current_clip:
            start = int(getattr(clip, "timeline_start", 0) or 0)
            total = self._color_timeline_clip_total_frames() or 1
            controller.set_playback_range(start, start + int(total) - 1)
        else:
            # TimelinePanel reserves a minimum empty tail for UI layout. That
            # must not become playable media in Color; use the real content
            # extent (or an explicit timeline in/out) instead.
            controller.set_playback_range(*self._color_timeline_content_range())
        controller.set_loop(loop_current_clip)

    def _color_timeline_content_range(self) -> tuple[int, int]:
        """Return the playable full-timeline range, excluding panel padding."""
        context = self._active_color_timeline_clip_playback_context()
        timeline_id = context.get("timeline_id") if context else None
        timeline = getattr(self, "_current_timeline_sequences", {}).get(timeline_id)
        if timeline is None:
            return (0, 0)
        explicit_start = getattr(timeline, "range_start", None)
        explicit_end = getattr(timeline, "range_end", None)
        if explicit_start is not None and explicit_end is not None:
            try:
                return (max(0, int(explicit_start)), max(int(explicit_start), int(explicit_end)))
            except (TypeError, ValueError):
                pass
        end = 0
        for track in getattr(timeline, "all_tracks", ()):
            for clip in getattr(track, "clips", ()):
                end = max(end, int(getattr(clip, "timeline_end", 0) or 0) - 1)
        return (0, max(0, end))

    def _on_color_timeline_preview_frame_ready(self, frame: int, result: object) -> None:
        """Present only completed, current-generation timeline preview frames."""
        if not self._color_timeline_preview_is_active() or not isinstance(result, QImage):
            return
        controller = self._color_timeline_playback_controller()
        if controller is not None and getattr(controller, "is_playing", False):
            if int(frame) < int(getattr(controller, "current_frame", frame)):
                return
        self.canvas.set_timeline_preview_image(result)
        self._on_color_timeline_playhead_changed(int(frame))

    def _on_color_timeline_playhead_changed(self, timeline_frame: int) -> None:
        """Update the Color transport from the shared clock.

        The range bar follows whichever clip the playhead is actually in, so
        cross-clip playback reads as one clip's range after another (first frame
        to last) rather than stalling the cursor at the selected clip's Out.
        """
        if not self._color_timeline_preview_is_active():
            return
        context = self._active_color_timeline_clip_playback_context()
        playing_clip = self._resolve_color_timeline_playing_clip(int(timeline_frame), context)
        bar = self.canvas.video_control_bar
        total = self._clip_total_frames(playing_clip) or 1
        local = int(timeline_frame) - int(getattr(playing_clip, "timeline_start", 0) or 0)
        bar.set_total_frames(total)
        bar.on_frame_changed(max(0, min(local, total - 1)))
        # Cross-clip playback: highlight the clip the playhead has reached so the
        # strip reads as "one clip after another playing".  The card highlight
        # follows the same clip the bar now reflects.
        self._advance_color_timeline_active_clip(timeline_frame, context)

    def _resolve_color_timeline_playing_clip(self, timeline_frame: int, context):
        """Return the clip the playhead is actually inside, else the bound clip.

        Cross-clip playback keeps the originally selected clip bound as the
        context (its in/out range and grade stay loaded), but the range bar and
        scrub mapping must reflect the clip the playhead has reached.  Both the
        playhead callback and ``on_video_seek`` use this so a scrub of the bar
        maps to the same clip the bar is displaying.
        """
        bound_clip = context["clip"] if context else None
        if context is None:
            return bound_clip
        timeline = getattr(self, "_current_timeline_sequences", {}).get(context.get("timeline_id"))
        if timeline is None:
            return bound_clip
        for track in getattr(timeline, "video_tracks", ()) or ():
            playing_clip = track.get_clip_at(int(timeline_frame))
            if playing_clip is not None:
                return playing_clip
        return bound_clip

    def _clip_total_frames(self, clip) -> int | None:
        """Return a clip's frame count from its duration attributes."""
        if clip is None:
            return None
        for attr_name in ("timeline_duration", "duration", "source_duration"):
            try:
                value = int(getattr(clip, attr_name, 0) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return max(1, value)
        return None

    def _advance_color_timeline_active_clip(self, timeline_frame: int, context) -> None:
        """Move the strip highlight to whichever clip the playhead now covers."""
        strip = getattr(self, "color_timeline_clip_strip", None)
        if strip is None or context is None:
            return
        bound_clip = context["clip"]
        bound_clip_id = getattr(bound_clip, "clip_id", None)
        timeline = getattr(self, "_current_timeline_sequences", {}).get(context.get("timeline_id"))
        if timeline is None:
            return
        for track in getattr(timeline, "video_tracks", ()) or ():
            playing_clip = track.get_clip_at(int(timeline_frame))
            if playing_clip is not None:
                playing_clip_id = getattr(playing_clip, "clip_id", None)
                if playing_clip_id and playing_clip_id != bound_clip_id:
                    strip.set_active_clip(playing_clip_id)
                return

    def _set_color_timeline_clip_playback_context(
        self,
        timeline_id: str,
        clip: object,
        *,
        effective_source_start: int | None = None,
    ) -> None:
        """Bind Color's source video player to a timeline clip frame domain."""
        _flash_diag.event(
            'clip_playback_context',
            clip_id=getattr(clip, 'clip_id', None),
            timeline_id=timeline_id,
            source_start=getattr(clip, 'source_start', None),
            effective_source_start=effective_source_start,
        )
        self._color_timeline_clip_playback_context = {
            "timeline_id": timeline_id,
            "clip": clip,
            "effective_source_start": effective_source_start,
        }
        self._sync_color_timeline_clip_video_controls()

    def _clear_color_timeline_clip_playback_context(self) -> None:
        self._color_timeline_clip_playback_context = None
        # Reset the same-source cross-clip wrap tracking so a fresh clip entry
        # does not mistake a stale previous frame for a wrap.
        self._last_color_source_frame = None
        self._cross_clip_advancing = False
        end_preview = getattr(getattr(self, "canvas", None), "end_timeline_preview", None)
        if callable(end_preview):
            end_preview()
        if self.video_controller is not None:
            try:
                self.video_controller.clear_in_out_range()
            except Exception:
                pass
        refresh_keyframes = getattr(self, "_color_keyframes_host_refresh", None)
        if callable(refresh_keyframes):
            refresh_keyframes()

    def _active_color_timeline_clip_playback_context(self) -> dict | None:
        if getattr(self, "current_page", None) != "Color":
            return None
        context = getattr(self, "_color_timeline_clip_playback_context", None)
        if not context:
            return None
        clip = context.get("clip")
        if clip is None:
            return None
        return context

    def _color_timeline_clip_total_frames(self) -> int | None:
        context = self._active_color_timeline_clip_playback_context()
        if not context:
            return None
        return self._clip_total_frames(context["clip"])

    def _color_local_frame_to_source_frame(self, frame_number: int) -> int:
        context = self._active_color_timeline_clip_playback_context()
        if not context:
            return int(frame_number)
        clip = context["clip"]
        total = max(1, int(getattr(clip, "timeline_duration", 1) or 1))
        local_frame = max(0, min(int(frame_number), total - 1))
        effective_start = context.get("effective_source_start")
        clip_start = int(getattr(clip, "source_start", 0) or 0)
        offset = int(effective_start) - clip_start if effective_start is not None else 0
        try:
            return int(clip.clip_local_to_source_frame(local_frame)) + offset
        except (AttributeError, TypeError, ValueError):
            return clip_start + offset + local_frame

    def _color_local_frame_to_timeline_frame(self, frame_number: int) -> int | None:
        """Map Color's clip-local transport coordinate to the shared timeline."""
        context = self._active_color_timeline_clip_playback_context()
        if not context:
            return None
        clip = context["clip"]
        total = self._color_timeline_clip_total_frames() or 1
        local_frame = max(0, min(int(frame_number), int(total) - 1))
        return int(getattr(clip, "timeline_start", 0) or 0) + local_frame

    def _sync_color_timeline_playhead(self, local_frame: int) -> None:
        """Reflect Color transport movement in the Edit timeline playhead."""
        timeline_frame = self._color_local_frame_to_timeline_frame(local_frame)
        if timeline_frame is None:
            return
        timeline_panel = getattr(self, "timeline_panel", None)
        if timeline_panel is None:
            return
        try:
            if timeline_panel.get_playhead_frame() == timeline_frame:
                return
            timeline_panel.set_playhead_frame(timeline_frame)
        except (AttributeError, TypeError, RuntimeError) as exc:
            logger.debug("Failed to synchronize Color transport to timeline: %s", exc)

    def _color_source_frame_to_local_frame(self, source_frame: int) -> int:
        context = self._active_color_timeline_clip_playback_context()
        if not context:
            return int(source_frame)
        clip = context["clip"]
        total = max(1, int(getattr(clip, "timeline_duration", 1) or 1))
        effective_start = context.get("effective_source_start")
        clip_start = int(getattr(clip, "source_start", 0) or 0)
        offset = int(effective_start) - clip_start if effective_start is not None else 0
        try:
            local = int(clip.source_to_clip_local_frame(int(source_frame) - offset))
        except (AttributeError, TypeError, ValueError):
            local = int(source_frame) - clip_start - offset
        return max(0, min(local, total - 1))

    def _sync_color_timeline_clip_video_controls(self) -> None:
        """Keep Color's transport in clip-local coordinates for timeline clips."""
        context = self._active_color_timeline_clip_playback_context()
        if not context:
            return
        sync_tracker = getattr(self, '_sync_tracker_clip_range', None)
        if callable(sync_tracker):
            sync_tracker()
        bar = getattr(getattr(self, "canvas", None), "video_control_bar", None)
        if bar is None:
            return
        clip = context["clip"]
        total = self._color_timeline_clip_total_frames()
        if total is not None:
            try:
                bar.set_total_frames(total)
            except (AttributeError, TypeError, ValueError):
                pass
        source_start = int(
            context.get("effective_source_start")
            if context.get("effective_source_start") is not None
            else getattr(clip, "source_start", 0) or 0
        )
        source_duration = max(1, int(getattr(clip, "source_duration", total or 1) or 1))
        source_out = source_start + source_duration - 1
        if self.video_controller is not None:
            try:
                self.video_controller.set_in_out_range(source_start, source_out)
            except Exception:
                pass
            try:
                raw_current = getattr(self.video_controller, "current_frame", source_start)
                current_source = source_start if raw_current is None else int(raw_current)
                bar.on_frame_changed(self._color_source_frame_to_local_frame(current_source))
            except (AttributeError, TypeError, ValueError):
                pass

    def _on_color_video_total_frames_changed(self, total_frames: int) -> None:
        sync_tracker = getattr(self, '_sync_tracker_clip_range', None)
        if callable(sync_tracker):
            sync_tracker()
        clip_total = self._color_timeline_clip_total_frames()
        if clip_total is not None:
            self.canvas.video_control_bar.set_total_frames(clip_total)
        else:
            self.canvas.video_control_bar.set_total_frames(total_frames)

    def _on_color_video_source_frame_changed(self, source_frame: int) -> None:
        source_frame = int(source_frame)
        display_frame = self._color_source_frame_to_local_frame(source_frame)
        self.canvas.video_control_bar.on_frame_changed(display_frame)
        self._sync_color_timeline_playhead(display_frame)
        # KF-02: 关键帧面板播放头回填（clip-local 域；set_playhead 不发
        # seek，无回声回路）。宿主未接线时钩子缺席，零开销。
        keyframes_hook = getattr(self, "_color_keyframes_frame_hook", None)
        if callable(keyframes_hook):
            keyframes_hook(display_frame)
        # KF-04: 自动录制路由的有效帧来源（源帧域；seek/播放/scrub 都经此）
        self._color_last_source_frame = source_frame
        # P1-COLOR-03: 记录当前源帧号；图层带调色关键帧轨道时按帧插值求值
        # （预览与导出共用同一求值函数，帧值一致）。无轨道时零开销。
        try:
            self.engine.set_grade_frame(source_frame)
        except Exception:
            logger.debug("Failed to set engine grade frame", exc_info=True)
        # Must run BEFORE the pre-advance/wrap-advance logic below rebinds the
        # context: the audio anchor has to be computed with the same clip
        # mapping the playhead sync above used.
        self._maybe_reanchor_color_audio(source_frame, display_frame)
        # Pre-advance into a contiguous same-source clip BEFORE the engine's
        # in/out range wraps.  Waiting for the wrap forced a stop + blocking
        # master seek + restart at every clip boundary (a visible hitch) and
        # raced the wrap's seek-to-in_frame against the new clip range (a
        # one-frame flash of the departing clip).  Rebinding the context on
        # the clip's last frame lets the decode stream flow into the next
        # clip with no seek at all.
        self._maybe_preadvance_same_source_clip(source_frame)
        # Detect the in/out-range wrap (clip reached its out-frame and the
        # engine looped back to in_frame).  When that happens and the next
        # timeline clip shares the same source file, advance to it without a
        # reload — a same-file seek keeps playback smooth and graded.
        self._maybe_advance_to_next_same_source_clip(source_frame)
        # Only remember frames inside the current clip's source range.  A wrap
        # emission (frame == the OLD clip's in_frame) can be processed after
        # the context already rebound to the NEXT clip, and recording that
        # out-of-range frame would fake a second wrap and skip a clip.
        ctx = self._active_color_timeline_clip_playback_context()
        if ctx is not None:
            cclip = ctx["clip"]
            range_start = int(
                ctx.get("effective_source_start")
                if ctx.get("effective_source_start") is not None
                else getattr(cclip, "source_start", 0) or 0
            )
            range_dur = max(1, int(getattr(cclip, "source_duration", 0) or 0))
            if range_start <= source_frame <= range_start + range_dur - 1:
                self._last_color_source_frame = source_frame
        else:
            self._last_color_source_frame = source_frame

    def _on_color_video_play_state_for_audio(self, is_playing: bool) -> None:
        """Mirror the Color-page video transport onto Edit's timeline audio.

        The graded video_controller path is a pure video engine; without this
        bridge Color-page playback is silent even when the timeline has audio
        tracks.  Gated on the Color timeline-clip context so Composite or
        Library usage of the shared video_controller never starts audio.
        """
        context = self._active_color_timeline_clip_playback_context()
        if context is None:
            return
        edit_page = getattr(self, "edit_page", None)
        if edit_page is None:
            return
        if not is_playing:
            edit_page.pause_timeline_audio_playback()
            return
        start_frame = self._color_video_timeline_frame_for_audio()
        if start_frame is None:
            return
        timeline = getattr(self, "_current_timeline_sequences", {}).get(
            context.get("timeline_id")
        ) or getattr(edit_page, "_current_timeline", None)
        edit_page.start_timeline_audio_playback(timeline, start_frame)

    def _color_video_timeline_frame_for_audio(self) -> int | None:
        """Map the Color video engine's current source frame to a timeline frame."""
        if self.video_controller is None:
            return None
        context = self._active_color_timeline_clip_playback_context()
        if context is None:
            return None
        raw = getattr(self.video_controller, "current_frame", None)
        if raw is None:
            raw = getattr(self, "_last_color_source_frame", None)
        if raw is None:
            clip = context["clip"]
            raw = (
                context.get("effective_source_start")
                if context.get("effective_source_start") is not None
                else getattr(clip, "source_start", 0) or 0
            )
        local = self._color_source_frame_to_local_frame(int(raw))
        return self._color_local_frame_to_timeline_frame(local)

    def _maybe_reanchor_color_audio(self, source_frame: int, local_frame: int) -> None:
        """Keep timeline audio aligned with the Color transport while playing.

        Covers slow clock drift and the in/out-range wrap (loop), where the
        video restarts from the clip's in-frame while the audio clock would
        run on past the clip.  A backward jump with a same-source successor
        is the cross-clip advance path instead: its stop -> seek -> restart
        re-fires ``play_state_changed`` and re-anchors audio there, so
        seeking here would only add a glitch.
        """
        if not bool(getattr(self.video_controller, "is_playing", False)):
            return
        context = self._active_color_timeline_clip_playback_context()
        if context is None:
            return
        last_frame = getattr(self, "_last_color_source_frame", None)
        if (
            last_frame is not None
            and int(source_frame) < int(last_frame) - 1
            and self._find_next_same_source_clip(context, context["clip"]) is not None
        ):
            return
        timeline_frame = self._color_local_frame_to_timeline_frame(local_frame)
        if timeline_frame is None:
            return
        edit_page = getattr(self, "edit_page", None)
        if edit_page is None:
            return
        edit_page.reanchor_timeline_audio(timeline_frame)

    def _maybe_preadvance_same_source_clip(self, source_frame: int) -> None:
        """Advance to a contiguous same-source clip before the in/out wrap.

        Fires on the clip's last displayed frame while playing.  When the
        following timeline clip uses the same source file and its source range
        starts exactly at ``current out + 1``, switching is only a context
        rebind: the playback clock keeps running, the already-prefetched next
        frame is inside the new in/out range, and no seek/stop/restart (and
        no wrap back to in_frame) happens.  Non-contiguous neighbours keep the
        legacy wrap-detection path.
        """
        if getattr(self, "_cross_clip_advancing", False):
            return
        if not bool(getattr(self.video_controller, "is_playing", False)):
            return
        context = self._active_color_timeline_clip_playback_context()
        if not context:
            return
        clip = context["clip"]
        source_start = int(
            context.get("effective_source_start")
            if context.get("effective_source_start") is not None
            else getattr(clip, "source_start", 0) or 0
        )
        source_duration = max(1, int(getattr(clip, "source_duration", 0) or 0))
        out_frame = source_start + source_duration - 1
        if source_frame != out_frame:
            return
        next_clip = self._find_next_same_source_clip(context, clip)
        if next_clip is None:
            return
        next_start = int(getattr(next_clip, "source_start", 0) or 0)
        if next_start != out_frame + 1:
            # Non-contiguous source ranges still need the wrap + seek path.
            return
        self._cross_clip_advancing = True
        try:
            self._set_color_timeline_clip_playback_context(
                context["timeline_id"],
                next_clip,
                effective_source_start=next_start,
            )
            strip = getattr(self, "color_timeline_clip_strip", None)
            if strip is not None:
                try:
                    strip.set_active_clip(getattr(next_clip, "clip_id", None))
                except (AttributeError, RuntimeError):
                    pass
            self._switch_color_stack_for_cross_clip_advance(next_clip)
        finally:
            self._cross_clip_advancing = False

    def _find_next_same_source_clip(self, context: dict, clip: object):
        """Return the timeline clip that follows ``clip`` if it is same-source.

        Shared by the pre-advance (seamless) and wrap-advance (seek) paths so
        both apply identical neighbour/source-file validation.
        """
        timeline = getattr(self, "_current_timeline_sequences", {}).get(context.get("timeline_id"))
        if timeline is None:
            return None
        boundary = int(getattr(clip, "timeline_end", 0) or 0)
        next_clip = None
        for track in getattr(timeline, "video_tracks", ()) or ():
            cand = track.get_clip_at(boundary)
            if cand is not None:
                next_clip = cand
                break
        if next_clip is None or getattr(next_clip, "clip_id", None) == getattr(clip, "clip_id", None):
            return None
        project = getattr(self, "_current_edit_project", None)
        if project is None:
            return None
        assets = getattr(project, "assets", {}) or {}
        cur_asset = assets.get(getattr(clip, "asset_id", None))
        next_asset = assets.get(getattr(next_clip, "asset_id", None))
        if not cur_asset or not next_asset:
            return None
        if getattr(cur_asset, "path", None) != getattr(next_asset, "path", None):
            return None
        return next_clip

    def _switch_color_stack_for_cross_clip_advance(self, next_clip: object) -> None:
        """Switch the active ColorStack when playback crosses into a new clip.

        Manual clip-card activation runs the full enter-Color transaction
        (flush current stack -> ensure target stack -> rebind the save
        callback -> hydrate panels/engine).  The same-source pre-advance and
        wrap-advance paths used to rebind only the playback context, so the
        engine kept applying the previously selected clip's grade and every
        following clip of the same source looked identical during continuous
        playback.  Replay the stack half of that transaction here; hydration
        also invalidates the player's cached render params so the very next
        decoded frame is graded with the new clip's stack.
        """
        if getattr(self, "current_page", None) != "Color":
            return
        if getattr(self, "_current_edit_project", None) is None:
            return
        clip_id = getattr(next_clip, "clip_id", None)
        if not clip_id:
            return
        stack_id = getattr(next_clip, "color_stack_id", None)
        stack = (
            getattr(self, "_current_edit_project", None).color_stacks.get(stack_id)
            if stack_id and getattr(self, "_current_edit_project", None) is not None
            else None
        )
        inherited_local_stack = bool(
            stack_id
            and not getattr(next_clip, "color_shared", False)
            and getattr(stack, "scope", "clip_local") == "clip_local"
            and getattr(stack, "clip_id", None)
            and getattr(stack, "clip_id", None) != clip_id
        )
        if not stack_id or inherited_local_stack:
            ensure = getattr(self, "_ensure_clip_has_color_stack", None)
            if callable(ensure):
                try:
                    stack_id = ensure(
                        clip_id,
                        getattr(next_clip, "asset_id", None) or "",
                    )
                except Exception:
                    logger.debug(
                        "Failed to ensure color stack for cross-clip advance",
                        exc_info=True,
                    )
        if not stack_id:
            return
        # Explicitly linked clips share one stack; nothing to switch then.
        current_stack_id = getattr(
            getattr(self, "grading_panel", None), "current_stack_id", None
        ) or getattr(self, "_grading_save_stack_id", None)
        if current_stack_id == stack_id:
            self._retarget_color_session_for_clip(next_clip, stack_id)
            return
        flush = getattr(self, "_flush_active_grading_stack_before_switch", None)
        if callable(flush):
            try:
                flush(stack_id)
            except Exception:
                logger.debug(
                    "Failed to flush color stack before cross-clip advance",
                    exc_info=True,
                )
        setup = getattr(self, "_setup_grading_save_callback", None)
        if callable(setup):
            try:
                setup(stack_id)
            except Exception:
                logger.debug(
                    "Failed to rebind grading save callback on cross-clip advance",
                    exc_info=True,
                )
        hydrate = getattr(self, "_load_color_stack_to_grading_panel", None)
        if callable(hydrate):
            # Suppress write-backs while the new stack's values flow into the
            # panels, exactly like the manual enter-Color path.
            self._color_stack_hydration_in_progress = True
            try:
                hydrate(stack_id)
            except Exception:
                logger.debug(
                    "Failed to hydrate color stack on cross-clip advance",
                    exc_info=True,
                )
            finally:
                self._color_stack_hydration_in_progress = False
        self._retarget_color_session_for_clip(next_clip, stack_id)

    def _retarget_color_session_for_clip(self, clip: object, stack_id: str) -> None:
        """Point the active ColorSession at ``clip`` so save routing follows.

        ColorSession is frozen, so swap in a replacement carrying the new
        clip/stack identifiers; frame fields stay untouched until playback
        updates them through the normal path.
        """
        manager = getattr(self, "_color_session_manager", None)
        session = getattr(manager, "get_current_session", lambda: None)() if manager else None
        if manager is None or session is None:
            return
        if getattr(session, "clip_id", None) == getattr(clip, "clip_id", None) \
                and getattr(session, "color_stack_id", None) == stack_id:
            return
        try:
            from dataclasses import replace as _dc_replace
            manager.set_current_session(
                _dc_replace(
                    session,
                    clip_id=getattr(clip, "clip_id", None),
                    color_stack_id=stack_id,
                )
            )
        except Exception:
            logger.debug(
                "Failed to retarget color session on cross-clip advance",
                exc_info=True,
            )

    def _maybe_advance_to_next_same_source_clip(self, source_frame: int) -> None:
        """On the in/out wrap, cross into the next clip if it is same-source.

        video_controller plays one source file with an in/out range, so at the
        out-frame it loops back to in_frame rather than stopping.  That wrap is
        the only signal a clip finished.  When the neighbouring timeline clip
        uses the same source asset, switching is just a seek (no load_video),
        so cross-clip playback stays smooth and goes through the full Color
        grading pipeline.  Different-source neighbours are left looping.
        """
        if getattr(self, "_cross_clip_advancing", False):
            return
        context = self._active_color_timeline_clip_playback_context()
        if not context:
            return
        clip = context["clip"]
        source_start = int(
            context.get("effective_source_start")
            if context.get("effective_source_start") is not None
            else getattr(clip, "source_start", 0) or 0
        )
        # The wrap lands exactly on in_frame after a frame at/after out.  A
        # plain user seek back to the start would not be preceded by an
        # out-area frame, so this pair distinguishes a real wrap.
        last_frame = getattr(self, "_last_color_source_frame", None)
        source_duration = max(1, int(getattr(clip, "source_duration", 0) or 0))
        out_frame = source_start + source_duration - 1
        if last_frame is None or source_frame != source_start:
            return
        # A genuine wrap requires the previous frame to have been AT the tail
        # of the current clip's displayed range.  A seek signal (e.g. the
        # non-blocking seek emitted by this very advance) carries last_frame ==
        # the previous clip's tail, which lies outside the freshly rebound
        # range — without the upper bound it would cascade into skipping the
        # next clip.
        if not (out_frame - 1 <= int(last_frame) <= out_frame + 1):
            return
        if source_duration <= 1:
            return
        # Find the clip that begins at the current clip's timeline end.  Only
        # cross when both clips resolve to the same source file — that is the
        # only case a seek can replace a reload.
        next_clip = self._find_next_same_source_clip(context, clip)
        if next_clip is None:
            return
        # Same source: rebind the context (resets the in/out range via the
        # sync call inside), seek to the next clip's source start, and move the
        # strip highlight.  The decode head never leaves the open file, so this
        # is a non-blocking seek with no visible stutter.
        self._cross_clip_advancing = True
        try:
            # seek_to_frame stops playback (it calls stop_playback internally),
            # so remember the playing state and resume afterwards — otherwise
            # the cross-clip advance would land on the next clip's first frame
            # and freeze there.
            was_playing = bool(getattr(self.video_controller, "is_playing", False))
            if was_playing and self.video_controller is not None:
                # Pre-arm the renderer guards BEFORE the stop -> seek ->
                # restart gap.  seek_to_frame stops playback internally, so a
                # RENDER_FRAME (grading refresh / viewport resize) already in
                # the async queue can be processed while playback is
                # momentarily inactive, with no pending flag and no generation
                # advance yet.  Without this, such a command republishes the
                # departing clip's retained frame and the viewer flashes
                # another clip's picture for one frame while the user adjusts
                # colour parameters.  start_playback() below repeats both
                # calls; they are idempotent.
                _bump_epoch = getattr(
                    self.video_controller,
                    "_begin_static_preview_generation",
                    None,
                )
                if callable(_bump_epoch):
                    try:
                        _bump_epoch()
                    except Exception:
                        logger.debug(
                            "Failed to bump video generation before clip advance",
                            exc_info=True,
                        )
                _mark_pending = getattr(
                    self.video_controller,
                    "_mark_renderer_video_texture_pending",
                    None,
                )
                if callable(_mark_pending):
                    try:
                        _mark_pending()
                    except Exception:
                        logger.debug(
                            "Failed to mark video texture pending before clip advance",
                            exc_info=True,
                        )
            self._set_color_timeline_clip_playback_context(
                context["timeline_id"],
                next_clip,
                effective_source_start=int(getattr(next_clip, "source_start", 0) or 0),
            )
            if self.video_controller is not None:
                try:
                    self.video_controller.seek_to_frame(int(getattr(next_clip, "source_start", 0) or 0))
                except (AttributeError, RuntimeError):
                    logger.debug("Failed to seek to next same-source clip", exc_info=True)
            strip = getattr(self, "color_timeline_clip_strip", None)
            if strip is not None:
                try:
                    strip.set_active_clip(getattr(next_clip, "clip_id", None))
                except (AttributeError, RuntimeError):
                    pass
            self._switch_color_stack_for_cross_clip_advance(next_clip)
            if was_playing and self.video_controller is not None:
                start_playback = getattr(self.video_controller, "start_playback", None)
                if callable(start_playback):
                    try:
                        start_playback()
                    except (AttributeError, RuntimeError):
                        logger.debug("Failed to resume playback after same-source clip advance", exc_info=True)
                    # Keep the control bar's play/pause button in sync with the
                    # engine state we just restored.
                    on_play_state = getattr(self.canvas.video_control_bar, "on_play_state_changed", None)
                    if callable(on_play_state):
                        try:
                            on_play_state(bool(getattr(self.video_controller, "is_playing", False)))
                        except (AttributeError, TypeError):
                            pass
        finally:
            self._cross_clip_advancing = False

    def on_video_seek(self, frame_number: int) -> None:
        """处理视频跳转请求"""
        timeline_controller = self._get_timeline_composite_playback_controller()
        if timeline_controller is not None:
            timeline_controller.seek(int(frame_number))
            return
        color_timeline_controller = self._color_timeline_playback_controller()
        if color_timeline_controller is not None:
            context = self._active_color_timeline_clip_playback_context()
            # The range bar follows the playing clip, so a scrub must map through
            # that same clip's timeline region (not the bound context clip), or
            # dragging clip B's bar would seek into clip A.  Resolve the clip at
            # the current playhead so seek and the bar agree on which clip a local
            # frame belongs to.
            current_frame = int(getattr(color_timeline_controller, "current_frame", int(frame_number)) or 0)
            playing_clip = self._resolve_color_timeline_playing_clip(current_frame, context)
            total = self._clip_total_frames(playing_clip) or 1
            local_frame = max(0, min(int(frame_number), total - 1))
            timeline_frame = local_frame + int(getattr(playing_clip, "timeline_start", 0) or 0)
            color_timeline_controller.seek(timeline_frame)
            return
        if self.video_controller:
            self._sync_color_timeline_playhead(int(frame_number))
            source_frame = self._color_local_frame_to_source_frame(int(frame_number))
            if getattr(self, "_viewport_scrub_settle_pending", False):
                # 松手精确定位：拖拽期间 bar 已停播，旧行为会按"暂停态 seek"
                # 推断为阻塞，在 UI 线程同步等 master 解码（长 GOP 4K 可卡
                # ~300ms）。改为非阻塞 seek + 就绪轮询上屏精确帧，松手瞬间
                # UI 不再冻结。
                self._viewport_scrub_settle_pending = False
                seek_kwargs = {"blocking": False}
                if getattr(self, "current_page", None) == "Color":
                    seek_kwargs["allow_proxy_fallback"] = False
                try:
                    self.video_controller.seek_to_frame(source_frame, **seek_kwargs)
                except TypeError:
                    seek_kwargs.pop("allow_proxy_fallback", None)
                    self.video_controller.seek_to_frame(source_frame, **seek_kwargs)
                self._poll_settle_exact_frame(int(frame_number))
                self._schedule_video_denoise_window()
                return
            self.video_controller.seek_to_frame(source_frame)
            self._schedule_video_denoise_window()

    def on_video_scrub_started(self, frame_number: int) -> None:
        """Start low-latency viewport scrub preview for Color/Composite bars."""
        scope_scrub = getattr(self, '_on_scope_scrub_state_changed', None)
        if callable(scope_scrub):
            scope_scrub(True)
        color_timeline_controller = self._color_timeline_playback_controller()
        if color_timeline_controller is not None:
            if getattr(color_timeline_controller, "is_playing", False):
                color_timeline_controller.pause()
            self.on_video_seek(int(frame_number))
            return
        self._viewport_scrub_active = True
        self._viewport_scrub_pending_frame = None
        self._viewport_scrub_timer_pending = False
        self._viewport_scrub_last_emit_ms = 0.0
        self._viewport_scrub_settle_pending = False
        # 新一轮拖拽从零速度开始，避免继承上一轮的隔行取帧步长。
        self._viewport_scrub_last_emit_frame = None
        self._viewport_scrub_last_emit_at_s = None
        self._viewport_scrub_velocity = 0.0
        # Invalidate a ready-frame poll left over from a previous drag.
        self._viewport_scrub_preview_token = int(
            getattr(self, "_viewport_scrub_preview_token", 0) or 0
        ) + 1
        # 就绪帧泵的作废键：token 会被每个拖拽目标翻新（作废旧式精确等待
        # 链），泵必须整轮拖拽存活，故用独立的拖拽代数。
        self._viewport_scrub_drag_generation = int(
            getattr(self, "_viewport_scrub_drag_generation", 0) or 0
        ) + 1
        self._viewport_scrub_pump_armed = False
        timeline_controller = self._get_timeline_composite_playback_controller()
        if timeline_controller is not None:
            try:
                if getattr(timeline_controller, "is_playing", False):
                    timeline_controller.pause()
            except Exception:
                pass
        elif self.video_controller is not None:
            try:
                if getattr(self.video_controller, "is_playing", False):
                    self.video_controller.stop_playback()
            except Exception:
                pass
        self.on_video_scrub_moved(frame_number)

    def on_video_scrub_moved(self, frame_number: int) -> None:
        """Coalesce high-frequency viewport scrub moves to the latest frame."""
        if self._color_timeline_playback_controller() is not None:
            self.on_video_seek(int(frame_number))
            return
        self._viewport_scrub_active = True
        self._viewport_scrub_pending_frame = int(frame_number)
        now_ms = time.monotonic() * 1000.0
        last_emit_ms = float(getattr(self, "_viewport_scrub_last_emit_ms", 0.0) or 0.0)
        interval_ms = int(getattr(self, "_viewport_scrub_interval_ms", 8) or 8)
        elapsed_ms = now_ms - last_emit_ms
        if getattr(self, "_viewport_scrub_timer_pending", False):
            # 自愈：合并定时器是一次性 singleShot 回调 + 布尔闩锁（与编辑页
            # scrub 派发同款结构）。回调丢失会让闩锁永久卡死，之后所有拖拽
            # 输入在下方 early-return——画面冻结。超窗未 flush 则强制复位。
            scheduled_at = float(
                getattr(self, "_viewport_scrub_timer_scheduled_ms", 0.0) or 0.0
            )
            if scheduled_at and (now_ms - scheduled_at) > 50.0:
                self._viewport_scrub_timer_pending = False
                playback_trace("viewport.scrub_coalesce_watchdog_recover", frame=int(frame_number))
            else:
                return
        if elapsed_ms >= interval_ms and not getattr(self, "_viewport_scrub_timer_pending", False):
            self._emit_pending_viewport_scrub()
            return
        delay_ms = max(1, int(interval_ms - elapsed_ms))
        self._viewport_scrub_timer_pending = True
        self._viewport_scrub_timer_scheduled_ms = now_ms
        self._viewport_scrub_timer_token = int(
            getattr(self, "_viewport_scrub_timer_token", 0) or 0
        ) + 1
        token = self._viewport_scrub_timer_token

        def _flush() -> None:
            if token != getattr(self, "_viewport_scrub_timer_token", 0):
                return
            self._viewport_scrub_timer_pending = False
            self._emit_pending_viewport_scrub()

        QTimer.singleShot(delay_ms, _flush)

    def on_video_scrub_finished(self, _frame_number: int) -> None:
        """End scrub mode; seek_requested emitted by the bar performs exact seek."""
        scope_scrub = getattr(self, '_on_scope_scrub_state_changed', None)
        if callable(scope_scrub):
            scope_scrub(False)
        if self._color_timeline_playback_controller() is not None:
            return
        self._viewport_scrub_active = False
        self._viewport_scrub_pending_frame = None
        self._viewport_scrub_timer_pending = False
        # 松手后紧随的 seek_requested 走非阻塞精确上屏（见 on_video_seek）。
        self._viewport_scrub_settle_pending = True
        # Prevent a late async preview from overtaking the exact release seek.
        self._viewport_scrub_preview_token = int(
            getattr(self, "_viewport_scrub_preview_token", 0) or 0
        ) + 1
        # 终止就绪帧泵（回调 ≤12ms 内自查 active/代数后退出）。
        self._viewport_scrub_drag_generation = int(
            getattr(self, "_viewport_scrub_drag_generation", 0) or 0
        ) + 1

    def _poll_settle_exact_frame(self, local_frame: int) -> None:
        """Present the exact master frame after a non-blocking scrub-release seek.

        与拖拽期间的 _poll_ready_viewport_scrub_frame 相同的"只取已就绪精确
        帧"策略，但生命周期独立于 _viewport_scrub_active（松手后仍需轮询），
        由 _viewport_scrub_preview_token 作废（新一轮拖拽/seek 即终止），
        并以 ~2s 超时兜底。
        """
        video_controller = getattr(self, "video_controller", None)
        present_ready = getattr(video_controller, "present_ready_scrub_frame", None)
        if not callable(present_ready):
            return

        source_frame = self._color_local_frame_to_source_frame(int(local_frame))
        token = int(getattr(self, "_viewport_scrub_preview_token", 0) or 0)
        deadline_ms = time.monotonic() * 1000.0 + 2000.0

        def _present_when_ready() -> None:
            if token != getattr(self, "_viewport_scrub_preview_token", 0):
                return
            if time.monotonic() * 1000.0 > deadline_ms:
                return
            try:
                if present_ready(source_frame):
                    return
            except (AttributeError, RuntimeError, TypeError) as exc:
                logger.debug("Settle exact-frame poll failed: %s", exc)
                return
            QTimer.singleShot(12, _present_when_ready)

        QTimer.singleShot(0, _present_when_ready)

    def _emit_pending_viewport_scrub(self) -> None:
        frame = getattr(self, "_viewport_scrub_pending_frame", None)
        self._viewport_scrub_pending_frame = None
        if frame is None:
            return
        now_s = time.monotonic()
        self._viewport_scrub_last_emit_ms = now_s * 1000.0
        stride = 1
        timeline_controller = self._get_timeline_composite_playback_controller()
        try:
            if timeline_controller is not None:
                timeline_controller.seek(int(frame))
            elif self.video_controller is not None:
                # V-P0-01: scrub 是高频交互热路径，必须非阻塞——绝不在 UI 线程
                # 同步等 master 解码（长 GOP 4K 可卡 ~300ms）。只发异步 seek，
                # 目标帧由静态预览轮询 / renderer 异步交付；松手后的精确 master
                # 提升由 bar 的 seek_requested -> on_video_seek 完成。
                # Each emitted target supersedes any still-polling older one.
                self._viewport_scrub_preview_token = int(
                    getattr(self, "_viewport_scrub_preview_token", 0) or 0
                ) + 1
                # 自适应隔行取帧：快速拖拽按 1x/2x/4x/8x/16x 步长量化 seek 目标，
                # 让每次 master 解码都能上屏（否则追不上的解码完成后即被丢弃）。
                # 慢拖步长为 1 逐帧；松手（settle）仍精确落在指针下方帧。
                from src.shared.video.scrub_stride import (
                    DEFAULT_DISPLAY_RATE_CAP,
                    display_stride,
                    quantize_frame,
                    update_velocity,
                )

                last_frame = getattr(self, "_viewport_scrub_last_emit_frame", None)
                last_at = getattr(self, "_viewport_scrub_last_emit_at_s", None)
                velocity = 0.0
                if last_frame is not None and last_at is not None:
                    velocity = update_velocity(
                        float(getattr(self, "_viewport_scrub_velocity", 0.0) or 0.0),
                        int(frame) - int(last_frame),
                        now_s - float(last_at),
                    )
                self._viewport_scrub_last_emit_frame = int(frame)
                self._viewport_scrub_last_emit_at_s = now_s
                self._viewport_scrub_velocity = velocity
                stride = display_stride(velocity, DEFAULT_DISPLAY_RATE_CAP)
                target_frame = quantize_frame(int(frame), stride)
                source_frame = self._color_local_frame_to_source_frame(target_frame)
                # 拖拽期允许就绪帧即时反馈（含 proxy）：重编码格式的 master 在
                # CPU 解码下太慢，只等精确 master 会让画面在拖拽中冻结。
                # 松手路径（on_video_seek settle + _poll_settle_exact_frame）
                # 仍以精确 master 帧收尾，调色落点始终是全画质。
                seek_kwargs = {"blocking": False, "allow_proxy_fallback": True, "fast_scrub": True}
                try:
                    self.video_controller.seek_to_frame(source_frame, **seek_kwargs)
                except TypeError:
                    # Compatibility with test doubles and older controllers
                    # that do not expose the optional quality policy yet.
                    seek_kwargs.pop("fast_scrub", None)
                    seek_kwargs.pop("allow_proxy_fallback", None)
                    self.video_controller.seek_to_frame(source_frame, **seek_kwargs)
                self._viewport_scrub_latest_source_target = int(source_frame)
                self._poll_ready_viewport_scrub_frame(int(frame))
            playback_trace("viewport.scrub_preview", frame=int(frame), stride=stride)
        except Exception as exc:
            logger.debug("Viewport scrub preview failed: %s", exc)

    def _poll_ready_viewport_scrub_frame(self, local_frame: int) -> None:
        """Present the latest async-decoded Color scrub frame without blocking.

        拖拽期间以单一 12ms 泵循环持续取"最新已完成"的 master 帧上屏。此前
        每个拖拽事件起一条精确帧等待链，新事件到来即作废旧链：事件快于解码
        时目标永远等不到，显示率被锁步在"解码 + 事件间隔 + 轮询粒度"周期
        （实测 4K ProRes ~29ups，解码本身 ~105fps）。泵与事件解耦后画面以
        解码完成率步进；拖拽代数与 active 状态负责作废，松手后的精确落点
        仍由 settle 路径（_poll_settle_exact_frame）负责。
        """
        if not getattr(self, "_viewport_scrub_active", False):
            return
        video_controller = getattr(self, "video_controller", None)
        present_latest = getattr(
            video_controller, "present_latest_ready_scrub_frame", None
        )
        if callable(present_latest):
            if getattr(self, "_viewport_scrub_pump_armed", False):
                return
            self._viewport_scrub_pump_armed = True
            generation = int(getattr(self, "_viewport_scrub_drag_generation", 0) or 0)

            def _pump_once() -> None:
                if (
                    generation != int(
                        getattr(self, "_viewport_scrub_drag_generation", -1) or -1
                    )
                    or not getattr(self, "_viewport_scrub_active", False)
                ):
                    self._viewport_scrub_pump_armed = False
                    return
                target = int(getattr(self, "_viewport_scrub_latest_source_target", 0) or 0)
                try:
                    present_latest(target)
                except (AttributeError, RuntimeError, TypeError) as exc:
                    logger.debug("Viewport scrub ready-frame pump failed: %s", exc)
                QTimer.singleShot(12, _pump_once)

            QTimer.singleShot(0, _pump_once)
            return

        # 兼容回退：控制器未提供 latest-ready 接口（测试替身/旧实现）时，
        # 维持旧的"精确帧等待链"行为。
        present_ready = getattr(video_controller, "present_ready_scrub_frame", None)
        if not callable(present_ready):
            return

        source_frame = self._color_local_frame_to_source_frame(int(local_frame))
        token = int(getattr(self, "_viewport_scrub_preview_token", 0) or 0)

        def _present_when_ready() -> None:
            if (
                token != getattr(self, "_viewport_scrub_preview_token", 0)
                or not getattr(self, "_viewport_scrub_active", False)
            ):
                return
            pending_local = getattr(self, "_viewport_scrub_pending_frame", None)
            if pending_local is not None and int(pending_local) != int(local_frame):
                return
            try:
                if present_ready(source_frame):
                    return
            except (AttributeError, RuntimeError, TypeError) as exc:
                logger.debug("Viewport scrub ready-frame preview failed: %s", exc)
                return
            QTimer.singleShot(12, _present_when_ready)

        QTimer.singleShot(0, _present_when_ready)

    def on_video_play_toggled(self) -> None:
        """处理播放/暂停切换"""
        timeline_controller = self._get_timeline_composite_playback_controller()
        if timeline_controller is not None:
            if timeline_controller.is_playing:
                timeline_controller.pause()
            else:
                timeline_controller.play()
            return
        color_timeline_controller = self._color_timeline_playback_controller()
        if color_timeline_controller is not None:
            if getattr(color_timeline_controller, "is_playing", False):
                color_timeline_controller.pause()
            else:
                self._configure_color_timeline_playback_range()
                color_timeline_controller.play()
            self.canvas.video_control_bar.on_play_state_changed(
                bool(getattr(color_timeline_controller, "is_playing", False))
            )
            return
        if self.video_controller:
            context = self._active_color_timeline_clip_playback_context()
            if context and not getattr(self.video_controller, "is_playing", False):
                self._sync_color_timeline_clip_video_controls()
                clip = context["clip"]
                source_start = int(
                    context.get("effective_source_start")
                    if context.get("effective_source_start") is not None
                    else getattr(clip, "source_start", 0) or 0
                )
                source_duration = max(1, int(getattr(clip, "source_duration", 1) or 1))
                source_out = source_start + source_duration - 1
                raw_current = getattr(self.video_controller, "current_frame", source_start)
                current_source = source_start if raw_current is None else int(raw_current)
                if current_source < source_start or current_source > source_out:
                    preview = getattr(self.video_controller, "request_static_preview_frame", None)
                    if callable(preview):
                        preview(source_start)
                    else:
                        try:
                            self.video_controller.seek_to_frame(source_start, force_display=True)
                        except TypeError:
                            self.video_controller.seek_to_frame(source_start)
            self.video_controller.toggle_play()
            self._schedule_video_denoise_window()

    def on_video_next_frame(self) -> None:
        """处理下一帧"""
        timeline_controller = self._get_timeline_composite_playback_controller()
        if timeline_controller is not None:
            timeline_controller.pause()
            timeline_controller.step(1)
            return
        if self._color_timeline_playback_controller() is not None:
            self.on_video_seek(int(self.canvas.video_control_bar.current_frame) + 1)
            return
        if self.video_controller:
            context = self._active_color_timeline_clip_playback_context()
            if context:
                current = self._color_source_frame_to_local_frame(
                    int(getattr(self.video_controller, "current_frame", 0) or 0)
                )
                total = self._color_timeline_clip_total_frames() or 1
                self.on_video_seek(min(current + 1, total - 1))
                return
            self.video_controller.next_frame()
            self._schedule_video_denoise_window()

    def on_video_prev_frame(self) -> None:
        """处理上一帧"""
        timeline_controller = self._get_timeline_composite_playback_controller()
        if timeline_controller is not None:
            timeline_controller.pause()
            timeline_controller.step(-1)
            return
        if self._color_timeline_playback_controller() is not None:
            self.on_video_seek(max(0, int(self.canvas.video_control_bar.current_frame) - 1))
            return
        if self.video_controller:
            context = self._active_color_timeline_clip_playback_context()
            if context:
                current = self._color_source_frame_to_local_frame(
                    int(getattr(self.video_controller, "current_frame", 0) or 0)
                )
                self.on_video_seek(max(0, current - 1))
                return
            self.video_controller.prev_frame()
            self._schedule_video_denoise_window()

    def on_video_first_frame(self) -> None:
        """处理跳到第一帧"""
        timeline_controller = self._get_timeline_composite_playback_controller()
        if timeline_controller is not None:
            timeline_controller.pause()
            timeline_controller.seek(0)
            return
        if self._color_timeline_playback_controller() is not None:
            self.on_video_seek(0)
            return
        if self.video_controller:
            if self._active_color_timeline_clip_playback_context():
                self.on_video_seek(0)
                return
            self.video_controller.first_frame()
            self._schedule_video_denoise_window()

    # ------------------------------------------------------------------
    # M4.2: 暂停态时域降噪邻帧窗口（UI 侧调度与交付）
    # ------------------------------------------------------------------

    def _video_denoise_window_provider(self):
        """惰性创建邻帧窗口提供器并连接交付信号。"""
        provider = getattr(self, '_video_denoise_window_provider_obj', None)
        if provider is None:
            from src.shared.video.denoise.window_provider import (
                VideoDenoiseWindowProvider,
            )

            provider = VideoDenoiseWindowProvider(self)
            provider.window_ready.connect(self._on_video_denoise_window_ready)
            self._video_denoise_window_provider_obj = provider
        return provider

    def _schedule_video_denoise_window(self, delay_ms: int = 300) -> None:
        """暂停/seek/逐帧/参数变化后调度邻帧窗口获取（去抖合并）。

        只覆盖单视频（video_controller）路径；Color 时间线 clip 路径的
        source 帧映射较复杂，作为后续迭代（见 docs/video_denoise_spec.md）。
        单独构造的 Mixin 测试桩没有全部主窗口属性，全部走 getattr 防御。
        """
        if not (getattr(self, 'current_file_is_video', False)
                and getattr(self, 'current_file_path', None)):
            return
        if getattr(self, 'video_controller', None) is None:
            return
        # Color 时间线 clip 上下文：帧号映射不同，V1 不覆盖
        timeline_ctrl = getattr(self, '_color_timeline_playback_controller', None)
        if callable(timeline_ctrl) and timeline_ctrl() is not None:
            return
        clip_ctx = getattr(self, '_active_color_timeline_clip_playback_context', None)
        if callable(clip_ctx) and clip_ctx():
            return

        token = int(getattr(self, '_vdw_schedule_token', 0) or 0) + 1
        self._vdw_schedule_token = token

        def _fire() -> None:
            if token != int(getattr(self, '_vdw_schedule_token', 0) or 0):
                return
            self._dispatch_video_denoise_window()

        QTimer.singleShot(max(0, int(delay_ms)), _fire)

    def _dispatch_video_denoise_window(self) -> None:
        """去抖到期：校验当前状态并派发后台取帧。"""
        if not (getattr(self, 'current_file_is_video', False)
                and getattr(self, 'current_file_path', None)):
            return
        video_controller = getattr(self, 'video_controller', None)
        if video_controller is None:
            return
        is_playing_fn = getattr(self, '_is_video_playing', None)
        if callable(is_playing_fn) and is_playing_fn():
            return
        if bool(getattr(video_controller, 'is_playing', False)):
            return
        timeline_ctrl = getattr(self, '_color_timeline_playback_controller', None)
        if callable(timeline_ctrl) and timeline_ctrl() is not None:
            return
        clip_ctx = getattr(self, '_active_color_timeline_clip_playback_context', None)
        if callable(clip_ctx) and clip_ctx():
            return

        engine = getattr(self, 'engine', None)
        params = getattr(engine, 'video_denoise_params', None) if engine else None
        temporal = getattr(params, 'temporal', None)
        if temporal is None or not temporal.is_active():
            return

        source_frame = getattr(video_controller, 'current_frame', None)
        if source_frame is None:
            return
        try:
            source_frame = int(source_frame)
        except (TypeError, ValueError):
            return
        if source_frame < 0:
            return

        try:
            self._video_denoise_window_provider().schedule(
                getattr(self, 'current_file_path'), source_frame, params
            )
        except Exception:
            logger.exception("schedule video denoise window failed")

    def _on_video_denoise_window_ready(self, payload: dict) -> None:
        """邻帧窗口就绪：投递渲染器并触发暂停帧重绘（仍需匹配当前状态）。"""
        engine = getattr(self, 'engine', None)
        renderer = getattr(engine, 'renderer', None) if engine else None
        if renderer is None or not hasattr(renderer, 'set_video_denoise_window'):
            return
        if not (getattr(self, 'current_file_is_video', False)
                and getattr(self, 'current_file_path', None)):
            return
        video_controller = getattr(self, 'video_controller', None)
        if video_controller is None:
            return
        is_playing_fn = getattr(self, '_is_video_playing', None)
        if (callable(is_playing_fn) and is_playing_fn()) or bool(
            getattr(video_controller, 'is_playing', False)
        ):
            return
        if payload and payload.get('path') != getattr(self, 'current_file_path'):
            return  # 旧素材的在途请求，丢弃

        try:
            renderer.set_video_denoise_window(payload or {})
        except Exception:
            logger.exception("submit video denoise window failed")
            return
        if payload:
            # 暂停帧用完整邻帧窗口重新降噪一遍
            request_render = getattr(self, 'request_render', None)
            if callable(request_render):
                request_render()


    def on_video_last_frame(self) -> None:
        """处理跳到最后一帧"""
        timeline_controller = self._get_timeline_composite_playback_controller()
        if timeline_controller is not None:
            timeline_controller.pause()
            timeline_controller.seek(max(0, timeline_controller.timeline_duration - 1))
            return
        if self._color_timeline_playback_controller() is not None:
            total = self.canvas.video_control_bar.total_frames
            self.on_video_seek(max(0, int(total or 1) - 1))
            return
        if self.video_controller:
            total = self._color_timeline_clip_total_frames()
            if total is not None:
                self.on_video_seek(max(0, total - 1))
                return
            self.video_controller.last_frame()
            self._schedule_video_denoise_window()

    def on_video_loop_toggled(self, enabled: bool) -> None:
        """Loop the selected Color clip only when the user explicitly asks."""
        if self._color_timeline_playback_controller() is not None:
            self._configure_color_timeline_playback_range()
            controller = self._color_timeline_playback_controller()
            if controller is not None:
                self._on_color_timeline_playhead_changed(
                    int(getattr(controller, "current_frame", 0) or 0)
                )
            return
        if self.video_controller is not None:
            self.video_controller.loop_playback = bool(enabled)

    # ── In/Out 播放范围（Composite 页专用，循环播放该区间）──────────────────
    def _composite_io_bar(self):
        """Return the Composite viewer video control bar, or None."""
        if (self._composite_available and self.composite_viewer
                and getattr(self.composite_viewer, 'video_control_bar', None)):
            return self.composite_viewer.video_control_bar
        return None

    def _sync_video_controller_io_range(self) -> None:
        """把页面工作区（clip 本地帧）同步给 video_controller（源帧）。

        I/O 范围的事实来源是 CompositePage/Keyframes 面板工作区；
        video_controller 只负责视频驱动的循环播放，所以它的范围要跟随
        页面工作区换算成源帧后更新。
        """
        page = getattr(self, 'composite_page', None)
        if page is None or not hasattr(page, 'play_range_marks'):
            return
        if not self.video_controller:
            return
        try:
            pin, pout = page.play_range_marks()
        except Exception:
            return
        try:
            if pin is None or pout is None:
                self.video_controller.clear_in_out_range()
            else:
                src_in = self._color_local_frame_to_source_frame(int(pin))
                src_out = self._color_local_frame_to_source_frame(int(pout))
                self.video_controller.set_in_out_range(src_in, src_out)
        except Exception:
            pass

    def on_composite_mark_in(self) -> None:
        """Set the In point to the current frame (synced with the Keyframes
        panel work area)."""
        page = getattr(self, 'composite_page', None)
        if page is not None and hasattr(page, 'mark_play_in'):
            page.mark_play_in()
            self._sync_video_controller_io_range()
            return
        # 无页面时的旧路径（仅 video_controller）。
        if not self.video_controller:
            return
        cur = getattr(self.video_controller, 'current_frame', 0)
        out = getattr(self.video_controller, 'out_frame', None)
        self.video_controller.set_in_out_range(cur, out)
        bar = self._composite_io_bar()
        if bar is not None:
            bar.set_in_out_points(cur, out)

    def on_composite_mark_out(self) -> None:
        """Set the Out point to the current frame (synced with the Keyframes
        panel work area)."""
        page = getattr(self, 'composite_page', None)
        if page is not None and hasattr(page, 'mark_play_out'):
            page.mark_play_out()
            self._sync_video_controller_io_range()
            return
        if not self.video_controller:
            return
        cur = getattr(self.video_controller, 'current_frame', 0)
        in_f = getattr(self.video_controller, 'in_frame', 0)
        self.video_controller.set_in_out_range(in_f, cur)
        bar = self._composite_io_bar()
        if bar is not None:
            bar.set_in_out_points(in_f, cur)

    def on_composite_clear_in_out(self) -> None:
        """Clear the In/Out playback range (synced with the Keyframes panel
        work area)."""
        page = getattr(self, 'composite_page', None)
        if page is not None and hasattr(page, 'clear_play_range_marks'):
            page.clear_play_range_marks()
            self._sync_video_controller_io_range()
            return
        if not self.video_controller:
            return
        self.video_controller.clear_in_out_range()
        bar = self._composite_io_bar()
        if bar is not None:
            bar.set_in_out_points(None, None)

    def on_video_frame_ready(self) -> None:
        """视频帧准备好渲染时更新 Canvas

        播放期间跳过 update_canvas()：视频帧已通过 RENDER_VIDEO_FRAME
        专用路径提交到 GPU，无需再走完整调色管线重渲染。
        暂停/停止时仍触发 update_canvas 以显示高质量渲染结果。

        Safety net: 如果播放期间超过 500ms 没有 canvas 更新，
        只做轻量级 viewport().update() 重绘 + 状态提示，
        不走 update_canvas / set_image 完整管线。
        """
        if self.video_controller and self.video_controller.is_playing:
            if getattr(self, "current_page", None) != "Color":
                self._schedule_video_controller_monitor_frame(reason="playback")
            # Safety net - 如果 RENDER_VIDEO_FRAME 路径停滞，
            # 只做轻量级重绘，避免注入完整 image render 管线。
            now = time.perf_counter()
            last_update = getattr(self, '_last_video_canvas_update', 0.0)
            if now - last_update > 0.5:
                logger.debug("on_video_frame_ready: safety net - lightweight repaint during playback")
                # Lightweight repaint only — do NOT call update_canvas or set_image
                # as that would inject the full image render pipeline mid-playback.
                self.canvas.viewport().update()
                self.status_bar.showMessage("视频帧渲染延迟，正在重试...", 1000)
            # Mark every frame_ready received during playback so the safety net
            # only fires on a genuine >0.5s stall, not after 0.5s of healthy
            # playback.  Previously this timestamp was only written inside the
            # safety-net branch, so the message appeared on every playback
            # after half a second regardless of whether rendering had stalled.
            self._last_video_canvas_update = now
            return
        if (
            self.video_controller
            and getattr(self.video_controller, "_last_frame_ready_direct_render", False)
        ):
            # Seek/clip-switch frames are already submitted through the video
            # renderer path. Running update_canvas() here would re-render the
            # stale static image input and overwrite the just-selected video
            # frame, making Timeline Clips appear stuck on the first shot.
            self.video_controller._last_frame_ready_direct_render = False
            self._last_video_canvas_update = time.perf_counter()
            self.canvas.viewport().update()
            submit_monitor = getattr(self, "_submit_video_controller_monitor_frame", None)
            if callable(submit_monitor):
                submit_monitor(reason="seek")
            return
        self._last_video_canvas_update = time.perf_counter()
        self.update_canvas(is_new_source=False)
        submit_monitor = getattr(self, "_submit_video_controller_monitor_frame", None)
        if callable(submit_monitor):
            submit_monitor(reason="static")

    def _on_video_frame_changed_for_composite(self, frame: int) -> None:
        """视频帧变化时更新合成页面（播放期间重新渲染合成图）。

        当视频在合成页面播放时，需要：
        1. 更新 runtime_context 的 source_frame 到当前帧
        2. 节流触发合成图重新求值（~10fps），避免渲染队列积压导致
           所有结果被 stale 检测丢弃。
        """
        import logging as _logging
        _logger = _logging.getLogger(__name__)

        if not self._composite_available or not self.composite_page:
            return
        # A timeline-backed Composite page is driven exclusively by
        # EditPlaybackController. Do not let the legacy Color video player
        # submit a second, source-local frame stream into the same graph.
        if getattr(self.composite_page, "_runtime_context", None) is not None:
            return
        if not self.composite_page._is_active:
            _logger.debug(f"[COMP-PLAY] frame_changed skipped: not active, frame={frame}")
            return
        # 节流：自适应合成求值频率
        # 轻量图可以接近源 fps，重量图限制到 ~10fps 防止积压
        import time as _time
        now = _time.monotonic()
        # Adapt interval based on recent eval time
        last_eval_time = getattr(self, '_last_composite_eval_duration', 0.0)
        if last_eval_time > 0.050:  # >50ms per eval
            min_interval = 0.10  # Heavy graph: ~10fps
        elif last_eval_time > 0.020:  # >20ms per eval
            min_interval = 0.05  # Medium graph: ~20fps
        else:
            min_interval = 0.033  # Light graph: ~30fps
        last_eval = getattr(self, '_last_composite_eval_time', 0.0)
        if now - last_eval < min_interval:
            return
        self._last_composite_eval_time = now
        eval_start = _time.monotonic()
        has_ctx = self.composite_page._runtime_context is not None
        _logger.info(
            f"[COMP-PLAY] eval graph: frame={frame}, has_ctx={has_ctx}, "
            f"renderer={self.composite_page._renderer is not None}"
        )
        if has_ctx:
            from dataclasses import replace
            runtime_context = replace(
                self.composite_page._runtime_context,
                source_frame=frame,
            )
            self.composite_page.set_timeline_clip_context(
                runtime_context=runtime_context,
                gpu_provider=getattr(self, '_timeline_clip_frame_provider', None),
            )
        else:
            self.composite_page._evaluate_graph()
        # Track eval duration for adaptive throttling
        self._last_composite_eval_duration = _time.monotonic() - eval_start

    def _on_video_play_state_for_composite(self, is_playing: bool) -> None:
        """视频播放状态变化时管理合成页面的缩略图更新。

        播放时挂起缩略图更新以避免 GPU 回读与实时渲染竞争，
        暂停时恢复缩略图更新。
        """
        if not self._composite_available or not self.composite_page:
            return
        self.composite_page.set_thumbnail_playback_suspended(is_playing)
        if is_playing:
            # 播放开始时重置节流计时器，确保第一帧立即求值
            self._last_composite_eval_time = 0.0
        else:
            if self.composite_page._is_active:
                # 暂停后立即重新渲染一帧以获得高质量结果
                self.composite_page._evaluate_graph()

    def _load_video_project(self, video_path: str) -> None:
        """
        加载视频对应的项目文件（.topos）

        Args:
            video_path: 视频文件路径
        """
        # 尝试加载项目文件
        video_topos = self.save_router._get_project_path_for_file(video_path)
        project_data = self.save_router.load_single_asset_data(video_topos) if video_topos else None

        if project_data:
            logger.info(f"加载视频项目文件成功: {video_path}")

            # 恢复图层数据
            if 'layers' in project_data:
                layers = project_data['layers']
                if isinstance(layers, list) and len(layers) > 0:
                    self.engine.layers = layers
                    self.engine.active_layer_index = 0
                    self.layer_panel.update_layers(self.engine.layers, 0, is_offline_mode=False)
                    self.engine.sync_all_resources()
                    self.update_from_layer(self.engine.get_active_layer())
                    if self.engine.get_active_layer():
                        self.mask_panel.update_shapes_list(self.engine.get_active_layer().mask_shapes)

            # 恢复 RAW 参数（如果有）
            if 'raw_params' in project_data and project_data['raw_params']:
                self.raw_panel.set_params(project_data['raw_params'])

            # M5: 恢复视频降噪参数（时域+空域，非破坏）
            self.engine.set_video_stabilization_state(project_data.get('video_stabilization') or {})
            video_denoise = project_data.get('video_denoise')
            if video_denoise:
                self.engine.set_video_denoise_state(video_denoise)
                if hasattr(self.denoise_panel, 'set_video_params_dict'):
                    self.denoise_panel.set_video_params_dict(video_denoise)

            # 更新 Canvas
            self.update_canvas(is_new_source=True)
            self.request_render()

            self.status_bar.showMessage(
                f"已加载视频项目: {os.path.basename(video_path)}",
                3000
            )
        else:
            logger.info(f"没有找到视频项目文件: {video_path}")

    def unload_video(self, *, save_before_unload: bool = True) -> None:
        """卸载当前视频, optionally saving the active project first."""
        if self.video_controller:
            # M4.2: 作废在途邻帧窗口请求并清掉渲染器侧窗口
            provider_obj = getattr(self, '_video_denoise_window_provider_obj', None)
            if provider_obj is not None:
                provider_obj.clear()
            engine = getattr(self, 'engine', None)
            renderer = getattr(engine, 'renderer', None) if engine else None
            if renderer is not None and hasattr(renderer, 'set_video_denoise_window'):
                try:
                    renderer.set_video_denoise_window({})
                except Exception:
                    pass
            # 保存当前视频的项目文件（如果有）
            if (
                save_before_unload
                and self.current_file_path
                and self.current_file_is_video
                and self.video_controller.is_video_loaded()
            ):
                self.save_router.save()

            self.video_controller.unload_video()
            service = getattr(self, 'stabilization_service', None)
            if service is not None:
                self._cancel_stabilization()
                self._stabilizer_params_timer.stop()
            self.engine.set_video_stabilization_state({})
            # 隐藏 Canvas 上的视频控制栏
            self.canvas.video_control_bar.hide_for_image()
            # 断开信号连接
            try:
                self.canvas.video_control_bar.seek_requested.disconnect(self.on_video_seek)
                self.canvas.video_control_bar.scrub_started.disconnect(self.on_video_scrub_started)
                self.canvas.video_control_bar.scrub_moved.disconnect(self.on_video_scrub_moved)
                self.canvas.video_control_bar.scrub_finished.disconnect(self.on_video_scrub_finished)
                self.canvas.video_control_bar.play_toggled.disconnect(self.on_video_play_toggled)
                self.canvas.video_control_bar.next_frame.disconnect(self.on_video_next_frame)
                self.canvas.video_control_bar.prev_frame.disconnect(self.on_video_prev_frame)
                self.canvas.video_control_bar.first_frame.disconnect(self.on_video_first_frame)
                self.canvas.video_control_bar.last_frame.disconnect(self.on_video_last_frame)
            except TypeError:
                pass  # 信号没有连接，忽略

            # 隐藏 CompositeViewer 上的视频控制栏（如果可用）
            if self._composite_available and self.composite_viewer:
                self.composite_viewer.video_control_bar.hide_for_image()
                try:
                    self.composite_viewer.video_control_bar.seek_requested.disconnect(self.on_video_seek)
                    self.composite_viewer.video_control_bar.scrub_started.disconnect(self.on_video_scrub_started)
                    self.composite_viewer.video_control_bar.scrub_moved.disconnect(self.on_video_scrub_moved)
                    self.composite_viewer.video_control_bar.scrub_finished.disconnect(self.on_video_scrub_finished)
                    self.composite_viewer.video_control_bar.play_toggled.disconnect(self.on_video_play_toggled)
                    self.composite_viewer.video_control_bar.next_frame.disconnect(self.on_video_next_frame)
                    self.composite_viewer.video_control_bar.prev_frame.disconnect(self.on_video_prev_frame)
                    self.composite_viewer.video_control_bar.first_frame.disconnect(self.on_video_first_frame)
                    self.composite_viewer.video_control_bar.last_frame.disconnect(self.on_video_last_frame)
                except TypeError:
                    pass  # 信号没有连接，忽略

            # 清除当前文件状态
            self.current_file_path = None
            self.current_file_is_video = False
            self._clear_color_timeline_clip_playback_context()

    # ------------------------------------------------------------------
    # Qt Event overrides
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        # In test_mode, only clean up essential resources (no panels were created)
        if getattr(self, '_test_mode', False):
            # Stop engine worker threads to prevent crash during GC
            if hasattr(self, 'engine') and self.engine:
                if hasattr(self.engine, 'renderer') and self.engine.renderer:
                    _stop_gpu_renderer_deferred(self.engine.renderer)
                if hasattr(self.engine, 'shutdown_worker_threads'):
                    self.engine.shutdown_worker_threads(timeout_ms=3000)
            # Stop timers
            if hasattr(self, '_backup_timer'):
                self._backup_timer.stop()
            if hasattr(self, '_periodic_save_timer'):
                self._periodic_save_timer.stop()
            # AudioPlaybackManager owns QThread children.  It must be fully
            # cleaned before Qt destroys the EditPage widget tree.
            if hasattr(self, 'edit_page') and self.edit_page is not None:
                try:
                    self.edit_page.shutdown_audio_playback()
                except Exception:
                    logger.exception("Failed to clean up edit-page audio playback")
            # Stop Source decode worker and GPU renderer
            if hasattr(self, '_source_decode_worker') and self._source_decode_worker is not None:
                try:
                    self._source_decode_worker.stop()
                except Exception:
                    pass
            if hasattr(self, '_source_gpu_renderer') and self._source_gpu_renderer is not None:
                _stop_gpu_renderer_deferred(self._source_gpu_renderer, 1000)
            QMainWindow.closeEvent(self, event)
            return

        # Prompt to save unsaved changes before closing (Ctrl+S route already
        # handles "save as" for untitled projects). Mirrors the explicit
        # close-project dialog in edit_controller._close_current_project.
        if not self._confirm_save_before_close():
            event.ignore()
            return

        # AudioPlaybackManager is parented below EditPage and contains live
        # QThread children (playback + scrub preview).  Join them explicitly
        # before Qt starts deleting the MainWindow/child QWidget hierarchy;
        # otherwise QThread::~QThread() aborts the process on macOS.
        if hasattr(self, 'edit_page') and self.edit_page is not None:
            try:
                self.edit_page.shutdown_audio_playback()
            except Exception:
                logger.exception("Failed to clean up edit-page audio playback")

        # Cleanup Composite page if active
        if self._composite_adapter:
            self._composite_adapter.runtime.cleanup()
        elif self._composite_available and self.composite_page is not None:
            self.composite_page.cleanup()

        # VP-B11: the Source widget must enter its shutdown sequence BEFORE
        # the decode worker / GPU renderer stop — otherwise its playback
        # timer, watchdog, displayed present slot (never acked -> pool
        # exhaustion) and the scrub-audio QThread (parented to the widget,
        # QThread::~QThread abort risk) all outlive this teardown.
        source_widget = getattr(self, 'source_viewer_widget', None)
        if source_widget is not None:
            try:
                source_widget.prepare_for_shutdown()
            except RuntimeError:
                pass  # C++ side already destroyed
            except AttributeError:
                pass  # widget without the B11 entry point

        # Stop Source decode worker thread, then the GPU renderer thread
        if hasattr(self, '_source_decode_worker') and self._source_decode_worker is not None:
            try:
                self._source_decode_worker.stop()
            except Exception:
                pass
        if hasattr(self, '_source_gpu_renderer') and self._source_gpu_renderer is not None:
            _stop_gpu_renderer_deferred(self._source_gpu_renderer, 2000)

        # Stop TimelinePreviewRenderer threads (worker + GPU renderer)
        if hasattr(self, 'timeline_preview_renderer') and self.timeline_preview_renderer is not None:
            try:
                self.timeline_preview_renderer.shutdown()
            except Exception:
                pass

        # Color owns a second, CPU-only timeline preview worker while that
        # page is active.  It must be joined before Qt destroys Canvas.
        try:
            self._deactivate_color_timeline_preview()
        except (AttributeError, RuntimeError):
            pass

        # FilesPanel will stop its own server in its closeEvent
        # No need to call it here to avoid double-stopping

        # Update current page layout in the dictionary before saving
        self.page_layouts[self.current_page] = self.saveState()

        # Phase A: Also save to LayoutStore for current page
        if hasattr(self, '_save_current_layout_to_store'):
            self._save_current_layout_to_store(self.current_page)

        # Save all page layouts to config, scoped by effective layout mode so
        # image/video/all work modes keep independent page layouts (batch).
        layouts_hex = {}
        for page, state in self.page_layouts.items():
            if isinstance(state, QByteArray):
                layouts_hex[page] = state.toHex().data().decode()
        try:
            from src.shared.feature_flags import get_effective_layout_mode
            elm = get_effective_layout_mode(self.config_manager)
        except Exception:
            elm = "studio"
        stored = self.config_manager.get("page_layouts") or {}
        # Backwards compat: if stored is a legacy flat dict {page: hex}, fold
        # it under the default mode bucket before merging the current mode.
        if stored and all(isinstance(v, str) for v in stored.values()):
            stored = {"all": stored}
        if not isinstance(stored, dict):
            stored = {}
        stored[elm] = layouts_hex

        self.config_manager.set("page_layouts", stored, auto_save=False)
        self.config_manager.set("current_page", self.current_page, auto_save=False)
        # Persist a work-mode-scoped current page too, so image/video each
        # remember their own last page.
        try:
            from src.shared.feature_flags import get_effective_layout_mode
            elm_page = get_effective_layout_mode(self.config_manager)
            self.config_manager.set(f"current_page_{elm_page}", self.current_page, auto_save=False)
        except Exception:
            pass

        # Save geometry and state to config.json (batch update, no auto-save)
        self.config_manager.set("window_geometry", self.saveGeometry().toHex().data().decode(), auto_save=False)
        self.config_manager.set("window_state", self.saveState().toHex().data().decode(), auto_save=False)

        # Save floating dock sizes and positions
        for dock in self.all_docks:
            obj_name = dock.objectName()
            if obj_name:
                if dock.isFloating():
                    # Save floating dock size and position
                    dock_size_key = f"dock_{obj_name}_floating_size"
                    dock_pos_key = f"dock_{obj_name}_floating_pos"
                    self.config_manager.set(dock_size_key, f"{dock.width()}x{dock.height()}", auto_save=False)
                    self.config_manager.set(dock_pos_key, f"{dock.x()},{dock.y()}", auto_save=False)
                else:
                    # Save docked dock size for tabified docks
                    dock_docked_size_key = f"dock_{obj_name}_docked_size"
                    self.config_manager.set(dock_docked_size_key, f"{dock.width()}x{dock.height()}", auto_save=False)

        # Save last opened file, scoped per work mode so image/video workflows
        # each remember their own last file (and image mode never tries to
        # restore a video file on next launch).
        if self.current_file_path:
            try:
                from src.shared.feature_flags import get_effective_layout_mode
                elm = get_effective_layout_mode(self.config_manager)
            except Exception:
                elm = "studio"
            self.config_manager.set("last_opened_file", self.current_file_path, auto_save=False)
            self.config_manager.set(f"last_opened_file_{elm}", self.current_file_path, auto_save=False)

        # Persist any active batch export queue (so it can be restored next launch).
        # 渲染过程中每个文件都会实时持久化，这里只做兜底，防止极端情况遗漏。
        try:
            if hasattr(self, '_persist_batch_queue_on_close'):
                self._persist_batch_queue_on_close()
        except Exception as e:
            logger.warning(f"closeEvent: persist batch queue failed: {e}")

        self._persist_active_project_before_close()

        # Cleanup orphaned baked cache files
        self._cleanup_baked_cache()

        # Stop timers
        self._backup_timer.stop()
        if hasattr(self, "_periodic_save_timer"):
            self._periodic_save_timer.stop()
        if getattr(self, "edit_project_io", None) is not None:
            self.edit_project_io.cleanup()

        # Temp file cleanup on exit
        if self.config_manager.get("cleanup_temp_on_exit"):
            max_age = self.config_manager.get("temp_file_max_age_days") or 7
            size_limit = self.config_manager.get("cache_size_limit_gb") or 0
            FileIO.cleanup_old_cache(max_age_days=max_age, max_total_size_gb=size_limit)

        # Single save operation for all config changes
        self.config_manager.save_config()

        # Legacy database save for compatibility
        self.db.set_setting("last_geometry", self.saveGeometry().toHex().data().decode())
        self.db.set_setting("last_state", self.saveState().toHex().data().decode())

        # Stop background threads
        if hasattr(self, 'engine') and self.engine:
            if self.engine.renderer:
                # P1-THREAD-01：检查 stop() 返回值，超时推迟销毁而非放任
                # 存活线程被析构
                _stop_gpu_renderer_deferred(self.engine.renderer)
            if hasattr(self.engine, 'shutdown_worker_threads'):
                self.engine.shutdown_worker_threads(timeout_ms=3000)

        QMainWindow.closeEvent(self, event)

    def _persist_active_project_before_close(self) -> None:
        """Persist the active document before shutdown cleanup starts.

        Closing must save regardless of whether unrelated batch-queue
        persistence succeeded.  Keeping this as a separate helper also makes
        the ordering explicit: flush Composite state, save the page-routed
        project, then remove the recovery snapshot.

        AUD-2800: 用户在关闭确认中明确选择 Discard 时跳过主工程保存——
        否则被丢弃的脏状态仍会经 save_router.save() 写回主工程文件，
        「不保存并关闭」语义失效。崩溃恢复语义不受影响（Discard 分支
        之前已经历 _confirm_save_before_close 的显式确认）。
        """
        try:
            if hasattr(self, '_flush_composite_to_edit_before_leave'):
                self._flush_composite_to_edit_before_leave()
        except Exception as exc:  # noqa: BLE001
            logger.warning("closeEvent: composite flush failed: %s", exc)

        if getattr(self, '_user_discarded_changes', False):
            logger.info(
                "closeEvent: skipping project save (user chose Discard)")
            return

        try:
            self.save_router.save()
        except Exception as exc:  # noqa: BLE001
            # Shutdown continues so resources and a valid recovery file can
            # still be handled deterministically; the failure remains logged.
            logger.exception("closeEvent: project save failed: %s", exc)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        files = [u.toLocalFile() for u in event.mimeData().urls()]
        if files:
            # AUD-2801: 拖入文件此前一律走 load_image——视频被塞进图像
            # 加载路径（解不出帧 → 误导性"找不到文件"错误框），非媒体
            # 文件同样误报。统一改走 _open_media_path 路由，与菜单
            # Open Media 的视频/图像/邻接工程分发保持一致。
            self._open_media_path(files[0])

    def resizeEvent(self, event) -> None:
        """主窗口大小改变时，更新播放器大小"""
        QMainWindow.resizeEvent(self, event)
        # 当主窗口大小改变时，确保播放器也更新大小
        files_panel = self.files_panel
        if not files_panel:
            return
        for attr in ('_topos_video_player', '_libav_video_player', 'video_player'):
            p = getattr(self, attr, None)
            if p is not None and p.isVisible():
                p.setGeometry(files_panel.rect())

    # ------------------------------------------------------------------
    # Auto-save trigger
    # ------------------------------------------------------------------

    def _trigger_autosave(self) -> None:
        """通过 SaveRouter 触发延迟自动保存（2s 去抖）。"""
        if not self.current_file_path and not self._current_edit_project_filepath:
            return
        if not hasattr(self, '_autosave_router_timer'):
            self._autosave_router_timer = QTimer(self)
            self._autosave_router_timer.setSingleShot(True)
            self._autosave_router_timer.timeout.connect(lambda: self.save_router.save())
        self._autosave_router_timer.start(2000)

    # ------------------------------------------------------------------
    # P1-16: FCPXML/OTIO 互操作导入/导出（受控子集 + 映射报告）
    # ------------------------------------------------------------------

    def import_interchange_timeline(self) -> None:
        """File 菜单入口：从 FCPXML/OTIO 导入时间线（preview → confirm → commit）。"""
        from src.features.edit.adapters.fcpxml_adapter import FCPXMLAdapter
        from src.features.edit.adapters.interchange_types import InterchangeReport
        from src.features.edit.adapters.otio_adapter import OTIOAdapter, otio_available
        from src.features.edit.ui.interchange_dialog import InterchangeReportDialog

        path_sel, _ = QFileDialog.getOpenFileName(
            self,
            I18N.tr("edit.interchange.import_title", "Import Timeline (FCPXML/OTIO)…"),
            "",
            "FCPXML (*.fcpxml *.xml);;OpenTimelineIO (*.otio)",
        )
        if not path_sel:
            return

        suffix = os.path.splitext(path_sel)[1].lower()
        if suffix == ".otio":
            if not otio_available():
                QMessageBox.warning(
                    self,
                    I18N.tr("edit.interchange.otio_unavailable", "OTIO Unavailable"),
                    I18N.tr(
                        "edit.interchange.otio_missing",
                        "opentimelineio is not installed. "
                        "Install it (pip install opentimelineio) to enable OTIO exchange.",
                    ),
                )
                return
            adapter = OTIOAdapter()
        else:
            adapter = FCPXMLAdapter()

        inspection = adapter.inspect(path_sel)
        if not inspection.supported:
            InterchangeReportDialog(
                inspection.report
                or InterchangeReport(format_id=adapter.format_id),
                title=I18N.tr("edit.interchange.unsupported", "Unsupported File"),
                parent=self,
            ).exec()
            return

        result = adapter.import_to_canonical(path_sel)
        if not result.sequences:
            QMessageBox.information(
                self,
                I18N.tr("edit.interchange.import_title", "Import Timeline (FCPXML/OTIO)…"),
                I18N.tr(
                    "edit.interchange.no_timeline",
                    "No importable timeline found in this file.",
                ),
            )
            InterchangeReportDialog(result.report, parent=self).exec()
            return

        # AUD-12613：逐素材 os.path.exists 磁盘探测移出 UI 线程——行数据
        # 经排队信号回 UI 线程后再弹出确认预览。
        rows_worker = _InterchangeRowsWorker(result, self)
        rows_worker.signals.ready.connect(self._on_interchange_rows_ready)
        rows_worker.signals.failed.connect(self._on_interchange_rows_failed)
        QThreadPool.globalInstance().start(rows_worker)

    def _on_interchange_rows_ready(self, result: Any, rows: list) -> None:
        """AUD-12613：行探测完成（UI 线程）——弹出导入确认预览。"""
        from src.features.edit.ui.interchange_dialog import InterchangeReportDialog

        preview = InterchangeReportDialog(
            result.report,
            title=I18N.tr("edit.interchange.preview_title", "Import Preview"),
            asset_rows=rows,
            confirm_mode=True,
            parent=self,
        )
        if preview.exec() != QDialog.DialogCode.Accepted:
            return
        self._apply_interchange_import(result)

    def _on_interchange_rows_failed(self, result: Any, error: str) -> None:
        """AUD-12613：行探测失败（UI 线程）——显式报错，不静默。"""
        QMessageBox.critical(
            self,
            I18N.tr("edit.interchange.import_title", "Import Timeline (FCPXML/OTIO)…"),
            I18N.tr(
                "edit.interchange.probe_failed",
                "Failed to probe import assets: {err}",
            ).format(err=error),
        )

    def _apply_interchange_import(self, result: Any) -> None:
        """把确认后的 ImportResult 落地为当前工程内容（素材 + 时间线）。

        AUD-12613：逐素材 ``os.path.exists`` + ``probe_metadata`` 阻塞探测
        在工作线程执行（``_InterchangeBuildWorker``），完成/失败经排队信号
        回 UI 线程应用/报错；``_interchange_import_inflight`` 防重入。
        """
        if getattr(self, "_interchange_import_inflight", False):
            logger.warning("interchange import already in flight; ignoring")
            return
        if not self._ensure_edit_project():
            return
        self._interchange_import_inflight = True
        build_worker = _InterchangeBuildWorker(
            self._current_edit_project, result, self)
        build_worker.signals.ready.connect(self._on_interchange_build_ready)
        build_worker.signals.failed.connect(self._on_interchange_build_failed)
        QThreadPool.globalInstance().start(build_worker)

    def _on_interchange_build_failed(self, result: Any, error: str) -> None:
        """AUD-12613：落地构建失败（UI 线程）——显式报错并解除占用。"""
        self._interchange_import_inflight = False
        QMessageBox.critical(
            self,
            I18N.tr("edit.interchange.import_done", "Import Completed"),
            I18N.tr(
                "edit.interchange.build_failed",
                "Import failed while building the project: {err}",
            ).format(err=error),
        )

    def _on_interchange_build_ready(
        self, result: Any, new_project: Any, sequences: Any, summary: Any,
    ) -> None:
        """AUD-12613：落地构建完成（UI 线程）——应用素材 + 时间线。"""
        from src.features.edit.ui.interchange_dialog import InterchangeReportDialog

        self._interchange_import_inflight = False

        # 先注册完整 TimelineSequence（sync 只补缺失 id，不会覆盖已有），
        # 再走与媒体池导入相同的同步路径。
        self._current_timeline_sequences.update(sequences)
        self.media_pool_panel.set_project(new_project)
        self._on_media_pool_project_changed(new_project)

        # 切到新导入的时间线
        if summary.sequence_ids:
            self._on_timeline_switched(summary.sequence_ids[0])

        parts = [
            I18N.tr("edit.interchange.summary_imported", "New assets: {n}").format(
                n=len(summary.imported_asset_ids)
            ),
            I18N.tr("edit.interchange.summary_reused", "Reused: {n}").format(
                n=len(summary.reused_asset_ids)
            ),
            I18N.tr("edit.interchange.summary_offline", "Offline (missing): {n}").format(
                n=len(summary.offline_assets)
            ),
            I18N.tr("edit.interchange.summary_sequences", "Timelines: {n}").format(
                n=len(summary.sequence_ids)
            ),
        ]
        if summary.unmapped_clip_ids:
            parts.append(
                I18N.tr(
                    "edit.interchange.summary_unmapped",
                    "Unmapped clips (kept as-is): {n}",
                ).format(n=len(summary.unmapped_clip_ids))
            )
        InterchangeReportDialog(
            result.report,
            title=I18N.tr("edit.interchange.import_done", "Import Completed"),
            summary_text="  ·  ".join(parts),
            parent=self,
        ).exec()

    def export_timeline_fcpxml(self) -> None:
        """File 菜单入口：把当前时间线导出为 FCPXML。"""
        from src.features.edit.adapters.fcpxml_adapter import FCPXMLAdapter

        self._export_interchange(FCPXMLAdapter(), ".fcpxml")

    def export_timeline_otio(self) -> None:
        """File 菜单入口：把当前时间线导出为 OTIO（需 opentimelineio）。"""
        from src.features.edit.adapters.otio_adapter import OTIOAdapter, otio_available

        if not otio_available():
            QMessageBox.warning(
                self,
                I18N.tr("edit.interchange.otio_unavailable", "OTIO Unavailable"),
                I18N.tr(
                    "edit.interchange.otio_missing",
                    "opentimelineio is not installed. "
                    "Install it (pip install opentimelineio) to enable OTIO exchange.",
                ),
            )
            return
        self._export_interchange(OTIOAdapter(), ".otio")

    def _active_interchange_sequence(self) -> Any:
        """当前活动时间线序列（活动 id 优先，唯一序列兜底）。"""
        project = self._current_edit_project
        if project is None:
            return None
        active_id = project.active_timeline_id
        sequences = getattr(self, "_current_timeline_sequences", None) or {}
        sequence = sequences.get(active_id) if active_id else None
        if sequence is None and len(sequences) == 1:
            sequence = next(iter(sequences.values()))
        return sequence

    def export_timeline_subtitles(self) -> None:
        """File 菜单入口（P1-21）：序列级批量导出字幕语言轨 + manifest。"""
        from src.features.edit.core.captions.delivery import (
            export_subtitle_tracks,
            list_subtitle_tracks,
        )
        from src.features.edit.ui.subtitle_delivery_dialog import (
            SubtitleDeliveryDialog,
        )

        sequence = self._active_interchange_sequence()
        if sequence is None:
            QMessageBox.information(
                self,
                I18N.tr("edit.subtitle_delivery.title", "导出字幕轨（序列级）"),
                I18N.tr(
                    "edit.subtitle_delivery.no_timeline",
                    "没有可导出的活动时间线。",
                ),
            )
            return

        summaries = list_subtitle_tracks(sequence)
        if not summaries:
            QMessageBox.information(
                self,
                I18N.tr("edit.subtitle_delivery.title", "导出字幕轨（序列级）"),
                I18N.tr(
                    "edit.subtitle_delivery.no_tracks",
                    "当前时间线没有字幕轨。",
                ),
            )
            return

        dialog = SubtitleDeliveryDialog(summaries, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        fmt, track_ids = dialog.values()
        if not track_ids:
            QMessageBox.information(
                self,
                I18N.tr("edit.subtitle_delivery.title", "导出字幕轨（序列级）"),
                I18N.tr("edit.subtitle_delivery.none_selected", "未选择任何字幕轨。"),
            )
            return

        target = QFileDialog.getExistingDirectory(
            self,
            I18N.tr("edit.subtitle_delivery.target_title", "选择字幕输出目录"),
            os.path.expanduser("~"),
        )
        if not target:
            return

        try:
            report = export_subtitle_tracks(
                sequence, output_dir=target, format_name=fmt,
                track_ids=track_ids,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Subtitle delivery failed")
            QMessageBox.critical(
                self,
                I18N.tr("edit.media_pool.error", "Error"),
                str(exc),
            )
            return

        lines = [
            I18N.tr(
                "edit.subtitle_delivery.done",
                "已写入 {n} 个字幕文件到:\n{dir}",
                n=len(report.written), dir=report.output_dir,
            ),
        ]
        for f in report.written:
            lines.append(
                f"  · {f.file_name}  [{f.summary.language}] {f.summary.role}"
            )
        skipped = [f for f in report.files if f.status == "skipped_empty"]
        if skipped:
            lines.append(
                I18N.tr(
                    "edit.subtitle_delivery.skipped",
                    "\n跳过 {n} 个空轨道",
                    n=len(skipped),
                )
            )
        if report.failed:
            lines.append(
                I18N.tr(
                    "edit.subtitle_delivery.failed",
                    "\n{n} 个轨道导出失败（见日志）！",
                    n=len(report.failed),
                )
            )
            for f in report.failed[:5]:
                lines.append(f"  · {f.summary.label}: {f.error}")
        if report.manifest_path:
            lines.append(
                I18N.tr(
                    "edit.subtitle_delivery.manifest",
                    "\n清单: {path}",
                    path=report.manifest_path,
                )
            )
        box = QMessageBox.warning if not report.passed else QMessageBox.information
        box(
            self,
            I18N.tr("edit.subtitle_delivery.title", "导出字幕轨（序列级）"),
            "\n".join(lines),
        )

    def export_stem_tracks(self) -> None:
        """File 菜单入口（B2）：Stem 分轨导出对话框。

        复用活动时间线；无音频轨时对话框自身呈现空态（只读关闭）。
        """
        from src.features.edit.ui.stem_export_dialog import StemExportDialog

        sequence = self._active_interchange_sequence()
        if sequence is None:
            QMessageBox.information(
                self,
                I18N.tr("edit.stem_export.title", "导出 Stem 分轨"),
                I18N.tr(
                    "edit.stem_export.no_timeline",
                    "没有可导出的活动时间线。",
                ),
            )
            return
        StemExportDialog(
            sequence, self._current_edit_project, parent=self,
            sequence_resolver=dict(getattr(self, "_current_timeline_sequences", {}) or {}).get,
        ).exec()

    def _export_interchange(self, adapter: Any, default_suffix: str) -> None:
        """导出当前活动时间线并展示映射报告（丢弃项显式列出）。"""
        from src.features.edit.ui.interchange_dialog import InterchangeReportDialog

        project = self._current_edit_project
        sequence = None
        if project is not None:
            active_id = project.active_timeline_id
            sequences = getattr(self, "_current_timeline_sequences", None) or {}
            sequence = sequences.get(active_id) if active_id else None
            if sequence is None and len(sequences) == 1:
                sequence = next(iter(sequences.values()))
        if sequence is None or not getattr(sequence, "video_tracks", ()):
            QMessageBox.information(
                self,
                I18N.tr("edit.interchange.export_title", "Export Timeline"),
                I18N.tr(
                    "edit.interchange.no_active_timeline",
                    "Open a timeline with clips first.",
                ),
            )
            return

        default_name = (sequence.name or "sequence") + default_suffix
        path_sel, _ = QFileDialog.getSaveFileName(
            self,
            I18N.tr("edit.interchange.export_title", "Export Timeline"),
            default_name,
            f"*{default_suffix}",
        )
        if not path_sel:
            return
        if os.path.splitext(path_sel)[1].lower() != default_suffix:
            path_sel += default_suffix

        try:
            result = adapter.export_from_canonical(
                sequence, options={"output_path": path_sel}
            )
        except Exception as exc:  # noqa: BLE001 — 导出失败必须给用户明确反馈
            logger.exception("Interchange export failed")
            QMessageBox.critical(
                self,
                I18N.tr("edit.interchange.export_title", "Export Timeline"),
                I18N.tr(
                    "edit.interchange.export_failed", "Export failed: {error}"
                ).format(error=exc),
            )
            return

        if result.report.dropped_count or result.report.requires_action:
            InterchangeReportDialog(
                result.report,
                title=I18N.tr("edit.interchange.export_done", "Export Completed"),
                summary_text=path_sel,
                parent=self,
            ).exec()
        else:
            QMessageBox.information(
                self,
                I18N.tr("edit.interchange.export_done", "Export Completed"),
                I18N.tr(
                    "edit.interchange.export_ok", "Exported to:\n{path}"
                ).format(path=path_sel),
            )
