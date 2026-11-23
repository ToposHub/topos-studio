# GPU 调用清单（阶段 0.2）

基线提交 `6249e136`，机器：i9-9900K + AMD Radeon RX 6900 XT（Intel 独显类，macOS 15.3.1）。
对应 [迁移计划](../plans/2026-11-23-macos-metal-full-migration.md) 阶段 0.2；逐条原始命中见
[evidence/gl-inventory-raw-6249e136.txt](evidence/gl-inventory-raw-6249e136.txt)。

## 口径

```bash
rg -n 'QOpenGLWidget|OpenGL[. ]|moderngl|[.]glo\b|GLsync' src apps plugins
```

- 静态命中 **1623 条 / 189 个文件**。其中 **89 个文件直接绑定 GL API**（导入 moderngl / PyOpenGL /
  `QOpenGLWidget` 子类、`.glo`、`glFenceSync` 等）；其余 100 个文件只在注释或文档字符串里声明
  「不使用 GL」，或只经封装层（`gl_backend`、`iosurface_gl_bridge`、`yuv_gpu_upload`）间接访问。
- 下表按**运行时真实到达的路径**组织（G-01 起），每条带入口、阶段、预计能力、计划编号、当前
  实际后端与回退方式。文件清单只列直接绑定者，封装层调用方见原始命中。
- 阶段 7 按本表逐条销账，状态取四种之一：`待迁` / `兼容保留` / `非图形` / `退役`。
- **2026-11-23 台账**（阶段 1–6 与 8.5 / 8.6 的代码落地后）：`兼容保留` 6 条（G-09、G-10、G-13、G-22、G-35、
  G-36），`非图形` 1 条（G-16），`退役` 0 条，`待迁` 28 条，另 G-30 非生产、G-37 不在本次范围。`待迁` 里有 11 条的
  Metal 路径已经实现并对拍、
  但默认关闭或没有生产调用方（G-02、G-03、G-04、G-05、G-11、G-12、G-14、G-18、G-19、G-21、G-23）；G-04 与
  G-25 的 IOSurface 中转要等原生视口转为默认后才退役。没有任何一条转为默认开启，也没有一条被退役。

## 后端选择的真实现状

生产中有两套并行的「选择器」，迁移期间必须同时处理：

| 层 | 位置 | 状态 |
|---|---|---|
| 声明式四维选择器 | `src/shared/gpu/backend_types.py`、`backend_selector.py`、`capabilities.py` | 已完成但**生产未接线**（仅测试使用）；`auto` 偏好链为 `[OPENGL, METAL]`，与迁移目标相反 |
| 实际生效 | `ConfigManager.get_backend_selection()`（`src/shared/config/config_manager.py:577`）+ 环境变量直读 | 导出端 `timeline_export.py:9890` 与预览端 `metal_timeline_preview.py:86` **各自解析** |

`auto` 在生产中的真实语义：

- **导出**（`TimelineExporter._select_timeline_graphics_backend`）：非 Windows 最终返回 `opengl`。
- **预览**（`metal_preview_mode()`）：返回 `lut`——仅「NV12 SDR VT 帧 + 可选 LUT」走 Metal compute，
  且结果仍经 BGRA IOSurface 转回 GL 槽位上屏；其余全部 GL。

## 环境变量

| 变量 | 位置 | 默认 | 语义 | 影响路径 |
|---|---|---|---|---|
| `TOPOS_BACKEND_GRAPHICS` | `timeline_export.py:9898`；`metal_timeline_preview.py:93` | 未设 → 配置（`auto`） | `auto/opengl/metal/d3d11/d3d12`，可带 `force_` | G-07 导出后端；G-03/04 预览门 |
| `TOPOS_EDIT_METAL_PREVIEW` | `metal_timeline_preview.py:90` | 空 | 假值 → `off`；真值在 `auto` 下 → `all` | G-03/04 |
| `TOPOS_EXPORT_NATIVE_VT` | `timeline_export.py`（`_maybe_create_default_native_gpu_bridge`、`_try_export_metal_h264_lut`） | 空 | 假值关闭 Metal H.264 快路（它在 macOS 上默认开启）；真值另启用 opt-in 的常规 VT 桥 | G-07 |
| `TOPOS_EDIT_METAL_VIEWPORT` | `edit_gpu_preview_renderer.py` | 空 | 真值：合格帧由原生 `CAMetalLayer` 呈现（节目与 Source 监视器） | G-02、G-05 |
| `TOPOS_COMPOSITE_METAL` | `composite_graph_render_service.py` | 空（关） | 真值：合成图走 Metal compute 链（尚无生产调用方） | G-18、G-19 |
| `TOPOS_COLOR_METAL` | `shared/gpu/metal/feature_switches.py`（`opt_in_enabled`） | 空（关） | 真值：调色域的 Metal 路径——节点缩略图（已接线）；Color Viewer 呈现端沿用同一开关、尚未接线 | G-31；G-11、G-14 |
| `TOPOS_METAL_DISABLE` | `shared/gpu/metal/feature_switches.py` | 空 | 逗号分隔的能力名，单独关掉对应能力，优先于以上各开关 | 全部 |
| `TOPOS_GRAPHICS_LEDGER` / `_FILE` | `src/main.py`（`start_session_ledger`） | 空（关） | 真值：记录本次会话经 moderngl 工厂与 `MetalComputeContext` 构造函数取得的每个图形上下文，被 moderngl 接管的 Qt 上下文也计入并标 `adopt`（没被接管的 `QOpenGLWidget` 上下文不计），退出时写快照 | 全部（度量） |
| `TOPOS_GRAPHICS_FACTORY` | `shared/gpu/graphics_ledger.py` | `allow` | `deny`：台账钩子已安装时，GL 上下文工厂创建前即拒绝（测试注入） | 全部（测试） |
| `TOPOS_EXPORT_GPU_RENDER` | `timeline_export.py:2789` | 生产启用；pytest 下默认 CPU | 强制 GPU / CPU 合成 | G-06 |
| `TOPOS_EXPORT_METAL_PREFETCH` | `timeline_export.py:2755` | `1` | Metal 导出与 VT 解码重叠预取 | G-07 |
| `TOPOS_METAL_COMPUTE` | `compute_shader_manager.py:247` | 禁用 | 启用 Composite Metal compute 桥（CPU staging） | G-18 |
| `TOPOS_COLOR_GRAPH_RENDER` | `image_engine.py:885` | 关 | Color 页由图执行器接管 | G-16 |
| `TOPOS_HEADLESS_GL` / `_REQUIRE` | `composite_export_renderer.py:560`、`:609` | 空 | 独立离屏上下文与版本协商 | G-19 |
| `TOPOS_METAL_VIDEO_PREVIEW` | `macos_vt/source.py:54` | 关 | 开发者开关：Metal 视频零拷贝预览 | G-25 |
| `TOPOS_VIDEO_ZERO_COPY*` | `macos_vt/capability.py:61-108` | `auto` | 零拷贝门禁与缓存 | G-25 |
| `QSG_RHI_BACKEND` | `src/main.py:235`（硬编码 `opengl`） | — | Qt Quick 强制 OpenGL | G-01 |

## 运行路径清单

### 启动与全局

| 编号 | 入口 | 阶段 | 预计能力 | 计划编号 | 当前实际后端 | 回退方式 | 状态 |
|---|---|---|---|---|---|---|---|
| G-01 | `src/main.py:235-264`：`QSG_RHI_BACKEND=opengl`、`QSurfaceFormat` 4.5 Core、`AA_ShareOpenGLContexts` | 初始化 | 创建前能力探测；GL 初始化收拢到兼容会话 | 7.5 | 固定 OpenGL，无判定 | 无 | 待迁 |

### Edit 页

| 编号 | 入口 | 阶段 | 预计能力 | 计划编号 | 当前实际后端 | 回退方式 | 状态 |
|---|---|---|---|---|---|---|---|
| G-02 | `edit/ui/program_viewer.py:809`（`QOpenGLWidget`）；`_copy_shared_texture_to_display` `:1907`；`paintGL` `:1301` | 呈现 | 原生 `CAMetalLayer` 视口适配器 | 2.1、2.3 | 默认 OpenGL；`TOPOS_EDIT_METAL_VIEWPORT=1` 时合格帧由原生 `CAMetalLayer` 呈现（2.1–2.3）；真机（显示器清醒）H.264 时间线 392～398 / 399 帧，画面位置与 GL Viewer 逐像素一致（`572d9f4c`、`c92d8703`） | 开关关闭、非 Cocoa、图层挂载失败、呈现停滞 → GL Viewer | 待迁 |
| G-03 | `edit/core/edit_gpu_preview_renderer.py:1338`（上下文）；`_finalize_present_frame` `:5430`（`glFenceSync` `:5502`）；`_ensure_composite_fbo` `:4094`；`_try_metal_preview` `:2609` | 处理 | 剪辑 GPU 计划全程 Metal | 2.2、2.6 | OpenGL 为主；默认 `lut` 模式下单 LUT NV12 走 Metal compute 后落回 GL 槽位；`all` 模式下合成帧（多层、变换、裁切、混合、dissolve、dip）也走 Metal（2.6） | 片段效果、planar 源、comp 句柄、空间转场、调整剪辑 → GL；Metal 失败 → 本会话回 GL | 待迁 |
| G-04 | `edit/core/metal_timeline_preview.py:143-199`（`_CGLTexImageIOSurface2D` `:186`）；`blit` `:271` | 呈现中转 | 原生视口接通后**退役** | 2.1 | Metal compute → BGRA IOSurface → `GL_TEXTURE_RECTANGLE`（GL Viewer 路线仍在用） | GL 合成 | 待迁 |
| G-05 | `edit/ui/source_viewer.py:683`（`QOpenGLWidget`） | 呈现 | 复用原生视口适配器 | 2.5、7.1 | 默认 OpenGL（`SourcePresentGate` 单槽 latest-wins）；`TOPOS_EDIT_METAL_VIEWPORT=1` 时原生视口（2.5）；真机对照未做 | GL Viewer | 待迁 |
| G-06 | `edit/core/timeline_gpu_export_renderer.py:992`（独立上下文） | 导出 | Metal 导出渲染器覆盖后退役 | 3.3、3.4 | OpenGL；白名单场景（同尺寸 BT.709 H.264、串联 LUT、两段 dissolve、含音频、分数帧率）默认走 Metal 导出渲染器（3.0–3.2） | GPU 不可用 → CPU 合成器 | 待迁 |
| G-07 | `edit/core/timeline_export.py:7415`（`_try_export_metal_h264_lut`）、`:9890`（后端选择） | 导出 | 含音频、复杂图、HDR 的原生快路 | 3.1–3.3 | macOS 默认启用：Metal → NV12 → VideoToolbox；同容器封装 AAC 48 kHz；精确有理数帧率（3.1 / 3.2） | 白名单外 → 常规路径；`TOPOS_EXPORT_NATIVE_VT=0` 或 `TOPOS_METAL_DISABLE=export_native_vt` 关闭 | 待迁 |
| G-08 | `edit/core/timeline_clip_frame_provider.py` | 上传 | 跟随 G-03 的 GPU 计划 | 2.6、3.3 | OpenGL 上传 | CPU 帧 | 待迁 |
| G-09 | `edit/core/timeline_preview_renderer.py`（描述符 stash/pop 与门禁） | 协议 | 原生发布分支 | 1.1、2.2 | 透传 GL 与原生两种描述符（1.1、2.2） | hold 上一帧 | 兼容保留 |
| G-10 | `program_viewer.py` `_paintGL_impl` 叠加层（辅助线、字幕、手柄、错误态） | 呈现 | 独立叠加层 | 2.3 | `QPainter` 于 `paintGL` 内；原生视口下同一绘制序列画进独立叠加层图像（2.3） | 复杂叠加回 GL | 兼容保留 |

### Color 页

| 编号 | 入口 | 阶段 | 预计能力 | 计划编号 | 当前实际后端 | 回退方式 | 状态 |
|---|---|---|---|---|---|---|---|
| G-11 | `color/ui/gl_viewport.py:13`（`GLCanvasViewport`） | 呈现 | 阶段 2 宿主替换原生上屏 | 4.5 | OpenGL；Metal 呈现端已实现、默认关闭、未接线（4.5） | GL 视口 | 待迁 |
| G-12 | `color/core/async_renderer.py:529`；`image_engine.py`；`shader_compiler.py`；`color_graph_executor.py`；`color_graph_texture.py`；`corrector_render_service.py` | 处理 | 共享调色图的 Metal 纹理执行 | 4.1、4.2 | OpenGL；Metal 纹理执行器已实现并对拍（4.1–4.3），无生产调用方 | fail-closed（保留上一帧 / 报失败，不静默回退） | 待迁 |
| G-13 | `color/core/opengl_render_backend.py:45`（`OpenGLRenderBackend`） | 兼容后端 | 保留为 `GraphicsBackend` 契约的 GL 实现 | 7.5 | OpenGL | — | 兼容保留 |
| G-14 | `shared/gpu/gpu_scope_engine.py`；`shared/ui/widgets/gpu_scope_widget.py:149` | 处理 + 呈现 | GPU 示波器逐项迁移 | 4.5 | OpenGL；Metal 示波器引擎已实现并对拍（4.5），未接线 | 保留 GL 示波器 | 待迁 |
| G-15 | `color/core/export_denoise_bridge.py:62`；`video_denoise_gl.py`；`video_denoise_pipeline.py`；`video_spatial_denoise.py`；`video_temporal_denoise.py` | 导出 | 调色降噪导出桥 | 4.6 | OpenGL（独立上下文） | CPU 降噪 | 待迁 |
| G-16 | `color/core/image_engine.py:885`（`TOPOS_COLOR_GRAPH_RENDER`） | 调度 | 共享图入口统一 | 4.1 | 计划步骤经共享图入口（4.1）；开关控制 | 旧 layers 循环 | 非图形 |

### Composite 页

| 编号 | 入口 | 阶段 | 预计能力 | 计划编号 | 当前实际后端 | 回退方式 | 状态 |
|---|---|---|---|---|---|---|---|
| G-17 | `composite/ui/composite_viewport.py:976`（`CompositeViewer`） | 呈现 | Composite Viewer | 5 | OpenGL | GL 视口 | 待迁 |
| G-18 | `composite/render/composite_renderer.py:455`；`composite_render_worker.py`；`composite_render_output.py`；`gpu_resource_pool.py`；`pbo_readback.py`；`gl_utils.py`；`evaluators/*`（13）；`nodes/*`（平面节点） | 处理 | 2D 链、矩形/遮罩/效果、Tile 与缓存 | 5.1–5.3 | OpenGL；Metal compute 链已实现（5.1–5.6，`TOPOS_COMPOSITE_METAL=1`），默认关闭、无生产调用方 | `_fallback_callback` + fragment 回退；未迁移节点整图回退 | 待迁 |
| G-19 | `composite/render/composite_export_renderer.py:563/620/632`（`TOPOS_HEADLESS_GL`） | 导出 | 无头输出接同一 GPU 计划 | 5.6、7.4 | OpenGL 独立上下文；Metal 导出桥已实现（5.6），未接线 | CPU 渲染 | 待迁 |
| G-20 | `composite/evaluators/histogram_manager.py` | 处理 | 随示波器迁移 | 4.5 | GL compute；不可用时自动切 Metal compute | fragment 回退 | 待迁 |
| G-21 | `composite/render3d/gizmo_renderer.py`、`hdri.py`、`material_texture_loader.py`；`nodes/scene3d_node.py`（161 处）、`pointcloud3d_node.py`、`volume3d_node.py`、`particle_node.py` | 处理 | 3D 与 AOV | 5.5 | OpenGL；仅有不接图链的最小 3D 验证 pass（5.5） | 无 | 待迁 |
| G-22 | `shared/gpu/composite_gpu_frame_handle.py`（GLsync fence 生命周期） | 协议 | 接阶段 1 租约与完成语义 | 5.1 | GLsync fence；Metal 句柄（backend=`metal`）由合成链按完成事件发布（5.1 / 5.6） | — | 兼容保留 |

### 共享效果

| 编号 | 入口 | 阶段 | 预计能力 | 计划编号 | 当前实际后端 | 回退方式 | 状态 |
|---|---|---|---|---|---|---|---|
| G-23 | `features/shared/effects/effect_pass_runner.py` | 处理 | 内置效果与可译 `.tglsl` | 6.2、6.3 | OpenGL GLSL；`.tglsl` 转译器与 Metal 效果运行时已实现（6.2 / 6.3），无生产调用方 | 按 ID 保留兼容路径 | 待迁 |

### 媒体与播放

| 编号 | 入口 | 阶段 | 预计能力 | 计划编号 | 当前实际后端 | 回退方式 | 状态 |
|---|---|---|---|---|---|---|---|
| G-24 | `shared/video/yuv_gpu_upload.py`（75 处）；`video_upload.py`；`video_texture_pool.py`；`external_yuv_renderer.py` | 上传 | 跟随共享 GPU 帧 | 2.6、7.1 | OpenGL 上传 | CPU 上传 | 待迁 |
| G-25 | `shared/media/macos_vt/iosurface_gl_bridge.py` | 上传（零拷贝） | 原生帧进入视口后**退役** | 1、2 | IOSurface → GL 纹理 | CPU 上传 | 待迁 |
| G-26 | `shared/playback/gl_video_canvas.py:134/234`；`gl_render_worker.py:88` | 呈现 + 处理 | 共用 GPU 帧描述与视口宿主 | 7.1 | OpenGL；Metal 渲染器与判定逻辑已实现并对拍 GL（7.1，第一部分），画布未接线、渲染线程未完成 | GL 画布 | 待迁 |
| G-27 | `shared/video/video_async_renderer.py:221` | 导出 | 能力探测 | 7.4 | OpenGL 独立上下文 | CPU | 待迁 |
| G-28 | `shared/video/monitor_gpu_scale.py:67` | 呈现 | 外接监看接完成事件 | 7.3 | OpenGL 独立上下文；显式读回队列与监看目的计数已实现（7.3，第一部分），监看未接线 | CPU 缩放 | 待迁 |
| G-29 | `shared/video/cfa_gpu_debayer.py` | 处理 | CFA 解拜耳 | 7.2、4.6 | OpenGL；Metal 解拜耳已实现对拍 CPU 参考（7.2，第一部分），未接线 | CPU 解拜耳 | 待迁 |
| G-30 | `shared/video/yuv_gpu_self_test.py`；`macos_vt/vt_zero_copy_probe.py:365` | 工具 | 非生产 | — | OpenGL | — | 非生产 |

### App 与其他渲染模式

| 编号 | 入口 | 阶段 | 预计能力 | 计划编号 | 当前实际后端 | 回退方式 | 状态 |
|---|---|---|---|---|---|---|---|
| G-31 | `app/controllers/node_thumbnail_evaluator.py:223` | 处理 | 节点缩略图与画廊走共享帧 | 7.2、4.6 | 默认 OpenGL 独立上下文；`TOPOS_COLOR_METAL=1` 时节点缩略图走 Metal（4.6，真实应用里该线程不再建 GL 上下文）；画廊未接 | CPU 缩略图 | 待迁 |
| G-32 | `app/controllers/file_controller.py`（监看帧、抓帧读取 viewer 帧） | 呈现 | 有界异步 GPU→CPU 搬运 | 7.3 | 经 viewer GL 帧；显式读回队列已实现（7.3，第一部分），截图与抓帧入口未接 | 无帧可抓时跳过 | 待迁 |
| G-33 | `app/controllers/deliver_controller.py:630` | 导出 | 交付走 GPU 计划 | 7.4 | 仅 CLI / 测试路径建 GL；UI 会话明确拒绝 | 回退 Color 输出 | 待迁 |
| G-34 | `features/teaching_animation/composite_node.py`（`.glo`）；`render/benchmark.py:501` | 处理 | 进入视频帧合成处接 GPU 帧 | 7.4 | OpenGL | — | 待迁 |

### 共享基础设施

| 编号 | 入口 | 阶段 | 预计能力 | 计划编号 | 当前实际后端 | 回退方式 | 状态 |
|---|---|---|---|---|---|---|---|
| G-35 | `shared/gpu/gl_backend.py:97/150`；`gl_context_hub.py`；`framebuffer_scope.py`；`gl_pass_timer.py` | 兼容后端 | 集中到兼容会话 | 7.5 | OpenGL | — | 兼容保留 |
| G-36 | `shared/gpu/present_frame.py` | 协议 | 新增原生发布分支，保留 GL 构造 | 1.1 | GL 纹理名 + GL fence，另有带租约键的原生发布分支（1.1） | 旧消费者继续收 GL 描述符 | 兼容保留 |
| G-37 | `shared/media/windows_hw/*`（D3D11 / CUDA 桥） | 跨平台 | 独立 OpenGL 路径，不在本次范围 | — | D3D11 / GL | — | 非本次范围 |

### 非图形命中（不进入销账）

`features/ai_workflow` 23 个文件、`features/ai_harness` 4 个文件，以及 `shared/node_canvas`、
`shared/io/file_io.py`、`shared/project/color_graph.py`、`shared/control_surfaces/tangent/hub_client.py`、
`app/services|integrations/.../edit_benchmark_service.py`、`app/ui/preferences_dialog.py`、
`features/files/benchmark/benchmark.py`、`plugins/warm_tint.tglsl`：均为「不使用 GL」的声明或注释。

## 可证明实际后端的日志与计数器

| 证据 | 位置 | 用途 |
|---|---|---|
| `delivery_path` / `full_gpu` / 分阶段 `backend` | 导出阶段遥测（`stage_telemetry`） | 导出实际路径；`metal_h264_nv12_lut` 表示原生桥 |
| `ExportGpuTrail` + `bridge_selection` | `src/shared/export/gpu_trail.py`；`timeline_export.py:2815`、`:2827` | 每段后端、回读/上传字节、回退原因 |
| `"Timeline export graphics backend: %s"` | `timeline_export.py:2806` | 导出后端选择日志 |
| `gpu_renderer.metal_preview` / `metal_preview_fallback` / `vt_import_failed` | `edit_gpu_preview_renderer.py:2668-2674`（`TOPOS_EDIT_PLAYBACK_TRACE`） | 预览 Metal 快路命中与回退 |
| `EditPlaybackFrameTrace` | `src/shared/video/playback_frame_trace.py` | 每帧 `decode_path` / `render_path` / `fallback_reason` |
| `pixel_copy_counters` | `src/shared/video/pixel_copy_counters.py` | CPU 上传 / GPU 回读字节，「零拷贝」只能由计数证明 |
| `frame_presented` | `program_viewer.py:1386`；`engine.py:895` | 实际呈现帧数 |
| `PerformanceMonitor._decode_path_counts` | `src/shared/media/performance_monitor.py:636` | 解码路径分布 |
| `MetalComputeContext.get_stats` | `src/shared/gpu/metal/context.py:670` | Metal GPU 实测时间与内核缓存 |
