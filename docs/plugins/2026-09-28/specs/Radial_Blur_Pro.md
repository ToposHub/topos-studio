# Radial_Blur_Pro 插件规格（计划03）

> 状态：已实现（plugins/Radial_Blur_Pro.tglsl）
> 依据：`docs/plugins/2026-09-28/STYLE_NOTES.md`、`plugins/HighPass_Retouch.tglsl`（采样循环与 delta 输出参照）
> 日期：2026-09-28

---

## 1. 元信息

| 项 | 值 |
|---|---|
| plugin_id | `Radial_Blur_Pro`（文件名 stem） |
| UI 显示名 | Radial Blur Pro（zh_CN：径向模糊 Pro） |
| 作者 / 版本 | Topos Team / 1.0 |
| 类型 | 空间采样类（变焦径向模糊 / zoom radial blur） |
| capability | `@input_space scene-linear`、`@output_space scene-linear`、`@preview_cost medium` |
| i18n 键 | `plugins.Radial_Blur_Pro.name`、`plugins.Radial_Blur_Pro.params.<var>`（片段见 `../i18n/Radial_Blur_Pro.json`） |

## 2. 功能定位

沿"模糊中心 → 当前像素"的射线方向做固定步长多点采样并取平均，产生从中心向外
发散的经典变焦（zoom）模糊效果。中心点可调（默认画面中心），用于聚焦引导、
速度感/冲击感营造。空间采样一律基于 `u_input_tex`（调色前基准）计算，经 delta
输出叠加到已调色的 `color` 上（HighPass_Retouch 惯例，保证上游调色正确叠加）。

## 3. 算法设计

### 3.1 采样公式

对每个像素 `uv`，设中心 `C = vec2(center_x, center_y)`、射线 `ray = uv - C`、
归一化采样跨度 `pull`：

```
sample_j = uv - ray * (pull * j / N)      j = 0 .. N-1（固定步长 pull/N）
result   = (1/N) * Σ texture(u_input_tex, sample_j)
```

- 所有采样点严格落在 中心→像素 线段内（`s_j ≤ pull ≤ 1.0`），天然不出画；
  另做显式半 texel 边界 clamp 作为 clamp-to-edge 保险（§4.1）。
- `j = 0` 恰为当前像素本身，权重 1/N，与其余采样等权。
- 强度曲线：`pull = pow(strength, 1.5)`（ease-in）。默认 `strength = 0.35` →
  `pull ≈ 0.207`，即采样段约占到中心距离的 21%，"刚添加即有可见但不过火"。

### 3.2 采样数 select → 编译期常量分支（红线 #2 落实方案）

`sample_quality` 为 select（int 索引 0..3），着色器内**不做**以 uniform 为上限的
循环，而是经宏 `RADIAL_ACCUMULATE(N)` 派发到 4 个字面量上限分支：

```glsl
if      (quality == 0) { RADIAL_ACCUMULATE(8)  }
else if (quality == 1) { RADIAL_ACCUMULATE(16) }
else if (quality == 2) { RADIAL_ACCUMULATE(24) }
else                   { RADIAL_ACCUMULATE(32) }   // 末尾兜底（if 链惯例）
```

每个分支内 `for (int i = 0; i < N; i++)` 的 `N` 在宏展开后均为字面量常量，
编译器可完全展开/优化；运行时只剩一次 int 分支（Color_Space_Transform 的
select 早返回分发惯例）。select 选项表与分支字面量一一对应且同序：
`"8 Samples" "16 Samples" "24 Samples" "32 Samples"`，默认索引 1（16 采样，
质量/性能平衡点）。

### 3.3 输出契约

```glsl
vec3 blurred       = radial_zoom_blur(uv, ray, pull, sample_quality, min_uv, max_uv);
return color + (blurred - original_color);   // delta 输出模式
```

其中 `original_color = texture(u_input_tex, uv).rgb`。

## 4. 约束落实（红线映射）

| 红线（STYLE_NOTES §7） | 落实 |
|---|---|
| 1 浮点显式 `.0` | 全部 float 字面量带小数点；仅循环计数、select 索引比较、`textureSize(.,0)`、宏调用点字面量为整数语境 |
| 2 循环上限编译期常量 | §3.2 宏字面量分支；无任何 uniform 上限循环 |
| 3 HDR 不无理由 clamp(0,1) | 采样在线性空间等权平均，内容值（可负、可 >>1）全程不 clamp；唯一 clamp 是 UV 坐标的半 texel 边界（几何安全，非色彩） |
| 4 UV 安全 | 采样偏移按像素位于中心→像素线段内 + `clamp(suv, half_texel, 1.0 - half_texel)` 显式 clamp-to-edge；`half_texel = 0.5 / max(tex_size, vec2(1.0, 1.0))` 分母有下限 |
| 5 4K 性能预算 | 首行早退出 `if (strength < 0.001) return color;`；纹理 tap 数 = N（8/16/24/32，用户可选）；全函数仅 1 次 `pow`（强度曲线）；`@preview_cost medium` 声明播放降级 |
| 6 无品牌商标名 | 名称/Label/描述均为通用英文 |
| 7 无未声明 `u_*` | 仅用内置 `u_input_tex`；`u_tex_size`/`u_aspect_ratio` 未使用（分辨率经 `textureSize(u_input_tex, 0)` 获取） |
| 8 头部包裹行 | `// --- Topos Plugin Metadata ---` … `// -----------------------------`，元数据全部在包裹行之前 |

**关于 u_aspect_ratio 的说明（有意不乘）**：变焦模糊的条纹沿"中心→像素"射线，
UV→像素的轴缩放是仿射映射，把射线仍映射为射线（条纹方向不变），因此无需纵横
比纠偏；`u_aspect_ratio` 纠偏仅在各向同性（圆形核）效果中必需。此决策已注释在
着色器内。

## 5. 参数表

| # | 类型 | var | Label（英文兜底） | 默认 | 范围/选项 | GLSL 类型 |
|---|---|---|---|---|---|---|
| 1 | select | `sample_quality` | Sample Quality | 1 | "8 Samples" / "16 Samples" / "24 Samples" / "32 Samples" | int（索引 0..3） |
| 2 | slider | `strength` | Blur Strength | 0.35 | 0.0 – 1.0 | float |
| 3 | slider | `center_x` | Center X | 0.5 | 0.0 – 1.0 | float（UV 空间） |
| 4 | slider | `center_y` | Center Y | 0.5 | 0.0 – 1.0 | float（UV 空间） |

`@param separator`（解析 no-op）置于 strength 与 center 组之间做视觉分组。

## 6. 性能预算

- 每像素纹理 tap = N（8/16/24/32 档），默认档 16 tap；对比基准：
  bilateral_skin_smooth 为 32 tap × 两遍 refine。
- 4K（~800 万像素）60FPS 目标下，32 Samples 档为重度配置，已声明
  `@preview_cost medium` 交由效果预览质量管理器播放降级。
- 除 N 次 `texture()` 与 1 次 `pow` 外无其他昂贵调用（无 sin/cos/exp/log）。

## 7. 验收标准

1. 头部含两条包裹行，元数据指令全部位于 `// -----------------------------` 之前；
   着色器代码区不含任何 `// @<key>` 形式指令。
2. 解析产物：4 个参数（select + 3 slider），select 选项 4 项、默认索引 1；
   capability 含 input_space/output_space = scene-linear、preview_cost = medium。
3. 着色器代码含 `vec3 process(vec3 color, vec2 uv)` 入口；每个 @param 裸名均被
   引用；无未声明 `u_*` 标识符。
4. 所有 for 循环上限为宏字面量（8/16/24/32），与 select 选项同序同值。
5. 逐项通过 `../tools/validate_Radial_Blur_Pro.py`（headless 解析验证）。
6. zh_CN/en_US i18n 片段含 `plugins.Radial_Blur_Pro.name` 与全部 4 个
   `.params.<var>` 键（含 desc 前向命名空间预留）。

## 8. 交付物清单

| 文件 | 说明 |
|---|---|
| `plugins/Radial_Blur_Pro.tglsl` | 实现（新文件） |
| `docs/plugins/2026-09-28/specs/Radial_Blur_Pro.md` | 本规格 |
| `docs/plugins/2026-09-28/tools/validate_Radial_Blur_Pro.py` | headless 解析验证 |
| `docs/plugins/2026-09-28/i18n/Radial_Blur_Pro.json` | zh_CN + en_US 片段 |
| `docs/plugins/2026-09-28/cards/Radial_Blur_Pro.md` | 效果卡片（含性能备注） |

---

## 9. 验证输出（validate_Radial_Blur_Pro.py）

命令：`python3 docs/plugins/2026-09-28/tools/validate_Radial_Blur_Pro.py`
（正则镜像自 `plugin_manager.py` 当前实态并标注行号；纯 stdlib headless，
不导入仓库代码。另做过负向对照：裸整数、uniform 循环上限、内容 clamp(0,1)、
未声明 `u_*`、select 选项与分支不一致、缺包裹行、Label 去引号等 7 类
人为破坏均被对应检查命中。）

```text
validate_Radial_Blur_Pro — plugins/Radial_Blur_Pro.tglsl
------------------------------------------------------------------------------
[PASS] 文件存在 — plugins/Radial_Blur_Pro.tglsl
[PASS] 头部包裹行 — 存在 //\s*-{5,} 结束行
[PASS] 头部包裹行 — 首行为 // --- Topos Plugin Metadata ---
[PASS] 元数据位置 — 全部 @ 指令位于 // ----------------------------- 之前
[PASS] @name — Radial Blur Pro
[PASS] @i18n_name == 文件名 stem — Radial_Blur_Pro
[PASS] @author — Topos Team
[PASS] @version — 1.0
[PASS] @description — 单行英文描述存在
[PASS] @input_space — scene-linear
[PASS] @output_space — scene-linear
[PASS] @preview_cost — medium
[PASS] 参数数量 = 4（select + 3 slider） — 解析到 4 个
[PASS] sample_quality 类型 — select
[PASS] sample_quality 选项表 — ['8 Samples', '16 Samples', '24 Samples', '32 Samples']
[PASS] sample_quality 默认索引 — default=1
[PASS] strength slider 契约 — default=0.35 range=[0.0,1.0]
[PASS] center_x slider 契约 — default=0.5 range=[0.0,1.0]
[PASS] center_y slider 契约 — default=0.5 range=[0.0,1.0]
[PASS] process 入口签名 — vec3 process(vec3 color, vec2 uv)
[PASS] 代码提取 — 包裹行之后含非注释代码行（非降级路径）
[PASS] 浮点显式 .0 — float 字面量均带小数点，裸整数仅在循环/比较/lod 语境
[PASS] 宏定义 — #define RADIAL_ACCUMULATE(N)
[PASS] 循环上限为宏参（编译期字面量） — bounds=['N']
[PASS] select 选项 ↔ 宏分支字面量同序同值 — branches=[8, 16, 24, 32] options=[8, 16, 24, 32]
[PASS] 分支字面量升序且唯一 — [8, 16, 24, 32]
[PASS] 末尾 else 兜底分支 — if 链最后分支为 else
[PASS] 无 clamp(0,1) 于图像内容 — 仅 UV 几何 clamp（半 texel 边界），内容值不 clamp
[PASS] UV clamp-to-edge 显式钳制 — clamp(suv, min_uv, max_uv)
[PASS] 分母下限守护 — 0.5 / max(tex_size, vec2(1.0, 1.0))
[PASS] 分辨率实时获取 — textureSize(u_input_tex, 0)
[PASS] process 首行早退出 — if (strength < 0.001) return color;
[PASS] delta 输出模式 — return color + (blurred - original_color);
[PASS] 昂贵函数用量 — pow×1，sin/cos/exp/log×0（tap 数 = N ≤ 32 由 select 控制）
[PASS] 无品牌商标名 — 全文件未命中商标词表
[PASS] u_* 白名单 — used=['u_input_tex'] ⊆ ['u_aspect_ratio', 'u_input_tex', 'u_tex_size']
[PASS] 参数全部被着色器引用 — 4/4 裸名引用
------------------------------------------------------------------------------
结果: 37/37 通过, 0 失败
退出码: 0 (ALL PASS)
```

### 9.1 补充：GPU 编译与数值验证（一次性人工核验，非交付脚本）

在仓库 `.venv`（ModernGL）standalone GL 上下文中，以引擎式包装
（`#version 330` + 参数 uniform 裸名注入）编译该着色器：编译/链接通过；
64×64 线性渐变（HDR 值域 -0.25..4.0）上对 4 个采样档 × 2 个强度
（0.5 / 1.0）× 离中心采样点逐一与解析值对比，最大偏差 ≤ 1.3e-5
（float32 精度），并验证：strength=0 早退输出与调色原值逐位一致、
负值通道经 delta 输出无 clamp 存活。该验证未产生任何仓库文件。
