"""ColorGraphController —— 节点 UI 与领域图服务之间的 intent 契约层（N08）。

B01 冻结的契约（计划 §6.2/§6.3）：

1. **Action 契约表**（``ACTION_CONTRACTS``）：E36/E37/E43 实际发出的每个
   edit action 在此登记——类别（结构/参数/布局/元信息/策略/历史）、未来
   N02 command 名、领域侧必需参数。**新增菜单未登记即契约测试失败。**
2. **Envelope 规范化**（``ColorGraphIntentNormalizer``）：panel envelope
   ``{'scope', 'compound_path', 'payload': {'result', 'arguments', 'draft'}}``
   → 带 ``intent_id/session_id/scope_owner_id/stack_id/expected_graph_
   revision/transaction_id`` 的领域 intent。``close_compound`` 是特殊事件
   （payload 只有 ``node_id/draft``），不得按统一字段盲读。
3. **会话状态机**（``GraphSessionState``）：disconnected → hydrating →
   ready → pending → failed。hydrate 有锁（不发 intent）；ack 按
   session/scope owner/compound path 匹配，晚到丢弃；reject 回滚到最近
   ack 的权威投影；clip 切换递增 session generation，pending 结果不得
   落到新 clip。

本文件在 B01 阶段**不 import** Qt / image_engine / GL / 项目模型——
UI 面板与领域服务都以参数注入，接通（B05/B06）后再挂 QWidget 信号。
``draft`` 仅作受校验的提案，不作为工程 ID 来源。
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from src.features.edit.core.commands.color_commands import (
    ApplyColorGraphCommand,
    GraphCommandConflict,
    GraphTransactionBuffer,
    SetColorGraphLockCommand,
)


# ── Action 契约表 ────────────────────────────────────────────────────────

class IntentCategory(enum.Enum):
    """intent 类别 → revision/undo 语义（计划 §6.2 特别规则）。"""

    STRUCTURE = "structure"        # 结构 command：改变图拓扑（render revision）
    PARAMETER = "parameter"        # 参数 command：改变像素（render revision）
    LAYOUT = "layout"              # 布局元信息：持久化但不触发 render revision
    METADATA = "metadata"          # label 等元信息：不 render
    POLICY = "policy"              # lock/cache：编辑权限/执行策略，不直接改像素
    HISTORY = "history"            # undo/redo：走项目/会话 command 栈
    DEFINITION = "definition"      # shared 定义：定义+引用 command
    NAVIGATION = "navigation"      # 导航：无工程变更
    RESET = "reset"                # 参数/整图重置事务
    SPECIAL = "special"            # close_compound：导航 + 待提交事务收束


@dataclass(frozen=True)
class ActionContract:
    """单个 edit action 的冻结契约。"""

    action: str
    command: str                    # 未来 N02 operation 名（B05 ApplyColorGraphCommand）
    category: IntentCategory
    # 领域侧必需的 argument 键（B05 接线前 UI 若未提供 → argument_gaps）
    required_arguments: tuple[str, ...] = ()
    # 当前 UI envelope 已提供的键（B00 现状，gaps = required - provided）
    provided_arguments: tuple[str, ...] = ()


ACTION_CONTRACTS: dict[str, ActionContract] = {c.action: c for c in (
    # ── 结构 command ──
    ActionContract("add_serial", "insert_corrector", IntentCategory.STRUCTURE,
                   ("active_node_id", "before"), ()),
    ActionContract("add_serial_before", "insert_corrector", IntentCategory.STRUCTURE,
                   ("active_node_id",), ()),
    ActionContract("add_parallel", "add_parallel_branch", IntentCategory.STRUCTURE,
                   ("branch_origin_id", "mixer_slots"), ()),
    ActionContract("add_layer", "add_layer_branch", IntentCategory.STRUCTURE,
                   ("base_input_port", "slot_order"), ()),
    ActionContract("add_outside", "add_outside_corrector", IntentCategory.STRUCTURE,
                   ("source_key_node_id", "image_input_port"), ()),
    ActionContract("add_splitter_combiner", "add_splitter_pair", IntentCategory.STRUCTURE,
                   ("upstream_edge", "downstream_edge"), ()),
    ActionContract("add_key_mixer", "add_key_mixer", IntentCategory.STRUCTURE,
                   ("key_source_ids",), ()),
    ActionContract("add_node", "add_node", IntentCategory.STRUCTURE,
                   ("kind", "position"), ("kind",)),
    ActionContract("add_alpha_output", "add_alpha_output", IntentCategory.STRUCTURE, ()),
    ActionContract("remove_alpha_output", "remove_alpha_output", IntentCategory.STRUCTURE,
                   ("node_id",)),
    ActionContract("connect", "connect_ports", IntentCategory.STRUCTURE,
                   ("src_node", "src_port", "dst_node", "dst_port"),
                   ("src_node", "src_port", "dst_node", "dst_port")),
    ActionContract("disconnect", "disconnect_edge", IntentCategory.STRUCTURE,
                   ("edge_id",)),
    ActionContract("delete_nodes", "delete_nodes", IntentCategory.STRUCTURE,
                   ("node_ids",)),
    ActionContract("extract_nodes", "extract_nodes", IntentCategory.STRUCTURE,
                   ("node_ids",)),
    ActionContract("duplicate_nodes", "duplicate_subgraph", IntentCategory.STRUCTURE,
                   ("source_ids", "offset", "boundary_policy"), ("offset",)),
    ActionContract("paste_nodes", "paste_subgraph", IntentCategory.STRUCTURE,
                   ("source_ids",)),
    ActionContract("make_compound", "group_subgraph", IntentCategory.STRUCTURE,
                   ("member_ids", "boundary_mapping"), ()),
    ActionContract("expand_compound", "ungroup_subgraph", IntentCategory.STRUCTURE,
                   ("node_id",)),
    ActionContract("add_input", "add_mixer_slot", IntentCategory.STRUCTURE,
                   ("node_id",)),
    ActionContract("morph_mixer", "change_mixer_type", IntentCategory.PARAMETER,
                   ("node_id", "kind"), ("node_id", "kind")),
    # move_and_insert：布局+结构一次事务（B01.4 冻结：一个 command、一个
    # undo、一次 revision；当前 UI arguments 只有 positions → 记 gap）
    ActionContract("move_and_insert", "move_and_insert", IntentCategory.STRUCTURE,
                   ("positions", "node_id", "edge_id"), ("positions",)),
    # ── 参数 command ──
    ActionContract("set_enabled", "set_bypass", IntentCategory.PARAMETER,
                   ("node_ids", "enabled"), ("ids", "enabled")),
    ActionContract("set_all_enabled", "set_all_enabled", IntentCategory.PARAMETER,
                   ("enabled",)),
    ActionContract("reset_grade", "reset_corrector_parameters", IntentCategory.PARAMETER,
                   ("node_ids",), ()),
    ActionContract("paste_grade", "paste_grade_domains", IntentCategory.PARAMETER,
                   ("grade_domains",)),
    ActionContract("set_node_option", "set_node_option", IntentCategory.PARAMETER,
                   ("node_id", "name", "value"), ("name", "value")),
    # ── 布局 / 元信息 / 策略 ──
    ActionContract("move_nodes", "update_layout", IntentCategory.LAYOUT,
                   ("positions",), ("positions",)),
    ActionContract("auto_layout", "update_layout", IntentCategory.LAYOUT, ()),
    ActionContract("node_color", "set_node_color", IntentCategory.LAYOUT,
                   ("node_ids", "color"), ("color",)),
    ActionContract("label_node", "set_node_metadata", IntentCategory.METADATA,
                   ("node_id", "label"), ("node_id", "label")),
    ActionContract("lock_node", "set_edit_lock", IntentCategory.POLICY, ("node_ids",)),
    ActionContract("node_cache", "set_cache_policy", IntentCategory.POLICY,
                   ("node_ids", "mode"), ("mode",)),
    # ── 定义 / 历史 / 重置 / 导航 ──
    ActionContract("save_shared", "create_shared_definition", IntentCategory.DEFINITION,
                   ("node_id", "param_domains", "version"), ()),
    ActionContract("unlink_shared", "make_local_corrector", IntentCategory.DEFINITION,
                   ("node_ids",), ()),
    ActionContract("undo", "graph_undo", IntentCategory.HISTORY, ()),
    ActionContract("redo", "graph_redo", IntentCategory.HISTORY, ()),
    ActionContract("reset_grades", "reset_graph_parameters", IntentCategory.RESET, ()),
    ActionContract("reset_all", "reset_graph", IntentCategory.RESET, ()),
    ActionContract("close_compound", "close_compound", IntentCategory.SPECIAL,
                   ("node_id",), ("node_id",)),
)}

#: E36/E37 会发出但不构成 edit intent 的 presentation/读取行为（契约登记，
#: 供 coverage 测试核对"没有无语义按钮"）：
NON_INTENT_SIGNALS: frozenset[str] = frozenset({
    "copy_grade",            # 读取剪贴板域（无 command）
    "fit_graph", "set_zoom", "zoom_changed",
    "toggle_grid", "toggle_thumbnails",
    "pointer_tool", "hand_tool", "space_pan", "middle_drag", "esc_cancel_gesture",
    "selection_changed", "active_node_changed",     # 选择状态
    "preview_requested",                              # viewer target 导航
    "scope_changed",                                  # 会话导航
    "compound_open_requested", "parent_requested",    # compound 导航
})

VALID_SCOPES: frozenset[str] = frozenset({
    "clip", "timeline", "group_pre_clip", "group_post_clip"})


# ── Envelope 规范化 ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class GraphIntent:
    """规范化后的领域 intent（UI → service 的唯一形态）。"""

    intent_id: str
    session_id: str
    session_generation: int
    scope: str
    scope_owner_id: str | None
    stack_id: str | None
    compound_path: tuple[str, ...]
    action: str
    command: str
    category: IntentCategory
    arguments: Mapping[str, Any]
    expected_graph_revision: int | None
    transaction_id: str | None
    # 仅诊断用（不作为 ID/工程来源；service 重新验证一切）
    draft: Any = None
    argument_gaps: tuple[str, ...] = ()


class EnvelopeError(ValueError):
    """envelope 形态非法（缺 action/scope 等结构性错误）。

    与领域 reject 不同：envelope 错误意味着 UI 侧 bug，直接抛出并保留
    原状态；领域 reject 走 reject 回滚路径。
    """


class ColorGraphIntentNormalizer:
    """panel envelope → 领域 intent（幂等、无副作用、不触 UI 控件）。"""

    def __init__(self, session_id: str, scope_owner_id: str | None,
                 stack_id: str | None, expected_graph_revision: int | None,
                 session_generation: int = 0) -> None:
        self.session_id = session_id
        self.scope_owner_id = scope_owner_id
        self.stack_id = stack_id
        self.expected_graph_revision = expected_graph_revision
        self.session_generation = session_generation

    def normalize(self, action: str, envelope: Mapping[str, Any]) -> GraphIntent:
        if not isinstance(envelope, Mapping):
            raise EnvelopeError(f"envelope 必须是 Mapping，得到 {type(envelope)!r}")
        scope = envelope.get("scope")
        if scope not in VALID_SCOPES:
            raise EnvelopeError(f"非法 scope: {scope!r}")
        compound_path = envelope.get("compound_path") or ()
        if not isinstance(compound_path, (list, tuple)):
            raise EnvelopeError("compound_path 必须是序列")
        compound_path = tuple(str(p) for p in compound_path)

        contract = ACTION_CONTRACTS.get(action)
        if contract is None:
            if action in NON_INTENT_SIGNALS:
                raise EnvelopeError(
                    f"{action!r} 是 presentation/读取信号，不构成 edit intent")
            raise EnvelopeError(f"未登记的 edit action: {action!r}（先补 ACTION_CONTRACTS）")

        payload = envelope.get("payload")
        if not isinstance(payload, Mapping):
            raise EnvelopeError(f"{action!r} 缺 payload")
        if action == "close_compound":
            # 特殊事件：内层 payload 只有 node_id/draft（E37 close_compound）
            if "node_id" not in payload:
                raise EnvelopeError("close_compound 缺 node_id")
            arguments: dict[str, Any] = {"node_id": payload.get("node_id")}
            provided = ("node_id",)
            draft = payload.get("draft")
        else:
            arguments = dict(payload.get("arguments") or {})
            provided = contract.provided_arguments
            draft = payload.get("draft")

        # add_serial 键盘 Shift+Alt+S 目前 action 仍是 add_serial、before
        # 只在 operation 里——契约要求统一携带 before（B01.4）。规范化层
        # 先按菜单语义补齐（before=False），UI 补参数前不产生二义 intent。
        if action == "add_serial" and "before" not in arguments:
            arguments["before"] = False

        gaps = tuple(
            key for key in contract.required_arguments
            if key not in arguments and key not in provided
        )

        return GraphIntent(
            intent_id=str(uuid.uuid4()),
            session_id=self.session_id,
            session_generation=self.session_generation,
            scope=scope,
            scope_owner_id=self.scope_owner_id,
            stack_id=self.stack_id,
            compound_path=compound_path,
            action=action,
            command=contract.command,
            category=contract.category,
            arguments=arguments,
            expected_graph_revision=self.expected_graph_revision,
            transaction_id=str(uuid.uuid4()),
            draft=draft,
            argument_gaps=gaps,
        )


# ── 会话状态机 + fake-service 契约 ──────────────────────────────────────

class GraphSessionState(enum.Enum):
    """UI 侧会话状态（B01 冻结：disconnected→hydrating→ready→pending→failed）。"""

    DISCONNECTED = "disconnected"
    HYDRATING = "hydrating"
    READY = "ready"
    PENDING = "pending"
    FAILED = "failed"


@dataclass(frozen=True)
class GraphProjection:
    """service → UI 的权威投影（ack 载荷）。"""

    session_id: str
    session_generation: int
    scope: str
    scope_owner_id: str | None
    graph: Any                      # 权威 GraphDraft/Graph 投影（UI 只读）
    graph_revision: int
    id_mapping: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class IntentRejection:
    """领域 reject：stable code + 目标 + 原 revision（不抛异常）。"""

    code: str
    message: str
    node_id: str | None = None
    port_id: str | None = None
    graph_revision: int | None = None


class ColorGraphSessionCoordinator:
    """单 scope 会话的 intent/ack/reject 生命周期（无 Qt，可注入 fake service）。

    service 协议（B05 接 N02 前 fake 实现，B05 后真实现）::

        service.submit_intent(intent) -> None（异步）
        service.cancel_pending(session_id, generation) -> None
        # 回调（service 在自身线程调用，coordinator 转发到 UI 线程的 sink）：
        on_ack(projection: GraphProjection, intent_id: str)
        on_reject(rejection: IntentRejection, intent_id: str)
        on_projection(projection: GraphProjection)   # 主动投影（切 clip 后）
    """

    def __init__(self, session_id: str,
                 on_load_graph: Callable[[Any, str], None],
                 on_state_changed: Optional[Callable[[GraphSessionState], None]] = None,
                 ) -> None:
        self.session_id = session_id
        self._on_load_graph = on_load_graph      # (graph, scope) UI 线程 hydrate
        self._on_state_changed = on_state_changed or (lambda state: None)
        self.state = GraphSessionState.DISCONNECTED
        self.generation = 0
        self.scope: str = "clip"
        self.scope_owner_id: str | None = None
        self.last_ack: GraphProjection | None = None
        self.pending: GraphIntent | None = None

    # ── 状态迁移 ──
    def _set_state(self, state: GraphSessionState) -> None:
        self.state = state
        self._on_state_changed(state)

    # ── hydrate（load/projection 通道；不发 intent） ──
    def begin_hydrate(self, scope: str, scope_owner_id: str | None) -> None:
        if self.state == GraphSessionState.HYDRATING:
            raise RuntimeError("hydrate 已在进行（锁未释放）")
        self.scope = scope
        self.scope_owner_id = scope_owner_id
        self._set_state(GraphSessionState.HYDRATING)

    def hydrate_projection(self, projection: GraphProjection) -> None:
        """权威投影到达：hydrate UI，无 intent 回流。"""
        self._accept(projection, source="hydrate")

    def fail_hydrate(self, reason: str) -> None:
        self._set_state(GraphSessionState.FAILED)

    # ── intent 通道 ──
    def submit(self, intent: GraphIntent) -> bool:
        """UI 编辑 intent 入口；hydrating/failed/disconnected 或跨会话 → False。"""
        if self.state not in (GraphSessionState.READY, GraphSessionState.PENDING):
            return False
        if (intent.session_id != self.session_id
                or intent.session_generation != self.generation):
            return False
        self.pending = intent
        self._set_state(GraphSessionState.PENDING)
        return True

    # ── ack / reject 通道（service 回调） ──
    def accept_ack(self, projection: GraphProjection,
                   intent_id: str | None = None) -> bool:
        """ack：匹配 session+generation+scope 才应用；晚到丢弃。"""
        if self._is_stale(projection):
            return False
        self._accept(projection, source="ack")
        if self.pending is not None and self.pending.intent_id == intent_id:
            self.pending = None
        if self.state == GraphSessionState.PENDING:
            self._set_state(GraphSessionState.READY)
        return True

    def accept_reject(self, rejection: IntentRejection,
                      intent_id: str | None = None) -> bool:
        """reject：回滚到最近 ack 投影（UI 可恢复），状态回 READY。"""
        if self.state != GraphSessionState.PENDING:
            return False
        self.pending = None
        if self.last_ack is not None:
            self._on_load_graph(self.last_ack.graph, self.last_ack.scope)
        self._set_state(GraphSessionState.READY)
        return True

    # ── clip 切换 ──
    def switch_session(self, session_id: str, scope: str,
                       scope_owner_id: str | None) -> int:
        """结束当前会话：晚到 ack/reject 全部失效；generation 递增。"""
        self.generation += 1
        self.session_id = session_id
        self.pending = None
        self.last_ack = None
        self.scope = scope
        self.scope_owner_id = scope_owner_id
        self._set_state(GraphSessionState.HYDRATING)
        return self.generation

    # ── 内部 ──
    def _is_stale(self, projection: GraphProjection) -> bool:
        return (projection.session_id != self.session_id
                or projection.session_generation != self.generation
                or projection.scope != self.scope
                or (projection.scope_owner_id != self.scope_owner_id
                    and projection.scope_owner_id is not None))

    def _accept(self, projection: GraphProjection, *, source: str) -> None:
        self.last_ack = projection
        self._on_load_graph(projection.graph, projection.scope)
        if self.state != GraphSessionState.READY:
            self._set_state(GraphSessionState.READY)


# ── B05：intent → 工程 command 桥（纯 Python，B06 再挂 Qt 面板） ────────

class GraphProjectBridge:
    """规范化 intent → ApplyColorGraphCommand → ProjectCommandStack。

    职责（B05.3/B05.4）：
    - ``submit``：ready/pending 语义沿用
      :class:`ColorGraphSessionCoordinator`；接项目栈后 undo/redo 走
      ``graph_undo/graph_redo`` 命令（不再回退 UI draft）。
    - 事务合并：同 ``transaction_id`` 的连续 intent 由
      :class:`GraphTransactionBuffer` 合并为一个 snapshot command。
    - 栈锁：locked 栈的提交在 command.apply 冲突（code=stack_locked），
      bridge 转成 reject 回调。
    - shared fanout：定义参数更新跨栈广播——每个引用栈一个 command，
      一次 undo 全还原（undo 还原依赖索引由整图快照天然承载）。
    """

    def __init__(self, coordinator: ColorGraphSessionCoordinator,
                 project_stack, get_project, set_project=None) -> None:
        """:param project_stack: ProjectCommandStack；:param get_project:
        返回当前 EditProject 的零参调用；:param set_project: 命令执行后
        发布新工程的回调（B06 挂宿主替换入口；缺省只读）。"""
        self._coordinator = coordinator
        self._project_stack = project_stack
        self._get_project = get_project
        self._set_project = set_project or (lambda _p: None)
        self._buffer = GraphTransactionBuffer()

    def _publish(self, project) -> None:
        self._set_project(project)

    # ── 单发 intent ──

    def submit(self, intent: GraphIntent) -> bool:
        """提交单事务 intent；成功返回 True 并更新协调器投影。"""
        project = self._get_project()
        stack = project.color_stacks.get(intent.stack_id) \
            if intent.stack_id else None
        if stack is None:
            return False
        expected = intent.expected_graph_revision
        if expected is None and stack.color_graph is not None:
            expected = stack.color_graph.graph_revision
        command = ApplyColorGraphCommand.create(
            stack_id=intent.stack_id,
            operation=intent.command,
            arguments=dict(intent.arguments),
            expected_graph_revision=expected,
            transaction_id=intent.transaction_id,
        )
        return self._execute(command, intent)

    # ── 事务合并（参数拖动等手势） ──

    def begin_transaction(self, transaction_id: str) -> None:
        project = self._get_project()
        graph = self._current_graph(project)
        from src.features.color.core.color_graph_migration import (
            migrate_legacy_stack,
        )
        if graph is None:
            stack = self._current_stack(project)
            graph = migrate_legacy_stack(stack) if stack else None
        self._buffer.begin(graph.to_dict() if graph else {}, transaction_id)

    def add_to_transaction(self, result, transaction_id: str,
                           operation: str = "") -> None:
        """把 service 成功结果（GraphEditResult）并入当前事务。"""
        self._buffer.add(result.graph.to_dict(), transaction_id,
                         operation, dict(result.id_mapping))

    def flush_transaction(self) -> bool:
        command = self._buffer.flush(self._stack_id())
        if command is None:
            return False
        intent = GraphIntent(
            intent_id=command.command_id,
            session_id=self._coordinator.session_id,
            session_generation=self._coordinator.generation,
            scope=self._coordinator.scope,
            scope_owner_id=self._coordinator.scope_owner_id,
            stack_id=self._stack_id(),
            compound_path=(),
            action="apply_snapshot", command="apply_snapshot",
            category=IntentCategory.STRUCTURE,
            arguments={}, expected_graph_revision=None,
            transaction_id=command.transaction_id)
        return self._execute(command, intent)

    def _undo_once(self) -> bool:
        """接通模式 undo：项目栈执行并统一投影（不回退 UI draft）。"""
        try:
            undone = self._project_stack.undo(self._get_project())
        except Exception:  # noqa: BLE001
            return False
        if undone is None:
            return False
        self._publish(undone)
        stack = self._current_stack(undone)
        if stack is not None and stack.color_graph is not None:
            self._coordinator.accept_ack(GraphProjection(
                session_id=self._coordinator.session_id,
                session_generation=self._coordinator.generation,
                scope=self._coordinator.scope,
                scope_owner_id=self._coordinator.scope_owner_id,
                graph=stack.color_graph.to_dict(),
                graph_revision=stack.color_graph.graph_revision))
        return True

    def _redo_once(self) -> bool:
        """接通模式 redo：同 _undo_once。"""
        try:
            redone = self._project_stack.redo(self._get_project())
        except Exception:  # noqa: BLE001
            return False
        if redone is None:
            return False
        self._publish(redone)
        stack = self._current_stack(redone)
        if stack is not None and stack.color_graph is not None:
            self._coordinator.accept_ack(GraphProjection(
                session_id=self._coordinator.session_id,
                session_generation=self._coordinator.generation,
                scope=self._coordinator.scope,
                scope_owner_id=self._coordinator.scope_owner_id,
                graph=stack.color_graph.to_dict(),
                graph_revision=stack.color_graph.graph_revision))
        return True

    # ── shared 定义 fanout（B05.4） ──

    def update_shared_definition(self, definition_id: str,
                                 params: Mapping[str, Any]) -> list[str]:
        """跨栈更新共享定义：所有引用栈一个 command、一起失效。"""
        project = self._get_project()
        updated: list[str] = []
        for stack_id, stack in list(project.color_stacks.items()):
            graph = stack.color_graph
            if graph is None or not any(
                    d.definition_id == definition_id
                    for d in graph.shared_definitions):
                continue
            command = ApplyColorGraphCommand.create(
                stack_id=stack_id,
                operation="update_shared_definition_params",
                arguments={"definition_id": definition_id,
                           "params": dict(params)},
            )
            try:
                project = self._project_stack.execute(command, project)
                self._publish(project)
                updated.append(stack_id)
            except Exception:  # noqa: BLE001 —— fanout 单栈失败不阻断其余
                continue
        return updated

    def set_stack_lock(self, stack_id: str, locked: bool) -> bool:
        """栈锁是工程元信息：走 SetColorGraphLockCommand（可 undo）。"""
        project = self._get_project()
        stack = project.color_stacks.get(stack_id)
        if stack is None:
            return False
        command = SetColorGraphLockCommand.create(stack_id, locked)
        try:
            self._publish(self._project_stack.execute(command, project))
            return True
        except Exception:  # noqa: BLE001
            return False

    # ── 内部 ──

    def _stack_id(self) -> str:
        return getattr(self._coordinator, "_bridge_stack_id", "") or ""

    def bind_stack(self, stack_id: str) -> None:
        self._coordinator._bridge_stack_id = stack_id

    def _current_project(self):
        return self._get_project()

    def _current_stack(self, project):
        stack_id = self._stack_id()
        return project.color_stacks.get(stack_id) if stack_id else None

    def _current_graph(self, project):
        stack = self._current_stack(project)
        return stack.color_graph if stack else None

    def _execute(self, command: ApplyColorGraphCommand,
                 intent: GraphIntent) -> bool:
        if not self._coordinator.submit(intent):
            return False
        try:
            new_project = self._project_stack.execute(
                command, self._get_project())
            self._publish(new_project)
        except Exception as exc:  # noqa: BLE001 —— 冲突 → reject
            self._coordinator.pending = None
            if self._coordinator.state == GraphSessionState.PENDING:
                self._coordinator._set_state(GraphSessionState.READY)
            code = getattr(exc, "code", "command_failed")
            self._coordinator.accept_reject(
                IntentRejection(code=code, message=str(exc),
                                graph_revision=command.expected_graph_revision),
                intent_id=intent.intent_id)
            return False
        project = self._get_project()
        stack = project.color_stacks.get(command.stack_id)
        graph = stack.color_graph if stack else None
        projection = GraphProjection(
            session_id=self._coordinator.session_id,
            session_generation=self._coordinator.generation,
            scope=self._coordinator.scope,
            scope_owner_id=self._coordinator.scope_owner_id,
            graph=graph.to_dict() if graph else None,
            graph_revision=graph.graph_revision if graph else 0,
            id_mapping=command.get_id_mapping())
        self._coordinator.accept_ack(projection, intent_id=intent.intent_id)
        return True


# ── B06：Corrector 句柄 / 面板宿主 / 旧面板适配 ─────────────────────────

@dataclass(frozen=True)
class CorrectorHandle:
    """活动 Corrector 的只读句柄（B06.1）。

    身份四元组 ``(session_id, scope_owner_id, compound_path, node_id)``；
    **显示序号永远不能**映射 ``engine.layers[index]``——本类型只经
    node_id 构造。参数来自权威图投影（不可变 mappingproxy 冻结形态）。
    """

    session_id: str
    scope_owner_id: str | None
    compound_path: tuple[str, ...]
    node_id: str
    node_type: str
    label: str
    enabled: bool
    params: Mapping[str, Any]

    @property
    def is_corrector(self) -> bool:
        from src.shared.project.color_graph import NodeType
        return self.node_type in (NodeType.CORRECTOR.value,
                                  NodeType.SHARED_CORRECTOR.value)

    def grade_params(self) -> Mapping[str, Any]:
        """图侧调色参数域（legacy Layer.grading_params 的图内形态）。"""
        grade = self.params.get("grade")
        return grade if isinstance(grade, Mapping) else {}

    def qualifier_params(self) -> Mapping[str, Any]:
        qualifier = self.params.get("qualifier")
        return qualifier if isinstance(qualifier, Mapping) else {}

    def shape_ids(self) -> tuple[str, ...]:
        windows = self.params.get("windows")
        if isinstance(windows, Mapping):
            return tuple(sorted(windows.keys()))
        if isinstance(windows, (tuple, list)):
            return tuple(str(s) for s in windows)
        return ()


class GraphHandleResolver:
    """权威图投影 + node_id → CorrectorHandle（无 engine 索引路径）。"""

    def __init__(self, session_id: str, scope_owner_id: str | None,
                 compound_path: tuple[str, ...] = ()) -> None:
        self.session_id = session_id
        self.scope_owner_id = scope_owner_id
        self.compound_path = tuple(compound_path)

    def resolve(self, graph_payload: Any, node_id: str) -> CorrectorHandle | None:
        if not isinstance(graph_payload, Mapping):
            return None
        for node in graph_payload.get("nodes") or ():
            if not isinstance(node, Mapping) or node.get("node_id") != node_id:
                continue
            params = node.get("params") or {}
            if not isinstance(params, Mapping):
                params = {}
            return CorrectorHandle(
                session_id=self.session_id,
                scope_owner_id=self.scope_owner_id,
                compound_path=self.compound_path,
                node_id=node_id,
                node_type=str(node.get("node_type") or ""),
                label=str(node.get("label") or ""),
                enabled=bool(node.get("enabled", True)),
                params=params)
        return None


class ColorGraphPanelHost:
    """把真实 ``ColorNodePanel`` 接到 bridge/coordinator（B06.1/B06.3/B06.4）。

    Qt 信号在 ``attach_panel`` 连接（宿主装配期）；本类自身不 import
    PyQt——panel 以 duck-typed 信号对象注入（测试可用真面板或 fake）。

    - **防递归锁**：``on_projection`` 装载权威投影期间屏蔽 edit intents
      （程序更新不回流）；装载结束解锁。
    - **晚到事件**：coordinator 的 generation/ session 匹配天然丢弃；
      选择/缩略图按当前投影句柄过滤。
    - **undo/redo**：接通模式走项目 command 栈并统一投影（B05 契约），
      不回退 UI draft。
    """

    def __init__(self, bridge: GraphProjectBridge,
                 coordinator: ColorGraphSessionCoordinator) -> None:
        self._bridge = bridge
        self._coordinator = coordinator
        self._panel: Any = None
        self._sync_lock = False
        self._active_node_id = ""
        self._active_handle: CorrectorHandle | None = None
        self._read_only_handlers: list[Callable[[str, str], None]] = []
        self._handle_handlers: list[Callable[[CorrectorHandle], None]] = []

    # ── 装配 ──

    def attach_panel(self, panel: Any) -> None:
        """连接面板信号（Qt 信号 duck-typed）。"""
        self._panel = panel
        panel.edit_requested.connect(self._on_edit_requested)
        panel.active_node_changed.connect(self._on_active_node_changed)
        panel.scope_changed.connect(self._on_scope_changed)

    def on_read_only(self, handler: Callable[[str, str], None]) -> None:
        """注册"非 Corrector 选择 → 只读"回调（node_id, node_type）。"""
        self._read_only_handlers.append(handler)

    def on_handle(self, handler: Callable[[CorrectorHandle], None]) -> None:
        """注册活动句柄回调（参数面板读取入口，B06.2 读路径）。"""
        self._handle_handlers.append(handler)

    # ── 投影（service → UI） ──

    @property
    def active_handle(self) -> CorrectorHandle | None:
        return self._active_handle

    def on_projection(self, projection: GraphProjection) -> None:
        """权威投影到达：锁内 hydrate 面板，无 intent 回流。"""
        if self._coordinator._is_stale(projection):
            return
        if self._panel is None:
            return
        self._sync_lock = True
        try:
            draft = graph_payload_to_draft(projection.graph)
            self._panel.load_graph(draft, scope=projection.scope)
        finally:
            self._sync_lock = False
        self._refresh_handle(projection)

    def _refresh_handle(self, projection: GraphProjection) -> None:
        resolver = GraphHandleResolver(
            projection.session_id, projection.scope_owner_id)
        handle = (resolver.resolve(projection.graph, self._active_node_id)
                  if self._active_node_id else None)
        if handle is not None and handle.is_corrector:
            self._active_handle = handle
            for handler in self._handle_handlers:
                handler(handle)
        elif handle is not None:
            # B06.3：Mixer/Source/AlphaOutput 等选择 → 只读，不改隐藏层
            self._active_handle = None
            for handler in self._read_only_handlers:
                handler(handle.node_id, handle.node_type)
        else:
            self._active_handle = None

    # ── UI → service ──

    def _on_edit_requested(self, action: str, envelope: Any) -> None:
        if self._sync_lock:
            return  # hydrate 装载期间不回流 intent
        if self._coordinator.state not in (GraphSessionState.READY,
                                           GraphSessionState.PENDING):
            return
        try:
            normalizer = ColorGraphIntentNormalizer(
                session_id=self._coordinator.session_id,
                scope_owner_id=self._coordinator.scope_owner_id,
                stack_id=self._bridge._stack_id(),
                expected_graph_revision=(
                    self._coordinator.last_ack.graph_revision
                    if self._coordinator.last_ack else None),
                session_generation=self._coordinator.generation)
            intent = normalizer.normalize(action, envelope)
        except EnvelopeError:
            return  # 契约外 action：静默忽略（B01 门禁另有测试兜底）
        self._bridge.submit(intent)
        if self._coordinator.last_ack is not None:
            self.on_projection(self._coordinator.last_ack)

    def _on_active_node_changed(self, node_id: str) -> None:
        if self._sync_lock:
            return  # 程序更新引发的选择变化不触发读路径
        self._active_node_id = str(node_id)
        if self._coordinator.last_ack is not None:
            self._refresh_handle(self._coordinator.last_ack)

    def _on_scope_changed(self, scope: str) -> None:
        # scope 切换是导航：宿主 (B06.4) 负责按真实 owner 重新水合；
        # 此处只拒绝把当前 draft 保存进新 scope
        if scope not in VALID_SCOPES:
            return
        self._pending_scope = scope

    # ── clip/asset/project 切换（B06.4） ──

    def on_clip_switch(self, new_session_id: str,
                       scope_owner_id: str | None,
                       *, flush_pending: bool = True) -> str:
        """切换协议：结束或取消当前事务 → generation++ → 重新水合。"""
        if flush_pending and self._bridge._buffer.active:
            self._bridge.flush_transaction()
        else:
            self._bridge._buffer = GraphTransactionBuffer()  # 丢弃
        generation = self._coordinator.switch_session(
            new_session_id, self._coordinator.scope, scope_owner_id)
        return generation

    # ── 历史（接通模式：项目栈） ──

    def undo(self) -> bool:
        return self._bridge._undo_once()

    def redo(self) -> bool:
        return self._bridge._redo_once()


class LegacyGradingAdapter:
    """旧 GradingPanel（面向 Layer 对象）↔ 图 Corrector 过渡适配（B06.2）。

    读路径：``params_for_panel`` 把 handle.grade_params 投影成面板
    set_params 形态；写路径：``submit_param_change`` 以事务合并提交
    set_node_option('grade', …) command——面板手势不再直写
    engine.layers（legacy 模式除外）。
    """

    def __init__(self, host: ColorGraphPanelHost,
                 bridge: GraphProjectBridge) -> None:
        self._host = host
        self._bridge = bridge
        self._gesture_open = False

    def params_for_panel(self) -> dict[str, Any]:
        handle = self._host.active_handle
        return dict(handle.grade_params()) if handle else {}

    def begin_gesture(self, transaction_id: str) -> None:
        self._bridge.begin_transaction(transaction_id)
        self._gesture_open = True

    def submit_param_change(self, node_id: str, param_name: str, value: Any,
                            transaction_id: str) -> bool:
        """单参数拖动：图内 grade 域 patch，并入当前手势事务。"""
        from src.features.color.core.color_graph_service import (
            ColorGraphService,
        )
        project = self._bridge._get_project()
        stack_id = self._bridge._stack_id()
        stack = project.color_stacks.get(stack_id) if stack_id else None
        graph = stack.color_graph if stack else None
        if graph is None:
            return False
        # 图侧先应用（服务端验证），再并入事务
        node = next((n for n in graph.nodes if n.node_id == node_id), None)
        if node is None:
            return False
        grade = dict(node.params.get("grade") or {})
        grade[param_name] = value
        counter = [0]

        def _factory() -> str:
            counter[0] += 1
            return f"gesture-{transaction_id}-{counter[0]}"

        service = ColorGraphService(id_factory=_factory)
        result = service.op_set_node_option(
            graph, node_id, "grade_patch", (param_name, value))
        if not result.ok:
            return False
        self._bridge.add_to_transaction(result, transaction_id,
                                        "set_node_option")
        return True

    def end_gesture(self) -> bool:
        if not self._gesture_open:
            return False
        self._gesture_open = False
        return self._bridge.flush_transaction()


# ── 权威图 → 面板 GraphDraft 投影（单向；draft 只是提案展示） ────────────

def graph_payload_to_draft(payload: Mapping[str, Any]):
    """ColorGraphData.to_dict() → 面板 GraphDraft（只读投影）。

    kind 值域与 NodeType 一致（shared_corrector→corrector 带 shared_id，
    outside→corrector 带 label 前缀，draft 无独立类型）。端口名按
    ``node_ports`` 的 presentation 约定重映射（image↔image_in/out、
    mixer 槽序号、splitter 通道名）。
    """
    from src.features.color.ui.node_editor_state import (
        ConnectionDraft,
        GraphDraft,
        NodeDraft,
        NodeKind,
    )

    kind_alias = {
        "shared_corrector": "corrector",
        "outside": "corrector",
    }
    ui = payload.get("ui_state") or {}
    positions = ui.get("positions") or {}

    nodes: dict[str, object] = {}
    for node in payload.get("nodes") or ():
        node_id = node.get("node_id")
        raw_kind = str(node.get("node_type") or "corrector")
        kind_value = kind_alias.get(raw_kind, raw_kind)
        try:
            kind = NodeKind(kind_value)
        except ValueError:
            kind = NodeKind.CORRECTOR  # 未知类型只读展示为 corrector 形态
        params = node.get("params") or {}
        locked = bool(params.get("locked", False))
        cache_mode = str(params.get("cache_mode", "auto"))
        slots = params.get("slots")
        nodes[node_id] = NodeDraft(
            id=node_id,
            kind=kind,
            label=str(node.get("label") or ""),
            x=float((positions.get(node_id) or (0.0, 0.0))[0]),
            y=float((positions.get(node_id) or (0.0, 0.0))[1]),
            number=0,  # load_graph 内部重编号
            enabled=bool(node.get("enabled", True)),
            locked=locked,
            color=str(params.get("ui_color", "") or ""),
            input_count=max(2, len(slots) if isinstance(slots, (list, tuple))
                            else 2),
            grade=dict(params.get("grade") or {}),
            source_layer_id=str(params.get("source_layer_id", "") or ""),
            cache_mode=cache_mode,
            shared_id=str(node.get("shared_definition_id", "") or ""),
        )

    def _port_name(node_id: str, port: str, out: bool, slot: str) -> str:
        node = payload_nodes_by_id.get(node_id) or {}
        kind_value = kind_alias.get(
            str(node.get("node_type") or ""), str(node.get("node_type") or ""))
        if kind_value in ("parallel_mixer", "layer_mixer"):
            if port in ("base", "branch", "slot"):
                index = 0
                if slot:
                    tail = slot.rsplit("/", 1)[-1]
                    if tail.isdigit():
                        index = int(tail)
                if port == "base":
                    index = 0
                elif port == "branch":
                    index = index + 1  # draft 槽 0 留给 base
                return f"image_in_{index}"
            return "image_out"
        if kind_value == "key_mixer":
            if port == "key":
                index = 0
                if slot:
                    tail = slot.rsplit("/", 1)[-1]
                    if tail.isdigit():
                        index = int(tail)
                return f"key_in_{index}"
            return "key_out"
        if kind_value == "splitter":
            return {"image": "image_in", "r": "red", "g": "green",
                    "b": "blue"}.get(port, port)
        if kind_value == "combiner":
            return {"r": "red", "g": "green", "b": "blue",
                    "image": "image_out"}.get(port, port)
        # source/output/alpha/corrector/compound
        if port == "image":
            return "image_out" if out else "image_in"
        return port  # key_in / key_out 同名

    payload_nodes_by_id = {n.get("node_id"): n
                           for n in payload.get("nodes") or ()}

    connections = []
    for edge in payload.get("edges") or ():
        connections.append(ConnectionDraft(
            src_node=str(edge.get("src_node")),
            src_port=_port_name(str(edge.get("src_node")),
                                str(edge.get("src_port")), True,
                                str(edge.get("slot_id") or "")),
            dst_node=str(edge.get("dst_node")),
            dst_port=_port_name(str(edge.get("dst_node")),
                                str(edge.get("dst_port")), False,
                                str(edge.get("slot_id") or "")),
        ))

    return GraphDraft(
        nodes=nodes,
        connections=connections,
        active_id=str(ui.get("active_node_id") or ""),
        selected_ids=set(ui.get("selected_ids") or ()),
        next_number=1,
    )
