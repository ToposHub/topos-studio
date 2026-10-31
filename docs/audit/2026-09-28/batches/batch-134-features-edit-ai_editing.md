# batch-134 审查报告 — src/features/edit/ai_editing/

编号区间：AUD-13400–AUD-13499（实际使用 AUD-13400–AUD-13414）
审查范围：8 个文件，共 2,880 行，全部通读；跨模块行为（transaction_service / tool_facade / plugin_runtime / export_service / plugin_broker）已只读核验。
维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。
本批次为纯逻辑/契约层（无 Qt、无 OpenGL、无 GLSL、无信号连接），GPU/GLSL/信号维度仅做适用性确认。

汇总：Critical 0 · Major 4 · Suggestion 11。

---

## src/features/edit/ai_editing/diagnostics.py (行数: 278)

### 发现

- [AUD-13400] [Major] `Diagnostic.from_dict` 将未知 severity/code 静默降级为 INFO/INTERNAL_ERROR，与 docstring 承诺的"回落到原始字符串"不符
  - 位置: src/features/edit/ai_editing/diagnostics.py:129-138
  - 证据:
    ```python
    try:
        severity = DiagnosticSeverity(sev_raw)
    except ValueError:
        severity = DiagnosticSeverity.INFO  # type: ignore[assignment]
    try:
        code = TaepDiagnosticCode(code_raw)
    except ValueError:
        code = TaepDiagnosticCode.INTERNAL_ERROR  # type: ignore[assignment]
    ```
    而 docstring（124–127 行）写明：``未知值回落到原始字符串（``DiagnosticSeverity``/``TaepDiagnosticCode`` 均为 ``str`` 枚举，兼容）``。
  - 影响: from_dict 的唯一用途是工程重开后回看历史诊断。遇到未知值（新版本枚举、损坏/大小写不一致数据）时，原本可能是 `error` 的诊断被静默替换为 `INFO`，`is_error` 变 False，原始值完全丢失（也不进 details）；若重载的诊断参与提交/导出门控，阻塞条件被解除。同时文档与实现直接矛盾。
  - 建议: 按 docstring 落到原始字符串（两个枚举均为 str 枚举，运行时兼容），或至少把原始值写入 `details` 并将未知 severity 按失败安全方向取 ERROR。
  - 置信度: 高（docstring 与实现的不一致逐行可证；实际触发需未知枚举值，概率中）

### 通过项
- 线程安全：纯不可变 dataclass/枚举/常量表，无共享可变状态。
- GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归：不适用（无 GPU、无浮点渲染数据、无信号）。
- `format_message` 对模板缺参安全降级（KeyError/IndexError 返回未填模板）。
- `make_diagnostic` 对 details 做防御性拷贝。
- 类型注解完整。

---

## src/features/edit/ai_editing/edit_api.py (行数: 412)

### 发现

- [AUD-13401] [Major] `_host_approval_service` 用宽泛 `except Exception` 吞掉宿主异常，把宿主缺陷伪装成授权失败
  - 位置: src/features/edit/ai_editing/edit_api.py:335-343
  - 证据:
    ```python
    def _host_approval_service(self) -> "ApprovalService | None":
        """安全取宿主的 ApprovalService（协议方法可选）。"""
        fn = getattr(self._host, "approval_service", None)
        if fn is None:
            return None
        try:
            return fn()
        except Exception:
            return None
    ```
  - 影响: 违反 .clinerules"不得宽泛 except 掩盖领域错误"铁律。宿主 `approval_service()` 访问器抛出的任何真实错误（初始化 bug、状态损坏）被静默转为 None；随后 `apply()` 走 `TransactionService.commit(approval_service=None)`，`_authorize` 拒绝 grant，用户看到的是误导性的 "commit requires a valid ApprovalGrant (TAEP §5.6)" 而非真实故障。方向上是 fail-closed（不会造成越权），但排查成本高且授权通道可能被静默禁用。
  - 建议: 只捕获预期的属性/初始化异常并记录/上抛为稳定诊断码（如 TAEP_INTERNAL_ERROR），或至少把异常文本带入返回的失败信息。
  - 置信度: 高

- [AUD-13402] [Major] TAEP §8"输出不允许静默覆盖"未强制执行：`PathGrant.allow_overwrite` 全库无消费者
  - 位置: src/features/edit/ai_editing/edit_api.py:361-378（request_export）、125-137（PathGrant 定义）
  - 证据:
    ```python
    # 若 request 没有显式 output_path，但给了 path_grant，则用 grant 的 path。
    if path_grant is not None and not getattr(request, "output_path", None):
        from dataclasses import replace as _replace

        request = _replace(request, output_path=path_grant.path)
    ```
    模块 docstring 第 11 行承诺 ``输出不允许静默覆盖``。全库 grep 核验：`allow_overwrite` 仅在 PathGrant 定义、`request_path_grant` 形参透传和测试中出现，src/ 下无任何消费者；`export_service.py` 中无 exists/overwrite 检查（grep exists|overwrite 无命中）。
  - 影响: `request_export`/`export_from_preset` 用 grant 的 path 覆写 output_path 后，`allow_overwrite=False` 的授权与 `allow_overwrite=True` 行为完全相同——已存在的输出文件可被静默覆盖，授权合同形同虚设。
  - 建议: 在 request_export（或 preflight）中检查目标路径存在性，`not path_grant.allow_overwrite and Path.exists()` 时拒绝并返回稳定诊断码；把 allow_overwrite 传入 ExportRequest。
  - 置信度: 高（死标志与缺失检查均已 grep 证实；最终编码 worker 在本批次外，未核验）

- [AUD-13403] [Suggestion] `EditSessionHost` Protocol 把 composite 方法声明为必需成员，运行时却按可选 getattr 探测——类型契约与实际用法相反
  - 位置: src/features/edit/ai_editing/edit_api.py:110-117 与 236-237、244-246、255-257
  - 证据:
    ```python
    # Protocol 中（无默认实现 → 对实现者是必需成员）：
    def get_composite_snapshot(self) -> dict[str, Any] | None: ...
    def validate_composite_edit(self, plan: CompositeEditPlan) -> CompositeValidationReport: ...
    ```
    而使用处：
    ```python
    fn = getattr(self._host, "get_composite_snapshot", None)
    return fn() if fn is not None else None
    ```
    注释自述 "keeping them optional at runtime preserves the existing headless/fake hosts"。
  - 影响: 静态类型检查会把不实现 composite 方法的宿主判为不符合 `EditSessionHost`（107-109 行注释明确说这类宿主合法存在）；运行时防御性 getattr 又表明协议声明不可信。两套语义并存，后续维护者无所适从。
  - 建议: 在 Protocol 内提供默认实现体（`return None` / `raise RuntimeError(...)`），或移出 Protocol 改为独立的 CompositeHost 协议。
  - 置信度: 高

- [AUD-13404] [Suggestion] `apply_with_confirmation` 失败分支重复调用 `get_state()`，new_project/new_sequences 可能来自不同瞬时状态
  - 位置: src/features/edit/ai_editing/edit_api.py:309-333
  - 证据:
    ```python
    if preview.has_errors:
        return CommitResult(
            applied=False,
            new_project=self._host.get_state().project,
            new_sequences=dict(self._host.get_state().sequences),
            error="plan has validation errors",
    ```
    （三个失败分支同构，共 6 次 get_state 调用）
  - 影响: 每个分支两次独立取状态，若宿主状态在两次调用之间被其它提交改变（渲染/脚本线程），返回的 `new_project` 与 `new_sequences` 可能来自不同 revision 的混合快照，违背 CommitResult"两层状态保持一致"的语义；同时也是多余的宿主往返。
  - 建议: 方法开头取一次 `state = self._host.get_state()` 复用于所有分支。
  - 置信度: 高（代码确定；实际竞态概率取决于宿主实现，标注为中低）

### 通过项
- 线程安全：模块自身无 Qt/GPU 依赖，长任务经 JobRegistry；`EditSession._current` 全局绑定按宿主单线程设计使用（未见并发 bind/unbind 路径）。
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不适用（纯逻辑层，模块 docstring 明确禁止 UI 线程 GPU 操作且无 GPU 调用）。
- 状态提交一致性：apply → TransactionService.commit → on_committed 顺序正确；授权失败返回 applied=False 且工程不变（已对照 transaction_service.py:458-471）。
- 输入验证：`inspect_subtitle_track` 对 timeline/track 存在性与类型显式校验并抛 ValueError；`load_plan` 走 EditPlan.from_dict。
- 类型注解完整。

---

## src/features/edit/ai_editing/edit_capability_plugin.py (行数: 235)

### 发现

- [AUD-13405] [Suggestion] `EditCapabilityDescriptor.display_name` 未做类型归一化，非字符串值可通过 `__post_init__` 校验后在 `to_dict` 崩溃
  - 位置: src/features/edit/ai_editing/edit_capability_plugin.py:45-48 与 62-66
  - 证据:
    ```python
    def __post_init__(self) -> None:
        plugin_id = str(self.plugin_id or "").strip()
        if not plugin_id or not str(self.display_name or "").strip():
            raise ValueError("edit capability plugin_id and display_name are required")
    ```
    ```python
    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "display_name": self.display_name[:200],
    ```
  - 影响: `plugin_id` 经 `str(...)` 归一化后回写（54 行 `object.__setattr__`），`display_name` 只做 `str(...)` 判空但保留原值——`display_name=123` 之类非字符串通过校验，`register()` 构造 `PluginCapability(metadata={"edit_capability": descriptor.to_dict()})` 时在 `[:200]` 处抛 TypeError，注册路径崩溃。边界输入验证缺口。
  - 建议: 与 plugin_id 一致，`object.__setattr__(self, "display_name", str(self.display_name or "").strip())` 并保持非空校验。
  - 置信度: 高（路径确定；触发需畸形描述符，概率低）

### 通过项
- 线程安全：适配器声明 `thread_safe: False / execution_mode: exclusive`，调用统一经 `PluginRegistry.invoke_tool`（其内部持锁取 entry 并强制 active 状态，plugin_runtime.py:732-749 已核验）。
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不适用（无 GPU、无信号）。
- 状态提交一致性：插件只允许 propose（PlanMode.PREVIEW 强制，177-181 行），commit 仅经宿主 facade 且要求 approval 非 None（204-205 行），fail-closed。
- 异常与取消路径：`propose` 对 `EditPlan.from_dict` 失败捕获并转为 EDIT_PLAN_INVALID 稳定错误（174-176 行）；ToolFacade.invoke 内部 fail-closed（tool_facade.py:712-717 已核验）。
- 输入验证：operation_kinds/source 归一化并去重；消息截断 500 字符防泄漏。
- 类型注解完整。

---

## src/features/edit/ai_editing/edit_project_service.py (行数: 350)

### 发现

- [AUD-13406] [Major] `CommitResult` docstring 承诺 revision 冲突返回 `applied=False`，`commit_command` 实际 raise `RevisionConflict`——公开"统一写入入口"的错误通道与契约不符
  - 位置: src/features/edit/ai_editing/edit_project_service.py:84-88（契约）与 204-208（实现）
  - 证据:
    ```python
    成功时 ``applied=True``，... 失败（revision 冲突 / domain 校验失败 / 异常）时
    ``applied=False`` 且 ``error`` 描述原因；两层状态保持不变（无半套 timeline）。
    ```
    ```python
    # 乐观锁：AI/脚本路径必须传 base_revision，与当前 revision 比较。
    if base_revision is not None and base_revision != sequence.revision:
        raise RevisionConflict(
            base_revision, sequence.revision, timeline_id=timeline_id
        )
    ```
  - 影响: `commit_command` 被文档定位为"UI/AI/脚本的统一写入入口"。按 docstring 只检查 `result.applied` 的直接调用方会被未捕获的 RevisionConflict 击穿。当前 `TransactionService.commit` 捕获了该异常（transaction_service.py:480-486 已核验），但任何绕过 TransactionService 的直接调用（类本身鼓励这种复用）都没有这层保护；且同为失败原因，domain 校验失败走 CommitResult、revision 冲突走异常，错误通道不一致。
  - 建议: 二选一并保持一致——commit_command 捕获 RevisionConflict 转为 `CommitResult(applied=False, error=...)`（保留 RevisionConflict 供深层使用），或修正 CommitResult/类 docstring 明确"revision 冲突以 RevisionConflict 异常上抛"。
  - 置信度: 高

- [AUD-13407] [Suggestion] `preview_command` 的 `validate_timeline` 未包异常保护，与 commit 路径对同一调用的处理不一致
  - 位置: src/features/edit/ai_editing/edit_project_service.py:342-350（对照 commit_command 的 227-235）
  - 证据:
    ```python
    try:
        proposed = command.apply(sequence)
        if isinstance(proposed, tuple):
            proposed = proposed[0]
    except Exception as exc:
        return sequence, [f"Command failed to apply: {exc}"]
    issues = validate_timeline(proposed)
    return proposed, issues
    ```
  - 影响: commit_command 明确处理了"Timeline validation raised"（227-235 行 try/except 转 error），preview_command 却让 `validate_timeline` 的异常直接穿透——畸形 proposed 可能使 preview 调用链（如 `EditSession.apply_with_confirmation` → `_txn.preview`）以异常而非 `CommitResult(applied=False)`/errors 列表收场，违背 preview"只校验不抛"的 dry-run 语义。
  - 建议: 与 commit 路径一致地包裹并转为 `[f"Timeline validation raised: {exc}"]`。
  - 置信度: 高（不一致确定；`_txn.preview` 内部是否有兜底未逐行核验，标注中）

### 通过项
- 线程安全：无状态纯函数式服务（staticmethod/classmethod，无实例可变状态）。
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不适用（docstring 明确无 UI/Qt/GPU 职责，代码符合）。
- 状态提交一致性：`assert_sequences_consistent` 把 save-time key 一致性检查提到提交边界；revision 只在提交边界 bump（246 行）；`_sync_two_layers` 保持 ref↔sequence key 一致并 bump project revision；失败路径均返回原状态副本，无半套状态。
- 异常路径：command.apply 异常捕获转为 CommitResult error，不产生半提交。
- 输入验证：get_sequence 对缺失/未物化 timeline 抛明确 ValueError；乐观锁语义（None=UI 路径不检查）有文档。
- 性能：函数内 `import time` 仅为避免顶层开销差异，可忽略；整体 O(1) 字典操作。
- 类型注解完整。

---

## src/features/edit/ai_editing/editorial_advanced.py (行数: 391)

### 发现

- [AUD-13408] [Suggestion] `hashlib`、`json` 导入未使用
  - 位置: src/features/edit/ai_editing/editorial_advanced.py:18-19
  - 证据:
    ```python
    from dataclasses import dataclass, field
    import hashlib
    import json
    from typing import Any, Iterable, Mapping
    ```
    （全文件无 `hashlib.` / `json.` 使用）
  - 影响: 死导入增加阅读噪声，暗示已删除的哈希/序列化逻辑残留。
  - 建议: 删除两个导入。
  - 置信度: 高

- [AUD-13409] [Suggestion] `ActiveAnglePlan`、`BeatGrid` 的 confidence 未做 [0,1] 校验，与同类模型不一致
  - 位置: src/features/edit/ai_editing/editorial_advanced.py:96-108、116-125（对照 89-93、173-175）
  - 证据:
    ```python
    @dataclass(frozen=True)
    class ActiveAnglePlan:
        ...
        confidence: float = 0.8

        @property
        def needs_confirmation(self) -> bool:
            return self.confidence < 0.75
    ```
    而 `MulticamSyncEvidence.__post_init__`（92-93 行）与 `BRollCandidate.__post_init__`（174-175 行）均强制 `0.0 <= confidence <= 1.0`。
  - 影响: confidence=1.5 之类外部数据静默通过：`needs_confirmation` 恒 False，本应确认的多机位切换计划被当作高置信处理；`BeatGrid.confidence` 同样无守护。同一模块内校验标准不一致。
  - 建议: 为两个 dataclass 补 `__post_init__` 范围校验。
  - 置信度: 高

- [AUD-13410] [Suggestion] `detect_conflicts` 对重复 clip_id 生成重复 conflict_id，且 ai_plan_ref/human_change 为占位空值
  - 位置: src/features/edit/ai_editing/editorial_advanced.py:265-284
  - 证据:
    ```python
    ai_set = set(ai_target_clip_ids)
    conflicts: list[TransactionConflict] = []
    for clip_id in human_changed_clip_ids:
        if clip_id in ai_set:
            conflicts.append(TransactionConflict(
                conflict_id=f"conflict-{clip_id}",
                ...
                ai_plan_ref="",
                human_change="edited",
                conflict_type="overwrite",
            ))
  - 影响: `human_changed_clip_ids` 为 Iterable，含重复时产生同 conflict_id 的多条冲突（下游按 id 去重/索引会互相覆盖）；`ai_plan_ref=""`/`human_change="edited"` 是硬编码占位，审计记录无法回溯真实计划引用与人工改动内容。
  - 建议: 先 `dict.fromkeys` 去重 human_changed_clip_ids；为函数补充 ai_plan_ref/human_change 参数或显式标注为未实现。
  - 置信度: 高

### 通过项
- 纯数据契约层：线程安全、GPU/GLSL/信号维度不适用。
- 输入验证（部分）：MulticamSyncEvidence 校验 sync_method 白名单与 confidence 范围；BRollCandidate 校验 confidence。
- 状态一致性：全部 frozen dataclass，`may_auto_insert` 门槛（confidence≥0.85 + license ok + brand compliant + 非 suggestion）多条件与注释语义一致。
- 类型注解完整。

---

## src/features/edit/ai_editing/editorial_models.py (行数: 665)

### 发现

- [AUD-13411] [Suggestion] 多个 `from_dict` 直通字段未做 str 归一化/范围校验，往返后类型不变量可被破坏
  - 位置: src/features/edit/ai_editing/editorial_models.py:553-562（UserOverride）、644-658（SemanticSegment）、288-316（EditorialBrief.safe_area_profile）、455-463（VariantScores）
  - 证据:
    ```python
    return UserOverride(
        override_id=str(data["override_id"]),
        target_clip_id=data.get("target_clip_id"),
        related_decision_id=data.get("related_decision_id"),
        kind=str(data.get("kind", "")),
    ...
    ```
    `target_clip_id`/`related_decision_id` 注解为 `str | None` 但未强制转换；`VariantScores` 的 duration_fit/beat_coverage/pace_estimate docstring 标注 0..1 但 from_dict 未校验。
  - 影响: 畸形持久化数据（int/list 值）可直接注入声明为 `str | None` 的字段，破坏 frozen dataclass 的类型不变量，下游字符串操作（拼接、切片、split）时崩溃；越界分数进入排序展示。对本模块"可验证对象"的定位是完整性缺口。
  - 建议: `str(v) if v is not None else None` 归一化直通字段；VariantScores 补范围钳制或校验。
  - 置信度: 高（缺口确定；触发需外部数据，概率低）

### 通过项
- 纯数据模型层：线程安全、GPU/GLSL/信号维度不适用。
- 状态提交一致性：明确"叠加在 canonical timeline 之上、绝不直接改变工程"；FrameRange/DurationTarget 的整帧合同（非负 start、正 duration、min≤ideal≤max）在 `__post_init__` 强制；brief 变更走 `with_revision_bumped` 产生新 revision 而非改写。
- 输入验证（核心字段）：FrameRange/DurationTarget/EditorialBrief/EditDecision/SemanticSegment 的 id 非空、confidence∈[0,1]、brand_requirements 经 BrandElementRequirement.from_dict 验证。
- to_dict/from_dict 除上述直通字段外对称（枚举经 value 往返、可选项按存在性写入）。
- 类型注解完整。

---

## src/features/edit/ai_editing/editorial_quality.py (行数: 341)

### 发现

- [AUD-13412] [Suggestion] `audio_continuity` 检查无条件记入 `checks_run`，通过时却不产生 finding——与其他检查的记录语义不一致
  - 位置: src/features/edit/ai_editing/editorial_quality.py:272-280
  - 证据:
    ```python
    # Audio continuity.
    checks.append("audio_continuity")
    if audio_gaps > 0:
        score = max(0.0, 1.0 - audio_gaps * 0.1)
        findings.append(EditorialQualityFinding(
    ```
    对照其它检查（如 250-251、283-284 行）均为"条件成立才 append check 且必产 finding"。
  - 影响: `audio_gaps == 0` 时 `checks_run` 含 audio_continuity 但 findings 无对应记录，消费方无法区分"检查通过"与"检查未跑"；且通过的时长/节拍检查会产 info finding 而通过的音频检查不产，报告口径不一。
  - 建议: 与其它检查对齐——仅 `audio_gaps > 0` 时 append，或通过时也产 severity="info" 的 finding。
  - 置信度: 高

- [AUD-13413] [Suggestion] `UsableRange.recommendation` 注释声明枚举集合但未校验取值
  - 位置: src/features/edit/ai_editing/editorial_quality.py:40-47
  - 证据:
    ```python
    @dataclass(frozen=True)
    class UsableRange:
        start_frame: int
        end_frame: int
        recommendation: str         # use / avoid / trim_candidate
        reason: str = ""

        def __post_init__(self) -> None:
            if self.start_frame < 0 or self.end_frame <= self.start_frame:
                raise ValueError(...)
    ```
  - 影响: 任意字符串通过校验；下游按 `recommendation == "use"` 分支时，拼写错误（如 "Use"）静默落入其它分支，推荐/回避区间语义失效——对外部（AI/provider）产生的数据是验证缺口。
  - 建议: 定义 frozenset 白名单并在 `__post_init__` 校验。
  - 置信度: 高

### 通过项
- 纯 Qt-free 契约层：线程安全、GPU/GLSL/信号维度不适用。
- 输入验证（部分）：UsabilityDimension 校验 score∈[0,1] 且 weight≥0；UsableRange 校验 start<end 非负；TimelineEditOperation 校验 kind 白名单与必填字段。
- `compute_usability_score` 对空集/零权重安全返回 0.0。
- `evaluate_editorial_quality` 各分值均经 max/min 钳制在 [0,1]；`is_passing` 以 error 严重级门控，评分只作参考。
- 性能：全 O(n) 纯计算，无热点。
- 类型注解完整。

---

## src/features/edit/ai_editing/eval_runner.py (行数: 208)

### 发现

- [AUD-13414] [Suggestion] `require_quality_gate=False` 只容忍 gate 缺失、不豁免已失败的 gate——策略开关语义与实际行为相悖
  - 位置: src/features/edit/ai_editing/eval_runner.py:140-141 与 189-190
  - 证据:
    ```python
    gate = trace.gate_result
    gate_passed = gate.passed if gate is not None else not policy.require_quality_gate
    ```
    ```python
    return ReleaseGateResult(
        allowed=not reasons and gate_passed,
    ```
  - 影响: 策略声明"不要求 quality gate"，但 trace 携带失败 gate 时 `gate_passed=False` 仍强制 `allowed=False`。开关无法实现其字面语义（跳过 gate 评估），配置了 `require_quality_gate=False` 的发布流程会被一次门禁失败意外阻断；方向 fail-closed 安全，但与字段名/默认策略的表达不符。
  - 建议: `require_quality_gate=False` 时 `gate_passed` 恒 True（仅记录结果），或拆成 `tolerate_missing`/`enforce` 两个明确字段并在 docstring 说明。
  - 置信度: 中（行为确定；属设计意图歧义而非明确缺陷）

### 通过项
- 线程安全：每次 `run` 的状态均为局部变量，注入式 clock 便于测试。
- 异常与取消路径：`EvalSuiteRunner.run` 对单个 case 的 `except Exception` 有 noqa 注释、转为失败用例数据（error_code=EVAL_CASE_EXCEPTION）且经 `redact_text` 脱敏并截断——符合"失败是数据不是崩溃"的评估器边界设计，不属违规宽泛 except。
- 输入验证/失败安全：空套件 pass rate 0.0 触发阻断（fail-closed）；matrix 缺失/类别缺失/真实证据缺失均产生 reasons；commercial audit 双条件（passed + report_id）校验。
- 隐私：error_message 与 report_id 均经 redact_text 处理并限长。
- 类型注解完整。

---

## 批次总结

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 4 | AUD-13400, AUD-13401, AUD-13402, AUD-13406 |
| Suggestion | 11 | AUD-13403, AUD-13404, AUD-13405, AUD-13407, AUD-13408, AUD-13409, AUD-13410, AUD-13411, AUD-13412, AUD-13413, AUD-13414 |

共性观察：本批次为 AI 剪辑契约/服务层，整体质量较高（frozen dataclass、fail-closed 边界、稳定诊断码）。主要风险集中在三处契约-实现失配（from_dict 降级、allow_overwrite 死标志、RevisionConflict 错误通道）与一处铁律违规（宽泛 except 吞宿主错误）。无 GPU/GLSL/信号相关代码，相关维度均为不适用。
