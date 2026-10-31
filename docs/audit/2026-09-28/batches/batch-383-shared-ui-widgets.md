# batch-383 审查报告 — src/shared/ui/widgets/（P2）

- 编号区间：AUD-38300–AUD-38399（实际使用 AUD-38300–AUD-38315）
- 审查文件：8 个 / 2,642 行，全部 Read 通读
- 汇总：Critical 0 / Major 4 / Suggestion 12

---

## src/shared/ui/widgets/gpu_scope_widget.py (行数: 509)

### 发现

- [AUD-38300] [Suggestion] blit/chrome 失败仅以 debug 级宽泛捕获，teardownGL 裸 pass，生产环境难以诊断
  - 位置: src/shared/ui/widgets/gpu_scope_widget.py:241, 263-268
  - 证据:
    ```python
    try:
        glDeleteFramebuffers(1, [self._read_fbo])
    except Exception:
        pass
    ...
    try:
        self._blit_shared_texture()
    except Exception as exc:
        logger.debug("GpuScopeWidget blit failed: %s", exc)
    ```
  - 影响: 展示链路任何 GL 错误（纹理共享失效、FBO 不完整、驱动差异）在默认日志级别下完全不可见，示波器"黑屏"时无线索可查；违反 .clinerules「不得宽泛 except 掩盖领域错误」的精神（虽有注释解释不让异常逃出 Qt 回调，但日志级别过低）。
  - 建议: 保留兜底 try，但将日志提升为 warning 并做频率限制（如首次失败 warning、重复降级 debug）；teardownGL 的 pass 至少记 debug。
  - 置信度: 高

- [AUD-38301] [Suggestion] `_paint_waveform_scale` 的 nits / code10_video 两分支为逐行重复代码；`_legend_strip` 属性未在 `__init__` 声明
  - 位置: src/shared/ui/widgets/gpu_scope_widget.py:338-346, 387-413
  - 证据:
    ```python
    if self._scale_mode == 'nits':
        ...
        for value, label in waveform_scale_marks('nits', self._transfer):
            t = (float(value) + 0.1) / 1.3
            y = max(9, min(h - 3, round((1.0 - t) * h)))
            painter.drawText(3, y + 3, label)
    ...
    if self._scale_mode == 'code10_video':
        ...
        for value, label in waveform_scale_marks('code10', self._transfer):
            t = (float(value) + 0.1) / 1.3
            y = max(9, min(h - 3, round((1.0 - t) * h)))
            painter.drawText(3, y + 3, label)
    ```
  - 影响: 标尺映射逻辑（t/y 计算）三处复制，改刻度映射时易漏改；`_legend_strip` 依赖 getattr 兜底，削弱可读性。
  - 建议: 抽出 `_draw_scale_marks(marks)` 公共函数；在 `__init__` 中声明 `self._legend_strip: Optional[tuple] = None`。
  - 置信度: 高

### 通过项
- 线程安全：`set_scope_frame` 经跨线程 queued 信号链（renderer 线程 emit `gpu_scope_ready` → engine → layer_controller.on_gpu_scope_ready → panel.update_gpu_scope）最终在 GUI 线程调用；paintGL 用全局 GL mutex `tryLock()`，锁忙时保留上一帧立即返回，不阻塞 GUI
- GPU 资源生命周期：read FBO 懒创建、teardownGL 释放；源纹理归渲染器 deferred-release 所有，blit 前 `glIsTexture` 验证 + FBO 完整性检查，失败降级为空/上一帧，无 CPU 回读
- GLSL·HDR 正确性：纯展示 blit（无 shader、无采样改写、无 clamp），单采样目标规避 macOS MSAA blit 无效问题
- 信号防递归：无自定义信号
- 状态提交一致性：set_scale_mode / set_legend 幂等（值未变不触发重绘）
- 输入验证：宽高 `max(1, int(...))` 防除零；blit 前 FBO 状态检查
- 性能模式：legend strip 按 (mode, transfer) 缓存，paintGL 显示频率下不重复构建 LUT

---

## src/shared/ui/widgets/hsl_curves_widget.py (行数: 955)

### 发现

- [AUD-38302] [Major] `set_lut(emit_changed=True)` 的 changed 信号被自身 blockSignals 吞掉，永不送达接收方
  - 位置: src/shared/ui/widgets/hsl_curves_widget.py:832-864
  - 证据:
    ```python
    was_blocked = self.blockSignals(True)

    try:
        ...
        self.update_lut(emit_changed=emit_changed)
        self.update()

        if emit_changed and self._pending_lut is not None:
            self._emit_changed_signal()
    finally:
        self.blockSignals(was_blocked)
    ```
  - 影响: `QObject.blockSignals(True)` 会屏蔽该对象所有信号的 emit——`_emit_changed_signal` 内的 `self.changed.emit(...)` 不触发任何已连接槽，但随后仍把 `_pending_lut` 置 None；16ms 定时器到期再进 `_emit_changed_signal` 时 pending 已空，什么都不做。净效果：legacy 工程恢复路径下 `emit_changed=True` 静默失效，调色不应用且无任何报错。当前库内调用方（layer_controller.py:47、tools_panel.py:217）均用默认 False，故为潜伏缺陷，但公开参数承诺的行为不存在。
  - 建议: 去掉 set_lut 内的 blockSignals 包裹（内部无会触发信号回灌的子控件操作），或在 finally 恢复信号后再调用 `flush()`/`_emit_changed_signal()`。
  - 置信度: 高

- [AUD-38303] [Major] 点击 hue 曲线端点的 wrap-around 同步修改了曲线数据，但未置脏、未重建 LUT，UI 显示与渲染状态发散
  - 位置: src/shared/ui/widgets/hsl_curves_widget.py:553-562, 582
  - 证据:
    ```python
    if clicked_idx != -1:
        self.selected_point_idx = clicked_idx

        # === Hue Wrap-around: Sync endpoints on click ===
        if self._should_sync_endpoints() and self._is_endpoint(clicked_idx):
            new_y = pts[clicked_idx].y()
            pts[0] = QPointF(0, new_y)
            pts[-1] = QPointF(255, new_y)
    ...
    self.update()
    ```
  - 影响: 2 点 hue 曲线两端 Y 不同时（可由工程恢复 `set_curves_data` 产生），单击任一端点会把另一端点 Y 拉齐——`paintEvent._draw_curve` 直接按点重算显示新曲线，但该分支既不置 `_lut_dirty` 也不调 `update_lut()`，渲染端持有的 LUT 仍是旧曲线；用户松开后 `interaction_ended` 触发的快照/自动保存若读取 `get_curves_data()` 则保存状态与渲染画面不一致。
  - 建议: 在 sync 修改 pts 后补 `self._lut_dirty = True; self.update_lut()`（与右键删点分支一致）。
  - 置信度: 中（机制由代码可证；触发需两端 Y 不等的 2 点 hue 曲线）

- [AUD-38304] [Major] `calculate_curve` 使用裸 `except:` 兜底，违反 .clinerules 并掩盖插值错误
  - 位置: src/shared/ui/widgets/hsl_curves_widget.py:527-528
  - 证据:
    ```python
            else:
                return np.interp(xi, xs, ys)
        except:
            return np.interp(np.arange(256), xs, ys)
    ```
  - 影响: 裸 except 连 KeyboardInterrupt/SystemExit 一并吞掉；PCHIP 构造/求值中的真实数值错误（重复 X、NaN 等）被静默降级为线性插值，调色结果无警示地改变，线上无法定位。该函数是 LUT 生成（渲染输入）的核心路径。
  - 建议: 改为 `except (ValueError, np.linalg.LinAlgError) as exc: logger.warning(...)` 并在输入侧防重 X/NaN；至少排除 BaseException。
  - 置信度: 高

- [AUD-38305] [Suggestion] `set_curves_data` 内联复制 update_lut 的重建逻辑，且对恢复数据无验证/钳制
  - 位置: src/shared/ui/widgets/hsl_curves_widget.py:774-795
  - 证据:
    ```python
    for mode in MODE_ORDER:
        if mode in data:
            self.curves[mode] = [QPointF(x, y) for x, y in data[mode]]
    ...
    if self._lut_dirty:
        self._lut_cache = np.zeros((6, 256), dtype=np.float32)
        for i, mode in enumerate(MODE_ORDER):
            curve_y = self.calculate_curve(self.curves[mode])
            ... y_type == 'mult' ... y_type == 'shift' ...
    ```
  - 影响: 与 `update_lut`（690-707）整段重复，改 LUT 公式需同步两处；工程文件（外部数据）中的越界/非数值点未经 clamp 或类型检查直接进入曲线，`set_lut` 有 clamp 而 `set_curves_data` 没有，行为不对称。
  - 建议: 复用 `update_lut(emit_changed=False)`；恢复时对每点做 `x/y` 数值与 [0,255] 钳制，坏点回退默认。
  - 置信度: 高

- [AUD-38306] [Suggestion] `hsl_color_received` 信号全库无 emit、无 connect，为死代码
  - 位置: src/shared/ui/widgets/hsl_curves_widget.py:112
  - 证据:
    ```python
    # HSL Color Picker signals
    hsl_picker_toggled = pyqtSignal(bool)  # True = activate picker, False = deactivate
    hsl_color_received = pyqtSignal(float, float, float)  # r, g, b (0-255)
    ```
  - 影响: 全库 grep 仅此一处声明（`hsl_picker_toggled` 有实际使用，`hsl_color_received` 无）；取色结果实际经 `add_point_from_color` 直接调用，信号接口误导后续维护者。
  - 建议: 删除该信号，或把 `add_point_from_color` 改为经信号解耦（二选一，与现有架构对齐）。
  - 置信度: 高

- [AUD-38307] [Suggestion] 大量公开/事件方法缺类型注解
  - 位置: src/shared/ui/widgets/hsl_curves_widget.py:246-258, 484, 535, 603, 641, 647, 755, 762 等
  - 证据:
    ```python
    def to_widget(self, x_img, y_img):
    def from_widget(self, wx, wy):
    def calculate_curve(self, points):
    def mousePressEvent(self, event):
    def set_curves_data(self, data, emit_changed: bool = False):
    ```
  - 影响: 与 .clinerules 强类型要求不符；`set_curves_data(data)` 无类型约束使 AUD-38305 的输入风险更难发现。
  - 建议: 补 `-> tuple[float, float]`、`data: Dict[str, List[tuple[float, float]]]` 等注解。
  - 置信度: 高

### 通过项
- 信号防递归：mode_combo 程序化更新用 blockSignals 正确包裹（205-207）；changed 经 16ms 单发 QTimer 节流 + pending 缓冲；对外发射 `_lut_cache.copy()`，接收方改数组不污染缓存
- 状态提交一致性：set_curves_data / set_lut 程序化恢复默认不触发 changed（emit_changed 显式控制）；拖拽由 interaction_started/ended 成对包裹（右键删点、双击重置路径均成对）
- 线程安全：全部操作在 GUI 线程，无跨线程共享可变状态
- 输入验证（部分）：set_lut 校验形状 (6,256) 并 clamp y_ui 到 [0,255]；add_point_from_color 对 x_coord clamp 且做 3.0 最小间距去重
- 性能模式：LUT 脏标记缓存避免每帧重算；曲线绘制 300 采样点

---

## src/shared/ui/widgets/lut_gallery.py (行数: 327)

### 发现

- [AUD-38308] [Major] LUT 导入复制失败后仍把指向不存在路径的条目加入图库
  - 位置: src/shared/ui/widgets/lut_gallery.py:236-253
  - 证据:
    ```python
    try:
        base, ext = os.path.splitext(name)
        suffix = 1
        while os.path.exists(target_path):
            target_path = os.path.join(self.lut_dir, f"{base}_{suffix}{ext}")
            suffix += 1
        shutil.copy2(path, target_path)
        name = os.path.basename(target_path)
    except Exception as e:
        logger.error(f"to copy LUT: {e}")

    # Create Thumbnail Icon
    ...
    list_item.setData(Qt.ItemDataRole.UserRole, target_path)
    ```
  - 影响: copy2 失败（源盘拔出、目标只读等）时 `target_path` 指向磁盘上不存在的文件，但条目照常入列；用户点击后 `engine.apply_lut(path)` 返回 False，`color_controller.apply_lut_from_path` 静默不渲染、无任何用户反馈，重启后该 LUT 也会消失——外部数据未验证即进入 UI 数据流。
  - 建议: except 分支 return（不入列），并以 QMessageBox/状态栏提示导入失败。
  - 置信度: 中

- [AUD-38309] [Suggestion] 顶层 `import cv2` 未使用，重依赖拖慢模块导入
  - 位置: src/shared/ui/widgets/lut_gallery.py:2
  - 证据:
    ```python
    import os
    import cv2
    import shutil
    ```
  - 影响: 全文件无 cv2 引用；OpenCP/OpenCV 导入耗时约百毫秒级，凡 import 本模块的面板启动都被拖慢。
  - 建议: 删除该 import。
  - 置信度: 高

- [AUD-38310] [Suggestion] `_on_zoom_changed` 为无连接的死代码；tooltip 探测用裸 `except Exception: pass`
  - 位置: src/shared/ui/widgets/lut_gallery.py:269-270, 306-308
  - 证据:
    ```python
    def _on_zoom_changed(self, val: int) -> None:
        """Legacy zoom hook — the slider now drives ColumnGridController."""
        self._column_grid.apply_columns(val)
    ...
    except Exception:
        pass
    ```
  - 影响: `_on_zoom_changed` 全库无连接（zoom_slider 已由 ColumnGridController 接管），且无 getattr 保护，一旦在 `_column_grid` 创建前被调用即 AttributeError；tooltip 探测失败被完全吞掉，LUT 元数据损坏时无从排查。
  - 建议: 删除 legacy hook；tooltip except 至少 `logger.debug`。
  - 置信度: 高

### 通过项
- 信号防递归：intensity slider↔spin 双向同步用 `_intensity_updating` 状态锁，两个 handler 均先查锁，无回灌；`_on_reset_clicked`/`set_intensity` 程序化置值在锁内，不触发 intensity_changed
- 状态提交一致性：`set_selected_lut` 用 blockSignals 包裹程序化选择；lut_reset 语义独立于 intensity_changed
- 线程安全：全部 GUI 线程
- 性能模式：ColumnGridController 管列数布局，缩放走设置持久化 key
- 输入验证（部分）：import 重名自动加 `_N` 后缀避免覆盖

---

## src/shared/ui/widgets/param_slider_row.py (行数: 246)

### 发现

- [AUD-38311] [Suggestion] int() 截断造成 slider/drag 初始与步进量化偏差；部分方法缺注解
  - 位置: src/shared/ui/widgets/param_slider_row.py:65, 84-87, 126, 238-240
  - 证据:
    ```python
    self._scale = 100 if step < 1 else 1
    ...
    default_value=int(default_val * self._scale))
    ...
    self._slider.setValue(int(float_val * self._scale))
    ...
    def collect_rows(parent: QWidget) -> list:
    ```
  - 影响: `int()` 向零截断而非四舍五入：default=0.999 时 slider 初值 99 而 DragValue 显示 0.999，两控件相差 1 tick；负数区间（如 -0.5..0.5）截断方向不一致会放大抖动。`-> list` 缺元素类型。
  - 建议: 统一 `int(round(v * self._scale))`；注解改 `list[ParamSliderRow]`。
  - 置信度: 高

### 通过项
- 信号防递归：`_updating` 状态锁正确覆盖 slider→drag、drag→slider 双向同步及 `setValue` 程序化路径；程序化 setValue 不发射 valueChanged（符合"程序化更新不回灌"规范）
- blockSignals 覆写正确转发到两个子控件并返回 Qt 原语义
- 状态提交一致性：reset 使用 setDefaultValue 同步双控件默认值
- 线程安全 / GPU·GLSL·HDR：不涉及（纯控件）
- 性能模式：slider 自适应显隐有宽度阈值，避免无效渲染

---

## src/shared/ui/widgets/pref_drag_value.py (行数: 56)

### 发现

- [AUD-38312] [Suggestion] 跨层导入 export_panel 的私有常量，导入期拉起整个导出面板模块链
  - 位置: src/shared/ui/widgets/pref_drag_value.py:16
  - 证据:
    ```python
    from src.shared.export.export_panel import _VALUE_FIXED_WIDTH
    ```
  - 影响: 本模块 docstring 明确声称为避免偏好页重导入链而独立成模块，却在模块级 import 一个完整 UI 面板（export_panel 顶层导入 QtWidgets、numpy、整个 shared.ui.widgets 包、QMimeData 等），形成 ui→export 反向依赖；任何 import pref_drag_value 的代码（preferences_dialog）都被迫加载导出面板。当前无循环，但层次上脆弱。
  - 建议: 把 `_VALUE_FIXED_WIDTH = 64` 提为共享常量（如 drag_value.py 或 style_sheet 常量），删除对 export_panel 的依赖。
  - 置信度: 高

### 通过项
- 状态初始化顺序：`_special_text` 在 `super().__init__()` 之前赋值，规避 DragValue.__init__ 末尾 `_update_display → _format_value` 读未定义属性
- 与 DragValue 契约一致：`_min`/`_format_value`/`_update_display`/`setFixedValueWidth` 均存在于父类（已核实 drag_value.py:88,171,176,215）；setRange 后重新评估 special 显示
- intValue 用 round 而非截断
- 信号防递归 / GPU·GLSL·HDR / 线程安全：不涉及或无风险

---

## src/shared/ui/widgets/pref_form_layout.py (行数: 90)

### 发现

（无）

### 通过项
- 通过（无逻辑）：纯布局工厂——导出 `make_pref_form`（设置 QFormLayout 字段增长策略/标签对齐/margins/spacing）与 `make_pref_label`（固定宽度 270px 的 QLabel），无可变状态、无信号、无输入路径
- 类型注解完整（tuple[int, int, int, int] 等）

---

## src/shared/ui/widgets/qualifier_range_bar.py (行数: 283)

### 发现

- [AUD-38313] [Suggestion] `type` 参数遮蔽内置名且无合法值验证；setter 不校验外部传入的取值范围
  - 位置: src/shared/ui/widgets/qualifier_range_bar.py:15-17, 37-48
  - 证据:
    ```python
    def __init__(self, type="hue", parent=None):
        super().__init__(parent)
        self.type = type # "hue", "sat", "lum"
    ...
    def set_range(self, low, high, soft_low=0.0, soft_high=0.0):
        self.low = low
        self.high = high
    ```
  - 影响: 传入 "sat"/"lum" 以外的 type 被绘制与交互分支静默按 lum 处理；`set_range` 对 low>high、soft 越界或 NaN 无钳制，坏数据画出负宽度矩形（如 `QRectF(0, 0, low_x - sl_x, h)`）且 `range_changed` 语义崩坏。工程恢复属于外部数据，按 .clinerules 应验证。
  - 建议: 参数改名 `range_type` 并断言/回退合法值；setter 内 clamp 到 [0,1] 并强制 low ≤ high - 0.01。
  - 置信度: 中

- [AUD-38314] [Suggestion] 事件处理器与公共方法缺类型注解；使用 PyQt6 已弃用的 `event.pos()`
  - 位置: src/shared/ui/widgets/qualifier_range_bar.py:162, 197, 219, 233, 240
  - 证据:
    ```python
    def _get_handle_at(self, pos):
    ...
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.mouse_pressed = True
            self.active_handle = self._get_handle_at(event.pos())
    ```
  - 影响: `pos()` 自 Qt6 起标记弃用（应为 `event.position()`），未来 PyQt 升级可能移除；注解缺失不符合 .clinerules 强类型要求。
  - 建议: 统一改 `event.position().toPoint()` 或直接用 `position()`；补 `-> Optional[str]` 等注解。
  - 置信度: 高

### 通过项
- 信号防递归：程序化 `set_range`/`set_hue_params` 只重绘不发射信号；交互信号由 interaction_started/ended 成对包裹（press 发 started，release 必发 ended）
- 交互安全：拖拽值 clamp 到 [0,1]；low/high 互锁最小间隔 0.01；hue 环形差值正确处理 wrap（±0.5 折叠）
- 线程安全 / GPU·GLSL·HDR / 状态提交：纯 GUI 控件，状态由持有者负责持久化，本层无风险

---

## src/shared/ui/widgets/realtime_fps_indicator.py (行数: 176)

### 发现

- [AUD-38315] [Suggestion] 过期判定逻辑在 `is_realtime` 与 `_refresh_display` 重复；`_refresh_display` 的 now 参数无调用方使用
  - 位置: src/shared/ui/widgets/realtime_fps_indicator.py:63-73, 119-135
  - 证据:
    ```python
    stale_after = max(0.6, 2.0 / max(self._target_fps, 1.0))
    ...
    last_sample = self._samples[-1] if self._samples else None
    stale_after = max(0.6, 2.0 / max(self._target_fps, 1.0))
    ```
  - 影响: 同一 staleness 规则两处维护，改动阈值易不同步；`now` 形参仅测试用途但无默认调用方，接口意图含混。
  - 建议: 抽 `_stale_after()` / `_last_sample_age()` 私有助手；now 参数若仅测试用可标注或改由 now_fn 注入。
  - 置信度: 高

### 通过项
- 线程安全：`record_presented_frame` 位于预览绘制路径（GUI 线程），只操作小 deque，UI 文本/颜色更新由 500ms QTimer 节流，不阻塞播放热路径
- 输入验证：set_playback_state 对 target_fps 做 float() 异常捕获与正值检查，失败回退现值
- 状态一致性：起播清空样本/重置 token；停播停表并降级显示 "FPS —"；帧 token 去重防止同一帧重复计数
- 性能模式：1 秒滑动窗口 + popleft 裁剪，O(1) 摊销；setText/setStyleSheet 均有值缓存跳过

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 4 | AUD-38302, AUD-38303, AUD-38304, AUD-38308 |
| Suggestion | 12 | AUD-38300, AUD-38301, AUD-38305, AUD-38306, AUD-38307, AUD-38309, AUD-38310, AUD-38311, AUD-38312, AUD-38313, AUD-38314, AUD-38315 |
