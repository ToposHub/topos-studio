# 批次报告：batch-256 [P2] src/features/color/ui//

- 编号区间：AUD-25600–AUD-25699
- 审查文件：4 个（2,745 行）
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解
- 交叉核实：ParamSliderRow.setValue（src/shared/ui/widgets/param_slider_row.py:138-144）内部锁保证不触发 valueChanged；QualifierRangeBar.set_range/set_hue_params（src/shared/ui/widgets/qualifier_range_bar.py:37-48）不发信号；qualifier params_changed → engine.set_qualifier_params（src/app/main_window.py:1592 → src/features/color/core/image_engine.py:1418-1421 写入 layer.qualifier_params）；interaction_started → history.push_state（src/app/controllers/menu_toolbar_controller.py:1607-1620）。

## src/features/color/ui/qualifier_panel.py (行数: 769)

### 发现

- [AUD-25600] [Critical] 程序化 set_params/reset 时 invert 复选框 toggled 绕过状态锁，强制把限定器置为 enabled 并提交引擎
  - 位置: src/features/color/ui/qualifier_panel.py:454（关联 :167、:706-711、:742）
  - 证据:
    ```python
    # :167
    self.invert_check.toggled.connect(self.on_param_changed)
    # :454-458 —— 无 _updating_flag 防护（对比 on_slider_param_changed :364 有防护）
    def on_param_changed(self):
        self.params["invert"] = self.invert_check.isChecked()
        if not self.params["enabled"]:
            self.params["enabled"] = True
        self.params_changed.emit(self.params)
    # :706-711 —— blockSignals 作用于面板自身，拦不住子控件 invert_check 的 toggled
    def set_params(self, params):
        self._updating_flag = True
        old = self.blockSignals(True)
        self.params.update(params)
        self.invert_check.setChecked(self.params["invert"])
    ```
  - 影响: Qt 的 blockSignals 只阻断对象自身发出的信号，invert_check.toggled 仍会同步触发 on_param_changed。当程序化同步（layer_controller.py:63 切层回填、undo 恢复、reset_all :662-683）传入 enabled=False 且 invert 状态与当前复选框不同时，setChecked 触发 on_param_changed 把 enabled 强改为 True；set_params 末尾 :742 无条件 emit，经 main_window.py:1592 → image_engine.py:1421 写入 layer.qualifier_params。后果：切到未启用限定器的图层 / 点"重置全部"后限定器被静默启用，matte 参与渲染、交付标志（delivery_matrix.py:227 检查 qualifier enabled）被改变，且随工程保存持久化——属状态污染 + 渲染错误。
  - 建议: on_param_changed 开头补 `if self._updating_flag: return`；同时对子控件（invert_check）在 set_params 期间 blockSignals 而非仅面板自身。
  - 置信度: 高

- [AUD-25601] [Suggestion] update_param 的 blockSignals 防护对象错误，实际是无效代码
  - 位置: src/features/color/ui/qualifier_panel.py:407-429
  - 证据:
    ```python
    old = self.blockSignals(True)
    if "hue" in key:
        self.hue_bar.set_hue_params(...)
        self.hue_center_ctrl.setValue(self.params["hue_center"])
        ...
    self.blockSignals(old)
    if not self.signalsBlocked():
        self.params_changed.emit(self.params)
    ```
  - 影响: 阻断的是面板自身信号而非子控件信号；当前未出事仅因 ParamSliderRow.setValue 内部 `_updating` 锁和 RangeBar setter 不发信号（已核实）。一旦控件实现变化（如 setValue 改为发 valueChanged），此处会立即出现递归/双重 emit；且未设 _updating_flag，auto-enable 分支 :403 在程序化路径语义不明。属误导性防护。
  - 建议: 与 set_params 一致：进入时置 `self._updating_flag = True`，finally 复位；删除无效的 blockSignals 段。
  - 置信度: 高（代码事实确凿；当前无实际故障）

- [AUD-25602] [Suggestion] params_changed 直接 emit 内部可变字典引用
  - 位置: src/features/color/ui/qualifier_panel.py:396、432、458、742
  - 证据:
    ```python
    self.params_changed.emit(self.params)
    ```
  - 影响: 所有 emit 传出的是面板存活状态字典本体。当前引擎侧 `layer.qualifier_params.update(params)`（image_engine.py:1421）是值拷贝语义暂无污染，但任何接收方一旦保存引用（对比 raw/soft_clip 每次 emit 构造新 dict），面板后续拖动会直接改写接收方快照，undo/序列化（layer.py:944、1038-1039 直接引用/浅拷贝链）存在别名风险。
  - 建议: emit `dict(self.params)` 浅拷贝。
  - 置信度: 中

- [AUD-25603] [Suggestion] invert 切换与 reset_all 缺少交互生命周期信号，撤销快照边界缺失
  - 位置: src/features/color/ui/qualifier_panel.py:454-458、662-683
  - 证据:
    ```python
    def on_param_changed(self):
        self.params["invert"] = self.invert_check.isChecked()
        ...
        self.params_changed.emit(self.params)   # 无 interaction_started/ended
    ```
  - 影响: undo 快照由 interaction_started 驱动（menu_toolbar_controller.py:1613-1620 history.push_state）。滑条类参数经 ParamSliderRow wiring 有完整交互边界，invert 复选框与"重置全部"没有——这些修改不产生历史边界，撤销粒度回退到上一次滑条交互，用户按一次 Ctrl+Z 可能连带回滚更多内容。
  - 建议: invert toggled 与 reset_all 前后补发 interaction_started/interaction_ended（参考 raw_panel.on_control_interacted 的做法）。
  - 置信度: 高

- [AUD-25604] [Suggestion] 死参数 hue_enabled/sat_enabled/lum_enabled 声明后无任何读写
  - 位置: src/features/color/ui/qualifier_panel.py:29、33、38
  - 证据:
    ```python
    "hue_enabled": True,
    ...
    "sat_enabled": True,
    ...
    "lum_enabled": True,
    ```
  - 影响: 全文件（含 reset_all :665-672）再无任何引用；这三个键永远以 True 传给引擎，若引擎侧依赖它们做通道开关则形同虚设，也误导后续维护者以为存在通道 UI。
  - 建议: 要么删除，要么补上对应 UI 控件并在 reset_all 中重置。
  - 置信度: 高

- [AUD-25605] [Suggestion] 类型注解缺失
  - 位置: src/features/color/ui/qualifier_panel.py:51、95、327、346、362、398、434-452、706、744、762 等方法无参数/返回注解
  - 证据:
    ```python
    def init_ui(self):
    def update_param(self, key, value):
    def set_params(self, params):
    ```
  - 影响: 违反项目强类型约束（.clinerules / AGENTS.md：所有函数有类型注解）。
  - 建议: 补齐参数与返回类型注解。
  - 置信度: 高

### 通过项
- 线程安全：纯 UI 线程控件，无 OpenGL/渲染线程直接交互
- GPU 资源生命周期：面板不创建、不持有任何 GL 资源
- GLSL/HDR：本文件无着色器代码；参数在归一化 0-1 域传递，无不当 clamp
- 拾色数学：hue 环绕差值（±0.5 归一）与 sat/lum 边界 min/max 钳制正确，无除零路径（k 最小 0.01 / 宽度最小 0.02）
- 信号防递归（滑条/范围条路径）：set_params 的 _updating_flag 状态锁有效（setValue/set_range 均无信号副作用，已核实）
- 异常路径：无宽泛 except

## src/features/color/ui/raw_panel.py (行数: 477)

### 发现

- [AUD-25606] [Suggestion] reset_all 用 findText("None") 查找 LUT 空项，但该项显示文本是 i18n 翻译，中文环境永远找不到
  - 位置: src/features/color/ui/raw_panel.py:403-407（对照 :217）
  - 证据:
    ```python
    # :217 —— addItem 的显示文本是翻译值
    self.lut_combo['combo'].addItem(I18N.tr("raw.none", "无"), None)
    # :402-407
    # Explicitly find and select "None" to be 100% sure
    none_idx = self.lut_combo['combo'].findText("None")
    if none_idx >= 0:
        self.lut_combo['combo'].setCurrentIndex(none_idx)
    elif self.lut_combo['combo'].count() > 0:
        self.lut_combo['combo'].setCurrentIndex(0)
    ```
  - 影响: 中文 locale 下 findText("None") 恒为 -1，注释声称的"100% sure"实际靠 count>0 兜底落到 index 0（恰为"无"项）才行为正确。若未来空项不排在首位或列表为空，重置将选错 LUT。
  - 建议: 改用 `findData(None)` 定位空项。
  - 置信度: 高

- [AUD-25607] [Suggestion] set_params 对外部 params 未验证，combo setCurrentText 不匹配时静默保持旧值并会被后续 emit 反向覆盖
  - 位置: src/features/color/ui/raw_panel.py:295-312、344-373
  - 证据:
    ```python
    if 'color_space' in params:
        self.color_space_combo['combo'].setCurrentText(params['color_space'])
    if 'gamma' in params:
        self.gamma_combo['combo'].setCurrentText(params['gamma'])
    ...
    'color_space': self.color_space_combo['combo'].currentText(),
    'gamma': self.gamma_combo['combo'].currentText(),
    ```
  - 影响: 工程/剪贴板等外部数据若含列表外的色彩空间或伽马名（版本演进、手改工程文件），不可编辑 QComboBox 的 setCurrentText 静默无效；UI 显示旧值，下一次 on_param_changed_return_dict 用显示值覆盖存储值，真实参数被无声替换（违反"外部数据须验证"铁律）。
  - 建议: setCurrentText 前用 findText 校验，找不到时记录日志并保持 UI 与状态一致（或显式落到安全默认值）。
  - 置信度: 中

- [AUD-25608] [Suggestion] 两个 QTimer 未指定 parent，与 soft_clip_panel 的 QTimer(self) 写法不一致
  - 位置: src/features/color/ui/raw_panel.py:33-42
  - 证据:
    ```python
    self.interaction_timer = QTimer()
    self.interaction_timer.setSingleShot(True)
    ...
    self.update_timer = QTimer()
    self.update_timer.setSingleShot(True)
    ```
  - 影响: 未挂父对象，脱离 Qt 对象树管理；面板 C++ 侧销毁与 Python 包装回收之间存在定时器触发窗口（timeout 连接到已销毁面板的绑定方法）。当前由 self 持引用通常无碍，属脆弱模式。
  - 建议: 改为 QTimer(self)。
  - 置信度: 中

- [AUD-25609] [Suggestion] 类型注解缺失
  - 位置: src/features/color/ui/raw_panel.py:47、183、194、213、228、232、239-272、344、375、441 等方法无注解
  - 证据:
    ```python
    def init_ui(self):
    def _create_row(self, label, min_val, max_val, default_val, step):
    def set_params(self, params, emit=False):
    ```
  - 影响: 违反项目强类型约束。
  - 建议: 补齐参数与返回类型注解。
  - 置信度: 高

### 通过项
- 信号防递归：set_params/reset_all/set_enabled_for_file 的 _syncing_ui 状态锁 + blockSignals 组合有效（ParamSliderRow.setValue 内部锁不触发 valueChanged，已核实；init_ui 中 combo 先 addItems 后 connect，无构造期假触发）
- 状态提交一致性：reset_all 阻塞信号重置后同步冲刷 `_pending_update` 并显式 emit interaction_ended 触发全质量解码（:432-439 注释明确的提交语义）
- 性能模式：16ms 节流 emit（约 60fps）+ 500ms 交互收尾 debounce；start_interaction 幂等
- 输入验证（数值路径）：set_val 经 float() 收敛，spin 自带范围钳制
- 异常路径：无宽泛 except
- 线程安全：控件与定时器均运行于 UI 线程

## src/features/color/ui/scope_panel.py (行数: 1017)

### 发现

- [AUD-25610] [Major] _present_gpu_frame 整体 except Exception: pass，GPU 呈现错误被静默吞掉，示波器冻结且无任何诊断
  - 位置: src/features/color/ui/scope_panel.py:986-1017（重点 :1016-1017）
  - 证据:
    ```python
    def _present_gpu_frame(self, frame) -> None:
        """Forward the frame to the matching GPU presenter(s). ..."""
        try:
            if self._current_mode == 'split':
                ...
                wf_presenter.set_scope_frame(
                    frame.waveform_texture_id, *frame.waveform_size
                )
            ...
        except Exception:
            pass
    ```
  - 影响: frame 描述符字段缺失/更名（AttributeError）、size 解包不符（TypeError）、set_scope_frame 内部共享纹理失败——任何一种都会让画面停留在旧帧，无日志无提示。这与本文件自己在 :310-314（P2-COLOR-12）确立的"GPU 失败不能静默"原则直接矛盾；控制器侧 on_gpu_scope_ready 仅 debug 级记录 present 异常（layer_controller.py:1186-1187），用户态完全不可见，排查成本高。
  - 建议: `except Exception as exc: logger.warning("GPU scope present failed: %s", exc)`，必要时触发一次 CPU 页降级提示。
  - 置信度: 高

- [AUD-25611] [Suggestion] 其余多处 `except Exception: pass` 无日志
  - 位置: src/features/color/ui/scope_panel.py:579-582、723-725、769-772、982-984
  - 证据:
    ```python
    try:
        self.display_stack.setCurrentIndex(0)
    except Exception:
        pass
    ...
    try:
        presenter.clear()
    except Exception:
        pass
    ```
  - 影响: 页面切换/清帧失败被完全掩盖；setCurrentIndex 属极廉价操作，异常只可能来自真实 Bug（如布局损坏），静默会延迟暴露。
  - 建议: 至少 logger.debug/warning 记录异常。
  - 置信度: 高（事实确凿；单处影响低）

- [AUD-25612] [Suggestion] update_scope 输入 ndarray 未校验形状，部分分支直接访问 image.shape[2]，2D 输入将 IndexError
  - 位置: src/features/color/ui/scope_panel.py:859、864、874（对照 :830-835 的 ndim==3 防护）
  - 证据:
    ```python
    # :830-835 有防护
    if (max_width is not None and max_height is not None
            and image.ndim == 3 and image.shape[2] >= 3):
    # :859-861 无防护直接取 shape[2]
    elif self._current_mode == 'vectorscope':
        if image.shape[2] >= 3:
            image_rgb = image[:, :, ::-1]
    ```
  - 影响: 面板有两个外部调用方（layer_controller._update_scope_panel:1346/1354/1362/1364、tools_panel.update_scope:184），docstring 只承诺 BGR float32 三通道；一旦传入灰度 2D 读回（source 原图路径 img_full_original 或外部宿主），UI 线程抛 IndexError 未捕获。同文件防护写法不一致说明属遗漏而非约定。
  - 建议: _process_pending_image 入口统一 `if image.ndim != 3: return`（或转三通道）。
  - 置信度: 中

- [AUD-25613] [Suggestion] resizeEvent 每个事件都 bump_demand_revision，交互式拖拽窗口期间无节流地强制重提交 GPU scope
  - 位置: src/features/color/ui/scope_panel.py:747-753
  - 证据:
    ```python
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.selector_container.move(5, 5)
        # A resize changes display_size, so the next demand needs a new
        # revision to invalidate any in-flight GPU scope frame.
        self.bump_demand_revision()
    ```
  - 影响: 拖拽窗口边界时每秒数十个 resize 事件 → 数十次 demand_changed → 控制器 `_on_scope_demand_changed` 以 force=True 重提交 GPU scope 需求（layer_controller.py:1153-1160），每次都可能触发 GPU pass 重建，冲击 4K 实时基准。尺寸失效语义本身正确，缺的是节流。
  - 建议: 对 resize 触发的 revision bump 加 50-100ms 单发定时器合并。
  - 置信度: 中

- [AUD-25614] [Suggestion] set_mode 程序化切换不回放 _last_image，新模式显示空白直到下一帧
  - 位置: src/features/color/ui/scope_panel.py:894-906（对照 _on_mode_changed :741-745）
  - 证据:
    ```python
    # _on_mode_changed（用户路径）会立即回放缓存：
    if hasattr(self, '_last_image') and self._last_image is not None:
        self._pending_image = self._last_image
        self._pending_mask = getattr(self, '_last_mask', None)
        self._process_pending_image()
    # set_mode（程序化路径）无对应逻辑，只 setCurrentIndex + bump
    ```
  - 影响: 静态图/暂停态下经 API 切模式（如记忆布局恢复）时，CPU 页新模式 widget 空白，需等下一次 update_scope 才有内容；与用户切换路径行为不一致。
  - 建议: set_mode 末尾复用同一段 _last_image 回放逻辑。
  - 置信度: 中

- [AUD-25615] [Suggestion] 直接读写子控件私有属性（_heatmap_image/_density/_display_image/_mode/_zebra_threshold），hasattr 掩蔽重构后静默失效
  - 位置: src/features/color/ui/scope_panel.py:618-637、926-929
  - 证据:
    ```python
    if widget is not None and hasattr(widget, '_heatmap_image'):
        widget._heatmap_image = None
        widget.update()
    ...
    'falsecolor_mode': getattr(self.falsecolor_widget, '_mode', 'sdr'),
    'zebra_threshold': float(getattr(
        self.falsecolor_widget, '_zebra_threshold', 1.0)),
    ```
  - 影响: 跨对象访问私有状态；子 widget 重命名属性后 hasattr/getattr 静默走默认值——清屏失效会残留旧源画面，falsecolor demand 会以错误模式请求 GPU 帧（渲染结果与 UI 不符）。
  - 建议: 在各 scope widget 上提供公开 API（clear_display()/get_variant()）。
  - 置信度: 高（耦合事实确凿；触发依赖未来重构）

### 通过项
- GPU 资源生命周期：presenter 仅存纹理描述符，实际 GL 工作在 paintGL 的 GL 互斥下进行（:966-968 注释明确）；hideEvent 清空显示帧防旧帧复现；presenter 创建失败有日志 + 降级横幅（:310-338，P2-COLOR-12）
- 代际守卫：update_gpu_scope 校验 frame.revision 与面板 revision 严格一致才呈现（:961-964），配合控制器 is_frame_valid_for_demand 双重防护
- 输入源切换防串台：source 不匹配的在途帧直接丢弃（:788-789），无缓存时清空全部 CPU widget（:618-637），GPU 帧仅在 graded 源呈现（:965-966）
- 性能模式：面板不可见时跳过 CPU 计算并丢弃 pending（:820-823）；CPU 分析可选 INTER_AREA 降采样、mask 用 INTER_NEAREST 保语义；50ms 节流定时器
- 线程安全：update_scope / update_gpu_scope 均由 UI 线程控制器槽调用（layer_controller.py:1186、1346-1364 已核实），无跨线程 QObject 调用
- 状态提交一致性：模式/缩放/标尺/色域/FalseColor 变体变化均 bump revision 使在途 GPU 帧作废，demand_state 快照字段完整
- 类型注解：公共 API 基本齐全（个别私有方法缺，不单列）

## src/features/color/ui/soft_clip_panel.py (行数: 482)

### 发现

- [AUD-25616] [Suggestion] enable_check.toggled 直连 emit_params，缺交互生命周期信号，撤销快照边界缺失
  - 位置: src/features/color/ui/soft_clip_panel.py:233（对照 raw_panel.py:228-231 的正确做法）
  - 证据:
    ```python
    self.enable_check.toggled.connect(self.emit_params)   # 无 interaction 边界
    ...
    row.interactionStarted.connect(self.interaction_started.emit)  # 滑条有完整边界
    ```
  - 影响: undo 快照由 interaction_started 驱动（menu_toolbar_controller.py:1613-1620）。启用开关这一影响整条软裁切链路的修改不产生历史边界；raw_panel 的 enable 开关经 on_control_interacted 有完整交互语义，同仓库行为不一致。
  - 建议: enable toggle 前后补发 interaction_started/interaction_ended。
  - 置信度: 高

- [AUD-25617] [Suggestion] 参数范围允许 white_point < high 与 black_point > low，肩/趾压缩被条件静默关闭，无钳制也无提示
  - 位置: src/features/color/ui/soft_clip_panel.py:262-292、147-158
  - 证据:
    ```python
    # UI 范围：high 0.0-2.0，white_point 1.0-5.0 —— 允许 high=2.0 > white_point=1.0
    # black_point -1.0..0.1，low -0.5..0.5 —— 允许 black_point=0.1 > low=-0.5
    # 曲线（与引擎侧映射一致）：
    if y_val > high and white_point > high:   # 条件不满足则肩部静默不生效
    ```
  - 影响: 用户可将高光阈值拉到白点之上（黑点同理），压缩功能静默失效，曲线可视化也直接不画压缩段，无任何 UI 反馈，易被当成算法失效。
  - 建议: 在 update 逻辑中联动钳制（如提高 high 时同步抬高 white_point 下限），或在行控件上给出禁用态提示。
  - 置信度: 中

- [AUD-25618] [Suggestion] reset_all 经 16ms 定时器异步 emit，与 raw_panel 的同步冲刷时序不一致
  - 位置: src/features/color/ui/soft_clip_panel.py:417-436（对照 raw_panel.py:438-439）
  - 证据:
    ```python
    self._syncing_ui = False
    self.emit_params()      # 只启动 16ms debounce 定时器
    ```
  - 影响: 重置后 params_changed 延迟 16ms 才到达引擎；期间 curve_viz 亦显示旧曲线（set_params 的手动刷新只发生在 set_params 路径，reset_all 路径依赖定时器后的 _emit_params_now）。时序窗口极小，但与 raw_panel 重置语义（立即同步提交）不一致，且若期间面板被销毁则重置丢失。
  - 建议: reset_all 末尾直接调用 _emit_params_now()。
  - 置信度: 高（事实确凿；影响低）

- [AUD-25619] [Suggestion] 类型注解缺失
  - 位置: src/features/color/ui/soft_clip_panel.py:195、207、374、385、417、439 等方法无注解
  - 证据:
    ```python
    def init_ui(self):
    def _create_row(self, label, min_val, max_val, default_val, step, label_width=90):
    def set_params(self, params, emit=False):
    ```
  - 影响: 违反项目强类型约束。
  - 建议: 补齐参数与返回类型注解。
  - 置信度: 高

### 通过项
- 信号防递归：_syncing_ui 状态锁 + blockSignals 双重防护；ParamSliderRow.setValue 无信号副作用（已核实），set_params 恢复后手动刷新 curve_viz 不经信号回路
- 性能模式：16ms debounce 限制重绘频率；_on_row_interaction_ended 在发 interaction_ended 前冲刷最终值，保证交互结束提交的是完整最终状态（:391-396 设计正确）
- GLSL/HDR 正确性（曲线镜像）：CurveVisualizer 在场景线性域计算，无 clamp(0,1)；肩部渐近白点、趾部渐近黑点、pivot 0.18 幂律对比度、k 下限 0.01 防除零，与 HDR 语义一致
- 状态提交一致性：reset/set_params 均阻塞信号后统一冲刷，控件值与 emit 字典同源（_emit_params_now 从控件读取）
- 异常路径：无宽泛 except
- 线程安全：纯 UI 线程，定时器已挂 parent
