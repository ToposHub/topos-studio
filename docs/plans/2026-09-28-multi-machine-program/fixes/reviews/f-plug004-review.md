# 复查报告 f-plug004-review — 批 F：PLUG-004 第二批 GLSL 插件（18 个）

- 日期：2026-10-01
- 复查人：GLM5.3 复查会话（只读复核，未做任何 git 写操作 / 未改 src、tests、plugins、REMEDIATION_PLAN.md、FIXLOG.md）
- 被复查对象：提交 `c117537cb`（[fix-F plug-004] 第二批 GLSL 插件库 18 个）
- 依据：FLASH_REMEDIATION_PLAN.md §4（批 F 段）、§6 复查清单 + 批 F 加码项（GLSL 红线逐项过）；执行报告 fixes/reports/ef-f-plug004.md
- 中心已复跑的测试证据（本次未重跑）：`tests/ -k plugin` → 402 passed / 1 skipped；`tests/color/test_builtin_plugin_compile.py` → 37 passed（34 个 .tglsl 真实 GL 编译）

## 0. 提交与清单核验

`git show --name-status c117537cb` 共 21 个文件、1897 行插入：

| 类别 | 文件 | 状态 |
|---|---|---|
| 插件 | plugins/{Anamorphic_Streak, Bokeh_Defocus, CRT_Scanlines, Channel_Mixer, Duotone_Toner, Edge_Ink_Outline, Glitch_Blocks, Grad_Filter_Exposure, Halftone_Print, Kaleido_Mirror, Lens_Distortion, Light_Leak_Corner, Motion_Blur_Linear, Pixel_Blocks, Tilt_Shift_Focus, Unsharp_Sharpen, Vibrance_Saturation, White_Balance_Advanced}.tglsl | 全部 A（新增），18 个 ✓ |
| i18n | src/shared/i18n/locales/{zh_CN,en_US}.json | M，diff 纯增量（各 +164/-0，经 grep 逐行核对；plugins 命名空间 16→34 组） |
| 报告 | docs/plans/.../fixes/reports/ef-f-plug004.md | A |

未改任何既有 .tglsl/.tai、未动 src 渲染逻辑、未碰 REMEDIATION_PLAN.md / FIXLOG.md。报告与提交内容一致（18 插件、六类方向分布、无方向与 batch-1 重复——已逐一比对 plugins/ 目录确认）。

## 1. §6 复查清单 8 项逐项判定

### 1.1 真实修复（交付质量）— **FAIL**

18 个文件真实存在、元数据/参数/结构完整，非"表面打勾"。但逐行审读发现 **3 个功能性缺陷（Critical）**：两个模糊插件的采样偏移存在像素/UV 单位错配（效果在真实分辨率下视觉损坏）、一个插件的滑杆参数声明后从未参与运算（滑杆无效）。这三种缺陷都无法被"编译门禁"发现——正是本批评验方式（只编译、不渲染、无逐插件断言）的盲区。

### 1.2 GLSL / HDR 红线 — **机制项 PASS，功能性红线 FAIL**

逐项机检 + 逐行人工审读：

- **浮点字面量 `.0`**：PASS。对 18 个文件做整数字面量扫描，所有裸 int 均处于整数语义上下文（循环计数 `int i = 0`、宏调用点字面量 `ACCUMULATE(14/22/30)`、select 索引比较 `== 0/1/2`、`textureSize(u_input_tex, 0)` 的 LOD 参数、Bayer 数组 int 数据与 `iy * 4 + ix` 下标、`N / 2` 整除、`float(shell + 1)` 显式转换）。未发现 float 上下文的 int 违规。
- **循环边界常量**：PASS。全部 for 循环上限为宏参数在调用点的字面常量（STREAK 14/22/30、MOTION 8/16/24、BK_ACCUM 2/3/4 环 × 8/6 向、USHARP 3/5、Tilt_Shift 字面量 2 环 × 8 向）；全仓库 18 文件无 while 循环。与 Radial_Blur_Pro 的宏展开惯例完全同构。
- **HDR 域无钳制**：PASS。18 个文件中所有 `clamp/min/max` 逐一核对语义域：采样 UV 半纹素边界钳（Anamorphic/Glitch/Edge_Ink/Kaleido/Lens/Motion/Unsharp/Tilt/Bokeh）、门控/权重钳（Anamorphic 的 `max(luma - threshold, 0.0)` 高光超出量、Vibrance 的 `clamp(chroma_mag/0.35, 0, 1)` 活泼度权重、Halftone 的 `clamp(l, 0, 1)` 网点几何半径、Edge_Ink 的 smoothstep 边缘掩码、CRT/Tilt/Unsharp 的 smoothstep 权重）、参数域钳（Kaleido `clamp(mirror_axis, 0.01, 0.99)`）、分母下限守护（`max(x, 0.001)`/`max(size, 1.0)` 族）。**没有任何一处对输出内容色做 clamp/min(.,1.0)/saturate**；全部输出路径为线性混合/乘性增益/加性增量/线性渐变外推（Duotone 的 `mix(shadow, highlight, l)` 对 l 无界线性外推，符合 Cross_Process 范式）。
- **色彩矩阵 601/709**：PASS（附更正）。复查指令假设 CRT_Scanlines / Halftone_Print 涉及 YUV/RGB 转换——实际核对：**CRT_Scanlines 完全不涉及任何色彩矩阵**（纯几何增益场）；Halftone_Print 仅用 Rec.709 亮度行向量（0.2126/0.7152/0.0722）作网点半径几何参数。全批亮度系数统一 709（Vibrance/White_Balance/Duotone/Anamorphic/Edge_Ink 五处逐字符核对一致），无 601/709 混用，无 limited/full range 语义出现。
- **但是**：Bokeh_Defocus 与 Tilt_Shift_Focus 的采样偏移单位错配（见 Critical C1/C2）属于"语法正确、语义损坏"的功能性红线违规——GL 编译通过但效果损坏。

### 1.3 并发 — N/A（PASS）

本批 diff 不含任何 Python / GL 对象 / UI 代码；.tglsl 为纯着色器文本，无线程边界。

### 1.4 资源 — N/A（PASS）

未创建任何 ModernGL/纹理/FBO 对象；无资源泄漏面。

### 1.5 异常 — N/A（PASS）

无 Python 代码，无 except 面。

### 1.6 测试质量（回归有效性）— **FAIL**

- 本提交**未新增任何测试文件**。18 个新插件仅被既有目录级门禁覆盖：`test_builtin_plugin_compile.py`（只做默认参数真实 GL 编译，无像素/渲染断言）与 scan_plugins 发现性扫描。
- batch-1 有专门的验收套件 `tests/plugins/test_plugin_batch1.py`（9 类断言 × 12 插件：真实 PluginManager 解析、元数据完整、@param schema 校验、循环边界静态检查、未声明 u_* 检查），**本批未按同一基准扩展**；报告 §4 自述"无需改测试"，与复查清单第 6 项（正向断言体现语义）和批 F 加码项第 8 条不符。
- 后果已被证实：3 个 Critical 缺陷全部处于"能编译、能解析、i18n 键齐全、但行为错"的区间——任何一条最基础的语义断言（如：均匀色输入上模糊类插件应近似恒等；滑杆变化输出必须变化）都能拦截它们。

### 1.7 越界 — **PASS**

diff 文件集 ⊆ plugins/ ∪ i18n locales（src/shared/i18n/locales）∪ fixes/reports/，全部为新增或纯增量修改；未触碰 REMEDIATION_PLAN.md、FIXLOG.md、既有 .tglsl 与 src 逻辑。工作树无本批残留改动。

### 1.8 复查产出 — 本文件。

## 2. Critical 缺陷（必须修复）

### C1（Critical）Bokeh_Defocus.tglsl — 像素量纲偏移直接加到归一化 UV 上，散焦核整体塌缩到画面边框

`plugins/Bokeh_Defocus.tglsl` L119：`radius_px = 1.0 + bokeh_radius * 0.03 * safe_size.x`（像素单位，4K 下最大约 116 px）；L76：`step_len = radius_px / float(SHELLS)`；L84 宏内采样偏移 `uv + dir * (step_len * float(shell + 1))` —— **step_len（像素）未除以纹理尺寸就直接加到 [0,1] 的归一化 uv 上**。以 1920×1080、默认参数（bokeh_radius=0.35、3 环）为例：radius_px ≈ 21.2，各环偏移 ≈ 7.1 / 14.1 / 21.2 **UV 单位**（整个画幅宽度才 1.0 UV），所有环 tap 全部被 `clamp(uv+…, min_uv, max_uv)` 压到画面边框纹素。结果是"整幅图向边框颜色平均"而非散焦——任何真实分辨率下、任何滑杆取值下（radius_px 恒 ≥ 1.0 px）效果均损坏。对照同批正确写法：Motion_Blur_Linear L74 `dir_step = dir * (span_px / safe_size.x)`、Unsharp L84 `step_px = (1.0 + 7.0*r) / safe_size`。修复：偏移按 `dir * (step_len / tex_size)` 换算回 UV（建议同时用 `vec2(1.0)/tex_size` 逐轴换算使圆盘在像素域各向同性）。

### C2（Critical）Tilt_Shift_Focus.tglsl — 同款单位错配

`plugins/Tilt_Shift_Focus.tglsl` L105：`radius_px = tilt_blur * m * 0.03 * safe_size.x`（像素）；L62：`step_len = max(radius_px * 0.5, 0.5)`；L71：`uv + dir * (step_len * float(ring + 1))` 同样未换算。因生效门槛是 `radius_px >= 0.5`（L106），**只要效果激活，最小环偏移就 ≥ 0.5 UV（半幅画面对角外）**；1920 宽、默认参数焦外区 radius_px ≈ 28.8 → 偏移 14.4/28.8 UV。整核采样全部命中边框。修复方式同 C1。

### C3（Critical）Edge_Ink_Outline.tglsl — @param `edge_strength` 声明后从未参与运算（滑杆无效）

`plugins/Edge_Ink_Outline.tglsl` 中 `edge_strength` 仅出现两处：L17 的 `@param slider edge_strength "Edge Strength" 0.6 0.0 1.0` 与 L54 的早退出判断 `if (edge_strength < 0.001) return color;`。L80 的边缘掩码 `edge = smoothstep(lo, lo + 0.15, mag)` 与 L83 的 `mix(original_color, ink_color, edge)` 均未乘 `edge_strength`——UI 上"Edge Strength"滑杆拖动无任何视觉变化（除归零旁路）。参数 ABI 破损（声明 ≠ 生效）。修复示例：`float edge = smoothstep(lo, lo + 0.15, mag) * edge_strength;`。

## 3. ISSUE（建议修复）

### I1 Halftone_Print.tglsl 单色墨分支是代数空转的伪 Delta

L82-83：`vec3 mono = vec3(l) * (1.0 - cov * ink_amount); return color + (mono - color);` —— 代数上恒等于 `return mono;`。注释与报告 §3.4 都声称"以 Delta 叠加到已调色画面"，但代码把 Delta 基准写成了 `color` 自身（正确写法应以 `texture(u_input_tex, uv).rgb` 为基准，同 Glitch_Blocks L82-84 的正确实现）。行为后果：单色墨模式完全丢弃上游调色的色度信息（keep_color 模式不受影响）。需要么改为真 Delta，么删除包装直书 `return mono;` 并修正注释。

### I2 测试缺口（对应 §6 第 6 项 / 批 F 加码第 8 条）

18 个新插件零新增测试：batch-1 的静态验收套件（test_plugin_batch1.py：解析/schema/循环边界/未声明 uniform）未扩展，渲染级语义断言（恒等性、滑杆响应性、HDR 单调性）也不存在。建议补 `tests/plugins/test_plugin_batch2.py`：至少覆盖（a）模糊/重采样类在均匀色输入上输出 ≈ 输入（可直接拦截 C1/C2）；（b）每个 @param 滑杆从默认值扰动时输出必须变化（可直接拦截 C3）；（c）全参数默认时 18 插件逐像素恒等或近恒等断言。

### I3 Anamorphic_Streak 最高档采样预算为仓库新高（Suggestion）

30 taps × 双向 = 60 次纹理采样/像素（默认档 22 taps = 44 次），高于既有最高 Radial_Blur_Pro 的 32 次。select 门控 + `@preview_cost medium` 声明齐全，不构成红线违规，但 4K 下 60 fetch/px 偏重，建议后续把顶档压到 ≤ 2×Radial 或在卡片中明示预算。

备注（不构成问题）：Motion_Blur_Linear 的行程在 UV 域等长 → 物理像素长度随方向与宽高比变化（横 12% 宽 vs 纵 12% 高），与 Radial_Blur_Pro 注释里"no aspect fix"同类设计取舍，建议注释明示；Bokeh/Tilt 修复 C1/C2 后若仅按 `safe_size.x` 归一，竖向半径物理长度会随宽高比缩放，与 Radial 同类，可接受。

## 4. 18 插件逐个明细

| # | 插件 | 红线判定（字面量/循环/钳制/矩阵） | 发现 |
|---|------|----------------------------------|------|
| 1 | Channel_Mixer | PASS / 无循环 / 无钳制 / 不涉及 | 纯线性 3x3 矩阵，默认恒等 + 早退出，干净 |
| 2 | Vibrance_Saturation | PASS / 无循环 / 权重域 clamp 合规 / 709 | clamp 仅作用于活泼度权重；亮度保持为精确代数补偿 |
| 3 | White_Balance_Advanced | PASS / 无循环 / 无内容钳制 / 709 | 线性增益模型；luma_after>0.0001 分母守护正确 |
| 4 | Duotone_Toner | PASS / 无循环 / 无钳制 / 709·平均双选 | 线性渐变对 l 无界单调外推，HDR 安全 |
| 5 | Anamorphic_Streak | PASS / 宏常量 14·22·30 / UV+门控域 / 709 | px→UV 换算正确（span_px/safe_size.x）；顶档 60 fetch/px 见 I3 |
| 6 | Light_Leak_Corner | PASS / 无循环 / 无钳制 / 不涉及 | 零采样纯点算；加性输出有界 |
| 7 | Grad_Filter_Exposure | PASS / 无循环 / 几何域 / 不涉及 | 每像素 2 三角 + 1 pow（uniform 角度），注释明示预算，可接受 |
| 8 | Pixel_Blocks | PASS / 无循环 / 下标+守护域 / 不涉及 | 量化单调无界；Bayer int 数组为整数语义，正确 |
| 9 | CRT_Scanlines | PASS / 无循环 / 守护域 / **不涉及矩阵** | 三角波纯代数；增益恒正（≥0.4），乘性 HDR 安全 |
| 10 | Edge_Ink_Outline | PASS / 无循环 / 掩码域 / 709 | **C3：edge_strength 未接线（滑杆无效）** |
| 11 | Halftone_Print | PASS / 无循环 / 几何半径域（已注释）/ 709 亮度行 | const mat2 旋转系数核对正确（15/30/45°）；**I1：单色分支伪 Delta** |
| 12 | Glitch_Blocks | PASS / 无循环 / UV 域 / 不涉及 | 保留字 `active`→`hot` 修复留痕；px→UV 换算正确 |
| 13 | Lens_Distortion | PASS / 无循环 / UV 域 / 不涉及 | auto_zoom 角点收缩分母恒 >1；Delta 输出正确 |
| 14 | Kaleido_Mirror | PASS / 无循环 / UV+参数域 / 不涉及 | 对角反射 const 法向零三角；折叠坐标边界钳合理 |
| 15 | Motion_Blur_Linear | PASS / 宏常量 8·16·24 / UV 域 / 不涉及 | px→UV 换算正确；行程宽高比备注见 §3 |
| 16 | Bokeh_Defocus | PASS（机制）/ 宏常量 2-4 环×8/6 向 / UV 域 / 不涉及 | **C1：radius_px 未换算直接加 UV，效果损坏** |
| 17 | Tilt_Shift_Focus | PASS（机制）/ 字面量 2×8 / UV+权重域 / 不涉及 | **C2：同款单位错配，效果损坏** |
| 18 | Unsharp_Sharpen | PASS / 宏常量 3·5 / UV+门控域 / 不涉及 | `N/2` 整除正确；step_px 换算正确；detail 增益 HDR 线性 |

（"矩阵"列：全部插件均不做 YUV/RGB 转换，亮度一律 Rec.709 行向量，无 601/709 混用、无 limited/full 语义。）

## 5. 结论

**FAIL（不予 --mark，修复后复验）**

- 判定理由：3 个 Critical（C1/C2 单位错配致两个模糊插件在真实分辨率下效果损坏；C3 声明参数未接线）+ 测试质量项 FAIL（18 插件零语义断言，且正是该缺口放行了上述缺陷）。
- 应肯定的部分：GLSL 机制红线（浮点字面量、循环边界、HDR 钳制、矩阵系数、性能准则）逐项干净；结构与 batch-1 基准对齐度高；越界与 i18n 纪律满分；报告诚实（自述测试覆盖缺口）。
- 修复路径建议（交执行会话，最小改动）：C1/C2 各一行量纲换算、C3 一处乘法、I1 一处 Delta 基准修正 + 新增 tests/plugins/test_plugin_batch2.py 语义断言（三条断言即可拦截本次全部 Critical），复验时本会话只查上述 diff 与新测试。

---

## 6. 复验记录（GLM5.3 中心会话，2026-10-02）——结论翻转：FAIL → **PASS（修复后）**

按本复核文件的修复路径建议，中心会话完成了全部整改并复验：

### 6.1 修复内容

| 项 | 文件 | 修复 |
|---|------|------|
| C1 | plugins/Bokeh_Defocus.tglsl | `radius_px` 在传入核函数前逐轴除以 `safe_size` 换算为 `radius_uv`；宏内偏移 `dir * (step_uv * float(shell+1))`，像素域圆盘各向同性 |
| C2 | plugins/Tilt_Shift_Focus.tglsl | 同款逐轴换算 `radius_uv = vec2(radius_px) / safe_size`；`tilt_shift_disc` 内步长下限 0.5 px 同步换算（`max(radius_uv*0.5, 0.5/safe_size)`），语义与原像素域下限一致 |
| C3 | plugins/Edge_Ink_Outline.tglsl | 边缘掩码乘 `edge_strength`：`smoothstep(lo, lo+0.15, mag) * edge_strength`，滑杆真实接线 |
| I1 | plugins/Halftone_Print.tglsl | 单色墨分支删除代数空转的伪 Delta 包装，直书 `return vec3(l) * (1.0 - cov * ink_amount)`，注释更正为"亮度骨架直接输出（上游调性明暗保留，单色语义色度有意丢弃）" |

### 6.2 新增语义测试（补 I2 测试缺口）

`tests/plugins/test_plugin_batch2.py`（59 用例，真实 GL 3.3 渲染级断言）：
- 中性旁路恒等（17 插件，Pixel_Blocks 无精确中性参数豁免并注明）
- 模糊类局域性（Bokeh 双光圈 / Tilt_Shift 焦外激活态 / Motion_Blur）+ 伴生"效果激活"防空转断言
- 全滑杆响应性扫描（18 插件 × 全部 slider，128×900 高帧避开 CRT 周期 1px 下限的分辨率门控）
- 默认参数有限性（NaN/Inf/量级）

### 6.3 回退敏感性验证（病根重引入必须红）

- 重引入 C1（`radius_uv = vec2(radius_px)`）→ `test_blur_plugins_locality[Bokeh_Defocus-params0/params1]` 双双 FAILED ✓
- 重引入 C2（同款）→ `test_blur_plugins_locality[Tilt_Shift_Focus-params2]` FAILED ✓（初版断言区落在默认居中焦带的焦内旁路里会空转，已把该用例焦带压到底部修复）
- 重引入 C3（去掉 `* edge_strength`）→ `test_slider_responsiveness[Edge_Ink_Outline]` FAILED ✓
- 还原修复后全套 59/59 passed

### 6.4 门禁复跑

- `pytest tests/ -k plugin` → **461 passed / 1 skipped**（402 既有 + 59 新增，零回归）
- `tests/color/test_builtin_plugin_compile.py` → 37 passed（修复后 4 个插件重新真实编译通过）

### 6.5 复验判定

C1/C2/C3 已修复并被渲染级回归锁定；I1 已修正；I2 测试缺口已补齐；I3（Anamorphic 60 fetch/px 顶档预算）维持原判：select 门控 + preview_cost 声明齐全，属性能风格项，登记为后续优化候选不阻塞。**批 F 达到 --mark 条件。**
