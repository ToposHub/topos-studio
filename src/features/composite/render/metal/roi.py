"""阶段 5.3 · 有限半径效果的 tile halo 与 ROI 推导（纯函数，无 GPU）。

实现迁移计划「参考实现 5A」中的 :func:`input_roi`：给定输出 tile 与效果
半径，推导需要向上游请求的扩张区域（半开区间矩形），以及渲染完成后把结果
裁回 tile 中心所用的裁剪矩形。

坐标系约定（与计划的示例一致）：

- ``Rect = (x0, y0, x1, y1)``，半开区间：``[x0, x1) × [y0, y1)``。
- 原点在画布左上角（图像行序，top-left），与 Metal 纹理行序一致；复合
  图内部元数据（BBox）仍是 GL 的 Y-up 约定，由调用方在边界换算。

halo 推导：:func:`node_halo_pixels` 按节点类型/参数给出「该节点为产出
任意输出像素需要额外读取的邻域半径」（卷积类/阴影类效果），再由
:func:`propagate_input_halos` 沿图反向递归累加，得到每个节点输出侧需要
向上游请求的总半径（多消费者取最大值）。Transform 反投影 ROI 由
:func:`transform_input_rect` 完成（含插值余量）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Rect = Tuple[int, int, int, int]

# 与 TransformNode._tile_padding 的取值对齐（linear/bilinear 1px、nearest 0）。
_FILTER_HALO_PIXELS = {"nearest": 0, "linear": 1}


def input_roi(tile: Rect, radius: int, width: int, height: int) -> Tuple[Rect, Rect]:
    """扩张 tile 以覆盖有限半径效果的采样邻域。

    Args:
        tile: 输出 tile（半开区间，图像 top-left 坐标）。
        radius: 效果采样半径（像素，>= 0）。
        width: 画布宽（扩张结果被夹在画布内）。
        height: 画布高。

    Returns:
        ``(expanded, crop)``：

        - ``expanded``：应交给上游渲染的区域（仍为半开区间，夹在画布内）。
        - ``crop``：渲染完成后写回输出 ``tile`` 时，在 ``expanded`` 内的
          裁剪矩形（半开区间，相对 ``expanded`` 原点）。

    Raises:
        ValueError: radius 为负，或 tile 越出 ``[0, width) × [0, height)``、
            或非半开区间（x1 <= x0 / y1 <= y0）。
    """
    x0, y0, x1, y1 = tile
    if radius < 0 or not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError("invalid tile bounds or radius")
    left = max(0, x0 - radius)
    top = max(0, y0 - radius)
    right = min(width, x1 + radius)
    bottom = min(height, y1 + radius)
    expanded = (left, top, right, bottom)
    crop = (x0 - left, y0 - top, x1 - left, y1 - top)
    return expanded, crop


# 计划中的两个断言用例（保持逐字语义；放到模块级常量供测试引用）。
PLAN_ASSERT_CASES: Tuple[Tuple[Rect, int, int, int, Tuple[Rect, Rect]], ...] = (
    (
        (16, 32, 32, 48), 2, 64, 64,
        ((14, 30, 34, 50), (2, 2, 18, 18)),
    ),
    (
        (0, 0, 16, 16), 2, 64, 64,
        ((0, 0, 18, 18), (0, 0, 16, 16)),
    ),
)


def iter_tile_rects(
    width: int, height: int, tile_w: int, tile_h: int
) -> Iterable[Rect]:
    """把 ``width × height`` 画布切成至多 ``tile_w × tile_h`` 的半开 tile。

    尾部 tile 取剩余部分（不越出画布），顺序为行优先（y 外层、x 内层），
    保证与逐 tile 渲染的写回顺序无关。
    """
    if width <= 0 or height <= 0:
        return
    tw = max(1, int(tile_w))
    th = max(1, int(tile_h))
    for y0 in range(0, height, th):
        for x0 in range(0, width, tw):
            yield (x0, y0, min(x0 + tw, width), min(y0 + th, height))


def clamp_rect(rect: Rect, width: int, height: int) -> Rect:
    """把半开矩形夹到 ``[0, width) × [0, height)``；空矩形返回 (0,0,0,0)。"""
    x0, y0, x1, y1 = rect
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(width, x1), min(height, y1)
    if x1 <= x0 or y1 <= y0:
        return (0, 0, 0, 0)
    return (x0, y0, x1, y1)


def rects_union(a: Rect, b: Rect) -> Rect:
    """两个半开矩形的并集外接矩形（与 BBox.union 对应的 top-down 版本）。"""
    if a[2] <= a[0] or a[3] <= a[1]:
        return b
    if b[2] <= b[0] or b[3] <= b[1]:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def rects_intersection(a: Rect, b: Rect) -> Rect:
    """两个半开矩形的交集（空交集返回 (0,0,0,0)）。"""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return (0, 0, 0, 0)
    return (x0, y0, x1, y1)


def expand_rect(rect: Rect, margin: int) -> Rect:
    """四边外扩 ``margin`` 像素（可为 0；不做画布夹取）。"""
    if margin == 0:
        return rect
    return (rect[0] - margin, rect[1] - margin, rect[2] + margin, rect[3] + margin)


# ── 节点 halo（卷积/阴影类采样半径） ─────────────────────────────────────


def node_halo_pixels(node_type: str, params: Optional[dict]) -> int:
    """该节点为产出任意输出像素需要额外读取的邻域半径（像素）。

    只覆盖有限半径效果（卷积/阴影类）。逐像素节点（merge/premult/crop/
    mask/constant）返回 0；全局统计类效果（直方图驱动）不适用本模型，
    由预检报告为未迁移而不是给出错误 halo。
    """
    parameters = params or {}
    if node_type == "transform":
        # 与 TransformNode._tile_padding 一致：nearest 0 / linear 1。
        filter_name = str(parameters.get("filter", "linear"))
        return _FILTER_HALO_PIXELS.get(filter_name, 1)
    if node_type in ("blur", "defocus", "erode_dilate", "glow", "sharpen",
                     "vectorblur", "zblur", "godrays", "grain", "light_wrap"):
        # 未迁移节点：按节点参数给出保守半径，供 halo 传播函数在
        # 混合图上仍能推导正确 ROI（实际执行会整体回退）。
        radius = parameters.get("radius", parameters.get("size", 0))
        try:
            return max(0, int(math.ceil(float(radius))))
        except (TypeError, ValueError):
            return 0
    if node_type == "layer_style":
        # 投影/外发光：模糊半径 + 偏移距离都计入 halo。
        try:
            blur = float(parameters.get("shadow_blur", parameters.get("blur", 0.0)))
            dist = float(parameters.get("shadow_distance",
                                        parameters.get("distance", 0.0)))
            return max(0, int(math.ceil(blur + abs(dist))))
        except (TypeError, ValueError):
            return 0
    return 0


@dataclass
class HaloPropagation:
    """沿图反向传播后的每节点输入侧 halo 半径。"""

    # 每个节点为了满足其全部下游消费者的需求，其「输入」需要额外读取的
    # 邻域半径：node_input_halo[n] = node_halo_pixels(n) + max(下游需求)。
    node_input_halo: Dict[str, int] = field(default_factory=dict)
    # 每个节点输出侧需要向上游请求的总半径（即其消费者们的输入 halo）。
    output_request_halo: Dict[str, int] = field(default_factory=dict)


def propagate_input_halos(
    nodes: Sequence[dict],
    connections: Sequence[dict],
    output_node_ids: Iterable[str],
) -> HaloPropagation:
    """从输出节点反向递归传播 halo 半径。

    Args:
        nodes: 序列化节点列表（含 ``id``/``type``/``parameters``）。
        connections: 序列化连接列表（``src_node_id``/``dst_node_id``）。
        output_node_ids: 终端输出节点 id 集合（halo 传播起点）。

    Notes:
        - 多消费者取最大值（与 RenderScheduler._propagate_roi 的 union 语义
          一致），不做二次外扩：每节点自身 halo 只在其被消费链上计一次。
        - 该函数是纯分析：不区分节点是否已迁移，混合图同样可推导。
    """
    node_types: Dict[str, str] = {}
    node_params: Dict[str, dict] = {}
    for nd in nodes:
        nid = str(nd.get("id", ""))
        node_types[nid] = str(nd.get("type", ""))
        params = nd.get("parameters")
        node_params[nid] = params if isinstance(params, dict) else {}

    # 反向邻接：消费者列表。
    consumers: Dict[str, List[str]] = {nid: [] for nid in node_types}
    in_degree: Dict[str, int] = {nid: 0 for nid in node_types}
    for conn in connections:
        src = str(conn.get("src_node_id", ""))
        dst = str(conn.get("dst_node_id", ""))
        if src in node_types and dst in node_types:
            consumers[src].append(dst)
            in_degree[dst] += 1

    # 拓扑序（Kahn），随后逆序遍历保证下游先于上游结算。
    queue = [nid for nid, deg in in_degree.items() if deg == 0]
    topo: List[str] = []
    while queue:
        nid = queue.pop()
        topo.append(nid)
        for consumer in consumers[nid]:
            in_degree[consumer] -= 1
            if in_degree[consumer] == 0:
                queue.append(consumer)

    result = HaloPropagation()
    for nid in topo:
        result.output_request_halo[nid] = 0
    for nid in reversed(topo):
        own = node_halo_pixels(node_types.get(nid, ""), node_params.get(nid))
        downstream_need = max(
            (result.node_input_halo.get(consumer, 0) for consumer in consumers[nid]),
            default=0,
        )
        result.node_input_halo[nid] = own + downstream_need
        # 该节点的输出需要满足自身 halo + 消费者的输入 halo。
        for consumer in consumers[nid]:
            result.output_request_halo[nid] = max(
                result.output_request_halo[nid],
                result.node_input_halo.get(consumer, 0),
            )

    # 输出节点自身的输入 halo 已在循环中结算（终端无消费者）。
    for output_id in output_node_ids:
        result.output_request_halo.setdefault(str(output_id), 0)
    return result


def transform_input_rect(
    output_rect: Rect,
    matrix: Tuple[float, float, float, float, float, float],
    canvas_width: int,
    canvas_height: int,
    extra_margin: int = 0,
) -> Rect:
    """把输出矩形通过（前向）仿射矩阵反投影为输入矩形。

    Args:
        output_rect: 输出半开矩形（top-left 图像坐标）。
        matrix: 前向仿射 ``(m00, m10, m01, m11, m02, m12)``（Y-down 像素
            空间，``in_abs = M(out_abs)`` 的前向方向；此处先求逆再用四角
            反投影）。
        canvas_width/canvas_height: 输入画布尺寸（结果夹取范围）。
        extra_margin: 插值余量（如 linear +1）。

    Returns:
        输入侧所需的半开矩形（夹在画布内；空则 (0,0,0,0)）。
    """
    m00, m10, m01, m11, m02, m12 = matrix
    det = m00 * m11 - m01 * m10
    if abs(det) < 1e-12:
        return (0, 0, 0, 0)
    i00, i01 = m11 / det, -m01 / det
    i10, i11 = -m10 / det, m00 / det
    i02 = -(i00 * m02 + i01 * m12)
    i12 = -(i10 * m02 + i11 * m12)

    x0, y0, x1, y1 = output_rect
    corners = ((float(x0), float(y0)), (float(x1), float(y0)),
               (float(x0), float(y1)), (float(x1), float(y1)))
    xs: List[float] = []
    ys: List[float] = []
    for (cx, cy) in corners:
        xs.append(i00 * cx + i01 * cy + i02)
        ys.append(i10 * cx + i11 * cy + i12)
    rect = (
        int(math.floor(min(xs))) - extra_margin,
        int(math.floor(min(ys))) - extra_margin,
        int(math.ceil(max(xs))) + extra_margin,
        int(math.ceil(max(ys))) + extra_margin,
    )
    return clamp_rect(rect, canvas_width, canvas_height)
