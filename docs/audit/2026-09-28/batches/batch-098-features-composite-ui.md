# 批次 batch-098 审查报告 — src/features/composite/ui//

- 任务书: docs/audit/2026-09-28/assignments/batch-098.md
- 编号区间: AUD-9800–AUD-9899
- 文件: connection_item.py (1,004 行)、graph_editor_view.py (1,845 行)
- 审查维度: 线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解

跨文件事实核查（用于支撑发现，均为只读验证）:
- `port_item.py` 中 PortItem 只有 `connection_count: int`（line 53/79-100），**不存在 `_connections` 属性**。
- `node_item.py:97` 确有 `self.output_ports: Dict[str, PortItem] = {}`。
- `node_engine.py:1187-1189` 在 `connect()` 内做模型层环检测（返回 False 拒绝）。
- `node_editor_widget.py:699/703/707/841` 对 flow 动画 start/stop 成对调用。
- `graph_model.py:627` 序列化同样只写单一 `interpolation`（in/out 同值），view 提交格式与其一致，无丢数据。

---

## src/features/composite/ui/connection_item.py (行数: 1,004)

### 发现

- [AUD-9800] [Major] 环检测 DFS 依赖 PortItem 不存在的 `_connections` 属性，恒返回 False（环防护形同虚设）
  - 位置: src/features/composite/ui/connection_item.py:724-727
  - 证据:
    ```python
    if hasattr(current_node, 'output_ports'):
        for port_name, port in current_node.output_ports.items():
            # Find connections from this port
            if hasattr(port, '_connections'):
                for conn in port._connections:
    ```
    而 port_item.py 中 PortItem 仅维护 `self.connection_count = 0`（整数计数），全模块 grep 无任何 `self._connections` 赋值。
  - 影响: `_would_create_cycle` 的 DFS 永不展开邻居，恒返回 False；`_can_reconnect`（line 678-679）据此放行。用户拖拽重连成环时仅剩 `node_engine.connect()` 的模型层校验兜底（静默 return False），表现为"拖拽视觉合法但连接无解释地失效"，且 UI 层防御性校验完全失效。
  - 建议: 改为遍历场景中 ConnectionItem（如 `self.scene().items()` 过滤 ConnectionItem 并沿 dst_port/src_port 的 parentItem 图做 DFS），或经 NodeEditorWidget 的数据模型 `get_output_connections(node_id)` 查询，而非依赖已废弃的端口级 `_connections` 列表。
  - 置信度: 高

- [AUD-9801] [Suggestion] Alt+拖拽（disconnect 模式）下 `_would_create_cycle` 使用未同步的 `_reconnect_src` 判方向，环检测方向错误
  - 位置: src/features/composite/ui/connection_item.py:691（对照 652、749）
  - 证据:
    ```python
    # _can_reconnect / _perform_reconnection 均按模式取值:
    dragging_src = self._reconnect_src if self._is_reconnecting else self._disconnect_src
    # 而 _would_create_cycle 只看 _reconnect_src:
    if self._reconnect_src:
        potential_src_port = new_port
        potential_dst_port = self.dst_port
    ```
    mousePressEvent 的 Alt 分支（line 519-523）只更新 `_disconnect_src`，不更新 `_reconnect_src`。
  - 影响: 当前被 AUD-9800 掩盖（整段逻辑已死）；一旦按建议修复 9800，Alt 拖目标端时（`_disconnect_src=False`）环检测仍会按"新端口为源"的错误方向遍历。
  - 建议: `_would_create_cycle` 复用与 `_perform_reconnection` 相同的 `dragging_src` 推导。
  - 置信度: 高（逻辑缺陷确定，实际影响被 9800 掩盖）

- [AUD-9802] [Suggestion] `_remove_from_port`/`_add_to_port` 为静默 no-op 的死代码
  - 位置: src/features/composite/ui/connection_item.py:762-764、776-779
  - 证据:
    ```python
    if hasattr(port, '_connections'):
        if self in port._connections:
            port._connections.remove(self)
    ```
    PortItem 无 `_connections` 属性，`hasattr` 恒 False，两个方法整体不执行任何操作；注释自称"backward compatibility"但调用方（`_perform_reconnection`）已改为发信号。
  - 影响: 误导性死代码——未来调用者以为端口级连接簿记仍生效，实际为空操作。
  - 建议: 直接删除这两个方法，或改为操作 PortItem 现有的 `connection_count`。
  - 置信度: 高

- [AUD-9803] [Suggestion] 最近点搜索的贪心单侧递归在曲线回折时可漏掉真正最近点
  - 位置: src/features/composite/ui/connection_item.py:911-919
  - 证据:
    ```python
    # Recurse on the closer half
    if dist_start < dist_end:
        return self._find_nearest_on_cubic_bezier(
            p0, mid01, mid012, mid_point, target, depth - 1)
    else:
        return self._find_nearest_on_cubic_bezier(
            mid_point, mid123, mid23, p3, target, depth - 1)
    ```
  - 影响: 只按两端点距离选择递归半段；贝塞尔 S 曲线（控制点偏移 `max(dx*0.5, 60)` 足以造成回折）在"端点较远"的半段内可能含有更近的采样点，导致 Dot 节点插入吸附（`get_nearest_point_on_path`）返回偏移的位置/距离。
  - 建议: 递归两侧取更近者（深度 6 成本可承受），或至少在 dist_mid 接近阈值时双递归。
  - 置信度: 中

- [AUD-9804] [Suggestion] 多处参数/属性缺类型注解（Suggestion 级）
  - 位置: src/features/composite/ui/connection_item.py:39、51（`conn`）、156（`src_port`/`dst_port`/`data_type`）、635（`target_port`）、731（`target_port`）等
  - 证据:
    ```python
    def add(self, conn) -> None:
    def __init__(self, src_port=None, dst_port=None, data_type="image"):
    def _can_reconnect(self, target_port) -> bool:
    ```
  - 影响: 与 .clinerules 强类型要求不符；`conn`/端口参数无类型约束降低可读性与静态检查覆盖。
  - 建议: 统一补 `conn: "ConnectionItem"`、`src_port: Optional["PortItem"]` 等注解（可用 TYPE_CHECKING 前置引用避免循环导入）。
  - 置信度: 高

### 通过项
- 线程安全: 全部逻辑在 GUI 线程（QGraphicsItem 事件 + `_FlowDriver` 的 QTimer 挂接在 QObject 上），无跨线程共享可变状态
- GPU 资源生命周期: 纯 QPainter/QGraphicsItem 绘制，不涉及 OpenGL/ModernGL 资源
- GLSL·HDR 正确性: 不适用（无着色器、无像素数据处理）
- 信号防递归: 删除/重连/断开均通过 `_ConnectionSignals` 单向请求信号交由 NodeEditorWidget 处理（node_editor_widget.py:699 连接），本类不直接回写数据模型，无递归回路
- 状态提交一致性: 三个破坏性操作均走宿主 undo 路径（`_delete_connection`/`_perform_reconnection`/`_disconnect_and_remove` 只 emit 不自改）
- 异常与取消路径: `_FlowDriver._advance` 捕获 RuntimeError 清理已删除 C++ 包装的陈旧连接并在列表清空后停表；flow 动画生命周期由 node_editor_widget.py:703/707/841 成对 start/stop
- 输入验证: 重连前校验类型匹配、方向（output→input）、同节点自环；`_update_tooltip` 对缺属性节点有 hasattr 回退
- 性能模式: LOD 简化绘制（`_simplified_draw`）、QTimer 空闲自动停止、吸附采样深度限制 6（64 采样）、笔宽 cosmetic 不随缩放变粗

---

## src/features/composite/ui/graph_editor_view.py (行数: 1,845)

### 发现

- [AUD-9805] [Suggestion] 表达式求值器懒加载使用裸 `except Exception` 掩盖真实错误（违反"不得宽泛 except"铁律）
  - 位置: src/features/composite/ui/graph_editor_view.py:342-349、355-363
  - 证据:
    ```python
    try:
        from src.features.composite.utils.expression_parser import \
            ExpressionContext
        ctx = ExpressionContext(fps=self._fps)
        self._expr_ctx = ctx
        return ctx
    except Exception:
        return None
    ```
  - 影响: 除 ImportError 外，`ExpressionContext(...)` 构造中的任何缺陷（TypeError 等）都被静默吞掉并返回 None → 表达式前/后曲线悄然不绘制且无任何日志，问题难以定位。docstring 只声明"Returns None if the evaluator can't be imported"，与实际捕获范围不符。
  - 建议: 收窄为 `except ImportError`（或 ModuleNotFoundError），构造异常让其抛出/记录日志。
  - 置信度: 高（规则违反确凿；实际触发概率低）

- [AUD-9806] [Suggestion] `resizeEvent` 无条件重置视口，忽略 `_user_adjusted`，与本文件其余路径的用户缩放保留语义矛盾
  - 位置: src/features/composite/ui/graph_editor_view.py:407-410（对照 169-170、233-238）
  - 证据:
    ```python
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Re-fit on resize so the graph fills the new geometry.
        self._refit_viewport()
    ```
    而 `set_series` 在 `_user_adjusted=True` 时保留用户视口仅重适配值轴，`set_frame_range` 同样先检查 `_user_adjusted`。
  - 影响: 用户缩放/平移（`_user_adjusted=True`）后，任何窗口或 splitter 尺寸变化都会把时间轴/值轴重置为自动适配，丢弃用户的 zoom/pan 状态，与 plan §22.4 auto_zoom 语义不一致。
  - 建议: resize 时按 `_user_adjusted` 分支——已手动调整时仅按比例重算 `pixels_per_frame/pixels_per_value` 以适配新尺寸，保持视口数据坐标不变。
  - 置信度: 中（可能是有意的"填满几何"设计，但与同文件语义冲突明显）

- [AUD-9807] [Suggestion] `_abort_active_drag` 恢复快照后不重排 `series.points`，与 `_cancel_drag` 不一致，可致曲线暂时绘制异常
  - 位置: src/features/composite/ui/graph_editor_view.py:640-652（对照 `_cancel_drag` 684-689、696-705）
  - 证据:
    ```python
    if self._drag_mode == "keyframe":
        for _s, kp, of, ov in self._drag_snapshot:
            kp.frame, kp.value = float(of), float(ov)
    ```
    `_cancel_drag` 在恢复后额外执行 `series.points.sort(key=lambda p: p.frame)`；`_abort_active_drag` 没有。拖动期间 `_do_keyframe_drag` 已多次 sort 改变列表顺序，恢复帧值后列表顺序可能与帧序不一致。
  - 影响: `graph_model.sample_value_curve` 对 `k0.frame >= k1.frame` 的段跳过（graph_model.py:394），乱序点列会导致部分曲线段缺失，直到下次提交/取消才恢复。
  - 建议: 在 `_abort_active_drag` 的 keyframe/box 分支恢复后同样对涉及的 series 排序。
  - 置信度: 高（不一致确凿；视觉影响自愈性较强）

- [AUD-9808] [Suggestion] 拖动吸附为每点每 move 全量重建候选列表，O(选中点 × 总关键帧)
  - 位置: src/features/composite/ui/graph_editor_view.py:839-845（调用点）与 1056-1078（候选构建）
  - 证据:
    ```python
    for series, kp, orig_frame, orig_value in self._drag_snapshot:
        target = orig_frame + df
        new_frame = self._snap_frame(target) if snap_on else \
            int(round(target))
    ```
    `_snap_frame` 每次调用都遍历 `self._markers` 及所有 series 的全部关键帧构建 candidates。
  - 影响: 开启吸附时多选大量关键帧拖动，每个 mouse-move 事件做 选中数×总关键帧 次比较；4K 实时基准下属可避免的每帧开销（当前典型规模影响中低）。
  - 建议: 在 `_begin_keyframe_drag` 时缓存一次候选帧集合（play_range + markers + 拖动前关键帧帧），move 中只做查表。
  - 置信度: 高（模式确凿；量级影响中低）

- [AUD-9809] [Suggestion] 部分方法缺返回类型注解（Suggestion 级）
  - 位置: src/features/composite/ui/graph_editor_view.py:728（`_hit_keyframe`）、747（`_hit_handle`）、885（`_series_for` 有注解，728/747/1254-1266 的 `_grip_cursor` 返回未注解）
  - 证据:
    ```python
    def _hit_keyframe(self, sp: QPointF):
    def _hit_handle(self, sp: QPointF):
    @staticmethod
    def _grip_cursor(grip: str):
    ```
  - 影响: 与文件整体强类型风格不一致（同文件多数方法已有 `-> None`/`-> Optional[...]`）。
  - 建议: 补 `-> Optional[Tuple[GraphSeries, GraphPoint]]`、`-> Qt.CursorShape` 等返回注解。
  - 置信度: 高

### 通过项
- 线程安全: 纯 QWidget 事件驱动，无后台线程交互
- GPU 资源生命周期: QPainter 画布，不涉及 OpenGL 资源
- GLSL·HDR 正确性: 不适用（无着色器/像素数据；曲线值域无不当 clamp，value 仅按用户拖动改变）
- 信号防递归: `selection_changed`/`keyframe_changed` 均为单向通知，本类不监听自身信号；set_series/set_graph_type 入口先 `_abort_active_drag` 防重建中拖拽状态串写（F2/F3 修复到位）
- 状态提交一致性: `_commit_drag` 以 `drag_about_to_commit`/`drag_committed` 包裹单次 undo 组且用 try/finally 保证配对（Bug-2 修复到位）；F4 无实际移动不 emit 不入 undo；Esc 取消经 `_cancel_drag` 完整还原帧/值/双手柄/锚点且不写节点；提交的 kfs 字典格式与 graph_model 解析端一致（单一 interpolation + bezier_in/out，graph_model.py:627/881-896 互证）
- 异常与取消路径: keyPressEvent Esc 覆盖全部五种 drag 模式；A5 修复防中/右键劫持进行中的左键拖拽；`_drag_actually_changed` 在清态前判定（顺序正确）
- 输入验证: `set_fps` 下限 1.0 且失效表达式上下文缓存（D1 修复）；`set_frame_range`/`set_current_frame` int 化 + 去重；`set_markers` 对 dict/对象双形态规范化；`_do_keyframe_drag` 帧钳制 ≥0；`apply_scale_transform` 端钳制 ≥0（graph_model.py:738-741 互证）；`_nice_step`/`_floor_to` 对非正输入有防御
- 性能模式: 采样随 ppf 密度自适应；`_rebuild_samples` 仅在视口/选项变化时执行；无逐 paint 重采样
- 类型注解: 除上述 9809 外整体覆盖良好（含 `from __future__ import annotations` 与 Optional/List 标注）

---

## 汇总

| 文件 | Critical | Major | Suggestion |
|------|----------|-------|------------|
| connection_item.py | 0 | 1 (AUD-9800) | 4 (AUD-9801..9804) |
| graph_editor_view.py | 0 | 0 | 5 (AUD-9805..9809) |
| 合计 | 0 | 1 | 9 |
