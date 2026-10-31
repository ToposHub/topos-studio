# Vignette_Shapes 插件规格（计划03）

> 状态：已实现（plugins/Vignette_Shapes.tglsl）
> 依据：`docs/plugins/2026-09-28/STYLE_NOTES.md`、`docs/PLUGIN_DEV_GUIDE.md` 暗角案例 A
> （仅作需求参照，实现未照抄：本插件为多形状 + 均值保持亮度补偿的完整重设计）、
> `plugins/Radial_Blur_Pro.tglsl`（计划03 select 分发与文档惯例参照）
> 日期：2026-09-28

---

## 1. 元信息

| 项 | 值 |
|---|---|
| plugin_id | `Vignette_Shapes`（文件名 stem） |
| UI 显示名 | Vignette Shapes（zh_CN：多形状暗角） |
| 作者 / 版本 | Topos Team / 1.0 |
| 类型 | 纯逐像素数学（空间遮罩 / 暗角，零纹理采样） |
| capability | `@input_space scene-linear`、`@output_space scene-linear`、`@determinism deterministic`、`@preview_cost low` |
| i18n 键 | `plugins.Vignette_Shapes.name`、`plugins.Vignette_Shapes.params.<var>`（片段见 `../i18n/Vignette_Shapes.json`） |

## 2. 功能定位

经典电影感暗角的三形状泛化：用户在**椭圆（默认）/ 矩形（圆角）/ 圆形（几何真圆）**
之间切换遮罩形状，控制羽化宽度、暗角强度，并可开启**亮度补偿**抵消暗角带来的
整帧平均亮度下降（乘性遮罩会把画面均值拉低 `strength × E[t]`）。

与指南案例 A 的差异（"比案例更完整"的具体内容，实现不共享任何一行代码）：

| 维度 | 案例A（指南） | 本插件 |
|---|---|---|
| 形状 | 单一圆形（aspect 纠偏后的径向距离） | select 三形状：椭圆 / 圆角矩形 / 真圆，各有独立度量 |
| 变暗方式 | `mix(color, vig_color, m)` 混向固定色 | 乘性增益 `color * weight`：HDR 负值/超白等比缩放、保色调比、黑点不动 |
| 亮度补偿 | 无 | 均值保持增益 `1 / (1 − strength·E[t])`，按 `luma_comp` 混入 |
| 羽化 | 固定 0.4 宽度 | `feather` 滑杆映射过渡半宽，且下限 0.08 保证 smoothstep 恒有效 |
| 矩形形状 | 无 | 圆角盒 SDF（aspect 纠正空间内，角半径随羽化联动） |
| 边界语义 | 无明确锚定 | 三形状统一"d = 1.0 为名义边界"，falloff 50% 点恰落在边界上（任意羽化下形状读数不漂移） |

非目标（明确不做）：暗角颜色/中心点/大小参数（案例 A 的 vig_color 与 radius 不在本期
参数表内，避免参数膨胀；乘性变暗本身不需要颜色）。

## 3. 算法设计

### 3.1 坐标与形状距离

帧坐标 `q = (uv − 0.5) × 2.0 ∈ [−1, 1]²`（角点 `length = √2`），宽高比
`A = u_aspect_ratio`。每形状定义距离型标量 `d`（0 = 画面中心，1 = 名义边界）：

| 索引 | 形状 | d 定义 | 名义边界位置 | 备注 |
|---|---|---|---|---|
| 0 | Ellipse | `length(q)` | 内切椭圆：与四边中点相接 | 定义在 q 空间，**天然随画面比例**（有意不做 aspect 纠偏） |
| 1 | Rectangle | `1.0 + rounded_box_sdf(q·A 空间, (A, 1), r)` | 画面矩形本身（圆角） | aspect 纠正空间内求 SDF，圆角在像素上是几何圆弧；角半径 `r = 0.9·w` 与羽化联动 |
| 2 | Circle | `length(vec2(q.x·A, q.y))` | 内切真圆：与上下边中点相接（横幅） | 红线 #4：径向效果乘 `u_aspect_ratio` 抵消 UV 拉伸 |

分发采用 select 早返回 if 链惯例（Color_Space_Transform 模式）：`shape`（int 索引）
作为参数传入 `shape_distance` / `falloff_mean` 两个辅助函数，各含
`if (idx == 0) … else if (idx == 1) … else …`，末分支兜底 = circle。

### 3.2 羽化与强度

```
w = mix(0.08, 0.55, feather)          // 过渡半宽；下限 0.08 → smoothstep edge0 < edge1 恒成立
t = smoothstep(1.0 − w, 1.0 + w, d)   // 0 中心 → 1 边界外；50% 点恰在名义边界
weight = 1.0 − strength · t           // 乘性暗角
out = color · weight · gain
```

- 默认（feather 0.5 → w ≈ 0.315）：椭圆模式角点 d=√2 ≥ 1+w → t=1（角点全强度），
  边中点 d=1 → t=0.5（strength 0.5 时边中点约 25% 变暗），中心 0 —— "刚添加即有
  可见但不过火"。
- `feather = 0` 时 w=0.08 仍保留一条 ~4% 半高全宽的细过渡带（免锯齿 + smoothstep
  内部除零保护）。
- 变暗为**乘性**：对 scene-linear 的 HDR 值（负值、>>1）等比缩放，色调比不变，
  0 点保持 0；不引入任何 clamp。

### 3.3 亮度补偿（均值保持）

乘性遮罩把帧均值从 1 降到 `1 − strength·E[t]`（E[t] = falloff 场的帧平均）。
补偿增益：

```
mean_t    = falloff_mean(shape, w, A)                 // E[t] 闭合式（见 3.4）
mean_w    = max(1.0 − strength · mean_t, 0.25)        // 分母下限 → gain ≤ 4
gain      = mix(1.0, 1.0 / mean_w, luma_comp)         // 0 = 经典暗角，1 = 均值保持
```

- `luma_comp = 1` 时，对常数图像帧均值精确回到 1（E[t] 闭合式误差范围内，
  实测最大偏差 2.0%，见 §9 数值验证）。
- `luma_comp = 0`（默认）为经典暗角行为；补偿按 strength 比例作用，strength=0 时
  gain 恒为 1，与首行早退出一致。

### 3.4 E[t] 闭合式标定（falloff_mean）

对三形状 × 羽化网格（w ∈ [0.08, 0.55]）× 宽高比网格（A ∈ {9/16, 1, 4/3, 16/9, 2.39}）
做 1800² 中点网格数值积分（开发期一次性离线标定，未产生仓库文件），得闭合式：

| 形状 | 闭合式 | 推导 | 最大误差 |
|---|---|---|---|
| Ellipse | `0.207 + 0.131·w` | 椭圆定义于 q 空间 → 帧平均与宽高比无关；线性拟合 | 0.0023 |
| Rectangle | `0.181·(1 + 1/A)·w` | 边界带面积 ≈ 周长×w×带内 smoothstep 均值×圆角损耗，实测折合系数 0.362×0.5 | ~0.017 |
| Circle | A ≥ 1：`1 − π/(4A) + s(A)·w`；A < 1：`(0.215 − 0.36(1−A)) + s(A)·w`；`s(A) = max(0.131 + 0.12·max(1−A,0) − 0.336·max(A−1,0), 0.007)` | 硬项 = 圆外帧面积占比：横幅精确式 1−π/(4A)（内切圆面积 π / 帧面积 4A），竖幅为角区积分的线性拟合；斜率项按 A 分段线性拟合 | ~0.036（竖幅中段最差） |

常数含义：`0.7853982 = π/4`；`0.215` 为 A→1 处硬项值（1−π/4 ≈ 0.2146 的拟合值）。
所有分母下限 `max(A, 0.1)`。补偿是矫正启发式，≤0.04 的 E[t] 误差换算为增益误差
≤ ~5%，处于感知不可辨范围；验证脚本按容差 0.05 / 均值偏差 0.06 验收（§9）。

### 3.5 输出契约与 HDR 行为

- 纯逐像素数学（warm_tint 类），**不采样 `u_input_tex`**，故不走 delta 输出模式
  （该模式是空间采样类插件的惯例）；直接 `return color * weight * gain;`。
- 全程无 clamp：负值通道（越出色域）被 weight < 1 等比拉向 0（变暗语义一致）；
  超白高光同比例衰减，细节相对关系保持；显示范围处理交给 soft-clip 阶段。
- 仅引用内置 `u_aspect_ratio`（P0-COL-01：禁止未声明 `u_*`）。

## 4. 约束落实（红线映射）

| 红线（STYLE_NOTES §7） | 落实 |
|---|---|
| 1 浮点显式 `.0` | 全部 float 字面量带小数点；裸整数仅 `shape_idx == 0/1` 的 select 索引比较 |
| 2 循环上限编译期常量 | 零 for 循环（纯逐像素数学），红线 vacuously 成立 |
| 3 HDR 不无理由 clamp(0,1) | 全文件零 `clamp` 调用；仅有分母/增益下限守护 `max(·, 0.25)`、`max(A, 0.1)` |
| 4 UV 安全 / 纠偏 | 无邻域采样；circle 度量与 rect SDF 空间均乘 `u_aspect_ratio`（`q.x * aspect` ×2 处）；smoothstep 边距下限 0.08 防 edge0==edge1 |
| 5 4K 性能预算 | 首行早退出 `if (strength < 0.001) return color;`；零纹理 tap；sin/cos/exp/log/pow 全为 0（仅 mix/max/min/length/smoothstep/abs）；`@preview_cost low` |
| 6 无品牌商标名 | 名称/Label/选项/描述均为通用英文 |
| 7 无未声明 `u_*` | used = {`u_aspect_ratio`} ⊆ 内置白名单 |
| 8 头部包裹行 | `// --- Topos Plugin Metadata ---` … `// -----------------------------`，元数据全部在包裹行之前 |

## 5. 参数表

| # | 类型 | var | Label（英文兜底） | 默认 | 范围/选项 | GLSL 类型 |
|---|---|---|---|---|---|---|
| 1 | select | `shape` | Shape | 0 | "Ellipse" / "Rectangle" / "Circle" | int（索引 0..2） |
| 2 | slider | `feather` | Feather | 0.5 | 0.0 – 1.0 | float |
| 3 | slider | `strength` | Strength | 0.5 | 0.0 – 1.0 | float |
| 4 | slider | `luma_comp` | Luma Compensation | 0.0 | 0.0 – 1.0 | float |

`@param separator`（解析 no-op）置于 feather 与 strength 之间（形状几何组 / 混合组）。

## 6. 性能预算

- 每像素：1 次 `length`（椭圆/圆）或圆角盒 SDF（abs/max/min/length）+ 1 次
  `smoothstep` + 1 次 `falloff_mean`（uniform 分支内的十几次标量四则）+ 2 次除法。
  无纹理读取、无超越函数、无循环。
- 对比基准：warm_tint 同为纯数学档；HighPass_Retouch 2 tap、bilateral 32 tap×2 遍。
  本插件是 12 个新插件中最轻的一档，`@preview_cost low` 如实声明。
- 4K（~800 万像素）60FPS：ALU-only，预算占用可忽略。

## 7. 验收标准

1. 头部含两条包裹行，元数据指令全部位于 `// -----------------------------` 之前。
2. 解析产物：4 参数（select + 3 slider），select 选项 3 项、默认索引 0；capability
   含 input_space/output_space = scene-linear、determinism = deterministic、
   preview_cost = low。
3. 含 `vec3 process(vec3 color, vec2 uv)` 入口；4 个 @param 裸名全部被引用；无未声明
   `u_*`；零 for 循环、零 clamp、零纹理采样、零 sin/cos/exp/log/pow。
4. 两条 select 分发 if 链（shape_distance + falloff_mean）均为
   `if(==0) / else if(==1) / else 兜底` 结构。
5. 数值验收（headless）：E[t] 闭合式 vs 逐点积分 max|Δ| ≤ 0.05；comp=1 均值偏差
   ≤ 0.06；comp=0 中心像素不变、角点暗化 = 1 − strength；标定常数自着色器文本
   提取后参与上述比较（防镜像脱钩）。
6. zh_CN/en_US i18n 片段含 `plugins.Vignette_Shapes.name` 与全部 4 个
   `.params.<var>` 键（含 desc 前向命名空间预留）。

## 8. 交付物清单

| 文件 | 说明 |
|---|---|
| `plugins/Vignette_Shapes.tglsl` | 实现（新文件） |
| `docs/plugins/2026-09-28/specs/Vignette_Shapes.md` | 本规格 |
| `docs/plugins/2026-09-28/tools/validate_Vignette_Shapes.py` | headless 解析 + 数值验证 |
| `docs/plugins/2026-09-28/i18n/Vignette_Shapes.json` | zh_CN + en_US 片段 |
| `docs/plugins/2026-09-28/cards/Vignette_Shapes.md` | 效果卡片（含性能备注） |

---

## 9. 验证输出（validate_Vignette_Shapes.py）

命令：`python3 docs/plugins/2026-09-28/tools/validate_Vignette_Shapes.py`
（正则镜像自 `plugin_manager.py` 当前实态并标注行号；纯 stdlib headless，不导入
仓库代码。数值部分把 `falloff_mean` 的 12 个标定常数从着色器文本正则提取后驱动
逐像素仿真，与实现不脱钩。另做过 8 类人为破坏负向对照：裸整数、内容 clamp(0,1)、
未声明 `u_*`、删包裹行、默认值越界、改坏椭圆/圆标定常数、去掉早退出——
均被对应检查命中，其中"改坏标定常数"同时触发标定精度与均值保持两项 FAIL。）

```text
validate_Vignette_Shapes — plugins/Vignette_Shapes.tglsl
------------------------------------------------------------------------------
[PASS] 文件存在 — plugins/Vignette_Shapes.tglsl
[PASS] 头部包裹行 — 存在 //\s*-{5,} 结束行
[PASS] 头部包裹行 — 首行为 // --- Topos Plugin Metadata ---
[PASS] 元数据位置 — 全部 @ 指令位于 // ----------------------------- 之前
[PASS] @name — Vignette Shapes
[PASS] @i18n_name == 文件名 stem — Vignette_Shapes
[PASS] @author — Topos Team
[PASS] @version — 1.0
[PASS] @description — 单行英文描述存在
[PASS] @input_space — scene-linear
[PASS] @output_space — scene-linear
[PASS] @determinism — deterministic
[PASS] @preview_cost — low
[PASS] 参数数量 = 4（select + 3 slider） — 解析到 4 个
[PASS] shape 类型 — select
[PASS] shape 选项表 — ['Ellipse', 'Rectangle', 'Circle']
[PASS] shape 默认索引 — default=0
[PASS] feather slider 契约 — default=0.5 range=[0.0,1.0]
[PASS] strength slider 契约 — default=0.5 range=[0.0,1.0]
[PASS] luma_comp slider 契约 — default=0.0 range=[0.0,1.0]
[PASS] process 入口签名 — vec3 process(vec3 color, vec2 uv)
[PASS] 代码提取 — 包裹行之后含非注释代码行（非降级路径）
[PASS] 浮点显式 .0 — float 字面量均带小数点，裸整数仅在 select 索引比较语境
[PASS] 零 for 循环 — 纯逐像素数学，循环上限红线 vacuously 成立
[PASS] shape 分支 if 链 — if(==0)×2 / else if(==1)×2 / else 兜底×2（shape_distance + falloff_mean 各一条，末分支 else = circle）
[PASS] 分发函数调用点 — shape_distance×1 falloff_mean×1（select 索引作参数传入）
[PASS] 零 clamp 调用 — 全文件无 clamp，内容值不裁切
[PASS] 分母下限守护 — max(aspect, 0.1)（rect/circle 分母）
[PASS] 增益分母守护 — mean_weight 下限 0.25（gain <= 4.0）
[PASS] smoothstep 边距下限 — w = mix(0.08, 0.55, feather)，edge0 < edge1 恒成立
[PASS] 径向纠偏 u_aspect_ratio — q.x * aspect 出现 2 处（circle 度量 + rect SDF 空间）
[PASS] process 首行早退出 — if (strength < 0.001) return color;
[PASS] 零纹理采样 — 纯 uv 数学，无 texture() tap（对比 HighPass_Retouch 的 2 tap）
[PASS] 昂贵函数用量 — sin/cos/exp/log/pow 总计 ×0（仅 mix/max/min/length/smoothstep）
[PASS] 无品牌商标名 — 全文件未命中商标词表
[PASS] u_* 白名单 — used=['u_aspect_ratio'] ⊆ ['u_aspect_ratio', 'u_input_tex', 'u_tex_size']
[PASS] 参数全部被着色器引用 — 4/4 裸名引用
[PASS] 标定常数提取自着色器文本 — cir_floor=0.1，cir_pa=0.215，cir_pb=0.36，cir_pi4=0.785398，cir_s0=0.131，cir_s1=0.12，cir_s2=0.336，cir_sfloor=0.007，ell_a=0.207，ell_b=0.131，rect_floor=0.1，rect_k=0.181
[PASS] E[t] 标定精度（默认羽化, 3 形状 × 3 宽高比） — max|Δ|=0.0179（circle@0.562: 数值 0.0974 vs 闭合式 0.1153）<= 0.05
[PASS] 均值保持（strength=1, comp=1） — max|mean-1|=0.0203（circle@0.562: mean=1.0203）<= 0.06
[PASS] comp=0 中心像素不变 — center 输出比 = 1.000000000000
[PASS] comp=0 角点暗化 = 1 - strength — corner 输出比 = 0.5000（期望 0.5000）
------------------------------------------------------------------------------
结果: 42/42 通过, 0 失败
退出码: 0 (ALL PASS)
```
