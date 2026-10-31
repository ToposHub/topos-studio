# batch-310 审查报告 — src/features/teaching_animation/ui//

批次：batch-310 [P2] · 编号区间 AUD-31000–AUD-31099 · 审查日期 2026-09-28
统计：Critical 0 · Major 4 · Suggestion 8（共 12 项发现）

审查方式：7 个文件全部 Read 通读；关键发现均交叉验证了关联模块
（`html_page_deck.py`、`drag_value.py`、`commands.py`、`editor_controller.py`、
`motion_graphic_editor_window.py`、`svg_renderer.py`、`model.py`）后再定级。

---

## src/features/teaching_animation/ui/motion_graphic_style.py (行数: 461)

### 发现
- [AUD-31001] [Suggestion] QSplitter::handle 规则重复定义两次
  - 位置: src/features/teaching_animation/ui/motion_graphic_style.py:457
  - 证据:
    ```css
    QSplitter::handle {{ background: {C.PANEL_DIVIDER}; }}
    QSplitter::handle:hover {{ background: {C.PANEL_DIVIDER}; }}
    ```
    （283–284 行已出现同样两条；457 行再次重复 `QSplitter::handle` 一条。）
  - 影响: 无功能影响，纯冗余；后续修改分隔条颜色时容易只改其一造成不一致。
  - 建议: 删除 457 行的重复规则，保留 283–284 行处的一份。
  - 置信度: 高

### 通过项
- 纯 QSS 字符串常量（`MOTION_GRAPHIC_EDITOR_QSS`），无逻辑、无信号、无资源生命周期问题
- 颜色均引用共享调色板 `C` / `EDIT_COLORS`，未硬编码散落值（个别 rgba 装饰色除外，属可接受）

---

## src/features/teaching_animation/ui/page_navigation.py (行数: 140)

### 发现
- [AUD-31002] [Suggestion] `_updating` 状态锁只写不读，是死代码
  - 位置: src/features/teaching_animation/ui/page_navigation.py:99
  - 证据:
    ```python
        def set_frame(self, frame: int) -> None:
            if self._scene is None or not self._buttons:
                return
            index = self._scene.page_index_for_frame(frame)
            self._updating = True
            try:
                for button_index, button in enumerate(self._buttons):
                    button.setChecked(button_index == index)
            finally:
                self._updating = False
    ```
    全文件对 `_updating` 仅有 35/99/104 三处赋值，无任何读取。
  - 影响: 防递归实际依赖的是按钮连接 `clicked`（程序化 `setChecked` 不会触发 `clicked`），
    锁本身无效但会误导后续维护者以为已有守卫，可能在改连 `toggled` 时引入回声循环。
  - 建议: 要么删除该锁并注释说明“`clicked` 不受程序化 setChecked 影响”，
    要么在 `_request_page` 中真正读取该锁。
  - 置信度: 高

### 通过项
- 信号防递归：页面按钮连接 `clicked`（87 行），程序化 `setChecked` 不会触发，无回声
- lambda 闭包均以默认参数绑定 `index`/`page_id`/`button`（87–90 行），无迟绑定陷阱
- 边界处理：`_request_page` 对 index 与 frame 双重 clamp（119–120 行）；`request_relative_page` 两端收敛
- `seconds = page.duration_frames / max(0.001, scene.fps)`（85 行）除法有保护
- UI 线程内操作，无 OpenGL/GPU 资源；类型注解完整

---

## src/features/teaching_animation/ui/page_timing_panel.py (行数: 433)

### 发现
- [AUD-31003] [Suggestion] `_link_slider` 双向互连未加状态锁，靠数值收敛终止乒乓
  - 位置: src/features/teaching_animation/ui/page_timing_panel.py:429
  - 证据:
    ```python
    @staticmethod
    def _link_slider(slider: ToposClickableSlider, value: DragValue, factor: float) -> None:
        """Keep Properties-style slider and drag value synchronized."""
        slider.valueChanged.connect(lambda position: value.setValue(position / factor))
        value.valueChanged.connect(lambda number: slider.setValue(round(number * factor)))
    ```
  - 影响: slider→DragValue→slider 每次交互至少多跑一轮冗余信号；当前因
    `DragValue.setValue` 有 `abs(v - self._value) < 1e-10` 短路（drag_value.py:200–205）
    且 `QSlider.setValue` 仅在值变化时发射而收敛终止，但该终止依赖两端控件的内部实现，
    与 `.clinerules`“程序化更新控件须状态锁防递归信号”及同项目
    scene_inspector `_add_number_row`（554–562 行，用 `blockSignals`）的惯用法不一致。
  - 建议: 改用与 `SceneInspector._add_number_row` 相同的 `blockSignals` 成对包裹模式。
  - 置信度: 中
- [AUD-31004] [Suggestion] `backgroundSettingsRequested` 无任何生产消费者，注释描述的刷新顺序已失实
  - 位置: src/features/teaching_animation/ui/page_timing_panel.py:384
  - 证据:
    ```python
        # Backgrounds are emitted first because the connected command refreshes
        # this panel.  Applying timing first would rebuild these controls from
        # the old scene and lose the just-entered background values before the
        # second signal was delivered.
        self.backgroundSettingsRequested.emit(
            self._global_background_edit.text().strip(), backgrounds,
        )
    ```
    全库 grep：编辑器仅连接 `settingsWithBackgroundRequested`
    （motion_graphic_editor_window.py:504–506），`backgroundSettingsRequested` 在 src/ 下无 connect。
  - 影响: 死信号每次 Apply 多发射一次；注释宣称的“connected command refreshes this panel”
    已不成立，误导后续排障（真实刷新来自 settingsWithBackgroundRequested 的处理器）。
  - 建议: 删除该发射与过时注释，或注明“仅保留给测试/外部集成”。
  - 置信度: 高
- [AUD-31005] [Suggestion] `self._timing` 缺少类型注解
  - 位置: src/features/teaching_animation/ui/page_timing_panel.py:60
  - 证据:
    ```python
        self._scene: TeachingSceneDocument | None = None
        self._timing = None
    ```
  - 影响: 实际类型为 `PageDeckTiming | None`，裸 `None` 削弱类型检查覆盖（类型注解维度）。
  - 建议: `self._timing: PageDeckTiming | None = None` 并补充 import。
  - 置信度: 高

### 通过项
- 状态提交一致性：Apply 走单一 `settingsWithBackgroundRequested` → 编辑器
  `_apply_html_page_settings_with_backgrounds` 一条 undo（编辑器 2255 行起），旧信号仅测试消费
- 信号防递归：`set_scene` 全程 `_updating=True` 且 finally 释放（250–335 行），所有 emit 入口（358/368/418）均检查 `_updating`
- 输入验证：`detect_page_count` 保证返回 ≥1（html_page_deck.py:379/385），244 行 `/ page_count` 无除零；
  `scene.pages[index]` 访问有 `index < len(scene.pages)` 守卫（294 行）
- 旧值捕获：`_emit_all` 在首次 emit 前捕获全部局部值（372–379 行），面板被刷新重建不影响后续发射
- 资源生命周期：页面行重建用 `setParent(None)` + `deleteLater`（337–346 行），无泄漏
- 颜色处理：`qcolor_to_linear_rgba` / `_color_from_css` 无异常路径，无效 CSS 回退 #111827

---

## src/features/teaching_animation/ui/preview_bridge.py (行数: 151)

### 发现

（无发现）

### 通过项
- 通过（无逻辑性缺陷）：`PreviewBridge` 为纯数据Transport；`WebEnginePreviewBridge` 按设计延迟导入
  QtWebEngine（56–58 行），产品不携带 WebEngine 时模块导入安全
- GPU 资源生命周期：`close()` 关闭并 `deleteLater` view（110–113 行），channel/channel_object 均以 view 为父随其释放
- 注入安全：`_send_frame` 用 `json.dumps(svg, ensure_ascii=False)` 构造 JS 字面量（121–127 行），SVG 内容正确转义
- 异常路径：不支持的 `pixel_format` 显式 `ValueError`（40 行），不吞错
- 线程安全注记：当前全库无生产调用方（仅 tests 与 `ui/__init__.py` 再导出），
  `runJavaScript` 的线程亲和要求暂不构成现网风险；若日后接入渲染回调需保证 UI 线程调用
- 就绪握手：`_pending_frame` 在 channel ready 后补发（115–118 行），无丢帧

---

## src/features/teaching_animation/ui/scene_inspector.py (行数: 958)

### 发现
- [AUD-31006] [Major] 多选编辑 Content 会把字符串写进 dict 内容的图元，破坏其数据并使渲染持续报错
  - 位置: src/features/teaching_animation/ui/scene_inspector.py:574
  - 证据:
    ```python
        value = self.content_edit.text()
        if len(self._selection_ids) > 1:
            self.selectionUpdateRequested.emit(self._selection_ids, {"content": value})
        elif isinstance(self._element.content, dict):
            content = dict(self._element.content)
            content["text"] = value
            self.elementUpdateRequested.emit(self._element.element_id, {"content": content})
    ```
    编辑器侧 `_selection_updated` 对每个选中元素原样应用：
    ```python
            update = dict(updates)
            style_patch = update.pop("style_patch", None)
            ...
            if update:
                commands.append(UpdateElementCommand(element.element_id, update))
    ```
  - 影响: 单选分支有 `isinstance(..., dict)` 合并保护，多选分支没有。多选（文本 + 图形）且
    primary 为文本时（Content 行按 primary 显隐，815 行），编辑内容会把 shape 的 dict content
    整体替换为纯字符串；`svg_renderer.py:304` 的 `content.get("kind", ...)` 随即抛
    `AttributeError`，此后每次刷新都落入编辑器的 "Preview error" 分支（968–972 行），
    预览全黑；若用户此时保存，图元几何数据（kind/corner_radius/points 等）永久丢失。
  - 建议: 多选分支同样按元素分流——dict 内容的元素改写 `content["text"]`，非 dict 元素才整体
    替换；或在 `_selection_updated` 中按 element.content 类型归一化。
  - 置信度: 高
- [AUD-31007] [Major] Transform 行每次拖动刻度都发射一条完整 elementUpdateRequested，逐 tick 生成 undo 条目
  - 位置: src/features/teaching_animation/ui/scene_inspector.py:557
  - 证据:
    ```python
        def from_slider(position: int) -> None:
            drag.blockSignals(True)
            drag.setValue(minimum + position * step)
            drag.blockSignals(False)
            self._transform_changed()
        ...
        slider.valueChanged.connect(from_slider)
        drag.valueChanged.connect(from_drag)
    ```
    `_transform_changed`（925–942 行）每次直接 `elementUpdateRequested.emit(...)`；
    编辑器 `_element_updated` → `editor_controller.update_element` → `execute(UpdateElementCommand)`
    逐次入栈（commands.py:1204–1210 无合并）。
  - 影响: 拖动 X/Y/宽高/缩放滑杆或 DragValue 的每个鼠标移动刻度都执行一次全场景不可变重建 +
    通知 + SVG 重渲染，并各自占据一条 undo（栈上限 100，会挤掉真实历史）；一次拖动手势需要
    数十次 Ctrl+Z 才能撤销。同文件的 style/layout/shape 行（325–347 行）以及画布拖拽
    （编辑器 1665–1673 行走 `preview_element`/`commit_preview`）都已采用延迟/预览提交，
    唯独 Transform 行未跟进。
  - 建议: Transform 行接入 `DragValue.interaction_ended` / `ParamSliderRow.interactionEnded`
    延迟提交（同 `_defer_style_field` 模式），或拖动期间走 `preview_element`、
    `interactionEnded` 时一次 `execute`。
  - 置信度: 高
- [AUD-31008] [Suggestion] 动画摘要读取使用宽泛 `except Exception` 静默吞错
  - 位置: src/features/teaching_animation/ui/scene_inspector.py:413
  - 证据:
    ```python
            try:
                from ..motion.motion_commands import read_motion_plans
                for plan in read_motion_plans(scene).values():
                    for element_id in plan.element_ids:
                        plans_by_element.setdefault(element_id, plan)
            except Exception:  # noqa: BLE001 — display-only path
                plans_by_element = {}
    ```
  - 影响: 虽有注释声明 display-only 且回退无害，但 `.clinerules` 禁止宽泛 except 掩盖领域错误；
    `read_motion_plans` 的真实缺陷（如 schema 变更导致的解析错误）会被永久隐藏。
  - 建议: 收窄为预期异常类型（ImportError/KeyError/ValueError），或至少在回退时记录 debug 日志。
  - 置信度: 中

### 通过项
- 信号防递归：`set_selection` 全程 `_updating` + finally（367–394 行）；所有 combo/slider
  handler 首行检查 `_updating`；`_populate_color_tokens`、`set_scene_canvas` 额外 blockSignals 成对恢复
- 状态提交一致性：style/layout/shape 采用 `_pending_*` 字段集合 + `interactionEnded` 提交，
  一次手势一条 undo、多选只写用户触碰的字段（114–118 行设计注释与实现一致）
- HDR/颜色：`linear_rgba_to_qcolor(...).name(HexRgb)` 与 `qcolor_to_linear_rgba` 往返转换
  （632–638、640–644 行），无未经转换的直通，也无无理由 clamp
- 输入验证：combo `findData` 结果统一 `max(0, ...)` 兜底（735–741、772–781 行）；
  `opacity` 提交时 clamp 到 0–100 再归一（673 行）
- `_canvas_breakpoint_changed` 校验 `isinstance(data, tuple) and len(data) == 2`（804 行）
- 折叠区/区段显隐按元素类型切换，`_set_line` 的 mixed 占位符处理正确（956–958 行）

---

## src/features/teaching_animation/ui/scene_timeline.py (行数: 291)

### 发现
- [AUD-31009] [Major] 时长 DragValue 每个 valueChanged 刻度都触发一条 SetDurationCommand，拖动一次产生大量 undo
  - 位置: src/features/teaching_animation/ui/scene_timeline.py:133
  - 证据:
    ```python
        self.duration_spin.valueChanged.connect(
            lambda seconds: self.durationChangeRequested.emit(
                max(1, int(round(seconds * self._fps))),
            )
        )
    ```
    编辑器侧（motion_graphic_editor_window.py:2957–2959）：
    ```python
    def _duration_changed(self, duration: int) -> None:
        if duration != self.editor_controller.scene.duration_frames:
            self.editor_controller.execute(SetDurationCommand(duration))
    ```
    而 `DragValue.mouseMoveEvent` 每个鼠标移动像素都 `setValue`（drag_value.py:345–357），
    `SceneCommandStack.execute` 对每条命令独立入栈（commands.py:1204–1210，无合并），
    `SetDurationCommand` 每条还持有 `_previous_scene` 整景快照（commands.py:383）。
  - 影响: 拖动时长控件 1 s→10 s（0.05 步进）约产生上百条命令：每刻度一次
    `resize_scene_pages`+`retime_page_tracks` 全景重建与通知，undo 粒度变成 0.05 s 一格，
    且每刻度 clear redo、挤占 100 条上限；与同项目 inspector 的延迟提交模式不一致。
  - 建议: `duration_spin` 改用 `interaction_started`/`interaction_ended`（DragValue 已提供）
    期间仅预览、释放时一次性 `durationChangeRequested`；或为 SetDurationCommand 增加合并逻辑。
  - 置信度: 高
- [AUD-31010] [Suggestion] `_MarkerTimelineSlider.paintEvent` 缺少参数类型注解
  - 位置: src/features/teaching_animation/ui/scene_timeline.py:41
  - 证据:
    ```python
    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
    ```
  - 影响: 类型注解维度缺失（同文件其余方法均完整标注）。
  - 建议: `def paintEvent(self, event: QPaintEvent) -> None:` 并导入 QPaintEvent。
  - 置信度: 高

### 通过项
- 信号防递归：`set_scene` 对 slider 与 duration_spin 均 blockSignals 成对恢复（200–208 行）；
  `set_frame` 静默更新（253–255 行）无回声；编辑器注释亦确认该静默契约
- 播放边界：`_advance` 越界时按 loop 回绕或停在最大帧并 `stop()`（272–281 行）；
  定时器间隔 `max(1, round(1000 / fps))` 有保护
- 标记菜单 lambda 均以默认参数绑定 `marker_id`/`frame`/`label`（229–245 行），无迟绑定陷阱
- `_frame_to_display` 除法 `max(self._fps, 0.001)` 保护（290 行）；`_marker_x` 的 span `max(1, ...)`
- `set_transport_host` 对重复调用与归还自身布局两种路径处理正确（172–195 行）

---

## src/features/teaching_animation/ui/scene_view.py (行数: 518)

### 发现
- [AUD-31011] [Major] 每帧新建的 QSvgRenderer 以 self 为父且从不释放，预览路径无限累积
  - 位置: src/features/teaching_animation/ui/scene_view.py:91
  - 证据:
    ```python
    def set_frame(self, frame: RenderedFrame) -> None:
        if frame.pixel_format == "svg":
            self._frame = frame
            self._image = None
            self._renderer = QSvgRenderer(QByteArray(frame.data), self)
    ```
    `set_scene` 丢弃缓存时同样只置 None（81–84 行）：
    ```python
        if previous is None or previous != scene:
            self._frame = None
            self._renderer = None
            self._image = None
    ```
  - 影响: `QSvgRenderer(..., self)` 把 C++ 所有权交给父 widget，Python 侧替换 `self._renderer`
    引用不会销毁旧对象——它们作为 SceneView 的子对象存活到窗口销毁为止。该路径由编辑器
    `_refresh_scene`（941–944 行）在每个 controller 通知上调用：画布拖拽期间
    `preview_element` 每个鼠标刻度通知一次，即每秒新建数十个持有已解析 SVG DOM 的
    renderer；长时间编辑会话内存单调增长（GPU/CPU 双侧），最终导致性能退化或 OOM。
  - 建议: 替换前对旧对象 `old = self._renderer; ... old.deleteLater()`（或不设父、
    改用 `renderer.load(QByteArray(...))` 复用单实例）。
  - 置信度: 高
- [AUD-31012] [Suggestion] rgba8 帧构造 QImage 前未校验缓冲区长度
  - 位置: src/features/teaching_animation/ui/scene_view.py:96
  - 证据:
    ```python
    elif frame.pixel_format == "rgba8":
        # HTML captures produce tightly-packed RGBA8888 bytes. Build the
        # QImage from the buffer, then .copy() so it owns its memory and
        # does not reference the (possibly GC'd) Python bytes.
        image = QImage(frame.data, frame.width, frame.height,
                       frame.width * 4, QImage.Format.Format_RGBA8888)
    ```
  - 影响: `RenderedFrame.__post_init__` 只校验维度为正和 data 是 bytes
    （contracts.py:134–140），不校验 `len(data) == width*height*4`；QImage 构造不复制也不
    检查长度，长度不足时后续 `drawImage` 越界读 worker 产物。`.copy()` 与 `isNull()`
    检查都在长度错误之后，无法兜底。当前数据源为内部 HTML 捕获 worker，风险低但违反
    “外部数据须验证”铁律。
  - 建议: 构造前 `if len(frame.data) < frame.width * frame.height * 4: raise ValueError(...)`，
    或在 RenderedFrame 契约层按 pixel_format 校验长度。
  - 置信度: 中

### 通过项
- 线程安全：全部为 UI 线程内的 QWidget 绘制与鼠标处理，无 OpenGL 调用
- `rgba8` 路径的 `.copy()` 正确切断 QImage 对 Python bytes 的生命周期依赖（94–100 行注释与实现一致）
- paintEvent/`_to_scene_point` 的除法安全：`TeachingSceneDocument.__post_init__` 强制
  width/height > 0（model.py:419–420 附近），不会出现 0 除
- 输入验证：拖放载荷 `json.loads` 包裹 `(ValueError, UnicodeDecodeError)` 且校验 dict 与
  preset_id 非空（200–212 行）；`set_draw_shape_kind` 校验 SHAPE_KINDS 并显式 ValueError（123–124 行）
- 选择/拖拽语义：锁定元素不可点选/框选（272–292、366–367 行），marquee 排除 hidden；
  shift 等比约束与最小 2 px 创建阈值处理正确（313–317、344–354 行）；
  `can_create` 三元表达式经优先级核验语义正确（按 is_stroke 二分）
- 路径工具：闭合判定以 8 px / zoom 缩放容差（431–436 行），Escape 清空绘制状态
- `mouseReleaseEvent` 对元素被中途删除的情形安全退出（359–361 行 element None 检查）
