# Skin_Even_Matte（哑光肤色平滑）插件规格

> 计划03（GLSL 插件库扩充）· 交付物规格
> 文件：`plugins/Skin_Even_Matte.tglsl` · plugin_id = `Skin_Even_Matte`
> 日期：2026-09-28 · 依据：`docs/plugins/2026-09-28/STYLE_NOTES.md`

---

## 1. 定位与目标

肤色选择性频率分离（skin-selective frequency separation）：以 YCbCr 色度
窗口圈出肤色像素，用**肤色门控低通**提取低频肤色层，毛孔/纹理作为高频差值
按可调比例回加，形成哑光均匀的肤质。适用场景：人像肤色均化、泛红/斑块
淡化、油光压制（哑光妆面）、保留毛孔纹理的自然磨皮。

与现有"肤质类"插件的差异定位（算法路径必须不同，禁止复制双边滤波）：

| 插件 | 选择机制 | 平滑机制 | 纹理控制 |
|---|---|---|---|
| **Skin_Even_Matte（本插件）** | **色度选择**：YCbCr 肤色窗口 matte，非肤色严格直通 | **频率分离**：肤色门控 17 tap 固定低通 + 高频差值回加 | Texture Keep 显式比例回加 |
| bilateral_skin_smooth | 无选择（全画面） | 边缘保持双边滤波（32 tap 螺旋 + 两遍 refine） | 边缘阈值 + 细节恢复两级间接控制 |
| HighPass_Retouch | 无选择（全画面） | 双 fast_blur 带阻重建（大/小两个模糊） | Detail Preserve 高频增益 |

本插件与 bilateral 的本质区别：**选择维度**（色度 matte vs 边缘权重）与
**滤波核**（肤色门控加权平均 vs 距离×色差联合权重的迭代双边）；与
HighPass_Retouch 的区别：单核 + 肤色落点 vs 双核全画面。三者可叠加使用
（均为 delta 输出模式）。

## 2. 参数（@param）

| # | 类型 | 变量 | Label（英文兜底） | 默认 | 范围 | 语义 |
|---|---|---|---|---|---|---|
| 1 | slider | `radius` | Radius | 0.4 | 0.0–1.0 | 低通半径 |
| 2 | slider | `skin_threshold` | Skin Threshold | 0.7 | 0.0–1.0 | 肤色检测阈值（窗口宽窄） |
| 3 | slider | `texture_keep` | Texture Keep | 0.5 | 0.0–1.0 | 纹理保留（高频回加比例） |

参数映射（着色器内部，全部分辨率无关）：

- **radius**：`radius_pix = max(2.0, width × (0.003 + r × 0.012))` → 第 1 环
  为图像宽度的 0.3%–1.5%（4K 下约 12–58 px，第 2 环 23–115 px）。小半径档
  刻意压低：肤色斑块/泛红的空间尺度远大于毛孔，低通覆盖斑块即可，毛孔层
  高频交给 texture_keep 控制。
- **skin_threshold**：肤色窗口半宽缩放 `mix(0.4, 1.0, t)`——0.4×（严格，
  只选典型饱和肤色）到 1.0×（经典全窗口，宽容纳入偏亮/偏灰肤色）。默认
  0.7 → 0.82×。半宽带 `max(…, 0.01)` 下限守护。
- **texture_keep**：直接缩放高频回加 `result = low + detail × keep`。
  keep = 1.0 时频率分离精确重建原值 → 首行早退出全画面旁路（近零开销）；
  keep = 0 为全哑光（重度磨平）。无独立 strength 滑杆——强度由肤色 matte
  与 keep 隐式构成（参数即任务指定的三个）。

默认值组合（0.4 / 0.7 / 0.5）让插件"刚添加即有可见但不过火"：33×33
合成 patch 上典型肤色斑点衰减至约 68% 残留（4K 大半径下邻域参与更多，
衰减更强；见 §11 双生子验证 #2）。

## 3. 算法规格

单 pass fragment shader 内完成（无 render target / ping-pong），全程在
`u_input_tex` 原生编码域运算（HighPass_Retouch 频率分离同款惯例，无
gamma 往返转换 → 零 pow）：

```
1. 肤色检测（skinMembership，每个采样独立计算）
   cb = dot(rgb, CB_WEIGHTS) + 0.5        // BT.601，色度以 0.5 为中心
   cr = dot(rgb, CR_WEIGHTS) + 0.5
   窗口 = [center ± half]，half = 经典半宽 × mix(0.4, 1.0, skin_threshold)
   经典窗口：Cb 77-127 / Cr 133-173（8 位公开肤色区间的近似）
   → [0,1] 域中心 102/255、153/255，半宽 25/255、20/255
   隶属度 = smoothstep 软边窗口（羽化 0.02 ≈ 8 位 5 个码值）的 Cb × Cr 交集
   HDR 安全：编码值可为负/超白——窗外色度自然得 0 权重，不 clamp 输入
2. 肤色门控低通（skinGatedLowPass）——固定 17 tap
   中心 tap 权重 = 1.0 + 自身隶属度（基线保证 sum ≥ 1.0，邻域无肤色时
   low → center、detail → 0，边界像素自然无效果）
   for ring in [0, NUM_RINGS):            // const int NUM_RINGS = 2
     ring_radius = radius_pix × (1 + ring)   // r、2r
     ring_weight = 1.0 − 0.5 × ring          // 1.0、0.5（近似高斯衰减）
     for t in [0, NUM_RING_TAPS):            // const int NUM_RING_TAPS = 8
       dir = RING_DIRS[t]（45° 环 const 表，零 trig）
       ring 1 经 const mat2 旋转 22.5° 错开相位 → 16 tap 得 16 个方向
       w = ring_weight × skinMembership(sample)
       accum += sample × w；sum_weight += w
   low = accum / max(sum_weight, 0.001)    // 分母下限守护（红线 #4）
3. 频率分离 + 纹理回加
   detail = original − low                 // 高频差值，可为负，不 clamp(0,1)
   detail = clamp(detail, −0.45, 0.45)     // 唯一 clamp：matte 边界增量限幅
   result = low + detail × texture_keep
4. 肤色 matte 落点 + delta 输出
   matte = smoothstep(0.15, 0.85, skinMembership(center))   // 过渡整形
   final = mix(original, result, matte)    // 非肤色严格直通
   return color + (final − original)       // 增量应用到已调色画面
```

设计理由：

- **肤色门控而非色差边缘权重**（与 bilateral 的根本分歧）：每个采样按
  自身色度隶属度决定参与度——发丝/背景/衣物（色度窗外）权重趋 0，不拉扯
  肤色均值；这是"按内容选择"，双边是"按反差保护"。
- **固定 17 tap**（任务要求的固定次数小半径采样）：1 中心 + 8×2 环；
  循环上限 `NUM_RINGS` / `NUM_RING_TAPS` 均为 `const int`（红线 #2），
  半径只改变采样步长、永不改变 tap 数——无动态收敛分支。
- **22.5° const mat2 错相**：两环 16 tap 覆盖 16 个不同方向，圆盘覆盖
  接近 32 tap 未错位的方环，全程零三角函数。
- **delta 输出模式**（HighPass_Retouch 核心惯例）：效果基于未调色的
  `u_input_tex` 计算，以增量应用到已调色 `color`，保证与上游调色正确叠加。

## 4. HDR 安全性（红线 #3）

- `color` / `u_input_tex` 采样值可为负、可远大于 1.0；**全管线无
  clamp(0,1)**。全文件仅有的数值守护/限幅：
  - `max(sum_weight, 0.001)`——归一化分母下限（红线 #4）；
  - `clamp(detail, -0.45, 0.45)`——效果增量限幅（红线 #3 明示允许的
    类别）：仅压制 matte 边界处肤色/背景反差带来的极端增量防 halo，
    不触碰基色；正常肤色内部 detail 远小于此界；
  - `max(…, 0.01)`（窗口半宽）与 `max(2.0, …)`（最小像素半径）——
    退化参数守护。
- 负值/超白编码值在 YCbCr 色度上自然落窗外（如 RGB (2,2,2) 的 Cb=Cr=0.5
  在 Cb 窗外、纯红 (10,0,0) 的 Cb 为 −1.19）→ 隶属度 0 → 严格直通；
  高频差值线性保留负值，显示域压缩交给 soft-clip 阶段。

## 5. 分辨率无关与 UV 安全（红线 #4）

- 物理分辨率实时取自 `textureSize(u_input_tex, 0)`（不缓存）；
  采样步长 = 方向单位向量 × 半径像素数 × `1.0 / current_size`。
- 半径按"图像宽度百分比"映射，4K/1080p/预览缩略图下视觉效果一致；
  环形核在像素空间各向同性，无需 `u_aspect_ratio` 纠偏（与
  bilateral_skin_smooth 同假设）。
- 越界 UV 交给纹理 clamp-to-edge（与 fast_blur 同假设）。

## 6. 性能预算（红线 #5，4K @ 60FPS）

| 项 | 数值 |
|---|---|
| 纹理 tap | 固定 17（1 中心 + 8×2 环）——低于 32-tap bilateral 预算档 |
| 三角函数 / pow / exp / log | **0**（方向表 + const mat2 旋转；原生编码域无 gamma 转换） |
| 每采样额外 ALU | 2×dot + 3×smoothstep + 少量乘加（肤色隶属度） |
| 分支 | 首行早退出 `texture_keep > 0.999`；循环无动态分支 |
| 自评 | `@preview_cost medium`（空间采样类，虽轻于 bilateral） |

对照：bilateral_skin_smooth 每像素 64 tap（32×2 遍）且每 tap 含 pow 转换；
本插件 17 tap、零 pow，整体轻于该性能标杆。

## 7. 头部元数据与 Capability Manifest

- 标准头部：`@name Skin Even Matte` / `@i18n_name Skin_Even_Matte`
  （= 文件名 stem，参数键 plugin_id 与之保持一致，先例
  HighPass_Retouch / Soft_Glow_Bloom 同款大小写）/ `@author Topos Team` /
  `@version 1.0` / `@description`。
- Capability：`@input_space scene-linear`、`@output_space scene-linear`
  （色彩类必写）、`@alpha premultiplied`、`@precision float16`、
  `@determinism deterministic`、`@export_support video`（GPU 管线可渲染
  导出，空间效果不可烘焙 LUT）、`@threading gpu`、
  `@graphics_backends opengl`、`@compute_backends glsl_fragment`。
- 不声明 `@occlusion`：读取输入画面邻域，按 fail-closed 处理。

## 8. i18n 键

片段文件：`docs/plugins/2026-09-28/i18n/Skin_Even_Matte.json`（zh_CN +
en_US，合并入 `src/shared/i18n/locales/*.json` 顶层 `plugins` 对象）：

```
plugins.Skin_Even_Matte.name                  → 哑光肤色平滑 / Skin Even Matte
plugins.Skin_Even_Matte.params.radius         → 半径 / Radius
plugins.Skin_Even_Matte.params.skin_threshold → 肤色检测阈值 / Skin Threshold
plugins.Skin_Even_Matte.params.texture_keep   → 纹理保留 / Texture Keep
```

英文兜底文案已直接写在 `.tglsl` 的参数 Label 中，其余 8 个 locale 可后补。

## 9. 交付物清单（本插件仅新增以下文件）

| 文件 | 用途 |
|---|---|
| `plugins/Skin_Even_Matte.tglsl` | 实现 |
| `docs/plugins/2026-09-28/specs/Skin_Even_Matte.md` | 本规格 |
| `docs/plugins/2026-09-28/tools/validate_Skin_Even_Matte.py` | headless 解析 + 算法验证 |
| `docs/plugins/2026-09-28/i18n/Skin_Even_Matte.json` | i18n 片段 |
| `docs/plugins/2026-09-28/cards/Skin_Even_Matte.md` | 插件卡片（含差异定位） |

## 10. 红线自检（STYLE_NOTES §7 / 附检清单）

- [x] 头部含 `// --- Topos Plugin Metadata ---` 与 `// -----------------------------` 包裹行
- [x] `@name` / `@i18n_name`（= 文件名 stem）/ `@author` / `@version` / `@description`
- [x] 色彩类声明 `@input_space scene-linear` + `@output_space scene-linear`
- [x] 参数 default/min/max 完整、Label 带引号且可直读英文
- [x] 所有 float 字面量带 `.0`；循环上限为 `const int`（NUM_RINGS / NUM_RING_TAPS）
- [x] 无未声明 `u_*` 引用（仅 `u_input_tex` + 3 个 @param 裸名）；无 clamp(0,1)
      （唯一 clamp 为 ±0.45 效果增量限幅，注释注明理由）
- [x] 分母有下限守护；环形核像素空间各向同性，无需 u_aspect_ratio
- [x] 空间采样类走 delta 输出：`return color + (final_color - original_color);`
- [x] 首行早退出；零 pow / 零三角函数 / 固定 17 tap；声明 `@preview_cost medium`
- [x] 无品牌商标名（BT.601 / YCbCr 为开放标准名，允许）
- [x] zh_CN 片段已含 `plugins.<id>.name` / `.params.<var>`

## 11. 验证

验证脚本：`docs/plugins/2026-09-28/tools/validate_Skin_Even_Matte.py`
（headless；优先复用真实解析器 `PluginManager._parse_plugin_file`，导入
失败时回退到内嵌的同源正则复刻；GLSL 红线静态检查之外，另含 **Python
双生子算法验证**——镜像 `.tglsl` 的常量与控制流（纹理按最近邻 texel
建模），在合成 patch 上断言：均匀肤色严格恒等 / 肤色斑点衰减 /
非肤色严格直通 / texture_keep=1.0 恒等 / 肤色检出与非肤色拒绝）。
运行：

```bash
python3 "docs/plugins/2026-09-28/tools/validate_Skin_Even_Matte.py"
```

输出见下方附录。

---

## 附录：验证输出（2026-09-28 实测）

命令：`python3 "docs/plugins/2026-09-28/tools/validate_Skin_Even_Matte.py"`（退出码 0）

```text
plugin_id        : Skin_Even_Matte
plugin_file      : plugins/Skin_Even_Matte.tglsl
parser           : src.features.color.core.plugin_manager.PluginManager（真实解析器）
params           : radius, skin_threshold, texture_keep
shader_chars     : 6208
------------------------------------------------------------------------
[PASS] 头部首行包裹注释 // --- Topos Plugin Metadata ---
[PASS] 头部含闭合包裹行 // -----------------------------（元数据全部位于其前）
[PASS] 解析器: src.features.color.core.plugin_manager.PluginManager（真实解析器）
[PASS] @name = 'Skin Even Matte'
[PASS] @i18n_name = 'Skin_Even_Matte'（= 文件名 stem）
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
[PASS] @param slider radius "Radius" 0.4 0.0 1.0
[PASS] @param slider skin_threshold "Skin Threshold" 0.7 0.0 1.0
[PASS] @param slider texture_keep "Texture Keep" 0.5 0.0 1.0
[PASS] 着色器代码非占位档（6208 字符 > 400）
[PASS] process 入口签名存在
[PASS] delta 输出模式（return color + (<result> - original_color);）
[PASS] 首行早退出（texture_keep > 0.999 时频率分离精确重建原值）
[PASS] 存在 for 循环（常量上限模式）
[PASS] 循环上限 'NUM_RINGS' 为编译期 const int 常量
[PASS] 循环上限 'NUM_RING_TAPS' 为编译期 const int 常量
[PASS] 固定采样核 tap 数 = 17（期望 17：中心 + 2 环 x 8 tap）
[PASS] float 语境无缺失 .0 的裸整数
[PASS] 无未声明 u_* 引用（实际引用: ['u_input_tex']）
[PASS] 无 clamp(0,1)（HDR 红线）
[PASS] 唯一 clamp 为增量限幅 clamp(detail, -0.45, 0.45)（实际 clamp 数 1）
[PASS] 全文件零 pow（原生编码域运算，无 gamma 转换）
[PASS] 全文件零三角/超越函数（方向表 + const mat2 旋转）
[PASS] 归一化分母下限守护 max(sum_weight, 0.001)
[PASS] 肤色检测常量 CB_WEIGHTS 在场
[PASS] 肤色检测常量 CR_WEIGHTS 在场
[PASS] 肤色检测常量 CB_CENTER 在场
[PASS] 肤色检测常量 CB_HALF_BASE 在场
[PASS] 肤色检测常量 CR_CENTER 在场
[PASS] 肤色检测常量 CR_HALF_BASE 在场
[PASS] 肤色检测常量 WINDOW_FEATHER 在场
[PASS] 花括号配平（6/6）
[PASS] 圆括号配平（51/51）
[PASS] 无品牌商标名 \bARRI\b
[PASS] 无品牌商标名 \bSony\b
[PASS] 无品牌商标名 \bRED\b
[PASS] 无品牌商标名 \bDaVinci\b
[PASS] 无品牌商标名 \bPhotoshop\b
[PASS] 无品牌商标名 \bLightroom\b
[PASS] 无品牌商标名 \bCapture\s+One\b
[PASS] 无品牌商标名 \bPremiere\b
[PASS] 无品牌商标名 \bFinal\s+Cut\b
[PASS] 均匀肤色严格恒等（|delta| = 1.57e-16）
[PASS] 肤色斑点被衰减（残留比例 = 0.679 < 0.95）
[PASS] 非肤色严格直通（|delta| = 0.00e+00）
[PASS] texture_keep=1.0 恒等（|delta| = 0.00e+00）
[PASS] 肤色窗口判定：典型中亮肤色 -> matte = 1.0000（检出）
[PASS] 肤色窗口判定：深肤色 (0.55,0.38,0.30) -> matte = 1.0000（检出）
[PASS] 肤色窗口判定：泛红/腮红 (0.75,0.50,0.45) -> matte = 1.0000（检出）
[PASS] 肤色窗口判定：蓝天 (0.45,0.65,0.90) -> matte = 0.0000（拒绝）
[PASS] 肤色窗口判定：绿植 (0.30,0.50,0.25) -> matte = 0.0000（拒绝）
[PASS] 肤色窗口判定：中性灰 (0.50,0.50,0.50) -> matte = 0.0000（拒绝）
[PASS] zh_CN name = '哑光肤色平滑'
[PASS] en_US name = 'Skin Even Matte'
[PASS] zh_CN params.radius = '半径'
[PASS] en_US params.radius = 'Radius'（= .tglsl 兜底 Label）
[PASS] zh_CN params.skin_threshold = '肤色检测阈值'
[PASS] en_US params.skin_threshold = 'Skin Threshold'（= .tglsl 兜底 Label）
[PASS] zh_CN params.texture_keep = '纹理保留'
[PASS] en_US params.texture_keep = 'Texture Keep'（= .tglsl 兜底 Label）
[PASS] i18n 片段仅含 zh_CN + en_US 两个 locale
[PASS] 规格文件存在且非平凡（7700 字符）
[PASS] 规格提及参数 radius
[PASS] 规格提及参数 skin_threshold
[PASS] 规格提及参数 texture_keep
[PASS] 卡片文件存在且非平凡（2244 字符）
[PASS] 卡片含插件英文名与中文名
[PASS] 卡片注明与 bilateral_skin_smooth 的差异定位
------------------------------------------------------------------------
TOTAL: 81 checks | PASS 81 | WARN 0 | FAIL 0
RESULT: ALL CHECKS PASSED
```

注：本环境无 glslangValidator / ModernGL，GL 真机编译验证由渲染管线加载时完成；
静态红线检查（循环上限、字面量、u_* 引用、clamp、配平）与 Python 双生子
算法验证（恒等性/衰减/直通/检出）已全部通过。
