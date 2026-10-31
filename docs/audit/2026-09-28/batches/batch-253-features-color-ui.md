# batch-253 审查报告 — src/features/color/ui//

任务书：docs/audit/2026-09-28/assignments/batch-253.md
编号区间：AUD-25300–AUD-25399
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解

统计：Critical 0 · Major 3 · Suggestion 16

---

## src/features/color/ui/color_mapping_panel.py (行数: 69)

### 发现
- [AUD-25318] [Suggestion] 本地手写 QTabBar 样式绕过共享主题常量 TAB_STYLE
  - 位置: src/features/color/ui/color_mapping_panel.py:37-49
  - 证据:
    ```python
    self.tabs.setStyleSheet(
        """
        QTabBar::tab {
            padding: 5px 15px;
            background: #222;
            color: #888;
        }
        QTabBar::tab:selected {
            background: #333;
            color: #cc6600;
        }
        """
    )
    ```
  - 影响: `src/shared/ui/style_sheet.py` 已提供 `TAB_STYLE`（同仓库其他面板经 `C`/`TAB_STYLE` 统一取色），此处硬编码 `#222/#333/#cc6600` 会随主题演进产生视觉漂移与双份维护。
  - 建议: 改用 `from src.shared.ui.style_sheet import TAB_STYLE`（或基于 `C` 常量生成），删除本地色值。
  - 置信度: 高

### 通过项
- 无自有信号/槽逻辑，纯容器装配（三个子面板由外部注入），无递归风险
- 构造参数与返回值类型注解完整
- 无 GPU/GLSL/线程相关代码
- i18n 标签经 I18N.tr 包装

---

## src/features/color/ui/color_status_bar.py (行数: 248)

### 发现
- [AUD-25300] [Major] 降噪档位轮询用宽泛 `except Exception` 静默吞掉 provider 错误
  - 位置: src/features/color/ui/color_status_bar.py:152-159
  - 证据:
    ```python
    def _poll_denoise_quality(self) -> None:
        provider = self._denoise_quality_provider
        quality = None
        if provider is not None:
            try:
                quality = provider()
            except Exception:
                quality = None
        if quality != self._denoise_quality:
    ```
  - 影响: 违反铁律「不得宽泛 except 掩盖领域错误」。provider 回调（读引擎/管理器单例）一旦持续抛错，每 500ms 被无痕吞掉，状态栏降噪档位永久消失且无任何日志可查，排障时完全不可见。
  - 建议: 至少记录一次告警日志（如 `logger.exception` + 首次告警去重），或收窄为预期异常类型；持续失败时可在 UI 上显示降级标记。
  - 置信度: 高

- [AUD-25301] [Suggestion] 500ms 轮询定时器无条件常驻，无 provider / 不可见时仍在空转
  - 位置: src/features/color/ui/color_status_bar.py:138-143
  - 证据:
    ```python
    from PyQt6.QtCore import QTimer
    self._poll_timer = QTimer(self)
    self._poll_timer.setInterval(500)
    self._poll_timer.timeout.connect(self._poll_denoise_quality)
    self._poll_timer.start()
    ```
  - 影响: provider 未注入或窗口隐藏时仍以 2Hz 唤醒并调用外部单例读取，属于无意义的常驻唤醒（对笔记本功耗/事件循环有轻微浪费）。
  - 建议: `set_denoise_quality_provider(None)` 时停表；或在 `hideEvent/showEvent` 中暂停/恢复轮询。
  - 置信度: 高

- [AUD-25302] [Suggestion] 类型注解缺失：`format_provider_badge` 无返回类型、provider 回调无类型
  - 位置: src/features/color/ui/color_status_bar.py:47-47, 147-149
  - 证据:
    ```python
    def format_provider_badge(diag: Optional[dict]):
    ```
    ```python
    def set_denoise_quality_provider(self, provider) -> None:
    """注入降噪档位探测回调（返回 'preview'/'paused'/'export'/None）。"""
    ```
  - 影响: docstring 已明确返回 `(text, warning, tooltip)` 三元组和回调返回域，但签名未表达，IDE/静态检查无法校验调用方。
  - 建议: 补 `-> tuple[str, bool, str]`；provider 用 `Callable[[], Optional[str]]`。
  - 置信度: 高

### 通过项
- 状态锁/防递归：所有数据入口仅做赋值 + `_refresh()`，无信号回环
- 异常路径：provider 回调有兜底（除 AUD-25300 的吞错方式外不会崩溃）
- paintEvent 正确创建/结束 QPainter 并转交 super()
- 无 GPU/GLSL/持久化代码；外部 diag dict 有 isinstance 校验
- i18n：用户可见文案均经 I18N.tr

---

## src/features/color/ui/color_wheel.py (行数: 1259)

### 发现
- [AUD-25303] [Major] `_rgb_to_hsv` 用 clamp(0,1) 截断归一化色度，RGB 数值框回读会折叠饱和度
  - 位置: src/features/color/ui/color_wheel.py:392-403
  - 证据:
    ```python
        # 归一化色度
        nr = r / b_mult
        ng = g / b_mult
        nb = b / b_mult

        # 使用 QColor 的 HSV 转换提取 hue + sat
        c = QColor.fromRgbF(
            max(0.0, min(1.0, nr)),
            max(0.0, min(1.0, ng)),
            max(0.0, min(1.0, nb)))
        hue = float(c.hueF()) * 360.0 if c.hueF() >= 0 else 0.0
        sat = float(c.saturationF())
    ```
  - 影响: 正向 `_update_rgb_values`（line 327-328）产出的是除以均值的色度 `r = (rf/avg)*b_mult`，饱和色分量可 >1（hue=0/sat=0.5 → 显示值 1.5/0.75/0.75，其中 nr=1.5）。反向把 1.5 直接 clamp 到 1.0 而非按最大分量等比缩放，比例被破坏：实测 hue=0、sat=0.5 的状态在触碰（拖动/输入）任一 RGB 框后回读为 sat=0.25——饱和度减半，色轮 puck 内跳，调色参数被错误改写并经 grading_changed 提交到渲染链。这正是铁律禁止的「无理由 clamp」在 UI 换算层的体现。
  - 建议: 反向改为等比归一化：`m = max(nr, ng, nb, 1e-6); c = QColor.fromRgbF(nr/m, ng/m, nb/m)`（HSV 对统一缩放不变），删除 clamp。
  - 置信度: 高

- [AUD-25304] [Suggestion] RGB 数值框上限 10.0 会在高亮度/高饱和时截断正向显示
  - 位置: src/features/color/ui/color_wheel.py:95-97
  - 证据:
    ```python
    self._bright_drag = _make_box(-1.0, 1.0, 0.0, 2, '#bbbbbb')
    self._r_drag      = _make_box(0.0, 10.0, 1.0, 2, '#cc3333')
    self._g_drag      = _make_box(0.0, 10.0, 1.0, 2, '#33aa33')
    self._b_drag      = _make_box(0.0, 10.0, 1.0, 2, '#3366cc')
    ```
  - 影响: 正向公式 `b_mult = 2^(brightness²·6)` 最高 64，纯红 sat=1 时显示值 r = 3×b_mult；brightness≈0.58 起即超过 10.0，DragValue.setValue（已核实按 min/max clamp）会把显示值钉在 10.0，此时用户再触碰 RGB 框将回读出失真状态（与 AUD-25303 叠加）。
  - 建议: 放宽 RGB 框上限（如 ≥200 以覆盖 3×64），或按比例缩放显示并标注倍率。
  - 置信度: 高

- [AUD-25305] [Suggestion] `set_hsv` 未清空/停止每轮 16ms 合并定时器的 pending 值（reset 有做）
  - 位置: src/features/color/ui/color_wheel.py:205-222
  - 证据:
    ```python
    def set_hsv(self, hue: float, sat: float, brightness: float):
        """从后端引擎设置 Hue/Sat/Brightness 值，更新 UI 控制点位置"""
        self.hue = hue
        self.sat = sat
        self.brightness = brightness

        self._update_knob_position()
    ```
    对比 `reset()`（line 182-186）明确 `self._update_timer.stop()` 并置 `self._pending_hue = None` 等。
  - 影响: 外部更新（tangent_bridge 的 `set_wheel_params`、layer_controller 的 `set_params`，均已核实存在）落在鼠标拖拽 16ms 窗口内时，残留 pending 会让 `changed` 信号再发射一次旧参数。当前因 `GradingPanel._emit_grading_changed` 重读控件实时状态（忽略信号参数）而无数据损坏，仅多一次冗余发射；但任何直接消费 `changed(hue, sat, val)` 参数的外部接线都会收到过期值，属 fragile 时序。
  - 建议: `set_hsv` 开头与 `reset` 一致：停 `_update_timer`、清空三个 `_pending_*`。
  - 置信度: 中

- [AUD-25306] [Suggestion] 防递归守卫错位：`_update_rgb_values` 只抬 `_updating_rgb`，但 `_bright_drag` 的处理器检查的是 `_updating_brightness`
  - 位置: src/features/color/ui/color_wheel.py:284-287, 314-335
  - 证据:
    ```python
    def _on_brightness_drag_changed(self, val: float):
        """亮度 DragValue 变化 → 同步滑动条 + 内部状态。"""
        if self._updating_brightness:
            return
    ```
    ```python
        self._updating_rgb = True
        self._bright_drag.setValue(brightness)
        self._r_drag.setValue(r)
        self._g_drag.setValue(g)
        self._b_drag.setValue(b)
        self._updating_rgb = False
    ```
  - 影响: 程序化 `_bright_drag.setValue()`（如滑块拖动触发的 `_update_rgb_values`）会重入 `_on_brightness_drag_changed` 一层：重设滑块、重算 RGB、再排一次 pending 发射。目前靠 DragValue「值不变则不发信号」（已核实 setValue 有 `abs(v-self._value)<1e-10: return`）收敛终止，属隐性依赖子控件实现的防递归。
  - 建议: `_on_brightness_drag_changed` 同时检查 `self._updating_rgb`，或 `_update_rgb_values` 内一并抬起 `_updating_brightness`。
  - 置信度: 高

- [AUD-25307] [Suggestion] 死代码：`_draw_bezel`、`_draw_polar_grid`、`_pending_grading_params` 无任何调用/读取
  - 位置: src/features/color/ui/color_wheel.py:499-512, 559-579, 851-851
  - 证据:
    ```python
    def _draw_bezel(self, painter: QPainter, center: QPointF, radius: float):
    ```
    ```python
    def _draw_polar_grid(self, painter: QPainter, center: QPointF, radius: float):
    ```
    ```python
        self._pending_grading_params = None
    ```
    （paintEvent 只调用 `_draw_title/_draw_color_wheel/_draw_hue_markers/_draw_wheel_edge/_draw_center_crosshair/_draw_puck_connection/_draw_puck`；`_pending_grading_params` 仅在 `__init__/set_params/reset_all` 被置 None，从未被读取。）
  - 影响: 无功能影响，但增加维护噪声、误导读者以为存在 bezel/网格绘制与 pending 参数合并逻辑。
  - 建议: 删除两个未调用的绘制方法与未读状态变量（或接入 paintEvent 完成设计意图）。
  - 置信度: 高

- [AUD-25308] [Suggestion] 浮点信号用 int 字面量发射、float 参数默认值写 `0`
  - 位置: src/features/color/ui/color_wheel.py:200, 1090-1091
  - 证据:
    ```python
    self.changed.emit(0, 0, 0)
    ```
    ```python
    def set_params(self, lift: tuple, gamma: tuple, gain: tuple,
                   exp: float, temp: float, tint: float, contrast: float,
                   shadow: float, sat: float, vibrance: float = 0,
                   mid_det: float = 0, hue: float = 0, emit: bool = False):
    ```
  - 影响: `changed = pyqtSignal(float, float, float)` 传 int 在 PyQt6 中可隐式转换，不崩溃，但违反「浮点字面量显式 .0」的项目约定，且削弱对签名精度的自查。
  - 建议: 改为 `changed.emit(0.0, 0.0, 0.0)`；默认值写 `0.0`。
  - 置信度: 高

- [AUD-25309] [Suggestion] `get_current_params` 硬编码 `highlights/sharpness` 为 0，保存路径可能清零他处设置的值
  - 位置: src/features/color/ui/color_wheel.py:1230-1245
  - 证据:
    ```python
        return {
            "lift": lift,
            ...
            "highlights": 0,  # Not exposed in GradingPanel
            "saturation": sat,
            "vibrance": vibrance,
            "mid_detail": mid_det,
            "sharpness": 0,  # Not exposed in GradingPanel
            "hue_shift": hue,
        }
    ```
  - 影响: `edit_controller` 的 save 回调与 `color_stack_hydrator` 的旧版回退路径（均已核实调用 `get_current_params`）会把本面板未暴露的两键固定写成 0；若调色栈/图层先前带有非零 highlights/sharpness（由其他控制路径写入），经此面板触发保存即被静默清零。当前 hydrator 主路径优先取引擎层数据，故风险集中在回退/保存路径。
  - 建议: 保存时对未暴露的键不写入（保留原值），或由控制器合并而非整体覆盖。
  - 置信度: 中

- [AUD-25310] [Suggestion] 类型注解缺失：`paintEvent/reset/set_hsv/_make_box` 等多处无参数/返回注解
  - 位置: src/features/color/ui/color_wheel.py:174-176, 178-203, 205-222, 86-92
  - 证据:
    ```python
    def paintEvent(self, event):
        """不绘制任何内容 — 由 _WheelCanvas 子控件负责。"""
        pass

    def reset(self):
    ```
  - 影响: 与仓库「所有函数有类型注解」约束不一致；`_rgb_to_hsv` 的返回也仅写 `tuple` 未参数化。
  - 建议: 补齐 `-> None` 与参数注解；`_rgb_to_hsv` 返回改 `tuple[float, float, float]`。
  - 置信度: 高

### 通过项
- 线程安全：纯 UI 控件，无 OpenGL 调用，无跨线程数据
- 信号节流设计正确：16ms single-shot 合并 + `interaction_ended`/`mouseReleaseEvent` 前强制 flush（line 307-312, 742-752），取消路径（reset）亦停表并清 pending
- `set_params/reset_all/set_keyframe_state/set_wheel_params` 均正确使用 blockSignals + `_syncing_ui` 状态锁防递归；`ColorWheel.blockSignals` 覆盖子控件
- `set_wheel_params` 对非法 wheel 名/值长度/非数值显式 raise ValueError（输入验证到位）
- 交互配对完整：interaction_started/ended 在滑块、拖拽框、画布三路均有源
- GPU 资源：无 ModernGL 对象；paintEvent 均在 QPainter 栈内完成

---

## src/features/color/ui/crop_panel.py (行数: 555)

### 发现
- [AUD-25311] [Major] Esc「取消」与 Enter「应用」行为完全相同——取消路径实际提交裁切
  - 位置: src/features/color/ui/crop_panel.py:382-392, 429-448
  - 证据:
    ```python
    def _on_apply_clicked(self):
        """Handle apply crop button click (Enter key)."""
        if not self.crop_active:
            return
        # Apply crop and deactivate crop tool (same as clicking the crop button)
        self._set_crop_active_internal(False)

    def _on_cancel_clicked(self):
        """Handle cancel (Esc key) - deactivate crop tool."""
        if self.crop_active:
            self._set_crop_active_internal(False)
    ```
    而 `_set_crop_active_internal(False)` 在 line 438-440 会 `self.crop_requested.emit(self.get_params())`。
  - 影响: 模块 docstring 与 eventFilter 注释承诺「Esc: cancel」，但 Esc 走的仍是「先 emit crop_requested 再关闭」的提交流程。用户按 Esc 想放弃当前裁切/旋转调整时，裁切效果反而被应用到节点状态，与业界惯例和自身文档相反，属取消路径功能缺陷。
  - 建议: 为取消实现独立路径：不 emit `crop_requested`，直接复位激活状态并（如需）恢复进入裁切前的参数快照；若产品语义确为「关闭即提交」，则更正文档并去掉 eventFilter 中 apply/cancel 的二元暗示。
  - 置信度: 中（代码行为确凿；产品意图二选一，需产品确认）

- [AUD-25312] [Suggestion] 死处理器 `_on_rotation_changed` 引用不存在的 `self.rot_value_label`，一旦接线即 AttributeError
  - 位置: src/features/color/ui/crop_panel.py:360-363, 336-338
  - 证据:
    ```python
    def _on_rotation_changed(self, value):
        """Handle rotation slider change."""
        self.rot_value_label.setText(f"{value}°")
        self.params_changed.emit(self.get_params())
    ```
    （全文件 grep 无 `rot_value_label` 赋值、无 `_on_rotation_changed` 连接；实际接线的是 `_on_rot_changed`。`_setup_action_buttons` 为空 pass 且无调用。）
  - 影响: 当前未连接故不触发；遗留代码若被重新接上会立刻崩溃，且误导维护者以为存在旋转数值标签。
  - 建议: 删除 `_on_rotation_changed` 与 `_setup_action_buttons`。
  - 置信度: 高

- [AUD-25313] [Suggestion] 宽高比预设项 "Free"/"Custom" 未走 i18n，且以英文字面量做相等比较
  - 位置: src/features/color/ui/crop_panel.py:235-236, 353, 471
  - 证据:
    ```python
    for name, _ in self.ASPECT_PRESETS:
        self.aspect_combo.addItem(name)
    ```
    ```python
    if hasattr(self, 'custom_widget'):
        self.custom_widget.setVisible(current_text == "Custom")
    ```
    ```python
    if self.aspect_combo.currentText() == "Custom":
        params["custom_width"] = self.width_spin.value()
    ```
  - 影响: 面板其余文案均经 I18N.tr，唯独预设名硬编码英文——非英文用户看到混杂 UI；同时 `get_params()["aspect"]` 与可见性判断都依赖英文字面量，未来若把预设名本地化，比较会静默失效（Custom 输入框不再出现、参数丢失）。
  - 建议: 预设名经 I18N.tr 显示，判断改用 `self.ASPECT_PRESETS[index][1] == "custom"`（按数据而非显示文本）。
  - 置信度: 高

- [AUD-25314] [Suggestion] 类型注解缺失：多数事件处理器无参数/返回注解
  - 位置: src/features/color/ui/crop_panel.py:344-344, 348-349, 124-124, 168-168
  - 证据:
    ```python
    def _on_activate_clicked(self, checked):
    def _on_aspect_combo_changed(self, index):
    def __init__(self, parent=None):
    def _setup_ui(self):
    ```
  - 影响: 与仓库强类型约束不一致（对比同批次 deblur/delivery 文件的注解完整度）。
  - 建议: 统一补 `checked: bool -> None`、`parent: Optional[QWidget]` 等。
  - 置信度: 高

### 通过项
- 信号防递归：旋转同步（`_on_reset_rotation`/`set_params`）用 blockSignals + `_slider_updating` 声明；`on_reset_all_clicked` 用 try/finally 恢复信号
- 节流正确：旋转 16ms single-shot 定时器合并 params_changed，reset 时 stop 后立即发射
- 快捷键作用域处理谨慎：WindowShortcut + `shortcut_owner_window_active` 守卫；Enter/Esc 走应用级 eventFilter（排除文本控件焦点、仅 crop_active 时接管并 accept），与 Motion Graphic 编辑器共存的设计有注释依据
- 输入验证：`set_params` 对 rotation clamp 到 [-45,45]；`set_aspect_ratio` findText 失败静默忽略属合理
- 线程安全/GPU：无 OpenGL、无跨线程状态
- `resizeEvent` 正确转交 super() 并复用 ParamSliderRow 可见性工具

---

## src/features/color/ui/deblur_panel.py (行数: 231)

### 发现
- [AUD-25315] [Suggestion] 自拼相对路径 `models/Deblur` 与引擎的模型目录知识重复
  - 位置: src/features/color/ui/deblur_panel.py:182
  - 证据:
    ```python
    params: Dict[str, Any] = {}
    params["model_path"] = os.path.join("models/Deblur", model_name)
    params["blend"] = self.blend_control.value()
    ```
  - 影响: 面板重复硬编码子目录字符串，且是 cwd 相关的相对路径。已核实 `RestorationEngine.apply_denoise`（denoise_engine.py:596-599）会对相对路径取 basename 再拼 `self.model_dir`，因此当前功能被引擎救回；但引擎若更改解析规则、或未来消费方直接使用该路径，即失效。
  - 建议: 直接传 `model_name`（引擎已能归一化），或暴露/使用 `self.engine.model_dir` 拼绝对路径。
  - 置信度: 高

- [AUD-25316] [Suggestion] `set_params` 用 setCurrentText 静默忽略非法持久化值；blockSignals 只锁了面板自身、未锁子控件
  - 位置: src/features/color/ui/deblur_panel.py:202-216
  - 证据:
    ```python
    self.blockSignals(True)

    if 'blend' in params:
        self.blend_control.setValue(float(params['blend']))

    if 'tile_size' in params:
        self.tile_size_control['combo'].setCurrentText(str(params['tile_size']))
    ```
  - 影响: 非可编辑 QComboBox 上 `setCurrentText("768")`（不在候选内）为无操作，坏数据被静默丢弃而非报警/回退默认；`self.blockSignals` 不影响子控件信号，当前恰好因未连接子控件信号而无效——未来接线恢复时这层保护是假的。
  - 建议: 用 `findText` 校验并回退默认值；对需要抑制的子控件逐一 blockSignals（或删掉无意义的 self.blockSignals）。
  - 置信度: 高

- [AUD-25317] [Suggestion] 混用未本地化文案与裸 list 类型注解
  - 位置: src/features/color/ui/deblur_panel.py:63, 84-84, 121-121, 227-228
  - 证据:
    ```python
    self.reset_btn.setToolTip("Reset All Deblur Parameters")
    ```
    ```python
    self.auto_tiling_combo = self.create_labeled_combo(I18N.tr("deblur.auto_tile", "自动分块:"), ["On", "Off"])
    ```
    ```python
    def create_labeled_combo(self, label: str, items: list) -> Dict[str, Any]:
    ```
  - 影响: On/Off、256/512 等选项及重置 tooltip 未走 I18N，非英文用户界面混杂；`items: list` 未参数化。另 `resizeEvent` 无返回注解。
  - 建议: 选项与 tooltip 走 I18N.tr；注解改 `items: list[str]`、补 `-> None`。
  - 置信度: 高

### 通过项
- 无模型时的守卫：`on_apply_clicked` 对 "No models found" 早退，`refresh_models` 空列表占位
- 应用按钮状态机（set_baked_state）正确恢复默认样式（置空 stylesheet）
- 无信号回环风险：参数控件均未连接到会回写自身的槽
- 线程安全/GPU：引擎调用仅在 apply 信号下游；UI 不触碰 OpenGL
- `resizeEvent` 复用 ParamSliderRow 可见性工具并转交 super()

---

## src/features/color/ui/delivery_matrix_dialog.py (行数: 141)

### 发现
（无）

### 通过项
- 通过（无逻辑问题）：只读展示型对话框；`confirm` 类方法对无损失报告直接放行、有损时强制模态确认，符合「导出前有损确认门槛」设计
- 输入验证：`_RESULT_COLORS.get(row.delivery)` 对未知枚举安全降级（不着色）
- 类型注解完整（含 classmethod 返回 bool/None）；QTableWidgetItem 数据带 UserRole 元信息
- 无信号递归、无 GPU/线程/持久化代码；i18n 全覆盖（含取消按钮复用 export.cancel 键）
- 表格高度上限 320 防止大清单撑爆对话框

---

## 附注

- AUD-25300 为铁律级（宽泛 except）问题，按任务书 severity 定义评为 Major（持续掩盖领域错误），未达崩溃/数据丢失级。
- AUD-25303 的数学缺陷已用可复算用例验证：hue=0/sat=0.5/brightness=0 正向显示 (1.5, 0.75, 0.75)，经 `_rgb_to_hsv` 回读 sat=0.25。
- 交叉验证过的外部事实：`DragValue.setValue` clamp 且等值不发信号（src/shared/ui/widgets/drag_value.py:190-197）；`RestorationEngine.apply_denoise` 归一化相对 model_path（src/features/color/core/denoise_engine.py:596-599）；`set_wheel_params` 高频调用方存在（src/features/color/integrations/tangent_bridge.py:886-888）。
