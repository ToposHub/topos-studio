# 批次报告：batch-106 [P1] src/features/composite/ui//

编号区间：AUD-10600–AUD-10699
审查文件：2 个（1,675 行）
交叉验证参考：`src/features/composite/core/node_engine.py`（NodeGraph API 行为）、`src/features/composite/ui/node_editor_widget.py`（调用方）

---

## src/features/composite/ui/node_graph_commands.py (行数: 843)

### 发现

- [AUD-10601] [Critical] DeleteNodeCommand.undo 在锁外直接改写 graph.connections，数据竞争且绕过 revision/dirty 标记
  - 位置: src/features/composite/ui/node_graph_commands.py:158-169
  - 证据:
    ```python
    for link in self._reconnected_links:
        try:
            # Find and remove the reconnection link
            for conn in list(self.graph.connections):
                if (conn.src_node_id == link['from_node'] and
                    conn.src_port_name == link['from_port'] and
                    conn.dst_node_id == link['to_node'] and
                    conn.dst_port_name == link['to_port']):
                    self.graph.connections.remove(conn)
                    break
    ```
  - 影响: NodeGraph 以 `self._lock = threading.RLock()`（node_engine.py:446）保护全部读写（56 处 with self._lock），评估/环检测在持锁线程遍历 `self.connections`（node_engine.py:2346、2363）。此处从 UI 线程无锁原地 `list.remove`：(1) 渲染/评估线程恰在遍历时触发 `RuntimeError: list changed size during iteration` 崩溃；(2) 绕过 `disconnect()` 内的 `_topology_revision += 1`、`_generation += 1`、`_mark_dirty_downstream`（node_engine.py:1218-1233），undo 后缓存/观察者仍认为拓扑未变 → 渲染结果错误；(3) 引擎各方法以"整体替换列表"方式更新（`self.connections = [...]`），若并发替换发生在快照与 remove 之间，remove 会落在旧列表上，undo 静默失效。
  - 建议: 逐条改用 `self.graph.disconnect(from_node, from_port, to_node, to_port)`（持锁、返回 bool、正确维护 revision）。
  - 置信度: 高

- [AUD-10602] [Major] 重连链路预测与引擎实现判定口径不一致（端口数 vs 连接数），undo 可泄漏自动重连链路
  - 位置: src/features/composite/ui/node_graph_commands.py:127-141
  - 证据:
    ```python
    # Find image input/output ports
    image_inputs = [p for p in node.inputs if p.data_type == "image"]
    image_outputs = [p for p in node.outputs if p.data_type == "image"]

    # Only auto-reconnect for single image input with one or more outputs
    if len(image_inputs) != 1 or len(image_outputs) < 1:
        return []
    ```
  - 影响: 引擎 `remove_node_and_reconnect` 按"连接数"判定（node_engine.py:547-558，`image_inputs = [c for c in incoming if ...]`，仅要求恰 1 条图像入连接），命令层却按"端口数"判定。对拥有 2 个图像输入端口但只连了一路的节点（如 Blend/合成类 A/B 输入），引擎会真实创建 upstream→downstream 重连，而命令预测结果为空 → undo 恢复节点与原连线后，泄漏的重连链路仍留在图中（下游为多输入端口时拓扑错误、渲染路径改变）。另引擎对每条新链有二次校验（node_engine.py:569-575），预测与实际本就可能漂移。
  - 建议: 不做二次预测：redo 前 snapshot `graph.connections`，删除后 diff 得出实际新建链路记入 `_reconnected_links`；或让 `remove_node_and_reconnect` 返回新建的连接列表。
  - 置信度: 中（两处实现的口径差异为事实；触发需多图像输入端口且恰一路已连接）

- [AUD-10603] [Major] SetParameterCommand 以 None 兼作"未捕获"哨兵，旧值为 None 的参数 undo 无效
  - 位置: src/features/composite/ui/node_graph_commands.py:397-409
  - 证据:
    ```python
    def redo(self):
        """Set the new parameter value."""
        node = self.graph.get_node(self.node_id)
        if node and self._old_value is None:
            self._old_value = node.parameters.get(self.param_name)
        if node:
            node.set_parameter(self.param_name, self.new_value)

    def undo(self):
        """Restore the old parameter value."""
        node = self.graph.get_node(self.node_id)
        if node and self._old_value is not None:
            node.set_parameter(self.param_name, self._old_value)
    ```
  - 影响: 参数此前未设置（旧值为 None）时，undo 的 `is not None` 守卫使其成为无操作——设参后无法撤销回"未设置"状态，undo 栈出现静默失效步骤；同文件 RenameNodeCommand 已用 `_captured` 标志的正确范式，此处未对齐。
  - 建议: 增加 `self._captured` 布尔标志区分"未捕获"与"捕获到 None"（照搬 RenameNodeCommand 424-448 的写法）。
  - 置信度: 高

- [AUD-10604] [Major] CopyNodesCommand 节点缺失时索引映射错位，粘贴时静默丢失内部连接
  - 位置: src/features/composite/ui/node_graph_commands.py:477-497
  - 证据:
    ```python
    for i, node_id in enumerate(self.node_ids):
        node = self.graph.get_node(node_id)
        if node:
            nodes_data.append({
                'id': node_id,
                ...
            })
            id_to_idx[node_id] = i
    ```
  - 影响: `i` 是 node_ids 的下标，但缺失节点被 `if node:` 跳过后未写入 `nodes_data`，`nodes_data` 实际下标与 `i` 错位；剪贴板契约是"索引指向 nodes 列表"（类 docstring 459-461），PasteNodesCommand.redo 以 `enumerate(nodes_data)` 重建 `idx_to_new_id`（549-566），错位索引解析为 None 或错误节点 → 本应保留的所选节点间连接在粘贴后丢失或接错。
  - 建议: 只对成功收集的节点累加计数器建索引（`idx = len(nodes_data)` 后再 append），保证 `id_to_idx` 与 `nodes_data` 下标一致；或前置过滤 `node_ids = [nid for nid in node_ids if self.graph.get_node(nid)]`。
  - 置信度: 高（逻辑确定；触发需 node_ids 含已失效 id，如陈旧的多选集合）

- [AUD-10605] [Major] PasteNodesCommand.redo 对剪贴板数据零验证，且绕过 set_parameter 直接注入参数
  - 位置: src/features/composite/ui/node_graph_commands.py:545-566
  - 证据:
    ```python
    nodes_data = self.clipboard_data.get('nodes', [])
    connections_data = self.clipboard_data.get('connections', [])

    # First pass: create nodes
    for i, data in enumerate(nodes_data):
        new_id = str(uuid.uuid4())[:8]
        node_class = NODE_REGISTRY.get(data['type'])
        if not node_class:
            continue
        node = node_class(node_id=new_id)
        node.parameters = dict(data['parameters'])
        node.position = [
            data['position'][0] + self.offset[0],
    ```
  - 影响: `data['type']`/`data['parameters']`/`data['position']` 直接下标访问，字段缺失或 position 不是二元序列时抛 KeyError/IndexError 使整个粘贴 redo 中断（已建节点残留在图中且不在 undo 语义内恢复）；`clipboard_data: Dict[str, Any]` 属外部形状数据，违反"外部数据须验证"铁律。`node.parameters = dict(data['parameters'])` 整体覆盖绕过节点自身的 `set_parameter` 校验/联动（脏标记、表达式绑定），陈旧或异型参数可直达求值层。
  - 建议: 逐字段 `.get()` + 类型/长度校验后再使用；参数恢复逐个走 `node.set_parameter(k, v)`（与 AddNodeCommand.redo 59-60 一致）。
  - 置信度: 高

- [AUD-10606] [Suggestion] 多处 undo 以宽泛 `except Exception: pass` 吞错（引擎 API 只返回 bool 不抛异常）
  - 位置: src/features/composite/ui/node_graph_commands.py:265-276（ConnectCommand.undo）、302-313（DisconnectCommand.undo）、178-186（DeleteNodeCommand.undo 恢复连线）
  - 证据:
    ```python
    def undo(self):
        """Remove the connection."""
        if self._connected:
            try:
                self.graph.disconnect(
                    self.from_node_id,
                    self.from_port,
                    self.to_node_id,
                    self.to_port
                )
            except Exception:
                pass
    ```
  - 影响: NodeGraph.connect/disconnect 全部失败路径返回 False、从不抛异常（node_engine.py:1163-1233），这些 except 捕获的只会是 AttributeError 等编程错误——被静默吞掉违反"不得宽泛 except 掩盖领域错误"铁律，且 undo 失败（返回 False）无任何记录，undo 栈与图状态可能悄然脱节。
  - 建议: 移除 try/except，改为检查返回值；返回 False 时记录 warning 日志。
  - 置信度: 高

- [AUD-10607] [Suggestion] ReconnectCommand.undo 忽略 disconnect/connect 返回值，旧连接恢复失败静默
  - 位置: src/features/composite/ui/node_graph_commands.py:358-377
  - 证据:
    ```python
    # Remove new connection
    self.graph.disconnect(
        self.new_from_node_id,
        self.new_from_port,
        self.to_node_id,
        self.to_port
    )

    # Restore old connection
    self.graph.connect(
        self.old_from_node_id,
        self.old_from_port,
        self.to_node_id,
        self.to_port
    )
    ```
  - 影响: 与同文件其他命令不同，此处无任何检查：若 undo 时 connect 校验不过（图状态已被栈外操作改变）返回 False，端口悬空且无提示——用户以为已撤销，实际旧连线丢失。disconnect 返回 False（新连线已不在）同样无感知。
  - 建议: 检查两个返回值，恢复失败时记录日志；必要时在 undo 抛出明确异常让 undo 框架感知。
  - 置信度: 高

- [AUD-10608] [Suggestion] ModifyBackdropCommand.undo 只恢复 old_data 中存在的键，新增键残留
  - 位置: src/features/composite/ui/node_graph_commands.py:837-843
  - 证据:
    ```python
    def undo(self):
        """Restore old properties."""
        backdrop_data = self.graph.backdrops.get(self.backdrop_id)
        if backdrop_data:
            for key, value in self.old_data.items():
                backdrop_data[key] = value
            self.graph.set_backdrop(self.backdrop_id, backdrop_data)
    ```
  - 影响: 撤销是"按 old_data 键回写"而非恢复快照：new_data 引入而 old_data 未包含的键（如首次为背景添加 note）在 undo 后残留，回不到修改前状态。
  - 建议: redo 前记录被修改键的完整旧值映射，undo 时对 new_data 有而 old_data 无的键执行删除，或直接快照整个 backdrop dict 回写。
  - 置信度: 中（取决于调用方构造 old_data 的方式，命令契约本身存在缺口）

- [AUD-10609] [Suggestion] 新节点 ID 取 uuid4 前 8 位且不查重，与 add_node 静默替换语义叠加有理论数据丢失
  - 位置: src/features/composite/ui/node_graph_commands.py:550（PasteNodesCommand）、630（DuplicateNodesCommand）
  - 证据:
    ```python
    new_id = str(uuid.uuid4())[:8]
    ```
  - 影响: `NodeGraph.add_node` 对重复 ID 直接 `self.nodes[node.id] = node` 静默替换（node_engine.py:464），命令层生成 8 位十六进制 ID 后不做唯一性检查；一旦碰撞（批量粘贴大量节点时概率非零），已有节点被新节点无提示覆盖（数据丢失）且 undo 语义错乱。
  - 建议: 生成后循环检查 `graph.get_node(new_id)` / 已生成本批 ID 集合，碰撞则重生成。
  - 置信度: 高（机制确定；单次碰撞概率约 2^-32，属低概率高后果）

- [AUD-10610] [Suggestion] Backdrop 命令在锁外原地改写引擎内部 backdrops 字典
  - 位置: src/features/composite/ui/node_graph_commands.py:777-780（MoveBackdropCommand.redo；ResizeBackdropCommand 802-807、ModifyBackdropCommand 829-835 同型）
  - 证据:
    ```python
    backdrop_data = self.graph.backdrops.get(self.backdrop_id)
    if backdrop_data:
        backdrop_data['position'] = list(self.new_position)
        self.graph.set_backdrop(self.backdrop_id, backdrop_data)
    ```
  - 影响: `set_backdrop` 持 RLock 并 deepcopy 入参（node_engine.py:957-962），但 `graph.backdrops.get()` 返回内部字典引用，先原地改键再提交的写法在锁外修改了共享结构，与引擎锁纪律不一致（backdrops 为纯 UI 元数据、无渲染影响，风险低）。
  - 建议: 取值后先 `dict(backdrop_data)` 拷贝再改写提交，或为引擎补充 `update_backdrop` 类原子 API。
  - 置信度: 高（代码事实；实际影响低）

- [AUD-10611] [Suggestion] 类型注解缺失：redo/undo/__post_init__ 等方法无返回类型注解
  - 位置: src/features/composite/ui/node_graph_commands.py:43-47、67-70、89-92、155-157、212-214、255-256、293-294、340-342、397-405、471-472、537-539、618-619、701-706 等
  - 证据:
    ```python
    def __post_init__(self):
        self._node = None
        self.description = f"Add {self.node_type}"

    def redo(self):
        """Add the node."""
    ```
  - 影响: 项目要求强类型；`_calculate_reconnection_links`、`get_clipboard_data` 等已带注解，同文件风格不一致，影响静态检查覆盖与可读性。
  - 建议: 统一补 `-> None` / `-> bool` / `-> Optional[...]` 等返回注解。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：不涉及 OpenGL 资源（纯数据层命令；引擎明确 remove_node 不做 GPU 清理、责任在渲染线程调度，本层未越界触碰）
- GLSL/HDR 正确性：不涉及着色器与像素数据；参数值透传未做无理由 clamp，符合 HDR 铁律
- 信号防递归：无 Qt 信号；MoveNodeCommand 的 ui_update_callback 由调用方持有，命令层无自触发回路
- RenameNodeCommand 状态捕获范式正确（_captured 标志 + 标签归一化 _normalize，429-448）
- AddNodeCommand 对未知节点类型快速失败（raise ValueError，54），符合外部输入验证原则
- DeleteBackdropCommand/DeleteBackdropsCommand 快照-恢复配对完整，set_backdrop 深拷贝杜绝共享引用（957-962）
- DeleteNodeCommand.redo 每次重置 `_deleted_data`/`_reconnected_links` 后按当前图状态重建，重复 redo 幂等基础成立（91-92）

---

## src/features/composite/ui/node_item.py (行数: 832)

### 发现

- [AUD-10620] [Major] boundingRect 未为常规节点的选中/抖动光晕扩展，绘制超出边界致局部重绘残影
  - 位置: src/features/composite/ui/node_item.py:446-452（对照 paint 512-527、540-554）
  - 证据:
    ```python
    def boundingRect(self) -> QRectF:
        """Expand bounding rect to cover selection glow drawn outside base rect."""
        rect = super().boundingRect()
        if self._is_dot:
            # Selection glow: radius r+3=10, pen width 4 → extends ~5px beyond rect
            return rect.adjusted(-5, -5, 5, 5)
        return rect
    ```
  - 影响: docstring 声称"扩展以覆盖选中光晕"，但仅 dot 分支扩展。常规节点 paint 里 Layer1 光晕取 `rect.adjusted(-4, -4, 4, 4)` 且 `QPen(..., 8)`（512-519），笔宽一半再外扩 4px，合计约 8px 超出 boundingRect；抖动光晕（540-554，-4 偏移 + 笔宽 4）同样越界。场景按 boundingRect 组织重绘区域，越界部分在局部重绘时被裁掉 → 选中/拖动时光晕边沿残影、闪烁。文件自身注释（312-313）已记录过同类"residual yellow glow"问题，说明该失效模式真实存在。
  - 建议: 非常规节点返回 `rect.adjusted(-8, -8, 8, 8)`（覆盖 4px 偏移 + 4px 半笔宽）；或在引入光晕时同步扩展常量并加注释说明推导。
  - 置信度: 高

- [AUD-10621] [Major] paint 每帧对缩略图执行 SmoothTransformation 缩放，无缓存
  - 位置: src/features/composite/ui/node_item.py:729-744
  - 证据:
    ```python
    if self._show_thumbnail and self._thumbnail:
        avail_w = self.NODE_WIDTH - self.THUMBNAIL_MARGIN * 2
        thumb_h = self._calc_thumbnail_display_height()
        ...
        # Scale pixmap to fill the area, cropping overflow
        scaled = self._thumbnail.scaled(
            avail_w, thumb_h,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation
        )
    ```
  - 影响: 缩放结果只依赖 (源 pixmap, 显示尺寸)，二者仅在 set_thumbnail/_update_layout 时变化，却在每次 paint 重复计算。预览策略开启后，拖动节点、进度条刷新、状态切换都会高频触发整节点重绘——多节点画布上平滑缩放为纯重复 CPU 开销，与 4K 实时性能基准相悖（LOD 简化绘制 488-495 已覆盖细节，但缩略图路径未受益）。
  - 建议: 在 set_thumbnail / set_thumbnail_visible / _update_layout 时缓存 `self._scaled_thumbnail`，paint 直接 drawPixmap；源或目标尺寸变化时才重算。
  - 置信度: 高

- [AUD-10622] [Suggestion] set_title 访问 dot 节点从未初始化的 _title_item，潜在 AttributeError
  - 位置: src/features/composite/ui/node_item.py:174-186（_title_item 仅在 _create_title 159-172 赋值，且 141-143 仅非 dot 调用）
  - 证据:
    ```python
    def set_title(self, new_title: str) -> None:
        """Update the on-canvas title dynamically (命名升级 P1).
        ...
        self.node_title = new_title
        if self._title_item is not None:
            self._title_item.setPlainText(new_title)
    ```
  - 影响: dot 节点走 `if not self._is_dot:` 分支跳过 `_create_title`，`_title_item` 属性不存在；一旦对 dot 节点调用 set_title 即 AttributeError 崩溃。调用方 `NodeEditorWidget.set_node_user_label`（node_editor_widget.py:4828-4844）按 `_node_items` 查找后直接 `item.set_title(...)`，无 dot 过滤；当前常规改名入口被 `begin_inline_rename` 的 `_is_dot` 短路守卫（202-204）挡住，但 4843 行"节点已不存在仍刷新 item 标题"的路径未防护，属潜伏崩溃点。
  - 建议: `__init__` 统一初始化 `self._title_item = None`（dot 分支保留为 None）；set_title 增加 `_is_dot` 早退。
  - 置信度: 高（属性缺失为代码事实；实际触发路径当前概率低）

- [AUD-10623] [Suggestion] clear_thumbnail 未调用 _update_layout，清除后保留缩略图占位高度
  - 位置: src/features/composite/ui/node_item.py:829-832（对照 set_thumbnail 775-784）
  - 证据:
    ```python
    def clear_thumbnail(self):
        """Clear the thumbnail preview."""
        self._thumbnail = None
        self.update()
    ```
  - 影响: set_thumbnail 置图后会 `_update_layout()` 重算高度（308-310 为缩略图预留 `thumb_h + THUMBNAIL_MARGIN * 2`），clear_thumbnail 只置 None + update，矩形高度不变，节点底部留下空白占位，直到下一次选择/状态变化触发布局。当前全库无调用方（潜伏缺陷）；对照 `set_thumbnail(None)` 路径行为正确。
  - 建议: clear_thumbnail 内同样调用 `self._update_layout()`。
  - 置信度: 高

- [AUD-10624] [Suggestion] 进度值只 clamp 上界，负值/非法输入产生负宽度进度条
  - 位置: src/features/composite/ui/node_item.py:416-419、650-652
  - 证据:
    ```python
    def set_progress(self, progress: float):
        """Update progress for processing state."""
        self._progress = progress
        self.update()
    ```
    ```python
    bar_w = rect.width() * min(self._progress, 1.0)
    ```
  - 影响: `min` 只挡上限；progress 为负（求值端异常值）时 `int(bar_w)` 为负，drawRect 以负宽度向左绘制，进度条画出节点左边界（视觉瑕疵）。进度是 UI 比率而非 HDR 数据，此处 clamp(0.0, 1.0) 不违反 HDR 铁律。
  - 建议: set_progress 入口或 paint 处 `max(0.0, min(1.0, progress))`。
  - 置信度: 中

- [AUD-10625] [Suggestion] _perf_error 文本从未展示给用户
  - 位置: src/features/composite/ui/node_item.py:421-434、703-705（对照 _refresh_tooltip 395-404）
  - 证据:
    ```python
    elif self._perf_error:
        badge_text = f"{self._perf_time_ms:.1f}ms"
        badge_color = QColor(255, 60, 60, 220)
    ```
  - 影响: 错误信息仅用于把计时徽章染红，错误字符串本身既不进徽章也不进 tooltip（_refresh_tooltip 只拼 _locked/_error_msg/_diagnostic_message），求值失败原因对用户完全不可见，违背字段存在意图。
  - 建议: 在 _refresh_tooltip 中追加 _perf_error（存在且非空时），或徽章悬停提示。
  - 置信度: 高

- [AUD-10626] [Suggestion] 类型注解缺失：Qt override 与绘制辅助方法无参数/返回注解
  - 位置: src/features/composite/ui/node_item.py:32-44（keyPressEvent/focusOutEvent）、454（_paint_dot）、479（paint）、759（mouseDoubleClickEvent）、786（itemChange）、344（set_disabled）、329-331（update_connections）等
  - 证据:
    ```python
    def paint(self, painter, option, widget=None):
        """Custom paint with rounded rect, header gradient, and state overlays."""

    def itemChange(self, change, value):
        """Update connections and layout when node state changes."""
    ```
  - 影响: 项目强类型要求下，Qt override（`paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: Optional[QWidget] = None) -> None` 等）与公开方法缺注解，静态检查盲区且与同文件已注解方法（set_locked、set_diagnostic 等）风格不一。
  - 建议: 统一补齐参数与返回类型注解。
  - 置信度: 高

### 通过项
- 线程安全：状态更新入口（set_state/set_progress/set_perf_badge）按注释约定由 UI 线程经信号调用（composite_page.py:1617、6172 调用点均在信号处理内），本文件无渲染线程直触 QGraphicsItem 的路径
- GPU 资源生命周期：不涉及 OpenGL；QPixmap/QImage 仅在主线程创建与消费
- GLSL/HDR 正确性：不涉及着色器；缩略图显示链路的 gamma/显示变换在 composite_page 生成侧处理（6045-6048），本层纯展示
- 信号防递归：begin_inline_rename 的 `_finish` 以 `_rename_proxy is None` 幂等守卫，正确防住 Enter(returnPressed) 与 focusOut 双触发；"文本未变视为取消"避免 undo 栈无操作洪泛（226-233）
- _update_layout 全路径先 prepareGeometryChange 再 setRect（288-315），注释明确防旧区域残影
- set_diagnostic 输入归一化（级别白名单 + strip，363-371）符合外部输入验证要求
- 性能设计良好处：LOD 简化绘制（436-444 提前去重）、dot 节点极简绘制路径、抖动光晕由编辑器侧 0.05 阈值去抖（node_editor_widget.py:3120-3121）
- boundingRect 的 dot 分支扩展量推导正确（半径 7 + 光晕 3 + 半笔宽 2 = 12 = 半径 + 5）
