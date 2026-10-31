# A0.3 · Mac 线帧预算拷贝台账报告（2026-10-22）

> 依据：[计划表 A v3](perf_plan_A_mac_2026-10-22.md) A0.3。本报告为**源码证据型**结论（file:line 可复核），
> 未含任何运行时测量；实测证伪/证实由 A0.2 基线与 A0.1 harness 输出补齐。
> 分支：`perf/a-playback`；基线提交：a353c857a（fix-ui 同步）。
> 环境：i9-9900K / RX 6900 XT 16GB / macOS 15.3.1 (24D70) / Python 3.12.13 / PyAV 17.0.1 (libavcodec 62.11.100) / ModernGL 5.12.0。

## 1. 结论摘要

1. **VT 零拷贝路径（h264/h265/hevc/prores）CPU 像素拷贝 = 0**：CVPixelBuffer 仅 Retain/Release，GL 线程 CGLTexImageIOSurface2D 直接绑定 IOSurface；P010 的 10bit MSB 对齐由 GL_R16 归一化采样吸收，无 CPU 移位。
2. **拷贝热点按路径分档**（每帧 4K 量级）：
   - VT planar 回退路径（零拷贝源未建成时）：2 次整平面 tight 拷贝（8bit ≈12.3MB，10bit ≈24.6MB）+ 10bit 时每平面一次全平面右移；
   - FFmpeg planar（ProRes/DNxHR 主路径）：每平面 1 次 tight 拷贝（ascontiguousarray，输入必跨步）；
   - FFmpeg 半平面（nv12/p010）：每帧 3 次拷贝（UV 提取 1 + U/V 去交织 2）；
   - float16 上传回退（R16UI self-test 失败时）：每平面 astype+divide 两次全平面值转换 + 临时分配；
   - 监看开启：每帧 PBO 回读 w×h×4（4K≈33MB）+ flipud/ascontiguousarray/QImage.copy/submit_qimage.copy/convertToFormat/frombuffer.copy/cv2.resize/tobytes ≈ 7 次 CPU 拷贝链；
   - 普通预览呈现：纯 GPU blit，无 CPU 回读（播放热路径）。
3. **两个"账面组件"未接线**：`GpuFramePool.ensure_gpu_frame()` 在 `src/` 无生产调用点（池恒空）；`AVSyncController` 的 HOLD/DROP/REBUFFER 决策仅被 `edit_page._evaluate_avsync_drift` 读入 telemetry，不反馈呈现路径。
4. **DeckLink GPU 直送为占位**：`topos_decklink.cpp` 的 `submit_gpu_texture` 恒返回 False、`supports_gpu_texture` 恒 False；PBO 回读是唯一活的监看路径。测试中的 GPU 提交验证基于 monkeypatch 假 bridge，非运行证据。
5. **呈现 ACK 语义 = "blit 命令已入队"**（PV `_copy_shared_texture_to_display` finally 块），不代表可观测显示；可观测信号目前只有 `ProgramViewer.frame_presented`（paint 事件后），未接入 trace。

## 2. 解码 → 上传链台账

符号：W×H 帧分辨率，bps=bytes/sample（8bit=1，>8bit=2）。全部路径前缀省略 `src/`。

### 2.0 调度层（零拷贝，仅引用传递）

| # | 位置 | 操作 | 字节 | 条件/频次 | 真复制 |
|---|---|---|---|---|---|
| 0.1 | features/edit/core/realtime_playback/timeline_decode_scheduler.py:151-256 | configure 为每可见层建 ClipDecodeStream | 0 | 时间线配置 | 否 |
| 0.2 | realtime_playback/decode_manager_source.py:28,74 | `_GPU_FRAME_TYPES` 白名单外的 `cpu` 帧按 miss 丢弃 | 0（丢帧） | packed 回退帧 | — |
| 0.3 | realtime_playback/clip_decode_stream.py:282-304,404-512 | `_decode`/`_build_handle` 仅类型识别，payload 直引用 | 0 | 每帧 | 否 |
| 0.4 | realtime_playback/frame_ring_buffer.py:67-116 | OrderedDict 存引用；淘汰走 safe_release | 0 | 每帧 | 否 |

### 2.1 解码路由（edit_gpu_decode_manager.py）

- `_playback_vt_policy`（:1771-1832）：非 darwin / 非 H264/H265 / ≤8bit → 'auto'（≤8bit 直接跳过探测）；>8bit 且 probe 判 VT 无硬解 → 'prefer_ffmpeg'；`vt_hardware_decode_available==True` → 'hw_or_ffmpeg'。结果一次性 trace `decode_manager.playback_route`（:1834-1847，reason='vt_software_only'/'hw_session_strict'）。
- `_should_preserve_native_planar_size`（:1745-1761）：4K ProRes/DNxHR + 目标≥1080p + ≥2× 缩比 → 保持原生尺寸 planar（避免 CPU swscale 逐帧缩放），缩放交给 GPU 采样。
- VT 零拷贝源三层门禁（shared/media/macos_vt/source.py:175-289 + capability.py:448-538）：capability 全局可用（PyObjC/VT/bridge 实装 + 未被 `TOPOS_VIDEO_FORCE_CPU_UPLOAD`/用户偏好关闭 + probe 证明）→ codec ∈ {h264,hevc,h265,prores} → VT session 创建成功。逐帧 IOSurface→GL 导入失败时**不做当帧 CPU 回退**，该帧 release + trace `gpu_renderer.vt_import_failed`（renderer :1784-1793，含 bridge reason 枚举），该轨 skip；回退只发生在 source 创建层（零拷贝 → VT planar → FFmpeg planar → packed）。

### 2.2 VT planar 下载源（真拷贝 #1）

| # | 位置 | 操作 | 读/写字节 | 条件/频次 |
|---|---|---|---|---|
| 2.2.1 | shared/media/macos_vt/source.py:1292-1303 `copy_plane` | `CVPixelBufferLockBaseAddress` → ctypes 借用视图 → `[:, :visible_row_bytes].copy()` tight 拷贝（去 CoreVideo 行填充） | Y: W×H×bps；UV: W×(H/2)×bps。4K8bit≈12.3MB、10bit≈24.6MB（读+写各一遍） | `vt_policy=='auto'` 且 codec∈{h264,h265,hevc} 且零拷贝源未建成（edit_gpu_decode_manager.py:2799-2826）；每帧 2 次 |
| 2.2.2 | source.py:1309-1311 | 10bit：`np.right_shift(plane, 6, out=plane)` 每平面一次（P010 MSB→0..1023） | 原地读改全平面 | `bit_depth==10`；每帧 2 次 |

### 2.3 FFmpeg planar 源（真拷贝 #2）

| # | 位置 | 操作 | 字节 | 条件/频次 |
|---|---|---|---|---|
| 2.3.1 | shared/media/ffmpeg_source.py:2824-2878 | `np.frombuffer(av_plane)` 零拷视图 → 切片（view）→ `np.ascontiguousarray`（跨步必拷） | 每平面 plane_w×plane_h×itemsize；420 10bit ≈ 3WH×2 样本字节 | 所有 FFmpeg planar 解码；每帧每平面 1 次（3~4 次） |
| 2.3.2 | ffmpeg_source.py:2737-2822 | 半平面 nv12/p010：UV 整块提取拷贝 1 次 + U/V 奇偶列去交织各 1 次紧拷 | 共 3 次拷贝/帧 | FFmpeg 输出 nv12/nv21/p010le |
| 2.3.3 | ffmpeg_source.py:2364-2377 | `original_downscaled` 路由 `frame.reformat(w,h,fmt)`（swscale 写新缓冲） | 输出平面总字节 | 目标<源 且不满足 native-planar 条件 |
| 2.3.4 | ffmpeg_source.py:1911-2017 | packed 回退 `to_ndarray()` 整帧拷贝 | W×H×3/×2 | 非 planar codec；注意实时管线把该类帧按 miss 丢弃（0.2），不进播放热路径 |

### 2.4 GPU 上传（shared/video/yuv_gpu_upload.py + edit_gpu_preview_renderer.py）

| # | 位置 | 操作 | 字节 | 条件/频次 | 真复制 |
|---|---|---|---|---|---|
| 2.4.1 | yuv_gpu_upload.py:838-869 | `_auto_enable_r16ui`：>8bit 首次自动 self-test，失败记 `_r16ui_disabled_reason` 回退 float16 | 测试纹理 | 每实例仅一次（`_r16ui_auto_tested` 标志；uploader 实例生命周期决定是否重复） | — |
| 2.4.2 | yuv_gpu_upload.py:875-899 | `texture.write(contiguous)` 走 buffer protocol（无 .tobytes）；ascontiguousarray 连续时 no-op | DMA nbytes | 所有直传路径；每平面 | 否（DMA 计入 CPU_UPLOAD/TEXTURE_WRITE 审计 :2905-2914） |
| 2.4.3 | yuv_gpu_upload.py:514-620 | `PBOUploadPool.upload_to_texture`：`pbo.write(data)` CPU→PBO 一次真拷 + GPU DMA | 额外 CPU 写 nbytes（256B 对齐） | 仅源像素≥2×输出像素的 4K planar 层（renderer `_configure_planar_upload_mode` :2418-2463） | **是** |
| 2.4.4 | yuv_gpu_upload.py:1460-1475 | float16 回退 `_normalize_plane`：staging ring 复用缓冲 + `astype(f16)`（临时分配）+ `np.divide(out=buf)` | 每平面读 W×H×2 写 W×H×2 + 同量 temp | R16UI 不可用/自检失败；每帧每平面 | **是**（两次全平面遍历） |
| 2.4.5 | renderer :2884-2914 | planar 上传字节进 stats_out + `pixel_copy_counters`（CPU_UPLOAD/TEXTURE_WRITE）；零拷贝 IOSurface 路径天然计 0 | = 平面总字节 | 每 planar 轨每帧 | 审计点 |

### 2.5 零拷贝收尾（GL 线程）

- renderer :1754-1827 `_import_vt_pixel_buffer_frame`：`bridge.bind_pixel_buffer`（CGLTexImageIOSurface2D，iosurface_gl_bridge.py:659-996，fallback_reason 全枚举）；成功 → `GpuDecodedFrame(zero_copy=True)`。
- P010 零拷贝**无 >>6**：GL_R16/GL_RG16 归一化采样（÷65535）吸收 MSB 对齐（bridge :584-587）；NV12 用 GL_R8/GL_RG8。
- 采样后 glFenceSync 延迟释放 IOSurface/CVPixelBuffer（renderer :5166-5247）。

### 2.6 10bit 移位/重排矩阵（汇总）

| 路径 | 移位/重排 | 证据 |
|---|---|---|
| VT 零拷贝 GL | 无移位（归一化吸收） | iosurface_gl_bridge.py:584-587 |
| VT planar | CPU 每帧每平面 `>>6` | macos_vt/source.py:1309-1311 |
| FFmpeg yuv422p10le/444p10le | 无（LSB 对齐，R16UI 直传，shader ÷1023） | yuv_gpu_upload.py:699,2163 |
| FFmpeg nv12/p010 | 无移位；U/V 去交织重排 2 次紧拷 | ffmpeg_source.py:2795-2807 |
| Windows NVDEC | 无移位（16bit 契约 ÷65535） | windows_hw/hw_planar_source.py:51-60 |

## 3. 合成 → 呈现/监看链台账

### 3.1 GPU 合成（零回读）

- 输出槽池：24 slot RGBA8 纹理（edit_gpu_preview_renderer.py:3712-3741），满时 trace `present_pool_full` + hold/drop，不静默复用；退役池延迟释放（:3796-3824）。
- 每帧 glFenceSync+glFlush（:5022-5024）；`timeline_gpu_compositor.py:153-154,437-456` 断言合成内部 `readback_count/cpu_qimage_count==0`。
- **唯一渲染侧回读**：受控 1/4 分辨率快照（:4890-4935，RGB8 ≈1.5MB/帧），仅 ① idle 后台预缓冲 ② 暂停态 seek/scrub settle（`_arm_snapshot_for_next_gpu_frame`）触发；播放热路径零成本。含 read/翻转 tobytes/QImage.copy 共 3 次 CPU 拷贝，计 `CPU_DOWNLOAD` 审计（:4922-4929），消费方为 seek 快速路径 CPU LRU（timeline_preview_renderer.py:11191-11210），不进 Viewer 显示。

### 3.2 Viewer 呈现（GPU blit，无 CPU）

- paintGL：glWaitSync(fence) → glBlitFramebuffer（present slot 纹理 → viewer 自有纹理，GPU 显存 w×h×4）→ shader 采样上屏（program_viewer.py:1827-1924, 1398-1420）。
- CPU 回退：`_set_frame_from_qimage` → `_rgb888_qimage_to_rgba`（预分配+一次跨距拷贝）→ glTexImage2D（PV:97-137, 1103-1130），仅 CPU 合成回退/拖拽路径。

### 3.3 监看输出（真拷贝 #3，最重的 CPU 链）

开关：file_controller.py:3841-3913（native 且 supports_gpu_texture 才走 GPU 信号；随仓库 bridge 恒 False ⇒ PBO 回读是唯一活路径）。

| # | 位置 | 操作 | 字节 | 频次 |
|---|---|---|---|---|
| 3.3.1 | PV:2063-2079 | glReadPixels 异步入 PBO（双缓冲环，1 帧延迟流水线） | w×h×4（4K≈33MB）DMA | 监看开时每帧 |
| 3.3.2 | PV:2034-2036 | glGetBufferSubData 取上一帧 PBO → bytes | w×h×4 | 每帧 |
| 3.3.3 | PV:2151-2173 | `np.frombuffer` → flipud+ascontiguousarray（真拷，GL 朝向）→ 丢 alpha → `QImage(Format_RGB888).copy()`（深拷取得所有权） | w×h×3（≈24.9MB），2-3 次 CPU 拷贝 | 每帧 |
| 3.3.4 | PV:2102-2149 | PBO 保险：同步 glReadPixels 直读兜底，40ms 节流 | w×h×4 | ≤25fps 兜底 |
| 3.3.5 | shared/video/monitor_output.py:757-760 | `submit_qimage` → `image.copy()` | 整帧 | 每次提交 |
| 3.3.6 | monitor_output.py:233-281 | convertToFormat(RGB888)（拷1）→ frombuffer+.copy()（拷2）→ cv2.resize（拷3）→ canvas 填充+tobytes()（拷4）→ BGRA 固定尺寸 | config.w×h×4 | 每帧（submitter 线程） |
| 3.3.7 | monitor_output.py:908-921 / topos_decklink.cpp:332,342,460 | ffmpeg 管道写 / C++ memcpy 进调度缓冲 | BGRA | 每帧 |
| 背压 | monitor_output.py:1117-1168 + PV:2112-2113 + present 槽池未 ack 不复用 | 四层：槽池 / 40ms 节流 / 有界队列丢最旧 / submitter 只留最新 | — | — |

Color/Composite 页另有 `_submit_video_controller_monitor_frame`（file_controller.py:2786-2890）：直接提交解码 CPU payload，无 GL 回读，节流 0.75×fps。

### 3.4 呈现状态机（现状）

状态环 `FREE→WRITING→READY→IN_VIEWER→RELEASABLE→FREE`（shared/gpu/present_frame.py:89-97）：
- submitted：渲染线程 composite 完成+fence 就绪后 `present_frame_ready.emit`（renderer:5076）。
- superseded：新帧到达时对未 paint 的 pending 帧 `_ack_pending_present_without_display`（PV:1674-1787-1797，5 个入口全覆盖）——**直接 ack 回收，不显示，目前无事件/计数**。
- ACK 时机：`_copy_shared_texture_to_display` blit 命令入队后的 finally（PV:1925-1931，AUD-20403 恰好一次）——语义是"命令已提交"，非可观测显示。
- 可观测呈现：`ProgramViewer.frame_presented`（paint 内 `_render_texture` 后 emit）——存在但未接 trace/telemetry。
- slot_released：ack → `slot_acked`（PF:516-526 → renderer:5081-5096，含退役池转发）。
- 引擎侧：`engine.request_present` 返回 True = "已提交"非"已显示"（engine.py:976-981 注释自述）。

### 3.5 GPU query 计时现状（A0.1 要修的点）

- GlPassTimer：GL_TIME_ELAPSED query 环（RING_DEPTH=3，读回延迟 2 帧，池化复用，unsupported 时 fail-closed 返回 None 而非 0）（shared/gpu/gl_pass_timer.py:50-149）。
- **缺陷**：query 结果不绑定发起帧身份；`_finalize_present_frame`（renderer:4973-4986）把约 2 帧前的 `last_ms("composite")` 写进当前帧 `GpuPresentFrame.extra["gpu_submit_ms"]`——计划 §2.1 点名的"旧 query 写进当前帧"。无迟到结果丢弃计数。

## 4. 帧对象生命周期

- FFmpeg：`_extract_visible_plane_data` 立即拷出 tight ndarray，DecodedFrame 拥有独立内存，AVFrame 随迭代置换 GC；不跨线程共享可变引用（decode worker 内完成，`_stream_lock` 串行）。
- VT：CVPixelBuffer 输出队列 Retain，`VtPixelBufferFrame.release` exactly-once → CVPixelBufferRelease（vt_session.py:495-511）；reorder 滞留帧 `_prune_reorder_buffer` 释放（每滞留帧 ~12MB 4K IOSurface，source.py:390-415）。
- GPU 帧：`GpuDecodedFrame.planes` 不可变 tuple；fence 完成才延迟释放 IOSurface（renderer:5166-5247）；陈旧任务 `_release_task_sources`（:3658-3696）。

## 5. 证据强度分级

**强（代码字面即拷贝/转换）**：§2.2.1 `.copy()`、§2.2.2 `right_shift`、§2.3.1/2.3.2 `ascontiguousarray`（输入必跨步）、§2.4.4 `astype+divide`、§2.4.3 `pbo.write`、§3.3.3 QImage.copy、§3.3.6 四连拷、DeckLink 占位（已人工复核源码）。
**中（API 语义可直接推出）**：texture.write buffer protocol 无 tobytes、reshape/列切片为 view、CGLTexImageIOSurface2D 零拷贝、调度层全链引用传递。
**弱/推测（待 A0.2 实测）**：各路径**实际触发占比**（零拷贝 vs VT planar vs FFmpeg 在本机的分布）、swscale 内部缓冲、VT 池回收行为、`_transfer_hw_frame_if_needed` 触发率、PBO 兜底直读频率。
**非运行证据（已排除）**：`tests/shared/test_monitor_output.py` 的 GPU 提交（monkeypatch 假 bridge）；`tests/edit/test_gpu_compositor_real_attach.py` 对 `ensure_gpu_frame` 的直调。

## 6. 可证伪假设（A0.2 用同一 harness 证实/证伪）

| # | 假设 | 预期量级（4K） | 证伪条件 |
|---|---|---|---|
| H1 | 本机 H264/HEVC 播放实际命中 VT 零拷贝路径（capability probe + session 成功），VT planar 的每帧 12~25MB 拷贝**不在**热路径 | 路由记录 zero_copy 占比 >90%；`decode_manager.playback_route`/`hw_session_denied_ffmpeg_fallback` trace 为证 | 若 playback_route 大量落 'prefer_ffmpeg'/hw_session 拒绝，或 vt_import_failed 频发 → H1 否，VT planar/FFmpeg 拷贝升级为热点 |
| H2 | 监看开启是普通预览之外的**最大增量 CPU 源**（每帧 ~7 次拷贝、≥4×w×h×4 字节触内存） | 4K 监看开：≥130MB/帧 读写字节 vs 关：≈0 回读 | A/B 同素材监看开/关的 CPU、`CPU_DOWNLOAD` 计数、p95 帧时间 |
| H3 | ProRes/DNxHR（FFmpeg planar 原生尺寸）每帧 3 平面 tight 拷贝 + PBO 中转拷贝是 4K 多轨场景的主要上传成本 | cpu_upload_bytes/texture_write_bytes ≈ 3WH×bps + PBO 附加 | planar_upload 审计字节与耗时反证 |
| H4 | CPU 编排（engine 派发）不是瓶颈：request_present 派发耗时应远小于帧预算 | 派发 total_ms（调度部分）<10% 帧预算 @目标fps | 若编排占比显著 → A1.3 候选激活 |
| H5 | R16UI 在本机（RX 6900 XT / GL 4.1 Core?）自检通过，float16 归一化回退不触发 | `_r16ui_disabled_reason is None`；planar_upload mode='r16ui_direct' | 若自检失败 → §2.4.4 每帧全平面值转换进热路径，A2.2 证据成立 |

## 7. 优先 PoC（唯一推荐先行）

**用 A0.1 harness（`scripts/bench_frame_budget.py`）在同一批素材上输出"路由+字节"联合记录**：每帧 trace 携带 decode route（zero_copy/vt_planar/ffmpeg_planar/packed + fallback reason）、`cpu_upload_bytes/texture_write_bytes/cpu_readback_bytes`、监看开/关分组。一次运行即可同时证实/证伪 H1/H2/H3/H5，且不需要先做任何优化变更——这正是 A0.1 的插桩目标。**在取得该数据前，A1/A2 全部任务维持候选状态。**

## 8. A1–A4 门禁判定（候选状态登记）

| 任务 | A0.3 证据 | 状态 |
|---|---|---|
| A1.1 GC | 无 GC 与掉帧关联数据 | 候选（待 A0.2 GC 回调观测） |
| A1.2 监看 CPU 路径 | 拷贝链已定位（§3.3），但**占比/耗时未测** | 候补第 1 顺位（H2 证实即激活） |
| A1.3 编排分配 | 派发耗时存疑但无 profile | 候选（H4） |
| A1.4 池与音画同步 | `GpuFramePool` 未接线、`AVSyncController` 仅遥测——**决策类**：接入与否待同步测试策略，非纯性能 | 候选（需单独决策记录） |
| A2.1 VT 路由 | 路由矩阵代码已核实；占比未测 | 候选（H1 证伪才可能激活） |
| A2.2 R16UI | 自检机制已核实；本机结果未测 | 候选（H5） |
| A2.3 stride 上传 | tight copy 位置已定位（§2.3.1/2.3.2）；是否热点未测 | 候选（H3） |
| A2.4 Topos GPU 解码 ADR | 未评估 | 候选（需 Topos 素材占比数据） |
| A3.1 native 调度器 | 无 profile 证据 | 未立项 |
| A4.x Metal 预览 | 无 A0 数据支撑 | 未立项 |

## 9. 复核记录

- 2026-10-22：三个 Explore 代理（解码上传链 / 合成呈现链 / 遥测设施）产出后，人工抽查了 6 处关键结论（source.py:1302 `.copy()`、DeckLink 占位、PV 监看 glReadPixels、PV blit-finally-ack、`ensure_gpu_frame` 无生产调用、`upload_ms/present_ms` 零生产者），全部与源码一致。
