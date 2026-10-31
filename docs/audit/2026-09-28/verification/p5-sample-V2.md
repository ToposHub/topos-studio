# P5 抽查复核任务书 V2（共 16 条）

每条：先读来源批次报告中该编号的完整论述（含证据与行号），再独立 Read 源码验证。

- [AUD-19740] [Critical] fps 下拉的 "Custom" 项无输入控件，选中后确定触发 `float("__custom__")` ValueError，在调用方 finished 槽内未捕获导致崩溃
  - 位置: N/A:441（配合 268-270、397-407）
  - 来源报告: docs/audit/2026-09-28/batches/batch-197-features-edit-ui.md

- [AUD-31703] [Major] 图像尺寸静默强制 2048×2048 正方形，`aspect_ratio` 请求字段被完全忽略
  - 位置: N/A:288-303
  - 来源报告: docs/audit/2026-09-28/batches/batch-317-shared-ai_models-generation.md

- [AUD-26704] [Major] insert_keyframe 在已占用帧上的 undo 不恢复原关键帧，反而把该帧关键帧整个删除（undo 路径数据丢失）
  - 位置: N/A:770
  - 来源报告: docs/audit/2026-09-28/batches/batch-267-features-teaching_animation-ai.md

- [AUD-25903] [Major] 曲线两点路径 clip(0,1) 与多点/PCHIP/Widget 的线性外推不裁切行为不一致
  - 位置: N/A:122
  - 来源报告: docs/audit/2026-09-28/batches/batch-259-features-scripting.md

- [AUD-15513] [Major] scrub 预览 worker 与播放启动存在竞态窗口，可在播放中扰动解码器位置并复位 DSP
  - 位置: N/A:3625-3634, 4232, 4269, 4370
  - 来源报告: docs/audit/2026-09-28/batches/batch-155-edit-core-audio-playback.md

- [AUD-25814] [Major] `set_params` 绕过 ColorWarper 的状态复位防护：未停 `_changed_timer`/清 `_pending_deltas`，陈旧拖动样本可在程序化更新后发布并覆盖新状态
  - 位置: N/A:99-105
  - 来源报告: docs/audit/2026-09-28/batches/batch-258-features-color-ui.md

- [AUD-16500] [Critical] close/invalidate 三条关闭路径无视 P2-09 租约，解码中途关闭 VT/FFmpeg 会话 → C 层 use-after-free（SIGSEGV）风险
  - 位置: N/A:2926-2969 同构）
  - 来源报告: docs/audit/2026-09-28/batches/batch-165-features-edit-core.md

- [AUD-36813] [Suggestion] with_modified/with_active_timeline 手工逐字段重建，存在新增字段漏带风险
  - 位置: N/A:202-240
  - 来源报告: docs/audit/2026-09-28/batches/batch-368-shared-project.md

- [AUD-0510] [Major] 粘贴时同一 patch dict 对象复用到多个目标图层，目标间共享可变嵌套结构
  - 位置: N/A:197
  - 来源报告: docs/audit/2026-09-28/batches/batch-005-color-core-grade-history.md

- [AUD-13500] [Critical] `complete_host_export` 引用未定义名 `subtitle_duration_frames`，每次调用必抛 NameError，AI 导出完成链路整体断裂
  - 位置: N/A:765
  - 来源报告: docs/audit/2026-09-28/batches/batch-135-features-edit-ai_editing.md

- [AUD-20903] [Major] CPU 回退为 UI 线程同步解码（关键帧 seek + 前向解码），watchdog 超时与播放 tick 直接阻塞界面
  - 位置: N/A:2161-2169（同步 read_frame）；2220-2233（watchdog 触发）；2255-2261（request_cpu_fallback 路径）
  - 来源报告: docs/audit/2026-09-28/batches/batch-209-features-edit-ui.md

- [AUD-8701] [Critical] Stretch 变形在 UI 允许的参数范围内可产生 NaN/inf 顶点并上传 GPU
  - 位置: N/A:246-249
  - 来源报告: docs/audit/2026-09-28/batches/batch-087-features-composite-render3d-deformers.md

- [AUD-18900] [Critical] `_GuardedPreviewSource` 的 acquire/retire 为无锁 check-then-act，UI 线程 retire 可关闭正在被 worker 读取的 PyAV 源
  - 位置: N/A:447-470
  - 来源报告: docs/audit/2026-09-28/batches/batch-189-features-edit-core.md

- [AUD-27900] [Critical] HTML 场景以 svg_sequence/html_sequence 导出无 pixel_format 分流：RGBA8 字节被写成 .svg（静默损坏）或 UTF-8 解码崩溃
  - 位置: N/A:213-214, 497-502
  - 来源报告: docs/audit/2026-09-28/batches/batch-279-features-teaching_animation.md

- [AUD-5300] [Critical] 共享内存版模糊计算着色器的 padding 协作装载只填充每 16 个槽位中的一个，卷积读取未初始化共享内存
  - 位置: N/A:3797-3832（H 左/右装载）、3841-3846（H 读取）、3895-3930（V 装载）、3937-3942（V 读取）
  - 来源报告: docs/audit/2026-09-28/batches/batch-053-features-composite-core.md

- [AUD-8604] [Suggestion] 公共 API 类型注解不完整（select_clip / clip_names / _CacheEntry / sample）
  - 位置: N/A:238-242
  - 来源报告: docs/audit/2026-09-28/batches/batch-086-features-composite-render3d.md
