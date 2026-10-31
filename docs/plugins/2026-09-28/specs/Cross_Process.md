# 规格：Cross_Process（交叉冲洗）

- 交付物：`plugins/Cross_Process.tglsl`
- 计划：计划03（GLSL 插件库扩充），2026-09-28
- 版本：1.0 ／ 作者：Topos Team
- 依据：`docs/PLUGIN_DEV_GUIDE.md`、`docs/plugins/2026-09-28/STYLE_NOTES.md`、
  `src/features/color/core/plugin_manager.py` 解析实现、
  `plugins/Bleach_Bypass.tglsl`（点操作色彩插件参照）、
  `plugins/Color_Space_Transform.tglsl`（select 分发惯例参照）

---

## 1. 目标与视觉签名

交叉冲洗（cross process，反转片走负片药水或反之）的核心观感是
**三通道密度曲线交叉**：阴影与高光向相反色相漂移，中间调仍可辨认，
并有染料层间的再曝光"雾"串扰。三个预设：

1. **Classic**：青蓝阴影 × 黄绿高光（原型负冲观感）；
2. **Green Shift**：病感绿罩染中间调与高光，R/B 两端压暗；
3. **Warm**：琥珀色阴影与高光，蓝色两端压扁漂白。

数字近似：每预设一组**有理函数**通道曲线（非查表）+ 小权重加性
染料雾交叉混合 + 感知强度插值。

## 2. 算法推导（HDR 安全的交叉曲线族）

输入 `color` 为上游调色后的 scene-linear 像素，可 `< 0.0`、可 `>> 1.0`。
本插件纯点操作，不采样纹理，因此不走 delta 输出模式（同
Bleach_Bypass / warm_tint 一类，直接作用于已调色 `color`）。

### 2.1 有理通道曲线核

```
f(x) = x * (a + m*x*x) / (1.0 + v*x*x)        a, m, v > 0
```

- `f(0) = 0` 精确成立：黑位保持纯黑，**暗部染色来自各通道黑端增益
  `f'(0) = a` 的差异**（这正是"曲线交叉"的暗部半边）；
- 通道增益从黑端 `a` 平滑过渡到渐近增益 `m/v`，过渡中心约
  `x ~ 1/sqrt(v)`：**高光染色来自各通道渐近斜率差异**（交叉的亮部半边）；
- 全实轴性质：
  - 分母 `1 + v*x*x >= 1` 恒正 → **无极点、无除零**，任意输入无 NaN 路径；
  - `x -> ±∞` 时 `f(x) ~ (m/v)*x` → **渐近线性**，HDR 持续增长不折叠；
  - `f` 为奇函数次序保持：负输入输出为负，单调不 clamp。

### 2.2 单调性证书（可证，非仅数值验证）

```
f'(x) * (1 + v*x*x)^2 = a + (3m - a*v)*x^2 + m*v*x^4   （令 y = x^2）
                      = a + (3m - a*v)*y + m*v*y^2 =: q(y)
```

`q(y)` 是 `y` 的开口向上二次式，`q(y) >= 0` 对所有 `y >= 0` 成立
（即 `f` 在全实轴单调递增）当且仅当 `a > 0, m > 0, v > 0` 且
`a*v <= 9*m`（`q` 的判别式条件 `(av−m)(av−9m) <= 0` 与 `q(0)=a>0`
联立化简所得）。9 条预设曲线全部满足且余量大（最大 av/9m ≈ 0.34），
验证器对**从 .tglsl 抽取的系数**逐条断言。

### 2.3 染料雾交叉混合（G→R 泄漏等）

显影时某通道染料密度再曝光另一通道，实现为小权重加性雾：

```
out = curved + vec3(dot(curved, TO_R), dot(curved, TO_G), dot(curved, TO_B))
```

- `TO_*` 为非负小权重（≤ 0.12）、对角为 0（通道不自雾）；
- 签名项：Classic 的 G→R 0.12——纯绿画面 `G=0.5` 渗出
  `R ≈ 0.059`，绿色富集区漂黄绿而非纯绿；
- 加性雾对 HDR 保持线性（渐近仍一次增长，不爆炸不折叠）；对负值
  仅加深（非负权重 × 负信号 ≤ 0），**sub-black 保持负**。

### 2.4 强度插值

```
amt = 1.0 - pow(1.0 - strength, 1.5)    // 感知滑杆曲线（同 bilateral）
return mix(color, crossed, amt)
```

`strength = 0` 严格恒等（函数首行 `strength < 0.001` 早退出）。

### 2.5 预设系数表（写死在 .tglsl，验证器抽取校验）

| 预设 | 通道 | a（黑端增益） | m | v | 渐近 m/v | av ≤ 9m |
|---|---|---|---|---|---|---|
| Classic | R | 0.85 | 4.40 | 4.00 | 1.10 | 3.40 ≤ 39.6 |
| Classic | G | 0.96 | 2.295 | 2.25 | 1.02 | 2.16 ≤ 20.7 |
| Classic | B | 1.32 | 4.51 | 5.50 | 0.82 | 7.26 ≤ 40.6 |
| Green Shift | R | 0.90 | 3.325 | 3.50 | 0.95 | 3.15 ≤ 29.9 |
| Green Shift | G | 1.08 | 2.016 | 1.80 | 1.12 | 1.94 ≤ 18.1 |
| Green Shift | B | 0.78 | 3.528 | 4.20 | 0.84 | 3.28 ≤ 31.8 |
| Warm | R | 1.14 | 2.86 | 2.60 | 1.10 | 2.96 ≤ 25.7 |
| Warm | G | 0.92 | 3.168 | 3.20 | 0.99 | 2.94 ≤ 28.5 |
| Warm | B | 0.68 | 2.812 | 3.80 | 0.74 | 2.58 ≤ 25.3 |

雾权重（行 = 目标通道，列 = 来源 R/G/B）：

| 预设 | TO_R | TO_G | TO_B |
|---|---|---|---|
| Classic | (0, 0.12, 0.03) | (0.02, 0, 0.06) | (0, 0.05, 0) |
| Green Shift | (0, 0.08, 0) | (0.02, 0, 0.04) | (0, 0.10, 0) |
| Warm | (0, 0.06, 0.05) | (0.05, 0, 0.02) | (0, 0.02, 0) |

### 2.6 关键决策记录

| 决策 | 理由 |
|---|---|
| 有理曲线而非 log 域 S 曲线 | 任务要求有理/多项式近似且免查表；`mul/add/div` 实现，比任何 pow 型 S 曲线便宜；分母恒 ≥ 1 直接给出无极点 + 无除零的数值安全 |
| 曲线过原点（f(0)=0）而非带黑场抬升 | 黑位严格保持，暗部染色由通道黑端增益差表达；黑场测试可写成精确断言 |
| 雾用加性而非乘性 screen | 乘性 `f_R·(1+λ·f_G)` 在 HDR 上二次增长会爆炸；加性保持渐近一次、负值保负 |
| 全文件 0 次 clamp | 曲线族全实轴单调 + 渐近线性，无任何有界化需求（对比 Bleach_Bypass 需要 1 处权重域 clamp） |
| preset 用 select + if/else 兜底链分发 | STYLE_NOTES §2④ 惯例（Color_Space_Transform / Radial_Blur_Pro 同款）；int 索引比较是允许的整数语境 |
| 不走 delta 输出模式 | delta 模式是 `u_input_tex` 空间采样类插件的惯例；本插件零采样，直接作用于 `color` |
| 参数默认 preset=Classic / strength=0.7 | 默认即原型负冲观感、可见但不过火（同批强度默认 0.35–0.8 惯例） |

## 3. 参数 ABI

| var | 类型 | Label（英文兜底） | default | 选项/范围 | zh_CN |
|---|---|---|---|---|---|
| `preset` | select | Preset | 0（Classic） | "Classic" / "Green Shift" / "Warm" | 预设 |
| `strength` | slider | Strength | 0.7 | 0.0 – 1.0 | 强度 |

`@i18n_name Cross_Process` ＝ 文件名 stem（与 Bleach_Bypass /
HighPass_Retouch 先例一致），locale 键：`plugins.Cross_Process.name` /
`.params.<var>`。

## 4. Capability Manifest

```
@input_space scene-linear      @output_space scene-linear
@alpha premultiplied           @precision float16
@determinism deterministic     @export_support all
@threading gpu                 @graphics_backends opengl
@compute_backends glsl_fragment
@preview_cost low
```

`export_support all`：纯点操作、确定性，video/LUT 导出路径均可用。

## 5. 性能预算（4K @ 60FPS）

- 每像素：3 × 有理曲线（各 3 乘 1 除）+ 3 × dot + 1 × pow（强度曲线）
  + mix/比较若干；**0 纹理 tap、0 循环、0 clamp**；
- 分支：首行早退出 + 单个 select 索引分支（3 选 1，无发散压力）；
- 声明 `@preview_cost low`，播放降级管理器不会牺牲本效果。

## 6. 数值行为摘要（满强度，来自验证模型）

| 输入（scene-linear） | Classic | Green Shift | Warm |
|---|---|---|---|
| 0.0（黑） | (0, 0, 0) | (0, 0, 0) | (0, 0, 0) |
| 0.18（中灰） | (0.186, 0.190, 0.233) 青蓝 | (0.179, 0.204, 0.161) 绿 | (0.221, 0.179, 0.127) 琥珀 |
| 1.0（显示白） | (1.197, 1.076, 0.947) 黄绿 | (1.027, 1.158, 0.939) 绿 | (1.206, 1.043, 0.747) 暖 |
| 10.0（HDR） | (12.46, 10.91, 8.72) | (10.39, 11.72, 9.52) | (11.97, 10.60, 7.60) |
| −0.05（超出色域） | 全通道为负 | 全通道为负 | 全通道为负 |

灰 ramp 逐通道在 [−2, 20] 全程单调；36k+ 随机/对抗采样无 NaN/Inf。

## 7. 交付物

| 文件 | 内容 |
|---|---|
| `plugins/Cross_Process.tglsl` | 插件实现（~160 行，含证明级头部注释） |
| `docs/plugins/2026-09-28/specs/Cross_Process.md` | 本规格 |
| `docs/plugins/2026-09-28/tools/validate_Cross_Process.py` | headless 解析 + 系数抽取 + 数值模型验证 |
| `docs/plugins/2026-09-28/i18n/Cross_Process.json` | zh_CN + en_US 片段（供编排方合并进 locale） |
| `docs/plugins/2026-09-28/cards/Cross_Process.md` | 效果卡片 |

## 8. STYLE_NOTES 自检清单

- [x] 头部含 `// --- Topos Plugin Metadata ---` 与 `// -----------------------------`
- [x] `@name` / `@i18n_name`（= 文件名 stem）/ `@author Topos Team` / `@version` / `@description`
- [x] 色彩类声明 `@input_space scene-linear` + `@output_space scene-linear`
- [x] 每个参数类型正确（select 选项带引号单行、slider 三浮点全写）、Label 可直读英文
- [x] 所有 float 字面量带 `.0`；唯一整数语境为 select 索引比较（验证器豁免并单独核对）
- [x] 无未声明 `u_*` 引用；**全文件 0 次 clamp**（曲线全实轴单调，无需任何有界化）
- [x] 分母 `1 + v*x*x` 恒 ≥ 1，无除零守护需求；无径向/采样代码
- [x] 首行早退出；pow×1、纹理 tap×0、无循环；`@preview_cost low`
- [x] 无品牌商标名（注释避免独立成词的颜色名 "red" 等，防大小写不敏感误报）
- [x] zh_CN/en_US 片段含 `plugins.Cross_Process.name` 与 2 个 `.params.<var>` 及 desc 前向命名空间

---

## 9. 验证输出（validate_Cross_Process.py，2026-09-28）

```
[PASS] @name present
[PASS] @i18n_name == plugin_id (Cross_Process)
[PASS] @author == Topos Team
[PASS] @version present
[PASS] @description present
[PASS] @preview_cost declared (low)
[PASS] color plugin declares scene-linear in/out -- caps={'input_space': 'scene-linear', 'output_space': 'scene-linear', 'alpha': 'premultiplied', 'precision': 'float16', 'determinism': 'deterministic', 'export_support': 'all', 'threading': 'gpu', 'graphics_backends': 'opengl', 'compute_backends': 'glsl_fragment'}
[PASS] 2 params parsed (select + slider) -- ['preset', 'strength']
[PASS] param preset ABI intact (select) -- {'type': 'select', 'var': 'preset', 'label': 'Preset', 'default': 0, 'options': ['Classic', 'Green Shift', 'Warm']}
[PASS] preset options + default index -- options=['Classic', 'Green Shift', 'Warm'], default=0
[PASS] param strength ABI intact (slider) -- {'type': 'slider', 'var': 'strength', 'label': 'Strength', 'default': 0.7, 'min': 0.0, 'max': 1.0}
[PASS] metadata wrapper close line (`-{5,}`) present
[PASS] no metadata directives leak into shader code -- leak=None
[PASS] process() entry point present
[PASS] no bare int literals outside select-index compares -- found=[]
[PASS] no u_* uniform references in shader code -- found=[]
[PASS] no for-loops (pure point op)
[PASS] clamp audit: zero clamps anywhere (monotone extrapolating curves) -- sites=[]
[PASS] first-line early exit at strength ~ 0
[PASS] no brand/trademark names -- found=[]
[PASS] perf budget: exactly 1 pow (strength curve), 0 texture taps -- pow=1, taps=0
[PASS] select options <-> dispatch branches (0/1/else) same count + order
[PASS] 3 preset functions parsed with 3 curves + 3 fog vectors each -- found={'cross_classic': True, 'cross_green_shift': True, 'cross_warm': True}
[PASS] select options map onto preset functions in order -- [('Classic', 'cross_classic'), ('Green Shift', 'cross_green_shift'), ('Warm', 'cross_warm')]
[PASS] monotonicity certificate a*v <= 9*m holds for all 9 curves -- classic.x av=3.400 9m=39.6; classic.y av=2.160 9m=20.7; classic.z av=7.260 9m=40.6; green_shift.x av=3.150 9m=29.9; green_shift.y av=1.944 9m=18.1; green_shift.z av=3.276 9m=31.8; warm.x av=2.964 9m=25.7; warm.y av=2.944 9m=28.5; warm.z av=2.584 9m=25.3
[PASS] fog weights in [0, 0.15], zero diagonal -- max_weight=0.12
[PASS] i18n zh_CN: name + 2 param labels -- name=交叉冲洗
[PASS] i18n en_US: name + 2 param labels -- name=Cross Process
[PASS] model: strength=0 is exact identity (all presets)
[PASS] model: black stays black (all presets)
[PASS] model: per-channel monotone over [-2, 20] ramp (all presets) -- grey@0.18 -> classic:[0.186,0.190,0.233]@0.18; green_shift:[0.179,0.204,0.161]@0.18; warm:[0.221,0.179,0.127]@0.18
[PASS] model: preset hue signatures at grey/white -- classic@0.18:(0.186,0.190,0.233); classic@1.0:(1.197,1.076,0.947); green_shift@0.18:(0.179,0.204,0.161); green_shift@1.0:(1.027,1.158,0.939); warm@0.18:(0.221,0.179,0.127); warm@1.0:(1.206,1.043,0.747)
[PASS] model: G->R leak visible on pure green input -- R_out=0.0589 (from G=0.5, R=B=0)
[PASS] model: HDR (10.0) grows unclamped, stays linear-ish -- classic:(12.46,10.91,8.72); green_shift:(10.39,11.72,9.52); warm:(11.97,10.60,7.60)
[PASS] model: sub-black stays negative (all presets)
[PASS] model: no NaN/Inf over 36k+ evaluations -- bad=0
[PASS] model: strength interpolation monotone (all presets)

RESULT: PASS - Cross_Process.tglsl satisfies spec + STYLE_NOTES red lines
```
