"""AI 编辑助手主 Dock 面板（Phase 9 UI-0）。

注册为 Edit/Studio 可用的右侧 ``QDockWidget``（TAEP §11.1）。首版（UI-0）是
**空壳与状态契约**：

- 使用 fake ``AiAssistantService`` 显示 idle/loading/error/empty project 状态，
  **不**接任何模型。
- 只发细粒度 PyQt signal（``brief_patch_requested`` / ``variant_selected`` /
  ``apply_requested``），不导入 renderer/exporter/controller 私有字段。
- 状态回填用 ``_updating`` 锁避免信号递归（TAEP §11.3）。
- 无项目时提示导入素材；未配置 profile 时只允许编辑 Brief。

后续 UI-1..UI-6 在此骨架上叠加 Brief/variant/diff/job 子组件。
"""

from __future__ import annotations

import mimetypes
import os
import json
import logging
import re
import time
import html as _html
import math
from typing import Any

from PyQt6.QtCore import QObject, QPoint, QRunnable, QTimer, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QImage,
    QKeyEvent,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFrame,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QListWidget,
    QListWidgetItem,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.features.edit.ai_editing.assistant_events import (
    AssistantState,
    AssistantStateSnapshot,
)
from src.features.edit.ai_editing.image_attachment import (
    FileAttachment,
    ImageAttachment,
    attachment_from_qimage,
)
from src.shared.i18n.i18n import tr


_MAX_WIDGET_HEIGHT = 16_777_215
_MAX_FILE_ATTACHMENT_BYTES = 512 * 1024 * 1024
_SUPPORTED_FILE_FILTER = (
    "支持的文件 (*.png *.jpg *.jpeg *.webp *.bmp *.gif "
    "*.mp4 *.mov *.avi *.mkv *.webm *.m4v *.flv *.wmv "
    "*.mp3 *.wav *.m4a *.aac *.flac *.ogg "
    "*.pdf *.txt *.md *.json *.csv *.doc *.docx *.xls *.xlsx *.ppt *.pptx)"
)
_MEDIA_POOL_ASSETS_MIME = "application/x-topos-media-pool-assets"

logger = logging.getLogger(__name__)


class _ImageAttachmentWorkerSignals(QObject):
    """后台图片附件任务的队列信号载体（AUD-19511）。"""

    #: (attachment | None, source, ok)
    ready = pyqtSignal(object, str, bool)


class _ImageAttachmentWorker(QRunnable):
    """在 QThreadPool 线程完成 QImage 解码（可选）+ 降采样 + JPEG 编码。

    AUD-19511: QImage 的文件加载 / 缩放 / JPEG 编码不依赖 GUI 线程（QImage
    是 Qt 的线程安全图像类；只有 QPixmap 和部件操作必须留在 UI 线程），
    结果经 ``ready`` 信号队列化回 UI 线程后再进附件区/预览。
    """

    def __init__(
        self,
        *,
        image: QImage | None,
        path: str,
        source: str,
        signals: _ImageAttachmentWorkerSignals,
    ) -> None:
        super().__init__()
        self._image = image
        self._path = path
        self._source = source
        self._signals = signals

    def run(self) -> None:
        attachment: ImageAttachment | None = None
        ok = False
        try:
            image = self._image
            if image is None and self._path:
                image = QImage(self._path)  # 后台线程整图解码
            if image is not None and not image.isNull():
                attachment = attachment_from_qimage(image, source=self._source)
                ok = True
        except (ValueError, RuntimeError) as exc:
            logger.warning(
                "Image attachment encode failed (source=%s, path=%s): %s",
                self._source, self._path, exc,
            )
        try:
            self._signals.ready.emit(attachment, self._source, ok)
        except RuntimeError:
            # 面板已销毁（用户秒关）：信号载体已删除，结果作废。
            logger.debug("Image attachment result arrived after panel destroy")


def _trf(key: str, default: str, **kwargs: object) -> str:
    """Translate a panel label and safely interpolate its dynamic values."""
    text = tr(key, default, **kwargs)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text
    return text


def _slash_text(command: dict[str, str], field: str) -> str:
    """Return localized slash-command chrome while keeping command ids stable."""
    name = command["name"]
    return tr(f"ai_assistant.slash_{name}_{field}", command[field])


# 斜杠命令只收录已经存在且有真实处理入口的能力，避免候选面板出现
# 点击后没有任何效果的“装饰性命令”。
_SLASH_COMMANDS: tuple[dict[str, str], ...] = (
    {
        "name": "context",
        "label": "检查上下文",
        "description": "查看本次模型会看到的素材、分析和 token",
        "signal": "context_inspector_requested",
    },
    {
        "name": "analyze",
        "label": "分析当前素材",
        "description": "为当前选中的素材创建分析计划",
        "signal": "analyze_context_requested",
    },
    {
        "name": "compress",
        "label": "压缩上下文",
        "description": "压缩较早的对话上下文，释放 token 空间",
        "signal": "context_compress_requested",
    },
    {
        "name": "scope",
        "label": "选择素材范围",
        "description": "调整本次对话使用的素材范围",
        "signal": "composer_scope_requested",
    },
    {
        "name": "new",
        "label": "新对话",
        "description": "创建新的会话，不删除已有历史",
        "signal": "new_session_requested",
    },
    {
        "name": "history",
        "label": "历史会话",
        "description": "切换到当前工程的历史会话",
        "signal": "show_history_requested",
    },
    {
        "name": "models",
        "label": "配置模型",
        "description": "打开 AI Editing 模型配置",
        "signal": "configure_models_requested",
    },
    {
        "name": "capture",
        "label": "抓取当前画面",
        "description": "把当前视口画面作为图片附件",
        "signal": "frame_grab_requested",
    },
    {
        "name": "workflow",
        "label": "显示工作流",
        "description": "查看当前 AI 工作流和运行活动",
        "signal": "show_workflow_requested",
    },
    {
        "name": "clear",
        "label": "清空历史会话",
        "description": "清除本机缓存和当前工程的对话历史",
        "signal": "clear_history_requested",
    },
)


def _decode_data_url_bytes(data_url: str) -> bytes:
    """把 ``data:image/...;base64,XXX`` 解码回原始字节（供 QPixmap.loadFromData）。"""
    try:
        comma = data_url.find(",")
        if comma < 0:
            return b""
        import base64 as _b64
        return _b64.b64decode(data_url[comma + 1:])
    except Exception:  # noqa: BLE001
        return b""


def _coerce_str_list(value: Any) -> list[str]:
    """AUD-19502：外部 dict 的列表字段归一为 ``list[str]``。

    ``None``/标量/含非字符串元素等畸形值一律降级为空列表，调用方不再
    对着未校验的 ``card_data.get(..., [])`` 做切片/迭代而抛 TypeError。
    """
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def _coerce_dict_list(value: Any) -> list[dict]:
    """AUD-19502：外部 dict 的"元素为 dict"列表字段归一（滤掉非 dict 项）。"""
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, dict)]


class _AssistantPromptEdit(QPlainTextEdit):
    """Chat-style prompt editor: Enter sends, Shift+Enter inserts a newline.

    多模态：粘贴图片（剪贴板里的截图）时不插入文本，而是发 ``image_pasted``
    信号，由面板把它加进待发送附件预览。
    """

    submitted = pyqtSignal()
    cancelled = pyqtSignal()
    slash_navigation = pyqtSignal(str)  # up / down / accept / dismiss
    mention_navigation = pyqtSignal(str)  # up / down / accept / dismiss
    image_pasted = pyqtSignal(object)  # QImage
    file_dropped = pyqtSignal(object)  # {paths: tuple[str, ...], asset_ids: tuple[str, ...]}

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

    def set_mention_mode(self, enabled: bool) -> None:
        """让键盘事件优先服务于 ``@`` 媒体候选面板。"""
        self._mention_mode = bool(enabled)

    def set_slash_mode(self, enabled: bool) -> None:
        """让键盘事件优先服务于斜杠候选面板。"""
        self._slash_mode = bool(enabled)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        slash_mode = bool(getattr(self, "_slash_mode", False))
        if slash_mode:
            if event.key() == Qt.Key.Key_Up:
                event.accept()
                self.slash_navigation.emit("up")
                return
            if event.key() == Qt.Key.Key_Down:
                event.accept()
                self.slash_navigation.emit("down")
                return
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    event.accept()
                    self.slash_navigation.emit("accept")
                    return
            if event.key() == Qt.Key.Key_Escape:
                event.accept()
                self.slash_navigation.emit("dismiss")
                return
        mention_mode = bool(getattr(self, "_mention_mode", False))
        if mention_mode:
            if event.key() == Qt.Key.Key_Up:
                event.accept()
                self.mention_navigation.emit("up")
                return
            if event.key() == Qt.Key.Key_Down:
                event.accept()
                self.mention_navigation.emit("down")
                return
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    event.accept()
                    self.mention_navigation.emit("accept")
                    return
            if event.key() == Qt.Key.Key_Escape:
                event.accept()
                self.mention_navigation.emit("dismiss")
                return
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            self.cancelled.emit()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                event.accept()
                self.submitted.emit()
                return
        super().keyPressEvent(event)

    @staticmethod
    def _media_pool_asset_ids(source) -> tuple[str, ...]:
        """Read the media-pool internal drag payload without importing the pool UI."""
        if source is None or not source.hasFormat(_MEDIA_POOL_ASSETS_MIME):
            return ()
        try:
            raw = bytes(source.data(_MEDIA_POOL_ASSETS_MIME)).decode("utf-8")
            values = json.loads(raw)
            if isinstance(values, list):
                return tuple(str(value) for value in values if value)
        except (TypeError, ValueError, UnicodeDecodeError):
            return ()
        return ()

    @staticmethod
    def _local_drop_paths(source) -> tuple[str, ...]:
        if source is None or not source.hasUrls():
            return ()
        return tuple(
            url.toLocalFile()
            for url in source.urls()
            if url.isLocalFile() and url.toLocalFile()
        )

    def _accepts_file_drop(self, source) -> bool:
        return bool(
            self._local_drop_paths(source)
            or self._media_pool_asset_ids(source)
        )

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._accepts_file_drop(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if self._accepts_file_drop(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        source = event.mimeData()
        paths = self._local_drop_paths(source)
        asset_ids = self._media_pool_asset_ids(source)
        if paths or asset_ids:
            event.acceptProposedAction()
            self.file_dropped.emit({"paths": paths, "asset_ids": asset_ids})
            return
        super().dropEvent(event)

    def insertFromMimeData(self, source) -> None:  # type: ignore[override]
        """拦截粘贴：图片 → 发信号；其余（文本）走默认行为。"""
        if source is not None and source.hasImage():
            image = source.imageData()
            if isinstance(image, QImage) and not image.isNull():
                self.image_pasted.emit(image)
                return
        super().insertFromMimeData(source)


class _AssistantMarkdownView(QTextBrowser):
    """A non-scrolling Markdown document that reports its real content height.

    ``QLabel(MarkdownText)`` is convenient for short messages, but its table
    layout is calculated before a narrow dock has settled on its final width.
    Long plan tables can consequently reserve empty rows or leave the scroll
    range stale.  A QTextDocument gives us one layout pass at the actual
    viewport width; the outer conversation area remains the only scroll
    container.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setOpenExternalLinks(True)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        self.setStyleSheet(
            "QTextBrowser { color: #d8d8dc; background: transparent; "
            "border: none; padding: 0px; font-size: 11px; }"
        )
        self.document().setDocumentMargin(0)
        self.document().setDefaultStyleSheet(
            "table { border-collapse: collapse; } "
            "td, th { padding: 2px 5px; } "
            "p { margin-top: 0px; margin-bottom: 3px; }"
        )

    def set_markdown_text(self, text: str) -> None:
        """Set Markdown and resize after the document has parsed its tables."""
        self.setMarkdown(str(text or ""))
        self._sync_document_height()

    def resizeEvent(self, event: object) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)  # type: ignore[arg-type]
        self._sync_document_height()

    def _sync_document_height(self) -> None:
        viewport_width = max(1, self.viewport().width())
        document = self.document()
        document.setTextWidth(viewport_width)
        document.adjustSize()
        document_height = document.documentLayout().documentSize().height()
        self.setFixedHeight(max(1, int(math.ceil(document_height))))

class AiEditAssistantPanel(QDockWidget):
    """AI 编辑助手主面板。

    只发 signal；不持有 AI session 业务状态（那在 controller/service 层）。
    状态显示由 ``refresh_state`` 驱动（controller 订阅 service 后回填）。
    """

    # 细粒度 signal（TAEP §11.3）
    brief_patch_requested = pyqtSignal(object)  # BriefPatch
    variant_selected = pyqtSignal(str)  # variant_id
    apply_requested = pyqtSignal(str)  # variant_id
    request_submitted = pyqtSignal(object)  # AssistantRequest
    cancel_requested = pyqtSignal()
    retry_requested = pyqtSignal()                    # 重试最近一次请求
    draft_changed = pyqtSignal(str)                   # 输入草稿变化（token 预估）
    # 多模态：用户点"抓取当前帧" → 宿主抓取当前视口画面后回调 add_image_attachment。
    frame_grab_requested = pyqtSignal()
    # M5：上下文检查器（§7.1 可检查/可排除）。用户排除某块 → host 据 this 重算 bundle。
    context_inspector_requested = pyqtSignal()          # 用户点"检查上下文"按钮
    analyze_context_requested = pyqtSignal()            # 分析/重新分析当前素材
    context_block_excluded = pyqtSignal(str)            # block_id（用户排除）
    context_block_restored = pyqtSignal(str)            # block_id（用户恢复）

    # ---- plan §10.2 P0.3 / P0.6：Composer footer + Run Drawer 信号 ---- #
    composer_scope_requested = pyqtSignal()             # 点 scope 区域 → 范围确认
    composer_permission_changed = pyqtSignal(str)       # 权限等级切换
    composer_profile_requested = pyqtSignal()           # 打开 profile/模型选择
    composer_profile_changed = pyqtSignal(str)          # 选择具体 Profile
    composer_mode_changed = pyqtSignal(str)             # chat / motion_graphic / media
    composer_reasoning_changed = pyqtSignal(str)        # low / medium / high
    context_compress_requested = pyqtSignal()           # 触发上下文压缩
    new_session_requested = pyqtSignal()                # 新建会话（不删历史）
    show_history_requested = pyqtSignal()               # 打开历史会话
    clear_history_requested = pyqtSignal()              # 清空本机/工程历史会话
    history_session_requested = pyqtSignal(str)         # 从二级菜单切换会话
    # E13：帮助卡（AiHelpAnswerCard）转发——此前卡片信号无人接收，
    # “引用定位/打开页面”按钮点击无任何反应。
    help_navigate_requested = pyqtSignal(str)           # entry_id（引用定位）
    help_open_panel_requested = pyqtSignal(str)         # page_ref（打开页面）
    run_cancel_requested = pyqtSignal(str)              # Run Drawer 取消某 item
    run_retry_requested = pyqtSignal(str)               # Run Drawer 重试某 item
    run_diagnostics_requested = pyqtSignal(str)         # Run Drawer 查看诊断
    # P1.1：Skill 推荐卡触发执行（转发 AiSkillGallery.start_requested）
    skill_start_requested = pyqtSignal(str, dict)       # skill_id, form_values
    # P1.5：结果卡动作触发（转发 AiResultCard.action_triggered；run_id, action_kind）
    result_action_triggered = pyqtSignal(str, str)
    # P5：右键触发显示工作流弹窗
    show_workflow_requested = pyqtSignal()
    # 分析是 Agent 的只读工具，但整批素材仍需用户明确确认。
    analysis_confirm_requested = pyqtSignal(str)  # request_id
    analysis_cancel_requested = pyqtSignal(str)   # request_id
    # Teaching Animation 宿主写入也必须经过显式计划确认。
    motion_graphic_confirm_requested = pyqtSignal(str)  # request_id
    motion_graphic_cancel_requested = pyqtSignal(str)   # request_id
    # AUD-19500："改用内置模板"按钮的专用信号。此前它与"确认生成"发同一个
    # motion_graphic_confirm_requested，宿主无法区分意图，MG-05 的
    # plan["use_template"] 分支永远无法触发。宿主应连接本信号并在确认前把
    # plan["use_template"] 置 True；未连接的旧宿主由发射端回退到普通确认。
    motion_graphic_template_requested = pyqtSignal(str)  # request_id
    # MG-04: user selects a creative proposal (request_id, proposal_id).
    motion_graphic_proposal_selected = pyqtSignal(str, str)
    # 打开 Preferences 的 AI Editing 页面
    configure_models_requested = pyqtSignal()
    conversation_title_change_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        title = tr("panel.ai_edit_assistant", "AI assistant")
        super().__init__(title, parent)
        self.setObjectName("dock_ai_edit_assistant")
        self._updating = False  # 状态锁，避免信号递归
        self._state = AssistantStateSnapshot()
        # 多模态：待发送的图片附件（粘贴/抓帧），发送后清空。
        self._pending_attachments: list[ImageAttachment | FileAttachment] = []
        # AUD-19511: 后台图片附件任务（解码+JPEG 编码）的信号载体与计数。
        self._attachment_signals = _ImageAttachmentWorkerSignals()
        self._attachment_signals.ready.connect(self._on_image_attachment_ready)
        self._attachment_jobs_pending = 0
        # 多选文件批次的回填计数（batch 完成 0 成功时提示一次）。
        self._image_batch: dict[str, int] | None = None
        # 媒体池中的 Motion Graphic 是虚拟资产，不一定对应磁盘文件。
        # 单独保存稳定 asset_id，避免把 topos:// URI 当成普通文件附件丢弃。
        self._pending_target_asset_ids: list[str] = []
        self._pending_target_labels: dict[str, str] = {}
        self._context_bundle: object | None = None  # M5：host 推入的 ContextBundle（检查器用）
        self._active_profile_display_name = ""
        self._conversation_title = ""
        self._conversation_title_is_custom = False
        self._analysis_proposals: dict[str, QWidget] = {}
        self._motion_graphic_proposals: dict[str, QWidget] = {}
        self._history_sessions: list[object] = []
        self._active_history_session_id = ""
        self._message_widgets: list[QWidget] = []
        self._last_rendered_message_signature: tuple[tuple[object, ...], ...] = ()
        self._messages_scroll_pending = False
        self._messages_scroll_retry = 0
        self._messages_scroll_timer = QTimer(self)
        self._messages_scroll_timer.setSingleShot(True)
        self._messages_scroll_timer.timeout.connect(self._attempt_messages_scroll)
        self._slash_popup: QListWidget | None = None
        self._slash_matches: list[dict[str, str]] = []
        self._media_pool_provider: Any = None
        self._media_popup: QListWidget | None = None
        self._media_matches: list[dict[str, str]] = []
        self._media_token_span: tuple[int, int] | None = None
        self._thinking_phase = 0
        self._thinking_timer = QTimer(self)
        self._thinking_timer.setInterval(420)
        self._thinking_timer.timeout.connect(self._animate_thinking)

        # 不调用 setAllowedAreas：与其它面板（info/library/properties 等）保持一致，
        # 使用 Qt 默认的 AllDockWidgetAreas。此前曾限制为 Left|Right，导致面板浮动后
        # 无法停靠——Top（viewport）、Bottom（timeline）等区域全部被拒绝，用户拖到
        # 标签栏或边缘都会被 Qt 弹回浮动，表现为"完全无法停靠"。
        # 与属性/效果等面板共享右侧标签组；不要对 Dock 本体设置最小宽度，
        # 否则切换到本面板会抬高整组 splitter 的最小宽度。

        self._build_ui()
        self.refresh_state(self._state)

        # P5：右键菜单触发"显示工作流"弹窗（用户要求改 dock → 弹窗）
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

    # ------------------------------------------------------------------ #
    # P5：右键菜单
    # ------------------------------------------------------------------ #

    def _on_context_menu(self, pos: QPoint) -> None:
        """右键菜单：收纳低频操作，保持对话区干净。"""
        self._show_context_menu(self.mapToGlobal(pos))

    def _show_context_menu(self, global_pos: QPoint) -> None:
        """在指定屏幕坐标显示面板右键菜单。"""
        menu = self._build_overflow_menu(include_frame_grab=True)
        menu.exec(global_pos)

    def _build_overflow_menu(self, *, include_frame_grab: bool = False) -> Any:
        """构建低频操作菜单；右键菜单可额外提供当前画面抓帧。"""
        from PyQt6.QtGui import QAction
        from PyQt6.QtWidgets import QMenu

        menu = QMenu(self)
        context_action = QAction(tr("ai_assistant.inspect_context", "检查上下文"), menu)
        context_action.triggered.connect(self._on_inspect_context_clicked)
        menu.addAction(context_action)
        ctx = getattr(self._state, "context", None)
        needs_analysis = bool(
            ctx is not None
            and getattr(ctx, "asset_count", 0)
            and (
                getattr(ctx, "analyzed_count", 0) <= 0
                or not getattr(ctx, "analysis_fresh", True)
            )
        )
        analyze_action = QAction(
            tr(
                "ai_assistant.reanalyze_context"
                if needs_analysis else "ai_assistant.analyze_context",
                "重新分析当前素材" if needs_analysis else "分析当前素材",
            ),
            menu,
        )
        analyze_action.triggered.connect(self.analyze_context_requested.emit)
        analyze_action.setEnabled(bool(getattr(ctx, "asset_count", 0)))
        menu.addAction(analyze_action)
        if include_frame_grab:
            frame_grab_action = QAction(
                tr(
                    "ai_assistant.grab_frame",
                    "抓取当前画面作为图片附件",
                ),
                menu,
            )
            frame_grab_action.triggered.connect(self.frame_grab_requested.emit)
            menu.addAction(frame_grab_action)
        scope_action = QAction(tr("ai_composer.scope_menu", "选择素材范围"), menu)
        scope_action.triggered.connect(self.composer_scope_requested.emit)
        menu.addAction(scope_action)
        compress_action = QAction(tr("ai_composer.compress", "压缩上下文"), menu)
        compress_action.triggered.connect(self.context_compress_requested.emit)
        menu.addAction(compress_action)
        menu.addSeparator()
        new_session_action = QAction(tr("ai_composer.new_session", "新对话"), menu)
        footer_state = getattr(self._composer_footer, "state", None)
        new_session_action.setEnabled(
            not bool(getattr(footer_state, "has_active_run", False))
        )
        new_session_action.triggered.connect(self.new_session_requested.emit)
        menu.addAction(new_session_action)
        history_menu = menu.addMenu(tr("ai_composer.history", "历史会话"))
        history_menu.aboutToShow.connect(self.show_history_requested.emit)
        self._populate_history_menu(history_menu)
        clear_history_action = QAction(
            tr("ai_assistant.clear_history", "清空历史会话"), menu,
        )
        clear_history_action.triggered.connect(self.clear_history_requested.emit)
        menu.addAction(clear_history_action)
        menu.addSeparator()
        workflow_action = QAction(tr("ai_assistant.show_workflow", "显示工作流"), menu)
        workflow_action.triggered.connect(self.show_workflow_requested.emit)
        menu.addAction(workflow_action)
        menu.addSeparator()
        configure_action = QAction(
            tr("ai_assistant.configure_models", "配置模型"), menu,
        )
        configure_action.triggered.connect(self.configure_models_requested.emit)
        menu.addAction(configure_action)
        # Dock 管理动作与 AI 操作放在同一个菜单，避免 QDockWidget 默认菜单
        # 再弹出第二个窗口。
        menu.addSeparator()
        float_action = QAction(
            tr("dock.dock", "停靠") if self.isFloating() else tr("dock.float", "浮动"),
            menu,
        )
        float_action.triggered.connect(
            lambda: self.setFloating(not self.isFloating())
        )
        menu.addAction(float_action)
        close_action = QAction(tr("dock.close", "关闭面板"), menu)
        close_action.triggered.connect(lambda: self.setVisible(False))
        menu.addAction(close_action)
        return menu

    def _show_overflow_menu(self) -> None:
        """标题栏的紧凑菜单按钮。"""
        button = self._overflow_btn
        self._build_overflow_menu().exec(
            button.mapToGlobal(button.rect().bottomLeft())
        )

    # ------------------------------------------------------------------ #
    # Codex-style slash command palette
    # ------------------------------------------------------------------ #

    def _build_slash_popup(self, parent: QWidget) -> None:
        """创建输入框上方的斜杠命令候选面板。"""
        popup = QListWidget(parent)
        popup.setObjectName("assistant_slash_commands")
        popup.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        popup.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        popup.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        popup.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        popup.setUniformItemSizes(True)
        popup.setStyleSheet(
            "QListWidget#assistant_slash_commands {"
            " background: #2d2d31; color: #dedee2;"
            " border: 1px solid #55565d; border-radius: 7px;"
            " padding: 4px; font-size: 11px; }"
            "QListWidget#assistant_slash_commands::item {"
            " padding: 6px 8px; border-radius: 4px; }"
            "QListWidget#assistant_slash_commands::item:selected {"
            " background: #4a3a2c; color: #ffffff; }"
            "QListWidget#assistant_slash_commands::item:hover {"
            " background: #3b3b40; }"
        )
        popup.itemClicked.connect(self._on_slash_item_clicked)
        popup.hide()
        self._slash_popup = popup

    def _update_slash_commands(self) -> None:
        """根据输入内容过滤并显示斜杠命令。"""
        popup = self._slash_popup
        if popup is None or self._updating:
            return
        text = self._request_edit.toPlainText()
        stripped = text.lstrip()
        if (
            self._request_edit.isReadOnly()
            or not stripped.startswith("/")
            or "\n" in stripped
        ):
            self._hide_slash_commands()
            return

        query_parts = stripped[1:].strip().split(None, 1)
        query = query_parts[0].lower() if query_parts else ""
        matches = [
            command for command in _SLASH_COMMANDS
            if not query
            or query in command["name"].lower()
            or query in _slash_text(command, "label").lower()
            or query in _slash_text(command, "description").lower()
        ]
        if not matches:
            self._hide_slash_commands()
            return

        self._slash_matches = matches
        popup.clear()
        for command in matches:
            label = _slash_text(command, "label")
            description = _slash_text(command, "description")
            item = QListWidgetItem(
                f"/{command['name']}   {label}   ·   {description}"
            )
            item.setData(Qt.ItemDataRole.UserRole, command["name"])
            popup.addItem(item)
        popup.setCurrentRow(0)
        self._position_slash_popup()
        popup.show()
        popup.raise_()
        self._request_edit.set_slash_mode(True)

    def _position_slash_popup(self) -> None:
        popup = self._slash_popup
        if popup is None or not self._slash_matches:
            return
        parent = self._assistant_surface
        top_left = self._request_edit.mapTo(parent, QPoint(0, 0))
        width = min(
            max(300, self._request_edit.width()),
            max(220, parent.width() - 8),
        )
        height = min(300, max(42, len(self._slash_matches) * 34 + 8))
        x = max(4, min(top_left.x(), parent.width() - width - 4))
        y = top_left.y() - height - 4
        if y < 4:
            y = min(parent.height() - height - 4, top_left.y() + self._request_edit.height() + 4)
        popup.setGeometry(x, max(4, y), width, height)

    def _hide_slash_commands(self) -> None:
        self._slash_matches = []
        if self._slash_popup is not None:
            self._slash_popup.hide()
            self._slash_popup.clear()
        self._request_edit.set_slash_mode(False)

    def _on_slash_navigation(self, action: str) -> None:
        popup = self._slash_popup
        if popup is None or popup.isHidden():
            return
        if action == "dismiss":
            self._hide_slash_commands()
            return
        if action in ("up", "down"):
            count = popup.count()
            if not count:
                return
            step = -1 if action == "up" else 1
            popup.setCurrentRow((popup.currentRow() + step) % count)
            return
        if action == "accept":
            self._activate_slash_command(popup.currentRow())

    def _on_slash_item_clicked(self, item: QListWidgetItem) -> None:
        if self._slash_popup is None:
            return
        self._activate_slash_command(self._slash_popup.row(item))

    def _activate_slash_command(self, row: int) -> None:
        if row < 0 or row >= len(self._slash_matches):
            return
        command = self._slash_matches[row]
        self._hide_slash_commands()
        self._request_edit.clear()
        self._request_edit.setFocus()
        signal = getattr(self, command["signal"], None)
        if signal is not None:
            signal.emit()

    # ------------------------------------------------------------------ #
    # ``@`` media-pool attachments
    # ------------------------------------------------------------------ #

    def set_media_pool_provider(self, provider: Any) -> None:
        """Inject a callable returning the current project's media-pool files.

        The provider is deliberately lazy: project switches replace the media
        pool contents without requiring the assistant panel to be recreated.
        Each entry may be a mapping or object with ``asset_id``, ``path``,
        ``display_name``/``name`` and ``media_type`` fields.
        """
        self._media_pool_provider = provider if callable(provider) else None
        self._update_media_mentions()

    def _media_pool_candidates(self) -> list[dict[str, str]]:
        provider = self._media_pool_provider
        if not callable(provider):
            return []
        try:
            values = provider() or ()
        except Exception:  # noqa: BLE001 - a stale project must not break typing
            return []
        candidates: list[dict[str, str]] = []
        for value in values:
            def read(name: str, default: str = "") -> str:
                if isinstance(value, dict):
                    return str(value.get(name, default) or default)
                return str(getattr(value, name, default) or default)

            path = read("path")
            display_name = read("display_name") or read("name") or os.path.basename(path)
            asset_id = read("asset_id")
            if not display_name:
                continue
            candidates.append({
                "asset_id": asset_id,
                "path": path,
                "display_name": display_name,
                "media_type": read("media_type", "file"),
            })
        return candidates

    def _build_media_popup(self, parent: QWidget) -> None:
        """创建输入框上方的媒体池候选面板。"""
        popup = QListWidget(parent)
        popup.setObjectName("assistant_media_mentions")
        popup.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        popup.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        popup.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        popup.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        popup.setUniformItemSizes(True)
        popup.setStyleSheet(
            "QListWidget#assistant_media_mentions {"
            " background: #2d2d31; color: #dedee2;"
            " border: 1px solid #55565d; border-radius: 7px;"
            " padding: 4px; font-size: 11px; }"
            "QListWidget#assistant_media_mentions::item {"
            " padding: 6px 8px; border-radius: 4px; }"
            "QListWidget#assistant_media_mentions::item:selected {"
            " background: #4a3a2c; color: #ffffff; }"
            "QListWidget#assistant_media_mentions::item:hover {"
            " background: #3b3b40; }"
        )
        popup.itemClicked.connect(self._on_media_item_clicked)
        popup.hide()
        self._media_popup = popup

    def _update_media_mentions(self) -> None:
        """Show media-pool files when the current prompt token starts with ``@``."""
        popup = self._media_popup
        if popup is None or self._updating:
            return
        text = self._request_edit.toPlainText()
        cursor_pos = self._request_edit.textCursor().position()
        before_cursor = text[:cursor_pos]
        match = re.search(r"(?:^|\s)@([^\s]*)$", before_cursor)
        if (
            self._request_edit.isReadOnly()
            or match is None
            or not self._media_pool_provider
            or not self._slash_popup_is_hidden()
        ):
            self._hide_media_mentions()
            return

        query = match.group(1).lower()
        candidates = self._media_pool_candidates()
        matches = [
            candidate for candidate in candidates
            if not query
            or query in candidate["display_name"].lower()
            or query in candidate["asset_id"].lower()
        ]
        if not matches:
            self._hide_media_mentions()
            return

        self._media_matches = matches
        self._media_token_span = (match.start(1) - 1, cursor_pos)
        popup.clear()
        for candidate in matches:
            kind = candidate["media_type"] or "file"
            item = QListWidgetItem(
                f"@{candidate['display_name']}   ·   {kind}"
            )
            item.setData(Qt.ItemDataRole.UserRole, candidate["asset_id"])
            popup.addItem(item)
        popup.setCurrentRow(0)
        self._position_media_popup()
        popup.show()
        popup.raise_()
        self._request_edit.set_mention_mode(True)

    def _slash_popup_is_hidden(self) -> bool:
        return self._slash_popup is None or self._slash_popup.isHidden()

    def _position_media_popup(self) -> None:
        popup = self._media_popup
        if popup is None or not self._media_matches:
            return
        parent = self._assistant_surface
        top_left = self._request_edit.mapTo(parent, QPoint(0, 0))
        width = min(max(300, self._request_edit.width()), max(220, parent.width() - 8))
        height = min(260, max(42, len(self._media_matches) * 34 + 8))
        x = max(4, min(top_left.x(), parent.width() - width - 4))
        y = top_left.y() - height - 4
        if y < 4:
            y = min(parent.height() - height - 4, top_left.y() + self._request_edit.height() + 4)
        popup.setGeometry(x, max(4, y), width, height)

    def _hide_media_mentions(self) -> None:
        self._media_matches = []
        self._media_token_span = None
        if self._media_popup is not None:
            self._media_popup.hide()
            self._media_popup.clear()
        self._request_edit.set_mention_mode(False)

    def _on_media_navigation(self, action: str) -> None:
        popup = self._media_popup
        if popup is None or popup.isHidden():
            return
        if action == "dismiss":
            self._hide_media_mentions()
            return
        if action in ("up", "down"):
            count = popup.count()
            if count:
                step = -1 if action == "up" else 1
                popup.setCurrentRow((popup.currentRow() + step) % count)
            return
        if action == "accept":
            self._activate_media_candidate(popup.currentRow())

    def _on_media_item_clicked(self, item: QListWidgetItem) -> None:
        if self._media_popup is not None:
            self._activate_media_candidate(self._media_popup.row(item))

    def _activate_media_candidate(self, row: int) -> None:
        if row < 0 or row >= len(self._media_matches):
            return
        candidate = self._media_matches[row]
        span = self._media_token_span
        if span is None:
            self._hide_media_mentions()
            return
        path = candidate.get("path", "")
        if candidate.get("media_type") == "motion_graphic":
            asset_id = str(candidate.get("asset_id", "") or "").strip()
            if not asset_id:
                self.set_agent_notice(
                    tr(
                        "ai_assistant.motion_graphic_missing_id",
                        "The Motion Graphic in the media pool has no stable asset ID.",
                    ),
                    warning=True,
                )
                return
            self._add_motion_graphic_target(
                asset_id,
                str(candidate.get("display_name", "Motion Graphic Clip") or "Motion Graphic Clip"),
            )
            cursor = self._request_edit.textCursor()
            cursor.setPosition(span[0])
            cursor.setPosition(span[1], cursor.MoveMode.KeepAnchor)
            cursor.insertText("@" + candidate.get("display_name", "Motion Graphic Clip"))
            self._request_edit.setTextCursor(cursor)
            self._hide_media_mentions()
            self._request_edit.setFocus()
            return
        if not self._add_file_attachment(path, source="media_pool"):
            self.set_agent_notice(
                _trf(
                    "ai_assistant.media_pool_file_unavailable",
                    "Media-pool file is unavailable: {name}.",
                    name=candidate.get("display_name", tr("ai_assistant.attachment", "附件")),
                ),
                warning=True,
            )
            return
        cursor = self._request_edit.textCursor()
        cursor.setPosition(span[0])
        cursor.setPosition(span[1], cursor.MoveMode.KeepAnchor)
        cursor.insertText("@" + candidate.get("display_name", "附件"))
        self._request_edit.setTextCursor(cursor)
        self._hide_media_mentions()
        self._request_edit.setFocus()

    def _populate_history_menu(self, menu: Any) -> None:
        """把当前历史会话填入标题栏/右键菜单的二级菜单。"""
        if not self._history_sessions:
            empty = QAction(tr("ai_composer.history_empty", "暂无历史会话"), menu)
            empty.setEnabled(False)
            menu.addAction(empty)
            return
        for entry in self._history_sessions:
            session_id = self._history_value(entry, "session_id", "")
            if not session_id:
                continue
            title = str(self._history_value(entry, "title", "") or "").strip()
            messages = self._history_value(entry, "messages", ()) or ()
            preview = ""
            for message in messages:
                if self._history_value(message, "role", "") == "user":
                    preview = str(self._history_value(message, "text", "") or "")
                    break
            if not title:
                title = preview.replace("\n", " ")[:30] or f"会话 {str(session_id)[:8]}"
            label = title
            if preview and preview.replace("\n", " ").strip() != title:
                label += f"  ·  {preview.replace(chr(10), ' ').strip()[:42]}"
            action = QAction(label, menu)
            action.setData(str(session_id))
            action.setCheckable(True)
            action.setChecked(str(session_id) == self._active_history_session_id)
            action.triggered.connect(
                lambda _checked=False, sid=str(session_id):
                self.history_session_requested.emit(sid)
            )
            menu.addAction(action)

    @staticmethod
    def _history_value(value: object, name: str, default: object = None) -> object:
        if isinstance(value, dict):
            return value.get(name, default)
        return getattr(value, name, default)

    # ------------------------------------------------------------------ #
    # UI 构建
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("assistant_surface")
        central.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        central.customContextMenuRequested.connect(
            lambda pos, surface=central: self._show_context_menu(
                surface.mapToGlobal(pos)
            )
        )
        # 允许 Dock 在窄侧栏中收紧；330px 仍能容纳底部控件和发送按钮。
        central.setMinimumWidth(330)
        central.setStyleSheet(
            "QWidget#assistant_surface { background: #27272b; color: #d8d8dc;"
            " border: none; border-radius: 0; }"
            "QWidget#assistant_surface QLabel { color: #d8d8dc; }"
            "QLabel#assistant_status { color: #d8d8dc; padding: 1px 0; font-size: 10px; }"
            "QLabel#assistant_thinking { color: #e87519; padding: 1px 0; font-size: 10px; }"
            "QToolButton#assistant_overflow {"
            " border: none; padding: 3px 6px; border-radius: 4px; }"
            "QToolButton#assistant_overflow:hover {"
            " background: palette(mid); }"
            "QPlainTextEdit#assistant_input { background: transparent; color: #d8d8dc;"
            " border: none; border-radius: 0px; padding: 7px 7px 2px 7px; }"
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget#qt_scrollarea_viewport { background: transparent; }"
            "QWidget#assistant_messages_container, QWidget#assistant_variants_container,"
            " QWidget#assistant_context_container { background: transparent; }"
            "QScrollBar:vertical { background: transparent; width: 7px;"
            " margin: 2px 1px 2px 2px; }"
            "QScrollBar::handle:vertical { background: #55565d;"
            " min-height: 28px; border-radius: 3px; }"
            "QScrollBar::handle:vertical:hover { background: #777982; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {"
            " height: 0px; background: transparent; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {"
            " background: transparent; }"
            "QScrollBar:horizontal { height: 0px; background: transparent; }"
            "QScrollBar::handle:horizontal { background: transparent; }"
            "QFrame#assistant_composer_shell { background: transparent; border: none; }"
            "QFrame#assistant_activity_status { background: #252529; border: none;"
            " border-radius: 4px; }"
            "QLabel#assistant_activity_status_label { color: #bfc0c6;"
            " padding: 2px 6px; font-size: 10px; }"
            "QFrame#assistant_input_frame { background: #252529;"
            " border: 1px solid #4a4a4a; border-radius: 6px; }"
            "QFrame#assistant_input_frame:hover { border-color: #4b4d55; }"
            "QPushButton#assistant_send { background: #e87519; color: white;"
            " border: none; border-radius: 6px; padding: 0px;"
            " min-width: 24px; max-width: 24px; min-height: 24px;"
            " max-height: 24px; font-size: 14px; font-weight: 600; }"
            "QPushButton#assistant_send:hover { background: #f28a2e; }"
            "QPushButton#assistant_send:pressed { background: #cc620f; }"
            "QPushButton#assistant_send:disabled { background: #8f531f;"
            " color: #e6e6e6; }"
        )
        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        # Header（M5：profile/隐私胶囊 + 上下文检查器入口；§2.1 状态胶囊）
        header_row = QWidget()
        hr_layout = QHBoxLayout(header_row)
        hr_layout.setContentsMargins(0, 0, 0, 0)
        hr_layout.setSpacing(4)
        self._conversation_title_btn = QToolButton()
        self._conversation_title_btn.setObjectName("assistant_conversation_title")
        self._conversation_title_btn.setAutoRaise(True)
        self._conversation_title_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._conversation_title_btn.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred,
        )
        self._conversation_title_btn.setStyleSheet(
            "QToolButton { font-weight: 600; font-size: 11px; text-align: left;"
            " border: none; padding: 1px 0; color: #e6e6e6; }"
            "QToolButton:hover { color: #e87519; }"
        )
        self._conversation_title_btn.setToolTip(
            tr("ai_assistant.edit_conversation_title", "点击修改当前会话标题")
        )
        self._conversation_title_btn.clicked.connect(
            self._on_conversation_title_clicked
        )
        # Keep the old private name for lightweight host/test integrations.
        self._header_label = self._conversation_title_btn
        hr_layout.addWidget(self._conversation_title_btn)
        self._context_budget_label = QLabel()
        self._context_budget_label.setStyleSheet(
            "color: gray; font-size: 10px; padding: 1px 4px;"
        )
        self._context_budget_label.setToolTip(tr(
            "ai_assistant.context_budget_tooltip",
            "当前对话上下文 token 用量与上限（本地估算）",
        ))
        hr_layout.addWidget(self._context_budget_label)
        self._state_label = QLabel()
        self._state_label.setObjectName("assistant_status")
        self._state_label.setVisible(False)
        hr_layout.addWidget(self._state_label)
        # M5：上下文检查器入口（点开看本次模型看到了什么 / token / 可排除块）
        self._ctx_inspect_btn = QToolButton()
        self._ctx_inspect_btn.setText(tr("ai_assistant.inspect_context", "上下文"))
        self._ctx_inspect_btn.setToolTip(tr(
            "ai_assistant.inspect_context_tooltip",
            "检查本次模型看到的上下文（token/优先级/可排除）",
        ))
        self._ctx_inspect_btn.setStyleSheet("padding: 2px;")
        self._ctx_inspect_btn.setVisible(False)
        self._ctx_inspect_btn.clicked.connect(self._on_inspect_context_clicked)
        hr_layout.addWidget(self._ctx_inspect_btn)
        self._thinking_indicator = QLabel()
        self._thinking_indicator.setObjectName("assistant_thinking")
        self._thinking_indicator.setVisible(False)
        hr_layout.addWidget(self._thinking_indicator)
        self._overflow_btn = QToolButton()
        self._overflow_btn.setObjectName("assistant_overflow")
        self._overflow_btn.setText("⋯")
        self._overflow_btn.setToolTip(tr("ai_assistant.more_actions", "更多操作"))
        self._overflow_btn.clicked.connect(self._show_overflow_menu)
        hr_layout.addWidget(self._overflow_btn)
        layout.addWidget(header_row)

        # 堆叠区：按状态切换内容
        self._stack = QStackedWidget()
        # AUD-19509: 页码具名常量（addWidget 顺序即页号），消除魔法数字。
        #   0 empty / 1 no_profile / 2 main / 3 error / 4 brief_editor / 5 ctx
        self._PAGE_EMPTY = 0
        self._PAGE_NO_PROFILE = 1
        self._PAGE_MAIN = 2
        self._PAGE_ERROR = 3
        self._PAGE_BRIEF_EDITOR = 4
        self._PAGE_CONTEXT_INSPECTOR = 5
        # Brief 表单等低频页面不应把整个 Dock 的最小宽度抬高。
        self._stack.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding
        )

        # --- empty project 页 ---
        empty_page = QWidget()
        empty_layout = QVBoxLayout(empty_page)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(QLabel(tr(
            "ai_assistant.empty_project",
            "尚无打开的工程。\n请先导入素材以使用 AI 编辑助手。",
        )))
        self._stack.addWidget(empty_page)

        # --- no profile 页（UI-1 之前：只允许编辑 Brief） ---
        no_profile_page = QWidget()
        npp_layout = QVBoxLayout(no_profile_page)
        npp_layout.addWidget(QLabel(tr(
            "ai_assistant.no_profile",
            "未配置模型 profile。\n可编辑 Brief 与查看上下文，AI 规划需先配置 profile。",
        )))
        self._stack.addWidget(no_profile_page)

        # --- idle/busy 主页 ---
        main_page = QWidget()
        mp_layout = QVBoxLayout(main_page)
        mp_layout.setContentsMargins(2, 2, 2, 2)
        mp_layout.setSpacing(4)

        self._composer_shell = QFrame()
        self._composer_shell.setObjectName("assistant_composer_shell")
        # Composer 必须贴在主内容底部；如果允许它垂直扩张，内部 footer
        # 会吸收整个 Dock 的剩余空间，造成“输入框在上方、工具栏在下方”的
        # Codex 式布局破坏。
        self._composer_shell.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        composer_shell_layout = QVBoxLayout(self._composer_shell)
        composer_shell_layout.setContentsMargins(0, 0, 0, 0)
        composer_shell_layout.setSpacing(0)

        # 固定在输入框上方的 AI 工作状态栏：不会随聊天滚动消失，且用流光
        # 文字动画持续反馈“界面仍在执行”。具体阶段由 controller/runtime 推送。
        self._activity_status_bar = QFrame(self._composer_shell)
        self._activity_status_bar.setObjectName("assistant_activity_status")
        self._activity_status_bar.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed,
        )
        activity_status_layout = QHBoxLayout(self._activity_status_bar)
        activity_status_layout.setContentsMargins(0, 0, 0, 0)
        activity_status_layout.setSpacing(0)
        self._activity_status_label = QLabel()
        self._activity_status_label.setObjectName("assistant_activity_status_label")
        self._activity_status_label.setTextFormat(Qt.TextFormat.RichText)
        self._activity_status_label.setWordWrap(False)
        activity_status_layout.addWidget(self._activity_status_label)
        self._activity_status_bar.setVisible(False)
        self._activity_status_text = ""
        self._activity_status_detail = ""
        self._activity_status_active = False
        self._activity_status_status = "running"
        self._activity_shimmer_phase = 0
        # The activity surface is intentionally the compact Run Drawer below
        # the conversation.  Keep the old widget as a compatibility fallback
        # for hosts that explicitly opt into a composer footer, but do not add
        # it to this panel's layout.
        self._activity_status_in_footer = False
        self._activity_shimmer_timer = QTimer(self)
        self._activity_shimmer_timer.setInterval(90)
        self._activity_shimmer_timer.timeout.connect(self._animate_activity_status)

        self._composer_input_frame = QFrame()
        self._composer_input_frame.setObjectName("assistant_input_frame")
        composer_input_layout = QVBoxLayout(self._composer_input_frame)
        composer_input_layout.setContentsMargins(0, 0, 0, 0)
        composer_input_layout.setSpacing(0)

        # M5：上下文摘要（接入死代码 AiContextSummary widget；§2.2 上下文栏）
        # 比 _context_label 更丰富：显示 analysis_fresh/snapshot_revision + 操作按钮。
        try:
            from src.features.edit.ui.ai_context_summary import AiContextSummary
            self._context_details = QFrame()
            context_layout = QVBoxLayout(self._context_details)
            context_layout.setContentsMargins(0, 0, 0, 0)
            context_layout.setSpacing(0)
            self._context_summary = AiContextSummary()
            self._context_summary.setSizePolicy(
                QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
            )
            self._context_details.setSizePolicy(
                QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
            )
            context_layout.addWidget(self._context_summary)
            mp_layout.addWidget(self._context_details)
            self._context_label = None  # 旧 QLabel 已弃用
        except Exception:
            # AiContextSummary 装配失败 → 回退到旧 QLabel（不阻断面板）
            self._context_details = QFrame()
            context_layout = QVBoxLayout(self._context_details)
            context_layout.setContentsMargins(0, 0, 0, 0)
            self._context_summary = None
            self._context_label = QLabel()
            self._context_label.setStyleSheet("color: gray; padding: 1px;")
            self._context_details.setSizePolicy(
                QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
            )
            context_layout.addWidget(self._context_label)
            mp_layout.addWidget(self._context_details)

        # P7/P8：插件状态只消费 controller 投影，不访问插件实现或 registry 内部。
        self._plugin_status_strip = QFrame()
        self._plugin_status_strip.setObjectName("assistant_plugin_status_strip")
        self._plugin_status_strip.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed,
        )
        plugin_status_layout = QHBoxLayout(self._plugin_status_strip)
        plugin_status_layout.setContentsMargins(4, 1, 4, 1)
        plugin_status_layout.setSpacing(4)
        self._plugin_status_label = QLabel()
        self._plugin_status_label.setObjectName("assistant_plugin_status")
        self._plugin_status_label.setStyleSheet(
            "color: #9ea0a8; font-size: 9px; padding: 1px 0;"
        )
        self._plugin_status_label.setTextFormat(Qt.TextFormat.PlainText)
        plugin_status_layout.addWidget(self._plugin_status_label)
        self._plugin_status_strip.setVisible(False)
        mp_layout.addWidget(self._plugin_status_strip)

        # Codex 式三段布局：上下文摘要在顶部，所有对话内容集中在中间，
        # composer 永远贴在底部。隐藏的消息/方案控件只影响这个容器内部，
        # 不会再把外层输入区和 footer 撑开。
        conversation_host = QWidget()
        conversation_host.setObjectName("assistant_conversation_host")
        conversation_host.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )
        conversation_layout = QVBoxLayout(conversation_host)
        conversation_layout.setContentsMargins(0, 0, 0, 0)
        conversation_layout.setSpacing(4)
        self._main_page_layout = conversation_layout
        mp_layout.addWidget(conversation_host, stretch=1)

        # ---- M5：消息区（对话历史；snapshot.messages 之前从未渲染）---- #
        # 镜像 _variants_area 的 QScrollArea + 容器 QVBoxLayout 模式。
        self._messages_area = QScrollArea()
        self._messages_area.setObjectName("assistant_messages_area")
        self._messages_area.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )
        self._messages_area.setWidgetResizable(True)
        self._messages_area.setFrameShape(QFrame.Shape.NoFrame)
        self._messages_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._messages_area.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._messages_container = QWidget()
        self._messages_container.setObjectName("assistant_messages_container")
        self._messages_container.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground
        )
        self._messages_container.setAutoFillBackground(False)
        self._messages_container.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
        )
        self._messages_layout = QVBoxLayout(self._messages_container)
        self._messages_layout.setContentsMargins(0, 0, 0, 0)
        self._messages_layout.setSpacing(4)
        self._messages_layout.addStretch(1)
        self._messages_area.setWidget(self._messages_container)
        self._messages_area.viewport().setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground
        )
        self._messages_area.viewport().setAutoFillBackground(False)
        self._messages_area.viewport().setStyleSheet("background: transparent;")
        self._messages_area.setVisible(False)  # 无消息时隐藏（避免空滚动区）
        # 对话是主内容；方案卡出现时仍给消息区更多阅读空间。
        conversation_layout.addWidget(self._messages_area, stretch=2)

        # P1：流式对话暂存区。增量内容只显示在 UI，不进入会话历史，直到
        # controller 收到完整回复后再一次性写入 AssistantService。
        self._streaming_label = QLabel()
        self._streaming_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._streaming_label.setMaximumHeight(0)
        self._streaming_label.setWordWrap(True)
        self._streaming_label.setTextFormat(Qt.TextFormat.MarkdownText)
        self._streaming_label.setOpenExternalLinks(True)
        self._streaming_label.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self._streaming_label.setStyleSheet(
            "color: #b6b7bd; background: transparent; padding: 4px 6px;"
            " border: none;"
        )
        self._streaming_label.setVisible(False)
        # 流式回复也必须位于同一个滚动容器内；否则长回复会把外层布局往下推，
        # 让 composer 失去固定底部位置。
        self._messages_layout.insertWidget(
            self._messages_layout.count() - 1, self._streaming_label,
        )

        # ---- variant 列表区（可滚动；按 snapshot.variants 渲染）---- #
        self._variants_area = QScrollArea()
        self._variants_area.setObjectName("assistant_variants_area")
        self._variants_area.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )
        self._variants_area.setWidgetResizable(True)
        self._variants_area.setFrameShape(QFrame.Shape.NoFrame)
        self._variants_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._variants_area.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._variants_container = QWidget()
        self._variants_container.setObjectName("assistant_variants_container")
        self._variants_container.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground
        )
        self._variants_container.setAutoFillBackground(False)
        self._variants_layout = QVBoxLayout(self._variants_container)
        self._variants_layout.setContentsMargins(0, 0, 0, 0)
        self._variants_layout.setSpacing(4)
        self._variants_layout.addStretch(1)
        self._variants_area.setWidget(self._variants_container)
        self._variants_area.viewport().setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground
        )
        self._variants_area.viewport().setAutoFillBackground(False)
        self._variants_area.viewport().setStyleSheet("background: transparent;")
        conversation_layout.addWidget(self._variants_area, stretch=1)

        # 状态占位（无 variant 时提示）
        self._placeholder = QLabel(tr(
            "ai_assistant.idle_hint",
            "输入需求以生成剪辑方案。\n助手会给出至少两个可比较的方案。",
        ))
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setStyleSheet("padding: 12px; color: gray;")
        self._placeholder.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        conversation_layout.addWidget(self._placeholder, stretch=1)

        # ---- request composer（M1.3：多行需求输入 + 发送/停止）---- #
        # 多模态：待发送图片的缩略图预览条（无附件时隐藏）。
        self._attachment_preview_row = QFrame()
        self._attachment_preview_row.setObjectName("assistant_attachment_row")
        self._attachment_preview_layout = QHBoxLayout(self._attachment_preview_row)
        self._attachment_preview_layout.setContentsMargins(4, 2, 4, 2)
        self._attachment_preview_layout.setSpacing(4)
        self._attachment_preview_layout.addStretch(1)
        self._attachment_preview_row.setVisible(False)
        composer_input_layout.addWidget(self._attachment_preview_row)

        self._request_edit = _AssistantPromptEdit()
        self._request_edit.setPlaceholderText(tr(
            "ai_assistant.request_placeholder",
            "描述你的需求（Enter 发送，Shift+Enter 换行；可粘贴截图）…",
        ))
        self._request_edit.setMaximumHeight(80)
        self._request_edit.setMinimumWidth(88)
        self._request_edit.setObjectName("assistant_input")
        # 长度上限（防止超长 prompt；真实 provider 在 M5 再做 token 预算）
        self._request_edit.setPlainText("")
        self._request_edit.submitted.connect(self._on_send_clicked)
        self._request_edit.cancelled.connect(self.cancel_requested.emit)
        self._request_edit.slash_navigation.connect(self._on_slash_navigation)
        self._request_edit.mention_navigation.connect(self._on_media_navigation)
        # 多模态：粘贴图片 → 加入待发送附件。
        self._request_edit.image_pasted.connect(self._on_image_pasted)
        self._request_edit.file_dropped.connect(self._on_files_dropped)
        self._request_edit.textChanged.connect(
            lambda: self.draft_changed.emit(self._request_edit.toPlainText())
        )
        self._request_edit.textChanged.connect(self._update_slash_commands)
        self._request_edit.textChanged.connect(self._update_media_mentions)
        composer_input_layout.addWidget(self._request_edit)

        # Codex 风格的单一发送/停止按钮：空闲显示上箭头，忙碌时显示方块。
        self._send_btn = QPushButton()
        self._send_btn.setObjectName("assistant_send")
        self._send_btn.setFixedSize(24, 24)
        self._send_btn.clicked.connect(self._on_send_or_cancel_clicked)
        # 保留旧属性名，兼容宿主/测试对取消按钮的引用；视觉上只有一个按钮。
        self._cancel_btn = self._send_btn
        self._set_send_button_busy(False)

        # ---- Run Drawer（plan §10.2 P0.6：运行活动流，先接 fake 数据）---- #
        # 显示逐项任务状态/进度/取消/重试/诊断，避免用户只看到"规划中"。
        # 可选装配：导入失败回退到无 drawer（不阻断面板）。
        self._run_drawer = None
        self._activity_started_at = 0.0
        self._activity_item_id = ""
        self._activity_timer = QTimer(self)
        self._activity_timer.setInterval(1000)
        self._activity_timer.timeout.connect(self._refresh_activity_status)
        try:
            from src.features.edit.ui.ai_assistant_composer import AiRunDrawer
            # 对话区只保留一行最新活动；完整节点详情在 Workflow 视图查看。
            self._run_drawer = AiRunDrawer(compact=True, single_line=True)
            self._run_drawer.cancel_clicked.connect(self._on_run_cancel)
            self._run_drawer.retry_clicked.connect(self._on_run_retry)
            self._run_drawer.diagnostics_clicked.connect(self._on_run_diagnostics)
            self._run_section = QFrame()
            self._run_section.setSizePolicy(
                QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
            )
            self._run_section.setMaximumHeight(0)
            run_layout = QVBoxLayout(self._run_section)
            run_layout.setContentsMargins(0, 0, 0, 1)
            run_layout.setSpacing(0)
            run_layout.addWidget(self._run_drawer)
            self._run_section.setVisible(False)
            # Activity is part of the message flow, not a fixed footer. It
            # therefore scrolls away together with the conversation history.
            self._messages_layout.insertWidget(
                self._messages_layout.count() - 1, self._run_section,
            )
        except Exception:
            self._run_drawer = None
            self._run_section = None

        # ---- Composer footer（plan §10.2 P0.3：底部控制栏）---- #
        # Scope/权限/Profile/上下文/压缩/新会话/历史 + 成本/网络提示。
        # 可选装配：导入失败回退到无 footer（不阻断面板）。
        self._composer_footer = None
        try:
            from src.features.edit.ui.ai_assistant_composer import AiAssistantComposer
            self._composer_footer = AiAssistantComposer()
            self._composer_footer.setSizePolicy(
                QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
            )
            self._composer_footer.scope_clicked.connect(self._on_composer_scope)
            self._composer_footer.permission_changed.connect(self._on_composer_permission)
            self._composer_footer.profile_clicked.connect(self._on_composer_profile)
            self._composer_footer.profile_changed.connect(self.composer_profile_changed.emit)
            self._composer_footer.mode_changed.connect(self.composer_mode_changed.emit)
            self._composer_footer.reasoning_changed.connect(self.composer_reasoning_changed.emit)
            self._composer_footer.context_inspect_clicked.connect(self._on_inspect_context_clicked)
            self._composer_footer.context_compress_clicked.connect(self._on_context_compress)
            self._composer_footer.new_session_clicked.connect(self._on_new_session)
            self._composer_footer.history_clicked.connect(self._on_show_history)
            self._composer_footer.history_session_selected.connect(
                self.history_session_requested.emit
            )
            self._composer_footer.configure_models_clicked.connect(
                self.configure_models_requested.emit
            )
            self._composer_footer.add_action_requested.connect(
                self._on_composer_add_action
            )
            self._composer_footer.add_action_widget(self._send_btn)
            composer_input_layout.addWidget(self._composer_footer)
        except Exception:
            self._composer_footer = None
            fallback_row = QHBoxLayout()
            fallback_row.addStretch(1)
            fallback_row.addWidget(self._send_btn)
            composer_input_layout.addLayout(fallback_row)

        # 输入区、底部选项与发送按钮共享同一个视觉容器。
        composer_shell_layout.addWidget(self._composer_input_frame)

        # Composer 统一作为主内容底部最后一块，输入/发送/工具栏保持一个视觉容器。
        mp_layout.addWidget(self._composer_shell)

        self._stack.addWidget(main_page)

        # --- error 页 ---
        error_page = QWidget()
        ep_layout = QVBoxLayout(error_page)
        self._error_label = QLabel()
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet("color: #c0392b; padding: 8px;")
        ep_layout.addWidget(self._error_label)
        retry_btn = QPushButton(tr("ai_assistant.retry", "重试"))
        retry_btn.clicked.connect(self.retry_requested.emit)
        ep_layout.addWidget(retry_btn)
        ep_layout.addStretch(1)
        self._stack.addWidget(error_page)

        # --- brief 批准页（M1.3：AWAITING_BRIEF_APPROVAL 时显示）---
        # 内嵌 AiBriefEditor；AI 提议的 patch 先 merge 成预览 brief 回填进表单，
        # 用户可改后点"批准 Brief"。editor 的 brief_patch_requested 转发到本面板
        # 同名信号，controller 据此触发 plan。
        from src.features.edit.ui.ai_brief_editor import AiBriefEditor

        self._brief_editor = AiBriefEditor()
        # 转发：editor emit → panel emit（controller 连的是 panel 的信号）
        self._brief_editor.brief_patch_requested.connect(self.brief_patch_requested.emit)
        self._stack.addWidget(self._brief_editor)

        # --- M5：上下文检查器页（§7.1 可检查/可预算/可排除）---
        # 显示 ContextBundle 每块的 kind/优先级/token/为何包含/可排除；用户排除某块
        # → emit context_block_excluded。无 bundle 时显示提示。
        ctx_page = QWidget()
        cp_layout = QVBoxLayout(ctx_page)
        cp_layout.setContentsMargins(8, 8, 8, 8)
        cp_layout.setSpacing(6)
        ctx_header_row = QWidget()
        chr_layout = QHBoxLayout(ctx_header_row)
        chr_layout.setContentsMargins(0, 0, 0, 0)
        chr_layout.addWidget(QLabel(f"<b>{tr('ai_assistant.context_inspector_title', '本次模型看到的上下文')}</b>"))
        ctx_back_btn = QPushButton(tr("ai_assistant.back", "返回"))
        ctx_back_btn.clicked.connect(lambda: self._stack.setCurrentIndex(self._PAGE_MAIN))
        chr_layout.addStretch(1)
        chr_layout.addWidget(ctx_back_btn)
        cp_layout.addWidget(ctx_header_row)
        self._ctx_total_label = QLabel()
        self._ctx_total_label.setStyleSheet("color: gray;")
        cp_layout.addWidget(self._ctx_total_label)
        self._ctx_blocks_area = QScrollArea()
        self._ctx_blocks_area.setObjectName("assistant_context_area")
        self._ctx_blocks_area.setWidgetResizable(True)
        self._ctx_blocks_area.setFrameShape(QFrame.Shape.NoFrame)
        self._ctx_blocks_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._ctx_blocks_container = QWidget()
        self._ctx_blocks_container.setObjectName("assistant_context_container")
        self._ctx_blocks_container.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground
        )
        self._ctx_blocks_container.setAutoFillBackground(False)
        self._ctx_blocks_layout = QVBoxLayout(self._ctx_blocks_container)
        self._ctx_blocks_layout.setContentsMargins(0, 0, 0, 0)
        self._ctx_blocks_layout.setSpacing(4)
        self._ctx_blocks_layout.addStretch(1)
        self._ctx_blocks_area.setWidget(self._ctx_blocks_container)
        self._ctx_blocks_area.viewport().setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground
        )
        self._ctx_blocks_area.viewport().setAutoFillBackground(False)
        self._ctx_blocks_area.viewport().setStyleSheet("background: transparent;")
        cp_layout.addWidget(self._ctx_blocks_area, stretch=1)
        self._stack.addWidget(ctx_page)

        layout.addWidget(self._stack, stretch=1)
        self._assistant_surface = central
        self._build_slash_popup(central)
        self._build_media_popup(central)
        self.setWidget(central)

    def set_pending_brief(self, brief: object) -> None:
        """把 AI 提议的预览 brief 回填进内嵌 Brief 编辑器（M1.3）。

        用 ``_updating`` 锁避免 set_brief 触发编辑器内部 change signal。
        """
        self._updating = True
        try:
            self._brief_editor.set_brief(brief)  # type: ignore[arg-type]
        finally:
            self._updating = False

    def set_history_sessions(
        self, sessions: object, *, active_session_id: str = ""
    ) -> None:
        """刷新历史会话二级菜单，供 controller 在缓存/工程变化后调用。"""
        self._history_sessions = list(sessions or [])
        self._active_history_session_id = str(active_session_id or "")
        if self._active_history_session_id:
            active_entry = next(
                (
                    entry for entry in self._history_sessions
                    if str(self._history_value(entry, "session_id", ""))
                    == self._active_history_session_id
                ),
                None,
            )
            if active_entry is None:
                self.set_conversation_title("")
            else:
                title = str(self._history_value(active_entry, "title", "") or "")
                if not title:
                    title = self._first_user_message_text(
                        self._history_value(active_entry, "messages", ()) or ()
                    )
                self.set_conversation_title(
                    title,
                    is_custom=bool(str(self._history_value(active_entry, "title", "") or "").strip()),
                )

    @staticmethod
    def _first_user_message_text(messages: object) -> str:
        for message in messages or ():
            if AiEditAssistantPanel._history_value(message, "role", "") == "user":
                text = str(AiEditAssistantPanel._history_value(message, "text", "") or "")
                return " ".join(text.split())
        return ""

    @staticmethod
    def _compact_conversation_title(title: str) -> str:
        normalized = " ".join(str(title or "").split())
        if len(normalized) <= 8:
            return normalized
        return normalized[:7] + "…"

    def set_conversation_title(self, title: str, *, is_custom: bool = False) -> None:
        """更新顶部标题；显示短标题，完整标题保留在 tooltip 中。"""
        normalized = " ".join(str(title or "").split())
        self._conversation_title = normalized
        self._conversation_title_is_custom = bool(is_custom and normalized)
        display = self._compact_conversation_title(normalized) or tr(
            "ai_composer.new_session", "新对话"
        )
        self._conversation_title_btn.setText(display)
        edit_hint = tr("ai_assistant.edit_conversation_title", "点击修改当前会话标题")
        self._conversation_title_btn.setToolTip(
            normalized + "\n" + edit_hint if normalized else edit_hint
        )

    def _on_conversation_title_clicked(self) -> None:
        current = self._conversation_title or tr("ai_composer.new_session", "新对话")
        title, accepted = QInputDialog.getText(
            self,
            tr("ai_assistant.edit_conversation_title_dialog", "修改会话标题"),
            tr("ai_assistant.conversation_title_label", "会话标题："),
            text=current,
        )
        if not accepted:
            return
        normalized = " ".join(str(title or "").split())
        if not normalized:
            return
        self.set_conversation_title(normalized, is_custom=True)
        self.conversation_title_change_requested.emit(normalized)

    def set_thinking(self, thinking: bool, text: str = "") -> None:
        """显示/隐藏非阻塞的模型思考状态提示。"""
        visible = bool(thinking)
        # A Run Drawer activity is more informative than the legacy header
        # dots.  Keep this fallback for states that have not created a run
        # item yet, but never let it cover the fixed composer status bar.
        if visible:
            label = text or tr("ai_assistant.thinking", "思考中")
            self._thinking_indicator.setText(f"{label}…")
            self._thinking_indicator.setVisible(True)
            if not self._thinking_timer.isActive():
                self._thinking_timer.start()
        else:
            self._thinking_timer.stop()
            self._thinking_indicator.setVisible(False)

    def _animate_thinking(self) -> None:
        """轻量点状动画，明确区分“正在思考”和“界面卡住”。"""
        if not self._thinking_indicator.isVisible():
            return
        self._thinking_phase = (self._thinking_phase + 1) % 4
        base = self._thinking_indicator.text().rstrip(".…")
        self._thinking_indicator.setText(base + "." * self._thinking_phase)

    def set_activity_status(
        self,
        text: str,
        *,
        detail: str = "",
        active: bool = True,
        status: str = "running",
    ) -> None:
        """Show a Codex-style activity line immediately above the composer.

        This is deliberately a small public UI contract.  Controllers and
        future tool runtimes can push human-readable phases without knowing
        how the panel animates or lays them out.  ``active`` keeps the shimmer
        running while work is in progress; terminal states remain visible so
        a slow or failed request is distinguishable from a dropped update.
        """
        bar = getattr(self, "_activity_status_bar", None)
        label = getattr(self, "_activity_status_label", None)
        if bar is None or label is None:
            return
        self._activity_status_text = str(
            text or tr("ai_assistant.activity_working", "AI 正在工作")
        ).strip()
        self._activity_status_detail = str(detail or "").strip()
        self._activity_status_active = bool(active)
        self._activity_status_status = str(status or "running")
        if not getattr(self, "_activity_status_in_footer", False):
            drawer = getattr(self, "_run_drawer", None)
            live = getattr(drawer, "set_live_status", None)
            if callable(live):
                live(
                    self._activity_status_text,
                    detail=self._activity_status_detail,
                    status=self._activity_status_status,
                )
            if self._activity_status_active:
                # AUD-19513: 无动画目标（无 Run Drawer / drawer 无 live 接口）
                # 时不再启动 90ms shimmer 定时器空转。
                if self._activity_status_has_animation_target() and (
                    not self._activity_shimmer_timer.isActive()
                ):
                    self._activity_shimmer_timer.start()
                self._animate_activity_status()
            else:
                self._activity_shimmer_timer.stop()
            return
        bar.setVisible(True)
        if self._activity_status_active:
            if self._activity_status_has_animation_target() and (
                not self._activity_shimmer_timer.isActive()
            ):
                self._activity_shimmer_timer.start()
            self._animate_activity_status()
        else:
            self._activity_shimmer_timer.stop()
            self._render_static_activity_status()

    def _activity_status_has_animation_target(self) -> bool:
        """AUD-19513: 是否存在 shimmer 动画的实际渲染目标。

        非 footer 模式需要 Run Drawer 且其暴露 ``set_live_status``；
        footer 模式需要活动状态 label 存在。
        """
        if getattr(self, "_activity_status_in_footer", False):
            return getattr(self, "_activity_status_label", None) is not None
        drawer = getattr(self, "_run_drawer", None)
        return callable(getattr(drawer, "set_live_status", None))

    def clear_activity_status(self) -> None:
        """Hide the fixed activity line when there is no current run."""
        timer = getattr(self, "_activity_shimmer_timer", None)
        if timer is not None:
            timer.stop()
        bar = getattr(self, "_activity_status_bar", None)
        if bar is not None:
            bar.setVisible(False)
        self._activity_status_text = ""
        self._activity_status_detail = ""
        self._activity_status_active = False

    def _activity_status_suffix(self) -> str:
        detail = str(getattr(self, "_activity_status_detail", "") or "").strip()
        return f" · {detail}" if detail else ""

    def _render_static_activity_status(self) -> None:
        """Render a terminal activity without an animated cursor."""
        status = str(getattr(self, "_activity_status_status", "running") or "running")
        icon = {
            "succeeded": "✓",
            "failed": "×",
            "cancelled": "■",
            "skipped": "–",
            "waiting_input": "!",
        }.get(status, "◌")
        plain = f"{icon} {self._activity_status_text}{self._activity_status_suffix()}"
        color = "#a7d98b" if status in ("succeeded", "skipped") else (
            "#ef8d8d" if status == "failed" else "#e8a04a"
        )
        self._activity_status_label.setText(
            f"<span style='color:{color}'>{_html.escape(plain)}</span>"
        )
        self._activity_status_label.setToolTip(plain)

    def _animate_activity_status(self) -> None:
        """Move a warm highlight through the status text as a liveness cue."""
        if not getattr(self, "_activity_status_active", False):
            return
        if not self._activity_status_has_animation_target():
            # AUD-19513: 动画目标消失（如 drawer 被卸载）时自愈停表，
            # 避免定时器在无渲染目标的情况下持续空转。
            self._activity_shimmer_timer.stop()
            return
        if not getattr(self, "_activity_status_in_footer", False):
            drawer = getattr(self, "_run_drawer", None)
            live = getattr(drawer, "set_live_status", None)
            if callable(live):
                spinner = ("◌", "◍", "◎", "◉")[
                    int(getattr(self, "_activity_shimmer_phase", 0)) % 4
                ]
                live(
                    self._activity_status_text,
                    detail=self._activity_status_detail,
                    status=self._activity_status_status,
                    activity_icon=spinner,
                )
                self._activity_shimmer_phase = (
                    int(getattr(self, "_activity_shimmer_phase", 0)) + 1
                ) % 4
            return
        label = getattr(self, "_activity_status_label", None)
        if label is None:
            return
        plain = f"◌ {self._activity_status_text}{self._activity_status_suffix()}"
        if not plain:
            return
        visible_positions = [index for index, char in enumerate(plain) if not char.isspace()]
        if not visible_positions:
            label.setText(_html.escape(plain))
            return
        phase = int(getattr(self, "_activity_shimmer_phase", 0)) % len(visible_positions)
        highlight = visible_positions[phase]
        parts: list[str] = []
        for index, char in enumerate(plain):
            escaped = _html.escape(char)
            if index == highlight:
                parts.append(f"<span style='color:#f6a15a'>{escaped}</span>")
            else:
                parts.append(f"<span style='color:#bfc0c6'>{escaped}</span>")
        label.setText("".join(parts))
        label.setToolTip(plain)
        self._activity_shimmer_phase = (phase + 1) % len(visible_positions)

    # ------------------------------------------------------------------ #
    # 状态回填（用 _updating 锁避免信号递归）
    # ------------------------------------------------------------------ #

    def refresh_state(self, snapshot: AssistantStateSnapshot) -> None:
        """由 controller 调用：用 service 的状态快照刷新 UI。

        ``_updating`` 锁防止回填过程中控件 signal 触发新的请求（递归）。
        """
        self._updating = True
        try:
            self._state = snapshot
            self._apply_state(snapshot)
        finally:
            self._updating = False

    def _apply_state(self, s: AssistantStateSnapshot) -> None:
        self._set_send_button_busy(s.state.is_busy)

        # Header：显示当前会话标题；模型与连接策略已在底部选择器中展示。
        if not self._conversation_title_is_custom:
            self.set_conversation_title(self._first_user_message_text(s.messages))

        # 上下文摘要（M5：优先喂 AiContextSummary widget；回退旧 QLabel）
        ctx = s.context
        if self._context_summary is not None:
            self._context_summary.set_summary(ctx)
        elif self._context_label is not None:
            self._context_label.setText(_trf(
                "ai_assistant.context_summary",
                "Assets {assets} | analyzed {done} | music {music} | locked {locked}",
                assets=ctx.asset_count,
                done=ctx.analyzed_count,
                music=ctx.music_count,
                locked=ctx.locked_count,
            ))

        # 状态切换页
        if s.state == AssistantState.NO_PROJECT:
            # AI Assistant 本身是独立的对话入口，不应因为宿主还没有工程、
            # 时间线或素材就把 composer 隐藏。需要写入宿主时再由具体能力
            # 返回“请先创建工程/时间线”的明确提示即可。
            self._stack.setCurrentIndex(self._PAGE_MAIN)
            self._state_label.setText(
                tr("ai_assistant.state_no_project_chat", "未创建工程 · 可先对话")
            )
            self._state_label.setVisible(True)
            self.set_thinking(False)
            self._send_btn.setEnabled(True)
            self._request_edit.setReadOnly(False)
            self._render_variants(s)
            self._render_messages(s)
        elif s.state == AssistantState.NO_PROFILE:
            self._stack.setCurrentIndex(self._PAGE_NO_PROFILE)
            self._state_label.setVisible(True)
            self.set_thinking(False)
        elif s.state == AssistantState.AWAITING_BRIEF_APPROVAL:
            # M1.3：显示 Brief 编辑器页，等用户显式批准后才规划
            self._stack.setCurrentIndex(self._PAGE_BRIEF_EDITOR)
            self._state_label.setVisible(True)
            self.set_thinking(False)
        elif s.state == AssistantState.FAILED:
            self._stack.setCurrentIndex(self._PAGE_ERROR)
            self._error_label.setText(s.error_message or tr("ai_assistant.unknown_error", "未知错误"))
            self._state_label.setVisible(True)
            self.set_thinking(False)
        else:
            self._stack.setCurrentIndex(self._PAGE_MAIN)
            # 主页：根据状态显示文案
            state_txt = {
                # 空闲时由输入框表达状态；顶部的“就绪”会和活动流的
                # 完成/运行信息重复，并挤压会话标题。
                AssistantState.IDLE: "",
                AssistantState.GATHERING_CONTEXT: tr("ai_assistant.state_gathering", "收集上下文中…"),
                AssistantState.PLANNING: tr("ai_assistant.state_planning", "规划中…"),
                AssistantState.VALIDATING: tr("ai_assistant.state_validating", "校验中…"),
                AssistantState.APPLYING: tr("ai_assistant.state_applying", "应用中…"),
                AssistantState.PREVIEW_RENDERING: tr("ai_assistant.state_preview", "渲染预览中…"),
                AssistantState.AWAITING_BRIEF_APPROVAL: tr("ai_assistant.state_brief", "请确认 Brief"),
                AssistantState.AWAITING_VARIANT_APPROVAL: tr("ai_assistant.state_variant", "请选择方案"),
                AssistantState.AWAITING_APPLY_APPROVAL: tr("ai_assistant.state_apply", "请确认应用"),
                AssistantState.STALE: tr("ai_assistant.state_stale", "快照已过期，请刷新"),
                AssistantState.CANCELLED: tr("ai_assistant.state_cancelled", "已取消"),
            }.get(s.state, s.state.value)
            self._state_label.setText(state_txt)
            self._state_label.setVisible(bool(state_txt))
            self.set_thinking(s.state.is_busy)
            # busy 时输入框只读，但统一操作按钮仍保持可用以停止生成。
            self._send_btn.setEnabled(True)
            self._request_edit.setReadOnly(s.state.is_busy)
            # 渲染 variant 列表（M1.3 最小闭环）
            self._render_variants(s)
            # M5：渲染对话消息（snapshot.messages 之前从未渲染）
            self._render_messages(s)

    # ------------------------------------------------------------------ #
    # 防递归辅助
    # ------------------------------------------------------------------ #

    @property
    def is_updating(self) -> bool:
        """回填中（子组件据此跳过 signal）。"""
        return self._updating

    # ------------------------------------------------------------------ #
    # variant 列表渲染（M1.3 最小闭环）
    # ------------------------------------------------------------------ #

    def _update_content_stretch(self) -> None:
        """只给当前可见的内容区分配 stretch，保持 composer 贴底。

        Qt 的隐藏控件仍可能保留 QBoxLayout 的 stretch 权重。消息区/方案区
        直接 ``setVisible(False)`` 时若不清零它们的权重，空态页面会把空间
        分配给不可见滚动区，最终造成输入框和底部工具栏脱节。
        """
        layout = self._main_page_layout
        messages_index = layout.indexOf(self._messages_area)
        variants_index = layout.indexOf(self._variants_area)
        placeholder_index = layout.indexOf(self._placeholder)
        if messages_index >= 0:
            messages_visible = not self._messages_area.isHidden()
            self._messages_area.setMaximumHeight(
                _MAX_WIDGET_HEIGHT if messages_visible else 0
            )
            layout.setStretch(
                messages_index, 2 if messages_visible else 0
            )
        if variants_index >= 0:
            variants_visible = not self._variants_area.isHidden()
            self._variants_area.setMaximumHeight(
                _MAX_WIDGET_HEIGHT if variants_visible else 0
            )
            layout.setStretch(
                variants_index, 1 if variants_visible else 0
            )
        if placeholder_index >= 0:
            layout.setStretch(
                placeholder_index, 1 if not self._placeholder.isHidden() else 0
            )

    def _render_variants(self, s: AssistantStateSnapshot) -> None:
        """按 snapshot.variants 渲染最小 variant 卡片列表。

        每张卡：label + 时长/分数摘要 + "选择并预览"按钮（emit variant_selected）
        + "应用"按钮（emit apply_requested，仅在校验通过后可点）。
        无 variant 时显示 placeholder。
        """
        # 清空旧卡片（保留尾部 stretch）
        while self._variants_layout.count() > 1:
            item = self._variants_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        variants = s.variants
        # 对话消息优先：已有消息时不再显示“输入需求…”占位提示，否则该提示
        # 会作为 stretch 项吞掉消息区约一半高度（截图中的大面积空白来源）。
        self._placeholder.setVisible(not variants and not s.messages)
        self._variants_area.setVisible(bool(variants))
        self._update_content_stretch()
        if not variants:
            return

        can_apply = s.state in (
            AssistantState.AWAITING_APPLY_APPROVAL,
            AssistantState.AWAITING_VARIANT_APPROVAL,
            AssistantState.VARIANTS_READY,
            AssistantState.IDLE,
        )
        for v in variants:
            self._variants_layout.insertWidget(
                self._variants_layout.count() - 1, self._build_variant_card(v, can_apply)
            )

    def _build_variant_card(self, v, can_apply: bool) -> QWidget:
        """单张 variant 卡。"""
        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card.setStyleSheet("QFrame{background: palette(window); border: 1px solid palette(mid); border-radius: 4px;}")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        # AUD-19503：v.label 来自 planner/模型输出，转义后再进富文本模板，
        # 防止 <>& 被当作标记（显示错乱/标记注入）。
        title = QLabel(f"<b>{_html.escape(str(v.label))}</b>")
        title.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(title)

        # 摘要：时长 / hard constraint / beat coverage
        dur_s = f"{v.duration_frames} 帧"
        hc = tr("ai_assistant.hard_pass", "✓ 硬约束") if v.hard_constraints_passed else tr("ai_assistant.hard_fail", "✗ 硬约束")
        cov = f"beat {v.beat_coverage:.0%}" if v.beat_coverage else ""
        summary = QLabel(f"{dur_s} · {hc}" + (f" · {cov}" if cov else ""))
        summary.setStyleSheet("color: gray;")
        layout.addWidget(summary)

        # 状态标记
        status_txt = {
            "selected": tr("ai_assistant.vstatus_selected", "已选择"),
            "applied": tr("ai_assistant.vstatus_applied", "已应用"),
            "rejected": tr("ai_assistant.vstatus_rejected", "已拒绝"),
        }.get(v.status, "")
        if status_txt:
            st = QLabel(status_txt)
            st.setStyleSheet("font-style: italic; color: palette(link);")
            layout.addWidget(st)

        # 按钮行：选择（预览）/ 应用
        btn_row = QWidget()
        br = QVBoxLayout(btn_row)
        br.setContentsMargins(0, 0, 0, 0)
        br.setSpacing(4)
        select_btn = QPushButton(tr("ai_assistant.select_preview", "选择并预览"))
        select_btn.clicked.connect(lambda _checked=False, vid=v.variant_id: self.variant_selected.emit(vid))
        br.addWidget(select_btn)

        apply_btn = QPushButton(tr("ai_assistant.apply", "应用此方案"))
        apply_btn.setEnabled(can_apply and v.hard_constraints_passed)
        apply_btn.clicked.connect(lambda _checked=False, vid=v.variant_id: self.apply_requested.emit(vid))
        br.addWidget(apply_btn)
        layout.addWidget(btn_row)
        return card

    # ------------------------------------------------------------------ #
    # M5：消息渲染（snapshot.messages 之前从未渲染）
    # ------------------------------------------------------------------ #

    def _render_messages(self, s: AssistantStateSnapshot) -> None:
        """按 snapshot.messages 渲染对话历史卡（§2.2 对话区）。

        只重建 service 消息卡，不触碰分析提议、结果卡、skill 卡等动态内容。
        无消息且没有动态内容时隐藏消息区。
        每条：role 标记 + 文本 + 结构化引用摘要（brief/variant/decision/job/error）。
        """
        message_signature = tuple(
            (
                getattr(message, "message_id", ""),
                getattr(message, "role", ""),
                getattr(message, "text", "") or "",
                getattr(message, "error_code", None),
                # 附件影响卡片内容（缩略图解码），纳入签名避免跳过重建后
                # 展示过期图片。
                len(getattr(message, "attachments", ()) or ()),
            )
            for message in s.messages
        )
        messages_changed = message_signature != self._last_rendered_message_signature
        has_dynamic = self._has_dynamic_message_widgets()
        has_activity = self._has_visible_run_activity()
        # AUD-19501：service 状态回调会高频触发 refresh_state；消息未变时
        # 提前返回，不再整批 deleteLater + 重建（每张卡含 Markdown 重解析与
        # base64 附件重新解码），避免长会话/多图时的周期性 UI 卡顿与闪烁。
        if (
            not messages_changed
            and self._message_widgets
            and len(self._message_widgets) == len(s.messages)
        ):
            self._messages_area.setVisible(
                bool(s.messages) or has_dynamic or has_activity
                or self._streaming_label.isVisible()
            )
            self._placeholder.setVisible(
                not s.messages and not s.variants and not has_dynamic and not has_activity
            )
            self._update_content_stretch()
            return
        self._last_rendered_message_signature = message_signature
        # 动态卡片不是 snapshot.messages 的一部分，不能随状态刷新被删除。
        for widget in self._message_widgets:
            self._messages_layout.removeWidget(widget)
            widget.setParent(None)
            widget.deleteLater()
        self._message_widgets = []

        messages = s.messages
        self._messages_area.setVisible(
            bool(messages) or has_dynamic or has_activity or self._streaming_label.isVisible()
        )
        # 没有方案但已有对话时，消息区独占主内容区域；只有完全空闲时才显示
        # 居中的引导语。
        self._placeholder.setVisible(
            not messages and not s.variants and not has_dynamic and not has_activity
        )
        self._update_content_stretch()
        if not messages:
            return
        for m in messages:
            card = self._build_message_card(m)
            self._insert_message_flow_widget(card)
            self._message_widgets.append(card)
        # 新消息到达时跟随到最新内容。滚动必须延迟到布局完成之后，不能只在
        # 当前 maximum() 仍为 0 的那一帧设置一次，否则长回复会停在顶部。
        if messages_changed:
            self._schedule_messages_scroll_to_bottom()

    def _has_dynamic_message_widgets(self) -> bool:
        """判断消息流中是否还有非 service 消息的交互卡片。"""
        for index in range(self._messages_layout.count()):
            widget = self._messages_layout.itemAt(index).widget()
            if (
                widget is not None
                and widget is not self._streaming_label
                and widget is not getattr(self, "_run_section", None)
                and not widget.isHidden()
            ):
                return True
        return False

    def _has_visible_run_activity(self) -> bool:
        """Return whether the compact activity row currently has content."""
        run_section = getattr(self, "_run_section", None)
        run_drawer = getattr(self, "_run_drawer", None)
        return bool(
            run_section is not None
            and not run_section.isHidden()
            and run_drawer is not None
            and getattr(getattr(run_drawer, "state", None), "items", ())
        )

    def _insert_message_flow_widget(self, widget: QWidget) -> None:
        """按消息优先、动态卡片随后、stretch 最后的顺序插入内容。"""
        insert_at = self._messages_layout.count() - 1
        for index in range(self._messages_layout.count() - 1):
            existing = self._messages_layout.itemAt(index).widget()
            if existing is not None and existing not in self._message_widgets:
                insert_at = index
                break
        self._messages_layout.insertWidget(insert_at, widget)
        self._messages_area.setVisible(True)
        self._placeholder.setVisible(False)
        self._update_content_stretch()
        self._schedule_messages_scroll_to_bottom()

    def _show_component_error(self, component: str, exc: Exception) -> None:
        """Expose optional-card failures without breaking the conversation.

        These cards are assembled from independently shipped feature widgets.
        A broad catch at that boundary is intentional, but silently swallowing
        it made the assistant look successful while dropping a result.  Keep
        the exception in logs for diagnostics and add a small, safe error card
        to the message flow.  The user-facing text deliberately omits paths or
        provider details that could contain sensitive data.
        """
        logger.warning("AI assistant component %s failed", component,
                       exc_info=(type(exc), exc, exc.__traceback__))
        try:
            label = QLabel(
                f"⚠️ {component} 暂时无法显示，请重试。"
            )
            label.setObjectName("assistant_component_error")
            label.setWordWrap(True)
            label.setStyleSheet(
                "color: #a94442; padding: 6px; background: #fff3f3;"
            )
            self._insert_message_flow_widget(label)
        except Exception:  # pragma: no cover - last-resort Qt teardown path
            logger.debug("failed to render AI component error card",
                         exc_info=True)

    def _schedule_messages_scroll_to_bottom(self) -> None:
        """Scroll after the message layout has acquired its real range.

        A newly inserted card initially reports ``maximum() == 0``. Retry for
        a short bounded window so both normal messages and tall Markdown tables
        land at the newest content without stealing the UI thread.
        """
        if self._messages_scroll_pending:
            return
        self._messages_scroll_pending = True
        self._messages_scroll_retry = 0
        self._messages_scroll_timer.start(0)

    def _attempt_messages_scroll(self) -> None:
        """Apply the pending scroll and retry while Qt is still laying out."""
        try:
            bar = self._messages_area.verticalScrollBar()
            maximum = bar.maximum()
            bar.setValue(maximum)
        except RuntimeError:
            # The timer is parented to the panel, but a queued timeout can
            # still race a native dock teardown on some Qt platforms.
            self._messages_scroll_pending = False
            return
        if maximum == 0 and self._messages_scroll_retry < 12:
            self._messages_scroll_retry += 1
            self._messages_scroll_timer.start(16)
            return
        self._messages_scroll_pending = False

    def set_streaming_reply(self, text: str) -> None:
        """显示尚未落入会话历史的流式回复。"""
        if text:
            self.set_thinking(False)
        self._streaming_label.setText(text)
        self._streaming_label.setMaximumHeight(
            _MAX_WIDGET_HEIGHT if text else 0
        )
        self._streaming_label.setVisible(bool(text))
        if text:
            self._messages_area.setVisible(True)
            self._placeholder.setVisible(False)
            self._update_content_stretch()
            self._schedule_messages_scroll_to_bottom()

    def preserve_cancelled_streaming_reply(self, text: str) -> None:
        """保留用户停止生成时已经收到的部分回复。"""
        partial = str(text or "").strip()
        if not partial:
            self.clear_streaming_reply()
            return
        self.set_streaming_reply(
            partial + "\n\n⏹ "
            + tr("ai_assistant.stopped_generation", "已停止生成")
        )
        self._streaming_label.setStyleSheet(
            "color: #c8c8cd; background: transparent; padding: 4px 6px;"
            " border: none;"
        )

    def clear_streaming_reply(self) -> None:
        """清除流式暂存区（完成、失败或取消）。"""
        self._streaming_label.clear()
        self._streaming_label.setMaximumHeight(0)
        self._streaming_label.setVisible(False)
        if not self._state.messages and not self._has_dynamic_message_widgets():
            has_activity = self._has_visible_run_activity()
            self._messages_area.setVisible(has_activity)
            self._placeholder.setVisible(not self._state.variants and not has_activity)
            self._update_content_stretch()

    def _build_message_card(self, m) -> QWidget:
        """单条消息卡（§2.2 对话区：role + 文本 + 结构化引用摘要）。"""
        card = QFrame()
        # 消息是内容流，不再绘制用户/助手各自的彩色背景框；通过小号 role
        # 标签区分身份，保持面板更轻、更接近原生对话界面。
        card.setFrameShape(QFrame.Shape.NoFrame)
        card.setStyleSheet("QFrame { background: transparent; border: none; }")
        role = getattr(m, "role", "user")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        # role 标签
        role_label = {
            "user": tr("ai_assistant.role_user", "用户"),
            "assistant": tr("ai_assistant.role_assistant", "助手"),
            "system": tr("ai_assistant.role_system", "系统"),
        }.get(role, role)
        head = QLabel(f"<b>{role_label}</b>")
        head.setTextFormat(Qt.TextFormat.RichText)
        head.setStyleSheet("color: gray; font-size: 9px;")
        layout.addWidget(head)

        # 文本（错误消息用红字）
        text = getattr(m, "text", "") or ""
        err = getattr(m, "error_code", None)
        body = _AssistantMarkdownView()
        body.set_markdown_text(text)
        if err:
            body.setStyleSheet("color: #c0392b; font-size: 11px;")
        # 消息正文保持可选中，去掉右侧复制按钮，避免窄面板被额外控件挤压。
        layout.addWidget(body)

        # 多模态：图片附件缩略图（仅 user 消息会带图）。
        attachments = getattr(m, "attachments", ()) or ()
        if attachments:
            thumb_row = QHBoxLayout()
            thumb_row.setContentsMargins(0, 2, 0, 0)
            thumb_row.setSpacing(4)
            for att in attachments:
                thumb = QLabel()
                pm = QPixmap()
                pm.loadFromData(_decode_data_url_bytes(getattr(att, "data_url", "")))
                if not pm.isNull():
                    thumb.setPixmap(pm.scaledToHeight(72, Qt.TransformationMode.SmoothTransformation))
                else:
                    kind = str(getattr(att, "input_kind", "file") or "file")
                    icon = {"video": "🎬", "audio": "🎵", "file": "📄"}.get(kind, "📎")
                    thumb.setText(
                        f"{icon} {getattr(att, 'filename', tr('ai_assistant.attachment', '附件'))}"
                    )
                thumb_row.addWidget(thumb)
            thumb_row.addStretch(1)
            layout.addLayout(thumb_row)

        # 结构化引用摘要（至少一个非空 for assistant 消息；§11.2 契约）
        refs: list[str] = []
        for attr, label_key, label_default in (
            ("brief_patch_ref", "ai_assistant.ref_brief", "Brief"),
            ("analysis_ref", "ai_assistant.ref_analysis", "分析"),
            ("variant_id", "ai_assistant.ref_variant", "方案"),
            ("decision_id", "ai_assistant.ref_decision", "决策"),
            ("job_id", "ai_assistant.ref_job", "任务"),
        ):
            v = getattr(m, attr, None)
            if v:
                refs.append(f"{tr(label_key, label_default)}: {v}")
        if err:
            refs.append(f"{tr('ai_assistant.error_code', '错误')}: {err}")
        if refs:
            ref_label = QLabel(" · ".join(refs))
            ref_label.setStyleSheet("color: gray; font-size: 9px;")
            ref_label.setWordWrap(True)
            layout.addWidget(ref_label)
        return card

    # ------------------------------------------------------------------ #
    # M5：上下文检查器（§7.1 可检查/可预算/可排除）
    # ------------------------------------------------------------------ #

    def set_context_bundle(self, bundle: object) -> None:
        """host 推入 ContextBundle 供检查器展示（§7.1）。

        bundle 是 ContextBundle（context_bundle.py）；本面板只读展示，不抓数据。
        """
        self._context_bundle = bundle
        # 若当前在检查器页，立即刷新
        if self._stack.currentIndex() == self._PAGE_CONTEXT_INSPECTOR:
            self._render_context_bundle()

    def _on_inspect_context_clicked(self) -> None:
        """用户点"上下文"按钮 → 切到检查器页 + 通知 host 计算 bundle（§7.1）。"""
        self._render_context_bundle()
        self._stack.setCurrentIndex(self._PAGE_CONTEXT_INSPECTOR)
        # 通知 host：用户想看上下文 → host 计算 ContextBundle 后回填（set_context_bundle）
        self.context_inspector_requested.emit()

    def _render_context_bundle(self) -> None:
        """渲染 ContextBundle 每块的 kind/优先级/token/为何包含/可排除（§7.1）。"""
        bundle = getattr(self, "_context_bundle", None)
        # 清空旧块卡（保留尾部 stretch）
        while self._ctx_blocks_layout.count() > 1:
            item = self._ctx_blocks_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        if bundle is None:
            self._ctx_total_label.setText(
                tr("ai_assistant.context_not_built", "尚未构建上下文（发起一次请求后可见）")
            )
            return

        # 总量 / 预算（§7.2 UI 显示"当前 8.4k / 32k"）。
        # 注意：tr() 在 catalog 缺该 key 时不插值 default，故显式 f-string 拼接数字部分。
        total = getattr(bundle, "total_tokens", 0)
        budget = getattr(bundle, "budget", None)
        limit = getattr(budget, "input_limit", 0) if budget else 0
        omitted = getattr(bundle, "omitted_block_ids", ())
        needs_compress = getattr(bundle, "needs_compression", False)
        prefix = tr("ai_assistant.context_total_prefix", "当前")
        unit = tr("ai_assistant.context_total_unit", "tokens")
        total_txt = f"{prefix} {total} / {limit} {unit}"
        if needs_compress:
            total_txt += " · " + tr("ai_assistant.context_compress_hint", "已达压缩阈值")
        if omitted:
            total_txt += f" · {len(omitted)} " + tr("ai_assistant.context_omitted", "块因预算未纳入")
        self._ctx_total_label.setText(total_txt)

        # 各 block 卡
        for b in getattr(bundle, "blocks", ()):
            self._ctx_blocks_layout.insertWidget(
                self._ctx_blocks_layout.count() - 1,
                self._build_context_block_card(b),
            )

    def _build_context_block_card(self, b) -> QWidget:
        """单块上下文卡：kind/优先级/token/为何包含/可排除（§7.1）。"""
        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card.setStyleSheet(
            "QFrame{background: palette(window); "
            "border: 1px solid palette(mid); border-radius: 4px;}"
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        # kind + 优先级标签 + token
        # AUD-19503：kind 来自外部 ContextBundle，转义后再进富文本模板。
        kind = _html.escape(str(getattr(b, "kind", "?") or "?"))
        priority = int(getattr(b, "priority", 0) or 0)
        priority_label = {
            60: tr("ai_assistant.prio_hard", "硬约束"),
            50: tr("ai_assistant.prio_delivery", "交付目标"),
            40: tr("ai_assistant.prio_decisions", "已批准决策"),
            30: tr("ai_assistant.prio_evidence", "证据"),
            20: tr("ai_assistant.prio_summary", "摘要"),
            10: tr("ai_assistant.prio_history", "历史"),
        }.get(priority, str(priority))
        tokens = getattr(b, "token_estimate", 0)
        truncated = getattr(b, "truncated", False)
        trunc_mark = " · " + tr("ai_assistant.block_truncated", "已截断") if truncated else ""
        head = QLabel(
            f"<b>{kind}</b> · {_html.escape(str(priority_label))} · {tokens} tokens"
            f"{_html.escape(trunc_mark)}"
        )
        head.setTextFormat(Qt.TextFormat.RichText)
        head.setStyleSheet("font-size: 10px;")
        layout.addWidget(head)

        # 为何包含
        why = getattr(b, "why_included", "")
        if why:
            why_label = QLabel(why)
            why_label.setStyleSheet("color: gray; font-size: 10px;")
            why_label.setWordWrap(True)
            layout.addWidget(why_label)

        # 排除按钮（HARD_CONSTRAINT 级不允许排除：§7.1 硬约束/用户锁定最优先）
        btn_row = QWidget()
        br = QHBoxLayout(btn_row)
        br.setContentsMargins(0, 0, 0, 0)
        br.addStretch(1)
        block_id = getattr(b, "id", "")
        if priority < 60 and block_id:
            exclude_btn = QPushButton(tr("ai_assistant.exclude_block", "排除"))
            exclude_btn.setStyleSheet("padding: 2px; font-size: 10px;")
            exclude_btn.clicked.connect(
                lambda _checked=False, bid=block_id: self.context_block_excluded.emit(bid)
            )
            br.addWidget(exclude_btn)
        layout.addWidget(btn_row)
        return card

    # ------------------------------------------------------------------ #
    # request composer（M1.3）
    # ------------------------------------------------------------------ #

    _MAX_REQUEST_LEN = 2000  # 防止超长 prompt（真实 token 预算在 M5）

    def _on_send_or_cancel_clicked(self) -> None:
        """统一发送/停止按钮：空闲发送，生成中协作取消。"""
        if self._updating:
            return
        if self._slash_popup is not None and not self._slash_popup.isHidden():
            self._activate_slash_command(self._slash_popup.currentRow())
            return
        if self._media_popup is not None and not self._media_popup.isHidden():
            self._activate_media_candidate(self._media_popup.currentRow())
            return
        if self._state.state.is_busy:
            self.cancel_requested.emit()
            return
        self._on_send_clicked()

    def _set_send_button_busy(self, busy: bool) -> None:
        """切换方形按钮的箭头/方块图标，不改变其橙白项目配色。"""
        is_busy = bool(busy)
        self._send_btn.setProperty("assistant_busy", is_busy)
        self._send_btn.setText("■" if is_busy else "↑")
        tooltip = (
            tr("ai_assistant.stop_generation", "停止生成")
            if is_busy
            else tr("ai_assistant.send_tooltip", "发送")
        )
        self._send_btn.setToolTip(tooltip)
        self._send_btn.setAccessibleName(tooltip)

    def _on_send_clicked(self) -> None:
        """发送按钮：取输入文本，封装为 request 并 emit。"""
        if self._updating:
            return
        text = self._request_edit.toPlainText().strip()
        has_attachments = bool(
            self._pending_attachments or self._pending_target_asset_ids
        )
        if not text and not has_attachments:
            return
        if len(text) > self._MAX_REQUEST_LEN:
            text = text[: self._MAX_REQUEST_LEN]
        self.submit_request(text)
        # 发送后清空输入框
        self._request_edit.clear()

    def submit_request(self, text: str) -> None:
        """用户提交一条需求（封装为 AssistantRequest 后发 signal）。

        Controller 订阅 ``request_submitted`` 后调 service。携带待发送的图片附件。
        """
        if self._updating:
            return
        import uuid as _uuid

        from src.features.edit.ai_editing.assistant_events import AssistantRequest

        footer_state = getattr(self._composer_footer, "state", None)
        attachments = tuple(self._pending_attachments)
        target_asset_ids = tuple(self._pending_target_asset_ids)
        req = AssistantRequest(
            request_id=str(_uuid.uuid4()),
            text=text,
            profile_id=self._state.active_profile_id,
            brief_revision=self._state.context.snapshot_revision if hasattr(self._state.context, "snapshot_revision") else 0,
            mode=str(getattr(footer_state, "mode", "chat") or "chat"),
            attachments=attachments,
            target_asset_ids=target_asset_ids,
        )
        # 附件随本次请求发出，清空待发送区。
        self._clear_pending_attachments()
        self.request_submitted.emit(req)

    # ------------------------------------------------------------------ #
    # 多模态图片附件（粘贴 / 抓帧）
    # ------------------------------------------------------------------ #

    def add_image_attachment(self, qimage: QImage, *, source: str = "user") -> bool:
        """把一张 QImage 加入待发送附件（降采样 + JPEG 编码在后台线程完成）。

        AUD-19511: 编码不再阻塞 UI 线程 —— 本方法只做 null 校验并派发
        QThreadPool 任务；附件在编码完成后经队列信号回填待发送区与预览。
        返回是否受理（null 图同步拒绝）；编码失败记日志并丢弃该附件。

        供宿主在 ``frame_grab_requested`` 抓帧后回调，也供粘贴路径复用。
        """
        if qimage is None or qimage.isNull():
            return False
        self._spawn_image_attachment_worker(image=qimage, path="", source=source)
        return True

    def _spawn_image_attachment_worker(
        self, *, image: QImage | None, path: str, source: str
    ) -> None:
        """派发一个后台解码/编码任务（AUD-19511）。"""
        self._attachment_jobs_pending += 1
        worker = _ImageAttachmentWorker(
            image=image,
            path=path,
            source=source,
            signals=self._attachment_signals,
        )
        QThreadPool.globalInstance().start(worker)

    def _on_image_attachment_ready(
        self, attachment: object, source: str, ok: bool
    ) -> None:
        """后台编码完成回调（经队列信号回 UI 线程）。"""
        self._attachment_jobs_pending = max(0, self._attachment_jobs_pending - 1)
        if not ok or not isinstance(attachment, ImageAttachment):
            self._settle_image_batch(added=False)
            return
        self._pending_attachments.append(attachment)
        self._render_attachment_preview()
        self._settle_image_batch(added=True)

    def _settle_image_batch(self, *, added: bool) -> None:
        """多选批次回填计数；整批 0 成功时提示一次。"""
        batch = self._image_batch
        if batch is None:
            return
        batch["pending"] -= 1
        if added:
            batch["added"] += 1
        if batch["pending"] <= 0:
            self._image_batch = None
            if batch["added"] == 0:
                self.set_agent_notice(
                    tr(
                        "ai_assistant.add_image_failed",
                        "没有读取到可用的图片文件。",
                    ),
                    warning=True,
                )

    def _on_image_pasted(self, qimage: QImage) -> None:
        """粘贴图片（来自剪贴板）→ 加入待发送附件。"""
        self.add_image_attachment(qimage, source="paste")

    def _on_files_dropped(self, payload: object) -> None:
        """接收系统文件或媒体池素材的拖拽，并加入待发送附件。"""
        if isinstance(payload, dict):
            paths = tuple(str(path) for path in payload.get("paths", ()) if path)
            asset_ids = tuple(str(asset_id) for asset_id in payload.get("asset_ids", ()) if asset_id)
        else:
            paths = tuple(str(path) for path in (payload or ()) if path)
            asset_ids = ()

        added = 0
        rejected = 0
        seen_paths: set[str] = set()
        for path in paths:
            normalized = os.path.abspath(path)
            if normalized in seen_paths:
                continue
            seen_paths.add(normalized)
            if self._add_file_attachment(normalized, source="drag_drop"):
                added += 1
            else:
                rejected += 1

        if asset_ids:
            candidates = {
                candidate.get("asset_id", ""): candidate
                for candidate in self._media_pool_candidates()
            }
            for asset_id in asset_ids:
                candidate = candidates.get(asset_id)
                if candidate and candidate.get("media_type") == "motion_graphic":
                    self._add_motion_graphic_target(
                        asset_id,
                        str(candidate.get("display_name", "Motion Graphic Clip") or "Motion Graphic Clip"),
                    )
                    added += 1
                    continue
                path = candidate.get("path", "") if candidate else ""
                normalized = os.path.abspath(path) if path else ""
                if normalized and normalized in seen_paths:
                    continue
                if normalized:
                    seen_paths.add(normalized)
                if self._add_file_attachment(path, source="media_pool"):
                    added += 1
                else:
                    rejected += 1

        if rejected:
            self.set_agent_notice(
                f"有 {rejected} 个拖入文件无法添加，请检查格式、大小和读取权限。",
                warning=True,
            )
        if added:
            self._request_edit.setFocus()

    def _clear_pending_attachments(self) -> None:
        self._pending_attachments.clear()
        self._pending_target_asset_ids.clear()
        self._pending_target_labels.clear()
        self._render_attachment_preview()

    def _add_motion_graphic_target(self, asset_id: str, display_name: str) -> bool:
        """Add one explicit virtual Motion Graphic target to the next request."""
        asset_id = str(asset_id or "").strip()
        if not asset_id:
            return False
        if asset_id not in self._pending_target_asset_ids:
            self._pending_target_asset_ids.append(asset_id)
        self._pending_target_labels[asset_id] = (
            str(display_name or "Motion Graphic Clip").strip() or "Motion Graphic Clip"
        )
        self._render_attachment_preview()
        return True

    def _remove_motion_graphic_target(self, asset_id: str) -> None:
        asset_id = str(asset_id or "").strip()
        if asset_id in self._pending_target_asset_ids:
            self._pending_target_asset_ids.remove(asset_id)
        self._pending_target_labels.pop(asset_id, None)
        self._render_attachment_preview()

    def pending_target_asset_ids(self) -> tuple[str, ...]:
        """Return explicit media-pool targets waiting to be sent."""
        return tuple(self._pending_target_asset_ids)

    def pending_attachment_kinds(self) -> tuple[str, ...]:
        """供 controller 在切换模型时即时检查待发送附件能力。"""
        return tuple(["motion_graphic"] * len(self._pending_target_asset_ids)) + tuple(
            "image" if getattr(att, "data_url", "") else str(
                getattr(att, "input_kind", "file") or "file"
            )
            for att in self._pending_attachments
        )

    def _render_attachment_preview(self) -> None:
        """重建待发送图片的缩略图预览条。"""
        layout = self._attachment_preview_layout
        # 清空旧 widget（保留末尾的 stretch）
        while layout.count() > 1:
            item = layout.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.deleteLater()
        attachments = self._pending_attachments
        target_ids = self._pending_target_asset_ids
        self._attachment_preview_row.setVisible(bool(attachments or target_ids))
        for asset_id in target_ids:
            chip = QFrame()
            chip_lay = QHBoxLayout(chip)
            chip_lay.setContentsMargins(0, 0, 0, 0)
            chip_lay.setSpacing(2)
            label = str(self._pending_target_labels.get(asset_id, "Motion Graphic Clip"))
            target_label = QLabel(f"🎞 {label[:18]}")
            target_label.setToolTip(f"目标 Motion Graphic Clip\n{asset_id}")
            chip_lay.addWidget(target_label)
            remove_btn = QToolButton()
            remove_btn.setText("×")
            remove_btn.setFixedSize(18, 18)
            remove_btn.setStyleSheet("font-size: 12px;")
            remove_btn.clicked.connect(
                lambda _checked=False, aid=asset_id: self._remove_motion_graphic_target(aid)
            )
            chip_lay.addWidget(remove_btn)
            layout.insertWidget(layout.count() - 1, chip)
        for idx, att in enumerate(attachments):
            chip = QFrame()
            chip_lay = QHBoxLayout(chip)
            chip_lay.setContentsMargins(0, 0, 0, 0)
            chip_lay.setSpacing(2)
            thumb = QLabel()
            data_url = getattr(att, "data_url", "")
            if data_url:
                pm = QPixmap()
                pm.loadFromData(_decode_data_url_bytes(data_url))
                if not pm.isNull():
                    thumb.setPixmap(pm.scaledToHeight(36, Qt.TransformationMode.SmoothTransformation))
                else:
                    thumb.setText("🖼")
            else:
                kind = str(getattr(att, "input_kind", "file") or "file")
                icon = {"video": "🎬", "audio": "🎵", "file": "📄"}.get(kind, "📎")
                attachment_fallback = tr("ai_assistant.attachment", "附件")
                name = str(getattr(att, "filename", attachment_fallback) or attachment_fallback)
                thumb.setText(f"{icon} {name[:18]}")
                thumb.setToolTip(name)
            chip_lay.addWidget(thumb)
            remove_btn = QToolButton()
            remove_btn.setText("×")
            remove_btn.setFixedSize(18, 18)
            remove_btn.setStyleSheet("font-size: 12px;")
            remove_btn.clicked.connect(
                lambda _checked=False, i=idx: self._remove_pending_attachment(i)
            )
            chip_lay.addWidget(remove_btn)
            layout.insertWidget(layout.count() - 1, chip)

    def _remove_pending_attachment(self, index: int) -> None:
        if 0 <= index < len(self._pending_attachments):
            del self._pending_attachments[index]
            self._render_attachment_preview()

    def _on_composer_add_action(self, action_id: str) -> None:
        """处理底部 ``+`` 菜单动作。

        文件选择和剪贴板读取都在 UI 层完成；真正的模型请求仍由现有
        ``request_submitted`` → controller 链路处理。这样菜单可以复用当前
        已经验证过的图片附件协议，不会引入一个无法发送的“假文件附件”。
        """
        action = str(action_id or "")
        if action == "image_files":
            added_paths, _ = QFileDialog.getOpenFileNames(
                self,
                tr("ai_assistant.add_image_files_dialog", "选择要添加的图片"),
                "",
                tr(
                    "ai_assistant.image_file_filter",
                    "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.gif)",
                ),
            )
            if added_paths:
                # AUD-19511: QImage(path) 整图解码 + JPEG 编码移到后台线程，
                # 多选大图不再在 UI 线程逐张同步解码；整批 0 成功时在回调
                # 里提示一次（_settle_image_batch）。
                self._image_batch = {"pending": len(added_paths), "added": 0}
                for path in added_paths:
                    self._spawn_image_attachment_worker(
                        image=None, path=path, source="file"
                    )
            return

        if action == "file_attachments":
            paths, _ = QFileDialog.getOpenFileNames(
                self,
                tr("ai_assistant.add_multimodal_files_dialog", "选择要添加的文件"),
                "",
                tr("ai_assistant.multimodal_file_filter", _SUPPORTED_FILE_FILTER),
            )
            added = 0
            rejected = 0
            for path in paths:
                if self._add_file_attachment(path):
                    added += 1
                else:
                    rejected += 1
            if rejected:
                notice = tr(
                    "ai_assistant.add_file_failed",
                    "有 {count} 个文件无法添加，请检查格式、大小和读取权限。",
                    count=rejected,
                )
                self.set_agent_notice(notice.format(count=rejected), warning=True)
            return

        if action == "clipboard_image":
            image = QApplication.clipboard().image()
            if image.isNull() or not self.add_image_attachment(
                image, source="clipboard"
            ):
                self.set_agent_notice(
                    tr(
                        "ai_assistant.clipboard_image_missing",
                        "剪贴板中没有可用的图片。",
                    ),
                    warning=True,
                )
            return

        if action == "current_frame":
            self.frame_grab_requested.emit()
        elif action == "scope":
            self.composer_scope_requested.emit()
        elif action == "context":
            self._on_inspect_context_clicked()
        elif action == "compress":
            self.context_compress_requested.emit()
        elif action == "new_session":
            self._on_new_session()
        elif action == "history":
            self._on_show_history()
        elif action == "models":
            self.configure_models_requested.emit()

    def _add_file_attachment(self, path: str, *, source: str = "file") -> bool:
        """把文件选择转换为图片附件或 File API 附件。"""
        try:
            if not path or not os.path.isfile(path):
                return False
            size = os.path.getsize(path)
            if size <= 0 or size > _MAX_FILE_ATTACHMENT_BYTES:
                return False
            mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if mime.startswith("image/"):
                # AUD-19511: QImage(path) 整图解码 + JPEG 编码移到后台线程；
                # 受理即返回 True，解码/编码失败在回调里记日志并丢弃。
                self._spawn_image_attachment_worker(
                    image=None, path=path, source=source
                )
                return True
            kind = "video" if mime.startswith("video/") else (
                "audio" if mime.startswith("audio/") else "file"
            )
            self._pending_attachments.append(FileAttachment(
                path=os.path.abspath(path),
                mime=mime,
                filename=os.path.basename(path),
                size_bytes=size,
                source=source,
                media_kind=kind,
            ))
            self._render_attachment_preview()
            return True
        except (OSError, ValueError):
            return False

    # ------------------------------------------------------------------ #
    # plan §10.2 P0.3 / P0.6：Composer footer + Run Drawer slots
    # ------------------------------------------------------------------ #
    # 这些 slot 只把 footer/drawer 的意图转发为面板级 signal；具体执行由
    # controller 订阅后做（面板不在 slot 里跑模型 / 写 timeline）。

    def _on_composer_scope(self) -> None:
        self.composer_scope_requested.emit()

    def _on_composer_permission(self, perm: str) -> None:
        if self._updating:
            return
        self.composer_permission_changed.emit(perm)

    def _on_composer_profile(self) -> None:
        self.composer_profile_requested.emit()

    def _on_context_compress(self) -> None:
        self.context_compress_requested.emit()

    def _on_new_session(self) -> None:
        self.new_session_requested.emit()

    def _on_show_history(self) -> None:
        self.show_history_requested.emit()

    def _on_run_cancel(self, item_id: str) -> None:
        self.run_cancel_requested.emit(item_id)

    def _on_run_retry(self, item_id: str) -> None:
        self.run_retry_requested.emit(item_id)

    def _on_run_diagnostics(self, item_id: str) -> None:
        self.run_diagnostics_requested.emit(item_id)

    # ------------------------------------------------------------------ #
    # plan §10.2 P0.3 / P0.6：composer/drawer 状态注入（由 controller 调）
    # ------------------------------------------------------------------ #

    def set_composer_footer(self, footer_state: object) -> None:
        """把底部控制栏状态（``ComposerFooterState``）灌进 footer。

        footer 在装配失败（``self._composer_footer is None``）时静默跳过，
        便于渐进接入。
        """
        if self._composer_footer is None:
            return
        self._updating = True
        try:
            profile_label = str(getattr(footer_state, "profile_label", "") or "")
            if profile_label:
                self._active_profile_display_name = profile_label
            self._composer_footer.set_state(footer_state)  # type: ignore[arg-type]
            # 顶部摘要使用用户配置的十进制 token 单位：200,000 → 200k。
            used = max(0, int(getattr(footer_state, "context_used", 0))) / 1000.0
            limit = max(1, int(getattr(footer_state, "context_limit", 200_000))) / 1000.0
            budget_text = tr(
                "ai_assistant.context_budget",
                "{used:.0f}k / {limit:.0f}k",
            )
            self._context_budget_label.setText(
                budget_text.format(used=used, limit=limit)
                + (
                    " · " + tr("ai_composer.compressed", "已压缩")
                    if bool(getattr(footer_state, "context_compressed", False))
                    else ""
                )
            )
            used_tokens = max(0, int(getattr(footer_state, "context_used", 0)))
            limit_tokens = max(1, int(getattr(footer_state, "context_limit", 200_000)))
            if used_tokens >= limit_tokens:
                self._context_budget_label.setStyleSheet(
                    "color: #e74c3c; font-weight: 600; font-size: 10px; padding: 1px 4px;"
                )
            elif used_tokens >= int(limit_tokens * 0.8):
                self._context_budget_label.setStyleSheet(
                    "color: #f39c12; font-size: 10px; padding: 1px 4px;"
                )
            else:
                self._context_budget_label.setStyleSheet(
                    "color: gray; font-size: 10px; padding: 1px 4px;"
                )
        finally:
            self._updating = False

    def set_run_drawer(self, drawer_state: object) -> None:
        """把运行活动流状态（``RunDrawerState``）灌进 Run Drawer。"""
        if self._run_drawer is None:
            return
        self._updating = True
        try:
            self._run_drawer.set_state(drawer_state)  # type: ignore[arg-type]
            if self._run_section is not None:
                items = list(getattr(drawer_state, "items", ()) or ())
                has_items = bool(items)
                active_item = next(
                    (
                        item for item in reversed(items)
                        if getattr(item, "status", "") in (
                            "running", "queued", "waiting_input",
                        )
                    ),
                    None,
                )
                if active_item is not None:
                    item_id = str(getattr(active_item, "item_id", ""))
                    if item_id != self._activity_item_id:
                        self._activity_item_id = item_id
                        self._activity_started_at = time.monotonic()
                    self._refresh_activity_status()
                    # The fixed composer line is the single live status
                    # surface while a run exists; keep the compact header
                    # free for the conversation title and context budget.
                    self._state_label.setVisible(False)
                    self._thinking_indicator.setVisible(False)
                    if not self._activity_timer.isActive():
                        self._activity_timer.start()
                elif has_items:
                    # Keep the terminal result visible long enough for the
                    # user to understand that the request finished, rather
                    # than leaving the header at a stale “thinking” label.
                    self._activity_timer.stop()
                    self._activity_item_id = ""
                    self._refresh_activity_status()
                    self._state_label.setVisible(False)
                    self._thinking_indicator.setVisible(False)
                else:
                    self._activity_timer.stop()
                    self._activity_item_id = ""
                    self.clear_activity_status()
                    if self._state.state.is_busy:
                        self.set_thinking(True)
                    elif not self._state_label.text():
                        self._state_label.setVisible(False)
                self._run_section.setMaximumHeight(
                    180 if has_items else 0
                )
                self._run_section.setVisible(has_items)
                # The activity row lives inside the conversation scroll area.
                # Keep it visible even when the current snapshot has no
                # service messages (for example while a tool starts).
                has_messages = bool(getattr(self._state, "messages", ()))
                has_dynamic = self._has_dynamic_message_widgets()
                has_activity = self._has_visible_run_activity()
                self._messages_area.setVisible(
                    has_messages or has_dynamic or has_activity
                    or self._streaming_label.isVisible()
                )
                self._placeholder.setVisible(
                    not has_messages and not has_dynamic and not has_activity
                    and not getattr(self._state, "variants", ())
                )
                self._update_content_stretch()
                if has_activity:
                    # AUD-19504：复用带 RuntimeError 防护的定时器路径
                    # （_attempt_messages_scroll 已捕获"排队超时与原生 dock
                    # 拆除竞态"），不再用裸 QTimer.singleShot lambda 访问
                    # verticalScrollBar()。
                    self._schedule_messages_scroll_to_bottom()
        finally:
            self._updating = False

    def _refresh_activity_status(self) -> None:
        """Keep the current AI phase and elapsed wait visible in the header."""
        drawer = getattr(self, "_run_drawer", None)
        state = getattr(drawer, "state", None)
        items = list(getattr(state, "items", ()) or ()) if state is not None else []
        if not items:
            self._activity_timer.stop()
            self.clear_activity_status()
            return
        active_statuses = ("running", "queued", "waiting_input")
        current = next(
            (item for item in reversed(items) if getattr(item, "status", "") in active_statuses),
            None,
        )
        if current is None:
            # A completed artifact/tool node may be appended after the main
            # run.  Prefer the main run for the terminal summary, otherwise
            # the footer could say “产出已完成” while the run itself failed.
            current = next(
                (
                    item for item in reversed(items)
                    if getattr(item, "kind", "") == "run"
                    and getattr(item, "status", "") in (
                        "succeeded", "failed", "cancelled", "skipped",
                    )
                ),
                next(
                    (
                        item for item in reversed(items)
                        if getattr(item, "status", "") in (
                            "succeeded", "failed", "cancelled", "skipped",
                        )
                    ),
                    items[-1],
                ),
            )
        status = str(getattr(current, "status", "") or "running")
        label = str(
            getattr(current, "label", "")
            or tr("ai_assistant.activity_task", "AI 任务")
        )
        detail = str(getattr(current, "output_summary", "") or "").strip()
        if status in ("running", "queued", "waiting_input"):
            elapsed = max(0, int(time.monotonic() - self._activity_started_at))
            if elapsed >= 30 and status == "waiting_input":
                text = _trf(
                    "ai_assistant.activity_waiting",
                    "{label} · waiting for {seconds}s; the network or model may be busy",
                    label=label,
                    seconds=elapsed,
                )
            else:
                text = _trf(
                    "ai_assistant.activity_elapsed",
                    "{label} · {seconds}s elapsed",
                    label=label,
                    seconds=elapsed,
                )
            self.set_activity_status(
                text, detail=detail, active=True, status=status,
            )
            self._state_label.setStyleSheet(
                "QLabel#assistant_status { color: #e87519; padding: 1px 0; font-size: 10px; }"
            )
        else:
            icon = {
                "succeeded": "✓",
                "failed": "×",
                "cancelled": "■",
                "skipped": "–",
            }.get(status, "•")
            text = f"{icon} {label}"
            if detail and detail != label:
                text += f" · {detail}"
            self.set_activity_status(
                label, detail=detail, active=False, status=status,
            )
            self._state_label.setStyleSheet(
                "QLabel#assistant_status { color: #9ea0a8; padding: 1px 0; font-size: 10px; }"
            )
        self._state_label.setText(text)
        self._state_label.setToolTip(detail or label)
        self._state_label.setVisible(True)
        # The header capsule is now the persistent status surface; the
        # separate animated indicator would duplicate the same information.
        self._thinking_indicator.setVisible(False)

    def set_plugin_status(self, views: object) -> None:
        """Render a compact, read-only plugin health summary from view models."""
        strip = getattr(self, "_plugin_status_strip", None)
        label = getattr(self, "_plugin_status_label", None)
        if strip is None or label is None:
            return
        rows = tuple(views or ())
        if not rows:
            strip.setVisible(False)
            label.clear()
            return
        active = sum(getattr(row, "runnable", False) for row in rows)
        attention = sum(
            getattr(row, "status_level", "ok") in {"warning", "error"}
            for row in rows
        )
        unverified = sum(bool(getattr(row, "unverified", False)) for row in rows)
        label.setText(_trf(
            "ai_assistant.plugin_status",
            "Plugins {total} · available {active} · attention {attention}"
            "{unverified_suffix}",
            total=len(rows),
            active=active,
            attention=attention,
            unverified_suffix=(
                " · unverified " + str(unverified) if unverified else ""
            ),
        ))
        tooltip_rows: list[str] = []
        for row in rows:
            diagnostics = getattr(row, "diagnostics", ())
            reason = "; ".join(
                str(getattr(item, "message", "")) for item in diagnostics
                if str(getattr(item, "message", ""))
            )
            line = (
                f"{getattr(row, 'display_name', getattr(row, 'plugin_id', 'plugin'))} "
                f"v{getattr(row, 'version', '')} · {getattr(row, 'state', '')}"
            )
            tooltip_rows.append(f"{line} · {reason}" if reason else line)
        label.setToolTip("\n".join(tooltip_rows))
        strip.setVisible(True)

    # ------------------------------------------------------------------ #
    # P1.1：Skill 驱动 Agent 渲染入口（help / clarify / skill 推荐卡）
    # ------------------------------------------------------------------ #

    def set_help_answer(self, answer: object) -> None:
        """把 HelpAnswer 渲染成 AiHelpAnswerCard，插进消息流（P1.1）。

        answer 为 None / 空 items 时静默跳过（不崩）。卡片插在 messages_layout
        尾部 stretch 之前（作为对话流的一部分）。
        """
        if answer is None:
            return
        self._updating = True
        try:
            from src.features.edit.ui.ai_help_answer import AiHelpAnswerCard
            card = AiHelpAnswerCard()
            card.set_answer(answer)  # type: ignore[arg-type]
            # E13: 卡片的“引用定位/打开页面”转发为面板级信号（控制器订阅）
            card.navigate_requested.connect(self.help_navigate_requested.emit)
            card.open_panel_requested.connect(self.help_open_panel_requested.emit)
            self._insert_message_flow_widget(card)
        except Exception as exc:
            self._show_component_error("帮助答案", exc)
        finally:
            self._updating = False

    def set_clarification(self, question: str, slot_name: str) -> None:
        """渲染一个澄清问题（最小可用：QLabel 显示问题文本，P1.1）。

        用户用现有 composer 输入框回答（不新建带输入的卡，避免本批引入新 widget）。
        问题插在 messages_layout 尾部 stretch 之前。
        """
        if not question:
            return
        self._updating = True
        try:
            from PyQt6.QtWidgets import QLabel
            label = QLabel(f"💬 {question}")
            label.setWordWrap(True)
            label.setStyleSheet("color: #8e44ad; padding: 6px; background: #fdf2f8;")
            self._insert_message_flow_widget(label)
        except Exception as exc:
            self._show_component_error("澄清提示", exc)
        finally:
            self._updating = False

    def set_agent_notice(self, message: str, *, warning: bool = False) -> None:
        """在对话流中显示宿主执行结果（如导出预检/启动反馈）。"""
        if not message:
            return
        self._updating = True
        try:
            from PyQt6.QtWidgets import QLabel

            label = QLabel(("⚠️ " if warning else "ℹ️ ") + message)
            label.setWordWrap(True)
            color = "#a94442" if warning else "#2c3e50"
            background = "#fff3f3" if warning else "#f2f7fb"
            label.setStyleSheet(
                f"color: {color}; padding: 6px; background: {background};"
            )
            self._insert_message_flow_widget(label)
        except Exception as exc:
            self._show_component_error("宿主提示", exc)
        finally:
            self._updating = False

    def set_analysis_proposal(self, request: object) -> None:
        """渲染 Agent 生成的分析提议，不在 UI 层直接调用 provider。"""
        request_id = str(getattr(request, "request_id", "") or "")
        if not request_id:
            return
        self._updating = True
        try:
            from PyQt6.QtWidgets import QFrame, QLabel, QPushButton

            card = QFrame(self)
            card.setFrameShape(QFrame.Shape.StyledPanel)
            card.setStyleSheet(
                "QFrame { background: #eef6ff; border: 1px solid #8bbcf0; "
                "border-radius: 6px; padding: 6px; }"
            )
            layout = QVBoxLayout(card)
            recipe = getattr(getattr(request, "recipe_id", None), "value", "")
            asset_ids = tuple(getattr(request, "asset_ids", ()) or ())
            title = getattr(request, "title", "") or tr(
                "ai_assistant.analysis_title", "AI 素材分析"
            )
            label = QLabel(
                _trf(
                    "ai_assistant.analysis_summary",
                    "{title}\nScope: {count} assets · recipe: {recipe}",
                    title=title,
                    count=len(asset_ids),
                    recipe=recipe or "local",
                )
            )
            label.setWordWrap(True)
            layout.addWidget(label)
            actions = QHBoxLayout()
            confirm = QPushButton(tr("ai_assistant.confirm", "确认执行"))
            cancel = QPushButton(tr("ai_assistant.cancel", "取消"))
            confirm.clicked.connect(
                lambda _checked=False, rid=request_id:
                self.analysis_confirm_requested.emit(rid)
            )
            cancel.clicked.connect(
                lambda _checked=False, rid=request_id:
                self.analysis_cancel_requested.emit(rid)
            )
            actions.addWidget(confirm)
            actions.addWidget(cancel)
            layout.addLayout(actions)
            self._insert_message_flow_widget(card)
            self._analysis_proposals[request_id] = card
        finally:
            self._updating = False

    def set_analysis_progress(self, request_id: str, message: str) -> None:
        """更新分析提议卡状态。"""
        card = self._analysis_proposals.get(request_id)
        if card is None:
            return
        label = card.findChild(QLabel)
        if label is not None:
            label.setText(message)
        # 确认后不允许重复入队；取消只对尚未确认的 proposal 生效。
        for button in card.findChildren(QPushButton):
            button.setEnabled(False)

    def clear_analysis_proposal(self, request_id: str) -> None:
        """移除已取消或已结束的分析提议卡。"""
        card = self._analysis_proposals.pop(request_id, None)
        if card is not None:
            card.deleteLater()

    def set_motion_graphic_proposal(
        self, request_id: str, title: str, details: str, *,
        show_confirm: bool = True,
    ) -> None:
        """Show a Motion Graphic plan and optionally wait for button approval."""
        if not request_id:
            return
        self.clear_motion_graphic_proposal(request_id)
        self._updating = True
        try:
            card = QFrame(self)
            card.setFrameShape(QFrame.Shape.StyledPanel)
            card.setStyleSheet(
                "QFrame { background: transparent; border: 1px solid #555861; "
                "border-radius: 6px; }"
            )
            layout = QVBoxLayout(card)
            layout.setContentsMargins(10, 8, 10, 8)
            layout.setSpacing(5)

            # AUD-19503：title 为模型/宿主生成的外部文本，转义后拼接显示。
            heading = QLabel(_trf(
                "ai_assistant.motion_plan_heading",
                "Plan · {title}",
                title=_html.escape(str(title or "Motion Graphic Clip")),
            ))
            heading.setTextFormat(Qt.TextFormat.PlainText)
            heading.setStyleSheet("font-weight: 600; color: #e5e7eb;")
            layout.addWidget(heading)

            body = QLabel(details)
            body.setTextFormat(Qt.TextFormat.PlainText)
            body.setWordWrap(True)
            body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            body.setStyleSheet("color: #b6b7bd;")
            layout.addWidget(body)

            actions = QHBoxLayout()
            actions.addStretch(1)
            cancel = QPushButton(tr("ai_assistant.cancel", "取消"))
            cancel.clicked.connect(
                lambda _checked=False, rid=request_id:
                self.motion_graphic_cancel_requested.emit(rid)
            )
            actions.addWidget(cancel)
            if show_confirm:
                confirm = QPushButton(tr("ai_assistant.confirm", "确认执行"))
                confirm.setObjectName("assistant_motion_confirm")
                confirm.setStyleSheet(
                    "QPushButton { background: #e87519; color: white; border: none; "
                    "border-radius: 4px; padding: 5px 14px; }"
                    "QPushButton:hover { background: #f28a2e; }"
                )
                confirm.clicked.connect(
                    lambda _checked=False, rid=request_id:
                    self.motion_graphic_confirm_requested.emit(rid)
                )
                actions.addWidget(confirm)
            layout.addLayout(actions)

            self._messages_area.setVisible(True)
            self._placeholder.setVisible(False)
            self._insert_message_flow_widget(card)
            self._motion_graphic_proposals[request_id] = card
            self._update_content_stretch()
        finally:
            self._updating = False

    def clear_motion_graphic_proposal(self, request_id: str) -> None:
        """Remove a pending Motion Graphic plan card."""
        card = self._motion_graphic_proposals.pop(request_id, None)
        if card is None:
            return
        card.deleteLater()

    def _request_motion_graphic_template(self, request_id: str) -> None:
        """AUD-19500：把"改用内置模板"意图发给专用信号。

        宿主已连接 ``motion_graphic_template_requested`` 时按模板路径处理
        （在 plan 上置 ``use_template`` 后再确认）；尚未连接的宿主回退为
        普通确认信号，按钮不会变成静默空操作。
        """
        try:
            has_subscriber = self.receivers(self.motion_graphic_template_requested) > 0
        except (AttributeError, RuntimeError, TypeError):  # pragma: no cover
            has_subscriber = False
        if has_subscriber:
            self.motion_graphic_template_requested.emit(request_id)
        else:
            self.motion_graphic_confirm_requested.emit(request_id)

    def set_motion_graphic_proposal_card(
        self, request_id: str, card_data: dict,
    ) -> None:
        """MG-02: show a typed proposal card with structured regions.

        ``card_data`` is a dict produced by
        ``MotionGraphicProposalCard.to_dict()`` — the Qt-free view model.
        Renders source label, duration/canvas, element list, beats, risks,
        quality warnings and action buttons.
        """
        if not request_id:
            return
        self.clear_motion_graphic_proposal(request_id)
        self._updating = True
        try:
            widget = QFrame(self)
            widget.setFrameShape(QFrame.Shape.StyledPanel)
            widget.setStyleSheet(
                "QFrame { background: transparent; border: 1px solid #555861; "
                "border-radius: 6px; }"
            )
            layout = QVBoxLayout(widget)
            layout.setContentsMargins(10, 8, 10, 8)
            layout.setSpacing(4)

            # AUD-19503：title/summary 来自模型输出，先做 HTML 转义再进
            # 富文本模板（QLabel AutoText 会把 <>& 当标记解析）。
            title = str(card_data.get("title", "Motion Graphic") or "Motion Graphic")
            source = str(card_data.get("source_label", "llm") or "llm")
            is_fallback = bool(card_data.get("is_fallback", False))
            heading_text = f"{'⚠ ' if is_fallback else '🎬 '}{_html.escape(title)}"
            if is_fallback:
                heading_text += _trf(
                    "ai_assistant.motion_source",
                    "  ·  Source: {source}",
                    source=_html.escape(source),
                )
            heading = QLabel(heading_text)
            heading.setTextFormat(Qt.TextFormat.RichText)
            heading.setStyleSheet(
                "font-weight: 600; color: #f59e0b;" if is_fallback
                else "font-weight: 600; color: #e5e7eb;"
            )
            layout.addWidget(heading)

            summary = card_data.get("summary", "")
            if summary:
                lbl = QLabel(_html.escape(str(summary)))
                lbl.setTextFormat(Qt.TextFormat.PlainText)
                lbl.setWordWrap(True)
                lbl.setStyleSheet("color: #b6b7bd;")
                layout.addWidget(lbl)

            # Format row: duration + canvas + insert position.
            # AUD-19502：duration/canvas_size 为外部 dict 字段，解包与
            # 数值化前先做类型/长度校验，畸形数据不再抛异常逃逸到 Qt 槽。
            try:
                duration_value = float(card_data.get("duration_seconds", 0) or 0)
            except (TypeError, ValueError):
                duration_value = 0.0
            canvas = card_data.get("canvas_size", [1920, 1080])
            try:
                canvas_w, canvas_h = int(canvas[0]), int(canvas[1])
            except (TypeError, ValueError, IndexError):
                canvas_w, canvas_h = 1920, 1080
            insert_frame = card_data.get("target_insert_frame")
            track = str(card_data.get("target_track_label", "Graphics") or "Graphics")
            fmt_parts: list[str] = []
            if duration_value > 0:
                fmt_parts.append(_trf(
                    "ai_assistant.motion_duration",
                    "Duration {duration:g}s",
                    duration=duration_value,
                ))
            fmt_parts.append(_trf(
                "ai_assistant.motion_canvas",
                "Canvas {width}×{height}",
                width=canvas_w,
                height=canvas_h,
            ))
            page_count = card_data.get("page_count")
            if page_count:
                fmt_parts.append(_trf(
                    "ai_assistant.motion_pages_verified",
                    "{count} pages verified",
                    count=int(page_count),
                ))
            if insert_frame is not None:
                fmt_parts.append(_trf(
                    "ai_assistant.motion_insert",
                    "Insert {track}@{frame}",
                    track=track,
                    frame=insert_frame,
                ))
            if fmt_parts:
                layout.addWidget(QLabel("  ·  ".join(fmt_parts)))

            validation = card_data.get("render_validation")
            if isinstance(validation, dict) and validation.get("detail"):
                validation_label = QLabel(f"✓ {validation['detail']}")
                validation_label.setStyleSheet("color: #8ccf92; font-size: 11px;")
                validation_label.setWordWrap(True)
                layout.addWidget(validation_label)

            # HTML Motion Graphic builds additionally carry a deterministic,
            # non-subjective completeness report.  Show a compact success
            # signal as well as warnings so users can see that the deck was
            # checked for information density, visual language and narrative
            # variation before they confirm it.
            html_design_quality = card_data.get("html_design_quality")
            if isinstance(html_design_quality, dict):
                try:
                    overall = float(html_design_quality["overall_score"])
                    visual = float(html_design_quality["visual_language_score"])
                    diversity = float(html_design_quality["page_diversity_score"])
                except (KeyError, TypeError, ValueError):
                    pass
                else:
                    quality_label = QLabel(_trf(
                        "ai_assistant.motion_quality",
                        "内容完整度 {overall:.0%}  ·  视觉语言 {visual:.0%}"
                        "  ·  页面差异 {diversity:.0%}",
                        overall=overall,
                        visual=visual,
                        diversity=diversity,
                    ))
                    quality_label.setStyleSheet("color: #8ccf92; font-size: 11px;")
                    quality_label.setToolTip(
                        tr(
                            "ai_assistant.motion_quality_tooltip",
                            "Deterministic check based on local HTML structure; it does not judge subjective aesthetics.",
                        )
                    )
                    layout.addWidget(quality_label)
                responsive = html_design_quality.get("responsive_layout_score")
                if isinstance(responsive, (int, float)):
                    adaptive_label = QLabel(_trf(
                        "ai_assistant.motion_responsive",
                        "布局适配 {score:.0%}",
                        score=float(responsive),
                    ))
                    adaptive_label.setStyleSheet("color: #8ccf92; font-size: 11px;")
                    adaptive_label.setToolTip(
                        tr(
                            "ai_assistant.motion_responsive_tooltip",
                            "Checks for fluid sizing or reflow rules; a fixed delivery canvas triggers a rebuild.",
                        )
                    )
                    layout.addWidget(adaptive_label)

            # Literal requirements such as quoted copy, an exact hex color,
            # or a requested fourth page are separately verified before this
            # card is shown.  Keep that result visible so users know the
            # confirmation is for a deck that met those explicit constraints.
            html_intent_checks = card_data.get("html_intent_checks")
            if isinstance(html_intent_checks, dict):
                requirements = html_intent_checks.get("requirements", [])
                issues = html_intent_checks.get("issues", [])
                if isinstance(requirements, (list, tuple)) and requirements:
                    requirement_label = QLabel(_trf(
                        "ai_assistant.motion_requirements",
                        "{icon} 已验收 {count} 条明确需求",
                        icon="✓" if not issues else "⚠",
                        count=len(requirements),
                    ))
                    requirement_label.setStyleSheet(
                        "color: #8ccf92; font-size: 11px;" if not issues
                        else "color: #f59e0b; font-size: 11px;"
                    )
                    requirement_label.setToolTip("; ".join(map(str, requirements)))
                    layout.addWidget(requirement_label)

            # Motion Graphic plan-review mode is intentionally non-executable:
            # the user edits the plan through chat and sends “可以” before the
            # browser authoring call starts.  Keep this separate from the
            # final scene confirmation card so the two approvals cannot be
            # confused.
            if card_data.get("mode") in {"plan_review", "generation_confirmation"}:
                plan_rows = _coerce_dict_list(card_data.get("plan_rows", []))
                for row in plan_rows:
                    if not isinstance(row, dict):
                        continue
                    row_frame = QFrame()
                    row_frame.setStyleSheet(
                        "QFrame { background: rgba(255,255,255,0.03); "
                        "border: 1px solid #3a3d44; border-radius: 4px; }"
                    )
                    row_layout = QHBoxLayout(row_frame)
                    row_layout.setContentsMargins(8, 5, 8, 5)
                    row_layout.setSpacing(8)
                    label = QLabel(str(row.get("label", "")))
                    label.setFixedWidth(64)
                    label.setStyleSheet("color: #9ca3af; font-size: 11px;")
                    value = QLabel(str(row.get("value", "")))
                    value.setWordWrap(True)
                    value.setStyleSheet("color: #e5e7eb; font-size: 11px;")
                    row_layout.addWidget(label)
                    row_layout.addWidget(value, 1)
                    layout.addWidget(row_frame)
                hint_text = (
                    tr(
                        "ai_assistant.motion_generation_hint",
                        "AI will be called only after confirmation; continue with specific feedback to adjust.",
                    )
                    if card_data.get("mode") == "generation_confirmation"
                    else tr(
                        "ai_assistant.motion_plan_hint",
                        "Continue with specific feedback to adjust; when satisfied, send “可以” or click “Start Generation”.",
                    )
                )
                hint = QLabel(hint_text)
                hint.setWordWrap(True)
                hint.setStyleSheet("color: #f59e0b; font-size: 11px;")
                layout.addWidget(hint)

            # MG-04: Creative proposal selection mode — render proposal options.
            if card_data.get("mode") == "proposal_selection":
                proposals = _coerce_dict_list(card_data.get("proposals", []))
                for prop in proposals:
                    pid = prop.get("proposal_id", "?")
                    prop_frame = QFrame()
                    prop_frame.setStyleSheet(
                        "QFrame { background: rgba(255,255,255,0.03); "
                        "border: 1px solid #3a3d44; border-radius: 4px; }"
                    )
                    prop_layout = QVBoxLayout(prop_frame)
                    prop_layout.setContentsMargins(8, 6, 8, 6)
                    prop_layout.setSpacing(2)
                    # AUD-19503：proposal 文案来自模型，转义后再拼接显示。
                    title_lbl = QLabel(
                        f"  {_html.escape(str(pid))}  "
                        f"{_html.escape(str(prop.get('title', '') or ''))}"
                    )
                    title_lbl.setTextFormat(Qt.TextFormat.RichText)
                    title_lbl.setStyleSheet("font-weight: 600; color: #e5e7eb;")
                    prop_layout.addWidget(title_lbl)
                    summary = prop.get("summary", "")
                    if summary:
                        s_lbl = QLabel(_html.escape(str(summary)))
                        s_lbl.setTextFormat(Qt.TextFormat.PlainText)
                        s_lbl.setWordWrap(True)
                        s_lbl.setStyleSheet("color: #9ca3af; font-size: 11px;")
                        prop_layout.addWidget(s_lbl)
                    beats = prop.get("beats_preview", "")
                    if beats:
                        b_lbl = QLabel(_trf(
                            "ai_assistant.motion_copy",
                            "Copy: {value}",
                            value=beats,
                        ))
                        b_lbl.setWordWrap(True)
                        b_lbl.setStyleSheet("color: #7d8087; font-size: 11px;")
                        prop_layout.addWidget(b_lbl)
                    palette = prop.get("palette", "")
                    if palette:
                        p_lbl = QLabel(_trf(
                            "ai_assistant.motion_palette",
                            "Palette: {value}",
                            value=palette,
                        ))
                        p_lbl.setStyleSheet("color: #7d8087; font-size: 11px;")
                        prop_layout.addWidget(p_lbl)
                    select_btn = QPushButton(_trf(
                        "ai_assistant.motion_select_proposal",
                        "Select Proposal {id}",
                        id=pid,
                    ))
                    select_btn.setStyleSheet(
                        "QPushButton { background: #374151; color: #e5e7eb; "
                        "border: 1px solid #4b5563; border-radius: 3px; "
                        "padding: 3px 10px; font-size: 11px; }"
                        "QPushButton:hover { background: #4b5563; }"
                    )
                    select_btn.clicked.connect(
                        lambda _checked=False, rid=request_id, p=pid:
                        self.motion_graphic_proposal_selected.emit(rid, p)
                    )
                    prop_layout.addWidget(select_btn)
                    layout.addWidget(prop_frame)

            # Element list (compact).
            elements = _coerce_str_list(card_data.get("elements", []))
            if elements:
                el_text = tr("ai_assistant.motion_elements", "Elements: ") + ", ".join(
                    f"{e.get('kind', '?')}:{e.get('element_id', '?')}"
                    for e in elements[:8]
                )
                if len(elements) > 8:
                    el_text += f" (+{len(elements) - 8})"
                layout.addWidget(QLabel(el_text))

            # Quality warnings.
            for warn in _coerce_str_list(card_data.get("quality_warnings", []))[:3]:
                layout.addWidget(QLabel(f"⚠ {warn}"))

            # Quality errors (blocking).
            for err in _coerce_str_list(card_data.get("quality_errors", []))[:3]:
                err_lbl = QLabel(f"✗ {err}")
                err_lbl.setStyleSheet("color: #ef4444;")
                layout.addWidget(err_lbl)

            # Risk flags.
            risks = _coerce_str_list(card_data.get("risks", []))
            if risks:
                layout.addWidget(QLabel(_trf(
                    "ai_assistant.motion_risks",
                    "Risks: {values}",
                    values=", ".join(risks[:3]),
                )))

            # Hash row (proves "what you see == what will be applied").
            plan_hash = card_data.get("plan_hash")
            if plan_hash:
                layout.addWidget(QLabel(f"plan: {str(plan_hash)[:12]}…"))

            # Actions.
            actions = QHBoxLayout()
            actions.addStretch(1)
            cancel = QPushButton(tr("ai_assistant.cancel", "取消"))
            actions.addWidget(cancel)
            actions_dict = card_data.get("actions") or ["confirm"]
            if (
                ("confirm" in actions_dict or "generate" in actions_dict or "start" in actions_dict)
                and not card_data.get("has_blocking_quality_errors")
            ):
                is_generate = "generate" in actions_dict
                is_start = "start" in actions_dict
                button_text = tr("ai_assistant.motion_start", "开始生成") if is_start else (
                    tr("ai_assistant.motion_generate", "确认生成") if is_generate
                    else tr("ai_assistant.confirm", "确认执行")
                )
                button_object_name = "assistant_motion_start" if is_start else (
                    "assistant_motion_generate" if is_generate
                    else "assistant_motion_confirm"
                )
                confirm = QPushButton(button_text)
                confirm.setObjectName(button_object_name)
                confirm.setStyleSheet(
                    "QPushButton { background: #e87519; color: white; border: none; "
                    "border-radius: 4px; padding: 5px 14px; }"
                    "QPushButton:hover { background: #f28a2e; }"
                )
                confirm.clicked.connect(
                    lambda _checked=False, rid=request_id:
                    self.motion_graphic_confirm_requested.emit(rid)
                )
                actions.addWidget(confirm)
            if "choose_template" in actions_dict:
                template_btn = QPushButton(
                    tr("ai_assistant.motion_use_template", "改用内置模板")
                )
                template_btn.setStyleSheet(
                    "QPushButton { background: #3b82f6; color: white; border: none; "
                    "border-radius: 4px; padding: 5px 14px; }"
                )
                template_btn.clicked.connect(
                    lambda _checked=False, rid=request_id:
                    self._request_motion_graphic_template(rid)
                )
                actions.addWidget(template_btn)
            cancel.clicked.connect(
                lambda _checked=False, rid=request_id:
                self.motion_graphic_cancel_requested.emit(rid)
            )
            layout.addLayout(actions)

            self._messages_area.setVisible(True)
            self._placeholder.setVisible(False)
            self._insert_message_flow_widget(widget)
            self._motion_graphic_proposals[request_id] = widget
            self._update_content_stretch()
        except Exception as exc:  # noqa: BLE001 - AUD-19502：畸形 card_data 的
            # 异常不允许逃逸到 Qt 槽（兄弟方法 set_skill_recommendation /
            # set_result_card / set_mission_workspace / set_skill_form 同款兜底）。
            self._show_component_error("Motion Graphic 方案卡", exc)
        finally:
            self._updating = False

    def set_skill_recommendation(self, cards: object) -> None:
        """把 SkillCardView 列表渲染成 AiSkillGallery，插进消息流（P1.1）。

        cards 为空列表 / None 时静默跳过。gallery 的 start_requested 转发到
        panel 的 skill_start_requested signal（controller 订阅后触发 skill 执行）。
        """
        if not cards:
            return
        self._updating = True
        try:
            from src.features.edit.ui.ai_skill_card import AiSkillGallery
            gallery = AiSkillGallery()
            gallery.set_cards(cards)  # type: ignore[arg-type]
            gallery.start_requested.connect(self.skill_start_requested)
            self._insert_message_flow_widget(gallery)
        except Exception as exc:
            self._show_component_error("技能推荐", exc)
        finally:
            self._updating = False

    def set_result_card(self, state: object) -> None:
        """把 ResultCardState 渲染成 AiResultCard，插进消息流（P1.5）。

        state 为 None 时静默跳过。卡片插在 messages_layout 尾部 stretch 之前。
        P1.5 动作接线：卡的 action_triggered 转发到 panel 级 result_action_triggered
        signal（controller 订阅后按 action_kind 分派）。
        """
        if state is None:
            return
        self._updating = True
        try:
            from src.features.edit.ui.ai_result_card import AiResultCard
            card = AiResultCard(state)  # type: ignore[arg-type]
            # P1.5 动作接线：转发卡的 action_triggered 到 panel 级 signal
            card.action_triggered.connect(self.result_action_triggered)
            self._insert_message_flow_widget(card)
        except Exception as exc:
            self._show_component_error("结果卡", exc)
        finally:
            self._updating = False

    def set_mission_workspace(self, data: dict) -> None:
        """OS-06: render a compact mission/plan workspace card.

        ``data`` is produced by ``build_mission_workspace_view()`` — shows
        status badge, phase, current step/tool, progress, cost, diagnostics
        and available actions so the user can judge what the agent is doing
        without reading logs.
        """
        if not data:
            return
        self._updating = True
        try:
            widget = QFrame(self)
            widget.setFrameShape(QFrame.Shape.StyledPanel)
            widget.setStyleSheet(
                "QFrame { background: rgba(59,130,246,0.04); "
                "border: 1px solid #3a3d44; border-radius: 6px; }"
            )
            layout = QVBoxLayout(widget)
            layout.setContentsMargins(10, 8, 10, 8)
            layout.setSpacing(3)

            badge = data.get("status_badge", {})
            status_label = badge.get("label", data.get("status", "?"))
            color = badge.get("color", "#6b7280")
            heading = QLabel(f"Mission  {status_label}")
            heading.setStyleSheet(f"font-weight: 600; color: {color};")
            layout.addWidget(heading)

            phase = data.get("phase", "")
            tool = data.get("current_tool_name", "")
            step = data.get("current_step_id", "")
            if phase or tool:
                parts = []
                if phase:
                    parts.append(f"phase: {phase}")
                if tool:
                    parts.append(f"tool: {tool}")
                elif step:
                    parts.append(f"step: {step}")
                layout.addWidget(QLabel("  |  ".join(parts)))

            completed = data.get("completed_steps", 0)
            total = data.get("total_steps", 0)
            pct = data.get("progress_pct", 0)
            cost = data.get("cost_units", 0)
            if total > 0:
                layout.addWidget(QLabel(
                    f"  progress: {completed}/{total} ({pct:g}%)  cost: {cost:g}"
                ))

            plan_hash = data.get("plan_hash", "")
            if plan_hash:
                layout.addWidget(QLabel(f"  plan: {str(plan_hash)[:12]}..."))

            reason = data.get("reason", "")
            if reason:
                reason_lbl = QLabel(f"  {reason[:200]}")
                reason_lbl.setWordWrap(True)
                reason_lbl.setStyleSheet("color: #9ca3af; font-size: 11px;")
                layout.addWidget(reason_lbl)

            diags = data.get("diagnostics", {})
            for err in diags.get("errors", [])[:2]:
                err_lbl = QLabel(f"  [ERROR] {err[:150]}")
                err_lbl.setWordWrap(True)
                err_lbl.setStyleSheet("color: #ef4444; font-size: 11px;")
                layout.addWidget(err_lbl)
            for warn in diags.get("warnings", [])[:2]:
                warn_lbl = QLabel(f"  [WARN] {warn[:150]}")
                warn_lbl.setWordWrap(True)
                warn_lbl.setStyleSheet("color: #f59e0b; font-size: 11px;")
                layout.addWidget(warn_lbl)

            for rf in data.get("rollback_failures", [])[:2]:
                rf_lbl = QLabel(f"  [ROLLBACK] {rf[:150]}")
                rf_lbl.setWordWrap(True)
                rf_lbl.setStyleSheet("color: #ef4444; font-size: 11px;")
                layout.addWidget(rf_lbl)

            risk = data.get("risk_flags", {})
            if risk.get("writes_timeline") or risk.get("requires_approval"):
                risk_parts = []
                if risk.get("writes_timeline"):
                    risk_parts.append(f"{risk['writes_timeline']} timeline writes")
                if risk.get("requires_approval"):
                    risk_parts.append(f"{risk['requires_approval']} need approval")
                layout.addWidget(QLabel(f"  risk: {', '.join(risk_parts)}"))

            actions_row = QHBoxLayout()
            actions_row.addStretch(1)
            for action in data.get("actions", [])[:4]:
                btn = QPushButton(action)
                btn.setStyleSheet(
                    "QPushButton { background: #374151; color: #e5e7eb; "
                    "border: 1px solid #4b5563; border-radius: 3px; "
                    "padding: 3px 10px; font-size: 11px; }"
                    "QPushButton:hover { background: #4b5563; }"
                )
                actions_row.addWidget(btn)
            layout.addLayout(actions_row)

            self._messages_area.setVisible(True)
            self._placeholder.setVisible(False)
            self._insert_message_flow_widget(widget)
            self._update_content_stretch()
        except Exception as exc:
            self._show_component_error("任务工作区", exc)
        finally:
            self._updating = False

    def set_skill_form(
        self, card_view: object, prefilled_values: dict | None = None,
    ) -> None:
        """渲染一张预填的 skill 表单卡（P1.5 adjust_params）。

        ``card_view`` 是 controller 从 ``catalog.card(skill_id)`` 构造好的
        :class:`SkillCardView`（panel 不持 catalog，解耦）。渲染后调
        :meth:`AiSkillCard.set_form_values` 用原 invocation.inputs 预填。
        表单的 start_requested 转发到 panel.skill_start_requested
        （已有，controller._on_skill_card_start 接）。
        """
        self._updating = True
        try:
            from src.features.edit.ui.ai_skill_card import AiSkillCard
            card = AiSkillCard(card_view)  # type: ignore[arg-type]
            if prefilled_values:
                card.set_form_values(prefilled_values)
            card.start_requested.connect(self.skill_start_requested)
            self._insert_message_flow_widget(card)
        except Exception as exc:
            self._show_component_error("技能表单", exc)
        finally:
            self._updating = False
