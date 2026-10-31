# BUGS-agent-C2 — video_playback.py 补测过程发现的疑似 bug

模块：`src/shared/video/video_playback.py`（覆盖率补齐，agent-C2）
日期：2026-09-28
说明：以下问题均为阅读/补测时发现，**未修改任何 src 代码**。编号 BUG-C2-xx。
验证方式：headless 脚本 + 新增测试 `tests/video/test_video_playback_coverage.py`
（BUG-C2-01 / BUG-C2-03 以 characterization test 固定当前行为；BUG-C2-02 的
死锁无法安全地在测试中复现，用独立脚本验证，测试侧以 QRecursiveMutex
workaround 绕开后覆盖队列逻辑本身）。

---

## BUG-C2-01 16-bit BGR 帧转换把白色变成黑色（除以 (1<<bit_depth) 后直接截断）

- 位置：`video_playback.py:3499`（`EnhancedVideoPlaybackEngine._process_decoded_frame`，
  `pixel_format in ('bgr24', 'bgra', 'bgra64')` 的 `data.dtype == np.uint16` 分支）
- 现象：
  ```python
  return (data[..., :3] / (1 << frame.bit_depth)).astype(np.uint8)
  ```
  除以 65536 后结果落在 `[0, 1)`，`astype(np.uint8)` 截断为 0——**任何 16-bit BGR
  帧都会变成全黑**。实测：`np.full((1,1,3), 65535, np.uint16)` → `0`。
  对照同文件 `ProxyPrefetchWorker._process_frame`（:667）的正确写法是
  `(data / 65535.0 * 255.0).astype(np.uint8)` → `255`。
- 触发条件：首帧/HQ 显示路径（`_show_first_frame_via_coordinator` →
  `_process_coordinator_frame` → `_process_decoded_frame`）遇到 16-bit BGR/BGRA
  packed 帧（如 bgra64 / bgr48 高位深源），画面全黑但不报错。
- 影响：高位深 BGR 素材首帧黑屏，易被误判为解码失败。
- 建议：改为 `(data[..., :3] / 65535.0 * 255.0).astype(np.uint8)`（与 worker 路径对齐）。
- 测试：`TestDecodedFrameProcessing::test_bgr16bit_truncates_to_black_bug_c2_01`
  固定当前（错误）行为。

## BUG-C2-02 ProxyPrefetchWorker 非递归 QMutex 嵌套加锁 → 必然死锁

- 位置：`video_playback.py:489-540`（`get_ready_frame`）与 `:612-628`（`_add_frame`）
  / `:630-637`（`_remove_frame`）
- 现象：`_ready_frames_mutex = QMutex()` 默认 **NonRecursive**，但存在两条嵌套加锁路径：
  1. `get_ready_frame` 在 `self._ready_frames_mutex.lock()` 的 try 块内调用
     `self._remove_frame(best_match[0])`（:534），而 `_remove_frame` 第一行再次
     `self._ready_frames_mutex.lock()`（:632）→ **只要取到帧就死锁**；
  2. `_add_frame` 队列满时在持锁状态下调用 `self._remove_frame(min_idx)`（:624）
     → **队列溢出丢帧时死锁**。
- 验证：独立脚本（daemon 线程 + 3s join 超时）确认两条路径均挂死；将
  `_ready_frames_mutex` 换成 `QRecursiveMutex` 后一切正常。
- 触发条件：legacy v2 播放路径（`_on_play_tick` 旧架构分支调用
  `get_ready_frame`；预读线程 run() 满队调用 `_add_frame`）。当前主路径为 v3
  PlaybackCoordinator（不使用该 worker），故线上未被普遍触发；但
  `_start_proxy_prefetch_worker` / 旧架构回退一旦激活即冻结 UI/预读线程。
- 影响：线程永久挂死（无异常、无日志），极难排障。
- 建议：`_remove_frame` 提供不加锁的内部版本（如 `_remove_frame_locked`），
  持锁调用方直接使用；或将 QMutex 换成 QRecursiveMutex（最小修复）。
- 测试：`tests/video/test_video_playback_coverage.py` 的 `_make_worker` 用
  QRecursiveMutex 替换后覆盖全部队列语义（文件内有注释指回本条）。

## BUG-C2-03 基类 VideoPlaybackEngine 引用子类属性 `_playback_clock` / `_proxy_prefetch_worker`，从未初始化

- 位置：
  - `seek_to_time`（:2254）、`get_current_time`（:2331）、`get_time_string`
    （:2345-2346）、`last_frame`（:2278）、`set_playback_speed`（:2291）直接读
    `self._playback_clock`；
  - `_check_proxy_ladder`（:2228）读 `self._proxy_prefetch_worker`。
  两者只在 `EnhancedVideoPlaybackEngine.__init__` 中赋值，基类 `__init__` 没有。
- 现象：纯基类实例调用以上方法抛
  `AttributeError: 'VideoPlaybackEngine' object has no attribute '_playback_clock'`
  （已实测复现）。`_check_proxy_ladder` 在 `_proxy_size` 已设置且性能监控触发
  降级时才会踩到 `_proxy_prefetch_worker`（同样 AttributeError）。
- 影响：基类被单独实例化/被新子类复用时的潜在崩溃；也说明这些方法实际上
  只对 Enhanced 子类有效，基类接口契约失真。
- 建议：基类 `__init__` 中初始化 `self._playback_clock = None`、
  `self._proxy_prefetch_worker = None`。
- 测试：`TestBaseEngineStubAndInfo::test_base_time_helpers_require_subclass_clock_bug_c2_03`
  固定当前行为；`test_last_frame_with_unknown_total_holds_current` 显式置空后
  覆盖 fallback 分支。

## BUG-C2-04（低）首帧三级处理的中间状态 FRAME_PATH_DEGRADED / TIMEOUT_PROXY 永远不可观测

- 位置：`video_playback.py:3148`、`:3206`、`:3223`（`_show_first_frame_via_coordinator`）
- 现象：Level-1/2 中间状态（`FRAME_PATH_DEGRADED`、`TIMEOUT_MASTER`、
  `TIMEOUT_PROXY`）一旦走到 Level-3，会被无条件覆盖为 `PLACEHOLDER`
  （`should_fallback` 恒为 False），而所有成功路径都提前 return。
  因此调用方（`load_video`）永远看不到这些中间状态；
  `FirstFrameResult.is_displayable` 对 `FRAME_PATH_DEGRADED` 的分支经此路径不可达。
- 影响：仅可观测性/诊断价值损失，无功能错误。
- 建议：Level-3 保留进入原因（如在 result 上附 `degraded_from` 字段），或删除
  不可达状态枚举避免误导。
