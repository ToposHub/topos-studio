# batch-238 [P2] — src/features/ai_workflow/tests//

编号区间：AUD-23800–AUD-23899。文件 2 个，共 413 行。
审查人核对依据（只读）：`workflow_run_bridge.py`、`workflow_validator.py`、`node_specs.py`、`workflow_models.py`、`ai_harness/runtime.py`、`ai_harness/invocation_models.py`。

---

## src/features/ai_workflow/tests/test_workflow_run_bridge.py (行数: 94)

### 发现

- [AUD-23801] [Suggestion] bridge 声明的 payload 参数与"无效 run_id 静默 no-op"契约均无测试
  - 位置: src/features/ai_workflow/tests/test_workflow_run_bridge.py:45
  - 证据:
    ```python
    emit = bridge.make_emit_for_run(run_id)
    emit("started", "n1")
    emit("succeeded", "n1")
    rec = rt.get_run(run_id)
    ```
  - 影响: `WorkflowRunBridge.make_emit_for_run`（workflow_run_bridge.py:42-56）的闭包签名是 `(kind, node_id, payload=None) -> None`，且文档声明"run 不存在或已 detached 时静默 no-op（runtime 自身保证）"。全部三个用例只做两参调用且 run 均有效，payload 透传（真实后端会传 `{"stage": ...}`）与 `emit_node_event` 返回 False 的 no-op 分支（runtime.py:687-690）零覆盖；这两条回归（如 payload 被丢弃、no-op 变抛错）不会被本文件捕获。
  - 建议: 补两个用例：(1) `emit("progress", "n1", payload={"stage": "s1"})` 后断言事件 payload 含该字段；(2) `bridge.make_emit_for_run("no-such-run")` 调用不抛异常且返回后 runtime 无新增事件。顺带把本文件 docstring 第 9 行的闭包签名 `(kind, node_id) -> None` 修正为含可选 payload。
  - 置信度: 高

- [AUD-23802] [Suggestion] `received` 列表缺少类型注解
  - 位置: src/features/ai_workflow/tests/test_workflow_run_bridge.py:64
  - 证据:
    ```python
    rt = HarnessRuntime()
    received = []
    rt.add_subscriber(lambda e: received.append(e))
    ```
  - 影响: 本文件所有测试函数与 helper 都有完整注解（`-> None`、`-> Invocation`），唯此局部列表裸写 `[]`，strict 模式下 mypy 需要推断依据；与项目强类型风格不一致。
  - 建议: `received: list[RunEvent] = []`（从 `ai_harness.invocation_models` 或 runtime 模块导入 `RunEvent`）。
  - 置信度: 高

### 通过项
- 断言与实现契约一致：kind 映射（"started"/"succeeded"/"failed" → `NODE_STARTED`/`NODE_SUCCEEDED`/`NODE_FAILED`，runtime.py:654-663）、事件追加进 `RunRecord.events`（runtime.py:766 + to_record:91）、订阅者链路（add_subscriber:302-316）、闭包按 run_id 隔离——均已逐条与 runtime 实现核对，无偏差。
- 线程安全：纯 Python 回调测试，不涉 Qt/OpenGL；未跨线程共享未保护状态。
- GPU 资源生命周期：不涉及（无 ModernGL/纹理）。
- GLSL/HDR 正确性：不涉及。
- 信号防递归：不涉及（无 Qt 信号）。
- 状态提交一致性：不涉及（无 undo/redo/revision/持久化）。
- 异常与取消路径：三个用例无 try/except 吞错，断言失败即暴露。
- 输入验证：`_make_invocation` fixture 合法（`InvocationScope.project_id="p1"` 满足必填校验 invocation_models.py:243-244；`source=WORKFLOW` 时 skill_id 非空满足 Invocation.__post_init__:417-420）。
- 性能模式：无循环/重复构造热点。

---

## src/features/ai_workflow/tests/test_workflow_validator.py (行数: 319)

### 发现

- [AUD-23803] [Major] approval_token 红线用 `or` 弱化断言，红线检查回归时测试仍通过
  - 位置: src/features/ai_workflow/tests/test_workflow_validator.py:249
  - 证据:
    ```python
    codes = [f.code for f in findings if f.severity == "error"]
    # apply 节点缺 approval_token 输入
    assert ValidationErrorCode.MISSING_APPROVAL_TOKEN in codes or \
           ValidationErrorCode.MISSING_REQUIRED_INPUT in codes
    ```
  - 影响: 该测试 docstring 明言"红线：apply_approved_plan 必须有 approval_token 输入"（plan §6.4/§1.4）。但 fixture 中 apply 节点的 `approval` 端口（approval_token，required，node_specs.py:438-439）恰好也无连接，必然触发通用的 `MISSING_REQUIRED_INPUT`。因此即便 `_check_approval_token`（workflow_validator.py:338-369）被整段删除或失效，断言仍因右支成立而通过——红线（apply 无 token 不得执行、timeline 写须审批）失去回归保护，是本测试文件唯一声称守卫的红线。
  - 建议: 拆成两条独立断言：`assert ValidationErrorCode.MISSING_APPROVAL_TOKEN in codes` 与 `assert ValidationErrorCode.MISSING_REQUIRED_INPUT in codes`（该图还可加 `PORT_TYPE_MISMATCH` 断言）。
  - 置信度: 高

- [AUD-23804] [Suggestion] approval_token 缺正向用例："token 来自 user.approval"的合法链路未测试
  - 位置: src/features/ai_workflow/tests/test_workflow_validator.py:228
  - 证据:
    ```python
    class TestTimelineWriteApproval:
        def test_apply_without_approval_token(self) -> None:
            """红线：apply_approved_plan 必须有 approval_token 输入。"""
    ```
  - 影响: `TestTimelineWriteApproval` 只有负向用例。没有"apply 连上 `topos.user.approval` 的 token 输出后不再报 MISSING_APPROVAL_TOKEN"的正向用例，validator 的 `_check_approval_token` 可能假阳性（合法图被拒）而测试无法发现；文件头 docstring 声明的红线"approval_token 必须来自 user.approval 节点"（来源约束，而非仅 artifact type）也未锁定——validator 目前只按 `from_type == "approval_token"` 判断（workflow_validator.py:356-357）。
  - 建议: 补正向用例：propose_track → user.approval → apply 的合法链，断言 errors 不含 MISSING_APPROVAL_TOKEN；如需锁定来源，再加"非 user.approval 节点产出 approval_token 时应报错"的规格讨论（当前实现不区分来源，属 validator 侧潜在缺口）。
  - 置信度: 高

- [AUD-23805] [Suggestion] `NodeCategory` 导入未使用
  - 位置: src/features/ai_workflow/tests/test_workflow_validator.py:26
  - 证据:
    ```python
    from src.features.ai_workflow.core.node_specs import (
        NodeCategory,
        NodeSpecRegistry,
        register_official_specs,
    )
    ```
  - 影响: grep 全文件 `NodeCategory` 仅出现在该 import 行，属死导入；ruff F401 会报，增加阅读噪音。
  - 建议: 从 import 列表中删除 `NodeCategory`。
  - 置信度: 高

- [AUD-23806] [Suggestion] validator 分支覆盖缺口：exposed_inputs 满足必填输入、docstring 声明的"锁定轨道"检查无测试
  - 位置: src/features/ai_workflow/tests/test_workflow_validator.py:5
  - 证据:
    ```python
    > WorkflowValidator：缺模型、缺权限、云上传、超预算、锁定轨道、stale revision。
    ```
  - 影响: (1) validator 对 `exposed_inputs` 绑定可满足必填输入有专门分支（workflow_validator.py:242-246），本文件所有 fixture 均未构造 `exposed_inputs`，该分支零覆盖；(2) 文件头与 validator 类 docstring 都声明"锁定轨道"为契约项，但既无对应测试，`ValidationErrorCode` 中也无该错误码（实现与测试两侧都缺，测试文件至少应显式标注该项 deferred 或补测试）。另 `DUPLICATE_NODE_ID`（validator.py:59 定义，从未发出）同样无覆盖。
  - 建议: 补一个带 `exposed_inputs` 的图断言必填端口被暴露绑定后不报 MISSING_REQUIRED_INPUT；对"锁定轨道"在 docstring 中标注现状（由 apply 期 revision/lock 校验承担）或补充规格化用例。
  - 置信度: 中（exposed_inputs 缺覆盖为事实；"锁定轨道"归属其他层属推断）

### 通过项
- 断言与实现/注册表核对一致（逐项验证）：DAG_CYCLE（Kahn 拓扑，workflow_validator.py:203-230）、UNKNOWN_NODE_SPEC、PORT_TYPE_MISMATCH（asset_set → subtitle_track_ref 确不在 `_COMPATIBLE_TYPES` 允许集）、MISSING_REQUIRED_INPUT（asr 的 assets 端口 required=True）、MISSING_CAPABILITY（asr required_capabilities=("asr",)）、NETWORK_FORBIDDEN（translate network_required=True，node_specs.py:374）、BUDGET_EXCEEDED（tts estimated_cost=0.1 > 预算 0.05，warning 级，node_specs.py:388）、STALE_REVISION 正反两例——fixture 数值与 node_specs.py 实际注册值全部吻合，无虚构 spec/端口。
- 端口引用有效性：cycle 用例的 `summary`/`any` 端口、approval 用例的 `timeline`/`plan` 端口均为真实注册端口，图构造不触发 WorkflowGraph.__post_init__ 的 ghost-edge/dup 校验误炸。
- `ValidationFinding.to_dict` 契约：`ValidationErrorCode.*` 为普通字符串常量（validator.py:50-59），`d["code"] == ValidationErrorCode.DAG_CYCLE` 成立。
- 类型注解：所有测试方法均有 `-> None`，helper 均有返回注解。
- 线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性：不涉及（纯数据模型单测，无 Qt/OpenGL/undo）。
- 异常路径：无 except 吞错；负向期望通过错误码断言而非异常捕获表达。
- 性能模式：每个用例独立构建 registry/graph，规模极小，无热点。
