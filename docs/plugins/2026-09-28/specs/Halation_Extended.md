# Halation_Extended 插件规格（Spec）

> 计划03 · GLSL 插件库扩充 · 2026-09-28
> 子任务：Halation_Extended —— Film_Halation 思路上的**独立变体**（不同模糊核 + 可调晕染色调）
> 状态：已实现，headless 解析验证通过（输出见文末附录）
> 交付物：`plugins/Halation_Extended.tglsl`、`docs/plugins/2026-09-28/tools/validate_Halation_Extended.py`、
> `docs/plugins/2026-09-28/i18n/Halation_Extended.json`、`docs/plugins/2026-09-28/cards/Halation_Extended.md`

---

## 1. 定位与差异化（vs Film_Halation）

Film_Halation 当前为 Phase 1 占位实现（`return color;`），其参数面
（`blur_radius` + `enable_tint` 开关式红染）指向"内置 fast_blur + 固定红色调"的
经典路线。Halation_Extended **不复制该路线**，核与色调路径均独立设计：

| 维度 | Film_Halation（规划路线） | Halation_Extended（本插件） |
|---|---|---|
| 模糊核 | 内置 `fast_blur`（盒式映射，见 HighPass_Retouch 的反解用法） | **ADR 交替方向环核**（自研 13-tap：3 环 × 4 tap + 中心） |
| 晕染色调 | checkbox 开关 + 固定红色 | **HSV 色相锚点 + 连续偏移**（`hue_shift` 旋钮，红锚点 ±0.5 覆盖全色环） |
| 高光选择 | 无（或硬阈值） | **软膝 luminance 阈值**（`threshold`，smoothstep 软过渡，无硬截止） |
| 强度 | 无独立强度 | `strength`（1-(1-s)^1.5 曲线整形 + 增量式加性光） |
| 空间采样语义 | — | 逐 tap 先掩蔽后累加（能量从亮区携带进暗区）+ delta 输出模式 |

两者 plugin_id 不同，可共存于同一条效果链。

## 2. 算法设计

### 2.1 高光提取（软膝，HDR 安全）

- luminance = `dot(c, vec3(0.2126, 0.7152, 0.0722))`（scene-linear）。
- 掩码 `m = smoothstep(t, t + 0.35, luma)`，`t = threshold ∈ [0,1]`。
- 软膝宽 0.35 为常量：luma ≥ t+0.35（**含 HDR > 1.0 的值**）满强度发光，
  无硬截止振铃；负值 / 低照度样本掩码为 0，不参与晕染。
- 不做任何 clamp(0,1)：输入可为负、可远大于 1。

### 2.2 ADR 交替方向环核（Alternating-Direction Ring kernel）

独立于 `fast_blur`（盒式）与 bilateral 的 Fermat 螺旋，采样方案：

- **3 个嵌套环 × 每环 4 tap**，环方向交替：轴对齐 → 对角 → 轴对齐。
  方向交替使采样格点去相关，避免可分离/盒式核遗留的十字形或轴向重影。
- **环半径**（占用户半径比例）：`[0.35, 0.6, 1.0]` —— 紧核心 + 长尾巴。
- **环权重**：`[1.0, 0.65, 0.45]` —— 前置加载衰减，既非均匀（盒式）也非
  高斯；同核心尺寸下尾部比高斯重，贴合 halation 的扩散观感。
- 中心 tap 权重 1.0，保证光晕"附着"在光源上。
- 总成本：**13 次纹理采样/像素**（对比 bilateral 的 64 tap），无 sin/cos，
  循环上限 `const int ADR_TAPS = 12;`（编译期常量，红线 #2）。
- 逐 tap **先掩蔽后累加**：`spill += s * (highlightMask(s) * w)`。
  若先模糊后掩蔽，光晕会在明暗区域边界处整体衰减而非向外扩散——
  这与 bloom/halation 的物理方向相反。
- 分辨率无关：`radius` 0→1 映射为图像宽度的 0.4%–3.0%
  （4K 下 15–115 px 等效），`tex_offset = 1.0 / textureSize(...)`，
  半径下限 `max(2.0, ...)` 防退化。

### 2.3 HSV 晕染色调路径

对累加平均 `spill_avg = spill / max(sum_w, 0.001)`：

1. `rgb2hsv`（分支极简 K-space 实现，分母带 `1.0e-10` 防 NaN；
   消色差/纯黑输入的 hue 无意义，随后即被覆盖，仅需防除零）。
2. **色相重锚定**：`h = fract(HALATION_BASE_HUE + hue_shift)`，
   `HALATION_BASE_HUE = 0.0`（经典胶片 halation 的红锚点）。
   `hue_shift ∈ [-0.5, 0.5]`，`fract` 对负输入良定义（x - floor(x)），
   ±0.5 合计覆盖全色环。
3. **饱和度下限**：`s = max(s, 0.55)` —— 让晕影读作"有色 halation"
   而非白色 bloom；纯黑邻域因 v=0 输出恒为 0，不受下限影响。
4. `hsv2rgb` 还原。**HDR 能量（v）原样通过**：spill_avg 可 > 1.0，
   v 不做任何范围限制。

### 2.4 强度整形与增量输出

- `glow_gain = (1.0 - pow(1.0 - strength, 1.5)) * 1.5`：1.5 次幂曲线
  保持滑杆手感线性（仓库惯例，cf. bilateral_skin_smooth）；`pow` 基数
  ∈ [0, 0.999]（首行早退出保证 strength ≥ 0.001），无负基数 UB。
- 效果为**纯加性光**：`result = original_color + tinted * glow_gain`，
  最终 `return color + (result - original_color);` —— 标准 delta 输出，
  上游调色正确叠加（空间采样类红线）。

## 3. 参数表

| # | 类型 | var | 英文 Label（兜底文案） | 默认 | 范围 | 语义 |
|---|---|---|---|---|---|---|
| 1 | slider | `strength` | "Strength" | 0.5 | 0.0–1.0 | 晕染发光强度（曲线整形后峰值增益 1.5） |
| 2 | slider | `radius` | "Radius" | 0.5 | 0.0–1.0 | 扩散半径（图像宽度 0.4%–3.0%，分辨率无关） |
| — | separator | — | — | — | — | 视觉分组（解析 no-op，仓库先例） |
| 3 | slider | `hue_shift` | "Hue Shift" | 0.0 | -0.5–0.5 | 晕染色相偏移（0=经典红，±0.5 覆盖全色环） |
| 4 | slider | `threshold` | "Highlight Threshold" | 0.55 | 0.0–1.0 | 仅高亮发光的软膝阈值（scene-linear luma） |

默认组合（strength 0.5 / threshold 0.55）满足"刚添加即有可见但不过火"：
典型 SDR 高光邻域产生约 0.2–0.3 的加性红晕，HDR 高光满强度扩散。

## 4. 文件头元数据与 capability 声明

- 标准头：`// --- Topos Plugin Metadata ---` … `// -----------------------------`
  包裹行；`@name Halation Extended`、`@i18n_name Halation_Extended`
  （= 文件名 stem，locale 键 `plugins.Halation_Extended.*`）、
  `@author Topos Team`、`@version 1.0`、`@description`（单行英文）。
- capability：`@input_space scene-linear`、`@output_space scene-linear`
  （色彩类必写）、`@precision float16`、`@determinism deterministic`、
  `@export_support video`（空间采样效果不可导出 LUT，按指南值域取 video）、
  `@threading gpu`。
- 不声明 `@preview_cost`：13 tap + 每 tap 一次 dot/smoothstep，成本低于
  未声明的 bilateral（64 tap）与 HighPass（2×fast_blur），未达"自评偏重"线。
- 不声明 `@occlusion`：维持 fail-closed 缺省。

## 5. 红线自检（对照 STYLE_NOTES §7）

1. 浮点显式 `.0`：全部 float 字面量带小数点（`1.0e-10` 科学计数合法）；
   整数语境仅限 `const int`、循环索引、`textureSize(..., 0)`、数组尺寸。
2. 循环上限：`const int ADR_TAPS = 12;`，`for (int i = 0; i < ADR_TAPS; i++)`。
3. 无理由不 clamp(0,1)：全文件唯一 `clamp` 在 `hsv2rgb` 色度重建内部
   （算法必需的归一化斜坡，HDR 值 v 在其后以乘子方式恢复），已加注释说明。
4. UV 安全：偏移 = 单位向量 × 像素半径 × `1.0/textureSize`；
   `max(sum_w, 0.001)`、`max(2.0, radius_pix)` 分母/退化下限守护。
5. 4K 性能：13 tap、1 次 `pow`（强度曲线）、0 次 sin/cos/exp/log、
   `process` 首行早退出（`strength < 0.001`）。
6. 无品牌商标名（名称/Label/描述/正文均扫描）。
7. 只引用引擎注入的 `u_input_tex`（+ `textureSize`），参数一律裸名，
   无未声明 `u_*` 标识符。
8. 头部包裹行齐全，元数据全部位于闭合 `// ----` 行之前；正文注释不含
   `// @xxx` 指令形态、不含 5 连字符行（避免解析串扰）。

## 6. i18n 键

片段文件 `docs/plugins/2026-09-28/i18n/Halation_Extended.json`
（顶层 `zh_CN` / `en_US`，合并时并入 `src/shared/i18n/locales/{zh,en}.json`
顶层 `plugins` 对象）：

```
plugins.Halation_Extended.name
plugins.Halation_Extended.params.strength
plugins.Halation_Extended.params.radius
plugins.Halation_Extended.params.hue_shift
plugins.Halation_Extended.params.threshold
plugins.Halation_Extended.desc.short        # 前向约定命名空间，解析器暂不查询
```

`.tglsl` 内英文 Label 为兜底文案（`I18N.tr(key, fallback)`），键缺失时
英文直读。其余 8 个 locale 可后补。

## 7. 验证计划

`tools/validate_Halation_Extended.py`：**headless**（仅 stdlib，不导入
PyQt/引擎模块），用与 `src/features/color/core/plugin_manager.py` 相同的
正则逐项镜像解析，另加红线静态扫描：

- 包裹行 / 元数据位置 / 指令不泄漏到正文；
- @name/@i18n_name(=stem)/@author/@version/@description；
- 4 参数类型、默认值、min≤default≤max、与 i18n en_US Label 一致；
- capability 键值（input/output space = scene-linear 等）；
- 着色器代码切分非空（首个 `//\s*-{5,}` 之后）；
- 红线：早退出、循环上限 const int、裸整数字面量扫描（含科学计数屏蔽
  与 const/for/textureSize/数组构造语境豁免）、clamp 仅出现在 hsv2rgb 内、
  `u_*` 白名单、delta 输出模式、分母守护、商标扫描、括号配平、tap 计数；
- i18n 片段：JSON 合法、双语键完整覆盖参数集。

退出码 0 = 全部通过。运行方式：
`python3 "docs/plugins/2026-09-28/tools/validate_Halation_Extended.py"`。

---

## 附录：headless 验证输出

运行：`python3 docs/plugins/2026-09-28/tools/validate_Halation_Extended.py`（退出码 0）

```
validate_Halation_Extended  (plugin: plugins/Halation_Extended.tglsl)
------------------------------------------------------------------------
[PASS] A1       metadata wrapper start line present
[PASS] A2       metadata wrapper closing line present
[PASS] A3       no metadata-style directives leak into shader body
[PASS] A4       all @directives located before the closing wrapper line
[PASS] B1       @name parsed (Halation Extended)
[PASS] B2       @i18n_name parsed and equals filename stem
[PASS] B3       @author parsed (Topos Team)
[PASS] B4       @version parsed (1.0)
[PASS] B5       @description parsed (single line)
[PASS] B6-strength strength: slider has default/min/max
[PASS] B6-radius radius: slider has default/min/max
[PASS] B6-hue_shift hue_shift: slider has default/min/max
[PASS] B6-threshold threshold: slider has default/min/max
[PASS] B6       @param count == 4 (separator correctly ignored)
[PASS] B7-strength param strength: type/label/default/min/max match spec table
[PASS] B8-strength param strength: min <= default <= max
[PASS] B7-radius param radius: type/label/default/min/max match spec table
[PASS] B8-radius param radius: min <= default <= max
[PASS] B7-hue_shift param hue_shift: type/label/default/min/max match spec table
[PASS] B8-hue_shift param hue_shift: min <= default <= max
[PASS] B7-threshold param threshold: type/label/default/min/max match spec table
[PASS] B8-threshold param threshold: min <= default <= max
[PASS] B9       @input_space scene-linear
[PASS] B10      @output_space scene-linear
[PASS] B11      @determinism deterministic
[PASS] C1       process entry signature present
[PASS] C2       exactly one process definition
[PASS] C3       process body brace-matched
[PASS] C4       early-exit guard at the top of process
[PASS] C5       loop bound 'ADR_TAPS' is a const int
[PASS] C6       no bare integer literals in float contexts (found: [])
[PASS] C7       clamp() only inside hsv2rgb chroma reconstruction (no HDR range clamps)
[PASS] C8       u_* identifiers within engine whitelist (found extra: [])
[PASS] C9       delta output pattern: return color + (result - original_color)
[PASS] C10      weight-sum denominator guarded (max(sum_w, 0.001))
[PASS] C11      radius degeneracy guarded (max(2.0, ...))
[PASS] C12      no brand/trademark names
[PASS] C13      braces and parentheses balanced
[PASS] C14      texture() call sites within budget (found 2 static sites; the ADR loop expands to 13 fetches/pixel at runtime)
[PASS] C15-rgb2hsv helper 'vec3 rgb2hsv' defined before process
[PASS] C15-hsv2rgb helper 'vec3 hsv2rgb' defined before process
[PASS] C15-highlightMask helper 'float highlightMask' defined before process
[PASS] C16      highlight mask uses soft knee (smoothstep), not hard cutoff
[PASS] C17      HSV tint: hue anchored + user offset
[PASS] D0       i18n fragment is valid JSON
[PASS] D1-zh_CN zh_CN: plugins.Halation_Extended.name present
[PASS] D2-zh_CN zh_CN: params cover declared set (missing: [])
[PASS] D3-zh_CN zh_CN: no stray param keys (extra: [])
[PASS] D1-en_US en_US: plugins.Halation_Extended.name present
[PASS] D2-en_US en_US: params cover declared set (missing: [])
[PASS] D3-en_US en_US: no stray param keys (extra: [])
[PASS] D4-strength en_US label for strength matches .tglsl fallback label
[PASS] D4-radius en_US label for radius matches .tglsl fallback label
[PASS] D4-hue_shift en_US label for hue_shift matches .tglsl fallback label
[PASS] D4-threshold en_US label for threshold matches .tglsl fallback label
------------------------------------------------------------------------
55/55 checks passed, 0 failed
```

补充（超出 headless 脚本范围的实机验证，2026-09-28）：着色器正文在
ModernGL standalone GL 4.1 core 上下文（ATI-6.1.13，引擎同款后端）中，
以裸名 uniform 注入与模拟引擎 `u_P0_*` 词边界改写两条路径分别编译，
均 `FRAGMENT COMPILE: OK`（含 const 数组构造与循环内动态索引）。
