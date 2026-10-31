# Topos Color 时间线总优化大纲

日期：2026-06-05  
目标：把 Edit 页时间线从“能放片段”提升到“主流剪辑软件可用”的稳定形态。优化顺序必须先审计播放链路和隐藏 BUG，再补齐显示、交互、音频、范围播放/导出、剪辑工具和验收体系。

本文档面向后续 AI 或开发者执行。执行时不要一次性重写时间线，应按阶段小步提交，每阶段结束必须运行对应测试并做视觉验收。

---

## 1. 当前代码边界

### 1.1 重点模块

| 模块 | 当前职责 | 优化关注点 |
|---|---|---|
| `src/features/edit/ui/timeline_panel.py` | 时间线工具栏、播放控制信号、`EditPlaybackController` 封装、快捷键入口 | 与 Program Viewer 传输栏、时间线播放头、音频播放状态统一 |
| `src/features/edit/ui/timeline_view.py` | QGraphicsView 主体、时间线绘制、轨道头、clip item、拖放、吸附、命令执行 | 坐标统一、交互稳定、复制粘贴、Alt 拖拽复制、范围条、工具验收 |
| `src/features/edit/ui/timeline_items.py` | `ClipItem`、`TrackHeaderItem`、`TimecodeRulerItem`、`PlayheadItem` 等绘制 | clip 外观、clip 颜色、波形绘制、播放头、刻度条、轨道头 |
| `src/features/edit/ui/timeline_layout.py` | 时间线坐标和 V/A 分区布局 | 所有 x/y 坐标唯一来源、滚动条范围、轨道可视区域 |
| `src/features/edit/core/timeline_v2.py` | 时间线数据模型 | clip 标签色、range/in-out、音频元数据、复制后的新 ID 语义 |
| `src/features/edit/core/commands.py` | Undo/Redo 命令体系 | 新增复制/粘贴/重复/设置 clip 颜色/范围条/链接解除等命令 |
| `src/features/edit/core/timeline_ops.py` | 纯函数编辑操作和 `SnapManager` | 播放头、clip、trim、drop、range 边界都走统一吸附 |
| `src/features/edit/core/edit_playback_controller.py` | 视频时间线播放调度 | 音频主时钟接入、范围播放、循环范围、掉帧策略 |
| `src/features/edit/core/audio_playback.py` | 音频时钟、解码、混音、输出 | 真正接入 Edit 页播放，支持 M/S/volume、AV sync |
| `src/features/edit/core/audio_waveform.py` | 波形抽取和缓存 | 在 `ClipItem` 音频片段内绘制 source range 波形 |
| `src/features/edit/core/timeline_thumbnail_scheduler.py` | 时间线缩略图调度 | zoom-aware、viewport-aware、播放时降载 |
| `src/features/edit/core/timeline_render_plan.py` | 当前帧渲染计划 | 检查多轨、转场、合成、调色、混合模式和导出一致性 |
| `src/features/edit/core/timeline_preview_renderer.py` | 时间线预览帧渲染 | 播放性能、缓存策略、错误回退 |
| `src/features/edit/core/timeline_export.py` | 时间线导出 | 导出范围、音频混音、视频/音频时长一致 |
| `src/features/edit/ui/edit_page.py` | Edit 页协调器 | 播放、seek、frame request、Program Viewer、Timeline Panel 的信号闭环 |
| `src/features/edit/ui/program_viewer.py` | 预览窗口和传输控制栏 | 播放/暂停状态、timecode、音频状态反馈 |

### 1.2 现有测试入口

| 测试 | 覆盖方向 |
|---|---|
| `tests/edit/test_timeline_command_regressions.py` | 命令栈、Undo/Redo、剪辑和轨道命令回归 |
| `tests/edit/test_timeline_ops.py` | 纯时间线操作 |
| `tests/edit/test_timeline_hit_and_drop.py` | hit-test、拖放目标、V/A 分区 |
| `tests/edit/test_timeline_view_layout.py` | 布局和视图行为，当前环境可能受 `rawpy` 依赖影响 |
| `tests/edit/test_timeline_p7_regression.py` | linked A/V、轨道目标、V/A 布局、防错拖放 |
| `tests/edit/test_timeline_thumbnail_scheduler.py` | 缩略图调度 |
| `tests/edit/test_audio_waveform.py` | 波形缓存和抽取 |
| `tests/edit/test_audio_playback.py` | 音频时钟、混音、播放管理 |
| `tests/edit/test_timeline_export.py` | 时间线导出 |
| `tests/edit/test_edit_page_playback_preview.py` | Edit 页播放预览链路 |
| `tests/benchmark/test_playback_performance.py` | 播放性能基线 |

---

## 2. 总体实施原则

1. 先审计链路，再改功能。时间线播放、音频、导出共享同一时间映射，不能 UI 先画一个假状态。
2. 坐标只从 `TimelineLayoutEngine` 来。禁止在 `ClipItem`、`TrackHeaderItem`、`TimelineView` 多处重复手算 track y、ruler x、splitter y。
3. 所有可撤销编辑必须走 `CommandStack` 和 `_execute_command()`。不要直接改 `TimelineSequence` 后全量刷新。
4. `paint()` 只绘制，不能解码、请求缩略图、修改 scene 坐标或发信号。
5. 缩略图、波形、音频解码、导出都要有取消过期请求和缓存上限。
6. 每个新功能至少包含核心单元测试、UI 行为测试或截图验收中的一种。
7. 先做主流剪辑软件最基础功能，再做高级功能。复制、粘贴、Alt 拖拽复制、音频播放、波形、范围播放优先级高于复杂特效 UI。

---

## 3. 阶段总览

| 阶段 | 名称 | 优先级 | 目标 | 完成标志 |
|---|---|---:|---|---|
| P0 | 播放链路和隐藏 BUG 审计 | 最高 | 确认 Source 预览、Timeline 播放、seek、预览、导出、音频、UI 状态闭环是否可靠 | 输出审计报告并修复 P0/P1 崩溃、蓝屏、卡顿和数据错乱 |
| P1 | 时间线视觉和坐标稳定 | 最高 | 刻度、timecode、播放头、轨道头、clip 边界、滚动条稳定 | 大量轨道滚动、拖 splitter、缩放都不丢 ruler/playhead/clip |
| P2 | 基础剪辑功能补齐 | 最高 | 复制、粘贴、Alt 拖拽复制、clip 颜色、启用禁用、链接解除 | 剪辑软件常用操作都可用并可 Undo/Redo |
| P3 | 音频播放和波形 | 最高 | 播放有声音，音频轨显示波形，M/S/volume 生效 | 视频播放时有同步音频，波形随 zoom 和 source range 正确 |
| P4 | 范围条和播放/导出范围 | 高 | In/Out 范围、range bar、loop range、export range | 播放和导出可选择整条时间线或范围 |
| P5 | 工具完整性验收 | 高 | Select/Trim/Blade/Ripple/Roll/Slip/Slide/Hand/Zoom 全部验收 | 每个工具有测试矩阵和已知限制 |
| P6 | 性能和大项目稳定 | 高 | 100 clip、20 track、4K/6K 素材下仍可交互 | 有 benchmark 数字和性能阈值 |
| P7 | 主流 NLE 功能扩展 | 中 | 标记、转场、淡入淡出、速度、轨道管理、代理/缓存、吸附增强 | 达到可长期剪辑的功能面 |
| P8 | 自动化验收和文档 | 中 | 截图、像素、行为、性能、导出一致性形成防回归体系 | CI 或本地脚本一键跑关键用例 |

---

## 4. P0：播放链路和隐藏 BUG 审计

### 4.1 视频播放链路审计

#### 目标

确认从用户点击播放，到播放头推进，到当前帧渲染，到 Program Viewer 显示，再到暂停/seek/循环/导出的链路是同一套时间语义。

#### 需要梳理的链路

```text
TimelinePanel / ProgramViewer transport
  -> EditPlaybackController
  -> TimelinePanel.frame_changed / seek_requested
  -> EditPage._on_playhead_changed / _emit_frame_request
  -> timeline_render_plan / timeline_preview_renderer
  -> ProgramViewer.set_texture / set_shared_texture
  -> UI timecode + timeline playhead + preview frame
```

#### 检查项

| 编号 | 检查项 | 实施方法 | 验证方法 |
|---|---|---|---|
| P0-PLAY-01 | 播放按钮来源是否唯一 | 检查 `TimelinePanel` 和 `ProgramViewer.TransportControlBar` 是否重复维护播放状态 | 点击两个播放入口，播放按钮状态、播放头、timecode 不冲突 |
| P0-PLAY-02 | playhead frame 是否是唯一当前帧 | 搜索 `_playhead_frame`、`current_frame`、`seek_to_frame`，确认 UI 和 controller 不各自维护不同真相 | 播放 5 秒后暂停，Timeline/Program Viewer/timecode 显示同一帧 |
| P0-PLAY-03 | seek 是否会请求正确帧 | 检查 `EditPage._on_seek_requested()`、`_request_frame_at_playhead()` | 拖动播放头到 clip 中间，预览显示对应素材帧 |
| P0-PLAY-04 | 空白时间线和空白帧处理 | 当前帧无 video clip 时，Program Viewer 应清屏或显示透明/黑帧 | 播放头放在 gap，预览不显示上一帧残影 |
| P0-PLAY-05 | 多轨叠加优先级 | 检查 `timeline_render_plan` 取顶层 clip、转场和 composite 的规则 | V2 覆盖 V1 时预览显示 V2 |
| P0-PLAY-06 | 变速 clip 时间映射 | 检查 `ClipInstance.timeline_to_source_frame()` 和 render plan 是否一致 | 0.5x/2x clip 播放帧与导出帧一致 |
| P0-PLAY-07 | 混合 FPS 素材 | 检查 `_source_pts_index`、`timeline_frame_mapper` 的使用 | 24/25/30/60fps 混放不跳错帧 |
| P0-PLAY-08 | 播放时缩略图降载 | `TimelineView.set_playback_active_for_thumbnails()` 应暂停低优先级缩略图 | 播放时不因缩略图任务卡顿 |
| P0-PLAY-09 | 播放结束和循环 | 检查 `end_reached`、loop、range loop | 到末尾后按设置停止或循环，不越界 |
| P0-PLAY-10 | JKL 状态 | J/K/L 对 controller speed/direction/timecode 是否一致 | 连按 L 加速，K 停，J 倒放，UI 状态正确 |

#### 建议新增审计脚本

新建 `tools/audit_edit_playback_chain.py`，输出 JSON：

```json
{
  "timeline_id": "sample",
  "fps": 24.0,
  "frames_checked": [0, 24, 48, 72],
  "preview_frame_matches_plan": true,
  "blank_gap_clears_viewer": true,
  "audio_available": false,
  "max_seek_latency_ms": 35.0,
  "warnings": []
}
```

#### 必跑测试

```bash
python3 -m pytest tests/edit/test_edit_page_playback_preview.py -q
python3 -m pytest tests/edit/test_timeline_frame_mapper.py -q
python3 -m pytest tests/edit/test_mixed_fps_playback_cadence.py -q
python3 -m pytest tests/benchmark/test_playback_performance.py -q
```

### 4.2 Source 素材预览窗口播放链路审计

#### 当前症状

用户反馈 Edit 页 Source 素材预览窗口播放视频时偶发蓝屏，并且播放非常卡顿。当前代码中 Source 预览由 `src/features/edit/ui/source_viewer.py::SourceViewerWidget` 自己维护简化播放链路，而不是复用完整时间线播放调度：

```text
SourceViewerWidget.play()
  -> QTimer.timeout
  -> _on_playback_tick()
  -> _current_frame += 1
  -> _load_frame(frame)
  -> GPU provider async submit 或 CPU VideoDecoder.get_frame()
  -> SourceViewer.set_shared_texture() 或 set_texture_from_qimage_with_display_size()
  -> SourceViewer.paintGL()
```

#### 初步诊断

| 编号 | 可疑点 | 代码入口 | 为什么可能导致蓝屏/卡顿 | 优先级 |
|---|---|---|---|---|
| P0-SRC-01 | 播放 tick 在 UI 线程同步取帧 | `SourceViewerWidget._on_playback_tick()` -> `_load_frame()` | CPU fallback 每帧 `VideoDecoder.get_frame()`，如果内部准确 seek 或解码慢，会直接阻塞 UI | P0 |
| P0-SRC-02 | GPU 异步提交后没有超时 fallback | `_load_video_frame()` 中 GPU provider 返回 `{'submitted': True}` 后直接 `return` | 如果 GPU render 失败、丢帧、纹理无效或回调被 stale guard 丢弃，当前帧没有 CPU 兜底，画面可能停留旧帧、清空或出现蓝屏/异常色 | P0 |
| P0-SRC-03 | generation 固定为 0 | `file_controller.py::_setup_source_gpu_provider()` 创建 render task 时 `generation_id=0` | 快速 seek/播放时缺少请求世代控制，只靠 frame guard；同一 frame 的旧任务、新任务难区分 | P0 |
| P0-SRC-04 | GPU 结果只按 frame 判断 stale | `SourceViewerWidget.set_gpu_frame(frame=...)` | 换素材后如果 frame 号相同，旧素材 GPU 结果可能覆盖新素材画面；应同时检查 asset/session/generation | P0 |
| P0-SRC-05 | 纹理生命周期不清楚 | `SourceViewer.set_shared_texture()`、`EditGpuPreviewRenderer.frame_ready` | shared texture 由渲染线程拥有，SourceViewer 只保存 tex id；如果渲染线程复用/释放，`glIsTexture` 失败后会清纹理，表现可能像蓝屏/黑屏 | P0 |
| P0-SRC-06 | CPU fallback 逐帧准确 seek | `_load_video_frame()` 注释写 `VideoDecoder.get_frame does FLUSH|ACCURATE seek` | 播放时每帧 seek 是最慢路径，H.264/H.265 GOP 素材会严重卡顿 | P0 |
| P0-SRC-07 | cache 太小且淘汰策略不适合播放 | `_frame_cache` 默认 10，淘汰 `min(keys)` | 顺序播放勉强可用，来回 scrub 或倒退容易全量重解码；不是 LRU，也没有 forward prefetch | P1 |
| P0-SRC-08 | GPU/CPU 双路径可能交错 | GPU submitted 时跳过 CPU，GPU unavailable 时同步 CPU | 不同帧可能一帧 GPU 一帧 CPU，颜色空间、尺寸、方向、时序都可能抖动 | P1 |
| P0-SRC-09 | Source 播放没有音频时钟 | `SourceViewerWidget` 只有 QTimer | 只按固定 interval 加 1 帧，不按 monotonic clock 追赶，解码慢时累计变慢，不丢帧追实时 | P1 |
| P0-SRC-10 | 蓝屏缺少可观测诊断 | 缺 Source 专用状态 overlay 和 trace 面板 | 用户只看到蓝屏，不知道是 GPU render、decode、texture、cache、颜色空间哪个失败 | P1 |

#### 立即止血方案

| 任务 | 实现方法 | 验收 |
|---|---|---|
| Source GPU 超时 fallback | GPU submit 后启动单帧 watchdog，例如 80-120ms；若目标 frame/generation 未显示，自动 CPU proxy fallback 或显示 last good frame | GPU 失败时不蓝屏，不清空画面 |
| last good frame 策略 | SourceViewer 保留最后一帧有效纹理/QImage；错误帧只显示轻量 overlay，不主动清空画面 | 解码偶发失败时预览保持上一帧并显示提示 |
| asset + generation guard | `_gpu_expected_frame` 扩展为 `(asset_id/path, frame, generation)`；换素材、seek、关闭 source 时递增 generation | 快速切素材不会串帧 |
| 禁止播放时准确 seek | 播放模式下使用顺序解码/预读队列，只有 pause/scrub 才走精确 seek/master | H.264 顺序播放不再每帧 seek |
| 播放卡顿时丢帧追时钟 | 使用 monotonic clock 计算目标 frame，落后超过 2-3 帧直接跳到目标 frame；UI timer 不简单 `+=1` | 解码慢时播放时间不越来越慢 |
| Source 专用 trace | 扩展 `EditPlaybackFrameTrace(page='edit_source')`，记录 `gpu_submitted/gpu_ready/gpu_timeout/cpu_fallback/decode_ms/upload_ms/displayed_frame` | 日志能定位蓝屏原因 |

#### 长期重构方案

| 任务 | 实现方法 | 验收 |
|---|---|---|
| 统一 Source 和 Timeline 的播放调度 | SourceViewer 使用 `EditPlaybackController` 或抽出 `SourcePlaybackController`，同样支持 monotonic clock、drop frame、pause exact | Source 播放和时间线播放手感一致 |
| SourceFramePipeline | 新增 `src/features/edit/core/source_preview_pipeline.py`，把 decode queue、GPU render submit、CPU fallback、cache、trace 从 UI 中移出 | `source_viewer.py` 不再直接解码每帧 |
| 后台预读队列 | 播放时预读当前 frame 后 6-12 帧 proxy；seek 时取消旧任务，优先当前帧 | 连续播放 cache hit 提升，UI 不阻塞 |
| 代理优先 | Source 播放默认 proxy/disk proxy，暂停/逐帧切 master 或高质量 | 4K/6K 源素材预览可用 |
| 单一路径显示协议 | GPU path 和 CPU path 统一返回 `DisplayFrameDescriptor(asset_id, frame, generation, tex/qimage, color_info, display_size)` | 显示层不关心解码来源 |
| 纹理生命周期托管 | shared texture 使用明确 owner/ref/release 协议；SourceViewer 不持有已释放 tex id | `glIsTexture` 不再频繁失败 |
| 错误状态 overlay | SourceViewer 增加 `decode_error/render_error/gpu_timeout/using_cpu_fallback` 小提示 | 用户看到原因，不只是蓝屏 |

#### 建议新增测试和工具

| 文件/工具 | 覆盖 |
|---|---|
| `tests/edit/test_source_viewer_playback_chain.py` | GPU submitted 后超时 fallback、generation guard、last good frame |
| `tests/edit/test_source_viewer_drop_frame_clock.py` | 解码慢时按 monotonic clock 丢帧追赶 |
| `tests/benchmark/test_source_viewer_playback_performance.py` | 1080p/4K H.264 Source 播放 FPS、decode_ms、upload_ms |
| `tools/benchmark_source_viewer_playback.py` | 打开素材、播放 10 秒、输出 dropped/displayed/cache/gpu_timeout JSON |

建议 benchmark 输出：

```json
{
  "page": "edit_source",
  "asset": "sample_4k_h264.mov",
  "duration_s": 10.0,
  "target_fps": 24.0,
  "displayed_fps": 23.4,
  "dropped_frames": 8,
  "gpu_submitted": 230,
  "gpu_timeouts": 0,
  "cpu_fallbacks": 3,
  "avg_decode_ms": 7.8,
  "p95_upload_ms": 3.2,
  "blue_or_blank_frames": 0
}
```

#### 必跑测试

```bash
python3 -m pytest tests/edit/test_edit_page_playback_preview.py -q
python3 -m pytest tests/video/test_video_playback_flags.py -q
python3 -m pytest tests/media/test_video_playback_decoded_submit.py -q
python3 -m pytest tests/media/test_video_playback_no_ui_copy.py -q
python3 -m pytest tests/benchmark/test_playback_performance.py -q
```

### 4.3 音频播放链路审计

#### 当前症状

用户反馈视频播放没有声音。代码中已经存在 `audio_playback.py`、`AudioPlaybackManager`、`AudioVideoSynchronizer`、`audio_waveform.py`，但需要确认是否真正接入 Edit 页播放和 timeline clip。

#### 需要梳理的链路

```text
TimelineSequence.audio_tracks
  -> audio ClipInstance source range
  -> MediaAsset file path / audio stream metadata
  -> AudioDecoder
  -> AudioMixer
  -> AudioOutput(QAudioSink)
  -> AudioClock
  -> video controller follows audio clock
```

#### 检查项

| 编号 | 检查项 | 实施方法 | 验收 |
|---|---|---|---|
| P0-AUDIO-01 | `AudioPlaybackManager` 是否实例化 | 搜索 EditPage/TimelinePanel 是否创建并连接 audio manager | 播放按钮触发 audio manager `play()` |
| P0-AUDIO-02 | 音频 clip 是否绑定实际文件 | 从 `asset_id` 查 `MediaAsset.path`，确认 linked audio clip 使用同一素材 source range | A1 clip 能定位到媒体文件和音频 stream |
| P0-AUDIO-03 | M/S/volume 是否传入 mixer | 轨道头按钮和 `SetTrackMuteCommand/SetTrackSoloCommand/SetTrackVolumeCommand` 变化后同步到 audio manager | M 静音，S 独奏，音量滑块有效 |
| P0-AUDIO-04 | seek 是否同步音频 | 拖播放头时 audio manager `seek(frame)` | seek 后播放声音从正确位置开始 |
| P0-AUDIO-05 | 播放暂停状态 | pause/stop 是否关闭 QAudioSink 或停止写入 | 停止后没有残留声音 |
| P0-AUDIO-06 | AV sync | 默认音频主时钟，视频追音频 | 连续播放 60 秒，音画误差小于 50ms |
| P0-AUDIO-07 | 无音频设备/无 PyAV 回退 | 缺依赖或无音频设备不应崩溃 | UI 显示静音/不可用状态，日志有原因 |

#### 必跑测试

```bash
python3 -m pytest tests/edit/test_audio_playback.py -q
python3 -m pytest tests/edit/test_effect_registry_audio.py -q
```

### 4.3 导出链路审计

#### 目标

导出必须与播放预览使用同一时间映射和同一 clip/range 规则。后续添加导出范围条后，导出不能仍然默认全 timeline。

#### 检查项

| 编号 | 检查项 | 实施方法 | 验收 |
|---|---|---|---|
| P0-EXP-01 | video frame provider 与 preview plan 一致 | 对比 `timeline_export.py`、`timeline_clip_frame_provider.py`、`timeline_render_plan.py` | 同一帧预览和导出取同一 source frame |
| P0-EXP-02 | gap/disabled clip/hidden track | 明确导出黑帧、跳过 disabled clip、隐藏轨是否生效 | 导出文件帧序符合 UI |
| P0-EXP-03 | 音频导出 | 音频混音、M/S/volume、range 是否进入导出 | 导出视频含音频且时长一致 |
| P0-EXP-04 | 转场/合成/调色 | composite/color/render cache 是否预览和导出一致 | 用含转场/调色 clip 导出抽帧对比 |
| P0-EXP-05 | Range export | 后续 P4 范围条完成后，导出可选 entire timeline / in-out range | 导出帧数等于范围长度 |

#### 必跑测试

```bash
python3 -m pytest tests/edit/test_timeline_export.py -q
python3 -m pytest tests/edit/test_comp_clip_timeline_export.py -q
python3 -m pytest tests/project/test_export_frame_provider.py -q
```

### 4.4 隐藏 BUG 审计清单

| 编号 | 风险点 | 重点文件 | 修复原则 | 验收 |
|---|---|---|---|---|
| P0-BUG-01 | splitter 拖动后 V/A clip 跑错区域 | `timeline_view.py`、`timeline_layout.py` | splitter 改变时保留 V1 靠近分割线，clip 裁剪区域刷新 | 拖 splitter 20 次，视频不进入音频区 |
| P0-BUG-02 | 垂直滚动后 ruler/timecode/playhead 消失 | `timeline_view.py` | ruler/playhead 固定在 viewport 顶部或 overlay 层 | 20 轨上下滚动，顶部刻度仍可见 |
| P0-BUG-03 | painter clip 泄漏导致 item 不显示 | `timeline_items.py`、`timeline_view.py` | 每个 paint 使用 save/restore，慎用 `DontSavePainterState` | 开启 clip 裁剪后仍显示所有 item |
| P0-BUG-04 | clip 右侧出现粗黑线 | `ClipItem.paint()` | clip 内容和边界分离，separator 固定 1px | 相邻 clip 边界清晰但不粗黑 |
| P0-BUG-05 | 无时间线时出现无意义滚动条 | `timeline_view.py` | 无 timeline 时隐藏 split scrollbar 并重置 geometry | 新建项目未建 timeline 时不出现左侧细滚动条 |
| P0-BUG-06 | 音频 clip 被识别成 video 或反过来 | `ClipItem._is_audio_track()`、layout hit-test | 优先按 track model 类型判断，不能只看 asset media_type | video/audio 轨移动和绘制正确 |
| P0-BUG-07 | Alt/Shift/Ctrl 修饰键冲突 | `TimelineView.keyPressEvent()`、mouse drag | 定义完整 modifier 语义表 | Alt 拖拽复制不影响 Shift 禁用吸附 |
| P0-BUG-08 | 命令绕过 `_execute_command()` | `timeline_view.py`、`commands.py` | 所有编辑统一发 `timeline_changed` 和支持 undo | 删除/移动/转场/marker 都可 Undo |
| P0-BUG-09 | linked A/V 操作不原子 | `commands.py`、`TimelineView` | video/audio linked move/copy/delete/split 用 MacroCommand | linked clip 一起动，一起撤销 |
| P0-BUG-10 | 全量 reload 闪烁和状态丢失 | `timeline_view.py` | 可增量更新则不全量 rebuild；保留 selection/scroll/zoom | 大量 clip 编辑不闪烁 |
| P0-BUG-11 | drop target 错区 | `timeline_layout.py` | audio drop 到 video 区应跳到 audio target，反之同理 | 拖素材到错误区域不会生成错类型 clip |
| P0-BUG-12 | subclip source range 丢失 | `timeline_view.py::_add_asset_as_clip()` | subclip 使用 metadata source_start/source_duration | 场景检测 subclip 拖入播放正确片段 |
| P0-BUG-13 | 轨道锁定未阻止所有编辑 | hit-test、drop、trim、delete | locked track 禁止 move/trim/drop/delete | 锁轨后所有工具不可破坏 clip |
| P0-BUG-14 | 选区和右键菜单不同步 | selection/context menu | 右键未选中 clip 时先选中该 clip | 右键操作作用对象符合用户预期 |
| P0-BUG-15 | 缩放后 hit-test 偏移 | layout engine | x_to_frame/frame_to_x 互逆，包含 header offset | 不同 zoom 下点击切点命中同一帧 |

---

## 5. P1：时间线视觉和坐标稳定

### 5.1 时间刻度条和 timecode

| 任务 | 实现方法 | 验证 |
|---|---|---|
| 左上 timecode 与 ruler 对齐 | 左上 timecode 固定在 track header 列顶部；ruler 从 `TRACK_HEADER_WIDTH` 开始 | 截图检查 timecode 右边界和 ruler `00:00` 左边界重合 |
| ruler 固定不随纵向滚动消失 | 优先改为 viewport overlay 或 pinned scene item，保证 z-value 高于轨道 | 多轨滚动时仍显示 ruler 和播放头头部 |
| 动态刻度 LOD | 目标主刻度间距 90 到 140px，副刻度 8 到 18px，根据 fps/ppf 选 step | zoom 0.1x 到 50x 不拥挤也不空 |
| SMPTE 格式 | 支持 `HH:MM:SS:FF`，项目起始 timecode 可配置 | 24/25/30/60fps 下 frame 转 timecode 正确 |
| grid 与 ruler 对齐 | grid 主线使用同一个 frame step 和 `frame_to_x()` | clip 边界、grid、ruler、playhead 同像素对齐 |
| 播放头吸附 | ruler click/drag、playhead drag 使用 `SnapManager`，吸附 clip start/end、marker、range in/out | 开启吸附时拖到切点附近自动停在切点 |

### 5.2 轨道头

| 任务 | 实现方法 | 验证 |
|---|---|---|
| DaVinci 风格信息层级 | badge、轨道名、clip count、控制按钮分两行排布 | V1/A1、视频 1/音频 1、片段数、按钮不重叠 |
| 视频/音频分区滚动 | 视频轨和音频轨独立纵向滚动条，轨道头和内容同步移动 | 20 条视频轨、20 条音频轨可分别浏览 |
| scrollbar 视觉统一 | split scrollbar 与底部 scrollbar 使用同一宽度、颜色、hover | 不出现系统原生突兀滑块 |
| 无 overflow 隐藏滚动条 | 轨道数量少或无 timeline 时隐藏 | 空项目无滚动条 |
| 轨道按钮功能核对 | lock、target、visibility、mute、solo、volume 全部接命令或明确禁用 | 点击按钮后 UI、model、播放/导出同步 |

### 5.3 Clip 外观

| 任务 | 实现方法 | 验证 |
|---|---|---|
| 片段分割清晰 | 每个 clip 内容内缩 1px，左右边界固定 1px separator，选中态单独画 | 相邻同色 clip 切点清楚但无粗黑线 |
| Clip 标签栏 | 视频蓝、音频绿；label bar 固定高度；文件名省略；速度标签右对齐 | 放大/缩小文字不溢出 |
| Clip 颜色标签 | `ClipInstance` 新增或 metadata 保存 `label_color`，右键菜单设置 | 片段可设置不同颜色并可 Undo |
| Disabled/muted 状态 | disabled clip 降透明或斜纹；muted audio 降透明 | 状态一眼可见 |
| Thumbnail LOD | 小宽度不画缩略图，中宽度画少量，大宽度按目标尺寸重采样 | 放大后不马赛克 |
| Audio waveform | 音频 clip 内显示 source range 波形 | 放大可看细节，缩小仍可看峰值 |

---

## 6. P2：基础剪辑功能补齐

### 6.1 复制、粘贴、重复

#### 功能要求

| 功能 | 快捷键/交互 | 行为 |
|---|---|---|
| 复制 clip | `Ctrl+C` | 复制当前选中 clip、linked A/V、转场可后续支持 |
| 粘贴 clip | `Ctrl+V` | 粘贴到播放头位置，优先目标轨；保持相对时间偏移 |
| 剪切 clip | `Ctrl+X` | 复制后删除原 clip，可 Undo |
| 原地重复 | `Ctrl+D` | 在选区末尾后方复制一份 |
| Alt 拖拽复制 | 按住 `Alt` 拖动已选 clip | 拖动预览为复制，松手后原 clip 保留，新 clip 落点生成 |
| 多选复制 | 框选或 Shift 选多个 clip 后复制 | 保持轨道相对关系和时间间距 |
| linked A/V 复制 | 复制视频 clip 时若 linked selection 开启，连带音频 | 新 video/audio clip 互相 linked，新 ID 不复用 |

#### 数据结构建议

新建 `TimelineClipboardPayload`，位置可放在 `src/features/edit/core/timeline_clipboard.py`：

```python
@dataclass(frozen=True)
class TimelineClipboardClip:
    clip: ClipInstance
    source_track_id: str
    source_track_type: str
    relative_start: int

@dataclass(frozen=True)
class TimelineClipboardPayload:
    timeline_fps: float
    anchor_frame: int
    clips: tuple[TimelineClipboardClip, ...]
```

#### 命令建议

| 命令 | 职责 |
|---|---|
| `PasteClipsCommand` | 根据 payload 生成新 clip ID，处理目标轨、重叠策略、linked id 重映射 |
| `DuplicateClipsCommand` | 等价于 paste 到选区结束后或指定 frame |
| `CutClipsCommand` | MacroCommand：copy payload 后 BatchDelete |

#### 实施步骤

1. 在 `commands.py` 增加 `PasteClipsCommand`，核心逻辑先支持 clip，不急着支持 transition。
2. 在 `TimelineView` 增加 `_copy_selected_clips_to_clipboard()`、`_paste_clipboard_at_playhead()`、`_duplicate_selected_clips()`。
3. 使用 Qt clipboard 存 JSON MIME：`application/x-topos-timeline-clips`，同时可写一份 plain text 用于调试。
4. 粘贴时优先使用 target video/audio track。如果源有多条轨道，目标轨不足则自动创建或提示，第一版建议自动创建。
5. 检测重叠。第一版默认 paste 到最近无重叠位置，后续可加 overwrite/ripple paste 模式。
6. Alt 拖拽时，在 drag session 中标记 `copy_mode=True`，预览用虚线/加号；松手后执行 `PasteClipsCommand` 而不是 `MoveClipCommand`。

#### 验收

```bash
python3 -m pytest tests/edit/test_timeline_command_regressions.py -q
python3 -m pytest tests/edit/test_timeline_ops.py -q
```

新增测试：

| 测试名 | 断言 |
|---|---|
| `test_copy_paste_single_clip_creates_new_id` | 粘贴后原 clip 仍在，新 clip ID 不同，source range 相同 |
| `test_copy_paste_linked_av_relinks_new_pair` | 新 video/audio 的 linked_clip_id 指向新 pair |
| `test_duplicate_keeps_relative_offsets` | 多选重复保持间隔 |
| `test_alt_drag_copy_does_not_move_original` | Alt 拖拽后原 clip 位置不变 |
| `test_paste_undo_redo` | 粘贴可撤销和重做 |

### 6.2 桌面素材直接拖拽到时间线和自动创建轨道

#### 功能要求

| 功能 | 行为 |
|---|---|
| 桌面/Finder 文件拖入时间线 | 接受 `text/uri-list` / `mimeData.hasUrls()`，支持视频、音频、图片、常见素材格式 |
| 自动导入 Media Pool | 如果文件不在项目资产中，先创建 `MediaAsset` 并写入 project assets；如果已存在，复用原 asset |
| 直接落到轨道 | 拖到 V 轨落为视频/图片 clip；拖到 A 轨落为音频 clip；视频含音频时自动创建 linked A/V |
| 拖到上一轨空白处自动创建轨道 | 视频素材拖到当前最高视频轨上方的 video 空白区时，新建一条更高的视频轨并放入；音频素材拖到音频区下方空白处时，新建音频轨 |
| 错区智能处理 | 音频拖到视频区时跳到目标音频轨或提示；视频拖到音频区时跳到目标视频轨或提示，不生成错类型 clip |
| 多文件连续插入 | 多个文件一次拖入时按顺序排列，后一个接在前一个后面 |
| 锁轨保护 | 拖到 locked track 不允许落入；可选择相邻可用轨或显示不可落入预览 |
| 自动建 timeline | 无 timeline 时拖入素材自动创建默认 timeline，且新 timeline 应同步到项目/面板状态 |
| 拖拽预览 | 预览框显示目标轨、起始 frame、自动建轨提示、冲突/锁轨状态 |

#### 当前代码观察

| 入口 | 当前情况 | 风险 |
|---|---|---|
| `TimelineView.dragEnterEvent()` | 已支持 media pool MIME、transition、effect、外部 `hasUrls()` | 外部文件拖入能被接受 |
| `TimelineView.dropEvent()` | media pool asset 使用 `TimelineLayoutEngine.resolve_drop_target()` | 新 drop target 逻辑较统一 |
| `TimelineView._handle_external_file_drop()` | 外部文件导入后走旧的 `_get_track_and_frame_from_pos()` + `_find_target_track()` | 没有完全复用 `resolve_drop_target()`，自动建轨和错区处理可能与 media pool 拖放不一致 |
| `TimelineLayoutEngine.resolve_drop_target()` | 空白 video/audio 区已能返回 `create_track=True` | 需要明确“视频轨上方创建更高轨、音频区下方创建新轨”的语义 |
| `_add_asset_as_clip()` | 视频含音频时使用 `InsertLinkedAssetCommand` 原子插入 linked A/V | 可复用，但 target audio track 选择需和 drop target 一致 |

#### 实施步骤

1. 新增统一 helper：`_import_external_files_as_assets(file_paths: list[str]) -> list[MediaAsset]`。
   - 只负责导入/复用 asset，不负责落轨。
   - 支持失败列表，导入失败不影响其它文件。
2. 新增统一 helper：`_drop_assets_at_scene_pos(assets, scene_pos) -> int`。
   - media pool asset drop 和 external file drop 都调用它。
   - 内部必须使用 `TimelineLayoutEngine.resolve_drop_target()`。
3. 修改 `_handle_external_file_drop()`：
   - 删除旧的 `_get_track_and_frame_from_pos()` + `_find_target_track()` 分支。
   - 导入资产后调用 `_drop_assets_at_scene_pos()`。
4. 明确自动建轨位置：
   - video：如果 drop 在 video_area 空白区且在当前最高视频轨上方，`AddTrackCommand.create("video", position=0)` 或等价逻辑创建新最高轨。
   - audio：如果 drop 在 audio_area 空白区且在当前最低音频轨下方，创建新音频轨追加到底部。
   - 如果命令当前只支持默认位置，需要扩展 `AddTrackCommand.create(track_type, position=...)` 并确保 view reload 后目标轨可取到。
5. 拖拽预览支持 create_track：
   - 预览框画在即将新建轨的位置。
   - 显示轻量提示，例如 `New V Track` / `New A Track`，但不要遮挡 clip。
6. 多文件插入策略：
   - 第一个文件使用 drop frame。
   - 后续文件从 `current_frame += duration_frames` 接续。
   - 视频文件如果含音频，linked audio 和 video 使用相同 start。
7. 冲突处理：
   - 第一版不 overwrite，遇到重叠则使用 `_find_free_frame_on_track()` 找最近空位，或显示不可落入。
   - 行为必须在计划执行前确定，建议默认“向右找空位”，避免误覆盖。
8. 项目同步：
   - 外部文件自动导入后要发出 asset imported / project dirty 信号。
   - 自动创建 timeline 后不能只改 `TimelineView._timeline`，还要让 `TimelinePanel/EditPage/project` 知道当前 timeline。

#### 验收

| 场景 | 标准 |
|---|---|
| 从桌面拖一个 `.mov` 到 V1 | 文件自动进入 Media Pool，V1 出现视频 clip，A1 出现 linked 音频 clip |
| 从桌面拖一个 `.wav` 到 A1 | A1 出现音频 clip，不生成视频 clip |
| 拖视频到 V1 上方空白处 | 自动创建新视频轨，例如 V2/V3，并把 clip 放到新轨 |
| 拖音频到 A 轨下方空白处 | 自动创建新音频轨并放入 |
| 拖到锁定轨 | 不添加 clip，预览显示 invalid |
| 多文件拖入 | 按文件顺序连续排列 |
| 无 timeline 时拖入 | 自动创建 timeline，并且项目当前 timeline 状态一致 |
| Undo/Redo | 自动建轨 + 添加 clip 能整体撤销，或至少每一步可撤销且不破坏 project |

#### 建议新增测试

| 测试名 | 断言 |
|---|---|
| `test_external_file_drop_uses_unified_drop_target` | 外部文件 drop 调用与 media pool 相同的 drop target 逻辑 |
| `test_external_video_drop_above_top_video_creates_track` | 拖到最高视频轨上方创建新视频轨 |
| `test_external_audio_drop_below_audio_area_creates_track` | 拖到音频区下方创建新音频轨 |
| `test_external_file_drop_imports_asset_once` | 同一路径二次拖入复用 asset |
| `test_external_video_with_audio_inserts_linked_pair` | 桌面视频拖入生成 linked A/V |

### 6.3 Clip 色彩标签

#### 功能要求

1. clip 可设置 label color，不影响调色节点或素材真实颜色。
2. 颜色在 UI 中影响 clip 主体色或顶部色条。
3. 支持右键菜单：默认、蓝、青、绿、黄、橙、红、紫、灰。
4. 多选 clip 可批量设置颜色。
5. 颜色应保存到项目文件，重新打开仍保留。

#### 实现建议

| 步骤 | 内容 |
|---|---|
| 1 | 在 `ClipInstance` 增加 `label_color: str | None = None`，或先放入 `metadata` 如果模型已有扩展字段 |
| 2 | 在 `to_dict/from_dict` 中序列化 |
| 3 | 新增 `SetClipLabelColorCommand`，支持单 clip 和批量 MacroCommand |
| 4 | `ClipItem.paint()` 根据 label color 调整主体色、左侧色条或顶部色条 |
| 5 | `ClipItem` 右键菜单增加 Label Color 子菜单 |
| 6 | 多选时右键对所有选中 clip 生效 |

#### 验收

| 场景 | 标准 |
|---|---|
| 单 clip 设置颜色 | UI 立即变化，Undo 后恢复 |
| 多 clip 设置颜色 | 全部变化，Undo 一次全部恢复 |
| 保存/加载项目 | 颜色保留 |
| 与 selected/hover 冲突 | 选中边框仍清楚 |

### 6.4 链接、解除链接、分组

| 功能 | 实现方法 | 验收 |
|---|---|---|
| Link/Unlink Clips | 右键菜单和快捷键，修改 `linked_clip_id`，命令化 | 解除链接后移动视频不带音频 |
| Linked selection 开关 | 现有按钮继续控制 `_linked_selection_enabled` | 开关状态影响 move/delete/copy |
| Group/Ungroup | 新增 group_id 或 selection group model | 多个 clip 成组移动和复制 |
| Select linked clip | 右键菜单跳选 linked pair | 找到对应音频或视频 |

### 6.5 启用/禁用 clip 和 track visibility

| 功能 | 实现方法 | 验收 |
|---|---|---|
| Disable Clip | `ClipInstance.is_enabled` 已有，补右键菜单和快捷键 | disabled clip 不参与播放/导出 |
| Video track visibility | `EditTrack` 可增加 `is_visible` 或复用 enabled 字段 | 关闭 V2 后显示 V1 |
| Audio clip mute | clip-level audio mute 字段或 audio effect gain 0 | 静音单个音频片段 |

---

## 7. P3：音频播放和波形

### 7.1 播放有声音

#### 实施步骤

1. 在 `TimelinePanel` 或 `EditPage` 创建 `AudioPlaybackManager`，推荐由 `EditPage` 拥有，避免 TimelinePanel 和 ProgramViewer 双方重复控制。
2. Timeline 设置或改变时，将 `TimelineSequence` 和 media asset resolver 传给 audio manager。
3. 播放开始时：
   - 收集所有未 mute、未锁定、未 disabled 的 audio clips。
   - 根据当前 playhead frame 创建音频解码任务。
   - 设置 timeline fps、duration、sample rate。
   - 启动 QAudioSink。
4. seek 时：
   - audio manager seek 到 frame。
   - 清空旧 buffer。
   - 重新从当前 frame 所在的 audio clip source range 解码。
5. pause/stop 时：
   - pause 保留当前 frame 和 buffer 策略。
   - stop 清空 buffer，停止音频输出。
6. 默认使用 audio master：
   - 有可播放音频时，视频帧跟随 `AudioClock.get_frame_for_fps()`。
   - 无音频或音频不可用时，回退 `EditPlaybackController` monotonic clock。

#### 必须处理的边界

| 边界 | 行为 |
|---|---|
| 时间线没有音频 clip | 正常无声播放，不报错 |
| 音频 clip 有 gap | gap 输出 silence |
| 多音频轨 | mixer 混音 |
| Mute track | 不混入 |
| Solo track | 只混 solo |
| Track volume | 影响 mixer 增益 |
| Clip speed | 第一版可先限制音频不支持变速，并 UI 标注；最终应支持 resample/time stretch |
| 播放倒放 | 第一版可静音倒放或暂停音频，必须明确 |
| JKL 2x/4x | 第一版可按 speed 重采样或静音，只要行为一致 |

#### 验收

| 场景 | 标准 |
|---|---|
| 单音频轨播放 | 有声音 |
| seek 后播放 | 声音从 seek 位置开始 |
| Mute | 立即静音 |
| Solo | 只听 solo 轨 |
| volume 50% | 音量明显降低，测试层面 mixer 数值正确 |
| 播放 60 秒 | 音画误差不超过 50ms |

### 7.2 音频波形显示

#### 实施步骤

1. `TimelineView` 加入 waveform request 调度，不要在 `ClipItem.paint()` 内发起抽取。
2. 可见音频 clip 进入 viewport 时，根据 `asset_id + source_start + source_duration + zoom_bucket + track_height` 请求波形。
3. `audio_waveform.py` 已有 `WaveformData.get_samples_for_range()`，需要确保 source range 和 timeline speed 映射正确。
4. `ClipItem` 增加 `set_waveform_data()`，收到波形后只 update 该 item。
5. 绘制时：
   - 音频 clip 主体绿色。
   - 波形浅绿，正负峰围绕中线。
   - stereo 可上下双波形，低高度时混合为 mono。
6. 缩放时使用 cache bucket：
   - 低 zoom：每 1px 一个 peak。
   - 高 zoom：每 2 到 4px 一个 peak，显示更细。
7. 播放时降低波形抽取优先级，避免影响预览。

#### 验收

```bash
python3 -m pytest tests/edit/test_audio_waveform.py -q
```

新增视觉验收：

| 场景 | 标准 |
|---|---|
| A1 上有音频 clip | clip 内可见波形 |
| 放大时间线 | 波形细节增加，不是拉伸图片 |
| source_start 非 0 | 波形从素材正确位置开始 |
| 轨道高度变化 | 波形垂直居中且不溢出 |
| 缓存命中 | 横向滚动返回后不重新全量抽取 |

### 7.3 音频电平和 mixer

| 功能 | 实施方法 | 验收 |
|---|---|---|
| 播放电平表 | 复用 `src/shared/ui/audio_mixer/audio_level_meter.py` | 播放时有实时 meter |
| 轨道 mixer 面板 | 复用 `track_mixer_widget.py` | 调整音量影响播放 |
| Clip gain | 新增 clip gain 参数或 audio gain effect | 单个 clip 可调音量 |
| 音频效果 | 复用 `effect_registry.py` audio category | Gain/EQ/Compressor 可作用并导出 |

---

## 8. P4：播放/导出范围条

### 8.1 功能定义

| 功能 | 行为 |
|---|---|
| Mark In | 设置范围起点，快捷键 `I` |
| Mark Out | 设置范围终点，快捷键 `O` |
| Clear In/Out | 清除范围，快捷键可用 `Alt+X` 或菜单 |
| Range Bar | 在 ruler 下方显示半透明范围条，两端可拖动 |
| Play In to Out | 只播放范围，结束后停止或循环 |
| Loop Range | loop 开启时优先循环 in/out 范围 |
| Export Range | 导出对话框可选 Entire Timeline / In-Out Range / Selected Clips |
| Render cache range | 后续可对范围生成缓存 |

### 8.2 数据模型

推荐在 `TimelineSequence` 增加：

```python
range_start: int | None = None
range_end: int | None = None
```

或新增轻量 model：

```python
@dataclass(frozen=True)
class TimelineRange:
    start_frame: int
    end_frame: int
    name: str = "In/Out"
```

第一版只支持一个 in/out 范围，后续再支持多个命名 range。

### 8.3 命令和 UI

| 命令 | 职责 |
|---|---|
| `SetTimelineInPointCommand` | 设置 in |
| `SetTimelineOutPointCommand` | 设置 out |
| `ClearTimelineRangeCommand` | 清除范围 |
| `SetTimelineRangeCommand` | 拖动 range 两端时一次性设置 |

UI item：

| Item | 职责 |
|---|---|
| `TimelineRangeItem` | 绘制 range bar、in/out handle、hover/drag |
| `RangeHandleItem` 可选 | 如果不想把 hit-test 写在一个 item 里，可拆分 |

### 8.4 实施步骤

1. 在模型中保存 range。
2. 在 `TimelineView` 创建 range item，位置固定在 ruler 下沿或 ruler 内底部。
3. `I/O` 快捷键设置 range，Shift+I/O 可跳转到 in/out。
4. 拖动 range handle 使用 `SnapManager`，吸附 clip 边界和 marker。
5. `EditPlaybackController` 增加可选 playback_range：
   - play 时如果启用 range，从 max(current, range_start) 开始。
   - 到 range_end 停止或 loop 回 range_start。
6. 导出对话框增加 range source：
   - Entire timeline。
   - In/Out range。
   - Selected clips range。
7. `timeline_export.py` 使用 range_start/range_end 裁切视频和音频。

### 8.5 验收

| 场景 | 标准 |
|---|---|
| 按 I/O | ruler 出现范围条 |
| 拖动 range handle | handle 吸附切点 |
| 范围播放 | 播放只在范围内 |
| loop range | 到 out 后回到 in |
| 导出范围 | 导出帧数等于 `out - in` |
| 清除范围 | 播放/导出恢复全 timeline |

---

## 9. P5：所有剪辑工具检查和修复

### 9.1 工具矩阵

| 工具 | 当前入口 | 应有行为 | 必查问题 | 验收 |
|---|---|---|---|---|
| Select | `A` | 单选、多选、框选、移动、Alt 复制 | 多选 linked A/V、锁轨、错区拖动 | 移动、复制、取消、Undo 正常 |
| Trim | `T` | 左右裁切，不覆盖相邻 clip | source range、speed、linked audio | trim 后播放源帧正确 |
| Blade | `B` | 点击 clip 切开，Ctrl+B 在播放头切所有目标轨 | linked A/V 是否一起切 | 切点两侧 source 连续 |
| Ripple | `R` | 裁切并移动后续片段，删除并闭合 gap | linked track 是否同步 ripple | 后续 clip 位移正确 |
| Roll | `W` | 调整相邻两个 clip 的切点，总时长不变 | 边界限制、handle 残留 | 左右 clip duration 互补 |
| Slip | `Y` | timeline 位置不变，移动 source range | source 边界、缩略图更新 | clip 位置不动，内容变 |
| Slide | `U` | 当前 clip 移动，相邻 clip trim 补偿 | 边界限制、linked audio | 总区间不变 |
| Hand | `H` | 平移时间线 | 和系统滚动条冲突 | 拖动画布流畅 |
| Zoom | `Z` | 点击/框选缩放、Shift 反向 | 与 `Ctrl+Z` 不冲突 | 缩放中心合理 |

### 9.2 每个工具的统一验收流程

1. 建立 3 视频轨、3 音频轨、每轨 3 clip 的测试 timeline。
2. 每个 clip 使用不同 source_start、source_duration、speed、linked_clip_id。
3. 对每个工具执行：
   - 普通 clip。
   - linked A/V clip。
   - 多选 clip。
   - 锁定轨道。
   - 吸附开启。
   - Shift 暂时禁用吸附。
   - Undo/Redo。
4. 检查：
   - TimelineSequence 不重叠。
   - clip source range 不越界。
   - view item 位置与 model 一致。
   - 播放/导出使用新结果。

### 9.3 建议新增测试文件

| 文件 | 覆盖 |
|---|---|
| `tests/edit/test_timeline_copy_paste.py` | Ctrl+C/V/X/D、Alt 拖拽复制 |
| `tests/edit/test_timeline_external_file_drop.py` | 桌面/Finder 文件拖入、自动导入素材、自动建轨 |
| `tests/edit/test_timeline_clip_label_color.py` | clip 色彩标签 |
| `tests/edit/test_timeline_range_bar.py` | in/out range、范围播放、范围导出 |
| `tests/edit/test_timeline_tool_acceptance.py` | Select/Trim/Blade/Ripple/Roll/Slip/Slide 工具矩阵 |
| `tests/edit/test_timeline_audio_integration.py` | 播放有声、mute/solo/volume、音频主时钟 |
| `tests/edit/test_source_viewer_playback_chain.py` | Source 预览蓝屏、GPU timeout fallback、generation guard |
| `tests/visual/test_timeline_davinci_layout.py` | 时间线截图像素验收 |

---

## 10. P6：性能优化

### 10.1 性能指标

| 指标 | 目标 |
|---|---|
| 100 clips 横向滚动 | UI 无明显卡顿，单次 frame paint 小于 16ms 到 33ms |
| 20 video + 20 audio tracks 纵向滚动 | ruler/playhead 固定，滚动流畅 |
| 4K H.264 单轨播放 | 可接近实时，允许预览掉帧追赶 |
| 4K 多轨叠加预览 | 降级策略明确，不阻塞 UI |
| 缩略图生成 | 后台、可取消、可见区域优先 |
| 波形抽取 | 后台、LRU、source range 缓存 |
| 导出 | 不使用 UI 缩略图缓存作为导出源 |

### 10.2 优化方向

| 方向 | 实施方法 | 验证 |
|---|---|---|
| QGraphicsItem 数量控制 | 长 clip 不拆太多 thumbnail item，缩略图在 ClipItem 内绘制 | scene item 数稳定 |
| 可见区域调度 | 只请求可见 clip 的缩略图和波形，额外 buffer 一屏 | 快速滚动不过量排队 |
| 过期请求丢弃 | 每次 zoom/scroll 生成 generation id，旧任务返回时丢弃 | 缩放后不闪回旧缩略图 |
| 缩略图目标尺寸 | 根据 track height 和 ppf 请求合适尺寸，避免拉伸 | 放大不糊 |
| 播放优先级 | 播放时暂停低优先级缩略图/波形任务 | 播放帧率更稳 |
| 增量更新 | clip move/trim 只更新受影响 item，不全量 reload | 编辑不闪 |
| 缓存统计 | thumbnail/waveform/render cache 输出 hit/miss | benchmark 可比较 |

### 10.3 Benchmark 建议

新增 `tools/benchmark_timeline_ui.py`：

```bash
python3 tools/benchmark_timeline_ui.py --clips 100 --video-tracks 10 --audio-tracks 10 --duration 600 --json
python3 tools/benchmark_timeline_ui.py --clips 500 --video-tracks 20 --audio-tracks 20 --duration 1800 --json
```

输出：

```json
{
  "scene_items": 742,
  "avg_layout_ms": 4.2,
  "p95_paint_ms": 18.7,
  "thumbnail_jobs_queued": 36,
  "waveform_cache_hit_rate": 0.82,
  "scroll_jank_count": 1
}
```

---

## 11. P7：主流剪辑软件功能清单

### 11.1 必做基础功能

| 功能 | 优先级 | 实现入口 | 验收 |
|---|---:|---|---|
| Copy/Paste/Cut/Duplicate | P0 | `TimelineView` + `PasteClipsCommand` | 快捷键和 Undo 正常 |
| Alt 拖拽复制 | P0 | drag session copy mode | 原 clip 不动，新 clip 生成 |
| 桌面文件直接拖入时间线 | P0 | `TimelineView.dragEnterEvent/dropEvent` + unified drop pipeline | 自动导入素材、落轨、必要时自动建轨 |
| Clip label color | P0 | `ClipInstance` + command + right menu | 多选设置颜色 |
| 音频播放 | P0 | `AudioPlaybackManager` 接入 EditPage | 播放有声音 |
| 音频波形 | P0 | waveform scheduler + ClipItem | A 轨显示波形 |
| In/Out range bar | P0 | model + item + controller | 范围播放和导出 |
| Disable clip | P1 | `is_enabled` UI 化 | 播放/导出跳过 |
| Link/Unlink clips | P1 | command + context menu | linked 行为切换 |
| Select all on track | P1 | context menu/shortcut | 只选同轨 clip |
| Add/Delete/Rename/Move track | P1 | 现有命令检查 UI | 轨道管理稳定 |
| Lock/Mute/Solo/Volume | P1 | 轨道头 + audio manager | 状态影响编辑/播放/导出 |
| Marker 完整功能 | P1 | marker commands + UI | 添加、编辑、移动、删除 |
| Snap 完整化 | P1 | `SnapManager` | clip/playhead/range/marker 都吸附 |
| Ripple delete | P1 | 已有命令验收 | 删除后 gap 闭合 |
| Insert/Overwrite/Append | P1 | 现有命令验收 | 源监看器入点出点正确 |

### 11.2 中期功能

| 功能 | 优先级 | 实现入口 | 验收 |
|---|---:|---|---|
| Clip fade handles | P2 | `ChangeClipFadeCommand` + item handles | 拖动淡入淡出并导出 |
| Transition browser/drop | P2 | `TransitionItem` + EffectsPanel | 拖转场到切点 |
| Speed/Retime UI | P2 | `SpeedParams` + properties | 变速播放/导出一致 |
| Match frame | P2 | viewer/timeline bridge | 从 timeline clip 跳 source viewer 对应帧 |
| Replace edit | P2 | new command | 用素材替换选中 clip 保持时长 |
| Lift/Extract range | P2 | range + commands | 删除范围留 gap/闭合 gap |
| Nudge clip | P2 | `,` `.` 或 Alt+Arrow | 逐帧移动选中 clip |
| Select nearest edit point | P2 | hit-test | 键盘 trim 操作可用 |
| Timeline search | P3 | asset/name index | 按素材名定位 clip |
| Clip notes | P3 | metadata + UI | 给 clip 写备注 |
| Timeline tabs | P3 | multi timeline UI | 多 timeline 切换 |

### 11.3 高级功能

| 功能 | 优先级 | 说明 |
|---|---:|---|
| Compound clip / nested timeline | P3 | 将多 clip 打包成可展开 timeline |
| Multicam | P4 | 多机位同步和切换 |
| Captions/subtitles track | P4 | 字幕轨、SRT 导入导出 |
| Audio time stretch | P4 | 变速音频保持音高 |
| Render cache per range | P3 | 对复杂片段或范围预渲染 |
| Proxy workflow UI | P3 | 代理状态、生成、切换 |
| Timeline comparison | P5 | 比较不同版本时间线 |

---

## 12. 验证总矩阵

### 12.1 每阶段通用命令

```bash
python3 -m py_compile \
  src/features/edit/ui/timeline_panel.py \
  src/features/edit/ui/timeline_view.py \
  src/features/edit/ui/timeline_items.py \
  src/features/edit/ui/timeline_layout.py \
  src/features/edit/core/timeline_v2.py \
  src/features/edit/core/commands.py \
  src/features/edit/core/edit_playback_controller.py \
  src/features/edit/core/audio_playback.py \
  src/features/edit/core/audio_waveform.py

python3 -m pytest tests/edit/test_timeline_command_regressions.py -q
python3 -m pytest tests/edit/test_timeline_hit_and_drop.py -q
python3 -m pytest tests/edit/test_timeline_p7_regression.py -q
python3 -m pytest tests/edit/test_audio_waveform.py -q
python3 -m pytest tests/edit/test_audio_playback.py -q
python3 -m pytest tests/edit/test_timeline_export.py -q
git diff --check
```

如果 `tests/edit/test_timeline_view_layout.py` 因可选依赖失败，应先记录依赖原因，例如 `rawpy` 缺失，不要把真实 layout 失败混进依赖失败。

### 12.2 视觉验收

每次改 `timeline_view.py`、`timeline_items.py`、`timeline_layout.py` 后至少截 4 张图：

| 截图 | 内容 |
|---|---|
| 空 timeline | 不出现无意义滚动条，timecode/ruler 状态正常 |
| 1V1A linked clip | 轨道头、clip、waveform、playhead 对齐 |
| 4V4A 多轨 | splitter、独立滚动、V1/A1 靠近分割线 |
| 20V20A 大轨道 | ruler/timecode/playhead 固定，滚动条风格统一 |

检查标准：

1. clip 不进入错误 V/A 区域。
2. clip 边界清晰但不出现粗黑边。
3. timecode 和 ruler 垂直边界对齐。
4. 播放头红线贯穿内容区，头部可见。
5. 底部水平滚动条左端与轨道头右边界对齐。
6. 纵向滚动后顶部 ruler 不消失。

### 12.3 手动验收脚本

建议建立一个固定测试工程：

```text
Timeline A:
  V4: 1 short overlay clip
  V3: 1 video clip, source_start != 0
  V2: 2 video clips, gap between
  V1: 3 linked video clips
  A1: 3 linked audio clips with waveform
  A2: 2 music clips
  A3: 1 SFX clip
  markers: 00:00:02:00, 00:00:05:00
  range: 00:00:01:00 to 00:00:06:00
```

手动步骤：

1. 在 Source 素材预览窗口打开 1080p/4K H.264 素材，连续播放 30 秒，确认不蓝屏、不严重卡顿。
2. 播放全 timeline，确认有声音。
3. seek 到每个切点，确认吸附。
4. 从桌面/Finder 拖一个视频到 V1，确认自动导入 Media Pool 并生成 linked A/V。
5. 从桌面/Finder 拖一个视频到最高视频轨上方空白区，确认自动创建新视频轨并落入。
6. `Ctrl+C`、`Ctrl+V` 复制粘贴 linked A/V。
7. 按住 `Alt` 拖拽复制 V1+A1 linked clip。
8. 设置 clip label color。
9. 拖动 splitter，滚动视频和音频分区。
10. 设置 I/O，范围播放，范围导出。
11. 逐个切换 Select/Trim/Blade/Ripple/Roll/Slip/Slide 工具。
12. Undo/Redo 20 次，确认状态回到原样。
13. 保存项目，重启，重新打开，确认颜色、range、轨道状态、clip 位置都保留。

---

## 13. 建议提交顺序

| Commit | 内容 | 原因 |
|---|---|---|
| 1 | Source/Timeline 播放链路审计脚本和隐藏 BUG 测试 | 先建立基线，定位蓝屏和卡顿 |
| 2 | 修复 Source 预览 GPU timeout fallback、generation guard、last good frame | 先让素材预览不蓝屏 |
| 3 | 修复 P0 timeline hidden bugs，尤其 ruler/playhead/scroll/splitter | 防止后续功能建在不稳定 UI 上 |
| 4 | 桌面文件直接拖入时间线和自动建轨 | 补用户导入素材的高频入口 |
| 5 | 时间刻度和轨道头视觉稳定 | 提升最明显的问题 |
| 6 | Clip 外观、边界、label bar、缩略图 LOD | 解决切点不明显和马赛克 |
| 7 | Copy/Paste/Duplicate/Alt drag copy | 补核心剪辑能力 |
| 8 | Clip label color | 小功能但高频且可验证 |
| 9 | 音频播放接入 | 解决播放无声音 |
| 10 | 音频波形接入 | 解决音频剪辑不可视 |
| 11 | Range bar 和范围播放 | 支撑专业预览 |
| 12 | Range export 和音频导出一致性 | 播放和交付闭环 |
| 13 | 工具验收矩阵修复 | 确保所有剪辑工具可用 |
| 14 | 性能优化和 benchmark | 大项目可用 |
| 15 | 自动截图和验收文档 | 防回归 |

---

## 14. 交给其它 AI 的执行提示词模板

```text
你在 Topos Color 项目中优化 Edit 页时间线。

技术栈：
- PyQt6 + QGraphicsView/QGraphicsScene
- 时间线 UI：src/features/edit/ui/timeline_view.py、timeline_items.py、timeline_layout.py、timeline_panel.py
- 时间线模型和命令：src/features/edit/core/timeline_v2.py、commands.py、timeline_ops.py
- 播放和音频：edit_playback_controller.py、audio_playback.py、audio_waveform.py

请只执行本文档中的一个阶段或一个编号任务，不要一次性重写所有时间线。
必须遵守：
1. TimelineLayoutEngine 是坐标唯一来源。
2. 所有可撤销编辑必须走 CommandStack 和 _execute_command()。
3. paint() 只绘制，不解码、不发请求、不改 scene 状态。
4. 不要回退用户已有修改。
5. 每次改动后运行文档中对应测试，并说明未能运行的原因。

当前任务：
[粘贴具体编号，例如 P2 复制/粘贴/Alt 拖拽复制]

输出：
1. 修改文件清单
2. 实施说明
3. 验证命令和结果
4. 剩余风险
```

---

## 15. 完成定义

时间线优化不以“看起来稍微像 DaVinci”为完成标准，而以以下条件为完成标准：

1. 播放链路正常：播放、暂停、seek、JKL、timecode、preview frame 一致。
2. Source 素材预览稳定：1080p/4K 常见素材播放不蓝屏，有 GPU timeout fallback 和 last good frame。
3. 桌面文件可直接拖入时间线：自动导入素材、落到正确轨道、必要时自动创建轨道。
4. 视频播放有声音：至少普通 1x 正放有同步音频。
5. 音频波形显示：A 轨 clip 能显示 source range 波形。
6. 主流基础剪辑可用：复制、粘贴、剪切、重复、Alt 拖拽复制、删除、ripple delete、split、trim、move 都可 Undo/Redo。
7. 时间线显示稳定：timecode、ruler、播放头、轨道头、clip、scrollbar 在多轨、缩放、滚动、拖 splitter 下不消失、不错位。
8. 范围条可用：I/O range 可显示、可播放、可导出。
9. 工具可验收：Select/Trim/Blade/Ripple/Roll/Slip/Slide/Hand/Zoom 都有明确测试或手动验收记录。
10. 性能可接受：大项目滚动、缩放、播放不会被缩略图和波形任务拖死。
11. 导出一致：预览看到的帧、音频和范围导出结果一致。
12. 防回归：核心行为测试、视觉截图、性能基线可以重复运行。
