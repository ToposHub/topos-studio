# 批次审查报告：batch-099 [P1] src/features/composite/ui//

任务书：docs/audit/2026-09-28/assignments/batch-099.md
编号区间：AUD-9900–AUD-9999（本报告实际使用 AUD-9900–AUD-9922）
文件数：7（2,953 行），全部逐行 Read 通读。
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。

本批次为 Graph Editor（关键帧 Graph 模式）纯数据模型 + Qt 面板 + 节点图分组撤销命令。无 GPU/GLSL 面；核心风险集中在撤销命令的快照一致性（group_commands）、采样性能模式（graph_model）与宽泛异常回退（graph_model）。

汇总：Critical 0 · Major 6 · Suggestion 17。

---

## src/features/composite/ui/graph_expression_editor.py (行数: 322)

### 发现

- [AUD-9900] [Suggestion] 表达式开关切换无宿主回滚路径，宿主拒绝后按钮状态与节点状态脱钩
  - 位置: src/features/composite/ui/graph_expression_editor.py:290-299
  - 证据:
    ```python
    def _on_enabled_toggled(self, disabled: bool) -> None:
        """Broadcast an expression enable/disable intent to the host."""
        if self._node_id is None or self._param_name is None:
            return
        if not self._text_edit.toPlainText().strip():
            return
        self._expression_enabled = not bool(disabled)
        self._refresh_enabled_button(True, self._expression_enabled)
        self.expression_enabled_changed.emit(
            self._node_id, self._param_name, self._expression_enabled)
    ```
  - 影响: `_refresh_enabled_button` 在宿主校验/推送 undo 之前就已更新按钮与 `self._expression_enabled`。若宿主（面板/节点）拒绝该意图（校验失败、节点已删除、undo 记录失败），编辑器无任何回滚或错误回报通道，开关会停在错误状态直到下一次 `set_target` 重新绑定。
  - 建议: 增加宿主回写接口（如 `set_expression_enabled_state()`，由宿主在应用成功后调用），或在 `show_error` 之外为开关增加失败回滚；与文本提交路径的"宿主是 canonical owner"契约对齐。
  - 置信度: 中

- [AUD-9901] [Suggestion] `target()` 返回裸 `tuple`，缺少参数化类型注解
  - 位置: src/features/composite/ui/graph_expression_editor.py:272-274
  - 证据:
    ```python
    def target(self) -> tuple:
        """The (node_id, param_name) currently bound, or (None, None)."""
        return (self._node_id, self._param_name)
    ```
  - 影响: 调用方与类型检查器无法得知元素类型，文件其余部分均已强类型注解，此处不一致。
  - 建议: 改为 `Tuple[Optional[str], Optional[str]]` 并导入 `Tuple`。
  - 置信度: 高

### 通过项
- 信号防递归：`set_target`/`clear_target` 的 `_guard` 状态锁 + `finally` 恢复正确；`_refresh_enabled_button` 用 `blockSignals` 防递归正确。
- 异常与取消路径：Bug-3 focus-out 去重（`_last_committed_text`）实现与注释一致，切选不误提交；Ctrl/Cmd+Return 显式提交语义清晰。
- 线程安全：纯 UI 线程控件，无跨线程访问。
- GPU 资源生命周期：不涉及 OpenGL。
- 输入验证：`node_id`/`param_name` 为 None 时各路径早退正确。

---

## src/features/composite/ui/graph_layout.py (行数: 472)

### 发现

- [AUD-9902] [Suggestion] 未使用的导入 `QGraphicsItem`
  - 位置: src/features/composite/ui/graph_layout.py:8
  - 证据:
    ```python
    from PyQt6.QtWidgets import QGraphicsItem
    ```
  - 影响: 模块为纯算法（无 Qt 控件操作），该导入从未被引用，徒增 PyQt6 依赖面与 lint 噪声。
  - 建议: 删除该导入。
  - 置信度: 高

- [AUD-9903] [Suggestion] `auto_layout` 死变量 `node_ids` 与死代码 `layer_height`
  - 位置: src/features/composite/ui/graph_layout.py:52、81-82
  - 证据:
    ```python
    # 获取节点ID集合
    node_ids = {n.id for n in nodes}
    ...
            # 计算该层的最大高度
            layer_height = len(layer) * (self.NODE_HEIGHT + self.VERTICAL_GAP)
    ```
  - 影响: 两处计算结果均未被使用（每层 y 直接从 `start_y` 起排，无需 layer_height），误导读者以为有按层高度居中的逻辑。
  - 建议: 删除死代码，或补上注释说明每列均从 `start_y` 顶对齐的设计决定。
  - 置信度: 高

- [AUD-9904] [Suggestion] `cleanup_overlaps` 50 次尝试上限后静默保留重叠
  - 位置: src/features/composite/ui/graph_layout.py:289-304
  - 证据:
    ```python
            attempts = 0
            while self._has_overlap(node_rect, occupied_regions):
                # 向下偏移
                y += self.NODE_HEIGHT + self.VERTICAL_GAP
                ...
                attempts += 1
                if attempts > 50:  # 防止无限循环
                    break

            new_positions[node.id] = (x, y)
            occupied_regions.append(node_rect)
    ```
  - 影响: 触发上限 break 时该节点仍与已占用区域重叠，且其（仍重叠的）矩形被加入 `occupied_regions`，后续节点基于错误占位继续计算；结果无任何告警。
  - 建议: break 后至少记录日志/返回标记，或改用按已占区域求首个空档的确定性搜索。
  - 置信度: 中

### 通过项
- 输入验证：所有公开方法对空列表/少于 2 个节点早退，`min(..., default=0.0)` 防空序列。
- 异常与取消路径：`_topological_sort` 检测到循环（无输入节点）返回空并走 `_fallback_layering`，BFS 中"未分配节点放最后一层"兜底孤立/环节点。
- 线程安全：纯算法无共享可变状态（对输入 `layer.sort` 仅修改局部列表）。
- 性能模式：`_has_overlap` O(n²) 上限对节点图规模可接受。
- 依赖 API：`NodeGraph.get_input_connections`/`get_node` 存在（node_engine.py:1252/600），调用签名匹配。

---

## src/features/composite/ui/graph_model.py (行数: 958)

### 发现

- [AUD-9905] [Major] 曲线采样先按关键帧全跨度生成、最后才裁剪到可视区，成本随数据跨度而非视口缩放
  - 位置: src/features/composite/ui/graph_model.py:383、400-405、418-427、429-434（`sample_value_curve`）；565、580-588（`sample_speed_curve`）
  - 证据:
    ```python
    step_frames = max(0.25, 3.0 / max(1e-9, pixels_per_frame))
    ...
        f = k0.frame
        while f < k1.frame:
            samples.append((f, k0.value))
            f += step_frames
    ...
    # Clip to the visible range (+one keyframe span of slack per plan §22.5).
    if frame_end > frame_start:
        lo = frame_start
        hi = frame_end
        samples = [(f, v) for f, v in samples if lo <= f <= hi]
    ```
  - 影响: 步长只被钳到 ≥0.25 帧，循环边界是关键帧全跨度而非 [frame_start, frame_end]。长项目（如 24fps 数小时素材 ≈ 10^5 帧）在极端缩放下每段生成多达 跨度/0.25 ≈ 4×10^5 个 (float, float) 元组，裁剪后才丢弃；多序列 × 多段叠加可达数百 MB 临时分配与 GC 压力，面板滚动/缩放出现可感卡顿，违背项目 4K 实时性能基线对交互面的要求。
  - 建议: 将采样循环的起点/终点钳到 `max(k0.frame, frame_start)` / `min(k1.frame, frame_end)`（端点样本仍强制包含），边采样边裁剪，去掉事后再过滤。
  - 置信度: 中

- [AUD-9906] [Major] 表达式求值器不可用时宽泛 `except Exception` 静默回退且 `has_error=False`，误导用户以为表达式生效
  - 位置: src/features/composite/ui/graph_model.py:793-800（另见 808-817 两处宽泛 except）
  - 证据:
    ```python
    if parser is None:
        try:
            from src.features.composite.utils.expression_parser import \
                ExpressionParser
            parser = ExpressionParser()
        except Exception:
            # Evaluator unavailable — fall back, no error flag (silent).
            return pre, list(pre), False
    ```
  - 影响: 求值器导入/构造失败（依赖缺失、初始化 bug）时，"表达式后曲线"直接等于前置曲线且不置 `expression_error`，视图不会画任何错误标记——用户写了表达式却看到一条与未写表达式完全一致且"无错"的曲线，属于被掩盖的领域错误（违背铁律"不得宽泛 except 掩盖领域错误"）。808-817 对 context 字段更新 `except Exception: pass`、对 evaluate `except Exception: result = None` 同样吞掉非预期异常类型。
  - 建议: 求值器不可用应显式置 `has_error=True`（或独立 `evaluator_unavailable` 标记）并记录日志，让视图给出可见提示；对 evaluate 的 except 收窄为求值器文档声明的异常类型。
  - 置信度: 中

- [AUD-9907] [Major] `build_graph_series` 对 frame/value 的 `float()` 无防护，损坏的工程数据可令异常逃逸到 UI 构建路径
  - 位置: src/features/composite/ui/graph_model.py:874-879
  - 证据:
    ```python
    for idx, kf in enumerate(kfs):
        if not isinstance(kf, dict):
            continue
        frame = float(kf.get("frame", 0))
        value = float(kf.get("value", 0.0))
        interp = str(kf.get("interpolation", "linear"))
    ```
  - 影响: `node.keyframe_tracks` 是工程 JSON 反序列化的原始 dict（上游 `Keyframe.from_dict` 同样不校验类型，见 src/shared/keyframe.py:94-108）。`frame`/`value` 为 None 或非数字字符串时 `float()` 抛 TypeError/ValueError，且未被捕获——同函数对 `_expressions`、`keyframe_tracks`、`parameters` 的读取均有 try/except（853-862、857-862、915-921），唯独此处裸奔，异常会沿 `_refresh_graph_editor` 逃逸，违反铁律"外部数据须验证"。
  - 建议: 循环体套 try/except（与同函数既有防御风格一致）跳过畸形关键帧，或提取带校验的 `_safe_float` 辅助函数。
  - 置信度: 中

- [AUD-9908] [Suggestion] `compute_speeds` 返回按帧排序后的顺序，与 docstring 承诺的"与 points 同长度同顺序"不符
  - 位置: src/features/composite/ui/graph_model.py:514-527
  - 证据:
    ```python
    Returns a list aligned with ``points`` (same length, same order).
    """
    if not points:
        return []
    ks = sorted(points, key=lambda p: p.frame)
    speeds: List[float] = []
    for i, k in enumerate(ks):
    ```
  - 影响: 输入未按帧排序时，速度值会错配到错误的关键帧上。当前 src/ 内无调用方（仅导出 + 测试均传有序点，见 tests/composite/test_graph_model.py:386-406），属公共 API 合同违背的潜伏风险。
  - 建议: 要么按输入顺序返回（记录排序后索引再映射回去），要么修改 docstring 明确"返回按帧升序对齐排序后 points"。
  - 置信度: 高

- [AUD-9909] [Suggestion] `_to_authoritative_keyframes` 将 float 帧取整为 int，亚帧关键帧预览漂移、相邻帧可退化为同帧
  - 位置: src/features/composite/ui/graph_model.py:461-478
  - 证据:
    ```python
    kf0 = Keyframe(
        frame=int(round(k0.frame)),
        value=float(k0.value),
    ```
  - 影响: 采样循环以原始 float 帧推进（`f = k0.frame + step_frames`），但权威求值使用取整后的帧：亚帧关键帧（GraphPoint.frame 为 float，公共 API 允许）会有轻微预览偏移；两相邻关键帧 round 到同一 int 时 df=0，`_sample_segment_value` 恒返回 kf0.value（`bezier_value_at_frame` 对 df<=0 也返回 v0，已验证 src/shared/keyframe.py:926-928，不致崩溃，但曲线段被压平）。仅影响编辑器预览，不影响渲染权威路径。
  - 建议: 权威 Keyframe 支持float 帧前，先对退化段（round 后同帧）直接线性绘制或跳过权威委托；并在注释中记录亚帧精度限制。
  - 置信度: 中

- [AUD-9910] [Suggestion] 若干内部函数类型注解缺失
  - 位置: src/features/composite/ui/graph_model.py:437-438（`_interp_type_for` 无返回注解）、452-453（`_to_authoritative_keyframes` 无返回注解）、481-483（`_sample_segment_value` 的 kf0/kf1/函数参数未注解）
  - 证据:
    ```python
    def _interp_type_for(seg_interp: str):
    ...
    def _sample_segment_value(frame: float, kf0, kf1, seg_interp: str,
                              bezier_value_at_frame,
                              ease_in_fn, ease_out_fn, ease_in_out_fn) -> float:
    ```
  - 影响: 模块自称"fully unit-testable headlessly"且整体强类型，这几处内部辅助函数未标注 Keyframe/插值函数类型，检查器无法验证委托契约。
  - 建议: 补 `-> InterpolationType`、`-> Tuple[Keyframe, Keyframe]`，kf 参数标 `"Keyframe"`、插值参数标 `Callable[..., float]`。
  - 置信度: 高

### 通过项
- HDR 正确性：`_sample_segment_value` 注释与实现一致——不做 clamp(0,1)，图上显示真实越界曲线（渲染期 clamp 归渲染层），符合 HDR 铁律。
- 输入验证（多数）：`value_to_y`/`x_to_frame` 等坐标变换均有 `max(1e-9, ...)` 除零防护；`compute_value_bounds` 过滤非数值并对单值给可见带；handle 长度校验 `len(hl) == 2`。
- 状态提交一致性：`apply_move_transform`/`apply_scale_transform` 不变更输入点、返回提案列表供调用方快照/回滚（Esc 路径），帧钳 ≥0 符合"负帧禁止"。
- 线程安全：纯数据 + 无共享可变全局，头less 可测。
- 性能模式：采样密度按像素自适应（~3px/样本）并有步长下限；`_interp_type_for` 内联导入避免模块级循环依赖。

---

## src/features/composite/ui/graph_navigation.py (行数: 213)

### 发现

- [AUD-9911] [Suggestion] `_normalise_viewport` 防 NaN 未防 ±inf，inf 缩放可进入书签并持久化为非标准 JSON
  - 位置: src/features/composite/ui/graph_navigation.py:25-36
  - 证据:
    ```python
    def number(name: str, default: float) -> float:
        try:
            value = float(viewport.get(name, default))
        except (TypeError, ValueError):
            return default
        return value if value == value else default

    return {
        "zoom": max(0.05, number("zoom", 1.0)),
    ```
  - 影响: `float("1e999")` 或 Python json 可解析的 `Infinity`（json.loads 默认接受 Infinity/NaN）会通过 `value == value` 检查（inf==inf 为 True）与 `max(0.05, inf)`，得到 zoom=inf 的书签；`to_dict` 再把它写回工程文件，`json.dumps` 输出 `Infinity` 非法严格 JSON，跨工具/再解析链路存在风险。NaN 被防住了，±inf 没有。
  - 建议: 用 `math.isfinite(value)` 一并拒绝 inf（回退默认或钳到上限，如 zoom ≤ 64）。
  - 置信度: 中

### 通过项
- 输入验证：`from_dict` 对 dict/list/tuple/嵌套 bookmarks 多形态防御解析，畸形条目逐条跳过；空名/非 str 名剔除；`add_bookmark` 拒绝空名并 raise ValueError（显式领域错误）。
- 状态提交一致性：会话历史与可持久化书签职责分离正确（to_dict 只序列化书签，历史不进工程负载）；`record_view` 截断前向分支 + 上限裁剪 + 去重，边界（index=-1、首条）正确。
- 线程安全：无 Qt 类型（模块头声明刻意如此），纯数据可 headless 测试。
- frozen dataclass + `to_dict` 浅拷贝 dict(self.viewport) 语义安全（值均为标量）。

---

## src/features/composite/ui/graph_node_search_popup.py (行数: 476)

### 发现

- [AUD-9912] [Suggestion] 帮助文本与 Shift+Enter 实际行为相反
  - 位置: src/features/composite/ui/graph_node_search_popup.py:164-166（文本）vs 413-415（行为）
  - 证据:
    ```python
    help_label = QLabel(
        "↑↓ Navigate  •  Enter Jump  •  Shift+Enter Jump & Close  •  Esc Close"
    )
    ```
    ```python
    # Check modifiers: Shift+Enter keeps popup open
    if not (self._search_input.modifiers() & Qt.KeyboardModifier.ShiftModifier):
        self.accept()
    ```
  - 影响: 界面告诉用户 "Shift+Enter Jump & Close"，代码实现（及其注释）却是 Shift+Enter 保持弹窗打开、普通 Enter 才关闭。用户按提示操作会得到相反结果。
  - 建议: 二选一对齐——改帮助文本为 "Enter Jump & Close • Shift+Enter Jump & Keep Open"，或改代码让 Shift 关闭。
  - 置信度: 高

- [AUD-9913] [Suggestion] `keyPressEvent` 中 Ctrl+Up/Ctrl+Down 分支不可达（死代码）
  - 位置: src/features/composite/ui/graph_node_search_popup.py:446-460
  - 证据:
    ```python
        if key == Qt.Key.Key_Up:
            self._navigate_previous()
            return
        if key == Qt.Key.Key_Down:
            self._navigate_next()
            return

        # Ctrl+Up/Ctrl+Down: cycle through all results (like search history)
        if mods & Qt.KeyboardModifier.ControlModifier:
            if key == Qt.Key.Key_Up:
                self._navigate_previous()
                return
    ```
  - 影响: Up/Down 在无修饰符分支已无条件 return，Ctrl+Up/Ctrl+Down 分支永远不会执行；且 `mods` 变量因此仅在死代码中使用。
  - 建议: 删除不可达分支，或若本意是"无修饰符交给列表控件选择、Ctrl 才循环"，需重排条件顺序。
  - 置信度: 高

- [AUD-9914] [Suggestion] 全部 UI 字符串硬编码英文，未走 I18N
  - 位置: src/features/composite/ui/graph_node_search_popup.py:100、119-121、139、147、154、164-166
  - 证据:
    ```python
    self.setWindowTitle("Search in Graph")
    ...
    self._search_input.setPlaceholderText(
        "Search nodes by name, type, or parameters... (e.g., 'blur', 'grade.gain', 'output')"
    )
    ...
    self._result_count_label = QLabel("0 results")
    ```
  - 影响: 同目录 graph_view.py / graph_expression_editor.py 均使用 `I18N.tr(...)`，此文件完全缺失 i18n，非英文 locale 下界面语言混杂，违反项目 UI 面板需支持国际化的约定。
  - 建议: 将窗口标题、占位符、按钮（Prev/Next）、计数与帮助文案迁到 `I18N.tr` 并补 key。
  - 置信度: 高

- [AUD-9915] [Suggestion] `"NodeItem"`/`"NodeGraph"` 前向引用从未导入（无 TYPE_CHECKING 块），事件处理参数缺类型注解
  - 位置: src/features/composite/ui/graph_node_search_popup.py:38-39、428、467、473
  - 证据:
    ```python
    def __init__(
        self,
        node_items: Dict[str, "NodeItem"],
        node_graph: "NodeGraph",
    ```
    ```python
    def keyPressEvent(self, event) -> None:
    ```
  - 影响: 引用的两个类型在本模块任何位置都没有 import（连 TYPE_CHECKING 均无），静态检查无法解析；`keyPressEvent`/`showEvent`/`closeEvent` 的 event 参数未注解，与项目强类型要求不一致。
  - 建议: 增加 `from typing import TYPE_CHECKING` + 受保护导入（node_item / node_engine），事件参数注解为 `QKeyEvent`/`QShowEvent`/`QCloseEvent`。
  - 置信度: 高

### 通过项
- 信号防递归：搜索输入 `textChanged` → 重建列表 → `setCurrentRow(0)` → `currentRowChanged` → `_on_selection_changed` 无环路（导航回调不回写搜索框）；`_populate_results` 重绑 `_matched_node_ids` 避免与 clear() 混用。
- 异常与取消路径：Esc 走 reject 且先清高亮；closeEvent 同样清高亮，双出口一致。
- 输入验证：`_build_searchable_data` 跳过失效 node（get_node 为 None）、None 参数值跳过；过滤文本统一 lower/strip。
- 性能模式：可搜索数据在 __init__ 一次性预构建（含 combined 串），键入过滤仅做子串匹配，代价 O(节点×参数) 每键可接受；结果列表 UniformItemSizes。
- 依赖 API：`NodeItem.node_title` 存在（node_item.py:92），`NodeGraph.get_node` 存在（node_engine.py:600）。

---

## src/features/composite/ui/graph_view.py (行数: 198)

### 发现

- [AUD-9916] [Suggestion] `set_graph_view(None)` 重置回占位页时旧 placeholder 移出布局后未销毁（泄漏/潜在重叠）
  - 位置: src/features/composite/ui/graph_view.py:105-112（对照安装路径 114-118）
  - 证据:
    ```python
    if graph_view is None:
        self._stack.removeWidget(self._placeholder) \
            if self._placeholder is not None else None
        self._placeholder = self._build_placeholder()
        self._stack.insertWidget(1, self._placeholder)
        if self._view_mode == self.GRAPH:
            self._stack.setCurrentIndex(1)
        return
    # Replace the placeholder at index 1 with the real canvas.
    if self._placeholder is not None:
        self._stack.removeWidget(self._placeholder)
        self._placeholder.setParent(None)
        self._placeholder.deleteLater()
    ```
  - 影响: `removeWidget` 不删除控件；None 分支缺少安装路径具备的 `setParent(None)` + `deleteLater()`。反复调用会累积残留 QLabel 子控件；若在 GRAPH 模式下调用（旧 placeholder 是当前可见页），移出布局后仍可能以旧 geometry 显示并与新 placeholder 重叠。当前 src/ 内无调用方传 None（keyframes_panel.py:840 仅安装真视图一次），故今日无实际损害，属文档化 API（"Passing None reverts to the placeholder"）的破损路径。
  - 建议: None 分支复用与安装路径相同的销毁三行；顺带把表达式语句 `removeWidget(...) if ... else None` 改为常规 if（当前写法可读性差）。
  - 置信度: 高（代码事实确凿；当前无调用方故降为 Suggestion）

- [AUD-9917] [Suggestion] Graph 画布信号未按类 docstring 承诺转发到宿主信号面
  - 位置: src/features/composite/ui/graph_view.py:40-44（承诺）、151-174（仅转发 timeline）、96-122（set_graph_view 不连线）
  - 证据:
    ```python
    """Stacked container swapping Timeline ↔ Graph canvases.

    Signals are re-emitted from whichever canvas is current, ...
    ```
    ```python
    tv = self._timeline_view
    # Be defensive: some timeline-view builds may not expose every
    # signal. Connect only what exists.
    for sig_name, host_sig in (
            ("current_frame_changed", self.current_frame_changed),
    ```
  - 影响: `set_graph_view` 安装真画布后未把 graph_view 的同名信号连到宿主信号——"面板只连宿主一次"的设计承诺在 Graph 模式下失效（宿主的 current_frame_changed/vertical_scroll_changed 等在 graph 模式不再反映画布事件）。现面板通过直连 `_graph_view` 信号绕过了此缺口（keyframes_panel.py:834-835、862-866），且 `_forward_timeline_signals` 的 try/except pass 宽泛但仅防御性连线，故为契约与实现不一致而非现行功能故障。
  - 建议: 在 `set_graph_view` 中对 graph_view 复用同一转发表；或修正 docstring/注释，明确宿主信号仅覆盖 Timeline、Graph 画布信号由面板直连。
  - 置信度: 中

### 通过项
- 状态提交一致性：`set_view_mode` 不触碰共享模型/选中/当前帧（符合 §22.1 "模式切换不销毁 shared model"），未知模式与重复模式 no-op。
- 信号防递归：无会回写的控件状态，纯转发/分发。
- 异常与取消路径：`_forward_timeline_signals` 对缺失信号 hasattr 防御、连线失败不致命；`refresh`/`set_current_frame` 对缺方法控件回退 `update()`/跳过。
- 线程安全：纯 UI 线程。

---

## src/features/composite/ui/group_commands.py (行数: 314)

### 发现

- [AUD-9918] [Major] `UpdateGroupCommand` 浅快照 + 原位 `update`：redo 直接污染 `_old_data`，第二次 undo 恢复的是新值
  - 位置: src/features/composite/ui/group_commands.py:148-162
  - 证据:
    ```python
    def redo(self):
        """Update group properties."""
        if hasattr(self.graph, '_node_groups') and self.group_id in self.graph._node_groups:
            if self._old_data is None:
                self._old_data = dict(self.graph._node_groups[self.group_id])
            self.graph._node_groups[self.group_id].update(self.new_data)

    def undo(self):
        """Restore old group properties."""
        if hasattr(self.graph, '_node_groups') and self.group_id in self.graph._node_groups and self._old_data:
            self.graph._node_groups[self.group_id] = self._old_data
    ```
  - 影响: undo 后 `graph._node_groups[group_id]` 与 `_old_data` 是同一对象；redo 的 `.update()` 原位改写该对象 → 快照被污染 → 再次 undo"恢复"的已是新值（且 `_old_data is None` 只在首次 redo 捕获，无法自愈）。Update→undo→redo→undo 序列后旧属性永久丢失，撤销历史对组属性失效。
  - 建议: `_old_data` 用 `copy.deepcopy` 捕获，且 undo/redo 均以"整体替换深拷贝"而非原位 update 实现互不共享的快照。
  - 置信度: 高

- [AUD-9919] [Major] `UngroupCommand` 浅拷贝与 `AddToGroupCommand` 原位修改共享同一 `node_ids` 列表，undo/redo 交错序列丢失组成员
  - 位置: src/features/composite/ui/group_commands.py:70-81（浅拷贝）、95-109（原位 append/remove）
  - 证据:
    ```python
    def redo(self) -> Optional[Dict]:
        if hasattr(self.graph, '_node_groups') and self.group_id in self.graph._node_groups:
            self._group_data = self.graph._node_groups[self.group_id].copy()
            del self.graph._node_groups[self.group_id]
    ```
    ```python
    def undo(self):
        ...
            group = self.graph._node_groups[self.group_id]
            for nid in self.node_ids:
                if nid not in group['node_ids']:
                    group['node_ids'].append(nid)
    ```
  - 影响: dict.copy() 是浅拷贝，`_group_data['node_ids']` 与组内列表同一对象。序列 [Add A] → [Ungroup] → undo(Ungroup) → undo(Add A)：undo(Add) 从恢复后的组里 remove A 时同步改写了 Ungroup 快照中的列表；此后 redo/undo(Ungroup) 恢复的组永远缺 A——撤销历史导航静默丢失组成员（RemoveFromGroupCommand 103-109/123-129 同构风险）。
  - 建议: 快照处改为 `copy.deepcopy`；或所有命令对 `node_ids` 的修改改为"构建新列表整体替换"而非 append/remove 原位变更。
  - 置信度: 中

- [AUD-9920] [Major] 组操作全部绕过 UndoStack：命令对象 `redo()` 后即被丢弃，分组/解组/加组不可撤销
  - 位置: src/features/composite/ui/group_commands.py:13-15（模块用法契约）；实际调用方 src/features/composite/ui/node_editor_actions.py:1046、1072-1073、1093-1094（直接 redo 后丢弃）
  - 证据:
    ```python
    Usage:
        from .group_commands import CreateGroupCommand, UngroupCommand, EnterGroupCommand

        # Create a group from selected nodes
        cmd = CreateGroupCommand(graph, selected_node_ids, group_id, group_data)
        undo_stack.push(cmd)
    ```
    ```python
    cmd = AddToGroupCommand(self._graph, group_id, node_ids)
    cmd.redo()
    ```
  - 影响: 页面有真实撤销系统（composite_page.py:319 `self._undo_stack = UndoStack(self)`，增删/连线/移动均入栈），但组操作走 `create_group_from_selection`（helper 内部 line 265-266 同样直接 `cmd.redo()`）或调用方直接 redo 后丢弃命令对象——模块声称的 undo 集成从未发生。用户 Ctrl+Z 无法撤销分组/解组；且因组变更绕过历史记录，撤销后续无关操作得到的图状态与组状态可能不一致。
  - 建议: 调用方改为把命令包成 UndoAction 推入页面 UndoStack（node_graph_commands.py 同模式的命令已如此接入）；`create_group_from_selection` 应返回命令对象（或接受 stack 参数）而非自行 redo。
  - 置信度: 高

- [AUD-9921] [Suggestion] `create_group_from_selection` 用 8 字符 uuid 片段作 group_id，碰撞时静默覆盖现有组
  - 位置: src/features/composite/ui/group_commands.py:247、46-50
  - 证据:
    ```python
    group_id = str(uuid.uuid4())[:8]
    ...
    def redo(self) -> str:
        ...
        self.graph._node_groups[self.group_id] = {
            'id': self.group_id,
    ```
  - 影响: 32 bit 熵的 id 在组数量增长/工程长期编辑下有非零碰撞概率；`_node_groups[group_id] = {...` 直接赋值会静默覆盖同 id 旧组（其节点归属丢失且无告警）。
  - 建议: 用完整 uuid4 字符串，或赋值前检查 id 已存在时重新生成。
  - 置信度: 低

- [AUD-9922] [Suggestion] 多处 `undo()` 及个别字段缺类型注解
  - 位置: src/features/composite/ui/group_commands.py:53、78、103、131、159、192、225（`def undo(self):`）；70/95/123/152/180/212（redo 返回类型不一：`Optional[Dict]`/无注解）
  - 证据:
    ```python
    def undo(self):
        """Remove the group."""
    ```
  - 影响: 撤销命令是状态提交一致性关键路径，返回值无注解（部分返回 bool/Dict/None 不统一）使调用方难以静态确认契约；与项目"所有函数有类型注解"约束不符。
  - 建议: 统一 `def undo(self) -> None:`，并规整 redo 的返回类型（命令模式通常不需要返回值，改为 None + 查询接口）。
  - 置信度: 高

### 通过项
- Enter/Exit 命令的 `_was_expanded` 在每次 redo 重捕获"变更前值"，toggle 式 undo 语义在 redo↔undo 往返下正确（166-227）。
- 输入验证（读取侧）：`get_group_for_node`/`get_nodes_in_group`/`get_all_groups` 对 `_node_groups` 缺失/组不存在均有 hasattr 与成员检查回退；`NodeGraph._node_groups` 确实在 node_engine.py:426 声明并在 2115-2118/2189 序列化/加载，防御性 hasattr 不掩盖真实缺属性错误。
- 线程安全 / GPU / GLSL·HDR：不涉及（纯数据命令，无渲染面）。
- `create_group_from_selection` 的包围盒计算对失效节点过滤并给默认 padding，position 兜底 (0.0, 0.0)。

---

## 批次小结

- Critical：0。
- Major：6 —— AUD-9905（采样全跨度生成后裁剪）、AUD-9906（求值器不可用静默回退无错误标记）、AUD-9907（关键帧 frame/value 裸 float() 无防护）、AUD-9918（UpdateGroupCommand 快照自污染）、AUD-9919（Ungroup/Add 浅拷贝共享列表丢成员）、AUD-9920（组操作绕过 UndoStack 不可撤销）。
- Suggestion：17。
- 维度覆盖说明：本批次无 GLSL/HDR 数值处理错误（graph_model 的不-clamp 策略正确），无 UI 线程外操作 OpenGL、无渲染线程交互；风险集中在撤销命令快照一致性（group_commands 三连）、性能模式（先生成后裁剪）与异常回退掩盖（graph_model）。
