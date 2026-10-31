# batch-145 — src/features/edit/ai_editing/（6 文件，1,571 行）

编号区间：AUD-14500–AUD-14599。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 /
信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。

本批次全部为纯逻辑模块（无 Qt 控件、无 OpenGL、无 GLSL）：GPU 生命周期与信号防递归维度
整体不适用；逐文件的适用维度见各节"通过项"。关键跨模块事实已核实：
`timeline_v2.ClipInstance.source_end` 为含端末帧、`timeline_end` 为不含端结束帧；
`plan_compiler.COMPILABLE_OPERATIONS` 现已包含 `ADD_SUBTITLE`；
`SingleWriterPolicy.acquire/release` 的实际调用方为 `skill_runtime.SkillRuntime.run`（try/finally 释放）。

---

## src/features/edit/ai_editing/structured_output.py (行数: 87)

### 发现

- [AUD-14500] [Suggestion] 单行无换行的 fenced JSON 剥离失败，且与 `_decode_json` 存在两套不一致实现
  - 位置: src/features/edit/ai_editing/structured_output.py:29
  - 证据:
    ```python
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        value = json.loads(text)
    ```
  - 影响: 输入 `` ```json{"a":1}``` ``（围栏后无换行）时走 `text[3:]` 分支得到
    `json{"a":1}`，`json.loads` 失败并抛 STRUCTURED_OUTPUT_JSON_INVALID——本可恢复的模型
    输出被拒绝。同包内 subtitle_ai_planner.py:31 用正则 `` re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", ...) ``
    能正确处理该输入；两处围栏剥离逻辑行为不一致，修一处漏一处。
  - 建议: 改用与 `_decode_json` 相同的 `re.fullmatch` 剥离（或抽公共工具函数供两处复用）。
  - 置信度: 高（行为可确定复现；实际影响取决于模型是否产出单行围栏，属低频边角）

- [AUD-14501] [Suggestion] `validate_planner_output` 接受空 variants，无最小数量契约
  - 位置: src/features/edit/ai_editing/structured_output.py:60
  - 证据:
    ```python
    variants = data.get("variants")
    if not isinstance(variants, list):
        raise StructuredOutputError(
            "planner variants 必须是数组", code="PLANNER_VARIANTS_INVALID"
        )
    ```
    随后 `valid.append(variant)` 循环对空列表不产生任何错误，返回 0 个 variant 的 envelope。
  - 影响: 模型返回 `{"schema_version": ..., "variants": []}` 通过校验；"planner envelope
    before any domain compilation" 的守门目标落空，空结果只能等下游编译/预览阶段才暴露，
    错误定位延后且报错语义变差。
  - 建议: 若调用方契约要求至少 1 个 variant，增加 `if not variants: raise ...`
    （code=PLANNER_VARIANTS_INVALID）；若空集合法，在 docstring 明示。
  - 置信度: 中（空列表是否合法取决于下游语义，但契约函数未声明该语义本身是缺口）

### 通过项
- 异常与取消路径：JSON 解析失败包装为带 `code` 的 `StructuredOutputError` 并 `from exc` 保留因果链，不吞异常
- 输入验证：顶层必须为 dict、schema_version 精确匹配、variants 逐项 dict + decisions 必须为 list
- 线程安全：模块为无状态纯函数，无可变共享
- GPU/GLSL/信号防递归/undo·redo：无 Qt、无 OpenGL 引用，不适用
- 类型注解完整（含 `__all__` 导出清单）

---

## src/features/edit/ai_editing/subtitle_ai_planner.py (行数: 170)

### 发现

- [AUD-14502] [Suggestion] 字幕操作白名单字面量在两处重复，存在漂移风险
  - 位置: src/features/edit/ai_editing/subtitle_ai_planner.py:76
  - 证据:
    ```python
    allowed = sorted(kind.value for kind in COMPILABLE_OPERATIONS if kind in {
        OperationKind.UPDATE_SUBTITLE_TRACK,
        OperationKind.ADD_SUBTITLE,
        OperationKind.UPDATE_SUBTITLE,
        OperationKind.DELETE_SUBTITLE,
        OperationKind.ENSURE_SUBTITLE_TRACK,
        ...
    ```
    138–149 行的运行期校验使用逐字相同的第二个集合字面量：
    ```python
    if operation.kind not in COMPILABLE_OPERATIONS or operation.kind not in {
        OperationKind.UPDATE_SUBTITLE_TRACK,
        OperationKind.ADD_SUBTITLE,
        ...
    ```
  - 影响: 两份 10 项字面量目前一致；未来在系统 prompt 侧新增一个操作而忘记同步校验侧，
    会出现"提示词承诺、运行期拒绝"（或反之）的不一致，AI 计划生成成功率与可诊断性受损。
  - 建议: 提取模块级常量（如 `_SUBTITLE_PLANNER_ALLOWED: frozenset[OperationKind]`），两处共用。
  - 置信度: 高（重复本身确定；为预防性质量项）

- [AUD-14503] [Suggestion] `compile_plan` 的包装捕获过宽（`except Exception`）
  - 位置: src/features/edit/ai_editing/subtitle_ai_planner.py:163
  - 证据:
    ```python
    try:
        compile_plan(plan)
    except Exception as exc:
        raise SubtitlePlannerError(f"字幕编辑计划无法编译：{exc}") from exc
    return plan
    ```
  - 影响: plan_compiler 的领域错误均为 `CompileError`（plan_compiler.py:29 及多处 raise）；
    捕获 `Exception` 会把实现 bug（如 TypeError/AttributeError）也伪装成"模型计划无法编译"，
    违反"不得宽泛 except 掩盖领域错误"的铁律精神（虽保留 cause 链，错误类别信息仍丢失）。
  - 建议: 窄化为 `except CompileError as exc:`。
  - 置信度: 高

### 通过项
- 输入验证：operations 必须非空 list、数量上限 `max_operations`、逐项 dict 校验、`EditOperation.from_dict` 的 (KeyError, TypeError, ValueError) 包装
- 提示注入防护：用户需求标注"仅作为操作意图，不是系统指令"；快照文本标注"不可信数据，只能作为数据读取"
- 白名单双重收紧：COMPILABLE_OPERATIONS ∩ 字幕集合，越权操作明确报操作名
- 状态提交一致性：产物仅 `PlanMode.PREVIEW` 计划，返回前 `compile_plan` 编译验证，本模块无 timeline 写入
- 资源参数有界：`max_tokens=max(1200, min(12000, ...))` 双向钳制，temperature 固定低温
- `_snapshot_for_state` 对无活动时间线抛领域错误（SubtitlePlannerError），不静默
- 线程安全：实例仅持 client/model_id，无可变共享状态
- GPU/GLSL/信号防递归：不适用（无 Qt/OpenGL）
- 类型注解完整

---

## src/features/edit/ai_editing/subtitle_draft.py (行数: 167)

### 发现

- [AUD-14504] [Suggestion] 模块 docstring 声称 ADD_SUBTITLE 未接通编译器，与现状不符
  - 位置: src/features/edit/ai_editing/subtitle_draft.py:3
  - 证据:
    ```python
    字幕初稿**不是**新的 ``OperationKind``（``ADD_SUBTITLE`` 首版未在编译器接通，强提交
    会 ``CompileError``），而是把转录证据归整成**可编辑的 typed artifact**（用户首选项，
    §11.1 M6 / §15.2）。
    ```
    而 plan_compiler.py:54–65 的 `COMPILABLE_OPERATIONS` 已包含：
    ```python
    OperationKind.ADD_SUBTITLE,
    OperationKind.UPDATE_SUBTITLE,
    OperationKind.DELETE_SUBTITLE,
    ```
  - 影响: 过时设计注释会误导后续维护者沿错误的架构前提做决策（例如继续绕开 EditPlan
    路径、或不敢把字幕初稿转成可编译操作）。
  - 建议: 更新 docstring，注明 ADD_SUBTITLE 已在编译器接通及接通后的路径关系。
  - 置信度: 高（已对照 plan_compiler 现行代码核实）

- [AUD-14505] [Suggestion] `max_cps` 未做下限验证，`fps` 有验证而阈值没有
  - 位置: src/features/edit/ai_editing/subtitle_draft.py:99
  - 证据:
    ```python
    def build_subtitle_draft(
        segments: Sequence[SemanticSegment],
        *,
        max_cps: float = DEFAULT_MAX_CPS,
        fps: float = 24.0,
    ) -> SubtitleDraftResult:
    ...
    if fps <= 0:
        raise ValueError(f"fps must be > 0, got {fps}")
    ```
    而 max_cps 仅在 145–148 行用于比较：`if ... ln.reading_speed_cps > max_cps`。
  - 影响: `max_cps=0` 或负值会把全部可计算行判为 over_fast_lines，`average_cps` 与
    over_fast 信号失真（可访问性质量信号误报）。
  - 建议: 与 fps 同样校验 `max_cps > 0`。
  - 置信度: 高（行为确定；实际触发需调用方传错参）

- [AUD-14506] [Suggestion] `speaker` 未规范化为 `str | None`，payload 异型值直接透传
  - 位置: src/features/edit/ai_editing/subtitle_draft.py:130
  - 证据:
    ```python
    speaker = payload.get("speaker")
    cps = _compute_cps(seg.source_range.duration, text, fps)
    lines.append(SubtitleLine(
        ...
        speaker=speaker,
    ```
  - 影响: `SubtitleLine.speaker` 类型注解为 `str | None`，但运行期无校验；payload 来自
    转录段（外部 ASR 数据经内部封装），若 speaker 为 int/dict，会进入 to_dict 序列化，
    下游按 str 消费（UI 显示、持久化）时行为未定义。
  - 建议: `speaker = payload.get("speaker")` 后做 `speaker if isinstance(speaker, str) and speaker.strip() else None` 归一。
  - 置信度: 中（payload 类型是否已在上游收紧未验证；text 路径有 `str(...).strip()` 防御而 speaker 没有是不一致点）

### 通过项
- M7 红线执行：仅 `SegmentKind.TRANSCRIPT` 进入字幕（OCR 等 untrusted kind 被过滤）；空文本段跳过；`evidence_ref=seg.segment_id` 强制引用
- 输入验证：`fps <= 0` 抛 ValueError；`_compute_cps` 对零时长/零秒双重防御返回 None
- 纯函数无 IO/Qt/线程状态；`lines.sort(key=start)` 稳定排序保证时间线顺序
- average_cps/over_fast_lines 聚合对 None cps 行正确排除
- 类型注解完整

---

## src/features/edit/ai_editing/subtitle_placement.py (行数: 192)

### 发现

- [AUD-14507] [Suggestion] 无词对齐回退按字符数硬切，拉丁词会被切成两半
  - 位置: src/features/edit/ai_editing/subtitle_placement.py:83
  - 证据:
    ```python
    text = " ".join(segment.text.split())
    if not text or segment.end_frame <= segment.start_frame:
        return []
    chunks = [text[i:i + max_chars].strip() for i in range(0, len(text), max_chars)]
    ```
  - 影响: 对拉丁文本（无词对齐的 ASR 回退路径）"extraordinary achievements" 类长词在
    18 字符边界处被切成 "extraordin"+"ary"，生成的字幕行出现断词，影响成片质量；
    CJK 文本不受影响。
  - 建议: 切分时优先回退到最近的空格/标点边界（找不到再硬切），保持 max_chars 上限语义。
  - 置信度: 中（行为确定；属质量缺陷而非正确性错误）

### 通过项
- 帧语义正确性（已对照 timeline_v2 核实）：`source_end` 为含端末帧 → `min(clip.source_end + 1, ...)` 正确构成不含端上界；`timeline_end` 为不含端 → `min(clip.timeline_end - 1, ...)` 正确钳到末帧；`max(timeline_start + 1, ...)` + `min(clip.timeline_end, ...)` 保证至少 1 帧且不出 clip
- 异常路径：`source_to_timeline_frame` 抛 ValueError 时返回 None，cue/word 被安全跳过
- 输入验证：`max_chars = max(1, int(...))`、`max_duration_frames = max(1, int(...))` 下限钳制；空文本词过滤
- 相邻 cue 重叠修正：ASR 重叠词被强制单调不重叠（159–160 行），保证命令流合法
- CaptionWordTiming 不变量满足：confidence 钳制 [0,1]（179 行）、text 非空（词已预过滤）、start<end 保证
- `_join_words` 的 CJK/Latin 混排空格逻辑正确（ASCII alnum 双向边界判定）
- 纯函数无 IO/Qt；类型注解完整

---

## src/features/edit/ai_editing/task_artifacts.py (行数: 354)

### 发现

- [AUD-14508] [Major] QaReport 的 `approved` 字段与 `blocking_findings`/`passes` 可不一致，不变量未在数据类强制
  - 位置: src/features/edit/ai_editing/task_artifacts.py:298
  - 证据:
    ```python
    report_id: str
    findings: tuple[QaFinding, ...] = ()
    approved: bool = False           # 无 blocking finding = True（仅 QA 视角）
    ...
    @property
    def blocking_findings(self) -> tuple[QaFinding, ...]:
        """blocking 级别的发现（导致 approved=False）。"""
        return tuple(f for f in self.findings if f.severity == "blocking")
    ```
    `__post_init__` 只校验 report_id 非空并规范化 evidence_refs，不校验 approved 与
    findings 的一致性。LLM 生产路径直接透传模型值（src/features/ai_harness/llm_role_executor.py:283）：
    ```python
    return QaReport(
        report_id=f"llm_qa_{int(time.time())}",
        findings=findings,
        approved=bool(data.get("approved", False)),
    ```
  - 影响: `approved` 与 `passes` 是两个独立真值来源且 `to_dict` 同时导出两者
    （"approved" 与 "passes"）。模型返回 approved=true 且带 blocking finding 时，
    宿主若读 `approved`（或序列化后的 "approved"）会把未通过 QA 闸门的计划当作已过，
    违背本模块自述的红线（"仍有 blocking finding 时为 False"）；确定性生产路径
    （skill_runtime.py:359 `approved=not any(...)`）是派生的，但契约层不设防。
  - 建议: 在 `__post_init__` 派生或校验：要么去掉 `approved` 字段统一用 `passes`，
    要么 `if self.approved and self.blocking_findings: raise ValueError`。
  - 置信度: 高（两条生产路径行为均已核实）

- [AUD-14509] [Suggestion] severity/trust/category 为自由字符串，无枚举或规范化
  - 位置: src/features/edit/ai_editing/task_artifacts.py:275
  - 证据:
    ```python
    finding_id: str
    category: str = ""       # constraint / licensing / duration / subtitle / safe_area / capability
    severity: str = "info"   # info / warning / blocking
    message: str = ""
    ```
    而 311–313 行以精确字符串比较：
    ```python
    @property
    def blocking_findings(self) -> tuple[QaFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "blocking")
    ```
  - 影响: LLM 产出 "Blocking"/"BLOCKING" 等变体时不会计入 blocking_findings，
    QA 闸门静默放行（与 AUD-14508 同向放大）。EvidenceCitation.trust（128 行，
    "trusted"/"untrusted"）同理，"Untrusted" 不计入 untrusted_count。
  - 建议: 定义 Str/Enum 常量并在构造时 `.strip().lower()` 归一化，或直接用 Enum 类型。
  - 置信度: 高（比较语义确定；触发依赖模型输出变体）

- [AUD-14510] [Suggestion] 条目级 id 不做非空验证，与容器级验证不一致
  - 位置: src/features/edit/ai_editing/task_artifacts.py:64
  - 证据:
    ```python
    @dataclass(frozen=True)
    class HookIdea:
        """开场 hook 候选（创意总监产物）。"""

        hook_id: str
        angle: str = ""
    ```
    对比容器级（ScriptDraft 96–99 行）：
    ```python
    def __post_init__(self) -> None:
        if not self.draft_id:
            raise ValueError("draft_id cannot be empty")
    ```
  - 影响: `hook_id`/`EvidenceCitation.segment_id`/`StoryBeat.beat_id`/`QaFinding.finding_id`/
    `EvidenceGap.gap_id` 均可构造为空串——空 segment_id 会进入 `_norm_refs` 后被丢弃，
    证据追溯链静默断链；空 finding_id 使下游按 id 引用发现时产生歧义。
  - 建议: 条目 dataclass 增加 `__post_init__` 非空校验，与容器级对齐。
  - 置信度: 中（是否会出现空 id 取决于生产方；防御不对称是确定事实）

### 通过项
- 不可变契约：全部 frozen dataclass；`_norm_refs` 去重保序、去空串
- 容器级验证：draft_id/set_id/outline_id/report_id 非空，抛 ValueError
- 溯源完整：produced_by/skill_id/skill_version/evidence_refs 四类产物齐全，to_dict 全量导出
- 不可信隔离：EvidenceCitation.trust 标记 untrusted 计数（untrusted_count）供 QA 监控
- 取消语义说明与实现一致：纯数据快照，无 IO/Qt/线程状态
- 类型注解完整

---

## src/features/edit/ai_editing/task_graph.py (行数: 601)

### 发现

- [AUD-14511] [Major] SingleWriterPolicy 无锁 check-then-set（TOCTOU），与其设想的多 graph 并发场景矛盾
  - 位置: src/features/edit/ai_editing/task_graph.py:324
  - 证据:
    ```python
    def acquire(self, timeline_id: str, skill_id: str) -> bool:
        if not timeline_id:
            return True  # 无 timeline 作用域（纯分析 graph）不占写候选
        current = self._writers.get(timeline_id)
        if current is not None and current != skill_id:
            return False
        self._writers[timeline_id] = skill_id
        return True
    ```
    类 docstring 明确设想并发（316–318 行）："只读检索/分析可并行（多 graph 可同时活跃），
    但写候选互斥。" 本模块未导入 threading；而同 feature 的兄弟模块对共享 dict 均持锁
    （agent_orchestrator.py:68 `self._lock = threading.RLock()`、agent_inbox.py:125、
    approval_service.py:35、autonomous_recovery.py:12）。
  - 影响: 两个线程同时对同一 timeline、不同 skill 调 `SkillRuntime.run` 时，都可能通过
    `current is not None` 检查后各自写入 `_writers`，双双获得写候选——单写者不变量被打破，
    可能产生双提交候选与 revision 冲突。当前调用链（assistant_service 无 threading）多为
    单线程，但策略类作为互斥原语本身不设防，且设计文档明示并发活跃。
  - 建议: `SingleWriterPolicy.__init__` 内建 `threading.Lock`，acquire/release/holder 在锁内
    完成读-改-写。
  - 置信度: 中（竞争窗口确定存在；当前是否有真实多线程 run() 调用未证实）

- [AUD-14512] [Suggestion] 超时/预算仅在执行返回后检查，无法中断挂起的 role executor
  - 位置: src/features/edit/ai_editing/task_graph.py:500
  - 证据:
    ```python
    started_at = time.monotonic()
    try:
        execute_with_budget = getattr(self.role_executor, "execute_with_budget", None)
        if callable(execute_with_budget):
            artifact = execute_with_budget(...)
        else:
            artifact = self.role_executor.execute(node.role, node.context, upstream_artifacts)
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
    ```
    571–572 行事后判定：
    ```python
    if budget.timeout_ms is not None and elapsed_ms > budget.timeout_ms:
        return f"task deadline exceeded: {elapsed_ms}ms > {budget.timeout_ms}ms"
    ```
  - 影响: 超时检测是事后归因而非中断：只实现 `execute()`（协议唯一要求的方法）且内部
    阻塞的 executor 会让整个 graph 永久卡死；`cancel_requested` 也只在节点间检查
    （452 行），进行中任务不可取消。v1 确定性 stub 无风险；LlmRoleExecutor 自带
    timeout 传递（llm_role_executor.py:383）可部分兜底，但该保证不覆盖自定义 executor。
  - 建议: 在 docstring 明示"timeout 依赖 executor 自律"；或为节点执行包一层带
    deadline 的等待（如后台线程 + join(timeout) 后标 NEEDS_USER_INPUT 并放弃其产物）。
  - 置信度: 中（现状注释已部分承认该设计；风险面向未来注入的 executor）

- [AUD-14513] [Suggestion] `expected_artifact_kind` 字段从未被实际校验，失败消息引用了未使用的声明值
  - 位置: src/features/edit/ai_editing/task_graph.py:522
  - 证据:
    ```python
    kind_ok = _artifact_kind_matches(node.role, artifact)
    if not kind_ok:
        results[node.task_id] = TaskResult(
            ...
            error_reason=(
                f"artifact kind mismatch: expected {node.expected_artifact_kind}, "
                f"got {type(artifact).__name__}"
            ),
    ```
    而 `_artifact_kind_matches`（553–565 行）只按 role→类型映射判定：
    ```python
    _EXPECTED = {
        TaskRole.CREATIVE_DIRECTOR: ScriptDraft,
        ...
    }
    expected_type = _EXPECTED.get(role)
    return expected_type is not None and isinstance(artifact, expected_type)
    ```
  - 影响: TaskNode 声明的 `expected_artifact_kind`（可自定义，__post_init__ 只在为空时
    填默认）不参与执行期校验：声明与 role 产物不符时既不报错也不拦截，字段沦为装饰；
    mismatch 报错文案引用的"expected"值与实际校验依据（role 映射）脱节，误导排障。
  - 建议: 校验时同时比对 `node.expected_artifact_kind == node.role.produces_kind`，
    不一致直接构造期报错；或让错误文案引用 role 的 produces_kind。
  - 置信度: 高

- [AUD-14514] [Suggestion] 通过 getattr 鸭子类型发现可选方法与共享可变属性，协议未声明
  - 位置: src/features/edit/ai_editing/task_graph.py:502
  - 证据:
    ```python
    execute_with_budget = getattr(self.role_executor, "execute_with_budget", None)
    if callable(execute_with_budget):
        artifact = execute_with_budget(
            node.role, node.context, upstream_artifacts, node.budget,
        )
    else:
        artifact = self.role_executor.execute(node.role, node.context, upstream_artifacts)
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    usage = getattr(self.role_executor, "last_usage", {})
    ```
    另 533–538 行读 `last_execution_source` / `last_degradation_reason`；而
    `RoleExecutor` 协议（372–375 行）仅声明 `execute`。
  - 影响: 类型检查器无法验证这些属性；executor 实现改名/拼错时静默回退到无预算路径，
    预算与 usage 记账静默失效（budget bypass 无告警）。同时结果归属依赖 executor 实例级
    可变状态，若同一 executor 被并发 graph 共享，last_usage/last_execution_source 会串账
    （顺序循环下正确）。
  - 建议: 在 `RoleExecutor` 协议中显式声明可选能力（或拆出 `BudgetedRoleExecutor`
    子协议 + `runtime_checkable`），last_* 属性改为 execute 的返回值字段。
  - 置信度: 高（结构事实确定；并发串账场景取决于部署）

- [AUD-14515] [Suggestion] 一次 execute 触发 validate() 三次重复计算
  - 位置: src/features/edit/ai_editing/task_graph.py:435
  - 证据:
    ```python
    # 1. graph 合法性
    errors = graph.validate()
    if errors:
    ...
    order = graph.topo_order()
    ```
    而 topo_order（279–280 行）内部再次校验且自身就调两次：
    ```python
    def topo_order(self) -> list[TaskNode]:
        """返回拓扑序节点列表（依赖在前）。有环时抛 ValueError。"""
        if self.validate():
            raise ValueError(f"invalid graph: {self.validate()}")
    ```
  - 影响: 每次执行对同一不可变图重复做 2–3 遍悬空依赖 + DFS 环检测（SkillRuntime.run
    里还有一遍 graph.validate，共 3–4 遍）。v1 四节点图无感知；纯冗余，且
    topo_order 的 f-string 内重复调用属明显笔误模式。
  - 建议: topo_order 内缓存 `errors = self.validate()` 一次判断一次引用；
    TaskGraphExecutor.execute 复用首次 validate 结果（或给 frozen 图加缓存字段）。
  - 置信度: 高

### 通过项
- 取消传播（§6.2 规则 6）：cancel_requested 在每节点派发前检查；上游 FAILED/CANCELLED/NEEDS_USER_INPUT 级联标 CANCELLED 且注明原因（拓扑序保证依赖先出结果，传递性成立）
- 失败可见（规则 5）：执行异常记录 `error_reason=f"{type(exc).__name__}: {exc}"`，不吞失败；仅捕获 Exception，KeyboardInterrupt/SystemExit 可穿透
- 子任务禁写红线：`assert_subagent_no_write` 执行期强制（PermissionError→FAILED）；TaskNode 构造期拒绝自环依赖；TaskGraph 拒绝重复 task_id 与空 skill_id
- DAG 校验：悬空依赖、DFS 环检测（GRAY/BLACK 正确，含环路径回溯）；悬空依赖存在时跳过环检测避免误报
- 预算收口：超预算/超时归入 NEEDS_USER_INPUT（可介入态）而非崩溃；usage 非 dict 回退 {}、float 转换失败可见报错；预算违规产物不入 artifact_by_task，下游正确级联
- typed artifact 契约：role→类型映射校验，mismatch 标 FAILED
- 单写者释放路径：实际 acquire/release 在 SkillRuntime.run 的 try/finally 中（已核实），异常路径不泄漏写候选
- 类型注解完整（含 `__all__`）
