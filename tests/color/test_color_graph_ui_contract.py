"""B01 UI 契约冻结测试 —— intent envelope / ack-reject / coverage 门禁。

计划 B01 验收：
- fake backend 记录一次 add/delete/connect/undo intent；
- 断言无 image_engine/GPU 调用（controller 是纯 Python，AST 门禁）；
- 拒绝可恢复（reject 回滚到最近 ack 投影）；
- 每个实际菜单/手势 action 已登记（新增未登记 → 本文件失败）；
- load/hydrate 无 intent 回流；stale session 拒绝；pending 不覆盖新 clip。
"""

from __future__ import annotations

import ast
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from src.app.controllers.color_graph_controller import (  # noqa: E402
    ACTION_CONTRACTS,
    NON_INTENT_SIGNALS,
    EnvelopeError,
    GraphIntent,
    GraphProjection,
    GraphSessionState,
    IntentRejection,
    ColorGraphIntentNormalizer,
    ColorGraphSessionCoordinator,
)

UI_FILES = [
    "src/features/color/ui/node_editor_view.py",
    "src/features/color/ui/node_editor_panel.py",
    "src/app/controllers/menu_toolbar_controller.py",
]

FORBIDDEN_MODULES = (
    "src.features.color.core.image_engine",
    "src.features.color.core.async_renderer",
    "src.features.color.core.shader_compiler",
    "moderngl",
    "PyQt6",
)


# ── B01.1 覆盖门禁：源码里的每个 action 必须登记 ────────────────────────

def _collect_apply_edit_actions(path: Path) -> set[str]:
    """AST 扫描 ``apply_edit("action", ...)`` 常量调用点。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    actions: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "apply_edit"):
            continue
        if not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            actions.add(arg.value)
    return actions


def _all_ui_actions() -> set[str]:
    actions: set[str] = set()
    for rel in UI_FILES:
        actions |= _collect_apply_edit_actions(REPO_ROOT / rel)
    return actions


class TestActionCoverageGate:
    def test_every_source_action_is_registered(self):
        actions = _all_ui_actions()
        assert actions, "AST 扫描未找到 apply_edit 调用（扫描逻辑失效）"
        unregistered = sorted(
            a for a in actions
            if a not in ACTION_CONTRACTS and a not in NON_INTENT_SIGNALS)
        assert not unregistered, (
            f"新增 action 未登记契约: {unregistered}——先在 "
            "ACTION_CONTRACTS/NON_INTENT_SIGNALS 登记再实现 UI")

    def test_core_actions_present_in_table(self):
        for must in ("add_serial", "add_serial_before", "add_parallel", "add_layer",
                     "add_outside", "add_splitter_combiner", "add_key_mixer",
                     "add_node", "add_alpha_output", "remove_alpha_output",
                     "connect", "disconnect", "move_nodes", "move_and_insert",
                     "label_node", "node_color", "set_enabled", "set_all_enabled",
                     "lock_node", "reset_grade", "paste_grade", "paste_nodes",
                     "duplicate_nodes", "delete_nodes", "extract_nodes",
                     "make_compound", "expand_compound", "save_shared",
                     "unlink_shared", "set_node_option", "add_input",
                     "morph_mixer", "node_cache", "auto_layout", "undo", "redo",
                     "reset_grades", "reset_all", "close_compound"):
            assert must in ACTION_CONTRACTS, must
            assert must in _all_ui_actions() or must == "close_compound", must

    def test_controller_module_has_no_engine_or_gl_imports(self):
        import src.app.controllers.color_graph_controller as mod
        src_path = Path(mod.__file__)
        tree = ast.parse(src_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                assert not any(
                    name == f or name.startswith(f + ".") for f in FORBIDDEN_MODULES
                ), f"契约层禁止 import {name}"

    def test_layout_actions_do_not_bump_render_semantics(self):
        """布局/元信息/策略 action 的 category 冻结（B05 revision 语义依据）。"""
        assert ACTION_CONTRACTS["move_nodes"].category.value == "layout"
        assert ACTION_CONTRACTS["auto_layout"].category.value == "layout"
        assert ACTION_CONTRACTS["node_color"].category.value == "layout"
        assert ACTION_CONTRACTS["label_node"].category.value == "metadata"
        assert ACTION_CONTRACTS["lock_node"].category.value == "policy"
        assert ACTION_CONTRACTS["node_cache"].category.value == "policy"
        assert ACTION_CONTRACTS["add_serial"].category.value == "structure"
        assert ACTION_CONTRACTS["set_enabled"].category.value == "parameter"


# ── B01.2 envelope 规范化 ────────────────────────────────────────────────

def _normalizer(**kwargs) -> ColorGraphIntentNormalizer:
    params = dict(session_id="sess-1", scope_owner_id="clip-1", stack_id="stack-1",
                  expected_graph_revision=7)
    params.update(kwargs)
    return ColorGraphIntentNormalizer(**params)


def _envelope(action: str, payload: dict | None = None,
              scope: str = "clip", compound_path: tuple = ()) -> dict:
    return {"scope": scope, "compound_path": list(compound_path),
            "payload": payload if payload is not None else {}}


class TestEnvelopeNormalization:
    def test_standard_payload_normalizes(self):
        n = _normalizer().normalize("add_serial", _envelope(
            "add_serial", {"result": "node-9", "arguments": None, "draft": object()}))
        assert n.command == "insert_corrector"
        assert n.session_id == "sess-1"
        assert n.scope_owner_id == "clip-1"
        assert n.stack_id == "stack-1"
        assert n.expected_graph_revision == 7
        assert n.transaction_id and n.intent_id
        assert n.arguments["before"] is False  # Shift+Alt+S 语义统一（B01.4）
        assert isinstance(n.compound_path, tuple)

    def test_close_compound_special_payload(self):
        n = _normalizer().normalize(
            "close_compound",
            {"scope": "clip", "compound_path": ["compound-1"],
             "payload": {"node_id": "compound-1", "draft": object()}})
        assert n.command == "close_compound"
        assert n.arguments == {"node_id": "compound-1"}
        assert n.compound_path == ("compound-1",)

    def test_close_compound_without_node_id_rejected(self):
        with pytest.raises(EnvelopeError):
            _normalizer().normalize(
                "close_compound",
                {"scope": "clip", "payload": {"draft": object()}})

    def test_unknown_action_rejected(self):
        with pytest.raises(EnvelopeError):
            _normalizer().normalize("brand_new_menu_action", _envelope("x"))

    def test_non_intent_signal_rejected_as_intent(self):
        with pytest.raises(EnvelopeError):
            _normalizer().normalize("fit_graph", _envelope("fit_graph"))

    def test_invalid_scope_rejected(self):
        with pytest.raises(EnvelopeError):
            _normalizer().normalize("add_serial", _envelope("add_serial", scope="clipx"))

    def test_move_and_insert_gaps_recorded(self):
        """当前 UI 只给 positions —— node_id/edge_id 缺口必须显式记录。"""
        n = _normalizer().normalize("move_and_insert", _envelope(
            "move_and_insert", {"result": True,
                                "arguments": {"positions": {"a": (0, 0)}},
                                "draft": object()}))
        assert "node_id" in n.argument_gaps
        assert "edge_id" in n.argument_gaps
        assert "positions" not in n.argument_gaps

    def test_add_serial_arguments_carry_before_flag_when_provided(self):
        n = _normalizer().normalize("add_serial", _envelope(
            "add_serial", {"arguments": {"before": True}}))
        assert n.arguments["before"] is True


# ── B01.3 fake service：ack / reject / projection 生命周期 ──────────────

@dataclass
class FakeGraphService:
    """记录 intent 的 fake 后端（不触 engine/GPU）。"""

    submitted: List[GraphIntent] = field(default_factory=list)
    cancelled: List[tuple[str, int]] = field(default_factory=list)

    def submit_intent(self, intent: GraphIntent) -> None:
        self.submitted.append(intent)

    def cancel_pending(self, session_id: str, generation: int) -> None:
        self.cancelled.append((session_id, generation))


class TestSessionCoordinatorLifecycle:
    def _coordinator(self, service: FakeGraphService):
        loads: List[tuple[Any, str]] = []
        states: List[GraphSessionState] = []

        coord = ColorGraphSessionCoordinator(
            session_id="sess-1",
            on_load_graph=lambda graph, scope: loads.append((graph, scope)),
            on_state_changed=states.append)

        def _submit(intent: GraphIntent) -> bool:
            ok = coord.submit(intent)
            if ok:
                service.submit_intent(intent)
            return ok

        coord._test_submit = _submit  # 测试注入：coordinator 通过宿主转投
        return coord, loads, states

    def _projection(self, coord, revision: int, graph: Any = None,
                    generation: int | None = None) -> GraphProjection:
        return GraphProjection(
            session_id=coord.session_id,
            session_generation=coord.generation if generation is None else generation,
            scope=coord.scope,
            scope_owner_id=coord.scope_owner_id,
            graph=graph if graph is not None else {"rev": revision},
            graph_revision=revision)

    def test_hydrate_emits_no_intent(self):
        service = FakeGraphService()
        coord, loads, states = self._coordinator(service)
        coord.begin_hydrate("clip", "clip-1")
        coord.hydrate_projection(self._projection(coord, 3))
        assert coord.state == GraphSessionState.READY
        assert service.submitted == []
        assert len(loads) == 1  # hydrate 只 load，不回流

    def test_edit_in_ready_records_exactly_one_intent(self):
        service = FakeGraphService()
        coord, _, _ = self._coordinator(service)
        coord.begin_hydrate("clip", "clip-1")
        coord.hydrate_projection(self._projection(coord, 3))

        intent = _normalizer().normalize(
            "add_serial", _envelope("add_serial", {"arguments": None}))
        assert coord._test_submit(intent) is True
        assert len(service.submitted) == 1
        assert coord.state == GraphSessionState.PENDING
        assert coord.pending is intent

        acked = coord.accept_ack(self._projection(coord, 4), intent.intent_id)
        assert acked is True
        assert coord.state == GraphSessionState.READY
        assert coord.last_ack.graph_revision == 4

    def test_edit_during_hydrating_is_dropped(self):
        service = FakeGraphService()
        coord, _, _ = self._coordinator(service)
        coord.begin_hydrate("clip", "clip-1")
        intent = _normalizer().normalize(
            "delete_nodes", _envelope("delete_nodes", {"arguments": {"node_ids": ["a"]}}))
        assert coord._test_submit(intent) is False
        assert service.submitted == []

    def test_reject_rolls_back_to_last_ack(self):
        service = FakeGraphService()
        coord, loads, _ = self._coordinator(service)
        coord.begin_hydrate("clip", "clip-1")
        coord.hydrate_projection(self._projection(coord, 3, graph="G3"))
        loads.clear()

        intent = _normalizer().normalize(
            "connect", _envelope("connect", {
                "arguments": {"src_node": "a", "src_port": "image",
                              "dst_node": "b", "dst_port": "image"}}))
        coord._test_submit(intent)
        # 服务端拒绝（成环）
        ok = coord.accept_reject(IntentRejection(
            code="cycle_detected", message="会成环",
            node_id="b", graph_revision=3), intent.intent_id)
        assert ok is True
        assert coord.state == GraphSessionState.READY
        assert loads == [("G3", "clip")]  # 回滚到最近 ack 投影，可恢复
        assert coord.pending is None

    def test_stale_session_ack_dropped(self):
        """切 clip 后晚到 ack 不覆盖新 clip（generation 失配）。"""
        service = FakeGraphService()
        coord, loads, _ = self._coordinator(service)
        coord.begin_hydrate("clip", "clip-A")
        coord.hydrate_projection(self._projection(coord, 3, graph="GA"))
        intent = _normalizer().normalize(
            "add_serial", _envelope("add_serial", {}))
        coord._test_submit(intent)

        gen = coord.switch_session("sess-2", "clip", "clip-B")
        assert gen == 1
        assert coord.state == GraphSessionState.HYDRATING

        stale = GraphProjection(
            session_id="sess-1", session_generation=0,
            scope="clip", scope_owner_id="clip-A",
            graph="GA-LATE", graph_revision=4)
        assert coord.accept_ack(stale, intent.intent_id) is False
        assert all(g != "GA-LATE" for g, _ in loads)
        coord.hydrate_projection(self._projection(coord, 1, graph="GB"))
        assert loads[-1] == ("GB", "clip")

    def test_pending_edit_does_not_leak_into_new_clip(self):
        service = FakeGraphService()
        coord, loads, _ = self._coordinator(service)
        coord.begin_hydrate("clip", "clip-A")
        coord.hydrate_projection(self._projection(coord, 3, graph="GA"))
        intent = _normalizer().normalize("add_serial", _envelope("add_serial", {}))
        coord._test_submit(intent)

        coord.switch_session("sess-2", "clip", "clip-B")
        # pending 已清空：新会话 reject/ack 旧 intent 均无效
        assert coord.pending is None
        assert coord.accept_reject(
            IntentRejection(code="stale", message="x"), intent.intent_id) is False
        coord.hydrate_projection(self._projection(coord, 0, graph="GB"))
        assert loads == [("GA", "clip"), ("GB", "clip")]

    def test_cross_session_intent_rejected_in_submit(self):
        service = FakeGraphService()
        coord, _, _ = self._coordinator(service)
        coord.begin_hydrate("clip", "clip-A")
        coord.hydrate_projection(self._projection(coord, 3))
        foreign = _normalizer(session_id="other").normalize(
            "add_serial", _envelope("add_serial", {}))
        assert coord._test_submit(foreign) is False
        assert service.submitted == []

    def test_scope_owner_mismatch_projection_dropped(self):
        service = FakeGraphService()
        coord, _, _ = self._coordinator(service)
        coord.begin_hydrate("clip", "clip-A")
        wrong_owner = GraphProjection(
            session_id="sess-1", session_generation=0, scope="clip",
            scope_owner_id="clip-OTHER", graph="GX", graph_revision=9)
        assert coord.accept_ack(wrong_owner) is False

    def test_failed_hydrate_reports_failure_state(self):
        service = FakeGraphService()
        coord, _, states = self._coordinator(service)
        coord.begin_hydrate("clip", "clip-A")
        coord.fail_hydrate("graph corrupt")
        assert coord.state == GraphSessionState.FAILED
        assert states[-1] == GraphSessionState.FAILED
        # failed 状态不接受编辑
        intent = _normalizer().normalize("add_serial", _envelope("add_serial", {}))
        assert coord._test_submit(intent) is False

    def test_undo_intent_uses_history_command(self):
        """undo/redo 是 HISTORY 类别：接通后走项目 command 栈（B05）。"""
        n = _normalizer().normalize("undo", _envelope("undo", {}))
        assert n.command == "graph_undo"
        n = _normalizer().normalize("redo", _envelope("redo", {}))
        assert n.command == "graph_redo"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
