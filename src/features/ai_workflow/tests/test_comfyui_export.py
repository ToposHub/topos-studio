"""P7.1 测试：ComfyUI Workflow 导出（plan §9.4 #8 / §1.4.2 C2 闭环）。

> 提供"导出回 ComfyUI API format"，但仅对 ``ComfyUI Workflow`` 子图及其绑定参数保证等价；
> Topos 原生 Agent、审批、时间线和 MCP 节点导出时生成注释/占位，不伪装成可直接执行的
> Comfy 节点。

验证 :class:`ComfyWorkflowExporter` 的核心契约（plan §9.4 #8）：

- 从 :class:`ComfyWorkflowSnapshot` 导出回 ComfyUI API-format JSON。
- 应用绑定后的 payload 与原 snapshot 的 ``raw_payload`` 结构等价（round-trip）。
- 混合图（Comfy 子图 + Topos 原生节点）导出时：
    - Comfy 子图节点 → 真实 API-format（保证等价）。
    - Topos 原生节点（Agent / 审批 / 时间线 / MCP）→ **注释/占位**，**不**伪装成 Comfy 节点。
- 导出 JSON **不**含 API key / credential 原文（红线 §0 第四条）。
- 导出 JSON 含 workflow_snapshot_hash + schema_version（可追溯）。

红线（plan §9.4 #8 / §0 / §1.4.2）：

- 仅对 ``ComfyUI Workflow`` 子图保证等价；原生节点**不**伪装成可执行 Comfy 节点。
- 不写 API key / credential 到 workflow JSON。
- 导出是**只读**操作；不联网 / 不执行任何东西。
"""
from __future__ import annotations

import json
from typing import Any


from src.features.ai_workflow.adapters.comfyui_export import (
    ComfyPlaceholderKind,
    ComfyWorkflowExporter,
    ExportResult,
    NativeNodePlaceholder,
)
from src.features.ai_workflow.adapters.comfyui_importer import ComfyWorkflowImporter
from src.features.ai_workflow.adapters.comfyui_models import (
    ComfyInputBinding,
    ComfyInputSlot,
    ComfyWorkflowSnapshot,
)


# ---------------------------------------------------------------------- #
# Fixtures
# ---------------------------------------------------------------------- #


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


def _imported_snapshot() -> ComfyWorkflowSnapshot:
    return ComfyWorkflowImporter().import_from_dict(
        "wf-t2i", _sdxl_t2i_workflow(), display_name="SDXL T2I",
    )


# ---------------------------------------------------------------------- #
# Round-trip：snapshot → export → snapshot
# ---------------------------------------------------------------------- #


class TestComfyExportRoundTrip:
    def test_export_snapshot_returns_api_format_json(self) -> None:
        snap = _imported_snapshot()
        exporter = ComfyWorkflowExporter()
        result = exporter.export_snapshot(snap)
        assert isinstance(result, ExportResult)
        payload = json.loads(result.api_format_json)
        # 结构：node_id -> {class_type, inputs}
        assert isinstance(payload, dict)
        assert "3" in payload
        assert payload["3"]["class_type"] == "KSampler"

    def test_exported_payload_structure_matches_original(self) -> None:
        snap = _imported_snapshot()
        exporter = ComfyWorkflowExporter()
        result = exporter.export_snapshot(snap)
        payload = json.loads(result.api_format_json)
        # 节点数等价（排除 _topos_meta 元数据 key）
        node_keys = {k for k in payload if k != "_topos_meta"}
        assert len(node_keys) == len(_sdxl_t2i_workflow())
        # 每个 class_type 一致
        for nid, orig in _sdxl_t2i_workflow().items():
            assert payload[nid]["class_type"] == orig["class_type"]

    def test_exported_json_includes_workflow_hash(self) -> None:
        snap = _imported_snapshot()
        exporter = ComfyWorkflowExporter()
        result = exporter.export_snapshot(snap)
        # 导出 JSON 含 _topos_meta（hash + schema_version）
        payload = json.loads(result.api_format_json)
        assert "_topos_meta" in payload
        meta = payload["_topos_meta"]
        assert meta["workflow_snapshot_hash"] == snap.content_hash
        assert meta["schema_version"] == "1"

    def test_export_applies_bindings_into_payload(self) -> None:
        # 红线：导出时绑定参数应写入 payload
        snap = _imported_snapshot()
        slot = ComfyInputSlot(
            slot_id="seed", node_id="3", input_name="seed",
            artifact_type="integer",
        )
        binding = ComfyInputBinding(slot=slot, resolved_value="99999")
        exporter = ComfyWorkflowExporter()
        result = exporter.export_snapshot(snap, bindings=(binding,))
        payload = json.loads(result.api_format_json)
        # seed 应被改写为 99999
        assert payload["3"]["inputs"]["seed"] == 99999

    def test_export_is_readonly_no_network(self, monkeypatch) -> None:
        """红线：导出不联网 / 不执行 / 幂等（AUD-23531）。

        旧版只断言两次导出结果一致（确定性≠无网络）；现把 socket/urllib 哨兵
        化——导出路径若触碰任何网络通道，哨兵抛错使红线可证伪。
        """
        import socket
        import urllib.request

        def _no_network(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("红线被破坏：导出触碰了网络（必须只读、离线）")

        monkeypatch.setattr(socket, "socket", _no_network)
        monkeypatch.setattr(urllib.request, "urlopen", _no_network)

        snap = _imported_snapshot()
        exporter = ComfyWorkflowExporter()
        # 多次导出无副作用
        r1 = exporter.export_snapshot(snap)
        r2 = exporter.export_snapshot(snap)
        assert r1.api_format_json == r2.api_format_json

    def test_exported_json_has_no_credentials(self) -> None:
        """红线（§0 第四条）：不写 API key / credential（AUD-23530）。

        旧版输入 fixture 本身不含任何凭据，红线空转——exporter 即使把凭据
        原样带出也照样通过。现构造**含凭据形状字段**的 workflow 参与导出路径：
        exporter 必须剥掉 credential 键与值，且 ``_topos_meta`` 键集合封闭。
        """
        raw = _sdxl_t2i_workflow()
        secret_value = "sk-live-SUPER-secret-42"
        # 注入凭据形状字段（节点 inputs 内，递归清除路径必须覆盖）
        raw["4"]["inputs"]["api_key"] = secret_value
        raw["9"]["inputs"]["authorization"] = f"Bearer {secret_value}"
        raw["9"]["inputs"]["credential_ref"] = "topos:cred/cloud/1"
        snap = ComfyWorkflowImporter().import_from_dict("wf-cred", raw)

        exporter = ComfyWorkflowExporter()
        result = exporter.export_snapshot(snap)
        low = result.api_format_json.lower()
        # 值本身不得出现（键被剥掉后值自然也不在）
        assert secret_value not in result.api_format_json
        for bad in ("api_key", "apikey", "secret", "password", "credential_ref",
                    "bearer", "authorization"):
            assert bad not in low, f"exported JSON leaks {bad!r}"
        # _topos_meta 键集合封闭（白名单）——元数据不能成为凭据夹带通道
        payload = json.loads(result.api_format_json)
        meta = payload["_topos_meta"]
        assert set(meta) <= {
            "workflow_snapshot_hash", "schema_version",
            "workflow_id", "display_name", "exported_at_kind",
        }
        # 剥离凭据后其余字段完好（round-trip 等价不被破坏）
        assert payload["3"]["class_type"] == "KSampler"
        assert payload["3"]["inputs"]["seed"] == 12345


# ---------------------------------------------------------------------- #
# NativeNodePlaceholder —— Topos 原生节点导出为占位（§9.4 #8）
# ---------------------------------------------------------------------- #


class TestNativeNodePlaceholder:
    def test_placeholder_kind_for_native_specs(self) -> None:
        # 红线（§9.4 #8）：Topos 原生节点（Agent / 审批 / 时间线 / MCP）导出为占位
        assert ComfyPlaceholderKind.for_spec_id("topos.agent.rough_cut_plan") == ComfyPlaceholderKind.AGENT
        assert ComfyPlaceholderKind.for_spec_id("topos.user.approval") == ComfyPlaceholderKind.APPROVAL
        assert ComfyPlaceholderKind.for_spec_id("topos.timeline.apply_approved_plan") == ComfyPlaceholderKind.TIMELINE
        # MCP（约定 spec_id 以 topos.mcp 开头）
        assert ComfyPlaceholderKind.for_spec_id("topos.mcp.outbound") == ComfyPlaceholderKind.MCP

    def test_placeholder_kind_for_comfy_spec_is_none(self) -> None:
        # ComfyUI Workflow 子图节点 → 不需要占位（直接导出真实 API-format）
        assert ComfyPlaceholderKind.for_spec_id("topos.comfy.workflow") == ComfyPlaceholderKind.NONE
        # 其他生成节点也不是占位（它们本身是 Comfy/provider 入口）
        # 但作为独立 Topos 节点导出时仍需占位（因为不是 Comfy 节点）
        # 这里只验证 comfy.workflow 是 NONE（它本身导出 API-format）

    def test_placeholder_construction(self) -> None:
        ph = NativeNodePlaceholder(
            topos_node_id="n_approval",
            topos_spec_id="topos.user.approval",
            kind=ComfyPlaceholderKind.APPROVAL,
            note="User Approval — not executable in ComfyUI",
        )
        assert ph.topos_node_id == "n_approval"
        assert ph.kind == ComfyPlaceholderKind.APPROVAL
        assert "not executable" in ph.note.lower()

    def test_placeholder_to_dict_does_not_fake_comfy_node(self) -> None:
        # 红线（§9.4 #8）：占位**不**伪装成可执行 Comfy 节点
        ph = NativeNodePlaceholder(
            topos_node_id="n1", topos_spec_id="topos.user.approval",
            kind=ComfyPlaceholderKind.APPROVAL, note="x",
        )
        d = ph.to_comfy_dict()
        # 占位节点用 class_type="_ToposNativePlaceholder"（下划线前缀，Comfy 不执行）
        assert d["class_type"].startswith("_")
        assert "_topos" in d["class_type"].lower()
        # inputs 里含原 spec_id + note，但不伪装成真实 Comfy 节点
        assert d["inputs"]["topos_spec_id"] == "topos.user.approval"
        assert "topos_node_id" in d["inputs"]


# ---------------------------------------------------------------------- #
# 混合图导出：Comfy 子图 + 原生节点
# ---------------------------------------------------------------------- #


class TestMixedGraphExport:
    def test_export_mixed_graph_keeps_comfy_and_placeholders_native(self) -> None:
        # 模拟混合图：Comfy 子图（wf-t2i）+ 一个 Topos 审批节点
        snap = _imported_snapshot()
        native_placeholders = (
            NativeNodePlaceholder(
                topos_node_id="n_approval",
                topos_spec_id="topos.user.approval",
                kind=ComfyPlaceholderKind.APPROVAL,
                note="user approval required",
            ),
        )
        exporter = ComfyWorkflowExporter()
        result = exporter.export_with_native_nodes(
            snapshot=snap, native_placeholders=native_placeholders,
        )
        payload = json.loads(result.api_format_json)

        # Comfy 子图节点（3-9）保留真实 class_type
        for nid in ("3", "4", "5", "6", "7", "8", "9"):
            assert payload[nid]["class_type"] == _sdxl_t2i_workflow()[nid]["class_type"]

        # 原生审批节点作为占位出现，class_type 以 _Topos 开头
        approval_keys = [k for k, v in payload.items()
                         if v.get("class_type", "").startswith("_Topos")]
        assert len(approval_keys) >= 1
        ap = payload[approval_keys[0]]
        assert ap["inputs"]["topos_spec_id"] == "topos.user.approval"
        # 不应有真实 Comfy class_type
        assert "SaveImage" not in ap["class_type"]

    def test_export_mixed_graph_lists_skipped_native_nodes(self) -> None:
        snap = _imported_snapshot()
        native_placeholders = (
            NativeNodePlaceholder(
                topos_node_id="n1", topos_spec_id="topos.agent.rough_cut_plan",
                kind=ComfyPlaceholderKind.AGENT, note="rough cut agent",
            ),
            NativeNodePlaceholder(
                topos_node_id="n2", topos_spec_id="topos.timeline.apply_approved_plan",
                kind=ComfyPlaceholderKind.TIMELINE, note="timeline apply",
            ),
        )
        exporter = ComfyWorkflowExporter()
        result = exporter.export_with_native_nodes(
            snapshot=snap, native_placeholders=native_placeholders,
        )
        # 导出结果应记录哪些节点被占位化
        assert len(result.skipped_native_specs) == 2
        assert "topos.agent.rough_cut_plan" in result.skipped_native_specs
        assert "topos.timeline.apply_approved_plan" in result.skipped_native_specs

    def test_export_warns_about_equivalence_limitation(self) -> None:
        # 红线（§9.4 #8）：混合图导出**不**保证等价；应有明确 warning
        snap = _imported_snapshot()
        native_placeholders = (
            NativeNodePlaceholder(
                topos_node_id="n1", topos_spec_id="topos.user.approval",
                kind=ComfyPlaceholderKind.APPROVAL, note="x",
            ),
        )
        exporter = ComfyWorkflowExporter()
        result = exporter.export_with_native_nodes(
            snapshot=snap, native_placeholders=native_placeholders,
        )
        # 有原生占位 → equivalence_guaranteed=False
        assert result.equivalence_guaranteed is False
        assert len(result.warnings) >= 1
        # warning 应说明"原生节点不可直接执行"
        warn_text = " ".join(result.warnings).lower()
        assert "native" in warn_text or "not executable" in warn_text

    def test_pure_comfy_export_guarantees_equivalence(self) -> None:
        # 纯 Comfy 子图导出 → equivalence_guaranteed=True
        snap = _imported_snapshot()
        exporter = ComfyWorkflowExporter()
        result = exporter.export_snapshot(snap)
        assert result.equivalence_guaranteed is True
        assert len(result.warnings) == 0


# ---------------------------------------------------------------------- #
# 导出文件名 / 元数据
# ---------------------------------------------------------------------- #


class TestExportMetadata:
    def test_export_result_has_filename(self) -> None:
        snap = _imported_snapshot()
        exporter = ComfyWorkflowExporter()
        result = exporter.export_snapshot(snap)
        assert result.suggested_filename.endswith(".json")
        assert "wf-t2i" in result.suggested_filename or "t2i" in result.suggested_filename.lower()

    def test_export_result_serializable(self) -> None:
        snap = _imported_snapshot()
        exporter = ComfyWorkflowExporter()
        result = exporter.export_snapshot(snap)
        d = result.to_dict()
        json.dumps(d)  # 可序列化
        assert "api_format_json" in d
