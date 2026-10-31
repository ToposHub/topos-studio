"""AI Assistant Composer —— 底部控制栏 + Run Drawer（plan §10.2 P0.3 / P0.5 / P0.6）。

把现有 AI Assistant 从"方案生成面板"升级成可操作、可观察的 Agent 控制台
（plan §0.3）。

本组件分两部分：

1. :class:`AiAssistantComposer` —— **底部控制栏**：Scope（项目范围）、权限等级、
   Profile/模型、上下文 `已用/上限`、压缩、新会话、历史会话、成本/网络提示。
   只发 signal；设置只作用于后续 invocation，绝不改写正在运行的 job。
2. :class:`AiRunDrawer` —— **运行活动流**：每一次工具调用 / 子任务 / 分析 /
   生成 / 审批都显示状态、耗时、输入范围、输出、取消/重试/查看诊断。

P0 阶段（plan §14 step 5）：先接 fake 数据（``ComposerFooterState`` /
``RunDrawerState``），确认 UI 布局后才接真实 :class:`HarnessRuntime`。组件
**不**直接订阅 runtime；由 controller 把 ``RunRecord`` 聚合成 ``RunDrawerState``
后喂进来（解耦渲染，plan §10.1 实施纪律）。

约束（与既有面板一致）：

- 只发细粒度 pyqtSignal，不导入 renderer/exporter/controller 私有字段。
- 状态回填用 ``_updating`` 锁避免信号递归。
- 不在 slot 里跑模型（plan §10.1）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PyQt6.QtCore import QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QBrush, QColor, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QMenu,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.shared.i18n.i18n import tr
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.features.edit.ai_editing.assistant_modes import (
    get_assistant_mode_specs,
    normalize_assistant_mode,
)
from src.features.edit.ai_editing.assistant_plugins import (
    default_assistant_plugin_directory,
    reload_assistant_plugins,
)


def _trf(key: str, default: str, **kwargs) -> str:
    """``tr`` 包装：当返回的是含 ``{}`` 的 default（未命中翻译）时，补做
    ``.format(**kwargs)``，使带 ``n=`` 等参数的默认文案也能正确插值。

    ``I18N.tr`` 在未加载翻译时直接返回 default 字面量、不 format；本 helper
    兜底，避免 UI 显示 ``运行 {n}``。
    """
    text = tr(key, default, **kwargs)
    if "{" in default and kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text


# ---------------------------------------------------------------------- #
# 状态模型（fake 数据；后续 controller 从 HarnessRuntime 聚合）
# ---------------------------------------------------------------------- #


@dataclass
class ComposerFooterState:
    """底部控制栏状态（plan §10.2 P0.3）。

    所有字段都是 UI 展示用的可读摘要；不含 secret / 绝对路径 / 完整 prompt。
    """

    scope_label: str = ""               # "当前选择" / "本 Bin" / "全部素材"
    scope_asset_count: int = 0          # 范围内素材数量
    permission: str = "propose"         # read_only / propose / apply_with_each_approval
    profile_label: str = ""             # "本地 Qwen" / "云 GPT-4o"
    model_label: str = ""               # 具体模型覆盖（空表示用 profile 默认）
    profile_options: tuple[tuple[str, str, str], ...] = ()  # (id, name, model)
    active_profile_id: str = ""
    mode: str = "chat"
    reasoning_effort: str = "medium"   # low / medium / high
    context_used: int = 0               # 已用 token
    context_limit: int = 200_000        # token 上限
    context_compressed: bool = False    # 是否已压缩
    cost_estimate: str = ""             # "约 ¥0.12" / ""
    network_hint: str = ""              # "将上传 2 个素材到云" / ""
    has_active_run: bool = False        # 是否有运行中的 job（影响"新对话"可用性）


@dataclass
class RunDrawerItem:
    """Run Drawer 单条活动（plan §0.3 第 3 条）。"""

    item_id: str = ""
    label: str = ""                     # "Qwen3-ASR 转录" / "字幕轨道提议"
    status: str = "idle"                # idle/queued/running/cached/waiting_input/
                                        #   succeeded/failed/cancelled/skipped
    progress: float = 0.0               # 0.0–1.0
    duration_ms: int | None = None      # 耗时（None 表示进行中）
    scope_summary: str = ""             # "a1, a2（2 个素材）"
    output_summary: str = ""            # "转录 1:23，3 段"
    can_cancel: bool = False
    can_retry: bool = False
    can_view_diagnostics: bool = False
    can_resume: bool = False
    can_handoff: bool = False
    can_rollback: bool = False
    error_code: str = ""
    kind: str = "run"                 # run / node / job / artifact / history
    parent_id: str = ""
    depth: int = 0
    checkpoint_summary: str = ""
    recovery_summary: str = ""


@dataclass
class RunDrawerState:
    """Run Drawer 整体状态。"""

    items: list[RunDrawerItem] = field(default_factory=list)
    active_count: int = 0
    completed_count: int = 0
    failed_count: int = 0
    history: list[RunDrawerItem] = field(default_factory=list)


# ---------------------------------------------------------------------- #
# 状态色 / 文案（集中，避免散落硬编码）
# ---------------------------------------------------------------------- #


_STATUS_COLORS: dict[str, str] = {
    "idle": "gray",
    "queued": "#2980b9",
    "running": "#2980b9",
    "cached": "#8e44ad",
    "waiting_input": "#f39c12",
    "succeeded": "#27ae60",
    "failed": "#c0392b",
    "cancelled": "#7f8c8d",
    "skipped": "#7f8c8d",
}

_STATUS_LABELS: dict[str, str] = {
    "idle": "未运行",
    "queued": "排队",
    "running": "运行中",
    "cached": "缓存命中",
    "waiting_input": "等待确认",
    "succeeded": "成功",
    "failed": "失败",
    "cancelled": "已取消",
    "skipped": "跳过",
}

_STATUS_LABEL_KEYS: dict[str, str] = {
    "idle": "ai_run_drawer.status_idle",
    "queued": "ai_run_drawer.status_queued",
    "running": "ai_run_drawer.status_running",
    "cached": "ai_run_drawer.status_cached",
    "waiting_input": "ai_run_drawer.status_waiting_input",
    "succeeded": "ai_run_drawer.status_succeeded",
    "failed": "ai_run_drawer.status_failed",
    "cancelled": "ai_run_drawer.status_cancelled",
    "skipped": "ai_run_drawer.status_skipped",
}

_PERMISSION_LABELS: dict[str, str] = {
    "read_only": "仅建议",
    "propose": "可提议",
    "apply_with_each_approval": "逐次审批",
}

_PERMISSION_LABEL_KEYS: dict[str, str] = {
    "read_only": "ai_composer.permission_read_only",
    "propose": "ai_composer.permission_propose",
    "apply_with_each_approval": "ai_composer.permission_apply_each",
}


def _status_color(status: str) -> str:
    return _STATUS_COLORS.get(status, "gray")


def _status_label(status: str) -> str:
    return tr(
        _STATUS_LABEL_KEYS.get(status, "ai_run_drawer.status_unknown"),
        _STATUS_LABELS.get(status, status),
    )


def _permission_label(permission: str) -> str:
    return tr(
        _PERMISSION_LABEL_KEYS.get(permission, "ai_composer.permission_unknown"),
        _PERMISSION_LABELS.get(permission, permission),
    )


def _mode_label(mode_id: str, fallback: str) -> str:
    """Translate a plugin-backed mode by its stable id."""
    return tr(f"ai_composer.mode_{mode_id}", fallback)


class _RotatingRing(QWidget):
    """Small fixed-size activity ring; only the arc rotates, never the layout."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._angle = 0
        self.setFixedSize(14, 14)
        self._timer = QTimer(self)
        self._timer.setInterval(55)
        self._timer.timeout.connect(self._tick)
        self.setVisible(False)

    @property
    def angle(self) -> int:
        """Current arc angle, exposed for deterministic UI tests."""
        return self._angle

    def set_active(self, active: bool) -> None:
        active = bool(active)
        self.setVisible(active)
        if active:
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _tick(self) -> None:
        self._angle = (self._angle + 24) % 360
        self.update()

    def paintEvent(self, _event: object) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(2, 2, -2, -2)
        painter.setPen(QPen(QColor("#45464f"), 1.3))
        painter.drawEllipse(rect)
        pen = QPen(QColor("#f28a27"), 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect, int(self._angle * 16), int(112 * 16))


class _ShimmerStatusLabel(QLabel):
    """Plain one-line label with a moving highlight, preserving text width."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._shimmer_active = False
        self._shimmer_phase = 0

    @property
    def shimmer_phase(self) -> int:
        return self._shimmer_phase

    def set_shimmer_active(self, active: bool) -> None:
        self._shimmer_active = bool(active)
        self.update()

    def advance_shimmer(self) -> None:
        self._shimmer_phase = (self._shimmer_phase + 1) % 120
        self.update()

    def paintEvent(self, event: object) -> None:  # noqa: N802 - Qt override
        if not self._shimmer_active or not self.text():
            super().paintEvent(event)  # type: ignore[arg-type]
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        progress = self._shimmer_phase / 119.0
        center = -0.18 + progress * 1.36
        clamp = lambda value: max(0.0, min(1.0, float(value)))
        gradient = QLinearGradient(0, 0, max(1, self.width()), 0)
        gradient.setColorAt(0.0, QColor("#9da0aa"))
        gradient.setColorAt(clamp(center - 0.16), QColor("#9da0aa"))
        gradient.setColorAt(clamp(center), QColor("#f6b36b"))
        gradient.setColorAt(clamp(center + 0.16), QColor("#9da0aa"))
        gradient.setColorAt(1.0, QColor("#9da0aa"))
        painter.setPen(QPen(QBrush(gradient), 1))
        painter.drawText(
            self.rect(),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self.text(),
        )


# ---------------------------------------------------------------------- #
# Composer footer
# ---------------------------------------------------------------------- #


class AiAssistantComposer(QWidget):
    """底部控制栏（plan §10.2 P0.3）。

    Signals（全部只通知意图，不直接执行）：

    - ``scope_clicked``：用户点击 scope 区域 → 弹范围确认（含"分析所有素材"提示）。
    - ``permission_changed(str)``：切换权限等级。
    - ``profile_clicked``：打开 profile/模型选择。
    - ``context_inspect_clicked``：打开上下文检查器。
    - ``context_compress_clicked``：触发压缩（生成新摘要，不丢原始 evidence）。
    - ``new_session_clicked``：新建会话（不删除历史）。
    - ``history_clicked``：打开历史会话列表。
    """

    scope_clicked = pyqtSignal()
    permission_changed = pyqtSignal(str)
    profile_clicked = pyqtSignal()
    profile_changed = pyqtSignal(str)
    mode_changed = pyqtSignal(str)
    reasoning_changed = pyqtSignal(str)
    context_inspect_clicked = pyqtSignal()
    context_compress_clicked = pyqtSignal()
    new_session_clicked = pyqtSignal()
    history_clicked = pyqtSignal()
    history_session_selected = pyqtSignal(str)
    configure_models_clicked = pyqtSignal()
    add_action_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._updating = False
        # AUD-19407: 上次插件目录签名为 None（未扫描）；菜单打开时只在签名
        # 变化后才同步重扫插件目录，避免每次打开菜单都在 UI 线程做全量 IO。
        self._plugins_dir_signature: tuple | None = None
        self._state = ComposerFooterState()
        self._build_ui()
        self.set_state(self._state)

    # ---------------- 构建 ---------------- #

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)

        # 第 1 行：模式 · 权限 · Profile · 推理级别。
        row1 = QHBoxLayout()
        row1.setSpacing(6)

        # Codex 风格的添加入口：低频能力集中在一个小菜单里，避免再把
        # 文件、截图、上下文和会话操作堆在输入框外面。
        self._add_btn = QToolButton()
        self._add_btn.setObjectName("assistant_add_button")
        self._add_btn.setText("+")
        self._add_btn.setFixedSize(24, 24)
        self._add_btn.setToolTip(tr(
            "ai_assistant.add_menu_tooltip", "添加图片、视频、音频、PDF、文档或上下文操作",
        ))
        self._add_btn.setStyleSheet(
            "QToolButton { border: none; background: transparent;"
            " color: #b8b8be; font-size: 19px; font-weight: 400;"
            " padding: 0; }"
            "QToolButton:hover { color: #ffffff; background: #3a3a3f;"
            " border-radius: 12px; }"
        )
        self._add_menu = self._build_add_menu()
        # 不把 QMenu 绑定到 QToolButton：Qt 会自动绘制下拉箭头，窄输入框里
        # 看起来像“+↧”且会让按钮基线偏低。保留菜单对象，但由点击事件显式弹出，
        # 视觉上只显示一个与后续文字垂直居中的加号。
        self._add_btn.clicked.connect(self._show_add_menu)
        row1.addWidget(self._add_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self._scope_btn = QPushButton()
        self._scope_btn.setToolTip(tr(
            "ai_composer.scope_tooltip",
            "点击确认分析范围（默认只分析当前选择）",
        ))
        self._scope_btn.clicked.connect(self.scope_clicked.emit)
        self._scope_btn.setStyleSheet(
            "QPushButton { border: none; background: transparent;"
            " text-align: left; padding: 3px 4px; }"
            "QPushButton:hover { color: #e87519; }"
        )
        self._scope_btn.setVisible(False)

        # 保留 combo 作为内部状态源与旧测试/宿主兼容；可见 UI 使用下方的
        # 无背景组合菜单，避免在窄面板里堆叠多个带边框下拉框。
        self._mode_combo = ChevronComboBox()
        for spec in get_assistant_mode_specs():
            self._mode_combo.addItem(
                _mode_label(spec.mode_id, spec.label), spec.mode_id
            )
        self._mode_combo.setObjectName("assistant_mode_combo")
        self._mode_combo.setToolTip(tr(
            "ai_composer.mode_tooltip",
            "选择本条消息的工作模式：普通对话、Motion Graphic 或媒体生成",
        ))
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self._mode_combo.setVisible(False)

        # 与 Color / Camera Raw 等面板共用 ChevronComboBox，避免 AI 面板
        # 自己再维护一套下拉框外观。
        self._perm_combo = ChevronComboBox()
        for perm_key, perm_lbl in _PERMISSION_LABELS.items():
            self._perm_combo.addItem(_permission_label(perm_key), perm_key)
        self._perm_combo.currentIndexChanged.connect(self._on_perm_changed)
        self._perm_combo.setVisible(False)

        self._conversation_btn = QToolButton()
        self._conversation_btn.setObjectName("assistant_conversation_selector")
        self._conversation_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._conversation_btn.setToolTip(tr(
            "ai_composer.conversation_selector_tooltip",
            "选择对话模式和权限",
        ))
        self._conversation_btn.setStyleSheet(
            "QToolButton { border: none; background: transparent;"
            " text-align: left; padding: 2px 3px; font-size: 11px; }"
            "QToolButton:hover { color: #e87519; }"
        )
        self._conversation_btn.clicked.connect(self._show_conversation_menu)
        row1.addWidget(self._conversation_btn)

        self._conversation_menu = QMenu(self._conversation_btn)
        self._mode_menu = self._conversation_menu.addMenu(
            tr("ai_composer.mode", "模式")
        )
        for spec in get_assistant_mode_specs():
            action = QAction(
                _mode_label(spec.mode_id, spec.label), self._mode_menu
            )
            action.setData(spec.mode_id)
            action.setCheckable(True)
            self._mode_menu.addAction(action)
        self._mode_menu.triggered.connect(self._on_mode_action)
        self._permission_menu = self._conversation_menu.addMenu(
            tr("ai_composer.permission", "权限")
        )
        for perm_key in _PERMISSION_LABELS:
            action = QAction(_permission_label(perm_key), self._permission_menu)
            action.setData(perm_key)
            action.setCheckable(True)
            self._permission_menu.addAction(action)
        self._permission_menu.triggered.connect(self._on_permission_action)

        self._profile_btn = QToolButton()
        self._profile_btn.setObjectName("assistant_model_reasoning_selector")
        self._profile_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._profile_btn.setToolTip(tr(
            "ai_composer.profile_tooltip", "选择模型和推理强度",
        ))
        self._profile_btn.clicked.connect(self._on_profile_clicked)
        self._profile_btn.setStyleSheet(
            "QToolButton { border: none; background: transparent;"
            " text-align: left; padding: 2px 3px; font-size: 11px; }"
            "QToolButton:hover { color: #e87519; }"
        )
        row1.addWidget(self._profile_btn, stretch=2)

        self._reasoning_combo = ChevronComboBox()
        self._reasoning_combo.addItem(tr("ai_composer.reasoning_low", "低"), "low")
        self._reasoning_combo.addItem(tr("ai_composer.reasoning_medium", "中"), "medium")
        self._reasoning_combo.addItem(tr("ai_composer.reasoning_high", "高"), "high")
        self._reasoning_combo.setFixedWidth(58)
        self._reasoning_combo.setToolTip(tr(
            "ai_composer.reasoning_tooltip", "选择模型推理强度（低 / 中 / 高）",
        ))
        self._reasoning_combo.currentIndexChanged.connect(self._on_reasoning_changed)
        self._reasoning_combo.setVisible(False)

        self._profile_reasoning_menu = QMenu(self._profile_btn)
        self._profile_menu = self._profile_reasoning_menu.addMenu(
            tr("ai_composer.model", "模型")
        )
        self._profile_menu.triggered.connect(self._on_profile_action)
        self._reasoning_menu = self._profile_reasoning_menu.addMenu(
            tr("ai_composer.reasoning", "推理强度")
        )
        for effort, label_key, default_label in (
            ("low", "ai_composer.reasoning_low", "低"),
            ("medium", "ai_composer.reasoning_medium", "中"),
            ("high", "ai_composer.reasoning_high", "高"),
        ):
            action = QAction(tr(label_key, default_label), self._reasoning_menu)
            action.setData(effort)
            action.setCheckable(True)
            self._reasoning_menu.addAction(action)
        self._reasoning_menu.triggered.connect(self._on_reasoning_action)

        # 同一行继续放上下文预算和成本摘要。
        row2 = row1
        row2.setSpacing(4)
        # 给发送按钮留一点右侧内边距，让它比输入框边缘略向左收。
        row2.setContentsMargins(0, 0, 4, 0)

        self._context_btn = QToolButton()
        self._context_btn.setText(tr("ai_composer.context", "上下文"))
        self._context_btn.clicked.connect(self.context_inspect_clicked.emit)
        self._context_btn.setVisible(False)

        self._context_label = QLabel()
        self._context_label.setStyleSheet("color: gray;")
        # 上下文用量已移到面板顶部状态栏，底部保留内部状态值供兼容测试与状态回填。
        self._context_label.setVisible(False)
        row2.addWidget(self._context_label)

        self._compress_btn = QToolButton()
        self._compress_btn.setText(tr("ai_composer.compress", "压缩"))
        self._compress_btn.setToolTip(tr(
            "ai_composer.compress_tooltip",
            "压缩上下文（生成新摘要，原始证据不丢，可恢复）",
        ))
        self._compress_btn.clicked.connect(self.context_compress_clicked.emit)
        self._compress_btn.setVisible(False)

        row2.addStretch(1)

        self._cost_label = QLabel()
        self._cost_label.setStyleSheet("color: gray; font-size: 9px;")
        row2.addWidget(self._cost_label)

        self._toolbar_layout = row2
        layout.addLayout(row2)

        # 第 3 行：网络提示（仅当有上传/付费风险时显示）
        self._network_label = QLabel()
        self._network_label.setStyleSheet(
            "color: #c0392b; padding: 1px; border-radius: 3px; font-size: 9px;"
        )
        self._network_label.setWordWrap(True)
        self._network_label.setVisible(False)
        layout.addWidget(self._network_label)

        # 保留隐藏按钮属性与 signal 兼容性；常规界面通过面板顶部/右键菜单触发。
        self._new_session_btn = QPushButton(tr("ai_composer.new_session", "新对话"))
        self._new_session_btn.setToolTip(tr(
            "ai_composer.new_session_tooltip",
            "新建会话（不删除历史，可随时切换）",
        ))
        self._new_session_btn.clicked.connect(self.new_session_clicked.emit)
        self._history_btn = QPushButton(tr("ai_composer.history", "历史"))
        self._history_btn.clicked.connect(self.history_clicked.emit)
        self._new_session_btn.setVisible(False)
        self._history_btn.setVisible(False)

    def _show_add_menu(self) -> None:
        """Show the add menu without Qt's built-in popup arrow indicator."""
        self._add_menu.exec(
            self._add_btn.mapToGlobal(self._add_btn.rect().bottomLeft())
        )

    def _build_add_menu(self) -> QMenu:
        """构建底部 ``+`` 菜单，只暴露当前项目已有的真实入口。"""
        menu = QMenu(self._add_btn)
        menu.setTitle(tr("ai_assistant.add_menu_title", "添加"))

        def add_action(text: str, action_id: str, *, tooltip: str = "") -> QAction:
            action = QAction(text, menu)
            action.setData(action_id)
            if tooltip:
                action.setToolTip(tooltip)
            action.triggered.connect(
                lambda _checked=False, aid=action_id: self.add_action_requested.emit(aid)
            )
            menu.addAction(action)
            return action

        add_action(
            tr("ai_assistant.add_image_files", "图片文件（可多选）"),
            "image_files",
            tooltip=tr(
                "ai_assistant.add_image_files_tooltip",
                "选择一张或多张图片，作为本条消息的视觉附件",
            ),
        )
        add_action(
            tr(
                "ai_assistant.add_multimodal_files",
                "文件（视频、音频、PDF、文档）",
            ),
            "file_attachments",
            tooltip=tr(
                "ai_assistant.add_multimodal_files_tooltip",
                "上传视频、音频、PDF、文本和常见办公文档",
            ),
        )
        add_action(
            tr("ai_assistant.add_clipboard_image", "剪贴板图片"),
            "clipboard_image",
            tooltip=tr(
                "ai_assistant.add_clipboard_image_tooltip",
                "把剪贴板中的截图加入本条消息",
            ),
        )
        add_action(
            tr("ai_assistant.add_current_frame", "当前画面"),
            "current_frame",
            tooltip=tr(
                "ai_assistant.add_current_frame_tooltip",
                "抓取当前视口画面作为图片附件",
            ),
        )

        menu.addSeparator()
        add_action(
            tr("ai_assistant.add_scope", "素材 / 文件夹范围"),
            "scope",
            tooltip=tr(
                "ai_assistant.add_scope_tooltip",
                "选择本次对话使用的素材范围",
            ),
        )
        add_action(
            tr("ai_assistant.add_context", "检查上下文"),
            "context",
        )
        add_action(
            tr("ai_assistant.add_compress", "压缩上下文"),
            "compress",
        )

        menu.addSeparator()
        add_action(tr("ai_assistant.add_new_session", "新对话"), "new_session")
        add_action(tr("ai_assistant.add_history", "历史会话"), "history")
        add_action(tr("ai_assistant.add_models", "配置模型"), "models")
        return menu

    def add_action_widget(self, widget: QWidget) -> None:
        """把外部主操作并入同一行工具栏，不额外占用一整行。"""
        self._toolbar_layout.addWidget(widget)

    def set_history_sessions(
        self, sessions: object, *, active_session_id: str = ""
    ) -> None:
        """保留旧调用入口；历史会话现在由面板顶部/右键菜单统一承载。"""
        del sessions, active_session_id

    @staticmethod
    def _history_value(value: object, name: str, default: object = None) -> object:
        if isinstance(value, dict):
            return value.get(name, default)
        return getattr(value, name, default)

    def _on_profile_clicked(self) -> None:
        """显示模型/推理强度菜单；无配置时保留旧的配置入口信号。"""
        if not self._state.profile_options:
            self.profile_clicked.emit()
            return
        self._profile_reasoning_menu.exec(
            self._profile_btn.mapToGlobal(self._profile_btn.rect().bottomLeft())
        )

    def _show_conversation_menu(self) -> None:
        """显示模式与权限的组合菜单。"""
        # AUD-19407: 目录签名未变则跳过同步重扫（详见 _refresh_plugins_if_changed）。
        self._refresh_plugins_if_changed()
        self._conversation_menu.exec(
            self._conversation_btn.mapToGlobal(
                self._conversation_btn.rect().bottomLeft()
            )
        )

    def _refresh_plugins_if_changed(self) -> None:
        """插件目录签名未变化时跳过同步重扫（AUD-19407）。

        Folder-based plugins can be added or removed while the application is
        open, so the selector must stay truthful — but a full rescan (JSON
        manifests per plugin folder) on every menu open is synchronous
        directory IO on the UI thread. A lightweight mtime signature of the
        plugin tree gates the rescan: unchanged tree → reuse the current
        registry, changed/unknown → full reload.
        """
        signature = self._plugins_directory_signature()
        if signature is not None and signature == self._plugins_dir_signature:
            return  # 目录未变化：跳过同步重扫
        self._plugins_dir_signature = signature
        self.refresh_assistant_plugins()

    @staticmethod
    def _plugins_directory_signature() -> tuple | None:
        """插件目录的轻量签名（mtime 级，不做 JSON 解析）。

        覆盖：插件文件夹增删（父目录 mtime）、manifest 新增/删除/改动
        （plugin.json mtime）。目录不可读时返回 ``None``（保守触发全量重扫）。
        """
        root = default_assistant_plugin_directory()
        try:
            root_stat = root.stat()
            entries: list[tuple[str, int, int]] = []
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                child_stat = child.stat()
                try:
                    manifest_stat = (child / "plugin.json").stat()
                    manifest_mtime = manifest_stat.st_mtime_ns
                except OSError:
                    manifest_mtime = -1
                entries.append(
                    (child.name, child_stat.st_mtime_ns, manifest_mtime)
                )
            return (root_stat.st_mtime_ns, tuple(sorted(entries)))
        except OSError:
            return None

    def refresh_assistant_plugins(self) -> None:
        """Rescan mode folders and rebuild the hidden combo/menu catalog."""

        reload_assistant_plugins()
        specs = get_assistant_mode_specs()
        current_mode = normalize_assistant_mode(self._state.mode)
        self._mode_combo.blockSignals(True)
        self._mode_combo.clear()
        for spec in specs:
            self._mode_combo.addItem(
                _mode_label(spec.mode_id, spec.label), spec.mode_id
            )
        current_index = self._mode_combo.findData(current_mode)
        if current_index >= 0:
            self._mode_combo.setCurrentIndex(current_index)
        self._mode_combo.blockSignals(False)

        self._mode_menu.clear()
        for spec in specs:
            action = QAction(
                _mode_label(spec.mode_id, spec.label), self._mode_menu
            )
            action.setData(spec.mode_id)
            action.setCheckable(True)
            action.setChecked(spec.mode_id == current_mode)
            self._mode_menu.addAction(action)
        self._state.mode = current_mode
        self._update_conversation_button()

    def _on_profile_action(self, action: QAction) -> None:
        profile_id = action.data()
        if profile_id:
            self.profile_changed.emit(str(profile_id))

    def _on_mode_action(self, action: QAction) -> None:
        if self._updating:
            return
        mode = action.data()
        if not mode:
            return
        mode_idx = self._mode_combo.findData(mode)
        if mode_idx >= 0:
            self._mode_combo.blockSignals(True)
            self._mode_combo.setCurrentIndex(mode_idx)
            self._mode_combo.blockSignals(False)
        self._state.mode = str(mode)
        self._update_conversation_button()
        self.mode_changed.emit(str(mode))

    def _on_permission_action(self, action: QAction) -> None:
        if self._updating:
            return
        permission = action.data()
        if not permission:
            return
        permission_idx = self._perm_combo.findData(permission)
        if permission_idx >= 0:
            self._perm_combo.blockSignals(True)
            self._perm_combo.setCurrentIndex(permission_idx)
            self._perm_combo.blockSignals(False)
        self._state.permission = str(permission)
        self._update_conversation_button()
        self.permission_changed.emit(str(permission))

    def _on_reasoning_action(self, action: QAction) -> None:
        if self._updating:
            return
        effort = action.data()
        if not effort:
            return
        effort_idx = self._reasoning_combo.findData(effort)
        if effort_idx >= 0:
            self._reasoning_combo.blockSignals(True)
            self._reasoning_combo.setCurrentIndex(effort_idx)
            self._reasoning_combo.blockSignals(False)
        self._state.reasoning_effort = str(effort)
        self._update_profile_reasoning_button()
        self.reasoning_changed.emit(str(effort))

    @staticmethod
    def _profile_text(profile_name: str, model_name: str) -> str:
        """紧凑显示 Profile 与模型，避免名称相同时重复渲染。"""
        name = (profile_name or "").strip()
        model = (model_name or "").strip()
        if name and model and name.casefold() == model.casefold():
            return name
        if name and model:
            return f"{name} · {model}"
        return name or model

    def _on_reasoning_changed(self, _index: int) -> None:
        if self._updating:
            return
        effort = self._reasoning_combo.currentData()
        if effort:
            self._state.reasoning_effort = str(effort)
            self._update_profile_reasoning_button()
            self.reasoning_changed.emit(str(effort))

    def _update_conversation_button(self) -> None:
        mode = self._mode_combo.currentText() or tr("ai_composer.mode_chat", "普通对话")
        permission = self._perm_combo.currentText() or _permission_label("propose")
        self._conversation_btn.setText(f"{mode} · {permission} ⌄")

    def _update_profile_reasoning_button(self) -> None:
        profile_text = self._profile_btn.property("assistant_profile_text")
        if not profile_text:
            profile_text = tr("ai_composer.no_profile", "未配置")
        reasoning_text = self._reasoning_combo.currentText() or tr(
            "ai_composer.reasoning_medium", "中"
        )
        self._profile_btn.setText(f"{profile_text} · {reasoning_text} ⌄")

    # ---------------- 状态回填 ---------------- #

    def set_state(self, state: ComposerFooterState) -> None:
        """用 footer 状态刷新 UI（``_updating`` 锁防递归）。"""
        self._updating = True
        try:
            self._state = state
            # Scope
            count_txt = (
                tr("ai_composer.scope_all", "全部素材")
                if state.scope_label == ""
                else state.scope_label
            )
            asset_txt = _trf(
                "ai_composer.scope_count", "（{n} 个）", n=state.scope_asset_count
            ) if state.scope_asset_count else ""
            self._scope_btn.setText(f"{count_txt}{asset_txt}")

            # 权限（按 data 定位，避免触发 currentIndexChanged）
            perm_idx = self._perm_combo.findData(state.permission)
            if perm_idx >= 0:
                self._perm_combo.blockSignals(True)
                self._perm_combo.setCurrentIndex(perm_idx)
                self._perm_combo.blockSignals(False)
            for action in self._permission_menu.actions():
                action.setChecked(action.data() == state.permission)

            # Mode（按 data 定位，避免状态回填时重新路由请求）。
            mode_idx = self._mode_combo.findData(getattr(state, "mode", "chat"))
            if mode_idx >= 0:
                self._mode_combo.blockSignals(True)
                self._mode_combo.setCurrentIndex(mode_idx)
                self._mode_combo.blockSignals(False)
            self._mode_combo.setEnabled(not state.has_active_run)
            for action in self._mode_menu.actions():
                action.setChecked(action.data() == getattr(state, "mode", "chat"))
            self._conversation_btn.setEnabled(not state.has_active_run)
            self._update_conversation_button()

            # Profile / 模型
            prof = state.profile_label or tr("ai_composer.no_profile", "未配置")
            self._profile_btn.setProperty(
                "assistant_profile_text",
                self._profile_text(prof, state.model_label),
            )

            self._profile_menu.clear()
            seen_profile_ids: set[str] = set()
            for profile_id, profile_name, model_name in state.profile_options:
                if profile_id in seen_profile_ids:
                    continue
                seen_profile_ids.add(profile_id)
                label = self._profile_text(profile_name or profile_id, model_name)
                action = QAction(label, self._profile_menu)
                action.setData(profile_id)
                action.setCheckable(True)
                action.setChecked(profile_id == state.active_profile_id)
                self._profile_menu.addAction(action)

            effort_idx = self._reasoning_combo.findData(state.reasoning_effort)
            if effort_idx >= 0:
                self._reasoning_combo.blockSignals(True)
                self._reasoning_combo.setCurrentIndex(effort_idx)
                self._reasoning_combo.blockSignals(False)
            for action in self._reasoning_menu.actions():
                action.setChecked(action.data() == state.reasoning_effort)
            self._update_profile_reasoning_button()

            # 上下文 used/limit
            used_k = state.context_used / 1024.0
            limit_k = state.context_limit / 1024.0
            compressed_mark = (
                " · " + tr("ai_composer.compressed", "已压缩")
                if state.context_compressed else ""
            )
            self._context_label.setText(
                f"{used_k:.1f}k / {limit_k:.0f}k{compressed_mark}"
            )

            # 成本 / 网络
            self._cost_label.setText(state.cost_estimate or "")
            if state.network_hint:
                self._network_label.setText("⚠ " + state.network_hint)
                self._network_label.setVisible(True)
            else:
                self._network_label.setVisible(False)

            # 运行中时禁用"新对话"（避免丢失活跃 run 上下文）
            self._new_session_btn.setEnabled(not state.has_active_run)
        finally:
            self._updating = False

    @property
    def state(self) -> ComposerFooterState:
        return self._state

    @property
    def is_updating(self) -> bool:
        return self._updating

    # ---------------- slots ---------------- #

    def _on_perm_changed(self, _idx: int) -> None:
        if self._updating:
            return
        perm = self._perm_combo.currentData()
        if perm:
            self._state.permission = str(perm)
            self._update_conversation_button()
            self.permission_changed.emit(str(perm))

    def _on_mode_changed(self, _idx: int) -> None:
        if self._updating:
            return
        mode = self._mode_combo.currentData()
        if mode:
            # Keep the local state current even when the composer is used in a
            # lightweight host without a controller immediately refreshing it.
            self._state.mode = str(mode)
            self._update_conversation_button()
            self.mode_changed.emit(str(mode))


# ---------------------------------------------------------------------- #
# Run Drawer
# ---------------------------------------------------------------------- #


class AiRunDrawer(QWidget):
    """运行活动流（plan §0.3 第 3 条 / §10.2 P0.6）。

    显示每一次工具调用 / 子任务 / 分析 / 生成 / 审批的状态、耗时、范围、输出。
    用户能取消 / 重试 / 查看诊断，不能只看到"规划中"。

    Signals：

    - ``cancel_clicked(str)``：item_id。
    - ``retry_clicked(str)``：item_id。
    - ``diagnostics_clicked(str)``：item_id。
    """

    cancel_clicked = pyqtSignal(str)
    retry_clicked = pyqtSignal(str)
    diagnostics_clicked = pyqtSignal(str)
    resume_clicked = pyqtSignal(str)
    handoff_clicked = pyqtSignal(str)
    rollback_clicked = pyqtSignal(str)
    artifact_clicked = pyqtSignal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        compact: bool = False,
        single_line: bool = False,
    ) -> None:
        super().__init__(parent)
        self._updating = False
        self._compact = bool(compact)
        self._single_line = bool(single_line)
        self._single_line_item_id = ""
        # The assistant only needs one unobtrusive status line during a run.
        # Details remain one click away, matching Codex's low-noise activity UI.
        self._expanded = not self._compact and not self._single_line
        self._state = RunDrawerState()
        self._build_ui()
        self.set_state(self._state)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2 if self._compact else 6, 1 if self._compact else 4,
                                  2 if self._compact else 6, 1 if self._compact else 4)
        layout.setSpacing(1 if self._compact else 4)

        # 标题 + 计数
        header = QHBoxLayout()
        self._activity_spinner = _RotatingRing()
        header.addWidget(self._activity_spinner)
        self._title = _ShimmerStatusLabel()
        if self._compact:
            self._title.setStyleSheet("font-size: 10px; color: #b8b8be;")
            self._title.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum,
            )
        self._title.setTextFormat(
            Qt.TextFormat.PlainText if self._compact else Qt.TextFormat.RichText
        )
        if not self._compact:
            self._title.setText(f"<b>{tr('ai_run_drawer.title', '运行活动')}</b>")
        header.addWidget(self._title)
        header.addStretch(1)
        self._count_label = QLabel()
        self._count_label.setStyleSheet(
            "color: #85858c; font-size: 9px;" if self._compact else "color: gray;"
        )
        header.addWidget(self._count_label)
        self._single_line_action: QPushButton | None = None
        if self._single_line:
            self._single_line_action = QPushButton()
            self._single_line_action.setFlat(True)
            self._single_line_action.setCursor(Qt.CursorShape.PointingHandCursor)
            self._single_line_action.setFixedHeight(16)
            self._single_line_action.setStyleSheet(
                "QPushButton { border: none; padding: 0 2px; color: #85858c; font-size: 9px; }"
                "QPushButton:hover { color: #e87519; }"
            )
            self._single_line_action.clicked.connect(self._on_single_line_action)
            self._single_line_action.setVisible(False)
            header.addWidget(self._single_line_action)
        self._toggle_btn: QToolButton | None = None
        if self._compact:
            self._toggle_btn = QToolButton()
            self._toggle_btn.setText("⌃")
            self._toggle_btn.setToolTip(
                tr("ai_run_drawer.toggle_details", "展开或收起活动详情")
            )
            self._toggle_btn.setAutoRaise(True)
            self._toggle_btn.setFixedSize(18, 16)
            self._toggle_btn.setStyleSheet(
                "QToolButton { border: none; padding: 0 3px; color: #9b9ba1; }"
                "QToolButton:hover { color: #e87519; }"
            )
            self._toggle_btn.clicked.connect(self._toggle_compact_details)
            header.addWidget(self._toggle_btn)
            if self._single_line:
                # The assistant composer deliberately keeps one activity row
                # in the conversation flow; detailed nodes belong to the
                # workflow view, not this compact chat surface.
                self._toggle_btn.setVisible(False)
        layout.addLayout(header)

        # 活动列表容器（由 _render_items 填充）
        self._items_host = QWidget()
        self._items_layout = QVBoxLayout(self._items_host)
        self._items_layout.setContentsMargins(0, 0, 0, 0)
        self._items_layout.setSpacing(1 if self._compact else 3)
        self._items_layout.addStretch(1)
        layout.addWidget(self._items_host, stretch=1)

        self._history_label = QLabel(tr("ai_run_drawer.history", "历史运行"))
        self._history_label.setStyleSheet(
            "color: #85858c; font-size: 9px; padding-top: 1px;"
            if self._compact else "color: gray; padding-top: 4px;"
        )
        layout.addWidget(self._history_label)
        self._history_text = QLabel()
        self._history_text.setWordWrap(True)
        self._history_text.setStyleSheet(
            "color: #666; font-size: 9px; padding: 1px 2px;"
            if self._compact else "color: #666; padding: 2px 6px;"
        )
        self._history_text.setVisible(False)
        layout.addWidget(self._history_text)

        # 空态
        self._empty_label = QLabel(
            tr("ai_run_drawer.empty", "暂无运行活动。\n发送需求后将显示逐项任务。")
        )
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setStyleSheet("color: gray; padding: 12px;")
        layout.addWidget(self._empty_label)

    def set_state(self, state: RunDrawerState) -> None:
        self._updating = True
        try:
            self._state = state
            # 计数
            active = state.active_count
            done = state.completed_count
            failed = state.failed_count
            parts = []
            if active:
                parts.append(_trf("ai_run_drawer.active", "运行 {n}", n=active))
            # 紧凑模式的终态主 run 已在标题中显示“✓ 已完成 · token · 耗时”；
            # 有明细时再显示“完成 1”只会重复占行。无 items 的兼容状态仍保留
            # 计数，方便旧版宿主/测试直接灌入聚合数据。
            if done and not (self._compact and state.items):
                parts.append(_trf("ai_run_drawer.done", "完成 {n}", n=done))
            if failed:
                parts.append(_trf("ai_run_drawer.failed", "失败 {n}", n=failed))
            self._count_label.setText(" · ".join(parts))

            if self._compact:
                self._update_compact_header(state)

            has_items = bool(state.items)
            self._empty_label.setVisible(not has_items)
            self._items_host.setVisible(
                has_items and (not self._compact or (self._expanded and not self._single_line))
            )
            self._render_items(self._items_for_compact_details(state.items))
            has_history = bool(state.history)
            self._history_label.setVisible(
                has_history
                and not self._single_line
                and (not self._compact or self._expanded)
            )
            self._history_text.setVisible(
                has_history
                and not self._single_line
                and (not self._compact or self._expanded)
            )
            self._render_history(state.history)
        finally:
            self._updating = False

    @property
    def state(self) -> RunDrawerState:
        return self._state

    def _render_items(self, items: list[RunDrawerItem]) -> None:
        # 清空旧条目（保留尾部 stretch）
        while self._items_layout.count() > 1:
            it = self._items_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        for item in items:
            self._items_layout.insertWidget(
                self._items_layout.count() - 1,
                self._build_compact_item_row(item)
                if self._compact else self._build_item_card(item),
            )

    def _items_for_compact_details(
        self, items: list[RunDrawerItem],
    ) -> list[RunDrawerItem]:
        """Avoid repeating the terminal run already summarized in the header."""
        if self._single_line:
            return []
        if not self._compact or not self._expanded or not items:
            return items
        current = self._compact_current_item(items)
        if current.kind == "run" and current.status in (
            "succeeded", "failed", "cancelled", "skipped",
        ):
            return [item for item in items if item.item_id != current.item_id]
        return items

    @staticmethod
    def _format_duration(duration_ms: int | None) -> str:
        if duration_ms is None:
            return ""
        total_seconds = max(0, int(round(duration_ms / 1000.0)))
        minutes, seconds = divmod(total_seconds, 60)
        if minutes:
            return _trf(
                "ai_run_drawer.duration_minutes",
                "{minutes}分{seconds}秒",
                minutes=minutes,
                seconds=seconds,
            )
        return _trf("ai_run_drawer.duration_seconds", "{seconds}秒", seconds=seconds)

    @classmethod
    def _compact_item_meta(cls, item: RunDrawerItem) -> str:
        parts: list[str] = []
        if item.output_summary and item.output_summary != item.label:
            parts.append(item.output_summary)
        duration = cls._format_duration(item.duration_ms)
        if duration:
            parts.append(duration)
        return " · ".join(parts)

    @staticmethod
    def _compact_current_item(items: list[RunDrawerItem]) -> RunDrawerItem:
        active = next(
            (
                item for item in reversed(items)
                if item.status in ("running", "queued", "waiting_input")
            ),
            None,
        )
        if active is not None:
            return active
        # Tool rows are appended after the main run. Prefer the run summary
        # when everything is terminal, otherwise the header would show the
        # last tool instead of the overall result.
        return next(
            (item for item in reversed(items) if item.kind == "run"),
            items[-1],
        )

    def _update_compact_header(self, state: RunDrawerState) -> None:
        """Show the latest observable action, like Codex's activity line."""
        if not self._compact:
            return
        current = self._compact_current_item(state.items) if state.items else None
        if current is None:
            self._title.setText("")
            self._activity_spinner.set_active(False)
            self._title.set_shimmer_active(False)
            self._single_line_item_id = ""
            if self._single_line_action is not None:
                self._single_line_action.setVisible(False)
            return
        icon = {
            "running": "◌",
            "queued": "…",
            "waiting_input": "!",
            "succeeded": "✓",
            "failed": "×",
            "cancelled": "■",
            "skipped": "–",
        }.get(current.status, "◌")
        is_active = current.status in ("running", "queued", "waiting_input")
        title = f"{current.label}" if is_active else f"{icon} {current.label}"
        meta = self._compact_item_meta(current)
        if meta:
            title += f" · {meta}"
        self._title.setText(title)
        self._activity_spinner.set_active(is_active)
        self._title.set_shimmer_active(is_active)
        self._title.setToolTip(
            tr("ai_run_drawer.toggle_details", "点击箭头查看活动详情")
            + "\n"
            + (meta or current.scope_summary or "")
        )
        if self._toggle_btn is not None:
            self._toggle_btn.setText("⌃" if self._expanded else "⌄")
        self._single_line_item_id = current.item_id
        if self._single_line_action is not None:
            if current.can_cancel:
                self._single_line_action.setText(tr("ai_run_drawer.stop", "停止"))
                self._single_line_action.setVisible(True)
            elif current.can_retry:
                self._single_line_action.setText(tr("ai_run_drawer.retry", "重试"))
                self._single_line_action.setVisible(True)
            else:
                self._single_line_action.setVisible(False)

    def _on_single_line_action(self) -> None:
        """Keep cancel/retry available without adding a second activity row."""
        item_id = self._single_line_item_id
        if not item_id:
            return
        current = (
            self._compact_current_item(self._state.items)
            if self._state.items else None
        )
        if current is not None and current.can_cancel:
            self.cancel_clicked.emit(item_id)
        elif current is not None and current.can_retry:
            self.retry_clicked.emit(item_id)

    def _toggle_compact_details(self) -> None:
        if not self._compact or self._single_line:
            return
        self._expanded = not self._expanded
        self.set_state(self._state)

    def set_live_status(
        self,
        label: str,
        *,
        status: str = "running",
        detail: str = "",
        activity_icon: str | None = None,
    ) -> None:
        """Update the compact header without adding another activity row."""
        if not self._compact:
            return
        del activity_icon  # kept for callers from the previous icon-frame API
        icon = {
            "running": "◌",
            "queued": "…",
            "waiting_input": "!",
            "succeeded": "✓",
            "failed": "×",
            "cancelled": "■",
            "skipped": "–",
        }.get(str(status), "◌")
        is_active = str(status) in ("running", "queued", "waiting_input")
        title = (
            f"{str(label or '').strip()}"
            if is_active else f"{icon} {str(label or '').strip()}"
        ).strip()
        if detail:
            title += f" · {str(detail).strip()}"
        self._title.setText(title)
        self._activity_spinner.set_active(is_active)
        self._title.set_shimmer_active(is_active)
        if is_active:
            self._title.advance_shimmer()
        self._title.setToolTip(title)

    def _build_compact_item_row(self, item: RunDrawerItem) -> QWidget:
        """Render a low-noise activity row; detailed cards remain available
        when ``AiRunDrawer`` is used outside the assistant composer.
        """
        row = QFrame()
        row.setObjectName("assistant_activity_row")
        row.setStyleSheet(
            "QFrame#assistant_activity_row { background: transparent; border: none; }"
        )
        layout = QHBoxLayout(row)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(3)
        row.setMaximumHeight(18)
        color = _status_color(item.status)
        icon = {
            "running": "◌",
            "queued": "…",
            "waiting_input": "!",
            "succeeded": "✓",
            "failed": "×",
            "cancelled": "■",
            "skipped": "–",
        }.get(item.status, "◌")
        status = QLabel(f"<span style='color:{color}'>{icon}</span>")
        status.setTextFormat(Qt.TextFormat.RichText)
        status.setStyleSheet("font-size: 10px;")
        layout.addWidget(status)
        label = QLabel(item.label)
        label.setStyleSheet("color: #c8c8cd; font-size: 10px;")
        label.setWordWrap(False)
        label.setToolTip(item.output_summary or item.scope_summary)
        layout.addWidget(label, stretch=1)
        meta = []
        duration = self._format_duration(item.duration_ms)
        if duration:
            meta.append(duration)
        if item.output_summary and item.output_summary != item.label:
            meta.append(item.output_summary)
        if meta:
            meta_label = QLabel(" · ".join(meta))
            meta_label.setStyleSheet("color: #85858c; font-size: 9px;")
            layout.addWidget(meta_label)
        if item.status in ("running", "queued") and item.progress > 0:
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(int(item.progress * 100))
            bar.setTextVisible(False)
            bar.setFixedSize(64, 5)
            layout.addWidget(bar)
        if item.can_cancel:
            # Keep QPushButton here for compatibility with host panels that
            # discover inline actions by button type; styling stays tool-like.
            btn = QPushButton()
            btn.setText(tr("ai_run_drawer.stop", "停止"))
            btn.setFlat(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(16)
            btn.setStyleSheet(
                "QPushButton { border: none; padding: 0 1px; color: #85858c; font-size: 9px; }"
                "QPushButton:hover { color: #e87519; }"
            )
            btn.clicked.connect(lambda _=False, iid=item.item_id: self.cancel_clicked.emit(iid))
            layout.addWidget(btn)
        if item.can_retry:
            btn = QPushButton()
            btn.setText(tr("ai_run_drawer.retry", "重试"))
            btn.setFlat(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(16)
            btn.setStyleSheet(
                "QPushButton { border: none; padding: 0 1px; color: #85858c; font-size: 9px; }"
                "QPushButton:hover { color: #e87519; }"
            )
            btn.clicked.connect(lambda _=False, iid=item.item_id: self.retry_clicked.emit(iid))
            layout.addWidget(btn)
        return row

    def _render_history(self, items: list[RunDrawerItem]) -> None:
        rows = []
        for item in items:
            details = [_status_label(item.status)]
            if item.checkpoint_summary:
                details.append(
                    _trf(
                        "ai_run_drawer.checkpoint",
                        "断点：{value}",
                        value=item.checkpoint_summary,
                    )
                )
            if item.recovery_summary:
                details.append(
                    _trf(
                        "ai_run_drawer.recovery",
                        "恢复：{value}",
                        value=item.recovery_summary,
                    )
                )
            rows.append(f"• {item.label} · {' · '.join(details)}")
        self._history_text.setText("\n".join(rows))

    def _build_item_card(self, item: RunDrawerItem) -> QWidget:
        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        color = _status_color(item.status)
        card.setStyleSheet(
            "QFrame{background: palette(window);"
            f" border-left: 3px solid {color};"
            " border-top: 1px solid palette(mid);"
            " border-right: 1px solid palette(mid);"
            " border-bottom: 1px solid palette(mid);"
            " border-radius: 3px;}"
        )
        v = QVBoxLayout(card)
        v.setContentsMargins(8, 4, 8, 4)
        v.setSpacing(2)

        # 第 1 行：label + 状态
        row1 = QHBoxLayout()
        indent = "　" * max(0, int(item.depth))
        kind_label = {
            "run": tr("ai_run_drawer.kind_run", "运行"),
            "node": tr("ai_run_drawer.kind_node", "节点"),
            "job": tr("ai_run_drawer.kind_job", "任务"),
            "artifact": tr("ai_run_drawer.kind_artifact", "产物"),
            "history": tr("ai_run_drawer.kind_history", "历史"),
        }.get(item.kind, item.kind)
        lbl = QLabel(f"<b>{indent}[{kind_label}] {item.label}</b>")
        lbl.setTextFormat(Qt.TextFormat.RichText)
        row1.addWidget(lbl, stretch=1)
        status_dot = QLabel(
            f"<span style='color:{color}'>● {_status_label(item.status)}</span>"
        )
        status_dot.setTextFormat(Qt.TextFormat.RichText)
        row1.addWidget(status_dot)
        v.addLayout(row1)

        # 第 2 行：范围 / 输出 / 耗时
        meta_parts = []
        if item.scope_summary:
            meta_parts.append(item.scope_summary)
        if item.output_summary:
            meta_parts.append(item.output_summary)
        if item.duration_ms is not None:
            secs = item.duration_ms / 1000.0
            meta_parts.append(f"{secs:.1f}s")
        if meta_parts:
            meta = QLabel(" · ".join(meta_parts))
            meta.setStyleSheet("color: gray;")
            meta.setWordWrap(True)
            v.addWidget(meta)

        # 错误码
        if item.error_code:
            err = QLabel(
                f"<span style='color:#c0392b'>⚠ {item.error_code}</span>"
            )
            err.setTextFormat(Qt.TextFormat.RichText)
            v.addWidget(err)
        if item.checkpoint_summary:
            checkpoint = QLabel(
                _trf(
                    "ai_run_drawer.checkpoint",
                    "断点：{value}",
                    value=item.checkpoint_summary,
                )
            )
            checkpoint.setStyleSheet("color: #666;")
            v.addWidget(checkpoint)
        if item.recovery_summary:
            recovery = QLabel(
                _trf(
                    "ai_run_drawer.recovery",
                    "恢复：{value}",
                    value=item.recovery_summary,
                )
            )
            recovery.setStyleSheet("color: #666;")
            v.addWidget(recovery)

        # 进度条（running/queued 时显示）
        if item.status in ("running", "queued") and item.progress > 0:
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(int(item.progress * 100))
            bar.setTextVisible(False)
            bar.setFixedHeight(6)
            v.addWidget(bar)

        # 操作按钮
        ops = QHBoxLayout()
        ops.addStretch(1)
        if item.can_cancel:
            btn = QPushButton(tr("ai_run_drawer.cancel", "取消"))
            btn.setStyleSheet("padding: 2px 8px;")
            btn.clicked.connect(lambda _, iid=item.item_id: self.cancel_clicked.emit(iid))
            ops.addWidget(btn)
        if item.can_retry:
            btn = QPushButton(tr("ai_run_drawer.retry", "重试"))
            btn.setStyleSheet("padding: 2px 8px;")
            btn.clicked.connect(lambda _, iid=item.item_id: self.retry_clicked.emit(iid))
            ops.addWidget(btn)
        if item.can_resume:
            btn = QPushButton(tr("ai_run_drawer.resume", "恢复"))
            btn.setStyleSheet("padding: 2px 8px;")
            btn.clicked.connect(lambda _, iid=item.item_id: self.resume_clicked.emit(iid))
            ops.addWidget(btn)
        if item.can_handoff:
            btn = QPushButton(tr("ai_run_drawer.handoff", "人工接管"))
            btn.setStyleSheet("padding: 2px 8px;")
            btn.clicked.connect(lambda _, iid=item.item_id: self.handoff_clicked.emit(iid))
            ops.addWidget(btn)
        if item.can_rollback:
            btn = QPushButton(tr("ai_run_drawer.rollback", "回滚"))
            btn.setStyleSheet("padding: 2px 8px;")
            btn.clicked.connect(lambda _, iid=item.item_id: self.rollback_clicked.emit(iid))
            ops.addWidget(btn)
        if item.can_view_diagnostics:
            btn = QPushButton(tr("ai_run_drawer.diagnostics", "诊断"))
            btn.setStyleSheet("padding: 2px 8px;")
            btn.clicked.connect(
                lambda _, iid=item.item_id: self.diagnostics_clicked.emit(iid)
            )
            ops.addWidget(btn)
        if item.kind == "artifact":
            btn = QPushButton(tr("ai_run_drawer.artifact", "查看 artifact"))
            btn.setStyleSheet("padding: 2px 8px;")
            btn.clicked.connect(lambda _, iid=item.item_id: self.artifact_clicked.emit(iid))
            ops.addWidget(btn)
        if (item.can_cancel or item.can_retry or item.can_resume or item.can_handoff
                or item.can_rollback or item.can_view_diagnostics or item.kind == "artifact"):
            v.addLayout(ops)

        card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        return card


__all__ = [
    "AiAssistantComposer",
    "AiRunDrawer",
    "ComposerFooterState",
    "RunDrawerItem",
    "RunDrawerState",
]
