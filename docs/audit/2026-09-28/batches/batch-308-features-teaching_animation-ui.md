# batch-308 [P2] src/features/teaching_animation/ui//

编号区间 AUD-30800–AUD-30899。文件 2 个，共 2,049 行。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性 / 异常与取消 / 输入验证 / 性能 / 类型注解。

## src/features/teaching_animation/ui/mg_keyframes_panel.py (行数: 1955)

### 发现

- [AUD-30800] [Major] 硬编码 Space 播放键绕过用户自定义快捷键配置
  - 位置: src/features/teaching_animation/ui/mg_keyframes_panel.py:1167
  - 证据:
    ```python
    def keyPressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if (
            event.key() == Qt.Key.Key_Space
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
        ):
            self._panel.playbackToggleRequested.emit()
    ```
    同样的硬编码分支在 `_TrackSide.keyPressEvent`（mg_keyframes_panel.py:1867）：
    ```python
    if key == Qt.Key.Key_Space and event.modifiers() == Qt.KeyboardModifier.NoModifier:
        panel.playbackToggleRequested.emit()
    ```
  - 影响: 面板持有用户可配置的播放快捷键 QShortcut（`apply_play_shortcut()`，宿主在 `shortcuts_changed` 时经 `motion_graphic_editor_window.py:281` 重新应用）。用户把 `motion_graphic/play_pause` 改绑到其他键后，QShortcut 只匹配新键，Space 不再被快捷键系统消费，而是落入子控件 keyPressEvent 的硬编码分支继续触发 `playbackToggleRequested`——播放/暂停键事实上无法改绑（Space 永远生效），并且与新绑定的键形成双触发入口；若用户把 Space 改绑给其他 MG 动作还会产生键冲突。代码库已有规范入口 `mg_shortcuts.event_matches(event, PLAY_PAUSE_ACTION_ID, PLAY_PAUSE_DEFAULT)`，此处未使用。
  - 建议: 两处 keyPressEvent 改用 `event_matches(event, PLAY_PAUSE_ACTION_ID, PLAY_PAUSE_DEFAULT)` 判断（导入自 `.mg_shortcuts`），或删除 keyPressEvent 分支、仅保留可配置 QShortcut。
  - 置信度: 高

- [AUD-30801] [Suggestion] 网格与标尺绘制固定从第 0 帧遍历，横向滚动后重复绘制大量屏幕外刻度线
  - 位置: src/features/teaching_animation/ui/mg_keyframes_panel.py:1306
  - 证据:
    ```python
    last_frame = max(0, int(panel._mapper.x_to_frame(self.width())) + 1)
    if pixels_per_second > 8:
        painter.setPen(QPen(QColor(70, 74, 82, 22), 1))
        for frame in range(0, last_frame + 1, minor_interval):
    ```
    标尺同样从 0 起（mg_keyframes_panel.py:1415）：
    ```python
    last_frame = max(0, int(mapper.x_to_frame(self.width())) + major_step * 2)
    last_label_right = float("-inf")
    frame = 0
    while frame <= last_frame:
    ```
  - 影响: `FrameMapper.frame_to_x = scroll_offset + frame * ppf`（mg_keyframes_model.py:388），水平滚动后 `scroll_offset` 为负，两个循环的迭代次数与"可见末帧"成正比而与视口无关——长时长场景滚动到中后段时，每次重绘（播放/擦洗期间 `set_current_frame` 每帧触发 `update()`）都会为 x<0 的屏幕外帧调用 drawLine，开销随场景总帧数线性增长。
  - 建议: 循环起点改为 `max(0, int(math.floor(mapper.x_to_frame(0.0))))`，终点维持现有可见右边界；标签的 `last_label_right` 逻辑不受影响。
  - 置信度: 高（代码行为确定；实际影响随场景时长与缩放级别放大）

- [AUD-30802] [Suggestion] 行索引/行查找在循环内线性扫描，形成 O(n²) 热点
  - 位置: src/features/teaching_animation/ui/mg_keyframes_panel.py:1833
  - 证据:
    ```python
    for row in panel._rows:
        if row.kind != "property":
            continue
        index = next(i for i, r in enumerate(panel._rows) if r is row)
        row_top = panel._row_y(index)
    ```
    同类模式：`_hit_keyframe` 对每个关键帧调用 `_row_center(row)`（mg_keyframes_panel.py:1215→1220），后者每次重新扫描全表：
    ```python
    def _row_center(self, row: MgKeyframesRow) -> float:
        index = next((i for i, r in enumerate(self._panel._rows) if r is row), 0)
        return self._panel._row_y(index) + _ROW_HEIGHT / 2.0
    ```
    `_row_by_id`（mg_keyframes_panel.py:487）在 `delete_selected`、`set_selected_interpolation`、`_finish_keyframe_drag` 的按行循环内被逐个调用。
  - 影响: 元素/属性行数与关键帧数量增大时，跑选框（marquee）、鼠标按下命中测试、批量删除/改插值均为平方复杂度；命中测试发生在每次鼠标按下。
  - 建议: 在 `_rebuild` 时构建 `row_id → index` 字典（或 `id(row) → index`）；`_hit_keyframe` 内先算一次 `row_center` 再遍历 points。
  - 置信度: 高

- [AUD-30803] [Suggestion] delete_selected 在未产生任何编辑时也无条件清空点选区
  - 位置: src/features/teaching_animation/ui/mg_keyframes_panel.py:863
  - 证据:
    ```python
    for row_id, frames in by_row.items():
        row = self._row_by_id(row_id)
        if row is None or not row.editable:
            continue
        edits.append((row.element_id, row.property_name,
                      track_with_keyframes_removed(self._effective_track(row), frames)))
    self._point_selection.clear()
    self._emit_edits(edits)
    ```
  - 影响: 当全部选中行不可编辑（或行已消失）时 `edits` 为空，`_emit_edits` 静默不发，但选区已被清空——Delete 键既无效果也无反馈，且与 `set_selected_interpolation`（不清空选区）行为不一致。
  - 建议: 仅在 `edits` 非空时清空（或只清除已成功提交的行的选区）。
  - 置信度: 高

- [AUD-30804] [Suggestion] 剪贴板跨场景残留，切换场景后仍可粘贴上一场景的关键帧
  - 位置: src/features/teaching_animation/ui/mg_keyframes_panel.py:928
  - 证据:
    ```python
    entries.append((row.element_id, row.property_name, frame - anchor, keyframe))
    self._clipboard = entries
    ```
    `set_scene`（mg_keyframes_panel.py:168-204）在 `scene_id` 变化时重置了选区/时长/缩放等状态，但从不清理 `_clipboard`；`paste_clipboard`（mg_keyframes_panel.py:934）仅按 element_id/property 在当前行中匹配。
  - 影响: 切换到另一场景后 Ctrl+V 仍保留旧场景的关键帧副本；当两场景元素 id 恰好相同（如同一模板派生）时会把旧场景的值/插值写进当前场景，属意外的状态泄漏。
  - 建议: `set_scene` 检测到 `scene_id` 变化时执行 `self._clipboard.clear()`。
  - 置信度: 中

- [AUD-30805] [Suggestion] 类型注解缺失/不精确：隐式 Optional 与 Qt 覆写方法上的 type: ignore
  - 位置: src/features/teaching_animation/ui/mg_keyframes_panel.py:292
  - 证据:
    ```python
    def _button(tooltip: str, icon: QIcon = None, text: str = "") -> QToolButton:  # type: ignore[assignment]
    ```
    另有约 10 处 `# type: ignore[no-untyped-def]`（如 mg_keyframes_panel.py:557 resizeEvent、561 showEvent、983/1002/1244 paintEvent、1151/1850 wheelEvent、1166/1864 keyPressEvent、1115/1593/1646/1675/1708 mouse*）。
  - 影响: `.clinerules` 要求所有函数有类型注解；`icon: QIcon = None` 是隐式 Optional（且用 ignore 掩盖了赋值检查），Qt 事件覆写的 `event` 参数均未标注具体类型（QMouseEvent/QWheelEvent/QKeyEvent/QResizeEvent/QShowEvent/QPaintEvent）。
  - 建议: 改为 `icon: QIcon | None = None`；为各 Qt 覆写方法补事件参数类型注解后移除 ignore。
  - 置信度: 高

- [AUD-30806] [Suggestion] _tool_icon 宽泛捕获 Exception 且无任何日志
  - 位置: src/features/teaching_animation/ui/mg_keyframes_panel.py:89
  - 证据:
    ```python
    try:
        from src.shared.assets.icon_manager import IconColors, get_icon
        return get_icon(name, IconColors.DEFAULT)
    except Exception:  # noqa: BLE001 - an icon must never break the toolbar
        return QIcon()
    ```
  - 影响: 注释已声明意图（图标失败不得拖垮工具栏），但 `Exception` 范围过宽会把 icon_manager 内部的编程错误（签名变更、返回类型错误等）一并静默吞掉，工具栏只显示空图标且无诊断线索，与"不得宽泛 except 掩盖领域错误"的铁律相悖。
  - 建议: 收窄为 `(ImportError, AttributeError)` 等预期异常，或至少在 except 分支记录 `logging.debug/exception`。
  - 置信度: 中

### 通过项
- 线程安全：纯 Qt UI 线程控件，无 OpenGL 调用、无跨线程共享数据，无需渲染线程边界处理
- GPU 资源生命周期：不涉及 ModernGL/纹理/缓冲区
- GLSL·HDR 正确性：不涉及（纯 2D QPainter 绘制，无数值 clamp 问题）
- 信号防递归：`_zoom_slider_updating`（mg_keyframes_panel.py:150/682-686）与 `_horizontal_scrollbar_updating`（151/537-547/551-556）状态锁完整覆盖程序化更新路径；`set_selection` 不回发 `element_selected`、`set_expanded_pages` 不发信号、宿主以 source 防回声，`pageExpansionChanged` 回显链无环
- 状态提交一致性（主体）：所有编辑以整轨批经 `tracks_changed` 单步提交；拖拽采用 snapshot-then-apply，`_drag_delta_valid` 整组校验（越界/碰撞整体拒绝）；选区在 emit 前预置并在 `_rebuild` 按 (row_id, frame) 重放、失效键剪枝（mg_keyframes_panel.py:455-463）；页面时长拖拽仅在帧数变化时发一次信号
- 异常与取消路径：`QInputDialog.getDouble` 取消即返回；`_format_timecode` 捕获具体异常（TypeError/ValueError/ZeroDivisionError）且有降级格式化；`edit_keyframe_value` 的 min/max 交换处理
- 输入验证：`set_property_filter` 白名单校验并抛 ValueError；帧号处处 clamp（`_seek_to`、`add_keyframe_at`、`paste_clipboard`、键盘 Home/End/左右步进、`set_current_frame`、`set_scene` 的 duration/当前帧钳制）
- 性能（部分）：`set_scene` 的对象身份快速路径避免播放期 24 次/秒全量重建（mg_keyframes_panel.py:184-188）；行绘制有视口上下界裁剪与 ruler 区域裁剪
- 资源：QShortcut/控件父子归属正确，无泄漏面

## src/features/teaching_animation/ui/mg_shortcuts.py (行数: 94)

### 发现

- [AUD-30807] [Suggestion] `Qt.Key(event.key())` 对枚举外的键值会抛 ValueError 且无防护
  - 位置: src/features/teaching_animation/ui/mg_shortcuts.py:82
  - 证据:
    ```python
    def _sequence_for_event(event: QEvent) -> QKeySequence:
        # PyQt6 returns a plain int for QKeyEvent.key(); wrap it back so the
        # | operator produces the QKeyCombination QKeySequence compares against.
        return QKeySequence(event.modifiers() | Qt.Key(event.key()))
    ```
  - 影响: PyQt6 中 `Qt.Key` 是严格枚举，`key()` 返回的整数若不在枚举表内（部分平台/输入法的扩展键值、驱动上报的原始键码）时 `Qt.Key(...)` 会抛 ValueError；该异常经 `event_matches` → 宿主 event filter（motion_graphic_editor_window.py:1059/2928、main_window.py:874）向上传播，可能中断按键事件处理链。
  - 建议: 用 `try/except ValueError: return QKeySequence()` 包裹，或改用 `QKeySequence(int(event.modifiers()) | event.key())` 的整数组合方式。
  - 置信度: 低（触发依赖平台上出现枚举外键值；未在真实设备复现，仅 API 语义风险）

- [AUD-30808] [Suggestion] 未类型化参数与宽泛的 event 类型注解
  - 位置: src/features/teaching_animation/ui/mg_shortcuts.py:46
  - 证据:
    ```python
    def register_motion_graphic_shortcuts(shortcut_manager) -> None:  # noqa: ANN001 - duck-typed ShortcutManager
    ...
    def _manager():  # noqa: ANN202 - avoids importing the manager for isinstance
    ```
    另 `event_matches(event: QEvent, ...)`、`motion_graphic_play_key(event: QEvent)` 实际接收 QKeyEvent，却注解为基类 QEvent。
  - 影响: `.clinerules` 要求全量类型注解；duck-typed 参数与基类事件注解削弱静态检查（如 `event.key()` 在 QEvent 上并不存在，注解错误但被"向上转型"掩盖）。
  - 建议: `shortcut_manager` 用 `Any` 或 Protocol 标注并保留注释；`_manager()` 补 `-> "ShortcutManager | None"`；事件参数改注 `QKeyEvent`。
  - 置信度: 高

### 通过项
- 线程安全：无状态纯函数 + 单例读取，仅 UI 线程使用
- GPU 资源生命周期 / GLSL·HDR：不涉及
- 信号防递归：不涉及信号
- 状态提交一致性：注册幂等（仅写注册表）、不绑 QAction，符合模块契约（快捷键改绑由宿主经 shortcuts_changed 重新应用）
- 异常与取消路径：ShortcutManager 缺失（ImportError）回退默认键，headless/测试路径安全；`configured_shortcut` 空串回退默认
- 输入验证：`event_matches` 仅响应 KeyPress，修饰键经 `modifiers() | Key` 正确组合，与 QKeySequence 比较语义一致

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 1 | AUD-30800 |
| Suggestion | 8 | AUD-30801…AUD-30808 |
