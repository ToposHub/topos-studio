"""P5.2 测试：WorkflowValidator（plan §12.1 单元测试）。

验证 :class:`WorkflowValidator` 的核心契约：

> WorkflowValidator：缺模型、缺权限、云上传、超预算、锁定轨道、stale revision。

检查项（plan §12.1）：

- **DAG 无环**：cycle → error。
- **端口兼容**：edge 的 from/to artifact type 兼容；输入端口必填有连接
  （``exposed_inputs`` 绑定同样满足必填——host 运行时注入）。
- **未知节点 spec**：node.spec_id 不在 registry → error。
- **缺模型**：节点 required_capabilities 不在 host available_capabilities → error。
- **缺权限 / 云上传**：network_required 但 policy.network=forbidden → error。
- **超预算**：节点 estimated_cost 累加超 policy.cost_budget → warning/error。
- **timeline 写**：apply_approved_plan 缺 approval_token 输入 → error（含
  ``topos.user.approval`` 合法链路的正向用例）。
- **stale revision**：外部传入的 revision 与 graph 锚定不一致 → error。
- **锁定轨道（deferred）**：validator 侧暂无对应错误码；该契约由 apply 期
  revision/lock 校验（CommandStack 唯一写入口）承担，见
  ``topos.timeline.apply_approved_plan`` 的 timeline_write_required。

红线（plan §1.4 / §6.4）：approval_token 必须来自 user.approval 节点；
apply 节点不得在没有 token 的情况下执行。
"""
from __future__ import annotations


from src.features.ai_workflow.core.node_specs import (
    NodeSpecRegistry,
    register_official_specs,
)
from src.features.ai_workflow.core.workflow_models import (
    PortRef,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    WorkflowPolicy,
)
from src.features.ai_workflow.core.workflow_validator import (
    ValidationErrorCode,
    ValidationFinding,
    WorkflowValidator,
)


def _registry() -> NodeSpecRegistry:
    r = NodeSpecRegistry()
    register_official_specs(r)
    return r


def _auto_caption_graph() -> WorkflowGraph:
    return WorkflowGraph(
        workflow_id="wf-ac",
        name="自动字幕",
        nodes=(
            WorkflowNode(node_id="input", spec_id="topos.media.selected_assets"),
            WorkflowNode(node_id="asr", spec_id="topos.analysis.transcribe_auto"),
            WorkflowNode(node_id="caption", spec_id="topos.caption.propose_track"),
        ),
        edges=(
            WorkflowEdge(
                from_port=PortRef(node_id="input", port="assets"),
                to_port=PortRef(node_id="asr", port="assets"),
            ),
            WorkflowEdge(
                from_port=PortRef(node_id="asr", port="transcript"),
                to_port=PortRef(node_id="caption", port="transcript"),
            ),
        ),
        policy=WorkflowPolicy(),
    )


# ---------------------------------------------------------------------- #
# 合法 graph
# ---------------------------------------------------------------------- #


class TestValidGraph:
    def test_valid_graph_no_findings(self) -> None:
        v = WorkflowValidator(_registry())
        findings = v.validate(
            _auto_caption_graph(),
            available_capabilities={"asr"},
        )
        errors = [f for f in findings if f.severity == "error"]
        assert errors == [], f"unexpected errors: {errors}"

    def test_findings_severity_levels(self) -> None:
        v = WorkflowValidator(_registry())
        findings = v.validate(_auto_caption_graph(), available_capabilities={"asr"})
        for f in findings:
            assert f.severity in ("error", "warning", "info")


# ---------------------------------------------------------------------- #
# DAG 无环
# ---------------------------------------------------------------------- #


class TestDagAcyclic:
    def test_cycle_detected(self) -> None:
        g = WorkflowGraph(
            workflow_id="wf-cycle", name="x",
            nodes=(
                WorkflowNode(node_id="a", spec_id="topos.report.run_summary"),
                WorkflowNode(node_id="b", spec_id="topos.report.run_summary"),
            ),
            edges=(
                WorkflowEdge(
                    from_port=PortRef(node_id="a", port="summary"),
                    to_port=PortRef(node_id="b", port="any"),
                ),
                WorkflowEdge(
                    from_port=PortRef(node_id="b", port="summary"),
                    to_port=PortRef(node_id="a", port="any"),
                ),
            ),
        )
        v = WorkflowValidator(_registry())
        findings = v.validate(g, available_capabilities=set())
        codes = [f.code for f in findings if f.severity == "error"]
        assert ValidationErrorCode.DAG_CYCLE in codes


# ---------------------------------------------------------------------- #
# 端口兼容
# ---------------------------------------------------------------------- #


class TestPortCompatibility:
    def test_unknown_spec_id(self) -> None:
        g = WorkflowGraph(
            workflow_id="wf", name="x",
            nodes=(WorkflowNode(node_id="n1", spec_id="topos.bogus.node"),),
        )
        v = WorkflowValidator(_registry())
        findings = v.validate(g, available_capabilities=set())
        codes = [f.code for f in findings if f.severity == "error"]
        assert ValidationErrorCode.UNKNOWN_NODE_SPEC in codes

    def test_incompatible_port_types(self) -> None:
        """edge 连接 artifact type 不兼容 → error。"""
        g = WorkflowGraph(
            workflow_id="wf", name="x",
            nodes=(
                WorkflowNode(node_id="input", spec_id="topos.media.selected_assets"),
                WorkflowNode(node_id="tts", spec_id="topos.tts.qwen3"),
            ),
            edges=(
                # assets(asset_set) → tts.subtitle(subtitle_track_ref) 不兼容
                WorkflowEdge(
                    from_port=PortRef(node_id="input", port="assets"),
                    to_port=PortRef(node_id="tts", port="subtitle"),
                ),
            ),
        )
        v = WorkflowValidator(_registry())
        findings = v.validate(g, available_capabilities={"tts"})
        codes = [f.code for f in findings if f.severity == "error"]
        assert ValidationErrorCode.PORT_TYPE_MISMATCH in codes

    def test_missing_required_input(self) -> None:
        """必填输入端口无连接 → error。"""
        # asr 需要 assets 输入，但不连 → error
        g = WorkflowGraph(
            workflow_id="wf", name="x",
            nodes=(WorkflowNode(node_id="asr", spec_id="topos.analysis.transcribe_auto"),),
        )
        v = WorkflowValidator(_registry())
        findings = v.validate(g, available_capabilities={"asr"})
        codes = [f.code for f in findings if f.severity == "error"]
        assert ValidationErrorCode.MISSING_REQUIRED_INPUT in codes

    def test_exposed_inputs_satisfy_required_port(self) -> None:
        """AUD-23806：``exposed_inputs`` 绑定满足必填输入（host 运行时注入）。

        同一图做正反对照：无 exposed 绑定 → MISSING_REQUIRED_INPUT；补上
        exposed 绑定 → 该必填缺口消失（分支可证伪，不是恒真断言）。
        """
        def _graph(*exposed: tuple[str, str, str]) -> WorkflowGraph:
            return WorkflowGraph(
                workflow_id="wf-exposed", name="x",
                nodes=(
                    WorkflowNode(
                        node_id="asr", spec_id="topos.analysis.transcribe_auto",
                    ),
                ),
                exposed_inputs=tuple(exposed),
            )

        v = WorkflowValidator(_registry())

        # 反例（对照）：无 exposed 绑定 → 必填缺口报错
        findings = v.validate(_graph(), available_capabilities={"asr"})
        codes = [f.code for f in findings if f.severity == "error"]
        assert ValidationErrorCode.MISSING_REQUIRED_INPUT in codes

        # 正例：host 声明运行时注入 assets → 必填缺口消失
        findings = v.validate(
            _graph(("host_assets", "asr", "assets")),
            available_capabilities={"asr"},
        )
        codes = [f.code for f in findings if f.severity == "error"]
        assert ValidationErrorCode.MISSING_REQUIRED_INPUT not in codes
        assert codes == [], f"exposed 绑定后仍有 error：{[f.message for f in findings]}"


# ---------------------------------------------------------------------- #
# 缺模型
# ---------------------------------------------------------------------- #


class TestMissingCapabilities:
    def test_missing_capability_error(self) -> None:
        v = WorkflowValidator(_registry())
        findings = v.validate(_auto_caption_graph(), available_capabilities=set())
        codes = [f.code for f in findings if f.severity == "error"]
        assert ValidationErrorCode.MISSING_CAPABILITY in codes

    def test_capability_present_no_error(self) -> None:
        v = WorkflowValidator(_registry())
        findings = v.validate(_auto_caption_graph(), available_capabilities={"asr"})
        codes = [f.code for f in findings if f.severity == "error"]
        assert ValidationErrorCode.MISSING_CAPABILITY not in codes


# ---------------------------------------------------------------------- #
# 缺权限 / 云上传
# ---------------------------------------------------------------------- #


class TestNetworkPermission:
    def test_network_required_but_forbidden(self) -> None:
        """translate 节点 network_required，但 policy.network=forbidden → error。"""
        g = WorkflowGraph(
            workflow_id="wf", name="x",
            nodes=(
                WorkflowNode(node_id="input", spec_id="topos.media.selected_assets"),
                WorkflowNode(node_id="tr", spec_id="topos.text.translate",
                             params={"target_language": "en"}),
            ),
            edges=(
                WorkflowEdge(
                    from_port=PortRef(node_id="input", port="assets"),
                    to_port=PortRef(node_id="tr", port="source"),
                ),
            ),
            policy=WorkflowPolicy(network="forbidden"),
        )
        v = WorkflowValidator(_registry())
        findings = v.validate(g, available_capabilities={"translate"})
        codes = [f.code for f in findings if f.severity == "error"]
        assert ValidationErrorCode.NETWORK_FORBIDDEN in codes


# ---------------------------------------------------------------------- #
# timeline 写需 approval_token
# ---------------------------------------------------------------------- #


class TestTimelineWriteApproval:
    def test_apply_without_approval_token(self) -> None:
        """红线：apply_approved_plan 必须有 approval_token 输入。"""
        g = WorkflowGraph(
            workflow_id="wf", name="x",
            nodes=(
                WorkflowNode(node_id="input", spec_id="topos.timeline.current"),
                WorkflowNode(node_id="apply", spec_id="topos.timeline.apply_approved_plan"),
            ),
            edges=(
                # 只连 timeline，没连 approval_token
                WorkflowEdge(
                    from_port=PortRef(node_id="input", port="timeline"),
                    to_port=PortRef(node_id="apply", port="plan"),  # 类型也不符
                ),
            ),
        )
        v = WorkflowValidator(_registry())
        findings = v.validate(g, available_capabilities=set())
        codes = [f.code for f in findings if f.severity == "error"]
        # AUD-23803：两条断言各自独立（不得用 ``or`` 弱化）——approval_token
        # 红线检查（_check_approval_token）回归时 MISSING_APPROVAL_TOKEN 断言
        # 必须失败；approval 端口无连接的通用必填检查同理单独锁定。
        assert ValidationErrorCode.MISSING_APPROVAL_TOKEN in codes
        assert ValidationErrorCode.MISSING_REQUIRED_INPUT in codes

    def test_apply_with_user_approval_chain_has_no_token_error(self) -> None:
        """AUD-23804 正向用例：token 来自 topos.user.approval 的合法链路。

        propose_track → user.approval → apply：apply 的 approval 端口接上
        ``approval_token`` 输出后，不得再报 MISSING_APPROVAL_TOKEN（防 validator
        假阳性拒绝合法图），且整图零 error。
        """
        g = WorkflowGraph(
            workflow_id="wf-approval-ok", name="approval chain",
            nodes=(
                WorkflowNode(node_id="input", spec_id="topos.media.selected_assets"),
                WorkflowNode(node_id="asr", spec_id="topos.analysis.transcribe_auto"),
                WorkflowNode(node_id="caption", spec_id="topos.caption.propose_track"),
                WorkflowNode(node_id="approval", spec_id="topos.user.approval"),
                WorkflowNode(node_id="apply", spec_id="topos.timeline.apply_approved_plan"),
            ),
            edges=(
                WorkflowEdge(
                    from_port=PortRef(node_id="input", port="assets"),
                    to_port=PortRef(node_id="asr", port="assets"),
                ),
                WorkflowEdge(
                    from_port=PortRef(node_id="asr", port="transcript"),
                    to_port=PortRef(node_id="caption", port="transcript"),
                ),
                WorkflowEdge(
                    from_port=PortRef(node_id="caption", port="plan"),
                    to_port=PortRef(node_id="approval", port="plan"),
                ),
                WorkflowEdge(
                    from_port=PortRef(node_id="caption", port="plan"),
                    to_port=PortRef(node_id="apply", port="plan"),
                ),
                # 红线正向：approval_token 来自 topos.user.approval
                WorkflowEdge(
                    from_port=PortRef(node_id="approval", port="token"),
                    to_port=PortRef(node_id="apply", port="approval"),
                ),
            ),
        )
        v = WorkflowValidator(_registry())
        findings = v.validate(g, available_capabilities={"asr"})
        codes = [f.code for f in findings if f.severity == "error"]
        assert ValidationErrorCode.MISSING_APPROVAL_TOKEN not in codes
        # 合法链路整图零 error（假阳性 = validator 回归）
        assert codes == [], f"合法 approval 链被拒：{[f.message for f in findings]}"


# ---------------------------------------------------------------------- #
# 超预算
# ---------------------------------------------------------------------- #


class TestBudgetExceeded:
    def test_cost_over_budget_warning(self) -> None:
        """累计 estimated_cost 超 cost_budget → warning（不硬拒，提示用户）。"""
        g = WorkflowGraph(
            workflow_id="wf", name="x",
            nodes=(
                WorkflowNode(node_id="tts", spec_id="topos.tts.qwen3"),
            ),
            # tts 默认 estimated_cost=0.1；预算 0.05 → 超
            policy=WorkflowPolicy(cost_budget=0.05),
        )
        v = WorkflowValidator(_registry())
        findings = v.validate(g, available_capabilities={"tts"})
        codes = [f.code for f in findings if f.severity == "warning"]
        assert ValidationErrorCode.BUDGET_EXCEEDED in codes


# ---------------------------------------------------------------------- #
# stale revision
# ---------------------------------------------------------------------- #


class TestStaleRevision:
    def test_stale_revision_error(self) -> None:
        """外部 revision 与 graph 锚定不一致 → error。"""
        v = WorkflowValidator(_registry())
        findings = v.validate(
            _auto_caption_graph(),
            available_capabilities={"asr"},
            current_revision=10,
            expected_revision=42,  # 不一致
        )
        codes = [f.code for f in findings if f.severity == "error"]
        assert ValidationErrorCode.STALE_REVISION in codes

    def test_fresh_revision_no_error(self) -> None:
        v = WorkflowValidator(_registry())
        findings = v.validate(
            _auto_caption_graph(),
            available_capabilities={"asr"},
            current_revision=42,
            expected_revision=42,
        )
        codes = [f.code for f in findings if f.severity == "error"]
        assert ValidationErrorCode.STALE_REVISION not in codes


# ---------------------------------------------------------------------- #
# ValidationFinding / Code
# ---------------------------------------------------------------------- #


class TestValidationFinding:
    def test_finding_serializable(self) -> None:
        f = ValidationFinding(
            severity="error", code=ValidationErrorCode.DAG_CYCLE,
            node_id="a", message="cycle a->b->a",
        )
        d = f.to_dict()
        assert d["severity"] == "error"
        assert d["code"] == ValidationErrorCode.DAG_CYCLE
        assert d["node_id"] == "a"


# ---------------------------------------------------------------------- #
# 端口存在 / allow_multi / params 契约（AUD-23217 / AUD-23218 / AUD-23219）
# ---------------------------------------------------------------------- #


class TestUnknownPort:
    def test_unknown_output_port_errors(self) -> None:
        """AUD-23217：edge 引用不存在的输出端口名 → UNKNOWN_PORT error。"""
        g = WorkflowGraph(
            workflow_id="wf", name="x",
            nodes=(
                WorkflowNode(node_id="input", spec_id="topos.media.selected_assets"),
                WorkflowNode(node_id="asr", spec_id="topos.analysis.transcribe_auto"),
            ),
            edges=(
                WorkflowEdge(
                    # typo：真实输出端口是 "assets"
                    from_port=PortRef(node_id="input", port="asset"),
                    to_port=PortRef(node_id="asr", port="assets"),
                ),
            ),
        )
        v = WorkflowValidator(_registry())
        findings = v.validate(g, available_capabilities={"asr"})
        matches = [f for f in findings if f.severity == "error"
                   and f.code == ValidationErrorCode.UNKNOWN_PORT]
        assert matches, [f.message for f in findings]
        # 错误信息可定位（.clinerules）：node + port 都在 finding 里
        assert matches[0].node_id == "input"
        assert matches[0].port == "asset"
        assert "input.asset" in matches[0].message

    def test_unknown_input_port_errors(self) -> None:
        """AUD-23217：edge 引用不存在的输入端口名 → UNKNOWN_PORT error。"""
        g = WorkflowGraph(
            workflow_id="wf", name="x",
            nodes=(
                WorkflowNode(node_id="input", spec_id="topos.media.selected_assets"),
                WorkflowNode(node_id="asr", spec_id="topos.analysis.transcribe_auto"),
            ),
            edges=(
                WorkflowEdge(
                    from_port=PortRef(node_id="input", port="assets"),
                    # typo：真实输入端口是 "assets"
                    to_port=PortRef(node_id="asr", port="assetz"),
                ),
            ),
        )
        v = WorkflowValidator(_registry())
        findings = v.validate(g, available_capabilities={"asr"})
        matches = [f for f in findings if f.severity == "error"
                   and f.code == ValidationErrorCode.UNKNOWN_PORT]
        assert matches, [f.message for f in findings]
        assert matches[0].node_id == "asr"
        assert matches[0].port == "assetz"

    def test_valid_graph_produces_no_unknown_port(self) -> None:
        v = WorkflowValidator(_registry())
        findings = v.validate(
            _auto_caption_graph(), available_capabilities={"asr"},
        )
        assert all(f.code != ValidationErrorCode.UNKNOWN_PORT for f in findings)


class TestAllowMulti:
    def test_two_edges_into_single_input_error(self) -> None:
        """AUD-23218：allow_multi=False 的输入端口连两条边 → error。"""
        g = WorkflowGraph(
            workflow_id="wf", name="x",
            nodes=(
                WorkflowNode(node_id="input", spec_id="topos.media.selected_assets"),
                WorkflowNode(node_id="cut", spec_id="topos.analysis.scene_cut"),
                # transcribe_auto.assets 声明 allow_multi=False
                WorkflowNode(node_id="asr", spec_id="topos.analysis.transcribe_auto"),
            ),
            edges=(
                WorkflowEdge(
                    from_port=PortRef(node_id="input", port="assets"),
                    to_port=PortRef(node_id="asr", port="assets"),
                ),
                WorkflowEdge(
                    from_port=PortRef(node_id="cut", port="analysis"),
                    to_port=PortRef(node_id="asr", port="assets"),
                ),
            ),
        )
        v = WorkflowValidator(_registry())
        findings = v.validate(g, available_capabilities={"asr", "scene_cut"})
        matches = [f for f in findings if f.severity == "error"
                   and f.code == ValidationErrorCode.MULTI_INPUT_NOT_ALLOWED]
        assert matches, [f.message for f in findings]
        assert matches[0].node_id == "asr"
        assert matches[0].port == "assets"

    def test_planner_analysis_declares_allow_multi(self) -> None:
        """AUD-23206 配套：planner.analysis 声明 allow_multi=True，双分析合法。"""
        spec = _registry().require("topos.agent.rough_cut_plan")
        analysis_port = next(p for p in spec.inputs if p.name == "analysis")
        assert analysis_port.allow_multi is True

    def test_single_edge_into_single_input_ok(self) -> None:
        v = WorkflowValidator(_registry())
        findings = v.validate(
            _auto_caption_graph(), available_capabilities={"asr"},
        )
        assert all(
            f.code != ValidationErrorCode.MULTI_INPUT_NOT_ALLOWED for f in findings
        )


class TestParamsSchema:
    def test_missing_required_param_errors(self) -> None:
        """AUD-23219：translate 声明 required target_language，缺失 → error。"""
        g = WorkflowGraph(
            workflow_id="wf", name="x",
            nodes=(
                WorkflowNode(node_id="input", spec_id="topos.timeline.current"),
                WorkflowNode(node_id="tr", spec_id="topos.text.translate",
                             params={}),  # 缺 target_language
            ),
            edges=(
                WorkflowEdge(
                    from_port=PortRef(node_id="input", port="timeline"),
                    to_port=PortRef(node_id="tr", port="source"),
                ),
            ),
        )
        v = WorkflowValidator(_registry())
        findings = v.validate(g, available_capabilities={"translate"})
        matches = [f for f in findings if f.severity == "error"
                   and f.code == ValidationErrorCode.MISSING_REQUIRED_PARAM]
        assert matches, [f.message for f in findings]
        assert matches[0].node_id == "tr"
        assert "target_language" in matches[0].message

    def test_param_type_mismatch_errors(self) -> None:
        """AUD-23219：schema 声明 string，传入 int → INVALID_PARAM_TYPE error。"""
        g = WorkflowGraph(
            workflow_id="wf", name="x",
            nodes=(
                WorkflowNode(node_id="tr", spec_id="topos.text.translate",
                             params={"target_language": 3}),
            ),
        )
        v = WorkflowValidator(_registry())
        findings = v.validate(g, available_capabilities={"translate"})
        matches = [f for f in findings if f.severity == "error"
                   and f.code == ValidationErrorCode.INVALID_PARAM_TYPE]
        assert matches, [f.message for f in findings]
        assert matches[0].node_id == "tr"
        assert "target_language" in matches[0].message

    def test_unknown_param_warns(self) -> None:
        """AUD-23219：schema 外参数 → UNKNOWN_PARAM warning（不阻断编译）。"""
        g = WorkflowGraph(
            workflow_id="wf", name="x",
            nodes=(
                WorkflowNode(node_id="input", spec_id="topos.media.selected_assets",
                             params={"media_kind": "video", "bogus_key": 1}),
            ),
        )
        v = WorkflowValidator(_registry())
        findings = v.validate(g, available_capabilities={"asr"})
        warnings = [f for f in findings if f.severity == "warning"
                    and f.code == ValidationErrorCode.UNKNOWN_PARAM]
        assert warnings, [f.message for f in findings]
        assert warnings[0].node_id == "input"
        assert "bogus_key" in warnings[0].message
        # warning 不升级为 error
        assert not [
            f for f in findings if f.severity == "error"
            and f.code == ValidationErrorCode.UNKNOWN_PARAM
        ]

    def test_declared_params_pass(self) -> None:
        """官方模板真实参数（media_kind / target_duration）合法（schema 已补齐）。"""
        g = WorkflowGraph(
            workflow_id="wf", name="x",
            nodes=(
                WorkflowNode(
                    node_id="input", spec_id="topos.media.selected_assets",
                    params={"media_kind": "video"},
                ),
                WorkflowNode(
                    node_id="planner", spec_id="topos.agent.rough_cut_plan",
                    params={"target_duration": 90},
                ),
            ),
            edges=(
                WorkflowEdge(
                    from_port=PortRef(node_id="input", port="assets"),
                    to_port=PortRef(node_id="planner", port="assets"),
                ),
            ),
        )
        v = WorkflowValidator(_registry())
        findings = v.validate(g, available_capabilities={"llm"})
        errors = [f for f in findings if f.severity == "error"]
        assert not errors, [f.message for f in errors]
