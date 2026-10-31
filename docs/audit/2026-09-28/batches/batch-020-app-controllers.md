# batch-020 审查报告（src/app/controllers/ai_edit_controller.py）

编号区间：AUD-2000–AUD-2099（实际使用 AUD-2000–AUD-2016）。
审查方式：全文 6,424 行逐段 Read 通读；关键外部事实（panel `_updating` 锁、
adapter plan observer 调用线程、bridge `get_project_sequences` 语义、
`HarnessRuntime.cancel` 未知 run 行为、session 标题脱敏）经只读 grep/sed 验证。

## src/app/controllers/ai_edit_controller.py (行数: 6424)

### 发现

- [AUD-2000] [Critical] `_PlanWorker` 对 `on_state_changed` 的非原子摘除/恢复在规划 worker 重叠时造成跨线程 UI 回填竞态
  - 位置: src/app/controllers/ai_edit_controller.py:146（run 内交换），5000（重叠触发点）
  - 证据:
    ```python
    def run(self) -> None:
        # 暂时摘掉 service 的状态回调：worker 线程内不直接回填 UI，
        # 由 finished 信号在主线程统一触发一次 refresh。
        original_cb = self._service.on_state_changed
        self._service.on_state_changed = None
        try:
            self._service.plan_variants(self._index, self._config)
            ...
        finally:
            self._service.on_state_changed = original_cb
    ```
    ```python
    # _start_plan：旧 worker 未结束（wait 超时）即启动新 worker
    if self._plan_worker is not None and self._plan_worker.isRunning():
        self._plan_worker.cancel_requested = True
        self._plan_worker.quit()
        self._plan_worker.wait(2000)
    ```
  - 影响: 旧 worker 的 HTTP 调用超过 2 秒（代码注释自认 LLM 请求可能数十秒）时，新 worker 已在其 run() 中把回调置 None；旧 worker 的 `finally` 随后把 `_on_service_state` 恢复回去——新 worker 线程内 `plan_variants` 触发的状态变化会直接在 worker 线程调用 `self._panel.refresh_state(snapshot)`（跨线程操作 QWidget，可致崩溃）。另一排列下，新 worker 的 `finally` 把回调恢复为其捕获的 `None`，状态回填从此永久失效。dispose() 期间 wait 超时也会让旧 worker 的 finally 在 `self._service.on_state_changed = None`（740 行）之后复活旧回调。
  - 建议: 不再用“摘回调”方式：让 `_PlanWorker` 持一个线程安全的“挂起中”标志，`_on_service_state` 检查标志丢弃 worker 线程回调；或 service 的状态回调改为 Qt 信号（emit 跨线程自动排队到主线程）。
  - 置信度: 中（机制由代码直接可证；触发需旧规划存活超过 2 秒 wait，属注释自认的常见场景）

- [AUD-2001] [Major] `_start_chat` 多模态校验失败早退路径遗留永不终结的 Run Drawer "running" 活动项
  - 位置: src/app/controllers/ai_edit_controller.py:1958（begin），2016（早退）
  - 证据:
    ```python
    self._begin_legacy_chat_activity(
        self._chat_activity_label(chat_mode, "prepare", system_instruction),
    )
    ...
    profile = self._chat_profile_for_mode(chat_mode)
    if profile is None:
        self._on_chat_failed("当前没有可用的 AI 模型，请先配置并连接模型。")
        return
    if attachments and not self._validate_chat_attachments(
        attachments, profile=profile,
    ):
        self._pending_chat_attachments = attachments
        return
    ```
  - 影响: `profile is None` 分支经 `_on_chat_failed` → `_finish_legacy_chat_activity(..., status="failed")` 收尾；但多模态校验失败分支直接 return，既不 `_finish_legacy_chat_activity` 也不 `_discard_legacy_chat_activity`。`_legacy_chat_activity_item` 停留在 status="running"、can_cancel=True，面板顶部活动行与 Run Drawer 出现一个无法完成也无法有效取消的幽灵“正在读取并整理项目上下文”条目，直到用户下次发起新聊天才被覆盖。
  - 建议: 该早退分支补 `self._finish_legacy_chat_activity("已阻止", status="cancelled")`（或等价收尾）后再 return。
  - 置信度: 高

- [AUD-2002] [Major] 非 chat 模式请求早退后 `_pending_chat_attachments` 跨请求残留，会把旧附件泄漏进后续 feature 发起的隔离 LLM 请求
  - 位置: src/app/controllers/ai_edit_controller.py:893（暂存），947-961（媒体模式早退），1975-1976 + 2000（消费）
  - 证据:
    ```python
    # 多模态：暂存附件供 _start_chat 消费（仅内存，不持久化为字节）。
    self._pending_chat_attachments = attachments
    ...
    if mode in get_media_assistant_modes():
        ...
            self._request_ai_generation(result, scope, text)
        ...
        return
    ```
    ```python
    attachments = self._pending_chat_attachments
    self._pending_chat_attachments = ()  # 消费即清空，避免跨请求残留
    ...
    draft_attachments=attachments if isolated_artifact else (),
    ```
  - 影响: 媒体生成 / Motion Graphic / Teaching 等“不经 `_start_chat`”的模式分支直接 return，附件既被这些路径完全忽略（用户以为图片参与了生成），又残留在 `_pending_chat_attachments`。之后 feature 模块调用 `start_html_generate_chat` / `start_motion_graphic_build_chat`（与用户下一次输入无关）时，`_start_chat` 会把陈旧附件当作本次请求的附件消费，注入隔离 artifact 消息（`draft_attachments`）并重新注册 MCP 图片，违反 526-527 行声明的“避免跨请求残留”不变量；多模态内容可能被发给与用户意图无关的请求。
  - 建议: 所有非 chat 模式分支 return 前清空 `self._pending_chat_attachments = ()`；或在 `_request_ai_generation` 等 entry 显式消费/拒绝附件并提示用户。
  - 置信度: 高

- [AUD-2003] [Major] `_on_chat_cancelled` 对 `teaching_html_generate` 未终结 V2 Motion Graphic run，与 `_on_chat_failed` 不对称，留下幽灵 running 行
  - 位置: src/app/controllers/ai_edit_controller.py:3956（cancel 路径），3913-3914（fail 路径对照）
  - 证据:
    ```python
    if self._chat_mode == "teaching_html_generate":
        request_id = self._teaching_html_generate_request_id
        handler = self._teaching_html_generate_response_handler
        if callable(handler) and request_id:
            try:
                handler(request_id, "")
            except Exception:  # noqa: BLE001
                logger.debug("HTML generation cancel notification failed", exc_info=True)
    ```
    对照 `_on_chat_failed`:
    ```python
        if self._chat_mode == "teaching_html_generate":
            ...
            # B3: 标记 Motion Graphic run 为失败。
            self.complete_motion_graphic_run(request_id, success=False)
    ```
  - 影响: `start_html_generate_chat` 创建的 V2 AgentRun（`_v2_mg_runs[request_id]`）在用户取消后不会被 `complete_motion_graphic_run` 终结，Run Drawer 中该 run 永远显示 running（`start_motion_graphic_run` 注释 2430-2434 行明确担心这种“phantom running row”）。只有用户复用同一 request_id 重试或工程切换时才被清掉。
  - 建议: `_on_chat_cancelled` 的 teaching_html_generate 分支补 `self.complete_motion_graphic_run(request_id, success=False)`。
  - 置信度: 高

- [AUD-2004] [Major] dispose() 不清 `_chat_worker`/`_plan_worker` 引用、不断开 worker 信号，排队信号在 dispose 后仍向共享 panel 发布旧会话状态
  - 位置: src/app/controllers/ai_edit_controller.py:648-657（cancel+wait），740（service 回调置 None），3940-3946（迟到槽）
  - 证据:
    ```python
    for worker in (
        self._plan_worker,
        getattr(self, "_chat_worker", None),
        getattr(self, "_subtitle_plan_worker", None),
        *getattr(self, "_motion_graphic_postprocess_workers", {}).values(),
    ):
        if worker is not None and worker.isRunning():
            worker.cancel_requested = True
            worker.quit()
            worker.wait(2000)
    ...
    self._service.on_state_changed = None
    ```
    ```python
    def _on_chat_cancelled(self, worker: QThread | None = None) -> None:
        if worker is not None and worker is not self._chat_worker:
            return
        partial_reply = self._streaming_reply_text
        self._finish_legacy_chat_activity("已取消", status="cancelled")
    ```
  - 影响: dispose 从不把 `self._chat_worker`/`self._plan_worker` 置 None、也不 disconnect worker 的 lambda 信号。wait(2000) 期间或在途 HTTP 返回后，worker 的 `cancelled`/`failed`/`finished_reply` 以 queued connection 在 dispose 之后投递；身份检查 `worker is not self._chat_worker` 依旧通过（引用未变），`_on_chat_cancelled`/`_on_chat_finished` 继续在旧 controller 上执行：向**共享 panel**（docstring 自述 panel 跨 controller 复用）发布 `set_run_drawer`/`set_activity_status`/`preserve_cancelled_streaming_reply(旧会话部分回复)`——正是 dispose 想避免的“新旧会话同时处理”。另外 dispose 也没有移除 `runtime.add_subscriber(self._on_run_event)`（632-636 行注册）。
  - 建议: dispose 中在 wait 后将 `_chat_worker`/`_plan_worker`/`_subtitle_plan_worker` 置 None（使身份检查丢弃迟到信号），并对三个 worker 的信号 disconnect；runtime 存在 `remove_subscriber` 时移除订阅。
  - 置信度: 高

- [AUD-2005] [Major] `_ChatWorker`/`_PlanWorker`/`_SubtitlePlanWorker` 以 `parent=self` 创建且从不 deleteLater——worker 对象及其持有的完整消息数组（含图片 data URL）在 controller 生命周期内泄漏
  - 位置: src/app/controllers/ai_edit_controller.py:2082-2112（chat），5016（plan），1276-1281（subtitle），3825（MG worker 有 deleteLater 的对照）
  - 证据:
    ```python
    worker = _ChatWorker(
        client,
        messages,
        ...
        stream=chat_mode != "teaching_html_generate",
        parent=self,
    )
    ...
    self._chat_worker = worker
    self._update_legacy_chat_activity(self._chat_activity_reply_label)
    worker.start()
    ```
    对照 Motion Graphic worker 的正确清理:
    ```python
    worker.finished.connect(worker.deleteLater)
    ```
  - 影响: 三个 worker 都是 QThread 且 parent 为 controller，run() 结束后对象不销毁、`_messages`（`_build_chat_messages` 构造的完整历史，多模态消息含 base64 图片 data URL，单附件可达数 MB）与 `_service`/`_index`/`_state` 引用全部随 worker 存活到 controller 死亡。长会话中每次聊天泄漏一个 worker，内存无界增长。
  - 建议: 与 MG worker 一致：`worker.finished.connect(worker.deleteLater)`；并在替换/取消旧 worker 时同步置 None（与 AUD-2004 联动）。
  - 置信度: 高

- [AUD-2006] [Major] Run Drawer 取消按钮对 V2 chat run 完全无效（`_on_run_cancel_requested` 只调 HarnessRuntime.cancel，未知 id 静默返回 False）
  - 位置: src/app/controllers/ai_edit_controller.py:6112-6137；对照 src/features/ai_harness/runtime.py:352-362
  - 证据:
    ```python
    def _on_run_cancel_requested(self, item_id: str) -> None:
        ...
        self._v2_complete_media_generation(
            item_id, success=False, reason="用户取消",
        )
        if item_id in self._materialization_job_ids:
            ...
            return
        if self.runtime is None:
            return
        try:
            self.runtime.cancel(item_id)
        except Exception:  # noqa: BLE001
            pass
    ```
    runtime 侧（已核验）:
    ```python
    state = self._runs.get(run_id)
    if state is None:
        return False
    ```
  - 影响: V2 drawer 条目（`_update_v2_drawer_from_event` run.created，3033-3038 行）以 `can_cancel=True` 展示，但 V2 run 不在 HarnessRuntime 中，`runtime.cancel` 返回 False 且被吞掉；方法也没有 `_v2_orchestrator.request_cancel(...)` 分支。用户点该条目的取消按钮没有任何效果，run 继续执行（面板级 `_on_cancel` 的 `request_cancel_for_session("default")` 才有效，粒度不符）。
  - 建议: `_on_run_cancel_requested` 增加 V2 分支：`self._v2_orchestrator.request_cancel(item_id, reason="user_requested")`（或按 run 归属分派到对应 orchestrator/runtime）。
  - 置信度: 高

- [AUD-2007] [Major] `_SubtitlePlanWorker` 在后台线程读取 live `ProjectSequences`（bridge 不做快照），与主线程时间线编辑并发
  - 位置: src/app/controllers/ai_edit_controller.py:1243（取 state），1276-1286（跨线程传入）；src/app/controllers/ai_edit_host_bridge_impl.py:294-306（已核验仅浅拷贝顶层 dict）
  - 证据:
    ```python
    state = self._bridge.get_project_sequences()
    ...
    worker = _SubtitlePlanWorker(
        client, text, state, project_id=project_id, timeline_id=timeline_id, parent=self,
    )
    ```
    bridge 实现（核验）:
    ```python
    def get_project_sequences(self) -> Any:
        ...
        return ProjectSequences(project=project, sequences=dict(sequences))
    ```
  - 影响: `dict(sequences)` 只复制顶层映射，project 与各 Sequence/Track 对象是活引用。`SubtitleEditPlanner.plan` 在 QThread 中遍历 `sequence.all_tracks`/cues（含 LLM 调用前后的多次读取，历时数秒），期间用户在主线程增删轨道/字幕可触发 "dict changed size during iteration" 类异常（worker 捕获后以误导性的“AI 字幕编辑未生成计划”失败）或读到半更新状态。`_on_subtitle_plan_ready` 在主线程重新取 state 预览，但规划输入本身已可能不一致。
  - 建议: `_start_subtitle_edit_agent` 传入深快照（或 bridge 提供 snapshot 变体，序列化-反序列化 / 拷贝轨道结构），规划仅基于快照。
  - 置信度: 中（GIL 下无内存损坏；并发修改导致的迭代异常/脏读为真实风险，触发依赖用户同时编辑）

- [AUD-2008] [Major] dispose() 对每个 Motion Graphic postprocess worker 串行 `wait(2000)`，Chromium 后处理不可中断，工程切换 UI 冻结可达 2s×N
  - 位置: src/app/controllers/ai_edit_controller.py:648-657
  - 证据:
    ```python
    for worker in (
        self._plan_worker,
        getattr(self, "_chat_worker", None),
        getattr(self, "_subtitle_plan_worker", None),
        *getattr(self, "_motion_graphic_postprocess_workers", {}).values(),
    ):
        if worker is not None and worker.isRunning():
            worker.cancel_requested = True
            worker.quit()
            worker.wait(2000)
    ```
  - 影响: `_MotionGraphicPostprocessWorker.cancel_requested` 只在 processor 返回后检查（268 行），浏览器验证/storyboard 无法中断，`wait(2000)` 几乎必然超时；多个待处理 request 时（`_motion_graphic_postprocess_workers` 是 dict，允许并存）主线程串行冻结 2 秒/个，工程切换出现多秒无响应。
  - 建议: MG worker 不做同步 wait（置 cancel 标志后立即返回，靠 AUD-2004 的信号丢弃兜底）；或统一一个总超时预算而不是每 worker 2 秒。
  - 置信度: 中（机制确定；实际冻结时长取决于并发 MG 任务数）

- [AUD-2009] [Suggestion] `start_chat_run` 用默认 `RunPolicy()` 而非 `_current_permission`，违反 footer 权限“只作用于后续 invocation”的声明语义
  - 位置: src/app/controllers/ai_edit_controller.py:4966-4977；对照 1416-1424（`_request_ai_generation` 正确使用）
  - 证据:
    ```python
    inv = Invocation(
        source=InvocationSource.CHAT,
        skill_id=skill_id,
        scope=InvocationScope(project_id=project_id or "default"),
        policy=RunPolicy(),
        inputs={...},
        user_text=user_text,
    )
    ```
  - 影响: `_on_composer_permission_changed`（6017-6026 行）声明权限“只作用于后续 invocation”，`_execute_skill_route`/`_request_ai_generation` 都经 `_current_permission` 派生 policy，唯独 `start_chat_run` 硬编码默认值；用户选 read_only 后经此入口的 invocation 仍按默认权限创建，权限口径不一致。
  - 建议: 改为 `policy=self._current_policy()`（1788 行已有现成方法）。
  - 置信度: 中（取决于该入口的实际调用方，但语义不一致本身成立）

- [AUD-2010] [Suggestion] `teaching_scene_edit` 响应 handler 返回 False 时穿透普通路径，追加占位消息后又把原始 JSON 追加为 assistant 消息
  - 位置: src/app/controllers/ai_edit_controller.py:3719-3728（占位 + 条件 return），3791-3798（穿透追加）
  - 证据:
    ```python
    self._service.append_chat_message(
        "assistant", "已读取当前场景上下文，生成了一份待确认的修改计划。",
    )
    if handler(original, text):
        self._record_assistant_message("已生成场景修改计划，等待确认。")
        return
    ...
    self._chat_mode = "normal"
    ...
    self._service.append_chat_message("assistant", text)
    self._record_assistant_message(text)
    ```
  - 影响: handler 返回 False（JSON 校验失败/无计划）时未 return，落入普通分支：用户先看到“生成了一份待确认的修改计划”的占位消息，紧接着又收到一条未解析的模型原始 JSON 文本——既自相矛盾，又把内部传输契约（JSON schema 指令的输出）直接暴露给用户。
  - 建议: handler 返回 False 时给出明确失败提示并 return，不把原始 JSON 当对话正文展示。
  - 置信度: 高

- [AUD-2011] [Suggestion] `_invoke_teaching_request_handler` 用捕获 TypeError 探测旧签名，会掩盖 handler 内部真实的 TypeError
  - 位置: src/app/controllers/ai_edit_controller.py:3553-3563
  - 证据:
    ```python
    try:
        return bool(handler(text, target_asset_ids=target_asset_ids))
    except TypeError:
        # Compatibility with older optional feature handlers and test doubles
        # that still expose the original ``handler(text)`` contract.
        return bool(handler(text))
    ```
  - 影响: 若新签名 handler 内部代码抛出 TypeError（如 None 拼接、参数个数错误的深层调用），会被误判为“旧签名”并再用 `handler(text)` 调一次——错误被吞、同一 handler 执行两次（若第一次已产生部分副作用则重复）。铁律“不得宽泛 except 掩盖领域错误”同类风险。
  - 建议: 用 `inspect.signature` 预探测形参，或规定 handler 统一签名后移除兼容层。
  - 置信度: 高

- [AUD-2012] [Suggestion] 以魔法字符串比较宿主错误消息决定降级路径
  - 位置: src/app/controllers/ai_edit_controller.py:4327-4330
  - 证据:
    ```python
    # 异步接缝存在但暂时不可用时，继续走同步兼容路径。
    if message and message != "AI 生成后台物化宿主尚未初始化":
        self._show_generated_notice(message, warning=True)
        return
    ```
  - 影响: controller 与宿主之间用展示文案做协议判断：宿主侧文案改动（i18n、标点）即静默破坏降级分支；且该中文文案与错误用户可见文案耦合。
  - 建议: starter 返回结构化状态（如 `(ok, code, message, job_id)`），按 code 判断“宿主未装配”。
  - 置信度: 高

- [AUD-2013] [Suggestion] `_teaching_html_generate_system_prompt` 是只写不读的死状态属性
  - 位置: src/app/controllers/ai_edit_controller.py:3670-3671（唯一赋非空值处），3691/3756/3912/3968（清空）
  - 证据:
    ```python
    self._teaching_html_generate_request_id = str(request_id)
    self._teaching_html_generate_system_prompt = prompt
    ```
    （全文件检索仅有 5 处赋值，无任何读取点）
  - 影响: 死状态增加误解面（读者以为 system prompt 会被回读/重放）；`__init__` 也未声明该属性，对象上动态出现/消失。
  - 建议: 删除该属性（transport 只需 request_id），或在 `__init__` 声明并真正使用。
  - 置信度: 高

- [AUD-2014] [Suggestion] 每次 service 状态变化都在 UI 线程全量重建聊天历史并估算 token
  - 位置: src/app/controllers/ai_edit_controller.py:815-822（`_on_service_state` → `_refresh_composer_footer`），5921-5927（全量重建），3523-3525（limit=100_000）
  - 证据:
    ```python
    def _on_service_state(self, snapshot: AssistantStateSnapshot) -> None:
        self.state_changed.emit(snapshot)
        self._panel.refresh_state(snapshot)
        self._refresh_plugin_status()
        self._refresh_composer_footer()
        self._refresh_history_menu()
    ```
    ```python
    try:
        self._build_chat_messages(
            draft_text=draft_text,
            record_checkpoint=not bool(draft_text.strip()),
        )
    ```
    ```python
    history = [
        {"role": m.role, "content": message_content(m.text, m.attachments)}
        for m in self._service.recent_chat_messages(limit=100_000)
    ]
    ```
  - 影响: 对话每追加/恢复一条消息（以及任何状态回调）都会在主线程重建全部历史 dict、重新拼接多模态 content、对全文跑 token 估算（`_estimate_chat_tokens`），长会话+多图时为 O(N·附件) 的重复 CPU 工作，与 4K 实时性能基准下的 UI 响应目标相悖；`_refresh_history_menu` 同样每次全量拉会话列表。
  - 建议: 以（消息数, 最后 message_id, profile limit）做脏检查缓存估算结果；footer 刷新仅在草稿变化或消息数变化时重算。
  - 置信度: 高

- [AUD-2015] [Suggestion] `persist_v2_state` 非原子写盘，崩溃时可损坏 v2_state.json
  - 位置: src/app/controllers/ai_edit_controller.py:2678-2687
  - 证据:
    ```python
    path = self._v2_state_file_path()
    if path is not None:
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(state, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    ```
  - 影响: 直接 `write_text` 覆盖唯一状态文件；应用在写入中途崩溃/断电会留下截断 JSON，`restore_v2_state` 的容错只是返回 0（可恢复 run 全部丢失）。该方法恰在“工程切换/应用关闭”时机调用，与崩溃窗口重叠概率不低。
  - 建议: 写临时文件 + `os.replace` 原子替换。
  - 置信度: 高

- [AUD-2016] [Suggestion] 类型注解缺失/不精确（少量）
  - 位置: src/app/controllers/ai_edit_controller.py:1758；6259-6313；133-137
  - 证据:
    ```python
    def _on_skill_card_start(self, skill_id: str, form_values: dict) -> None:
    ```
    ```python
    class _InspectorSources:
        def get_brief_dict(self):
            ...
        def get_snapshot_dict(self, *, detail_level, asset_limit):
    ```
    ```python
    def __init__(
        self,
        service: AssistantService,
        index: Any,          # _PlanWorker：语义索引未标注具体类型
    ```
  - 影响: `form_values: dict` 未参数化（应为 `dict[str, Any]`）；`_InspectorSources` 全部方法无返回注解、`detail_level`/`asset_limit` 无类型；`_PlanWorker.index` 为 Any。与文件其余部分“所有函数有类型注解”的规范不一致。
  - 建议: 补齐上述注解；`_InspectorSources` 可实现协议类型以便静态检查。
  - 置信度: 高

### 通过项

- 线程安全（信号模式）：runtime/V2 orchestrator/auto-caption/materialization 的跨线程回调一律只 emit Qt 信号（`_on_run_event`、`_on_v2_orchestrator_event`、auto-caption 三个 lambda 回调），由 queued connection 切回主线程，符合“订阅者只 emit”模式。
- 信号防递归：`_on_service_state` 仅转发，回填由 panel `refresh_state` 的 `_updating` 锁保护（已核验 ai_edit_assistant_panel.py:1783-1790）；`_on_composer_permission_changed`/`_on_composer_mode_changed`/`_on_composer_reasoning_changed` 均先验证取值再写状态，无重入回路。
- GPU 资源生命周期：本文件不创建/持有 ModernGL 对象；`_on_frame_grab_requested` 的 `grabFramebuffer()` 在 GUI 线程执行并有注释约束，无 UI 线程外 OpenGL 调用。
- GLSL·HDR 正确性：不适用（无着色器代码、无 HDR 像素数据处理路径）。
- 状态提交一致性（undo/redo/revision/持久化）：`_on_apply_requested`、`_on_place_generated_media`、`_on_subtitle_plan_ready` 三条提交路径均为“PlanRepository 取回不可变 plan → 以当前工程状态重新 preview → bridge 签发 ApprovalGrant → consume 原子校验 → 现场编译 MacroCommand → commit_ai_command 推入真实 CommandStack（单次 Ctrl+Z 撤销）→ refresh_after_ai_commit”；`restore_v2_state` 清空 `active_run_by_session`、不恢复旧授权（红线 5 遵守）。
- 异常与取消路径（总体）：各 worker 的 `run()` 均捕获异常并 emit failed + logger；`_PlanWorker` 失败还显式 `record_error`；协作取消语义一致（cancel_requested 复检、迟到结果按 run 身份/活动 run 丢弃，`_on_chat_delta`、`_on_v2_event_main` 均有过滤）；`_autonomous_mode_enabled` 异常时 fail-closed。
- 输入验证：`restore_autonomous_goals` 校验 schema_version + 上限 64 行 + 逐行捕获类型错误；`restore_v2_state` 对损坏 JSON 容错返回 0；`_validate_chat_attachments` 按 Profile 能力阻断不支持的附件并给出可操作提示；消息/标题持久化经 `truncate_for_storage`/`sanitize_for_storage` 脱敏（已核验 session_models.py:210 标题在 to_dict 时脱敏）；`_needs_analysis_before_skill` 无法确认 freshness 时保守要求分析（fail-closed）。
- 性能模式（部分）：有界缓存使用正确——`_v2_drawer_history` 上限 20、`AuditStore(max_entries=256)`、`_archive_v2_drawer_items` 有界归档、RunTraceStore 为有界投影；`_compress_chat_messages` 逐级降级不静默突破 provider 上限且保留至少最后一条。
- 类型注解（总体）：绝大多数公有/私有方法均有完整参数与返回注解，`from __future__ import annotations` 在位；仅存 AUD-2016 所列少量缺口。
