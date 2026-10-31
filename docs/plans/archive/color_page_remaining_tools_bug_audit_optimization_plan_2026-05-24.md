# Color 页面其余调色工具 BUG 审查与优化计划

日期：2026-05-24

范围：本计划审查 Color 页面中除 `Curves`、`Soft Clip`、`HSL Curves`、`Mixer` 之外的调色/处理工具。

已单独覆盖的工具见：

- `docs/color_tools_bug_audit_optimization_plan_2026-05-24.md`

本次覆盖工具：

- Primaries / Grading
- RAW 控制
- Qualifier
- Color Warper
- Blur / Sharpen
- Film Grain
- LUT Gallery / LUT 强度
- Denoise / Deblur

审查重点：

- UI 参数同步是否会误触发渲染、历史记录、自动保存
- 项目打开、图层切换、撤销/重做后参数是否能稳定恢复
- UI 参数、Layer 参数、AsyncRenderer uniform、GLSL shader 是否一致
- Color 页面工具在视频播放、RAW、HDR、LUT、遮罩场景下是否有行为断层
- 是否存在明确崩溃点、路径依赖、资源泄漏或性能浪费

---

## 结论摘要

### P0：需要优先修复

1. `WarperPanel.set_params()` 使用 `np.zeros` 但文件未导入 `numpy as np`，项目恢复/图层切换到已有 Warper 数据时会直接崩溃。
2. 多个面板的 `set_params()` 只 block 父组件信号，没有阻断子控件信号或内部定时器，导致“图层同步/项目恢复”被误当成用户调参。
3. `Blur/Sharpen` shader 中 Blur 会覆盖 Sharpen 的结果：同时开启锐化和模糊时，最终只保留模糊路径。
4. `BlurSharpenPanel.set_params_dict()` 恢复高级锐化半径时单位换算错误，会把半径恢复到错误值。
5. `RawPanel` 初始化后启动一个永久 16 ms 定时器，即使无任何 RAW 操作也持续唤醒 UI。

### P1：建议本轮一起修复

1. `QualifierPanel.set_params()` 在 UI 同步完成后无条件 emit `params_changed`，会触发 engine render 和视频参数失效。
2. `GradingPanel.set_params()` 会通过子控件信号启动 throttle timer，图层切换后延迟 emit `grading_changed`。
3. `RawPanel.set_params()`、`set_enabled_for_file()` 会在文件打开/项目恢复阶段进入 interaction 状态并触发 RAW 参数更新。
4. `FilmGrainPanel.set_params()` 会被子控件信号间接触发，并且 film grain seed 每次参数更新都会变化，拖动强度时颗粒图案可能跳变。
5. LUT Gallery 的 LUT 目录基于 `os.getcwd()`，从不同工作目录启动应用时 LUT 列表可能错位。
6. LUT Reset 只重置 engine，不清理 UI 选中态和强度显示，界面和实际状态可能不一致。

### P2：中期优化

1. Color 页面缺少统一的“程序化同步静音机制”，每个 panel 各自 block signals，容易漏掉子控件。
2. Grading、RAW、Qualifier 中存在 HDR/HSV/HSL 语义和 UI 文案不完全一致的问题，需要定义明确色彩数学契约。
3. Denoise / Deblur worker 在已有任务运行时调用 `wait()`，可能让 UI 卡住；应改为取消、排队或禁用再次 Apply。
4. LUT import 同名文件会覆盖本地 LUT，缺少冲突处理。
5. reset 行为不统一：有些 reset 保持 enable，有些 reset 关闭效果，有些 reset 不发最终参数。

---

## 相关代码地图

### UI 面板

- `src/ui/widgets/color_wheel.py`
  - `GradingPanel`
  - `set_params()`
  - `reset_all()`
- `src/ui/raw_panel.py`
  - `RawPanel`
  - `load_lut_list()`
  - `set_params()`
  - `set_enabled_for_file()`
- `src/ui/qualifier_panel.py`
  - `QualifierPanel`
  - `set_params()`
  - `reset_all()`
- `src/ui/warper_panel.py`
  - `WarperPanel`
  - `on_grid_size_changed()`
  - `set_params()`
- `src/ui/blur_sharpen_panel.py`
  - `BlurSharpenPanel`
  - `get_params_dict()`
  - `set_params_dict()`
  - `reset_all()`
  - `reset_blur()`
- `src/ui/film_grain_panel.py`
  - `FilmGrainPanel`
  - `set_params()`
  - `reset_all()`
- `src/ui/widgets/lut_gallery.py`
  - `LutGallery`
  - `scan_luts()`
  - `add_lut_item()`
  - `_on_reset_clicked()`
- `src/ui/denoise_panel.py`
  - `DenoisePanel`
- `src/ui/deblur_panel.py`
  - `DeblurPanel`

### MainWindow 信号入口

- `src/ui/main_window.py`
  - `connect_signals()`
  - `update_from_layer()`
  - `on_grading_changed()`
  - `on_raw_params_changed()`
  - `on_warper_changed()`
  - `on_blur_sharpen_changed()`
  - `apply_lut_from_path()`
  - `on_lut_intensity_changed()`
  - `on_lut_reset()`
  - `on_denoise_requested()`
  - `on_deblur_requested()`

### Engine / Renderer / Shader

- `src/core/image_engine.py`
  - `grade_image()`
  - `on_raw_params_changed()`
  - `set_qualifier_params()`
  - `set_film_grain_params()`
  - `apply_lut()`
  - `set_lut_intensity()`
  - `reset_lut()`
- `src/core/async_renderer.py`
  - `_generate_matte()`
  - `_apply_matte_postprocess()`
  - `_update_composition_uniforms()`
- `src/core/shader_compiler.py`
  - `build_composition_shader()`
  - `set_uniforms()`
- `src/core/shader_nodes.py`
  - `GRADING_SNIPPET`
  - `BLUR_SHARPEN_SNIPPET`
  - `WARPER_SNIPPET`
  - `QUALIFIER_SNIPPET`
  - `FILM_GRAIN_SNIPPET`
- `src/core/denoise_engine.py`
  - `DenoiseEngine`
  - `RestorationEngine`

---

## 全局问题：程序化 UI 同步会被误判为用户调参

### 现象

很多 Color 面板在 `update_from_layer()` 或项目加载时调用 `set_params()`，本意只是把 Layer 参数显示到 UI。但这些方法通常只调用 `self.blockSignals(True)`，没有 block 子控件信号，也没有停掉已经启动的 throttle timer。

结果是：

- 切换图层后可能延迟触发 `grading_changed`、`effects_changed`、`grain_changed`、`raw_params_changed`
- 项目恢复后可能马上重新 render
- 自动保存可能被触发
- 视频播放中的 cached render params 被不必要地 invalidate
- Undo/Redo 可能多出看起来“自己生成”的历史点

### 涉及位置

- `src/ui/widgets/color_wheel.py`
  - `GradingPanel.set_params()`
- `src/ui/raw_panel.py`
  - `RawPanel.set_params()`
  - `RawPanel.set_enabled_for_file()`
- `src/ui/qualifier_panel.py`
  - `QualifierPanel.set_params()`
- `src/ui/blur_sharpen_panel.py`
  - `BlurSharpenPanel.set_params_dict()`
- `src/ui/film_grain_panel.py`
  - `FilmGrainPanel.set_params()`

### 优化思路

建立统一约定：

- `set_params(..., emit=False)` 默认只同步 UI，不触发 engine。
- 用户交互入口才 emit，例如 slider dragged、spin edited、checkbox toggled、reset button clicked。
- panel 内部增加 `_syncing_ui` 或 `_programmatic_update` 标志。
- `_schedule_param_update()`、`emit_params()`、`on_param_changed()` 第一行检查该标志。
- 批量同步前停止 throttle timer，并清空 `_pending_params`。
- 批量同步时使用 `QSignalBlocker` 或统一 helper block 所有子控件。

建议新增工具函数：

```python
class UiSyncGuard:
    def __init__(self, panel: QWidget, widgets: list[QObject], timers: list[QTimer] | None = None):
        ...
```

或先在每个 panel 内部使用更简单的模式：

```python
self._syncing_ui = True
try:
    self._param_timer.stop()
    self._pending_params = None
    for widget in self._param_widgets:
        widget.blockSignals(True)
    ...
finally:
    for widget in self._param_widgets:
        widget.blockSignals(False)
    self._syncing_ui = False
```

### 验证方法

自动化测试：

- 创建 FakeEngine，记录每个 setter/render 调用次数。
- 调用 `MainWindow.update_from_layer(layer)`。
- 断言以下方法均不应被调用：
  - `grade_image`
  - `set_effects_params`
  - `set_qualifier_params`
  - `set_film_grain_params`
  - `on_raw_params_changed`
  - `set_warper_params`
- 使用 `QTest.qWait(50)` 等待 throttle timer，确认仍未 emit。

手动验证：

1. 打开项目。
2. 切换两个已有不同调色参数的图层。
3. 不触碰任何控件。
4. 观察：
   - 无新增 History entry。
   - 无自动保存状态闪烁。
   - 视频播放中无额外卡顿。
   - Debug log 中无无意义 render。

---

## Primaries / Grading

### BUG G1：`set_params()` 会通过子控件信号延迟 emit

位置：

- `src/ui/widgets/color_wheel.py`
  - `GradingPanel.set_params()`
  - `_on_slider_value_changed()`
  - `_emit_grading_changed()`

原因：

- `set_params()` 只调用 `self.blockSignals(True)`。
- 但参数控件是子控件，`spin.setValue()` 和 `slider.setValue()` 仍会触发子控件自己的 signal。
- 子控件 signal 会调用 `_on_slider_value_changed()`，启动 `_grading_update_timer`。
- `self.blockSignals(False)` 后，timer 到期时 emit `grading_changed`。

风险：

- 图层切换变成调色操作。
- 项目打开后可能重新写入当前 layer grading params。
- 视频播放时因为 `_invalidate_video_render_params_if_playing()` 被调用，造成额外 render。

优化：

- 为 `GradingPanel` 增加 `_syncing_ui`。
- `set_params()` 中 block 所有 wheel、slider、spin 的信号。
- 进入 `set_params()` 前停止 `_grading_update_timer`。
- 同步完成后不 emit。
- `reset_all()` 保持 emit，因为 reset 是用户操作。

验证：

- 单元测试调用 `grading_panel.set_params(...)` 后 `QTest.qWait(50)`，确认 `grading_changed` 未触发。
- 手动切图层后确认 History 不增加。

### BUG G2：数值框小数会被 slider 同步截断

位置：

- `src/ui/widgets/color_wheel.py`
  - `create_primary_slider()`

原因：

- `QDoubleSpinBox` 支持一位小数。
- 但 `spin.valueChanged.connect(lambda v: slider.setValue(int(v)))` 会把小数截断。
- slider valueChanged 又把整数写回 spin。

风险：

- 用户输入 `0.5` 可能立刻变为 `0.0`。
- 代码注释说读取 spin 保留 float 精度，但实际双向同步已经破坏精度。

优化：

- slider 内部使用放大比例，例如 `scale = 10` 或 `scale = 100`。
- slider 范围改成 `min * scale` 到 `max * scale`。
- slider -> spin 用 `v / scale`。
- spin -> slider 用 `round(v * scale)`。
- 保持统一精度，不要每个 slider 自己写 lambda。

验证：

- 手动输入 `0.5`、`-0.5`、`12.5`，确认 UI 显示不丢精度。
- 保存项目再打开，确认小数仍然存在。

### BUG G3：Grading shader 与 HDR 处理契约需要整理

位置：

- `src/core/shader_nodes.py`
  - `GRADING_SNIPPET`

观察：

- shadows 部分只对 `[0, 1]` 范围做 power curve。
- vibrance/saturation 使用 SDR luma/mix 逻辑。
- 负值保护会改变暗部色彩比例。
- mid detail / sharpness 采样 `u_input_tex`，不是当前已经经过 blur/sharpen 的 `graded`。

这不一定是立即崩溃 bug，但属于高风险色彩一致性问题。

优化：

- 为 Grading 定义一份 math contract：
  - 哪些操作是 scene-linear？
  - 哪些操作允许超过 1.0？
  - 哪些操作允许负值？
  - 哪些操作必须不 clamp？
- 对 HDR 测试图建立黄金图：
  - 负值暗部
  - 高光 1.0 到 16.0
  - 高饱和色
  - Log/RAW 输入
- 如果保留当前行为，需要在文档和注释里明确它是设计选择。

验证：

- 生成 synthetic float32 ramp，检查曝光、对比、饱和、阴影、高光不会产生 NaN/Inf。
- 比较 CPU reference 和 shader 输出误差。

---

## RAW 控制

### BUG R1：`RawPanel` 永久 16 ms 定时器造成 idle 性能浪费

位置：

- `src/ui/raw_panel.py`
  - `__init__()`

原因：

- `update_timer` 初始化后立即 `start()`。
- `_emit_params()` 每 16 ms 被调用一次，即使没有 `_pending_update`。

风险：

- App 空闲时 UI event loop 仍持续唤醒。
- 多窗口/长期使用时增加 CPU 和电量消耗。
- 其他 throttle timer 是按需启动，RAW 面板是例外。

优化：

- `update_timer.setSingleShot(True)`。
- `on_param_changed()` 中如果 timer 未 active，则启动一次。
- `_emit_params()` 发完后停止，不 pending 时不运行。

验证：

- 打开应用不操作 RAW，确认 `_emit_params()` 不持续被调用。
- 拖动 RAW slider 时仍保持平滑更新。

### BUG R2：`set_params()` 会触发 RAW interaction 和参数更新

位置：

- `src/ui/raw_panel.py`
  - `set_params()`
  - `on_enable_toggled()`

原因：

- `set_params()` 末尾调用 `on_enable_toggled(self.enable_cb.isChecked())`。
- `on_enable_toggled()` 调用 `on_control_interacted()`。
- `on_control_interacted()` 会发 `interaction_started`，设置 pending update，并启动 interaction timer。

风险：

- 项目加载/图层同步触发 RAW 预览。
- RAW worker 可能被不必要地唤醒。
- `ImageEngine.current_raw_params` 和 `_last_applied_raw_params` 可能在恢复阶段被污染。

优化：

- 拆分 `on_enable_toggled()`：
  - `sync_enabled_ui(enabled)` 只更新 container enabled。
  - `on_enable_toggled_by_user(enabled)` 才触发 interaction/update。
- `set_params()` 调用 `sync_enabled_ui()`。
- 所有 combo/spin/checkbox 的程序化 set 都用 signal blocker。

验证：

- 打开包含 RAW params 的项目，确认 `ImageEngine.on_raw_params_changed()` 不因 UI 同步被调用。
- 切换 RAW/non-RAW 文件，确认 enable 状态正确，但不会生成多余历史记录。

### BUG R3：`set_enabled_for_file()` 在文件加载阶段发出用户交互

位置：

- `src/ui/raw_panel.py`
  - `set_enabled_for_file()`
- `src/ui/main_window.py`
  - 文件加载后调用 `raw_panel.set_enabled_for_file(is_raw)`

原因：

- `enable_cb.setChecked()` 和 `spin.setValue()` 没有 block 子信号。
- 文件加载阶段可能触发 `raw_params_changed`。

优化：

- `set_enabled_for_file(is_raw, emit=False)`。
- 默认只同步 UI。
- 如果需要为新 RAW 文件自动启用 RAW preview，由 `MainWindow.load_image()` 在明确阶段调用 engine，而不是通过 UI signal 间接触发。

验证：

- 加载普通 JPEG，RAW panel disabled/default，但 engine 不收到 RAW 更新。
- 加载 RAW 文件，RAW 默认启用逻辑只发生一次。

### BUG R4：RAW 输入 LUT 目录依赖当前工作目录

位置：

- `src/ui/raw_panel.py`
  - `load_lut_list()`

原因：

- 使用 `os.path.join(os.getcwd(), "luts")`。
- 如果从 IDE、Finder、打包 App、不同 cwd 启动，指向的 LUT 目录不同。

优化：

- 使用项目根目录或配置目录。
- 建议统一提供 `get_project_root()` / `get_lut_dir()`。
- RAW Panel 和 LUT Gallery 共用同一 LUT 路径解析函数。

验证：

- 从项目根目录启动。
- 从上一级目录启动。
- 从打包 App 目录启动。
- 三种方式看到同一组 LUT。

---

## Qualifier

### BUG Q1：`set_params()` 无条件 emit `params_changed`

位置：

- `src/ui/qualifier_panel.py`
  - `set_params()`
- `src/ui/main_window.py`
  - `qualifier_panel.params_changed.connect(self.engine.set_qualifier_params)`
- `src/core/image_engine.py`
  - `set_qualifier_params()`

原因：

- `QualifierPanel.set_params()` 末尾无条件 `self.params_changed.emit(self.params)`。
- `MainWindow.update_from_layer()` 每次图层切换都会调用它。
- engine 收到后会更新 layer 并 `render_pipeline()`。

风险：

- 图层切换触发实际渲染。
- 可能把 panel 内部默认参数反写 layer。
- 视频播放中额外 invalidate。

优化：

- `set_params(params, emit=False)`。
- reset、picker、slider 用户操作显式 `emit=True`。
- emit 时传 `copy.deepcopy(self.params)`，避免 engine 持有 UI dict 引用。

验证：

- 调用 `update_from_layer()` 后，engine `set_qualifier_params()` 调用次数为 0。
- 用户拖动 Qualifier slider 后，调用次数为 1，并且 layer params 更新。

### BUG Q2：Qualifier 的 `blur` UI 与主 composition shader 不完全一致

位置：

- `src/ui/qualifier_panel.py`
  - `blur` 参数
- `src/core/async_renderer.py`
  - `_generate_matte()`
  - `_apply_matte_postprocess()`
- `src/core/shader_compiler.py`
  - `build_composition_shader()`

观察：

- AsyncRenderer 的 matte 路径会在 `_apply_matte_postprocess()` 中读取 qualifier 的 `blur`、`clean_black`、`clean_white`。
- composition shader 中也会在 `has_qualifier` 时将 qualifier 乘到 mask 上。
- 这意味着 qualifier 可能同时存在：
  - source/viewport matte 生成路径
  - composition shader 再次按原图计算 q_matte 的路径

风险：

- 某些预览/导出路径中 qualifier 处理顺序不一致。
- clean/blur 后处理只作用于 matte cache，composition shader 的二次 q_matte 不经过 clean/blur。
- 如果 matte 已经包含 qualifier，再 composition 中再乘一次 q_matte，会导致边缘更窄或选择范围变暗。

需要进一步确认：

- `AsyncRenderer._generate_matte()` 生成的 matte 是否已经包含 qualifier。
- `build_composition_shader(has_qualifier=True)` 是否还应该再次乘 `q_matte`。

优化：

- 明确一条唯一数据流：
  - 推荐：Qualifier 只在 matte generation 阶段计算，并在 `_apply_matte_postprocess()` 后输出最终 `u_matte_tex`。
  - composition shader 只读取 `u_matte_tex`，不再重复计算 qualifier。
- 或者如果 qualifier 必须在 composition shader 中计算，则 matte generation 不应再计算 qualifier。

验证：

- 创建红/绿边界测试图。
- Qualifier 选红色，设置 blur/clean。
- 对比：
  - viewport preview
  - full-res render
  - export
  - mask preview
- 确认 matte 宽度和边缘软化一致。

### BUG Q3：HSL/HSV 命名与 UI 语义不一致

位置：

- `src/core/shader_nodes.py`
  - `rgb_to_hsl()`
  - `calculate_qualifier_hsl()`

观察：

- 函数名叫 `rgb_to_hsl`。
- 注释说明实际是安全 HSV：`l = min(c_max, 1.0)`，`s = delta / c_max`。
- UI 文案仍是 Lum/HSL 语义。

风险：

- 用户调 `Lum` 以为是 HSL lightness，但 shader 实际使用 value/max channel。
- 高光超过 1.0 时被压到 1.0，RAW/HDR 范围内无法区分更亮高光。

优化：

- 如果保持 HSV，应改名为 `rgb_to_keyer_hsv()` 或文档注明 Lum 是 Value。
- 如果要真正 HSL，需要实现 HSL lightness 并定义 HDR 映射。
- Picker、UI bar、shader 应共享同一转换定义。

验证：

- 输入同 hue/saturation 但不同 luminance/value 的色块，确认 UI picker 和 shader matte 一致。
- HDR 高光 1.0、2.0、4.0 测试，确认 UI 设计符合预期。

---

## Color Warper

### BUG W1：`WarperPanel.set_params()` 缺少 numpy import

位置：

- `src/ui/warper_panel.py`
  - `set_params()`

原因：

- `set_params()` 使用 `np.zeros(..., dtype=np.float32)`。
- 文件没有 `import numpy as np`。

触发方式：

- 打开包含 `layer.warper_params` 的项目。
- 切换到包含 Warper 数据的图层。
- 调用 `MainWindow.update_from_layer()`。

风险：

- 直接 `NameError: name 'np' is not defined`。

优化：

- 增加 `import numpy as np`。
- 同时为 `grid_data` 增加 shape 校验。

验证：

- 构造 6x6、8x8、12x12 warper params，调用 `set_params()` 不报错。
- 打开保存过 Warper 的项目不崩溃。

### BUG W2：切换网格密度不会通知 engine 清空旧 warper

位置：

- `src/ui/warper_panel.py`
  - `on_grid_size_changed()`

原因：

- 切换 combo 时只调用 `self.warper.set_grid_size(size)`。
- 没有 emit `warper_changed(None)` 或新的 identity grid。

风险：

- UI 已显示新网格，但 engine 仍使用旧 `layer.warper_params`。
- 用户以为已经清空/改变网格，画面仍保留旧 warp。
- 如果 renderer 纹理尺寸和 UI grid size 不一致，后续行为难判断。

优化：

- 切换网格密度视为 reset：
  - 清空 engine warper：emit `warper_changed(None)`。
  - 或生成当前 grid 的 identity delta 并设置为 None。
- 弹出确认不是必须，但需要一致状态。

验证：

- 先拖动 warper 产生明显变化。
- 切换 6x6 到 8x8。
- 画面应立即恢复未 warp。
- 保存项目再打开，warper params 应为空或与新网格一致。

### W3：Warper shader 忽略 `grid_size`

位置：

- `src/core/shader_nodes.py`
  - `WARPER_SNIPPET`

观察：

- `apply_warper(vec3 color, sampler2D warper_tex, float grid_size)` 接收 `grid_size`。
- 函数内部没有使用 `grid_size`。

影响：

- 这不是立即 bug，因为 warper texture 自身已经包含插值结果。
- 但 uniform 存在会误导维护者，以为 shader 依赖 grid size。

优化：

- 如果不需要，删除参数和 uniform。
- 如果需要 grid-aware sampling，则补实现。

验证：

- 不同 grid size 下同一色相/饱和度映射应符合 UI 操作预期。

---

## Blur / Sharpen

### BUG B1：`apply_blur_sharpen()` 中 Blur 覆盖 Sharpen 结果

位置：

- `src/core/shader_nodes.py`
  - `BLUR_SHARPEN_SNIPPET`
  - `apply_blur_sharpen()`

原因：

- 函数先将锐化结果写入 `res`。
- Blur 阶段却调用 `bilateral_blur(tex, ...)`、`bokeh_blur(tex, ...)` 等，直接从原始 `tex` 采样并赋值给 `res`。
- 结果：只要 blur enabled，前面 sharpen 对 `res` 的修改被覆盖。

风险：

- UI 同时提供 Sharpen 和 Blur，但实际无法组合。
- 用户打开 blur 后觉得 sharpen 失效。

优化选项：

1. 明确互斥：
   - UI 上让 Sharpen 和 Blur 互斥。
   - shader 中只保留一个路径。
2. 支持组合：
   - blur 函数改为以当前 `res` 为输入。
   - 或先生成 blur result，再按设计与 sharpen result mix。
   - 由于 GLSL 中 blur 函数需要 texture 采样，若要对 sharpen 后结果做 blur，需要两 pass 或临时纹理。
3. 设计为独立叠加：
   - `res = mix(res, blur_result, blur_amount)`。
   - 这样 Sharpen 的结果不会完全丢失。

推荐：

- 短期先使用选项 3，避免 UI 行为完全失效。
- 中期重构为独立 effects chain，多 pass 才能精确处理“先锐化后模糊”。

验证：

- 测试图：黑白边缘 + 高频纹理。
- 只开 sharpen、只开 blur、同时开 sharpen+blur。
- 同时开启时结果应不同于“只开 blur”。

### BUG B2：高级锐化半径恢复单位错误

位置：

- `src/ui/blur_sharpen_panel.py`
  - `set_params_dict()`

原因：

- 半径控件 `scale=0.1`。
- `get_params_dict()` 返回的是 spin value，也就是真实半径。
- `set_params_dict()` 却执行：

```python
self.radius_slider['spin'].setValue(int(radius_val / self.radius_slider['scale']))
```

如果保存半径为 `5.0`，恢复时设置 spin 为 `50`，超出 spin 范围后被 clamp，最终显示错误。

优化：

- `spin.setValue(float(radius_val))`。
- slider 同步逻辑由控件内部处理。

验证：

- 设置 advanced radius = 5.0。
- 保存项目并重开。
- UI 显示仍为 5.0。
- engine layer params 仍为 5.0。

### BUG B3：Blur mode-specific 参数恢复不完整

位置：

- `src/ui/blur_sharpen_panel.py`
  - `get_params_dict()`
  - `set_params_dict()`

原因：

- `get_params_dict()` 保存了：
  - `texture_soften_intensity`
  - `texture_soften_detail`
  - `optical_blur_radius`
  - `optical_blur_bokeh`
  - `motion_blur_intensity`
  - `motion_blur_angle`
  - `fast_blur_intensity`
  - `fast_blur_radius`
- `set_params_dict()` 只恢复 motion 和 fast 的部分参数。
- optical、texture soften 的 mode-specific 状态没有恢复。

风险：

- 用户在不同 blur mode 之间切换后保存，再打开项目，部分 mode 参数丢失。
- UI 显示和 layer 存储不一致。

优化：

- `set_params_dict()` 恢复所有 mode-specific 字段。
- active generic 字段和 mode-specific 字段的优先级要明确：
  - 当前 mode 使用 generic `blur_*` 作为实时 shader 参数。
  - 非当前 mode 使用 mode-specific 字段保存 UI 状态。

验证：

- 分别设置 4 个 blur mode 的参数。
- 保存项目。
- 重新打开并切换 4 个 mode。
- 每个 mode 的 UI 值都应恢复。

### BUG B4：`reset_all()` 与 `reset_blur()` reset 范围不完整且可能不 emit

位置：

- `src/ui/blur_sharpen_panel.py`
  - `reset_all()`
  - `reset_blur()`

观察：

- `reset_all()` 只 reset sharpen 参数，不 reset blur 参数。
- `reset_blur()` 只 reset blur 子参数，不关闭 blur enable。
- 两者都依赖 `_flush_pending_params()`，但在 block signals 后可能没有 pending params。

风险：

- 用户点击 reset 后画面不一定更新。
- reset 文案和实际行为不一致。

优化：

- 拆分按钮语义：
  - Reset Sharpen
  - Reset Blur
  - Reset All
- 每个 reset 完成后显式 emit `effects_changed(self.get_params_dict())`。
- reset 是否保留 enable 状态要统一。

验证：

- 开启 sharpen/blur 后点击 reset。
- engine params 与 UI 显示完全一致。
- 画面立即变化。

### BUG B5：布局中同一 container 被 addWidget 两次

位置：

- `src/ui/blur_sharpen_panel.py`
  - `init_ui()`

原因：

- `main_layout.addWidget(self.container)` 出现两次。

风险：

- Qt 可能重新布局同一 widget，出现 warning 或布局顺序异常。

优化：

- 删除重复 addWidget。

验证：

- 启动应用无 Qt layout warning。
- Blur/Sharpen 面板布局不重叠、不跳动。

---

## Film Grain

### BUG F1：`set_params()` 可能触发 grain_changed

位置：

- `src/ui/film_grain_panel.py`
  - `set_params()`

原因：

- `self.blockSignals(True)` 只 block panel 自己。
- 子控件的 spin/checkbox 仍可能触发 `emit_params()`。
- `_param_timer` 会在 block 恢复后 emit。

风险：

- 图层切换/项目恢复导致 film grain 参数被反写并 render。

优化：

- 使用 `_syncing_ui`。
- block 子控件信号。
- 停止 `_param_timer`，清空 `_pending_params`。
- `set_params()` 默认不 emit。

验证：

- 调用 `film_grain_panel.set_params(...)` 后等待 50 ms，不应有 `grain_changed`。

### BUG F2：Film Grain seed 每次参数变化都会改变

位置：

- `src/core/image_engine.py`
  - `set_film_grain_params()`

原因：

- 如果 params 中没有 `seed`，每次调用都会设置：

```python
self.film_grain_params["seed"] = (time.time() % 1000) / 1000.0
```

风险：

- 拖动 intensity/size 时，颗粒图案每个事件都变。
- 静态图片中颗粒看起来闪动，不利于精细调节。
- 视频播放时 seed 变化策略不明确。

优化：

- seed 生命周期明确化：
  - 静态图片：seed 在启用或点击 randomize 时变化。
  - 参数拖动：seed 不变化。
  - 视频：如需 animated grain，使用 frame/time uniform，而不是改变静态 seed。
- UI 可增加一个随机 seed 按钮。

验证：

- 开启 grain，拖动 intensity。
- 相同像素位置的随机纹理相位不应跳变，只改变强度。
- 点击 randomize 后才变化。

### BUG F3：Film Grain 与视频播放的 render 策略不一致

位置：

- `src/ui/main_window.py`
  - `film_grain_panel.grain_changed.connect(self.engine.set_film_grain_params)`

观察：

- Grading、Mixer、Warper、Blur/Sharpen 经过 MainWindow handler，会 invalidate video params 并在播放中 debounce。
- Film Grain 直接连接 engine setter。

风险：

- 视频播放时 grain 参数可能不进入相同缓存失效机制。
- 拖动 grain 可能造成 render flood。

优化：

- 新增 `MainWindow.on_film_grain_changed(params)`。
- 逻辑与 `on_blur_sharpen_changed()` 保持一致：
  - engine update
  - invalidate video render params
  - playback 时 debounce
  - 非 playback request_render

验证：

- 播放视频时拖动 grain intensity。
- 无 render queue flood。
- 参数立即生效或按 debounce 生效。

### BUG F4：`reset_all()` 可能不发最终参数

位置：

- `src/ui/film_grain_panel.py`
  - `reset_all()`

原因：

- reset 时 block panel，但子控件是否产生 `_pending_params` 不稳定。
- 最终只 `_flush_pending_params()`，没有显式 emit 当前参数。

优化：

- reset 完成后直接：

```python
self.grain_changed.emit(self._build_params())
```

或调用统一 `emit_params_now()`。

验证：

- 修改 grain 后点击 reset，画面立即恢复默认 grain 参数。

---

## LUT Gallery / LUT 强度

### BUG L1：LUT 目录依赖 `os.getcwd()`

位置：

- `src/ui/widgets/lut_gallery.py`
  - `__init__()`
- `src/ui/raw_panel.py`
  - `load_lut_list()`

原因：

- LUT Gallery 和 RAW 输入 LUT 都使用当前工作目录。

风险：

- 打包 App 或从不同目录启动时扫描不到项目 LUT。
- RAW 输入 LUT 和主 LUT Gallery 可能看到不同目录。

优化：

- 统一 LUT 路径解析：

```python
from src.utils.paths import get_lut_dir
```

- 路径优先级：
  1. 用户配置的 LUT 目录
  2. 项目根目录 `luts/`
  3. App data 目录

验证：

- 从不同 cwd 启动，LUT 列表一致。
- RAW 输入 LUT 和 Gallery 列表一致。

### BUG L2：LUT Reset 后 UI 状态不清理

位置：

- `src/ui/widgets/lut_gallery.py`
  - `_on_reset_clicked()`
- `src/ui/main_window.py`
  - `on_lut_reset()`

原因：

- Reset button 会 rescan LUT 并 emit reset。
- engine `reset_lut()` 清空 active LUT。
- 但 Gallery 没有清空 selection，也没有把 intensity 复位到默认显示。

风险：

- 画面 LUT 已关闭，但 UI 仍显示某 LUT 被选中。
- 用户误以为 LUT 仍 active。

优化：

- `_on_reset_clicked()`：
  - clear selection
  - set intensity to 1.0 或保留 layer intensity，需定义
  - emit reset
- MainWindow `on_lut_reset()` 后也可以调用：
  - `lut_panel.set_selected_lut(None)`
  - `lut_panel.set_intensity(1.0)`，如果产品定义 reset 强度

验证：

- 应用 LUT 后点击 reset。
- 画面恢复。
- UI 无选中 LUT。
- 保存项目后 active_lut_path 为 None。

### BUG L3：导入同名 LUT 会覆盖本地文件

位置：

- `src/ui/widgets/lut_gallery.py`
  - `add_lut_item()`

原因：

- copy target 是 `self.lut_dir/name`。
- 如果同名文件已存在，`shutil.copy2()` 会覆盖。

风险：

- 用户导入不同目录的同名 LUT，原 LUT 被无提示替换。

优化：

- 如果目标已存在：
  - 对比文件 hash；相同则跳过。
  - 不同则生成 `name_2.cube` 或弹窗确认。

验证：

- 导入两个同名不同内容 LUT，不覆盖原文件。

### BUG L4：从 Gallery 移除 active LUT 后 engine 状态可能仍指向旧路径

位置：

- `src/ui/widgets/lut_gallery.py`
  - `remove_item()`

观察：

- 右键 “Remove from Gallery” 只删除列表 item。
- 如果删除的是当前 active LUT，engine 仍保留 `layer.active_lut_path`。

优化：

- 如果 remove 的 path 等于 active LUT：
  - emit `lut_reset`
  - 或 emit 新 signal `lut_removed(path)`，由 MainWindow 判断。
- 文案可改为 “Hide from Gallery”，如果本意不是删除/停用。

验证：

- 应用 LUT。
- 从 Gallery 移除该 LUT。
- 画面状态和 UI 状态应一致。

---

## Denoise / Deblur

### D1：已有 worker 运行时 `wait()` 可能阻塞 UI

位置：

- `src/core/denoise_engine.py`
  - `DenoiseEngine.apply_denoise()`

原因：

- 如果已有 worker 正在运行，新 apply 会调用 `self.worker.wait()`。
- 如果从 UI 线程进入，该 wait 会阻塞界面直到旧任务结束。

风险：

- 用户重复点击 Apply 或切换工具时 UI 卡死。
- 大图/AI 模型推理时卡顿明显。

优化：

- Apply 按钮运行期间保持 disabled，已有代码部分已有。
- Engine 层仍应防御：
  - 如果 worker running，直接返回 busy 状态。
  - 或提供 cancel。
  - 不在 UI 线程同步 wait。

验证：

- 大图 AI denoise 中重复触发 apply。
- UI 不冻结。
- 用户得到 busy/取消提示。

### D2：Deblur 复用 DenoiseEngine，路径大小写和模型目录需要统一

位置：

- `src/ui/deblur_panel.py`
  - `RestorationEngine(model_subdir="Deblur")`
  - `params["model_path"] = os.path.join("models/Deblur", model_name)`
- `src/core/denoise_engine.py`
  - `DenoiseEngine(model_subdir="denoise")`

观察：

- Deblur 使用 `models/Deblur`，Denoise 使用 `models/denoise`。
- macOS 默认大小写不敏感，但 Linux/打包环境可能敏感。

优化：

- 明确模型目录规范：
  - `models/deblur`
  - `models/denoise`
- 如果保留 `Deblur`，则所有文档、下载器、ModelManager 都同步。

验证：

- 在大小写敏感文件系统或 CI 上运行 model discovery 测试。

### D3：reset_defaults 会 emit reset_requested，可能语义过重

位置：

- `src/ui/denoise_panel.py`
  - `reset_defaults()`
- `src/ui/deblur_panel.py`
  - `reset_defaults()`

观察：

- reset defaults 不只重置 UI 参数，还 emit `reset_requested`。
- MainWindow reset handler 会恢复 source bake。

风险：

- 用户想重置参数，却同时撤销已经 bake 的 denoise/deblur 结果。
- 需要确认产品语义：Reset 是“重置参数”还是“撤销已应用结果”。

优化：

- 拆分两个按钮或两个 action：
  - Reset Params
  - Revert Baked Result
- 如果只保留一个按钮，UI 文案应明确。

验证：

- 应用 denoise bake 后点击 reset。
- 用户预期和实际结果一致。

---

## 建议实施顺序

### 阶段 1：修复崩溃与误触发

目标：保证项目恢复、图层切换、撤销/重做不会触发工具误写。

任务：

1. 修复 `WarperPanel` 缺少 `numpy` import。
2. 为以下 panel 增加 `_syncing_ui`：
   - `GradingPanel`
   - `RawPanel`
   - `QualifierPanel`
   - `BlurSharpenPanel`
   - `FilmGrainPanel`
3. `set_params()` 默认不 emit。
4. 停止/清空同步期间 throttle timer。
5. 为 `MainWindow.update_from_layer()` 建立“无副作用”测试。

验收：

- 打开项目不崩溃。
- 图层切换不触发 engine setter。
- `QTest.qWait(50)` 后仍无延迟 emit。

### 阶段 2：修复工具行为错误

目标：UI 显示、Layer 参数、shader 输出一致。

任务：

1. 修复 Blur 覆盖 Sharpen 的 shader 行为。
2. 修复 Blur/Sharpen advanced radius 恢复。
3. 修复 Blur mode-specific 参数恢复。
4. 修复 Warper 网格密度切换状态不一致。
5. 修复 Film Grain seed 每次调参变化。
6. 修复 LUT Reset UI 状态。

验收：

- 保存/打开后所有工具参数一致。
- 同时开 Sharpen + Blur 时输出不同于只开 Blur。
- Film Grain 拖动参数不跳 seed。
- LUT reset 后 UI 无残留 active selection。

### 阶段 3：统一路径、reset 语义和视频策略

目标：减少未来工具新增时重复踩坑。

任务：

1. 新增 `src/utils/paths.py` 或扩展现有路径工具。
2. RAW LUT 和 Gallery LUT 使用同一个 LUT dir。
3. Film Grain 改走 MainWindow handler，视频播放中统一 debounce。
4. Denoise/Deblur reset 文案和 bake revert 行为拆分。
5. DenoiseEngine 避免 UI 线程 `wait()`。

验收：

- 不同 cwd 启动时 LUT 一致。
- 视频播放时所有 Color 调色工具参数更新策略一致。
- Denoise/Deblur 重复 apply 不冻结 UI。

### 阶段 4：色彩数学和渲染契约整理

目标：建立可维护的 color math contract。

任务：

1. Grading HDR/负值/高光行为写入文档。
2. Qualifier HSL/HSV 语义统一。
3. Matte generation 与 composition shader 中 qualifier 的职责去重。
4. 建立 synthetic image golden tests。

验收：

- 关键 shader 有 CPU reference 或 golden image。
- preview、full-res、export 对同一参数输出一致。

---

## 自动化测试计划

### 1. Panel set_params 无副作用测试

建议新增：

- `tests/ui/test_color_panel_sync_no_emit.py`

覆盖：

- `GradingPanel.set_params()`
- `RawPanel.set_params()`
- `QualifierPanel.set_params()`
- `BlurSharpenPanel.set_params_dict()`
- `FilmGrainPanel.set_params()`
- `WarperPanel.set_params()`

断言：

- set_params 期间和之后 50 ms 内无用户变化 signal。
- 内部 pending timer 清空。

### 2. MainWindow update_from_layer 无副作用测试

建议新增：

- `tests/ui/test_update_from_layer_no_engine_write.py`

方法：

- 使用 FakeEngine 替换 engine setter。
- 构造包含所有 Color 参数的 Layer。
- 调用 `update_from_layer(layer)`。
- 等待 timers。

断言：

- engine setters 调用次数为 0。
- request_render 调用次数为 0。

### 3. Blur/Sharpen 参数恢复测试

建议新增：

- `tests/ui/test_blur_sharpen_panel_params.py`

覆盖：

- advanced radius 保存/恢复。
- 4 个 blur mode-specific 参数保存/恢复。
- reset_all/reset_blur 后 params 与 UI 一致。

### 4. Warper 恢复测试

建议新增：

- `tests/ui/test_warper_panel_restore.py`

覆盖：

- 6x6、8x8、12x12 grid_data。
- `set_params()` 不报错。
- grid combo 显示正确。
- 切换 grid size 后 emit 清空状态。

### 5. LUT 路径与 reset 测试

建议新增：

- `tests/ui/test_lut_gallery_state.py`

覆盖：

- 不同 cwd 下 `get_lut_dir()` 一致。
- reset 清空 selection。
- 同名导入不覆盖。
- active LUT remove 时 engine reset。

### 6. Film Grain seed 测试

建议新增：

- `tests/core/test_film_grain_params.py`

覆盖：

- 修改 intensity 不改变 seed。
- 显式 randomize 改变 seed。
- video animated grain 使用 time/frame 参数，不污染静态 seed。

### 7. Qualifier matte 一致性测试

建议新增：

- `tests/render/test_qualifier_matte_consistency.py`

覆盖：

- source matte 和 composition path 不重复乘 q_matte。
- clean_black/clean_white/blur 在 preview/full/export 一致。

---

## 手动回归清单

### 基础状态

1. 打开普通 JPEG/PNG。
2. Color 页面所有工具保持默认。
3. 切换图层。
4. 确认没有自动新增历史、自动保存、无意义 render。

### Primaries / Grading

1. 输入小数参数，例如 Exposure `0.5`。
2. 保存项目。
3. 重新打开。
4. 确认参数仍为 `0.5`。

### RAW

1. 从不同 cwd 启动应用。
2. RAW 输入 LUT 列表一致。
3. 打开 RAW 文件，RAW 控制只初始化一次。
4. 打开非 RAW 文件，RAW 默认关闭且不触发 worker。

### Qualifier

1. 用 picker 选择颜色。
2. 调整 blur、clean black、clean white。
3. 切换 mask preview / normal preview。
4. full-res 导出后边缘一致。

### Warper

1. 拖动几个色彩控制点。
2. 保存项目并重开。
3. 确认不崩溃且 warper 恢复。
4. 切换 grid density，画面和 UI 同步清空旧 warp。

### Blur / Sharpen

1. 同时开启 Sharpen 和 Blur。
2. 确认结果不是只开 Blur。
3. 高级模式设置 radius `5.0`。
4. 保存项目并重开，radius 仍是 `5.0`。
5. 逐个 blur mode 设置参数，保存重开后切换 mode，值全部恢复。

### Film Grain

1. 开启 grain。
2. 拖动 intensity。
3. 确认颗粒图案不随机跳变，只改变强度。
4. 点击 reset，参数立即恢复默认。

### LUT

1. 应用 LUT。
2. 调整 intensity。
3. 点击 reset。
4. 确认画面无 LUT，UI 无选中 LUT。
5. 导入同名不同 LUT，确认不会覆盖旧文件。

### Denoise / Deblur

1. AI 处理大图时重复点击 Apply。
2. UI 不冻结。
3. Reset Params 不误撤销 bake，或者文案明确为 Revert Baked Result。
4. 保存项目后 baked source 能恢复。

---

## 风险与注意事项

1. 不要把所有 reset 都改成同一种行为，先定义产品语义：
   - reset 参数
   - reset enable
   - reset baked source
   - reset selected LUT
2. 不要在 UI 面板里直接调用 engine，继续保持 MainWindow 作为信号协调层。
3. 不要在 UI 线程等待 AI worker。
4. shader 修改后必须验证 preview、full-res、export 三条路径。
5. 所有 GLSL 数字保持显式 float，例如 `1.0`，for 循环边界保持常量。

---

## 完成标准

本计划完成后应满足：

1. Color 页面所有工具的 `set_params()` 都是无副作用同步。
2. 用户操作才会触发 engine 更新、history、autosave。
3. 项目保存/恢复后 UI、Layer、Renderer 参数一致。
4. 视频播放中 Color 调参都走统一 debounce/缓存失效策略。
5. LUT/模型/资源路径不依赖启动 cwd。
6. 明确区分参数 reset 和 baked source revert。
7. 关键 shader 行为有自动化或黄金图验证。

