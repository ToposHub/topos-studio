# batch-196 审查报告 — src/features/edit/ui/（AI 面板组件）

批次：batch-196 [P1]，编号区间 AUD-19600–AUD-19699，共 7 个文件 / 2,678 行。
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。
本批次全部为纯 Qt UI 组件：无 OpenGL/GLSL 触碰（GPU·GLSL·HDR 维度全批次通过），核心风险集中在 UI 线程阻塞、提交语义与状态展示真实性。

汇总：Critical 0 · Major 7 · Suggestion 17。

---

## src/features/edit/ui/ai_help_answer.py (行数: 185)

### 发现

- [AUD-19600] [Suggestion] 富文本标签直接插值未转义的帮助索引文本
  - 位置: src/features/edit/ui/ai_help_answer.py:118-123
  - 证据:
    ```python
    title_row = QLabel(
        f"<b>{item.title}</b> "
        f"<span style='color:{border_color}; font-size: small'>"
        f"({'可用' if item.feature_enabled else '未启用/未安装'})</span>"
    )
    title_row.setTextFormat(Qt.TextFormat.RichText)
    ```
  - 影响: `item.title` / `item.page_ref` / `item.next_steps` 来自 HelpIndex 抓取的帮助条目（外部化文本），含 `<`、`&` 时被按 RichText 解析，标题片段会被吞掉或错排（如 "剪裁<初级>"）。仅渲染问题，无脚本执行风险。
  - 建议: 插值前用 `html.escape()` 转义动态段，或对动态文本 QLabel 保持 PlainText。
  - 置信度: 高（代码事实）；影响概率低。

- [AUD-19601] [Suggestion] `_trf` 兜底函数在三个文件中重复实现且行为分叉
  - 位置: src/features/edit/ui/ai_help_answer.py:25-32（另见 ai_skill_card.py:48-56、ai_timeline_diff.py:36-45）
  - 证据:
    ```python
    def _trf(key: str, default: str, **kwargs) -> str:
        text = tr(key, default, **kwargs)
        if "{" in default and kwargs:
            try:
                return text.format(**kwargs)
            except (KeyError, IndexError):
                return text
        return text
    ```
  - 影响: 同名helper三份拷贝、判定条件各不相同（本文件/ai_skill_card 判 `"{" in default`，ai_timeline_diff 判 `"{" in result`）。i18n 兜底语义漂移，修一处漏两处。
  - 建议: 上移到 `src/shared/i18n/i18n.py` 统一一个实现（`tr` 本身已做 kwargs 插值，见 i18n.py:132-160，多数场景可直接删掉 `_trf`）。
  - 置信度: 高

### 通过项
- 信号防递归：组件无程序化回填路径，signal 仅由用户点击触发（navigate_requested/open_panel_requested），无递归风险
- 线程安全：纯 UI 线程构建/更新，无跨线程访问
- GPU 资源生命周期：不触碰 OpenGL/ModernGL
- 状态提交一致性：只读渲染 HelpAnswer，不涉及 undo/revision/持久化
- 异常路径：无 IO，无需异常处理
- 性能：条目卡数量受 HelpIndex 命中数约束，重建成本可控
- 输入验证：`set_answer` 空列表分支正确显示 empty/index_empty 提示

---

## src/features/edit/ui/ai_job_panel.py (行数: 126)

### 发现

- [AUD-19602] [Suggestion] set_jobs 每次快照全量拆建行控件，高频进度刷新下开销与体验受损
  - 位置: src/features/edit/ui/ai_job_panel.py:107-122
  - 证据:
    ```python
    def set_jobs(self, jobs: tuple[JobCard, ...]) -> None:
        self._updating = True
        try:
            while self._container_layout.count() > 1:
                item = self._container_layout.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.deleteLater()
            self._empty_label.setVisible(len(jobs) == 0)
            for card in jobs:
                row = _JobRowWidget(card)
    ```
  - 影响: `_JobRowWidget` 只在构造时消费 JobCard，无更新方法；服务端每个进度 tick 发 AssistantStateSnapshot 都会销毁并重建全部行——控件 GC 压力、闪烁、滚动位置丢失。job 数少时影响有限，但与项目"实时面板增量刷新"的既有模式不一致。
  - 建议: 按 job_id 建立行缓存，仅新增/移除变化的行，已有行增加 `update(card)` 方法原地刷新状态/进度。
  - 置信度: 中（取决于上游快照发射频率；代码模式问题确定）

### 通过项
- 信号防递归：`_updating` 锁在重建期间正确置位/复位；行控件 cancel/retry 信号只向上转发，不回写状态
- 线程安全：纯 UI 线程
- GPU 资源生命周期：不涉及
- 状态提交一致性：只经 signal（cancel_requested/retry_requested）控制任务，符合"UI 不直接操作 job service"约束
- 异常与取消路径：终态/运行态按钮可见性（succeeded/failed/cancelled vs queued/running/cancelling）判定正确
- 输入验证：无自由输入
- 类型注解：完整

---

## src/features/edit/ui/ai_preferences_page.py (行数: 1402)

### 发现

- [AUD-19603] [Major] "测试连接"在 UI 线程同步发起阻塞 HTTP 请求，最长可冻结界面数分钟
  - 位置: src/features/edit/ui/ai_preferences_page.py:1359-1362
  - 证据:
    ```python
    try:
        client = make_client(profile, _StaticKeyStore(api_key))
        client.test_connection()
        self._connection_results[profile.profile_id] = True
        self._refresh_capability_status()
        QMessageBox.information(
    ```
  - 影响: `client.test_connection()` 是同步 chat completion（llm_client.py:976-981 `self.complete([...], max_tokens=8)`），超时默认 `request_timeout_seconds=120.0`、`max_retries=2`（provider_profiles.py:42-43，含重试循环），最坏情况 UI 线程阻塞约 6 分钟，期间整个应用无响应（违背项目"耗时操作不占 UI 线程"的架构约定）。
  - 建议: 移到 QThreadPool/QThread 或复用项目的异步任务队列，完成后经信号回填 `_connection_results`；期间禁用按钮显示"测试中…"。
  - 置信度: 高

- [AUD-19604] [Major] SOURCE_LOCAL 分支未经任何验证即写入 `_connection_results=True`，能力矩阵将显示"连接测试已通过"
  - 位置: src/features/edit/ui/ai_preferences_page.py:1328-1336
  - 证据:
    ```python
    if profile.source_kind == SOURCE_LOCAL:
        self._connection_results[profile.profile_id] = True
        self._refresh_capability_status()
        QMessageBox.information(
            None,
            "Topos",
            "已选择本地模型。实际可用性将在运行时检查模型包和本机依赖，不会发起网络请求。",
        )
        return
    ```
  - 影响: 与本文件自述不变量冲突（170-173 行："profile configuration alone must not be displayed as a reachable service"）。本地 OpenAI 兼容 profile（provider_kind `local_http`、endpoint `http://127.0.0.1:8000/v1`，经 `_infer_source_kind` 归为 SOURCE_LOCAL）具有 http endpoint，capability_status.py:215-218 命中 `connection is True` 分支后展示 `AVAILABLE · 连接测试已通过`——实际未做任何测试（服务未启动也显示通过），误导用户。
  - 建议: 本地分支应存入独立"未验证（本地）"标记（如不写入 `_connection_results`，或写 `None`），能力矩阵对 SOURCE_LOCAL 显示 UNVERIFIED/AVAILABLE 时附"运行时检查"理由，不宣称"测试通过"。
  - 置信度: 高

- [AUD-19605] [Major] 输入中的 API Key 在切换 profile 时被静默丢弃，与其他字段"切换即 flush"行为不一致
  - 位置: src/features/edit/ui/ai_preferences_page.py:1074-1076、878-886、928
  - 证据:
    ```python
    def _on_key_changed(self, text: str) -> None:
        """API key 输入 → 暂存到 _pending_api_key（apply 时才落盘）。"""
        self._pending_api_key = text
    # _on_profile_selected:
    #   self._flush_current_profile()   ← 只 flush 表单字段，不含 pending key
    #   self._current_profile_id = profile_id
    # _load_profile_to_form 末尾（928 行）:
        self._pending_api_key = None
    ```
  - 影响: 用户在 profile A 输入 key 后点击 profile B：A 的显示名/endpoint 等均被 flush 保存，唯独 key 被丢弃且无任何提示；用户以为已保存（占位符文案"留空则保持已保存的 key"强化此预期），实际 A 的 key 丢失，需重输。
  - 建议: `_on_profile_selected` 切换前若 `_pending_api_key` 非空，先对旧 profile 执行 `set_secret`（或弹提示确认丢弃）；至少与其它字段的即时 flush 语义对齐。
  - 置信度: 高（行为可由代码完整推演）

- [AUD-19606] [Major] 删除 profile 时立即从磁盘永久删除已存凭据，与"Apply 才落盘"的提交语义不一致
  - 位置: src/features/edit/ui/ai_preferences_page.py:1291-1299
  - 证据:
    ```python
        profile_id = item.data(Qt.ItemDataRole.UserRole)
        profile = self._registry.get(profile_id)
        # 清理用户级配置文件里的 key
        if profile is not None and profile.credential_ref:
            try:
                self._credential_store.delete_secret(profile.credential_ref)
            except Exception:  # noqa: BLE001
                pass
        self._registry.remove(profile_id)
    ```
  - 影响: profile 删除只改内存 registry、需等 `apply()` 持久化，但 `delete_secret` 立即写盘（FileCredentialStore.delete_secret 直接 save_user_ai_config）且不可逆。用户点了"删除"后取消/不 Apply：profile 仍在，key 已永久丢失（重新打开设置显示"未设置"）。对比同页 `_on_key_changed` 刻意"apply 时才落盘"，提交边界混乱。
  - 建议: 凭据删除延后到 `apply()`（与 registry.remove 同一提交点），或删除前对"将同时删除已存 API key"单独确认。
  - 置信度: 中-高（立即删盘为代码事实；"取消后 profile 仍在"取决于宿主对话框 Cancel 语义，但即便无 Cancel，未 Apply 的破坏性落盘也不符合本页自述约束）

- [AUD-19607] [Major] 每次键入触发两次全量列表重建与两次凭据读盘，性能反模式
  - 位置: src/features/edit/ui/ai_preferences_page.py:974-977、1109-1178（flush 内 `_refresh_capability_status`）、514-519、874-876
  - 证据:
    ```python
    def _on_field_changed(self) -> None:
        """表单字段变化 → 实时更新当前 profile（frozen，整体替换）。"""
        self._flush_current_profile()      # 内部 new!=old 时再调一次 _refresh_capability_status()
        self._refresh_profile_list()       # 末尾又调 _refresh_capability_status()

    # _refresh_capability_status 内（518-519 行）:
        credential_available = bool(
            profile is not None
            and profile.credential_ref
            and self._credential_store.get_secret(profile.credential_ref)
        )
    ```
  - 影响: display_name/endpoint/model_id 每个字符的 textChanged 都会：整表重建 QListWidget + 两次 `_refresh_capability_status`，每次同步 `get_secret`——FileCredentialStore 每次调用都重新读盘解析 `~/.topos/ai_profiles.json`（credential_store.py:228-233），Hybrid 模式未命中时还要走 keyring 安全守护进程往返；中文 IME 组字期 textChanged 频率成倍放大，违反项目 4K 实时性能纪律下的 UI 开销约束。
  - 建议: 键入路径只 flush registry、不重建列表（列表标签用 setData 局部更新）；`_refresh_capability_status` 结果做短 TTL 缓存或去抖（QTimer debounce），`credential_available` 在 profile/凭据变更事件时才重查。
  - 置信度: 高（调用链与读盘行为均经源码核实）

- [AUD-19608] [Suggestion] 模块级 I18N.tr 快照与大段硬编码中文绕过 i18n
  - 位置: src/features/edit/ui/ai_preferences_page.py:80-92（模块级 tr）、117-133、440-444、560、723
  - 证据:
    ```python
    _PROVIDER_OPTIONS = [
        ...
        (
            I18N.tr("preferences.ai_editing.provider_openai", "OpenAI"),
            "openai", "https://api.openai.com", "gpt-4o",
        ),
    ...
    _CATEGORY_OPTIONS = (
        ("language", "语言模型"),
        ("vision", "视觉模型"),
    ```
  - 影响: 80/86/92 行在 import 时求值，语言加载顺序早于 import 或运行时切换语言均不会刷新；`_CATEGORY_OPTIONS`、`_CAPABILITY_OPTIONS`、automation 文案（440-444）、"连接设置"（560）、"服务能力（用于分类和路由）"（723）完全未走 i18n，与其余 `I18N.tr(...)` 混用，多语言界面出现中英夹杂。
  - 建议: 模块级选项表改为惰性求值（函数/首次 create_widget 时构建）；未翻译文案统一补 `I18N.tr` 键。
  - 置信度: 高

- [AUD-19609] [Suggestion] 上下文上限以"k"为单位 round 后回写，静默篡改原始 token 数
  - 位置: src/features/edit/ui/ai_preferences_page.py:918、634-638、1165
  - 证据:
    ```python
    context_spin.setValue(max(1, round(profile.limits.max_context_tokens / 1_000)))
    ...
    context_spin.setRange(1, 2_000)
    ...
    limits=ProviderLimits(
        max_context_tokens=int(context_spin.value()) * 1_000,
    ```
  - 影响: 任一次 `_flush_current_profile`（任意字段键入即触发）都会把非整千的 `max_context_tokens`（如 153600 → 154000）改写；>2,000,000 的值被 QSpinBox 钳到 2,000k 后回写缩水。用户未编辑该字段，配置仍被静默变更。
  - 建议: flush 时若表单值与 profile 原值等值（round-trip 一致）则保留原值；或以精确 token 为内部单位、仅显示层取整。
  - 置信度: 高（行为确定；触发面广）

- [AUD-19610] [Suggestion] MCP 勾选回调 `except Exception: pass` 静默吞掉网关启停失败
  - 位置: src/features/edit/ui/ai_preferences_page.py:419-431
  - 证据:
    ```python
        try:
            from src.features.edit.ai_editing.mcp_vision_gateway import (
                get_mcp_vision_gateway,
            )
            get_mcp_vision_gateway().set_server_enabled(profile_id, enabled)
        except Exception:  # noqa: BLE001 — UI 回调不抛
            pass
    ```
  - 影响: 违反".clinerules 不得宽泛 except 掩盖领域错误"：网关启停失败（含 import 失败、内部状态错误）被吞，勾选框停留在用户点击的状态、与网关真实启停状态脱节，且无任何反馈；用户以为工具已对 Agent 隐藏/暴露。
  - 建议: 捕获后回滚勾选状态（blockSignals + setCheckState）并经状态栏/ QMessageBox 提示失败原因；至少记录日志。
  - 置信度: 高

- [AUD-19611] [Suggestion] `_PROVIDER_OPTIONS` 中 "openai" 值重复，智谱 GLM 条目的 placeholder 永不可达
  - 位置: src/features/edit/ui/ai_preferences_page.py:97-103、1211-1217
  - 证据:
    ```python
    (
        "智谱 GLM Coding",
        "openai",
        "https://open.bigmodel.cn/api/coding/paas/v4",
        "glm-5.2",
    ),
    ...
    def _update_placeholders(self, provider_kind: str) -> None:
        for _label, value, url, model in _PROVIDER_OPTIONS:
            if value == provider_kind:
                self._widgets["endpoint"].setPlaceholderText(url)
                self._widgets["model_id"].setPlaceholderText(model)
                return
    ```
  - 影响: 按 value 首个命中即返回，协议选 "智谱 GLM Coding"（openai）时 placeholder 永远显示 OpenAI 的 `https://api.openai.com` / `gpt-4o`，第 5 条的 GLM 提示成死数据。
  - 建议: 给每个条目独立 value（如 `openai` / `zhipu_glm`），或按当前索引而非 value 匹配 placeholder。
  - 置信度: 高

- [AUD-19612] [Suggestion] `dict[str, QWidget]` 存取控件 + 密集 `type: ignore[assignment]`，类型安全持续侵蚀
  - 位置: src/features/edit/ui/ai_preferences_page.py:234、899-906、1116-1122、1186、1287
  - 证据:
    ```python
        self._widgets["profile_list"] = QListWidget()
        lst: QListWidget = self._widgets["profile_list"]  # type: ignore[assignment]
    ...
        kind_combo: ChevronComboBox = self._widgets["provider_kind"]  # type: ignore[assignment]
        source_combo: ChevronComboBox = self._widgets["source_kind"]  # type: ignore[assignment]
    ```
  - 影响: 本文件 10+ 处 ignore 强转；键名拼错或控件类型不符只能在运行时暴露（`_refresh_profile_list` 就不得不防御 `if lst is None`）。
  - 建议: 拆为显式类型属性（`self._kind_combo: ChevronComboBox`）或 TypedDict 描述 `_widgets`，消除 ignore。
  - 置信度: 高

### 通过项
- 信号防递归：blockSignals 使用全面且正确——`_load_profile_to_form`/`_clear_form` 表单回填、`_load_automation_flags`、`_sync_active_button`、`_refresh_profile_list` 选中（含注释说明的竞态规避）、`_refresh_mcp_section`/`_refresh_capability_status` 列表回填均成对屏蔽
- 凭据安全约束：key 以 EchoMode.Password 显示、界面永不回显、仅显示"已设置/未设置"；`_connection_results` 只存 bool/str 不含 secret（注释与实现一致）；勾选 keychain/文件存储读双后端不丢 key
- 状态提交一致性（profile 字段）：`_flush_current_profile` frozen 整体替换、编辑后清除过期连接结果（1173-1177）正确
- 输入验证：测试连接前检查 endpoint/model_id 缺失、ASR/TTS/图/视频类能力无 key 时拒绝并说明不消耗额度；SOURCE_LOCAL 明确不发网络请求
- feature flags：automation 三键默认 False，与 DEFAULT_FEATURE_FLAGS M0 冻结一致；`_save_automation_flags` 先拷贝既有 features dict 再覆盖，不冲掉其它 flag
- 线程安全（除 19603 阻塞外）：页面为普通 Python 类，回调均在 UI 线程；`_connection_results` 仅 UI 线程读写
- GPU 资源生命周期 / GLSL·HDR：不涉及

---

## src/features/edit/ui/ai_result_card.py (行数: 191)

### 发现

- [AUD-19613] [Suggestion] 富文本标签未转义 AI 侧来源的 title/status 文本
  - 位置: src/features/edit/ui/ai_result_card.py:101-103、110-113
  - 证据:
    ```python
        title = QLabel(f"<b>{self._state.title}</b>")
        title.setTextFormat(Qt.TextFormat.RichText)
    ...
        status = QLabel(
            f"<span style='color:{status_color}'>{self._state.status_label}</span>"
        )
        status.setTextFormat(Qt.TextFormat.RichText)
    ```
  - 影响: `ResultCardState` 由 controller 从 RunRecord/artifact 聚合，title/summary 常含模型生成文本；含 `<`/`&` 时 RichText 解析吞字或错排（外部数据未验证维度的轻量违例；无脚本执行风险）。
  - 建议: 动态段 `html.escape()` 后再拼富文本。
  - 置信度: 高（代码事实）；影响低

- [AUD-19614] [Suggestion] `_KIND_LABELS` 硬编码中文不走 i18n
  - 位置: src/features/edit/ui/ai_result_card.py:50-59
  - 证据:
    ```python
    _KIND_LABELS = {
        "variant": "方案",
        "subtitle_draft": "字幕",
        "script": "脚本",
        "evidence": "证据",
        "generated_media": "生成素材",
        "qa_report": "QA",
        "error": "错误",
        "info": "信息",
    }
    ```
  - 影响: 徽标文案固定中文，英文/日文等 locale 下与其余 `tr(...)` 文案混杂。
  - 建议: 改为惰性 `I18N.tr(f"ai_result.kind.{k}", ...)` 查表。
  - 置信度: 高

### 通过项
- 核心约束达成：按钮 enabled 严格跟随 `action.enabled`、禁用时 tooltip 给 `disabled_reason`；requires_permission 有提示——未审批 proposal 不会呈现为可应用
- 信任标注：`trust != "trusted"` 时显示徽标，untrusted 的 apply 禁用由 state 层决定、UI 只渲染，符合"UI 不做决策"
- 信号防递归：静态卡无程序化回填；闭包用默认参捕获 `action.kind`，无迟绑定 bug
- 线程安全 / GPU 生命周期 / GLSL·HDR：不涉及
- 类型注解：完整

---

## src/features/edit/ui/ai_skill_card.py (行数: 360)

### 发现

- [AUD-19615] [Suggestion] number/required 表单字段缺输入校验，form_values 返回类型不一致
  - 位置: src/features/edit/ui/ai_skill_card.py:211-215、197、239-250、278-281
  - 证据:
    ```python
        if f.field_type == "number":
            w = QLineEdit()
            if f.default is not None:
                w.setText(str(f.default))
            return w
    ...
            elif isinstance(w, QLineEdit):
                txt = w.text().strip()
                values[name] = txt if txt else None
    ...
    def _on_start(self) -> None:
        if self._updating:
            return
        self.start_requested.emit(self._view.skill_id, self.form_values())
    ```
  - 影响: number 字段无 QDoubleValidator、可提交任意文本（下游 `float(...)` 抛错）；required 字段仅以 `" *"` 标注，为空（映射 None）也能点"开始"直接发 start_requested；返回值 int/str/None 混杂，消费方需按字段名猜测类型。
  - 建议: number 用 QDoubleSpinBox 或加 validator；`_on_start` 对 required 字段为 None 时禁用/提示；`form_values` 按 field_type 归一化类型。
  - 置信度: 高（行为确定；下游是否兜底未知，故定 Suggestion）

- [AUD-19616] [Suggestion] set_form_values 逐字段 `except Exception: pass` 吞掉类型错误
  - 位置: src/features/edit/ui/ai_skill_card.py:264-274
  - 证据:
    ```python
            try:
                if isinstance(w, QSpinBox):
                    w.setValue(int(val))
                elif isinstance(w, QComboBox):
                    idx = w.findText(str(val))
                    if idx >= 0:
                        w.setCurrentIndex(idx)
                elif isinstance(w, QLineEdit):
                    w.setText(str(val))
            except Exception:  # noqa: BLE001
                pass
    ```
  - 影响: 违反铁律"不得宽泛 except 掩盖领域错误"：坏值（如 "abc"）静默跳过，P1.5 adjust_params 回填悄悄丢字段，用户无从得知表单未按原 invocation 预填。
  - 建议: 收窄为 `(TypeError, ValueError)` 并对失败字段标红/记录日志。
  - 置信度: 高

- [AUD-19617] [Suggestion] `AiSkillCard._updating` 是永不生效的死守卫；`is_ready` 与开始按钮判定不一致
  - 位置: src/features/edit/ui/ai_skill_card.py:78、158-159、278-281、287-289
  - 证据:
    ```python
        self._updating = False          # __init__，此后无任何置 True 的路径
    ...
        ready = self._view.prereq.ready if self._view.prereq else True
        self._start_btn.setEnabled(ready)
    ...
        def is_ready(self) -> bool:
            return bool(self._view.prereq and self._view.prereq.ready)
    ```
  - 影响: `_on_start` 的 `if self._updating: return` 永不触发（死代码、虚假防护感）；prereq 为 None 时按钮可点但 `is_ready` 返回 False，两个"就绪"口径矛盾，调用方易误判。
  - 建议: 删除死守卫或让 `set_form_values` 真正置位；统一 `is_ready = prereq is None or prereq.ready`（与按钮一致）。
  - 置信度: 高

- [AUD-19618] [Suggestion] `_build_form(self, fields)` 参数缺类型注解
  - 位置: src/features/edit/ui/ai_skill_card.py:189
  - 证据:
    ```python
    def _build_form(self, fields) -> QWidget:
        host = QWidget()
        form = QFormLayout(host)
    ```
  - 影响: 本文件其余签名均有注解，此处 `fields`（实为 `Iterable[FormField]`）裸奔，类型检查不覆盖。
  - 建议: 标注为 `fields: Iterable[FormField]`。
  - 置信度: 高

### 通过项
- P1.2 验收达成：前置条件清单逐项 ✓/✗ 展示；未满足时"开始"禁用且 tooltip 列出 blocking_messages
- 信号防递归：`AiSkillGallery.set_cards` 重建期间 `_updating` 锁正确；卡片 start_requested 仅向上转发；`_apply_preset` 填表时 blockSignals 成对
- 闭包捕获：`lambda _, ak=action.kind`（ai_result_card 同模式）正确
- 线程安全 / GPU 生命周期 / GLSL·HDR / 状态提交：不涉及（只发 start_requested，不执行模型/不写 timeline）
- 性能：integer 字段用 QSpinBox 且 min/max 映射正确；枚举默认值 `if f.default in f.options` 越界保护

---

## src/features/edit/ui/ai_timeline_diff.py (行数: 250)

### 发现

- [AUD-19619] [Major] `set_diff` 读取的 `warnings` 从未使用，预检警告在 diff 视图中被整体丢弃
  - 位置: src/features/edit/ui/ai_timeline_diff.py:145-159
  - 证据:
    ```python
            added = diff.get("added", [])
            removed = diff.get("removed", [])
            moved = diff.get("moved", [])
            trimmed = diff.get("trimmed", [])
            warnings = diff.get("warnings", [])      # ← 读取后全文再无引用
    ...
            for entry in trimmed:
                self._add_entry(entry, high_risk=False)
    ```
  - 影响: `TimelineDiff.to_dict()`（transaction_service.py:125-131）明确输出 warnings（含音乐版权、降级等预检提示），本面板读入即弃；grep 全仓无其它消费者使用 `AiTimelineDiffPanel`，src 内没有任何界面展示这些警告——用户在只读 diff 视图看不到预检警告，违背模块自述"确认 dialog 显示 …预检 warning"链路的前半段（dialog 的 warnings 形参也无来源填充）。
  - 建议: 在 summary 或条目区渲染 warnings（高风险样式），并将 `diff["warnings"]` 透传给 `AiApplyConfirmationDialog`。
  - 置信度: 高（未使用变量与无调用方均经 grep 核实）

- [AUD-19620] [Suggestion] 高风险置顶仅覆盖 removed，与文档承诺的"替换/音乐版权/降级 effect"不符
  - 位置: src/features/edit/ui/ai_timeline_diff.py:3-4、151-153
  - 证据:
    ```python
    按视频/音频/字幕轨显示 operation；高风险项（delete、替换、音乐版权、降级 effect）
    置顶（TAEP §11.6 步骤 10）。…
    ...
            # 高风险项（removed）置顶
            for entry in removed:
                self._add_entry(entry, high_risk=True)
    ```
  - 影响: ClipDiff.to_dict 无 risk 字段，"替换"（remove+add 对）只有 removed 半边标红、"音乐版权/降级 effect"警告（在 warnings 里，见 19619）完全无法置顶标红，高风险提示覆盖面低于设计承诺。
  - 建议: diff schema 增加 `high_risk`/`risk_reason`（由 compute_diff 依据 operation_summaries 标注），UI 按 schema 渲染而非仅看 removed。
  - 置信度: 中（规格差距明确；to_dict 现状已核实）

- [AUD-19621] [Suggestion] 函数内局部 import QPushButton；`_on_apply` 不防空 variant_id
  - 位置: src/features/edit/ui/ai_timeline_diff.py:123、192-195
  - 证据:
    ```python
        from PyQt6.QtWidgets import QPushButton
        self._apply_btn = QPushButton(tr("ai_diff.apply", "应用方案…"))
    ...
    def _on_apply(self) -> None:
        if self._updating:
            return
        self.apply_requested.emit(self._variant_id)
    ```
  - 影响: QPushButton 应在模块头导入（本文件已从 PyQt6 导入 8 个控件，唯独它放函数内）；`set_diff(variant_id="")` 后按钮仍可点，controller 收到 `apply_requested("")` 无法定位方案。
  - 建议: 移到模块 imports；`variant_id` 为空时禁用 `_apply_btn`。
  - 置信度: 高

### 通过项
- 状态提交一致性（revision 门禁）：`stale=True` 显示过期提示并禁用应用；`has_errors` 禁用应用——base revision 改变禁止提交的约束达成
- 信号防递归：`set_diff` 重建期间 `_updating` 锁正确，`_on_apply` 有守卫
- 确认对话框：显示 undo 组名/影响 clip 数/删除数/预检 warning，`approved` 仅经 Ok 置位（一次性 token 语义交 controller）
- 线程安全 / GPU 生命周期 / GLSL·HDR：不涉及
- 类型注解：完整

---

## src/features/edit/ui/ai_variant_list.py (行数: 164)

### 发现

- [AUD-19622] [Major] 硬编码 24.0 fps 做帧→秒换算，非 24fps 工程时长显示错误
  - 位置: src/features/edit/ui/ai_variant_list.py:60
  - 证据:
    ```python
        # 时长
        dur_s = round(card.duration_frames / 24.0, 1) if card.duration_frames else 0
        layout.addWidget(QLabel(tr("ai_variant.duration", "时长: {s}s", s=dur_s)))
    ```
  - 影响: `VariantCard.duration_frames` 是帧数（assistant_service.py:385-387 由 beat target_range 累加），而项目 fps 明确为可变 Rational（ai_editing/__init__.py:14 "fps 保存为 Rational（如 24000/1001）"；analysis_models.py:365 fps 为参数）。30fps 工程显示偏慢 20%、NTSC 24000/1001 也有偏差——方案对比核心指标（时长）失真，直接影响用户 A/B 选择。
  - 建议: VariantCard/快照携带 fps（Rational），UI 用其换算；或展示帧数 + 由 controller 换算好的秒数。
  - 置信度: 高

- [AUD-19623] [Suggestion] 超过 MAX_VISIBLE 的 variant 被静默截断且无任何提示
  - 位置: src/features/edit/ui/ai_variant_list.py:111、151-152
  - 证据:
    ```python
        MAX_VISIBLE = 3
    ...
            visible = variants[: self.MAX_VISIBLE]
            self._empty_label.setVisible(len(visible) == 0)
    ```
  - 影响: 若上游给出 >3 个方案，第 4 个起无声消失，用户不知道存在被隐藏的候选（TAEP 要求"至少两个可比较方案"，但未禁止更多）。
  - 建议: 截断时显示"还有 N 个方案未展示"提示行。
  - 置信度: 高（行为确定）；影响低

### 通过项
- 信号防递归：`set_variants` 重建期间 `_updating` 锁正确；卡片 selected/preview/compare 仅向上转发，不回写
- 约束达成：不直接 edit clip，只发 signal
- 线程安全 / GPU 生命周期 / GLSL·HDR / 状态提交：不涉及
- 输入验证：beat_coverage×100、pace round 显示性换算，无自由输入
- 类型注解：完整

---

## 统计

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 7 | AUD-19603, 19604, 19605, 19606, 19607, 19619, 19622 |
| Suggestion | 17 | AUD-19600–19602, 19608–19618, 19620, 19621, 19623 |

无 Critical（未发现可致崩溃/数据丢失/渲染错误的路径；19606 的 key 删除属可重录入的用户凭据，故定 Major）。
