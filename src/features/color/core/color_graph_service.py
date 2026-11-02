"""ColorGraphService —— 图编辑/验证的领域服务（N02，B04）。

纯 CPU、无 Qt/GL/engine。每个操作都是**一次原子事务**：

    验证前置条件 → 构建候选图（新对象，不碰原图）→ 候选图全量校验
    → 通过：返回新图（render 语义变更 bump graph_revision）；
      失败：返回原 图 + 稳定诊断（图完全不变，可恢复）。

诊断沿用 :class:`GraphSchemaError` 的稳定 code；``apply_intent`` 是
N08 规范化 intent 的统一入口（B01 ACTION_CONTRACTS.command 分派）。

边 ID 由端点 uuid5 派生（同端点同 ID——重连/重建确定性）；节点 ID 由
可注入 ``id_factory`` 分配（测试可注入确定性工厂）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping, Sequence

from src.shared.project.color_graph import (
    ColorEdgeData,
    ColorGraphData,
    ColorGraphUiState,
    ColorNodeData,
    GraphSchemaError,
    NodeType,
    PortDirection,
    PortKind,
    SharedDefinitionData,
)


@dataclass(frozen=True)
class GraphEditResult:
    """一次事务的结果：成功带新图与 ID 映射；失败原图原样带回。"""

    ok: bool
    graph: ColorGraphData
    diagnostics: tuple[GraphSchemaError, ...] = ()
    id_mapping: Mapping[str, str] = field(default_factory=dict)

    @property
    def primary_code(self) -> str | None:
        return self.diagnostics[0].code if self.diagnostics else None


def _edge_id(src: str, src_port: str, dst: str, dst_port: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"topos-color-edge/{src}/{src_port}->{dst}/{dst_port}"))


def _make_edge(src: str, src_port: str, dst: str, dst_port: str,
               slot_id: str = "") -> ColorEdgeData:
    return ColorEdgeData(edge_id=_edge_id(src, src_port, dst, dst_port),
                         src_node=src, src_port=src_port,
                         dst_node=dst, dst_port=dst_port, slot_id=slot_id)


class ColorGraphService:
    """结构/参数/布局编辑服务（B04 全量操作 + B05 command 的执行体）。"""

    def __init__(self,
                 id_factory: Callable[[], str] | None = None) -> None:
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))

    # ── 统一 intent 入口（N08 → N02） ──

    def apply_intent(self, graph: ColorGraphData, command: str,
                     arguments: Mapping[str, Any]) -> GraphEditResult:
        """按 B01 契约表的 command 名分派；未知 command 显式拒绝。"""
        handler = getattr(self, f"op_{command}", None)
        if handler is None or command.startswith("_"):
            return GraphEditResult(
                False, graph,
                (GraphSchemaError("unknown_command",
                                  f"未实现的图命令: {command}"),))
        return handler(graph, **dict(arguments)) if arguments else handler(graph)

    # ── 内部工具 ──

    def _fail(self, graph: ColorGraphData, code: str, message: str, *,
              node_id: str | None = None) -> GraphEditResult:
        return GraphEditResult(False, graph,
                               (GraphSchemaError(code, message, node_id=node_id),))

    def _commit(self, graph: ColorGraphData, candidate: ColorGraphData,
                mapping: Mapping[str, str], *,
                render_change: bool = True) -> GraphEditResult:
        errors = candidate.validate()
        if errors:
            return GraphEditResult(False, graph, tuple(errors))
        if render_change:
            candidate = replace(candidate,
                                graph_revision=graph.graph_revision + 1)
        return GraphEditResult(True, candidate, (), mapping)

    def _node(self, graph: ColorGraphData, node_id: str) -> ColorNodeData | None:
        return next((n for n in graph.nodes if n.node_id == node_id), None)

    def _edges_into(self, graph: ColorGraphData, node_id: str,
                    port: str | None = None) -> list[ColorEdgeData]:
        return [e for e in graph.edges
                if e.dst_node == node_id and (port is None or e.dst_port == port)]

    def _edges_out(self, graph: ColorGraphData, node_id: str,
                   port: str | None = None) -> list[ColorEdgeData]:
        return [e for e in graph.edges
                if e.src_node == node_id and (port is None or e.src_port == port)]

    def _upstream_edge(self, graph: ColorGraphData,
                       node_id: str) -> ColorEdgeData | None:
        ins = self._edges_into(graph, node_id, "image")
        return ins[0] if len(ins) == 1 else None

    def _downstream_edge(self, graph: ColorGraphData,
                         node_id: str) -> ColorEdgeData | None:
        outs = self._edges_out(graph, node_id, "image")
        return outs[0] if len(outs) == 1 else None

    def _check_port(self, graph: ColorGraphData, node_id: str, port_id: str,
                    direction: PortDirection) -> GraphSchemaError | None:
        node = self._node(graph, node_id)
        if node is None:
            return GraphSchemaError("node_not_found",
                                    f"节点不存在: {node_id}", node_id=node_id)
        spec = next((p for p in node.ports()
                     if p.port_id == port_id
                     and p.direction is direction), None)
        if spec is None:
            return GraphSchemaError(
                "port_not_found",
                f"端口 {node_id}.{port_id}({direction.value}) 不存在",
                node_id=node_id)
        return None

    # ════════════════════════════════════════════════════════════════
    # 串行插入 / 加节点 / 连接
    # ════════════════════════════════════════════════════════════════

    def op_insert_corrector(self, graph: ColorGraphData,
                            active_node_id: str | None = None,
                            before: bool = False) -> GraphEditResult:
        """在 active 节点之后（或之前）插入空 Corrector（serial 命令）。

        策略：anchor 是 Corrector 时按其 image 边拆入；anchor 是
        Source/Output 时在相邻边内插入；无合法插入点返回诊断。
        """
        anchor_id = active_node_id or graph.source_node_id
        anchor = self._node(graph, anchor_id)
        if anchor is None:
            return self._fail(graph, "node_not_found",
                              f"插入锚点不存在: {anchor_id}", node_id=anchor_id)
        if before:
            edge = self._upstream_edge(graph, anchor_id)
            if edge is None:
                return self._fail(graph, "edge_endpoint_missing",
                                  f"{anchor_id} 无上游 image 边，无法 before 插入",
                                  node_id=anchor_id)
            src, dst = edge.src_node, anchor_id
        else:
            edge = self._downstream_edge(graph, anchor_id)
            if edge is None:
                return self._fail(graph, "edge_endpoint_missing",
                                  f"{anchor_id} 无下游 image 边，无法 after 插入",
                                  node_id=anchor_id)
            src, dst = anchor_id, edge.dst_node

        new_id = self._id_factory()
        node = ColorNodeData(node_id=new_id, node_type=NodeType.CORRECTOR.value,
                             params={"key_source_mode": "node_input"})
        kept = tuple(e for e in graph.edges
                     if not (e.src_node == src and e.dst_node == dst
                             and e.src_port == "image" and e.dst_port == "image"))
        edges = kept + (_make_edge(src, "image", new_id, "image"),
                        _make_edge(new_id, "image", dst, "image"))
        candidate = replace(graph, nodes=graph.nodes + (node,), edges=edges)
        return self._commit(graph, candidate, {new_id: new_id})

    def op_add_node(self, graph: ColorGraphData, kind: str,
                    position: Sequence[float] = (0.0, 0.0)
                    ) -> GraphEditResult:
        """未连接新节点（§6.3：允许保留，导出只取依赖锥）。"""
        try:
            node_type = NodeType(kind)
        except ValueError:
            return self._fail(graph, "unknown_node_type", f"未知节点类型: {kind}")
        if node_type in (NodeType.SOURCE, NodeType.OUTPUT):
            return self._fail(graph, "terminal_count",
                              f"禁止新增 {node_type.value}（端子唯一）")
        new_id = self._id_factory()
        params = ({"key_source_mode": "node_input"}
                  if node_type is NodeType.CORRECTOR else {})
        node = ColorNodeData(node_id=new_id, node_type=node_type.value,
                             params=params)
        ui = replace(graph.ui_state,
                     positions={**dict(graph.ui_state.positions),
                                new_id: (float(position[0]), float(position[1]))})
        candidate = replace(graph, nodes=graph.nodes + (node,), ui_state=ui)
        # 未连接节点结构合法（孤立允许），revision 仍 bump：图内容变了
        return self._commit(graph, candidate, {new_id: new_id})

    def op_connect_ports(self, graph: ColorGraphData, src_node: str,
                         src_port: str, dst_node: str, dst_port: str,
                         *, replace_existing: bool = True) -> GraphEditResult:
        """先完整验证新边，成功后才替换旧边（§6.2 原则）。"""
        for node_id, port_id, direction in (
                (src_node, src_port, PortDirection.OUT),
                (dst_node, dst_port, PortDirection.IN)):
            err = self._check_port(graph, node_id, port_id, direction)
            if err is not None:
                return GraphEditResult(False, graph, (err,))
        src_node_obj = self._node(graph, src_node)
        dst_spec = next(
            p for p in self._node(graph, dst_node).ports()
            if p.port_id == dst_port and p.direction is PortDirection.IN)
        src_spec = next(
            p for p in src_node_obj.ports()
            if p.port_id == src_port and p.direction is PortDirection.OUT)
        if src_spec.kind is not dst_spec.kind:
            return self._fail(
                graph, "port_kind",
                f"禁止 {src_spec.kind.value}→{dst_spec.kind.value} 隐式转换")
        edges = list(graph.edges)
        if not dst_spec.multi:
            # 单输入端口：验证通过后原子替换旧边
            existing = [e for e in edges
                        if e.dst_node == dst_node and e.dst_port == dst_port]
            if existing and not replace_existing:
                return self._fail(graph, "port_multi",
                                  f"{dst_node}.{dst_port} 已有连接")
            edges = [e for e in edges if e not in existing]
        # 成环检查：dst 的依赖锥不得包含 src
        reach = self._downstream_closure(graph, dst_node)
        if src_node in reach or src_node == dst_node:
            return self._fail(graph, "cycle",
                              f"连接 {src_node}→{dst_node} 会成环",
                              node_id=dst_node)
        new_edge = _make_edge(src_node, src_port, dst_node, dst_port)
        candidate = replace(graph, edges=tuple(edges) + (new_edge,))
        return self._commit(graph, candidate,
                            {new_edge.edge_id: new_edge.edge_id})

    def _downstream_closure(self, graph: ColorGraphData,
                            start: str) -> set[str]:
        adjacency: dict[str, list[str]] = {}
        for e in graph.edges:
            adjacency.setdefault(e.src_node, []).append(e.dst_node)
        seen = {start}
        stack = [start]
        while stack:
            for nxt in adjacency.get(stack.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    def op_disconnect_edge(self, graph: ColorGraphData,
                           edge_id: str) -> GraphEditResult:
        edge = next((e for e in graph.edges if e.edge_id == edge_id), None)
        if edge is None:
            return self._fail(graph, "edge_endpoint_missing",
                              f"边不存在: {edge_id}")
        candidate = replace(
            graph, edges=tuple(e for e in graph.edges if e.edge_id != edge_id))
        return self._commit(graph, candidate, {})

    # ════════════════════════════════════════════════════════════════
    # 分支结构（parallel/layer/outside/splitter/key mixer/alpha）
    # ════════════════════════════════════════════════════════════════

    def op_add_parallel_branch(self, graph: ColorGraphData,
                               branch_origin_id: str | None = None
                               ) -> GraphEditResult:
        """在 origin 与其下游之间加 Parallel Mixer + 空分支 Corrector。

        origin.image 原下游边改接 mixer.base；新建分支 C 与 mixer；
        C 挂 mixer.branch[0]；mixer.image → 原下游。
        """
        origin_id = branch_origin_id or graph.source_node_id
        down = self._downstream_edge(graph, origin_id)
        if down is None:
            return self._fail(graph, "edge_endpoint_missing",
                              f"{origin_id} 无唯一下游 image 边（分支需先有链）",
                              node_id=origin_id)
        mixer_id, branch_id = self._id_factory(), self._id_factory()
        mixer = ColorNodeData(node_id=mixer_id,
                              node_type=NodeType.PARALLEL_MIXER.value)
        branch = ColorNodeData(
            node_id=branch_id, node_type=NodeType.CORRECTOR.value,
            params={"key_source_mode": "node_input"})
        edges = [e for e in graph.edges if e.edge_id != down.edge_id]
        edges += [
            _make_edge(origin_id, "image", mixer_id, "base"),
            _make_edge(origin_id, "image", branch_id, "image"),
            _make_edge(branch_id, "image", mixer_id, "branch",
                       slot_id=f"{mixer_id}/branch/0"),
            _make_edge(mixer_id, "image", down.dst_node, "image"),
        ]
        candidate = replace(graph,
                            nodes=graph.nodes + (mixer, branch),
                            edges=tuple(edges))
        return self._commit(graph, candidate,
                            {mixer_id: mixer_id, branch_id: branch_id})

    def op_add_layer_branch(self, graph: ColorGraphData,
                            base_input_id: str | None = None
                            ) -> GraphEditResult:
        """Layer Mixer：有序 slot（由下到上），空分支占位 slot 0。"""
        origin_id = base_input_id or graph.source_node_id
        down = self._downstream_edge(graph, origin_id)
        if down is None:
            return self._fail(graph, "edge_endpoint_missing",
                              f"{origin_id} 无唯一下游 image 边", node_id=origin_id)
        mixer_id, branch_id = self._id_factory(), self._id_factory()
        mixer = ColorNodeData(node_id=mixer_id,
                              node_type=NodeType.LAYER_MIXER.value)
        branch = ColorNodeData(
            node_id=branch_id, node_type=NodeType.CORRECTOR.value,
            params={"key_source_mode": "node_input"})
        edges = [e for e in graph.edges if e.edge_id != down.edge_id]
        edges += [
            _make_edge(origin_id, "image", mixer_id, "slot",
                       slot_id=f"{mixer_id}/slot/0"),   # 基底（最底）
            _make_edge(origin_id, "image", branch_id, "image"),
            _make_edge(branch_id, "image", mixer_id, "slot",
                       slot_id=f"{mixer_id}/slot/1"),   # 分支压在基底上
            _make_edge(mixer_id, "image", down.dst_node, "image"),
        ]
        candidate = replace(graph,
                            nodes=graph.nodes + (mixer, branch),
                            edges=tuple(edges))
        return self._commit(graph, candidate,
                            {mixer_id: mixer_id, branch_id: branch_id})

    def op_add_outside_corrector(self, graph: ColorGraphData,
                                 source_key_node_id: str) -> GraphEditResult:
        """Outside：引用原节点 key_out 的补集；颜色输入显式连接。"""
        source = self._node(graph, source_key_node_id)
        if source is None:
            return self._fail(graph, "node_not_found",
                              f"key 源节点不存在: {source_key_node_id}",
                              node_id=source_key_node_id)
        key_out_spec = next(
            (p for p in source.ports()
             if p.port_id == "key_out" and p.direction is PortDirection.OUT),
            None)
        if key_out_spec is None:
            return self._fail(graph, "port_not_found",
                              f"{source_key_node_id} 无 key_out（Corrector 才有 outside）",
                              node_id=source_key_node_id)
        down = self._downstream_edge(graph, source_key_node_id)
        if down is None:
            return self._fail(graph, "edge_endpoint_missing",
                              f"{source_key_node_id} 无唯一下游 image 边",
                              node_id=source_key_node_id)
        outside_id = self._id_factory()
        outside = ColorNodeData(
            node_id=outside_id, node_type=NodeType.OUTSIDE.value,
            params={"key_source_mode": "node_input",
                    "outside_key_source": source_key_node_id})
        edges = [e for e in graph.edges if e.edge_id != down.edge_id]
        edges += [
            _make_edge(source_key_node_id, "image", outside_id, "image"),
            _make_edge(source_key_node_id, "key_out", outside_id, "key_in"),
            _make_edge(outside_id, "image", down.dst_node, "image"),
        ]
        candidate = replace(graph,
                            nodes=graph.nodes + (outside,),
                            edges=tuple(edges))
        return self._commit(graph, candidate, {outside_id: outside_id})

    def op_add_splitter_pair(self, graph: ColorGraphData,
                             node_id: str | None = None) -> GraphEditResult:
        """Splitter + Combiner 成对原子插入（§2.3 一次事务）。"""
        anchor_id = node_id or graph.source_node_id
        down = self._downstream_edge(graph, anchor_id)
        if down is None:
            return self._fail(graph, "edge_endpoint_missing",
                              f"{anchor_id} 无唯一下游 image 边", node_id=anchor_id)
        split_id, comb_id = self._id_factory(), self._id_factory()
        splitter = ColorNodeData(node_id=split_id,
                                 node_type=NodeType.SPLITTER.value)
        combiner = ColorNodeData(node_id=comb_id,
                                 node_type=NodeType.COMBINER.value)
        edges = [e for e in graph.edges if e.edge_id != down.edge_id]
        edges += [_make_edge(anchor_id, "image", split_id, "image")]
        edges += [_make_edge(split_id, ch, comb_id, ch) for ch in ("r", "g", "b")]
        edges += [_make_edge(comb_id, "image", down.dst_node, "image")]
        candidate = replace(graph,
                            nodes=graph.nodes + (splitter, combiner),
                            edges=tuple(edges))
        return self._commit(graph, candidate,
                            {split_id: split_id, comb_id: comb_id})

    def op_add_key_mixer(self, graph: ColorGraphData,
                         key_source_ids: Sequence[str] = ()
                         ) -> GraphEditResult:
        """Key Mixer：多个 key 源混合；无源时允许未连接创建。"""
        mixer_id = self._id_factory()
        mixer = ColorNodeData(node_id=mixer_id,
                              node_type=NodeType.KEY_MIXER.value)
        edges = list(graph.edges)
        for slot_index, source_id in enumerate(key_source_ids):
            source = self._node(graph, source_id)
            if source is None:
                return self._fail(graph, "node_not_found",
                                  f"key 源不存在: {source_id}", node_id=source_id)
            if not any(p.port_id == "key_out" and p.direction is PortDirection.OUT
                       for p in source.ports()):
                return self._fail(graph, "port_not_found",
                                  f"{source_id} 无 key_out", node_id=source_id)
            edges.append(_make_edge(source_id, "key_out", mixer_id, "key",
                                    slot_id=f"{mixer_id}/key/{slot_index}"))
        candidate = replace(graph, nodes=graph.nodes + (mixer,),
                            edges=tuple(edges))
        return self._commit(graph, candidate, {mixer_id: mixer_id})

    def op_add_alpha_output(self, graph: ColorGraphData,
                            key_source_id: str | None = None
                            ) -> GraphEditResult:
        if graph.alpha_output_node_id:
            return self._fail(graph, "terminal_count", "Alpha Output 已存在（≤1）")
        alpha_id = self._id_factory()
        alpha = ColorNodeData(node_id=alpha_id,
                              node_type=NodeType.ALPHA_OUTPUT.value)
        edges = list(graph.edges)
        if key_source_id is not None:
            source = self._node(graph, key_source_id)
            if source is None:
                return self._fail(graph, "node_not_found",
                                  f"key 源不存在: {key_source_id}",
                                  node_id=key_source_id)
            edges.append(_make_edge(key_source_id, "key_out", alpha_id, "key"))
        candidate = replace(graph, nodes=graph.nodes + (alpha,),
                            edges=tuple(edges),
                            alpha_output_node_id=alpha_id)
        return self._commit(graph, candidate, {alpha_id: alpha_id})

    def op_remove_alpha_output(self, graph: ColorGraphData,
                               node_id: str | None = None) -> GraphEditResult:
        target = node_id or graph.alpha_output_node_id
        if not target or self._node(graph, target) is None:
            return self._fail(graph, "node_not_found", f"Alpha Output 不存在: {target}")
        if target == graph.output_node_id:
            return self._fail(graph, "terminal_count",
                              "不能删除主 Output（目标是 Alpha Output）")
        nodes = tuple(n for n in graph.nodes if n.node_id != target)
        edges = tuple(e for e in graph.edges
                      if target not in (e.src_node, e.dst_node))
        candidate = replace(graph, nodes=nodes, edges=edges,
                            alpha_output_node_id=None)
        return self._commit(graph, candidate, {})

    # ════════════════════════════════════════════════════════════════
    # 删除 / 提取 / 复制
    # ════════════════════════════════════════════════════════════════

    def op_delete_nodes(self, graph: ColorGraphData,
                        node_ids: Sequence[str],
                        reconnect: str = "auto") -> GraphEditResult:
        """删除节点。``reconnect='auto'``：单进单出节点桥接上下游；
        多分支/端子节点删除需显式策略，否则诊断拒绝（不静默断效果）。
        """
        ids = set(node_ids)
        if not ids:
            return self._fail(graph, "illegal_param_key", "删除集合为空")
        protected = {graph.source_node_id, graph.output_node_id,
                     graph.alpha_output_node_id} - {None}
        hit = ids & protected
        if hit:
            return self._fail(graph, "terminal_count",
                              f"禁止删除受保护端子: {sorted(hit)}",
                              node_id=sorted(hit)[0])
        for node_id in ids:
            if self._node(graph, node_id) is None:
                return self._fail(graph, "node_not_found",
                                  f"节点不存在: {node_id}", node_id=node_id)

        remove_edges: set[str] = set()
        add_edges: list[ColorEdgeData] = []
        for node_id in sorted(ids):
            ins = self._edges_into(graph, node_id)
            outs = self._edges_out(graph, node_id)
            remove_edges.update(e.edge_id for e in ins + outs)
            image_ins = [e for e in ins if _port_kind_of(graph, e) is PortKind.IMAGE]
            image_outs = [e for e in outs if _port_kind_of(graph, e) is PortKind.IMAGE]
            if reconnect == "auto":
                if len(image_ins) <= 1 and len(image_outs) <= 1:
                    if image_ins and image_outs:
                        # 上下游桥接（类型一致已由原图保证）
                        add_edges.append(_make_edge(
                            image_ins[0].src_node, image_ins[0].src_port,
                            image_outs[0].dst_node, image_outs[0].dst_port))
                    # 端点缺失 = 链端删除，允许
                else:
                    return self._fail(
                        graph, "ambiguous_reconnect",
                        f"{node_id} 有 {len(image_ins)} 进 {len(image_outs)} 出，"
                        "自动重连策略不安全（需显式 extract/选择保留分支）",
                        node_id=node_id)
        kept = tuple(e for e in graph.edges if e.edge_id not in remove_edges)
        # 去重（桥接可能与既有边重复）
        seen: set[str] = set()
        final: list[ColorEdgeData] = []
        for e in kept + tuple(add_edges):
            if e.edge_id in seen:
                continue
            seen.add(e.edge_id)
            final.append(e)
        nodes = tuple(n for n in graph.nodes if n.node_id not in ids)
        candidate = replace(graph, nodes=nodes, edges=tuple(final))
        return self._commit(graph, candidate, {})

    def op_extract_nodes(self, graph: ColorGraphData,
                         node_ids: Sequence[str]) -> GraphEditResult:
        """提取节点：桥接其上下游、节点保留为未连接（UI 语义 Alt+Backspace）。"""
        ids = set(node_ids)
        for node_id in ids:
            node = self._node(graph, node_id)
            if node is None:
                return self._fail(graph, "node_not_found",
                                  f"节点不存在: {node_id}", node_id=node_id)
        remove_edges: set[str] = set()
        bridges: list[ColorEdgeData] = []
        for node_id in sorted(ids):
            ins = [e for e in self._edges_into(graph, node_id)
                   if _port_kind_of(graph, e) is PortKind.IMAGE]
            outs = [e for e in self._edges_out(graph, node_id)
                    if _port_kind_of(graph, e) is PortKind.IMAGE]
            remove_edges.update(e.edge_id
                                for e in self._edges_into(graph, node_id)
                                + self._edges_out(graph, node_id))
            if len(ins) == 1 and len(outs) == 1:
                bridges.append(_make_edge(ins[0].src_node, ins[0].src_port,
                                          outs[0].dst_node, outs[0].dst_port))
        kept = tuple(e for e in graph.edges if e.edge_id not in remove_edges)
        seen = {e.edge_id for e in kept}
        for bridge in bridges:
            if bridge.edge_id not in seen:
                seen.add(bridge.edge_id)
                kept = kept + (bridge,)
        candidate = replace(graph, edges=kept)
        return self._commit(graph, candidate, {})

    def op_duplicate_subgraph(self, graph: ColorGraphData,
                              source_ids: Sequence[str],
                              offset: Sequence[float] = (40.0, 40.0)
                              ) -> GraphEditResult:
        """复制子图：内部 ID 全重写，内部边保持；外部边不复制。"""
        return self._copy_nodes(graph, source_ids, offset)

    def op_paste_subgraph(self, graph: ColorGraphData,
                          payload_nodes: Sequence[Mapping[str, Any]],
                          payload_edges: Sequence[Mapping[str, Any]] = (),
                          offset: Sequence[float] = (40.0, 40.0)
                          ) -> GraphEditResult:
        """粘贴子图载荷（B14 clipboard 域）：ID 重写后未连接落图。"""
        remap: dict[str, str] = {}
        nodes: list[ColorNodeData] = []
        for payload in payload_nodes:
            data = dict(payload)
            old_id = data.get("node_id")
            new_id = self._id_factory()
            if old_id:
                remap[old_id] = new_id
            data["node_id"] = new_id
            data.pop("compound_children", None)  # 子图粘贴不携带嵌套 compound（B11 扩展）
            nodes.append(ColorNodeData.from_dict(data))
        edges: list[ColorEdgeData] = []
        for payload in payload_edges:
            data = dict(payload)
            if data.get("src_node") in remap and data.get("dst_node") in remap:
                edges.append(_make_edge(remap[data["src_node"]], data["src_port"],
                                        remap[data["dst_node"]], data["dst_port"],
                                        slot_id=str(data.get("slot_id") or "")))
        candidate = replace(graph,
                            nodes=graph.nodes + tuple(nodes),
                            edges=graph.edges + tuple(edges))
        return self._commit(graph, candidate, remap)

    def _copy_nodes(self, graph: ColorGraphData,
                    source_ids: Sequence[str],
                    offset: Sequence[float]) -> GraphEditResult:
        ids = set(source_ids)
        if not ids:
            return self._fail(graph, "illegal_param_key", "复制集合为空")
        remap: dict[str, str] = {}
        for node_id in ids:
            if self._node(graph, node_id) is None:
                return self._fail(graph, "node_not_found",
                                  f"节点不存在: {node_id}", node_id=node_id)
            remap[node_id] = self._id_factory()
        new_nodes = tuple(
            replace(self._node(graph, old), node_id=new,
                    shared_definition_id=(  # shared 定义引用保留（定义共享语义）
                        self._node(graph, old).shared_definition_id))
            for old, new in remap.items())
        internal = [e for e in graph.edges
                    if e.src_node in ids and e.dst_node in ids]
        new_edges = tuple(
            _make_edge(remap[e.src_node], e.src_port,
                       remap[e.dst_node], e.dst_port, slot_id=e.slot_id)
            for e in internal)
        positions = dict(graph.ui_state.positions)
        for old, new in remap.items():
            if old in positions:
                pos = positions[old]
                positions[new] = (pos[0] + float(offset[0]),
                                  pos[1] + float(offset[1]))
        ui = replace(graph.ui_state, positions=positions)
        candidate = replace(graph, nodes=graph.nodes + new_nodes,
                            edges=graph.edges + new_edges, ui_state=ui)
        return self._commit(graph, candidate, remap)

    # ════════════════════════════════════════════════════════════════
    # mixer / 参数 / 元信息
    # ════════════════════════════════════════════════════════════════

    def op_add_mixer_slot(self, graph: ColorGraphData,
                          node_id: str) -> GraphEditResult:
        node = self._node(graph, node_id)
        if node is None:
            return self._fail(graph, "node_not_found", f"节点不存在: {node_id}")
        if node.node_type not in (NodeType.PARALLEL_MIXER.value,
                                  NodeType.LAYER_MIXER.value):
            return self._fail(graph, "port_not_found",
                              f"{node_id} 不是 mixer，无 slot 可加", node_id=node_id)
        max_slots = 8
        edge_slots = {e.slot_id for e in self._edges_into(graph, node_id)
                      if e.slot_id}
        param_slots = set(node.params.get("slots", ())
                          if isinstance(node.params.get("slots", ()),
                                        (tuple, list)) else ())
        existing = edge_slots | param_slots
        if len(existing) >= max_slots:
            return self._fail(graph, "size_limit",
                              f"mixer slot 上限 {max_slots}", node_id=node_id)
        # slot 分配：稳定 slot ID（新分支输入在 B10 执行语义接线时连接）
        next_index = len(existing)
        params = dict(node.params)
        params["slots"] = sorted(existing |
                                 {f"{node_id}/slot-or-branch/{next_index}"})
        new_node = replace(node, params=params)
        nodes = tuple(new_node if n.node_id == node_id else n
                      for n in graph.nodes)
        candidate = replace(graph, nodes=nodes)
        return self._commit(graph, candidate, {})

    def op_change_mixer_type(self, graph: ColorGraphData, node_id: str,
                             kind: str) -> GraphEditResult:
        node = self._node(graph, node_id)
        if node is None:
            return self._fail(graph, "node_not_found", f"节点不存在: {node_id}")
        try:
            target = NodeType(kind)
        except ValueError:
            return self._fail(graph, "unknown_node_type", f"未知类型: {kind}")
        if target not in (NodeType.PARALLEL_MIXER, NodeType.LAYER_MIXER):
            return self._fail(graph, "port_not_found",
                              f"morph 目标必须是 mixer: {kind}", node_id=node_id)
        if node.node_type not in (NodeType.PARALLEL_MIXER.value,
                                  NodeType.LAYER_MIXER.value):
            return self._fail(graph, "port_not_found",
                              f"{node_id} 不是 mixer，不能 morph", node_id=node_id)
        # 端口目录不同（base/branch ↔ slot）——双向重写相邻边端口
        edges = list(graph.edges)
        to_parallel = target is NodeType.PARALLEL_MIXER
        for i, e in enumerate(edges):
            if e.dst_node == node_id:
                if to_parallel:
                    if e.dst_port == "slot":
                        idx = (e.slot_id.rsplit("/", 1)[-1]
                               if e.slot_id else "0")
                        index = int(idx) if idx.isdigit() else 0
                        if index == 0:
                            edges[i] = _make_edge(e.src_node, e.src_port,
                                                  node_id, "base")
                        else:
                            edges[i] = _make_edge(
                                e.src_node, e.src_port, node_id, "branch",
                                slot_id=f"{node_id}/branch/{index - 1}")
                else:  # → layer mixer
                    if e.dst_port == "base":
                        edges[i] = _make_edge(e.src_node, e.src_port,
                                              node_id, "slot",
                                              slot_id=f"{node_id}/slot/0")
                    elif e.dst_port == "branch":
                        idx = e.slot_id.rsplit("/", 1)[-1]
                        edges[i] = _make_edge(e.src_node, e.src_port,
                                              node_id, "slot",
                                              slot_id=f"{node_id}/slot/{int(idx) + 1}")
        new_node = replace(node, node_type=target.value)
        nodes = tuple(new_node if n.node_id == node_id else n
                      for n in graph.nodes)
        candidate = replace(graph, nodes=nodes, edges=tuple(edges))
        return self._commit(graph, candidate, {})

    def op_set_bypass(self, graph: ColorGraphData,
                      node_ids: Sequence[str], enabled: bool) -> GraphEditResult:
        return self._patch_nodes(graph, node_ids, enabled=enabled)

    def op_set_all_enabled(self, graph: ColorGraphData,
                           enabled: bool = True) -> GraphEditResult:
        ids = [n.node_id for n in graph.nodes
               if n.node_type not in (NodeType.SOURCE.value,
                                      NodeType.OUTPUT.value)]
        return self._patch_nodes(graph, ids, enabled=enabled)

    def op_set_node_option(self, graph: ColorGraphData, node_id: str,
                           name: str, value: Any) -> GraphEditResult:
        """按 name 分派到领域 schema 键（§6.3：不做通用 dict update）。"""
        node = self._node(graph, node_id)
        if node is None:
            return self._fail(graph, "node_not_found", f"节点不存在: {node_id}")
        allowed = {
            "key_invert": ("key_settings", "invert_key_in"),
            "blend_mode": ("mixer", "blend_mode"),
            "lut_ref": ("resources", "lut_ref"),
            "color_space": ("color", "color_space"),
            "gamma": ("color", "gamma"),
        }
        if name == "grade_patch":
            # B06.2 旧面板手势过渡：图内 grade 域单键 patch（B09 前旧
            # 引擎经 legacy 投影消费；服务端仍走完整事务/校验）
            if not isinstance(value, tuple) or len(value) != 2:
                return self._fail(graph, "illegal_param_key",
                                  "grade_patch 需要 (param_name, value) 元组",
                                  node_id=node_id)
            grade = dict(node.params.get("grade") or {})
            grade[str(value[0])] = value[1]
            params = dict(node.params)
            params["grade"] = grade
            new_node = replace(node, params=params)
            nodes = tuple(new_node if n.node_id == node_id else n
                          for n in graph.nodes)
            candidate = replace(graph, nodes=nodes)
            return self._commit(graph, candidate, {})
        if name not in allowed:
            return self._fail(graph, "illegal_param_key",
                              f"未知节点选项: {name}（须显式登记 schema 键）",
                              node_id=node_id)
        group, key = allowed[name]
        params = dict(node.params)
        section = dict(params.get(group, {}))
        section[key] = value
        params[group] = section
        new_node = replace(node, params=params)
        nodes = tuple(new_node if n.node_id == node_id else n
                      for n in graph.nodes)
        candidate = replace(graph, nodes=nodes)
        return self._commit(graph, candidate, {})

    def op_patch_key_settings(self, graph: ColorGraphData, node_id: str,
                              **settings: Any) -> GraphEditResult:
        node = self._node(graph, node_id)
        if node is None:
            return self._fail(graph, "node_not_found", f"节点不存在: {node_id}")
        params = dict(node.params)
        section = dict(params.get("key_settings", {}))
        section.update(settings)
        params["key_settings"] = section
        new_node = replace(node, params=params)
        nodes = tuple(new_node if n.node_id == node_id else n
                      for n in graph.nodes)
        candidate = replace(graph, nodes=nodes)
        return self._commit(graph, candidate, {})

    def op_reset_corrector_parameters(self, graph: ColorGraphData,
                                      node_ids: Sequence[str]) -> GraphEditResult:
        def reset(node: ColorNodeData) -> ColorNodeData:
            params = {"key_source_mode": node.params.get(
                "key_source_mode", "node_input")}
            if "outside_key_source" in node.params:
                params["outside_key_source"] = node.params["outside_key_source"]
            return replace(node, params=params)
        return self._patch_nodes_with(graph, node_ids, reset)

    def op_reset_graph_parameters(self, graph: ColorGraphData) -> GraphEditResult:
        ids = [n.node_id for n in graph.nodes
               if n.known_type() not in (NodeType.SOURCE, NodeType.OUTPUT)]
        return self.op_reset_corrector_parameters(graph, ids)

    def op_reset_graph(self, graph: ColorGraphData) -> GraphEditResult:
        """整图重置：保留 Source/Output 与 scope owner 绑定。"""
        base = ColorGraphData.create_serial()
        candidate = replace(
            base, graph_id=graph.graph_id,
            graph_revision=graph.graph_revision + 1,
            working_color_context=dict(graph.working_color_context))
        return GraphEditResult(True, candidate, {})

    # ── 布局/元信息（不动 render revision） ──

    def op_update_layout(self, graph: ColorGraphData,
                         positions: Mapping[str, Sequence[float]]
                         ) -> GraphEditResult:
        merged = dict(graph.ui_state.positions)
        for node_id, pos in positions.items():
            if self._node(graph, node_id) is None:
                return self._fail(graph, "node_not_found",
                                  f"布局引用不存在节点: {node_id}")
            merged[node_id] = (float(pos[0]), float(pos[1]))
        ui = replace(graph.ui_state,
                     positions=merged,
                     layout_revision=graph.ui_state.layout_revision + 1)
        candidate = graph.with_layout(ui)
        return GraphEditResult(True, candidate, {}, {})

    def op_move_and_insert(self, graph: ColorGraphData,
                           positions: Mapping[str, Sequence[float]],
                           node_id: str, edge_id: str) -> GraphEditResult:
        """布局+结构一次事务（§6.2）：一个 command、一次 revision。"""
        step1 = self.op_update_layout(graph, positions)
        if not step1.ok:
            return step1
        edge = next((e for e in step1.graph.edges
                     if e.edge_id == edge_id), None)
        node = self._node(step1.graph, node_id)
        if edge is None or node is None:
            return self._fail(graph, "edge_endpoint_missing",
                              f"插入目标边/节点不存在: {edge_id}/{node_id}")
        ins = self._edges_into(step1.graph, node_id, "image")
        outs = self._edges_out(step1.graph, node_id, "image")
        if ins or outs:
            return self._fail(graph, "port_multi",
                              f"{node_id} 已接入链，move_and_insert 仅插入自由节点",
                              node_id=node_id)
        err = self._check_port(step1.graph, node_id, "image", PortDirection.IN)
        if err is not None:
            return GraphEditResult(False, graph, (err,))
        edges = [e for e in step1.graph.edges if e.edge_id != edge_id]
        edges += [
            _make_edge(edge.src_node, edge.src_port, node_id, "image"),
            _make_edge(node_id, "image", edge.dst_node, edge.dst_port),
        ]
        candidate = replace(step1.graph, edges=tuple(edges),
                            graph_revision=graph.graph_revision + 1)
        errors = candidate.validate()
        if errors:
            return GraphEditResult(False, graph, tuple(errors))
        return GraphEditResult(True, candidate, {}, {})

    def op_set_node_metadata(self, graph: ColorGraphData, node_id: str,
                             label: str) -> GraphEditResult:
        return self._patch_nodes(graph, [node_id], label=str(label))

    def op_set_node_color(self, graph: ColorGraphData,
                          node_ids: Sequence[str], color: str) -> GraphEditResult:
        if color.lower() not in {
                "default", "red", "orange", "yellow", "green", "cyan",
                "blue", "purple", "pink"}:
            return self._fail(graph, "illegal_param_key", f"未知节点颜色: {color}")
        return self._patch_nodes(graph, node_ids, ui_color=color)

    def op_set_edit_lock(self, graph: ColorGraphData,
                         node_ids: Sequence[str], locked: bool) -> GraphEditResult:
        return self._patch_nodes(graph, node_ids, locked=locked)

    def op_set_cache_policy(self, graph: ColorGraphData,
                            node_ids: Sequence[str], mode: str) -> GraphEditResult:
        if mode.lower() not in {"auto", "on", "off"}:
            return self._fail(graph, "illegal_param_key", f"未知缓存策略: {mode}")
        return self._patch_nodes(graph, node_ids, cache_mode=mode.lower())

    # ════════════════════════════════════════════════════════════════
    # Compound / Shared 定义
    # ════════════════════════════════════════════════════════════════

    def op_group_subgraph(self, graph: ColorGraphData,
                          member_ids: Sequence[str]) -> GraphEditResult:
        """成员打包 compound：成员整体移入 children 子图，外部边改接
        compound 边界端口（B11 完整 mapping；此处落定结构）。
        """
        ids = set(member_ids)
        if not ids:
            return self._fail(graph, "illegal_param_key", "group 集合为空")
        protected = {graph.source_node_id, graph.output_node_id,
                     graph.alpha_output_node_id} - {None}
        if ids & protected:
            return self._fail(graph, "terminal_count",
                              "端子节点不能进 compound")
        for node_id in ids:
            if self._node(graph, node_id) is None:
                return self._fail(graph, "node_not_found",
                                  f"成员不存在: {node_id}", node_id=node_id)
        compound_id = self._id_factory()
        # 边界：指向成员集合的外部边 → compound.image 入；成员集合出 → 出
        incoming = [e for e in graph.edges
                    if e.dst_node in ids and e.src_node not in ids]
        outgoing = [e for e in graph.edges
                    if e.src_node in ids and e.dst_node not in ids]
        if len(incoming) > 1 or len(outgoing) > 1:
            return self._fail(
                graph, "ambiguous_reconnect",
                f"成员集合有 {len(incoming)} 入 {len(outgoing)} 出边界，"
                "第一版 compound 只支持单入单出连续段", node_id=sorted(ids)[0])
        # 子图：成员 + 内部边 + 虚拟 Source/Output（对齐端子规则）
        inner_nodes = tuple(replace(n) for n in graph.nodes if n.node_id in ids)
        inner_edges = tuple(_remap_edge(e, {}) for e in graph.edges
                            if e.src_node in ids and e.dst_node in ids)
        inner_source = ColorNodeData(
            node_id=f"{compound_id}/source", node_type=NodeType.SOURCE.value)
        inner_output = ColorNodeData(
            node_id=f"{compound_id}/output", node_type=NodeType.OUTPUT.value)
        child_edges = list(inner_edges)
        if incoming:
            head = incoming[0].dst_node
            child_edges.append(_make_edge(inner_source.node_id, "image",
                                          head, "image"))
        if outgoing:
            tail = outgoing[0].src_node
            child_edges.append(_make_edge(tail, "image",
                                          inner_output.node_id, "image"))
        children = ColorGraphData(
            nodes=(inner_source,) + inner_nodes + (inner_output,),
            edges=tuple(child_edges),
            source_node_id=inner_source.node_id,
            output_node_id=inner_output.node_id)
        if children.validate():
            return GraphEditResult(
                False, graph,
                (GraphSchemaError("invalid_graph",
                                  f"compound 内部图非法: {children.validate()[0]}"),))
        compound = ColorNodeData(
            node_id=compound_id, node_type=NodeType.COMPOUND.value,
            compound_children=children)
        # 外层：成员移除，边界边改接 compound
        outer_edges = [e for e in graph.edges
                       if e.src_node not in ids and e.dst_node not in ids]
        if incoming:
            outer_edges.append(_make_edge(incoming[0].src_node,
                                          incoming[0].src_port,
                                          compound_id, "image"))
        if outgoing:
            outer_edges.append(_make_edge(compound_id, "image",
                                          outgoing[0].dst_node,
                                          outgoing[0].dst_port))
        candidate = replace(graph,
                            nodes=tuple(n for n in graph.nodes
                                        if n.node_id not in ids) + (compound,),
                            edges=tuple(outer_edges))
        return self._commit(graph, candidate, {compound_id: compound_id})

    def op_ungroup_subgraph(self, graph: ColorGraphData,
                            node_id: str) -> GraphEditResult:
        node = self._node(graph, node_id)
        if node is None or node.node_type != NodeType.COMPOUND.value:
            return self._fail(graph, "node_not_found",
                              f"compound 不存在: {node_id}", node_id=node_id)
        children = node.compound_children
        if children is None:
            return self._fail(graph, "invalid_graph",
                              "compound 无内部子图（损坏数据）", node_id=node_id)
        # 内部节点抬升（去掉虚拟 Source/Output），边界边恢复
        promoted = tuple(n for n in children.nodes
                         if n.node_type not in (NodeType.SOURCE.value,
                                                NodeType.OUTPUT.value))
        remap = {n.node_id: n.node_id for n in promoted}
        outer_edges = [e for e in graph.edges
                       if e.src_node != node_id and e.dst_node != node_id]
        # 内部边抬升（去掉指向/来自虚拟端子的部分）
        outer_edges += [e for e in children.edges
                        if e.src_node != children.source_node_id
                        and e.dst_node != children.output_node_id]
        into_compound = [e for e in graph.edges if e.dst_node == node_id]
        out_compound = [e for e in graph.edges if e.src_node == node_id]
        if into_compound and out_compound:
            # 内部链头尾（对齐 children 的虚拟端子连接）
            head = next((e.dst_node for e in children.edges
                         if e.src_node == children.source_node_id), None)
            tail = next((e.src_node for e in children.edges
                         if e.dst_node == children.output_node_id), None)
            if head and tail:
                outer_edges.append(_make_edge(
                    into_compound[0].src_node, into_compound[0].src_port,
                    head, "image"))
                outer_edges.append(_make_edge(
                    tail, "image", out_compound[0].dst_node,
                    out_compound[0].dst_port))
        candidate = replace(
            graph,
            nodes=tuple(n for n in graph.nodes if n.node_id != node_id)
            + promoted,
            edges=tuple(outer_edges))
        return self._commit(graph, candidate, remap)

    def op_create_shared_definition(self, graph: ColorGraphData,
                                    node_id: str,
                                    name: str = "Shared",
                                    param_domains: Sequence[str] = ()
                                    ) -> GraphEditResult:
        """把 Corrector 参数抽为共享定义；节点变 SharedCorrector 引用。"""
        node = self._node(graph, node_id)
        if node is None:
            return self._fail(graph, "node_not_found",
                              f"节点不存在: {node_id}", node_id=node_id)
        if node.node_type not in (NodeType.CORRECTOR.value,
                                  NodeType.SHARED_CORRECTOR.value):
            return self._fail(graph, "unknown_node_type",
                              f"{node.node_type} 不能抽为共享定义",
                              node_id=node_id)
        definition_id = self._id_factory()
        definition = SharedDefinitionData(
            definition_id=definition_id, name=name,
            node_type=NodeType.CORRECTOR.value,
            params=dict(node.params))
        new_node = replace(node, node_type=NodeType.SHARED_CORRECTOR.value,
                           shared_definition_id=definition_id)
        nodes = tuple(new_node if n.node_id == node_id else n
                      for n in graph.nodes)
        candidate = replace(graph, nodes=nodes,
                            shared_definitions=graph.shared_definitions
                            + (definition,))
        return self._commit(graph, candidate,
                            {definition_id: definition_id})

    def op_update_shared_definition_params(self, graph: ColorGraphData,
                                            definition_id: str,
                                            params: Mapping[str, Any]
                                            ) -> GraphEditResult:
        """更新共享定义参数：所有引用该定义的图 revision 一起失效（fanout
        由 bridge 跨栈执行；本 op 负责单图内定义版本+引用节点缓存失效）。"""
        definition = next((d for d in graph.shared_definitions
                           if d.definition_id == definition_id), None)
        if definition is None:
            return self._fail(graph, "node_not_found",
                              f"shared 定义不存在: {definition_id}")
        referenced = any(n.shared_definition_id == definition_id
                         for n in graph.nodes)
        new_definition = replace(definition,
                                 params=dict(params),
                                 version=definition.version + 1)
        definitions = tuple(new_definition if d.definition_id == definition_id
                            else d for d in graph.shared_definitions)
        candidate = replace(graph, shared_definitions=definitions)
        # 无引用节点时定义更新不改 render revision（执行策略级）
        return self._commit(graph, candidate, {}, render_change=referenced)

    def op_unlock_stack(self, graph: ColorGraphData) -> GraphEditResult:
        """解锁由 stack 级元信息承载（with_graph_locked）；占位 op 用于
        command 路由统一。"""
        return GraphEditResult(True, graph, {}, {})

    def op_make_local_corrector(self, graph: ColorGraphData,
                                node_ids: Sequence[str]) -> GraphEditResult:
        """深复制定义为本地参数，解除引用（不解除用户锁）。"""
        definitions = {d.definition_id: d for d in graph.shared_definitions}
        patched: list[ColorNodeData] = []
        for node_id in node_ids:
            node = self._node(graph, node_id)
            if node is None:
                return self._fail(graph, "node_not_found",
                                  f"节点不存在: {node_id}", node_id=node_id)
            ref = node.shared_definition_id
            if ref is None or ref not in definitions:
                return self._fail(graph, "node_not_found",
                                  f"{node_id} 无可解除的 shared 引用",
                                  node_id=node_id)
            local_params = dict(definitions[ref].params)
            local_params.update({k: v for k, v in node.params.items()
                                 if k not in definitions[ref].params})
            patched.append(replace(node,
                                   node_type=NodeType.CORRECTOR.value,
                                   shared_definition_id=None,
                                   params=local_params))
        by_id = {n.node_id: n for n in patched}
        nodes = tuple(by_id.get(n.node_id, n) for n in graph.nodes)
        candidate = replace(graph, nodes=nodes)
        return self._commit(graph, candidate, {})

    # ── 参数/元信息内部工具 ──

    def _patch_nodes(self, graph: ColorGraphData,
                     node_ids: Sequence[str], **fields: Any) -> GraphEditResult:
        param_fields = {"ui_color", "locked", "cache_mode"}
        direct = {k: v for k, v in fields.items() if k not in param_fields}

        def patch(node: ColorNodeData) -> ColorNodeData:
            if set(fields) & param_fields:
                params = dict(node.params)
                for key in set(fields) & param_fields:
                    params[key] = fields[key]
                return replace(node, params=params, **direct)
            return replace(node, **direct)
        return self._patch_nodes_with(graph, node_ids, patch)

    def _patch_nodes_with(self, graph: ColorGraphData,
                          node_ids: Sequence[str],
                          transform: Callable[[ColorNodeData], ColorNodeData]
                          ) -> GraphEditResult:
        ids = set(node_ids)
        if not ids:
            return self._fail(graph, "illegal_param_key", "目标集合为空")
        for node_id in ids:
            if self._node(graph, node_id) is None:
                return self._fail(graph, "node_not_found",
                                  f"节点不存在: {node_id}", node_id=node_id)
        nodes = tuple(transform(n) if n.node_id in ids else n
                      for n in graph.nodes)
        candidate = replace(graph, nodes=nodes)
        return self._commit(graph, candidate, {})


def _port_kind_of(graph: ColorGraphData, edge: ColorEdgeData) -> PortKind | None:
    node = next((n for n in graph.nodes if n.node_id == edge.src_node), None)
    if node is None:
        return None
    spec = next((p for p in node.ports()
                 if p.port_id == edge.src_port
                 and p.direction is PortDirection.OUT), None)
    return spec.kind if spec else None


def _remap_edge(edge: ColorEdgeData, remap: Mapping[str, str]) -> ColorEdgeData:
    return ColorEdgeData(
        edge_id=edge.edge_id,
        src_node=remap.get(edge.src_node, edge.src_node),
        src_port=edge.src_port,
        dst_node=remap.get(edge.dst_node, edge.dst_node),
        dst_port=edge.dst_port,
        slot_id=edge.slot_id)
