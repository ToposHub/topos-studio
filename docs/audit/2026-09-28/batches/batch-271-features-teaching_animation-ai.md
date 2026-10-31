# batch-271 审查报告（src/features/teaching_animation/ai/）

批次：batch-271 [P2] | 编号区间：AUD-27100–AUD-27199 | 文件数：4 | 总行数：907
说明：session_store.py / slo_benchmark.py / test_matrix.py 顶部带 DORMANT(2026-08-19) 标记；经全库 grep 确认 `MotionGraphicSessionStore`、`summarize_samples`、`compute_coverage` 目前仅被 ai/__init__.py re-export 与 tests/ 引用，无 src/ 生产调用方；`TeachingAssistantSessionRegistry` 同样仅测试引用。以下发现按代码现状评定，接线后风险将落地。

## src/features/teaching_animation/ai/session.py (行数: 54)

### 发现
- [AUD-27100] [Major] 全局会话注册表无锁，get_or_create 存在 check-then-act 竞态
  - 位置: src/features/teaching_animation/ai/session.py:30-37, 46-50
  - 证据:
    ```python
    def get_or_create(self, project_id: str, *, context_provider: ... = None) -> TeachingAssistantSession:
        session = self._sessions.get(project_id)
        if session is None:
            session = TeachingAssistantSession(project_id, context_provider)
            self._sessions[project_id] = session
    ...
    _GLOBAL_SESSION_REGISTRY = TeachingAssistantSessionRegistry()

    def get_shared_teaching_assistant_session(project_id: str, ...) -> TeachingAssistantSession:
        return _GLOBAL_SESSION_REGISTRY.get_or_create(project_id, context_provider=context_provider)
    ```
  - 影响: 模块定位是"跨宿主 surface 共享每工程一个会话"（docstring 第 1 行），编辑/composite 等不同 surface 可能在 UI 线程与后台线程各自调用。两个线程同时判定 `session is None` 会各自创建会话，后写者覆盖前者，先创建方 attach 的 surface 集合丢失；`attached_surfaces.add/discard`（19-23 行）也是无锁跨线程集合修改。会话身份分裂还会导致 context_provider 状态不一致。
  - 建议: 为 `_sessions` 加 `threading.Lock`（或至少文档化"仅 UI 线程调用"）；`attach/detach` 同步保护。
  - 置信度: 中（竞态窗口真实存在；当前生产代码尚无调用方，属接线后暴露的风险）

- [AUD-27101] [Suggestion] get_or_create 静默覆盖已有会话的 context_provider
  - 位置: src/features/teaching_animation/ai/session.py:35-36
  - 证据:
    ```python
    elif context_provider is not None:
        session.context_provider = context_provider
    ```
  - 影响: 任何后续调用传入新 provider 即静默替换既有会话的 provider，已 attach surface 持有的旧引用与新引用指向不同 provider，行为不可预期且无日志。
  - 建议: 覆盖时记录日志，或仅在原值为 None 时填充、显式提供 replace 接口。
  - 置信度: 高

- [AUD-27102] [Suggestion] close_project 不通知已 attach 的 surface
  - 位置: src/features/teaching_animation/ai/session.py:42-43
  - 证据:
    ```python
    def close_project(self, project_id: str) -> None:
        self._sessions.pop(project_id, None)
    ```
  - 影响: 被移除会话仍被各 surface 持有（attached_surfaces 非空），可继续读写已关闭会话及其 context_provider，形成悬空生命周期。
  - 建议: 关闭前对 session 的 attached_surfaces 广播 detach/关闭回调，或将 session 标记 closed 使后续访问快速失败。
  - 置信度: 中

### 通过项
- GPU 资源生命周期：无 GPU/ModernGL 资源（不涉及）
- GLSL/HDR 正确性：不涉及
- 信号防递归：无 Qt 信号，不涉及
- 异常与取消路径：纯字典/集合操作，无吞异常路径
- 类型注解：完整（含返回类型）

## src/features/teaching_animation/ai/session_store.py (行数: 382)

### 发现
- [AUD-27103] [Major] upsert 更新分支：status 缺省回落 "draft" 静默重置状态，且具名参数被忽略
  - 位置: src/features/teaching_animation/ai/session_store.py:244-258（重点 257 行）
  - 证据:
    ```python
    timeline_id=updates.get("timeline_id", existing.timeline_id),
    brief_id=updates.get("brief_id", existing.brief_id),
    brief_revision=updates.get("brief_revision", existing.brief_revision),
    ...
    status=updates.get("status", status),
    note=updates.get("note", existing.note),
    ```
  - 影响: 更新已有记录时（else 分支），若调用方未显式传 `status`（签名默认 `"draft"`）也未放入 `updates`，记录状态从 `waiting_approval`/`applied` 被静默重置为 `draft`——与上一行 `note` 回落 `existing.note` 的行为不一致，属状态丢失。同时按签名传入的 `timeline_id`/`brief_id`/`brief_revision` 具名参数在更新分支被完全忽略（只有 `**updates` 生效），按直觉传参的调用方得到无声 no-op（如传入新 brief_revision 却被丢弃，可能触发审批 scope 误判 stale）。
  - 建议: 更新分支 status 回落改为 `existing.status`；统一参数与 `updates` 的优先级（如参数视同 updates 的一部分），或至少在 docstring 明示"具名参数仅创建时生效"。
  - 置信度: 高

- [AUD-27104] [Major] 磁盘写失败时内存已提交，异常路径造成内存/磁盘状态分叉且无回滚
  - 位置: src/features/teaching_animation/ai/session_store.py:260-261, 264-269, 354-363
  - 证据:
    ```python
    self._requests[record.request_id] = record
    self._write_locked()
    return record
    ...
    def discard(self, request_id: str) -> bool:
        with self._lock:
            existed = self._requests.pop(request_id, None) is not None
            if existed:
                self._write_locked()
    ...
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)
    ```
  - 影响: `_write_locked` 中 open/json.dump/os.replace 抛 OSError（磁盘满、目录只读、target_dir 被删）时，内存已经完成 pop/替换，异常却上抛——调用方看到异常以为操作失败，实际内存态已变更且与磁盘分叉；此后 `reload()` 会把旧记录从磁盘读回，出现"已删除的请求复活"或状态回跳。
  - 建议: 先写盘成功再提交内存（write-ahead），或在 `_write_locked` 失败时回滚本次内存变更。
  - 置信度: 高

- [AUD-27105] [Major] reload 对外部 JSON 无防御：scope 未知键/缺字段、数值转换异常直接崩溃
  - 位置: src/features/teaching_animation/ai/session_store.py:96-97, 103, 112-113, 341
  - 证据:
    ```python
    scope_data = data.get("grant_scope")
    scope = MotionGraphicApprovalScope(**scope_data) if isinstance(scope_data, Mapping) else None
    ...
    brief_revision=int(data.get("brief_revision", 1)),
    ...
    created_at=float(data.get("created_at", 0.0) or 0.0),
    ...
    snapshot = MotionGraphicSessionSnapshot.from_dict(data)   # reload() 内，无 try 包裹
    ```
  - 影响: `.topos` 下的 JSON 属外部数据（手改、新版本 schema、半损坏）。`MotionGraphicApprovalScope(**scope_data)` 遇未知键抛 TypeError、缺 project_id/brief_id/brief_hash 抛 ValueError（approval_scope.py `__post_init__` 校验）；`int()/float()` 遇非数值字符串抛 ValueError。`_read_locked` 只捕获 OSError/JSONDecodeError，`from_dict` 的异常在 `reload()`（341 行）不被捕获，直接炸到宿主。模块 docstring（12-14 行）承诺 "unknown-payload passthrough (so future fields do not break older builds)"，grant_scope 恰恰违背该承诺。违反"外部数据须验证"铁律。
  - 建议: scope 构造前过滤/校验键并转换为枚举；`reload` 包裹 (TypeError, ValueError) 降级为丢弃该条记录或整体拒绝加载，与 docstring 承诺对齐。
  - 置信度: 高

- [AUD-27106] [Major] grant_scope 反序列化不还原类型：allowed_actions 变纯 str、asset_ids 变 list
  - 位置: src/features/teaching_animation/ai/session_store.py:96-97（写侧对照 approval_scope.py:182-183，受害对照 approval_scope.py:165）
  - 证据:
    ```python
    scope = MotionGraphicApprovalScope(**scope_data) if isinstance(scope_data, Mapping) else None
    ```
    （scope 声明为 `asset_ids: tuple[str, ...]`、`allowed_actions: tuple[MotionGraphicAction, ...]`；写侧 to_dict 输出 `"asset_ids": list(...)`、`"allowed_actions": [action.value ...]`）
  - 影响: 保存/重载 round-trip 后 asset_ids 是 list、allowed_actions 是纯字符串而非 `MotionGraphicAction` 枚举。因该枚举是 `str, Enum`，`matches()` 的集合比较侥幸可用，但 `diff_from()`（approval_scope.py:165 `action.value for action in extra`）对纯 str 元素抛 AttributeError；frozen dataclass 的 `==`（list != tuple）也永不相等，任何依赖等值比较的恢复后校验失效。
  - 建议: `from_dict` 时显式规范化：`tuple(asset_ids)`、`tuple(MotionGraphicAction(v) for v in allowed_actions)`，或为 scope 提供 from_dict 类方法统一处理。
  - 置信度: 高（类型破坏确定；下游 AttributeError 取决于恢复后的 scope 是否进入 diff_from，为中等）

- [AUD-27107] [Suggestion] upsert 不校验 project_id 归属与 status 取值
  - 位置: src/features/teaching_animation/ai/session_store.py:201-212, 70, 114
  - 证据:
    ```python
    def upsert_request(self, *, request_id: str | None = None, project_id: str, ...,
                       status: str = "draft", note: str = "", **updates: Any) -> ...:
    ...
    status: str = "draft"          # draft / waiting_approval / applied / cancelled
    ```
  - 影响: 外部工程的 project_id 可写入当前工程文件（restore/reload 只校验快照级 project_id，不校验单条）；status 注释约定四值但无白名单校验，任意字符串可持久化并在恢复后流入宿主状态机。
  - 建议: upsert 校验 `project_id == self._project_id`（不一致抛 MotionGraphicFailure）；status 用 frozenset 白名单。
  - 置信度: 高

- [AUD-27108] [Suggestion] 临时文件固定名、无 fsync、写失败残留
  - 位置: src/features/teaching_animation/ai/session_store.py:360-363
  - 证据:
    ```python
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)
    ```
  - 影响: 单实例锁内无并发，但若两个 store 实例指向同一 target_dir（FILENAME 固定）或写中途异常，`.tmp` 残留/交叉写；无 fsync，崩溃后 replace 的文件内容可能截断。
  - 建议: tmp 名加 pid/uuid；`handle.flush()+os.fsync()` 后再 replace。
  - 置信度: 中

- [AUD-27109] [Suggestion] time_fn 类型注解为 Any
  - 位置: src/features/teaching_animation/ai/session_store.py:174
  - 证据: `time_fn: Any = None,`
  - 影响: 注入点失去类型约束，传错签名（如传 `time.time()` 的结果 float）要到运行时才发现。
  - 建议: `Callable[[], float] | None`。
  - 置信度: 高

### 通过项
- 线程安全：RLock 使用正确，全部查询/变更/持久化均在锁内（invalidate_stale_grants 遍历用了 list() 快照）
- GPU 资源生命周期：不涉及
- GLSL/HDR 正确性：不涉及
- 信号防递归：无 Qt 信号
- revision 冲突处理：restore 的跨工程快照正确抛 MOTION_GRAPHIC_REVISION_CONFLICT 并带 details
- 性能模式：每变更全量写一个小 JSON，规模可控

## src/features/teaching_animation/ai/slo_benchmark.py (行数: 213)

### 发现
- [AUD-27110] [Major] 空报告 all_meet_targets 恒为 True（空真），CI 门可假绿
  - 位置: src/features/teaching_animation/ai/slo_benchmark.py:122-124, 162-164
  - 证据:
    ```python
    @property
    def all_meet_targets(self) -> bool:
        return all(s.meets_p50 and s.meets_p95 for s in self.summaries)
    ...
    values = sorted(bucket[slo_id])
    if not values:
        continue
    ```
  - 影响: 采样失败、样本 slo_id 全部拼错（见 AUD-27111）或空输入时，summaries 为空元组，`all()` 空真返回 True——基准"全达标"。这正是模块 docstring 要防的 "degradation slips in unnoticed"；一旦接入 CI 门禁，采集链路故障会被判绿。
  - 建议: summaries 为空时返回 False，或在 BenchmarkReport 中显式暴露 total_sample_count 供门禁校验。
  - 置信度: 中（DORMANT 未接线，当前影响限于测试与未来 CI；逻辑缺陷本身确定）

- [AUD-27111] [Suggestion] 未登记 slo_id 的样本被静默丢弃
  - 位置: src/features/teaching_animation/ai/slo_benchmark.py:156-159
  - 证据:
    ```python
    bucket: dict[str, list[float]] = {slo_id: [] for slo_id in slo_table}
    for sample in samples:
        if sample.slo_id in bucket:
            bucket[slo_id].append(sample.value)
    ```
  - 影响: 拼错 id 的样本无声消失、无任何提示，与 AUD-27110 叠加放大假绿风险。
  - 建议: 未登记 slo_id 记入 report.notes（或抛错），保留可观测性。
  - 置信度: 高

- [AUD-27112] [Suggestion] 退化检测仅基于 p50，忽略 p95
  - 位置: src/features/teaching_animation/ai/slo_benchmark.py:169-175
  - 证据:
    ```python
    if baseline and slo_id in baseline:
        baseline_value = baseline[slo_id]
        if slo.direction == "lower":
            degraded = p50 > baseline_value * (1.0 + slo.degradation_threshold)
    ```
  - 影响: 尾部（p95）劣化而中位数稳定时不报 degraded，SLO 表中专门定义的 p95_target 在退化检测里形同虚设。
  - 建议: p50 或 p95 任一超阈值即判 degraded。
  - 置信度: 中（可能是有意的保守设计，建议确认意图）

- [AUD-27113] [Suggestion] 未使用导入 time、field
  - 位置: src/features/teaching_animation/ai/slo_benchmark.py:17-19
  - 证据:
    ```python
    from dataclasses import dataclass, field
    import statistics
    import time
    ```
    （全文件无 `field(` 与 `time.` 使用）
  - 影响: 死导入，增加阅读噪声。
  - 建议: 删除两个未用导入。
  - 置信度: 高

### 通过项
- 线程安全：纯函数/不可变 dataclass，无共享可变状态
- GPU 资源生命周期：不涉及
- GLSL/HDR 正确性：不涉及
- 异常与取消路径：无吞异常路径
- 类型注解：完整
- _percentile 边界处理正确（空列表、单元素）

## src/features/teaching_animation/ai/test_matrix.py (行数: 258)

### 发现
- [AUD-27114] [Suggestion] compute_coverage 的 any() 语义与 required_layers 契约/模块文档矛盾
  - 位置: src/features/teaching_animation/ai/test_matrix.py:218-220（对照 7-10、62-64 行）
  - 证据:
    ```python
    layers_present = collected_by_capability.get(requirement.capability_id, ())
    if layers_present and any(layer in layers_present for layer in requirement.required_layers):
        covered.append(requirement.capability_id)
    ```
    模块 docstring： "every P0/P1 capability must have focused tests at each layer that matters"；字段名 `required_layers`。
  - 影响: 只要任意一个必需层有测试即计 covered。例如 MG-20 要求 unit+integration+fault_injection，仅 unit 一层也算覆盖，MG-25 验收口径被放宽（tests/ai/test_motion_graphic_b4_release.py:138-153 断言的正是该宽语义——代码与测试一致，但与文档及字段命名矛盾，后续维护者会按 all 语义误信覆盖）。
  - 建议: 二选一消除歧义：改为 `all(...)`（严格），或将字段更名 any_layer_of 并同步 docstring。
  - 置信度: 高（矛盾本身确定；哪一侧是本意不确定）

- [AUD-27115] [Suggestion] Any 在文件底部导入并依赖 type: ignore[name-defined] 补丁
  - 位置: src/features/teaching_animation/ai/test_matrix.py:189, 241
  - 证据:
    ```python
    def to_dict(self) -> dict[str, Any]:  # type: ignore[name-defined]
    ...
    from typing import Any  # noqa: E402
    ```
  - 影响: 仅靠 `from __future__ import annotations` 的惰性求值才不触发 NameError；`type: ignore` 掩盖问题、文件中部导入损害可读性。
  - 建议: 将 Any 并入顶部 `from typing import Iterable, Mapping, Any`，删除 ignore 与底部导入。
  - 置信度: 高

### 通过项
- 线程安全：纯数据目录，无可变共享状态
- GPU 资源生命周期：不涉及
- GLSL/HDR 正确性：不涉及
- 输入验证：TestRequirement.__post_init__ 对未知 layer 名抛 ValueError，有效
- 异常路径：compute_coverage 对缺映射能力正确归入 missing；real/mock 证据区分逻辑正确
- 类型注解：完整（仅 Any 导入位置问题，见 AUD-27115）

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 6 | AUD-27100, AUD-27103, AUD-27104, AUD-27105, AUD-27106, AUD-27110 |
| Suggestion | 10 | AUD-27101, AUD-27102, AUD-27107, AUD-27108, AUD-27109, AUD-27111, AUD-27112, AUD-27113, AUD-27114, AUD-27115 |
