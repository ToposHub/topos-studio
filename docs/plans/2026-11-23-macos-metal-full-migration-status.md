# macOS Metal 全链路迁移 · 实施记录

对应 [实施计划](2026-11-23-macos-metal-full-migration.md)，代码基线 `6249e136`。每完成一个编号在此
登记：提交、运行命令、测试报告、实际后端、差异与回退结果。证据与清单见
[docs/macos_metal_migration](../macos_metal_migration/README.md)。

本机为 **Intel 独显类**（i9-9900K + AMD Radeon RX 6900 XT，macOS 15.3.1，显示器 3840×2160 @ 60 Hz，
DPR = 1）。Apple Silicon 机器尚未采集，凡标「仅 Intel 独显」的验收项在另一类机器上仍为待办。

## 总览

| 阶段 | 状态 | 说明 |
|---|---|---|
| 0 环境和基线 | ✅ 0.1–0.4 完成；0.5 样本 A 完成，B–G 已登记规格与归属阶段 | Resolve 对照沿用同机 11-22 记录，本次未驱动正在运行的 Resolve |
| 1 共享 GPU 帧 | ✅ 1.1–1.3 完成 | 原生发布分支、租约键与带键 ack 已在生产协议上生效；帧事务自阶段 2 起承载原生预览帧 |
| 2 原生视口和剪辑 | 🟡 2.1–2.3、2.5（Source Viewer）代码与定向测试完成，默认关闭；2.4 的真机性能验收待做 | 需 `TOPOS_EDIT_METAL_VIEWPORT=1` 显式开启。本机已验证：像素逐位一致、真实图层呈现成功、真实应用冒烟正常。播放帧数、seek p95、GPU 时间、换屏与叠加交互对照**尚未测量**（测量时显示器已休眠） |
| 2.6 | ✅ 代码、对拍与定向测试完成，显式开启 | 多层、变换、裁切、混合、交叉溶解、`dip_to_color` 走 Metal；31 个场景对 GL 逐像素一致。片段效果、planar 源、comp 句柄、空间转场仍回退 GL。真实应用多层工程播放未测 |
| 4.1–4.3 | 🟡 实现与对拍完成；Metal 执行器尚无生产调用方 | 共享入口（计划步骤与需求键）已接进预览；纹理执行器已对拍但未启用。4.4–4.6 进行中 |
| 5.1–5.3 | 🟡 实现与对拍完成，默认关闭，无生产调用方 | `TOPOS_COMPOSITE_METAL=1`；Constant/Transform/Merge/Output/Crop/Premult/矩形遮罩已迁，其余节点整图回退。5.4–5.6 进行中 |
| 8.5 / 8.6 | 🟡 回退开关与启用台账代码完成 | `TOPOS_METAL_DISABLE`；能力矩阵与长时验收留到最后 |
| 3、6、7 | ⏳ 进行中 | 导出音频/时间戳、插件效果、辅助渲染 |

既有失败（与迁移无关，均在基线代码上复现或属本机环境）：
`tests/edit/test_ef01_aud_part1.py::TestAud15506::test_hotpath_dependencies_imported_at_module_level`；
`tests/media/test_topos_source.py::test_no_deblock_request_skips_output_filter` 与
`tests/native/test_topos_binding.py::test_codec_capabilities_are_truthful`（本机原生编解码库未广播
`NO_DEBLOCK`，`flags=159`，为本地构建产物陈旧）；在干净的 `ac6557a7` 检出上复现：
`tests/edit/test_gpu_io_surface_sync.py::TestIOSurfaceSyncBarrier::test_process_task_finishes_output_before_emit`
（检查 `_process_task` 源码里的 `glFinish`，该字符串早已随 fence 协议移走）与
`tests/edit/test_gpu_render_dispatch_gating.py::TestGlRendererSignatureContract::test_gl_entries_reject_yuv422p10_kwarg`。

## 阶段 0 环境、基线和覆盖清单

### 0.1 配环境（提交 `d0de0351`，0.1–0.5 同一提交）

- 命令：`sw_vers` / `uname -m` / `python -V` / `ffmpeg -version` / `system_profiler SPDisplaysDataType`，
  `PYTHONPATH=. .venv/bin/python tools/probe_metal_capability.py --output-json … --output-md …`。
  工具脚本依赖仓库根在 `sys.path` 上，非 editable 安装须加 `PYTHONPATH=.`。
- 结果：`import PyQt6, Metal, Quartz, VideoToolbox` 通过；Metal 设备 AMD Radeon RX 6900 XT
  （`hasUnifiedMemory=False`，纹理为 Managed 存储，`argumentBuffersTier=1`）；MSL 编译与 dispatch
  正确；NV12 / P010 / BGRA / 4:2:2 8-bit / v210 经 CVMetalTextureCache 零拷贝导入全部通过；
  PyObjC 的 CoreVideo 路由 `-6661` 不可用，沿用 ctypes。
- 版本：macOS 15.3.1 (24D70)、Python 3.12.13、PyQt6 6.11.0、PyAV 18.1.0、moderngl 5.12.0、
  FFmpeg 9.0.2、DaVinci Resolve 21.2.0。
- 产物：[environment](../macos_metal_migration/evidence/environment-intel-2026-11-23.md)、
  [capabilities](../macos_metal_migration/evidence/capabilities-intel-amd-2026-11-23.md)。

### 0.2 建清单（同一提交）

- 命令：`rg -n 'QOpenGLWidget|OpenGL[. ]|moderngl|[.]glo\b|GLsync' src apps plugins`。
- 结果：1623 条命中 / 189 个文件，其中 89 个直接绑定 GL API；按运行路径整理为 37 条（G-01～G-37），
  每条带入口、阶段、预计能力、计划编号、当前实际后端、回退方式与销账状态。
  见 [gl-inventory.md](../macos_metal_migration/gl-inventory.md)。
- 关键事实：生产中「选择器」有两套——声明式 `backend_selector` 未接线，实际由 `ConfigManager`
  与 `TOPOS_BACKEND_GRAPHICS` 决定，且导出端（`timeline_export.py:9890`）与预览端
  （`metal_timeline_preview.py:86`）各自解析；`auto` 在导出端落 `opengl`，在预览端为 `lut`。
  6 个 `QOpenGLWidget` 视口、约 20 处 GL 上下文创建点。

### 0.3 对照工程（同一提交）

- 命令：`python tools/bench_metal_ab_topos.py --ab-dir bench_out/metal-ab.2026-11-23 --build`。
- 结果：隔离目录内重建 `Topos_UHD25_AB.topos`，4 条时间线（`h264_Original`、`h264_LUT`、
  `dnxhr_Original`、`dnxhr_LUT`），媒体路径正确，均为 400 帧、3840×2160、25/1、16 秒、无音轨。
  LUT SHA-256 前 16 位 `ffba7587f1e154dc`。验收通过。
- Resolve 侧：`resolve_bench.py` 会 `LoadProject` 切换当前工程并添加渲染任务；测量时 Resolve
  正在运行且无法确认当前库是否为测试库，**未执行**。同机 11-22 记录（H.264 LUT 6.82–6.93 秒、
  DNxHR LUT 10.21 秒）作为参考。

### 0.4 采时间与质量（同一提交）

导出（显示器清醒，首跑 + 5 次暖跑，逐轮交替顺序，18/18 成功，成片核验全部合格）：

| 路径 | 首跑 | 暖跑中位数 | 暖跑 min–max | 实际交付路径 | 分阶段 p50（毫秒） |
|---|---:|---:|---|---|---|
| GL H.264 | 11.149 秒 | 10.843 秒 | 10.708–11.861 | 通用 | 解码 6.57（`provider_session`）· 渲染 23.09（`opengl`）· 编码 13.90（CPU `h264`） |
| Metal H.264 | 7.114 秒 | 7.124 秒 | 7.050–7.302 | `metal_h264_nv12_lut`，全 GPU | 解码 0.11（`videotoolbox`）· 渲染 1.94（`metal`）· 编码 6.91（`videotoolbox`） |
| GL DNxHR | 19.225 秒 | 19.276 秒 | 18.586–19.634 | 通用 | 解码 7.46（`ffmpeg_planar`）· 渲染 27.92（`opengl`）· 编码 43.25（CPU `dnxhr`） |

- 实际码率：GL H.264 29.78、Metal H.264 28.42（目标同为 50 Mbit/s VBR）、DNxHR 728.27 Mbit/s；
  色彩标签均为 tv / bt709 / bt709 / bt709。
- 与计划起点相比：Metal 持平（7.06 → 7.12 秒）；两条 GL 路径慢约 7%（10.16 → 10.84、
  17.7–17.9 → 19.28 秒）。GL 路径是 CPU 解码 + CPU 编码，对 CPU 争用敏感；Resolve 耗时比：
  Metal H.264 ≈ 1.04×，DNxHR ≈ 1.89×（目标 ≤ 1.5×）。
- **测量条件（会话结束时核实）**：整段会话期间机器有持续的背景负载，负载均值约 7–10
  （i9-9900K，16 线程）：`mediaanalysisd`（macOS 媒体分析）持续占用 1 核以上，`VTDecoderXPCService`
  峰值 3 核以上，另有 Resolve 在运行。以上数字因此偏保守；阶段 3.4 的 DNxHR 归因和纯 FFmpeg
  基线**没有在这种条件下采集**，留待机器安静时补测。
- **DNxHR 由 CPU 编码主导**：编码 43.25 ms/帧 × 400 ≈ 17.3 秒，与整段墙钟同量级。

播放（显示器清醒，三种配置 × 四条时间线 × 4 轮，48/48 有效；呈现帧中位数，分母 400）：

| 配置 | H.264 Original | H.264 LUT | DNxHR Original | DNxHR LUT |
|---|---:|---:|---:|---:|
| `gl` | 399 | 398 | 370 | 368.5 |
| `auto` | 400 | 391 | 373.5 | 370 |
| `metal` | 399.5 | 399 | 374.5 | 384.5 |

- 实际渲染后端（门控诊断逐帧统计，不依赖显示器状态）：

  | 配置 | H.264 Original | H.264 LUT | DNxHR |
  |---|---|---|---|
  | `gl` | OpenGL | OpenGL | OpenGL（planar 上传） |
  | `auto` | OpenGL | **Metal** | OpenGL（planar 上传） |
  | `metal` | **Metal** | **Metal** | OpenGL（planar 上传） |

  DNxHR 解码为 planar 帧，不满足 Metal 预览的 `frame_type == "vt_pixel_buffer"`，这是 Metal 预览
  在 DNxHR 上零命中的原因。计划目标「轻剪辑 ≥ 396 帧」：H.264 基本达到，DNxHR 为 368–384。
- 播放基线文件里的 `backend_counts` 一列**不可信**（早于下述 trace 修复），已在文件内注明。
- **测量前置条件**：显示器休眠后同一构建每例只呈现个位数到几十帧（实测 398 → 5 / 17 / 6，在
  基线代码上用 `git worktree` 对照复现，排除了代码回归）。采样器现在拒绝在休眠显示器上测量。
- 计划附录 A 的 `topos_bench.py playback --repeat 601`：`playback()` 里 `--repeat` 是**重复次数**
  而非标签，按字面执行每个配置约需 13 小时。`tools/bench_metal_ab_playback.py` 用 `--repeats`
  显式限定轮数。

为证明「实际后端」而修掉的两个既有遥测缺陷：

1. 帧 trace 的 `backend` 在入队时写死 `opengl`。现在 present 描述符到达时标注实际后端，
   `summary()` 新增 `backend_counts`（`playback_frame_trace.py`、`timeline_preview_renderer.py`）。
2. `TimelinePreviewRenderer._lookup_frame_trace_id` 只查历史映射，而 `present_frame_ready` 总是先于
   `frame_ready` 到达、此时在飞条目尚未入历史——`submitted` 标注在正常播放路径上一直静默落空
   （`present_ms` 因此从未算出）。现在先查在飞表。

测试：新增 `tests/video/test_frame_trace_backend_counts.py`（8 项）、
`tests/edit/test_frame_budget_wiring.py`（+2 项）；相关遥测套件 37 项通过。

待处理（记入阶段 2 开工条件）：

- 播放基线 `stale_frame_emitted` 不为零：DNxHR 约 1460 帧出现 1 次旧帧覆盖（`gl` 两行、`auto`
  一行各 1 次），计划目标为零。
- 缩略图服务对 OpenCV 打不开的媒体持续重试（一次会话 18,935 次），伴随时间线视图重绘风暴。
- 带正确 `backend_counts` 的三配置播放重测：需显示器清醒时执行一条命令（见 README）。

### 0.5 固化项目样本（同一提交）

样本 A（单 LUT 交付与播放对照）已建成并核验，可由 `tools/bench_metal_ab_topos.py --build` 重建。
样本 B–G 已登记内容、媒体、核验点与归属阶段，各自在首次改动其覆盖的渲染路径之前建成并采集
强制 OpenGL 基线。实测发现冻结媒体集里没有 HDR 素材（`4K_H265.mov` 是 10-bit BT.709 SDR），
样本 E 的 HDR 源需要合成。见 [samples.md](../macos_metal_migration/samples.md)。

## 阶段 1 共享 GPU 帧和生命周期

阶段 1 的三个编号在同一提交里完成。相关回归 550 项通过（`tests/shared/gpu/metal/`、present 协议、
架构门禁、Metal 预览、CVMetal 桥、VT 源、trace）；引用被改接口的 95 个测试文件 2180 通过，
3 个既有失败见文首。

### 1.1 原生发布分支与租约键

- `src/shared/gpu/present_frame.py`
  - `NativePresentDescriptor`：api、设备身份、原生句柄、像素格式、尺寸、可见区域、方向
    （`top_left` / `bottom_left`）、色彩四元组（range / matrix / primaries / transfer）。
    `validate_for_consumer()` 返回机器可读的消费方契约失败原因。
  - `PresentLeaseKey` =（资源池代次，槽号，递增提交号）。池代次默认取进程级单调计数，退役池与
    现行池永不重号；每次 `begin_writing` 提交号 +1。
  - `TextureLease.make_present_frame(native=...)`：原生分支要求槽位以非 GL 资源注册且 api 一致，
    描述符 `texture_id=0`、`fence_ns=0`、`extra["render_backend"]` 取原生 api。不传 `native` 时
    GL 分支的字段与构造参数完全不变，`native=None`。
  - `OutputSlotPool.register_slot(..., resource=...)` 注册原生槽位；`ack` / `discard` /
    `mark_in_viewer` 可带键，键不匹配即过期确认——计入 `stats()["stale_acks"]`、不改变状态。
    不带键的调用保持旧语义。
- **修掉的既有隐患**：`EditGpuPreviewRenderer.ack_present_slot` 原先按 `slot_id` 同时投给现行池和
  全部退役池。池因尺寸重建后，一个迟到的旧 ack 会确认新池里另一帧正在占用的同号槽，该槽随即
  可被复用而 Viewer 仍在采样——正是协议要防的「未确认先复用」。
- 设备或格式不匹配：`GpuPresentFrame.validate_for_consumer()` 给出
  `device_mismatch:frame=…|consumer=…`、`graphics_api_mismatch:…`、`resource_format_unsupported:…`。
  「按既有路径重取本帧并记录原因」的接线属于 2.2：原生帧目前还没有生产消费者，现有 GL 消费者
  不会收到原生描述符。
- 测试：`tests/edit/test_gpu_present_frame_native.py` 31 项——重复确认、过期确认（旧提交号 /
  退役池代次）、设备变化、GL 描述符形状不变、旧式鸭子类型识别、原生发布的前置校验。

### 1.2 有界提交、完成队列与帧事务

- `src/shared/gpu/metal/submission_scheduler.py`（线程契约重写）
  - 驱动完成回调**只把结果入队**；`release_callbacks` 与 `on_complete` 改由资源线程在 `poll()`
    执行。此前两者都跑在 Metal 的完成线程上。
  - `try_reserve()` / `submit_reserved()`：先预留再编码，编码前即知环是否已满。
  - `pending_timeouts()` / `pause()` / `resume_if_idle()`：完成超时可见，可暂停该设备的新提交，
    在途工作排空后才解除。
  - `wait_idle()` / `drain()` 返回 GPU 是否确已空闲；`False` 表示调用方不得销毁资源。
- `src/shared/gpu/metal/context.py`：`discard_pending()` / `has_pending_work()`——已录制未提交的
  命令整组丢弃，不再被下一帧的 `dispatch` 续写；`submit(raise_after_commit=)`。
- **修掉的既有缺陷**：`MetalComputeContext.submit()` 在 `commit()` **之后**仍可能抛异常（驱动同步
  报告错误）。旧调度器在异常路径丢弃预留，而完成回调终将到来并发现预留不在——释放回调永远不会
  执行。现在调度器以 `raise_after_commit=False` 提交，已提交的 buffer 一律经完成回调回收。
- `src/shared/gpu/metal/frame_transaction.py`（新增）：把「预留 → 编码 → 提交 → 完成回收」绑成一帧
  的事务。输入在该事务的命令缓冲完成时释放一次；输出还要等显示消费者 `note_display_done()`，
  两个条件都满足才释放一次；失败帧的输出立即释放。环满沿用 hold/drop（`begin()` 返回 `None`，
  此时什么都没编码）。提交失败或编码后取消时整组丢弃已录制命令。
- 设备身份：`CVMetalVideoBridge.device_identity` 与导入平面上的 `GpuPlaneResource.device_identity`、
  VT 源 `GpuDecodedFrame.device_identity` 此前都是空串，`validate_for_device()` 的设备比对实际被
  跳过。现在桥接层与 `import_pixel_buffer_frame_metal` 填充进程内稳定身份，格式与
  `MetalComputeContext.device_identity` 一致。
- **边界（阶段 2 更新）**：调度器与帧事务自阶段 2 起由 `NativePreviewProducer` 用于原生预览帧
  （会话显式开启时）；未开启时预览仍走每帧同步 `finish()`。退役与超时暂停目前只有注入故障的
  单元测试证据，没有连续拖动 / 反复开关窗口的真机证据。
- 测试：`test_submission_scheduler_owner_thread.py` 22 项、`test_context_discard_pending.py` 5 项、
  `test_frame_transaction.py` 19 项（含 300 帧乱序完成的压测，每个输入与输出恰好释放一次）、
  `test_cv_metal_bridge.py` +2 项（含真实设备导入）。既有 3 个断言旧契约的调度器测试按新契约更新。

### 1.3 代际、退役与确认路由

- `EditGpuPreviewRenderer.ack_present_slot(slot_id, lease_key=None)`：带键时按键里的池代次只投递
  给那一代池（现行或退役）；不带键保持旧的广播语义。
- `TimelinePreviewRenderer`：所有持有描述符的 ack 调用点（共 9 处）与 Viewer 面向的 `ack_cb`
  都带上租约键；渲染器早于租约键（只收 `slot_id`）时回退按槽号确认。
- 代际与退役沿用现有语义：`set_generation_id()` 单调校验与旧任务丢弃、`_drain_retired_output_pools`
  等全部槽位回收后再销毁（超时以 `ctx.finish()` 兜底）不变。完成超时暂停、保留上一帧、GPU 确认
  空闲后才允许销毁资源，落在帧事务池里（见 1.2）。
- **边界**：Metal 原生池的退役与「停止接收旧帧后收齐已发出工作的完成事件」要等阶段 2 把原生
  槽位接入渲染线程后才能做真机验证（连续拖动、反复开关窗口、换屏）。
- 测试：`tests/edit/test_present_lease_key_routing.py` 14 项——退役池的迟到 ack 不触碰新池同号槽、
  过期提交号被拒、未知池代次不改变任何状态、无键广播语义保留、Viewer 回调绑定键、旧式渲染器回退。

### 计划片段的实测

计划 1.2 给出的 `GpuFrameRelease` 片段（两个消费者都结束后只回收一次）在现有实现上原样通过，
未作改动。

## 阶段 2 原生 Metal 视口和剪辑预览

**默认关闭。** 只有会话设置 `TOPOS_EDIT_METAL_VIEWPORT=1` 且 `metal_preview_mode()` 不是 `off` 时，
合格的帧才进原生视口。未开启、平台不是 Cocoa、图层挂载失败、呈现停滞，任何一种情况下 GL Viewer
照常呈现每一帧。计划的发布门（阶段 8）之前不改变默认行为。

| 编号 | 提交 | 内容 |
|---|---|---|
| 2.1 | `64e4ae17` | 呈现端口与着色器（`present_port.py`） |
| 2.1 | `106b7373` | GUI 宿主控件（`metal_viewport_host.py`） |
| 2.2 | `bfc498e7` | 原生帧生产器（`native_metal_preview.py`）与渲染器接线 |
| 2.2 / 2.3 | `97ca6c0a` | Viewer 路由、叠加层图像、`file_controller` 接入 |
| — | `20bf448a` | 帧_trace 的呈现面计数 `present_path_counts` |
| — | `5292d843` | 真机冒烟发现的缺陷修复（见下） |
| 2.5 | `33df4283` | Source Viewer 原生视口（渲染器侧无需改动） |
| 2.6 | 见下 | 剪辑合成：多层、变换、裁切、混合、交叉溶解、dip（`metal_edit_compositor.py`） |

### 2.1 原生视口适配器

- **呈现端口** `MetalPresentPort`（资源线程）：至多 2 个在途 drawable、至多 2 帧等待；最新提交的帧胜出，
  队满时挤掉最旧的等待帧并由资源线程归还其租约。帧只有在仍匹配请求（时间线 generation、拖动令牌、
  渲染尺寸、截止时间）且图层可用时才上屏；否则归还租约、保留上一帧。拖动令牌与截止时间随帧携带，
  后一帧不能放宽前一帧的约束。`nextDrawable` 不阻塞（`allowsNextDrawableTimeout = False`）。
- **呈现着色器**：全屏三角形把帧纹理采样进 drawable，画面按显示矩形等比缩放，黑边保持背景色，
  左上原点纹理做行翻转。
- **租约释放规则**：一帧的呈现命令缓冲完成、且已被更新的帧替换、且没有重绘还在读它的纹理时，
  恰好释放一次。缩放 / 平移 / 改尺寸用保留的上一帧重绘，不占新槽位。
- **GUI 宿主** `MetalViewportHost`：GUI 线程创建、定位、移除 `NSView` + `CAMetalLayer`，并把尺寸、
  缩放、可见性与尺寸代次发布给渲染线程。两个附加视图的 `hitTest:` 返回 nil，鼠标事件仍到达下层的
  Qt 视图——字幕拖拽、变换手柄、缩放平移的既有代码没有改动。非 Cocoa 平台一律拒绝（`winId()`
  只在 Cocoa 下是 NSView 指针）。
- **停滞检测**：已提交的呈现超过 1 秒没有完成（显示器休眠或被遮挡）时端口进入停滞状态。那些帧
  的纹理可能仍被显示系统读取，因此保留；渲染线程报告一次原因，让新帧走 GL 路径，完成事件回来后
  原生路径自行恢复。

### 2.2 播放引擎接线

- `NativePreviewProducer`（渲染线程）：把预览内核直接写进原生 present 槽位纹理，经阶段 1 的帧事务提交，
  GPU 完成后在 `poll()` 里把槽位置为 READY 并发布原生 `GpuPresentFrame`。VT 源帧与 CVMetal 导入是
  事务的输入，完成时各释放一次。任何回退都把帧原样留给 GL 路径。
- `EditGpuPreviewRenderer`：GUI 线程请求 attach / detach 并转交帧；渲染线程在自己的线程上建端口与
  生产器，每次循环轮询（有帧在 GPU 上时 2 ms 内唤醒），经 `present_frame_ready` 加既有的 `frame_ready`
  发布，后者继续驱动预览渲染器的状态机。代际变化即时到达端口。
- `TimelinePreviewRenderer` 的守卫不变：generation、单调帧、拖动终帧的校验仍在帧到达 Viewer 之前；
  端口在呈现前再比对一次。
- 音频主时钟与丢帧策略没有改动，仍在 `EditPlaybackController.tick()`。

### 2.3 叠加层

- `ProgramViewer._paint_overlays()` 是唯一的叠加绘制序列，`paintGL` 与原生叠加层图像共用。缩放、
  平移、字幕拖拽命中与手柄的坐标映射沿用 Viewer 的显示矩形。
- 原生模式下叠加层画进一张透明 `QImage`，显示在视频图层上方的独立视图里。Viewer 的 `update()`
  被调用时（叠加状态变化都经由它）按事件循环合并刷新一次。
- 叠加层与视频的像素和交互对照**尚未做**（需要显示器清醒时的人工或截屏比对）。

### 2.5 Source Viewer

- Source Viewer 用的是第二个 `EditGpuPreviewRenderer`，渲染器侧的原生帧生产、端口与信号原样可用；
  这次只加了窗口侧，开关与节目监视器同一个（`TOPOS_EDIT_METAL_VIEWPORT=1`）。
- 帧协议不同于节目监视器：Source 走元组协议，描述符在控制器里经 `SourcePresentGate` 配对后只把
  `slot_id` 传给控件。原生描述符**不进这个门**——门的 ack 不带租约键，会把确认打到 GL 池的同号槽上。
  原生描述符走控制器里的单槽 latest-wins，被顶掉、配不上对、过期、图层不在屏幕上，每种情况都交回
  渲染线程归还。
- Source 画布上有一个 FPS 读数子控件，会被图层盖住；改为画进图层的叠加层图像，由渲染线程的
  `native_frame_presented` 驱动计数。
- **范围边界**：Trim 双画面与多机位网格走各自的 CPU 解码，既不用这个控件也不用 GPU 渲染器，不在本次
  覆盖内。Source 的「反复切源、拖动、源与时间线切换、双 Viewer 并行」真机对照**未做**。
- 测试：`tests/edit/test_source_viewer_native_viewport.py` 18 项；相关回归 64 个文件无新增失败。

### 2.6 剪辑合成

- **计划是纯数据**：`build_metal_composite_plan()`（`metal_edit_compositor.py`）逐帧判定。一帧合格的条件是
  每一层都是 SDR NV12 VideoToolbox 帧或纯色，可带仿射变换（位移、缩放、旋转、自定义轴心）、裁切遮罩、
  不透明度、16 种混合模式和单个串联 LUT；层间按 `_composite_tracks` 自下而上混合——交叉溶解本来就是
  两层带改写不透明度的普通层；合成后可接 `dip_to_color`。片段效果、其他调色、调整剪辑、planar / packed
  源、comp 句柄和空间转场都会让**整帧**留在 GL，不存在部分 Metal 的帧。字幕是 Qt 叠加层，嵌套序列不进
  GPU 任务，两者无需处理。
- **逐遍复刻 GL**：`MetalEditCompositor` 按 GL 的顺序编码（轨道纹理带变换 → 裁切遮罩 → LUT → 混合 →
  dip），中间纹理都是 8 位；`inverse_quad_map` 是 GL 顶点四边形变形的逆矩阵，变换坐标用序列画布尺寸。
  原生槽位与 IOSurface 路线共用同一组 kernel。
- **两条路线都接了**：IOSurface → GL Viewer 用 `render_composite`；原生视口用 `try_composite`，所有层的
  VT 帧都是帧事务的输入，GPU 完成时各释放一次。导入失败时帧原样留给 GL，半编码的 command buffer 被丢弃。
- **只有显式开启才生效**：`all` 模式（`TOPOS_BACKEND_GRAPHICS=metal` 或 `TOPOS_EDIT_METAL_PREVIEW=1`）。
  自动默认的 `lut` 模式行为不变。编码异常使整个会话回到 GL，计划被拒则只影响那一帧。
- **顺带修正资格判定的边角**：只有羽化的裁切此前被旧判定当成恒等（`crop_cpu.crop_is_identity` 不看
  羽化），而 GL 会执行这一遍；现在单片段计划与合成计划用同一个判定 `gl_crop_params`。
- **对拍时发现并修复既有 CPU 缺陷**（单独提交）：`blend_modes.py` 的 ClipColor 漏了公式里的 `L +`，
  四种 HSL 混合模式在需要裁剪时 CPU 导出 / 回退与 GL 预览不一致。见
  `tests/edit/test_clip_blend_modes.py::TestHslModesWhenTheResultClips`。
- **未做**：片段效果、planar 源（DNxHR / ProRes）、comp 句柄、空间转场、调整剪辑仍回退 GL；真实应用里的
  多层工程播放与播放期性能**未测**（排到最后的长时验证）。

### 验证结果

| 项目 | 结果 | 方式 |
|---|---|---|
| 合成计划预检 | ✅ 86 项通过 | 纯数据：资格、拒绝原因、逆矩阵对 GL 矩阵的往返 |
| 混合 kernel 对 NumPy | ✅ 17 项通过 | 真实设备、相同 8 位输入：16 种模式与底层绘制均 ≤ 1 级 |
| 合成帧对 GL 逐像素 | ✅ 31 个场景 | 真实解码帧：平均误差 ≤ 0.59、p99 ≤ 2；见下 |
| 原生路线合成端到端 | ✅ 平均 0.035、最大 1 | 真实生产器发布后读回槽位纹理，两个 VT 帧各释放一次 |
| 渲染器接线与生命周期 | ✅ 70 项通过 | 假端口与假生产器：两条路线的接管、回退、丢弃、会话回退 |
| 真实应用多层工程 | ⏳ 未测 | 需要显示器清醒 |
| 呈现着色器像素 | ✅ 4 项通过 | 真实设备离屏渲染并读回：左上原点正立且不镜像、左下原点翻转、黑边背景、Alpha 不透明 |
| 端口逻辑 | ✅ 33 项通过 | 无需 Metal：匹配校验、截止时间、无 drawable、停滞、重复上屏、乱序完成 |
| 原生帧生产器 | ✅ 12 项通过 | 真实设备：GPU 完成后才发布、内核输出落在槽位纹理、输入各释放一次、ack 后复用 |
| 渲染器接线 | ✅ 30 项通过 | 假端口与假生产器 |
| GUI 宿主 | ✅ 12 项（Cocoa）/ 拒绝路径（offscreen） | 真实 Cocoa 窗口 |
| Viewer 路由与叠加层 | ✅ 26 项通过 | 替身渲染器 |
| Source Viewer 路由与配对 | ✅ 18 项通过 | 替身渲染器与替身门 |
| 默认关闭时的行为 | ✅ 与改动前一致 | 真实应用 `auto` / `metal` 配置：呈现面全部 `gl_viewer`，无原生视口对象 |
| 端到端像素对拍 | ✅ MAE 0、最大差 0 | 真实视频帧：原生槽位纹理与 IOSurface 路径逐位相同 |
| 真实图层呈现 | ✅ presented = 1 | 真实 `CAMetalLayer` 取到 drawable 并提交呈现（显示器清醒时） |
| 真实应用冒烟 | ✅ 正常 | 见下 |
| 相关回归 | ✅ 4862 通过 | 其余失败均逐项在基线代码上复现，见文首 |
| 播放帧数、seek p95、GPU 时间 | ⏳ 未测 | 测量时显示器已休眠，数值不可比 |
| 连续 resize、换屏、全屏、关闭 | ⏳ 未测 | 需要显示器清醒 |
| 叠加层像素与交互对照 | ⏳ 未测 | 同上 |
| Apple Silicon | ⏳ 未采集 | 无机器 |

真实应用冒烟（显示器休眠，**帧数不作比较**，`--allow-display-asleep`）：

| 时间线 | 实际渲染后端 | 实际呈现面 | 旧帧覆盖 | 槽位违规 |
|---|---|---|---:|---:|
| H.264 Original | metal 147 / opengl 3 | `native_metal` 147 | 0 | 0 |
| H.264 LUT | metal 80 | `native_metal` 80 | 0 | 0 |
| DNxHR Original | opengl 42 | `gl_viewer` 42 | 0 | 0 |
| DNxHR LUT | opengl 53 | `gl_viewer` 52 | 0 | 0 |

DNxHR 解码为 planar 帧，不满足 Metal 预览的资格，按设计留在 OpenGL。

2.6 的 31 个对拍场景（1920×1080，片段第 25 / 60 帧，两者是不同镜头）：两层与三层、交叉溶解、
位移缩放、旋转加自定义轴心、缩小叠在未变换的层上、代理尺寸下的变换、硬裁切、羽化裁切、仅羽化裁切、
裁切加不透明度加 LUT、两侧带 LUT、纯色垫底、纯色单独、`dip_to_color`、15 种非 normal 混合模式。
平均误差 0.00–0.59 个 8 位码值，p99 均为 1–2。离群像素只有三类，且都已定位到原因：

| 来源 | 数量（1080p） | 原因 |
|---|---:|---|
| 旋转后的片段边缘 | 2–5 | 像素中心离边界 1e-7～3e-6 个纹理坐标，光栅化与解析逆映射在浮点噪声下判定相反 |
| 纯色 0.1 / 0.3 / 0.7 / 0.9 | 全帧 1 级 | 这四个值乘 255 恰为半整数，GL 的 clear 与 Metal 的纹理存储舍入不同（各 1 个码值） |
| hue / saturation 混合 | 153 / 3 | 与「仅把 GL 和 Metal 的单层结果分别代入同一 NumPy 公式」得到的离群数完全相等，即全部由输入的 1 级舍入差解释 |

### 真机冒烟发现并修掉的缺陷（`5292d843`）

1. **被拒收的原生帧泄漏槽位。** 端口队满或图层无法上屏时，Viewer 退回帧——但原生帧的 Viewer 侧回调
   只做记录，槽位从未归还。4 帧之后原生池耗尽，后续帧全部静默回落到 IOSurface → GL 路径。现在
   未被接手的帧交给渲染线程归还（`release_native_frame`）。
2. **呈现停滞没有显式处理。** 现在有停滞检测与报告（见 2.1）。
3. **关闭时白等 2 秒。** 原生视口的收回原先排在关闭流程靠后，那时预览渲染线程已经退出。现在放在
   保存确认之后、第一个线程停止之前；线程已退出时立即确认。
4. **宿主创建期报错。** Qt 在控件创建过程中就会派发事件，而 `event()` 读到的字段尚未赋值。

### 复现命令

```bash
source .venv/bin/activate
export PYTHONPATH=.
# 单元与真机（第二、三个需要 Cocoa 平台）
.venv/bin/python -m pytest -q tests/shared/gpu/metal/ \
  tests/edit/test_native_metal_preview_producer.py \
  tests/edit/test_native_viewport_renderer_wiring.py \
  tests/edit/test_program_viewer_native_viewport.py
QT_QPA_PLATFORM=cocoa .venv/bin/python -m pytest -q \
  tests/edit/test_metal_viewport_host.py tests/edit/test_native_viewport_end_to_end.py
# 真实应用（显示器必须清醒，数值才可比）
.venv/bin/python tools/bench_metal_ab_playback.py \
  --ab-dir bench_out/metal-ab.2026-11-23 --configs gl,metal,native --repeats 4 \
  --output-json docs/macos_metal_migration/evidence/ab-playback-native.json \
  --output-md docs/macos_metal_migration/evidence/ab-playback-native.md
```

### 2.4 待办（需要显示器清醒）

- 三种配置（`gl` / `metal` / `native`）各 4 轮：显示帧数、掉帧、首帧与 seek p95、GPU 时间、
  峰值 GPU 资源、每帧搬运量。
- 连续 resize、隐藏、全屏、换屏、关闭：GUI 无等待，无旧帧覆盖，无悬挂槽。
- 叠加层：字幕拖拽命中、辅助线、变换手柄、错误状态的像素与交互对照。

## 阶段 4 调色图、Color Viewer 和示波器

| 编号 | 提交 | 内容 |
|---|---|---|
| 4.1 | `6cc7a7f4` | 共享入口 `color_graph_entry.py`：需求键、共享计划、Metal 优先 + 兼容回退 |
| 4.2 | `6cc7a7f4` | `ColorGraphMetalTextureExecutor`（`color_graph_texture_metal.py`）与 MSL（`metal_texture_kernels.py`） |
| 4.3 | `6cc7a7f4` | qualifier、窗口、组合遮罩、key 通道、关键帧 |

- **4.1 已接进生产的只有两处**：`async_renderer._execute_graph_frame` 的计划步骤改经
  `plan_color_graph_frame(backend="gl")`（同一计划与计划缓存，资源预检问题记为诊断，GL 执行器不变），
  以及 `image_engine` 随参下发的需求键。`SharedColorGraphEntry.render_frame`（Metal 纹理执行 + `run_plan_cpu`
  兼容回退）**没有任何生产调用方**；剪辑渲染与交付仍用既有的 CPU/GL 路径。
- **4.2 / 4.3 的执行器处于「已实现、已对拍、未启用」**：已接 Primary、RGB/HSL 曲线、LUT3D、Mixer、Warper、
  soft clip、混合收尾，以及 qualifier、窗口、组合遮罩、key 通道与同一帧时间的关键帧。时域类（降噪、时域
  分析、跟踪）、Normalize、fx/blur 显式拒绝（fail-closed），不会把失败节点的输出换成原图。
- **未做**：4.4 色彩管理与 HDR、4.5 Color Viewer / 示波器、4.6 静帧入口（进行中）；异步提交与完成回调；
  节点级结果缓存。

### 验证结果（4.1–4.3）

| 项目 | 结果 | 方式 |
|---|---|---|
| 计划 4A 参考实现 | ✅ 4 项通过 | 真实设备：线性增益 + 遮罩的像素期望值位级通过，含尺寸不整除线程组、乱序完成、透传旁路 |
| 执行器对 CPU 参考 | ✅ 29 项通过 | SDR ≤ 1/255、HDR ≤ 2e-3 相对；极值、NaN、顺序、池、取消、fail-closed |
| 遮罩与 key 通道 | ✅ 16 项通过 | qualifier、窗口、反向、羽化、key mixer 三模式、outside、parallel key 槽、关键帧 |
| 共享入口 | ✅ 17 项通过 | 需求键、同一计划、诊断回退、取消终态、旧工程样本 |
| 调色域回归 | ✅ 1348 通过、3 跳过 | 57 个文件（async_renderer、image_engine、color_graph、composition 等） |
| 生产路径上的效果 | ✅ 无行为变化 | 计划步骤等价；Metal 执行器无调用方 |
| 真实工程预览/导出对拍 | ⏳ 未测 | 需要执行器接入剪辑渲染与交付之后 |

### 对拍时发现的既有分歧（未改动，待裁决）

1. **HSL 曲线**：CPU 参考（`apply_hsl_curves_image`，HSV 域直接缩放）与 GL/MSL（`mix(gray)` + 亮度下限）
   是两套算法；Metal 纹理路径贴 GL，CPU 导出与 GL 预览在该域本就不一致。
2. **qualifier 色相域**：`keyer.glsl` 的色相轴有 `fract(h + 0.5)` 预移，对拍测试会换算 `hue_center`，但生产
   GL 宿主（`async_renderer.py` 等）直接传原值，GL 预览与 CPU 的 qualifier 色相选择疑似有既有点位偏差。
3. **qualifier 饱和度边界**：`s_high == 1.0` 且 `soft == 0` 时 CPU 与 GLSL 对 s == 1 的像素取舍不同。

## 阶段 5 二维合成、时间类效果和 3D

| 编号 | 提交 | 内容 |
|---|---|---|
| 5.1 | `94b2c3a2` | `src/features/composite/render/metal/`：预检 → 计划 → halo → 渲染 → 池与缓存 |
| 5.2（部分） | `94b2c3a2` | Rectangle、Crop、32 种混合模式、Premultiply / Unpremultiply |
| 5.3 | `94b2c3a2` | `input_roi`、halo 沿图反向传播、tile 裁回中心、缓存键、按字节计量的纹理池 |

- **默认关闭、无生产调用方**：入口 `CompositeGraphRenderService.render_metal()`（`TOPOS_COMPOSITE_METAL=1`），
  `render_cpu` / `render_gpu` 未改。输出的句柄是 Metal 纹理，GL 消费者（时间线 comp clip）读不了，接线留给 5.6。
- **已迁移**：Constant（四种生成器）、Transform（linear / nearest）、Merge（32 混合模式、mask 通道、alpha 策略）、
  Output、Crop（reformat 与越界策略）、Premultiply / Unpremultiply、矩形 Mask。其余节点在能力表里登记为
  未迁移，预检返回带节点 ID 与类型的显式不可用句柄，整图回退，不跳节点。
- **已知差异**（写在 `docs/architecture/composite.md`）：Transform 固定中间画布（GL 的 bbox 跟随会保留到
  Merge 画布）；每帧 `waitUntilCompleted` 后发布（同步）；GL `_MASK_FRAGMENT` 的 SDF 实测是「形状内 0、形状外 1」，
  与其注释相反，Metal 复刻了实测行为。
- **未做**：5.4 历史与多帧、5.5 3D / AOV、5.6 输出与资源池（进行中）；Keyer、Matte、模糊、形态学、Glow、
  Light Wrap、文本、PSD、媒体输入节点。

### 验证结果（5.1–5.3）

| 项目 | 结果 | 方式 |
|---|---|---|
| 计划阶段 5 命令 + 新套件 | ✅ 203 通过 | preflight、服务契约、cache、3D、Cryptomatte、deep、comp clip、`test_metal_composite_chain.py` |
| 计划参考实现 5A | ✅ 两个断言成立 | `input_roi` 原样实现 |
| 分块对完整帧 | ✅ 逐位相等 | 真机：17×19 与 64×64 分块对比完整帧，含亚像素变换与 merge |
| Alpha 语义例 | ✅ 精确命中 | 线性、预乘下 `(0.5,0,0,0.5)` 覆盖 `(0,0,1,1)` → `(0.5,0,0.5,1)` |
| 32 种混合模式 GPU↔CPU | ✅ ≤ 5e-3 | 中间纹理 RGBA16F（≈ 2⁻¹¹ 相对误差），实测 ≤ 0.0019 |
| 默认关闭 | ✅ `render_metal` 返回 None | 未设置开关时不创建 Metal 渲染器 |
| 时间线 comp clip / 导出接线 | ⏳ 未接 | 句柄是 Metal 纹理，GL 消费者读不了 |

## 阶段 8 回退开关（8.5 / 8.6 的代码部分）

| 编号 | 提交 | 内容 |
|---|---|---|
| 8.5 / 8.6 | `41bc0a19` | `feature_switches.py`：`TOPOS_METAL_DISABLE` 与启用台账 `ROLLOUT` |

- 每项 Metal 能力可以单独关掉，且优先于它自己的开启方式；原因写作 `disabled_by_switch:<能力>`，可与设备、
  格式、shader、drawable 失败区分。台账记录每项默认开/关、开启方式与转为默认的兼容条件——目前只有 Metal →
  VideoToolbox 导出快路径默认开启。
- 已接：剪辑预览模式、剪辑合成计划、原生视口开启判定、合成图链。导出开关待 3.1 / 3.2 落地后接。
- 测试：`tests/shared/gpu/metal/test_feature_switches.py` 为每个能力保留一条「设置开关 → 能力拒绝」的回退测试。
- **未做**：8.1 能力矩阵、8.2 画质与工程兼容、8.3 性能与稳定性、8.4 安装包，以及把任何一项转为默认开启。
