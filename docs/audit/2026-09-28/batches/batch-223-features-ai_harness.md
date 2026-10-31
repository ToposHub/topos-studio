# batch-223 审查报告 — src/features/ai_harness（8 文件，2,916 行）

编号区间：AUD-22300–AUD-22324。汇总：Critical 0 · Major 5 · Suggestion 20。
维度：线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能 / 类型注解。
本批全部为纯 Python feature 层（无 Qt / OpenGL / GLSL），GPU·GLSL·信号防递归三个维度不适用，已在各文件通过项注明。

---

## src/features/ai_harness/result_card_builder.py (行数: 351)

### 发现

- [AUD-22300] [Suggestion] resolver 失败的兜底结果卡固定 trust="trusted"，与"trust 只降级不升级"红线存在张力
  - 位置: src/features/ai_harness/result_card_builder.py:137-144
  - 证据:
    ```python
    if typed is None:
        # resolver 未取到 → 用 ArtifactRef.summary 兜底（不崩）
        return variant_result(
            result_id=run.run_id,
            title=_title(skill_id, "运行完成"),
            summary=art.summary or f"产物 {art.artifact_id}",
            trust="trusted",
        )
    ```
  - 影响: 当 artifact_resolver 抛错或未注册时，即使是 EvidenceSet 类产物（可能含 untrusted 引用）也会以 trusted 通用卡呈现；信息不足时应默认 unverified 而非 trusted，否则用户可能跳过核查。
  - 建议: 兜底卡 trust 改用 "unverified"（无法证明可信时不下发 trusted）。
  - 置信度: 中

- [AUD-22301] [Suggestion] _evidence_refs_and_trust 对非数值 untrusted_count 直接比较，类型异常会让成功 run 渲染成错误卡
  - 位置: src/features/ai_harness/result_card_builder.py:247-253
  - 证据:
    ```python
    untrusted_count = getattr(typed, "untrusted_count", None)
    if untrusted_count is None:
        # 无 untrusted_count 属性时手动统计
        untrusted_count = sum(
            1 for c in citations if getattr(c, "trust", "") == "untrusted"
        )
    trust: TrustLevel = "unverified" if untrusted_count > 0 else "trusted"
    ```
  - 影响: 本模块自称"跨多种 artifact 形状"防御（文件头注释），但若 duck-typed artifact 的 untrusted_count 是字符串，`> 0` 抛 TypeError 被外层 except 捕获，整个 SUCCEEDED run 降级为 RENDER_FAILED 错误卡，误导用户。（真实 EvidenceSet.untrusted_count 为 int 属性，task_artifacts.py:177，故实际风险低。）
  - 建议: 容错转换，如 `int(untrusted_count or 0)` 并 try/except 回退手动统计。
  - 置信度: 低

### 通过项
- 异常与取消路径：外层统一降级 error_result，绝不抛出，符合"降级优先"设计；FAILED/CANCELLED/SKIPPED/ACTIVE 分支齐全
- 线程安全：纯函数无共享可变状态
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及（无 Qt、无渲染）
- 状态提交一致性：只读 RunRecord，不触碰 undo/revision/持久化
- 输入验证：resolver 异常与空 artifact 均有兜底；evidence refs 跳过空 id
- 类型注解：公开与私有函数注解完整

---

## src/features/ai_harness/rollout_metrics.py (行数: 151)

### 发现

- [AUD-22302] [Suggestion] quality_gate_failures 用 "gate" 子串启发式匹配，investigate/propagate 等诊断码会被误计
  - 位置: src/features/ai_harness/rollout_metrics.py:128
  - 证据:
    ```python
    for diagnostic in record.diagnostics:
        code = str(diagnostic.get("code", "UNKNOWN") or "UNKNOWN")[:80]
        diagnostic_codes[code] += 1
        if "gate" in code.lower() or code.upper().startswith("QUALITY"):
            quality_gate_failures += 1
    ```
  - 影响: 任何含 "gate" 子串的诊断码（investigate、navigate、propagate、mitigate…）都会被计入质量门失败，dashboard 指标虚高，误导 rollout 评估。
  - 建议: 改为精确前缀匹配（如 `code.lower().startswith(("quality_gate", "gate."))`）或由诊断事件显式携带 gate 标记。
  - 置信度: 高（匹配逻辑确证；现有码表中是否出现此类子串未知，影响概率中等）

### 通过项
- 线程安全：纯聚合函数，无共享可变状态；调用方传入快照
- 异常路径：cost/latency 均有空值兜底（`or 0.0`、`finished_at is not None`），p95/平均除零有保护
- 输入验证：诊断码统一 str 化并截断 80 字符；UNKNOWN 兜底
- 性能：单遍 O(n) 聚合，latencies 排序 O(n log n) 可接受
- 类型注解：dataclass 与函数注解完整；to_public_dict 输出均为 dashboard-safe 原语
- 状态提交一致性：只读 RunRecord，不写任何持久层

---

## src/features/ai_harness/run_trace.py (行数: 695)

### 发现

- [AUD-22303] [Suggestion] project() 把会话级 dropped 计数当作单 run 的截断数，误报 trace_truncated
  - 位置: src/features/ai_harness/run_trace.py:351-364
  - 证据:
    ```python
    def project(self, *, session_id: str, run_id: str) -> RunTraceProjection:
        session_id = str(session_id or "default")
        with self._lock:
            events = tuple(
                event for event in self._events.get(session_id, ())
                if event.run_id == run_id
            )
            dropped = self._dropped.get(session_id, 0)
    ```
  - 影响: `_dropped` 按会话统计；同一会话多个 run 时，丢弃事件即使全来自 run A，run B 的投影也会带 `trace_truncated=True / dropped_event_count>0`，Run Drawer 对无辜 run 误报"轨迹不完整"。
  - 建议: 丢弃时记录所属 run_id，按 run 维度提供 dropped 计数；或仅在投影包含该 run 早期事件缺失时提示。
  - 置信度: 高

- [AUD-22304] [Suggestion] fork_session 不继承源会话 dropped 计数，截断历史被当作完整历史分发
  - 位置: src/features/ai_harness/run_trace.py:411-416
  - 证据:
    ```python
    self._events[child_id] = copied
    self._next_seq[child_id] = max((event.seq for event in copied), default=-1) + 1
    self._dropped[child_id] = 0
    self._seen_ids[child_id] = {
        (event.source, event.event_id) for event in copied
    }
    ```
  - 影响: 源会话若已触发丢弃（最早 seq 缺失），fork 出的子会话 dropped=0；模块文档声称"消费者可以看出投影是部分的而不是完整重放"，此处该保证失效。
  - 建议: `_dropped[child_id] = self._dropped.get(source_session_id, 0)`（或按 boundary 之前被丢弃数精确计算）。
  - 置信度: 高

- [AUD-22305] [Suggestion] project_trace_events 的 tool call key 在 started/finished 两侧推导不对称，无标识事件会永久挂起 pending_tool_calls
  - 位置: src/features/ai_harness/run_trace.py:617-636
  - 证据:
    ```python
    if kind == "run.tool_started":
        call_key = str(
            payload.get("step_id")
            or payload.get("call_id")
            or payload.get("tool_name")
            or event.event_id
        )
        tool_started.add(call_key)
    elif kind == "run.tool_finished":
        call_key = str(
            payload.get("step_id")
            or payload.get("call_id")
            or payload.get("tool_name")
            or ""
        )
        if call_key:
            tool_started.discard(call_key)
    ```
  - 影响: 若 tool 事件不带 step_id/call_id/tool_name，started 用 event_id 入集合而 finished 用 "" 直接跳过 discard，pending_tool_calls 永不清空，UI 永远显示"工具仍在执行"。
  - 建议: finished 侧回退到相邻配对（如 started 事件回溯）或两侧一致跳过无键事件并计数。
  - 置信度: 中

- [AUD-22306] [Suggestion] project_session 对每个 run 各自全量扫描并折叠整个会话事件，O(runs×events)
  - 位置: src/features/ai_harness/run_trace.py:366-372
  - 证据:
    ```python
    def project_session(self, *, session_id: str) -> tuple[RunTraceProjection, ...]:
        session_id = str(session_id or "default")
        with self._lock:
            run_ids = tuple(dict.fromkeys(
                event.run_id for event in self._events.get(session_id, ())
            ))
        return tuple(self.project(session_id=session_id, run_id=run_id) for run_id in run_ids)
    ```
  - 影响: 每次 project 内部再持锁线性过滤整个会话列表（默认上限 4000 事件）；长会话多 run 时 Run Drawer 每次刷新做数十次全量遍历+排序，UI 线程卡顿风险。
  - 建议: 单次持锁遍历按 run_id 分桶，再对各桶分别调用纯函数 project_trace_events。
  - 置信度: 高

- [AUD-22307] [Suggestion] clear() 不清理 _next_seq，清理语义自相矛盾
  - 位置: src/features/ai_harness/run_trace.py:529-539
  - 证据:
    ```python
    def clear(self, session_id: str | None = None) -> None:
        with self._lock:
            if session_id is None:
                self._events.clear()
                self._dropped.clear()
                self._seen_ids.clear()
                return
            sid = str(session_id)
            self._events.pop(sid, None)
            self._dropped.pop(sid, None)
            self._seen_ids.pop(sid, None)
    ```
  - 影响: 清空后重新 append，seq 从旧水位继续（保留水位却不保留 `_seen_ids` 去重集）；若意图是完全重置则 `_next_seq` 应一并 pop，若意图是保留水位则 `_seen_ids` 不应清——两种语义混用，行为不可预期。
  - 建议: 明确一种语义并补齐：重置则 `self._next_seq.pop(sid, None)`；保留水位则同时保留 `_seen_ids`。
  - 置信度: 中

- [AUD-22308] [Suggestion] append() 分发 subscriber 时异常被静默吞掉且无任何日志
  - 位置: src/features/ai_harness/run_trace.py:304-311
  - 证据:
    ```python
    for subscriber in subscribers:
        try:
            subscriber(event)
        except Exception:
            # Trace observers are diagnostics/UI conveniences. They must
            # never become a new failure path for the assistant run.
            continue
    ```
  - 影响: 与 HarnessRuntime._dispatch（`_LOG.exception` 记录）不同，坏掉的 trace 观察者在此永远无人察觉；诊断层自身失去可诊断性，UI 订阅悄悄失效时无从排查。
  - 建议: 至少 `logger.debug(..., exc_info=True)` 记录后再 continue。
  - 置信度: 高

### 通过项
- 线程安全：store 全部读写路径持 RLock；subscriber 快照后锁外分发（除上述日志缺失外模式正确）
- 输入验证：from_dict/restore_snapshot 校验 source 枚举、seq 非负、重复 (source,event_id)、seq 连续性、metadata 非负，坏条目记 skipped 不中断
- 状态提交一致性（持久化）：restore 先在锁外解析为 detached 值再整体提交，失败不残留半恢复状态；export 含 schema_version
- 脱敏：payload 双重防线（sanitize_for_storage + _bounded_trace_value 有界截断/深度限制）
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及
- 类型注解：完整

---

## src/features/ai_harness/runtime.py (行数: 786)

### 发现

- [AUD-22309] [Major] finish() 在迁移校验通过前就写入 finished_at / error 字段，失败路径留下脏状态并可能发出伪状态事件
  - 位置: src/features/ai_harness/runtime.py:538-562
  - 证据:
    ```python
    now = time.time()
    state.finished_at = now
    if success:
        ...
    if not RunStatus.can_transition(state.status, target):
        # 已是终态：仍允许把失败信息补进 diagnostics，但不改状态。
        if not RunStatus.is_terminal(state.status):
            return False
    ```
  - 影响: 对 DRAFT run 调 finish(success=True)（DRAFT→SUCCEEDED 不在 _TRANSITIONS）返回 False，但 finished_at 已被写入——run 仍活跃却带 finished_at；rollout_metrics.py:131-133 只看 finished_at 非空即计入延迟统计，指标失真。对已终态 run 再次 finish(success=False) 时 error_code/error_message 会覆盖写入已成功 run，并额外发出一条状态实际未变的 RUN_STATE_CHANGED 事件。
  - 建议: 先校验 can_transition 再写入任何字段；终态兜底分支不覆盖 error 字段、不发伪 RUN_STATE_CHANGED。
  - 置信度: 高

- [AUD-22310] [Major] start() 在锁外读取 _detached_sessions，与 detach_session 存在竞态，可产生漏标 detached 的 run
  - 位置: src/features/ai_harness/runtime.py:332-343
  - 证据:
    ```python
    run_id = uuid.uuid4().hex
    state = _RunState(
        run_id=run_id,
        invocation=invocation,
        status=RunStatus.DRAFT,
        session_id=invocation.parent_session_id,
        # detach 传染：若父 session 已关闭，新建 run 也标 detached（防御）。
        session_detached=invocation.parent_session_id in self._detached_sessions,
    )
    with self._lock:
        self._runs[run_id] = state
    ```
  - 影响: 时序：线程 A 在锁外检查时 session 未 detach → 线程 B 持锁执行 detach_session（加集合并遍历既有 runs 标记）→ A 再持锁插入 run。该 run 既不在 B 的遍历中、又未带 detached 标记，随后 finish/attach_job/record_artifact 均可回写已关闭工程，违反"工程关闭后 run 不再回写"红线（§1.4）。
  - 建议: 把 detached 判定移入 `with self._lock` 临界区（构造 _RunState 前先取标志，或在锁内构造）。
  - 置信度: 高（代码顺序确证竞态窗口；实际触发需精确时序）

- [AUD-22311] [Major] cancel() 不校验 run 状态，允许对已终态 run 置 cancel token 并发布 run.cancel_requested
  - 位置: src/features/ai_harness/runtime.py:361-377
  - 证据:
    ```python
    with self._lock:
        state = self._runs.get(run_id)
        if state is None:
            return False
        if state.session_detached:
            ...
            return False
        if state.cancel_requested:
            return True  # 幂等
        state.cancel_requested = True
        state.cancel_reason = reason
    self._emit(state, RunEventKind.RUN_CANCEL_REQUESTED, payload={"reason": reason})
    ```
  - 影响: 对 SUCCEEDED/FAILED 等终态 run 调 cancel 返回 True 并追加 run.cancel_requested 事件；run_trace.py:593 将该 kind 映射为 "cancelling"，终态 run 的投影状态会被翻成 cancelling，Run Drawer 显示错误状态。同文件 handoff()（391 行）有 `is_active` 检查而 cancel() 没有，行为不一致。
  - 建议: cancel() 增加 `RunStatus.is_active` 检查，终态 run 直接返回 False（或幂等 True 且不发事件）。
  - 置信度: 高

- [AUD-22312] [Suggestion] _emit/_emit_node 在锁外向 state.events 追加，事件流不受类文档声明的 RLock 保护
  - 位置: src/features/ai_harness/runtime.py:735-743
  - 证据:
    ```python
    def _emit(self, state: _RunState, kind: str, *, payload: dict[str, Any]) -> None:
        event = RunEvent(
            run_id=state.run_id,
            kind=kind,
            payload=sanitize_for_storage(payload),
        )
        state.events.append(event)
        self._dispatch(event)
    ```
  - 影响: 类文档（126-127 行）声称"内部用 RLock 保护 _runs 与订阅者列表"，但事件列表追加发生在锁外；provider 线程 finish 与 UI 线程 emit_node_event 并发时事件因果顺序可交错（CPython 下 append 原子不会损坏，但 to_record 快照里的事件顺序可能与真实发生顺序不一致，影响审计重放）。
  - 建议: 持锁完成 `state.events.append(event)`，dispatch 保持在锁外。
  - 置信度: 中

- [AUD-22313] [Suggestion] retry() 中的空锁块为死代码
  - 位置: src/features/ai_harness/runtime.py:425-428
  - 证据:
    ```python
    new_run_id = self.start(invocation)
    with self._lock:
        new_state = self._runs.get(new_run_id)
        if new_state is not None:
            pass  # event 已在 start 内发布
    ```
  - 影响: 无任何副作用的锁获取 + 条件 pass，白增一次锁开销并误导读者以为此处有状态操作。
  - 建议: 删除该块（retry 直接 start 后 emit RUN_RETRY_REQUESTED 即可）。
  - 置信度: 高

- [AUD-22314] [Suggestion] detach 后无任何 re-attach 路径，_detached_sessions 只增不减
  - 位置: src/features/ai_harness/runtime.py:701-724
  - 证据:
    ```python
    def detach_session(self, session_id: str) -> int:
        ...
        n = 0
        with self._lock:
            self._detached_sessions.add(session_id)
            for state in self._runs.values():
                ...
    ```
  - 影响: runtime 设计支持跨进程 restore_snapshot（长生命周期实例）；若宿主以稳定 session_id 关闭再打开工程，start()（339 行）会把新 run 直接标 detached、restore_snapshot（234 行）也按 detached 恢复，所有写路径被永久拒绝且无 API 解除，工程重开后 AI 功能静默瘫痪。
  - 建议: 提供 attach_session(session_id) 在工程重开时移出集合（并解除对应 run 的 detached 标记），或文档明确 runtime 必须随工程生命周期重建。
  - 置信度: 中（取决于宿主是否复用 runtime 实例与 session_id）

### 通过项
- 订阅者管理：add_subscriber 返回幂等退订；_dispatch 锁外分发、异常记录日志不影响 run
- 状态机：_set_status_unlocked 统一走 RunStatus.transition 校验，非法迁移显式失败
- restore_snapshot：schema/形状/重复 run_id/invocation 完整性校验，活跃 run fail-closed 转 FAILED+RUN_INTERRUPTED，不复活孤儿 worker
- 协作取消：cancel token 幂等、detach 后拒写路径（set_status/finish/attach_job/record_*）一致
- record_progress：单调 clamp 0.0–1.0
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及
- 类型注解：完整

---

## src/features/ai_harness/runtime_invariants.py (行数: 139)

### 发现

- [AUD-22315] [Major] strict 模式抛出的 RuntimeError 会被 orchestrator 的订阅分发吞掉，strict 经 install() 永不生效
  - 位置: src/features/ai_harness/runtime_invariants.py:95-96（配合 agent_orchestrator.py:684-689）
  - 证据:
    ```python
    self._seen.add(key)
    self._violations.append(violation)
    if self._strict:
        raise RuntimeError(f"runtime invariant violated: {violation.code}: {violation.message}")
    ```
  - 影响: 模块文档承诺"host can install the engine in strict mode during tests"，但 install() 经 orchestrator.subscribe 接入，AgentOrchestrator._notify_all 对每个 sink `except Exception: pass`（agent_orchestrator.py:686-689）——strict 抛错被静默吞掉，违例既不上抛也不留痕（RuntimeError 无日志），strict 语义完全失效。
  - 建议: strict 模式改为记录后由宿主轮询 violations / clear 主动断言，或在 engine 内对 strict 违例打日志（logging.error）保证可见；文档同步修正。
  - 置信度: 高（两条代码路径均确证）

- [AUD-22316] [Suggestion] check_run 全量回放时 `events[:-1]` 语义错位，终态之后还有事件的 run 会对早期合法 retry 误报 RETRY_AFTER_TERMINAL
  - 位置: src/features/ai_harness/runtime_invariants.py:120-135
  - 证据:
    ```python
    if kind == AgentEventKind.RUN_REQUEST_RETRY:
        prior_terminal = any(
            str(getattr(candidate, "kind", "")) in {
                AgentEventKind.RUN_COMPLETED,
                AgentEventKind.RUN_FAILED,
                AgentEventKind.RUN_CANCELLED,
            }
            for candidate in events[:-1]
        )
    ```
  - 影响: `events[:-1]` 只排除"最后一个事件"，不等于"当前事件之前"。实时路径（_on_event，新事件恰为最后一个）正确；但 check_run 对历史中每个 retry 事件重放时，若 run 在该 retry 之后进入终态且终态事件后面还有任何事件（如 run.usage_recorded / artifact.staged 常在 completed 后发出，见 agent_orchestrator.py:391-396），就会把发生在终态之前的合法请求重试误判为 RETRY_AFTER_TERMINAL。
  - 建议: check 传入当前事件索引或按 event_id 定位截止位置，仅扫描当前事件之前的序列。
  - 置信度: 中

- [AUD-22317] [Suggestion] _on_event 的 _seen/_violations 无锁，订阅回调跨线程时同一违例可被重复记录
  - 位置: src/features/ai_harness/runtime_invariants.py:90-96
  - 证据:
    ```python
    key = (violation.run_id, violation.code, violation.event_id)
    if key in self._seen:
        continue
    self._seen.add(key)
    self._violations.append(violation)
    ```
  - 影响: orchestrator 事件可从不同 worker 线程触发 _notify_all；check-then-add 竞态可让同一违例入列多次，violations 报告重复计数。
  - 建议: 加 threading.Lock 保护判重+追加（clear 同锁）。
  - 置信度: 中

- [AUD-22318] [Suggestion] 每个事件回放全量历史做 any() 扫描，整 run 累计 O(n²)
  - 位置: src/features/ai_harness/runtime_invariants.py:84-96
  - 证据:
    ```python
    def _on_event(self, event: Any) -> None:
        events = tuple(self._orchestrator.get_events(getattr(event, "run_id", "")))
        for check in self._checks:
            violation = check(event, events)
    ```
  - 影响: 每个新事件复制全量事件列表并对 TOOL_FINISH 检查做 O(n) any() 扫描；数千事件的 run 在订阅回调里累计 O(n²)，拖慢事件流（该回调挂在每次 _notify_all 上）。
  - 建议: 增量维护已见 (kind, call_id) 索引 / 终态标志，检查退化为 O(1)。
  - 置信度: 高（复杂度确证；实际事件量级未知）

### 通过项
- 检查为只读诊断，不改 orchestrator 状态；install/uninstall 幂等
- InvariantViolation 为 frozen dataclass，to_dict 输出纯字符串
- GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 持久化：不涉及
- 类型注解：完整（InvariantCheck 类型别名明确）

---

## src/features/ai_harness/scheduler_adapter.py (行数: 191)

### 发现

- [AUD-22319] [Suggestion] JobRunBridge 的 _bindings/_job_to_runs 无并发保护，Job 回调线程与 UI 线程并发时可丢绑定或迭代崩溃
  - 位置: src/features/ai_harness/scheduler_adapter.py:136-183
  - 证据:
    ```python
    def bind(self, run_id: str, registry: Any) -> None:
        self._bindings.setdefault(run_id, {})["_registry"] = registry
    ...
    def propagate_cancel(self, run_id: str) -> int:
        n = 0
        binding = self._bindings.get(run_id, {})
        registry = binding.get("_registry")
        for job_id in self._job_to_runs.keys():
            if run_id not in self._job_to_runs.get(job_id, set()):
    ```
  - 影响: bind/track/sync 在 Job 完成回调（worker 线程）与 propagate_cancel（UI 取消路径）并发时：setdefault+赋值非原子可丢 registry 或 job→run 映射，取消无法传播到 Job（削弱 P0.6 真实取消红线）；propagate_cancel 迭代 `_job_to_runs.keys()` 期间他线程插入可抛 `RuntimeError: dictionary changed size during iteration`。
  - 建议: 加 threading.Lock；或 propagate_cancel 先 `tuple(self._job_to_runs)` 快照再遍历。
  - 置信度: 中

- [AUD-22320] [Suggestion] 每个 run 仅存单一 _registry，与模块自述"一个 run 可能挂多个 Job（分析 + 生成 + 渲染）"矛盾，跨 registry 的 Job 取消被静默跳过
  - 位置: src/features/ai_harness/scheduler_adapter.py:37-39, 136-145
  - 证据:
    ```python
    #: 一个 run 可能挂多个 Job（分析 + 生成 + 渲染），一个 Job 也可能被多 run 引用
    #: （不常见，但 adapter 不阻止）。
    ...
    def bind(self, run_id: str, registry: Any) -> None:
        """声明 run 将在 ``registry`` 中创建 Job。后续 :meth:`sync` 据此查找。"""
        self._bindings.setdefault(run_id, {})["_registry"] = registry
    ```
  - 影响: 注释（131 行 `# run_id -> {job_id -> registry}`）与实现不符——实际只保存一个 "_registry"，后 bind 覆盖前 bind；若 run 的 Job 分属 AnalysisJobScheduler 与 JobRegistry（正是文档描述的场景），propagate_cancel 只查最后绑定的 registry，其余 Job 永远不会被 request_cancel。
  - 建议: `_bindings[run_id]` 按 registry 身份存集合（如 dict[id(registry), registry]），sync/propagate_cancel 遍历全部 registry；或至少在文档中标明单 registry 限制。
  - 置信度: 中

### 通过项
- poll_cancel_to_job：对 terminal 状态与已 cancelling 的 Job 防御正确，request_cancel 缺失时安全跳过
- job_ref_from_handle / sync_job_to_run：只读 JobHandle 快照字段，progress 有 `or 0.0` 兜底，attach/update 双路径去重正确
- 不引入 Qt / 网络 / GPU；类型注解完整
- 异常路径：duck-typed get/get_job 探测，不可调用即返回 False，不抛出

---

## src/features/ai_harness/session_fork.py (行数: 138)

### 发现

- [AUD-22321] [Suggestion] SessionEventLog 无任何锁保护，append 的 seq 计算存在 check-then-act 竞态
  - 位置: src/features/ai_harness/session_fork.py:74-94
  - 证据:
    ```python
    def append(self, session_id, kind, *, run_id="", payload=None) -> SessionEvent:
        session_id = str(session_id or "").strip()
        rows = self._events.setdefault(session_id, [])
        event = SessionEvent(
            session_id=session_id,
            seq=rows[-1].seq + 1 if rows else 0,
            ...
        )
        rows.append(event)
    ```
  - 影响: 并发 append 时两个线程可读到同一 `rows[-1].seq` 并各自 +1，产生重复 seq，破坏"append-only、seq 单调"不变量，进而使 fork 边界校验与重放失真。同目录 RunTraceStore 对同构操作全程持 RLock，本模块不一致。当前 src 内无其他调用方（run_trace 仅复用 SessionFork 数据类），属潜在风险。
  - 建议: 与 RunTraceStore 一致加 threading.RLock。
  - 置信度: 中（不一致确证；现存无并发调用方）

### 通过项
- fork 边界校验完整：不存在 seq 拒绝、boundary<-1 拒绝、child_id 冲突/空拒绝
- append 截断保序：`del rows[:...]` 后 seq 仍继续递增
- SessionEvent.__post_init__ 输入验证（空 session_id/负 seq/空 kind 拒绝）、payload 经 sanitize_for_storage 脱敏
- 类型注解完整；GPU/GLSL/信号/持久化不涉及

---

## src/features/ai_harness/skill_catalog.py (行数: 465)

### 发现

- [AUD-22322] [Major] brief_template / input_schema 数值未经类型校验直接 int()/float()，畸形 manifest 会炸掉整个目录渲染
  - 位置: src/features/ai_harness/skill_catalog.py:74-76
  - 证据:
    ```python
    td_ideal = int(td_default["ideal"]) if isinstance(td_default, dict) and "ideal" in td_default else None
    td_min = int(td_default.get("min", 1)) if isinstance(td_default, dict) else None
    td_max = int(td_default.get("max", 100000)) if isinstance(td_default, dict) else None
    ```
  - 证据补充: `_field_from_spec` 内 `min_v = float(spec["minimum"])`（124 行）同样裸转换。SkillManifest.brief_template 声明为 `dict[str, Any]`（skill_manifest.py:194），无 schema 校验。
  - 影响: 内置 manifest 均为 int（skill_manifest.py:365 等），但第三方 / local_dev skill 写 `"ideal": "auto"`、`"minimum": "fast"` 之类值时 build_form_fields 抛 ValueError/TypeError，SkillCatalogView.cards() 整体失败 → 聊天推荐卡与输入表单全部不可用，违反"外部数据须验证"铁律。
  - 建议: 安全转换辅助函数（isinstance 数值检查 + try/except 回退 None/默认值），或在上游 SkillManifest 加载时校验 brief_template/input_schema 形状。
  - 置信度: 中（裸转换确证；外部 skill 加载路径是否另有校验未在本批次内）

- [AUD-22323] [Suggestion] _merge_p2_meta 宽泛 except + pass 且无日志，静默掩盖 editorial_skills 导入/合并错误
  - 位置: src/features/ai_harness/skill_catalog.py:360-370
  - 证据:
    ```python
    def _merge_p2_meta() -> None:
        """把 P2 editorial_skills.P2_SKILL_META 合并进 _SKILL_META（运行时一次性）。"""
        try:
            from src.features.ai_harness.editorial_skills import P2_SKILL_META
            for sid, meta in P2_SKILL_META.items():
                _SKILL_META.setdefault(sid, meta)
        except Exception:  # noqa: BLE001 — editorial_skills 可能在该 import 阶段未就绪
            pass
    ```
  - 影响: 违反"不得宽泛 except 掩盖领域错误"铁律；P2 元数据缺失时（estimated_work/trigger_examples 为空）无任何日志痕迹，推荐卡降质无从排查。
  - 建议: 缩窄为 ImportError 并 `logging.debug/exception` 记录；循环内错误单独处理。
  - 置信度: 高

- [AUD-22324] [Suggestion] ui_card.form_fields / labels / enum 对非预期形状无防御，字符串被拆成字符级垃圾字段
  - 位置: src/features/ai_harness/skill_catalog.py:66, 120, 138
  - 证据:
    ```python
    declared_order: tuple[str, ...] = tuple(ui_card.get("form_fields", ()) or ())
    ...
    label = ui_card.get("labels", {}).get(name, _default_label(name))
    ...
    options=tuple(spec.get("enum", [])) if isinstance(spec.get("enum"), list) else (),
    ```
  - 影响: form_fields 为字符串时 `tuple("ab")` → ('a','b') 逐字符生成垃圾表单字段；labels 为 list 时 `.get` 抛 AttributeError 使 cards() 崩溃。与 AUD-22322 同源（manifest 未验证）。
  - 建议: 对 form_fields 做 isinstance(item, str) 过滤；labels 先验证 dict。
  - 置信度: 中

### 通过项
- 前置条件不静默放行：unverified(local_dev) / 缺证据 / 缺模型 / 无 profile 均令 ready=False 并给出 blocking_messages
- 权限红线：只读 manifest，不在本层提权；risk_label 展示完整
- recommend 排序稳定（ready 优先 + title）
- 类型注解完整（registry 参数 duck-typed Any 属有意并注释说明）
- GPU/GLSL/信号/持久化/线程安全：纯只读聚合，无共享可变状态（除模块级 _SKILL_META 一次性合并外）
