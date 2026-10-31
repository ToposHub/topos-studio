# 剪辑页视频播放解码渲染链路审计与优化计划

创建日期: 2026-06-05
范围: Source Viewer 素材播放、Program Viewer 时间线片段播放、Color 页 viewport 视频预览、Composite 页 viewport/节点视频预览、GPU 解码/渲染、CPU fallback、帧序/纹理生命周期。

目标:

1. 普通 H.264/H.265 时间线播放优先走硬件解码 + GPU resident 渲染。
2. 播放热路径不在 UI 线程做同步 seek、CPU YUV/RGB 转换、QImage 拷贝、每帧 `glTexImage2D` 上传。
3. 时间线播放不出现旧帧回跳、GPU texture 被复用导致的画面抖动、Source Viewer CPU/GPU 双路径互相覆盖。
4. 每一项优化都有可复现验证、可量化指标和明确验收标准。

---

## 一、当前链路结论

### 1. Source Viewer 素材播放链路

```text
SourceViewerWidget QTimer(约 30fps)
  -> _on_playback_tick()
  -> _load_frame(frame)
  -> _load_video_frame(frame)
  -> 先调用 _gpu_frame_provider(asset_path, frame)
  -> provider 异步提交 GPU render task 后返回 None
  -> 立即进入 CPU fallback:
       VideoDecoder.get_frame(frame)
       -> 每帧 GStreamer FLUSH | ACCURATE seek
       -> PLAYING 拉一帧
       -> PAUSED
       -> BGR numpy
       -> QImage.copy()
       -> SourceViewer.set_texture_from_qimage()
       -> glTexImage2D 上传
  -> 稍后 GPU 结果到达时再 set_shared_texture() 覆盖
```

结论: Source Viewer 目前不是全 GPU。即使 GPU provider 存在，也会立即执行 CPU fallback；素材播放卡顿的主因很可能就是 UI 线程同步逐帧 seek + CPU 拷贝 + OpenGL 上传。

证据:

- `src/features/edit/ui/source_viewer.py:1087` 使用 `QTimer`，默认 33ms。
- `src/features/edit/ui/source_viewer.py:1403` 每个 tick 直接 `_load_frame()`。
- `src/features/edit/ui/source_viewer.py:1497` GPU provider 被调用后，当前实现预期返回 `None`，继续 CPU fallback。
- `src/app/controllers/file_controller.py:1036` 注释说明 Source GPU provider “Always returns None”，CPU fallback 会立即运行。
- `src/features/edit/ui/source_viewer.py:1526` CPU fallback 调用 `VideoDecoder.get_frame(frame)`。
- `src/shared/video/video_decoder.py:582` 每帧执行 `FLUSH | ACCURATE` seek，并在 `595-602` 播放/暂停 pipeline。

### 2. 时间线片段播放链路

```text
TimelinePanel / EditPlaybackController
  -> PlaybackControllerTimer._on_tick()
  -> controller.tick()
  -> controller.frame_changed.emit(frame)
  -> timer.frame_ready.emit(frame)
  -> TimelineView.set_playhead(frame)
  -> EditPage._on_playhead_changed(frame)
  -> timeline_frame_requested.emit(frame)
  -> TimelinePreviewRenderer.request_render(frame)
  -> PreviewRenderWorker._execute_render()
  -> 优先 _try_gpu_render()
       EditGpuDecodeManager.get_frame(asset_path, source_frame)
       -> gpu: VT zero-copy GpuDecodedFrame
       -> planar: CPU planar YUV + GPU upload
       -> cpu: CPU packed RGB
  -> gpu/planar 成功: EditGpuPreviewRenderer
  -> gpu 不可用或任一层失败: CPU composite path
       VideoFrameProviderService.request_frame()
       -> FFmpegMediaSource.read_frame()
       -> seek_frame() + read_next_frame()
       -> frame.reformat()/to_ndarray()
       -> numpy composite
       -> QImage.copy()
       -> ProgramViewer paintGL 内 glTexImage2D
```

结论: Timeline 有 GPU 优先路径，但不是强制全 GPU。H.264 只有在 `EditGpuDecodeManager` 成功创建 VT/zero-copy source 时才 GPU；否则普通 H.264 会进入 CPU packed RGB fallback。多轨、comp clip、composite-bound clip、GPU renderer 未初始化、queue full、任一层 decode 失败都会整帧 fallback。

证据:

- `src/features/edit/core/edit_gpu_decode_manager.py:154` 优先尝试 hwaccel/zero-copy，失败后在 `203-218` 对非 planar preferred codec 返回 CPU packed source。
- `src/features/edit/core/timeline_preview_renderer.py:516` 先尝试 GPU，`521-547` 失败后进入 CPU composite。
- `src/features/edit/core/timeline_preview_renderer.py:590` composite-bound clip 直接禁用 GPU direct preview。
- `src/features/edit/core/timeline_preview_renderer.py:621` `frame_type == "cpu"` 时释放并 fallback。
- `src/shared/media/ffmpeg_source.py:1040` 明确标注 `read_frame` 是同步解码，不应在播放 tick 直接调用。
- `src/shared/media/ffmpeg_source.py:1066` 非当前帧会先 `seek_frame()`，`1218-1227` GOP codec 走 keyframe seek + decode forward。
- `src/shared/media/ffmpeg_source.py:1493-1507` CPU packed path 会 reformat + `to_ndarray()`。

### 3. Source Viewer 画面变形链路

```text
SourceViewer CPU fallback
  -> 使用 viewer 当前控件尺寸生成 VideoDecoder target_size
  -> 解码输出被缩放到控件比例，而不是素材原始显示比例
  -> set_texture_from_qimage() 使用缩放后的 QImage 尺寸
  -> _tex_width/_tex_height 和 _video_aspect_ratio 被更新为 proxy 尺寸
  -> _calculate_display_rect() 再按这个错误比例 fit

SourceViewer GPU path
  -> set_shared_texture(tex_id, w, h)
  -> _video_aspect_ratio 又被更新为 GPU texture 尺寸
  -> CPU/GPU 结果交替到达时，显示比例可能在两套尺寸之间跳变
```

结论: Source Viewer 视频变形不只是渲染矩形问题，更像是“媒体显示尺寸”和“纹理/代理尺寸”混用。CPU fallback 目前把 viewer 尺寸当作解码目标，可能把 1920x1080 素材解成接近面板比例的 proxy；随后 `set_texture_from_qimage()` 又把这个 proxy 尺寸写回显示比例。GPU shared texture 到达时又用另一套尺寸更新比例，因此会出现变形或比例跳动。

证据:

- `src/features/edit/ui/source_viewer.py:1512-1523` CPU fallback 用 `viewer.width()/height()` 生成 `target_size`。
- `src/features/edit/ui/source_viewer.py:1526-1539` CPU 解码结果转 QImage 后直接设置为显示纹理。
- `src/features/edit/ui/source_viewer.py:840-848` CPU texture 会用传入 width/height 更新 `_video_aspect_ratio`。
- `src/features/edit/ui/source_viewer.py:856-877` GPU shared texture 也会用 width/height 更新 `_video_aspect_ratio`。
- `src/features/edit/ui/source_viewer.py:779-783` display rect 优先使用 `_tex_width/_tex_height`，因此错误 proxy 尺寸会直接影响显示比例。

### 4. 媒体池拖拽导入崩溃链路

```text
MediaPoolPanel.dropEvent()
  -> _import_files(file_paths)
  -> _do_import_with_progress() / _do_import()
  -> UI 线程循环 _import_single_file(path)
       get_media_type(path)
       probe_media_metadata(path)
       EditProject.import_asset() 返回新 project
       asset_imported.emit()
       project_changed.emit()
  -> FileController._on_media_pool_project_changed()
       timeline_panel.set_project(project)
       timeline_preview_renderer.set_project(project)
  -> 媒体池刷新 + 缩略图请求 + renderer/project 热更新交织
```

结论: 媒体拖到媒体池容易崩溃，核心风险是 dropEvent 在 UI 线程同步探测媒体并逐文件热更新 project；每导入一个文件都会触发 `project_changed`，同时 timeline renderer 立即 `set_project()`。如果此时预览渲染线程、缩略图线程、媒体探测或 timeline UI 正在读旧 project/asset，很容易出现重入、对象生命周期错位、Qt GUI 线程违规或原生解码库崩溃。即使没有立即崩溃，大文件/损坏文件也会卡死 UI。

证据:

- `src/features/edit/ui/media_pool_panel.py:2687-2693` `dropEvent()` 直接同步调用 `_import_files()`。
- `src/features/edit/ui/media_pool_panel.py:2540-2588` 多文件导入在 UI 线程循环执行 `_import_single_file()`，进度对话框也是同一线程。
- `src/features/edit/ui/media_pool_panel.py:2616-2655` 每个文件同步 `probe_media_metadata()`，随后立刻 `asset_imported.emit()` 和 `project_changed.emit()`。
- `src/app/controllers/file_controller.py:698-715` 每次 `project_changed` 都会 `timeline_panel.set_project(project)` 并热更新 `timeline_preview_renderer.set_project(project)`。
- `src/app/controllers/dock_layout_controller.py:307-313` `auto_project_needed` 与 `asset_imported` 都连接到上层控制器，拖拽导入期间会触发跨模块同步。

### 5. Color 页 viewport 视频播放链路

```text
Edit timeline clip -> Color page load_video(for_timeline_clip=True)
  -> EnhancedVideoPlaybackEngine
  -> PlaybackCoordinator
  -> VideoSession
  -> ColorFrameProviderAdapter
  -> VideoFrameProviderService.request_frame()
  -> DecodeWorker / ReadyFrameQueue
  -> EnhancedVideoPlaybackEngine._on_play_tick()
  -> _submit_decoded_frame_playback()
  -> AsyncRenderer.RENDER_VIDEO_FRAME / RENDER_VIDEO_GPU_FRAME
  -> Canvas.drawBackground() 只绘制 current_front_tex_id
```

结论: Color 页不像 Edit Source Viewer 那样明显“每 tick 同步 seek”。它已经有 `PlaybackCoordinator + VideoSession + DecodeWorker + ReadyFrameQueue`，播放 tick 主要从队列非阻塞取帧。但它仍然不能算稳定全 GPU：`ColorFrameProviderAdapter.request_proxy()` 当前调用的是 `VideoFrameProviderService.request_frame()`，不是 `request_gpu_frame()`；普通路径返回 `DecodedFrame` 后，`AsyncRenderer.RENDER_VIDEO_FRAME` 仍会做 planar GPU upload 或 packed RGB CPU upload。只有上游真正交给 `_submit_decoded_frame_playback()` 一个 `GpuDecodedFrame` 时，Color 页才进入 `RENDER_VIDEO_GPU_FRAME` 零拷贝路径。

证据:

- `src/shared/video/video_playback.py:2293-2318` Color 播放架构为 `EnhancedVideoPlaybackEngine -> PlaybackCoordinator -> VideoSession`。
- `src/shared/video/video_playback.py:3847-3853` v3 tick 从 coordinator 取 frame 后提交，不在 tick 中直接 `VideoDecoder.get_frame()`。
- `src/shared/video/color_frame_provider_adapter.py:306-310` proxy 请求调用 `VideoFrameProviderService.request_frame(... quality='proxy')`。
- `src/shared/video/frame_provider_service.py:1222-1224` cache miss 时会同步 `_decode_frame()`。
- `src/shared/video/video_playback.py:1361-1395` 如果输入是 `GpuDecodedFrame` 才提交 `RENDER_VIDEO_GPU_FRAME`。
- `src/features/color/core/async_renderer.py:2963-3039` planar frame 优先 GPU planes upload，失败会 renderer 线程 CPU fallback；packed frame 进入 decoded frame upload。
- `src/features/color/core/async_renderer.py:3049-3108` legacy numpy frame path 会 CPU 预处理后上传。
- `src/features/color/ui/canvas.py:5182-5202` paint 只绘制已完成 front texture，避免 paint 中触发 render loop。

### 6. Composite 页 viewport / 节点视频预览链路

```text
CompositePage._evaluate_graph()
  -> CompositeRenderer render thread
  -> NodeGraph evaluate
       TimelineClipInputNode:
         provider.get_timeline_clip_texture(runtime)
         -> ColorOutput GPU texture
         -> 或 VideoFrameProviderService.request_gpu_frame()
         -> 或 request_frame() + CPU decoded frame upload

       VideoFrameNode:
         VideoContext.request_frame_for_node()
         -> VideoFrameProviderService.request_frame()
         -> DecodedFrame -> RGBA float32 numpy
         -> ctx.texture()/texture.write()
  -> CompositeRenderer.frame_ready(tex_id)
  -> CompositeViewer.set_texture()
       若 shared context 不可读:
         READBACK_PRESENT_ASYNC
         -> render thread FBO readback/PBO
         -> UI thread set_image_data()
         -> glTexImage2D viewer-local upload
```

结论: Composite 页有 GPU 优先设计，但同样不是保证全 GPU。`TimelineClipInputNode` 在上下文共享可用时可能直接拿 ColorOutput 或 zero-copy texture；但 cache miss / provider 失败时会同步请求 CPU frame 并上传。普通 `VideoFrameNode` 更重：它把每帧转换为 RGBA float32 numpy，再 `ctx.texture(... data=...)` 或 `texture.write()`，这在播放预览时会和 Edit 的 CPU fallback 有类似卡顿。Composite viewport 如果读不到 renderer texture，还会走异步 readback + UI 本地 `glTexImage2D`，形成 GPU->CPU->GPU 回路。

证据:

- `src/features/composite/nodes/timeline_clip_input_node.py:119-126` 节点求值时直接调用 provider 获取 timeline clip texture。
- `src/features/edit/core/timeline_clip_frame_provider.py:334-364` 优先请求 `request_gpu_frame()` 并缓存 texture id。
- `src/features/edit/core/timeline_clip_frame_provider.py:368-400` GPU path 失败后调用 `request_frame()`，再上传 decoded frame。
- `src/features/edit/core/timeline_clip_frame_provider.py:599-641` packed decoded frame 会 `data.tobytes()` 创建 ModernGL texture。
- `src/features/composite/nodes/video_frame_node.py:527-545` VideoFrameNode 通过 VideoContext 请求 DecodedFrame 后转换成 RGBA float32。
- `src/features/composite/nodes/video_frame_node.py:862-918` VideoFrameNode 每帧按需 decode/cache，并创建或 write GPU texture。
- `src/app/controllers/composite_controller.py:31-49` Composite frame 无有效 shared texture 时提交 `READBACK_PRESENT_ASYNC`。
- `src/features/composite/render/composite_renderer.py:654-699` async present fallback 做 PBO/direct FBO readback。
- `src/features/composite/ui/composite_viewport.py:729-789` viewer 收到 CPU readback 后 `glTexImage2D` 上传本地 texture。
- `src/app/controllers/file_controller.py:1922-1959` Composite 播放期间按约 10fps 节流重新求值图，可能表现为视频预览不流畅。

### 7. 剪辑/调色/合成融合的最优架构路线

```text
页面职责:
  Edit Page       只编辑 timeline，并播放素材/CompClip 的已就绪帧
  Color Page      只编辑 ColorStackData
  Composite Page  只编辑 CompositeGraphData

项目数据:
  MediaAsset
  TimelineSequence / Clip
  ColorStackData
  CompositeGraphData
  CompClip asset -> composition_id

运行服务:
  PlaybackOrchestrator
  VideoDecodeService
  ColorRenderService
  CompositionRenderService
  RenderCacheManager
  TextureLease/GpuFrameDescriptor
```

结论: “剪辑、调色、合成互相融合”这个产品方向是对的，但实现上不能让页面互相直接调用、互相持有 renderer/texture、切页时自动创建项目数据。最优做法是“页面只编辑数据，后台服务负责渲染，Edit 播放缓存结果”。Composite 页面应该是 `CompositeGraphData` 的编辑器；CompClip 应该是用户明确创建的 timeline asset；Edit 时间线播放 CompClip 时优先读取 Composition 渲染缓存，没有缓存或缓存过期时请求 `CompositionRenderService` 后台渲染。

推荐 CompClip 播放流程:

```text
Edit playhead 命中 CompClip
  -> 根据 composition_id + graph_hash + source_hash + color_stack_hash + frame_range + quality 查询 RenderCacheManager
  -> READY: 把缓存视频/帧序列当普通素材交给统一播放管线
  -> STALE: 允许临时播放旧缓存，同时后台重渲染 dirty range
  -> MISSING: 显示占位/低质量实时帧，并提交后台 render job
  -> RENDERING: 显示进度，可播放已完成的连续 frame range
  -> FAILED: 显示错误帧和失败原因，不阻塞 Edit 播放线程
```

页面切换原则:

1. 切到 Composite 页面只打开编辑器，不自动创建 CompClip，不自动修改 timeline。
2. 从 Edit 选中普通 clip 进入 Composite 时，只显示“可从此片段创建合成”的未绑定状态。
3. 只有用户执行 `Create Comp Clip` / `Create Composition From Selection` / `Insert Composition to Timeline` 时，才创建 `CompositeGraphData`、CompClip asset 或 timeline clip。
4. 已有 CompClip 双击进入 Composite 只编辑它绑定的 `composition_id`，不重复创建。
5. 关闭或切出 Composite 页面不能留下空 graph、空 asset、空 timeline clip。
6. Edit、Color、Composite 之间禁止长期缓存裸 `tex_id`，跨页资源必须通过 `TextureLease/GpuFrameDescriptor` 管理。

这条路线会把当前“页面互相绑架”的复杂度，收敛成稳定的数据流:

```text
用户动作修改项目数据
  -> 标记缓存 dirty
  -> 后台服务渲染缓存
  -> viewport 只显示 ready frame/cache
```

---

## 二、隐藏问题与风险清单

| ID | 优先级 | 区域 | 问题 | 影响 | 关键证据 | 验收标准 |
|---|---|---|---|---|---|---|
| VPLAY-01 | P0 | Source Viewer | GPU provider 异步提交后返回 `None`，CPU fallback 仍立即执行。 | 造成 CPU/GPU 双解码、双提交、GPU 结果覆盖 CPU 帧，素材播放卡顿和轻微闪烁。 | `file_controller.py:1036-1043`, `source_viewer.py:1497-1526` | GPU provider 接管成功时不再进入 CPU fallback；每播放 300 帧，CPU fallback frame count 为 0。 |
| VPLAY-02 | P0 | Source Viewer | `VideoDecoder.get_frame()` 每帧 `FLUSH | ACCURATE` seek。 | H.264/H.265 GOP 视频逐帧播放会反复从关键帧解码到目标帧，普通 H.264 也卡。 | `video_decoder.py:561-602` | 顺序播放时只在 seek/scrub 使用精准 seek；连续播放用顺序 decode worker/queue，30fps 源在 1080p proxy 下不阻塞 UI。 |
| VPLAY-03 | P0 | Timeline | GPU renderer 未 ready 时 `submit()` 返回 False，立刻整帧 CPU fallback。 | 启动阶段或 GL shared context 慢时，播放进入 CPU 热路径，形成抖动/卡顿。 | `edit_gpu_preview_renderer.py:294`, `timeline_preview_renderer.py:663-670` | GPU pipeline 初始化状态显式可见；未 ready 时播放可延迟/降级到代理队列，不同步解码 master。 |
| VPLAY-04 | P0 | Timeline | H.264/H.265 只有 zero-copy 成功才 GPU；zero-copy 探测失败后走 CPU packed RGB。 | “常见 H.264 也卡”的核心原因之一。 | `edit_gpu_decode_manager.py:154-218` | H.264/H.265 路径报告显示 `gpu_zero_copy` 或 `hw_decode_planar_gpu_upload`；CPU packed fallback 必须可见且计数。 |
| VPLAY-05 | P0 | Timeline | 播放 tick 存在双更新入口：`frame_changed` 和 `timer.frame_ready` 都设置 playhead。 | 同一帧可能触发重复 `TimelineView.set_playhead()` 和重复 render request，增大队列压力。 | `timeline_panel.py:868-896`, `edit_playback_controller.py:964-971` | 每个 tick 最多一次 timeline frame request；测试统计 300 tick 内 duplicate request ratio < 1%。 |
| VPLAY-06 | P0 | Timeline | GPU direct path 对任一 layer 失败后整帧 CPU composite。 | 多轨/comp clip/单层失败会把整帧拖回 CPU，且用户不知情。 | `timeline_preview_renderer.py:581-652` | fallback reason 精确到 layer/asset/frame，HUD/日志/报告可查；普通单轨 H.264 不允许静默 CPU fallback。 |
| VPLAY-07 | P0 | 抖动 | 单调帧守卫只天然适合正向播放。反向 JKL、短距离 scrub、seek 后旧帧结果可能被误丢或误放。 | 画面前后帧跳动、反向播放不稳定。 | `timeline_preview_renderer.py:1740-1798`, `2036-2169` | 帧序守卫按 direction/generation/request_id 判定；反向播放 10 秒无旧帧误丢，无回跳。 |
| VPLAY-08 | P0 | GPU texture 生命周期 | GPU output 用 texture id 传给 UI，渲染线程三缓冲复用；UI 没有 fence/ack 机制。 | UI 可能采样到已被下一帧改写的 texture，出现画面抖动或偶现错帧。 | `edit_gpu_preview_renderer.py:641`, `890-924`, `program_viewer.py:919-946` | 引入 slot id + fence/ack 或足够 ring + UI consume ack；渲染端不得覆盖 UI 仍在使用的 slot。 |
| VPLAY-09 | P1 | CPU fallback | CPU path 在 worker 中 numpy 合成、QImage.copy，UI paintGL 再 `glTexImage2D`。 | fallback 可用但太重，播放时每帧仍会 CPU copy + upload。 | `timeline_preview_renderer.py:1374-1428`, `program_viewer.py:565-638` | fallback 仅用于静帧/诊断/不支持素材；播放中启用 proxy 或 GPU planar upload，不用 QImage 热路径。 |
| VPLAY-10 | P1 | Source Viewer | CPU 和 GPU 两个结果可能都显示：先 CPU QImage，再 GPU shared texture。 | 素材预览出现“闪一下/跳一下”的视觉抖动。 | `source_viewer.py:1491-1547`, `source_viewer.py:1270-1284` | 每个 frame 只有一个显示提交策略；GPU pending 时不显示同帧 CPU fallback，或 CPU fallback 标记为临时且同帧覆盖不触发时间码/状态回跳。 |
| VPLAY-11 | P1 | 请求调度 | `TimelinePreviewRenderer.prefetch()` 逐帧调用 `request_render()`，但 `_render_in_flight` 会跳过绝大多数请求。 | 实际预取能力弱，不能建立播放窗口。 | `timeline_preview_renderer.py:1953-1963`, `1770-1779` | 预取使用专门 decode queue，不通过 display render in-flight guard；forward window 可稳定保持 N 帧。 |
| VPLAY-12 | P1 | GPU 合成 | 多轨合成在同一个 FBO 中读写 `_composite_tex`。 | OpenGL 同纹理读写未定义，可能导致多轨时画面抖动/脏帧。 | `edit_gpu_preview_renderer.py:872-884` | 多轨合成使用 ping-pong FBO/texture；GL debug/测试确认无 read/write same texture。 |
| VPLAY-13 | P1 | 诊断 | 没有每帧 pipeline report 证明“本帧无 CPU copy/seek/upload”。 | UI 看起来在 GPU，但隐藏 fallback 无法定位。 | 多处 fallback 只有 debug/warning | 每帧记录 decode_path、render_path、seek_count、cpu_copy_count、qimage_upload_count、texture_slot、fallback_reason。 |
| VPLAY-14 | P1 | 解码共享 | Source Viewer 和 Timeline 各自创建/使用不同 decoder/renderer，可能重复打开同一文件。 | 同一素材在源/时间线同时播放时双倍解码。 | `source_viewer.py:1520`, `timeline_preview_renderer.py:2210` | 同路径共享 `VideoFrameProviderService` / decode session；引用计数和关闭可测。 |
| VPLAY-15 | P0 | Source Viewer 变形 | CPU fallback 用 viewer 控件尺寸创建 `VideoDecoder(target_size)`，把代理帧比例变成面板比例。 | 普通视频在 Source Viewer 播放时被拉伸/压扁；窗口大小不同，变形程度不同。 | `source_viewer.py:1512-1523`, `840-848`, `779-783` | 显示比例只来自媒体 metadata 的 display size/DAR/SAR/rotation，不来自 viewer 尺寸；16:9、9:16、1:1 素材播放比例稳定。 |
| VPLAY-16 | P0 | Source Viewer 变形/抖动 | CPU QImage path 和 GPU shared texture path 使用不同尺寸更新 `_video_aspect_ratio`，同一 frame 可能先后显示两套比例。 | 画面比例跳变、fit 重置、播放中轻微“呼吸式”缩放。 | `source_viewer.py:840-877`, `1497-1539` | 引入统一 `DisplayFrameDescriptor`，区分 `texture_size`、`coded_size`、`display_size`；同一素材播放中 display aspect 不随 CPU/GPU path 改变。 |
| VPLAY-17 | P1 | Source Viewer 变形 | 缺少 sample aspect ratio、display aspect ratio、rotation/matrix 的统一处理。 | 手机竖拍、非方形像素、旋转 metadata 视频容易方向或比例错误。 | 当前 display rect 仅使用 width/height | metadata probe 写入 `display_width/display_height/rotation/pixel_aspect_ratio`；Source/Program Viewer 使用同一显示尺寸算法。 |
| VPLAY-18 | P0 | 媒体池拖拽崩溃 | `dropEvent()` 在 UI 线程同步导入和探测媒体。 | 大文件、损坏文件、网络盘文件会冻结 UI；原生 probe 卡住时可能被用户操作触发崩溃。 | `media_pool_panel.py:2687-2693`, `2540-2588`, `2616-2634` | 拖拽后 UI 线程只创建 import job；metadata probe 在 worker 中执行，可取消、可超时、错误逐文件隔离。 |
| VPLAY-19 | P0 | 媒体池拖拽崩溃 | 每导入一个文件都 emit `project_changed`，并立即热更新 timeline renderer project。 | 批量拖拽会形成 project 更新风暴；renderer/thumbnail/UI 可能同时读写不同 project 快照。 | `media_pool_panel.py:2647-2655`, `file_controller.py:698-715` | 批量导入只在 batch commit 后 emit 一次 `project_changed`；renderer 在安全点切换 project，播放中先 pause/epoch bump/cancel in-flight。 |
| VPLAY-20 | P0 | 媒体池缩略图/拖拽 | 缩略图请求可能在刷新、paint、拖拽预览期间启动，与导入和 project 热更新叠加。 | QPixmap/QImage 线程边界、解码器并发和对象销毁时序可能导致崩溃或随机空图。 | `media_pool_panel.py` 中 model/delegate/drag preview 会触发 thumbnail provider | 缩略图只在主线程创建 QPixmap；worker 只产出 QImage/bytes；project 切换时可取消旧请求；paint 中不直接启动无界任务。 |
| VPLAY-21 | P1 | 媒体池拖拽重入 | 无 project 时 `_import_files()` emit `auto_project_needed` 后递归调用自身。 | 如果项目创建触发 UI 刷新/信号链，导入路径存在重入风险。 | `media_pool_panel.py:2489-2497`, `dock_layout_controller.py:307-309` | 改为显式 `ensure_project()` 返回 project；增加 `_import_in_progress` guard；递归调用计数为 0。 |
| VPLAY-22 | P1 | 媒体池输入健壮性 | drop 的路径只按扩展名过滤，缺少路径规范化、目录/包/不存在文件/权限/超大文件/损坏文件的统一策略。 | 异常文件可进入 probe，导致长时间阻塞或底层库异常。 | `media_pool_panel.py:2523-2538`, `2689-2692` | drop 前规范化去重；目录和不可读文件提前跳过；probe 有 timeout；每个失败文件独立记录，不影响 batch。 |
| VPLAY-23 | P1 | Color viewport | Color 播放 tick 基本非阻塞，但共享帧服务 cache miss 时 `request_frame()` 会同步 decode；如果该调用落在 UI/渲染热路径，会复现卡顿。 | seek、首帧、cache miss、worker 未预热时会突然卡住。 | `color_frame_provider_adapter.py:306-310`, `frame_provider_service.py:1222-1224`, `video_playback.py:3008-3077` | 播放态 tick 只从 ReadyFrameQueue 取帧；cache miss 只能触发 async prefetch，不能同步 decode；trace 记录 cache miss 线程。 |
| VPLAY-24 | P0 | Color GPU 路径 | ColorFrameProviderAdapter 请求的是 CPU `DecodedFrame`，没有优先调用 `request_gpu_frame()`；H.264/H.265 可能默认变成 CPU decode + GPU upload。 | Color 页普通 H.264 仍可能比预期卡，且不是全 GPU。 | `color_frame_provider_adapter.py:306-310`, `video_playback.py:1361-1460`, `async_renderer.py:2963-3108` | Color H.264/H.265 trace 显示 `GpuDecodedFrame` 或 `hw_decode_planar_gpu_upload`；packed RGB CPU upload 必须有 fallback reason。 |
| VPLAY-25 | P1 | Color 首帧/seek | 首帧和高质量 seek 仍有 blocking 获取和 `engine.set_image()` CPU 图像路径。 | 切到 Color、seek、暂停取高质量帧时会卡 UI 或触发完整 image pipeline。 | `video_playback.py:3008-3107`, `3271-3304`, `file_controller.py:1895-1920` | 首帧显示用 placeholder + async first frame；seek 只更新 generation，不在 UI 等 master frame。 |
| VPLAY-26 | P1 | Color 操作期间播放 | 部分图层/蒙版操作仍直接访问 `video_controller.decoder.get_frame()`。 | 播放中创建层/蒙版可能同步 seek/decode，造成停顿或旧帧状态错乱。 | `src/app/controllers/layer_controller.py` 中 `decoder.get_frame(...)` | 图层/蒙版取帧统一走 VideoSession/FrameProviderService，播放态禁止旧 decoder 同步取帧。 |
| VPLAY-27 | P0 | Composite TimelineClipInput | 节点求值时同步调用 `TimelineClipFrameProvider.get_timeline_clip_texture()`；GPU miss 后同步 `request_frame()` 并上传。 | 合成图每帧求值可能被解码和 CPU upload 卡住，预览播放抖动。 | `timeline_clip_input_node.py:119-126`, `timeline_clip_frame_provider.py:334-400` | TimelineClipInput 有独立 prefetch/texture queue；render thread 求值只消费 ready texture，不同步 decode。 |
| VPLAY-28 | P0 | Composite VideoFrameNode | 每帧转换 RGBA float32 numpy，再 `ctx.texture(... data=...)` / `texture.write()`。 | 普通视频节点播放极重，4K/H.264 会明显卡顿。 | `video_frame_node.py:527-545`, `862-918` | VideoFrameNode 改用共享 GPU frame/planar upload；播放态禁止每帧 RGBA float32 CPU upload。 |
| VPLAY-29 | P0 | Composite viewport | shared texture 不可访问时，走 async readback + UI `glTexImage2D` 本地上传。 | 预览看似异步但仍是 GPU->CPU->GPU，播放时带宽和延迟都高。 | `composite_controller.py:31-49`, `composite_renderer.py:654-699`, `composite_viewport.py:729-789` | context sharing 必须启动时探测并显式报告；播放态禁止静默 readback present，或降级到低分辨率 proxy + HUD 标记。 |
| VPLAY-30 | P1 | Composite 播放节奏 | Composite 播放期间图求值被节流到约 10fps。 | 用户会感觉合成页 viewport 视频播放不流畅，即使解码本身不卡。 | `file_controller.py:1922-1959` | 用 latest-frame render scheduler 替代固定 10fps 节流；轻图可接近源 fps，重图明确显示降级 fps。 |
| VPLAY-31 | P0 | 跨页纹理生命周期 | Composite provider 可能缓存外部 GPU frame 的裸 texture id，没有统一 retain/release/ack 描述符。 | 外部 decoder/renderer 复用或释放 texture 后，Composite 可能采样错帧/黑帧/抖动。 | `timeline_clip_frame_provider.py:340-364`, `composite_viewport.py:685-710` | 跨页传递 `GpuFrameDescriptor(texture_id, owner, fence, generation, release_cb)`；消费者 ack 前不得释放/复用。 |
| VPLAY-32 | P1 | Composite 缩略图/回读 | 合成页已有播放时挂起缩略图逻辑，但需要验证所有 readback/thumbnail 入口都遵守。 | 播放时 thumbnail/readback 与主预览抢 GPU/PCIe，造成卡顿。 | `composite_page.py:1236-1238`, `2027-2045`, `2055-2065` | 播放态主预览优先；thumbnail/readback 计数为 0 或低优先级可取消。 |
| VPLAY-33 | P0 | 架构边界 | 切换到 Composite 页面如果自动创建 CompClip/graph/timeline clip，会把“打开页面”变成“修改项目数据”。 | 空 comp、重复 comp、撤销历史混乱、项目脏状态、切页卡顿。 | 当前 Composite/Edit 入口需要审计所有自动创建路径 | 页面切换 project diff 为空；只有明确用户命令才创建 Composition/CompClip/timeline clip。 |
| VPLAY-34 | P0 | CompClip 生命周期 | CompClip 不是普通页面状态，必须资产化并绑定 `composition_id`。 | Edit/Composite 状态互相依赖，页面关闭后引用失效或丢内容。 | CompClip 与 CompositeGraphData 绑定逻辑分散在 controller | `CompClipAsset(composition_id, source_refs, cache_policy)` 成为唯一入口；重复打开不重复创建。 |
| VPLAY-35 | P0 | Edit 播放 CompClip | Edit 目前可能实时依赖 Composite graph/provider/render thread，而不是播放已渲染缓存。 | 时间线播放被合成求值拖慢；切页后容易卡死或黑帧。 | `TimelineClipInputNode`、`TimelineClipFrameProvider`、Composite controller 热更新路径 | Edit 命中 CompClip 时只查 `RenderCacheManager`；无缓存只提交 render job，不同步求值合成图。 |
| VPLAY-36 | P0 | Composition 缓存状态 | 缺少统一缓存状态机: READY/STALE/MISSING/RENDERING/FAILED。 | UI 不知道该播旧缓存、占位、实时预览还是等待；容易出现隐藏阻塞。 | 当前 fallback 分散在 provider/renderer/controller | 每个 CompClip frame range 都能查询 cache state、dirty reason、job id、progress、error。 |
| VPLAY-37 | P0 | Composition 后台渲染服务 | 页面对象承担渲染职责，而不是独立 `CompositionRenderService`。 | 关闭/切换页面会影响 Edit 播放；后台缓存渲染难以排队、取消、恢复。 | CompositePage/CompositeRenderer 与 Edit controller 互相调用 | Composition 渲染由服务按 job 执行；CompositePage 只是提交 graph 变更和显示 job 状态。 |
| VPLAY-38 | P1 | 缓存失效粒度 | graph、source、ColorStack、format、proxy/full quality 的 hash 未形成统一 cache key。 | 缓存错用旧内容，或改一点参数就全量重渲染。 | 缓存 key/dirty range 需要全局设计 | cache key 包含 composition_id、graph_hash、source_hash、color_stack_hash、frame_range、quality、format；支持 dirty range。 |
| VPLAY-39 | P1 | 用户工作流 | 创建 Composition、创建 CompClip、插入时间线三个动作混在一起。 | 用户只是切页或查看就产生素材；后续项目越来越脏。 | 需要审计 Composite 入口和菜单/右键动作 | UI 命令拆分为 `New Composition`、`Create Comp Clip From Selection`、`Insert Composition To Timeline`，每个命令有 undo/redo。 |
| VPLAY-40 | P1 | 导出一致性 | 导出时如果直接依赖当前页面 cache/viewport，结果可能与时间线播放不一致。 | 导出错帧、漏更新、使用低质量 proxy。 | Deliver/export 需要读取同一 Composition cache contract | 导出强制要求 full-quality cache READY，或由离线 CompositionRenderService 渲染缺失范围。 |

---

## 三、优化实施计划表

| 阶段 | 任务 | 涉及文件 | 实施步骤 | 验证方法 | 完成标准 |
|---|---|---|---|---|---|
| 0 | 加入播放链路诊断基线 | `src/features/edit/core/timeline_preview_renderer.py`, `src/features/edit/core/edit_gpu_decode_manager.py`, `src/features/edit/ui/source_viewer.py`, `src/shared/media/performance_monitor.py` 或新增诊断模块 | 1. 新增 `EditPlaybackFrameTrace` 数据结构。2. 每帧记录 source/timeline、asset、timeline_frame、source_frame、decode_path、render_path、fallback_reason、sync_seek_count、cpu_to_ndarray、qimage_copy、gl_tex_upload、gpu_texture_slot。3. 增加最近 N 帧 ring buffer 和 JSON 导出接口。 | 写单元测试构造 gpu/planar/cpu 三种结果；运行一次 300 帧播放导出 trace。 | 能从日志或 JSON 明确回答“这一帧是否全 GPU”。 |
| 1 | 修复 Source Viewer GPU/CPU 双路径 | `src/features/edit/ui/source_viewer.py`, `src/app/controllers/file_controller.py` | 1. 修改 GPU provider 协议，返回 `GpuRequestStatus(submitted=True/False, immediate_texture=None)`。2. `submitted=True` 时不执行 CPU fallback，显示上一帧或 loading 状态。3. GPU 失败/超时才进入 CPU fallback。4. 为 seek/scrub 保留精确 CPU fallback，但播放态禁用同步 fallback。 | mock provider 返回 submitted，断言 `_video_decoder.get_frame()` 未调用；真实 H.264 播放 300 帧，CPU fallback 计数为 0。 | Source Viewer 播放不再同步每帧 CPU decode。 |
| 2 | Source Viewer 改为异步顺序解码/预读 | `src/features/edit/ui/source_viewer.py`, `src/shared/video/decode_worker.py`, `src/shared/video/frame_queue.py` | 1. 引入 source playback session。2. play 时启动 decode worker，按当前帧顺序预读 `current+1..+N`。3. QTimer 只消费 ready queue，不直接解码。4. seek/scrub 时 increment generation 并 flush old queue。 | 用 H.264 GOP 测试文件统计 seek 调用次数；连续播放 300 帧时 seek 只发生在开始/用户 seek。 | UI 线程不出现 `VideoDecoder.get_frame()` 热路径；队列平均等待低于 1 帧。 |
| 3 | 时间线播放请求去重 | `src/features/edit/core/edit_playback_controller.py`, `src/features/edit/ui/timeline_panel.py`, `src/features/edit/ui/edit_page.py` | 1. 选择单一 tick 输出入口，避免 `frame_changed` 和 `frame_ready` 双写 playhead。2. `TimelineView.set_playhead()` 增加 source/reason 或 no-emit 参数。3. EditPage 对同一 frame 的连续请求做 last-frame 去重。 | 单元测试模拟 100 tick，统计 `timeline_frame_requested` 次数等于有效 frame 变化次数。 | 每 tick 最多一个 render request。 |
| 4 | H.264/H.265 GPU decode 强化 | `src/features/edit/core/edit_gpu_decode_manager.py`, `src/shared/media/factory.py`, `src/shared/media/macos_vt/*`, `src/shared/media/decode_policy.py` | 1. 启动时真实 probe: decode sample frame + IOSurface bind + GL sample。2. macOS 对 H.264/H.265 优先 `MacOSVtZeroCopySource`。3. zero-copy 失败时尝试硬解到 NV12/P010 + GPU upload，不直接 packed RGB。4. 失败原因写入 trace。 | 使用 1080p/4K H.264、H.265 样片生成 pipeline report。 | 普通 H.264/H.265 报告不再静默 `cpu_packed_rgb`。 |
| 5 | Timeline 使用 decode queue 而不是逐帧同步 request | `src/features/edit/core/timeline_preview_renderer.py`, `src/features/edit/core/edit_gpu_decode_manager.py`, `src/shared/video/decode_worker.py`, `src/shared/video/frame_queue.py` | 1. 为当前 timeline frame plan 建立 clip-level decode request。2. 播放态请求 proxy/GPU queue，暂停/scrub 请求 master/exact。3. queue 允许 latest-before-or-at 帧追时钟，避免等慢帧卡住 UI。4. 所有 sync seek 只在 worker 内发生。 | 测试播放态调用 `FFmpegMediaSource.read_frame` guard 不触发；trace 中 UI thread sync decode 为 0。 | 时间线播放不阻塞在同步解码。 |
| 6 | GPU renderer 初始化与 fallback 策略 | `src/app/controllers/file_controller.py`, `src/features/edit/core/timeline_preview_renderer.py`, `src/features/edit/core/edit_gpu_preview_renderer.py` | 1. GPU renderer 未 initialized 时返回 `pending_gpu_renderer`，不立刻 CPU master fallback。2. 如果用户开始播放而 GPU 未 ready，使用低分辨率 proxy queue 或延迟首帧。3. GPU init 失败显示一次明确状态，不每帧 warning。 | mock renderer initialized 延迟 2 秒，播放时无每帧 CPU packed fallback。 | GPU 未 ready 不造成隐藏 CPU 热路径。 |
| 7 | 帧序守卫按播放方向重写 | `src/features/edit/core/timeline_preview_renderer.py`, `src/features/edit/core/edit_playback_controller.py` | 1. 每个请求携带 `request_id`、`generation_id`、`direction`、`playback_epoch`。2. 正向只接受 >= expected 或 latest-before-or-at；反向只接受 <= expected。3. seek/scrub 创建新 epoch，旧 epoch 结果全部丢弃。4. 缓存命中也走同一判定。 | 测试正向、反向、JKL 切换、短距离 scrub；断言 emitted frame 序列无非法回跳。 | 反向播放和 seek 后不再误用单调正向逻辑。 |
| 8 | GPU texture slot 生命周期 | `src/features/edit/core/edit_gpu_preview_renderer.py`, `src/features/edit/ui/program_viewer.py`, `src/features/edit/ui/source_viewer.py` | 1. `frame_ready` 从 `(tex_id,w,h)` 扩展为 descriptor: `texture_id,width,height,slot_id,frame,generation,fence`。2. Viewer paintGL 后发出 consumed/ack。3. Renderer ring buffer 只复用 ack 后 slot；无 ack 时扩大 ring 或丢帧。4. 不缓存裸 tex_id。 | 压测 60fps 10 秒，记录 slot reuse distance 和 UI ack；无同 slot 未 ack 复用。 | 消除 texture 被覆盖导致的错帧/抖动。 |
| 9 | 修复 GPU 多轨 ping-pong 合成 | `src/features/edit/core/edit_gpu_preview_renderer.py` | 1. 为 composite 使用两个 FBO 交替读写。2. 第一层 copy 到 ping，后续 overlay 从 ping 读、pong 写，再交换。3. 单轨可直接 copy/convert 到 output slot。 | GL debug test 或单元 mock 检查不会同时 bind 同一 texture 为 input/output；多轨测试画面稳定。 | 多轨 GPU 合成无 undefined read/write。 |
| 10 | 降低 CPU fallback 成本 | `src/features/edit/core/timeline_preview_renderer.py`, `src/features/edit/ui/program_viewer.py`, `src/features/edit/ui/source_viewer.py` | 1. fallback 播放态优先 proxy 分辨率。2. QImage path 只用于暂停/错误/不支持素材。3. CPU fallback 若必须显示，复用 texture storage，优先 `glTexSubImage2D`。4. 记录每帧 upload bytes。 | 关闭 GPU 强制 fallback，比较优化前后 CPU/frame time 和 upload bytes。 | fallback 可用但不会假装全 GPU；播放态成本可控。 |
| 11 | 统一 Source/Timeline 解码会话 | `src/shared/video/frame_provider_service.py`, `src/features/edit/ui/source_viewer.py`, `src/features/edit/core/timeline_preview_renderer.py`, `src/app/controllers/file_controller.py` | 1. Source Viewer 使用共享 `VideoFrameProviderService` 或新的 EditPlaybackMediaService。2. 同路径同质量共享 source、proxy、cache、GPU decode manager。3. 做 ref_count 生命周期清理。 | 打开同一素材到 Source 和 Timeline，检查只有一个 decode source/session。 | 同素材不重复打开解码器。 |
| 12 | UI 显示状态与严格模式 | `src/shared/ui/playback_hud.py`, `src/features/edit/ui/program_viewer.py`, `src/features/edit/ui/source_viewer.py` | 1. 增加 Debug HUD: GPU/CPU path、fallback reason、decode ms、render ms、dropped frames。2. 增加 strict GPU playback 开关。3. strict 模式下 H.264/H.265 若 CPU fallback 直接报错或暂停。 | 手工打开 HUD，跑 H.264/H.265/ProRes/comp clip；检查状态准确。 | 用户能看见隐藏 fallback，不再靠猜。 |
| 13 | 回归测试与基准脚本 | `tests/edit/`, `tests/media/`, `tools/timeline_preview.py` 或新增 `tools/benchmark_edit_playback.py` | 1. 增加 fake decoder/renderer 单元测试。2. 增加真实媒体 E2E benchmark 脚本。3. 输出 JSON: avg/p95 frame latency、dropped、fallback counts、texture slot violations。 | 本地运行测试矩阵；CI 可跑 fake，真实媒体脚本手动/夜间跑。 | 每次优化有数字对比，不靠主观感受。 |
| 14 | 修复 Source Viewer 显示尺寸契约 | `src/features/edit/ui/source_viewer.py`, `src/features/edit/core/project.py`, `src/shared/media/*` | 1. 在 metadata 中保存 `coded_size`、`natural_size`、`display_size`、`pixel_aspect_ratio`、`rotation`。2. Source Viewer 打开素材时固定 `display_size`。3. CPU proxy 解码目标按原始显示比例等比缩放，不直接使用 viewer 宽高比例。4. `_calculate_display_rect()` 使用 display size，纹理尺寸只用于采样。 | 16:9、9:16、1:1、带 rotation、非方形像素视频逐个打开并截图比较。 | Source Viewer 播放、暂停、resize、CPU/GPU path 切换均不变形。 |
| 15 | Source Viewer 统一帧描述符 | `src/features/edit/ui/source_viewer.py`, `src/app/controllers/file_controller.py`, `src/features/edit/core/timeline_preview_renderer.py` | 1. 新增 `DisplayFrameDescriptor(texture_id/qimage, texture_size, display_size, source_frame, generation, origin, color_range)`。2. CPU/GPU path 都提交 descriptor。3. `set_texture()` 和 `set_shared_texture()` 不再直接修改媒体显示比例。4. 只有素材切换或 metadata 变化时 reset fit。 | mock CPU 后 GPU 同帧到达，断言 display aspect 和 zoom 不变化。 | 消除 CPU/GPU 尺寸振荡导致的变形和缩放跳动。 |
| 16 | 媒体池异步导入 Worker | `src/features/edit/ui/media_pool_panel.py`, `src/features/edit/core/project.py`, `src/shared/media/*`, `tests/edit/` | 1. 新增 `MediaImportJob/MediaImportWorker`。2. dropEvent 只收集规范化路径并启动 job。3. metadata probe 在 worker 线程执行，带取消、timeout、逐文件错误结果。4. UI 用信号更新进度，不能在 worker 创建 QPixmap/QWidget。 | 拖拽 1/10/100 个文件、损坏文件、网络盘路径；用 Qt signal spy 确认 UI 不阻塞。 | 拖拽导入期间主线程可响应，单文件失败不崩溃。 |
| 17 | 媒体池批量提交 project 变更 | `src/features/edit/ui/media_pool_panel.py`, `src/app/controllers/file_controller.py`, `src/features/edit/core/timeline_preview_renderer.py` | 1. Worker 返回 `ImportedAssetDraft` 列表。2. UI 线程在 batch 末尾一次性调用 project import/merge。3. 只 emit 一次 `project_changed(batch_id)`。4. renderer 收到 project update 时 bump playback epoch、取消旧请求、在安全点替换 project。 | 批量导入 50 个文件时 `project_changed` 次数为 1；播放中导入不会崩溃或错帧。 | 消除导入信号风暴和 renderer 热更新竞态。 |
| 18 | 缩略图与拖拽导入稳定性 | `src/features/edit/ui/media_pool_panel.py`, `src/features/edit/ui/thumbnail_provider.py` 或实际 provider 文件, `tests/edit/` | 1. 审计 thumbnail provider 线程模型。2. 禁止 paint/drag preview 中启动无界 probe；改为队列化、限流、可取消。3. worker 只返回 QImage/bytes，QPixmap 只在 UI 线程创建。4. project/batch 切换时取消旧 asset_id/path 请求。 | 快速拖拽、滚动媒体池、同时导入大批素材；开启 Qt fatal warnings 验证无跨线程 GUI 调用。 | 缩略图加载不再与导入/刷新互相踩踏。 |
| 19 | Color 页接入统一播放 trace | `src/shared/video/video_playback.py`, `src/shared/video/color_frame_provider_adapter.py`, `src/features/color/core/async_renderer.py`, `src/features/color/ui/canvas.py` | 1. 将阶段 0 的 `EditPlaybackFrameTrace` 扩展为 `ViewportPlaybackFrameTrace(page=color)`。2. 记录 `coordinator_tick_ms`、queue hit/miss、decode_path、render_frame_path、cpu_upload、legacy_numpy_path、front_tex_id。3. HUD 显示 Color 当前是否 GPU/CPU。 | Color 页播放 300 帧导出 trace。 | 能明确回答 Color viewport 每帧是否全 GPU。 |
| 20 | Color 优先使用 GPU frame provider | `src/shared/video/color_frame_provider_adapter.py`, `src/shared/video/session.py`, `src/shared/video/decode_worker.py`, `src/shared/video/video_playback.py` | 1. 在 adapter 增加 `request_gpu_proxy()`，优先调用 `VideoFrameProviderService.request_gpu_frame()`。2. DecodeWorker/PlaybackCoordinator 能传递 `GpuDecodedFrame`。3. `_submit_decoded_frame_playback()` 对 GPU frame 走 `RENDER_VIDEO_GPU_FRAME`。4. CPU fallback 写 trace reason。 | H.264/H.265 样片在 Color 页播放 300 帧。 | 普通 H.264/H.265 不再默认 CPU DecodedFrame upload。 |
| 21 | Color 首帧/seek 非阻塞化 | `src/shared/video/video_playback.py`, `src/shared/video/session.py`, `src/app/controllers/file_controller.py` | 1. 首帧显示 placeholder，不在 UI 等 master/proxy。2. seek 只 bump generation 并请求 async frame。3. 暂停高质量帧通过 worker 完成后替换。4. 移除播放态 `engine.set_image()` 热路径。 | 切入 Color、拖动 seek、暂停/播放反复 50 次。 | UI 无 100ms 以上阻塞；不触发完整 image pipeline storm。 |
| 22 | Composite TimelineClipInput 预取队列 | `src/features/composite/nodes/timeline_clip_input_node.py`, `src/features/edit/core/timeline_clip_frame_provider.py`, `src/features/composite/render/prefetch_scheduler.py` | 1. TimelineClipInput 求值只取 ready texture。2. provider 增加 async prefetch/source texture queue。3. GPU/CPU fallback 都先在 worker/render-prepass 准备，不在节点求值同步解码。4. cache key 加 generation/timeline_id/clip_id/frame。 | 合成页 timeline clip 播放 30 秒，统计 render thread sync decode。 | render thread node evaluate 不同步 decode，不出现旧帧 cache 回放。 |
| 23 | Composite VideoFrameNode GPU 化 | `src/features/composite/nodes/video_frame_node.py`, `src/features/composite/core/video_context.py`, `src/shared/video/frame_provider_service.py` | 1. VideoFrameNode 优先请求 GPU frame/planar frame。2. 支持 TextureResult descriptor 直接引用 GPU texture。3. CPU RGBA float32 upload 仅作暂停/不支持 fallback。4. 限制 `_frame_cache` 内存并记录 upload bytes。 | VideoFrameNode 播放 1080p/4K H.264。 | 播放态不再每帧 RGBA float32 `texture.write()`。 |
| 24 | Composite shared context / readback fallback 治理 | `src/features/composite/ui/composite_viewport.py`, `src/features/composite/render/composite_renderer.py`, `src/app/controllers/composite_controller.py` | 1. 启动时探测 renderer->viewer context sharing。2. 失败时 HUD 明确显示 readback present。3. 播放态 readback 走低分辨率 proxy/PBO，禁止无提示 full-res direct readback。4. 记录 GPU->CPU->GPU bytes。 | 人为禁用 context sharing，播放 30 秒。 | fallback 可见且可控，不静默高带宽回读。 |
| 25 | 跨页面 GPU texture 生命周期描述符 | `src/shared/video/`, `src/features/edit/core/timeline_clip_frame_provider.py`, `src/features/composite/render/composite_renderer.py`, `src/features/composite/ui/composite_viewport.py`, `src/features/color/core/color_output.py` | 1. 定义 `GpuFrameDescriptor`/`TextureLease`。2. 包含 owner、slot、generation、fence、release/ack。3. ColorOutput、TimelineClipInput、CompositeViewer 都传 descriptor，不传裸 tex_id。4. 加泄漏和复用检测。 | 压测 Color->Composite 页面切换、播放、seek。 | 无未 ack 复用、无裸外部 texture id cache。 |
| 26 | Composite 播放调度器 | `src/app/controllers/file_controller.py`, `src/features/composite/ui/composite_page.py`, `src/features/composite/render/render_scheduler.py` | 1. 用 latest-frame scheduler 替代固定 10fps 节流。2. 根据图复杂度和 render time 自适应目标 fps。3. 新 frame 到来时取消/合并旧 graph request。4. HUD 显示 source fps、composite fps、dropped/coalesced。 | 轻图/重图各播放 30 秒。 | 轻图接近源 fps；重图稳定降级但不积压、不旧帧回跳。 |
| 27 | 定义 Composition/CompClip 数据边界 | `src/features/edit/core/composite_graph_data.py`, `src/features/edit/core/project.py`, `src/features/composite/core/graph_snapshot.py`, `src/features/composite/core/graph_migration.py` | 1. 明确 `CompositeGraphData` 是项目级数据。2. 定义 `CompClipAsset` 只引用 `composition_id` 和 source refs。3. timeline clip 只引用 CompClip asset，不持有页面状态。4. 增加 migration/validation。 | 构造项目: 普通 clip、独立 composition、CompClip timeline clip。 | 保存/加载后引用稳定；无 CompositePage 对象参与数据恢复。 |
| 28 | 页面切换无副作用 | `src/app/controllers/page_controller.py`, `src/app/controllers/composite_controller.py`, `src/features/composite/ui/composite_page.py` | 1. 审计进入 Composite 的所有路径。2. 移除切页自动创建 CompClip/timeline clip 的逻辑。3. 切页只设置 active context 和 viewer state。4. 增加 project snapshot diff guard。 | 在无选中 clip、选中普通 clip、选中 CompClip 三种状态切 Composite。 | 仅切页时 project diff 为空，undo stack 不新增命令。 |
| 29 | 明确创建合成的用户命令 | `src/features/edit/ui/timeline_view.py`, `src/features/edit/ui/timeline_panel.py`, `src/features/composite/ui/composite_page.py`, `src/app/controllers/composite_controller.py` | 1. 增加 `Create Comp Clip From Selection`。2. 增加 `New Composition`。3. 增加 `Insert Composition To Timeline`。4. 每个命令走 undo/redo command，不在页面 activate 中执行。 | UI 操作测试三条命令；撤销/重做验证。 | 用户明确动作才修改 project/timeline。 |
| 30 | Composition Render Cache Manager | `src/features/composite/render/cache_manager.py`, `src/features/composite/render/composite_export_renderer.py`, `src/features/edit/core/timeline_preview_renderer.py`, `src/features/edit/core/project.py` | 1. 定义 cache key: composition_id、graph_hash、source_hash、color_stack_hash、frame_range、quality、format。2. 定义状态 READY/STALE/MISSING/RENDERING/FAILED。3. 支持 proxy/full 两级缓存和 dirty range。4. 提供查询 API 给 Edit timeline。 | 单测 cache key/dirty range/status transition。 | Edit 可在不打开 Composite 页面时查询 CompClip 缓存状态。 |
| 31 | CompositionRenderService 后台渲染队列 | `src/features/composite/render/composite_graph_render_service.py`, `src/features/composite/render/composite_frame_renderer.py`, `src/features/composite/render/render_scheduler.py` 或新增服务模块 | 1. 新增 render job: composition_id、frame_range、quality、priority。2. 支持取消、合并、暂停、恢复。3. 输出缓存文件/帧序列和 manifest。4. 失败记录 error 和可重试信息。 | 提交 3 个重叠 dirty range job，验证合并和取消。 | 缓存渲染不依赖 CompositePage 是否打开。 |
| 32 | Edit 播放 CompClip 只读缓存 | `src/features/edit/core/timeline_preview_renderer.py`, `src/features/edit/core/timeline_v2.py`, `src/features/edit/ui/program_viewer.py` | 1. TimelinePreviewRenderer 命中 CompClip 时先查 cache。2. READY 直接作为普通媒体/帧序列播放。3. STALE 播旧缓存并提交后台 job。4. MISSING/RENDERING 显示占位或已完成范围，不同步求值 graph。 | 播放含 CompClip 的 timeline 30 秒，CompositePage 保持关闭。 | Edit 播放不调用 CompositePage/CompositeRenderer 页面对象。 |
| 33 | 缓存文件格式策略 | `src/features/composite/render/`, `src/shared/export/video_encoder.py`, `src/shared/media/factory.py` | 1. proxy cache 使用易解码格式或帧序列。2. full cache 使用高质量中间格式。3. manifest 记录 color space、bit depth、alpha、fps、frame range。4. 缓存可被统一解码服务读取。 | 生成 proxy/full cache 并在 Edit 播放/导出读取。 | CompClip 缓存像普通素材一样进入统一播放管线。 |
| 34 | 导出与缓存一致性 | `src/features/deliver/`, `src/features/composite/export_provider.py`, `src/features/composite/render/composite_export_renderer.py` | 1. 导出前检查所有 CompClip full cache。2. 缺失/过期则离线渲染缺失范围。3. 禁止导出使用 viewport/readback proxy。4. 输出 cache usage report。 | 导出含 stale/missing CompClip 的 timeline。 | 导出结果与 full-quality Composition render 一致。 |

---

## 四、建议执行顺序

| 批次 | 目标 | 包含任务 | 为什么先做 |
|---|---|---|---|
| Batch A | 先证明问题 | 阶段 0、12、13 的最小版本 | 没有 trace，后续 AI 很容易“看起来优化了”但仍隐藏 CPU fallback。 |
| Batch B | 立刻缓解素材播放卡顿和变形 | 阶段 1、2、14、15 | Source Viewer 目前最明确地在 UI 线程同步解码，并且显示尺寸契约已经混用。 |
| Batch C | 稳定时间线播放节奏 | 阶段 3、5、7 | 解决重复请求、同步解码和异步旧帧抖动。 |
| Batch D | 打通真正 GPU 热路径 | 阶段 4、6、8、9 | 让 H.264/H.265 和 GPU texture 生命周期真正可靠。 |
| Batch E | 降级与共享优化 | 阶段 10、11 | 保证复杂素材/comp clip 也不会把系统拖垮。 |
| Batch F | 稳定媒体池拖拽导入 | 阶段 16、17、18 | 拖拽崩溃属于数据导入和渲染热更新竞态，应独立收敛并压测。 |
| Batch G | 覆盖 Color viewport | 阶段 19、20、21 | Color 已有较好的队列架构，但需要证明 H.264 是否真进 GPU 路径。 |
| Batch H | 覆盖 Composite viewport | 阶段 22、23、24、25、26 | Composite 牵涉节点求值、跨上下文 texture 和 readback fallback，需要单独压测。 |
| Batch I | 收敛架构边界 | 阶段 27、28、29 | 先阻止“切页修改项目数据”，否则后续缓存服务会被脏状态拖垮。 |
| Batch J | CompClip 缓存化播放 | 阶段 30、31、32、33、34 | 让 Edit 把 CompClip 当缓存素材播放，Composite 只负责产出缓存。 |

---

## 五、验证矩阵

| 场景 | 素材 | 操作 | 必看指标 | 通过条件 |
|---|---|---|---|---|
| Source-H264-1080p | 8-bit H.264 MP4 | Source Viewer 播放 300 帧 | UI sync decode、CPU fallback、avg frame latency | GPU 接管时 UI sync decode = 0；无连续卡顿。 |
| Source-H264-4K | 4K H.264 MP4 | Source Viewer 播放 300 帧 | dropped frames、fallback reason | 不出现每帧精准 seek；允许丢帧追时钟。 |
| Timeline-H264-single | 单轨 H.264 | Program Viewer 播放 30 秒 | decode_path、render_path、texture slot reuse | H.264 不静默 CPU packed fallback；slot 无未 ack 复用。 |
| Timeline-H264-multi | 双轨 H.264 + opacity | 播放 10 秒 | ping-pong 合成、错帧/抖动 | 无同纹理读写；画面不闪回旧帧。 |
| Reverse-JKL | 任意 H.264 | J/K/L 正反切换 | emitted frame sequence | 正向非递减，反向非递增，seek epoch 清晰。 |
| Scrub-small | H.264 长 GOP | 在 20 帧范围来回拖动 | stale result drops、latest frame | 不显示旧 epoch 帧；不把小回退误判成乱序。 |
| GPU-init-delay | mock renderer 延迟 ready | 启动后立即播放 | CPU fallback count | GPU 未 ready 不触发每帧 CPU master fallback。 |
| CPU-forced | 关闭 GPU | 播放 10 秒 | proxy usage、QImage upload bytes | 明确显示 CPU/proxy fallback，帧率可接受，不卡 UI。 |
| CompClip | composite-bound clip | 播放 10 秒 | fallback reason | 明确标记 composite CPU bridge，不误报全 GPU。 |
| Source-aspect-basic | 16:9、9:16、1:1 H.264 | Source Viewer 播放、暂停、resize | display_size、texture_size、截图比例 | 不拉伸；resize 不改变素材显示比例。 |
| Source-aspect-metadata | rotation/SAR/DAR 测试素材 | Source Viewer 与 Program Viewer 对照 | rotation、pixel_aspect_ratio、display_rect | 两个 viewer 显示方向和比例一致。 |
| MediaPool-drop-small | 1-3 个普通视频 | 拖到媒体池 | UI blocked ms、project_changed count | UI 不冻结；project_changed 每 batch 1 次。 |
| MediaPool-drop-batch | 50 个混合视频/图片/音频 | 拖到媒体池，导入中滚动媒体池 | import errors、thumbnail cancel、renderer epoch | 无崩溃；失败文件隔离；缩略图请求可取消。 |
| MediaPool-drop-bad | 损坏文件、不可读文件、目录、重复路径 | 拖到媒体池 | probe timeout、error list | 不崩溃；不导入无效项；错误可见。 |
| MediaPool-drop-playing | 时间线播放中拖入素材 | 保持播放 30 秒并导入 | renderer project swap、in-flight cancel | 无错帧/崩溃；renderer 在新 epoch 安全切换。 |
| Color-H264-1080p | Edit timeline clip H.264 | 从 Edit 进入 Color 播放 300 帧 | queue hit、decode_path、render_frame_path、cpu_upload | 播放 tick 无同步 decode；H.264 GPU/planar path 可见。 |
| Color-seek-scrub | H.264 长 GOP | Color 页频繁 seek/scrub | generation、blocking wait、engine.set_image count | UI 不阻塞；旧 generation 帧不显示。 |
| Color-Composite-switch | 同一 timeline clip | Color/Composite 来回切换并播放 | ColorOutput texture lease、shared context、stale texture | 无黑帧/旧帧/裸 tex id 失效。 |
| Composite-TimelineClipInput | timeline-backed comp graph | Composite 页播放 30 秒 | provider path、sync decode in render thread、texture lease | TimelineClipInput 不同步 decode；GPU miss 可见。 |
| Composite-VideoFrameNode | 普通 VideoFrameNode H.264 | 节点 frame_number 动画播放 | RGBA float upload bytes、texture.write count | 播放态不走每帧 float32 CPU upload。 |
| Composite-context-fail | 禁用/破坏 context sharing | Composite 播放 30 秒 | readback_present bytes、PBO/direct readback | fallback 明确显示并限流；UI 不冻结。 |
| Composite-heavy-graph | 多节点重图 | 播放 30 秒 | composite fps、coalesced requests、stale drops | 不积压旧请求；降级 fps 稳定且可见。 |
| PageSwitch-no-side-effect | 未选 clip/普通 clip/CompClip | Edit/Color/Composite 来回切换 50 次 | project diff、undo count、created assets | 仅切页不创建 graph/asset/clip，undo stack 不变。 |
| CreateCompClip-explicit | 选中普通 timeline clip | 执行 Create Comp Clip From Selection | composition_id、CompClip asset、undo command | 只在明确命令后创建，撤销可完整移除。 |
| CompClip-cache-ready | 含 READY 缓存的 CompClip | Edit 播放 30 秒，Composite 页面关闭 | cache hit、CompositePage calls、decode path | Edit 只读缓存，不调用 CompositePage。 |
| CompClip-cache-missing | 含 MISSING 缓存的 CompClip | playhead 移入片段 | render job、placeholder、UI blocked ms | 提交后台 job；Edit 不阻塞；状态可见。 |
| CompClip-cache-stale | 修改合成节点后回 Edit | 播放旧缓存并后台重渲染 | stale reason、dirty range、job progress | 旧缓存可临时播放，新缓存完成后自动切换。 |
| CompClip-export | 含 missing/stale CompClip timeline | 执行导出 | full cache readiness、offline render job | 导出前补齐 full cache，不使用 viewport proxy。 |

---

## 六、交给 AI 执行时的 Prompt 建议

```text
请按 docs/edit_page_video_playback_decode_render_chain_audit_optimization_plan_2026-06-05.md 执行 Batch A，然后按顺序处理 Batch B、Batch F、Batch G、Batch H、Batch I、Batch J。

约束:
- 遵守 .clinerules: PyQt 信号防递归、OpenGL/ModernGL 不在 UI 线程调用、所有新函数加类型注解。
- 不改无关 UI 样式。
- 不隐藏 CPU fallback；所有 fallback 必须写入 frame trace。

输出:
1. 修改文件列表
2. 新增/更新测试
3. 一份 300 帧播放 trace 示例
4. 说明哪些场景仍非全 GPU
5. Source Viewer 变形截图对照和媒体池拖拽导入压测结果
6. Color/Composite viewport 的 300 帧 pipeline trace 和跨页 texture 生命周期报告
7. 页面切换无副作用测试结果，以及 CompClip 缓存状态机/后台渲染 job 报告
```

---

## 七、最终 Definition of Done

普通 H.264/H.265 时间线播放完成时，最近 300 帧 trace 至少能给出如下结论:

```json
{
  "page": "edit_timeline",
  "codec": "h264",
  "decode_path": "macos_vt_iosurface_zero_copy",
  "render_path": "gpu_yuv_to_rgb_composite",
  "ui_thread_sync_decode_frames": 0,
  "cpu_to_ndarray_frames": 0,
  "qimage_upload_frames": 0,
  "gl_tex_image_upload_frames": 0,
  "texture_slot_reuse_without_ack": 0,
  "stale_frame_emitted": 0,
  "fallback_frames": 0
}
```

对于不支持全 GPU 的素材或 comp clip，必须明确报告:

```json
{
  "fallback_frames": 300,
  "fallback_reason": "composite_bound_clip_requires_composite_renderer_cpu_bridge",
  "user_visible": true,
  "strict_gpu_mode_result": "blocked_or_paused"
}
```

对于 CompClip，Edit 播放必须优先满足如下缓存契约:

```json
{
  "page": "edit_timeline",
  "clip_type": "comp_clip",
  "composition_id": "comp_...",
  "cache_state": "READY",
  "cache_quality": "proxy_or_full",
  "edit_depends_on_composite_page": false,
  "composite_page_calls_during_playback": 0,
  "ui_thread_composite_eval_frames": 0,
  "missing_cache_action": "enqueue_composition_render_job",
  "page_switch_project_mutations": 0
}
```
