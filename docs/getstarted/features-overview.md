# 功能总览 / Feature Overview

> getstarted · 计划 04 P3（2026-09-28）。本文内容整理自 `README.md` / `README_EN.md`（迁移瘦身），未新增事实。
> 全部文档导航见 [docs/INDEX.md](../INDEX.md)；架构分册见 [docs/architecture/](../architecture/)。
> 本文为双语文档：简体中文在前，English follows below。

## 中文

### 1. 产品变体

Topos 采用**产品变体架构**，同一代码库构建四个专业产品：

| 产品 | 功能组合 | 用途 |
|------|----------|------|
| **Topos Studio** | Color + Edit + Composite + Deliver + Teaching Animation | 全功能版本，专业影视后期与动态图形 |
| **Topos Color** | Color + Deliver (轻量) | 调色专注版，摄影师/调色师 |
| **Topos Edit** | Edit + Deliver + Color (查看器) | 剪辑专注版，视频剪辑师 |
| **Topos Composite** | Composite + Deliver + Color (查看器) + Teaching Animation | 合成专注版，特效与动态图形 |

另有两个独立 CLI 工具（随 `pip install -e .` 安装）：

| 命令 | 用途 |
|------|------|
| **toposctl** | AI 编辑协议 CLI：`read` / `validate` / `preview` 经与 UI/MCP **同一 ToolFacade** 路径执行，写操作一律由 GUI/ApprovalService 签发授权 |
| **topos-vault** | 素材资产库 CLI：素材 AI 打标（`index`）、移动后重连（`relink` 三级匹配）、provider 列表与依赖状态 |

产品形态的功能开关与测试矩阵见 [docs/product_test_matrix.md](../product_test_matrix.md)。

### 2. 项目核心架构

项目采用 **"产品变体 + 特性模块 + 共享基础设施 + 原生编解码核心"** 分层架构：

```
┌─────────────────────────────────────────────────────────────────┐
│                         Products Layer                          │
│  topos-studio | topos-color | topos-edit | topos-composite      │
├─────────────────────────────────────────────────────────────────┤
│                        Features Layer                           │
│ Color │ Edit │ Composite │ Deliver │ TeachingAnim │ AI/Workflow │
│                     Scripting (Automation)                      │
├─────────────────────────────────────────────────────────────────┤
│                        Shared Layer                             │
│ UI Widgets │ Audio Mixer │ Scopes │ GPU/Render │ Media/Video    │
│ Codec Bindings │ Project │ AI Models │ Color Mgmt │ i18n │ ...  │
├─────────────────────────────────────────────────────────────────┤
│                      Native Codec Layer                         │
│    libtopos_codec (C11 · 稳定 C ABI) — Python 经 ctypes 接入     │
└─────────────────────────────────────────────────────────────────┘
```

**GPU 后端策略**（[ADR-0003](../adr/0003-metal-gpu-backend-dual-track-and-phase1-gates.md)）：OpenGL/ModernGL 永久保留为跨平台基线、回退与画质参考；Metal 作为 macOS 第二实现渐进接入（渲染/计算/解码/互操作四个独立维度的能力接口，`src/shared/gpu/backend_types.py` + `backend_selector.py`）。macOS 侧已有 VideoToolbox 零拷贝解码桥与 Metal 编码面直投；Windows 侧已接入 NVDEC/D3D11VA 硬解下载与 NVENC/AMF 硬件编码（D3D11 全链路 GPU 渲染仍在建设中）。

### 3. 核心模块

#### Native Codec (libtopos_codec 原生编解码器)

*   **C11 核心 + 稳定 C ABI**：CMake 构建（`native/topos_codec/`），Python 经**纯标准库 ctypes** 绑定（不编译 CPython 扩展），库加载做三态解析（ok / incompatible / not_found）+ ABI 校验
*   **bd16 整数管线**：16-bit 整数编码管线，位流规范禁浮点进入任何规范步骤；精确正交整数 8×8 DCT，AVX2/NEON SIMD 位精确
*   **rANS2 熵编码**：per-slice 自适应 rANS（产品默认），较前代码率 -7~12%、解码 -11~27%
*   **V7-A / V7-B packet**：可跳过 coefficient-band 目录 + 共享分析金字塔（支撑 8K/12K reduced 解码）
*   **`.toos` 图片格式**：TPIM 信封 + TPIC 帧内包，整数（YUV 422 / GBR 444）/ 浮点（float16）/ RAW（CFA）色彩模式 + 元数据；视频走 MOV 容器封装 TPIC
*   **TOPOS native routing**：`MediaSourceFactory.detect_media_backend()` 统一嗅探路由，TOPOS 媒体全程绕开通用解码器

#### GPU Backend (跨平台 GPU 后端)

*   **四维能力接口**：graphics / compute / decode / interop 四个独立维度（`backend_types.py`），选择器统一仲裁
*   **OpenGL 基线**：ModernGL 跨平台基线、回退与画质参考（永久保留）
*   **Metal 双轨**（macOS）：渲染 compute 路径、MSL shader 翻译批次、Metal-resident 帧、DCT/编解码 GPU kernel 一期
*   **平台互操作**：macOS VideoToolbox 零拷贝（CVPixelBuffer/IOSurface + CVMetalTextureCache，全线 ctypes）；Windows D3D11 硬解下载（NVDEC/D3D11VA，默认 NVIDIA，kill switch `TOPOS_DISABLE_WIN_HW_DECODE`）

#### Color Engine (调色引擎)

*   **ImageEngine**: 图层管理、渲染调度
*   **AsyncRenderer**: OpenGL 异步渲染、双缓冲
*   **ShaderPipeline**: 动态 GLSL 编译，EffectSpec 双发射器权威输入 + 结构化 uniform manifest（[ADR-0004](../adr/0004-dual-emitter-refactor-and-coverage-gates.md)）
*   **ColorManager**: OCIO v2 集成（内置回退配置，可指向外部 ACES Studio Config）
*   **ColorSession**: 色彩会话管理

#### Composite Node Engine (合成节点引擎)

*   **NodeEngine**: 节点图执行，**93 个注册节点**；节点契约五分类（image_filter / source / generator / ai_model / 3d_file_output）+ 六态可解释状态矩阵
*   **GraphEvaluator**: 拓扑排序、依赖解析；AI 模型节点不可用时返回 `MODEL_UNAVAILABLE` 结构化诊断
*   **节点磁盘缓存**：TOPOS-first payload（单帧落 `.toos`）+ 子树缓存 + Render-in-Place 烘焙 + 内容驱动 dirty cones
*   **统一 GPU 渲染服务**：GPU 资源池预算化、免回读 GPU 生产者、异步 PBO 回读、Metal compute bridge、逐帧 trace 归因
*   **9 个 graph ops**（duplicate / set_output / group / ungroup 等）与脚本 / AI / MCP 共用同一编辑协议

#### Video Pipeline (视频管线)

*   **FrameProviderService**: 统一帧供应（有界实时预取、平台 surface 能力门）
*   **DecodeWorker**: 多线程解码 + 有界解码会话池
*   **PlaybackClock**: 精确帧调度，J/K/L 变速感知调度前瞻、负载自适应预览质量（带迟滞）
*   **GOP 索引 + HEVC SLICE seek**: 持久索引，seek p95 约减半
*   **遮挡剔除**: 时间线保守可见性分析器（fail-closed），预览（CPU/GPU）与导出（CPU/GL/Metal）共用同一裁剪语义

#### Export Capability (导出与能力契约)

*   **EffectExecutionCapability**: 单一七轴契约（CPU/GPU 预览、CPU/Metal/OpenGL 导出、preview_only、unsupported）+ 机器可读 reason，UI 徽标 / AI manifest / Deliver preflight 共用同一真值
*   **Quality Tiers**: 效果按档位降采样渲染 + 逐素材 CPU/GPU 对拍协议
*   **导出 Manifest**（`topos.export_manifest.v1`）：源素材 SHA-256、能力快照、preflight 阻断与降级、**输出探针**（输出文件全量解码验证）、GPU trail/阶段遥测、**降级清单**（gpu_blocker / preflight_degraded / effect_export_skipped / audio_omitted）
*   **preview_only 策略**: 无导出后端的效果在交付计划验证时直接拒绝（绝不静默漏效果）

### 4. 主要功能特性

#### 4.1 调色功能 (Color)
*   **一级校色**：Lift, Gamma, Gain, Offset, Contrast, Saturation
*   **二级校色 (Qualifier)**：基于 HSL 的高效抠像
*   **曲线调整**：RGB, HSL, Saturation 曲线
*   **色彩管理**：OCIO v2 集成 + 内置回退配置（lin / sRGB / Rec.709 / BT.2020 PQ·HLG / Display P3），ACEScg (AP1) 工作空间与 IDT，可指向外部 ACES Studio Config
*   **GPU 示波器（8 模式）**：Waveform、Vectorscope（色靶/肤色线）、Split 分屏、RGB Parade、Luma Waveform、Histogram、CIE 色度图、False Color——全 GPU 分析 shader，零 CPU 回读，自适应分辨率与刷新率；Color/Composite 共用示波器 dock
*   **视频降噪**：GPU 时域 + 空域降噪核心与专用面板，随工程保存/恢复
*   **跟踪**：DaVinci 风格 Tracker 面板（点跟踪 + 置信度统计 + 四态状态机）
*   **硬件调色台**：原生 Tangent 控制台集成（轨迹球/旋钮，Hub 缺失优雅待机）
*   **LUT**：统一加载器、tetrahedral 采样、`.cube` / `.3DL`、GPU 半 texel 边缘修复

#### 4.2 视频编辑 (Edit)
*   **多轨时间线**：视频轨、音频轨、图形轨；16 种片段混合模式、插入/覆盖模式
*   **剪辑操作**：Trim, Split, Move, Ripple, Delete；多机位自动同步、冻结帧、平滑速度斜坡、帧混合变速、比例拉伸、章节标记、占位符片段、场景检测 overlay
*   **字幕与标题**：可自定义字幕特效、标题模板库 + 逐字符文字动画
*   **素材管理**：Media Pool, Bin, 项目管理
*   **转场效果**：无缝全帧转场 + 风格化运动转场 + 动画预览（GIF 导出）
*   **时间线遮挡剔除**：上层片段完全遮挡下层时自动跳过其解码/合成——实测 2K 两轨全屏遮挡 **3.26×**（11.3→36.7 fps，下轨解码请求 -100%）、四轨 **5.23×**、4K 两轨 **3.67×**；分析器单帧开销 < 11 µs（结果存档 `benchmarks/`）
*   **播放引擎**：有界实时解码预取、J/K/L 变速感知前瞻、负载自适应预览质量、codec 感知代理默认策略、预览 FPS 显示
*   **专业音频混音**：多轨混音台（Master + 推子/电平表/声像/Mute-Solo-Monitor）、效果器机架（图示 EQ / Compressor / Gate / Limiter / Reverb / Delay / Saturation / Imager）、路由矩阵、自动化车道（Read/Write/Touch/Latch）、通道条预设与 A/B 对比、波形显示、录音（VO/ADR）、片段级 Clip Gain 包络
*   **效率**：快捷键完全可自定义、工作区预设（Editing / Audio / Captions / Review）

#### 4.3 合成节点 (Composite，93 节点)
*   **输入节点**：Image（多层 EXR + AOV 透传）、Video, Constant, Ramp, Motion Graphic
*   **变换节点**：Transform（运动模糊）, Crop, CornerPin（视口手柄+对齐模式）, Lens Distortion (Brown-Conrady)
*   **跟踪/稳定**：Tracker 节点（分析/烘焙/色彩导入）、稳定器（smoothed-inverse）、人脸跟踪/平滑
*   **色彩节点**：ColorCorrect, Grade, HueCorrect
*   **抠像节点**：ChromaKeyer, LumaKeyer, DifferenceKeyer + Cryptomatte Select（ID 拾取）、Light Wrap、Matte Cleanup
*   **深度/氛围工具组**：Relight（NORMAL/POSITION AOV 重打光）、ZBlur / Fog / DepthMatte、God Rays 体积光、镜头效果包（色差/暗角/光晕）、Deflicker 去闪烁
*   **绘制/粒子/变形**：Paint 节点（克隆/擦除/纯色笔刷，逐笔撤销）+ 查看器 PAINT 工具、确定性 GPU 2D 粒子、mesh warp 网格变形、ML Roto
*   **AI 节点**：Generate, Inpaint, Expand（生成式扩展/填充）, Remove（异步 LaMa）, Denoise, RIFE 补帧
*   **3D 子系统**：Model3D 后台导入（蒙皮 / morph target / 动画采样缓存 / LOD）、场景 outliner、BVH 拾取、holdout / shadow catcher、材质槽覆盖、AOV 通道（normal / position / UV / material id / 时域运动矢量 / cryptomatte）、HDRI / IES、体积
*   **工程规模**：最高 16K 工程尺寸；93 节点 GPU 覆盖矩阵全绿

#### 4.4 导出功能 (Deliver)
*   **视频格式**：H.264, H.265, ProRes, DNxHR + **TOPOS 中间编码**（历史 YUV 域直通测量：10-bit 4:2:0 源直通 h264/DNxHR 2.6×、TOPOS 21×；HQ 档 +40%；七个帧内档 + 帧间 LP 档 + RAW 档 tier 8；当前整链速度验收见 [TOPOS 全格式 ProRes 对标与终轮优化计划](../codec/TOPOS全格式ProRes对标与终轮优化计划_2026-09-27.md)）
*   **硬件编码**：macOS VideoToolbox 原生桥（NV12/P010 Metal 编码面直投、HEVC P010 原生 mux、时间线音频合流）；Windows NVENC + 10-bit HEVC AMF
*   **图像格式**：PNG, TIFF, EXR, JPEG + **TOOS**（原生 `.toos`：整数 / 浮点 / RAW 全档位）
*   **HDR 支持**：元数据嵌入
*   **渲染队列**：批量导出 / DaVinci 式交付工作区，可分离面板；Stem 分轨导出
*   **导出 Manifest**：输出探针全量验证、GPU trail 遥测、降级清单如实上报（详见上文 Export Capability）
*   **字幕导出**：时间线字幕轨导出为 SRT（多轨合并）

#### 4.5 AI 功能
*   **AI 生成**：文生图 / 图生图 / 文生视频 / 图生视频
*   **AI 修复**：智能图像修复（Inpaint）
*   **AI 外扩**：边界扩展（Expand）
*   **AI 超分辨率**：4x/2x 图像放大
*   **统一 AI 技能运行时** `[实验性]`：`ai_harness` 提供意图路由 + Skill 体系（自动字幕、字幕翻译配音、口播精剪、产品粗剪、素材分析 ASR/OCR/镜头/节拍）+ **插件化沙箱**（manifest/生命周期/信任边界/进程隔离执行/资源 broker/rollout 质量门）+ MCP 出站客户端 + 多模态视觉输入
*   **可解释 AI**：节点契约五分类、93 节点六态可解释状态矩阵、`MODEL_UNAVAILABLE` 结构化诊断、节点级性能归因
*   **事务化 AI 编辑**：WorkflowTransaction 状态机（QUEUED→PREFLIGHT→RUNNING→VERIFYING→SUCCEEDED/DEGRADED/FAILED/CANCELLED），ApprovalGrant 由宿主签发、模型不可自签；W01–W08 全产品 AI 工作流回放矩阵 8/8 PASS
*   **可视化 AI 工作流编排** `[实验性]`：`ai_workflow` 提供类 ComfyUI 的节点画布 + 模板库，**真实 ComfyUI 客户端桥**（工作流导入导出），并可“发布为 Skill”；工作流通过 AI Assistant 入口打开，不作为默认工作区页面
*   **toposctl CLI**：AI 编辑协议命令行入口（read/validate/preview 与 UI/MCP 语义等价）

#### 4.6 教学动画 / 动态图形 (Teaching Animation)
*   **场景编辑器**：独立的 Motion Graphic 场景编辑器（图层树 / 场景时间线 / storyboard / HTML inspector）
*   **场景页 (Pages)**：一等公民多场景页 + 页级画布/页面时间线/页面转场 + 全局与页面背景层
*   **关键帧时间线**：独立数据层 + 批量轨道/关键帧命令，与编辑页标尺对齐
*   **预设库**：13 个内置代码预设（GIF 预览）+ 三级模板库（内置/用户/工程）+ 工程内 `mg_presets/` 目录
*   **画布交互**：多选/框选/批量编辑与重父级、对齐/分布
*   **运动语言**：帧精确的 Series/Sequence 时间图、缓动、spring、节拍（BPM）对齐、stagger，预览与导出逐像素一致
*   **双渲染后端**：SVG 渲染 + HTML/Chromium 渲染，支持 SVG 序列导出与 FFmpeg 转视频；空闲后台缓存渲染 + 缓存质量档（MG 片段不阻塞播放）
*   **能力桥**：声明的效果经 SVG 渲染桥接进 Composite GPU、场景音频计划桥接宿主、HDR 交付证据、组件与响应式变体/断点
*   **AI 辅助生成**：brief → 创意提案 → 场景命令编译 → 用户审批的 fail-closed 流水线 + Freeform HTML 生成回路（HTML Patch 审计 + token 净化器），AI 仅输出受限命令词表
*   **自动布局与设计令牌**：Figma 式栈/网格自动布局（改画幅自动重排）、版本化设计 token、品牌主题、内置模板（标题卡 / 章节条 / 分步 / 下三分之一）
*   **质量门禁**：首帧/中帧/末帧渲染一致性、透明度、文字溢出、安全区、对比度/字号无障碍检查、创意评分卡

#### 4.7 脚本自动化 (Scripting)
*   **Python 脚本 API**：`topos` 对象提供图像处理 / 调色自动化接口
*   **Composite 脚本 API**：与 AI / MCP 走同一套编辑协议（含 9 个图结构 graph ops）
*   **示例**：basic_grade / batch_grade / advanced_workflow

#### 4.8 原生媒体格式 (.toos / Topos RAW)
*   **`.toos` 图片格式**：Topos 图片中间格式（原 `.tpic`），TPIM 信封 + TPIC 帧内包；色彩模式四族——整数（Image Preview YUV 4:2:2 / Image HQ GBR 4:4:4 10-bit / Image XQ 12-bit，HQ/XQ 是 profile 类名）、浮点（Image Float，float16 线性域）、RAW（Image RAW，Bayer CFA）；完整 C ABI + `image_cli` + 原子写 + PNG 互转
*   **编码档位体系**（单一注册表，全部输出规格入口同名）：
    *   整数：`Topos 422 Low/Medium/High/Ultra` + `Topos 444 High/Ultra`（文件内名 `toos <Quality> <bd>bit`）
    *   浮点：`toos Low/Medium/High/Ultra 16bit float`（CLI `--tier float-low/medium/high/ultra`，qp 82/72/58/44；四档均有损，Inf/NaN 输入显式拒绝，带符号零也不保证保留；逐档误差见验收计划）
    *   RAW：`Topos RAW 12-bit 2:1 … 16:1`（Log 制作档）和 `Topos RAW 16-bit 2:1 … 16:1`（Linear 归档档）各六档，CLI `--tier raw12-*/raw16-*`；`N:1` 是档位名称，实际比率随素材变化
*   **HALF (float16) 采样域**：IEEE 754 binary16 存储模式，经冻结的 symlog 域单调映射进入同一整数编码器（spec §15）；显式 `--half --qp` 的逐位无损性取决于输入码域与 QP，须逐样本验收，不能由档名推断；浮点域由文件 IDSC sample_kind 自描述
*   **视频侧**：MOV 容器封装 TPIC 流；七个帧内档（NetProxy、422 Proxy/LT/422/HQ、4444、4444 XQ）+ 帧间 **Topos 422 LP** 微 GOP 档 + **Topos RAW** 视频 RAW 档（tier 8，ADR-C058：CFA 相位平面 12/16-bit × 名义比率 12 档，全 I 帧 + TRWM as-shot 元数据）；V7-A 可跳过频带目录（reduced reader 按需读取）、V7-B 共享分析金字塔（8K/12K reduced 解码）；MOV 中 V7/V8 流**零重编码帧提取**
*   **工作流集成**：图片序列导入（有界预取 + 取消）、ROI/目标尺寸预览解码、内嵌 OCIO 元数据 chunk；渲染缓存与素材代理默认 topos-first（`topos_proxy`），Composite 节点磁盘缓存直接落 `.toos`
*   **Topos RAW (TRAW)**：相机 RAW 编解码——12-bit Log 制作档 + 16-bit Linear 归档档（规格命名 `Topos RAW <bd>-bit <N>:1`，N ∈ {2,4,6,8,12,16}，是名义档位而非实际压缩比保证；文件内名 `toos RAW <bd>bit <N>:1`），GPU debayer 回放 + WB/EI 非破坏参数；见[图片白皮书](../../native/topos_codec/Topos_Image_白皮书.md)

#### 4.9 国际化
*   **10 种界面语言**：简体中文、英语、德语、西班牙语、法语、意大利语、日语、韩语、葡萄牙语（巴西）、俄语

### 5. 深入阅读

*   架构分册：[docs/architecture/](../architecture/)（app-products / color-rendering / composite / deliver-export / edit-timeline / shared-foundations）
*   全部文档索引：[docs/INDEX.md](../INDEX.md)

---

## English

### 1. Product Editions

Topos builds four professional product editions from one codebase:

| Product | Feature set | Intended use |
|---|---|---|
| **Topos Studio** | Color + Edit + Composite + Deliver, with Teaching Animation | Full post-production suite |
| **Topos Color** | Color + lightweight Deliver | Dedicated grading for photographers and colorists |
| **Topos Edit** | Edit + Deliver + Color viewer/transform | Video editing |
| **Topos Composite** | Composite + Deliver + Color viewer/transform + Teaching Animation | Node-based compositing and motion graphics |

Two command-line tools are also installed with `pip install -e .`:

| Command | Purpose |
|---|---|
| **`toposctl`** | AI editing protocol CLI. `read`, `validate`, and `preview` use the same `ToolFacade` path as the UI and MCP; write operations require authorization issued by the GUI/`ApprovalService`. |
| **`topos-vault`** | Media asset library CLI for AI indexing, three-stage relinking after files move, provider listing, and dependency status. |

Feature toggles and the test matrix for the four editions: [docs/product_test_matrix.md](../product_test_matrix.md).

### 2. High-Level Architecture

Topos uses a **product editions + feature modules + shared infrastructure + native codec** architecture:

```text
┌─────────────────────────────────────────────────────────────────┐
│                         Product Layer                           │
│  topos-studio | topos-color | topos-edit | topos-composite      │
├─────────────────────────────────────────────────────────────────┤
│                         Feature Layer                           │
│ Color │ Edit │ Composite │ Deliver │ Teaching Animation │ AI   │
│                    Scripting and Workflows                      │
├─────────────────────────────────────────────────────────────────┤
│                         Shared Layer                            │
│ UI │ Audio │ Scopes │ GPU/Render │ Media/Video │ Color │ i18n  │
│ Codec Bindings │ Projects │ AI Models │ Export │ and more       │
├─────────────────────────────────────────────────────────────────┤
│                      Native Codec Layer                         │
│ libtopos_codec (C11, stable C ABI; accessed from Python by ctypes)│
└─────────────────────────────────────────────────────────────────┘
```

**GPU backend strategy** ([ADR-0003](../adr/0003-metal-gpu-backend-dual-track-and-phase1-gates.md)): OpenGL/ModernGL remains the cross-platform baseline, fallback, and image-quality reference. Metal is being added as a second macOS implementation through separate graphics, compute, decode, and interop capability interfaces (`src/shared/gpu/backend_types.py` and `backend_selector.py`). macOS includes a VideoToolbox zero-copy decode bridge and direct Metal encode-surface submission. Windows includes NVDEC/D3D11VA hardware decode and NVENC/AMF hardware encode; a fully GPU-rendered D3D11 pipeline is still under development.

### 3. Core Modules

#### Native Codec (`libtopos_codec`)

- **C11 core and stable C ABI:** Built with CMake under `native/topos_codec/`. Python uses the standard-library `ctypes` module rather than a compiled CPython extension. Library loading distinguishes `ok`, `incompatible`, and `not_found` states and checks the ABI.
- **16-bit integer pipeline:** Floating-point values are excluded from normative bitstream steps. The codec uses an exact orthogonal integer 8×8 DCT and bit-exact AVX2/NEON SIMD paths.
- **rANS2 entropy coding:** Per-slice adaptive rANS is the product default. Reported results show 7–12% lower bitrate and 11–27% faster decoding than the previous code path.
- **V7-A / V7-B packets:** Skippable coefficient-band directories and a shared analysis pyramid support reduced decoding at 8K/12K.
- **`.toos` images:** TPIM envelope with TPIC frame payloads; integer YUV 4:2:2 / GBR 4:4:4, float16, and Bayer CFA RAW modes with metadata. Video stores TPIC streams in MOV containers.
- **Native media routing:** `MediaSourceFactory.detect_media_backend()` routes Topos media through the native path instead of a general-purpose decoder.

#### GPU Backend

- **Four independent capability dimensions:** graphics, compute, decode, and interop (`src/shared/gpu/backend_types.py`), resolved by a common selector.
- **OpenGL baseline:** ModernGL remains the cross-platform baseline, fallback, and image-quality reference.
- **Metal on macOS:** Metal rendering and compute paths, batches of MSL shader translations, Metal-resident frames, and first-phase DCT/codec GPU kernels.
- **Platform interop:** macOS VideoToolbox zero-copy through CVPixelBuffer/IOSurface and CVMetalTextureCache using ctypes; Windows hardware decode download through NVDEC/D3D11VA (NVIDIA enabled by default; controlled by `TOPOS_DISABLE_WIN_HW_DECODE`).

#### Color Engine

- **ImageEngine:** Layer management and render scheduling.
- **AsyncRenderer:** Asynchronous OpenGL rendering with double buffering.
- **Shader pipeline:** Dynamic GLSL compilation. EffectSpec is the authoritative input to dual emitters and a structured uniform manifest ([ADR-0004](../adr/0004-dual-emitter-refactor-and-coverage-gates.md)).
- **ColorManager:** OCIO v2 integration with a bundled fallback configuration; can use an external ACES Studio configuration.
- **ColorSession:** Color-session management.

#### Composite Node Engine

- **NodeEngine:** Executes node graphs and registers 93 nodes. Node contracts use five categories (`image_filter`, `source`, `generator`, `ai_model`, and `3d_file_output`) with a six-state explainability matrix.
- **GraphEvaluator:** Topological ordering and dependency resolution. Unavailable AI model nodes return structured `MODEL_UNAVAILABLE` diagnostics.
- **Node disk cache:** TOPOS-first `.toos` payloads, subtree caching, Render-in-Place baking, and content-driven dirty cones.
- **Unified GPU render service:** Budgeted GPU resource pool, GPU-resident producers, asynchronous PBO readback, Metal compute bridge, and per-frame trace attribution.
- **Graph operations:** Nine graph operations (including duplicate, set output, group, and ungroup) share the same edit protocol with scripting, AI, and MCP.

#### Video Pipeline

- **FrameProviderService:** Bounded real-time prefetch and platform-surface capability checks.
- **DecodeWorker:** Multithreaded decoding with a bounded decoder-session pool.
- **PlaybackClock:** Frame-accurate scheduling, J/K/L-aware lookahead, and load-adaptive preview quality with hysteresis.
- **GOP index and HEVC slice seeking:** Persistent indexes; seek p95 is reported to be roughly halved.
- **Timeline occlusion culling:** Conservative, fail-closed visibility analysis. Preview (CPU/GPU) and export (CPU/GL/Metal) use the same culling semantics.

#### Export Capability and Effect Contracts

- **EffectExecutionCapability:** A single seven-axis contract covering CPU/GPU preview, CPU/Metal/OpenGL export, `preview_only`, and `unsupported`, with machine-readable reasons. UI badges, AI manifests, and Deliver preflight use the same source of truth.
- **Quality tiers:** Effects can render at downsampled quality tiers and use a per-source CPU/GPU comparison protocol.
- **Export manifest (`topos.export_manifest.v1`):** Source SHA-256 hashes, capability snapshots, preflight blocks and degradations, full output-file decode probes, GPU-trail and stage telemetry, and explicit degradation entries (`gpu_blocker`, `preflight_degraded`, `effect_export_skipped`, `audio_omitted`).
- **`preview_only` policy:** Effects without an export backend are rejected during delivery-plan validation; they are never silently omitted.

### 4. Features

#### 4.1 Color Grading

- Primary controls: Lift, Gamma, Gain, Offset, Contrast, and Saturation.
- HSL-based secondary qualification.
- RGB, HSL, and Saturation curves.
- OCIO v2 color management with a bundled fallback configuration (linear, sRGB, Rec.709, BT.2020 PQ/HLG, and Display P3), ACEScg (AP1) working space and IDTs, and optional external ACES Studio configuration.
- **Eight GPU scopes:** Waveform, Vectorscope (color targets/skin-tone line), Split, RGB Parade, Luma Waveform, Histogram, CIE chromaticity, and False Color. Analysis runs in GPU shaders without CPU readback; resolution and refresh rate adapt to load. Color and Composite share the scopes dock.
- GPU temporal and spatial video denoising with a dedicated panel; settings persist with the project.
- DaVinci-style tracker panel with point tracking, confidence statistics, and a four-state state machine.
- Native Tangent hardware-panel integration for trackballs and knobs; the app waits gracefully if the Tangent Hub is unavailable.
- Unified LUT loader with tetrahedral sampling, `.cube`/`.3DL` support, and a GPU half-texel edge fix.

#### 4.2 Video Editing

- Multitrack video, audio, and graphics timelines; 16 clip blend modes; insert and overwrite editing.
- Trim, split, move, ripple, and delete; multicamera auto-sync; freeze frames; smooth speed ramps; frame-blended retiming; aspect-ratio stretch; chapter markers; placeholder clips; and scene-detection overlays.
- Customizable subtitle effects, title-template library, and character-by-character text animation.
- Media Pool, bins, and project management.
- Seamless full-frame and stylized motion transitions with animated previews and GIF export.
- **Timeline occlusion culling:** Fully covered lower clips skip decode/compositing. Reported benchmarks: 2K, two-track full-screen occlusion **3.26×** (11.3→36.7 fps; lower-track decode requests −100%); four tracks **5.23×**; 4K, two tracks **3.67×**. Analyzer overhead is under 11 µs per frame. Results are archived in `benchmarks/`.
- Playback engine with bounded decode prefetch, J/K/L-aware lookahead, load-adaptive preview quality, codec-aware proxy defaults, and preview-FPS display.
- **Professional audio mixer:** Multitrack mixer (Master, faders, meters, pan, Mute/Solo/Monitor); graphic EQ, Compressor, Gate, Limiter, Reverb, Delay, Saturation, and Imager effects; routing matrix; Read/Write/Touch/Latch automation lanes; channel-strip presets and A/B comparison; waveforms; VO/ADR recording; and clip-level Clip Gain envelopes.
- Fully customizable keyboard shortcuts and Editing/Audio/Captions/Review workspace presets.

#### 4.3 Node-Based Compositing (93 Nodes)

- **Inputs:** Image (multi-layer EXR and AOV passthrough), Video, Constant, Ramp, and Motion Graphic.
- **Transforms:** Transform with motion blur, Crop, CornerPin with viewport handles and alignment modes, and Brown–Conrady lens distortion.
- **Tracking and stabilization:** Tracker analysis/baking/color import, smoothed-inverse stabilizer, face tracking, and smoothing.
- **Color:** ColorCorrect, Grade, and HueCorrect.
- **Keying:** ChromaKeyer, LumaKeyer, DifferenceKeyer, Cryptomatte Select with ID picking, Light Wrap, and Matte Cleanup.
- **Depth and atmosphere:** Relight using NORMAL/POSITION AOVs, ZBlur, Fog, DepthMatte, volumetric God Rays, lens effects (chromatic aberration/vignette/halation), and Deflicker.
- **Paint, particles, and deformation:** Paint node (clone/erase/solid brush with per-stroke undo) and viewer PAINT tool; deterministic GPU 2D particles; mesh warp; ML Roto.
- **AI nodes:** Generate, Inpaint, Expand, asynchronous LaMa Remove, Denoise, and RIFE frame interpolation.
- **3D subsystem:** Background Model3D import (skinning, morph targets, animation-sampling cache, and LOD), scene outliner, BVH picking, holdout/shadow catcher, material-slot overrides, AOVs (normal, position, UV, material ID, temporal motion vectors, Cryptomatte), HDRI/IES lighting, and volumes.
- Projects up to 16K; the 93-node GPU coverage matrix is reported all green.

#### 4.4 Delivery and Export

- **Video:** H.264, H.265, ProRes, DNxHR, and **TOPOS intermediate encoding**. Historical YUV-domain passthrough measurements: 2.6× for H.264/DNxHR and 21× for TOPOS from 10-bit 4:2:0 sources; HQ mode was 40% faster in that setup. Supports seven intra-frame tiers, inter-frame LP, and RAW tier 8. Current end-to-end speed qualification is tracked in [the TOPOS ProRes benchmark and final optimization plan](../codec/TOPOS全格式ProRes对标与终轮优化计划_2026-09-27.md).
- **Hardware encoding:** Native macOS VideoToolbox bridge (NV12/P010 direct Metal encode surfaces, native HEVC P010 muxing, timeline audio muxing); Windows NVENC and 10-bit HEVC AMF.
- **Images:** PNG, TIFF, EXR, JPEG, and native **TOOS** (`.toos`) integer, float, and RAW tiers.
- HDR metadata embedding.
- Render queue, batch export, detachable DaVinci-style delivery workspace, and stem export.
- Export manifest with full output probing, GPU-trail telemetry, and explicit degradation reporting (see Export Capability above).
- SRT subtitle export with multiple timeline tracks merged.

#### 4.5 AI Features

- Text-to-image, image-to-image, text-to-video, and image-to-video generation.
- AI inpainting, outpainting/Expand, and 2×/4× super-resolution.
- **Unified AI skill runtime** `[Experimental]`: `ai_harness` provides intent routing and skills for subtitles, subtitle translation/dubbing, talking-head edits, product rough cuts, and media analysis (ASR/OCR/shots/rhythm). Includes a plugin sandbox (manifest, lifecycle, trust boundaries, process isolation, resource broker, rollout quality gates), outbound MCP client, and multimodal visual input.
- **Explainable AI:** Five node-contract categories; six-state status matrix for 93 nodes; structured `MODEL_UNAVAILABLE` diagnostics; and node-level performance attribution.
- **Transactional AI editing:** `WorkflowTransaction` state machine (`QUEUED` → `PREFLIGHT` → `RUNNING` → `VERIFYING` → `SUCCEEDED`/`DEGRADED`/`FAILED`/`CANCELLED`). The host issues `ApprovalGrant`; models cannot self-authorize. The W01–W08 cross-product AI workflow replay matrix reports 8/8 passing.
- **Visual AI workflow orchestration** `[Experimental]`: `ai_workflow` provides a ComfyUI-style node canvas and templates, a real ComfyUI client bridge for workflow import/export, and a “publish as Skill” flow. The workflow is opened through the AI Assistant rather than being a default workspace page.
- **`toposctl`:** AI editing protocol CLI; `read`, `validate`, and `preview` follow the same semantics as the UI/MCP path.

#### 4.6 Teaching Animation and Motion Graphics

- Standalone Motion Graphic scene editor with layer tree, scene timeline, storyboard, and HTML inspector.
- First-class multi-scene pages with page canvas, page timelines and transitions, and global/page background layers.
- Separate keyframe data layer and batch track/keyframe commands aligned with the Edit-page ruler.
- Preset library with 13 built-in code presets and GIF previews; built-in/user/project template levels; project-local `mg_presets/` directory.
- Multi-select, marquee selection, batch editing, reparenting, alignment, and distribution.
- Frame-accurate Series/Sequence timing; easing, spring motion, BPM synchronization, and stagger. Preview and export are pixel-consistent.
- SVG and HTML/Chromium renderers; SVG sequence export and FFmpeg video export. Background idle rendering with quality tiers keeps Motion Graphic clips from blocking playback.
- Effects can bridge through SVG into Composite GPU; scene audio plans bridge to the host; HDR delivery evidence; reusable components and responsive variants/breakpoints.
- AI-assisted generation follows brief → proposal → scene-command compilation → user approval, with a fail-closed pipeline. Freeform HTML generation includes HTML patch auditing and token sanitization; AI output is limited to an approved command vocabulary.
- Figma-style stack/grid auto-layout with responsive canvas resizing, versioned design tokens, brand themes, and built-in title card/chapter bar/steps/lower-third templates.
- Quality gates for first/middle/last-frame consistency, transparency, text overflow, safe areas, contrast, accessible font sizes, and creative scorecards.

#### 4.7 Scripting

- Python scripting API: the `topos` object exposes image-processing and grading automation.
- Composite scripting API shares the editing protocol with AI/MCP, including nine graph operations.
- Examples: `basic_grade`, `batch_grade`, and `advanced_workflow`.

#### 4.8 Native Media Formats (`.toos` / Topos RAW)

- **`.toos` image format:** Topos image intermediate format (formerly `.tpic`) with a TPIM envelope and TPIC frame payload. Integer modes include Image Preview YUV 4:2:2, Image HQ GBR 4:4:4 10-bit, and Image XQ 12-bit (HQ/XQ are profile names); float mode is Image Float (linear float16); RAW mode is Image RAW (Bayer CFA). Includes a C ABI, `image_cli`, atomic writes, and PNG conversion.
- **Encoding tiers** use a single registry and consistent names across output surfaces:
  - Integer: `Topos 422 Low/Medium/High/Ultra` and `Topos 444 High/Ultra` (in-file name: `toos <Quality> <bd>bit`).
  - Float: `toos Low/Medium/High/Ultra 16bit float`; CLI tiers `float-low/medium/high/ultra`, QP 82/72/58/44. All four tiers are lossy, reject Inf/NaN, and do not guarantee preservation of signed zero; see the qualification plan for per-tier error measurements.
  - RAW: six `Topos RAW 12-bit 2:1 … 16:1` Log production tiers and six `Topos RAW 16-bit 2:1 … 16:1` Linear archive tiers; CLI tiers `raw12-*`/`raw16-*`. The `N:1` suffix names a tier; actual compression depends on the source.
- **HALF (float16) sample domain:** IEEE 754 binary16 storage is mapped monotonically through the frozen symlog domain into the integer codec (spec §15). Bit-exactness with explicit `--half --qp` depends on the source token domain and QP and must be checked on the actual input. The file's IDSC sample kind describes the float domain.
- **Video:** MOV containers carry TPIC streams. Seven intra-frame tiers (NetProxy, 422 Proxy/LT/422/HQ, 4444, 4444 XQ), inter-frame **Topos 422 LP** micro-GOP tier, and **Topos RAW** video RAW tiers (tier 8: CFA phase planes, 12/16-bit × 12 nominal ratios, all-I frames, and TRWM as-shot metadata; ADR-C058). V7-A skippable band directories support reduced reads; V7-B shared analysis pyramids support reduced 8K/12K decoding. V7/V8 streams can be extracted from MOV without re-encoding.
- **Workflow integration:** Image-sequence import with bounded prefetch and cancellation, ROI/target-size preview decode, embedded OCIO metadata chunks, TOPOS-first render cache and media proxy (`topos_proxy`), and `.toos` Composite node disk caches.
- **Topos RAW (TRAW):** Camera RAW codec with 12-bit Log production and 16-bit Linear archive tiers. Ratio suffixes are nominal tier names, not guaranteed compression ratios. Includes GPU debayer playback and non-destructive white-balance/exposure-index parameters. See the [Topos Image whitepaper](../../native/topos_codec/Topos_Image_Whitepaper.md).

#### 4.9 Localization

The interface includes ten locales: Simplified Chinese, English, German, Spanish, French, Italian, Japanese, Korean, Brazilian Portuguese, and Russian.

### 5. Further Reading

- Architecture volumes: [docs/architecture/](../architecture/) (app-products / color-rendering / composite / deliver-export / edit-timeline / shared-foundations)
- Full documentation index: [docs/INDEX.md](../INDEX.md)
