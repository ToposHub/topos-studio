# 批次报告：batch-258 — src/features/color/ui/（Tracker/Viewer/Warper UI）

审查日期：2026-09-28 · 编号区间：AUD-25800–AUD-25899
文件：6 个，共 2,671 行。全部通读，跨文件调用点（canvas.py、color_controller.py、
main_window.py、color_warper.py、track_data.py/types.py）已只读核验。

汇总：Critical 0 · Major 5 · Suggestion 11

---

## src/features/color/ui/tracker_overlay.py (行数: 711)

### 发现

- [AUD-25801] [Major] 手动修正拖动过程无任何视觉反馈：`mouseMoveEvent` 计算的新区域是死存储，overlay 状态从未更新
  - 位置: src/features/color/ui/tracker_overlay.py:619
  - 证据:
```python
        region = self._dragged_region(dx, dy)
        # 拖动过程只更新 overlay，不提交模型
        self._sync_handles()
        self.update()
        event.accept()
        return
```
  - 影响: 注释声称"拖动过程只更新 overlay"，但 `region` 既未写入 `_homography_corners` 也未写入 `_current_transform`，`_sync_handles()/update()` 重绘的内容完全不变。非透视模式的"current"内部拖动与透视模式的"homography"内部拖动期间，目标框/十字不跟随鼠标，直到释放瞬间 `current_region_committed` 才提交，用户在无反馈状态下盲拖。与透视角点拖动（`_update_homography_drag` 会实时写 `_homography_corners`）行为不一致。
  - 建议: 在 move 分支把 `_dragged_region` 的结果写入一个临时显示状态（如 `_preview_current_region`，`_current_corners()` 优先读取），或在 homography 内部拖动时改走 `_homography_corners` 实时更新路径；同时删除死存储。
  - 置信度: 高

- [AUD-25802] [Major] 透视边中点拖动会移动已锁定的角点，锁定语义被绕过
  - 位置: src/features/color/ui/tracker_overlay.py:665-670
  - 证据:
```python
        else:
            # 边中点拖动：移动该边的两个端点，保持相邻角点关系。
            a, b = index, (index + 1) % 4
            corners[a] = (corners[a][0] + dx, corners[a][1] + dy)
            corners[b] = (corners[b][0] + dx, corners[b][1] + dy)
        self._homography_corners = corners
```
  - 影响: 角点拖动分支有 `if index in self._locked_homography_corners: return` 防护，`_hit_part` 也拒绝直接抓取锁定角点（tracker_overlay.py:543-544）；但边中点分支无任何锁定检查，拖动与锁定角点相邻的边中点仍会平移该锁定角点并在释放时随 `homography_corners_committed` 提交，锁定承诺（保持锚定平面角不动）被静默破坏。
  - 建议: 边中点分支中对锁定端点跳过位移（或按锁定端点做仿射/保持约束），至少在存在相邻锁定角点时禁用该边中点手柄。
  - 置信度: 中（代码事实确凿；是否属有意设计无注释佐证，但与既有锁定语义矛盾）

- [AUD-25803] [Suggestion] 拖动被中断时 `_drag`/`_manual_correction_active` 不复位，MANUAL_COLOR 高亮残留
  - 位置: src/features/color/ui/tracker_overlay.py:588-602, 356-361
  - 证据:
```python
            self.set_state(OverlayState.MANUAL_CORRECTION)
            self._manual_correction_active = True
            event.accept()
            return
```
  - 影响: 若拖动期间 overlay 被 `set_overlay_active(False)` 隐藏或图层被移除，Qt 不会补发 `mouseReleaseEvent`，`_drag` 残留、`_manual_correction_active` 恒为 True，`paint` 中当前帧颜色持续用 `MANUAL_COLOR`（紫色）而非 `ACTIVE_SHAPE_COLOR`，状态机停在 MANUAL_CORRECTION；下次重新显示后靠下一次 press/release 才能自愈。
  - 建议: 在 `set_overlay_active(False)`（以及可见性/场景移除变化）中取消进行中的拖动：清空 `_drag`、复位 `_manual_correction_active` 并回退状态机。
  - 置信度: 中

- [AUD-25804] [Suggestion] `transform_point` 对退化单应（w≈0）静默按 w=1 计算，产出无意义远点
  - 位置: src/features/color/ui/tracker_overlay.py:140-144
  - 证据:
```python
def transform_point(matrix: np.ndarray, x: float, y: float) -> Point:
    """单应性变换单个归一化点。"""
    v = matrix @ np.array([x, y, 1.0], dtype=np.float64)
    w_ = v[2] if abs(v[2]) > 1e-12 else 1.0
    return (float(v[0] / w_), float(v[1] / w_))
```
  - 影响: 退化矩阵（接近奇异）时返回未除 w 的原始坐标（可能远超 0-1 归一化范围），绘制出远离画布的巨型四边形且不提示退化；下游 `homography_corners()` 取到的也是失真角点。
  - 建议: w 接近 0 时返回 None 或抛出领域异常/标记退化，由调用方回退到上一有效矩阵。
  - 置信度: 低（边界场景，可能是有意的容错取舍）

- [AUD-25805] [Suggestion] `set_tracking_mode(mode: object)` 弱类型收窄为字符串，`_drag` 无结构注解
  - 位置: src/features/color/ui/tracker_overlay.py:262-265, 217
  - 证据:
```python
    def set_tracking_mode(self, mode: object) -> None:
        """设置 overlay 模式；接受字符串或 ``TrackingMode``。"""
        value = getattr(mode, "value", mode)
        self._tracking_mode = str(value).lower()
```
  - 影响: 任意输入都被字符串化吞掉，拼错的模式名（如 "perspectve"）不会在类型检查或运行期暴露，只会静默落入非透视分支；`_drag: Optional[dict]` 的键结构（kind/corner/start_region/start_norm/start_corners）仅靠约定。
  - 建议: 用 `str | TrackingMode` 联合类型并在非白名单值时校验；`_drag` 定义 TypedDict。
  - 置信度: 高

### 通过项
- 线程安全：所有数据 setter（set_features/set_path/set_failed_markers/set_current_transform 等）均由 UI 线程的 canvas.py 调用，无跨线程直接访问。
- GPU 资源生命周期：纯 QGraphicsItem 绘制，不涉及 OpenGL/ModernGL 资源。
- 信号防递归：拖动过程只更新内部状态、释放才 emit，符合"提交式"约定；`state_changed` 无环路。
- 输入验证：`set_homography_corners` 校验 4 点、`set_homography_corner_locked` 校验索引 0-3；`clamp_region` 归一化钳制正确。
- 性能模式：路径显示抽样上限 2000 且首尾点保留、原始数据不丢；paint 使用 cosmetic pen；命中测试 O(角点数)。
- 包含性校验 `search_contains_pattern` 带容差，`can_start_tracking` 语义正确。

---

## src/features/color/ui/tracker_panel.py (行数: 1058)

### 发现

- [AUD-25806] [Major] `set_params` 程序化更新控件未加信号锁，违反"状态锁防递归信号"铁律，导致参数处理器双重执行与中间不一致状态外泄
  - 位置: src/features/color/ui/tracker_panel.py:1040-1058
  - 证据:
```python
    def set_params(self, params: dict) -> None:
        if "mode" in params:
            idx = self.mode_combo.findData(params["mode"])
            if idx >= 0:
                self.mode_combo.setCurrentIndex(idx)
        ...
        if "min_confidence" in params:
            self.conf_slider.setValue(int(params["min_confidence"] * 100))
```
  - 影响: `mode_combo/quality_combo/feature_spin/search_spin/conf_slider/failure_combo` 全部连接 `_emit_params_changed`（tracker_panel.py:612-660）。程序化 `set_params` 每设一个控件就发一次 `params_changed`：① 实测调用点 color_controller.py:512 `set_params({"search_multiplier": ...})` 后，spin 的 valueChanged 先经 main_window.py:1619-1620 连接触发一次 `_on_tracker_params_changed`，紧接 controller 第 513 行又显式调用一次——同一变更被处理两遍（重复渲染/参数下发）；② 一次设置多个键时会发出多个"半新半旧"的参数快照，控制器可能基于混合状态启动跟踪。
  - 建议: `set_params` 全程对 6 个参数控件 `blockSignals(True/False)`（或引入 `_updating_programmatically` 状态锁），完成后按需手动 emit 一次。
  - 置信度: 高

- [AUD-25807] [Suggestion] `set_tracking_result` 跨类读取私有属性 `graph.view._reference_frame`
  - 位置: src/features/color/ui/tracker_panel.py:989-992
  - 证据:
```python
        binding = _ResultBinding(
            self._bound_shape or "Window 1",
            int(getattr(self.graph.view, "_reference_frame", 0)),
        )
```
  - 影响: 依赖 `_GraphView` 的私有字段名，重命名即静默退回 0（参考帧跳变），getattr 默认值掩盖破坏。
  - 建议: 在 TrackerGraph 上暴露公开的 `reference_frame()` 读接口。
  - 置信度: 高

- [AUD-25808] [Suggestion] 置信度滑块拖动时每个 tick 都广播 `params_changed`，无合流/防抖
  - 位置: src/features/color/ui/tracker_panel.py:649-651
  - 证据:
```python
        self.conf_slider.valueChanged.connect(
            lambda v: self.conf_value.setText(f"{v / 100.0:.2f}"))
        self.conf_slider.valueChanged.connect(self._emit_params_changed)
```
  - 影响: 拖动 0-100 滑块一次可产生数十次 `params_changed`；同类控件 ColorWarper 已用 16 ms 单发定时器合流（color_warper.py:23-26），本面板无等价机制，控制器若据此触发重跟踪/重渲染会造成抖动与冗余任务。
  - 建议: 参照 ColorWarper 的单发 QTimer 合流，或仅在 sliderReleased 时提交。
  - 置信度: 中（面板侧事实确凿，控制器侧放大程度未逐行核验）

- [AUD-25809] [Suggestion] `set_tracking_binding` 每次全量重建 series 并三重排序，长片段高频刷新时 O(n) 重复开销
  - 位置: src/features/color/ui/tracker_panel.py:926-955
  - 证据:
```python
        for frame in sorted(all_transforms):
            ...
        keyframes = sorted(all_transforms)
        manual_frames = sorted(manual)
        failed_frames = sorted(...)
```
  - 影响: 跟踪进行中每次进度回调都对全部帧重建 dict/list 并多次排序；数千帧素材上逐帧刷新为 O(n) 每次调用，整体退化为 O(n²) 级 UI 开销。
  - 建议: 排序结果复用（一次 sorted 得 keyframes，其余用推导）；增量追加新帧而非全量重建；必要时限制刷新频率。
  - 置信度: 中

### 通过项
- 线程安全：面板只在 UI 线程被调用；`set_tracking_binding` 文档明确异步结果由控制器在 UI 线程合并后回填。
- 信号防递归（除 AUD-25806 外）：`_on_dim_changed` 式回退、`segment` 按钮经 `_set_graph_mode` 直控 graph，无环。
- 状态机：`_apply_state` 对 RUNNING 态禁用全部参数/动作按钮、auto 按钮切换取消语义正确；`set_bound_shape(None)` 在 RUNNING 态不误清状态。
- 异常路径：`_trf` 对 format 失败有回退；`set_progress` 有 total>0 除零防护。
- 输入验证：`set_params` 用 findData 判 idx>=0，未知值不越界；`set_cloud_available`/`_refresh_cloud_combo` 禁用云端项而非假按钮。
- QSS 图标路径用 `as_posix()` 正斜杠（Windows 反斜杠会毁掉整张样式表），注释说明了取舍。

---

## src/features/color/ui/tracker_status.py (行数: 67)

### 发现

（无 Critical/Major/Suggestion 级发现；仅 `__init__(self, parent=None)` 的 parent 未注解，属可忽略级别，不占编号。）

### 通过项
- 状态灯颜色映射（STATE_LIGHT）与 PanelState 五态一一对应，无遗漏。
- `set_status` 用 QColor 相等性去重，避免无变化重绘。
- 绘制无资源分配，paintEvent 正常结束 painter（栈对象）。
- 通过（无逻辑风险）。

---

## src/features/color/ui/tracker_toolbar.py (行数: 178)

### 发现

- [AUD-25810] [Major] `set_target_names(None)` 必然 TypeError 崩溃：函数体对 falsy 输入自相矛盾
  - 位置: src/features/color/ui/tracker_toolbar.py:146-153
  - 证据:
```python
    def set_target_names(self, names: list[str]) -> None:
        current = self.target_combo.currentText()
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        self.target_combo.addItems(names or [tr("tracker.window_default", "Window 1")])
        if current in names:
            self.target_combo.setCurrentText(current)
        self.target_combo.blockSignals(False)
```
  - 影响: `addItems(names or [...])` 明确预期 `names` 可为 None/空，但随后 `current in names` 在 names=None 时抛 `TypeError: argument of type 'NoneType' is not iterable`，且发生在 blockSignals(True) 之后——异常逃逸时信号锁未释放，目标下拉后续程序化更新全部静默失效。当前 src 内无调用方（仅 `set_targets` 被 color_controller.py:295 使用），属潜伏地雷。
  - 建议: 统一入口：`names = list(names or [])` 后再判断；或用 try/finally 包裹 blockSignals 配对。
  - 置信度: 高

- [AUD-25811] [Suggestion] 点击已选中的工具按钮仍重复发射 `tool_changed`
  - 位置: src/features/color/ui/tracker_toolbar.py:122-126
  - 证据:
```python
    def _on_tool_clicked(self, tool_id: str) -> None:
        for tid, btn in self._tool_buttons.items():
            btn.setChecked(tid == tool_id)
        self._refresh_tool_styles()
        self.tool_changed.emit(tool_id)
```
  - 影响: checkable 按钮再次点击当前工具时状态未变，但信号照发；控制器若据此重置跟踪状态/重绑目标会造成无谓扰动。
  - 建议: 记录当前 tool_id，未变化时早退。
  - 置信度: 中

### 通过项
- 信号防递归：`_on_dim_changed` 3D 回退用 blockSignals 包裹 setCurrentIndex，正确防止二次触发；`set_targets` 填充期间全程 blockSignals，选中变化不回流。
- 输入验证：`set_targets` 对空 pairs 提供默认项；`findData` 判 index>=0。
- `current_target_id()` 对 currentData() 为 None 回退空串。
- 3D 摄像机/维度按范围约定显示禁用态并回退 2D，无假可用控件。

---

## src/features/color/ui/viewer_guides.py (行数: 540)

### 发现

- [AUD-25812] [Suggestion] 缩略图卡片 `mousePressEvent` 任意按键（右键/中键）都触发选择，且不检查事件内容
  - 位置: src/features/color/ui/viewer_guides.py:256-257
  - 证据:
```python
    def mousePressEvent(self, event: object) -> None:
        self.clicked.emit(self._guide.id)
```
  - 影响: 右键或中键按下同样切换构图参考并关闭弹出面板，绕过右键上下文菜单的常规预期；`event: object` 注解使事件类型信息丢失。
  - 建议: 判定 `event.button() == Qt.MouseButton.LeftButton` 后再 emit；注解改为 QMouseEvent。
  - 置信度: 高

- [AUD-25813] [Suggestion] `GuideOverlayItem.paint` 的 option 参数注解 `QStyle.QStyleOptionGraphicsItem` 不是有效类型
  - 位置: src/features/color/ui/viewer_guides.py:31, 124-129
  - 证据:
```python
    def paint(  # type: ignore[override]
        self,
        painter: QPainter,
        option: QStyle.QStyleOptionGraphicsItem,
        widget: Optional[QWidget],
    ) -> None:
```
  - 影响: PyQt6 中 QStyleOptionGraphicsItem 位于 QtWidgets 命名空间，`QStyle.` 下无此属性；仅因文件顶部 `from __future__ import annotations` 使注解未在运行期求值才未抛 AttributeError。静态检查与 IDE 均会误报/误导。
  - 建议: 直接从 QtWidgets 导入 QStyleOptionGraphicsItem 并用裸名注解。
  - 置信度: 中高

### 通过项
- GPU 资源：纯 QPainter 绘制，无 OpenGL 生命周期问题。
- 信号防递归：`set_active_guide` 程序化更新高亮不 emit；黑边关闭→burn-in 联动的发射序列经核对恰好一次（`_on_burn_in_toggled` 先触发后 return），无重复。
- 输入验证：`_guide_by_id(None)` 安全返回 None；ratio<=0（Default）早退不绘制。
- 坐标计算：letterbox 计算（ratio 与画面宽高比比较）正确，dim 区域用 QPainterPath 布尔差实现，无除零（area 尺寸为固定常量）。
- `boundingRect` 返回固定大矩形 + `setAcceptedMouseButtons(NoButton)`，不干扰画布事件。

---

## src/features/color/ui/warper_panel.py (行数: 117)

### 发现

- [AUD-25814] [Major] `set_params` 绕过 ColorWarper 的状态复位防护：未停 `_changed_timer`/清 `_pending_deltas`，陈旧拖动样本可在程序化更新后发布并覆盖新状态
  - 位置: src/features/color/ui/warper_panel.py:99-105
  - 证据:
```python
        self.warper.blockSignals(True)
        self.warper.grid_size = grid_data.shape[0]
        self.warper.points = np.zeros((self.warper.grid_size, self.warper.grid_size, 2), dtype=np.float32)
        for r in range(self.warper.grid_size):
            for t in range(self.warper.grid_size):
                self.warper.points[r, t] = self.warper._initial_position(r, t)
        self.warper.apply_deltas(grid_data)
```
  - 影响: ColorWarper 的 `reset_grid`（color_warper.py:68-75）注释明确说明"reset/网格变更必须丢弃等待中的 60Hz 合流拖动样本"并 stop `_changed_timer`、清 `_pending_deltas`；本方法手工复制了重建逻辑却漏掉这两步。若在拖动进行中（timer 16ms 窗口内）被 undo/预设恢复调用 set_params，待发定时器随后把旧拖动 deltas 经 `warper.changed` 发布（blockSignals 此时已解除），引擎收到陈旧网格，覆盖刚 hydrate 的正确状态。同时跨类访问私有 `_initial_position`、直接赋 `grid_size` 绕过 `set_grid_size`。
  - 建议: set_params 改为 `self.warper.set_grid_size(...)` + `apply_deltas(...)` 的组合（在 blockSignals 内调用 set_grid_size 即可复用其定时器清理与重建逻辑），删除手工复制段。
  - 置信度: 中（竞态窗口窄，但机制与 reset_grid 的既有修复意图对照确凿）

- [AUD-25815] [Suggestion] `set_params` 不校验网格尺寸合法性：非 6/8/12 时 combo 与实际网格脱节；`set_params(None)` 静默无操作
  - 位置: src/features/color/ui/warper_panel.py:96-110
  - 证据:
```python
            if self.warper.grid_size == 6: self.grid_combo.setCurrentIndex(0)
            elif self.warper.grid_size == 8: self.grid_combo.setCurrentIndex(1)
            elif self.warper.grid_size == 12: self.grid_combo.setCurrentIndex(2)
            self.grid_combo.blockSignals(False)
```
  - 影响: 外部数据（堆栈/持久化文件）若带 grid_size=16，warper 实际按 16x6 重建（shape[0]=16）而 combo 仍显示旧档位，UI 与引擎状态不一致；grid_data=None 时直接返回，界面残留旧网格也不发任何信号同步。
  - 建议: 对 shape[0] 做 {6,8,12} 白名单校验（非法值拒绝并记日志）；None 分支显式重置或至少同步 combo。
  - 置信度: 高

- [AUD-25816] [Suggestion] 全文件无类型注解，且 reset 按钮提示硬编码英文未走 i18n
  - 位置: src/features/color/ui/warper_panel.py:41, 74-117
  - 证据:
```python
        self.reset_btn.setToolTip("Reset Grid")
...
    def on_grid_size_changed(self, index):
        if index == 0:
            size = 6
```
  - 影响: `init_ui/on_grid_size_changed/on_reset/set_params/reset_all` 无参数与返回注解，与项目"所有函数有类型注解"约束不符；"Reset Grid" 绕过 I18N.tr，中文界面下混入英文提示（同文件其他文案均走 i18n）。
  - 建议: 补类型注解；tooltip 改用 `I18N.tr("warper.reset_grid", "Reset Grid")`。
  - 置信度: 高

### 通过项
- 信号防递归：`on_grid_size_changed`/`on_reset` 用 blockSignals 包裹 warper 的 set_grid_size/reset（两者内部都会 emit changed），正确阻止 identity 网格外泄；`set_params` 中 combo 更新亦被 blockSignals 包裹。
- 状态提交一致性（语义）：重置/换网格统一 emit None 通知引擎旁路 warper，语义清晰；reset_all 复用同一路径。
- GPU 资源：面板不触碰 OpenGL；grid 数据经信号传给引擎侧处理。
- 异常路径：combo index 到 size 的映射封闭（0/1/2 → 6/8/12）。

---

## 附注（跨文件核验记录，只读）
- tracker_panel.params_changed 连接：src/app/main_window.py:1619-1620。
- set_params 程序化调用点：src/app/controllers/color_controller.py:512-513（AUD-25806 的影响证据）。
- ColorWarper 定时器/复位内部：src/shared/ui/widgets/color_warper.py:23-31, 68-84, 247-280。
- 跟踪数据契约：src/shared/tracking/track_data.py、types.py（manual_corrections 为 TrackTransform，status=MANUAL、confidence=1.0，故 panel 统计逻辑自洽）。
