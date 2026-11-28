# S0-1 实时播放接管 · 实机验收清单（2026-09-17）

> 目的：把 P1-05 / P1-07 / P1-10、ring bundle 消费与 Comp Clip GPU 路径从"已单测"变成"已实测"。
> 本清单对应提交：`42221aec`（P1-05）、`0b47f58e`（P1-10）、`3a334864`（P1-07）、`421d5841`（文档）。
> 环境：macOS GUI 实机 + `tests/test_videos` 真实素材（P0-13 门）；采集遵循总计划 §16.1 四道门与 §20.9 完成定义。
> 采集开关：`TOPOS_EDIT_PLAYBACK_TRACE=1`（终端 trace），`TOPOS_EDIT_PLAYBACK_SLOW_MS=40` 可选。

---

## 1. 素材与场景矩阵

| 场景 | 素材 | 操作 | 验证目标 |
|---|---|---|---|
| S1 基准正放 | 4K_H265（长 GOP）、4K_ProRes_HQ、Topos 422 | play 60s 匀速 | bundle 命中率、miss 词表分布 |
| S2 变速穿梭 | 同上 | J/K/L 2x→4x→8x 正反 | P1-07 窗口扩展与 hold 频率 |
| S3 重负载降档 | 多轨（≥3 层 4K）或加重型效果 | play 60s | P1-10 降档触发/回退/FPS 变化 |
| S4 效果瓶颈 | 单轨 + unsupported/重型效果 | play 60s | P1-05 unsupported_effect 警告且不 rebuffer |
| S5 代理等待 | 无代理素材 + 触发代理生成 | play | P1-05 proxy_pending 立即 rebuffer |
| S6 Comp Clip | 含有效 Composite graph 的 comp clip | play 60s + scrub | P1-13 零 readback 证据 |
| S7 seek 漫游 | 4K_H265 | 随机 seek 20 次 | 无阻塞 seek（P1-08 现状记录） |

## 2. 采集的 trace 字段与判据

### 2.1 P1-05 miss 语义

- 事件：`engine.quality_pressure_change` 不相关；关注 telemetry `miss_reason` 字段与 `playback_warning`。
- trace/telemetry 采集点：`PlaybackFrameTelemetry.miss_reason`、`engine.back_pressure_drop`、`playback_warning("rebuffering"/"unsupported_effect")`。
- 判据：
  - [ ] 瞬态 miss（decode/gpu/cache 类）在阈值(3)内**不**产生 rebuffering 警告（hold-first 生效）；
  - [ ] `unsupported_effect` 类每 miss 段只警告一次，全程无 REBUFFERING；
  - [ ] S5 中 `proxy generating` 归因的 miss 立即进入 REBUFFERING；
  - [ ] telemetry 的 `miss_reason` 与 playback_warning 的 `reason` 字段一致（可对拍）。
- 汇总方式：trace 终端输出按 reason 计数；grep `miss_reason` 汇总分布表。

### 2.2 P1-10 自适应质量

- 事件：`engine.quality_pressure_change`（level/old_level/reason/scale）、`renderer.adaptive_quality_scale`。
- 判据：
  - [ ] S3 在持续负载下出现 `elevated`（正常应随后 `critical`），且伴随 `renderer.quality_generation_invalidated`；
  - [ ] 负载解除后（≤ up_streak×帧时距 + 驻留）回 `normal`；
  - [ ] 用户显式选分辨率档（ProgramViewer 菜单 1/2、1/3…）时**不出现**降档（手动覆盖保护）；
  - [ ] 关闭偏好 `video_preview_adaptive_quality=false` 后无任何降档事件；
  - [ ] decode 目标尺寸变化可从 `decode_manager.create_source` 的 target_size 看到（64px 桶）。
- **调参矩阵**（依次实测 FPS-p95 与降档次数，选 Pareto 最优组合写入默认值）：

| 组 | down_streak | up_streak | min_dwell | 观察 |
|---|---|---|---|---|
| A(默认) | 6 | 24 | 30 | 基线 |
| B | 4 | 16 | 15 | 更激进降档 |
| C | 8 | 48 | 60 | 更保守（少换档） |

- 记录：每组播放 60s 的 present FPS（telemetry summary `session_total_frames`/时长）、降档次数、回退延迟。

### 2.3 P1-07 变速窗口

- 采集点：`engine`→`prepare_window_async` 的 `radius_after`（可在 trace 或调试器观察）、ring hold、`_prefetch_dropped`。
- 判据：
  - [ ] 2x 时 radius_after=16、4x/8x 触顶 32（可用单测已锁，实机确认 controller.playback_speed 传递链）；
  - [ ] 8x 穿梭 hold/rebuffer 频率显著低于基线行为（如能跑旧版对比则对比，否则记录绝对值）；
  - [ ] 方向反转时旧窗口取消（`renderer.gpu_result_drop_*` 无旧方向帧上屏）。

### 2.4 ring bundle 消费（S0-1 主链）

- 采集点：`scheduler_bundle_hit` / `scheduler_bundle_layers`（worker trace_info）。
- 判据：
  - [ ] S1 普通 H264/H265/Topos 正放 bundle 命中率 ≥ 目标值（首版建议 ≥80%；低于此值需要定位 partial/occluded 原因分布）；
  - [ ] 命中失败时 trace 能看出原因类别（效果层/特殊资产/generation 不匹配）。

### 2.5 Comp Clip 零 readback（S6）

- 采集点：帧 trace `import_upload` 段（`FrameTraceCollector.annotate` 迟到合并的 bytes/mode/pixel_format）、`comp_gpu_handle_missing` 出现次数。
- 判据：
  - [ ] 播放中 comp clip 层无 CPU readback 证据（frame trace import 段为 GPU import 且 bytes 合理）；
  - [ ] 色彩/alpha 与暂停全质量帧目测对拍（±可接受范围记录）；
  - [ ] 记录实际 FPS 与 deadline_drops。

### 2.6 P1-08 现状记录（S7）

- 记录 seek 期间 UI 冻结感（有无）、`decode_manager.interactive_progress_frame` 频率；不做 pass/fail，作为后续立项依据。

## 3. 产出物

- `bench_out/s01_takeover/`（gitignored）：原始 trace 文本、每组调参的 summary 截图/文本。
- 本文档追加一节"实测结论"：每个判据的 ✔/✘ + 数字；P1-10 选定的参数组；bundle 命中率不达标时的归因分布。
- 结论回写主计划 §22.4 与 §7（P1 状态表）。

## 4. 已知限制

- 本清单所有判据无法在无 GUI 环境执行——这是总计划 §20.9/P0-13 的硬性要求，不是可选项。
- 变速扩展的"收益对比"没有旧版对照分支时，只记录绝对值，不宣称提升幅度。
