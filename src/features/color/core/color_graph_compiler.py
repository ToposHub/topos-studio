"""ColorGraphRenderPlan 编译器（N05，B08.2–B08.4）。

职责（纯 CPU，不触 GPU）：

1. **依赖锥**：只编译 ``viewer_target``（主 Output 或 viewer:<node_id>）
   需要的子图——孤立昂贵插件/未连接节点不进 plan（导出只取依赖锥）。
2. **compound 展开**：compound 节点按 children 内部边 inline（边界端口
   直通），shared_corrector 解析为定义参数实例（参数共享 ≠ 输入共享）。
3. **拓扑排序**：image+key 边共同参与；成环/断链在
   ``validate_execution`` 报错。
4. **typed passes**：每个 pass 声明输入端口→上游 pass 输出、消费者
   计数、最后使用点、估算 bytes（RGBA16F = w*h*8）、alias（分叉输入
   在所有消费者结束前保持 live——release_after = max(消费者完成序)）。
5. **能力预检**：``validate_capabilities`` 按后端 catalog 声明
   exact/fallback/unsupported——B08 时 mixer/splitter 族为 pending
   （B10 落实数学后翻 exact）；预检不通过时 plan 仍可解释（导出侧
   B13 阻断）。
6. **编译缓存**：键 = snapshot.cache_key() + 后端签名。

``to_plan_json`` 导出可解释 plan（每个输出来自哪个 pass/port）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from src.shared.project.color_graph import (
    ColorEdgeData,
    ColorGraphData,
    ColorNodeData,
    GraphSchemaError,
    NodeType,
)
from src.features.color.core.color_graph_snapshot import ColorGraphSnapshot

#: RGBA16F 每像素 bytes（§5.4 峰值预算口径）
BYTES_PER_PIXEL_RGBA16F = 8


@dataclass(frozen=True)
class PlanPortRef:
    """一个 pass 输入对上游输出的引用。"""

    pass_index: int
    node_id: str
    port: str


@dataclass(frozen=True)
class RenderPass:
    """typed pass：一个节点在依赖锥内的一次执行。"""

    index: int
    node_id: str
    node_type: str
    op: str                      # source / corrector / parallel_mixer / …
    params: Mapping[str, Any]
    inputs: tuple[tuple[str, PlanPortRef | None], ...]  # (port, ref)
    outputs: tuple[str, ...]
    consumers: int = 0           # 本 pass 输出被后续 pass 引用次数
    release_after: int = -1      # 本 pass 完成后其输出可释放的位置（max 消费者 idx）
    estimated_bytes: int = 0
    shared_definition_id: str | None = None
    # compound 展开来源（诊断/可解释性）
    expanded_from: str | None = None


@dataclass(frozen=True)
class CapabilityReport:
    """后端能力预检结果（exact/fallback/unsupported/pending）。"""

    backend: str
    supported_ops: tuple[str, ...]
    unsupported: tuple[tuple[str, str], ...]     # (pass 索引标签, 原因)

    @property
    def executable(self) -> bool:
        return not any(status == "unsupported"
                       for _, status in self.unsupported_status)

    unsupported_status: tuple[tuple[str, str], ...] = field(default_factory=tuple)


#: GL 后端（B08 时点）可执行的算子：corrector 族经 N06 exact；
#: mixer/splitter/combiner/outside/key_mixer/alpha 等 B10 前为 pending。
GL_CAPABILITY_CATALOG: Mapping[str, str] = {
    "source": "exact",
    "corrector": "exact",
    "shared_corrector": "exact",
    "output": "exact",
    "parallel_mixer": "pending",
    "layer_mixer": "pending",
    "key_mixer": "pending",
    "outside": "pending",
    "splitter": "pending",
    "combiner": "pending",
    "alpha_output": "pending",
    "compound": "pending",     # 由展开消除；未展开=数据异常
}


class GraphCompileError(ValueError):
    """编译失败（断链/环/损坏 compound），带稳定 code。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code


@dataclass
class ColorGraphRenderPlan:
    """执行计划（可解释、可序列化、可缓存）。"""

    snapshot_key: tuple
    target: str
    passes: list[RenderPass] = field(default_factory=list)
    peak_live_bytes: int = 0
    peak_live_textures: int = 0
    output_pass_index: int = -1
    compile_ms: float = 0.0
    warnings: tuple[str, ...] = ()

    # ── 能力预检 ──

    def validate_capabilities(
            self, catalog: Mapping[str, str] | None = None
    ) -> CapabilityReport:
        catalog = catalog if catalog is not None else GL_CAPABILITY_CATALOG
        unsupported: list[tuple[str, str]] = []
        statuses: list[tuple[str, str]] = []
        for render_pass in self.passes:
            status = catalog.get(render_pass.op, "unsupported")
            label = f"{render_pass.index}:{render_pass.node_id}:{render_pass.op}"
            statuses.append((label, status))
            if status != "exact":
                # fail-closed：unsupported 与 pending 都不可执行（B10
                # 落实 mixer 数学后翻 exact）
                unsupported.append((label, (
                    f"算子 {render_pass.op} 后端状态 {status}"
                    "（unsupported/pending 均拒绝执行）")))
        return CapabilityReport(
            backend="gl", supported_ops=tuple(
                op for op, st in catalog.items() if st == "exact"),
            unsupported=tuple(unsupported),
            unsupported_status=tuple(statuses))

    # ── 可解释导出 ──

    def to_plan_json(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "snapshot_key": [str(k) for k in self.snapshot_key],
            "peak_live_bytes": self.peak_live_bytes,
            "peak_live_textures": self.peak_live_textures,
            "compile_ms": round(self.compile_ms, 3),
            "passes": [
                {
                    "index": p.index,
                    "node": p.node_id,
                    "op": p.op,
                    "inputs": {
                        port: (None if ref is None else
                               f"pass{ref.pass_index}:{ref.node_id}.{ref.port}")
                        for port, ref in p.inputs},
                    "outputs": list(p.outputs),
                    "consumers": p.consumers,
                    "release_after": p.release_after,
                    "estimated_bytes": p.estimated_bytes,
                    "expanded_from": p.expanded_from,
                }
                for p in self.passes],
            "warnings": list(self.warnings),
        }


# ── 展开辅助：compound inline / shared 解析 ─────────────────────────────

def _flatten_graph(graph: ColorGraphData) -> tuple[list[ColorNodeData],
                                                    list[ColorEdgeData],
                                                    list[str]]:
    """compound 展开（inline children 边界直通）+ shared 参数解析。

    返回 (nodes, edges, warnings)。展开保持成员身份（node_id 不变），
    shared_corrector 节点 params 合并定义参数（实例覆盖定义）。
    """
    nodes: list[ColorNodeData] = []
    edges: list[ColorEdgeData] = list(graph.edges)
    warnings: list[str] = []

    stack: list[ColorNodeData] = list(graph.nodes)
    while stack:
        node = stack.pop(0)
        if node.node_type == NodeType.COMPOUND.value:
            children = node.compound_children
            if children is None:
                warnings.append(f"compound {node.node_id} 无子图，按空展开")
                # 边界直通：compound 的入边改接出边目标
                into = [e for e in edges if e.dst_node == node.node_id]
                out_of = [e for e in edges if e.src_node == node.node_id]
                edges = [e for e in edges
                         if e.dst_node != node.node_id
                         and e.src_node != node.node_id]
                for ein, eout in zip(into, out_of):
                    edges.append(ColorEdgeData(
                        edge_id=f"{ein.edge_id}->{eout.edge_id}",
                        src_node=ein.src_node, src_port=ein.src_port,
                        dst_node=eout.dst_node, dst_port=eout.dst_port))
                continue
            # 内部成员抬升 + 内部边（虚拟端子断开）+ 外部边界直通
            inner_nodes = [n for n in children.nodes
                           if n.node_type not in (NodeType.SOURCE.value,
                                                  NodeType.OUTPUT.value)]
            inner_edges = [e for e in children.edges
                           if e.src_node != children.source_node_id
                           and e.dst_node != children.output_node_id]
            into = [e for e in edges if e.dst_node == node.node_id]
            out_of = [e for e in edges if e.src_node == node.node_id]
            edges = [e for e in edges
                     if e.dst_node != node.node_id
                     and e.src_node != node.node_id]
            edges.extend(inner_edges)
            # 边界：外部入边 → 内部链头；内部链尾 → 外部出边目标
            heads = [e.dst_node for e in children.edges
                     if e.src_node == children.source_node_id]
            tails = [e.src_node for e in children.edges
                     if e.dst_node == children.output_node_id]
            for boundary_in, external_in in zip(heads, into):
                edges.append(ColorEdgeData(
                    edge_id=f"cx-{external_in.edge_id}",
                    src_node=external_in.src_node,
                    src_port=external_in.src_port,
                    dst_node=boundary_in, dst_port="image"))
            for boundary_tail, external_out in zip(tails, out_of):
                edges.append(ColorEdgeData(
                    edge_id=f"cx-{external_out.edge_id}",
                    src_node=boundary_tail, src_port="image",
                    dst_node=external_out.dst_node,
                    dst_port=external_out.dst_port))
            for inner in inner_nodes:
                if inner.node_type == NodeType.COMPOUND.value:
                    stack.append(inner)   # 嵌套 compound 继续展开
                else:
                    nodes.append(inner)
            continue
        if node.node_type == NodeType.SHARED_CORRECTOR.value:
            definition = next(
                (d for d in graph.shared_definitions
                 if d.definition_id == node.shared_definition_id), None)
            if definition is None:
                warnings.append(
                    f"shared_corrector {node.node_id} 定义缺失，按 neutral")
                nodes.append(node)
                continue
            merged = {k: v for k, v in definition.params.items()}
            merged.update({k: v for k, v in node.params.items()
                           if k != "shared_definition_id"})
            from dataclasses import replace as _replace
            nodes.append(_replace(node, node_type=NodeType.CORRECTOR.value,
                                  params=merged,
                                  shared_definition_id=node.shared_definition_id))
            continue
        nodes.append(node)
    return nodes, edges, warnings


# ── 主入口 ──

def compile_graph(snapshot: ColorGraphSnapshot,
                  *, size: tuple[int, int] = (1920, 1080),
                  cache: dict | None = None) -> ColorGraphRenderPlan:
    """编译 target 依赖锥 → RenderPlan（不触 GPU）。

    ``cache``：可选 dict（调用方持有）；键含 snapshot.cache_key()。
    """
    started = time.perf_counter()
    if cache is not None:
        cached = cache.get(snapshot.cache_key())
        if cached is not None:
            return cached          # 编译缓存命中（§5.3：拓扑/版本/帧/上下文）
    graph = snapshot.graph
    target = snapshot.viewer_target
    target_node = (graph.output_node_id if target == "output"
                   else target.removeprefix("viewer:"))

    errors = graph.validate()
    if errors:
        raise GraphCompileError("invalid_graph", f"图结构非法: {errors[0]}")

    flat_nodes, flat_edges, warnings = _flatten_graph(graph)
    by_id = {n.node_id: n for n in flat_nodes}
    if target_node not in by_id:
        raise GraphCompileError(
            "node_not_found", f"目标节点不存在: {target_node}")

    # ── 依赖锥（image+key 共同） ──
    incoming: dict[str, list[ColorEdgeData]] = {}
    outgoing: dict[str, list[ColorEdgeData]] = {}
    for edge in flat_edges:
        if edge.src_node in by_id and edge.dst_node in by_id:
            incoming.setdefault(edge.dst_node, []).append(edge)
            outgoing.setdefault(edge.src_node, []).append(edge)

    cone: set[str] = set()
    stack = [target_node]
    while stack:
        current = stack.pop()
        if current in cone:
            continue
        cone.add(current)
        for edge in incoming.get(current, []):
            if edge.src_node not in cone:
                stack.append(edge.src_node)

    # ── 拓扑排序（Kahn，锥内） ──
    indegree = {nid: len([e for e in incoming.get(nid, [])
                          if e.src_node in cone])
                for nid in cone}
    ready = sorted(nid for nid, deg in indegree.items() if deg == 0)
    order: list[str] = []
    cone_edges = [e for e in flat_edges
                  if e.src_node in cone and e.dst_node in cone]
    while ready:
        current = ready.pop(0)
        order.append(current)
        for edge in sorted(outgoing.get(current, []),
                           key=lambda e: e.edge_id):
            if edge.dst_node not in cone:
                continue
            indegree[edge.dst_node] -= 1
            if indegree[edge.dst_node] == 0:
                ready.append(edge.dst_node)
    if len(order) != len(cone):
        raise GraphCompileError("cycle", "依赖锥内存在环")

    # ── typed passes + liveness ──
    index_of = {nid: i for i, nid in enumerate(order)}
    width, height = int(size[0]), int(size[1])
    bytes_per_output = width * height * BYTES_PER_PIXEL_RGBA16F

    passes: list[RenderPass] = []
    consumer_count = {nid: 0 for nid in order}
    for edge in cone_edges:
        consumer_count[edge.src_node] += 1

    for index, nid in enumerate(order):
        node = by_id[nid]
        known = node.known_type()
        op = known.value if known else "unknown"
        inputs = []
        for edge in sorted(incoming.get(nid, []), key=lambda e: e.edge_id):
            if edge.src_node not in cone:
                continue
            inputs.append((edge.dst_port, PlanPortRef(
                pass_index=index_of[edge.src_node],
                node_id=edge.src_node,
                port=edge.src_port)))
        outputs = tuple(p.port_id for p in node.ports()
                        if p.direction.value == "out") if known else ("image",)
        passes.append(RenderPass(
            index=index,
            node_id=nid,
            node_type=node.node_type,
            op=op,
            params=node.params,
            inputs=tuple(inputs),
            outputs=outputs,
            consumers=consumer_count[nid],
            estimated_bytes=bytes_per_output,
            shared_definition_id=node.shared_definition_id))

    # release_after = max(消费者 index)；分叉输入保持到所有消费者完成
    last_use: dict[str, int] = {}
    for render_pass in passes:
        for _port, ref in render_pass.inputs:
            if ref is not None:
                last_use[ref.node_id] = max(
                    last_use.get(ref.node_id, -1), render_pass.index)
    for render_pass in passes:
        object.__setattr__(
            render_pass, "release_after",
            last_use.get(render_pass.node_id, -1))

    # 峰值 live 统计（每 pass 后：源 + 未释放输出）
    peak_textures = 0
    peak_bytes = 0
    live: set[str] = set()
    for render_pass in passes:
        live.add(render_pass.node_id)   # 本 pass 输出生效
        if len(live) + 1 > peak_textures:   # +1 = 源输入常驻
            peak_textures = len(live) + 1
            peak_bytes = peak_textures * bytes_per_output
        for done in [p.node_id for p in passes
                     if p.release_after == render_pass.index]:
            live.discard(done)

    plan = ColorGraphRenderPlan(
        snapshot_key=snapshot.cache_key(),
        target=target,
        passes=passes,
        peak_live_bytes=peak_bytes,
        peak_live_textures=peak_textures,
        output_pass_index=index_of.get(target_node, -1),
        compile_ms=(time.perf_counter() - started) * 1000.0,
        warnings=tuple(warnings))

    if cache is not None:
        cache[snapshot.cache_key()] = plan
    return plan


def compute_liveness(plan: ColorGraphRenderPlan) -> Mapping[str, int]:
    """node_id → release_after（B09 执行器资源归还表）。"""
    return {p.node_id: p.release_after for p in plan.passes}
