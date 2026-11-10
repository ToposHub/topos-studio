import os
import json
import logging
import shutil
import zipfile
from typing import Optional, OrderedDict
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFrame,
                             QStyledItemDelegate, QStyle, QListWidget, QListWidgetItem,
                             QToolButton, QSlider, QLabel, QLineEdit,
                             QMenu, QInputDialog, QMessageBox,
                             QDockWidget)
from PyQt6.QtCore import (QStandardPaths, QObject, pyqtSignal, Qt, QSize,
                          QRect)
from PyQt6.QtGui import (QPainter, QColor, QBrush, QPen, QPixmap, QLinearGradient)
from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.assets.icon_manager import get_icon
from src.features.color.core.gallery_contracts import BUNDLE_SUFFIX_BY_ROLE

logger = logging.getLogger(__name__)

#: 大库阈值：相册条目数达到该值时扫描走后台 runner（BR04），小相册
#: 保持同步路径（与媒体池 P0-4 的 MEDIA_POOL_ASYNC_QUERY_THRESHOLD
#: 同一模式）。测试可经 ``_async_query_threshold`` 注入更小值。
GALLERY_ASYNC_QUERY_THRESHOLD = 2000


class _AsyncScanBridge(QObject):
    """工作线程 → GUI 线程的队列化投递桥（跨线程 emit 自动排队）。"""

    delivered = pyqtSignal(object, object)   # query, result


def _resolve_presets_root() -> str:
    """返回稳定的用户预设根目录（AUD-25514）。

    之前绑定 os.getcwd()：从 Finder/快捷方式/cron 以不同工作目录启动时
    面板读写的是另一份空的 ``<cwd>/Presets``，已有静帧"丢失"、新抓取写进
    临时位置；只读 CWD 下 makedirs 还会在构造函数直接抛 PermissionError。
    现统一使用应用数据目录（与 motion_graphic_template_store 同口径），
    Qt 不可用或返回空时回退 ``~/.topos-color``。
    """
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation)
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".topos-color")
    return os.path.join(base, "Presets")


def _is_safe_preset_name(name: str) -> bool:
    """校验用户输入的预设/文件夹名（AUD-25515）。

    G03：实现移至领域层 ``is_safe_bundle_stem``（单一来源）；本别名保留
    供既有调用方／测试。拒绝路径分隔符、``.``/``..`` 与控制字符，防止
    ``create_folder("../x")`` 之类的路径注入把文件写出预设区。
    """
    from src.features.color.core.gallery_contracts import is_safe_bundle_stem
    return is_safe_bundle_stem(name)


class PresetsPanel(QWidget):
    """Gallery panel（原 Presets，P1-COLOR-04 升级为 DaVinci Gallery）。

    Captures still frames from the viewport, saves them as .topos project
    files (with thumb / full-res reference / meta sidecars), and supports
    applying grades to other images or videos — full or by category
    (Paste Attributes) — plus loading a still as reference wipe.
    面板显示名为 Gallery；代码名 PresetsPanel 与 objectName "dockPresets"
    保留以兼容既有引用与已保存布局。
    """

    grade_applied = pyqtSignal(str)   # Emits .topos file path
    capture_requested = pyqtSignal()  # Emits when user clicks Capture Still
    # P1-COLOR-04（Gallery 并入 Presets）
    apply_categories_requested = pyqtSignal(str)  # 分类应用调色（.topos 路径）
    reference_requested = pyqtSignal(str)         # 加载为参考帧 wipe
    # P1-COLOR-21: Color Match（把当前调色结果匹配到此静帧）
    match_requested = pyqtSignal(str)
    # BR06：bundle 变更通知（event: "renamed"/"deleted"，topos 路径）——
    # 比较会话据此失效在场参考（改名前的路径随事件一起发出）。
    bundle_mutated = pyqtSignal(str, str)
    # G13：分屏比较设置窗的预留协议（presenter 未实现——宿主如实拒绝）
    split_comparison_requested = pyqtSignal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.presets_root = _resolve_presets_root()
        self._ensure_dirs()
        self._migrate_legacy_cwd_presets()
        self._current_folder = os.path.join(self.presets_root, "User")
        self._preset_meta_cache = {}  # path -> dict (project.json data cache)
        self._search_query = ""       # 当前搜索词（扫描后重放过滤）
        # G01：磁盘读取统一走 repository 查询（与能力宿主共用事实源）；
        # 能力宿主由 app 层安装（未安装时菜单保持旧的恒启用行为）。
        from src.features.color.core.gallery_repository import (
            GalleryFolderRepository,
        )
        from src.features.color.core.gallery_query_service import (
            GalleryQueryService,
        )
        self._repository = GalleryFolderRepository(self.presets_root)
        # G05：扫描经索引服务——分层签名（逐条目 stat 多重集）未变时
        # 复用已解析条目，重扫零磁盘 I/O；rename/delete/capture 改目录、
        # 原地改 meta/sidecar 改文件 stat，都会自动失效（BR04）。
        self._query_service = GalleryQueryService(self._repository)
        # BR04：大库走后台 runner（同 album 合并保留最新＋代际守卫），
        # 结果经队列化桥回 GUI 线程；小相册保持同步路径。
        self._async_query_threshold = GALLERY_ASYNC_QUERY_THRESHOLD
        self._scan_generation = 0
        self._last_scan_signature: Optional[str] = None
        self._async_bridge = _AsyncScanBridge()
        self._async_bridge.delivered.connect(self._on_async_query_delivered)
        self._query_service.attach_runner(
            on_result=self._async_bridge.delivered.emit)
        # G03：rename/delete 统一走 bundle 事务服务（不再面板内逐文件操作）
        from src.features.color.core.gallery_bundle_service import (
            GalleryBundleService,
        )
        self._bundle_service = GalleryBundleService(self.presets_root)
        # BR05：传输服务（导入/导出 bundle）真正接到面板；Label 服务＝
        # preset_meta 落盘（``_set_still_label``）。宿主据此反映真实能力。
        from src.features.color.core.gallery_transfer_service import (
            GalleryTransferService,
        )
        self._transfer_service = GalleryTransferService(
            self._repository, self._bundle_service)
        self._label_service = self._set_still_label
        self._gallery_capability_host = None
        self._init_ui()
        # G11：用户级显示偏好（版本化、原子落盘；见 gallery_preferences）
        self._load_preferences()
        self.scan_presets()

    def _ensure_dirs(self) -> None:
        """Ensure Presets directory structure exists.

        AUD-25514：创建失败（如只读卷/权限）记录警告而不是从构造函数
        抛 PermissionError 导致启动崩溃；后续扫描/保存各自的 OSError
        已有独立处理路径。
        """
        for sub in ["User", "System"]:
            try:
                os.makedirs(os.path.join(self.presets_root, sub), exist_ok=True)
            except OSError as exc:
                logger.warning(
                    "Presets: cannot create %s (%s); panel will stay empty "
                    "until the location becomes writable",
                    os.path.join(self.presets_root, sub), exc)

    def _migrate_legacy_cwd_presets(self) -> None:
        """把旧版 ``<cwd>/Presets`` 的用户数据复制进稳定根目录（AUD-25514）。

        仅在新位置对应子目录为空时搬迁，避免覆盖既有数据；采用**复制**
        而非移动——旧目录可能是仓库内真实用户数据（测试/开发环境从仓库
        根启动），迁移绝不删除来源。任何 OSError 只记录警告，迁移是
        尽力而为的、幂等的（已存在的目标跳过）。
        """
        legacy_root = os.path.join(os.getcwd(), "Presets")
        if os.path.abspath(legacy_root) == os.path.abspath(self.presets_root):
            return
        if not os.path.isdir(legacy_root):
            return
        for sub in ("User", "System"):
            legacy_sub = os.path.join(legacy_root, sub)
            new_sub = os.path.join(self.presets_root, sub)
            if not os.path.isdir(legacy_sub):
                continue
            try:
                entries = os.listdir(legacy_sub)
                if not entries or any(os.listdir(new_sub)):
                    continue
                copied = 0
                for entry in entries:
                    source = os.path.join(legacy_sub, entry)
                    destination = os.path.join(new_sub, entry)
                    if os.path.exists(destination):
                        continue
                    if os.path.isdir(source):
                        shutil.copytree(source, destination)
                    else:
                        shutil.copy2(source, destination)
                    copied += 1
                if copied:
                    logger.info(
                        "Presets: copied %d entries from legacy %s to %s",
                        copied, legacy_sub, new_sub)
            except OSError as exc:
                logger.warning(
                    "Presets: legacy migration %s -> %s failed: %s",
                    legacy_sub, new_sub, exc)

    def _init_ui(self) -> None:
        """Install the Gallery presentation while retaining the existing services."""
        from src.features.color.ui.gallery_chrome import GalleryChrome

        self._gallery = GalleryChrome(self)
        self._column_grid = self._gallery.grid
        self.delegate = PresetDelegate(self)
        self.list_widget.setItemDelegate(self.delegate)
        self._populate_folder_combo()
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_panel_context_menu)

    # --- Panel Float / Dock ---

    def open_comparison(self, mode: str = "wipe") -> None:
        """Open reference setup; only the existing wipe service is executable."""
        from src.features.color.ui.gallery_compare import GalleryCompareDialog

        entries = [(self.list_widget.item(i).text(),
                    self.list_widget.item(i).data(Qt.ItemDataRole.UserRole))
                   for i in range(self.list_widget.count())
                   if not self.list_widget.item(i).isHidden()]
        selected = [item.data(Qt.ItemDataRole.UserRole)
                    for item in self.list_widget.selectedItems()]
        dialog = GalleryCompareDialog(entries, selected, mode, self)
        dialog.wipe_requested.connect(self.reference_requested.emit)
        # G13：split 协议转发给宿主（接受/拒绝由宿主如实回填）
        dialog.split_requested.connect(
            self.split_comparison_requested.emit)
        dialog.exec()

    def _find_dock_widget(self):
        """Walk up the widget hierarchy to find the parent QDockWidget."""
        parent = self.parent()
        while parent is not None:
            if isinstance(parent, QDockWidget):
                return parent
            parent = parent.parent()
        return None

    def _toggle_float(self) -> None:
        """Float the panel as an independent window."""
        dock = self._find_dock_widget()
        if dock and not dock.isFloating():
            dock.setFloating(True)

    def _dock_panel(self) -> None:
        """Dock the panel back to the main window."""
        dock = self._find_dock_widget()
        if dock and dock.isFloating():
            dock.setFloating(False)

    def _show_panel_context_menu(self, pos) -> None:
        """Show right-click context menu for the panel header/toolbar area."""
        menu = QMenu(self)

        dock = self._find_dock_widget()
        is_floating = dock.isFloating() if dock else False

        float_action = menu.addAction(I18N.tr("panel.float", "Float") if not is_floating else I18N.tr("panel.floating", "Floating"))
        float_action.setEnabled(not is_floating)
        float_action.triggered.connect(self._toggle_float)

        dock_action = menu.addAction(I18N.tr("panel.dock", "Dock") if is_floating else I18N.tr("panel.docked", "Docked"))
        dock_action.setEnabled(is_floating)
        dock_action.triggered.connect(self._dock_panel)

        menu.addSeparator()

        refresh_act = menu.addAction(I18N.tr("presets.refresh", "Refresh"))
        refresh_act.triggered.connect(self.scan_presets)

        menu.exec(self.mapToGlobal(pos))

    # --- Folder Navigation ---

    def _populate_folder_combo(self) -> None:
        """Populate the folder combo with User and System preset folders."""
        self.folder_combo.blockSignals(True)
        self.folder_combo.clear()

        # User section
        user_root = os.path.join(self.presets_root, "User")
        self.folder_combo.addItem("User", user_root)
        for name in sorted(self._list_subdirs(user_root)):
            self.folder_combo.addItem(f"  User/{name}", os.path.join(user_root, name))

        # System section
        system_root = os.path.join(self.presets_root, "System")
        self.folder_combo.addItem("System", system_root)
        for name in sorted(self._list_subdirs(system_root)):
            self.folder_combo.addItem(f"  System/{name}", os.path.join(system_root, name))

        # Restore current selection
        for i in range(self.folder_combo.count()):
            if self.folder_combo.itemData(i) == self._current_folder:
                self.folder_combo.setCurrentIndex(i)
                break

        self.folder_combo.blockSignals(False)

    def _on_folder_changed(self, text) -> None:
        idx = self.folder_combo.currentIndex()
        if idx >= 0:
            path = self.folder_combo.itemData(idx)
            if path and os.path.isdir(path):
                self._current_folder = path
                self.scan_presets()

    def _go_up(self) -> None:
        """Navigate to parent folder."""
        parent = os.path.dirname(self._current_folder)
        if parent and os.path.isdir(parent) and parent.startswith(self.presets_root):
            self._current_folder = parent
            self._populate_folder_combo()
            self.scan_presets()

    def _list_subdirs(self, path):
        """List subdirectory names in a given path."""
        if not os.path.isdir(path):
            return []
        return [d for d in os.listdir(path)
                if os.path.isdir(os.path.join(path, d)) and not d.startswith('.')]

    # --- Preset Meta Cache ---

    def _get_preset_meta(self, topos_path: str) -> dict:
        """Read and cache project.json from a .topos file."""
        if topos_path in self._preset_meta_cache:
            return self._preset_meta_cache[topos_path]
        data = {}
        try:
            with zipfile.ZipFile(topos_path, 'r') as zf:
                if "project.json" in zf.namelist():
                    with zf.open("project.json") as f:
                        data = json.load(f)
        except Exception:
            logger.debug("Presets: failed to read project.json from %s",
                         topos_path)
        self._preset_meta_cache[topos_path] = data
        return data

    # --- Preset Scanning ---

    def get_current_folder(self) -> str:
        """Return the currently active folder path."""
        return self._current_folder

    # --- G11: 用户级显示偏好（版本化、原子落盘） ---

    def _load_preferences(self) -> None:
        """载入偏好并应用到 chrome（zoom/视图模式/排序）；挂保存钩子。"""
        from src.features.color.core.gallery_preferences import (
            load_preferences,
        )
        prefs = load_preferences(self.presets_root)
        self._preferences = prefs
        grid = self._gallery.grid
        grid.set_thumbnail_width(prefs.thumbnail_width)
        self.zoom_slider.setValue(prefs.thumbnail_width)
        self._gallery.set_view_mode(prefs.view_mode)
        if prefs.sort_desc:
            from PyQt6.QtCore import Qt as _Qt
            self._gallery.sort(_Qt.SortOrder.DescendingOrder)
        # 变更钩子（写盘去抖 500ms；原子写不阻塞交互）
        from PyQt6.QtCore import QTimer
        self._prefs_save_timer = QTimer(self)
        self._prefs_save_timer.setSingleShot(True)
        self._prefs_save_timer.setInterval(500)
        self._prefs_save_timer.timeout.connect(self._flush_preferences)
        self.zoom_slider.valueChanged.connect(self._schedule_prefs_save)
        chrome = self._gallery
        for button in (getattr(chrome, "grid_button", None),
                       getattr(chrome, "list_button", None)):
            if button is not None:
                button.toggled.connect(self._schedule_prefs_save)
        sort_menu = getattr(chrome, "sort_menu", None)
        if sort_menu is not None:
            sort_menu.triggered.connect(self._schedule_prefs_save)

    def _schedule_prefs_save(self, *args) -> None:
        self._prefs_save_timer.start()

    def _flush_preferences(self) -> None:
        """把当前 chrome 状态收进偏好并原子落盘（失败只记录）。"""
        from src.features.color.core.gallery_preferences import (
            GalleryPreferences,
            save_preferences,
        )
        sidebar = getattr(self._gallery, "albums", None)
        prefs = GalleryPreferences(
            thumbnail_width=self.zoom_slider.value(),
            view_mode="list" if self._gallery.grid.list_mode else "grid",
            sort_desc=self._gallery._sort_order ==
            Qt.SortOrder.DescendingOrder,
            sidebar_visible=sidebar.isVisible() if sidebar is not None
            else True,
        )
        self._preferences = prefs
        if not save_preferences(self.presets_root, prefs):
            logger.warning("Presets: gallery preferences save failed")

    # --- G11: 外部变化同步（可见性驱动＋mtime 失效，不轮询磁盘） ---

    def showEvent(self, event) -> None:
        """面板重新可见时同步外部变化。

        BR04：按**分层文件签名**比较——目录 mtime 检测不到的原地修改
        （meta/JPEG/EXR/grade 改写）也会触发重扫；未变化时零成本，也不
        重建视图（保留当前项与选中）。"""
        super().showEvent(event)
        if getattr(self, "_gallery", None) is None:
            return
        from src.features.color.core.gallery_query_service import (
            GalleryQueryIndex,
        )
        signature = GalleryQueryIndex.folder_signature(self._current_folder)
        if signature != getattr(self, "_last_scan_signature", None):
            self.scan_presets()

    def _selected_keys(self):
        """选中键集合：优先 still_id（BR04——rename 后选择跟随身份），
        无 meta/ID 的降级条目回退路径（明确的迁移回退，不猜）。"""
        keys = set()
        for item in self.list_widget.selectedItems():
            meta = item.data(Qt.ItemDataRole.UserRole + 1)
            still_id = getattr(meta, "still_id", None)
            if still_id:
                keys.add(f"id:{still_id}")
            else:
                keys.add(f"path:{item.data(Qt.ItemDataRole.UserRole)}")
        return keys

    def _restore_selection(self, keys) -> None:
        """重扫后按 still_id 恢复仍在场的选中（无 ID 回退路径）。"""
        if not keys:
            return
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            meta = item.data(Qt.ItemDataRole.UserRole + 1)
            still_id = getattr(meta, "still_id", None)
            key = (f"id:{still_id}" if still_id else
                   f"path:{item.data(Qt.ItemDataRole.UserRole)}")
            if key in keys:
                item.setSelected(True)

    # --- G01: 能力宿主接入（未安装时保持旧的恒启用行为） ---

    def set_gallery_capability_host(self, host) -> None:
        """由 ``GalleryCapabilityHost`` 安装／解除（None 清除）。

        BR05：宿主**关闭**时解除（``host.shutdown`` 已置位）与"从未
        安装"不同——关闭态下统一执行门拒绝一切动作（窗口正在关闭），
        未安装保持旧的恒启用行为。
        """
        previous = getattr(self, "_gallery_capability_host", None)
        if host is not None:
            self._gallery_actions_dead = False
        elif previous is not None and getattr(previous, "shutdown", False):
            self._gallery_actions_dead = True
        self._gallery_capability_host = host

    def gallery_action_caps(self):
        """当前能力快照；宿主未安装时返回 None（菜单不门控）。"""
        host = self._gallery_capability_host
        return getattr(host, "last_capabilities", None) if host else None

    def _apply_action_capability(self, action_key: str, action) -> None:
        """按能力注册表回填 QAction 的 enabled／tooltip（无宿主跳过）。"""
        caps = self.gallery_action_caps()
        if not caps:
            return
        cap = caps.get(action_key)
        if cap is None:
            return
        action.setEnabled(cap.supported)
        if not cap.supported and cap.reason:
            action.setToolTip(cap.reason)

    # --- BR05: 统一执行门 + 传输/Label 服务接线 ---

    _ACTION_SIGNALS = ("apply.grade", "apply.categories", "reference.load",
                       "match.color")

    def _execute_gallery_action(self, action_key: str,
                                topos_path: object) -> bool:
        """统一执行门（BR05）：菜单／双击／Enter／中键的单一入口。

        执行前按**被操作对象**（非碰巧的选中项）重新评估权威上下文
        （媒体在场／目标 grade 探测／album 可写／宿主关闭态）；拒绝时
        原因进 tooltip 诊断并返回 False，不 emit 动作信号——同一坏包／
        无媒体／关闭态下所有入口结论一致。宿主未安装时保持旧的恒启用
        行为（文件在场即执行，与菜单门控同一约定）。
        """
        if not isinstance(topos_path, str) or not os.path.isfile(topos_path):
            return False
        if getattr(self, "_gallery_actions_dead", False):
            return False          # 宿主已关闭（窗口关闭序列）：一切动作拒绝
        host = self._gallery_capability_host
        if host is not None:
            cap = host.evaluate_targeted(action_key, topos_path)
            if not cap.supported:
                logger.info("gallery action %s rejected for %s: %s",
                            action_key, topos_path, cap.reason)
                return False
        signal = {"apply.grade": self.grade_applied,
                  "apply.categories": self.apply_categories_requested,
                  "reference.load": self.reference_requested,
                  "match.color": self.match_requested}.get(action_key)
        if signal is None:
            return False
        signal.emit(topos_path)
        return True

    def _import_bundle_interactive(self) -> None:
        """Import Still 入口（BR05）：文件选择 → 导入 → 失败弹窗（交互层）。"""
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, I18N.tr("gallery.import_title", "Import Still Bundle"),
            "", "Topos Still Bundle (*.toposbundle *.zip)")
        if not path:
            return
        result = self._import_bundle_from(path)
        if not result.succeeded:
            QMessageBox.warning(
                self, I18N.tr("gallery.import_failed", "Import Failed"),
                I18N.tr("gallery.import_failed_detail",
                        f"Import failed ({result.reason}): "
                        + "; ".join(result.warnings)))

    def _import_bundle_from(self, zip_path: str) -> object:
        """经传输服务导入 bundle（纯执行，无对话框——可编程/测试安全）。

        成功后失效缓存、重扫并选中新条目；失败不改原选择，结果如实
        返回（弹窗属交互包装层）。
        """
        folder = self._current_folder
        result = self._transfer_service.import_bundle(zip_path, folder)
        if not result.succeeded:
            logger.info("gallery import rejected: %s (%s)", result.reason,
                        "; ".join(result.warnings))
            return result
        self._query_service.invalidate(
            self._repository.album_id_for_folder(folder))
        self.scan_presets()
        self._select_items_by_stems(result.affected_still_ids)
        return result

    def _export_bundle_interactive(self, topos_path: str) -> None:
        """Export Bundle 入口（BR05）：选择目标 → 导出（源不动）。"""
        from PyQt6.QtWidgets import QFileDialog
        stem = os.path.splitext(os.path.basename(topos_path))[0]
        suggested = os.path.join(
            os.path.expanduser("~"), f"{stem}.toposbundle")
        dest, _ = QFileDialog.getSaveFileName(
            self, I18N.tr("gallery.export_title", "Export Still Bundle"),
            suggested, "Topos Still Bundle (*.toposbundle)")
        if not dest:
            return
        result = self._transfer_service.export_bundle(
            os.path.dirname(topos_path), stem, dest)
        if result.succeeded:
            QMessageBox.information(
                self, I18N.tr("gallery.export_done", "Exported"),
                I18N.tr("gallery.export_done_detail",
                        f"Exported {stem} → {dest}"))
        else:
            QMessageBox.warning(
                self, I18N.tr("gallery.export_failed", "Export Failed"),
                I18N.tr("gallery.export_failed_detail",
                        f"Export failed ({result.reason}): "
                        + "; ".join(result.warnings)))

    def _set_still_label(self, topos_path: str,
                         label: Optional[str] = None) -> bool:
        """Label 服务（BR05）：独立 label 落盘到 meta sidecar。

        空 label＝清除；保存成功后失效该相册缓存并重扫（分层签名对
        原地写敏感，双保险）。失败如实返回 False。
        """
        from src.features.color.core.preset_meta import (
            load_preset_meta, save_preset_meta,
        )
        meta = load_preset_meta(topos_path)
        if meta is None:
            return False
        meta.label = (label or "").strip() or None
        if not save_preset_meta(topos_path, meta):
            return False
        self._query_service.invalidate(
            self._repository.album_id_for_folder(
                os.path.dirname(topos_path)))
        self.scan_presets()
        return True

    def _set_label_interactive(self, topos_path: str) -> None:
        current = ""
        from src.features.color.core.preset_meta import load_preset_meta
        meta = load_preset_meta(topos_path)
        if meta is not None and meta.label:
            current = meta.label
        label, ok = QInputDialog.getText(
            self, I18N.tr("gallery.set_label_title", "Set Label"),
            I18N.tr("gallery.set_label", "Label (empty to clear):"),
            text=current)
        if not ok:
            return
        if not self._set_still_label(topos_path, label):
            QMessageBox.warning(
                self, I18N.tr("gallery.label_failed", "Label Not Saved"),
                I18N.tr("gallery.label_failed_detail",
                        "Could not save the label to the still's meta "
                        "sidecar."))

    def _select_items_by_stems(self, stems) -> None:
        """按 stem 选中条目（导入/改名成功后选中新条目；失败不动选择）。"""
        wanted = {os.path.normcase(f"{stem}.topos") for stem in stems}
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            name = os.path.basename(
                str(item.data(Qt.ItemDataRole.UserRole) or ""))
            if os.path.normcase(name) in wanted:
                item.setSelected(True)
                self.list_widget.setCurrentItem(item)
            else:
                item.setSelected(False)

    def scan_presets(self, force: bool = False) -> None:
        """Scan current folder for .topos preset files and populate the grid.

        G01：磁盘读取经 ``GalleryFolderRepository.query``（与能力宿主共用
        同一查询事实源）；条目全部入列后按 matches_query 置 hidden——
        不把不匹配条目从模型中剔除，重扫后过滤状态可回放。

        BR04：失效按**分层文件签名**（逐条目 stat 多重集——原地改
        meta/sidecar 不改目录 mtime 也能检测）；``force=True`` 显式失效
        强制重建（手动刷新）。相册条目数达到 ``_async_query_threshold``
        时走后台 runner（同 album 合并保留最新＋代际守卫，结果经队列化
        桥回 GUI 线程），小相册保持同步路径。
        """
        from src.features.color.core.gallery_contracts import GalleryQuery
        from src.features.color.core.gallery_query_service import (
            GalleryQueryIndex,
        )
        folder = self._current_folder
        signature = GalleryQueryIndex.folder_signature(folder)
        self._last_scan_signature = signature
        if force:
            try:
                self._query_service.invalidate(
                    self._repository.album_id_for_folder(folder))
            except Exception:
                self._query_service.invalidate()
        if not os.path.isdir(folder):
            self.list_widget.clear()
            self._preset_meta_cache.clear()
            self.delegate.clear_thumbnail_cache()
            return
        try:
            album_id = self._repository.album_id_for_folder(folder)
        except Exception:
            album_id = None
        if not album_id:
            return
        try:
            entry_count = int(str(signature).split(":", 1)[0])
        except ValueError:
            entry_count = 0
        runner = self._query_service.runner
        if runner is not None and entry_count >= self._async_query_threshold:
            # 大库：后台扫描——用户操作不卡 UI；结果回填前复核代际与相册
            self._scan_generation += 1
            runner.begin_generation()
            runner.submit(GalleryQuery(
                album_id=album_id,
                request_revision=self._scan_generation))
            return
        result = self._query_service.query(GalleryQuery(album_id=album_id))
        self._populate_from_result(result)

    def _populate_from_result(self, result) -> None:
        """按查询结果重建列表（同步/异步回填共用；选择按 still_id 恢复）。"""
        from src.features.color.core.preset_meta import matches_query
        # G11：重扫保留仍在场的选中（外部删除的条目自然清出选择）
        selected_keys = self._selected_keys()
        self.list_widget.clear()
        self._preset_meta_cache.clear()
        # AUD-25513：缩略图 QPixmap 缓存随目录重扫失效（重命名/删除/新增
        # 都可能改变磁盘内容）。
        self.delegate.clear_thumbnail_cache()
        folder = self._current_folder
        for still in result.stills:
            topos_path = os.path.join(folder, still.bundle.topos_name)
            preset_name = still.display_name

            item = QListWidgetItem(preset_name)
            item.setData(Qt.ItemDataRole.UserRole, topos_path)

            # P1-COLOR-04: Gallery 元数据 sidecar → tooltip + 搜索过滤
            meta = still.meta
            item.setData(Qt.ItemDataRole.UserRole + 1, meta)
            if meta is not None:
                summary = meta.describe()
                if summary:
                    item.setToolTip(f"{preset_name} | {summary}")

            # AUD-25517: 不再在扫描路径为每个缩略图 eagerly 解码一次
            # QPixmap（仅用于 setIcon，而 PresetDelegate.paint 走自己的
            # _thumbnail_pixmap LRU 缓存按需解码，item 图标从未被绘制）——
            # 大库扫描的每条 JPEG 解码成本直接砍半。
            self.list_widget.addItem(item)
            # setHidden 必须在 addItem 之后（插入前设置会被视图重置）
            item.setHidden(
                not matches_query(preset_name, meta, self._search_query))
        # Fresh items carry no sizeHint — pin them to the current column
        # cell so the grid stays flush after a rescan.
        self._gallery.after_scan()
        self._restore_selection(selected_keys)

    def _on_async_query_delivered(self, query, result) -> None:
        """后台扫描回填（GUI 线程）：代际与相册复核通过才重建列表。"""
        try:
            current_album = self._repository.album_id_for_folder(
                self._current_folder)
        except Exception:
            current_album = None
        if query.album_id != current_album:
            return                      # 用户已切走相册：旧结果不回填
        if query.request_revision != self._scan_generation:
            return                      # 已有更新的扫描请求在途
        if result.scan_error is not None:
            logger.warning("gallery async scan failed for %s: %s",
                           query.album_id, result.scan_error)
            return
        self._populate_from_result(result)

    def closeEvent(self, event) -> None:
        """关闭时停掉后台 runner（未执行任务丢弃，不触碰已销毁 UI）。"""
        self._query_service.shutdown()
        super().closeEvent(event)

    def _on_search_changed(self, text: str) -> None:
        """搜索词变化：对现有条目重放过滤（不重扫磁盘）。"""
        from src.features.color.core.preset_meta import matches_query
        self._search_query = text
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            meta = item.data(Qt.ItemDataRole.UserRole + 1)
            item.setHidden(
                not matches_query(item.text(), meta, self._search_query))

        self._column_grid.pin_items()

    # --- Actions ---

    def create_folder(self) -> None:
        """Create a new subfolder in the current preset folder.

        AUD-25515：用户输入的名称先做安全校验（拒绝路径分隔符/../
        控制字符），makedirs 的 OSError 以弹窗报告而不是从槽内抛出。
        """
        name, ok = QInputDialog.getText(
            self,
            I18N.tr("presets.new_folder_title", "New Folder"),
            I18N.tr("presets.folder_name", "Folder Name:")
        )
        if ok and name:
            name = name.strip()
            if not name:
                return
            if not _is_safe_preset_name(name):
                QMessageBox.warning(
                    self,
                    I18N.tr("presets.invalid_name_title", "Invalid Name"),
                    I18N.tr("presets.invalid_name",
                            "Name must not contain path separators, "
                            "'..', or control characters."))
                return
            folder_path = os.path.join(self._current_folder, name)
            if os.path.exists(folder_path):
                QMessageBox.warning(self, I18N.tr("presets.exists", "Already Exists"),
                                    I18N.tr("presets.folder_exists", "A folder with this name already exists."))
                return
            try:
                os.makedirs(folder_path, exist_ok=True)
            except OSError as exc:
                logger.warning("Presets: create folder %s failed: %s",
                               folder_path, exc)
                QMessageBox.critical(
                    self,
                    I18N.tr("presets.create_failed_title", "Create Failed"),
                    I18N.tr("presets.create_folder_failed",
                            f"Could not create folder: {exc}"))
                return
            self._populate_folder_combo()
            self._current_folder = folder_path
            self._populate_folder_combo()
            self.scan_presets()

    # --- Sidecar file management (P1-COLOR-04；G03 起走 bundle 服务) ---

    # 单一 manifest 来源（gallery_contracts）：含旧清单遗漏的 .still.exr.json
    _SIDECAR_SUFFIXES = tuple(
        suffix for suffix in BUNDLE_SUFFIX_BY_ROLE.values()
        if suffix != ".topos")

    @classmethod
    def _sibling_paths(cls, topos_path: str):
        base = os.path.splitext(topos_path)[0]
        return [f"{base}{s}" for s in cls._SIDECAR_SUFFIXES]

    def _delete_preset_files(self, topos_path: str, *,
                             permanent: bool = False):
        """删除 bundle：默认移入可恢复 trash；``permanent`` 为显式直删。

        G03：不再面板内逐文件 ``os.remove`` 吞 OSError；结果带逐文件
        失败（``partial``），调用方据此如实回填。
        """
        folder = os.path.dirname(topos_path)
        stem = os.path.splitext(os.path.basename(topos_path))[0]
        return self._bundle_service.delete_bundle(
            folder, stem, permanent=permanent)

    def _delete_selected(self) -> None:
        """Delete the selected preset(s)（G03：移入可恢复 trash）。"""
        items = self.list_widget.selectedItems()
        if not items:
            return
        names = ", ".join(item.text() for item in items)
        reply = QMessageBox.question(
            self,
            I18N.tr("presets.delete_title", "Delete Preset"),
            I18N.tr("presets.delete_confirm", f"Delete preset(s): {names}?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            failures = self._delete_items_to_trash(items)
            self.scan_presets()
            if failures:
                # G03 门槛：删除后刷新不假称全部成功。
                QMessageBox.warning(
                    self,
                    I18N.tr("presets.delete_partial_title",
                            "Deleted with Errors"),
                    I18N.tr("presets.delete_partial",
                            "Some files could not be moved to the "
                            "recoverable trash:\n") + "\n".join(failures))

    def _delete_items_to_trash(self, items) -> list:
        failures: list = []
        for item in items:
            topos_path = item.data(Qt.ItemDataRole.UserRole)
            if not (topos_path and os.path.isfile(topos_path)):
                continue
            result = self._delete_preset_files(topos_path)
            if result is not None and result.status != "success":
                failures.extend(result.warnings or (result.reason or "",))
            else:
                # BR06：删除成功 → 通知比较会话失效在场参考
                self.bundle_mutated.emit("deleted", topos_path)
        return failures

    def _rename_preset(self, item) -> None:
        """Rename a preset（整个 bundle 单事务，见 GalleryBundleService）。

        AUD-25515：新名称先做安全校验；G03 起改名经统一 bundle 事务——
        前置冲突验证先于文件动作，journal 可恢复，失败按稳定 reason 回填。
        """
        old_name = item.text()
        new_name, ok = QInputDialog.getText(
            self,
            I18N.tr("presets.rename_title", "Rename Preset"),
            I18N.tr("presets.new_name", "New Name:"),
            text=old_name
        )
        if not (ok and new_name and new_name != old_name):
            return
        new_name = new_name.strip()
        if not new_name:
            return
        if not _is_safe_preset_name(new_name):
            QMessageBox.warning(
                self,
                I18N.tr("presets.invalid_name_title", "Invalid Name"),
                I18N.tr("presets.invalid_name",
                        "Name must not contain path separators, "
                        "'..', or control characters."))
            return

        topos_path = item.data(Qt.ItemDataRole.UserRole)
        if not topos_path:
            return
        folder = os.path.dirname(topos_path)

        # G03：整个 bundle（.topos + 全部 sidecar，含 .still.exr.json）经
        # GalleryBundleService 单事务改名——前置验证（同名/大小写变体/孤立
        # sidecar/只读/symlink）先于任何文件动作；中断可由 journal 恢复，
        # 失败按稳定 reason 如实回填。
        result = self._bundle_service.rename_bundle(
            folder, old_name, new_name)
        if not result.succeeded:
            detail = "; ".join(result.warnings) if result.warnings else ""
            logger.warning("Presets: rename '%s' -> '%s' rejected (%s): %s",
                           old_name, new_name, result.reason, detail)
            QMessageBox.critical(
                self,
                I18N.tr("presets.rename_failed_title", "Rename Failed"),
                I18N.tr("presets.rename_failed_structured",
                        f"Could not rename preset ({result.reason})."
                        + (f"\n{detail}" if detail else "")))
            return
        new_topos = os.path.join(folder, f"{new_name}.topos")
        # G02：rename 是对旧条目的首次写入——原子补齐稳定 still_id（旧
        # 条目从此跨改名身份不变）。补写失败不回滚文件改名（那是用户刚
        # 确认过的动作），但必须如实告知，不假装已保存。
        from src.features.color.core.preset_meta import ensure_still_identity
        still_id, backfilled = ensure_still_identity(new_topos)
        if still_id is None:
            logger.warning(
                "Presets: identity backfill after rename '%s' -> '%s' "
                "failed; still metadata not saved", old_name, new_name)
            QMessageBox.warning(
                self,
                I18N.tr("presets.rename_done_with_warnings_title",
                        "Renamed with a Warning"),
                I18N.tr("presets.identity_backfill_failed",
                        "Renamed, but saving the still's stable ID failed. "
                        "The still keeps working; its identity may change "
                        "on the next rename."))
        # BR05：成功后选中新条目（失败路径不改原选择——上面已 return）
        self.bundle_mutated.emit("renamed", topos_path)   # 旧路径（BR06）
        self._query_service.invalidate(
            self._repository.album_id_for_folder(folder))
        self.scan_presets()
        self._select_items_by_stems((new_name,))

    # --- Context Menu ---

    def _show_context_menu(self, pos) -> None:
        """Show right-click context menu.

        G01：菜单打开前先让能力宿主做一次新鲜评估（媒体在场／album 可
        写／选中项 grade 探测），逐项回填 enabled／原因 tooltip；宿主未
        安装时保持旧的恒启用行为。
        """
        host = self._gallery_capability_host
        if host is not None:
            host.refresh()
        item = self.list_widget.itemAt(pos)
        menu = QMenu(self)

        if item:
            topos_path = item.data(Qt.ItemDataRole.UserRole)

            apply_act = menu.addAction(I18N.tr("presets.apply_grade", "Apply Grade"))
            apply_act.triggered.connect(
                lambda: self._execute_gallery_action("apply.grade",
                                                     topos_path))
            self._apply_action_capability("apply.grade", apply_act)

            # P1-COLOR-04: 分类应用（Paste Attributes）与参考帧对比
            apply_cats_act = menu.addAction(
                I18N.tr("presets.apply_categories", "Apply Grade (Categories)…"))
            apply_cats_act.triggered.connect(
                lambda: self._execute_gallery_action("apply.categories",
                                                     topos_path))
            self._apply_action_capability("apply.categories", apply_cats_act)

            reference_act = menu.addAction(
                I18N.tr("presets.load_reference", "Load as Reference Wipe"))
            reference_act.triggered.connect(
                lambda: self._execute_gallery_action("reference.load",
                                                     topos_path))
            self._apply_action_capability("reference.load", reference_act)

            compare_act = menu.addAction(
                I18N.tr("gallery.split_screen", "Split Screen") + "…")
            compare_act.triggered.connect(lambda: self.open_comparison("split"))
            self._apply_action_capability("compare.split", compare_act)

            # P1-COLOR-21: Color Match（增量建议 + 可撤销应用）
            match_act = menu.addAction(
                I18N.tr("presets.match_to_still", "Color Match to This Still"))
            match_act.triggered.connect(
                lambda: self._execute_gallery_action("match.color",
                                                     topos_path))
            self._apply_action_capability("match.color", match_act)

            menu.addSeparator()

            rename_act = menu.addAction(I18N.tr("presets.rename", "Rename"))
            rename_act.triggered.connect(lambda: self._rename_preset(item))
            self._apply_action_capability("rename.bundle", rename_act)

            label_act = menu.addAction(
                I18N.tr("gallery.set_label_title", "Set Label…"))
            label_act.triggered.connect(
                lambda: self._set_label_interactive(topos_path))
            self._apply_action_capability("label.rename", label_act)

            export_bundle_act = menu.addAction(
                I18N.tr("gallery.export_bundle", "Export Bundle…"))
            export_bundle_act.triggered.connect(
                lambda: self._export_bundle_interactive(topos_path))
            self._apply_action_capability("export.bundle",
                                          export_bundle_act)

            export_act = menu.addAction(I18N.tr("presets.export_thumb", "Export Thumbnail"))
            export_act.triggered.connect(lambda: self._export_thumbnail(topos_path))
            self._apply_action_capability("export.thumbnail", export_act)

            menu.addSeparator()

            delete_act = menu.addAction(I18N.tr("presets.delete", "Delete"))
            delete_act.triggered.connect(lambda: self._delete_item(item))
            self._apply_action_capability("delete.bundle", delete_act)
        else:
            # Blank area menu
            new_folder_act = menu.addAction(I18N.tr("presets.new_folder", "New Folder"))
            new_folder_act.triggered.connect(self.create_folder)
            self._apply_action_capability("album.create", new_folder_act)

            capture_act = menu.addAction(I18N.tr("presets.capture_still", "Capture Still"))
            capture_act.triggered.connect(lambda: self.capture_requested.emit())
            self._apply_action_capability("capture.still", capture_act)

            menu.addSeparator()

            refresh_act = menu.addAction(I18N.tr("presets.refresh", "Refresh"))
            refresh_act.triggered.connect(self.scan_presets)

        menu.exec(self.list_widget.mapToGlobal(pos))

    def _delete_item(self, item) -> None:
        """Delete a single preset item（G03：移入可恢复 trash）。"""
        name = item.text()
        reply = QMessageBox.question(
            self,
            I18N.tr("presets.delete_title", "Delete Preset"),
            I18N.tr("presets.delete_confirm", f"Delete preset: {name}?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            failures = self._delete_items_to_trash([item])
            self.scan_presets()
            if failures:
                QMessageBox.warning(
                    self,
                    I18N.tr("presets.delete_partial_title",
                            "Deleted with Errors"),
                    I18N.tr("presets.delete_partial",
                            "Some files could not be moved to the "
                            "recoverable trash:\n") + "\n".join(failures))

    def _export_thumbnail(self, topos_path) -> None:
        """Export the thumbnail image to a user-selected location."""
        from PyQt6.QtWidgets import QFileDialog
        preset_name = os.path.splitext(os.path.basename(topos_path))[0]
        thumb_path = os.path.join(os.path.dirname(topos_path), f"{preset_name}.thumb.jpg")
        if not os.path.exists(thumb_path):
            return
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            I18N.tr("presets.export_thumb", "Export Thumbnail"),
            f"{preset_name}.jpg",
            "JPEG (*.jpg *.jpeg)"
        )
        if save_path:
            # AUD-25517: copy2 遇目标只读/磁盘满/权限不足会抛 OSError——
            # 槽内不再裸奔，弹窗告知用户并留日志。
            try:
                shutil.copy2(thumb_path, save_path)
            except OSError as exc:
                logger.warning(
                    "PresetsPanel: 导出缩略图失败 %s -> %s: %s",
                    thumb_path, save_path, exc, exc_info=True)
                QMessageBox.warning(
                    self,
                    I18N.tr("presets.export_thumb_failed_title",
                            "Export Failed"),
                    I18N.tr(
                        "presets.export_thumb_failed",
                        f"Could not export thumbnail: {exc}"),
                )


class PresetDelegate(QStyledItemDelegate):
    """Custom delegate for painting preset thumbnails in the grid."""

    # AUD-25513：paint 每次重绘都从磁盘解码 JPEG 会让滚动/悬停/resize
    # 持续做重复 I/O；按 topos_path 做有上限的 LRU 缓存（含"无缩略图"
    # 的负结果，避免反复 exists 探测）。
    _THUMB_CACHE_LIMIT = 256
    _THUMB_CACHE_MISS = object()

    def __init__(self, panel, parent=None) -> None:
        super().__init__(parent)
        self.panel = panel
        self._thumb_cache: OrderedDict[str, Optional[QPixmap]] = OrderedDict()

    def clear_thumbnail_cache(self) -> None:
        """Drop cached thumbnails (called when the folder is rescanned)."""
        self._thumb_cache.clear()

    def _thumbnail_pixmap(self, topos_path: str) -> Optional[QPixmap]:
        """Return the preset's thumbnail pixmap, or None if unavailable.

        Results (including misses) are memoized per topos_path; disk is hit
        at most once per entry between rescans.
        """
        cached = self._thumb_cache.get(topos_path, self._THUMB_CACHE_MISS)
        if cached is not self._THUMB_CACHE_MISS:
            self._thumb_cache.move_to_end(topos_path)
            return cached
        pixmap: Optional[QPixmap] = None
        preset_name = os.path.splitext(os.path.basename(topos_path))[0]
        thumb_path = os.path.join(os.path.dirname(topos_path),
                                  f"{preset_name}.thumb.jpg")
        if os.path.exists(thumb_path):
            loaded = QPixmap(thumb_path)
            if not loaded.isNull():
                pixmap = loaded
        self._thumb_cache[topos_path] = pixmap
        while len(self._thumb_cache) > self._THUMB_CACHE_LIMIT:
            self._thumb_cache.popitem(last=False)
        return pixmap

    def paint(self, painter: QPainter, option: object, index: object) -> None:
        from src.features.color.ui.gallery_chrome import paint_gallery_item

        paint_gallery_item(painter, option, index, self)

    def _draw_placeholder(self, painter, rect) -> None:
        """Draw a placeholder for missing thumbnails."""
        painter.setBrush(QBrush(QColor(30, 30, 30)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, 6, 6)
        painter.setPen(QColor(80, 80, 80))
        font = painter.font()
        font.setPointSize(16)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "📷")

    def sizeHint(self, option: object, index: object) -> QSize:
        hint = index.data(Qt.ItemDataRole.SizeHintRole)
        if isinstance(hint, QSize) and hint.isValid():
            return hint
        return QSize(108, 27 if self.panel._gallery.grid.list_mode else 98)

    def editorEvent(self, event, model, option, index):
        """Handle middle-click to apply grade（BR05：走统一执行门）。"""
        if event.type() == event.Type.MouseButtonRelease:
            if event.button() == Qt.MouseButton.MiddleButton:
                topos_path = index.data(Qt.ItemDataRole.UserRole)
                if self.panel._execute_gallery_action("apply.grade",
                                                      topos_path):
                    return True
        return super().editorEvent(event, model, option, index)
