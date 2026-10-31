# 批次报告 batch-128：src/features/edit/ai_editing/

- 任务书：docs/audit/2026-09-28/assignments/batch-128.md
- 编号区间：AUD-12800–AUD-12899
- 文件数：6（2,734 行）
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解
- 说明：本批次为 Agent 执行器、工具循环、Provider 目录与分析数据模型，不涉及 OpenGL/GLSL/Qt 信号，相关维度按"不适用"处理。

---

## src/features/edit/ai_editing/agent_presets.py (行数: 119)

### 发现

（无发现）

### 通过项
- 输入验证：`AgentPreset.__post_init__` 完整校验 id/预算/temperature/max_tokens/tool_allowlist（agent_presets.py:28-38）
- 类型注解：全量注解，frozen dataclass，无缺失
- 线程安全：注册表仅在控制器初始化时构建（ai_edit_controller.py:2300），启动后只读，无并发写风险
- 无 GPU/信号/状态提交/异常路径涉；纯声明式数据，无逻辑分支
- 预设不含凭据（模块 docstring 约束与实现一致）

---

## src/features/edit/ai_editing/agent_task_executor.py (行数: 938)

### 发现

- [AUD-12800] [Major] 取消/审批竞态下 orchestrator 状态转换异常逃逸 execute()，无顶层异常防护
  - 位置: src/features/edit/ai_editing/agent_task_executor.py:617-620（另见 279-440、399-400、427-429）
  - 证据:
    ```python
    granted = self._approval_bridge.request_approval(run_id, plan)  # 可能阻塞等待用户
    if granted is not None:
        # WAITING_APPROVAL → EXECUTING.
        self._orchestrator.grant_approval(run_id)
    ```
    而 orchestrator 侧（agent_orchestrator.py:658-664）：
    ```python
    run = self.get_run(run_id)
    if run is None:
        raise RunNotFoundError(run_id)
    if run.state != expected_from:
        raise RunNotTransitionableError(
            f"expected {expected_from!r} but run is {run.state!r}"
        )
    ```
  - 影响: `request_approval` 桥接调用可长时间阻塞等待用户交互；期间 UI 线程 `request_cancel` 已把 run 置为 CANCELLED（终态，出边为空），`grant_approval` 随即抛 `RunNotTransitionableError`。`execute()` 主循环（279-440）没有顶层 try/except，异常直接逃逸；调用方 `chat_run_driver.py:549` 对 `executor.execute(...)` 同样无防护，worker 线程收到未处理异常，AgentTaskResult 契约（应返回 CANCELLED 结果）被破坏。`begin_verification`/`finish_verification`（399-400、427-429）存在同样的 is_run_active 检查与转换之间的 TOCTOU 窗口。
  - 建议: 在 `execute()` 外层包 try/except，捕获 RunNotTransitionableError/ValueError 并返回 CANCELLED（或 FAIL）的 AgentTaskResult；或在 orchestrator 增加可吞并取消竞态的安全转换辅助（如 `try_advance` 返回 bool）。
  - 置信度: 高

- [AUD-12801] [Suggestion] 规划失败原因失真：allowlist/目录过滤与 JSON 解析失败混为一谈
  - 位置: src/features/edit/ai_editing/agent_task_executor.py:309-314、546-547、579-580
  - 证据:
    ```python
    if plan is None:
        reason = "规划失败：模型未返回有效的 JSON 计划"
    ```
    而 `_parse_plan_json` 内：
    ```python
    if self._tool_allowlist and tool_name not in self._tool_allowlist:
        continue
    ...
    if not steps:
        return None
    ```
  - 影响: 模型可能返回了完全合法的 JSON 计划，但全部步骤被 allowlist / 工具目录过滤掉，用户与调试者看到的失败原因却是"未返回有效 JSON"，误导排障。
  - 建议: `_parse_plan_json` 区分返回"解析失败"与"步骤全被过滤"两种原因（如返回 (plan, reason) 或抛特定异常），`execute` 据此给出准确 reason。
  - 置信度: 高

- [AUD-12802] [Suggestion] `_gather_context` 直接调用 tool_facade.invoke，绕过 ToolInvocationRunner
  - 位置: src/features/edit/ai_editing/agent_task_executor.py:449-455
  - 证据:
    ```python
    try:
        result = self._tool_facade.invoke("topos.get_project_snapshot", {})
        if isinstance(result, dict) and result.get("ok"):
            return result.get("data", result)
    ```
  - 影响: 与本模块 docstring "every tool execution goes through the ToolInvocationRunner" 的架构约束不一致：该调用不产生 run.tool_started/finished 事件、不进 ToolOperationLedger、不做能力目录声明检查，durable trace 与恢复审计缺少这两次快照调用记录（每轮 plan/verify 各一次）。
  - 建议: 快照收集也走 `ToolInvocationRunner.invoke`（read 工具无需 approval），或在文档中明确豁免理由。
  - 置信度: 高

- [AUD-12803] [Suggestion] AlwaysApproveBridge 哨兵字符串可被 facade 当作合法授权（安全脚枪）
  - 位置: src/features/edit/ai_editing/agent_task_executor.py:161-172
  - 证据:
    ```python
    class AlwaysApproveBridge:
        """Test-only bridge: always approves with a sentinel object."""
        _SENTINEL = "host-approved"
        def request_approval(self, run_id: str, plan: TaskPlan) -> Any:
            return self._SENTINEL
    ```
    而 tool_facade.py:672 的签名为 `approval: ApprovalGrant | str | None = None`（字符串是被接受的授权形态）。
  - 影响: 该类导出于 `__all__`；若被误接到生产路径（facade 不校验 grant 类型/不经过 ApprovalService 验证），任何字符串都会放行写操作，等于绕过审批门禁。
  - 建议: 在构造处加断言/环境检查（仅测试可用），或改为只在测试模块中定义；facade 侧对 str 型 grant 强制走 ApprovalService 解析。
  - 置信度: 高

- [AUD-12804] [Suggestion] `_facade_to_step_result` 为死代码
  - 位置: src/features/edit/ai_editing/agent_task_executor.py:678-714
  - 证据: 全仓库 grep 仅命中定义处（`src/features/edit/ai_editing/agent_task_executor.py:679`），无任何调用；实际路径使用 `_tool_result_to_step_result`。
  - 影响: 36 行未使用的转换逻辑（含 PERMISSION_DENIED 诊断解析）留在核心执行器中，增加维护误导。
  - 建议: 删除，或若保留诊断解析需求则并入 `_tool_result_to_step_result`。
  - 置信度: 高

### 通过项
- 线程安全：executor 为同步 Qt-free 设计（docstring 明确），状态均在局部变量，跨线程状态由 orchestrator 的 RLock 保护
- is_write 权限判定：从工具定义/能力目录派生，绝不信任模型 JSON（agent_task_executor.py:88-98、549-577），工具不存在/未声明时 fail-closed
- 空计划 = FAILED 不伪造成功（316-321）；验证为硬门禁（385、414-440）
- 取消路径：各阶段边界均有 is_run_active 检查（281、295、340、352、362、377）——窗口期竞态见 AUD-12800
- 输入验证：`__init__` 校验 max_steps>0（221-222）、allowlist 去空格（223）、温度/预算经 AgentPreset 校验
- 有界性：_MAX_STEPS=10、_MAX_REPLANS=1，replan 计数递增（415-420），无死循环
- 类型注解完整；工具输出经 JSON 序列化回喂（build_*_prompt 内 json.dumps）
- 无 GPU/GLSL/信号涉

---

## src/features/edit/ai_editing/agent_task_prompts.py (行数: 201)

### 发现

（无发现）

### 通过项
- 不可信数据隔离：项目快照、步骤结果均经 `json.dumps(..., default=str)` 序列化后注入 prompt（agent_task_prompts.py:45、117-118、153），符合"工具输出不可改系统指令"约束
- 输入验证：`_format_tool_catalog` 的输入来自宿主 `to_openai_tool()`（tool_facade.py:140-147 恒返回 dict parameters），且 executor `_get_tool_catalog` 对每个转换有 try/except 兜底
- 输出契约明确：规划/验证 prompt 均显式声明严格 JSON 格式与字段（59-78、128-137）
- 类型注解完整；纯函数无状态、线程安全
- 无 GPU/信号/状态提交/异常路径涉

---

## src/features/edit/ai_editing/agent_tool_loop.py (行数: 563)

### 发现

- [AUD-12805] [Suggestion] assistant envelope 构建对非 dict arguments 无防御，异常会逃出 run()
  - 位置: src/features/edit/ai_editing/agent_tool_loop.py:235
  - 证据:
    ```python
    arguments = dict(getattr(tc, "arguments", {}) or {})
    ```
    对比同文件 `_invoke_one`（482-508）将同样的转换包在 try/except 内并转为 `ToolResult.failure(..., error_code="TOOL_EXCEPTION")`。
  - 影响: `run()` 的设计承诺"循环内一切错误都转为 AgentToolLoopResult"；但若某个实现 `_ToolCallingClient` 协议的第三方客户端返回字符串形式 arguments（OpenAI 原始协议即为 JSON 字符串），`dict(str)` 抛 ValueError 且不在任何 try 块内，直接逃出 `run()`。内置客户端（llm_client.py:1092-1100）已归一为 dict，故当前代码不触发，属防御不一致。
  - 建议: 将 envelope 构建循环纳入 try/except 并以 `_ERROR` 结果返回，或复用 executor 的 `_coerce_args` 归一逻辑。
  - 置信度: 中

- [AUD-12806] [Suggestion] 陈旧的 final_text 被写入 assistant tool_calls envelope
  - 位置: src/features/edit/ai_editing/agent_tool_loop.py:201、248-250
  - 证据:
    ```python
    final_text = getattr(response, "text", "") or final_text
    ...
    local_messages.append(
        {"role": "assistant", "content": final_text, "tool_calls": assistant_tool_calls}
    )
    ```
  - 影响: 当本轮 response.text 为空时，`or final_text` 回退到上一轮的文本，envelope 中携带的 content 与本轮 tool_calls 不属于同一次模型输出，可能干扰模型对上下文的理解（协议保真度问题，非崩溃）。
  - 建议: envelope 只使用本轮 `getattr(response, "text", "")`（可为空串或 None），历史 final_text 仅用于最终结果。
  - 置信度: 高

### 通过项
- 线程安全：并行组经有界 ThreadPoolExecutor（max_workers = min(预算, 组大小)），结果按模型顺序回填（ordered[index]），取消时先排空 in-flight 再补 TOOL_NOT_STARTED，无未收割 future
- 有界性：max_iterations/max_tool_calls/max_parallel_tool_calls 均在 `__post_init__` 强制为正（69-75）；预算耗尽以 _MAX_TOOL_CALLS/_MAX_ITERATIONS 可读原因终止（261-267、312-315、317-318）
- 取消路径：模型调用前（162）、每组工具调用前（257）、组内每次调度前（348、365、392）均轮询 cancel_check；LLM_CANCELLED 错误码单独识别（188-196）
- 工具白名单：`_invoke_one` 拒绝未在宿主目录中公布的工具（476-481，TOOL_NOT_EXPOSED）；call_id 缺失/重复显式报错（210-227）
- 并行准入 fail-closed：`_execution_mode` 对未公布工具、legacy runner、解析失败一律 EXCLUSIVE（446-465）
- 工具输出不可信：结果经 `_tool_result_to_text`（pruner.render 的 JSON 序列化）回喂（296-303、530-541）
- OpenAI 协议形状：assistant(tool_calls) envelope 先于 role="tool" 消息（229-250），arguments 以 JSON 字符串编码
- 类型注解完整；无 GPU/信号/状态提交涉

---

## src/features/edit/ai_editing/ai_provider_catalog.py (行数: 332)

### 发现

（无发现）

### 通过项
- 输入验证/fail-closed：`profile_supports_input` 对 video/audio/file 在非 Responses 型 provider 上直接 False（274-275），避免把多模态请求发到无文件上传实现的客户端才收到 400
- 预设不含凭据（全部字段为协议/端点/模型 ID），与模块安全约束一致
- `infer_capabilities` 用 `dict.fromkeys` 去重且顺序稳定（239）；`_is_known_volcengine_multimodal` 白名单同时锁定 provider_kind 与 model 前缀，不会把 DeepSeek/GLM 误判为多模态（249-251）
- 纯静态目录 + 纯函数，无状态、线程安全；类型注解完整
- 无 GPU/GLSL/信号/异常路径涉

---

## src/features/edit/ai_editing/analysis_models.py (行数: 581)

### 发现

- [AUD-12807] [Suggestion] WordSegment/TranscriptSegment 缺少帧序与置信度校验，倒置区间被静默钳制
  - 位置: src/features/edit/ai_editing/analysis_models.py:78-79、118-119（对比 OcrDetection:233-239、OcrBox:199-203）
  - 证据:
    ```python
    def to_frame_range(self) -> FrameRange:
        return FrameRange(self.start_frame, max(1, self.end_frame - self.start_frame))
    ```
    WordSegment（65-96）/TranscriptSegment（99-149）均无 `__post_init__`；而同类外部数据模型 OcrDetection 校验了 `frame >= 0` 与 `confidence ∈ [0.0, 1.0]`。
  - 影响: ASR/对齐 provider 属外部数据源；`from_dict` 不校验负帧、end < start、confidence 越界。倒置区间经 `max(1, ...)` 被静默改成 duration=1 的片段，词级搜索/定位位置错误且无任何告警，违背"外部数据须验证"与"不静默降级"的项目铁律。
  - 建议: 为两个 dataclass 增加 `__post_init__`（start_frame >= 0、end_frame >= start_frame、confidence ∈ [0,1]），或至少在 from_dict/to_frame_range 对倒置区间抛错/打点。
  - 置信度: 高

### 通过项
- 输入验证（其余）：OcrBox 限 [0,1]、OcrDetection 限 frame>=0 与 confidence∈[0,1]、AudioAnalysis.from_dict 过滤畸形 speech_ranges 项（177-181）；AnalysisService 恢复路径对 from_dict 的 KeyError/TypeError/ValueError 有兜底（analysis_service.py:660-661，缓存损坏按 cache-miss 处理）
- 状态提交一致性：指纹失配即失效（is_valid_for → AssetFingerprint.matches，content_hash 优先）；用户标注与 AI 推断经 user_verified 分层，provenance 字段级独立（FieldProvenance.with_field 不可变转换并校验字段名，323-328）
- 不可变建模：全 dataclass frozen；AnalysisProvenance.extra 的可变 dict 在 to_dict/from_dict 均做了拷贝
- 线程安全：纯数据模型 + 无副作用函数；`analysis_to_segments` 每次生成新列表
- 类型注解完整；无 GPU/GLSL/信号/异常路径涉

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 1 | AUD-12800 |
| Suggestion | 7 | AUD-12801–12804、AUD-12805–12807 |

（合计 8 项；agent_presets.py、agent_task_prompts.py、ai_provider_catalog.py 无发现。）
