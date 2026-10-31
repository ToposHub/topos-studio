# batch-208 审查报告 — src/features/edit/ui（scene cut UI 三件套）

- 审查日期: 2026-09-28
- 任务书: docs/audit/2026-09-28/assignments/batch-208.md
- 编号区间: AUD-20800–AUD-20899
- 文件: scene_cut_result_model.py (1,110) / scene_cut_shortcuts.py (98) / scene_cut_timeline_overlay.py (897)
- 汇总: Critical 0 / Major 6 / Suggestion 13
- 交叉验证说明: 已对照 `src/features/edit/core/scene_cut/models.py`（字段与属性）、`src/features/edit/ui/scene_cut_dialog.py`（消费者）、`src/features/edit/ui/timeline_view.py`（overlay 接线）、`src/features/edit/ui/timeline_items.py`（同类 item 的 prepareGeometryChange 模式）、`src/shared/ui/shortcut_manager.py`（API 签名）确认发现，未猜测。

---

## src/features/edit/ui/scene_cut_result_model.py (行数: 1,110)

### 发现

- [AUD-20800] [Major] Start TC 时间码永远按硬编码 24fps 格式化，非 24fps 项目显示错误且随时长漂移
  - 位置: src/features/edit/ui/scene_cut_result_model.py:675、:800
  - 证据:
    ```python
    def _format_timecode(self, seconds: float, fps: float = 24.0) -> str:
        ...
        total_frames = int(seconds * fps + 0.5)
        hours = total_frames // (3600 * int(fps))
    ```
    ```python
    elif col == Column.TIMECODE.value:
        return self._format_timecode(candidate.seconds)
    ```
    模型没有 set_fps/update_fps 入口；而消费者 scene_cut_dialog.py 持有真实的 `_playback_fps` 却无法传入。
  - 影响: 25/30/29.97/60fps 媒体的 Start TC 列全部错误，误差随时间累积；且 `int(fps)` 对 29.97 截断为 29，即使将来传入真实 fps，小时/分钟分桶与 total_frames 计算仍不一致。
  - 建议: 增加 `set_fps(fps)`（或构造参数），data() 中传入真实时间线帧率；分桶统一使用同一份（不截断的）fps。
  - 置信度: 高

- [AUD-20801] [Suggestion] set_decision 的 dataChanged 只覆盖 DECISION 单列与 [Display, Background] 角色，Scene 列与 ForegroundRole 不会刷新
  - 位置: src/features/edit/ui/scene_cut_result_model.py:397-401
  - 证据:
    ```python
    self.dataChanged.emit(
        self.index(index, Column.DECISION.value),
        self.index(index, Column.DECISION.value),
        [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.BackgroundRole]
    )
    ```
    而 `_calculate_scene_number`（:664-671）依赖"位于其前的 accepted 数"，`_get_foreground_color`（:983-984）依赖本行 decision。
  - 影响: 任何不经全量重绘的消费者（该模型是 `__init__.py` 导出的通用组件）在决策变化后会看到后续行 Scene 编号过期、Rejected 行文字颜色不更新。当前 scene_cut_dialog.py:2357 用 `self._scene_list.update()` 全量重绘掩盖了此问题。
  - 建议: dataChanged 覆盖整行（SCENE..KIND 列）并追加 ForegroundRole；或决策变化时对受影响行区间发通知。
  - 置信度: 高（机制确定；实际影响依赖消费者）

- [AUD-20802] [Suggestion] set_candidates 直接持有调用方列表，未做防御性拷贝，也未维持 source_frame 唯一不变量
  - 位置: src/features/edit/ui/scene_cut_result_model.py:253-255
  - 证据:
    ```python
    self.beginResetModel()
    self._candidates = candidates
    self._analysis = None
    ```
    对比 set_analysis（:231）使用 `list(analysis.candidates)`；insert_candidate（:334）则假设 source_frame 唯一。
  - 影响: 调用方后续原地增删列表会使 rowCount 与模型通知脱节（视图索引陈旧/越界）；重复 source_frame 会破坏 insert_candidate 的去重假设。当前 dialog 未调用该方法（仅测试/潜在使用），风险为潜伏。
  - 建议: `self._candidates = list(candidates)`。
  - 置信度: 高

- [AUD-20803] [Suggestion] 批量操作循环期间不持锁，decision_changed 处理器可重入修改列表
  - 位置: src/features/edit/ui/scene_cut_result_model.py:460-472（accept_high_confidence；474-555 其余 4 个批量方法同构）
  - 证据:
    ```python
    if self._updating:
        return 0
    count = 0
    for i, candidate in enumerate(self._candidates):
        if candidate.is_high_confidence and not candidate.is_warning:
            if candidate.is_pending:
                if self.set_decision(i, SceneCutDecision.ACCEPTED):
    ```
    set_decision（:403-406）在释放 `_updating` 后才 emit decision_changed，dialog 的 `_on_decision_changed`（scene_cut_dialog.py:2349-2359）在每次发射中同步执行 to_updated_analysis/_sync_graph_candidates。
  - 影响: 任何 decision_changed 处理器若触发批量操作或 insert/remove_candidate，会与进行中的 enumerate 脱节（索引错位、重复计数）。当前 dialog 处理器只读+重绘，未触发；属防御缺口。
  - 建议: 批量循环整体持有 `_updating`，循环结束后统一发 dataChanged 与 batch_operation_completed；或在文档中明确禁止重入。
  - 置信度: 中

- [AUD-20804] [Suggestion] to_updated_analysis 的 decisions 字典是自映射 no-op，且按 source_frame 键在重复帧时会静默合并
  - 位置: src/features/edit/ui/scene_cut_result_model.py:315-323
  - 证据:
    ```python
    decisions = {
        c.source_frame: c.decision
        for c in self._candidates
    }
    updated_candidates = tuple(
        c.with_decision(decisions.get(c.source_frame, c.decision))
        for c in self._candidates
    )
    ```
  - 影响: 字典与生成器遍历同一列表，get 永远命中自身，等价于 `tuple(self._candidates)`——代码有误导性；若上游经 set_candidates 引入重复 source_frame，后者的决策会静默覆盖前者。
  - 建议: 直接返回 `replace(self._analysis, candidates=tuple(self._candidates))`；如需映射语义，先断言键唯一。
  - 置信度: 高

- [AUD-20805] [Suggestion] _string_to_decision 对未知字符串静默返回 PENDING，把无效编辑变成状态变更
  - 位置: src/features/edit/ui/scene_cut_result_model.py:898-907
  - 证据:
    ```python
    elif value_lower in ("pending", self._translate(I18N_KEYS["decision_pending"]).lower()):
        return SceneCutDecision.PENDING
    return SceneCutDecision.PENDING
    ```
  - 影响: setData 收到拼错/未翻译字符串时，候选会被悄悄改为 PENDING（一次真实状态变更+信号发射），而不是返回 False 拒绝编辑。
  - 建议: 未知值返回 None，setData 判 None 后返回 False，并记录 warning 日志。
  - 置信度: 高

- [AUD-20806] [Suggestion] move_selection_up 在空模型时把 _current_index 置为 0，留下不一致状态
  - 位置: src/features/edit/ui/scene_cut_result_model.py:575-584、:559-569
  - 证据:
    ```python
    new_index = max(0, self._current_index - 1)
    self.set_current_index(new_index)
    ```
    set_current_index 无边界校验：列表为空时 `_current_index` 被写成 0（不发信号但状态失真），后续 insert_candidate（:347-348）基于该值调整索引。
  - 影响: 轻微——空列表后选择索引指向不存在的行，插入行后选中位置偏移一行的可能性。
  - 建议: set_current_index 中越界（除 -1 外）时归一为 -1。
  - 置信度: 高（机制）/ 低（实际影响）

- [AUD-20807] [Suggestion] 文档宣称支持排序，但未实现 sort()
  - 位置: src/features/edit/ui/scene_cut_result_model.py:5-6、:110-112
  - 证据:
    ```
    Supports decision editing, sorting, filtering by status, and i18n.
    "Provides editable decision column, sortable rows, and i18n support."
    ```
    全文无 `def sort(` 覆写（QAbstractTableModel 默认排序为 no-op）。
  - 影响: 挂接到开启 sortingEnabled 的 QTableView 时点击表头无效，与文档不符。
  - 建议: 实现 sort()（按帧号/分数列排序并保持结构通知），或修正文档。
  - 置信度: 高

### 通过项
- 线程安全：模型仅在 UI 线程被驱动（scene_cut_dialog 经 worker 的 queued 信号 `_on_detection_completed` → set_analysis），模型内部无跨线程共享可变状态。
- GPU 资源生命周期 / GLSL·HDR：纯 Qt 表模型，无 OpenGL 调用、无 GPU 资源、无 HDR 数据处理。
- 信号防递归：set_analysis/set_candidates/clear/set_decision 均有 `_updating` 状态锁；dataChanged 在锁内、decision_changed 在锁外发射，与 dialog 消费方式匹配（残余风险见 AUD-20803）。
- 状态提交一致性：dialog 在决策/增删后均以 `to_updated_analysis() or self._analysis` 回写 `_analysis`（scene_cut_dialog.py:2196/2266/2229），模型侧 beginResetModel/beginInsertRows/beginRemoveRows 结构通知完整；remove/insert 对 `_current_index` 的平移正确。
- 异常与取消路径：无吞异常的宽泛 except；输入（index/decision）边界检查完备（get_candidate/set_decision/rowCount/data 均校验）。
- 输入验证：`_format_timecode` 对负数秒返回 "00:00:00:00"；候选字段由 SceneCutCandidate.__post_init__ 保证范围。
- 性能：`_calculate_scene_number` 为 O(n²)（每行重新计数），但候选数量为场景切换级（数百），且仅显示时对可见行调用，可接受。
- 类型注解：公开方法与返回值注解完整（`Optional[Any]` 的颜色返回来自 Qt 类型，可接受）。

---

## src/features/edit/ui/scene_cut_shortcuts.py (行数: 98)

### 发现

- [AUD-20808] [Suggestion] register_scene_cut_shortcuts 参数与 _manager 返回值缺类型注解（noqa 压制）
  - 位置: src/features/edit/ui/scene_cut_shortcuts.py:38、:88
  - 证据:
    ```python
    def register_scene_cut_shortcuts(shortcut_manager) -> None:  # noqa: ANN001
    ...
    def _manager():  # noqa: ANN202 - avoids importing the manager at module load
    ```
  - 影响: 违反项目"所有函数有类型注解"约束；`shortcut_manager` 的协议（register/get_shortcut/instance，已确认存在于 src/shared/ui/shortcut_manager.py:135/141/187）无法被静态检查。
  - 建议: 定义 `ShortcutManagerProtocol`（structural typing）或在 `TYPE_CHECKING` 块中导入 ShortcutManager 标注，保持运行时惰性导入不变。
  - 置信度: 高

- [AUD-20809] [Suggestion] 小键盘 Enter（带 KeypadModifier）无法匹配 Enter 快捷键
  - 位置: src/features/edit/ui/scene_cut_shortcuts.py:71-79、:82-85
  - 证据:
    ```python
    if event.modifiers() != Qt.KeyboardModifier.NoModifier:
        return False
    ...
    aliases = {
        "Enter": (Qt.Key.Key_Enter, Qt.Key.Key_Return),
    ```
    `_sequence_for_event` 把 `event.modifiers()` 原样并进 QKeySequence；数字小键盘的 Enter 按下时 modifiers 含 KeypadModifier，精确匹配失败后又被 NoModifier 检查挡在别名路径之外。
  - 影响: 使用数字小键盘 Enter 的用户无法触发 "Accept Current Cut"。
  - 建议: 比较前剥离 KeypadModifier：`mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier`，并用于两处判断。
  - 置信度: 中（Qt 平台行为，未在本机实测）

### 通过项
- 线程安全 / GPU 资源生命周期：纯快捷键定义与事件匹配，无状态修改、无 OpenGL、无资源持有。
- 信号防递归：无信号连接，不适用。
- 异常与取消路径：`_manager()` 仅捕获特定 `ImportError`（headless 防御），符合"不得宽泛 except"铁律。
- 输入验证：`event_matches` 先校验 `QEvent.Type.KeyPress`；别名仅在动作仍使用默认键时生效、用户自定义后严格精确匹配的设计正确（:73-74）。
- ShortcutManager API 对齐：`register(action_id, label, category, default, feature_id=...)` 与 `get_shortcut` 签名已核对一致（shortcut_manager.py:141/187）。

---

## src/features/edit/ui/scene_cut_timeline_overlay.py (行数: 897)

### 发现

- [AUD-20810] [Major] `timeline_frame or source_frame` 把合法的第 0 帧当作假值回退，标记错位
  - 位置: src/features/edit/ui/scene_cut_timeline_overlay.py:322
  - 证据:
    ```python
    def _update_geometry(self) -> None:
        """Update X from frame and zoom. Y is fixed at ruler bottom."""
        # Use timeline_frame if available, else source_frame
        frame = self._candidate.timeline_frame or self._candidate.source_frame
    ```
    models.py:593 定义 `timeline_frame: int | None = None`，0 是有效帧位。
  - 影响: 切换点位于时间线第 0 帧时，标记会画在 source_frame 对应的 X 位置；在子片段/带偏移的探测（source_frame ≠ timeline_frame）场景下用户会在错误位置审看与决策。
  - 建议: `frame = self._candidate.timeline_frame if self._candidate.timeline_frame is not None else self._candidate.source_frame`。
  - 置信度: 高

- [AUD-20811] [Major] update_num_tracks 改变 boundingRect 高度但未调用 prepareGeometryChange，违反 Qt 几何契约
  - 位置: src/features/edit/ui/scene_cut_timeline_overlay.py:344-347（boundingRect 依赖见 :288）
  - 证据:
    ```python
    def update_num_tracks(self, num_tracks: int) -> None:
        """Update number of tracks (for line height)."""
        self._num_tracks = num_tracks
        self.update()
    ```
    boundingRect 中 `track_area_height = self._num_tracks * EDIT_SIZES["track_h_video"]`。同库同类 item 的既有模式是先 `prepareGeometryChange()` 再改高度（timeline_items.py:6745-6748、:9166-9169）。
  - 影响: 场景 BSP 索引与缓存包围盒过期——轨道数变化后出现重绘残影、命中测试在新区域失效。
  - 建议: 变更 `_num_tracks` 前调用 `self.prepareGeometryChange()`（与 timeline_items 的 PlayheadItem 保持一致）。
  - 置信度: 高

- [AUD-20812] [Major] 竖线长度按 `num_tracks × 视频轨高(82px)` 估算，含音频/字幕轨或轨道调高时线长错误
  - 位置: src/features/edit/ui/scene_cut_timeline_overlay.py:288、:401
  - 证据:
    ```python
    track_area_height = self._num_tracks * EDIT_SIZES["track_h_video"]
    ```
    而 edit_style.py 定义 `track_h_video: 82`、`track_h_audio: 66`、`track_h_min/max: 24/200`（轨道可调）；timeline_view.py `_scene_cut_track_count()` 传入的是 video+audio+subtitle 总轨数；同视图的 playhead/snap 指示器走精确的 `update_track_area_height(total_height)`（timeline_view.py:960/1350-1356）。
  - 影响: 例——1 视频 + 4 音频轨：估 5×82=410px，实际 82+4×66=346px；轨道手动调高后偏差更大。overlay 竖线越过真实轨道区画到时间线外的视图区域。
  - 建议: 仿照 PlayheadItem 增加 `update_track_area_height(height)`，由 timeline_view 在 `_sync_scene_cut_overlay` 与布局刷新时传 `_total_track_area_height()` 真值。
  - 置信度: 高

- [AUD-20813] [Major] select_candidate 持有 `_updating` 锁时发射 candidate_selected，处理器内的同步回调被静默丢弃
  - 位置: src/features/edit/ui/scene_cut_timeline_overlay.py:724-741
  - 证据:
    ```python
    self._updating = True
    try:
        ...
        self._selected_candidate_id = candidate_id
        marker = self._marker_items.get(candidate_id)
        if marker:
            marker.set_selected(True)
        # Emit signal
        self.candidate_selected.emit(candidate_id)
    finally:
        self._updating = False
    ```
    同类的 set_candidates/update_candidates/select_candidate 都以 `if self._updating: return` 拦截（:616、:650、:721），直连信号为同步调用。
  - 影响: 外部处理器在信号槽内对 overlay 的任何回写（update_candidates/set_candidates/select_candidate）都会被静默吞掉且无日志；与 SceneCutResultModel"锁内改状态、锁外发信号"（result_model.py:403-406）的项目约定相反。当前 timeline_view.py 尚未连接该信号，属潜伏缺陷，一旦接线即触发。
  - 建议: 先在锁内完成状态变更，`finally` 释放锁后再 emit（对齐 result_model 的时序）。
  - 置信度: 高（机制）/ 当前未触发

- [AUD-20814] [Major] hover tooltip 绘制在 boundingRect 之外，违反 Qt 绘制契约，产生残影/不刷新
  - 位置: src/features/edit/ui/scene_cut_timeline_overlay.py:296-301 与 :464-468
  - 证据:
    ```python
    return QRectF(
        -half_size - CANDIDATE_HANDLE_MARGIN / 2,
        -triangle_height,          # 顶部为 -10
        ...
    ```
    ```python
    tooltip_y = -CANDIDATE_TRIANGLE_SIZE - tooltip_height - 5   # 高于 -10
    tooltip_rect = QRectF(tooltip_x, tooltip_y, tooltip_width, tooltip_height)
    painter.drawRoundedRect(tooltip_rect, 4, 4)
    ```
    tooltip 高度约 6 行×14+8=92px，绘制区域远超包围盒顶界；hoverEnterEvent 仅 `self.update()`（:513-517），只失效包围盒区域。
  - 影响: Qt 要求 paint() 不越出 boundingRect——tooltip 出现/消失时其所在区域不被重绘，留下残影或显示不全，缩放/滚动后更明显。
  - 建议: 将 tooltip 区域并入 boundingRect（hover 状态切换时用 prepareGeometryChange 调整），或改用 `QToolTip.showText` / 独立悬浮 item 绘制。
  - 置信度: 高

- [AUD-20815] [Suggestion] show_overlay/hide_overlay/toggle_overlay 不使用 `_updating` 锁，overlay_visibility_changed 存在回环隐患
  - 位置: src/features/edit/ui/scene_cut_timeline_overlay.py:766-783
  - 证据:
    ```python
    def show_overlay(self) -> None:
        """Show all candidate markers."""
        self._is_visible = True
        self._update_visibility()
        self.overlay_visibility_changed.emit(True)
    ```
    EditPage 侧由菜单/持久化开关驱动 `set_scene_cut_overlay_visible`（edit_page.py:2882），是"程序化更新控件"场景。
  - 影响: 若外部把该信号接回会改变自身状态的控件且未 blockSignals，可形成一次信号回环（通常被相等性判断截断，不至死循环）；与文件内其他方法统一使用 `_updating` 的防递归约定不一致。
  - 建议: 与 set_candidates/select_candidate 一致套用 `_updating` 锁（锁外发信号），或在类文档中声明由外部保证。
  - 置信度: 中

- [AUD-20816] [Suggestion] shape() 只含 ruler 底部倒三角，竖线与 handle margin 区域不可点击/悬停
  - 位置: src/features/edit/ui/scene_cut_timeline_overlay.py:303-317（对照 :276-301）
  - 证据:
    ```python
    def shape(self) -> QPainterPath:
        """Return shape for collision detection (triangle area)."""
        ...
        path.addPolygon(triangle)
        return path
    ```
    boundingRect 明确为竖线+三角形+`CANDIDATE_HANDLE_MARGIN` 预留了宽度（:291），docstring 声称 "Click to select candidate for review"。
  - 影响: 深度缩放时唯一点击目标是 10px 三角形；点击/悬停竖线无响应，预留的 handle margin 是死区。注释表明限定三角形是刻意的，故列为体验/设计改进。
  - 建议: shape 加入竖线细长矩形（宽 CANDIDATE_LINE_WIDTH + margin）与三角形并集。
  - 置信度: 高（机制）

- [AUD-20817] [Suggestion] 未使用导入与双重常量导入
  - 位置: src/features/edit/ui/scene_cut_timeline_overlay.py:33、:57、:62-66
  - 证据:
    ```python
    from typing import Optional, Dict, Any, List, Callable   # Any/List/Callable 未使用
    from src.features.edit.ui.edit_style import EDIT_SIZES, get_color   # get_color 未使用
    from src.features.edit.ui.timeline_layout import (
        TimelineLayoutEngine,
        TRACK_HEADER_WIDTH as LAYOUT_TRACK_HEADER_WIDTH,     # 未使用
        RULER_HEIGHT as LAYOUT_RULER_HEIGHT,                 # 未使用
    )
    ```
    全文颜色均为模块级硬编码 QColor 常量，未走 `get_color` 主题体系；两处 RULER_HEIGHT 目前同源于 EDIT_SIZES（timeline_items.py:91 与 timeline_layout.py:34）暂无分叉。
  - 影响: 冗余导入；若两个 RULER_HEIGHT 将来分叉，风险隐匿；颜色游离于主题系统之外，暗色/亮色主题下不可适配。
  - 建议: 删除未用导入；统一从 timeline_layout 导入；颜色改经 edit_style/get_color 管理。
  - 置信度: 高

- [AUD-20818] [Suggestion] paint 的 widget 参数类型注解错误；`_pixels_per_frame`/`_fps` 为存而不用的死状态
  - 位置: src/features/edit/ui/scene_cut_timeline_overlay.py:368-372、:824-828、:171/:334、:174/:350
  - 证据:
    ```python
    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: Optional[QGraphicsItem] = None   # Qt 签名为 QWidget*
    ) -> None:
    ```
    `self._pixels_per_frame` 只在 update_zoom 中被赋值，从未参与定位（`_update_geometry` 完全依赖 layout_engine.frame_to_x，其内已含缩放，见 timeline_layout.py:564-566）；`_fps` 同样只在 update_fps 赋值、无消费者。
  - 影响: 错误注解误导调用方；死状态暗示"zoom 双通道"的错觉，增加维护成本。
  - 建议: 注解改为 `Optional[QWidget]`；移除 `_pixels_per_frame`/`_fps` 或注明保留原因。
  - 置信度: 高

### 通过项
- 线程安全：overlay 仅在 UI 线程被驱动（timeline_view 的 set_candidates/show/hide 均来自视图槽），无跨线程共享可变状态。
- GPU 资源生命周期 / GLSL·HDR：纯 QGraphicsObject 绘制，无 OpenGL 调用（符合模块头声明），无 GPU 资源、无 HDR 数据。
- 状态提交一致性：不写时间线数据（只读 overlay），marker 生命周期由 `_remove_marker_item`→`scene().removeItem` 管理，父项持有避免 Python 侧提前回收；`clear()/cleanup()` 路径完整。
- 异常与取消路径：set_candidates/update_candidates 用 try/finally 保证锁释放；无宽泛 except。
- 输入验证：`_remove_marker_item` 用 `pop(..., None)` 容忍缺失键；`update_candidates` 正确同步增删（按 source_frame 键 diff）。
- 性能模式：set_candidates 全量重建 marker 仅在探测完成时发生；update_zoom 逐 marker setPos/update 由 Qt 合并重绘，可接受。
- 与布局引擎一致性：定位统一走 layout_engine.frame_to_x（与时间线同源，TRACK_HEADER_WIDTH 偏移正确）；Z 值分层（520/510）符合"clips 之上、playhead 之下"声明。

---

## 汇总统计

| 文件 | Critical | Major | Suggestion |
|------|----------|-------|------------|
| scene_cut_result_model.py | 0 | 1 (AUD-20800) | 7 (AUD-20801…20807) |
| scene_cut_shortcuts.py | 0 | 0 | 2 (AUD-20808…20809) |
| scene_cut_timeline_overlay.py | 0 | 5 (AUD-20810…20814) | 4 (AUD-20815…20818) |
| 合计 | 0 | 6 | 13 |
