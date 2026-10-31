# Deliver 导出与编解码架构（deliver-export）

> 本文对应计划04「Deliver 导出与编解码」交付面，描述从 Deliver 页面把一条
> 时间线变成一个交付文件（视频 / GIF / 图像序列）的完整链路：导出队列与
> 作业状态机、渲染→效果→编码→封装的导出管线、Topos 自研编解码层与 native
> 桥、以及 macOS VideoToolbox/Metal 与 Windows D3D11/AMF/NVENC 的硬件后端
> 接入点。所有信息均来自对当前 dev 分支源码的实际阅读（审计佐证见文末）。

---

## 1. 总览：五层结构

```
┌─────────────────────────────────────────────────────────────────────┐
│ UI 层  src/features/deliver/ui/                                      │
│   deliver_workspace · video_export_settings_panel ·                  │
│   video_batch_export_panel（设置草稿 → 生成 VideoExportJob 快照）      │
├─────────────────────────────────────────────────────────────────────┤
│ 队列层  src/features/deliver/core/                                   │
│   VideoBatchExportController（粘合）→ ExportQueue（串行调度/持久化）    │
│   → VideoJobRunner（同步桥）→ VideoExportJob（类型化作业快照）         │
├─────────────────────────────────────────────────────────────────────┤
│ Worker 层  src/shared/export/video_export_worker.py                  │
│   VideoExporter（主线程 API）→ VideoExportWorker（QThread 执行体）     │
├─────────────────────────────────────────────────────────────────────┤
│ 导出管线  src/features/edit/core/timeline_export.py（TimelineExporter）│
│   preflight → TimelineRenderPlan → 逐帧渲染（GPU/CPU 双路径）∥编码     │
│   → 音频并行渲染 → 封装 mux → manifest/telemetry                     │
├─────────────────────────────────────────────────────────────────────┤
│ 编码层  src/shared/export/ + src/shared/codec/ + native/topos_codec/  │
│   FFmpegVideoEncoder / GifVideoEncoder / ToposVideoEncoder           │
│   → ctypes 桥 topos_binding → libtopos_codec（C11 native 核心）       │
└─────────────────────────────────────────────────────────────────────┘
```

职责边界要点：

- **队列层不含像素**：`ExportQueue` 是 PyQt-light 的 `QObject`，导出执行通过
  注入的 runner 回调完成（`ExportRunner = Callable[[ExportJob, ProgressCallback], bool]`），
  因此同一队列可驱动预览、Deliver 页或无头测试。
- **Worker 层不含业务**：`VideoExportWorker.run()` 只做"延迟导入
  `create_timeline_exporter` → 执行 → 三态结果回传"，保证主线程启动时不加载
  PyAV。
- **管线层不知道队列**：`TimelineExporter` 只消费 `TimelineExportConfig`，
  取消/暂停经由 `cancel_export()/pause_export()/resume_export()` 协作式标志。

---

## 2. Deliver feature：导出队列与作业状态机（src/features/deliver/core/）

### 2.1 作业状态机（JobStatus / VideoJobStatus，同一字符串值空间）

```
                    enqueue（强制 PENDING）
                          │
                          ▼
   ┌───────────────── PENDING ─────────────────┐
   │      _process_next() 取队首 PENDING        │
   ▼                      │                    │
 NEEDS_RELINK ──retry──▶ RUNNING ──runner 返回──┼──▶ DONE（终态）
 （时间线缺失，        │  │  │                  │
  永不自动运行）        │  │  │ 异常/返回 False  ├──▶ FAILED ──retry──┐
   ▲                   │  │  └ 取消标志置位     ├──▶ CANCELLED ─retry─┤
   │ restore_from_disk │  │                    │                     │
   └───────────────────┘  └── pause ──▶ PAUSED（队首标记）            │
                                                                  └──▶ PENDING
```

- 状态常量：`pending / running / done / failed / cancelled / paused / needs_relink`
  （`export_queue.JobStatus` 与 `video_export_job.VideoJobStatus` 同值镜像）。
- **NEEDS_RELINK** 是恢复态：重启后队列从磁盘恢复，若 `timeline_id` 在当前
  工程中解析不到（`VideoBatchExportController.restore_from_disk`），作业被置
  为 NEEDS_RELINK 并以 `enqueue(..., preserve_status=True)` 入队——队列的
  `_process_next` 只取 PENDING，因此该状态**永不自动运行**，必须等用户重连
  后 `retry()`（retry 不验证重连本身，信任调用方）。
- **runner 返回值语义**：返回 `True`→DONE；返回 `False` 且取消标志置位→
  CANCELLED；异常→FAILED 且 `job.error = str(exception)`（这是携带具体错误
  消息的唯一途径，返回 False 只会得到泛化的 "export runner returned failure"）。
- 取消标志 `_cancelled_current` 在 `_process_next` 开头**刻意不清零**——
  start() 之前到达的取消请求必须作用于本作业；作业结束后立即消费，避免
  泄漏到下一作业。

### 2.2 持久化（重启可恢复）

- 队列文件：`~/.topos/deliver_export_queue.json`（`TOPOS_CONFIG_PATH` 指向
  配置文件时写其同级目录，与图像批队列 `batch_queue_store` 同约定）。
- 原子写：临时文件 + `os.replace` + fsync；坏文件整体跳过并记日志。
- 加载时**陈旧 RUNNING 一律降级 PENDING**（写 RUNNING 的进程已不存在），
  progress 清零。
- 可恢复状态：`PENDING / RUNNING / FAILED / NEEDS_RELINK`（终态 DONE/
  CANCELLED 不恢复）。

### 2.3 类型化作业快照（VideoExportJob，schema v4）

`VideoExportJob` 是队列真正存储的任务描述：JSON 友好（无 Qt/无 edit 域
enum 引用）、版本容忍（`schema_version` + `_unknown_fields` 保留未知字段，
老构建 round-trip 不丢新字段）、与 edit 特性**延迟解耦**（`to_timeline_
export_config()` 运行时才导入 `TimelineExportConfig` 并做 enum 映射；
`_map_codec` 表把 25 个字符串 codec id 映射到 `ExportCodec`）。

嵌套配置：`VideoConfig`（codec/container/pix_fmt/位深/alpha/`alpha_budget_
policy`…）、`AudioConfig`（v1.6 M-B8 起含 `stems: Tuple[StemTrackConfig]`
追加音轨声明，仅 Topos MOV 容器消费）、`SubtitleConfig`（off/burn/sidecar）、
`NamingConfig`（single_clip/individual_clips、filename_use、唯一名、handles）。

**双模型桥**：队列内部货币是 legacy `ExportJob`；完整类型化快照打包在
`config["video_job"]` 下（`to_legacy_job`/`from_legacy_job`），运行时字段
（status/progress/timestamps）以 legacy 队列为**唯一权威**回填到类型化视图。
`from_flat_config` 兼容视频批面板出现之前的扁平队列格式。

### 2.4 VideoJobRunner：同步队列 × 异步导出的桥

`ExportQueue._process_next` 期望同步 runner；真实导出在 `QThread` 上异步
执行。`VideoJobRunner.run_job` 的桥接方式：

1. `VideoExportJob.from_legacy_job` 恢复类型化视图；
2. `JobContextProvider` 解析活的 `EditProject + TimelineSequence`（由
   controller 注入，runner 保持 UI 无关）——解析失败直接 `RuntimeError
   (reason)`，队列据此写 FAILED；
3. 惰性映射 `to_timeline_export_config()`（ImportError/ValueError 转
   RuntimeError）；
4. 每作业新建一个 `VideoExporter`（worker + 专属 QThread，取消/清理按作业
   界定，不跨作业携带状态）；
5. **嵌套 `QEventLoop` 阻塞等待**：`exporter.export(...)` 投递到 worker
   线程后 `loop.exec()`，进度信号经事件循环送达（主线程保持响应），完成/
   错误回调经 `QTimer.singleShot(0, loop.quit)` 退出（避免信号分发期重入
   Qt）；
6. 结果分型：worker 的 `"EXPORT_CANCELLED"` 哨兵 → 置 `_cancelled` 返回
   False（队列记 CANCELLED 而非 FAILED）；其余错误 `RuntimeError(msg)`
   上抛（队列记真实 error）；finally 中恒定 `exporter.cleanup()` 收线程。

暂停/恢复：`request_pause/request_resume` 转发到 exporter，同时暂停期间
的墙钟从 export_fps 测量中扣除（`_progress_paused_total`）。
速率测量：`_on_progress` 用净耗时计算 `legacy_job.export_fps`（队列持久化
与卡片 UI 直接读取）。

### 2.5 入队前置检查（video_export_preflight.py）

设置面板与队列之间的门禁，**从不静默修复**——降级必须显式上报
（blocking = 不能入队；warning = 可入队但需用户知晓）：
上下文/帧范围/codec-容器组合（`video_export_formats.CODEC_CONTAINER_TABLE`）/
输出路径与父目录可写/磁盘空间估算/编辑域 preflight（`preflight_audio_export`、
`preflight_video_effects_export`、`preflight_fonts_export` 同一规范路径）。
`allow_placeholder_clips=True`（B7）可把占位片段 blocking 降级为 warning
（彩条渲染进成片）。

其余配套：`video_export_presets`（Resolve 风格预设 + 文件名模板展开 +
`build_job_from_preset`）、`custom_export_presets`（用户预设，原子写 + 坏
JSON 逐文件跳过）、`delivery_capability`（能力描述）、`srt_exporter`
（sidecar 字幕）。

---

## 3. 导出管线：timeline_export.py 主流程

`TimelineExporter`（约 11k 行，位于 edit 域）是唯一的渲染→编码执行体；
Deliver 队列与 Quick Export 都汇聚到 `export_timeline(config) -> ExportOutcome`。

### 3.1 主流程（渲染 → 效果 → 编码 → mux）

```
export_timeline(config)
  │ validate() 失败即 raise ValueError
  ▼
_export_timeline_dispatch(config)
  │
  ├─ render_mode == INDIVIDUAL_CLIPS ──────────▶ _export_individual_clips
  │                                              （按启用视频 clip 展开为多个
  │                                               单 clip 导出，复用同一管线）
  ├─ Preflight 链（依次，任一 blocking 即 raise）：
  │    ① preflight_audio_export（scipy 后端缺失且需 EQ/响度 → 阻断）
  │    ② preflight_fonts_export（烧录字幕缺关键字体 → 阻断；替代字体 → 降级）
  │    ③ preflight_video_effects_export（无交付实现/身份不明的效果 → 阻断；
  │       预览效果绝不静默丢失）
  │    ④ preflight_caption_graphics_export
  │    ⑤ 音频路由图快照校验（环/未知 send → 阻断，防静默回落 Master）
  │    ⑥ run_delivery_qc（离线素材等 BLOCKING → 阻断；WARNING 记录归档）
  ▼
计算帧范围 _calculate_frame_range → TimelineRenderPlan
  ▼
_create_encoder_config（分辨率/精确 Rational fps/codec_map 查 pix_fmt 与
  profile；HEVC profile↔位深归一；Topos RAW 比率档 qp 换算；CRF/VBR/CBR；
  threads=0 + pipelined_conversion + convert_workers=3）
  ▼
codec == 'topos' 且 build_direct_topos_delivery_plan 判定通过
  ├─ 是 ──▶ _export_direct_topos（§3.3 直通快路径，成功即 return）
  └─ 否 ──▶ _create_encoder（§4 分派）→ encoder.open()
  ▼
音频准备：expand_sequence_audio_for_render（含嵌套序列音轨展开）
  ├─ add_audio_stream（声道布局单一真相 resolve_export_channels）
  ├─ _declare_audio_stems（v1.6 stems 追加轨声明，须在首帧前）
  └─ 启动后台音频线程 _audio_export_worker（解码/混音/Loudness 与视频并行）
  ▼
帧循环（二选一）：
  ├─ _pipeline_eligible == True ─▶ _render_encode_pipeline
  │     渲染生产者线程 ──深度 3 有界队列──▶ 导出线程消费编码
  │     （ctypes 原生编码释放 GIL，与解码/合成重叠；无死锁收尾）
  └─ 否（任一重叠 clip 启用降噪等）─▶ 串行 for 循环：
        _render_frame → _encode_frame → 进度回调（帧间检查取消/暂停）
  ▼
汇合音频线程 → write_audio_samples（主混音轨 0）→ _write_stem_tracks
  ▼
封装：取消时 encoder.abort()（丢弃 .topos-tmp）；正常 encoder.close()
  （"mux" 阶段 wall 计时）→ _cleanup_clips()
  ▼
成功：_write_success_manifest_async（后台线程生成交付 manifest sidecar，
  等待 _delivery_finalized 事件后才 probe 输出文件）
  ▼
ExportOutcome（三态显式：success / cancelled / failed+error_code+cause；
  附 audio_omitted、hw_fallback_reason、stage_telemetry、delivery_path）
```

帧渲染 `_render_frame_impl` 的内部次序：`get_frame_plan` → 尝试
**planar 直通**（`_try_render_planar_passthrough`）→ 无内容返回黑帧 →
`_composite_layers`（多轨叠加 + 转场混合 + clip 效果 + 调色 + 字幕烧录）。
效果的 CPU 参考实现（blur/sharpen/glow/erode/edge/denoise/defocus）与 GPU
分发键（`_GPU_OPENGL_EFFECT_DISPATCH_KEYS` / `_PLUGIN_OPENGL_DISPATCH_KEYS`）
同文件维护；`_effect_export_skips` 收集无交付实现的效果逐项进 manifest
degradations（T-P1-70，只记日志不可审计是不被接受的）。

### 3.2 GPU / CPU 双路径

图形后端在 `__init__` 一次性解析（`_select_timeline_graphics_backend`）：
读 `ConfigManager.get_backend_selection().graphics`，`TOPOS_BACKEND_GRAPHICS`
环境变量可覆盖；`auto` 时仅当 `probe_d3d11_export_capability().ready` 才选
d3d11，否则 opengl；显式注入 Metal-only 编码桥时强制 metal。

每帧合成的路径选择（`_composite_layers` → `_gpu_render_composite`）：

- **GPU 路径**（metal / opengl）：headless GL 或 Metal 上下文**在渲染线程
  惰性创建**（`_gpu_export_renderer`），覆盖 packed/planar 视频与图像层的
  主调色、缩放、不透明度、常规混合、cross-dissolve、dip-to-color 与全部
  目录化双源转场；Bounded Blur/Sharpen/Glow/Erode/Sobel/Denoise/Defocus 可
  作为 GPU 驻留 BGR 中间体的有序链；只回读最终 BGR 帧，或 Topos 10-bit
  交付直接回读 YUV422P10 平面，或 H.264 场景回读 YUV420P 平面。
  Metal 路径另有逐 clip 预取线程池（`TOPOS_EXPORT_METAL_PREFETCH`，默认开）
  与 VT 解码重叠。
- **CPU 回退**（`_gpu_render_composite` 返回 None）：自适应跳过（主机可见
  单层轻量帧，GPU 往返不划算）、alpha 交付、GPU 不适用 clip、headless
  上下文已失败（本导出内永久）等场景走 `_apply_clip_effects_cpu` +
  `_CPU_EFFECT_HANDLERS` 参考实现。
- **直通门**：严格无效果的 planar 源帧（Topos 422/444、ProRes/DNxHR 同格式、
  h264/hevc+YUV420P）跳过 YUV→RGB→GPU→YUV 往返，直接以 `DecodedFrame`
  planar 形态交给编码器（`_encode_frame_impl` 的 planar 分支）。
- **环境开关**：`TOPOS_EXPORT_GPU_RENDER=0/1`（未设置时 pytest 默认 CPU
  保证确定性，生产默认启用；=1 视为强制，自适应跳过让位）。

GPU 诚实性：`ExportGpuTrail`（legacy）与 `ExportTelemetryRecorder`
（T-P0-20，schema `topos-export-stage-telemetry/1`）逐阶段记录 backend 与
fallback 根因（first-wins）；`full_gpu` 唯一判据是必经阶段（decode/render/
encode_submit）全部 GPU/硬件词表、encode 为硬件编码器、零像素 upload/
download——CPU 编码面绝不虚报 GPU。

### 3.3 Topos 直通交付（T-P1-10 快路径）

单一 TOPOS clip → TOPOS 输出时，`build_direct_topos_delivery_plan` 严格
eligibility 判定通过后走 `_export_direct_topos`：`ToposMediaSource` 的
planar 帧直接喂 `ToposVideoEncoder`，绕过 render plan/provider/BGR48/resize；
RAW→RAW（`topos_cfa*` 源）透传源 TRWM as-shot 元数据。帧循环按档位门控：
重档（standard/hq/4444/4444xq）默认"解码∥编码流水线（depth=3）"，轻档
（proxy/lt）默认串行（重叠换不来新容量，交接反而 −7%）；`TOPOS_EXPORT_
DIRECT_PIPELINE=1/0` 强制覆盖。telemetry 标记 `delivery_path=topos_direct_
planar_cpu_encode`（诚实标注 CPU 编码面）。取消统一 `encoder.abort()`，
不留目标文件或 `.topos-tmp` 残留。

### 3.4 音频与字幕

- 主混音母带在独立线程 `_audio_render_thread` 中渲染（P0-EXPORT-AUDIO），
  视频循环结束后串行写入编码器——容器 mux 保持单线程语义。取消后跳过
  写入；异常在汇合点重抛（与串行同语义）。
- v1.6 M-B8 stems：`_render_stem_masters` 渲染各角色组母带，`_declare_
  audio_stems` 在首帧前向编码器声明追加轨（仅 Topos MOV；其它编码器整体
  降级单轨并告警），`_write_stem_tracks` 在主混音后逐轨写入。
- 无音轨编码器（GIF/图像序列）显式跳过音频并置 `audio_omitted`（能力、
  作业结果、日志三处一致）；Topos 容器 v1.1 起自带音轨不再触发。
- 烧录字幕 `_burn_in_subtitles`；sidecar SRT 由 `deliver/srt_exporter` 产出。
- 响度报告（AM-10）`last_loudness_report`、preflight 结果、QC 报告均挂在
  exporter 上供 UI/manifest 审计。

---

## 4. 编解码层

### 4.1 src/shared/export/video_encoder.py — FFmpeg 家族通用编码器

`FFmpegVideoEncoder`（PyAV）+ `FFmpegEncoderConfig` 是 ProRes/DNxHR/H.264/
HEVC/FFV1 的统一出口，duck-type 兼容 ToposVideoEncoder（`TimelineExporter._
create_encoder` 直接分派）：

- **帧格式转换流水线**：`pipelined_conversion=True` + `convert_workers=3`
  后台线程转换帧格式与编码重叠（4K 10bit 单帧转换 ~45ms 是端到端地板，
  帧级并行推到 max(≈15, 编码) 段；in-flight 内存上界 ~4 帧）；零转换帧
  （`_is_zero_conversion`）直填跳过。
- **码控**：CRF / VBR 目标码率 / CBR（min=max=目标 + 2× VBV）；DNxHR 按
  profile × 分辨率查表。
- **色彩**：`_set_color_metadata` 写容器/流色彩标签；HDR 静态元数据（HDR10
  mastering display/MaxCLL/MaxFALL、HLG）经 `_inject_hdr_static_metadata`。
  色彩配置类型中立化在 `src/shared/codec/color_metadata_config.py`（与
  Topos 编码器共同消费）。
- **硬件编码选择**（详见 §5）：`hwaccel_engaged()` 门控 auto/amf/nvenc/
  videotoolbox；`get_encoder_name()` 经 encoder.py 的 `(hwaccel, codec)`
  seam 映射；open() 内建单帧探针，驱动级失败回退 CPU 并产出结构化
  `hw_fallback_reason`（`encoder_hw_unavailable:<hwaccel>[:detail]`）。
- 平台 kill switch：`TOPOS_DISABLE_WIN_HW_ENCODE` /
  `TOPOS_DISABLE_MACOS_HW_ENCODE`。

配套模块：`gif_encoder.py`（GifVideoEncoder，调色板）；
`sequence_exporter.py`（EXR/TIFF/PNG 图像序列 + HDR）；
`topos_rate_control.py`（ToposRateFeedback 码控反馈）；
`render_cache.py`（Windows 导出调度的确定性段落渲染缓存，键含全部语义
输入）；`frame_provider.py`（ExportFrameProvider，与播放解耦的确定性帧迭代）；
`quick_export_dialog.py` / `export_panel.py` / `export_dialog.py`（单发导出
与 Color 页静止图像导出 UI 及文件名生成）；`gpu_trail.py` /
`export_stage_telemetry.py` / `gpu_benchmark_report.py`（可观测性）；
`batch_queue_store.py`（图像批队列持久化，与视频队列同约定）。

### 4.2 src/shared/export/topos_encoder.py — ToposVideoEncoder 与序列事务

自研 Topos Video Codec 的应用层编码器（ADR-C008），duck-type 对齐
`FFmpegVideoEncoder` 生命周期：

- **临时文件 + 原子替换**：始终写 `<output>.topos-tmp`，close() 成功（≥1
  帧 + finish）才 `os.replace` 到目标路径；任何失败/`abort()`（用户取消）
  删除临时文件——取消不会留下被误识别为成功的输出。
- **无静默回退**：native 库缺失/档位不可用/色彩标签未知均抛带明确诊断的
  `RuntimeError`，绝不回退到其它 codec。
- **双输入形态**：planar（`yuv422/444p{10,12,16}le`、`gbrp*`、
  `topos_yuva*/topos_gbrap*` 白名单，bit-preserving 直通，Alpha 直通）与
  packed BGR24/BGR48/BGRA（numpy 正向矩阵 RGB→YUV，BT.709/601/2020 limited/
  full 精确位移 + 4:2:2 box 子采样）；域外布局显式报错。
- **码控**：档位（capability schema）定标 bpp → 帧级确定性 qp 搜索；
  `config.crf`（0–63）固定 QP 覆盖；`ToposRateFeedback` 复锚。
- **Alpha 预算闭环**（R3）：逐帧消费 native stats；隐式位深首帧探测降档；
  超硬上限三态策略 `alpha_budget_policy = record/error/continue`；close 经
  `tc_mux_set_alpha_budget` 写 tpcB。
- **音频**：v1.1 音轨（PCM 直存 / AAC 应用层编码 `_aac_encode` + priming/
  ASC 回写）；v1.6 stems 追加轨（`add_audio_track_stream` /
  `write_audio_track_samples`）；v1.5 tmcd 起始时码；v1.7 tpcD 档位厂商标识；
  RC1 trwm RAW 元数据。
- **线程预算**：`slice_threads` 受 `src/shared/codec/slice_threads_policy.py`
  的进程级登记表协调（native ABI v2 的 slice 线程数是进程全局，嵌套保存/
  恢复不是线程安全的——登记表以"后设先得"重算并一次性下发）；
  `_topos_timeline_cpu_threads` 在 ≥12 核机器上为 TOPOS 时间线的并行源解码
  预留约 1/3（≤8 worker）解码配额。
- **GOP 分段并行**（v1.8 lp 档，ADR-C050）与音轨声明互斥：带音频导出显式
  `gop_size=0` 单 GOP 保音频可用（ADR-C053 共存路线未落地）。

**图像序列两阶段事务**（`ToposImageSequenceEncoder` / N01）：

```
写帧阶段：暂存目录 <output><mark>.<ts>/ 逐帧写（journal: state=writing）
    │
    ▼ _commit_image_sequence（持输出目录 fd 上的 flock，同目标提交互斥）
 1) 备份：将被替换的输出帧建同 inode 硬链接（不可用降级 copy2）
 2) journal 落 state=prepared（含逐帧 existed 与 created_output）
    ── 此后任意中断，下次导出按日志整体回滚 ──
 3) 暂存帧 fsync
 4) 逐帧 os.replace 发布（单帧原子；集合边界由提交锁串行化）
 5) journal 落 state=committed → 丢弃备份与暂存目录
    │
    ├─ 失败 → _rollback_image_sequence（还原备份/摘除新增帧/新建空目录移除）
    │         回滚未完成 → journal=rollback_incomplete，保留唯一备份并抛
    │         带恢复位置的错误（下次对同目标导出自动尝试恢复）
    ▼
陈旧恢复：_recover_stale_image_staging —— 扫描同前缀暂存目录；宿主进程
  已死（journal pid 探活 + 同进程登记表 _SEQ_COMMIT_IN_FLIGHT）才接管：
  writing/无日志→清理；prepared/rollback_incomplete→按日志收敛回提交前
  状态；committed/rolled_back→输出已是终态，清理即可
```

Windows 上 flock 不可用时降级无锁提交（帧级 `os.replace` 本身原子，串行
化是集合边界）。

### 4.3 src/shared/codec/ — native 桥与中立策略层

| 模块 | 职责 |
| --- | --- |
| `topos_binding.py`（4.4k 行） | ctypes 绑定与 ABI 结构（`_CFrameConfig/_CFrameInput/_CFrameOutput/_CDecodeRequest/_CMovieConfig/...`）；`load_library` 双布局搜索（仓库 `native/topos_codec/build/<cfg>/` 与发布包同级 `lib/`；`TOPOS_CODEC_LIB` 显式指定时不静默回退）；三态库解析（ok/incompatible/not_found）；`ToposCodec`（编码入口 + 进程级 stage stats）、`ToposDecoder`（CPU surface / 批量 / 租约解码）、`ToposGopContext`（v1.8 帧间档序列状态，事务提交在原生侧）、`ToposMovieFile`（mov 容器随机读 + 预读回调，pread/pwrite 全量语义） |
| `topos_profiles.py` | 档位注册表**单一来源**（视频 9 档 + 图片 22 档 + RAW 比率档；主仓库与发布包 `python/topos_codec/` 双份同步） |
| `slice_threads_policy.py` | 进程级 slice 线程数协作登记（§4.2） |
| `decode_worker_budget.py` | per-context 解码 worker 预算协作分摊（进程级 slice pool 与多解码上下文公平化，D3） |
| `color_metadata_config.py` | 色彩元数据中立配置（Topos/FFmpeg 编码器共用） |
| `topos_meta.py` / `topos_trwm.py` | tpcD 容器元数据 / trwm RAW as-shot 元数据编解码 |
| `topos_image_binding.py` | `.toos` 图片容器绑定（TPIM/TPIC，复用同一 native 核心） |

### 4.4 native/topos_codec/ — C11 核心分层

```
native/topos_codec/
├── include/        topos_codec.h / topos_codec_version.h / topos_image.h（C ABI）
├── src/
│   ├── bitstream/  band_codec · band_tokens · bitio · frame_header · packet…
│   ├── codec/      base_encoder/base_decoder/base_policy/base_residual ·
│   │               gop/ctx_*.c（帧内与 IP-2 帧间档核心状态机）
│   ├── entropy/    rans · rice · vlc · block_coding（V1/V2-VLC/V7-R2/V8 写域）
│   ├── transform/  pyramid_transform · quant · plane · sparse_inverse…
│   ├── image/      image_container/reader/writer · half_map（.toos 静态图）
│   ├── mov/        mov.c——Topos MOV 容器读写（tpcA~tpcD 子原子）
│   ├── simd/       color_convert/planar_convert/transform 的 AVX2/NEON 分派
│   ├── common/     cpudetect · color_convert · crc32 · error · alloc…
│   └── cli/        encoder/decoder/image/probe/inspect 等 CLI 工具
├── scripts/        build_sdk / rebuild_lib_manual / perf_gate / check_exports…
└── tools/          基准与调优脚本（tierdec / tierspeed / iso / rdo_qp_eval…）
```

要点：视频与图片像素编解码 100% 复用同一核心（规范冻结"禁止第二实现"）；
熵写域已收缩为 {V1, V2-VLC, V7-R2(默认), V8}（ADR-C046），实验代际读写端
退役；跨平台构建产物由 `candidate_library_paths()` 在 Python 侧定位（Windows
额外扫描工具链命名目录与 VS 多配置子目录、PyInstaller `_MEIPASS`）。

---

## 5. 硬件后端接入点

### 5.1 macOS：VideoToolbox / Metal

macOS 上有**三条互补的硬件通路**，接入点各不相同：

1. **FFmpeg/PyAV 硬件编码（默认通路）**：`FFmpegVideoEncoder.hwaccel_
   engaged()` 在 `auto`/`videotoolbox` 模式下为 8-bit 4:2:0 H.264/HEVC 选择
   `h264_videotoolbox`/`hevc_videotoolbox`（encoder.py seam + 注册探测缓
   存），open() 单帧探针失败回退 CPU。auto 模式限 SDR。
2. **native VT 导出桥（Metal→VT 全 GPU 面，T-P0-30/31）**：
   `src/shared/media/macos_vt/gpu_export_bridge.py` 的
   `VideoToolboxGpuExportBridge` 组合三个部件——`VideoToolboxCompression
   Session`（native 压缩会话，`require_hardware=True`）、`VideoToolboxPacket
   Muxer`（压缩包 mux）、`VideoToolboxGpuSurfaceProvider`（Metal surface 池）。
   接入点有二：
   - **默认自动路径**：`TimelineExporter._maybe_create_default_native_gpu_
     bridge`——仅在 `TOPOS_EXPORT_NATIVE_VT=1` 且 `can_use_native_gpu_
     bridge()` 纯函数 eligibility 全过（fail-closed 词表：源像素格式→surface
     映射仅 yuv420p/nv12→NV12 与 yuv420p10le/p010→P010；无字幕内容/无
     alpha/无转场/无非恒等几何与不透明度/无 clip 效果；TOPOS 4:2:2 家族
     显式拒绝）时创建。初始化失败时**在写任何帧之前**清理半成品输出并整体
     回退普通 encoder（渲染侧 metal 选择一并回退，否则宿主会收到 planar
     帧直接崩溃）；选择状态进 `_bridge_selection` 与 telemetry
     （`bridge_not_selected:<reason>` / `bridge_init_failed`）。
   - **调用方注入路径**：构造 `TimelineExporter(gpu_encode_surface_provider=
     bridge)`；`handles_timeline_video=True` 的桥使 `_create_encoder` 返回
     `_ExternalGpuSurfaceEncoder` 适配器（严格校验 codec/pix_fmt 限于
     h264+yuv420p / hevc+yuv420p10le、输出路径与宽高一致），帧以
     `GpuEncodeSurface` 形态 `submit()` 直达硬件编码；`close_on_export=True`
     在导出 finally 中关闭。
3. **Metal 合成后端**：`_gpu_render_composite` 经 `timeline_gpu_export_
   renderer`（batch-186 审查对象，2.2k 行）在 Metal 栈上完成合成/效果/转场，
   直接回读 NV12/P010/YUV422P10 平面喂对应编码器；Metal 解码帧全程 GPU
   驻留 + 逐 clip 预取与 VT 解码重叠。VT **解码**侧（`macos_vt/source.py`、
   `vt_session.py`、`packet_demux.py` 等）为同一 macOS_vt 包的另一半，供
   预览与导出取帧。

### 5.2 Windows：D3D11 / AMF / NVENC

- **硬件编码（已通）**：Windows 的 h264/hevc 硬编走 `FFmpegVideoEncoder` 的
  AMF/NVENC 通道——`detect_hw_vendor()` 经 `windows_hw/gpu_capability.
  probe_gpu_capability()` 的 DXGI 适配器枚举路由（nvidia→NVENC、amd→AMF；
  primary_vendor 为主显示适配器，Optimus 布局保守回退 CPU），选项由
  `_build_amf_options/_build_nvenc_options` 构造，open() 探针回退并出结构
  化诊断。
- **D3D11 全 GPU 导出（fail-closed，未接线）**：
  - 选择门：`_select_timeline_graphics_backend` 对 `d3d11`（显式或 auto）
  调用 `windows_hw/d3d11_export_capability.probe_d3d11_export_capability()`
  ——要求可选 native 解码/渲染/编码桥**且**真实 T0 导出证据（同 LUID、
  输出可播、零 CPU upload/download/staging/map/copy）齐备才 ready。
  - 合成路径：`_gpu_render_composite` 对 d3d11 后端**有意 fail-closed**，
  返回 None 走 CPU 合成（`not_yet_implemented:d3d11_timeline_composite_
  integration`）——绝不把选中 D3D11 的作业路由进 OpenGL 合成器再谎报为
  D3D11 结果。
  - 契约就绪：`windows_hw/gpu_encode_surface.py` 定义 `GpuEncodeSurface`
  与 `D3D11EncodeSurfaceProvider`（编码器身份词表 nvenc/amf/onevPL、同
  LUID 校验、格式/尺寸匹配为真实现），`submit_to_encoder` 在原生桥落地前
  返回 `not_yet_implemented`，绝不静默走 CPU 转换伪装硬编提交。
  - D3D11 解码/帧基础设施（`d3d11_decoder/d3d11_frame/d3d11_texture_pool/
  d3d11_gl_bridge` 等）已存在并服务预览侧。

---

## 6. 关键调用链

**Deliver 批量导出（主链）**

```
DeliverWorkspace(video_batch_export_panel 设置草稿)
  → run_video_export_preflight(PreflightContext, VideoExportJob)      [入队门禁]
  → VideoBatchExportController.add_jobs
      → VideoExportJob.to_legacy_job → ExportQueue.enqueue（持久化）
  → start_queue → ExportQueue.start → _process_next（队首 PENDING → RUNNING）
      → VideoJobRunner.run_job（同步桥）
          → JobContextProvider._resolve_context（活 project+sequence）
          → VideoExportJob.to_timeline_export_config（惰性 enum 映射）
          → VideoExporter.export（QMetaObject.invokeMethod 队列投递）
              → [Worker QThread] VideoExportWorker.run
                  → create_timeline_exporter（工厂：frame_provider + 序列解析器）
                  → TimelineExporter.export_timeline(config)
                      → preflight 链 → TimelineRenderPlan
                      → _create_encoder_config → _create_encoder
                      → 帧循环 _render_frame / _encode_frame（或流水线/直通）
                      → 音频并行线程 → encoder.close()（mux）
                      → ExportOutcome
                  → on_post_export 质量门（可选）
              → finished/error/progress 信号
          → 嵌套 QEventLoop 退出（EXPORT_CANCELLED 哨兵 / 错误上抛）
      → 队列置 DONE/CANCELLED/FAILED → _persist → _process_next（下一作业）
  → controller.job_finished/jobs_changed → 面板刷新
```

**Topos 编码面（Deliver 主链的编码末端展开）**

```
_encode_frame_impl（planar DecodedFrame）
  → ToposVideoEncoder.encode_frame → _prepare_planar（白名单/位深/stride 校验）
  → _encode_packet → ToposCodec（ctypes）→ libtopos_codec
      （codec/entropy/transform/simd；lp 档经 ToposGopContext 事务）
  → ToposMovieFile mux（tpcA~tpcD、tmcd、trwm、alpha budget、stems 音轨）
  → close()：finish → os.replace(.topos-tmp → 目标)（或 abort() 删临时）
```

**取消链**

```
面板 → VideoBatchExportController.cancel_current
  → VideoJobRunner.request_cancel → VideoExporter.cancel
      → VideoExportWorker.request_cancel（置标志 + 转发 exporter.cancel_export）
          → TimelineExporter._cancel_requested（帧间检查）
              → encoder.abort()（丢弃未定稿数据）→ ExportOutcome.user_cancelled
      → error.emit("EXPORT_CANCELLED")
  → runner 置 _cancelled 返回 False → 队列置 CANCELLED（非 FAILED）
```

---

## 7. 改动注意事项

1. **取消/失败三态不可合并**：`ExportOutcome` 显式区分 success/cancelled/
   failed（P0-05）；worker 的 `"EXPORT_CANCELLED"` 字符串哨兵是 runner 判别
   取消的唯一信号——新增错误码时绝不能与之碰撞；取消必须走 `abort()` 而非
   `close()`（失败/取消不得留下伪成功文件）。
2. **队列状态权威**：运行期 status/progress 以 legacy `ExportJob` 为准
   （`from_legacy_job` 回填）；磁盘恢复时 RUNNING→PENDING 降级、NEEDS_RELINK
   必须 `preserve_status=True` 入队，改动 enqueue 语义前先核对这两条不变量。
3. **无静默降级铁律**：preflight 阻断（音频后端/字体/无交付实现的效果/音频
   路由/QC）与 Topos 编码器"库缺失即抛"是交付可信性的根基；新增降级路径
   必须进 `degradations`/`hw_fallback_reason`/telemetry 显式留痕（W6-02）。
4. **GPU 资源线程亲和**：headless GL/Metal 合成上下文"谁创建谁释放"——
   流水线模式下生产者线程 finally 释放，串行模式导出线程释放；GPU 降噪桥
   同理。跨线程释放 standalone context 是崩溃源。注入 Metal 桥后若 bridge
   初始化失败，渲染侧的 metal 选择必须一并回退（T-P0-31）。
5. **进程级 native 全局**：slice 线程数与解码 worker 预算都是进程级协作
   登记表（`slice_threads_policy` / `decode_worker_budget`）——新增 Topos
   编码/解码上下文必须走登记表，禁止恢复"保存/恢复前一值"模式（已证明
   线程不安全）。
6. **音频线程汇合**：`_audio_render_thread` 在成功/取消/异常三条路径都要
   汇合（`_join_audio_render_thread`）；`_export_direct_topos` 的异常路径
   已补齐汇合（AUD-18401 曾报遗漏类问题）——改动异常路径时保持该纪律。
7. **manifest 定稿事件**：native muxer 在 export 的 finally 里关闭，后台
   manifest 线程必须等 `_delivery_finalized` 置位后才能 probe 输出文件；
   `wait_for_manifest_writers` 在 atexit 排空，勿在导出关键路径上做源 hash。
8. **Topos pix_fmt 域**：deliver 层 `VideoConfig.pixel_format` 的默认值
   （yuv420p 等）不得闯入 Topos 编码域——映射层已有 `_topos_pixel_format_
   override`（单一解析器 `parse_topos_pix_fmt`，未知值告警回落 codec_map
   默认）与 HEVC 域内白名单（profile="main10" 语义优先）双闸，新增 codec
   时延续"域内透传、域外显式拒绝"模式。
9. **图像序列事务**：改 `_commit_image_sequence` 时保持 journal 状态机
   （writing→prepared→committed/rolled_back/rollback_incomplete）与
   `_SEQ_COMMIT_IN_FLIGHT` 同进程登记的完整语义；跨实例同目标并发依赖
   flock 串行化 + 帧级原子 replace 的组合。
10. **schema 演进**：`VideoExportJob` 加字段须同步 `to_dict/from_dict` 的
    known_keys 集合并依赖 `_unknown_fields` 容忍旧构建；改磁盘形态时评估
    `VIDEO_EXPORT_JOB_SCHEMA_VERSION` 是否 bump。
11. **known pitfalls（审计佐证，改动时自查）**：文件名模板只消毒替换值，
    模板字面量中的路径分隔符可逃出输出目录（AUD-12000）；native VT 桥以
    `int(round(fps))` 传帧率，非整数帧率时间线有音画漂移（AUD-18406）；
    PNG/TIFF 序列在 `_get_codec_settings` 被映射到 ProRes 无对应编码器
    （AUD-18404）；CUSTOM 帧范围负值/越界验证缺口（AUD-18405）；CPU 效果
    处理器曾按 uint8 假设归一（AUD-18400）。这些点在触碰相关分支时应回归
    对应测试。
12. **测试口径**：`TOPOS_EXPORT_GPU_RENDER` 未设置时 pytest 环境默认 CPU
    （确定性）；`TOPOS_EXPORT_DIRECT_PIPELINE`/`TOPOS_EXPORT_METAL_PREFETCH`
    等环境开关在基准对比时需显式固定。

---

## 8. 依据的源文件

### Deliver feature（队列/作业/门禁）

- `src/features/deliver/core/export_queue.py`
- `src/features/deliver/core/video_export_job.py`
- `src/features/deliver/core/video_job_runner.py`
- `src/features/deliver/core/video_batch_export_controller.py`
- `src/features/deliver/core/video_export_preflight.py`
- `src/features/deliver/core/video_export_presets.py`
- `src/features/deliver/core/custom_export_presets.py`
- `src/features/deliver/core/video_export_formats.py`
- `src/features/deliver/core/delivery_capability.py`
- `src/features/deliver/ui/deliver_workspace.py`（控制器接线）
- `src/features/deliver/srt_exporter.py`

### 导出管线（edit 域）

- `src/features/edit/core/timeline_export.py`（主文件：`TimelineExporter`、
  `TimelineExportConfig`、`ExportCodec`、`ExportOutcome`、preflight 函数族、
  `create_timeline_exporter`、`estimate_export_size/duration`）
- `src/features/edit/core/timeline_render_plan.py`（`TimelineRenderPlan` /
  `build_direct_topos_delivery_plan`，间接引用）
- `src/features/edit/core/timeline_gpu_export_renderer.py`（GPU 合成渲染器，
  经由 `_gpu_render_composite` 引用）
- `src/features/edit/core/delivery_qc.py`、`export_manifest.py`、
  `audio_graph_snapshot.py`、`channel_layout.py`（preflight/QC/manifest 协作）

### Worker 与编码层（shared/export）

- `src/shared/export/video_export_worker.py`（`VideoExportWorker` /
  `VideoExporter`）
- `src/shared/export/video_encoder.py`（`FFmpegVideoEncoder` /
  `FFmpegEncoderConfig`）
- `src/shared/export/topos_encoder.py`（`ToposVideoEncoder` /
  `ToposImageSequenceEncoder` 序列事务）
- `src/shared/export/topos_rate_control.py`
- `src/shared/export/gif_encoder.py`、`sequence_exporter.py`、
  `render_cache.py`、`frame_provider.py`
- `src/shared/export/gpu_encode_surface.py`（`GpuEncodeSurface` 契约）
- `src/shared/export/export_stage_telemetry.py`、`gpu_trail.py`
- `src/shared/export/encoder.py`（hwaccel 编码器名 seam 与注册探测，间接）
- `src/shared/export/quick_export_dialog.py`、`export_panel.py`、
  `export_dialog.py`、`batch_queue_store.py`

### 编解码中立层（shared/codec）

- `src/shared/codec/topos_binding.py`（ctypes 桥 / 库解析 / ToposCodec /
  ToposDecoder / ToposGopContext / ToposMovieFile）
- `src/shared/codec/topos_profiles.py`（档位注册表单一来源）
- `src/shared/codec/slice_threads_policy.py`、`decode_worker_budget.py`
- `src/shared/codec/color_metadata_config.py`、`topos_meta.py`、
  `topos_trwm.py`、`topos_image_binding.py`

### 硬件后端

- `src/shared/media/macos_vt/gpu_export_bridge.py`（`VideoToolboxGpuExport
  Bridge`）
- `src/shared/media/macos_vt/vt_compression.py`、`gpu_encode_surface.py`
  （压缩会话 / surface provider）
- `src/shared/gpu/metal/context.py`（`MetalComputeContext`，间接）
- `src/shared/media/windows_hw/d3d11_export_capability.py`（D3D11 真相门）
- `src/shared/media/windows_hw/gpu_encode_surface.py`（D3D11 surface 契约）
- `src/shared/media/windows_hw/gpu_capability.py`（DXGI vendor 探测，间接）

### Native 核心

- `native/topos_codec/README.md`（分层与档位综述）
- `native/topos_codec/include/topos_codec.h`（C ABI）
- `native/topos_codec/src/{bitstream,codec,entropy,transform,image,mov,simd,
  common,cli}/`（目录分层）

### 应用层入口（间接）

- `src/app/controllers/deliver_controller.py`（图像批队列 / 静止图导出，
  与视频队列并存的另一交付面）
- `src/app/main_window.py`（Deliver 页装配，经 facade）

### 审计佐证（docs/audit/2026-09-28/batches/）

- batch-119 / batch-120 / batch-121（deliver core + facade）
- batch-122~124（deliver ui）、batch-125/126（edit / adapters 上下文）
- batch-184~186（timeline_export.py、timeline_gpu_export_renderer.py /
  Metal 导出渲染器）
- batch-331~336（shared/export 全目录，含 topos_encoder.py、
  video_encoder.py、video_export_worker.py 专批）
