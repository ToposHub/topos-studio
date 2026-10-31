# Topos Color 常用调色工具 Bug 审计与优化方案

日期：2026-05-24

范围：Curves、Soft Clip、HSL Curves、Mixer

技术栈：PyQt6 + ModernGL + GLSL 动态融合管线

## 结论

本次检查没有发现 Curves、Soft Clip、HSL Curves、Mixer 在主预览路径中完全不可用的灾难性问题。当前日常调色预览大体走通：

```text
UI Widget/Panel
  -> MainWindow 信号处理
  -> ImageEngine 更新 Layer 参数 / LUT
  -> AsyncRenderer 上传辅助纹理与渲染参数
  -> ShaderPipeline.build_composition_shader()
  -> shader_nodes.glsl 片段执行
```

但这四个工具周围存在多处会影响体验、保存恢复、脚本 API、LUT 导出一致性和视频播放性能的缺陷。优先级最高的是：HSL Curves 重复触发渲染、Mixer 面板对部分参数不健壮、Curves/HSL 的旧 LUT 恢复路径为空、脚本 API 坐标和数据结构错误、LUT Exporter 与实时预览算法不一致。

## 本次检查到的限制

尝试运行现有测试时，当前本机环境未能完成验证：

| 验证项 | 结果 | 阻塞原因 |
|---|---|---|
| `pytest tests/test_layer.py tests/test_save_router.py` | 未通过收集 | 当前 Python 环境缺少 `cv2` |
| UI 轻量脚本触发 | 未运行成功 | 当前 Python 环境缺少 `scipy`、`qtawesome` |
| `pytest tests/test_image_engine_params.py` | 未通过 | 测试上下文异常指向 `topos-color01`，并报 `src.core` 未暴露 `image_engine` 属性 |

因此本文中的问题判断主要基于静态代码审计、信号链路追踪和渲染管线阅读。修复后必须补齐环境并重新跑验证命令。

## 工具数据流

### Curves

```text
src/ui/widgets/curves_widget.py
  changed(lut, curves_data)
src/ui/main_window.py:on_curves_changed()
  engine.apply_curves(lut)
  layer.curves_data = curves_data
src/core/image_engine.py:apply_curves()
  layer.curves_lut = curves_lut
  renderer.UPDATE_AUX_TEX(type="curves")
  render_pipeline()
src/core/async_renderer.py:_do_update_aux_tex()
  上传 256x1 RGB float texture
src/core/shader_compiler.py
  build_composition_shader(has_curves=True)
src/core/shader_nodes.py:apply_curves()
```

### HSL Curves

```text
src/ui/widgets/hsl_curves_widget.py
  changed(lut, curves_data)
src/ui/hsl_curves_panel.py:_on_hsl_curve_changed()
  engine.apply_hsl_curve(lut)
src/ui/main_window.py:on_hsl_curve_changed()
  engine.apply_hsl_curve(lut)
  layer.hsl_curve_data = curves_data
src/core/image_engine.py:apply_hsl_curve()
  layer.hsl_curve_lut = hsl_curve_lut
  renderer.UPDATE_AUX_TEX(type="hsl_curve")
  render_pipeline()
src/core/async_renderer.py:_do_update_aux_tex()
  上传 6x256 R32F texture
src/core/shader_compiler.py
  build_composition_shader(has_hsl_curve=True)
src/core/shader_nodes.py:apply_hsl_curve()
```

### Mixer

```text
src/ui/mixer_panel.py
  mixer_changed(params)
src/ui/main_window.py:on_mixer_changed()
  engine.set_mixer_params(params)
  request_render()
src/core/image_engine.py:set_mixer_params()
  layer.mixer_params = params
src/core/async_renderer.py:_update_composition_uniforms()
  u_red_p ... u_magenta_p
src/core/shader_nodes.py:apply_mixer()
```

### Soft Clip

```text
src/ui/soft_clip_panel.py
  params_changed(params)
src/ui/main_window.py direct connect
  engine.set_soft_clip_params(params)
src/core/image_engine.py:set_soft_clip_params()
  layer.soft_clip_params.update(params)
  render_pipeline()
src/core/async_renderer.py:_update_composition_uniforms()
  u_sc_*
src/core/shader_nodes.py:apply_soft_clip()
```

## P0 必须修复

### P0-1 HSL Curves 变化会重复应用与重复渲染

位置：

- `src/ui/hsl_curves_panel.py:75-85`
- `src/ui/main_window.py:2439-2441`
- `src/ui/main_window.py:6241-6252`

现象：

`HSLCurvesPanel` 内部已经将 `hsl_curves_widget.changed` 连接到 `_on_hsl_curve_changed()`，并在其中直接调用 `engine.apply_hsl_curve(lut)`。`MainWindow` 又把同一个 widget 的 `changed` 信号连接到 `on_hsl_curve_changed()`，该函数再次调用 `engine.apply_hsl_curve(lut)`、写入 `layer.hsl_curve_data`、请求渲染。

影响：

- 每次拖动 HSL 曲线可能上传两次 HSL LUT。
- 每次变化可能触发两次 `render_pipeline()`。
- 视频播放时会增加 render queue 压力，造成拖动卡顿或播放抖动。
- HSL 曲线与其他调色面板的信号职责不一致，后续维护容易误接。

根因：

Panel 层和 MainWindow 层都在处理同一条业务信号。`HSLCurvesPanel` 应该只作为 UI 包装和信号转发，业务状态写入与渲染调度应统一由 `MainWindow` 或统一 controller 负责。

优化方案：

1. 移除 `HSLCurvesPanel._on_hsl_curve_changed()` 中对 `engine.apply_hsl_curve(lut)` 的直接调用。
2. 给 `HSLCurvesPanel` 增加清晰的转发信号，例如 `curves_changed = pyqtSignal(np.ndarray, dict)`，由 panel 转发 widget 的 `changed`。
3. `MainWindow` 只连接 `self.hsl_curves_panel.curves_changed.connect(self.on_hsl_curve_changed)`。
4. `interaction_started/ended` 也统一转发到 `MainWindow`，避免 panel 直接改 `engine.is_interacting`。
5. 如果为了兼容短期不重构，也至少删除 panel 内部的 `engine.apply_hsl_curve(lut)`，保留 MainWindow 当前连接。

验收标准：

- 拖动 HSL 曲线时，每次节流 tick 只调用一次 `ImageEngine.apply_hsl_curve()`。
- `renderer.UPDATE_AUX_TEX(type="hsl_curve")` 每次变化只提交一次。
- `layer.hsl_curve_data` 正常保存。
- 播放视频时拖动 HSL 曲线不出现明显重复渲染日志。

建议测试：

```python
def test_hsl_curves_changed_applies_once(qtbot, main_window, monkeypatch):
    calls = []
    monkeypatch.setattr(main_window.engine, "apply_hsl_curve", lambda lut: calls.append(lut))

    lut = np.ones((6, 256), dtype=np.float32)
    data = {"LumSat": [(0.0, 127.5), (255.0, 127.5)]}
    main_window.hsl_curves_panel.hsl_curves_widget.changed.emit(lut, data)

    assert len(calls) == 1
```

手工验证：

1. 打开一张图片。
2. 打开 HSL Curves 面板，拖动 Hue vs Sat 曲线。
3. 观察日志或临时计数器，确认 `apply_hsl_curve()` 不重复。
4. 切换图层再切回来，确认曲线控制点仍在。

### P0-2 Mixer 面板无法安全加载部分参数

位置：

- `src/ui/mixer_panel.py:218-245`

现象：

`MixerPanel.set_params()` 做 `self.params = params.copy()` 后，`_refresh_ui()` 直接访问 `self.params[color][param_idx]`。如果传入旧工程、脚本或迁移数据，例如：

```python
{"red": [10, 0, 0]}
```

则访问 `orange`、`yellow` 等缺失 key 时会抛 `KeyError`。

影响：

- 旧项目或不完整参数可能导致切换图层、加载工程时 UI 崩溃。
- 脚本 API 或外部导入只设置部分颜色时，Mixer 面板无法显示。
- `Layer.from_dict()` 默认接受任何 `mixer_params`，但 UI 没有做 schema 归一化。

根因：

`MixerPanel` 假定所有 8 个颜色都有完整 `[hue, sat, lum]` 列表，没有做默认值合并和类型校验。

优化方案：

1. 增加 `_normalize_params(params: dict | None) -> dict[str, list[int]]`。
2. 默认值始终为：

```python
{color: [0, 0, 0] for color in self.colors}
```

3. 对每个颜色做容错：
   - 缺失颜色：填 `[0, 0, 0]`
   - list/tuple 长度不足：补齐到 3 个值
   - dict 格式：兼容脚本 API 的 `{"hue": 0, "saturation": 0, "lightness": 0}`
   - 非数字：回退 0
   - 超范围：限制到 `[-100, 100]`
4. `set_params()` 只使用归一化后的副本。
5. `_emit_pending_params()` 发出深拷贝，避免外部持有同一个 `self.params` 对象后意外修改。

验收标准：

- `MixerPanel.set_params({"red": [10, 0, 0]})` 不崩溃。
- `MixerPanel.set_params({"red": {"hue": 10}})` 能显示 red hue=10。
- 所有未提供颜色显示 0。
- 拖动任一 slider 后发出的 params 始终包含 8 个颜色，每个值都是长度 3 的 list。

建议测试：

```python
def test_mixer_panel_accepts_partial_params(qtbot):
    panel = MixerPanel()
    qtbot.addWidget(panel)

    panel.set_params({"red": [10, 0, 0]})

    assert panel.params["red"] == [10, 0, 0]
    assert panel.params["orange"] == [0, 0, 0]
    assert panel.params["magenta"] == [0, 0, 0]
```

### P0-3 Curves/HSL Curves 的 `set_lut()` 是空实现

位置：

- `src/ui/widgets/curves_widget.py:614-616`
- `src/ui/widgets/hsl_curves_widget.py:844-846`
- `src/ui/main_window.py:5531-5545`
- `src/ui/tools_panel.py:211-215`

现象：

`update_from_layer()` 在图层只有 `curves_lut`、没有 `curves_data` 时会调用 `curves_widget.set_lut(layer.curves_lut)`，但该函数直接 `pass`。HSL Curves 的 `set_lut()` 也是空实现。

影响：

- 旧工程只保存 LUT、没有控制点时，图像效果可能还在，但 UI 曲线显示为默认或不更新。
- 脚本 API 设置 Curves 后只写 `layer.curves_lut`，没有写 `layer.curves_data`，切换图层后 UI 无法恢复曲线形状。
- 用户会看到“画面被调了，但曲线面板是直线”的状态，容易误操作覆盖旧效果。

根因：

项目后来转向保存 control points，但 legacy LUT fallback 未实现 UI 逆向显示。

优化方案：

短期方案：

1. `set_lut()` 不再 `pass`。
2. 对 RGB Curves：
   - 接受 `(256, 1, 3)` 或 `(256, 3)` 或 `(256, 1)`。
   - 将 LUT 近似转换成每通道控制点。
   - 至少生成 2 到 9 个采样点，用于 UI 显示。
   - 注意 UI y 坐标是反向的：`y = 255.0 - lut_value * 255.0`。
3. 对 HSL Curves：
   - 接受 `(6, 256)`。
   - multiplier 曲线转换：`y = 255.0 - value * 127.5`。
   - HueHue shift 曲线转换：`y = 127.5 - shift * 255.0`。
   - 生成每个 mode 的近似控制点。
4. `set_lut()` 应标记 dirty 并更新 UI，但不要主动 emit changed，避免仅切换图层就覆盖数据。

中期方案：

优先保存和恢复 `curves_data` / `hsl_curve_data`。只有旧工程或脚本没有 control points 时才走 LUT 逆向近似。

验收标准：

- 只有 `curves_lut` 的 layer，切换到该 layer 后 Curves 面板显示非默认曲线。
- `set_lut()` 不会触发 `changed`，不会在图层切换时发起新渲染。
- 旧项目打开后不丢 UI 状态。

建议测试：

```python
def test_curves_widget_set_lut_restores_visible_points(qtbot):
    widget = CurvesWidget()
    qtbot.addWidget(widget)

    lut = np.zeros((256, 1, 3), dtype=np.float32)
    lut[:, 0, 0] = np.linspace(0.0, 1.0, 256)
    lut[:, 0, 1] = np.linspace(1.0, 0.0, 256)
    lut[:, 0, 2] = 0.5

    with qtbot.assertNotEmitted(widget.changed):
        widget.set_lut(lut)

    data = widget.get_curves_data()
    assert data["G"] != [(0.0, 255.0), (255.0, 0.0)]
```

### P0-4 脚本 API 的 Curves/HSL Curves 坐标约定错误

位置：

- `src/scripting/api.py:27-54`
- `src/scripting/api.py:308-345`
- `src/scripting/api.py:361-393`

现象：

脚本 API 文档示例写的是：

```python
topos.set_curves(master=[(0, 0), (128, 140), (255, 255)])
```

但 `_interpolate_curve()` 内部用 `t = i / 255.0`，然后拿 `t` 与点的 x 值比较。这意味着函数实际期待 x 是 `0.0-1.0`，但文档和注释说点是 `0-255`。传入 `128`、`255` 时，绝大多数采样都会落在第一个控制点之前或错误区间。

HSL Curves 还有额外问题：当 `layer.hsl_curve_lut is None` 时，初始化为 6 条 `0..1` ramp：

```python
for c in range(6):
    layer.hsl_curve_lut[c] = np.linspace(0, 1, 256)
```

但 HSL shader 的默认应是：

- LumSat = `1.0`
- HueHue = `0.0`
- HueSat = `1.0`
- HueLum = `1.0`
- SatSat = `1.0`
- SatLum = `1.0`

影响：

- `topos.set_curves()` 生成错误曲线。
- `topos.set_hsl_curve()` 第一次调用时，未修改的 HSL 曲线会有非预期效果。
- 自动化调色和批处理输出与 UI 调色不一致。

根因：

脚本 API 的曲线插值函数与 UI 的曲线坐标系统没有统一。HSL 默认 LUT 没有复用 renderer/default widget 的约定。

优化方案：

1. 将 `_interpolate_curve()` 改成显式参数：

```python
def _interpolate_curve(points, x_range="255", y_range="255", output="rgb_curve"):
```

或拆成两个函数：

```python
_interpolate_rgb_curve_points_255(points) -> np.ndarray
_interpolate_hsl_curve_points_255(points, curve_type) -> np.ndarray
```

2. RGB Curves：
   - 输入点统一使用 `0-255`。
   - 输出点统一使用 `0-255`。
   - 返回值归一化到 `0.0-1.0`。
3. HSL Curves：
   - 输入点 x 使用 `0-255`。
   - 输入点 y 使用 UI 坐标 `0-255`，和 `HSLCurvesWidget` 一致。
   - multiplier 类型输出 `(255.0 - y) / 127.5`。
   - HueHue 输出 `(127.5 - y) / 255.0`。
4. 初始化 HSL 默认 LUT 时使用同一份 helper：

```python
defaults = np.zeros((6, 256), dtype=np.float32)
defaults[0] = 1.0
defaults[2] = 1.0
defaults[3] = 1.0
defaults[4] = 1.0
defaults[5] = 1.0
```

5. `set_curves()` 同时写入 `layer.curves_data`，便于 UI 恢复。
6. `set_hsl_curve()` 同时写入完整 `layer.hsl_curve_data`，未修改曲线用默认点。

验收标准：

- 脚本示例 `topos.set_curves(master=[(0,0),(128,140),(255,255)])` 生成中点约 `140/255`。
- 只设置一条 HSL 曲线时，其他 5 条保持 identity。
- 脚本设置后切换图层，UI 能显示对应控制点。

建议测试：

```python
def test_script_curve_points_use_255_domain():
    lut = _interpolate_rgb_curve_points_255([(0, 0), (128, 140), (255, 255)])
    assert abs(lut[128] - 140.0 / 255.0) < 0.01

def test_script_hsl_defaults_are_identity():
    lut = build_default_hsl_lut()
    assert np.allclose(lut[0], 1.0)
    assert np.allclose(lut[1], 0.0)
    assert np.allclose(lut[2], 1.0)
    assert np.allclose(lut[3], 1.0)
    assert np.allclose(lut[4], 1.0)
    assert np.allclose(lut[5], 1.0)
```

### P0-5 脚本 API 的 Mixer 数据结构与渲染端不兼容

位置：

- `src/scripting/api.py:602-620`
- `src/core/async_renderer.py:1870-1874`
- `src/core/shader_compiler.py:790-801`

现象：

脚本 API 写入：

```python
mp[color] = {"hue": 0, "saturation": 0, "lightness": 0}
```

但渲染端读取时期待：

```python
{"red": [hue, saturation, luminance]}
```

如果值是 dict，`get_v3()` 会认为不是 list/tuple，回退 `(0, 0, 0)`。

影响：

- `topos.set_mixer(red={"hue": 20})` 看似成功，实际渲染无效果。
- 保存后 UI 也无法按预期显示，因为 `MixerPanel` 同样期待 list。

根因：

脚本 API 使用了语义 dict，核心渲染和 UI 使用了三元 list，缺少统一 schema 或转换层。

优化方案：

1. 将 `ToposAPI.set_mixer()` 写入格式改为 list：

```python
current = normalize_existing_mixer_value(mp.get(color))
current[0] = values.get("hue", current[0])
current[1] = values.get("saturation", current[1])
current[2] = values.get("luminance", values.get("lightness", current[2]))
mp[color] = current
```

2. 保留 `lightness` 作为兼容别名，但内部统一字段叫 luminance 或 lum。
3. `MixerPanel._normalize_params()` 兼容旧 dict。
4. `Layer.from_dict()` 或保存迁移层也做一次 mixer schema 归一化。

验收标准：

- 脚本设置 Mixer 后实时预览有变化。
- `layer.mixer_params["red"]` 是长度 3 的 list。
- 旧 dict 格式项目打开后被转换，不崩溃。

建议测试：

```python
def test_script_mixer_writes_renderer_schema(api, engine):
    api.set_mixer(red={"hue": 20, "saturation": 10, "lightness": -5})
    layer = engine.get_active_layer()
    assert layer.mixer_params["red"] == [20, 10, -5]
```

### P0-6 LUT Exporter 与实时预览不一致

位置：

- `src/core/shader_compiler.py:637-644`
- `src/core/lut_exporter.py:380-393`
- `src/core/lut_exporter.py:637-723`
- `src/core/lut_exporter.py:725-760`

实时 composition shader 顺序：

```text
Grading
  -> Curves
  -> HSL Curves
  -> Soft Clip
  -> Mixer
  -> Warper
  -> LUT
  -> Plugins
```

LUT Exporter 当前顺序：

```text
Grading
  -> Curves
  -> Mixer
  -> Soft Clip
  -> LUT
```

问题：

- LUT Exporter 完全没有应用 HSL Curves。
- Mixer 和 Soft Clip 顺序与实时预览不同。
- Soft Clip CPU 的 contrast 公式与 shader 不一致。shader 使用 `factor = 1.0 + contrast` 或倒数；exporter 使用 `1.0 + contrast / 100.0`。
- Curves CPU 对 HDR/负值采用 `np.clip(result * 255, 0, 255)`，而 shader `apply_curves()` 支持线性外推。
- Mixer CPU clamp HSV 的 saturation/value 到 `[0,1]`，shader 没有完全相同的 HDR 处理行为。

影响：

- 导出的 `.cube` 与用户实时预览不一致。
- HSL Curves 调色无法被 bake 进导出 LUT。
- Soft Clip/Mixer 同时使用时，导出偏差会非常明显。

根因：

CPU LUT 导出维护了独立算法，没有与 shader 节点共享数学定义，也没有跟随 composition shader 的顺序更新。

优化方案：

1. 明确 LUT Exporter 的目标：
   - 如果导出的是“调色工具 LUT”，应尽量匹配实时 shader 中所有像素无关的调色节点。
   - Warper、mask、qualifier、blur、grain、plugin 可按能力明确排除或单独处理。
2. 调整 CPU 顺序为：

```text
Grading
  -> Curves
  -> HSL Curves
  -> Soft Clip
  -> Mixer
  -> 3D LUT
```

3. 新增 `_apply_hsl_curves_cpu(lut, hsl_curve_lut)`，实现与 `shader_nodes.py:apply_hsl_curves()` 同等逻辑。
4. 重写 `_apply_soft_clip_cpu()`，逐行对应 shader：
   - exposure: `result *= 2 ** exposure`
   - contrast: `factor = 1.0 + contrast` 或 `1.0 / (1.0 + abs(contrast))`
   - toe per channel
   - dark balance correction
   - shoulder ratio-preserving compression
5. `_apply_curves_cpu()` 增加 shader 同款边界外推，避免强制 clip。
6. `_apply_mixer_cpu()` 与 shader 保持相同 hue centers、mask 和 HDR 策略。
7. 加像素级 golden test：对同一批 RGB 样本，CPU exporter 与 shader reference 或 Python shader-port 输出误差在阈值内。

验收标准：

- 只开 Curves，导出 LUT 与预览一致。
- 只开 HSL Curves，导出 LUT 能体现 HSL 调整。
- 同时开 Soft Clip + Mixer，导出 LUT 与预览顺序一致。
- CPU reference 与 shader-port 的最大误差小于 `1e-3` 到 `3e-3`，按 LUT 尺寸和插值误差确定。

建议测试：

```python
def test_lut_exporter_applies_hsl_curves():
    exporter = LUTExporter()
    lut = make_identity_cube(size=17)
    hsl = build_default_hsl_lut()
    hsl[2] = 2.0  # HueSat: double saturation

    out = exporter._apply_hsl_curves_cpu(lut, hsl)
    assert not np.allclose(out, lut)

def test_lut_exporter_order_matches_shader_pipeline():
    layer = {
        "grading_params": {},
        "curves_lut": make_test_curves(),
        "hsl_curve_lut": make_test_hsl_curves(),
        "soft_clip_params": {"enabled": True, "high": 0.8},
        "mixer_params": {"red": [10, 20, -5]},
    }
    out = exporter._apply_grading_cpu(identity, layer)
    ref = python_reference_shader_order(identity, layer)
    assert np.max(np.abs(out - ref)) < 0.003
```

## P1 高优先级优化

### P1-1 HSL Curves 没有接入旧 `build_shader()` 融合路径

位置：

- `src/core/shader_compiler.py:33-437`
- `src/core/shader_compiler.py:439-678`

现状：

主预览路径使用 `build_composition_shader()`，这里已经有 `has_hsl_curve`、`u_hsl_curve_tex` 和 `apply_hsl_curve()`。但旧的 `build_shader()` 多层融合路径只处理 Curves、Soft Clip、Mixer，没有处理 HSL Curves。

影响：

- 如果仍有导出、全分辨率、旧同步渲染或未来某个路径调用 `build_shader()`，HSL Curves 会被忽略。
- 代码出现“双管线行为不一致”，后续修复容易漏。

优化方案：

1. 搜索并确认 `build_shader()` 是否仍为生产路径。
2. 如果不再使用：标记 deprecated，删除或只保留测试 stub。
3. 如果仍使用：补齐 HSL Curves：
   - pipeline hash 加 `hsl_curve`
   - uniform declaration 加 `L{i}_hsl_curve_tex`
   - shader order 在 Curves 后、Soft Clip 前调用 `apply_hsl_curve`
   - uniform/texture binding 逻辑补齐
4. 给 `build_shader()` 和 `build_composition_shader()` 建立同一份 feature-order spec，防止分叉。

验收标准：

- 任意仍使用 `build_shader()` 的路径也支持 HSL Curves。
- 两条管线的调色工具顺序一致。

### P1-2 Soft Clip 面板缺少完整交互结束信号

位置：

- `src/ui/soft_clip_panel.py:421-479`
- `src/ui/main_window.py:2508-2517`

现象：

Soft Clip 没有像 Mixer/Curves 那样定义 `interaction_started` / `interaction_ended`。`MainWindow` 目前用 `params_changed` 上的 lambda 检查部分 slider 是否 down 来调用 `on_interaction_started()`，但没有稳定的 ended 对应逻辑，而且没有覆盖 exposure/contrast slider。

影响：

- 拖动 Soft Clip 时交互状态可能不能正确结束。
- 视频播放时 render debounce 和交互状态不可靠。
- exposure/contrast 拖动不进入 interaction_started 判断。

优化方案：

1. `SoftClipPanel` 增加：

```python
interaction_started = pyqtSignal()
interaction_ended = pyqtSignal()
```

2. 在 `create_double_slider()` 中连接：

```python
slider.sliderPressed.connect(self.interaction_started.emit)
slider.sliderReleased.connect(self.interaction_ended.emit)
```

3. enable checkbox 点击也可发出 started/ended，或由 MainWindow 直接普通 render。
4. 删除 `MainWindow` 中检查 slider down 的 lambda。
5. `params_changed` 只负责参数变化，不负责交互生命周期。

验收标准：

- 拖动 high、white point、shoulder、low、black、toe、exposure、contrast 都有 started/ended。
- 松手后 `engine.is_interacting` 恢复 false。
- 播放中拖动 Soft Clip 不无限触发 full render。

### P1-3 Soft Clip 初始 UI 会在构造时发出参数变化

位置：

- `src/ui/soft_clip_panel.py:380-381`
- `src/ui/soft_clip_panel.py:489-503`

现象：

`SoftClipPanel.init_ui()` 末尾调用 `self.emit_params()`。如果未来连接顺序或面板复用变化，这种构造期信号可能在未加载图片时触发引擎状态变更。

当前 `MainWindow` 多数情况下是在构造后才连接信号，因此风险不一定立刻触发。但它仍是不稳定设计。

优化方案：

1. 构造期只更新 `curve_viz`，不 emit 外部信号。
2. 拆成：

```python
def _collect_params(self) -> dict: ...
def _update_visualizer(self) -> None: ...
def emit_params(self) -> None:
    params = self._collect_params()
    self._update_visualizer(params)
    if not self._suppress_emit:
        self.params_changed.emit(params)
```

3. `set_params()` 期间使用 `_suppress_emit` 或 `QSignalBlocker`，最后只在明确需要时 emit。

验收标准：

- 初始化 MainWindow 时不会因为 Soft Clip 构造发起 render。
- `set_params()` 用于图层切换时不意外覆盖当前 layer。

### P1-4 Curves/HSL Curves 的 `set_curves_data()` 会 emit changed，图层切换可能触发渲染

位置：

- `src/ui/widgets/curves_widget.py:593-612`
- `src/ui/widgets/hsl_curves_widget.py:826-842`
- `src/ui/main_window.py:5526-5550`

现象：

`set_curves_data()` 内部调用 `update_lut()`，`update_lut()` 会启动 16ms timer，随后 emit `changed`。当 `MainWindow.update_from_layer()` 只是想把 UI 同步到新 layer 时，这可能触发 `on_curves_changed()` 或 `on_hsl_curve_changed()`，导致：

- 图层切换触发不必要 render。
- UI 恢复数据又写回 layer。
- 在异步 timer 场景下可能写到已经切换后的 active layer。

优化方案：

1. 为 widget 增加 `_suppress_changed`。
2. `set_curves_data(data, emit_changed: bool = False)` 默认不发业务 changed。
3. 内部更新 LUT cache 但不启动 timer。
4. 用户鼠标拖动、reset、双击、拾色加点才 emit。
5. MainWindow 图层切换时显式使用 `emit_changed=False`。

验收标准：

- 切换图层时 Curves/HSL Curves 不调用 `engine.apply_*`。
- 手动拖动曲线仍正常发出 changed。

### P1-5 Curves 的通道顺序注释和实现需要统一

位置：

- `src/ui/widgets/curves_widget.py:11`
- `src/ui/widgets/curves_widget.py:524-555`
- `src/scripting/api.py:315-342`

现象：

`CurvesWidget` 注释写 “BGR”，但代码中 `('R', 0), ('G', 1), ('B', 2)` 将 R 放在 texture channel 0。shader `texture(...).rgb` 也按 RGB 读。脚本 API 又注释 LUT 是 BGR，并将 blue 放 slot 0、red 放 slot 2。

影响：

- UI Curves 与脚本 API Curves 的红蓝通道可能相反。
- 开发者看到不同注释后容易继续写错。

优化方案：

1. 全项目统一 Curves LUT channel order 为 RGB。
2. 修改脚本 API 注释和赋值：

```python
if red: lut[:, 0, 0] = ...
if green: lut[:, 0, 1] = ...
if blue: lut[:, 0, 2] = ...
```

3. 修改 `CurvesWidget.changed` 注释。
4. 检查 `lut_exporter._apply_curves_cpu()` 是否假定 RGB，并补测试。

验收标准：

- 调红色曲线只影响红色。
- 脚本 API `set_curves(red=...)` 与 UI R 曲线效果一致。

### P1-6 保存恢复时只保存 control points，不重建 LUT 的路径要明确

位置：

- `src/core/layer.py:838-845`
- `src/core/layer.py:927-938`
- `src/ui/main_window.py:1515-1522`

现状：

`Layer.to_dict()` 只保存 `curves_data` 和 `hsl_curve_data`，不保存 LUT。这是合理的，但加载后必须确保：

1. UI 从 control points 重建 LUT。
2. 引擎拿到重建后的 `layer.curves_lut` / `layer.hsl_curve_lut`。
3. 在用户未打开相关面板时也能应用这些曲线。

风险：

如果只把 `layer.curves_data` 赋值给 layer，而没有重建 LUT，渲染端看 `curves_lut is None`，曲线不会生效。当前 `update_from_layer()` 会让 UI set data 并 emit changed，这可能刚好重建 LUT，但这依赖 UI 副作用，架构不稳。

优化方案：

1. 抽出纯函数：

```python
build_curves_lut_from_data(curves_data) -> np.ndarray
build_hsl_curve_lut_from_data(hsl_curve_data) -> np.ndarray
```

2. `Layer.from_dict()` 或加载工程后直接重建 LUT，不依赖 UI。
3. UI widget 也复用同一套 builder，避免算法分叉。
4. `set_curves_data(..., emit_changed=False)` 时更新 UI cache，但 layer LUT 已由 engine/load 层重建。

验收标准：

- 打开项目后，即使 Curves/HSL 面板没有显示，渲染结果也包含曲线。
- 保存/关闭/重开后 Curves/HSL 画面一致。

## P2 中优先级改进

### P2-1 HSL Curves 中的 clamp 可能限制 HDR/极端调色

位置：

- `src/core/shader_nodes.py:270-297`
- `src/core/shader_nodes.py:315-340`

现状：

HSL Curves 对 luma/sat 采样做 `clamp(..., 0.0, 1.0)`，对 multiplier clamp 到 `0.0-2.0`。这对 UI 安全有帮助，但和 `.clinerules` 中“不暴力 clamp HDR”原则存在张力。

影响：

- HDR 高光区的 LumSat 采样被压到 1.0，无法区分 1.0 以上亮度。
- 曲线最大只能 2 倍，无法做更激进的脚本/专业参数。

优化方案：

短期保留 clamp，避免用户曲线导致画面炸裂。中期可增加 HDR-aware 模式：

- UI 仍显示 `0-255` 普通域。
- shader 对 `luma > 1.0` 使用最后两点斜率外推，类似 `apply_curves()`。
- multiplier 上限可保留 4.0 或由 UI 限制。

验收标准：

- 默认模式不变。
- HDR-aware 模式下高光曲线可继续影响 1.0 以上亮度。

### P2-2 Soft Clip shader 与可视化器数学应共享

位置：

- `src/ui/soft_clip_panel.py:108-153`
- `src/core/shader_nodes.py:837-899`

现状：

CurveVisualizer 用 Python 手写了一份 Soft Clip 曲线。它与 shader 当前大体相似，但后续 shader 一旦修改，UI 预览容易不同步。

优化方案：

1. 把 Soft Clip 数学写成一份 Python reference 函数，CurveVisualizer 和 CPU LUT Exporter 共用。
2. 给 shader 片段旁边保留 reference 测试，确保公式同步。
3. 修改 shader 时必须同步 reference 测试。

验收标准：

- CurveVisualizer 曲线与 CPU reference 完全一致。
- CPU reference 与 shader-port 的采样误差在阈值内。

### P2-3 Mixer 色相范围和 UI 色名需要确认

位置：

- `src/ui/mixer_panel.py:17-21`
- `src/core/shader_nodes.py:571-576`

现状：

UI hue map 中 purple=280、magenta=315；shader centers 中 purple=270、magenta=300。两者不完全一致。

影响：

- UI 渐变显示的中心色与 shader 实际影响中心略有偏差。
- 用户调 purple/magenta 时可能觉得影响范围和视觉提示不一致。

优化方案：

1. 定义唯一 `MIXER_COLOR_CENTERS`。
2. UI 和 shader generation 都从同一份中心表生成。
3. 如果 shader 常量不能动态共享，至少保持两个文件数值一致。

验收标准：

- UI hue map 与 shader centers 完全一致。
- red wrap-around、magenta/red 边界手工验证正常。

## 推荐执行顺序

1. 修 P0-1：HSL Curves 去掉重复应用和重复渲染。
2. 修 P0-2：Mixer 参数归一化，保证旧数据/部分数据不崩。
3. 修 P0-5 和 P1-5：统一 Mixer/Curves 的脚本 API schema 和通道顺序。
4. 修 P0-4：脚本 Curves/HSL Curves 坐标和默认 LUT。
5. 修 P0-3 和 P1-6：实现 LUT fallback，并把 LUT builder 从 UI 中抽成纯函数。
6. 修 P0-6：让 LUT Exporter 与实时 shader 顺序、HSL Curves、Soft Clip 数学一致。
7. 修 P1-2 到 P1-4：清理 Soft Clip 与 Curves/HSL 的 UI 交互生命周期和图层切换副作用。
8. 做 P2：HDR-aware、数学共享、Mixer 中心色统一。

## 建议新增测试文件

| 测试文件 | 覆盖内容 |
|---|---|
| `tests/ui/test_curves_widget.py` | `set_curves_data()` 不 emit、`set_lut()` 恢复 UI、RGB 通道顺序 |
| `tests/ui/test_hsl_curves_widget.py` | 默认 HSL LUT、`set_lut()` 恢复 UI、set data 不 emit |
| `tests/ui/test_mixer_panel.py` | partial params、dict legacy params、完整 params emission |
| `tests/ui/test_soft_clip_panel.py` | interaction_started/ended、set_params 不乱 emit |
| `tests/test_scripting_color_tools.py` | scripting Curves/HSL/Mixer 写入正确 schema |
| `tests/test_lut_exporter_color_tools.py` | exporter 与 shader reference 顺序和数学一致 |
| `tests/test_color_stack_roundtrip_tools.py` | Curves/HSL/Mixer/SoftClip 保存、加载、重建 LUT |

## 最小验证命令

修复环境后，先跑：

```bash
python3 -m py_compile \
  src/ui/widgets/curves_widget.py \
  src/ui/widgets/hsl_curves_widget.py \
  src/ui/hsl_curves_panel.py \
  src/ui/mixer_panel.py \
  src/ui/soft_clip_panel.py \
  src/ui/main_window.py \
  src/core/image_engine.py \
  src/core/async_renderer.py \
  src/core/shader_compiler.py \
  src/core/shader_nodes.py \
  src/core/lut_exporter.py \
  src/scripting/api.py
```

然后跑定向测试：

```bash
.venv/bin/python -m pytest \
  tests/test_image_engine_params.py \
  tests/test_layer.py \
  tests/test_save_router.py \
  tests/ui/test_curves_widget.py \
  tests/ui/test_hsl_curves_widget.py \
  tests/ui/test_mixer_panel.py \
  tests/ui/test_soft_clip_panel.py \
  tests/test_scripting_color_tools.py \
  tests/test_lut_exporter_color_tools.py \
  tests/test_color_stack_roundtrip_tools.py \
  -q --tb=short
```

如果 UI 测试跑在无显示环境：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/ui -q --tb=short
```

## 手工回归清单

### Curves

1. 打开图片，调 Master 曲线，画面变化正常。
2. 分别调 R/G/B 曲线，确认只影响对应通道。
3. 切换图层后再切回来，控制点不丢。
4. 保存项目、关闭、重开，曲线效果和 UI 控制点一致。
5. 使用脚本 `set_curves(red=...)`，确认红色通道不是蓝色通道。

### HSL Curves

1. 调 Hue vs Sat，确认只触发一次 `apply_hsl_curve()`。
2. 调 Hue vs Hue，确认色相环 wrap-around 正常。
3. 只设置一条 HSL 曲线，其他曲线保持 identity。
4. 保存项目、关闭、重开，不打开 HSL 面板也能看到 HSL 曲线效果。
5. 视频播放时拖动 HSL 曲线，确认没有明显重复卡顿。

### Mixer

1. 加载只有 `{"red": [20, 0, 0]}` 的旧/模拟参数，不崩溃。
2. 加载旧 dict 参数 `{"red": {"hue": 20}}`，自动转换。
3. 拖动 red/orange/yellow 等 8 个颜色，画面变化正常。
4. 保存项目、关闭、重开，Mixer 参数一致。
5. 脚本 `set_mixer(red={"hue": 20})` 能实际改变画面。

### Soft Clip

1. 开启 Soft Clip，high/white/shoulder/toe 等参数变化正常。
2. 拖动每个 slider 都有 started/ended。
3. exposure/contrast slider 也进入交互生命周期。
4. 保存项目、关闭、重开，Soft Clip 参数一致。
5. LUT 导出后与预览对比，Soft Clip 曲线观感一致。

### LUT Export

1. 只开 Curves 导出 LUT，对比预览。
2. 只开 HSL Curves 导出 LUT，对比预览。
3. 同时开 Curves + HSL Curves + Soft Clip + Mixer 导出 LUT，对比预览。
4. 用一张 RGB ramp/checker 测试图观察红蓝通道是否反了。

## 最终目标状态

1. Curves/HSL Curves 的 control points 是唯一可编辑源，LUT 是可重建缓存。
2. UI 图层切换只同步显示，不触发业务 changed。
3. MainWindow 或统一 controller 独占调色状态写入与 render 调度。
4. 脚本 API、UI、Layer、Renderer 使用同一套参数 schema。
5. LUT Exporter 的数学和顺序与实时 shader 对齐。
6. 所有旧项目/旧脚本数据都有归一化入口，不在 UI 中崩溃。

