# 批次审查报告：batch-094 [P1] src/features/composite/ui//

- 任务书：docs/audit/2026-09-28/assignments/batch-094.md
- 编号区间：AUD-9400–AUD-9499（本报告使用 AUD-9400 – AUD-9409）
- 文件数：3（1,031 行）
- 汇总：Critical 0 · Major 3 · Suggestion 7

审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性（undo·redo·revision·持久化）/ 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。
交叉验证过的外部事实：`viewer_tool_system.py` 中 11 个被导出符号全部存在；`asset_dependency.py` 的 `AssetReference`/`AssetDependencyReport` 字段与面板用法一致；`node_editor_widget.py:2658-2674`、`composite_page.py:4167-4192`（undo 消费方）、`node_editor_widget.py:537-550, 855-872`（加载路径）、`file_controller.py:798-815`（项目恢复调用链，无 try/except 包裹 backdrop 循环）。

---

## src/features/composite/ui/__init__.py (行数: 38)

### 发现
- [AUD-9409] [Suggestion] 文档字符串声称本包提供 CompositeViewer/CompositeViewerBar/PixelInspectorBar，实际并未导入或导出
  - 位置: src/features/composite/ui/__init__.py:4-8
  - 证据:
    ```python
    Provides UI components for the Composite node editor page:
    - CompositeViewer: OpenGL viewport for displaying rendered textures
    - CompositeViewerBar: Toolbar overlay for viewer controls
    - PixelInspectorBar: Pixel value display under cursor
    - ViewerToolMode: Tool modes for viewer interaction
    - ViewerInteractionState: State tracking for tool interactions
    ```
    而实际 `from src.features.composite.ui.viewer_tool_system import (...)` 只导出 viewer_tool_system 的 11 个符号；CompositeViewer/CompositeViewerBar 定义于 `composite_viewport.py`（PixelInspectorBar 同文件），均不在 `__all__` 中。
  - 影响: 使用者按文档 `from src.features.composite.ui import CompositeViewer` 将直接 ImportError；文档与导出面长期漂移。
  - 建议: 要么在 `__all__` 中补导出这三个类（从 composite_viewport.py re-export），要么把文档改为只描述 viewer_tool_system 的符号。
  - 置信度: 高

### 通过项
- 纯 re-export 无逻辑；已核对 `viewer_tool_system.py` 中 ViewerToolMode/ViewerInteractionState/HandleType/TOOL_MODE_LABELS/TOOL_MODE_SHORTCUTS/view_pos_to_ndc/ndc_to_view_pos/ndc_to_image_pos/image_pos_to_ndc/compute_image_ndc_rect/hit_test_handle 全部存在，导入名与 `__all__` 一致，无循环导入
- 无线程安全 / GPU / GLSL / 信号 / 持久化逻辑（不适用）

---

## src/features/composite/ui/asset_dependency_panel.py (行数: 199)

### 发现
- [AUD-9407] [Suggestion] set_operation_message 永久覆盖 summary 样式，update_report 不恢复中性色
  - 位置: src/features/composite/ui/asset_dependency_panel.py:146-150（对照 119-122、139-144）
  - 证据:
    ```python
    def set_operation_message(self, message: str, *, error: bool = False) -> None:
        """Show the last page-level operation result below the table."""
        color = "#e88" if error else "#9c9"
        self._summary.setStyleSheet(f"color: {color}; font-size: 10px;")
        self._summary.setText(str(message))
    ```
    而 `update_report` 仅重设文本（`self._summary.setText(f"共 {len(report.references)} 项 · …")`），初始的 `"color: #888; font-size: 10px;"` 样式（第 121 行）从未被恢复。
  - 影响: 一次收集/重链接操作后，后续正常扫描的统计行永远沿用上次操作的红（错误）或绿（成功）颜色，状态色语义失真。
  - 建议: 在 `update_report` 开头恢复中性样式（提取常量），或让 `set_operation_message` 在下次 update 时自动清除错误态。
  - 置信度: 高
- [AUD-9408] [Suggestion] “角色”列直接显示原始 role slug，未使用已本地化的 purpose 字段
  - 位置: src/features/composite/ui/asset_dependency_panel.py:169-175
  - 证据:
    ```python
    values = (
        reference.node_id,
        reference.parameter,
        reference.role,
        _STATUS_LABELS.get(reference.status, reference.status),
        reference.resolved_path or reference.stored_path or "—",
    )
    ```
    核心层 `AssetReference` 已提供本地化 `purpose` 字段（asset_dependency.py 中 `_iter_node_paths` 产出 "媒体源"/"3D LUT"/“材质覆写贴图（…）”等），面板未使用。
  - 影响: 界面中英混杂，用户看到 "media"/"material_texture" 等内部标识而非中文角色名；同时 material_overrides 的合成参数名（如 `material_overrides[x].slot_overrides[y].path`）远超 120px 列宽且无 tooltip 兜底。
  - 建议: 角色列改用 `reference.purpose`（保留 role 作 tooltip），参数列可复用现有 tooltip 模式。
  - 置信度: 高

### 通过项
- 线程安全：纯 UI 边界控件，不触碰渲染线程/GPU 资源，所有对外通信经 pyqtSignal 发出，符合模块自述约束
- 信号防递归：按钮 clicked 直连信号 emit，表格重建期间无反馈回路，无递归风险
- 输入验证：`update_report(None)` 显式处理；状态标签/颜色用 `.get(..., fallback)` 兜底未知状态；`str()` 强转所有单元格值；`set_search_roots` 过滤空串
- lambda 捕获：`lambda _checked=False, node_id=..., parameter=...` 用默认参数正确绑定，无晚绑定缺陷
- 性能：逐行 insertRow 对典型依赖规模（几十行）无压力；无逐像素路径
- 异常路径：不吞异常、无宽泛 except；操作失败经 set_operation_message(error=True) 呈现
- 核心字段用法与 asset_dependency.py 的 frozen dataclass 定义逐一核对一致（references/resolved/missing/relink_candidates、is_missing/is_relink_candidate）

---

## src/features/composite/ui/backdrop_item.py (行数: 794)

### 发现
- [AUD-9400] [Major] _choose_color 非模态对话框无引用保持，方法返回即被 Python GC 销毁
  - 位置: src/features/composite/ui/backdrop_item.py:707-718
  - 证据:
    ```python
    def _choose_color(self):
        """Open color picker dialog with real-time color preview."""
        dlg = QColorDialog(self._color, None)
        dlg.setWindowTitle("Choose Backdrop Color")
        dlg.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog)

        def _on_live(color: QColor):
            if color.isValid():
                self.setColor(color.name())

        dlg.currentColorChanged.connect(_on_live)
        dlg.show()
    ```
    `dlg` 无父对象（Python 持有所有权）且仅为局部变量；`dlg.show()` 非模态不阻塞，`_change_backdrop_color`（node_editor_widget.py:3661 调用本方法）返回后局部引用归零，PyQt6 随即删除 C++ 对象。
  - 影响: 取色器窗口闪现即被销毁（右键“更改颜色”功能实际不可用）；销毁与信号分发竞态时可能抛 RuntimeError（wrapped C++ object deleted）。注：sticky_note_item.py:586-597 存在完全相同的模式（另行批次）。
  - 建议: 将对话框存为实例属性（`self._color_dialog = dlg`）并连接 finished 做清理；或改用模态 `exec()` 并仅在 Accepted 后一次性应用，同时支持取消回滚。
  - 置信度: 高
- [AUD-9401] [Major] 取色器实时预览每个颜色 tick 都提交一次完整 undo 组并全图同步（undo 洪泛）
  - 位置: src/features/composite/ui/backdrop_item.py:713-717（触发源）、357-365（setColor）、240-253（_notify_changed）
  - 证据:
    ```python
    def _on_live(color: QColor):
        if color.isValid():
            self.setColor(color.name())

    dlg.currentColorChanged.connect(_on_live)
    ```
    ```python
    def setColor(self, color: str):
        """Set the backdrop color."""
        self._begin_change()
        ...
        self._notify_changed()
    ```
    `_notify_changed` 中 `old_data == new_data` 仅在颜色未变时短路；拖动色轮时每个 `currentColorChanged` tick 颜色都不同 → 每 tick 都会调用 `_changed_handler`（→ `_sync_backdrop_to_graph` → `update_contained_nodes` 全量 containment 重算 + `graph_changed.emit()` UI 状态保存）和 `_modified_handler`（→ composite_page.py:4175-4192 `begin_group/push/end_group` 压入一个 "Modify backdrop" undo 组）。
  - 影响: 一次拖动色轮产生几十个 undo 步，Ctrl+Z 只回退一个中间色而不是整个取色操作；undo 栈被冲刷，早先操作被挤出；每 tick 的 containment 重算与 graph_changed 保存属于鼠标移动频率的重复工作。
  - 建议: 实时预览期间置 `self._applying_state = True`（仅刷新画笔/填充，不通知），在对话框 `accepted`/`finished` 时以 `_begin_change` 捕获的原始色 + 最终色提交一次——这正是本文件 `_open_edit_dialog` 第 689-705 行已采用的成熟模式。
  - 置信度: 高
- [AUD-9402] [Major] from_dict/apply_state 对持久化数据无键校验，缺键/类型错直接 KeyError/TypeError 中断项目恢复
  - 位置: src/features/composite/ui/backdrop_item.py:775-791（from_dict）、266-269（apply_state）
  - 证据:
    ```python
    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'BackdropItem':
        """Create backdrop from dict."""
        return BackdropItem(
            backdrop_id=data['id'],
            title=data.get('title', 'Backdrop'),
            position=QPointF(data['position'][0], data['position'][1]),
            size=QRectF(0, 0, data['size'][0], data['size'][1]),
    ```
    调用链：`file_controller.py:813 build_from_graph(restored_graph)` → `node_editor_widget.py:871-872` 遍历 `graph.backdrops.values()` → `add_backdrop_visual`（仅校验 `"id" not in data`，第 539 行）→ `from_dict` 直接下标 `data['position']`、`data['size']`。`apply_state` 中 `data['position'][0]`、`float(data['opacity'])`、`int(data['z_order'])` 同样无类型/存在性校验。
  - 影响: 项目文件中任一 backdrop 条目缺 `position`/`size`（旧版本格式、手改文件、第三方生成）或类型不符，即抛 KeyError/TypeError；项目恢复调用链无 try/except 包裹，恢复中断，违反 .clinerules “外部数据须验证”。
  - 建议: `from_dict`/`apply_state` 对必填键做存在性与 list/tuple 长度校验，缺省回退默认值（position=(0,0)、size=(300,200)）并记录告警；数值字段 try/except (TypeError, ValueError) 回退默认。
  - 置信度: 高（代码路径确定会抛；实际触发依赖异常数据，发生率标中）
- [AUD-9403] [Suggestion] contained_node_ids 由 set 转 list，序列化顺序跨会话不确定
  - 位置: src/features/composite/ui/backdrop_item.py:510-514
  - 证据:
    ```python
    old_ids = set(self._contained_node_ids)
    new_ids = set(self._get_contained_node_ids(all_node_items))

    self._contained_node_ids = list(new_ids)
    return old_ids != new_ids
    ```
    `to_dict()`（第 772 行）原样写入 `'contained_node_ids': self._contained_node_ids`，经 `_sync_backdrop_to_graph` 持久化到图数据。
  - 影响: str 哈希随机化（默认启用）导致同一图在不同进程保存出的 JSON 中 contained_node_ids 顺序不同——保存文件 diff 噪声、任何基于字典/列表相等性的状态比较在跨会话场景下误报“已变更”。
  - 建议: `self._contained_node_ids = sorted(new_ids)`（一字符修复，同时让 to_dict 输出稳定）。
  - 置信度: 高
- [AUD-9404] [Suggestion] resize 每个鼠标 tick 重复更新 resize handle 并对 note 全文重排
  - 位置: src/features/composite/ui/backdrop_item.py:460-469（对照 169-175、159-161）
  - 证据:
    ```python
    if self._resizing:
        pos = event.pos()
        delta = pos - self._resize_start_pos
        new_width = max(100, self._resize_start_rect.width() + delta.x())
        new_height = max(80, self._resize_start_rect.height() + delta.y())
        self.setRect(0, 0, new_width, new_height)
        self._update_resize_handle()
    ```
    `setRect` 覆写（169-175 行）已调用 `_update_resize_handle()` 与 `_update_note_item()`；此处显式 `_update_resize_handle()` 属重复调用；`_update_note_item` 每 tick 执行 `setTextWidth` + `setPlainText`（QTextDocument 全文重排）。
  - 影响: 交互式 resize 期间每个 mouse-move 做双份 handle 更新与一次文本布局；对长 note 是每像素级的重复排版开销（项目 4K 实时性能基调下属可避免浪费）。
  - 建议: 删除 mouseMoveEvent 中显式的 `_update_resize_handle()`；note 布局可仅在宽度变化超阈值（如 1px 累积）或 resize 结束时重排。
  - 置信度: 高
- [AUD-9405] [Suggestion] 构造参数 None 默认值未标 Optional，大量公有方法与事件处理器缺返回类型注解
  - 位置: src/features/composite/ui/backdrop_item.py:35-49 及全文件多处
  - 证据:
    ```python
    def __init__(
        self,
        backdrop_id: str,
        title: str = "Backdrop",
        position: QPointF = None,
        size: QRectF = None,
        color: str = None,
    ```
    类型注解与默认值矛盾（应为 `Optional[QPointF]` 等）。此外 `setTitle/setNote/setColor/setZOrder/send_to_back/hoverMoveEvent/hoverLeaveEvent/mousePressEvent/mouseMoveEvent/mouseReleaseEvent/itemChange/set_changed_handler/set_node_items_provider/take_node_moves/_update_resize_handle/_choose_color/_open_edit_dialog` 等均无返回注解（同文件的 `setBackgroundOpacity`、`backgroundOpacity` 已有注解，标准不一）。
  - 影响: 类型检查器（mypy/pyright strict）误报、IDE 签名提示失真，违反项目强类型约束。
  - 建议: 统一补 `Optional[...]` 与 `-> None`/`-> bool`/事件处理器签名注解。
  - 置信度: 高
- [AUD-9406] [Suggestion] _was_moved 标记只写不读（死状态）
  - 位置: src/features/composite/ui/backdrop_item.py:76、452、489、498
  - 证据:
    ```python
    # Move tracking (for undo)
    self._move_start_position: Optional[QPointF] = None
    self._was_moved = False
    ```
    ```python
    if moved:
        self._was_moved = True
        # Record how far each contained node moved, for undo
        self._finalize_node_moves()
    ```
    全文件 4 处均为赋值，无任何读取点（已 grep 确认）。
  - 影响: 死代码误导维护者以为存在基于该标记的分支；附带 `send_to_back(z_value: float)` 中 `self._z_order = int(z_value)` 与 `setZValue(z_value)` 截断/原值不一致的轻微隐患。
  - 建议: 删除 `_was_moved`（或补上本应使用它的逻辑）；`send_to_back` 统一先取整再用于两处。
  - 置信度: 高

### 通过项
- 线程安全：纯 QGraphicsItem UI 线程代码，无跨线程共享状态、无 OpenGL 调用
- GPU 资源生命周期：不创建/持有任何 ModernGL 或纹理资源（backdrop 不参与渲染，与文档一致）
- GLSL·HDR：不涉及着色器与 HDR 数据（不适用）
- 信号防递归（状态锁）：`_applying_state` 在 `apply_state`（finally 恢复旧值，支持重入）与 `_open_edit_dialog`（689-705 行，一次事务、一次通知）两处使用正确；undo/redo 恢复期间 itemChange 的节点跟随被正确抑制
- undo 提交一致性（除取色器路径外）：move/resize 经 `_begin_change`（press 时捕获）+ `_notify_changed`（release 时 old==new 短路无操作判断正确）构成单条 undo；跟随节点位移经 `_snapshot_contained_nodes`/`_finalize_node_moves`/`take_node_moves` 与 backdrop 修改并入同一 undo 组，顺序正确
- 输入验证：`opacity` 在构造、`setBackgroundOpacity`、`apply_state`、编辑对话框四处均做 0.0–1.0 clamp；`setBackgroundOpacity` 有 1e-6 去抖
- 渲染正确性：`boundingRect` 加 5px 边距覆盖 2px 边框 + 3px 选中光晕 + 抗锯齿的绘制外溢，修复脏区残留；画笔/填充不透明度分离保证标题可读
- 异常路径：`apply_state` 用 try/finally 保证 `_applying_state` 状态锁必被恢复，不会因单字段异常而永久锁死通知

---

## 审查方法说明

- 三文件全文逐行 Read（backdrop_item.py 794 行一次读全），所有发现均贴自真实行号代码
- 外部交叉验证：undo 消费链（node_editor_widget.py:2658-2710、composite_page.py:4167-4215）、加载链（node_editor_widget.py:537-550/855-884、file_controller.py:798-815）、re-export 目标符号、`_was_moved` 读写点、`DIALOG_BASE_STYLE` 导出，均以只读命令核对
- 宁缺毋滥：浮点字面量（本批无 GLSL）、线程/GPU 维度确认不适用即记通过项，未虚构发现
