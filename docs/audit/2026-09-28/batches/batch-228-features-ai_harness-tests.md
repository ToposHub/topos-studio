# Batch 228 审查报告：src/features/ai_harness/tests/

批次：batch-228（AUD-22800–AUD-22899），P2，8 个测试文件，共 2,146 行。
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。
本批全部为纯逻辑测试（无 OpenGL、无 Qt 信号、无渲染路径），GPU/GLSL/信号维度按"不涉及"处理。
汇总：Critical 0 / Major 4 / Suggestion 11。

---

## src/features/ai_harness/tests/test_runtime_node_events.py (行数: 125)

### 发现

- [AUD-22800] [Suggestion] no-op 用例无任何断言，仅靠"不抛"通过
  - 位置: src/features/ai_harness/tests/test_runtime_node_events.py:83-87
  - 证据:
    ```python
    def test_emit_node_event_unknown_run_is_noop() -> None:
        """不存在的 run_id 静默 no-op（不抛）。"""
        rt = HarnessRuntime()
        # 不应抛
        rt.emit_node_event("nonexistent", "started", "n1")
    ```
  - 影响: `emit_node_event` 返回 bool（runtime.py:665，False=run 不存在/detached 的 no-op），该契约未被断言；订阅者未收到事件、返回值为 False 均未验证。若实现改为静默吞错或错误发布全局事件，测试无法发现。
  - 建议: 补 `assert rt.emit_node_event(...) is False`，并注册订阅者断言未收到任何事件。
  - 置信度: 高

### 通过项
- 线程安全：单线程内直接调用 runtime API，无并发假设
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及（纯逻辑测试）
- 异常与取消路径：unknown run no-op 有专门用例（见 AUD-22800 的断言缺口）
- 输入验证：9 种 kind 映射逐一断言（queued/started/progress/waiting_input/succeeded/failed/cancelled/cached/skipped）
- 状态提交一致性：payload 携带 node_id、run 级 `_emit` 保持 node_id="" 的红线回归均有断言
- 类型注解：模块函数均带 `-> None` / `-> Invocation`

---

## src/features/ai_harness/tests/test_scheduler_adapter.py (行数: 176)

### 发现

- [AUD-22801] [Suggestion] 全部测试方法与助手缺少返回类型注解
  - 位置: src/features/ai_harness/tests/test_scheduler_adapter.py:37,45-46,63,72,86,93,101,108,116,125,137,155,165
  - 证据:
    ```python
    def _start_run(rt: HarnessRuntime) -> str:
        ...

    class TestJobRefFromHandle:
        def test_converts_active_job(self):
            h = JobHandle(kind=JobKind.ANALYSIS, status=JobStatus.RUNNING,
    ```
  - 影响: 同目录 test_runtime_node_events.py / test_skill_dry_run.py 等均带 `-> None`，本文件与 .clinerules 强类型约束不一致；不利于静态检查发现误返回。
  - 建议: 为所有测试方法补 `-> None`。
  - 置信度: 高

- [AUD-22802] [Suggestion] edit_core 标记在同批次 8 文件中仅 3 个使用，标记口径不一致
  - 位置: src/features/ai_harness/tests/test_scheduler_adapter.py:19
  - 证据:
    ```python
    pytestmark = pytest.mark.edit_core
    ```
    同批次 test_runtime_node_events.py / test_skill_dry_run.py / test_skill_eval_suite.py / test_skill_release.py / test_skill_studio_drafts.py 均无 pytestmark（grep -L 验证）；pytest.ini:23 注册了该 marker。
  - 影响: 用 `-m edit_core`（或 `-m "not edit_core"`）选择运行时，5 个未标记文件会被静默排除/包含，同属 ai_harness 的测试分裂到两套选择集。
  - 建议: 统一批次内标记——要么 8 个文件都挂 edit_core，都改用 ai_edit/ai_harness 专属标记。
  - 置信度: 高

### 通过项
- 线程安全：不涉及并发，桥接 API 与实现签名一致（scheduler_adapter.py:66-113 逐条核对）
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及
- 异常与取消路径：cancel token 真实传播 4 用例（传播/未取消不动/终态不动/幂等）+ detach 后 sync 拒绝且进度不回写（与 runtime.attach_job/update_job_progress 的 detached 拒绝一致）
- 输入验证：unknown run_id 返回 False、未 bind 的 sync 返回 False 均覆盖
- 性能模式：无重复构造开销问题
- 状态提交一致性：JobRef 去重（第二次 sync 不重复 append）有断言

---

## src/features/ai_harness/tests/test_skill_catalog.py (行数: 270)

### 发现

- [AUD-22803] [Major] test_subtitle_draft_has_target_duration 名不副实：注释与 manifest 事实相反，且未断言 target_duration 的任何行为
  - 位置: src/features/ai_harness/tests/test_skill_catalog.py:50-58
  - 证据:
    ```python
    def test_subtitle_draft_has_target_duration(self):
        fields = build_form_fields(OFFICIAL_SUBTITLE_DRAFT)
        names = [f.name for f in fields]
        # subtitle_draft ui_card.form_fields=() 但 brief_template 无 target_duration
        # → 不会自动补；只可能为空
        assert "asset_ids" not in names  # 自动填的不进表单
        assert "timeline_id" not in names
    ```
    而事实是 OFFICIAL_SUBTITLE_DRAFT.brief_template 明确含有该键（src/features/edit/ai_editing/skill_manifest.py:443-447）：
    ```python
    brief_template={
        "required_beats": (),
        "target_duration": {"ideal": 720, "min": 648, "max": 792},
        "platform": "vertical_social",
    },
    ```
    且 skill_catalog.py:96-103 的兜底逻辑会因 ideal=720 自动补 target_duration 字段（ui_card.form_fields=()，schema required 全为自动填字段）。
  - 影响: 测试名与文件头 docstring（"target_duration 按 brief_template 默认补"）承诺的行为没有任何断言；注释声称"brief_template 无 target_duration、不会自动补"与数据相反。若 build_form_fields 的自动补/兜底逻辑回归（ideal/min/max 丢失），本测试与 test_voiceover_polish_has_target_duration 之外的 subtitle 路径无人守护。
  - 建议: 修正注释；补 `assert "target_duration" in names` 及 default==720 / min==648 / max==792 断言（与 voiceover 用例对称）。
  - 置信度: 高

- [AUD-22804] [Suggestion] 测试方法与 _registry() 助手缺少类型注解
  - 位置: src/features/ai_harness/tests/test_skill_catalog.py:196-198,50,108,202 等
  - 证据:
    ```python
    def _registry():
        from src.features.edit.ai_editing.skill_registry import SkillRegistry
        return SkillRegistry()
    ```
  - 影响: 同 AUD-22801；与同目录带注解的测试文件不一致。
  - 建议: 补 `-> SkillRegistry` 与各方法 `-> None`。
  - 置信度: 高

### 通过项
- 线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及
- 异常与取消路径：无异常路径需求（纯函数）
- 输入验证：缺证据/缺模型/无 profile/local_dev unverified → ready=False 全部覆盖，且与实现核对一致
- 状态提交一致性：registry 只读（不 register 修改），仅 test_card_unverified_flag 显式注册临时 skill
- 性能模式：recommend 排序断言 O(n²) 仅测试开销，无问题

---

## src/features/ai_harness/tests/test_skill_compiler.py (行数: 383)

### 发现

- [AUD-22805] [Suggestion] 测试方法与 _build_service_with_analysis 缺少类型注解
  - 位置: src/features/ai_harness/tests/test_skill_compiler.py:47,157,179,191,207,222,235,255,283,305,327,346,354,367
  - 证据:
    ```python
    def _build_service_with_analysis(segs):
        from src.features.edit.ai_editing.brief_service import BriefService
    ```
  - 影响: 同 AUD-22801；参数 segs 与返回值 (AssistantService, ProjectSequences) 类型未声明。
  - 建议: 补类型注解（可拆 Tuple 返回类型）。
  - 置信度: 高

- [AUD-22806] [Suggestion] "三入口同结构"仅比较 to_public_dict 的键集合，一致性验证鉴别力弱
  - 位置: src/features/ai_harness/tests/test_skill_compiler.py:276-281（同模式 302）
  - 证据:
    ```python
    # 同结构：字段集合一致
    assert set(rec_chat.to_public_dict().keys()) == set(rec_wf.to_public_dict().keys())
    # 同 skill_id / 同终态集合
    assert rec_chat.skill_id == rec_wf.skill_id == "topos.voiceover_polish"
    ```
  - 影响: P1.3 验收核心是"三入口同一执行路径"；仅比键集合无法发现两侧走不同代码路径但字段恰好同名的回归（如 jobs/events/artifacts 形状分叉）。
  - 建议: 增加形状级对比（jobs/artifacts/diagnostics 的元素键集合、events 的 kind 序列前缀等）。
  - 置信度: 中

### 通过项
- 线程安全：EditSession 全局单例 bind/unbind 由 fixture yield-teardown 配对清理（135-136），不泄漏跨用例状态
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及
- 异常与取消路径：unrunnable 编译 → FAILED + COMPILE_FAILED + MISSING_REQUIRED_INPUT 诊断有断言（305-325）
- 输入验证：缺 brief_id、空 asset_ids 阻断均有覆盖（206-233），与编译器诊断契约一致
- 性能模式：TestCompilerPurity 用 try/finally 恢复被替换方法，无泄漏
- 状态提交一致性：编译纯度红线（不调 service.run_workflow/run_skill）有守护

---

## src/features/ai_harness/tests/test_skill_dry_run.py (行数: 287)

### 发现

- [AUD-22807] [Major] 红线"无副作用"事件断言使用不存在/拼写错误的 RunEventKind 字符串，全部恒真
  - 位置: src/features/ai_harness/tests/test_skill_dry_run.py:199-202,239-242,249-252
  - 证据:
    ```python
    event_kinds = {e.kind for e in outcome.run_record.events}
    assert "approval_granted" not in event_kinds
    assert "timeline_committed" not in event_kinds
    ```
    以及 240-242、251-252 的 `"network_request"` / `"cloud_upload"` / `"process_spawn"` / `"shell_exec"` / `"code_exec"` / `"fs_write"`。
    真实词汇表（src/features/ai_harness/invocation_models.py:180-203）只有点分形式且无上述 kind：
    ```python
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_CONSUMED = "approval.consumed"
    ```
    （全表仅 run.*/node.*/artifact.*/job.*/approval.*/cost.*/diagnostic，无 timeline_committed、network_request、cloud_upload、process_spawn 等。dry-run 走真实 HarnessRuntime 构造事件。）
  - 影响: "approval_granted"（下划线）永远不等于真实常量 "approval.granted"——即使 dry-run 错误地发出审批事件，该红线断言也不会失败；其余 6 个事件名在词汇表中根本不存在，断言对任何 RunRecord 恒真。红线（dry-run 不审批/不写时间线/不联网/不执行任意代码）实际处于无守护状态。
  - 建议: 改用 RunEventKind 常量断言（如 `RunEventKind.APPROVAL_GRANTED.value not in event_kinds`）；不存在的事件类别应改为断言 executor/provider 层的注入点而非虚构事件名。
  - 置信度: 高

- [AUD-22808] [Major] 文件 docstring 声称覆盖"权限不足 → PERMISSION_DENIED"，但无任何用例断言该错误码
  - 位置: src/features/ai_harness/tests/test_skill_dry_run.py:15-16（docstring）与 187-202（唯一权限用例）
  - 证据:
    ```python
    - 失败场景：缺模型 → 终态 FAILED + MODEL_MISSING；缺证据 → EVIDENCE_INSUFFICIENT；
      超预算 → BUDGET_EXCEEDED；权限不足 → PERMISSION_DENIED。
    ```
    而唯一的权限用例只断言 preview 与（恒真的）事件检查：
    ```python
    outcome = SkillDryRunner().run(draft, fixture)
    assert outcome.edit_plan is not None
    assert outcome.edit_plan.mode.value == "preview"
    ```
    DryRunErrorCode.PERMISSION_DENIED 已定义（skill_dry_run.py:218），但 `_check_permission` 对非任意代码 executor 恒通过（skill_dry_run.py:734-740），该失败路径在本文件与整个 tests/ 目录均无断言（grep 验证仅 docstring 一处提及）。
  - 影响: docstring 承诺的第四类失败场景零覆盖；类名 TestDryRunPermissionDenied 名不副实。若 PERMISSION_OK 检查/PERMISSION_DENIED 映射回归，无人发现。
  - 建议: 构造可触发的权限失败 fixture（或注入 fake executor 返回越权 plan），断言 error_code == "PERMISSION_DENIED"；或修正 docstring 移除虚假覆盖声明。
  - 置信度: 高

- [AUD-22809] [Suggestion] 未使用的导入 dataclasses.replace
  - 位置: src/features/ai_harness/tests/test_skill_dry_run.py:190
  - 证据:
    ```python
    from dataclasses import replace
    draft = _voiceover_draft()
    # 把草稿风险改成 write（即便如此，dry-run 不得真正写时间线）
    draft = draft.update_field("risk", SkillRisk.WRITE_REQUIRES_APPROVAL, kind="patch")
    ```
  - 影响: 死导入；实际用的是 update_field（"risk" 在 _DRAFT_UPDATABLE_FIELDS 白名单内，skill_studio_drafts.py:308-313，调用合法）。
  - 建议: 删除该导入。
  - 置信度: 高

### 通过项
- 线程安全：纯单线程，runner 不共享
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及
- 输入验证：缺模型（空能力集）/缺证据（空证据）/超预算（budget_limit=0.0）三类失败路径有真实断言，错误码与 DryRunErrorCode 一致
- 状态提交一致性：fixture 确定性（两次工厂调用相等）、cache_key 稳定性/区分度（同草稿同 key、不同 draft_id 不同 key）覆盖
- 性能模式：无异常开销
- 类型注解：完整（全部 `-> None` / `-> SkillDraft`）

---

## src/features/ai_harness/tests/test_skill_eval_suite.py (行数: 317)

### 发现

- [AUD-22810] [Suggestion] upload_rejected 用例 fixture 与草稿错配，且判定断言对词汇表恒真，鉴别力弱
  - 位置: src/features/ai_harness/tests/test_skill_eval_suite.py:128-139
  - 证据:
    ```python
    def test_upload_rejected_case(self) -> None:
        """upload_rejected: policy 禁止 upload → dry-run 不联网（拒绝）。"""
        case = SkillEvalCase(
            kind=EvalCaseKind.UPLOAD_REJECTED,
            name="upload_rejected",
            fixture_factory=make_auto_caption_fixture_factory(),
        )
        result = case.evaluate(_voiceover_draft())
    ```
    auto_caption fixture（能力 asr/demucs）配 voiceover 草稿；断言 `assert "cloud_upload" not in kinds` 中的 "cloud_upload" 不在 RunEventKind 词汇表内（invocation_models.py:180-203），恒真。_judge 的 UPLOAD_REJECTED 判定（skill_eval_suite.py:340-344）同样是"无 network/cloud 事件即 PASS"。
  - 影响: 该用例无法检测"策略禁止上传被忽略"的回归——dry-run 根本不存在会上传的路径时判定永远 PASS；fixture 错配进一步使用例脱离真实组合。发布门禁（P4.4 消费该报告）对 upload_rejected 这一最低用例的保护是形式性的。
  - 建议: fixture 与草稿配对一致（voiceover fixture + voiceover draft）；判定改为可失败的具体谓词（如 policy.allow_generation=False 时断言存在 upload_rejected/diagnostic 事件或检查项）。
  - 置信度: 高（fixture 错配为事实；判定语义为设计局限）

- [AUD-22811] [Suggestion] 函数内重复导入 TimelineSnapshot；建议事件断言改用 RunEventKind 常量
  - 位置: src/features/ai_harness/tests/test_skill_eval_suite.py:304-305（对照模块顶部 30-33），及 245-246,252-253,260 的词汇表外事件名
  - 证据:
    ```python
    from src.features.ai_harness.skill_dry_run import TimelineSnapshot
    stale = TimelineSnapshot(
    ```
    模块顶部已 `from src.features.ai_harness.skill_dry_run import (DryRunFixture, TimelineSnapshot, make_auto_caption_fixture)`；245-260 的 "cloud_upload"/"network_request"/"timeline_committed" 不在 RunEventKind 词汇表内。
  - 影响: 死重复导入（噪音）；词汇表外事件名断言恒真（同 AUD-22807 根因），无副作用红线的实际守护只落在 "approval.granted"/"approval.consumed"（拼写正确）两条上。
  - 建议: 删除重复导入；事件断言统一改用 RunEventKind 常量。
  - 置信度: 高

### 通过项
- 线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及
- 异常与取消路径：cancel 用例断言终态 cancelled + result.cancelled（与 skill_eval_suite.py:351-356 判定一致）
- 输入验证：revision stale（revision=-1 sentinel）→ STALE_REVISION 有断言；六类最低用例 kind 集合校验通过
- 状态提交一致性：report 可序列化（to_dict 键校验）、失败草稿如实报告 report.passed=False
- 性能模式：无异常开销
- 类型注解：完整

---

## src/features/ai_harness/tests/test_skill_release.py (行数: 266)

### 发现

- [AUD-22812] [Major] test_publish_arbitrary_code_executor_blocked 恒真，门禁 ARBITRARY_CODE_EXECUTOR 拒绝路径零覆盖
  - 位置: src/features/ai_harness/tests/test_skill_release.py:179-190
  - 证据:
    ```python
    def test_publish_arbitrary_code_executor_blocked(self) -> None:
        """红线：executor_kind 任意代码的草稿不得发布。"""
        gate = SkillReleaseGate()
        ...
        result = gate.publish(draft, released_by="dev-alice")
        # 正常 draft 应通过；这里只验证门禁不会放行任意代码路径
        if result.success:
            assert not result.release.executor_kind.is_arbitrary_code
    ```
    门禁确有该拒绝分支（src/features/ai_harness/skill_release.py:249-258）：
    ```python
    if draft.executor_kind.is_arbitrary_code:
        return PublishResult(
            success=False,
            deny_code=PublishDenyCode.ARBITRARY_CODE_EXECUTOR,
    ```
    但 ExecutorKind 所有成员 is_arbitrary_code 恒 False（skill_studio_drafts.py:73-148），正常草稿不可能触发该分支；测试从未构造任意代码 executor，`if result.success:` 条件断言使其不可能失败（result.success=False 时直接空跑通过）。
  - 影响: 发布门禁"二次防御"分支（红线）在整套测试中零覆盖——若该分支被误删或条件写反（如 `if not ...is_arbitrary_code: return deny`），无任何测试报警；用例名给出虚假的安全感。
  - 建议: 用对象替换/子类注入一个 is_arbitrary_code=True 的假 executor_kind 草稿（绕过枚举构造），断言 deny_code == ARBITRARY_CODE_EXECUTOR；或删除该用例并在 skill_release.py 侧补可测的注入点。
  - 置信度: 高

### 通过项
- 线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及
- 异常与取消路径：eval 失败阻断（EVAL_FAILED）有真实失败草稿注入与断言
- 输入验证：dev 版本（0.x）阻断 DEV_VERSION_NOT_PUBLISHABLE 覆盖；发布产物无 secret/绝对路径断言（/Users/、sk-、password）
- 状态提交一致性：幂等发布（同草稿同版本同 content_hash）、不同版本不同 hash、local_dev 注册不覆盖 official（count 断言）均覆盖
- 性能模式：无异常开销
- 类型注解：完整

---

## src/features/ai_harness/tests/test_skill_studio_drafts.py (行数: 322)

### 发现

- [AUD-22813] [Suggestion] kind_for_workflow 用例断言过弱：仅断言值属于 3 值集合
  - 位置: src/features/ai_harness/tests/test_skill_studio_drafts.py:56-59
  - 证据:
    ```python
    def test_kind_for_workflow_returns_declared(self) -> None:
        assert ExecutorKind.kind_for_workflow({}).value in {
            "workflow_runner", "taskgraph_roles", "editorial_builder",
        }
    ```
  - 影响: 空入参的映射结果未钉住具体值；kind_for_workflow 的路由规则（nodes/policy → kind）若在三个合法值之间漂移，测试无鉴别力。方法名 returns_declared 承诺的"返回声明值"未验证。
  - 建议: 钉住空 workflow 的确定返回值，并补带 nodes/policy 的用例覆盖路由分支。
  - 置信度: 高

- [AUD-22814] [Suggestion] 草稿隔离红线用例探测私有属性，实现重命名后退化为恒真
  - 位置: src/features/ai_harness/tests/test_skill_studio_drafts.py:303-309
  - 证据:
    ```python
    def test_drafts_isolated_from_registry(self) -> None:
        """红线：草稿不得自动进入生产 SkillRegistry。"""
        store = SkillStudioStore()
        d = _make_minimal_draft()
        store.save(d)
        # store 不应内置对 SkillRegistry 的引用 / 注册副作用
        assert not hasattr(store, "_registry") or store._registry is None
    ```
  - 影响: 断言绑定私有属性名 `_registry`；若实现改名（如 `_prod_registry`），`not hasattr` 使左枝恒真，用例静默失效。红线"保存草稿不触碰生产注册表"没有被行为级验证。
  - 建议: 改为行为断言：保存草稿前后构建全局/官方 SkillRegistry 的条目数不变，或 mock SkillRegistry.register 断言未被调用。
  - 置信度: 高

### 通过项
- 线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及
- 异常与取消路径：非法版本串 ValueError、非法 bump kind ValueError、未知字段 KeyError 均覆盖
- 输入验证：ExecutorKind 构造期拒绝 forbidden kinds（pytest.raises match）+ 枚举成员恒非任意代码，红线守护到位
- 状态提交一致性：版本单调（patch/minor/major）、content_hash 随内容变化、草稿不可变（旧对象未被修改）均断言
- 性能模式：无异常开销
- 类型注解：完整

---

## 批次级备注

- Major 中的 3 个（AUD-22807、AUD-22808、AUD-22812）共同指向同一模式：红线类断言恒真或声称覆盖但零断言；建议后续批次对 ai_harness 其余测试文件优先做同口径筛查（词汇表外事件名、条件断言、无断言用例）。
- AUD-22802 的标记不一致为批次内事实（grep -L 验证），修复时应全批次统一。
