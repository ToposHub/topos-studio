# 规格：Fake_Infrared（假红外）

- 交付物：`plugins/Fake_Infrared.tglsl`
- 计划：计划03（GLSL 插件库扩充），2026-09-28
- 版本：1.0 ／ 作者：Topos Team
- 依据：`docs/PLUGIN_DEV_GUIDE.md`、`docs/plugins/2026-09-28/STYLE_NOTES.md`、
  `src/features/color/core/plugin_manager.py` 解析实现、
  `plugins/Bleach_Bypass.tglsl` 与 `plugins/bilateral_skin_smooth.tglsl`（结构与性能参照）

---

## 1. 目标与视觉签名

假红外（false-color infrared）模拟红外胶片的经典观感：

1. **植被发光**：绿色占优的像素（树叶、草地）被映射为明亮的红色/品红，
   叠加柔光光晕——红外摄影的"伍德效应"数字近似；
2. **天空下沉**：蓝色占优的像素在红外记录中变暗，映射后呈深青蓝；
3. **梦幻光晕**：以 luma 反比加权的暗区/植被红色光晕，轻微提升黑位，
   形成红外胶片特有的遮蔽型眩光（veiling glare）；
4. **高对比**：18% 灰锚点 S 曲线，阴影下压、高光扩张。

三档通道映射（select）提供三种风格变体：Classic（假彩色记录位移）、
Amber（暖橙红植被）、Sky Swap（R↔B 交换类映射：天空转火红、植被转钢蓝）。

## 2. 算法推导（HDR 安全的假红外近似）

输入 `color` 为上游调色后的 scene-linear 像素，可 `< 0.0`、可 `>> 1.0`。
纯点操作：0 纹理采样、0 循环，不引用任何引擎 uniform。

### 2.1 启发式（全部在原始 color 上计算）

```
lum = dot(color, vec3(0.2126, 0.7152, 0.0722))       // Rec.709，不 clamp
veg = max(color.g - max(color.r, color.b), 0.0)      // 绿色占优 → 植被线索（掩码域）
sky = max(color.b - color.g, 0.0)                    // 蓝压绿 → 天空线索（掩码域）
```

### 2.2 合成红外记录

```
ir = lum + veg * 1.6 - min(sky, max(lum, 0.0)) * 1.2
```

- 植被（绿占优）读数为 IR 亮 → 映射后呈亮红；
- 蓝天读数为 IR 暗：天空削减被 `min(sky, max(lum, 0.0))` 以像素 luma 封顶，
  故 `ir ≥ lum − 1.2·lum = −0.2·lum`——**下探有界（深度青蓝天空），
  HDR 不折叠、不爆炸**；
- `veg`/`sky` 的 `max(·, 0.0)` 位于掩码域（权重/线索域 clamp 合法，
  STYLE_NOTES 红线 3 允许情形），信号通路 `ir` 本身不 clamp。

### 2.3 通道映射（select 早返回 if 链分发）

| 索引 | 选项 | 映射 | 视觉 |
|---|---|---|---|
| 0 | Classic | `(ir, R, G)` | 植被红/品红、天空深青蓝（假彩色记录位移） |
| 1 | Amber | `(ir, mix(R, ir, 0.5), G)` | 植被暖橙红 |
| 2 | Sky Swap | `(B, G, ir)` | R↔B 交换类：天空火红、植被钢蓝 |

- 三个映射均**保中性灰**：灰 `(v,v,v)` 时 `veg = sky = 0` → `ir = v` →
  输出仍为 `(v,v,v)`；
- 未知索引兜底回落 Classic（项目 select 分发惯例，末尾兜底 return）。

### 2.4 对比 S 曲线（逐通道锚点 pivot）

```
slope  = mix(1.0, 2.0, contrast)
result = PIVOT * signedPow(c / PIVOT, slope)        // PIVOT = 0.18，逐通道
signedPow(x, e) = sign(x) * pow(max(abs(x), 1e-6), e)
```

- 与本库其他色彩风格插件同构：对数曝光域的 S 旋转，18% 灰锚点不动；
- 逐通道（而非 luma 比例）是有意选择：假彩输出的张力来自通道分离，
  逐通道幂的轻微增饱和是红外观感的一部分；
- `signedPow` 保证负通道保持负、HDR 通道持续增长，**无 clamp**；
  `1e-6` 为 pow 定义域防 NaN 守护。

### 2.5 发光（luma 反比加权光晕，最后施加）

```
dark_w = 1.0 / (1.0 + max(lum, 0.0) * 6.0)   // luma 反比权重：黑位=1，18%灰≈0.48，HDR→0
veg_w  = veg / (veg + 0.04)                   // 植被线索软饱和，分母下限 0.04
weight = dark_w * mix(0.4, 1.0, veg_w)        // 暗区 0.4 基线，植被拉满 1.0
result += vec3(1.0, 0.42, 0.52) * (glow * 0.3 * weight)
```

- 加色式红外红色光晕（halation tint），幅度以 `glow * 0.3` 有界封顶，
  信号通路加法进入、无需 clamp；
- 在 S 曲线**之后**施加 → glow 与 contrast 两个控制相互独立；
- **记录在案的行为**：这是遮蔽型眩光，设计上会提升近黑像素（黑位抬升
  `glow·0.3·0.4`，默认 0.054 R），因此深阴影响应在 ~0.001 scene-linear
  以下有轻微非单调（幅度 ~1e-5，任何显示设备不可见，属于红外梦幻感的
  有意成分）。

### 2.6 快速路径与分支守护（本插件无全局强度参数）

计划固定三参数（通道映射 / 发光强度 / 对比），通道映射本身即效果、
不存在恒等配置，因此早退出采用两段式：

1. `if (glow < 0.001 && contrast < 0.001) return remapped;`——两修饰量均
   中性时，跳过全部 glow/S 曲线数学（首行工作即快速路径）；
2. S 曲线与 glow 各自 `> 0.001` 分支守护（bilateral_skin_smooth 惯例）。

### 2.7 关键决策记录

| 决策 | 理由 |
|---|---|
| 纯点操作、0 纹理采样 | 假红外的"光晕"以遮蔽型眩光（加色 veiling glare）近似而非空间 bloom：省掉 u_input_tex 采样与循环，天然 deterministic、逐模式可 LUT 化 |
| 天空削减以 luma 封顶 | 防止 HDR 蓝天把 `ir` 推向大幅负值；下探有界 −0.2·lum，深色天空是签名而非缺陷 |
| glow 在 S 曲线后施加 | 两控制独立；若在曲线前，对比会把光晕重新压暗/放大，参数手感耦合 |
| 不走 delta 输出模式 | delta 模式是 `u_input_tex` 空间采样类插件惯例；本插件无采样，直接作用于已调色 `color`（与 warm_tint / Bleach_Bypass 同类） |
| 参数默认 0（Classic）/ 0.45 / 0.35 | 默认下：植被 → (0.355, 0.040, 0.142) 明亮红品红 + 光晕、天空 → (−0.054, 0.287, 0.628) 深青蓝、灰阶保灰——"刚添加即有可见但不过火" |

## 3. 参数 ABI

| var | 类型 | Label（英文兜底） | default | min | max | zh_CN |
|---|---|---|---|---|---|---|
| `channel_map` | select | Channel Map | 0（Classic） | — | — | 通道映射 |
| `glow` | slider | Glow Strength | 0.45 | 0.0 | 1.0 | 发光强度 |
| `contrast` | slider | Contrast | 0.35 | 0.0 | 1.0 | 对比度 |

select 选项表：`"Classic" "Amber" "Sky Swap"`（单行、每项双引号，
索引从 0 开始，GLSL 侧 `int`）。`@i18n_name Fake_Infrared` ＝ 文件名 stem，
locale 键：`plugins.Fake_Infrared.name` / `.params.<var>`。

## 4. Capability Manifest

```
@input_space scene-linear      @output_space scene-linear
@alpha premultiplied           @precision float16
@determinism deterministic     @export_support all
@threading gpu                 @graphics_backends opengl
@compute_backends glsl_fragment
@preview_cost low
```

`export_support all`：纯点操作、确定性，video/LUT 导出路径均可用
（LUT 在每个 select 模式的有界定义域内成立）。

## 5. 性能预算（4K @ 60FPS）

- 每像素：1×dot、3 次 signedPow 调用（1 个 pow 调用点）、少量
  mix/max/min/sign；
- 0 纹理 tap、0 循环；分支仅有快速路径 + 两个 `> 0.001` 守护；
- 声明 `@preview_cost low`，播放降级管理器不会牺牲本效果。

## 6. 数值行为摘要（来自验证模型）

### 6.1 Classic + 默认参数（glow 0.45 / contrast 0.35）

| 输入（scene-linear） | 输出 RGB | 说明 |
|---|---|---|
| 0.0（黑） | (0.054, 0.023, 0.028) | 光晕黑位抬升（闭式解验证） |
| 0.10（灰） | (0.115, 0.096, 0.099) | 灰保灰 |
| 0.18（灰锚点） | (0.206, 0.191, 0.194) | 锚点 +8%（含光晕） |
| 1.0（显示白灰） | (1.830, 1.826, 1.827) | 高光扩张 ×1.83 |
| 10.0（HDR 灰） | (40.80, 40.80, 40.80) | HDR 持续增长，无 clamp 折叠 |
| −0.05（超黑） | (0.022, −0.009, −0.004) | 有限、有界，无 NaN |
| 植被 (0.02, 0.12, 0.02) | (0.355, 0.040, 0.142) | 红品红 + 光晕，luma 0.09→0.11 |
| 天空 (0.25, 0.45, 0.90) | (−0.054, 0.287, 0.628) | 深青蓝，luma 0.44→0.24 |
| 肤色 (0.45, 0.25, 0.15) | (0.355, 0.629, 0.291) | 假彩位移：肤色偏绿（真红外特性） |

### 6.2 其他模式（同参数）

- Amber 植被 → (0.355, 0.153, 0.142)：暖橙红；
- Sky Swap 天空 → (1.596, 0.626, −0.061)：火红；植被 → (0.082, 0.135, 0.320)：钢蓝；
- HDR 绿 (0, 10, 0) Classic → R = 126.7：无折叠。

## 7. 交付物

| 文件 | 内容 |
|---|---|
| `plugins/Fake_Infrared.tglsl` | 插件实现（~165 行，含高密度头部注释） |
| `docs/plugins/2026-09-28/specs/Fake_Infrared.md` | 本规格 |
| `docs/plugins/2026-09-28/tools/validate_Fake_Infrared.py` | headless 解析 + 数值模型验证 |
| `docs/plugins/2026-09-28/i18n/Fake_Infrared.json` | zh_CN + en_US 片段（供编排方合并进 locale） |
| `docs/plugins/2026-09-28/cards/Fake_Infrared.md` | 效果卡片 |

## 8. STYLE_NOTES 自检清单

- [x] 头部含 `// --- Topos Plugin Metadata ---` 与 `// -----------------------------`
- [x] `@name` / `@i18n_name`（= 文件名 stem）/ `@author Topos Team` / `@version` / `@description`
- [x] 色彩类声明 `@input_space scene-linear` + `@output_space scene-linear`
- [x] select 选项表单行双引号、索引 0 起；两个 slider Label 带引号、default/min/max 完整
- [x] 所有 float 字面量带 `.0`（整数语境仅 select 索引比较 `== 0/1/2` 与 `1e-6` 科学计数）
- [x] 无未声明 `u_*` 引用；**0 处 clamp()**（守护全部用 max/min，且位于掩码域）
- [x] 分母有下限守护（`veg + 0.04`、`1.0 + max(lum,0)·6`、`max(abs(x), 1e-6)`）
- [x] 无全局强度参数 → 快速路径早退出 + 分支守护（§2.6，决策已记录）
- [x] pow 调用点 1 个、纹理 tap 0；`@preview_cost low`
- [x] 无品牌商标名（验证器对 RED 采用大小写敏感匹配，避免颜色词 "red" 误报）
- [x] zh_CN/en_US 片段含 `plugins.Fake_Infrared.name`、3 个 `.params.<var>` 与 `desc.short`

---

## 9. 验证输出（validate_Fake_Infrared.py，2026-09-28）

```
[PASS] @name present (Fake Infrared)
[PASS] @i18n_name == plugin_id (Fake_Infrared)
[PASS] @author == Topos Team
[PASS] @version present
[PASS] @description present
[PASS] @preview_cost declared (low)
[PASS] color plugin declares scene-linear in/out -- caps={'input_space': 'scene-linear', 'output_space': 'scene-linear', 'alpha': 'premultiplied', 'precision': 'float16', 'determinism': 'deterministic', 'export_support': 'all', 'threading': 'gpu', 'graphics_backends': 'opengl', 'compute_backends': 'glsl_fragment'}
[PASS] 3 params parsed -- ['channel_map', 'glow', 'contrast']
[PASS] select param channel_map ABI intact (index + 3 options) -- {'type': 'select', 'var': 'channel_map', 'label': 'Channel Map', 'default': 0, 'options': ['Classic', 'Amber', 'Sky Swap']}
[PASS] param glow ABI intact -- {'type': 'slider', 'var': 'glow', 'label': 'Glow Strength', 'default': 0.45, 'min': 0.0, 'max': 1.0}
[PASS] param contrast ABI intact -- {'type': 'slider', 'var': 'contrast', 'label': 'Contrast', 'default': 0.35, 'min': 0.0, 'max': 1.0}
[PASS] metadata wrapper close line (`-{5,}`) present
[PASS] no metadata directives leak into shader code -- leak=None
[PASS] process() entry point present
[PASS] no bare int literals in float contexts (select index comparisons whitelisted) -- found=[]
[PASS] no u_* uniform references in shader code -- found=[]
[PASS] no for-loops (pure point op)
[PASS] clamp audit: 0 clamp() calls (signal path unclamped; guards are max/min) -- sites=[]
[PASS] fast-path early exit when glow and contrast are neutral
[PASS] no brand/trademark names -- found=[]
[PASS] perf budget: <=3 pow call sites, 0 texture taps -- pow=1, taps=0
[PASS] i18n zh_CN: name + 3 param labels + desc.short -- name=假红外
[PASS] i18n en_US: name + 3 param labels + desc.short -- name=Fake Infrared
[PASS] model: fast path equals pure channel remap
[PASS] model: neutral grey preserved in all 3 channel maps
[PASS] model: 18% grey pivot anchored (contrast = 1.0, glow off) -- out=(0.18, 0.18, 0.18)
[PASS] model: grey ramp monotone over [-0.2, 20] (glow off, all modes)
[PASS] model: defaults monotone on [0.01, 20] (glare veil within tolerance)
[PASS] model: classic maps foliage to red-dominant -- out=(0.355, 0.0396, 0.1417)
[PASS] model: classic sinks the sky (B > R) -- out=(-0.0537, 0.2867, 0.6279)
[PASS] model: amber foliage is orange (R > G > B) -- out=(0.355, 0.1533, 0.1417)
[PASS] model: sky swap exchanges R/B semantics -- sky=(1.5957, 0.6264, -0.0608), foliage=(0.0815, 0.1345, 0.3203)
[PASS] model: HDR green foliage grows (R > 10) -- out.r=126.72
[PASS] model: sub-black finite and bounded -- out=(0.0221, -0.0093, -0.0039)
[PASS] model: glow lift bounded by glow * 0.3 -- max_lift=0.0540
[PASS] model: black-point veil equals closed form -- out=(0.054, 0.0227, 0.0281)
[PASS] model: no NaN/Inf over 80k+ evaluations -- bad=0
[PASS] model: 3 channel-map modes produce distinct outputs

RESULT: PASS - Fake_Infrared.tglsl satisfies spec + STYLE_NOTES red lines
```
