# Shared 基础层架构（src/shared/）

> 撰写日期：2026-09-28 ｜ 分支：dev ｜ 依据：源码实读 + `docs/audit/2026-09-28/batches/` 审查报告佐证
> 本文是计划04「架构文档」的 Shared 基础层分册。信息全部来自对当前 dev 分支源码的实际阅读，文末附依据的源文件清单。

---

## 1. 分层契约：products → features → shared → app

### 1.1 分层总图

```
┌─────────────────────────────────────────────────────────────────────┐
│  apps/（可执行入口，薄壳）                                             │
│    topos_studio / topos_color / topos_edit / topos_composite /       │
│    topos_vault（独立服务进程）                                        │
│    职责：设置 TOPOS_PRODUCT_MODE 环境变量 → 委托 src/main.py 启动      │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ import
┌──────────────────────────────▼──────────────────────────────────────┐
│  src/products/（产品预设层）                                          │
│    ProductPreset + PRODUCT_PRESETS + module_loader 委托              │
│    topos-studio / topos-color / topos-edit / topos-composite        │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ 只允许 import src.shared.feature_flags
┌──────────────────────────────▼──────────────────────────────────────┐
│  src/features/（可选特性层）                                          │
│    color（恒启用）/ edit / composite / deliver /                      │
│    teaching_animation / ai_workflow / ai_harness / scripting / …     │
│  依赖方向：→ shared、→ products、→ 其它 feature 的边界模块            │
│  （module.py / facade.py / commands_facade.py 等白名单）              │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ 只依赖 stdlib / 三方库 / shared / products
┌──────────────────────────────▼──────────────────────────────────────┐
│  src/shared/（共享基础层，本文范围）                                   │
│    project/  gpu/  media/  config/  i18n/  preferences/  db/         │
│    vault/  diagnostics/  logging/  paths/  actions/  panels/         │
│    codec/  color/  video/  effects/  export/  ui/  …                 │
│  硬约束：禁止 import src.features.* 与 src.app.*                      │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ shared 被所有上层消费
┌──────────────────────────────▼──────────────────────────────────────┐
│  src/app/（应用壳层）                                                 │
│    main_window / app_context / feature_registry / module_loader /    │
│    controllers / menu / ui / page_contract                           │
│  依赖方向：→ shared（任意）、→ products、→ feature 边界白名单模块      │
└─────────────────────────────────────────────────────────────────────┘
```

依赖规则（依据 `docs/module_boundary_rules.md`，该文档由 `tests/test_boundary_guardrails.py::TestAppLayerForbiddenImports` 静态强制）：

| 层 | 允许 import | 禁止 import |
|---|---|---|
| `src/app` | `src.shared.*`、`src.products.*`、feature 边界白名单模块（`edit.module`、`composite.facade`、`deliver.module`、`color.*` 等） | `src.features.*.core/ui/render/adapters` 内部实现 |
| `src/shared` | stdlib、三方库（PyQt6/ModernGL/numpy/PyAV 等）、`src.shared.*`、`src.products.*` | **`src.features.*`、`src.app.*`（硬红线）** |
| `src/features/<f>` | `src.shared.*`、`src.products.*`、其它 feature 的 `module.py`/`facade.py` | 任意深入其它 feature 内部 |
| `src/products` | stdlib、`src.products.*`、`src.shared.feature_flags` | 其它一切 |

两个值得注意的裁定（`module_boundary_rules.md`「Platform / Backend Code Ownership」节，P9D-03 落档）：

- 后端无关契约/探测/台账放 `src/shared/gpu/`（如 `graphics_backend.py` 的 `GraphicsBackend` Protocol）；包装 feature 侧 renderer 的具体实现必须放 feature 侧（如 `src/features/color/core/opengl_render_backend.py`、`src/features/composite/render/metal_compute_bridge.py`），否则违反「shared 不得 import features」。
- `src/shared/gpu/gpu_scope_engine.py` 目前 import 了 `src.features.color.core.scope_types` 并引用 `features/color/shaders/gpu_scope.glsl`，是 shared 层对 features 的一个已知例外（历史共享物，双侧渲染线程共用）；改动 scope_types 时需同步评估本文件。

### 1.2 feature_flags 与产品裁剪

产品裁剪由两套机制叠加（`src/shared/feature_flags.py` + `src/products/__init__.py`）：

**FeatureFlags（运行时开关，`src/shared/feature_flags.py`）**
- `DEFAULT_FEATURE_FLAGS`：`library/color/edit/composite/deliver/teaching_animation` 默认全开；AI 能力旗标（`ai_assistant/media_analysis/cloud_analysis/generation/mcp_server`，M0 冻结）与商业化自动动作旗标（`agent_auto_mode/cloud_upload/auto_export`）默认全部 False，只能显式开启。
- `load_feature_flags(config)` 的优先级链：① `TOPOS_FEATURE_<NAME>` 单项环境变量（最高）→ ② `TOPOS_DISABLE_FEATURES` 批量禁用 → ③ 产品模式 preset（仅当 `TOPOS_PRODUCT_MODE` 显式设置）→ ④ config 文件 `features` 段 → ⑤ 默认值。**Color 不可禁用**（Step 6 强制回 True）。
- `PRODUCT_MODE_PRESETS`：`color/studio/edit/composite` 四种模式各自的功能矩阵；`PRODUCT_MODE_ALIASES` 把打包名（`topos-studio` 等）归一为短名。
- **Work Mode**（studio 专属）：`image` / `video` 二选一（无 "full"），存于 config `work_mode` 键；`WORK_MODE_FEATURE_OVERRIDES` 仅在 studio 下叠加（image 隐藏 Edit），不覆盖显式 `TOPOS_FEATURE_*`。`get_effective_layout_mode()` 产出 `studio_image` / `studio_video` 复合键用于布局隔离。

**ProductPreset（产品预设，`src/products/__init__.py`）**
- `ALWAYS_ENABLED = {color, library}`：`resolved_enabled = (enabled - disabled) | ALWAYS_ENABLED`，保证任何产品都有调色与媒体库。
- 四个预设：`topos-studio`（全功能）、`topos-color`（调色+轻量交付，禁 edit/composite/teaching_animation）、`topos-edit`（剪辑+交付）、`topos-composite`（合成+teaching_animation，禁 edit）。
- `src/products/topos_*.py` 的 `register()` 委托 `module_loader.register_preset_modules`，按 preset 过滤同一张 `_FEATURE_MODULES` 注册表（R2-3 收敛：main.py 内联、module_loader、products register() 三份平行实现已合并为一份）。

**注册链路（`src/main.py` 启动序列）**：
`ConfigManager` → `load_feature_flags(config)` → `I18N.load_locale` → `FeatureRegistry()` + `AppContext` → `register_color_module`（核心，导入失败快速中止）→ `register_builtin_modules`（可选模块按 flag 注册，单模块失败隔离为 warning，失败清单给 UI 提示 R2-8）→ `MainWindow(feature_flags, feature_registry)` → `app_context.save_router = window.save_router` → `registry.register_all_save_handlers()`。

`src/app/feature_registry.py::FeatureRegistry` 是特性模块的中央登记处：注册顺序 i18n → pages → actions → ai_providers → save_handlers，每步 try 隔离（R2-4）。**消费方裁剪**：`ActionRegistry`、`PreferencesRegistry`、`PanelRegistry`（均在 src/shared）都接受 `enabled_features` 参数过滤贡献项。

---

## 2. 工程包：src/shared/project/

### 2.1 ToposProject 数据模型（topos_project.py）

- 唯一工程对象，`@dataclass(frozen=True)` 不可变保证线程安全；工厂方法返回新实例。
- `ProjectMode` 四种：`standalone_image`（单图调色，schema v2 引入）/ `edit_project` / `composite_shot` / `mixed_project`。
- 结构是「索引 + 引用」：`project.json` 只存 `MediaAssetRef / ProjectBinRef / TimelineRef / ColorStackRef / CompositionRef / DeliverPresetRef` 等轻量索引；完整数据在各 feature 子目录 JSON（`edit/media_pool.json`、`color/color_stacks.json` 等）。
- 活动引用（`primary_asset_id`、`active_timeline_id`、`active_composition_id`）+ `revision` 字段（TAEP P0 工程逻辑乐观锁，单调递增，与 schema 版本正交）。
- `is_virtual_asset_path()`：`topos://` 前缀标识应用生成的非文件资产。

### 2.2 包格式与序列化（topos_project_io.py + manifest.py + payload_cache.py）

`.topos` 是 ZIP 包，`ToposProjectIO` 是唯一读写入口：

```
project.topos (ZIP)
├── manifest.json          # ToposManifest：format="topos_project_archive"、schema_version、
│                          #   edit_content_schema_version、project_mode、features
├── project.json           # ToposProject 核心索引
├── edit/    media_pool.json, bins.json, timelines.json, timeline_sequences.json, comp_clips.json
├── color/   color_stacks.json, looks/
├── composite/ graphs.json, viewer_state.json
├── deliver/ presets.json
├── ui/      workspace_state.json
├── ai/      session_catalog.json, plans.json, jobs.json, analysis.json, runtime.json, trace.json, plugins.json
├── ai_workflow/ workflows.json        # teaching_animation/ 同级可选目录
├── assets/  baked/, ai_results/, thumbnails/, proxies/
└── cache_manifest.json
```

- **Schema 版本迁移**：`TOPOS_SCHEMA_VERSION = 3`（manifest.py）；`from_dict` 对 `schema_version < 3` 的旧包**静默自动升版**（v2→v3 只 bump 版本号），加载时若 `schema_version > TOPOS_SCHEMA_VERSION` 直接拒绝。`edit_content_schema_version`（P2-13，当前 3）是编辑内容载荷契约版本，**迁移链由 feature 侧 `src/features/edit/core/project_migration.py` 注册**，共享层只持数值避免反向依赖。
- **跨产品数据保留**（payload_cache.py）：保存前 `ProjectArchivePayload.load_from_file` 把既有 ZIP 全部条目读入缓存，当前产品只覆写自己的模块，未知模块（其他产品写入的）原样回写——防止 topos-color 打开后把 edit 时间线数据抹掉。`None` 参数 = 保留已有，空 dict = 显式清除。
- 路径相对化：素材路径存相对路径，加载时转绝对；离线素材检测/重链接（`check_offline_assets` / `relink_assets` / `OfflineAssetManager`）。
- 序列化格式 JSON（默认）/ MessagePack（可选）；`schema_v1_spec.py` 提供 JSON Schema 注册表（`SCHEMA_REGISTRY_V1 / get_schema / validate_json`）。
- 旧格式兼容：`ProjectManager`（project_manager.py）同时支持 ZIP v2.0 与 legacy 明文 JSON v1.0（魔数 `PK` 判别），是 Color 页单图 sidecar 工程的旧路径。

### 2.3 SaveRouter 统一保存路由（save_router.py）

文件头声明的红线：**Save 不生成旧格式工程文件；工程内调色统一保存到 .topos 包；页面不能绕过 SaveRouter 直接写工程文件。**

- `SaveContext` 六种上下文：`LIBRARY_SINGLE / COLOR_ASSET / COLOR_CLIP / COMPOSITE / EDIT_TIMELINE / DELIVER`；`get_current_page` 回调决定路由。
- 保存路由表：image project → `color_stacks[primary_color_stack_id]`；video asset → `color_stacks[asset_default_stack_id]`；timeline_clip → `color_stacks[clip.color_stack_id]`；Composite → `compositions[active_composite_id]`；Edit → `timeline_sequences`。
- 回调解耦：`set_callback(s)` 注入 `get_current_page / get_current_edit_project / …`，避免直接依赖 MainWindow 内部状态；feature 模块经 `register_handler(page_id, handler)` 注册页面级保存器（由 `FeatureRegistry.register_all_save_handlers` 在 save_router 就绪后统一接线）。
- 附加职责：备份（`backup_project_async`，轮转上限 `max_backups`）、新建/加载工程（`create_new_project / load_project`）、`get_save_target()` 产出 SaveTarget 描述。
- 脏状态与自动保存由 `ToposProjectController`（topos_project_controller.py，QObject）负责：`PageType` 页面粒度 DirtyState、`mark_dirty/clear_dirty` 信号、`start_auto_save(interval_minutes=5)`。
- 跨 feature 文档协议：`document.py::ProjectDocument`（Protocol：`document_type / dirty / save_to_path / load_from_path`），feature 把自己的数据暴露为 ProjectDocument 即可参与统一保存生命周期而无需耦合具体实现。
- 视频模式工程库：`library.py` 扫描固定库目录（默认 `ToposProjects/`）中的 .topos 包做卡片化元数据轻读取（不整包加载）+ 封面缩略图；图像模式不触碰（sidecar 模式）。
- 只读服务：`topos_project_read_service.py::ToposProjectBundle` / `ToposProjectReadService` 为外部读取（如 AI）提供 `to_legacy_dict()` 兼容视图。

---

## 3. GPU 抽象：src/shared/gpu/

### 3.1 四维后端模型（backend_types.py / capabilities.py / backend_selector.py，P2 系列）

单一 `backend` 枚举无法表达真实选择空间（CUDA 是 compute/video 生态而非窗口图形后端），因此拆为四个**独立可序列化**的维度，`auto` 为中性值、`force_` 前缀用于回归/诊断：

| 维度 | 枚举 | 职责 |
|---|---|---|
| graphics | auto/opengl/metal/d3d11/d3d12 | 窗口/光栅：纹理、FBO、present |
| compute | auto/glsf_fragment/glsf_compute/metal_compute/cuda/directml/cpu | 图像计算：降噪、示波器、直方图、缩放 |
| decode | auto/cpu/videotoolbox/nvdec/d3d11va/qsv/amf | 压缩视频解码 |
| interop | auto/none/iosurface/cvmetal/d3d_shared/cuda_interop | 解码面 ↔ 渲染资源共享 |

- **BackendCapabilities**（capabilities.py）：probe 支撑的能力矩阵。复用既有探测（不建平行真相）：decode/VT → `src.shared.media.macos_vt.hardware_capability`；Windows → `src.shared.media.windows_hw.capability`；interop → `src.shared.media.topos_surface_capability`（zero_copy 主张仅在 `evidence == "probe"` 时可信，P0-06）；graphics → 活的 GL driver 串。诊断规则（.clinerules:11）：不可用/未探测维度必须带显式 reason，禁止静默视为可用。
- **backend_selector**（P2-06）：AUTO 沿每维偏好链（如 compute：metal_compute → glsl_compute → glsl_fragment → cpu）选第一个 probe 可用的后端，跳过的步骤记入 fallback_trail；显式请求不可用时**保持 unavailable + probe reason，绝不静默降级**。产出 `ResolvedBackendPlan`（四维 + §4.3 必填回退日志字段）。
- 选择持久化在 ConfigManager 的 `backend` 键，`get_backend_selection()` 还原为 `BackendSelection`（P2-05）。
- 环境变量约定：`TOPOS_BACKEND_<DIM>=[force_]<kind>`（backend_types.py 的 `FORCE_PREFIX`/`ENV_PREFIX`）。

### 3.2 GL 运行时设施

- **gl_backend.py**：`GLContextManager`（单例，`AA_ShareOpenGLContexts` 下初始化 ModernGL context，`QMutex` 全局 GPU 锁；双重检查锁保证渲染线程/UI 生产线程并发首次到达也只有一个 mutex）+ `FramebufferManager`。被十余个模块引用的运行时助手。
- **gl_context_hub.py**：`GLContextHub` 单例按名登记 GL context（viewer/renderer/preview），2×2 像素探测两 context 是否共享纹理对象，结果缓存；`is_direct_texture_enabled()` 是直接纹理呈现决策的单一事实源。（batch-012 AUD-1214 Critical：`_do_pixel_probe` 的 ctx_b 未真正参与 GL 调用，结果取决于调用线程的隐含当前上下文——使用时注意该已知缺陷。）
- **gl_resource_registry.py**（P3-03）：GL 资源台账，纯记账不做 GL 调用。规范资源组：`upload/framebuffers/shaders/scope/denoise` 五组 + `present/grading/matte/scratch` 辅助组。`release_fn=None` 为纯台账模式（AsyncRenderer P3-02/03 阶段默认）；带 release_fn 的释放回调必须由持有 GL context 的渲染线程执行（调用方负责线程正确性）。
- **present_frame.py**（RP-P0 系列）：专业 NLE present 协议。`GpuPresentFrame` 携带 texture_id/fence/slot_id/generation/release 回调/色彩朝向元数据；`TextureLease` 状态机 `FREE → WRITING → READY → IN_VIEWER → RELEASABLE → FREE`；`OutputSlotPool` 强制 Viewer ack，未 ack 的 slot 永不复用。禁止：裸 tex_id 跨模块长期传递、缓存 GPU texture id（pool 复用会显示错帧）、热路径每帧 glFinish（用 fence+ack）。模块不依赖 Qt/GL（fence 用不透明 int），可 mock 测试。
- **graphics_backend.py**（P3-02）：`GraphicsBackend` Protocol（initialize/create_texture/create_target/render_pass/present/release）+ 生命周期状态机 `GraphicsBackendState`（uninitialized/ready/released/failed）+ 线程违例异常族（`BackendThreadViolationError`）。契约独立成模块的原因：Metal backend 必须能无 moderngl 依赖地 import 它；具体 GL 实现在 feature 侧 `opengl_render_backend.py`。
- **gl_pass_timer.py**：GPU pass 计时；**host_gpu_info.py**：宿主 GPU 信息；**gpu_scope_engine.py**：双页（Color AsyncRenderer / Composite CompositeRenderer）共用的原生 GPU 示波器管线（sample/waveform/vectorscope/parade/luma/histogram/CIE/false color 全部光栅化 pass、无 read()）；**composite_gpu_frame_handle.py**：合成侧 GPU 帧句柄。

### 3.3 Metal compute（metal/）

- `context.py`：`MetalComputeContext`（MSL 编译、dispatch、buffer/texture 绑定、fence 同步）+ `MetalKernel` + `MetalComputeError`。PyObjC 路线（ADR 0003 K4 复核：compute 不在视频渲染热路径，PyObjC 微秒级开销可忽略；原生 Obj-C++ 扩展留给 Phase 5+）。线程契约：`dispatch()` 只编码进当前 command buffer 不提交不等待（一次同步点可打包多 kernel）；`finish()` 才 commit + waitUntilCompleted；与 GL 全局锁完全无关。能力不足/编译失败抛显式异常，不静默降级；`is_supported()` 供可用性探测。
- kernel 集：`kernels.py`（大半径模糊/腐蚀膨胀，与 composite GLSL compute 逐名对齐，RGBA16F 线性 buffer）、`kernels_color.py`、`kernels_histogram.py`（Normalize 直方图，P4-03）、`kernels_dct/idct/enc/dec/video.py`（编解码相关）、`submission_scheduler.py`（提交调度）。只依赖 PyObjC/numpy，不触碰 feature 对象；GL 纹理 ↔ Metal 的桥在 feature 侧 `metal_compute_bridge.py`（macOS GL 4.1 与 Metal 无纹理共享通道，走 CPU staging）。

---

## 4. 媒体层：src/shared/media/

### 4.1 数据模型与工厂

- 核心数据结构（`__init__.py` 文档）：`metadata.Rational/VideoMetadata/AudioMetadata`、`timecode.Timecode`（P9.1，支持丢帧）、`timeline.TimelineClip/Track/Timeline`、`frame.DecodedFrame/PlaneInfo`、`pixel_format.PixelFormatRegistry`。全部 frozen dataclass，解耦外部库（不依赖 GStreamer/OpenCV 类型），`source.py` 定义 `MediaSource` Protocol（duck typing，不强制继承）。
- **factory.py**：`MediaSourceFactory` 后端选择器。auto 模式优先级：
  0. Topos 自研解码（libtopos_codec）——仅当内容探测（`is_topos_movie`，结构化 magic `TPIC`）确认是 Topos MOV 时优先（阶段 6，R-17）；Topos MOV 与通用 QuickTime 共用 `.mov` 扩展名，**按文件内容而非扩展名路由**，误探测在逐后端回退中自愈（topos open 失败 → ffmpeg）；
  1. FFmpeg(PyAV) + 硬件加速（可用且策略允许）；
  2. FFmpeg(PyAV) CPU。
  `MediaBackendDecision` 是内容路由的唯一判定（T-P0-10，文件签名 size+mtime_ns 做探测缓存失效键）。
- **ffmpeg_source.py**：`FFmpegMediaSource` 实现 MediaSource Protocol：元数据探测、顺序解码、精确 seek（GOP/intra codec 差异处理，`GopIndex` 缓存）、高位深（10/12/16-bit）支持、色彩元数据映射（primaries/transfer/space/range）。
- **topos_source.py**：自研 Topos MOV 解码后端。恒 planar 输出（YUV 4:2:2 [+Alpha]，uint16 满刻度码值）；全 intra + O(1) 索引使 seek 只是移动游标；顺序读批量预取（`decode_views_batch`，深度自适应，输出与逐帧逐位一致）；单实例不可重入（对齐 SourceManager._decode_lock 串行化假设）；普通接口不触碰 OpenGL/Qt，例外是显式 `read_frame_metal` 导出接口（创建 IOSurface→Metal 资源给渲染线程）。

### 4.2 硬件解码

- **decode_policy.py**：`DecodeTarget`（预览/母版/导出）+ `DecodePolicy` + `DecodePolicyManager` + `HardwareDecodeStatus`。硬件加速只是 DecodePolicy 的一个选项，**不改变 MediaSource 外部接口**；硬解输出无法安全转 numpy/planes 时先 CPU download，不做零拷贝，且记录 fallback 原因。
- **hwaccel.py**：`HardwareDecoder/HardwareCapability/FFmpegHwaccelSource`（P10.1/P10.2），平台自适应探测，优雅降级。平台策略：macOS 首选 FFmpeg VideoToolbox；Windows NVIDIA 硬解优先，AMD/Intel 默认 CPU（PyAV 每帧同步下载使 D3D11VA/QSV 劣于 CPU 多线程，`TOPOS_FORCE_WIN_HW_DECODE=1` 可强开）。
- **macos_vt/**：VideoToolbox/IOSurface 零拷贝 GPU 解码管线。`hardware_capability`/`capability.py` 能力门控，`vt_session/vt_compression/packet_demux` 解码会话，`source.py::MacOSVtZeroCopySource` 输出 `GpuDecodedFrame`（验收：`zero_copy == True`、`decode_path == "macos_vt_iosurface_zero_copy"`、绝不 `to_ndarray()`/`transfer()`），`iosurface_gl_bridge/cv_metal_bridge/external_texture` 桥接，`topos_surface.py` + `topos_surface_capability.py` 是 interop 维度 probe 的共享依据，另有 `gpu_encode_surface/gpu_export_bridge` 编码侧。
- **windows_hw/**：D3D11 体系（device/decoder/frame/gl_bridge/passthrough_chain/render_graph/shaders/texture_pool/sync/effects/vendor_policy/windows_zero_copy_probe 等），`capability.py`/`gpu_capability.py` 供 capabilities.py 的 platform 维度条目（给出 `not_windows` 类显式 reason）。
- **gpu_frame.py**：`GpuDecodedFrame`（GPU 驻留帧统一抽象：plane 描述、retain/release/fence 生命周期）。红线：zero_copy 路径禁止 Texture.write / to_ndarray / transfer；失败必须 fallback 不许黑屏；`release.release()` 只由渲染线程在安全时机调用、丢弃时 exactly once。

### 4.3 代理策略（disk proxy）

- **proxy.py**：`ProxyStrategy`（ORIGINAL/HALF/QUARTER/CUSTOM/AUTO）+ `ProxyConfig` + `calculate_proxy_size`（视口/性能自适应）；`DiskProxyConfig/DiskProxyInfo/ProxyStatus` 等磁盘代理数据类型。DecodePolicy 定格式与质量，ProxyStrategy 定分辨率降级，两者共同决定最终解码参数。
- **disk_proxy_service.py**：`DiskProxyService`（单例 `get_disk_proxy_service()`）统一管理磁盘代理生命周期：后台任务调度（不阻塞 UI）、状态追踪回调、与 VideoFrameProviderService 整合（`open_clip_with_disk_proxy`）。架构：MediaPool Panel / VideoFrameProviderService → DiskProxyService → ProxyManager（调度）+ ProxyInfoCache（状态）+ CallbackDispatcher。
- 周边模块：`proxy_manager.py`（生成任务）、`proxy_generator.py`、`proxy_manifest.py`（清单）、`asset_proxy_manager.py`（DORMANT，batch-343 备注）、`frame_cache.py`/`decode_queue.py`/`thumbnail_provider.py`/`performance_monitor.py`、`topos_image_sequence.py`/`topos_image_source.py`（TPIC 静态图像）、`adapter.py::VideoDecoderAdapter`（旧 BGR uint8 接口兼容层，composite `video_frame_node.py` 回退路径使用）。

---

## 5. 横切关注

### 5.1 i18n（src/shared/i18n/）

`I18N` 类级单例：`load_locale(code)` 加载 JSON 语言文件，`tr(key, default, **kwargs)` 翻译（占位符格式化，回退 en_US），`tr_or_none`，`reload_locale`（切语言后 `FeatureRegistry.reload_i18n` 会连带重载所有 feature 模块 locales 目录），`get_system_locale`。10 种语言（en_US/zh_CN/ja_JP/ko_KR/fr_FR/de_DE/es_ES/pt_BR/ru_RU/it_IT）；feature 侧各自 `locales/` 目录经 `register_module_locales` 登记。模块级便捷函数 `tr()/tr_or_none()`。（batch-338 AUD-33800 Major：占位符格式化只捕获 KeyError，ValueError/IndexError 会逃逸。）

### 5.2 preferences / config

- **src/shared/config/config_manager.py::ConfigManager**：JSON 配置（平台数据目录下 `config.json`，tempfile 原子写入）。DEFAULT_CONFIG 覆盖窗口/布局、`locale`、`features` 段（供 feature_flags Step 4）、`work_mode`、`developer_mode`、外观、`recent_files/recent_projects` 等；内建若干惰性迁移（如默认帧率 23.976→25、Render Cache 编解码器清理）；`get_backend_selection()` 反序列化四维后端选择（P2-05）。
- **src/shared/preferences/__init__.py::PreferencesRegistry**：feature 模块以 `PreferencePageDescriptor`（page_id/title/category/order/feature_id/懒 factory）贡献设置页；`ModularPreferencePage` Protocol 让页面完全自持 widget 创建/load/apply/defaults。PreferencesDialog 按 enabled_features 过滤动态构建。

### 5.3 db（src/shared/db/database.py）

SQLite（`topos.db`）四张表：`settings`（KV）、`layouts`（name+work_mode 复合主键，含 legacy 单主键表自动重建迁移）、`shortcuts`、`file_meta`（主键 NFC 归一——宿主与 file_explorer 必须同一键空间，避免 NFD/NFC 双行，R1-4）。（batch-329 AUD-32901 Major：排序热路径每次调用新建/关闭连接，无复用无批量接口。）

### 5.4 vault（src/shared/vault/）

主项目与 vault 服务（`apps/topos_vault/` 独立进程）之间**唯一**耦合点：`client.py::VaultClient`（只读 HTTP 客户端，`VaultStats`）、`bridge.py`（vault 检索结果 → TAEP `SemanticSegment`，喂 AI 剪辑 Executor；clip/face_track/transcript/search_hit 四种转换）。当前 DORMANT（batch-386：src/ 内无生产引用，仅 tests/vault/ 引用；bridge 的 SegmentKind/字段契约与 `src/features/edit/ai_editing/editorial_models.py` 实测一致）。

### 5.5 diagnostics（src/shared/diagnostics/）

- **performance_bundle.py**（P0-10）：`collect_performance_bundle()` 汇总帧 trace / 引擎遥测 / 像素拷贝计数 / 资源快照 / GPU pass 时间为一份 JSON；`export_performance_bundle()` 落盘。脱敏：移除/遮蔽素材路径、工程路径、用户目录（§16.3 不上传用户数据）。Deliver/Quick Export 复用同一入口（`extra_sections`）。
- **resource_snapshot.py**：资源快照；**flash_diag.py**：闪烁诊断。另有 `src/shared/render_diagnostics.py`（渲染诊断，render 包门口）。

### 5.6 其它基础设施

`paths/paths.py`（get_project_root/get_lut_dir/get_models_dir/get_cache_dir 等资源路径单一来源）；`logging/log_manager.py::LogManager`（单例，控制台+轮转文件 handler，config 驱动级别/路径/大小，运行时可重配）；`ffmpeg_locator.py`（FFmpeg 定位）；`app_restart.py`（应用重启）；`actions/`（`MenuActionDescriptor` 动作贡献系统）与 `panels/`（`PanelDescriptor` + `PanelRegistry`，Window > Panels 菜单按 feature_id/类别分组过滤）——两者都是 feature → app 的解耦贡献通道。

---

## 6. 关键接口表与改动注意事项

### 6.1 关键接口表（模块 → 主要入口 → 消费者）

| 模块 | 主要入口 | 消费者（实测 grep） |
|---|---|---|
| `shared/feature_flags` | `load_feature_flags` / `FeatureFlags.is_enabled` / `get_effective_layout_mode` | `src/main.py`、app 壳、products、各 feature 模块注册门控 |
| `shared/project/__init__` | `ToposProject` / `SaveRouter` / `ToposProjectIO.save/load` / `ProjectDocument` | app 层（main_window、file/edit/deliver/menu 控制器、project_manager_dialog）；features：edit（module/project.py/edit_project_io/project_serializer/project_migration/commands/relink）、color（color_stack_hydrator） |
| `shared/project/save_router` | `SaveRouter.save()/save_with_context()` / `register_handler` | MainWindow 持有（`self.save_router`，L425）；各 feature 模块 `register_save_handlers(AppContext)` |
| `shared/gpu/backend_types`+`backend_selector` | `BackendSelection` / `resolve_backend_plan` / `BackendCapabilities` | ConfigManager 持久化；feature 侧渲染后端选择；诊断面板 |
| `shared/gpu/gl_backend` | `GLContextManager.instance()` / `FramebufferManager` | color（gl_viewport/canvas/async_renderer/image_engine/opengl_render_backend）、composite 渲染族、edit 预览/导出渲染器、preferences_dialog |
| `shared/gpu/present_frame` | `GpuPresentFrame` / `OutputSlotPool` / `TextureLease` | color/composite 渲染器 → viewport 线程协议 |
| `shared/gpu/gl_resource_registry` | `GLResourceRegistry.register/release_group/snapshot` | AsyncRenderer、composite gpu_resource_pool、诊断报告 |
| `shared/gpu/metal` | `MetalComputeContext` / `MetalKernel` / `is_supported` | composite `metal_compute_bridge`、edit `timeline_metal_export_renderer`、histogram_manager Metal 分支 |
| `shared/media/__init__` | `MediaSourceFactory.create/create_with_hwaccel` / `DecodedFrame` / `Timecode` | color（async_renderer/image_engine/color_session/timeline_clip_strip）、composite（video_frame_node/video_context 等）、edit 播放链、teaching_animation |
| `shared/media/ffmpeg_source` | `FFmpegMediaSource`（open/seek_frame/read_next_frame） | factory 后端 1/2、各播放/导出路径 |
| `shared/media/topos_source` | `ToposMediaSource` / `is_topos_movie` / `read_frame_metal` | factory auto 路由（TPIC 内容探测优先）、Metal 导出 |
| `shared/media/macos_vt` | `MacOSVtZeroCopySource` / `get_zero_copy_capability` | factory 硬解路径、gpu capabilities probe |
| `shared/media/proxy`+`disk_proxy_service` | `get_disk_proxy_service()` / `calculate_proxy_size` / `DiskProxyInfo` | MediaPool Panel、VideoFrameProviderService、播放整合 |
| `shared/i18n` | `I18N.tr` / `load_locale` / `reload_locale` | 全 UI 层（app + 所有 feature），main.py 启动加载 |
| `shared/config` | `ConfigManager.get/set/save_config` / `get_backend_selection` | main.py、MainWindow、PreferencesDialog、feature_flags Step 4 |
| `shared/preferences` | `PreferencesRegistry.register` / `PreferencePageDescriptor` | feature 模块注册设置页、PreferencesDialog 构建 |
| `shared/db` | `Database`（settings/layouts/shortcuts/file_meta） | MainWindow 布局持久化、file_explorer 共享 file_meta |
| `shared/vault` | `VaultClient` / `clip_to_semantic_segment` 等 | （DORMANT）TAEP AI 剪辑 Executor 预留 |
| `shared/diagnostics` | `export_performance_bundle` / resource_snapshot | 帮助菜单诊断导出、Deliver/Quick Export 复用 |
| `shared/actions`、`shared/panels` | `MenuActionDescriptor` / `PanelRegistry` | feature 模块贡献、menu builder / Window > Panels |

### 6.2 改动注意事项

1. **边界红线**：shared 新增代码禁止 import `src.features.*` / `src.app.*`（test_boundary_guardrails 强制）。需要 feature 对象的"具体后端实现"一律放 feature 侧（`module_boundary_rules.md` 平台归属裁定表）。`gpu_scope_engine.py` 对 `features.color.core.scope_types` 的既有 import 是已知例外，动 scope_types 需双侧评估。
2. **SaveRouter 红线**：页面不得绕过 SaveRouter 直接写工程文件；新增 feature 的保存必须走 `register_handler` / `register_save_handlers(AppContext)`；`ToposProjectIO.save` 的模块参数遵循「None=保留、空 dict=清除」语义，勿用 `None` 表达"清空"。
3. **schema 演进**：`TOPOS_SCHEMA_VERSION` bump 需在 `ToposManifest.from_dict` 补自动迁移分支；编辑内容契约改动走 feature 侧 project_migration 迁移链并同步 `CURRENT_EDIT_CONTENT_SCHEMA_VERSION`；`revision` 是乐观锁不是 schema 版本，勿混用。
4. **线程契约**：UI 线程绝不调用 GL（.clinerules）；`GLResourceRegistry.release_group` 的 release_fn 必须在渲染线程执行；`GpuDecodedFrame.release()` 只在渲染线程安全时机调用且 exactly once；`OutputSlotPool` slot 未 ack 永不复用，禁止缓存 GPU texture id、禁止热路径每帧 glFinish；Metal `dispatch()` 不提交、`finish()` 才同步，与 GL 全局锁无关。
5. **不静默降级**：后端选择、硬解、Metal、probe 的任何不可用都必须产出显式 reason（.clinerules:11）， AUTO 链记 fallback_trail；硬解失败降级 CPU 时记录 HardwareDecodeStatus 原因。
6. **AI/自动化旗标默认 False**（M0 冻结）：新增 AI 能力面必须先注册为显式 False 默认的旗标，禁止 import 副作用开启；Color 恒不可禁用。
7. **共享 SQLite 键空间**：file_meta 主键必须 NFC 归一（`_meta_key`），宿主与 file_explorer 两端一致，否则双行/误清理回归。
8. **i18n**：新增语言要在 shared 与每个 feature 的 locales/ 目录同步建 JSON 并登记 `SUPPORTED_LOCALES`；运行时切语言走 `I18N.reload_locale` + `FeatureRegistry.reload_i18n`。
9. **factory 路由**：`.mov` 按内容（TPIC magic）而非扩展名路由；新增后端时保持逐后端回退自愈与 `MediaBackendDecision` 单一判定，缓存失效键是文件签名 (size, mtime_ns)。
10. **已知质量债**（审计佐证，改动时顺带规避）：`gl_context_hub._do_pixel_probe` 的 ctx_b 未真正参与探测（AUD-1214 Critical）；`vault/bridge.py` segment_id 三元优先级缺陷（AUD-38600）；`i18n.tr` 占位符异常逃逸（AUD-33800）；`db` 热路径每调用新建连接（AUD-32901）；`media/__init__` 私有名漏出 `__all__`（AUD-34308）。

---

## 7. 依据的源文件

以下文件均在仓库 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/` 下（相对路径省略前缀）：

**分层契约与注册**
- `src/main.py`（启动序列：config → flags → i18n → registry → module_loader → MainWindow）
- `src/app/feature_registry.py`、`src/app/app_context.py`、`src/app/module_loader.py`
- `src/shared/feature_flags.py`、`src/products/__init__.py`、`src/products/topos_studio.py`
- `src/shared/actions/__init__.py`、`src/shared/panels/registry.py`、`src/shared/preferences/__init__.py`
- `docs/module_boundary_rules.md`（分层规则与平台归属裁定的既有落档）
- `apps/topos_studio/main.py`（产品入口薄壳）

**工程包**
- `src/shared/project/__init__.py`、`topos_project.py`、`manifest.py`、`topos_project_io.py`、`payload_cache.py`、`save_router.py`、`topos_project_controller.py`、`topos_project_read_service.py`、`document.py`、`library.py`、`offline_asset_manager.py`、`project_manager.py`、`schema_v1_spec.py`（导出符号核对）
- `src/features/edit/core/project_migration.py`（编辑内容迁移链归属核对）

**GPU**
- `src/shared/gpu/__init__.py`、`backend_types.py`、`capabilities.py`、`backend_selector.py`、`graphics_backend.py`、`gl_backend.py`、`gl_context_hub.py`、`gl_resource_registry.py`、`present_frame.py`、`gpu_scope_engine.py`（头部契约）、`gl_pass_timer.py`/`host_gpu_info.py`/`composite_gpu_frame_handle.py`（清单核对）
- `src/shared/gpu/metal/__init__.py`、`context.py`、`kernels.py`

**媒体**
- `src/shared/media/__init__.py`、`factory.py`、`ffmpeg_source.py`、`topos_source.py`、`source.py`（经 __init__ 文档）、`decode_policy.py`、`hwaccel.py`、`gpu_frame.py`、`proxy.py`、`disk_proxy_service.py`
- `src/shared/media/macos_vt/__init__.py`、`source.py`；`src/shared/media/windows_hw/`（目录清单）

**横切**
- `src/shared/i18n/i18n.py`、`src/shared/config/config_manager.py`、`src/shared/db/database.py`、`src/shared/vault/__init__.py`、`src/shared/diagnostics/performance_bundle.py`、`src/shared/paths/__init__.py`、`src/shared/logging/log_manager.py`

**审计佐证（docs/audit/2026-09-28/batches/）**
- `batch-012-shared-gpu-core.md`、`batch-013-shared-gpu-registry-scope.md`（GPU 层问题与契约）
- `batch-338-shared-i18n.md`、`batch-326-shared-config.md`、`batch-329-shared-db.md`、`batch-330-shared-diagnostics.md`、`batch-386-shared-vault.md`
- `batch-343`~`batch-362`（shared/media 系列）、`batch-368`~`batch-371`（shared/project 系列）
