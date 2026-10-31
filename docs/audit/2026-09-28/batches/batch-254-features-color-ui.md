# 批次审查报告：batch-254 [P2] src/features/color/ui//

- 任务书：docs/audit/2026-09-28/assignments/batch-254.md
- 编号区间：AUD-25400–AUD-25499
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解
- 统计：Critical 2 · Major 6 · Suggestion 21

跨文件关键结论（详见各发现）：`layer_panel.py` 的混合模式下拉以本地化显示名作为信号值直通引擎，而引擎/渲染侧 `Layer.BLEND_MODES` 只认英文显示名，中文（及其他本地化）界面下选择任意混合模式都会静默回退 Normal 渲染并在重载时重置；另有两处可复现的运行时错误（`_is_visible` 属性不存在、`remove_layer(-1)` 负索引误删）。

---

## src/features/color/ui/denoise_panel.py (行数: 576)

### 发现

- [AUD-25400] [Major] reset_defaults 硬编码亮度默认 0.05，忽略 ConfigManager 配置默认值
  - 位置: src/features/color/ui/denoise_panel.py:398
  - 证据:
    ```python
    _cfg_denoise = ConfigManager().get("default_denoise_strength") or 50
    _luma_default = _cfg_denoise / 1000.0  # config 0-100 maps to 0.0-0.1 luma range
    self.luma_control = self._create_row(I18N.tr("denoise.luminance", "亮度"), 0.0, 1.0, _luma_default, 0.01)
    ```
    ```python
    def reset_defaults(self) -> None:
        """重置所有参数到默认值"""
        self.mode_combo.setCurrentIndex(0)
        self.luma_control.setValue(0.05)
    ```
  - 影响: 用户配置 `default_denoise_strength` 非 50 时（如 80 → 初始 0.08），点重置后回到 0.05 而非配置默认值；"重置到默认"与"初始默认"不一致，静默偏离用户配置。
  - 建议: 将 `_luma_default` 存为实例属性（如 `self._luma_default`），`reset_defaults` 使用该值而非字面量 0.05。
  - 置信度: 高

- [AUD-25401] [Major] 视频降噪滑杆参数无节流直发，每个 tick 触发渲染管线并重置时域历史
  - 位置: src/features/color/ui/denoise_panel.py:333
  - 证据:
    ```python
    def _on_video_slider_changed(self, _value: float) -> None:
        if self._video_updating:
            return
        self._emit_video_params()
    ```
    （`_emit_video_params` 注释，第 339 行）
    ```python
    def _emit_video_params(self, *_args) -> None:
        """收集当前视频降噪参数并广播（渲染器参数变化会自动重置时域历史）。"""
    ```
  - 影响: 拖动时域/空域任一滑杆时每个鼠标 tick 都构造 `VideoDenoiseParams` 并 emit；下游 `color_controller.on_video_denoise_changed`（color_controller.py:1434）同步调用 `request_render()` → `engine.render_pipeline()`，无节流合并。对照同目录 film_grain_panel.py:169-173 有显式 16ms 节流，本面板缺失；且按注释所述每次参数变化会重置时域历史，拖动过程中预览反复闪变并浪费渲染（4K 实时基准下尤甚）。
  - 建议: 仿照 FilmGrainPanel 使用 singleShot QTimer 做 ~16ms 节流，滑杆释放（interactionEnded）时立即 flush；或仅在值稳定后 emit。
  - 置信度: 中（面板侧无节流为已核实事实；渲染风暴程度取决于下游合并策略，已确认 request_render 为同步调用无合并）

- [AUD-25402] [Suggestion] on_apply_clicked 将 mode==2（视频模式）错误映射为 "AI"
  - 位置: src/features/color/ui/denoise_panel.py:441
  - 证据:
    ```python
    def on_apply_clicked(self) -> None:
        """Apply 按钮点击处理"""
        mode = "Basic" if self.mode_combo.currentIndex() == 0 else "AI"
    ```
  - 影响: 当前 UI 中视频模式隐藏了 Apply 按钮（on_mode_changed index==2 分支），故正常操作不可达；但任何程序化调用（测试/快捷键/未来 UI 变更）都会在视频模式下发出带 AI 参数的 "AI" 烘焙请求。
  - 建议: 显式三分支映射，或 `if currentIndex() == 2: return`。
  - 置信度: 高

- [AUD-25403] [Suggestion] set_params 的防递归锁挂在面板自身，无法阻挡子控件信号
  - 位置: src/features/color/ui/denoise_panel.py:471
  - 证据:
    ```python
    self.blockSignals(True)

    if 'mode' in params:
        mode_idx = 0 if params['mode'] == 'Basic' else 1
        self.mode_combo.setCurrentIndex(mode_idx)
    ```
    （mode_combo 的信号连接，第 69 行）
    ```python
    self.mode_combo.currentIndexChanged.connect(self.on_mode_changed)
    ```
  - 影响: `blockSignals(True)` 作用于 `self`（DenoisePanel），不会阻断子控件 `mode_combo.currentIndexChanged`；恢复状态时 `on_mode_changed` 仍被触发（refresh_models 清空重建模型列表、触发 `_on_model_changed` 改写 tile/overlap），随后第 496 行又显式调用一次 `on_mode_changed`，产生冗余副作用。当前无外部信号泄漏（主窗口只连面板级信号），但锁的语义是失效的，后续在子控件上加内部连接即成信号风暴。
  - 建议: 改用实例状态锁（如已有的 `_video_updating` 模式）或在恢复期间对 `mode_combo` 等子控件 `blockSignals`。
  - 置信度: 高

- [AUD-25404] [Suggestion] 未使用导入 IconColors
  - 位置: src/features/color/ui/denoise_panel.py:10
  - 证据:
    ```python
    from src.shared.assets.icon_manager import icon_reset, IconColors
    ```
  - 影响: `IconColors` 全文件未使用，引入冗余依赖。
  - 建议: 删除未用名称。
  - 置信度: 高

- [AUD-25405] [Suggestion] 多个方法缺少类型注解
  - 位置: src/features/color/ui/denoise_panel.py:277
  - 证据:
    ```python
    def _create_row(self, label, min_val, max_val, default_val, step):
        """Create a ParamSliderRow with standard wiring."""
    ```
    （同类：`init_ui`(45)、`resizeEvent`(507) 等均无参数/返回注解）
  - 影响: 与项目强类型约定不一致，IDE/静态检查无法校验。
  - 建议: 补齐 `label: str, min_val: float, ... -> ParamSliderRow` 等注解。
  - 置信度: 高

- [AUD-25406] [Suggestion] resizeEvent 每次全树 findChildren 收集滑杆行
  - 位置: src/features/color/ui/denoise_panel.py:507
  - 证据:
    ```python
    def resizeEvent(self, event):
        super().resizeEvent(event)
        ParamSliderRow.apply_visibility_to_all(
            ParamSliderRow.collect_rows(self), self.width())
    ```
    （collect_rows 实现，param_slider_row.py:238-240：`return parent.findChildren(ParamSliderRow)`）
  - 影响: 拖拽窗口边缘时每个 resize 事件都做一次控件树遍历；本面板 20+ 个 ParamSliderRow，属重复无缓存开销。
  - 建议: 在 init_ui 后缓存 rows 列表，或由 ParamSliderRow 自身根据父级宽度事件响应。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：面板不直接触碰 OpenGL/ModernGL 对象，AI 推理经 DenoiseEngine 异步执行。
- GLSL·HDR 正确性：无着色器代码；参数以 0.0–1.0 / 0–100 原值传递，无越界 clamp。
- 视频参数恢复（set_video_params_dict）：`_video_updating` 状态锁 + try/finally 模式正确，combo 索引有 max/min 钳制，motion/mode 枚举有回退默认。
- 状态提交一致性：Classic/AI 为烘焙式（denoise_requested + set_baked_state 反映烘焙态），Video 参数由外部持久化（get/set_video_params_dict 对称）。
- 异常与取消路径：on_apply_clicked 对 "No models" 有早退守卫。
- 输入验证：set_params/set_video_params_dict 均做 dict/空值校验；`_emit_video_params` 的 radius 有 int(round()) 归一。

---

## src/features/color/ui/dependency_report_dialog.py (行数: 110)

### 发现

（无发现）

### 通过项
- 线程安全：纯模态对话框，全部操作在 UI 线程。
- 异常与取消路径：`_export_baseline` 捕获 `OSError` 并以 QMessageBox.critical 反馈；取消保存路径（空 path）正确早退。
- 输入验证：依赖 `DependencyReport` 强类型入参；表格条目按 `STATUS_OK` 判色，缺 hash 显示空串。
- 信号防递归：无自反信号连接。
- 状态提交一致性：只读展示 + 导出，不修改工程状态（符合模块注释"纯查询 UI"）。
- 类型注解：完整（`Optional[QWidget]`、`-> None` 等）。

---

## src/features/color/ui/film_grain_panel.py (行数: 261)

### 发现

- [AUD-25407] [Suggestion] reset_all 未重置启用开关 enable_check
  - 位置: src/features/color/ui/film_grain_panel.py:230
  - 证据:
    ```python
    def reset_all(self):
        self._syncing_ui = True
        self._param_timer.stop()
        ...
        for row in all_rows:
            row.reset()
        self.mono_check.setChecked(True)
        self.animated_check.setChecked(False)
    ```
    （`all_widgets = [self.enable_check, self.mono_check, self.animated_check] + all_rows` 中 enable_check 被屏蔽信号但从未 setChecked）
  - 影响: "重置所有胶片颗粒参数"后 `enabled` 保持原值（若为开启则仍开启）并随 grain_changed 发出 enabled=True 的"默认"参数；与 `_build_params` 把 enabled 视为参数之一的语义不完全一致。若为有意保留效果开关，应在 tooltip/注释说明。
  - 建议: reset_all 中补 `self.enable_check.setChecked(False)`（回到构造默认），或注释明确"重置保留启用状态"。
  - 置信度: 中（可能是有意设计，但无任何注释佐证）

- [AUD-25408] [Suggestion] 未使用导入 Qt、IconColors
  - 位置: src/features/color/ui/film_grain_panel.py:4
  - 证据:
    ```python
    from PyQt6.QtCore import Qt, pyqtSignal, QSize, QTimer
    ...
    from src.shared.assets.icon_manager import icon_reset, IconColors
    ```
  - 影响: `Qt` 与 `IconColors` 全文件未引用。
  - 建议: 删除两个未用名称。
  - 置信度: 高

- [AUD-25409] [Suggestion] 方法普遍缺少类型注解
  - 位置: src/features/color/ui/film_grain_panel.py:140
  - 证据:
    ```python
    def _create_row(self, label, min_val, max_val, default_val, step, label_width=80):
    ```
    （同类：init_ui(39)、set_params(189)、reset_all(230)、resizeEvent(257)）
  - 影响: 与项目强类型约定不一致。
  - 建议: 补齐参数与返回类型注解。
  - 置信度: 高

- [AUD-25410] [Suggestion] resizeEvent 每次全树 findChildren 收集滑杆行
  - 位置: src/features/color/ui/film_grain_panel.py:257
  - 证据:
    ```python
    def resizeEvent(self, event):
        super().resizeEvent(event)
        ParamSliderRow.apply_visibility_to_all(
            ParamSliderRow.collect_rows(self), self.width())
    ```
  - 影响: 同 AUD-25406，交互式缩放期间重复控件树遍历。
  - 建议: 缓存 rows 列表复用。
  - 置信度: 高

### 通过项
- 信号防递归：`_syncing_ui` 状态锁 + 子控件 blockSignals 双保险；set_params/reset_all 均先停 `self._param_timer`、清 `_pending_params`，恢复期间无信号风暴。
- 性能模式：16ms singleShot 节流 + 滑杆释放 flush，是本目录的正面范例。
- 状态提交一致性：set_params 支持 emit 参数用于撤销/重做后主动广播；reset 后立即 emit 保持引擎与 UI 一致。
- 线程安全：无跨线程交互；QTimer 归属 UI 线程。
- 异常与取消路径：节流计时器路径简单，无吞异常的宽泛 except。

---

## src/features/color/ui/gl_viewport.py (行数: 190)

### 发现

- [AUD-25411] [Major] show 后延迟渲染无取消/可见性检查，尺寸无效时无限重试
  - 位置: src/features/color/ui/gl_viewport.py:183
  - 证据:
    ```python
                else:
                    # If size is still invalid, try again later
                    QTimer.singleShot(50, self._trigger_render_after_show)
                break
    ```
    （触发处，第 158 行）
    ```python
                # Use QTimer to defer the render call until after the show event completes
                QTimer.singleShot(50, self._trigger_render_after_show)
    ```
  - 影响: (1) 若 50ms 内用户切走页面/隐藏视口，回调仍执行 `engine.render_pipeline(render_reason='viewport_refresh')`，对隐藏页面做完整渲染；(2) 视口若长期保持无效尺寸（≤10px，如停在被折叠的标签页），50ms 重试永不停止，形成无终止的定时器循环。回调内没有 `self.isVisible()` / 重试上限检查。
  - 建议: 回调开头检查 `if not self.isVisible(): return`；重试加计数上限或改用有父子关系的 QTimer 并在 hideEvent 中 stop。
  - 置信度: 中（无限重试路径已由代码逻辑确证；对隐藏视口渲染的实际频率取决于页面切换节奏）

- [AUD-25412] [Suggestion] 死状态变量 `_needs_context_restore` / `_pending_render_after_show`
  - 位置: src/features/color/ui/gl_viewport.py:28
  - 证据:
    ```python
        self._needs_context_restore = False
        self._pending_render_after_show = False
    ```
    （后续仅被赋值：第 142 行 `self._needs_context_restore = False`、第 190 行 `self._needs_context_restore = True`，从无读取）
  - 影响: 两个状态变量全文件只写不读；hideEvent 标记的"需恢复上下文"从未被消费，属于缺失的恢复逻辑或残留死代码，误导维护者以为存在恢复机制。
  - 建议: 实现基于 `_needs_context_restore` 的恢复路径，或删除两个变量。
  - 置信度: 高

- [AUD-25413] [Suggestion] paintGL 中的父级遍历结果从未使用（每帧热路径死代码）
  - 位置: src/features/color/ui/gl_viewport.py:127
  - 证据:
    ```python
        parent_canvas = self.parent()
        while parent_canvas and not hasattr(parent_canvas, 'engine'):
            parent_canvas = parent_canvas.parent()
        # Clear the background
        if self.gl_manager.ctx:
    ```
  - 影响: `parent_canvas` 计算后未参与任何逻辑；paintGL 每次重绘都白做一次父链遍历，并让读者误以为背景清除与 canvas 相关。
  - 建议: 删除该遍历，仅保留 `ctx.clear`。
  - 置信度: 高

- [AUD-25414] [Suggestion] showEvent 中 makeCurrent 后未调用 doneCurrent
  - 位置: src/features/color/ui/gl_viewport.py:145
  - 证据:
    ```python
        # Make the GL context current
        if self.gl_manager.ctx:
            self.makeCurrent()
    ```
  - 影响: QOpenGLWidget 约定在 paintGL/initializeGL/resizeGL 之外 makeCurrent 后应配对 doneCurrent；showEvent 中置为当前后不释放，可能干扰合成器对 framebuffer 的使用，也与共享上下文（渲染线程）的互斥假设产生额外竞争窗口。
  - 建议: 在 showEvent 的更新触发后补 `self.doneCurrent()`，或直接依赖随后的 update() 走标准绘制路径。
  - 置信度: 中（GLContextManager 内部可能有额外保护，本文件内未见）

- [AUD-25415] [Suggestion] initializeGL 的一次性守卫阻止 GL 上下文重建后的再初始化
  - 位置: src/features/color/ui/gl_viewport.py:40
  - 证据:
    ```python
    def initializeGL(self):
        """Called once before the first call to paintGL() or resizeGL()."""
        if self._gl_initialized:
            return
        self._gl_initialized = True
    ```
  - 影响: Qt 在特定平台事件（屏幕切换/设备重置）后会重建上下文并再次调用 initializeGL；该守卫使 `gl_manager.initialize(self.context())` 与异步渲染器初始化被跳过，gl_manager 中残留失效的旧上下文对象。
  - 建议: 守卫应比较上下文是否变化（记录 `self.context()`），变化时重新初始化并通知引擎重建。
  - 置信度: 中（重建触发场景平台相关，代码路径本身确证）

- [AUD-25416] [Suggestion] 多数方法缺少类型注解
  - 位置: src/features/color/ui/gl_viewport.py:21
  - 证据:
    ```python
    def __init__(self, parent=None):
    ```
    （同类：initializeGL(38)、_on_frame_ready(61)、resizeGL(89)、paintGL(117)、showEvent(137)、hideEvent(187)）
  - 影响: 与项目强类型约定不一致。
  - 建议: 补齐注解（如 `_on_frame_ready(self, tex_id: int) -> None`）。
  - 置信度: 高

### 通过项
- 线程安全（渲染回调）：`frame_ready` 由渲染线程发出，经 Qt 自动排队连接在 UI 线程执行 `_on_frame_ready`，回调内仅做 scene.invalidate + update，未触碰 GL API，符合"UI 线程不操作 OpenGL"铁律（且有注释明确说明该设计）。
- GPU 资源生命周期：视口本身不创建 ModernGL 资源；resize 仅更新 viewport 参数，clear 使用共享上下文且发生在 GL 回调内（上下文已当前）。
- 性能模式：P4-1 resize 50ms 防抖 + 轻量视口刷新优先（复用 GPU 纹理）避免 render storm，是正面设计。
- 信号防递归：gl_initialized 单次发射；frame_ready 回调不重入渲染。

---

## src/features/color/ui/grade_paste_dialog.py (行数: 81)

### 发现

- [AUD-25417] [Suggestion] 未注册类别在对话框中被静默丢弃
  - 位置: src/features/color/ui/grade_paste_dialog.py:46
  - 证据:
    ```python
    #: 注册表顺序（tracking/raw 在尾部）
    _CATEGORY_ORDER = list(GRADE_CATEGORY_KEYS) + ["tracking", "raw"]
    ...
        for cat in _CATEGORY_ORDER:
            if cat not in available:
                continue
    ```
  - 影响: 遍历以 `_CATEGORY_ORDER` 为准而非 `available`；若剪贴板将来捕获了新的类别键（不在 GRADE_CATEGORY_KEYS 也不属于 tracking/raw），该类别不会出现在对话框中且无任何提示，用户无法选择粘贴它。已核实当前 GRADE_CATEGORY_KEYS（12 类）与 tracking/raw 无重复、无遗漏。
  - 建议: 遍历 `available` 并按 `_CATEGORY_ORDER` 排序，未知类别用其键名兜底显示；或至少对差集打日志。
  - 置信度: 低（当前数据集下无实际影响，属前瞻性防御）

### 通过项
- 线程安全 / GPU 生命周期 / GLSL·HDR：纯模态对话框，均不涉及。
- 信号防递归：无自反连接；`_toggle_all` 对空集合的 `all()` 为 True 分支无害。
- 状态提交一致性：纯查询 UI 不修改工程状态，返回 None 表示取消的契约清晰（get_categories）。
- 异常与取消路径：exec() 结果判断正确，Cancel 返回 None。
- 输入验证：类别集合与注册表求交，天然过滤无效键。
- 类型注解：完整（`Optional[Set[str]]`、`-> Optional[Set[str]]` 等）。

---

## src/features/color/ui/hsl_curves_panel.py (行数: 113)

### 发现

- [AUD-25418] [Suggestion] `_on_hsl_curve_changed` 为死代码，docstring 宣称的转发信号不存在
  - 位置: src/features/color/ui/hsl_curves_panel.py:78
  - 证据:
    ```python
        # Connect signals
        self.hsl_curves_widget.changed.connect(self._on_hsl_curve_changed)
    ...
    def _on_hsl_curve_changed(self, lut, data):
        """Handle HSL curve changes - forward to MainWindow via changed signal."""
        pass
    ```
  - 影响: 面板类没有 `changed` 信号，处理器为空；实际链路是 main_window.py:1555 直接连接 `hsl_curves_panel.hsl_curves_widget.changed`。每个曲线变更都空跑一次槽调用，且注释误导维护者以为面板层有转发。另：构造参数 `engine` 存为 `self.engine` 后在本文件内从未使用。
  - 建议: 删除该连接与空槽（或真正实现面板级 `changed` 转发信号并让主窗口改连面板）；顺带处理未用的 `self.engine`。
  - 置信度: 高

- [AUD-25419] [Suggestion] 部分方法缺少类型注解
  - 位置: src/features/color/ui/hsl_curves_panel.py:33
  - 证据:
    ```python
    def __init__(self, engine=None):
    ...
    def init_ui(self):
    ...
    def get_curves_data(self):
    ```
  - 影响: 与项目强类型约定不一致（`_on_hsl_curve_changed` 的 lut/data 也无注解）。
  - 建议: 补齐注解。
  - 置信度: 高

### 通过项
- 线程安全 / GPU 生命周期 / GLSL·HDR：纯包装面板，均不涉及。
- 信号防递归：set_curves_data 委托给 HSLCurvesWidget（其 `emit_changed=False` 默认不触发 changed 信号），恢复无信号风暴；picker 信号仅转发。
- 状态提交一致性：get/set_curves_data 对称，reset 支持显式 emit_changed 控制。
- 异常与取消路径：`if self.hsl_curves_widget` 防御性判空。

---

## src/features/color/ui/layer_panel.py (行数: 820)

### 发现

- [AUD-25420] [Critical] `_toggle_current_visibility` 访问不存在的属性 `widget._is_visible`，触发即 AttributeError 崩溃
  - 位置: src/features/color/ui/layer_panel.py:549
  - 证据:
    ```python
        def _toggle_current_visibility(self):
            """切换当前图层可见性。"""
            idx = self.list_widget.currentRow()
            if idx >= 0:
                model_idx = self._ui_to_model(idx)
                item = self.list_widget.item(idx)
                widget = self.list_widget.itemWidget(item)
                if widget and isinstance(widget, LayerItemWidget):
                    new_state = not widget._is_visible
    ```
  - 影响: `LayerItemWidget` 全类未定义 `_is_visible`（可见状态存于 `self.visible_btn.setChecked/isChecked`，见第 34-38 行），本文件中 `_is_visible` 仅此一处出现。从图层设置菜单点"Toggle Layer Visibility"必然抛 AttributeError；PyQt6 中槽内未处理异常默认调用 qFatal 终止程序（除非安装了自定义 excepthook），功能完全不可用且可能直接崩溃。
  - 建议: 改为 `new_state = not widget.visible_btn.isChecked()`，或在 LayerItemWidget 上维护 `_is_visible` 属性。
  - 置信度: 高

- [AUD-25421] [Critical] 混合模式下拉以本地化显示名直发引擎，BLEND_MODES 反查失败后静默回退 Normal（渲染错误 + 持久化状态丢失）
  - 位置: src/features/color/ui/layer_panel.py:372
  - 证据:
    ```python
    blend_mode_keys = ["normal", "multiply", "screen", "overlay", "soft_light", "hard_light",
                      "color_dodge", "color_burn", "lighten", "darken", "difference", "exclusion",
                      "hue", "saturation_blend", "color", "luminosity"]
    blend_mode_names = [I18N.tr(f"layer.{key}", key.replace("_", " ").title()) for key in blend_mode_keys]
    self.blend_combo.addItems(blend_mode_names)
    self.blend_combo.currentTextChanged.connect(self.layer_blend_mode_changed.emit)
    ```
  - 影响: 信号值是本地化显示名。已核实链路：main_window.py:1605 将其直连 `engine.set_layer_blend_mode`（image_engine.py:1194-1197 无验证直接 `layer.blend_mode = mode`）；渲染侧 composition_uniforms.py:366-369 `Layer.BLEND_MODES.index(bm_name)` 失败即 `bm_idx = 0`（Normal）——`BLEND_MODES`（layer.py:349-353）只含英文名。zh_CN.json 对全部 layer.* 键有翻译（"正常"/"正片叠底"/…），故中文界面选择任意混合模式都会：实时渲染静默按 Normal 合成（渲染错误）；保存后重载 from_dict（layer.py:1013-1016）将其重置为 "Normal"（状态丢失）。即使在英文缺省回退下，`saturation_blend` 生成的 "Saturation Blend" 也不在 BLEND_MODES（"Saturation"）中，该模式在所有语言下都失效。UI 侧还因 `update_layers` 的 setCurrentText 反向匹配失败显示旧值（见 AUD-25422）。
  - 建议: 信号携带稳定键（blend_mode_keys 或 BLEND_MODES 英文名），displayRole 仅用于显示；`layer_blend_mode_changed` 改为按键发射，接收端按键写入；`update_layers` 用 `findText` 匹配显示名或按索引设置。
  - 置信度: 高

- [AUD-25422] [Major] update_layers 用引擎侧英文 blend_mode 匹配本地化下拉条目，恢复/换层后下拉框显示旧值
  - 位置: src/features/color/ui/layer_panel.py:769
  - 证据:
    ```python
        if 0 <= active_index < len(layers):
            active_layer = layers[active_index]
            self.blend_combo.setCurrentText(active_layer.blend_mode)
    ```
  - 影响: 下拉条目是 `I18N.tr("layer.multiply", ...)` 的本地化名，而 `active_layer.blend_mode` 在项目加载/规范化后是英文 BLEND_MODES 名（layer.py:1013-1016）。中文界面下 `setCurrentText("Multiply")` 永不匹配（条目是"正片叠底"），combo 保持上一个选择——加载工程或切换图层后显示的混合模式与实际不符，用户在此基础上再改动会叠加 AUD-25421 的写入错误。
  - 建议: 维护 key↔显示名映射，用 `setCurrentIndex(blend_mode_keys.index(key))` 设置。
  - 置信度: 高

- [AUD-25423] [Major] 子控件右键菜单坐标以错误的控件为原点映射，菜单位置偏移
  - 位置: src/features/color/ui/layer_panel.py:139
  - 证据:
    ```python
        # Make all child widgets also propagate context menu events
        for child in self.findChildren(QWidget):
            child.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            child.customContextMenuRequested.connect(self._show_context_menu)

    def _show_context_menu(self, pos):
        """Forward context menu request to parent layer panel."""
        # Find the parent LayerPanel and show its context menu
        parent = self.parent()
        while parent:
            if isinstance(parent, LayerPanel):
                # Map position to global coordinates
                global_pos = self.mapToGlobal(pos)
    ```
  - 影响: `customContextMenuRequested(pos)` 的 pos 位于发送者（子控件）坐标系，槽内统一用 `self`（LayerItemWidget）做 `mapToGlobal`；请求来自 grading/fx/mask/ai 图标、bake/reset/del 按钮等子控件时菜单弹出位置偏移该子控件在行内的坐标（右键眼睛按钮或行尾按钮时偏移可达百余像素），菜单出现在错误位置。
  - 建议: 在槽内用 `sender = self.sender()` 做坐标映射（`sender.mapToGlobal(pos)`），仅当 sender 即 self 时回退 `self.mapToGlobal(pos)`。
  - 置信度: 高

- [AUD-25424] [Major] 底部删除按钮未守护无选择状态，layer_removed(-1) 负索引误删顶层图层并自动保存
  - 位置: src/features/color/ui/layer_panel.py:487
  - 证据:
    ```python
        self.del_btn.clicked.connect(lambda: self.layer_removed.emit(self._ui_to_model(self.list_widget.currentRow())))
    ```
    （对比同文件有守护的路径，第 534-539 行）
    ```python
    def _delete_current_layer(self):
        """删除当前选中图层。"""
        idx = self.list_widget.currentRow()
        if idx >= 0:
            model_idx = self._ui_to_model(idx)
            self.layer_removed.emit(model_idx)
    ```
  - 影响: `currentRow()` 为 -1 时（`update_layers` 中 active_index 无效即 `setCurrentRow(-1)` 清除选择，第 775-776 行），`_ui_to_model(-1)` 返回 -1 直接 emit；下游 on_layer_removed（layer_controller.py:96-100）无校验调 `engine.remove_layer(-1)`，Python 负索引命中 `self.layers[-1]`——删除的是最顶层图层而非"无操作"，且随即 `_trigger_autosave()` 把误删持久化（数据丢失）。
  - 建议: 与 `_delete_current_layer` 一致地加 `if idx >= 0` 守卫；引擎侧 remove_layer 同时做 0 <= index < len 校验。
  - 置信度: 高

- [AUD-25425] [Suggestion] update_layers 的 blockSignals 无 try/finally，异常后面板信号永久失效
  - 位置: src/features/color/ui/layer_panel.py:725
  - 证据:
    ```python
        self.list_widget.blockSignals(True)
        self.blend_combo.blockSignals(True)
        self.opacity_slider.blockSignals(True)
        ...
        self.list_widget.clear()
        # Reverse iterate layers for UI (Top layer at index N shows at row 0)
        for i in range(len(layers) - 1, -1, -1):
            layer = layers[i]
    ```
    （解除在第 778-780 行，之间无异常保护）
  - 影响: 重建过程中任何异常（如某 layer 缺 `name`/`blend_mode` 属性、`has_grading_effects()` 抛错）都会跳过解除 blockSignals，之后列表选择/混合模式/不透明度全部无响应，直到下次成功调用。
  - 建议: 用 try/finally 包裹重建逻辑，finally 中统一解除三个控件的 blockSignals。
  - 置信度: 中（异常触发依赖 Layer 对象异常，防护缺失本身确证）

- [AUD-25426] [Suggestion] 未使用导入 QIcon、QPixmap、QKeySequence、QShortcut、QAction
  - 位置: src/features/color/ui/layer_panel.py:6
  - 证据:
    ```python
    from PyQt6.QtGui import QIcon, QPixmap, QKeySequence, QShortcut, QAction
    ```
  - 影响: 五个名称全文件未引用（tooltip 文案提到 Ctrl+Shift+C 等快捷键但并未用 QShortcut 注册，快捷键实际由主窗口/动作层承担）。
  - 建议: 删除未用导入；若本面板应响应快捷键，则补注册。
  - 置信度: 高

- [AUD-25427] [Suggestion] 多数方法缺少类型注解
  - 位置: src/features/color/ui/layer_panel.py:24
  - 证据:
    ```python
    def __init__(self, name, visible=True, has_fx=False, has_mask=False, has_grading=False, is_baked=False, is_offline_mode=False, has_source_bake=False, parent=None):
    ```
    （同类：init_ui(297)、update_layers(717)、_on_rows_moved(644)、_ui_to_model(687) 等约 30 个方法）
  - 影响: 与项目强类型约定不一致；`update_layers` 的 layers/active_index 语义仅靠 docstring。
  - 建议: 补齐注解（`layers: Sequence["Layer"]`, `active_index: int` 等）。
  - 置信度: 高

### 通过项
- 线程安全：纯 UI 线程面板，无 OpenGL/渲染线程交互。
- GPU 资源生命周期：不持有 GL 资源。
- 信号防递归（正常路径）：update_layers 对 list_widget/blend_combo/opacity_slider 的 blockSignals 对象正确、解除顺序正确；item_widget 的可见性信号在构造时先 setChecked 后 connect，无初始风暴；lambda 均以默认参数绑定模型索引 i，无经典闭包晚绑定问题。
- 索引换算：_ui_to_model/_model_to_ui 对负值返回 -1；_on_rows_moved 对 Qt 越界 dest row 与模型索引均有 clamp（第 644-660 行）。
- 拖拽重排：rowsMoved 触发 layer_moved 由控制器全量校验索引后重排并回刷 UI。
- 离线模式：离线横幅显示与 bake 按钮禁用（防误删唯一缓存）逻辑完整。

---

## src/features/color/ui/lut_panel.py (行数: 59)

### 发现

- [AUD-25428] [Suggestion] 代理方法缺少类型注解
  - 位置: src/features/color/ui/lut_panel.py:27
  - 证据:
    ```python
    # Proxy methods for convenience
    def set_selected_lut(self, path):
        self.lut_gallery.set_selected_lut(path)

    def set_intensity(self, val):
        self.lut_gallery.set_intensity(val)
    ```
  - 影响: 薄代理层无注解，调用方无法静态获知参数类型（path: Optional[str]、val: float）。
  - 建议: 补齐 `path: Optional[str] -> None`、`val: float -> None`。
  - 置信度: 高

### 通过项
- 纯代理面板（无自有逻辑）：控件与信号全部转发 LutGallery，七个 pyqtSignal 以 property 形式透出，无状态、无副作用。
- 线程安全 / GPU 生命周期 / GLSL·HDR / 信号防递归 / 状态提交 / 异常路径：均不涉及（委托给 LutGallery 实现，不在本批次范围）。

---

## 附：跨文件验证记录（证据来源）

- `src/features/color/core/layer.py:349-353` BLEND_MODES（英文显示名集合）；`:360` 默认 "Normal"；`:1013-1016` from_dict 校验失败重置 "Normal"。
- `src/features/color/core/composition_uniforms.py:366-369` `BLEND_MODES.index(bm_name)` 失败回退 `bm_idx = 0`。
- `src/app/main_window.py:1605` `layer_blend_mode_changed` 直连 `engine.set_layer_blend_mode`；`src/features/color/core/image_engine.py:1194-1197` 无验证写入。
- `src/shared/i18n/locales/zh_CN.json` "layer" 命名空间含全部混合模式中文翻译。
- `src/app/controllers/layer_controller.py:96-100` on_layer_removed 无索引校验；`image_engine.py:1075-1090` remove_layer 直接 `self.layers[index]`。
- `src/app/controllers/color_controller.py:1434-1452` on_video_denoise_changed 每 tick 同步 request_render，无节流。
- `src/shared/ui/widgets/param_slider_row.py:238-240` collect_rows = findChildren（无缓存）。
- `src/app/main_window.py:1555` HSL 曲线 changed 由主窗口直连 widget 层，面板层转发未实现。
