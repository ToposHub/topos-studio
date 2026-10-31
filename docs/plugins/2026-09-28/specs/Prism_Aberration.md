# Prism_Aberration 插件规格（计划03）

> 状态：已实现（plugins/Prism_Aberration.tglsl）
> 依据：`docs/plugins/2026-09-28/STYLE_NOTES.md`、`plugins/Radial_Blur_Pro.tglsl`（同批风格参照）、`plugins/HighPass_Retouch.tglsl`（delta 输出参照）
> 日期：2026-09-28

---

## 1. 元信息

| 项 | 值 |
|---|---|
| plugin_id | `Prism_Aberration`（文件名 stem） |
| UI 显示名 | Prism Aberration（zh_CN：棱镜色散） |
| 作者 / 版本 | Topos Team / 1.0 |
| 类型 | 空间采样类（径向 RGB 色散 / chromatic aberration，升级版：分离轴可旋转） |
| capability | `@input_space scene-linear`、`@output_space scene-linear`、`@preview_cost low` |
| i18n 键 | `plugins.Prism_Aberration.name`、`plugins.Prism_Aberration.params.<var>`（片段见 `../i18n/Prism_Aberration.json`） |

## 2. 功能定位

升级版色散（棱镜色散）：R 与 B 通道沿"径向 + 可旋转"的分离轴做对称偏移重采样，
G 通道保持原位作参考通道，产生经典的红/蓝镶边。三档可调维度：

- **分离量**（separation）：通道偏移量上限，经 ease-in 曲线映射，0 时整体旁通；
- **角度**（rotation）：分离轴在径向基础上旋转——0° 为经典径向色散，
  90° 为切向扭转（twist），180° 为边缘极性互换（红/蓝镶边对调）；
- **边缘衰减**（edge_falloff）：偏移量随离中心距离的增长曲线——0 为全画面
  均匀，1 为强烈集中于最外角（默认 0.5 ≈ 距离平方）。

空间采样一律基于 `u_input_tex`（调色前基准）计算，经 delta 输出叠加到已调色的
`color` 上（HighPass_Retouch 惯例，保证上游调色正确叠加）。中心固定为画面中心
（0.5, 0.5）——参数面按任务约束保持 3 项，中心不设旋钮。

## 3. 算法设计

### 3.1 纵横比纠偏空间（红线 #4 落实）

所有径向几何在"像素比例空间" P 中计算：`p = vec2(uv.x * aspect, uv.y)`，
`aspect = max(u_aspect_ratio, 0.001)`，P 中 1 单位 = 画面高度。这保证：

- 径向距离是真正的圆形距离（不被 UV 拉伸成椭圆）；
- 旋转角是真正的几何角度（在拉伸的 UV 空间里旋转会随位置畸变）；
- 偏移量在不同纵横比画面下视觉一致。

采样前换回 UV 单位：`offset_uv = vec2(offset_p.x / aspect, offset_p.y)`。

### 3.2 分离轴与偏移量

对每个像素 `uv`：

```
radial    = vec2(uv.x * aspect, uv.y) - vec2(0.5 * aspect, 0.5)   // 径向向量（P 空间）
dist      = length(radial)
radial_dir= radial / max(dist, 0.0001)                            // 中心退化守护
R         = mat2(cosθ, sinθ, -sinθ, cosθ)                          // θ = rotation * π/180
sep_dir   = R * radial_dir                                        // 可旋转分离轴（P 空间）

max_radius  = length(vec2(0.5 * aspect, 0.5))                     // 中心→角距离（构造性上界）
dist01      = dist / max(max_radius, 0.0001)                      // ≤ 1.0（构造保证，无需 clamp）
exponent    = mix(0.0, 4.0, edge_falloff)
falloff_mask= pow(max(dist01, 0.0001), exponent)                  // 基数守护，防 pow(0,0) 未定义
eased       = pow(separation, 1.5)                                // ease-in 滑杆手感
offset_len  = eased * 0.015 * max(aspect, 1.0) * falloff_mask      // 较长边归一 → 分辨率无关
offset_uv   = vec2(sep_dir.x, sep_dir.y) * offset_len → 除 aspect 还原 UV
```

量级：`SEP_MAX_RATIO = 0.015`（较长边的 1.5%）。4K（3840×2160）下
separation=1 时远角偏移 ≈ 57.6px；默认 0.35 经 ease-in ≈ 12px（"刚添加即有
可见但不过火"，R↔B 总分离 ≈ 24px 且仅在远角达到）。

### 3.3 通道采样与输出契约

```glsl
vec3 dispersed = original_color;                                  // G 原位参考
dispersed.r = texture(u_input_tex, clamp(uv + offset_uv, min_uv, max_uv)).r;  // R 沿轴 +
dispersed.b = texture(u_input_tex, clamp(uv - offset_uv, min_uv, max_uv)).b;  // B 沿轴 -
return color + (dispersed - original_color);                      // delta 输出模式
```

其中 `original_color = texture(u_input_tex, uv).rgb`，
`min_uv/max_uv` 为半 texel 边界（clamp-to-edge 显式保险，
`half_texel = 0.5 / max(tex_size, vec2(1.0, 1.0))`）。每像素恰好 3 个纹理 tap
（参考 + R + B），无循环。

## 4. 约束落实（红线映射）

| 红线（STYLE_NOTES §7） | 落实 |
|---|---|
| 1 浮点显式 `.0` | 全部 float 字面量带小数点；唯一整数语境为 `textureSize(u_input_tex, 0)` 的 lod |
| 2 循环上限编译期常量 | **无任何 for 循环**（3-tap 直采），天然满足；验证脚本断言零循环 |
| 3 HDR 不无理由 clamp(0,1) | 全文件仅 2 处 clamp，均为 UV 几何钳制（半 texel 边界）；内容值（可负、可 >>1）经逐通道重采样与 delta 输出全程不 clamp |
| 4 UV 安全 | `clamp(suv, min_uv, max_uv)` clamp-to-edge；分母守护：`max(tex_size, vec2(1.0,1.0))`、`max(u_aspect_ratio, 0.001)`、`max(dist, 0.0001)`、`max(max_radius, 0.0001)`、`max(dist01, 0.0001)`；径向几何乘 `u_aspect_ratio` 纠偏（§3.1） |
| 5 4K 性能预算 | 首行早退出 `if (separation < 0.001) return color;`；纹理 tap = 3；昂贵函数精确配额 sin×1、cos×1（旋转必需）、pow×2（衰减曲线 + 强度 ease-in）、exp/log×0；`@preview_cost low` |
| 6 无品牌商标名 | 名称/Label/描述均为通用英文（Prism Aberration / Dispersion / Falloff） |
| 7 无未声明 `u_*` | 仅用内置 `u_input_tex`、`u_aspect_ratio`；分辨率经 `textureSize(u_input_tex, 0)` 获取，`u_tex_size` 未用 |
| 8 头部包裹行 | `// --- Topos Plugin Metadata ---` … `// -----------------------------`，元数据全部在包裹行之前 |

**关于 sin/cos 的说明**：旋转参数在数学上必需三角函数；用量压到每种恰 1 次
（每像素 2 次超越函数 + 2 次 pow），远低于 bilateral_skin_smooth 的 32-tap
两遍采样预算，属 `@preview_cost low` 档。

## 5. 参数表

| # | 类型 | var | Label（英文兜底） | 默认 | 范围 | GLSL 类型 |
|---|---|---|---|---|---|---|
| 1 | slider | `separation` | Dispersion Strength | 0.35 | 0.0 – 1.0 | float |
| 2 | slider | `rotation` | Rotation (Degrees) | 0.0 | -180.0 – 180.0 | float |
| 3 | slider | `edge_falloff` | Edge Falloff | 0.5 | 0.0 – 1.0 | float |

`@param separator`（解析 no-op）置于 rotation 与 edge_falloff 之间做视觉分组
（方向类参数 / 分布类参数）。

## 6. 性能预算

- 每像素纹理 tap = **3**（参考 + R + B）；对比基准：Radial_Blur_Pro 默认档
  16 tap、bilateral_skin_smooth 为 32 tap × 两遍 refine——本插件是最轻的空间
  采样类插件。
- 每像素超越函数：sin×1、cos×1、pow×2；其余为 mix/max/length/mat2 乘法。
- 4K（~800 万像素）60FPS 目标下余量充足，声明 `@preview_cost low`。
- 分辨率无关：`textureSize(u_input_tex, 0)` 实时取分辨率 + 偏移按较长边比例，
  预览与导出（含 4K→8K）效果一致。

## 7. 验收标准

1. 头部含两条包裹行，元数据指令全部位于 `// -----------------------------` 之前。
2. 真实解析器（`PluginManager._parse_plugin_file`）产物：3 个 slider 参数，
   契约与本表一致；capability 含 input_space/output_space = scene-linear、
   preview_cost = low。
3. 着色器代码含 `vec3 process(vec3 color, vec2 uv)` 入口；每个 @param 裸名均被
   引用；无未声明 `u_*` 标识符。
4. 全文件零 for 循环；恰好 3 次 `texture(` 调用、2 处 UV 几何 clamp、
   sin/cos/pow 用量 1/1/2。
5. 逐项通过 `../tools/validate_Prism_Aberration.py`（真实解析路径 headless 验证）。
6. zh_CN/en_US i18n 片段含 `plugins.Prism_Aberration.name` 与全部 3 个
   `.params.<var>` 键（含 desc 前向命名空间预留）。

## 8. 交付物清单

| 文件 | 说明 |
|---|---|
| `plugins/Prism_Aberration.tglsl` | 实现（新文件） |
| `docs/plugins/2026-09-28/specs/Prism_Aberration.md` | 本规格 |
| `docs/plugins/2026-09-28/tools/validate_Prism_Aberration.py` | headless 解析验证（真实解析路径） |
| `docs/plugins/2026-09-28/i18n/Prism_Aberration.json` | zh_CN + en_US 片段 |
| `docs/plugins/2026-09-28/cards/Prism_Aberration.md` | 效果卡片（含性能备注） |

---

## 9. 验证输出（validate_Prism_Aberration.py）

命令：`python3 docs/plugins/2026-09-28/tools/validate_Prism_Aberration.py`
（优先经 `src.features.color.core.plugin_manager.PluginManager._parse_plugin_file`
真实解析路径——本次运行确认真实解析器 headless 导入成功；仓库外/导入失败时
自动回退内嵌同源正则复刻。另做过负向对照：8 类人为破坏——裸整数、内容
clamp(0,1)、未声明 `u_*`、uniform 上限循环、Label 去引号、缺包裹行、移除 UV
clamp、增加第 4 个纹理 tap——对应检查全部命中。）

```text
validate_Prism_Aberration — plugins/Prism_Aberration.tglsl
------------------------------------------------------------------------------
[PASS] 文件存在 — plugins/Prism_Aberration.tglsl
[PASS] 头部首行包裹注释 // --- Topos Plugin Metadata ---
[PASS] 头部含闭合包裹行 // -----------------------------
[PASS] 元数据位置 — 全部 @ 指令位于包裹行之前
[PASS] 解析器: src.features.color.core.plugin_manager.PluginManager（真实解析器）
[PASS] @name = 'Prism Aberration'
[PASS] @i18n_name = 'Prism_Aberration'（= 文件名 stem）
[PASS] @author = 'Topos Team'
[PASS] @version = '1.0'
[PASS] @description 非空
[PASS] @preview_cost = 'low'
[PASS] @input_space = 'scene-linear'
[PASS] @output_space = 'scene-linear'
[PASS] 参数数量 = 3（期望 3）
[PASS] @param slider separation "Dispersion Strength" 0.35 0.0 1.0
[PASS] @param slider rotation "Rotation (Degrees)" 0.0 -180.0 180.0
[PASS] @param slider edge_falloff "Edge Falloff" 0.5 0.0 1.0
[PASS] 着色器代码非占位档（5177 字符 > 400）
[PASS] process 入口签名存在
[PASS] 首行早退出（separation < 0.001）
[PASS] delta 输出模式（return color + (dispersed - original_color);）
[PASS] R 通道 = uv + offset_uv 采样
[PASS] B 通道 = uv - offset_uv 采样
[PASS] G 通道未重采样（dispersed.g 沿用原位参考通道）
[PASS] dispersed 以 original_color 为基底（G 原位）
[PASS] 无 for 循环（0 个；3-tap 直采天然满足编译期上限红线）
[PASS] float 语境无缺失 .0 的裸整数
[PASS] 无 clamp(0,1)（HDR 红线）
[PASS] clamp 仅 2 处且均为 UV 几何钳制（实际 clamp=2, UV=2）
[PASS] 半 texel 分母下限守护 max(tex_size, vec2(1.0, 1.0))
[PASS] aspect 分母下限守护 max(u_aspect_ratio, 0.001)
[PASS] 中心退化守护 radial / max(dist, 0.0001)
[PASS] 半径分母守护 dist / max(max_radius, 0.0001)
[PASS] 分辨率实时获取 textureSize(u_input_tex, 0)
[PASS] 径向几何乘 u_aspect_ratio 纠偏（uv.x * aspect）
[PASS] pow 基数守护 max(dist01, 0.0001)（防 pow(0, 0) 未定义）
[PASS] 纹理 tap 数 = 3（期望 3：参考 + R + B）
[PASS] 昂贵函数用量 sin×1 cos×1 pow×2 exp/log×0/0（期望 1/1/2/0/0）
[PASS] 无未声明 u_* 引用（实际引用: ['u_aspect_ratio', 'u_input_tex']）
[PASS] 花括号配平（1/1）
[PASS] 圆括号配平（30/30）
[PASS] 无品牌商标名 \bARRI\b
[PASS] 无品牌商标名 \bSony\b
[PASS] 无品牌商标名 \bRED\b
[PASS] 无品牌商标名 \bDaVinci\b
[PASS] 无品牌商标名 \bPhotoshop\b
[PASS] 无品牌商标名 \bLightroom\b
[PASS] 无品牌商标名 \bCapture\s+One\b
[PASS] 无品牌商标名 \bPremiere\b
[PASS] 无品牌商标名 \bFinal\s+Cut\b
[PASS] 参数 'separation' 裸名被着色器引用
[PASS] 参数 'rotation' 裸名被着色器引用
[PASS] 参数 'edge_falloff' 裸名被着色器引用
[PASS] i18n 片段存在（Prism_Aberration.json）
[PASS] [zh_CN] plugins.Prism_Aberration.name = '棱镜色散'
[PASS] [zh_CN] plugins.Prism_Aberration.params.separation = '色散强度'
[PASS] [zh_CN] plugins.Prism_Aberration.params.rotation = '旋转角度'
[PASS] [zh_CN] plugins.Prism_Aberration.params.edge_falloff = '边缘衰减'
[PASS] [zh_CN] desc.params 前向命名空间齐备
[PASS] [en_US] plugins.Prism_Aberration.name = 'Prism Aberration'
[PASS] [en_US] plugins.Prism_Aberration.params.separation = 'Dispersion Strength'
[PASS] [en_US] plugins.Prism_Aberration.params.rotation = 'Rotation (Degrees)'
[PASS] [en_US] plugins.Prism_Aberration.params.edge_falloff = 'Edge Falloff'
[PASS] [en_US] params.separation 与 .tglsl Label 一致（'Dispersion Strength' == 'Dispersion Strength'）
[PASS] [en_US] params.rotation 与 .tglsl Label 一致（'Rotation (Degrees)' == 'Rotation (Degrees)'）
[PASS] [en_US] params.edge_falloff 与 .tglsl Label 一致（'Edge Falloff' == 'Edge Falloff'）
[PASS] [en_US] desc.params 前向命名空间齐备
------------------------------------------------------------------------------
结果: 67/67 通过, 0 失败/警告
退出码: 0 (ALL PASS)
```

### 9.1 补充：GPU 编译与数值验证（一次性人工核验，非交付脚本）

在仓库 `.venv`（ModernGL 5.12）standalone GL 上下文中，以引擎式包装
（`#version 330` + 参数 uniform 裸名注入）编译该着色器：编译/链接通过。
96×48（aspect=2.0，非方形验证纵横比纠偏）HDR 平滑场（值域 -0.35..4.25，
上游调色 `3.2*tex - 1.5` 使 `color` 值域 -2.62..12.1）上，对 24 组参数组合
（separation 0.35/1.0 × rotation 0°/37°/90°/180° × edge_falloff 0/0.5/1）
× 6 个像素中心点逐一与 Python 参考实现（逐式镜像着色器，含 GL 双线性/最近邻
采样复刻）对比：

- **LINEAR 过滤**：最大偏差 1.55e-04（约 1.3e-05 相对，值域至 12.1）；
- **NEAREST 对照**：最大偏差 **5.76e-07**（纯 float32 舍入）——证明着色器
  逻辑与数学规范完全一致，LINEAR 残差系 GPU 对 float32 纹理双线性权重的
  硬件精度上限，非着色器缺陷；
- 早退验证：separation=0（rotation=77 干扰项）时输出与调色原值最大偏差
  9.28e-07（float32 容差内一致）；
- HDR 验证：负值调色（最低 -2.62）经 delta 输出存活（输出最小值 -2.778，
  无 clamp(0,1)）。

该验证未产生任何仓库文件。
