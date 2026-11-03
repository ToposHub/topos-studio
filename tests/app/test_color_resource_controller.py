"""R03: typed target、资源命令与 ack/reject 主干。

验收（计划 R03 验证清单）：锁定、Source/Mixer、无目标、删除节点后晚到
结果、跨 clip/scope（会话失配）、revision 冲突、重复 intent；拒绝图字节
不变、成功一次操作一次 undo；真实项目栈与宿主（非 fake presenter）。
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("PyQt6")

from src.app.controllers.color_resource_controller import ColorResourceController
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.edit.core.commands import ProjectCommandStack
from src.features.edit.core.commands import color_commands
from src.features.edit.core.project import EditProject
from src.features.color.core.color_graph_migration import migrate_legacy_stack
from tests.color.fixtures.color_graph import f03_layer_stack

SCHEMA = [
    {"var_name": "amount", "type": "slider", "default": 0.2,
     "min": 0.0, "max": 1.0},
    {"var_name": "toggle", "type": "checkbox", "default": 0},
    {"var_name": "mode", "type": "select", "default": 0,
     "options": ["a", "b"]},
    {"var_name": "tint", "type": "color", "default": [1.0, 1.0, 1.0]},
]

LUT_URIS = {"topos-res://lut_good": True}


def _schema(resource_id: str):
    return list(SCHEMA) if resource_id == "grain" else None


def _lut_valid(ref: str) -> bool:
    return LUT_URIS.get(ref, False)


@pytest.fixture()
def patched_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """命令执行路径的 service 用注入资源权威（不依赖磁盘注册表）。"""
    monkeypatch.setattr(
        color_commands, "_graph_service",
        lambda: ColorGraphService(effect_schema_provider=_schema,
                                  lut_reference_validator=_lut_valid))


def _project_with_graph():
    project, stack_id = EditProject.create(name="r03").create_color_stack(
        asset_id="a1", name="s")
    stack_data = project.color_stacks[stack_id].with_layers(
        [l.to_dict(None) for l in f03_layer_stack()])
    graph = migrate_legacy_stack(stack_data)
    project = project.update_color_stack(stack_id, color_graph=graph)
    return project, stack_id, graph


class _ResourceHarness:
    def __init__(self, qapp, project, with_panel=True):
        from PyQt6.QtWidgets import QWidget
        from tests.app.test_color_graph_app_host import _Harness
        from src.features.color.ui.effects_browser_panel import ColorEffectsBrowser
        from src.features.color.ui.lut_browser_panel import ColorLutBrowser
        self.graph = _Harness(qapp, project, with_panel=with_panel)
        self.ui = QWidget()
        self.ui.color_graph_host = self.graph.host
        self.ui.color_node_panel = self.graph.panel
        self.ui.color_effects_browser = ColorEffectsBrowser()
        self.ui.color_lut_browser = ColorLutBrowser()
        self.catalog = SimpleNamespace(entries=lambda: [
            {"id": "grain", "kind": "effect", "name": "Grain",
             "category": "Texture", "available": True, "parameters": SCHEMA},
            {"id": "missing_fx", "kind": "effect", "name": "Missing FX",
             "category": "Texture", "available": False,
             "disabled_reason": "ai_model_missing", "parameters": []},
            {"id": "topos-res://lut_good", "kind": "lut", "name": "Good LUT",
             "category": "LUT", "available": True,
             "resource_uri": "topos-res://lut_good"},
        ])
        self.controller = ColorResourceController(
            self.ui, catalog_provider=self.catalog)
        self.rejections: list[Any] = []
        self.controller.resource_rejected.connect(self.rejections.append)


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def rig(qapp, patched_service):
    project, stack_id, graph = _project_with_graph()
    harness = _ResourceHarness(qapp, project)
    harness.graph.host.on_clip_stack_loaded(stack_id, scope_owner_id=stack_id)
    harness.stack_id = stack_id
    harness.corrector = next(n.id for n in harness.graph.panel.snapshot().nodes.values()
                             if n.kind.value == "corrector")
    return harness


def _graph(rig) -> Any:
    return rig.graph.holder["project"].color_stacks[rig.stack_id].color_graph


def _instances(rig, node_id=None) -> list[dict]:
    node = next((n for n in _graph(rig).nodes
                 if n.node_id == (node_id or rig.corrector)), None)
    plugins = (node.legacy_payload or {}).get("plugins") if node else None
    return list(plugins or [])


# ═══ service 层矩阵 ═══════════════════════════════════════════════════

def test_service_add_effect_defaults_and_revision() -> None:
    project, _sid, graph = _project_with_graph()
    service = ColorGraphService(effect_schema_provider=_schema,
                                lut_reference_validator=_lut_valid)
    node = next(n for n in graph.nodes if n.node_type == "corrector")
    before = graph.to_dict()
    result = service.apply_intent(graph, "add_node_effect",
                                  {"node_id": node.node_id, "resource_id": "grain"})
    assert result.ok, result.diagnostics
    assert result.graph.graph_revision == graph.graph_revision + 1
    plugins = result.graph.nodes[
        [n.node_id for n in result.graph.nodes].index(node.node_id)] \
        .legacy_payload["plugins"]
    assert plugins[-1]["params"]["amount"] == 0.2
    assert plugins[-1]["plugin_id"] == "grain"
    assert graph.to_dict() == before  # 原图不变（不可变快照）


def test_service_reject_matrix() -> None:
    project, _sid, graph = _project_with_graph()
    service = ColorGraphService(effect_schema_provider=_schema,
                                lut_reference_validator=_lut_valid)
    source = next(n for n in graph.nodes if n.node_type == "source")
    corrector = next(n for n in graph.nodes if n.node_type == "corrector")
    before = graph.to_dict()

    def fails(command: str, arguments: dict, code: str) -> None:
        result = service.apply_intent(graph, command, arguments)
        assert not result.ok and result.primary_code == code, \
            (command, arguments, result.primary_code)

    fails("add_node_effect", {"node_id": source.node_id,
                              "resource_id": "grain"}, "node_not_effectable")
    fails("add_node_effect", {"node_id": corrector.node_id,
                              "resource_id": "nope"}, "resource_unknown")
    fails("add_node_effect", {"node_id": "ghost", "resource_id": "grain"},
          "node_not_found")
    fails("set_node_lut", {"node_id": corrector.node_id,
                           "lut_ref": "topos-res://lut_bad"}, "resource_unknown")
    fails("set_node_lut_strength", {"node_id": corrector.node_id,
                                    "strength": 1.5}, "param_out_of_range")
    fails("remove_node_effect", {"node_id": corrector.node_id,
                                 "instance_id": "nope"}, "instance_not_found")
    fails("move_node_effect", {"node_id": corrector.node_id,
                               "instance_id": "nope", "index": 0},
          "instance_not_found")
    # 全部拒绝后图字节不变
    assert graph.to_dict() == before


def test_service_locked_node_rejects_all_resource_ops() -> None:
    from dataclasses import replace as _r
    project, _sid, graph = _project_with_graph()
    service = ColorGraphService(effect_schema_provider=_schema,
                                lut_reference_validator=_lut_valid)
    corrector = next(n for n in graph.nodes if n.node_type == "corrector")
    params = dict(corrector.params)
    params["locked"] = True
    graph = _r(graph, nodes=tuple(
        _r(n, params=params) if n.node_id == corrector.node_id else n
        for n in graph.nodes))
    before = graph.to_dict()
    result = service.apply_intent(graph, "add_node_effect",
                                  {"node_id": corrector.node_id,
                                   "resource_id": "grain"})
    assert not result.ok and result.primary_code == "node_locked"
    assert graph.to_dict() == before


def test_service_param_validation_and_unknown_preserved() -> None:
    project, _sid, graph = _project_with_graph()
    service = ColorGraphService(effect_schema_provider=_schema,
                                lut_reference_validator=_lut_valid)
    corrector = next(n for n in graph.nodes if n.node_type == "corrector")
    added = service.apply_intent(graph, "add_node_effect",
                                 {"node_id": corrector.node_id,
                                  "resource_id": "grain"})
    assert added.ok
    node = next(n for n in added.graph.nodes if n.node_id == corrector.node_id)
    instance_id = node.legacy_payload["plugins"][-1]["instance_id"]
    # 旧工程未知参数：编辑不触碰，reset 保留
    node_idx = [n.node_id for n in added.graph.nodes].index(corrector.node_id)
    from dataclasses import replace as _r
    plugins = [dict(p) for p in node.legacy_payload["plugins"]]
    plugins[-1] = dict(plugins[-1])
    plugins[-1]["params"] = dict(plugins[-1]["params"],
                                 __future_key="keep-me")
    patched = _r(added.graph, nodes=tuple(
        _r(n, legacy_payload={**n.legacy_payload, "plugins": plugins})
        if i == node_idx else n
        for i, n in enumerate(added.graph.nodes)))
    result = service.apply_intent(patched, "set_node_effect_parameter",
                                   {"node_id": corrector.node_id,
                                    "instance_id": instance_id,
                                    "name": "amount", "value": 5.0})
    assert not result.ok and result.primary_code == "param_out_of_range"
    result = service.apply_intent(patched, "set_node_effect_parameter",
                                  {"node_id": corrector.node_id,
                                   "instance_id": instance_id,
                                   "name": "amount", "value": "fast"})
    assert not result.ok and result.primary_code == "param_type_mismatch"
    result = service.apply_intent(patched, "set_node_effect_parameter",
                                  {"node_id": corrector.node_id,
                                   "instance_id": instance_id,
                                  "name": "__future_key", "value": 1})
    assert not result.ok and result.primary_code == "param_unknown"
    # reset：已知参数回默认，未知字段保留
    result = service.apply_intent(patched, "reset_node_effect",
                                  {"node_id": corrector.node_id,
                                   "instance_id": instance_id})
    assert result.ok
    reset_node = next(n for n in result.graph.nodes
                      if n.node_id == corrector.node_id)
    final = reset_node.legacy_payload["plugins"][-1]["params"]
    assert final["amount"] == 0.2 and final["__future_key"] == "keep-me"


def test_service_duplicate_instances_and_reorder_identity() -> None:
    project, _sid, graph = _project_with_graph()
    service = ColorGraphService(effect_schema_provider=_schema,
                                lut_reference_validator=_lut_valid)
    corrector = next(n for n in graph.nodes if n.node_type == "corrector")
    first = service.apply_intent(graph, "add_node_effect",
                                 {"node_id": corrector.node_id, "resource_id": "grain"})
    second = service.apply_intent(first.graph, "add_node_effect",
                                  {"node_id": corrector.node_id, "resource_id": "grain"})
    assert second.ok
    node = next(n for n in second.graph.nodes if n.node_id == corrector.node_id)
    plugins = list(node.legacy_payload["plugins"])
    assert len(plugins) == 2 and plugins[0]["instance_id"] != plugins[1]["instance_id"]
    moved = service.apply_intent(second.graph, "move_node_effect",
                                 {"node_id": corrector.node_id,
                                  "instance_id": plugins[0]["instance_id"],
                                  "index": 1})
    moved_node = next(n for n in moved.graph.nodes if n.node_id == corrector.node_id)
    after = list(moved_node.legacy_payload["plugins"])
    assert [p["instance_id"] for p in after] == \
        [plugins[1]["instance_id"], plugins[0]["instance_id"]]


def test_service_compound_path_targets_child() -> None:
    project, _sid, graph = _project_with_graph()
    service = ColorGraphService(effect_schema_provider=_schema,
                                lut_reference_validator=_lut_valid)
    corrector = next(n for n in graph.nodes if n.node_type == "corrector")
    grouped = service.apply_intent(graph, "group_subgraph",
                                   {"member_ids": [corrector.node_id]})
    assert grouped.ok
    compound = next(n for n in grouped.graph.nodes if n.node_type == "compound")
    inner = next(n for n in compound.compound_children.nodes
                 if n.node_type == "corrector")
    result = service.apply_intent(grouped.graph, "add_node_effect",
                                  {"node_id": inner.node_id,
                                   "resource_id": "grain",
                                   "_compound_path": (compound.node_id,)})
    assert result.ok, result.diagnostics
    final_compound = next(n for n in result.graph.nodes
                          if n.node_type == "compound")
    final_inner = next(n for n in final_compound.compound_children.nodes
                       if n.node_type == "corrector")
    assert final_inner.legacy_payload["plugins"]


# ═══ 宿主/命令层（真实项目栈） ═══════════════════════════════════════

def test_controller_add_one_command_one_undo(rig) -> None:
    rig.controller.activate_effect(
        {"id": "grain", "kind": "effect", "name": "Grain", "available": True,
         "parameters": SCHEMA})
    instances = _instances(rig)
    assert len(instances) == 1 and instances[0]["plugin_id"] == "grain"
    # ack 水合：浏览器从权威投影读到实例
    assert rig.ui.color_effects_browser._instances
    assert rig.ui.color_effects_browser._instances[0]["resource_id"] == "grain"
    assert len(rig.graph.cmd_stack._undo_stack) == 1  # 一次操作一次 undo
    assert rig.graph.host.undo()  # 宿主 undo：命令回滚 + 推送新 ack 投影
    assert _instances(rig) == []
    rig.controller.refresh_target()
    assert rig.ui.color_effects_browser._instances == []


def test_controller_unavailable_and_no_target_reject(rig) -> None:
    rig.controller.activate_effect(
        {"id": "missing_fx", "kind": "effect", "available": False})
    assert [r["code"] for r in rig.rejections] == ["resource_unavailable"]
    assert _instances(rig) == []
    # 无目标：active 节点改为 source 后再激活
    view = rig.graph.panel.view
    source_id = next(n.id for n in rig.graph.panel.snapshot().nodes.values()
                     if n.kind.value == "source")
    view.state.graph.active_id = source_id
    rig.controller.refresh_target()  # 生产中由 active_node_changed 信号驱动
    rig.controller.activate_effect(
        {"id": "grain", "kind": "effect", "available": True})
    assert rig.rejections[-1]["code"] == "no_target"
    assert _instances(rig) == []


def test_controller_locked_node_reject_keeps_graph_bytes(rig) -> None:
    from src.features.edit.core.commands import color_commands as cc
    lock = cc.SetColorGraphLockCommand.create(rig.stack_id, True)
    rig.graph.holder["project"] = rig.graph.cmd_stack.execute(
        lock, rig.graph.holder["project"])
    rig.graph.host.on_clip_stack_loaded(rig.stack_id,
                                        scope_owner_id=rig.stack_id)
    before = _graph(rig).to_dict()
    history_before = len(rig.graph.cmd_stack._undo_stack)
    rig.controller.activate_effect(
        {"id": "grain", "kind": "effect", "available": True})
    assert rig.rejections and rig.rejections[-1]["code"] in (
        "rejected", "node_locked", "stale_session")
    assert _graph(rig).to_dict() == before  # 拒绝：图字节不变
    assert len(rig.graph.cmd_stack._undo_stack) == history_before  # 不入历史


def test_controller_revision_conflict_rejects(rig) -> None:
    rig.controller.activate_effect(
        {"id": "grain", "kind": "effect", "available": True})
    assert len(_instances(rig)) == 1
    before = _graph(rig).to_dict()
    # 构造陈旧 expected revision（模拟并发编辑后晚到 intent）
    target = rig.controller._capture_target(rig.corrector)
    stale = dict(target, expected_graph_revision=(target[
        "expected_graph_revision"] or 0) - 1)
    from src.app.controllers.color_graph_controller import (
        ColorGraphIntentNormalizer,
    )
    normalizer = ColorGraphIntentNormalizer(
        session_id=stale["session_id"],
        scope_owner_id=stale["scope_owner_id"],
        stack_id=stale["stack_id"],
        expected_graph_revision=stale["expected_graph_revision"],
        session_generation=stale["generation"])
    intent = normalizer.normalize("add_effect", {
        "scope": "clip", "compound_path": [],
        "payload": {"arguments": {"node_id": rig.corrector,
                                  "resource_id": "grain"}}})
    assert not rig.graph.host.bridge.submit(intent)
    assert _graph(rig).to_dict() == before


def test_controller_late_result_after_node_delete(rig) -> None:
    target = rig.controller._capture_target(rig.corrector)
    assert target is not None
    # 删除目标节点（经权威命令）
    ok = rig.graph.panel.view.apply_edit(
        "delete_nodes",
        lambda: rig.graph.panel.view.state.delete_nodes({rig.corrector}))
    assert ok is not None
    assert next((n for n in _graph(rig).nodes
                 if n.node_id == rig.corrector), None) is None
    before = _graph(rig).to_dict()
    history = len(rig.graph.cmd_stack._undo_stack)
    # 晚到的资源 intent 指向已删除节点 → 领域拒绝
    assert not rig.controller._submit("add_effect", rig.corrector,
                                      {"resource_id": "grain"})
    assert _graph(rig).to_dict() == before
    assert len(rig.graph.cmd_stack._undo_stack) == history


def test_controller_stale_session_rejects(rig) -> None:
    # 会话代际切换（切 clip）：晚到 intent 被协调器拒绝
    target = rig.controller._capture_target(rig.corrector)
    coordinator = rig.graph.host._coordinator
    coordinator.end_session() if hasattr(coordinator, "end_session") else None
    from src.app.controllers.color_graph_controller import (
        ColorGraphIntentNormalizer,
    )
    normalizer = ColorGraphIntentNormalizer(
        session_id="other-session",
        scope_owner_id=target["scope_owner_id"],
        stack_id=target["stack_id"],
        expected_graph_revision=target["expected_graph_revision"],
        session_generation=99)
    intent = normalizer.normalize("add_effect", {
        "scope": "clip", "compound_path": [],
        "payload": {"arguments": {"node_id": rig.corrector,
                                  "resource_id": "grain"}}})
    assert not rig.graph.host.bridge.submit(intent)


def test_controller_lut_apply_strength_remove_roundtrip(rig) -> None:
    lut_entry = {"id": "topos-res://lut_good", "kind": "lut",
                 "available": True, "resource_uri": "topos-res://lut_good"}
    rig.controller.activate_resource(lut_entry)
    node = next(n for n in _graph(rig).nodes if n.node_id == rig.corrector)
    resources = node.params["resources"]
    assert resources["lut_ref"] == "topos-res://lut_good"
    assert resources["lut_strength"] == 1.0
    # 未注册 uri（旧路径条目）拒绝
    rig.controller.activate_resource(
        {"id": "lut:/abs/path.cube", "kind": "lut", "available": True})
    assert rig.rejections[-1]["code"] == "resource_unavailable"
    # 强度 + 移除
    rig.controller.edit("set_lut_strength", {"strength": 0.42})
    node = next(n for n in _graph(rig).nodes if n.node_id == rig.corrector)
    assert abs(node.params["resources"]["lut_strength"] - 0.42) < 1e-9
    rig.controller.edit("remove_lut", {})
    node = next(n for n in _graph(rig).nodes if n.node_id == rig.corrector)
    assert "lut_ref" not in node.params["resources"]
    assert not rig.ui.color_lut_browser._current_lut


def test_controller_drop_uses_dropped_node_not_active(rig) -> None:
    view = rig.graph.panel.view
    snap = view.state.snapshot()
    correctors = sorted(n.id for n in snap.nodes.values()
                        if n.kind.value == "corrector")
    second = correctors[-1] if len(correctors) > 1 else correctors[0]
    view.state.graph.active_id = correctors[0]
    rig.controller.refresh_target()
    rig.controller.drop(second, {"kind": "effect", "resource_id": "grain"})
    assert _instances(rig, node_id=second), "drop 落点未成为目标"
    first_instances = _instances(rig, node_id=correctors[0])
    assert not first_instances or all(
        p["plugin_id"] != "grain" for p in first_instances)


def test_controller_edit_signals_route_through_commands(rig) -> None:
    rig.controller.activate_effect(
        {"id": "grain", "kind": "effect", "available": True})
    instance_id = _instances(rig)[0]["instance_id"]
    rig.controller.edit("set_parameter", {"instance_id": instance_id,
                                          "name": "amount", "value": 0.9})
    assert _instances(rig)[0]["params"]["amount"] == 0.9
    rig.controller.edit("set_enabled", {"instance_id": instance_id,
                                        "enabled": False})
    assert _instances(rig)[0]["enabled"] is False
    rig.controller.edit("reset_instance", {"instance_id": instance_id})
    assert _instances(rig)[0]["params"]["amount"] == 0.2
    rig.controller.edit("remove_instance", {"instance_id": instance_id})
    assert _instances(rig) == []
    # 5 次操作 5 个 command（每个可单独 undo）
    assert len(rig.graph.cmd_stack._undo_stack) == 5


# ═══ R11：真实状态投影与诊断 ═════════════════════════════════════════

def test_resource_lut_lights_tool_indicator_via_projection() -> None:
    """params.resources.lut_ref（权威域）点亮 LUT 标记；移除即灭。"""
    from src.app.controllers.color_graph_controller import (
        _indicator_payload,
    )
    from src.features.color.ui.node_tool_indicators import detect_tool_indicators
    legacy = {"active_lut_path": "/old.cube", "lut_intensity": 0.7}
    params = {"resources": {"lut_ref": "topos-res://lut_new",
                            "lut_strength": 0.9}}
    merged = _indicator_payload(legacy, params)
    assert merged["active_lut_path"] == "topos-res://lut_new"
    indicators = detect_tool_indicators(merged, params)
    assert "lut" in indicators
    removed = _indicator_payload(legacy, {"resources": {"other": 1}})
    assert "active_lut_path" not in removed  # 移除不复活旧值
    assert "lut" not in detect_tool_indicators(removed, {})


def test_instance_status_projection_from_preflight(rig) -> None:
    """未知插件实例 → status=resource_unknown（fail-visible，配置保留）。"""
    rig.controller.activate_effect(
        {"id": "grain", "kind": "effect", "available": True})
    node = next(n for n in _graph(rig).nodes if n.node_id == rig.corrector)
    # 注入未注册插件实例（模拟旧工程缺失插件）
    from dataclasses import replace as _r
    idx = [n.node_id for n in _graph(rig).nodes].index(rig.corrector)
    plugins = [dict(p) for p in node.legacy_payload["plugins"]]
    plugins.append({"instance_id": "ghost", "plugin_id": "missing_fx",
                    "params": {}, "enabled": True, "type": "dctl"})
    patched = _r(_graph(rig), nodes=tuple(
        _r(n, legacy_payload={**n.legacy_payload, "plugins": plugins})
        if i == idx else n for i, n in enumerate(_graph(rig).nodes)))
    from src.shared.project.color_graph import ColorGraphData
    payload = next(n for n in patched.to_dict()["nodes"]
                   if n["node_id"] == rig.corrector)
    instances = rig.controller._project_instances(payload)
    status = {i["instance_id"]: i["status"] for i in instances}
    assert status.get("ghost") == "resource_unknown"
    ghost = next(i for i in instances if i["instance_id"] == "ghost")
    assert ghost["available"] is False and ghost["disabled_reason"]


def test_rejection_diagnostic_carries_target_and_duration(rig) -> None:
    """拒绝诊断携带 action/code/target/revision/时长（不冒充成功）。"""
    from src.features.edit.core.commands import color_commands as cc
    lock = cc.SetColorGraphLockCommand.create(rig.stack_id, True)
    rig.graph.holder["project"] = rig.graph.cmd_stack.execute(
        lock, rig.graph.holder["project"])
    rig.graph.host.on_clip_stack_loaded(rig.stack_id,
                                        scope_owner_id=rig.stack_id)
    rig.controller.activate_effect(
        {"id": "grain", "kind": "effect", "available": True})
    assert rig.rejections
    payload = rig.rejections[-1]
    assert payload["code"] in ("rejected", "node_locked", "stale_session")
    assert "duration_ms" in payload and payload["duration_ms"] >= 0.0
    assert (payload.get("target") or {}).get("node_id") == rig.corrector


def test_production_mode_removes_preview_banner(qtbot) -> None:
    from src.features.color.ui.effects_browser_panel import ColorEffectsBrowser
    from src.features.color.ui.lut_browser_panel import ColorLutBrowser
    effects = ColorEffectsBrowser()
    luts = ColorLutBrowser()
    assert effects.status_label.text()  # preview 横幅存在
    effects.set_production_mode(True)
    luts.set_production_mode(True)
    assert effects.status_label.text() == "" and luts.status_label.text() == ""
    effects.set_production_mode(False)
    assert effects.status_label.text()  # 显式开发模式恢复横幅


def test_node_lut_menu_shares_command_path(rig, monkeypatch) -> None:
    """N04：菜单 apply/browse 与浏览器共用命令；勾选态读权威 resources。"""
    from src.shared.color.lut_resource_registry import get_lut_resource_session
    get_lut_resource_session().reset()
    view = rig.graph.panel.view
    applied: list[tuple[str, str]] = []
    view.node_lut_apply_requested.connect(
        lambda node_id, ref: applied.append((node_id, ref)))
    # 菜单信号（main_window 处理器逻辑在此直接验证控制器路径）
    view.node_lut_apply_requested.emit(rig.corrector, "topos-res://lut_good")
    assert applied == [(rig.corrector, "topos-res://lut_good")]
    # 控制器路径（同浏览器）：设置后权威域持有 LUT → menu_options 投影
    rig.controller.activate_resource(
        {"id": "topos-res://lut_good", "kind": "lut", "available": True,
         "resource_uri": "topos-res://lut_good"})
    node = next(n for n in _graph(rig).nodes if n.node_id == rig.corrector)
    assert node.params["resources"]["lut_ref"] == "topos-res://lut_good"
    from src.app.controllers.color_graph_controller import graph_payload_to_draft
    draft = graph_payload_to_draft(_graph(rig).to_dict())
    options = draft.nodes[rig.corrector].node_options
    assert options.get("lut_path") == "topos-res://lut_good"  # 菜单勾选态
    # 移除路径
    rig.controller.edit("remove_lut", {})
    node = next(n for n in _graph(rig).nodes if n.node_id == rig.corrector)
    assert "lut_ref" not in node.params.get("resources", {})
    get_lut_resource_session().reset()


def test_external_project_change_reprojects(rig) -> None:
    """N11：外部栈 undo（非图菜单路径）后统一重投影到旧 revision。"""
    rig.controller.activate_effect(
        {"id": "grain", "kind": "effect", "available": True})
    assert len(_instances(rig)) == 1
    revised = _graph(rig).graph_revision
    # 模拟全局 undo：直接驱动共享工程栈（绕过图桥），再发布新工程
    undone = rig.graph.cmd_stack.undo(rig.graph.holder["project"])
    assert undone is not None
    rig.graph.holder["project"] = undone
    # 图桥未被调用——last_ack 仍是新 revision（外部变更未感知）
    assert rig.graph.host._coordinator.last_ack.graph_revision == revised
    # N11：外部工程变更统一重投影
    rig.graph.host.on_external_project_change()
    assert rig.graph.host._coordinator.last_ack.graph_revision == \
        _graph(rig).graph_revision
    assert _graph(rig).graph_revision < revised
    rig.controller.refresh_target()
    assert _instances(rig) == []  # 面板随权威恢复


def test_external_change_is_idempotent_when_revision_matches(rig) -> None:
    rig.controller.activate_effect(
        {"id": "grain", "kind": "effect", "available": True})
    before = rig.graph.host._coordinator.last_ack.graph_revision
    rig.graph.host.on_external_project_change()  # 自家命令流已推同 ack
    assert rig.graph.host._coordinator.last_ack.graph_revision == before


def test_global_history_gating_helper(rig) -> None:
    """N11：图会话 READY + 共享栈有历史 → 全局 undo 走图宿主。"""
    from src.app.controllers.layer_controller import LayerOpsMixin
    rig.graph.host._project_stack = rig.graph.cmd_stack
    host = SimpleNamespace(color_graph_host=rig.graph.host)
    # 借宿主方法（mixin 解析 self 属性）
    gating = LayerOpsMixin._color_graph_history_available.__get__(host)
    assert gating() is False  # 栈空（水合未入栈）
    rig.controller.activate_effect(
        {"id": "grain", "kind": "effect", "available": True})
    assert gating() is True
