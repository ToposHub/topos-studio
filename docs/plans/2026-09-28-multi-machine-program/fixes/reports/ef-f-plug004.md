# 修复报告 ef-f-plug004 — 批 F：PLUG-004 第二批 GLSL 插件库（18 个）

- 日期：2026-10-01
- 台账条目：PLUG-004（docs/plugins/2026-09-28/FINDINGS.json，"第二批插件候选（计划已列 12-20 个方向）待第 2 轮开发"）
- 计划依据：docs/plans/2026-09-28-multi-machine-program/plan-03-glsl-plugin-library.md（第 3 节候选清单 + 第 7 节"第二批 12–20 个"）
- 规范依据：docs/PLUGIN_DEV_GUIDE.md、.clinerules GLSL 铁律、STYLE_NOTES（batch-1 质量基准）
- 分支：dev（无任何 git 写操作；工作树留改动）

## 1. 插件清单（18 个新文件，均在 plugins/）

方向覆盖六类：色彩 4、光效 3、风格化 5、畸变 2、模糊 3、细节 1——与 batch-1（胶片颗粒/柔光/光晕/暗角/色散等）无方向重复。

### 色彩类

| 文件 | 方向 | 参数表摘要 |
|------|------|-----------|
| plugins/Channel_Mixer.tglsl | RGB 3x3 通道混合矩阵 | red/green/blue_gain (0–2, 默认 1)；6 个带符号串扰滑杆 green_into_red 等 (±1, 默认 0)；mixer_strength (0–1, 默认 1)。纯线性矩阵，HDR 单调 |
| plugins/Vibrance_Saturation.tglsl | 饱和度 / 活泼度分离 | saturation (±1)、vibrance (±1，按色度幅值反比加权，保护肤色)、lum_preserve checkbox（精确 709 亮度守恒） |
| plugins/White_Balance_Advanced.tglsl | 色温/色调白平衡 | temperature (±1)、tint (±1)、wb_method select（Channel Gains / Luma Preserving）、strength (0–1)。线性通道增益模型，亮度保持为精确代数补偿 |
| plugins/Duotone_Toner.tglsl | 双色调映射 | shadow_color / highlight_color（color 控件）、tone_amount (0–1)、tone_detail (0–1)、luma_mode select（709 / Average）。线性渐变对 l 单调外推，HDR 不钳制 |

### 光效类

| 文件 | 方向 | 参数表摘要 |
|------|------|-----------|
| plugins/Anamorphic_Streak.tglsl | 变形镜头横向拉丝 | streak_quality select（14/22/30 taps，宏展开编译期常量分支）、streak_reach (0–1)、highlight_threshold (0–1)、intensity (0–1)、streak_tint color。高光超出量线性门控，加性输出 |
| plugins/Light_Leak_Corner.tglsl | 角落漏光 | leak_corner select（4 角）、leak_size (0.05–1)、strength (0–1)、leak_color color（默认暖 1.0/0.72/0.42）。零采样纯逐像素，加性平方衰减 |
| plugins/Grad_Filter_Exposure.tglsl | 旋转载渐变曝光（ND grad） | exposure (±3 stops)、grad_angle (±180°)、grad_center (0–1)、feather (0.01–1)。乘性 2^(stops·t) 增益，HDR 安全 |

### 风格化类

| 文件 | 方向 | 参数表摘要 |
|------|------|-----------|
| plugins/Pixel_Blocks.tglsl | 像素块马赛克 + 色彩量化 | block_px (1–64 px)、color_levels (2–32)、dither_mode select（None / Ordered 4x4 Bayer）。线性域量化对 HDR 单调无界 |
| plugins/CRT_Scanlines.tglsl | CRT 扫描线 / 荫罩 | crt_mode select（Scanlines / RGB Grille / Full CRT）、scan_count (120–1200)、scan_strength、mask_strength。三角波（零三角函数）乘性增益，均值亮度中性 |
| plugins/Edge_Ink_Outline.tglsl | Sobel 描边上墨 | edge_strength (0–1)、edge_threshold (0–1)、ink_color color、edge_source select（Luma / Max Channel）。8 邻域手工展开，无循环 |
| plugins/Halftone_Print.tglsl | 印刷网屏网点 | screen_angle select（0/15/30/45°，const mat2 旋转，零三角）、dot_pitch (3–32 px)、ink_amount (0–1)、keep_color checkbox。乘性油墨掩码 |
| plugins/Glitch_Blocks.tglsl | 数字信号毛刺 | band_height (4–128 px)、band_shift (0–1)、rgb_split (0–1)、corruption (0–1)。hash 激活条带错位，确定性（无时间 uniform） |

### 畸变类

| 文件 | 方向 | 参数表摘要 |
|------|------|-----------|
| plugins/Lens_Distortion.tglsl | 径向镜头畸变（桶形/枕形） | distortion (±1)、zoom_comp (0–1)、auto_zoom checkbox（按角点 r2 精确收缩采样域）。单采样重映射 |
| plugins/Kaleido_Mirror.tglsl | 折叠镜像万花筒 | mirror_mode select（Horizontal / Vertical / Quad / Diagonal）、mirror_axis (0–1)、mirror_mix (0–1)。对角反射用 const 法向，零三角函数 |

### 模糊类

| 文件 | 方向 | 参数表摘要 |
|------|------|-----------|
| plugins/Motion_Blur_Linear.tglsl | 线性方向运动模糊 | motion_quality select（8/16/24 taps，宏展开）、motion_length (0–1，上限画幅宽 12%)、motion_angle (±180°)。与 Radial_Blur_Pro（放射）互补的平移向模糊 |
| plugins/Bokeh_Defocus.tglsl | 光圈散焦 | bokeh_quality select（2/3/4 环）、bokeh_radius (0–1，上限画幅宽 3%)、iris_shape select（Circle 8 向错位环 / Hexagon 6 向对齐环保留叶片形状） |
| plugins/Tilt_Shift_Focus.tglsl | 移轴焦点（微缩） | tilt_orientation select（竖直/水平/对角焦带）、focus_center (0–1)、focus_width (0.05–1)、tilt_blur (0–1)。固定 17 tap 圆盘，焦内 <0.5 px 严格直通 |

### 细节类

| 文件 | 方向 | 参数表摘要 |
|------|------|-----------|
| plugins/Unsharp_Sharpen.tglsl | 反锐化掩模锐化 | kernel_size select（3x3 / 5x5，宏展开）、sharpen_amount (0–2)、sharpen_radius (0–1 → 1–8 px)、sharpen_threshold (0–1 边缘门限)。距离倒数加权低通，detail 增益输出 |

## 2. 验证（全过）

### 2.1 真实 GL 编译门禁（tests/color/test_builtin_plugin_compile.py 自动收录全部 .tglsl）

```
$ .venv/bin/python -m pytest tests/color/test_builtin_plugin_compile.py -q -p no:cacheprovider
.............................. [ 78%]
........                                                          [100%]
37 passed in 2.05s
```

（= 34 个 .tglsl 插件逐一以默认参数经 GL 3.3 composition 真实编译 + 3 个回归锚点测试；18 个新插件全部在内。）

### 2.2 临时冒烟脚本（/tmp/ef_f_plug004_smoke.py，按 test_builtin_plugin_compile 同法直驱 shader_compiler）

逐个加载 18 个新插件 → PluginManager.scan_plugins 解析 → plugin_binding_from_instance（默认参数）→ ShaderPipeline.build_composition_shader（standalone GL 3.3 context）：

```
[OK] Anamorphic_Streak / Light_Leak_Corner / Grad_Filter_Exposure / Channel_Mixer
[OK] Vibrance_Saturation / White_Balance_Advanced / Duotone_Toner / Pixel_Blocks
[OK] CRT_Scanlines / Edge_Ink_Outline / Halftone_Print / Glitch_Blocks
[OK] Lens_Distortion / Kaleido_Mirror / Motion_Blur_Linear / Bokeh_Defocus
[OK] Tilt_Shift_Focus / Unsharp_Sharpen
ALL 18 new plugins compiled under GL 3.3 composition path
```

开发中发现并修复 1 处：Glitch_Blocks 局部变量 `active` 是 GLSL 保留字（ERROR: 'active' : Reserved word），改名 `hot` 后通过。脚本留在 /tmp，不入库。

### 2.3 仓库插件测试扫描（任务指定命令）

```
$ .venv/bin/python -m pytest tests/ -k "plugin" -q -p no:cacheprovider
tests/plugins/test_plugin_batch1.py .... (89 passed)
tests/plugins/test_plugin_registry.py .... (39 passed)
tests/color/test_builtin_plugin_compile.py ............................. (29 项编译门禁含于本行)
============== 402 passed, 1 skipped, 34126 deselected in 29.24s ===============
```

0 failed；skip 为既有环境跳过项。已知预存失败（GPU 位精确测试、x265 10-bit、VT 探针）未被本命令选中，未出现。

### 2.4 i18n

`src/shared/i18n/locales/zh_CN.json` 与 `en_US.json` 的 `plugins` 命名空间**只增** 18 个新键组（`plugins.<id>.name` + `plugins.<id>.params.<var>`，与文件内 @param 变量一一对应，中文描述齐备）；未动任何既有键、未动其他命名空间。两文件改后 json.load 校验通过（test_plugin_registry 的逐键 exact-set 校验只针对 batch-1 的 12 个 id，不受影响且已实测通过）。

## 3. 与现有插件的结构对齐说明

新插件逐项对齐 batch-1（2026-09-28 批次）确立的质量基准（STYLE_NOTES）：

1. **元数据头**：完全一致的 `// --- Topos Plugin Metadata ---` / `// ----` 包裹；`@name`（英文显示名）+ `@i18n_name`（=文件名词干）+ `@author Topos Team` + `@version 1.0` + 英文单行 `@description`；能力声明 `@input_space/@output_space scene-linear`、`@alpha premultiplied`、`@precision float16`、`@determinism deterministic`、`@export_support`（采样类 video / 点算类 all）、`@threading gpu`、`@graphics_backends opengl`、`@compute_backends glsl_fragment`；`@preview_cost`（采样类 medium / 单采样或纯点算 low）。
2. **@param 风格**：四类控件语法、`@param separator` 分组、英文 label（中文经 locale 提供，与 batch-1 完全一致）；参数变量名避开 `_GLSL_RESERVED_IDENTIFIERS` 与 `process()` 形参（AUD-0832 构建期校验会显式拒绝）。
3. **GLSL 红线**：
   - 所有浮点字面量带 `.0`；int 仅用于整数语义（select 索引、checkbox、循环计数、Bayer 下标、`N/2` 整除）；
   - for 循环上限全部编译期常量：select 分档走 batch-1 的宏展开惯例（`STREAK_ACCUMULATE(14/22/30)`、`MOTION_ACCUMULATE(8/16/24)`、`BK_ACCUM(..., SHELLS, ...)`、`USHARP_ACCUM(3/5)`，调用点均为整数字面量，Radial_Blur_Pro 同款）；固定核用 `const int`（Tilt_Shift 17 tap）；无 while 循环；
   - 无隐式转换：`float(i)` / `vec2(textureSize(...))` / `int(min(x, 3.0))` 全部显式；
   - HDR 域不乱钳制：全部输出对 HDR 单调外推（线性混合/乘性增益/加性增量/有理或线性渐变）；仅有的 clamp 都作用于权重/几何参数并有注释说明（亮度加权、边缘门限、网点几何半径、UV 半纹素边界），与 Film_Grain 的允许-clamp 目录一致；
   - 色度仅用开放标准系数（Rec.709 亮度行向量 / BT.601 不涉 limited/full 混用）；
   - 性能：零/低三角函数（旋转用 const mat2、波形用三角波代数式；uniform 角度处的 cos/sin 与曝光 pow 在注释中明示预算）；昂贵运算无每像素重复；采样类插件 tap 数 8–33 且全部有界；分母全部下限守护；采样 UV 全部钳半纹素。
4. **惯用写法**：首行 bail-out 早退出（`< 0.001` / `abs() < 0.001`）；空间采样类一律 Delta 输出（`color + (result - original)`，HighPass_Retouch 模式）保证与上游调色正确堆叠；select 用早退 if 链分派（Color_Space_Transform 惯例）；辅助函数/常量/宏全部使用插件专属前缀（`streak_*`、`bk_*`、`usharp_*` 等），与既有 16 个插件的符号表零冲突（叠加编译安全）。
5. **注释风格**：文件头中文设计注释块（原理 / 设计要点 / 参数 ABI / P0-COL-01 禁引用未声明 u_* 警示），分节注释 `// ====== SECTION ======`，与 Film_Grain / Cross_Process / Skin_Even_Matte 一致。

## 4. 边界与遗留

- 未改任何既有 .tglsl/.tai、未动 src 逻辑与 plugin_manager/shader_compiler；
- 未修改 REMEDIATION_PLAN.md；无任何 git 写操作；
- batch-1 的 docs 卡片/规格/specs 目录（docs/plugins/2026-09-28/{specs,cards,i18n}/）与本批无关，未追加（本批交付以 plugins/ + 本报告 + locale 为限）；如需为 18 个新插件补效果卡片与 docs i18n 片段，可在后续批以同流程追加；
- 测试侧 test_plugin_batch1 / test_plugin_registry 只覆盖 batch-1 的 12 个 id，新插件由 test_builtin_plugin_compile（全量 GL 编译门禁）与 scan_plugins 发现性测试自动覆盖，无需改测试。
