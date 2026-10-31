"""
节点图自动排布算法。

提供节点整理、对齐、分布等功能。
"""

from typing import List, Dict, Set, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from src.features.composite.core.node_engine import NodeGraph, Node


class GraphLayout:
    """节点图排布管理器。"""

    # 排布参数
    NODE_WIDTH = 180      # 节点宽度
    NODE_HEIGHT = 100     # 节点高度（估算）
    HORIZONTAL_GAP = 60   # 水平间距
    VERTICAL_GAP = 40     # 垂直间距

    def __init__(self, graph: "NodeGraph") -> None:
        """初始化排布管理器。

        Args:
            graph: NodeGraph 数据模型
        """
        self._graph = graph

    def auto_layout(self, nodes: List["Node"]) -> Dict[str, Tuple[float, float]]:
        """
        自动排布选中节点。

        算法：
        1. 对节点做拓扑排序（分层）
        2. 输入节点放左侧，输出节点放右侧
        3. 每层按 y 排列
        4. 返回新位置映射

        Args:
            nodes: 要排布的节点列表

        Returns:
            {node_id: (x, y)} 新位置映射
        """
        if not nodes:
            return {}

        # 拓扑排序分层
        layers = self._topological_sort(nodes)

        if not layers:
            # 无法拓扑排序，按当前 x 坐标分层
            layers = self._fallback_layering(nodes)

        # 计算新位置
        new_positions: Dict[str, Tuple[float, float]] = {}

        # 找到当前最左边的节点位置作为起始点
        start_x = min(
            (n.position[0] for n in nodes),
            default=0.0
        )

        # 找到当前最上边的节点位置作为起始点
        start_y = min(
            (n.position[1] for n in nodes),
            default=0.0
        )

        x = start_x
        for layer_idx, layer in enumerate(layers):
            # 每层节点按 y 坐标排序（保持上下顺序）
            layer.sort(key=lambda n: n.position[1])

            # 计算起始 y（居中对齐）
            y = start_y

            for node_idx, node in enumerate(layer):
                new_positions[node.id] = (x, y)
                y += self.NODE_HEIGHT + self.VERTICAL_GAP

            # 下一层的 x 位置
            x += self.NODE_WIDTH + self.HORIZONTAL_GAP

        return new_positions

    def align_left(self, nodes: List["Node"]) -> Dict[str, float]:
        """
        左对齐：所有节点 x 设为最小 x。

        Args:
            nodes: 要对齐的节点列表

        Returns:
            {node_id: new_x} 新 x 坐标映射
        """
        if not nodes:
            return {}

        min_x = min(n.position[0] for n in nodes)
        return {n.id: min_x for n in nodes}

    def align_right(self, nodes: List["Node"]) -> Dict[str, float]:
        """
        右对齐：所有节点 x 设为最大 x。

        Args:
            nodes: 要对齐的节点列表

        Returns:
            {node_id: new_x} 新 x 坐标映射
        """
        if not nodes:
            return {}

        max_x = max(n.position[0] for n in nodes)
        return {n.id: max_x for n in nodes}

    def align_top(self, nodes: List["Node"]) -> Dict[str, float]:
        """
        顶部对齐：所有节点 y 设为最小 y。

        Args:
            nodes: 要对齐的节点列表

        Returns:
            {node_id: new_y} 新 y 坐标映射
        """
        if not nodes:
            return {}

        min_y = min(n.position[1] for n in nodes)
        return {n.id: min_y for n in nodes}

    def align_bottom(self, nodes: List["Node"]) -> Dict[str, float]:
        """
        底部对齐：所有节点 y 设为最大 y。

        Args:
            nodes: 要对齐的节点列表

        Returns:
            {node_id: new_y} 新 y 坐标映射
        """
        if not nodes:
            return {}

        max_y = max(n.position[1] for n in nodes)
        return {n.id: max_y for n in nodes}

    def align_center_horizontal(self, nodes: List["Node"]) -> Dict[str, float]:
        """
        水平居中：所有节点 y 设为平均 y。

        Args:
            nodes: 要对齐的节点列表

        Returns:
            {node_id: new_y} 新 y 坐标映射
        """
        if not nodes:
            return {}

        avg_y = sum(n.position[1] for n in nodes) / len(nodes)
        return {n.id: avg_y for n in nodes}

    def align_center_vertical(self, nodes: List["Node"]) -> Dict[str, float]:
        """
        垂直居中：所有节点 x 设为平均 x。

        Args:
            nodes: 要对齐的节点列表

        Returns:
            {node_id: new_x} 新 x 坐标映射
        """
        if not nodes:
            return {}

        avg_x = sum(n.position[0] for n in nodes) / len(nodes)
        return {n.id: avg_x for n in nodes}

    def distribute_horizontal(self, nodes: List["Node"]) -> Dict[str, float]:
        """
        水平均匀分布。

        在当前最左和最右节点之间均匀分布。
        保持 y 坐标不变。

        Args:
            nodes: 要分布的节点列表

        Returns:
            {node_id: new_x} 新 x 坐标映射
        """
        if len(nodes) < 2:
            return {}

        # 按 x 排序
        sorted_nodes = sorted(nodes, key=lambda n: n.position[0])

        min_x = sorted_nodes[0].position[0]
        max_x = sorted_nodes[-1].position[0]

        # 计算均匀间距
        total_width = max_x - min_x
        gap = total_width / (len(nodes) - 1)

        new_positions: Dict[str, float] = {}
        for i, node in enumerate(sorted_nodes):
            new_positions[node.id] = min_x + i * gap

        return new_positions

    def distribute_vertical(self, nodes: List["Node"]) -> Dict[str, float]:
        """
        垂直均匀分布。

        在当前最上和最下节点之间均匀分布。
        保持 x 坐标不变。

        Args:
            nodes: 要分布的节点列表

        Returns:
            {node_id: new_y} 新 y 坐标映射
        """
        if len(nodes) < 2:
            return {}

        # 按 y 排序
        sorted_nodes = sorted(nodes, key=lambda n: n.position[1])

        min_y = sorted_nodes[0].position[1]
        max_y = sorted_nodes[-1].position[1]

        # 计算均匀间距
        total_height = max_y - min_y
        gap = total_height / (len(nodes) - 1)

        new_positions: Dict[str, float] = {}
        for i, node in enumerate(sorted_nodes):
            new_positions[node.id] = min_y + i * gap

        return new_positions

    def cleanup_overlaps(self, nodes: List["Node"]) -> Dict[str, Tuple[float, float]]:
        """
        清理重叠节点（微调位置避免重叠）。

        算法：从左到右扫描，遇到重叠则向下或向上偏移。

        Args:
            nodes: 要清理重叠的节点列表

        Returns:
            {node_id: (x, y)} 新位置映射
        """
        if not nodes:
            return {}

        # 按 x + y 排序（先处理左上方的节点）
        sorted_nodes = sorted(nodes, key=lambda n: (n.position[0], n.position[1]))

        new_positions: Dict[str, Tuple[float, float]] = {}

        # 记录已处理的节点区域
        occupied_regions: List[Tuple[float, float, float, float]] = []

        for node in sorted_nodes:
            x, y = node.position

            # 检查是否有重叠
            node_rect = (
                x, y,
                x + self.NODE_WIDTH,
                y + self.NODE_HEIGHT
            )

            # 尝试找到一个不重叠的位置
            attempts = 0
            while self._has_overlap(node_rect, occupied_regions):
                # 向下偏移
                y += self.NODE_HEIGHT + self.VERTICAL_GAP
                node_rect = (
                    x, y,
                    x + self.NODE_WIDTH,
                    y + self.NODE_HEIGHT
                )
                attempts += 1
                if attempts > 50:  # 防止无限循环
                    break

            new_positions[node.id] = (x, y)
            occupied_regions.append(node_rect)

        return new_positions

    def _topological_sort(self, nodes: List["Node"]) -> List[List["Node"]]:
        """
        拓扑排序，返回分层节点列表。

        算法：
        1. 找出所有输入节点（没有上游连接）
        2. 从输入节点开始 BFS
        3. 每个节点根据上游节点层数确定自己的层数
        4. 层数 = max(上游节点层数) + 1
        5. 同层节点按 y 坐标排序

        Args:
            nodes: 要排序的节点列表

        Returns:
            [[layer0_nodes], [layer1_nodes], ...] 分层结果
        """
        if not nodes:
            return []

        # 获取节点ID集合
        node_ids = {n.id for n in nodes}
        node_map = {n.id: n for n in nodes}

        # 计算每个节点在选中范围内的上游和下游
        upstream: Dict[str, Set[str]] = {nid: set() for nid in node_ids}
        downstream: Dict[str, Set[str]] = {nid: set() for nid in node_ids}

        for node in nodes:
            # 获取输入连接（上游）
            input_conns = self._graph.get_input_connections(node.id)
            for conn in input_conns:
                if conn.src_node_id in node_ids:
                    upstream[node.id].add(conn.src_node_id)
                    downstream[conn.src_node_id].add(node.id)

        # 找出输入节点（在选中范围内没有上游连接）
        input_nodes = [nid for nid in node_ids if len(upstream[nid]) == 0]

        if not input_nodes:
            # 所有节点都有上游，说明是循环或独立子图
            # 使用 fallback 方法
            return []

        # BFS 分层
        layers: List[List[str]] = []
        layer_map: Dict[str, int] = {}

        # 第一层是输入节点
        layers.append(input_nodes)
        for nid in input_nodes:
            layer_map[nid] = 0

        # BFS 扩展
        current_layer = 0
        while True:
            next_layer_nodes: List[str] = []

            for nid in layers[current_layer]:
                for ds_nid in downstream[nid]:
                    if ds_nid in layer_map:
                        continue  # 已分配层

                    # 检查所有上游是否已分配
                    all_upstream_assigned = all(
                        us_nid in layer_map for us_nid in upstream[ds_nid]
                    )

                    if all_upstream_assigned:
                        # 层数 = max(上游层数) + 1
                        max_upstream_layer = max(
                            layer_map[us_nid] for us_nid in upstream[ds_nid]
                        )
                        layer_map[ds_nid] = max_upstream_layer + 1
                        next_layer_nodes.append(ds_nid)

            if not next_layer_nodes:
                break

            # 合并同层节点（可能有多个上游指向同一节点）
            actual_next_layer: List[str] = []
            for nid in next_layer_nodes:
                if nid not in actual_next_layer:
                    actual_next_layer.append(nid)

            layers.append(actual_next_layer)
            current_layer += 1

        # 处理未分配的节点（可能是孤立节点）
        unassigned = [nid for nid in node_ids if nid not in layer_map]
        if unassigned:
            # 放在最后一层
            layers.append(unassigned)
            for nid in unassigned:
                layer_map[nid] = len(layers) - 1

        # 转换为 Node 对象
        result: List[List["Node"]] = []
        for layer in layers:
            layer_nodes = [node_map[nid] for nid in layer if nid in node_map]
            if layer_nodes:
                result.append(layer_nodes)

        return result

    def _fallback_layering(self, nodes: List["Node"]) -> List[List["Node"]]:
        """
        备用分层算法：按 x 坐标分组。

        当拓扑排序失败时使用。

        Args:
            nodes: 要分层的节点列表

        Returns:
            [[layer0_nodes], [layer1_nodes], ...] 分层结果
        """
        if not nodes:
            return []

        # 按 x 坐标排序
        sorted_nodes = sorted(nodes, key=lambda n: n.position[0])

        # 按 x 坐标相近程度分组
        layers: List[List["Node"]] = []
        current_layer: List["Node"] = []
        last_x = None

        for node in sorted_nodes:
            if last_x is None:
                current_layer.append(node)
            elif abs(node.position[0] - last_x) < self.NODE_WIDTH / 2:
                # 同一层
                current_layer.append(node)
            else:
                # 新层
                if current_layer:
                    layers.append(current_layer)
                current_layer = [node]

            last_x = node.position[0]

        if current_layer:
            layers.append(current_layer)

        return layers

    def _has_overlap(self, rect: Tuple[float, float, float, float],
                     occupied: List[Tuple[float, float, float, float]]) -> bool:
        """
        检查矩形是否与已占用区域重叠。

        Args:
            rect: (x1, y1, x2, y2) 要检查的矩形
            occupied: 已占用区域列表

        Returns:
            True 如果有重叠
        """
        x1, y1, x2, y2 = rect
        for ox1, oy1, ox2, oy2 in occupied:
            # 矩形相交检测
            if not (x2 <= ox1 or x1 >= ox2 or y2 <= oy1 or y1 >= oy2):
                return True
        return False
