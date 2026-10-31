# Three_Way_Tint 规格（阴影 / 中间调 / 高光三段着色）

> 计划03 插件库扩充 · 色彩类
> 规格日期：2026-09-28 · 版本：1.0 · 状态：已实现
> 规范依据：`docs/plugins/2026-09-28/STYLE_NOTES.md`（行号以该文为准）
> 色彩类参照：`plugins/Color_Space_Transform.tglsl`

---

## 1. 目标与定位

按亮度把画面分成**阴影 / 中间调 / 高光**三个连续分区，各自独立着色。典型用途：

- 冷阴影 + 暖高光的经典 teal-orange 分离；
- 只污染中间调的场景氛围渲染；
- 不动黑位与白位前提下的分区色调偏移（乘性增益不引入 additive lift）。

非目标：不做分区曝光/对比度控制（那是其他插件的职责）；不做 qualifier 式色度限定。

## 2. 参数 ABI（共 6 个有效参数 + 2 个 `@param separator` 视觉分组）

| # | 指令 | var | Label（英文兜底） | 默认 | 范围 |
|---|------|-----|-------------------|------|------|
| 1 | `@param color` | `shadow_color` | `Shadow Color` | `0.38 0.46 0.60`（冷） | 各分量 0.0–1.0 |
| 2 | `@param slider` | `shadow_strength` | `Shadow Strength` | `0.30` | `0.0 1.0` |
| 3 | `@param color` | `midtone_color` | `Midtone Color` | `0.52 0.50 0.47`（近中性微暖） | 各分量 0.0–1.0 |
| 4 | `@param slider` | `midtone_strength` | `Midtone Strength` | `0.25` | `0.0 1.0` |
| 5 | `@param color` | `highlight_color` | `Highlight Color` | `0.60 0.53 0.42`（暖） | 各分量 0.0–1.0 |
| 6 | `@param slider` | `highlight_strength` | `Highlight Strength` | `0.30` | `0.0 1.0` |

- color 控件 GLSL 侧为 `vec3`（scene-linear），slider 为 `float`。
- `@param separator` 在解析器中是 no-op（STYLE_NOTES §1），仅作源码视觉分组。
- 文件名 `Three_Way_Tint.tglsl` → plugin_id = `Three_Way_Tint`；`@i18n_name Three_Way_Tint`
  与文件名一致（沿用 HighPass_Retouch 先例：name 键大小写跟随 @i18n_name，
  params 键恒用文件名 stem）。

## 3. 亮度分区算法（平滑混合，无硬边）

### 3.1 HDR 安全的色调坐标 t

`process` 收到的 `color` 是 scene-linear，可 < 0.0 也可 >> 1.0（HDR）。分区权重的
自变量必须先压到 0–1 连续区间，且**对 HDR 高值保持单调、不饱和到无法区分**：

```glsl
float luma = dot(color, vec3(0.2126, 0.7152, 0.0722));   // Rec.709 luma
float t = max(luma, 0.0);
t = t / (t + 0.18);   // Reinhard 型软压缩，pivot = 中灰 0.18
```

- 锚点：`luma 0.18 → t 0.50`；`luma 1.0 → t 0.847`；`luma 4.0 → t 0.957`。
  高光过渡区不会在 100 nits 一到就"用完"。
- `max(luma, 0.0)`：负 luma（out-of-gamut 像素）按纯阴影处理（t = 0），
  分母恒 ≥ 0.18，除法天然安全，无需额外下限守护。
- **此处 clamp/max 只作用于分区坐标，不作用于颜色本身**——输出仍保留 HDR 与负值。

### 3.2 三区权重（partition of unity）

```glsl
float w_shadow    = 1.0 - smoothstep(0.25, 0.55, t);
float w_highlight = smoothstep(0.55, 0.85, t);
float w_midtone   = clamp(1.0 - w_shadow - w_highlight, 0.0, 1.0);
```

- 两段 smoothstep 区间 `[0.25, 0.55]` 与 `[0.55, 0.85]` 不重叠（仅在 0.55 相接），
  所以 `w_shadow + w_highlight ≤ 1.0` 恒成立，`w_midtone ≥ 0`；
  clamp 仅作浮点累加误差保险（允许的 clamp：防病态值守护，STYLE_NOTES 红线 3）。
- 换算回 scene-linear（`l = 0.18·t/(1−t)`）：
  - 阴影→中间调过渡：luma ≈ 0.060 → 0.220
  - 中间调→高光过渡：luma ≈ 0.220 → 1.020（中灰 0.18 恰落在阴影侧过渡带内，
    0.22 处三区权重为 (0, 1, 0)）
- 任意 t 下 `w_s + w_m + w_h = 1`，无亮度断层、无硬边（需求"平滑混合"）。

## 4. 着色混合模型（乘性 / 叠加式，保留 HDR 动态范围）

```glsl
// 单区归一化增益：tint 0.5 = 中性锚点（增益 1.0）
vec3 zone_gain(vec3 tint_color, float weight, float strength) {
    return mix(vec3(1.0), tint_color * 2.0, weight * strength);
}
// 总增益 = 三区增益连乘（权重互斥，连乘等价于按区独立作用）
vec3 gain = zone_gain(shadow_color,    w_shadow,    shadow_strength)
          * zone_gain(midtone_color,   w_midtone,   midtone_strength)
          * zone_gain(highlight_color, w_highlight, highlight_strength);
return color * gain;
```

设计论证：

- **乘性保 HDR**：增益是纯比例因子，`10.0 × 1.2 = 12.0`，高光动态范围不被压缩；
  负值（out-of-gamut）同样按比例缩放，方向不翻转。
- **"叠加"手感**：经典 overlay 在 0.5 pivot 处做 2× 提升/衰减，但把输出压回
  [0,1]（显示编码边界），会毁掉 HDR。`tint_color * 2.0` 的归一化比值乘法保留
  overlay 的 0.5 对称手感（0.25 → ×0.5，0.75 → ×1.5），**不设上界**，
  等价于"无界 overlay"。
- **无 additive lift**：tint 全 0.5 或 strength 全 0 时 `gain ≡ 1.0`，黑位/白位
  严格不动（早退出路径直接 `return color`）。
- 增益界：tint 分量 ∈ [0,1] → 单区增益 ∈ [0, 2]，仅当 `w·strength = 1` 达到端点；
  默认参数下满权重增益为 shadow (0.93, 0.98, 1.06)、midtone (1.01, 1.00, 0.99)、
  highlight (1.06, 1.02, 0.95) —— "刚添加即有可见但不过火"。
- 全流程无 clamp(0,1)、无 pow/exp/log，逐像素仅 1 次 dot、2 次 smoothstep、
  3 次 mix、1 次除法 → 满足 4K/60FPS 预算（STYLE_NOTES 红线 5）。

## 5. 早退出与性能

```glsl
if (shadow_strength < 0.001 && midtone_strength < 0.001
    && highlight_strength < 0.001) return color;   // 首行早退出
```

- 纯逐像素数学、零纹理采样、零循环 → `@determinism deterministic`、
  `@preview_cost low`。
- 不读 `u_input_tex`，因此**不走 delta 输出模式**（该模式是空间采样类的惯例，
  见 STYLE_NOTES §6 HighPass_Retouch）；直接变换已调色的 `color` 本身，
  与 warm_tint 同类。
- `uv` 形参按 process 契约保留但未使用。

## 6. 文件头元数据与 capability 声明

```
@name Three-Way Tint
@i18n_name Three_Way_Tint
@author Topos Team
@version 1.0
@description <单行英文，见实现>
@input_space scene-linear
@output_space scene-linear
@determinism deterministic
@export_support opengl        （沿用 warm_tint 已验证先例）
@preview_cost low
```

头部必须含 `// --- Topos Plugin Metadata ---` 与 `// -----------------------------`
包裹行，元数据全部在包裹行之前（STYLE_NOTES 红线 8）；着色器代码注释中
**不得出现** `@<key>` 形态的注释（解析正则全文扫描）。

## 7. i18n 片段

文件：`docs/plugins/2026-09-28/i18n/Three_Way_Tint.json`（zh_CN + en_US，
待合并进 `src/shared/i18n/locales/*.json` 的 `plugins` 对象）：

```
plugins.Three_Way_Tint.name
plugins.Three_Way_Tint.params.{shadow_color, shadow_strength,
                               midtone_color, midtone_strength,
                               highlight_color, highlight_strength}
plugins.Three_Way_Tint.desc.short / desc.params.<var>   （计划03 前向约定命名空间）
```

英文兜底文案即 .tglsl 内的 Label；zh_CN 未合并前显示英文兜底。

## 8. 验收标准（验证脚本逐条覆盖）

1. 真实 `PluginManager._parse_plugin_file` 可解析：6 参数（3 color + 3 slider）
   类型/默认值/范围与 §2 一致，shader_code 非空。
2. capability 声明解析出 `input_space=scene-linear`、`output_space=scene-linear`、
   `determinism=deterministic`、`preview_cost=low`。
3. 结构红线：包裹行存在；元数据全部位于包裹行之前；正文无 `@指令` 泄漏。
4. GLSL 红线：无整数语境外的无 `.0` float 字面量；无未声明 `u_*` 引用；
   无循环；无 clamp(0,1)；无商标名。
5. 数学性质（脚本内置纯 Python 复算）：分区权重和恒为 1；三强度为 0 时
   恒等输出；增益 = 1 当 tint = 0.5；HDR 样点（4.0 / 10.0）按比例缩放；
   负值样点符号保持。
6. 验证输出贴于本文末尾（脚本 `--update-spec` 生成）。

## 9. 交付物清单

| 文件 | 说明 |
|---|---|
| `plugins/Three_Way_Tint.tglsl` | 实现（唯一新增运行时文件） |
| `docs/plugins/2026-09-28/specs/Three_Way_Tint.md` | 本规格 |
| `docs/plugins/2026-09-28/tools/validate_Three_Way_Tint.py` | headless 解析验证 |
| `docs/plugins/2026-09-28/i18n/Three_Way_Tint.json` | i18n 片段（zh_CN + en_US） |
| `docs/plugins/2026-09-28/cards/Three_Way_Tint.md` | 效果卡片 |

红线：不修改任何既有文件（含 `src/shared/i18n/locales/*.json`）。
<!-- Three_Way_Tint:validate start -->
## 10. 验证输出（tools/validate_Three_Way_Tint.py --update-spec 生成）

```
engine: real PluginManager（src/features/color/core/plugin_manager.py）
target: plugins/Three_Way_Tint.tglsl
date: 2026-09-29 08:58:09

[OK  ] 0.1 插件文件存在 —— /Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/plugins/Three_Way_Tint.tglsl
[OK  ] 1.1 真实解析器解析成功 —— plugin_id=Three_Way_Tint
[OK  ] 1.2 @name 解析 —— got='Three-Way Tint'
[OK  ] 1.3 @author —— got='Topos Team'
[OK  ] 1.4 @version —— got='1.0'
[OK  ] 1.5 @description 非空 —— Separate tint grading for shadows, midtones and highlights w...
[OK  ] 1.6 shader_code 非空且含 process 契约 —— 3062 chars
[OK  ] 2.1 有效参数共 6 个（3 color + 3 slider） —— got 6
[OK  ] 2.2 无 @param 行被解析器静默丢弃 —— raw=8 separator=2 parsed=6
[OK  ] 2.3 参数[shadow_color] 类型/默认值/范围 —— type=color var=shadow_color rgb=[0.38, 0.46, 0.6]
[OK  ] 2.4 参数[shadow_strength] 类型/默认值/范围 —— type=slider var=shadow_strength d/m/x=0.3/0.0/1.0
[OK  ] 2.5 参数[midtone_color] 类型/默认值/范围 —— type=color var=midtone_color rgb=[0.52, 0.5, 0.47]
[OK  ] 2.6 参数[midtone_strength] 类型/默认值/范围 —— type=slider var=midtone_strength d/m/x=0.25/0.0/1.0
[OK  ] 2.7 参数[highlight_color] 类型/默认值/范围 —— type=color var=highlight_color rgb=[0.6, 0.53, 0.42]
[OK  ] 2.8 参数[highlight_strength] 类型/默认值/范围 —— type=slider var=highlight_strength d/m/x=0.3/0.0/1.0
[OK  ] 3.1 @input_space —— got='scene-linear' want='scene-linear'
[OK  ] 3.2 @output_space —— got='scene-linear' want='scene-linear'
[OK  ] 3.3 @determinism —— got='deterministic' want='deterministic'
[OK  ] 3.4 @export_support —— got='opengl' want='opengl'
[OK  ] 3.5 @preview_cost —— got='low'
[OK  ] 4.1 头部包裹起始行 —— // --- Topos Plugin Metadata ---
[OK  ] 4.2 头部包裹结束行 // ----
[OK  ] 4.3 元数据指令未泄漏进着色器代码 —— body 干净
[OK  ] 4.4 @i18n_name 与文件名 stem 一致 —— Three_Way_Tint
[OK  ] 5.1 无未声明 u_* 引用（P0-COL-01） —— refs=无
[OK  ] 5.2 float 字面量全部带 .0（整数语境除外） —— 无违规
[OK  ] 5.3 clamp 审计：唯一 clamp 是中间调权重浮点保险 —— float w_midtone   = clamp(1.0 - w_shadow - w_highlight, 0.0, 1.0);
[OK  ] 5.4 返回路径无 clamp（HDR/负值直通 soft-clip）
[OK  ] 5.5 零循环（无需常量上限检查）
[OK  ] 5.6 零纹理采样（与头部声明一致，无需 delta 输出模式）
[OK  ] 5.7 无品牌商标名
[OK  ] 5.8 process 契约签名
[OK  ] 5.9 首行早退出（三强度全 0 恒等返回）
[OK  ] 6.1 三区权重和恒为 1（partition of unity） —— max|sum-1|=0.00e+00
[OK  ] 6.2 阴影权重单调降 / 高光权重单调升（无硬边回摆）
[OK  ] 6.3 三强度为 0 时严格恒等（含 HDR/负值样点）
[OK  ] 6.4 tint 全 0.5（中性锚点）时增益恒为 1
[OK  ] 6.5 HDR 样点按比例缩放（4.0 -> (4.80,4.24,3.36)，不被压回 [0,1]） —— t=0.957 w_h=1.000
[OK  ] 6.6 负值（out-of-gamut）按纯阴影分区且符号保持 —— out.r=-0.3800 w_s=1.000
[OK  ] 6.7 锚点：luma 0.22 为纯中间调、0.06 为纯阴影 —— 0.22 -> (s=0.00, m=1.00, h=0.00); 0.06 -> s=1.00

== 40/40 checks passed ==
```
<!-- Three_Way_Tint:validate end -->
