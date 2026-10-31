# Film_Grain（胶片颗粒）插件规格

- 仓库：Topos Color（计划03 · GLSL 插件库扩充）
- 交付日期：2026-09-28
- 文件：`plugins/Film_Grain.tglsl`（plugin_id = `Film_Grain`）
- 版本：1.0 · 作者：Topos Team

---

## 1. 用途

模拟胶片感光乳剂的颗粒质感：在 scene-linear 画面上叠加单色颗粒噪声，
噪声幅度按像素亮度做"中间调峰值"加权，并允许用户把颗粒往阴影区/高光区
再分配。典型用途：

- 数字画面"去数字感"，为照片/视频增加胶片材质；
- 调色链末端做质感统一（隐藏条带、弱化Posterize痕迹）；
- 与 warm_tint、Film_Halation 组合构建完整胶片风格。

## 2. 算法概述

1. **格子量化（分辨率无关）**：`gp = uv * u_tex_size / max(grain_size, 1.0)`，
   颗粒尺寸单位为输出像素（即采样步长），4K 与 1080p 观感一致。
2. **hash value noise（4-tap 平滑）**：Dave Hoskins 无三角函数 hash
   （`fract(x*0.1031)` 系），格子四角 hash 后 smoothstep 淡入淡出双线性
   插值，噪声重映射到 `[-1, 1]`。零纹理采样、无 sin/cos/pow。
3. **亮度加权**：`l = clamp(dot(color, Rec.709 luma), 0.0, 1.0)`（clamp
   仅作用于权重，不碰颜色），权重
   `w = 4l(1-l) + (1-l)² · shadow_bias + l² · highlight_bias`。
4. **加性输出**：`color + vec3(noise · strength · 0.12 · w)`，全程不
   clamp 基色，HDR 安全（负值/超白输入线性穿透）。

## 3. 参数表

| var | 控件 | Label（英文兜底） | 默认 | 范围 | i18n key | 说明 |
|---|---|---|---|---|---|---|
| `strength` | slider | Strength | 0.35 | 0.0–1.0 | `plugins.Film_Grain.params.strength` | 颗粒总强度；峰值幅度 = strength × 0.12（scene-linear）；< 0.001 早退出 |
| `grain_size` | slider | Grain Size | 1.5 | 1.0–8.0 | `plugins.Film_Grain.params.grain_size` | 颗粒尺寸（输出像素），决定格子步长；1.0 ≈ 逐像素白噪声，越大颗粒团簇越粗 |
| `shadow_bias` | slider | Shadow Bias | 0.25 | 0.0–1.0 | `plugins.Film_Grain.params.shadow_bias` | 阴影偏置：往暗部追加 `(1-l)²` 加权颗粒，模拟欠曝冲洗/数字噪点风格 |
| `highlight_bias` | slider | Highlight Bias | 0.15 | 0.0–1.0 | `plugins.Film_Grain.params.highlight_bias` | 高光偏置：往亮部追加 `l²` 加权颗粒，模拟过曝乳剂堆积 |

名称 i18n：`plugins.Film_Grain.name`（`@i18n_name Film_Grain`，与文件名
stem 一致，键大小写敏感）。

## 4. 能力声明（capability manifest）

| 键 | 值 | 理由 |
|---|---|---|
| `@input_space` / `@output_space` | `scene-linear` | 色彩类必写；加性增量在 linear 域定义 |
| `@precision` | `float16` | 纯 ALU 噪声，半精度足够 |
| `@determinism` | `deterministic` | 无时间输入、无纹理采样，同帧同输入结果恒定 |
| `@export_support` | `video` | 依赖像素位置（uv），可随视频渲染导出；不能导出 LUT |
| `@occlusion` | `background_independent` | 只读上游 `color` 与 `uv`/`u_tex_size`（不读下层背景内容），颗粒不扩出画布 |

未声明 `@preview_cost`：4 次 hash + ~30 ALU，远轻于 bilateral（32 tap），
无需播放降级。

## 5. 已知限制

1. **静态颗粒**：引擎未注入时间 uniform，颗粒图案逐帧不变——照片无感，
   视频播放时呈固定纹理（非动态"跳动"颗粒）。引入时域抖动需引擎侧
   新增 `u_time` 注入，属后续计划。
2. **亮度加权的域近似**：真实乳剂密度响应定义在 log 曝光域，本插件用
   scene-linear 亮度近似（clamp 到 0–1 调制），在极端 HDR 区权重饱和
   于两端偏置值，属有意的简化。
3. **单色颗粒**：对 RGB 三通道加同一增量（黑白乳剂模型），不做染料
   云彩色颗粒。
4. **格子对齐**：颗粒格子与输出像素网格对齐，转分辨率/缩放导出时
   观感会随 grain_size 的像素语义等比缩放（分辨率无关指"相同像素数"，
   非"相同画幅占比"）。

## 6. 验证

见下方「验证结果」——由 `docs/plugins/2026-09-28/tools/validate_Film_Grain.py`
headless 调用 `plugin_manager._parse_plugin_file`（`scan_plugins` 内部使用的
同一解析函数，纯 Python 依赖、只读仓库、只写 stdout）产出。运行日期
2026-09-28，退出码 0。

### 验证结果

```text
repo root : /Users/testuser/Documents/vscode/topos-color-multitask /topos-color01
plugin    : /Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/plugins/Film_Grain.tglsl
exists    : True
------------------------------------------------------------
[PASS] parse succeeds (non-None)
[PASS] name == 'Film Grain' -- 'Film Grain'
[PASS] author == 'Topos Team' -- 'Topos Team'
[PASS] version == '1.0' -- '1.0'
[PASS] description non-empty
[PASS] preview_cost undeclared (light effect)
[PASS] capability input_space == 'scene-linear' -- 'scene-linear'
[PASS] capability output_space == 'scene-linear' -- 'scene-linear'
[PASS] capability precision == 'float16' -- 'float16'
[PASS] capability determinism == 'deterministic' -- 'deterministic'
[PASS] capability export_support == 'video' -- 'video'
[PASS] capability occlusion == 'background_independent' -- 'background_independent'
[PASS] param count == 4 -- got 4
[PASS] param var set matches spec -- ['grain_size', 'highlight_bias', 'shadow_bias', 'strength']
[PASS] strength: type slider -- 'slider'
[PASS] strength: label fallback == 'Strength' -- 'Strength'
[PASS] strength: default == 0.35 -- 0.35
[PASS] strength: min == 0.0 -- 0.0
[PASS] strength: max == 1.0 -- 1.0
[PASS] strength: min <= default <= max
[PASS] grain_size: type slider -- 'slider'
[PASS] grain_size: label fallback == 'Grain Size' -- 'Grain Size'
[PASS] grain_size: default == 1.5 -- 1.5
[PASS] grain_size: min == 1.0 -- 1.0
[PASS] grain_size: max == 8.0 -- 8.0
[PASS] grain_size: min <= default <= max
[PASS] shadow_bias: type slider -- 'slider'
[PASS] shadow_bias: label fallback == 'Shadow Bias' -- 'Shadow Bias'
[PASS] shadow_bias: default == 0.25 -- 0.25
[PASS] shadow_bias: min == 0.0 -- 0.0
[PASS] shadow_bias: max == 1.0 -- 1.0
[PASS] shadow_bias: min <= default <= max
[PASS] highlight_bias: type slider -- 'slider'
[PASS] highlight_bias: label fallback == 'Highlight Bias' -- 'Highlight Bias'
[PASS] highlight_bias: default == 0.15 -- 0.15
[PASS] highlight_bias: min == 0.0 -- 0.0
[PASS] highlight_bias: max == 1.0 -- 1.0
[PASS] highlight_bias: min <= default <= max
[PASS] shader_code non-empty
[PASS] process entry point present
[PASS] content_hash is 64-hex
[PASS] no undeclared u_* uniforms (only u_tex_size) -- found: ['u_tex_size']
[PASS] no @param directive inside shader body
[PASS] exactly 4 @param directives in whole file
[PASS] metadata wrapped by // --- header line
[PASS] closing // ----------------------------- line present
[PASS] all @ directives before closing line
[PASS] no for/while loops
[PASS] no sin/cos/pow/exp/log builtins
[PASS] first-line early exit on strength
[PASS] return statement not clamped
[PASS] no bare int in float ops (x * 2 style)
------------------------------------------------------------
RESULT: PASS (all checks passed)
EXIT=0
```

