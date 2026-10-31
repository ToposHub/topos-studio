# 规格：Bleach_Bypass（漂白旁路）

- 交付物：`plugins/Bleach_Bypass.tglsl`
- 计划：计划03（GLSL 插件库扩充），2026-09-28
- 版本：1.0 ／ 作者：Topos Team
- 依据：`docs/PLUGIN_DEV_GUIDE.md`、`docs/plugins/2026-09-28/STYLE_NOTES.md`、
  `src/features/color/core/plugin_manager.py` 解析实现、
  `plugins/bilateral_skin_smooth.tglsl`（结构与性能参照）

---

## 1. 目标与视觉签名

漂白旁路（bleach bypass / skip bleach）是胶片冲洗时跳过漂白步骤、保留银盐
颗粒的工艺，画面呈现：

1. **高对比**：阴影深挖下压，高光泛银提亮，中间调绕 18% 灰锚点展开；
2. **低饱和**：彩色信息被无光的银层稀释，只剩残余色度；
3. **银盐高光**：高光区趋向无彩的银白，而非彩色溢出。

数字近似（经典做法）：把 luma 与原图做 screen/overlay 类混合 + 饱和度控制。

## 2. 算法推导（HDR 安全的 bleach bypass 近似）

输入 `color` 为上游调色后的 scene-linear 像素，可 `< 0.0`、可 `>> 1.0`。

### 2.1 luma

```
lum = dot(color, vec3(0.2126, 0.7152, 0.0722))     // Rec.709，不做 clamp
```

### 2.2 overlay 侧：锚点 S 曲线（银版）

经典 overlay(image, luma) 在 scene-linear 域的对应物是以 18% 灰为锚点的
pivot contrast（对数斜率）：

```
slope  = mix(1.0, 2.0, contrast)                    // 1 = 恒等
silver = PIVOT * signedPow(lum / PIVOT, slope)      // PIVOT = 0.18
signedPow(x, e) = sign(x) * pow(max(abs(x), 1e-6), e)
```

- 锚在 pivot → 中间调曝光不动，暗部按幂律下压、亮部扩张；
- `signedPow` 保证负 luma（超出色域）仍为负、HDR luma（>>1）持续增长，
  **不折叠、不 clamp**；`1e-6` 是 pow 定义域防 NaN 守护（`sign(0)=0`
  使黑点输出恰好为 0）。

### 2.3 screen 侧：有界银色提亮

screen(a,b) = a+b−a·b 只在 [0,1]² 上行为正确（a、b 同时 >>1 时坍缩为负），
因此 **screen 的混合输入做软压缩有界化，信号通路本身不 clamp**：

```
unit  = clamp(lum / (1.0 + max(lum, 0.0)), 0.0, 1.0)   // 混合键（权重域）
lift  = unit * unit * 0.25                              // 高光银色提亮
```

- `unit` 是**权重/混合键**而非颜色信号：权重域 clamp 合法（STYLE_NOTES
  红线 3 的允许情形），负 luma 映射到 0 → 黑位以下不产生提亮；
- `lift` 以加法进入信号（`silver + lift`），对 HDR 输入保持单调、有上界
  0.25，模拟银盐高光的泛白。

### 2.4 饱和度控制（色度保留）

```
chroma    = color - vec3(lum)
bypassed  = vec3(silver + lift) + chroma * saturation
```

- `saturation = 1.0` 保留原始色度（对比来自银版），`0.0` 完全收敛到银灰；
- 色度按通道偏移保留，负值与 HDR 值原样通过。

### 2.5 强度混合

```
amt = 1.0 - pow(1.0 - strength, 1.5)    // 滑杆手感曲线（同 bilateral）
return mix(color, bypassed, amt)
```

`strength = 0` 严格恒等（且函数首行 `strength < 0.001` 早退出）。

### 2.6 关键决策记录

| 决策 | 理由 |
|---|---|
| 不做字面 per-channel overlay/screen | 两者分支条件基于通道值，c>1 时 screen 分支线性外推斜率为负、HDR 折叠；故分解为「锚点 S 曲线（overlay 侧）+ 有界提亮（screen 侧）」，两者对 HDR 单调 |
| 纯点操作、0 纹理采样 | bleach bypass 无空间成分；无循环、无 `u_*` 引用，天然 deterministic、可 LUT 化 |
| 不走 delta 输出模式 | delta 模式（`return color + (result - original)`）是 `u_input_tex` 空间采样类插件的惯例；本插件直接作用于已调色 `color`，与 warm_tint 同类 |
| 参数默认 0.6 / 0.4 / 0.35 | 默认下：18% 灰 +2.4%、显示白 ×1.78、10% 灰 −14%、强去饱和——"刚添加即有可见但不过火" |

## 3. 参数 ABI

| var | 类型 | Label（英文兜底） | default | min | max | zh_CN |
|---|---|---|---|---|---|---|
| `strength` | slider | Strength | 0.6 | 0.0 | 1.0 | 强度 |
| `contrast` | slider | Contrast | 0.4 | 0.0 | 1.0 | 对比度 |
| `saturation` | slider | Saturation Retention | 0.35 | 0.0 | 1.0 | 饱和保留 |

`@i18n_name Bleach_Bypass` ＝ 文件名 stem（与 HighPass_Retouch 先例一致），
locale 键：`plugins.Bleach_Bypass.name` / `.params.<var>`。

## 4. Capability Manifest

```
@input_space scene-linear      @output_space scene-linear
@alpha premultiplied           @precision float16
@determinism deterministic     @export_support all
@threading gpu                 @graphics_backends opengl
@compute_backends glsl_fragment
@preview_cost low
```

`export_support all`：纯点操作、确定性，video/LUT 导出路径均可用（LUT 在其
有界定义域内成立）。

## 5. 性能预算（4K @ 60FPS）

- 每像素：1×dot、2×pow（signedPow + 强度曲线）、少量 mix/clamp/max/sign；
- 0 纹理 tap、0 循环、0 分支发散（唯一分支为首行早退出）；
- 声明 `@preview_cost low`，播放降级管理器不会牺牲本效果。

## 6. 数值行为摘要（默认参数，来自验证模型）

| 输入（scene-linear） | 输出 luma | 说明 |
|---|---|---|
| 0.0（黑） | 0.0 | 黑位保持 |
| 0.10 | 0.086 | 阴影下压 −14% |
| 0.18（中灰锚点） | 0.184 | 锚点几乎不动（+2.4%） |
| 1.0（显示白） | 1.783 | 高光扩张 ×1.78 |
| 10.0（HDR） | 39.94 | HDR 持续增长，无 clamp 折叠 |
| −0.05（超出色域） | −0.035 | 负值保持为负，无 NaN |

## 7. 交付物

| 文件 | 内容 |
|---|---|
| `plugins/Bleach_Bypass.tglsl` | 插件实现（~110 行，含高密度头部注释） |
| `docs/plugins/2026-09-28/specs/Bleach_Bypass.md` | 本规格 |
| `docs/plugins/2026-09-28/tools/validate_Bleach_Bypass.py` | headless 解析 + 数值模型验证 |
| `docs/plugins/2026-09-28/i18n/Bleach_Bypass.json` | zh_CN + en_US 片段（供编排方合并进 locale） |
| `docs/plugins/2026-09-28/cards/Bleach_Bypass.md` | 效果卡片 |

## 8. STYLE_NOTES 自检清单

- [x] 头部含 `// --- Topos Plugin Metadata ---` 与 `// -----------------------------`
- [x] `@name` / `@i18n_name`（= 文件名 stem）/ `@author Topos Team` / `@version` / `@description`
- [x] 色彩类声明 `@input_space scene-linear` + `@output_space scene-linear`
- [x] 每个参数类型正确、Label 带引号且可直读英文、default/min/max 完整
- [x] 所有 float 字面量带 `.0`（唯一整型语境 `1e-6` 为科学计数浮点）
- [x] 无未声明 `u_*` 引用；唯一 clamp 位于权重域 `unit`（有注释理由）
- [x] 分母有下限守护（`max(lum, 0.0)`、`max(abs(x), 1e-6)`）
- [x] 首行早退出；pow×2、纹理 tap×0；`@preview_cost low`
- [x] 无品牌商标名
- [x] zh_CN/en_US 片段含 `plugins.Bleach_Bypass.name` 与 3 个 `.params.<var>`

---

## 9. 验证输出（validate_Bleach_Bypass.py，2026-09-28）

```
[PASS] @name present
[PASS] @i18n_name == plugin_id (Bleach_Bypass)
[PASS] @author == Topos Team
[PASS] @version present
[PASS] @description present
[PASS] @preview_cost declared (low)
[PASS] color plugin declares scene-linear in/out -- caps={'input_space': 'scene-linear', 'output_space': 'scene-linear', 'alpha': 'premultiplied', 'precision': 'float16', 'determinism': 'deterministic', 'export_support': 'all', 'threading': 'gpu', 'graphics_backends': 'opengl', 'compute_backends': 'glsl_fragment'}
[PASS] 3 slider params parsed -- ['strength', 'contrast', 'saturation']
[PASS] param strength ABI intact -- {'type': 'slider', 'var': 'strength', 'label': 'Strength', 'default': 0.6, 'min': 0.0, 'max': 1.0}
[PASS] param contrast ABI intact -- {'type': 'slider', 'var': 'contrast', 'label': 'Contrast', 'default': 0.4, 'min': 0.0, 'max': 1.0}
[PASS] param saturation ABI intact -- {'type': 'slider', 'var': 'saturation', 'label': 'Saturation Retention', 'default': 0.35, 'min': 0.0, 'max': 1.0}
[PASS] metadata wrapper close line (`-{5,}`) present
[PASS] no metadata directives leak into shader code -- leak=None
[PASS] process() entry point present
[PASS] no bare int literals in shader code -- found=[]
[PASS] no u_* uniform references in shader code -- found=[]
[PASS] no for-loops (pure point op)
[PASS] clamp audit: single clamp on blend key `unit` only -- sites=['float unit = clamp(lum / (1.0 + max(lum, 0.0)), 0.0, 1.0);']
[PASS] first-line early exit at strength ~ 0
[PASS] no brand/trademark names -- found=[]
[PASS] perf budget: <=3 pow calls, 0 texture taps -- pow=2, taps=0
[PASS] i18n zh_CN: name + 3 param labels -- name=漂白旁路
[PASS] i18n en_US: name + 3 param labels -- name=Bleach Bypass
[PASS] model: strength=0 is exact identity
[PASS] model: black stays black
[PASS] model: output luma monotone over [-0.2, 20] ramp -- outs=['-0.2064', '-0.03502', '0', '0.00216', '0.01134', '0.03545', '0.08589', '0.1843', '0.4422', '0.9035', '1.783', '3.063', '4.503', '11.46', '29.43', '103.6']
[PASS] model: 18% grey pivot anchored -- out=0.1843
[PASS] model: HDR (10.0) grows, never clamped -- out=39.942
[PASS] model: sub-black stays negative -- out=-0.0350
[PASS] model: no NaN/Inf over 15k+ evaluations -- bad=0
[PASS] model: saturation 1.0 keeps chroma / 0.0 fully silver -- spread1=0.850, spread0=5.55e-17

RESULT: PASS - Bleach_Bypass.tglsl satisfies spec + STYLE_NOTES red lines
```
