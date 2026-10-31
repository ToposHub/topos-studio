# 开发指南 / Development Guide

> getstarted · 计划 04 P3（2026-09-28）。本文内容整理自 `README.md` / `README_EN.md`（迁移瘦身），未新增事实。
> 构建与打包见 [building.md](building.md)；模块分层硬约束见 [docs/module_boundary_rules.md](../module_boundary_rules.md)。
> 本文为双语文档：简体中文在前，English follows below。

## 中文

### 1. 目录结构说明

```text
├── apps/                          # 产品入口层
│   ├── topos_color/               # Topos Color 入口
│   │   └── main.py                # 调色产品启动器
│   ├── topos_edit/                # Topos Edit 入口
│   │   └── main.py                # 剪辑产品启动器
│   ├── topos_studio/              # Topos Studio 入口
│   │   └── main.py                # 全功能产品启动器
│   ├── topos_composite/           # Topos Composite 入口
│   │   └── main.py                # 合成产品启动器
│   └── topos_vault/               # 素材资产库 CLI（AI 打标 / 重连 / provider）
├── src/                           # 核心代码库
│   ├── main.py                    # 统一入口（平台加固 + --product 产品选择）
│   ├── products/                  # 产品预设定义
│   │   ├── __init__.py            # ProductPreset, FEATURE_* 常量
│   │   ├── topos_color.py         # Color 产品注册逻辑
│   │   ├── topos_edit.py          # Edit 产品注册逻辑
│   │   ├── topos_studio.py        # Studio 产品注册逻辑
│   │   ├── topos_composite.py     # Composite 产品注册逻辑
│   │   └── packaging.py           # 打包配置
│   ├── features/                  # 特性模块层 (Feature Modules)
│   │   ├── color/                 # Color 特性模块（调色）
│   │   │   ├── module.py          # ColorModule 注册
│   │   │   ├── facade.py          # ColorFacade 外观接口
│   │   │   ├── core/              # 调色引擎核心
│   │   │   │   ├── image_engine.py    # 图像引擎
│   │   │   │   ├── async_renderer.py  # 异步渲染器
│   │   │   │   ├── shader_compiler.py # 着色器编译器（双发射器 + EffectSpec 权威输入）
│   │   │   │   ├── color_manager.py   # OCIO v2 / 色彩空间管理
│   │   │   │   ├── color_session.py   # 色彩会话管理
│   │   │   │   ├── color_output.py    # ColorOutput 渲染输出
│   │   │   │   ├── scope_types.py     # GPU 示波器类型定义（8 模式）
│   │   │   │   ├── video_temporal_denoise.py # GPU 时域+空域视频降噪
│   │   │   │   └── layer.py           # 图层数据模型
│   │   │   ├── integrations/      # Tangent 控制台原生桥
│   │   │   ├── shaders/           # 调色 GLSL（含 gpu_scope 全 GPU 示波器）
│   │   │   └── ui/                # Color UI 面板（调色/限定器/示波器/图层/跟踪…）
│   │   ├── edit/                  # Edit 特性模块（剪辑）
│   │   │   ├── module.py          # EditModule 注册
│   │   │   ├── core/              # Edit 核心逻辑
│   │   │   │   ├── project.py             # EditProject, MediaAsset
│   │   │   │   ├── timeline_v2.py         # TimelineSequence
│   │   │   │   ├── commands.py            # 命令模式（undo/redo）
│   │   │   │   ├── timeline_occlusion.py  # 时间线遮挡剔除分析器（fail-closed）
│   │   │   │   ├── capability_registry.py # 运行时能力快照
│   │   │   │   ├── export_manifest.py     # 导出清单（探针/遥测/降级）
│   │   │   │   ├── audio_playback.py      # 音频引擎（主时钟/混音）
│   │   │   │   ├── audio_waveform.py      # 波形提取与缓存
│   │   │   │   ├── scene_cut/             # 场景检测（分析/缩略图缓存）
│   │   │   │   ├── captions/              # 字幕特效
│   │   │   │   └── timeline_preview_renderer.py
│   │   │   ├── ai_editing/        # AI 剪辑（WorkflowTransaction / toposctl CLI）
│   │   │   └── ui/                # Edit UI 面板（时间线/素材池/多机位/调音台…）
│   │   ├── composite/             # Composite 特性模块（合成节点，93 节点）
│   │   │   ├── module.py          # CompositeModule 注册
│   │   │   ├── core/              # 节点引擎（注册表/契约分类/graph ops）
│   │   │   ├── nodes/             # 节点定义（含 tracker/depth/paint/ai/3d 组）
│   │   │   ├── evaluators/        # 节点评估器（MODEL_UNAVAILABLE 结构化诊断）
│   │   │   ├── render/            # 统一 GPU 渲染服务 / 节点磁盘缓存（.toos payload）
│   │   │   ├── render3d/          # 3D 子系统（导入/拾取/AOV/HDRI/体积）
│   │   │   └── ui/                # Composite UI（画布/查看器 PAINT 工具/outliner）
│   │   ├── teaching_animation/    # 教学动画 / 动态图形 (Motion Graphic)
│   │   │   ├── module.py          # TeachingAnimationModule 注册
│   │   │   ├── facade.py          # 对外接口
│   │   │   ├── composite_node.py  # 合成图插件节点（注册进 Composite）
│   │   │   ├── core/              # 场景模型 / 命令栈 / 布局 / 设计令牌 / 预设库
│   │   │   ├── motion/            # 运动原语（缓动/spring/节拍/stagger）
│   │   │   ├── render/            # SVG + HTML 双后端渲染 / 导出 / 缓存质量档
│   │   │   ├── ai/                # AI 场景生成（brief→proposal→command→approve）
│   │   │   └── ui/                # 场景编辑器 / 关键帧时间线 / 场景页 / storyboard
│   │   ├── ai_harness/            # AI 统一运行时 (Skill 执行引擎) [实验性]
│   │   │   ├── invocation_router.py   # 意图路由 + Skill 分发
│   │   │   ├── generation_adapter.py  # 生成能力接入
│   │   │   ├── editorial_skills.py    # 官方技能（字幕/配音/口播精剪）
│   │   │   ├── analysis_skills.py     # 素材分析（ASR/OCR/镜头/节拍）
│   │   │   ├── plugin_sandbox_worker.py # 插件进程隔离沙箱
│   │   │   └── mcp_outbound.py        # MCP 出站客户端
│   │   ├── ai_workflow/           # 可视化 AI 工作流编排 (类 ComfyUI) [实验性]
│   │   │   ├── module.py          # AiWorkflowModule（实验性工作流界面）
│   │   │   ├── core/              # 节点图模型 / 编译器 / 模板库
│   │   │   ├── adapters/          # ComfyUI 客户端桥 / 生成节点 / Composite
│   │   │   ├── runtime/           # 运行控制器
│   │   │   └── ui/                # 画布 / inspector / 节点搜索
│   │   ├── deliver/               # Deliver 特性模块（导出交付）
│   │   │   ├── module.py          # DeliverModule 注册
│   │   │   ├── core/              # 导出引擎 / 交付能力 / 批量导出（TOPOS 直通 / 硬件编码 / Stem 分轨）
│   │   │   ├── adapters/          # 交付适配器
│   │   │   ├── srt_exporter.py    # SRT 字幕导出
│   │   │   └── ui/                # 渲染队列 / 批量导出 UI
│   │   ├── scripting/             # 脚本自动化模块
│   │   │   ├── engine.py          # 脚本引擎
│   │   │   └── api.py             # 脚本 API (topos 对象)
│   │   ├── shared/                # 跨特性共享
│   │   │   └── effects/           # 效果执行能力契约与质量档
│   │   │       ├── effect_capability.py # EffectExecutionCapability 七轴契约
│   │   │       └── effect_tiers.py      # 下采样质量档 + CPU/GPU 对拍
│   │   └── models/                # 共享数据模型
│   ├── shared/                    # 共享基础设施层
│   │   ├── codec/                 # libtopos_codec ctypes 绑定
│   │   │   ├── topos_binding.py       # 视频绑定（三态解析 + ABI 校验）
│   │   │   └── topos_image_binding.py # .toos 图片绑定（解码零拷贝入 numpy）
│   │   ├── gpu/                   # GPU 后端抽象
│   │   │   ├── backend_types.py   # graphics/compute/decode/interop 四维能力接口
│   │   │   ├── backend_selector.py# 后端选择器
│   │   │   ├── gl_backend.py      # OpenGL 上下文
│   │   │   ├── gl_context_hub.py  # GL 上下文库
│   │   │   ├── gpu_scope_engine.py# 全 GPU 示波器引擎
│   │   │   └── metal/             # Metal 子包（context/kernels_*/submission 调度）
│   │   ├── media/                 # 媒体处理与平台桥
│   │   │   ├── image_io.py        # 图像 IO
│   │   │   ├── metadata.py        # 元数据提取
│   │   │   ├── macos_vt/          # VideoToolbox 零拷贝桥（IOSurface/CVMetal/GL 桥/硬编会话）
│   │   │   ├── windows_hw/        # D3D11 硬解下载 + NVENC/AMF 硬编
│   │   │   └── disk_proxy_service.py # topos-first 素材代理服务
│   │   ├── render/                # 渲染基础设施
│   │   │   └── source.py          # 渲染数据 source 契约
│   │   ├── video/                 # 视频基础设施（异步渲染/解码/帧供应/GPU 上传）
│   │   │   ├── video_async_renderer.py # 异步视频渲染
│   │   │   ├── decode_worker.py   # 多线程解码
│   │   │   └── frame_provider_service.py # 统一帧供应（有界预取/Metal-resident）
│   │   ├── ai_models/             # AI 模型封装（超分/降噪/修复）
│   │   ├── project/               # 项目管理
│   │   ├── export/                # 导出基础设施
│   │   ├── color/                 # 色彩描述符 / 变换 provider（ACEScg AP1）
│   │   ├── effects/               # 通用效果
│   │   ├── vault/                 # 素材资产库（指纹/打标/重连）
│   │   ├── control_surfaces/      # 外部控制面（Tangent 等）
│   │   ├── tracking/              # 跟踪基础设施
│   │   ├── ui/                    # 共享 UI 控件
│   │   │   ├── widgets/           # 自定义控件（GPU 示波器 / 矢量图控件）
│   │   │   ├── audio_mixer/       # 音频混音台 / 效果器 / 自动化 / 路由
│   │   │   └── style_sheet.py     # 样式表
│   │   ├── i18n/                  # 国际化（10 种界面语言）
│   │   ├── config/                # 配置管理
│   │   ├── db/                    # 数据库
│   │   ├── io/                    # 文件 IO
│   │   ├── logging/               # 日志系统
│   │   ├── paths/                 # 路径管理
│   │   ├── preferences/           # 偏好设置
│   │   ├── assets/                # 共享资源（字体等）
│   │   ├── actions/               # 动作系统
│   │   ├── layout/                # 布局管理
│   │   ├── panels/                # 共享面板
│   │   └── utils/                 # 工具函数
│   ├── native/                    # 原生扩展
│   │   └── decklink_bridge/       # DeckLink 输出 C 扩展（可选构建）
│   ├── luts/                      # LUT 文件
│   └── app/                       # 应用层
│       ├── main_window.py         # 主窗口
│       ├── app_context.py         # 应用上下文
│       ├── feature_registry.py    # 特性注册中心
│       ├── feature_flags.py       # 特性开关
│       ├── page_contract.py       # 页面契约
│       ├── module_loader.py       # 模块加载器
│       ├── controllers/           # 控制器
│       │   ├── color_controller.py
│       │   ├── edit_controller.py
│       │   ├── deliver_controller.py
│       │   └── layer_controller.py
│       ├── menu/                  # 菜单系统
│       ├── integrations/          # 集成模块
│       │   └── file_explorer/     # 文件浏览器集成
│       └── ui/                    # 应用级 UI（素材库/偏好设置/工作区预设…）
├── native/                        # 原生编解码核心
│   └── topos_codec/               # libtopos_codec（C11 · CMake · 稳定 C ABI）
│       ├── include/               # 公共头文件（topos_codec.h / topos_image.h）
│       ├── src/                   # transform / bitstream / entropy / codec / mov / simd / image
│       ├── tests/                 # unit / conformance / interop / abi / fuzz
│       ├── docs/                  # 编解码 SDK、位流/容器规范、ADR-C 系列
│       ├── Topos_Codec_白皮书.md  # 编码引擎白皮书（英文版 Whitepaper 同目录）
│       └── run_tests.sh           # 原生测试门禁
├── plugins/                       # 插件目录
│   ├── *.tglsl                    # GLSL 着色器插件
│   ├── *.tai                      # AI 智能插件
│   └── node_plugins/              # 节点插件
├── models/                        # AI 模型权重
├── Presets/                       # 预设文件
├── luts/                          # 预设 3D LUT
├── hdri/                          # HDRI 素材
├── tools/                         # 工程化脚本库（基准/探针/验证，不随应用分发）
│   ├── bench_*                    # 编解码/播放/导出/合成性能基准
│   ├── probe_* / dbg_*            # Metal/VT/D3D11 能力探针与调试
│   ├── verify_*                   # i18n / 依赖层级 / 矩阵验证
│   ├── e2e_*                      # 播放/scrub/seek 端到端基准
│   ├── perf_regression_gate.py    # 性能回归门禁
│   └── convert_*                  # ONNX 模型转换等
├── ocio/                          # 内置回退 OCIO 配置（minimal.ocio）
├── benchmarks/                    # 基准结果存档（遮挡剔除等）
├── scripts/                       # 脚本工具
├── tests/                         # 测试文件（1400+ 文件 / 2.2 万+ 用例）
├── docs/                          # 文档（codec/ 与 image/ 为编解码与图片格式专项）
├── packaging/                     # 打包配置
│   └── pyinstaller/               # PyInstaller 配置（四个产品 spec）
├── external/                      # 外部模块
│   ├── file_explorer/             # 文件浏览器
│   └── blackmagic/                # DeckLink SDK
├── config/                        # 运行配置
│   └── performance_budgets.json   # 性能预算
└── pyproject.toml                 # 项目配置（v3.0）
```

分层导入硬约束见 [docs/module_boundary_rules.md](../module_boundary_rules.md)（products → features → shared → app）。

### 2. Feature Module System (特性模块系统)

每个特性模块遵循统一接口：

```python
class FeatureModule:
    def is_available(self) -> bool: ...
    def create_pages(self, context: AppContext) -> List[PageDescriptor]: ...
    def create_facade(self, context: AppContext) -> Optional[Any]: ...
    def on_register(self, context: AppContext) -> None: ...
    def on_unregister(self) -> None: ...
```

### 3. ProductPreset (产品预设)

```python
@dataclass(frozen=True)
class ProductPreset:
    name: str                      # 产品标识
    display_name: str              # 显示名称
    enabled_features: frozenset    # 启用特性
    disabled_features: frozenset   # 禁用特性
    description: str               # 描述
```

### 4. 插件系统

#### DCTL (.tglsl) 着色器插件

```glsl
// --- Topos Plugin Metadata ---
// @name 电影感暗角
// @param slider amount "暗角强度" 0.5 0.0 1.0
// @param slider radius "暗角范围" 0.7 0.1 2.0
// @export_support all              # 声明支持所有导出类型
// @graphics_backends opengl,metal  # 声明可用的图形后端
// @occlusion background_independent # 声明遮挡剔除安全（不依赖下层画面）
// -----------------------------

vec3 process(vec3 color, vec2 uv) {
    // 实现代码...
}
```

*   **能力自声明**：`@export_support` 声明支持的导出类型（`all` / `video` / `lut` / `none`），`@graphics_backends` 声明图形后端；未声明导出支持的效果按 `preview_only` 策略在交付前被阻断。`@occlusion background_independent` 声明该效果不依赖下层画面，可参与时间线遮挡剔除；未声明一律按不安全保守处理。

#### AI (.tai) 智能插件

```
// --- Topos AI Plugin Metadata ---
// @name AI Upscale 4X
// @ai_model models/upscale/4x_ultrasharp.onnx
// @ai_tile_size 512
// @ai_overlap 32
// @ai_scale 4
// -----------------------------
```

完整插件开发规范（含 file_explorer 双插件体系辨析）见 [docs/PLUGIN_DEV_GUIDE.md](../PLUGIN_DEV_GUIDE.md)。

### 5. 开发流程

#### 添加新特性模块

1. **创建模块目录**：`src/features/my_feature/`
2. **实现模块接口**：创建 `module.py` 实现 `FeatureModule`
3. **定义页面**：实现 `create_pages()` 返回 `PageDescriptor`
4. **注册模块**：在产品文件中调用 `registry.register_module()`

#### 添加新产品变体

1. **创建产品入口**：`apps/my_product/main.py`
2. **定义产品预设**：在 `src/products/__init__.py` 添加 `ProductPreset`
3. **创建产品配置**：`src/products/my_product.py`
4. **添加打包配置**：`packaging/pyinstaller/my_product_spec.py`

#### 添加新合成节点

1. **定义节点类**：`src/features/composite/nodes/my_node.py`
2. **实现评估器**：`src/features/composite/evaluators/my_evaluator.py`
3. **声明节点契约**：在 `core/node_capabilities.py` 登记契约分类（六态状态矩阵据此解释）
4. **注册节点**：在 `nodes/__init__.py` 中注册

#### 原生编解码器开发

构建与测试门禁见 [building.md](building.md)（`bash native/topos_codec/run_tests.sh` / `TOPOS_CODEC_LIB`）。

### 6. 关键开发注意事项

*   **线程安全**：严禁在 UI 线程中直接调用 OpenGL API
*   **资源释放**：ModernGL 对象必须显式调用 `.release()`
*   **坐标转换**：UI(像素) -> 遮罩(UV 0-1) -> OpenGL(NDC -1~1)
*   **GLSL 类型**：所有数字必须使用浮点数 (`1.0` 而非 `1`)
*   **特性开关**：使用 `src/shared/feature_flags.py` 检查特性可用性
*   **效果能力契约**：新增效果须声明 `EffectExecutionCapability`；未声明导出后端的效果按 `preview_only` 处理，交付时被阻断而非静默跳过
*   **遮挡剔除协作**：效果不依赖下层画面时声明 `@occlusion background_independent`，否则剔除按不安全保守处理（fail-closed）
*   **虚拟环境架构**：`.venv` 与 CPU 架构绑定，换机器/换芯片后需重建

### 7. 开发规范文档

*   [插件开发指南](../PLUGIN_DEV_GUIDE.md) — `.tglsl` / `.tai` 插件规范（含两套插件体系辨析）
*   [模块边界规则](../module_boundary_rules.md) — 特性模块分层与依赖约束
*   [TAEP 工程规范](../taep_engineering_conventions.md) — 编码与架构约定
*   [HDR 数学契约](../hdr_math_contract.md) — HDR 管线数学定义
*   [Prompt 库](../PROMPTS.md)

---

## English

### 1. Repository Layout

```text
├── apps/                         # Product launchers and the Topos Vault CLI
│   ├── topos_color/main.py
│   ├── topos_edit/main.py
│   ├── topos_studio/main.py
│   ├── topos_composite/main.py
│   └── topos_vault/              # AI indexing, relinking, and query-service CLI
├── src/
│   ├── main.py                   # Unified app entry point and product selector
│   ├── products/                 # Product presets and packaging definitions
│   ├── features/
│   │   ├── color/                # Color grading, shaders, UI, Tangent integration
│   │   ├── edit/                 # Timeline editing, audio, AI editing, UI
│   │   ├── composite/            # Node graph, evaluators, GPU renderer, 3D, UI (93 nodes)
│   │   ├── deliver/              # Export engine, delivery adapters, render queue
│   │   ├── teaching_animation/   # Motion graphics scenes, animation, and rendering
│   │   ├── ai_harness/           # Experimental unified AI skill runtime
│   │   ├── ai_workflow/          # Experimental visual AI workflow tools
│   │   ├── scripting/            # Python automation API
│   │   ├── shared/effects/       # Shared effect capability and quality-tier contracts
│   │   └── models/               # Shared feature data models
│   ├── shared/                   # Cross-feature application infrastructure
│   │   ├── codec/                # ctypes bindings for libtopos_codec
│   │   ├── gpu/                  # OpenGL/Metal backend abstractions and GPU scopes
│   │   ├── media/                # Image I/O and macOS/Windows media bridges
│   │   ├── video/                # Decode workers, frame providers, async rendering
│   │   ├── ai_models/            # AI model adapters
│   │   ├── vault/                # Asset fingerprints, indexing, and relinking
│   │   ├── ui/                   # Shared widgets and audio mixer
│   │   ├── i18n/                 # Ten interface locales
│   │   └── project/, export/, color/, tracking/, and other services
│   ├── app/                      # Main window, feature registry, controllers, UI
│   ├── native/decklink_bridge/   # Optional DeckLink output extension
│   └── luts/                     # Application LUT resources
├── native/topos_codec/          # C11 codec library, stable C ABI, tests, and docs
├── plugins/                     # .tglsl shaders, .tai AI plugins, node plugins
├── models/                       # AI model weights and model packs
├── Presets/                      # System and user presets
├── luts/                          # 3D LUT presets
├── hdri/                          # HDRI assets
├── ocio/                          # Bundled fallback OCIO configuration
├── config/performance_budgets.json # Runtime performance budgets
├── tools/                         # Benchmarks, probes, verification, conversion tools
├── scripts/                       # Development and maintenance scripts
├── benchmarks/                    # Archived benchmark results
├── tests/                         # 1,465+ test files and 22,000+ test functions/cases
├── docs/                          # Architecture, codec, image, video, and feature docs
├── packaging/pyinstaller/         # PyInstaller specs for the four products
├── external/
│   ├── file_explorer/             # Embedded media browser
│   └── blackmagic/                # DeckLink SDK
├── requirements.txt
└── pyproject.toml                 # Package metadata and console entry points (v3.0)
```

Layering and import constraints: [docs/module_boundary_rules.md](../module_boundary_rules.md) (products → features → shared → app).

### 2. Feature Module System

Feature modules follow a common interface:

```python
class FeatureModule:
    def is_available(self) -> bool: ...
    def create_pages(self, context: AppContext) -> List[PageDescriptor]: ...
    def create_facade(self, context: AppContext) -> Optional[Any]: ...
    def on_register(self, context: AppContext) -> None: ...
    def on_unregister(self) -> None: ...
```

### 3. ProductPreset

```python
@dataclass(frozen=True)
class ProductPreset:
    name: str
    display_name: str
    enabled_features: frozenset
    disabled_features: frozenset
    description: str
```

### 4. Plugin System

The repository has two plugin systems: the main Topos application plugins described here, and a separate Python plugin system for `external/file_explorer` (see `docs/PLUGIN_DEV_GUIDE.md`).

#### GLSL Shader Plugins (`.tglsl`)

```glsl
// --- Topos Plugin Metadata ---
// @name Cinematic Vignette
// @param slider amount "Vignette strength" 0.5 0.0 1.0
// @param slider radius "Vignette radius" 0.7 0.1 2.0
// @export_support all              # Supports all export types
// @graphics_backends opengl,metal  # Supported graphics backends
// @occlusion background_independent # Safe for timeline occlusion culling
// -----------------------------

vec3 process(vec3 color, vec2 uv) {
    // Plugin implementation...
}
```

- **Capability declarations:** `@export_support` declares supported output types (`all`, `video`, `lut`, or `none`); `@graphics_backends` declares graphics backends. Effects without export support are treated as `preview_only` and blocked before delivery. `@occlusion background_independent` declares that an effect does not depend on lower layers and may participate in timeline occlusion culling. Undeclared effects are conservatively treated as unsafe.

#### AI Plugins (`.tai`)

```text
// --- Topos AI Plugin Metadata ---
// @name AI Upscale 4X
// @ai_model models/upscale/4x_ultrasharp.onnx
// @ai_tile_size 512
// @ai_overlap 32
// @ai_scale 4
// -----------------------------
```

Full plugin conventions, including the two plugin systems: [docs/PLUGIN_DEV_GUIDE.md](../PLUGIN_DEV_GUIDE.md).

### 5. Development Workflows

#### Add a Feature Module

1. Create `src/features/my_feature/`.
2. Implement the module interface in `module.py`.
3. Define pages by implementing `create_pages()` and returning `PageDescriptor` objects.
4. Register the module through `registry.register_module()` in the product setup.

#### Add a Product Edition

1. Create the launcher at `apps/my_product/main.py`.
2. Add a `ProductPreset` in `src/products/__init__.py`.
3. Create `src/products/my_product.py`.
4. Add a PyInstaller spec under `packaging/pyinstaller/`.

#### Add a Composite Node

1. Define the node in `src/features/composite/nodes/my_node.py`.
2. Implement its evaluator in `src/features/composite/evaluators/my_evaluator.py`.
3. Register its contract category in `core/node_capabilities.py` so the six-state matrix can explain it.
4. Register the node in `nodes/__init__.py`.

#### Develop the Native Codec

For builds and test gates, see [building.md](building.md) (`bash native/topos_codec/run_tests.sh` / `TOPOS_CODEC_LIB`).

### 6. Development Guidelines

- **Thread safety:** Never call OpenGL APIs directly from the UI thread.
- **Resource release:** Explicitly call `.release()` on ModernGL objects.
- **Coordinate transforms:** UI pixels → mask UV (0–1) → OpenGL NDC (−1–1).
- **GLSL types:** Write floating-point literals with a decimal point (`1.0`, not `1`).
- **Feature flags:** Use `src/shared/feature_flags.py` to check feature availability.
- **Effect capability contracts:** New effects must declare `EffectExecutionCapability`. Effects without an export backend are `preview_only` and blocked during delivery rather than silently skipped.
- **Occlusion-culling safety:** Effects independent of lower layers may declare `@occlusion background_independent`; undeclared effects are conservatively treated as unsafe (fail-closed).
- **Virtual environments:** `.venv` is tied to the CPU architecture and must be recreated when changing machines or chips.

### 7. Development References

- [Plugin development guide](../PLUGIN_DEV_GUIDE.md) — `.tglsl`/`.tai` conventions and the two plugin systems.
- [Module boundary rules](../module_boundary_rules.md) — feature layering and dependency constraints.
- [TAEP engineering conventions](../taep_engineering_conventions.md) — coding and architecture rules.
- [HDR math contract](../hdr_math_contract.md) — HDR pipeline definitions.
- [Prompt library](../PROMPTS.md).
