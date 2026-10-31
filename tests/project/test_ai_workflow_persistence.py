"""AI Workflow 持久化通道往返测试（plan §13：工作流存进项目文件）。

验收：

- ``ToposProjectIO.save`` 接受 ``ai_workflows_data`` 并写入
  ``ai_workflow/workflows.json``；``load`` 读回 ``ai_workflows``。
- Phase B 往返：不传 ``ai_workflows_data`` 的保存不覆盖已有条目；
  显式空存储覆盖旧图（工程边界语义）。
- payload 经 ``WorkflowGraph.to_dict/from_dict`` 往返后 ``content_hash`` 一致。
"""
from __future__ import annotations

import json

import pytest

from src.features.ai_workflow.core.template_registry import (
    WorkflowTemplateRegistry,
    register_official_templates,
)
from src.features.ai_workflow.core.workflow_models import (
    WorkflowGraph,
    parse_workflow_dict,
)
from src.shared.project.topos_project import ToposProject
from src.shared.project.topos_project_io import ToposProjectIO


def _read_zip_json(path: str, arc_name: str) -> dict | None:
    import zipfile
    with zipfile.ZipFile(path, "r") as zf:
        names = zf.namelist()
        if arc_name not in names:
            return None
        return json.loads(zf.read(arc_name))


def _make_project() -> ToposProject:
    return ToposProject.create_single_asset(
        name="WFTest", asset_path="/tmp/test.jpg", media_type="image",
    )


def _make_store() -> dict:
    treg = WorkflowTemplateRegistry()
    register_official_templates(treg)
    graph = treg.instantiate("wf-talking-head-fine-cut")
    return {
        "schema": "topos.workflow-store/1",
        "workflows": [{
            "graph": graph.to_dict(),
            "skill_id": "topos.voiceover_polish",
            "template_id": "wf-talking-head-fine-cut",
        }],
        "active_workflow_id": graph.workflow_id,
    }


class TestWorkflowStoreI0:
    def test_save_writes_workflow_module(self, tmp_path) -> None:
        zf_path = str(tmp_path / "p.topos")
        store = _make_store()
        assert ToposProjectIO.save(_make_project(), zf_path, ai_workflows_data=store)
        payload = _read_zip_json(zf_path, "ai_workflow/workflows.json")
        assert payload == store

    def test_load_reads_workflows_back(self, tmp_path) -> None:
        zf_path = str(tmp_path / "p.topos")
        store = _make_store()
        ToposProjectIO.save(_make_project(), zf_path, ai_workflows_data=store)
        project, full_data = ToposProjectIO.load(zf_path)
        assert project is not None
        assert isinstance(full_data, dict)
        assert full_data["ai_workflows"] == store

    def test_none_keeps_existing_entry(self, tmp_path) -> None:
        """不传 ai_workflows_data 的保存（其他产品）不覆盖已有条目。"""
        zf_path = str(tmp_path / "p.topos")
        store = _make_store()
        ToposProjectIO.save(_make_project(), zf_path, ai_workflows_data=store)
        # 第二次保存不传 → 保留
        ToposProjectIO.save(_make_project(), zf_path)
        payload = _read_zip_json(zf_path, "ai_workflow/workflows.json")
        assert payload == store

    def test_empty_store_overwrites(self, tmp_path) -> None:
        """空存储覆盖旧图：清空画布 → 保存 → 工程里没有残留。"""
        zf_path = str(tmp_path / "p.topos")
        ToposProjectIO.save(
            _make_project(), zf_path, ai_workflows_data=_make_store(),
        )
        empty = {"schema": "topos.workflow-store/1",
                 "workflows": [], "active_workflow_id": ""}
        ToposProjectIO.save(_make_project(), zf_path, ai_workflows_data=empty)
        payload = _read_zip_json(zf_path, "ai_workflow/workflows.json")
        assert payload == empty


class TestGraphRoundtrip:
    def test_store_graph_roundtrip_preserves_hash(self) -> None:
        store = _make_store()
        graph_dict = store["workflows"][0]["graph"]
        restored = parse_workflow_dict(graph_dict)
        original = parse_workflow_dict(graph_dict)
        assert restored.content_hash == original.content_hash
        assert restored.workflow_id == graph_dict["workflow_id"]
        assert len(restored.nodes) == len(graph_dict["nodes"])

    def test_workflow_graph_schema(self) -> None:
        store = _make_store()
        assert store["schema"] == "topos.workflow-store/1"
        graph_dict = store["workflows"][0]["graph"]
        assert graph_dict["schema"] == WorkflowGraph.SCHEMA
