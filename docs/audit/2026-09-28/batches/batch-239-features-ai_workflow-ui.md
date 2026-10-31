# batch-239 [P2] — src/features/ai_workflow/ui//

编号区间：AUD-23900–AUD-23999。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性 / 异常与取消 / 输入验证 / 性能 / 类型注解。

汇总：Critical 0 · Major 8 · Suggestion 16（共 24 项）。

交叉验证说明：信号接线与编辑层行为引用了本批次之外的真实文件（workflow_panel.py / workflow_graph_edit.py / workflow_graph_models.py / graph_ui_protocol.py），仅作证据，未改动。

---

## src/features/ai_workflow/ui/__init__.py (行数: 0)

### 发现

（无）

### 通过项
- 通过（无逻辑）：空文件，不导出任何符号。

---

## src/features/ai_workflow/ui/workflow_canvas_adapter.py (行数: 197)

### 发现

- [AUD-23912] [Major] build_from_graph 对节点坐标 float() 强转无 None/非法值防护，载入持久化图可崩溃
  - 位置: src/features/ai_workflow/ui/workflow_canvas_adapter.py:69-72
  - 证据:
    ```python
    for node in getattr(graph, "nodes", ()):
        x = float(node.ui.get("x", 0.0)) if isinstance(node.ui, dict) else 0.0
        y = float(node.ui.get("y", 0.0)) if isinstance(node.ui, dict) else 0.0
        self.add_node_visual(node.node_id, node.spec_id, x=x, y=y)
    ```
  - 影响: 本方法是 workflow_panel.load_graph（工程持久化还原入口，panel.py:569 → _set_current_graph:581）的渲染路径。JSON 中 `"x": null` 或坐标为非数值字符串时 `float(None)` 抛 TypeError，整个图加载中断——违反「外部数据须验证」铁律（isinstance 只挡了 ui 不是 dict 的情况，未挡值类型）。
  - 建议: 抽 `_safe_float(v, default=0.0)`（try/except (TypeError, ValueError) 回退默认值）再喂 add_node_visual。
  - 置信度: 中

- [AUD-23913] [Suggestion] registry 构造参数缺类型注解
  - 位置: src/features/ai_workflow/ui/workflow_canvas_adapter.py:44-52
  - 证据:
    ```python
    def __init__(
        self,
        *,
        registry,
        visual_tokens: Optional[VisualTokens] = None,
    ) -> None:
    ```
  - 影响: registry 约定实现 `get(spec_id) -> spec|None` 协议，无注解降低可读性与 IDE 检查；与项目强类型要求不符。
  - 建议: 定义/引用 NodeSpecRegistry 协议类型并标注。
  - 置信度: 高

### 通过项
- 未知 spec_id 走 `_PlaceholderSpec`（category=control）不崩溃，符合红线。
- `remove_connection_visual` 先解除两端节点注册再从 view 移除，顺序正确。
- 未引入 moderngl / composite.core；不直接写 timeline；不传 GPU texture。
- `add_connection_visual` 节点缺失时静默跳过（None 检查，120 行附近）。

---

## src/features/ai_workflow/ui/workflow_canvas_view.py (行数: 637)

### 发现

- [AUD-23901] [Major] 框选矩形 overlay 用视口坐标绘制，而 drawForeground 的 painter 处于场景坐标系
  - 位置: src/features/ai_workflow/ui/workflow_canvas_view.py:632-637（另见 368-374 存储侧）
  - 证据:
    ```python
    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        """框选矩形 overlay。"""
        if self._rubber_active and not self._rubber_rect.isNull():
            painter.setBrush(QColor(255, 180, 60, 30))
            painter.setPen(QPen(QColor(255, 180, 60, 150), 1))
            painter.drawRect(self._rubber_rect)
    ```
    而 `_rubber_rect` 来自 `event.position().toPoint()`（视口坐标，374 行 `self._rubber_rect = QRectF(x, y, w, h)`）。
  - 影响: Qt 6 文档明确 drawBackground/drawForeground "All painting is done in scene coordinates"。场景原点与视口原点不重合是常态（sceneRect 从 -5000 起、可平移、zoom 0.2–3.0 缩放），框选矩形会被平移/缩放绘制到错误位置甚至完全不可见；用户在缩放/平移后看不到正在拖拽的框选反馈（命中判定本身用视口坐标，所以选区正确、只有显示错）。
  - 建议: drawForeground 里先 `painter.setTransform(QTransform())` 切到视口坐标再画，或将 `_rubber_rect` 换算为场景坐标存储。
  - 置信度: 高

- [AUD-23902] [Major] 空地点击与框选都不清除已有选中，用户无法取消选中
  - 位置: src/features/ai_workflow/ui/workflow_canvas_view.py:311-318、434-445
  - 证据:
    ```python
    if event.button() == Qt.MouseButton.LeftButton:
        item = self.itemAt(event.position().toPoint())
        if item is None:
            self._rubber_active = True
            self._rubber_start = event.position().toPoint()
            self._rubber_rect = QRectF()
            event.accept()
            return
    ```
    ```python
    view_rect = QRectF(self.mapFromScene(item_rect).boundingRect())
    if self._rubber_rect.intersects(view_rect):
        item.setSelected(True)
    ```
  - 影响: 空地按下直接 accept 返回，`super().mousePressEvent` 不被调用，QGraphicsScene 收不到 press，Qt 默认「点空地清除选中」失效；且 `_update_rubber_selection` 只 `setSelected(True)` 不清除框外的既有选中 → 选中集合只增不减。Inspector（经 node_selected/selection_cleared 桥接）无法通过点击空白复位，属交互功能缺陷。
  - 建议: 空地按下（无 Ctrl 修饰）先 `self._scene.clearSelection()`；框选开始时同样先清一次。
  - 置信度: 高

- [AUD-23903] [Major] nodes_moved 快照在选中变化之前采集，首次点选未选中节点并拖动时移动不提交
  - 位置: src/features/ai_workflow/ui/workflow_canvas_view.py:319-325、404-417
  - 证据:
    ```python
    # P10.1c：点中节点 → 记当前选中节点的 start 快照（release 时比对 emit nodes_moved）
    self._move_start_positions = {
        nid: QPointF(it.x(), it.y())
        for nid, it in self._node_items.items()
        if it.isSelected()
    }
    super().mousePressEvent(event)
    ```
  - 影响: Qt 在 `super().mousePressEvent`（场景 press 分发）阶段才把被点击的未选中节点置为选中；快照在此之前采集，不含该节点。用户点击一个未选中节点直接拖动 → release 比对时它不在快照 → `nodes_moved` 不 emit → panel._on_nodes_moved 不执行（panel.py:323）：graph 数据与 undo 栈丢失这次移动，视觉与模型不一致，任意后续重建（undo 其它操作 / 重载模板）后节点跳回原位。仅当节点「已经是选中态」再拖才正常。
  - 建议: 将快照采集移到 `super().mousePressEvent(event)` 之后（此时选中已更新、位置尚未变化——移动发生在 mouseMove）。
  - 置信度: 高

- [AUD-23904] [Major] add_connection_item 重复 key 直接覆盖 dict，旧连线孤儿化残留 scene
  - 位置: src/features/ai_workflow/ui/workflow_canvas_view.py:208-213（同模式 183-187）
  - 证据:
    ```python
    def add_connection_item(self, conn: WorkflowConnectionItem) -> None:
        """添加连线视觉项到画布。"""
        self._scene.addItem(conn)
        self._connection_items[conn.key] = conn
        self.node_connected.emit(*conn.key)
    ```
  - 影响: 数据模型不校验重复边（workflow_graph_edit.py:137 注释明确「model 的 __post_init__ 不查重复边」，workflow_models.py:295-313 的 post_init 也只查 node_id 唯一与 ghost）。`load_graph` 载入含重复 edge 的持久化 JSON 时 `build_from_graph` 会以相同 key 调两次 add_connection_item：dict 覆盖后第一条 conn 仍在 scene 中，且从此无法经 remove_connection_item（按 dict 查找）移除——重复视觉 + 不可回收项。节点侧因模型强制 node_id 唯一暂不可达，但同款无防护写法。
  - 建议: 写 dict 前检查既有 key，存在则先 `_remove_connection_internal(key)`（add_node_item 同理）。
  - 置信度: 中

- [AUD-23905] [Major] assets_dropped 信号 + setAcceptDrops(True) 无任何拖放事件实现，功能为死通道
  - 位置: src/features/ai_workflow/ui/workflow_canvas_view.py:82、115
  - 证据:
    ```python
    assets_dropped = pyqtSignal(list, tuple)
    ```
    ```python
    self.setAcceptDrops(True)
    ```
  - 影响: 全仓 grep 确认 src/features/ai_workflow 与 src/shared/node_canvas 均无 dragEnterEvent/dropEvent/dragMoveEvent 实现；QGraphicsView 收到拖入会转发给场景内 item，而没有任何 item 接受拖放 → 拖素材到画布无任何反应，`assets_dropped` 永不发射。GraphUiSignals.ALL 承诺的 ASSETS_DROPPED 通道名存实亡（test_node_canvas_protocol.py:74 只断言信号名存在）。
  - 建议: 实现 dragEnterEvent（校验 mime 后 `acceptProposedAction()`）与 dropEvent（mapToScene 换算 + emit assets_dropped）；或先移除 setAcceptDrops 与信号声明，避免上游误接。
  - 置信度: 高

- [AUD-23906] [Major] 程序化 add_connection_item 发用户意图信号 node_connected，造成 host 编辑层必然再入
  - 位置: src/features/ai_workflow/ui/workflow_canvas_view.py:208-213；交叉证据 workflow_panel.py:367-380、workflow_canvas_adapter.py:74-81
  - 证据:
    ```python
    self._scene.addItem(conn)
    self._connection_items[conn.key] = conn
    self.node_connected.emit(*conn.key)
    ```
    panel 槽：
    ```python
    self._current_graph = self._edit.add_edge(
        self._current_graph, from_node, from_port, to_node, to_port,
    )
    self.adapter.add_connection_visual(from_node, from_port, to_node, to_port)
    ...
    except ValueError:
        # 重复边 / 类型不符 / ghost：忽略（view 已做类型校验，这里兜底）
    ```
  - 影响: 两条再入链均已核实：① 用户拖线 → view emit → panel add_edge + add_connection_visual → add_connection_item **再次 emit** → panel 再入 add_edge（重复边 ValueError 被吞）——每次建线多一轮全边扫描的无效编辑；② `build_from_graph` 静默重建期间（panel 先置 _current_graph 再 build，panel.py:577-581）每条边同样触发一次必然失败的 add_edge，载入成本 O(E²)。回路终止完全依赖 add_edge 的重复边异常，属未加防护的信号递归（作者对 graph_changed 已做抑制——adapter.py:79-81 注释，node_connected 漏防），违反 .clinerules「程序化更新须防递归信号」。
  - 建议: `add_connection_item` 增加静默参数或拆出 `_insert_connection`（不发信号），仅 `_finish_connection_drag` 的用户手势路径 emit node_connected。
  - 置信度: 高

- [AUD-23907] [Suggestion] remove_node_item 删除关联连线时不清理存活端节点的 _connections 注册
  - 位置: src/features/ai_workflow/ui/workflow_canvas_view.py:194-201；交叉 workflow_node_item.py:201-206
  - 证据:
    ```python
    related = [
        key for key in self._connection_items
        if key[0] == node_id or key[2] == node_id
    ]
    for key in related:
        self._remove_connection_internal(key)
    self._scene.removeItem(item)
    ```
  - 影响: `_remove_connection_internal` 只从 scene/dict 移除；连线另一端存活节点的 `_connections` 列表仍持有死引用，此后每次拖动该节点，`itemChange` 都对已出场景的连线调 `update_path()`（无效计算），且连线 wrapper 生命周期被拖住。adapter.remove_connection_visual 有 unregister 步骤，但删节点路径绕过了它。
  - 建议: 循环内对两端仍存活的节点项 `unregister_connection(conn)` 后再移除。
  - 置信度: 高

- [AUD-23908] [Suggestion] drawBackground 逐点 drawPoint 双重循环，缩小视图时单次重绘可达 10 万+ 绘点调用
  - 位置: src/features/ai_workflow/ui/workflow_canvas_view.py:616-630
  - 证据:
    ```python
    grid = self._tokens.grid_size
    painter.setPen(QPen(QColor(255, 255, 255, 20), 1))
    left = int(rect.left()) - (int(rect.left()) % grid)
    top = int(rect.top()) - (int(rect.top()) % grid)
    x = left
    while x < rect.right():
        y = top
        while y < rect.bottom():
            painter.drawPoint(x, y)
            y += grid
        x += grid
    ```
  - 影响: grid_size=20、zoom_min=0.2（graph_ui_protocol.py:86-88）。zoom 0.2 时 1920px 视口横向覆盖约 9600 scene px → 单次背景重绘约 480×270 ≈ 13 万次 drawPoint；4K 屏更甚，画布交互明显卡顿（项目 4K 实时基准）。
  - 建议: 预渲染单格 tile QPixmap 后平铺；或对可见点数设上限（超限退化为纯色背景）。
  - 置信度: 高

- [AUD-23909] [Suggestion] _space_held 无 focusOut 兜底，Space 状态可卡死为平移模式
  - 位置: src/features/ai_workflow/ui/workflow_canvas_view.py:584-612
  - 证据:
    ```python
    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space:
            self._space_held = True
            self.setCursor(Qt.CursorShape.OpenHandCursor)
    ...
    def keyReleaseEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space:
            self._space_held = False
    ```
  - 影响: 按住 Space 期间焦点切到其它控件（release 收不到）→ `_space_held` 永久为 True；回到画布后左键全部被当作平移起点（294-300 行），选择/连线/框选失效，需再按一次 Space 才能恢复。
  - 建议: 重写 focusOutEvent：重置 `_space_held = False` 并恢复光标。
  - 置信度: 中

- [AUD-23910] [Suggestion] 命中检测直接访问节点项私有成员，且与锚点规则两处重复实现
  - 位置: src/features/ai_workflow/ui/workflow_canvas_view.py:464-477、492-514
  - 证据:
    ```python
    local = item.mapFromScene(scene_pos)
    # input 端口 x ≈ port_radius*0.5；output x ≈ width - port_radius*0.5
    in_x = item._tokens.port_radius * 0.5
    out_x = item._width - item._tokens.port_radius * 0.5
    for pname, py in item._input_port_ys.items():
    ```
  - 影响: 跨对象访问 `_tokens/_width/_input_port_ys`；「±0.5*port_radius」规则在 view 命中检测与 node_item.port_scene_pos（226/232 行）各自硬编码一份，节点项布局一旦调整会静默不同步。
  - 建议: WorkflowNodeItem 提供公开 `port_local_pos(name, direction)`（或 `port_hit_test(local_pos)`），view 统一调用。
  - 置信度: 高

- [AUD-23911] [Suggestion] 事件处理器参数缺类型注解
  - 位置: src/features/ai_workflow/ui/workflow_canvas_view.py:584、606
  - 证据:
    ```python
    def keyPressEvent(self, event) -> None:
    ...
    def keyReleaseEvent(self, event) -> None:
    ```
  - 影响: 应为 `event: QKeyEvent`；与项目「所有函数有类型注解」约束不符。
  - 建议: 补 QKeyEvent 注解。
  - 置信度: 高

### 通过项
- 线程安全：全部逻辑在 UI 线程，无 OpenGL/渲染线程交互，无跨线程 emit。
- GPU 资源生命周期：不涉及（纯 QGraphicsView）。
- 缩放：zoom_to 正确 clamp 到 zoom_min..zoom_max 且 1e-6 去抖，wheelEvent 无除零风险。
- 异常路径：连线拖拽中源节点被删时 `self._node_items.get(self._drag_src_node_id or "")` 容错；`_finish_connection_drag` 状态清理完整（临时连线移除 + 五个状态字段复位）。
- 连线拖拽校验：类型匹配 + 自连禁止（_find_nearest_valid_target）与释放时的方向/类型复核一致。
- 输入验证：rubber/panning 状态机分支互斥，`self._pan_start is not None` 等空值防护到位。

---

## src/features/ai_workflow/ui/workflow_connection_item.py (行数: 181)

### 发现

- [AUD-23918] [Suggestion] shape() 命中区宽度 12px 硬编码，违背视觉 token 单一来源
  - 位置: src/features/ai_workflow/ui/workflow_connection_item.py:153-158
  - 证据:
    ```python
    def shape(self) -> QPainterPath:
        """扩宽命中区到 12px（便于点击）。"""
        stroker = QPainterPathStroker()
        stroker.setWidth(12)
        stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
        return stroker.createStroke(self.path())
    ```
  - 影响: 本文件头部红线声明「视觉 token 单一来源」，但命中宽度不走 VisualTokens（connection_width=2 来自 token，命中 12 为魔法数）；token 调整时命中区不联动。
  - 建议: VisualTokens 增加 `hit_width`（如 connection_width + 固定余量）字段。
  - 置信度: 高

- [AUD-23919] [Suggestion] src_port/dst_port 构造参数无类型注解
  - 位置: src/features/ai_workflow/ui/workflow_connection_item.py:46-51
  - 证据:
    ```python
        *,
        src_port,
        dst_port,
        artifact_type: str,
        visual_tokens: VisualTokens | None = None,
    ```
  - 影响: 期望鸭子类型协议（`port_scene_pos(port, direction) -> QPointF`）未注明，接口契约只存在于 docstring。
  - 建议: 定义 PortProvider Protocol 并标注。
  - 置信度: 高

### 通过项
- update_path 端口缺失（None）时置空 QPainterPath 容错，不崩溃。
- make_temp_connection 用 _DummyPort 占位避免临时连线 update_path 崩溃，设计意图明确。
- set_drag_validity 保留 pen 宽度只换 color，恢复路径正确。
- 浮点字面量规范（60.0、0.5）；无 GLSL/HDR 维度问题（纯 UI）。

---

## src/features/ai_workflow/ui/workflow_form_page.py (行数: 147)

### 发现

- [AUD-23923] [Suggestion] _inspectors 列表只写不读（死状态）；registry 参数注解为 Any
  - 位置: src/features/ai_workflow/ui/workflow_form_page.py:39-46、124、144
  - 证据:
    ```python
    self._node_registry = node_registry
    self._inspectors: list[WorkflowInspector] = []
    ```
    ```python
    self._inspectors.append(inspector)
    ```
    ```python
    self._inspectors = []
    ```
  - 影响: 收集后无任何读取用途，维护噪音；`node_registry: Any` 弱类型（约定 `get(spec_id)` 协议未体现）。
  - 建议: 删除该字段或注明用途；registry 标注协议类型。
  - 置信度: 高

### 通过项
- 信号防递归：inspector.parameter_changed 仅透传给 host，本页不回写控件，无回环。
- set_graph(None) 正确复位标题；_clear_sections 保留末尾 stretch、对子部件 deleteLater，无泄漏/双删。
- 无 params_schema 的节点正确跳过；全空时给出提示文案；i18n 走 tr() 带默认值。
- 布局插入位置 `insertWidget(count()-1, ...)` 保证 stretch 恒在末尾。

---

## src/features/ai_workflow/ui/workflow_gallery_page.py (行数: 111)

### 发现

- [AUD-23924] [Suggestion] 文档承诺「双击卡片」选择模板，实现缺失（仅按钮路径可用）
  - 位置: src/features/ai_workflow/ui/workflow_gallery_page.py:6-7（对照 84-108 全函数）
  - 证据:
    ```python
    - 双击或「使用」按钮 → emit ``template_selected(template_id)``（host 调
      ``panel.load_template``）。
    ```
    而 `_make_card` 中唯一的触发路径是：
    ```python
    use_button.clicked.connect(
        lambda _=False, tid=str(entry.template_id):
            self.template_selected.emit(tid)
    )
    ```
  - 影响: 模块文档与交互不符：用户按文档双击卡片无任何响应（无 mouseDoubleClickEvent 实现），功能入口仅剩按钮。
  - 建议: 卡片补 mouseDoubleClickEvent → emit template_selected；或修正文档删除「双击」承诺。
  - 置信度: 高

### 通过项
- lambda 默认参捕获 template_id，避免循环晚绑定经典错误。
- rebuild 清理保留末尾 stretch，deleteLater 旧卡片无泄漏；分类动态 i18n key 带原文回退。
- 纯 Qt，无 GPU/线程/GLSL 维度问题。

---

## src/features/ai_workflow/ui/workflow_inspector.py (行数: 179)

### 发现

- [AUD-23920] [Major] set_node 对参数值 int()/float() 强转无异常防护，异常外部数据使选中/加载路径崩溃
  - 位置: src/features/ai_workflow/ui/workflow_inspector.py:146-168
  - 证据:
    ```python
    if ptype == "number":
        sb = QDoubleSpinBox()
        mn = definition.get("minimum", 0.0)
        mx = definition.get("maximum", 1.0)
        sb.setRange(float(mn), float(mx))
        sb.setValue(float(value) if value is not None else float(mn))
    ```
    ```python
    if ptype == "integer":
        ...
        sb.setValue(int(value) if value is not None else int(mn))
    ```
  - 影响: 本类的调用链是 panel._on_node_selected（选中节点）与 form_page.set_graph（模板加载），value 来自 `dict(node.params)`，而 node.params 最终源自持久化 `topos.workflow/1` JSON（load_graph 还原）。JSON 中参数类型漂移（字符串 "abc"、嵌套对象、bool 与 integer 混用等）→ `float()/int()` 抛 ValueError/TypeError，选中节点或加载模板表单直接崩溃；schema 侧 minimum/maximum 非数值同样裸转。违反「外部数据须验证」。
  - 建议: 抽 `_to_number(value, default)` 做容错转换（try/except (TypeError, ValueError) 回退 minimum/0），value 与 mn/mx 一并防护。
  - 置信度: 中

- [AUD-23921] [Suggestion] JSON-Schema 的 default 从未被读取，缺键参数初值取 minimum/空串/False
  - 位置: src/features/ai_workflow/ui/workflow_inspector.py:89（对照 119、139、151、164 的回退值）
  - 证据:
    ```python
    for key, definition in properties.items():
        value = params.get(key)
    ```
    ```python
    sb.setValue(float(value) if value is not None else float(mn))
    ```
  - 影响: 若 graph 侧实例化未补全缺省参数，控件显示 schema.minimum（常为 0.0）而非 schema default，表单初值与编译期实际默认可能不一致。取决于上游是否已填默认，故标低置信。
  - 建议: `value = params.get(key, definition.get("default"))`，并将 default 一并纳入 _to_number 容错。
  - 置信度: 低

- [AUD-23922] [Suggestion] "(no selection)" 占位文案硬编码英文，未走 tr()
  - 位置: src/features/ai_workflow/ui/workflow_inspector.py:57、102
  - 证据:
    ```python
    self._title = QLabel("<b>(no selection)</b>")
    ```
  - 影响: 与项目 i18n 规范不一致（同目录 form/gallery 均使用 tr()）。
  - 建议: `tr("workflow.inspector.no_selection", "(no selection)")`。
  - 置信度: 高

### 通过项
- 信号防递归（重点核查通过）：四类控件的程序化设值全部发生在 connect 之前——QCheckBox 119-123（setChecked 后 connect toggled）、QComboBox 128-135（addItem/setCurrentIndex 后 connect）、QDoubleSpinBox 150-156 与 QSpinBox 162-167（setRange/setValue 后 connect valueChanged）、QLineEdit 文本构造即定。重建式 set_node 不会触发 parameter_changed 回环。
- clear_node 用 removeRow（自动 deleteLater 行内控件）且注释说明了不再手动删除的原因，无对已删 C++ 对象操作的风险。
- 未知类型兜底 QLineEdit，enum findText 失败安全降级（不选中）。
- 无 GPU/线程/GLSL 维度问题。

---

## src/features/ai_workflow/ui/workflow_node_item.py (行数: 391)

### 发现

- [AUD-23914] [Suggestion] paint() 中 bar_rect 为死变量（计算后从未使用）
  - 位置: src/features/ai_workflow/ui/workflow_node_item.py:303-312
  - 证据:
    ```python
    status_color = QColor(self.status_color)
    bar_rect = QRectF(
        rect.left(), rect.bottom() - 4, rect.width(), 4,
    )
    bar_path = QPainterPath()
    # 底部色条也圆角（与 body 底部一致）
    bar_path.addRoundedRect(
        QRectF(rect.left(), rect.bottom() - 8, rect.width(), 8),
        radius, radius,
    )
    ```
  - 影响: 死代码误导读者（看似色条高 4px，实际绘制 8px 再裁剪）。
  - 建议: 删除 bar_rect 两行。
  - 置信度: 高

- [AUD-23915] [Suggestion] 端口圆点绘制中心（x=0 / x=width）与命中检测、连线锚点（±0.5*port_radius）系统性不一致
  - 位置: src/features/ai_workflow/ui/workflow_node_item.py:330、344（绘制）对照 226、232（锚点）
  - 证据:
    ```python
    self._draw_port_dot(painter, 0.0, y, token)
    ...
    self._draw_port_dot(painter, self._width, y, token)
    ```
    对照锚点：
    ```python
    local = QPointF(self._tokens.port_radius * 0.5, y)
    ...
    local = QPointF(self._width - self._tokens.port_radius * 0.5, y)
    ```
  - 影响: 圆点中心画在节点边界线上（外半侧悬在节点外），而点击热区与连线锚点在边界内侧 0.5*port_radius=3px（port_radius=6，graph_ui_protocol.py:83）。视觉与命中/锚点偏移 3px：点在可见圆点外半侧时命中的是节点本体（触发拖动而非连线拖拽），连线端点也与圆点视觉错位。
  - 建议: 绘制与 port_scene_pos/命中检测统一使用同一 local 坐标（推荐全部内嵌 0.5*radius），单一来源提供。
  - 置信度: 高

- [AUD-23916] [Suggestion] input/output_artifact_types 属性每次访问重建 dict，位于鼠标移动热路径
  - 位置: src/features/ai_workflow/ui/workflow_node_item.py:150-155；调用方 workflow_canvas_view.py:504-509
  - 证据:
    ```python
    @property
    def input_artifact_types(self) -> Dict[str, str]:
        return {p.name: p.artifact_type for p in self._input_ports}
    ```
  - 影响: `_find_nearest_valid_target` 在连线拖拽的每次 mouseMove 中对每个节点访问该属性 → 每次移动 O(n) 个临时 dict 分配，纯浪费（端口集构造后不变）。
  - 建议: __init__ 一次性构建并缓存两个 dict。
  - 置信度: 高

- [AUD-23917] [Suggestion] 若干成员/参数缺泛型或类型注解
  - 位置: src/features/ai_workflow/ui/workflow_node_item.py:76-77、115、246-248
  - 证据:
    ```python
    self._input_ports: Tuple = tuple(getattr(node_spec, "input_ports", ()))
    ...
    self._connections: list = []
    ...
    def paint(
        self, painter: QPainter, option, widget=None,
    ) -> None:
    ```
  - 影响: 裸 Tuple/list 与未注解 option/widget 削弱类型检查价值。
  - 建议: `Tuple[Any, ...]`（或端口类型）、`list` → 具体元素类型、`option: QStyleOptionGraphicsItem`。
  - 置信度: 高

### 通过项
- 线程安全 / GPU 生命周期：纯 QGraphicsItem，无 OpenGL 调用，无跨线程状态。
- itemChange 的宽 except 有 noqa: BLE001 注明理由（单连线失败不扩散），且作用域为逐连线 update_path，属可接受的最小吞噬，未掩盖领域错误。
- boundingRect 对 glow 外扩与 paint 的三层 glow 绘制范围一致。
- register/unregister_connection 幂等（in 判重 / except ValueError no-op）。
- 尺寸计算无除零；端口 y 布局与 port_spacing 换算一致。

---

## 汇总索引

| 编号 | 严重度 | 文件 | 标题 |
|---|---|---|---|
| AUD-23901 | Major | workflow_canvas_view.py | drawForeground 视口/场景坐标错位 |
| AUD-23902 | Major | workflow_canvas_view.py | 无法取消选中（空地点击/框选不清除） |
| AUD-23903 | Major | workflow_canvas_view.py | nodes_moved 快照早于选中变化，首次拖动不提交 |
| AUD-23904 | Major | workflow_canvas_view.py | 重复 key 覆盖致连线孤儿残留 |
| AUD-23905 | Major | workflow_canvas_view.py | assets_dropped 无拖放实现（死信号） |
| AUD-23906 | Major | workflow_canvas_view.py | 程序化 add_connection_item 发 node_connected 致 host 再入 |
| AUD-23907 | Suggestion | workflow_canvas_view.py | 删节点不清理存活端 _connections |
| AUD-23908 | Suggestion | workflow_canvas_view.py | drawBackground 逐点绘制性能 |
| AUD-23909 | Suggestion | workflow_canvas_view.py | _space_held 无 focusOut 重置 |
| AUD-23910 | Suggestion | workflow_canvas_view.py | 命中检测访问私有成员/规则重复 |
| AUD-23911 | Suggestion | workflow_canvas_view.py | 事件参数缺类型注解 |
| AUD-23912 | Major | workflow_canvas_adapter.py | float(None) 载入持久化图崩溃 |
| AUD-23913 | Suggestion | workflow_canvas_adapter.py | registry 参数缺注解 |
| AUD-23914 | Suggestion | workflow_node_item.py | bar_rect 死变量 |
| AUD-23915 | Suggestion | workflow_node_item.py | 端口绘制与命中/锚点偏移 |
| AUD-23916 | Suggestion | workflow_node_item.py | artifact_types 属性热路径重建 dict |
| AUD-23917 | Suggestion | workflow_node_item.py | 类型注解缺失 |
| AUD-23918 | Suggestion | workflow_connection_item.py | 命中宽度硬编码未走 token |
| AUD-23919 | Suggestion | workflow_connection_item.py | src_port/dst_port 缺注解 |
| AUD-23920 | Major | workflow_inspector.py | int()/float() 强转无防护崩溃 |
| AUD-23921 | Suggestion | workflow_inspector.py | schema default 未使用 |
| AUD-23922 | Suggestion | workflow_inspector.py | 占位文案未 i18n |
| AUD-23923 | Suggestion | workflow_form_page.py | _inspectors 死状态/Any 注解 |
| AUD-23924 | Suggestion | workflow_gallery_page.py | 双击选择模板文档承诺未实现 |
