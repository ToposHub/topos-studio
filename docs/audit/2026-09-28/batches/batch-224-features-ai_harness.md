# 批次报告：batch-224 — src/features/ai_harness（Skill Studio / 编译 / 子代理运行时）

审查范围：skill_compiler.py / skill_dry_run.py / skill_eval_suite.py / skill_release.py / skill_studio_drafts.py / subagent_runtime.py（6 文件，2,826 行）。
编号区间：AUD-22400–AUD-22499。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解。
本批次均为纯 Python 逻辑层（无 OpenGL、无 Qt 信号、无 GLSL），GPU/信号防递归/GLSL·HDR 三维度不适用。

统计：Critical 0 · Major 7 · Suggestion 10。

---

## src/features/ai_harness/skill_compiler.py (行数: 291)

### 发现

- [AUD-22400] [Suggestion] taskgraph 输入映射用宽泛 `except Exception`，掩盖非领域异常
  - 位置: src/features/ai_harness/skill_compiler.py:184
  - 证据:
    ```python
    elif target == "taskgraph":
        try:
            from src.features.ai_harness.legacy_adapters import (
                invocation_to_skill_input,
            )
            skill_input = invocation_to_skill_input(invocation)
        except Exception as exc:  # noqa: BLE001
    ```
  - 影响: workflow 分支只捕 `ValueError`（第 172 行），taskgraph 分支却捕全部 `Exception`——上游代码 bug（TypeError/KeyError 等）会被转成 SKILL_INPUT_FAILED 诊断静默吞掉，违反 `.clinerules`"不得宽泛 except 掩盖领域错误"。
  - 建议: 与 workflow 分支对齐，只捕获领域异常（ValueError），其余向上抛。
  - 置信度: 高

- [AUD-22401] [Suggestion] 必填输入校验不覆盖空容器（`[]`/`{}`）
  - 位置: src/features/ai_harness/skill_compiler.py:248
  - 证据:
    ```python
    for field_name in required:
        val = effective.get(field_name)
        if val is None or val == "" or val == ():
            diags.append({
                "severity": "error",
                "code": "MISSING_REQUIRED_INPUT",
    ```
  - 影响: `asset_ids=[]`、空 dict 等空容器会通过必填校验进入 provider 路径；空 list 比 `()` 更常见（JSON 反序列化产物）。
  - 建议: 统一用 `not val`（对 str/list/tuple/dict 均适用）或显式枚举空容器形态。
  - 置信度: 高

### 通过项
- 纯编译无副作用：不执行 provider、不写盘、不联网（符合模块红线）
- 输入 schema 校验缺必填即拒绝，不静默进 provider
- 诊断经 `sanitize_for_storage` 脱敏后才入 CompiledSkill
- 类型注解完整（含返回类型）；无循环依赖（legacy_adapters 按需 import）
- EDITORIAL_BUILDER_SKILL_IDS 路由与 editorial 构造器边界一致

---

## src/features/ai_harness/skill_dry_run.py (行数: 818)

### 发现

- [AUD-22402] [Major] 注入的 `fake_executor` 从未被调用，文档宣称的可注入点失效
  - 位置: src/features/ai_harness/skill_dry_run.py:320（赋值）；476 / 526-540（实际分派绕开它）
  - 证据:
    ```python
    self._fake_executor = fake_executor or _FakeDryRunExecutor()
    ...
    edit_plan = self._invoke_fake_executor(draft, fixture, snapshot)
    ...
    def _invoke_fake_executor(
        self, draft: SkillDraft, fixture: DryRunFixture, snapshot: TimelineSnapshot,
    ) -> Optional[EditPlan]:
        ek = draft.executor_kind
        if ek.is_arbitrary_code:
            return None
        if ek == ExecutorKind.EDITORIAL_BUILDER:
            return _fake_editorial_plan(draft, fixture, snapshot)
    ```
  - 影响: `SkillDryRunner(fake_executor=...)` 注入的自定义 fake（`_FakeDryRunExecutor` docstring 明言"测试可传入自定义 fake（如统计调用次数）"）永远不被调用——注入方（测试/宿主）拿到静默无效的钩子，基于它的调用计数/行为断言全部失真。
  - 建议: 要么在 `_invoke_fake_executor` 内先调 `self._fake_executor`（受控枚举约束下包装其返回），要么删掉该参数与 `_FakeDryRunExecutor` 注入点声明。
  - 置信度: 高

- [AUD-22403] [Major] STALE_REVISION 检查只判 `revision >= 0` 哨兵，从不比对草稿 base_revision，真实 stale 场景必然通过
  - 位置: src/features/ai_harness/skill_dry_run.py:379-387
  - 证据:
    ```python
    # plan §6.4：跨 revision 审批不可复用。fixture.snapshot.revision < 0 是
    # eval 用的 stale sentinel；真实场景由 host 注入实际 revision。
    revision_ok = snapshot.revision >= 0
    revision_msg = (
        f"revision current ({snapshot.revision})"
        if revision_ok
        else f"stale revision: snapshot.revision={snapshot.revision} < 0"
    )
    ```
  - 影响: `SkillDraft` 携带 `manifest`/`workflow_template` 各自的 base revision，但检查不与任何一方对比；宿主按注释"注入实际 revision"后（如 revision=42 而草稿基于 7），检查仍 PASS——plan §6.4"跨 revision 审批不可复用"的门禁在 dry-run 里形同虚设，eval 的 revision_stale 用例只覆盖人工哨兵。
  - 建议: 将 draft 侧 base_revision（或宿主传入的 current_revision）纳入 `TimelineSnapshot` 对比，`snapshot.revision != base_revision` 即 STALE。
  - 置信度: 中（哨兵语义为有意设计，但"真实场景"路径确实无法判 stale）

- [AUD-22404] [Suggestion] 取消路径不调 `runtime.finish`：终态 CANCELLED 但 `finished_at` 恒为 None，且"partial artifact"承诺未落地
  - 位置: src/features/ai_harness/skill_dry_run.py:467-474
  - 证据:
    ```python
    if cancel_before_executor:
        runtime.cancel(run_id, reason="cancelled before executor (eval cancel case)")
        runtime.set_status(run_id, RunStatus.CANCELLED)
        record = runtime.get_run(run_id)
        return DryRunOutcome(
            success=False, run_record=record, edit_plan=None,
            checks=tuple(checks), cache_key=_compute_cache_key(draft, fixture),
        )
    ```
  - 影响: RunRecord 状态为终态 CANCELLED 但 `finished_at`/`progress` 永不落值；docstring 宣称"已完成检查保留为 partial artifact（plan §6.2）"，实际 checks 未带任何 partial 标注，仅存在于返回值。
  - 建议: 取消路径补 `runtime.finish(run_id, success=False)`（runtime.finish 对 cancel_requested 的 run 会正确落 CANCELLED），并在 checks/DryRunOutcome 上显式标注 partial。
  - 置信度: 高

- [AUD-22405] [Suggestion] `_check_dag_acyclic` 对重复 node_id 误报环
  - 位置: src/features/ai_harness/skill_dry_run.py:664-688
  - 证据:
    ```python
    nodes = [str(n.get("node_id", "")) for n in (wf.get("nodes", []) or []) if isinstance(n, dict)]
    ...
    adj: Dict[str, List[str]] = {n: [] for n in nodes}
    indeg: Dict[str, int] = {n: 0 for n in nodes}
    ...
    if visited == len(nodes):
        return True, f"DAG acyclic ({len(nodes)} nodes, {len(edges)} edges)"
    return False, f"DAG cycle detected ({visited}/{len(nodes)} nodes reachable)"
    ```
  - 影响: `nodes` 含重复 node_id 时（外部 WorkflowDefinition 输入未验证唯一性），dict 构建去重而 `len(nodes)` 计数不去重，`visited < len(nodes)` 恒真 → 无环图被误报 DAG_CYCLE，发布门禁误拒。
  - 建议: 先对 node_id 去重/校验唯一（重复应报 schema 诊断而非 cycle），或 `len(set(nodes))` 参与比较。
  - 置信度: 高（边界输入场景）

- [AUD-22406] [Suggestion] `DryRunCheckCode.ALL` 与实际检查集不一致（缺 REVISION_CURRENT / EVIDENCE_SUFFICIENT）
  - 位置: src/features/ai_harness/skill_dry_run.py:160-163（对照 385-387、445-447）
  - 证据:
    ```python
    ALL: Tuple[str, ...] = (
        PORTS_COMPATIBLE, DAG_ACYCLIC, MODEL_REQUIREMENTS,
        BUDGET_WITHIN, PERMISSION_OK, CACHE_KEY_STABLE, PLAN_DIFF_NONEMPTY,
    )
    ```
  - 影响: run() 实际产出 9 类检查（多出 "REVISION_CURRENT"、"EVIDENCE_SUFFICIENT"，均为裸字符串字面量），按 `ALL` 遍历断言"七项齐全"的消费方（eval/报表）会漏掉两项，且两处字面量未入常量类易拼写漂移。
  - 建议: 把两个补检项纳入 `DryRunCheckCode` 常量与 `ALL`。
  - 置信度: 高

### 通过项
- EditPlan 恒 mode=PREVIEW，全程无 approval_granted/timeline_committed 写路径（红线达成）
- 无联网/无任意代码执行：executor 分派仅受控枚举 + `is_arbitrary_code` 二次防御
- RunRecord 状态机迁移合法（DRAFT→VALIDATING→QUEUED→RUNNING→终态，已对照 runtime._TRANSITIONS）
- 阻断性检查失败短路在 executor 之前；错误码映射 `_blocking_error_code` 完整
- fixture 无绝对路径/私人素材；payload 摘要截断 32 字符
- cache-key/fixture-id 用 sha256(sort_keys json)，稳定可复现
- 诊断与 RunRecord 事件均经 sanitize_for_storage 脱敏

---

## src/features/ai_harness/skill_eval_suite.py (行数: 416)

### 发现

- [AUD-22407] [Suggestion] UPLOAD_REJECTED 判定引用的事件名不在 RunEventKind 词表，检查恒真
  - 位置: src/features/ai_harness/skill_eval_suite.py:337-341
  - 证据:
    ```python
    if kind == EvalCaseKind.UPLOAD_REJECTED:
        kinds = {e.kind for e in outcome.run_record.events}
        if "cloud_upload" not in kinds and "network_request" not in kinds:
            return True, "no cloud_upload/network events (policy rejected)"
    ```
  - 影响: `RunEventKind`（invocation_models.py:182-205）词表只有 `run.*`/`node.*`/`artifact.*`/`approval.*`/`job.*`/`cost.recorded`/`diagnostic`，不存在 "cloud_upload"/"network_request"——该红线 canary 在当前事件体系下不可能失败，即便未来真有网络行为以既有命名发事件也检不出。
  - 建议: 与 `RunEventKind` 对齐定义网络/云事件常量后再判定；或改为断言 policy（allow_generation/network_allowed=False）与事件白名单。
  - 置信度: 高

- [AUD-22408] [Suggestion] `_KIND_EXPECTED_ERROR` 为死代码
  - 位置: src/features/ai_harness/skill_eval_suite.py:90-96
  - 证据:
    ```python
    #: 每种 kind 期望的 dry-run error_code（""=期望成功）。
    _KIND_EXPECTED_ERROR: Dict[str, str] = {
        EvalCaseKind.SUCCESS: "",
        ...
    }
    ```
  - 影响: 判定逻辑全部内联在 `_judge` 中，该映射无任何引用（全库 grep 仅定义处）；期望码与 `_judge` 内字面量双份维护，未来改期望码只改一处必致漂移。
  - 建议: 删除，或让 `_judge` 改为查表以单一事实来源。
  - 置信度: 高

### 通过项
- 六类最低用例 + revision_stale 装配齐全；EXPECTED_FAILURE 语义（期望失败码才算 PASS）实现正确
- cancel 用例经 `cancel_before_executor` 受控注入，不依赖时序睡眠
- fixture 工厂齐全能力集，避免 evidence/revision 用例误触发 MODEL_MISSING
- EvalCaseResult/SkillEvalReport to_dict 脱敏（继承 RunRecord.to_public_dict）
- 用例共享 runner 实例（SkillEvalSuite.run 传入），无重复装配开销

---

## src/features/ai_harness/skill_release.py (行数: 333)

### 发现

- [AUD-22409] [Major] `register_into` 未强制"draft.* 前缀、绝不覆盖 official skill"红线，SkillRegistry 允许同 id 高版本覆盖官方条目
  - 位置: src/features/ai_harness/skill_release.py:285-297
  - 证据:
    ```python
    def register_into(self, registry: Any, release: SkillRelease) -> List[Any]:
        """把发布产物注册进 :class:`SkillRegistry`。

        红线：
        - 草稿 skill_id 是 ``draft.*`` 前缀，绝不覆盖 official skill；
        ...
        diags = registry.register(release.manifest, enabled=True)
        return list(diags or [])
    ```
  - 影响: 红线仅写在 docstring，代码对 `release.manifest.skill_id` 无任何前缀/来源校验。`SkillRelease` 是公开 frozen dataclass，可绕过 `from_draft` 用任意 manifest（如 skill_id="topos.auto_caption", version="9.9.9"）直接构造；`SkillRegistry.register`（skill_registry.py:152-189）对同 skill_id 仅要求版本更高即覆盖——官方内置 skill 会被静默顶替。反序列化/未来 market 导入路径一旦接入即触达。
  - 建议: `register_into` 内校验 `release.skill_id.startswith("draft.")` 且 `source == LOCAL_DEV`，否则返回拒绝诊断；或由 SkillRegistry 拒绝覆盖 official source 条目。
  - 置信度: 高（不变量未编码是事实；触发需绕过工厂路径，故定 Major 而非 Critical）

### 通过项
- 三道门齐全：版本门（is_dev 拒绝）、executor 门（is_arbitrary_code 拒绝）、eval 门（report.passed 拒绝并附报告）
- 发布幂等成立：`with_signature` 无时间戳，同草稿+同 released_by → 同 content_hash（已核对 compute_content_hash 语义字段集）
- `executor_kind` property 对未知值保守视为任意代码（fail-closed）
- local_dev 发布后 unverified 保持（source 不变，registry 不改写）
- to_dict 无 secret/绝对路径（继承 manifest 脱敏）

---

## src/features/ai_harness/skill_studio_drafts.py (行数: 692)

### 发现

- [AUD-22410] [Major] `new_draft_from_workflow` 的 `produces_plan` 逻辑缺陷：`"timeline_write" in policy` 只查键存在，"forbidden" 也判 True，第二子句恒被短路
  - 位置: src/features/ai_harness/skill_studio_drafts.py:529
  - 证据:
    ```python
    manifest = SkillManifest(
        ...
        produces_plan="timeline_write" in policy or "approval" in str(policy.get("timeline_write", "")),
        ...
    )
    ```
  - 影响: policy={"timeline_write": "forbidden"}（明令禁止写）时 manifest 仍声明 produces_plan=True；而 policy 无该键时第二子句取 `policy.get(...,"")` 恒空串、永不生效——表达式实际语义退化为"声明过 timeline_write 键即产 plan"。声明与 risk=PROPOSAL_ONLY 自相矛盾，误导 registry/UI/编译路由。
  - 建议: 明确语义后改为单条件，如 `produces_plan = str(policy.get("timeline_write", "")).startswith("approval")`。
  - 置信度: 中（意图无法从代码确认，但当前行为与字段名明显不符）

- [AUD-22411] [Major] 浅拷贝破坏草稿冻结不可变约定：嵌套结构与调用方共享，workflow_hash 可与内容静默脱钩
  - 位置: src/features/ai_harness/skill_studio_drafts.py:548-549（对照 508、674-679）
  - 证据:
    ```python
    wf_hash = _hash_workflow(workflow_definition)          # L508 创建时算 hash
    ...
    return SkillDraft(
        ...
        workflow_template=dict(workflow_definition),        # L548 一层浅拷贝
        default_policy=dict(policy),                        # L549 同上
    ...
    def _hash_workflow(wf: Dict[str, Any]) -> str:
        raw = json.dumps(wf, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    ```
  - 影响: `dict(...)` 只拷顶层；nodes 列表、params/policy 嵌套 dict 仍与调用方同一对象。SkillDraft 是 frozen dataclass，但调用方后续原地改 `workflow_definition["nodes"]` 会静默改变已存草稿的 workflow_template 内容，且不再匹配创建时固化的 `origin.workflow_hash`——来源追溯（hash 防篡改）失效；dry-run 的 DAG/端口检查读到的也是被改后的图。
  - 建议: 深拷贝（`copy.deepcopy` 或 json round-trip）后再存入与哈希，保证 draft 与 hash 快照一致。
  - 置信度: 高

- [AUD-22412] [Suggestion] `_input_schema_from_workflow` 的 `required` 永远为空，且给每个输入节点加幻影属性 `_{node_id}`
  - 位置: src/features/ai_harness/skill_studio_drafts.py:637-655
  - 证据:
    ```python
    props: Dict[str, Any] = {}
    required: List[str] = []
    for node in wf.get("nodes", []) or []:
        ...
        for k, v in params.items():
            props[k] = {"type": _json_type_of(v)}
        node_id = str(node.get("node_id", ""))
        if node_id:
            props[f"_{node_id}"] = {"type": "string"}
    if not props:
        return {"type": "object", "properties": {}}
    return {"type": "object", "properties": props, "required": required}
    ```
  - 影响: `required` 声明后从未 append，产出的 schema 必填约束恒空——下游 `SkillCompiler._validate_inputs` 据此不做任何必填校验；`_{node_id}` 幻影属性混入用户可见 schema，语义未注释。
  - 建议: 删掉幻影属性；required 若无来源信息则连字段一并省略，勿输出恒空列表误导消费方。
  - 置信度: 高

- [AUD-22413] [Suggestion] `SkillStudioStore` 无锁、无容量上限，`_registry` 为死字段
  - 位置: src/features/ai_harness/skill_studio_drafts.py:572-601
  - 证据:
    ```python
    def __init__(self) -> None:
        self._drafts: Dict[str, SkillDraft] = {}
        # 红线：不持有 SkillRegistry 引用（草稿隔离）
        self._registry: Any = None  # 显式 None，标识"未发布"

    def save(self, draft: SkillDraft) -> SkillDraft:
        self._drafts[draft.draft_id] = draft
        return draft
    ```
  - 影响: save/delete/list 无任何同步——studio UI 线程与后台 publish/eval 线程并发访问时存在竞态（迭代中 delete 触发 RuntimeError）；`_registry` 永远为 None 仅作注释载体。同仓 `SubagentRegistry`/`HarnessRuntime` 均已加锁，此处不一致。
  - 建议: 加 `threading.RLock` 包住四个操作；删除 `_registry` 死字段（红线写进 docstring 即可）。
  - 置信度: 中（取决于宿主是否跨线程访问 store）

### 通过项
- ExecutorKind 受控枚举红线达成：`is_arbitrary_code` 对全部成员恒 False、未知值构造即拒且判定保守为 True（fail-closed）
- `_clone_manifest_as_local_dev` 强制 `draft.` 前缀 + LOCAL_DEV source + 重签名，不覆盖生产 skill
- `update_field` 白名单禁止改 skill_id/source/signature 等身份字段；bump kind 校验完整
- SkillDraftVersion.from_string 拒绝非三段/非数字/负数
- `with_executor_kind` 校验实例类型与 is_arbitrary_code
- 类型注解完整；不读盘不联网（纯 Python）

---

## src/features/ai_harness/subagent_runtime.py (行数: 276)

### 发现

- [AUD-22414] [Major] Supervisor 记账字典只增不删，子代理完成后无清理，长会话无界内存增长
  - 位置: src/features/ai_harness/subagent_runtime.py:131-134, 176-178, 221-223
  - 证据:
    ```python
    self._children_by_parent: dict[str, int] = {}
    self._rounds: dict[str, int] = {}
    self._requests: dict[str, SubagentRequest] = {}
    ...
    self._children_by_parent[str(parent_run_id)] = children + 1
    self._requests[child_run_id] = request
    self._rounds[child_run_id] = 1
    ```
  - 影响: 每次 `run()` 与 `continue_child()` 都向三个 dict 写入条目，全文件无任何 pop/淘汰（`SubagentRequest` 含完整 prompt 字符串）。Supervisor 为长生命周期对象（宿主 composition 侧持有），多轮会话下 prompt 文本随 `_requests` 永久滞留——既是内存泄漏也违背本模块"trace 不持久化 child prompt"的意图（对象仍驻留内存可被枚举）。
  - 建议: 子代理终态后仅保留有界元数据（或 LRU/TTL 淘汰），`_requests` 只存 continue 所需最小字段并设上限；parent 计数随 parent run 结束清理。
  - 置信度: 高

- [AUD-22415] [Major] `continue_child` 轮次上限为跨锁 TOCTOU：并发 follow-up 可同轮次双双通过并并发执行 runner
  - 位置: src/features/ai_harness/subagent_runtime.py:208-223
  - 证据:
    ```python
    with self._lock:
        round_number = self._rounds.get(previous.child_run_id, previous.round) + 1
        if round_number > self._policy.max_rounds:
            raise SubagentError("subagent maximum rounds exceeded")
    request = SubagentRequest(...)
    ...
    with self._lock:
        self._requests[request.child_run_id] = request
        self._rounds[request.child_run_id] = round_number
    ```
  - 影响: "读 `_rounds`+1 并检查"与"回写 round_number"分属两次加锁，中间窗口另一线程对同一 child 读到相同旧值——两个 follow-up 得到同一 round_number，均通过 `max_rounds` 检查，随后 runner 对同一 child 并发执行两次；轮次权威（max_rounds 契约）被并发绕过，事件流出现重复轮次。
  - 建议: 把"检查 + 回写 + 构造 request"合并进单次 `with self._lock:` 临界区（RLock 可重入，开销可忽略）。
  - 置信度: 高

- [AUD-22416] [Suggestion] `_emit` 直接调用宿主 event_sink，sink 异常会吞掉已完成的子结果
  - 位置: src/features/ai_harness/subagent_runtime.py:253-265
  - 证据:
    ```python
    def _emit(self, child_run_id: str, kind: str, value: Any) -> None:
        if self._event_sink is None:
            return
        payload = (...)
        self._event_sink(child_run_id, kind, payload)
    ```
  - 影响: runner 异常有 containment（except 转 failed 结果），但 `run()`/`continue_child()` 末尾的 `_emit(child_run_id, "subagent.finished", result)` 若 sink 抛异常，异常直接传播给调用方，已算出的 `SubagentResult` 丢失且 `_children_by_parent`/`_rounds` 已记账——调用方重试会再扣一个子名额。
  - 建议: `_emit` 内 try/except 包住 sink 调用并记日志（或至少保证 finished 事件的 sink 异常不吞 result）。
  - 置信度: 高

### 通过项
- 准入校验齐全：深度上限、每 parent 子数上限、prompt 非空、未知 agent 拒绝（fail-closed SubagentError）
- 子写权限只能收窄不能提升：`parent_allows_write and policy.allow_write_tools`
- `to_trace_dict` 只落 prompt/hash/长度，不持久化 prompt 原文；error 截断 300 字符
- lineage 不可变（tuple 拼接）；Supervisor 自身状态有 RLock 保护（除上述 TOCTOU）
- 类型注解完整；runner 返回 str 时自动包装为 SubagentResult

---

## 批次总结

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 7 | AUD-22402, AUD-22403, AUD-22409, AUD-22410, AUD-22411, AUD-22414, AUD-22415 |
| Suggestion | 10 | AUD-22400, AUD-22401, AUD-22404, AUD-22405, AUD-22406, AUD-22407, AUD-22408, AUD-22412, AUD-22413, AUD-22416 |

共性观察：(1) 多条"红线"只存在于 docstring 而未编码为校验（register_into 的 draft.* 前缀、dry-run 的 revision stale）；(2) 跨模块对照验证过 HarnessRuntime 状态机、RunEventKind 词表、SkillRegistry.register 升级语义、with_signature 确定性，相关结论均已按真实源码核对。无 Critical：本批次不触碰渲染管线/GPU/时间线提交，最重问题为防御纵深缺口与资源/并发缺陷，均不直接致崩溃或数据丢失。
