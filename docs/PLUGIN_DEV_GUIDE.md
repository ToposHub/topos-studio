# 🚀 Topos 插件开发指南

欢迎来到 Topos Color 插件开发文档！

> **⚠️ 先分清两套插件体系（2026-09 补充）**
>
> Topos 仓库里存在**两套互不相通的插件系统**，开发前先确认你要扩展的是哪一个：
>
> | | 主程序插件系统 | file_explorer 插件系统 |
> |---|---|---|
> | 位置 | 仓库根 `plugins/` | `external/file_explorer/plugins/` |
> | 形态 | `.tglsl`（GLSL 着色器）/ `.tai`（ONNX 推理）单文件 | Python 包目录（`__init__.py` + `plugin.json` + 可选 `static/` 前端） |
> | 运行时 | 主程序渲染管线（ModernGL / ONNX Runtime） | file_explorer 的 Flask 服务（独立或嵌入模式） |
> | 注册方式 | 放入目录即被发现（`@name` 等元数据头） | `plugin_manager.init_app()` 扫描 + `setup(context)` 钩子，注册 Blueprint / JS / UI 组件 |
> | 前端资源 | 无 | `/static_plugin/<name>/<file>` 只服务 `plugins/<name>/static/`；JS 经 `context.register_js()` 注入 |
> | 启停配置 | 无（放入即启用） | FE 自己的 `config.json` 的 `disabled_plugins` 列表（宿主偏好页可读写） |
> | API 路径 | 无 HTTP API | `/api/plugin/<name>/...`（受 `_guard_api` token 保护） |
>
> 本指南第一、二部分讲的是**主程序插件系统**。file_explorer 插件的
> 最佳参考是 `external/file_explorer/plugins/` 下的现有实现
> （如 `ai_library_browser`、`format_factory`）。

Topos 支持两种类型的插件：

1. **DCTL (.tglsl) 着色器插件** — 基于 OpenGL 着色语言 (GLSL) 的实时特效引擎，通过编写 GLSL 代码注入 GPU 渲染流水线，实现 60FPS 实时反馈和 32-bit 浮点无损精度。
2. **AI (.tai) 智能插件** — 基于 ONNX Runtime 的神经网络推理引擎，支持超分辨率、降噪、增强、风格化等 AI 模型效果，自动利用 GPU 加速（CUDA / CoreML / DirectML）。

---

# 第一部分：DCTL (.tglsl) 着色器插件

## 📂 1. 基础架构：什么是 .tglsl 文件？

插件是一个纯文本文件，后缀名为 `.tglsl`。
只需将写好的文件放入 Topos Color 根目录的 `plugins/` 文件夹中，重启软件，它就会自动出现在顶部菜单栏的 `Plugins` 列表中，并自动生成 UI 控制面板。

一个标准的 `.tglsl` 文件由两部分组成：

1. **元数据头部 (Metadata Header)**：用于定义插件名称、作者以及在界面上自动生成的滑杆/按钮。
2. **着色器逻辑 (Shader Code)**：GLSL 代码，用于告诉 GPU 如何计算每一个像素的颜色。

### 💡 极简示例代码

```glsl
// --- Topos Plugin Metadata ---
// @name 简单曝光调整
// @author Topos Team
// @param slider exposure "曝光度" 0.0 -5.0 5.0
// -----------------------------

vec3 process(vec3 color, vec2 uv) {
    // 将滑杆的数值转化为真实的亮度乘数
    float multiplier = pow(2.0, exposure);
    return color * multiplier;
}

```

---

## 🎛️ 2. 界面元数据定义 (UI Parameters)

Topos 引擎会读取文件开头的注释块（必须包含在 `// ---` 之间），并自动为你生成原生 UI 面板。你不需要写任何 Python 或 PyQt 代码！

**语法规则：** `// @param [类型] [变量名] "[界面显示标签]" [默认值] [最小值] [最大值]`

支持以下四种 UI 控件：

### ① 滑杆 (Slider) - 最常用

用于连续的浮点数值调整。

* **语法**: `// @param slider <var_name> "<Label>" <default> <min> <max>`
* **示例**: `// @param slider blur_radius "模糊半径" 2.5 0.0 5.0`
* **在代码中的类型**: `float blur_radius;`

### ② 复选框 (Checkbox)

用于功能的开启/关闭。

* **语法**: `// @param checkbox <var_name> "<Label>" <default>` (default 只能是 0 或 1)
* **示例**: `// @param checkbox enable_tint "开启红色染色" 1`
* **在代码中的类型**: `int enable_tint;` (GLSL中用 int 代替 bool)

### ③ 颜色拾取器 (Color)

用于选择 RGB 颜色。

* **语法**: `// @param color <var_name> "<Label>" <default_R> <default_G> <default_B>` (0.0 到 1.0)
* **示例**: `// @param color tint_color "高光染色" 1.0 0.9 0.8`
* **在代码中的类型**: `vec3 tint_color;`
* **UI**: 色块 + 屏幕吸管 + 统一 Color Inspector（P1-COLOR-14 起不再是占位文本）。数值为 scene-linear RGB [0,1]，直接传入 vec3 uniform；确认拾色后以单一 undo 提交。

### ④ 下拉菜单 (Select)

用于提供几个固定的选项。

* **语法**: `// @param select <var_name> "<Label>" <default_index> "<opt1>" "<opt2>" ...`
* **示例**: `// @param select blend_mode "混合模式" 0 "正常" "正片叠底" "滤色"`
* **在代码中的类型**: `int blend_mode;` (返回你选中的索引，如 0, 1, 2)

### Capability Manifest（能力声明，P1-COLOR-15）

除参数外，头部注释块还可声明插件的能力契约。声明会显示在 Effect Controls
插件分组的 tooltip 上，供调色员在启用前了解兼容性：

```glsl
// @description 高光染色，模拟胶片 halation
// @input_space scene-linear
// @output_space scene-linear
// @alpha premultiplied
// @precision float16
// @determinism deterministic
// @export_support none
// @threading gpu
```

| 指令 | 含义 | 常用值 |
|---|---|---|
| `@description` | 单行描述 | 自由文本 |
| `@input_space` / `@output_space` | 期望的输入/输出色彩空间 | `scene-linear` / `srgb` / `display` |
| `@alpha` | Alpha 语义 | `premultiplied` / `straight` |
| `@precision` | 最低精度要求 | `uint8` / `float16` / `float32` |
| `@determinism` | 是否确定性输出 | `deterministic` / `seeded` / `nondeterministic` |
| `@export_support` | 导出管线支持 | `all` / `video` / `lut` / `opengl` / `none` |
| `@threading` | 线程/设备模型 | `gpu` / `cpu` |
| `@graphics_backends` | 可运行的图形后端（逗号分隔，Phase 7） | `opengl` / `metal` / `d3d11` / `d3d12` |
| `@compute_backends` | 可运行的计算后端（逗号分隔，Phase 7） | `glsl_fragment` / `glsl_compute` / `metal_compute` / `cuda` / `directml` |

所有声明均为可选；未声明时按引擎默认假设处理。

`@export_support opengl`（PLUG-002 文档化）：插件效果依赖 GLSL fragment
管线本身（无等价的 CPU/LUT 数学），导出仅经 GPU（OpenGL composition）路径
渲染——warm_tint 为已验证先例，Three_Way_Tint / Day_Night_Converter 沿用。
交付矩阵（delivery_matrix）会按各层插件的 `export_support` 声明与所选导出
管线的现实差异给出行级提示。`@version`、`@preview_cost`
（播放降级）与 `content_hash`（身份指纹）在更早版本已支持。

**后端声明的缺省语义（Phase 7）**：未写 `@graphics_backends` 的插件按
`opengl` 处理，未写 `@compute_backends` 的按 `glsl_fragment` 处理——与
历史行为一致。需要跑在 Metal/D3D 渲染器上的插件必须显式声明；声明了
不支持的值（如 `@graphics_backends vulkan`）会在解析时得到明确报错，
不会被静默忽略。声明了 `glsl_compute` 的效果在 fragment 自动回退通道
（GL 4.1 大半径效果）中仍然可用。

---

## 💻 3. GLSL 开发环境 (Built-in Variables)

你的所有图形学算法都必须写在一个名为 `process` 的函数中。

```glsl
vec3 process(vec3 color, vec2 uv) {
    // 你的代码
    return color;
}

```

### 📥 传入参数：

* `vec3 color`: 当前像素的 RGB 颜色值。由于采用线性物理工作流，这个值**可能小于 0.0，也可能远远大于 1.0**（HDR高光）。请尽量避免使用 `clamp(color, 0.0, 1.0)` 暴力裁切它，以免丢失高光细节。
* `vec2 uv`: 当前像素的屏幕坐标。左下角为 `(0.0, 0.0)`，右上角为 `(1.0, 1.0)`。常用于制作暗角、遮罩等空间类效果。

### 🌍 全局环境变量 (Uniforms)：

引擎为你自动注入了以下全局变量，你可以直接在代码中使用它们：

* `sampler2D u_input_tex`: 原始画面的纹理句柄。可以使用 `texture(u_input_tex, uv).rgb` 采样其他位置的像素（用于模糊、锐化、色差分离等空间特效）。
* `vec2 u_tex_size`: 当前画面的物理分辨率（例如 `vec2(1920.0, 1080.0)`）。
* `float u_aspect_ratio`: 画面的宽高比（Width / Height）。在绘制正圆形时非常关键，用于抵消 UV 坐标的拉伸。

---

## 🛠️ 4. 变量隔离机制 (Namespacing)

**作为开发者，你完全不需要担心变量名冲突。**

即使同一种插件被叠加了 10 层，Topos 引擎的 `ShaderPipeline` 也会在底层自动把你的变量名重命名为 `L1_P0_blur_radius` 等私有名称。你只需要专注你的逻辑，直接使用你在 `@param` 中定义的变量名（如 `blur_radius`）即可。

---

## 📚 5. 实战案例学习

### 案例 A：电影感暗角 (Cinematic Vignette)

这个案例展示了如何利用 `uv` 坐标和 `u_aspect_ratio` 来制作空间效果。

```glsl
// --- Topos Plugin Metadata ---
// @name 电影感暗角
// @author Topos Team
// @param slider amount "暗角强度" 0.5 0.0 1.0
// @param slider radius "暗角范围" 0.7 0.1 2.0
// @param color vig_color "暗角颜色" 0.0 0.0 0.0
// -----------------------------

vec3 process(vec3 color, vec2 uv) {
    // 1. 将 UV 坐标系移到画面中心 (-0.5 到 0.5)
    vec2 center_uv = uv - 0.5;

    // 2. 修正宽高比，确保暗角是正圆形而不是椭圆
    center_uv.x *= u_aspect_ratio;

    // 3. 计算当前像素到中心的距离
    float dist = length(center_uv);

    // 4. 使用 smoothstep 创建平滑的过渡渐变
    float vignette = smoothstep(radius, radius - 0.4, dist);

    // 5. 根据强度混合原图与暗角颜色
    float mix_factor = (1.0 - vignette) * amount;
    return mix(color, vig_color, mix_factor);
}

```

### 案例 B：色差分离 (Chromatic Aberration)

这个案例展示了如何使用 `u_input_tex` 和 `u_tex_size` 进行相邻像素采样。

```glsl
// --- Topos Plugin Metadata ---
// @name 镜头色差分离
// @author Topos Team
// @param slider shift_amt "偏移强度" 2.0 0.0 10.0
// @param checkbox radial "中心放射模式" 1
// -----------------------------

vec3 process(vec3 color, vec2 uv) {
    if (shift_amt <= 0.0) return color;

    // 计算单个像素的物理尺寸
    vec2 pixel_size = 1.0 / u_tex_size;

    vec2 shift_dir;
    if (radial == 1) {
        // 放射模式：边缘偏移大，中心偏移小
        shift_dir = uv - 0.5;
    } else {
        // 线性模式：固定向右偏移
        shift_dir = vec2(1.0, 0.0);
    }

    // 计算红绿蓝三个通道的不同 UV 坐标
    vec2 uv_r = uv + shift_dir * shift_amt * pixel_size;
    vec2 uv_g = uv; // 绿通道不动
    vec2 uv_b = uv - shift_dir * shift_amt * pixel_size;

    // 分别采样
    float r = texture(u_input_tex, uv_r).r;
    float g = color.g; // 直接用当前像素的G
    float b = texture(u_input_tex, uv_b).b;

    return vec3(r, g, b);
}

```

---

# 第二部分：AI (.tai) 智能插件

## 🧠 6. 什么是 .tai 插件？

`.tai`（Topos AI）插件是一种基于 ONNX 神经网络模型的特效插件。与 DCTL 插件在 GPU 着色器中实时计算不同，AI 插件通过 ONNX Runtime 运行预训练的神经网络模型，适合处理超分辨率、降噪、去模糊、图像增强等复杂任务。

### 与 DCTL 插件的核心区别

| 特性 | DCTL (.tglsl) | AI (.tai) |
|------|---------------|-----------|
| 计算方式 | GPU 着色器实时计算 | ONNX 神经网络推理 |
| 响应速度 | 实时（60FPS） | 异步处理（需点击 Process） |
| 精度 | 32-bit 浮点 | 取决于模型（通常 float32） |
| 适用场景 | 颜色变换、空间特效 | 超分辨率、降噪、AI 增强 |
| 开发语言 | GLSL | 无需写代码，只需配置元数据 |
| 分块处理 | 不需要 | 自动分块 + 余弦窗融合 |

### 工作流程

1. 将 `.tai` 文件和对应的 `.onnx` 模型文件放入指定位置
2. 重启软件后，插件出现在 `Add Effect > AI Effects` 菜单中
3. 添加到图层后，点击 **Process** 按钮触发异步推理
4. 推理完成后结果自动上传到 GPU，替换原始图像显示

---

## 📂 7. .tai 文件格式

`.tai` 文件是一个纯文本元数据文件，**不需要编写任何算法代码**，只需描述插件信息和模型参数。

### 文件结构

```
// --- Topos AI Plugin Metadata ---
// @name <插件显示名称>
// @i18n_name <国际化键名>          （可选）
// @author <作者名>
// @version <版本号>                 （可选）
// @category <分类>                  （可选）
// @ai_model <模型文件路径>
// @ai_tile_size <分块大小>          （可选，默认 512）
// @ai_overlap <重叠像素数>          （可选，默认 32）
// @ai_scale <缩放倍数>              （可选，默认 1.0）
// @ai_input_format <输入色彩格式>   （可选，默认 RGB）
// @ai_output_format <输出色彩格式>  （可选，默认 RGB）
// @param <参数定义>                  （可选，同 DCTL 语法）
// -----------------------------
```

### 完整示例：4x 超分辨率插件

```
// --- Topos AI Plugin Metadata ---
// @name AI Upscale 4X
// @i18n_name AI_Upscale_4X
// @author Topos Team
// @version 1.0
// @category upscale
// @ai_model models/upscale/4x_ultrasharp.onnx
// @ai_tile_size 512
// @ai_overlap 32
// @ai_scale 4
// @ai_input_format RGB
// @ai_output_format RGB
// @param slider blend "Blend" 1.0 0.0 1.0
// -----------------------------
```

---

## 🏷️ 8. AI 插件元数据指令详解

### 基础信息

| 指令 | 必需 | 说明 |
|------|------|------|
| `@name` | 是 | 插件在 UI 中显示的名称 |
| `@i18n_name` | 否 | 国际化翻译键名，用于多语言支持 |
| `@author` | 否 | 作者名称，默认 "Unknown" |
| `@version` | 否 | 插件版本号 |
| `@category` | 否 | 插件分类标签，显示在 UI 中 |

### AI 模型配置

#### `@ai_model` (必需)

ONNX 模型文件的路径。路径相对于 Topos Color 项目根目录。

```
// @ai_model models/upscale/4x_ultrasharp.onnx
// @ai_model models/denoise/best_denoise.onnx
```

**推荐的目录结构：**
```
Topos Color/
├── plugins/
│   ├── AI_Upscale_4X.tai          ← AI 插件元数据
│   ├── AI_Denoise.tai
│   └── MyShader.tglsl             ← DCTL 着色器插件
├── models/
│   ├── upscale/
│   │   └── 4x_ultrasharp.onnx     ← ONNX 模型文件
│   ├── denoise/
│   │   └── best_denoise.onnx
│   └── enhance/
│       └── enhance_v2.onnx
```

#### `@ai_tile_size` (可选，默认 512)

分块推理时每个 tile 的像素尺寸。影响显存占用和处理速度。

- **值越大**：显存占用越高，边缘接缝越少，速度可能更快（减少重叠区域占比）
- **值越小**：显存占用越低，适合低端 GPU，但边缘接缝风险增加
- **推荐值**：`256` ~ `1024`，取决于模型设计

#### `@ai_overlap` (可选，默认 32)

相邻 tile 之间的重叠像素数。引擎使用**余弦窗融合 (Cosine-Window Blending)** 在重叠区域进行平滑过渡，消除 tile 边界处的接缝。

- **值越大**：融合过渡更平滑，但计算量增加（重叠区域被重复处理）
- **值越小**：计算效率高，但可能出现 tile 边界痕迹
- **推荐值**：`16` ~ `64`，通常 `32` 是良好的平衡点

#### `@ai_scale` (可选，默认 1.0)

输出图像相对于输入的缩放倍数。

- `1.0` — 输入输出尺寸相同（降噪、增强类模型）
- `2.0` — 2x 超分辨率
- `4.0` — 4x 超分辨率

**重要**：此值必须与模型的实际输出尺寸匹配。例如 4x 超分模型输入 `512x512`，输出必须是 `2048x2048`。

#### `@ai_input_format` (可选，默认 RGB)

模型期望的输入色彩格式。

- `RGB` — 标准 RGB 顺序（推荐，Topos 内部使用 RGB）
- `BGR` — OpenCV 风格的 BGR 顺序（某些 PyTorch 导出的模型可能需要）

引擎会在送入模型前自动进行色彩空间转换。

#### `@ai_output_format` (可选，默认 RGB)

模型输出的色彩格式。

- `RGB` — 模型输出 RGB 顺序
- `BGR` — 模型输出 BGR 顺序，引擎会自动转回 RGB

### 用户参数

与 DCTL 插件完全相同的语法，用于在 UI 面板中生成用户可调的参数。

```
// @param slider blend "混合强度" 1.0 0.0 1.0
// @param slider strength "降噪强度" 0.8 0.0 1.0
// @param checkbox enable_detail "保留细节" 1
```

---

## 🔧 9. ONNX 模型要求

为了让 AI 插件正常工作，你的 ONNX 模型需要满足以下要求：

### 输入格式

- **输入数量**：1 个
- **输入形状**：`[1, 3, H, W]`（NCHW 格式）
  - `1` — batch size
  - `3` — RGB 或 BGR 三个通道
  - `H` — 图像高度（等于 `@ai_tile_size` 或其倍数）
  - `W` — 图像宽度（等于 `@ai_tile_size` 或其倍数）
- **数据类型**：`float32`
- **值范围**：`[0.0, 1.0]`（归一化后的浮点数）

### 输出格式

- **输出数量**：1 个
- **输出形状**：
  - 对于 `@ai_scale 1.0`：`[1, 3, H, W]`（与输入相同）
  - 对于 `@ai_scale > 1.0`：`[1, 3, H*scale, W*scale]`
- **数据类型**：`float32`
- **值范围**：`[0.0, 1.0]`（引擎会自动 clip 到合法范围）

### PyTorch 导出示例

```python
import torch

class MyModel(torch.nn.Module):
    def forward(self, x):
        # x: [1, 3, H, W], float32, range [0, 1]
        # ... 你的网络结构 ...
        return y  # [1, 3, H, W] or [1, 3, H*scale, W*scale]

model = MyModel()
model.eval()

dummy_input = torch.randn(1, 3, 512, 512)
torch.onnx.export(
    model,
    dummy_input,
    "models/upscale/my_model.onnx",
    opset_version=17,
    input_names=["input"],
    output_names=["output"],
    dynamic_axes={
        "input": {2: "height", 3: "width"},
        "output": {2: "height", 3: "width"}
    }
)
```

**注意**：建议使用 `dynamic_axes` 让模型支持任意尺寸输入，这样引擎才能灵活地进行分块推理。

---

## 🧩 10. 分块推理架构

对于大尺寸图像，引擎会将图像分成多个 tile 逐块处理，避免一次性占用过多显存。

### 工作原理

```
┌───────────────────────────────────┐
│            原始图像                 │
│  ┌───────┐  ┌───────┐  ┌───────┐ │
│  │ Tile 1│  │ Tile 2│  │ Tile 3│ │
│  │       │  │       │  │       │ │
│  └───────┘  └───────┘  └───────┘ │
│  ┌───────┐  ┌───────┐  ┌───────┐ │
│  │ Tile 4│  │ Tile 5│  │ Tile 6│ │
│  │       │  │       │  │       │ │
│  └───────┘  └───────┘  └───────┘ │
└───────────────────────────────────┘

  ←overlap→←overlap→
  █░░░░░░░░██░░░░░░░██░░░░░░░░█
  █ = 余弦窗全权重区域
  ░ = 余弦窗渐变过渡区域（overlap）
```

### 余弦窗融合

在 tile 重叠区域，引擎使用余弦函数创建平滑的权重渐变：

```
权重 = 0.5 * (1 - cos(π * t))
```

其中 `t` 从 0 到 1 在重叠区域内变化。这确保了：
- tile 中心区域权重为 1.0（完全使用该 tile 的输出）
- tile 边缘区域权重逐渐减小（与相邻 tile 混合）
- 最终所有 tile 的权重之和为 1.0（无亮度偏移）

### 参数调优建议

| 场景 | tile_size | overlap | 说明 |
|------|-----------|---------|------|
| 轻量模型 (< 50MB) | 512 | 32 | 均衡配置 |
| 重量模型 (> 200MB) | 256 | 16 | 减少显存峰值 |
| 高质量超分 | 512 | 48 | 更大 overlap 确保无缝 |
| 快速降噪 | 512 | 16 | 减少重复计算 |

---

## ⚡ 11. GPU 加速支持

引擎自动检测并使用最佳可用的 GPU 加速方案：

| 平台 | 首选 Provider | 回退 |
|------|--------------|------|
| Windows + NVIDIA | CUDAExecutionProvider | CPUExecutionProvider |
| macOS | CoreMLExecutionProvider | CPUExecutionProvider |
| Windows + AMD/Intel | DmlExecutionProvider (DirectML) | CPUExecutionProvider |
| Linux + NVIDIA | CUDAExecutionProvider | CPUExecutionProvider |

引擎内置 60 秒 GPU 加载超时保护，避免 GPU 驱动问题导致程序卡死。

---

## 📋 12. AI 插件完整示例

### 示例 A：AI 降噪插件

```
// --- Topos AI Plugin Metadata ---
// @name AI Denoise
// @i18n_name AI_Denoise
// @author Topos Team
// @version 1.0
// @category denoise
// @ai_model models/denoise/drdn_denoise.onnx
// @ai_tile_size 512
// @ai_overlap 32
// @ai_scale 1.0
// @ai_input_format RGB
// @ai_output_format RGB
// @param slider blend "降噪强度" 0.85 0.0 1.0
// -----------------------------
```

**说明**：
- `@ai_scale 1.0` — 降噪不改变分辨率
- `blend` 参数控制降噪结果与原图的混合比例，1.0 为完全使用降噪结果，0.0 为无效果

### 示例 B：2x 超分辨率插件

```
// --- Topos AI Plugin Metadata ---
// @name AI Upscale 2X
// @author My Team
// @version 2.0
// @category upscale
// @ai_model models/upscale/2x_esrgan.onnx
// @ai_tile_size 512
// @ai_overlap 32
// @ai_scale 2
// @ai_input_format RGB
// @ai_output_format RGB
// @param slider blend "混合强度" 1.0 0.0 1.0
// -----------------------------
```

### 示例 C：AI 图像增强插件

```
// --- Topos AI Plugin Metadata ---
// @name AI Enhance
// @author Topos Team
// @version 1.0
// @category enhance
// @ai_model models/enhance/swin_enhance.onnx
// @ai_tile_size 256
// @ai_overlap 24
// @ai_scale 1.0
// @ai_input_format RGB
// @ai_output_format RGB
// @param slider blend "增强强度" 0.7 0.0 1.0
// @param checkbox sharpen "锐化增强" 1
// -----------------------------
```

---

## 🔍 13. 调试与排错

### 常见问题

**Q: 插件没有出现在 AI Effects 菜单中**

- 检查 `.tai` 文件是否放在 `plugins/` 目录下
- 检查 `.tai` 文件编码是否为 UTF-8
- 检查是否包含必需的 `@ai_model` 指令
- 查看控制台日志中是否有 `[PluginManager]` 开头的错误信息

**Q: 点击 Process 后报错 "AI model not found"**

- 检查 `@ai_model` 路径是否正确，路径相对于 Topos Color 根目录
- 确认 `.onnx` 文件确实存在且未损坏
- 路径中避免使用中文或特殊字符

**Q: 推理结果出现 tile 边界接缝**

- 增大 `@ai_overlap` 值（推荐 32 ~ 64）
- 确保 `@ai_tile_size` 是模型期望的输入尺寸

**Q: 处理速度太慢**

- 确认 GPU 加速是否生效（查看控制台是否有 `[AIPlugin] GPU:` 日志）
- 减小 `@ai_tile_size`（降低显存峰值，但可能增加 tile 数量）
- 减小 `@ai_overlap`（减少重复计算区域）
- 使用更小的模型文件

**Q: 处理后图像色彩异常**

- 检查 `@ai_input_format` 和 `@ai_output_format` 是否与模型匹配
- PyTorch 导出的模型通常使用 RGB 格式
- TensorFlow 导出的模型可能使用 BGR 格式

---

## ⚠️ 14. 开发者避坑指南

### DCTL 插件注意事项

1. **避免死循环与动态边界**：在 GLSL 中，`for` 循环的边界通常必须是常量（Constant）。如果你需要做一个半径可调的模糊，请参考 *Beauty Retouch* 插件，使用一个常量最大值（如 `const int MAX_R = 6;`），然后在循环内部使用 `if` 来跳出。
2. **浮点数必须带小数点**：GLSL 是强类型语言，`1` 是整数，`1.0` 是浮点数。如果你写 `color * 2` 会直接报错导致插件加载失败，必须写成 `color * 2.0`。
3. **性能优化**：`process` 函数会被画面的每一个像素执行（一张 4K 图片会执行 800 万次）。请尽量少用昂贵的数学运算（如大量的 `sin`, `cos`, `pow`），多利用内置的高效混合函数。

### AI 插件注意事项

1. **模型值范围**：引擎传入和期望的值范围都是 `[0.0, 1.0]`。如果你的模型期望 `[-1.0, 1.0]` 或 `[0, 255]`，需要在模型内部进行归一化，或使用前处理/后处理节点包装 ONNX 模型。
2. **模型输入形状**：必须支持 `NCHW` 格式（Batch, Channels, Height, Width），且支持动态的 H 和 W 维度以配合分块推理。
3. **blend 参数**：建议始终包含一个 `blend` 滑杆参数（默认 1.0），让用户可以控制 AI 效果的强度。引擎会自动在推理完成后将结果与原图混合。
4. **显存管理**：ONNX Runtime 的模型会话有缓存机制，同一个模型只会加载一次。但如果模型文件很大（> 1GB），首次加载可能需要几秒钟。

---

祝开发愉快！期待看到你的创意在 Topos Color 中大放异彩！

# 附录：批产插件案例（2026-09-28 批次）

以下三个插件来自 2026-09-28 批量开发（共 12 个），覆盖三类典型手法，可作为新插件参考：

## 案例 C：频率分离肤色平滑（Skin_Even_Matte）

见 `plugins/Skin_Even_Matte.tglsl`。要点：BT.601 YCbCr 软边肤色窗口检测 → 固定 17 tap 错相环形低通（`const int` 上限、零三角函数）→ `(original - low) × texture_keep` 回加纹理。与 `bilateral_skin_smooth.tglsl` 的差异：本插件是"检测 + 频率分离"，非肤色严格直通；bilateral 是全图边缘保持。参数命名空间 `u_P0_*` 隔离。

## 案例 D：编译期常量分档循环（Radial_Blur_Pro）

见 `plugins/Radial_Blur.tglsl` 同名实现 `plugins/Radial_Blur_Pro.tglsl`。要点：select 参数（采样数 8/16/24/32）经宏 `RADIAL_ACCUMULATE(N)` 展开为四个编译期常量循环分支——满足"循环上限编译期常量"红线的同时保留用户档位选择。`@preview_cost medium`，卡片注明性能预算。

## 案例 E：HDR 安全的色调风格（Cross_Process）

见 `plugins/Cross_Process.tglsl`。要点：通道曲线用有理函数 `f(x)=x(a+m·x²)/(1+v·x²)`——可证全实轴单调、无极点、渐近线性，因此 HDR 超界输入单调外推而无需 clamp；强度插值回原值。这是"风格曲线不 clamp"的通用范式。

> 本批全部 12 个插件的规格（含验证输出）在 `docs/plugins/2026-09-28/specs/`，效果卡片在 `docs/plugins/2026-09-28/cards/`，解析测试在 `tests/plugins/`。
