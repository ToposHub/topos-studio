# Color 调色工具验收报告与剩余优化计划

日期：2026-05-24

范围：

- 验收 `docs/color_tools_bug_audit_optimization_plan_2026-05-24.md`
- 验收 `docs/color_page_remaining_tools_bug_audit_optimization_plan_2026-05-24.md`
- 覆盖 Color 页面常用与其余调色工具：Curves、Soft Clip、HSL Curves、Mixer、Grading、RAW、Qualifier、Warper、Blur/Sharpen、Film Grain、LUT、Denoise/Deblur

## 1. 总体验收结论

这两份计划里的大多数 P0/P1 实现已经完成，特别是以下高风险项已经有代码落地：

1. HSL Curves 不再由面板和 MainWindow 双重应用。
2. Mixer 支持旧数据、部分参数和 dict/list 兼容。
3. Curves 与 HSL Curves 增加了 `set_lut()` fallback。
4. 脚本 API 的 Curves/HSL/Mixer 数据结构开始向实时渲染路径对齐。
5. LUT Exporter 已补 HSL Curves，并调整了 Soft Clip 与 Mixer 顺序。
6. Warper 补了 `numpy` import，网格密度变化会清理旧 warper 数据。
7. RAW、Grading、Qualifier、Blur/Sharpen、Film Grain 多数 `set_params()` 已加入同步保护。
8. Blur/Sharpen 的参数恢复、重置和 Blur/Sharpen 同时开启的覆盖问题已有修复。
9. Film Grain seed 不再每次普通参数变化都刷新。
10. LUT Gallery 开始使用统一路径工具，不再依赖 `os.getcwd()`。

本次验收中，我又直接修复了几个残留小问题：

1. `src/scripting/api.py`
   - 修复 HSL 三点以上曲线中 `res` 未初始化导致的运行时报错。
   - 为 Curves/HSL 的脚本插值增加 `scipy` 缺失时的 `numpy.interp` fallback，避免脚本 API 因可选依赖不可用而崩溃。

2. `src/ui/mixer_panel.py`
   - 增加 `_syncing_ui`，避免 `set_params()`、分组 reset、全量 reset 通过子控件信号误触发 `mixer_changed`。
   - 待发送参数改为浅拷贝，避免节流期间被后续 UI 变化改写。

3. `src/ui/soft_clip_panel.py`
   - 增加 `_syncing_ui`，修复 `set_params()` 在图层切换/项目恢复时仍会 emit 的问题。
   - `reset_all()` 同步设置 slider 和 spin，最后只发一次最终参数。

4. `src/ui/widgets/curves_widget.py`
   - 修复 `set_lut(..., emit_changed=True)` 没有设置 pending LUT 导致不会真正 emit 的问题。

5. `src/ui/warper_panel.py`
   - `set_params()` 同步 grid combo 时阻断 combo 信号，避免恢复旧 warper 数据时被 `on_grid_size_changed()` 清空。

6. `src/ui/widgets/color_wheel.py`
   - Grading 数值框手动输入小数现在会触发节流更新。
   - `reset_all()` 使用 `_syncing_ui` 阻断中间信号，只发一次最终状态。

7. `src/ui/raw_panel.py`
   - `set_enabled_for_file()` 阻断子控件信号，避免文件加载阶段被误判为用户 RAW 调参。

8. `src/ui/widgets/lut_gallery.py`、`src/ui/lut_panel.py`、`src/ui/main_window.py`
   - 导入同名 LUT 时自动生成后缀，避免覆盖本地同名 LUT。
   - 从 Gallery 移除当前 active LUT 时通知 MainWindow 清空 engine active LUT。

## 2. 本次验证结果

已通过：

```bash
python3 -m compileall -q src
```

结果：通过。

已通过脚本 API 插值验证：

```bash
python3 - <<'PY'
from src.scripting.api import _interpolate_curve_255, _interpolate_hsl_curve_255, _build_default_hsl_lut
rgb = _interpolate_curve_255([(0, 0), (128, 140), (255, 255)])
hsl = _interpolate_hsl_curve_255([(0, 127.5), (128, 100), (255, 127.5)], 'HueSat')
hue = _interpolate_hsl_curve_255([(0, 127.5), (128, 80), (255, 127.5)], 'HueHue')
print(rgb.shape, hsl.shape, hue.shape, _build_default_hsl_lut().shape)
PY
```

结果：通过，并覆盖 `scipy` 缺失 fallback。

已通过 UI 同步不误发信号的轻量验证：

```bash
QT_QPA_PLATFORM=offscreen python3 - <<'PY'
# 对 Mixer、SoftClip、RAW、Warper、Grading 的程序化 set/sync 做信号计数
# 期望：非 emit 模式下信号计数为 0
PY
```

结果：

```text
MixerPanel 0
SoftClipPanel 0
RawPanel 0
WarperPanel 0
GradingPanel 0
```

已通过：

```bash
python3 -m pytest tests/test_config_manager.py -q
```

结果：

```text
4 passed
```

未通过/被环境阻塞：

```bash
python3 -m pytest tests/test_config_manager.py tests/test_expression_parser.py -q
```

阻塞原因：

- `tests/test_expression_parser.py` 导入链需要 `moderngl`
- 当前 Python 环境缺少 `moderngl`

UI 原始实例化验证也曾被环境阻塞：

- 当前环境缺少 `qtawesome`
- 验收时通过临时 mock `qtawesome.icon()` 验证了本次信号同步逻辑

## 3. 剩余大问题

下面这些不建议在验收补丁中顺手改，因为它们会影响渲染架构、色彩数学或长任务线程策略。

### R1. Qualifier matte 仍有双路径语义风险

相关文件：

- `src/core/async_renderer.py`
- `src/core/shader_compiler.py`
- `src/core/image_engine.py`

现状：

1. AsyncRenderer 中存在 `_generate_matte()` 与 `_apply_matte_postprocess()`，会处理 Qualifier、mask、clean black、clean white、blur。
2. `shader_compiler.py` 的 shader 生成路径里仍然存在 `has_qualifier` 时的 `calculate_qualifier_hsl()` 与 `mask *= q_matte`。
3. 部分路径可能使用预生成 matte，部分路径可能在 composition shader 中再次计算 qualifier。

风险：

- 单层、图层栈、预览、全分辨率渲染、导出之间的 matte 行为可能不完全一致。
- Qualifier blur/clean 的 UI 参数可能在某些路径有效，在另一些路径弱化或重复。
- 后续修 Blur/Mask 时容易出现“看起来修了一处，另一条渲染路径又回退”的问题。

优化思路：

1. 明确唯一权威路径：
   - 方案 A：Qualifier 永远先生成 matte texture，composition shader 只采样 matte。
   - 方案 B：Qualifier 永远在 composition shader 中计算，postprocess 只处理外部 mask。
2. 建议采用方案 A：
   - Qualifier、mask shapes、AI mask、blur、clean 全部在 matte pass 中统一。
   - composition shader 不再拥有 qualifier 参数，只接收最终 mask。
3. 给 shader compiler 增加模式约束：
   - 如果 renderer 已提供 matte texture，则 `has_qualifier=False`。
   - 避免同一层同时传 qualifier uniform 和 matte texture。

验证方法：

1. 构造同一张图片、同一组 qualifier 参数。
2. 分别跑：
   - 单层预览
   - 多图层 composition
   - 全分辨率 render
   - LUT/export 或 still export
3. 对比 matte 边缘、invert、blur、clean_black、clean_white。
4. 对同一像素位置采样 matte 值，要求四条路径一致或差异有明确文档说明。

### R2. Qualifier HSL/HSV 语义仍需统一

相关文件：

- `src/core/shader_nodes.py`
- `src/ui/qualifier_panel.py`
- `src/core/shader_compiler.py`

现状：

- UI 文案是 HSL/Lum。
- shader 中 `rgb_to_hsl()` 的实现更接近 HSV/value 语义。
- `lum_low/lum_high` 实际控制的是 value、luma 还是 HSL lightness，需要统一。

风险：

- 用户用取色器选高亮/暗部时，UI 反馈和实际 matte 选择范围可能不一致。
- 后续如果把 CPU matte、GPU matte、LUT exporter 或脚本 API 对齐，会出现数学定义冲突。

优化思路：

1. 定义项目级 qualifier 色彩空间契约：
   - 推荐命名为 HSV Qualifier，UI 改成 Hue/Sat/Value。
   - 或者真正实现 HSL lightness，并同步 CPU/GPU/文案。
2. 如果保留当前 shader 算法：
   - 代码函数从 `rgb_to_hsl` 重命名为 `rgb_to_hsv_for_qualifier`。
   - UI 的 `Lum` 改为 `Value` 或在中文中标为“明度”。
3. 所有 picker、range bar、script API 使用同一命名。

验证方法：

1. 红、绿、蓝、灰阶、HDR 高亮、负值暗部测试图。
2. 固定 hue/sat/value 范围，对 GPU matte 输出做黄金图。
3. Picker 取样后生成参数，再回采同一像素，要求 matte 接近 1。

### R3. HDR 数学契约还没有完整落文档与测试

相关文件：

- `src/core/shader_nodes.py`
- `src/core/lut_exporter.py`
- `src/core/shader_compiler.py`

现状：

- Grading、Soft Clip、HSL Curves、Mixer 已经做了不少 HDR 保护。
- 但不同工具对负值、大于 1.0 的高光、饱和度/luma 的 clamp 策略并不完全统一。
- LUT Exporter 的 CPU 实现和实时 GLSL 已经靠近，但还没有黄金图验证。

风险：

- HDR 图片、Log 素材、ACES/宽色域素材在预览和导出之间可能存在细微偏差。
- Soft Clip、HSL Curves、Mixer 顺序或 clamp 改动后，很难肉眼定位是哪一段造成差异。

优化思路：

1. 为 Color pipeline 写一份数学契约：
   - 每个工具是否允许负值输入。
   - 每个工具是否允许输出超过 1.0。
   - 哪些工具只能在 display-referred 区间工作。
   - LUT export 对 HDR 的处理边界。
2. 给 GLSL 与 CPU exporter 建黄金样本：
   - SDR ramp
   - HDR ramp
   - 负值 ramp
   - hue wheel
   - saturation/value grid
3. 每次改 shader 或 exporter 后跑数值对比。

验证方法：

1. CPU exporter 与实时 shader 对同一输入的平均误差、最大误差报告。
2. 针对 Soft Clip/Mixer/HSL Curves 单独开关测试。
3. 组合顺序测试：Grading → Curves → HSL → Soft Clip → Mixer → LUT。

### R4. Denoise/Deblur 取消策略仍可能阻塞 UI

相关文件：

- `src/core/denoise_engine.py`
- `src/ui/denoise_panel.py`
- `src/ui/deblur_panel.py`

现状：

- `DenoiseEngine.cancel()` 仍使用 `terminate()` 后 `wait()`。
- 如果 ONNX 推理或大图分块正忙，UI 线程调用 cancel 可能被 wait 阻塞。

风险：

- 用户点击取消/关闭窗口时短时间卡住。
- 强制 terminate 可能留下模型 runtime 或数组状态不干净。

优化思路：

1. 把 worker 改成协作式取消：
   - `requestInterruption()`
   - worker 内部分块循环检查 `isInterruptionRequested()`
   - ONNX 单次推理无法中断时，至少在 tile 边界退出
2. `cancel()` 不在 UI 线程无限 wait：
   - 使用短 timeout
   - 通过 finished/error 信号恢复按钮状态
3. Deblur 与 Denoise 共用同一个取消协议。

验证方法：

1. 大图 AI denoise 开始后立刻取消。
2. 取消期间拖动 UI，确认不卡死。
3. 连续 Apply → Cancel → Apply，不出现残留 worker 或重复 signal。

### R5. 依赖与测试环境还没有锁定

相关文件：

- `requirements.txt`
- `pytest.ini`
- 本地 Python 环境

现状：

- 当前环境缺 `qtawesome`，UI 原始实例化测试无法直接跑。
- 当前环境缺 `moderngl`，部分 composite/render 测试无法收集。
- 之前还遇到过 `cv2`、`scipy` 缺失。

风险：

- 修复调色工具时只能靠静态检查和局部 mock，无法稳定跑完整回归。
- 新增依赖或可选依赖没有被 CI/本机一致验证。

优化思路：

1. 明确最小开发环境：
   - PyQt6
   - qtawesome
   - numpy
   - opencv-python
   - moderngl
   - scipy 是否必需，如果不是必需，所有调用点必须 fallback
2. 增加 headless UI smoke tests：
   - `QT_QPA_PLATFORM=offscreen`
   - 可 mock 图标，但不应 mock 核心 widget 行为
3. 增加 color tools 专项测试文件：
   - `tests/test_color_tools_params.py`
   - `tests/test_color_tools_scripting_api.py`
   - `tests/test_color_tools_lut_exporter.py`

验证方法：

1. 新建干净 venv，安装 `requirements.txt`。
2. 跑：
   - `python3 -m compileall -q src`
   - `python3 -m pytest tests/test_config_manager.py -q`
   - `python3 -m pytest tests/test_color_tools_params.py -q`
3. 记录不可 headless 的 GPU 测试，并分离为 manual/GPU 标签。

## 4. 建议实施顺序

### Phase 1：补测试环境与专项回归

目标：

- 先让后续验证稳定。

任务：

1. 清点 `requirements.txt` 与真实 import。
2. 把 `scipy` 这类可选依赖调用点全部加 fallback 或写入必需依赖。
3. 新增 color tools 参数同步测试：
   - `set_params(..., emit=False)` 不 emit。
   - `set_params(..., emit=True)` 只 emit 一次。
   - reset 只 emit 一次最终状态。
4. 新增脚本 API 测试：
   - Curves 0-255 坐标。
   - HSL 默认 LUT。
   - Mixer dict/list 兼容。

验收标准：

- 无需 GPU 的 color tools 测试可在 headless 环境稳定运行。
- 缺少 `scipy` 不影响脚本 API。

### Phase 2：统一 Qualifier matte 架构

目标：

- 消除 matte 双路径歧义。

任务：

1. 画出当前四条路径：
   - UI preview
   - AsyncRenderer layer stack
   - ImageEngine composition shader
   - export/full-res
2. 选择唯一 matte 生成权威路径。
3. 删除或禁用重复 qualifier shader 分支。
4. 对 blur/clean/invert 增加黄金图。

验收标准：

- 同一 qualifier 参数在预览、多图层、全分辨率输出中 matte 一致。
- `blur`、`clean_black`、`clean_white` 不重复应用。

### Phase 3：统一 Qualifier 色彩空间命名

目标：

- UI、shader、脚本和文档用同一套术语。

任务：

1. 决定 HSL 还是 HSV。
2. 重命名 shader helper 或改数学实现。
3. 同步 UI 文案、变量名、保存字段兼容层。
4. 添加取色器回采测试。

验收标准：

- 用户取样的像素在生成参数后可被稳定选中。
- Hue/Sat/Lum 或 Hue/Sat/Value 的文案与实际算法一致。

### Phase 4：HDR/Exporter 黄金图

目标：

- 保证实时预览和 LUT/export 数值一致。

任务：

1. 建 SDR/HDR/负值测试 ramp。
2. 建每个工具的单项黄金输出。
3. 建组合顺序黄金输出。
4. 记录允许误差。

验收标准：

- LUT Exporter 与实时 shader 在约定输入范围内误差可控。
- HDR clamp 策略有文档和测试覆盖。

### Phase 5：AI Denoise/Deblur 协作式取消

目标：

- 消除取消/关闭时 UI 阻塞风险。

任务：

1. Worker 支持 interruption flag。
2. Tile loop 检查取消。
3. UI cancel 不无限 wait。
4. Denoise/Deblur 共用取消状态恢复逻辑。

验收标准：

- 大图处理时取消不会明显卡 UI。
- 连续处理不会遗留 running worker。

## 5. 手动回归清单

每次完成 Phase 2 到 Phase 4 后，至少手动跑：

1. Curves
   - RGB 各通道单独调节。
   - 项目保存/恢复。
   - 脚本 API 设置三点曲线。

2. HSL Curves
   - 六条曲线分别调节。
   - HueHue 红色 wrap-around。
   - 项目保存/恢复。

3. Soft Clip
   - 负值暗部。
   - 大于 1.0 高光。
   - 预览和 LUT export 对比。

4. Mixer
   - red/magenta 边界。
   - dict/list 老项目恢复。
   - 脚本 API partial update。

5. Qualifier
   - picker normal/add/subtract。
   - blur/clean/invert。
   - 单层与多层一致性。

6. RAW
   - RAW 文件打开自动启用。
   - 非 RAW 文件不误触发 RAW update。
   - input LUT 路径从非项目 cwd 启动仍正确。

7. Blur/Sharpen
   - Sharpen 与 Blur 同时开启。
   - 四种 blur 模式保存/恢复。
   - reset all/reset blur。

8. Film Grain
   - 拖强度不刷新 seed。
   - 关闭再开启才生成新 seed。
   - 视频播放时参数变化 debounce。

9. LUT
   - 导入同名文件不覆盖。
   - 删除 active LUT 会清空预览。
   - Reset 后 UI selection 和 intensity 一致。

10. Denoise/Deblur
    - Apply、Cancel、Reset。
    - 大图处理时窗口仍响应。

## 6. 最终完成标准

1. 两份原计划中的 P0/P1 均有自动化或手动验收记录。
2. Qualifier matte 只剩一条权威路径。
3. Qualifier HSL/HSV 术语与算法一致。
4. Color pipeline HDR 数学契约写入项目文档。
5. LUT Exporter 与实时 shader 有黄金图或数值误差测试。
6. Denoise/Deblur 取消不会阻塞 UI。
7. headless color tools 测试可在干净环境稳定运行。
