# 批次报告：batch-173 — src/features/edit/core/realtime_playback//

编号区间：AUD-17300–AUD-17399。审查日期：2026-09-28。
维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解。

统计：Critical 0 · Major 3 · Suggestion 11。

---

## src/features/edit/core/realtime_playback/__init__.py (行数: 147)

### 发现

（无）

### 通过项
- 通过（无逻辑）：纯 re-export 包入口，无控制流。
- 导出清单核对：types/policy/telemetry/frame_ring_buffer/clip_decode_stream/timeline_decode_scheduler/decode_manager_source/gpu_frame_pool/timeline_gpu_compositor/render_cache_adapter/engine/adaptive_quality/source_resolver/audio_ring_buffer/avsync_controller 的公共符号与磁盘模块一一存在，`__all__` 与 import 一致，无缺失/多余导出。

---

## src/features/edit/core/realtime_playback/adaptive_quality.py (行数: 271)

### 发现

（无）

### 通过项
- 线程安全：无跨线程共享；设计上由 engine 播放 tick 单线程调用（文档明确），无锁需求。
- 状态机正确性：迟滞（down_streak/up_streak）、最小驻留（min_dwell_frames，`_step` 内清零、`observe` 内自增）逻辑自洽；`reset`/`set_enabled`/`_force_normal` 回退路径对称。
- 异常与取消路径：无 I/O、无外部调用，纯决策逻辑，无可失败路径。
- 输入验证：`max(1, int(...))` / `max(0, int(...))` 归一化所有构造参数；`level_from_value` 对未知值返回 None。
- 性能模式：档位/`scale`/`rank` 为字典查表，O(1)。
- 类型注解完整。

---

## src/features/edit/core/realtime_playback/audio_ring_buffer.py (行数: 262)

### 发现

- [AUD-17320] [Suggestion] write() 通道数不匹配时静默截断多余通道，无日志无统计
  - 位置: src/features/edit/core/realtime_playback/audio_ring_buffer.py:132-137
  - 证据:
    ```python
    n = samples.shape[-1]
    # 通道不匹配时广播/截断
    if samples.shape[0] != self._channels:
        if samples.shape[0] == 1:
            samples = np.repeat(samples, self._channels, axis=0)
        else:
            samples = samples[:self._channels]
    ```
  - 影响: 调用方误传 5.1/6ch 素材数据时右侧声道被无声丢弃，没有任何 underrun/overflow 之外的观测点，混音错误只能靠听感发现。
  - 建议: 截断分支记一次 `logger.warning` 或新增 `channel_truncations` 统计项；或在首次写入时锁定通道数并拒绝不匹配批次。
  - 置信度: 高

### 通过项
- 线程安全：`threading.RLock` 覆盖全部读写/统计/清空路径；decode 线程写、audio 输出线程读的两段 slice 拷贝全部在锁内。
- 环形数学正确性：逐一验证了三条路径——满容量整批覆盖（write_pos==read_pos 时覆写最旧 n 个、read 紧跟 write，线性序保持 O_n..O_{C-1},N_0..N_{n-1}）、部分溢出（read_pos 前移 overflow_n）、超容量截断保留尾部——指针与 `_filled` 均一致，无错序/丢样本。
- 性能模式：两段向量化 slice 替代逐样本循环；fade-in 也是向量化 gain 曲线。
- 资源释放：`read`/`peek` 返回独立分配数组，无 buffer 别名泄漏给调用方。
- clear/apply_fade_in 均在锁内、环形段处理正确。
- 类型注解完整（返回值、参数均有注解）。

---

## src/features/edit/core/realtime_playback/avsync_controller.py (行数: 213)

### 发现

- [AUD-17325] [Suggestion] 模块头注释的阈值描述与实现常量不一致（80ms vs 150ms）
  - 位置: src/features/edit/core/realtime_playback/avsync_controller.py:8-11（注释）与 74-76、130-152（实现）
  - 证据:
    ```python
    # 核心策略（规格书 §15.2）：
    # - drift < 20ms：忽略（正常抖动）
    # - 20ms ≤ drift < 80ms：视频轻微 hold（不主动 drop，让 audio 追）
    # - drift ≥ 80ms：rebuffer（暂停视频推进，等音频填满到合理水位）
    ```
    而实现为：
    ```python
    DEFAULT_IGNORE_MS: float = 20.0
    DEFAULT_HOLD_MS: float = 80.0
    DEFAULT_REBUFFER_MS: float = 150.0
    ...
    elif drift_ms >= self._rebuffer_ms:
        action = AVSyncAction.REBUFFER
    elif frame_diff > 0:
        action = AVSyncAction.DROP
    ```
  - 影响: 注释声称 20–80ms 区间"不主动 drop"，实现里该区间 audio 领先即 DROP；注释声称 ≥80ms rebuffer，实现是 ≥150ms。按注释调参或写测试会得到与实现相反的预期。
  - 建议: 以实现为准更新模块头注释（区间改为 ignore<20 / 20–150 按方向 hold|drop / ≥150 rebuffer），并注明与规格书 §15.2 的偏差原因。
  - 置信度: 高

### 通过项
- 线程安全：`threading.RLock` 覆盖统计更新与 `last_decision` 读写；`evaluate` 决策计算所用 `_fps`/阈值在构造后不可变，锁外读取安全。
- 构造输入验证：`max(1.0, fps)`，且强制 ignore < hold < rebuffer 单调。
- 决策逻辑：四分支互斥完备，方向语义（audio 领先 drop / 落后 hold）与 docstring 一致（除阈值数字外）。
- `reset_stats`/`stats` 均持锁；决策对象 frozen dataclass，不可变。
- 类型注解完整。

---

## src/features/edit/core/realtime_playback/clip_decode_stream.py (行数: 506)

### 发现

- [AUD-17310] [Major] P010/P016 位深回退正则不命中，10/16-bit HDR 帧被标记为 8-bit
  - 位置: src/features/edit/core/realtime_playback/clip_decode_stream.py:464-475
  - 证据:
    ```python
        if bit_depth <= 0:
            # DecodedFrame carries the authoritative format string rather
            # than a separate bit_depth field.  Avoid treating every uint16
            # plane as 16-bit: P010/yuv422p10le are 10-bit samples stored in
            # uint16 containers.
            match = re.search(
                r"(?:^|[^0-9])(10|12|14|16)(?:le)?$",
                pixel_format.lower(),
            )
            if match:
                bit_depth = int(match.group(1))
        bit_depth = bit_depth or 8
    ```
  - 影响: 正则要求捕获组前一个字符是非数字，`p010le`/`p016le` 中 "10"/"16" 前面是数字 "0"，永远不匹配（已用 python 实测：p010le→None, p016le→None, yuv422p10le→10）。注释明确声称支持 P010，实际失效。当解码帧没有显式 bit_depth 字段时，P010 10-bit / P016 16-bit 帧回落为 8；下游 GpuFramePool 把 `frame.bit_depth` 用作纹理格式选择与缓存 key（gpu_frame_pool.py:200 附近），位深错判会导致 10-bit 数据按 8-bit 格式解释/缓存复用错位——HDR 渲染错误（层次压缩/banding）。注：GpuDecodedFrame 自带 bit_depth 字段（gpu_frame.py:331），该回退仅在无字段的帧类型（如裸 DecodedFrame）上触发，故影响面收窄但缺陷真实。
  - 建议: 改取格式串末尾完整数字串再映射，如 `re.search(r"(\d+)(?:le)?$", fmt)` 后按 {8,10,12,14,16} 白名单取值（"p010le"→"010"→10，"bgr24"→24 不在白名单→8）；或显式前缀匹配 p010/p016。
  - 置信度: 高（正则行为实测复现；下游影响为中——取决于无 bit_depth 字段的 p010 帧是否实际出现）

- [AUD-17311] [Suggestion] _decode 的 read_next_frame 回退路径 `except Exception: pass` 完全静默
  - 位置: src/features/edit/core/realtime_playback/clip_decode_stream.py:292-298
  - 证据:
    ```python
        if cursor >= 0 and source_frame == cursor + 1:
            read_next = getattr(src, "read_next_frame", None)
            if callable(read_next):
                try:
                    return read_next()
                except Exception:
                    pass  # 回退到 read_frame
        # 非顺序：read_frame 内部会 seek
        try:
            return read_frame(source_frame)
        except Exception as e:
            logger.debug(f"read_frame({source_frame}) failed: {e}")
            return None
    ```
  - 影响: read_next 持续失败时每帧都走"异常→静默回退到带 seek 的 read_frame"，顺序播放优化悄悄退化，且没有任何 trace 可发现；与包级"miss 必须可观测"原则相悖。
  - 建议: `except Exception: logger.debug(..., exc_info=True)`，至少保留一次性降级标记。
  - 置信度: 高

- [AUD-17312] [Suggestion] _build_handle 把原始 timebase 单位的 pts 当作秒写入 pts_seconds
  - 位置: src/features/edit/core/realtime_playback/clip_decode_stream.py:434-437
  - 证据:
    ```python
        pts = getattr(decoded, "pts", None)
        pts_seconds = float(getattr(decoded, "time_seconds", 0.0) or 0.0)
        if pts is not None and pts_seconds == 0.0:
            pts_seconds = float(pts)
    ```
  - 影响: `pts` 通常是 timebase 计数（如 1/90000s 或流帧号），仅在 `time_seconds` 恰为 0.0（含真 0 点的帧）时被直接当秒使用，FrameHandle.pts_seconds 可能偏离数个数量级，污染下游基于 pts 的时间对齐/telemetry。
  - 建议: 该分支只做 `pts_seconds = 0.0` 或结合 metadata.time_base 换算；至少加注释声明单位假设。
  - 置信度: 中（pts 单位取决于上游解码器实现，未在本次批次文件内证实）

### 通过项
- 线程安全：`_decode_condition`（基于同一 RLock）串行化单流解码；foreground 请求可插队、prefetch 在 decode_active 或有 foreground 等待时让路；等待循环内复查 closed/ready/generation，无死锁路径（Condition 与锁同源，notify_all 均在持锁时调用）。
- GPU 资源生命周期：解码中 `set_source`/`close` 不直接关闭正在使用的 source，而是进入 `_deferred_source_closes`，由 `request_frame` 的 finally 在 decode_active 释放后统一关闭——正确避免 use-after-close；被拒绝/过期的 decoded payload 通过 `safe_release_gpu_frame` 释放。
- generation 守卫：decode 后与 ring.put 前双重检查 `gen != self._generation` / closed / cancel_event，seek flush、source 切换、close 的旧结果均被丢弃并释放。
- 帧正确性：exact 模式拒绝任何错帧、non-exact 也拒绝 mismatch>1（§25 wrong_source_frame_presented==0 的实现侧守卫）；GpuDecodedFrame 带 frame_index（gpu_frame.py:324），actual_index 检查有效。
- 取消路径：cancel_event 在入队前、等待中、decode 后三处检查。
- 性能模式：顺序播放 cursor+1 直接 read_next_frame 避免逐帧 seek。
- 输入验证：StreamKey frozen dataclass；capacity 归一化在 FrameRingBuffer 内完成。

---

## src/features/edit/core/realtime_playback/decode_manager_source.py (行数: 64)

### 发现

- [AUD-17315] [Major] read_frame 宽泛 except 吞掉解码管理器全部异常且零日志，真实错误伪装成 miss
  - 位置: src/features/edit/core/realtime_playback/decode_manager_source.py:41-52
  - 证据:
    ```python
        try:
            decoded, frame_type = self._manager.get_frame(
                self._key.source_path,
                int(frame_index),
                target_size=(int(self._key.target_width), int(self._key.target_height)),
                stream_key=(self._key.decoder_stream_key or self._key.clip_id),
            )
        except Exception:
            return None
    ```
  - 影响: EditGpuDecodeManager 内部的任何异常（会话失效、VT 错误、断言、编码 bug）都被折成 None，与"解码未就绪"的语义 miss 不可区分；没有任何 logging，违反 .clinerules"不得宽泛 except 掩盖领域错误"与本包"任何 miss 都必须可观测、可 trace"的设计原则——持续异常表现为无因可查的永久黑帧/hold。
  - 建议: 捕获后 `logger.warning("decode manager get_frame failed", exc_info=True)` 再返回 None；或按异常类型区分（可恢复的 IO/时序 miss 与不可恢复的会话错误分流上报）。
  - 置信度: 高

### 通过项
- CPU-packed 帧返回 None 的策略有明确文档且与 `_GPU_FRAME_TYPES` 白名单一致，防止 GPU 预览路径静默做不兼容转换。
- `close()` 幂等（重复调用安全），manager 会话所有权归属清晰（适配器不拥有会话）。
- `read_next_frame` 保持顺序语义（`_current_frame + 1` 经 read_frame 精确索引）。
- 类型注解完整；`manager is None` 防御存在。

---

## src/features/edit/core/realtime_playback/engine.py (行数: 1112)

### 发现

- [AUD-17300] [Major] 引擎状态字段的加锁策略不一致，多线程入口下存在数据竞争
  - 位置: src/features/edit/core/realtime_playback/engine.py:364-373（持锁）vs 640-646、652-673、701-707、814-833、1022-1045（无锁）
  - 证据: `set_mode` 持锁写状态：
    ```python
    def set_mode(self, mode: PlaybackMode) -> None:
        with self._lock:
            previous_mode = self._mode
            self._mode = mode
            if mode is PlaybackMode.REALTIME:
                if self._state in (EngineState.READY, EngineState.PAUSED, EngineState.REBUFFERING):
                    self._set_state(EngineState.PLAYING)
    ```
    而 `seek`/`_set_state`/`_handle_miss` 完全无锁读写同一组字段：
    ```python
    def _set_state(self, new_state: EngineState) -> None:
        if self._state is new_state:
            return
        old = self._state
        self._state = new_state
        self.state_changed.emit(new_state.value)
    ...
    should_rebuffer = self._policy.should_rebuffer(
        miss,
        consecutive_miss_count=self._consecutive_misses,   # 无锁读，写侧持锁
        threshold=self._rebuffer_threshold,
    )
    ```
  - 影响: engine 明确存在跨线程入口——worker 线程调用 `note_playback_miss`（文档注明）、渲染线程调用 `sync_generation_id`、UI 线程驱动 tick 与 controller 信号。GIL 下不会崩溃，但 `_state`/`_consecutive_misses`/`_generation_id` 的无锁读可读到旧值：rebuffer 判定阈值用旧 miss 计数（误触发或永不触发）、seek 与 tick 并发时 `_set_state(PLAYING)` 覆盖 WARMING 等。另外 `shutdown()` 也在无锁下清空 `_renderer/_delegate_render/_source_resolver` 等引用，与在途 tick 竞争。
  - 建议: 统一为 `_state/_mode/_consecutive_misses/_generation_id` 的所有读写都走 `self._lock`（把 `seek`/`_set_state`/`_handle_miss` 的读改写移入锁内），或明确文档化"仅主线程驱动"并移除半套锁。
  - 置信度: 中（竞争窗口确定存在；实际触发依赖 tick 与 controller/worker 是否真的跨线程并发）

- [AUD-17301] [Suggestion] set_mode 在持有 self._lock 期间发射 state_changed 信号，锁内执行直连槽代码
  - 位置: src/features/edit/core/realtime_playback/engine.py:366-373 + 640-646
  - 证据:
    ```python
        with self._lock:
            previous_mode = self._mode
            self._mode = mode
            if mode is PlaybackMode.REALTIME:
                if self._state in (...):
                    self._set_state(EngineState.PLAYING)   # 内部 state_changed.emit(...)
    ```
    `_set_state` 最后一行 `self.state_changed.emit(new_state.value)` 在锁未释放时执行。
  - 影响: DirectConnection 槽同步运行任意订阅者代码（可能回调 `engine.stats()`、等待 worker）；若槽内同步等待的 worker 正阻塞在 `note_playback_miss`/`sync_generation_id` 的 `self._lock` 上，构成 ABBA 式死锁；至少也是锁持有时间不受控。
  - 建议: 先在锁内计算目标状态并暂存，出锁后再 emit。
  - 置信度: 中

- [AUD-17302] [Suggestion] _execute_delegate 旧签名 fallback 以 `except TypeError` 判定签名不兼容，会把 delegate 内部业务的 TypeError 误判并丢弃 decisions 重试
  - 位置: src/features/edit/core/realtime_playback/engine.py:915-927
  - 证据:
    ```python
                # 兼容旧签名（4 参）的 fallback：只在 delegate 接受 keywords 时传。
                try:
                    return bool(
                        delegate(
                            timeline_frame, generation_id, deadline_ns, source_decisions,
                        )
                    )
                except TypeError:
                    # 旧 3 参 delegate：直接忽略 decisions。
                    return bool(delegate(timeline_frame, generation_id, deadline_ns))
    ```
  - 影响: 4 参 delegate 业务体内抛出的 TypeError 会被误认为签名不匹配，落到 3 参调用再次 TypeError，最终错误日志显示的是第二次调用的栈，原始异常被掩盖（诊断误导）。代码注释（902-904）已对 trace_id 分支明确禁止这种"静默重试"，旧签名分支仍保留该反模式。
  - 建议: 在 configure 探测签名时同时记录参数个数（inspect 已在用），运行时按探测结果选择调用形态，彻底去掉 except TypeError 分派。
  - 置信度: 高（行为确定，影响为可诊断性）

- [AUD-17303] [Suggestion] request_scrub_present 记录 telemetry 但不发射 telemetry_recorded 信号
  - 位置: src/features/edit/core/realtime_playback/engine.py:876-884（对照 request_present 的 807-808）
  - 证据:
    ```python
        self._telemetry.record(PlaybackFrameTelemetry(
            timeline_frame=timeline_frame,
            trace_id=trace_id,
            mode="scrub",
            priority="scrub_latest",
            total_ms=elapsed_ms,
            miss_reason=None if ok else "scrub_dispatch_rejected",
        ))
        return ok
    ```
    request_present 对应位置是 `self._telemetry.record(trace)` 后紧跟 `self.telemetry_recorded.emit(trace)`。
  - 影响: 订阅 `telemetry_recorded` 的消费者（帧 trace 对拍/调试面板）看不到 scrub 帧，scrub 通道成为观测盲区，与 P0-01"统一 trace id 跨通道可对拍"的目标不一致。
  - 建议: 补 `self.telemetry_recorded.emit(trace)`（或注释说明刻意不发的原因）。
  - 置信度: 高

- [AUD-17304] [Suggestion] back-pressure 分支的 _handle_miss 会消费并顶替 worker 上报的 miss 归因槽
  - 位置: src/features/edit/core/realtime_playback/engine.py:716-723 + 1007
  - 证据:
    ```python
        if mode is PlaybackMode.REALTIME and not self._budget.before_render(timeline_frame):
            # back-pressure：drop this frame
            ...
            self._handle_miss(timeline_frame, gen, reason="back_pressure")
            # P1-10: 背压 drop 属负载观测（miss_reason=back_pressure）。
            self._observe_adaptive_quality(False, "back_pressure")
    ```
    `_handle_miss` 第一行 `resolved_reason = self._resolve_miss_reason(generation_id, reason)` 会取出同 generation 的 `_noted_miss`（若有），使 rebuffer 警告用 worker 归因、adaptive quality 用 "back_pressure"，同一帧两种归因。
  - 影响: 低——归因偶发错位、noted 槽被预算事件吞掉，telemetry 与 warning 的原因可能不一致一次。
  - 建议: back-pressure 分支直接以 "back_pressure" 构造决策，不经过 `_resolve_miss_reason`（或在该分支先清槽再消费）。
  - 置信度: 高（代码路径确定）

- [AUD-17305] [Suggestion] get_default_engine() 单例 check-then-act 无锁
  - 位置: src/features/edit/core/realtime_playback/engine.py:1104-1112
  - 证据:
    ```python
    _default_engine: Optional[RealtimePlaybackEngine] = None

    def get_default_engine() -> RealtimePlaybackEngine:
        global _default_engine
        if _default_engine is None:
            _default_engine = RealtimePlaybackEngine()
        return _default_engine
    ```
  - 影响: 两个线程首次并发获取会各建一个 engine（QObject），后续不同消费者持有不同实例，状态机/信号桥分叉，且多余 QObject 悬挂。
  - 建议: 模块级 `threading.Lock` 双重检查，或在应用启动时主线程预创建。
  - 置信度: 高（竞争确定存在；实际触发概率低）

- [AUD-17306] [Suggestion] configure_adaptive_quality 直接改写 AdaptiveQualityController 私有字段
  - 位置: src/features/edit/core/realtime_playback/engine.py:428-433
  - 证据:
    ```python
        if down_streak is not None:
            controller._down_streak = max(1, int(down_streak))
        if up_streak is not None:
            controller._up_streak = max(1, int(up_streak))
        if min_dwell_frames is not None:
            controller._min_dwell_frames = max(0, int(min_dwell_frames))
    ```
  - 影响: 封装穿透；控制器后续重构（改名/加联动不变量）会静默破坏 engine。
  - 建议: 给 AdaptiveQualityController 增加 `configure(down_streak=..., up_streak=..., min_dwell_frames=...)` 方法。
  - 置信度: 高

- [AUD-17307] [Suggestion] 预算记账不对称：before_render 仅 REALTIME 调用，after_render 全模式调用
  - 位置: src/features/edit/core/realtime_playback/engine.py:716 与 785-787
  - 证据:
    ```python
        if mode is PlaybackMode.REALTIME and not self._budget.before_render(timeline_frame):
            ...
    ...
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        # 记录预算耗时（before/after）
        self._budget.after_render(timeline_frame, render_start_ns, presented=presented)
    ```
  - 影响: PAUSED/step 等非 REALTIME 的 request_present tick 不经 before_render（不登记 in-flight）却调用 after_render——`after_render` 无条件清空 `_in_flight_start/_in_flight_frame` 并把该帧耗时计入预算历史（playback_budget.py:153-180）。模式切换瞬间的 paused 单帧渲染会提前清掉在途 REALTIME 帧的 in-flight 标记并污染 over-budget 统计，可能诱发误 back-pressure。
  - 建议: after_render 同样以 `mode is PlaybackMode.REALTIME` 为门槛，或让 budget 忽略未登记帧号。
  - 置信度: 中（不对称确定存在；实际影响取决于非 REALTIME tick 是否与 REALTIME 在途帧交错）

### 通过项
- generation 守卫统一：`seek`/`sync_generation_id` 均同步 scheduler 的 `reset_generation`，外部（序列/工程变更）代际也能作废 ring；`_consecutive_misses` 在代际变化时清零。
- miss 归因单槽设计自洽：`note_playback_miss` 旧代际丢弃、`_resolve_miss_reason` 消费即清空，request_present 与 _handle_miss 二次 resolve 结果一致（注释 789-791 论证成立）。
- 信号防递归：`_on_playback_controller_state_changed` 用 `previous != is_playing` 边沿触发 `playback_active_changed`，重复 controller 通知不会重放 prewarm/quality 副作用；`bind_playback_controller` 重绑时先断开旧信号，避免双桥。
- 异常路径：delegate/provider/scheduler 调用均有 try/except 且逐点记日志（logger.error/debug），miss 不静默 CPU fallback（hold/rebuffer 语义保留）。
- 性能模式：热路径无 inspect（configure 一次性探测 trace_id 签名）、无 av.open/磁盘代理触发（proxy 生成明确排除在 tick 外，763-766 注释）、变速 lookahead 有 32 帧上限与 min(rate,8) 钳制。
- 状态机：IDLE/WARMING/READY/PLAYING/REBUFFERING/PAUSED/SHUTDOWN 转换在 presented/miss/seek 路径上闭合，无未定义转换。
- shutdown 路径：断开 controller 信号、清空全部委托引用、SHUTDOWN 态 request_present 直接拒绝。
- 类型注解基本完整（stats 等返回 dict 为聚合容器，可接受）。

---

## src/features/edit/core/realtime_playback/frame_ring_buffer.py (行数: 211)

### 发现

（无）

### 通过项
- 线程安全：RLock 覆盖 put/get/take/contains/peek/evict/clear/stats 全部路径。
- GPU 资源生命周期：被覆盖、容量淘汰、窗口淘汰、clear 的句柄统一收集后在锁外 `_release_handle`（safe_release_gpu_frame），且释放失败仅 debug 不打断 decode 生产者；`take_exact` 提供所有权转移语义，避免 compositor 与 ring 重复持有 external 资源。
- LRU 语义：put 覆盖时 move_to_end 并释放旧句柄；get_exact 命中刷新 recency；容量淘汰 popitem(last=False) 淘汰最旧。
- 统计完整（hits/misses/puts/evicts/flushes/hit_rate），reset_stats 持锁。
- 类型注解完整。

---

## 汇总

| 严重度 | 数量 | 编号 |
|---|---|---|
| Critical | 0 | — |
| Major | 3 | AUD-17300, AUD-17310, AUD-17315 |
| Suggestion | 11 | AUD-17301–17307, AUD-17311, AUD-17312, AUD-17320, AUD-17325 |

重点（Major）：
1. AUD-17300 engine.py 状态字段加锁不一致，跨线程入口下状态机/miss 计数存在数据竞争。
2. AUD-17310 clip_decode_stream.py P010/P016 位深回退正则失效，10/16-bit HDR 帧可能被标为 8-bit（已实测复现）。
3. AUD-17315 decode_manager_source.py 宽泛 except 零日志吞掉解码管理器异常，真实错误伪装成 miss 且不可观测。
