import sys
import os
import logging
import sqlite3
import unicodedata
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QListView, QLineEdit,
                             QHBoxLayout, QLabel, QFrame, QStyledItemDelegate,
                             QStyle, QSlider, QComboBox, QMenu,
                             QInputDialog, QMessageBox, QToolButton,
                             QAbstractItemView, QApplication, QDockWidget)
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import (
    apply_combo_dropdown_style,
    LIBRARY_PANEL_STYLE, LIBRARY_LIST_VIEW_STYLE,
    LIBRARY_MODE_BTN_STYLE, LIBRARY_ZOOM_LABEL_STYLE,
)
from PyQt6.QtCore import (QDir, pyqtSignal, QSize, Qt, QRect, QPoint, QTimer,
                          QSortFilterProxyModel, QModelIndex, QMimeData, QUrl)
from PyQt6.QtGui import (QFileSystemModel, QPainter, QColor, QBrush, QPen,
                         QLinearGradient, QDrag)
from src.shared.db.database import Database
from src.shared.media.thumbnail_provider import ThumbnailProvider
from src.shared.config.config_manager import ConfigManager
from src.shared.assets.icon_manager import (
    icon_arrow_up, icon_new_folder, icon_grid, icon_list
)

logger = logging.getLogger(__name__)


def _nfc(path: str) -> str:
    """file_meta 键约定：与 ``Database._meta_key`` 一致的 NFC 归一。

    宿主与 file_explorer 共享 topos.db 的 file_meta 表且均以 NFC 键读写，
    评分缓存与迁移 SQL 必须用同一键空间。"""
    return unicodedata.normalize("NFC", path) if path else path


class _RatingCache:
    """[AUD-3801/AUD-3802] 评分内存缓存（GUI 线程专用）。

    此前 paint 热路径与 Rating 排序的 ``lessThan`` 每次都走
    ``Database.get_file_rating``——每调用一次就 connect/execute/close
    一个 SQLite 连接。进入大目录时每个缩略图到达都触发整 viewport
    重绘（N 文件 ≈ N 次全量 paint × 每项一次 DB 往返），Rating 排序
    O(N log N) 次比较 × 2 连接，GUI 线程被同步 DB 往返卡死。

    此处改为单连接整表读入 ``{NFC path: rating}``：file_meta 仅对评过
    级的文件有行（与素材规模同阶），目录加载时刷新、评分写入点增量
    维护，delegate 绘制与代理排序共享同一实例。"""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._ratings: dict[str, int] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            conn = sqlite3.connect(self._db.db_path)
            try:
                rows = conn.execute("SELECT path, rating FROM file_meta").fetchall()
            finally:
                conn.close()
            self._ratings = {_nfc(str(p)): int(r) for p, r in rows}
        except sqlite3.Error:
            logger.warning("RatingCache: load failed", exc_info=True)
            self._ratings = {}

    def refresh(self) -> None:
        """整表重读（目录重载 / 边车迁移 / 行删除后）。"""
        self._loaded = False
        self._ensure_loaded()

    def get(self, path: str) -> int:
        self._ensure_loaded()
        return self._ratings.get(_nfc(path), 0)

    def update(self, path: str, rating: int) -> None:
        self._ensure_loaded()
        self._ratings[_nfc(path)] = int(rating)


def _migrate_file_meta_path(db_path: str, old: str, new: str) -> None:
    """[AUD-3803] 重命名文件时迁移 file_meta 行（NFC 键约定）。"""
    try:
        conn = sqlite3.connect(db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE OR IGNORE file_meta SET path = ? WHERE path = ?",
                    (_nfc(new), _nfc(old)))
        finally:
            conn.close()
    except sqlite3.Error:
        logger.warning("file_meta path migration failed", exc_info=True)


def _migrate_file_meta_prefix(db_path: str, old_dir: str, new_dir: str) -> None:
    """[AUD-3803] 重命名目录时把 ``old_dir/`` 前缀的行改写到 ``new_dir/``。

    用 ``substr`` 前缀比较而非 LIKE，避免路径中的 ``%``/``_`` 被当通配符。"""
    old_prefix = _nfc(old_dir.rstrip("/")) + "/"
    new_prefix = _nfc(new_dir.rstrip("/")) + "/"
    try:
        conn = sqlite3.connect(db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE OR IGNORE file_meta SET path = ? || substr(path, ?) "
                    "WHERE substr(path, 1, ?) = ?",
                    (new_prefix, len(old_prefix) + 1, len(old_prefix), old_prefix))
        finally:
            conn.close()
    except sqlite3.Error:
        logger.warning("file_meta prefix migration failed", exc_info=True)


def _delete_file_meta_rows(db_path: str, path: str, *, is_dir: bool) -> None:
    """[AUD-3803] 删除文件（或目录前缀下全部行）的 file_meta 行。"""
    try:
        conn = sqlite3.connect(db_path)
        try:
            with conn:
                if is_dir:
                    prefix = _nfc(path.rstrip("/")) + "/"
                    conn.execute(
                        "DELETE FROM file_meta WHERE substr(path, 1, ?) = ?",
                        (len(prefix), prefix))
                else:
                    conn.execute(
                        "DELETE FROM file_meta WHERE path = ?", (_nfc(path),))
        finally:
            conn.close()
    except sqlite3.Error:
        logger.warning("file_meta row cleanup failed", exc_info=True)

class LibraryDelegate(QStyledItemDelegate):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.db = Database()
        self.thumb_provider = ThumbnailProvider.instance()
        self.thumb_provider.thumbnail_ready.connect(self.on_thumbnail_ready)
        self.view = None # Will be set by panel
        # [AUD-3801] 评分内存缓存 + .topos 边车存在性缓存：paint 热路径
        # 不再逐项做 SQLite 连接与 stat 往返；缓存由 panel 在目录加载/
        # 文件系统变更时失效。
        self._rating_cache = _RatingCache(self.db)
        self._project_cache: dict[str, bool] = {}
        self._thumb_repaint_scheduled = False

    def set_rating_cache(self, cache: _RatingCache) -> None:
        """共享 panel 级评分缓存（与排序代理同源，写入点单点维护）。"""
        if cache is not None:
            self._rating_cache = cache

    def invalidate_sidecar_cache(self) -> None:
        """清空 .topos 边车存在性缓存（目录加载/文件增删时调用）。"""
        self._project_cache.clear()

    def on_thumbnail_ready(self, path, pixmap):
        # [AUD-3801] 缩略图风暴节流：N 个缩略图逐 path 到达时此前每个
        # 都触发一次整 viewport 重绘；合并为每帧（≈16ms）至多一次。
        if self._thumb_repaint_scheduled or not self.view:
            return
        self._thumb_repaint_scheduled = True
        QTimer.singleShot(16, self._flush_thumbnail_repaint)

    def _flush_thumbnail_repaint(self) -> None:
        self._thumb_repaint_scheduled = False
        if self.view:
            self.view.viewport().update()

    @staticmethod
    def _source_index(index: QModelIndex):
        """穿透代理模型取 (source_model, source_index)。"""
        model = index.model()
        if isinstance(model, QSortFilterProxyModel):
            return model.sourceModel(), model.mapToSource(index)
        return model, index

    def _is_dir(self, index: QModelIndex, file_path: str) -> bool:
        """[AUD-3801] 目录判定走 QFileSystemModel 节点树（O(1)、无 stat）。

        直接挂源模型时 index 即源索引；仅当模型不提供 isDir 时回退 stat。"""
        model, source_index = self._source_index(index)
        is_dir = getattr(model, "isDir", None)
        if callable(is_dir) and source_index.isValid():
            return bool(is_dir(source_index))
        return os.path.isdir(file_path)

    def _has_project(self, file_path: str, is_dir: bool) -> bool:
        """[AUD-3801] .topos 边车存在性按路径缓存，避免每帧逐项 stat。"""
        if is_dir:
            return False
        cached = self._project_cache.get(file_path)
        if cached is None:
            project_path = f"{os.path.splitext(file_path)[0]}.topos"
            cached = os.path.exists(project_path)
            self._project_cache[file_path] = cached
        return cached

    def get_star_metrics(self, option):
        margin = 4
        content_rect = option.rect.adjusted(margin, margin, -margin, -margin)
        thumb_rect = content_rect
        
        star_size = 8
        star_spacing = 2
        # Stars will be placed above filename at the bottom left
        stars_x = thumb_rect.left() + 8
        stars_y = thumb_rect.bottom() - 36 # Positioned above filename overlay
        
        return stars_x, stars_y, star_size, star_spacing, thumb_rect, content_rect

    def paint(self, painter, option, index):
        view = option.widget
        if not isinstance(view, QListView):
            return super().paint(painter, option, index)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        file_path = index.data(QFileSystemModel.Roles.FilePathRole)
        # [AUD-3801] 模型侧判定，无 stat
        is_dir = self._is_dir(index, file_path)
        
        # Background for items
        if view.viewMode() == QListView.ViewMode.ListMode:
            # List mode: absolute seamless background (no margins)
            bg_rect = option.rect
            radius = 0
        else:
            bg_rect = option.rect.adjusted(4, 4, -4, -4)
            radius = 10

        if option.state & QStyle.StateFlag.State_Selected:
            painter.setBrush(QBrush(QColor(255, 136, 0, 80))) # Orange selection to match theme
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRect(bg_rect) if radius == 0 else painter.drawRoundedRect(bg_rect, radius, radius)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.setBrush(QBrush(QColor(255, 255, 255, 15)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRect(bg_rect) if radius == 0 else painter.drawRoundedRect(bg_rect, radius, radius)

        # Handle List Mode separately
        if view.viewMode() == QListView.ViewMode.ListMode:
            self._paint_list_mode(painter, option, index, file_path, is_dir)
        else:
            self._paint_grid_mode(painter, option, index, file_path, is_dir, bg_rect)

        painter.restore()

    def _paint_grid_mode(self, painter, option, index, file_path, is_dir, bg_rect):
        stars_x, stars_y, star_size, star_spacing, thumb_rect, content_rect = self.get_star_metrics(option)
        view = option.widget

        # 1. Check for project file (.topos)
        has_project = self._has_project(file_path, is_dir)
        request_size = thumb_rect.width()

        # Check if we are in "Grid" mode or "Adaptive" mode via a custom property or just check Fixed resize mode
        is_grid_mode = view.resizeMode() == QListView.ResizeMode.Fixed

        if is_dir:
            icon = option.widget.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
            folder_rect = thumb_rect.adjusted(10, 10, -10, -10)
            icon.paint(painter, folder_rect)
        else:
            pixmap = self.thumb_provider.get_thumbnail(file_path, request_size)
            if pixmap:
                pw, ph = pixmap.width(), pixmap.height()
                target_w, target_h = thumb_rect.width(), thumb_rect.height()
                
                if is_grid_mode:
                    # Grid Mode: Fill card (crop/stretch)
                    display_w, display_h = target_w, target_h
                    target_rect = thumb_rect
                    # Calculate source rect for center cropping
                    aspect_pix = pw / ph
                    aspect_target = target_w / target_h
                    if aspect_pix > aspect_target:
                        src_h = ph
                        src_w = int(ph * aspect_target)
                        src_x = (pw - src_w) // 2
                        src_y = 0
                    else:
                        src_w = pw
                        src_h = int(pw / aspect_target)
                        src_x = 0
                        src_y = (ph - src_h) // 2
                    painter.drawPixmap(target_rect, pixmap, QRect(src_x, src_y, src_w, src_h))
                else:
                    # Adaptive Mode: Keep original ratio
                    if pw > ph:
                        display_w = target_w
                        display_h = int(ph * (target_w / pw))
                    else:
                        display_h = target_h
                        display_w = int(pw * (target_h / ph))
                    
                    target_rect = QRect(
                        thumb_rect.left() + (target_w - display_w) // 2,
                        thumb_rect.top() + (target_h - display_h) // 2,
                        display_w, display_h
                    )
                    painter.drawPixmap(target_rect, pixmap)
            else:
                painter.setBrush(QBrush(QColor(30, 30, 30)))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(thumb_rect, 6, 6)

        # Draw Overlay (Filename and Stars) - extend to cover full item area including the margin
        overlay_height = 48
        # Move gradient down 2 pixels to align with image bottom
        overlay_rect = QRect(bg_rect.left(), bg_rect.bottom() - overlay_height + 2, bg_rect.width(), overlay_height)
        
        gradient = QLinearGradient(float(overlay_rect.left()), float(overlay_rect.top()),
                                    float(overlay_rect.left()), float(overlay_rect.bottom()))
        gradient.setColorAt(0, QColor(0, 0, 0, 0))
        gradient.setColorAt(0.4, QColor(0, 0, 0, 100))
        gradient.setColorAt(1, QColor(0, 0, 0, 180))
        painter.setBrush(QBrush(gradient))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(overlay_rect)

        if not is_dir:
            # Draw Stars (above filename)
            rating = self._rating_cache.get(file_path)
            for i in range(5):
                star_pt = QPoint(stars_x + i * (star_size + star_spacing) + star_size//2, stars_y + star_size//2)
                if i < rating:
                    painter.setBrush(QBrush(QColor(255, 180, 0)))
                else:
                    painter.setBrush(QBrush(QColor(255, 255, 255, 60)))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(star_pt, star_size//2, star_size//2)

        # File/Folder Name
        name = index.data(Qt.ItemDataRole.DisplayRole)
        name_rect = QRect(thumb_rect.left() + 8, thumb_rect.bottom() - 20, thumb_rect.width() - 16, 18)
        painter.setPen(QColor(255, 255, 255))
        font = painter.font()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        elided_name = painter.fontMetrics().elidedText(name, Qt.TextElideMode.ElideMiddle, name_rect.width())
        painter.drawText(name_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided_name)

        # Draw "Edited" Badge
        if not is_dir and has_project:
            self.draw_edit_badge(painter, thumb_rect)

    def draw_edit_badge(self, painter, rect):
        """Draws a small blue badge in the top right corner indicating the file has been edited."""
        size = 18
        badge_rect = QRect(rect.right() - size - 6, rect.top() + 6, size, size)
        
        # Shadow
        painter.setBrush(QBrush(QColor(0, 0, 0, 80)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(badge_rect.translated(1, 1))
        
        # Background
        painter.setBrush(QBrush(QColor(0, 122, 255))) # Nice macOS/iOS Blue
        painter.drawEllipse(badge_rect)
        
        # Pen/Edit icon (symbolic)
        painter.setPen(QPen(QColor(255, 255, 255), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        cx, cy = badge_rect.center().x(), badge_rect.center().y()
        # Drawing a diagonal line with a small notch to look like a pen
        painter.drawLine(int(cx - 3.5), int(cy + 3.5), int(cx + 3.5), int(cy - 3.5))
        painter.drawLine(int(cx + 1.5), int(cy - 3.5), int(cx + 3.5), int(cy - 1.5))

    def _paint_list_mode(self, painter, option, index, file_path, is_dir):
        rect = option.rect
        # Make thumbnail occupy the full height for "touching" effect
        thumb_size = rect.height()
        thumb_rect = QRect(rect.left(), rect.top(), thumb_size, thumb_size)
        
        if is_dir:
            icon = option.widget.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
            icon_rect = thumb_rect.adjusted(4, 4, -4, -4)
            icon.paint(painter, icon_rect)
        else:
            # For list mode, request a small but high-quality thumb
            pixmap = self.thumb_provider.get_thumbnail(file_path, 64)
            if pixmap:
                # Center crop to fill the square area
                pw, ph = pixmap.width(), pixmap.height()
                side = min(pw, ph)
                src_rect = QRect((pw - side) // 2, (ph - side) // 2, side, side)
                painter.drawPixmap(thumb_rect, pixmap, src_rect)
            else:
                painter.setBrush(QBrush(QColor(30, 30, 30)))
                painter.drawRect(thumb_rect)

        # Check for project file
        if self._has_project(file_path, is_dir):
            # In list mode, draw badge over thumbnail
            self.draw_edit_badge(painter, thumb_rect)

        # Name and Info
        name = index.data(Qt.ItemDataRole.DisplayRole)
        text_rect = QRect(thumb_rect.right() + 10, rect.top(), rect.width() - thumb_size - 40, rect.height())
        
        painter.setPen(QColor(220, 220, 220))
        font = painter.font()
        font.setPointSize(11)
        painter.setFont(font)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, name)
        
        # Rating (right aligned)
        if not is_dir:
            rating = self._rating_cache.get(file_path)
            for i in range(5):
                star_x = rect.right() - 80 + i * 14
                star_pt = QPoint(star_x, rect.center().y())
                if i < rating:
                    painter.setBrush(QBrush(QColor(255, 180, 0)))
                else:
                    painter.setBrush(QBrush(QColor(255, 255, 255, 18)))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(star_pt, 4, 4)

    def sizeHint(self, option, index):
        view = option.widget
        if view and view.viewMode() == QListView.ViewMode.ListMode:
            return QSize(view.width(), 22)
        
        panel = self.parent()
        if hasattr(panel, 'list_view'):
            gs = panel.list_view.gridSize()
            if gs.isValid() and gs.width() > 0:
                return gs
        return QSize(100, 145) # Fallback grid size

    def editorEvent(self, event, model, option, index):
        file_path = index.data(QFileSystemModel.Roles.FilePathRole)
        if not os.path.isfile(file_path):
            return super().editorEvent(event, model, option, index)

        stars_x, stars_y, star_size, star_spacing, _, _ = self.get_star_metrics(option)
        hit_padding = 4
        
        if event.type() == event.Type.MouseButtonDblClick:
            row_rect = QRect(stars_x - hit_padding, stars_y - hit_padding,
                             (star_size + star_spacing) * 5, star_size + 2 * hit_padding)
            if row_rect.contains(event.pos()):
                self.db.set_file_rating(file_path, 0)
                # [AUD-3801] 写入点同步内存缓存（绘制/排序共用）
                self._rating_cache.update(file_path, 0)
                self.view.viewport().update()
                return True
        elif event.type() == event.Type.MouseButtonRelease:
            # 中键点击：应用调色（仅当有 .topos 项目文件时）
            if event.button() == Qt.MouseButton.MiddleButton:
                project_path = f"{os.path.splitext(file_path)[0]}.topos"
                if os.path.exists(project_path):
                    panel = self.parent()
                    if panel and hasattr(panel, 'grade_applied'):
                        panel.grade_applied.emit(file_path)
                    return True
            # 左键点击：星级评分
            for i in range(5):
                star_hit_rect = QRect(
                    stars_x + i * (star_size + star_spacing) - star_spacing//2,
                    stars_y - hit_padding,
                    star_size + star_spacing,
                    star_size + 2 * hit_padding
                )
                if star_hit_rect.contains(event.pos()):
                    self.db.set_file_rating(file_path, i + 1)
                    # [AUD-3801] 写入点同步内存缓存（绘制/排序共用）
                    self._rating_cache.update(file_path, i + 1)
                    self.view.viewport().update()
                    # 发射信号通知 Files Panel 同步更新
                    # 通过 parent() 获取 LibraryPanel 实例并发射信号
                    panel = self.parent()
                    if panel and hasattr(panel, 'rating_changed'):
                        panel.rating_changed.emit(file_path, i + 1)
                    return True
        return super().editorEvent(event, model, option, index)

class LibraryProxyModel(QSortFilterProxyModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.db = Database()
        self.sort_criteria = "name"
        # [AUD-3802] Rating 排序的比较器不得逐次建 SQLite 连接
        self._rating_cache = _RatingCache(self.db)

    def set_rating_cache(self, cache: _RatingCache) -> None:
        """共享 panel 级评分缓存（与 delegate 绘制同源）。"""
        if cache is not None:
            self._rating_cache = cache

    def set_sort_criteria(self, criteria):
        # [AUD-3802] 切到 Rating 排序时一次性刷新评分缓存（单连接批量），
        # lessThan 的 O(N log N) 次比较全部查内存 dict
        if criteria == "rating":
            self._rating_cache.refresh()
        self.sort_criteria = criteria
        self.invalidate()
        self.sort(0, Qt.SortOrder.AscendingOrder if criteria == "name" else Qt.SortOrder.DescendingOrder)

    def lessThan(self, left, right):
        source = self.sourceModel()
        if not source: return False
        left_path = source.filePath(left)
        right_path = source.filePath(right)

        if self.sort_criteria == "rating":
            r1 = self._rating_cache.get(left_path)
            r2 = self._rating_cache.get(right_path)
            return r1 < r2
        elif self.sort_criteria == "size":
            return source.size(left) < source.size(right)
        elif self.sort_criteria == "date":
            return source.lastModified(left) < source.lastModified(right)
        else: # name
            return source.fileName(left).lower() < source.fileName(right).lower()

class DraggableListView(QListView):
    """支持拖拽的列表视图"""
    
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
    
    def startDrag(self, supportedActions):
        """启动拖拽操作"""
        drag = QDrag(self)
        mimedata = QMimeData()
        
        # 获取选中的文件路径
        urls = []
        for index in self.selectedIndexes():
            # 获取源模型索引（从代理模型映射回来）
            if hasattr(self.model(), 'mapToSource'):
                source_index = self.model().mapToSource(index)
                file_path = self.model().sourceModel().filePath(source_index)
            else:
                file_path = self.model().filePath(index)
            
            if os.path.isfile(file_path):
                urls.append(QUrl.fromLocalFile(file_path))
        
        if urls:
            mimedata.setUrls(urls)
            drag.setMimeData(mimedata)
            drag.exec(Qt.DropAction.CopyAction)

class LibraryPanel(QWidget):
    image_selected = pyqtSignal(str)
    video_selected = pyqtSignal(str)  # 新增：视频选中信号
    directory_changed = pyqtSignal(str)
    rating_changed = pyqtSignal(str, int)  # 信号：星级变更
    grade_applied = pyqtSignal(str)  # 信号：中键应用调色
    quick_project_requested = pyqtSignal(str)  # 信号：请求创建 Quick Project（媒体路径）

    def __init__(self, start_path=None, parent=None) -> None:
        super().__init__(parent)
        self.config = ConfigManager()
        self.db = Database()  # 添加数据库实例
        if start_path is None:
            start_path = self.config.get("last_library_path") or QDir.homePath()
        self.start_path = start_path
        # [AUD-3504] Library ↔ Files 双向目录同步的防环状态：
        # _last_synced_path 记录最近一次对外广播的目录（同路径重入直接
        # 忽略），_dir_syncing 为 emit 期间的递归锁（信号防递归铁律）。
        self._last_synced_path: str | None = None
        self._dir_syncing = False
        # [AUD-3801/3802] delegate 绘制与 Rating 排序共用的评分缓存
        self._rating_cache = _RatingCache(self.db)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setStyleSheet(LIBRARY_PANEL_STYLE)

        # --- Header ---
        header = QFrame()
        header.setObjectName("Header")
        header.setFixedHeight(38)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 0, 10, 0)
        header_layout.setSpacing(8)

        self.up_btn = QToolButton()
        self.up_btn.setIcon(icon_arrow_up())
        self.up_btn.clicked.connect(self.go_up)
        header_layout.addWidget(self.up_btn)

        self.path_edit = QLineEdit(self.start_path)
        self.path_edit.returnPressed.connect(self.on_path_entered)
        header_layout.addWidget(self.path_edit)

        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["Name", "Size", "Date", "Rating"])
        self.sort_combo.setFixedWidth(85)
        apply_combo_dropdown_style(self.sort_combo)
        self.sort_combo.currentTextChanged.connect(self.on_sort_changed)
        header_layout.addWidget(self.sort_combo)
        layout.addWidget(header)

        # --- Model ---
        self.model = QFileSystemModel()
        self.model.setRootPath("")
        # 包含图像和视频格式
        self.model.setNameFilters([
            "*.png", "*.jpg", "*.jpeg", "*.bmp", "*.tif", "*.tiff", "*.webp", "*.arw", "*.cr2", "*.nef", "*.dng", "*.orf", "*.raf", "*.psd", "*.exr", "*.hdr",
            "*.mp4", "*.mov", "*.avi", "*.mkv", "*.m4v", "*.flv", "*.wmv", "*.webm"
        ])
        self.model.setNameFilterDisables(False)
        self.proxy_model = LibraryProxyModel()
        self.proxy_model.setSourceModel(self.model)
        # [AUD-3801/3802] 排序比较器与 delegate 绘制共享同一评分缓存
        self.proxy_model.set_rating_cache(self._rating_cache)
        # [AUD-3801] 目录加载/文件系统变更时失效 stat 与评分缓存
        self.model.directoryLoaded.connect(self._on_model_directory_loaded)
        self.model.rowsInserted.connect(self._on_model_rows_changed)
        self.model.rowsRemoved.connect(self._on_model_rows_changed)

        # --- List View ---
        self.list_view = DraggableListView()
        self.list_view.setObjectName("LibraryList")
        self.list_view.setModel(self.proxy_model)
        self.list_view.setViewMode(QListView.ViewMode.IconMode)
        self.list_view.setResizeMode(QListView.ResizeMode.Fixed)
        self.list_view.setSpacing(10)
        self.list_view.setUniformItemSizes(True)
        self.list_view.setMovement(QListView.Movement.Static)
        self.list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_view.customContextMenuRequested.connect(self.show_context_menu)
        self.list_view.setStyleSheet(LIBRARY_LIST_VIEW_STYLE)
        self.list_view.setLayoutMode(QListView.LayoutMode.Batched)
        self.list_view.setBatchSize(100)

        self.delegate = LibraryDelegate(self)
        self.delegate.view = self.list_view
        self.delegate.set_rating_cache(self._rating_cache)
        self.list_view.setItemDelegate(self.delegate)
        layout.addWidget(self.list_view)
        # Column-count zoom: thumbs are square and the delegate paints the
        # filename ON the thumb, so the cell carries no caption strip.  The
        # delegate's sizeHint already follows the view's gridSize.  Item
        # hints are NOT pinned — QFileSystemModel rows are not QListWidget
        # items.
        from src.shared.ui.column_grid import ColumnGridController
        self._column_grid = ColumnGridController(
            self.list_view, slider=None,
            settings_key="library_columns", columns_default=4,
            gap=8, aspect=1.0, extra_height=0, pin_item_hints=False)

        # --- Footer ---
        footer = QFrame()
        footer.setObjectName("Footer")
        footer.setFixedHeight(32)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(12, 0, 12, 0)
        footer_layout.setSpacing(8)

        self.new_folder_btn = QToolButton()
        self.new_folder_btn.setIcon(icon_new_folder())
        self.new_folder_btn.clicked.connect(self.create_folder)
        footer_layout.addWidget(self.new_folder_btn)
        footer_layout.addStretch()

        # View Modes
        self.mode_group = QHBoxLayout()
        self.mode_group.setSpacing(2)
        self.btn_adaptive = QToolButton(); self.btn_adaptive.setIcon(icon_grid())
        self.btn_adaptive.setCheckable(True); self.btn_adaptive.setChecked(True); self.btn_adaptive.setStyleSheet(LIBRARY_MODE_BTN_STYLE)
        self.btn_adaptive.clicked.connect(lambda: self.set_view_mode("adaptive"))
        self.btn_grid = QToolButton(); self.btn_grid.setIcon(icon_grid())
        self.btn_grid.setCheckable(True); self.btn_grid.setStyleSheet(LIBRARY_MODE_BTN_STYLE)
        self.btn_grid.clicked.connect(lambda: self.set_view_mode("grid"))
        self.btn_list = QToolButton(); self.btn_list.setIcon(icon_list())
        self.btn_list.setCheckable(True); self.btn_list.setStyleSheet(LIBRARY_MODE_BTN_STYLE)
        self.btn_list.clicked.connect(lambda: self.set_view_mode("list"))
        self.mode_group.addWidget(self.btn_adaptive); self.mode_group.addWidget(self.btn_grid); self.mode_group.addWidget(self.btn_list)
        footer_layout.addLayout(self.mode_group)
        footer_layout.addStretch()

        zoom_lbl = QLabel(I18N.tr("library.size", "大小"))
        zoom_lbl.setStyleSheet(LIBRARY_ZOOM_LABEL_STYLE)
        footer_layout.addWidget(zoom_lbl)
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setFixedWidth(80)
        self._column_grid.attach_slider(self.zoom_slider)
        footer_layout.addWidget(self.zoom_slider)
        layout.addWidget(footer)

        self.list_view.doubleClicked.connect(self.on_item_double_clicked)
        self.list_view.setDragEnabled(True)
        self.list_view.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.set_root_path(self.start_path)

    def update_grid_size(self, value):
        # Legacy hook — sizing is owned by the column-grid controller.
        self._column_grid.apply_columns(self._column_grid.columns)

    def set_view_mode(self, mode):
        self.btn_adaptive.setChecked(mode == "adaptive")
        self.btn_grid.setChecked(mode == "grid")
        self.btn_list.setChecked(mode == "list")
        if mode == "list":
            # Suspend the column controller: list mode owns its geometry
            # (flat rows, no gridSize) until an icon mode resumes it.
            self._column_grid.set_active(False)
            self.list_view.setViewMode(QListView.ViewMode.ListMode)
            self.list_view.setSpacing(0); self.list_view.setGridSize(QSize())
            self.list_view.setStyleSheet("QListView#LibraryList { background: #27272b; border: none; padding: 0px; }")
        else:
            self._column_grid.set_active(True)
            self.list_view.setViewMode(QListView.ViewMode.IconMode)
            self.list_view.setSpacing(10)
            self.list_view.setStyleSheet("QListView#LibraryList { background: #27272b; border: none; padding: 10px; }")
            self.list_view.setResizeMode(QListView.ResizeMode.Fixed)
            self.list_view.setLayoutMode(QListView.LayoutMode.Batched)
            self.list_view.setBatchSize(100)
        self.list_view.doItemsLayout()

    def on_sort_changed(self, text): self.proxy_model.set_sort_criteria(text.lower())
    def on_path_entered(self):
        path = self.path_edit.text()
        if os.path.isdir(path): self.set_root_path(path)
    def go_up(self):
        parent = os.path.dirname(self.path_edit.text())
        if os.path.isdir(parent): self.set_root_path(parent)
    def set_root_path(self, path: str) -> None:
        # [AUD-3504] 同路径守卫 + emit 期间状态锁（信号防递归）：
        # Library ↔ Files 的 directory_changed 双向互连下，重入的相同
        # 路径同步不得再次 emit——否则前端对程序化 loadPath 回发
        # directoryChanged 时，两个面板会无限互相触发目录重载。
        if self._dir_syncing:
            return
        if not os.path.exists(path):
            return
        new_path = os.path.abspath(os.path.normpath(path))
        if self._last_synced_path is not None and new_path == self._last_synced_path:
            return  # 同路径：视图/路径框/配置均无变化，直接断环
        self._dir_syncing = True
        try:
            self.list_view.setRootIndex(self.proxy_model.mapFromSource(self.model.index(new_path)))
            self.path_edit.setText(new_path)
            self.config.set("last_library_path", new_path)
            self.config.save_config()
            self._last_synced_path = new_path
            # 发出目录切换信号，通知 Files Panel 同步
            self.directory_changed.emit(new_path)
        finally:
            self._dir_syncing = False

    def _on_model_directory_loaded(self, _path: str) -> None:
        """[AUD-3801] 目录加载完成：刷新评分缓存、失效边车存在性缓存。"""
        self._rating_cache.refresh()
        self.delegate.invalidate_sidecar_cache()

    def _on_model_rows_changed(self, *_args) -> None:
        """[AUD-3801] 文件增删：失效边车存在性缓存（dict 清空，O(1)）。"""
        self.delegate.invalidate_sidecar_cache()

    def refresh_file_rating(self, path: str, rating: int) -> None:
        """刷新指定文件的星级显示"""
        # 更新数据库中的评分
        self.db.set_file_rating(path, rating)
        # [AUD-3801] 写入点同步内存缓存（绘制/排序共用）
        self._rating_cache.update(path, rating)
        # 刷新视图以显示更新后的星级
        self.list_view.viewport().update()

    def on_item_double_clicked(self, index):
        path = self.model.filePath(self.proxy_model.mapToSource(index))
        if os.path.isdir(path):
            self.set_root_path(path)
        else:
            # 判断文件类型：视频或图像
            from src.shared.media.format_registry import VIDEO_EXTENSIONS
            video_extensions = tuple(VIDEO_EXTENSIONS)
            if path.lower().endswith(video_extensions):
                # 统一工程包架构方案 13.3：
                # 双击 MOV 推荐创建 Quick Project
                # 发射 quick_project_requested 信号，由 MainWindow 处理
                self.quick_project_requested.emit(path)
            else:
                self.image_selected.emit(path)

    def show_context_menu(self, pos):
        index = self.list_view.indexAt(pos); menu = QMenu(self)
        if index.isValid():
            path = self.model.filePath(self.proxy_model.mapToSource(index))
            if os.path.isfile(path):
                rm = menu.addMenu("Set Rating")
                for i in range(6): rm.addAction(f"{i} Stars" if i > 0 else "Clear").triggered.connect(lambda c, r=i: self.set_file_rating(path, r))
            menu.addAction(I18N.tr("pm.rename", "Rename")).triggered.connect(lambda: self.rename_item(path))
            menu.addAction(I18N.tr("pm.delete", "Delete")).triggered.connect(lambda: self.delete_item(path))
        else: menu.addAction(I18N.tr("library.new_folder", "New Folder")).triggered.connect(self.create_folder)
        menu.addSeparator()
        self._add_float_action(menu)
        menu.exec(self.list_view.mapToGlobal(pos))

    def _add_float_action(self, menu):
        """Add float/dock toggle action to the context menu."""
        dock = self.parent()
        while dock and not isinstance(dock, QDockWidget):
            dock = dock.parent()
        if dock:
            act_text = I18N.tr("dock.dock", "Dock") if dock.isFloating() else I18N.tr("dock.float", "Float")
            menu.addAction(act_text).triggered.connect(lambda: dock.setFloating(not dock.isFloating()))

    def set_file_rating(self, path: str, r: int) -> None:
        self.db.set_file_rating(path, r)
        # [AUD-3801] 写入点同步内存缓存（绘制/排序共用）
        self._rating_cache.update(path, r)
        self.list_view.viewport().update()
    def create_folder(self):
        name, ok = QInputDialog.getText(self, "New Folder", "Name:")
        if ok and name:
            try: os.makedirs(os.path.join(self.path_edit.text(), name))
            except Exception as e: QMessageBox.critical(self, "Error", str(e))
    def _file_meta_db_path(self) -> str | None:
        """file_meta 迁移用的 db_path；未完整初始化的替身面板返回 None。

        既有回归测试经 ``LibraryPanel.__new__`` 构造裸面板（无 __init__），
        此时普通属性访问会触发 PyQt 的 "super-class __init__() was never
        called" RuntimeError——经 ``__dict__`` 取值可安全判存。"""
        db = self.__dict__.get("db")
        return getattr(db, "db_path", None)

    def _refresh_rating_cache_safe(self) -> None:
        """DB 行变更后刷新共享评分缓存（替身面板上安全跳过）。"""
        cache = self.__dict__.get("_rating_cache")
        if cache is not None:
            cache.refresh()

    def rename_item(self, path: str) -> None:
        name, ok = QInputDialog.getText(self, "Rename", "New Name:", text=os.path.basename(path))
        if not (ok and name):
            return
        new_path = os.path.join(os.path.dirname(path), name)
        if os.path.abspath(new_path) == os.path.abspath(path):
            return  # 名称未变化，重命名是 no-op
        # AUD-3800：POSIX rename(2) 对已存在的同名文件/目录是静默原子覆盖，
        # 必须先做冲突检查，否则输入重名会直接丢失被覆盖文件（含其 .topos
        # 工程关联），不可恢复。lexists 连悬空符号链接也一并拦截。
        if os.path.lexists(new_path):
            QMessageBox.warning(
                self, "Rename",
                f"An item named \"{name}\" already exists:\n{new_path}")
            return
        try:
            os.rename(path, new_path)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return
        # AUD-3803：同步迁移 .topos 边车与 file_meta 评分行——徽章/中键
        # "应用调色"/评分均按路径关联，不迁移则编辑关联静默断裂。
        db_path = self._file_meta_db_path()
        if os.path.isdir(new_path):
            # 目录重命名：内部文件与其边车一起搬移、相对关系保持，只有
            # DB 中按绝对路径记的评分行需要改前缀。
            if db_path:
                _migrate_file_meta_prefix(db_path, path, new_path)
        else:
            old_sidecar = f"{os.path.splitext(path)[0]}.topos"
            new_sidecar = f"{os.path.splitext(new_path)[0]}.topos"
            if os.path.lexists(old_sidecar) and not os.path.lexists(new_sidecar):
                try:
                    os.rename(old_sidecar, new_sidecar)
                except OSError as e:
                    QMessageBox.warning(
                        self, "Rename",
                        f"Renamed the file but failed to move its project file:\n{e}")
            if db_path:
                _migrate_file_meta_path(db_path, path, new_path)
        self._refresh_rating_cache_safe()

    def delete_item(self, path: str) -> None:
        if QMessageBox.question(self, "Delete", f"Delete {os.path.basename(path)}?") == QMessageBox.StandardButton.Yes:
            is_dir = os.path.isdir(path)
            # AUD-3803：删除文件时询问是否连同删除其 .topos 工程边车，
            # 否则残留孤儿边车在同名文件出现时被误关联。
            sidecar = None if is_dir else f"{os.path.splitext(path)[0]}.topos"
            remove_sidecar = False
            if sidecar is not None and os.path.lexists(sidecar):
                remove_sidecar = QMessageBox.question(
                    self, "Delete",
                    f"Also delete the associated project file?\n{sidecar}",
                ) == QMessageBox.StandardButton.Yes
            try:
                if is_dir:
                    import shutil; shutil.rmtree(path)
                else:
                    os.remove(path)
                    if remove_sidecar and sidecar is not None:
                        os.remove(sidecar)
            except Exception as e:
                QMessageBox.critical(self, "Error", str(e))
                return
            # AUD-3803：清理 DB 评分行（目录按前缀清理其下全部行）
            db_path = self._file_meta_db_path()
            if db_path:
                _delete_file_meta_rows(db_path, path, is_dir=is_dir)
            self._refresh_rating_cache_safe()

if __name__ == "__main__":
    app = QApplication(sys.argv); panel = LibraryPanel(); panel.show(); sys.exit(app.exec())
