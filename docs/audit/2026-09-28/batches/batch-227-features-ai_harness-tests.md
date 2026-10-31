# batch-227 审查报告 — src/features/ai_harness/tests/

批次：batch-227（P2）｜编号区间：AUD-22700–AUD-22799｜文件 8 个，共 2,118 行。
统计：Critical 0 ｜ Major 2 ｜ Suggestion 12。

批次级说明（详见 AUD-22704）：本批 8 个测试文件属于 `src/features/ai_harness/tests/`，
而仓库 `pytest.ini` 的 `testpaths = tests`，且 `.github/workflows/` 全部 pytest 调用只指向
`tests/...` —— 该套件（含本批全部安全红线测试）不被任何 CI 工作流或默认 pytest 运行收集。

---

## src/features/ai_harness/tests/test_p3_red_lines.py (行数: 242)

### 发现
- [AUD-22700] [Suggestion] 测试方法与辅助函数缺类型注解
  - 位置: src/features/ai_harness/tests/test_p3_red_lines.py:156（及 48、62、91 等）
  - 证据:
    ```python
    def _adapter(self):
        reg = ProviderRegistry()
    ```
    ```python
    def test_llm_executor_has_no_approval_fields(self):
        ex = LlmRoleExecutor(skill_id="t", skill_version="1")
    ```
  - 影响: 与同包 test_p4_acceptance.py / test_p4_security.py（全部 `-> None`）风格不一致；
    削弱 .clinerules 强类型约束在测试层的示范作用。
  - 建议: 为所有 test 方法补 `-> None`，为 `_adapter` 补返回类型注解。
  - 置信度: 高

### 通过项
- 线程安全：纯单线程对象级测试，无共享可变状态、无 Qt/OpenGL 触碰
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及（无 UI、无渲染代码）
- 异常与取消路径：consent 缺失 → WAITING_FOR_USER、MCP 未 allowlist/未启用拒绝均有断言
- 输入验证：`denied_not_allowlisted` / `denied_not_enabled` / `denied_write_needs_approval` 路径覆盖
- 断言有效性：`_T` fake transport 在模块级后置定义但运行期解析无碍；事件 kind 子串过滤
  （"approval"/"commit"/"apply"）与实现的事件命名一致，未见恒真断言

---

## src/features/ai_harness/tests/test_p4_acceptance.py (行数: 356)

### 发现
- [AUD-22701] [Suggestion] PREVIEW 红线断言被 `if` 包裹，存在空转风险
  - 位置: src/features/ai_harness/tests/test_p4_acceptance.py:273-275
  - 证据:
    ```python
    outcome = SkillDryRunner().run(ready, make_voiceover_polish_fixture())
    if outcome.edit_plan is not None:
        assert outcome.edit_plan.mode.value == "preview"
    ```
  - 影响: 若回归导致 `edit_plan` 变为 None（skill_dry_run.py 失败路径均返回
    `edit_plan=None`），本"发布后仍 PREVIEW"红线测试将静默通过，不产生任何断言。
  - 建议: 先 `assert outcome.edit_plan is not None`，再断言 mode；与同文件 84 行的
    `assert outcome.edit_plan is not None` 保持一致。
  - 置信度: 高
- [AUD-22702] [Suggestion] `__import__` 动态导入 ToolPolicy 且函数内重复局部导入
  - 位置: src/features/ai_harness/tests/test_p4_acceptance.py:238-241（另 80、257、272）
  - 证据:
    ```python
    tool_policy=__import__(
        "src.features.edit.ai_editing.skill_manifest", fromlist=["ToolPolicy"]
    ).ToolPolicy(allowed_tools=("exec_python", "call_shell")),
    ```
    ```python
    from src.features.ai_harness.skill_dry_run import make_voiceover_polish_fixture
    ```
    （该局部导入在 80、257、272 三处重复，而顶层已从 skill_dry_run 导入其他名字）
  - 影响: 绕过静态导入降低可读性与可 grep 性；`ToolPolicy` 完全可并入顶层 import
    （同包 test_p4_security.py:40 即是静态导入）。
  - 建议: 顶层补 `ToolPolicy`；`make_voiceover_polish_fixture` 提升至模块顶部导入。
  - 置信度: 高
- [AUD-22703] [Suggestion] 缺 `pytestmark = pytest.mark.edit_core` 标记
  - 位置: src/features/ai_harness/tests/test_p4_acceptance.py:30-56
  - 证据:
    ```python
    import pytest

    from src.features.ai_harness.editorial_skills import p2_official_manifests
    ...
    from src.features.edit.ai_editing.skill_manifest import (
        OFFICIAL_VOICEOVER_POLISH, ...
    ```
    （无 pytestmark 赋值；同包 32 个测试文件中 21 个声明了 `pytestmark = pytest.mark.edit_core`，
    pytest.ini:23 注册了该标记"requires edit module"）
  - 影响: 本文件重度依赖 `src.features.edit.ai_editing`，却缺 edit 模块标记；
    按 `-m edit_core` 选择性运行时会漏掉这批验收测试，同包内标记口径不一致。
  - 建议: 补 `pytestmark = pytest.mark.edit_core`（与 test_p3_red_lines.py:19 一致）。
  - 置信度: 中

### 通过项
- 线程安全 / GPU / GLSL·HDR / 信号防递归：不涉及
- 异常与取消路径：eval 失败、dev 版本不可发布（DEV_VERSION_NOT_PUBLISHABLE / EVAL_FAILED）覆盖
- 输入验证：无 secret/路径红线（`"/Users/"`、`"sk-"`、repr 级 Windows 路径 `"C:\\\\Users"`
  与 repr 转义语义吻合，检查正确）
- 状态提交一致性：local_dev → unverified 在草稿/发布/注册三期不变式有断言
- 类型注解：test 方法均带 `-> None`，通过
- 断言有效性：端到端两流水线断言具体（manifest schema 相等、graph_template 相等），非恒真

---

## src/features/ai_harness/tests/test_p4_security.py (行数: 289)

### 发现
- [AUD-22704] [Major] 本批全部测试未被任何 CI / 默认 pytest 收集，安全红线测试实际不进 CI
  - 位置: src/features/ai_harness/tests/test_p4_security.py:1（适用于本批 8 个文件及整个
    `src/features/ai_harness/tests/` 共 32 个文件）
  - 证据:
    ```ini
    # pytest.ini:2
    testpaths = tests
    ```
    ```yaml
    # .github/workflows/pr-fast-gate.yml:89-95, 185-195, 219 等全部 pytest 调用均只指向 tests/...
    run: python -m pytest tests/project -q --no-header -p no:cacheprovider
    run: python -m pytest tests/edit -q --no-header -p no:cacheprovider
    ```
    （grep 全部 workflows / scripts / Makefile：无任何 `pytest src` 调用；
    tests/conftest.py:215-258 的自动打标也仅覆盖 `tests/` 路径）
  - 影响: 本文件声称的"第三方不得执行任意代码"七层深度防御、P3/P4 全部红线验收、
    runtime 快照恢复/取消等测试，在默认 `pytest` 与全部 CI 工作流中都不会被收集执行；
    红线回归只能靠开发者手动传入路径才会被发现。
  - 建议: 二选一——(a) 在 CI（如 pr-fast-gate）增加 `python -m pytest src/features/ai_harness/tests`
    步骤；(b) 将该套件纳入 `testpaths` 或建立 tests/ 下的收集入口。修复前应先本地跑通确认无漂移。
  - 置信度: 高（仓库内证据完备；不排除仓库外自定义运行方式，但仓库内无任何入口）
- [AUD-22705] [Major] "层 4 发布门禁拒绝任意代码"实未测试，且断言守卫可空转
  - 位置: src/features/ai_harness/tests/test_p4_security.py:164-172（对照文件头 11 行声明）
  - 证据:
    ```python
    def test_publish_gate_rejects_arbitrary_executor(self) -> None:
        """红线：发布门禁二次防御（即便绕过 ExecutorKind 构造，仍拒绝）。"""
        gate = SkillReleaseGate()
        # 正常 draft 应通过；这里验证门禁不会放行任意代码
        d = _ready_draft()
        result = gate.publish(d, released_by="third-party")
        # 正常 draft 通过 → 验证 release.executor_kind 仍受控
        if result.success:
            assert not result.release.executor_kind.is_arbitrary_code
    ```
    （文件头第 11 行声称覆盖"4. **发布期**：发布门禁拒绝 executor_kind=任意代码的草稿"；
    而被测拒绝路径 skill_release.py:250-258 `deny_code=PublishDenyCode.ARBITRARY_CODE_EXECUTOR`
    从未被触发）
  - 影响: 测试名与文档声称验证门禁拒绝，实际只发布了一个正常草稿——若 `result.success`
    为 False 测试也静默通过；门禁的二次防御分支（ARBITRARY_CODE_EXECUTOR）零覆盖，
    该分支被删改不会被任何测试发现。
  - 建议: 用 monkeypatch / `dataclasses.replace` 构造 `executor_kind.is_arbitrary_code=True`
    的草稿（或直接构造带任意值的 SkillRelease），断言 `publish` 返回
    `deny_code == PublishDenyCode.ARBITRARY_CODE_EXECUTOR` 且 `success is False`；去掉
    `if result.success:` 守卫改为无条件断言。
  - 置信度: 高
- [AUD-22706] [Suggestion] docstring 声称"对未知值视为危险"但未覆盖该回退路径
  - 位置: src/features/ai_harness/tests/test_p4_security.py:174-181（对照 skill_release.py:117-126）
  - 证据:
    ```python
    def test_release_executor_kind_property_safe(self) -> None:
        """SkillRelease.executor_kind 对未知值视为危险。"""
        release = SkillRelease.from_draft(
            draft=_ready_draft(), eval_suite_version="v1", released_by="x",
        )
        # 正常 release → 受控
        assert not release.executor_kind.is_arbitrary_code
    ```
    （实现中的未知值回退分支 skill_release.py:121-126 `class _Dangerous: ... is_arbitrary_code = True`
    未被任何测试触达）
  - 影响: docstring 承诺的保守拒绝属性（未知 executor_kind_value → 危险）无覆盖，
    该回退分支被删除不会导致测试失败。
  - 建议: 直接构造 `SkillRelease(..., executor_kind_value="python_exec", ...)`，
    断言 `release.executor_kind.is_arbitrary_code is True`。
  - 置信度: 高
- [AUD-22707] [Suggestion] `_is_error_diag` 与 test_p4_acceptance.py `_is_error` 重复实现
  - 位置: src/features/ai_harness/tests/test_p4_security.py:59-70（对照 test_p4_acceptance.py:350-356）
  - 证据:
    ```python
    def _is_error_diag(d: object) -> bool:
        """判断一个 Diagnostic 是否 ERROR 级。 ..."""
        sev = getattr(d, "severity", None)
        if sev is None:
            return False
        if hasattr(sev, "value"):
            return str(sev.value).lower() == "error"
        return str(sev).lower() == "error"
    ```
  - 影响: 同包两份近似逻辑双份维护，易漂移（两处判定口径一旦分叉，同一 Diagnostic
    在两个测试文件中的 ERROR 判定结果不一致）。
  - 建议: 提取到共享 helper（conftest.py 或 tests 包内公共模块），两个文件统一引用。
  - 置信度: 高
- [AUD-22708] [Suggestion] 缺 `pytestmark = pytest.mark.edit_core` 标记
  - 位置: src/features/ai_harness/tests/test_p4_security.py:21-42
  - 证据:
    ```python
    import pytest

    from src.features.ai_harness.skill_dry_run import SkillDryRunner, make_voiceover_polish_fixture
    ...
    from src.features.edit.ai_editing.skill_manifest import (
        OFFICIAL_VOICEOVER_POLISH, SkillManifest, SkillRisk, SkillSource, ToolPolicy,
    )
    ```
    （无 pytestmark；文件依赖 `src.features.edit.ai_editing`）
  - 影响: 同 AUD-22703——按标记选择性运行时这批安全测试被漏掉，同包标记口径不一致。
  - 建议: 补 `pytestmark = pytest.mark.edit_core`。
  - 置信度: 中

### 通过项
- 线程安全 / GPU / GLSL·HDR / 信号防递归：不涉及
- 异常与取消路径：dry-run 失败降级（edit_plan=None）路径在实现侧存在，事件级红线断言正确
- 输入验证：ExecutorKind 构造期拒绝（FORBIDDEN_EXEC_KINDS 参数化）、registry
  FORBIDDEN_TOOLS 参数化拒绝、validate_tool_call 双重过滤覆盖良好
- 状态提交一致性：local_dev unverified/source 不可升级不变式覆盖完整
- 断言有效性：层 1/2/5 断言具体（ValueError 拒绝、ERROR 级诊断、注册结果为 None）

---

## src/features/ai_harness/tests/test_result_card.py (行数: 193)

### 发现
- [AUD-22709] [Suggestion] `_action` 辅助函数为死代码
  - 位置: src/features/ai_harness/tests/test_result_card.py:38-39
  - 证据:
    ```python
    def _action(state, kind):
        return next(a for a in state.available_actions if a.kind == kind)
    ```
    （全文仅使用 `_action_kinds`；`_action(` 无任何调用点，各测试均内联
    `next(a for a in actions if ...)`）
  - 影响: 未使用代码，误导读者以为存在调用方；内联重复本可由该 helper 消除。
  - 建议: 删除 `_action`，或让测试改用它以消除内联重复。
  - 置信度: 高
- [AUD-22710] [Suggestion] 辅助函数与测试方法缺类型注解
  - 位置: src/features/ai_harness/tests/test_result_card.py:34-39（及 48、55 等全部 test 方法）
  - 证据:
    ```python
    def _action_kinds(state) -> set[str]:
        return {a.kind for a in state.available_actions}


    def _action(state, kind):
        return next(a for a in state.available_actions if a.kind == kind)
    ```
  - 影响: `state` 参数未注解（可为 `ResultCardState`）；test 方法均无 `-> None`，
    与同包 p4 文件风格不一致。
  - 建议: 补 `state: "ResultCardState"` 与各方法 `-> None`。
  - 置信度: 高

### 通过项
- 线程安全 / GPU / GLSL·HDR / 信号防递归：不涉及（纯数据构造测试）
- 状态提交一致性：approved/applied 状态机与 apply 启用逻辑的映射有正反两侧断言
- 输入验证：untrusted 禁 apply、OCR 不可信标注、error 卡无 apply/approve 均覆盖
- 断言有效性：`disabled_reason` 文案断言与红线语义对应，未见恒真断言

---

## src/features/ai_harness/tests/test_result_card_builder.py (行数: 342)

### 发现
- [AUD-22711] [Suggestion] skill_id 可追溯性断言被 `or "r1" in text` 短路，几乎恒真
  - 位置: src/features/ai_harness/tests/test_result_card_builder.py:341-342
  - 证据:
    ```python
    text = f"{state.result_id} {state.title} {state.summary}"
    assert "topos.auto_caption" in text or "r1" in text, "应可追溯到 skill/run"
    ```
    （`_make_run` 固定 `run_id="r1"`（95-96 行），result_id 通常派生自 run_id，
    右侧析取几乎必然成立）
  - 影响: "skill_id 传入后标题/摘要体现来源"的检查被 run_id 兜底短路——即使
    builder 完全丢弃 skill_id，测试仍通过。
  - 建议: 若断言意图是 skill 可追溯，改为单侧 `assert "topos.auto_caption" in text`；
    若 run 兜底是产品行为，拆成两个显式断言并注明优先级。
  - 置信度: 高
- [AUD-22712] [Suggestion] lambda 赋值（E731 noqa ×5）与 test 方法缺 `-> None`
  - 位置: src/features/ai_harness/tests/test_result_card_builder.py:181（另 203、216、231、260；方法如 109）
  - 证据:
    ```python
    resolver = lambda aid: draft if aid == "draft-1" else None  # noqa: E731
    ```
    ```python
    def test_failed_run_returns_error_result(self):
    ```
  - 影响: 靠 noqa 压制的 E731 五处重复；测试方法无返回注解，与 p4 文件风格不一致。
  - 建议: 改为局部 `def resolver(aid): ...`；补 `-> None`。
  - 置信度: 高

### 通过项
- 线程安全 / GPU / GLSL·HDR / 信号防递归：不涉及
- 异常与取消路径：resolver 抛异常 → 降级不崩（308-320）覆盖良好；FAILED/CANCELLED → error 卡
- 输入验证：未知 typed artifact、无 resolver、corrupt 均有兜底断言
- 状态一致性：ACTIVE 状态 → None（参数化 RUNNING/QUEUED/VALIDATING/WAITING_FOR_USER/SKIPPED）
- 类型注解：fake dataclass 与 `_make_run` 均带注解（含 `-> int` property），部分通过

---

## src/features/ai_harness/tests/test_rollout_metrics.py (行数: 71)

### 发现
（无）

### 通过项
- 通过（无发现）：聚合逻辑（recovery/handoff/latency/cost/quality gate）、
  敏感信息不泄漏（`"/private/secret" not in str(public)`）、空输入可序列化
  （p95_latency_ms == 0）均有具体断言
- 线程安全 / GPU / GLSL·HDR / 信号防递归 / 状态提交一致性：不涉及
- 类型注解：两个 test 函数均带 `-> None`，通过
- 注：本文件未声明 `pytestmark = pytest.mark.edit_core`，但其不导入
  `src.features.edit.ai_editing`，与标记语义（requires edit module）不冲突，不计发现

---

## src/features/ai_harness/tests/test_run_trace.py (行数: 182)

### 发现
- [AUD-22713] [Suggestion] skipped_entries 精确匹配内部异常类名，与实现细节耦合
  - 位置: src/features/ai_harness/tests/test_run_trace.py:126（对照 src/features/ai_harness/run_trace.py:473）
  - 证据:
    ```python
    assert report.skipped_entries == ("s:event_1:ValueError",)
    ```
    ```python
    # run_trace.py:473
    skipped.append(f"{sid}:event_{index}:{type(exc).__name__}")
    ```
  - 影响: 断言把恢复失败归因到实现内部抛出的具体异常类型（ValueError）；实现侧重构
    校验异常类型（如引入自定义异常）即误报，而行为（跳过损坏行、fail-closed）未变。
  - 建议: 放宽为前缀/子串匹配（如 `entry.startswith("s:event_1:")`），只锚定会话与行号。
  - 置信度: 中

### 通过项
- 线程安全 / GPU / GLSL·HDR / 信号防递归：不涉及
- 异常与取消路径：损坏行跳过、未知 schema 拒绝且不改动现有 store、坏元数据原子性覆盖良好
- 输入验证：绝对路径脱敏（`"<absolute-path>"`）、secret 脱敏（`api_key=<redacted-secret>`）、
  payload 截断（< 1300）、去重（同 event_id → None）、clear 后 seq 单调不减
- 类型注解：全部 test 函数带 `-> None`，通过

---

## src/features/ai_harness/tests/test_runtime.py (行数: 443)

### 发现
- [AUD-22714] [Suggestion] 测试方法缺 `-> None` 返回注解
  - 位置: src/features/ai_harness/tests/test_runtime.py:61（及 74、86、93 等全部 test 方法）
  - 证据:
    ```python
    def test_start_assigns_run_id_and_emits_created(self):
        rt = HarnessRuntime()
    ```
    （辅助 `_make_invocation` / `_wf_invocation` 已带完整注解，仅 test 方法缺失）
  - 影响: 与同包 p4 文件（全部 `-> None`）风格不一致；.clinerules 强类型约束未贯穿到测试层。
  - 建议: 批量补 `-> None`。
  - 置信度: 高

### 通过项
- 线程安全：订阅者异常被吞（243-252）、退订后不再收事件（232-241）验证了 runtime 的事件分发健壮性
- 异常与取消路径：cancel 幂等、cancel→finish→CANCELLED、handoff 拒绝终态 run、
  retry 旧 run 不变、detach 后 finish/set_status/cancel/retry 全部冻结——覆盖是本批最完整的
- 输入验证：未知 run_id 返回 None/False；restore 对 None 行、重复行、未知 schema 拒绝且不污染
- 状态提交一致性（快照持久化）：export→json roundtrip 后 active run 标记 RUN_INTERRUPTED /
  application_restart、终态保持、redaction（"/Users/testuser" 与 secret 不出现在序列化结果）
- 性能模式：`abs(cost - 0.3) < 1e-9` 浮点比较处理正确；progress clamp 断言正确
- 断言有效性：事件序列精确匹配（`events == [(rid, "run.created")]`），未见恒真断言
