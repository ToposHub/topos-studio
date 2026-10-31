# .tglsl 插件开发风格基准笔记（STYLE_NOTES）

> 用途：计划03（GLSL 插件库扩充）12 个插件开发子智能体的统一规范输入。
> 依据：`docs/PLUGIN_DEV_GUIDE.md`（658 行）、`plugins/` 下全部 5 个现有 `.tglsl`
> （warm_tint、Film_Halation、HighPass_Retouch、bilateral_skin_smooth、
> Color_Space_Transform）、`src/features/color/core/plugin_manager.py` 解析实现、
> `src/shared/i18n/locales/*.json` 现状。日期：2026-09-28。
> 本文所有行号/键名均以当前仓库实态为准；与指南文档不一致处以"实测"标注。

---

## 0. 文件骨架（标准模板）

```glsl
// --- Topos Plugin Metadata ---
// @name <UI 显示名>
// @i18n_name <locale 键（见 §5）>
// @author Topos Team
// @version <如 1.0>
// @description <单行英文描述，进 tooltip>
// <capability manifest 指令（可选，见 §4）>
// @param ...（见 §2）
// -----------------------------

// 着色器代码：辅助函数 + vec3 process(vec3 color, vec2 uv)
```

规则：

- **开头 `// --- Topos Plugin Metadata ---`、结尾 `// -----------------------------` 两行包裹行必须写**。
  解析器按第一条 `//\s*-{5,}` 行切分元数据与着色器代码（`plugin_manager.py` 的
  `_extract_shader_code`），元数据必须全部位于该行之前。
  4/5 现有插件遵守此格式；`warm_tint.tglsl` 省略了包裹行，靠"最后一个 @param
  之后跳过注释行"的降级路径才取到代码——**不要模仿 warm_tint 的头部**。
- 元数据以 `//` 注释行形式书写，每条指令一行；正则按 `//\s*@<key>\s+(.+)` 匹配，
  写在着色器代码注释里也会被扫到，因此元数据指令只出现在头部区域。
- 文件 UTF-8 编码；文件名即 `plugin_id`（去扩展名），建议 `snake_case.tglsl`。

---

## 1. 文件头元数据指令清单（以现有插件实测为准）

| 指令 | 必填 | 解析行为（plugin_manager.py） | 现有插件中的实际使用 |
|---|---|---|---|
| `@name` | 是 | `//\s*@name\s+(.+)` 取首个匹配；缺失时回退为 plugin_id | 5/5 全部使用 |
| `@i18n_name` | 否（**强烈建议**） | 命中后名称改查 `plugins.<该值>.name`，查不到回落 `@name` 原文 | 5/5 全部使用 |
| `@author` | 否 | 缺省 `"Unknown"` | 5/5 全部使用（均为 `Topos Team`） |
| `@version` | 否 | 自由文本，进 `content_hash` 身份元数据与 tooltip（`Version x.x`） | 4/5（Film_Halation 未写） |
| `@description` | 否 | 单行描述，作为 capability tooltip 首行 | warm_tint、HighPass_Retouch、Color_Space_Transform |
| `@param` | 按需 | 见 §2 | 5/5 |
| `@param separator` | 否 | **解析 no-op**：不匹配参数正则（缺 var/label/values），被静默忽略，UI 也不会据此生成分隔线。现有 3 个插件（warm_tint、HighPass_Retouch、bilateral_skin_smooth）用它做视觉分组标记，可写可不写 | 3/5 |
| `@preview_cost` | 否 | `high / medium / low / skip` 四档，注册到效果预览质量管理器（播放降级）；5 个现有插件均未使用，重型新插件应评估声明 | 0/5 |
| capability 系列（§4） | 否 | `@input_space` 等 10 键 | 仅 warm_tint 用了 `@export_support opengl` |

最小可用头部 = `@name` + `@i18n_name` + `@author` + `@param`（Film_Halation 即此档）。
新插件标准头部 = 最小档 + `@version` + `@description` + 按需 capability 声明。

---

## 2. `@param` 四种控件

通用语法：`// @param <type> <var_name> "<Label>" <values...>`
正则：`//\s*@param\s+(\w+)\s+(\w+)\s+"([^"]+)"\s+(.+)` —— var_name 必须是
`\w+`，Label 必须带双引号，否则整行被静默丢弃。

### ① slider（滑杆，最常用）

- 语法：`// @param slider <var> "<Label>" <default> <min> <max>`
- 实例：`// @param slider strength "Strength" 0.35 0.0 1.0`
- GLSL 侧类型：`float`（uniform 自动注入，直接裸名引用）
- 约定：default/min/max 三个浮点全写；只写 1 个值时解析器补 `min=0.0, max=1.0`
  （不要依赖该兜底）。强度类参数惯例范围 `0.0–1.0`；曝光类对称范围如
  `-3.0 3.0`；默认值应让插件"刚添加即有可见但不过火"的效果（现有强度默认
  0.35–0.8）。

### ② checkbox（复选框）

- 语法：`// @param checkbox <var> "<Label>" <default>`，default 只能 `0` 或 `1`
- 实例：`// @param checkbox enable_tint "Enable Red Tint" 1`
- GLSL 侧类型：`int`（引擎以 int 代替 bool）。判断写法两种在野：
  `if (radial == 1)`（指南案例）与 `if (enable_adaptation > 0.5)`（Color_Space_Transform），
  均有效；新代码统一用 `== 1` / `== 0`。

### ③ color（颜色拾取器）

- 语法：`// @param color <var> "<Label>" <default_R> <default_G> <default_B>`，三分量 `0.0–1.0`
- 实例（指南案例）：`// @param color vig_color "暗角颜色" 0.0 0.0 0.0`
- GLSL 侧类型：`vec3`（scene-linear RGB，单一 undo 提交，带吸管 + Color Inspector）
- 现有 5 插件均未使用 color 控件；缺分量时解析器兜底 `[1.0, 1.0, 1.0]`，不要依赖。

### ④ select（下拉菜单）

- 语法：`// @param select <var> "<Label>" <default_index> "<opt1>" "<opt2>" ...`
- 实例（Color_Space_Transform.tglsl 第 12 行）：
  `// @param select tone_mapping "Tone Mapping" 0 "None" "Reinhard" "Filmic" "ACES"`
- GLSL 侧类型：`int`，值为选中项的**从 0 开始的索引**
- 选项表写法：每个选项单独一对双引号、空格分隔，写在同一行（现有多达 18 项的
  单行先例）；解析正则 `([\d.]+)\s+(.+)` + `re.findall(r'"([^"]+)"', ...)`，
  选项内不能含双引号
- 分发惯例：着色器内用早返回 if 链按索引分发（见 Color_Space_Transform
  `decode_input_gamma` / `apply_tone_mapping`：`if (gamma == 0) return c; ...`，
  末尾兜底 `return c;`），不用 switch 也可，保持每个分支单函数调用。

---

## 3. 内置 uniforms / 变量与 namespacing（PLUGIN_DEV_GUIDE 第 3–4 节 + 实测）

### process 契约（唯一入口）

```glsl
vec3 process(vec3 color, vec2 uv)
```

- `vec3 color`：当前像素已含上游调色的 RGB，scene-linear，**可 < 0.0 也可 >> 1.0（HDR）**。
- `vec2 uv`：左下 `(0.0, 0.0)` 到右上 `(1.0, 1.0)`。

### 引擎自动注入的全局 uniforms

| Uniform | 类型 | 含义 / 用途 |
|---|---|---|
| `u_input_tex` | `sampler2D` | 原始（调色前基准）画面纹理；`texture(u_input_tex, uv).rgb` 采样邻域做模糊/锐化/频率分离。实测还可用 `textureSize(u_input_tex, 0)` 取物理分辨率（HighPass_Retouch、bilateral_skin_smooth 均如此，且比 `u_tex_size` 更常用） |
| `u_tex_size` | `vec2` | 画面物理分辨率，如 `vec2(3840.0, 2160.0)` |
| `u_aspect_ratio` | `float` | Width/Height；画正圆/径向效果时乘到 x 分量抵消 UV 拉伸 |

### 引擎提供的辅助函数（实测）

- `fast_blur(u_input_tex, uv, 1.0, radius_param, tex_size)`：内置快速模糊，
  HighPass_Retouch 在用。其内部映射 `r = (intensity * radius * scale) / 50.0`，
  `scale = max(tex_size.x, tex_size.y) / 1024.0`；要得到目标像素半径
  `R_target` 需反解 `radius = R_target * 50.0 / max(scale, 0.001)`（见该插件
  第 19–51 行的完整推导注释——新插件涉及模糊时照抄该换算，保证分辨率无关）。

### namespacing 隔离规则

- 完全不需要手工隔离：`ShaderPipeline`/`shader_compiler` 在编译期把插件变量
  重命名为 `u_P<n>_<name>`（uniform manifest 以 `u_P{p_idx}_` 为前缀注入，
  `shader_compiler.py` 第 308 行），同插件叠 10 层也不冲突。代码里**直接裸名引用
  `@param` 声明的变量**（如 `strength`）。
- **红线（P0-COL-01 实测事故，warm_tint 头部注释记录）**：禁止引用任何**未声明**
  的 `u_*` 标识符。改写用的是词边界匹配，不会命中 `u_` 前缀的标识符，残留如
  `u_strength` 会让真实 GL 编译失败，该效果层随即被渲染循环整体跳过。
- 自定义辅助函数/常量直接写在 `process` 上方即可，无需前缀（重命名机制只作用于
  参数 uniform；跨插件函数名冲突由每插件独立注入作用域消化）。

---

## 4. Capability Manifest 写法（P1-COLOR-15 + Phase 7）

写在头部注释块内，`plugin_manager.py` 的 `CAPABILITY_KEYS` 逐键解析
（`//\s*@<key>\s+(\S+)`，值不设白名单，未知原样保留展示）：

```glsl
// @description 高光染色，模拟胶片 halation
// @input_space scene-linear
// @output_space scene-linear
// @alpha premultiplied
// @precision float16
// @determinism deterministic
// @export_support none
// @threading gpu
// @graphics_backends opengl
// @compute_backends glsl_fragment
```

| 键 | 常用值 | 说明 |
|---|---|---|
| `@input_space` / `@output_space` | `scene-linear` / `srgb` / `display` | 期望输入/输出色彩空间 |
| `@alpha` | `premultiplied` / `straight` | Alpha 语义 |
| `@precision` | `uint8` / `float16` / `float32` | 最低精度要求 |
| `@determinism` | `deterministic` / `seeded` / `nondeterministic` | 确定性 |
| `@export_support` | 指南表：`all` / `video` / `lut` / `none`；实测 warm_tint 写 `opengl` | 导出管线支持（值不由解析器校验，按文档语义写） |
| `@threading` | `gpu` / `cpu` | 线程/设备模型 |
| `@graphics_backends` | 逗号分隔：`opengl` / `metal` / `d3d11` / `d3d12` | 未声明默认 `opengl`；声明不支持的值（如 `vulkan`）会在消费端得到明确报错而非静默忽略 |
| `@compute_backends` | 逗号分隔：`glsl_fragment` / `glsl_compute` / `metal_compute` / `cuda` / `directml` | 未声明默认 `glsl_fragment`；声明 `glsl_compute` 的效果在 fragment 自动回退通道（GL 4.1 大半径效果）仍可用 |
| `@occlusion` | `background_independent` | O7 遮挡裁剪声明：不读下层背景且不把可见内容扩出画布才可声明；缺省 = fail-closed |

现有参照：warm_tint 是唯一声明过 capability 的插件（`@description` +
`@export_support opengl`）。新插件建议至少写 `@input_space` / `@output_space`
（色彩类必写 `scene-linear`）与 `@description`；声明显示在 Effect Controls 插件
分组 tooltip（`format_capability_summary`）。

---

## 5. i18n key 约定

解析器（`plugin_manager.py` 第 121、298 行）实际查询的键：

```
plugins.<plugin_id>.name            # 插件名（键 = @i18n_name 的值；未写 @i18n_name 时 = 文件名去扩展名）
plugins.<plugin_id>.params.<var>    # 参数 Label（键中的 <plugin_id> 恒为文件名 stem）
plugins.<plugin_id>.desc.*          # 描述类文本（计划03 前向约定，如 desc.short / desc.params.<var> 参数 tooltip；
                                     # 当前 10 个 locale 均未填充、解析器也未查询——新增条目按此命名空间预留）
```

实测要点：

- **`name` 键跟随 `@i18n_name` 的值且大小写敏感**：`Color_Space_Transform.tglsl`
  写了 `@i18n_name color_space_transform`，locale 键即小写
  `plugins.color_space_transform.name`；HighPass_Retouch 未改大小写，键为
  `plugins.HighPass_Retouch.name`。新插件统一：文件名 snake_case、
  `@i18n_name` 与文件名一致、locale 键全小写。
- locale 文件：`src/shared/i18n/locales/*.json`（de/en/es/fr/it/ja/ko/pt/ru/zh
  共 10 个）顶层 `plugins` 对象下按 plugin_id 分组。当前仅
  bilateral_skin_smooth、Film_Halation、HighPass_Retouch、color_space_transform
  四组有键（结构 `name` + `params.{var}`）。
- `.tglsl` 内的英文 Label 即兜底文案（`I18N.tr(key, fallback)`），没查到键时
  直接显示，因此 **Label 必须写成可直读的英文**；中文翻译进 zh_CN.json，
  其余 9 个 locale 可后续补。

---

## 6. 现有 5 个插件的质量基准要点

### warm_tint.tglsl（21 行，最简档标杆）

- 纯逐像素数学、零纹理采样 → DCTL 包装路径天然 deterministic。
- 头部注释质量极高：写明 process 契约、参数 ABI（裸名 → `u_P<n>_<name>` 改写）、
  以及"引用未声明 u_* 会导致整层被跳过"的实测教训（P0-COL-01）。新插件头部
  注释至少达到该信息密度。
- 缺点勿模仿：无 `// ---` 包裹行（见 §0）。

### Film_Halation.tglsl（11 行，占位档）

- Phase 1 占位实现（`return color;`），仅证明最小头部 + 2 参数（slider +
  checkbox）即可被完整解析加载。新插件交付不允许停留在占位档。

### HighPass_Retouch.tglsl（86 行，结构标杆）

- **早退出**：`if (mix_amount < 0.001) return color;` 放函数第一行。
- **分辨率无关**：`vec2 current_size = vec2(textureSize(u_input_tex, 0));`
  取实时分辨率，参数按"图像宽度百分比"映射（注释给出 4K 下的等效像素数），
  并完整推导 fast_blur 参数反解公式（§3）。
- **delta 输出模式（核心惯例）**：效果基于 `u_input_tex` 计算，最后
  `return color + (result - original_color);` —— 把效果增量应用到已调色的
  `color` 上，保证插件与上游调色正确叠加。空间采样类新插件一律照此写。
- 高频分离保留负值，显式注释"不 clamp，交给 soft-clip 阶段"。

### bilateral_skin_smooth.tglsl（146 行，性能标杆）

- **循环上限编译期常量**：`const int NUM_SPIRAL_SAMPLES = 32;`（for 写
  `for (int i = 0; i < NUM_SPIRAL_SAMPLES; i++)`）。
- **采样数削减**：32 抖动 Fermat 螺旋采样点（`sqrt(i+0.5)/sqrt(N)` 半径 +
  黄金角 `2.39996322972`）配合两遍 refine，以一半纹理读取达到 64 tap 视觉
  质量——头部注释明说这是 4K 时间线 #1 播放瓶颈的修复。
- 分支守护：`if (strength < 0.001) return color;`、`if (recover > 0.01)`。
- 无分支 gamma 转换写法：`mix(lin, pow, step(thresh, c))` 模式（srgbToLinear /
  linearToSrgb），通道级 smoothstep 软阈值。
- clamp 只作用于**增量**：`clamp(detail, -0.35, 0.35)`，不动基色。
- 强度曲线整形：`1.0 - pow(1.0 - strength, 1.5)` 让滑杆手感线性。
- 常量集中声明在文件顶部并配选型理由注释。

### Color_Space_Transform.tglsl（665 行，大型插件标杆）

- **分节结构**：`// ====== SECTION n: 标题 ======` 横幅注释把文件切成
  输入 gamma 解码 / 输出 gamma 编码 / 矩阵 / 白点适配 / 色调映射 / 主函数
  六节，文件头再用一段"Conversion Pipeline: 1..6"总览注释。
- **常量表带出处**：每个 `mat3` 注明转换链（Input RGB → XYZ(input WP) →
  Bradford → XYZ(D50) → ProPhoto）与白点；数值矩阵列对齐排版。
- **数值安全**：所有 `log`/`pow` 前置 `max(c, vec3(1e-10))` 类守护防 NaN。
- clamp 有理由才用：仅 ACES tone map 在显示编码边界 `clamp(..., 0.0, 1.0)`。
- select 选项驱动 + 早返回 if 链分发（§2④）。

---

## 7. 红线重申（每个新插件交付前逐条自检）

1. **浮点显式 `.0`**：GLSL 强类型，`color * 2` 直接编译失败；所有 float
   字面量必须带小数点（`1.0`、`0.5`、`vec3(2.4)`）。整数语境（int 索引、
   textureSize lod `0`）除外。
2. **循环上限编译期常量**：`for` 边界不能是 uniform 可变值；用
   `const int MAX_R = 6;` 式常量做上限、循环体内 `if (i >= 实际半径) break;`
   收敛（指南 §14 明示参照 Beauty Retouch 手法；bilateral 的
   `NUM_SPIRAL_SAMPLES` 同理）。
3. **HDR 内部不无理由 clamp(0,1)**：`color` 可为负、可远大于 1；频率分离 /
   高频增量必须保留负值。允许的 clamp：效果增量（如 ±0.35）、显示编码边界、
   防 NaN 的 `max(c, eps)` 守护。每处 clamp 要能说出理由（学
   HighPass_Retouch 的注释写法）。
4. **UV 安全**：邻域采样偏移按 `1.0 / textureSize(u_input_tex, 0)` 计算并保证
   分母有下限（`max(scale, 0.001)`、`max(sum_weight, 0.001)`）；径向/圆形
   效果乘 `u_aspect_ratio` 纠偏；不做越界假设。
5. **4K 实时性能预算**：`process` 每帧对 ~800 万像素执行，60FPS 目标。少用
   `sin/cos/pow/exp/log`（bilateral 全文件仅 gamma 转换与强度曲线用 pow），
   多用 `mix/step/smoothstep/clamp/dot`；首行早退出；纹理 tap 数压到个位
   或 ~32；空间采样类一律走 delta 输出模式（§6 HighPass_Retouch）。自评
   偏重时声明 `@preview_cost high|medium|low`。
6. **禁止品牌商标名**：`@name`、`@i18n_name`、参数 Label、描述文本不得出现
   商标名（ARRI/Sony/RED/DaVinci/Photoshop 等）。Color_Space_Transform 的
   选项表含大量厂商曲线名为历史遗留，**不得模仿或扩散**；同类需求用通用
   描述（如 "Camera Log Curve A"）或行业开放标准名（sRGB、Rec.709、HLG、
   ACES、ACEScct）。
7. （补自实测）**禁止引用未声明的 `u_*` uniform**：编译失败会导致整层被
   渲染循环跳过（P0-COL-01），症状隐蔽；只允许 §3 列出的 `u_input_tex` /
   `u_tex_size` / `u_aspect_ratio` 与 `@param` 裸名。
8. （补自实测）**头部必须带 `// --- ... ---` 包裹行**，元数据全部在
   `// -----------------------------` 之前；参数 Label 必须带双引号，
   var_name 只用 `\w` 字符。

---

## 附：快速自检清单（子智能体交付前跑一遍）

- [ ] 头部含 `// --- Topos Plugin Metadata ---` 与 `// -----------------------------`
- [ ] `@name` / `@i18n_name`（= 文件名 snake_case）/ `@author Topos Team` / `@version` / `@description`
- [ ] 色彩类插件声明 `@input_space scene-linear` + `@output_space scene-linear`
- [ ] 每个参数：类型正确、Label 带引号且为可直读英文、default/min/max 完整
- [ ] 所有 float 字面量带 `.0`；循环上限为 `const int`
- [ ] 无未声明 `u_*` 引用；除显示编码边界外无 clamp(0,1)
- [ ] 分母有下限守护；径向效果乘 `u_aspect_ratio`
- [ ] 空间采样类走 delta 输出：`return color + (result - original_color);`
- [ ] 首行早退出；昂贵函数用量已最小化；必要时声明 `@preview_cost`
- [ ] 无品牌商标名
- [ ] zh_CN.json 已加 `plugins.<id>.name` / `.params.<var>`（英文兜底已写在
      .tglsl 内，其余 locale 可后补）
