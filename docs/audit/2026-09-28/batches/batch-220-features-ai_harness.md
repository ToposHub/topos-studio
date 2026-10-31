# batch-220 — src/features/ai_harness（P2）

区间：AUD-22000–AUD-22099。文件 6 个，共 2,813 行。
汇总：Critical 1 / Major 7 / Suggestion 15。
本批次均为纯 Python harness/协议层，不涉及 GLSL·HDR 与 GPU 资源；审查重点落在
状态机一致性、取消/异常路径、输入验证与出站安全边界。交叉验证引用了
`invocation_models.py`（RunStatus 状态机、sanitize_for_storage）、`runtime.py`
（HarnessRuntime.finish 语义）、`skill_compiler.py`（CompiledSkill 字段）、
`edit/ai_editing/llm_client.py`（LLMClient 协议）与 `mcp_protocol.py`
（SlidingWindowRateLimiter 无锁）。

## src/features/ai_harness/invocation_router.py (行数: 499)

### 发现

- [AUD-22000] [Suggestion] skill_resolver 抛异常时静默放行（fail-open），未注册 Skill 被当作有效
  - 位置: src/features/ai_harness/invocation_router.py:329-339
  - 证据:
    ```python
    skill_id = rule.skill_id
    if skill_id and self._skill_resolver is not None:
        try:
            if not self._skill_resolver(skill_id):
                return RouteResult(
                    intent=rule.intent, matched=False,
                    ... clarify_question=f"该功能需要的 Skill「{skill_id}」未安装或未启用。")
        except Exception:  # noqa: BLE001
            pass
    ```
  - 影响: resolver 本应用于校验 skill_id 已注册启用（外部数据须验证）；其抛错时
    `pass` 使路由结果 `matched=True` 且携带未校验的 skill_id。下游虽有
    SkillCompiler 兜底，但"未安装"提示失效、用户会看到意图命中而非澄清卡。
  - 建议: 异常时按"未安装"处理（返回 clarify），或至少记录 warning 并标
    低置信度降级。
  - 置信度: 高

- [AUD-22001] [Suggestion] 澄清槽位检查只实现 target_language/target_duration，platform/voice_style 模板不可达；language 二次提取是死代码
  - 位置: src/features/ai_harness/invocation_router.py:347-371（另见 168-173 的 _CLARIFY_QUESTIONS）
  - 证据:
    ```python
    for slot in rule.optional_slots:
        if slot == "target_language":
            if not merged.get("target_language"):
                lang = _extract_language(cleaned)
                ...
        elif slot == "target_duration" and not merged.get("target_duration"):
            return RouteResult(...)
    ```
    而 `_CLARIFY_QUESTIONS` 定义了 `target_language/target_duration/voice_style/platform`
    四个模板；内置 INTENT_RULES 的 optional_slots 只出现 `("target_language",)`。
  - 影响: `voice_style`/`platform` 的最小问题永远不会被问出（槽位循环无对应分支）；
    `target_language` 分支里 `_extract_slots` 已提取过 language（445-447 行），此处
    二次提取恒为空，属于死代码。
  - 建议: 补齐 platform/voice_style 分支或删除不可达模板；删除冗余的二次提取。
  - 置信度: 高

- [AUD-22002] [Suggestion] 时长正则不识别一位数秒、五位以上秒被静默截断错解
  - 位置: src/features/ai_harness/invocation_router.py:223（_DURATION_RE）与 237-244
  - 证据:
    ```python
    _DURATION_RE = re.compile(r"(\d{2,4})\s*(?:秒|s\b)")
    ...
    m = _DURATION_RE.search(text)
    if m:
        return max(1, int(m.group(1)))
    ```
  - 影响: "5 秒"/"9s" 等一位数时长提取不到（返回 0，槽位缺失）；"12000 秒"
    会在偏移 1 处匹配成 2000，得到错误时长且无任何提示。
  - 建议: 改为 `(\d{1,6})` 并对超范围值钳制或忽略。
  - 置信度: 高

- [AUD-22003] [Suggestion] prefilled_inputs 的值未做类型/形状验证即并入 invocation inputs
  - 位置: src/features/ai_harness/invocation_router.py:304、344
  - 证据:
    ```python
    pre = dict(prefilled_inputs or {})
    ...
    merged = {**extracted, **{k: v for k, v in pre.items() if v}}
    ```
  - 影响: UI 传入的槽位值（如 `target_duration="很长"`、`base_revision="abc"`）
    只过滤了 falsy 就覆盖确定性提取值，未经校验进入 Invocation.inputs，下游
    `int(...)` 映射抛异常（见 AUD-22005 的连锁后果）。
  - 建议: 对已知槽位按类型（int/枚举/float 范围）校验后再合并，非法值丢弃并按缺失处理。
  - 置信度: 高

- [AUD-22004] [Suggestion] _extract_placement 函数内重复 import re；英文 "start" 子串匹配会误命中
  - 位置: src/features/ai_harness/invocation_router.py:479-489
  - 证据:
    ```python
    if "开头" in text or "起始" in text or "start" in lowered:
        return {"placement": "start"}
    ...
    import re

    match = re.search(r"第\s*(\d+(?:\.\d+)?)\s*(秒|s)", text, flags=re.IGNORECASE)
    ```
  - 影响: 模块顶部已 `import re`（25 行），函数内重复 import 是每次调用的多余开销
    且令人误解；`"start" in lowered` 会把 "restart"/"startup" 误判为放置到开头。
  - 建议: 删除局部 import；英文匹配改用词边界正则（如 `r"\bstart\b"`）。
  - 置信度: 高

### 通过项
- 线程安全：无共享可变状态，router 为纯函数式对象（规则表为构造期不可变 tuple）。
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及（纯 Python、无 Qt、无渲染）。
- 不把用户原文拼进 invocation：route() 先 sanitize_for_storage，to_invocation 显式 user_text=""。
- help_search 注入失败不影响路由（317-318 行有兜底且注释说明意图）。
- 意图表声明式、无 secret / 绝对路径；help 候选截断 [:10] 防爆。

## src/features/ai_harness/legacy_adapters.py (行数: 734)

### 发现

- [AUD-22005] [Major] DRAFT 态调用 finish(success=False) 是非法迁移，SKILL_INPUT_FAILED 路径的 run 永久停在 draft
  - 位置: src/features/ai_harness/legacy_adapters.py:368-379（同型问题 249-261）
  - 证据:
    ```python
    def _run_skill_path(self, run_id: str, inv: Invocation) -> None:
        try:
            sk_input = invocation_to_skill_input(inv)
        except Exception as exc:  # noqa: BLE001
            self._runtime.record_diagnostic(
                run_id, {"code": "SKILL_INPUT_FAILED", "message": str(exc)}
            )
            self._runtime.finish(
                run_id, success=False,
                error_code="SKILL_INPUT_FAILED", error_message=str(exc),
            )
    ```
    此时 run 刚由 `runtime.start()` 创建，状态为 DRAFT（mark_running 尚未调用）。
    而 RunStatus 状态机（invocation_models.py:135-138）规定
    `DRAFT: frozenset({VALIDATING, QUEUED, CANCELLED, SKIPPED})`，不含 FAILED；
    runtime.finish（runtime.py:554-557）遇非法迁移直接 `return False` 不改状态。
  - 影响: inputs 含非法值（如 base_revision 非数字，见 AUD-22003 的入口）时，
    run 永远停留在 draft（非终态），等待终态的 UI/审计收不到结束事件；且
    runtime.finish 在校验前已写 `finished_at`，记录出现"draft 却有 finished_at"
    的自相矛盾。WORKFLOW_MAPPING_FAILED 分支同型（多数路径不可达，因
    _should_route_workflow 已预探测）。
  - 建议: 在映射失败分支前先 `set_status(run_id, RunStatus.VALIDATING)`（如
    invoke_compiled 209-219 行的正确做法）再 finish；或对 start 后、mark_running
    前的失败统一走 validating 中转。
  - 置信度: 高

- [AUD-22006] [Major] _should_route_workflow 未实现文档承诺的 "source 不是 developer" 条件
  - 位置: src/features/ai_harness/legacy_adapters.py:239-247（文档在 101-105）
  - 证据:
    ```python
    # ``invoke`` 根据 ``invocation.skill_id`` 路由到 workflow 或 skill：
    # - skill_id 能映射到 ``WorkflowId`` 且 source 不是 developer → 走
    #   ``run_workflow``（向后兼容 M7）。
    # - 否则走 ``run_skill``（含官方与多角色 Skill）。
    def _should_route_workflow(self, inv: Invocation) -> bool:
        """skill_id 能映射到 WorkflowId 时优先走 workflow（与 M7 一致）。"""
        if not inv.skill_id:
            return False
        try:
            invocation_to_workflow_input(inv)
            return True
        except ValueError:
            return False
    ```
  - 影响: source=developer 且 skill_id 恰好映射到 WorkflowId 的调用会被送进旧
    run_workflow 路径而非多角色 skill 路径，违背类文档声明的路由契约（developer
    入口应走 run_skill）；行为与文档不一致，容易在 CLI/MCP 重跑场景产生意外结果。
  - 建议: 在 `_should_route_workflow` 中加入 `inv.source == InvocationSource.DEVELOPER`
    的排除判断，或修正文档说明该条件已废弃。
  - 置信度: 中

- [AUD-22007] [Major] 结果归档路径（repo.get_plan / repo.store）不在异常保护内，抛错会让 run 卡死在 RUNNING
  - 位置: src/features/ai_harness/legacy_adapters.py:330-339、636-639
  - 证据:
    ```python
    variants = getattr(result, "variants", ()) or ()
    if variants:
        repo = getattr(self._service, "plan_repository", None)
        for variant in variants:
            plan_id = str(getattr(variant, "edit_plan_ref", "") or "")
            if not plan_id or repo is None:
                continue
            plan = repo.get_plan(plan_id)      # 无 try 保护
    ```
    以及 editorial 路径 `repo.store(plan)`（639 行）。这些调用发生在
    `_run_workflow_path_with_input` / `_run_editorial_path` 的 try/except 之后。
  - 影响: plan_repository 是宿主注入的鸭子类型对象，get_plan/store 抛错（损坏的
    plan 存储、序列化失败等）会从 `invoke()` 直接冒泡，RunRecord 已 mark_running
    却永远不会 finish——与类文档"adapter 必须把异常归入 RunRecord"的承诺相悖，
    UI 侧 run 永久转圈。
  - 建议: 把 `_record_*_result` 整体纳入 try/except，兜底 `finish(success=False,
    error_code="RECORD_FAILED")` 并记 diagnostic。
  - 置信度: 中

- [AUD-22008] [Major] _editorial_analyses 实例属性跨 run 不清理，转录追踪会污染后续 run；亦非线程安全
  - 位置: src/features/ai_harness/legacy_adapters.py:567-579、581-583
  - 证据:
    ```python
    def _fetch_evidence(self, asset_ids, kind):
        ...
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
            self._editorial_analyses = result[1]  # 供 trace 记录用
            return tuple(result[0])
    ...
    def _record_transcript_traces(self, run_id, asset_ids):
        analyses = getattr(self, "_editorial_analyses", None) or {}
    ```
  - 影响: adapter 是长生命周期对象；若本次 provider 只返回 segments（无 analyses
    二元组），属性保留上一次 run 的值，上一 run 的转录路由追踪会被写进本 run 的
    TRANSCRIPT_ROUTE_TRACE 诊断（审计数据污染）。editorial 路径文档明确运行在
    worker 线程，若宿主并发跑两个 editorial run，该共享可变属性还存在数据竞争。
  - 建议: 把 analyses 作为局部结果随 outcome 传递（或 run 级显式 reset），不要放
    实例属性；并发场景下补锁或文档化单线程约束。
  - 置信度: 中

- [AUD-22009] [Suggestion] 未使用的导入 InvocationSource / PermissionLevel / RunStatus
  - 位置: src/features/ai_harness/legacy_adapters.py:26-32
  - 证据:
    ```python
    from src.features.ai_harness.invocation_models import (
        ArtifactRef,
        Invocation,
        InvocationSource,
        PermissionLevel,
        RunStatus,
    )
    ```
  - 影响: 三个符号在文件内无引用（状态均用字符串字面量传递），徒增耦合与阅读噪音。
  - 建议: 删除未用导入；若修复 AUD-22005/22006 则改为真正引用 RunStatus /
    InvocationSource 常量。
  - 置信度: 高

### 通过项
- 取消路径：skill 路径把 runtime.is_cancelled 透传为 cancel_requested 回调
  （387-392）；workflow 已 cancel 时 DRAFT→CANCELLED 是合法迁移（runtime.finish
  548-549 行确认 cancel 分支）；cancelled_tasks + is_cancelled → CANCELLED 正确。
- invoke_compiled 不可执行路径状态合法：先 set_status("validating") 再 FAILED。
- provider 异常（workflow/skill/editorial）均被捕获并归入 RunRecord 诊断 + 终态。
- editorial 降级诚实：TTS/合成渲染 provider 失败返回 None 而非伪造 artifact；
  显式输入表单保留但标注 trust="unverified"。
- _observe_plan 不直接执行 plan；observer 异常被隔离（665-667）。
- GPU/GLSL/信号维度不涉及。

## src/features/ai_harness/llm_role_executor.py (行数: 504)

### 发现

- [AUD-22010] [Suggestion] 错误/超时响应在计量前返回，token/成本台账系统性低估
  - 位置: src/features/ai_harness/llm_role_executor.py:390-402
  - 证据:
    ```python
    # 3. 模型返回错误 / 超时
    if resp.error or resp.finish_reason == "timeout":
        self._record_degradation(role, resp.finish_reason or resp.error or "model_error")
        return self._fallback(role, context, upstream)

    # 4. 成本 / token 累计
    self.last_usage = {
        "tokens": resp.tokens_used, ...
    self.total_cost_units += resp.cost_units
    self.total_tokens += resp.tokens_used
    ```
  - 影响: 超时/被截断的调用往往已消耗 token（ModelResponse 可携带 tokens_used），
    但提前 return 使这些消耗永不入账，total_cost_units 偏低，预算判断（403 行）
    依据失真。
  - 建议: 在错误/超时分支也先累计 resp.tokens_used/cost_units 再降级。
  - 置信度: 高

- [AUD-22011] [Suggestion] typed artifact ID 用秒级时间戳生成，同秒内可碰撞
  - 位置: src/features/ai_harness/llm_role_executor.py:189（同型 227、254、281）
  - 证据:
    ```python
    return ScriptDraft(
        draft_id=f"llm_cd_{int(time.time())}",
        ...
    ```
  - 影响: 同一角色在同一秒内产出/重试两个 artifact（或多 run 共存）会得到相同
    draft_id/set_id/outline_id/report_id，破坏"artifact 可追溯"假设，下游按 id
    去重或索引时互相覆盖。
  - 建议: 加 uuid4 片段（如 `llm_cd_{uuid.uuid4().hex[:8]}`）或 run_id + 序号。
  - 置信度: 高

- [AUD-22012] [Suggestion] execute_with_budget 临时改写共享 role_budgets 字典，不可重入
  - 位置: src/features/ai_harness/llm_role_executor.py:442-452
  - 证据:
    ```python
        if current is None:
            self.role_budgets[role] = merged
            try:
                return self.execute(role, context, upstream)
            finally:
                self.role_budgets.pop(role, None)
        self.role_budgets[role] = merged
        try:
            return self.execute(role, context, upstream)
        finally:
            self.role_budgets[role] = current
    ```
  - 影响: executor 是共享实例；若 TaskGraph 将来并行/嵌套执行同角色节点，两个
    execute_with_budget 会互相覆盖/弹出对方的预算条目，导致预算串号。当前
    task_graph 为顺序执行，故仅为风险。
  - 建议: 把合并后的预算作为参数传给内部执行函数，而不是临时改实例状态。
  - 置信度: 高（行为确定；当前无并发调用方，影响为潜在）

- [AUD-22013] [Suggestion] 模型输出的数值字段未做范围校验（confidence 可为任意 float）
  - 位置: src/features/ai_harness/llm_role_executor.py:248
  - 证据:
    ```python
    beats = tuple(
        StoryBeat(
            ...
            confidence=float(b.get("confidence", 0.5)),
    ```
  - 影响: 模型（外部不可信输出）返回 confidence=42.0 或 -1.0 会原样进入
    StoryOutline；下游若按 0–1 语义展示/加权（beats 置信度排序、UI 置信色）会
    得到越界值。schema 校验只保证可转 float，未钳制范围。
  - 建议: `confidence=min(1.0, max(0.0, float(...)))`，越界值可选记降级。
  - 置信度: 中

### 通过项
- 模型输出校验链完整：_parse_json（fence/噪声容错）+ 角色专属 _validate，失败统一
  降级到 DeterministicRoleExecutor 且记录 degradation_counts / last_degradation_reason，
  无静默吞失败。
- 不持有 ApprovalGrant、不写时间线（与 SingleWriterPolicy 契约一致）。
- prompt 构造只传最小 ContextSlice（brief 摘要 / 前 20 条引用），不含 secret/绝对路径。
- 模型调用异常被捕获并降级（385-388），不会冒泡打断 TaskGraph。
- GPU/GLSL/信号维度不涉及。

## src/features/ai_harness/llm_role_model_client.py (行数: 78)

### 发现

- [AUD-22014] [Major] timeout_ms 参数被静默丢弃，角色超时预算完全不生效
  - 位置: src/features/ai_harness/llm_role_model_client.py:40-51
  - 证据:
    ```python
    if timeout_ms is not None and timeout_ms <= 0:
        return ModelResponse(error="invalid_role_timeout")
    try:
        text = self.client.complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            json_mode=True,
            temperature=0.3,
            max_tokens=max_tokens,
        )
    ```
    而 `LLMClient.complete` 协议（edit/ai_editing/llm_client.py:104-111）本身不
    接受任何超时参数。
  - 影响: ModelClient 协议约定"generate 应尊重 timeout_ms"（llm_role_executor.py:65），
    executor 也完全依赖客户端执行超时（自身无墙钟检查）。该适配器只在 `<=0` 时
    报错、合法值直接丢弃，子任务超时只剩 profile 级默认 HTTP 超时兜底——慢端点
    可让角色执行远超 RoleBudget.timeout_ms，违背 plan §10.5 P3.1"子任务有超时"。
  - 建议: 扩展 LLMClient 协议支持 per-request timeout，或在适配器内用
    threading/async 超时包装；至少在无法支持时返回显式降级而非假装已执行。
  - 置信度: 高

- [AUD-22015] [Suggestion] finish_reason 硬编码 "stop"，截断对上层不可见
  - 位置: src/features/ai_harness/llm_role_model_client.py:58-61
  - 证据:
    ```python
    return ModelResponse(
        text=str(text or ""), finish_reason="stop",
        tokens_used=tokens, cost_units=cost,
    )
    ```
  - 影响: LLMClient.complete 内部处理 finish=length（空内容重试等，llm_client.py
    798-833）但适配层一律上报 "stop"；若输出被 max_tokens 截断，executor 的
    "length" 语义失效，只能靠 JSON 解析失败间接降级，诊断信息变弱。
  - 建议: complete 若能暴露 finish_reason 则透传；否则至少在 text 为空时上报
    非 stop 的 finish_reason。
  - 置信度: 中

- [AUD-22016] [Suggestion] 读取共享 last_usage 属性取成本，客户端并发复用时归属错乱
  - 位置: src/features/ai_harness/llm_role_model_client.py:55-57
  - 证据:
    ```python
    usage: Any = getattr(self.client, "last_usage", None)
    tokens = _as_int(getattr(usage, "total_tokens", 0))
    cost = _as_float(getattr(usage, "cost_units", 0.0))
    ```
  - 影响: last_usage 是 client 实例级可变状态；若同一 LlmRoleModelClient/LLMClient
    被两个执行流并发使用，读到的可能是对方调用的 usage，成本台账记错角色。当前
    task_graph 顺序执行，影响为潜在。
  - 建议: 让 complete 返回 usage（协议演进），或在适配器文档化"独占使用"约束。
  - 置信度: 中

### 通过项
- 异常处理：complete 抛错统一转 ModelResponse(error=类型名)，不泄 message 细节，
  executor 侧可见降级（52 行 noqa 注释说明意图）。
- timeout 非法值（<=0）显式拒绝而非忽略。
- _as_int/_as_float 对畸形 usage 防御充分（负值钳 0）。
- 无 secret 存储/记录；GPU/GLSL/信号维度不涉及。

## src/features/ai_harness/mcp_http_transport.py (行数: 395)

### 发现

- [AUD-22017] [Major] urllib 默认跟随重定向且保留 Authorization 头，Bearer 凭据可被引到任意主机（含 https→http 降级，绕过 localhost-only 约束）
  - 位置: src/features/ai_harness/mcp_http_transport.py:242-249（约束定义在 106-109）
  - 证据:
    ```python
    req = urllib.request.Request(self._url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — configured endpoint
    ```
    构造器明确规定：
    ```python
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("plain HTTP MCP endpoints are allowed only on localhost")
    ```
  - 影响: CPython HTTPRedirectHandler 构造重定向 Request 时仅剔除 content-type/
    content-length，Authorization（Bearer api_key）会原样转发到 Location 目标。
    被攻破/恶意的 MCP 端点返回 30x 即可把 API key 送往任意外部主机；https→http
    降级重定向还会绕过"明文 HTTP 仅限 localhost"的构造期校验。
  - 建议: 用自定义 opener 禁用重定向（或 redirect 时校验目标 scheme/host 与
    原始 endpoint 一致并剥离 Authorization 后再放行）。
  - 置信度: 高

- [AUD-22018] [Suggestion] HTTP 响应体读取无大小上限，恶意服务器可造成内存耗尽
  - 位置: src/features/ai_harness/mcp_http_transport.py:248、251
  - 证据:
    ```python
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return (
            resp.status,
            resp.headers.get("Mcp-Session-Id"),
            resp.read().decode("utf-8", errors="replace"),
        )
    ...
    body = exc.read().decode("utf-8", errors="replace")
    ```
  - 影响: 外部 server 输出不可信（模块自身红线），无上限 read() + json.loads 可被
    超大响应撑爆内存（4K 实时基准下进程 OOM 直接崩溃）。
  - 建议: 限制读取字节数（如 resp.read(10 * 1024 * 1024)）并超限报 McpHttpTransportError。
  - 置信度: 高

- [AUD-22019] [Suggestion] 服务器返回 "error": null 时 404 探测分支抛 AttributeError
  - 位置: src/features/ai_harness/mcp_http_transport.py:164
  - 证据:
    ```python
    if status == 404 and isinstance(response, dict) and response.get("error", {}).get("code") == -32601:
        self._mode = "modern"
        return
    ```
  - 影响: JSON-RPC 响应含显式 `"error": null` 时 `response.get("error", {})` 返回
    None（默认值仅在键缺失时生效），`.get("code")` 抛 AttributeError，从 start()
    直接冒泡为非 McpHttpTransportError 的意外崩溃。对比 mcp_outbound.py:433 的
    `isinstance(response.get("error"), dict)` 是正确写法。
  - 建议: 改为 `isinstance(response.get("error"), dict) and response["error"].get("code") == -32601`。
  - 置信度: 中（缺陷确定；触发依赖服务器发送非标准 error:null）

### 通过项
- 线程安全：RLock 串行化请求/会话状态，start() 幂等；_next_request_id 在锁内自增。
- 明文 HTTP 仅限 localhost 的构造期校验完整（含 ::1）。
- 用户 extra_headers 不能覆盖 Authorization/Host/Content-Length/MCP 路由头（222-227）。
- tool definition 经 validate_tool_definition + x-mcp-header 注解校验（名称唯一、
  类型白名单、数组下禁止嵌套），不合规定义被拒并告警。
- SSE/JSON 双格式解析兼容旧端点；按 id 匹配响应；HTTPError 401/403 映射为专用
  授权异常类型。

## src/features/ai_harness/mcp_outbound.py (行数: 603)

### 发现

- [AUD-22020] [Critical] 写审批闸门使用晚发现 manifest 前计算的 is_write，注解标记的写工具可在无审批下执行
  - 位置: src/features/ai_harness/mcp_outbound.py:369-396
  - 证据:
    ```python
    # 5. 写操作审批（plan §10.5：写操作仍弹 Topos 审批）
    is_write = self._is_write_operation(profile, tool_name, tool_spec)
    if is_write and profile.requires_approval_for_writes and not write_approved:
        ...
        return {"ok": False, "decision": "denied_write_needs_approval", ...}

    if manifest is None:
        self.discover(profile_id)
        manifest = self._registry.get_manifest(profile_id)
        tool_spec = self._manifest_tool(manifest, tool_name)
        ...
    ```
    `_is_write_operation`（534-549）依据 tool_spec.annotations（readOnlyHint=False /
    destructiveHint=True）或名称启发式判定；此处 tool_spec 来自旧 manifest（首次
    调用时为 None），补发现后 tool_spec 已更新但 is_write **未重算**，后续
    `self._record(..., write=is_write)` 也沿用旧值。
  - 影响: 会话内首次调用（manifest 未缓存）且工具名不含 write/create/update/...
    启发词（如 "rename_clip"、"deploy_config"）而 annotations 声明为写时：
    is_write=False → 审批检查被跳过 → 远程写工具在 write_approved=False、
    requires_approval_for_writes=True（默认）下被执行，直接违反"写操作必须经
    Topos 审批"红线；且审计事件 write_operation=False，授权卡/审计记录失真。
    若上游 ToolFacade 另有独立审批可部分兜底，本层审计错误仍然成立。
  - 建议: 在补发现 manifest 后重算 `is_write = self._is_write_operation(profile,
    tool_name, tool_spec)` 并重走审批闸门（denied 则返回），再继续 transport 调用。
  - 置信度: 高

- [AUD-22021] [Major] discovery 失败后放行未校验的工具调用（fail-open）
  - 位置: src/features/ai_harness/mcp_outbound.py:378-396（结合 353-360 的前置校验）
  - 证据:
    ```python
    if manifest is None:
        # Discovery happens only after the local authorization gate. ...
        self.discover(profile_id)
        manifest = self._registry.get_manifest(profile_id)
        tool_spec = self._manifest_tool(manifest, tool_name)
        if manifest is not None and tool_spec is None:
            ... return denied_tool_not_discovered
        if tool_spec is not None:
            schema_error = validate_json_schema(arguments, tool_spec.get("inputSchema", {}))
            ...
    # 6. 经 transport 发请求
    transport = self._get_transport(profile)
    ```
    discover() 内部 `except Exception: return None`（529-530），失败后 manifest
    仍为 None → tool_spec=None → 存在性与 schema 校验全部跳过，直接发 tools/call。
  - 影响: 服务器不可发现（或 discover 抛错被吞）时，任意工具名 + 任意参数都会被
    发往外部 server——既不做 inputSchema 校验（外部数据须验证红线），也无
    "工具在清单中"的确认，与 353-360 行"manifest 存在时严格拒绝未发现工具"的
    fail-closed 语义自相矛盾。
  - 建议: discovery 失败（manifest 仍 None）时返回 denied_tool_not_discovered 或
    专用 decision（如 "error_discovery_failed"），仅在 host 显式声明离线容忍时放行。
  - 置信度: 中（行为确定；是否属有意容忍 fake/离线 transport 的设计意图不明）

- [AUD-22022] [Suggestion] registry / auth log / transport 缓存 / 请求 id 均无锁保护，多线程调用会竞态
  - 位置: src/features/ai_harness/mcp_outbound.py:129-137、277-279、551-560、417-421
  - 证据:
    ```python
    def record(self, event: McpAuthorizationEvent) -> None:
        self._events.append(event)
        if len(self._events) > self._max:
            self._events = self._events[-self._max:]
    ...
    self._transports: dict[str, StdioTransport] = {}
    self._rate_limiters: dict[str, SlidingWindowRateLimiter] = {}
    ...
    "id": f"topos-{now}",      # now = time.time() 在入口取一次
    ```
    SlidingWindowRateLimiter（mcp_protocol.py:258-274）同样无内部锁。
  - 影响: call_tool/discover 若被多个 skill 任务并发调用：授权日志丢事件或
    `_events` 列表重建竞态；`_transports` 可能对同一 server 重复建 transport；
    同一微秒内两次调用产生重复 JSON-RPC id（417-421），响应按 id 匹配会串。
    当前未见并发调用方，属潜在风险。
  - 建议: 为 McpOutboundClient/McpServerRegistry/McpAuthorizationLog 加轻量
    Lock；请求 id 改用 itertools.count 或 uuid。
  - 置信度: 高（缺锁确定；并发场景是否存在取决于宿主）

### 通过项
- 授权顺序总体正确：allowlist → 请求形状校验 → BLOCKED → 会话启用 → 工具收窄 →
  写审批 → transport；denied 决策不发远程请求（注释与实现一致）。
- 写判定启发式偏保守（export/set/add 等均视为写），profile.data_scope 非
  read_only 一律写。
- discover 分页循环有 20 页上限且相同 cursor 即断，恶意服务器无法造成无限循环；
  非 dict 响应、缺 tools 键均拒绝。
- 工具定义/结果经 mcp_protocol 校验（validate_tool_definition /
  validate_tool_result / outputSchema 对 structuredContent 校验）。
- McpConnectionProfile 不存 secret（只存 credential_ref）；BLOCKED server 无法
  被 enable_for_session。
- GPU/GLSL/信号维度不涉及。
