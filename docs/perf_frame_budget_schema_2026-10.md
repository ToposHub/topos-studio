# A0.1 · 帧预算遥测契约（schema v3，2026-10-22）

> 依据：[计划表 A v3](perf_plan_A_mac_2026-10-22.md) §2 / A0.1；实现见
> `src/shared/video/frame_trace_schema.py`（词表）、`playback_frame_trace.py`
> （trace 通道）、`src/features/edit/core/realtime_playback/telemetry.py`
> （telemetry 通道）；harness：`scripts/bench_frame_budget.py`。
> 关联：[A0.3 拷贝台账](perf_frame_budget_report_mac_2026-10.md)。
> B 线（Windows）复用本契约的报告格式与字段语义，不另建第二套事件模型。

## 1. 帧身份与请求关联

- 一次播放请求以 `(session_id 约定=进程, generation_id, trace_id)` 关联；
  `trace_id` 由 `next_frame_trace_id()` 进程内单调分配（0 保留给旁路帧），
  engine `request_present` / `request_scrub_present` 入口分配，随
  delegate → worker → GL 完成 → present → Viewer paint 逐段携带。
- 源素材帧身份与时间线帧分别记录：`frame_index`（时间线帧）、
  `source_frame`（源帧）。槽位事件带 `present_slot_id`；租约代次由
  `generation_id` 区分，防复用错配。
- 同帧多请求：trace_id 各自独立；telemetry `enrich_last` 优先按
  trace_id 精确匹配，匹配不到退回帧号（播放前进方向 64 帧内）。

## 2. 有效性 / 状态 / 原因（实测 0 ≠ 未测量）

### 2.1 段有效性词表 `STAGE_VALIDITY_STATES`

`("unset", "measured", "not_applicable", "unavailable")`

| 值 | 语义 | 例 |
|---|---|---|
| `unset` | 证据未到（迟到 annotate 没赶上 / 帧被丢弃） | trace 滚出 ring 的上传统计 |
| `measured` | 有实测值；**0.0 也是有效实测** | planar 上传 0.3ms |
| `not_applicable` | 该帧路径无需该段操作 | 零拷贝 IOSurface 导入帧的 import_upload |
| `unavailable` | 该帧上不可测量（驱动/平台不支持） | GL timer query 不可用 |

- trace 通道字段：`import_upload_status`（含 `gpu_time_unavailable`
  既有布尔沿用）；telemetry 通道：`PlaybackFrameTelemetry.stage_status`
  dict（键 ∈ decode/import_upload/composite/present/total，record 时
  词表校验，未知键/值直接 ValueError）。
- telemetry `summary()` 的每个 `*_ms_*` 分位键都配
  `coverage[stage] = {samples, coverage}`；旧记录（无状态标注）按
  `value > 0` 兼容采样，实测 0 的帧从此不再被静默剔出样本。

### 2.2 呈现状态词表 `PRESENT_STATUSES`

`("submitted", "viewer_copy", "displayed", "superseded", "dropped", "slot_released")`

| 值 | 语义 | 观测点（file:line 见代码） |
|---|---|---|
| `submitted` | composite 完成，descriptor 已发出（fence 就绪） | TPR `_on_present_frame_ready` |
| `viewer_copy` | blit 命令已入队 + fence 已等待（= ACK 时点；**不是可观测显示**） | PV `_copy_shared_texture_to_display` finally |
| `displayed` | paint 内 `_render_texture` 之后可观测呈现（paint 级；**无平台 vsync/扫描反馈，不得解读为物理屏幕扫描时间**） | PV paintGL `frame_presented` 前 |
| `superseded` | pending 帧被更新帧顶掉（未显示即回收） | PV `_ack_pending_present_without_display` / TPR stash 覆盖 |
| `dropped` | stale/切代/背压/守卫拒绝，显示前显式丢弃（带 `extra.present_drop_reason`） | TPR 守卫、engine 背压 |
| `slot_released` | present slot 经 ack 回收（资源协议终点） | TPR `_on_present_slot_acked`（dropped/superseded 帧不被覆盖） |

- `present_ms` 仅在 `submitted → displayed` 完整路径上计算
  （单调时钟差，annotate 自动回填）；superseded/dropped 帧 present_ms
  恒 0（不虚构）。
- ACK 只证明资源协议推进（viewer_copy）；GPU 完成事件不用 CPU 发出
  绘制命令代替（fence 语义保留）。

### 2.3 回读分类词表 `READBACK_KINDS`

`("seek_snapshot", "background_snapshot", "explicit_snapshot", "monitor", "cpu_fallback")`

- 普通预览热路径**不应出现任何回读**；出现即按词表归因：
  快照（seek 布防 / idle 后台 / 显式）、监看（PBO 取回与直读兜底，
  PV `_note_monitor_readback_bytes`，字节同时进全局 CPU_DOWNLOAD 审计）、
  CPU 回退合成（TPR worker `readback_kind='cpu_fallback'）。
- 字段：`readback_kind` + `cpu_download_bytes`（累加合并）。

## 3. GPU query 绑定发起帧（迟到回填纪律）

- `GlPassTimer.begin_frame(tag=timeline_frame)`；`collect()` 返回
  `[{pass, ms|None, frame_seq, tag}]`，结果属于 RING_DEPTH-1 帧前的
  **发起帧**。
- GL 线程把完成结果放入有界 deque（16），TPR 的 GL 完成回调
  `drain_completed_gpu_queries()` 后按发起帧 trace_id `annotate`
  （`gpu_submit_ms` + `gpu_time_unavailable=False`）。
- **禁止**把 `last_ms()`（最近完成值）写进当前帧——
  `_finalize_present_frame` 的 `GpuPresentFrame.extra` 现为
  `{gpu_submit_ms: None, gpu_time_unavailable, gpu_query_deferred}`；
  HUD 的"最近值"继续走 `gpu_pass_timer_stats()`（并新增
  `timer_health.{in_flight_queries, overflow_drops}`）。
- 读回失败（ms=None）保持 fail-closed（unavailable），不当 0.0；
  迟到证据 annotate 失败（滚出 ring / trace_id=0）计入
  `summary()['annotate']['missed']`，不静默。

## 4. 单位、时钟与线程归属

| 项 | 规则 |
|---|---|
| 单位 | `*_ms` 毫秒（float）；`*_bytes` 字节（int，累加）；`*_ns` 纳秒（deadline） |
| 时钟 | 耗时用 `time.perf_counter()`；呈现提交/显示配对用 `time.monotonic()`（`present_submitted_mono`）；**不混减 wall-clock 与 GPU 时间戳** |
| 六类诊断段 | CPU 编排（engine 派发 total_ms 初值）/ 解码（decode_ms）/ 导入上传（upload_ms, import_upload_ms）/ GPU 合成（gpu_submit_ms + composite_ms）/ 回读（readback_kind + cpu_download_bytes）/ 呈现（present_status + present_ms）。段可能重叠，**不得六段相加当总耗时**；另有 annotate merged/missed 与 eof_wraps 类计数 |
| 线程 | engine 派发/telemetry.record：UI/tick 线程；trace 构造/enrich：PreviewRenderWorker；GlPassTimer/上传统计暂存/快照：GL 渲染线程；annotate/collector：任意线程（RLock）；PV 呈现状态：UI 线程 |
| 通知不阻塞 | 全部走有界暂存 + 完成回调取走（planar/readback/query 三 store 同模式）；无每帧同步 GPU 等待 |

## 5. 兼容策略

- **旧字段全部保留**；新增字段全部带缺省（''/0/-1），`to_compact_dict`
  自动省略空值——旧消费者无需修改。
- `FRAME_TRACE_SCHEMA_VERSION` 2→3；`annotate()` 白名单收紧为显式枚举
  （未知字段 ValueError），5 个字节字段改**累加合并**（同一帧多份迟到
  证据各自累加；既有调用方每帧只 annotate 一次，行为不变）。
- telemetry `summary()` 旧键语义不变（采样从 ">0 过滤" 改为
  "measured 或 legacy>0"——实测 0 帧进入样本，属修复而非破坏）；
  新增键：`coverage` / `enrich_misses` / `present_outcome_counts` /
  `upload_ms_*` / `present_ms_*`。
- `set_present_frame` 新增可选 `trace_id` 形参（缺省 0，兼容旧调用）；
  `GlPassTimer.begin_frame` 新增可选 `tag`；`collect()` 返回值从 None →
  list（既有调用方忽略返回值，不受影响）。
- engine scrub 记录补 `generation_id`（原默认 0，属修复）；背压 drop
  在 trace 通道新增 `present_status='dropped'` 记录。

## 6. 报告产物（harness 输出契约）

`scripts/bench_frame_budget.py` 每素材一个 JSON + 汇总 `summary.json`：
`env`（git head/dirty、python/av/moderngl/PyQt6、GL renderer、
bench_manifest 或降级原因）、`route`（zero_copy_capability 真实探测结果、
decode_route、vt_fallback_reason、eof_wraps）、`r16ui`（self-test 结果与
disabled_reason）、`telemetry_summary`（含 coverage/enrich_misses/
present_outcome_counts）、`trace_summary`（含 present_status_counts/
readback_kind_counts/annotate merged-missed/import_upload_coverage/
pixel_copy_bytes/deadline）、`errors`。
退出码：0 全成功 / 1 无素材 / 2 部分或全部失败。

## 7. 验收对照（计划 A0.1）

| 验收项 | 实现位 |
|---|---|
| 正常帧关联 | trace_id 全链携带 + enrich/annotate 双通道 |
| 同帧多请求 | trace_id 精确匹配优先（测试 `test_enrich_*`） |
| seek 切代 | generation 守卫 → dropped+reason；scrub 记录补 generation |
| 丢弃/超时 | engine 背压 dropped 记录；monotonic/generation 守卫 dropped |
| 迟到 query | 发起帧回填 + annotate missed 计数（`test_gl_pass_timer_*`） |
| 槽位复用 | slot→frame 映射 slot_released；未 ack 复用仍由 OutputSlotPool 断言 |
| 异常/关闭 | annotate 异常吞掉但计数；`_ack` finally 恰好一次（AUD-20403 保留） |
| 有效零值 vs unavailable | STAGE_VALIDITY_STATES + gpu_time_unavailable fail-closed |
| 样本覆盖率 | telemetry coverage + trace gpu_time/annotate/import_upload_coverage |
| 插桩开销 | 同机 A/B：`--snapshot-every 0` 关闭回读分支对比（A0.2 执行） |
