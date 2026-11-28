# Topos Composite 对标专业合成软件差距审计与补齐计划（2026-10-13）

> 状态：**审计完成，待排期**（本文档为问题台账 + 补齐计划，执行进度直接勾选下文任务表）
> 审计方式：三路并行代码盘点 —— ①节点注册表与逐节点实现抽查（`core/node_engine.py`、`core/node_capabilities.py`、`nodes/**`）②图编辑器/查看器/曲线编辑交互（`ui/**`）③渲染引擎/缓存/色彩/时间求值/IO/导出（`render/**`、`core/**`、IO 层）。对照基线：Foundry Nuke、DaVinci Fusion、After Effects 的公开能力面。
> 审计范围：`src/features/composite/**`（293 个 Python 文件；节点注册表 93 项）。
> 编号体系：`CP-xxx`（gap/任务项，本文档唯一编号，勿与 AUD-xxx 混用）。

---

## 0. 现状底数（避免重复建设的基线认知）

骨架已达到专业合成器形态，以下能力**已具备**，计划中不再重复立项：

| 领域 | 已有能力 | 证据 |
|---|---|---|
| 求值模型 | 请求驱动（pull-based）+ ROI 请求传播 + 1024² tile 分块 + LOD/proxy 分级 | `core/time_remap.py:10`、`core/tile_scheduler.py:66`、`render/render_scheduler.py:951` |
| GPU 管线 | 独立 GL 上下文渲染线程 + 纹理池 + PBO 异步回读 + compute shader（radius>16 切换）+ macOS Metal 桥 | `render/composite_renderer.py`、`render/compute_shader_manager.py:29`、`render/metal_compute_bridge.py` |
| 并行导出 | 多帧并行渲染线程池 + 有序重排缓冲，N=1/4 结果确定性有测试 | `render/parallel_frame_render.py:40-52` |
| 缓存 | 内容寻址键（参数+上游+ROI+帧）内存 LRU + 20GB 磁盘 LRU + 自有 `.toos` 缓存编码 + 参数趋势预取 | `render/cache_manager.py:22-60`、`render/disk_cache_store.py:60` |
| ROI/烘焙 | viewer ROI 局部渲染；子树 bake/Unbake | `composite_viewport.py:5521`、`bake_service.py` |
| 图编辑 | Tab 搜索/右键分类菜单/连线插入（insert-on-wire 两种方式）/框选/复制粘贴/group/backdrop/sticky/dot/undo 全命令化/minimap/视图书签/自动布局/网格吸附 | `ui/node_editor_widget.py:2155-2200`、`node_editor_actions.py:687-1066` |
| 查看器 | A/B 快照 + wipe + 差值热图、7 档通道查看、exposure/gamma、放大镜、像素检查条、棋盘/黑/白背景、代理档位（交互自动降档）、on-screen 手柄全套（含修饰键约束与吸附）、专业示波器（waveform/vector/parade/CIE/falsecolor，与调色页共享）、3D 视口（orbit 模式/着色模式/隔离/gizmo） | `ui/composite_viewport.py`、`ui/viewer_tool_system.py`、`ui/composite_scope_widgets.py:13` |
| 动画 | 6 种插值 + 6 种切线模式 + 外插 constant/linear + AE 风格表达式（wiggle/loopIn/valueAtTime）+ dope sheet/曲线双模式 + 关键帧批量缩放/复制 | `src/shared/keyframe.py:25-50`、`ui/spline_panel.py:375-395`、`ui/keyframes_timeline_view.py` |
| 时间 | 上游 source 侧时间重映射闭式（speed/offset/hold/reverse/loop 组合）、RIFE 双帧补帧节点 | `core/time_remap.py`、`nodes/frame_interpolate_node.py` |
| IO 读 | EXR 多 part（含 Z）、DPX/TIFF16/PNG16/HDR/RAW(调色页)、PTS 精确 seek 视频解码（含硬解） | `core/image_loader.py:139-142`、`shared/media/ffmpeg_source.py:1449` |
| IO 写 | PNG/JPEG/TIFF/EXR(8/16/32 + 4 种压缩)；视频 H264/H265/ProRes 4444XQ/DNxHR/FFV1/VP9/AV1/GIF + 硬编；bt709/bt2020 元数据 | `nodes/file_output_node.py:499-516`、`shared/export/encoder.py:41-68` |
| CLI | 无头渲染（帧选择/格式/AOV/JSON 输出），与 GUI 同路径保证像素一致 | `scripts/topos_render.py` |
| 节点规模 | 93 项注册：约 84 个完整实现（含 5534 行 PBR 3D 渲染器、Keylight 风格键控、cryptomatte、RIFE、LaMa 等重器） | `core/node_engine.py:131-246` |

**统计口径**：93 项 = 91 个具体类型（blend/merge 同类、motion_graphic 延迟注册）+ AI 占位 4 个 + unknown_placeholder（不在注册表）。其中完整 ~84、简化 5（defocus / relight / retime / group / paint 帧范围）、占位 4（AI 家族）。

---

## 1. 确定性缺陷（低成本、应最先修复）

这类是审计中发现的**实际 bug/遗漏**，不是能力差距：

- [x] **CP-001 · matte_cleanup 未列入节点分类菜单**：已注册（`node_engine.py:201`）且有完整 morphology+levels 实现，但 `NODE_CATEGORIES`（`node_engine.py:250-373`）Keyer 分类下没有它 → 用户无法从 Add Node 菜单创建，只能靠反序列化出现。修复=分类表补一行 + 一致性测试（现有 `check_registry_consistency()` 只查注册表，建议同时查分类覆盖）。
- [x] **CP-002 · 官方节点插件 import 路径失效**：`plugins/node_plugins/posterize_plugin.py:14-15`、`vintage_plugin.py:20-21` 仍 `from src.composite.node_plugin import ...`（旧路径，已迁 `src/features/composite`）→ 加载必然 ImportError，节点插件通道实际为零可用插件。修复=改 import + 加载冒烟测试。
- [x] **CP-003 · paint 节点 frame_range 不参与求值**：strokes 的 `frame_range` 已解析/序列化但求值器不按帧过滤（`nodes/paint_node.py:25-27` 自述 "MVP scope"）→ 笔画"永生"。修复=求值时按当前帧过滤 stroke 生命期。
- [x] **CP-004 · 渲染对话框未暴露 parallel_frames**：多帧并行已实现（`composite_render_output.py:340` API 参数），对话框无控件 → 导出只用默认串行/低并行。修复=对话框加"并行帧数"spinbox。
- [x] **CP-005 · 导出对话框 codec 列表是引擎子集**：引擎支持 H264/H265/ProRes/DNxHR/VP9/AV1/GIF，UI 只有 h264/hevc/prores/vp9（`ui/composite_render_dialog.py:63-68`）。修复=列表对齐 + 容器联动。
- [x] **CP-006 · EXR 压缩因 opencv 轮子 libdeflate SIGILL 回避 ZIP/ZIPS/PIZ**（`file_output_node.py:503-506`）：技术债，升级轮子或换 OpenEXR 官方绑定后恢复完整压缩枚举。
- [ ] **CP-007 · CLI 无头渲染不支持视频编码**：`topos_render.py` 仅 exr/png/jpg 序列帧 → 农场式多开 CLI 只能出序列。修复=接 `render_video` 路径。
- [x] **CP-008 · model3d 文件头"提交 1：占位骨架"注释已过时**（实现已完整，1243 行）→ 误导后续维护调研。修复=清理注释（零风险文档级）。
- [x] **CP-009 · group 节点子图执行未实现却可创建**：dispatch 显式透传 + warning（`base_evaluator.py:2255-2262` 的 `PLACEHOLDER_EFFECT_TYPES={"group"}`）。短期=图菜单标注"仅组织用，不参与求值"；长期见 CP-031。
- [x] **CP-010 · defocus 名不副实**：实为"屏幕位置驱动变半径高斯"，无 depth 输入、无真实 bokeh disc（`defocus_node.py:1-13`；真景深在 zblur）。短期=改名/文档标注；长期=补 depth 输入版。

---

## 2. 缺失节点清单（对标 Nuke / Fusion / AE）

按"对标软件中属于日常主力"到"长线能力"排序。优先级：P1=主力缺口（专业用户第一天就会找）、P2=常用但可绕过、P3=长线。

### 2.1 Merge / 合成（P1）

- [x] **CP-011 · Keymix**（Nuke）：用 matte 在 A/B 间混合，键控返修日常件。现有 blend+mask 可近似，但缺独立节点与 mask 通道语义。
- [x] **CP-012 · Merge 操作集核对与补全**：现有 15 种 blend 模式（`nodes/blend_node.py`），需核对是否含 Nuke 标配的 `stencil / mask / in / out / atop / conjoint / disjoint / from`（matte 布尔族），缺则补——这些是合成语义而不仅是混合模式。
- [ ] **CP-013 · ContactSheet**（拼贴联络表）：审片/素材核对用，也是多 AOV 对照查看的低成本方案。

### 2.2 色彩（P1）

- [x] **CP-014 · ColorLookup（RGB 独立曲线查找）**：合成图内没有任何 per-channel 曲线调整节点（huecorrect 是色相域）。Nuke ColorLookup / AE Curves 是高频节点。曲线 UI（spline 体系）已有，缺节点化接线。
- [x] **CP-015 · CDL（ASC CDL：slope/power/offset/sat）**：与调色部门往返的标准交接格式（.cdl 文件读写）。
- [x] **CP-016 · HSVTool / 饱和度-色相-明度独立控制**：核对完成（2026-10-14）——color_correct 已含 hue_shift/saturation/temperature/tint + 三区 lift/gamma/gain，HSVTool 的核心（H/S/V 独立控制）已覆盖；按色相区间选择性调整属 huecorrect 域。无需新节点。
- [ ] **CP-017 · ColorTransfer**（从参考图迁移色彩统计）：Nuke/Fusion 均有，匹配镜头用。
- [ ] **CP-018 · ExpToLog / LogToExp**（胶片密度往返）：cs_transform 仅少量预设（sRGB↔linear 等），无 Log 家族。

### 2.3 滤镜（P1–P2）

- [x] **CP-019 · Grain / DeGrain**（P1）：合成图内完全没有胶片颗粒节点（调色页有 Film_Grain.tglsl 插件但不进合成图）。Grain(带 size/softness/动画种子) + DeGrain(去颗粒) 是实拍合成刚需。
- [x] **CP-020 · VectorBlur**（P1）：运动矢量图驱动的 2D 运动模糊（引擎审计确认无此节点；3D 已输出 MOTION_VECTOR AOV 却没有消费端）。transform 运动模糊只有子帧采样、blur 的 velocity 输入需要手喂矢量。
- [x] **CP-021 · 全局 shutter 设置**（P1）：运动模糊只有 transform 节点级 shutter，无项目级 shutter/offset 统一控制。
- [ ] **CP-022 · Median / Convolve（自定义核）**（P2）：去椒盐/边缘保真的常用小件；erode_dilate 形态学已有。
- [ ] **CP-023 · TimeBlur（时间域平均）**（P2）：与 deflicker 的时间环缓冲基建可复用。
- [ ] **CP-024 · STMap / 畸变数据导出导入**（P2）：lens_distortion 节点有，但 matchback 工作流（STMap 网格往返、与 3D 追踪软件互认畸变模型）缺失。

### 2.4 键控（P1–P2）

- [ ] **CP-025 · IBK 级键控**（IBKGizmo/IBKColour 思路：背景色估计而非屏幕色抑制）：绿幕重灾区场景 Keylight 风格之外的第二条路。中期项。
- [ ] **CP-026 · 独立 HueKeyer / 曲线级 despill 控制**（P2）：keyer 的 despill 是参数式，无 per-hue 曲线控制。

### 2.5 Roto / 蒙版（P1 交互、P2 数据）

- [ ] **CP-027 · roto 16 点上限**：`roto_shape_node` 上限 16 点（`roto_shape_node.py:98-152`），专业 roto 一条边就超。需评估数据结构是否真有硬限制还是校验过紧。
- [ ] **CP-028 · 逐点 feather 的 viewer 交互**：数据模型已支持逐点 6 通道（x/y/c1/c2/feather 可动画），但 viewer 里 feather 是形状级单一参数（`viewer_handles.py:1188`，clamp 0-0.5）→ 能力在、交互没接到。补 viewer 内逐点 feather 手柄 + feather offset。
- [ ] **CP-029 · 单节点多形状布尔**（Nuke 一个 Roto 多 shape 相互 +/-/intersect）：核对 mask_merge 是否已覆盖节点间场景，节点内多形状为缺口。

### 2.6 通道 / 工具（P2）

- [ ] **CP-030 · Channel Boolean（通道级位运算合成）**、**AddChannels**（copy_channel 覆盖部分场景，需核对）。
- [ ] **CP-031 · group 子图执行**（P1，与 CP-009 呼应）：Group 目前仅 UI 容器。Nuke Group=可进入可执行的子图，是大型工程组织的基础；同时联动**外部 precomp 引用**（见 CP-049）。
- [ ] **CP-032 · 元数据系统**：全仓无 metadata 节点/查看器（CopyMetaData/ViewMetaData 对标）。EXR metadata 读写透传也需核对。

### 2.7 时间（P1）

- [ ] **CP-033 · 光流变速 retime**（设计已定，实现挂账 Batch 2）：`use_optical_flow` 是未实现元数据（AUD-7209 已挂账）；frame_interpolate 是双帧 RIFE，不是变速曲线。**实现设计（2026-10-14）**：需「双子帧上游求值」——对目标帧 t 的源位置 s∈(n, n+1)，按 `_try_transform_motion_blur` 的子帧采样先例求值上游 floor(s) 与 ceil(s) 两帧，交 RIFE 以 t=s-n 插值；涉及求值器上游重求值，属引擎级手术，不宜压缩实现。临时手工等价链：retime + 两个 time_offset + frame_interpolate(t 动画)。

### 2.8 3D / Deep（P3 长线）

- [ ] **CP-034 · 真 Deep 数据管线**：当前 deep_merge 是 RGBA+Z 的 2.5D 深度感知合成，非 deep sample buffer；无 DeepHoldout/DeepToImage 族、不能读写 deep EXR。属架构级长线。
- [ ] **CP-035 · CameraTracker（3D 相机反求）**：现有 2D tracker（translation/trs/planar）+ bake，无反求。可与 pointcloud3d/scene3d 打通成长线方向。
- [ ] **CP-036 · 立体/多 view 工作流**：JoinViews/SplitViews/OneView/Anaglyph 全缺。若产品定位不含立体，可明确降级为"不支持"并记档。
- [ ] **CP-037 · 深度几何工具族**：PositionToPoints/DepthToPosition 等（Nuke 深度 AOV 生态件），视 3D 线投入决定。

### 2.9 音频（P1 定位决策 / P2 实现）

- [ ] **CP-038 · 合成页音频参考**（决策记录，实现挂 Batch 2/3）：整条合成链无音频处理证据，播放无声。AE/Nuke/Fusion 均有音频参考（波形/scrub/跟随播放）。**建议决策（2026-10-14）**：采纳"最小可用"路线——(1) 视频源音频跟随播放（QMediaPlayer/QAudioSink 旁路，与播放头锁定，不做图内处理）；(2) 时间线波形只读显示；(3) 图内音频节点/混音明确列为不支持并记档（沿用 docs/video 边界冻结惯例）。等用户确认后实施。

---

## 3. 现有节点功能缺口（节点在、深度不够）

| 编号 | 节点 | 缺口 | 优先级 |
|---|---|---|---|
| CP-039 | relight | 仅 Lambert 漫反射（自述 "MVP is diffuse-only"），specular/roughness 参数槽预留未实现 | P2 |
| CP-040 | cs_transform | 仅少量预设色彩空间；无任意 OCIO 色彩空间选择（与 CP-044 联动） | P1 |
| CP-041 | particle | 仅 2D 无状态点粒子（dot/soft_dot、单色、无 sprite/纹理/3D） | P3 |
| CP-042 | pointcloud3d | CPU 光栅化、固定正交 fit-to-frame，未作为 GL_POINTS 进 Scene3D | P3 |
| CP-043 | volume3d | 仅 .npz 密度体 CPU raymarch；OpenVDB/GPU/自发光未过打包门槛 | P3 |

（defocus / retime / paint / group 的缺口已入 §1 缺陷项，不重复。）

---

## 4. 节点通用能力缺口（与 Nuke 差距最大的结构性一项）

- [x] **CP-044 · 通用 mask 输入**（P1，结构性）：Nuke 每个效果节点都有 mask 输入；Topos 仅 12 个节点有（blend/merge、deep_merge、grade、color_correct、huecorrect、lut、godrays、set_alpha、face_smooth、ai_remove + 两个 AI 占位）。其余全部滤镜节点（blur/glow/erode/zblur/transform/…）无法局部作用。实现思路：BaseNode 层统一注入 mask 端口 + 求值器统一 mask apply pass（对 fusible 节点需评估 mega-shader 融合路径）。
- [x] **CP-045 · 通用 mix 滑杆**（P1）：Nuke mix 为全节点通用属性；Topos 仅 9 个节点有。与 CP-044 同层实现。
- [ ] **CP-046 · 通用 channels 选择**（P2）：效果作用于哪些通道（Nuke channels 下拉）目前仅 blur（R/G/B/A 开关）与 merge（mask_channel）局部支持；专用节点(shuffle 族)之外无统一语义。
- [ ] **CP-047 · BBox（数据窗口）语义**：现有 format/crop 体系，未发现 Nuke 式 BBox 传播（大画布小元素不付全帧代价）。ROI 体系已具备一半地基，需评估 BBox union/intersect 策略（capability 表的 roi_policy 可扩展）。核对后立项。

---

## 5. 图编辑器交互缺口

来自交互盘点 Top 项（证据见 §8 索引）：

- [ ] **CP-048 · 连线走线样式单一**：仅 cubic Bezier + 流动动画，无 straight / horizontal-vertical / 直角选项（Nuke/Fusion 均可切换）。P2。
- [ ] **CP-049 · 外部 precomp 引用**：precomp 目前仅内部 LayerStack 边界节点，不能把外部 `.topos` 工程文件作为引用合成（Nuke 的 Read/Write 与外部 .nk 引用生态）。P1（大型工程协作刚需），与 CP-031 联动。
- [ ] **CP-050 · 双击交互习惯**：无双击进入 Group / 双击节点聚焦属性 / 双击空白加节点（仅右键菜单路径）。P2。
- [x] **CP-051 · 参数级复制/粘贴与 pick-whip**：剪贴板仅节点级（`node_editor_actions.py:771`），无单参数值拷贝、无 AE 式 pick-whip 参数链接（表达式引擎已具备，缺交互）。P1。
- [ ] **CP-052 · 表达式编辑器无函数补全/参考面板**（`graph_expression_editor.py` 仅错误提示）。P2。
- [ ] **CP-053 · 节点工具栏 Palette/库面板拖拽创建**：创建仅 Tab 搜索 + 右键菜单 + 少量快捷键（Fusion 式库面板缺失）。P3（Tab 搜索已较好）。
- [ ] **CP-054 · dope sheet 无用户 marker**：markers 仅只读 overlay 从外部传入，不能在时间线上添加/拖动/命名。P2。

---

## 6. 查看器 Viewer 缺口

- [x] **CP-055 · viewer 无 gain/lift/offset 色彩控制**：仅 exposure stops + gamma 预设（`composite_viewport.py:524-542`）；Nuke viewer 是完整 gain/gamma/lift/offset 组合。P1（抠像检查刚需）。
- [x] **CP-056 · 吸管取色写回参数**：PixelInspectorBar 只读显示，不能把像素色填入节点 color 参数（AE/Nuke 标配）。P1。
- [ ] **CP-057 · 无标尺/测量工具**：仅有安全框 guides overlay。P3。
- [x] **CP-058 · 播放无变速/无 pingpong**：固定 fps、work-area 单向循环（`keyframes_panel.py:2827-2833`）；无 0.5×/2×/倒放。P2。
- [ ] **CP-059 · 检查器分辨率下 4K 播放性能目标**：已有代理/LOD/预取基建，但无回放帧率指标面板常驻显示（fps counter）。核对 perf_panel 是否可在 viewer 角落常驻。P2。

---

## 7. 渲染 / 缓存 / 色彩 / IO / 导出缺口

### 7.1 引擎

- [ ] **CP-060 · 交互求值单渲染线程**：`_build_parallel_groups` 并行分组只做计划不并行执行（`render_scheduler.py:783`）；多帧并行仅存在于导出路径。交互帧内多 tile/多分支并行是长线优化。P3。
- [ ] **CP-061 · 无纯 CPU 软件渲染回退**：全图求值必须 GL 上下文（Nuke 有 CPU 渲染模式；对无 GPU 机器/CI 截图/农场异构有意义）。P3。
- [ ] **CP-062 · 全局 motion blur 设置**：并入 CP-021。

### 7.2 缓存

- [ ] **CP-063 · 无用户可见缓存仪表盘/浏览器**：Nuke cache 仪表盘、清除/锁定/预充工具的对应物缺失（PerfPanel 只有统计）。磁盘缓存是节点 opt-in（`cache_evaluator.py:84`）。P2。
- [ ] **CP-064 · 回放预取是媒体层 + 参数预取两套**，非图级回放方向预测预取（播放时按播放方向预计算下游帧）。P2。

### 7.3 色彩管理（审计公认最大差距域）

- [ ] **CP-065 · 合成图内接入 OCIO v2 GPU processor**（P1）：目前 OCIO 只覆盖导出编码与调色页（`shared/color/color_transform_provider.py`）；合成图求值用固定 shader，工作空间硬编码 BT.709 线性。Nuke 的 OCIO 是全图节点级能力。
- [ ] **CP-066 · ACES 全流程**（P1–P2）：无 ACES2065-1(AP0)/ACEScc/RRT+ODT 内建；`ocio/minimal.ocio` 自述"要完整电影管线请外接 ACES Studio Config"。需决策：随包发行 ACES config（体积/许可）或文档引导外接。
- [ ] **CP-067 · 项目级工作空间可选**：当前 `core/color_space.py` 仅 LINEAR/SRGB/DISPLAY/UNKNOWN 四标签（自述 Stage 1，Stage 2 才做 ACEScg）。应支持项目设置中选工作空间，IDT/ODT 全链生效。

### 7.4 IO

- [x] **CP-068 · 多层 EXR 写出**：读支持多 part，写仅单层 RGBA（AOV 需多 part/多通道写出——CLI 的 `--aov` 出多文件而非多通道单文件）。P1（3D 合成往返）。
- [ ] **CP-069 · DPX 写出**（挂账：需参考文件比对）：只读不写（`image_loader.py` 有读，file_output 无 DPX）。**2026-10-14 实勘**：本机 cv2 轮子无 DPX 编码器（读也存疑）、无 imageio/freeimage；手写 SMPTE DPX 头原型未通过 PyAV 解码（头部细节待与参考文件比对），为避免产出不可读文件不强行合入。建议后续经 imageio+freeimage 插件或与参考 DPX 逐字节比对后落地。
- [ ] **CP-070 · PSD 读入**：不支持（分层 PSD 导入拆层是 AE 高频入口）。P2–P3。
- [ ] **CP-071 · deep EXR 读**：并入 CP-034。
- [ ] **CP-072 · EXR metadata（keys/attributes）透传读写**：与 CP-032 元数据系统联动，需核对现状。P2。

### 7.5 导出与渲染管理

- [ ] **CP-073 · 渲染队列**（多任务排队/后台连续渲染/完成后动作）：无（`composite_render_worker.py` 单任务）。P2。
- [ ] **CP-074 · 渲染农场协议**：无队列管理器/无 Tractor/Deadline 类集成；现状=手动多开 CLI（`topos_render.py:226` 自述）。P3（依赖 CP-007）。
- [ ] **CP-075 · 单次多输出**：图内多 output 节点可行，对话框一次只导一路（`composite_render_dialog.py`）。P2。

---

## 8. 任务总表（按优先级收敛的执行批次）

> 勾选即表示完成。每批建议附验证记录（现有惯例：FIXLOG/对账行）。

### Batch 0 · 快修（半天~2天/项，全部来自 §1）

> ✅ 2026-10-14 完成除 CP-006（EXR 压缩枚举需换绑定）/CP-007（CLI 视频）外的全部 8 项；CP-006/007 不在本批表内，随 Batch 2 处理。测试：`tests/composite/test_node_plugin_smoke.py`（6）、`test_paint_node.py::TestPaintStrokeFrameGating`（5）、`test_render_dialog_cp_batch0.py`（9）、`test_node_capabilities.py` 分类覆盖新增（2）。

| 编号 | 任务 | 预估 |
|---|---|---|
| CP-001 | matte_cleanup 补入分类菜单 + 注册表/分类一致性测试 | 0.5d |
| CP-002 | 修复两个官方节点插件 import + 加载冒烟测试 | 0.5d |
| CP-004 | 渲染对话框暴露 parallel_frames | 0.5d |
| CP-005 | 导出 codec 列表对齐引擎 | 0.5d |
| CP-008 | model3d 过时注释清理 | 0.1d |
| CP-009 | group 节点 UI 标注"不参与求值" | 0.5d |
| CP-010 | defocus 命名/文档标注（长期项另立） | 0.2d |
| CP-003 | paint frame_range 帧过滤求值 | 1d |

### Batch 1 · 主力能力（P1，专业用户日常缺口）

| 编号 | 任务 | 预估 |
|---|---|---|
| CP-044 | 通用 mask 输入（BaseNode 层统一） | 5d |
| CP-045 | 通用 mix（与 CP-044 同 PR） | 1d |
| CP-014 | ColorLookup 曲线节点（复用 spline 体系） | 3d |
| CP-020+021 | VectorBlur 节点 + 全局 shutter（消费 MOTION_VECTOR AOV） | 4d |
| CP-019 | Grain / DeGrain 节点 | 3d |
| CP-011+012 | Keymix + Merge matte 布尔操作族核对补全 | 2d |
| CP-055 | viewer gain/gamma/lift/offset | 2d |
| CP-056 | 吸管取色写回参数 | 1d |
| CP-051 | 参数级复制粘贴 + pick-whip | 3d |
| CP-012 核对 / CP-016 核对 | merge 操作集 / color_correct HSV 覆盖核对 | 1d |
| CP-068 | 多层 EXR 写出（AOV→多 part） | 4d |
| CP-033 | 光流变速 retime（复用 RIFE） | 5d |
| CP-038 | 音频参考：定位决策 + 最小实现（视频源音频跟随播放） | 5d |

### Batch 2 · 工作流与生态（P2）

| 编号 | 任务 | 预估 |
|---|---|---|
| CP-015+018 | CDL 节点（.cdl 读写）+ Log 族变换 | 3d |
| CP-040+065 | cs_transform 接 OCIO GPU processor | 5d |
| CP-066+067 | ACES/工作空间可选（决策 + config 分发方案） | 5d |
| CP-049 | 外部 .topos precomp 引用 | 5d |
| CP-031 | group 子图执行 | 8d |
| CP-027+028 | roto 点数上限放开 + 逐点 feather viewer 交互 | 4d |
| CP-013 | ContactSheet | 2d |
| CP-024 | STMap 畸变往返 | 3d |
| CP-046 | 通用 channels 选择 | 3d |
| CP-006 | EXR 压缩完整枚举（换绑定） | 2d |
| CP-069 | DPX 写出 | 1d |
| CP-073+075 | 渲染队列 + 多输出对话框 | 5d |
| CP-063+064 | 缓存仪表盘 + 回放方向预取 | 5d |
| CP-005/058/048/050/052/054 | 播放变速、连线样式、双击交互、表达式补全、dope sheet marker | 各 1-2d |

### Batch 3 · 长线（P3，按产品方向决策后立项）

CP-034（真 Deep）、CP-035（CameraTracker）、CP-036（立体，或明确"不支持"记档）、CP-037（深度几何族）、CP-041（粒子升级）、CP-042/043（点云/体积进 Scene3D）、CP-060（交互并行）、CP-061（CPU 回退）、CP-074（农场协议）、CP-070（PSD）、CP-053（节点库面板）、CP-057（标尺）。

---

## 9. 需产品决策的事项（不是纯技术项）

1. **CP-038 音频**：合成页要不要音频参考？影响时间线架构，宜早决策。
2. **CP-066 ACES 分发**：随包发行 ACES Studio Config（体积+更新责任）vs 文档引导用户外接。
3. **CP-036 立体**：明确支持/不支持，不支持则在文档记边界（项目有"边界冻结"惯例，见 `docs/video/V8_V9_release_matrix.md`）。
4. **CP-034 真 Deep**：架构级投入，与 3D 线（scene3d 已 5534 行）的路线图排序。
5. **CP-010 defocus 定位**：补深度输入版 or 保留为"屏幕位置景深"并改名。

---

## 10. 证据索引（关键文件）

- 节点注册/能力：`src/features/composite/core/node_engine.py:131-373`（93 项注册 + 16 类分类）、`core/node_capabilities.py:121-1110`
- 求值分发：`src/features/composite/evaluators/base_evaluator.py:2049-2270`（含 `PLACEHOLDER_EFFECT_TYPES`）、`evaluators/shader_library.py`
- mask/mix 覆盖面：`grep 'Port("mask", "input"' nodes/*.py`（12 节点）、`grep '"mix"' nodes/*.py`（9 节点）
- 交互：`ui/node_editor_widget.py:2155-2200`（快捷键表）、`ui/composite_viewport.py:524-542`（viewer 色彩控制）、`ui/viewer_handles.py:1188`（feather 单参数）
- 色彩：`core/color_space.py:32-51`（Stage 1 自述）、`ocio/minimal.ocio:40`（无 ACES 自述）、`shared/color/color_transform_provider.py`
- IO：`core/image_loader.py:139-142`（读格式矩阵）、`nodes/file_output_node.py:499-516`（EXR 压缩）、`ui/composite_render_dialog.py:63-68`（codec UI 子集）
- 导出/CLI：`render/parallel_frame_render.py:40-52`、`scripts/topos_render.py:226-228`

## 11. 复查记录（2026-10-14，隐藏 bug 清单）

对已勾选批次做的一次全量代码复查，发现并修复 7 处隐藏缺陷（全部带回归测试）：

| # | 位置 | 缺陷 | 修复 |
|---|---|---|---|
| 1 | `blur_evaluator._apply_vectorblur` | 无 velocity 输入时落入 blur_motion 的"角度×半径"路径（radius 默认 5px），VectorBlur 变成意外水平模糊 | 无有效 velocity 纹理时直通 |
| 2 | `file_output_node._save_to_disk` EXR 绑定路径 | BGR→RGB 用全数组反转，BGRA 变 ARGB（alpha 挪到首位）、灰度路径同样被污染 | 仅换前 3 通道，alpha 保末位 |
| 3 | `file_output_node._write_multipart_exr` | `_prepare_for_export` 输出的 BGR(A) 直传期望 RGB 的 multipart writer，beauty 通道红蓝互换 | 写出前显式换序（同 #2 规则） |
| 4 | `exr_multipart_writer.write_exr_multipart` | 3 通道 RGB beauty 缺 alpha 时 `[:, :, 3]` 越界，被 except 吞掉后静默回落单层 | 自动补不透明 alpha |
| 5 | `composite_render_dialog._VIDEO_CODECS` | GIF 列入 UI 后实测 `avcodec_open2("gif")` EINVAL（yuv420p/rgb24 均拒）——导出时硬失败；同时实测确认 dnxhr/ffv1/av1/prores 经项目编码器全部可用 | GIF 移出对话框（引擎枚举保留） |
| 6 | `node_properties_panel._show_pick_whip_dialog` | `display_title` 是 property（返回 str），被当函数调用 → 打开对话框即 TypeError | 直接取属性值 |
| 7 | `keyframes_panel.set_playing_state` | 停止播放不清变速累加器/往复方向，下次播放从残留半帧偏移或反向开始 | 停止时重置 |

另核验通过（无需修改）：generic mask shader 花括号与 marker 清理（`_filter_marker_keys` 过滤 `_generic_mask`）、吸管 `_compute_uv_at_mouse` 签名、播放按钮所在工具栏引用、keymix/dissolve 双输入 marker 与通用后处理互不干扰、新字段 `CompositeEvalContext.motion_blur_shutter` 的默认值不破坏既有构造。

> 对账口径：本文档任务共 75 项（CP-001…CP-075，其中 CP-062 并入 CP-021 计一次、CP-071 并入 CP-034 计一次）。执行时按批勾选，FIXLOG 引用编号。
