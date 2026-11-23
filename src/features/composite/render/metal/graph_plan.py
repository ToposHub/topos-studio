"""Metal 合成链的执行计划与几何推导（CPU/Metal 双后端共享）。

本模块把序列化合成图（``NodeGraph.to_dict()`` 形式）整理成有序的节点
执行计划，并推导每个节点的画布、数据窗口（data rect）与纹理打包区域
（pack rect）。CPU 参考实现（`cpu_reference.py`）与 Metal 执行器
（`renderer.py`）消费同一份计划与参数包，保证两端语义一致。

坐标模型（top-down 图像坐标，半开区间 ``Rect = (x0, y0, x1, y1)``）：

- 每个节点输出一张「pack 区域大小」的纹理，内容按**绝对画布坐标**放置
  （pack 原点 = 区域原点）。采样输入时先做数据窗口包含检查，再用
  ``(abs - pack.xy + 0.5) / pack.size`` 换算 uv。
- 与 GL 路径的对应关系：GL 的 packed FBO + bbox 放置 ⇔ 本文的绝对放置
  （内容位于画布原位的 canvas 尺寸等价形式）。已声明的差异：Transform
  采用固定画布语义——移出画布的内容在中间级被裁掉（GL 的 bbox 跟随
  内容语义保留到 Merge 画布裁剪为止），见架构文档「已知差异」。
- tile 渲染：先按 halo 扩张 tile（`roi.input_roi`），所有节点在扩张区
  上执行，Output 节点只 dispatch tile 中心（裁回中心）。Transform 在
  反投影输入区域时计入滤镜插值余量（`roi.transform_input_rect`）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from src.features.composite.core.layer_model import (
    LAYER_MERGE_OWNER_PARAM,
)
from src.features.composite.nodes.blend_node import BLEND_MODES, MASK_CHANNELS
from src.features.composite.nodes.crop_node import OUTSIDE_POLICIES
from src.features.composite.nodes.transform_node import FILTER_MODES
from src.features.composite.nodes.transform_node import TransformNode

from . import roi
from .node_support import (
    METAL_COMPOSITE_SUPPORTED_TYPES,
    merged_node_parameters,
)

Rect = Tuple[int, int, int, int]

# Output 节点 alpha_policy → kernel u_alpha_mode 索引（_ALPHA_FRAGMENT）。
_OUTPUT_ALPHA_MODES = {"keep": 0, "premult": 1, "straight": 2, "discard": 3}
# Constant 节点 mode → kernel mode 索引。
_CONSTANT_MODES = {"solid": 0, "checker": 1, "gradient": 2, "radial": 3}


class PlanError(ValueError):
    """计划构建失败（图结构不满足 Metal 链的前提）。"""


@dataclass
class FrameMeta:
    """节点输出的几何/色彩元数据（与 TextureResult 的对应字段同义）。"""

    canvas_w: int
    canvas_h: int
    data_rect: Rect
    alpha_premultiplied: Optional[bool] = None
    color_space: Optional[str] = None
    # 无 GPU pass 的纯透传（crop reformat=False）复用输入纹理。
    passthrough: bool = False


@dataclass
class PlannedNode:
    """一个参与执行的节点及其解析后的参数。"""

    node_id: str
    node_type: str
    params: dict
    inputs: Dict[str, str] = field(default_factory=dict)  # port → node_id
    # 执行期填充。
    meta: Optional[FrameMeta] = None
    region: Rect = (0, 0, 0, 0)
    pack: Rect = (0, 0, 0, 0)


@dataclass
class ExecutionPlan:
    """整图执行计划。"""

    nodes: List[PlannedNode]
    output_node_id: str
    graph_revision: int
    frame: int
    output_canvas: Tuple[int, int] = (0, 0)
    # 调试/诊断。
    warnings: List[str] = field(default_factory=list)

    def node_index(self) -> Dict[str, PlannedNode]:
        return {node.node_id: node for node in self.nodes}


def build_execution_plan(
    graph_data: dict,
    timeline_frame: int = 0,
) -> ExecutionPlan:
    """从序列化图构建执行计划（不接触 GPU）。

    前提：图已通过 ``evaluate_metal_support`` 判定（无未迁移节点）。
    """
    nodes_raw = graph_data.get("nodes") or []
    connections = graph_data.get("connections") or []
    graph_revision = int(graph_data.get("graph_revision", 0) or 0)

    node_types: Dict[str, str] = {}
    planned: Dict[str, PlannedNode] = {}
    for nd in nodes_raw:
        nid = str(nd.get("id", ""))
        ntype = str(nd.get("type", ""))
        node_types[nid] = ntype
        if ntype not in METAL_COMPOSITE_SUPPORTED_TYPES:
            # 预检已拦截；此处防御性跳过（不产出半成品）。
            continue
        params = nd.get("parameters")
        planned[nid] = PlannedNode(
            node_id=nid,
            node_type=ntype,
            params=merged_node_parameters(
                ntype, params if isinstance(params, dict) else {}),
        )

    inputs_of: Dict[str, Dict[str, str]] = {nid: {} for nid in planned}
    consumers_of: Dict[str, List[str]] = {nid: [] for nid in planned}
    for conn in connections:
        src = str(conn.get("src_node_id", ""))
        dst = str(conn.get("dst_node_id", ""))
        port = str(conn.get("dst_port_name", ""))
        if src in planned and dst in planned:
            # 单连接端口：后写覆盖前写（与 NodeGraph.connect 的替换语义一致）。
            inputs_of[dst][port] = src
            consumers_of[src].append(dst)

    # 输出节点：优先 output_name == "display"，否则第一个有输入的 output。
    output_id = ""
    fallback_output_id = ""
    for nd in nodes_raw:
        nid = str(nd.get("id", ""))
        if node_types.get(nid) != "output" or nid not in planned:
            continue
        name = str((nd.get("parameters") or {}).get("output_name", "display"))
        if inputs_of.get(nid):
            if name == "display":
                output_id = output_id or nid
            fallback_output_id = fallback_output_id or nid
    output_id = output_id or fallback_output_id
    if not output_id:
        raise PlanError("图中没有带输入连接的 Output 节点")

    # 参与锥 + 拓扑序（Kahn）。
    contributing = {output_id}
    stack = [output_id]
    while stack:
        nid = stack.pop()
        for src in inputs_of[nid].values():
            if src not in contributing:
                contributing.add(src)
                stack.append(src)

    in_degree = {nid: 0 for nid in contributing}
    downstream: Dict[str, List[str]] = {nid: [] for nid in contributing}
    for nid in contributing:
        for src in inputs_of[nid].values():
            in_degree[nid] += 1
            downstream[src].append(nid)
    queue = sorted(nid for nid, deg in in_degree.items() if deg == 0)
    order: List[str] = []
    while queue:
        nid = queue.pop()
        order.append(nid)
        for consumer in downstream[nid]:
            in_degree[consumer] -= 1
            if in_degree[consumer] == 0:
                queue.append(consumer)
    if len(order) != len(contributing):
        raise PlanError("图中存在环路，无法确定求值顺序")

    plan = ExecutionPlan(
        nodes=[planned[nid] for nid in order if nid in planned],
        output_node_id=output_id,
        graph_revision=graph_revision,
        frame=int(timeline_frame),
    )
    for node in plan.nodes:
        node.inputs = {
            port: src for port, src in inputs_of[node.node_id].items()
        }

    # 正向推导元数据（canvas / data rect / alpha / color space）。
    metas: Dict[str, FrameMeta] = {}
    for node in plan.nodes:
        in_metas = {
            port: metas[src] for port, src in inputs_of[node.node_id].items()
            if src in metas
        }
        node.meta = compute_node_meta(node, in_metas, inputs_of[node.node_id])
        metas[node.node_id] = node.meta

    output_meta = metas[output_id]
    plan.output_canvas = (output_meta.canvas_w, output_meta.canvas_h)
    return plan


# ── 几何推导 ─────────────────────────────────────────────────────────────


def _full_rect(meta: FrameMeta) -> Rect:
    return (0, 0, meta.canvas_w, meta.canvas_h)


def compute_node_meta(
    node: PlannedNode,
    in_metas: Dict[str, FrameMeta],
    inputs: Dict[str, str],
) -> FrameMeta:
    """推导节点输出的 canvas / data rect / alpha 语义（纯函数）。"""
    t = node.node_type
    p = node.params

    if t == "constant":
        w = max(1, min(16384, int(p.get("width", 1920))))
        h = max(1, min(16384, int(p.get("height", 1080))))
        return FrameMeta(w, h, (0, 0, w, h),
                         alpha_premultiplied=False, color_space="linear")

    if t in ("noop", "dot"):
        src = in_metas.get(next(iter(inputs), ""))
        if src is None:
            raise PlanError(f"节点 {node.node_id} 缺少输入")
        return FrameMeta(src.canvas_w, src.canvas_h, src.data_rect,
                         src.alpha_premultiplied, src.color_space,
                         passthrough=True)

    if t == "premult":
        src = in_metas.get("image")
        if src is None:
            raise PlanError(f"节点 {node.node_id} 缺少 image 输入")
        return FrameMeta(src.canvas_w, src.canvas_h, src.data_rect,
                         alpha_premultiplied=True, color_space=src.color_space,
                         passthrough=src.alpha_premultiplied is True)

    if t == "unpremult":
        src = in_metas.get("image")
        if src is None:
            raise PlanError(f"节点 {node.node_id} 缺少 image 输入")
        return FrameMeta(src.canvas_w, src.canvas_h, src.data_rect,
                         alpha_premultiplied=False, color_space=src.color_space,
                         passthrough=src.alpha_premultiplied is False)

    if t == "transform":
        src = in_metas.get("image")
        if src is None:
            raise PlanError(f"节点 {node.node_id} 缺少 image 输入")
        if _is_identity_transform(p):
            return FrameMeta(src.canvas_w, src.canvas_h, src.data_rect,
                             src.alpha_premultiplied, src.color_space,
                             passthrough=True)
        matrix = transform_absolute_matrix(p, src.canvas_w, src.canvas_h)
        moved = _transform_rect(src.data_rect, matrix)
        return FrameMeta(src.canvas_w, src.canvas_h, moved,
                         src.alpha_premultiplied, src.color_space)

    if t == "crop":
        src = in_metas.get("image")
        if src is None:
            raise PlanError(f"节点 {node.node_id} 缺少 image 输入")
        crop_x = max(0, int(p.get("x", 0)))
        crop_y = max(0, int(p.get("y", 0)))
        crop_w = max(1, int(p.get("width", 512)))
        crop_h = max(1, int(p.get("height", 512)))
        reformat = bool(p.get("reformat", True))
        if reformat:
            return FrameMeta(crop_w, crop_h, (0, 0, crop_w, crop_h),
                             src.alpha_premultiplied, src.color_space)
        # 仅元数据：数据窗口 = 裁剪框 ∩ 输入窗口（Y-up 参数换算成 top-down）。
        y0 = src.canvas_h - (crop_y + crop_h)
        crop_rect_td = roi.clamp_rect(
            (crop_x, y0, crop_x + crop_w, y0 + crop_h),
            src.canvas_w, src.canvas_h)
        data = roi.rects_intersection(crop_rect_td, src.data_rect)
        return FrameMeta(src.canvas_w, src.canvas_h, data,
                         src.alpha_premultiplied, src.color_space,
                         passthrough=True)

    if t == "mask":
        has_input = "image" in in_metas and in_metas["image"] is not None
        if has_input:
            src = in_metas["image"]
            return FrameMeta(src.canvas_w, src.canvas_h, src.data_rect,
                             alpha_premultiplied=True,
                             color_space=src.color_space)
        w = max(1, int(p.get("output_width", 1920)))
        h = max(1, int(p.get("output_height", 1080)))
        return FrameMeta(w, h, (0, 0, w, h),
                         alpha_premultiplied=False, color_space="linear")

    if t == "merge":
        meta_a = in_metas.get("A")
        meta_b = in_metas.get("B")
        if meta_a is None and meta_b is None:
            raise PlanError(f"节点 {node.node_id} 缺少 A/B 输入")
        if meta_a is None:
            return FrameMeta(meta_b.canvas_w, meta_b.canvas_h, meta_b.data_rect,
                             meta_b.alpha_premultiplied, meta_b.color_space,
                             passthrough=True)
        alpha_policy = str(p.get("alpha_policy", "auto"))
        if meta_b is None:
            if not bool(p.get(LAYER_MERGE_OWNER_PARAM, "")):
                return FrameMeta(meta_a.canvas_w, meta_a.canvas_h,
                                 meta_a.data_rect, meta_a.alpha_premultiplied,
                                 meta_a.color_space, passthrough=True)
            # 受管图层缺少背景：以 A 提供几何、按透明背景渲染。
            return FrameMeta(meta_a.canvas_w, meta_a.canvas_h, meta_a.data_rect,
                             alpha_policy != "output_straight",
                             meta_a.color_space)
        canvas = _merge_canvas(meta_a, meta_b,
                               str(p.get("format_policy", "from_b")))
        data = _merge_data_rect(meta_a, meta_b,
                                str(p.get("bbox_policy", "union")), canvas)
        return FrameMeta(canvas[0], canvas[1], data,
                         alpha_policy != "output_straight",
                         meta_b.color_space or meta_a.color_space)

    if t == "output":
        src = in_metas.get("image")
        format_mode = str(p.get("format_mode", "input"))
        if format_mode == "custom":
            w = max(1, min(16384, int(p.get("width", 1920))))
            h = max(1, min(16384, int(p.get("height", 1080))))
        else:
            if src is None:
                raise PlanError(
                    f"节点 {node.node_id} 的 format_mode=input 但没有输入")
            w, h = src.canvas_w, src.canvas_h
        data = roi.clamp_rect(src.data_rect, w, h) if src is not None \
            else (0, 0, w, h)
        alpha_policy = str(p.get("alpha_policy", "keep"))
        bg = _parse_output_bg(str(p.get("background_color", "0,0,0,0")))
        if bg[3] > 0.0:
            premult: Optional[bool] = True
        elif alpha_policy == "premult":
            premult = True
        elif alpha_policy == "straight":
            premult = False
        else:
            premult = src.alpha_premultiplied if src is not None else None
        return FrameMeta(w, h, data, premult,
                         src.color_space if src is not None else "linear")

    raise PlanError(f"节点类型 {t!r} 不在 Metal 执行计划内")


def _merge_canvas(a: FrameMeta, b: FrameMeta, policy: str) -> Tuple[int, int]:
    if policy == "from_a":
        return (a.canvas_w, a.canvas_h)
    if policy == "larger":
        return (max(a.canvas_w, b.canvas_w), max(a.canvas_h, b.canvas_h))
    if policy == "smaller":
        return (min(a.canvas_w, b.canvas_w), min(a.canvas_h, b.canvas_h))
    return (b.canvas_w, b.canvas_h)  # from_b（默认）


def _merge_data_rect(a: FrameMeta, b: FrameMeta, policy: str,
                     canvas: Tuple[int, int]) -> Rect:
    if policy == "intersection":
        rect = roi.rects_intersection(a.data_rect, b.data_rect)
    elif policy == "a":
        rect = a.data_rect
    elif policy == "b":
        rect = b.data_rect
    else:
        rect = roi.rects_union(a.data_rect, b.data_rect)
    clamped = roi.clamp_rect(rect, canvas[0], canvas[1])
    if clamped == (0, 0, 0, 0):
        return (0, 0, canvas[0], canvas[1])
    return clamped


def _parse_output_bg(raw: str) -> Tuple[float, float, float, float]:
    try:
        parts = [float(v) for v in str(raw).split(",")]
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0, 0.0)
    if len(parts) < 3:
        return (0.0, 0.0, 0.0, 0.0)
    if len(parts) == 3:
        parts.append(1.0)
    return tuple(max(0.0, min(1.0, v)) for v in parts[:4])  # type: ignore[return-value]


# ── Transform 矩阵（复用 TransformNode 的实现，绝对画布坐标） ────────────

_TRANSFORM_NODE = TransformNode(node_id="__metal_plan__")


def _is_identity_transform(params: dict) -> bool:
    return (
        float(params.get("translate_x", 0.0)) == 0.0
        and float(params.get("translate_y", 0.0)) == 0.0
        and float(params.get("scale_x", 1.0)) == 1.0
        and float(params.get("scale_y", 1.0)) == 1.0
        and float(params.get("rotation", 0.0)) == 0.0
    )


def transform_absolute_matrix(
    params: dict, canvas_w: int, canvas_h: int
) -> Tuple[float, float, float, float, float, float]:
    """前向仿射（in_abs → out_abs，top-down 像素空间）。

    与 ``TransformNode._build_transform_matrix(params, w, h)`` 同源：pivot
    按画布解析（center 模式取画布中心），正 translate_y 向下。
    """
    return _TRANSFORM_NODE._build_transform_matrix(params, canvas_w, canvas_h)


def inverse_affine(
    matrix: Tuple[float, float, float, float, float, float]
) -> Tuple[float, float, float, float, float, float]:
    """2×2 仿射的闭式逆（与 TransformNode._build_inverse_matrix 同式）。"""
    m00, m10, m01, m11, m02, m12 = matrix
    det = m00 * m11 - m01 * m10
    if abs(det) < 1e-12:
        return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    i00, i01 = m11 / det, -m01 / det
    i10, i11 = -m10 / det, m00 / det
    i02 = -(i00 * m02 + i01 * m12)
    i12 = -(i10 * m02 + i11 * m12)
    return (i00, i01, i02, i10, i11, i12)


def _transform_rect(rect: Rect, matrix: Tuple[float, ...]) -> Rect:
    """矩形四角过前向仿射后的外接半开矩形（floor/ceil）。"""
    m00, m10, m01, m11, m02, m12 = matrix
    x0, y0, x1, y1 = rect
    xs: List[float] = []
    ys: List[float] = []
    for cx in (float(x0), float(x1)):
        for cy in (float(y0), float(y1)):
            xs.append(m00 * cx + m01 * cy + m02)
            ys.append(m10 * cx + m11 * cy + m12)
    rx0, ry0 = int(math.floor(min(xs))), int(math.floor(min(ys)))
    rx1, ry1 = int(math.ceil(max(xs))), int(math.ceil(max(ys)))
    if rx1 <= rx0 or ry1 <= ry0:
        return (0, 0, 0, 0)
    return (rx0, ry0, rx1, ry1)


def transform_filter_halo(params: dict) -> int:
    """Transform 的插值余量（nearest 0 / linear 1）。"""
    filter_name = str(params.get("filter", "linear"))
    return 0 if filter_name == "nearest" else 1


# ── 区域传播（按全图坐标反推每个输入 ROI） ───────────────────────────────


def input_region_request(
    consumer: PlannedNode,
    input_meta: FrameMeta,
    consumer_region: Rect,
    is_tile_pass: bool,
) -> Rect:
    """消费者节点的执行区域反推到某个输入所需的区域。

    - 逐像素节点（merge/premult/mask/noop/output）：同一区域（绝对采样）。
    - transform：逆投影 + 插值余量（tile pass 时；全帧时结果即全画布）。
    - crop（reformat）：平移到源画布坐标；非 reformat 为透传。
    """
    t = consumer.node_type
    if t == "transform":
        if not is_tile_pass:
            return (0, 0, input_meta.canvas_w, input_meta.canvas_h)
        matrix = transform_absolute_matrix(
            consumer.params, input_meta.canvas_w, input_meta.canvas_h)
        return roi.transform_input_rect(
            consumer_region, matrix,
            input_meta.canvas_w, input_meta.canvas_h,
            extra_margin=transform_filter_halo(consumer.params))
    if t == "crop" and bool(consumer.params.get("reformat", True)):
        src_x0, src_y0 = crop_source_origin(consumer, input_meta)
        return (consumer_region[0] + src_x0, consumer_region[1] + src_y0,
                consumer_region[2] + src_x0, consumer_region[3] + src_y0)
    return consumer_region


def crop_source_origin(node: PlannedNode, src_meta: FrameMeta) -> Tuple[int, int]:
    """crop 的源采样原点（top-down；节点 Y-up 参数换算）。"""
    crop_x = max(0, int(node.params.get("x", 0)))
    crop_y = max(0, int(node.params.get("y", 0)))
    crop_h = max(1, int(node.params.get("height", 512)))
    return crop_x, src_meta.canvas_h - (crop_y + crop_h)


def assign_execution_regions(
    plan: ExecutionPlan,
    render_region: Rect,
    is_tile_pass: bool,
) -> None:
    """反向传播执行区域：输出节点渲染 ``render_region``，反推各输入 ROI。

    多消费者取并集（与调度器 ROI 传播一致）。区域最终夹到各节点画布。
    """
    index = plan.node_index()
    requests: Dict[str, Rect] = {plan.output_node_id: render_region}
    consumers_of: Dict[str, List[str]] = {node.node_id: []
                                          for node in plan.nodes}
    for node in plan.nodes:
        for src in node.inputs.values():
            consumers_of[src].append(node.node_id)

    for node in reversed(plan.nodes):
        region = requests.get(node.node_id, (0, 0, 0, 0))
        meta = node.meta
        assert meta is not None
        clamped = roi.clamp_rect(region, meta.canvas_w, meta.canvas_h)
        if clamped == (0, 0, 0, 0):
            # 区域与画布无交集（例如 tile 落在内容之外）：保留空区域，
            # dispatch 尺寸为 0 的 pass 由执行器跳过。
            clamped = (0, 0, 0, 0)
        node.region = clamped
        node.pack = clamped
        for port, src_id in node.inputs.items():
            src = index.get(src_id)
            if src is None or src.meta is None:
                continue
            need = input_region_request(node, src.meta, clamped, is_tile_pass)
            existing = requests.get(src_id)
            requests[src_id] = need if existing is None \
                else roi.rects_union(existing, need)


# ── 参数包（CPU 参考与 Metal kernel 共用的字段值） ────────────────────────


def constant_params(node: PlannedNode) -> dict:
    p = node.params
    mode = _CONSTANT_MODES.get(str(p.get("mode", "solid")), 0)
    c1 = _constant_color(p, "color",
                         ("color_r", "color_g", "color_b", "color_a"),
                         (0.0, 0.0, 0.0, 1.0))
    c2 = _constant_color(p, "color2",
                         ("color2_r", "color2_g", "color2_b", "color2_a"),
                         (1.0, 1.0, 1.0, 1.0))
    checker = max(2, int(p.get("checker_size", 64)))
    meta = node.meta
    assert meta is not None
    return {
        "mode": mode,
        "color1": c1,
        "color2": c2,
        "checker_size": checker,
        "start_x": float(p.get("start_x", 0.0)),
        "start_y": float(p.get("start_y", 0.5)),
        "end_x": float(p.get("end_x", 1.0)),
        "end_y": float(p.get("end_y", 0.5)),
        "center_x": float(p.get("center_x", 0.5)),
        "center_y": float(p.get("center_y", 0.5)),
        "radial_radius": max(0.001, float(p.get("radial_radius", 0.5))),
        "feather": float(p.get("feather", 0.0)),
        "canvas_w": meta.canvas_w,
        "canvas_h": meta.canvas_h,
    }


def _constant_color(p: dict, tuple_key: str, mirrors: Tuple[str, str, str, str],
                    default: Tuple[float, float, float, float]) -> Tuple[float, ...]:
    """与 ConstantNode._color_value 同序：原子 color 参数优先，通道镜像兜底。"""
    raw = p.get(tuple_key)
    if isinstance(raw, (tuple, list)) and len(raw) == 4:
        return tuple(float(v) for v in raw)
    return tuple(float(p.get(name, d)) for name, d in zip(mirrors, default))


def transform_params(node: PlannedNode, in_meta: FrameMeta) -> dict:
    matrix = transform_absolute_matrix(
        node.params, in_meta.canvas_w, in_meta.canvas_h)
    i00, i01, i02, i10, i11, i12 = inverse_affine(matrix)
    filter_name = str(node.params.get("filter", "linear"))
    # 像素中心对齐（GL 语义）：输出像素 i 的中心 (i+0.5) 过逆矩阵得到
    # 输入连续坐标；kernel 的采样式以「texel 起点 + 0.5」为中心，因此
    # 把平移项折算 -0.5 + 0.5*线性部分，使 s = M(i + 0.5)。
    # 纯整数平移时该修正为零（与 GL 逐像素对齐一致）。
    i02 += 0.5 * (i00 + i01) - 0.5
    i12 += 0.5 * (i10 + i11) - 0.5
    # in_rect_* 描述输入纹理的打包区域（执行器按实际 pack 覆盖）。
    return {
        "i00": i00, "i01": i01, "i02": i02,
        "i10": i10, "i11": i11, "i12": i12,
        "in_rect_x": 0, "in_rect_y": 0,
        "in_w": in_meta.canvas_w, "in_h": in_meta.canvas_h,
        "filter": FILTER_MODES.index(filter_name)
        if filter_name in FILTER_MODES else 0,
    }


def fill_input_pack(params: dict, pack: Rect) -> None:
    """把输入纹理的实际打包区域写进参数包（in_rect/in_w/in_h 四元组）。"""
    params["in_rect_x"], params["in_rect_y"] = pack[0], pack[1]
    params["in_w"] = pack[2] - pack[0]
    params["in_h"] = pack[3] - pack[1]


def fill_merge_packs(params: dict, pack_a: Rect, pack_b: Rect,
                     pack_mask: Rect) -> None:
    params["pack_a"] = pack_a
    params["pack_b"] = pack_b
    params["pack_mask"] = pack_mask


def merge_params(node: PlannedNode, meta_a: Optional[FrameMeta],
                 meta_b: Optional[FrameMeta],
                 meta_mask: Optional[FrameMeta]) -> dict:
    p = node.params
    alpha_policy = str(p.get("alpha_policy", "auto"))
    blend_mode = str(p.get("blend_mode", "over"))
    mask_channel = str(p.get("mask_channel", "luma"))
    full = (0, 0, 0, 0)

    def _rect_or(meta: Optional[FrameMeta]) -> Tuple[int, int, int, int]:
        return meta.data_rect if meta is not None else full

    def _straight(meta: Optional[FrameMeta], policy: str) -> int:
        if policy == "premult_force":
            return 0
        if policy == "straight_force":
            return 1
        return 1 if (meta is not None
                     and meta.alpha_premultiplied is False) else 0

    layer_owner = bool(p.get(LAYER_MERGE_OWNER_PARAM, ""))
    return {
        "canvas_w": node.meta.canvas_w if node.meta else 0,
        "canvas_h": node.meta.canvas_h if node.meta else 0,
        "rect_a": _rect_or(meta_a),
        "rect_b": _rect_or(meta_b),
        "rect_mask": _rect_or(meta_mask),
        # 打包区域由执行器按各输入节点的实际 pack 填充（见 fill_merge_packs）。
        "pack_a": (0, 0, 0, 0),
        "pack_b": (0, 0, 0, 0),
        "pack_mask": (0, 0, 0, 0),
        "has_mask": 1 if meta_mask is not None else 0,
        "blend_mode": BLEND_MODES.index(blend_mode)
        if blend_mode in BLEND_MODES else 0,
        "opacity": float(p.get("opacity", 1.0)),
        "mix": float(p.get("mix", 1.0)),
        "a_straight": _straight(meta_a, alpha_policy),
        "b_straight": _straight(meta_b, alpha_policy),
        "alpha_clamp": 1 if bool(p.get("alpha_clamp", True)) else 0,
        "output_unpremult": 1 if alpha_policy == "output_straight" else 0,
        "mask_channel": MASK_CHANNELS.index(mask_channel)
        if mask_channel in MASK_CHANNELS else 0,
        "mask_invert": 1 if bool(p.get("mask_invert", False)) else 0,
        "transparent_background": 1 if meta_b is None and layer_owner else 0,
        "layer_composite": 1 if layer_owner else 0,
        "mask_straight": 1 if (meta_mask is not None
                               and meta_mask.alpha_premultiplied is False) else 0,
    }


def alpha_op_params(mode: int) -> dict:
    return {"mode": mode}


def crop_params(node: PlannedNode, src_meta: FrameMeta) -> dict:
    outside = str(node.params.get("outside",
                                  node.params.get("outside_policy",
                                                  "transparent")))
    src_x0, src_y0 = crop_source_origin(node, src_meta)
    return {
        # abs 原点 = 源画布采样原点 + 区域原点（执行器补区域原点）。
        "abs_x0": src_x0, "abs_y0": src_y0,
        "pack_x": 0, "pack_y": 0,     # 执行器按输入实际 pack 填充
        "pack_w": src_meta.canvas_w, "pack_h": src_meta.canvas_h,
        "src_w": src_meta.canvas_w,
        "src_h": src_meta.canvas_h,
        "outside": OUTSIDE_POLICIES.index(outside)
        if outside in OUTSIDE_POLICIES else 1,
    }


def fill_crop_pack(params: dict, pack: Rect, region: Rect) -> None:
    params["abs_x0"] += region[0]
    params["abs_y0"] += region[1]
    params["pack_x"], params["pack_y"] = pack[0], pack[1]
    params["pack_w"] = pack[2] - pack[0]
    params["pack_h"] = pack[3] - pack[1]


def mask_params(node: PlannedNode, has_input: bool,
                consumed_port: str) -> dict:
    p = node.params
    meta = node.meta
    assert meta is not None
    # GL mask 空间是 bottom-up uv；参数换算到 top-down（1 - y）。
    if consumed_port == "matte":
        output_mode = 0
    elif has_input:
        output_mode = 2
    else:
        output_mode = 1
    return {
        "has_input": 1 if has_input else 0,
        "canvas_w": meta.canvas_w,
        "canvas_h": meta.canvas_h,
        "center_x": float(p.get("center_x", 0.5)),
        "center_y": 1.0 - float(p.get("center_y", 0.5)),
        "mask_size_x": float(p.get("width", 0.5)),
        "mask_size_y": float(p.get("height", 0.5)),
        "rotation_deg": float(p.get("rotation", 0.0)),
        "feather": float(p.get("feather", 0.0)),
        "invert": 1 if bool(p.get("invert", False)) else 0,
        "output_mode": output_mode,
    }


def output_params(node: PlannedNode, src_meta: Optional[FrameMeta]) -> dict:
    p = node.params
    alpha_policy = str(p.get("alpha_policy", "keep"))
    bg = _parse_output_bg(str(p.get("background_color", "0,0,0,0")))
    encode_srgb = 0
    output_cs = str(p.get("output_color_space", "same"))
    if output_cs == "srgb":
        encode_srgb = 1
    rect = src_meta.data_rect if src_meta is not None else (0, 0, 0, 0)
    return {
        "in_rect_x": rect[0], "in_rect_y": rect[1],
        "in_w": rect[2] - rect[0], "in_h": rect[3] - rect[1],
        "pack_x": 0, "pack_y": 0,     # 执行器按输入实际 pack 填充
        "pack_w": 1, "pack_h": 1,
        "alpha_mode": _OUTPUT_ALPHA_MODES.get(alpha_policy, 0),
        "input_premult": 1 if (src_meta is not None
                               and src_meta.alpha_premultiplied) else 0,
        "bg_r": bg[0], "bg_g": bg[1], "bg_b": bg[2], "bg_a": bg[3],
        "encode_srgb": encode_srgb,
    }


def fill_output_pack(params: dict, pack: Rect) -> None:
    params["pack_x"], params["pack_y"] = pack[0], pack[1]
    params["pack_w"] = pack[2] - pack[0]
    params["pack_h"] = pack[3] - pack[1]
