# batch-237 审查报告：src/features/ai_workflow/tests/（8 文件，1,837 行）

编号区间：AUD-23700–AUD-23799。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR /
信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。

本批全部为纯 Python 测试文件（无 Qt 控件、无 ModernGL/GLSL、无渲染路径），故
线程安全、GPU 生命周期、GLSL·HDR、信号防递归四个维度整体为 N/A，在各文件通过项中
一并说明。重点核查状态提交一致性（approval/revision/rollback）、断言强度、异常路径
与输入验证覆盖。所有涉及被测源码行为的结论均已 Read 源码验证（workflow_compiler.py、
workflow_action_service.py、skill_release.py）。

---

## src/features/ai_workflow/tests/test_publish_as_skill.py (行数: 215)

### 发现

- [AUD-23700] [Major] deny_code 断言被 `or result.deny_code` 弱化为"任意拒绝码即通过"
  - 位置: src/features/ai_workflow/tests/test_publish_as_skill.py:157-158
  - 证据:
    ```python
        assert not result.success
        assert "DEV_VERSION" in result.deny_code or result.deny_code
    ```
  - 影响: 测试意图是"版本 < 1.0.0 被 DEV_VERSION 门禁拒绝"，但 `or result.deny_code`
    使第二个断言在 deny_code 为任意非空字符串时都通过。若版本门禁回归（例如
    DEV_VERSION_NOT_PUBLISHABLE 检查被删除，发布因 EVAL_FAILED 等其它门禁失败），
    该测试仍然绿灯，红线（plan §10.7.4 发布走 P4 同一门禁的版本检查）失去回归保护。
  - 建议: 改为精确断言
    `assert result.deny_code == "DEV_VERSION_NOT_PUBLISHABLE"`（或至少去掉
    `or result.deny_code`，仅保留子串断言并断言 `result.release is None`）。
  - 置信度: 高（已核实 skill_release.py:61 中拒绝码为
    `DEV_VERSION_NOT_PUBLISHABLE`，子串 `"DEV_VERSION"` 本可匹配；问题仅在 `or` 分支）

- [AUD-23701] [Major] executor_kind 安全红线断言包裹在 `if result.success:` 内，失败时静默跳过
  - 位置: src/features/ai_workflow/tests/test_publish_as_skill.py:160-168
  - 证据:
    ```python
        def test_executor_kind_safe(self) -> None:
            """红线：发布的 workflow skill executor_kind 受控（不执行任意代码）。"""
            ...
            result = publisher.publish(
                graph=_make_graph(), author="x", skill_title="T", version="1.0.0",
            )
            if result.success:
                assert not result.release.executor_kind.is_arbitrary_code
    ```
  - 影响: 这是"不执行任意代码"的安全红线测试，但当 publish 因任何原因失败时，
    断言体完全不执行、测试静默通过。门禁误伤（合法 workflow 被错误拒绝导致安全
    检查从未运行）或发布成功路径变化都不会被察觉。
  - 建议: 先 `assert result.success and result.release is not None`，再无条件断言
    `not result.release.executor_kind.is_arbitrary_code`。
  - 置信度: 高

- [AUD-23702] [Suggestion] 未使用的 import：pytest、SkillReleaseGate、PublishFromWorkflowResult
  - 位置: src/features/ai_workflow/tests/test_publish_as_skill.py:23,25,42
  - 证据:
    ```python
    import pytest

    from src.features.ai_harness.skill_release import SkillReleaseGate
    ...
    from src.features.ai_workflow.core.publish_as_skill import (
        PublishFromWorkflowResult,
        WorkflowPublisher,
    )
    ```
  - 影响: 全文件无 `pytest.`、`SkillReleaseGate`、`PublishFromWorkflowResult` 的任何
    使用（已 grep 核实，各仅出现 1 次即 import 行）。徒增阅读噪音，暗示测试删改后
    未清理。
  - 建议: 删除三个未使用 import；可启用 ruff F401 兜底。
  - 置信度: 高

### 通过项
- 状态提交一致性：发布产物 unverified/local_dev 标记、skill_id draft. 前缀不覆盖
  official、graph_template 为模板副本且含参数覆盖，均有断言。
- 异常路径：无 except 掩盖；失败路径经 `result.success` 显式断言（除上述两处弱断言）。
- 输入验证：dev 版本拒绝路径有覆盖（见 AUD-23700 的断言强度问题）。
- 线程安全 / GPU 生命周期 / GLSL·HDR / 信号防递归：N/A（纯 Python 测试，无 Qt/GL 对象）。

---

## src/features/ai_workflow/tests/test_template_registry.py (行数: 202)

### 发现

- [AUD-23703] [Suggestion] 未使用的 import：pytest、WorkflowTemplateEntry、WorkflowGraph、WorkflowPolicy
  - 位置: src/features/ai_workflow/tests/test_template_registry.py:20,27,33-34
  - 证据:
    ```python
    import pytest
    ...
    from src.features.ai_workflow.core.template_registry import (
        WorkflowTemplateEntry,
        WorkflowTemplateRegistry,
        register_official_templates,
    )
    from src.features.ai_workflow.core.workflow_models import (
        WorkflowGraph,
        WorkflowPolicy,
    )
    ```
  - 影响: 已 grep 核实 `pytest`、`WorkflowTemplateEntry`、`WorkflowPolicy` 仅出现在
    import 行；`WorkflowGraph` 另一次出现是模块 docstring 的 `:class:` 引用，非代码
    使用。未使用 import 造成误导。
  - 建议: 删除未使用 import。
  - 置信度: 高

### 通过项
- 输入验证：模板元数据（template_id/display_name/description/category/skill_id）
  非空断言齐全；模板节点 spec 全部在 NodeSpecRegistry 注册的回归检查有效。
- 状态提交一致性：官方模板 frozen → instantiate 生成新 workflow_id 副本且
  content_hash 变化，断言正确。
- 异常路径：approval_token 红线测试对含 apply_approved_plan 的模板逐一验证
  MISSING_APPROVAL_TOKEN 不出现，循环内 continue 逻辑正确（当前官方模板集中确有
  含 apply 节点者，非空转）。
- 序列化：to_dict 的 schema/skill_id/graph 字段断言与 plan §3.2 一致。
- 线程安全 / GPU 生命周期 / GLSL·HDR / 信号防递归：N/A（纯 Python 测试）。

---

## src/features/ai_workflow/tests/test_template_runnability.py (行数: 223)

### 发现

- [AUD-23704] [Suggestion] `_compile_template` 与各测试方法缺返回类型注解
  - 位置: src/features/ai_workflow/tests/test_template_runnability.py:61-66,81-83,96-97
  - 证据:
    ```python
    def _compile_template(
        compiler: WorkflowCompiler,
        templates: WorkflowTemplateRegistry,
        template_id: str,
    ):
        entry = templates.require(template_id)
    ```
  - 影响: 辅助函数无返回注解（应标注 `-> WorkflowCompilation`），两个测试方法
    `test_targets_routable`、`test_routes_to_voiceover_polish_backend` 等缺
    `-> None`；与同目录其它文件（如 test_workflow_compiler.py 全量 `-> None`）风格
    不一致。
  - 建议: 补齐返回类型注解，统一风格。
  - 置信度: 高

### 通过项
- 宣称的回归红线"所有模板编译 runnable=True"实际有效：已核实
  workflow_compiler.py:256 `success=compiled.runnable`，故 `result.success` 断言
  等价于 runnable 断言。
- 输入验证：target 路由白名单（workflow/editorial）断言防 legacy_unsupported 回归；
  无边必填端口的 MISSING_REQUIRED_INPUT 反例测试有效。
- 异常路径：无 except；失败信息聚合（failures 列表带 error codes）便于诊断。
- 状态提交一致性：node_ids 管道（编译产物 → invocation_to_workflow_input →
  WorkflowInput.node_ids）与聊天入口退化空 tuple 均有断言。
- 线程安全 / GPU 生命周期 / GLSL·HDR / 信号防递归：N/A（纯 Python 测试）。

---

## src/features/ai_workflow/tests/test_workflow_action_service.py (行数: 184)

### 发现

- [AUD-23705] [Major] rollback 测试声称验证"宿主无回滚能力时 fail closed"，实际走的是
  NO_APPLIED_PLAN 分支，ROLLBACK_UNAVAILABLE 路径零覆盖
  - 位置: src/features/ai_workflow/tests/test_workflow_action_service.py:180-184
  - 证据:
    ```python
    def test_rollback_fails_closed_without_host_capability() -> None:
        bridge = _Bridge()
        actions = _action_service(bridge, ApprovalService())
        actions.register_plan("run-no-rollback", _plan())
        assert actions.rollback("run-no-rollback").diagnostic_code == "NO_APPLIED_PLAN"
    ```
    被测源码 workflow_action_service.py:189-197 的分支顺序：
    ```python
        if not applied:
            return self._failure(run_id, "NO_APPLIED_PLAN", ...)
        bridge = self._bridge_provider()
        rollback = getattr(bridge, "rollback_ai_command", None) if bridge is not None else None
        if not callable(rollback):
            return self._failure(run_id, "ROLLBACK_UNAVAILABLE", ...)
    ```
  - 影响: 该测试只 register 从不 apply，回滚在第一个分支即以 NO_APPLIED_PLAN 失败，
    永远到不了 `ROLLBACK_UNAVAILABLE`（宿主缺 rollback_ai_command 能力的 fail-closed
    分支）。测试名与意图（宿主能力缺失）不符；若该分支回归（例如改成直接抛异常或
    误调用 bridge），现有测试无法发现。
  - 建议: 新增用例：apply 成功后使用无 `rollback_ai_command` 的 `_Bridge` 调用
    rollback，断言 `diagnostic_code == "ROLLBACK_UNAVAILABLE"` 且
    `bridge.commit_count` 不变、时间线未被修改；或把本测试改名并补上述用例。
  - 置信度: 高（分支顺序已 Read 源码核实）

- [AUD-23706] [Suggestion] 未使用 import `compile_plan`；伪造对象方法缺类型注解
  - 位置: src/features/ai_workflow/tests/test_workflow_action_service.py:10,57-58,65-80
  - 证据:
    ```python
    from src.features.edit.ai_editing.plan_compiler import compile_plan
    ...
    class _Service:
        def preview_plan(self, plan, state):
            return TransactionService().preview(plan, state)
    ...
        def commit_ai_command(self, timeline_id, macro, *, undo_label):
    ```
  - 影响: `compile_plan` 全文件未使用（仅 import 行出现一次）；`_Service`/`_Bridge`
    的方法参数与返回值均无注解，与项目强类型要求不符。
  - 建议: 删除未使用 import；为伪造对象补类型注解（返回 `PreviewResult`、
    `TimelineSequence` 等）。
  - 置信度: 高

### 通过项
- 状态提交一致性（本批最关键维度）：覆盖优秀——apply 幂等（重放 already_applied 且
  commit_count==1）、STALE_REVISION 在消费 grant 前拦截（active_count()==1）、
  approval 缺失 fail-closed、rollback 后 can_rollback 翻转、host 回滚守卫
  （expected_undo_label/expected_revision 断言）。
- 异常与取消路径：无宽泛 except；失败路径全部经 diagnostic_code 显式断言。
- 输入验证：空 grant_id（""）作为越界输入有覆盖。
- 线程安全 / GPU 生命周期 / GLSL·HDR / 信号防递归：N/A（纯 Python 测试，被测服务
  内部锁行为通过单线程用例间接验证）。

---

## src/features/ai_workflow/tests/test_workflow_compiler.py (行数: 280)

### 发现

- [AUD-23707] [Suggestion] 未使用的 import：pytest、Invocation、RunPolicy
  - 位置: src/features/ai_workflow/tests/test_workflow_compiler.py:25,27
  - 证据:
    ```python
    import pytest

    from src.features.ai_harness.invocation_models import Invocation, RunPolicy
    ```
  - 影响: 已 grep 核实：`pytest` 仅 import 行；`Invocation`/`RunPolicy` 其余出现
    均在模块 docstring（`:class:` 引用），无代码使用。文件实际通过
    `result.invocation` 属性访问，无需直接 import。
  - 建议: 删除三个未使用 import。
  - 置信度: 高

- [AUD-23708] [Suggestion] policy 映射断言值与 WorkflowPolicy 默认值相同，测不出映射回归
  - 位置: src/features/ai_workflow/tests/test_workflow_compiler.py:128-137
  - 证据:
    ```python
        def test_compile_policy_mapped_from_graph(self) -> None:
            ...
            # graph.policy.timeline_write=approval_required → policy.timeline_write 一致
            assert result.invocation.policy.timeline_write == "approval_required"
    ```
  - 影响: `_voiceover_graph()` 使用 `WorkflowPolicy()`（默认
    timeline_write="approval_required"，见 test_workflow_models.py:150），RunPolicy
    默认值亦如此。即使编译器完全忽略 graph.policy 而始终输出默认 RunPolicy，该测试
    仍通过——"graph 的 policy 映射为 RunPolicy"的契约未被真正验证。
  - 建议: 构造 `WorkflowPolicy(timeline_write="forbidden")` 的 graph，断言
    `result.invocation.policy.timeline_write == "forbidden"`，再加 network="ask" 等
    变体。
  - 置信度: 高（逻辑确定；因不影响产品行为本身定为 Suggestion）

### 通过项
- 红线覆盖：source="workflow"、user_text==""（防 prompt 注入）、复用 CompiledSkill
  路径（target 白名单）、编译期不执行任务（纯断言无副作用）。
- 异常路径：环图（cycle）与缺能力（empty capabilities）拒绝编译均覆盖，且断言
  diagnostics 含 error。
- 输入验证：graph_template 透传（同一对象 + content_hash 相等）与 to_dict 可序列化
  均有断言。
- 线程安全 / GPU 生命周期 / GLSL·HDR / 信号防递归：N/A（纯 Python 测试）。

---

## src/features/ai_workflow/tests/test_workflow_context.py (行数: 133)

### 发现

- [AUD-23709] [Suggestion] 伪造服务类方法缺返回类型注解
  - 位置: src/features/ai_workflow/tests/test_workflow_context.py:21-26,43-45,52-53
  - 证据:
    ```python
    class _Analysis:
        def get_analysis(self, asset_id: str):
            return SimpleNamespace(to_dict=lambda: {"asset_id": asset_id, "version": 1})
    ...
        def get_project_sequences(self):
            seq = SimpleNamespace(revision=7)
    ...
        def get_brief(self, brief_id: str):
            return SimpleNamespace(revision=3)
    ```
  - 影响: 参数有注解但返回值缺失；与项目"所有函数有类型注解"约束不符（Suggestion 级）。
  - 建议: 补返回注解（如 `-> object` 或对应协议类型），或抽出 Protocol 使伪造对象
    类型可检。
  - 置信度: 高

### 通过项
- 输入验证：无选中素材时 fail-closed（WorkflowContextError + code=="NO_ASSETS"）
  覆盖到位，实例方法替换处已标注 `# type: ignore[method-assign]`。
- 状态提交一致性：plan_hash 确定性（同输入同 hash、base_revision 变化 hash 变）、
  固定 `now=100.0` 排除时间戳干扰、scope.base_revision 与
  inputs["analysis_snapshot_hash"] 透传断言正确；`plan_preview["execution"] ==
  "preview_only_until_approval"` 锁定审批前仅预览语义。
- 异常路径：pytest.raises + match 中文消息精确。
- 线程安全 / GPU 生命周期 / GLSL·HDR / 信号防递归：N/A（纯 Python 测试）。

---

## src/features/ai_workflow/tests/test_workflow_graph_edit.py (行数: 231)

### 发现

- [AUD-23710] [Suggestion] 红线源码扫描为朴素子串匹配，可被动态 import 绕过且可能误伤注释
  - 位置: src/features/ai_workflow/tests/test_workflow_graph_edit.py:38-45
  - 证据:
    ```python
    def test_source_does_not_import_gpu_or_qt_or_composite() -> None:
        """红线：edit 层纯 Python，不 import moderngl / composite.core / Qt。"""
        with open(_SRC, "r", encoding="utf-8") as f:
            source = f.read()
        assert "import moderngl" not in source
        assert "from src.features.composite" not in source
        assert "TextureResult" not in source
        assert "from PyQt6" not in source
        assert "import PyQt6" not in source
    ```
  - 影响: (1) `importlib.import_module("moderngl")`、`from importlib import import_module`
    后字符串调用、别名导入等可绕过子串检查，红线保护可被无声穿透；(2) 反向误伤——
    源码注释/文档字符串中出现 "TextureResult" 等词会导致测试假失败。
  - 建议: 改用 AST 检查（`ast.parse` 后遍历 Import/ImportFrom 节点比对模块名），
    同时不扫描注释与 docstring。
  - 置信度: 中（绕过场景为假设，当前源码无此类导入；机制缺陷确定）

- [AUD-23711] [Suggestion] `_node` 的 `params` 参数与部分测试函数缺类型注解
  - 位置: src/features/ai_workflow/tests/test_workflow_graph_edit.py:54,74,84-105 等
  - 证据:
    ```python
    def _node(node_id: str, *, x: float = 0.0, y: float = 0.0, params=None) -> WorkflowNode:
    ...
    @pytest.fixture
    def edit():
    ```
  - 影响: `params` 无注解（建议 `dict | None`）；`edit` fixture 无返回注解；大量
    `test_*` 函数缺 `-> None`（如 test_move_node_updates_ui 等）。
  - 建议: 补齐注解与本项目其余测试文件对齐。
  - 置信度: 高

### 通过项
- 状态提交一致性（冻结模型红线）：move/set_param/add_edge/remove_edge 均断言返回新
  实例且原 graph 不变（原值捕获比较、`new is not simple_graph`）。
- 输入验证：非 JSON-safe 参数值抛 ValueError、未知 node_id 抛 KeyError、重复边/
  自连/ghost 节点抛 ValueError、未知边 remove 为 no-op，正反例齐全；对"同节点不同
  port 非自连"的语义边界有明确注释与测试。
- 异常路径：全部经 pytest.raises 精确断言异常类型。
- 线程安全 / GPU 生命周期 / GLSL·HDR / 信号防递归：N/A（纯 Python 测试；GPU/Qt
  隔离另由源码扫描测试守护，见 AUD-23710 的机制弱点）。

---

## src/features/ai_workflow/tests/test_workflow_models.py (行数: 369)

### 发现

（无）

### 通过项
- 通过（无发现；本批质量最好的文件）：全量 `-> None` 注解齐备。
- 输入验证：空 node_id/spec_id/workflow_id 拒绝、重复 node_id 拒绝、edge ghost 节点
  拒绝、exposed_input ghost 节点拒绝、非法 network/timeline_write 枚举拒绝、错误
  schema 拒绝——正反例覆盖系统且完整。
- 状态提交一致性：frozen 模型 + content_hash 稳定性（同内容同 hash、内容变异 hash）
  有专测；to_dict/from_dict 往返等价断言保证可序列化契约（plan §3.2）。
- 异常路径：所有拒绝路径均 pytest.raises(ValueError) 显式断言；wrong_schema 用
  match="schema" 锁定消息。
- 线程安全 / GPU 生命周期 / GLSL·HDR / 信号防递归：N/A（纯数据模型测试）。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 3 | AUD-23700, AUD-23701, AUD-23705 |
| Suggestion | 9 | AUD-23702, AUD-23703, AUD-23704, AUD-23706, AUD-23707, AUD-23708, AUD-23709, AUD-23710, AUD-23711 |

合计 12 条；编号区间 AUD-23700–AUD-23711（未用满，剩余留给本批次后续增量）。
