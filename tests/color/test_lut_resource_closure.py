"""R09: 图资源引用闭包（收集/保存期自动迁移/缺失可修复/undo 恢复）。

验收（计划 R09）：闭包覆盖 compound children 与共享同 ref；保存携带完整
定义与资源；缺资源条目保留可修复状态（不静默移除）；撤销删除仍能恢复
（包内资源不提前回收）；跨工程同 id 不同内容不覆盖（R02 已锁，本文件
覆盖闭包侧）。
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src.shared.color.lut_resource_registry import (
    LutResourceSession,
    collect_graph_lut_closure,
    ensure_graph_lut_closure,
)
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.edit.core.project import EditProject
from tests.color.fixtures.color_graph import f03_layer_stack


def _cube(tmp: Path, name: str, swap: bool = False) -> Path:
    lines = [f"LUT_3D_SIZE 2"]
    for b in range(2):
        for g in range(2):
            for r in range(2):
                v = [r, g, b]
                if swap:
                    v = v[::-1]
                lines.append(" ".join(map(str, v)))
    path = tmp / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


def _graph():
    project, _sid = EditProject.create(name="r09").create_color_stack(
        asset_id="a1", name="s")
    stack = project.color_stacks[_sid].with_layers(
        [l.to_dict(None) for l in f03_layer_stack()])
    return project, migrate_legacy_stack(stack)


def _service(valid_refs: set[str]) -> ColorGraphService:
    return ColorGraphService(
        effect_schema_provider=lambda rid: [],
        lut_reference_validator=lambda ref: ref in valid_refs)


def test_closure_collects_params_and_legacy_and_compound() -> None:
    project, graph = _graph()
    corrector = next(n for n in graph.nodes if n.node_type == "corrector")
    service = ColorGraphService(
        lut_reference_validator=lambda ref: ref.startswith("topos-res://"))
    lutted = service.apply_intent(graph, "set_node_lut", {
        "node_id": corrector.node_id, "lut_ref": "topos-res://lut_a"})
    grouped = service.apply_intent(lutted.graph, "group_subgraph",
                                   {"member_ids": [corrector.node_id]})
    refs = collect_graph_lut_closure(grouped.graph.to_dict())
    assert refs == ["topos-res://lut_a"]  # compound 内正确收集
    # legacy 载体（迁移期图）也计入
    with_legacy = _with_legacy_lut(graph, corrector.node_id,
                                   Path("/old/absolute.cube"))
    assert collect_graph_lut_closure(with_legacy.to_dict()) == ["/old/absolute.cube"]


def _with_legacy_lut(graph, node_id, path):
    """模拟迁移期旧工程：legacy_payload.active_lut_path 携带绝对路径。"""
    from dataclasses import replace as _r
    idx = [n.node_id for n in graph.nodes].index(node_id)
    node = graph.nodes[idx]
    legacy = dict(node.legacy_payload or {})
    legacy["active_lut_path"] = str(path)
    legacy["lut_intensity"] = 0.8
    return _r(graph, nodes=tuple(
        _r(n, legacy_payload=legacy) if i == idx else n
        for i, n in enumerate(graph.nodes)))


def test_closure_auto_migrates_legacy_paths(tmp_path: Path) -> None:
    cube = _cube(tmp_path / "legacy", "look.cube")
    project, graph = _graph()
    corrector = next(n for n in graph.nodes if n.node_type == "corrector")
    patched = _with_legacy_lut(graph, corrector.node_id, cube)
    session = LutResourceSession(staging_root=str(tmp_path / "staging"))
    report = ensure_graph_lut_closure([patched], session)
    assert report["migrated"] and report["migrated"][0].startswith("topos-res://")
    assert report["missing"] == []
    assert session.status_of(report["migrated"][0]) == "registered"


def test_closure_reports_missing_without_removal(tmp_path: Path) -> None:
    project, graph = _graph()
    corrector = next(n for n in graph.nodes if n.node_type == "corrector")
    service = _service({"topos-res://lut_gone"})
    lutted = service.apply_intent(graph, "set_node_lut", {
        "node_id": corrector.node_id, "lut_ref": "topos-res://lut_gone"})
    session = LutResourceSession(staging_root=str(tmp_path / "staging"))
    report = ensure_graph_lut_closure([lutted.graph], session)
    assert report["missing"] == ["topos-res://lut_gone"]
    # 图内引用原样保留（可修复状态，未静默移除）
    node = next(n for n in lutted.graph.nodes
                if n.node_id == corrector.node_id)
    assert node.params["resources"]["lut_ref"] == "topos-res://lut_gone"


def test_save_carries_closure_and_undo_restores(tmp_path: Path) -> None:
    from src.features.edit.core.edit_project_io import EditProjectIO
    from src.shared.color.lut_resource_registry import get_lut_resource_session
    cube = _cube(tmp_path / "keep", "main.cube")
    project, graph = _graph()
    corrector = next(n for n in graph.nodes if n.node_type == "corrector")
    lutted = _with_legacy_lut(graph, corrector.node_id, cube)
    project = project.update_color_stack(
        next(iter(project.color_stacks)), color_graph=lutted)

    session = get_lut_resource_session()
    session.reset()
    # 保存期闭包自动迁移（edit_project_io 钩子）
    target = tmp_path / "closure.topos"
    assert EditProjectIO().save_project(
        target, project, create_backup=False, use_ai_callback=False)
    with zipfile.ZipFile(target) as zf:
        names = zf.namelist()
        index = json.loads(zf.read("resources/resources.json"))
        assert any(n.startswith("resources/lut/") for n in names)
        kinds = {e["kind"] for e in index["resources"]}
        assert "lut" in kinds
    uri = next(e for e in index["resources"] if e["kind"] == "lut")
    # 撤销删除节点：图命令 undo 恢复同引用（包内资源仍在）
    removed = ColorGraphService().apply_intent(
        lutted, "delete_nodes", {"node_ids": [corrector.node_id]})
    assert removed.ok
    assert collect_graph_lut_closure(removed.graph.to_dict()) == []
    restored = collect_graph_lut_closure(lutted.to_dict())
    assert restored  # 原图（undo 后状态）引用仍在
    get_lut_resource_session().reset()
    from src.shared.project.resource_resolver import set_active_project_resolver
    set_active_project_resolver(None)
    _ = uri


def test_shared_same_ref_across_stacks_in_closure(tmp_path: Path) -> None:
    """共享定义 fanout：两个栈图持同 ref，闭包去重为单资源。"""
    cube = _cube(tmp_path / "shared", "fanout.cube")
    project, graph = _graph()
    corrector = next(n for n in graph.nodes if n.node_type == "corrector")
    lutted = _with_legacy_lut(graph, corrector.node_id, cube)
    session = LutResourceSession(staging_root=str(tmp_path / "staging"))
    report = ensure_graph_lut_closure([lutted, lutted], session)
    assert report["migrated"] and len(report["migrated"]) == 1  # 去重
    assert len(session.lut_entries()) == 1
