# Topos Color 调色页隐藏缺陷审计与视频调色性能提升计划

> 文档日期：2026-07-30  
> 审计分支：`feature`  
> 审计基线：`59bfecf3`（`effects优化计划表`）  
> 审计范围：调色页 UI/状态同步、时间线调色数据、视频解码与播放调度、GPU 上传与渲染、GLSL、色彩管理、资源生命周期、测试与性能基准  
> 文档性质：本轮只读审计后的实施计划；本文件不代表问题已经修复  
> 约束：遵循项目 `.clinerules` 和 `AGENTS.md`；核心代码修改必须先补回归测试，修改后执行 Review 与真实硬件验证

---

## 1. 结论先行

当前调色页仍存在一批会直接影响结果正确性、视频交互和长时间稳定性的隐藏问题。最危险的并不是“平均 FPS 不够高”，而是以下几类基础问题同时存在：

1. 遮罩、Qualifier、AUX 纹理和图层 ID 的状态代际不统一，可能出现 UI 已变而 GPU 仍显示旧结果。
2. 暂停、拖动和 Seek 会在 UI 线程同步等待 master 解码，4K 长 GOP 视频可造成数百毫秒卡顿。
3. 播放时钟的倍速公式、未知总帧数、VFR/PTS 和音频主时钟尚不可靠。
4. NV12 shader 每帧编译、YUV FBO 每帧重建、Scope 每帧同步回读、`glFinish()`、整帧 `.tobytes()` 等热点阻塞了实时链路。
5. 跨 OpenGL 上下文没有可靠 fence，多个纹理池和缓存没有字节预算或完整释放，长时间播放/缩放存在撕裂、覆盖和 VRAM 增长风险。
6. NV21/P016/GBRP、高位深静帧、BT.601、HDR/PQ/HLG 等格式或色彩路径存在明确错误或缺口。
7. 时间线 `ColorStack` 仍是扁平模型，无法可靠保存调色页的多层、遮罩、插件、效果等完整状态。
8. 现有“运行时 benchmark”没有覆盖真实 QTimer→解码→上传→调色→present 全链路，且计算的 effective FPS 会掩盖解码和 coalescing，不能作为接近 DaVinci Resolve 的证明。

因此，本轮必须按“正确性与可观测性 → UI 非阻塞 → 解码/调度 → GPU 同步与资源 → pass fusion/零拷贝 → 专业色彩管理”的顺序推进。只做 shader 微优化，无法解决当前视频调色的主要卡顿。

“接近达芬奇性能”在本计划中定义为：在明确的目标硬件、素材、节点复杂度和代理策略下，达到可重复的实时播放、低延迟交互、稳定内存和专业色彩正确性指标；不以单个平均 FPS 或主观观感宣称达成。完成一轮修改可以显著接近目标，但不能在未通过本文验收门槛前承诺与 Resolve 全面等价。

---

## 2. 审计方法与当前基线

### 2.1 本轮检查方法

- 追踪调色 UI signal → controller → `ImageEngine` → `AsyncRenderer` → shader → viewport 的完整调用链。
- 追踪 Color 时间线 → `EnhancedVideoPlaybackEngine` → `PlaybackCoordinator` → `VideoSession` → decode worker/source → `DecodedFrame` → GPU 上传/呈现。
- 检查所有 latest-wins lane、generation、revision、缓存 key、纹理池、GL 对象和 shutdown 路径。
- 检查常用视频像素格式、高位深/HDR、YUV matrix/range/transfer/primaries 的处理。
- 对照已有测试、历史 benchmark JSON 和旧计划，避免把已经修复或仅属猜测的问题写成当前事实。
- 将结论分为“已由代码证明的缺陷”和“需 profiler/真实硬件确认的优化候选”。

### 2.2 本轮执行的测试

执行：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m compileall -q src tests
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests/color tests/video \
  tests/media/test_video_playback_no_ui_copy.py \
  tests/media/test_video_playback_decoded_submit.py \
  tests/media/test_playback_coordinator_tick.py \
  tests/media/test_color_frame_provider_adapter.py \
  tests/app/test_color_timeline_clip_playback_mapping.py -q
```

结果：`1303 passed, 13 failed, 10 skipped, 2 xfailed`。

13 个失败不是 13 个互不相关的产品缺陷，主要暴露了以下质量门问题：

- 5 个 coordinator tick 测试的 fixture 未提供有效 `total_frames`，当前实现提前进入 EOF；它同时暴露了“未知总帧数被当作 0”的真实产品缺陷。
- 2 个 `AsyncRenderer.__new__` 测试没有初始化 QObject/QThread 基类，直接访问 Qt property，测试夹具已经与实现契约漂移。
- `ColorFrameProviderAdapter` 的 positional/keyword 调用契约与测试预期不一致，需要统一公共 API，而不是只修改 mock。
- `ColorFrameProviderAdapter.request_proxy` 缺返回类型注解，违反项目强类型约束。

在 P0 开工前，必须先把这些失败分成“修产品”和“修测试夹具”，恢复相关测试 100% 通过；禁止删除 EOF 防护或降低断言来制造绿灯。

### 2.3 已有真实性能证据

现有产物只能作为历史参考，不能代替本轮修复后的重测：

| 证据 | 已记录结果 | 结论 |
|---|---:|---|
| `docs/playback_benchmark_final.json` | 4K H.265 约 28.97 fps，decode p95 43.55 ms | 4K30 已处在掉帧边缘，主瓶颈偏解码/调度 |
| 同一文件 | 4K ProRes 422HQ 约 33.47 fps，decode p95 19.63 ms | 可接近 30 fps，但余量不足 |
| `tests/media/results/interactive_scrub_decode_4k_h265.json` | scrub p50 141.8 ms、p95 183.7 ms，deadline miss 83.3% | 交互 Seek 与专业目标差距明显 |
| `tests/media/results/playback_runtime_20260628_220034.json` | 1080p ProRes proxy decode p95 39.6 ms；upload+YUV render p95 约 5.24 ms | 应先优化解码/Seek/调度，再做 shader 微优化 |
| `tests/media/results/vt_probe_h264_decode.json` | 当时 `zero_copy=false`、无 IOSurface/GL context | 当前零拷贝能力尚未被真实证明 |

现有 `tests/benchmark/benchmark_playback_runtime.py` 不是真实应用全链路：它没有完整覆盖 QTimer/AsyncRenderer/present，循环内创建释放输出对象，模拟队列也未正常消费，且 effective FPS 未包含 decode。该 benchmark 必须重写后才可用于发布结论。

---

## 3. 目标架构与统一状态契约

本轮需要先建立一个贯穿 UI、解码和 GPU 的不可变帧契约：

```text
UI mutation
  -> ColorStateTransaction
     {session_id, clip_id, layer_id, state_revision, dirty_domains}
  -> immutable RenderSnapshot
     {source_generation, frame_index, pts, quality_tier, color_revision}
  -> Decode/Upload/Render scheduler
  -> PresentToken
     {texture_id, frame_index, pts, state_revision, gpu_fence}
  -> presentation-only UI context
```

原则：

- `generation` 表示源/Seek 生命周期；`state_revision` 表示调色状态；`frame_index/pts` 表示素材时刻，三者不可互相替代。
- 所有异步结果落地前同时核对 session、clip、generation、revision；旧结果释放但不呈现。
- UI OpenGL context 只负责 present；资源创建、上传、计算、readback 和释放都在 renderer 所有者线程完成。
- 纯颜色参数、遮罩资源、LUT/曲线纹理、viewport、ColorOutput 使用不同 dirty domain，但同一帧发布必须是原子的一致快照。
- “播放质量”和“暂停 master 质量”是 RenderSnapshot 的显式字段，不能从全局 `is_playing` 临时推断。
- 解码、上传、渲染、scope、thumbnail 分别有预算和优先级；低优先级任务不能阻塞视频 present。

---

## 4. 问题总表

### 4.1 P0：结果错误、崩溃、死锁、泄漏或直接阻塞

| ID | 问题 | 影响 | 主要证据 | 处理阶段 |
|---|---|---|---|---|
| C-P0-01 | 遮罩编辑被当成 transient grading，跳过 GPU mask 更新 | 调色区域陈旧/错误 | `color_controller.py:57-87,301-308,480-548`; `layer_controller.py:539-551`; `image_engine.py:1983-1990` | A1 |
| C-P0-02 | 视频 Qualifier matte cache 不含帧/源 revision | 后续视频帧复用首帧 matte | `async_renderer.py:1447-1478,1617-1628` | A1 |
| C-P0-03 | AUX_TEX 全局单槽合并，独立资源更新互相覆盖 | 曲线/LUT/warper/多层更新丢失 | `async_renderer.py:201-207,349-366,414-417`; `image_engine.py:847-868` | A1 |
| C-P0-04 | 遮罩组判定、嵌套 brush/AI 查找不一致 | 空组禁用 grade、组内画笔崩溃或重复 | `image_engine.py:1882-1921,2064-2066`; `video_playback.py:2067-2068`; `color_controller.py:443-447` | A1 |
| C-P0-05 | “重置调色”实际清空整层且重复提交 | 用户数据丢失、一次操作多次渲染 | `color_wheel.py:1007-1044`; `layer_controller.py:977-980`; `layer.py:455-517` | A1 |
| C-P0-06 | ColorStack 不能往返多层/遮罩/插件/效果 | 切 clip、保存、导出时扁平化或丢失 | `color_stack.py:17-80`; `color_stack_hydrator.py:36-73`; `edit_controller.py:970-1009`; `save_router.py:1061-1100` | A2 |
| C-P0-07 | 默认值/空值无法表达显式 clear | 曲线/HSL/mixer/LUT 切片后“复活” | `save_router.py:1081-1094`; `project.py:2738-2751` | A2 |
| V-P0-01 | scrub 因先 stop 而进入 UI 同步 master seek | 长 GOP 4K 拖动可卡约 300 ms | `file_controller.py:3783-3846`; `video_playback.py:4077-4084`; `session.py:757-760` | B1 |
| V-P0-02 | 倍速时钟把 speed 乘在累计 frame 上 | 从中段改速会跳到错误帧/倒退 | `playback_clock.py:1032-1037,1050-1057,1087-1122` | B2 |
| V-P0-03 | CFR/wall-clock 调度，不使用 PTS/音频主时钟 | VFR、长片、音频同步漂移 | `playback_clock.py:1111-1122` | B2 |
| V-P0-04 | in-flight 去重不含 generation | Seek 到同帧可永久丢请求 | `decode_worker.py:409-448,981-1010`; `session.py:750-764` | B1 |
| V-P0-05 | generation 只丢结果，不取消 GOP 前向解码 | 过期 Seek 继续占满 decoder | `decode_worker.py:594-649`; `ffmpeg_source.py:1294-1374` | B1 |
| V-P0-06 | pause/seek/stop 混用并频繁销毁 worker | 热缓存丢失、恢复延迟、线程抖动 | `session.py:639-697`; `video_playback.py:4077`; `file_controller.py:3791-3795` | B1 |
| V-P0-07 | v3 播放未切效果预览质量且缓存跨状态 | 播放仍跑全质量重效果或暂停仍低质 | `video_playback.py:1991-2089,2144,2173,3744-3981` | B1 |
| V-P0-08 | 动态代理阶梯和 resize 未更新 v3 source | 日志降级但仍解原尺寸，或画面长期过糊 | `video_playback.py:2326-2388,4720-4729`; `session.py:1351-1431`; `playback_clock.py:744-754` | B3 |
| V-P0-09 | 未知 total_frames 被当作 0/-1 | 未知时长源在首帧停止或请求负帧 | `video_playback.py:2601-2648,4492-4507`; `session.py:475-481`; `playback_clock.py:1068,1121-1122` | B1 |
| V-P0-10 | master queue 对不可丢帧实际无界 | 随机 seek 后 RSS 持续增长 | `frame_queue.py:406-468` | B3 |
| V-P0-11 | 外部 callback 在不可重入锁内调用 | 重入读取/暂停/卸载可能死锁 | `session.py:408-415,520-522,632-668,793-798`; `playback_state.py:403-423` | B1 |
| V-P0-12 | 高位深静态/Seek 转 8-bit 的缩放错误 | 10/12-bit 几乎全黑，HDR 与播放不一致 | `video_playback.py:3667-3685,4052` | B4 |
| V-P0-13 | renderer 忙时 200 ms 内连时钟/解码也不推进 | 堵塞后变成 5 Hz 卡顿，恢复追旧帧 | `video_playback.py:4440-4470` | B2 |
| V-P0-14 | 视频加载在 UI 同步等待 proxy/master | 打开素材可阻塞约 0.8–1.8 秒 | `video_playback.py:3168-3345,3405-3415` | B1 |
| G-P0-01 | NV12 shader 每帧编译并带来 VAO 增长 | 编译尖峰、GL 对象泄漏 | `async_renderer.py:1359-1360`; `yuv_gpu_upload.py:2070-2179`; `async_renderer.py:1875-1884` | C1 |
| G-P0-02 | P016/NV21 半平面分发与 UV 顺序错误 | 解包错误、色度交换或崩溃 | `async_renderer.py:1225-1239,2706-2755` | C2 |
| G-P0-03 | GBRP/GBRAP 被当作 YUV444 | 专业 RGB planar 素材颜色/alpha 错误 | `edit_gpu_preview_renderer.py:233-281`; `async_renderer.py:1275-1339,2745-2751` | C2 |
| G-P0-04 | BT.601 inverse matrix 常数错误 | SD/601 素材色彩系统性偏差 | `yuv_gpu_upload.py:1717-1721`; `yuv_gpu_convert.glsl:93-100` | C2 |
| G-P0-05 | zero-copy 原子交换先释放旧 backing | 包装失败后可采样悬空纹理 | `async_renderer.py:3366-3413` | C1 |
| G-P0-06 | front texture、上传池与 YUV ring 无真实跨上下文 fence | 重载时撕裂、覆盖、释放 in-flight 纹理 | `async_renderer.py:220-224,723-788,1125-1163`; `video_texture_pool.py:126-140` | C1 |
| G-P0-07 | YUV 输出 cache 按任意尺寸永久保留 3×RGBA16F | 4K 每尺寸约 190–200 MiB，resize 可爆 VRAM | `async_renderer.py:230-234,723-766,3710-3716` | C1 |
| G-P0-08 | renderer cleanup 遗漏大量 GL 对象 | 反复开关/换源后资源不回基线 | `async_renderer.py:3683-3780`; `shader_compiler.py:28-32,726-729` | C1 |
| G-P0-09 | 图层 normal path 用 `id(layer)`，删除用 UUID | 删除层后 AUX/matte 纹理残留 | `image_engine.py:658-675,847-868,1918-2067`; `async_renderer.py:599-633` | C1 |
| G-P0-10 | AI/plugin/bake snapshot 字段和 ID 契约不完整 | AI texture 不被选中，bake 路径疑似死路 | `image_engine.py:234-240,1043-1068,2041-2047`; `async_renderer.py:992-1058,3947-3990` | C3 |
| G-P0-11 | 命令 handler 异常会丢后续已 drain 命令 | GPU frame 不释放、queue.join 卡住 | `async_renderer.py:292-329,4079` | C1 |
| G-P0-12 | CPU YUV fallback clamp 到 [0,1] 并量化 uint8 | 高位深/HDR 高光全部丢失 | `async_renderer.py:2805-2862` | C2 |
| G-P0-13 | 视频链路缺 PQ/HLG/primaries/working space | HDR/WCG 不能达到专业色彩正确性 | `async_renderer.py:165-168`; `video_color_decode.glsl` 未进入热路径 | D1 |
| G-P0-14 | morphology/blur 等动态循环和极端采样量 | 违反规范，重效果可拖垮一帧 | `morphology.glsl:13-26`; `blur.glsl:21-30`; `shader_nodes.py:531-536` | C4 |
| G-P0-15 | Scope 在 UI 每帧同步 RGB32F readback | GPU-CPU 同步、UI 卡顿、锁失败黑闪 | `layer_controller.py:553-567`; `image_engine.py:2987-2999,3101-3127`; `scope_panel.py:276-329` | B4 |
| G-P0-16 | HDR soft-light 对负值 `sqrt` | 产生 NaN/Inf 并污染后续 pass | `shader_nodes.py:663-672` | C2 |
| G-P0-17 | rectangle/external zero-copy target 用 sampler2D | 平台路径 GL error/黑帧 | `async_renderer.py:3487-3543`; `yuv_gpu_upload.py:1689+` | C2 |

### 4.2 P1/P2：高收益优化与工程质量

| ID | 问题/机会 | 计划方法 | 是否先 profile |
|---|---|---|---|
| C-P1-01 | UI setter 与 engine 同时 render，soft clip/qualifier 无统一 debounce | setter 只 mutation，统一 `ColorRenderScheduler` latest-wins | 是，记录 submit/gesture |
| C-P1-02 | 程序化 Qualifier 回填仍发 edit signal | `set_params(..., emit=False)` + hydration transaction | 否 |
| C-P1-03 | mask pending timer 只存 shape id，切层/clip 可串线 | key 带 session/clip/layer/revision；切换时 flush/cancel | 否 |
| C-P1-04 | mask tree rebuild 总选最后一项 | UUID 恢复选择，generation coalescing，不发用户信号 | 否 |
| C-P1-05 | Warper/Mask 等 panel 回填不完整 | 每个 panel 提供 total snapshot/default reset | 否 |
| C-P1-06 | 时间线 Color 播放忽略 speed/reverse/rational FPS | 复用唯一 FrameMapper 和 timeline clock | 否 |
| C-P1-07 | timeline strip 播放时仍做 thumbnail，且全量 QWidget rebuild | 暂停 thumbnail；虚拟化/diff/recycle；DPR pixmap cache | 是 |
| C-P1-08 | 每次 interaction_started 序列化全部层历史 | 首次 mutation 才 capture；增量 command/asset ref | 是 |
| C-P1-09 | LUT 反编译和 denoise/deblur 结果处理留在 UI slot | worker + generation/cancel + 原子 swap | 是 |
| C-P1-10 | unload 的多组 signal disconnect 放在同一 `try` | 每个连接独立安全断开并用连接 owner 管理 | 否 |
| C-P2-01 | clip strip timecode 固定 24 fps | 注入 timeline Rational FPS 与 drop-frame formatter | 否 |
| V-P1-01 | provider zero-copy 私有 `_vt_source` 路径不可达 | 正式 `GpuDecodedFrame` capability + lease | 否 |
| V-P1-02 | 多 consumer 共用可变 decoder/cache 无锁 | per-source 串行或 decoder pool；keyed Future 合并 | 否 |
| V-P1-03 | proxy request 可能 fallback 到 master | 实时调用强制 `allow_master_fallback=False` | 否 |
| V-P1-04 | packed upload `.tobytes()`、RGB24 补 alpha | memoryview/PBO；原生 RGB；优先 planar | 是 |
| V-P1-05 | `video_preview_drop_frames` 配置未接行为 | realtime drop 与 quality rebuffer 两种显式策略 | 否 |
| V-P1-06 | 首帧/静帧缓存使用整帧 `.copy()` | 定义 DecodedFrame lease/不可变 buffer 与池化所有权 | 是 |
| V-P1-07 | seek/master command 被视为不可合并关键命令 | 按 source/generation 建 replaceable seek lane；export 保持严格队列 | 是 |
| V-P1-08 | provider API positional/keyword 与类型契约漂移 | 固化 protocol、返回类型和 contract tests | 否 |
| V-P2-01 | 固定帧数 lookahead 不适应 fps/speed/codec | 改毫秒预算，结合 decode/GPU p95 自适应 | 是 |
| V-P2-02 | 反向播放无 GOP ring 调度 | signed rate；keyframe→正解 GOP→逆序呈现；代理优先 | 是 |
| G-P1-01 | YUV ring 只有单 FBO，轮转时每帧重建 | 每个 slot 固定 FBO，随 ring 生命周期释放 | 否 |
| G-P1-02 | program cache key 误含 uniform select、缺 source hash、无 LRU | 结构 key + source hash + bounded LRU + safe eviction | 否 |
| G-P1-03 | 编译前释放 last-good shader | candidate 成功后原子 swap；失败继续显示 last-good | 否 |
| G-P1-04 | 每层一次 RGBA16F 全屏 pass，缺真正 fusion | identity fast path；融合连续 color-only 层；空间算子为边界 | 是，GPU timer 后决定 |
| G-P1-05 | 每帧构建/复制 dict、全量判断和 uniform 上传 | immutable snapshot、structural signature、dirty bitset | 是，CPU profiler 后决定 |
| G-P1-06 | ColorOutput/viewport_refresh 使用 `ctx.finish()` | output ring + GLsync + consumer ACK，禁止全局 finish | 否 |
| G-P1-07 | 上传失败不回滚 texture pool lease | context-manager lease，成功 commit，异常 rollback | 否 |
| G-P1-08 | renderer 线程现场解析大 3D LUT | worker parse/cache(path+mtime/hash)，renderer 仅 upload | 是 |
| G-P1-09 | full-range chroma 固定减 0.5 | 按 bit depth 使用准确 code center | 否 |
| G-P1-10 | hidden 尾层使 last-visible grain 不执行 | 预计算最后可见 composited layer | 否 |
| G-P1-11 | ColorStack render service 与 Color 页字段不一致 | canonical DTO/hydrator，preview/export parity | 否 |
| G-P2-03 | 不存在对应 uniform 时仍先 bind AUX texture | 先检查 program uniform，再绑定实际需要的纹理 | 是 |
| G-P2-01 | VAO 在 program identity 未变时也全清 | 只失效关联 program/revision 的 VAO | 是 |
| G-P2-02 | LUT 先 clamp 再拉伸 HDR，且仅 trilinear | 评估 shaper+tetrahedral+域外规则 | 必须色彩科学对标 |
| OBS-P1-01 | render 异常只记录日志，旧帧仍像成功结果 | typed error signal + revision watchdog + HUD | 否 |
| TEST-P1-01 | benchmark 不覆盖真实全链路 | 建立 production instrumentation 与自动报告 | 否 |

---

## 5. 关键问题的具体修复与验证

### 5.1 A1：统一调色事务、遮罩、Qualifier 和 AUX 资源

#### 实现方法

1. 新增强类型枚举和不可变 DTO：
   - `DirtyDomain.COLOR_PARAMS`
   - `DirtyDomain.MASK_GEOMETRY`
   - `DirtyDomain.AUX_CURVE/LUT/WARPER/AI_TEXTURE`
   - `DirtyDomain.VIEWPORT`
   - `RenderReason.INTERACTION/PRESENT_FINAL/PLAYBACK/COLOR_OUTPUT`
2. 所有 UI mutation 只修改 model 并递增 `state_revision`；只有统一 scheduler 提交 render。
3. AUX lane 从单槽改为 keyed map：key 至少为 `(stable_layer_id, resource_type)`；同 key latest-wins，不同 key 全部保留。
4. grade/render 前建立依赖屏障：所需 AUX revision 没完成时，不允许发布对应 state revision 的帧。
5. mask 使用唯一递归函数判断“有效可见内容”；空 group、全隐藏 child 不得把整层 matte 变成 0。
6. brush/AI 查找统一用递归 typed iterator 或 `get_mask_item_by_id()`，禁止访问并不存在的 `MaskGroup.type`。
7. 动态 Qualifier matte key 包含 `source_generation + frame_index/source_texture_revision + qualifier_revision`；静态 shape mask 可继续独立缓存。
8. 所有 layer/AUX/plugin key 统一使用稳定 UUID；迁移期间 cleanup 同时识别历史 numeric key。

#### 验证方法

- 对 ellipse/polygon/linear/radial/brush/AI、group/nested group、invert、feather、operation 建 pixel golden。
- 红/绿交替视频上使用红色 qualifier，每帧 matte hash 必须交替，Seek/暂停后仍正确。
- 同一 event-loop tick 更新 2 层 × curves/LUT/warper，断言 6 个不同 key 都执行；同 key 100 次只执行最后一次。
- 快速拖 mask 100 次，最终 present token 的 revision 必须等于 model revision；中途旧帧不得覆盖最终帧。
- 删除/新增 100 层后，AUX/matte texture 数量回到基线。
- 一个用户 valueChanged 最多触发一次 render submit；gesture end 只产生一次 final-quality render。

### 5.2 A2：ColorStack 完整 round-trip 与安全 reset

#### 实现方法

- 发布 `ColorStack schema vNext`：保存 ordered `layers:[LayerSnapshot]`、active layer、mask tree、plugin/effect、warper、qualifier、grain、LUT、RAW/source settings 和 engine-global fields。
- 定义 `UNSET` 与显式 `None/{}` 的区别；空值必须可以清除旧状态。
- 提供旧 flat schema → 单 layer 的确定性迁移，并保存 schema version。
- `reset_primary_grade` 只重置主调色；`reset_layer_all` 单独命名、二次确认；RAW reset 独立。
- reset/hydration 通过单个 undoable transaction 完成，只提交一次 final render。
- 若 vNext 未完成，时间线模式必须暂时禁用多层编辑并明确提示，不能继续静默扁平化。

#### 验证方法

- 3 层分别设置 primary、curves、mask、LUT、plugin/effect、warper；执行 A↔B clip、Color↔Edit、save/reopen、preview、export，序列化结构与输出 hash 一致。
- 每个字段执行 apply→save→reset→切 clip→重开→export，确认不会“复活”。
- 点 primary reset 后，mask/LUT/effect 等保持不变，undo stack 只增加 1 条，render submit=1。

### 5.3 B1：无阻塞 Seek、代际取消与稳定生命周期

#### 实现方法

- 引入显式 `SeekMode`：`SCRUB_PROXY_ASYNC`、`PAUSED_MASTER_ASYNC`、`EXACT_BLOCKING_NON_UI`；UI 永远不能调用 blocking mode。
- scrub 使用 latest-only mailbox，12–25 ms 自适应 debounce；拖动只取 proxy/keyframe-nearest；松手只发一次 master promotion。
- 视频加载改为全异步状态机：立即显示 placeholder/上一张安全画面，优先请求 proxy，master 到达后按 generation 原子升级；禁止 UI `sleep` 或 timeout wait。
- 将 `stop()` 拆为 `pause_transport(keep_workers=True)`、`seek_async()`、`teardown()`；只有卸载/退出销毁线程。
- in-flight key 改为 `(generation, frame, quality)`；旧 generation 结束后必须重新检查最新同帧请求。
- cancellation token 下沉到 demux/GOP forward decode，每解一帧检查取消；不可取消的 decoder 支持安全重建。
- session/coordinator 锁内只更新状态和复制 callback，释放锁后才通知外部。
- 未知时长用 `Optional[int]`；只由明确 EOS 结束，不做 `-1` 或 0 上界 clamp。
- seek/master 实时命令按 `(source, generation)` latest-wins；只有 export、精确离线处理等不可丢工作保留严格 FIFO。
- 第一帧和静态预览保留带 lease 的不可变 `DecodedFrame`，只有 decoder buffer 生命周期确实要求时才复制；复制字节数必须可观测。

#### 验证方法

- barrier 单测复现“旧 generation 正在解 N，新 generation 也请求 N”；新请求恰好交付一次。
- 4K H.265 长 GOP 随机 scrub 1000 次：UI event-loop p99 < 8 ms；拖动 proxy p95 < 120 ms；松手 master p95 < 300 ms；UI blocking decode 计数=0。
- 打开 100 个不同 GOP/codec 素材：load handler 自身 < 16 ms，first proxy p50 < 80 ms、p95 < 200 ms，旧素材回调不会覆盖新素材。
- play/pause/seek/play 循环 1000 次：worker/thread 数恒定，恢复首帧 p95 < 50 ms。
- callback 内重入 `state/is_playing/pause/unload`，watchdog 下无死锁。
- 未知 total_frames 的假 source 从 0 播放至 EOS，绝不请求负帧。
- 统计 frame copy bytes：稳定播放为 0，首次/暂停/Seek 不出现无所有权理由的 4K 整帧复制。

### 5.4 B2：PTS 驱动的专业播放时钟

#### 实现方法

- 修正倍速公式：`ideal = anchor_frame_or_pts + elapsed_since_anchor * rate`；seek、resume、rate change 都重置锚点。
- 使用 `time.monotonic_ns()`；禁止 wall-clock 作为播放推进依据。
- VFR 使用 `best_effort_timestamp × time_base`；有音频时 audio-device clock 为 master，无音频时 monotonic clock 为 master。
- renderer 背压时继续推进 clock、playhead 和 latest decode，只合并/抑制 render submit；恢复后直接追最新目标，不补播全部旧帧。
- drop/rebuffer 策略显式接入 `video_preview_drop_frames`。
- Color 时间线统一走 rational FPS/speed/reverse 的 FrameMapper，不再用 source decoder 自由顺播模拟 timeline。

#### 验证方法

- 起点 0/100/中段，0.25×/0.5×/1×/2×/4×：改速首 tick 不跳变、不倒退。
- 23.976、29.97、VFR 一小时模拟；漂移 p95 < 0.5 帧。
- 音频素材 AV skew p95 < 20 ms、max < 40 ms；模拟系统时间跳变，播放位置不变。
- renderer 人为 stall 500 ms，playhead 继续单调推进，恢复后一个帧周期内追到最新帧。
- 0.5×/2×/reverse/trim/effective subclip 的逐帧映射与 `timeline_to_source_frame` 完全一致。

### 5.5 B3/B4：代理、缓存、高位深和 Scope

#### 实现方法

- proxy tier 成为 `VideoSession` 的正式状态；后台建立新 source，首帧 ready 后原子交换并 bump generation。
- viewport size 量化为少量 size class，跨 class 才重建；连续 resize debounce。
- master/proxy/GPU frame queue 同时受帧数和字节预算约束；只 pin 当前 consumer 目标，其余使用有界 LRU。
- 播放、暂停和 Seek 均走同一 `DecodedFrame` 高位深 GPU 路径；禁止为了静帧将 10/12/16-bit/HDR 压成 uint8。
- Scope 改为 renderer 低优先级 lane：GPU downsample/compute histogram/waveform/vectorscope，10–15 Hz latest-only；优先只异步读 bins，最低限度使用 PBO ring+fence。
- Scope lock/fence 未就绪时保留上一帧，不读 clear 后的 FBO。

#### 验证方法

- 代理降级后检查实际 `DecodedFrame.width/height`，不能只检查日志；resize 1000 次 source 创建次数有界、无黑帧。
- 连续请求 10000 master 帧，queue 不超过 `limit+pinned`，RSS/VRAM 平台稳定。
- 10/12/16-bit ramp 与 HDR 高光：播放与暂停同帧 pixel parity，无全黑、banding 或 >1 高光丢失。
- 4K60 + scopes on/off：present drop 差异 < 0.2%，UI event-loop p99 < 8 ms，主线程 GL readback 调用=0，无黑闪。

### 5.6 C1：GPU 同步、资源所有权与无泄漏热路径

#### 实现方法

- NV12/P010 等 shader/program/VAO 缓存为 uploader 所有对象，context 生命周期只编译一次并完整释放。
- 每个 YUV ring slot 固定 `(texture, framebuffer, producer_fence, lease_state)`，禁止单 FBO 随帧轮换重建。
- front buffer 使用 3-slot present ring。renderer 发布带 GLsync 的 PresentToken；UI 非阻塞 poll，未完成则继续显示上一个完整帧；消费后 ACK。
- texture/FBO/program/VAO/VBO/IBO/uploader/pipeline/utility shader 进入统一 ownership registry；`release()` 幂等，异常初始化也可清理。
- YUV size cache 使用 size class + 字节预算 LRU；只有 fence 完成且无 lease 时释放。
- 所有上传/临时纹理使用 lease/context manager；写入或 render 异常自动 rollback。
- command loop 每命令独立 `try/finally`，总能调用 payload discard/release 和 `task_done()`；shutdown drain pending。
- shader candidate 编译成功后才原子替换 last-good；失败保留旧画面并上报 diagnostics。

#### 验证方法

- 连续 1000 NV12 帧：program compile=1，FBO create≤ring size，VAO/GL object 数稳定。
- 注入 empty/wrap/write/render/compile failure：旧完整帧安全保留，新资源恰好释放一次，下一命令继续执行，`queue.join()` 完成。
- 4K 高 GPU 负载 + 慢 consumer + 连续 resize：无撕裂/混帧/invalid GL ID，`glFinish` 调用=0。
- 打开/关闭 100 个视频、add/remove 100 层：GL live objects、RSS、VRAM 回到允许误差内的基线。
- 100 个不同 viewport 尺寸后，YUV cache 保持在字节预算内。

### 5.7 C2/D1：像素格式与专业色彩管理

#### 实现方法

- `DecodedFrame` 显式携带 `plane_layout`、`uv_order`、`bit_depth`、`range`、`matrix`、`transfer`、`primaries`、`chroma_location`、alpha 和 texture target；禁止靠字符串 substring 分发。
- NV12/NV21/P010/P016 分别选择正确 UV 顺序与 bit-depth shader；GBRP/GBRAP 使用独立 planar RGB(A) shader并执行 G/B/R 重排。
- 建立单一权威 YUV matrix/range table，BT.601 使用标准常数；full-range chroma 中心按 bit depth 计算。
- CPU fallback 保持 float16/32 或正确的高位深 swscale 输出，不 clamp HDR 到 [0,1]。
- rectangle/external target 分别使用 `sampler2DRect`/平台 external variant，坐标和 extension 正确，target 纳入 program key。
- 建立视频色彩链：YUV matrix/range → EOTF（PQ/HLG/log）→ primaries/chromatic adaptation → scene-linear working space → grade → display/output transform。
- 对 HDR-safe blend 做 [-4,16] 数域测试，消除 `sqrt(negative)`、NaN/Inf；不使用暴力 clamp 掩盖问题。
- LUT 的 shaper、tetrahedral 插值和域外行为先与 OCIO/Resolve reference 对比，再决定实现，不预设一定更快或更准。

#### 验证方法

- 格式矩阵：NV12/NV21/P010/P016、YUV420/422/444、GBRP/GBRAP 10/12/16、packed RGB/BGR；每种覆盖奇数尺寸、full/limited、alpha。
- 使用 FFmpeg/libswscale/OCIO 生成独立 golden，不允许测试复制生产常数充当 oracle。
- SMPTE bars、neutral gray、ramp、skin tone、saturated primaries；记录 max/mean RGB error 和 ΔE。
- BT.709、P3-D65、BT.2020、PQ、HLG；验证暂停/播放/导出 parity，HDR >1 高光保留。
- 所有 blend mode 对输入 [-4,16] sweep，NaN/Inf 数=0。

### 5.8 C3/C4：插件、bake、重效果和 pass fusion

#### 实现方法

- canonical plugin/layer DTO 必须含 stable layer id、plugin type、instance id、enabled、params、AI texture revision、is_baked/bake revision。
- 明确 bake contract 并接入 multi-pass renderer；bake 前后像素一致，只有通过 benchmark 才宣称加速。
- 动态循环改成编译期常量上界；但 morphology/大半径 blur 不只做语法修复：采用 separable/sliding-window/compute、dual-Kawase 或 mip 算法。
- 首先用 GPU timer query 量化每个 pass；随后实现：
  1. 中性参数 identity fast path；
  2. 按结构专门化昂贵可选节点；
  3. 融合连续无 mask、normal blend、color-only 图层；
  4. mask、neighborhood operation、复杂 blend 作为明确 pass boundary；
  5. 播放代理质量，暂停/idle refine 到 master。
- program cache key 只包含真正改变源码的结构项，并加入 source/version hash；bounded LRU 淘汰时 fence-safe release。

#### 验证方法

- AI+DCTL/GLSL 混合栈正确选中 texture；bake 前后输出在误差阈值内，pass count/GPU time 下降。
- morphology/blur 在 macOS、Intel、AMD、NVIDIA 编译；GPU p95 与 CPU reference 像素误差均达标。
- 1/4/8/16 层矩阵分别记录 pass 数、GPU p50/p95/p99；简单连续 color-only 层的 pass 数显著低于层数。
- 切换 uniform select 不增加 program 数；修改插件源码 hash 必须重新编译；非法 GLSL 时仍呈现 last-good。
- 所有“预期提升”以 timer 结果决定是否保留；不足 5–10% 且增加复杂度的优化应回滚。

---

## 6. 实施计划表

复杂度采用 `S/M/L/XL`，表示相对工程量，不是工期承诺。每一阶段必须通过退出门才进入下一阶段。

| 阶段/任务 | 具体交付物 | 依赖 | 复杂度 | 退出门 |
|---|---|---|---:|---|
| P0-00 恢复测试基线 | 修复 13 个失败的产品/fixture/API 契约；新增 benchmark manifest | 无 | M | 聚焦测试 100% pass |
| P0-01 全链路 telemetry | 统一 span 与 frame identity；CPU/GPU timer、queue、drop、VRAM、GL object counter | P0-00 | L | 每帧可从 request 追到 present |
| P0-02 Golden 素材库 | codec/bit-depth/range/matrix/transfer/PTS/mask/qualifier 素材及独立 oracle | P0-00 | L | CI 可重复生成/校验 |
| A1 调色事务与 AUX | DirtyDomain、RenderSnapshot、keyed AUX、mask/qualifier revision、stable ID | P0-01 | XL | 5.1 全部测试通过 |
| A2 ColorStack vNext | 完整 LayerSnapshot、UNSET/clear、迁移、reset/undo transaction | A1 | XL | save/reopen/export hash parity |
| B1 无阻塞 Seek | SeekMode、latest mailbox、cancel token、稳定 worker、锁外 callback | P0-01 | XL | scrub/生命周期指标通过 |
| B2 专业时钟 | monotonic/PTS/audio master、rate anchor、背压追帧、FrameMapper | B1 | XL | VFR/长片/AV sync 通过 |
| B3 代理与缓存 | session proxy tier、size class、原子交换、byte-budget queue | B1 | L | 实际尺寸与内存平台稳定 |
| B4 高位深与 Scope | 统一 DecodedFrame 路径、GPU scope/异步 bins | A1,B1 | XL | parity + UI p99 通过 |
| C1 GPU 生命周期 | program/FBO cache、present fence、lease、LRU、完整 cleanup、异常 drain | P0-01 | XL | 100-cycle soak 无增长/混帧 |
| C2 格式正确性 | plane descriptor、NV21/P016/GBRP、BT.601/full-range、target variants | C1,P0-02 | XL | 全格式 golden 通过 |
| C3 plugin/bake | canonical DTO、AI texture、bake 接入与 parity | A1,C1 | L | 功能与 pass/time 门槛通过 |
| C4 重效果优化 | morphology/blur 算法、identity path、quality tier | C1,P0-01 | XL | GPU p95 达到帧预算 |
| D1 HDR/WCG 管线 | EOTF/primaries/working space/ODT、HDR-safe blend、LUT 评估 | C2,P0-02 | XL | ΔE/HDR/导出 parity 通过 |
| D2 零拷贝 | capability API、GpuDecodedFrame lease、平台 external textures、fallback telemetry | C1,C2 | XL | 支持平台 ratio≥95% |
| D3 pass fusion | timer 驱动的 color-only fusion、结构化 shader cache | A1,C1,C4 | XL | 1/4/8/16 层门槛通过 |
| E1 时间线 UI | thumbnail 抑制、clip strip 虚拟化、rational/drop-frame timecode | B2 | L | 2000 clips 滚动/刷新达标 |
| E2 30 分钟 soak | 多 codec、切片、resize、scope、seek、调参组合压力 | 全部 | L | RSS/VRAM/thread/GL 稳定 |
| E3 Release Review | Review 核心文件、性能报告、回滚开关、兼容矩阵 | 全部 | M | 无 Critical，发布门全部通过 |

### 6.1 每个任务必须填写的实现记录

实施时在本表或对应 PR 中补齐：

- 负责人、起止 commit、影响文件。
- 修改前/修改后的相同素材、相同硬件、相同设置数据。
- correctness test、performance test、soak test 的命令和产物路径。
- 新增线程、缓存、GL 对象的所有权与释放者。
- feature flag/回滚路径；失败时是否能安全回退到上一张完整帧。
- 未通过的指标、原因和后续任务，禁止只记录平均 FPS。

---

## 7. 可观测性与新 benchmark 规范

### 7.1 统一事件字段

每个 span 至少包含：

```text
session_id, clip_id, source_generation,
frame_index, pts, state_revision, quality_tier,
codec, pixel_format, bit_depth, width, height,
zero_copy, fallback_reason, queue_depth
```

阶段事件：

```text
ui_input
decode_request -> decode_start -> decode_ready
upload_start -> upload_end
render_start -> pass[n] -> render_end
fence_signal -> fence_ready
present_request -> presented
scope_request -> scope_ready
```

### 7.2 必须报告的指标

- 帧时间：CPU submit、decode、upload、每个 GPU pass、fence wait、present 的 p50/p95/p99/max。
- 交互：input-to-present、scrub proxy、master promotion、first-frame 延迟。
- 播放：requested/decoded/submitted/presented/dropped/duplicated/held/late。
- 稳定性：RSS、VRAM、GL object、texture/FBO/program/VAO 数、线程数、queue bytes。
- 色彩：max/mean error、ΔE、NaN/Inf、HDR over-range 保留率。
- 能力：hardware decode 命中率、zero-copy ratio、每种 fallback reason 次数。

### 7.3 benchmark 必须使用真实应用链路

- 真实 Qt event loop、真实 `PlaybackCoordinator/VideoSession/AsyncRenderer`、真实 GL context sharing 和 present。
- 预热与正式测量分离；固定功耗模式、显示刷新率、代理等级、scopes 状态和后台任务。
- effective FPS 以实际 presented frame/真实墙钟时间计算；decode 不能从总耗时中排除。
- 队列必须真实生产与消费，记录 coalescing/drop；不允许只把 enqueue 当呈现完成。
- 报告硬件、OS、驱动、Python/FFmpeg/PyAV/ModernGL 版本和 git SHA。
- 每项至少 3 次、每次不少于 60 秒；soak 不少于 30 分钟。

---

## 8. 验收素材矩阵

| 维度 | 必测组合 |
|---|---|
| 分辨率/帧率 | 1080p24/25/30/60；4K 23.976/25/29.97/50/59.94/60；奇数尺寸 |
| Codec | H.264、H.265/HEVC 8/10-bit、ProRes 422HQ/4444、DNxHR HQX、无硬解 fallback |
| Layout | NV12、NV21、P010、P016、YUV420/422/444、GBRP/GBRAP、packed RGB/BGR |
| Range/matrix | full/limited；BT.601、BT.709、BT.2020 NCL |
| Transfer/gamut | SDR gamma、PQ、HLG；Rec.709、P3-D65、BT.2020 |
| Timeline | CFR、VFR、23.976/29.97 rational、trim、subclip、0.5×/2×/4×、reverse |
| 调色复杂度 | 1/4/8/16 层；primary、curve、LUT、mask、qualifier、grain、blur、morphology、plugin、bake |
| 交互 | 连续调参、mask 拖动、随机 seek、play/pause、切 clip、resize、scope on/off |
| 生命周期 | 连续开关 100 文件、切换 1000 clip、30 分钟播放、异常/取消/解码失败注入 |

标准素材应由脚本生成或记录合法来源与 hash；色彩 golden 使用独立实现生成，不复制生产 shader 常数。

---

## 9. “接近 DaVinci”发布门槛

以下是首轮工程目标，需按目标硬件分别记录。若目标硬件未定义，至少覆盖项目主要开发 Mac 和一台独显机器。

### 9.1 实时播放与交互

| 指标 | 目标 |
|---|---:|
| 4K 24/25/30 常用调色栈 steady-state presented drop | `< 0.1%` |
| 4K60 | 半分辨率或高质量代理实时；drop `< 0.1%` |
| presentation jitter p95 | `< 0.5 帧周期` |
| renderer queue depth p95 | `≤ 2` |
| UI event-loop p99 | `< 8 ms`，不得出现 >50 ms 的同步 Seek/Scope stall |
| scrub proxy p95 | `< 120 ms` |
| 松手 master promotion p95 | `< 300 ms` |
| play/pause 恢复首帧 p95 | `< 50 ms`（热 worker） |
| AV skew | p95 `< 20 ms`，max `< 40 ms` |
| 调色 input-to-present | 1080/proxy p95 `< 33 ms`；4K master p95 `< 50 ms` |

### 9.2 GPU、内存与稳定性

| 指标 | 目标 |
|---|---:|
| GPU render p95 | 不超过目标帧预算的 70%，保留 present/系统余量 |
| 4K upload p95 | `< 2 ms`（零拷贝路径接近 0） |
| 支持平台 zero-copy ratio | `≥ 95%` |
| scopes on/off 对 drop 的影响 | `< 0.2%` |
| 30 分钟 RSS/VRAM | 预热后平台稳定；无单调增长，波动在预算内 |
| 100 次 resize/open-close | texture/FBO/program/VAO/thread 数回基线 |
| `glFinish` 热路径调用 | `0` |
| NaN/Inf/GL debug error | `0` |

### 9.3 色彩正确性

- 暂停 master、播放 proxy/master、Color viewport、Edit preview、export 对同一 snapshot 保持定义内的一致性。
- YUV 格式、range、matrix、transfer、primaries 由 metadata 明确驱动；缺失 metadata 有可见、可记录的默认策略。
- SDR/HDR golden 的 RGB/ΔE 阈值在素材库 manifest 中按路径固定；任何性能优化不得通过扩大阈值掩盖错误。
- 10/12/16-bit 和 HDR 超范围不得被中途无条件量化到 8-bit 或 clamp 到 [0,1]。

---

## 10. 测试新增清单

### 单元测试

- keyed AUX lane：不同 key 不丢、同 key latest-wins。
- dynamic qualifier cache key 包含 frame/source revision。
- mask effective-content、nested brush/AI、stable layer id。
- playback rate anchor、unknown duration、generation same-frame race、锁外 callback。
- plane descriptor dispatch、BT.601/full-range 独立 golden、HDR negative/over-range。
- shader last-good、program cache key/source hash/LRU、upload lease rollback。

### 集成测试

- controller mask mutation → AUX upload → render → present revision。
- ColorStack 多层 save/reopen/page switch/export round-trip。
- scrub proxy→master promotion，快速反向 Seek 不显示旧 generation。
- v3 proxy tier 实际 source 尺寸切换；pause 恢复 full-quality 参数。
- repeated NV12 真实 GL frame：compile/FBO/object count 稳定。
- scope on/off：无 UI GL readback、无黑闪。

### 压力与故障注入

- decoder/GPU/upload/shader compile 任意阶段抛错，资源恰好释放一次，last-good frame 保留。
- renderer stall、慢 consumer、窗口 resize、切 clip、关闭 session 并发。
- 30 分钟 codec×调色复杂度 soak；记录 RSS/VRAM/thread/queue/GL objects。

---

## 11. 风险、回滚与实施纪律

- 新调度器、ColorStack vNext、present fence、零拷贝和色彩管理均使用独立 feature flag 分阶段启用。
- 每次只改变一个主要变量，保留 A/B benchmark；禁止同时重写 decoder、scheduler 和 shader 后无法定位收益/回归。
- 任何缓存都必须同时定义 key、字节预算、eviction、in-flight 安全条件和 shutdown owner。
- 任何“降级”都必须在 HUD/telemetry 中记录实际 quality tier、尺寸和 fallback reason，不能只写日志。
- 任何异步操作都必须定义 cancel、generation、完成回调线程、旧结果处理和资源释放。
- GPU 优化以 timer query 为依据；CPU 优化以 profiler/alloc/frame 为依据。没有数据时标记为假设，不写“提升 X%”。
- 修改 core renderer、session、clock、shader compiler 后必须执行 Review；Critical 和 Suggestion 处理完再进入下一阶段。

---

## 12. 最终 Definition of Done

只有同时满足以下条件，本轮才可标记完成：

- 本文 P0 项全部修复或有明确、被接受的功能禁用方案。
- 本轮聚焦测试、完整测试和新增 golden/integration/soak 测试全部通过。
- 真实应用全链路 benchmark 已替代误导性 synthetic effective FPS，并保存硬件/版本/素材/命令/原始 JSON。
- 4K30、4K60 代理、scrub、AV sync、UI p99、zero-copy、RSS/VRAM、色彩 golden 达到第 9 节门槛。
- scopes 开启不会让 UI 主线程做 GL readback，播放热路径没有 `glFinish()`。
- 30 分钟压力测试后无单调内存/显存/线程/GL 对象增长。
- ColorStack 多层调色在切 clip、切页、保存重开、preview 和 export 间完整一致。
- 所有核心修改完成 Review，未遗留 Critical；回滚开关与兼容说明已写入发布文档。

在这些门槛全部通过之前，只能描述为“完成某阶段优化”，不应宣称已经达到或等同 DaVinci Resolve。
