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
from PyQt6.QtCore import (QStandardPaths, pyqtSignal, Qt, QSize, QRect)
from PyQt6.QtGui import (QPainter, QColor, QBrush, QPen, QPixmap, QLinearGradient)
from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.assets.icon_manager import get_icon
from src.features.color.core.gallery_contracts import BUNDLE_SUFFIX_BY_ROLE

logger = logging.getLogger(__name__)


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
        self._repository = GalleryFolderRepository(self.presets_root)
        # G03：rename/delete 统一走 bundle 事务服务（不再面板内逐文件操作）
        from src.features.color.core.gallery_bundle_service import (
            GalleryBundleService,
        )
        self._bundle_service = GalleryBundleService(self.presets_root)
        self._gallery_capability_host = None
        self._init_ui()
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

    # --- G01: 能力宿主接入（未安装时保持旧的恒启用行为） ---

    def set_gallery_capability_host(self, host) -> None:
        """由 ``GalleryCapabilityHost`` 安装／解除（None 清除）。"""
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

    def scan_presets(self) -> None:
        """Scan current folder for .topos preset files and populate the grid.

        G01：磁盘读取经 ``GalleryFolderRepository.query``（与能力宿主共用
        同一查询事实源）；条目构建／UserRole／tooltip／搜索隐藏重放与旧
        实现逐项一致（条目全部入列后按 matches_query 置 hidden——不把不
        匹配条目从模型中剔除，重扫后过滤状态可回放）。
        """
        from src.features.color.core.gallery_contracts import GalleryQuery
        from src.features.color.core.preset_meta import matches_query
        self.list_widget.clear()
        self._preset_meta_cache.clear()
        # AUD-25513：缩略图 QPixmap 缓存随目录重扫失效（重命名/删除/新增
        # 都可能改变磁盘内容）。
        self.delegate.clear_thumbnail_cache()
        folder = self._current_folder
        if not os.path.isdir(folder):
            return

        result = self._repository.query(GalleryQuery(
            album_id=self._repository.album_id_for_folder(folder),
        ))
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
        self.scan_presets()

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
            apply_act.triggered.connect(lambda: self.grade_applied.emit(topos_path))
            self._apply_action_capability("apply.grade", apply_act)

            # P1-COLOR-04: 分类应用（Paste Attributes）与参考帧对比
            apply_cats_act = menu.addAction(
                I18N.tr("presets.apply_categories", "Apply Grade (Categories)…"))
            apply_cats_act.triggered.connect(
                lambda: self.apply_categories_requested.emit(topos_path))
            self._apply_action_capability("apply.categories", apply_cats_act)

            reference_act = menu.addAction(
                I18N.tr("presets.load_reference", "Load as Reference Wipe"))
            reference_act.triggered.connect(
                lambda: self.reference_requested.emit(topos_path))
            self._apply_action_capability("reference.load", reference_act)

            compare_act = menu.addAction(
                I18N.tr("gallery.split_screen", "Split Screen") + "…")
            compare_act.triggered.connect(lambda: self.open_comparison("split"))
            self._apply_action_capability("compare.split", compare_act)

            # P1-COLOR-21: Color Match（增量建议 + 可撤销应用）
            match_act = menu.addAction(
                I18N.tr("presets.match_to_still", "Color Match to This Still"))
            match_act.triggered.connect(
                lambda: self.match_requested.emit(topos_path))
            self._apply_action_capability("match.color", match_act)

            menu.addSeparator()

            rename_act = menu.addAction(I18N.tr("presets.rename", "Rename"))
            rename_act.triggered.connect(lambda: self._rename_preset(item))
            self._apply_action_capability("rename.bundle", rename_act)

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
        """Handle middle-click to apply grade."""
        if event.type() == event.Type.MouseButtonRelease:
            if event.button() == Qt.MouseButton.MiddleButton:
                topos_path = index.data(Qt.ItemDataRole.UserRole)
                if topos_path and os.path.isfile(topos_path):
                    self.panel.grade_applied.emit(topos_path)
                    return True
        return super().editorEvent(event, model, option, index)
