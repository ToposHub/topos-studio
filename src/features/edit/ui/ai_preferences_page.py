"""AI Editing 偏好设置页（Phase 9 UI-1 + LLM provider 接入）。

TAEP §11.4 / §11.9 UI-1：模型设置不散在聊天面板、工程文件或脚本中。

本页管理 AI provider profile 列表，提供像 cline 一样的配置表单：
- provider 类型（OpenAI / Ollama / Claude）
- 显示名、endpoint URL、模型 ID
- API key（存用户级 ``~/.topos/ai_profiles.json``，界面不回显，只显示"已设置/未设置"）
- 设为活动 profile、测试连接

- API key、profile 和 active profile 统一保存到用户 home 的 ``~/.topos/ai_profiles.json``；
  文件权限为 0600，界面永不回显 token。
- 界面永不回显 token。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QGroupBox,
    QCheckBox,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.features.edit.ai_editing.credential_store import (
    CREDENTIAL_STORAGE_FILE,
    CREDENTIAL_STORAGE_KEYCHAIN,
    get_credential_storage_preference,
    make_credential_ref,
    make_credential_store,
)
from src.features.edit.ai_editing.llm_client import make_client
from src.features.edit.ai_editing.provider_profiles import (
    AiProviderProfile,
    CredentialStore,
    DataPolicy,
    ProfileRegistry,
    ProviderLimits,
)
from src.features.edit.ai_editing.ai_provider_catalog import (
    SOURCE_OPTIONS,
    SOURCE_CUSTOM,
    SOURCE_LOCAL,
    SOURCE_OLLAMA,
    SOURCE_PRESET,
    category_for_profile,
    get_preset,
    infer_capabilities,
    presets_for_category,
)
from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.ui.widgets.pref_form_layout import make_pref_label

# provider_kind 选项（显示名, 值, endpoint placeholder, model placeholder）
_PROVIDER_OPTIONS = [
    (
        "本地 OpenAI 兼容",
        "local_http",
        "http://127.0.0.1:8000/v1",
        "qwen2.5-7b-instruct",
    ),
    (
        I18N.tr("preferences.ai_editing.provider_openai", "OpenAI"),
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
    ("DeepSeek", "deepseek", "https://api.deepseek.com", "deepseek-chat"),
    (
        "智谱 GLM Coding",
        "openai",
        "https://open.bigmodel.cn/api/coding/paas/v4",
        "glm-5.2",
    ),
    ("火山方舟 Responses", "volcengine_responses", "https://ark.cn-beijing.volces.com/api/plan/v3", "doubao-seed-2.0-lite"),
    ("火山方舟 Seedream 图片", "volcengine_seedream", "https://ark.cn-beijing.volces.com/api/plan/v3", "doubao-seedream-5.0-lite"),
    ("火山方舟 TTS", "volcengine_plan_tts", "https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional", "doubao-seed-tts-2.0"),
    ("本地 Qwen3-ASR", "local_qwen_asr", "", "qwen3_asr"),
    ("本地 Qwen3-TTS", "local_qwen_tts", "", "qwen3_tts_customvoice"),
    (
        "火山方舟 ASR",
        "volcengine_plan_asr",
        "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash",
        "volc.seedasr.sauc.duration",
    ),
]

_CATEGORY_OPTIONS = (
    ("language", "语言模型"),
    ("vision", "视觉模型"),
    ("speech", "语音模型"),
    ("embedding", "向量化模型"),
    ("other", "其他服务"),
)

_CAPABILITY_OPTIONS = (
    ("language", "语言理解 / 对话"),
    ("vision", "视觉理解"),
    ("image", "图片生成"),
    ("video", "视频生成"),
    ("asr", "语音转文字（ASR）"),
    ("tts", "文字转语音（TTS）"),
    ("embedding", "向量化 / Embedding"),
)

logger = logging.getLogger(__name__)


class _ConnectionTestWorker(QThread):
    """AUD-19603：后台执行"测试连接"的阻塞 HTTP 请求。

    ``client.test_connection()`` 是同步 chat completion（默认 120s 超时 ×
    重试），此前在 UI 线程直接调用，最坏可冻结界面数分钟。结果经信号
    回 UI 线程（PyQt 的 lambda 连接代理生成于 connect() 所在线程，
    Auto 连接会排队回 UI 线程执行）。
    """

    succeeded = pyqtSignal(str)        # profile_id
    failed = pyqtSignal(str, str)      # profile_id, error message

    def __init__(
        self,
        profile: AiProviderProfile,
        api_key: str,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._profile = profile
        self._api_key = api_key

    def run(self) -> None:
        try:
            client = make_client(self._profile, _StaticKeyStore(self._api_key))
            client.test_connection()
        except Exception as exc:  # noqa: BLE001 - 错误信息原样回 UI 呈现
            self.failed.emit(str(self._profile.profile_id), str(exc))
        else:
            self.succeeded.emit(str(self._profile.profile_id))


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
        self._credential_keychain_checkbox: QCheckBox | None = None
        self._config: Any | None = None
        self._model_pack_manager = model_pack_manager
        # 当前选中的 profile_id（编辑表单绑定到此）
        self._current_profile_id: str = ""
        self._selected_category = "language"
        # 标记 API key 是否在本会话被修改过（apply 时才落盘）
        self._pending_api_key: str | None = None
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

    # ------------------------------------------------------------------ #
    # ModularPreferencePage 接口
    # ------------------------------------------------------------------ #

    def create_widget(self, parent: QWidget | None = None) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("PrefPageContent")
        scroll.setWidget(content)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        # --- 隐私说明 ---
        privacy = QLabel(
            I18N.tr(
                "preferences.ai_editing.privacy_note",
                "AI Profile 与 key：~/.topos/ai_profiles.json（权限 0600），可直接编辑。",
            )
        )
        privacy.setObjectName("AiPrivacyNote")
        privacy.setContentsMargins(4, 0, 4, 2)
        layout.addWidget(privacy)

        # --- Profile 管理（按能力分类，列表在上，编辑表单在下）---
        profiles_group = QGroupBox(
            I18N.tr("preferences.ai_editing.profiles", "模型与服务 Profile")
        )
        outer = QVBoxLayout(profiles_group)
        outer.setContentsMargins(10, 8, 10, 10)
        outer.setSpacing(8)

        category_tabs = QTabWidget()
        category_tabs.setObjectName("AiProviderCategoryTabs")
        category_tabs.setDocumentMode(True)
        for _category, label in _CATEGORY_OPTIONS:
            category_tabs.addTab(QWidget(), label)
        category_tabs.currentChanged.connect(self._on_category_changed)
        self._widgets["category_tabs"] = category_tabs
        outer.addWidget(category_tabs)

        # profile 列表 + 操作按钮：恢复为表单上方的纵向结构。
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(6)
        self._widgets["profile_list"] = QListWidget()
        lst: QListWidget = self._widgets["profile_list"]  # type: ignore[assignment]
        # 默认多显示一行 Profile；仍限制上限，避免列表挤压下方编辑表单。
        lst.setMinimumHeight(72)
        lst.setMaximumHeight(112)
        lst.setUniformItemSizes(True)
        lst.currentItemChanged.connect(self._on_profile_selected)
        left.addWidget(lst)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(5)
        add_btn = QPushButton(I18N.tr("preferences.ai_editing.add_profile", "新建"))
        add_btn.clicked.connect(self._on_add_profile)
        btn_row.addWidget(add_btn)

        dup_btn = QPushButton(I18N.tr("preferences.ai_editing.duplicate", "复制"))
        dup_btn.clicked.connect(self._on_duplicate_profile)
        btn_row.addWidget(dup_btn)

        remove_btn = QPushButton(I18N.tr("preferences.ai_editing.remove_profile", "删除"))
        remove_btn.clicked.connect(self._on_remove_profile)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch()

        # 活动 Profile 与列表操作保持同一行，避免单独占用表单空间。
        active_chk = QPushButton(
            I18N.tr("preferences.ai_editing.set_active", "设为活动 Profile")
        )
        active_chk.setCheckable(True)
        active_chk.setFixedHeight(26)
        active_chk.setToolTip(I18N.tr(
            "preferences.ai_editing.set_active_tooltip",
            "将此 Profile 设为 AI Editing 默认使用的模型。",
        ))
        active_chk.toggled.connect(self._on_active_toggled)
        self._widgets["set_active"] = active_chk
        btn_row.addWidget(active_chk)

        btn_w = QWidget()
        btn_w.setLayout(btn_row)
        left.addWidget(btn_w)

        left_w = QWidget()
        left_w.setLayout(left)

        outer.addWidget(left_w)

        # 字段编辑表单：主结构纵向，表单内部再按内容做局部双列。
        form_w = self._build_edit_form()
        outer.addWidget(form_w)

        layout.addWidget(profiles_group)
        # 策略与运行状态放在 Profile 配置之后，避免首屏被低频状态信息占满。
        layout.addWidget(self._build_automation_section())
        layout.addWidget(self._build_environment_section())
        layout.addStretch()

        self._refresh_profile_list()
        return scroll

    def _build_environment_section(self) -> QWidget:
        """Show capability status and MCP services in one compact tab group."""
        group = QGroupBox(I18N.tr("preferences.ai_editing.environment", "服务状态"))
        group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(group)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(0)

        tabs = QTabWidget()
        tabs.setObjectName("AiEnvironmentTabs")
        tabs.setDocumentMode(True)
        tabs.addTab(
            self._build_capability_section(),
            I18N.tr("preferences.ai_editing.capabilities", "能力状态"),
        )
        tabs.addTab(
            self._build_mcp_section(),
            I18N.tr("preferences.ai_editing.mcp_servers", "MCP 服务"),
        )
        self._widgets["environment_tabs"] = tabs
        layout.addWidget(tabs)
        return group

    def _build_capability_section(self) -> QWidget:
        """Build the capability status tab without making network calls."""
        page = QWidget()
        form = QVBoxLayout(page)
        form.setContentsMargins(2, 6, 2, 2)
        form.setSpacing(4)
        status_list = QListWidget()
        status_list.setObjectName("AiCapabilityStatusList")
        # 与 MCP 服务列表保持一致：显示多行状态，超出可视区域时滚动。
        status_list.setMinimumHeight(96)
        status_list.setMaximumHeight(168)
        status_list.setUniformItemSizes(True)
        status_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self._widgets["capability_status"] = status_list
        status_label = QLabel(I18N.tr("preferences.ai_editing.capability_status", "当前状态:"))
        status_label.setObjectName("PrefFormLabel")
        # 这是列表上方的提示行，不应像普通 Preferred QLabel 一样吸收整个标签页高度。
        status_label.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )
        form.addWidget(status_label)
        form.addWidget(status_list)
        return page

    def _build_mcp_section(self) -> QWidget:
        """MCP 服务管理：展示内置 MCP server 的状态并允许按 server 启停。

        只读展示 + 本地启停开关，不做网络调用（状态来自网关的环境探测：
        npx/key 是否就绪）。每个条目可勾选启用；取消勾选后其工具不再暴露给
        Agent，也不会发起任何 MCP 调用。
        """
        page = QWidget()
        form = QVBoxLayout(page)
        form.setContentsMargins(2, 6, 2, 2)
        form.setSpacing(4)
        hint = QLabel(I18N.tr(
            "preferences.ai_editing.mcp_hint",
            "内置 MCP server（视觉分析 / 联网搜索 / 网页读取）。取消勾选即对 Agent 隐藏其工具。",
        ))
        hint.setObjectName("PrefFormLabel")
        hint.setWordWrap(True)
        server_list = QListWidget()
        server_list.setObjectName("AiMcpServerList")
        server_list.setMinimumHeight(96)
        server_list.setMaximumHeight(168)
        server_list.setUniformItemSizes(True)
        server_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        # 勾选变化 → 网关启停（防递归：refresh 时 blockSignals）。
        server_list.itemChanged.connect(self._on_mcp_server_toggled)
        self._widgets["mcp_server_list"] = server_list
        form.addWidget(hint)
        form.addWidget(server_list)
        return page

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
            mark = "🟢" if st.get("available") else "⚪"
            n_tools = len(st.get("tools", ()))
            label = (
                f"{mark} {st.get('name', '')} · {st.get('transport', '')}"
                f" · {n_tools} 个工具"
            )
            if not st.get("available") and st.get("reason"):
                label += f"（{st['reason']}）"
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

    def _build_automation_section(self) -> QWidget:
        """Expose safe commercial automation flags with secure defaults."""
        group = QGroupBox(I18N.tr("preferences.ai_editing.automation", "自动执行策略"))
        group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        form = QHBoxLayout(group)
        form.setContentsMargins(10, 8, 10, 10)
        form.setSpacing(22)
        entries = (
            ("agent_auto_mode", "启用受限自动模式", "关闭时只生成建议和预览。"),
            ("cloud_upload", "允许云端素材上传", "每次素材上传仍需独立 consent。"),
            ("auto_export", "允许自动导出", "最终导出仍受质量、版权和项目范围门禁。"),
        )
        for key, label, tooltip in entries:
            checkbox = QCheckBox(I18N.tr(f"preferences.ai_editing.flag.{key}", label))
            checkbox.setMinimumHeight(26)
            checkbox.setToolTip(tooltip)
            checkbox.stateChanged.connect(self._on_automation_flag_changed)
            self._automation_widgets[key] = checkbox
            form.addWidget(checkbox)
        form.addStretch()
        return group

    def _create_credential_storage_checkbox(self) -> QCheckBox:
        """Create the key-storage selector shown directly below API Key."""
        checkbox = QCheckBox(
            I18N.tr(
                "preferences.ai_editing.use_keychain",
                "将 API 密钥存储到系统钥匙串（更安全，但不随配置文件迁移到其他电脑）",
            )
        )
        checkbox.setMinimumHeight(24)
        checkbox.setToolTip(
            I18N.tr(
                "preferences.ai_editing.use_keychain_tooltip",
                "默认关闭：密钥存在 ~/.topos/ai_profiles.json，拷贝该文件即可迁移到新电脑。\n"
                "勾选后：密钥存入系统钥匙串，更安全但换电脑需重新录入。",
            )
        )
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
            line = f"{status.label} · {status.state.value}"
            if status.reason:
                line += f"（{status.reason}）"
            item = QListWidgetItem(line)
            item.setData(Qt.ItemDataRole.UserRole, status.to_dict())
            if status.next_step:
                item.setToolTip(status.next_step)
            status_list.addItem(item)
        status_list.blockSignals(False)
        # MCP 服务分区与能力状态一起刷新（同一"当前环境"视图）。
        self._refresh_mcp_section()

    # ------------------------------------------------------------------ #
    # 字段编辑表单
    # ------------------------------------------------------------------ #

    def _build_edit_form(self) -> QWidget:
        group = QWidget()
        form = QVBoxLayout(group)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)

        # 连接设置：用 4 列网格把可以并排的字段收拢到同一行。
        connection_group = QGroupBox(
            I18N.tr("preferences.ai_editing.connection_group", "连接设置"))
        connection_grid = QGridLayout(connection_group)
        connection_grid.setContentsMargins(8, 6, 8, 8)
        connection_grid.setHorizontalSpacing(8)
        connection_grid.setVerticalSpacing(5)
        connection_grid.setColumnStretch(1, 1)
        connection_grid.setColumnStretch(3, 1)

        def add_full_row(row: int, label: str, widget: QWidget) -> None:
            connection_grid.addWidget(make_pref_label(label, width=88), row, 0)
            connection_grid.addWidget(widget, row, 1, 1, 3)

        def add_pair(
            row: int,
            left_label: str,
            left_widget: QWidget,
            right_label: str,
            right_widget: QWidget,
        ) -> None:
            connection_grid.addWidget(make_pref_label(left_label, width=88), row, 0)
            connection_grid.addWidget(left_widget, row, 1)
            connection_grid.addWidget(make_pref_label(right_label, width=88), row, 2)
            connection_grid.addWidget(right_widget, row, 3)

        # 显示名
        name_edit = QLineEdit()
        name_edit.setFixedHeight(28)
        name_edit.setPlaceholderText(
            I18N.tr("preferences.ai_editing.name_placeholder", "My OpenAI"))
        name_edit.textChanged.connect(self._on_field_changed)
        self._widgets["display_name"] = name_edit
        add_full_row(0, I18N.tr("preferences.ai_editing.field_name", "显示名:"), name_edit)

        # 接入方式：本地模型 / Ollama / 第三方预设 / 自定义 API
        source_combo = ChevronComboBox()
        source_combo.setFixedHeight(28)
        for label, value in SOURCE_OPTIONS:
            source_combo.addItem(label, value)
        source_combo.currentIndexChanged.connect(self._on_source_kind_changed)
        self._widgets["source_kind"] = source_combo

        # 第三方预设只填非 secret 配置；API key 仍由 CredentialStore 保存。
        preset_combo = ChevronComboBox()
        preset_combo.setFixedHeight(28)
        self._refresh_preset_options(preset_combo)
        preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        self._widgets["preset_id"] = preset_combo

        # provider 类型 / 协议
        kind_combo = ChevronComboBox()
        kind_combo.setFixedHeight(28)
        for label, value, _url, _model in _PROVIDER_OPTIONS:
            kind_combo.addItem(label, value)
        kind_combo.currentIndexChanged.connect(self._on_provider_kind_changed)
        self._widgets["provider_kind"] = kind_combo

        # endpoint URL
        endpoint_edit = QLineEdit()
        endpoint_edit.setFixedHeight(28)
        endpoint_edit.setPlaceholderText(
            I18N.tr("preferences.ai_editing.base_url_placeholder", "Base URL 或完整接口地址"))
        endpoint_edit.setToolTip(
            I18N.tr(
                "preferences.ai_editing.base_url_tooltip",
                "可填写 Base URL（例如 /v4）或完整接口地址（例如 /v4/chat/completions）；程序不会重复拼接路径")
        )
        endpoint_edit.textChanged.connect(self._on_field_changed)
        self._widgets["endpoint"] = endpoint_edit

        # 模型 ID
        model_edit = QLineEdit()
        model_edit.setFixedHeight(28)
        model_edit.setPlaceholderText(
            I18N.tr("preferences.ai_editing.model_placeholder", "gpt-4o"))
        model_edit.textChanged.connect(self._on_field_changed)
        self._widgets["model_id"] = model_edit

        # 上下文长度：按 Profile 保存，默认 200k，允许用户按模型能力调整。
        context_spin = QSpinBox()
        # 以千 token 为用户单位（200k），内部仍保存精确 token 数。
        context_spin.setRange(1, 2_000)
        context_spin.setSingleStep(1)
        context_spin.setSuffix(I18N.tr("preferences.ai_editing.context_suffix", "k"))
        context_spin.setValue(200)
        context_spin.setFixedHeight(28)
        context_spin.valueChanged.connect(self._on_context_limit_changed)
        self._widgets["context_limit"] = context_spin

        # API key（密码框 + 设置按钮 + 状态标签）
        key_edit = QLineEdit()
        key_edit.setFixedHeight(28)
        key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        key_edit.setPlaceholderText(
            I18N.tr("preferences.ai_editing.key_placeholder", "留空则保持已保存的 key")
        )
        key_edit.textChanged.connect(self._on_key_changed)
        self._widgets["api_key"] = key_edit

        key_status = QLabel(I18N.tr("preferences.ai_editing.key_not_set", "未设置"))
        self._widgets["key_status"] = key_status
        keychain_checkbox = self._create_credential_storage_checkbox()

        test_btn = QPushButton(I18N.tr("preferences.ai_editing.test_connection", "测试连接"))
        test_btn.setFixedHeight(28)
        test_btn.clicked.connect(self._on_test_connection)
        self._widgets["test_btn"] = test_btn

        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(6)
        key_layout.addWidget(key_edit, 1)
        key_layout.addWidget(test_btn, 0)

        add_pair(1, "接入方式:", source_combo, "服务预设:", preset_combo)
        add_pair(
            2,
            I18N.tr("preferences.ai_editing.field_provider", "协议:"),
            kind_combo,
            I18N.tr("preferences.ai_editing.field_context_limit", "上下文上限:"),
            context_spin,
        )
        add_full_row(3, I18N.tr("preferences.ai_editing.field_endpoint", "URL:"), endpoint_edit)
        add_full_row(4, I18N.tr("preferences.ai_editing.field_model", "模型 ID:"), model_edit)
        add_full_row(5, I18N.tr("preferences.ai_editing.field_apikey", "API Key:"), key_row)
        connection_grid.addWidget(key_status, 6, 1, 1, 3)
        add_full_row(
            7,
            I18N.tr("preferences.ai_editing.security", "密钥存储:"),
            keychain_checkbox,
        )
        form.addWidget(connection_group)

        # 两组勾选项并排；每组内部使用两列，避免窄框内逐项挤成一条长列。
        routes_row = QHBoxLayout()
        routes_row.setContentsMargins(0, 0, 0, 0)
        routes_row.setSpacing(10)
        modality_group = QGroupBox(
            I18N.tr("preferences.ai_editing.media_routes", "媒体能力与上传策略")
        )
        modality_grid = QGridLayout(modality_group)
        modality_grid.setContentsMargins(10, 8, 10, 10)
        modality_grid.setHorizontalSpacing(16)
        modality_grid.setVerticalSpacing(9)
        for modality, label in (
            ("text", "文本 Agent"),
            ("tool_call", "工具调用"),
            ("image", "图片生成"),
            ("video", "视频生成"),
            ("music", "音乐生成"),
            ("voice", "配音/TTS"),
        ):
            checkbox = QCheckBox(label)
            checkbox.setMinimumHeight(25)
            checkbox.stateChanged.connect(self._on_modality_changed)
            self._modality_widgets[modality] = checkbox
            index = len(self._modality_widgets) - 1
            modality_grid.addWidget(checkbox, index // 2, index % 2)
        upload_checkbox = QCheckBox(
            I18N.tr("preferences.ai_editing.allow_upload", "允许将参考素材上传到此 Profile"))
        upload_checkbox.setMinimumHeight(25)
        upload_checkbox.setToolTip(I18N.tr(
            "preferences.ai_editing.allow_upload_tooltip",
            "只声明数据策略；每次云端分析/生成仍需独立用户确认。"))
        upload_checkbox.stateChanged.connect(self._on_modality_changed)
        self._widgets["allow_media_upload"] = upload_checkbox
        modality_grid.addWidget(upload_checkbox, 3, 0, 1, 2)
        modality_group.setMinimumHeight(140)
        modality_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        routes_row.addWidget(modality_group, 1)

        capability_group = QGroupBox(
            I18N.tr("preferences.ai_editing.capabilities_group", "服务能力（用于分类和路由）"))
        capability_grid = QGridLayout(capability_group)
        capability_grid.setContentsMargins(10, 8, 10, 10)
        capability_grid.setHorizontalSpacing(16)
        capability_grid.setVerticalSpacing(9)
        for capability, label in _CAPABILITY_OPTIONS:
            checkbox = QCheckBox(label)
            checkbox.setMinimumHeight(25)
            checkbox.stateChanged.connect(self._on_capability_changed)
            self._capability_widgets[capability] = checkbox
            index = len(self._capability_widgets) - 1
            capability_grid.addWidget(checkbox, index // 2, index % 2)
        capability_group.setMinimumHeight(140)
        capability_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        routes_row.addWidget(capability_group, 1)
        form.addLayout(routes_row)

        return group

    # ------------------------------------------------------------------ #
    # load / apply / restore_defaults（修复持久化缺口）
    # ------------------------------------------------------------------ #

    def load(self, config: Any) -> None:
        """从 config 加载 profiles 到 registry 并刷新列表。

        ``config`` 是 config dict（ConfigManager.config 或等价 dict）；兼容传入
        ConfigManager（用 .get）。
        """
        self._config = config
        self._load_automation_flags(config)
        self._load_credential_pref()
        self._registry.load_from_config(config)
        # 用户级文件优先；旧 ConfigManager 数据会在首次保存时迁移过去。
        self._registry.load_from_user_file()
        self._refresh_profile_list()

    def apply(self, config_manager: Any) -> None:
        """把当前编辑表单写回 registry，再持久化到 config。

        先 flush 当前编辑中的 profile（用户可能在表单里改了字段没切列表），
        再调 ``registry.save_to_config``，并同步写入用户级 ``~/.topos`` 文件。
        """
        self._flush_current_profile()
        # 持久化密钥存储位置偏好（文件 / 钥匙串）
        self._save_credential_pref()
        # 写入待落盘的 API key（如果有）。空输入按占位符语义保持已存 key
        # 不覆盖（AUD-19605：与切换时的 _flush_pending_key 行为一致）。
        if self._pending_api_key:
            profile = self._registry.get(self._current_profile_id) if self._current_profile_id else None
            if profile is not None:
                try:
                    self._credential_store.set_secret(profile.credential_ref, self._pending_api_key)
                except Exception:  # noqa: BLE001
                    pass  # 错误已在 set_secret 内记录；不阻塞 apply
                self._credential_available_cache.clear()
            self._pending_api_key = None
        self._registry.save_to_config(config_manager)
        self._save_automation_flags(config_manager)
        self._registry.save_to_user_file()
        # AUD-19606：与 registry.remove 同一提交点执行延后的凭据删除。
        self._commit_pending_secret_deletions()
        self._refresh_capability_status()
        if self.on_profiles_changed:
            self.on_profiles_changed()

    def _commit_pending_secret_deletions(self) -> None:
        """AUD-19606：apply() 时统一删除已移除 profile 的凭据。

        同 ref 的 profile 若被重建（极端顺序），跳过删除避免误伤新凭据。
        """
        if not self._pending_secret_deletions:
            return
        live_refs = {
            str(p.credential_ref) for p in self._registry.list() if p.credential_ref
        }
        for ref in self._pending_secret_deletions:
            if ref in live_refs:
                continue
            try:
                self._credential_store.delete_secret(ref)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to delete credential %r on apply", ref, exc_info=True,
                )
            self._credential_available_cache.pop(ref, None)
        self._pending_secret_deletions.clear()

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
        self._current_profile_id = ""
        self._refresh_profile_list()
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

    def _profile_list_label(self, profile: AiProviderProfile) -> str:
        """profile 在列表中的显示标签（只展示用户命名 + 接入方式 + 活动星标）。"""
        source_label = {
            SOURCE_LOCAL: "本地",
            SOURCE_OLLAMA: "Ollama",
            SOURCE_PRESET: "预设",
            SOURCE_CUSTOM: "自定义",
        }.get(profile.source_kind, profile.source_kind)
        label = f"{profile.display_name} · {source_label}"
        if self._registry.is_active(profile.profile_id):
            label = "★ " + label
        return label

    def _update_current_list_item_label(self) -> None:
        """AUD-19607：就地更新当前行的列表标签，不整表重建。

        此前每次键入都会 clear+rebuild QListWidget 并连带触发第二次
        ``_refresh_capability_status``（又一次凭据读盘）。
        """
        lst = self._widgets.get("profile_list")
        if not isinstance(lst, QListWidget) or not self._current_profile_id:
            return
        profile = self._registry.get(self._current_profile_id)
        if profile is None:
            return
        for i in range(lst.count()):
            item = lst.item(i)
            if (
                item is not None
                and item.data(Qt.ItemDataRole.UserRole) == profile.profile_id
            ):
                item.setText(self._profile_list_label(profile))
                break

    def _refresh_profile_list(self) -> None:
        lst: QListWidget = self._widgets.get("profile_list")  # type: ignore[assignment]
        if lst is None:
            return
        lst.blockSignals(True)
        lst.clear()
        visible_profiles = [
            profile for profile in self._registry.list()
            if category_for_profile(profile) == self._selected_category
        ]
        for profile in visible_profiles:
            # 列表只展示用户给 Profile 起的名称；协议类型属于编辑详情，避免
            # 在名称后重复显示用户不关心的内部值（例如 ``(openai)``）。
            item = QListWidgetItem(self._profile_list_label(profile))
            item.setData(Qt.ItemDataRole.UserRole, profile.profile_id)
            lst.addItem(item)
        # Select while signals are blocked.  Otherwise a newly-created profile
        # is briefly loaded with the previous form values and gets overwritten
        # before its preset fields reach the editor.
        target_id = self._current_profile_id
        target_row = next(
            (
                i for i in range(lst.count())
                if lst.item(i).data(Qt.ItemDataRole.UserRole) == target_id
            ),
            -1,
        ) if target_id else -1
        if target_row < 0 and lst.count() > 0:
            target_row = 0
            target_id = str(lst.item(0).data(Qt.ItemDataRole.UserRole))
        if target_row >= 0:
            lst.setCurrentRow(target_row)
        lst.blockSignals(False)
        if target_row >= 0:
            self._current_profile_id = target_id
            self._load_profile_to_form(target_id)
        else:
            self._current_profile_id = ""
            self._clear_form()
        self._sync_active_button()
        self._refresh_capability_status()

    def _on_profile_selected(self, current: QListWidgetItem, _previous: QListWidgetItem | None) -> None:
        """列表选中变化 → 先 flush 旧 profile，再加载新 profile 到表单。"""
        if current is None:
            return
        # flush 之前的编辑（避免切换丢失）
        self._flush_current_profile()
        # AUD-19605：输入中的 API key 与其他字段一致"切换即保存"，
        # 不再随切换被静默丢弃（需在 _current_profile_id 改指向前落盘）。
        self._flush_pending_key()
        profile_id = current.data(Qt.ItemDataRole.UserRole)
        self._current_profile_id = profile_id
        self._load_profile_to_form(profile_id)

    def _flush_pending_key(self) -> None:
        """AUD-19605：把暂存的 API key 写到当前 profile 的凭据仓。

        空输入按占位符语义（"留空则保持已保存的 key"）不写盘；写入后
        既有连接测试结果不再可信，与 ``_flush_current_profile`` 一致地清除。
        """
        key = self._pending_api_key
        self._pending_api_key = None
        if not key or not self._current_profile_id:
            return
        profile = self._registry.get(self._current_profile_id)
        if profile is None or not profile.credential_ref:
            return
        try:
            self._credential_store.set_secret(profile.credential_ref, key)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to persist pending API key for %s",
                profile.profile_id, exc_info=True,
            )
            return
        self._credential_available_cache.clear()
        self._connection_results.pop(profile.profile_id, None)
        self._refresh_capability_status()

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
        self._set_combo_data(preset_combo, profile.preset_id)
        # 选中对应 provider_kind
        for i in range(kind_combo.count()):
            if kind_combo.itemData(i) == profile.provider_kind:
                kind_combo.setCurrentIndex(i)
                break
        endpoint_edit.setText(profile.endpoint)
        model_edit.setText(profile.model_id)
        context_spin.setValue(max(1, round(profile.limits.max_context_tokens / 1_000)))
        key_edit.clear()
        for modality, checkbox in self._modality_widgets.items():
            checkbox.setChecked(modality in profile.modalities)
        capabilities = set(profile.capabilities or infer_capabilities(profile.provider_kind, profile.modalities))
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
        self._sync_active_button()

    def _clear_form(self) -> None:
        self._block_form_signals(True)
        self._widgets["display_name"].setText("")  # type: ignore[union-attr]
        self._set_combo_data(self._widgets["source_kind"], SOURCE_CUSTOM)  # type: ignore[arg-type]
        self._set_combo_data(self._widgets["preset_id"], "")  # type: ignore[arg-type]
        self._set_combo_data(self._widgets["provider_kind"], "openai")  # type: ignore[arg-type]
        self._widgets["endpoint"].setText("")  # type: ignore[union-attr]
        self._widgets["model_id"].setText("")  # type: ignore[union-attr]
        self._widgets["context_limit"].setValue(200)  # type: ignore[union-attr]
        self._widgets["api_key"].setText("")  # type: ignore[union-attr]
        for checkbox in self._modality_widgets.values():
            checkbox.setChecked(False)
        for checkbox in self._capability_widgets.values():
            checkbox.setChecked(False)
        upload_checkbox = self._widgets.get("allow_media_upload")
        if isinstance(upload_checkbox, QCheckBox):
            upload_checkbox.setChecked(False)
        self._widgets["set_active"].setChecked(False)  # type: ignore[union-attr]
        self._block_form_signals(False)
        self._pending_api_key = None
        self._sync_active_button()

    def _block_form_signals(self, blocked: bool) -> None:
        for key in (
            "display_name", "source_kind", "preset_id", "provider_kind", "endpoint", "model_id",
            "context_limit", "api_key",
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
        self._update_current_list_item_label()

    def _on_provider_kind_changed(self) -> None:
        """provider 类型变化 → 更新 placeholder + flush。"""
        kind_combo: ChevronComboBox = self._widgets["provider_kind"]  # type: ignore[assignment]
        kind = kind_combo.currentData()
        self._update_placeholders(kind)
        self._flush_current_profile()
        self._update_current_list_item_label()

    def _on_category_changed(self, index: int) -> None:
        self._flush_current_profile()
        if 0 <= index < len(_CATEGORY_OPTIONS):
            self._selected_category = _CATEGORY_OPTIONS[index][0]
        self._refresh_preset_options()
        self._refresh_profile_list()

    def _refresh_preset_options(self, combo: ChevronComboBox | None = None) -> None:
        """只显示当前能力分类对应的预设，避免跨分类混在同一个下拉框。"""
        if combo is None:
            widget = self._widgets.get("preset_id")
            if not isinstance(widget, ChevronComboBox):
                return
            combo = widget

        current_id = str(combo.currentData() or "")
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("不使用预设（手动填写）", "")
        for preset in presets_for_category(self._selected_category):
            combo.addItem(preset.label, preset.preset_id)
        self._set_combo_data(combo, current_id)
        if combo.currentIndex() < 0:
            combo.setCurrentIndex(0)
        combo.blockSignals(False)

    def _on_source_kind_changed(self) -> None:
        self._flush_current_profile()
        self._update_current_list_item_label()

    def _on_preset_changed(self) -> None:
        combo: ChevronComboBox = self._widgets["preset_id"]  # type: ignore[assignment]
        preset = get_preset(str(combo.currentData() or ""))
        if preset is None:
            self._flush_current_profile()
            return
        self._apply_preset(preset)
        self._flush_current_profile()
        self._refresh_profile_list()

    def _on_capability_changed(self, _value: int) -> None:
        self._flush_current_profile()
        self._refresh_profile_list()

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

    def _apply_preset(self, preset: Any) -> None:
        """Copy non-secret preset fields into the current editor form."""
        self._block_form_signals(True)
        self._set_combo_data(self._widgets["source_kind"], preset.source_kind)
        self._set_combo_data(self._widgets["provider_kind"], preset.provider_kind)
        self._widgets["endpoint"].setText(preset.endpoint)  # type: ignore[union-attr]
        self._widgets["model_id"].setText(preset.model_id)  # type: ignore[union-attr]
        for capability, checkbox in self._capability_widgets.items():
            checkbox.setChecked(capability in preset.capabilities)
        for modality, checkbox in self._modality_widgets.items():
            checkbox.setChecked(modality in preset.modalities)
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

    def _on_active_toggled(self, checked: bool) -> None:
        if not checked:
            # 不允许直接取消勾选；改为选别的 profile 时自动切换
            active_chk: QPushButton = self._widgets["set_active"]  # type: ignore[assignment]
            active_chk.blockSignals(True)
            active_chk.setChecked(True)
            active_chk.blockSignals(False)
            self._sync_active_button()
            return
        if self._current_profile_id:
            self._registry.set_active(self._current_profile_id)
            self._refresh_profile_list()
            self._refresh_capability_status()

    def _sync_active_button(self) -> None:
        """同步活动 Profile 按钮的勾选状态与可见文案。"""
        active_chk: QPushButton | None = self._widgets.get("set_active")  # type: ignore[assignment]
        if active_chk is None:
            return
        is_active = bool(self._current_profile_id) and (
            self._registry.active_profile_id == self._current_profile_id
        )
        active_chk.blockSignals(True)
        active_chk.setChecked(is_active)
        active_chk.setText(I18N.tr(
            "preferences.ai_editing.active_profile" if is_active
            else "preferences.ai_editing.set_active",
            "Active Profile" if is_active else "Set as Active Profile",
        ))
        active_chk.blockSignals(False)

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
        modalities = [
            modality for modality, checkbox in self._modality_widgets.items()
            if checkbox.isChecked()
        ]
        if not modalities:
            modalities = ["text", "tool_call"]
        upload_checkbox = self._widgets.get("allow_media_upload")
        allow_upload = isinstance(upload_checkbox, QCheckBox) and upload_checkbox.isChecked()
        if allow_upload and "media_upload" not in modalities:
            modalities.append("media_upload")
        if not allow_upload:
            modalities = [item for item in modalities if item != "media_upload"]
        capabilities = tuple(
            capability for capability, checkbox in self._capability_widgets.items()
            if checkbox.isChecked()
        )
        if not capabilities:
            capabilities = infer_capabilities(
                kind_combo.currentData() or old.provider_kind,
                modalities,
            )
        source_kind = source_combo.currentData() or old.source_kind or SOURCE_CUSTOM
        new = AiProviderProfile(
            profile_id=old.profile_id,
            display_name=name_edit.text().strip() or old.display_name,
            provider_kind=kind_combo.currentData() or old.provider_kind,
            source_kind=source_kind,
            preset_id=str(preset_combo.currentData() or ""),
            endpoint=endpoint_edit.text().strip(),
            model_id=model_edit.text().strip(),
            credential_ref=old.credential_ref or make_credential_ref(old.profile_id),
            modalities=tuple(modalities),
            capabilities=capabilities,
            data_policy=DataPolicy(
                execution=(
                    "local" if source_kind in {SOURCE_LOCAL, SOURCE_OLLAMA}
                    else ("cloud" if allow_upload else old.data_policy.execution)
                ),
                allow_media_upload=allow_upload,
                retain_prompts=old.data_policy.retain_prompts,
            ),
            limits=ProviderLimits(
                max_context_tokens=int(context_spin.value()) * 1_000,
                max_parallel_jobs=old.limits.max_parallel_jobs,
                monthly_cost_alert=old.limits.monthly_cost_alert,
                request_timeout_seconds=old.limits.request_timeout_seconds,
                max_retries=old.limits.max_retries,
                max_cost_units_per_request=old.limits.max_cost_units_per_request,
            ),
        )
        if new != old:
            self._registry.add(new)
            # Any edit can change endpoint/model/credential/data policy, so a
            # previous successful ping no longer proves this profile is live.
            self._connection_results.pop(old.profile_id, None)
            self._refresh_capability_status()

    # ------------------------------------------------------------------ #
    # 内部：UI 辅助
    # ------------------------------------------------------------------ #

    def _update_key_status(self, profile: AiProviderProfile) -> None:
        """根据 credential_store 是否已有 key 更新状态标签。"""
        status: QLabel = self._widgets["key_status"]  # type: ignore[assignment]
        if not profile.credential_ref:
            status.setText(I18N.tr("preferences.ai_editing.key_not_set", "未设置"))
            return
        secret = self._credential_store.get_secret(profile.credential_ref)
        if secret:
            status.setText(
                I18N.tr(
                    "preferences.ai_editing.key_set",
                    "✓ 已保存（不会回显，输入新值可替换）",
                )
            )
            status.setStyleSheet("color: #6abf69;")
            self._widgets["api_key"].setPlaceholderText(I18N.tr(
                "preferences.ai_editing.key_saved_placeholder",
                "已保存 API key（不会显示内容，输入新值可替换）",
            ))  # type: ignore[union-attr]
        else:
            status.setText(I18N.tr("preferences.ai_editing.key_not_set", "未设置"))
            status.setStyleSheet("color: #999;")
            self._widgets["api_key"].setPlaceholderText(I18N.tr(
                "preferences.ai_editing.key_placeholder",
                "留空则保持已保存的 key",
            ))  # type: ignore[union-attr]

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
            display_name=I18N.tr("preferences.ai_editing.new_profile", "新 Profile"),
            provider_kind=provider_kind,
            endpoint=preset.endpoint if preset else "",
            model_id=preset.model_id if preset else "",
            credential_ref=make_credential_ref(profile_id),
            source_kind=preset.source_kind if preset else SOURCE_CUSTOM,
            preset_id=preset.preset_id if preset else "",
            modalities=preset.modalities if preset and preset.modalities else ("text", "tool_call"),
            capabilities=preset.capabilities if preset else ("language",),
        )
        self._registry.add(profile)
        # 新建后立即设为活动（首个 profile 自动活动）
        if not self._registry.active_profile_id:
            self._registry.set_active(profile_id)
        # AUD-19605：切到新 profile 前保存旧 profile 输入中的 key。
        self._flush_pending_key()
        self._current_profile_id = profile_id
        self._refresh_profile_list()
        self._load_profile_to_form(profile_id)
        if self.on_profiles_changed:
            self.on_profiles_changed()

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
            display_name=f"{src.display_name} (copy)",
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
        self._refresh_profile_list()
        self._load_profile_to_form(new_id)
        if self.on_profiles_changed:
            self.on_profiles_changed()

    def _on_remove_profile(self) -> None:
        lst: QListWidget = self._widgets["profile_list"]  # type: ignore[assignment]
        item = lst.currentItem()
        if item is None:
            return
        profile_id = item.data(Qt.ItemDataRole.UserRole)
        profile = self._registry.get(profile_id)
        # AUD-19606：凭据删除延后到 apply()（与 registry.remove 同一提交点）。
        # 此前 delete_secret 在点击删除时立即写盘且不可逆——用户删除后
        # 不 Apply / 取消，profile 仍在而已存 key 永久丢失。
        if profile is not None and profile.credential_ref:
            self._pending_secret_deletions.append(str(profile.credential_ref))
        self._registry.remove(profile_id)
        self._connection_results.pop(str(profile_id), None)
        self._current_profile_id = ""
        self._refresh_profile_list()
        # 选中第一个（若有）
        if lst.count() > 0:
            lst.setCurrentRow(0)
        else:
            self._clear_form()
        if self.on_profiles_changed:
            self.on_profiles_changed()

    def _on_test_connection(self) -> None:
        """测试连接：用当前表单值构造临时 client 发一次最小请求。

        AUD-19603：远端 profile 的同步 chat completion（默认 120s 超时 ×
        重试，最坏约 6 分钟）移到后台 QThread，期间按钮禁用显示"测试中…"，
        结果经信号回 UI；不再阻塞界面。
        """
        self._flush_current_profile()
        profile = self._registry.get(self._current_profile_id) if self._current_profile_id else None
        if profile is None:
            QMessageBox.warning(
                None, "Topos",
                I18N.tr("preferences.ai_editing.test_no_profile", "请先选择或新建一个 Profile。"),
            )
            return
        # 取 key：优先 pending，否则读用户级配置文件
        api_key = self._pending_api_key
        if api_key is None and profile.credential_ref:
            api_key = self._credential_store.get_secret(profile.credential_ref) or ""
        capabilities = set(profile.capabilities or infer_capabilities(
            profile.provider_kind, profile.modalities,
        ))
        if profile.source_kind == SOURCE_LOCAL:
            # AUD-19604：本地分支未做任何验证，不得把能力矩阵标成
            # "连接测试已通过"（违反本页 170-173 行自述不变量）。保持
            # 未验证状态，运行时再检查。
            self._connection_results.pop(profile.profile_id, None)
            self._refresh_capability_status()
            QMessageBox.information(
                None,
                "Topos",
                "已选择本地模型。本页不会发起网络请求，也不会将其标记为已通过连接测试；"
                "实际可用性将在运行时检查模型包和本机依赖。",
            )
            return
        if not profile.endpoint or not profile.model_id:
            QMessageBox.warning(
                None, "Topos",
                I18N.tr("preferences.ai_editing.test_missing", "请先填写 URL 和模型 ID。"),
            )
            return
        if capabilities.intersection({"asr", "tts", "image", "video", "embedding"}):
            if not api_key:
                QMessageBox.warning(None, "Topos", "请先设置 API Key。")
                return
            # These profiles deliberately have no quota-consuming generic
            # ping endpoint. Keep their state unverified until an actual,
            # user-consented capability request returns an outcome.
            self._connection_results.pop(profile.profile_id, None)
            self._refresh_capability_status()
            QMessageBox.information(
                None,
                "Topos",
                "配置完整。该能力的真实请求会在用户明确发起转录、合成或生成时执行，"
                "这里不会为了测试而消耗额度。",
            )
            return
        # 已有测试在跑：忽略重复点击（按钮同时被禁用，双保险）。
        if self._test_worker is not None and self._test_worker.isRunning():
            return
        test_btn = self._widgets.get("test_btn")
        if isinstance(test_btn, QPushButton):
            test_btn.setEnabled(False)
            test_btn.setText(I18N.tr("preferences.ai_editing.testing", "测试中…"))
        worker = _ConnectionTestWorker(profile, api_key or "")
        worker.succeeded.connect(
            lambda pid=str(profile.profile_id): self._on_test_connection_success(pid)
        )
        worker.failed.connect(
            lambda pid, err: self._on_test_connection_failure(str(pid), str(err))
        )
        worker.finished.connect(worker.deleteLater)
        self._test_worker = worker
        worker.start()

    def _restore_test_button(self) -> None:
        test_btn = self._widgets.get("test_btn")
        if isinstance(test_btn, QPushButton):
            test_btn.setEnabled(True)
            test_btn.setText(I18N.tr("preferences.ai_editing.test_connection", "测试连接"))

    def _on_test_connection_success(self, profile_id: str) -> None:
        """后台连接测试成功（UI 线程经排队信号回到这里）。"""
        self._test_worker = None
        self._restore_test_button()
        profile = self._registry.get(profile_id)
        if profile is None:
            return  # 测试期间 profile 已被删除
        self._connection_results[profile_id] = True
        self._refresh_capability_status()
        QMessageBox.information(
            None, "Topos",
            I18N.tr(
                "preferences.ai_editing.test_ok",
                "连接成功！模型 {model} 可用。",
            ).format(model=profile.model_id),
        )

    def _on_test_connection_failure(self, profile_id: str, error: str) -> None:
        """后台连接测试失败（UI 线程经排队信号回到这里）。"""
        self._test_worker = None
        self._restore_test_button()
        profile = self._registry.get(profile_id)
        if profile is None:
            return  # 测试期间 profile 已被删除
        self._connection_results[profile_id] = error
        self._refresh_capability_status()
        QMessageBox.critical(
            None,
            I18N.tr("preferences.ai_editing.test_fail_title", "连接失败"),
            error,
        )


class _StaticKeyStore:
    """测试连接用的临时 credential store（只返回给定的 key）。"""

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    def get_secret(self, credential_ref: str) -> str | None:
        return self._key or None

    def set_secret(self, credential_ref: str, secret: str) -> None:
        pass

    def delete_secret(self, credential_ref: str) -> None:
        pass
