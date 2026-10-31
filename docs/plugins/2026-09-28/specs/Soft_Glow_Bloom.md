# Soft_Glow_Bloom（柔光泛光）插件规格

> 计划03（GLSL 插件库扩充）· 交付物规格
> 文件：`plugins/Soft_Glow_Bloom.tglsl` · plugin_id = `Soft_Glow_Bloom`
> 日期：2026-09-28 · 依据：`docs/plugins/2026-09-28/STYLE_NOTES.md`

---

## 1. 定位与目标

阈值式柔光/泛光（threshold-based soft glow / bloom）：从画面中提取高于
亮度阈值的高光，经低成本模糊扩散成柔和光晕，再以可调强度叠加回原图。
适用场景：夜景点光源辉光、逆光人像轮廓光、高对比画面的柔化高光、
梦幻/氛围感调色。

三类效果差异定位：

| 插件 | 机制 | 差异 |
|---|---|---|
| **Soft_Glow_Bloom（本插件）** | 亮度阈值提取 + 加法泛光 | 只增强亮部，暗部不受影响 |
| Film_Halation | 高光染色 + 光晕 | 偏胶片红色 halation 质感（当前为占位实现） |
| bilateral_skin_smooth | 边缘保持平滑 | 全画面磨皮，无亮度选择性 |

## 2. 参数（@param）

| # | 类型 | 变量 | Label（英文兜底） | 默认 | 范围 | 语义 |
|---|---|---|---|---|---|---|
| 1 | slider | `threshold` | Threshold | 0.5 | 0.0–1.0 | 亮度阈值（提取门限） |
| 2 | slider | `radius` | Glow Radius | 0.5 | 0.0–1.0 | 泛光扩散半径 |
| 3 | slider | `intensity` | Blend Intensity | 0.5 | 0.0–1.0 | 混合强度 |

参数映射（着色器内部，全部分辨率无关）：

- **threshold**：`thresh_lum = 0.25 + t² × 2.25` → scene-linear 软膝中心
  亮度 0.25–2.5。二次曲线在漫反射白附近的常用区段提供更细控制分辨率；
  上限 2.5 覆盖 HDR 超白（>1.0）高光。软膝半宽 `knee = max(thresh_lum ×
  0.4, 0.001)`，权重在 `[thresh−knee, thresh+knee]` 间 smoothstep 爬升，
  避免硬阈值的抖动/带状伪影。
- **radius**：`radius_pix = max(2.0, width × (0.004 + r × 0.026))` →
  图像宽度的 0.4%–3.0%（4K 下约 15–115 px），与 HighPass_Retouch 低频
  提取半径同一档位跨度。同时决定激活的 ring pass 数：
  `num_passes = 1 + int(min(r, 0.9999) × 3.999)` → 四分位映射 1/2/3/4。
- **intensity**：`glow_gain = 1.0 − pow(1.0 − i, 1.5)`（感知曲线，全文件
  唯一 pow），直接缩放泛光增量。

默认值组合让插件"刚添加即有可见但不过火"（中等阈值 + 中等半径 + 半强度）。

## 3. 算法规格

单 pass fragment shader 内完成（无 render target / ping-pong）：

```
1. 亮部提取（extractBright）
   lum = dot(c, LUMA_709)                  // Rec.709 scene-linear
   w   = smoothstep(thresh−knee, thresh+knee, lum)
   bright = c × w                           // HDR 安全：c 不做任何 clamp
2. 多 pass 环形模糊（glowBlur）——常量循环上限的近似
   for pass_i in [0, MAX_BLUR_PASSES):      // const int MAX_BLUR_PASSES = 4
       if pass_i >= num_passes: break       // 动态收敛，循环上限恒为编译期常量
       sub_radius = radius_pix × (pass_i+1) / num_passes
       weight     = 1/(pass_i+2)            // 近似高斯同心衰减
       黄金角旋转 pass_i×2.39996 后，沿 const vec2 RING_DIRS[8]（45° 环）
       采样 8 tap 的亮部图层并加权累计
   glow = Σ / max(Σw, 0.001)                // 分母下限守护
3. 泛光整形
   glow = mix(vec3(lum(glow)), glow, 0.75)  // 固定 25% 去饱和，防过饱和 halo
4. 合成（加法 + delta 输出模式）
   result  = original + glow × glow_gain
   return color + (result − original)       // 增量应用到已调色画面
```

设计理由：

- **环形多 pass 而非方框卷积**：8 tap/ring × ≤4 ring + 1 中心 = **≤33 纹理
  tap**，与 32-tap bilateral（性能标杆）同预算档；黄金角相位旋转让相邻
  ring 的采样点错开，8 tap 达到接近 16 tap 未旋转环的圆盘覆盖。
- **const 方向表 `RING_DIRS[8]`**：每 tap 零三角函数；每 pass 仅 2 次
  trig（cos/sin 旋转），全像素 ≤8 次。
- **加权平均而非纯累加**：glow 幅值稳定在小亮源/大亮源之间可预期，
  intensity 滑杆手感一致。
- **delta 输出模式**（HighPass_Retouch 核心惯例）：效果基于未调色的
  `u_input_tex` 计算，以增量应用到已调色 `color`，保证与上游调色正确叠加。

## 4. HDR 安全性（红线 #3）

- `color` / `u_input_tex` 采样值可为负、可远大于 1.0；**亮度提取与整条
  管线无任何 clamp(0,1)**。全文件仅有的数值守护：
  - `max(sum_weight, 0.001)`——归一化分母下限（红线 #4）；
  - `max(thresh_lum × 0.4, 0.001)`——smoothstep 边缘严格递增守护（防 NaN）；
  - `max(2.0, width × ratio)`——最小像素半径守护。
- 负亮度（负通道）在 softstep 下自然得到权重 0；超白高光按原始线性
  能量进入泛光，显示域压缩交给 soft-clip 阶段。

## 5. 分辨率无关与 UV 安全（红线 #4）

- 物理分辨率实时取自 `textureSize(u_input_tex, 0)`（不缓存）；
  采样步长 = 半径像素数 × `1.0 / current_size`。
- 半径按"图像宽度百分比"映射，4K/1080p/预览缩略图下视觉效果一致。
- 越界 UV 交给纹理 clamp-to-edge（与 fast_blur 同假设），不做额外越界计算。

## 6. 性能预算（红线 #5，4K @ 60FPS）

| 项 | 数值 |
|---|---|
| 纹理 tap | 1 + 8×passes ≤ 33（默认 radius=0.5 → 2 passes → 17 tap） |
| 三角函数 | ≤ 8/像素（2 × 激活 pass 数） |
| pow | 1（intensity 感知曲线） |
| 分支 | 首行早退出 `intensity < 0.001`；循环内 break 收敛 |
| 自评 | `@preview_cost medium` |

## 7. 头部元数据与 Capability Manifest

- 标准头部：`@name` / `@i18n_name Soft_Glow_Bloom`（= 文件名 stem，与
  参数键 plugin_id 保持一致，先例 HighPass_Retouch/Film_Halation 同款
  大小写）/ `@author Topos Team` / `@version 1.0` / `@description`。
- Capability：`@input_space scene-linear`、`@output_space scene-linear`
  （色彩类必写）、`@alpha premultiplied`、`@precision float16`、
  `@determinism deterministic`、`@export_support video`（GPU 管线可渲染
  导出，空间效果不可烘焙 LUT）、`@threading gpu`、
  `@graphics_backends opengl`、`@compute_backends glsl_fragment`。
- 不声明 `@occlusion`：泛光读取并扩散输入画面内容，按 fail-closed 处理。

## 8. i18n 键

片段文件：`docs/plugins/2026-09-28/i18n/Soft_Glow_Bloom.json`（zh_CN +
en_US，合并入 `src/shared/i18n/locales/*.json` 顶层 `plugins` 对象）：

```
plugins.Soft_Glow_Bloom.name                → 柔光泛光 / Soft Glow Bloom
plugins.Soft_Glow_Bloom.params.threshold    → 阈值 / Threshold
plugins.Soft_Glow_Bloom.params.radius       → 泛光半径 / Glow Radius
plugins.Soft_Glow_Bloom.params.intensity    → 混合强度 / Blend Intensity
```

英文兜底文案已直接写在 `.tglsl` 的参数 Label 中，其余 8 个 locale 可后补。

## 9. 交付物清单（本插件仅新增以下文件）

| 文件 | 用途 |
|---|---|
| `plugins/Soft_Glow_Bloom.tglsl` | 实现 |
| `docs/plugins/2026-09-28/specs/Soft_Glow_Bloom.md` | 本规格 |
| `docs/plugins/2026-09-28/tools/validate_Soft_Glow_Bloom.py` | headless 解析验证 |
| `docs/plugins/2026-09-28/i18n/Soft_Glow_Bloom.json` | i18n 片段 |
| `docs/plugins/2026-09-28/cards/Soft_Glow_Bloom.md` | 插件卡片 |

## 10. 红线自检（STYLE_NOTES §7 / 附检清单）

- [x] 头部含 `// --- Topos Plugin Metadata ---` 与 `// -----------------------------` 包裹行
- [x] `@name` / `@i18n_name`（= 文件名 stem）/ `@author` / `@version` / `@description`
- [x] 色彩类声明 `@input_space scene-linear` + `@output_space scene-linear`
- [x] 参数 default/min/max 完整、Label 带引号且可直读英文
- [x] 所有 float 字面量带 `.0`；循环上限为 `const int`（动态收敛用 break）
- [x] 无未声明 `u_*` 引用（仅 `u_input_tex` + 3 个 @param 裸名）；无 clamp(0,1)
- [x] 分母有下限守护；无径向画圆需求（未用 u_aspect_ratio，方向表作用于 UV 空间时半径以像素等比缩放）
- [x] 空间采样类走 delta 输出：`return color + (result - original_color);`
- [x] 首行早退出；trig ≤8 / pow=1 / tap ≤33；声明 `@preview_cost medium`
- [x] 无品牌商标名（Rec.709 为开放标准名，允许）
- [x] zh_CN 片段已含 `plugins.<id>.name` / `.params.<var>`

## 11. 验证

验证脚本：`docs/plugins/2026-09-28/tools/validate_Soft_Glow_Bloom.py`
（headless；优先复用真实解析器 `PluginManager._parse_plugin_file`，导入
失败时回退到内嵌的同源正则复刻；另做 GLSL 红线静态检查、i18n/卡片/规格
交叉校验）。运行：

```bash
python3 "docs/plugins/2026-09-28/tools/validate_Soft_Glow_Bloom.py"
```

输出见下方附录。

---

## 附录：验证输出（2026-09-28 实测）

命令：`python3 "docs/plugins/2026-09-28/tools/validate_Soft_Glow_Bloom.py"`（退出码 0）

```text
plugin_id        : Soft_Glow_Bloom
plugin_file      : plugins/Soft_Glow_Bloom.tglsl
parser           : src.features.color.core.plugin_manager.PluginManager（真实解析器）
params           : threshold, radius, intensity
shader_chars     : 4883
------------------------------------------------------------------------
[PASS] 头部首行包裹注释 // --- Topos Plugin Metadata ---
[PASS] 头部含闭合包裹行 // -----------------------------（元数据全部位于其前）
[PASS] 解析器: src.features.color.core.plugin_manager.PluginManager（真实解析器）
[PASS] @name = 'Soft Glow Bloom'
[PASS] @i18n_name = 'Soft_Glow_Bloom'（= 文件名 stem）
[PASS] @author = 'Topos Team'
[PASS] @version = '1.0'
[PASS] @description 非空
[PASS] @preview_cost = 'medium'
[PASS] @input_space = 'scene-linear'
[PASS] @output_space = 'scene-linear'
[PASS] @alpha = 'premultiplied'
[PASS] @precision = 'float16'
[PASS] @determinism = 'deterministic'
[PASS] @export_support = 'video'
[PASS] @threading = 'gpu'
[PASS] @graphics_backends = 'opengl'
[PASS] @compute_backends = 'glsl_fragment'
[PASS] 参数数量 = 3（期望 3）
[PASS] @param slider threshold "Threshold" 0.5 0.0 1.0
[PASS] @param slider radius "Glow Radius" 0.5 0.0 1.0
[PASS] @param slider intensity "Blend Intensity" 0.5 0.0 1.0
[PASS] 着色器代码非占位档（4883 字符 > 400）
[PASS] process 入口签名存在
[PASS] delta 输出模式（return color + (result - original_color);）
[PASS] 首行早退出（intensity < 0.001）
[PASS] 存在 for 循环（常量上限模式）
[PASS] 循环上限 'MAX_BLUR_PASSES' 为编译期 const int 常量
[PASS] 循环上限 'NUM_RING_TAPS' 为编译期 const int 常量
[PASS] 动态半径经 break 收敛（if (i >= n) break;，n 为运行期标量）
[PASS] float 语境无缺失 .0 的裸整数
[PASS] 无未声明 u_* 引用（实际引用: ['u_input_tex']）
[PASS] 无 clamp(0,1)（HDR 红线）
[PASS] 全文件零 clamp（增量/显示边界均不需要）
[PASS] 归一化分母下限守护 max(sum_weight, 0.001)
[PASS] 花括号配平（5/5）
[PASS] 圆括号配平（46/46）
[PASS] 无品牌商标名 \bARRI\b
[PASS] 无品牌商标名 \bSony\b
[PASS] 无品牌商标名 \bRED\b
[PASS] 无品牌商标名 \bDaVinci\b
[PASS] 无品牌商标名 \bPhotoshop\b
[PASS] 无品牌商标名 \bLightroom\b
[PASS] 无品牌商标名 \bCapture\s+One\b
[PASS] 无品牌商标名 \bPremiere\b
[PASS] 无品牌商标名 \bFinal\s+Cut\b
[PASS] zh_CN name = '柔光泛光'
[PASS] en_US name = 'Soft Glow Bloom'
[PASS] zh_CN params.threshold = '阈值'
[PASS] en_US params.threshold = 'Threshold'（= .tglsl 兜底 Label）
[PASS] zh_CN params.radius = '泛光半径'
[PASS] en_US params.radius = 'Glow Radius'（= .tglsl 兜底 Label）
[PASS] zh_CN params.intensity = '混合强度'
[PASS] en_US params.intensity = 'Blend Intensity'（= .tglsl 兜底 Label）
[PASS] i18n 片段仅含 zh_CN + en_US 两个 locale
[PASS] 规格文件存在且非平凡（5976 字符）
[PASS] 规格提及参数 threshold
[PASS] 规格提及参数 radius
[PASS] 规格提及参数 intensity
[PASS] 卡片文件存在且非平凡（1391 字符）
[PASS] 卡片含插件英文名与中文名
------------------------------------------------------------------------
TOTAL: 61 checks | PASS 61 | WARN 0 | FAIL 0
RESULT: ALL CHECKS PASSED
```

注：本环境无 glslangValidator / ModernGL，GL 真机编译验证由渲染管线加载时完成；
静态红线检查（循环上限、字面量、u_* 引用、clamp、配平）已全部通过。

