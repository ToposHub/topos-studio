# L00 逐项去重台账 · F/X/C/P

> 依据 `docs/upgrade_plan_L_content_library_2026-10-24.md` §8 L00。
> 基线：HEAD 70bfe5fee，2026-10-25（见 `docs/validation/upgrade_L/L00_baseline.md`）。
> 状态均为 **planned**（本表为 L00 去重结论，非完成声明）。
> 证据列给到"文件（行号按开工 checkout）"；行号是定位锚点不是固定接口。
> 去重方法：不只搜文件名——逐个读算法/参数/调用方（四份 Explore 扫描 + 人工抽查承重事实）。

> **状态更新 2026-10-25（L02-L05 + 试点 + F18 之后）**：
> 已交付并接宿主真实运行——F16/F19（plugins/LIB1_*.tglsl + L02 报告）、
> X01/X09（transition_packs 生产核 + L03 报告）、C01/C17（nodes/lib1_nodes +
> L04 报告）、F18（EffectCatalog 内置 lib1_posterize）。下表"状态"列保持
> L00 冻结口径；逐项最新状态见 docs/validation/upgrade_L/ 各报告与
> docs/upgrade_plan_L_content_library_2026-10-24.md 的包表勾选。

图例：分类 N=新算法候选，E=已有算法明确差量，W=复用现有核提供入口；
等价度 = **无重复**（全新核）/ **部分等价**（算法或子集已存在，差量明确）/ **可复用**（入口差量）。

---

## 1. 滤镜/调色算子 F01–F20（20 张）

| ID/slug | 分类/包 | 等价度 | 已有实现与参数级差量 | 目标页面 | 主责 | 真实依赖 | 证据 |
|---|---|---|---|---|---|---|---|
| F01 wb_adaptation | E/L10 | 部分等价 | `White_Balance_Advanced.tglsl`：temp/tint/strength/wb_method，**固定系数通道增益**（R=1+0.30t+0.10g…）+可选 709 luma 保持。差量：source/target white 显式白点、可解释色适应模型（Bradford 等）——不是把 gain 换名。旧插件不动，新版本化入口 | Color+Edit | 第三机核/集成者接缝 | L02 点处理子门；白点/工作空间冻结按 M19.1 已验子集声明 | plugins/White_Balance_Advanced.tglsl |
| F02 subtractive_density | N/L10 | 无重复 | 最近似 `Cross_Process.tglsl`（每通道有理曲线+染料雾加性串扰），非减色密度模型。视觉+数值双向差量记录 | Color+Edit | 第三机 | L02 点处理子门 | plugins/Cross_Process.tglsl |
| F03 protected_vibrance | E/L10 | 部分等价 | `Vibrance_Saturation.tglsl`：保护权重=全画面低**色度幅值**反比（0.35 归一），无色相区分。差量：独立**肤色 hue 区间**保护（中心/宽度）、亮度保护。肤色窗口先例：`Skin_Even_Matte.tglsl` BT.601 YCbCr 软窗（可作参考，非复用） | Color+Edit | 第三机 | L02 点处理子门 | plugins/Vibrance_Saturation.tglsl；plugins/Skin_Even_Matte.tglsl |
| F04 zoned_tint | E/L10 | 部分等价 | `Three_Way_Tint.tglsl`：Reinhard 色调坐标三分区，分界**编译期常量**（[0.25,0.55]/[0.55,0.85]）。差量：可移动分界 pivot/交叉 width 滑杆+分区强度；中性恒等语义保留 | Color+Edit | 第三机 | L02 点处理子门 | plugins/Three_Way_Tint.tglsl |
| F05 gamut_rolloff | N/L12 | 无重复 | 全库无 gamut 压缩项（`Color_Space_Transform.tglsl` 是 CSC+tone map，非 rolloff）。负值/超白策略显式 | Color | 第三机 | L02；色彩敏感声明按 M19.1 子集 | plugins/Color_Space_Transform.tglsl（对照） |
| F06 multiscale_halation | E/L11 | 部分等价 | `Halation_Extended.tglsl`：单尺度 13-tap ADR 核+HSV 重着色。`Film_Halation.tglsl` 是**空壳**（process 直通，329B）。差量：多尺度扩散、红色散射能量权重、阈值/能量控制；多 pass 用原生 renderer，不强塞 .tglsl | Color+Edit | 第三机 | L02 纹理邻域子门；多 pass 另开子门 | plugins/Halation_Extended.tglsl；plugins/Film_Halation.tglsl（空壳证据） |
| F07 seeded_grain | E/L12 | 部分等价 | `Film_Grain.tglsl`：hash value-noise，**无 seed/无 frame uniform/无彩色颗粒**（引擎不注入时间，文件注释明示）。差量：seed、彩色相关颗粒、动画模式。**动态模式依赖正式 frame/seed 子门（现不存在）；先发布静态版** | Color+Edit | 第三机 | L02；动态另过 frame/seed 子门 | plugins/Film_Grain.tglsl |
| F08 gate_weave | N/L12 | 无重复 | 无门片位移类。确定性帧身份（不读墙钟）；静态版先行 | Color+Edit | 第三机 | L02；动态过 frame/seed 子门 | plugins/（全目录无等价） |
| F09 dust_scratches | N/L12 | 无重复 | 无灰尘/划痕类。程序静态版先行；外部纹理版依赖资源子门+M20 闭包，后置 | Color+Edit | 第三机 | L02；外部纹理过资源子门 | plugins/（全目录无等价） |
| F10 starburst | E/L11 | 部分等价 | `Anamorphic_Streak.tglsl`：**仅水平 ±X** 14/22/30 tap。差量：多叶/多方向（blades 为有界常量档）、angle、length；与水平 streak 数值可区分 | Color+Edit | 第三机 | L02 纹理子门 | plugins/Anamorphic_Streak.tglsl |
| F11 lens_breathing | E/L12 | 部分等价 | `Lens_Distortion.tglsl`：静态 r² 多项式重映射+auto-zoom。差量：呼吸式**可动画**缩放/中心微移（静态参数+既有关键帧先行；不称自动估计焦距） | Color+Edit | 第三机 | L02；动画走既有 keyframe 通道 | plugins/Lens_Distortion.tglsl |
| F12 palette_dither | N/L10 | 部分等价（对照项） | `Pixel_Blocks.tglsl`：逐通道**均匀阶梯**量化+4x4 Bayer——无任意调色板最近色。差量：palette、固定蓝噪 pattern；蓝噪资源过资源子门，程序纹理先行 | Color+Edit | 第三机 | L02；资源子门（蓝噪） | plugins/Pixel_Blocks.tglsl |
| F13 print_registration | E/L11 | 部分等价 | `Halftone_Print.tglsl`：**单张网屏**（4 档角度 select）、无 CMYK 分离/套印偏移/纸底。差量三项参数全缺，先核已有参数不重复造 | Color+Edit | 第三机 | L02 纹理子门 | plugins/Halftone_Print.tglsl |
| F14 hatching | E/L11 | 无重复（算法级） | `Edge_Ink_Outline.tglsl` 是 Sobel 描边上墨，无排线。差量：亮度驱动交叉排线/纸纹；纸纹外部资源过资源子门，程序版先行 | Color+Edit | 第三机 | L02 纹理子门 | plugins/Edge_Ink_Outline.tglsl |
| F15 watercolor | N/L11 | 无重复 | 无水彩/扩散模型。有界核/多 pass；禁 blur+saturation 冒充 | Color+Edit | 第三机 | L02 纹理+多 pass 子门 | plugins/（全目录无等价） |
| F16 gradient_map | E/L10 | 部分等价（试点） | `Duotone_Toner.tglsl`：**仅 2 色停靠** shadow/highlight（已抽查证实）。差量：≥4 色停靠+positions+插值模式+mix。**当前 @param 四类无法表达数组**：试点以 4 color 参数+4 position 滑杆表达 4 停靠（ABI 内合法）；N 停靠需原生参数适配（对集成者的提案随试点交付） | Color+Edit | 第三机（试点） | L02 点处理子门 | plugins/Duotone_Toner.tglsl（L00 抽查） |
| F17 solarize | N/L10 | 无重复 | 全库无 solarize（Invert 是全反相，不同）。阈值折返/软过渡/通道选择/mix；强度零恒等 | Color+Edit | 第三机 | L02 点处理子门 | plugins/（全目录无等价） |
| F18 posterize_shared | W/L10 | 可复用 | Composite `Posterize` 节点已存在（`plugins/node_plugins/posterize_plugin.py` + GPU 核 `src/features/composite/core/shaders.py` `_POSTERIZE_FRAGMENT`，levels/channel_mode/白噪 dither）。差量：提供 Color+Edit 一致入口；共享核不反向依赖 Composite；不再造第二个 Posterize 节点 | Color+Edit | 集成者为主 | L02；L04 共享核摆放 | plugins/node_plugins/posterize_plugin.py；src/features/composite/core/shaders.py |
| F19 median_rank | N/L11 | 无重复（试点） | **全库无中值/排序滤波**（blur 五型 box/gaussian/bilateral/motion/bokeh；denoise=bilateral；sharpen=unsharp；均已核实）。3×3/5×5 常量档、percentile、luma/RGB 模式；独立手算排序 oracle | Color+Edit | 第三机（试点） | L02 纹理子门 | src/features/composite/nodes/blur_node.py; denoise_node.py; sharpen_node.py; plugins/Unsharp_Sharpen.tglsl |
| F20 convolution | N/L11 | 无重复 | 无自定义核（sharpen/edge_detect 均固定核）。kernel 编辑需原生 schema/UI（9/25 float 非 @param 可表达）；identity/负权重/零和核验收 | Color+Edit | 第三机 | L02 纹理子门+原生参数适配 | src/features/composite/nodes/edge_detect_node.py; sharpen_node.py |

F07/F08/F09 动态模式不依赖相邻帧，但依赖确定性 frame/seed 接口——无该接口只发静态模式；
真正时域（拖尾/光流模糊/时域降噪）需要历史帧 provider，不列入本轮。

## 2. 视频转场 X01–X18（18 家族，均 N）

现有基线：**33 视频 + 4 音频转场**（`transition_catalog.py`/`audio_transition_catalog.py`，已核实）。
去重结论：**10 个点名家族在现有 33 项中零等价实现**（全仓 grep + 逐实现读码核实）。
易误判项已排除：slide_* 是 wipe mask 揭示（双源画面均不移动，transition_mask.py:185-190，已抽查证实）；
spin_blur 是源侧旋转拖影非极角；checkerboard 仅存在于视口底纹/Constant 生成器等无关资产。

CPU mask 可复用基座：`transition_mask.py::_get_coord_grid`（aspect 校正中心网格，63-81）——极角/周期条带类的坐标输入。
**接缝事实**：现有管线只有"几何 mask × progress + left×(1-m)+right×m"单一合成形态；
双源 UV 重采样通道**不存在**（X04/X09-X17 的 L03 接缝）；
mask 参数无 seed 类参数（X06/X18 需新增）；mask 全部纯几何、不含图像内容项（X05 需内容驱动 mask 源）。

| ID/slug | 包/路径类型 | 等价度 | 差量要点（对现有 33 项） | 真实依赖 | 证据 |
|---|---|---|---|---|---|
| X01 clock_wipe | L13/mask | 无重复（试点） | 无极角计算（全仓转场用途 atan2=0）；iris 是径向距离、wipe 是线性投影 | L03 mask 子门 | transition_mask.py:210-227; transition_wipe.py:165-188 |
| X02 venetian_blinds | L13/mask | 无重复 | 现有唯一 mask 边界是 wipe 单边界，无周期条带 | L03 mask 子门 | transition_wipe.py:144-147 |
| X03 checker_reveal | L13/mask | 无重复 | 二维交错推进无实现（棋盘资产均非转场） | L03 mask 子门 | composite_viewport.py:11408（无关资产对照） |
| X04 staggered_tiles | L14/sample | 无重复 | 每块独立时序+UV 变化；需双源重采样通道 | L03 重采样子门 | transition_effects.py（无 UV 位移路径） |
| X05 luma_reveal | L13/mask | 无重复 | 内容驱动 mask（现有全几何）；需把源图像亮度接进 mask 源 | L03 mask 子门+内容源接缝 | transition_mask.py 全文 |
| X06 noise_dissolve | L13/mask | 无重复 | rank-field 揭示+seed；dissolve 族四项均全局混合无噪声场 | L03 mask 子门+seed 参数 | transition_catalog.py:25-92 |
| X07 voronoi_reveal | L14/mask | 无重复 | 全仓无 voronoi | L03 mask 子门 | grep 0 命中 |
| X08 halftone_reveal | L13/mask | 无重复 | 网点扩张 mask；`Halftone_Print.tglsl` 是单输入滤镜非转场（可作几何参考） | L03 mask 子门 | plugins/Halftone_Print.tglsl（对照） |
| X09 true_push | L13/sample | 无重复（试点） | slide=wipe mask 双源不动（已抽查）；whip_pan 仅模糊后 6% 行程 warp。**注意：时间线模型无媒体 handles，越界帧=邻接 clip 冻结边帧**（render_plan 1028-1048 钳制）——border 策略必须显式 | L03 重采样子门 | transition_mask.py:185-190; transition_effects.py:112-126; timeline_render_plan.py:1028-1048 |
| X10 hinged_doors | L14/sample+composite | 无重复 | 2D 投影双门+遮挡/背面/阴影模型 | L03 重采样+合成 | 无等价 |
| X11 cube_faces | L14/sample+composite | 无重复 | 两面透视旋转映射；不建 3D 引擎 | L03 重采样+合成 | 无等价 |
| X12 page_curl | L15/sample+composite | 无重复 | 非线性卷页曲面 UV；无 UV 弯曲类转场 | L03 重采样+合成 | grep 0 命中 |
| X13 pixelate_bridge | L13/sample | 无重复 | 动态量化时序（块内采样变粗→换源→恢复） | L03 重采样子门 | 无等价 |
| X14 displacement_bridge | L14/sample | 无重复 | RG/XY map 双源位移；**程序 map 先行；外部 map 过辅助 sampler 资源子门+M20 闭包** | L03 重采样+资源子门 | 无等价 |
| X15 ripple_refraction | L14/sample | 无重复 | 径向波场折射（UV 波场非 radial blur）；进出位移归零 | L03 重采样子门 | plugins/Radial_Blur_Pro.tglsl（对照排除） |
| X16 scanline_slices | L14/sample | 无重复 | 分行带 UV 错位/换源（非 RGB split：rgb_split 是通道平移非行带 UV） | L03 重采样子门 | transition_effects.py:462-488（对照） |
| X17 kaleido_bridge | L14/sample | 无重复 | 角域折叠桥接；`Kaleido_Mirror.tglsl` 是单输入折叠镜像非转场；两端原画语义 | L03 重采样子门 | plugins/Kaleido_Mirror.tglsl（对照） |
| X18 burn_reveal | L15/mask+composite | 无重复 | rank-field+焦边/亮边合成；与 X06 的区别由实际焦边/发光证明 | L03 mask+合成 | grep 0 命中 |

转场接缝清单（三端同步坑）：新增家族需同步 CPU（transition_mask/effects）→ GPU spec
（transition_mask_gpu）→ GLSL（gpu_transition_shaders）→ MSL（timeline_metal_export_renderer，
**注意其 iris kernel 仍是旧常数半径 1.55·progress 未跟上 AUD-16900**——三端不同步的现成先例）。
用户转场 base 白名单现 15 项（custom_transitions.py:44-50），新家族必须经 L03 注册后才可配方化。

## 3. 合成节点 C01–C21（21 项）

现有基线：静态 `NODE_REGISTRY` 99 键（1 延迟占位）+ 节点类 97 + 插件节点 3；
capability 表 99 条一一对应。**未知 `_effect_type` 警告后透传**（base_evaluator.py:2312-2314）——
L04 必须接真实 dispatcher，不能 marker 完事。
多输入先例：blur 的 image+velocity（RG 位移 map 契约）、keyer image+reference、light_wrap 双源、relight 三输入；
多输出先例：mask_merge/roto_shape/paint 的 result+matte（无 low/high 双彩色先例，C21 首开）。
node_type 命名空间 `lib1_*`（L01 冻结），`register_node_type` 拒绝覆盖已有键。

| ID | 分类/包 | 等价度 | Composite 现状 / Edit·Color 现状 | 真实依赖 | 证据 |
|---|---|---|---|---|---|
| C01 gradient_map | W/L16 | 部分等价（试点） | 无节点入口；.tglsl 侧 Duotone_Toner 2 色。复用 F16 多停靠核 | L04 单输入子门（依赖 L02 F16 子门） | colorlookup_node.py（曲线非映射，对照）；ramp_node.py（生成器，对照） |
| C02 rgb_matrix | W/L16 | 部分等价 | 无 3×3 矩阵节点（Shuffle/CopyChannel/ChannelMerge 无串扰系数）；.tglsl `Channel_Mixer.tglsl` 现成矩阵核 | L04 | channel_mixer 相关 nodes/（对照） |
| C03 despill | W/L16 | 部分等价 | **算法已存在于 Keyer 内**（keyer_node.py:91-95 despill_strength/method none-subtract-scale，hidden；chroma_keyer despill bool）。差量：独立节点+选择色+亮度恢复+mask | L04 | keyer_node.py:91-95; keyer_evaluator.py:158-169; shaders.py `_KEYLIGHT_FRAGMENT` |
| C04 solarize | W/L16 | 部分等价 | Composite 无（只有 Invert）；复用 F17 | L04（依赖 F17） | shaders.py 无 solarize |
| C05 mosaic | W/L16 | 部分等价 | 无 mosaic 节点；复用 `Pixel_Blocks.tglsl` 块中心采样；差量：grid origin/mask/border/非整块边缘 | L04 | pixel blocks 相关（对照） |
| C06 halftone | W/L16 | 部分等价 | 无；复用 Halftone_Print+F13 | L04（依赖 F13 纹理子门） | 同 F13 |
| C07 hatching | W/L16 | 部分等价 | 无；复用 F14 | L04（依赖 F14） | 同 F14 |
| C08 crt | W/L16 | 部分等价 | 无；复用 CRT_Scanlines；差量：辉光组合+显示域声明 | L04 | plugins/CRT_Scanlines.tglsl |
| C09 glitch | W/L16 | 部分等价 | 无；复用 Glitch_Blocks（**静态确定性**）；动态 frame 模式过 frame/seed 子门后才开 | L04 | plugins/Glitch_Blocks.tglsl |
| C10 kaleidoscope | W/L16 | 部分等价 | Kaleido_Mirror 是折叠镜像（2/4/8 重），**无扇区数旋转万花筒**（判定比计划口径更严：部分等价非可复用） | L04 | plugins/Kaleido_Mirror.tglsl |
| C11 polar_coordinates | N/L17 | 无重复 | 全库无极坐标变换（正/反向） | L04 | 无等价 |
| C12 vector_displace | N/L17 | 无重复 | 无图像驱动位移（mesh_warp 是 UI 手柄网格；velocity map 只用于模糊）。**首个多输入子门**；输入角色不可串线 | L04 多输入子门 | warp_node.py:37-56; blur velocity 契约 shaders.py:739 |
| C13 stmap | N/L17 | 无重复 | 无绝对 UV 重映射；与 C12 同子门；map 空间/精度/无效 UV 语义 | L04 多输入子门 | 无等价 |
| C14 tile_array | N/L17 | 无重复 | crop 的 outside=repeat 只是裁剪外填充，非阵列平铺；输出 bbox/空隙 alpha 显式 | L04 | crop_node.py:22-48（对照） |
| C15 median_rank | W/L16 | 无重复 | 无中值（依赖尚未存在的 F19 共享核——实际为全新实现，计划 W 分类按差量记录修正） | L04（依赖 F19） | blur_node.py:30-111 |
| C16 convolution | W/L16 | 无重复 | 无自定义核（同 F20 修正口径） | L04（依赖 F20） | edge_detect_node.py; sharpen_node.py |
| C17 edge_extend | N/L17 | 无重复（试点） | erode_dilate 的 edge_mode "extend" 是采样钳制策略**非 RGB 延伸**；light_wrap 是叠背景光。**首个 alpha 子门** | L04 alpha 子门 | erode_dilate_node.py:59; light_wrap_node.py |
| C18 distance_matte | N/L17 | 无重复 | 无距离变换（depth_matte 用 depth AOV 非距离场）；无法证明 tile-safe 即声明 full-frame | L04 全帧预算 | depth_matte_node.py（对照） |
| C19 fill_holes | N/L17 | 无重复 | matte_cleanup 仅 erode/dilate/blur/clamp/invert/levels，无孔洞填充；全帧算法预算/取消 | L04 全帧预算 | matte_cleanup_node.py:27-58 |
| C20 halation | W/L16 | 部分等价 | Glow 节点（threshold/radius/intensity/tint）基础可用；专用 halation 复用 F06 | L04（依赖 F06） | glow_node.py:29-37 |
| C21 frequency_separation | N/L17 | 无重复 | 无双彩色输出节点；**首个多输出子门**；low+high 重建=输入（声明域），high 允许负值 | L04 多输出子门 | mask_merge_node.py（result+matte 先例，对照）；HighPass_Retouch.tglsl（单输入频率分离，对照） |

判定汇总：部分等价 9（C01/C02/C03/C05/C06/C08/C09/C10/C20）、全新 12（其余）；
与计划的 13 入口/8 专用出入：C10 按扇区语义缺失降为部分等价、C15/C16 依赖尚未存在的共享核记为全新——**保留差量记录，分类以本表为准**。

## 4. 配方与宏 P01–P80（按计划 §7 分组登记）

配方不新增算法；每项依赖其实际使用算法过对应子门。范围级登记（四目标具名见计划 §7 表）：

| 范围 | 组 | 主要复用（已核实存在） | 阻塞依赖 |
|---|---|---|---|
| P01–P04 | 人像肤色 | 既有 WB/Vibrance/Three_Way（+F03/F04 可用后版本化增强） | 仅既有项即可先做 |
| P05–P08 | 胶片 look | Bleach_Bypass/Cross_Process/曲线（+F02） | F02 项后置 |
| P09–P12 | 时间/天气 | Day_Night_Converter/Grad_Filter_Exposure/曲线 | 仅既有项 |
| P13–P16 | 分色 | Three_Way/Duotone/曲线 | 仅既有项 |
| P17–P20 | 光效 | Soft_Glow_Bloom/Halation_Extended（+F06/F10） | F06/F10 项后置 |
| P21–P24 | 颗粒/灰尘 | F07/F08/F09 | 静态模式先做 |
| P25–P28 | 印刷/水彩 | F12/F13/F14/F15 | 对应 F 项 |
| P29–P32 | 量化/渐变 | F16/F18/F17+Duotone | 对应 F 项 |
| P33–P36 | mask 转场 | X01/X02/X03/X08 | 对应 X 项经 L03 |
| P37–P40 | push 四向 | X09 | X09 |
| P41–P44 | 噪声溶解 | X06 | X06 |
| P45–P48 | 亮度揭示 | X05 | X05 |
| P49–P52 | 采样类转场 | X15/X14/X16/X17 | 对应 X 项（X14 外部 map 用程序生成固定资源） |
| P53–P56 | 卷页/灼烧/晶胞/合页 | X12/X18/X07/X10 | 对应 X 项 |
| P57–P60 | 边缘整理宏 | C03/C17+既有 MatteCleanup/Keyer | C 项经 L04 |
| P61–P64 | 频率分离宏 | C21+既有 Grade/Mask/Merge | C21 |
| P65–P68 | 阵列/极坐标宏 | C14/C11+既有 Transform/Mask | C11/C14 |
| P69–P72 | 光效宏 | 既有 Edge/Glow/LightWrap/Text/Merge | 仅既有项 |
| P73–P76 | map 合成宏 | C12/C13+既有 Merge | C12/C13 |
| P77–P80 | 风格宏 | C06/C07/C08/C09+基础节点 | 对应 C 项 |

## 5. 附：共享接缝事实（L02–L04 的输入，L00 冻结口径）

1. **跨页 GLSL ABI 无别名层（L02 主缝）**：Edit/共享 runner 侧 `u_texture/u_resolution/v_uv/fragColor/参数=u_<name>`（dctl_wrapper.py:118-133 声明 u_<name> 但**不改写**插件体）；Color 侧 `u_input_tex/u_tex_size/u_aspect_ratio/v_texcoord/f_color/裸参数→u_P<n>_<name>`（shader_compiler.py:420-426 改写）。现有 33 插件按 Color 约定书写 → Edit 包装后编译必失败（undeclared identifier）→ 现状 warning+no-op。L02 修共享缝（别名/改写）后才能声明跨页。
2. **EffectCatalog 三源聚合**：7 内置（blur/sharpen/denoise/edge_detect/erode_dilate/defocus/glow，复用 Composite PARAM_DEFS+GLSL）+ node_plugins（Phase 3 未接线）+ `plugin:<id>` DCTL。
3. **能力 fail-closed**：`@export_support {all|video|opengl}` 白名单外（含 cpu/metal）只留 gpu_preview；插件不进 CPU_PREVIEW/EXPORT 白名单。LUT 烘出字段当前不存在（L20 新建矩阵轴）。
4. **插件身份**：`@version`/content_hash/parameter_schema_hash 已入 EffectSpec→EffectInstance.plugin_identity（M07.1 链路）；L01 复用该协议。
5. **转场三端同步**：CPU→GPU spec→GLSL→MSL；Metal iris 旧半径先例（timeline_metal_export_renderer.py:2872）为不同步警示。
6. **时间线无媒体 handles**：转场越界帧=邻接 clip 冻结边帧（timeline_render_plan.py:1028-1048）；X09/X10/X11 等 sample 类的 border 策略必须显式声明。
7. **.tai 缺模型**：4x_ultrasharp.onnx 不存在，AI_Upscale_4X 不可运行，不计入内容库。

## 6. L00 结论与去重调整

- 59 张工作单**无一因等价实现取消**；但分类修正 3 项：C10（可复用→部分等价）、C15/C16（W→实际全新，因 F19/F20 核尚不存在）。差量已记录，不复制文件凑数。
- 六试点确认全部为真实差量：F16（多停靠 vs 2 停靠）、F19（全库无中值）、X01（无极角）、X09（无双源位移）、C01（无节点入口）、C17（无 alpha 延伸）。
- 下一步：L01（身份/参数 schema/能力 manifest + 黄金夹具），随后六试点组件交付。
