# batch-103 [P1] — src/features/composite/ui//

编号区间：AUD-10300–AUD-10399。审查日期：2026-09-28。
文件：keyframes_tree_view.py (1,683)、material_panel.py (387)、minimap_widget.py (367)，共 2,437 行，全部通读。
交叉核对：keyframes_model.py（KeyframesRow 字段）、keyframes_panel.py（信号接线）、timeline_layout.py、
src/shared/ui/widgets/drag_value.py、src/features/composite/render3d/material_model.py。

统计：Critical 1 / Major 2 / Suggestion 11。

---

## src/features/composite/ui/keyframes_tree_view.py (行数: 1683)

### 发现

- [AUD-10300] [Critical] mouseReleaseEvent 绑定单击路径引用未赋值局部变量，触发 UnboundLocalError 崩溃
  - 位置: src/features/composite/ui/keyframes_tree_view.py:1391-1409
  - 证据:
    ```python
            if active and 0 <= idx < len(rows):
                source = rows[idx]
                target_node = str(drag["node_id"])
                target_param = str(drag["param_name"])
                ...
            elif not active:
                self.binding_requested.emit(
                    target_node, target_param)
    ```
  - 影响: `target_node`/`target_param` 只在 `if active and ...` 分支内赋值。单击 bind 列（mousePressEvent:1308-1321 置 `_binding_drag` 且 `_binding_drag_active=False`，未移动 ≥4px 即释放）进入 `elif not active:` 分支时二者未定义，抛 UnboundLocalError；PyQt6 中事件处理器内未捕获异常会经 qFatal 中止进程，即用户单击链接图标即崩溃。`binding_requested` 已在 keyframes_panel.py:892 接线，属真实可达交互路径。
  - 建议: 在 `if active ...` 判定之前统一从 `drag` 提取 `target_node`/`target_param`（与 source 判定解耦），并为该处理器补充回归测试（press→release 无 move）。
  - 置信度: 高

- [AUD-10301] [Major] Eye/Solo/Shy 列读写的行字段名与 KeyframesRow 模型不一致，开关图标状态永不反映真实值
  - 位置: src/features/composite/ui/keyframes_tree_view.py:755-758、1219-1224；对照 src/features/composite/ui/keyframes_model.py:135-137
  - 证据:
    ```python
        if row.row_type in ("node", "layer"):
            self._draw_eye(p, geom, y, getattr(row, "eye", True))
            self._draw_solo(p, geom, y, getattr(row, "solo", False))
            self._draw_lock(p, geom, y, row.locked)
            self._draw_shy(p, geom, y, getattr(row, "shy", False))
    ```
    而 KeyframesRow 实际字段为（keyframes_model.py:135-137，349/351/381/383 处由 layer.enabled/layer.shy 填充）：
    ```python
        layer_enabled: bool = True        # AE eye toggle state
        layer_solo: bool = False
        layer_shy: bool = False
    ```
  - 影响: 模型从不填充 `eye`/`solo`/`shy`（全库 grep 仅本文件引用），`getattr(row, "eye", True)` 永远取默认 True——已隐藏图层仍画"睁眼"图标；单击写入的 `row.eye` 是临时实例属性，模型重建后丢失；solo/shy 图标永远不会以激活态绘制（单击 solo 用的却是正确的 `layer_solo`，1234-1236）。文件头 docstring 第 14-16 行宣称的 "row.eye 由 host 填充" 契约并未实现。功能信号通路（eye_changed→panel→controller）本身有效，但树侧状态显示错误。
  - 建议: 统一改读写 `layer_enabled`/`layer_solo`/`layer_shy`，删除对不存在字段的 getattr 兜底；或让模型在构建行时别名填充。
  - 置信度: 高

- [AUD-10302] [Major] `_value_provider` 在交互路径未加异常防护（同一 provider 在绘制路径有防护）
  - 位置: src/features/composite/ui/keyframes_tree_view.py:1275-1276、1293-1294、1434-1435；对照 964-967
  - 证据:
    ```python
            if bool(status.get("at_current")):
                base_text = self._value_provider(row.node_id, row.param_name) \
                    if self._value_provider else None
    ```
    而绘制路径（964-967）对同一回调做了防护：
    ```python
        if provider is not None:
            try:
                text = provider(row.node_id, row.param_name)
            except Exception:
                text = None
    ```
  - 影响: provider 由宿主（KeyframesPanel）注入并做图查询，若在模型重建与鼠标事件之间节点/参数被删除等情况下抛异常，未捕获异常将从 mousePressEvent/mouseDoubleClickEvent 逃逸进 Qt 事件循环，导致 qFatal 中止。同文件 6 处防护该 provider 恰恰说明异常被认为可能，防护不一致。
  - 建议: 与绘制路径一致地包裹 try/except（捕获后回退 None/空串），或抽出 `_safe_value_text()` 帮助函数统一调用。
  - 置信度: 中

- [AUD-10303] [Suggestion] 内联关键帧编辑器四个控件同连 commit，一次操作触发多次提交信号
  - 位置: src/features/composite/ui/keyframes_tree_view.py:402-412
  - 证据:
    ```python
        apply_btn.clicked.connect(commit)
        frame_box.editingFinished.connect(commit)
        value_box.editingFinished.connect(commit)
        interp_box.currentTextChanged.connect(lambda _text: commit())
    ```
  - 影响: 点击 Apply 时 value_box/frame_box 失焦先触发 editingFinished、随后 clicked 再触发，`keyframe_edit_commit_requested` 双发（面板侧可能产生重复图变更/undo 记录）；切换插值下拉会立即提交中途值；改帧号回车也立即提交。
  - 建议: 只保留 Apply 与显式提交语义，或对 commit 加去抖/脏标记（任一控件被用户改动过才发一次）。
  - 置信度: 高

- [AUD-10304] [Suggestion] 多处宽泛 `except Exception` 静默吞错，且赋值失败后仍继续发信号
  - 位置: src/features/composite/ui/keyframes_tree_view.py:1220-1223（典型）、501-504、645-648、964-967、999-1008、1035-1041、1068-1071、1652-1656
  - 证据:
    ```python
            new_val = not getattr(row, "eye", True)
            try:
                row.eye = new_val
            except Exception:
                pass
            self.eye_changed.emit(row.node_id, new_val)
    ```
  - 影响: 违反 .clinerules "不得宽泛 except 掩盖领域错误"：行属性写入失败被吞后仍 emit eye_changed，模型状态与信号载荷可能不一致；dropEvent 中宿主 handler 异常被吞后仅 event.ignore()，无任何日志可排查。绘制路径对 provider 的兜底属可辩护的健壮性写法，但交互路径的 `pass` 无日志不可取。
  - 建议: 至少对交互路径（1220-1223、1652-1656）收窄异常类型并记录日志；行字段写入失败时不要继续 emit。
  - 置信度: 高

- [AUD-10305] [Suggestion] `_value_controls` 只增不减，DragValue 子控件随会话无界累积
  - 位置: src/features/composite/ui/keyframes_tree_view.py:530、550-552
  - 证据:
    ```python
                    self._value_controls[key] = control
        ...
        for key, control in self._value_controls.items():
            if key not in visible_keys:
                control.hide()
    ```
  - 影响: 每个曾显示过的 (node_id, param_name) 的 DragValue 永久保留为隐藏子控件；长会话、多节点/参数切换下原生控件句柄与内存单调增长，且每次 `_sync_value_controls`（随垂直滚动/refresh 高频触发）都要遍历全量 dict 并 raise_ 覆盖层。
  - 建议: 对连续多次不可见（或模型行已消失）的 key 执行 `control.deleteLater()` 并从 dict 移除。
  - 置信度: 高

- [AUD-10306] [Suggestion] eye/lock 列点击命中判定覆盖 param 行，而图标只在 node/layer 行绘制
  - 位置: src/features/composite/ui/keyframes_tree_view.py:1218-1226、1243-1247；对照绘制侧 754-757 的 `if row.row_type in ("node", "layer")`
  - 证据:
    ```python
        if self._hit_column(geom, "eye", x):
            new_val = not getattr(row, "eye", True)
            ...
        if self._hit_column(geom, "lock", x):
            row.locked = not row.locked
    ```
  - 影响: 在 param 行所处的 y 上点击 eye/lock 单元格（该处不绘制任何图标）会隐形切换其父节点的可见性/锁定并发出信号，用户无视觉反馈即改变了图状态。
  - 建议: 点击处理与绘制同样限定 `row.row_type in ("node", "layer")`（param 行该列点击落入普通选中逻辑）。
  - 置信度: 高

- [AUD-10307] [Suggestion] `_draw_solo`/`_draw_shy` 存在不可达分支（死代码）
  - 位置: src/features/composite/ui/keyframes_tree_view.py:822-825、837-843
  - 证据:
    ```python
        if not solo:
            return
        state = "checked" if solo else "normal"
        timeline_icons.draw_icon(p, "solo", rect, state)
    ```
    以及 `_draw_shy` 中 `if not shy: return` 之后的 `"collapse" if shy else "expand"`。
  - 影响: early-return 之后的条件分支永不为假，属死代码，误导后续维护者以为存在非激活态绘制路径。
  - 建议: 删除恒真条件，直接 `state = "checked"` / 固定 `"collapse"`。
  - 置信度: 高

- [AUD-10308] [Suggestion] 类型注解缺失：provider 安装器、事件参数与 row 参数均无类型
  - 位置: src/features/composite/ui/keyframes_tree_view.py:215、223、233、243、248、253、1549（`def set_expression_status_provider(self, provider) -> None:` 等）；1130/1191/1341/1467（`event` 裸参数）；733（`row` 裸参数）
  - 证据:
    ```python
        def set_expression_status_provider(self, provider) -> None:
    ```
    ```python
        def mousePressEvent(self, event) -> None:
    ```
  - 影响: 九维度之"类型注解缺失"。provider 回调签名（参数个数/返回类型）只存在于 docstring，IDE 与静态检查无法校验宿主实现是否匹配（历史上正是 eye 字段契约漂移的同类问题）。
  - 建议: 为 provider 参数标注 `Callable[[str, Optional[str]], Optional[str]]` 等具体类型，事件参数标注 `QMouseEvent`/`QContextMenuEvent`，行参数标注 `KeyframesRow`。
  - 置信度: 高

### 通过项
- 线程安全：纯 UI 线程自绘 QWidget，无渲染线程/OpenGL 交互，无跨线程共享状态。
- GPU 资源生命周期：不持有任何 ModernGL/纹理对象，不适用。
- GLSL·HDR 正确性：无着色器与像素数据处理，不适用。
- 信号防递归（核心路径正确）：`_sync_value_controls` 程序化回填使用 `blockSignals(True/False)`（535-537）；`set_selected_layer_id` 只改行状态不发信号；keyframe 编辑器 commit→面板→refresh 仅重定位不重设值，无环路。
- 状态提交一致性：视图只发意图信号（promote/attach/reorder/delete/commit 等），突变与 undo 边界归 KeyframesPanel/LayerStackController，分层正确。
- 滚动/几何同步：`_sync_value_controls`、`_sync_keyframe_editor`、`_position_header_time_display` 在 resize/scroll/refresh 均被调用，行列几何经 `timeline_layout.left_column_widths` 统一解析。
- 媒体拖放：MIME 白名单判定 + handler 异常时 `event.ignore()`，拒绝路径不误收（AUD-10304 仅针对其无日志）。
- 键盘路径：Escape 取消内联值编辑、Delete 删除选中 Layer 行有 row 存在性校验。

---

## src/features/composite/ui/material_panel.py (行数: 387)

### 发现

- [AUD-10309] [Suggestion] `_refresh_current` 的 `_guard` 复位不在 finally 中，异常路径将永久锁死面板编辑
  - 位置: src/features/composite/ui/material_panel.py:245-288
  - 证据:
    ```python
        values = self._current_values()
        self._guard = True
        try:
            color = values.get("base_color", [0.8, 0.8, 0.8])
            ...
        except (TypeError, ValueError):
            pass

        missing = []
        slots = item.get("slots", {})
        ...
        self._guard = False
    ```
  - 影响: try/except 只覆盖 250-255 的浮点转换且只捕 (TypeError, ValueError)；258-287 的槽位回填段在 guard=True 下无保护，`self._guard = False`（288）不在 finally 中。该区间任何其他异常会使 `_guard` 永久为 True，此后 `_on_value_changed`/`_on_slot_changed`/`_on_preset_changed` 全部静默失效且无提示。对照 `set_materials`（197-209）已正确使用 try/finally，本方法属遗漏。当前代码现实触发概率低，但结构性不满足"程序化更新控件须状态锁（且异常安全）"的要求。
  - 建议: 将 `_guard = False` 放入 finally，或将整个回填包进 try/finally。
  - 置信度: 高（结构性缺陷）/ 触发概率低

- [AUD-10310] [Suggestion] `decode_material_overrides` 返回的诊断码被静默丢弃
  - 位置: src/features/composite/ui/material_panel.py:194
  - 证据:
    ```python
        self._overrides, _ = decode_material_overrides(overrides)
    ```
    material_model.py:432-434 明确该返回值的用途：
    ```python
        The second return value contains stable diagnostic codes for invalid input;
        malformed entries are skipped instead of making a render fail.
    ```
  - 影响: 持久化的 material_overrides JSON 损坏或形状非法时，覆盖项被静默丢弃且用户在面板上得不到任何提示（只会看到"未覆盖"状态），违背"外部数据须验证并反馈"的意图——验证做了，结果没消费。
  - 建议: 接收诊断码并在 `_status_label` 显示计数/警告（如 "overrides 部分无效"），或上报宿主日志。
  - 置信度: 高

- [AUD-10311] [Suggestion] `_on_preset_changed` 程序化 `setCurrentIndex(0)` 未加状态锁，实际发生一次信号重入
  - 位置: src/features/composite/ui/material_panel.py:355-361
  - 证据:
    ```python
    def _on_preset_changed(self, _index: int) -> None:
        if self._guard:
            return
        preset = self._preset_combo.currentData()
        self._preset_combo.setCurrentIndex(0)
    ```
  - 影响: guard 为 False 时 `setCurrentIndex(0)` 同步发射 currentIndexChanged，重入 `_on_preset_changed(0)`；当前因索引 0 的 data 为 None 而在 `if preset is None` 处无害返回，但违反 .clinerules "程序化更新控件须状态锁防递归信号"，重入确实发生，未来在该函数尾部追加逻辑即成实际 bug。
  - 建议: 重置索引前置 `self._guard = True` / finally 复位，或改用 `blockSignals`。
  - 置信度: 高

- [AUD-10312] [Suggestion] `set_materials` 形参注解为 `object`，类型契约过弱
  - 位置: src/features/composite/ui/material_panel.py:189-190
  - 证据:
    ```python
    def set_materials(self, materials: object, overrides: object = None) -> None:
    ```
  - 影响: 九维度之"类型注解缺失"。运行时 isinstance 校验存在（191-193，好事），但注解未表达期望输入（如 `Sequence[Mapping[str, Any]]` / `Union[str, Mapping, None]`），静态检查无法发现传参错误。
  - 建议: 收紧注解与实际校验一致；顺带为返回元组的 decode 调用保留显式类型。
  - 置信度: 高

### 通过项
- 线程安全：UI-only adapter（注释声明并兑现），不触 OpenGL/`MaterialData` 实例，无跨线程访问。
- GPU 资源生命周期：不适用（无 GL 对象）。
- GLSL·HDR 正确性：不适用（PBR 参数 spin 范围 [0,1] 属 UI 输入约束，非内部 HDR 数据 clamp）。
- 信号防递归（主体正确）：`set_materials` 的 combo clear/setCurrentIndex 均在 try/finally 的 guard 内；`_refresh_current` 回填期间 guard 屏蔽 valueChanged/currentIndexChanged（异常安全性见 AUD-10309）。
- 状态提交一致性：面板只经 `overrides_changed` 发序列化文档，undo/revision 归 CompositePage，分层正确；`_reset_current` 走同一提交通道。
- 输入验证：materials 逐项 Mapping 过滤、mesh_count 兜底、base_color 形状/长度校验、slots/slot 子项 Mapping 校验、贴图路径 str() 规整。
- 异常路径：浮点转换 (TypeError, ValueError) 收窄捕获；`_browse_slot` 取消路径直接返回。

---

## src/features/composite/ui/minimap_widget.py (行数: 367)

### 发现

- [AUD-10313] [Suggestion] 全文件类型注解缺失（返回注解、事件参数、parent 参数）
  - 位置: src/features/composite/ui/minimap_widget.py:38-39、84-85、90、99、201、298、305、325、328、335
  - 证据:
    ```python
    def __init__(self, editor: QGraphicsView, parent=None):
    ```
    ```python
    def set_selected_node(self, node_id: str | None):
    def refresh_viewport(self):
    def update_from_editor(self):
    ```
    ```python
    def paintEvent(self, event):
    def leaveEvent(self, event):
    ```
  - 影响: 九维度之"类型注解缺失"。多数方法无返回注解；`parent`、多数事件 `event` 参数无类型（mousePress/Move/Release 已注 QMouseEvent，paint/leave 未注）；`_node_at` 返回注解存在但 `pos` 参数未注；同文件注解风格不统一。
  - 建议: 补齐 `-> None`、`parent: Optional[QWidget]`、`event: QPaintEvent/QEvent`，并统一导入 `from __future__ import annotations`。
  - 置信度: 高

### 通过项
- 线程安全：仅 UI 线程访问 QGraphicsView/scene，无跨线程状态。
- GPU 资源生命周期：不适用（无 GL 对象）。
- GLSL·HDR 正确性：不适用。
- 信号防递归：`navigate_requested` → 宿主 centerOn → `refresh_viewport()` 只 update()，无回环信号。
- 异常与取消路径：`update_from_editor` 采样端口位置时捕获 RuntimeError（C++ 对象已删除，123-134）正确；`_navigate_to` 在无数据/无效矩形时安全返回。
- 数值健壮性：`_compute_transform` 对无效场景矩形回退 scale=1.0，`_minimap_to_scene` 无除零路径；悬停索引越界在绘制侧仅做相等比较、不做列表索引，重建后安全。
- 性能：每帧 O(nodes+connections) 线性绘制、缓存变换，规模合理；折叠态提前返回。
- 布局：`_toggle_collapse` 同步 setFixedSize + 重定位按钮/自身，状态一致。
