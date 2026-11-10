"""
Media Pool Panel - Project-level media asset management.

This panel displays imported media assets, bins, and timeline sequences.
Provides file import via button click, drag-and-drop, and context menu.
Provides List, Thumbnail and Metadata presentations with shared selection.

Features:
- List View: File name | Type | Duration | Resolution | FPS | Codec
- Grid View: Large thumbnails with filename overlay
- View mode toggle buttons (list/grid)
- Thumbnail size selector (Small/Medium/Large)
- Asynchronous thumbnail loading with cache
- Drag-and-drop import support
- Context menu for asset actions
"""
from __future__ import annotations

import logging
import math
import os
import uuid
import platform
import subprocess
import json
from typing import Optional, List, Dict, Any
from enum import Enum, auto

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLabel, QHBoxLayout,
    QTreeWidget, QTreeWidgetItem, QFileDialog,
    QProgressDialog, QMessageBox, QMenu, QDialog, QDialogButtonBox,
    QFormLayout, QLineEdit, QListView, QToolButton, QComboBox,
    QAbstractItemView, QStyledItemDelegate, QStyle, QStyleOptionViewItem, QSlider, QFrame, QPushButton,
    QTreeWidgetItemIterator, QRubberBand, QSpinBox, QCheckBox,
    QDoubleSpinBox,
)
from PyQt6.QtCore import (
    Qt, pyqtSignal, QUrl, QTimer, QSize, QRect, QModelIndex, QPoint,
    QAbstractListModel, QSortFilterProxyModel, QMimeData, QByteArray,
    QThreadPool, QObject, QRunnable, pyqtSlot, QItemSelectionModel,
    QItemSelection,
)
from PyQt6.QtGui import (
    QDragEnterEvent, QDragMoveEvent, QDropEvent, QMouseEvent,
    QIcon, QPainter, QColor, QBrush, QPen, QPixmap, QFont,
    QLinearGradient, QDrag, QPolygon, QKeyEvent, QPainterPath, QKeySequence,
    QShortcut, QActionGroup, QFontMetrics,
)
import qtawesome as qta

from src.shared.ui.panel_splitter import PanelSplitter
from src.shared.ui.style_sheet import C
from .media_pool_icons import pool_icon
from src.shared.i18n.i18n import I18N
from src.shared.media.thumbnail_provider import ThumbnailProvider
from src.shared.assets.icon_manager import icon_grid, icon_list, icon_folder, icon_smart, icon_search, icon_arrow_up, icon_arrow_down, icon_warning, icon_video_track, icon_audio_track, icon_image
from src.features.edit.core.project import (
    EditProject, MediaAsset, ProjectBin, TimelineSequenceRef,
    probe_media_metadata,
    media_type_from_probe,
    update_offline_status,
    update_asset_proxy,
    ASSET_COLOR_LABELS,
    SmartBin,
    SMART_BIN_ALL_VIDEO,
    SMART_BIN_ALL_AUDIO,
    SMART_BIN_ALL_STILLS,
    SMART_BIN_OFFLINE_MEDIA,
    SMART_BIN_RECENT_IMPORTS,
)
from src.features.edit.core.timeline_v2 import (
    MEDIA_POOL_PREVIEW_ASSET_ID_KEY,
    MEDIA_POOL_PREVIEW_FRAME_KEY,
)
from src.features.edit.core.asset_usage_index import build_asset_usage_index
from src.features.edit.core.media_replacement import (
    merge_replacement_metadata, validate_replacement_usages,
)
from src.features.edit.core.project_archive import collect_project
from src.features.edit.core.media_management_worker import (
    KIND_COLLECT,
    KIND_CONSOLIDATE,
    MediaManagementWorker,
    estimate_collect_bytes,
    estimate_consolidate_bytes,
    preflight_disk_space,
)
from src.features.edit.core.consolidate import (
    MODE_REMUX,
    MODE_TRANSCODE,
    consolidate_project,
)
from src.features.edit.core.edit_project_io import EditProjectIO
from src.features.edit.ui.relink_media_dialog import RelinkMediaDialog
from src.features.edit.core.proxy_generator import (
    check_ffmpeg_available,
    delete_proxy as delete_proxy_file,
)
from src.shared.media.disk_proxy_service import get_disk_proxy_service
from src.shared.media.proxy import DiskProxyInfo, ProxyStatus
from src.shared.video.frame_provider_service import get_frame_provider_service
from src.features.edit.core.commands import (
    CreateBinCommand,
    UpdateSmartBinRulesCommand,
    RelinkAssetCommand,
    BatchRelinkAssetsCommand,
    DeleteBinCommand,
    DeleteAssetCommand,
    DeleteTimelineCommand,
    ProjectMacroCommand,
    RenameAssetCommand,
    RenameBinCommand,
    MoveAssetToBinCommand,
    MoveAssetBetweenBinsCommand,
    MoveBinCommand,
    MoveTimelineToBinCommand,
    MoveTimelineBetweenBinsCommand,
    UnfileAssetCommand,
    UnfileTimelineCommand,
    RemoveAssetFromBinCommand,
    RemoveTimelineFromBinCommand,
    DuplicateAssetCommand,
    UpdateAssetProductionInfoCommand,
    CreateSmartBinCommand,
    DeleteSmartBinCommand,
    ReplaceMediaCommand,
    UpdateAssetLogCommand,
    BatchUpdateAssetLogCommand,
    UpdateProxyPolicyCommand,
    UpdateSourcePatchCommand,
    ProjectCommandStack,
)
from src.features.edit.ui.edit_style import (
    get_color,
    tree_style,
)
from src.shared.ui.dock_context_menu import (
    add_dock_management_actions,
    find_parent_dock,
)

logger = logging.getLogger(__name__)


# ================================================================
# Sort Constants
# ================================================================

SORT_BY_NAME = "name"
SORT_BY_DATE = "date"
SORT_BY_TYPE = "type"
SORT_BY_DURATION = "duration"

# P0-4 大数据查询：素材数达到阈值后，过滤/排序走异步查询会话
# （MediaPoolQuerySession，latest-wins），小池保持同步内存路径。
MEDIA_POOL_ASYNC_QUERY_THRESHOLD = 5000


# ================================================================
# 支持的媒体文件扩展名
# ================================================================

# Professional media format extensions (unified registry)
from src.shared.media.format_registry import (
    VIDEO_EXTENSIONS as VIDEO_EXTENSIONS,
    IMAGE_EXTENSIONS as IMAGE_EXTENSIONS,
    AUDIO_EXTENSIONS as AUDIO_EXTENSIONS,
    SUBTITLE_EXTENSIONS as SUBTITLE_EXTENSIONS,
    get_media_type,
)


# ================================================================
# MIME Types for Drag and Drop
# ================================================================

MIME_TYPE_MEDIA_POOL_ASSET = "application/x-topos-media-pool-asset"
MIME_TYPE_MEDIA_POOL_ASSETS = "application/x-topos-media-pool-assets"  # For multiple assets
MIME_TYPE_MEDIA_POOL_TIMELINES = "application/x-topos-media-pool-timelines"  # For timeline drag-to-bin
MIME_TYPE_MEDIA_POOL_BINS = "application/x-topos-media-pool-bins"  # For bin drag-into-bin (reparent)


# ================================================================
# Helper Functions
# ================================================================

_COLOR_METADATA_KEYS = (
    "color_primaries", "color_transfer", "color_matrix", "color_range", "is_hdr",
)


def _extract_color_metadata(metadata: Optional[dict]) -> Optional[dict]:
    """Pull the color-space fields out of a probed metadata dict.

    Returns a dict with whichever color_* keys are present (None if none),
    suitable for passing to ``EditProject.import_asset(color_metadata=...)``.
    These let the IDT pick the right transfer function and the encoder tag
    the output stream correctly.
    """
    if not metadata:
        return None
    cm = {k: metadata[k] for k in _COLOR_METADATA_KEYS if metadata.get(k) is not None}
    return cm or None


def build_media_pool_drag_mime(
    asset_ids: Optional[List[str]] = None,
    asset_paths: Optional[List[str]] = None,
    timeline_ids: Optional[List[str]] = None,
) -> QMimeData:
    """
    构建媒体池拖拽 MIME 数据 (统一 list/grid 模式).

    Args:
        asset_ids: Asset ID 列表（可选）
        asset_paths: Asset 路径列表（可选，用于 urls fallback）
        timeline_ids: Timeline ID 列表（可选，用于拖入 bin）

    Returns:
        QMimeData 包含:
        - application/x-topos-media-pool-assets: JSON 格式的 asset_ids（如果有）
        - application/x-topos-media-pool-timelines: JSON 格式的 timeline_ids（如果有）
        - text/plain: asset_ids 字符串（fallback）
        - text/uri-list: 文件 URLs（如果提供了 paths）
    """
    mime = QMimeData()

    # Asset MIME（保持向后兼容）
    if asset_ids:
        unique_ids = list(dict.fromkeys(asset_ids))
        mime.setData(
            MIME_TYPE_MEDIA_POOL_ASSETS,
            QByteArray(json.dumps(unique_ids).encode("utf-8")),
        )
        mime.setText(",".join(unique_ids))

    # Timeline MIME（用于拖入 bin）
    if timeline_ids:
        unique_tl_ids = list(dict.fromkeys(timeline_ids))
        mime.setData(
            MIME_TYPE_MEDIA_POOL_TIMELINES,
            QByteArray(json.dumps(unique_tl_ids).encode("utf-8")),
        )

    # 添加 urls fallback（用于外部应用或不支持自定义 MIME 的场景）
    if asset_paths:
        # 去重 paths
        unique_paths = list(dict.fromkeys(asset_paths))
        urls = [QUrl.fromLocalFile(p) for p in unique_paths if p and os.path.exists(p)]
        if urls:
            mime.setUrls(urls)

    return mime


def _is_internal_media_pool_drag(mime: QMimeData) -> bool:
    """Return True if the drag carries internal media-pool assets/timelines/bins.

    Internal drags always set the custom MIME types (and *also* attach file
    URLs for assets, as a fallback). We must check the custom MIME types
    *before* ``hasUrls()`` in every drag handler, otherwise internal asset
    drags get misrouted to file-import instead of drop-into-bin.
    """
    return (
        mime.hasFormat(MIME_TYPE_MEDIA_POOL_ASSETS)
        or mime.hasFormat(MIME_TYPE_MEDIA_POOL_TIMELINES)
        or mime.hasFormat(MIME_TYPE_MEDIA_POOL_BINS)
    )


def build_bin_drag_mime(bin_ids: List[str]) -> QMimeData:
    """Build MIME data carrying bin ids (for bin-into-bin reparent drags)."""
    mime = QMimeData()
    unique_ids = list(dict.fromkeys(bin_ids))
    mime.setData(
        MIME_TYPE_MEDIA_POOL_BINS,
        QByteArray(json.dumps(unique_ids).encode("utf-8")),
    )
    return mime


def parse_bin_drag_mime(mime: QMimeData) -> List[str]:
    """Parse bin ids from a bin-drag MIME. Returns [] if absent/invalid."""
    data = mime.data(MIME_TYPE_MEDIA_POOL_BINS)
    if data is None or data.size() == 0:
        return []
    try:
        raw = bytes(data).decode("utf-8")
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed if x]
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return []


def parse_media_pool_drag_mime(mime: QMimeData) -> tuple[List[str], List[str]]:
    """解析媒体池拖拽 MIME 数据，返回 (asset_ids, timeline_ids)。

    用于 dropEvent 接收方。两种 MIME 都是 JSON 格式的 ID 列表。
    缺失的 MIME 类型返回空列表。
    """
    asset_ids: List[str] = []
    timeline_ids: List[str] = []

    assets_data = mime.data(MIME_TYPE_MEDIA_POOL_ASSETS)
    if assets_data is not None and assets_data.size() > 0:
        try:
            raw = bytes(assets_data).decode("utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                asset_ids = [str(x) for x in parsed if x]
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Fallback: text/plain comma-separated
            text = mime.text()
            if text:
                asset_ids = [s.strip() for s in text.split(",") if s.strip()]

    timelines_data = mime.data(MIME_TYPE_MEDIA_POOL_TIMELINES)
    if timelines_data is not None and timelines_data.size() > 0:
        try:
            raw = bytes(timelines_data).decode("utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                timeline_ids = [str(x) for x in parsed if x]
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    return asset_ids, timeline_ids


class MediaImportSignals(QObject):
    """Signals for media import probing worker."""

    progress = pyqtSignal(str, int)  # batch_id, completed count
    finished = pyqtSignal(str, str, object)  # batch_id, project_id, results


class MediaImportWorker(QRunnable):
    """Probe media metadata off the UI thread and return plain Python results."""

    def __init__(self, batch_id: str, project_id: str, file_paths: List[str]) -> None:
        super().__init__()
        self.batch_id = batch_id
        self.project_id = project_id
        self.file_paths = list(file_paths)
        self.signals = MediaImportSignals()
        self._cancelled: bool = False

    def cancel(self) -> None:
        """Request cancellation between file probes."""
        self._cancelled = True

    @pyqtSlot()
    def run(self) -> None:
        results: List[tuple[str, Optional[str], Optional[Dict[str, Any]], Optional[str]]] = []
        for index, path in enumerate(self.file_paths):
            if self._cancelled:
                break
            try:
                metadata = probe_media_metadata(path)
                # R12：probe 修正类型——纯音频容器（无视频流的
                # .mov/.mxf）按 audio 导入，不按扩展名伪装 video
                media_type = media_type_from_probe(path, metadata)
                results.append((path, media_type, metadata, None))
            except Exception as exc:
                logger.error(f"Failed to probe file: {path}, error={exc}")
                results.append((path, None, None, str(exc)))
            self.signals.progress.emit(self.batch_id, index + 1)

        self.signals.finished.emit(self.batch_id, self.project_id, results)


class ReplacementProbeSignals(QObject):
    """Plain probe results delivered to the pool's UI-thread receiver."""

    finished = pyqtSignal(str, str, object)  # asset_id, request_id, result


class ReplacementProbeWorker(QRunnable):
    """Inspect a replacement file without blocking the GUI or changing models."""

    def __init__(self, asset_id: str, request_id: str, path: str) -> None:
        super().__init__()
        self.asset_id = asset_id
        self.request_id = request_id
        self.path = path
        self.signals = ReplacementProbeSignals()

    @pyqtSlot()
    def run(self) -> None:
        signature = None
        try:
            before = os.stat(self.path)
            signature = (before.st_size, before.st_mtime_ns)
            media_type = get_media_type(self.path)
            metadata = probe_media_metadata(self.path)
            error = str(metadata.get("error") or "") if metadata else "unknown format"
            if media_type not in {"video", "audio", "image"}:
                error = "unsupported replacement type"
            elif not error and media_type in {"video", "image"}:
                if not metadata.get("width") or not metadata.get("height"):
                    error = "新源文件缺少可解码的图像/视频流"
            elif not error and media_type == "audio":
                audio = metadata.get("audio_info") or {}
                if not audio.get("channels") or not audio.get("sample_rate"):
                    error = "新源文件缺少可解码的音频流"
            after = os.stat(self.path)
            if (after.st_size, after.st_mtime_ns) != signature:
                error = "探测期间新源文件发生变化，请重试替换"
        except Exception as exc:  # External decoder/file probe failure is data.
            media_type, metadata, error = None, None, str(exc)
        # Application shutdown may delete QObjects while a decoder is still
        # returning. A destroyed receiver never needs this result.
        from PyQt6 import sip
        if not sip.isdeleted(self.signals):
            self.signals.finished.emit(
                self.asset_id, self.request_id,
                (media_type, metadata, error, signature))


class OfflineScanSignals(QObject):
    """Signals for the offline-availability scan worker (AUD-20202)."""

    # source project (identity token), checked project or None if unchanged
    finished = pyqtSignal(object, object)


class OfflineScanWorker(QRunnable):
    """Run the file-availability scan off the UI thread (AUD-20202).

    ``update_offline_status`` only reads the frozen ``EditProject`` snapshot
    (per-asset ``os.path.exists``), which can block for hundreds of ms on
    network/removable volumes — it must never run on the UI thread timer.
    """

    def __init__(self, project: EditProject) -> None:
        super().__init__()
        self._project = project
        self.signals = OfflineScanSignals()

    @pyqtSlot()
    def run(self) -> None:
        try:
            checked = update_offline_status(self._project)
        except Exception as exc:  # noqa: BLE001 — 扫描失败只记日志，不中断面板
            logger.warning(f"Offline availability scan failed: {exc}")
            self.signals.finished.emit(self._project, None)
            return
        if checked is self._project:
            self.signals.finished.emit(self._project, None)
        else:
            self.signals.finished.emit(self._project, checked)


def _probe_proxy_resolution(
    proxy_path: str, asset: "MediaAsset"
) -> Optional[tuple[int, int]]:
    """探测代理文件分辨率（PyAV 优先，ffprobe 子进程回退，估算兜底）。

    AUD-20210: 该函数是纯探测（av.open + 最长 5s 的 ffprobe 子进程 + 文件
    IO），必须只在 worker 线程执行，不得在 UI 线程同步调用。
    """
    # Try to probe proxy file resolution (PyAV first, ffprobe binary fallback)
    try:
        import av
        with av.open(proxy_path) as container:
            if container.streams.video:
                stream = container.streams.video[0]
                width = int(stream.codec_context.width)
                height = int(stream.codec_context.height)
                if width > 0 and height > 0:
                    return (width, height)
    except Exception as exc:  # noqa: BLE001 — PyAV 失败走 ffprobe 回退
        logger.debug(f"PyAV proxy probe failed for {proxy_path}: {exc}")

    try:
        import subprocess
        from src.shared.ffmpeg_locator import find_ffprobe
        ffprobe_bin = find_ffprobe()
        if ffprobe_bin is None:
            raise FileNotFoundError("ffprobe binary not found")
        result = subprocess.run(
            [
                ffprobe_bin,
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0",
                proxy_path
            ],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split(",")
            if len(parts) == 2:
                return (int(parts[0]), int(parts[1]))
    except Exception as exc:  # noqa: BLE001 — ffprobe 失败走估算兜底
        logger.debug(f"ffprobe proxy probe failed for {proxy_path}: {exc}")

    # Fallback: estimate from asset resolution (720p proxy)
    if asset.width and asset.height:
        # ProxyConfig.video_width is 1280 (720p)
        proxy_width = 1280
        aspect = asset.width / asset.height
        proxy_height = int(proxy_width / aspect)
        return (proxy_width, proxy_height)

    return None


class ProxyResolutionSignals(QObject):
    """Signals for the proxy-resolution probe worker (AUD-20210).

    N21/RF05：载荷携带 asset_id 与 proxy session generation——二阶段
    探测的身份凭据。回调到达时 get（不先 pop）并核对完整身份，只有
    generation/asset/source 三者都匹配才消费；旧会话回调零写入且不得
    清掉重新登记的新 pending。
    """

    #: proxy_path, source_path, resolution | None, asset_id, generation
    probed = pyqtSignal(str, str, object, str, int, str)


class ProxyServiceSignals(QObject):
    """R03（媒体池复查）：统一代理服务回调 → UI 线程的信号桥。

    ``DiskProxyService.generate_proxy`` 的 on_complete/on_progress/
    on_error 在管理器后台线程直接调用；本桥把回调转成 Qt 信号
    （自动排队到面板线程），沿用既有 token+代际+源签名守卫链。
    """

    progress = pyqtSignal(str, int, int)   # asset_id, percent, token
    finished = pyqtSignal(str, str, int)   # asset_id, proxy_path, token
    error = pyqtSignal(str, str, int)      # asset_id, message, token


class ProxyResolutionWorker(QRunnable):
    """Run the proxy file probe off the UI thread (AUD-20210).

    ``_probe_proxy_resolution`` 可能执行 ``av.open`` 与最长 5s 超时的
    ffprobe 子进程 —— 慢盘/杀毒拦截时可阻塞 UI 数秒，必须移入 worker。
    """

    def __init__(
        self,
        proxy_path: str,
        source_path: str,
        asset: "MediaAsset",
        signals: Optional[ProxyResolutionSignals] = None,
        asset_id: str = "",
        session_generation: int = -1,
        request_id: str = "",
    ) -> None:
        super().__init__()
        self._request_id = request_id
        self._proxy_path = proxy_path
        self._source_path = source_path
        self._asset = asset
        self._asset_id = asset_id
        self._session_generation = session_generation
        self.signals = signals if signals is not None else ProxyResolutionSignals()

    @pyqtSlot()
    def run(self) -> None:
        try:
            resolution = _probe_proxy_resolution(self._proxy_path, self._asset)
        except Exception as exc:  # noqa: BLE001 — 探测失败按 None 回退
            logger.warning(f"Proxy resolution probe failed: {exc}")
            resolution = None
        from PyQt6 import sip
        if not sip.isdeleted(self.signals):
            self.signals.probed.emit(
                self._proxy_path, self._source_path, resolution,
                self._asset_id, self._session_generation, self._request_id,
            )


# ================================================================
# View Mode and Thumbnail Size Enums
# ================================================================

class ViewMode(Enum):
    """视图模式枚举"""
    LIST = auto()  # 列表视图（详细元数据）
    GRID = auto()  # 网格视图（缩略图）
    METADATA = auto()  # 缩略图与日志元数据


class ThumbnailSize(Enum):
    """缩略图尺寸枚举 — DaVinci 紧凑规格"""
    SMALL = (60, 40)    # 小尺寸
    MEDIUM = (100, 67)  # 中尺寸（默认）
    LARGE = (160, 107)  # 大尺寸

    @property
    def size(self) -> tuple[int, int]:
        """获取尺寸"""
        return self.value


class SearchFilterMode(Enum):
    """搜索过滤模式枚举"""
    ALL_FIELDS = "all_fields"
    FILENAME = "filename"
    CODEC = "codec"
    RESOLUTION = "resolution"
    FPS = "fps"


# ================================================================
# SearchLineEdit - 搜索输入框（支持多过滤模式）
# ================================================================

class SearchLineEdit(QWidget):
    """
    搜索输入框，支持按文件名/codec/resolution/fps过滤。

    Signals:
        search_changed: 搜索文本或过滤模式变化 (text, filter_mode)
    """

    search_changed = pyqtSignal(str, str)  # text, filter_mode value

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)
        self.line_edit = QLineEdit()
        self.line_edit.setPlaceholderText(I18N.tr("edit.media_pool.search", "Search"))
        self.line_edit.setFixedHeight(24)
        self.line_edit.setClearButtonEnabled(True)
        self.line_edit.setAccessibleName(I18N.tr("edit.media_pool.search", "Search"))
        layout.addWidget(self.line_edit, stretch=1)
        layout.addWidget(QLabel(I18N.tr("edit.media_pool.filter_by", "Filter by")))
        self.filter_combo = QComboBox()
        for key, label, mode in (
            ("filter_all_fields", "All Fields", SearchFilterMode.ALL_FIELDS),
            ("filter_filename", "File Name", SearchFilterMode.FILENAME),
            ("filter_codec", "Codec", SearchFilterMode.CODEC),
            ("filter_resolution", "Resolution", SearchFilterMode.RESOLUTION),
            ("filter_fps", "FPS", SearchFilterMode.FPS),
        ):
            self.filter_combo.addItem(I18N.tr(f"edit.media_pool.{key}", label), mode.value)
        self.filter_combo.setCurrentIndex(1)
        self.filter_combo.setMinimumWidth(100)
        self.filter_combo.setFixedHeight(24)
        layout.addWidget(self.filter_combo)

        # 连接信号
        self.line_edit.textChanged.connect(self._on_text_changed)
        self.filter_combo.currentIndexChanged.connect(self._on_filter_changed)

    def _on_text_changed(self, text: str) -> None:
        """搜索文本变化"""
        filter_mode = self.filter_combo.currentData()
        self.search_changed.emit(text, filter_mode or SearchFilterMode.FILENAME.value)

    def _on_filter_changed(self, index: int) -> None:
        """过滤模式变化"""
        text = self.line_edit.text()
        filter_mode = self.filter_combo.currentData()
        self.search_changed.emit(text, filter_mode or SearchFilterMode.FILENAME.value)

    def text(self) -> str:
        """获取搜索文本"""
        return self.line_edit.text()

    def filter_mode(self) -> str:
        """获取当前过滤模式"""
        return self.filter_combo.currentData() or SearchFilterMode.FILENAME.value


# ================================================================
# BinTreeWidget - 左侧素材箱树
# ================================================================

class BinTreeWidget(QTreeWidget):
    """
    左侧素材箱树，显示 Smart Bins 和 Regular Bins。

    顶层节点：
    - Smart Bins（智能素材箱，不可编辑）
      - All Video / All Audio / All Stills / Offline / Recent Imports
    - Bins（普通素材箱，可新建/删除/重命名）

    Signals:
        bin_selected: 选择了一个素材箱 (bin_id, is_smart_bin)
        create_bin_requested: 请求创建新素材箱
        delete_bin_requested: 请求删除素材箱 (bin_id)
        rename_bin_requested: 请求重命名素材箱 (bin_id)
    """

    bin_selected = pyqtSignal(str, bool)  # bin_id, is_smart_bin
    # P1-12: Smart Bin 管理（由面板执行命令走 undo 栈）
    create_smart_bin_requested = pyqtSignal()
    edit_smart_bin_requested = pyqtSignal(str)    # bin_id（R14）
    delete_smart_bin_requested = pyqtSignal(str)  # bin_id
    create_bin_requested = pyqtSignal()
    create_bin_in_requested = pyqtSignal(str)  # explicit parent for tree context actions
    delete_bin_requested = pyqtSignal(str)  # bin_id
    rename_bin_requested = pyqtSignal(str)  # bin_id

    # Bin tree item data roles
    BIN_ID_ROLE = Qt.ItemDataRole.UserRole
    BIN_TYPE_ROLE = Qt.ItemDataRole.UserRole + 1  # "smart_bin" or "bin"
    BIN_USER_ROLE = Qt.ItemDataRole.UserRole + 4  # True = 用户自建 smart bin

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self._project: Optional[EditProject] = None

        self.setHeaderHidden(True)
        self.setRootIsDecorated(True)
        self.setAnimated(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setIndentation(14)
        # Inline rename only via programmatic editItem() (F2 / menu), never on
        # double-click (double-click would conflict with expand/collapse).
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        # Accept drops: assets/timelines can be dragged from the right-side
        # view onto a bin node here to add them to that bin. Bins themselves
        # can also be dragged (reparent into another bin).
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        # Highlight the bin node under the cursor during a drag so the user
        # gets clear feedback about which bin will receive the drop.
        self.setDefaultDropAction(Qt.DropAction.CopyAction)

        # Drop-target highlight + spring-loaded folder expansion state.
        self._drop_target_item: Optional[QTreeWidgetItem] = None
        self._spring_timer: QTimer = QTimer(self)
        self._spring_timer.setSingleShot(True)
        self._spring_timer.setInterval(700)  # ms before auto-expand
        self._spring_timer.timeout.connect(self._spring_expand_current)
        self._spring_target_id: Optional[str] = None

        # 连接信号
        self.currentItemChanged.connect(self._on_item_changed)
        self.customContextMenuRequested.connect(self._on_context_menu)
        self.itemChanged.connect(self._on_item_data_changed)

        # 生成展开/收起箭头图标并应用样式
        self._arrow_collapsed, self._arrow_expanded = self._create_arrow_images()
        self.setStyleSheet(self._build_tree_style())

    # ---- 箭头图标生成 ----

    @staticmethod
    def _create_arrow_images() -> tuple[str, str]:
        """用 QPainter 绘制小巧的三角箭头，保存到临时文件，返回路径。"""
        import tempfile, os
        _tmp = tempfile.gettempdir()
        collapsed_path = os.path.join(_tmp, "topos_arrow_right.png")
        expanded_path = os.path.join(_tmp, "topos_arrow_down.png")

        arrow_color = QColor("#666")
        sz = 9

        # ---- 右三角（收起态） ----
        pm_r = QPixmap(sz, sz)
        pm_r.fill(QColor(0, 0, 0, 0))
        p = QPainter(pm_r)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QBrush(arrow_color))
        p.setPen(Qt.PenStyle.NoPen)
        poly_r = QPolygon([
            QPoint(3, 1), QPoint(7, sz // 2), QPoint(3, sz - 2)
        ])
        p.drawPolygon(poly_r)
        p.end()
        pm_r.save(collapsed_path)

        # ---- 下三角（展开态） ----
        pm_d = QPixmap(sz, sz)
        pm_d.fill(QColor(0, 0, 0, 0))
        p = QPainter(pm_d)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QBrush(arrow_color))
        p.setPen(Qt.PenStyle.NoPen)
        poly_d = QPolygon([
            QPoint(1, 3), QPoint(sz // 2, 7), QPoint(sz - 2, 3)
        ])
        p.drawPolygon(poly_d)
        p.end()
        pm_d.save(expanded_path)

        return collapsed_path, expanded_path

    def _build_tree_style(self) -> str:
        """基于 edit_style 的 tree_style，追加展开/收起箭头图片。"""
        base = tree_style()
        collapsed = self._arrow_collapsed.replace("\\", "/")
        expanded = self._arrow_expanded.replace("\\", "/")

        arrow_css = f"""
            QTreeWidget::branch:has-children:!has-siblings:closed,
            QTreeWidget::branch:closed:has-children:has-siblings {{
                image: url("{collapsed}");
            }}

            QTreeWidget::branch:open:has-children:!has-siblings,
            QTreeWidget::branch:open:has-children:has-siblings {{
                image: url("{expanded}");
            }}
        """
        # 插入到 base 样式的最后一个 }} 之前
        return base + arrow_css

    def populate(self, project: Optional[EditProject]) -> None:
        """
        填充素材箱树。

        Args:
            project: 当前项目
        """
        self.clear()

        if project is None:
            return

        # Resolve's Master node maps to the existing implicit root bin.
        master = QTreeWidgetItem(self)
        master.setText(0, I18N.tr("edit.media_pool.master", "Master"))
        master.setData(0, self.BIN_ID_ROLE, project.root_bin_id)
        master.setData(0, self.BIN_TYPE_ROLE, "bin")
        master.setExpanded(True)
        master.setSizeHint(0, QSize(0, 29))
        master.setFlags(master.flags() & ~Qt.ItemFlag.ItemIsDragEnabled & ~Qt.ItemFlag.ItemIsEditable)
        root_id = project.root_bin_id
        children_by_parent: dict[str | None, list[str]] = {}
        for bin_id, bin_obj in project.bins.items():
            if bin_id == root_id:
                # The root bin itself is never rendered; skip it but its
                # children are still picked up via root_id below.
                continue
            parent_id = bin_obj.parent_id
            if parent_id not in project.bins or parent_id == bin_id:
                parent_id = None
            # Treat the root bin as the implicit top level: a bin whose
            # parent is root_id mounts at the top of the tree.
            if parent_id == root_id:
                parent_id = None
            children_by_parent.setdefault(parent_id, []).append(bin_id)

        for child_ids in children_by_parent.values():
            child_ids.sort(key=lambda child_id: project.bins[child_id].name.casefold())

        visited: set[str] = set()
        # Mark the root bin as visited so add_bin never creates a node for it.
        if root_id:
            visited.add(root_id)

        def add_bin(bin_id: str, parent_item: Optional[QTreeWidgetItem]) -> None:
            if bin_id in visited:
                return
            visited.add(bin_id)
            bin_obj = project.bins[bin_id]
            item = QTreeWidgetItem(parent_item if parent_item is not None else master)
            # Display text carries the asset count; the EditRole stores the
            # plain name so inline editing only changes the name (not count).
            item.setData(0, Qt.ItemDataRole.DisplayRole, bin_obj.name)
            item.setData(0, Qt.ItemDataRole.EditRole, bin_obj.name)
            item.setData(0, self.BIN_ID_ROLE, bin_id)
            item.setData(0, self.BIN_TYPE_ROLE, "bin")

            # Every visible bin is user-created → editable and draggable
            # (reparent into another bin). The root bin never reaches here.
            item.setFlags(
                item.flags()
                | Qt.ItemFlag.ItemIsEditable
                | Qt.ItemFlag.ItemIsDragEnabled
            )
            for child_id in children_by_parent.get(bin_id, []):
                add_bin(child_id, item)
            item.setExpanded(False)

        # Mount every bin whose parent is the implicit root (or legacy None)
        # at the top level, then any orphaned bins (circular/malformed parent
        # links) at the top level too.
        for bin_id in children_by_parent.get(None, []):
            add_bin(bin_id, None)
        for bin_id in sorted(project.bins, key=lambda item: project.bins[item].name.casefold()):
            if bin_id not in visited:
                add_bin(bin_id, None)

        # ===== Smart Bins 节点 =====
        # Appended below the bin tree so Root stays on top.
        smart_root = QTreeWidgetItem(self)
        smart_root.setText(0, I18N.tr("edit.media_pool.smart_bins", "Smart Bins"))
        smart_root.setIcon(0, icon_smart())
        smart_root.setData(0, self.BIN_ID_ROLE, "__smart_bins__")
        smart_root.setData(0, self.BIN_TYPE_ROLE, "section")
        smart_root.setExpanded(True)
        smart_root.setFlags(smart_root.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)

        # Smart Bin 条目 — DaVinci 风格：柔和灰蓝图标
        smart_bins = EditProject.get_default_smart_bins()
        smart_bin_icons = {
            SMART_BIN_ALL_VIDEO: "fa5s.film",
            SMART_BIN_ALL_AUDIO: "fa5s.music",
            SMART_BIN_ALL_STILLS: "fa5s.image",
            SMART_BIN_OFFLINE_MEDIA: "fa5s.unlink",
            SMART_BIN_RECENT_IMPORTS: "fa5s.clock",
        }
        _muted_icon_color = "#7a8899"  # 柔和灰蓝
        for sb in smart_bins:
            item = QTreeWidgetItem(smart_root)
            item.setText(0, sb.name)
            item.setData(0, self.BIN_ID_ROLE, sb.bin_id)
            item.setData(0, self.BIN_TYPE_ROLE, "smart_bin")
            # 设置图标 — 柔和色调
            icon_name = smart_bin_icons.get(sb.bin_id, "fa5s.filter")
            try:
                item.setIcon(0, qta.icon(icon_name, color=_muted_icon_color))
            except Exception:
                item.setIcon(0, icon_smart())
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)

        # P1-12: 用户自建 Smart Bin（已保存搜索），带 bookmark 图标以示区分
        for sb in project.smart_bins.values() if project else ():
            item = QTreeWidgetItem(smart_root)
            item.setText(0, sb.name)
            item.setData(0, self.BIN_ID_ROLE, sb.bin_id)
            item.setData(0, self.BIN_TYPE_ROLE, "smart_bin")
            item.setData(0, self.BIN_USER_ROLE, True)
            try:
                item.setIcon(0, qta.icon("fa5s.bookmark", color=_muted_icon_color))
            except Exception:
                item.setIcon(0, icon_smart())
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)

    def contextMenuEvent(self, event: Any) -> None:  # noqa: N802
        self._on_context_menu(self.viewport().mapFromGlobal(event.globalPos()))
        event.accept()

    def select_bin(self, bin_id: str, is_smart_bin: bool) -> bool:
        """Restore a bin selection without emitting a second refresh request."""
        expected_type = "smart_bin" if is_smart_bin else "bin"
        def find_item(parent: QTreeWidgetItem) -> Optional[QTreeWidgetItem]:
            # Check the item itself first (top-level nodes like the root bin
            # are now real bin items, not wrapped in a section).
            if (
                parent.data(0, self.BIN_ID_ROLE) == bin_id
                and parent.data(0, self.BIN_TYPE_ROLE) == expected_type
            ):
                return parent
            for child_index in range(parent.childCount()):
                item = parent.child(child_index)
                if item is None:
                    continue
                if (
                    item.data(0, self.BIN_ID_ROLE) == bin_id
                    and item.data(0, self.BIN_TYPE_ROLE) == expected_type
                ):
                    return item
                nested = find_item(item)
                if nested is not None:
                    return nested
            return None

        for top_index in range(self.topLevelItemCount()):
            root = self.topLevelItem(top_index)
            if root is None:
                continue
            item = find_item(root)
            if item is not None:
                previous = self.blockSignals(True)
                try:
                    self.setCurrentItem(item)
                finally:
                    self.blockSignals(previous)
                return True
        return False

    def _on_item_changed(
        self, current: Optional[QTreeWidgetItem], previous: Optional[QTreeWidgetItem]
    ) -> None:
        """选择项变化"""
        if current is None:
            return

        bin_id = current.data(0, self.BIN_ID_ROLE)
        bin_type = current.data(0, self.BIN_TYPE_ROLE)

        if bin_type == "smart_bin":
            self.bin_selected.emit(bin_id, True)
        elif bin_type == "bin":
            self.bin_selected.emit(bin_id, False)
        # section 节点不触发选择

    def _build_context_menu(self, pos: QPoint) -> QMenu:
        """Blank space under Master creates a root Bin, independent of selection."""
        menu = QMenu(self)
        item = self.itemAt(pos)
        if item is None:
            if bool(self.property("smartBinsOnly")):
                menu.addAction(I18N.tr("edit.media_pool.new_smart_bin", "New Smart Bin…")).triggered.connect(self.create_smart_bin_requested.emit)
            elif self._project is not None:
                root_id = self._project.root_bin_id
                menu.addAction(I18N.tr("edit.media_pool.new_bin", "New Bin")).triggered.connect(
                    lambda _checked=False, bid=root_id: self.create_bin_in_requested.emit(bid))
            return menu
        bin_id = item.data(0, self.BIN_ID_ROLE)
        bin_type = item.data(0, self.BIN_TYPE_ROLE)
        if bin_type == "bin":
            menu.addAction(I18N.tr("edit.media_pool.new_bin", "New Bin")).triggered.connect(
                lambda _checked=False, bid=bin_id: self.create_bin_in_requested.emit(bid))
            if self._project is not None and bin_id != self._project.root_bin_id:
                menu.addSeparator()
                menu.addAction(I18N.tr("edit.media_pool.rename_bin", "Rename Bin")).triggered.connect(
                    lambda _checked=False: self.rename_bin_requested.emit(bin_id))
                menu.addAction(I18N.tr("edit.media_pool.remove_from_project", "Remove from Media Pool")).triggered.connect(
                    lambda _checked=False: self.delete_bin_requested.emit(bin_id))
        elif bin_type == "section":
            menu.addAction(I18N.tr("edit.media_pool.new_smart_bin", "New Smart Bin…")).triggered.connect(self.create_smart_bin_requested.emit)
        elif bin_type == "smart_bin" and bool(item.data(0, self.BIN_USER_ROLE)):
            menu.addAction(I18N.tr("edit.media_pool.edit_smart_bin", "Edit Smart Bin…")).triggered.connect(
                lambda _checked=False: self.edit_smart_bin_requested.emit(bin_id))
            menu.addAction(I18N.tr("edit.media_pool.remove_from_project", "Remove from Media Pool")).triggered.connect(
                lambda _checked=False: self.delete_smart_bin_requested.emit(bin_id))
        return menu

    def _on_context_menu(self, pos: QPoint) -> None:
        menu = self._build_context_menu(pos)
        if not menu.actions():
            return
        dock = find_parent_dock(self)
        if dock is not None:
            menu.addSeparator()
            add_dock_management_actions(menu, dock)
        menu.exec(self.viewport().mapToGlobal(pos))

    def set_project(self, project: Optional[EditProject]) -> None:
        """设置项目引用"""
        self._project = project

    def startDrag(self, supportedActions: Qt.DropAction) -> None:
        """Start a drag carrying the selected bin id(s) for reparent.

        Only real bins (not the root, not section headers, not smart bins)
        are draggable. Builds the bin MIME so drop handlers can reparent.
        """
        selected = self.selectedItems()
        bin_ids: List[str] = []
        for item in selected:
            if (
                item.data(0, self.BIN_TYPE_ROLE) == "bin"
                and item.data(0, self.BIN_ID_ROLE)
            ):
                panel = self._find_media_pool_panel()
                root_id = panel._project.root_bin_id if panel and panel._project else None
                if root_id and item.data(0, self.BIN_ID_ROLE) == root_id:
                    continue  # root bin not draggable
                bin_ids.append(item.data(0, self.BIN_ID_ROLE))
        if not bin_ids:
            return
        mime = build_bin_drag_mime(bin_ids)
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.MoveAction)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        mime = event.mimeData()
        if _is_internal_media_pool_drag(mime) or mime.hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        mime = event.mimeData()
        if _is_internal_media_pool_drag(mime):
            # Internal drag: only accept when hovering over a bin node so the
            # cursor gives honest feedback (drops elsewhere are ignored).
            panel = self._find_media_pool_panel()
            if panel is not None:
                pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
                target_bin_id = panel._target_bin_at(pos, source="sidebar")
                if target_bin_id is not None:
                    self._set_drop_target(target_bin_id)
                    event.acceptProposedAction()
                    return
            self._clear_drop_target()
            event.ignore()
            return
        if mime.hasUrls():
            # External OS file drop — highlight the bin under the cursor so the
            # user sees where files will land (Finder-style). Also arms the
            # spring-loaded auto-expand for hovered collapsed bins.
            panel = self._find_media_pool_panel()
            if panel is not None:
                pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
                target_bin_id = panel._target_bin_at(pos, source="sidebar")
                if target_bin_id is not None:
                    self._set_drop_target(target_bin_id)
                    event.acceptProposedAction()
                    return
            self._clear_drop_target()
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dragLeaveEvent(self, event: Any) -> None:
        """Clear the drop-target highlight and cancel spring expansion."""
        self._clear_drop_target()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        mime = event.mimeData()
        # Internal drag takes priority: drop assets/timelines/bins into the bin
        # node under the cursor. (Checked before hasUrls() because internal
        # asset drags also carry file URLs as a fallback.)
        if _is_internal_media_pool_drag(mime):
            panel = self._find_media_pool_panel()
            if panel is not None:
                pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
                target_bin_id = panel._target_bin_at(pos, source="sidebar")
                if target_bin_id is not None:
                    # Bin reparent (drag bin into bin) takes priority.
                    bin_ids = parse_bin_drag_mime(mime)
                    if bin_ids:
                        if panel._handle_drop_bin_into_bin(target_bin_id, bin_ids):
                            self._clear_drop_target()
                            event.acceptProposedAction()
                            return
                    else:
                        asset_ids, timeline_ids = parse_media_pool_drag_mime(mime)
                        # P0-5：Alt/Option = 拷贝引用（多箱共存）；普通拖拽 = 移动。
                        handled = panel._handle_drop_into_bin(
                            target_bin_id, asset_ids, timeline_ids,
                            copy=bool(
                                event.modifiers()
                                & Qt.KeyboardModifier.AltModifier),
                        )
                        if handled:
                            self._clear_drop_target()
                            event.acceptProposedAction()
                            return
            self._clear_drop_target()
            event.ignore()
            return

        # OS file drops are forwarded to the panel for import. Resolve the bin
        # under the cursor so files land *inside* it (Finder behavior) instead
        # of always falling back to the project root.
        if mime.hasUrls():
            panel = self._find_media_pool_panel()
            if panel is not None:
                pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
                target_bin_id = panel._target_bin_at(pos, source="sidebar")
                self._clear_drop_target()
                panel.dropEvent(event, target_bin_id=target_bin_id)
                event.acceptProposedAction()
                return
            event.ignore()
            return

        self._clear_drop_target()
        super().dropEvent(event)

    def _set_drop_target(self, bin_id: str) -> None:
        """Highlight the hovered bin and arm the spring-loaded auto-expand."""
        if bin_id == self._spring_target_id:
            return  # Same bin — keep the timer running.
        # Target changed: cancel pending expansion and (re)arm the timer.
        self._spring_target_id = bin_id
        self._spring_timer.start()
        # Update visual highlight.
        item = self._find_bin_item(bin_id)
        if item is not self._drop_target_item:
            self._clear_drop_target()
            if item is not None:
                self._drop_target_item = item
                item.setBackground(0, QColor(get_color("accent")))

    def _clear_drop_target(self) -> None:
        """Remove the drop highlight and cancel spring expansion."""
        self._spring_timer.stop()
        self._spring_target_id = None
        if self._drop_target_item is not None:
            self._drop_target_item.setBackground(0, QColor(0, 0, 0, 0))
            self._drop_target_item = None

    def _spring_expand_current(self) -> None:
        """Expand the currently hovered bin (spring-loaded folders)."""
        if self._spring_target_id is None:
            return
        item = self._find_bin_item(self._spring_target_id)
        if item is not None and not item.isExpanded():
            item.setExpanded(True)

    def _find_bin_item(self, bin_id: str) -> Optional[QTreeWidgetItem]:
        """Locate the tree node for a bin id (recursive)."""
        iterator = QTreeWidgetItemIterator(self)
        while iterator.value() is not None:
            item = iterator.value()
            if (
                item.data(0, self.BIN_TYPE_ROLE) == "bin"
                and item.data(0, self.BIN_ID_ROLE) == bin_id
            ):
                return item
            iterator += 1
        return None

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Forward Delete/Backspace to the owning MediaPoolPanel for batch delete.

        F2 / Return starts inline rename on the selected bin (Finder-style).
        """
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            panel = self._find_media_pool_panel()
            if panel is not None and panel.handle_delete_key_from(self):
                event.accept()
                return
        if event.key() in (Qt.Key.Key_F2, Qt.Key.Key_Return):
            item = self.currentItem()
            if (
                item is not None
                and item.data(0, self.BIN_TYPE_ROLE) == "bin"
                and item.flags() & Qt.ItemFlag.ItemIsEditable
            ):
                self.editItem(item, 0)
                event.accept()
                return
        # Space — toggle Quick Look on the selected asset (Finder behavior).
        if event.key() == Qt.Key.Key_Space and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            panel = self._find_media_pool_panel()
            if panel is not None:
                panel._show_quick_look()
                event.accept()
                return
        # Escape — clear selection (Finder behavior).
        if event.key() == Qt.Key.Key_Escape:
            self.clearSelection()
            event.accept()
            return
        super().keyPressEvent(event)

    def _on_item_data_changed(self, item: QTreeWidgetItem, column: int) -> None:
        """Handle inline-edit commits: route bin renames to the panel.

        The ``_updating`` guard on the panel prevents recursion when a refresh
        rebuilds the tree (programmatic setText fires this signal too); those
        rebuilds happen while the panel holds ``_updating=True``.
        """
        panel = self._find_media_pool_panel()
        if panel is None or panel._updating:
            return
        if column != 0:
            return
        if item.data(0, self.BIN_TYPE_ROLE) != "bin":
            return
        bin_id = item.data(0, self.BIN_ID_ROLE)
        new_name = item.data(0, Qt.ItemDataRole.EditRole) or ""
        panel._apply_bin_rename(bin_id, new_name)

    def start_inline_rename(self, bin_id: str) -> None:
        """Select the bin node for ``bin_id`` and enter inline-edit mode."""
        if bin_id is None:
            return
        iterator = QTreeWidgetItemIterator(self)
        while iterator.value() is not None:
            item = iterator.value()
            if (
                item.data(0, self.BIN_TYPE_ROLE) == "bin"
                and item.data(0, self.BIN_ID_ROLE) == bin_id
            ):
                ancestor = item.parent()
                while ancestor is not None:
                    ancestor.setExpanded(True)
                    ancestor = ancestor.parent()
                self.scrollToItem(item)
                self.setCurrentItem(item)
                self.editItem(item, 0)
                return
            iterator += 1

    def _find_media_pool_panel(self) -> Optional[MediaPoolPanel]:
        """Walk up the parent chain to find the owning MediaPoolPanel."""
        parent = self.parent()
        while parent is not None:
            if isinstance(parent, MediaPoolPanel):
                return parent
            parent = parent.parent()
        return None


# ================================================================
# Grid Item - Unified wrapper for assets and timelines
# ================================================================

class GridItem:
    """
    Unified wrapper for grid view items (assets or timelines).

    Attributes:
        item_type: "asset" or "timeline"
        item_id: asset_id or timeline_id
        data: MediaAsset or TimelineSequenceRef
    """
    def __init__(
        self,
        item_type: str,
        item_id: str,
        data: Any,  # MediaAsset or TimelineSequenceRef
    ) -> None:
        self.item_type = item_type
        self.item_id = item_id
        self.data = data

    @property
    def display_name(self) -> str:
        """Get display name for the item."""
        if self.item_type == "asset" and isinstance(self.data, MediaAsset):
            return self.data.display_name
        elif self.item_type in ("timeline", "bin"):
            return self.data.name
        return ""

    @property
    def is_asset(self) -> bool:
        return self.item_type == "asset"

    @property
    def is_timeline(self) -> bool:
        return self.item_type == "timeline"


# ================================================================
# Asset List Model (for Grid View) - Extended for timelines
# ================================================================

class AssetListModel(QAbstractListModel):
    """素材列表模型，用于网格视图（支持 assets + timelines + bins）"""

    # Custom data roles
    ITEM_TYPE_ROLE = Qt.ItemDataRole.UserRole + 10  # "asset", "timeline", or "bin"
    ITEM_ID_ROLE = Qt.ItemDataRole.UserRole + 11    # asset_id, timeline_id, or bin_id
    TIMELINE_DATA_ROLE = Qt.ItemDataRole.UserRole + 12  # TimelineSequenceRef
    IS_ACTIVE_ROLE = Qt.ItemDataRole.UserRole + 13  # True if current active timeline (V-10, UI-27)
    BIN_DATA_ROLE = Qt.ItemDataRole.UserRole + 14   # ProjectBin (for "bin" items)
    TIMELINE_PREVIEW_ROLE = Qt.ItemDataRole.UserRole + 15  # QPixmap preview

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._items: List[GridItem] = []  # Mixed assets + timelines
        self._thumbnails: Dict[str, QPixmap] = {}  # path -> pixmap
        # Virtual scene-cut assets share the parent's path, so their poster
        # frames must be keyed by asset id rather than path.
        self._asset_thumbnails: Dict[str, QPixmap] = {}
        self._timeline_previews: Dict[str, QPixmap] = {}  # timeline_id -> pixmap
        self._active_timeline_id: Optional[str] = None  # Active timeline for highlight (V-10, UI-27)

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        default_flags = super().flags(index)
        if index.isValid():
            item = self._get_item(index)
            if item is None:
                return default_flags
            # Assets can be dragged to the timeline or to a bin.
            # Timelines can be dragged to a bin (the timeline panel ignores
            # them — it only reads the assets MIME).
            # Bins are not draggable, but are editable in-place (Finder-style
            # rename via F2 / context menu).
            if item.is_asset or item.is_timeline:
                flags = default_flags | Qt.ItemFlag.ItemIsDragEnabled
                # Assets support inline rename (F2 / double-click in edit mode).
                if item.is_asset:
                    flags |= Qt.ItemFlag.ItemIsEditable
                return flags
            if item.item_type == "bin":
                return default_flags | Qt.ItemFlag.ItemIsEditable
        return default_flags

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        """获取行数"""
        if parent.isValid():
            return 0
        return len(self._items)

    def _get_item(self, index: QModelIndex) -> Optional[GridItem]:
        """Get item at index."""
        if (
            not index.isValid()
            or index.model() is not self
            or index.row() < 0
            or index.row() >= len(self._items)
        ):
            return None
        return self._items[index.row()]

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        """获取数据"""
        item = self._get_item(index)
        if not item:
            return None

        if role == Qt.ItemDataRole.ToolTipRole:
            path = getattr(item.data, "path", "")
            return f"{item.display_name}\n{path}" if path else item.display_name

        if role == Qt.ItemDataRole.DisplayRole:
            return item.display_name

        elif role == Qt.ItemDataRole.EditRole:
            # Inline rename reads the plain name. Bins and assets are both
            # editable; timelines are not.
            if item.item_type == "bin" and isinstance(item.data, ProjectBin):
                return item.data.name
            if item.is_asset and isinstance(item.data, MediaAsset):
                return item.data.display_name
            return None

        elif role == Qt.ItemDataRole.UserRole:
            # Return the underlying data (MediaAsset or TimelineSequenceRef)
            return item.data

        elif role == self.ITEM_TYPE_ROLE:
            return item.item_type

        elif role == self.ITEM_ID_ROLE:
            return item.item_id

        elif role == self.TIMELINE_DATA_ROLE:
            if item.is_timeline:
                return item.data
            return None

        elif role == self.IS_ACTIVE_ROLE:
            # Check if this timeline is the current active one (V-10, UI-27)
            if item.is_timeline:
                return item.item_id == self._active_timeline_id
            return False

        elif role == self.BIN_DATA_ROLE:
            # Return the underlying ProjectBin for "bin" items
            if item.item_type == "bin":
                return item.data
            return None

        elif role == self.TIMELINE_PREVIEW_ROLE:
            if item.is_timeline:
                return self._timeline_previews.get(item.item_id)
            return None

        elif role == Qt.ItemDataRole.UserRole + 1:  # Thumbnail role (assets only)
            if item.is_asset and isinstance(item.data, MediaAsset):
                asset_thumbnail = self._asset_thumbnails.get(item.data.asset_id)
                if item.data.metadata.get("is_subclip"):
                    return asset_thumbnail
                return asset_thumbnail or self._thumbnails.get(item.data.path)

        elif role == Qt.ItemDataRole.UserRole + 2:  # Path role (assets only)
            if item.is_asset and isinstance(item.data, MediaAsset):
                return item.data.path

        return None

    def thumbnail_for_path(self, path: str) -> Optional[QPixmap]:
        """Return the cached thumbnail for an asset or preview source path."""
        return self._thumbnails.get(path)

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        """Handle inline-edit commits for bin/asset items (Finder-style rename).

        The model does not own the data — it mirrors the project. So we
        forward the rename to the owning ``MediaPoolPanel`` (which runs it
        through the command stack for Undo support) rather than mutating the
        cached ``GridItem`` directly. The panel's refresh rebuilds the model.
        """
        if role != Qt.ItemDataRole.EditRole or not index.isValid():
            return False
        item = self._get_item(index)
        if item is None:
            return False
        panel = self.parent()
        if not isinstance(panel, MediaPoolPanel):
            return False
        new_name = value.strip() if isinstance(value, str) else ""
        if item.item_type == "bin":
            return panel._apply_bin_rename(item.item_id, new_name)
        if item.is_asset:
            return panel._apply_asset_rename(item.item_id, new_name)
        return False

    def row_for_asset(self, asset_id: str) -> Optional[int]:
        """Return the model row for the given asset id, or None."""
        for i, item in enumerate(self._items):
            if item.is_asset and item.item_id == asset_id:
                return i
        return None

    def set_items(self, items: List[GridItem]) -> None:
        """设置混合列表（assets + timelines）"""
        self.beginResetModel()
        try:
            self._items = list(items)
            visible_timeline_ids = {
                item.item_id for item in self._items if item.is_timeline
            }
            self._timeline_previews = {
                timeline_id: pixmap
                for timeline_id, pixmap in self._timeline_previews.items()
                if timeline_id in visible_timeline_ids
            }
            # 不清空缩略图缓存，保留已加载的缩略图
            # 从 ThumbnailProvider 获取已有的缓存缩略图
            thumb_provider = ThumbnailProvider.instance()
            assets_by_id = {
                item.data.asset_id: item.data
                for item in self._items
                if item.is_asset and isinstance(item.data, MediaAsset)
            }
            for item in self._items:
                if item.is_asset and isinstance(item.data, MediaAsset):
                    preview_asset = item.data
                    if item.data.is_comp_clip:
                        source_id = item.data.metadata.get("original_asset_id", "")
                        preview_asset = assets_by_id.get(source_id, item.data)
                    cached = thumb_provider.get_cached_thumbnail(preview_asset.path, 120)
                    if cached:
                        self._thumbnails[preview_asset.path] = cached
                        self._thumbnails[item.data.path] = cached
        finally:
            self.endResetModel()

    def set_asset_thumbnail(self, asset_id: str, pixmap: QPixmap) -> None:
        """Store a poster for one asset, including virtual subclips."""
        if not asset_id or pixmap is None or pixmap.isNull():
            return
        self._asset_thumbnails[asset_id] = pixmap
        for row, item in enumerate(self._items):
            if item.is_asset and item.item_id == asset_id:
                index = self.index(row, 0)
                if index.isValid():
                    self.dataChanged.emit(index, index)
                break

    def set_timeline_preview(self, timeline_id: str, pixmap: QPixmap) -> None:
        """Store a decoded timeline preview and repaint its card."""
        if pixmap is None or pixmap.isNull():
            return
        self._timeline_previews[timeline_id] = pixmap
        for row, item in enumerate(self._items):
            if item.is_timeline and item.item_id == timeline_id:
                index = self.index(row, 0)
                self.dataChanged.emit(index, index, [self.TIMELINE_PREVIEW_ROLE])
                return

    def set_assets(self, assets: List[MediaAsset]) -> None:
        """设置素材列表（向后兼容）"""
        items = [GridItem("asset", a.asset_id, a) for a in assets]
        self.set_items(items)

    def set_thumbnail(self, path: str, pixmap: QPixmap) -> None:
        """设置缩略图"""
        self._thumbnails[path] = pixmap
        # Update every visible reference to this path. A project may contain
        # subclips or duplicated references that intentionally share media.
        source_asset_ids = {
            item.data.asset_id
            for item in self._items
            if item.is_asset
            and isinstance(item.data, MediaAsset)
            and item.data.path == path
        }
        for i, item in enumerate(self._items):
            if item.is_asset and isinstance(item.data, MediaAsset):
                # A scene-cut subclip has its own source frame.  Do not paint
                # the parent's path poster onto every subclip card.
                if item.data.metadata.get("is_subclip"):
                    continue
                is_source = item.data.path == path
                is_comp_preview = (
                    item.data.is_comp_clip
                    and (
                        item.data.metadata.get("original_asset_id", "")
                        in source_asset_ids
                        or item.data.metadata.get("original_asset_path", "") == path
                    )
                )
                if is_source or is_comp_preview:
                    # Keep the source-path cache as well as the comp path
                    # alias so both cards repaint from the same decoded frame.
                    self._thumbnails[item.data.path] = pixmap
                    idx = self.index(i)
                    if idx.isValid():
                        self.dataChanged.emit(idx, idx)

    def set_active_timeline(self, timeline_id: Optional[str]) -> None:
        """
        Set the active timeline ID for highlighting (V-10, UI-27).

        Args:
            timeline_id: Timeline ID to highlight, or None to clear
        """
        if self._active_timeline_id == timeline_id:
            return  # No change

        self._active_timeline_id = timeline_id

        # Emit dataChanged for all timeline items to update their appearance
        for i, item in enumerate(self._items):
            if item.is_timeline:
                idx = self.index(i)
                if idx.isValid():
                    self.dataChanged.emit(idx, idx)


# ================================================================
# Grid View Delegate (Thumbnail Rendering with Badges)
# ================================================================

class GridDelegate(QStyledItemDelegate):
    """
    网格视图自定义委托，负责绘制缩略图和 badges。

    Badges 包括：
    - duration badge（右上角，视频/音频时长）
    - media type badge（左上角，类型图标）
    - resolution/fps badge（底部叠加）
    - offline badge（红色警示，文件不存在）
    - proxy badge（黄色标识，代理已生成）
    """

    # Badge 颜色常量（使用 edit_style.py 颜色）
    BADGE_COLORS = {
        "duration": "#2D4B5F",       # 视频深青蓝
        "audio": "#2D5F3A",          # 音频深绿
        "image": "#7a5c3e",          # 图片棕色
        "offline": "#d65f5f",        # 红色（危险）
        "proxy": "#ba8b45",          # 黄色（代理）
        "selected": "#E29227",       # DaVinci橘黄（选中）
        # M3: 分析状态点（§17.2 badge）。颜色与 host 状态字符串对齐。
        "analysis_ready": "#3a8a4a",     # 绿（已分析）
        "analysis_running": "#3a6a9a",   # 蓝（分析中）
        "analysis_failed": "#ba5a3a",    # 橙红（失败/过期）
        "analysis_pending": "#888888",   # 灰（待确认/排队）
    }

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.thumb_provider = ThumbnailProvider.instance()
        self.thumb_provider.thumbnail_ready.connect(self._on_thumbnail_ready)
        self.thumb_provider.frame_thumbnail_ready.connect(
            self._on_subclip_frame_thumbnail_ready
        )
        self.view: Optional[QListView] = None
        self.thumb_size = (100, 67)  # default: 100x67 (DaVinci compact)
        self._pending_thumbnails: set = set()
        self._pending_subclip_frames: dict[tuple[str, int], set[str]] = {}

    def set_thumb_size(self, size: tuple[int, int]) -> None:
        """设置缩略图尺寸"""
        self.thumb_size = size

    def _get_panel(self) -> Optional["MediaPoolPanel"]:
        """Walk up the parent chain to find the owning MediaPoolPanel."""
        start = self.view if self.view is not None else self.parent()
        parent = start.parent() if start is not None else None
        while parent is not None:
            if isinstance(parent, MediaPoolPanel):
                return parent
            parent = parent.parent()
        return None

    def _resolve_comp_preview_asset(self, asset: MediaAsset) -> MediaAsset:
        """Resolve the source asset used as a Comp Clip poster frame."""
        if not asset.is_comp_clip:
            return asset
        panel = self._get_panel()
        project = getattr(panel, "_project", None)
        source_id = asset.metadata.get("original_asset_id", "")
        source = project.assets.get(source_id) if project and source_id else None
        return source if isinstance(source, MediaAsset) else asset

    def _cached_thumbnail_for_path(self, path: str) -> Optional[QPixmap]:
        """Read a cached thumbnail through the model, including proxy models."""
        if self.view is None:
            return None
        model = self.view.model()
        if isinstance(model, QSortFilterProxyModel):
            model = model.sourceModel()
        getter = getattr(model, "thumbnail_for_path", None)
        return getter(path) if callable(getter) else None

    def _asset_model(self) -> Optional[AssetListModel]:
        """Return the source model even when the grid has a filter proxy."""
        if self.view is None:
            return None
        model = self.view.model()
        if isinstance(model, QSortFilterProxyModel):
            model = model.sourceModel()
        return model if isinstance(model, AssetListModel) else None

    @staticmethod
    def _subclip_frame_request(asset: MediaAsset) -> tuple[int, int] | None:
        """Return ``(poster_frame, parent_total_frames)`` for a subclip."""
        if not asset.metadata.get("is_subclip"):
            return None
        try:
            frame = max(
                0,
                int(asset.metadata.get(
                    "thumbnail_source_frame",
                    asset.metadata.get("source_start", 0),
                )),
            )
            source_start = max(0, int(asset.metadata.get("source_start", 0)))
            source_duration = max(1, int(asset.metadata.get("source_duration", 1)))
            total = int(asset.metadata.get("source_total_frames", 0))
        except (TypeError, ValueError):
            return None
        total = max(total, frame + 1, source_start + source_duration)
        return frame, total

    def _on_subclip_frame_thumbnail_ready(
        self, path: str, frame_index: int, pixmap: QPixmap
    ) -> None:
        """Publish one decoded parent frame to only the matching subclips."""
        asset_ids = self._pending_subclip_frames.pop(
            (path, int(frame_index)), set()
        )
        if not asset_ids:
            return
        model = self._asset_model()
        if model is None:
            return
        for asset_id in asset_ids:
            model.set_asset_thumbnail(asset_id, pixmap)

    def _on_thumbnail_ready(self, path: str, pixmap: QPixmap) -> None:
        """缩略图加载完成回调"""
        if path in self._pending_thumbnails:
            self._pending_thumbnails.remove(path)

        # 更新模型
        if self.view:
            model = self.view.model()
            if isinstance(model, QSortFilterProxyModel):
                source_model = model.sourceModel()
            else:
                source_model = model

            if isinstance(source_model, AssetListModel):
                source_model.set_thumbnail(path, pixmap)

    def createEditor(
        self, parent: QWidget, option: QStyleOptionViewItem, index: QModelIndex
    ) -> Optional[QWidget]:
        """Provide an inline rename editor for bin and asset cards.

        Timelines are not editable, so they return no editor.
        """
        item_type = index.data(AssetListModel.ITEM_TYPE_ROLE)
        if item_type not in ("bin", "asset"):
            return None
        editor = QLineEdit(parent)
        editor.setFrame(False)
        editor.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        # Select all on focus so the user can type over the default name.
        editor.selectAll()
        return editor

    def setEditorData(self, editor: QWidget, index: QModelIndex) -> None:
        """Initialise the editor with the plain name (no count suffix)."""
        if isinstance(editor, QLineEdit):
            name = index.data(Qt.ItemDataRole.EditRole) or ""
            editor.setText(name)
            editor.selectAll()

    def setModelData(
        self, editor: QWidget, model: QAbstractListModel, index: QModelIndex
    ) -> None:
        """Commit the edited name back through the model's setData."""
        if isinstance(editor, QLineEdit):
            model.setData(index, editor.text(), Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(
        self, editor: QWidget, option: QStyleOptionViewItem, index: QModelIndex
    ) -> None:
        """Position the editor over the card's name area (bottom strip).

        Works for both bin cards and asset cards since both draw the name in
        the same bottom strip.
        """
        rect = option.rect
        pad = 3
        card_rect = rect.adjusted(pad, pad, -pad, -pad)
        filename_area_h = 22
        thumb_margin = 2
        thumb_bottom = (
            card_rect.top()
            + thumb_margin
            + (card_rect.height() - filename_area_h - thumb_margin)
        )
        name_rect = QRect(
            card_rect.left() + 4,
            thumb_bottom + 1,
            card_rect.width() - 8,
            filename_area_h,
        )
        editor.setGeometry(name_rect)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index: QModelIndex) -> None:
        """绘制网格项 — DaVinci Resolve 风格简洁卡片

        设计原则：
        - 缩略图占据卡片大部分空间
        - 仅在缩略图右下角显示小巧的时长文字（半透明底条）
        - 底部仅显示文件名，无多余元数据
        - 类型/代理/离线等状态以小色块点缀，不遮挡缩略图
        """
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 获取 item 类型
        item_type = index.data(AssetListModel.ITEM_TYPE_ROLE)

        if item_type == "timeline":
            self._draw_timeline_card(painter, option, index)
            painter.restore()
            return

        if item_type == "bin":
            self._draw_bin_card(painter, option, index)
            painter.restore()
            return

        # 绘制 asset 卡片
        asset = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(asset, MediaAsset):
            painter.restore()
            return

        rect = option.rect
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
        is_hover = bool(option.state & QStyle.StateFlag.State_MouseOver)

        # ---- 卡片边距与圆角 ----
        pad = 3
        card_rect = rect.adjusted(pad, pad, -pad, -pad)
        cr = 4  # card corner radius

        # ---- 1. 卡片底色 ----
        if is_selected:
            painter.setBrush(QBrush(QColor(get_color("accent"))))
            painter.setPen(Qt.PenStyle.NoPen)
        elif is_hover:
            painter.setBrush(QBrush(QColor("#28292d")))
            painter.setPen(QPen(QColor("#3a3b40")))
        else:
            painter.setBrush(QBrush(QColor("#222326")))
            painter.setPen(QPen(QColor("#2c2d31")))
        painter.drawRoundedRect(card_rect, cr, cr)

        # ---- 布局参数 ----
        filename_area_h = 22  # 底部文件名区域高度
        thumb_margin = 2      # 缩略图与卡片边缘的间距
        thumb_rect = QRect(
            card_rect.left() + thumb_margin,
            card_rect.top() + thumb_margin,
            card_rect.width() - thumb_margin * 2,
            card_rect.height() - filename_area_h - thumb_margin
        )

        # ---- 2. 缩略图 ----
        self._draw_thumbnail_area(painter, thumb_rect, asset, index)

        # ---- 3. 小巧的状态指示（不遮挡缩略图主体） ----
        self._draw_compact_indicators(painter, thumb_rect, asset)

        # ---- 4. 文件名（卡片底部，简洁单行） ----
        painter.setPen(QPen(QColor(get_color("text") if is_selected else get_color("text"))))
        font = QFont()
        font.setPixelSize(11)
        font.setBold(False)
        painter.setFont(font)

        name_rect = QRect(
            card_rect.left() + 4,
            thumb_rect.bottom() + 1,
            card_rect.width() - 8,
            filename_area_h
        )
        display_name = self._truncate_filename(asset.display_name, name_rect.width(), font)
        painter.drawText(
            name_rect,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            display_name,
        )

        painter.restore()

    def _draw_timeline_card(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex
    ) -> None:
        """绘制 timeline 卡片

        - 紫色背景，居中白色胶片图标
        - 顶部 TIMELINE 文字标签
        - 激活态：橙色边框 + 右上角激活图标
        - 底部显示 timeline 名称
        """
        timeline = index.data(AssetListModel.TIMELINE_DATA_ROLE)
        if not isinstance(timeline, TimelineSequenceRef):
            return

        is_active = index.data(AssetListModel.IS_ACTIVE_ROLE)
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
        is_hover = bool(option.state & QStyle.StateFlag.State_MouseOver)

        rect = option.rect
        pad = 3
        card_rect = rect.adjusted(pad, pad, -pad, -pad)
        cr = 4

        # ---- 卡片底色 ----
        timeline_color = QColor("#4a3d6e")         # 紫色底
        timeline_hover = QColor("#564880")          # 悬停浅紫
        active_bg = QColor("#5b4b8b")              # 活跃紫色
        active_border = QColor("#E29227")           # 活跃橙色边框

        if is_selected:
            painter.setBrush(QBrush(QColor(get_color("accent"))))
            painter.setPen(Qt.PenStyle.NoPen)
        elif is_active:
            painter.setBrush(QBrush(active_bg))
            painter.setPen(QPen(active_border, 2))
        elif is_hover:
            painter.setBrush(QBrush(timeline_hover))
            painter.setPen(QPen(QColor("#6a5d90")))
        else:
            painter.setBrush(QBrush(timeline_color))
            painter.setPen(QPen(QColor("#5a4d7a")))
        painter.drawRoundedRect(card_rect, cr, cr)

        # ---- 布局参数 ----
        filename_area_h = 22
        thumb_margin = 2
        thumb_rect = QRect(
            card_rect.left() + thumb_margin,
            card_rect.top() + thumb_margin,
            card_rect.width() - thumb_margin * 2,
            card_rect.height() - filename_area_h - thumb_margin,
        )

        # ---- 时间线预览背景（铺满整个缩略图区域） ----
        preview = index.data(AssetListModel.TIMELINE_PREVIEW_ROLE)
        if isinstance(preview, QPixmap) and not preview.isNull():
            scaled = preview.scaled(
                thumb_rect.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            source_rect = QRect(
                max(0, (scaled.width() - thumb_rect.width()) // 2),
                max(0, (scaled.height() - thumb_rect.height()) // 2),
                thumb_rect.width(),
                thumb_rect.height(),
            )
            painter.drawPixmap(thumb_rect, scaled, source_rect)
            painter.fillRect(thumb_rect, QColor(0, 0, 0, 22))
        else:
            painter.setBrush(QBrush(QColor("#3f315c")))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(thumb_rect, 2, 2)

        # ---- TIMELINE 标签（叠加在预览图上方） ----
        font_tl = QFont()
        font_tl.setPixelSize(7)
        font_tl.setBold(True)
        font_tl.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        painter.setFont(font_tl)

        fm = painter.fontMetrics()
        tl_text = "TIMELINE"
        tl_text_w = fm.horizontalAdvance(tl_text)
        badge_pad_x = 7
        badge_pad_y = 2
        badge_w = tl_text_w + badge_pad_x * 2
        badge_h = fm.height() + badge_pad_y * 2
        # 居中于缩略图区域顶部
        badge_x = thumb_rect.center().x() - badge_w // 2
        badge_y = thumb_rect.top() + 4
        badge_rect = QRect(badge_x, badge_y, badge_w, badge_h)

        # 半透明紫色标签，不再占用单独的空白区域。
        painter.setBrush(QBrush(QColor(104, 83, 157, 228)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(badge_rect, 4, 4)

        # 白色文字
        painter.setPen(QPen(QColor("#ffffff")))
        painter.drawText(
            badge_rect,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            tl_text,
        )

        # 胶片图标叠加在预览图左下角，作为时间线类型标记。
        if isinstance(preview, QPixmap) and not preview.isNull():
            film_badge = QRect(thumb_rect.left() + 4, thumb_rect.bottom() - 22, 20, 19)
            painter.setBrush(QColor(0, 0, 0, 150))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(film_badge, 3, 3)
            try:
                qta.icon("fa5s.film", color="#ffffff").paint(
                    painter, film_badge.left() + 2, film_badge.top() + 2, 15, 15
                )
            except Exception:
                painter.setPen(QPen(QColor("#ffffff")))
                painter.drawText(film_badge, Qt.AlignmentFlag.AlignCenter, "▣")
        else:
            # 没有预览源时，在完整紫色背景上显示胶片占位图。
            try:
                timeline_icon = qta.icon("fa5s.film", color="#f4f1ff")
                icon_sz = min(42, max(24, min(thumb_rect.width(), thumb_rect.height()) // 2))
                ix = thumb_rect.center().x() - icon_sz // 2
                iy = thumb_rect.center().y() - icon_sz // 2 + 3
                timeline_icon.paint(painter, ix, iy, icon_sz, icon_sz)
            except Exception:
                painter.setPen(QPen(QColor("#f4f1ff")))
                font_fb = QFont()
                font_fb.setPixelSize(18)
                font_fb.setBold(True)
                painter.setFont(font_fb)
                painter.drawText(thumb_rect, Qt.AlignmentFlag.AlignCenter, "TL")

        # ---- 激活图标（右上角，橙色圆点 + 勾号） ----
        if is_active and not is_selected:
            badge_sz = 16
            bx = card_rect.right() - badge_sz - 2
            by = card_rect.top() + 2

            # 橙色圆底
            painter.setBrush(QBrush(QColor("#E29227")))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(bx, by, badge_sz, badge_sz)

            # 白色勾号
            try:
                check_icon = qta.icon("fa5s.check", color="#ffffff")
                check_margin = 3
                check_icon.paint(
                    painter,
                    bx + check_margin,
                    by + check_margin,
                    badge_sz - check_margin * 2,
                    badge_sz - check_margin * 2,
                )
            except Exception:
                # 备用：手绘勾号
                painter.setPen(QPen(QColor("#ffffff"), 2))
                cx = bx + badge_sz // 2
                cy = by + badge_sz // 2
                painter.drawLine(cx - 4, cy, cx - 1, cy + 3)
                painter.drawLine(cx - 1, cy + 3, cx + 4, cy - 3)

        # ---- 底部名称 ----
        painter.setPen(QPen(QColor(get_color("text"))))
        font = QFont()
        font.setPixelSize(11)
        font.setBold(False)
        painter.setFont(font)

        name_rect = QRect(
            card_rect.left() + 4,
            thumb_rect.bottom() + 1,
            card_rect.width() - 8,
            filename_area_h,
        )
        display_name = self._truncate_filename(timeline.name, name_rect.width(), font)
        painter.drawText(
            name_rect,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            display_name,
        )

    def _draw_bin_card(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex
    ) -> None:
        """绘制 bin 卡片（普通素材箱）

        - 深色中性背景，居中扁平灰色文件夹图标
        - 右下角显示子项数量（asset 数 + 子 bin 数）
        - 底部显示 bin 名称
        """
        bin_obj = index.data(AssetListModel.BIN_DATA_ROLE)
        if not isinstance(bin_obj, ProjectBin):
            return

        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
        is_hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        # Drop-target highlight: accent border when this bin is hovered during
        # an internal drag (clear feedback about the drop destination).
        is_drop_target = False
        panel = self._get_panel()
        if (
            panel is not None
            and getattr(panel, "_drop_target_bin_id", None) == bin_obj.bin_id
        ):
            is_drop_target = True

        rect = option.rect
        pad = 3
        card_rect = rect.adjusted(pad, pad, -pad, -pad)
        cr = 4

        # ---- 卡片底色（中性深灰，更接近 DaVinci 的 Bin 卡片） ----
        bin_color = QColor("#292b2f")
        bin_hover = QColor("#34373c")
        bin_border = QColor("#45484e")
        bin_drop = QColor("#5a4a82")           # 拖拽目标蓝紫
        bin_drop_border = QColor(get_color("accent"))

        if is_selected:
            painter.setBrush(QBrush(QColor(get_color("accent"))))
            painter.setPen(Qt.PenStyle.NoPen)
        elif is_drop_target:
            painter.setBrush(QBrush(bin_drop))
            painter.setPen(QPen(bin_drop_border, 2))
        elif is_hover:
            painter.setBrush(QBrush(bin_hover))
            painter.setPen(QPen(QColor("#6a5a3a")))
        else:
            painter.setBrush(QBrush(bin_color))
            painter.setPen(QPen(bin_border))
        painter.drawRoundedRect(card_rect, cr, cr)

        # ---- 布局参数 ----
        filename_area_h = 22
        thumb_margin = 2
        thumb_rect = QRect(
            card_rect.left() + thumb_margin,
            card_rect.top() + thumb_margin,
            card_rect.width() - thumb_margin * 2,
            card_rect.height() - filename_area_h - thumb_margin,
        )

        # ---- 居中扁平文件夹图标 ----
        icon_area = QRect(
            thumb_rect.left(),
            thumb_rect.top(),
            thumb_rect.width(),
            thumb_rect.height(),
        )
        self._draw_folder_icon(painter, icon_area)

        # ---- 子项数量（右下角，半透明圆角底） ----
        # 取该 bin 的 asset 数 + 子 bin 数（需要从 project 统计，这里用 model 持有的 bin_obj）
        item_count = len(bin_obj.asset_ids)
        if item_count > 0:
            count_text = str(item_count)
            font_count = QFont()
            font_count.setPixelSize(9)
            painter.setFont(font_count)
            cfm = painter.fontMetrics()
            count_w = cfm.horizontalAdvance(count_text)
            count_pad_x = 5
            count_pad_y = 2
            count_bg_w = count_w + count_pad_x * 2
            count_bg_h = cfm.height() + count_pad_y * 2
            count_bg_x = thumb_rect.right() - count_bg_w - 4
            count_bg_y = thumb_rect.bottom() - count_bg_h - 4
            count_rect = QRect(count_bg_x, count_bg_y, count_bg_w, count_bg_h)

            painter.setBrush(QBrush(QColor(0, 0, 0, 160)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(count_rect, 2, 2)

            painter.setPen(QPen(QColor("#ffffff")))
            painter.drawText(
                count_rect,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                count_text,
            )

        # ---- 底部名称 ----
        painter.setPen(QPen(QColor(get_color("text"))))
        font = QFont()
        font.setPixelSize(11)
        font.setBold(False)
        painter.setFont(font)

        name_rect = QRect(
            card_rect.left() + 4,
            thumb_rect.bottom() + 1,
            card_rect.width() - 8,
            filename_area_h,
        )
        display_name = self._truncate_filename(bin_obj.name, name_rect.width(), font)
        painter.drawText(
            name_rect,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            display_name,
        )

    @staticmethod
    def _draw_folder_icon(painter: QPainter, area: QRect) -> None:
        """Draw a restrained, flat folder with a crisp two-tone finish."""
        if area.width() <= 0 or area.height() <= 0:
            return

        desired_height = max(32, min(42, int(area.height() * 0.66)))
        # Keep the body close to a 4:3 silhouette like the reference folder.
        desired_width = int(round(desired_height * 4 / 3))
        folder_height = min(desired_height, max(1, area.height() - 4))
        folder_width = min(desired_width, max(1, area.width() - 4))
        if folder_width < 8 or folder_height < 8:
            return
        x = area.center().x() - folder_width // 2
        y = area.center().y() - folder_height // 2 + 1
        tab_height = max(7, folder_height // 4)
        tab_width = max(20, folder_width // 3)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Back tab: restrained volume without a heavy drop shadow.
        tab_path = QPainterPath()
        tab_path.moveTo(x + 1, y + tab_height + 1)
        tab_path.lineTo(x + 1, y + 4)
        tab_path.lineTo(x + tab_width, y + 4)
        tab_path.lineTo(x + tab_width + 6, y + tab_height + 1)
        tab_path.closeSubpath()
        tab_gradient = QLinearGradient(0, y + 3, 0, y + tab_height + 2)
        tab_gradient.setColorAt(0.0, QColor("#b2b6bd"))
        tab_gradient.setColorAt(1.0, QColor("#7f848c"))
        painter.setBrush(QBrush(tab_gradient))
        painter.setPen(QPen(QColor("#5c6169"), 1))
        painter.drawPath(tab_path)

        # Front body: a very soft vertical gradient keeps the icon refined
        # while avoiding the chunky, overly dimensional look of the old one.
        body_y = y + tab_height - 1
        body_path = QPainterPath()
        body_path.moveTo(x, body_y)
        body_path.lineTo(x + folder_width, body_y)
        body_path.lineTo(x + folder_width, y + folder_height - 2)
        body_path.quadTo(
            x + folder_width, y + folder_height - 1,
            x + folder_width - 2, y + folder_height - 1,
        )
        body_path.lineTo(x + 2, y + folder_height - 1)
        body_path.quadTo(x, y + folder_height - 1, x, y + folder_height - 3)
        body_path.closeSubpath()
        body_gradient = QLinearGradient(0, body_y, 0, y + folder_height)
        body_gradient.setColorAt(0.0, QColor("#a0a5ad"))
        body_gradient.setColorAt(0.45, QColor("#858a93"))
        body_gradient.setColorAt(1.0, QColor("#6a7079"))
        painter.setBrush(QBrush(body_gradient))
        painter.setPen(QPen(QColor("#454a52"), 1))
        painter.drawPath(body_path)

        painter.setPen(QPen(QColor(232, 235, 239, 145), 1))
        painter.drawLine(x + 2, body_y + 2, x + folder_width - 2, body_y + 2)
        painter.setPen(QPen(QColor(48, 52, 58, 90), 1))
        painter.drawLine(x + 2, y + folder_height - 2, x + folder_width - 3, y + folder_height - 2)
        painter.restore()

    def _draw_thumbnail_area(self, painter: QPainter, rect: QRect,
                              asset: MediaAsset, index: QModelIndex) -> None:
        """绘制缩略图区域"""
        is_subclip = bool(asset.metadata.get("is_subclip"))
        # Hover-scrub: if the mouse is over this video card and a scrub frame
        # is cached, show that frame instead of the static poster.
        hover_pixmap = None
        if (
            self.view is not None
            and isinstance(self.view, AssetListView)
            and asset.media_type == "video"
            and not is_subclip
        ):
            hover_pixmap = self.view.hover_frame_for(asset.path)
        if hover_pixmap is not None:
            self._draw_thumbnail(painter, rect, hover_pixmap)
            return
        cached_pixmap = index.data(Qt.ItemDataRole.UserRole + 1)

        if is_subclip:
            # Virtual subclips all point to the same physical file.  Decode
            # the poster at each subclip's own source frame and keep the
            # result keyed by asset id so one completion cannot repaint every
            # sibling card with the same image.
            request = self._subclip_frame_request(asset)
            if cached_pixmap is not None:
                self._draw_thumbnail(painter, rect, cached_pixmap)
                return
            if request is not None:
                frame_index, total_frames = request
                key = (asset.path, frame_index)
                self._pending_subclip_frames.setdefault(key, set()).add(asset.asset_id)
                if len(self._pending_subclip_frames[key]) == 1:
                    self.thumb_provider.get_frame_thumbnail(
                        asset.path,
                        frame_index,
                        total_frames,
                        max(self.thumb_size),
                        asset_id=asset.asset_id,
                    )
            painter.setBrush(QBrush(QColor("#1a1b1e")))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 2, 2)
            painter.setPen(QPen(QColor(get_color("text_muted"))))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "…")
            return

        preview_asset = self._resolve_comp_preview_asset(asset)
        if asset.is_comp_clip and preview_asset.asset_id != asset.asset_id:
            cached_pixmap = self._cached_thumbnail_for_path(preview_asset.path)

        if cached_pixmap:
            self._draw_thumbnail(painter, rect, cached_pixmap)
            if asset.is_comp_clip:
                # Keep the source poster recognizable while making it clear
                # that the card represents the composite graph.
                painter.fillRect(rect, QColor(77, 54, 128, 34))
        elif asset.is_motion_graphic:
            # Motion Graphic assets are scene-backed and intentionally have no
            # filesystem thumbnail.  Keep the card visibly distinct from a
            # rendered video while communicating that it is ready to render.
            painter.setBrush(QBrush(QColor("#24183a")))
            painter.setPen(QPen(QColor("#b28be8"), 1))
            painter.drawRoundedRect(rect, 2, 2)
            painter.setPen(QPen(QColor("#eadcff")))
            font = painter.font()
            font.setPixelSize(max(rect.width() // 7, 10))
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "MOTION\nGRAPHIC")
        elif asset.is_comp_clip:
            # Timeline-backed Comp Clip: request the original source poster
            # through the normal async thumbnail provider.  Empty Comp Clips
            # have no source and keep the graph placeholder.
            if preview_asset.asset_id != asset.asset_id:
                if preview_asset.path not in self._pending_thumbnails:
                    self._pending_thumbnails.add(preview_asset.path)
                    self.thumb_provider.get_thumbnail(
                        preview_asset.path, max(self.thumb_size)
                    )
            painter.setBrush(QBrush(QColor("#2a1a3a")))  # Dark purple bg
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 2, 2)
            painter.setPen(QPen(QColor("#8b45a6")))
            font = painter.font()
            font.setPixelSize(max(rect.width() // 5, 10))
            font.setBold(False)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "COMP")
        elif asset.is_adjustment:
            # Adjustment Clip: purple placeholder with ADJ icon (matches the
            # timeline clip gradient). No disk file → never request a thumbnail.
            painter.setBrush(QBrush(QColor("#221432")))  # Dark purple bg
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 2, 2)
            painter.setPen(QPen(QColor("#9a6bd8")))
            font = painter.font()
            font.setPixelSize(max(rect.width() // 5, 10))
            font.setBold(False)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "ADJ")
        elif asset.is_model3d:
            # 3D models do not have a 2D thumbnail until Model3D imports them.
            # Give the asset a clear, draggable placeholder instead of
            # sending it through the image/video thumbnail decoder.
            painter.setBrush(QBrush(QColor("#21183a")))
            painter.setPen(QPen(QColor("#a978db"), 1))
            painter.drawRoundedRect(rect, 2, 2)
            painter.setPen(QPen(QColor("#eadcff")))
            font = painter.font()
            font.setPixelSize(max(rect.width() // 6, 10))
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(
                rect, Qt.AlignmentFlag.AlignCenter,
                I18N.tr("edit.media_pool.model_3d", "3D MODEL"),
            )
        elif asset.media_type == "solid":
            # Solid Color: fill the rect with the asset's colour so the media
            # pool previews the actual colour. No disk file → no thumbnail load.
            rgb = asset.solid_color or (0.0, 0.6, 0.8)
            fill = QColor.fromRgbF(
                max(0.0, min(1.0, rgb[0])),
                max(0.0, min(1.0, rgb[1])),
                max(0.0, min(1.0, rgb[2])),
            )
            painter.setBrush(QBrush(fill))
            painter.setPen(QPen(QColor(0, 0, 0, 80)))
            painter.drawRoundedRect(rect, 2, 2)
        elif asset.is_subtitle:
            # Subtitle files are text assets; do not ask the thumbnail
            # provider to decode them as video/image media.
            painter.setBrush(QBrush(QColor("#202A3A")))
            painter.setPen(QPen(QColor("#72A7E8"), 1))
            painter.drawRoundedRect(rect, 2, 2)
            painter.setPen(QPen(QColor("#D9E9FF")))
            font = painter.font()
            font.setPixelSize(max(rect.width() // 5, 10))
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "SUB")
        else:
            # 如果不在待加载列表，请求加载
            if asset.path not in self._pending_thumbnails:
                self._pending_thumbnails.add(asset.path)
                self.thumb_provider.get_thumbnail(asset.path, max(self.thumb_size))

            # 绘制占位符 — 简洁深色底
            painter.setBrush(QBrush(QColor("#1a1b1e")))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 2, 2)

            # 小巧的加载提示
            painter.setPen(QPen(QColor(get_color("text_muted"))))
            font = QFont()
            font.setPixelSize(10)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "…")

    def _draw_thumbnail(self, painter: QPainter, rect: QRect,
                        pixmap: QPixmap) -> None:
        """绘制缩略图（居中裁剪）"""
        pw, ph = pixmap.width(), pixmap.height()
        tw, th = rect.width(), rect.height()

        # 保持宽高比，居中裁剪
        aspect = pw / ph
        target_aspect = tw / th

        if aspect > target_aspect:
            src_h = ph
            src_w = int(ph * target_aspect)
            src_x = (pw - src_w) // 2
            src_y = 0
        else:
            src_w = pw
            src_h = int(pw / target_aspect)
            src_x = 0
            src_y = (ph - src_h) // 2

        src_rect = QRect(src_x, src_y, src_w, src_h)
        painter.drawPixmap(rect, pixmap, src_rect)

    # ---- DaVinci Resolve 风格：紧凑状态指示 ----

    def _draw_compact_indicators(
        self, painter: QPainter, rect: QRect, asset: MediaAsset
    ) -> None:
        """在缩略图上绘制小巧的状态指示 — DaVinci 风格

        - 时长：右下角半透明底条 + 小字
        - 离线：左上角小红点
        - 代理：左上角小黄点（紧邻离线点右侧）
        """
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # ---- 时长条（右下角，半透明） ----
        duration_text = self._get_duration_badge_text(asset)
        if duration_text and getattr(self, "show_duration_badge", True):
            font = QFont()
            font.setPixelSize(9)
            font.setBold(False)
            painter.setFont(font)
            fm = painter.fontMetrics()
            tw = fm.horizontalAdvance(duration_text)

            bar_h = 14
            bar_pad = 3
            bar_w = tw + bar_pad * 2
            bar_x = rect.right() - bar_w - 2
            bar_y = rect.bottom() - bar_h - 2

            # 半透明黑底
            painter.setBrush(QBrush(QColor(0, 0, 0, 160)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(QRect(bar_x, bar_y, bar_w, bar_h), 2, 2)

            # 白色文字
            painter.setPen(QPen(QColor(230, 230, 230)))
            painter.drawText(
                QRect(bar_x, bar_y, bar_w, bar_h),
                Qt.AlignmentFlag.AlignCenter,
                duration_text,
            )

        # ---- 离线红点（左上角） ----
        dot_size = 6
        dot_margin = 3
        if asset.is_offline:
            dx = rect.left() + dot_margin
            dy = rect.top() + dot_margin
            painter.setBrush(QBrush(QColor(self.BADGE_COLORS["offline"])))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(dx, dy, dot_size, dot_size)

        # ---- 代理黄点（左上角偏右） ----
        if asset.has_proxy:
            dx = rect.left() + dot_margin + (dot_size + 2 if asset.is_offline else 0)
            dy = rect.top() + dot_margin
            painter.setBrush(QBrush(QColor(self.BADGE_COLORS["proxy"])))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(dx, dy, dot_size, dot_size)

        # ---- M3: 分析状态点（左下角，§17.2 badge）----
        # 与左上角 offline/proxy/type badge 错开；颜色由 host 状态字符串映射。
        # 状态仅从 panel 的 _analysis_status_cache 读（delegate 经 _get_panel 访问）。
        if asset.is_comp_clip:
            self._draw_comp_clip_icon(painter, rect)
        self._draw_analysis_status_dot(painter, rect, asset)

        painter.restore()

    def _draw_badges(self, painter: QPainter, rect: QRect,
                     asset: MediaAsset) -> None:
        """绘制 badges（右上角和左上角）"""
        badge_height = 18
        badge_padding = 4

        # ========== 右上角 badges ==========

        # Offline badge（红色警示，最高优先级）
        if asset.is_offline:
            self._draw_badge(
                painter,
                rect.right() - 60 - badge_padding,
                rect.top() + badge_padding,
                60,
                badge_height,
                "OFFLINE",
                self.BADGE_COLORS["offline"],
                "#ffffff"
            )
        else:
            # Duration badge（视频/音频时长）
            duration_badge_text = self._get_duration_badge_text(asset)
            if duration_badge_text:
                duration_badge_width = self._calculate_badge_width(duration_badge_text, 6)
                self._draw_badge(
                    painter,
                    rect.right() - duration_badge_width - badge_padding,
                    rect.top() + badge_padding,
                    duration_badge_width,
                    badge_height,
                    duration_badge_text,
                    self.BADGE_COLORS["duration"],
                    "#ffffff"
                )

        # ========== 左上角 badges ==========

        # Media type badge
        type_badge_text = self._get_type_badge_text(asset)
        type_badge_color = self._get_type_badge_color(asset)
        if type_badge_text:
            type_badge_width = self._calculate_badge_width(type_badge_text, 8)
            self._draw_badge(
                painter,
                rect.left() + badge_padding,
                rect.top() + badge_padding,
                type_badge_width,
                badge_height,
                type_badge_text,
                type_badge_color,
                "#ffffff"
            )

        # Proxy badge（黄色标识，左上角偏移）
        if asset.has_proxy:
            proxy_badge_width = self._calculate_badge_width("PROXY", 6)
            self._draw_badge(
                painter,
                rect.left() + badge_padding,
                rect.top() + badge_padding + badge_height + 2,
                proxy_badge_width,
                badge_height,
                "PROXY",
                self.BADGE_COLORS["proxy"],
                "#ffffff"
            )

    def _draw_badge(self, painter: QPainter, x: int, y: int,
                    width: int, height: int, text: str,
                    bg_color: str, text_color: str) -> None:
        """绘制单个 badge"""
        # 绘制背景
        painter.setBrush(QBrush(QColor(bg_color)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(QRect(x, y, width, height), 4, 4)

        # 绘制文字
        painter.setPen(QPen(QColor(text_color)))
        font = QFont()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)

        badge_rect = QRect(x, y, width, height)
        painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, text)

    def _calculate_badge_width(self, text: str, padding: int) -> int:
        """计算 badge 宽度"""
        # 简单估算：每个字符约 7px + padding
        char_width = 7
        return len(text) * char_width + padding * 2

    # 分析状态字符串 → badge 颜色 key（与 host 的 STATUS_* 对齐，§17.2）
    _ANALYSIS_STATUS_COLOR_KEY = {
        "ready": "analysis_ready",
        "running": "analysis_running",
        "failed": "analysis_failed",
        "stale": "analysis_failed",
        "pending": "analysis_pending",
    }

    def _draw_analysis_status_dot(
        self, painter: QPainter, rect: QRect, asset: MediaAsset,
    ) -> None:
        """M3：绘制分析状态点（左下角，§17.2 badge）。

        状态从所属 panel 的 ``_analysis_status_cache`` 读取；panel 未启用分析或
        asset 无记录时不画。颜色映射见 :data:`_ANALYSIS_STATUS_COLOR_KEY`。
        """
        panel = self._get_panel()
        if panel is None or not getattr(panel, "_media_analysis_enabled", False):
            return
        cache = getattr(panel, "_analysis_status_cache", {})
        status = cache.get(getattr(asset, "asset_id", ""), "")
        color_key = self._ANALYSIS_STATUS_COLOR_KEY.get(status)
        if color_key is None:
            return  # unknown/无记录：不画点
        dot_size = 6
        dot_margin = 3
        dx = rect.left() + dot_margin + (24 if asset.is_comp_clip else 0)
        dy = rect.bottom() - dot_margin - dot_size
        painter.setBrush(QBrush(QColor(self.BADGE_COLORS[color_key])))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(dx, dy, dot_size, dot_size)

    def _draw_comp_clip_icon(self, painter: QPainter, rect: QRect) -> None:
        """Draw the small Comp Clip marker in the thumbnail's lower-left."""
        badge = QRect(rect.left() + 3, rect.bottom() - 21, 20, 18)
        painter.save()
        painter.setBrush(QBrush(QColor(35, 20, 58, 210)))
        painter.setPen(QPen(QColor("#a978db"), 1))
        painter.drawRoundedRect(badge, 3, 3)
        try:
            qta.icon("fa5s.layer-group", color="#f0e7ff").paint(
                painter, badge.left() + 3, badge.top() + 2, 14, 14
            )
        except Exception:
            # Font Awesome is optional in minimal test/runtime environments;
            # use a simple stacked-layer fallback with the same silhouette.
            painter.setPen(QPen(QColor("#f0e7ff"), 1.2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRect(badge.left() + 5, badge.top() + 4, 9, 5), 1, 1)
            painter.drawRoundedRect(QRect(badge.left() + 7, badge.top() + 8, 9, 5), 1, 1)
        painter.restore()

    def _get_duration_badge_text(self, asset: MediaAsset) -> str:
        """获取时长 badge 文本"""
        if asset.media_type == "image":
            return ""

        duration = asset.duration_seconds
        if duration is None:
            return ""

        return self._format_duration_short(duration)

    def _get_type_badge_text(self, asset: MediaAsset) -> str:
        """获取类型 badge 文本"""
        if asset.is_motion_graphic:
            return "MOTION"
        if asset.is_comp_clip:
            return "COMP"
        elif asset.is_adjustment:
            return "ADJ"
        elif asset.media_type == "solid":
            return "SOLID"
        elif asset.is_model3d:
            return "3D"
        elif asset.is_video:
            return "VIDEO"
        elif asset.is_image:
            return "IMAGE"
        elif asset.is_audio:
            return "AUDIO"
        elif asset.is_subtitle:
            return "SUB"
        return ""

    def _get_type_badge_color(self, asset: MediaAsset) -> str:
        """获取类型 badge 颜色"""
        if asset.is_motion_graphic:
            return "#6f4aa8"  # Scene-backed Motion Graphic
        if asset.is_comp_clip:
            return "#8b45a6"  # Purple for comp clips
        elif asset.is_adjustment:
            return "#9a6bd8"  # Lighter purple for adjustment clips
        elif asset.media_type == "solid":
            return "#7a7a8a"  # Slate grey for solid color clips
        elif asset.is_model3d:
            return "#6f4aa8"  # Purple for scene-backed model assets
        elif asset.is_video:
            return self.BADGE_COLORS["duration"]
        elif asset.is_image:
            return self.BADGE_COLORS["image"]
        elif asset.is_audio:
            return self.BADGE_COLORS["audio"]
        elif asset.is_subtitle:
            return "#4D78A8"
        return "#555555"

    def _format_duration_short(self, seconds: float) -> str:
        """格式化短时长显示"""
        if seconds < 1:
            return f"{seconds * 1000:.0f}ms"
        elif seconds < 60:
            return f"{seconds:.1f}s"
        elif seconds < 3600:
            mins = int(seconds // 60)
            secs = int(seconds % 60)
            return f"{mins}:{secs:02d}"
        else:
            hours = int(seconds // 3600)
            mins = int((seconds % 3600) // 60)
            secs = int(seconds % 60)
            return f"{hours}:{mins:02d}:{secs:02d}"

    def _draw_overlay(self, painter: QPainter, bg_rect: QRect,
                      thumb_rect: QRect, asset: MediaAsset) -> None:
        """绘制底部叠加层（文件名和元数据）"""
        overlay_height = 50

        # 计算叠加层区域（从缩略图底部开始）
        overlay_rect = QRect(
            bg_rect.left(), thumb_rect.bottom(),
            bg_rect.width(), overlay_height
        )

        # 渐变背景
        gradient = QLinearGradient(
            float(overlay_rect.left()), float(overlay_rect.top()),
            float(overlay_rect.left()), float(overlay_rect.bottom())
        )
        gradient.setColorAt(0, QColor(0, 0, 0, 200))
        gradient.setColorAt(1, QColor(0, 0, 0, 240))

        painter.setBrush(QBrush(gradient))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(overlay_rect)

        # 绘制文件名
        painter.setPen(QPen(QColor(get_color("text"))))
        font = QFont()
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)

        filename_rect = QRect(
            overlay_rect.left() + 6, overlay_rect.top() + 4,
            overlay_rect.width() - 12, 18
        )

        # 截断文件名（如果太长）
        display_name = self._truncate_filename(asset.display_name, filename_rect.width(), font)
        painter.drawText(filename_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                         display_name)

        # 绘制元数据行（分辨率/FPS/编码）
        font.setBold(False)
        font.setPointSize(8)
        painter.setFont(font)
        painter.setPen(QPen(QColor(get_color("text_muted"))))

        meta_rect = QRect(
            overlay_rect.left() + 6, overlay_rect.top() + 24,
            overlay_rect.width() - 12, 18
        )

        meta_text = self._get_metadata_text(asset)
        painter.drawText(meta_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, meta_text)

    def _truncate_filename(self, name: str, max_width: int, font: QFont) -> str:
        """截断文件名以适应宽度"""
        return QFontMetrics(font).elidedText(name, Qt.TextElideMode.ElideMiddle, max(0, max_width))

    def _get_metadata_text(self, asset: MediaAsset) -> str:
        """获取元数据文本（用于底部显示）"""
        parts = []

        # 分辨率
        if asset.width and asset.height:
            parts.append(f"{asset.width}×{asset.height}")

        # 帧率（视频）
        if asset.fps:
            parts.append(f"{asset.fps:.2f}fps")

        # 编码
        codec = asset.codec
        if codec:
            parts.append(codec.upper())

        # 音频声道
        if asset.audio_channels:
            parts.append(asset.audio_channels_str)

        return " | ".join(parts) if parts else "-"

    def _draw_timeline_overlay(
        self, painter: QPainter, bg_rect: QRect,
        thumb_rect: QRect, timeline: TimelineSequenceRef
    ) -> None:
        """绘制 timeline 卡片的底部叠加层（V-08, UI-28）"""
        overlay_height = 50

        # 计算叠加层区域
        overlay_rect = QRect(
            bg_rect.left(), thumb_rect.bottom(),
            bg_rect.width(), overlay_height
        )

        # 渐变背景
        gradient = QLinearGradient(
            float(overlay_rect.left()), float(overlay_rect.top()),
            float(overlay_rect.left()), float(overlay_rect.bottom())
        )
        gradient.setColorAt(0, QColor(0, 0, 0, 200))
        gradient.setColorAt(1, QColor(0, 0, 0, 240))

        painter.setBrush(QBrush(gradient))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(overlay_rect)

        # 绘制 timeline 名称
        painter.setPen(QPen(QColor(get_color("text"))))
        font = QFont()
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)

        filename_rect = QRect(
            overlay_rect.left() + 6, overlay_rect.top() + 4,
            overlay_rect.width() - 12, 18
        )

        display_name = self._truncate_filename(timeline.name, filename_rect.width(), font)
        painter.drawText(
            filename_rect,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            display_name
        )

        # 绘制元数据行（分辨率/FPS）
        font.setBold(False)
        font.setPointSize(8)
        painter.setFont(font)
        painter.setPen(QPen(QColor(get_color("text_muted"))))

        meta_rect = QRect(
            overlay_rect.left() + 6, overlay_rect.top() + 24,
            overlay_rect.width() - 12, 18
        )

        meta_parts = [
            f"{timeline.width}×{timeline.height}",
            f"{timeline.fps:.2f}fps",
        ]
        meta_text = " | ".join(meta_parts)
        painter.drawText(
            meta_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            meta_text
        )

    def _format_duration(self, seconds: float) -> str:
        """格式化时长"""
        if seconds < 60:
            return f"{seconds:.1f}s"
        else:
            mins = int(seconds // 60)
            secs = seconds % 60
            return f"{mins}:{secs:04.1f}"

    def sizeHint(self, option: QStyleOptionViewItem,
                 index: QModelIndex) -> QSize:
        """尺寸提示 — DaVinci 紧凑卡片"""
        # card: thumb + 2px margin top + 22px filename + 3px pad bottom + 3+3 outer pad
        return QSize(self.thumb_size[0] + 10, self.thumb_size[1] + 34)


# ================================================================
# Custom Tree Widget with Drag Support
# ================================================================

class AssetTreeWidget(QTreeWidget):
    """
    Custom tree widget that provides drag support for media assets.

    Overrides startDrag to create custom MIME data with asset IDs.
    Also accepts file drops from OS and forwards to parent MediaPoolPanel.

    Manual drag handling via mousePressEvent/mouseMoveEvent to ensure
    drag operation starts correctly (bypassing default selection behavior).
    """

    # Qt 默认拖拽阈值
    DRAG_THRESHOLD = 4

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._project: Optional[EditProject] = None
        # 拖拽状态
        self._drag_start_pos: Optional[QPoint] = None
        self._drag_item: Optional[QTreeWidgetItem] = None
        self._is_dragging: bool = False
        # Inline rename only via programmatic editItem() (F2 / menu), never on
        # double-click (double-click opens/enters the item).
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

    def set_project(self, project: Optional[EditProject]) -> None:
        """Set the project reference for accessing asset data."""
        self._project = project

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Forward Delete/Backspace to the owning MediaPoolPanel for batch delete.

        F2 starts inline rename on the selected bin/asset (Finder-style).
        Return/Enter opens the selected asset (double-click equivalent) or
        renames a bin. Cmd+↓ enters the selected bin, Cmd+↑ goes up.
        Cmd/Ctrl+A selects all items. Escape clears the selection.
        """
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            panel = self._find_media_pool_panel()
            if panel is not None and panel.handle_delete_key_from(self):
                event.accept()
                return
        # F2 — rename selected bin/asset (Finder-style).
        if event.key() == Qt.Key.Key_F2:
            item = self.currentItem()
            if item is not None and item.data(0, Qt.ItemDataRole.UserRole):
                item_type = item.data(0, Qt.ItemDataRole.UserRole + 1)
                if item_type in ("bin", "asset"):
                    self.editItem(item, 0)
                    event.accept()
                    return
        # Return / Enter — open asset, or rename bin.
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            item = self.currentItem()
            if item is not None and item.data(0, Qt.ItemDataRole.UserRole):
                item_type = item.data(0, Qt.ItemDataRole.UserRole + 1)
                item_id = item.data(0, Qt.ItemDataRole.UserRole)
                if item_type == "bin":
                    self.editItem(item, 0)
                    event.accept()
                    return
                if item_type == "asset":
                    panel = self._find_media_pool_panel()
                    if panel is not None:
                        # Generator clips (Adjustment / Solid) have no decodable
                        # source — skip Source Viewer (same as double-click).
                        asset = panel.asset_model.get_asset(item_id) if hasattr(panel.asset_model, 'get_asset') else None
                        if asset is not None and (getattr(asset, 'is_adjustment', False) or getattr(asset, 'media_type', '') == 'solid'):
                            logger.debug(f"Generator asset Enter (no source viewer): {item_id}")
                        else:
                            panel.asset_double_clicked.emit(item_id)
                    event.accept()
                    return
        # Cmd/Ctrl+↓ — enter the selected bin.
        if event.key() == Qt.Key.Key_Down and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
            or event.modifiers() & Qt.KeyboardModifier.MetaModifier
        ):
            item = self.currentItem()
            if (
                item is not None
                and item.data(0, Qt.ItemDataRole.UserRole + 1) == "bin"
            ):
                panel = self._find_media_pool_panel()
                if panel is not None:
                    panel._enter_bin(item.data(0, Qt.ItemDataRole.UserRole))
                event.accept()
                return
        # Cmd/Ctrl+↑ — navigate up to the parent bin.
        if event.key() == Qt.Key.Key_Up and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
            or event.modifiers() & Qt.KeyboardModifier.MetaModifier
        ):
            panel = self._find_media_pool_panel()
            if panel is not None and panel._navigation_stack:
                panel._navigate_up()
                event.accept()
                return
        # Cmd/Ctrl+] — navigate forward (re-enter a bin left via Cmd+↑).
        if event.key() == Qt.Key.Key_BracketRight and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
            or event.modifiers() & Qt.KeyboardModifier.MetaModifier
        ):
            panel = self._find_media_pool_panel()
            if panel is not None and panel._navigation_forward_stack:
                panel._navigate_forward()
                event.accept()
                return
        # Clipboard shortcuts (Cmd/Ctrl+C/X/V, Cmd/Ctrl+D duplicate).
        panel = self._find_media_pool_panel()
        mod = (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
            or event.modifiers() & Qt.KeyboardModifier.MetaModifier
        )
        if panel is not None and mod:
            if event.key() == Qt.Key.Key_C:
                panel._copy_selected_assets()
                event.accept()
                return
            if event.key() == Qt.Key.Key_X:
                panel._cut_selected_assets()
                event.accept()
                return
            if event.key() == Qt.Key.Key_V:
                panel._paste_assets()
                event.accept()
                return
            if event.key() == Qt.Key.Key_D:
                panel._duplicate_selected_assets()
                event.accept()
                return
        # Cmd/Ctrl+A — select all (Finder/NLE standard).
        if event.key() == Qt.Key.Key_A and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
            or event.modifiers() & Qt.KeyboardModifier.MetaModifier
        ):
            self.selectAll()
            event.accept()
            return
        # Space — toggle Quick Look on the selected asset (Finder behavior).
        if event.key() == Qt.Key.Key_Space and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            panel = self._find_media_pool_panel()
            if panel is not None:
                panel._show_quick_look()
                event.accept()
                return
        # Escape — clear selection (Finder behavior).
        if event.key() == Qt.Key.Key_Escape:
            self.clearSelection()
            event.accept()
            return
        super().keyPressEvent(event)

    def _on_item_data_changed(self, item: QTreeWidgetItem, column: int) -> None:
        """Handle inline-edit commits: route bin/asset renames to the panel."""
        panel = self._find_media_pool_panel()
        if panel is None or panel._updating:
            return
        if column != 0:
            return
        item_type = item.data(0, Qt.ItemDataRole.UserRole + 1)
        item_id = item.data(0, Qt.ItemDataRole.UserRole)
        if not item_id:
            return
        if item_type == "bin":
            new_name = item.data(0, Qt.ItemDataRole.EditRole) or ""
            panel._apply_bin_rename(item_id, new_name)
        elif item_type == "asset":
            # Asset display name has no count suffix — the edited text (column
            # 0) is the new name directly. EditRole falls back to text(0).
            new_name = item.data(0, Qt.ItemDataRole.EditRole) or item.text(0) or ""
            panel._apply_asset_rename(item_id, new_name)

    def _find_media_pool_panel(self) -> Optional[MediaPoolPanel]:
        """Walk up the parent chain to find the owning MediaPoolPanel."""
        parent = self.parent()
        while parent is not None:
            if isinstance(parent, MediaPoolPanel):
                return parent
            parent = parent.parent()
        return None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """
        Record drag start position on left button press.
        This allows us to manually initiate drag when mouse moves.
        """
        if event.button() == Qt.MouseButton.RightButton:
            item = self.itemAt(event.pos())
            if item is not None and item.isSelected():
                self._context_menu_selection_ids = tuple(
                    selected.data(0, Qt.ItemDataRole.UserRole)
                    for selected in self.selectedItems()
                    if selected.data(0, Qt.ItemDataRole.UserRole + 1) == "asset"
                    and selected.data(0, Qt.ItemDataRole.UserRole)
                )
            else:
                self._context_menu_selection_ids = ()
        if event.button() == Qt.MouseButton.LeftButton:
            # 记录起始位置和当前 item
            self._drag_start_pos = event.pos()
            self._drag_item = self.itemAt(event.pos())
            self._is_dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """
        Manually initiate drag when mouse moves beyond threshold.
        This bypasses QTreeWidget's default selection behavior that
        may intercept drag gestures.
        """
        # 如果已经在拖拽中，不处理
        if self._is_dragging:
            return

        if (
            event.buttons() & Qt.MouseButton.LeftButton
            and self._drag_start_pos is not None
            and self._drag_item is not None
        ):
            # 计算移动距离
            distance = (event.pos() - self._drag_start_pos).manhattanLength()
            if distance >= self.DRAG_THRESHOLD:
                # 检查 item 是否可拖拽（asset / timeline 都支持拖入 bin）
                item_type = self._drag_item.data(0, Qt.ItemDataRole.UserRole + 1)
                if item_type in ("asset", "timeline"):
                    self._is_dragging = True
                    # 直接调用 Qt 标准的 startDrag 方法
                    self.startDrag(Qt.DropAction.CopyAction)
                    # 清除状态
                    self._drag_start_pos = None
                    self._drag_item = None
                    self._is_dragging = False
                    return  # 关键：不调用 super，防止选择行为
        else:
            # 没有左键按下或没有起始位置，清除状态
            self._drag_start_pos = None
            self._drag_item = None

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """Clear drag state on mouse release."""
        self._drag_start_pos = None
        self._drag_item = None
        self._is_dragging = False
        super().mouseReleaseEvent(event)

    def _find_panel(self) -> Optional[MediaPoolPanel]:
        """Find the parent MediaPoolPanel."""
        parent = self.parent()
        while parent:
            if isinstance(parent, MediaPoolPanel):
                return parent
            parent = parent.parent()
        return None


    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        mime = event.mimeData()
        if _is_internal_media_pool_drag(mime) or mime.hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        mime = event.mimeData()
        if _is_internal_media_pool_drag(mime):
            # Internal drag: only accept when hovering over a bin row so the
            # cursor gives honest feedback (drops elsewhere are ignored).
            panel = self._find_panel()
            if panel is not None:
                pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
                target_bin_id = panel._target_bin_at(pos, source="tree")
                if target_bin_id is not None:
                    panel._set_drop_target_bin(target_bin_id)
                    event.acceptProposedAction()
                    return
            if panel is not None:
                panel._set_drop_target_bin(None)
            event.ignore()
            return
        if mime.hasUrls():
            # External OS file drop — highlight the bin row under the cursor so
            # the user sees where files will land (Finder-style).
            panel = self._find_panel()
            if panel is not None:
                pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
                target_bin_id = panel._target_bin_at(pos, source="tree")
                panel._set_drop_target_bin(target_bin_id)
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dragLeaveEvent(self, event: Any) -> None:
        panel = self._find_panel()
        if panel is not None:
            panel._set_drop_target_bin(None)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        mime = event.mimeData()
        # Internal drag takes priority: drop assets/timelines into the bin row
        # under the cursor. (Checked before hasUrls() because internal asset
        # drags also carry file URLs as a fallback.)
        if _is_internal_media_pool_drag(mime):
            panel = self._find_panel()
            if panel is not None:
                pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
                target_bin_id = panel._target_bin_at(pos, source="tree")
                if target_bin_id is not None:
                    asset_ids, timeline_ids = parse_media_pool_drag_mime(mime)
                    # P0-5：Alt/Option = 拷贝引用（多箱共存）；普通拖拽 = 移动。
                    handled = panel._handle_drop_into_bin(
                        target_bin_id, asset_ids, timeline_ids,
                        copy=bool(
                            event.modifiers() & Qt.KeyboardModifier.AltModifier),
                    )
                    if handled:
                        panel._set_drop_target_bin(None)
                        event.acceptProposedAction()
                        return
            if panel is not None:
                panel._set_drop_target_bin(None)
            event.ignore()
            return

        if mime.hasUrls():
            parent = self.parent()
            while parent:
                if isinstance(parent, MediaPoolPanel):
                    pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
                    target_bin_id = parent._target_bin_at(pos, source="tree")
                    parent._set_drop_target_bin(None)
                    parent.dropEvent(event, target_bin_id=target_bin_id)
                    event.acceptProposedAction()
                    return
                parent = parent.parent()
            event.ignore()
            return

        super().dropEvent(event)

    def startDrag(self, supportedActions: Qt.DropAction) -> None:
        """
        Start drag operation with custom MIME data.

        Creates MIME data containing:
        - asset_ids: List of asset IDs being dragged (JSON format)
        - timeline_ids: List of timeline IDs being dragged (JSON format)
        - urls: File URLs for fallback support
        - thumbnail: Preview pixmap for drag visualization
        """
        selected_items = self.selectedItems()
        if not selected_items:
            return

        # 使用 panel 的 _project（确保同步）
        panel = self._find_panel()
        project = panel._project if panel else self._project
        if not project:
            return

        # Collect asset IDs/paths and timeline IDs from selected items.
        # Bins are intentionally skipped — they cannot be dragged.
        asset_ids: List[str] = []
        asset_paths: List[str] = []
        assets: List[MediaAsset] = []
        timeline_ids: List[str] = []

        for item in selected_items:
            item_type = item.data(0, Qt.ItemDataRole.UserRole + 1)
            item_id = item.data(0, Qt.ItemDataRole.UserRole)
            if not item_id:
                continue
            if item_type == "asset":
                asset = project.assets.get(item_id)
                if asset:
                    asset_ids.append(item_id)
                    asset_paths.append(asset.path)
                    assets.append(asset)
            elif item_type == "timeline":
                if item_id in project.timelines:
                    timeline_ids.append(item_id)

        if not asset_ids and not timeline_ids:
            return

        # Create MIME data using helper function (统一格式)
        mime_data = build_media_pool_drag_mime(
            asset_ids=asset_ids or None,
            asset_paths=asset_paths or None,
            timeline_ids=timeline_ids or None,
        )

        # Create drag pixmap (thumbnail preview)
        drag = QDrag(self)
        drag.setMimeData(mime_data)

        # Create preview pixmap
        if assets:
            preview_pixmap = self._create_drag_preview(assets)
            drag.setPixmap(preview_pixmap)
            # Set hotspot at center of pixmap
            drag.setHotSpot(preview_pixmap.rect().center())

        # Execute drag
        drag.exec(Qt.DropAction.CopyAction)

    def _create_drag_preview(self, assets: List[MediaAsset]) -> QPixmap:
        """
        Create a preview pixmap for drag operation.

        Shows thumbnail of the first asset with count indicator if multiple.
        """
        # Get thumbnail for first asset
        thumb_provider = ThumbnailProvider.instance()

        # Create preview pixmap
        preview_width = 80
        preview_height = 60

        pixmap = QPixmap(preview_width, preview_height)
        pixmap.fill(QColor(40, 40, 50))

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Try to get thumbnail for first asset
        first_asset = assets[0]
        thumb_size = max(preview_width, preview_height)
        thumb_pixmap = thumb_provider.get_cached_thumbnail(first_asset.path, thumb_size)

        if thumb_pixmap:
            # Draw thumbnail centered
            tw, th = thumb_pixmap.width(), thumb_pixmap.height()
            if tw > 0 and th > 0:
                # Scale to fit preview
                scaled = thumb_pixmap.scaled(
                    preview_width, preview_height,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation
                )
                # Center it
                x = (preview_width - scaled.width()) // 2
                y = (preview_height - scaled.height()) // 2
                painter.drawPixmap(x, y, scaled)
        else:
            # Draw placeholder
            painter.setPen(QPen(QColor(100, 100, 100)))
            painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "...")
            # Request thumbnail for future
            thumb_provider.get_thumbnail(first_asset.path, thumb_size)

        # Draw count indicator if multiple assets
        if len(assets) > 1:
            count_badge_size = 24
            badge_x = preview_width - count_badge_size - 4
            badge_y = preview_height - count_badge_size - 4

            painter.setBrush(QBrush(QColor(255, 136, 0)))
            painter.setPen(QPen(Qt.PenStyle.NoPen))
            painter.drawEllipse(badge_x, badge_y, count_badge_size, count_badge_size)

            painter.setPen(QPen(QColor(255, 255, 255)))
            font = QFont()
            font.setPointSize(10)
            font.setBold(True)
            painter.setFont(font)
            badge_rect = QRect(badge_x, badge_y, count_badge_size, count_badge_size)
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, str(len(assets)))

        painter.end()
        return pixmap


# ================================================================
# Custom List View with Drag Support
# ================================================================

class AssetListView(QListView):
    """
    Custom list view for grid view that provides drag support for media assets.

    Overrides startDrag to create custom MIME data with asset IDs.
    Also accepts file drops from OS and forwards to parent MediaPoolPanel.
    """

    # AUD-20201: per-path cap for the hover-scrub frame cache (full-size
    # QPixmaps); back-and-forth scrubs on long videos otherwise grow unbounded.
    _HOVER_FRAMES_PER_PATH: int = 32

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._project: Optional[EditProject] = None
        self._context_menu_selection_ids: tuple[str, ...] = ()
        self._info_pressed_key: Optional[tuple[str, str]] = None
        self._info_hover_key: Optional[tuple[str, str]] = None
        # Inline rename only via programmatic edit() (F2 / menu), never on
        # double-click (double-click opens/enters the item).
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # Rubber-band (marquee) selection state for icon/grid mode.
        self._rubber_band: Optional[QRubberBand] = None
        self._rubber_origin: Optional[QPoint] = None
        # Marquee selection is throttled to one frame (~16ms): each mouse-move
        # during a drag only updates the band geometry immediately; the actual
        # model selection is rebuilt on a coalesced timer so rapid mouse moves
        # don't trigger a per-event full selectionChanged cascade.
        self._marquee_select_timer: QTimer = QTimer(self)
        self._marquee_select_timer.setSingleShot(True)
        self._marquee_select_timer.setInterval(16)
        self._marquee_select_timer.timeout.connect(self._apply_marquee_selection)
        # Hover-scrub state: track the hovered asset path + frame index so the
        # delegate can render the preview frame while the mouse moves over a
        # video card.
        self._thumb_provider = ThumbnailProvider.instance()
        self._thumb_provider.frame_thumbnail_ready.connect(self._on_hover_frame_ready)
        self._hover_path: Optional[str] = None
        self._hover_frame_index: Optional[int] = None
        # map path -> {frame_index: QPixmap} for the currently hovered asset.
        # AUD-20201: per-path cache is bounded below; only the currently
        # hovered video keeps frames (see _handle_hover_scrub/leaveEvent).
        self._hover_frames: dict[str, dict[int, QPixmap]] = {}
        self.setMouseTracking(True)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Start a rubber-band marquee when pressing on empty space.

        When the press lands on an item, defer to the default handler so the
        normal click-select / drag-initiate flow still works.
        """
        if event.button() == Qt.MouseButton.LeftButton:
            info_index = self._info_index_at(event.pos())
            if info_index.isValid():
                self._info_pressed_key = (info_index.data(AssetListModel.ITEM_TYPE_ROLE), info_index.data(AssetListModel.ITEM_ID_ROLE))
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                event.accept()
                return
        if event.button() == Qt.MouseButton.RightButton:
            # Some Qt styles collapse an extended selection while processing
            # the right-button press.  Snapshot the selected asset IDs before
            # delegating so the context-menu slot can restore the group.
            model = self.model()
            selection_model = self.selectionModel()
            if model is not None and selection_model is not None:
                target = self.indexAt(event.pos())
                if target.isValid() and selection_model.isSelected(target):
                    self._context_menu_selection_ids = tuple(
                        index.data(AssetListModel.ITEM_ID_ROLE)
                        for index in selection_model.selectedRows()
                        if index.data(AssetListModel.ITEM_TYPE_ROLE) == "asset"
                        and index.data(AssetListModel.ITEM_ID_ROLE)
                    )
                else:
                    self._context_menu_selection_ids = ()
            else:
                self._context_menu_selection_ids = ()
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.indexAt(event.pos())
            if not index.isValid():
                # Empty area: begin marquee selection (Finder-style).
                # The custom marquee path intentionally skips QListView's
                # mousePressEvent, so restore focus explicitly.  Without it
                # Delete/Backspace is delivered to the previously focused
                # widget instead of this view after a rubber-band selection.
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                self._rubber_origin = event.pos()
                if self._rubber_band is None:
                    self._rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())
                self._rubber_band.setGeometry(QRect(event.pos(), event.pos()))
                self._rubber_band.show()
                # Clear current selection unless Ctrl/Cmd is held (additive).
                if not (
                    event.modifiers() & Qt.KeyboardModifier.ControlModifier
                    or event.modifiers() & Qt.KeyboardModifier.MetaModifier
                ):
                    self.clearSelection()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Grow the rubber band; the selection update is throttled."""
        if self._info_pressed_key is not None:
            event.accept()
            return

        if self._rubber_origin is not None and self._rubber_band is not None:
            # Update the band geometry immediately (cheap, smooth feedback).
            self._rubber_band.setGeometry(
                QRect(self._rubber_origin, event.pos()).normalized()
            )
            # Coalesce selection updates to ~60fps so rapid mouse moves don't
            # trigger a full selectionChanged cascade per event.
            self._marquee_select_timer.start()
            event.accept()
            return
        self._update_info_hover(event.pos())
        # Inspecting the information badge must not scrub the underlying media.
        if event.buttons() == Qt.MouseButton.NoButton:
            if self._info_index_at(event.pos()).isValid():
                self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
                if self._hover_path is not None:
                    self._hover_path = None
                    self._hover_frame_index = None
                    self.viewport().update()
            else:
                self.viewport().unsetCursor()
                self._handle_hover_scrub(event.pos())
        super().mouseMoveEvent(event)

    def _apply_marquee_selection(self) -> None:
        """Rebuild the model selection from the current rubber-band rect."""
        if (
            self._rubber_origin is None
            or self._rubber_band is None
            or not self._rubber_band.isVisible()
        ):
            return
        sel_model = self.selectionModel()
        model = self.model()
        if sel_model is None or model is None:
            return
        rect = self._rubber_band.geometry()
        # Quick reject: skip items whose visual rect is entirely outside the
        # viewport vertically (their visualRect can't intersect the marquee
        # which lives inside the viewport). The full sweep is retained for
        # correctness; the viewport check trims the intersects() work.
        vp_top = self.viewport().rect().top()
        vp_bottom = self.viewport().rect().bottom()
        selection = QItemSelection()
        for row in range(model.rowCount()):
            idx = model.index(row, 0)
            if not idx.isValid():
                continue
            vr = self.visualRect(idx)
            if vr.bottom() < vp_top or vr.top() > vp_bottom:
                continue  # off-screen — can't intersect the marquee
            if rect.intersects(vr):
                selection.select(idx, idx)
        cmd = (
            QItemSelectionModel.SelectionFlag.Select
            | QItemSelectionModel.SelectionFlag.Clear
        )
        sel_model.select(selection, cmd)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """End the rubber-band marquee."""
        if self._info_pressed_key is not None and event.button() == Qt.MouseButton.LeftButton:
            expected = self._info_pressed_key
            self._info_pressed_key = None
            index = self._info_index_at(event.pos())
            key = (index.data(AssetListModel.ITEM_TYPE_ROLE), index.data(AssetListModel.ITEM_ID_ROLE)) if index.isValid() else None
            if key == expected:
                panel = self._find_media_pool_panel()
                if panel is not None:
                    panel._show_pool_item_info(index, self)
            event.accept()
            return

        if self._rubber_origin is not None and self._rubber_band is not None:
            # Apply a final selection immediately so the result reflects the
            # exact release position (the throttled timer may be pending).
            self._marquee_select_timer.stop()
            self._apply_marquee_selection()
            self._rubber_band.hide()
            self._rubber_origin = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _update_info_hover(self, pos: QPoint | None) -> None:
        index = self.indexAt(pos) if pos is not None else QModelIndex()
        key = None
        if index.isValid() and index.data(AssetListModel.ITEM_TYPE_ROLE) in ("asset", "timeline"):
            key = (index.data(AssetListModel.ITEM_TYPE_ROLE), index.data(AssetListModel.ITEM_ID_ROLE))
        if key != self._info_hover_key:
            self._info_hover_key = key
            self.viewport().update()

    def _info_index_at(self, pos: QPoint) -> QModelIndex:
        index = self.indexAt(pos)
        if not index.isValid() or index.data(AssetListModel.ITEM_TYPE_ROLE) not in ("asset", "timeline"):
            return QModelIndex()
        delegate = self.itemDelegateForIndex(index)
        geometry = getattr(delegate, "info_button_rect", None)
        if geometry is not None and geometry(self.visualRect(index)).contains(pos):
            return index
        return QModelIndex()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if self._info_index_at(event.pos()).isValid():
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def leaveEvent(self, event: Any) -> None:
        """Clear hover-scrub state when the cursor leaves the view."""
        self.viewport().unsetCursor()
        self._update_info_hover(None)
        self._hover_path = None
        self._hover_frame_index = None
        # AUD-20201: the cursor left the view — release every cached scrub
        # frame so long sessions don't accumulate pixmaps across assets.
        self._hover_frames.clear()
        self._update_for_hover_change()
        super().leaveEvent(event)

    def _handle_hover_scrub(self, pos: QPoint) -> None:
        """Request the video frame under ``pos`` for hover-scrub preview."""
        if not getattr(self, "live_preview_enabled", True):
            return
        model = self.model()
        if model is None or self._project is None:
            return
        index = self.indexAt(pos)
        if not index.isValid():
            self._hover_path = None
            self._hover_frame_index = None
            return
        asset = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(asset, MediaAsset) or asset.media_type != "video":
            self._hover_path = None
            self._hover_frame_index = None
            return
        total_frames = asset.metadata.get("frame_count") or asset.metadata.get("total_frames")
        try:
            total_frames = int(total_frames)
        except (TypeError, ValueError):
            total_frames = 0
        if total_frames <= 0:
            return
        # Map horizontal position within the card to a frame index.
        rect = self.visualRect(index)
        if rect.width() <= 0:
            return
        ratio = max(0.0, min(1.0, (pos.x() - rect.left()) / rect.width()))
        frame_index = int(ratio * (total_frames - 1))
        if self._hover_path == asset.path and self._hover_frame_index == frame_index:
            return  # Already showing this frame.
        if self._hover_path is not None and self._hover_path != asset.path:
            # AUD-20201: hover moved to another asset — drop the previous
            # asset's scrub frames (bounded, single-path cache).
            self._hover_frames.pop(self._hover_path, None)
        self._hover_path = asset.path
        self._hover_frame_index = frame_index
        # If cached, update immediately; otherwise request async.
        cached = self._hover_frames.get(asset.path, {}).get(frame_index)
        if cached is None:
            self._thumb_provider.get_frame_thumbnail(
                asset.path, frame_index, total_frames,
                icon_size=max(self.iconSize().width(), 128),
                asset_id=asset.asset_id,
            )
        self._update_for_hover_change()

    def _on_hover_frame_ready(self, path: str, frame_index: int, pixmap: QPixmap) -> None:
        """Cache a frame thumbnail delivered by the provider and repaint."""
        if pixmap is None:
            return
        frames = self._hover_frames.setdefault(path, {})
        frames[frame_index] = pixmap
        # AUD-20201: bound the per-path cache (FIFO by insertion order).
        # Unbounded growth used to retain a full-size QPixmap for every frame
        # ever scrubbed across, leaking tens of MB per long video.
        while len(frames) > self._HOVER_FRAMES_PER_PATH:
            oldest_index = next(iter(frames))
            frames.pop(oldest_index, None)
        if self._hover_path == path and self._hover_frame_index == frame_index:
            self._update_for_hover_change()

    def hover_frame_for(self, path: str) -> Optional[QPixmap]:
        """Return the cached hover-scrub frame for ``path``, if any."""
        if self._hover_path != path:
            return None
        frames = self._hover_frames.get(path)
        if frames is None or self._hover_frame_index is None:
            return None
        return frames.get(self._hover_frame_index)

    def _update_for_hover_change(self) -> None:
        """Repaint the viewport so the delegate picks up the new hover frame."""
        self.viewport().update()

    def set_project(self, project: Optional[EditProject]) -> None:
        """Set the project reference for accessing asset data."""
        self._project = project

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Forward Delete/Backspace to the owning MediaPoolPanel for batch delete.

        F2 starts inline rename on the selected bin/asset (Finder-style).
        Return/Enter opens the selected asset (double-click equivalent) or
        renames a bin. Cmd+↓ enters the selected bin, Cmd+↑ goes up.
        Cmd/Ctrl+A selects all items. Escape clears the selection.
        """
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            panel = self._find_media_pool_panel()
            if panel is not None and panel.handle_delete_key_from(self):
                event.accept()
                return
        index = self.currentIndex()
        model = self.model()
        item_type = (
            index.data(AssetListModel.ITEM_TYPE_ROLE)
            if (index.isValid() and model is not None)
            else None
        )
        item_id = (
            index.data(AssetListModel.ITEM_ID_ROLE)
            if index.isValid()
            else None
        )
        # F2 — rename selected bin/asset.
        if event.key() == Qt.Key.Key_F2 and item_type in ("bin", "asset"):
            self.edit(index)
            event.accept()
            return
        # Return / Enter — open asset, or rename bin.
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if item_type == "bin":
                self.edit(index)
                event.accept()
                return
            if item_type == "asset" and item_id:
                panel = self._find_media_pool_panel()
                if panel is not None:
                    panel.asset_double_clicked.emit(item_id)
                event.accept()
                return
        # Cmd/Ctrl+↓ — enter the selected bin.
        if event.key() == Qt.Key.Key_Down and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
            or event.modifiers() & Qt.KeyboardModifier.MetaModifier
        ):
            if item_type == "bin" and item_id:
                panel = self._find_media_pool_panel()
                if panel is not None:
                    panel._enter_bin(item_id)
                event.accept()
                return
        # Cmd/Ctrl+↑ — navigate up to the parent bin.
        if event.key() == Qt.Key.Key_Up and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
            or event.modifiers() & Qt.KeyboardModifier.MetaModifier
        ):
            panel = self._find_media_pool_panel()
            if panel is not None and panel._navigation_stack:
                panel._navigate_up()
                event.accept()
                return
        # Cmd/Ctrl+] — navigate forward.
        if event.key() == Qt.Key.Key_BracketRight and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
            or event.modifiers() & Qt.KeyboardModifier.MetaModifier
        ):
            panel = self._find_media_pool_panel()
            if panel is not None and panel._navigation_forward_stack:
                panel._navigate_forward()
                event.accept()
                return
        # Clipboard shortcuts (Cmd/Ctrl+C/X/V, Cmd/Ctrl+D duplicate).
        panel = self._find_media_pool_panel()
        mod = (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
            or event.modifiers() & Qt.KeyboardModifier.MetaModifier
        )
        if panel is not None and mod:
            if event.key() == Qt.Key.Key_C:
                panel._copy_selected_assets()
                event.accept()
                return
            if event.key() == Qt.Key.Key_X:
                panel._cut_selected_assets()
                event.accept()
                return
            if event.key() == Qt.Key.Key_V:
                panel._paste_assets()
                event.accept()
                return
            if event.key() == Qt.Key.Key_D:
                panel._duplicate_selected_assets()
                event.accept()
                return
        # Cmd/Ctrl+A — select all (Finder/NLE standard).
        if event.key() == Qt.Key.Key_A and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
            or event.modifiers() & Qt.KeyboardModifier.MetaModifier
        ):
            if panel is not None and panel._view_mode == ViewMode.METADATA and model is not None and model.rowCount():
                selection = QItemSelection(model.index(0, 0), model.index(model.rowCount() - 1, 0))
                self.selectionModel().select(selection, QItemSelectionModel.SelectionFlag.ClearAndSelect)
            else:
                self.selectAll()
            event.accept()
            return
        # Space — toggle Quick Look on the selected asset (Finder behavior).
        if event.key() == Qt.Key.Key_Space and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            panel = self._find_media_pool_panel()
            if panel is not None:
                panel._show_quick_look()
                event.accept()
                return
        # Escape — clear selection (Finder behavior).
        if event.key() == Qt.Key.Key_Escape:
            self.clearSelection()
            event.accept()
            return
        super().keyPressEvent(event)

    def _find_media_pool_panel(self) -> Optional[MediaPoolPanel]:
        """Walk up the parent chain to find the owning MediaPoolPanel."""
        parent = self.parent()
        while parent is not None:
            if isinstance(parent, MediaPoolPanel):
                return parent
            parent = parent.parent()
        return None

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        mime = event.mimeData()
        if _is_internal_media_pool_drag(mime) or mime.hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        mime = event.mimeData()
        if _is_internal_media_pool_drag(mime):
            # Internal drag: only accept when hovering over a bin card so the
            # cursor gives honest feedback (drops elsewhere are ignored).
            panel = self._find_media_pool_panel()
            if panel is not None:
                target_bin_id = panel._target_bin_at(
                    event.position().toPoint() if hasattr(event, 'position') else event.pos(),
                    source=self,
                )
                if target_bin_id is not None:
                    panel._set_drop_target_bin(target_bin_id)
                    event.acceptProposedAction()
                    return
            if panel is not None:
                panel._set_drop_target_bin(None)
            event.ignore()
            return
        if mime.hasUrls():
            # External OS file drop — highlight the bin card under the cursor so
            # the user sees where files will land (Finder-style).
            panel = self._find_media_pool_panel()
            if panel is not None:
                pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
                target_bin_id = panel._target_bin_at(pos, source=self)
                panel._set_drop_target_bin(target_bin_id)
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        mime = event.mimeData()
        # Internal drag takes priority: drop assets/timelines into the bin card
        # under the cursor. (Checked before hasUrls() because internal asset
        # drags also carry file URLs as a fallback.)
        if _is_internal_media_pool_drag(mime):
            panel = self._find_media_pool_panel()
            if panel is not None:
                pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
                target_bin_id = panel._target_bin_at(pos, source=self)
                if target_bin_id is not None:
                    asset_ids, timeline_ids = parse_media_pool_drag_mime(mime)
                    # P0-5：Alt/Option = 拷贝引用（多箱共存）；普通拖拽 = 移动。
                    handled = panel._handle_drop_into_bin(
                        target_bin_id, asset_ids, timeline_ids,
                        copy=bool(
                            event.modifiers() & Qt.KeyboardModifier.AltModifier),
                    )
                    if handled:
                        panel._set_drop_target_bin(None)
                        event.acceptProposedAction()
                        return
            if panel is not None:
                panel._set_drop_target_bin(None)
            event.ignore()
            return

        if mime.hasUrls():
            parent = self.parent()
            while parent:
                if isinstance(parent, MediaPoolPanel):
                    pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
                    target_bin_id = parent._target_bin_at(pos, source=self)
                    parent._set_drop_target_bin(None)
                    parent.dropEvent(event, target_bin_id=target_bin_id)
                    event.acceptProposedAction()
                    return
                parent = parent.parent()
            event.ignore()
            return

        super().dropEvent(event)

    def dragLeaveEvent(self, event: Any) -> None:
        panel = self._find_media_pool_panel()
        if panel is not None:
            panel._set_drop_target_bin(None)
        super().dragLeaveEvent(event)

    def startDrag(self, supportedActions: Qt.DropAction) -> None:
        """
        Start drag operation with custom MIME data.

        Creates MIME data containing:
        - asset_ids: List of asset IDs being dragged (JSON format)
        - timeline_ids: List of timeline IDs being dragged (JSON format)
        - urls: File URLs for fallback support
        - thumbnail: Preview pixmap for drag visualization
        """
        selected_indexes = self.selectionModel().selectedIndexes()
        if not selected_indexes:
            return

        # Get source model (may have proxy model)
        model = self.model()
        if isinstance(model, QSortFilterProxyModel):
            source_model = model.sourceModel()
        else:
            source_model = model

        if not isinstance(source_model, AssetListModel):
            return

        # Collect asset IDs/paths and timeline IDs from selected items.
        # Bins are intentionally skipped — they cannot be dragged.
        asset_ids: List[str] = []
        asset_paths: List[str] = []
        assets: List[MediaAsset] = []
        timeline_ids: List[str] = []

        # QListView may emit one index per column; dedupe by row.
        seen_rows: set[int] = set()
        for index in selected_indexes:
            if index.row() in seen_rows:
                continue
            seen_rows.add(index.row())

            # Map to source index if using proxy
            if isinstance(model, QSortFilterProxyModel):
                source_index = model.mapToSource(index)
            else:
                source_index = index

            item_type = source_model.data(source_index, AssetListModel.ITEM_TYPE_ROLE)
            item_id = source_model.data(source_index, AssetListModel.ITEM_ID_ROLE)
            if not item_id:
                continue

            if item_type == "asset":
                asset = source_model.data(source_index, Qt.ItemDataRole.UserRole)
                if isinstance(asset, MediaAsset):
                    asset_ids.append(asset.asset_id)
                    asset_paths.append(asset.path)
                    assets.append(asset)
            elif item_type == "timeline":
                timeline_ids.append(item_id)

        if not asset_ids and not timeline_ids:
            return

        # Create MIME data using helper function (统一格式)
        mime_data = build_media_pool_drag_mime(
            asset_ids=asset_ids or None,
            asset_paths=asset_paths or None,
            timeline_ids=timeline_ids or None,
        )

        # Create drag pixmap (thumbnail preview)
        drag = QDrag(self)
        drag.setMimeData(mime_data)

        # Create preview pixmap
        if assets:
            preview_pixmap = self._create_drag_preview(assets)
            drag.setPixmap(preview_pixmap)
            # Set hotspot at center of pixmap
            drag.setHotSpot(preview_pixmap.rect().center())

        # Execute drag
        drag.exec(Qt.DropAction.CopyAction)

    def _create_drag_preview(self, assets: List[MediaAsset]) -> QPixmap:
        """
        Create a preview pixmap for drag operation.

        Shows thumbnail of the first asset with count indicator if multiple.
        """
        # Get thumbnail for first asset
        thumb_provider = ThumbnailProvider.instance()

        # Create preview pixmap
        preview_width = 80
        preview_height = 60

        pixmap = QPixmap(preview_width, preview_height)
        pixmap.fill(QColor(40, 40, 50))

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Try to get thumbnail for first asset
        first_asset = assets[0]
        thumb_size = max(preview_width, preview_height)
        thumb_pixmap = thumb_provider.get_cached_thumbnail(first_asset.path, thumb_size)

        if thumb_pixmap:
            # Draw thumbnail centered
            tw, th = thumb_pixmap.width(), thumb_pixmap.height()
            if tw > 0 and th > 0:
                # Scale to fit preview
                scaled = thumb_pixmap.scaled(
                    preview_width, preview_height,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation
                )
                # Center it
                x = (preview_width - scaled.width()) // 2
                y = (preview_height - scaled.height()) // 2
                painter.drawPixmap(x, y, scaled)
        else:
            # Draw placeholder
            painter.setPen(QPen(QColor(100, 100, 100)))
            painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "...")
            # Request thumbnail for future
            thumb_provider.get_thumbnail(first_asset.path, thumb_size)

        # Draw count indicator if multiple assets
        if len(assets) > 1:
            count_badge_size = 24
            badge_x = preview_width - count_badge_size - 4
            badge_y = preview_height - count_badge_size - 4

            painter.setBrush(QBrush(QColor(255, 136, 0)))
            painter.setPen(QPen(Qt.PenStyle.NoPen))
            painter.drawEllipse(badge_x, badge_y, count_badge_size, count_badge_size)

            painter.setPen(QPen(QColor(255, 255, 255)))
            font = QFont()
            font.setPointSize(10)
            font.setBold(True)
            painter.setFont(font)
            badge_rect = QRect(badge_x, badge_y, count_badge_size, count_badge_size)
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, str(len(assets)))

        painter.end()
        return pixmap


# ================================================================
# 素材属性对话框
# ================================================================

class AssetPropertiesDialog(QDialog):
    """
    素材属性对话框，显示元数据信息。

    Attributes:
        asset: 要显示的素材
    """

    def __init__(self, asset: MediaAsset, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.asset = asset
        self._setup_ui()

    def _setup_ui(self) -> None:
        """设置 UI"""
        self.setWindowTitle(I18N.tr("edit.media_pool.properties_title", "素材属性"))
        self.setMinimumWidth(400)
        self.setMinimumHeight(300)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # 表单布局显示元数据
        form_layout = QFormLayout()
        form_layout.setSpacing(8)

        # 基本信息
        form_layout.addRow(
            I18N.tr("edit.media_pool.name", "名称:"),
            QLineEdit(self.asset.display_name)
        )
        form_layout.addRow(
            I18N.tr("edit.media_pool.path", "路径:"),
            QLineEdit(self.asset.path)
        )
        form_layout.addRow(
            I18N.tr("edit.media_pool.type", "类型:"),
            QLineEdit(self.asset.media_type)
        )

        # 元数据
        metadata = self.asset.metadata

        # 分辨率
        resolution = self.asset.resolution_str
        if resolution != "-":
            form_layout.addRow(
                I18N.tr("edit.media_pool.resolution", "分辨率:"),
                QLineEdit(resolution)
            )

        # 时长
        duration = self.asset.duration_seconds
        if duration is not None:
            duration_str = self._format_duration(duration)
            form_layout.addRow(
                I18N.tr("edit.media_pool.duration", "时长:"),
                QLineEdit(duration_str)
            )

        # 帧率
        fps = self.asset.fps
        if fps is not None:
            fps_str = f"{fps:.3f} fps"
            form_layout.addRow(
                I18N.tr("edit.media_pool.fps", "帧率:"),
                QLineEdit(fps_str)
            )

        # 总帧数
        total_frames = self.asset.total_frames
        if total_frames is not None:
            form_layout.addRow(
                I18N.tr("edit.media_pool.total_frames", "总帧数:"),
                QLineEdit(str(total_frames))
            )

        # 编码格式
        codec = metadata.get("codec")
        if codec:
            form_layout.addRow(
                I18N.tr("edit.media_pool.codec", "编码:"),
                QLineEdit(codec)
            )

        # 文件格式
        format_str = metadata.get("format", "")
        if format_str:
            form_layout.addRow(
                I18N.tr("edit.media_pool.format", "格式:"),
                QLineEdit(format_str)
            )

        # 文件大小
        file_size = metadata.get("file_size")
        if file_size:
            size_str = self._format_file_size(file_size)
            form_layout.addRow(
                I18N.tr("edit.media_pool.file_size", "文件大小:"),
                QLineEdit(size_str)
            )

        # 设置所有已有的 QLineEdit 为只读（元数据展示字段）。
        for i in range(form_layout.rowCount()):
            field_widget = form_layout.itemAt(i, QFormLayout.ItemRole.FieldRole)
            if field_widget and field_widget.widget():
                line_edit = field_widget.widget()
                if isinstance(line_edit, QLineEdit):
                    line_edit.setReadOnly(True)
                    line_edit.setStyleSheet("""
                        QLineEdit {
                            background: transparent;
                            border: none;
                            color: #aaa;
                        }
                    """)

        layout.addLayout(form_layout)

        # ---- 可编辑的影视制作元数据（Reel / Scene / Shot / Take） ----
        edit_group = QFormLayout()
        edit_group.setSpacing(8)
        self._reel_edit = QLineEdit(self.asset.reel or "")
        self._scene_edit = QLineEdit(self.asset.scene or "")
        self._shot_edit = QLineEdit(self.asset.shot or "")
        self._take_edit = QLineEdit(self.asset.take or "")
        edit_group.addRow(
            I18N.tr("edit.media_pool.header_reel", "Reel:"), self._reel_edit
        )
        edit_group.addRow(
            I18N.tr("edit.media_pool.header_scene", "Scene/Shot/Take:"),
            self._scene_edit,
        )
        edit_group.addRow(I18N.tr("edit.media_pool.shot", "Shot:"), self._shot_edit)
        edit_group.addRow(I18N.tr("edit.media_pool.take", "Take:"), self._take_edit)
        layout.addLayout(edit_group)

        # ---- 可编辑的日志字段（P1-12：关键词/评级/旗标/颜色/注释） ----
        log_group = QFormLayout()
        log_group.setSpacing(8)
        self._keywords_edit = QLineEdit(", ".join(self.asset.keywords))
        self._keywords_edit.setPlaceholderText(
            I18N.tr("edit.media_pool.keywords_hint", "逗号或顿号分隔，如: 采访, 室内")
        )
        log_group.addRow(
            I18N.tr("edit.media_pool.keywords", "关键词:"), self._keywords_edit
        )

        self._rating_combo = QComboBox()
        rating_labels = {
            0: I18N.tr("edit.media_pool.rating_unrated", "未评级"),
        }
        for value in range(0, 6):
            label = rating_labels.get(value) or ("★" * value)
            self._rating_combo.addItem(label, value)
        self._rating_combo.setCurrentIndex(self.asset.rating)
        log_group.addRow(
            I18N.tr("edit.media_pool.rating", "评级:"), self._rating_combo
        )

        self._flag_combo = QComboBox()
        self._flag_combo.addItem(I18N.tr("edit.media_pool.flag_none", "无"), "none")
        self._flag_combo.addItem(I18N.tr("edit.media_pool.flag_reject", "拒绝"), "reject")
        flag_index = self._flag_combo.findData(self.asset.flag)
        self._flag_combo.setCurrentIndex(max(0, flag_index))
        log_group.addRow(I18N.tr("edit.media_pool.flag", "旗标:"), self._flag_combo)

        self._color_combo = QComboBox()
        for label_value in ASSET_COLOR_LABELS:
            self._color_combo.addItem(label_value, label_value)
        color_index = self._color_combo.findData(self.asset.color_label)
        self._color_combo.setCurrentIndex(max(0, color_index))
        log_group.addRow(
            I18N.tr("edit.media_pool.color_label", "颜色标签:"), self._color_combo
        )

        self._comment_edit = QLineEdit(self.asset.comment or "")
        log_group.addRow(
            I18N.tr("edit.media_pool.comment", "注释:"), self._comment_edit
        )
        layout.addLayout(log_group)

        # Save + Close buttons.
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Close
        )
        button_box.button(QDialogButtonBox.StandardButton.Save).clicked.connect(
            self._on_save
        )
        button_box.rejected.connect(self.close)
        layout.addWidget(button_box)

    def production_info_values(self) -> tuple[str, str, str, str]:
        """Return the (reel, scene, shot, take) entered by the user."""
        return (
            self._reel_edit.text().strip(),
            self._scene_edit.text().strip(),
            self._shot_edit.text().strip(),
            self._take_edit.text().strip(),
        )

    def log_field_values(self) -> tuple[tuple[str, ...], int, str, str, str]:
        """Return (keywords, rating, flag, color_label, comment) entered by the user."""
        raw = self._keywords_edit.text().replace("、", ",")
        keywords = tuple(
            kw.strip() for kw in raw.split(",") if kw.strip()
        )
        rating = self._rating_combo.currentData()
        if not isinstance(rating, int):
            rating = 0
        flag = self._flag_combo.currentData() or "none"
        color_label = self._color_combo.currentData() or "none"
        return keywords, rating, flag, color_label, self._comment_edit.text().strip()

    def _on_save(self) -> None:
        """Accept the dialog so the caller can read the edited values."""
        self.accept()

    def _format_duration(self, seconds: float) -> str:
        """格式化时长"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:05.2f}"
        elif minutes > 0:
            return f"{minutes}:{secs:05.2f}"
        else:
            return f"{secs:.2f}s"

    def _format_file_size(self, size: int) -> str:
        """格式化文件大小"""
        if size >= 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024 * 1024):.2f} GB"
        elif size >= 1024 * 1024:
            return f"{size / (1024 * 1024):.2f} MB"
        elif size >= 1024:
            return f"{size / 1024:.2f} KB"
        else:
            return f"{size} B"


class BatchAssetLogDialog(QDialog):
    """批量标记对话框（P1-12 收尾）：对多选素材应用统一日志字段。

    「不变」语义：留空 / 选「不变」的字段参数为 None，
    ``BatchUpdateAssetLogCommand`` 将保留各素材原值。
    """

    def __init__(self, asset_count: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(I18N.tr("edit.media_pool.batch_edit_log_title", "批量标记"))
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        header = QLabel(
            I18N.tr(
                "edit.media_pool.batch_edit_log_header",
                "将应用于 {n} 个选中素材（留空/「不变」的字段保持原值）",
            ).format(n=asset_count)
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        form = QFormLayout()
        form.setSpacing(8)

        self._add_kw_edit = QLineEdit()
        self._add_kw_edit.setPlaceholderText(
            I18N.tr(
                "edit.media_pool.batch_add_keywords_hint",
                "逗号或顿号分隔，追加到现有关键词",
            )
        )
        form.addRow(
            I18N.tr("edit.media_pool.batch_add_keywords", "添加关键词:"),
            self._add_kw_edit,
        )

        self._remove_kw_edit = QLineEdit()
        self._remove_kw_edit.setPlaceholderText(
            I18N.tr(
                "edit.media_pool.batch_remove_keywords_hint",
                "从现有关键词中移除",
            )
        )
        form.addRow(
            I18N.tr("edit.media_pool.batch_remove_keywords", "移除关键词:"),
            self._remove_kw_edit,
        )

        self._rating_combo = QComboBox()
        self._rating_combo.addItem(
            I18N.tr("edit.media_pool.keep_unchanged", "不变"), None
        )
        for value in range(0, 6):
            label = (
                I18N.tr("edit.media_pool.rating_unrated", "未评级")
                if value == 0
                else ("★" * value)
            )
            self._rating_combo.addItem(label, value)
        form.addRow(I18N.tr("edit.media_pool.rating", "评级:"), self._rating_combo)

        self._flag_combo = QComboBox()
        self._flag_combo.addItem(
            I18N.tr("edit.media_pool.keep_unchanged", "不变"), None
        )
        self._flag_combo.addItem(I18N.tr("edit.media_pool.flag_none", "无"), "none")
        self._flag_combo.addItem(
            I18N.tr("edit.media_pool.flag_reject", "拒绝"), "reject"
        )
        form.addRow(I18N.tr("edit.media_pool.flag", "旗标:"), self._flag_combo)

        self._color_combo = QComboBox()
        self._color_combo.addItem(
            I18N.tr("edit.media_pool.keep_unchanged", "不变"), None
        )
        for label_value in ASSET_COLOR_LABELS:
            self._color_combo.addItem(label_value, label_value)
        form.addRow(
            I18N.tr("edit.media_pool.color_label", "颜色标签:"), self._color_combo
        )

        self._comment_edit = QLineEdit()
        self._comment_edit.setPlaceholderText(
            I18N.tr("edit.media_pool.comment_keep_hint", "留空=保持原注释")
        )
        form.addRow(I18N.tr("edit.media_pool.comment", "注释:"), self._comment_edit)

        layout.addLayout(form)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    @staticmethod
    def _parse_keywords(text: str) -> tuple[str, ...]:
        raw = text.replace("、", ",")
        return tuple(k.strip() for k in raw.split(",") if k.strip())

    def batch_params(self) -> dict[str, Any]:
        """返回 ``BatchUpdateAssetLogCommand.create`` 的关键字参数。"""
        return {
            "add_keywords": self._parse_keywords(self._add_kw_edit.text()),
            "remove_keywords": self._parse_keywords(self._remove_kw_edit.text()),
            "rating": self._rating_combo.currentData(),
            "flag": self._flag_combo.currentData(),
            "color_label": self._color_combo.currentData(),
            "comment": self._comment_edit.text().strip() or None,
        }


class ProxyPolicyDialog(QDialog):
    """项目级代理策略设置（P1-14：导入自动生成阈值 + 导出取源）。"""

    def __init__(self, policy: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(I18N.tr("edit.media_pool.proxy_policy_title", "代理策略"))
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        intro = QLabel(
            I18N.tr(
                "edit.media_pool.proxy_policy_intro",
                "策略决定导入时是否自动生成磁盘代理（播放流畅层），"
                "以及导出是否强制使用原片。策略随工程保存。",
            )
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        form.setSpacing(8)

        self._mode_combo = QComboBox()
        self._mode_combo.addItem(
            I18N.tr("edit.media_pool.proxy_policy_mode_auto", "自动：超过阈值的视频"), "auto"
        )
        self._mode_combo.addItem(
            I18N.tr("edit.media_pool.proxy_policy_mode_all", "全部在线视频"), "all"
        )
        self._mode_combo.addItem(
            I18N.tr("edit.media_pool.proxy_policy_mode_off", "关闭（不自动生成）"), "off"
        )
        self._mode_combo.setCurrentIndex(max(0, self._mode_combo.findData(policy.mode)))
        form.addRow(I18N.tr("edit.media_pool.proxy_policy_mode", "自动生成:"), self._mode_combo)

        self._max_width_spin = QSpinBox()
        self._max_width_spin.setRange(1, 16384)
        self._max_width_spin.setValue(policy.max_native_width)
        form.addRow(
            I18N.tr("edit.media_pool.proxy_policy_max_width", "原生最大宽度(px):"),
            self._max_width_spin,
        )

        self._max_height_spin = QSpinBox()
        self._max_height_spin.setRange(1, 4320)
        self._max_height_spin.setValue(policy.max_native_height)
        form.addRow(
            I18N.tr("edit.media_pool.proxy_policy_max_height", "原生最大高度(px):"),
            self._max_height_spin,
        )

        self._target_height_spin = QSpinBox()
        self._target_height_spin.setRange(360, 2160)
        self._target_height_spin.setSingleStep(180)
        self._target_height_spin.setValue(policy.target_height)
        form.addRow(
            I18N.tr("edit.media_pool.proxy_policy_target", "代理目标高度(px):"),
            self._target_height_spin,
        )

        self._auto_generate_check = QCheckBox(
            I18N.tr("edit.media_pool.proxy_policy_auto", "导入时自动触发生成")
        )
        self._auto_generate_check.setChecked(policy.auto_generate)
        form.addRow("", self._auto_generate_check)

        self._export_master_check = QCheckBox(
            I18N.tr("edit.media_pool.proxy_policy_export_master", "导出强制使用原片（推荐）")
        )
        self._export_master_check.setChecked(policy.export_use_master)
        form.addRow("", self._export_master_check)

        layout.addLayout(form)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def policy_values(self) -> Any:
        from src.features.edit.core.proxy_policy import ProxyPolicySettings

        return ProxyPolicySettings(
            mode=self._mode_combo.currentData() or "auto",
            max_native_width=self._max_width_spin.value(),
            max_native_height=self._max_height_spin.value(),
            target_height=self._target_height_spin.value(),
            auto_generate=self._auto_generate_check.isChecked(),
            export_use_master=self._export_master_check.isChecked(),
        )


class ConsolidateOptionsDialog(QDialog):
    """整理工程选项：分段模式（流复制/转码 ProRes）+ 手柄帧数。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(I18N.tr("edit.media_pool.consolidate_title", "整理工程（按剪辑范围）"))
        layout = QVBoxLayout(self)

        mode_form = QFormLayout()
        self._mode_combo = QComboBox()
        self._mode_combo.addItem(
            I18N.tr(
                "edit.media_pool.consolidate_remux",
                "流复制（无损，起点对齐关键帧）",
            ),
            MODE_REMUX,
        )
        self._mode_combo.addItem(
            I18N.tr(
                "edit.media_pool.consolidate_transcode",
                "转码 ProRes 422（帧精确，文件较大）",
            ),
            MODE_TRANSCODE,
        )
        mode_form.addRow(
            I18N.tr("edit.media_pool.consolidate_mode", "分段模式:"), self._mode_combo
        )
        self._handles_spin = QSpinBox()
        self._handles_spin.setRange(0, 600)
        self._handles_spin.setValue(12)
        self._handles_spin.setSuffix(" " + I18N.tr("edit.media_pool.frames_unit", "帧"))
        self._handles_spin.setToolTip(
            I18N.tr(
                "edit.media_pool.consolidate_handles_tip",
                "每段前后额外保留的源帧数，为后续修剪留余量",
            )
        )
        mode_form.addRow(
            I18N.tr("edit.media_pool.consolidate_handles", "手柄:"), self._handles_spin
        )
        layout.addLayout(mode_form)

        hint = QLabel(
            I18N.tr(
                "edit.media_pool.consolidate_hint",
                "只收集时间线实际使用的源区间；一个素材的多个区间生成多个分段，"
                "剪辑引用自动重映射。归档工程保存在目标目录，不影响当前工程。",
            )
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple:
        """返回 (mode, handles_frames)。"""
        return (
            self._mode_combo.currentData() or MODE_REMUX,
            self._handles_spin.value(),
        )


class QuickLookDialog(QDialog):
    """macOS-Finder-style Quick Look floating preview.

    Shows a large thumbnail of the asset plus its name and key metadata.
    Space or Escape closes it (toggle behavior). Double-clicking the
    thumbnail defers to the full Source Viewer open.
    """

    def __init__(self, asset: MediaAsset, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(asset.display_name)
        self.setModal(False)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Dialog
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self._asset = asset

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        # Thumbnail label (scaled large).
        self._image_label = QLabel()
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setMinimumSize(480, 320)
        self._image_label.setStyleSheet("background-color: #1a1b1e; border-radius: 6px;")
        layout.addWidget(self._image_label, 1)

        # Name + meta line.
        self._name_label = QLabel(asset.display_name)
        self._name_label.setStyleSheet("color: #e8e8e8; font-size: 14px; font-weight: bold;")
        layout.addWidget(self._name_label)

        meta_parts: list[str] = []
        res = asset.resolution_str
        if res and res != "-":
            meta_parts.append(res)
        if asset.duration_seconds is not None:
            meta_parts.append(self._format_duration(asset.duration_seconds))
        if asset.fps is not None:
            meta_parts.append(f"{asset.fps:.2f} fps")
        self._meta_label = QLabel("  ·  ".join(meta_parts) if meta_parts else asset.media_type)
        self._meta_label.setStyleSheet("color: #999;")
        layout.addWidget(self._meta_label)

        # Hint.
        hint = QLabel(I18N.tr("edit.media_pool.quick_look_hint", "Space to close · Double-click to open"))
        hint.setStyleSheet("color: #666; font-size: 11px;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)

        self.setStyleSheet("QuickLookDialog { background-color: #2a2b2e; }")

        self._thumb_provider = ThumbnailProvider.instance()
        self._thumb_provider.thumbnail_ready.connect(self._on_thumb_ready)
        self._thumb_provider.get_thumbnail(asset.path, 512)

    def _on_thumb_ready(self, path: str, pixmap: QPixmap) -> None:
        if path != self._asset.path or pixmap is None:
            return
        scaled = pixmap.scaled(
            640, 480,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._image_label.setPixmap(scaled)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # Space or Escape closes the Quick Look (Finder behavior).
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Escape, Qt.Key.Key_Return):
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)

    @staticmethod
    def _format_duration(seconds: float) -> str:
        minutes = int(seconds // 60)
        secs = seconds % 60
        if minutes > 0:
            return f"{minutes}:{secs:05.2f}"
        return f"{secs:.2f}s"


class BinPropertiesDialog(QDialog):
    """Read-only properties for a bin (Finder 'Get Info' for a folder).

    Shows item counts and, where available, total duration of contained
    assets. Read-only — editing the bin name is done via inline rename.
    """

    def __init__(
        self,
        bin_obj: ProjectBin,
        project: EditProject,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(
            I18N.tr("edit.media_pool.bin_properties_title", "Bin Properties")
        )
        self.setMinimumWidth(340)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(8)

        assets = [
            project.assets[aid]
            for aid in bin_obj.asset_ids
            if aid in project.assets
        ]
        timelines = [
            project.timelines[tid]
            for tid in bin_obj.timeline_ids
            if tid in project.timelines
        ]
        total_duration = sum(
            (a.duration_seconds or 0.0) for a in assets
        )

        form.addRow(
            I18N.tr("edit.media_pool.name", "名称:"),
            self._readonly_field(bin_obj.name),
        )
        form.addRow(
            I18N.tr("edit.media_pool.asset_count", "素材数:"),
            self._readonly_field(str(len(assets))),
        )
        form.addRow(
            I18N.tr("edit.media_pool.timeline_count", "时间线数:"),
            self._readonly_field(str(len(timelines))),
        )
        if total_duration > 0:
            form.addRow(
                I18N.tr("edit.media_pool.total_duration", "总时长:"),
                self._readonly_field(self._format_duration(total_duration)),
            )
        layout.addLayout(form)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        button_box.rejected.connect(self.close)
        layout.addWidget(button_box)

    @staticmethod
    def _readonly_field(text: str) -> QLineEdit:
        edit = QLineEdit(text)
        edit.setReadOnly(True)
        edit.setStyleSheet(
            "QLineEdit { background: transparent; border: none; color: #aaa; }"
        )
        return edit

    @staticmethod
    def _format_duration(seconds: float) -> str:
        minutes = int(seconds // 60)
        secs = seconds % 60
        if minutes > 0:
            return f"{minutes}:{secs:05.2f}"
        return f"{secs:.2f}s"


# ================================================================
# MediaPoolPanel
# ================================================================

class MediaPoolPanel(QWidget):
    """
    Media Pool panel for managing project media assets.

    Signals:
        asset_import_requested: User requested to import assets (list of file paths)
        asset_imported: An asset was successfully imported (asset_id, metadata)
        timeline_create_requested: User requested to create a new timeline
        timeline_settings_requested: User requested to edit an existing timeline
        timeline_delete_requested: User requested to delete an existing timeline
        asset_selected: An asset was selected in the pool (asset_id)
        timeline_selected: A timeline was selected in the pool (timeline_id)
        project_changed: Project data changed (EditProject instance)

    Attributes:
        _project: Current EditProject reference
        _updating: Flag to prevent recursive UI updates
    """

    # Signals
    asset_import_requested = pyqtSignal(list)  # list of file paths
    asset_imported = pyqtSignal(str, dict)     # asset_id, metadata
    # E22: asset_removed 已删除（2 个 emit、0 个 connect 的死信号）
    timeline_create_requested = pyqtSignal()
    timeline_switch_requested = pyqtSignal(str)  # timeline_id (switch to this timeline)
    timeline_settings_requested = pyqtSignal(str)  # timeline_id
    timeline_delete_requested = pyqtSignal(str)  # timeline_id
    # Batch delete via Delete/Backspace key. The handler requests full
    # TimelineSequence snapshots from the edit controller so tracks/clips
    # survive the undo/redo cycle. Payload: (timeline_ids, callback).
    timeline_command_snapshot_requested = pyqtSignal(list, object)
    asset_selected = pyqtSignal(str)           # asset_id
    timeline_selected = pyqtSignal(str)        # timeline_id (inspect only)
    project_changed = pyqtSignal(object)       # EditProject
    # F2.7：解释覆盖已提交（asset_ids, fps）——控制器可提示应用到当前
    # 时间线的既有片段（时基重锚）
    interpretation_applied = pyqtSignal(tuple, float)
    # F2.10：视频素材请求绑定外录音频（源级同步入口）
    bind_external_audio_requested = pyqtSignal(str)
    asset_double_clicked = pyqtSignal(str)     # asset_id (for Source Viewer)
    # P9.1: Open asset grade in Color page
    open_asset_grade_requested = pyqtSignal(str)  # asset_id (open asset's default grade stack in Color)
    add_to_timeline_requested = pyqtSignal(str)   # asset_id (add asset clip to current timeline)
    # Phase 5: Open Comp Clip in Composite page
    open_comp_clip_in_composite_requested = pyqtSignal(str)  # asset_id (open comp clip composite)
    create_empty_comp_clip_requested = pyqtSignal()  # Create new empty comp clip
    create_empty_motion_graphic_requested = pyqtSignal()  # Create new empty Motion Graphic asset
    edit_comp_clip_settings_requested = pyqtSignal(str)  # asset_id (edit comp clip settings)
    # P9-3: Scene Cut Detection for video assets
    scene_cut_detect_requested = pyqtSignal(str)  # asset_id (start scene cut detection)
    # M3: 媒体池可选快速分析（§17.2）。UI 只上报意图，由 host 接 MediaAnalysisController。
    # 分析结果只产候选 segment，绝不直接改 timeline（§0 红线）。开关默认 off。
    # 范围 scope: "selected" | "bin" | "all"。"全部"范围由 host 弹二次确认卡（§17.3）。
    analysis_requested = pyqtSignal(list, str)       # asset_ids, scope
    analysis_cancel_requested = pyqtSignal(str)      # asset_id
    analysis_retry_requested = pyqtSignal(str)       # asset_id
    analysis_result_cleared = pyqtSignal(str)        # asset_id（删除该 asset 的分析结果）
    # P1-01: 多机位组创建（多选视频素材，UI 只上报意图）
    create_multicam_group_requested = pyqtSignal(list)  # asset_ids（顺序即 1..N 角度序）
    # Auto-project creation when user starts working without a project
    auto_project_needed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        # 状态变量
        self._project: Optional[EditProject] = None
        self._updating: bool = False
        self._import_progress: Optional[QProgressDialog] = None
        # P2-2：收集/整理后台任务运行态（运行中禁止重复启动）
        self._media_mgmt_running: bool = False
        # R04（媒体池复查）：Media Management 会话代际 + 在场任务状态。
        # 切工程/关闭时递增代际并请求协作取消——晚到的完成/失败回调
        # 按代际+工程身份丢弃，不向新工程弹"成功"提示或写归档。
        self._media_mgmt_session_generation: int = 0
        self._media_mgmt_state: Optional[dict] = None
        # 进度对话框重入保护：setValue 期间的嵌套事件派发不得销毁对话框
        self._progress_update_in_flight: bool = False
        self._finish_import_deferred: bool = False
        self._view_mode: ViewMode = ViewMode.GRID  # 默认网格视图
        self._thumb_size_enum: ThumbnailSize = ThumbnailSize.MEDIUM
        self._thumb_width: int = 100  # DaVinci compact default
        self._current_timeline_id: Optional[str] = None  # Current active timeline (V-10, UI-27)
        self._import_in_progress: bool = False
        self._active_import_batch_id: Optional[str] = None
        self._active_import_project_id: Optional[str] = None
        self._active_import_worker: Optional[MediaImportWorker] = None
        self._pending_import_files: List[str] = []
        self._proxy_jobs: Dict[str, str] = {}  # asset_id -> source path at job start
        # FE-B11：任务 generation token。旧回调只在 expected_path 非空时
        # 校验路径；换源/删除把 _proxy_jobs pop 后 expected=None，旧代理
        # 的 success/error 回调被接收，把旧 proxy 标到新素材上。token 与
        # 任务绑定，pop 即失效（等价 tombstone），晚到的回调一律拒绝。
        self._proxy_job_tokens: Dict[str, int] = {}
        self._proxy_token_counter: int = 0
        # F-R07/R26：代理任务会话代次——切工程/清空即注销全部在途任务
        # 与 pending 探测，跨工程复用 asset ID/path 的旧回调零写入。
        self._proxy_session_generation: int = 0
        self._proxy_job_generations: Dict[str, int] = {}
        self._proxy_job_signatures: dict[str, tuple[int, int]] = {}
        # AUD-20200: the panel tracks its Quick Look dialog explicitly.
        # findChild(QuickLookDialog) never matched because the dialog's QObject
        # parent is the top-level window, not this panel (nested in a dock).
        self._quick_look_dialog: Optional[QuickLookDialog] = None
        self._pool_info_popup: Optional[QWidget] = None
        self._pool_info_key: Optional[tuple[str, str]] = None
        self._offline_status_signature: Optional[tuple[tuple[str, str, bool, bool], ...]] = None
        # Detect files moved/deleted while the project stays open.  The scan
        # is deliberately infrequent and only publishes a project change when
        # an asset's derived offline state actually changes.
        self._offline_refresh_timer = QTimer(self)
        self._offline_refresh_timer.setInterval(5000)
        self._offline_refresh_timer.timeout.connect(self._refresh_offline_status)
        self._offline_refresh_timer.start()
        # AUD-20202: availability scans run on a pool worker; at most one in
        # flight at a time so slow network volumes can't queue up scans.
        self._offline_scan_in_flight: bool = False
        self._refresh_bins_pending: bool = False
        # M3: 媒体池可选快速分析（§17.2）。默认 off（M3 验收第 1 项：导入零 AI 推理）。
        # controller 由 host 注入（set_media_analysis_controller）；panel 不直接调
        # AnalysisService，只发 analysis_requested 信号上报意图（遵循 .clinerules：UI 只更新状态并发信号）。
        self._media_analysis_enabled: bool = False
        self._media_analysis_controller: Any = None
        self._analysis_status_cache: dict[str, str] = {}  # asset_id → status（badge 用）
        self._motion_graphic_creation_available: bool = False

        # Command stack for undo/redo
        self._command_stack: ProjectCommandStack = ProjectCommandStack()

        # P0-4 大数据查询：十万素材级池的异步过滤/排序会话（latest-wins；
        # 小池走同步内存路径，见 _large_pool_query_active）
        from src.features.edit.core.media_pool_query import MediaPoolQuerySession
        self._query_session = MediaPoolQuerySession(self)
        self._query_session.results_ready.connect(self._on_pool_query_results)
        self._query_session.query_failed.connect(self._on_pool_query_failed)
        self._query_asset_ids: Optional[tuple[str, ...]] = None
        self._last_query_fingerprint: tuple = ()
        self._pending_query_fingerprint: tuple = ()

        # Search and sort state
        self._search_all_bins: bool = False
        self._search_text: str = ""
        self._search_filter_mode: str = SearchFilterMode.FILENAME.value
        self._sort_by: str = SORT_BY_NAME
        self._sort_order: str = "asc"  # asc or desc
        self._current_bin_id: str = ""  # Currently selected bin
        self._current_bin_is_smart: bool = False  # Currently selected bin type
        # Navigation stack for the right-side "enter bin / go up" flow.
        # Holds the bin_id chain that leads to the current view; empty means
        # we're at the project root level.
        self._navigation_stack: list[str] = []
        # Forward stack mirrors _navigation_stack for Cmd+] / "forward".
        # Pushed to when navigating up; cleared when entering a new bin.
        self._navigation_forward_stack: list[str] = []
        # Drop-target highlight for grid/tree bin cards during internal drags.
        self._drop_target_bin_id: Optional[str] = None
        # Internal clipboard for copy/cut/paste of assets (Cmd+C/X/V).
        # Stores (asset_ids, is_cut). Cut is a no-op on the source until paste
        # (Finder semantics: cut is deferred-move).
        self._clipboard_asset_ids: List[str] = []
        self._clipboard_is_cut: bool = False
        # When set, the next import mirrors this folder's subfolder structure
        # into bins (cleared after the import finishes).
        self._import_folder_root: Optional[str] = None
        # Target bin for the active external import (drag files/folder onto a
        # specific bin). None = import into the project root. Cleared on finish.
        self._active_import_target_bin_id: Optional[str] = None

        # 防抖刷新定时器（UI-33）
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(100)  # 100ms 防抖延迟
        self._refresh_timer.timeout.connect(self._delayed_refresh)

        # 缩略图提供器
        self._thumb_provider = ThumbnailProvider.instance()
        self._pending_timeline_frame_previews: dict[tuple[str, int], set[str]] = {}
        self._pending_timeline_image_previews: dict[str, set[str]] = {}

        # AUD-20209: path → 树节点索引（懒重建）。每张缩略图到达时按 path
        # O(1) 定位节点，取代每张到达即全树扫描的 O(n²) 模式。
        self._tree_items_by_path: Dict[str, List[QTreeWidgetItem]] = {}
        self._tree_index_dirty: bool = True

        # AUD-20210: 代理分辨率探测 worker 的队列信号与元数据暂存。
        self._proxy_resolution_signals = ProxyResolutionSignals()
        self._proxy_resolution_signals.probed.connect(
            self._on_proxy_resolution_probed)
        self._pending_proxy_meta: Dict[str, tuple[Any, ...]] = {}
        self._replacement_requests: dict[str, tuple[Any, ...]] = {}
        self._replacement_workers: dict[str, ReplacementProbeWorker] = {}
        # R03：统一代理服务回调 → UI 线程信号桥（服务回调来自后台线程）。
        self._proxy_service_signals = ProxyServiceSignals()
        self._proxy_service_signals.progress.connect(
            self._on_proxy_progress)
        self._proxy_service_signals.finished.connect(
            self._on_proxy_finished)
        self._proxy_service_signals.error.connect(self._on_proxy_error)

        # 设置拖拽支持
        self.setAcceptDrops(True)

        self._setup_ui()
        self._setup_grid_view()
        self._setup_context_menu()
        self._connect_signals()
        self._connect_thumbnail_signals()
        # Keep Delete/Backspace working even when Qt delivers the key to a
        # child viewport or a platform-specific focus proxy instead of the
        # custom view subclass itself.
        self._delete_shortcuts: list[QShortcut] = []
        for key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(self._delete_from_focused_view)
            self._delete_shortcuts.append(shortcut)
        # Restore last-used sort preferences (persisted across restarts).
        self._restore_sort_prefs()
        self._sync_sort_indicator()
        search_escape = QShortcut(QKeySequence(Qt.Key.Key_Escape), self.search_box)
        search_escape.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        search_escape.activated.connect(lambda: self.btn_search.setChecked(False))
        self._search_escape = search_escape
        self._pool_shortcuts: list[QShortcut] = []
        for key, callback in (("Ctrl+f", lambda: self.btn_search.setChecked(True)), ("Ctrl+Shift+n", self._on_new_bin_clicked), ("Ctrl+i", self._show_selected_pool_info)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
            self._pool_shortcuts.append(shortcut)

        logger.debug("MediaPoolPanel initialized (with bin tree, smart bins, and search filter)")

    # ==================== UI Setup ====================

    def _setup_ui(self) -> None:
        """Set up the panel UI structure with bin tree + asset view splitter."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.setObjectName("mediaPoolPanel")
        # Keep panel surfaces on the application's global theme. No local
        # slider/scrollbar subcontrols inherit the shared application QSS.
        self.setStyleSheet(f"""
            QWidget#mediaPoolPanel, QWidget#mediaPoolContent {{ background: {C.BG_PANEL}; }}
            QWidget#mediaPoolToolbar {{ background: {C.BG_DARK}; border-bottom: 1px solid {C.PANEL_DIVIDER}; }}
            QToolButton {{ border: none; background: transparent; padding: 2px; margin: 0; border-radius: 2px; }}
            QToolButton:hover {{ background: {C.BG_ACTIVE}; }}
            QToolButton:checked {{ background: {C.BG_BUTTON}; }}
            QToolButton::menu-indicator {{ image: none; }}
            QTreeWidget, QListView {{ background: {C.BG_PANEL}; color: {C.TEXT_SECONDARY}; border: none; }}
            QTreeWidget {{ alternate-background-color: {C.BG_INPUT}; }}
            QTreeWidget::item {{ height: 23px; padding: 0 4px; border: none; }}
            QTreeWidget::item:selected {{ background: {C.BG_DARKEST}; color: {C.TEXT_PRIMARY}; }}
            QHeaderView::section {{ background: {C.BG_INPUT}; color: {C.TEXT_LABEL}; border: none;
                border-right: 1px solid {C.BORDER}; border-bottom: 1px solid {C.PANEL_DIVIDER}; padding: 3px 6px; }}
            QLabel {{ color: {C.TEXT_LABEL}; background: transparent; }}
            QFrame#mediaPoolBreadcrumb {{ background: {C.BG_PANEL}; border: none; border-bottom: 1px solid {C.PANEL_DIVIDER}; }}
            QFrame#mediaPoolBreadcrumb QPushButton {{ padding: 0; font-size: 12px; }}
            QFrame#mediaPoolBreadcrumb QLabel {{ font-size: 12px; }}
            QToolButton#mediaPoolPathBack {{ border-right: 1px solid {C.PANEL_DIVIDER}; border-radius: 0; padding: 0; }}
            QPushButton {{ background: transparent; color: {C.TEXT_SECONDARY}; border: none; padding: 2px 4px; }}
            QPushButton:hover {{ background: {C.BG_ACTIVE}; }}
        """)
        self.toolbar_container = QWidget()
        self.toolbar_container.setObjectName("mediaPoolToolbar")
        self.toolbar_container.setFixedHeight(30)
        toolbar = QHBoxLayout(self.toolbar_container)
        toolbar.setContentsMargins(4, 0, 4, 0)
        toolbar.setSpacing(2)

        self.btn_toggle_sidebar = self._pool_button("fa5s.columns", "toggle_sidebar", "Bin List", True)
        self.btn_toggle_sidebar.setChecked(True)
        self.btn_toggle_sidebar.clicked.connect(self._on_toggle_sidebar)
        toolbar.addWidget(self.btn_toggle_sidebar)
        self.btn_color_tag = self._pool_button("fa5s.tag", "clip_color", "Clip Color")
        toolbar.addWidget(self.btn_color_tag)
        self.btn_relink = self._pool_button("fa5s.link", "relink_media", "Relink Media")
        self.btn_relink.clicked.connect(self._on_relink_batch)
        toolbar.addWidget(self.btn_relink)
        self.btn_analysis_toggle = self._pool_button("fa5s.brain", "analysis_toggle_tooltip", "Enable AI Media Analysis", True)
        self.btn_analysis_toggle.toggled.connect(self._on_analysis_toggle)
        toolbar.addWidget(self.btn_analysis_toggle)
        toolbar.addStretch(1)
        self.thumb_slider = QSlider(Qt.Orientation.Horizontal)
        self.thumb_slider.setFixedWidth(64)
        self.thumb_slider.setObjectName("mediaPoolThumbnailSlider")
        # Only the widget surface is transparent; groove/handle colors and
        # geometry still come from the shared application slider stylesheet.
        self.thumb_slider.setStyleSheet("QSlider#mediaPoolThumbnailSlider { background: transparent; border: none; }")
        # Larger images to the right, as in Resolve; the stored value remains
        # a column count owned by ColumnGridController.
        self.thumb_slider.setInvertedAppearance(True)
        self.thumb_slider.setInvertedControls(True)
        self.thumb_slider.setToolTip(I18N.tr("edit.media_pool.thumb_size_tooltip", "Thumbnail Size"))
        self.thumb_slider.setAccessibleName(self.thumb_slider.toolTip())
        toolbar.addWidget(self.thumb_slider)
        toolbar.addSpacing(20)
        self.btn_metadata_view = self._pool_button("fa5s.id-card", "metadata_view", "Metadata View", True)
        self.btn_grid_view = self._pool_button("fa5s.th", "grid_view_tooltip", "Thumbnail View", True)
        self.btn_list_view = self._pool_button("fa5s.list-ul", "list_view_tooltip", "List View", True)
        self.btn_grid_view.setChecked(True)
        for button in (self.btn_metadata_view, self.btn_grid_view, self.btn_list_view):
            button.setFixedWidth(28)
            toolbar.addWidget(button)
        self.btn_search = self._pool_button("fa5s.search", "search_tooltip", "Search", True)
        self.btn_search.setFixedWidth(28)
        toolbar.addWidget(self.btn_search)
        self.btn_search_scope = self._pool_button("fa5s.chevron-down", "search_scope", "Search in Selected Bin")
        self.btn_search_scope.setFixedWidth(16)
        toolbar.addWidget(self.btn_search_scope)
        self.btn_sort = self._pool_button("fa5s.sort-amount-down", "sort", "Sort")
        self.btn_sort.setFixedWidth(36)
        toolbar.addWidget(self.btn_sort)
        self.btn_more = self._pool_button("fa5s.ellipsis-h", "options", "Media Pool Options")
        self.btn_more.setFixedWidth(36)
        toolbar.addWidget(self.btn_more)
        self.btn_offline_warning = self._pool_button("fa5s.exclamation-triangle", "offline_warning_tooltip", "Offline Assets - Click to Relink")
        self.btn_offline_warning.clicked.connect(self._on_relink_batch)
        self.btn_offline_warning.hide()
        toolbar.addWidget(self.btn_offline_warning)
        # Keep the existing sort controls as adapters for callers and tests;
        # the visible UI uses the compact Resolve sort menu.
        self.sort_combo = QComboBox(self)
        for key, label, value in (("sort_name", "Clip Name", SORT_BY_NAME), ("sort_date", "Date Imported", SORT_BY_DATE),
                                  ("sort_type", "Clip Type", SORT_BY_TYPE), ("sort_duration", "Duration", SORT_BY_DURATION)):
            self.sort_combo.addItem(I18N.tr(f"edit.media_pool.{key}", label), value)
        for key, label in (("reel", "Reel Name"), ("scene", "Scene/Shot/Take"), ("fps", "FPS"),
                           ("resolution", "Resolution"), ("codec", "Codec"), ("audio", "Audio"), ("path", "File Path")):
            self.sort_combo.addItem(I18N.tr(f"edit.media_pool.sort_{key}", label), key)
        self.sort_combo.hide()
        self.btn_sort_order = QToolButton(self)
        self.btn_sort_order.setCheckable(True)
        self.btn_sort_order.hide()
        self.status_label = QLabel(self)
        self.status_label.hide()
        layout.addWidget(self.toolbar_container)
        self.search_box = SearchLineEdit()
        self.search_box.hide()
        layout.addWidget(self.search_box)

        self.main_splitter = PanelSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        self.sidebar = PanelSplitter(Qt.Orientation.Vertical)
        self.sidebar.setChildrenCollapsible(False)
        self.sidebar.setMinimumWidth(100)
        self.bin_tree = BinTreeWidget()
        self.bin_tree.setIndentation(18)
        self.bin_tree.setStyleSheet(self._pool_tree_style())
        self.smart_bin_tree = BinTreeWidget()
        self.smart_bin_tree.setProperty("smartBinsOnly", True)
        self.smart_bin_tree.setStyleSheet(self._pool_tree_style())
        self.sidebar.addWidget(self.bin_tree)
        self.sidebar.addWidget(self.smart_bin_tree)
        self.sidebar.setSizes([560, 150])
        self.sidebar.setStretchFactor(0, 1)
        self.sidebar.setStretchFactor(1, 0)
        self.main_splitter.addWidget(self.sidebar)
        self.view_container = QWidget()
        self.view_container.setObjectName("mediaPoolContent")
        container_layout = QVBoxLayout(self.view_container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)
        self.breadcrumb_widget = QFrame()
        self.breadcrumb_widget.setObjectName("mediaPoolBreadcrumb")
        self.breadcrumb_widget.setFixedHeight(29)
        breadcrumb_layout = QHBoxLayout(self.breadcrumb_widget)
        breadcrumb_layout.setContentsMargins(0, 0, 5, 0)
        breadcrumb_layout.setSpacing(6)
        self.btn_breadcrumb_back = self._pool_button("fa5s.chevron-left", "navigate_up", "Back")
        self.btn_breadcrumb_back.setObjectName("mediaPoolPathBack")
        self.btn_breadcrumb_back.setFixedSize(29, 29)
        self.btn_breadcrumb_back.clicked.connect(self._navigate_up)
        breadcrumb_layout.addWidget(self.btn_breadcrumb_back)
        self.breadcrumb_path_container = QWidget()
        self.breadcrumb_path_layout = QHBoxLayout(self.breadcrumb_path_container)
        self.breadcrumb_path_layout.setContentsMargins(0, 0, 0, 0)
        self.breadcrumb_path_layout.setSpacing(3)
        breadcrumb_layout.addWidget(self.breadcrumb_path_container, 1)
        container_layout.addWidget(self.breadcrumb_widget)

        self.tree = AssetTreeWidget()
        self.tree.setHeaderLabels([
            I18N.tr("edit.media_pool.clip_name", "Clip Name"),
            I18N.tr("edit.media_pool.header_reel", "Reel Name"),
            I18N.tr("edit.media_pool.header_scene", "Scene/Shot/Take"),
            I18N.tr("edit.media_pool.header_duration", "Duration"),
            I18N.tr("edit.media_pool.header_fps", "FPS"),
            I18N.tr("edit.media_pool.header_resolution", "Resolution"),
            I18N.tr("edit.media_pool.header_codec", "Codec"),
            I18N.tr("edit.media_pool.header_audio", "Audio"),
            I18N.tr("edit.media_pool.header_path", "File Path"),
        ])
        for column, width in enumerate((200, 90, 120, 90, 65, 110, 90, 70, 250)):
            self.tree.setColumnWidth(column, width)
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setStyleSheet(self._pool_tree_style())
        self.tree.header().setSectionsClickable(True)
        self.tree.header().setSectionsMovable(True)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSortIndicatorShown(True)
        self.tree.header().sectionClicked.connect(self._on_header_clicked)
        self.tree.header().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.header().customContextMenuRequested.connect(self._show_column_menu)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        self.tree.itemDoubleClicked.connect(self._on_tree_double_clicked)
        self.tree.itemChanged.connect(self.tree._on_item_data_changed)
        self.tree.setDragEnabled(True)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setAcceptDrops(True)
        container_layout.addWidget(self.tree)
        self.tree.hide()
        self.grid_view: Optional[QListView] = None
        self.grid_delegate: Optional[GridDelegate] = None
        self.asset_model: Optional[AssetListModel] = None
        self.main_splitter.addWidget(self.view_container)
        self.main_splitter.setSizes([150, 405])
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        layout.addWidget(self.main_splitter, 1)
        self._setup_pool_menus()
        self._update_breadcrumb()

    def _pool_button(self, icon_name: str, key: str, label: str, checkable: bool = False) -> QToolButton:
        """Create a compact, accessible toolbar button using the global theme."""
        button = QToolButton()
        icon_names = {
            "toggle_sidebar": "sidebar", "clip_color": "tag", "relink_media": "relink",
            "analysis_toggle_tooltip": "analysis", "metadata_view": "metadata",
            "grid_view_tooltip": "grid", "list_view_tooltip": "list", "search_tooltip": "search",
            "search_scope": "chevron-down", "sort": "sort", "options": "more",
            "offline_warning_tooltip": "warning", "navigate_up": "back",
        }
        button.setIcon(pool_icon(icon_names.get(key, "more")))
        button.setFixedSize(25, 25)
        button.setIconSize(QSize(16, 16))
        button.setToolTip(I18N.tr(f"edit.media_pool.{key}", label))
        button.setAccessibleName(button.toolTip())
        button.setCheckable(checkable)
        return button

    @staticmethod
    def _pool_tree_style() -> str:
        """Scope view styling without overriding global scrollbars."""
        return f"""
            QTreeWidget {{ background: {C.BG_PANEL}; alternate-background-color: {C.BG_INPUT};
                color: {C.TEXT_SECONDARY}; border: none; font-size: 11px; }}
            QTreeWidget::item {{ height: 23px; padding: 0 4px; border: none; }}
            QTreeWidget::item:selected {{ background: {C.BG_DARKEST}; color: {C.TEXT_PRIMARY}; }}
            QTreeWidget::item:hover {{ background: {C.BG_ACTIVE}; }}
            QHeaderView::section {{ background: {C.BG_INPUT}; color: {C.TEXT_LABEL}; border: none;
                border-right: 1px solid {C.BORDER}; border-bottom: 1px solid {C.PANEL_DIVIDER}; padding: 3px 6px; }}
        """

    def _setup_pool_menus(self) -> None:
        """Expose existing actions through Resolve-style compact menus."""
        color_menu = QMenu(self)
        self._color_actions = {}
        for label in ASSET_COLOR_LABELS:
            action = color_menu.addAction(I18N.tr(f"edit.media_pool.label_{label}", label.title()))
            if label != "none":
                action.setIcon(qta.icon("fa5s.square", color=label))
            action.triggered.connect(lambda _checked=False, value=label: self._apply_pool_color_label(value))
            self._color_actions[label] = action
        color_menu.aboutToShow.connect(self._sync_color_actions)
        self.btn_color_tag.setMenu(color_menu)
        self.btn_color_tag.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        scope_menu = QMenu(self)
        scope_group = QActionGroup(scope_menu)
        for all_bins, key, text in ((False, "selected_bins", "Selected Bin"), (True, "all_bins", "All Bins")):
            action = scope_menu.addAction(I18N.tr(f"edit.media_pool.{key}", text))
            action.setCheckable(True)
            action.setChecked(not all_bins)
            scope_group.addAction(action)
            action.triggered.connect(lambda _checked=False, value=all_bins: self._set_search_scope(value))
        self.btn_search_scope.setMenu(scope_menu)
        self.btn_search_scope.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.sort_menu = QMenu(self)
        self._sort_actions = []
        group = QActionGroup(self.sort_menu)
        for index in range(self.sort_combo.count()):
            action = self.sort_menu.addAction(self.sort_combo.itemText(index))
            action.setCheckable(True)
            group.addAction(action)
            action.triggered.connect(lambda _checked=False, i=index: self.sort_combo.setCurrentIndex(i))
            self._sort_actions.append(action)
        self.sort_menu.addSeparator()
        order_group = QActionGroup(self.sort_menu)
        self._order_actions = []
        for order, key, label in (("asc", "sort_asc", "Ascending"), ("desc", "sort_desc", "Descending")):
            action = self.sort_menu.addAction(I18N.tr(f"edit.media_pool.{key}", label))
            action.setCheckable(True)
            order_group.addAction(action)
            action.triggered.connect(lambda _checked=False, value=order: self._set_sort_order(value))
            self._order_actions.append(action)
        self.sort_menu.aboutToShow.connect(self._sync_sort_menu)
        self.btn_sort.setMenu(self.sort_menu)
        self.btn_sort.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self)
        for key, label, callback in (
            ("import_media", "Import Media…", self._on_import_clicked),
            ("import_folder", "Import Folder…", self._on_import_folder_clicked),
            ("new_bin", "New Bin", self._on_new_bin_clicked),
            ("new_smart_bin", "New Smart Bin…", self._on_create_smart_bin),
        ):
            menu.addAction(I18N.tr(f"edit.media_pool.{key}", label)).triggered.connect(callback)
        menu.addSeparator()
        action = menu.addAction(I18N.tr("edit.media_pool.show_smart_bins", "Show Smart Bins"))
        action.setCheckable(True)
        action.setChecked(True)
        action.toggled.connect(self.smart_bin_tree.setVisible)
        self.action_show_smart_bins = action
        hover = menu.addAction(I18N.tr("edit.media_pool.live_preview", "Live Media Preview"))
        hover.setCheckable(True)
        hover.setChecked(True)
        hover.toggled.connect(self._set_live_preview)
        menu.addSeparator()
        menu.addAction(I18N.tr("edit.media_pool.collect_project", "Collect Project…")).triggered.connect(self._on_collect_project)
        menu.addAction(I18N.tr("edit.media_pool.consolidate", "Consolidate Media…")).triggered.connect(self._on_consolidate_project)
        menu.addAction(I18N.tr("edit.media_pool.proxy_policy", "Proxy Settings…")).triggered.connect(self._on_proxy_policy_settings)
        self.btn_more.setMenu(menu)
        self.btn_more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

    def _sync_color_actions(self) -> None:
        has_selection = bool(self.get_selected_asset_ids())
        for action in self._color_actions.values():
            action.setEnabled(has_selection)

    def _apply_pool_color_label(self, color_label: str) -> None:
        """Use the existing undoable log command for the selected clips."""
        if self._project is None or color_label not in ASSET_COLOR_LABELS:
            return
        asset_ids = self.get_selected_asset_ids()
        if not asset_ids:
            return
        command = BatchUpdateAssetLogCommand.create(
            asset_ids, color_label=color_label,
            label=I18N.tr("edit.media_pool.clip_color", "Clip Color"))
        self._project = self._command_stack.execute(command, self._project)
        self.project_changed.emit(self._project)
        self._refresh_tree()

    def _set_live_preview(self, enabled: bool) -> None:
        if self.grid_view is not None:
            self.grid_view.live_preview_enabled = enabled
            if not enabled:
                self.grid_view._hover_path = None
                self.grid_view._hover_frame_index = None
                self.grid_view._hover_frames.clear()
                self.grid_view.viewport().update()

    def _set_search_scope(self, all_bins: bool) -> None:
        self._search_all_bins = all_bins
        self.btn_search_scope.setToolTip(I18N.tr("edit.media_pool.all_bins" if all_bins else "edit.media_pool.selected_bins",
                                               "All Bins" if all_bins else "Selected Bin"))
        self._refresh_tree()

    def _set_sort_order(self, order: str) -> None:
        if self._sort_order != order:
            self._on_sort_order_toggled()

    def _sync_sort_menu(self) -> None:
        for i, action in enumerate(self._sort_actions):
            action.setChecked(self.sort_combo.itemData(i) == self._sort_by)
        for order, action in zip(("asc", "desc"), self._order_actions):
            action.setChecked(order == self._sort_order)

    def _show_column_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        for column in range(self.tree.columnCount()):
            action = menu.addAction(self.tree.headerItem().text(column))
            action.setCheckable(True)
            action.setChecked(not self.tree.isColumnHidden(column))
            action.setEnabled(column != 0)
            action.toggled.connect(lambda visible, c=column: self.tree.setColumnHidden(c, not visible))
        menu.exec(self.tree.header().mapToGlobal(pos))

    def _setup_grid_view(self) -> None:
        """Set up the grid view (thumbnail display)."""
        # Create grid view (use custom AssetListView for drag support)
        self.grid_view = AssetListView()

        # Model setup
        self.asset_model = AssetListModel(self)
        # Filtering and sorting are applied before items reach AssetListModel.
        # Keeping an unused proxy here made selection/thumbnail callbacks prone
        # to stale-index mapFromSource warnings during model resets.
        self.grid_view.setModel(self.asset_model)

        # Delegate setup
        from .media_pool_presentation import ResolvePoolDelegate
        self.grid_delegate = ResolvePoolDelegate(self)
        self.grid_delegate.poster_inset = 6
        self.grid_delegate.view = self.grid_view
        self.grid_view.setItemDelegate(self.grid_delegate)

        # Configure view
        self.grid_view.setViewMode(QListView.ViewMode.IconMode)
        self.grid_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid_view.setUniformItemSizes(True)  # Performance optimization
        self.grid_view.setSpacing(0)
        self.grid_view.setViewportMargins(15, 8, 0, 0)
        # Enable drag for assets (drag to timeline / drop into bins)
        self.grid_view.setDragEnabled(True)
        # DragDrop: support both outgoing drags (to timeline) and incoming
        # drops (asset/timeline -> bin). The view's dropEvent is overridden in
        # AssetListView to dispatch bin-targeted drops.
        self.grid_view.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.grid_view.setAcceptDrops(True)

        # Enable right-click context menu (same as list view)
        self.grid_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid_view.customContextMenuRequested.connect(self._on_grid_context_menu)

        # Column-count zoom: media thumbs are 16:9 with a caption strip the
        # delegate paints below the thumb.  The controller derives card
        # size from the live viewport so rows tile flush with the right
        # edge; its callback syncs the delegate's thumb_size (which drives
        # both painting and sizeHint) and re-requests thumbnails.
        from src.shared.ui.column_grid import ColumnGridController
        self._column_grid = ColumnGridController(
            self.grid_view, slider=None,
            settings_key="media_pool_columns", columns_default=3,
            gap=34, aspect=9.0 / 16.0, extra_height=0,
            pin_item_hints=False, scrollbar_as_needed=True,
            on_columns_applied=self._on_columns_applied)

        # Add to container — grid view 为默认视图
        if self.view_container:
            container_layout = self.view_container.layout()
            if container_layout:
                container_layout.addWidget(self.grid_view)
                self.grid_view.show()

        self.metadata_shelf = AssetListView()
        self.metadata_shelf.setModel(self.asset_model)
        self.metadata_shelf.setSelectionModel(self.grid_view.selectionModel())
        self.metadata_shelf.setViewMode(QListView.ViewMode.IconMode)
        self.metadata_shelf.setFlow(QListView.Flow.LeftToRight)
        self.metadata_shelf.setWrapping(True)
        self.metadata_shelf.setResizeMode(QListView.ResizeMode.Adjust)
        self.metadata_shelf.setMovement(QListView.Movement.Static)
        self.metadata_shelf.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.metadata_shelf.setDragEnabled(True)
        self.metadata_shelf.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.metadata_shelf.setAcceptDrops(True)
        self.metadata_shelf.setGridSize(QSize(116, 88))
        self.metadata_shelf.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.metadata_shelf.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.shelf_delegate = ResolvePoolDelegate(self)
        self.shelf_delegate.view = self.metadata_shelf
        self.shelf_delegate.set_thumb_size((86, 48))
        self.metadata_shelf.setItemDelegate(self.shelf_delegate)
        self.metadata_shelf.doubleClicked.connect(self._on_grid_double_clicked)
        self.metadata_shelf.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.metadata_shelf.customContextMenuRequested.connect(
            lambda pos: self._on_grid_context_menu(pos, self.metadata_shelf))
        self.view_container.layout().insertWidget(1, self.metadata_shelf)
        self.metadata_shelf.hide()
        logger.debug("Grid view setup complete")

    def _sync_metadata_shelf(self) -> None:
        """Group folders/sequences above media rows using shared model indexes."""
        if not hasattr(self, "metadata_shelf") or self.asset_model is None or self.grid_view is None:
            return
        is_metadata = self._view_mode == ViewMode.METADATA
        count = 0
        for row in range(self.asset_model.rowCount()):
            is_asset = self.asset_model.index(row).data(AssetListModel.ITEM_TYPE_ROLE) == "asset"
            self.metadata_shelf.setRowHidden(row, is_asset)
            self.grid_view.setRowHidden(row, is_metadata and not is_asset)
            count += int(not is_asset)
        self._metadata_shelf_count = count
        self.metadata_shelf.setVisible(is_metadata and count > 0)
        self._resize_metadata_shelf()

    def _resize_metadata_shelf(self) -> None:
        if not hasattr(self, "metadata_shelf"):
            return
        columns = max(1, (self.view_container.width() - 16) // 116)
        rows = (getattr(self, "_metadata_shelf_count", 0) + columns - 1) // columns
        self.metadata_shelf.setFixedHeight(max(88, min(176, rows * 88)))

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._resize_metadata_shelf()

    def _setup_context_menu(self) -> None:
        """Set up context menu for the panel."""
        self._context_menu = QMenu(self)

    def _connect_signals(self) -> None:
        """Connect internal signals."""
        # Search toggle
        self.btn_search.toggled.connect(self._on_search_toggled)

        # View mode toggle (mutually exclusive)
        self.btn_list_view.clicked.connect(self._toggle_list_view)
        self.btn_grid_view.clicked.connect(self._toggle_grid_view)
        self.btn_metadata_view.clicked.connect(self._toggle_metadata_view)

        # Thumbnail size slider — column-count model (range set by the
        # controller; card width follows the viewport width).
        self._column_grid.attach_slider(self.thumb_slider)

        # Search and sort
        self.search_box.search_changed.connect(self._on_search_filter_changed)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        self.btn_sort_order.clicked.connect(self._on_sort_order_toggled)

        # Bin tree signals
        self.bin_tree.bin_selected.connect(self._on_bin_selected)
        self.bin_tree.create_smart_bin_requested.connect(self._on_create_smart_bin)
        self.bin_tree.delete_smart_bin_requested.connect(self._on_delete_smart_bin)
        self.bin_tree.edit_smart_bin_requested.connect(self._on_edit_smart_bin)
        self.bin_tree.create_bin_requested.connect(self._on_new_bin_clicked)
        self.bin_tree.create_bin_in_requested.connect(lambda parent_id: self._create_bin(parent_id=parent_id))
        self.bin_tree.delete_bin_requested.connect(self._delete_bin)
        self.bin_tree.rename_bin_requested.connect(self._start_rename_bin)
        self.smart_bin_tree.bin_selected.connect(self._on_bin_selected)
        self.smart_bin_tree.create_smart_bin_requested.connect(self._on_create_smart_bin)
        self.smart_bin_tree.delete_smart_bin_requested.connect(self._on_delete_smart_bin)
        self.smart_bin_tree.edit_smart_bin_requested.connect(self._on_edit_smart_bin)

        # Grid view selection
        if self.grid_view:
            self.grid_view.selectionModel().selectionChanged.connect(
                self._on_grid_selection_changed
            )
            self.grid_view.doubleClicked.connect(self._on_grid_double_clicked)

    def _connect_thumbnail_signals(self) -> None:
        """Connect thumbnail provider signals."""
        # Thumbnail provider ready
        self._thumb_provider.thumbnail_ready.connect(self._on_thumbnail_ready)
        self._thumb_provider.frame_thumbnail_ready.connect(
            self._on_timeline_preview_frame_ready
        )

    # ==================== Project Binding ====================

    def set_project(self, project: Optional[EditProject]) -> None:
        """
        Set the current project reference.

        When a project is set, automatically checks all assets for offline status.

        Args:
            project: EditProject instance to bind
        """
        if self._updating:
            return

        if (
            self._project is not None
            and (project is None or project.project_id != self._project.project_id)
        ):
            # Do not let a late decode result from the previous project paint
            # a timeline card with the same ID in the new project.
            self._pending_timeline_frame_previews.clear()
            self._pending_timeline_image_previews.clear()
            # F-R07/R26：跨工程注销全部代理任务——旧工程的 completion/
            # error/探测回调不得写新工程模型或服务缓存。
            self._invalidate_all_proxy_jobs()
            # R04：跨工程取消在场的收集/整理任务——晚到回调按
            # （代际, 工程身份）丢弃，不向新工程弹结果/写归档。
            self._invalidate_media_management()
            # P0-4：跨工程丢弃旧查询结果（会话侧另有 project_id/revision
            # 守卫，这里清 UI 缓存避免渲染旧工程素材）。
            self._query_asset_ids = None
            self._last_query_fingerprint = ()

        # Timeline activation replaces the project dataclass but keeps the
        # pool-owned collections intact. Updating drag references is enough;
        # rebuilding every card here visibly stalls large pools for no gain.
        if self._offline_status_signature is not None and self._has_same_pool_content(project):
            self._project = project
            self.tree.set_project(project)
            self.bin_tree.set_project(project)
            self.smart_bin_tree.set_project(project)
            if self.grid_view:
                self.grid_view.set_project(project)
                self.metadata_shelf.set_project(project)
            self._update_status_label()
            return

        self._close_pool_info()
        offline_status_changed = False
        self._updating = True
        try:
            if (
                self._active_import_batch_id is not None
                and (
                    project is None
                    or self._active_import_project_id != project.project_id
                )
            ):
                self._cancel_import_batch(self._active_import_batch_id)

            # File availability checks use os.path.exists for every asset. Do
            # not repeat that work when only the active timeline changes.
            if project is not None:
                signature = self._offline_signature(project)
                if signature != self._offline_status_signature:
                    checked_project = update_offline_status(project)
                    offline_status_changed = checked_project is not project
                    project = checked_project
                    self._offline_status_signature = self._offline_signature(project)
                offline_count = sum(1 for a in project.assets.values() if a.is_offline)
                if offline_count > 0:
                    logger.info(f"Detected {offline_count} offline asset(s) in project")
            else:
                self._offline_status_signature = None

            self._project = project
            # Update project reference in tree and grid view for drag support
            self.tree.set_project(project)
            self.bin_tree.set_project(project)
            self.smart_bin_tree.set_project(project)
            if self.grid_view:
                self.grid_view.set_project(project)
                self.metadata_shelf.set_project(project)
            self._refresh_bin_tree()
            self._refresh_tree()
            self._update_status_label()
        finally:
            self._updating = False

        # M3：工程切换时通知分析 controller（cancel_all 旧工程的分析 + unbind），
        # 并清空 badge cache（旧工程的状态不带过来，§17.7）。
        new_pid = project.project_id if project is not None else None
        self._notify_analysis_project_changed(new_pid)
        self._analysis_status_cache.clear()
        self._refresh_analysis_badges()

        if offline_status_changed and project is not None:
            self.project_changed.emit(project)

    def _refresh_offline_status(self) -> None:
        """Refresh derived file availability while the project is open."""
        if self._updating or self._project is None:
            return

        # AUD-20202: os.path.exists on network/removable volumes can block for
        # hundreds of ms per asset. Never scan synchronously on the UI thread
        # (5s timer); run the scan on a pool worker and apply on the UI thread.
        if self._offline_scan_in_flight:
            return
        self._offline_scan_in_flight = True
        worker = OfflineScanWorker(self._project)
        worker.signals.finished.connect(self._on_offline_scan_finished)
        QThreadPool.globalInstance().start(worker)

    def _on_offline_scan_finished(self, source: object, checked: object) -> None:
        """Apply a background availability scan result (UI thread slot)."""
        self._offline_scan_in_flight = False
        if self._updating or self._project is None:
            return
        # The scan ran on a snapshot; if the panel's project was swapped in
        # the meantime (import, undo, timeline activation), discard the stale
        # result — the next timer tick rescans the new project.
        if source is not self._project or checked is None or checked is self._project:
            return
        if not isinstance(checked, EditProject):
            return

        # Force one normal UI refresh, then notify the host so the updated
        # immutable project reaches the timeline/preview controllers too.
        self._offline_status_signature = None
        self.set_project(checked)
        self.project_changed.emit(checked)

    @staticmethod
    def _offline_signature(project: EditProject) -> tuple[tuple[str, str, bool, bool], ...]:
        """Return the fields that determine an availability scan result."""
        return tuple(
            sorted(
                (
                    asset_id,
                    asset.path,
                    asset.is_comp_clip,
                    asset.is_offline,
                )
                for asset_id, asset in project.assets.items()
            )
        )

    def _has_same_pool_content(self, project: Optional[EditProject]) -> bool:
        """Whether a project update only changes state outside the media pool."""
        current = self._project
        return (
            project is not None
            and current is not None
            and project.project_id == current.project_id
            and project.assets is current.assets
            and project.bins is current.bins
            and project.timelines is current.timelines
        )

    def get_project(self) -> Optional[EditProject]:
        """Get the current project reference."""
        return self._project

    def set_comp_clip_preview(self, asset_id: str, pixmap: QPixmap) -> None:
        """Publish a rendered Composite preview to its Media Pool card."""
        if not asset_id or pixmap is None or pixmap.isNull() or self.asset_model is None:
            return
        asset = self._project.assets.get(asset_id) if self._project else None
        if asset is None or not asset.is_comp_clip:
            return
        self.asset_model.set_thumbnail(asset.path, pixmap)
        if self.grid_view is not None:
            self.grid_view.viewport().update()

    def clear_project(self) -> None:
        """Clear the current project reference and reset UI."""
        self._close_pool_info()
        if self._active_import_batch_id is not None:
            self._cancel_import_batch(self._active_import_batch_id)
            self._finish_import_batch()
        # M3：工程关闭时通知 controller 取消所有分析任务（§17.7：无 orphan worker）。
        self._notify_analysis_project_changed(None)
        self._analysis_status_cache.clear()
        self._project = None
        self._current_bin_id = ""
        self._current_bin_is_smart = False
        self._navigation_stack.clear()
        self._navigation_forward_stack.clear()
        self._refresh_timer.stop()
        self._current_timeline_id = None  # Clear active timeline
        self._offline_status_signature = None
        self._pending_timeline_frame_previews.clear()
        self._pending_timeline_image_previews.clear()
        # F-R07/R26：清空工程同样注销全部代理任务与 pending 探测
        self._invalidate_all_proxy_jobs()
        # R04：关闭工程取消在场的收集/整理任务（回调守卫同一纪律）
        self._invalidate_media_management()
        self.tree.clear()
        # AUD-20209: 树已清空 → 索引失效。
        self._tree_index_dirty = True
        self.bin_tree.populate(None)
        self.smart_bin_tree.populate(None)
        if self.asset_model:
            self.asset_model.set_items([])
        self._sync_metadata_shelf()
        self._update_breadcrumb()
        self._update_status_label()

    # ==================== M3: 媒体池可选快速分析（§17.2）==================== #
    #
    # 边界（遵循 .clinerules）：panel 不直接调 AnalysisService；只发 analysis_requested
    # 信号上报意图，由 host（edit_page）接到 MediaAnalysisController。badge 状态由
    # controller 的 analysis_status_changed 信号回灌，panel 据 cache 刷新（不阻塞 UI）。
    # 分析开关默认 off（M3 验收第 1 项：导入零 AI 推理）。

    def set_media_analysis_controller(self, controller: Any) -> None:
        """注入 MediaAnalysisController 并接驳状态信号（§17.2）。

        panel 不持有 controller 的强业务引用；只接 ``analysis_status_changed`` /
        ``analysis_completed`` 信号刷新 badge。意图（分析/取消/重试/清结果）经
        ``analysis_requested`` 等信号上报，由 host 转给 controller。
        """
        # 解绑旧 controller 信号（若有）
        if self._media_analysis_controller is not None:
            try:
                self._media_analysis_controller.analysis_status_changed.disconnect(
                    self._on_analysis_status_changed
                )
                self._media_analysis_controller.analysis_completed.disconnect(
                    self._on_analysis_completed
                )
            except (TypeError, RuntimeError):
                pass  # 信号未连接过
        self._media_analysis_controller = controller
        if controller is not None:
            controller.analysis_status_changed.connect(self._on_analysis_status_changed)
            controller.analysis_completed.connect(self._on_analysis_completed)
        self._refresh_analysis_badges()

    def _on_analysis_toggle(self, enabled: bool) -> None:
        """分析开关 toggled（§17.2）。开 → 菜单可见；关 → 隐藏分析项。"""
        self._media_analysis_enabled = enabled
        # 图标颜色反映开关状态
        color = C.ORANGE if enabled else C.TEXT_HINT
        self.btn_analysis_toggle.setIcon(pool_icon("analysis", color))
        self._refresh_analysis_badges()

    def _on_analysis_status_changed(self, asset_id: str, status: str) -> None:
        """controller 回报某 asset 的分析状态变化 → 更新 cache + 刷新 badge。"""
        self._analysis_status_cache[asset_id] = status
        self._refresh_analysis_badges()

    def _on_analysis_completed(self, *_args: Any) -> None:
        """一个 request 全部完成 → 刷新 badge（结果可见，§11.1 M3 退出条件）。"""
        self._refresh_analysis_badges()

    def _refresh_analysis_badges(self) -> None:
        """轻量刷新：触发 grid delegate 重绘（badge 点）。

        tree view 的 tooltip 在下次右键/重建时自然更新（避免全量 rebuild 8455 行性能）。
        """
        if self.grid_view is not None and self.grid_view.viewport() is not None:
            self.grid_view.viewport().update()

    def _emit_analysis_scope(self, scope: str) -> None:
        """收集 asset_ids 并发 analysis_requested 信号（§17.2 / §17.3）。

        scope: "selected"（_collect_selected_asset_ids）/ "bin"（_get_current_bin_assets）
            / "all"（project.assets.keys()）。
        "all" 范围由 host/controller 弹二次确认卡（§17.3 红线：所有素材必须列出范围
        并二次确认）。
        """
        if self._project is None:
            return
        if scope == "selected":
            asset_ids = self._collect_selected_asset_ids()
        elif scope == "bin":
            asset_ids = [a.asset_id for a in self._get_current_bin_assets()]
        elif scope == "all":
            asset_ids = list(self._project.assets.keys())
        else:
            return
        if not asset_ids:
            return
        self.analysis_requested.emit(asset_ids, scope)

    def _notify_analysis_project_changed(self, project_id: Optional[str]) -> None:
        """工程切换/关闭时通知 controller（cancel_all + unbind，§17.7）。"""
        if self._media_analysis_controller is not None:
            try:
                self._media_analysis_controller.on_project_changed(project_id)
            except Exception:
                logger.debug("analysis controller on_project_changed failed", exc_info=True)

    def _add_analysis_actions_to_menu(self, menu: QMenu, asset_id: str) -> None:
        """往右键菜单加单个 asset 的分析动作（§17.2）。

        动作随 badge 状态变化：unknown/ready → Analyze；running → Cancel；
        failed/stale → Retry；ready → Clear Result。均经信号上报，由 host 转 controller。
        """
        menu.addSeparator()
        analyze_act = menu.addAction(
            I18N.tr("edit.media_pool.analyze", "Analyze...")
        )
        analyze_act.triggered.connect(
            lambda aid=asset_id: self.analysis_requested.emit([aid], "selected")
        )
        status = self._analysis_status_cache.get(asset_id, "unknown")
        if status == "running":
            cancel_act = menu.addAction(
                I18N.tr("edit.media_pool.cancel_analysis", "Cancel Analysis")
            )
            cancel_act.triggered.connect(
                lambda aid=asset_id: self.analysis_cancel_requested.emit(aid)
            )
        elif status in ("failed", "stale"):
            retry_act = menu.addAction(
                I18N.tr("edit.media_pool.retry_analysis", "Retry Analysis")
            )
            retry_act.triggered.connect(
                lambda aid=asset_id: self.analysis_retry_requested.emit(aid)
            )
        elif status == "ready":
            clear_act = menu.addAction(
                I18N.tr("edit.media_pool.clear_analysis", "Clear Analysis Result")
            )
            clear_act.triggered.connect(
                lambda aid=asset_id: self.analysis_result_cleared.emit(aid)
            )

    def _analysis_status_tooltip_segment(self, asset_id: str) -> str:
        """构造分析状态的 tooltip 片段（tree view 用，§17.2 首版）。

        开关 off 或无记录 → 空串（不污染 tooltip）。状态标签与 badge 颜色一致。
        """
        if not self._media_analysis_enabled:
            return ""
        status = self._analysis_status_cache.get(asset_id, "unknown")
        labels = {
            "ready": I18N.tr("edit.media_pool.analysis_ready", "Analyzed"),
            "running": I18N.tr("edit.media_pool.analysis_running", "Analyzing..."),
            "failed": I18N.tr("edit.media_pool.analysis_failed", "Analysis Failed"),
            "stale": I18N.tr("edit.media_pool.analysis_stale", "Analysis Stale"),
            "pending": I18N.tr("edit.media_pool.analysis_pending", "Analysis Pending"),
        }
        label = labels.get(status)
        return f"[{label}]" if label else ""

    def set_current_timeline(self, timeline_id: Optional[str]) -> None:
        """
        Set the current active timeline ID.

        Updates the visual highlight in both list and grid views.

        Args:
            timeline_id: Timeline ID to set as active, or None to clear
        """
        if self._current_timeline_id == timeline_id:
            return  # No change

        self._current_timeline_id = timeline_id

        self._update_timeline_highlight_grid()
        for row in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(row)
            if item.data(0, Qt.ItemDataRole.UserRole + 1) == "timeline":
                active = item.data(0, Qt.ItemDataRole.UserRole) == timeline_id
                item.setForeground(0, QColor(C.ORANGE if active else C.TEXT_SECONDARY))

    def _update_timeline_highlight_grid(self) -> None:
        """Update timeline highlight in grid view (request repaint)."""
        if not self.grid_view or not self.asset_model:
            return

        self.asset_model.set_active_timeline(self._current_timeline_id)

    # ==================== View Mode Toggle ====================

    def _toggle_list_view(self) -> None:
        self._switch_view_mode(ViewMode.LIST)

    def _toggle_grid_view(self) -> None:
        self._switch_view_mode(ViewMode.GRID)

    def _toggle_metadata_view(self) -> None:
        self._switch_view_mode(ViewMode.METADATA)

    def _switch_view_mode(self, mode: ViewMode) -> None:
        """Share selection, navigation and commands across the three views."""
        if self._updating or self.grid_view is None or self.grid_delegate is None:
            return
        snapshot = self._capture_pool_selection()
        self._close_pool_info()
        self._updating = True
        try:
            self._view_mode = mode
            for button, target in ((self.btn_list_view, ViewMode.LIST),
                                   (self.btn_grid_view, ViewMode.GRID),
                                   (self.btn_metadata_view, ViewMode.METADATA)):
                button.setChecked(mode == target)
            self._column_grid.set_active(mode == ViewMode.GRID)
            self.thumb_slider.setVisible(mode == ViewMode.GRID)
            self.grid_delegate.metadata_mode = mode == ViewMode.METADATA
            if mode == ViewMode.METADATA:
                self.grid_view.setViewportMargins(0, 0, 0, 0)
                self.grid_view.setViewMode(QListView.ViewMode.ListMode)
                self.grid_view.setFlow(QListView.Flow.TopToBottom)
                self.grid_view.setWrapping(False)
                self.grid_view.setGridSize(QSize())
                self.grid_view.setSpacing(0)
                self.grid_view.setUniformItemSizes(True)
            elif mode == ViewMode.GRID:
                self.grid_view.setViewportMargins(15, 8, 0, 0)
                self.grid_view.setViewMode(QListView.ViewMode.IconMode)
                self.grid_view.setFlow(QListView.Flow.LeftToRight)
                self.grid_view.setWrapping(True)
                self.grid_view.setSpacing(0)
                self._column_grid.schedule_retile()
            self.tree.setVisible(mode == ViewMode.LIST)
            self.grid_view.setVisible(mode != ViewMode.LIST)
            if mode != ViewMode.LIST:
                self._populate_grid_view()
                self.grid_view.doItemsLayout()
            self._sync_metadata_shelf()
            self._restore_pool_selection(snapshot)
        finally:
            self._updating = False

    def _capture_pool_selection(self) -> tuple[list[tuple[str, str]], Optional[tuple[str, str]], int]:
        """Capture stable item identities rather than invalidatable indexes."""
        keys: list[tuple[str, str]] = []
        current_key = None
        if self._view_mode == ViewMode.LIST:
            for item in self.tree.selectedItems():
                keys.append((item.data(0, Qt.ItemDataRole.UserRole + 1), item.data(0, Qt.ItemDataRole.UserRole)))
            current = self.tree.currentItem()
            if current is not None:
                current_key = (current.data(0, Qt.ItemDataRole.UserRole + 1), current.data(0, Qt.ItemDataRole.UserRole))
            scroll = self.tree.verticalScrollBar().value()
        elif self.grid_view is not None:
            for index in self.grid_view.selectionModel().selectedIndexes():
                keys.append((index.data(AssetListModel.ITEM_TYPE_ROLE), index.data(AssetListModel.ITEM_ID_ROLE)))
            current = self.grid_view.currentIndex()
            if current.isValid():
                current_key = (current.data(AssetListModel.ITEM_TYPE_ROLE), current.data(AssetListModel.ITEM_ID_ROLE))
            scroll = self.grid_view.verticalScrollBar().value()
        else:
            scroll = 0
        return keys, current_key, scroll

    def _restore_pool_selection(self, snapshot: tuple[list[tuple[str, str]], Optional[tuple[str, str]], int]) -> None:
        """Restore every selected item silently, including bins and timelines."""
        keys, current_key, scroll = snapshot
        selected = set(keys)
        view = self.tree if self._view_mode == ViewMode.LIST else self.grid_view
        if view is None:
            return
        selection_model = view.selectionModel()
        previous = selection_model.blockSignals(True)
        previous_view = view.blockSignals(True)
        try:
            selection_model.clearSelection()
            if self._view_mode == ViewMode.LIST:
                for row in range(self.tree.topLevelItemCount()):
                    item = self.tree.topLevelItem(row)
                    key = (item.data(0, Qt.ItemDataRole.UserRole + 1), item.data(0, Qt.ItemDataRole.UserRole))
                    item.setSelected(key in selected)
                    if key == current_key:
                        self.tree.setCurrentItem(item, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
            elif self.asset_model is not None:
                for row in range(self.asset_model.rowCount()):
                    index = self.asset_model.index(row)
                    key = (index.data(AssetListModel.ITEM_TYPE_ROLE), index.data(AssetListModel.ITEM_ID_ROLE))
                    if key in selected:
                        selection_model.select(index, QItemSelectionModel.SelectionFlag.Select)
                    if key == current_key:
                        selection_model.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
            view.verticalScrollBar().setValue(scroll)
        finally:
            selection_model.blockSignals(previous)
            view.blockSignals(previous_view)

    def _navigate_to_sidebar_bin(self, bin_id: str) -> None:
        self._on_bin_selected(bin_id, False)
        self.bin_tree.select_bin(bin_id, False)

    def _on_toggle_sidebar(self, checked: bool) -> None:
        self.sidebar.setVisible(checked)
        if checked and self._project is not None:
            tree = self.smart_bin_tree if self._current_bin_is_smart else self.bin_tree
            tree.select_bin(self._current_bin_id or self._project.root_bin_id, self._current_bin_is_smart)

    def _on_search_toggled(self, checked: bool) -> None:
        """Toggle search bar visibility."""
        if checked:
            self.search_box.show()
            self.search_box.line_edit.setFocus()
        else:
            self.search_box.hide()
            self.search_box.line_edit.clear()

    def _on_search_filter_changed(self, text: str, filter_mode: str) -> None:
        """Handle search text or filter mode change from SearchLineEdit."""
        self._search_text = text
        self._search_filter_mode = filter_mode
        self._refresh_tree()

    # ==================== P2-2 Media Management 后台执行 ====================

    def _invalidate_media_management(self) -> None:
        """R04：切工程/关闭——使在场的收集/整理任务过期并请求取消。

        代际递增后，已排队/运行中的任务即使完成，其回调也会按
        （代际, project_id）双重校验丢弃；协作取消让核心尽快停在
        素材/分段边界。输出目录中的半成品由收集报告如实记录。
        """
        self._media_mgmt_session_generation += 1
        state = self._media_mgmt_state
        if state is not None and not state.get("done", False):
            try:
                state["worker"].cancel()
            except RuntimeError:  # noqa: BLE001 — 包装器已销毁等
                pass

    def _run_media_management(
        self,
        kind: str,
        work,
        on_finish,
        *,
        estimate_bytes: int,
        target_dir: str,
        project_id: str,
    ) -> None:
        """磁盘空间预检 + 后台执行收集/整理（进度对话框 + 协作取消）。

        核心已在后台线程运行（UI 不再同步复制/转码）；完成/失败/取消
        均回主线程收尾。空间不足直接拒绝（保守估算，宁可误拒）。
        R04：``project_id`` 为发起时工程身份——完成回调仅在面板仍在
        同一工程且代际未变时触发（切工程/关闭后丢弃晚到结果）。
        """
        if self._media_mgmt_running:
            QMessageBox.information(
                self._get_top_level_widget(),
                I18N.tr("edit.media_pool.media_mgmt_title", "Media Management"),
                I18N.tr(
                    "edit.media_pool.media_mgmt_busy",
                    "已有收集/整理任务在进行中，请等待完成或取消。",
                ),
            )
            return
        ok, free_bytes = preflight_disk_space(target_dir, estimate_bytes)
        if not ok:
            QMessageBox.warning(
                self._get_top_level_widget(),
                I18N.tr("edit.media_pool.media_mgmt_title", "Media Management"),
                I18N.tr(
                    "edit.media_pool.media_mgmt_no_space",
                    "目标目录空间不足：预估需要约 {need} MiB（保守估算），"
                    "当前可用 {free} MiB。",
                    need=estimate_bytes // (1024 * 1024),
                    free=free_bytes // (1024 * 1024),
                ),
            )
            return

        self._media_mgmt_running = True
        title = (I18N.tr("edit.media_pool.collect_title", "收集工程")
                 if kind == KIND_COLLECT
                 else I18N.tr("edit.media_pool.consolidate_title", "整理工程"))
        dialog = QProgressDialog(
            I18N.tr("edit.media_pool.media_mgmt_running", "正在处理…"), 
            I18N.tr("edit.media_pool.cancel", "取消"), 0, 0,
            self._get_top_level_widget(),
        )
        dialog.setWindowTitle(title)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setMinimumDuration(300)
        dialog.show()

        worker = MediaManagementWorker(kind, work)
        state = {
            "worker": worker,
            "dialog": dialog,
            "done": False,
            # R04：发起时的代际与工程身份（回调接受的双重守卫）
            "generation": self._media_mgmt_session_generation,
            "project_id": project_id,
        }
        self._media_mgmt_state = state

        def _settle() -> None:
            if state["done"]:
                return
            state["done"] = True
            if self._media_mgmt_state is state:
                self._media_mgmt_state = None
            self._media_mgmt_running = False
            dialog.reset()
            dialog.close()
            dialog.deleteLater()

        def _stale() -> bool:
            """R04：发起后切工程/关闭 → 本轮回调全部过期。"""
            return (
                state["generation"] != self._media_mgmt_session_generation
                or self._project is None
                or self._project.project_id != state["project_id"]
            )

        def _on_progress(kind_id: str, done: int, total: int) -> None:
            if kind_id != kind or total <= 0:
                return
            dialog.setMaximum(total)
            dialog.setValue(done)

        def _on_finished(kind_id: str, payload, report) -> None:
            if kind_id != kind:
                return
            _settle()
            if _stale():
                logger.warning(
                    "Media management result dropped (project switched, "
                    "kind=%s)", kind,
                )
                return
            on_finish(payload, report)

        def _on_failed(kind_id: str, message: str) -> None:
            if kind_id != kind:
                return
            _settle()
            QMessageBox.critical(
                self._get_top_level_widget(),
                I18N.tr("edit.media_pool.error", "Error"),
                message,
            )

        def _on_cancelled(kind_id: str) -> None:
            if kind_id != kind:
                return
            _settle()
            self._update_status_label()

        worker.signals.progress.connect(_on_progress)
        worker.signals.finished.connect(_on_finished)
        worker.signals.failed.connect(_on_failed)
        worker.signals.cancelled.connect(_on_cancelled)
        dialog.canceled.connect(worker.cancel)
        QThreadPool.globalInstance().start(worker)

    def _on_collect_project(self) -> None:
        """收集工程（P1-13）：媒体 + 工程包复制到自洽归档目录。

        收集后的工程路径指向归档内媒体副本并单独保存为
        <归档>/<工程名>.topos；不切换当前工作工程。
        """
        if not self._project:
            QMessageBox.warning(
                self._get_top_level_widget(),
                I18N.tr("edit.media_pool.no_project_title", "No Project"),
                I18N.tr("edit.media_pool.no_project_message", "Please create or open a project first.")
            )
            return

        target = QFileDialog.getExistingDirectory(
            self._get_top_level_widget(),
            I18N.tr("edit.media_pool.collect_target_title", "选择收集目标目录"),
            os.path.expanduser("~"),
        )
        if not target:
            return

        # R04：冻结发起时工程 + 序列——排队期间切工程不得混用
        # "新工程 + 旧时间线"（发起快照整体进任务，回调按身份守卫）。
        project = self._project
        sequences = self._collect_timeline_sequences()
        sequence_map = {
            getattr(seq, "timeline_id", None): seq
            for seq in sequences if getattr(seq, "timeline_id", None)
        }
        project_name = project.name or "collected"

        def _work(progress_cb, cancel_check):
            archived, report = collect_project(
                project, target_dir=target, sequences=sequences,
                progress_callback=progress_cb, cancelled_fn=cancel_check,
            )
            return archived, report

        self._run_media_management(
            KIND_COLLECT, _work,
            lambda archived, report: self._finish_collect(
                archived, report, target,
                project_name=project_name,
                timeline_sequences=sequence_map),
            estimate_bytes=estimate_collect_bytes(project,
                                                  sequences=sequences),
            target_dir=target,
            project_id=project.project_id,
        )
        return

    def _unique_archive_topos_path(self, target: str,
                                   project_name: str) -> str:
        """R02：归档工程包目标名也不覆盖既有文件（追加序号）。"""
        base = os.path.join(target, f"{project_name}.topos")
        if not os.path.exists(base):
            return base
        stem, ext = os.path.splitext(base)
        counter = 2
        while os.path.exists(f"{stem}_{counter}{ext}"):
            counter += 1
        return f"{stem}_{counter}{ext}"

    def _finish_collect(self, archived, report, target: str, *,
                        project_name: str = "",
                        timeline_sequences: Optional[dict] = None) -> None:
        """收集完成（主线程）：保存归档工程包 + 汇总报告（P2-2 后台化）。

        R01：``timeline_sequences``（发起时冻结的序列快照）随包保存——
        只存 timeline refs 不存序列时，重开归档时间线为空。R04：工程
        名取发起快照（不读当前面板工程）。保存失败按失败报告（warning
        级弹窗 + 明示"归档不完整"），不得提示完成。
        """
        # 收集出的工程包保存在归档目录内（媒体就在旁边，跨机器可开）
        topos_path = self._unique_archive_topos_path(
            target, project_name or "collected")
        try:
            io = EditProjectIO()
            saved = io.save_project(
                topos_path, archived,
                timeline_sequences=timeline_sequences or None)
        except Exception:  # noqa: BLE001
            logger.exception("Collect project save failed")
            saved = False
        if not saved:
            report.warnings = tuple(report.warnings) + (
                "工程包保存失败——归档不完整，请检查目标磁盘/权限后重试",)

        lines = [
            I18N.tr(
                "edit.media_pool.collect_summary",
                "已收集 {copied} 个媒体到:\n{root}",
                copied=len(report.copied), root=target,
            ),
        ]
        if report.missing:
            lines.append(
                I18N.tr(
                    "edit.media_pool.collect_missing",
                    "\n缺失 {n} 个素材（不影响收集，重开归档后可重链接）：",
                    n=len(report.missing),
                )
            )
            for asset_id, locations in list(report.missing_usage.items())[:8]:
                lines.append(f"  · {asset_id}")
                for loc in locations[:3]:
                    lines.append(f"      {loc}")
        if report.failures:
            lines.append(
                I18N.tr(
                    "edit.media_pool.collect_failed",
                    "\n{n} 个文件校验失败（哈希不一致/复制失败）！",
                    n=len(report.failures),
                )
            )
        lines.append(
            I18N.tr(
                "edit.media_pool.collect_saved",
                "\n工程包: {path}",
                path=topos_path if saved else I18N.tr(
                    "edit.media_pool.collect_save_failed", "保存失败（见日志）"
                ),
            )
        )
        # R01：保存失败与收集失败同级——不得以 information 级提示"完成"
        box = (QMessageBox.warning
               if (report.failures or not saved) else QMessageBox.information)
        box(
            self._get_top_level_widget(),
            I18N.tr("edit.media_pool.collect_title", "收集工程"),
            "\n".join(lines),
        )

    def _on_consolidate_project(self) -> None:
        """整理工程（P1-13）：按时间线使用范围裁剪收集，产出更小的自洽归档。

        与"收集工程"（整文件复制）互补：每个素材只收集时间线实际使用的
        源区间（±手柄），clip 重映射到分段素材；归档工程包保存在目标目录
        内，不切换当前工作工程。
        """
        if not self._project:
            QMessageBox.warning(
                self._get_top_level_widget(),
                I18N.tr("edit.media_pool.no_project_title", "No Project"),
                I18N.tr("edit.media_pool.no_project_message", "Please create or open a project first.")
            )
            return

        # R04：冻结发起时工程 + 序列（与收集路径同一快照纪律）
        project = self._project
        sequences = self._collect_timeline_sequences()
        has_clips = any(
            getattr(track, "clips", ())
            for seq in sequences
            for track in tuple(getattr(seq, "video_tracks", ()))
            + tuple(getattr(seq, "audio_tracks", ()))
        )
        if not has_clips:
            QMessageBox.information(
                self._get_top_level_widget(),
                I18N.tr("edit.media_pool.consolidate_title", "整理工程"),
                I18N.tr(
                    "edit.media_pool.consolidate_no_clips",
                    "时间线上没有素材引用，无需整理。",
                ),
            )
            return

        dialog = ConsolidateOptionsDialog(self._get_top_level_widget())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        mode, handles = dialog.values()

        target = QFileDialog.getExistingDirectory(
            self._get_top_level_widget(),
            I18N.tr("edit.media_pool.consolidate_target_title", "选择整理目标目录"),
            os.path.expanduser("~"),
        )
        if not target:
            return

        def _work(progress_cb, cancel_check):
            new_project, new_seqs, report = consolidate_project(
                project, sequences=sequences, target_dir=target,
                mode=mode, handles_frames=handles,
                progress_cb=progress_cb, cancel_check=cancel_check,
            )
            # R01：重映射后的新序列随任务载荷返回——归档工程包必须保存
            # 它们（旧序列引用的是整理前的源路径，写进包=断链）。
            sequence_map = {
                getattr(seq, "timeline_id", None): seq
                for seq in new_seqs if getattr(seq, "timeline_id", None)
            }
            return (new_project, sequence_map), report

        self._run_media_management(
            KIND_CONSOLIDATE, _work,
            lambda payload, report: self._finish_consolidate(
                payload[0], report, target,
                project_name=project.name or "consolidated",
                timeline_sequences=payload[1]),
            estimate_bytes=estimate_consolidate_bytes(project, sequences),
            target_dir=target,
            project_id=project.project_id,
        )
        return

    def _finish_consolidate(self, new_project, report, target: str, *,
                            project_name: str = "",
                            timeline_sequences: Optional[dict] = None) -> None:
        """整理完成（主线程）：保存归档工程包 + 汇总报告（P2-2 后台化）。

        R01：保存**重映射后的序列**（分段素材的新引用）；R04：工程名取
        发起快照；保存失败按失败报告（与收集路径同一纪律）。
        """
        topos_path = self._unique_archive_topos_path(
            target, project_name or "consolidated")
        try:
            io = EditProjectIO()
            saved = io.save_project(
                topos_path, new_project,
                timeline_sequences=timeline_sequences or None)
        except Exception:  # noqa: BLE001
            logger.exception("Consolidate project save failed")
            saved = False
        if not saved:
            report.warnings = tuple(report.warnings) + (
                "工程包保存失败——归档不完整，请检查目标磁盘/权限后重试",)

        def _mb(num_bytes: int) -> str:
            return f"{num_bytes / 1024 / 1024:.1f} MB"

        lines = [
            I18N.tr(
                "edit.media_pool.consolidate_summary",
                "已生成 {n} 个分段（{segs}，节省 {saved}）到:\n{root}",
                n=len(report.consolidated),
                segs=_mb(report.segment_bytes),
                saved=_mb(max(0, report.source_bytes - report.segment_bytes)),
                root=target,
            ),
            I18N.tr(
                "edit.media_pool.consolidate_remapped",
                "重映射 {n} 个剪辑引用",
                n=len(report.remapped_clips),
            ),
        ]
        if report.missing:
            lines.append(
                I18N.tr(
                    "edit.media_pool.collect_missing",
                    "\n缺失 {n} 个素材（不影响整理，重开归档后可重链接）：",
                    n=len(report.missing),
                )
            )
        if report.failures:
            lines.append(
                I18N.tr(
                    "edit.media_pool.collect_failed",
                    "\n{n} 个分段失败（损坏媒体/ffmpeg 错误，见日志）！",
                    n=len(report.failures),
                )
            )
        if report.unmapped_clip_ids:
            lines.append(
                I18N.tr(
                    "edit.media_pool.consolidate_unmapped",
                    "\n{n} 个剪辑未能映射到分段，已保持原样！",
                    n=len(report.unmapped_clip_ids),
                )
            )
        lines.append(
            I18N.tr(
                "edit.media_pool.collect_saved",
                "\n工程包: {path}",
                path=topos_path if saved else I18N.tr(
                    "edit.media_pool.collect_save_failed", "保存失败（见日志）"
                ),
            )
        )
        # R01：保存失败与整理失败同级——不得以 information 级提示"完成"
        box = (QMessageBox.warning
               if (not report.passed or not saved) else QMessageBox.information)
        box(
            self._get_top_level_widget(),
            I18N.tr("edit.media_pool.consolidate_title", "整理工程"),
            "\n".join(lines),
        )

    def _smart_bin_dialog(self, existing: Any = None) -> Any:
        """R14（媒体池复查）：Smart Bin 创建/编辑共用表单（能力分组）。

        字段覆盖平铺 filter_params 的能力面：媒体类型 / 关键词（任一）
        / 评级下限 / 旗标 / 颜色标签 / 注释包含 / 最小宽度 / 最小帧率
        ——提交经 CreateSmartBinCommand / UpdateSmartBinRulesCommand
        （单步撤销）。使用 v2 规则树（嵌套 AND/OR）的 Smart Bin 只允许
        改名并明示"高级规则保持不变"（不静默降级）。
        """
        from src.features.edit.core.project import (
            ASSET_COLOR_LABELS,
            ASSET_FLAG_VALUES,
        )
        from src.features.edit.core.smart_bin_rules import has_rule_tree

        params: dict = dict(getattr(existing, "filter_params", None) or {})
        rule_tree_bin = has_rule_tree(params)
        editing = existing is not None

        dialog = QDialog(self._get_top_level_widget())
        dialog.setWindowTitle(
            I18N.tr("edit.media_pool.edit_smart_bin", "编辑 Smart Bin")
            if editing else
            I18N.tr("edit.media_pool.new_smart_bin", "新建 Smart Bin"))
        form = QFormLayout(dialog)

        name_edit = QLineEdit()
        name_edit.setPlaceholderText(
            I18N.tr("edit.media_pool.smart_bin_name_hint", "如：访谈精选"))
        name_edit.setText(getattr(existing, "name", "") or "")
        form.addRow(I18N.tr("edit.media_pool.smart_bin_name", "名称:"), name_edit)

        if rule_tree_bin:
            # 能力声明式限制：规则树超出本表单能力面——诚实显示，
            # 不把树降级成平铺参数（降级 = 静默丢规则）。
            note = QLabel(I18N.tr(
                "edit.media_pool.smart_bin_rule_tree_note",
                "该 Smart Bin 使用高级规则树（嵌套 AND/OR），"
                "此处仅可改名——规则保持不变。"))
            note.setWordWrap(True)
            form.addRow(note)
        else:
            def _combo(items: tuple, current: Any, any_label: str) -> QComboBox:
                box = QComboBox()
                box.addItem(I18N.tr("edit.media_pool.rating_any", any_label), None)
                for value in items:
                    box.addItem(str(value), value)
                index = box.findData(current) if current is not None else 0
                box.setCurrentIndex(index if index >= 0 else 0)
                return box

            media_combo = _combo(
                ("video", "audio", "image"), params.get("media_type"),
                "不限")
            keywords_edit = QLineEdit()
            keywords_edit.setPlaceholderText(I18N.tr(
                "edit.media_pool.keywords_hint", "逗号或顿号分隔，如: 采访, 室内"))
            keywords_edit.setText(
                ", ".join(params.get("keywords") or ()))
            rating_combo = QComboBox()
            rating_combo.addItem(
                I18N.tr("edit.media_pool.rating_any", "不限"), None)
            for value in range(1, 6):
                rating_combo.addItem("★" * value, value)
            rating = params.get("min_rating")
            rating_combo.setCurrentIndex(rating if isinstance(rating, int) else 0)
            flag_combo = _combo(ASSET_FLAG_VALUES, params.get("flag"), "不限")
            color_combo = _combo(tuple(ASSET_COLOR_LABELS),
                                 params.get("color_label"), "不限")
            comment_edit = QLineEdit()
            comment_edit.setText(params.get("comment_contains") or "")
            width_spin = QSpinBox()
            width_spin.setRange(0, 16000)
            width_spin.setValue(int(params.get("min_width") or 0))
            width_spin.setSpecialValueText(
                I18N.tr("edit.media_pool.unlimited", "不限"))
            fps_spin = QDoubleSpinBox()
            fps_spin.setRange(0.0, 240.0)
            fps_spin.setDecimals(3)
            fps_spin.setValue(float(params.get("min_fps") or 0.0))
            fps_spin.setSpecialValueText(
                I18N.tr("edit.media_pool.unlimited", "不限"))

            form.addRow(I18N.tr("edit.media_pool.media_type", "媒体类型:"), media_combo)
            form.addRow(I18N.tr("edit.media_pool.keywords", "关键词:"), keywords_edit)
            form.addRow(I18N.tr("edit.media_pool.min_rating", "最低评级:"), rating_combo)
            form.addRow(I18N.tr("edit.media_pool.flag", "旗标:"), flag_combo)
            form.addRow(I18N.tr("edit.media_pool.color_label", "颜色标签:"), color_combo)
            form.addRow(I18N.tr("edit.media_pool.comment_contains", "注释包含:"), comment_edit)
            form.addRow(I18N.tr("edit.media_pool.min_width", "最小宽度:"), width_spin)
            form.addRow(I18N.tr("edit.media_pool.min_fps", "最小帧率:"), fps_spin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None

        name = name_edit.text().strip()
        if not name:
            return None

        if rule_tree_bin:
            # 仅改名；规则树原样保留
            return SmartBin(
                bin_id=getattr(existing, "bin_id", ""),
                name=name,
                filter_type=getattr(existing, "filter_type", "custom"),
                filter_params=params,
            )

        # 平铺能力字段（None/空 = 不限，不落入 params）
        media_type = media_combo.currentData()
        raw = keywords_edit.text().replace("、", ",")
        keywords = tuple(kw.strip() for kw in raw.split(",") if kw.strip())
        min_rating = rating_combo.currentData()
        flag = flag_combo.currentData()
        color_label = color_combo.currentData()
        comment = comment_edit.text().strip()
        min_width = width_spin.value()
        min_fps = fps_spin.value()

        filter_params: dict = {}
        if media_type:
            filter_params["media_type"] = media_type
        if keywords:
            filter_params["keywords"] = list(keywords)
        if isinstance(min_rating, int):
            filter_params["min_rating"] = min_rating
        if flag:
            filter_params["flag"] = flag
        if color_label:
            filter_params["color_label"] = color_label
        if comment:
            filter_params["comment_contains"] = comment
        if min_width > 0:
            filter_params["min_width"] = min_width
        if min_fps > 0.0:
            filter_params["min_fps"] = min_fps

        return SmartBin(
            bin_id=(getattr(existing, "bin_id", None)
                    or f"sb-{uuid.uuid4().hex[:8]}"),
            name=name,
            filter_type="custom",
            filter_params=filter_params,
        )

    def _on_create_smart_bin(self) -> None:
        """新建用户 Smart Bin（R14：能力分组表单，单步撤销）。"""
        if not self._project:
            QMessageBox.warning(
                self._get_top_level_widget(),
                I18N.tr("edit.media_pool.no_project_title", "No Project"),
                I18N.tr("edit.media_pool.no_project_message", "Please create or open a project first.")
            )
            return
        smart_bin = self._smart_bin_dialog()
        if smart_bin is None:
            return
        cmd = CreateSmartBinCommand.create(smart_bin)
        self._project = self._command_stack.execute(cmd, self._project)
        self.project_changed.emit(self._project)
        self._refresh_bin_tree()

    def _on_edit_smart_bin(self, bin_id: str) -> None:
        """R14：编辑用户 Smart Bin（UpdateSmartBinRulesCommand，单步撤销）。"""
        if not self._project:
            return
        existing = self._project.smart_bins.get(bin_id)
        if existing is None:
            return
        smart_bin = self._smart_bin_dialog(existing)
        if smart_bin is None:
            return
        try:
            cmd = UpdateSmartBinRulesCommand.create(smart_bin)
            self._project = self._command_stack.execute(cmd, self._project)
        except (ValueError, Exception) as exc:  # noqa: BLE001 — 规则校验失败明示
            QMessageBox.warning(
                self._get_top_level_widget(),
                I18N.tr("edit.media_pool.error", "Error"), str(exc))
            return
        self.project_changed.emit(self._project)
        self._refresh_bin_tree()

    def _on_delete_smart_bin(self, bin_id: str) -> None:
        """删除用户自建 Smart Bin（可撤销）。"""
        if not self._project or bin_id not in self._project.smart_bins:
            return
        cmd = DeleteSmartBinCommand.create(bin_id)
        self._project = self._command_stack.execute(cmd, self._project)
        self.project_changed.emit(self._project)
        # 若正停在被删的 bin 上，回到根层级
        if self._current_bin_id == bin_id and self._current_bin_is_smart:
            self._current_bin_id = ""
            self._current_bin_is_smart = False
        self._refresh_bin_tree()
        self._refresh_tree()

    def _on_bin_selected(self, bin_id: str, is_smart_bin: bool) -> None:
        """Navigate from either sidebar section and synchronize the path."""
        if self._updating or self._project is None:
            return
        self._current_bin_id = "" if bin_id == self._project.root_bin_id else bin_id
        self._current_bin_is_smart = is_smart_bin
        self._navigation_forward_stack.clear()
        self._navigation_stack.clear()
        if self._current_bin_id and not is_smart_bin:
            ancestors: list[str] = []
            cursor = self._project.bins.get(self._current_bin_id)
            visited = {self._current_bin_id}
            while cursor is not None and cursor.parent_id and cursor.parent_id not in visited:
                parent_id = cursor.parent_id
                visited.add(parent_id)
                if parent_id != self._project.root_bin_id:
                    ancestors.append(parent_id)
                cursor = self._project.bins.get(parent_id)
            self._navigation_stack = [self._project.root_bin_id] + list(reversed(ancestors))
        other = self.bin_tree if is_smart_bin else self.smart_bin_tree
        previous = other.blockSignals(True)
        other.setCurrentItem(None)
        other.clearSelection()
        other.blockSignals(previous)
        self._update_breadcrumb()
        self._refresh_tree()

    def _refresh_bin_tree(self) -> None:
        """Refresh regular and Smart Bins, preserving expanded folders."""
        expanded: set[str] = set()
        iterator = QTreeWidgetItemIterator(self.bin_tree)
        had_items = self.bin_tree.topLevelItemCount() > 0
        while iterator.value() is not None:
            item = iterator.value()
            if item.isExpanded():
                expanded.add(item.data(0, BinTreeWidget.BIN_ID_ROLE))
            iterator += 1
        for tree, smart_only in ((self.bin_tree, False), (self.smart_bin_tree, True)):
            previous = tree.blockSignals(True)
            try:
                tree.set_project(self._project)
                tree.populate(self._project)
                for row in range(tree.topLevelItemCount()):
                    item = tree.topLevelItem(row)
                    is_section = item.data(0, BinTreeWidget.BIN_TYPE_ROLE) == "section"
                    item.setHidden(is_section != smart_only)
                if self._project and self._current_bin_is_smart == smart_only:
                    tree.select_bin(self._current_bin_id or self._project.root_bin_id, smart_only)
            finally:
                tree.blockSignals(previous)
        if had_items:
            iterator = QTreeWidgetItemIterator(self.bin_tree)
            while iterator.value() is not None:
                item = iterator.value()
                item.setExpanded(item.data(0, BinTreeWidget.BIN_ID_ROLE) in expanded)
                iterator += 1
        if self._project and self._current_bin_id and not self._current_bin_is_smart and self._current_bin_id not in self._project.bins:
            self._current_bin_id = ""
            self._navigation_stack.clear()
        self._update_breadcrumb()

    def _on_sort_changed(self, index: int) -> None:
        """Handle sort criteria change."""
        self._sort_by = self.sort_combo.currentData()
        self._persist_sort_prefs()
        self._refresh_tree()

    def _on_sort_order_toggled(self) -> None:
        """Toggle sort order (asc/desc)."""
        if self._sort_order == "asc":
            self._sort_order = "desc"
            self.btn_sort_order.setIcon(icon_arrow_down())
            self.btn_sort_order.setToolTip(I18N.tr("edit.media_pool.sort_desc", "Descending"))
        else:
            self._sort_order = "asc"
            self.btn_sort_order.setIcon(icon_arrow_up())
            self.btn_sort_order.setToolTip(I18N.tr("edit.media_pool.sort_asc", "Ascending"))
        self._persist_sort_prefs()
        self._refresh_tree()

    # Column index -> sort key for click-to-sort (Name=0, Duration=3).
    _COLUMN_SORT_MAP = {
        0: SORT_BY_NAME, 1: "reel", 2: "scene", 3: SORT_BY_DURATION,
        4: "fps", 5: "resolution", 6: "codec", 7: "audio", 8: "path",
    }

    def _on_header_clicked(self, column: int) -> None:
        """Handle column-header click: sort by that column.

        Clicking the column already sorted by toggles the direction; clicking
        a different column switches the sort key (ascending). Mirrors the
        Finder/Premiere column-header sort UX.
        """
        sort_key = self._COLUMN_SORT_MAP.get(column)
        if sort_key is None:
            return  # Unsupported column (no real sort key).
        if sort_key == self._sort_by:
            # Same column — flip direction.
            new_order = "desc" if self._sort_order == "asc" else "asc"
        else:
            self._sort_by = sort_key
            new_order = "asc"
            # Keep the toolbar combo in sync.
            for i in range(self.sort_combo.count()):
                if self.sort_combo.itemData(i) == sort_key:
                    self.sort_combo.blockSignals(True)
                    self.sort_combo.setCurrentIndex(i)
                    self.sort_combo.blockSignals(False)
                    break
        if new_order != self._sort_order:
            self._sort_order = new_order
            if new_order == "asc":
                self.btn_sort_order.setIcon(icon_arrow_up())
                self.btn_sort_order.setToolTip(I18N.tr("edit.media_pool.sort_asc", "Ascending"))
            else:
                self.btn_sort_order.setIcon(icon_arrow_down())
                self.btn_sort_order.setToolTip(I18N.tr("edit.media_pool.sort_desc", "Descending"))
        self._persist_sort_prefs()
        self._refresh_tree()

    def _sync_sort_indicator(self) -> None:
        column = next((c for c, key in self._COLUMN_SORT_MAP.items() if key == self._sort_by), None)
        self.tree.header().setSortIndicatorShown(column is not None)
        if column is not None:
            self.tree.header().setSortIndicator(column, Qt.SortOrder.AscendingOrder if self._sort_order == "asc" else Qt.SortOrder.DescendingOrder)

    def _persist_sort_prefs(self) -> None:
        """Save sort key/order to QSettings so they survive restarts."""
        self._sync_sort_indicator()
        try:
            from PyQt6.QtCore import QSettings

            settings = QSettings("ToposColor", "MediaPool")
            settings.setValue("sort_by", self._sort_by)
            settings.setValue("sort_order", self._sort_order)
        except Exception:  # noqa: BLE001
            pass

    def _restore_sort_prefs(self) -> None:
        """Restore sort key/order from QSettings if present."""
        try:
            from PyQt6.QtCore import QSettings

            settings = QSettings("ToposColor", "MediaPool")
            saved_by = settings.value("sort_by")
            saved_order = settings.value("sort_order")
            if saved_by in {self.sort_combo.itemData(i) for i in range(self.sort_combo.count())}:
                self._sort_by = saved_by
                for i in range(self.sort_combo.count()):
                    if self.sort_combo.itemData(i) == saved_by:
                        self.sort_combo.blockSignals(True)
                        self.sort_combo.setCurrentIndex(i)
                        self.sort_combo.blockSignals(False)
                        break
            if saved_order in ("asc", "desc"):
                self._sort_order = saved_order
                if saved_order == "desc":
                    self.btn_sort_order.setIcon(icon_arrow_down())
                else:
                    self.btn_sort_order.setIcon(icon_arrow_up())
        except Exception:  # noqa: BLE001
            pass

    def _sort_assets(self, assets: List[MediaAsset]) -> List[MediaAsset]:
        """Sort assets by current sort criteria."""
        reverse = self._sort_order == "desc"

        if self._sort_by == SORT_BY_NAME:
            return sorted(assets, key=lambda a: a.display_name.lower(), reverse=reverse)
        elif self._sort_by == SORT_BY_DATE:
            return sorted(assets, key=lambda a: a.created_at, reverse=reverse)
        elif self._sort_by == SORT_BY_TYPE:
            return sorted(assets, key=lambda a: a.media_type, reverse=reverse)
        elif self._sort_by == SORT_BY_DURATION:
            # Assets without a duration (normally stills) remain after timed
            # media for both ascending and descending sorts.
            timed = [asset for asset in assets if asset.duration_seconds is not None]
            untimed = [asset for asset in assets if asset.duration_seconds is None]
            return sorted(
                timed,
                key=lambda asset: asset.duration_seconds or 0.0,
                reverse=reverse,
            ) + untimed

        fields = {
            "reel": lambda a: (a.reel or "").casefold(),
            "scene": lambda a: (a.production_info_str or "").casefold(),
            "fps": lambda a: a.fps or 0.0,
            "resolution": lambda a: (a.width or 0) * (a.height or 0),
            "codec": lambda a: (a.codec or "").casefold(),
            "audio": lambda a: a.audio_channels or 0,
            "path": lambda a: a.path.casefold(),
        }
        key = fields.get(self._sort_by)
        return sorted(assets, key=key, reverse=reverse) if key is not None else assets

    def _update_grid_size(self) -> None:
        """更新网格视图尺寸 — 尺寸计算归列数控制器所有"""
        grid = getattr(self, "_column_grid", None)
        if grid is not None:
            grid.schedule_retile()
        elif self.grid_view and self.grid_delegate:
            w = self._thumb_width
            h = round(w * 9 / 16)
            self.grid_delegate.set_thumb_size((w, h))
            self.grid_view.setGridSize(QSize(w + 14, h + 38))
            self.grid_view.viewport().update()

    def _reload_grid_thumbnails(self) -> None:
        """重新加载网格视图缩略图（尺寸变化时）"""
        if not self._project or not self.asset_model:
            return

        # 清空模型缓存，强制重新请求
        self.asset_model._thumbnails.clear()

        # 从 ThumbnailProvider 全局缓存获取已有缩略图（使用新尺寸）
        thumb_size = self._thumb_width
        thumb_provider = ThumbnailProvider.instance()

        for item in self.asset_model._items:
            if item.is_asset and isinstance(item.data, MediaAsset):
                if item.data.is_motion_graphic:
                    continue
                preview_path = self._thumbnail_path_for_asset(item.data)
                cached = thumb_provider.get_cached_thumbnail(preview_path, thumb_size)
                if cached:
                    self.asset_model._thumbnails[preview_path] = cached
                    self.asset_model._thumbnails[item.data.path] = cached
                else:
                    # 请求新尺寸的缩略图
                    thumb_provider.get_thumbnail(preview_path, thumb_size)

        # 触发视图更新
        self.asset_model.layoutChanged.emit()
        if self.grid_view:
            self.grid_view.viewport().update()

    # ==================== Thumbnail Size ====================

    def _on_columns_applied(self, icon: QSize, cell: QSize) -> None:
        """列数控制器回调：同步委托 thumb_size 并按新尺寸重取缩略图。"""
        self._thumb_width = icon.width()
        if self.grid_delegate:
            self.grid_delegate.set_thumb_size((icon.width(), icon.height()))
        if self._view_mode != ViewMode.LIST:
            self._reload_grid_thumbnails()
        logger.debug(f"Thumbnail columns applied: {icon.width()}x{icon.height()}px")

    def _thumbnail_path_for_asset(self, asset: MediaAsset) -> str:
        """Return the async poster path for a media-pool asset."""
        if asset.is_comp_clip:
            source_id = asset.metadata.get("original_asset_id", "")
            source = self._project.assets.get(source_id) if self._project and source_id else None
            if isinstance(source, MediaAsset):
                return source.path
        return asset.path

    # ==================== Thumbnail Loading ====================

    def _rebuild_tree_item_index(self) -> None:
        """重建 path → 树节点索引（AUD-20209）。

        树在 ``_delayed_refresh`` 中整体重建，索引随树懒重建一次（O(n)），
        之后每张缩略图按 path O(1) 命中，整体 O(n + P) 取代 O(n·P)。
        """
        index: Dict[str, List[QTreeWidgetItem]] = {}
        if self._project is not None:
            for i in range(self.tree.topLevelItemCount()):
                top_level_item = self.tree.topLevelItem(i)
                if top_level_item is None:
                    continue
                candidates = [top_level_item] + [
                    top_level_item.child(j)
                    for j in range(top_level_item.childCount())
                ]
                for asset_item in candidates:
                    if asset_item is None:
                        continue
                    asset_id = asset_item.data(0, Qt.ItemDataRole.UserRole)
                    asset = self._project.assets.get(asset_id)
                    if asset is not None:
                        index.setdefault(asset.path, []).append(asset_item)
        self._tree_items_by_path = index
        self._tree_index_dirty = False

    def _on_thumbnail_ready(self, path: str, pixmap: QPixmap) -> None:
        """缩略图加载完成"""
        if self._updating:
            return

        # List items are currently top-level entries, but this also supports
        # nested asset items used by older project layouts.
        icon_pixmap = pixmap.scaled(
            32, 32,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        # AUD-20209: 用懒重建的 path → items 索引替代每张缩略图的全树扫描
        # （P 张缩略图 × n 个节点的 O(n²) 扫描 → 一次 O(n) 重建 + O(1) 命中）。
        if self._tree_index_dirty:
            self._rebuild_tree_item_index()
        for asset_item in self._tree_items_by_path.get(path, []):
            asset_item.setIcon(0, QIcon(icon_pixmap))

        # Grid view delegate handles its own updates via signal connection

        # Image timelines use the regular thumbnail path rather than a frame
        # seek. Share the result with every visible timeline that references
        # this image asset.
        if self.asset_model is not None:
            for timeline_id in self._pending_timeline_image_previews.pop(path, set()):
                self.asset_model.set_timeline_preview(timeline_id, pixmap)

    def _on_timeline_preview_frame_ready(
        self, path: str, frame_index: int, pixmap: QPixmap
    ) -> None:
        """Publish an asynchronously decoded frame to timeline cards."""
        if self.asset_model is None:
            return
        key = (path, int(frame_index))
        for timeline_id in self._pending_timeline_frame_previews.pop(key, set()):
            self.asset_model.set_timeline_preview(timeline_id, pixmap)

    # ==================== Grid Selection ====================

    def _on_grid_selection_changed(
        self,
        selected: Any,
        deselected: Any
    ) -> None:
        """网格视图选择变化。

        只在"恰好选中一个 asset"时才发射 ``asset_selected`` 触发 Source
        Viewer 预览；多选或框选（rubber band）期间不预览，避免每帧都
        全量解码（Finder/NLE 通用语义：多选时不切换预览）。
        """
        if self._updating:
            return

        if not self.asset_model:
            return

        sel_model = self.grid_view.selectionModel() if self.grid_view else None
        if sel_model is None:
            return

        if len(sel_model.selectedIndexes()) != 1:
            return

        # A selected timeline is inspectable without switching the active
        # sequence.  Route it to the contextual Info panel and stop here.
        timeline_indexes = [
            idx for idx in sel_model.selectedIndexes()
            if idx.isValid()
            and idx.model() is self.asset_model
            and self.asset_model.data(idx, AssetListModel.ITEM_TYPE_ROLE) == "timeline"
        ]
        if len(timeline_indexes) == 1:
            timeline_id = self.asset_model.data(
                timeline_indexes[0], AssetListModel.ITEM_ID_ROLE
            )
            if timeline_id:
                self.timeline_selected.emit(timeline_id)
            return

        # Collect the currently-selected asset indexes (exclude bins/timelines).
        asset_indexes = [
            idx for idx in sel_model.selectedIndexes()
            if idx.isValid()
            and idx.model() is self.asset_model
            and self.asset_model.data(idx, AssetListModel.ITEM_TYPE_ROLE) == "asset"
        ]
        # Only preview when exactly one asset is selected — marquee and
        # multi-select leave the Source Viewer untouched.
        if len(asset_indexes) != 1:
            return
        asset = asset_indexes[0].data(Qt.ItemDataRole.UserRole)
        # Duck-type rather than isinstance(): the asset object may come from a
        # different import path in some test contexts. ITEM_TYPE_ROLE already
        # guaranteed it's an asset row, so just verify it has an asset_id.
        asset_id = getattr(asset, "asset_id", None)
        if asset_id:
            self.asset_selected.emit(asset_id)

    def _on_grid_double_clicked(self, index: QModelIndex) -> None:
        """Handle grid view item double-click - asset: open in Source Viewer, comp_clip: open Composite, timeline: switch, bin: enter（V-09）"""
        if not self.asset_model or not index.isValid() or index.model() is not self.asset_model:
            return

        # 获取 item 类型
        item_type = self.asset_model.data(index, AssetListModel.ITEM_TYPE_ROLE)
        item_id = self.asset_model.data(index, AssetListModel.ITEM_ID_ROLE)

        if item_type == "bin" and item_id:
            # Bin 双击：进入该 bin（导航）
            logger.debug(f"Bin double-clicked in grid view: {item_id}")
            self._enter_bin(item_id)
        elif item_type == "timeline" and item_id:
            # Timeline 双击：发射切换信号（V-09, UI-29）
            logger.debug(f"Timeline double-clicked in grid view: {item_id}")
            self.timeline_switch_requested.emit(item_id)
        elif item_type == "asset":
            asset = self.asset_model.data(index, Qt.ItemDataRole.UserRole)
            if isinstance(asset, MediaAsset):
                if asset.is_comp_clip:
                    # Comp Clip 双击：打开 Composite 页面
                    logger.debug(f"Comp Clip double-clicked: {asset.asset_id}")
                    self.open_comp_clip_in_composite_requested.emit(asset.asset_id)
                elif asset.is_adjustment or asset.media_type == "solid":
                    # Generator clips (Adjustment / Solid) have no decodable
                    # source — don't open Source Viewer. They're meant to be
                    # dragged onto the timeline; double-click is a no-op.
                    logger.debug(
                        f"Generator asset double-clicked (no source viewer): {asset.asset_id}"
                    )
                else:
                    # Asset 双击：打开 Source Viewer
                    logger.debug(f"Asset double-clicked in grid view: {asset.asset_id}")
                    self.asset_double_clicked.emit(asset.asset_id)

    # ==================== Import Logic ====================

    def _on_import_clicked(self) -> None:
        """Handle Import button click - open file dialog."""
        # 获取顶层窗口作为父级
        parent_widget = self._get_top_level_widget()

        # 构建文件过滤器
        # NOTE: VIDEO/IMAGE/AUDIO_EXTENSIONS are frozenset (use | for union, not +)
        video_exts = " ".join(sorted(VIDEO_EXTENSIONS))
        image_exts = " ".join(sorted(IMAGE_EXTENSIONS))
        audio_exts = " ".join(sorted(AUDIO_EXTENSIONS))
        subtitle_exts = " ".join(sorted(SUBTITLE_EXTENSIONS))
        all_exts = " ".join(sorted(
            VIDEO_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS | SUBTITLE_EXTENSIONS
        ))

        video_filter = I18N.tr("edit.media_pool.filter_video", f"Video files ({video_exts})")
        image_filter = I18N.tr("edit.media_pool.filter_image", f"Image files ({image_exts})")
        audio_filter = I18N.tr("edit.media_pool.filter_audio", f"Audio files ({audio_exts})")
        subtitle_filter = I18N.tr(
            "edit.media_pool.filter_subtitle", f"Subtitle files ({subtitle_exts})"
        )
        all_filter = I18N.tr("edit.media_pool.filter_all", f"All media files ({all_exts})")

        filters = [all_filter, video_filter, image_filter, audio_filter, subtitle_filter]

        # 打开文件选择对话框
        files, _ = QFileDialog.getOpenFileNames(
            parent_widget,
            I18N.tr("edit.media_pool.select_files", "Select media files to import"),
            os.path.expanduser("~"),
            ";;".join(filters)
        )

        if files:
            self._import_files(files)

    def _on_import_folder_clicked(self) -> None:
        """Import a folder, mirroring its subfolder structure into bins."""
        parent_widget = self._get_top_level_widget()
        folder = QFileDialog.getExistingDirectory(
            parent_widget,
            I18N.tr("edit.media_pool.select_folder", "Select a folder to import"),
            os.path.expanduser("~"),
        )
        if not folder:
            return
        # Collect all media files under the folder (recursive).
        all_files = self._filter_valid_media_files([folder])
        if not all_files:
            return
        # Remember the folder root so we can rebuild bin structure after import.
        self._import_folder_root = os.path.abspath(folder)
        self._import_files(all_files)

    def _import_files(self, file_paths: List[str]) -> None:
        """
        Import multiple files to the project.

        Delegates to async import to avoid blocking the UI thread.
        Falls back to synchronous import for compatibility.

        Args:
            file_paths: List of file paths to import
        """
        if not self._project:
            # Auto-create project so the import can proceed
            self.auto_project_needed.emit()
            if not self._project:
                return
            # Project was created, proceed with import (no recursion)
            # Fall through to actual import below

        # 过滤有效文件
        valid_files = self._filter_valid_media_files(file_paths)

        if not valid_files:
            return

        # 去重：排除已导入的文件
        new_files = []
        skipped_files = []

        for path in valid_files:
            if self._project.has_asset_path(path):
                skipped_files.append(path)
            else:
                new_files.append(path)

        # 显示已跳过的提示
        if skipped_files:
            logger.info(f"Skipped {len(skipped_files)} already imported files")

        # 导入新文件 - delegate to async import
        if new_files:
            self._import_files_async(new_files)

    def _filter_valid_media_files(self, file_paths: List[str]) -> List[str]:
        """
        Filter valid media files from input paths.

        Directories are recursively expanded into their contained media files,
        so importing (or dragging) a folder imports its whole tree (Finder/
        Premiere behavior). The folder structure is mirrored into bins by the
        caller via ``_remember_folder_structure``.

        Args:
            file_paths: Input file paths

        Returns:
            Valid media file paths
        """
        valid: List[str] = []
        seen: set[str] = set()

        def _walk(path: str) -> None:
            normalized = os.path.abspath(os.path.expanduser(path))
            if normalized in seen:
                return
            seen.add(normalized)
            if os.path.isdir(normalized):
                try:
                    for entry in sorted(os.listdir(normalized)):
                        _walk(os.path.join(normalized, entry))
                except OSError as exc:
                    logger.warning(f"Cannot read folder {normalized}: {exc}")
                return
            if not os.path.isfile(normalized):
                logger.debug(f"Skipping non-file media import path: {normalized}")
                return
            if not os.access(normalized, os.R_OK):
                logger.warning(f"Skipping unreadable media import path: {normalized}")
                return
            ext = os.path.splitext(normalized)[1].lower()
            if (
                ext in VIDEO_EXTENSIONS
                or ext in IMAGE_EXTENSIONS
                or ext in AUDIO_EXTENSIONS
                or ext in SUBTITLE_EXTENSIONS
            ):
                valid.append(normalized)

        for path in file_paths:
            if path:
                _walk(path)
        return valid

    def _do_import_with_progress(self, file_paths: List[str]) -> None:
        """
        Import files with progress feedback.

        Args:
            file_paths: List of files to import
        """
        if len(file_paths) <= 3:
            # 少量文件直接导入，不显示进度
            self._do_import(file_paths)
            return

        # 创建进度对话框
        parent_widget = self._get_top_level_widget()
        self._import_progress = QProgressDialog(
            I18N.tr("edit.media_pool.importing", "Importing media files..."),
            I18N.tr("edit.media_pool.cancel", "Cancel"),
            0,
            len(file_paths),
            parent_widget
        )
        self._import_progress.setWindowTitle(I18N.tr("edit.media_pool.import_title", "Import Progress"))
        self._import_progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._import_progress.setMinimumDuration(0)
        # 与 _import_files_async 相同：禁用 autoClose/autoReset，
        # 由下方完成逻辑显式 close + deleteLater。
        self._import_progress.setAutoClose(False)
        self._import_progress.setAutoReset(False)
        self._import_progress.setValue(0)

        # 导入文件
        imported_count = 0
        cancelled = False

        for i, path in enumerate(file_paths):
            if self._import_progress.wasCanceled():
                cancelled = True
                break

            self._import_progress.setLabelText(
                I18N.tr("edit.media_pool.importing_file", "Importing: {file}",
                        file=os.path.basename(path))
            )
            self._import_progress.setValue(i)

            # 执行导入
            if self._import_single_file(path):
                imported_count += 1

        # 完成进度（显式 close 后再延迟删除，见 _finish_import_batch 注释）
        self._import_progress.setValue(len(file_paths))
        self._import_progress.close()
        self._import_progress.deleteLater()
        self._import_progress = None

        # 显示结果
        if not cancelled and imported_count > 0:
            self._refresh_tree(refresh_bins=True)
            QMessageBox.information(
                parent_widget,
                I18N.tr("edit.media_pool.import_complete", "Import Complete"),
                I18N.tr("edit.media_pool.import_success", "Successfully imported {count} files.",
                        count=imported_count)
            )

    def _do_import(self, file_paths: List[str]) -> None:
        """
        Import files without progress dialog.

        Args:
            file_paths: List of files to import
        """
        imported_count = 0
        for path in file_paths:
            if self._import_single_file(path):
                imported_count += 1

        if imported_count > 0:
            self._refresh_tree(refresh_bins=True)
            logger.info(f"Imported {imported_count} files")

    def _import_single_file(self, path: str) -> bool:
        """
        Import a single file to the project.

        Args:
            path: File path to import

        Returns:
            True if successful
        """
        if not self._project:
            return False

        try:
            # 探测元数据
            metadata = probe_media_metadata(path)

            # 获取媒体类型（R12：probe 修正——纯音频容器按 audio）
            media_type = media_type_from_probe(path, metadata)

            # 调用 EditProject.import_asset()
            # 注意：EditProject 是 frozen dataclass，import_asset 返回新实例
            new_project = self._project.import_asset(
                path=path,
                media_type=media_type,
                metadata=metadata,
                color_metadata=_extract_color_metadata(metadata),
            )

            # 检查是否真正导入（去重时返回原实例）
            # 注意：EditProject.__eq__ 只比较 project_id，无法区分内容变化
            # 使用 is 比较对象身份：去重时返回 self，成功导入时返回新实例
            if new_project is not self._project:
                # 更新项目引用
                self._project = new_project
                # 发射信号
                asset = new_project.get_asset_by_path(path)
                if asset:
                    self.asset_imported.emit(asset.asset_id, metadata)
                    self.project_changed.emit(new_project)
                    logger.debug(f"Imported asset: {path}, id={asset.asset_id}")
                    return True
            else:
                logger.debug(f"Asset already exists: {path}")

        except Exception as e:
            logger.error(f"Failed to import file: {path}, error={e}")

        return False

    # ==================== Drag and Drop ====================

    def _drag_offers_media(self, file_paths: List[str]) -> bool:
        """Shallow drag sniff (AUD-20206): can this drop possibly carry media?

        Only inspects top-level entries: a file with a known media extension
        accepts immediately; a directory accepts optimistically (its content
        is fully expanded off the UI thread by the async import worker after
        the drop). This deliberately avoids the recursive ``_walk`` that used
        to freeze the UI during dragEnter/dragMove on large/network folders.
        """
        for path in file_paths:
            if not path:
                continue
            ext = os.path.splitext(path)[1].lower()
            if (
                ext in VIDEO_EXTENSIONS
                or ext in IMAGE_EXTENSIONS
                or ext in AUDIO_EXTENSIONS
                or ext in SUBTITLE_EXTENSIONS
            ):
                return True
            if os.path.isdir(path):
                return True
        return False

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        """Handle drag enter event - accept file drops."""
        if event.mimeData().hasUrls():
            # 检查是否有有效媒体文件（浅层嗅探，见 _drag_offers_media）
            file_paths = [
                url.toLocalFile()
                for url in event.mimeData().urls()
                if url.isLocalFile()
            ]
            if self._drag_offers_media(file_paths):
                event.acceptProposedAction()
            else:
                event.ignore()
        else:
            event.ignore()

    def dropEvent(
        self,
        event: QDropEvent,
        target_bin_id: Optional[str] = None,
    ) -> None:
        """Handle drop event - import dropped files via async worker.

        ``target_bin_id`` lets a forwarding child view indicate which bin the
        user dropped onto (so files land inside that bin instead of the root).
        When omitted (dropped on empty space), the import falls back to the bin
        the user is currently browsing — so dragging files into an open bin
        window lands them inside it (Finder behavior). At the project root
        level, files go to the root bin as before.
        """
        if event.mimeData().hasUrls():
            file_paths = [
                url.toLocalFile()
                for url in event.mimeData().urls()
                if url.isLocalFile()
            ]
            # No explicit drop target (empty space): use the bin currently
            # being browsed, if any. "" / None means root level → root bin.
            if target_bin_id is None:
                target_bin_id = self._current_bin_id or None
            event.acceptProposedAction()
            self._import_files_async(file_paths, target_bin_id=target_bin_id)
        else:
            event.ignore()

    @staticmethod
    def _single_dropped_folder(file_paths: List[str]) -> Optional[str]:
        """If ``file_paths`` is exactly one directory, return its absolute path.

        Used to detect a Finder folder drop so we can mirror its structure
        into bins. Returns None for multi-item drops or file-only drops.
        """
        if len(file_paths) != 1:
            return None
        path = file_paths[0]
        if not path:
            return None
        path = os.path.abspath(os.path.expanduser(path))
        if os.path.isdir(path):
            return path
        return None

    def _import_files_async(
        self,
        file_paths: List[str],
        target_bin_id: Optional[str] = None,
        folder_root: Optional[str] = None,
    ) -> None:
        """
        Start async import of files.

        Metadata probing runs in a worker thread. Results are applied
        on the UI thread as a single batch to avoid project_changed storms.

        Args:
            file_paths: Files (and/or directories) to import. Directories are
                expanded recursively into their media files by
                ``_filter_valid_media_files``.
            target_bin_id: When set, imported assets land in this bin instead
                of the project root. ``folder_root`` (if any) takes precedence
                for structuring — the bin tree is mirrored *under* this bin.
            folder_root: Absolute path of a dropped/imported folder whose
                subfolder structure should be mirrored into bins. Detected
                automatically when a single directory is dropped.
        """
        if not self._project:
            self.auto_project_needed.emit()
            if not self._project:
                return

        # Auto-detect a single-folder drop so its structure is mirrored. Don't
        # clobber a folder_root already set by the "Import Folder" button (it
        # pre-stages _import_folder_root before calling the sync entry point).
        if folder_root is None:
            folder_root = self._single_dropped_folder(file_paths)
        if folder_root is None:
            folder_root = self._import_folder_root  # may be pre-staged or None

        # Filter and deduplicate
        valid_files = self._filter_valid_media_files(file_paths)
        new_files = [p for p in valid_files if not self._project.has_asset_path(p)]

        if not new_files:
            return

        # Guard against concurrent imports
        if getattr(self, '_import_in_progress', False):
            logger.warning("Import already in progress, queuing files")
            self._pending_import_files.extend(new_files)
            return

        self._import_in_progress = True
        import uuid
        batch_id = str(uuid.uuid4())
        project_id = self._project.project_id
        self._active_import_batch_id = batch_id
        self._active_import_project_id = project_id
        # Persist target bin + folder root for this batch so the batch-finished
        # handler knows where assets should end up.
        self._active_import_target_bin_id = target_bin_id
        self._import_folder_root = folder_root

        # Show progress for batch imports
        if len(new_files) > 3:
            parent_widget = self._get_top_level_widget()
            self._import_progress = QProgressDialog(
                I18N.tr("edit.media_pool.importing", "Importing media files..."),
                I18N.tr("edit.media_pool.cancel", "Cancel"),
                0,
                len(new_files),
                parent_widget
            )
            self._import_progress.setWindowTitle(
                I18N.tr("edit.media_pool.import_title", "Import Progress")
            )
            self._import_progress.setWindowModality(Qt.WindowModality.WindowModal)
            self._import_progress.setMinimumDuration(0)
            # 关闭 autoClose/autoReset：值到达 maximum 时对话框会经历
            # close -> reset -> 重新 show，紧接着 _finish_import_batch 又对它
            # setValue + deleteLater。这个"关闭/重开/再删除"序列与仍在派发中的
            # 排队进度信号竞争，在 Qt6Core 里触发 fail-fast (0xc0000409)。
            # 改为完全由 _finish_import_batch 显式收尾，生命周期可预测。
            self._import_progress.setAutoClose(False)
            self._import_progress.setAutoReset(False)
            self._import_progress.setValue(0)
            self._import_progress.canceled.connect(
                lambda bid=batch_id: self._cancel_import_batch(bid)
            )

        # Store results for batch apply
        self._import_total_files = len(new_files)
        self._import_cancelled = False

        worker = MediaImportWorker(batch_id, project_id, new_files)
        worker.signals.progress.connect(self._on_import_progress)

        def _deliver_finished(bid: str, pid: str, res: object) -> None:
            # 延迟一拍投递：批量收尾（含对话框销毁）绝不能在
            # _on_import_progress -> setValue 的嵌套事件派发里重入执行，
            # 否则外层帧会继续使用已销毁的对话框（见 _on_import_progress）。
            QTimer.singleShot(
                0, lambda: self._on_import_batch_finished(bid, pid, res)
            )

        worker.signals.finished.connect(_deliver_finished)
        self._active_import_worker = worker
        QThreadPool.globalInstance().start(worker)

    def _cancel_import_batch(self, batch_id: str) -> None:
        """Cancel the active import batch between file probes."""
        if batch_id != self._active_import_batch_id:
            return
        self._import_cancelled = True
        if self._active_import_worker is not None:
            self._active_import_worker.cancel()
        logger.info(f"Media import batch cancelled: {batch_id}")

    def _on_import_progress(self, batch_id: str, completed: int) -> None:
        """Update import progress on the UI thread."""
        if batch_id != self._active_import_batch_id:
            return
        dialog = self._import_progress
        if dialog is None:
            return
        # QProgressDialog.setValue() 会在原生层派发排队事件，本批次的
        # finished -> _finish_import_batch 可能借此重入并销毁对话框 ——
        # 而 setValue 自身返回时仍会解引用已删除的 this。用重入锁把
        # 销毁挡到本帧返回之后（见 _finish_import_batch 的延迟逻辑）。
        self._progress_update_in_flight = True
        try:
            dialog.setValue(completed)
            if self._import_progress is dialog:
                dialog.setLabelText(
                    I18N.tr(
                        "edit.media_pool.importing_progress",
                        "Importing media files... ({done}/{total})",
                        done=completed,
                        total=getattr(self, "_import_total_files", completed),
                    )
                )
        finally:
            self._progress_update_in_flight = False
            if getattr(self, "_finish_import_deferred", False):
                self._finish_import_deferred = False
                # 栈已展开完毕，此时再延迟一拍执行收尾是安全的。
                QTimer.singleShot(0, self._finish_import_batch)

    def _apply_folder_structure(
        self,
        results: list,
        base_bin_id: Optional[str] = None,
    ) -> None:
        """Mirror the imported folder tree into bins.

        For each successfully imported file, compute its path relative to the
        import root. Each subfolder becomes a bin (nested by path); files at
        the root stay in the implicit root. Existing bins with the same name
        are reused so re-importing is idempotent.

        Args:
            results: Import worker results ``[(path, media_type, metadata, error), ...]``.
            base_bin_id: When set, the mirrored bin tree is created *under* this
                bin (instead of the project root). Files that live directly in
                the dropped folder root are moved into ``base_bin_id`` itself.
        """
        if not self._project or not self._import_folder_root:
            return
        root = self._import_folder_root
        root_bin_id = base_bin_id or self._project.root_bin_id
        # Map "parent_bin_id/subfolder_name" -> bin_id (memoised per import).
        bin_cache: dict[str, str] = {}

        def _ensure_bin(rel_dir: str) -> str:
            """Return the bin id for a relative directory path, creating bins."""
            if not rel_dir or rel_dir == ".":
                return root_bin_id
            if rel_dir in bin_cache:
                return bin_cache[rel_dir]
            parts = rel_dir.split(os.sep)
            parent_id = root_bin_id
            accumulated = ""
            for part in parts:
                accumulated = os.path.join(accumulated, part) if accumulated else part
                if accumulated in bin_cache:
                    parent_id = bin_cache[accumulated]
                    continue
                # Find an existing child bin with this name under parent_id.
                existing_id = None
                for bid, b in self._project.bins.items():
                    if b.parent_id == parent_id and b.name == part:
                        existing_id = bid
                        break
                if existing_id is not None:
                    parent_id = existing_id
                    bin_cache[accumulated] = existing_id
                    continue
                # Create a new bin.
                cmd = CreateBinCommand.create(part, parent_id)
                self._project = self._command_stack.execute(cmd, self._project)
                new_id = cmd._created_bin_id
                bin_cache[accumulated] = new_id
                parent_id = new_id
            bin_cache[rel_dir] = parent_id
            return parent_id

        for path, media_type, metadata, error in results:
            if error or media_type is None or metadata is None:
                continue
            try:
                rel = os.path.relpath(path, root)
            except ValueError:
                continue
            rel_dir = os.path.dirname(rel)
            # When dropped onto a specific bin, even root-level files go into
            # that bin (rel_dir == "." resolves to base_bin_id via _ensure_bin).
            # When dropped at the project root, root-level files stay put.
            if not base_bin_id and (not rel_dir or rel_dir == "."):
                continue  # Root-level files stay in root.
            target_bin_id = _ensure_bin(rel_dir)
            asset = self._project.get_asset_by_path(path)
            if asset is None:
                continue
            if asset.asset_id in self._project.bins.get(target_bin_id, self._project.bins[self._project.root_bin_id]).asset_ids:
                continue
            cmd = MoveAssetToBinCommand.create(asset.asset_id, target_bin_id)
            self._project = self._command_stack.execute(cmd, self._project)

    def _on_import_batch_finished(
        self,
        batch_id: str,
        project_id: str,
        results: object,
    ) -> None:
        """Apply batched import results on the UI thread."""
        if batch_id != self._active_import_batch_id:
            logger.info(f"Ignoring stale media import batch: {batch_id}")
            return
        if self._project is None or self._project.project_id != project_id:
            logger.info(f"Dropping media import results for inactive project: {project_id}")
            self._finish_import_batch()
            return
        if getattr(self, "_import_cancelled", False):
            logger.info(f"Dropping cancelled media import batch: {batch_id}")
            self._finish_import_batch()
            return
        if not isinstance(results, list):
            logger.warning("Media import worker returned invalid results")
            self._finish_import_batch()
            return

        if not results:
            self._finish_import_batch()
            return

        imported_count = 0
        errors: List[tuple[str, str]] = []
        imported_assets: List[MediaAsset] = []
        project = self._project

        # M04.4：通用序列分类——连号静帧合成单一 image_sequence 素材
        # （此前逐张独立 image，媒体池冗余）；不成组路径走既有逐张导入。
        probed_ok = [(path, media_type, metadata)
                     for path, media_type, metadata, error in results
                     if not error and media_type is not None
                     and metadata is not None]
        sequence_paths = {p for p, mt, _ in probed_ok if mt == "image"}
        groups = []
        if len(sequence_paths) >= 3:
            from src.features.edit.core.sequence_classify import (
                classify_import_batch,
            )
            groups, _singles = classify_import_batch(
                sorted(sequence_paths))
        grouped_frames = {fp for g in groups for fp in g.frame_paths}

        for path, media_type, metadata, error in results:
            if error or media_type is None or metadata is None:
                errors.append((path, error or 'probe_failed'))
                continue
            if path in grouped_frames:
                continue  # 序列帧：经组单资产导入（下方循环）

            try:
                new_project = project.import_asset(
                    path=path,
                    media_type=media_type,
                    metadata=metadata,
                    color_metadata=_extract_color_metadata(metadata),
                )
                if new_project is not project:
                    project = new_project
                    imported_count += 1
                    imported_asset = project.get_asset_by_path(path)
                    if imported_asset is not None:
                        imported_assets.append(imported_asset)
            except Exception as e:
                errors.append((path, str(e)))
                logger.error(f"Failed to import: {path}, error={e}")

        # 序列组：每组一个 image_sequence 素材（metadata 携带帧清单/缺口）
        for group in groups:
            try:
                seq_metadata = dict(group.asset_metadata())
                # 首帧探测 metadata 保留（分辨率/色彩域参考）
                first_meta = next(
                    (m for p, _mt, m in probed_ok if p == group.first_path),
                    {})
                for key in ("width", "height", "fps"):
                    if key in first_meta and key not in seq_metadata:
                        seq_metadata[key] = first_meta[key]
                new_project = project.import_asset(
                    path=group.first_path,
                    media_type="image_sequence",
                    metadata=seq_metadata,
                )
                if new_project is not project:
                    project = new_project
                    imported_count += 1
                    imported_asset = project.get_asset_by_path(
                        group.first_path)
                    if imported_asset is not None:
                        imported_assets.append(imported_asset)
            except Exception as e:
                errors.append((group.first_path, str(e)))
                logger.error(
                    "Failed to import still sequence: %s, error=%s",
                    group.first_path, e)

        # Update project reference once (not per file)
        if project is not self._project:
            self._project = project
            target_bin_id = self._active_import_target_bin_id
            # If this batch came from a folder import/drop, mirror the folder
            # tree into bins before emitting (so the structure shows up at once).
            # When dropped onto a specific bin, the tree is nested under it and
            # root-level files go into that bin.
            if self._import_folder_root:
                self._apply_folder_structure(results, base_bin_id=target_bin_id)
                self._import_folder_root = None
            elif target_bin_id and target_bin_id != self._project.root_bin_id:
                # Plain file drop onto a bin (no folder structure): move every
                # newly imported asset into that bin as references.
                for path, media_type, metadata, _err in results:
                    if media_type is None or metadata is None:
                        continue
                    asset = self._project.get_asset_by_path(path)
                    if asset is None:
                        continue
                    if asset.asset_id in self._project.bins.get(target_bin_id, self._project.bins[self._project.root_bin_id]).asset_ids:
                        continue
                    move_cmd = MoveAssetToBinCommand.create(asset.asset_id, target_bin_id)
                    self._project = self._command_stack.execute(move_cmd, self._project)
            # Emit project_changed only ONCE for the entire batch
            self.project_changed.emit(project)
            # A 4K-or-larger video gets a background 1080p ProRes proxy as
            # soon as it enters the media pool.  The playback hot path only
            # reads a ready proxy; it never starts a transcode itself.  This
            # gives multi-track ProRes timelines a stable full-HD source while
            # keeping the program monitor at its native 1080p resolution.
            for imported_asset in imported_assets:
                self._maybe_auto_ensure_disk_proxy(imported_asset)
            # Emit asset_imported for each successful import
            for path, media_type, metadata, _ in results:
                if metadata is not None:
                    asset = project.get_asset_by_path(path)
                    if asset:
                        self.asset_imported.emit(asset.asset_id, metadata)
            self._refresh_tree(refresh_bins=True)

        if imported_count > 0:
            logger.info(f"Batch import completed: {imported_count} files imported")
        if errors:
            logger.warning(f"Batch import had {len(errors)} errors")

        self._finish_import_batch()

        # Process any queued imports
        pending = list(self._pending_import_files)
        if pending:
            self._pending_import_files = []
            self._import_files_async(pending)

    def _finish_import_batch(self) -> None:
        """Reset import UI and worker state."""
        # 正在更新进度对话框时禁止收尾：setValue 的嵌套事件派发可能重入
        # 本函数，此时销毁对话框会让 setValue 返回到已删除的对象上。
        if getattr(self, "_progress_update_in_flight", False):
            self._finish_import_deferred = True
            return
        if self._import_progress is not None:
            self._import_progress.setValue(getattr(self, '_import_total_files', 0))
            # 先显式 close 再延迟删除：对一个仍处于 WindowModal 状态的可见
            # 对象直接 deleteLater，会在后续事件派发/布局期间命中已删除的
            # C++ 对象（Qt6Core fail-fast）。
            self._import_progress.close()
            self._import_progress.deleteLater()
            self._import_progress = None

        self._import_in_progress = False
        self._active_import_batch_id = None
        self._active_import_project_id = None
        self._active_import_worker = None
        self._import_cancelled = False
        self._active_import_target_bin_id = None
        self._import_folder_root = None

    # ==================== Tree Management ====================

    def _refresh_tree(self, refresh_bins: bool = False) -> None:
        """Refresh the tree widget to show current project assets.

        使用防抖机制：启动定时器，避免快速连续刷新（UI-33）。
        """
        self._refresh_bins_pending = self._refresh_bins_pending or refresh_bins
        # QTimer runs after the current UI mutation returns to the event loop,
        # so scheduling is safe even when set_project() holds its update lock.
        self._refresh_timer.start()

    def _delayed_refresh(self) -> None:
        """延迟刷新（防抖回调，UI-33）。

        When a Smart Bin is selected, shows filtered assets from the Smart Bin.
        When a regular bin is selected, shows assets from that bin.
        When no bin is selected, shows all assets from root bin.
        """
        if self._updating:
            self._refresh_timer.start()
            return

        self._updating = True
        try:
            selection_snapshot = self._capture_pool_selection()
            self.tree.clear()
            # AUD-20209: 树已重建/清空 → 索引失效，下次缩略图到达时懒重建。
            self._tree_index_dirty = True

            if not self._project:
                # 清空网格视图
                if self.asset_model:
                    self.asset_model.set_items([])
                self._sync_metadata_shelf()
                self._update_breadcrumb()
                if self._refresh_bins_pending:
                    self._refresh_bin_tree()
                    self._refresh_bins_pending = False
                return


            # P0-4：大数据池先确保异步查询已按当前状态提交（指纹变化
            # 才重发）；本轮渲染用上一次结果，结果到达后再刷新一次。
            self._ensure_pool_query()

            for pool_item in self._current_pool_items():
                if pool_item.item_type == "bin":
                    self._add_bin_item_direct(pool_item.data)
                elif pool_item.item_type == "asset":
                    self._add_asset_item_direct(pool_item.data)
                else:
                    self._add_timeline_item_direct(pool_item.data)
            self._populate_grid_view()

            if self._refresh_bins_pending:
                self._refresh_bin_tree()
                self._refresh_bins_pending = False
            self._restore_pool_selection(selection_snapshot)

        finally:
            self._updating = False

    def _get_current_assets(self) -> List[MediaAsset]:
        """Search scope affects presentation, not AI or command scope."""
        if self._project and self._search_text and self._search_all_bins:
            return list(self._project.assets.values())
        return self._get_current_bin_assets()

    def _get_current_bin_assets(self) -> List[MediaAsset]:
        """Read the selected bin independently of presentation filters."""
        if not self._project:
            return []

        # Smart Bin 选中
        if self._current_bin_is_smart and self._current_bin_id:
            return self._project.get_smart_bin_assets(self._current_bin_id)

        # 普通 Bin 选中
        if self._current_bin_id and not self._current_bin_is_smart:
            bin_obj = self._project.bins.get(self._current_bin_id)
            if bin_obj:
                assets: List[MediaAsset] = []
                for asset_id in bin_obj.asset_ids:
                    asset = self._project.assets.get(asset_id)
                    if asset:
                        assets.append(asset)
                return assets

        # Root 层级：只显示未归类素材（没有被任何用户 bin 引用的素材）。
        # 拖进文件夹后，素材从 root 视图消失，但引用语义不变。
        unfiled_ids = self._project.get_unfiled_asset_ids()
        assets: List[MediaAsset] = []
        for asset_id in unfiled_ids:
            asset = self._project.assets.get(asset_id)
            if asset:
                assets.append(asset)
        return assets

    def _get_current_bins(self) -> List[ProjectBin]:
        """Read visible folders without changing project membership semantics."""
        if self._project is None or self._current_bin_is_smart:
            return []
        parent_id = self._current_bin_id or self._project.root_bin_id
        child_bins = [b for bid, b in self._project.bins.items()
                      if bid != self._project.root_bin_id and
                      ((self._search_text and self._search_all_bins) or b.parent_id == parent_id
                       or (not self._current_bin_id and b.parent_id is None))]
        if self._search_text:
            child_bins = [b for b in child_bins if self._search_text.casefold() in b.name.casefold()
                          and self._search_filter_mode in (SearchFilterMode.FILENAME.value, SearchFilterMode.ALL_FIELDS.value)]
        return sorted(child_bins, key=lambda b: b.name.casefold(), reverse=self._sort_order == "desc")

    def _get_current_timelines(self) -> List[TimelineSequenceRef]:
        """Use the same timeline references in all three presentations."""
        if self._project is None or self._current_bin_is_smart:
            return []
        if self._search_text and self._search_all_bins:
            result = list(self._project.timelines.values())
        elif not self._current_bin_id:
            result = [self._project.timelines[tid] for tid in self._project.get_unfiled_timeline_ids()
                      if tid in self._project.timelines]
        else:
            bin_obj = self._project.bins.get(self._current_bin_id)
            result = [self._project.timelines[tid] for tid in (bin_obj.timeline_ids if bin_obj else ())
                      if tid in self._project.timelines]
        if self._search_text:
            result = [t for t in result if self._search_text.casefold() in t.name.casefold()
                      and self._search_filter_mode in (SearchFilterMode.FILENAME.value, SearchFilterMode.ALL_FIELDS.value)]
        return sorted(result, key=lambda t: t.name.casefold(), reverse=self._sort_order == "desc")

    def _is_at_root_level(self) -> bool:
        """当前是否在 root 层级（未进入任何 bin）。"""
        return not self._navigation_stack

    def _enter_bin(self, bin_id: str) -> None:
        """进入指定的 bin（在右侧视图中导航到下一层）。

        更新 ``_current_bin_id`` / ``_navigation_stack``，并同步左侧边栏的
        选中状态（如果可见）。最后触发一次刷新重建视图。

        Args:
            bin_id: 要进入的 bin ID
        """
        if not self._project:
            return
        if bin_id == self._current_bin_id:
            return  # Already viewing this bin
        if bin_id not in self._project.bins:
            logger.warning(f"_enter_bin: bin {bin_id} not found")
            return

        # Record where we came from so _navigate_up can go back.
        previous = self._current_bin_id or self._project.root_bin_id
        self._navigation_stack.append(previous)
        # Entering a new bin invalidates any "forward" history.
        self._navigation_forward_stack.clear()

        self._current_bin_id = bin_id
        self._current_bin_is_smart = False

        # Keep the sidebar selection in sync when it is visible.
        if self.bin_tree.isVisible():
            self.bin_tree.select_bin(bin_id, False)

        self._update_breadcrumb()
        self._refresh_tree()
        logger.debug(f"Entered bin: {bin_id} (depth={len(self._navigation_stack)})")

    def _navigate_up(self) -> None:
        """返回上一级 bin（撤销最近一次 _enter_bin）。"""
        if not self._navigation_stack or not self._project:
            return

        parent_bin_id = self._navigation_stack.pop()
        # Push the bin we're leaving onto the forward stack so Cmd+] can
        # re-enter it.
        leaving = self._current_bin_id or self._project.root_bin_id
        self._navigation_forward_stack.append(leaving)
        # Empty string means "root level" (parent was the project root).
        if parent_bin_id == self._project.root_bin_id:
            self._current_bin_id = ""
        else:
            self._current_bin_id = parent_bin_id
        self._current_bin_is_smart = False

        if self.bin_tree.isVisible() and self._project:
            self.bin_tree.select_bin(self._current_bin_id or self._project.root_bin_id, False)

        self._update_breadcrumb()
        self._refresh_tree()
        logger.debug(f"Navigated up to: {self._current_bin_id or 'root'}")

    def _navigate_forward(self) -> None:
        """Re-enter the bin most recently left via _navigate_up (Cmd+])."""
        if not self._navigation_forward_stack or not self._project:
            return
        next_bin_id = self._navigation_forward_stack.pop()
        if next_bin_id not in self._project.bins:
            # Bin may have been deleted.
            return
        # Mirror _enter_bin but without clearing the forward stack.
        previous = self._current_bin_id or self._project.root_bin_id
        self._navigation_stack.append(previous)
        self._current_bin_id = next_bin_id
        self._current_bin_is_smart = False
        if self.bin_tree.isVisible():
            self.bin_tree.select_bin(next_bin_id, False)
        self._update_breadcrumb()
        self._refresh_tree()
        logger.debug(f"Navigated forward to: {next_bin_id}")

    def _navigate_to_root(self) -> None:
        """Master remains available from every regular or Smart Bin."""
        if self._project is None:
            return
        self._on_bin_selected(self._project.root_bin_id, False)
        self.bin_tree.select_bin(self._project.root_bin_id, False)

    def _update_breadcrumb(self) -> None:
        """Keep Resolve's current-bin header visible even at Master."""
        while self.breadcrumb_path_layout.count():
            item = self.breadcrumb_path_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self.breadcrumb_widget.show()
        self.btn_breadcrumb_back.setVisible(True)
        self.btn_breadcrumb_back.setEnabled(bool(self._navigation_stack))
        master = QPushButton(I18N.tr("edit.media_pool.master", "Master"))
        master.clicked.connect(self._navigate_to_root)
        self.breadcrumb_path_layout.addWidget(master)
        if self._project is not None:
            if self._current_bin_is_smart:
                bins = list(EditProject.get_default_smart_bins()) + list(self._project.smart_bins.values())
                name = next((b.name for b in bins if b.bin_id == self._current_bin_id), "")
                self.breadcrumb_path_layout.addWidget(QLabel("›"))
                self.breadcrumb_path_layout.addWidget(QLabel(name))
            else:
                for depth, bin_id in enumerate(self._navigation_stack):
                    if bin_id == self._project.root_bin_id:
                        continue
                    bin_obj = self._project.bins.get(bin_id)
                    if bin_obj is not None:
                        self.breadcrumb_path_layout.addWidget(QLabel("›"))
                        button = QPushButton(bin_obj.name)
                        button.clicked.connect(lambda _checked=False, bid=bin_id: self._navigate_to_sidebar_bin(bid))
                        self.breadcrumb_path_layout.addWidget(button)
                current = self._project.bins.get(self._current_bin_id)
                if current is not None:
                    self.breadcrumb_path_layout.addWidget(QLabel("›"))
                    self.breadcrumb_path_layout.addWidget(QLabel(current.name))
        self.breadcrumb_path_layout.addStretch(1)

    def _navigate_to_depth(self, depth: int) -> None:
        """Pop the navigation stack until it has exactly ``depth`` entries.

        ``depth`` counts the number of _enter_bin calls to keep; 0 means go
        all the way back to root.
        """
        if depth < 0 or depth >= len(self._navigation_stack):
            return
        while len(self._navigation_stack) > depth:
            self._navigation_stack.pop()
        # Set current bin to the last remaining stack entry (or root)
        if self._navigation_stack:
            last = self._navigation_stack[-1]
            self._current_bin_id = "" if (self._project and last == self._project.root_bin_id) else last
        else:
            self._current_bin_id = ""
        self._current_bin_is_smart = False
        if self.bin_tree.isVisible() and self._project:
            self.bin_tree.select_bin(self._current_bin_id or self._project.root_bin_id, False)
        self._update_breadcrumb()
        self._refresh_tree()

    def _apply_search_filter(self, assets: List[MediaAsset]) -> List[MediaAsset]:
        """根据搜索文本和过滤模式过滤素材。防御性检查避免 None 报错。"""
        if not self._search_text:
            return assets

        search_lower = self._search_text.casefold()
        if self._search_filter_mode == SearchFilterMode.ALL_FIELDS.value:
            return [asset for asset in assets if search_lower in " ".join(str(value or "") for value in (
                asset.display_name, asset.path, asset.reel, asset.scene, asset.shot, asset.take,
                asset.codec, asset.resolution_str, asset.fps, asset.keywords, asset.comment, asset.color_label,
            )).casefold()]

        if self._search_filter_mode == SearchFilterMode.FILENAME.value:
            return [a for a in assets if search_lower in (a.display_name or "").lower()]

        elif self._search_filter_mode == SearchFilterMode.CODEC.value:
            # 防御性检查：codec 可能是 None
            return [a for a in assets if search_lower in (a.codec or "").lower()]

        elif self._search_filter_mode == SearchFilterMode.RESOLUTION.value:
            result: List[MediaAsset] = []
            for a in assets:
                # 防御性检查：resolution_str 可能是 None
                res_str = (a.resolution_str or "").lower()
                # 支持搜索 "1920x1080" 或 "1080" 等
                width_height = f"{a.width or 0}x{a.height or 0}"
                if search_lower in res_str or search_lower in width_height:
                    result.append(a)
            return result

        elif self._search_filter_mode == SearchFilterMode.FPS.value:
            result_fps: List[MediaAsset] = []
            for a in assets:
                # 防御性检查：fps 可能是 None
                if a.fps is not None:
                    fps_str = f"{a.fps:.2f}"
                    if search_lower in fps_str or search_lower in str(a.fps):
                        result_fps.append(a)
            return result_fps

        return assets

    def _add_bin_item_direct(self, bin_obj: ProjectBin) -> QTreeWidgetItem:
        """Add a bin item to the tree as a top-level entry.

        Folder names match the sidebar; counts stay in the tooltip.

        Args:
            bin_obj: ProjectBin to add

        Returns:
            Created tree item
        """
        item = QTreeWidgetItem(self.tree)

        # Display and inline-edit roles both carry the plain bin name.
        item.setData(0, Qt.ItemDataRole.DisplayRole, bin_obj.name)
        item.setData(0, Qt.ItemDataRole.EditRole, bin_obj.name)
        item.setIcon(0, icon_folder())

        # Tooltip with full bin info
        item.setToolTip(
            0,
            f"{bin_obj.name}\n{len(bin_obj.asset_ids)} asset(s)",
        )

        # Store bin identity + type for context menu / double-click dispatch
        item.setData(0, Qt.ItemDataRole.UserRole, bin_obj.bin_id)
        item.setData(0, Qt.ItemDataRole.UserRole + 1, "bin")

        # Bins are not draggable to the timeline (AssetTreeWidget enforces this
        # via item_type check in startDrag, but be explicit here too). They are
        # editable in-place (Finder-style rename via F2 / context menu).
        item.setFlags(
            (item.flags() | Qt.ItemFlag.ItemIsEditable)
            & ~Qt.ItemFlag.ItemIsDragEnabled
        )

        return item

    def _add_asset_item_direct(self, asset: MediaAsset) -> QTreeWidgetItem:
        """
        Add an asset item directly to the tree (as top-level item, not under a bin).

        Args:
            asset: MediaAsset to add

        Returns:
            Created tree item
        """
        item = QTreeWidgetItem(self.tree)

        # Import must stay cheap. In particular, it must not launch a full
        # ffmpeg proxy transcode that later competes with immediate timeline
        # playback. Proxy generation is an explicit media-pool action.

        # 类型图标 (使用 qtawesome 图标替代 emoji)
        type_icons = {
            "video": icon_video_track(),
            "image": icon_image(),
            "audio": icon_audio_track(),
        }
        type_icon = type_icons.get(asset.media_type)

        # Col 0: Name（设置图标）
        display_name = asset.display_name
        # UI-34: 移除 emoji 前缀，使用图标系统
        # offline 状态通过红色前景色区分
        # proxy 状态通过 tooltip 和图标变化区分
        item.setText(0, display_name)

        # 设置类型图标（offline/proxy 使用特殊图标）
        if asset.is_offline:
            # Offline 使用警告图标（红色）
            item.setIcon(0, icon_warning(color=get_color("danger")))
        elif asset.has_proxy:
            # Proxy 使用普通图标，但在 tooltip 中标识
            if type_icon:
                item.setIcon(0, type_icon)
        else:
            if type_icon:
                item.setIcon(0, type_icon)

        item.setData(0, Qt.ItemDataRole.UserRole, asset.asset_id)
        item.setData(0, Qt.ItemDataRole.UserRole + 1, "asset")

        # 启用拖拽功能（拖拽到时间线）+ 原地重命名（F2）
        item.setFlags(
            item.flags()
            | Qt.ItemFlag.ItemIsDragEnabled
            | Qt.ItemFlag.ItemIsEditable
        )

        # 设置 tooltip 显示状态信息
        tooltip_parts = [display_name]
        if asset.is_offline:
            tooltip_parts.append(f"[{I18N.tr('edit.media_pool.offline', 'OFFLINE')}]")
        if asset.has_proxy:
            tooltip_parts.append(f"[{I18N.tr('edit.media_pool.proxy', 'PROXY')}]")
        # M3: 分析状态片段（§17.2 首版 tooltip；开关 off/无记录时为空）
        analysis_seg = self._analysis_status_tooltip_segment(asset.asset_id)
        if analysis_seg:
            tooltip_parts.append(analysis_seg)
        item.setToolTip(0, " | ".join(tooltip_parts))

        # offline 素材设置红色前景色
        if asset.is_offline:
            for col in range(9):
                item.setForeground(col, QColor(get_color("danger")))

        # Col 1: Reel（卷号）
        item.setText(1, asset.reel or "-")

        # Col 2: Scene/Shot/Take
        item.setText(2, asset.production_info_str)

        # Col 3: Duration
        duration_str = "-"
        if asset.duration_seconds is not None:
            duration_str = self._format_duration_short(asset.duration_seconds)
        item.setText(3, duration_str)

        # Col 4: FPS
        fps_str = "-"
        if asset.fps is not None:
            fps_str = f"{asset.fps:.2f}"
        item.setText(4, fps_str)

        # Col 5: Resolution
        item.setText(5, asset.resolution_str)

        # Col 6: Codec
        item.setText(6, asset.codec_str)

        # Col 7: Audio
        item.setText(7, asset.audio_channels_str)

        # Col 8: Path
        path_display = asset.path
        if len(path_display) > 40:
            path_display = path_display[:20] + "..." + path_display[-20:]
        item.setText(8, asset.path)
        item.setToolTip(8, asset.path)

        # Scene-backed virtual assets render their own deterministic poster;
        # never send the topos:// URI to the file thumbnail decoder.
        if not asset.is_motion_graphic:
            self._thumb_provider.get_thumbnail(asset.path, 64)

        return item

    def _add_timeline_item_direct(self, timeline: TimelineSequenceRef) -> QTreeWidgetItem:
        """Present existing timeline actions consistently in List View."""
        item = QTreeWidgetItem(self.tree)
        item.setText(0, timeline.name)
        item.setIcon(0, pool_icon("timeline"))
        item.setData(0, Qt.ItemDataRole.UserRole, timeline.timeline_id)
        item.setData(0, Qt.ItemDataRole.UserRole + 1, "timeline")
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsDragEnabled)
        item.setText(4, self._format_timeline_fps(timeline.fps))
        item.setText(5, f"{timeline.width}×{timeline.height}")
        item.setToolTip(0, timeline.name)
        if timeline.timeline_id == self._current_timeline_id:
            item.setForeground(0, QColor(C.ORANGE))
        return item

    def _current_pool_items(self) -> List[GridItem]:
        """Use one ordering/filtering result for all media-pool presentations."""
        bins = self._get_current_bins()
        if self._large_pool_query_active():
            # P0-4：大数据池的过滤/排序由异步查询会话给出（latest-wins；
            # 结果未到达时素材区为空，bins/timelines 照常同步渲染）。
            assets = self._large_pool_ordered_assets()
        else:
            assets = self._apply_search_filter(self._get_current_assets())
        timelines = self._get_current_timelines()
        items = ([GridItem("bin", b.bin_id, b) for b in bins]
                 + [GridItem("asset", a.asset_id, a) for a in assets]
                 + [GridItem("timeline", t.timeline_id, t) for t in timelines])
        known: list[tuple[Any, GridItem]] = []
        unknown: list[GridItem] = []
        for item in items:
            value = self._pool_sort_value(item)
            if value is None:
                unknown.append(item)
            else:
                known.append((value, item))
        known.sort(key=lambda pair: (pair[0], pair[1].display_name.casefold()), reverse=self._sort_order == "desc")
        unknown.sort(key=lambda item: item.display_name.casefold())
        return [item for _, item in known] + unknown

    def _pool_sort_value(self, item: GridItem) -> Any:
        """Sort references by available UI fields; missing values stay last."""
        if self._sort_by == SORT_BY_NAME:
            return item.display_name.casefold()
        data = item.data
        if self._sort_by == SORT_BY_TYPE:
            return str(getattr(data, "media_type", item.item_type)).casefold()
        if self._sort_by == "resolution":
            width, height = getattr(data, "width", None), getattr(data, "height", None)
            if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
                return width * height
            return None
        field = {SORT_BY_DATE: "created_at", SORT_BY_DURATION: "duration_seconds", "audio": "audio_channels", "scene": "production_info_str"}.get(self._sort_by, self._sort_by)
        value = getattr(data, field, None)
        if self._sort_by in (SORT_BY_DATE, SORT_BY_DURATION, "fps", "audio"):
            return value if isinstance(value, (int, float)) and math.isfinite(value) else None
        return str(value).casefold() if value is not None and str(value).strip() else None

    # ==================== P0-4 大数据池异步查询 ====================

    def _large_pool_query_active(self) -> bool:
        """素材量达到阈值时启用异步查询路径（含 Smart Bin）。

        R10（媒体池复查）：Smart Bin 的规则求值进入同一异步查询
        （scope=smart_bin），大数据池切 Smart Bin 不再在 UI 线程同步
        全量求值；普通/Smart Bin 共用 latest-wins 会话与取消。
        """
        return (self._project is not None
                and len(self._project.assets) >= MEDIA_POOL_ASYNC_QUERY_THRESHOLD)

    def _pool_query_fingerprint(self) -> tuple:
        """影响查询结果的全部 UI 状态 + 工程身份/修订。"""
        return (
            self._search_text, self._search_filter_mode, self._search_all_bins,
            self._current_bin_id, self._sort_by, self._sort_order,
            self._project.project_id if self._project else "",
            self._project.revision if self._project else -1,
        )

    def _ensure_pool_query(self) -> None:
        """指纹变化或结果缺失时提交异步查询（防抖由 _refresh_timer 承担）。"""
        if not self._large_pool_query_active() or self._project is None:
            return
        fingerprint = self._pool_query_fingerprint()
        if (self._query_asset_ids is not None
                and fingerprint == self._last_query_fingerprint):
            return
        # R10：指纹变化即丢弃上一轮结果——加载期间素材区为空（不把
        # 旧结果当作新搜索/Bin 范围的操作目标；bins/timelines 照常）。
        self._query_asset_ids = None
        from src.features.edit.core.media_pool_query import (
            VALID_SORT_KEYS, MediaPoolQuery,
        )
        if self._search_text and self._search_all_bins:
            scope, bin_id = "all_bins", None
        elif self._current_bin_is_smart and self._current_bin_id:
            scope, bin_id = "smart_bin", self._current_bin_id
        elif self._current_bin_id:
            scope, bin_id = "bin", self._current_bin_id
        else:
            scope, bin_id = "unfiled", None
        sort_key = self._sort_by if self._sort_by in VALID_SORT_KEYS else "name"
        self._pending_query_fingerprint = fingerprint
        self._query_session.submit(self._project, MediaPoolQuery(
            text=self._search_text,
            field=self._search_filter_mode,
            scope=scope,
            bin_id=bin_id,
            sort_key=sort_key,
            sort_desc=self._sort_order == "desc",
        ))

    def _large_pool_ordered_assets(self) -> List[MediaAsset]:
        """把查询结果的稳定 asset_id 序映射回素材（缺项跳过）。"""
        if self._query_asset_ids is None or self._project is None:
            return []
        return [
            asset for aid in self._query_asset_ids
            if (asset := self._project.assets.get(aid)) is not None
        ]

    def _on_pool_query_results(self, result: Any) -> None:
        """应用最新查询结果并刷新（旧结果已被会话按代际丢弃）。"""
        if self._project is None:
            return
        if getattr(result, "project_id", "") != self._project.project_id:
            return
        # R10：面板侧 revision 复验（会话在发射时校验；排队送达期间
        # 工程可能又前进一步——晚到结果不得按新工程渲染）。
        if getattr(result, "revision", None) != self._project.revision:
            logger.info("Dropping pool query result (stale revision)")
            return
        if getattr(result, "cancelled", False):
            return
        self._query_asset_ids = tuple(result.asset_ids)
        self._last_query_fingerprint = self._pending_query_fingerprint
        self._delayed_refresh()

    def _on_pool_query_failed(self, query_id: str, error: str) -> None:
        logger.warning(f"Media pool query failed ({query_id}): {error}")
        # 查询失败时保留上一次结果，等待下一次刷新重试。

    def closeEvent(self, event: Any) -> None:
        """面板关闭：取消在途查询并等待查询线程退出（不留后台任务）。"""
        self._query_session.shutdown()
        # R04：面板关闭取消在场的收集/整理任务
        self._invalidate_media_management()
        super().closeEvent(event)

    def _populate_grid_view(self) -> None:
        """填充网格视图（包含 bins + assets + timelines，V-08, UI-28）"""
        if not self._project or not self.asset_model:
            return

        items = self._current_pool_items()
        assets = [item.data for item in items if item.item_type == "asset"]
        timelines = [item.data for item in items if item.item_type == "timeline"]
        bins = [item.data for item in items if item.item_type == "bin"]

        # 更新模型
        self.asset_model.set_items(items)
        self._sync_metadata_shelf()

        # Timeline cards use the first dropped media asset's persisted random
        # frame as their preview source. Requests are asynchronous and are
        # intentionally made only for timelines visible in the current bin.
        self._request_timeline_previews(timelines)

        # 触发缩略图加载（仅 assets）
        for asset in assets:
            if asset.is_motion_graphic:
                continue
            self._thumb_provider.get_thumbnail(
                self._thumbnail_path_for_asset(asset), self._thumb_width
            )

        logger.debug(
            f"Grid view populated with {len(bins)} bins + "
            f"{len(assets)} assets + "
            f"{len(timelines)} timelines"
        )

    def _request_timeline_previews(
        self, timelines: List[TimelineSequenceRef]
    ) -> None:
        """Request visible timeline preview frames without blocking the UI."""
        if self._project is None or self.asset_model is None:
            return

        for timeline in timelines:
            settings = dict(timeline.format_settings or {})
            asset_id = settings.get(MEDIA_POOL_PREVIEW_ASSET_ID_KEY)
            asset = (
                self._project.assets.get(asset_id)
                if isinstance(asset_id, str) and asset_id
                else None
            )
            # Timelines created from the New Timeline dialog may not have a
            # persisted preview source yet. Use the first available visual
            # asset as an immediate fallback so the card is never blank; a
            # later media drop still replaces this with its random frame.
            if asset is None:
                asset = next(
                    (
                        candidate
                        for candidate in self._project.assets.values()
                        if candidate.media_type in ("video", "image")
                        and not candidate.is_offline
                    ),
                    None,
                )
            if asset is None or asset.is_offline:
                continue

            preview_path = asset.effective_path
            if not preview_path:
                continue

            try:
                frame_index = max(
                    0, int(settings.get(MEDIA_POOL_PREVIEW_FRAME_KEY, 0))
                )
            except (TypeError, ValueError):
                frame_index = 0

            # Show the regular thumbnail immediately while the requested
            # random video frame is decoded asynchronously.
            cached = self._thumb_provider.get_cached_thumbnail(
                preview_path, self._thumb_width
            )
            if cached is not None:
                self.asset_model.set_timeline_preview(timeline.timeline_id, cached)
            else:
                self._pending_timeline_image_previews.setdefault(
                    preview_path, set()
                ).add(timeline.timeline_id)
                self._thumb_provider.get_thumbnail(preview_path, self._thumb_width)

            if asset.media_type == "image":
                continue

            if asset.media_type != "video":
                continue

            try:
                total_frames = int(asset.total_frames or 0)
            except (TypeError, ValueError):
                total_frames = 0
            if total_frames <= 0 and asset.duration_seconds and asset.fps:
                total_frames = max(1, int(round(asset.duration_seconds * asset.fps)))
            total_frames = max(1, total_frames)
            frame_index = min(frame_index, total_frames - 1)

            key = (preview_path, frame_index)
            self._pending_timeline_frame_previews.setdefault(key, set()).add(
                timeline.timeline_id
            )
            self._thumb_provider.get_frame_thumbnail(
                preview_path,
                frame_index,
                total_frames,
                icon_size=self._thumb_width,
                asset_id=asset.asset_id,
            )

    @staticmethod
    def _format_timeline_fps(fps: float) -> str:
        """Render a concise fps label without losing 1001-based rates."""
        return f"{fps:.3f}".rstrip("0").rstrip(".") + " fps"

    def _add_asset_item(self, parent_item: QTreeWidgetItem, asset: MediaAsset) -> QTreeWidgetItem:
        """
        Add an asset item to the tree.

        扩展列：Name | Reel | Scene/Shot/Take | Duration | FPS | Resolution | Codec | Audio | Path

        Args:
            parent_item: Parent tree item (bin)
            asset: MediaAsset to add

        Returns:
            Created tree item
        """
        item = QTreeWidgetItem(parent_item)

        # 类型图标 (使用 qtawesome 图标替代 emoji)
        type_icons = {
            "video": icon_video_track(),
            "image": icon_image(),
            "audio": icon_audio_track(),
        }
        type_icon = type_icons.get(asset.media_type)

        # Col 0: Name（设置图标）
        display_name = asset.display_name
        # UI-34: 移除 emoji 前缀，使用图标系统
        item.setText(0, display_name)

        # 设置类型图标（offline/proxy 使用特殊图标）
        if asset.is_offline:
            # Offline 使用警告图标（红色）
            item.setIcon(0, icon_warning(color=get_color("danger")))
        elif asset.has_proxy:
            # Proxy 使用普通图标，但在 tooltip 中标识
            if type_icon:
                item.setIcon(0, type_icon)
        else:
            if type_icon:
                item.setIcon(0, type_icon)

        item.setData(0, Qt.ItemDataRole.UserRole, asset.asset_id)
        item.setData(0, Qt.ItemDataRole.UserRole + 1, "asset")

        # 启用拖拽功能（拖拽到时间线）+ 原地重命名（F2）
        item.setFlags(
            item.flags()
            | Qt.ItemFlag.ItemIsDragEnabled
            | Qt.ItemFlag.ItemIsEditable
        )

        # 设置 tooltip 显示状态信息
        tooltip_parts = [display_name]
        if asset.is_offline:
            tooltip_parts.append(f"[{I18N.tr('edit.media_pool.offline', 'OFFLINE')}]")
        if asset.has_proxy:
            tooltip_parts.append(f"[{I18N.tr('edit.media_pool.proxy', 'PROXY')}]")
        # M3: 分析状态片段（§17.2 首版 tooltip；开关 off/无记录时为空）
        analysis_seg = self._analysis_status_tooltip_segment(asset.asset_id)
        if analysis_seg:
            tooltip_parts.append(analysis_seg)
        item.setToolTip(0, " | ".join(tooltip_parts))

        # offline 素材设置红色前景色
        if asset.is_offline:
            item.setForeground(0, QColor(get_color("danger")))
            item.setForeground(1, QColor(get_color("danger")))
            item.setForeground(2, QColor(get_color("danger")))
            item.setForeground(3, QColor(get_color("danger")))
            item.setForeground(4, QColor(get_color("danger")))
            item.setForeground(5, QColor(get_color("danger")))
            item.setForeground(6, QColor(get_color("danger")))
            item.setForeground(7, QColor(get_color("danger")))
            item.setForeground(8, QColor(get_color("danger")))

        # Col 1: Reel（卷号）
        item.setText(1, asset.reel or "-")

        # Col 2: Scene/Shot/Take
        item.setText(2, asset.production_info_str)

        # Col 3: Duration
        duration_str = "-"
        if asset.duration_seconds is not None:
            duration_str = self._format_duration_short(asset.duration_seconds)
        item.setText(3, duration_str)

        # Col 4: FPS
        fps_str = "-"
        if asset.fps is not None:
            fps_str = f"{asset.fps:.2f}"
        item.setText(4, fps_str)

        # Col 5: Resolution
        item.setText(5, asset.resolution_str)

        # Col 6: Codec
        item.setText(6, asset.codec_str)

        # Col 7: Audio
        item.setText(7, asset.audio_channels_str)

        # Col 8: Path
        # 显示相对路径（缩短显示）
        path_display = asset.path
        if len(path_display) > 40:
            # 截断中间部分
            path_display = path_display[:20] + "..." + path_display[-20:]
        item.setText(8, asset.path)
        item.setToolTip(8, asset.path)

        # Scene-backed virtual assets render their own deterministic poster;
        # never send the topos:// URI to the file thumbnail decoder.
        if not asset.is_motion_graphic:
            self._thumb_provider.get_thumbnail(asset.path, 64)

        return item

    def _format_duration_short(self, seconds: float) -> str:
        """Format duration for display in tree."""
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        if minutes > 0:
            return f"{minutes}:{secs:02d}"
        else:
            return f"{secs}s"

    def _update_status_label(self) -> None:
        """Update the status label text and offline warning indicator."""
        if self._project:
            asset_count = self._project.asset_count
            timeline_count = self._project.timeline_count
            offline_count = sum(1 for a in self._project.assets.values() if a.is_offline)
            proxy_count = sum(1 for a in self._project.assets.values() if a.has_proxy)

            status_text = I18N.tr(
                "edit.media_pool.status",
                "Assets: {assets} | Timelines: {timelines}",
                assets=asset_count, timelines=timeline_count
            )

            if offline_count > 0:
                status_text += I18N.tr(
                    "edit.media_pool.status_offline",
                    " | Offline: {count}",
                    count=offline_count
                )
                # 显示离线警告按钮
                self.btn_offline_warning.show()
                self.btn_offline_warning.setToolTip(
                    I18N.tr(
                        "edit.media_pool.offline_warning_tooltip",
                        "{count} Offline Assets - Click to Relink",
                        count=offline_count
                    )
                )
            else:
                # 隐藏离线警告按钮
                self.btn_offline_warning.hide()

            if proxy_count > 0:
                status_text += I18N.tr(
                    "edit.media_pool.status_proxy",
                    " | Proxy: {count}",
                    count=proxy_count
                )

            self.status_label.setText(status_text)
        else:
            self.status_label.setText(I18N.tr("edit.media_pool.no_project", "No project loaded"))
            self.btn_offline_warning.hide()

    # ==================== Selection Handling ====================

    def _on_selection_changed(self) -> None:
        """Handle tree selection change.

        Only previews when exactly one asset is selected; multi-selection
        leaves the Source Viewer untouched (consistent with grid view).
        """
        if self._updating:
            return

        selected_items = self.tree.selectedItems()
        if not selected_items:
            return
        timeline_items = [
            it for it in selected_items
            if it.data(0, Qt.ItemDataRole.UserRole + 1) == "timeline"
        ]
        if len(timeline_items) == 1:
            timeline_id = timeline_items[0].data(0, Qt.ItemDataRole.UserRole)
            if timeline_id:
                self.timeline_selected.emit(timeline_id)
            return

        # Count selected assets — preview only on a single-asset selection.
        asset_items = [
            it for it in selected_items
            if it.data(0, Qt.ItemDataRole.UserRole + 1) == "asset"
        ]
        if len(self.tree.selectedItems()) != 1 or len(asset_items) != 1:
            return
        item = asset_items[0]
        asset_id = item.data(0, Qt.ItemDataRole.UserRole)
        if asset_id:
            self.asset_selected.emit(asset_id)

    def _selected_pool_item_key(self) -> Optional[tuple[str, str]]:
        """Capture the current asset/timeline selection before a model reset."""
        if self._view_mode == ViewMode.LIST:
            selected_items = self.tree.selectedItems()
            if selected_items:
                item = selected_items[0]
                item_type = item.data(0, Qt.ItemDataRole.UserRole + 1)
                item_id = item.data(0, Qt.ItemDataRole.UserRole)
                if item_type in ("asset", "timeline") and item_id:
                    return (item_type, item_id)
            return None

        if self.grid_view and self.asset_model:
            indexes = self.grid_view.selectionModel().selectedIndexes()
            if indexes:
                index = indexes[0]
                item_type = self.asset_model.data(index, AssetListModel.ITEM_TYPE_ROLE)
                item_id = self.asset_model.data(index, AssetListModel.ITEM_ID_ROLE)
                if item_type in ("asset", "timeline") and item_id:
                    return (item_type, item_id)
        return None

    def _restore_pool_item_selection(self, selected_key: Optional[tuple[str, str]]) -> None:
        """Restore an asset/timeline selection after rebuilding the content view."""
        if selected_key is None:
            return
        item_type, item_id = selected_key

        if self._view_mode == ViewMode.LIST:
            target: Optional[QTreeWidgetItem] = None
            for top_index in range(self.tree.topLevelItemCount()):
                top_level_item = self.tree.topLevelItem(top_index)
                if top_level_item is None:
                    continue
                candidates = [top_level_item] + [
                    top_level_item.child(child_index)
                    for child_index in range(top_level_item.childCount())
                ]
                for candidate in candidates:
                    if (
                        candidate is not None
                        and candidate.data(0, Qt.ItemDataRole.UserRole + 1) == item_type
                        and candidate.data(0, Qt.ItemDataRole.UserRole) == item_id
                    ):
                        target = candidate
                        break
                if target is not None:
                    break
            if target is not None:
                previous = self.tree.blockSignals(True)
                try:
                    self.tree.setCurrentItem(target)
                finally:
                    self.tree.blockSignals(previous)
            return

        if self.grid_view and self.asset_model:
            selection_model = self.grid_view.selectionModel()
            for row in range(self.asset_model.rowCount()):
                index = self.asset_model.index(row)
                if (
                    self.asset_model.data(index, AssetListModel.ITEM_TYPE_ROLE) == item_type
                    and self.asset_model.data(index, AssetListModel.ITEM_ID_ROLE) == item_id
                ):
                    selection_model.select(
                        index,
                        QItemSelectionModel.SelectionFlag.ClearAndSelect,
                    )
                    self.grid_view.setCurrentIndex(index)
                    break

    def _on_tree_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        """Handle tree item double-click.

        - For comp_clip asset: open in Composite page
        - For asset: open in Source Viewer
        - For timeline: switch to this timeline in TimelinePanel
        - For bin: enter the bin (navigate into it)
        """
        item_type = item.data(0, Qt.ItemDataRole.UserRole + 1)
        item_id = item.data(0, Qt.ItemDataRole.UserRole)

        if item_type == "bin" and item_id:
            logger.debug(f"Bin double-clicked (tree): {item_id}")
            self._enter_bin(item_id)
        elif item_type == "asset" and item_id:
            # Check if this is a comp_clip
            if self._project:
                asset = self._project.get_asset_by_id(item_id)
                if asset and asset.is_comp_clip:
                    logger.debug(f"Comp Clip double-clicked (tree): {item_id}")
                    self.open_comp_clip_in_composite_requested.emit(item_id)
                    return
            logger.debug(f"Asset double-clicked: {item_id}")
            self.asset_double_clicked.emit(item_id)
        elif item_type == "timeline" and item_id:
            logger.debug(f"Timeline double-clicked: {item_id}")
            self.timeline_switch_requested.emit(item_id)

    # ==================== Context Menu ====================

    def _append_batch_delete_action(
        self, menu: QMenu, selected_asset_ids: List[str]
    ) -> None:
        """Add a context-menu action for deleting the current asset selection."""
        if len(selected_asset_ids) < 2:
            return

        items = I18N.tr(
            "edit.media_pool.delete_selected_assets",
            "{count} asset(s)",
            count=len(selected_asset_ids),
        )
        delete_selected_act = menu.addAction(
            I18N.tr(
                "edit.media_pool.delete_selected",
                "Delete {items}",
                items=items,
            )
        )
        delete_selected_act.triggered.connect(
            lambda _checked=False, ids=tuple(selected_asset_ids): self._delete_selected_pool_items(
                selected_asset_ids=list(ids)
            )
        )
        menu.addSeparator()

    def _select_single_context_tree_item(self, item: QTreeWidgetItem) -> None:
        """Select the context-menu target, preserving an intentional group.

        Qt views commonly keep an existing extended selection when the user
        opens a context menu with the right mouse button.  Keep the group when
        the target is already selected so batch actions can act on it; an
        unselected target becomes the sole selection.
        """
        if item is None:
            return
        # Preserve an existing extended selection when the user right-clicks
        # one of its members.  This is what makes a following batch action
        # operate on the whole selection.  A right-click on an unselected item
        # still establishes that item as the sole target.
        if item.isSelected():
            selection_model = self.tree.selectionModel()
            item_index = self.tree.indexFromItem(item, 0)
            if selection_model is not None and item_index.isValid():
                # QTreeWidget.setCurrentItem() can collapse an extended
                # selection; update only the current index instead.
                selection_model.setCurrentIndex(
                    item_index, QItemSelectionModel.SelectionFlag.NoUpdate
                )
            else:
                self.tree.setCurrentItem(item)
            return
        self.tree.clearSelection()
        item.setSelected(True)
        self.tree.setCurrentItem(item)

    def _select_single_context_grid_item(self, index: QModelIndex) -> None:
        """Select the context-menu card, preserving an intentional group."""
        if (
            self.grid_view is None
            or self.asset_model is None
            or not index.isValid()
            or index.model() is not self.asset_model
        ):
            return
        selection_model = self.grid_view.selectionModel()
        if selection_model is None:
            return
        if selection_model.isSelected(index):
            # QListView.setCurrentIndex() may update the selection when the
            # view is in ExtendedSelection mode.  Set only the current index
            # so an intentional multi-selection survives the context menu.
            selection_model.setCurrentIndex(
                index, QItemSelectionModel.SelectionFlag.NoUpdate
            )
            return
        selection_model.select(
            index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect,
        )
        selection_model.setCurrentIndex(
            index, QItemSelectionModel.SelectionFlag.NoUpdate
        )

    def _restore_grid_context_selection(
        self, asset_ids: List[str], target: QModelIndex
    ) -> None:
        """Restore a grid selection captured before a context-menu press."""
        if self.grid_view is None or self.asset_model is None:
            return
        selection_model = self.grid_view.selectionModel()
        if selection_model is None:
            return

        selection = QItemSelection()
        for asset_id in asset_ids:
            row = self.asset_model.row_for_asset(asset_id)
            if row is None:
                continue
            index = self.asset_model.index(row, 0)
            if index.isValid():
                selection.select(index, index)
        if not selection.isEmpty():
            selection_model.select(
                selection,
                QItemSelectionModel.SelectionFlag.ClearAndSelect,
            )
            selection_model.setCurrentIndex(
                target, QItemSelectionModel.SelectionFlag.NoUpdate
            )

    def _restore_tree_context_selection(
        self, asset_ids: List[str], target: QTreeWidgetItem
    ) -> None:
        """Restore a list selection captured before a context-menu press."""
        wanted = set(asset_ids)
        self.tree.clearSelection()
        iterator = QTreeWidgetItemIterator(self.tree)
        while iterator.value() is not None:
            item = iterator.value()
            if (
                item.data(0, Qt.ItemDataRole.UserRole + 1) == "asset"
                and item.data(0, Qt.ItemDataRole.UserRole) in wanted
            ):
                item.setSelected(True)
            iterator += 1
        self.tree.selectionModel().setCurrentIndex(
            self.tree.indexFromItem(target, 0),
            QItemSelectionModel.SelectionFlag.NoUpdate,
        )

    def _on_tree_context_menu(self, pos: Any) -> None:
        """Show context menu for tree item."""
        item = self.tree.itemAt(pos)

        menu = QMenu(self)

        if item:
            context_ids = getattr(self.tree, "_context_menu_selection_ids", ())
            if context_ids:
                self._restore_tree_context_selection(list(context_ids), item)
            self.tree._context_menu_selection_ids = ()
            # Preserve a selected group; otherwise select only the item under
            # the context-menu pointer.
            self._select_single_context_tree_item(item)

        # Capture the selection after normalising the context-menu target.  A
        # right-click on a selected item keeps the whole group; a right-click
        # elsewhere intentionally reduces it to one item.
        batch_ids = self._collect_selected_asset_ids() if item else []
        if len(batch_ids) >= 2:
            self._append_batch_delete_action(menu, batch_ids)
            batch_act = menu.addAction(
                I18N.tr("edit.media_pool.batch_edit_log_menu", "批量标记…")
            )
            batch_act.triggered.connect(
                lambda _checked=False, ids=tuple(batch_ids): self._on_batch_edit_log(
                    list(ids)
                )
            )
            # F2.7：批量解释帧率（原始探测不改写；覆盖随工程保存可撤销）
            interpret_act = menu.addAction(I18N.tr(
                "edit.media_pool.batch_interpret_menu", "批量解释帧率…"))
            interpret_act.triggered.connect(
                lambda _checked=False, ids=tuple(batch_ids):
                    self._on_batch_interpret_fps(list(ids))
            )
            menu.addSeparator()
            # P1-01: 多机位组（≥2 个视频素材）
            self._append_multicam_group_action(menu, batch_ids)

        if item:
            item_type = item.data(0, Qt.ItemDataRole.UserRole + 1)
            item_id = item.data(0, Qt.ItemDataRole.UserRole)

            if item_type == "asset":
                asset = self._project.assets.get(item_id) if self._project else None
                if asset:
                    # Relink (for all assets, emphasized for offline)
                    if asset.is_offline:
                        relink_act = menu.addAction(
                            I18N.tr("edit.media_pool.relink", "Relink Offline Media")
                        )
                    else:
                        relink_act = menu.addAction(
                            I18N.tr("edit.media_pool.relink_path", "Relink to New Path")
                        )
                    relink_act.triggered.connect(lambda: self._on_relink_asset(item_id))

                    replace_act = menu.addAction(
                        I18N.tr("edit.media_pool.replace_media", "替换素材源…")
                    )
                    replace_act.triggered.connect(lambda: self._on_replace_media(item_id))

                    # F2.10：视频素材绑定外录 WAV（源级同步；非破坏）
                    if asset.media_type == "video":
                        bind_audio_act = menu.addAction(I18N.tr(
                            "edit.media_pool.bind_external_audio",
                            "绑定外录音频…"))
                        bind_audio_act.triggered.connect(
                            lambda _checked=False, aid=asset.asset_id:
                                self.bind_external_audio_requested.emit(aid))

                    menu.addSeparator()

                    # Phase 5: Open Comp Clip in Composite
                    if asset.is_comp_clip:
                        open_comp_act = menu.addAction(
                            I18N.tr("edit.media_pool.open_in_composite", "Open in Composite")
                        )
                        open_comp_act.triggered.connect(
                            lambda: self.open_comp_clip_in_composite_requested.emit(item_id)
                        )

                        # Edit Comp Clip Settings (resolution / fps / duration / bg color)
                        settings_act = menu.addAction(
                            I18N.tr("edit.media_pool.comp_clip_settings", "Comp Clip Settings...")
                        )
                        settings_act.triggered.connect(
                            lambda: self.edit_comp_clip_settings_requested.emit(item_id)
                        )
                        menu.addSeparator()

                    # P9.1: Open Asset Grade in Color (image only; video must go through timeline)
                    if asset.media_type == "image":
                        open_grade_act = menu.addAction(
                            I18N.tr("edit.media_pool.open_asset_grade", "Open Asset Grade in Color")
                        )
                        open_grade_act.triggered.connect(
                            lambda: self.open_asset_grade_requested.emit(item_id)
                        )

                        menu.addSeparator()

                    # Generate Proxy / Delete Proxy
                    if asset.has_proxy:
                        delete_proxy_act = menu.addAction(
                            I18N.tr("edit.media_pool.delete_proxy", "Delete Proxy")
                        )
                        delete_proxy_act.triggered.connect(lambda: self._on_delete_proxy(item_id))
                    else:
                        gen_proxy_act = menu.addAction(
                            I18N.tr("edit.media_pool.generate_proxy", "Generate Proxy")
                        )
                        gen_proxy_act.triggered.connect(lambda: self._on_generate_proxy(item_id))
                        # E21: 音频代理明确不支持——菜单项禁用而非点击后弹提示。
                        if getattr(asset, "is_audio", False):
                            gen_proxy_act.setEnabled(False)
                            gen_proxy_act.setToolTip(I18N.tr(
                                "edit.media_pool.no_proxy_audio_message",
                                "Proxy generation is not supported for audio files."))

                    menu.addSeparator()

                    # Rename asset (inline edit, Finder-style)
                    rename_asset_act = menu.addAction(
                        I18N.tr("edit.media_pool.rename_asset", "Rename")
                    )
                    rename_asset_act.triggered.connect(
                        lambda: self._start_rename_asset(item_id)
                    )

                    # Duplicate asset (new id, shared source file)
                    dup_asset_act = menu.addAction(
                        I18N.tr("edit.media_pool.duplicate_asset", "Duplicate")
                    )
                    dup_asset_act.triggered.connect(
                        lambda: self._duplicate_selected_assets()
                    )

                    # 添加到时间线
                    add_to_tl_act = menu.addAction(
                        I18N.tr("edit.media_pool.add_to_timeline", "Add to Timeline")
                    )
                    add_to_tl_act.triggered.connect(
                        lambda: self.add_to_timeline_requested.emit(item_id)
                    )

                    # P9-3: Scene Cut Detection (for video assets only)
                    if asset.media_type == "video":
                        menu.addSeparator()
                        scene_cut_act = menu.addAction(
                            I18N.tr("edit.media_pool.scene_cut_detect", "Scene Cut Detection...")
                        )
                        scene_cut_act.triggered.connect(
                            lambda: self.scene_cut_detect_requested.emit(item_id)
                        )

                    # G10：Topos 源单帧零再编码导出（.toos）——直取码流
                    # packet 封装（TMET/档位透传，已建成提取器首接 UI）；
                    # 帧间档 P 帧由提取器显式拒收并提示。
                    if (asset.media_type == "video" and str(
                            (getattr(asset, "metadata", {}) or {})
                            .get("codec", "")) == "topos"):
                        menu.addSeparator()
                        frame_toos_act = menu.addAction(
                            I18N.tr("edit.media_pool.export_frame_toos",
                                    "Export Frame as .toos (lossless)...")
                        )
                        frame_toos_act.triggered.connect(
                            lambda aid=item_id: self._export_frame_to_toos(aid)
                        )

                    # M3: AI 媒体分析（§17.2）。开关 off 时隐藏分析项。
                    if self._media_analysis_enabled:
                        self._add_analysis_actions_to_menu(menu, item_id)

                    menu.addSeparator()

                    # 删除：只移除工程引用，不触碰源文件。
                    delete_act = menu.addAction(
                        I18N.tr("edit.media_pool.remove_from_project", "Remove from Media Pool")
                    )
                    delete_act.triggered.connect(lambda: self._delete_asset(item_id))

                    menu.addSeparator()

                    # 在文件管理器中显示
                    reveal_act = menu.addAction(I18N.tr("edit.media_pool.reveal", "Show in File Manager"))
                    reveal_act.triggered.connect(lambda: self._reveal_file(asset.path))

                    menu.addSeparator()

                    # 属性
                    props_act = menu.addAction(I18N.tr("edit.media_pool.properties", "Properties"))
                    props_act.triggered.connect(lambda: self._show_properties(asset))

            elif item_type == "bin":
                # 打开素材箱（进入）
                open_act = menu.addAction(I18N.tr("edit.media_pool.open_bin", "Open Bin"))
                open_act.triggered.connect(lambda: self._enter_bin(item_id))

                # 创建子素材箱
                create_sub_act = menu.addAction(I18N.tr("edit.media_pool.create_sub_bin", "Create Sub Bin"))
                create_sub_act.triggered.connect(lambda: self._create_bin(parent_id=item_id))

                # Phase 5: New Comp Clip
                menu.addSeparator()
                new_comp_clip_act = menu.addAction(
                    I18N.tr("edit.media_pool.new_comp_clip", "New Comp Clip")
                )
                new_comp_clip_act.triggered.connect(
                    lambda: self.create_empty_comp_clip_requested.emit()
                )

                menu.addSeparator()

                # 重命名素材箱（原地编辑）
                rename_act = menu.addAction(I18N.tr("edit.media_pool.rename_bin", "Rename Bin"))
                rename_act.triggered.connect(lambda: self._start_rename_bin(item_id))

                # 删除素材箱（不能删除 root bin）
                if item_id != self._project.root_bin_id:
                    menu.addSeparator()
                    delete_act = menu.addAction(
                        I18N.tr(
                            "edit.media_pool.remove_from_project",
                            "Remove from Media Pool",
                        )
                    )
                    delete_act.triggered.connect(lambda: self._delete_bin(item_id))

                menu.addSeparator()
                bin_props_act = menu.addAction(
                    I18N.tr("edit.media_pool.properties", "Properties")
                )
                bin_props_act.triggered.connect(
                    lambda: self._show_bin_properties(item_id)
                )

            elif item_type == "timeline":
                open_act = menu.addAction(I18N.tr("edit.media_pool.open_timeline", "Open Timeline"))
                open_act.triggered.connect(lambda: self.timeline_switch_requested.emit(item_id))

                settings_act = menu.addAction(
                    I18N.tr("edit.media_pool.timeline_settings", "Timeline Settings...")
                )
                settings_act.triggered.connect(
                    lambda: self.timeline_settings_requested.emit(item_id)
                )

                menu.addSeparator()
                delete_act = menu.addAction(
                    I18N.tr("edit.media_pool.remove_from_project", "Remove from Media Pool")
                )
                delete_act.triggered.connect(
                    lambda tid=item_id: self._delete_selected_pool_items(
                        selected_items=[("timeline", tid)]
                    )
                )

        # Always show panel actions (Import, New Bin, New Timeline, Relink)
        if menu.actions():
            menu.addSeparator()

        import_act = menu.addAction(I18N.tr("edit.media_pool.import", "Import Media..."))
        import_act.triggered.connect(self._on_import_clicked)

        import_folder_act = menu.addAction(
            I18N.tr("edit.media_pool.import_folder", "Import Folder...")
        )
        import_folder_act.triggered.connect(self._on_import_folder_clicked)

        new_bin_act = menu.addAction(I18N.tr("edit.media_pool.new_bin", "New Bin"))
        new_bin_act.triggered.connect(self._on_new_bin_clicked)

        self._append_motion_graphic_create_action(menu)

        collect_act = menu.addAction(
            I18N.tr("edit.media_pool.collect_project", "收集工程…")
        )
        collect_act.triggered.connect(self._on_collect_project)

        consolidate_act = menu.addAction(
            I18N.tr("edit.media_pool.consolidate_menu", "整理工程（按剪辑范围）…")
        )
        consolidate_act.triggered.connect(self._on_consolidate_project)

        proxy_policy_act = menu.addAction(
            I18N.tr("edit.media_pool.proxy_policy", "代理策略…")
        )
        proxy_policy_act.triggered.connect(self._on_proxy_policy_settings)

        new_timeline_act = menu.addAction(I18N.tr("edit.media_pool.new_timeline", "New Timeline"))
        new_timeline_act.triggered.connect(self.timeline_create_requested.emit)

        # M3: 批量分析入口（§17.2）。开关 on 且有素材时显示。"全部"由 host 弹二次确认。
        if (
            self._media_analysis_enabled
            and self._project
            and self._project.assets
        ):
            menu.addSeparator()
            analyze_bin_act = menu.addAction(
                I18N.tr("edit.media_pool.analyze_bin", "Analyze Current Bin...")
            )
            analyze_bin_act.triggered.connect(
                lambda: self._emit_analysis_scope("bin")
            )
            analyze_all_act = menu.addAction(
                I18N.tr("edit.media_pool.analyze_all", "Analyze All Assets...")
            )
            analyze_all_act.triggered.connect(
                lambda: self._emit_analysis_scope("all")
            )

        # Relink Offline (only show if there are offline assets)
        if self._project and any(a.is_offline for a in self._project.assets.values()):
            menu.addSeparator()
            relink_act = menu.addAction(I18N.tr("edit.media_pool.relink_offline", "Relink Offline"))
            relink_act.triggered.connect(self._on_relink_batch)

        # 显示菜单
        global_pos = self.tree.viewport().mapToGlobal(pos)
        menu.exec(global_pos)

    def _on_grid_context_menu(self, pos: Any, source_view: Optional[QListView] = None) -> None:
        """Show context menu for grid view item (assets and timelines)."""
        view = source_view if source_view is not None else self.grid_view
        if view is None or not self.asset_model:
            return

        # Get the index at the position
        index = view.indexAt(pos)
        if not index.isValid():
            # Click on empty area - show panel actions only
            menu = QMenu(self)
            import_act = menu.addAction(I18N.tr("edit.media_pool.import", "Import Media..."))
            import_act.triggered.connect(self._on_import_clicked)

            new_bin_act = menu.addAction(I18N.tr("edit.media_pool.new_bin", "New Bin"))
            new_bin_act.triggered.connect(self._on_new_bin_clicked)

            new_comp_clip_act = menu.addAction(
                I18N.tr("edit.media_pool.new_comp_clip", "New Comp Clip")
            )
            new_comp_clip_act.triggered.connect(
                lambda: self.create_empty_comp_clip_requested.emit()
            )

            self._append_motion_graphic_create_action(menu)

            new_timeline_act = menu.addAction(I18N.tr("edit.media_pool.new_timeline", "New Timeline"))
            new_timeline_act.triggered.connect(self.timeline_create_requested.emit)

            proxy_policy_grid_act = menu.addAction(
                I18N.tr("edit.media_pool.proxy_policy", "代理策略…")
            )
            proxy_policy_grid_act.triggered.connect(self._on_proxy_policy_settings)

            # M3: 批量分析入口（§17.2）。开关 on 且有素材时显示。
            if (
                self._media_analysis_enabled
                and self._project
                and self._project.assets
            ):
                menu.addSeparator()
                analyze_bin_act = menu.addAction(
                    I18N.tr("edit.media_pool.analyze_bin", "Analyze Current Bin...")
                )
                analyze_bin_act.triggered.connect(
                    lambda: self._emit_analysis_scope("bin")
                )
                analyze_all_act = menu.addAction(
                    I18N.tr("edit.media_pool.analyze_all", "Analyze All Assets...")
                )
                analyze_all_act.triggered.connect(
                    lambda: self._emit_analysis_scope("all")
                )

            dock = find_parent_dock(self)
            if dock is not None:
                menu.addSeparator()
                add_dock_management_actions(menu, dock)

            global_pos = view.viewport().mapToGlobal(pos)
            menu.exec(global_pos)
            return

        if index.model() is not self.asset_model:
            return

        # Restore the pre-context-menu group if Qt collapsed it while
        # processing the right-button press.  The action also receives the
        # captured IDs below, so a later menu interaction cannot change the
        # deletion target.
        context_ids = getattr(view, "_context_menu_selection_ids", ())
        if context_ids:
            self._restore_grid_context_selection(list(context_ids), index)
        view._context_menu_selection_ids = ()

        # Preserve a selected group; otherwise select only the card under the
        # context-menu pointer.  This keeps single-item actions unambiguous
        # without destroying an intentional multi-selection.
        self._select_single_context_grid_item(index)

        # Capture the selection after normalising the context-menu target so
        # batch actions operate on exactly the visible selection.
        batch_ids = self._collect_selected_asset_ids()

        # Get item type and data
        item_type = self.asset_model.data(index, AssetListModel.ITEM_TYPE_ROLE)
        item_id = self.asset_model.data(index, AssetListModel.ITEM_ID_ROLE)

        menu = QMenu(self)

        if len(batch_ids) >= 2:
            self._append_batch_delete_action(menu, batch_ids)
            batch_act = menu.addAction(
                I18N.tr("edit.media_pool.batch_edit_log_menu", "批量标记…")
            )
            batch_act.triggered.connect(
                lambda _checked=False, ids=tuple(batch_ids): self._on_batch_edit_log(
                    list(ids)
                )
            )
            # F2.7：批量解释帧率（原始探测不改写；覆盖随工程保存可撤销）
            interpret_act = menu.addAction(I18N.tr(
                "edit.media_pool.batch_interpret_menu", "批量解释帧率…"))
            interpret_act.triggered.connect(
                lambda _checked=False, ids=tuple(batch_ids):
                    self._on_batch_interpret_fps(list(ids))
            )
            menu.addSeparator()
            # P1-01: 多机位组（≥2 个视频素材）
            self._append_multicam_group_action(menu, batch_ids)

        if item_type == "asset":
            asset = self.asset_model.data(index, Qt.ItemDataRole.UserRole)
            if isinstance(asset, MediaAsset):
                # Relink (for all assets, emphasized for offline)
                if asset.is_offline:
                    relink_act = menu.addAction(
                        I18N.tr("edit.media_pool.relink", "Relink Offline Media")
                    )
                else:
                    relink_act = menu.addAction(
                        I18N.tr("edit.media_pool.relink_path", "Relink to New Path")
                    )
                relink_act.triggered.connect(lambda: self._on_relink_asset(asset.asset_id))

                replace_act = menu.addAction(
                    I18N.tr("edit.media_pool.replace_media", "替换素材源…")
                )
                replace_act.triggered.connect(
                    lambda: self._on_replace_media(asset.asset_id)
                )

                menu.addSeparator()

                # Phase 5: Open Comp Clip in Composite
                if asset.is_comp_clip:
                    open_comp_act = menu.addAction(
                        I18N.tr("edit.media_pool.open_in_composite", "Open in Composite")
                    )
                    open_comp_act.triggered.connect(
                        lambda: self.open_comp_clip_in_composite_requested.emit(asset.asset_id)
                    )
                    menu.addSeparator()

                # P9.1: Open Asset Grade in Color (image only; video must go through timeline)
                if asset.media_type == "image":
                    open_grade_act = menu.addAction(
                        I18N.tr("edit.media_pool.open_asset_grade", "Open Asset Grade in Color")
                    )
                    open_grade_act.triggered.connect(
                        lambda: self.open_asset_grade_requested.emit(asset.asset_id)
                    )
                    menu.addSeparator()

                # Generate Proxy / Delete Proxy
                if asset.has_proxy:
                    delete_proxy_act = menu.addAction(
                        I18N.tr("edit.media_pool.delete_proxy", "Delete Proxy")
                    )
                    delete_proxy_act.triggered.connect(lambda: self._on_delete_proxy(asset.asset_id))
                else:
                    gen_proxy_act = menu.addAction(
                        I18N.tr("edit.media_pool.generate_proxy", "Generate Proxy")
                    )
                    gen_proxy_act.triggered.connect(lambda: self._on_generate_proxy(asset.asset_id))
                    # E21: 音频代理明确不支持——菜单项禁用而非点击后弹提示。
                    if getattr(asset, "is_audio", False):
                        gen_proxy_act.setEnabled(False)
                        gen_proxy_act.setToolTip(I18N.tr(
                            "edit.media_pool.no_proxy_audio_message",
                            "Proxy generation is not supported for audio files."))

                menu.addSeparator()

                # Rename asset (inline edit, Finder-style)
                rename_asset_act = menu.addAction(
                    I18N.tr("edit.media_pool.rename_asset", "Rename")
                )
                rename_asset_act.triggered.connect(
                    lambda: self._start_rename_asset(asset.asset_id)
                )

                # Duplicate asset (new id, shared source file)
                dup_asset_act = menu.addAction(
                    I18N.tr("edit.media_pool.duplicate_asset", "Duplicate")
                )
                dup_asset_act.triggered.connect(
                    lambda: self._duplicate_selected_assets()
                )

                # 添加到时间线
                add_to_tl_act = menu.addAction(
                    I18N.tr("edit.media_pool.add_to_timeline", "Add to Timeline")
                )
                add_to_tl_act.triggered.connect(
                    lambda: self.add_to_timeline_requested.emit(asset.asset_id)
                )

                # P9-3: Scene Cut Detection (for video assets only)
                if asset.media_type == "video":
                    menu.addSeparator()
                    scene_cut_act = menu.addAction(
                        I18N.tr("edit.media_pool.scene_cut_detect", "Scene Cut Detection...")
                    )
                    scene_cut_act.triggered.connect(
                        lambda: self.scene_cut_detect_requested.emit(asset.asset_id)
                    )

                # M3: AI 媒体分析（§17.2）。开关 off 时隐藏分析项。
                if self._media_analysis_enabled:
                    self._add_analysis_actions_to_menu(menu, asset.asset_id)

                menu.addSeparator()

                # 删除：只移除工程引用，不触碰源文件。
                delete_act = menu.addAction(
                    I18N.tr("edit.media_pool.remove_from_project", "Remove from Media Pool")
                )
                delete_act.triggered.connect(lambda: self._delete_asset(asset.asset_id))

                menu.addSeparator()

                # 在文件管理器中显示
                reveal_act = menu.addAction(I18N.tr("edit.media_pool.reveal", "Show in File Manager"))
                reveal_act.triggered.connect(lambda: self._reveal_file(asset.path))

                menu.addSeparator()

                # 属性
                props_act = menu.addAction(I18N.tr("edit.media_pool.properties", "Properties"))
                props_act.triggered.connect(lambda: self._show_properties(asset))

        elif item_type == "timeline":
            open_act = menu.addAction(I18N.tr("edit.media_pool.open_timeline", "Open Timeline"))
            open_act.triggered.connect(lambda: self.timeline_switch_requested.emit(item_id))

            settings_act = menu.addAction(
                I18N.tr("edit.media_pool.timeline_settings", "Timeline Settings...")
            )
            settings_act.triggered.connect(
                lambda: self.timeline_settings_requested.emit(item_id)
            )

            menu.addSeparator()
            delete_act = menu.addAction(
                I18N.tr("edit.media_pool.remove_from_project", "Remove from Media Pool")
            )
            delete_act.triggered.connect(
                lambda tid=item_id: self._delete_selected_pool_items(
                    selected_items=[("timeline", tid)]
                )
            )

        elif item_type == "bin" and item_id:
            # 打开素材箱（进入）
            open_act = menu.addAction(I18N.tr("edit.media_pool.open_bin", "Open Bin"))
            open_act.triggered.connect(lambda: self._enter_bin(item_id))

            # 创建子素材箱
            create_sub_act = menu.addAction(
                I18N.tr("edit.media_pool.create_sub_bin", "Create Sub Bin")
            )
            create_sub_act.triggered.connect(lambda: self._create_bin(parent_id=item_id))

            menu.addSeparator()

            # 重命名素材箱（原地编辑）
            rename_act = menu.addAction(I18N.tr("edit.media_pool.rename_bin", "Rename Bin"))
            rename_act.triggered.connect(lambda: self._start_rename_bin(item_id))

            # 删除素材箱（不能删除 root bin）
            if self._project and item_id != self._project.root_bin_id:
                menu.addSeparator()
                delete_act = menu.addAction(
                    I18N.tr(
                        "edit.media_pool.remove_from_project",
                        "Remove from Media Pool",
                    )
                )
                delete_act.triggered.connect(lambda: self._delete_bin(item_id))

            menu.addSeparator()
            bin_props_act = menu.addAction(
                I18N.tr("edit.media_pool.properties", "Properties")
            )
            bin_props_act.triggered.connect(
                lambda: self._show_bin_properties(item_id)
            )

        # Always show panel actions (Import, New Bin, New Timeline, Relink)
        if menu.actions():
            menu.addSeparator()

        import_act = menu.addAction(I18N.tr("edit.media_pool.import", "Import Media..."))
        import_act.triggered.connect(self._on_import_clicked)

        import_folder_act = menu.addAction(
            I18N.tr("edit.media_pool.import_folder", "Import Folder...")
        )
        import_folder_act.triggered.connect(self._on_import_folder_clicked)

        new_bin_act = menu.addAction(I18N.tr("edit.media_pool.new_bin", "New Bin"))
        new_bin_act.triggered.connect(self._on_new_bin_clicked)

        new_timeline_act = menu.addAction(I18N.tr("edit.media_pool.new_timeline", "New Timeline"))
        new_timeline_act.triggered.connect(self.timeline_create_requested.emit)

        self._append_motion_graphic_create_action(menu)

        # Relink Offline (only show if there are offline assets)
        if self._project and any(a.is_offline for a in self._project.assets.values()):
            menu.addSeparator()
            relink_act = menu.addAction(I18N.tr("edit.media_pool.relink_offline", "Relink Offline"))
            relink_act.triggered.connect(self._on_relink_batch)

        dock = find_parent_dock(self)
        if dock is not None:
            menu.addSeparator()
            add_dock_management_actions(menu, dock)

        # 显示菜单
        global_pos = view.viewport().mapToGlobal(pos)
        menu.exec(global_pos)

    def _append_motion_graphic_create_action(self, menu: QMenu) -> None:
        """Add the optional Motion Graphic creation entry to a pool menu."""
        if not self._motion_graphic_creation_available:
            return
        action = menu.addAction(
            I18N.tr(
                "edit.media_pool.new_motion_graphic_clip",
                "New Motion Graphic Clip",
            )
        )
        action.triggered.connect(
            lambda _checked=False: self.create_empty_motion_graphic_requested.emit()
        )

    def set_motion_graphic_creation_available(self, available: bool) -> None:
        """Expose Motion Graphic creation only when its feature is attached."""
        self._motion_graphic_creation_available = bool(available)

    # ==================== Actions ====================

    def _delete_asset(self, asset_id: str) -> None:
        """Remove an asset from the project while keeping its source file."""
        self._do_remove_asset(asset_id)

    def _confirm_delete_with_usage(self, asset_ids: List[str]) -> bool:
        """R11（媒体池复查）：删除确认消费**扩展使用索引**。

        ``usage_check_for_assets`` 把同路径父/子片段整组核验（删其一
        不等于文件无人引用），时间线/嵌套引用全部列出；与定位、Replace
        的使用检查共用同一索引——两处看到的使用事实一致。无任何引用
        时不弹窗（Delete 键行为不变）；用户拒绝则中止删除。
        """
        from src.features.edit.core.asset_usage_index import (
            usage_check_for_assets,
        )
        sequences = self._collect_timeline_sequences()
        try:
            usage = usage_check_for_assets(
                self._project, sequences, asset_ids)
        except Exception:  # noqa: BLE001 — 索引失败不阻断删除
            logger.exception("usage check before delete failed")
            return True
        if not usage:
            return True
        lines = [I18N.tr(
            "edit.media_pool.delete_usage_header",
            "以下素材正被时间线使用（含同路径父/子片段组），"
            "删除后引用片段将离线：")]
        for asset_id, locations in list(usage.items())[:12]:
            asset = self._project.assets.get(asset_id)
            name = asset.display_name if asset else asset_id
            lines.append(f"  · {name}")
            for loc in locations[:3]:
                lines.append(f"      {loc.format()}")
        if len(usage) > 12:
            lines.append(
                I18N.tr("edit.media_pool.usage_more",
                        "  · …（另 {n} 个素材）", n=len(usage) - 12))
        reply = QMessageBox.question(
            self._get_top_level_widget(),
            I18N.tr("edit.media_pool.delete_usage_title",
                    "删除使用中的素材"),
            "\n".join(lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _do_remove_asset(self, asset_id: str) -> bool:
        """Remove one asset through the same command path as batch deletion."""
        if not self._project or asset_id not in self._project.assets:
            return False
        if not self._confirm_delete_with_usage([asset_id]):
            return False
        self._invalidate_proxy_job(asset_id)
        try:
            new_project = self._command_stack.execute(
                DeleteAssetCommand.create(asset_id), self._project
            )
        except ValueError as e:
            QMessageBox.warning(
                self._get_top_level_widget(),
                I18N.tr("edit.media_pool.error", "Error"),
                str(e),
            )
            return False
        self._project = new_project
        self.project_changed.emit(new_project)
        # E22: asset_removed emit 已删除（无接收者；project_changed 是活链路）
        self._refresh_tree(refresh_bins=True)
        self._update_status_label()
        return True

    def _delete_bin(self, bin_id: str) -> None:
        """Remove a folder from the project without deleting its contents."""
        self._delete_selected_pool_items(
            selected_items=[("bin", bin_id)]
        )

    def apply_source_patch(self, asset_id: str, spec) -> bool:
        """F2.1：提交素材级 Source Patch 覆盖（UpdateSourcePatchCommand）。

        spec 为 ``SourcePatchSpec``，None = 清除覆盖回默认直通。经
        ProjectCommandStack 单步可撤销；映射越界（素材已知声道数时）以
        明确弹窗反馈，不落半提交。
        """
        if not self._project or asset_id not in self._project.assets:
            return False
        try:
            new_project = self._command_stack.execute(
                UpdateSourcePatchCommand.create(asset_id, spec),
                self._project,
            )
        except ValueError as e:
            QMessageBox.warning(
                self._get_top_level_widget(),
                I18N.tr("edit.media_pool.error", "Error"),
                str(e),
            )
            return False
        self._project = new_project
        self.project_changed.emit(new_project)
        return True

    # ==================== Keyboard Batch Delete ====================

    def _delete_from_focused_view(self) -> None:
        """Delete the selection belonging to the currently focused pool view."""
        focused = QApplication.focusWidget()
        if focused is None:
            return

        for view in (self.bin_tree, self.tree, self.grid_view, self.metadata_shelf):
            if view is None:
                continue
            if focused is view or view.isAncestorOf(focused):
                self.handle_delete_key_from(view)
                return

    def handle_delete_key_from(self, source_widget: QWidget) -> bool:
        """
        Entry point for Delete/Backspace keys captured by child views.

        Dispatches to the appropriate batch-delete handler based on which
        child widget currently holds focus. Returns ``True`` when the key
        event was consumed (a delete actually happened or was intentionally
        ignored), ``False`` to let the caller fall back to default handling.

        Args:
            source_widget: The child widget that received the key event
                (``BinTreeWidget``, ``AssetTreeWidget`` or ``AssetListView``).

        Returns:
            ``True`` if the event was handled, ``False`` otherwise.
        """
        if self._project is None:
            return False

        # Bin tree owns bin deletion
        if source_widget is self.bin_tree:
            return self._delete_selected_bins()
        # List / grid views own asset + timeline deletion
        if source_widget is self.metadata_shelf:
            return self._delete_selected_pool_items(source_widget=self.grid_view)
        if source_widget is self.tree or source_widget is self.grid_view:
            return self._delete_selected_pool_items(source_widget=source_widget)
        return False

    def _delete_selected_bins(self) -> bool:
        """Delete all selected bins via the project command stack.

        Smart bins and the root bin are silently skipped (not deletable).
        Returns ``True`` if at least one bin was deleted.
        """
        if self._project is None:
            return False

        bin_ids: List[str] = []
        for item in self.bin_tree.selectedItems():
            bin_id = item.data(0, BinTreeWidget.BIN_ID_ROLE)
            bin_type = item.data(0, BinTreeWidget.BIN_TYPE_ROLE)
            if not bin_id or bin_type != "bin":
                continue
            if bin_id == self._project.root_bin_id:
                continue
            bin_ids.append(bin_id)

        # De-duplicate while preserving order
        seen: set[str] = set()
        unique_bin_ids: List[str] = []
        for bid in bin_ids:
            if bid not in seen:
                seen.add(bid)
                unique_bin_ids.append(bid)

        if not unique_bin_ids:
            return False

        commands = [DeleteBinCommand.create(bid) for bid in unique_bin_ids]
        label = I18N.tr(
            "edit.media_pool.delete_selected_bins",
            "Delete {count} bins",
            count=len(unique_bin_ids),
        ) if len(unique_bin_ids) > 1 else I18N.tr(
            "edit.media_pool.delete_bin", "Delete Bin"
        )
        macro = ProjectMacroCommand.create(label, commands)

        previous_bin_id = self._current_bin_id
        try:
            new_project = self._command_stack.execute(macro, self._project)
        except ValueError as e:
            QMessageBox.warning(
                self._get_top_level_widget(),
                I18N.tr("edit.media_pool.error", "Error"),
                str(e),
            )
            return False

        self._project = new_project
        # Clear current bin selection if it was deleted
        if previous_bin_id in unique_bin_ids:
            self._current_bin_id = ""
            self._current_bin_is_smart = False
        self.project_changed.emit(new_project)
        self._refresh_bin_tree()
        self._refresh_tree()
        self._update_status_label()
        return True

    def _delete_selected_pool_items(
        self,
        source_widget: Optional[QWidget] = None,
        selected_asset_ids: Optional[List[str]] = None,
        selected_items: Optional[List[tuple[str, str]]] = None,
    ) -> bool:
        """Delete all selected assets, timelines, and bins via the project command stack.

        ``source_widget`` makes keyboard dispatch independent of a stale view
        mode flag. ``selected_asset_ids`` and ``selected_items`` are used by
        context-menu actions to keep the exact pre-menu selection stable until
        the action is triggered.

        Returns ``True`` if at least one element was deleted.
        """
        if self._project is None:
            return False

        # Collect (item_type, item_id) tuples from the focused view.
        selection: list[tuple[str, str]] = []
        if selected_items is not None:
            selection = [
                (item_type, item_id)
                for item_type, item_id in dict.fromkeys(selected_items)
                if item_type in ("asset", "timeline", "bin") and item_id
            ]
        elif selected_asset_ids is not None:
            selection = [
                ("asset", asset_id)
                for asset_id in dict.fromkeys(selected_asset_ids)
                if asset_id in self._project.assets
            ]
        elif source_widget is self.tree or (
            source_widget is None and self._view_mode == ViewMode.LIST
        ):
            for item in self.tree.selectedItems():
                if item is None:
                    continue
                item_type = item.data(0, Qt.ItemDataRole.UserRole + 1)
                item_id = item.data(0, Qt.ItemDataRole.UserRole)
                if item_type in ("asset", "timeline", "bin") and item_id:
                    selection.append((item_type, item_id))
        elif (
            source_widget is self.grid_view
            or (source_widget is None and self._view_mode != ViewMode.LIST)
        ) and self.grid_view is not None and self.asset_model is not None:
            seen_ids: set[tuple[str, str]] = set()
            for index in self.grid_view.selectionModel().selectedIndexes():
                if not index.isValid():
                    continue
                item_type = self.asset_model.data(
                    index, AssetListModel.ITEM_TYPE_ROLE
                )
                item_id = self.asset_model.data(index, AssetListModel.ITEM_ID_ROLE)
                if item_type in ("asset", "timeline", "bin") and item_id:
                    key = (item_type, item_id)
                    if key not in seen_ids:
                        seen_ids.add(key)
                        selection.append(key)

        if not selection:
            return False

        asset_ids = [iid for (itype, iid) in selection if itype == "asset"]
        timeline_ids = [iid for (itype, iid) in selection if itype == "timeline"]
        bin_ids = [iid for (itype, iid) in selection if itype == "bin"]

        # De-duplicate
        asset_ids = list(dict.fromkeys(asset_ids))
        timeline_ids = list(dict.fromkeys(timeline_ids))
        bin_ids = list(dict.fromkeys(bin_ids))

        # Filter out non-deletable bins: root bin is protected.
        if self._project is not None:
            bin_ids = [b for b in bin_ids if b != self._project.root_bin_id]

        # R11：素材删除前经扩展使用索引确认（同路径整组 + 全部引用）。
        if asset_ids and not self._confirm_delete_with_usage(asset_ids):
            return False

        # Build sub-commands
        sub_commands: list[Any] = []
        for aid in asset_ids:
            if aid in self._project.assets:
                sub_commands.append(DeleteAssetCommand.create(aid))
        for tid in timeline_ids:
            if tid in self._project.timelines:
                sub_commands.append(DeleteTimelineCommand.create(tid))
        for bid in bin_ids:
            if bid in self._project.bins:
                sub_commands.append(DeleteBinCommand.create(bid))

        if not sub_commands:
            return False

        # Compose a human-readable macro label
        parts: list[str] = []
        if asset_ids:
            parts.append(
                I18N.tr(
                    "edit.media_pool.delete_selected_assets",
                    "{count} asset(s)",
                    count=len(asset_ids),
                )
            )
        if timeline_ids:
            parts.append(
                I18N.tr(
                    "edit.media_pool.delete_selected_timelines",
                    "{count} timeline(s)",
                    count=len(timeline_ids),
                )
            )
        if bin_ids:
            parts.append(
                I18N.tr(
                    "edit.media_pool.delete_selected_bins_count",
                    "{count} bin(s)",
                    count=len(bin_ids),
                )
            )
        label = I18N.tr(
            "edit.media_pool.delete_selected",
            "Delete {items}",
            items=", ".join(parts),
        )
        macro = ProjectMacroCommand.create(label, sub_commands)

        # Capture timeline snapshots BEFORE executing the macro, so the edit
        # controller can restore full TimelineSequence objects on undo. We
        # synchronously request snapshots via a signal/callback; if no slot is
        # connected (e.g. during tests), we proceed without snapshots.
        captured_snapshots: dict[str, Any] = {}
        if timeline_ids:
            try:
                self.timeline_command_snapshot_requested.emit(
                    list(timeline_ids), captured_snapshots.update
                )
            except Exception:
                logger.exception("Failed to request timeline snapshots")
        if captured_snapshots:
            self._store_timeline_snapshots(timeline_ids, captured_snapshots)

        # Detect if we're about to delete the bin currently being viewed — if
        # so we need to navigate up afterwards to avoid pointing at a gone bin.
        viewing_deleted_bin = (
            self._current_bin_id in bin_ids
            or any(bid in self._navigation_stack for bid in bin_ids)
        )

        try:
            new_project = self._command_stack.execute(macro, self._project)
        except ValueError as e:
            QMessageBox.warning(
                self._get_top_level_widget(),
                I18N.tr("edit.media_pool.error", "Error"),
                str(e),
            )
            return False

        # Cancel any pending proxy jobs for the removed assets
        for aid in asset_ids:
            self._invalidate_proxy_job(aid)

        # If we deleted the bin we were viewing (or any of its ancestors),
        # reset navigation to the root level so the view stays consistent.
        if viewing_deleted_bin:
            self._navigation_stack = [
                bid for bid in self._navigation_stack if bid in new_project.bins
            ]
            if self._current_bin_id not in new_project.bins:
                self._current_bin_id = ""
                self._current_bin_is_smart = False

        self._project = new_project
        self.project_changed.emit(new_project)
        # E22: asset_removed emit 已删除（无接收者）
        self._update_breadcrumb()
        self._refresh_tree(refresh_bins=True)
        self._update_status_label()
        return True

    def _store_timeline_snapshots(
        self,
        timeline_ids: List[str],
        snapshots: dict[str, Any],
    ) -> None:
        """Cache full TimelineSequence snapshots for undo restoration.

        The edit controller inspects this cache when ``project_changed`` is
        emitted with a previously-unknown timeline_id (i.e. after an undo),
        falling back to an empty sequence only if no snapshot is found.
        """
        if not hasattr(self, "_timeline_snapshots_for_undo") or \
                self._timeline_snapshots_for_undo is None:
            self._timeline_snapshots_for_undo: dict[str, Any] = {}
        for tid in timeline_ids:
            seq = snapshots.get(tid)
            if seq is not None:
                self._timeline_snapshots_for_undo[tid] = seq

    def consume_timeline_snapshot(self, timeline_id: str) -> Any:
        """Pop and return a cached TimelineSequence snapshot, if any.

        Called by the edit controller when restoring a timeline that reappeared
        after an undo. Returns ``None`` if no snapshot is cached.
        """
        cache = getattr(self, "_timeline_snapshots_for_undo", None)
        if not cache:
            return None
        return cache.pop(timeline_id, None)

    def _reveal_file(self, path: str) -> None:
        """Show file in system file manager."""
        if not os.path.exists(path):
            QMessageBox.warning(
                self._get_top_level_widget(),
                I18N.tr("edit.media_pool.file_not_found", "File Not Found"),
                I18N.tr("edit.media_pool.file_not_found_message",
                        "The file '{path}' does not exist.",
                        path=path)
            )
            return

        system = platform.system()

        try:
            if system == "Darwin":  # macOS
                subprocess.run(["open", "-R", path], check=True)
            elif system == "Windows":
                subprocess.run(["explorer", "/select,", path], check=True)
            else:  # Linux
                # 尝试多种文件管理器
                folder = os.path.dirname(path)
                subprocess.run(["xdg-open", folder], check=True)
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to reveal file: {path}, error={e}")
            QMessageBox.warning(
                self._get_top_level_widget(),
                I18N.tr("edit.media_pool.error", "Error"),
                I18N.tr("edit.media_pool.reveal_failed", "Failed to open file manager.")
            )

    def _close_pool_info(self) -> None:
        if self._pool_info_popup is not None:
            self._pool_info_popup.close()
        self._pool_info_key = None

    def _clear_pool_info_reference(self, popup: QWidget) -> None:
        if self._pool_info_popup is popup:
            self._pool_info_popup = None
            self._pool_info_key = None

    def _show_selected_pool_info(self) -> None:
        if self.asset_model is None:
            return
        keys, current, _scroll = self._capture_pool_selection()
        target = current if current in keys else (keys[0] if len(keys) == 1 else None)
        if target is None:
            return
        for row in range(self.asset_model.rowCount()):
            index = self.asset_model.index(row)
            key = (index.data(AssetListModel.ITEM_TYPE_ROLE), index.data(AssetListModel.ITEM_ID_ROLE))
            if key == target:
                if self._view_mode == ViewMode.LIST:
                    for tree_row in range(self.tree.topLevelItemCount()):
                        item = self.tree.topLevelItem(tree_row)
                        item_key = (item.data(0, Qt.ItemDataRole.UserRole + 1), item.data(0, Qt.ItemDataRole.UserRole))
                        if item_key == target:
                            self._show_pool_item_info(index, self.tree, self.tree.visualItemRect(item))
                            break
                else:
                    view = self.metadata_shelf if self._view_mode == ViewMode.METADATA and key[0] == "timeline" else self.grid_view
                    self._show_pool_item_info(index, view)
                return

    def _show_pool_item_info(self, index: QModelIndex, view: QAbstractItemView, anchor_rect: Optional[QRect] = None) -> None:
        """Badge clicks inspect cached metadata without changing selection."""
        from .media_pool_info import MediaPoolInfoPopup
        if self._project is None or not index.isValid():
            return
        kind, item_id = index.data(AssetListModel.ITEM_TYPE_ROLE), index.data(AssetListModel.ITEM_ID_ROLE)
        key = (kind, item_id)
        if kind not in ("asset", "timeline"):
            return
        if self._pool_info_popup is not None and self._pool_info_popup.isVisible() and self._pool_info_key == key:
            self._close_pool_info()
            return
        self._close_pool_info()
        data = self._project.assets.get(item_id) if kind == "asset" else self._project.timelines.get(item_id)
        if data is None:
            return
        title = getattr(data, "display_name", getattr(data, "name", ""))
        label = lambda field, fallback: I18N.tr(f"edit.media_pool.{field}", fallback)
        value = lambda field: str(getattr(data, field, None) or "—")
        rows = [(label("type", "Type"), label("timeline", "Timeline") if kind == "timeline" else str(data.media_type).title()),
                (label("header_resolution", "Resolution"), f"{data.width} × {data.height}" if data.width and data.height else "—"),
                (label("header_fps", "FPS"), value("fps"))]
        if kind == "asset":
            duration = data.duration_seconds
            duration_text = "—"
            if isinstance(duration, (int, float)) and math.isfinite(duration) and duration >= 0:
                duration_text = f"{int(duration // 60)}:{duration % 60:06.3f}".rstrip("0").rstrip(".")
            rows.extend([(label("header_duration", "Duration"), duration_text),
                         (label("header_codec", "Codec"), data.codec_str),
                         (label("header_audio", "Audio"), data.audio_channels_str),
                         (label("header_reel", "Reel Name"), data.reel or "—"),
                         (label("header_scene", "Scene/Shot/Take"), data.production_info_str),
                         (label("total_frames", "Frames"), value("total_frames"))])
            audio_info = data.metadata.get("audio_info", {})
            if isinstance(audio_info, dict) and audio_info.get("sample_rate"):
                rows.append((label("sample_rate", "Sample Rate"), f"{audio_info['sample_rate']} Hz"))
            rows.append((label("header_path", "File Path"), data.path))
        else:
            rows.append((label("sample_rate", "Sample Rate"), f"{data.sample_rate} Hz"))
        popup = MediaPoolInfoPopup(title, rows, self)
        self._pool_info_popup, self._pool_info_key = popup, key
        popup.destroyed.connect(lambda _obj=None, owned=popup: self._clear_pool_info_reference(owned))
        local = anchor_rect if anchor_rect is not None else view.visualRect(index)
        if anchor_rect is None:
            geometry = getattr(view.itemDelegateForIndex(index), "info_button_rect", None)
            if geometry is not None:
                local = geometry(local)
        anchor = QRect(view.viewport().mapToGlobal(local.topLeft()), local.size())
        popup.show_at(anchor, view.screen().availableGeometry())

    def _show_properties(self, asset: MediaAsset) -> None:
        """Show asset properties dialog; save editable production info on accept."""
        dialog = AssetPropertiesDialog(asset, self._get_top_level_widget())
        result = dialog.exec()
        if result != QDialog.DialogCode.Accepted:
            return
        reel, scene, shot, take = dialog.production_info_values()
        keywords, rating, flag, color_label, comment = dialog.log_field_values()

        production_changed = (
            reel != (asset.reel or "")
            or scene != (asset.scene or "")
            or shot != (asset.shot or "")
            or take != (asset.take or "")
        )
        log_changed = (
            keywords != asset.keywords
            or rating != asset.rating
            or flag != asset.flag
            or color_label != asset.color_label
            or comment != (asset.comment or "")
        )
        if not (production_changed or log_changed):
            return

        sub_commands = []
        if production_changed:
            sub_commands.append(
                UpdateAssetProductionInfoCommand.create(
                    asset.asset_id, reel, scene, shot, take
                )
            )
        if log_changed:
            sub_commands.append(
                UpdateAssetLogCommand.create(
                    asset.asset_id,
                    keywords=keywords,
                    rating=rating,
                    flag=flag,
                    color_label=color_label,
                    comment=comment,
                )
            )
        if len(sub_commands) == 1:
            cmd: Any = sub_commands[0]
        else:
            cmd = ProjectMacroCommand.create(
                I18N.tr("edit.media_pool.edit_asset_info", "Edit Asset Info"),
                sub_commands,
            )
        self._project = self._command_stack.execute(cmd, self._project)
        self.project_changed.emit(self._project)
        self._refresh_tree()

    def _append_multicam_group_action(self, menu: QMenu, batch_ids: list) -> None:
        """P1-01：多选含 ≥2 个视频素材时提供"创建多机位组"。"""
        if self._project is None:
            return
        video_ids = [
            aid for aid in batch_ids
            if getattr(self._project.assets.get(aid), "media_type", "") == "video"
        ]
        if len(video_ids) < 2:
            return
        act = menu.addAction(
            I18N.tr("edit.media_pool.create_multicam_group", "Create Multicam Group")
        )
        act.setToolTip(I18N.tr(
            "edit.media_pool.create_multicam_group_tip",
            "把所选视频组成多机位组并在 playhead 插入多机位片段；"
            "回放中按 1-9 实时切换角度（可撤销）",
        ))
        act.triggered.connect(
            lambda _checked=False, ids=tuple(video_ids):
                self.create_multicam_group_requested.emit(list(ids))
        )
        menu.addSeparator()

    def _on_batch_interpret_fps(self, asset_ids: list) -> None:
        """F2.7：批量帧率解释覆盖（单事务 + 逐项失败报告 + 代理失效）。

        原始探测 metadata 不改写；覆盖经 UpdateAssetInterpretationCommand
        组成 ProjectMacroCommand 单步撤销；缺失素材逐项报告不入事务。
        """
        from PyQt6.QtWidgets import QInputDialog

        from src.features.edit.core.commands import (
            ProjectMacroCommand,
            UpdateAssetInterpretationCommand,
        )
        from src.features.edit.core.interpretation import (
            InterpretationOverride,
            effective_asset_fps,
        )
        if not self._project or not asset_ids:
            return
        first = self._project.assets.get(asset_ids[0])
        default_fps = float(
            effective_asset_fps(first, self._project.interpretation_overrides)
            or 24.0) if first is not None else 24.0
        fps, ok = QInputDialog.getDouble(
            self._get_top_level_widget(),
            I18N.tr("edit.media_pool.batch_interpret_title", "批量解释帧率"),
            I18N.tr(
                "edit.media_pool.batch_interpret_prompt",
                "帧率解释（1–240，不改写原始探测；新插入与显式重锚生效）：",
            ),
            default_fps, 1.0, 240.0, 3)
        if not ok:
            return
        try:
            InterpretationOverride(fps=fps).validate()
        except ValueError as exc:
            QMessageBox.warning(
                self._get_top_level_widget(),
                I18N.tr("edit.media_pool.error", "Error"), str(exc))
            return

        missing = [aid for aid in asset_ids if aid not in self._project.assets]
        existing = [aid for aid in asset_ids if aid in self._project.assets]
        commands = [
            UpdateAssetInterpretationCommand.create(
                aid, InterpretationOverride(fps=fps),
                label="Interpret Media")
            for aid in existing
        ]
        if commands:
            try:
                new_project = self._command_stack.execute(
                    ProjectMacroCommand.create(
                        "批量解释帧率", commands), self._project)
            except ValueError as exc:
                QMessageBox.warning(
                    self._get_top_level_widget(),
                    I18N.tr("edit.media_pool.error", "Error"), str(exc))
                return
            self._project = new_project
            self.project_changed.emit(new_project)
            for aid in existing:
                self._invalidate_proxy_job(aid)
        summary = I18N.tr(
            "edit.media_pool.batch_interpret_done",
            "已解释 {n} 个素材为 {fps} fps（代理已失效待重生成）",
        ).format(n=len(existing), fps=fps)
        if missing:
            summary += "；" + I18N.tr(
                "edit.media_pool.batch_missing",
                "失败（素材不存在）：{ids}").format(
                ids=", ".join(missing))
        self._update_status_label()
        self.interpretation_applied.emit(tuple(existing), fps)
        QMessageBox.information(
            self._get_top_level_widget(),
            I18N.tr("edit.media_pool.batch_interpret_title", "批量解释帧率"),
            summary)

    def _on_batch_edit_log(self, asset_ids: list[str]) -> None:
        """P1-12 收尾：对多选素材批量应用日志字段（单条可撤销命令）。"""
        if self._project is None:
            return
        ids = tuple(
            aid for aid in asset_ids if aid in self._project.assets
        )
        if not ids:
            return
        dialog = BatchAssetLogDialog(len(ids), self._get_top_level_widget())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        params = dialog.batch_params()
        # AUD-20204: rating 0（"未评级"）是合法操作。只有当所有字段都取
        # "不变" 语义值（None / 空元组 / 空串）时才视为无操作——原来的
        # `not any(params.values())` 会把仅清除评级的提交静默丢弃。
        if all(value in (None, (), "") for value in params.values()):
            return
        cmd = BatchUpdateAssetLogCommand.create(
            ids,
            label=I18N.tr("edit.media_pool.batch_edit_log", "Batch Edit Asset Log"),
            **params,
        )
        self._project = self._command_stack.execute(cmd, self._project)
        self.project_changed.emit(self._project)
        self._refresh_tree()
        QMessageBox.information(
            self._get_top_level_widget(),
            I18N.tr("edit.media_pool.batch_edit_log_title", "批量标记"),
            I18N.tr(
                "edit.media_pool.batch_edit_log_done", "已更新 {n} 个素材"
            ).format(n=len(ids)),
        )

    def _on_proxy_policy_settings(self) -> None:
        """P1-14: 打开项目级代理策略设置（命令可撤销 + 服务配置桥接）。"""
        if self._project is None:
            return
        dialog = ProxyPolicyDialog(
            self._project.get_proxy_policy(), self._get_top_level_widget()
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        policy = dialog.policy_values()
        cmd = UpdateProxyPolicyCommand.create(policy)
        self._project = self._command_stack.execute(cmd, self._project)
        self.project_changed.emit(self._project)
        # 桥接磁盘代理服务目标分辨率（配置参与代理 key，变化后按需重建）
        try:
            from src.shared.media.disk_proxy_service import get_disk_proxy_service
            from src.shared.media.proxy import DiskProxyConfig

            service = get_disk_proxy_service()
            default = getattr(service, "_config", None)
            service.update_config(
                DiskProxyConfig(
                    format=getattr(default, "format", None),
                    resolution=getattr(default, "resolution", None),
                    prores_profile=getattr(default, "prores_profile", "proxy"),
                    target_height=policy.target_height,
                )
            )
        except Exception as e:  # noqa: BLE001 — 策略保存不应因服务桥接失败回滚
            logger.warning(f"apply proxy policy to disk service failed: {e}")

    def _show_bin_properties(self, bin_id: str) -> None:
        """Show a read-only properties dialog for a bin."""
        if not self._project or bin_id not in self._project.bins:
            return
        dialog = BinPropertiesDialog(
            self._project.bins[bin_id], self._project, self._get_top_level_widget()
        )
        dialog.exec()

    def _show_quick_look(self) -> None:
        """Toggle the Quick Look floating preview for the selected asset.

        Finder-style: press Space on a selected asset to pop a large preview;
        press Space again (or Esc) to close.
        """
        if not self._project:
            return
        # AUD-20200: toggle via the panel-tracked instance (Space closes the
        # open Quick Look, Finder-style). The old findChild() lookup searched
        # only this panel's subtree while the dialog's parent is the top-level
        # window, so it never found the open dialog — every Space press leaked
        # another QuickLookDialog.
        existing = self._quick_look_dialog
        if existing is not None:
            self._quick_look_dialog = None
            was_visible = False
            try:
                was_visible = existing.isVisible()
                existing.close()
                existing.deleteLater()
            except RuntimeError:
                pass  # underlying C++ object already destroyed elsewhere
            if was_visible:
                return
        asset_id = None
        if self._view_mode != ViewMode.LIST and self.grid_view is not None:
            sel = self.grid_view.selectionModel()
            if sel is not None:
                for idx in sel.selectedRows():
                    if idx.data(AssetListModel.ITEM_TYPE_ROLE) == "asset":
                        asset_id = idx.data(AssetListModel.ITEM_ID_ROLE)
                        break
        elif self.tree is not None:
            for item in self.tree.selectedItems():
                if item.data(0, Qt.ItemDataRole.UserRole + 1) == "asset":
                    asset_id = item.data(0, Qt.ItemDataRole.UserRole)
                    break
        if not asset_id:
            return
        asset = self._project.assets.get(asset_id)
        if asset is None:
            return
        dlg = QuickLookDialog(asset, self._get_top_level_widget())
        dlg.destroyed.connect(self._on_quick_look_destroyed)
        dlg.show()
        self._quick_look_dialog = dlg

    def _on_quick_look_destroyed(self, _obj: object = None) -> None:
        """Drop the Quick Look reference when the dialog is destroyed."""
        self._quick_look_dialog = None

    def _on_new_bin_clicked(self) -> None:
        """Handle New Bin button click."""
        if not self._project:
            QMessageBox.warning(
                self._get_top_level_widget(),
                I18N.tr("edit.media_pool.no_project_title", "No Project"),
                I18N.tr("edit.media_pool.no_project_message", "Please create or open a project first.")
            )
            return

        self._create_bin()

    def _create_bin(self, parent_id: Optional[str] = None) -> None:
        """Create a new bin in-place (Finder-style, no dialog).

        Generates a unique default name, creates the bin via the command
        stack, then drops the new bin straight into inline-edit mode so the
        user can type the real name immediately.
        """
        if not self._project:
            return

        base_name = I18N.tr("edit.media_pool.new_bin_default", "New Bin")
        name = self._next_unique_bin_name(base_name, parent_id)

        command = CreateBinCommand.create(name, parent_id)
        self._project = self._command_stack.execute(command, self._project)

        # Refresh so the new bin shows up, then enter inline edit on it.
        self._refresh_bin_tree()
        self._refresh_tree()

        new_bin_id = getattr(command, "_created_bin_id", None)
        if new_bin_id:
            self._start_rename_bin(new_bin_id)

        self.project_changed.emit(self._project)

    def _next_unique_bin_name(self, base: str, parent_id: Optional[str] = None) -> str:
        """Return a bin name not yet used among sibling bins.

        Finder-style: ``base``, ``base 2``, ``base 3`` … skipping names that
        already exist among bins that share ``parent_id`` (root level when
        ``parent_id`` is None).
        """
        if not self._project:
            return base

        existing: set[str] = set()
        root_bin_id = self._project.root_bin_id
        for bin_obj in self._project.bins.values():
            if bin_obj.bin_id == root_bin_id:
                continue
            # Treat parent_id None as "top level" (matches root_bin_id too).
            b_parent = bin_obj.parent_id
            norm_parent = b_parent if b_parent != root_bin_id else None
            norm_target = parent_id if parent_id != root_bin_id else None
            if (parent_id is None and norm_parent is None) or norm_parent == norm_target:
                existing.add(bin_obj.name)

        if base not in existing:
            return base
        n = 2
        while f"{base} {n}" in existing:
            n += 1
        return f"{base} {n}"

    def _apply_bin_rename(self, bin_id: str, new_name: str) -> bool:
        """Apply a bin rename through the command stack (Undo support).

        Central entry point used by all three views' inline editors. Empty /
        whitespace-only names and no-op renames are silently ignored (matching
        Finder: Escape or clearing the field cancels the edit).

        Returns:
            ``True`` if the rename was actually performed.
        """
        if not self._project:
            return False
        name = new_name.strip()
        if not name:
            return False
        bin_obj = self._project.bins.get(bin_id)
        if bin_obj is None:
            return False
        if name == bin_obj.name:
            return False

        command = RenameBinCommand.create(bin_id, name)
        self._project = self._command_stack.execute(command, self._project)
        self._refresh_bin_tree()
        self._refresh_tree()
        self.project_changed.emit(self._project)
        return True

    def _apply_asset_rename(self, asset_id: str, new_name: str) -> bool:
        """Apply an asset display-name rename through the command stack.

        Mirrors ``_apply_bin_rename`` for assets. Empty/whitespace-only and
        no-op renames are silently ignored (Finder behavior).

        Returns:
            ``True`` if the rename was actually performed.
        """
        if not self._project:
            return False
        name = new_name.strip()
        if not name:
            return False
        asset = self._project.assets.get(asset_id)
        if asset is None:
            return False
        if name == asset.display_name:
            return False

        command = RenameAssetCommand.create(asset_id, name)
        self._project = self._command_stack.execute(command, self._project)
        self._refresh_tree()
        self.project_changed.emit(self._project)
        return True

    # ==================== Clipboard (copy/cut/paste/duplicate) ====================

    def _collect_selected_asset_ids(self) -> List[str]:
        """Return asset ids selected in the currently active view."""
        ids: List[str] = []
        if self._view_mode != ViewMode.LIST and self.grid_view is not None:
            sel_model = self.grid_view.selectionModel()
            if sel_model is not None:
                for idx in sel_model.selectedRows():
                    if idx.data(AssetListModel.ITEM_TYPE_ROLE) == "asset":
                        aid = idx.data(AssetListModel.ITEM_ID_ROLE)
                        if aid:
                            ids.append(aid)
        elif self.tree is not None:
            for item in self.tree.selectedItems():
                if item.data(0, Qt.ItemDataRole.UserRole + 1) == "asset":
                    aid = item.data(0, Qt.ItemDataRole.UserRole)
                    if aid:
                        ids.append(aid)
        return ids

    def get_selected_asset_ids(self) -> tuple[str, ...]:
        """公共选择访问器（P1：供 AiEditHostBridge.get_selected_asset_ids 用）。

        返回当前选中素材 ID 的 tuple（与 InvocationScope.selected_asset_ids 契约一致）。
        无选择返回空 tuple。
        """
        return tuple(self._collect_selected_asset_ids())

    def get_ai_attachment_candidates(self) -> tuple[dict[str, str], ...]:
        """Return current media-pool files for the AI Assistant ``@`` picker.

        This is a read-only snapshot.  Paths are intentionally kept in the
        payload so the assistant can reuse the exact project asset when a user
        selects it or drags it into the prompt; no duplicate copy is created.
        Virtual Motion Graphic assets are included even though their ``topos://``
        path is not a file upload.  The assistant uses their stable asset ID as
        an explicit edit target and never tries to upload the URI.
        """
        project = self._project
        assets = getattr(project, "assets", {}) if project is not None else {}
        candidates: list[dict[str, str]] = []
        for asset in assets.values():
            path = str(getattr(asset, "path", "") or "")
            media_type = str(getattr(asset, "media_type", "file") or "file")
            if not path and media_type != "motion_graphic":
                continue
            candidates.append({
                "asset_id": str(getattr(asset, "asset_id", "") or ""),
                "path": path,
                "display_name": str(
                    getattr(asset, "display_name", "")
                    or os.path.basename(path)
                ),
                "media_type": media_type,
            })
        return tuple(candidates)

    def _copy_selected_assets(self) -> None:
        """Cmd+C — copy selected asset ids to the internal clipboard."""
        ids = self._collect_selected_asset_ids()
        if ids:
            self._clipboard_asset_ids = ids
            self._clipboard_is_cut = False

    def _cut_selected_assets(self) -> None:
        """Cmd+X — cut selected asset ids (deferred removal on paste)."""
        ids = self._collect_selected_asset_ids()
        if ids:
            self._clipboard_asset_ids = ids
            self._clipboard_is_cut = True

    def _paste_assets(self) -> None:
        """Cmd+V — paste clipboard assets into the current bin.

        Copy semantics: duplicate each clipboard asset (new id, shared file).
        Cut semantics: move the originals out of their bins into the current
        bin (Finder "move" on paste).
        """
        if not self._project or not self._clipboard_asset_ids:
            return
        target_bin_id = self._current_bin_id or self._project.root_bin_id
        if target_bin_id not in self._project.bins:
            return
        sub_commands: list[Any] = []
        for aid in self._clipboard_asset_ids:
            if aid not in self._project.assets:
                continue
            if self._clipboard_is_cut:
                # Move original into target bin (add reference).
                sub_commands.append(MoveAssetToBinCommand.create(aid, target_bin_id))
            else:
                sub_commands.append(
                    DuplicateAssetCommand.create(aid, target_bin_id)
                )
        if not sub_commands:
            return
        macro = ProjectMacroCommand.create("Paste assets", sub_commands)
        try:
            self._project = self._command_stack.execute(macro, self._project)
        except ValueError as e:
            logger.warning(f"Paste failed: {e}")
            return
        # After a cut-paste, clear the clipboard (consumed).
        if self._clipboard_is_cut:
            self._clipboard_asset_ids = []
            self._clipboard_is_cut = False
        self.project_changed.emit(self._project)
        self._refresh_tree(refresh_bins=True)

    def _duplicate_selected_assets(self) -> None:
        """Cmd+D — duplicate each selected asset in place (new id)."""
        ids = self._collect_selected_asset_ids()
        if not ids or not self._project:
            return
        target_bin_id = self._current_bin_id or self._project.root_bin_id
        if target_bin_id not in self._project.bins:
            return
        sub_commands = [
            DuplicateAssetCommand.create(aid, target_bin_id) for aid in ids
        ]
        macro = ProjectMacroCommand.create("Duplicate assets", sub_commands)
        try:
            self._project = self._command_stack.execute(macro, self._project)
        except ValueError as e:
            logger.warning(f"Duplicate failed: {e}")
            return
        self.project_changed.emit(self._project)
        self._refresh_tree(refresh_bins=True)

    def _start_rename_asset(self, asset_id: str) -> None:
        """Enter inline-edit mode on an asset (Finder-style rename)."""
        if not self._project or not asset_id:
            return
        if self._view_mode != ViewMode.LIST and self.grid_view is not None:
            self._start_grid_asset_rename(asset_id)
        elif self.tree is not None:
            self._start_tree_asset_rename(asset_id)

    def _start_tree_asset_rename(self, asset_id: str) -> None:
        """Find the asset row in the right-side tree view and edit it."""
        if self.tree is None:
            return
        iterator = QTreeWidgetItemIterator(self.tree)
        while iterator.value() is not None:
            item = iterator.value()
            if (
                item.data(0, Qt.ItemDataRole.UserRole + 1) == "asset"
                and item.data(0, Qt.ItemDataRole.UserRole) == asset_id
            ):
                self.tree.setCurrentItem(item)
                self.tree.editItem(item, 0)
                return
            iterator += 1

    def _start_grid_asset_rename(self, asset_id: str) -> None:
        """Find the asset card in the grid view and edit it."""
        if self.grid_view is None or self.asset_model is None:
            return
        row = self.asset_model.row_for_asset(asset_id)
        if row is not None and row >= 0:
            index = self.asset_model.index(row, 0)
            if index.isValid():
                self.grid_view.setCurrentIndex(index)
                self.grid_view.edit(index)

    def _start_rename_bin(self, bin_id: str) -> None:
        """Enter inline-edit mode on a bin (Finder-style rename).

        Operates on whichever view is currently relevant: the active
        right-side view (list or grid), and—if visible—the left sidebar.
        """
        if not self._project or not bin_id:
            return

        # Right-side views: only the visible one needs editing.
        if self._view_mode != ViewMode.LIST and self.grid_view is not None:
            self._start_grid_bin_rename(bin_id)
        elif self.tree is not None:
            self._start_tree_bin_rename(bin_id)

        # Left sidebar (if visible).
        if self.bin_tree is not None and self.bin_tree.isVisible():
            self.bin_tree.start_inline_rename(bin_id)

    def _start_tree_bin_rename(self, bin_id: str) -> None:
        """Find the bin row in the right-side tree view and edit it."""
        if self.tree is None:
            return
        iterator = QTreeWidgetItemIterator(self.tree)
        while iterator.value() is not None:
            item = iterator.value()
            if (
                item.data(0, Qt.ItemDataRole.UserRole + 1) == "bin"
                and item.data(0, Qt.ItemDataRole.UserRole) == bin_id
            ):
                self.tree.setCurrentItem(item)
                self.tree.editItem(item, 0)
                return
            iterator += 1

    def _start_grid_bin_rename(self, bin_id: str) -> None:
        """Find the bin card in the grid view and edit it."""
        if self.grid_view is None or self.asset_model is None:
            return
        row_count = self.asset_model.rowCount()
        for row in range(row_count):
            index = self.asset_model.index(row, 0)
            if (
                self.asset_model.data(index, AssetListModel.ITEM_TYPE_ROLE) == "bin"
                and self.asset_model.data(index, AssetListModel.ITEM_ID_ROLE) == bin_id
            ):
                self.grid_view.setCurrentIndex(index)
                view = self.metadata_shelf if (self._view_mode == ViewMode.METADATA and index.data(AssetListModel.ITEM_TYPE_ROLE) != "asset") else self.grid_view
                view.edit(index)
                return

    # ==================== Drop-into-Bin ====================

    def _set_drop_target_bin(self, bin_id: Optional[str]) -> None:
        """Track the bin currently hovered during an internal drag.

        The grid delegate and tree view read ``_drop_target_bin_id`` to paint
        a highlight. Emit a dataChanged on the grid so the delegate repaints.
        """
        if self._drop_target_bin_id == bin_id:
            return
        self._drop_target_bin_id = bin_id
        # Repaint the grid so bin cards update their drop highlight.
        if self.grid_view is not None and self.asset_model is not None:
            top = self.asset_model.index(0, 0)
            bottom = self.asset_model.index(
                max(self.asset_model.rowCount() - 1, 0), 0
            )
            if top.isValid() and bottom.isValid():
                self.asset_model.dataChanged.emit(top, bottom)

    def _handle_drop_bin_into_bin(
        self, target_bin_id: str, bin_ids: List[str]
    ) -> bool:
        """Reparent dragged bins into ``target_bin_id`` (bin-into-bin).

        ``target_bin_id`` may be the root bin id (move to root level) or a
        real bin. Cycle protection is enforced inside ``reparent_bin``.

        Returns ``True`` if at least one bin was moved.
        """
        if not self._project or not bin_ids:
            return False
        moved = False
        for bid in bin_ids:
            if bid == target_bin_id:
                continue  # Cannot move a bin into itself.
            if bid not in self._project.bins:
                continue
            if target_bin_id != self._project.root_bin_id and target_bin_id not in self._project.bins:
                continue
            cmd = MoveBinCommand.create(bid, target_bin_id)
            try:
                self._project = self._command_stack.execute(cmd, self._project)
                moved = True
            except ValueError as e:
                # Cycle or invalid reparent — skip this bin.
                logger.warning(f"Move bin {bid} into {target_bin_id} rejected: {e}")
        if moved:
            self.project_changed.emit(self._project)
            self._refresh_bin_tree()
            self._refresh_tree()
        return moved

    def _unfile_items(
        self, asset_ids: List[str], timeline_ids: List[str]
    ) -> bool:
        """Move the given items back to Master（未归类）——移出全部普通箱。

        P0-5 移动语义：拖到 root/Master 节点是"移动到未归类"（exclusive），
        不是只从当前箱拿掉一个引用。素材/时间线本体保留，undo 一次恢复
        全部跨箱归属。
        """
        if not self._project:
            return False
        sub_commands: list[Any] = []
        for aid in asset_ids:
            if aid not in self._project.assets:
                continue
            # root 是隐式容器不算"归类"：只有用户箱引用才需要 unfile
            if any(
                aid in b.asset_ids
                for bid, b in self._project.bins.items()
                if bid != self._project.root_bin_id
            ):
                sub_commands.append(UnfileAssetCommand.create(aid))
        for tid in timeline_ids:
            if tid not in self._project.timelines:
                continue
            if any(
                tid in b.timeline_ids
                for bid, b in self._project.bins.items()
                if bid != self._project.root_bin_id
            ):
                sub_commands.append(UnfileTimelineCommand.create(tid))
        if not sub_commands:
            return False
        macro = ProjectMacroCommand.create("Move to Master", sub_commands)
        try:
            self._project = self._command_stack.execute(macro, self._project)
        except ValueError as e:
            logger.warning(f"Move to Master failed: {e}")
            return False
        self.project_changed.emit(self._project)
        self._refresh_tree(refresh_bins=True)
        return True

    def _handle_drop_into_bin(
        self,
        target_bin_id: str,
        asset_ids: List[str],
        timeline_ids: List[str],
        copy: bool = False,
    ) -> bool:
        """Move/copy the given assets/timelines into a bin (drag-and-drop).

        Called by ``AssetListView.dropEvent``, ``AssetTreeWidget.dropEvent``
        and ``BinTreeWidget.dropEvent``. Builds a single macro command so the
        whole drag is one undo unit.

        P0-5 移动语义（对齐 Resolve 呈现；存储保持引用式、旧工程零迁移）：
        - ``copy=False``（普通拖拽）＝移动：目标箱成为唯一普通箱归属，
          其它箱移除引用（MoveAssetBetweenBinsCommand，undo 恢复完整
          跨箱归属映射）。
        - ``copy=True``（Alt/Option 拖拽）＝拷贝引用：仅向目标箱加引用，
          可多箱共存（MoveAssetToBinCommand，即引用式加法）。
        - 拖到 root/Master＝移回未归类：移出全部普通箱（Unfile*Command）。
        - 素材实体复制不在拖拽路径：右键菜单 Duplicate / Cmd+D。

        Args:
            target_bin_id: Destination bin ID. Root bin = move back to
                Master (un-file from every regular bin).
            asset_ids: Asset IDs to move/copy.
            timeline_ids: Timeline IDs to move/copy.
            copy: ``True`` = add references (Alt-drop); ``False`` = move.

        Returns:
            ``True`` if at least one item was moved/copied.
        """
        if not self._project:
            return False
        if not asset_ids and not timeline_ids:
            return False
        # Dropping onto the root bin means "move back to Master" — remove
        # the items from every regular bin (they stay in the project).
        if target_bin_id == self._project.root_bin_id:
            return self._unfile_items(asset_ids, timeline_ids)
        target_bin = self._project.bins.get(target_bin_id)
        if target_bin is None:
            return False

        sub_commands: list[Any] = []
        new_asset_ids: List[str] = []
        new_timeline_ids: List[str] = []
        if copy:
            # Alt = 拷贝引用：仅加引用，可多箱共存（幂等过滤已引用项）。
            for aid in asset_ids:
                if (aid in self._project.assets
                        and aid not in target_bin.asset_ids):
                    new_asset_ids.append(aid)
                    sub_commands.append(
                        MoveAssetToBinCommand.create(aid, target_bin_id))
            for tid in timeline_ids:
                if (tid in self._project.timelines
                        and tid not in target_bin.timeline_ids):
                    new_timeline_ids.append(tid)
                    sub_commands.append(
                        MoveTimelineToBinCommand.create(tid, target_bin_id))
        else:
            # 普通拖拽 = 移动（exclusive）：目标箱成为唯一普通箱归属。
            # 已"独占在目标箱"的素材跳过（幂等）。
            for aid in asset_ids:
                if aid not in self._project.assets:
                    continue
                in_target = aid in target_bin.asset_ids
                in_others = any(
                    aid in b.asset_ids
                    for bid, b in self._project.bins.items()
                    if bid not in (target_bin_id, self._project.root_bin_id))
                if in_target and not in_others:
                    continue
                new_asset_ids.append(aid)
                sub_commands.append(
                    MoveAssetBetweenBinsCommand.create(aid, target_bin_id))
            for tid in timeline_ids:
                if tid not in self._project.timelines:
                    continue
                in_target = tid in target_bin.timeline_ids
                in_others = any(
                    tid in b.timeline_ids
                    for bid, b in self._project.bins.items()
                    if bid not in (target_bin_id, self._project.root_bin_id))
                if in_target and not in_others:
                    continue
                new_timeline_ids.append(tid)
                sub_commands.append(
                    MoveTimelineBetweenBinsCommand.create(tid, target_bin_id))
        if not sub_commands:
            return False

        # Compose label
        parts: list[str] = []
        if new_asset_ids:
            parts.append(
                I18N.tr(
                    "edit.media_pool.drop_assets_count",
                    "{count} asset(s)",
                    count=len(new_asset_ids),
                )
            )
        if new_timeline_ids:
            parts.append(
                I18N.tr(
                    "edit.media_pool.drop_timelines_count",
                    "{count} timeline(s)",
                    count=len(new_timeline_ids),
                )
            )
        label = I18N.tr(
            "edit.media_pool.drop_into_bin",
            "Drop {items} into '{bin}'",
            items=", ".join(parts),
            bin=target_bin.name,
        )
        macro = ProjectMacroCommand.create(label, sub_commands)

        try:
            new_project = self._command_stack.execute(macro, self._project)
        except ValueError as e:
            logger.warning(f"Drop into bin failed: {e}")
            return False

        self._project = new_project
        self.project_changed.emit(new_project)
        self._refresh_tree(refresh_bins=True)
        self._update_status_label()
        logger.info(
            f"Dropped {len(new_asset_ids)} asset(s) + "
            f"{len(new_timeline_ids)} timeline(s) into bin {target_bin_id}"
        )
        return True

    def _target_bin_at(self, pos: QPoint, source: str | QListView) -> Optional[str]:
        """Resolve the bin id under a drag/drop position, if any.

        Args:
            pos: Position in the coordinate space of the view identified by
                ``source`` (``"grid"``, ``"tree"`` or ``"sidebar"``).
            source: Which view the position is relative to.

        Returns:
            The bin id under the cursor if it points at a bin item, else None.
        """
        if source == "grid" or isinstance(source, QListView):
            if self.grid_view is None or self.asset_model is None:
                return None
            view = self.grid_view if source == "grid" else source
            index = view.indexAt(pos)
            if not index.isValid():
                return None
            if self.asset_model.data(index, AssetListModel.ITEM_TYPE_ROLE) != "bin":
                return None
            return self.asset_model.data(index, AssetListModel.ITEM_ID_ROLE)
        if source == "tree":
            item = self.tree.itemAt(pos)
            if item is None:
                return None
            if item.data(0, Qt.ItemDataRole.UserRole + 1) != "bin":
                return None
            return item.data(0, Qt.ItemDataRole.UserRole)
        if source == "sidebar":
            item = self.bin_tree.itemAt(pos)
            if item is None:
                return None
            bin_type = item.data(0, BinTreeWidget.BIN_TYPE_ROLE)
            bin_id = item.data(0, BinTreeWidget.BIN_ID_ROLE)
            # Only real bin nodes are drop targets. The root bin node (now shown
            # directly, without a "Bins" section wrapper) resolves to the
            # project root — meaning "remove from the current bin".
            if bin_type != "bin":
                return None
            return bin_id
        return None

    # ==================== Relink Actions ====================

    def _collect_timeline_sequences(self) -> list[Any]:
        """同步抓取当前工程全部时间线序列（供使用位置查询）。

        通过 timeline_command_snapshot_requested 信号让控制器回填；
        无连接（如测试环境）时返回空列表。
        """
        if not self._project or not self._project.timelines:
            return []
        captured: dict[str, Any] = {}
        try:
            self.timeline_command_snapshot_requested.emit(
                list(self._project.timelines.keys()), captured.update
            )
        except Exception:
            logger.exception("Failed to request timeline snapshots")
        return [seq for seq in captured.values() if seq is not None]

    def _on_replace_media(self, asset_id: str) -> None:
        """替换素材源文件（P1-13）：asset_id 不变，剪辑参数全部保留。

        与 Relink 的区别：Replace 是有意的换源（旧代理失效清除、
        文件名跟随新文件），并在确认对话框中列出当前使用位置。
        """
        if not self._project:
            return
        asset = self._project.assets.get(asset_id)
        if not asset:
            return

        from copy import deepcopy
        original_asset = deepcopy(asset)
        project_id = self._project.project_id
        generation = int(self.__dict__.get("_proxy_session_generation", 0))
        original_dir = os.path.dirname(asset.path)
        if not os.path.isdir(original_dir):
            original_dir = os.path.expanduser("~")

        new_path, _ = QFileDialog.getOpenFileName(
            self._get_top_level_widget(),
            I18N.tr(
                "edit.media_pool.replace_media_title",
                "替换素材源: {name}",
                name=asset.display_name,
            ),
            original_dir,
            I18N.tr("edit.media_pool.filter_all",
                    "All media files (*.mp4 *.mov *.avi *.mkv *.jpg *.png *.tif *.exr *.mp3 *.wav *.aac)")
            + ";;" +
            I18N.tr("edit.media_pool.filter_video", "Video files (*.mp4 *.mov *.avi *.mkv)")
            + ";;" +
            I18N.tr("edit.media_pool.filter_image", "Image files (*.jpg *.png *.tif *.exr)")
            + ";;" +
            I18N.tr("edit.media_pool.filter_audio", "Audio files (*.mp3 *.wav *.aac)")
        )
        if not new_path or new_path == asset.path:
            return

        # 使用位置确认（有时间线快照来源时展示具体位置）
        locations = []
        try:
            sequences = self._collect_timeline_sequences()
            index = build_asset_usage_index(sequences, self._project) if sequences else None
            if index is not None:
                locations = list(index.locations_for(asset_id))
        except Exception:
            index = None
        if index is not None:
            locations = index.locations_for(asset_id)
            usage_lines = "\n".join(f"  · {loc.format()}" for loc in locations[:12])
            if len(locations) > 12:
                usage_lines += f"\n  · … (+{len(locations) - 12})"
            usage_section = (
                I18N.tr(
                    "edit.media_pool.replace_usage_header",
                    "该素材当前被 {count} 处剪辑引用（替换后入出点/特效保持不变）：",
                    count=len(locations),
                )
                + "\n" + (usage_lines or "  -")
                if locations
                else I18N.tr(
                    "edit.media_pool.replace_no_usage",
                    "该素材当前未被任何时间线使用。"
                )
            )
        else:
            usage_section = I18N.tr(
                "edit.media_pool.replace_usage_unknown",
                "（无法获取时间线快照，跳过使用位置检查）"
            )

        answer = QMessageBox.question(
            self._get_top_level_widget(),
            I18N.tr("edit.media_pool.replace_confirm_title", "替换素材源"),
            I18N.tr(
                "edit.media_pool.replace_confirm_body",
                "将 {old} 替换为:\n{new}\n\n{usage}\n\n此操作可通过撤销恢复。",
                old=asset.display_name,
                new=os.path.basename(new_path),
                usage=usage_section,
            ),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        # Both dialogs run event loops. Do not register a request against a
        # different project/source that became current while either was open.
        current_asset = self._project.assets.get(asset_id) if self._project else None
        if (self._project is None or self._project.project_id != project_id
                or generation != int(self.__dict__.get("_proxy_session_generation", 0))
                or current_asset is None or vars(current_asset) != vars(original_asset)):
            return

        # Decoder probing can wait on slow disks or external tools. Only a
        # request token is registered here; the worker never touches the model.
        request_id = uuid.uuid4().hex
        # Frozen assets still contain mutable feature dictionaries. Retain an
        # independent payload snapshot so an in-place metadata edit cannot
        # make both sides of the completion identity guard change together.
        self._replacement_requests[asset_id] = (
            request_id, project_id, original_asset, new_path, generation)
        worker = ReplacementProbeWorker(asset_id, request_id, new_path)
        worker.signals.finished.connect(self._on_replacement_probed)
        self._replacement_workers[request_id] = worker
        QThreadPool.globalInstance().start(worker)

    def _on_replacement_probed(
        self, asset_id: str, request_id: str, result: object,
    ) -> None:
        """Commit a successful, current request after checking every live usage."""
        self._replacement_workers.pop(request_id, None)
        pending = self._replacement_requests.get(asset_id)
        if pending is None or pending[0] != request_id:
            return
        _token, project_id, original_asset, new_path, generation = pending
        self._replacement_requests.pop(asset_id, None)
        if (self._project is None or self._project.project_id != project_id
                or generation != int(self.__dict__.get("_proxy_session_generation", 0))
                or self._project.assets.get(asset_id) is None
                or vars(self._project.assets[asset_id]) != vars(original_asset)):
            logger.info("Discarded stale replacement probe for %s", asset_id)
            return
        media_type, probed_metadata, error = result[:3]
        signature = result[3] if len(result) > 3 else None
        if signature is not None:
            try:
                current = os.stat(new_path)
                if (current.st_size, current.st_mtime_ns) != signature:
                    error = "探测完成后新源文件发生变化，请重试替换"
            except OSError as exc:
                error = str(exc)
        if error or not media_type or not probed_metadata:
            self._show_replacement_error(new_path, str(error or "unknown format"))
            return
        # Re-read snapshots after the worker: edits made while probing must also
        # pass preflight. A partial snapshot must never imply an unused source.
        sequences = self._collect_timeline_sequences()
        expected = set(self._project.timelines)
        captured = {seq.timeline_id for seq in sequences}
        try:
            if expected - captured:
                raise ValueError("无法获取全部时间线，请重试替换以核验所有剪辑的源范围。")
            validate_replacement_usages(
                original_asset, media_type, probed_metadata, sequences)
            merged_metadata = merge_replacement_metadata(
                original_asset.metadata or {}, probed_metadata)
            color_metadata = _extract_color_metadata(probed_metadata) or {}
            cmd = ReplaceMediaCommand.create(
                asset_id, new_path, new_metadata=merged_metadata,
                new_media_type=media_type, color_metadata=color_metadata)
            self._project = self._command_stack.execute(cmd, self._project)
        except ValueError as exc:
            self._show_replacement_error(new_path, str(exc))
            return
        self._invalidate_proxy_job(asset_id)
        self.project_changed.emit(self._project)
        self._refresh_tree()
        logger.info("Replace media committed: %s -> %s", original_asset.path, new_path)

    def _show_replacement_error(self, path: str, reason: str) -> None:
        """Explain a failed preflight while leaving the original asset intact."""
        QMessageBox.warning(
            self._get_top_level_widget(),
            I18N.tr("edit.media_pool.error", "Error"),
            I18N.tr(
                "edit.media_pool.replace_probe_failed",
                "无法替换新源文件，已保留原素材：\n{path}\n{err}",
                path=path, err=reason),
        )

    def _on_relink_asset(self, asset_id: str) -> None:
        """
        Handle Relink context menu action for a single offline asset.

        Opens a file dialog to select a new path, then updates the asset.

        Args:
            asset_id: ID of the offline asset to relink
        """
        if not self._project:
            return

        asset = self._project.assets.get(asset_id)
        if not asset:
            return

        # R05（媒体池复查）：模态前冻结身份（与 Replace 同一纪律）——
        # 对话框期间切到另一工程（即便含同 asset_id）或素材被并发修改，
        # 返回后不得把新路径写进别的工程/别的内容。
        from copy import deepcopy
        original_asset = deepcopy(asset)
        project_id = self._project.project_id

        # Determine starting directory
        original_dir = os.path.dirname(asset.path)
        if not os.path.isdir(original_dir):
            original_dir = os.path.expanduser("~")

        # Open file dialog
        new_path, _ = QFileDialog.getOpenFileName(
            self._get_top_level_widget(),
            I18N.tr("edit.media_pool.relink_title", "Relink: {name}", name=asset.display_name),
            original_dir,
            I18N.tr("edit.media_pool.filter_all",
                    "All media files (*.mp4 *.mov *.avi *.mkv *.jpg *.png *.tif *.exr *.mp3 *.wav *.aac)")
            + ";;" +
            I18N.tr("edit.media_pool.filter_video", "Video files (*.mp4 *.mov *.avi *.mkv)")
            + ";;" +
            I18N.tr("edit.media_pool.filter_image", "Image files (*.jpg *.png *.tif *.exr)")
            + ";;" +
            I18N.tr("edit.media_pool.filter_audio", "Audio files (*.mp3 *.wav *.aac)")
        )

        if not new_path:
            return

        # R05：模态后守卫——工程或素材已变化即丢弃本次重连意图。
        if (self._project is None
                or self._project.project_id != project_id):
            logger.info(
                "Relink dropped: project switched during dialog (%s)",
                asset_id)
            return
        current_asset = self._project.assets.get(asset_id)
        if (current_asset is None
                or vars(current_asset) != vars(original_asset)):
            logger.info(
                "Relink dropped: asset changed during dialog (%s)", asset_id)
            return

        # R05：单步命令事务——旧代理属于旧源路径，命令内一并失效；
        # Undo/Redo 恢复完整旧素材（路径/代理/离线状态）。
        self._invalidate_proxy_job(asset_id)
        try:
            cmd = RelinkAssetCommand.create(asset_id, new_path)
            new_project = self._command_stack.execute(cmd, self._project)
        except ValueError as exc:
            logger.warning(f"Relink failed for {asset_id}: {exc}")
            return
        self._project = new_project
        self.project_changed.emit(new_project)

        # Verify the new status
        updated_asset = new_project.assets.get(asset_id)
        if updated_asset and not updated_asset.is_offline:
            logger.info(f"Asset relinked successfully: {asset.display_name} -> {new_path}")
        else:
            logger.warning(f"Asset relinked but still offline: {new_path}")

        self._refresh_tree()

    def _on_relink_batch(self) -> None:
        """Handle batch relink for all offline assets using RelinkMediaDialog."""
        if not self._project:
            return

        # R05：模态前冻结工程身份（批量对话框同样在模态期运行事件循环）
        project_id = self._project.project_id
        offline_assets = [a for a in self._project.assets.values() if a.is_offline]
        if not offline_assets:
            QMessageBox.information(
                self._get_top_level_widget(),
                I18N.tr("edit.media_pool.no_offline_title", "No Offline Assets"),
                I18N.tr("edit.media_pool.no_offline_message", "All assets are online.")
            )
            return

        dialog = RelinkMediaDialog(offline_assets, self._get_top_level_widget())
        if dialog.exec() == RelinkMediaDialog.DialogCode.Accepted:
            relink_map = dialog.get_relink_map()
            if relink_map:
                # R05：模态后守卫——切工程即丢弃；素材已删除的条目剔除。
                if (self._project is None
                        or self._project.project_id != project_id):
                    logger.info(
                        "Batch relink dropped: project switched during dialog")
                    return
                valid_map = {
                    aid: path for aid, path in relink_map.items()
                    if aid in self._project.assets
                }
                if not valid_map:
                    return
                # R05：批量重连走命令事务（单步 Undo/Redo；代理失效在
                # 命令内，撤销恢复完整旧素材表）。
                for asset_id in valid_map:
                    self._invalidate_proxy_job(asset_id)
                try:
                    cmd = BatchRelinkAssetsCommand.create(valid_map)
                    new_project = self._command_stack.execute(cmd, self._project)
                except ValueError as exc:
                    logger.warning(f"Batch relink failed: {exc}")
                    return
                self._project = new_project
                self.project_changed.emit(new_project)
                self._refresh_tree()

                online_count = sum(
                    1 for aid in valid_map
                    if not new_project.assets.get(aid, MediaAsset(
                        asset_id="", path="/__invalid__", media_type="video",
                        display_name="", metadata={}
                    )).is_offline
                )
                logger.info(
                    f"Batch relink: {online_count}/{len(valid_map)} assets now online"
                )

    # ==================== Proxy Actions ====================

    def _maybe_auto_ensure_disk_proxy(self, asset: MediaAsset) -> None:
        """
        Final-6 (FINAL-P1-12): 拖入视频素材后自动触发磁盘代理生成。

        通过全局 DiskProxyService.generate_proxy 后台执行（不阻塞 UI）。
        SourceResolver 在播放时查询 proxy ready 状态：
        - ready → DISK_PROXY 主路径（realtime）
        - generating/missing → PLACEHOLDER + hold（不解 master）

        Only trigger this for online video larger than the 1080p program
        canvas. The configured proxy target is fixed at 1080p, so this never
        creates a preview below the active Full-HD timeline resolution.
        audio/image/已 ready/已 generating 的素材跳过。
        """
        try:
            source_path = getattr(asset, "path", None)
            if not source_path:
                return
            # P1-14: 策略评估（项目级阈值/模式），决策可诊断
            from src.features.edit.core.proxy_policy import (
                ProxyPolicySettings, evaluate_proxy_action,
            )
            policy = (
                self._project.get_proxy_policy()
                if self._project is not None
                else ProxyPolicySettings()
            )
            # P2-02: codec 从 metadata dict 安全提取（MediaAsset 的
            # width/height property 同样来自 metadata；MagicMock 等无
            # metadata 的对象按 unknown 处理，策略显式记 dimensions_unknown）。
            _meta = getattr(asset, "metadata", None)
            _codec = ""
            if isinstance(_meta, dict):
                _codec = str(_meta.get("codec", "") or "")
            decision = evaluate_proxy_action(
                policy,
                media_type=str(getattr(asset, "media_type", "") or ""),
                width=int(getattr(asset, "width", 0) or 0),
                height=int(getattr(asset, "height", 0) or 0),
                is_offline=bool(getattr(asset, "is_offline", False)),
                codec=_codec,
            )
            if not decision.should_generate:
                logger.debug(
                    "proxy policy skip %s: %s", source_path, decision.reason
                )
                return
            from src.shared.media.disk_proxy_service import get_disk_proxy_service
            service = get_disk_proxy_service()
            # 检查是否已 ready/generating，避免重复入队
            info = service.get_disk_proxy_info(source_path)
            if getattr(info, "is_ready", False):
                return
            status = getattr(info, "status", None)
            status_val = getattr(status, "value", str(status))
            if status_val == "generating":
                return
            # 后台触发（不阻塞）
            service.generate_proxy(
                source_path=source_path,
                asset_id=getattr(asset, "asset_id", ""),
            )
            logger.debug(f"MediaPoolPanel: auto-ensured disk proxy for {source_path}")
        except Exception as e:
            # 自动 proxy 失败不应阻塞素材添加流程
            logger.debug(f"_maybe_auto_ensure_disk_proxy failed: {e}")

    def _export_frame_to_toos(self, asset_id: str) -> None:
        """G10：单帧零再编码导出为 .toos（媒体池右键，Topos 源专用）。

        直连 ``extract_topos_frame_to_toos``——源 MOV 码流 packet 原样
        封装进 TPIM 信封（零再编码），image_profile/TMET 档位透传；
        帧间档（LP）P 帧不可独立解码，提取器显式拒绝并以对话框说明。
        """
        import os

        from PyQt6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

        from src.shared.i18n.i18n import I18N
        from src.shared.media.topos_frame_extract import (
            extract_topos_frame_to_toos,
        )

        if not self._project:
            return
        asset = self._project.assets.get(asset_id)
        if not asset or not getattr(asset, "path", ""):
            return
        metadata = getattr(asset, "metadata", {}) or {}
        try:
            total = int(metadata.get("total_frames", 0) or 0)
        except (TypeError, ValueError):
            total = 0
        frame_idx, ok = QInputDialog.getInt(
            self,
            I18N.tr("edit.media_pool.export_frame_toos_title",
                    "Export Frame as .toos"),
            I18N.tr("edit.media_pool.export_frame_toos_prompt",
                    "Frame index"),
            0, 0, max(0, total - 1) if total > 0 else 999999,
        )
        if not ok:
            return
        base = os.path.splitext(
            os.path.basename(str(asset.path)))[0]
        out_path, _ = QFileDialog.getSaveFileName(
            self,
            I18N.tr("edit.media_pool.export_frame_toos_title",
                    "Export Frame as .toos"),
            f"{base}_frame{frame_idx:04d}.toos",
            "Topos Image (*.toos)",
        )
        if not out_path:
            return
        if not out_path.lower().endswith(".toos"):
            out_path += ".toos"
        try:
            result = extract_topos_frame_to_toos(
                str(asset.path), frame_idx, out_path, overwrite=True)
        except Exception as exc:  # noqa: BLE001 —— 提取器拒绝（P 帧等）
            QMessageBox.warning(
                self,
                I18N.tr("edit.media_pool.export_frame_toos_failed",
                        "Frame Export Failed"),
                str(exc),
            )
            return
        QMessageBox.information(
            self,
            I18N.tr("edit.media_pool.export_frame_toos_done",
                    "Frame Exported"),
            I18N.tr(
                "edit.media_pool.export_frame_toos_done_detail",
                "Frame {idx} → {path}\nimage profile: {profile}, "
                "{size} bytes",
                idx=result.frame_index, path=result.output_path,
                profile=result.image_profile, size=result.file_bytes,
            ),
        )

    def _on_generate_proxy(self, asset_id: str) -> None:
        """
        Handle Generate Proxy context menu action.

        Generates a low-resolution proxy file for the asset using FFmpeg.

        Args:
            asset_id: ID of the asset to generate proxy for
        """
        if not self._project:
            return

        asset = self._project.assets.get(asset_id)
        if not asset:
            return

        # AUD-20205: has_proxy is only True once status == "ready"; while a
        # job is pending the menu keeps showing "Generate Proxy" and a second
        # click would start a concurrent ffmpeg transcode writing the same
        # proxy file. Reject re-entry while a job is known in-flight or the
        # asset's proxy status is already pending/generating.
        if asset_id in self._proxy_jobs or asset.proxy_status in (
            "pending",
            "generating",
        ):
            QMessageBox.information(
                self._get_top_level_widget(),
                I18N.tr("edit.media_pool.proxy_in_progress_title",
                        "Proxy Generation In Progress"),
                I18N.tr("edit.media_pool.proxy_in_progress_message",
                        "A proxy for '{name}' is already being generated.",
                        name=asset.display_name),
            )
            return

        # Check FFmpeg availability
        if not check_ffmpeg_available():
            QMessageBox.warning(
                self._get_top_level_widget(),
                I18N.tr("edit.media_pool.ffmpeg_not_found_title", "FFmpeg Not Found"),
                I18N.tr("edit.media_pool.ffmpeg_not_found_message",
                        "FFmpeg is required to generate proxy files. "
                        "Please install FFmpeg and try again.")
            )
            return

        # Check if asset is offline
        if asset.is_offline:
            QMessageBox.warning(
                self._get_top_level_widget(),
                I18N.tr("edit.media_pool.asset_offline_title", "Asset Offline"),
                I18N.tr("edit.media_pool.asset_offline_message",
                        "Cannot generate proxy for an offline asset. "
                        "Please relink the asset first.")
            )
            return

        # Audio files don't need proxy
        if asset.is_audio:
            QMessageBox.information(
                self._get_top_level_widget(),
                I18N.tr("edit.media_pool.no_proxy_audio_title", "Not Supported"),
                I18N.tr("edit.media_pool.no_proxy_audio_message",
                        "Proxy generation is not supported for audio files.")
            )
            return

        # A previous resolution probe must not publish while a new encode is
        # writing the same target file.
        try:
            source_stat = os.stat(asset.path)
        except OSError as exc:
            QMessageBox.warning(
                self._get_top_level_widget(),
                I18N.tr("edit.media_pool.error", "Error"), str(exc))
            return
        self._invalidate_proxy_job(asset_id)
        # Update status to pending
        new_project = update_asset_proxy(self._project, asset_id, None, "pending")
        self._project = new_project
        self.project_changed.emit(new_project)
        self._refresh_tree()
        self._proxy_jobs[asset_id] = asset.path
        self._proxy_token_counter += 1
        token = self._proxy_token_counter
        self._proxy_job_tokens[asset_id] = token
        self._proxy_job_generations[asset_id] = self._proxy_session_generation
        self.__dict__.setdefault("_proxy_job_signatures", {})[asset_id] = (
            source_stat.st_size, source_stat.st_mtime_ns)

        # R03（媒体池复查）：菜单接**统一代理服务**——队列/并发上限/
        # 磁盘预检/失败重试/编码进程取消由服务统一管理（此前直接构造
        # 旧 ProxyGeneratorWorker 绕过了预检与队列，失败还会误删既有
        # 同名代理文件）。服务回调来自后台线程，经 ProxyServiceSignals
        # 桥接回 UI 线程，沿用 token+代际+源签名守卫链。
        service = get_disk_proxy_service()
        signals = self._proxy_service_signals

        def _service_complete(source_path: str, info: Any) -> None:
            signals.finished.emit(
                asset_id, str(getattr(info, "proxy_path", "") or ""), token)

        def _service_progress(source_path: str, progress: Any) -> None:
            try:
                percent = int(getattr(progress, "percent", 0) or 0)
            except (TypeError, ValueError):
                percent = 0
            signals.progress.emit(asset_id, percent, token)

        def _service_error(source_path: str, message: str) -> None:
            signals.error.emit(asset_id, str(message), token)

        service.generate_proxy(
            source_path=asset.path,
            asset_id=asset_id,
            on_complete=_service_complete,
            on_progress=_service_progress,
            on_error=_service_error,
        )

        logger.info(f"Proxy generation started for: {asset.display_name}")

    def _on_delete_proxy(self, asset_id: str) -> None:
        """
        Handle Delete Proxy context menu action.

        Deletes the proxy file and updates the asset's proxy status.

        Args:
            asset_id: ID of the asset to delete proxy for
        """
        if not self._project:
            return

        asset = self._project.assets.get(asset_id)
        if not asset or not asset.has_proxy:
            return

        # Confirm deletion
        reply = QMessageBox.question(
            self._get_top_level_widget(),
            I18N.tr("edit.media_pool.delete_proxy_title", "Delete Proxy"),
            I18N.tr("edit.media_pool.delete_proxy_message",
                    "Are you sure you want to delete the proxy file for '{name}'?",
                    name=asset.display_name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            # Delete the proxy file from disk
            delete_proxy_file(asset)

            # Update asset proxy status
            try:
                new_project = update_asset_proxy(self._project, asset_id, None, "none")
            except ValueError as exc:
                logger.warning(f"Delete proxy skipped for missing asset {asset_id}: {exc}")
                return
            self._project = new_project
            self._invalidate_proxy_job(asset_id)
            self.project_changed.emit(new_project)
            self._refresh_tree()

            logger.info(f"Proxy deleted for: {asset.display_name}")

    def _invalidate_proxy_job(self, asset_id: str) -> None:
        """FE-B11：注销任务并令其 token 失效（晚到回调将被拒绝）。

        R03：同时取消统一代理服务上该源的在途生成（删素材/换源/重新
        入队 = 终止编码进程本身，不只丢弃回调）。
        """
        source = self._proxy_jobs.get(asset_id)
        self._proxy_jobs.pop(asset_id, None)
        self._proxy_job_tokens.pop(asset_id, None)
        self._proxy_job_generations.pop(asset_id, None)
        self.__dict__.get("_proxy_job_signatures", {}).pop(asset_id, None)
        pending = self.__dict__.get("_pending_proxy_meta", {})
        for path, meta in list(pending.items()):
            if len(meta) > 3 and meta[3] == asset_id:
                pending.pop(path, None)
        if source:
            # 轻量宿主（测试替身按本方法既有防御风格构造）无此方法时跳过
            cancel = getattr(self, "_cancel_service_proxy_generation", None)
            if callable(cancel):
                cancel(source)

    def _cancel_service_proxy_generation(self, source_path: str) -> None:
        """R03：取消统一代理服务上该源的在途任务（容错——服务不可用
        或任务已终态时静默）。"""
        try:
            get_disk_proxy_service().cancel_generation(source_path)
        except Exception:  # noqa: BLE001 — 注销路径不得因服务异常中断
            logger.debug(
                "proxy service cancel failed for %s", source_path,
                exc_info=True)

    def _proxy_job_token_valid(self, asset_id: str, token: int) -> bool:
        # F-R07/R26：token 与会话代次都匹配才有效（切工程后旧 token
        # 即便数字巧合也拒绝）
        return (
            self._proxy_job_tokens.get(asset_id) == token
            and self._proxy_job_generations.get(asset_id)
            == self._proxy_session_generation
        )

    def _invalidate_all_proxy_jobs(self) -> None:
        """F-R07/R26：切工程/清空工程——注销全部在途代理任务与探测。

        R03：面板登记过的源同步取消统一代理服务上的在途生成（不波及
        服务上其他调用方自行排队的任务）。
        """
        self._proxy_session_generation += 1
        self.__dict__.get("_replacement_requests", {}).clear()
        tracked_sources = list(self._proxy_jobs.values())
        self._proxy_jobs.clear()
        self._proxy_job_tokens.clear()
        self._proxy_job_generations.clear()
        self.__dict__.get("_proxy_job_signatures", {}).clear()
        self._pending_proxy_meta.clear()
        for source in tracked_sources:
            self._cancel_service_proxy_generation(source)

    def _on_proxy_progress(
        self, asset_id: str, percentage: int, token: int = -1
    ) -> None:
        """
        Handle proxy generation progress update.

        Args:
            asset_id: Asset ID being processed
            percentage: Progress percentage (0-100)
            token: FE-B11 任务 token（不匹配即忽略过期进度）
        """
        if token != -1 and not self._proxy_job_token_valid(asset_id, token):
            return
        logger.debug(f"Proxy progress: {asset_id} - {percentage}%")
        # Could update status bar or progress indicator here

    def _on_proxy_finished(
        self, asset_id: str, proxy_path: str, token: int = -1
    ) -> None:
        """
        Handle proxy generation completion.

        Updates the asset's proxy_path and proxy_status to "ready",
        and notifies DiskProxyService and FrameProviderService.

        Args:
            asset_id: Asset ID that completed
            proxy_path: Path to the generated proxy file
            token: FE-B11 任务 token——换源/删除/重新入队后旧任务回调
                一律拒绝，防止旧代理污染新素材。
        """
        if not self._project:
            return

        asset = self._project.assets.get(asset_id)
        if token != -1 and not self._proxy_job_token_valid(asset_id, token):
            logger.info(
                "Ignoring stale proxy completion (token) for %s", asset_id
            )
            return
        expected_path = self._proxy_jobs.get(asset_id)
        if asset is None:
            logger.info(f"Ignoring proxy completion for removed asset: {asset_id}")
            self._invalidate_proxy_job(asset_id)
            return
        if expected_path is None or asset.path != expected_path:
            # FE-B11：无在册任务或源已变化 → 拒绝（旧逻辑 expected=None
            # 时放行，正是换源污染路径）
            logger.info(
                f"Ignoring stale proxy completion for {asset_id}: "
                f"{expected_path} -> {asset.path}"
            )
            self._invalidate_proxy_job(asset_id)
            return

        # An editor outside this application can replace a source in place
        # without changing its asset ID/path or our request token. Encoding
        # results from the former file must never be attached to that new file.
        signature = self.__dict__.get("_proxy_job_signatures", {}).get(asset_id)
        if signature is not None:
            try:
                stat = os.stat(asset.path)
                unchanged = (stat.st_size, stat.st_mtime_ns) == signature
            except OSError:
                unchanged = False
            if not unchanged:
                self._on_proxy_error(
                    asset_id, "生成代理期间源文件发生变化，请重新生成代理", token)
                return

        # Verify proxy file exists
        if not os.path.exists(proxy_path):
            logger.error(f"Proxy file not found after generation: {proxy_path}")
            self._on_proxy_error(asset_id, "Proxy file not found after generation")
            return

        # Update asset proxy info
        try:
            new_project = update_asset_proxy(self._project, asset_id, proxy_path, "ready")
        except ValueError as exc:
            logger.warning(f"Proxy completion skipped for missing asset {asset_id}: {exc}")
            self._invalidate_proxy_job(asset_id)
            return
        self._project = new_project
        self._invalidate_proxy_job(asset_id)
        self.project_changed.emit(new_project)
        self._refresh_tree()

        asset = new_project.assets.get(asset_id)
        name = asset.display_name if asset else asset_id
        logger.info(f"Proxy generated successfully: {name} -> {proxy_path}")

        # Update DiskProxyService and FrameProviderService
        if asset:
            self._update_disk_proxy_services(asset, proxy_path)

    def _update_disk_proxy_services(self, asset: MediaAsset, proxy_path: str) -> None:
        """
        Update DiskProxyService and FrameProviderService after proxy generation.

        AUD-20210: 分辨率探测（av.open / ffprobe 子进程，最长 5s）移入
        QThreadPool worker；本方法只做廉价的文件 stat，随后服务回填在
        ``_apply_proxy_ready``（UI 线程队列回调）完成。

        Args:
            asset: The asset that had proxy generated
            proxy_path: Path to the generated proxy file
        """
        source_path = asset.path

        try:
            # Get source file info for validation
            source_mtime = os.path.getmtime(source_path)
            source_size = os.path.getsize(source_path)
        except OSError:
            source_mtime = 0.0
            source_size = 0

        # AUD-20210: worker 探测完成后经队列信号回 UI 线程回填服务缓存。
        request_id = uuid.uuid4().hex
        worker = ProxyResolutionWorker(
            proxy_path, source_path, asset,
            signals=self._proxy_resolution_signals,
            asset_id=str(getattr(asset, "asset_id", "") or ""),
            session_generation=int(self.__dict__.get("_proxy_session_generation", 0) or 0),
            request_id=request_id,
        )
        # mtime/size 经由回调传递，挂起在 worker 完成前先暂存（N21：
        # 记录携带 asset_id/generation 完整身份；轻量替身宿主无
        # asset_id 时退空串，generation 仍是有效身份维度）。
        self._pending_proxy_meta[proxy_path] = (
            source_path, source_mtime, source_size,
            str(getattr(asset, "asset_id", "") or ""),
            int(self.__dict__.get("_proxy_session_generation", 0) or 0),
            request_id,
        )
        QThreadPool.globalInstance().start(worker)

    def _on_proxy_resolution_probed(
        self, proxy_path: str, source_path: str, resolution: object,
        asset_id: str = "", generation: int = -1, request_id: str = "",
    ) -> None:
        """探测完成（UI 线程）→ 回填 DiskProxyService / FrameProviderService。

        F-R07/R26：pending 记录在切工程/清空时被整体注销；同 proxy_path
        的旧工程探测到达时，其 source_path 与当前 pending 记录不一致 →
        丢弃（零服务写入）。
        """
        # N21/RF05：先 get 核对完整身份（generation + asset + source 三
        # 者匹配）再 pop——此前先 pop 再比 source，同 proxy_path 的旧工
        # 程探测会消耗掉新登记的 pending 并把旧 resolution 配新任务
        # 的 mtime/size 写进服务；异源旧回调还会吞掉新 pending。
        meta = self._pending_proxy_meta.get(proxy_path)
        if meta is None:
            return
        (pending_source_path, source_mtime, source_size,
         pending_asset_id, pending_generation) = meta[:5]
        pending_request_id = meta[5] if len(meta) > 5 else ""
        current_generation = int(
            self.__dict__.get("_proxy_session_generation", 0) or 0)
        if (pending_source_path != source_path
                or pending_asset_id != asset_id
                or pending_generation != generation
                or pending_generation != current_generation
                or pending_request_id != request_id):
            logger.info(
                "Ignoring stale proxy resolution probe: %s "
                "(pending src=%s asset=%s gen=%s, callback src=%s "
                "asset=%s gen=%s, current gen=%s)",
                proxy_path, pending_source_path, pending_asset_id,
                pending_generation, source_path, asset_id, generation,
                current_generation,
            )
            return
        self._pending_proxy_meta.pop(proxy_path, None)
        # A file can also be replaced in place while this worker is probing.
        # Do not attach a result measured against a different source identity.
        if request_id and (source_mtime or source_size):
            try:
                current_stat = os.stat(source_path)
            except OSError:
                return
            if (current_stat.st_mtime != source_mtime
                    or current_stat.st_size != source_size):
                logger.info("Ignoring proxy probe after source changed: %s", source_path)
                return
        project = self.__dict__.get("_project")
        if project is not None and asset_id:
            asset = project.assets.get(asset_id)
            if asset is None or asset.path != source_path or asset.proxy_path != proxy_path:
                return
        self._apply_proxy_ready(
            source_path, proxy_path, source_mtime, source_size,
            resolution if isinstance(resolution, tuple) else None,
        )

    def _apply_proxy_ready(
        self,
        source_path: str,
        proxy_path: str,
        source_mtime: float,
        source_size: int,
        proxy_resolution: Optional[tuple[int, int]],
    ) -> None:
        """用已探测的代理信息回填服务缓存（AUD-20210，UI 线程）。"""
        # Create DiskProxyInfo
        proxy_info = DiskProxyInfo(
            status=ProxyStatus.READY,
            proxy_path=proxy_path,
            proxy_resolution=proxy_resolution,
            source_mtime=source_mtime,
            source_size=source_size,
        )

        # Update DiskProxyService cache via its public write API.
        # AUD-20203（服务侧已就位）: the panel must not hold the service's
        # private lock or write its private caches — DiskProxyService.mark_ready
        # publishes the info and dispatches status callbacks on the service side.
        try:
            get_disk_proxy_service().mark_ready(source_path, proxy_info)
            logger.debug(f"DiskProxyService cache updated: {source_path}")
        except Exception as e:
            logger.warning(f"Failed to update DiskProxyService cache: {e}")

        # Update FrameProviderService if clip is open.
        # AUD-20203: the panel must not hold the service's private lock or
        # read its private caches. Worse, FrameProviderService._lock is a
        # plain (non-reentrant) threading.Lock, so the old
        # `with frame_service._lock: ... update_disk_proxy_info(...)` would
        # deadlock the UI thread whenever the source had an open clip. Use
        # the public accessors only.
        try:
            frame_service = get_frame_provider_service()
            clip_ids = frame_service.get_shared_clips_info().get(source_path, [])
            if clip_ids:
                clip_id = clip_ids[0]
                session = frame_service.get_clip_session(clip_id)
                if session is not None and session.is_open:
                    frame_service.update_disk_proxy_info(clip_id, proxy_info)
                    logger.info(
                        f"FrameProviderService updated with new proxy: "
                        f"{source_path} -> {proxy_path}"
                    )
        except Exception as e:
            logger.warning(f"Failed to update FrameProviderService: {e}")

    def _get_proxy_resolution(
        self, proxy_path: str, asset: MediaAsset
    ) -> Optional[tuple[int, int]]:
        """
        Get proxy resolution by probing the file.

        AUD-20210: 探测实现已提取为模块级 :func:`_probe_proxy_resolution`
        （供 :class:`ProxyResolutionWorker` 在后台线程调用）。本方法仅保留
        为兼容入口 —— **不要在 UI 线程调用**（av.open / ffprobe 子进程可
        阻塞最长 5s）；正常路径走 ``_update_disk_proxy_services`` 的 worker。

        Args:
            proxy_path: Path to the proxy file
            asset: Source asset for reference resolution

        Returns:
            (width, height) tuple or None if cannot determine
        """
        return _probe_proxy_resolution(proxy_path, asset)

    def _on_proxy_error(
        self, asset_id: str, error_message: str, token: int = -1
    ) -> None:
        """
        Handle proxy generation error.

        Updates the asset's proxy_status to "failed".

        Args:
            asset_id: Asset ID that failed
            error_message: Error message
        """
        if not self._project:
            return

        asset = self._project.assets.get(asset_id)
        if token != -1 and not self._proxy_job_token_valid(asset_id, token):
            logger.info("Ignoring stale proxy error (token) for %s", asset_id)
            return
        expected_path = self._proxy_jobs.get(asset_id)
        if asset is None:
            logger.info(f"Ignoring proxy error for removed asset: {asset_id}")
            self._invalidate_proxy_job(asset_id)
            return
        if expected_path is None or asset.path != expected_path:
            # FE-B11：同 finished——无在册任务/源已变化的错误不再触碰新素材
            logger.info(
                f"Ignoring stale proxy error for {asset_id}: "
                f"{expected_path} -> {asset.path}"
            )
            self._invalidate_proxy_job(asset_id)
            return

        # Update asset proxy status to failed
        try:
            new_project = update_asset_proxy(self._project, asset_id, None, "failed")
        except ValueError as exc:
            logger.warning(f"Proxy error skipped for missing asset {asset_id}: {exc}")
            self._invalidate_proxy_job(asset_id)
            return
        self._project = new_project
        self._invalidate_proxy_job(asset_id)
        self.project_changed.emit(new_project)
        self._refresh_tree()

        asset = new_project.assets.get(asset_id)
        name = asset.display_name if asset else asset_id
        logger.error(f"Proxy generation failed for {name}: {error_message}")

        QMessageBox.warning(
            self._get_top_level_widget(),
            I18N.tr("edit.media_pool.proxy_error_title", "Proxy Generation Failed"),
            I18N.tr("edit.media_pool.proxy_error_message",
                    "Failed to generate proxy for '{name}':\n{error}",
                    name=name, error=error_message)
        )

    # ==================== Utility Methods ====================

    def _get_top_level_widget(self) -> QWidget:
        """Get top-level window for dialogs."""
        widget: QWidget = self
        while widget.parent() is not None:
            widget = widget.parent()
        return widget

    # ==================== Public Methods ====================

    def clear(self) -> None:
        """Clear the media pool content."""
        self.clear_project()

    def refresh(self) -> None:
        """Refresh the media pool display."""
        self._offline_status_signature = None
        self.set_project(self._project)
        logger.debug("MediaPoolPanel.refresh() called")

    def get_selected_asset(self) -> Optional[MediaAsset]:
        """Get currently selected asset."""
        # List view
        if self._view_mode == ViewMode.LIST:
            selected_items = self.tree.selectedItems()
            if selected_items:
                item = selected_items[0]
                item_type = item.data(0, Qt.ItemDataRole.UserRole + 1)
                asset_id = item.data(0, Qt.ItemDataRole.UserRole)

                if item_type == "asset" and asset_id and self._project:
                    return self._project.assets.get(asset_id)

        # Grid view
        elif self._view_mode != ViewMode.LIST and self.grid_view:
            indexes = self.grid_view.selectionModel().selectedIndexes()
            if indexes:
                asset = indexes[0].data(Qt.ItemDataRole.UserRole)
                if isinstance(asset, MediaAsset):
                    return asset

        return None

    # ==================== View Mode API ====================

    def get_view_mode(self) -> ViewMode:
        """Get current view mode."""
        return self._view_mode

    def get_thumb_size(self) -> int:
        """Get current thumbnail width in pixels."""
        return self._thumb_width

    def set_view_mode(self, mode: ViewMode) -> None:
        """Set view mode programmatically."""
        if mode == ViewMode.LIST:
            self._toggle_list_view()
        elif mode == ViewMode.GRID:
            self._toggle_grid_view()
        elif mode == ViewMode.METADATA:
            self._toggle_metadata_view()

    def set_thumb_size(self, width: int) -> None:
        """Set thumbnail size programmatically.

        The grid is column-driven now, so an approximate pixel width maps
        to the column count that best produces that card width at the
        current viewport size.
        """
        width = max(60, min(240, width))
        viewport_width = (self.grid_view.viewport().width()
                          if self.grid_view else 0)
        if viewport_width >= width:
            columns = max(1, min(8, round(viewport_width / width)))
        else:
            columns = 1
        self._column_grid.apply_columns(columns)

    # ==================== Undo/Redo API ====================

    def undo(self) -> None:
        """Undo last bin operation."""
        if self._project is None:
            return

        new_project = self._command_stack.undo(self._project)
        if new_project:
            self._project = new_project
            self._refresh_tree(refresh_bins=True)
            self.project_changed.emit(new_project)

    def redo(self) -> None:
        """Redo last undone bin operation."""
        if self._project is None:
            return

        new_project = self._command_stack.redo(self._project)
        if new_project:
            self._project = new_project
            self._refresh_tree(refresh_bins=True)
            self.project_changed.emit(new_project)

    def can_undo(self) -> bool:
        """Check if undo is available."""
        return self._command_stack.can_undo()

    def can_redo(self) -> bool:
        """Check if redo is available."""
        return self._command_stack.can_redo()

    def get_undo_label(self) -> Optional[str]:
        """Get label of next undoable command."""
        return self._command_stack.get_undo_label()

    def get_redo_label(self) -> Optional[str]:
        """Get label of next redoable command."""
        return self._command_stack.get_redo_label()

    def get_command_stack(self) -> ProjectCommandStack:
        """Get the command stack for external access."""
        return self._command_stack

    def reveal_asset(self, asset_id: str) -> bool:
        """Reveal and select an asset in the media pool (R-3).

        Walks the tree to find the item whose UserRole data matches
        *asset_id*, expands parent nodes, selects the item, and scrolls
        the tree so it is visible.

        Args:
            asset_id: The asset ID to find and select

        Returns:
            True if asset was found and revealed, False otherwise
        """
        if not asset_id:
            return False

        # Recursive search through all top-level items and their children
        def _find_item(parent: QTreeWidgetItem, target_id: str) -> QTreeWidgetItem | None:
            for i in range(parent.childCount()):
                child = parent.child(i)
                # Check if this item is the asset
                if child.data(0, Qt.ItemDataRole.UserRole) == target_id:
                    return child
                # Recurse into children (e.g. inside a "Timelines" group)
                found = _find_item(child, target_id)
                if found is not None:
                    return found
            return None

        target_item = None
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            if top.data(0, Qt.ItemDataRole.UserRole) == asset_id:
                target_item = top
                break
            found = _find_item(top, asset_id)
            if found is not None:
                target_item = found
                break

        if target_item is None:
            return False

        # Expand all ancestors so the item is visible
        parent = target_item.parent()
        while parent is not None:
            parent.setExpanded(True)
            parent = parent.parent()

        # Select and scroll
        self.tree.setCurrentItem(target_item)
        self.tree.scrollToItem(target_item)

        logger.debug(f"Revealed asset in media pool: {asset_id}")
        return True
