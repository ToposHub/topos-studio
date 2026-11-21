"""AI model profiles, default assignments, policy and service status in Preferences."""

from __future__ import annotations

import logging
from dataclasses import replace
from hashlib import sha256
from urllib.parse import urlsplit
from typing import Any, Callable

from PyQt6 import sip
from PyQt6.QtCore import QObject, QSignalBlocker, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QStandardItemModel
from PyQt6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTabBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.features.edit.ai_editing.credential_store import (
    CREDENTIAL_STORAGE_FILE,
    CREDENTIAL_STORAGE_KEYCHAIN,
    HybridCredentialStore,
    get_credential_storage_preference,
    make_credential_ref,
    make_credential_store,
)
from src.features.edit.ai_editing.provider_profiles import (
    AiProviderProfile,
    CredentialStore,
    DataPolicy,
    ProfileRegistry,
    ProviderLimits,
)
from src.features.edit.ai_editing.mcp_vision_gateway import (
    McpVisionGateway,
    ZHIPU_VISION_ORIGIN,
    ZHIPU_VISION_PROVIDER_KIND,
    ZHIPU_VISION_TOOL,
)
from src.features.edit.ai_editing.ai_provider_catalog import (
    AiProviderPreset,
    SOURCE_OPTIONS,
    SOURCE_CUSTOM,
    SOURCE_LOCAL,
    SOURCE_OLLAMA,
    SOURCE_PRESET,
    category_for_profile,
    get_preset,
    presets_for_category,
)
from src.shared.i18n.i18n import I18N
from src.shared.ai_models.generation.media_inference import (
    MediaInferenceClient,
    MediaInferenceConfigurationError,
    validate_media_endpoint,
)
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.ui.style_sheet import C

# provider_kind 选项（显示名, 值, endpoint placeholder, model placeholder）
_PROVIDER_OPTIONS = [
    (
        "OpenAI 兼容（本机或局域网）",
        "local_http",
        "http://127.0.0.1:8000/v1",
        "qwen2.5-7b-instruct",
    ),
    (
        I18N.tr("preferences.ai_editing.openai_compatible", "OpenAI-compatible"),
        "openai",
        "https://api.openai.com",
        "gpt-4o",
    ),
    (
        I18N.tr("preferences.ai_editing.provider_ollama", "Ollama (本地)"),
        "ollama",
        "http://localhost:11434",
        "llama3.2",
    ),
    (
        I18N.tr("preferences.ai_editing.provider_anthropic", "Claude (Anthropic)"),
        "anthropic",
        "https://api.anthropic.com",
        "claude-3-5-sonnet-20241022",
    ),
    ("DeepSeek", "deepseek", "https://api.deepseek.com", "deepseek-flash"),
    ("智谱视觉 MCP", ZHIPU_VISION_PROVIDER_KIND, ZHIPU_VISION_ORIGIN, ZHIPU_VISION_TOOL),
    ("OpenAI Responses", "openai_responses", "https://api.openai.com/v1", ""),
    ("火山方舟 Responses", "volcengine_responses", "https://ark.cn-beijing.volces.com/api/plan/v3", "doubao-seed-2.0-lite"),
    ("火山方舟 Seedream 图片", "volcengine_seedream", "https://ark.cn-beijing.volces.com/api/plan/v3", "doubao-seedream-5.0-lite"),
    ("火山方舟 TTS", "volcengine_plan_tts", "https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional", "doubao-seed-tts-2.0"),
    ("本地 Qwen3-ASR", "local_qwen_asr", "", "qwen3_asr"),
    ("本地 Qwen3-TTS", "local_qwen_tts", "", "qwen3_tts_customvoice"),
    (
        "火山方舟 ASR",
        "volcengine_plan_asr",
        "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash",
        "volc.bigasr.auc_turbo",
    ),
]

_CATEGORY_OPTIONS = (
    ("language", "语言", "Language"),
    ("vision", "视觉", "Vision"),
    ("speech", "语音", "Speech"),
    ("embedding", "向量", "Embeddings"),
    ("other", "其他", "Other"),
)

_CAPABILITY_OPTIONS = (
    ("language", "语言理解", "Language"),
    ("vision", "视觉理解", "Visual understanding"),
    ("image", "图片生成", "Image generation"),
    ("video", "视频生成", "Video generation"),
    ("asr", "语音转录（ASR）", "Transcription (ASR)"),
    ("tts", "语音合成（TTS）", "Synthesis (TTS)"),
    ("embedding", "向量化", "Embeddings"),
)

_CATEGORY_CAPABILITIES = {
    "language": {"language"},
    "vision": {"vision", "image", "video"},
    "speech": {"asr", "tts"},
    "embedding": {"embedding"},
}
_PROBED_CAPABILITIES = ("language", "vision", "asr", "embedding")
_BUILTIN_PROTOCOLS = {"local_qwen_asr", "local_qwen_tts"}

_STATUS_LABELS = {
    "available": ("可用", "Available"),
    "not_installed": ("未安装", "Not installed"),
    "offline": ("离线", "Offline"),
    "demo": ("演示", "Demo"),
    "blocked_by_policy": ("策略已禁用", "Disabled by policy"),
    "failed": ("失败", "Failed"),
    "unverified": ("待验证", "Unverified"),
    "disabled": ("已禁用", "Disabled"),
    "not_configured": ("未配置", "Not configured"),
    "unavailable": ("不可用", "Unavailable"),
    "blocked": ("已阻止", "Blocked"),
}

_PAGE_STYLE = f"""
    QWidget#AiPreferences, QWidget#PrefPageContent {{ background: {C.BG_PANEL}; }}
    QTabBar#AiPageTabs::tab, QTabBar#AiProviderCategoryTabs::tab {{
        background: transparent; color: {C.TEXT_SECONDARY};
        border: none; font-size: 12px; padding: 6px 12px;
    }}
    QTabBar#AiPageTabs::tab {{
        border-bottom: 2px solid transparent; border-radius: 0; padding: 10px 16px;
    }}
    QTabBar#AiPageTabs::tab:hover, QTabBar#AiProviderCategoryTabs::tab:hover {{
        background: {C.BG_ACTIVE}; color: {C.TEXT_PRIMARY};
    }}
    QTabBar#AiPageTabs::tab:selected {{
        background: transparent; color: {C.ORANGE}; border-bottom-color: {C.ORANGE};
    }}
    QTabBar#AiProviderCategoryTabs::tab {{ border-radius: 4px; }}
    QTabBar#AiProviderCategoryTabs::tab:selected {{
        background: {C.TOGGLE_PILL_BG}; color: {C.TEXT_PRIMARY};
    }}
    QTabBar#AiPageTabs::tab:focus, QTabBar#AiProviderCategoryTabs::tab:focus {{
        color: {C.ORANGE};
    }}
    QLabel#AiSectionTitle {{
        background: transparent; color: {C.TEXT_PRIMARY}; font-size: 14px; font-weight: 600;
    }}
    QLabel#AiFieldLabel {{ background: transparent; color: {C.TEXT_SECONDARY}; font-size: 12px; }}
    QLabel#AiNote {{ background: transparent; color: {C.TEXT_LABEL}; font-size: 12px; }}
    QFrame#AiDivider {{ background: {C.TOGGLE_PILL_BG}; border: none; }}
    QPushButton#AiAction, QPushButton#AiAddModel, QToolButton#AiMore, QToolButton#AiDisclosure {{
        color: {C.TEXT_PRIMARY}; background: transparent;
        border: 1px solid {C.TOGGLE_PILL_BG}; border-radius: 5px; padding: 4px 10px;
        font-size: 12px;
    }}
    QPushButton#AiAddModel {{ background: {C.TOGGLE_PILL_BG}; }}
    QToolButton#AiMore {{ padding: 0px; }}
    QToolButton#AiMore::menu-indicator {{ image: none; }}
    QToolButton#AiDisclosure {{ border-color: transparent; padding-left: 3px; }}
    QPushButton#AiAction:hover, QPushButton#AiAddModel:hover,
    QToolButton#AiMore:hover, QToolButton#AiDisclosure:hover {{
        background: {C.BG_ACTIVE}; border-color: {C.BORDER_MID};
    }}
    QPushButton#AiAction:focus, QPushButton#AiAddModel:focus,
    QToolButton#AiMore:focus, QToolButton#AiDisclosure:focus {{ border-color: {C.ORANGE}; }}
    QPushButton#AiAction:disabled, QToolButton#AiMore:disabled {{ color: {C.TEXT_HINT}; }}
    QListWidget#AiCapabilityStatusList, QListWidget#AiMcpServerList {{
        background: {C.BG_PANEL}; border: none; outline: none;
        color: {C.TEXT_SECONDARY}; font-size: 12px;
    }}
    QListWidget#AiCapabilityStatusList::item, QListWidget#AiMcpServerList::item {{
        padding: 5px 2px; border-bottom: 1px solid {C.BG_ACTIVE};
    }}
"""


def _tr(key: str, zh: str, en: str) -> str:
    return I18N.tr(f"preferences.ai_editing.{key}", zh if I18N.get_current_locale().startswith("zh") else en)


def _preset_text(preset: AiProviderPreset) -> tuple[str, str]:
    key = f"preset_{preset.preset_id.replace('-', '_')}"
    return (
        _tr(f"{key}_label", preset.label, preset.label_en) if preset.label_en else preset.label,
        _tr(f"{key}_description", preset.description, preset.description_en)
        if preset.description_en else preset.description,
    )


def _label(text: str, style: str = "AiFieldLabel") -> QLabel:
    label = QLabel(text)
    label.setObjectName(style)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def _note(text: str = "") -> QLabel:
    label = _label(text, "AiNote")
    label.setWordWrap(True)
    return label


def _divider() -> QFrame:
    line = QFrame()
    line.setObjectName("AiDivider")
    line.setFixedHeight(1)
    return line


def _combo() -> ChevronComboBox:
    combo = ChevronComboBox()
    combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    combo.setSizeAdjustPolicy(ChevronComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setFixedHeight(28)
    return combo

logger = logging.getLogger(__name__)


class _ConnectionTestWorker(QThread):
    """AUD-19603：后台执行"测试连接"的阻塞 HTTP 请求。

    ``client.test_connection()`` 是同步 chat completion（默认 120s 超时 ×
    重试），此前在 UI 线程直接调用，最坏可冻结界面数分钟。结果经信号
    回 UI 线程（PyQt 的 lambda 连接代理生成于 connect() 所在线程，
    Auto 连接会排队回 UI 线程执行）。
    """

    completed = pyqtSignal(object, str)

    def __init__(
        self,
        profile: AiProviderProfile,
        api_key: str,
        operation: str,
        parent: QObject | None = None,
        *,
        model_pack_manager: Any | None = None,
    ) -> None:
        super().__init__(parent)
        self._profile = profile
        self._api_key = api_key
        self._operation = operation
        self._model_pack_manager = model_pack_manager

    def run(self) -> None:
        try:
            profile = replace(self._profile, limits=replace(
                self._profile.limits, max_retries=0,
            ))
            if self._operation == "local_pack":
                pack_id = ("qwen3_tts_customvoice" if profile.provider_kind == "local_qwen_tts" else
                           profile.model_id if profile.model_id in {"qwen3_asr", "qwen3_asr_large"} else "qwen3_asr")
                manager = self._model_pack_manager
                status = manager.get_status(pack_id) if manager is not None else None
                result: str | list[str] = (
                    _tr("pack_verified", "模型包已安装并通过校验。", "Model pack installed and verified.")
                    if status is not None and status.usable else
                    _tr("pack_missing", "模型包不可用：{pack}", "Model pack unavailable: {pack}").format(pack=pack_id)
                )
            elif profile.provider_kind == ZHIPU_VISION_PROVIDER_KIND:
                gateway = McpVisionGateway(
                    api_key_provider=lambda: self._api_key, request_timeout=profile.limits.request_timeout_seconds,
                )
                try:
                    result = gateway.test_vision()
                finally:
                    gateway.shutdown()
            elif self._operation == "models":
                result = MediaInferenceClient(profile, self._api_key).list_models()
            else:
                result = MediaInferenceClient(profile, self._api_key).test(self._operation)
        except Exception as exc:  # noqa: BLE001 - 错误信息原样回 UI 呈现
            error = str(exc)
            if self._api_key:
                error = error.replace(self._api_key, "••••")
            self.completed.emit(None, error[:400])
        else:
            self.completed.emit(result, "")


class AiEditingPreferencePage:
    """Preferences → AI Editing 页面。

    管理 AI provider profile 列表与字段编辑表单。``page_id`` 用于偏好系统注册。
    本页是普通 Python 类（非 QObject），profile 变更通过 ``on_profiles_changed``
    回调通知（与现有 ``EditPreferencePage`` 模式一致）。
    """

    page_id = "edit.ai_editing"

    def __init__(
        self,
        registry: ProfileRegistry | None = None,
        credential_store: CredentialStore | None = None,
        model_pack_manager: Any | None = None,
    ) -> None:
        self._registry = registry or ProfileRegistry()
        self._credential_store = credential_store or make_credential_store(
            get_credential_storage_preference()
        )
        self._widgets: dict[str, QWidget] = {}
        self._modality_widgets: dict[str, QCheckBox] = {}
        self._capability_widgets: dict[str, QCheckBox] = {}
        self._automation_widgets: dict[str, QCheckBox] = {}
        self._route_widgets: dict[str, ChevronComboBox] = {}
        self._credential_keychain_checkbox: QCheckBox | None = None
        self._config: Any | None = None
        self._model_pack_manager = model_pack_manager
        # 当前选中的 profile_id（编辑表单绑定到此）
        self._current_profile_id: str = ""
        self._selected_category = "language"
        # 标记 API key 是否在本会话被修改过（apply 时才落盘）
        self._pending_api_key: str | None = None
        self._draft_keys: dict[str, str] = {}
        self._original_secrets: dict[str, Callable[[], None]] = {}
        self._original_registry: ProfileRegistry | None = None
        self._original_storage: str | None = None
        self._credential_origins: dict[str, tuple[str, str]] = {}
        self._probe_version = 0
        self._probe_results: dict[tuple[str, str], str] = {}
        self._provided_credential_store = credential_store
        # AUD-19606：删除 profile 时延后到 apply() 才真正删盘的凭据引用
        # （与 registry.remove 同一提交点，未 Apply 前 key 不丢）。
        self._pending_secret_deletions: list[str] = []
        # AUD-19607：凭据"是否已存"按 ref 缓存，键入路径不再每次击穿读盘
        # （FileCredentialStore 每次调用都重新解析 ~/.topos/ai_profiles.json）。
        self._credential_available_cache: dict[str, bool] = {}
        # AUD-19603：进行中的后台连接测试 worker。
        self._test_worker: _ConnectionTestWorker | None = None
        # profile 变更回调（供 controller 重新绑定 planner）。
        self.on_profiles_changed: Callable[[], None] | None = None
        # Runtime-only connection verification results. They never contain a
        # secret and are cleared when a profile is edited/removed; profile
        # configuration alone must not be displayed as a reachable service.
        self._connection_results: dict[str, bool | str] = {}

    def set_model_pack_manager(self, manager: Any | None) -> None:
        """Bind the shared model-pack registry and refresh repair guidance."""
        self._model_pack_manager = manager
        self._refresh_capability_status()

    @property
    def registry(self) -> ProfileRegistry:
        return self._registry

    def select_model_category(self, category: str) -> None:
        tabs = self._widgets.get("category_tabs")
        if isinstance(tabs, QTabBar):
            for index, (key, _, _) in enumerate(_CATEGORY_OPTIONS):
                if key == category:
                    tabs.setCurrentIndex(index)
                    return

    # ------------------------------------------------------------------ #
    # ModularPreferencePage 接口
    # ------------------------------------------------------------------ #

    def create_widget(self, parent: QWidget | None = None) -> QWidget:
        page = QWidget(parent)
        page.setObjectName("AiPreferences")
        page.setStyleSheet(_PAGE_STYLE)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        tabs = QTabBar()
        tabs.setObjectName("AiPageTabs")
        tabs.setDrawBase(False)
        tabs.setExpanding(False)
        self._widgets["page_tabs"] = tabs
        tab_row = QHBoxLayout()
        tab_row.setContentsMargins(18, 0, 18, 0)
        tab_row.addWidget(tabs)
        tab_row.addStretch()
        layout.addLayout(tab_row)
        layout.addWidget(_divider())

        pages = QStackedWidget()
        for label, panel in (
            (_tr("tab_models", "模型配置", "Models"), self._build_models_section()),
            (_tr("tab_policy", "安全与自动化", "Security && automation"), self._build_policy_section()),
            (_tr("tab_status", "服务状态", "Service status"), self._build_environment_section()),
        ):
            tabs.addTab(label)
            scroll = QScrollArea()
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            panel.setObjectName("PrefPageContent")
            scroll.setWidget(panel)
            pages.addWidget(scroll)
        self._widgets["models_scroll"] = pages.widget(0)
        tabs.currentChanged.connect(pages.setCurrentIndex)
        layout.addWidget(pages, 1)
        self._refresh_profile_picker()
        return page

    def _build_models_section(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 13, 20, 4)
        layout.setSpacing(8)
        layout.addWidget(self._build_model_routes())
        layout.addSpacing(4)
        layout.addWidget(_divider())

        category_tabs = QTabBar()
        category_tabs.setObjectName("AiProviderCategoryTabs")
        category_tabs.setDrawBase(False)
        category_tabs.setExpanding(False)
        for category, zh, en in _CATEGORY_OPTIONS:
            category_tabs.addTab(_tr(f"category_{category}", zh, en))
        category_tabs.setCurrentIndex(next(
            index for index, (category, _, _) in enumerate(_CATEGORY_OPTIONS)
            if category == self._selected_category
        ))
        category_tabs.currentChanged.connect(self._on_category_changed)
        self._widgets["category_tabs"] = category_tabs

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        toolbar.addWidget(category_tabs)
        toolbar.addStretch()
        add_button = QPushButton(_tr("add_model", "＋ 新建模型", "+ Add model"))
        add_button.setObjectName("AiAddModel")
        add_button.setFixedHeight(28)
        add_button.clicked.connect(self._on_add_profile)
        self._widgets["add"] = add_button
        toolbar.addWidget(add_button)
        layout.addLayout(toolbar)

        picker = _combo()
        picker.setAccessibleName(_tr("configured_model", "已配置的模型", "Configured model"))
        picker.currentIndexChanged.connect(self._on_profile_selected)
        self._widgets["profile_picker"] = picker
        more = QToolButton()
        more.setObjectName("AiMore")
        more.setText("•••")
        more.setFixedSize(32, 28)
        more.setAccessibleName(_tr("model_actions", "模型操作", "Model actions"))
        more.setToolTip(more.accessibleName())
        menu = QMenu(more)
        menu.addAction(_tr("duplicate_model", "复制配置", "Duplicate configuration"), self._on_duplicate_profile)
        menu.addAction(_tr("remove_model", "删除配置", "Delete configuration"), self._on_remove_profile)
        more.setMenu(menu)
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._widgets["model_actions"] = more
        picker_row = QHBoxLayout()
        picker_row.setSpacing(6)
        picker_label = _label(_tr("edit_model_label", "编辑模型", "Edit model"))
        picker_label.setBuddy(picker)
        picker_row.addWidget(picker_label)
        picker_row.addWidget(picker, 1)
        picker_row.addWidget(more)
        picker_panel = QWidget()
        picker_row.setContentsMargins(0, 0, 0, 0)
        picker_panel.setLayout(picker_row)
        self._widgets["picker_panel"] = picker_panel
        layout.addWidget(picker_panel)

        empty = _note(_tr("models_empty", "此分类还没有模型。点击「新建模型」添加。",
                          "No models in this category. Select Add model to set one up."))
        self._widgets["profile_empty"] = empty
        layout.addWidget(empty)

        form = self._build_edit_form()
        self._widgets["profile_form"] = form
        layout.addWidget(form)
        layout.addStretch()
        return page

    def _build_model_routes(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(_label(_tr("model_routes", "默认模型", "Default models"), "AiSectionTitle"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        routes = (
            ("language", _tr("route_language", "剪辑助手", "Editing assistant")),
            ("vision", _tr("route_vision", "素材视觉打标", "Visual tagging")),
            ("asr", _tr("route_asr", "语音转录", "Transcription")),
            ("tts", _tr("route_tts", "剪辑配音", "Voiceover")),
            ("embedding", _tr("route_embedding", "素材语义搜索", "Library semantic search")),
        )
        for index, (capability, label) in enumerate(routes):
            combo = _combo()
            combo.currentIndexChanged.connect(
                lambda _index, cap=capability: self._on_route_changed(cap)
            )
            self._route_widgets[capability] = combo
            col = (index % 2) * 2
            field_label = _label(label)
            field_label.setBuddy(combo)
            combo.setAccessibleName(label)
            grid.addWidget(field_label, index // 2, col)
            grid.addWidget(combo, index // 2, col + 1)
            grid.setColumnStretch(col + 1, 1)
        layout.addLayout(grid)
        layout.addWidget(_note(_tr(
            "asset_binding_hint", "素材打标和转录需要在 AI 素材库中按目录启用。",
            "Enable tagging and transcription per folder in AI Library.",
        )))
        return panel

    def _refresh_routes(self) -> None:
        for capability, combo in self._route_widgets.items():
            combo.blockSignals(True)
            combo.clear()
            empty_label = (
                _tr("route_builtin", "使用本机默认", "Local default")
                if capability in {"asr", "tts"} else
                _tr("route_none", "未指定", "Unassigned")
            )
            combo.addItem(empty_label, "")
            active = self._registry.active_profile_for_capability(capability)
            for profile in self._registry.list():
                if capability not in profile.routed_capabilities:
                    continue
                if capability == "embedding" and profile != active and not MediaInferenceClient.supports(
                    profile.provider_kind, capability,
                ):
                    continue
                combo.addItem(profile.display_name, profile.profile_id)
            self._set_combo_data(combo, active.profile_id if active else "")
            combo.blockSignals(False)

    def _on_route_changed(self, capability: str) -> None:
        self._flush_pending_key()
        profile_id = self._route_widgets[capability].currentData()
        self._registry.set_active_for_capability(capability, str(profile_id or ""))
        self._refresh_profile_picker()

    def _build_environment_section(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 15, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(_label(_tr("mcp_servers", "MCP 服务", "MCP services"), "AiSectionTitle"))
        layout.addWidget(_note(_tr(
            "mcp_controls_hint", "勾选要提供给剪辑助手的 MCP 服务。",
            "Select the MCP services available to the editing assistant.",
        )))
        server_list = self._status_list("AiMcpServerList")
        server_list.itemChanged.connect(self._on_mcp_server_toggled)
        self._widgets["mcp_server_list"] = server_list
        layout.addWidget(server_list)
        layout.addWidget(_divider())
        layout.addWidget(_label(_tr("capabilities", "能力状态", "Capabilities"), "AiSectionTitle"))
        status_list = self._status_list("AiCapabilityStatusList")
        self._widgets["capability_status"] = status_list
        layout.addWidget(status_list)
        layout.addStretch()
        return page

    @staticmethod
    def _status_list(object_name: str) -> QListWidget:
        view = QListWidget()
        view.setObjectName(object_name)
        view.setUniformItemSizes(True)
        view.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return view

    @staticmethod
    def _fit_status_list(view: QListWidget) -> None:
        view.ensurePolished()
        # The status page scrolls as one surface, including both lists.
        view.setFixedHeight(max(0, view.sizeHintForRow(0)) * view.count() + 2 * view.frameWidth())

    def _refresh_mcp_section(self) -> None:
        """刷新 MCP 服务列表（从网关单例读取状态）。"""
        server_list = self._widgets.get("mcp_server_list")
        if not isinstance(server_list, QListWidget):
            return
        statuses: list = []
        try:
            from src.features.edit.ai_editing.mcp_vision_gateway import (
                get_mcp_vision_gateway,
            )
            statuses = get_mcp_vision_gateway().server_statuses()
        except Exception:  # noqa: BLE001 — 网关不可用时显示占位说明
            statuses = []
        server_list.blockSignals(True)
        server_list.clear()
        if not statuses:
            item = QListWidgetItem(
                I18N.tr("preferences.ai_editing.mcp_unavailable", "（MCP 网关不可用）"))
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)  # 不可勾选
            server_list.addItem(item)
        for st in statuses:
            n_tools = len(st.get("tools", ()))
            state = (_tr("mcp_ready", "可用", "Available") if st.get("available")
                     else _tr("mcp_unready", "不可用", "Unavailable"))
            label = (
                f"{st.get('name', '')}  ·  {state}  ·  "
                + _tr("mcp_tool_count", "{count} 个工具", "{count} tools").format(count=n_tools)
            )
            item = QListWidgetItem(label)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if st.get("enabled") else Qt.CheckState.Unchecked
            )
            # profile_id 放 UserRole，勾选回调据此启停。
            item.setData(Qt.ItemDataRole.UserRole, st.get("profile_id", ""))
            tooltip = (
                f"server: {st.get('server', '')}\n"
                f"作者: {st.get('author', '')}\n"
                f"传输: {st.get('transport', '')}\n"
                f"工具: {', '.join(st.get('tools', ()))}"
            )
            if st.get("reason"):
                tooltip += f"\n状态: {st['reason']}"
            item.setToolTip(tooltip)
            server_list.addItem(item)
        server_list.blockSignals(False)
        self._fit_status_list(server_list)

    def _on_mcp_server_toggled(self, item: QListWidgetItem) -> None:
        """用户勾选/取消一个 MCP server → 网关启停。"""
        profile_id = item.data(Qt.ItemDataRole.UserRole)
        if not profile_id:
            return
        enabled = item.checkState() == Qt.CheckState.Checked
        try:
            from src.features.edit.ai_editing.mcp_vision_gateway import (
                get_mcp_vision_gateway,
            )
            get_mcp_vision_gateway().set_server_enabled(profile_id, enabled)
        except Exception as e:  # noqa: BLE001 — UI 回调不抛
            logger.warning("MCP server toggle failed for %s: %s", profile_id, e)

    def _build_policy_section(self) -> QWidget:
        page = QWidget()
        form = QVBoxLayout(page)
        form.setContentsMargins(20, 15, 20, 16)
        form.setSpacing(8)
        form.addWidget(_label(_tr("key_storage_title", "密钥存储", "Credential storage"), "AiSectionTitle"))
        keychain_checkbox = self._create_credential_storage_checkbox()
        form.addWidget(keychain_checkbox)
        form.addWidget(_note(keychain_checkbox.toolTip()))
        form.addSpacing(6)
        form.addWidget(_divider())
        form.addSpacing(4)
        form.addWidget(_label(_tr("automation", "自动执行策略", "Automation"), "AiSectionTitle"))
        entries = (
            ("agent_auto_mode", "启用受限自动模式", "Restricted automatic mode",
             "关闭时，剪辑助手只生成建议和预览。", "When disabled, the assistant provides suggestions and previews."),
            ("cloud_upload", "允许云端素材上传", "Allow cloud uploads",
             "发起上传时仍需授权。", "Uploads still require authorization."),
            ("auto_export", "允许自动导出", "Allow automatic exports",
             "导出前仍会检查质量、版权和项目范围。", "Exports still require quality, rights, and project scope checks."),
        )
        for key, zh, en, help_zh, help_en in entries:
            checkbox = QCheckBox(_tr(f"flag.{key}", zh, en))
            checkbox.stateChanged.connect(self._on_automation_flag_changed)
            self._automation_widgets[key] = checkbox
            form.addWidget(checkbox)
            form.addWidget(_note(_tr(f"flag_hint.{key}", help_zh, help_en)))
            form.addSpacing(4)
        form.addStretch()
        return page

    def _create_credential_storage_checkbox(self) -> QCheckBox:
        """Create the global selector for where new keys are stored."""
        checkbox = QCheckBox(_tr("prefer_keychain", "新密钥优先存入系统钥匙串", "Prefer the system keychain for new keys"))
        checkbox.setMinimumHeight(24)
        checkbox.setToolTip(_tr("prefer_keychain_note",
            "未启用或钥匙串不可用时，新密钥会保存在用户的 AI 配置文件中。已保存的密钥仍可使用。",
            "New keys use the user AI configuration file when the system keychain is disabled or unavailable. Saved keys remain available."))
        checkbox.stateChanged.connect(self._on_keychain_pref_changed)
        self._credential_keychain_checkbox = checkbox
        return checkbox

    def _on_keychain_pref_changed(self, _state: int) -> None:
        """Sync the in-memory credential store to the checkbox immediately."""
        checked = (
            self._credential_keychain_checkbox is not None
            and self._credential_keychain_checkbox.isChecked()
        )
        location = CREDENTIAL_STORAGE_KEYCHAIN if checked else CREDENTIAL_STORAGE_FILE
        if self._provided_credential_store is None:
            self._credential_store = make_credential_store(location)
        # AUD-19607：存储后端切换后旧缓存全部失效。
        self._credential_available_cache.clear()
        self._refresh_capability_status()

    def _load_credential_pref(self) -> None:
        if self._credential_keychain_checkbox is None:
            return
        pref = get_credential_storage_preference()
        self._credential_keychain_checkbox.blockSignals(True)
        self._credential_keychain_checkbox.setChecked(pref == CREDENTIAL_STORAGE_KEYCHAIN)
        self._credential_keychain_checkbox.blockSignals(False)
        if self._provided_credential_store is None:
            self._credential_store = make_credential_store(pref)
        self._credential_available_cache.clear()

    def _save_credential_pref(self) -> None:
        if self._credential_keychain_checkbox is None:
            return
        checked = self._credential_keychain_checkbox.isChecked()
        location = CREDENTIAL_STORAGE_KEYCHAIN if checked else CREDENTIAL_STORAGE_FILE
        from src.features.edit.ai_editing.credential_store import (
            set_credential_storage_preference,
        )
        set_credential_storage_preference(location)
        self._refresh_capability_status()

    def _credential_secret_present(self, credential_ref: str) -> bool:
        """AUD-19607：按 ref 缓存"凭据是否已存"，避免逐键入读盘。

        缓存在写入/删除凭据或切换存储后端时失效（见各失效点）。
        """
        if credential_ref not in self._credential_available_cache:
            try:
                self._credential_available_cache[credential_ref] = bool(
                    self._credential_store.get_secret(credential_ref)
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to read credential %r", credential_ref, exc_info=True,
                )
                return False
        return self._credential_available_cache[credential_ref]

    def _refresh_capability_status(self) -> None:
        """刷新"当前状态"能力矩阵（凭据/连接/feature flag 综合判定）。

        被 16 处调用（profile 变更、能力勾选、凭据切换等都会触发）。原先这段
        逻辑被误内联进 ``_save_credential_pref``，导致独立调用点全部崩溃。
        """
        status_list = self._widgets.get("capability_status")
        if not isinstance(status_list, QListWidget):
            return
        profile = self._registry.active_profile
        credential_available = bool(
            profile is not None
            and profile.credential_ref
            and self._credential_secret_present(profile.credential_ref)
        )
        from src.features.edit.ai_editing.capability_status import build_capability_matrix

        statuses = build_capability_matrix(
            feature_flags={
                key: checkbox.isChecked()
                for key, checkbox in self._automation_widgets.items()
            },
            profile=profile,
            credential_available=credential_available,
            # The local scene-cut adapter is always registered by AnalysisService.
            analysis_provider_ids=("scene_cut_local",),
            model_pack_manager=self._model_pack_manager,
            connection_results=self._connection_results,
        )
        status_list.blockSignals(True)
        status_list.clear()
        for status in statuses:
            state = status.state.value
            state_text = _tr(f"status_{state}", *_STATUS_LABELS[state])
            item = QListWidgetItem(f"{status.label}  ·  {state_text}")
            item.setData(Qt.ItemDataRole.UserRole, status.to_dict())
            details = "\n".join(filter(None, (status.reason, status.next_step)))
            item.setToolTip(details)
            item.setData(Qt.ItemDataRole.AccessibleDescriptionRole, details)
            status_list.addItem(item)
        status_list.blockSignals(False)
        self._fit_status_list(status_list)
        # MCP 服务分区与能力状态一起刷新（同一"当前环境"视图）。
        self._refresh_mcp_section()

    # ------------------------------------------------------------------ #
    # 字段编辑表单
    # ------------------------------------------------------------------ #

    def _build_edit_form(self) -> QWidget:
        page = QWidget()
        form = QVBoxLayout(page)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(6)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        name_edit = QLineEdit()
        name_edit.setPlaceholderText(_tr("name_example", "例如：工作站千问", "e.g. Workstation Qwen"))
        name_edit.textChanged.connect(self._on_field_changed)
        self._widgets["display_name"] = name_edit
        self._add_field(grid, 0, 0, _tr("name_label", "显示名称", "Display name"), name_edit, span=3)

        source_combo = _combo()
        source_labels = {
            SOURCE_LOCAL: _tr("source_local", "本机模型", "Local model"),
            SOURCE_OLLAMA: _tr("source_ollama", "Ollama", "Ollama"),
            SOURCE_PRESET: _tr("source_preset", "第三方服务预设", "Provider preset"),
            SOURCE_CUSTOM: _tr("source_custom", "自定义 API", "Custom API"),
        }
        for _label_text, source in SOURCE_OPTIONS:
            source_combo.addItem(source_labels[source], source)
        source_combo.currentIndexChanged.connect(self._on_source_kind_changed)
        self._widgets["source_kind"] = source_combo
        self._add_field(grid, 1, 0, _tr("source_label", "接入方式", "Connection"), source_combo)

        preset_combo = _combo()
        self._refresh_preset_options(preset_combo)
        preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        self._widgets["preset_id"] = preset_combo
        self._widgets["preset_label"] = self._add_field(
            grid, 1, 2, _tr("preset_label", "服务预设", "Preset"), preset_combo,
        )

        kind_combo = _combo()
        for label, value, _url, _model in _PROVIDER_OPTIONS:
            kind_combo.addItem(label, value)
        kind_model = kind_combo.model()
        if isinstance(kind_model, QStandardItemModel):
            kind_model.item(kind_combo.findData(ZHIPU_VISION_PROVIDER_KIND)).setEnabled(False)
        kind_combo.currentIndexChanged.connect(self._on_provider_kind_changed)
        self._widgets["provider_kind"] = kind_combo
        self._widgets["protocol_label"] = self._add_field(
            grid, 1, 2, _tr("protocol_label", "协议", "Protocol"), kind_combo,
        )

        endpoint = QLineEdit()
        endpoint.setToolTip(_tr(
            "address_hint", "可填写 API 基础地址或完整接口地址。",
            "Enter an API base URL or the full endpoint address.",
        ))
        endpoint.textChanged.connect(self._on_field_changed)
        self._widgets["endpoint"] = endpoint
        self._widgets["endpoint_label"] = self._add_field(
            grid, 2, 0, _tr("address_label", "服务地址", "Service URL"), endpoint, span=3,
        )

        model = QLineEdit()
        model.textChanged.connect(self._on_field_changed)
        self._widgets["model_id"] = model
        discover = self._action_button(_tr("discover_models", "获取模型", "Get models"), self._on_discover_models)
        self._widgets["discover_models"] = discover
        model_row = self._field_with_action(model, discover)
        self._add_field(grid, 3, 0, _tr("model_label", "模型 ID", "Model ID"), model_row, span=3, buddy=model)

        key = QLineEdit()
        key.setEchoMode(QLineEdit.EchoMode.Password)
        key.textChanged.connect(self._on_key_changed)
        self._widgets["api_key"] = key
        remove_key = self._action_button(_tr("remove_key", "清除密钥", "Clear key"), self._on_remove_key)
        self._widgets["remove_key"] = remove_key
        key_row = self._field_with_action(key, remove_key)
        self._widgets["key_row"] = key_row
        self._widgets["key_label"] = self._add_field(grid, 4, 0, "API Key", key_row, span=3, buddy=key)
        key_status = _note()
        self._widgets["key_status"] = key_status
        grid.addWidget(key_status, 5, 1, 1, 3)
        form.addLayout(grid)
        for edit in (name_edit, endpoint, model, key):
            edit.setFixedHeight(28)

        upload = QCheckBox(_tr("profile_upload", "允许向此服务上传素材", "Allow media uploads to this service"))
        upload.setToolTip(_tr(
            "profile_upload_hint", "发起任务时仍需授权，AI 素材库按登记目录授权。",
            "Tasks still require authorization. AI Library grants access per registered folder.",
        ))
        upload.stateChanged.connect(self._on_modality_changed)
        self._widgets["allow_media_upload"] = upload

        advanced = self._build_advanced_form()
        advanced.hide()
        disclosure = QToolButton()
        disclosure.setObjectName("AiDisclosure")
        disclosure.setText(_tr("advanced_model", "高级设置", "Advanced settings"))
        disclosure.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        disclosure.setArrowType(Qt.ArrowType.RightArrow)
        disclosure.setCheckable(True)
        disclosure.setFixedHeight(28)
        disclosure.toggled.connect(advanced.setVisible)
        self._widgets["advanced_toggle"] = disclosure
        self._widgets["advanced_form"] = advanced
        disclosure.toggled.connect(self._on_advanced_toggled)

        test = self._action_button(_tr("test_model", "测试连接", "Test connection"), self._on_test_connection)
        test.setToolTip(_tr("probe_hint", "用文本或合成样本测试，服务商可能按请求计费。",
                            "Uses a prompt or synthetic sample. Provider usage charges may apply."))
        self._widgets["test_btn"] = test
        actions = QHBoxLayout()
        actions.setSpacing(6)
        actions.addWidget(upload)
        actions.addStretch()
        actions.addWidget(disclosure)
        actions.addWidget(test)
        form.addLayout(actions)
        probe_status = _note()
        self._widgets["probe_status"] = probe_status
        form.addWidget(probe_status)
        form.addWidget(advanced)
        return page

    @staticmethod
    def _add_field(
        layout: QGridLayout, row: int, col: int, text: str, widget: QWidget,
        *, span: int = 1, buddy: QWidget | None = None,
    ) -> QLabel:
        label = _label(text)
        control = widget if buddy is None else buddy
        label.setBuddy(control)
        control.setAccessibleName(text)
        layout.addWidget(label, row, col)
        layout.addWidget(widget, row, col + 1, 1, span)
        return label

    @staticmethod
    def _action_button(text: str, handler: Callable[[], None]) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("AiAction")
        button.setFixedHeight(28)
        button.clicked.connect(handler)
        return button

    @staticmethod
    def _field_with_action(edit: QLineEdit, button: QPushButton) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(edit, 1)
        layout.addWidget(button)
        return row

    def _build_advanced_form(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(_divider())

        limits = QGridLayout()
        limits.setHorizontalSpacing(10)
        limits.setColumnStretch(1, 1)
        limits.setColumnStretch(3, 1)
        context = QSpinBox()
        context.setRange(1, 2_000)
        context.setSuffix(_tr("context_suffix", "k", "k"))
        context.setValue(200)
        context.setFixedHeight(28)
        context.valueChanged.connect(self._on_context_limit_changed)
        self._widgets["context_limit"] = context
        self._add_field(limits, 0, 0, _tr("context_label", "上下文上限", "Context limit"), context)
        timeout = QSpinBox()
        timeout.setRange(5, 120)
        timeout.setSuffix(_tr("seconds_suffix", " 秒", " s"))
        timeout.setValue(120)
        timeout.setFixedHeight(28)
        timeout.setToolTip(_tr("timeout_hint", "模型冷启动较慢时，可以增加单次请求的等待时间。",
                               "Increase the per-request timeout if the model takes a while to start."))
        timeout.valueChanged.connect(self._on_context_limit_changed)
        self._widgets["request_timeout"] = timeout
        self._add_field(limits, 0, 2, _tr("field_timeout", "请求超时", "Request timeout"), timeout)
        layout.addLayout(limits)

        layout.addWidget(_label(_tr("route_capabilities", "用于分类和默认模型分配的能力", "Capabilities for model assignment"), "AiSectionTitle"))
        capabilities = QGridLayout()
        for index, (capability, zh, en) in enumerate(_CAPABILITY_OPTIONS):
            checkbox = QCheckBox(_tr(f"capability_{capability}", zh, en))
            checkbox.stateChanged.connect(self._on_capability_changed)
            self._capability_widgets[capability] = checkbox
            capabilities.addWidget(checkbox, index // 3, index % 3)
        layout.addLayout(capabilities)

        layout.addWidget(_label(_tr("media_capabilities", "请求与媒体能力", "Request & media capabilities"), "AiSectionTitle"))
        modalities = QGridLayout()
        for index, (value, zh, en) in enumerate((
            ("text", "文本 Agent", "Text agent"),
            ("tool_call", "工具调用", "Tool calling"),
            ("image", "图片输入", "Image input"),
            ("video", "视频输入", "Video input"),
            ("music", "音乐生成", "Music generation"),
            ("voice", "配音 / TTS", "Voiceover / TTS"),
        )):
            checkbox = QCheckBox(_tr(f"modality_{value}", zh, en))
            checkbox.stateChanged.connect(self._on_modality_changed)
            self._modality_widgets[value] = checkbox
            modalities.addWidget(checkbox, index // 3, index % 3)
        layout.addLayout(modalities)
        return page

    def _sync_connection_fields(self) -> None:
        source = self._widgets["source_kind"].currentData()  # type: ignore[union-attr]
        kind = self._widgets["provider_kind"].currentData()  # type: ignore[union-attr]
        preset = source in {SOURCE_PRESET, SOURCE_LOCAL}
        protocol = source == SOURCE_CUSTOM
        builtin = kind in _BUILTIN_PROTOCOLS
        vision_mcp = kind == ZHIPU_VISION_PROVIDER_KIND
        for name in ("preset_label", "preset_id"):
            self._widgets[name].setVisible(preset)
        for name in ("protocol_label", "provider_kind"):
            self._widgets[name].setVisible(protocol)
        for name in ("endpoint", "endpoint_label", "key_row", "key_label", "discover_models", "allow_media_upload"):
            self._widgets[name].setVisible(not builtin)
        self._widgets["discover_models"].setVisible(not builtin and not vision_mcp)
        self._widgets["endpoint"].setReadOnly(vision_mcp)  # type: ignore[union-attr]
        self._widgets["model_id"].setReadOnly(vision_mcp)  # type: ignore[union-attr]
        for checkbox in (*self._capability_widgets.values(), *self._modality_widgets.values()):
            checkbox.setEnabled(not vision_mcp)
        self._widgets["test_btn"].setText(
            _tr("verify_pack", "校验模型包", "Verify model pack") if builtin
            else _tr("test_model", "测试连接", "Test connection")
        )

    def _on_advanced_toggled(self, expanded: bool) -> None:
        toggle = self._widgets["advanced_toggle"]
        toggle.setArrowType(  # type: ignore[union-attr]
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        if expanded:
            self._reveal_model_control("context_limit")

    def _reveal_model_control(self, name: str) -> None:
        def reveal() -> None:
            scroll = self._widgets.get("models_scroll")
            target = self._widgets.get(name)
            if isinstance(scroll, QScrollArea) and target is not None and not sip.isdeleted(target) and target.isVisible():
                scroll.ensureWidgetVisible(target, 0, 12)

        # Let the scroll area lay out the newly shown row before locating it.
        QTimer.singleShot(0, reveal)

    # ------------------------------------------------------------------ #
    # load / apply / restore_defaults（修复持久化缺口）
    # ------------------------------------------------------------------ #

    def load(self, config: Any) -> None:
        """从 config 加载 profiles 到 registry 并刷新列表。

        ``config`` 是 config dict（ConfigManager.config 或等价 dict）；兼容传入
        ConfigManager（用 .get）。
        """
        self._config = config
        self._draft_keys.clear()
        self._pending_secret_deletions.clear()
        self._pending_api_key = None
        self._load_automation_flags(config)
        self._load_credential_pref()
        self._registry.load_from_config(config)
        # 用户级文件优先；旧 ConfigManager 数据会在首次保存时迁移过去。
        self._registry.load_from_user_file()
        self._remember_credential_origins()
        self._refresh_profile_picker()

    def apply(self, config_manager: Any) -> None:
        """把当前编辑表单写回 registry，再持久化到 config。

        先 flush 当前编辑中的 profile（用户可能在表单里改了字段没切列表），
        再调 ``registry.save_to_config``，并同步写入用户级 ``~/.topos`` 文件。
        """
        self._flush_current_profile()
        self._flush_pending_key()
        if self._original_registry is None:
            baseline = ProfileRegistry()
            baseline.load_from_config(self._config if self._config is not None else config_manager)
            baseline.load_from_user_file()
            self._original_registry = baseline
            self._original_storage = get_credential_storage_preference()
        previous_storage = get_credential_storage_preference()
        self._detach_changed_shared_keys()
        refs = self._key_changes()
        previous = {ref: self._capture_rollback(ref) for ref in refs}
        for ref, restore in previous.items():
            self._original_secrets.setdefault(ref, restore)
        changed: list[str] = []
        try:
            self._save_credential_pref()
            for ref, key in refs.items():
                changed.append(ref)
                self._write_key(self._credential_store, ref, key)
            self._registry.save_to_user_file()
        except (OSError, RuntimeError, ValueError):
            from src.features.edit.ai_editing.credential_store import set_credential_storage_preference

            if get_credential_storage_preference() != previous_storage:
                set_credential_storage_preference(previous_storage)
            for ref in reversed(changed):
                previous[ref]()
            raise
        self._registry.save_to_config(config_manager)
        self._save_automation_flags(config_manager)
        self._draft_keys.clear()
        self._pending_secret_deletions.clear()
        self._credential_available_cache.clear()
        self._remember_credential_origins()
        self._refresh_profile_picker()
        self._refresh_capability_status()
        if self.on_profiles_changed:
            self.on_profiles_changed()

    def _key_changes(self) -> dict[str, str | None]:
        live_refs = {
            str(p.credential_ref) for p in self._registry.list() if p.credential_ref
        }
        changes = {ref: None for ref in self._pending_secret_deletions if ref not in live_refs}
        for profile in self._registry.list():
            if profile.credential_ref and self._changed_credential_origin(profile) and sum(
                current.credential_ref == profile.credential_ref for current in self._registry.list()
            ) == 1:
                changes[profile.credential_ref] = None
        for profile_id, key in self._draft_keys.items():
            profile = self._registry.get(profile_id)
            if profile.credential_ref:
                changes[profile.credential_ref] = key or None
        return changes

    def _detach_changed_shared_keys(self) -> None:
        profiles = self._registry.list()
        for profile in profiles:
            if not self._changed_credential_origin(profile) or not any(
                other.profile_id != profile.profile_id and other.credential_ref == profile.credential_ref
                for other in profiles
            ):
                continue
            origin = self._credential_origin(profile)
            suffix = sha256(repr(origin).encode()).hexdigest()[:12]
            self._registry.add(replace(profile, credential_ref=make_credential_ref(f"{profile.profile_id}:{suffix}")))

    def _capture_rollback(self, ref: str) -> Callable[[], None]:
        store = self._credential_store
        if isinstance(store, HybridCredentialStore):
            return store.snapshot_for_rollback(ref)
        key = store.get_secret(ref)
        return lambda: self._write_key(store, ref, key)

    @staticmethod
    def _credential_origin(profile: AiProviderProfile) -> tuple[str, str]:
        try:
            address = urlsplit(profile.endpoint)
            return address.scheme, address.netloc.lower()
        except ValueError:
            return "", ""

    def _remember_credential_origins(self) -> None:
        self._credential_origins = {
            profile.profile_id: self._credential_origin(profile) for profile in self._registry.list()
        }

    def _changed_credential_origin(self, profile: AiProviderProfile) -> bool:
        original = self._credential_origins.get(profile.profile_id)
        return original is not None and original != self._credential_origin(profile)

    @staticmethod
    def _write_key(store: CredentialStore, ref: str, key: str | None) -> None:
        if key is None:
            store.delete_secret(ref)
        else:
            store.set_secret(ref, key)

    def rollback(self) -> None:
        if self._original_registry is None or self._original_storage is None:
            return
        from src.features.edit.ai_editing.credential_store import set_credential_storage_preference

        set_credential_storage_preference(self._original_storage)
        for restore in reversed(tuple(self._original_secrets.values())):
            restore()
        self._original_registry.save_to_user_file()
        self._original_registry = None
        self._original_secrets.clear()

    def restore_defaults(self, defaults: dict) -> None:
        """重置：清空所有 profile（保留用户已存的 key 不动）。"""
        reply = QMessageBox.question(
            None,
            I18N.tr("preferences.ai_editing.reset_title", "重置 AI 配置"),
            I18N.tr(
                "preferences.ai_editing.reset_confirm",
                "确定要删除所有 AI provider profile 吗？已保存的 API key 不会被清除。",
            ),
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        for p in list(self._registry.list()):
            self._registry.remove(p.profile_id)
        self._draft_keys.clear()
        self._current_profile_id = ""
        self._refresh_profile_picker()
        self._clear_form()
        self._refresh_capability_status()

    def _load_automation_flags(self, config: Any) -> None:
        raw = config.get("features") if hasattr(config, "get") else None
        if not isinstance(raw, dict):
            raw = {}
        from src.shared.feature_flags import DEFAULT_FEATURE_FLAGS

        for key, checkbox in self._automation_widgets.items():
            checkbox.blockSignals(True)
            checkbox.setChecked(bool(raw.get(key, DEFAULT_FEATURE_FLAGS.get(key, False))))
            checkbox.blockSignals(False)

    def _save_automation_flags(self, config_manager: Any) -> None:
        raw = config_manager.get("features") if hasattr(config_manager, "get") else None
        features = dict(raw) if isinstance(raw, dict) else {}
        for key, checkbox in self._automation_widgets.items():
            features[key] = checkbox.isChecked()
        if hasattr(config_manager, "set"):
            config_manager.set("features", features, auto_save=False)

    # ------------------------------------------------------------------ #
    # 内部：profile 列表
    # ------------------------------------------------------------------ #

    def _update_current_picker_item(self) -> None:
        picker = self._widgets.get("profile_picker")
        if not isinstance(picker, ChevronComboBox):
            return
        profile = self._registry.get(self._current_profile_id)
        if profile is not None:
            row = picker.findData(profile.profile_id)
            if row >= 0:
                picker.setItemText(row, profile.display_name)
                picker.setItemData(row, profile.model_id, Qt.ItemDataRole.ToolTipRole)

    def _refresh_profile_picker(self) -> None:
        picker = self._widgets.get("profile_picker")
        if not isinstance(picker, ChevronComboBox):
            return
        wanted = _CATEGORY_CAPABILITIES.get(self._selected_category)
        visible_profiles = [
            profile for profile in self._registry.list()
            if (wanted.intersection(profile.routed_capabilities) if wanted is not None
                else category_for_profile(profile) == "other")
        ]
        picker.blockSignals(True)
        picker.clear()
        for profile in visible_profiles:
            picker.addItem(profile.display_name, profile.profile_id)
            picker.setItemData(picker.count() - 1, profile.model_id, Qt.ItemDataRole.ToolTipRole)
        row = picker.findData(self._current_profile_id)
        picker.setCurrentIndex(max(0, row) if visible_profiles else -1)
        picker.blockSignals(False)

        self._current_profile_id = str(picker.currentData() or "")
        if self._current_profile_id:
            self._load_profile_to_form(self._current_profile_id)
        else:
            self._clear_form()
        self._widgets["profile_empty"].setVisible(not visible_profiles)
        for name in ("picker_panel", "profile_form"):
            self._widgets[name].setVisible(bool(visible_profiles))
        self._refresh_routes()
        self._refresh_capability_status()

    def _on_profile_selected(self, _index: int) -> None:
        picker = self._widgets.get("profile_picker")
        if not isinstance(picker, ChevronComboBox) or not picker.currentData():
            return
        self._flush_current_profile()
        self._flush_pending_key()
        self._current_profile_id = str(picker.currentData())
        self._load_profile_to_form(self._current_profile_id)

    def _flush_pending_key(self) -> None:
        key = self._pending_api_key
        self._pending_api_key = None
        if key and self._current_profile_id:
            self._draft_keys[self._current_profile_id] = key

    # ------------------------------------------------------------------ #
    # 内部：表单 ↔ profile 同步
    # ------------------------------------------------------------------ #

    def _load_profile_to_form(self, profile_id: str) -> None:
        """把指定 profile 的字段填入编辑表单。"""
        profile = self._registry.get(profile_id)
        if profile is None:
            self._clear_form()
            return
        self._block_form_signals(True)
        name_edit: QLineEdit = self._widgets["display_name"]  # type: ignore[assignment]
        kind_combo: ChevronComboBox = self._widgets["provider_kind"]  # type: ignore[assignment]
        source_combo: ChevronComboBox = self._widgets["source_kind"]  # type: ignore[assignment]
        preset_combo: ChevronComboBox = self._widgets["preset_id"]  # type: ignore[assignment]
        endpoint_edit: QLineEdit = self._widgets["endpoint"]  # type: ignore[assignment]
        model_edit: QLineEdit = self._widgets["model_id"]  # type: ignore[assignment]
        context_spin: QSpinBox = self._widgets["context_limit"]  # type: ignore[assignment]
        key_edit: QLineEdit = self._widgets["api_key"]  # type: ignore[assignment]

        name_edit.setText(profile.display_name)
        self._set_combo_data(source_combo, profile.source_kind or self._infer_source_kind(profile))
        self._refresh_preset_options(preset_combo)
        if profile.preset_id and preset_combo.findData(profile.preset_id) < 0:
            preset = get_preset(profile.preset_id)
            label, description = _preset_text(preset) if preset else (profile.preset_id, "")
            preset_combo.addItem(label, profile.preset_id)
            preset_combo.setItemData(preset_combo.count() - 1, description, Qt.ItemDataRole.ToolTipRole)
        self._set_combo_data(preset_combo, profile.preset_id)
        # 选中对应 provider_kind
        if kind_combo.findData(profile.provider_kind) < 0:
            kind_combo.addItem(profile.provider_kind or "OpenAI-compatible", profile.provider_kind)
        self._set_combo_data(kind_combo, profile.provider_kind)
        endpoint_edit.setText(profile.endpoint)
        model_edit.setText(profile.model_id)
        context_spin.setValue(max(1, min(2_000, round(profile.limits.max_context_tokens / 1_000))))
        self._widgets["request_timeout"].setValue(max(5, min(120, round(profile.limits.request_timeout_seconds))))  # type: ignore[union-attr]
        key_edit.setText(self._draft_keys.get(profile_id, ""))
        for modality, checkbox in self._modality_widgets.items():
            checkbox.setChecked(modality in profile.modalities)
        capabilities = set(profile.routed_capabilities)
        for capability, checkbox in self._capability_widgets.items():
            checkbox.setChecked(capability in capabilities)
        upload_checkbox = self._widgets.get("allow_media_upload")
        if isinstance(upload_checkbox, QCheckBox):
            upload_checkbox.setChecked(profile.data_policy.allow_media_upload)
        self._pending_api_key = None
        self._block_form_signals(False)

        # 更新 key 状态 + placeholder（按 provider 类型给提示）
        self._update_key_status(profile)
        self._update_placeholders(profile.provider_kind)
        self._sync_connection_fields()
        self._refresh_probe_status()

    def _clear_form(self) -> None:
        self._block_form_signals(True)
        self._widgets["display_name"].setText("")  # type: ignore[union-attr]
        self._set_combo_data(self._widgets["source_kind"], SOURCE_CUSTOM)  # type: ignore[arg-type]
        self._set_combo_data(self._widgets["preset_id"], "")  # type: ignore[arg-type]
        self._set_combo_data(self._widgets["provider_kind"], "openai")  # type: ignore[arg-type]
        self._widgets["endpoint"].setText("")  # type: ignore[union-attr]
        self._widgets["model_id"].setText("")  # type: ignore[union-attr]
        self._widgets["context_limit"].setValue(200)  # type: ignore[union-attr]
        self._widgets["request_timeout"].setValue(120)  # type: ignore[union-attr]
        self._widgets["api_key"].setText("")  # type: ignore[union-attr]
        for checkbox in self._modality_widgets.values():
            checkbox.setChecked(False)
        for checkbox in self._capability_widgets.values():
            checkbox.setChecked(False)
        upload_checkbox = self._widgets.get("allow_media_upload")
        if isinstance(upload_checkbox, QCheckBox):
            upload_checkbox.setChecked(False)
        self._block_form_signals(False)
        self._pending_api_key = None

    def _block_form_signals(self, blocked: bool) -> None:
        for key in (
            "display_name", "source_kind", "preset_id", "provider_kind", "endpoint", "model_id",
            "context_limit", "request_timeout", "api_key",
        ):
            w = self._widgets.get(key)
            if w is not None:
                w.blockSignals(blocked)
        for checkbox in self._modality_widgets.values():
            checkbox.blockSignals(blocked)
        for checkbox in self._capability_widgets.values():
            checkbox.blockSignals(blocked)
        upload_checkbox = self._widgets.get("allow_media_upload")
        if upload_checkbox is not None:
            upload_checkbox.blockSignals(blocked)

    def _on_field_changed(self) -> None:
        """表单字段变化 → 实时更新当前 profile（frozen，整体替换）。

        AUD-19607：键入路径只 flush registry 并就地更新列表行标签；
        不再整表重建 QListWidget（原路径每次击键两次全量列表重建 +
        两次凭据读盘，IME 组字期被成倍放大）。
        """
        self._flush_current_profile()
        self._update_current_picker_item()

    def _on_provider_kind_changed(self) -> None:
        """provider 类型变化 → 更新 placeholder + flush。"""
        kind_combo: ChevronComboBox = self._widgets["provider_kind"]  # type: ignore[assignment]
        kind = kind_combo.currentData()
        self._update_placeholders(kind)
        self._sync_connection_fields()
        self._flush_current_profile()
        self._update_current_picker_item()

    def _on_category_changed(self, index: int) -> None:
        self._flush_current_profile()
        self._flush_pending_key()
        if 0 <= index < len(_CATEGORY_OPTIONS):
            self._selected_category = _CATEGORY_OPTIONS[index][0]
        self._refresh_preset_options()
        self._refresh_profile_picker()

    def _refresh_preset_options(self, combo: ChevronComboBox | None = None) -> None:
        """只显示当前能力分类对应的预设，避免跨分类混在同一个下拉框。"""
        if combo is None:
            widget = self._widgets.get("preset_id")
            if not isinstance(widget, ChevronComboBox):
                return
            combo = widget

        current_id = str(combo.currentData() or "")
        # Loading a profile already blocks form signals. Keep that block in
        # place so selecting its saved preset cannot reapply the template.
        with QSignalBlocker(combo):
            combo.clear()
            combo.addItem(_tr("select_preset", "选择预设", "Select a preset"), "")
            for preset in presets_for_category(self._selected_category):
                label, description = _preset_text(preset)
                combo.addItem(label, preset.preset_id)
                combo.setItemData(combo.count() - 1, description, Qt.ItemDataRole.ToolTipRole)
            self._set_combo_data(combo, current_id)
            if combo.currentIndex() < 0:
                combo.setCurrentIndex(0)

    def _on_source_kind_changed(self) -> None:
        source = self._widgets["source_kind"].currentData()  # type: ignore[union-attr]
        kind = self._widgets["provider_kind"]
        endpoint = self._widgets["endpoint"]
        model = self._widgets["model_id"]
        if source == SOURCE_OLLAMA:
            old = self._registry.get(self._current_profile_id)
            preset = get_preset(old.preset_id) if old else None
            self._block_form_signals(True)
            self._set_combo_data(self._widgets["preset_id"], "")
            self._set_combo_data(kind, "ollama")
            endpoint_value = endpoint.text().strip()  # type: ignore[union-attr]
            if not endpoint_value or (preset is not None and endpoint_value == preset.endpoint):
                ollama_embedding = get_preset("ollama-embedding") if self._selected_category == "embedding" else None
                endpoint.setText(ollama_embedding.endpoint if ollama_embedding else "http://127.0.0.1:11434")  # type: ignore[union-attr]
            if preset is not None and model.text().strip() == preset.model_id:  # type: ignore[union-attr]
                model.clear()  # type: ignore[union-attr]
            self._block_form_signals(False)
            self._update_placeholders("ollama")
        elif source == SOURCE_LOCAL:
            if self._selected_category == "speech":
                capability = self._registry.get(self._current_profile_id)
                preset_id = ("local-qwen-tts" if capability is not None
                             and "tts" in capability.routed_capabilities else "local-qwen-asr")
                preset = get_preset(preset_id)
                if preset is not None:
                    self._set_combo_data(self._widgets["preset_id"], preset.preset_id)
            else:
                self._set_combo_data(kind, "local_http")
        self._sync_connection_fields()
        self._flush_current_profile()
        self._update_current_picker_item()

    def _on_preset_changed(self) -> None:
        combo: ChevronComboBox = self._widgets["preset_id"]  # type: ignore[assignment]
        preset = get_preset(str(combo.currentData() or ""))
        if preset is None:
            self._flush_current_profile()
            return
        self._flush_pending_key()
        self._apply_preset(preset)
        self._flush_current_profile()
        self._refresh_profile_picker()

    def _on_capability_changed(self, _value: int) -> None:
        self._flush_current_profile()
        self._update_current_picker_item()

    @staticmethod
    def _set_combo_data(combo: QWidget, value: str) -> None:
        """Set a combo value without coupling the page to QComboBox methods."""
        if not isinstance(combo, ChevronComboBox):
            return
        for index in range(combo.count()):
            if combo.itemData(index) == value:
                combo.setCurrentIndex(index)
                return

    @staticmethod
    def _infer_source_kind(profile: AiProviderProfile) -> str:
        kind = (profile.provider_kind or "").lower()
        if kind.startswith("local_"):
            return SOURCE_LOCAL
        if kind == "ollama":
            return SOURCE_OLLAMA
        return SOURCE_CUSTOM

    def _apply_preset(self, preset: AiProviderPreset) -> None:
        """Copy non-secret preset fields into the current editor form."""
        self._block_form_signals(True)
        self._set_combo_data(self._widgets["source_kind"], preset.source_kind)
        self._set_combo_data(self._widgets["provider_kind"], preset.provider_kind)
        self._widgets["endpoint"].setText(preset.endpoint)  # type: ignore[union-attr]
        self._widgets["model_id"].setText(preset.model_id)  # type: ignore[union-attr]
        for capability, checkbox in self._capability_widgets.items():
            checkbox.setChecked(capability in preset.capabilities or (
                capability == "vision" and "vision_understanding" in preset.capabilities))
        for modality, checkbox in self._modality_widgets.items():
            checkbox.setChecked(modality in preset.modalities)
        self._widgets["allow_media_upload"].setChecked(False)  # type: ignore[union-attr]
        old = self._registry.get(self._current_profile_id)
        if old is not None:
            self._registry.add(replace(old, modalities=preset.modalities, capabilities=preset.capabilities))
        self._block_form_signals(False)
        self._update_placeholders(preset.provider_kind)

    def _on_context_limit_changed(self, _value: int) -> None:
        """上下文上限变化 → 写回当前 Profile 的 limits。"""
        self._flush_current_profile()

    def _on_modality_changed(self, _value: int) -> None:
        self._flush_current_profile()

    def _on_automation_flag_changed(self, _value: int) -> None:
        self._refresh_capability_status()

    def _on_key_changed(self, text: str) -> None:
        """API key 输入 → 暂存到 _pending_api_key（apply 时才落盘）。"""
        self._pending_api_key = text
        self._invalidate_probe(self._current_profile_id)
        profile = self._registry.get(self._current_profile_id)
        if profile is not None:
            self._update_key_status(profile)

    def _on_remove_key(self) -> None:
        profile = self._registry.get(self._current_profile_id)
        if profile is None:
            return
        self._widgets["api_key"].clear()  # type: ignore[union-attr]
        self._draft_keys[profile.profile_id] = ""
        self._invalidate_probe(profile.profile_id)
        self._update_key_status(profile)

    def _flush_current_profile(self) -> None:
        """把表单当前值写回 registry 中的 profile（frozen 整体替换）。"""
        if not self._current_profile_id:
            return
        old = self._registry.get(self._current_profile_id)
        if old is None:
            return
        kind_combo: ChevronComboBox = self._widgets["provider_kind"]  # type: ignore[assignment]
        source_combo: ChevronComboBox = self._widgets["source_kind"]  # type: ignore[assignment]
        preset_combo: ChevronComboBox = self._widgets["preset_id"]  # type: ignore[assignment]
        name_edit: QLineEdit = self._widgets["display_name"]  # type: ignore[assignment]
        endpoint_edit: QLineEdit = self._widgets["endpoint"]  # type: ignore[assignment]
        model_edit: QLineEdit = self._widgets["model_id"]  # type: ignore[assignment]
        context_spin: QSpinBox = self._widgets["context_limit"]  # type: ignore[assignment]
        timeout_spin: QSpinBox = self._widgets["request_timeout"]  # type: ignore[assignment]
        modalities = [value for value in old.modalities if value not in self._modality_widgets]
        modalities.extend(value for value, checkbox in self._modality_widgets.items() if checkbox.isChecked())
        upload_checkbox = self._widgets.get("allow_media_upload")
        allow_upload = isinstance(upload_checkbox, QCheckBox) and upload_checkbox.isChecked()
        if allow_upload and "media_upload" not in modalities:
            modalities.append("media_upload")
        custom_capabilities = [value for value in old.routed_capabilities
                               if value not in self._capability_widgets and value != "service"]
        capabilities = tuple(custom_capabilities) + tuple(
            "vision_understanding" if capability == "vision" and "vision_understanding" in old.capabilities else capability
            for capability, checkbox in self._capability_widgets.items()
            if checkbox.isChecked()
        )
        source_kind = source_combo.currentData() or old.source_kind or SOURCE_CUSTOM
        endpoint = endpoint_edit.text().strip()
        model_id = model_edit.text().strip()
        provider_kind = str(kind_combo.currentData() or old.provider_kind)
        if {"vision", "vision_understanding"}.intersection(capabilities) and "image" not in modalities and "vision" not in old.routed_capabilities:
            modalities.append("image")
        context = old.limits.max_context_tokens
        if context_spin.value() != max(1, min(2_000, round(context / 1_000))):
            context = context_spin.value() * 1_000
        new = AiProviderProfile(
            profile_id=old.profile_id,
            display_name=name_edit.text().strip() or old.display_name,
            provider_kind=provider_kind,
            source_kind=source_kind,
            preset_id=str(preset_combo.currentData() or ""),
            endpoint=endpoint,
            model_id=model_id,
            credential_ref=old.credential_ref or make_credential_ref(old.profile_id),
            modalities=tuple(modalities),
            capabilities=capabilities or ("service",),
            data_policy=DataPolicy(
                execution=self._execution_for(provider_kind, endpoint, old.data_policy.execution),
                allow_media_upload=allow_upload,
                retain_prompts=old.data_policy.retain_prompts,
            ),
            limits=ProviderLimits(
                max_context_tokens=context,
                max_parallel_jobs=old.limits.max_parallel_jobs,
                monthly_cost_alert=old.limits.monthly_cost_alert,
                request_timeout_seconds=(
                    old.limits.request_timeout_seconds if timeout_spin.value()
                    == max(5, min(120, round(old.limits.request_timeout_seconds))) else float(timeout_spin.value())
                ),
                max_retries=old.limits.max_retries,
                max_cost_units_per_request=old.limits.max_cost_units_per_request,
            ),
        )
        if new != old:
            self._registry.add(new)
            if replace(new, display_name=old.display_name) != old:
                self._invalidate_probe(old.profile_id)
            if self._credential_origin(new) != self._credential_origin(old):
                self._pending_api_key = None
                self._draft_keys.pop(old.profile_id, None)
                key_edit = self._widgets["api_key"]
                key_edit.blockSignals(True)
                key_edit.clear()  # type: ignore[union-attr]
                key_edit.blockSignals(False)
                self._update_key_status(new)
            self._refresh_routes()
            self._refresh_capability_status()

    @staticmethod
    def _execution_for(kind: str, endpoint: str, current: str) -> str:
        if kind in _BUILTIN_PROTOCOLS:
            return "local"
        try:
            _, loopback = validate_media_endpoint(endpoint)
        except MediaInferenceConfigurationError:
            return current
        return "local" if loopback else "cloud"

    # ------------------------------------------------------------------ #
    # 内部：UI 辅助
    # ------------------------------------------------------------------ #

    def _update_key_status(self, profile: AiProviderProfile) -> None:
        field = self._widgets["api_key"]
        status = self._widgets["key_status"]
        clear = self._widgets["remove_key"]
        changed_origin = self._changed_credential_origin(profile)
        saved = bool(
            not changed_origin and profile.credential_ref
            and self._credential_secret_present(profile.credential_ref)
        )
        pending = self._pending_api_key or self._draft_keys.get(profile.profile_id)
        message = ""
        if pending is not None:
            message = (
                _tr("key_pending", "保存后更新密钥", "Key will update on save") if pending
                else _tr("key_clear_pending", "保存后清除密钥", "Key will be cleared on save")
            )
        elif changed_origin:
            message = _tr(
                "key_endpoint_changed", "服务地址已变更。需要时请为新地址输入密钥。",
                "Service address changed. Enter a key for the new address if needed.",
            )
        state = (
            _tr("key_saved_short", "已保存", "Saved") if saved else
            _tr("key_not_set", "未设置", "Not set")
        )
        field.setPlaceholderText(  # type: ignore[union-attr]
            _tr("key_masked", "已保存（不会显示，输入新值可替换）", "Saved (hidden). Enter a new key to replace it.")
            if saved and pending != "" else
            _tr("key_enter", "按服务要求填写", "Enter a key if required")
        )
        status.setText(message or state)  # type: ignore[union-attr]
        status.setVisible(bool(message) and profile.provider_kind not in _BUILTIN_PROTOCOLS)
        clear.setVisible(bool(pending) or (saved and pending is None))

    def _update_placeholders(self, provider_kind: str) -> None:
        """按 provider 类型更新 endpoint/model 的 placeholder 提示。"""
        for _label, value, url, model in _PROVIDER_OPTIONS:
            if value == provider_kind:
                self._widgets["endpoint"].setPlaceholderText(url)  # type: ignore[union-attr]
                self._widgets["model_id"].setPlaceholderText(model)  # type: ignore[union-attr]
                return
        # Custom protocols still receive useful generic hints.
        self._widgets["endpoint"].setPlaceholderText(
            I18N.tr("preferences.ai_editing.base_url_placeholder", "Base URL 或完整接口地址"))  # type: ignore[union-attr]
        self._widgets["model_id"].setPlaceholderText(
            I18N.tr("preferences.ai_editing.model_name_placeholder", "model-name"))  # type: ignore[union-attr]

    # ------------------------------------------------------------------ #
    # 操作按钮
    # ------------------------------------------------------------------ #

    def _on_add_profile(self) -> None:
        import uuid as _uuid

        profile_id = f"profile-{_uuid.uuid4().hex[:8]}"
        preset = next(iter(presets_for_category(self._selected_category)), None)
        # New language profiles default to a well-known OpenAI-compatible
        # preset; other tabs default to the first relevant built-in template.
        provider_kind = preset.provider_kind if preset else "openai"
        profile = AiProviderProfile(
            profile_id=profile_id,
            display_name=_tr("new_model", "新模型", "New model"),
            provider_kind=provider_kind,
            endpoint=preset.endpoint if preset else "",
            model_id=preset.model_id if preset else "",
            credential_ref=make_credential_ref(profile_id),
            source_kind=preset.source_kind if preset else SOURCE_CUSTOM,
            preset_id=preset.preset_id if preset else "",
            modalities=preset.modalities if preset else ("text", "tool_call"),
            capabilities=preset.capabilities if preset else ("service",),
        )
        self._registry.add(profile)
        # AUD-19605：切到新 profile 前保存旧 profile 输入中的 key。
        self._flush_pending_key()
        self._current_profile_id = profile_id
        self._refresh_profile_picker()
        self._widgets["display_name"].setFocus()
        self._widgets["display_name"].selectAll()  # type: ignore[union-attr]

    def _on_duplicate_profile(self) -> None:
        if not self._current_profile_id:
            return
        src = self._registry.get(self._current_profile_id)
        if src is None:
            return
        import uuid as _uuid

        new_id = f"profile-{_uuid.uuid4().hex[:8]}"
        dup = AiProviderProfile(
            profile_id=new_id,
            display_name=_tr("model_copy", "{name}（副本）", "{name} (copy)").format(name=src.display_name),
            provider_kind=src.provider_kind,
            endpoint=src.endpoint,
            model_id=src.model_id,
            credential_ref=make_credential_ref(new_id),
            modalities=src.modalities,
            source_kind=src.source_kind,
            preset_id=src.preset_id,
            capabilities=src.capabilities,
            data_policy=src.data_policy,
            limits=src.limits,
        )
        self._registry.add(dup)
        # AUD-19605：切到副本前保存原 profile 输入中的 key。
        self._flush_pending_key()
        self._current_profile_id = new_id
        self._refresh_profile_picker()
        self._widgets["display_name"].setFocus()
        self._widgets["display_name"].selectAll()  # type: ignore[union-attr]

    def _on_remove_profile(self) -> None:
        profile_id = self._current_profile_id
        profile = self._registry.get(profile_id)
        if profile is None:
            return
        # AUD-19606：凭据删除延后到 apply()（与 registry.remove 同一提交点）。
        # 此前 delete_secret 在点击删除时立即写盘且不可逆——用户删除后
        # 不 Apply / 取消，profile 仍在而已存 key 永久丢失。
        if profile.credential_ref:
            self._pending_secret_deletions.append(str(profile.credential_ref))
        self._draft_keys.pop(str(profile_id), None)
        self._pending_api_key = None
        self._registry.remove(profile_id)
        self._invalidate_probe(str(profile_id))
        self._current_profile_id = ""
        self._refresh_profile_picker()

    def _api_key_for(self, profile: AiProviderProfile) -> str:
        if self._pending_api_key:
            return self._pending_api_key.strip()
        if profile.profile_id in self._draft_keys:
            return self._draft_keys[profile.profile_id].strip()
        if self._changed_credential_origin(profile):
            return ""
        return self._credential_store.get_secret(profile.credential_ref) or ""

    def _invalidate_probe(self, profile_id: str) -> None:
        self._probe_version += 1
        self._connection_results.pop(profile_id, None)
        self._probe_results = {
            key: result for key, result in self._probe_results.items() if key[0] != profile_id
        }
        self._refresh_probe_status()

    def _refresh_probe_status(self) -> None:
        label = self._widgets.get("probe_status")
        if not isinstance(label, QLabel):
            return
        names = {cap: _tr(f"capability_{cap}", zh, en) for cap, zh, en in _CAPABILITY_OPTIONS}
        results = [f"{names.get(capability, capability)} · {value}"
                   for (profile_id, capability), value in self._probe_results.items()
                   if profile_id == self._current_profile_id]
        label.setText("\n".join(results))
        label.setVisible(bool(results))

    def _on_test_connection(self) -> None:
        self._flush_current_profile()
        profile = self._registry.get(self._current_profile_id)
        if profile is None or self._test_worker is not None:
            return
        available = [cap for cap in _PROBED_CAPABILITIES if cap in profile.routed_capabilities]
        relevant = _CATEGORY_CAPABILITIES.get(self._selected_category, set())
        operations = [cap for cap in available if cap in relevant] or available
        if profile.provider_kind == ZHIPU_VISION_PROVIDER_KIND:
            self._start_probe(profile, "vision")
        elif profile.provider_kind in _BUILTIN_PROTOCOLS:
            self._start_probe(profile, "local_pack")
        elif profile.source_kind == SOURCE_LOCAL and profile.provider_kind not in {"local_http", "openai_compat"}:
            QMessageBox.information(None, "Topos", _tr("legacy_local_check",
                "此配置没有声明已接入的本地模型协议，不会将其标记为已通过连接测试。",
                "This profile has no supported local protocol, so it remains unverified."))
        elif not operations:
            self._set_probe_status(profile.profile_id, "service", _tr("probe_use_action",
                "在对应生成或合成操作中验证此服务。", "Verify this service with its generation or synthesis action."))
        elif len(operations) == 1:
            self._start_probe(profile, operations[0])
        else:
            menu = QMenu(self._widgets["test_btn"])
            names = {cap: _tr(f"capability_{cap}", zh, en) for cap, zh, en in _CAPABILITY_OPTIONS}
            actions = {menu.addAction(names[cap]): cap for cap in operations}
            button = self._widgets["test_btn"]
            selected = menu.exec(button.mapToGlobal(button.rect().bottomLeft()))
            if selected in actions:
                self._start_probe(profile, actions[selected])

    def _on_discover_models(self) -> None:
        self._flush_current_profile()
        profile = self._registry.get(self._current_profile_id)
        if profile is not None and profile.provider_kind != ZHIPU_VISION_PROVIDER_KIND:
            self._start_probe(profile, "models")

    def _start_probe(self, profile: AiProviderProfile, operation: str) -> None:
        if self._test_worker is not None:
            return
        try:
            key = self._api_key_for(profile) if operation != "local_pack" else ""
        except (OSError, RuntimeError, ValueError):
            self._set_probe_status(profile.profile_id, operation, _tr("key_read_failed",
                "无法读取 API 密钥，检查密钥存储设置。", "Unable to read the API key. Check the key storage settings."))
            return
        if operation != "local_pack":
            if profile.provider_kind == ZHIPU_VISION_PROVIDER_KIND and (
                profile.endpoint != ZHIPU_VISION_ORIGIN or profile.model_id != ZHIPU_VISION_TOOL
            ):
                self._set_probe_status(profile.profile_id, operation, _tr(
                    "mcp_vision_invalid_profile", "请选择智谱视觉 MCP 内置预设。",
                    "Select the built-in Zhipu Vision MCP preset.",
                ))
                return
            if not profile.endpoint or (operation != "models" and not profile.model_id):
                self._set_probe_status(profile.profile_id, operation, _tr("probe_required",
                    "填写服务地址，以及测试所需的模型 ID。", "Enter a service address and the model ID if required."))
                return
            try:
                validate_media_endpoint(profile.endpoint, key)
            except MediaInferenceConfigurationError as exc:
                self._set_probe_status(profile.profile_id, operation, str(exc))
                return
        version = self._probe_version
        self._widgets["test_btn"].setEnabled(False)
        self._widgets["discover_models"].setEnabled(False)
        self._set_probe_status(profile.profile_id, operation, _tr("probe_working", "正在验证…", "Checking…"))
        worker = _ConnectionTestWorker(profile, key, operation, model_pack_manager=self._model_pack_manager)
        worker.completed.connect(lambda result, error: self._probe_finished(
            profile.profile_id, operation, version, result, error,
        ))
        worker.finished.connect(worker.deleteLater)
        self._test_worker = worker
        worker.start()

    def _set_probe_status(self, profile_id: str, cap: str, value: str) -> None:
        if cap == "models":
            cap = _tr("models", "模型列表", "Model list")
        elif cap == "local_pack":
            cap = _tr("local_pack", "本机模型包", "Local model pack")
        elif cap == "service":
            cap = _tr("service", "服务", "Service")
        self._probe_results[profile_id, cap] = value
        self._refresh_probe_status()
        if profile_id == self._current_profile_id:
            self._reveal_model_control("probe_status")

    def _probe_finished(self, profile_id: str, operation: str, version: int,
                        result: str | list[str] | None, error: str) -> None:
        self._test_worker = None
        button = self._widgets.get("test_btn")
        if not isinstance(button, QPushButton) or sip.isdeleted(button):
            return
        button.setEnabled(True)
        self._widgets["discover_models"].setEnabled(True)
        if version != self._probe_version or self._registry.get(profile_id) is None:
            self._probe_results = {key: value for key, value in self._probe_results.items()
                                   if key[0] != profile_id or value != _tr("probe_working", "正在验证…", "Checking…")}
            self._refresh_probe_status()
            return
        if operation == "language":
            self._connection_results[profile_id] = error or True
            self._refresh_capability_status()
        if error:
            self._set_probe_status(profile_id, operation, error)
        elif isinstance(result, list):
            self._set_probe_status(profile_id, operation, _tr("probe_found", "返回 {count} 个模型。", "{count} models returned.").format(count=len(result)))
            if result and profile_id == self._current_profile_id and button.isVisible():
                self._choose_model(result)
        else:
            self._set_probe_status(profile_id, operation, result or _tr("probe_passed", "验证通过", "Verified"))

    def _choose_model(self, models: list[str]) -> None:
        from PyQt6.QtWidgets import QInputDialog

        model_edit = self._widgets["model_id"]
        if not isinstance(model_edit, QLineEdit):
            return
        current = model_edit.text()
        picked, ok = QInputDialog.getItem(
            model_edit, _tr("choose_model", "选择模型", "Select model"),
            _tr("choose_model_label", "模型 ID", "Model ID"), models,
            models.index(current) if current in models else 0, True,
        )
        if ok and picked.strip():
            model_edit.setText(picked.strip())
