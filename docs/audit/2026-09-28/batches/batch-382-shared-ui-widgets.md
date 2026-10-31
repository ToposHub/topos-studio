# 批次报告：batch-382 [P2] src/shared/ui/widgets/

- 审查日期：2026-09-28
- 编号区间：AUD-38200–AUD-38299（实际使用 AUD-38200–AUD-38222）
- 文件数：8（约 2,300 行），全部完整通读
- 汇总：Critical 1 · Major 5 · Suggestion 17
- 交叉验证：GpuScopeWidget（基类契约）、EyeDropperOverlay/get_color（src/shared/color/color_picker.py）、warper_panel/layer_controller/tools_panel/main_window/menu_toolbar_controller（调用方）、style_sheet/icon_manager（导入符号存在性）

---

## src/shared/ui/widgets/__init__.py (行数: 14)

### 发现

（无）

### 通过项
- 通过（无逻辑）：纯 re-export，导出 ChevronComboBox、ClickableSlider、ColorWarper、ColorPickerControl、CurvesWidget、DragValue、HSLCurvesWidget、LutGallery、QualifierRangeBar、ToggleSwitch、ToposClickableSlider、ParamSliderRow、VectorscopeWidget、WaveformWidget 共 14 个符号；无逻辑、无副作用。

---

## src/shared/ui/widgets/chevron_combo.py (行数: 119)

### 发现
- [AUD-38200] [Suggestion] 公共方法普遍缺少类型注解
  - 位置: src/shared/ui/widgets/chevron_combo.py:28,50,74,78,82,116
  - 证据:
    ```python
    def __init__(self, parent=None):
        super().__init__(parent)
        self._popup_open = False
    ...
    def paintEvent(self, event):
    ...
    def showPopup(self):
    ```
  - 影响: 与同目录 drag_value.py / color_picker_control.py 的强类型风格不一致，IDE/静态检查无法捕获误用。
  - 建议: 为 `__init__(self, parent: Optional[QWidget] = None)`、`paintEvent(self, event: QPaintEvent) -> None` 等补齐注解。
  - 置信度: 高

### 通过项
- 线程安全：仅 UI 线程 paintEvent/showPopup，无跨线程状态。
- GPU 资源生命周期：不涉及。
- 信号防递归：无自定义信号；`hidePopup` 正确复位 `_popup_open` 并重绘。
- 弹出定位：屏幕边界处理含 `self.screen()` None 守卫；`apply_combo_dropdown_style` 作用于 view（palette/spacing/QSS），`__init__` 的 `setStyleSheet` 设置在 combo 本体，二者不冲突（已核对 style_sheet.py:926-966）。
- lambda 闭包均用默认参数绑定（无迟绑定陷阱；本文件无 lambda，菜单闭包在 cie 文件已核）。

---

## src/shared/ui/widgets/cie_scope_widget.py (行数: 555)

### 发现
- [AUD-38201] [Suggestion] 光谱轨迹与色温曲线每次重绘全量重算，静态几何未预计算
  - 位置: src/shared/ui/widgets/cie_scope_widget.py:418-435, 499-503
  - 证据:
    ```python
    bezier_segments = catmull_rom_to_bezier(
        [(x, y) for _, x, y in points])
    samples_per_segment = 12
    ...
            color = wavelength_to_rgb(
                wl_start + (wl_end - wl_start) * f)
            if prev_px is not None:
                seg = self._clip_segment(prev_px, nxt, rect)
    ...
        for kelvin in range(t_start, t_end + 1, step):
            xy = locate(kelvin)
    ```
  - 影响: `_paint_scope_chrome` 在每次 paintGL 执行（播放期间随每帧 scope 更新触发）：约 32 段 × 12 采样点的 Bézier 求值 + 384 次 `wavelength_to_rgb`（含 QColor 构造）+ 每小段一次 QPen 分配，色温曲线约 614 次插值。这些输入全部是编译期常量表（CIE_SPECTRAL_LOCUS、色温拟合），每帧重复计算浪费 CPU，属可避免的每帧开销。
  - 建议: 将轨迹的 Bézier 段、采样点序列与波长颜色在模块级（或首次绘制后）缓存；色温曲线按 (curve, t_start, t_end, step) 缓存 xy 点列，绘制时仅做 `_xy_to_px` 投影。
  - 置信度: 高（重复计算确定；实测影响幅度未测量，故列 Suggestion）
- [AUD-38202] [Suggestion] `_px_to_xy` 为死代码
  - 位置: src/shared/ui/widgets/cie_scope_widget.py:205-210
  - 证据:
    ```python
    def _px_to_xy(self, px: QPointF) -> Tuple[float, float]:
        vx0, vx1, vy0, vy1 = self._view_window()
        rect = self._plot_dst_rect()
        u = (px.x() - rect.left()) / max(rect.width(), 1e-6)
        v = (px.y() - rect.top()) / max(rect.height(), 1e-6)
        return vx0 + u * (vx1 - vx0), vy1 - v * (vy1 - vy0)
    ```
  - 影响: 全库 grep 仅此一处定义、无任何调用（含子类）；类 docstring 明确"无缩放/平移交互"，该方法没有预期使用者，徒增维护面。
  - 建议: 删除，或在引入取值交互时再添加。
  - 置信度: 高

### 通过项
- 线程安全：chrome 纯 QPainter、仅 UI 线程；GL 部分完全委托基类 GpuScopeWidget 的 mutex tryLock 模式，本类未触碰 GL API。
- GPU 资源生命周期：本类不创建 GL 资源；`_paint_scope_chrome` 覆写发生在基类 blit 之后（gpu_scope_widget.py:266 调用点已核对），契约正确。
- 信号防递归：右键菜单 toggled → 状态 setter → `self.update()`，无信号回环。
- 数值正确性：五组色域 preset 基色坐标、Planckian/Daylight 拟合系数、wavelength_to_rgb 分段与 Bruton 因子均核对无误；Liang–Barsky 裁剪实现正确（p==0 退化分支处理齐全）。
- CIE_SPECTRAL_LOCUS 为 380→700nm 升序 10nm 表（33 点），`points[i][0]`/`points[i+1][0]` 波长插值索引边界安全（bezier 段数 = 32）。
- GLSL·HDR 正确性：不适用（纯展示 chrome，无 HDR 数据处理）。

---

## src/shared/ui/widgets/clickable_slider.py (行数: 32)

### 发现
- [AUD-38203] [Suggestion] `set_gradient_stops` 对 stops 无输入验证，异常输入生成非法 QSS 被静默忽略
  - 位置: src/shared/ui/widgets/clickable_slider.py:18-32
  - 证据:
    ```python
    def set_gradient_stops(self, stops):
        grad_str = "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
        for pos, color in stops:
            if isinstance(color, QColor):
                color_str = f"rgba({color.red()}, ..."
            else:
                color_str = color
            grad_str += f"stop:{pos} {color_str}, "
        grad_str = grad_str.rstrip(", ") + ")"
    ```
  - 影响: `stops` 为空列表时产出 `qlineargradient(x1:0, y1:0, x2:1, y2:0, )`（非法 QSS，Qt 仅在控制台告警，滑槽样式丢失）；`color` 既非 QColor 也非 str（如 tuple）时直接拼接进样式表，同样静默失效；`pos` 越界（<0 或 >1）不校验。
  - 建议: 入口校验 stops 非空、pos ∈ [0,1]、color 限定 QColor/str，否则提前 return 保留旧样式。
  - 置信度: 高（边界行为确定；实际调用方均传合法数据，故列 Suggestion）
- [AUD-38204] [Suggestion] 方法与参数缺少类型注解
  - 位置: src/shared/ui/widgets/clickable_slider.py:10,14,18
  - 证据:
    ```python
    def __init__(self, orientation=Qt.Orientation.Horizontal, default_val=0, parent=None):
    ...
    def mouseDoubleClickEvent(self, event):
    ...
    def set_gradient_stops(self, stops):
    ```
  - 影响: 同 AUD-38200。
  - 建议: 补齐 `default_val: float`、`stops: list[tuple[float, object]]`、返回值注解。
  - 置信度: 高

### 通过项
- 双击行为明确：先 `setValue(default_val)`（触发 Qt 内建 valueChanged）再发 `doubleClicked`，顺序无歧义。
- 信号防递归：无程序化联动路径，无递归风险。
- 无资源/线程/HDR 维度问题。

---

## src/shared/ui/widgets/color_picker_control.py (行数: 170)

### 发现
- [AUD-38205] [Critical] 吸管覆盖层在自身事件处理栈内丢失最后一个 Python 引用被立即析构（use-after-free 风险）
  - 位置: src/shared/ui/widgets/color_picker_control.py:145-150, 155-159, 169-170
  - 证据:
    ```python
    def _start_eyedropper(self) -> None:
        self._active_dropper = EyeDropperOverlay()
        self._active_dropper.colorPicked.connect(self._commit_color)
        self._active_dropper.cancelled.connect(self._clear_dropper)
        self._active_dropper.start()

    def _commit_color(self, color: QColor) -> None:
        ...
        finally:
            self._clear_dropper()

    def _clear_dropper(self) -> None:
        self._active_dropper = None
    ```
  - 影响: `EyeDropperOverlay` 是无父 QWidget（src/shared/color/color_picker.py:423-427，未设 WA_DeleteOnClose），`self._active_dropper` 是其唯一 Python 引用。取色时其 `mousePressEvent`（color_picker.py:448-456）先 `close()` 再 `colorPicked.emit(color)`，直连槽 `_commit_color` 在 finally 中把引用置 None → 引用计数归零，sip 在"该对象自己的事件处理器仍在调用栈上"时立即析构 C++ 对象——等价于在自身 event handler 内 `delete this`，Qt 文档明确为未定义行为：从 emit 返回后继续在已释放对象上执行事件分发，可导致崩溃（Esc 取消路径 `cancelled.emit()` → `_clear_dropper` 同理）。控件位于新建 Clip / Project Settings 关键路径，崩溃即丢用户工作。
  - 建议: `_clear_dropper` 中改为 `dropper = self._active_dropper; self._active_dropper = None; dropper.deleteLater()`（或给 overlay 设 `WA_DeleteOnClose` 由 Qt 接管销毁），确保析构发生在事件循环安全点。
  - 置信度: 中（引用丢失点与"自身 handler 内析构"确凿；是否每次都崩取决于堆/时机，但该模式是 Qt 明示的 UB）
- [AUD-38206] [Suggestion] scene-linear RGBA 在构造/刷新时即被量化为 8-bit sRGB，无用户交互也会损失 HDR 精度
  - 位置: src/shared/ui/widgets/color_picker_control.py:22-30, 73, 112-119
  - 证据:
    ```python
    def linear_rgba_to_qcolor(rgba: ...) -> QColor:
        r, g, b, a = rgba
        return QColor(
            max(0, min(255, int(round(linear_to_srgb(r) * 255)))),
    ...
    def set_rgba(self, rgba) -> None:
        self._color = linear_rgba_to_qcolor(rgba)
        self._render(self._color)
    ```
  - 影响: 控件构造或 `set_rgba` 后立即 `rgba()` 回读，即使从未打开拾色器，>1.0 的线性分量也被 clamp/量化到 8-bit；用户"打开拾色器后原样确认"同样经 QColor 往返降精度。若调用方用其承载 HDR 背景/边缘色，则违反"HDR 内部数据不得无理由 clamp"的精神（用于 sRGB 色块显示属合理，但回写路径使量化外溢到数据层）。
  - 建议: 保留原始线性值为权威状态（`self._linear`），QColor 仅作显示缓存；提交时用权威值合并 alpha，避免静默降精度。
  - 置信度: 中（量化路径确定；是否存在 HDR 取值调用方未证实——背景色场景通常 LDR，故列 Suggestion）

### 通过项
- 信号防递归/提交语义：`color_changed` 仅在确认拾色器或完成屏幕取色后发射，live 预览走 `_render` 不写状态，取消路径恢复 `initial` 显示且不污染 `self._color`（docstring 承诺与实现一致）。
- 异常与取消路径：`_pick_color` 用 try/finally 保证 `_restore_parent_window` 必然执行；`get_color` 返回 None 时正确还原。
- get_color 的 QEventLoop 模式独立于父窗口模态（已核对 color_picker.py:2710-2797），吸管可正常收事件。
- 类型注解齐全；i18n 使用 I18N.tr 规范。

---

## src/shared/ui/widgets/color_warper.py (行数: 266)

### 发现
- [AUD-38207] [Suggestion] `reset_grid`/`set_grid_size` 程序化路径同步 emit changed，控件不提供无信号变体
  - 位置: src/shared/ui/widgets/color_warper.py:28-31, 68-84
  - 证据:
    ```python
    def set_grid_size(self, size):
        self.grid_size = size
        self.reset_grid()
        self.update()
    ...
        # Emit identity deltas (all zeros) — no color change
        self.changed.emit(np.zeros((self.grid_size, self.grid_size, 2), dtype=np.float32))
    ```
  - 影响: 编程调用（恢复工程、切换密度）会以"用户变更"的面貌发射 `changed`。当前唯一调用方 warper_panel.py:81-94/99-111 已用 `blockSignals` 包裹防护，但这是调用方约定而非控件契约——新增调用方漏包即触发递归/伪变更（多余渲染、误标脏）。与 CurvesWidget 的 `emit_changed=False` 模式不一致。
  - 建议: 为 `reset_grid`/`set_grid_size`/`reset` 增加 `emit_changed: bool = True` 参数，把防递归锁内聚到控件。
  - 置信度: 高
- [AUD-38208] [Suggestion] 拖拽幅值上限 1.1 与 `apply_deltas` 的 sat 钳制 1.0 不一致，拖拽状态不能完整往返
  - 位置: src/shared/ui/widgets/color_warper.py:196-199 vs 261
  - 证据:
    ```python
    mag = math.sqrt(new_rel_x**2 + new_rel_y**2)
    if mag > 1.1:
        new_rel_x /= (mag/1.1)
        new_rel_y /= (mag/1.1)
    ...
    target_sat = max(0.0, min(init_sat + ds, 1.0))
    ```
  - 影响: 拖拽允许外圈点到 sat=1.1（`_compute_deltas` 产生的 ds 使 cur_sat 最高 1.1），而 undo/工程恢复走 `apply_deltas` 时 clamp 到 1.0——拖出界的状态在撤销重做/持久化重载后回弹，编辑会话与恢复状态不一致。
  - 建议: 统一两边边界（拖拽也钳到 1.0，或恢复时放宽到 1.1），任选其一保持往返一致。
  - 置信度: 高
- [AUD-38209] [Suggestion] `_padding` 在 paintEvent 赋值（35），命中测试的 fallback 默认却是 30，首帧前几何不一致
  - 位置: src/shared/ui/widgets/color_warper.py:92 vs 152, 186
  - 证据:
    ```python
    self._padding = 35          # paintEvent 内
    ...
    padding = getattr(self, '_padding', 30)   # _get_point_at / mouseMoveEvent
    ```
  - 影响: 首次 paintEvent 之前（控件已 show 未绘制、或 offscreen 场景）`_get_point_at`/拖拽换算使用 30 的边距，与实际绘制几何（35）差 2.5px 半径，命中判定与拖拽落点轻微偏移；同时"渲染状态在 paint 里副作用赋值"本身是反模式。
  - 建议: 把 `_padding = 35` 提为 `__init__` 中的实例常量，删去 getattr fallback。
  - 置信度: 高
- [AUD-38210] [Suggestion] 全文件方法缺少类型注解
  - 位置: src/shared/ui/widgets/color_warper.py:13,28,33,40,68,86,149,172,180,231,240,247,251
  - 证据:
    ```python
    def __init__(self, parent=None):
    ...
    changed = pyqtSignal(np.ndarray)
    ...
    def apply_deltas(self, deltas):
    ```
  - 影响: ndarray 形状约定（grid,grid,2）只存在于 docstring；`deltas` 参数无类型约束，易传错形状（运行期 numpy 广播错误而非静态发现）。
  - 建议: 补齐注解，`changed` 建议在 docstring 中固定形状契约或引入 TypedDict/ndarray 泛型说明。
  - 置信度: 高

### 通过项
- 信号节流：16ms 单发 `_changed_timer` + `_pending_deltas` 只发布最新采样；`mouseReleaseEvent` 立即停表冲刷，交互收尾不丢尾帧。
- `reset_grid` 显式停止挂起定时器并清 `_pending_deltas`，防止复位后发布陈旧拖拽样本（注释明确、实现正确）。
- interaction_started/ended 严格配对：仅在命中控制点时发 started，release 必发 ended。
- hue 环绕（±0.5 折叠）与 shader 编码逆变换 `_hue_from_pos` 互逆自洽。
- 线程安全/GPU 生命周期：纯 QWidget + QPainter，无 GL 调用、无跨线程状态。

---

## src/shared/ui/widgets/curves_widget.py (行数: 749)

### 发现
- [AUD-38211] [Major] 左键点击空白处（未加点）时 interaction_started 无配对 interaction_ended，undo/空闲渲染状态被破坏
  - 位置: src/shared/ui/widgets/curves_widget.py:355-363 vs 422-425
  - 证据:
    ```python
    if event.button() == Qt.MouseButton.LeftButton:
        self.interaction_started.emit()          # 无条件发射
        if clicked_idx != -1:
            self.selected_point_idx = clicked_idx
        else:
            rect = self.get_draw_rect()
            if rect.adjusted(-5, -5, 5, 5).contains(pos):   # 矩形外则什么都不做
    ...
    def mouseReleaseEvent(self, event):
        if self.selected_point_idx != -1:        # 仅选中时补 ended
            self.interaction_ended.emit()
        self.selected_point_idx = -1
    ```
  - 影响: 左键点在放宽矩形之外的空白处（画布边缘/margin 区）：started 已发而 selected_point_idx 保持 -1，release 不发 ended。消费方 menu_toolbar_controller.py:1607-1625 的 `on_interaction_started` 每次都 `idle_render_timer.stop()` + `engine.set_interaction_mode(True)` + `history.push_state(...)`，`on_interaction_ended` 才重启定时器并 `_trigger_autosave()`——失衡后果：每次空点击压入一个多余 undo 快照，且空闲全质量渲染定时器与交互模式卡在"交互中"，直到下一次完整交互结束才恢复。
  - 建议: 将 `interaction_started.emit()` 移到确认会改变状态之后（命中点 / 成功加点），或 release 无条件与 press 配对发射 ended。
  - 置信度: 高
- [AUD-38212] [Major] set_lut 的 `emit_changed=True` 路径被自身 Qt 级 blockSignals 完全吞掉，信号永不送达
  - 位置: src/shared/ui/widgets/curves_widget.py:588-640（关键 632-638）
  - 证据:
    ```python
    was_blocked = self.blockSignals(True)      # QWidget 级：阻断本对象一切信号
    try:
        ...
        self.update_lut(emit_changed=emit_changed)   # 已启动 16ms 定时器并暂存 pending
        self.update()
        if emit_changed and self._final_lut_cache is not None:
            self._pending_lut = self._final_lut_cache.copy()
            self._pending_data = self.get_curves_data()
            self._emit_changed_signal()         # changed.emit 被阻断，但 pending 被清空
    finally:
        self.blockSignals(was_blocked)
    ```
  - 影响: `_emit_changed_signal` 内 `self.changed.emit(...)` 在 blockSignals(True) 作用域内被 Qt 丢弃，而 `_pending_lut = None` 照常执行；此前 `update_lut` 启动的定时器 16ms 后重放时 pending 已空，再次空转。净效果：`set_lut(lut, emit_changed=True)` 从不发射 changed，参数文档化行为静默失效（当前调用方 layer_controller.py:47、tools_panel.py:217 均用默认 False，故为潜伏缺陷而非现行故障）。
  - 建议: 移除 set_lut 内的 blockSignals 包裹（改为显式不调 emit 路径），或把直接发射移到 finally 解除阻断之后。
  - 置信度: 高
- [AUD-38213] [Major] mouseDoubleClickEvent 未检查鼠标按钮，右键/中键双击会意外重置当前通道曲线
  - 位置: src/shared/ui/widgets/curves_widget.py:428-434
  - 证据:
    ```python
    def mouseDoubleClickEvent(self, event):
        self.interaction_started.emit()
        self.curves[self.active_channel] = [QPointF(0, 255), QPointF(255, 0)]
        self.selected_point_idx = -1
        self.update_lut()
        self.update()
        self.interaction_ended.emit()
    ```
  - 影响: Qt 对任意按钮的双击都会调用本方法。右键在本控件是"删除控制点"专用键（mousePressEvent:372-380），用户快速删除两个点即构成右键双击 → 整条活动通道曲线被重置（多点精细调整丢失，虽可 undo 但属数据损失路径）；中键双击同样触发。对照 drag_value.py:370-373 有 `event.button()` 检查，本处遗漏。
  - 建议: 增加 `if event.button() != Qt.MouseButton.LeftButton: return`。
  - 置信度: 高
- [AUD-38214] [Major] set_lut 文档声明支持 (256, 3) 形状但代码未实现，旧工程恢复静默丢失曲线
  - 位置: src/shared/ui/widgets/curves_widget.py:573-575 vs 591, 604
  - 证据:
    ```python
    Args:
        lut: LUT array of shape (256, 1, 3), (256, 3), or (256, 1)
    ...
    if lut.ndim == 2 and lut.shape[1] == 1:
        ...
    elif lut.ndim == 3 and lut.shape[1] == 1 and lut.shape[2] == 3:
    ```
  - 影响: `(256, 3)`（ndim==2 且 shape[1]==3）不满足任一分支，函数静默落到"标记脏 + 按默认直线重建"，调用方（layer_controller.py:46-47 仅存 LUT 的旧工程回退路径）不会得到任何错误提示，调色恢复为identity曲线——静默的恢复错误。
  - 建议: 增加 `elif lut.ndim == 2 and lut.shape[1] == 3:` 分支（等价于 lut[:, :] 逐通道），或在无法识别形状时记 log 并显式返回失败。
  - 置信度: 高
- [AUD-38215] [Major] calculate_curve 结尾裸 `except:` 掩盖插值领域错误并静默降级
  - 位置: src/shared/ui/widgets/curves_widget.py:336-337
  - 证据:
    ```python
            else:
                return np.interp(xi, xs, ys)
        except:
            return np.interp(np.arange(256), xs, ys)
    ```
  - 影响: 捕获一切异常（含 KeyboardInterrupt/SystemExit）：PCHIP 构造失败、xs 含 NaN、内存错误等都被吞掉，曲线悄悄退化为线性插值——用户看到"曲线拉不直/颜色不对"却无任何日志，渲染结果错误且不可诊断。违反 .clinerules"不得宽泛 except 掩盖领域错误"。
  - 建议: 至少 `except Exception as exc: logger.warning(...)`，并考虑只捕获预期数值异常（如 ValueError）。
  - 置信度: 高
- [AUD-38216] [Suggestion] set_curves_data 不验证外部工程数据
  - 位置: src/shared/ui/widgets/curves_widget.py:547-551
  - 证据:
    ```python
    for ch in self.channels:
        if ch in data:
            self.curves[ch] = [QPointF(x, y) for x, y in data[ch]]
        else:
            self.curves[ch] = [QPointF(0, 255), QPointF(255, 0)]
    ```
  - 影响: `layer.curves_data` 来自工程文件（layer_controller.py:44-45 无 try 包裹直接调用）：畸形数据（非 2 元组 → ValueError 上抛打断工程加载；y 超出 [0,255]、少于 2 点、NaN 不校验）违反"外部数据须验证"。少于 2 点时 calculate_curve 虽有常量填充兜底，但黑/白端点约束逻辑（mouseMoveEvent:396-408 假设 index 0/-1 为端点）会失配。
  - 建议: 恢复前过滤：数值 clip 到 [0,255]、按 x 排序、不足 2 点补端点、非数值项跳过并记 log。
  - 置信度: 中（上游可能已有校验未证实；本控件自身确无验证）
- [AUD-38217] [Suggestion] add_point_from_color 内 `import colorsys` 未使用
  - 位置: src/shared/ui/widgets/curves_widget.py:685
  - 证据:
    ```python
        import colorsys

        ch = self.active_channel

        # Calculate X coordinate based on active channel
        if ch == 'M':
    ```
  - 影响: 死导入；BT.601 亮度在下方手工计算（693 行），colorsys 从未被引用。
  - 建议: 删除该 import。
  - 置信度: 高
- [AUD-38218] [Suggestion] 拖拽的相邻点约束可矛盾，后写入的 min 覆盖 max，点序可被瞬时破坏
  - 位置: src/shared/ui/widgets/curves_widget.py:411-414
  - 证据:
    ```python
    # Constraints: avoid swapping order
    if self.selected_point_idx > 0:
        mx = max(mx, pts[self.selected_point_idx-1].x() + 0.1)
    if self.selected_point_idx < len(pts) - 1:
        mx = min(mx, pts[self.selected_point_idx+1].x() - 0.1)
    pts[self.selected_point_idx] = QPointF(mx, my)
    ```
  - 影响: 加点无最小间距检查（mousePressEvent 任意位置可加），当相邻两点间距 < 0.2 时 `prev+0.1 > next-0.1`，最终 mx 取 next-0.1 < prev+0.1——被拖点越过左邻，列表顺序与索引假设（index 0 为黑点、-1 为白点的端点特判）失配；曲线绘制因内部 sorted 兜底不至于画错，但端点吸附逻辑可能作用在错误点上。
  - 建议: 约束后再次校验 `mx` 区间有效性（区间为空时保持原 x），或加点时强制最小间距。
  - 置信度: 中（需要密集点才触发，路径确定但触发条件较窄）
- [AUD-38219] [Suggestion] paintEvent 每帧对 4 个通道全量重算 calculate_curve，未复用未脏通道缓存
  - 位置: src/shared/ui/widgets/curves_widget.py:166-172, 226
  - 证据:
    ```python
    for ch in self.channels:
        if ch == self.active_channel:
            continue
        self._draw_curve(painter, ch, inactive_colors[ch], rect, thickness=1.0)
    ...
    vals = self.calculate_curve(points)
    ```
  - 影响: `_draw_curve` 每次都重建 PCHIP 并求值 256 点（含 300 采样绘线），而 `_lut_cache[ch] = (255.0 - curve_y) / 255.0` 已缓存同源数据且带脏标记——拖拽期间每帧 4 次冗余插值。numpy 规模小、实测影响有限，故列 Suggestion。
  - 建议: `_draw_curve` 优先从 `_lut_cache[ch]` 反推 `curve_y = 255*(1-lut)`（未脏时），或为绘制建独立脏缓存。
  - 置信度: 高（冗余确定；幅度未测量）
- [AUD-38220] [Suggestion] 多数交互/数据方法缺少类型注解
  - 位置: src/shared/ui/widgets/curves_widget.py:119,123,127,133,294,339,384,422,428,528,535 等
  - 证据:
    ```python
    def set_active_channel(self, ch):
    ...
    def calculate_curve(self, points):
    ...
    def set_curves_data(self, data, emit_changed: bool = False):
    ```
  - 影响: `data`（嵌套 dict）、`points`（QPointF 列表）等关键参数无类型契约。
  - 建议: 补齐 `-> None` / 参数类型。
  - 置信度: 高

### 通过项
- HDR 语义：`_compute_lut` 对主曲线映射超出 [0,255] 的输入用端点斜率线性外推（mask_low/high 分支），不 clamp——符合"HDR 内部数据不得无理由 clamp"，且注释明示设计意图。
- LUT 节流：16ms 单发 QTimer + pending 暂存，`update_lut(emit_changed=False)` 清 pending 并停表（程序化恢复静默），`flush()` 同步冲刷供 apply-grade 场景——时序设计自洽。
- 右键删点路径 started/ended 显式配对（372-380）。
- 脏缓存一致性：`reset`/`set_curves_data`/`set_lut` 均正确标记全通道脏并失效 `_final_lut_cache`。
- 线程安全：无 GL 调用、无跨线程状态（信号节流依赖 Qt 事件循环，均在 UI 线程）。
- GPU 资源生命周期：不涉及。

---

## src/shared/ui/widgets/drag_value.py (行数: 395)

### 发现
- [AUD-38221] [Suggestion] blockSignals 覆盖遮蔽 QWidget.blockSignals，语义与 Qt 契约不符
  - 位置: src/shared/ui/widgets/drag_value.py:199-202
  - 证据:
    ```python
    def blockSignals(self, block: bool) -> bool:
        old = self._block_signals
        self._block_signals = block
        return old
    ```
  - 影响: 该覆盖只抑制 `valueChanged`（Python 发射路径），不会阻断本对象其他 Qt 信号，也不会被 C++ 侧调用感知（blockSignals 非虚函数）。调用方（如 color_controller.py:1217-1230 对 `*_drag` 控件的使用）若未来依赖 Qt 语义（例如期望 interaction_started 也被静默、或期望 Qt 内部行为联动），将与实际行为偏离。当前用法恰好只需 valueChanged 静默，故无现行缺陷。
  - 建议: 改名为 `setValueSilent`/`blockValueChanged` 之类的显式 API，避免与 QObject API 同名。
  - 置信度: 高
- [AUD-38222] [Suggestion] setRange/setSingleStep 不验证退化输入
  - 位置: src/shared/ui/widgets/drag_value.py:206-213
  - 证据:
    ```python
    def setRange(self, min_v: float, max_v: float):
        self._min = float(min_v)
        self._max = float(max_v)
        self.setValue(self._value)  # 重新 clamp
    ...
    def setSingleStep(self, step: float):
        self._single_step = float(step)
    ```
  - 影响: `min_v > max_v` 时 `max(min, min(max, v))` 恒返回 min，控件锁死在 min 且无提示；step ≤ 0 使滚轮失效。
  - 建议: setRange 内交换/拒绝逆序区间（或 assert + log），setSingleStep 拒绝非正值。
  - 置信度: 高（边界行为确定；未见实际传入逆序区间的调用方，故列 Suggestion）

### 通过项
- 交互信号配对：拖拽越过 3px 阈值才发 started，释放仅 engaged 时发 ended；滚轮一次完整交互（started+ended 包住 valueChanged），undo 分组语义明确（注释明示）；内联编辑提交同样配对。
- 内联编辑状态机：Esc 取消（eventFilter 拦截并独占返回 True）、Enter/returnPressed/editingFinished 双路径提交有 `_editing` 守卫防双提交；全局点击外部提交用 `mapFromGlobal`+geometry 判定且不吞事件（return False）。
- 事件过滤器泄漏：`_begin_edit` 安装、`_end_edit` 移除，QApplication None 守卫到位；控件销毁时 QObject 析构自动注销过滤器。
- 输入解析：逗号→点号替换 + QLocale(Language.C) 固定小数点 + QDoubleValidator + ValueError 兜底回退当前值，越界值 clamp 后提交。
- 拖拽速率：`drag_speed_per_pixel` 为纯函数（固定 200px 参考宽度，三档修饰键 + 微调下限规则与注释一致），可独立测试。
- 去重发射：1e-10 容差避免重置同值时误发 valueChanged。
- 线程安全/GPU/HDR：不涉及。

---

## 批次汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-38205 |
| Major | 5 | AUD-38211, AUD-38212, AUD-38213, AUD-38214, AUD-38215 |
| Suggestion | 17 | AUD-38200–38204, 38206–38210, 38216–38222 |

Critical 集中在 color_picker_control.py 的吸管覆盖层生命周期；5 个 Major 全部位于 curves_widget.py（交互信号失衡、set_lut 双重缺陷、双击按钮遗漏、裸 except）。
