# 批次报告：batch-148 — src/features/edit/ai_editing/

- 编号区间：AUD-14800–AUD-14899
- 审查日期：2026-09-28
- 文件数：5（1,730 行）
- 结论汇总：Critical 0 / Major 6 / Suggestion 12
- 说明：本批次为纯逻辑层（无 Qt 控件、无 ModernGL/GLSL、无渲染线程交互），GPU 生命周期与信号防递归两维标注"不适用"；审查重点落在幂等台账正确性、审计契约、CLI 异常路径与事务授权/diff 一致性。

---

## src/features/edit/ai_editing/tool_operation_ledger.py (行数: 340)

### 发现

- [AUD-14800] [Major] 指纹规范化对字符串/容器做静默截断，不同参数可碰撞出相同指纹导致错误 REPLAY
  - 位置: src/features/edit/ai_editing/tool_operation_ledger.py:49-54
  - 证据:
    ```python
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda item: str(item[0]))[:64]
        return {str(key): _canonical(item, depth=depth + 1) for key, item in items}
    if isinstance(value, (list, tuple)):
        return [_canonical(item, depth=depth + 1) for item in value[:64]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value[:2000] if isinstance(value, str) else value
    ```
  - 影响: 两个仅在"第 2000 字符之后"不同（或第 64 个键/元素之后不同）的调用参数得到同一 fingerprint。对 `IDEMPOTENCY_CONTENT_KEY` 工具（catalog 中多个工具使用 content_key，跨 run 去重），第二次不同调用会命中 `begin()` 的 REPLAY 分支（tool_operation_ledger.py:210-211），直接返回第一次的结果而不再执行——副作用类工具拿到的是别人的结果，且无任何告警。字幕/转录类长文本参数最容易越过 2000 字符阈值。
  - 建议: 截断时把被丢弃部分的长度（或其 hash）并入规范化结果，如 `{"__trunc__": value[:2000], "__len__": len(value)}`，使"截断前不同"的输入仍产生不同指纹。
  - 置信度: 中（机制确定；触发依赖超长参数，catalog 显示 content_key 策略真实在用）

- [AUD-14801] [Suggestion] `_canonical` 对不可序列化对象回退 `str(value)`，跨进程指纹不稳定，重启后去重失效
  - 位置: src/features/edit/ai_editing/tool_operation_ledger.py:55
  - 证据:
    ```python
    return str(value)[:2000]
    ```
  - 影响: 普通 Python 对象的 `str()` 含内存地址（`<Foo object at 0x...>`）。`import_all` 专为跨重启恢复设计（RUNNING→UNKNOWN，tool_operation_ledger.py:305-313），但同一参数在重启后会算出不同 fingerprint，恢复的记录永远匹配不上新调用——幂等台账"防重复执行"的核心保证对这些参数失效（fail-open，重复副作用）。
  - 建议: 对不可序列化值返回稳定的类型标记（如 `f"<{type(value).__name__}>"`）或直接抛出领域错误，要求调用方传入 JSON 兼容参数。
  - 置信度: 中（代码事实确定；实际调用是否传非 JSON 参数未在本批次验证）

- [AUD-14802] [Suggestion] `ToolResult` 的反序列化逻辑在 ledger 内手写重复实现，与 `ToolResult.to_dict` 无对称 `from_dict`
  - 位置: src/features/edit/ai_editing/tool_operation_ledger.py:110-125
  - 证据:
    ```python
    if isinstance(result_data, dict):
        diagnostics = tuple(
            Diagnostic.from_dict(item)
            for item in result_data.get("diagnostics", ())
            if isinstance(item, dict)
        )
        result = ToolResult(
            tool_name=str(result_data.get("tool_name", data.get("tool_name", "unknown"))),
            outcome=str(result_data.get("outcome", "failure")),
        ...
    ```
  - 影响: `ToolResult.to_dict`（tool_result.py:124-134）新增字段时此处的手工重建会静默丢字段，恢复的 REPLAY 结果与原结果不一致；且 `data` 默认回退 `"failure"` 的容错语义只存在于这一份拷贝里。
  - 建议: 在 `tool_result.py` 上补 `ToolResult.from_dict`（与 `Diagnostic.from_dict` 对称），ledger 改为调用它。
  - 置信度: 高

### 通过项
- 线程安全：`RLock` 覆盖 begin/finish/mark_unknown/get/records/import_all/clear 全部读写路径，`finish`/`mark_unknown` 以 operation_id 比对防止落到被 trim 后重开的新记录（正确）
- 状态提交一致性：import_all 将遗留 RUNNING 降级为 UNKNOWN 且 UNKNOWN 不可 REPLAY，需显式 reconcile，语义正确
- 输入验证：strategy 白名单校验（ALL_IDEMPOTENCY_STRATEGIES，已核对 capability_catalog.py:47-52）、from_dict 对非 dict/缺 key/坏枚举抛 ValueError 并被 import_all 捕获跳过
- 性能：记录量受 `_max_items`（默认 256）硬上限约束，`_trim_locked` O(n log n) 代价可忽略
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不适用（纯数据结构）
- 类型注解完整（含 `-> "ToolOperationRecord"` 等返回注解）

---

## src/features/edit/ai_editing/tool_result.py (行数: 252)

### 发现

- [AUD-14803] [Suggestion] `to_dict` 的 `artifact_refs` 未过 `sanitize_for_storage`，与模块自述的"审计事件绝不泄漏绝对路径"规则不一致
  - 位置: src/features/edit/ai_editing/tool_result.py:130
  - 证据:
    ```python
    return {
        "tool_name": self.tool_name,
        "outcome": self.outcome,
        "data": sanitize_for_storage(dict(self.data or {})),
        "diagnostics": [d.to_dict() for d in self.diagnostics],
        "artifact_refs": list(self.artifact_refs),
    ```
  - 影响: `data`/`error_message`/diagnostic message 都经 `sanitize_for_storage`（已核对 invocation_models.py:669-699，替换绝对路径与 secret 形状），唯独 `artifact_refs` 原样输出。当前调用方多传 asset_id/scene_id 等不透明 id（如 teaching_animation/module.py:1003），但一旦某工具把文件路径放进 refs，审计侧即泄漏绝对路径，违反模块 docstring 第 10-12 行声明的红线。
  - 建议: `"artifact_refs": [sanitize_for_storage(str(r)) for r in self.artifact_refs]`。
  - 置信度: 中（缺口确定；现有调用是否传路径未全量验证）

- [AUD-14804] [Suggestion] `Diagnostic.from_dict` 未校验 severity 取值，持久化回读可注入任意严重级
  - 位置: src/features/edit/ai_editing/tool_result.py:77-83
  - 证据:
    ```python
    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Diagnostic":
        return Diagnostic(
            severity=str(d.get("severity", "info")),
            code=str(d.get("code", "")),
            message=str(d.get("message", "")),
        )
    ```
  - 影响: severity 仅是字符串约定（"info/warning/error"，tool_result.py:66），从持久化/外部 JSON 重建时不校验。UI 若按 severity 分流（错误置顶/中断），畸形值会落进默认分支或渲染异常字符串；下游 `severity == "error"` 类判断（toposctl.py:293 同款模式）会被非标准值绕过。
  - 建议: from_dict 中将未知 severity 归一为 "error" 或 "info"（显式 fail-safe），并枚举已知集合。
  - 置信度: 高

### 通过项
- 线程安全：frozen dataclass + 默认不可变容器，无共享可变状态
- 输入验证：`__post_init__` 对空 tool_name 抛 ValueError、未知 outcome 归一 FAILURE（fail-safe，tool_result.py:104-110）
- 异常路径：DENIED 与 FAILURE 显式分离，工厂函数语义清晰
- `_permission_value` 对 `ToolPermission(str, Enum)` 成员与裸字符串双兼容（已核对 tool_facade.py:69-77，值为 "read"/"write" 等）
- GPU/GLSL/HDR、信号防递归：不适用
- 类型注解完整

---

## src/features/edit/ai_editing/tool_result_pruner.py (行数: 82)

### 发现

- [AUD-14805] [Suggestion] `render` 用 `default=str` 序列化任意对象，与"确定性输出"承诺相悖
  - 位置: src/features/edit/ai_editing/tool_result_pruner.py:55
  - 证据:
    ```python
    def render(self, payload: Any) -> PrunedToolResultText:
        """Serialize *payload* and prune only the model-facing text."""

        text = json.dumps(payload, ensure_ascii=False, default=str)
        return self.prune_text(text)
    ```
  - 影响: 模块首行自述 "Deterministic, model-facing tool-result pruning"。对未定义稳定 `__repr__`/`__str__` 的对象，`str()` 产出 `<Foo object at 0x...>`，同一 payload 两次渲染文本不同：模型上下文里出现内存地址（脏上下文），且基于该文本的任何缓存/对比失效。
  - 建议: `default=` 回调里对不可序列化类型返回稳定标记（类名或字段摘要），或在源头要求 JSON-safe payload。
  - 置信度: 低（机制确定；取决于上层是否真的传非 JSON 对象）

### 通过项
- 边界处理：`tail_chars == 0` 时显式回退空串，避开 `value[-0:]` 返回全串的经典陷阱（tool_result_pruner.py:67）
- 输入验证：`__post_init__` 校验 threshold>0、head/tail≥0、head+marker+tail≤threshold（默认 4096+41+1024 ≤ 8192 成立）
- 线程安全：无状态对象；性能 O(n) 单次切片
- GPU/GLSL/HDR、信号防递归：不适用
- 类型注解完整

---

## src/features/edit/ai_editing/toposctl.py (行数: 455)

### 发现

- [AUD-14806] [Major] 数值型选项 `int()` 抛出的 ValueError 不在任何 except 列表内，CLI 以 traceback 崩溃而非 exit 2
  - 位置: src/features/edit/ai_editing/toposctl.py:321（另 337、373）
  - 证据:
    ```python
    elif a == "--asset-limit" and i + 1 < len(args):
        out["asset_limit"] = int(args[i + 1]); i += 2
    ...
    elif a == "--limit" and i + 1 < len(args):
        out["limit"] = int(args[i + 1]); i += 2
    ```
    而 run_cli 只捕获（toposctl.py:187-189）:
    ```python
    except _CliUsageError as exc:
        ...
    except (OSError, ConnectionError, TimeoutError, json.JSONDecodeError):
    ```
  - 影响: `toposctl snapshot --asset-limit abc` / `search-assets --limit x` / `help --limit x` 触发未捕获 ValueError，`_parse_*` 在 run_cli 的 try 内调用，直接穿透到调用方；console 入口 `main()`（toposctl.py:437）无兜底，进程打印 Python traceback、以解释器默认码退出。违反本模块自订契约 "2 = CLI 用法错误"（CliResult docstring，toposctl.py:66），脚本/CI 按 exit_code 分流逻辑失效。
  - 建议: 在三个解析器内包一层 `try: int(...) except ValueError: raise _CliUsageError(...)`。
  - 置信度: 高

- [AUD-14807] [Major] `main()` 不传 `call_log`，审计写入随进程退出即丢弃的临时内存日志
  - 位置: src/features/edit/ai_editing/toposctl.py:437（配合 130）
  - 证据:
    ```python
    result = run_cli(filtered, invoke_fn)
    ```
    run_cli 内部（toposctl.py:130）:
    ```python
    log = call_log or ToolCallLog()
    ```
  - 影响: `ToolCallLog` 纯内存 append（已核对 tool_call_log.py:165-199，无持久化）。模块红线自述 "审计（§4.2 step 1）：每次调用经 ToolCallLog 记录（client="cli"）"（toposctl.py:19），而 console 入口（pyproject.toml:85 注册的 `toposctl = ...:main`）产生的审计记录在进程退出后全部丢失，§12.1 安全矩阵要求的"日志是越权/伪造 grant/重放的取证依据"在 CLI 侧不可用（仅当 host 恰好也记录时才有补救）。
  - 建议: main() 构造持久化 sink（文件/审计存储）传入 call_log，或至少将 `log.to_dict()` 随 JSON 输出/落盘。
  - 置信度: 中（代码事实确定；host 侧可能另有审计，故降为中）

- [AUD-14808] [Suggestion] 离线 capabilities 的审计写入失败被 `except Exception: pass` 完全静默
  - 位置: src/features/edit/ai_editing/toposctl.py:249-255
  - 证据:
    ```python
    try:
        log.record(
            tool_name="topos.get_capabilities", args={},
            result=result, client="cli",
        )
    except Exception:  # noqa: BLE001
        pass
    ```
  - 影响: 审计是本模块红线；静默吞掉意味着"看似已审计实则没记"，且无任何日志可查，违反"不得宽泛 except 掩盖领域错误"精神。
  - 建议: 至少 `logging.getLogger("toposctl").warning(...)` 落 stderr。
  - 置信度: 高

- [AUD-14809] [Suggestion] `--log-level` 非法值使 `setLevel` 抛 ValueError，`main()` 无捕获直接崩溃
  - 位置: src/features/edit/ai_editing/toposctl.py:423-426
  - 证据:
    ```python
    if a == "--log-level" and i + 1 < len(args):
        import logging
        logging.getLogger("toposctl").setLevel(args[i + 1].upper())
        i += 2; continue
    ```
  - 影响: `toposctl --log-level bogus version` → `ValueError: Unknown level: 'BOGUS'` traceback。与 AUD-14806 同类：main/run_cli 的结构化错误契约未覆盖。
  - 建议: 解析时校验合法级别集合（`logging.getLevelNamesMapping()` 或 try/except 归一为用法错误）。
  - 置信度: 高

- [AUD-14810] [Suggestion] 手写参数解析静默吞掉未知 flag 与悬空选项值，拼写错误无反馈
  - 位置: src/features/edit/ai_editing/toposctl.py:311-324（snapshot；search/help 同构 327-340、365-376）
  - 证据:
    ```python
    while i < len(args):
        a = args[i]
        if a == "--detail" and i + 1 < len(args):
            out["detail_level"] = args[i + 1]; i += 2
        elif a == "--timeline" and i + 1 < len(args):
            out["timeline_id"] = args[i + 1]; i += 2
        ...
        else:
            i += 1
    ```
  - 影响: `--detaill full`（拼错）或 `snapshot --detail`（末尾缺值）被 else 分支静默跳过：用户以为设了 detail_level 实际用默认值；`preview PLAN extra` 的多余位置参数同样被忽略。另有 `_parse_plan` 不校验 JSON 顶层必须是 object（`json.loads("[1,2]")` 得 `{"plan": [1, 2]}` 直传工具，全靠下游 ToolFacade 兜底）。
  - 建议: else 分支遇 `a.startswith("-")` 或已消耗 `--flag` 缺值时 raise `_CliUsageError`；`_parse_plan` 校验 `isinstance(plan, dict)`。
  - 置信度: 高

### 通过项
- 授权红线：apply-plan 仅在宿主注入 approval_grant 时升级，CLI 不自带 grant（与 ToolFacade 红线一致）
- 异常路径：IPC/连接类 OSError/ConnectionError/TimeoutError 归一为 NOT_CONNECTED 结构化诊断且不泄漏 socket 路径（toposctl.py:189-196）
- 未知命令 exit 2 优先于"未连接"判断（toposctl.py:145-149）；`--help`/`-h` 正常
- 诊断一致性：`_run` 以 `severity == "error"` 判 exit_code，与 DiagnosticSeverity 值一致（已核对 diagnostics.py:22-27）
- 线程安全/GPU/GLSL/信号防递归：不适用（无状态函数 + 每次调用独立对象）
- 类型注解完整

---

## src/features/edit/ai_editing/transaction_service.py (行数: 601)

### 发现

- [AUD-14811] [Major] `commit` 文档承诺的"直接传 ApprovalService 自动构造 scope 并 consume"路径，实现恒返回 None
  - 位置: src/features/edit/ai_editing/transaction_service.py:536-537（与 docstring 434-438 矛盾）
  - 证据:
    docstring（transaction_service.py:434-436）:
    ```python
    - 一个 ``ApprovalService``（此时内部会用 plan 的 canonical JSON +
      project/timeline/base_revision 构造 expected scope 并 ``consume``），
      或
    ```
    实现（transaction_service.py:536-537）:
    ```python
    if isinstance(approval, ApprovalService):
        return None  # ApprovalService 本身不是 grant；需要 grant_id
    ```
  - 影响: 按文档契约 `commit(plan, state, approval=service)` 的调用方永远拿到 "commit requires a valid ApprovalGrant..." 的拒绝（transaction_service.py:462-471），无任何诊断区分"路径未实现"。集成者按 docstring 接线必然全量提交失败，且易误判为授权服务故障。
  - 建议: 二选一——实现该路径（service 侧提供按 expected_scope 检索/签发 grant 的 consume 变体），或删改 docstring 明确"approval 只接受 ApprovalGrant/grant_id 字符串"。
  - 置信度: 高

- [AUD-14812] [Major] simulate 阶段 `except Exception` 一律标 SCHEMA_INVALID，宽泛捕获掩盖领域错误并给出错误诊断码
  - 位置: src/features/edit/ai_editing/transaction_service.py:390-401
  - 证据:
    ```python
    except Exception as exc:
        ...
        err_diag = make_diagnostic(
            DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
            detail=f"simulation failed: {exc}",
        )
    ```
  - 影响: 走到 simulate 前 plan 已通过 schema+domain 校验（transaction_service.py:356-358），此处异常来自 MacroCommand.apply 的运行期缺陷（AttributeError/KeyError 等 bug 或数据问题），却被标为"schema invalid"。违反铁律"不得宽泛 except 掩盖领域错误"：内部 bug 被伪装成用户可修复的 plan 问题，模型/用户反复改 plan 无果，真实堆栈只剩 `f"simulation failed: {exc}"` 一个字符串。
  - 建议: 改用 INTERNAL_ERROR（或新增 SIMULATION_FAILED）诊断码，并在 detail 中保留异常类型；至少对 `CompileError`/领域异常与裸 `Exception` 分流。
  - 置信度: 高

- [AUD-14813] [Major] `compute_diff` 单标签分类：跨轨移动+时长变化只报 "trimmed"，且 before/after 载荷不含 track，批准 UI 看不到轨道变更
  - 位置: src/features/edit/ai_editing/transaction_service.py:213-221（载荷 186-199）
  - 证据:
    ```python
    if old.track_id != clip.track_id or old.timeline_start != clip.timeline_start:
        if kind == "trim_clip" or (old.timeline_start == clip.timeline_start and old.timeline_duration != clip.timeline_duration):
            trimmed.append(ClipDiff(change="trimmed", clip_id=cid, track_id=clip.track_id, before=before, after=after))
        else:
            moved.append(...)
    ```
    而 `_range_payload`（transaction_service.py:186-199）只含 timeline/source/text，无 track 字段。
  - 影响: 同一 clip 既被移到另一轨又改时长（多 op plan 中 move+trim，`op_kinds_by_clip` 只保留最后一个 op 的 kind，transaction_service.py:172-178）时被归为 "trimmed"：轨道变化既不在 change 标签里，也不在 before/after 载荷里（仅 track_id 显示新轨）。用户批准的 diff 缺失一项实际变更，违背"预览即提交"不变量的展示侧（§1.4）。`total_changes` 同步少计。
  - 建议: 变更类型允许组合（如 `changes: [...]`）或在 `_range_payload` 中加入 track_id 并对轨道变化强制归为 moved（或双记）。
  - 置信度: 中（分支逻辑确定；依赖多 op 作用同一 clip 的场景是否常见）

- [AUD-14814] [Suggestion] `compute_diff` 在 simulate 的 try 块之外调用，其异常以裸异常逃逸 `preview`
  - 位置: src/features/edit/ai_editing/transaction_service.py:409
  - 证据:
    ```python
            return PreviewResult(report=failed_report, compiled=compiled, diff=None)

        diff = compute_diff(sequence, proposed, compiled)
        return PreviewResult(
    ```
  - 影响: simulate 失败被结构化为 ValidationReport（fail-safe），但紧随其后的 `compute_diff` 里 `int(clip.timeline_start)`/`clip.track_id` 等属性访问一旦失败（自定义 clip 缺属性），异常直接抛给 preview 调用方——同一函数内两条失败路径行为不一致，UI 侧若只按 PreviewResult 处理会崩。
  - 建议: 将 compute_diff 纳入同一 try（或独立 try 归一为 INTERNAL_ERROR 诊断）。
  - 置信度: 高（结构事实确定）/影响低

- [AUD-14815] [Suggestion] `TimelineDiff/ClipDiff.from_dict` 未校验持久化数据形状，畸形值可被拆成字符元组或抛 AttributeError
  - 位置: src/features/edit/ai_editing/transaction_service.py:138-149
  - 证据:
    ```python
    def _clips(key: str) -> tuple[ClipDiff, ...]:
        return tuple(ClipDiff.from_dict(c) for c in data.get(key, []))
    ...
    affected_track_ids=tuple(data.get("affected_track_ids", [])),
    warnings=tuple(data.get("warnings", [])),
    ```
  - 影响: 若持久化 JSON 中 `affected_track_ids` 被写成字符串（外部可编辑的工程文件），`tuple("ab")` 得 `('a','b')` 静默污染数据；若 `added` 是 dict，迭代出 key（字符串）传入 `ClipDiff.from_dict` 后 `data.get` 抛 AttributeError，工程重开时的 diff 回看直接失败。铁律"外部数据须验证"未覆盖。
  - 建议: from_dict 各字段先 `isinstance(x, list)` 校验，非法时抛带字段的 ValueError 或返回空并记 warning。
  - 置信度: 高（代码事实）/触发依赖损坏数据

- [AUD-14816] [Suggestion] `_record_audit` 的 sink 异常被 `except Exception: pass` 静默，且函数内 `DiagnosticSeverity` 为死导入
  - 位置: src/features/edit/ai_editing/transaction_service.py:554、572-576
  - 证据:
    ```python
    from src.features.edit.ai_editing.diagnostics import DiagnosticSeverity
    ...
    try:
        self._audit_sink(record)  # type: ignore[misc]
    except Exception:
        # 审计失败不影响提交结果
        pass
    ```
  - 影响: "审计失败不影响提交"方向合理（避免审计故障回滚提交），但完全静默使审计链路断连不可观测；`DiagnosticSeverity` 导入后从未使用（diag_summary 手工构造），徒增导入开销与误导。
  - 建议: 捕获后以 logging 记 warning（含 plan_id 与异常类型）；删除死导入。
  - 置信度: 高

- [AUD-14817] [Suggestion] grant 在乐观锁检查之前被 consume，revision 冲突时一次性授权被白白烧掉
  - 位置: src/features/edit/ai_editing/transaction_service.py:458-486
  - 证据:
    ```python
    grant = self._authorize(
        plan, state, approval, approval_service, diff_hash=preview.diff_hash,
    )
    if grant is None:
        ...
    try:
        result = EditProjectService.commit_command(
            state, plan.target.timeline_id, preview.compiled.macro, ...
    except RevisionConflict:
    ```
  - 影响: `_authorize` 内 `consume` 会把 single_use grant 标 used（已核对 approval_service.py:264-265）。随后 `commit_command` 抛 RevisionConflict 时提交未发生，但 grant 已作废：用户刷新快照、重生成 plan 后必须重新走一遍批准流程。多操作批量提交遇到一次冲突即连环烧 grant。commit_command 抛出其它异常（未捕获）同样如此。
  - 建议: 将授权移到可预先失败的检查之后不可行（乐观锁本在 commit 内），但可在 RevisionConflict 分支向调用方返回明确"grant 已消耗、需重新批准"的错误文案，或 ApprovalService 提供 conflict 场景的补偿性 reissue。
  - 置信度: 中

### 通过项
- 状态提交一致性：preview 与 commit 复用同一 compiler 与 `commit_command`（内部乐观锁+bump+sync，已核对 edit_project_service.py:81-96/156），任一步失败不产生半套状态；RevisionConflict 不做 last-write-wins，返回结构化 STALE 语义
- 授权红线：`_authorize` 对"ApprovalGrant 无 service""裸字符串无 service"一律拒绝（transaction_service.py:522-545），单次 consume 防重放语义与 approval_service 实现一致；`_expected_commit_scope` 双绑定 plan_hash+diff_hash 与 ApprovalScope 字段匹配（ApprovalAction 为 Literal["commit_plan",...]，已核对 approval_service.py:44/71-77）
- 持久化边界：`PreviewResult.to_dict` 明确剔除 compiled/proposed_sequence 活对象并在 from_dict 注释"不可直接用于 commit"，恢复后必须重新 preview（防 stale 提交）
- 审计内容：AuditRecord 不含 secret/对话/绝对路径（仅计数与 id）
- 线程安全：TransactionService 无共享可变状态（仅 `_audit_sink` 只读引用）
- GPU/GLSL/HDR、信号防递归：不适用
- 类型注解：整体完整；唯一缺口 `compute_diff(base, proposed, ...)` 的 base/proposed 无注解（transaction_service.py:152，Suggestion 级，并入本条）

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 6 | AUD-14800, AUD-14806, AUD-14807, AUD-14811, AUD-14812, AUD-14813 |
| Suggestion | 12 | AUD-14801, AUD-14802, AUD-14803, AUD-14804, AUD-14805, AUD-14808, AUD-14809, AUD-14810, AUD-14814, AUD-14815, AUD-14816, AUD-14817 |
