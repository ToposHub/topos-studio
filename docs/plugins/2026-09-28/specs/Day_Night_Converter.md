# Day_Night_Converter 规格（日转夜启发式）

> 计划03 插件库扩充 · 色彩类
> 规格日期：2026-09-28 · 版本：1.0 · 状态：已实现
> 规范依据：`docs/plugins/2026-09-28/STYLE_NOTES.md`（行号以该文为准）
> 色彩类参照：`plugins/warm_tint.tglsl`、`plugins/Three_Way_Tint.tglsl`

---

## 1. 目标与定位

**启发式 day-for-night**：把白天拍摄的画面压成夜景观感。四个成分，全部是
乘性增益（无 additive lift、无显示域 clamp）：

1. **曝光下压**：夜景深度驱动最多 −2.3 档乘性下压；
2. **冷色温偏移**：R 下沉 / G 锚 / B 抬升的经典夜景冷调；
3. **亮度加权**：暗区/中间调吃满夜景深度，亮区（≈天空）按 Sky Protection
   衰减下压与冷移，保住高光层次；
4. **轻微蓝移高光**：受保护的天空仍带一点月光蓝，避免"只压暗不加色"。

**明确非目标（卡片同步注明）**：这是启发式效果，**不是真实日夜替换**——
不做天空语义分割、不合成月亮/街灯/窗户光、不做时间一致性处理（视频逐帧
独立）。需要真实替换时应走 AI 生成管线而非本插件。

## 2. 参数 ABI（共 4 个有效参数 + 1 个 `@param separator` 视觉分组）

| # | 指令 | var | Label（英文兜底） | 默认 | 范围 |
|---|------|-----|-------------------|------|------|
| 1 | `@param slider` | `night_depth` | `Night Depth` | `0.55` | `0.0 1.0` |
| 2 | `@param slider` | `temperature_shift` | `Cool Shift` | `0.45` | `0.0 1.0` |
| 3 | `@param slider` | `sky_protection` | `Sky Protection` | `0.50` | `0.0 1.0` |
| 4 | `@param slider` | `exposure` | `Exposure` | `0.0` | `-3.0 3.0` |

- 全部 slider → GLSL 侧 `float`；`exposure` 用对称范围 `-3.0 3.0`（档位，
  STYLE_NOTES §2① 曝光类惯例），默认 `0.0`（只微调，不抢夜景深度的戏）。
- `@param separator` 在解析器中是 no-op（STYLE_NOTES §1），仅作源码视觉分组
  （主效果两参 vs 保护/微调两参）。
- 文件名 `Day_Night_Converter.tglsl` → plugin_id = `Day_Night_Converter`；
  `@i18n_name Day_Night_Converter` 与文件名一致（沿用 HighPass_Retouch /
  Three_Way_Tint 先例：name 键大小写跟随 @i18n_name，params 键恒用文件名 stem）。

## 3. 算法（全乘性，HDR 安全）

### 3.1 HDR 安全的色调坐标 t

与 Three_Way_Tint 同源的 Reinhard 型软压缩：

```glsl
float luma = max(dot(color, vec3(0.2126, 0.7152, 0.0722)), 0.0);  // Rec.709
float t = luma / (luma + 0.18);   // pivot = 中灰 0.18
```

- 锚点：`luma 0.18 → t 0.50`；`luma 1.0 → t 0.847`；`luma 4.0 → t 0.957`。
  高光过渡区不会在 100 nits 一到就"用完"。
- `max(luma, 0.0)`：负 luma（out-of-gamut）按最暗档处理（t = 0），分母恒
  ≥ 0.18，除法天然安全。该 max 只作用于权重坐标，不触碰颜色本身。

### 3.2 亮度加权（天空保护）

```glsl
float w_sky = smoothstep(0.55, 0.85, t) * sky_protection;
float night = night_depth * (1.0 - w_sky);
```

- 过渡带 [0.55, 0.85] 换算回线性 luma ≈ 0.220 → 1.020：SDR 白点以下开始
  保护，HDR 高值（>1.0）完全进入保护区。
- `sky_protection = 0`：全画面吃满深度（经典整体压暗）；`= 1`：亮区
  `night → 0`，不下压也不吃冷移，只剩月光蓝。
- `night` 是逐像素有效夜景量：暗区（t ≤ 0.55）恒等于 `night_depth`，
  完全不受 `sky_protection` 影响——"暗区加深、亮区受保护"。

### 3.3 四成分合成为单一乘性增益

```glsl
float stops = exposure - 2.3 * night;                  // 1) 曝光下压（档）
float cool  = temperature_shift * night;               // 2) 冷移（随保护衰减）
float moon  = smoothstep(0.60, 0.90, t) * night_depth; // 4) 月光蓝（不被保护衰减）
vec3 gain = vec3(exp2(stops))
          * mix(vec3(1.0), vec3(0.78, 0.94, 1.16), cool)
          * mix(vec3(1.0), vec3(0.94, 0.98, 1.06), moon);
return color * gain;
```

设计论证：

- **曝光下压用 exp2**：档位语义要求 2^x；`exp2` 是单条硬件指令（EX2），
  比 `pow`（内部 exp+log 两条）便宜，整档换算精确（−1 档 = ×0.5）。
  全文件唯一超越函数，满足红线 5 的预算。
- **冷调 (0.78, 0.94, 1.16)**：经典夜景蓝的手感锚点，满强度时
  B/R 增益比 ≈ 1.49；默认参数下暗区冷移 0.2475 → 增益 (0.947, 0.985, 1.040)，
  "刚添加即有可见但不过火"。
- **月光蓝只看 `night_depth` 不被 `sky_protection` 衰减**：满保护的天空
  仍有夜的色温（0.94/0.98/1.06，满深度 B/R 比 ≈ 1.13），否则保护会把
  天空退回白天。过渡带 [0.60, 0.90]（线性 luma 0.270 → 1.620）略宽于
  天空带，让蓝移稍早介入。
- **默认参数手感**（暗像素，`night = 0.55`）：下压 −1.265 档 → ×0.414，
  冷移 ×(0.947, 0.985, 1.040)，总增益 ≈ ×(0.392, 0.408, 0.430)——中灰
  0.18 压到 ≈ 0.073 的深暮色；天空像素（luma 2.0，默认保护 0.5）总增益
  ≈ ×(0.607, 0.634, 0.681)，2.0 → (1.21, 1.27, 1.36) 仍为 HDR 且偏蓝。
- **全乘性、无 clamp**：`10.0` 按比例缩放不被压回 [0,1]；负值
  （out-of-gamut）按正比例缩放，符号不翻转；`night_depth = 0` 且
  `exposure = 0` 时增益恒为 1（黑位白位不动）。

## 4. 早退出与性能

```glsl
if (night_depth < 0.001 && temperature_shift < 0.001
    && abs(exposure) < 0.001) return color;   // 首行早退出
```

- `sky_protection` 不参与早退出判断：夜景量与月光蓝都以 `night_depth`
  为门，它单独为非 0 时不产生任何效果。
- 纯逐像素数学、零纹理采样、零循环、唯一超越函数 1 次 exp2 →
  `@determinism deterministic`、`@preview_cost low`。
- 不读 `u_input_tex`，不走 delta 输出模式（该模式是空间采样类的惯例，
  见 STYLE_NOTES §6 HighPass_Retouch）；直接变换已调色的 `color` 本身，
  与 warm_tint / Three_Way_Tint 同类。
- `uv` 形参按 process 契约保留但未使用。

## 5. 文件头元数据与 capability 声明

```
@name Day-Night Converter
@i18n_name Day_Night_Converter
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

## 6. i18n 片段

文件：`docs/plugins/2026-09-28/i18n/Day_Night_Converter.json`（zh_CN + en_US，
待合并进 `src/shared/i18n/locales/*.json` 的 `plugins` 对象）：

```
plugins.Day_Night_Converter.name
plugins.Day_Night_Converter.params.{night_depth, temperature_shift,
                                    sky_protection, exposure}
plugins.Day_Night_Converter.desc.short / desc.params.<var>   （计划03 前向约定命名空间）
```

英文兜底文案即 .tglsl 内的 Label；zh_CN 未合并前显示英文兜底。
zh 的 name/desc 均注明"启发式、非真实日夜替换"。

## 7. 验收标准（验证脚本逐条覆盖）

1. 真实 `PluginManager._parse_plugin_file` 可解析：4 个 slider 参数
   类型/默认值/范围（含 exposure 的负最小值）与 §2 一致，shader_code 非空。
2. capability 声明解析出 `input_space=scene-linear`、`output_space=scene-linear`、
   `determinism=deterministic`、`preview_cost=low`。
3. 结构红线：包裹行存在；元数据全部位于包裹行之前；正文无 `@指令` 泄漏；
   `@i18n_name` 与文件名 stem 一致。
4. GLSL 红线：无整数语境外的无 `.0` float 字面量；无未声明 `u_*` 引用；
   无循环；零 clamp（含返回路径）；无商标名。
5. 数学性质（脚本内置纯 Python 复算）：全零参数严格恒等（含 HDR/负值样点）；
   `night_depth` 单调加深、`sky_protection` 单调提亮（保护亮区）；
   曝光档位精确（−1 档恰 ×0.5）；冷移方向正确（B 增 R 减）；满保护时天空
   只剩月光蓝（无下压无冷移）；HDR/负值不被压回、符号保持。
6. i18n 片段 JSON 可解析且键覆盖 4 个参数 var + name + desc。
7. 验证输出贴于本文末尾（脚本 `--update-spec` 生成）。

## 8. 交付物清单

| 文件 | 说明 |
|---|---|
| `plugins/Day_Night_Converter.tglsl` | 实现（唯一新增运行时文件） |
| `docs/plugins/2026-09-28/specs/Day_Night_Converter.md` | 本规格 |
| `docs/plugins/2026-09-28/tools/validate_Day_Night_Converter.py` | headless 解析验证 |
| `docs/plugins/2026-09-28/i18n/Day_Night_Converter.json` | i18n 片段（zh_CN + en_US） |
| `docs/plugins/2026-09-28/cards/Day_Night_Converter.md` | 效果卡片（注明启发式） |

红线：不修改任何既有文件（含 `src/shared/i18n/locales/*.json`）。
<!-- Day_Night_Converter:validate start -->
## 9. 验证输出（tools/validate_Day_Night_Converter.py --update-spec 生成）

```
engine: real PluginManager（src/features/color/core/plugin_manager.py）
target: plugins/Day_Night_Converter.tglsl
date: 2026-09-29 09:12:11

[OK  ] 0.1 插件文件存在 —— /Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/plugins/Day_Night_Converter.tglsl
[OK  ] 1.1 真实解析器解析成功 —— plugin_id=Day_Night_Converter
[OK  ] 1.2 @name 解析 —— got='Day-Night Converter'
[OK  ] 1.3 @author —— got='Topos Team'
[OK  ] 1.4 @version —— got='1.0'
[OK  ] 1.5 @description 非空（含启发式说明） —— Heuristic day-for-night look: luminance-weighted exposure pu...
[OK  ] 1.6 @description 注明启发式性质
[OK  ] 1.7 shader_code 非空且含 process 契约 —— 3428 chars
[OK  ] 2.1 有效参数共 4 个（全 slider） —— got 4
[OK  ] 2.2 无 @param 行被解析器静默丢弃 —— raw=5 separator=1 parsed=4
[OK  ] 2.3 参数[night_depth] 类型/默认值/范围 —— type=slider var=night_depth d/m/x=0.55/0.0/1.0
[OK  ] 2.4 参数[temperature_shift] 类型/默认值/范围 —— type=slider var=temperature_shift d/m/x=0.45/0.0/1.0
[OK  ] 2.5 参数[sky_protection] 类型/默认值/范围 —— type=slider var=sky_protection d/m/x=0.5/0.0/1.0
[OK  ] 2.6 参数[exposure] 类型/默认值/范围 —— type=slider var=exposure d/m/x=0.0/-3.0/3.0
[OK  ] 3.1 @input_space —— got='scene-linear' want='scene-linear'
[OK  ] 3.2 @output_space —— got='scene-linear' want='scene-linear'
[OK  ] 3.3 @determinism —— got='deterministic' want='deterministic'
[OK  ] 3.4 @export_support —— got='opengl' want='opengl'
[OK  ] 3.5 @preview_cost —— got='low'
[OK  ] 4.1 头部包裹起始行 —— // --- Topos Plugin Metadata ---
[OK  ] 4.2 头部包裹结束行 // ----
[OK  ] 4.3 元数据指令未泄漏进着色器代码 —— body 干净
[OK  ] 4.4 @i18n_name 与文件名 stem 一致 —— Day_Night_Converter
[OK  ] 5.1 无未声明 u_* 引用（P0-COL-01） —— refs=无
[OK  ] 5.2 float 字面量全部带 .0（整数语境除外） —— 无违规
[OK  ] 5.3 零 clamp 审计（全乘性增益，HDR/负值直通 soft-clip） —— 无 clamp
[OK  ] 5.4 返回路径无 clamp
[OK  ] 5.5 唯一超越函数为 1 次 exp2（档位换算，单条硬件指令） —— pow/exp/log/sin/cos=0 exp2=1
[OK  ] 5.6 零循环（无需常量上限检查）
[OK  ] 5.7 零纹理采样（与头部声明一致，无需 delta 输出模式）
[OK  ] 5.8 无品牌商标名
[OK  ] 5.9 process 契约签名
[OK  ] 5.10 首行早退出（深度/冷移全 0 且曝光为 0 时恒等返回）
[OK  ] 6.1 全零参数（深度/冷移/曝光为 0）严格恒等，且与保护量无关
[OK  ] 6.2 night_depth 单调加深暗区（无回摆） —— 0.0->0.180 1.0->0.036
[OK  ] 6.3 sky_protection 单调提亮亮区（luma 2.0 天空样点） —— 0.0->0.822 1.0->1.993
[OK  ] 6.4 曝光档位精确（-1 档恰 x0.5，+1 档恰 x2.0）
[OK  ] 6.5 冷移方向正确（B 增 R 减），冷移为 0 时增益中性 —— cool=0.248 R_gain=0.946 B_gain=1.040
[OK  ] 6.6 满保护时天空不吃下压与冷移，只剩月光蓝（x(0.94,0.98,1.06)） —— t=0.917 night=0.000
[OK  ] 6.7 HDR 样点不被压回 [0,1]（4.0 默认参数 -> 全通道 > 1） —— -> (1.52,1.62,1.79)
[OK  ] 6.8 全乘性：恒定权重档内输出对输入成比例（x2 输入 -> x2 输出） —— t=0.125/0.222
[OK  ] 6.9 负值（out-of-gamut）符号保持 —— out.r=-0.1967
[OK  ] 6.10 暗区（t <= 0.55）完全不受 sky_protection 影响
[OK  ] 7.1 i18n 片段存在且 JSON 可解析 —— zh_CN + en_US
[OK  ] 7.2 zh_CN/en_US 键覆盖 name + 4 参数 + desc.short —— 全覆盖

== 45/45 checks passed ==
```
<!-- Day_Night_Converter:validate end -->
