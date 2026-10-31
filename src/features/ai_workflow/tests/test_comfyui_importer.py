"""P6.2 测试：ComfyUI 只读 importer + preflight gate（plan §14.1 step 3 / §9.4）。

> 实现只读 Comfy workflow importer：输出节点/模型/自定义节点依赖报告；禁止导入时
> 执行任何 custom node。

验证 importer 的核心契约：

- 从 ComfyUI API-format JSON / 文件路径构造 :class:`ComfyWorkflowSnapshot`。
- 产出 :class:`ComfyImportReport`：节点清单 / 模型依赖 / 自定义节点依赖 / 缺失依赖。
- preflight gate（:class:`ComfyPreflightChecker`）：检查 Comfy server 是否具备 workflow
  所需节点/模型；缺失 → 诊断 ERROR，**禁止执行**（§9.4 #8 "在执行前 BLOCKED"）。
- 缺少 class_type / custom node / 模型 / 参数绑定时在执行前 BLOCKED。

红线（plan §14.1 step 3 / §9.4 / §1.4.2）：

- importer **不执行**任何 custom node 代码（只读解析）。
- 缺失依赖 → preflight 报 ERROR；不允许"先跑再说"。
- importer 不联网（不连 Comfy server）；preflight 的 server 状态由调用方注入
  （``ComfyServerCapabilities`` DTO）。
- 用户绑定未填的必填 slot → preflight 报 ERROR。
- preflight gate 不替代 consent gate（生成仍需用户确认上传范围 + 成本）。
"""
from __future__ import annotations

import json

import pytest

from src.features.ai_workflow.adapters.comfyui_importer import (
    ComfyImportReport,
    ComfyPreflightChecker,
    ComfyServerCapabilities,
    ComfyWorkflowImporter,
    PreflightDenyCode,
)
from src.features.ai_workflow.adapters.comfyui_models import (
    ComfyWorkflowSnapshot,
)


def _sdxl_t2i_workflow() -> dict:
    return {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": 12345, "cfg": 7.0,
            "model": ["4", 0], "positive": ["6", 0],
            "negative": ["7", 0], "latent_image": ["5", 0],
        }},
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {
            "ckpt_name": "sd_xl_base_1.0.safetensors",
        }},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a cat", "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "topos"}},
    }


# ---------------------------------------------------------------------- #
# ComfyWorkflowImporter
# ---------------------------------------------------------------------- #


class TestComfyWorkflowImporter:
    def test_import_from_dict_returns_snapshot(self) -> None:
        imp = ComfyWorkflowImporter()
        snap = imp.import_from_dict(
            workflow_id="wf-t2i",
            raw=_sdxl_t2i_workflow(),
            display_name="SDXL T2I",
        )
        assert isinstance(snap, ComfyWorkflowSnapshot)
        assert snap.workflow_id == "wf-t2i"
        assert snap.display_name == "SDXL T2I"
        assert snap.node_count == 7

    def test_import_returns_report_with_dependency_summary(self) -> None:
        imp = ComfyWorkflowImporter()
        report = imp.import_and_report(
            workflow_id="wf-t2i",
            raw=_sdxl_t2i_workflow(),
            display_name="SDXL T2I",
        )
        assert isinstance(report, ComfyImportReport)
        assert report.snapshot is not None
        # 模型依赖至少有 1 个 checkpoint
        assert any(m.model_type == "checkpoint" for m in report.required_models)
        # 节点清单
        assert report.node_count == 7
        # 至少有 1 个输出节点
        assert report.output_node_count >= 1

    def test_import_detects_unknown_custom_nodes(self) -> None:
        imp = ComfyWorkflowImporter()
        raw = _sdxl_t2i_workflow()
        raw["999"] = {"class_type": "SomeObscureCustomNode", "inputs": {"x": 1}}
        report = imp.import_and_report("wf", raw)
        # 未知 class_type → 自定义节点依赖
        assert report.unknown_custom_node_count == 1
        # 报告里应列出
        assert any(c.display_name == "SomeObscureCustomNode"
                   for c in report.snapshot.required_custom_nodes)

    def test_import_does_not_execute_custom_nodes(self) -> None:
        # 红线：导入即使遇到"危险" class_type 也不执行任何东西
        imp = ComfyWorkflowImporter()
        raw = _sdxl_t2i_workflow()
        raw["666"] = {"class_type": "EvalNode", "inputs": {"code": "import os; os.system('rm -rf /')"}}
        report = imp.import_and_report("wf", raw)
        # 导入成功；EvalNode 仅作为元数据出现
        assert report.snapshot is not None
        assert any(n.class_type == "EvalNode" for n in report.snapshot.nodes)

    def test_import_invalid_dict_raises(self) -> None:
        imp = ComfyWorkflowImporter()
        with pytest.raises(ValueError):
            imp.import_from_dict("wf", "not a dict")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            imp.import_from_dict("wf", {})  # 空
        with pytest.raises(ValueError):
            imp.import_from_dict("wf", {"3": {"inputs": {}}})  # 无 class_type

    def test_import_from_json_string(self) -> None:
        imp = ComfyWorkflowImporter()
        s = json.dumps(_sdxl_t2i_workflow())
        snap = imp.import_from_json_string("wf", s, display_name="from json")
        assert snap.node_count == 7

    def test_import_from_json_string_rejects_non_object(self) -> None:
        imp = ComfyWorkflowImporter()
        with pytest.raises(ValueError):
            imp.import_from_json_string("wf", "[1,2,3]")  # 数组不是 workflow

    def test_report_to_dict_serializable(self) -> None:
        imp = ComfyWorkflowImporter()
        report = imp.import_and_report("wf", _sdxl_t2i_workflow(), display_name="T")
        d = report.to_dict()
        json.dumps(d)  # 不应抛
        assert "snapshot" in d
        assert "required_models" in d
        assert d["node_count"] == 7


# ---------------------------------------------------------------------- #
# ComfyServerCapabilities
# ---------------------------------------------------------------------- #


class TestComfyServerCapabilities:
    def test_capabilities_basic(self) -> None:
        caps = ComfyServerCapabilities(
            profile_id="local-default",
            available_class_types=frozenset({"KSampler", "CheckpointLoaderSimple"}),
            available_models=frozenset({"sd_xl_base_1.0.safetensors"}),
            custom_node_repos=frozenset(),  # 无 custom node
        )
        assert caps.profile_id == "local-default"
        assert "KSampler" in caps.available_class_types
        assert caps.has_class_type("KSampler") is True
        assert caps.has_class_type("UnknownNode") is False
        assert caps.has_model("sd_xl_base_1.0.safetensors") is True

    def test_capabilities_unknown_custom_node_count(self) -> None:
        caps = ComfyServerCapabilities(
            profile_id="local",
            available_class_types=frozenset({"KSampler", "MyCustom"}),
            available_models=frozenset(),
            custom_node_repos=frozenset({"https://github.com/x/y"}),
        )
        # has_class_type True 时，即使不在白名单也算"server 提供"
        assert caps.has_class_type("MyCustom") is True


# ---------------------------------------------------------------------- #
# ComfyPreflightChecker —— 执行前 gate
# ---------------------------------------------------------------------- #


class TestComfyPreflightChecker:
    def test_preflight_passes_when_all_deps_met(self) -> None:
        imp = ComfyWorkflowImporter()
        report = imp.import_and_report("wf", _sdxl_t2i_workflow())
        caps = ComfyServerCapabilities(
            profile_id="local-default",
            available_class_types=frozenset({n.class_type for n in report.snapshot.nodes}),
            available_models=frozenset({m.model_name for m in report.required_models}),
            custom_node_repos=frozenset(),
        )
        checker = ComfyPreflightChecker()
        result = checker.check(report, capabilities=caps)
        assert result.allowed is True
        assert result.deny_code is None
        assert not result.has_blocking_errors

    def test_preflight_blocks_missing_class_type(self) -> None:
        imp = ComfyWorkflowImporter()
        report = imp.import_and_report("wf", _sdxl_t2i_workflow())
        # server 缺一个 class_type
        caps = ComfyServerCapabilities(
            profile_id="local",
            available_class_types=frozenset({"KSampler"}),  # 缺大多数
            available_models=frozenset({m.model_name for m in report.required_models}),
            custom_node_repos=frozenset(),
        )
        checker = ComfyPreflightChecker()
        result = checker.check(report, capabilities=caps)
        assert result.allowed is False
        assert result.deny_code == PreflightDenyCode.MISSING_NODE
        assert result.has_blocking_errors

    def test_preflight_blocks_missing_model(self) -> None:
        imp = ComfyWorkflowImporter()
        report = imp.import_and_report("wf", _sdxl_t2i_workflow())
        caps = ComfyServerCapabilities(
            profile_id="local",
            available_class_types=frozenset({n.class_type for n in report.snapshot.nodes}),
            available_models=frozenset(),  # 缺所有模型
            custom_node_repos=frozenset(),
        )
        result = ComfyPreflightChecker().check(report, capabilities=caps)
        assert result.allowed is False
        assert result.deny_code == PreflightDenyCode.MISSING_MODEL

    def test_preflight_does_not_auto_download(self) -> None:
        # 红线（§13.7）：缺失模型时只报错，不触发下载
        imp = ComfyWorkflowImporter()
        report = imp.import_and_report("wf", _sdxl_t2i_workflow())
        caps = ComfyServerCapabilities(
            profile_id="local",
            available_class_types=frozenset({n.class_type for n in report.snapshot.nodes}),
            available_models=frozenset(),
            custom_node_repos=frozenset(),
        )
        result = ComfyPreflightChecker().check(report, capabilities=caps)
        assert result.allowed is False
        # 建议动作必须要求用户**手动**放置文件，不得暗示 Topos 会代为下载
        for d in result.diagnostics:
            if d.code == PreflightDenyCode.MISSING_MODEL and d.suggested_action:
                low = d.suggested_action.lower()
                assert "manually" in low or "your comfyui" in low
                # 不应出现 "topos will download" 这类暗示代下载的措辞
                assert "topos will download" not in low
                assert "auto download" not in low
                assert "auto-download" not in low

    def test_preflight_blocks_missing_required_bindings(self) -> None:
        imp = ComfyWorkflowImporter()
        report = imp.import_and_report("wf", _sdxl_t2i_workflow())
        caps = ComfyServerCapabilities(
            profile_id="local",
            available_class_types=frozenset({n.class_type for n in report.snapshot.nodes}),
            available_models=frozenset({m.model_name for m in report.required_models}),
            custom_node_repos=frozenset(),
        )
        # 空必填 binding 应在构造时报错（required + 空）→ 不能用，所以我们改用
        # report.required_bindings 为空 + required_slot_ids 非空的形式
        # 改测：preflight 接受 required_unbound_slots 列表
        report2 = ComfyImportReport(
            snapshot=report.snapshot,
            required_unbound_slot_ids=("must_bind",),
        )
        result = ComfyPreflightChecker().check(report2, capabilities=caps)
        assert result.allowed is False
        assert result.deny_code == PreflightDenyCode.MISSING_BINDING

    def test_preflight_blocks_unknown_custom_node(self) -> None:
        imp = ComfyWorkflowImporter()
        raw = _sdxl_t2i_workflow()
        raw["999"] = {"class_type": "SomeCustom", "inputs": {}}
        report = imp.import_and_report("wf", raw)
        # server 没有这个 class_type
        caps = ComfyServerCapabilities(
            profile_id="local",
            available_class_types=frozenset({n.class_type for n in report.snapshot.nodes if n.class_type != "SomeCustom"}),
            available_models=frozenset({m.model_name for m in report.required_models}),
            custom_node_repos=frozenset(),
        )
        result = ComfyPreflightChecker().check(report, capabilities=caps)
        assert result.allowed is False
        # class_type 缺失优先报 MISSING_NODE
        assert result.deny_code in (PreflightDenyCode.MISSING_NODE, PreflightDenyCode.MISSING_CUSTOM_NODE)

    def test_preflight_blocks_when_custom_node_unpinned(self) -> None:
        # 红线（§13.7）：未 pin commit 的 custom node 默认拒绝（用户需显式接受）
        imp = ComfyWorkflowImporter()
        raw = _sdxl_t2i_workflow()
        raw["999"] = {"class_type": "SomeCustom", "inputs": {}}
        report = imp.import_and_report("wf", raw)
        caps = ComfyServerCapabilities(
            profile_id="local",
            available_class_types=frozenset({n.class_type for n in report.snapshot.nodes}),
            available_models=frozenset({m.model_name for m in report.required_models}),
            custom_node_repos=frozenset({"https://github.com/x/SomeCustom"}),  # 提供 repo 但无 commit
        )
        checker = ComfyPreflightChecker(require_pinned_custom_nodes=True)
        result = checker.check(report, capabilities=caps)
        assert result.allowed is False
        assert result.deny_code == PreflightDenyCode.UNPINNED_CUSTOM_NODE

    def test_preflight_result_to_dict(self) -> None:
        imp = ComfyWorkflowImporter()
        report = imp.import_and_report("wf", _sdxl_t2i_workflow())
        caps = ComfyServerCapabilities(
            profile_id="local",
            available_class_types=frozenset({n.class_type for n in report.snapshot.nodes}),
            available_models=frozenset({m.model_name for m in report.required_models}),
            custom_node_repos=frozenset(),
        )
        result = ComfyPreflightChecker().check(report, capabilities=caps)
        d = result.to_dict()
        assert d["allowed"] is True
        assert "diagnostics" in d
        s = json.dumps(d)  # 可序列化
        assert "allowed" in s


# ---------------------------------------------------------------------- #
# 集成：importer + preflight 端到端
# ---------------------------------------------------------------------- #


class TestImportAndPreflightE2E:
    def test_full_pipeline_blocks_unsafe_workflow_before_submit(self) -> None:
        # 红线（§9.4 #8）：导入 ComfyUI API workflow 后，缺少 class_type/custom node/
        # 模型/参数绑定时在执行前 BLOCKED
        imp = ComfyWorkflowImporter()
        raw = _sdxl_t2i_workflow()
        raw["999"] = {"class_type": "MysteryNode", "inputs": {}}
        report = imp.import_and_report("wf", raw)
        # server 缺 MysteryNode
        caps = ComfyServerCapabilities(
            profile_id="local",
            available_class_types=frozenset({n.class_type for n in report.snapshot.nodes if n.class_type != "MysteryNode"}),
            available_models=frozenset({m.model_name for m in report.required_models}),
            custom_node_repos=frozenset(),
        )
        result = ComfyPreflightChecker().check(report, capabilities=caps)
        assert result.allowed is False
        # 没有任何"已提交"的字段
        assert not hasattr(result, "submitted") or not getattr(result, "submitted", False)

    def test_preflight_denial_gives_callers_gate_info_before_jobref(self) -> None:
        """preflight 拒绝结果必须携带可判别的 gate 信息（调用方据此不建 JobRef）。

        AUD-23541：旧版为死分支（``if result.allowed:`` 紧跟 ``allowed is
        False`` 断言，永不可达）+ 同义反复断言（``not hasattr(...) or not
        getattr(...)`` 属性不存在时恒真），验证力为零。``ComfyJobRef`` 本身是
        无 gate 的数据类，构造 gate 由调用方承担——executor 侧的强制点由
        ``test_comfyui_executor.py::test_submit_requires_preflight_pass``
        （PREFLIGHT_FAILED）独立锁定。本测试锁定调用方可依赖的事实：拒绝
        必须 ``allowed is False`` 且 ``deny_code`` 明确（缺 class_type 优先报
        MISSING_NODE），不留下"半允许"歧义态。
        """
        imp = ComfyWorkflowImporter()
        report = imp.import_and_report("wf", _sdxl_t2i_workflow())
        caps = ComfyServerCapabilities(
            profile_id="local",
            available_class_types=frozenset(),
            available_models=frozenset(),
            custom_node_repos=frozenset(),
        )
        result = ComfyPreflightChecker().check(report, capabilities=caps)
        # gate 信息明确：拒绝 + 可判别 deny_code（调用方据此不创建 JobRef）
        assert result.allowed is False
        assert result.deny_code == PreflightDenyCode.MISSING_NODE
        # deny 结果可序列化传递给调用方（UI/日志消费）
        d = result.to_dict()
        assert d["allowed"] is False
        assert d["deny_code"] == PreflightDenyCode.MISSING_NODE
