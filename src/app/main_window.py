from __future__ import annotations

import sys
import os
import logging
import threading
from typing import TYPE_CHECKING, Any, Optional
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import (QMainWindow, QWidget,
                             QMessageBox,
                             QComboBox, QApplication,
                             QLineEdit, QTextEdit,
                             QPlainTextEdit, QAbstractSpinBox)
from PyQt6.QtCore import Qt, QTimer, QEvent, pyqtSignal

from src.app.controllers.edit_controller import EditControllerMixin
from src.app.controllers.deliver_controller import DeliverControllerMixin
from src.app.controllers.color_controller import ColorControllerMixin
from src.app.controllers.composite_controller import CompositeControllerMixin
from src.app.controllers.menu_toolbar_controller import MenuToolbarMixin
from src.app.controllers.page_controller import PageManagementMixin
from src.app.controllers.dock_layout_controller import DockLayoutMixin
from src.app.controllers.layer_controller import LayerOpsMixin
from src.app.controllers.color_workspace_controller import ColorWorkspaceMixin
from src.app.controllers.file_controller import FileOpsMixin
from src.app.controllers.ai_editing_mixin import AiEditingMixin

logger = logging.getLogger(__name__)


def _config_bool(value, default: bool = False) -> bool:
    """Safely read boolean config value, handling None correctly.

    Unlike `value or default`, this returns False when value is explicitly False.
    """
    return default if value is None else bool(value)


# --- Category 2: Imports needed in __init__ (module-level) ---
from src.features.color.core.image_engine import ImageEngine
from src.shared.io.file_io import FileIO
from src.shared.ui.style_sheet import DARK_THEME
from src.shared.ui.panel_divider import paint_shared_dock_dividers
from src.shared.db.database import Database
from src.shared.config.config_manager import ConfigManager
from src.shared.i18n.i18n import I18N
from src.shared.ui.shortcut_manager import ShortcutManager
from src.app.update_checker import UpdateChecker, CURRENT_VERSION

# Edit Project IO essentials (used in __init__ before create_panels)
try:
    from src.features.edit.commands_facade import (
        get_project_io_classes,
    )
    _io = get_project_io_classes()
    if _io is None:
        raise ImportError("Edit project_io facade unavailable")
    EditProjectIO = _io["EditProjectIO"]
    EditProjectIOConfig = _io["EditProjectIOConfig"]
    UIState = _io["UIState"]
    EDIT_PROJECT_EXTENSION = _io["EDIT_PROJECT_EXTENSION"]
    EDIT_MODULE_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Edit module not available: {e}")
    EditProjectIO = None
    EditProjectIOConfig = None
    UIState = None
    EDIT_PROJECT_EXTENSION = None
    EDIT_MODULE_AVAILABLE = False
from src.shared.project.save_router import SaveRouter  # P9.1: 统一保存路由器
from src.shared.feature_flags import FeatureFlags  # Phase 1: Feature flags
from src.app.feature_registry import FeatureRegistry  # Phase 3-4: Feature registry

if TYPE_CHECKING:  # pragma: no cover - annotation-only imports
    from src.features.edit.core.project import EditProject
    from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer

# 导入内置视频播放器（可选组件；Qt Multimedia 缺失时提示不可用，绝不回退系统播放器）
external_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'external', 'file_explorer')
# [R2-1] append 而非 insert(0)：该目录含 main.py/const.py/utils.py 等通用名
# 模块，置于 sys.path 头部会让任何裸 `import main/const/utils` 命中
# file_explorer 的——低频但极难排查的隐形地雷。FE 的 `plugins/` 是正规包
# （有 __init__.py），根目录 plugins/ 是命名空间包，正规包优先级与
# sys.path 顺序无关，append 不影响播放器导入。
sys.path.append(external_path)

try:
    # 9a8bfd99 插件化后播放器迁至 plugins/video_pro/player_qt.py（旧 player.py 已删除）；
    # 预导入以探测 QtMultimedia 组件存在，异常时仅置 PLAYER_AVAILABLE=False。
    from PyQt6.QtMultimedia import QMediaPlayer  # noqa: F401
    from plugins.video_pro.player_qt import UltimatePlayer  # noqa: F401 — 播放器可用性探测（置 PLAYER_AVAILABLE）
    PLAYER_AVAILABLE = True
except ImportError as e:
    logger.warning(f"UltimatePlayer unavailable, 视频将无法播放（不回退系统播放器）: {e}")
    PLAYER_AVAILABLE = False

# ================================================================
# PageContext - 页面切换上下文管理 (P9)
# ================================================================

from dataclasses import dataclass
from enum import Enum


class PageType(Enum):
    """页面类型枚举。"""
    LIBRARY = "Library"
    EDIT = "Edit"
    COLOR = "Color"
    COMPOSITE = "Composite"
    DELIVER = "Deliver"


@dataclass
class PageContext:
    """
    页面切换上下文数据。

    用于页面切换时传递必要的状态信息，实现页面间的数据协调。

    Attributes:
        source_page: 来源页面类型
        target_page: 目标页面类型
        clip_id: 当前选中的剪辑 ID（Edit → Color/Composite）
        asset_id: 当前素材 ID（Edit → Color）
        timeline_frame: 时间线上的帧号（playhead 位置）
        source_frame: 源素材帧号
        clip_local_frame: 剪辑本地帧号（相对于剪辑起始）
        color_stack_id: 调色栈 ID（Edit → Color）
        composite_id: 合成 ID（Edit → Composite）
        timeline_id: 时间线 ID（Edit → Deliver）
        deliver_source: 导出来源类型（Color/Edit → Deliver）
    """
    source_page: PageType = PageType.COLOR
    target_page: PageType = PageType.COLOR
    clip_id: str | None = None
    asset_id: str | None = None
    timeline_frame: int | None = None
    source_frame: int | None = None
    clip_local_frame: int | None = None
    color_stack_id: str | None = None
    composite_id: str | None = None
    timeline_id: str | None = None
    deliver_source: str = "color"  # color, composite, timeline

    def to_dict(self) -> dict:
        """序列化为字典。"""
        return {
            "source_page": self.source_page.value,
            "target_page": self.target_page.value,
            "clip_id": self.clip_id,
            "asset_id": self.asset_id,
            "timeline_frame": self.timeline_frame,
            "source_frame": self.source_frame,
            "clip_local_frame": self.clip_local_frame,
            "color_stack_id": self.color_stack_id,
            "composite_id": self.composite_id,
            "timeline_id": self.timeline_id,
            "deliver_source": self.deliver_source,
        }

    @staticmethod
    def from_dict(data: dict) -> PageContext:
        """从字典反序列化。"""
        return PageContext(
            source_page=PageType(data.get("source_page", "Color")),
            target_page=PageType(data.get("target_page", "Color")),
            clip_id=data.get("clip_id"),
            asset_id=data.get("asset_id"),
            timeline_frame=data.get("timeline_frame"),
            source_frame=data.get("source_frame"),
            clip_local_frame=data.get("clip_local_frame"),
            color_stack_id=data.get("color_stack_id"),
            composite_id=data.get("composite_id"),
            timeline_id=data.get("timeline_id"),
            deliver_source=data.get("deliver_source", "color"),
        )


class PageContextManager:
    """
    页面上下文管理器。

    管理页面切换时的上下文状态，包括：
    - 当前页面上下文
    - 页面 dirty 状态追踪
    - 跨页面数据传递

    使用方法：
        manager = PageContextManager()
        context = manager.create_context_for_edit_to_color(clip_id, asset_id, timeline_frame, ...)
        manager.switch_to_page("Color", context)
    """

    def __init__(self):
        self._current_context: PageContext | None = None
        self._page_dirty_states: dict[PageType, bool] = {
            PageType.LIBRARY: False,
            PageType.EDIT: False,
            PageType.COLOR: False,
            PageType.COMPOSITE: False,
            PageType.DELIVER: False,
        }
        self._last_contexts: dict[PageType, PageContext] = {}

    def get_current_context(self) -> PageContext | None:
        """获取当前页面上下文。"""
        return self._current_context

    def set_current_context(self, context: PageContext) -> None:
        """设置当前页面上下文。"""
        self._current_context = context
        # 保存为该页面的最后上下文
        self._last_contexts[context.target_page] = context

    def get_last_context(self, page: PageType) -> PageContext | None:
        """获取指定页面的最后上下文。"""
        return self._last_contexts.get(page)

    def is_page_dirty(self, page: PageType) -> bool:
        """检查页面是否有未保存的更改。"""
        return self._page_dirty_states.get(page, False)

    def set_page_dirty(self, page: PageType, dirty: bool) -> None:
        """设置页面 dirty 状态。"""
        self._page_dirty_states[page] = dirty

    def any_page_dirty(self) -> bool:
        """检查是否有任何页面 dirty。"""
        return any(self._page_dirty_states.values())

    def create_context_for_edit_to_color(
        self,
        clip_id: str | None = None,
        asset_id: str | None = None,
        timeline_id: str | None = None,
        timeline_frame: int | None = None,
        source_frame: int | None = None,
        clip_local_frame: int | None = None,
        color_stack_id: str | None = None,
    ) -> PageContext:
        """
        创建 Edit → Color 切换上下文。

        Args:
            clip_id: 选中的剪辑 ID
            asset_id: 素材 ID
            timeline_id: 时间线 ID
            timeline_frame: 时间线上的帧号（playhead 位置，可选）
            source_frame: 源素材帧号（可选）
            clip_local_frame: 剪辑本地帧号（可选）
            color_stack_id: 调色栈 ID（可选，如无则创建新的）

        Returns:
            PageContext 实例
        """
        return PageContext(
            source_page=PageType.EDIT,
            target_page=PageType.COLOR,
            clip_id=clip_id,
            asset_id=asset_id,
            timeline_id=timeline_id,
            timeline_frame=timeline_frame,
            source_frame=source_frame,
            clip_local_frame=clip_local_frame,
            color_stack_id=color_stack_id,
        )

    def create_context_for_edit_to_composite(
        self,
        clip_id: str,
        composite_id: str | None = None,
        create_new: bool = False,
        asset_id: str | None = None,
        timeline_id: str | None = None,
        timeline_frame: int | None = None,
        source_frame: int | None = None,
        clip_local_frame: int | None = None,
        color_stack_id: str | None = None,
    ) -> PageContext:
        """
        创建 Edit → Composite 切换上下文。

        Args:
            clip_id: 选中的剪辑 ID
            composite_id: 合成 ID（可选）
            create_new: 是否创建新合成

        Returns:
            PageContext 实例
        """
        return PageContext(
            source_page=PageType.EDIT,
            target_page=PageType.COMPOSITE,
            clip_id=clip_id,
            asset_id=asset_id,
            timeline_id=timeline_id,
            timeline_frame=timeline_frame,
            source_frame=source_frame,
            clip_local_frame=clip_local_frame,
            color_stack_id=color_stack_id,
            composite_id=composite_id,
            deliver_source="composite",
        )

    def create_context_for_to_deliver(
        self,
        source: str = "color",
        timeline_id: str | None = None,
    ) -> PageContext:
        """
        创建 Color/Edit → Deliver 切换上下文。

        Args:
            source: 来源类型（color, composite, timeline）
            timeline_id: 时间线 ID（当 source=timeline 时）

        Returns:
            PageContext 实例
        """
        source_page = PageType.COLOR if source == "color" else PageType.EDIT
        return PageContext(
            source_page=source_page,
            target_page=PageType.DELIVER,
            timeline_id=timeline_id,
            deliver_source=source,
        )


from src.app.controllers.stabilizer_controller import StabilizerControllerMixin

class MainWindow(QMainWindow, StabilizerControllerMixin, EditControllerMixin, DeliverControllerMixin, ColorControllerMixin, CompositeControllerMixin, MenuToolbarMixin, PageManagementMixin, DockLayoutMixin, LayerOpsMixin, ColorWorkspaceMixin, FileOpsMixin, AiEditingMixin):
    # AUD-3513: the audio-backend probe runs on a worker thread; the snapshot
    # is delivered back to the UI thread through this queued signal.
    audio_capabilities_probed = pyqtSignal(object)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().paintEvent(event)
        painter = QPainter(self)
        paint_shared_dock_dividers(self, painter)
        painter.end()

    def __init__(self, feature_flags: FeatureFlags = None, feature_registry: FeatureRegistry = None, test_mode: bool = False,
                 startup_project: str = None, startup_no_dialog: bool = False):
        super().__init__()
        self._test_mode = test_mode
        # AUD-3513: worker-thread audio probe results land here on the UI thread.
        self.audio_capabilities_probed.connect(self._apply_audio_capabilities)
        # [R2-10] 命令行启动行为：--project PATH / --no-startup-dialog
        self._startup_project = startup_project
        self._startup_no_dialog = startup_no_dialog
        self.feature_flags = feature_flags or FeatureFlags()
        self.feature_registry = feature_registry  # Phase 4: Feature registry (optional)
        self.color_facade = None  # Phase 6: Color facade (set after engine init)
        # AI editing subsystem state (M1.2 组合根；面板存在后才真正装配)
        self._init_ai_editing_state()
        # N09：工程级自动备份心跳（调度器懒建后由 QTimer 周期拍备；
        # 与磁盘自动保存互不替代——见 snapshot_backup_scheduler 模块注）
        try:
            self._ensure_snapshot_backup()
        except Exception:  # noqa: BLE001 — 备份心跳启动失败不阻断主窗口
            import logging
            logging.getLogger(__name__).exception(
                "[AutoBackup] heartbeat init failed")

        # ActionRegistry for dynamic menu contributions from feature modules
        from src.shared.actions import ActionRegistry
        self._action_registry = ActionRegistry()
        self.setWindowTitle(I18N.tr("app.brand", "Topos"))
        self.resize(1600, 1000)
        self.setStyleSheet(DARK_THEME)

        # --- Apply preference-backed settings on startup ---
        self.config_manager = ConfigManager()
        if self.config_manager.get("show_version_in_title"):
            self.setWindowTitle(f"Topos {CURRENT_VERSION}")
        # Append the active work mode to the title so users always know which
        # workflow they are in (Image/Video/Full). Only studio honors it.
        self._update_title_work_mode()
        self._apply_accent_preview(self.config_manager.get("accent_color") or "#ff8800")
        self._show_panel_titles = self.config_manager.get("show_panel_titles")

        # Disable tooltips if configured
        if self.config_manager.get("show_tooltips") is False:
            self.setToolTipDuration(0)

        # Initialize logging system (console + optional file handler)
        from src.shared.logging.log_manager import LogManager
        self.log_manager = LogManager.initialize(self.config_manager)

        # Initialize Color Management system
        from src.features.color.core.color_manager import ColorManager
        self.color_manager = ColorManager(self.config_manager)

        self.setDockOptions(QMainWindow.DockOption.AnimatedDocks |
                            QMainWindow.DockOption.AllowNestedDocks |
                            QMainWindow.DockOption.AllowTabbedDocks |
                            QMainWindow.DockOption.GroupedDragging)

        # Initialize Page Context Manager (P9)
        self._page_context_manager = PageContextManager()

        # AUD-3507: GPU/引擎初始化失败（显卡驱动缺失、数据库目录不可写等）
        # 不再让进程带着裸 traceback 直接崩溃 —— 明确报错后安全退出，
        # 避免留下半初始化窗口。
        try:
            self.db = Database()
            self.engine = ImageEngine()
            self.engine.history.max_steps = self.config_manager.get("undo_history_limit") or 50
        except Exception as exc:
            self._abort_startup("engine", exc)

        # Initialize Edit Project IO manager
        if EDIT_MODULE_AVAILABLE:
            self.edit_project_io = EditProjectIO(
                EditProjectIOConfig(
                    autosave_interval=self.config_manager.get("auto_save_interval") or 5,
                    autosave_to_temp=True,
                    # Backups are made only by the 15-minute backup timer,
                    # never as a side effect of every auto-save.
                    backup_on_save=False,
                    max_backup_count=3,
                ),
                parent=self
            )
            # Set up callbacks for Edit Project IO
            self.edit_project_io.set_callbacks(
                get_project=self._get_current_edit_project,
                get_ui_state=self._get_edit_ui_state,
                get_timeline_sequences=self._get_timeline_sequences,
                get_teaching_animation_payload=self._get_teaching_animation_payload,
                on_teaching_animation_payload_loaded=self._restore_teaching_animation_payload,
            )
            # AUD-16715：手动保存为"排队成功即清 dirty"的后台写，落盘失败
            # 必须反馈给用户（此前 manual_save_failed 全仓无订阅者）
            self.edit_project_io.manual_save_failed.connect(
                self._on_manual_save_failed)
        else:
            self.edit_project_io = None
        # Track current edit project state
        self._current_edit_project = None
        self._current_edit_project_filepath = None
        self._current_timeline_sequences = {}

        # P9.1: ColorSessionManager - Color 页面会话管理
        from src.features.color.core.color_session import ColorSessionManager
        self._color_session_manager = ColorSessionManager()

        # 跟踪结果应用（阶段 6 集成）：渲染链经此回调读取 clip_id + 实时帧号，
        # 蒙版纹理/画布轮廓按「基准几何 + 跟踪变换」求值绘制。
        self.engine.tracking_context_provider = self._tracker_render_context

        # Initialize SaveRouter - 统一保存路由器 (P9.1)
        self.save_router = SaveRouter()
        self._setup_save_router_callbacks()

        # Timeline Preview Renderer (P2.1)
        self.timeline_preview_renderer: Optional[TimelinePreviewRenderer] = None

        # Initialize Shortcut Manager
        self.shortcut_manager = ShortcutManager.initialize(self.config_manager)
        if self.config_manager.get("global_shortcuts"):
            self.shortcut_manager.set_global_mode(True)

        self.idle_render_timer = QTimer()
        self.idle_render_timer.setSingleShot(True)
        self.idle_render_timer.setInterval(500)
        self.idle_render_timer.timeout.connect(self.perform_idle_render)

        # Mask update throttling
        self.mask_update_timer = QTimer()
        self.mask_update_timer.setSingleShot(True)
        self.mask_update_timer.setInterval(33)
        self.mask_update_timer.timeout.connect(self.process_mask_update)
        self.pending_mask_updates = {}

        # P0-5: Grading debounce timer — coalesces rapid slider changes during video playback
        # into a single lightweight render, avoiding render-queue flooding.
        self._grading_debounce_timer = QTimer(self)
        self._grading_debounce_timer.setSingleShot(True)
        self._grading_debounce_timer.setInterval(25)  # ~40 Hz, responsive yet coalesced
        self._grading_debounce_timer.timeout.connect(self._debounced_grading_render)

        # Prevent recursive titlebar updates (performance fix)
        self._updating_titlebars = False
        self._titlebar_update_timer = QTimer()
        self._titlebar_update_timer.setSingleShot(True)
        self._titlebar_update_timer.setInterval(50)  # Longer debounce for better performance
        self._titlebar_update_timer.timeout.connect(self._perform_update_all_titlebars)

        # Auto-backup timer
        self._backup_timer = QTimer(self)
        self._backup_timer.timeout.connect(self._perform_auto_backup)
        self._start_backup_timer()

        # EditProjectIO owns the single recovery autosave timer. Keeping a
        # second full-save timer here used to cause two complete ZIP writes at
        # the same five-minute boundary.

        # Apply custom cache directory from config
        custom_cache = self.config_manager.get("cache_directory")
        if custom_cache and os.path.isdir(custom_cache):
            FileIO.set_cache_directory(custom_cache)

        # Restore current page from config
        self.page_layouts = {} # Stores bytearray states for each page
        self._floating_docks = set()  # Track which docks are floating

        # Phase 8-9: Feature availability flags (must be set before create_panels)
        self._composite_available = self.feature_flags.is_enabled("composite")
        if self._composite_available:
            try:
                from src.features.composite.facade import get_composite_runtime_class
                _RuntimeCls = get_composite_runtime_class()
                if _RuntimeCls is None:
                    raise ImportError
                self._composite_available = True
            except ImportError:
                logger.warning("Composite module unavailable; Composite feature will be disabled")
                self._composite_available = False

        self._edit_available = EDIT_MODULE_AVAILABLE and self.feature_flags.is_enabled("edit")
        if not self._edit_available:
            logger.warning("Edit module disabled or unavailable")

        self._deliver_available = self.feature_flags.is_enabled("deliver")
        if not self._deliver_available:
            logger.warning("Deliver module disabled or unavailable")

        # Check if saved current_page is available, fallback to Color if disabled.
        # Prefer the work-mode-scoped page so image/video each restore to their
        # own last page (e.g. video → Edit, image → Color) without overwriting.
        try:
            from src.shared.feature_flags import get_effective_layout_mode
            elm = get_effective_layout_mode(self.config_manager)
        except Exception:
            elm = "studio"
        self.current_page = (
            self.config_manager.get(f"current_page_{elm}")
            or self.config_manager.get("current_page")
            or "Color"
        )
        page_lower = self.current_page.lower()
        if page_lower == "edit" and not self._edit_available:
            logger.warning(f"Saved page '{self.current_page}' is disabled, falling back to Color")
            self.current_page = "Color"
        elif page_lower == "composite" and not self._composite_available:
            logger.warning(f"Saved page '{self.current_page}' is disabled, falling back to Color")
            self.current_page = "Color"
        elif page_lower == "deliver" and not self._deliver_available:
            logger.warning(f"Saved page '{self.current_page}' is disabled, falling back to Color")
            self.current_page = "Color"

        if not self._test_mode:
            # AUD-3507: 面板树创建失败同样明确报错后安全退出，不留半初始化窗口。
            try:
                self.create_panels()
            except Exception as exc:
                self._abort_startup("panels", exc)

        # Layout Versioning - Update to support all page layouts (no longer clears old layouts)
        layout_ver = self.db.get_setting("layout_version", "2.0")
        if layout_ver != "4.0":
            self.db.set_setting("layout_version", "4.0")

        # Check if we have saved window state - if so, skip apply_default_layout
        self._has_saved_state = False
        last_geom = self.config_manager.get("window_geometry")
        last_state = self.config_manager.get("window_state")
        if last_geom and last_state:
            self._has_saved_state = True

        if not self._test_mode:
            self.setup_docks()
            self._wire_color_keyframes_host()
            self.setup_menus()
            # Sync mask overlay state between Viewport panel and main window shortcut
            self.canvas.viewport_menu.mask_toggled.connect(
                lambda checked: self.mask_overlay_act.setChecked(checked)
            )
            self.setup_toolbar()

            # 禁用原生菜单栏，由工具栏统一提供菜单入口
            # 共享 QMenu 对象时，原生菜单栏会显示快捷键但工具栏弹出菜单不会
            # 禁用后两者都由 Qt 渲染，快捷键显示一致
            self.menuBar().setNativeMenuBar(False)
            self.menuBar().hide()
            self.setup_statusbar()
            self.setup_connections()
            # M1.2/M1.3/M2.2：装配 AI 编辑子系统（dock 面板已由 setup_docks 创建）
            self._setup_ai_editing()
            QApplication.instance().installEventFilter(self)

            # Initialize default layouts for each page (synchronous - before show())
            self.initialize_page_layouts()

        self.current_file_path = None
        self.current_file_is_video = False  # 跟踪当前是视频还是图像

        # EditProject state management
        self.current_edit_project: Optional['EditProject'] = None
        self.current_edit_project_path: Optional[str] = None
        self.edit_project_dirty: bool = False  # 是否有未保存的更改

        self.setAcceptDrops(True)

        if not self._test_mode:
            self.restore_layout_state()

            self.layer_panel.update_layers(self.engine.layers, self.engine.active_layer_index)

            # Phase 6: Initialize ColorFacade for other modules
            from src.features.color.facade import ColorFacade
            self.color_facade = ColorFacade(self)

            # Restore last opened file - wait for GL context to be initialized
            # to ensure the async renderer is ready before loading images.
            # Prefer the work-mode-scoped entry so image/video workflows each
            # restore their own last file; fall back to the shared key.
            try:
                from src.shared.feature_flags import get_effective_layout_mode
                elm = get_effective_layout_mode(self.config_manager)
            except Exception:
                elm = "studio"
            last_opened_file = (
                self.config_manager.get(f"last_opened_file_{elm}")
                or self.config_manager.get("last_opened_file")
            )
            # Video mode skips auto-restore: it must enter through the Project
            # Manager, so the last file is never auto-loaded on startup.
            # Image mode keeps the Lightroom-style "reopen last image" behavior.
            try:
                from src.shared.feature_flags import get_work_mode as _get_wm
                _video_mode_startup = _get_wm(self.config_manager) == "video"
            except Exception:
                _video_mode_startup = False
            if (not _video_mode_startup
                    and self.config_manager.get("auto_load_last_project")
                    and last_opened_file
                    and os.path.exists(last_opened_file)):
                # Connect to the gl_initialized signal to load file after renderer is ready
                def load_last_file():
                    from src.shared.media.format_registry import is_video as _is_video_file
                    is_video = _is_video_file(last_opened_file)
                    # Work-mode-aware restore: each mode only auto-restores files
                    # of its own kind, so a file opened in the other mode (e.g. an
                    # image opened as a reference in video mode) does not leak into
                    # the wrong workflow on next launch.
                    try:
                        from src.shared.feature_flags import get_work_mode
                        active_mode = get_work_mode(self.config_manager)
                    except Exception:
                        active_mode = "video"
                    if is_video and active_mode == "image":
                        # Image mode has no Edit page — skip video restore silently.
                        logger.info(
                            "[STARTUP] Skipping video restore in Image mode: %s",
                            last_opened_file,
                        )
                        return
                    if not is_video and active_mode == "video":
                        # Video mode is timeline-centric — don't auto-restore a
                        # still image into the Color/Composite pages.
                        logger.info(
                            "[STARTUP] Skipping image restore in Video mode: %s",
                            last_opened_file,
                        )
                        return
                    if is_video:
                        self.load_video(last_opened_file)
                        # On Composite-page startup with a video, the legacy Color
                        # pipeline never emits color_output_changed, so the pending-
                        # swap wait below would always time out. Build the timeline
                        # clip context directly so TimelineClipInputNode can resolve
                        # frames and the Output node produces a valid image.
                        # Deferred to the next event-loop tick to avoid GL/Qt
                        # re-entrancy during initializeGL's signal chain.
                        if self.current_page == "Composite" and self._composite_available:
                            def _establish_ctx():
                                try:
                                    self._establish_composite_timeline_context(last_opened_file)
                                except Exception:
                                    logger.exception("[COMPOSITE-STARTUP] establish timeline context failed")
                            QTimer.singleShot(0, _establish_ctx)
                    else:
                        self.load_image(last_opened_file)

                # Use QueuedConnection to defer file loading to the next event loop
                # iteration. DirectConnection would run load_last_file() inside
                # initializeGL(), blocking the window from ever finishing show().
                from PyQt6.QtCore import Qt
                self.canvas.gl_viewport.gl_initialized.connect(load_last_file, Qt.ConnectionType.QueuedConnection)

            # If starting on Composite page, the canvas MUST stay in the dock
            # until the Color engine finishes its first render (color_output_changed).
            # Only then can we safely swap to CompositeViewer.
            if self.current_page == "Composite" and self._composite_available:
                if self.config_manager.get("auto_load_last_project") and last_opened_file and os.path.exists(last_opened_file):
                    # Detect whether the file is a video. Video files never trigger
                    # color_output_changed from the legacy Color pipeline, so waiting
                    # for it would always time out (2s fallback). For videos we instead
                    # build the timeline clip context directly (see load_last_file above)
                    # and drive the viewer swap from there.
                    from src.shared.media.format_registry import is_video as _is_startup_video_file
                    _is_startup_video = _is_startup_video_file(last_opened_file)
                    if _is_startup_video:
                        # Video: do not wait for color_output_changed. The context-
                        # building path (load_last_file → _establish_composite_timeline_context)
                        # owns the runtime_context setup. Keep a short fallback only to
                        # guarantee the viewer swap + Properties wiring if the context
                        # path fails or is slow.
                        self._composite_startup_pending = True
                        self._composite_enter_needed_on_startup = True
                        logger.info(f"[COMPOSITE-STARTUP] Video startup, building timeline context (file: {last_opened_file})")
                        QTimer.singleShot(2500, self._composite_startup_fallback)
                    else:
                        # Image: wait for color_output_changed to swap.
                        # The canvas stays visible so the Color render pipeline can complete.
                        self._composite_startup_pending = True
                        logger.info(f"[COMPOSITE-STARTUP] Waiting for color_output_changed (file: {last_opened_file})")
                        # Fallback: if color_output_changed doesn't arrive within 2s
                        # (e.g. ImageInput graphs that don't need Color output, or a
                        # slow/failed Color render), force the viewer swap so the user
                        # doesn't see a stale Color canvas. The fallback is a no-op if
                        # color_output_changed already swapped the viewer (it checks
                        # _composite_startup_pending and returns early).
                        QTimer.singleShot(2000, self._composite_startup_fallback)
                        # Run the full Composite activation (enter_page_hook) once the
                        # startup completes, regardless of whether color_output_changed
                        # or the fallback fires. This ensures the Properties panel is
                        # wired up and the graph is bound on first launch.
                        self._composite_enter_needed_on_startup = True
                else:
                    # No file to load — swap to CompositeViewer immediately
                    self._composite_startup_pending = False
                    logger.info("[COMPOSITE-STARTUP] No file to load, swapping immediately")
                    if self.composite_viewer and self.dock_viewport:
                        self.dock_viewport.setWidget(self.composite_viewer)
                    self.composite_page._is_active = True
                    # Run the full Composite activation path so that the
                    # Properties panel gets its widget, dock visibility is
                    # restored, and on_enter() / graph binding run. Without this,
                    # first-launch-on-Composite leaves the Properties panel empty
                    # until the user manually switches to another page and back.
                    try:
                        self.enter_page_hook("Composite")
                    except Exception as e:
                        logger.warning(f"Composite first-launch enter_page_hook failed: {e}")

            # Initialize Library Panel path
            last_path = self.config_manager.get("last_path")
            if last_path:
                self.library_panel.set_root_path(last_path)
                # 同时初始化 Files Panel 的路径，确保切换语言重启后 Files 面板能正确显示内容
                self.files_panel.set_root_path(last_path)

            # Startup dialog: video mode shows the Project Manager (DaVinci
            # style — must pick/create a project before entering the app);
            # image mode keeps the lightweight Welcome dialog.
            # [R2-10] --no-startup-dialog 可整体跳过；--project 直接打开指定项目
            try:
                from src.shared.feature_flags import get_work_mode as _get_wm
                _startup_video_mode = _get_wm(self.config_manager) == "video"
            except Exception:
                _startup_video_mode = False
            if getattr(self, "_startup_no_dialog", False):
                pass
            elif _startup_video_mode:
                QTimer.singleShot(100, lambda: self._show_project_manager(startup=True))
            elif self.config_manager.get("show_welcome_on_startup"):
                QTimer.singleShot(100, self._show_welcome_dialog)

            _startup_project = getattr(self, "_startup_project", None)
            if _startup_project:
                QTimer.singleShot(200, lambda: self._open_project_path(_startup_project))

            # Check for updates on startup
            if self.config_manager.get("check_updates_on_startup"):
                self._update_checker = UpdateChecker(self)
                self._update_checker.update_available.connect(self._on_update_available)
                # [R0-4] 检查失败/无更新此前完全不可见，接上信号给状态栏提示
                self._update_checker.no_update.connect(self._on_no_update)
                self._update_checker.error.connect(self._on_update_error)
                self._update_checker.start()

            # 恢复上次的批量导出队列（若有未完成项）
            # 延迟 800ms，确保窗口完全显示后再弹窗，避免抢启动焦点
            try:
                if self._has_restorable_queue():
                    QTimer.singleShot(800, self._prompt_restore_queue)
            except Exception as e:
                import logging
                logging.getLogger(__name__).debug(f"restore queue check failed: {e}")

            # AM-01: 启动诊断音频后端可用性（PyAV/SciPy/输出设备）。
            # 延迟执行避免抢启动焦点；缺失核心依赖时通过状态栏永久 widget
            # 持续展示降级状态，而非让 EQ/LUFS 静默旁路。
            QTimer.singleShot(600, self._run_audio_backend_diagnostics)

    def _update_title_work_mode(self) -> None:
        """Append the active work mode (Image/Video) to the window title.

        Only the studio product honors work mode; other launchers keep their
        plain title. Every studio user is in exactly one of the two modes.
        """
        try:
            from src.shared.feature_flags import get_product_mode, get_work_mode
            if get_product_mode() != "studio":
                return
            work_mode = get_work_mode(self.config_manager)
        except Exception:
            return
        base = self.windowTitle()
        tag = "Image Mode" if work_mode == "image" else "Video Mode"
        if tag in base:
            return  # already tagged
        # Strip a previously-applied mode tag before appending the new one.
        for prev in (" — Image Mode", " — Video Mode"):
            if base.endswith(prev):
                base = base[: -len(prev)]
                break
        self.setWindowTitle(f"{base} — {tag}")

    def _abort_startup(self, stage: str, exc: BaseException) -> None:
        """AUD-3507: report a fatal init failure and abort startup safely.

        Logs the full traceback, shows a clear error dialog (skipped in test
        mode) and re-raises a typed error so construction stops cleanly
        instead of leaving a half-initialized window behind.
        """
        logger.exception("Startup failed during %s initialization", stage)
        if not getattr(self, "_test_mode", False):
            try:
                QMessageBox.critical(
                    self,
                    I18N.tr("dialog.startup_failed.title", "Topos 启动失败"),
                    I18N.tr(
                        "dialog.startup_failed.body",
                        "{stage} 初始化失败，应用即将退出：\n{error}",
                    ).format(stage=stage, error=exc),
                )
            except Exception:  # pragma: no cover - dialog 失败不得掩盖原始错误
                logger.debug("Startup failure dialog could not be shown")
        raise RuntimeError(
            f"Topos startup failed during {stage} initialization") from exc

    def _run_audio_backend_diagnostics(self) -> None:
        """AM-01: 启动时探测音频后端并把降级状态反馈给用户。

        - 全部可用：静默（不打扰用户）。
        - 有降级：在状态栏添加永久告警 widget，并在日志记录每个不可用项。
        EQ/LUFS 的 UI 控件会通过 `audio_capabilities.get_audio_capabilities()`
        读取同一快照，自行决定是否显示降级态。

        AUD-3513: 探测（含 PyAV/SciPy 惰性导入与输出设备枚举）移入后台
        线程执行，结果经 `audio_capabilities_probed` 信号回 UI 线程应用，
        不再阻塞启动后的交互。
        """

        def _probe() -> None:
            try:
                from src.features.edit.core.audio_capabilities import (
                    get_audio_capabilities,
                    reset_audio_capabilities_cache,
                )
                # 重新探测（包含输出设备检查），覆盖 DSP 路径的惰性缓存
                reset_audio_capabilities_cache()
                caps = get_audio_capabilities(check_output_device=True)
            except Exception as e:  # pragma: no cover - 诊断失败不应阻断启动
                logger.debug(f"audio backend diagnostics failed: {e}")
                return
            try:
                self.audio_capabilities_probed.emit(caps)
            except RuntimeError:
                # 窗口已销毁：结果无处投递，直接丢弃（线程为 daemon，随进程退出）。
                logger.debug("audio diagnostics result dropped: window gone")

        threading.Thread(
            target=_probe, name="audio-backend-diagnostics", daemon=True,
        ).start()

    def _apply_audio_capabilities(self, caps: Any) -> None:
        """AUD-3513: apply a probed audio snapshot on the UI thread."""
        self._audio_capabilities = caps

        disabled = caps.disabled_components()
        if disabled:
            logger.warning(
                "音频后端降级，以下组件不可用: %s", ", ".join(disabled)
            )
            self._show_audio_backend_warning(caps, disabled)

    def _show_audio_backend_warning(self, caps, disabled: list) -> None:
        """在状态栏展示一个永久告警 widget，点击可查看详情。"""
        try:
            from PyQt6.QtWidgets import QLabel
            from src.shared.i18n import I18N
            label = QLabel(I18N.tr(
                "status.audio_backend_degraded",
                "音频后端降级",
            ))
            label.setStyleSheet(
                "color: #ffaa00; font-size: 11px; margin-right: 8px;"
                " padding: 1px 6px; border: 1px solid #664400; border-radius: 3px;"
            )
            label.setToolTip(I18N.tr(
                "status.audio_backend_degraded_tip",
                "以下音频组件不可用，EQ/LUFS/导出可能受影响：\n",
            ) + "\n".join(f"• {d}" for d in disabled))
            label.setCursor(Qt.CursorShape.PointingHandCursor)
            self.status_bar.addPermanentWidget(label)
            self._audio_backend_warning_label = label
        except Exception:  # pragma: no cover
            pass

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.KeyPress and not event.isAutoRepeat():
            key = event.key()
            # Space is the default transport key for every page; the Motion
            # Graphic editor's play key is user-customizable, so include it
            # in the same first-refusal chain (MG editor gets the key before
            # this window's own pages when its context is active).
            if key == Qt.Key.Key_Space or self._is_motion_graphic_play_key(event):
                if (
                    self._handle_motion_graphic_space_playback()
                    or self._handle_color_space_playback_shortcut()
                    or self._handle_composite_space_playback_shortcut()
                    or self._handle_edit_space_playback_shortcut()
                ):
                    event.accept()
                    return True
            # B1: frame-step / jump-to-end keyboard shortcuts for the Composite
            # page. The transport bar buttons advertise "(←)" / "(→)" tooltips
            # — these keys make those shortcuts real. Home/End jump to the
            # In/Out point of the playback range. Gated to Composite only and
            # suppressed while a text widget has focus (so arrow editing still
            # works inside spinboxes / value boxes).
            elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right,
                         Qt.Key.Key_Home, Qt.Key.Key_End):
                if self._handle_composite_frame_step_shortcut(key):
                    event.accept()
                    return True
        return super().eventFilter(obj, event)

    @staticmethod
    def _is_motion_graphic_play_key(event) -> bool:  # noqa: ANN001 - Qt event
        """Whether *event* matches the configured Motion Graphic play key."""
        try:
            from src.features.teaching_animation.ui.mg_shortcuts import motion_graphic_play_key
            return motion_graphic_play_key(event)
        except ImportError:  # pragma: no cover - module is part of the app
            return False

    def _handle_motion_graphic_space_playback(self) -> bool:
        """Give a visible Motion Graphic Tool window first refusal on Space.

        Motion Graphic editors are modeless top-level windows.  Their own
        QApplication event filter normally sees Space first, but the host may
        also have a global filter installed (Edit/Color/Composite shortcuts).
        Route through the editor's context-aware handler here as well so the
        host filter cannot consume the key before the Tool window does.
        """
        for candidate in QApplication.topLevelWidgets():
            if (
                candidate is self
                or candidate.objectName() != "MotionGraphicEditor"
                or not candidate.isVisible()
            ):
                continue
            handler = getattr(candidate, "handle_space_playback_shortcut", None)
            if not callable(handler):
                continue
            try:
                if bool(handler()):
                    return True
            except RuntimeError:
                # A closing modeless editor can disappear while Qt dispatches
                # the pending key event; let the remaining filters continue.
                continue
        return False

    def _handle_color_space_playback_shortcut(self) -> bool:
        if getattr(self, "current_page", None) != "Color":
            return False
        video_controller = getattr(self, "video_controller", None)
        if video_controller is None:
            return False
        is_loaded = getattr(video_controller, "is_video_loaded", None)
        try:
            if callable(is_loaded) and not is_loaded():
                return False
        except Exception:
            return False

        focus = QApplication.focusWidget()
        if isinstance(
            focus,
            (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QComboBox),
        ):
            return False

        self.on_video_play_toggled()
        return True

    @staticmethod
    def _focus_is_within(focus: Optional[QWidget], container: object) -> bool:
        """Return whether ``focus`` belongs to a docked playback surface."""
        if focus is None or container is None:
            return False
        if focus is container:
            return True
        is_ancestor = getattr(container, "isAncestorOf", None)
        if not callable(is_ancestor):
            return False
        try:
            return bool(is_ancestor(focus))
        except RuntimeError:
            # A dock can be deleted while Qt dispatches a pending key event.
            return False

    def _raise_edit_viewer_dock(self, dock_name: str) -> None:
        """Reveal the requested monitor inside the shared Edit Viewport."""
        viewport = getattr(self, "edit_viewport", None)
        if viewport is not None:
            if dock_name == "dock_source_viewer":
                viewport.show_source(focus=False)
            else:
                viewport.show_program(focus=False)
            dock_name = "dock_program_viewer"
        dock = getattr(self, dock_name, None)
        if dock is None:
            return
        try:
            dock.show()
            dock.raise_()
        except RuntimeError:
            logger.debug("Edit viewer dock is no longer available: %s", dock_name)

    def _handle_composite_space_playback_shortcut(self) -> bool:
        """Route Space to the Composite page playback."""
        if getattr(self, "current_page", None) != "Composite":
            return False
        # Image Color mode: no time-driven playback on the Composite page.
        page = getattr(self, "composite_page", None)
        if page is not None and getattr(page, "_is_image_color_mode", False):
            return False

        focus = QApplication.focusWidget()
        if isinstance(
            focus,
            (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QComboBox),
        ):
            return False

        if page is None:
            return False

        # 独立 comp clip（无 runtime_context）走自身的播放引擎；
        # timeline-bound 合成片段走时间线 controller 的 play/pause。
        if page.is_comp_clip_playback_active():
            page.comp_toggle_play()
            bar = getattr(getattr(self, "composite_viewer", None), "video_control_bar", None)
            if bar is not None:
                bar.set_playing(page._comp_playing)
            return True

        # timeline-bound：复用 transport bar 的 play_toggled 信号，
        # 这样 _on_play 闭包会更新按钮状态 + 驱动 controller。
        bar = getattr(getattr(self, "composite_viewer", None), "video_control_bar", None)
        if bar is not None:
            # 直接调 controller，避免依赖 bar 的 signal 连接状态
            controller = None
            timeline_panel = getattr(self, "timeline_panel", None)
            if timeline_panel is not None:
                try:
                    controller = timeline_panel.get_playback_controller()
                except (AttributeError, TypeError):
                    controller = None
            if controller is not None:
                if getattr(controller, "is_playing", False):
                    controller.pause()
                else:
                    controller.play()
                return True
        return False

    def _handle_composite_frame_step_shortcut(self, key) -> bool:
        """B1: route ←/→/Home/End to Composite playback when not in a text field.

        - Comp-clip path: page.comp_next/prev/first/last_frame.
        - Timeline-bound path: controller.seek at the current timeline frame
          ± 1, clamped to the clip's local range.
        Mirrors the bar buttons' already-wired slots so the keyboard and the
        buttons behave identically.
        """
        if getattr(self, "current_page", None) != "Composite":
            return False
        # Image Color mode: no frame-stepping on the Composite page.
        page = getattr(self, "composite_page", None)
        if page is not None and getattr(page, "_is_image_color_mode", False):
            return False

        focus = QApplication.focusWidget()
        if isinstance(
            focus,
            (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QComboBox),
        ):
            return False

        page = getattr(self, "composite_page", None)
        if page is None:
            return False

        # Comp-clip (independent composition) path.
        if page.is_comp_clip_playback_active():
            if key == Qt.Key.Key_Left:
                page.comp_prev_frame()
            elif key == Qt.Key.Key_Right:
                page.comp_next_frame()
            elif key == Qt.Key.Key_Home:
                page.comp_first_frame()
            elif key == Qt.Key.Key_End:
                page.comp_last_frame()
            else:
                return False
            bar = getattr(getattr(self, "composite_viewer", None),
                          "video_control_bar", None)
            if bar is not None:
                try:
                    bar.set_current_frame(page._composite_current_frame)
                except Exception:
                    pass
            return True

        # Timeline-bound comp path: seek the timeline controller.
        timeline_panel = getattr(self, "timeline_panel", None)
        controller = None
        if timeline_panel is not None:
            try:
                controller = timeline_panel.get_playback_controller()
            except (AttributeError, TypeError):
                controller = None
        if controller is None:
            return False
        cur = int(getattr(controller, "current_frame", 0) or 0)
        if key == Qt.Key.Key_Left:
            controller.seek(cur - 1)
        elif key == Qt.Key.Key_Right:
            controller.seek(cur + 1)
        elif key == Qt.Key.Key_Home:
            start = getattr(controller, "_playback_range_start", None)
            controller.seek(start if start is not None else 0)
        elif key == Qt.Key.Key_End:
            end = getattr(controller, "_playback_range_end", None)
            if end is None:
                end = getattr(controller, "duration", cur) or cur
            controller.seek(end)
        else:
            return False
        return True

    def _handle_edit_space_playback_shortcut(self) -> bool:
        """Route Space to the focused Edit playback surface.

        Timeline and Program Viewer control the timeline (Program) monitor;
        Source Viewer controls the selected media asset.  Raising the matching
        dock first keeps tabified monitors in sync with the active transport.
        """
        if getattr(self, "current_page", None) != "Edit":
            return False

        focus = QApplication.focusWidget()
        if isinstance(
            focus,
            (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QComboBox),
        ):
            return False

        source_viewer = getattr(self, "source_viewer_widget", None)
        if MainWindow._focus_is_within(focus, source_viewer):
            if getattr(source_viewer, "current_asset", None) is None:
                return False
            toggle_source = getattr(source_viewer, "toggle_playback", None)
            if not callable(toggle_source):
                return False
            MainWindow._raise_edit_viewer_dock(self, "dock_source_viewer")
            return bool(toggle_source())

        timeline_panel = getattr(self, "timeline_panel", None)
        program_viewer = getattr(self, "program_viewer_widget", None)
        if not (
            MainWindow._focus_is_within(focus, timeline_panel)
            or MainWindow._focus_is_within(focus, program_viewer)
        ):
            return False

        toggle_timeline = getattr(timeline_panel, "toggle_playback", None)
        if not callable(toggle_timeline):
            return False
        MainWindow._raise_edit_viewer_dock(self, "dock_program_viewer")
        reset_jkl = getattr(getattr(timeline_panel, "_playback_controller", None), "jkl_reset", None)
        if callable(reset_jkl):
            reset_jkl()
        toggle_timeline()
        return True

    def _create_pages_from_registry(self) -> bool:
        """
        通过 FeatureRegistry 创建所有注册的页面

        Returns:
            True if all pages created successfully, False if fallback needed
        """
        if not self.feature_registry:
            logger.warning("FeatureRegistry not available, falling back to direct import")
            return False

        class MainWindowContext:
            def __init__(self, main_window):
                self._main_window = main_window

            def get_main_window(self):
                return self._main_window

        ctx = MainWindowContext(self)

        created_pages = {}
        for page_desc in self.feature_registry.get_all_pages():
            try:
                page_widget = page_desc.create_page(ctx)
                created_pages[page_desc.page_id] = page_widget
                logger.debug(f"Created page '{page_desc.page_id}' from registry")
            except Exception as e:
                logger.error(f"Failed to create page '{page_desc.page_id}': {e}")
                return False

        for page_id, page_widget in created_pages.items():
            if page_id == "edit":
                self.edit_page = page_widget
            elif page_id == "deliver":
                self.deliver_page = page_widget
            elif page_id == "composite":
                self.composite_page = page_widget

        return True

    def create_panels(self):
        # --- Deferred imports: only load heavy UI modules when creating panels ---
        # Color UI panels
        from src.features.color.ui.canvas import Canvas
        from src.app.ui.library_panel import LibraryPanel
        from src.app.ui.files_panel import FilesPanel
        from src.app.ui.info_panel import InfoPanel
        from src.features.color.ui.layer_panel import LayerPanel
        from src.features.color.ui.node_editor_panel import ColorNodePanel
        from src.features.color.ui.color_wheel import GradingPanel
        from src.features.color.ui.denoise_panel import DenoisePanel
        from src.features.color.ui.deblur_panel import DeblurPanel
        from src.features.color.ui.mixer_panel import MixerPanel
        from src.app.ui.audio_mixer import AudioMixerPanel
        from src.features.color.ui.warper_panel import WarperPanel
        from src.features.color.ui.blur_sharpen_panel import BlurSharpenPanel
        from src.features.color.ui.film_grain_panel import FilmGrainPanel
        from src.features.color.ui.mask_panel import MaskPanel
        from src.features.color.ui.qualifier_panel import QualifierPanel
        from src.features.color.ui.raw_panel import RawPanel
        from src.features.color.ui.crop_panel import CropPanel
        from src.shared.ui.widgets.curves_widget import CurvesWidget
        from src.features.color.ui.curves_panel import CurvesPanel
        from src.features.color.ui.hsl_curves_panel import HSLCurvesPanel
        from src.features.color.ui.scope_panel import ScopePanel
        from src.shared.export.export_panel import ExportPanel
        from src.features.color.ui.presets_panel import PresetsPanel
        from src.features.color.ui.tracker_panel import TrackerPanel
        from src.features.color.tracking.tracking_service import TrackingService

        self.canvas = Canvas()
        self.canvas.engine = self.engine
        self.engine.canvas_ref = self.canvas  # P4-2 SAFETY: Enable paint/render cycle detection
        self.canvas.config_manager = self.config_manager
        self.library_panel = LibraryPanel()

        self.files_panel = FilesPanel(self)  # 传入 self 作为 parent

        # Register the explicit Qt-side host bridge used by the decode
        # benchmark plugin.  The service is intentionally created after the
        # file-explorer module has been imported, but before the user can
        # launch its window, so Flask routes never need to reach into the
        # Edit renderer themselves.
        try:
            from src.app.integrations.file_explorer.edit_benchmark_service import (
                EditBenchmarkHostService,
            )
            from file_explorer.main import bridge as file_explorer_bridge

            self._edit_benchmark_host_service = EditBenchmarkHostService(self)
            register_service = getattr(
                file_explorer_bridge, "register_edit_benchmark_service", None
            )
            if callable(register_service):
                register_service(self._edit_benchmark_host_service)
        except Exception as exc:
            # File explorer is an optional integration.  A missing bridge must
            # not prevent the main editor from starting.
            self._edit_benchmark_host_service = None
            logger.warning("Edit benchmark host bridge unavailable: %s", exc)

        self.info_panel = InfoPanel()
        self.layer_panel = LayerPanel(self)
        self.layer_panel.hide()
        # UI-only node workspace. Keep the legacy layer presenter available to
        # existing controllers until the ColorGraph adapter is implemented.
        # Node edit intents deliberately have no engine/project connections.
        self.color_node_panel = ColorNodePanel()
        from src.app.controllers.color_node_preview import ColorNodeSourcePreview
        self.color_node_source_preview = ColorNodeSourcePreview(self)
        # R06-B：离线 bake 服务（能力 fail-closed；UI 经状态信号消费；
        # 工程关闭经 on_project_closed → reset_all_bake_services 收口）。
        try:
            from src.features.color.core.color_bake_backend import (
                install_default_backends,
            )
            from src.features.color.core.color_bake_service import (
                ColorBakeService,
            )
            install_default_backends()
            self.color_bake_service = ColorBakeService(parent=self)
        except Exception:  # noqa: BLE001 — bake 为可选能力，不阻断启动
            self.color_bake_service = None
            logger.warning("color bake service unavailable", exc_info=True)
        # R08：逐节点已处理缩略图（独立 worker + standalone GL；GUI 零 GL）。
        from src.app.controllers.node_thumbnail_evaluator import (
            ColorNodeOutputPreview,
        )
        self.color_node_output_preview = ColorNodeOutputPreview()
        self.color_node_output_preview.node_thumbnail_ready.connect(
            self._on_node_output_thumbnail_ready)
        self._node_thumbnail_timer = QTimer(self)
        self._node_thumbnail_timer.setInterval(600)
        self._node_thumbnail_timer.timeout.connect(
            self._refresh_node_output_thumbnails)
        self._node_thumbnail_timer.timeout.connect(
            self._refresh_node_cache_hits)
        self._node_thumbnail_timer.start()
        self.grading_panel = GradingPanel()
        # Native Tangent Hub integration is optional and runs its socket worker
        # off the Qt thread. It reuses the grading controller below instead of
        # writing renderer state directly from the hardware callback.
        from src.features.color.integrations.tangent_bridge import TangentColorBridge
        self.tangent_color_bridge = TangentColorBridge(self, parent=self)
        self.tangent_color_bridge.start()
        self.denoise_panel = DenoisePanel()
        self.deblur_panel = DeblurPanel()
        self.curves_widget = CurvesWidget()
        self.hsl_curves_panel = HSLCurvesPanel(engine=self.engine)
        self.curves_panel = CurvesPanel(self.curves_widget, hsl_panel=self.hsl_curves_panel)
        self.warper_panel = WarperPanel()
        self.mixer_panel = MixerPanel()
        self.audio_mixer_panel = AudioMixerPanel()
        self.effects_panel = BlurSharpenPanel()
        self.film_grain_panel = FilmGrainPanel()
        # D2（N03-e）：旧 LutPanel/PluginPanel 实例已删除——LUT/Effects 的
        # 唯一 UI 是 ColorLutBrowser/ColorEffectsBrowser（R03/R10 命令路径）。
        self.scope_panel = ScopePanel()
        self.mask_panel = MaskPanel()
        # 单帧遮罩工具（Remove BG / Smart Brush / Brush / Eraser）产出静态
        # mask，会无差别应用到整段视频，因此仅在图片模式显示；work mode
        # 在启动时确定（切换需重启），此处一次性设置即可。
        try:
            from src.shared.feature_flags import get_work_mode as _get_wm
            _mask_image_tools_visible = _get_wm(self.config_manager) == "image"
        except Exception:
            _mask_image_tools_visible = True
        self.mask_panel.set_image_only_tools_visible(_mask_image_tools_visible)
        self.canvas.set_video_window_mode(not _mask_image_tools_visible)
        self.tracker_panel = TrackerPanel()
        self.tracking_service = TrackingService(parent=self)
        # Tracker 当前绑定的形状 id（None = 未绑定，面板空态）
        self._tracker_shape_id = None
        self._tracker_params = None
        self._active_tracker_task = None
        self._active_tracker_directions = ()
        self.qualifier_panel = QualifierPanel()
        # 旧 SoftClipPanel 已删除（2026-11-01 UI 整合计划任务 9）：柔化裁切
        # 四参数 UI 在 curves_panel 侧栏，旧 8 参数仅保留渲染/工程兼容路径
        # （layer.soft_clip_params 经脚本 API / 旧工程数据继续生效）。
        self.raw_panel = RawPanel()
        self.crop_panel = CropPanel()
        self.export_panel = ExportPanel()
        self.export_panel.set_default_export_settings(
            self.config_manager.get("default_export_format") or "JPEG",
            self.config_manager.get("default_export_quality") or 85
        )
        from src.features.color.ui.effects_browser_panel import ColorEffectsBrowser
        from src.features.color.ui.lut_browser_panel import ColorLutBrowser
        self.color_effects_browser = ColorEffectsBrowser(self)
        self.color_lut_browser = ColorLutBrowser(self)
        # R10：库偏好为用户配置域（不进工程 undo；缺失收藏可诊断）。
        self.color_effects_browser.set_browser_state(
            self.config_manager.get('color_effects_browser_state', {}))
        self.color_lut_browser.set_browser_state(
            self.config_manager.get('color_lut_browser_state', {}))

        # Scripting
        from src.features.scripting.module import create_script_engine
        from src.app.ui.script_panel import ScriptPanel
        self.script_engine = create_script_engine(self.engine)
        # P1-COLOR-24: 注入时间线只读上下文（脚本镜头遍历 / clip 调色快照）
        self.script_engine.set_timeline_provider(self._script_timeline_context)
        self.script_panel = ScriptPanel()

        # Presets Panel
        self.presets_panel = PresetsPanel()
        # G01：Gallery 能力宿主（无素材 Capture 禁用／只读 album／grade
        # 不可加载等如实回填；面板缺席时安静跳过）。
        from src.app.controllers.gallery_capability_host import (
            install_gallery_capability_host,
        )
        install_gallery_capability_host(self)

        # P9.1: Timeline Clip Strip (Color page)
        from src.features.color.ui.timeline_clip_strip import TimelineClipStrip
        self.color_timeline_clip_strip = TimelineClipStrip()

        # Edit Page panels (P1 phase) - Phase 8: Conditional creation
        self.edit_page = None
        self.deliver_page = None
        self.media_pool_panel = None
        self.program_viewer_widget = None
        self.source_viewer_widget = None
        self.timeline_panel = None
        self.edit_info_panel = None
        self.edit_properties_panel = None
        self.edit_effects_panel = None
        self.caption_workspace_panel = None  # SUB-P2-03: Caption Workspace dock
        self.adr_take_sheet_panel = None  # P1-10: ADR Take Sheet dock
        self.multicam_panel = None  # P1-01: Multicam angles dock

        # Try creating pages from registry first
        self._create_pages_from_registry()

        # Fallback: create Edit panels directly if not created via registry
        # Deferred edit UI imports (heavy, only needed if edit module is available)
        EditPage = None
        MediaPoolPanel = None
        ProgramViewerWidget = None
        SourceViewerWidget = None
        TimelinePanel = None
        EditInfoPanel = None
        EditPropertiesPanel = None
        EditEffectsPanel = None
        AdrTakeSheetPanel = None
        MulticamPanel = None
        if self._edit_available:
            try:
                from src.features.edit.commands_facade import get_edit_ui_classes
                _ui = get_edit_ui_classes()
                if _ui is None:
                    raise ImportError("Edit UI facade unavailable")
                EditPage = _ui["EditPage"]
                MediaPoolPanel = _ui["MediaPoolPanel"]
                TimelinePanel = _ui["TimelinePanel"]
                EditInfoPanel = _ui["EditInfoPanel"]
                EditPropertiesPanel = _ui["EditPropertiesPanel"]
                EditEffectsPanel = _ui["EditEffectsPanel"]
                ProgramViewerWidget = _ui["ProgramViewerWidget"]
                SourceViewerWidget = _ui["SourceViewerWidget"]
                # SUB-P2-03: Caption Workspace panel (optional; None if absent).
                CaptionWorkspacePanel = _ui.get("CaptionWorkspacePanel")
                AdrTakeSheetPanel = _ui.get("AdrTakeSheetPanel")
                MulticamPanel = _ui.get("MulticamPanel")
                # P1-10: ADR Take Sheet panel (optional).
                AdrTakeSheetPanel = _ui.get("AdrTakeSheetPanel")
            except ImportError as e:
                logger.warning(f"Edit UI imports failed: {e}")
                self._edit_available = False

        if self._edit_available and self.edit_page is None:
            if EditPage is not None:
                self.edit_page = EditPage()
                # B1: keep the checkable Scene Cut Overlay menu action in
                # sync when the overlay is toggled from the detection dialog.
                overlay_sig = getattr(
                    self.edit_page, "scene_cut_overlay_visibility_changed", None
                )
                if overlay_sig is not None:
                    overlay_sig.connect(self._on_scene_cut_overlay_visibility_changed)
        if self._edit_available:
            if MediaPoolPanel is not None and self.media_pool_panel is None:
                self.media_pool_panel = MediaPoolPanel()
            if ProgramViewerWidget is not None and self.program_viewer_widget is None:
                self.program_viewer_widget = ProgramViewerWidget()
            if SourceViewerWidget is not None and self.source_viewer_widget is None:
                self.source_viewer_widget = SourceViewerWidget()
            if TimelinePanel is not None and self.timeline_panel is None:
                self.timeline_panel = TimelinePanel()
            if EditInfoPanel is not None and self.edit_info_panel is None:
                self.edit_info_panel = EditInfoPanel()
            if EditPropertiesPanel is not None and self.edit_properties_panel is None:
                self.edit_properties_panel = EditPropertiesPanel()
            if EditEffectsPanel is not None and self.edit_effects_panel is None:
                self.edit_effects_panel = EditEffectsPanel()
            # SUB-P2-03: Caption Workspace panel.
            if (CaptionWorkspacePanel is not None
                    and self.caption_workspace_panel is None):
                self.caption_workspace_panel = CaptionWorkspacePanel()
            # P1-10: ADR Take Sheet panel.
            if (AdrTakeSheetPanel is not None
                    and self.adr_take_sheet_panel is None):
                self.adr_take_sheet_panel = AdrTakeSheetPanel()
            # P1-01: Multicam angles panel.
            if (MulticamPanel is not None
                    and self.multicam_panel is None):
                self.multicam_panel = MulticamPanel()

        # Deliver Page (P9.3) - Phase 9/R-001: Conditional creation with delayed import
        # Fallback: create directly if not created via registry
        if self._deliver_available and self.deliver_page is None:
            try:
                from src.features.deliver.facade import get_deliver_page_class
                _DeliverPage = get_deliver_page_class()
                if _DeliverPage is None:
                    raise ImportError
                self.deliver_page = _DeliverPage()
            except ImportError as e:
                logger.warning(f"Deliver page creation failed: {e}")
                self.deliver_page = None

        # 视频控制器（延迟初始化，需要等待 engine）
        self.video_controller = None

        # B06.5：真实节点面板接权威图（coordinator/bridge/host 装配）。
        # 失败不阻断启动（color_graph_host=None → 原工作流不变）。
        self._wire_color_graph_host()

    def _wire_color_keyframes_host(self) -> None:
        """装配关键帧面板、播放头和图命令。停靠面板创建后调用。"""
        self._color_keyframes_host = None
        self._color_keyframes_frame_hook = None
        panel = getattr(self, "color_keyframes_panel", None)
        if panel is None or getattr(self, "color_graph_host", None) is None:
            return
        try:
            from src.app.controllers.color_keyframe_recorder import (
                KeyframeAutoRecorder,
            )
            from src.app.controllers.color_keyframes_host import (
                ColorKeyframesHost,
            )
            self._color_keyframe_auto_recorder = KeyframeAutoRecorder()

            def _graph_provider():
                host = getattr(self, "color_graph_host", None)
                return host.bound_graph() if host is not None else None

            def _active_node_provider():
                host = getattr(self, "color_graph_host", None)
                return host.active_grade_node_identity() if host is not None else ""

            def _session_provider():
                manager = getattr(self, "_color_session_manager", None)
                return (manager.get_current_session()
                        if manager is not None else None)

            def _timeline_fps(timeline_id: str):
                # R1：传递时间线精确帧率（Rational）——29.97/59.94 家族
                # 的 DF 时间码显示与真实帧映射不按名义整数帧率折算
                project = getattr(self, "_current_edit_project", None)
                timeline = project.timelines.get(timeline_id) if project is not None else None
                return timeline.fps if timeline is not None else 0

            host = ColorKeyframesHost(
                panel,
                graph_provider=_graph_provider,
                active_node_provider=_active_node_provider,
                session_provider=_session_provider,
                playback=self,
                stack_id_provider=lambda: self.color_graph_host.bound_stack_id,
                binding_provider=self.color_graph_host.binding_description,
                fps_provider=_timeline_fps,
                # KF-03：真实写入口（批处理一次提交 = 一个历史条目；
                # lock/enable 复用既有节点级命令）
                submit_batch=self.color_graph_host.submit_keyframe_batch,
                submit_action=self.color_graph_host.submit_panel_action,
                # KF-04：自动录制注册表（手势 flush 与 base 参数同事务）
                auto_recorder=self._color_keyframe_auto_recorder,
            )
            # KF-04：录制器接调色手势路径（有效帧 = 当前播放头源帧）
            self.color_graph_host.attach_keyframe_recorder(
                self._color_keyframe_auto_recorder,
                frame_provider=lambda: getattr(
                    self, "_color_last_source_frame", None))
            self._color_keyframes_host = host
            self._color_keyframes_frame_hook = host.on_local_frame_changed
            self.grading_panel.use_central_keyframes()

            def _on_session_changed(_session) -> None:
                try:
                    host.refresh()
                except Exception:  # noqa: BLE001 — 投影失败不阻断会话切换
                    logger.debug("keyframes panel refresh failed",
                                 exc_info=True)

            manager = getattr(self, "_color_session_manager", None)
            if manager is not None:
                manager.add_session_observer(_on_session_changed)
            host.refresh()
            logger.debug("color keyframes host wired")
        except Exception:
            logger.warning("color keyframes host wiring failed",
                           exc_info=True)
            self._color_keyframes_host = None
            self._color_keyframes_frame_hook = None

    def _color_keyframes_host_refresh(self, *, force: bool = False) -> bool:
        """关键帧面板只读投影刷新（宿主未接线时空操作）。"""
        host = getattr(self, "_color_keyframes_host", None)
        if host is None:
            return False
        return host.refresh(force=force)

    def _wire_color_graph_host(self) -> None:
        """装配 ColorGraphAppHost（节点面板/调色手势接权威图）。

        - 工程/命令栈：media_pool 的 ProjectCommandStack（图命令与
          edit 命令同一 undo 栈）；
        - clip 水合钩子在 ``_load_color_stack_to_grading_panel`` 尾部
          （bind_stack + 投影水合）；
        - 提交后引擎同步经 bridge 的 set_project 回调（重投影 + 渲染）。
        """
        self.color_graph_host = None
        try:
            if getattr(self, "media_pool_panel", None) is None:
                logger.info(
                    "color graph host: edit unavailable; node panel stays "
                    "UI-only this session")
                return
            from src.app.controllers.color_graph_controller import (
                ColorGraphAppHost,
            )
            cmd_stack = self.media_pool_panel.get_command_stack()

            def _get_project():
                return getattr(self, "_current_edit_project", None)

            def _set_project(project):
                self._current_edit_project = project
                self.edit_project_dirty = True
                self._publish_color_graph_to_previews(project)
                # N11：外部工程替换统一重投影（幂等：自家命令流已推同
                # revision ack 时跳过）。
                try:
                    self.color_graph_host.on_external_project_change()
                except Exception:  # noqa: BLE001 — 重投影失败不阻断发布
                    pass
                # KF-02：图 revision 变化后刷新关键帧面板只读投影（幂等）
                try:
                    self._color_keyframes_host_refresh()
                except Exception:  # noqa: BLE001 — 面板投影失败不阻断发布
                    pass

            def _request_render():
                engine = getattr(self, "engine", None)
                if engine is not None:
                    engine.render_pipeline(render_reason='grading')

            def _color_context():
                manager = getattr(self, "_color_session_manager", None)
                session = (manager.get_current_session()
                           if manager is not None else None)
                if session is None:
                    return ("", "")
                return (getattr(session, "timeline_id", "") or "",
                        getattr(session, "clip_id", "") or "")

            self.color_graph_host = ColorGraphAppHost(
                get_project=_get_project,
                set_project=_set_project,
                project_stack=cmd_stack,
                engine_provider=lambda: getattr(self, "engine", None),
                request_render=_request_render,
                panels_provider=self._collect_color_panels,
                set_hydration_guard=self._set_color_stack_hydration_guard,
                color_context_provider=_color_context,
            )
            self.color_graph_host.panel_host.on_handle(self._on_color_graph_handle)
            self.color_graph_host.panel_host.on_window_combo(
                self._on_color_window_combo)
            self.color_graph_host.panel_host.on_read_only(
                lambda _node_id, _node_type: self._set_color_graph_panels_enabled(False))
            self.color_graph_host.attach_panel(self.color_node_panel)
            logger.debug("color graph host wired (node panel projection-"
                         "backed)")
        except Exception:
            logger.warning("color graph host wiring failed", exc_info=True)
            self.color_graph_host = None

    def _set_color_graph_panels_enabled(self, enabled: bool) -> None:
        """Non-corrector selection cannot edit the previous corrector's grade."""
        panels = list(self._collect_color_panels().values())
        panels.extend(getattr(self, name, None) for name in (
            "effects_panel", "qualifier_panel", "warper_panel", "mask_panel",
            "tracker_panel"))
        for panel in panels:
            if isinstance(panel, QWidget):
                panel.setEnabled(enabled)

    def _on_color_graph_handle(self, handle: Any) -> None:
        """Refresh parameter panels from the selected authoritative corrector."""
        from src.features.color.core.layer import Layer

        self._set_color_graph_panels_enabled(not bool(handle.params.get("locked", False)))
        graph_host = self.color_graph_host
        # N03：读合并投影（legacy 基底 + params 域覆盖 + LUT/曲线投影）
        # ——裸 legacy_payload 会让图命令写入的 qualifier 等域在面板不可见
        payload = graph_host.panel_layer_payload(handle.node_id) or {}
        layer = Layer.from_dict(payload)
        layer.grading_params.update(handle.grade_params())
        aligned = graph_host.align_engine_active_layer(handle.node_id)
        self._set_color_stack_hydration_guard(True)
        try:
            self.update_from_layer(layer)
            if aligned:
                engine = getattr(self, "engine", None)
                active_layer = (engine.get_active_layer()
                                if engine is not None else None)
                if active_layer is not None:
                    self.mask_panel.update_shapes_list(active_layer.mask_shapes)
                    self.canvas.update_mask_overlay(active_layer)
        finally:
            self._set_color_stack_hydration_guard(False)
        # 关键帧面板随正确体选择/参数反馈刷新（测试替身可能未装配宿主）
        refresh_keyframes = getattr(self, "_color_keyframes_host_refresh", None)
        if callable(refresh_keyframes):
            refresh_keyframes()

    def _on_color_window_combo(self, combo: str, node_id: str) -> None:
        """成功的组合命令选中 Window，曲线组合进入绘制状态。"""
        graph_host = getattr(self, "color_graph_host", None)
        engine = getattr(self, "engine", None)
        if (graph_host is None or engine is None
                or not graph_host.align_engine_active_layer(node_id)):
            return
        layer = engine.get_active_layer()
        if layer is None or not layer.mask_shapes:
            return
        shape = layer.mask_shapes[-1]
        if combo not in {"ppw", "pcw"}:
            self.canvas.set_drawing_mode(False)
        self.mask_panel.update_shapes_list(layer.mask_shapes)
        self.canvas.update_mask_overlay(layer)
        self.mask_panel.select_shape(shape.id)
        self.canvas.set_active_shape(shape.id)
        dock = getattr(self, "dock_masks", None)
        if dock is not None:
            dock.raise_()
        if combo in {"ppw", "pcw"}:
            self.canvas.set_mask_creation_mode(
                "polygon" if combo == "ppw" else "curve")

    def _publish_color_graph_to_previews(self, project: Any) -> None:
        """Invalidate every grade consumer when the authoritative graph commits.

        Graph gestures bypass the legacy stack-save callback. Both timeline
        preview workers still need the new immutable snapshot, otherwise seek
        can alternate old cached grades with the current Color render.
        """
        seen: set[int] = set()
        for name in ("timeline_preview_renderer", "_color_timeline_preview_renderer"):
            renderer = getattr(self, name, None)
            if renderer is None or id(renderer) in seen:
                continue
            seen.add(id(renderer))
            update = getattr(renderer, "update_color_stacks", None)
            if callable(update):
                update(project)
        invalidate = getattr(self, "_invalidate_video_render_params_if_playing", None)
        if callable(invalidate):
            invalidate()
        renderer = getattr(self, "_color_timeline_preview_renderer", None)
        if renderer is not None and getattr(self, "current_page", None) == "Color":
            provider = getattr(self, "_color_timeline_playback_controller", None)
            controller = provider() if callable(provider) else None
            if controller is not None:
                renderer.request_render(int(controller.current_frame))

    def _set_color_stack_hydration_guard(self, on: bool) -> None:
        """图宿主投影同步期间的面板回流抑制（与既有水合守卫同一标志）。"""
        self._color_stack_hydration_in_progress = bool(on)

    def _script_timeline_context(self, what: str, clip_id: str = None):
        """P1-COLOR-24: 脚本 API 的时间线只读上下文 provider。

        在主线程执行（经 BlockingBridge），返回纯数据：
        - what == "list_clips"：当前时间线 clip 清单（含 has_grade 标记）
        - what == "clip_grade"：指定 clip 的调色快照或 None
        """
        try:
            edit_page = getattr(self, "edit_page", None)
            sequence = getattr(edit_page, "_current_timeline", None) if edit_page else None
            project = getattr(self, "_current_edit_project", None)
            if sequence is None or project is None:
                return [] if what == "list_clips" else None

            stacks = getattr(project, "color_stacks", {}) or {}
            clips = list(getattr(sequence, "clips", []) or [])

            if what == "list_clips":
                from src.features.color.ui.timeline_clip_strip import (
                    _color_stack_has_grade,
                )
                result = []
                for clip in clips:
                    stack_id = getattr(clip, "color_stack_id", None)
                    stack = stacks.get(stack_id) if stack_id else None
                    result.append({
                        "clip_id": getattr(clip, "clip_id", ""),
                        "asset_id": getattr(clip, "asset_id", ""),
                        "track_id": getattr(clip, "track_id", ""),
                        "timeline_start": int(getattr(clip, "timeline_start", 0) or 0),
                        "timeline_duration": int(getattr(clip, "timeline_duration", 0) or 0),
                        "source_start": int(getattr(clip, "source_start", 0) or 0),
                        "source_duration": int(getattr(clip, "source_duration", 0) or 0),
                        "color_stack_id": stack_id or None,
                        "has_grade": bool(_color_stack_has_grade(stack)),
                    })
                result.sort(key=lambda c: (c["timeline_start"], c["clip_id"]))
                return result

            if what == "clip_grade":
                clip = next(
                    (c for c in clips
                     if getattr(c, "clip_id", None) == clip_id), None)
                stack_id = getattr(clip, "color_stack_id", None) if clip else None
                stack = stacks.get(stack_id) if stack_id else None
                if stack is None:
                    return None

                def _field(name, default=None):
                    if isinstance(stack, dict):
                        return stack.get(name, default)
                    return getattr(stack, name, default)

                grading = dict(_field("grading_params", {}) or {})
                return {
                    "grading_params": grading,
                    "lut_path": _field("lut_path", None),
                    "lut_intensity": float(_field("lut_intensity", 1.0) or 1.0),
                    "has_curves": bool(_field("curves_data", None)),
                    "has_qualifier": bool(
                        (_field("qualifier_params", None) or {}).get(
                            "enabled", False)),
                }

            return None
        except Exception:
            logger.exception("script timeline context failed")
            return [] if what == "list_clips" else None

    def setup_connections(self):
        self.engine.frame_ready.connect(self.on_frame_ready)
        self.engine.scope_snapshot_ready.connect(self.on_scope_snapshot_ready)
        # Native GPU scope descriptor (no CPU pixel readback).
        if hasattr(self.engine, 'gpu_scope_ready'):
            self.engine.gpu_scope_ready.connect(self.on_gpu_scope_ready)
        if hasattr(self.scope_panel, 'demand_changed'):
            self.scope_panel.demand_changed.connect(self._on_scope_demand_changed)
        self.curves_widget.histogram_geometry_changed.connect(self._on_scope_demand_changed)
        self.hsl_curves_panel.hsl_curves_widget.histogram_geometry_changed.connect(self._on_scope_demand_changed)
        self.curves_panel.curve_mode_changed.connect(self._on_scope_demand_changed)
        if hasattr(self, "_on_color_monitor_frame_ready"):
            self.engine.frame_ready.connect(self._on_color_monitor_frame_ready)
        self.engine.color_output_changed.connect(self._on_color_output_changed)
        if hasattr(self, "_on_color_output_monitor_ready") and hasattr(self.engine, "monitor_color_output_changed"):
            self.engine.monitor_color_output_changed.connect(self._on_color_output_monitor_ready)
        self.engine.mask_shapes_changed.connect(lambda: self.mask_panel.update_shapes_list(self.engine.get_active_layer().mask_shapes if self.engine.get_active_layer() else []))
        self.library_panel.image_selected.connect(self.load_image)
        self.library_panel.video_selected.connect(self._on_library_video_selected)  # Library 面板视频打开（备用）
        self.library_panel.grade_applied.connect(self.on_apply_grade_from_library)
        self.library_panel.quick_project_requested.connect(self._on_quick_project_requested)  # Quick Project 入口

        # Presets/Gallery Panel connections (P1-COLOR-04: Gallery 并入 Presets)
        self.presets_panel.grade_applied.connect(self.on_apply_grade_from_preset)
        self.presets_panel.capture_requested.connect(self.on_capture_still)
        self.presets_panel.apply_categories_requested.connect(
            self.on_apply_preset_categories_requested)
        self.presets_panel.reference_requested.connect(
            self.on_preset_reference_requested)
        # G13：split 协议闭环（宿主如实拒绝，直到 presenter 落地）；清除
        # 参考时结束 wipe 会话（菜单路径）。BR06：bundle 删除/改名 →
        # 在场 wipe 会话按来源身份失效并释放引擎参考。
        self.presets_panel.split_comparison_requested.connect(
            self.on_split_compare_requested)
        self.presets_panel.bundle_mutated.connect(
            self.on_gallery_bundle_mutated)
        self.canvas.viewport_menu.clear_reference_requested.connect(
            self.end_gallery_compare_session)
        self.canvas.color_viewport_chrome.gallery_compare_requested.connect(
            self.presets_panel.open_comparison)
        # P1-COLOR-21: Auto Color / Color Match
        self.presets_panel.match_requested.connect(
            self.on_match_to_still_requested)
        self.canvas.auto_color_requested.connect(self.on_auto_color_requested)
        self.canvas.viewport_menu.capture_still_requested.connect(self.on_capture_still)
        # P1-COLOR-19: Color workspace 预设（View > Workspace 菜单）
        self.setup_color_workspace_menu()

        self.files_panel.image_opened.connect(self.load_image)
        # Files 面板不支持视频打开，请使用 Library 面板
        self.files_panel.directory_changed.connect(self.library_panel.set_root_path)

        # 新增：双向目录同步 (Library → Files)
        self.library_panel.directory_changed.connect(self.files_panel.set_root_path)

        # 新增：星级评定双向同步 (Files → Library)
        self.files_panel.rating_changed.connect(self.library_panel.refresh_file_rating)

        # 新增：星级评定双向同步 (Library → Files)
        self.library_panel.rating_changed.connect(self.files_panel.refresh_file_rating)

        self.export_panel.export_requested.connect(self.perform_panel_export)
        self.export_panel.batch_export_requested.connect(self.perform_batch_export)
        self.export_panel.batch_cancel_requested.connect(self._cancel_batch_export)
        self.export_panel.batch_pause_requested.connect(self._pause_batch_export)
        self.export_panel.batch_resume_requested.connect(self._resume_batch_export)
        self.grading_panel.grading_changed.connect(self.on_grading_changed)
        self.grading_panel.interaction_started.connect(self.on_interaction_started)
        # P1-COLOR-03: 调色关键帧（模式 / 当前帧快照 / 删除）
        self.grading_panel.keyframe_mode_changed.connect(self.on_keyframe_mode_toggled)
        self.grading_panel.keyframe_snapshot_requested.connect(self.on_keyframe_snapshot_requested)
        self.grading_panel.keyframe_remove_requested.connect(self.on_keyframe_remove_requested)
        self.grading_panel.interaction_ended.connect(self.on_interaction_ended)
        self.grading_panel.reset_clicked.connect(self.on_adjustments_reset)
        self.grading_panel.tonal_saturation_changed.connect(
            self.on_tonal_saturation_changed)
        if hasattr(self, "tangent_color_bridge"):
            self.tangent_color_bridge.status_changed.connect(
                lambda message: self.status_bar.showMessage(message, 4000)
            )
        self.mixer_panel.mixer_changed.connect(self.on_mixer_changed)
        self.mixer_panel.interaction_started.connect(self.on_interaction_started)
        self.mixer_panel.interaction_ended.connect(self.on_interaction_ended)
        self.warper_panel.warper_changed.connect(self.on_warper_changed)
        self.warper_panel.interaction_started.connect(self.on_interaction_started)
        self.warper_panel.interaction_ended.connect(self.on_interaction_ended)
        self.effects_panel.effects_changed.connect(self.on_blur_sharpen_changed)
        self.effects_panel.interaction_started.connect(self.on_interaction_started)
        self.effects_panel.interaction_ended.connect(self.on_interaction_ended)
        self.film_grain_panel.grain_changed.connect(self.on_film_grain_changed)
        self.film_grain_panel.interaction_started.connect(self.on_interaction_started)
        self.film_grain_panel.interaction_ended.connect(self.on_interaction_ended)
        # P1-COLOR-13: 颗粒换种子
        self.film_grain_panel.reseed_requested.connect(
            self.on_film_grain_reseed_requested)
        self.curves_widget.changed.connect(self.on_curves_changed)
        self.curves_widget.interaction_started.connect(self.on_interaction_started)
        self.curves_widget.interaction_ended.connect(self.on_interaction_ended)
        self.hsl_curves_panel.hsl_curves_widget.changed.connect(self.on_hsl_curve_changed)
        self.hsl_curves_panel.hsl_curves_widget.interaction_started.connect(self.on_interaction_started)
        self.hsl_curves_panel.hsl_curves_widget.interaction_ended.connect(self.on_interaction_ended)
        # HSL Curves Picker signals
        self.hsl_curves_panel.hsl_picker_toggled.connect(self.on_hsl_picker_toggled)
        self.canvas.hsl_color_for_curves.connect(self.on_hsl_color_for_curves)
        # RGB Curves Picker signals
        self.curves_widget.picker_toggled.connect(self.on_curves_picker_toggled)
        self.canvas.curves_color_for_curves.connect(self.on_curves_color_for_curves)
        # D2：旧 LutPanel 已删除——LUT 应用/强度/重置的唯一入口是
        # ColorLutBrowser（activate_resource/set_lut_strength）与节点 LUT 菜单
        self.mask_panel.shape_added.connect(self.on_mask_shape_added)
        self.mask_panel.shape_removed.connect(self.on_mask_shape_removed)
        self.mask_panel.shape_selected.connect(self.on_panel_shape_selected)
        self.mask_panel.shape_selected.connect(self._on_tracker_shape_selected)
        self.mask_panel.send_to_tracker_requested.connect(
            self.on_send_to_tracker_requested)
        self.mask_panel.shape_updated.connect(self.on_mask_shape_updated)
        self.mask_panel.interaction_started.connect(self.on_interaction_started)
        self.mask_panel.interaction_ended.connect(self.on_interaction_ended)
        self.mask_panel.ai_birefnet_requested.connect(self.on_birefnet_requested)
        self.mask_panel.ai_sam_toggled.connect(self.on_ai_sam_toggled)
        self.mask_panel.all_masks_cleared.connect(self.on_all_masks_cleared)
        # P1-COLOR-09: 窗口批量复制
        self.mask_panel.duplicate_shape_requested.connect(
            self.on_duplicate_mask_shape)
        self.mask_panel.copy_shapes_to_layer_requested.connect(
            self.on_copy_masks_to_layer_requested)
        self.mask_panel.brush_toggled.connect(self.on_brush_toggled)
        self.mask_panel.brush_params_changed.connect(self.on_brush_params_changed)
        self.mask_panel.group_added.connect(self.on_mask_group_added)
        self.mask_panel.hierarchy_updated.connect(self.on_mask_hierarchy_updated)
        self.canvas.brush_stroke_added.connect(self.on_brush_stroke_added)
        self.canvas.brush_params_updated.connect(self.on_canvas_brush_params_updated)
        # N03：Qualifier 写入统一入口（图模式 typed handle 命令 / legacy 直写）
        self.qualifier_panel.params_changed.connect(self.on_qualifier_params_changed)
        self.qualifier_panel.interaction_started.connect(self.on_interaction_started)
        self.qualifier_panel.interaction_ended.connect(self.on_interaction_ended)
        # P1-COLOR-08: qualifier 面板的 matte 预览模式请求（'' = 关闭）
        self.qualifier_panel.matte_preview_requested.connect(self._on_matte_preview_requested)
        self.layer_panel.layer_selected.connect(self.on_layer_selected)
        self.layer_panel.layer_added.connect(self.on_layer_added)
        self.layer_panel.layer_removed.connect(self.on_layer_removed)
        self.layer_panel.layer_reset.connect(self.on_layer_reset)
        self.layer_panel.layer_moved.connect(self.on_layer_moved)
        self.layer_panel.layer_visibility_changed.connect(self.on_layer_visibility_changed)
        self.layer_panel.layer_renamed.connect(self.on_layer_renamed)
        self.layer_panel.layer_blend_mode_changed.connect(self.engine.set_layer_blend_mode)
        self.layer_panel.layer_opacity_changed.connect(self.engine.set_layer_opacity)

        # Layer Copy/Paste/Duplicate signals
        self.layer_panel.layer_copy.connect(self.on_layer_copy)
        self.layer_panel.layer_paste.connect(self.on_layer_paste)
        self.layer_panel.layer_duplicate.connect(self.on_layer_duplicate)

        # AI Baking signals (栅格化)
        self.layer_panel.layer_bake.connect(self.on_layer_bake)
        self.layer_panel.layer_unbake.connect(self.on_layer_unbake)

        self.canvas.mask_shape_changed.connect(self.on_mask_shape_updated)
        self.canvas.mask_shape_selected.connect(self.on_mask_shape_selected)
        self.canvas.mask_shape_selected.connect(self._on_tracker_shape_selected)
        self.tracker_panel.params_changed.connect(
            self._on_tracker_params_changed)
        self.tracker_panel.clear_tracking_requested.connect(self._clear_tracker_tracking)
        self.tracker_panel.insert_point_requested.connect(self._on_tracker_insert_point_requested)
        self.tracker_panel.set_point_requested.connect(self._on_tracker_set_point_requested)
        self.tracker_panel.delete_point_requested.connect(self._on_tracker_delete_point_requested)
        self.canvas.tracker_feature_selected.connect(self.tracker_panel.set_selected_feature)
        self.canvas.tracker_feature_inserted.connect(self._on_tracker_feature_inserted)
        self.canvas.tracker_overlay.insert_mode_changed.connect(self.tracker_panel.set_insert_mode_active)
        # 工具栏目标下拉切换：按形状 id 重绑跟踪目标
        self.tracker_panel.toolbar.target_changed.connect(
            self._on_tracker_target_changed)
        self.tracker_panel.track_forward_requested.connect(
            lambda: self._start_tracker_tracking("forward"))
        self.tracker_panel.track_backward_requested.connect(
            lambda: self._start_tracker_tracking("backward"))
        self.tracker_panel.track_forward_one_requested.connect(
            lambda: self._start_tracker_tracking("forward", single_frame=True))
        self.tracker_panel.track_backward_one_requested.connect(
            lambda: self._start_tracker_tracking("backward", single_frame=True))
        self.tracker_panel.track_auto_requested.connect(
            self._start_tracker_auto_tracking)
        self.tracker_panel.track_both_requested.connect(
            lambda: self._start_tracker_tracking("both"))
        self.tracker_panel.cancel_requested.connect(self._cancel_tracker_tracking)
        self.tracker_panel.retrack_failed_requested.connect(
            self._retrack_tracker_failed)
        self.tracker_panel.delete_manual_requested.connect(
            self._reset_tracker_current_frame)
        self.tracker_panel.reset_current_requested.connect(
            self._reset_tracker_current_frame)
        self.tracker_panel.corner_pin_export_requested.connect(
            self._export_tracker_corner_pin)
        self.tracker_panel.nav_requested.connect(self._on_tracker_nav_requested)
        self.tracker_panel.graph.frame_clicked.connect(
            self._on_tracker_graph_frame_clicked)
        self.tracker_panel.graph.scrub_started.connect(self._on_tracker_graph_scrub_started)
        self.tracker_panel.graph.scrub_finished.connect(self._on_tracker_graph_scrub_finished)
        self._init_stabilizer()
        self.tracking_service.task_started.connect(self._on_tracker_task_started)
        self.tracking_service.task_progress.connect(self._on_tracker_task_progress)
        self.tracking_service.task_finished.connect(self._on_tracker_task_finished)
        self.tracking_service.task_partial_result.connect(self._on_tracker_task_finished)
        self.tracking_service.task_failed.connect(self._on_tracker_task_failed)
        self.tracking_service.task_cancelled.connect(self._on_tracker_task_cancelled)
        # 画布跟踪区域框交互：拖动释放后写回窗口几何/搜索倍率（此前为死端信号）
        self.canvas.tracker_pattern_committed.connect(
            self._on_tracker_pattern_committed)
        self.canvas.tracker_search_committed.connect(
            self._on_tracker_search_committed)
        self.canvas.tracker_failed_marker_clicked.connect(
            self._on_tracker_failed_marker_clicked)
        self.canvas.interaction_started.connect(self.on_interaction_started)
        self.canvas.interaction_ended.connect(self.on_interaction_ended)
        self.canvas.color_picked.connect(self.on_color_picked)
        self.canvas.sam_point_clicked.connect(self.engine.request_sam_decode)
        self.engine.sam_encode_finished.connect(self.on_sam_encode_finished)
        self.engine.ai_process_finished.connect(self.on_ai_process_finished)
        self.engine.layer_baking_finished.connect(self.on_layer_baking_finished)

        def on_picker_toggled(enabled):
            self.canvas.set_picking_mode(enabled)
            if enabled:
                self.mask_panel.btn_brush.setChecked(False)
                self.mask_panel.btn_sam.setChecked(False)
                if hasattr(self.crop_panel, 'set_crop_active'): self.crop_panel.set_crop_active(False)
        self.qualifier_panel.picker_toggled.connect(on_picker_toggled)

        # 曲线面板侧栏（四参数柔化裁切 + Edit 强度）：正式参数信号。
        # 画布控制点仍走 curves_widget.changed -> on_curves_changed，
        # 两路互不重复提交；interaction 对复用同一撤销快照通道。
        self.curves_panel.soft_clip_changed.connect(self.engine.set_curves_soft_clip_params)
        self.curves_panel.soft_clip_changed.connect(self._invalidate_video_render_params_if_playing)
        self.curves_panel.edit_changed.connect(self.engine.set_curves_edit_params)
        self.curves_panel.edit_changed.connect(self._invalidate_video_render_params_if_playing)
        self.curves_panel.interaction_started.connect(self.on_interaction_started)
        self.curves_panel.interaction_ended.connect(self.on_interaction_ended)

        self.raw_panel.raw_params_changed.connect(self.on_raw_params_changed)
        self.raw_panel.interaction_started.connect(self.on_interaction_started)
        self.raw_panel.interaction_ended.connect(self.on_interaction_ended)

        self.crop_panel.crop_requested.connect(self.on_crop_applied)
        self.crop_panel.params_changed.connect(self.on_crop_params_changed)
        self.crop_panel.reset_requested.connect(self.on_crop_reset_requested)
        self.crop_panel.crop_toggled.connect(self.on_crop_toggled)
        self.crop_panel.constrain_changed.connect(self.canvas.set_crop_constrain)
        self.canvas.crop_rotation_changed.connect(lambda r: self.crop_panel.set_params(r))

        # Reset all crop parameters button in header
        self.crop_panel.reset_all_btn.clicked.connect(self.on_crop_reset_all_requested)
        # Note: Crop mode is only activated via crop button click (crop_toggled signal), not by showing the dock

        # D2：旧 PluginPanel 已删除——插件参数编辑的唯一入口是
        # ColorEffectsBrowser（R03 set_effect_parameter 命令路径）

        # Script Panel connections
        self.script_panel.run_clicked.connect(self.script_engine.execute)
        self.script_panel.stop_clicked.connect(self.script_engine.stop)
        self.script_engine.output_ready.connect(self.script_panel.append_output)
        self.script_engine.error_output_ready.connect(self.script_panel.append_error)
        self.script_engine.script_finished.connect(self._on_script_finished)
        self.script_engine.progress_updated.connect(self._on_script_progress)
        self.script_engine.status_message.connect(self._on_script_status)
        self.script_engine.action_registered.connect(self._on_action_registered)
        self.script_engine.script_error.connect(self._on_script_error)

        self.denoise_panel.denoise_requested.connect(self.on_denoise_requested)
        self.denoise_panel.reset_requested.connect(self.on_denoise_reset_requested)
        self.denoise_panel.video_denoise_changed.connect(self.on_video_denoise_changed)
        self.denoise_panel.video_history_reset_requested.connect(self.on_video_denoise_history_reset)
        self.denoise_panel.video_debug_overlay_toggled.connect(self.on_video_denoise_debug_overlay_toggled)
        self.denoise_panel.engine.processing_finished.connect(self.on_denoise_finished)
        self.denoise_panel.engine.error_occurred.connect(self.on_denoise_error)
        self.deblur_panel.deblur_requested.connect(self.on_deblur_requested)
        self.deblur_panel.reset_requested.connect(self.on_deblur_reset_requested)
        self.deblur_panel.engine.processing_finished.connect(self.on_deblur_finished)
        self.deblur_panel.engine.error_occurred.connect(self.on_deblur_error)

        self.engine.error_occurred.connect(self.on_engine_error)

        # Edit Properties Panel connections (conditional)
        if self._edit_available and self.edit_properties_panel is not None:
            self.edit_properties_panel.clip_param_changed.connect(self._on_edit_clip_param_changed)
            self.edit_properties_panel.clips_param_batch_changed.connect(self._on_edit_clips_param_batch_changed)
            self.edit_properties_panel.reset_transform_requested.connect(self._on_edit_reset_transform)
            self.edit_properties_panel.effect_param_changed.connect(self._on_edit_effect_param_changed)
            # X/Y chain-linked effect params (e.g. blur radius_x/radius_y) →
            # batched into one MacroCommand for single-step undo.
            self.edit_properties_panel.effect_params_changed.connect(self._on_edit_effect_params_changed)
            self.edit_properties_panel.effect_enabled_changed.connect(self._on_edit_effect_enabled_changed)
            self.edit_properties_panel.effect_remove_requested.connect(self._on_edit_effect_remove_requested)
            # P-clip-FX: 「打开面板」按钮 → 打开 clip-level audio 效果的独立参数对话框
            self.edit_properties_panel.effect_open_dialog_requested.connect(self._on_edit_effect_open_dialog_requested)
            # FX-H04-UI: 拖拽排序 → 执行 ReorderEffectCommand
            self.edit_properties_panel.effect_reorder_requested.connect(self._on_edit_effect_reorder_requested)
            # Phase 3 / round 2: per-effect keyframe toggle (◆ button in effect sections).
            self.edit_properties_panel.effect_keyframe_toggled.connect(self._on_edit_effect_keyframe_toggled)
            # E02: 普通片段参数（Transform/Opacity/Crop）◆ 按钮此前无人
            # 接收；与效果关键帧共用命令栈与面板刷新。
            self.edit_properties_panel.keyframe_toggled.connect(self._on_edit_clip_keyframe_toggled)
            # R10（第七轮审计）：已动画参数的数值编辑 → 当前帧关键帧更新
            #（此前只写静态 base，输入值被关键帧求值覆盖、渲染不可见）。
            self.edit_properties_panel.animated_params_edited.connect(self._on_edit_animated_params_edited)
            # Track-level params (volume / pan / mute / solo / lock / name) — edited
            # both from the track header and from the properties panel's audio section.
            self.edit_properties_panel.track_param_changed.connect(self._on_edit_track_param_changed)
            self.edit_properties_panel.transition_param_changed.connect(
                self._on_edit_transition_param_changed
            )
            # 拖动期间的纯视觉预览（同步轨道头显示，不进命令栈）
            self.edit_properties_panel.track_param_preview.connect(self._on_edit_track_param_preview)
            # P10.3: 字幕片段参数变化
            self.edit_properties_panel.subtitle_param_changed.connect(self.on_subtitle_param_changed)
            # SUB-P1-01: 字幕多字段合并提交（text debounce / RGBA 一次 patch）
            self.edit_properties_panel.subtitle_params_changed.connect(self.on_subtitle_params_changed)
            self.edit_properties_panel.subtitle_scope_changed.connect(
                self.on_subtitle_scope_changed
            )
            # Generators: Solid Color 颜色变化（更新 asset.metadata["color"]）
            self.edit_properties_panel.generator_color_changed.connect(self._on_edit_generator_color_changed)
            # Generators: 完整生成器参数变化（更新 asset.metadata["generator_params"]）
            self.edit_properties_panel.generator_params_changed.connect(self._on_edit_generator_params_changed)

        # Audio Mixer Panel connections (track_id 为 str)
        #   - *_changed = 松手时最终提交（进命令栈）
        #   - *_preview = 拖动期纯视觉/听感（不进命令栈）
        if hasattr(self, 'audio_mixer_panel') and self.audio_mixer_panel is not None:
            mp = self.audio_mixer_panel
            mp.track_volume_changed.connect(self._on_mixer_volume_committed)
            mp.track_volume_preview.connect(self._on_mixer_volume_preview)
            mp.track_balance_changed.connect(self._on_mixer_balance_committed)
            mp.track_balance_preview.connect(self._on_mixer_balance_preview)
            mp.track_mute_toggled.connect(self._on_mixer_mute_toggled)
            mp.track_solo_toggled.connect(self._on_mixer_solo_toggled)
            mp.track_record_arm_toggled.connect(self._on_mixer_record_arm_toggled)
            mp.master_volume_changed.connect(self._on_mixer_master_volume)
            mp.master_mute_toggled.connect(self._on_mixer_master_mute)
            mp.show_effects_for_track.connect(self._on_mixer_show_effects)
            # E17: purge_cache 信号已随 no-op 处理器一并删除
            mp.track_strip_eq_preview.connect(self._on_mixer_strip_eq_preview)
            mp.track_strip_eq_committed.connect(self._on_mixer_strip_eq_committed)
            mp.track_effects_preview.connect(self._on_mixer_effects_preview)
            mp.track_effects_committed.connect(self._on_mixer_effects_committed)
            mp.track_name_changed.connect(self._on_mixer_track_name_changed)
            mp.track_color_changed.connect(self._on_mixer_track_color_changed)
            mp.track_show_automation.connect(self._on_mixer_show_automation)
            mp.track_show_routing.connect(self._on_mixer_show_routing)
            mp.track_routing_committed.connect(self._on_mixer_routing_committed)
            mp.batch_solo_cleared.connect(self._on_mixer_batch_solo_cleared)
            mp.batch_mute_changed.connect(self._on_mixer_batch_mute_changed)

        # §3.1: 播放停止时提交自动化录制（Write/Touch/Latch）
        if self._edit_available and self.edit_page is not None:
            try:
                self.edit_page.signal_hub.playback_stopped.connect(
                    self._on_playback_stopped_commit_automation
                )
            except AttributeError:
                pass

        # Edit Effects Panel connections (conditional)
        if self._edit_available and self.edit_effects_panel is not None:
            self.edit_effects_panel.effect_add_requested.connect(self._on_edit_effect_add_requested)
            # P1-06: double-click a transition to apply it to the selection
            # (playhead fallback) through the same command path as drag-drop.
            self.edit_effects_panel.transition_apply_requested.connect(
                self._on_edit_transition_apply_requested
            )
            # C1: Titles tab —— 双击模板在播放头处创建带样式字幕
            if hasattr(self.edit_effects_panel, "title_template_selected"):
                self.edit_effects_panel.title_template_selected.connect(
                    self._on_title_template_selected
                )
            if hasattr(self.edit_effects_panel, "caption_effect_apply_requested"):
                self.edit_effects_panel.caption_effect_apply_requested.connect(
                    self.on_caption_effect_apply
                )

        # Timeline drag-drop effect add → auto-select & scroll the new
        # effect section in the properties panel. The timeline_view emits
        # effect_added(clip_id, effect_id) after executing AddEffectCommand
        # with a pre-generated id; the panel has already been rebuilt by
        # the command stack side effect, so the handler just selects+scrolls.
        if (self._edit_available and self.timeline_panel is not None
                and hasattr(self.timeline_panel, 'timeline_view')):
            self.timeline_panel.timeline_view.effect_added.connect(
                self._on_edit_effect_added
            )

        # Connect TimelinePanel selection to PropertiesPanel (conditional)
        if self._edit_available and self.timeline_panel is not None:
            if hasattr(self.timeline_panel, 'timeline_view'):
                self.timeline_panel.timeline_view.selection_changed.connect(self._on_timeline_selection_changed)

        # E04: TimelineView 的 18 个错误发射点（锁轨/事务拒绝/Slip 拒绝等）
        # 此前 0 个接收者，错误只进日志。统一接到状态栏，长文本走日志。
        if (self._edit_available and self.timeline_panel is not None
                and hasattr(self.timeline_panel, 'timeline_view')):
            self.timeline_panel.timeline_view.status_message_requested.connect(
                self._on_timeline_status_message
            )

        # P10.3: Connect subtitle clip signals to controller handlers
        if self._edit_available and self.timeline_panel is not None:
            tp = self.timeline_panel
            tp.add_subtitle_clip_requested.connect(self.on_add_subtitle_clip)
            tp.subtitle_clip_drag_finished.connect(self.on_subtitle_clip_drag_finished)
            tp.subtitle_clip_trim_left_finished.connect(self.on_subtitle_clip_trim_left_finished)
            tp.subtitle_clip_trim_right_finished.connect(self.on_subtitle_clip_trim_right_finished)
            tp.subtitle_clip_delete.connect(self.on_subtitle_clip_delete)
            tp.caption_graphic_action_requested.connect(self.on_caption_graphic_action)
            tp.caption_track_graphic_template_requested.connect(
                self.on_caption_track_graphic_template
            )
            tp.subtitle_clip_clicked.connect(self.on_subtitle_clip_clicked)
            tp.subtitle_clip_edit_text_requested.connect(self.on_subtitle_clip_edit_text)

        # SUB-P2-03: Caption Workspace → controller (three-way selection sync +
        # batch command execution). All three signals are guarded so a missing
        # or failed panel import never breaks the edit page.
        if self._edit_available and getattr(self, 'caption_workspace_panel', None) is not None:
            cwp = self.caption_workspace_panel
            try:
                cwp.cue_selected.connect(self.on_subtitle_clip_clicked)
                cwp.command_requested.connect(self._on_caption_command_requested)
                cwp.find_requested.connect(self._on_caption_find_requested)
                # U05（第六轮审计）：Find 命中 → 播放头/画面定位到 cue 开始
                find_hit = getattr(self, "on_subtitle_find_hit", None)
                if callable(find_hit):
                    cwp.find_hit_selected.connect(find_hit)
            except Exception:
                logger.exception("Failed to connect caption workspace signals")

        # Connect TimelinePanel timeline_changed to sync edit_page and project state (conditional)
        if self._edit_available and self.timeline_panel is not None:
            self.timeline_panel.timeline_changed.connect(self._on_edit_timeline_changed)

        # 空闲 MG 缓存调度：播放开始时取消后台 Chromium/FFmpeg 录制，
        # 会话空闲时自动补齐未缓存的 Motion Graphic 磁盘缓存。
        if self._edit_available and self.timeline_panel is not None:
            if hasattr(self.timeline_panel, 'playback_state_changed'):
                self.timeline_panel.playback_state_changed.connect(
                    self._on_playback_state_for_mg_idle
                )
                self.timeline_panel.playback_state_changed.connect(
                    self._on_scope_play_state_changed
                )
            self._ensure_mg_idle_cache_scheduler()

        # Background waveform extraction reports through EditPage so the
        # application status bar can explain why a clip is temporarily using a
        # placeholder without blocking playback or timeline interaction.
        if self._edit_available and self.edit_page is not None:
            self.edit_page.waveform_status_message.connect(self.status_bar.showMessage)

        # Sync timeline toolbar mixer button with dock_audio_mixer visibility
        if self._edit_available and self.timeline_panel is not None:
            dock = getattr(self, 'dock_audio_mixer', None)
            if dock is not None and hasattr(self.timeline_panel, 'sync_mixer_button'):
                dock.visibilityChanged.connect(self.timeline_panel.sync_mixer_button)

        # P2: Debounced clip-strip sync — refreshes Color clip strip on edit changes
        self._setup_clip_strip_sync()

        # Cleanup clip-strip sync signals on window destruction
        self.destroyed.connect(self._cleanup_clip_strip_sync)
        from src.app.controllers.color_node_tool_feedback import ColorNodeToolFeedback
        self.color_node_tool_feedback = ColorNodeToolFeedback(self)
        # R03：资源适配器互斥装配——默认正式命令适配器（typed target +
        # 图命令 + ack 水合）；显式开发 preview 模式（config
        # color_resource_ui_preview_mode）才装 UI 草稿 presenter。
        # 两组浏览器信号永不双写。
        from src.app.controllers.color_resource_catalog import EffectsResourceCatalog
        self.color_resource_catalog_provider = EffectsResourceCatalog()
        if self.config_manager.get('color_resource_ui_preview_mode', False):
            from src.app.controllers.color_resource_preview import ColorResourcePreview
            self.color_resource_preview = ColorResourcePreview(
                self, catalog_provider=self.color_resource_catalog_provider)
        else:
            from src.app.controllers.color_resource_controller import (
                ColorResourceController,
            )
            self.color_resource_preview = None
            self.color_resource_controller = ColorResourceController(
                self, catalog_provider=self.color_resource_catalog_provider)
            # R11：真实后端模式——浏览器移除"交互预览"横幅（命令已落图）。
            self.color_effects_browser.set_production_mode(True)
            self.color_lut_browser.set_production_mode(True)
            # N04：节点右键 LUT 菜单 → 与浏览器共用命令/导入入口。
            view = self.color_node_panel.view
            view.node_lut_apply_requested.connect(self._on_node_lut_menu_apply)
            view.node_lut_browse_requested.connect(self._on_node_lut_menu_browse)
        self.color_resource_catalog_provider.warm_background()
        # N17：可选依赖聚合一条（替代散落 WARNING；目录/预检以
        # optional_dependencies 为单一探测源）。
        try:
            from src.shared.diagnostics.optional_dependencies import (
                log_startup_summary,
            )
            log_startup_summary()
        except Exception:  # noqa: BLE001 — 诊断失败不阻断启动
            pass

    def _on_node_lut_menu_apply(self, node_id: str, lut_ref: str) -> None:
        """N04：菜单应用/移除 LUT——与浏览器同一命令路径。"""
        controller = getattr(self, "color_resource_controller", None)
        if controller is None:
            return
        if lut_ref:
            entry = {"id": lut_ref, "kind": "lut", "available": True,
                     "resource_uri": lut_ref}
            view = self.color_node_panel.view
            if view.state.graph.active_id != node_id and node_id in view.cards:
                view.state.graph.active_id = node_id
                controller.refresh_target()
            controller.activate_resource(entry)
        else:
            controller.edit("remove_lut", {})

    def _on_node_lut_menu_browse(self, node_id: str) -> None:
        """N04：菜单浏览——R02 导入（验证/注册/暂存）后应用。"""
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "", "", "LUT (*.cube *.3dl *.look)")
        if not path:
            return
        from src.shared.color.lut_resource_registry import (
            get_lut_resource_session,
        )
        results = get_lut_resource_session().import_files([path])
        controller = getattr(self, "color_resource_controller", None)
        if controller is None or not results or results[0].entry is None:
            self.status_bar.showMessage(
                I18N.tr("resource_browser.import_failed",
                        "LUT import failed"), 4000)
            return
        self._on_node_lut_menu_apply(node_id, results[0].entry.uri)

    def _on_node_output_thumbnail_ready(self, node_id: str, image) -> None:
        """GUI 线程：投递已评估节点输出（晚于卡片删除则忽略）。"""
        view = self.color_node_panel.view
        if node_id in view.cards and not image.isNull():
            from PyQt6.QtGui import QPixmap
            view.set_thumbnail(node_id, QPixmap.fromImage(image))

    def _refresh_node_cache_hits(self) -> None:
        """N06：按最近一次图执行的 hit 事件点亮卡片命中徽标。

        「状态仅实际命中后显示」——事件来自渲染线程的
        ``last_node_cache_events``（只读快照），无事件/非命中一律熄灭。
        """
        panel = getattr(self, "color_node_panel", None)
        renderer = getattr(self.engine, "renderer", None)
        events = getattr(renderer, "last_node_cache_events", None)
        if panel is None or not events:
            return
        if not panel.isVisible():
            return
        hits = {node_id for node_id, kind, *_rest in events
                if kind == "hit"}
        panel.view.set_cache_hits(hits)

    def _refresh_node_output_thumbnails(self) -> None:
        """R08 demand：面板可见 + 源缩略图就绪时请求可见节点输出。"""
        preview = getattr(self, "color_node_output_preview", None)
        if preview is None:
            return
        panel = self.color_node_panel
        if not panel.isVisible():
            return
        source_pixmap = getattr(panel.view, "_source_thumbnail", None)
        if source_pixmap is None or source_pixmap.isNull():
            return
        graph_host = getattr(self, "color_graph_host", None)
        coordinator = getattr(graph_host, "_coordinator", None)
        ack = coordinator.last_ack if coordinator else None
        graph_payload = ack.graph if ack is not None else None
        if not isinstance(graph_payload, dict):
            return

        def graph_provider():
            coordinator2 = getattr(getattr(self, "color_graph_host", None),
                                   "_coordinator", None)
            ack2 = coordinator2.last_ack if coordinator2 else None
            return ack2.graph if isinstance(getattr(ack2, "graph", None), dict) else None

        def source_provider():
            import numpy as np
            from PyQt6.QtGui import QImage
            pixmap = getattr(panel.view, "_source_thumbnail", None)
            if pixmap is None or pixmap.isNull():
                return None
            image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGB888)
            ptr = image.constBits()
            ptr.setsize(image.sizeInBytes())
            arr = np.frombuffer(ptr, dtype=np.uint8).reshape(
                image.height(), image.bytesPerLine())
            arr = arr[:, :image.width() * 3].reshape(
                image.height(), image.width(), 3)
            return arr.astype(np.float32) / 255.0

        from src.features.color.ui.node_editor_state import NodeKind
        # 只请求权威图中存在的节点（草稿本地 id/已删节点不进 demand；
        # 水合后新 id 会在下一拍补上）。
        authoritative_ids = {node.get("node_id")
                             for node in graph_payload.get("nodes", ())
                             if isinstance(node, dict)}
        node_ids = [node_id for node_id, card in panel.view.cards.items()
                    if node_id in authoritative_ids
                    and getattr(card.node, "kind", None) in (
                        NodeKind.CORRECTOR, NodeKind.COMPOUND)]
        preview.set_identity(
            coordinator.session_id if coordinator else "",
            getattr(graph_host, "_bound_stack_id", "") or "",
            ack.graph_revision if ack else 0)
        preview.request(graph_provider, source_provider, node_ids)

    def on_engine_error(self, message):
        # Hide progress bar on any error to ensure UI state recovery
        self.progress_bar.hide()
        self.status_bar.showMessage(f"Error: {message}", 5000)
        QMessageBox.critical(self, "Error", message)

    def _on_manual_save_failed(self, filepath: str, error: str) -> None:
        """AUD-16715：后台手动保存失败必须让用户可见。

        排队成功即清 dirty 的契约下，后台落盘失败若无人消费
        ``manual_save_failed``，用户会误信已保存。非模态：状态栏 + 日志。
        """
        logger.warning(
            "Manual background save failed (AUD-16715): %s (%s)",
            filepath, error)
        self.status_bar.showMessage(
            I18N.tr(
                "edit.io.manual_save_failed_status",
                f"Save failed: {os.path.basename(filepath)} — see logs"),
            8000)

    # ================================================================
    # Shutdown
    # ================================================================

    def closeEvent(self, event) -> None:
        # R10：保存资源浏览器用户偏好（收藏/视图/缩放；无像素缓存）。
        try:
            self.config_manager.set(
                'color_effects_browser_state',
                self.color_effects_browser.browser_state(), auto_save=False)
            self.config_manager.set(
                'color_lut_browser_state',
                self.color_lut_browser.browser_state(), auto_save=False)
        except Exception:  # noqa: BLE001 — 偏好失败不阻断关闭
            pass
        """窗口关闭时按序停止后台线程，避免 QThread 析构崩溃。

        macOS 上 QThread::~QThread() 在线程仍运行时会 abort 整个进程
        （实测：播放后退出必现 SIGSEGV/SIGABRT，"QThread: Destroyed
        while thread is still running"）。各线程拥有者都有停止 API，
        但此前没有任何关闭路径调用它们。

        [AUD-3500] 可取消的保存确认必须在任何后台服务停机之前执行：
        此前 Tangent/tracking/files_panel/回放管线在保存确认框弹出前就
        被 shutdown，且 `_pipeline_shutdown_done=True` 使其不可恢复——
        用户在确认框选"取消"后窗口不关闭，但跟踪/播放/预览渲染已全部
        失效，只能重启应用。首轮确认通过后，FileOpsMixin.closeEvent
        内的二次确认直接放行（实例级 bypass，finally 恢复类级解析），
        避免"丢弃"选择（脏标记仍在）被问两次。
        [AUD-3501] UpdateChecker 为 QThread，关闭路径 quit()+wait()。
        [AUD-3502] script_engine 的后台脚本线程先于 engine worker 停止
        （脚本线程持有 engine 引用，清理顺序不得倒置）。
        [AUD-3503] denoise/deblur 的 DenoiseWorker 线程经引擎 cancel()
        协作停止（中断请求 + 有界等待 + terminate 兜底）。
        """
        confirm_bypassed = False
        try:
            # ---- [AUD-3500] 可取消的保存确认先行 ----
            confirm = getattr(self, "_confirm_save_before_close", None)
            if callable(confirm):
                confirmed = True
                try:
                    confirmed = bool(confirm())
                except Exception:
                    # 确认流程自身异常不得吞掉关闭：mixin 内还会再问一次
                    logger.exception(
                        "MainWindow: save-before-close confirmation failed")
                if not confirmed:
                    event.ignore()
                    return
                # 首轮已确认（保存/丢弃/无未保存变更）——mixin 的二次
                # 确认直接放行；"丢弃"后脏标记仍在，否则会被问两次。
                self._confirm_save_before_close = lambda: True
                confirm_bypassed = True

            try:
                tangent_bridge = getattr(self, "tangent_color_bridge", None)
                if tangent_bridge is not None:
                    tangent_bridge.shutdown()
            except Exception:
                logger.exception("MainWindow: Tangent Hub shutdown failed")
            try:
                stabilization_service = getattr(self, "stabilization_service", None)
                if stabilization_service is not None:
                    stabilization_service.shutdown()
                stabilization_plan_service = getattr(self, "stabilization_plan_service", None)
                if stabilization_plan_service is not None:
                    stabilization_plan_service.shutdown()
                tracking_service = getattr(self, "tracking_service", None)
                if tracking_service is not None:
                    tracking_service.shutdown()
            except Exception:
                logger.exception("MainWindow: tracking service shutdown failed")
            try:
                resource_controller = getattr(self, "color_resource_controller", None)
                if resource_controller is not None:
                    resource_controller.shutdown()
            except Exception:
                logger.exception("MainWindow: Color resource thumbnail shutdown failed")
            try:
                benchmark_service = getattr(self, "_edit_benchmark_host_service", None)
                if benchmark_service is not None:
                    benchmark_service.shutdown()
            except Exception:
                logger.exception("MainWindow: edit benchmark service shutdown failed")
            # ---- [AUD-3501] UpdateChecker（QThread）----
            # run() 内 urlopen(timeout=10)，wait 上限略高于它保证必收；
            # 常见路径（请求早已完成/测试短路）立即返回，不拖慢关闭。
            try:
                checker = getattr(self, "_update_checker", None)
                if checker is not None:
                    checker.quit()
                    if not checker.wait(10500):
                        logger.warning(
                            "MainWindow: update checker still running after wait")
            except Exception:
                logger.exception("MainWindow: update checker shutdown failed")
            # ---- [AUD-3502] 脚本引擎后台线程 ----
            # 必须先于 FileOpsMixin 停 engine worker：脚本线程持有 engine
            # 引用，倒置会在退出阶段触碰已被停掉的引擎/Qt 对象。
            try:
                script_engine = getattr(self, "script_engine", None)
                if script_engine is not None and hasattr(script_engine, "stop"):
                    script_engine.stop()
                    script_thread = getattr(script_engine, "_thread", None)
                    if script_thread is not None:
                        script_thread.wait(3000)
            except Exception:
                logger.exception("MainWindow: script engine shutdown failed")
            # ---- [AUD-3503] 去噪/去模糊 worker（DenoiseWorker QThread）----
            for panel_attr in ("denoise_panel", "deblur_panel"):
                try:
                    panel = getattr(self, panel_attr, None)
                    engine = getattr(panel, "engine", None) if panel is not None else None
                    cancel = getattr(engine, "cancel", None)
                    if callable(cancel):
                        cancel()
                except Exception:
                    logger.exception(
                        "MainWindow: %s worker shutdown failed", panel_attr)
            # [CRASH-2] file_explorer 停靠面板装在 QDockWidget 内，其 closeEvent
            # 在窗口关闭时不会触发——Flask 服务必须在此显式停止
            try:
                files_panel = getattr(self, "files_panel", None)
                if files_panel is not None:
                    files_panel.shutdown()
            except Exception:
                logger.exception("MainWindow: files panel shutdown failed")
            try:
                self._shutdown_playback_pipeline()
            except Exception:
                logger.exception("MainWindow: playback pipeline shutdown failed")
            # QMainWindow 在本类 MRO 中排在所有 controller mixin 之前，
            # super().closeEvent() 会直接命中 QWidget 的实现并跳过
            # FileOpsMixin.closeEvent —— 丢失保存提示、配置持久化和
            # edit_project_io.cleanup()（删除恢复用 .autosave 文件）。
            # 必须显式调用 mixin 的完整退出清理。
            FileOpsMixin.closeEvent(self, event)
        finally:
            if confirm_bypassed:
                # closeEvent 可能因后续路径再次进入（如显式 ignore）——
                # 移除实例级 bypass，恢复类级方法解析。
                self.__dict__.pop("_confirm_save_before_close", None)

    def _shutdown_playback_pipeline(self) -> bool:
        """幂等地停止走带/音频/预览渲染/合成渲染线程。

        R-24（2026-10-14 审计）：只有所有必须终止的 owner 真正停止才
        把管线标记为 done。合成渲染线程超时未停时保持 done=False
        （后续 closeEvent 可重试），并明确记录未完成的 owner——不再先
        行永久标记 done 而丢失重试机会。返回最终 done 状态。
        """
        if getattr(self, "_pipeline_shutdown_done", False):
            return True

        # 0. I01：播放仲裁器关门——此后一切播放请求被拒（不虚报成功），
        #    入口门同步失效，后续各 owner 停机照常进行。
        try:
            coordinator = getattr(self, "_viewport_playback_coordinator", None)
            if coordinator is not None:
                coordinator.mark_shutdown()
        except Exception:
            logger.debug("shutdown: mark playback arbiter shutdown failed",
                         exc_info=True)

        # 0.5 CV-B10：Color 视口能力宿主关门——关闭后晚到的刷新请求
        #     不再触碰销毁中的 chrome/菜单控件。
        try:
            color_host = getattr(self, "_color_viewport_capability_host", None)
            if color_host is not None:
                color_host.mark_shutdown()
        except Exception:
            logger.debug("shutdown: mark color capability host failed",
                         exc_info=True)

        # 1. 走带停止（先停时钟，避免关闭过程中又派发新渲染请求）
        try:
            panel = getattr(self, "timeline_panel", None)
            controller = panel.get_playback_controller() if panel else None
            if controller is not None and getattr(controller, "is_playing", False):
                controller.pause()
        except Exception:
            logger.debug("shutdown: pause playback controller failed", exc_info=True)

        # 2. 音频（EditPage.shutdown_audio_playback 专为关闭路径设计，幂等）
        try:
            edit_page = getattr(self, "edit_page", None)
            if edit_page is not None and hasattr(edit_page, "shutdown_audio_playback"):
                edit_page.shutdown_audio_playback()
        except Exception:
            logger.debug("shutdown: audio playback failed", exc_info=True)

        # 3. Color 页视频控制器
        try:
            video_controller = getattr(self, "video_controller", None)
            if video_controller is not None and hasattr(video_controller, "stop_playback"):
                video_controller.stop_playback()
        except Exception:
            logger.debug("shutdown: video controller failed", exc_info=True)

        # 3.5 CV-B10：Color 私有时间线预览渲染器（自有 worker）。离开
        #     Color 页走 leave hook，但停在 Color 页直接关窗时不会经过
        #     它——在停掉视频控制器后同样断开/停用，避免 worker 在关闭
        #     过程中把晚到帧投递给正在销毁的 canvas。
        try:
            deactivate_preview = getattr(
                self, "_deactivate_color_timeline_preview", None)
            if callable(deactivate_preview):
                deactivate_preview()
        except Exception:
            logger.debug("shutdown: deactivate color timeline preview failed",
                         exc_info=True)

        # 3.6 CV-B06：恢复 Color 监看静音（页面作用域状态不带入关闭后
        # 的全局音频配置）。
        try:
            restore_mute = getattr(self, "_restore_color_monitor_mute", None)
            if callable(restore_mute):
                restore_mute()
        except Exception:
            logger.debug("shutdown: restore color monitor mute failed",
                         exc_info=True)

        # 3.7 R03：双视图预算执行器关门——停更新/摘 watcher/解除监看域
        #     预算（用户预览质量偏好从未被预算改动，无需恢复动作）。
        try:
            budget_executor = getattr(self, "_viewport_budget_executor", None)
            if budget_executor is not None and hasattr(
                    budget_executor, "mark_shutdown"):
                budget_executor.mark_shutdown()
        except Exception:
            logger.debug("shutdown: budget executor close failed",
                         exc_info=True)

        # 3.8 G06：抓帧会话登记处关门——全部在场请求令牌失效，迟到
        #     阶段据此取消（不再把旧上下文的帧写盘）。
        try:
            capture_coordinator = getattr(
                self, "_gallery_capture_coordinator_inst", None)
            if capture_coordinator is not None and hasattr(
                    capture_coordinator, "mark_shutdown"):
                capture_coordinator.mark_shutdown()
        except Exception:
            logger.debug("shutdown: gallery capture coordinator close failed",
                         exc_info=True)

        # 4. 时间线预览渲染器（内部依次 join worker 与 GPU 渲染线程）
        try:
            renderer = getattr(self, "timeline_preview_renderer", None)
            if renderer is not None and hasattr(renderer, "shutdown"):
                renderer.shutdown()
                self.timeline_preview_renderer = None
        except Exception:
            logger.debug("shutdown: timeline preview renderer failed", exc_info=True)

        # 5. 合成页渲染线程
        try:
            page = getattr(self, "composite_page", None)
            page_cleanup = getattr(page, "cleanup", None)
            if callable(page_cleanup):
                # Let CompositePage stop its own comp timer and Keyframes
                # fallback timer before joining the GL render thread.
                page_cleanup()
            else:
                comp_renderer = getattr(page, "_renderer", None)
                if (comp_renderer is not None
                        and hasattr(comp_renderer, "stop_thread")):
                    comp_renderer.stop_thread()
        except Exception:
            logger.debug("shutdown: composite renderer failed", exc_info=True)

        # R-24：汇总 owner 停机结果。合成渲染线程超时未停（页面仍持有
        # 引用）时 done 保持 False，可重试；不假报停止。
        page = getattr(self, "composite_page", None)
        comp_complete = True
        if page is not None:
            checker = getattr(page, "pipeline_shutdown_complete", None)
            if callable(checker):
                try:
                    comp_complete = bool(checker())
                except Exception:
                    logger.debug("shutdown: completeness probe failed",
                                 exc_info=True)
        if comp_complete:
            self._pipeline_shutdown_done = True
            logger.info("MainWindow: playback pipeline shut down")
        else:
            self._pipeline_shutdown_done = False
            logger.error(
                "MainWindow: playback pipeline shutdown INCOMPLETE — "
                "composite render thread did not stop; done flag left "
                "clear so a later close can retry")
        return self._pipeline_shutdown_done

    # ================================================================
    # Edit Properties Panel Handlers
    # ================================================================
