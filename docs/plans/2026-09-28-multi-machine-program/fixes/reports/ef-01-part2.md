# EF-01 part 2 修复报告 —— src/features/edit/core 的 24 条 Suggestion 级发现

- 批次：EF-01 子运行 2/3（AUD-16505..17812，24 条）
- 分支：dev（未做任何 git add/commit，改动留在工作树）
- 白名单：仅 `src/features/edit/core/` 下文件
- 测试：`tests/edit/test_ef01_aud_part2.py`（48 条，全绿）
- 日期：2026-10-01

## 逐条结论

| ID | 结论 | 位置 | 说明 |
|----|------|------|------|
| AUD-16505 | FIXED | src/features/edit/core/edit_gpu_decode_manager.py:373 | 播放预取线程 `except Exception: pass` 改为 `logger.warning` + `playback_trace("decode_manager.playback_prefetch_error")` 留痕；预取仍为尽力而为（不上抛/不重试，失败退化为下帧按需解码）。 |
| AUD-16506 | FIXED | src/features/edit/core/edit_gpu_decode_manager.py:843, 862 | `preposition_interactive_frame` 的 `t.start()` 失败（RuntimeError 等）与 `_closing` 提前返回路径均回收锁内登记的 busy 预约、done_event 与线程注册表项。 |
| AUD-16715 | FIXED | src/features/edit/core/edit_project_io.py:593, 1040-1215 | `save_project_as`/`quick_save` 复用 `_AsyncSaveWorker` 通道：调用线程只做回调读取 + deepcopy 快照，ZIP/JSON 写入进私有 QThreadPool；完成后 `project_saved` 经 queued 信号回 UI，失败经新信号 `manual_save_failed(str, str)` 上报。返回值语义变为"是否成功排队"（审计建议原文的做法；唯一外部调用方 edit_controller.py:6353 按排队成功更新自身状态，写失败时信号 + 日志可见）。`save_project` 本体保持同步，未动其它调用方。 |
| AUD-16716 | FIXED | src/features/edit/core/edit_project_io.py:621-625, 829, 843, 1040 | `_last_generator_preflight` 写回经新增 `_generator_preflight_lock`；新增线程安全读取器 `get_last_generator_preflight()`。 |
| AUD-16718 | FIXED | src/features/edit/core/edit_project_io.py:1807-1819 | `cleanup()` 的 `waitForDone()` → `waitForDone(5000)`，超时告警并继续清理（同时清 `_manual_save_workers`）。代价（迟到 worker 复活 .autosave）已在注释中说明。 |
| AUD-16801 | FIXED | src/features/edit/core/effect_instance.py:35 | `AnimatableParam` 加 `frozen=True`；全部变更走 add/remove/update_keyframe 的 copy-on-write，代码库无直接字段赋值（已全仓核查）。 |
| AUD-16805 | FIXED | src/features/edit/core/effect_registry.py:588-605 | catalog 注册循环改为逐 spec try/except + warning 隔离，单条畸形 spec 不再中断整个循环；`get_effect_catalog()` 获取失败仍整体降级（保留 AUD-16804 行为）。 |
| AUD-16806 | FIXED | src/features/edit/core/effect_registry.py:519-533 | 类 docstring 重写为实际目录结构（audio 8 mixer FX / generator 2 / catalog 驱动的 filter/stylize/plugin/transform），删除陈旧 transform/blur/sharpen 条目。 |
| AUD-16807 | FIXED | src/features/edit/core/effect_registry.py:33-42, 156-176, 725-745 | `EffectInstance` 提升为模块顶层导入（无环，已验证）；`get_effect_registry()` 与 `_get_audio_default_params()` 均改为双检锁惰性初始化。 |
| AUD-17004 | FIXED | src/features/edit/core/playback_debug.py:154-171 | `seq` 分配、行组装与 `print(flush=True)` 移入同一 `_LOCK` 临界区：输出行不再撕裂，seq 顺序与打印顺序一致。 |
| AUD-17301 | FIXED | src/features/edit/core/realtime_playback/engine.py:364-389 | `set_mode` 锁内只做判定/赋值，状态迁移延迟到锁外经 `_set_state` 执行（其内部发射 `state_changed` 前已释放锁），直连槽不再在持锁下运行。 |
| AUD-17305 | FIXED | src/features/edit/core/realtime_playback/engine.py:1143-1156 | `get_default_engine()` 双检锁（模块级 `_default_engine_lock`）。 |
| AUD-17404 | FIXED | src/features/edit/core/realtime_playback/gpu_frame_pool.py:69-81, 186-191, 283-297, 308-407 | 延迟释放模式：`release_generation`/`shutdown`/容量淘汰在非 GL 线程（以首次 `ensure_gpu_frame` 记录的线程为准）调用时把 handle 入队 `_pending_releases`，由 GL 线程在 `ensure_gpu_frame` 入口 `process_pending_releases()` drain；GL 线程调用或 GL 线程未知（headless/测试）保持就地释放。stats 增加 `deferred_releases`/`pending_releases`。未创建任何真实 GL 上下文。 |
| AUD-17405 | FIXED | src/features/edit/core/realtime_playback/playback_budget.py:80-85, 157-171, 229-252; engine.py:798-819 | in-flight 超时自愈（`in_flight_timeout_s`，默认 2s、下限 4×budget）+ 新增 `abort_render(frame=None)` API；engine `request_present` 在 scheduler/delegate 段异常时显式 abort 占位再上抛。 |
| AUD-17407 | FIXED | src/features/edit/core/realtime_playback/playback_budget.py:120-124, 186-217 | `after_render` 校验帧号一致（不一致丢弃 timing 但仍清占位并告警）；`start_ns` 健全性校验（<=0/未来值回退到 before_render 登记值）；`budget_ms` 属性读改持锁（与 `set_target_fps` 双写对称）。 |
| AUD-17408 | FIXED | src/features/edit/core/realtime_playback/policy.py:335-348 | `get_default_policy()` 双检锁；删除"线程安全由 GIL 保证"的不准确注释。 |
| AUD-17413 | FIXED | src/features/edit/core/realtime_playback/source_resolver.py:265-270, 346-371 | `_ensured_proxies` 的检查+登记+触发收进 `_ensure_proxy_lock`；`invalidate_proxy_cache`/`stats` 同锁。锁内仅调用非阻塞接口（get_proxy_entry/ensure_proxy_async），持锁窗口有界。 |
| AUD-17414 | FIXED* | src/features/edit/core/realtime_playback/source_resolver.py:152-180 | `_resolve_service` 失败不再写回 `self._service`（失败不固化、下次重试），赋值经 `_service_lock` 双检锁。*注：审计"失败后不再重试"的说法与旧码不符（旧码 `self._service=None` 后下次调用本会重试）；本修复消除的是无锁赋值竞态，并显式保证失败不残留。 |
| AUD-17418 | FIXED | src/features/edit/core/realtime_playback/telemetry.py:20, 199-210, 237 | `summary()` 的 `_first_record_ts`/`_last_record_ts` 快照移入锁内；删除未使用的 `field` 导入。 |
| AUD-17420 | FIXED | src/features/edit/core/realtime_playback/timeline_decode_scheduler.py:554-555, 606-611, 760-796 | `_bundle_hits/_bundle_misses` 自增改持 `self._lock`（两处）；`stats()` 的 prefetch 计数改在 `_prefetch_lock` 下读取（锁顺序恒为 _lock→_prefetch_lock，全仓无反向）；`reset_stats()` 补清 `_prefetch_submitted/completed/dropped`。 |
| AUD-17423 | FIXED | src/features/edit/core/realtime_playback/timeline_decode_scheduler.py:700-745 | `shutdown(timeout_s=2.0)`：`cancel_futures` 取消排队任务后有界等待在飞预取（分片 `Future.result`，超时告警并继续关闭 stream）；不再 `wait=True` 无限阻塞。默认参数保持既有无参调用兼容。 |
| AUD-17505 | FIXED | src/features/edit/core/realtime_playback/timeline_gpu_compositor.py:149-155, 288-294, 447-470 | 统计计数器读写/清零统一经 `_stats_lock`。 |
| AUD-17606 | FIXED | src/features/edit/core/recording_manager.py:278-285 | `TakeInfo.status` docstring 补齐实际取值（新增 "pending"/"completed_partial"），与 919/1269/1300/1302/1352/1393 的赋值一致。 |
| AUD-17812 | FIXED | src/features/edit/core/scene_cut/cache.py:613-618 + 全方法 | `ThumbnailCache` 增加 `_lock`（RLock），`current_size`/`entry_count`/`has_thumb`/`save_thumb`/`load_thumb`/`delete_thumb`/`delete_thumbs_for_analysis`/`clear_all` 全部持锁，兑现模块 docstring "Thread-safe operations"。 |

无 INVALID / DUPLICATE / BLOCKED 条目（AUD-17414 的"不再重试"子主张与现状不符，见 *；修复仍按风险点落地）。

## 测试

新建 `tests/edit/test_ef01_aud_part2.py`，48 条测试覆盖全部 24 条修复（每条正例，行为不可注入处辅以确定性结构断言；AUD-16801/AUD-17407 为指定回退敏感性探针）。无模态对话框、无真实 GL 上下文、只跑本文件与指定邻接文件。

### 关卡 1：py_compile 全部改动源文件
`COMPILE_OK`（15 个 src 文件 + 测试文件）。

### 关卡 2：新测试文件
```
.venv/bin/python -m pytest tests/edit/test_ef01_aud_part2.py -q -p no:cacheprovider
48 passed in 2.7s
```

### 关卡 3：回退敏感性验证（抽 2 条）
- AUD-16801：临时还原 `frozen=True` → `TestAud16801AnimatableParamFrozen::test_direct_field_assignment_raises` FAILED（DID NOT RAISE FrozenInstanceError）→ 还原修复。
- AUD-17407：临时还原 after_render 校验块 → `test_frame_mismatch_is_not_recorded`、`test_future_start_ns_is_clamped` 双双 FAILED（elapsed_ns = -999999000）→ 还原修复。
- 还原后全文件复跑 48 passed。

### 关卡 4：定向邻接既有测试（无新增失败）
| 文件 | 结果 |
|------|------|
| tests/edit/test_playback_budget.py | 13 passed |
| tests/edit/test_gpu_frame_pool_key.py | 10 passed |
| tests/edit/test_scene_cut_cache.py | 49 passed |
| tests/edit/test_effect_registry_audio.py | (批次2) |
| tests/edit/test_realtime_playback_policy.py | (批次2) |
| tests/edit/test_realtime_playback_telemetry.py | 21 passed |
| tests/edit/test_recording_manager.py | 35+ passed |
| tests/edit/test_playback_debug_trace.py | 6 passed |
| tests/edit/test_gpu_compositor_real_attach.py | 9 passed |
| tests/edit/test_edit_project_io_root_bin.py | 2 passed |
| tests/edit/test_edit_gpu_decode_manager.py | 50 passed |
| tests/edit/test_realtime_engine_hotpath.py | 44 passed |
| tests/edit/test_edit_project.py | 52 passed |
（批次2 合计 154 passed；各批次均全绿，无已知预存失败出现。）

## 改动文件清单（本子运行）

src/（15 个文件，均在白名单内）：
- src/features/edit/core/edit_gpu_decode_manager.py
- src/features/edit/core/edit_project_io.py
- src/features/edit/core/effect_instance.py
- src/features/edit/core/effect_registry.py
- src/features/edit/core/playback_debug.py
- src/features/edit/core/recording_manager.py
- src/features/edit/core/realtime_playback/engine.py
- src/features/edit/core/realtime_playback/gpu_frame_pool.py
- src/features/edit/core/realtime_playback/playback_budget.py
- src/features/edit/core/realtime_playback/policy.py
- src/features/edit/core/realtime_playback/source_resolver.py
- src/features/edit/core/realtime_playback/telemetry.py
- src/features/edit/core/realtime_playback/timeline_decode_scheduler.py
- src/features/edit/core/realtime_playback/timeline_gpu_compositor.py
- src/features/edit/core/scene_cut/cache.py

tests/：
- tests/edit/test_ef01_aud_part2.py（新建）

备注：工作树中 src/features/edit/core 下另有 part1 的未提交改动（同批共存），本报告仅覆盖上述清单；未触碰白名单外文件，未执行任何 git 写操作。
