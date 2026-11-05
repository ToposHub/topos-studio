"""legacy 调色栈 → ColorGraph 无损迁移（N03，B03）。

职责（计划 §3.1 B03 / §4.1/§4.2）：

- ``migrate_legacy_stack``：按 legacy 执行顺序生成 Source → C0 … Cn →
  Output。每个 Corrector 携带完整 ``Layer.to_dict(None)`` 载荷
  （``legacy_payload``：参数/Window/shape IDs/bitmap 引用/tracking/
  keyframes/插件/bake 状态逐字节守恒）+ 结构化图侧投影（enabled、
  key_source_mode=legacy_raw、bake/AI 语义标记）。
- ``can_project_to_legacy_layers``：**严格**判定图是否能无损压平成旧
  layers（纯串行、无 key/共享/混合器/compound/alpha/未知类型）；
  复杂 DAG 一律拒绝，禁止静默丢分支。
- ``project_legacy_layers``：可投影图的 Layer 载荷重建（B06 前旧引擎
  的事实来源；迁移后初次水合即走此路径）。

幂等：对已携带合法 ``color_graph`` 的栈再迁移返回原图不变。失败抛
``LegacyMigrationError``，定位到 stack / node / resource。
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any, Mapping

from src.shared.project.color_graph import (
    ColorEdgeData,
    ColorGraphData,
    ColorNodeData,
    GraphSchemaError,
    NodeType,
    unfreeze_params,
)
from src.shared.project.color_stack import ColorStackData


class LegacyMigrationError(ValueError):
    """迁移失败：stack/node/resource 定位 + 稳定 code。"""

    def __init__(self, code: str, message: str, *,
                 layer_index: int | None = None,
                 layer_id: str | None = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.layer_index = layer_index
        self.layer_id = layer_id


#: 图侧 Corrector 参数中迁移语义的固定键（§4.1/§4.2）
KEY_SOURCE_MODE = "key_source_mode"
LEGACY_RAW = "legacy_raw"
SOURCE_LAYER_ID = "source_layer_id"
BAKE_STATE = "bake_state"
AI_OVERRIDE = "ai_override"

_PROJECTABLE_TYPES = frozenset({
    NodeType.SOURCE.value, NodeType.CORRECTOR.value, NodeType.OUTPUT.value,
})


def _corrector_from_layer(layer_dict: Mapping[str, Any], index: int,
                          ) -> ColorNodeData:
    layer_id = layer_dict.get("id")
    if not layer_id:
        raise LegacyMigrationError(
            "layer_missing_id", f"第 {index} 层缺 id，无法映射稳定 UUID",
            layer_index=index)
    params: dict[str, Any] = {
        KEY_SOURCE_MODE: LEGACY_RAW,   # §4.1：旧节点 qualifier 采样 RAW preview
        SOURCE_LAYER_ID: str(layer_id),  # 仅迁移追踪，不是身份
        "blend_mode": layer_dict.get("blend_mode", "Normal"),
        "opacity": float(layer_dict.get("opacity", 1.0)),
    }
    # §4.2 bake：冻结"该节点完整上游依赖输出"状态 + digest（旧栈可能无）
    if layer_dict.get("is_baked"):
        params[BAKE_STATE] = {
            "is_baked": True,
            "baked_file_name": layer_dict.get("baked_file_name"),
            "prefix_digest": None,  # 旧无 digest → 兼容策略 + 诊断（B14 深化）
            "digest_known": False,
        }
    # §4.2 AI：最后一个启用 AI 插件结果替代该层输入（既有语义原样迁移）
    ai_plugins = [
        p for p in (layer_dict.get("plugins") or ())
        if isinstance(p, Mapping) and p.get("type") == "ai" and p.get("enabled", True)
    ]
    if ai_plugins:
        params[AI_OVERRIDE] = {
            "plugin_ids": [p.get("plugin_id") for p in ai_plugins],
            "replaces_input": True,
        }
    return ColorNodeData(
        node_id=_stable_node_uuid(str(layer_id)),
        node_type=NodeType.CORRECTOR.value,
        params=params,
        enabled=bool(layer_dict.get("visible", True)),
        label=str(layer_dict.get("name") or f"Layer {index + 1}"),
        legacy_payload=dict(layer_dict),
    )


_UUID_CACHE: dict[str, str] = {}


def _stable_node_uuid(layer_id: str) -> str:
    """layer_id → 稳定节点 UUID（同 id 重复迁移得到同 UUID：幂等关键）。"""
    if layer_id not in _UUID_CACHE:
        _UUID_CACHE[layer_id] = str(uuid.uuid5(uuid.NAMESPACE_URL,
                                               f"topos-color-node/{layer_id}"))
    return _UUID_CACHE[layer_id]


def migrate_legacy_stack(stack: ColorStackData) -> ColorGraphData:
    """把 legacy 栈迁移为权威 ColorGraphData。

    - 栈已携带合法 ``color_graph`` → 原样返回（幂等，不再从 layers 重建，
      禁止双权威）。
    - v2+（stack.layers 非空）→ 逐层 Corrector 链。
    - v1（平铺键）→ 单 Corrector（平铺参数进 legacy_payload 视图）。
    """
    existing = getattr(stack, "color_graph", None)
    if existing is not None:
        if not isinstance(existing, ColorGraphData):
            raise LegacyMigrationError(
                "invalid_graph", "stack.color_graph 类型非法（损坏的工程数据）")
        if existing.validate():
            raise LegacyMigrationError(
                "invalid_graph",
                f"stack.color_graph 结构非法: {existing.validate()[0]}")
        return existing

    source = ColorNodeData(
        node_id=_stable_node_uuid(f"source/{stack.stack_id}"),
        node_type=NodeType.SOURCE.value, label="Source",
        params={KEY_SOURCE_MODE: LEGACY_RAW})  # legacy_raw key source reference
    output = ColorNodeData(
        node_id=_stable_node_uuid(f"output/{stack.stack_id}"),
        node_type=NodeType.OUTPUT.value, label="Output")

    nodes: list[ColorNodeData] = [source]
    edges: list[ColorEdgeData] = []
    prev = source.node_id

    if stack.layers:
        for index, layer_dict in enumerate(stack.layers):
            if not isinstance(layer_dict, Mapping):
                raise LegacyMigrationError(
                    "layer_payload_corrupt",
                    f"第 {index} 层载荷不是 Mapping: {type(layer_dict)!r}",
                    layer_index=index)
            node = _corrector_from_layer(layer_dict, index)
            nodes.append(node)
            edges.append(ColorEdgeData(
                edge_id=_stable_node_uuid(f"edge/{prev}/{node.node_id}"),
                src_node=prev, src_port="image",
                dst_node=node.node_id, dst_port="image"))
            prev = node.node_id
    elif _flat_view_nonempty(stack):
        # v1：单 Corrector 承载平铺参数（含 curves/mixer/soft_clip/lut）
        flat_layer = _flat_layer_view(stack)
        node = _corrector_from_layer(flat_layer, 0)
        nodes.append(node)
        edges.append(ColorEdgeData(
            edge_id=_stable_node_uuid(f"edge/{prev}/{node.node_id}"),
            src_node=prev, src_port="image",
            dst_node=node.node_id, dst_port="image"))
        prev = node.node_id
    else:
        # 空 legacy 层：Source → Output 直连（B03.3 明确 fallback）
        pass

    nodes.append(output)
    edges.append(ColorEdgeData(
        edge_id=_stable_node_uuid(f"edge/{prev}/{output.node_id}"),
        src_node=prev, src_port="image",
        dst_node=output.node_id, dst_port="image"))

    try:
        graph = ColorGraphData(
            nodes=tuple(nodes), edges=tuple(edges),
            source_node_id=source.node_id,
            output_node_id=output.node_id,
            working_color_context={"origin": "legacy_migration",
                                   "stack_schema": "v2+" if stack.layers else "v1"},
            graph_revision=1,
        )
        errors = graph.validate()
        if errors:
            raise LegacyMigrationError(
                "invalid_graph", f"迁移产物非法: {errors[0]}")
    except GraphSchemaError as exc:
        # 重复 layer id / 大小超限等构造期错误 → 迁移诊断（定位 layer）
        raise LegacyMigrationError(
            "invalid_graph", f"迁移产物非法: {exc}",
            layer_id=getattr(exc, "node_id", None)) from None
    return graph


def _flat_view_nonempty(stack: ColorStackData) -> bool:
    grading = stack.grading_params or {}
    defaults = ColorStackData.create(asset_id=None, name="probe").grading_params
    differs = any(grading.get(k) != v for k, v in defaults.items())
    return bool(differs or stack.curves_data or stack.hsl_curve_data
                or stack.mixer_params or stack.lut_path
                or stack.soft_clip_params.get("enabled"))


def reconcile_projectable_graph(
    graph: ColorGraphData,
    layers: Any,
    *,
    active_layer_index: int = 0,
) -> tuple[ColorGraphData, bool]:
    """保存边界：可投影图与引擎层快照一致化（B06 collect 权威图收口）。

    背景：legacy 写通道（wheel 手势/层操作）只落 ``stack.layers``，
    ``stack.color_graph`` 不动——重载时图优先（E07）会静默丢掉这些编辑。
    可投影（串行）图的 layers 是两通道的**并集视图**（图命令提交后会重
    投影引擎），因此以 layers 重新迁移即全量一致（参数/顺序/增删/旁路）。

    - 无变化（layers 与当前图投影 JSON 形态一致）→ ``(graph, False)``
      （不动 revision，避免每次手势提交推 revision）；
    - 有变化 → 重新迁移，**保留旧图布局**（``ui_state``，node_id 稳定）
      与 shared/锁等 layers 表达不了的图侧状态，revision = 旧 + 1；
    - 图不可投影（分支/compound 等复杂结构 layers 表达不了）→
      ``(graph, False)``：图保持权威不同步（legacy 写通道对复杂图的
      失配是登记债务，见台账 E10/E27；不应把复杂图压平丢结构）。
    """
    import json as _json

    if not isinstance(layers, (list, tuple)):
        return graph, False
    if not can_project_to_legacy_layers(graph):
        return graph, False

    def _norm(payloads: Any) -> str | None:
        try:
            return _json.dumps(payloads, default=list, sort_keys=True)
        except (TypeError, ValueError):
            return None  # 快照含不可序列化值：跳过相等捷径，走迁移校验

    try:
        current_projection = project_legacy_layers(graph)
    except LegacyMigrationError:
        return graph, False
    if (_norm(current_projection) is not None
            and _norm(current_projection) == _norm(list(layers))):
        return graph, False

    probe = ColorStackData.create(asset_id=None, name="reconcile")
    probe_stack = probe.with_layers(
        [dict(p) for p in layers],
        active_layer_index=int(active_layer_index or 0))
    try:
        fresh = migrate_legacy_stack(probe_stack)
    except Exception:  # noqa: BLE001 —— 坏快照不碰权威图
        return graph, False
    if fresh.validate():
        return graph, False
    reconciled = replace(
        fresh,
        graph_id=graph.graph_id,
        shared_definitions=graph.shared_definitions,
        working_color_context=graph.working_color_context,
        ui_state=graph.ui_state,
        minimum_reader_version=max(
            graph.minimum_reader_version, fresh.minimum_reader_version),
        graph_revision=int(graph.graph_revision) + 1,
    )
    if reconciled.validate():
        return graph, False
    return reconciled, True


def _flat_layer_view(stack: ColorStackData) -> dict[str, Any]:
    """v1 平铺键 → 等价单层 Layer 载荷（保持旧 v1 水合语义的字段形态）。"""
    grading = dict(stack.grading_params or {})
    # v1 兼容双名（temp/temperature、shadow/shadows）对齐 Layer 默认形态
    if "temperature" in grading:
        grading.setdefault("temp", grading["temperature"])
    if "shadows" in grading:
        grading.setdefault("shadow", grading["shadows"])
    return {
        "id": _stable_node_uuid(f"flat/{stack.stack_id}"),
        "name": stack.name or "Layer 1",
        "visible": True,
        "opacity": 1.0,
        "blend_mode": "Normal",
        "grading_params": grading,
        "curves_data": dict(stack.curves_data or {}),
        "hsl_curve_data": dict(stack.hsl_curve_data or {}),
        "mixer_params": dict(stack.mixer_params or {}),
        "soft_clip_params": dict(stack.soft_clip_params or {}),
        "active_lut_path": stack.lut_path,
        "lut_intensity": stack.lut_intensity,
    }


# ── 反向投影（严格） ─────────────────────────────────────────────────────

def can_project_to_legacy_layers(graph: ColorGraphData) -> bool:
    """严格判定：图必须能**无损**表达为旧 layers 数组。

    拒绝条件（任一即 False）：
    - 非法图 / 端子不唯一；
    - 存在 key 边、mixer/splitter/combiner/compound/outside/alpha/
      shared/未知类型节点；
    - 存在 shared_definitions；
    - 非 Source→C…→Output 单链（分叉/旁路/乱序）。

    Corrector 可由 ``node_layer_payload`` 物化为默认 Layer DTO；空载荷
    节点也有与 node_id 相同的稳定图层身份。
    """
    if not isinstance(graph, ColorGraphData) or graph.validate():
        return False
    if graph.shared_definitions:
        return False
    if graph.alpha_output_node_id:
        return False
    for node in graph.nodes:
        if node.node_type not in _PROJECTABLE_TYPES:
            return False

    # 单链校验：从 source 出发沿唯一 image 边走到 output，节点数 = 边数 + 1
    by_id = {n.node_id: n for n in graph.nodes}
    out_edges: dict[str, list[ColorEdgeData]] = {}
    for edge in graph.edges:
        out_edges.setdefault(edge.src_node, []).append(edge)
        if edge.src_port != "image" or edge.dst_port != "image":
            return False
    current = graph.source_node_id
    visited = 0
    seen: set[str] = set()
    while current != graph.output_node_id:
        outs = out_edges.get(current, [])
        if len(outs) != 1:
            return False  # 分叉/断链
        nxt = outs[0].dst_node
        if nxt in seen or nxt not in by_id:
            return False
        seen.add(nxt)
        current = nxt
        visited += 1
    # 所有节点都在链上（无孤立/旁路节点）
    return len(seen) + 1 == len(graph.nodes) and len(graph.edges) == len(graph.nodes) - 1


def project_legacy_layers(graph: ColorGraphData) -> list[dict[str, Any]]:
    """可投影图 → Layer 载荷列表（执行顺序）。不可投影抛错（不静默压平）。"""
    if not can_project_to_legacy_layers(graph):
        raise LegacyMigrationError(
            "not_projectable",
            "图含分支/key/共享/compound 等复杂结构，禁止压平为 legacy layers")
    layers: list[dict[str, Any]] = []
    by_id = {n.node_id: n for n in graph.nodes}
    out_edges: dict[str, list[ColorEdgeData]] = {}
    for edge in graph.edges:
        out_edges.setdefault(edge.src_node, []).append(edge)
        current = by_id[graph.source_node_id]
    while current.node_id != graph.output_node_id:
        nxt_id = out_edges[current.node_id][0].dst_node
        current = by_id[nxt_id]
        if current.node_type == NodeType.CORRECTOR.value:
            # 生产合并（S04/B06.5）：legacy_payload 基底 + node.params
            # 结构化域（grade/qualifier/opacity/blend/enabled）——图命令
            # 编辑过的 grade 必须随投影回到引擎，否则图→引擎回路丢参数。
            # include_lut_cache=False：持久化边界语义（Layer.from_dict
            # 按控制点重建 LUT，载荷守恒按 JSON 形态比较）
            from src.features.color.core.color_graph_param_source import (
                node_layer_payload,
            )
            payload = node_layer_payload(current, include_lut_cache=False)
            if payload is not None:
                layers.append(payload)
    return layers
