# batch-127 审查报告 — src/features/edit/ai_editing/（8 文件，2,779 行）

审查日期：2026-09-28。编号区间：AUD-12700–AUD-12799。
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。
本批次为纯数据/逻辑层（无 Qt widget、无 ModernGL、无 GLSL），GPU 与 GLSL·HDR 维度对所有文件均不适用。

汇总：Critical 0 · Major 5 · Suggestion 8。

---

## src/features/edit/ai_editing/__init__.py (行数: 142)

纯 re-export 包入口，无逻辑。导出 edit_project_service（CommitResult / EditProjectService / ProjectSequences / RevisionConflict）、diagnostics、editorial_models、models、capability_manifest / composition_plugin / mcp_plugin / provider_plugin / schema / semantic_index / snapshot_service / plan_validator 的公共符号；`__all__`（87–142 行）与实际 import 一一核对无遗漏、无多余。注意：`agent_models` / `agent_orchestrator` / `agent_inbox` / `agent_metrics` / `agent_feature_flags` / `action_contract` / `agent_phase_presenter` 不在本入口导出，由调用方按模块直接导入。

### 发现
（无）

### 通过项
- 纯 re-export，无逻辑，无状态，无线程/GPU/信号问题。
- `__all__` 与 import 列表逐项核对一致。

---

## src/features/edit/ai_editing/action_contract.py (行数: 237)

### 发现
- [AUD-12700] [Suggestion] PostconditionResult.to_dict 丢弃 finding 的 observed 字段
  - 位置: src/features/edit/ai_editing/action_contract.py:185-189
  - 证据:
    ```python
    "findings": [
        {"kind": f.kind, "target_ref": f.target_ref, "passed": f.passed,
         "severity": f.severity, "message": f.message}
        for f in self.findings
    ],
    ```
    而 `PostconditionFinding`（152–159 行）定义了 `observed: Any = None`。
  - 影响: 审计/持久化轨迹丢失"实际观测值"，只能看到 message 文本，事后无法机器比对期望 vs 实际，削弱 OS-10 verify 阶段的可审计性。
  - 建议: 序列化时加入 `"observed": sanitize(f.observed)`（或至少对可 JSON 化类型透传）。
  - 置信度: 高

### 通过项
- 输入验证：PostconditionSpec/CompensationSpec/ActionContract 的 `__post_init__` 校验 kind、severity、strategy、undo_tool 配套、负数 cost（73–77 / 88–92 / 111–117 行）。
- 全部为 frozen dataclass，无可变共享状态，线程安全。
- `evaluate_postconditions` 的补偿触发条件（error 且可逆）逻辑正确；异常向上传播符合"不宽泛掩盖领域错误"。
- 类型注解完整。

---

## src/features/edit/ai_editing/agent_feature_flags.py (行数: 93)

### 发现
（无）

### 通过项
- 线程安全：`_override` 读写均在 `_override_lock` 内（60–63 / 77–79 行）；override 不污染 os.environ。
- 优先级（override → env → 默认 True）实现与文档一致；未知 env 值经 `_truthy` 失败关闭。
- 纯逻辑无状态泄漏，类型注解完整。

---

## src/features/edit/ai_editing/agent_inbox.py (行数: 257)

### 发现
- [AUD-12701] [Major] 已 claim 的条目永久占用 per-run 容量，长 run 会拒绝新的用户转向输入
  - 位置: src/features/edit/ai_editing/agent_inbox.py:163 与 189-204
  - 证据:
    ```python
    if len(queue) >= self._max_items_per_run:
        raise OverflowError("agent inbox is full")
    ```
    claim() 中被认领的条目以 CLAIMED 状态整体放回队列：
    ```python
    for item in queue:
        if item.item_id in claimed_ids:
            updated.append(InboxItem(..., status=InboxItemStatus.CLAIMED, ...))
        else:
            updated.append(item)
    self._items[run_id] = updated
    ```
    队列唯一的收缩途径是 `clear_run`（仅 run 结束时由宿主调用，见 ai_edit_controller.py:2976）。
  - 影响: `max_items_per_run=32` 实际限制的是单个 run 的**终身入队总数**而非待处理数；一个长 run 在累计 32 条输入（无论是否已消费）后，用户的 steer/followup 将抛 `OverflowError` 被拒。当前 chat_run_driver 每 run 最多约 4 轮 followup（chat_run_driver.py:352），实际触发概率低，但机制性缺陷存在。
  - 建议: enqueue 的容量检查改为只统计 QUEUED 条目，或 claim 后将已消费条目移出主队列（单独保留 trace 元数据）。
  - 置信度: 中（缺陷代码证据充分，触发概率低）

### 通过项
- 线程安全：RLock 覆盖所有 `_items` / `_event_sink` 访问；sink 回调在锁外调用（166-168 / 225-236 行），不会死锁。
- 输入验证：run_id / kind / content 非空、内容长度上限、每 run 条数上限（143-152 行）；InboxItem `__post_init__` 二次校验。
- 隐私：trace 只输出 content_hash/长度，不落原始输入（57-68 行）；注入消息带 untrusted 标注（92-95 行）。
- clear_run 幂等、返回丢弃计数。类型注解完整。

---

## src/features/edit/ai_editing/agent_metrics.py (行数: 238)

### 发现
- [AUD-12702] [Major] 指标收集器完全无锁，跨线程读写可致迭代崩溃与计数丢失
  - 位置: src/features/edit/ai_editing/agent_metrics.py:92 与 120-121、101-110、157-197
  - 证据:
    ```python
    def __init__(self, *, time_fn: Callable[[], float] | None = None) -> None:
        self._time = time_fn or time.monotonic
        self._trackers: dict[str, _RunTracker] = {}
    ```
    `all_metrics()` 直接迭代：
    ```python
    return tuple(self._build_metrics(t) for t in self._trackers.values())
    ```
    而 `_on_event`（由 orchestrator 在 worker 线程回调）执行 `self._trackers[run_id] = tracker` 与 `tracker.tool_call_count += 1`（166-197 行）；`record_first_token` 由 controller 在首个 stream_delta 时调用（108-110 行），查询接口可在 UI 线程调用。
  - 影响: `_on_event` 插入新 run 时与 `all_metrics` 的生成器迭代并发，可抛 `RuntimeError: dictionary changed size during iteration`；`tool_call_count += 1` 等读-改-写非原子，并发事件丢计数；`_build_metrics` 可读到 outcome 已置而 terminal_time 未置的撕裂快照。同包的 AgentInbox 尚且全程持锁，此处形成不一致的线程纪律。
  - 建议: 为 `_trackers` 增加一把轻量 Lock（或改用 `collections.Counter`+不可变替换），查询走快照拷贝。
  - 置信度: 高
- [AUD-12703] [Major] partial 判定依赖的 payload 键从未被生产，PARTIAL_SUCCESS 永远统计为 succeeded
  - 位置: src/features/edit/ai_editing/agent_metrics.py:188-194（对照 agent_orchestrator.py:256-271）
  - 证据:
    ```python
    payload = getattr(event, "payload", {}) or {}
    tracker.outcome = (
        AgentRunState.PARTIAL_SUCCESS
        if payload.get("partial")
        else AgentRunState.SUCCEEDED
    )
    ```
    而 `run.completed` 唯一来源 `finish_verification` 的 payload 只有：
    ```python
    payload={"reason": reason} if reason else None,
    ```
    全库 grep 确认无任何代码在 run.completed payload 中写入 "partial"。
  - 影响: 发布门（release_gate.py 消费 summary() 的 outcome 混合比）永远看不到 partial，partial 回归被静默记为成功，A7/§9 质量门失真。
  - 建议: `finish_verification` 在 partial 分支的 payload 中加入 `{"partial": True}`，或 metrics 改从 run 快照状态取终态。
  - 置信度: 高
- [AUD-12704] [Suggestion] `_trackers` 无淘汰，随进程生命周期无界增长
  - 位置: src/features/edit/ai_editing/agent_metrics.py:163-169
  - 证据:
    ```python
    if tracker is None:
        tracker = _RunTracker(run_id=run_id, start_time=now)
        self._trackers[run_id] = tracker
    ```
    无任何删除/过期路径。
  - 影响: 长会话中每个历史 run 保留一个 tracker，summary() 的聚合范围也无界膨胀（历史 run 永远参与平均）。
  - 建议: 提供 `prune(keep_last_n)` 或按 terminal 时间做窗口聚合。
  - 置信度: 高

### 通过项
- 订阅只读不可变事件，不阻塞 orchestrator 主流程（attach 经 orchestrator.subscribe 返回退订句柄）。
- 异常路径：`_on_event` 用 getattr 容错缺失字段；首事件缺失时以任一事件锚定 start（164-169 行注释明确）。
- 时延换算（monotonic 差 ×1000）与空值保护正确；类型注解完整。

---

## src/features/edit/ai_editing/agent_models.py (行数: 897)

### 发现
- [AUD-12705] [Suggestion] from_dict 对持久化序列字段无类型校验，字符串会被静默拆成字符元组
  - 位置: src/features/edit/ai_editing/agent_models.py:260、407、774、827、830
  - 证据:
    ```python
    acceptance_criteria=tuple(d.get("acceptance_criteria", [])),
    ```
    ```python
    artifact_refs=tuple(d.get("artifact_refs", [])),
    ```
    同模式见 AgentRun.from_dict 与 AgentCheckpoint.from_dict 的 completed_steps / protected_context。
  - 影响: 持久化数据损坏（如字段被写成字符串 "abc"）时不报错，得到 `('a','b','c')` 之类的静默变形条目，违背"外部数据须验证"铁律的精神（restore 路径只有抛异常才会被跳过，这种变形不会抛）。
  - 建议: from_dict 中先 `isinstance(x, list)` 校验再转换，否则按损坏条目处理。
  - 置信度: 高（行为确定）/ 中（触发依赖持久层损坏）
- [AUD-12706] [Suggestion] schema_version 只写不读，无迁移钩子
  - 位置: src/features/edit/ai_editing/agent_models.py:44 与 742、760-777
  - 证据:
    ```python
    AGENT_MODELS_SCHEMA_VERSION: int = 1
    ```
    to_dict 写入 `"schema_version": AGENT_MODELS_SCHEMA_VERSION`，而 `AgentRun.from_dict` 从不读取该键，任何版本都按当前结构解析。
  - 影响: 模块头注释承诺"旧版本 from_dict 必须迁移、绝不静默丢弃"，但当前实现既不检查版本也无迁移分支；未来 bump 版本后旧 checkpoint 会被字段默认值静默降级。
  - 建议: from_dict 读取 schema_version，未知版本显式抛错或走迁移函数。
  - 置信度: 高

### 通过项
- 状态机：`transition` 非法翻转抛 ValueError；终态无出边；`normalize` 对未知状态 fail-closed 落 FAILED 而非宽松态（143-177 行）。
- 持久化脱敏：所有自由文本（title/user_goal/content/state_reason/provenance/diff_summary/payload）经 `sanitize_for_storage`（已核实其对 dict/list 递归、绝对路径与 secret 形状脱敏，invocation_models.py:669-694）。
- frozen dataclass 不可变快照，线程安全；`_opt_int` 容错转换；`window_ratio` 对 limit 缺失/为 0 返回 None。
- 输入验证：AgentRun/AgentEvent/ApprovalRequest 的 run_id/tool_name 必填；未知 risk fail-safe 为 WRITE；未知 kind 显式 UNKNOWN。
- 类型注解完整。

---

## src/features/edit/ai_editing/agent_orchestrator.py (行数: 697)

### 发现
- [AUD-12707] [Major] fail() 声称支持任意非终态，但 PAUSED_FOR_RESUME 无 FAILED 出边，实际抛 ValueError
  - 位置: src/features/edit/ai_editing/agent_orchestrator.py:273-287（对照 agent_models.py:127）
  - 证据:
    ```python
    def fail(self, run_id: str, *, reason: str = "") -> AgentRun:
        """Move a run straight to FAILED from any non-terminal state."""
        run = self.get_run(run_id)
        ...
        if run.is_terminal:
            return run
        return self.transition(run_id, AgentRunState.FAILED, ...)
    ```
    而状态机：
    ```python
    PAUSED_FOR_RESUME: frozenset({GATHERING_CONTEXT, CANCELLED}),
    ```
    动态放行规则只覆盖 CANCELLED（agent_models.py:147-152）。
  - 影响: 对恢复失败/校验失败的 paused run 调用 `fail()` 会以 ValueError 崩出而非落 FAILED，违反自身文档契约；异常路径（重启恢复出错）被二次异常掩盖。
  - 建议: 在 `_TRANSITIONS` 的 PAUSED_FOR_RESUME 中加入 FAILED，或 fail() 对该态先经 CANCELLED。
  - 置信度: 高
- [AUD-12708] [Major] runs/events/tasks/checkpoints 四张映射无任何淘汰路径，进程级无界内存增长
  - 位置: src/features/edit/ai_editing/agent_orchestrator.py:70-77
  - 证据:
    ```python
    self._runs: dict[str, AgentRun] = {}
    self._tasks: dict[str, AgentTask] = {}
    self._events: dict[str, list[AgentEvent]] = {}
    ...
    self._checkpoints: dict[str, list[Any]] = {}
    ```
    全文无 remove/prune/GC API；`_events` 经 `_emit_locked` 每事件 append（629 行）。宿主侧确认：controller 持有单例 orchestrator（ai_edit_controller.py:2294，伴随整个应用会话），每次聊天新建 run。
  - 影响: 长编辑会话中每个历史 run 的完整事件流（含 tool_progress 高频事件）与 checkpoint 常驻内存，构成慢性内存泄漏；`_checkpoints` 按 run 无上限累积。
  - 建议: 增加 `prune_terminal_runs(keep_last_n)`（保留最近 N 个终态 run 供 Run Drawer），并在 run 终态事件时由宿主或内部触发。
  - 置信度: 高
- [AUD-12709] [Suggestion] restore_state 用裸 `except Exception: continue` 静默吞掉损坏条目，无任何日志
  - 位置: src/features/edit/ai_editing/agent_orchestrator.py:567-581
  - 证据:
    ```python
    try:
        task = AgentTask.from_dict(task_data)
        self._tasks[task.task_id] = task
    except Exception:  # pragma: no cover - skip corrupt entry
        continue
    ```
    runs 循环同模式（575-581 行）。
  - 影响: 跳过损坏持久化条目是合理设计，但零日志意味着 from_dict 内部代码缺陷（非数据损坏）也会被无声吞掉，违背"不宽泛掩盖领域错误"的红线精神。
  - 建议: 捕获后至少 logger.warning 记录 run_id/task_id 与异常摘要。
  - 置信度: 高
- [AUD-12710] [Suggestion] record_usage/stage_artifact/save_checkpoint 对终态 run 静默 no-op，调用方无法感知写入被丢弃
  - 位置: src/features/edit/ai_editing/agent_orchestrator.py:375-376、407-408、457-458
  - 证据:
    ```python
    run = self._require_run_locked(run_id)
    if run.is_terminal:
        return
    ```
    三处均返回 None。
  - 影响: 迟到的 usage/artifact 回调（如取消后网络返回）被丢弃是既定设计，但无返回值/事件区分"已记录"与"被丢弃"，宿主无法补偿（例如把 artifact 转挂到新 run 或提示用户）。
  - 建议: 返回 bool（或 emit 一个 `run.late_write_dropped` 类事件）供宿主决策。
  - 置信度: 中
- [AUD-12711] [Suggestion] _notify_all 以 `except Exception: pass` 吞掉订阅者异常，无诊断痕迹
  - 位置: src/features/edit/ai_editing/agent_orchestrator.py:686-689
  - 证据:
    ```python
    for event in events:
        for sink in subscribers:
            try:
                sink(event)
            except Exception:  # pragma: no cover - defensive: never let a sink crash the run
                pass
    ```
  - 影响: 防 sink 崩溃扩散是正确取向，但连 debug 日志都没有，订阅者（面板/指标/诊断）持续抛错时完全不可见，问题只表现为"某面板不更新"。
  - 建议: 至少 logger.debug(..., exc_info=True)（宿主侧同模式的 drawer 更新就是这么做的，见 ai_edit_controller.py:2964）。
  - 置信度: 高

### 通过项
- 线程安全：单一 RLock 覆盖 runs/tasks/events/subscribers/active 映射；通知在锁外进行（677-689 行），慢/抛异常 sink 不阻塞 orchestrator，与模块头声明的并发契约一致。
- TOCTOU 防御：`_advance`/`request_cancel`/`begin_executing` 等先查后转的路径在锁内经 `with_state` 二次验证，竞态下抛显式异常而非落错态。
- 取消路径：request_cancel 对终态 no-op 返回 False；cancel_requested 先于 CANCELLED 事件发出（306-317 行），满足指标采集时序。
- 持久化一致性：export_state 在锁内快照；restore_state 逐条容错合并、终态覆盖内存副本；active 映射恢复后经 active_run_id 的 run 存在性+非终态校验兜底（518-525 行）。
- 订阅/退订闭包正确（591-602 行）。类型注解完整。

---

## src/features/edit/ai_editing/agent_phase_presenter.py (行数: 218)

### 发现
- [AUD-12712] [Suggestion] RUN_CANCELLED 事件被呈现为非终态的"正在取消…"，事件驱动 UI 会停在取消中
  - 位置: src/features/edit/ai_editing/agent_phase_presenter.py:179-183
  - 证据:
    ```python
    if event_kind in (
        AgentEventKind.RUN_CANCEL_REQUESTED,
        AgentEventKind.RUN_CANCELLED,
    ):
        return PhasePresentation(label="正在取消…", kind="active")
    ```
    对照 `present_state(CANCELLED)` 返回终态"已取消"（124 行）。
  - 影响: 仅按事件更新相位的界面（present_event 的文档定位）在收到 run.cancelled 后显示永不结束的"正在取消…"（auto_continue=True），spinner/等待态不会按事件收口；须依赖后续状态推送才能纠正。
  - 建议: 将 RUN_CANCELLED 单独映射为 `is_terminal=True, kind="terminal", label="已取消"`，仅 RUN_CANCEL_REQUESTED 保留"正在取消…"。
  - 置信度: 高（映射不一致确定；实际 UI 影响取决于宿主是否混用 present_state）

### 通过项
- 状态全覆盖：12 个状态分支与 normalize 的 fail-closed（未知→FAILED→"失败"）一致，末尾"未知状态"仅为防御性兜底。
- 不暴露思维链：标签全部为动作/阶段描述，无模型内部内容透出（符合 §A3 约束）。
- 用户阻塞三态正确设 auto_continue=False；bookkeeping 事件返回 None 不扰动相位。
- 纯函数无状态，线程安全；类型注解完整。

---

## 汇总统计

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 5 | AUD-12701, AUD-12702, AUD-12703, AUD-12707, AUD-12708 |
| Suggestion | 8 | AUD-12700, AUD-12704, AUD-12705, AUD-12706, AUD-12709, AUD-12710, AUD-12711, AUD-12712 |

Major 主题：inbox 已消费条目挤占容量（12701）；指标收集器无锁跨线程（12702）；partial 统计永假（12703）；PAUSED_FOR_RESUME 不可 fail（12707）；orchestrator 四映射无界增长（12708）。
