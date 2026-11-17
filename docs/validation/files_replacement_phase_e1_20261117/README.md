# E 阶段第一批（E1/E2，FUP-06）：共享图片解码 runtime

日期：2026-11-17 · 分支：fix-ui · 范围：`src/app/ui` 预览/比较窗口的解码
调度层换轨——单一共享 runtime 统一并发、总字节/像素预算与分层取消。

## 交付内容

| 文件 | 变更 |
| --- | --- |
| `src/app/ui/image_decode_runtime.py` | **新增**：`ImageDecodeRuntime`（进程级共享线程池 + 总预算 + 分层取消 + 完成即回收）、`DecodeLease`（窗口租约）、`decode_image_scaled`（预览/比较共用解码实现）、`estimate_pixels`（提交时头部估宽） |
| `src/app/ui/native_files_preview.py` | `PreviewDecoder` 不再自建 `QThreadPool(self)`/`_retired` 列表——改为持有共享 runtime 租约；`decode_preview` 收敛为 `decode_image_scaled` 包装（保留测试 monkeypatch 入口）；`shutdown()` 不再 `waitForDone(2000)` |
| `src/app/ui/native_files_compare.py` | `CompareDialog` 删除每窗口线程池与 `_CompareDecodeJob`（FUP-06：teardown `tryTake` 失败仍清引用的 GC 风险点）——改走共享 runtime 租约；新增 `decode_compare` 入口 |
| `src/features/files/import_job.py` | `ImportProbeRunner.cancel()`：`tryTake` 失败（任务已运行）时保留强引用到 `finished` 送达即回收——同款 `setAutoDelete(False)` GC 风险修复 |
| `src/main.py` | 有序关停接入 `shutdown_shared_runtime()`：先作废全部回包再退出，不同步等待 |

## FUP-06 缺口 → 对策映射

| FUP-06 确认的风险 | 对策（测试锚点） |
| --- | --- |
| N 窗口 = 2+2N 条解码线程、无总预算 | 单一无父对象共享池；总字节/像素预算在提交时判定，超限**立即回绝且可解释**（不无界积压内存）（`test_concurrency_capped_across_leases`、`test_aggregate_budget_rejects_with_explanation`、`test_two_windows_share_one_pool_budget`） |
| teardown `tryTake` 失败即清引用 → 运行中 job 包装被 GC | 排队即丢（预算立即归还）；在途只作废回包，引用由 runtime 保留到**真实完成**（`test_cancel_running_invalidates_and_reclaims_on_completion`、`test_runner_cancel_keeps_running_job_reference_until_finished`） |
| shutdown 同步等 2 秒后忽略结果清引用 | 关闭先标记作废（此后回包不送达）、清队列；**无任何同步等待**；慢解码 >2s 时关闭 <0.5s（`test_shutdown_suppresses_callbacks_and_does_not_block`、`test_preview_decoder_shutdown_does_not_block_on_slow_decode`） |
| 完成的 job 压缩字节留到 shutdown | `run()` 结束即清 `data`；台账/预算在完成回调里立即归还（`test_completion_releases_compressed_bytes`、`test_admits_delivers_and_reclaims`） |
| 验收：100 次开关 | `test_hundred_open_close_cycles_converge`（100 循环后 in_flight/字节/像素全部归零） |

## 验证

- 新增：`tests/ui/test_image_decode_runtime.py`（15 项：预算/并发/分层取消/引用回收/关闭语义/100 次开关/消费方换轨）、`tests/files/test_import_job.py` +1（引用保全）
- 回归：`test_native_files_preview` + `test_files_session_regressions` + `test_native_files_p2`（31 通过）；`test_native_files_transport` + trash/pagination/jobs/four_layouts（84 通过）；`test_files_location_regressions` + `test_native_files_panel`（34 通过）；native host/actions/metadata/platform/plugins/prefs（47 通过）；import job（11 通过）

## 诚实边界

- **E 表第 1 项的"视频解码 runtime"未动**：`base_player`/`libav`/`topos`
  播放器仍是各 widget 独立解码线程（有界泄漏关停纪律已在），共享化归
  E3（EOF 语义）与 E5（GPU 归属）批次——本批只收图片预览/比较路径。
- **CompareDialog 保留的已解码 QImage** 仍由窗口持有（显示位图有
  `MAX_PIXMAP_PIXELS` 上限）；runtime 预算覆盖**在途**任务，不含客户端
  留存——按 FUP-06 原文口径（"runtime 持有在途 job"）。
- 仓库其他特性的 `waitForDone` 站点（stabilization 无超时、media_pool
  200ms 忽略结果、edit_project_io 已检查结果）不在 Files 换轨范围，
  未改。
- 缩略图仍走 HTTP 桥 + GUI 线程解码（E 后续批次解锁，见 D1 记录）。

## 隐私

本批测试全部使用合成 PNG（`QImage` 内存生成）与临时目录；无真实媒体
路径、无隐私图片提交。
