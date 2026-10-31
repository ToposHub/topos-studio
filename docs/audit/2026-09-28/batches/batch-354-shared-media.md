# 批次报告：batch-354 [P2] src/shared/media/

- 编号区间：AUD-35400–AUD-35499
- 审查文件：metadata.py (571)、motion_graphic_delivery.py (124)、performance_monitor.py (1,782)
- 严重度统计：Critical 0 · Major 4 · Suggestion 8

---

## src/shared/media/metadata.py (行数: 571)

### 发现

- [AUD-35400] [Major] Rational.from_string 不校验外部字符串：多余分段被静默丢弃、畸形输入抛裸 ValueError
  - 位置: src/shared/media/metadata.py:202-207
  - 证据:
    ```python
    s = s.strip()
    if '/' in s:
        parts = s.split('/')
        return Rational(int(parts[0]), int(parts[1]))
    else:
        return Rational.from_float(float(s))
    ```
  - 影响: 该方法被用于解析持久化时间轴 JSON（src/features/edit/core/timeline_v2.py:3449 `Rational.from_string(data.get("fps", "24/1"))`）与外部转录服务返回的 fps 串（src/features/edit/ai_editing/transcript_provider.py:943 等）。`"24/1/2"` 会静默解析为 Rational(24,1)（第三段被丢弃），产出错误帧率导致时间轴错位/音画不同步；`"abc"`、`""`、`"24/"` 则抛出无上下文的裸 ValueError，可致加载流程崩溃。违反"外部数据须验证"铁律。
  - 建议: 校验 `len(parts) == 2`，对 int()/float() 失败捕获并以带原始串上下文的 ValueError 重新抛出。
  - 置信度: 高

- [AUD-35401] [Major] VideoMetadata 未验证 fps/SAR 为正，duration_seconds 与 display_size 存在除零崩溃路径
  - 位置: src/shared/media/metadata.py:279-290, 300-304, 317-326
  - 证据:
    ```python
    def __post_init__(self) -> None:
        """验证元数据有效性"""
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"Invalid dimensions: {self.width}x{self.height}")
        if self.bit_depth not in (8, 10, 12, 14, 16, 32):
            raise ValueError(f"Invalid bit depth: {self.bit_depth}")
    ```
    ```python
    if self.total_frames is None:
        return None
    return self.total_frames / self.fps.to_fps()
    ```
    ```python
    else:
        # 非方形像素，垂直拉伸
        return (self.width, int(self.height / sar))
    ```
  - 影响: `__post_init__` 不检查 `fps.num > 0` 与 `sample_aspect_ratio` 为正。`Rational(0, 1)` 是合法实例（如 topos 源从持久化/外部数据解析出的 "0/1"），构造 VideoMetadata 后访问 `duration_seconds` 触发 ZeroDivisionError；SAR 为 0 时 `display_size` 同样除零。ffmpeg_source.py 的 `_extract_fps` 依赖 `if rate` 的偶然真值判断挡住了 0 分子，但 topos_source/factory 等其他构造点无此保护。
  - 建议: `__post_init__` 增加 `self.fps.num <= 0` 与 `sample_aspect_ratio is not None and num <= 0` 的显式校验。
  - 置信度: 高（代码路径确定；触发依赖畸形元数据，故未评 Critical）

- [AUD-35402] [Suggestion] Rational 算术不完备：缺 `__rmul__`/`__rtruediv__`，float 乘法用 int() 截断
  - 位置: src/shared/media/metadata.py:146-155
  - 证据:
    ```python
    def __mul__(self, other: int | float | Rational) -> Rational:
        """乘法运算"""
        if isinstance(other, int):
            return Rational(self.num * other, self.den)
        elif isinstance(other, float):
            # 浮点数乘法，转为有理数近似
            return Rational(int(self.num * other * 1000), self.den * 1000)
    ```
  - 影响: 未定义 `__rmul__`，`2 * Rational(24, 1)` 直接 TypeError（int.__mul__ 返回 NotImplemented 后无反射方法可走）；float 乘法经 `int()` 向零截断而非四舍五入，累积精度损失。当前库内未发现反向标量乘法调用，故仅 Suggestion。
  - 建议: 补 `__rmul__ = __mul__`、`__rtruediv__`；float 分支改用 `Fraction(other).limit_denominator()`（模块已导入 Fraction）。
  - 置信度: 高

- [AUD-35403] [Suggestion] audio_tracks 使用裸 tuple 注解，丢失元素结构类型
  - 位置: src/shared/media/metadata.py:277
  - 证据:
    ```python
    # {name, codec, sample_rate, channel_count, channel_layout,
    #  bits_per_sample, sample_format, priming_samples}。旧 audio_* 字段
    # 保持为首轨视图（轨 0），兼容既有消费方；无音轨为空元组。
    audio_tracks: tuple = ()
    ```
  - 影响: 每项结构仅存在于注释中，IDE/类型检查器无法校验消费方取键，字段拼写错误无法静态发现。
  - 建议: 定义 `AudioTrackInfo` dataclass 并注解为 `tuple[AudioTrackInfo, ...]`（或最低限度 `tuple[dict, ...]`）。
  - 置信度: 高

### 通过项
- 线程安全：全部为 `frozen=True` 不可变 dataclass，跨线程共享安全
- GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性：纯数据模型，不涉及（is_hdr 基于 smpte2084/arib-std-b67 判定正确）
- Rational 自身验证：den==0 抛 ValueError、den<0 符号规范化、gcd 约分均正确；`__truediv__` 除以 0 经构造器抛错而非静默
- 异常与取消路径：无吞异常，验证失败显式抛 ValueError
- 预定义帧率常量数值正确（24000/1001、30000/1001 等）
- format_codec_label 边界（双空、单边、ProRes profile 映射、已知前缀去重、旧缓存 'Image ' 前缀剥离）逻辑自洽

---

## src/shared/media/motion_graphic_delivery.py (行数: 124)

### 发现

（无）

### 通过项
- 通过（无逻辑问题）：纯常量 + 纯函数，无状态、无线程/GPU/信号/undo 维度
- 输入验证：`motion_graphic_preview_size` 对 0/负值经 `max(1, int(...))` 钳制并保证偶数尺寸（yuv420p 编码要求）；`motion_graphic_quality_preset`/`describe_motion_graphic_quality` 对 None/未知 tier 回退默认档；`motion_graphic_bitrate_kbps` 的 `max(reference, requested, scaled)` 保证不低于基准码率
- 类型注解完整，`__all__` 与导出一致

---

## src/shared/media/performance_monitor.py (行数: 1,782)

### 发现

- [AUD-35404] [Major] 会话内统计列表无界增长：长播放会话内存持续膨胀且每秒聚合排序在 RLock 内执行
  - 位置: src/shared/media/performance_monitor.py:443-454（及 464/481/497/509/526/537/551/567/577/590/604/614/624/634 各 append 点），对比 849-852 唯一有上限的列表
  - 证据:
    ```python
    with self._lock:
        self._tick_times.append(tick_ms)

        # 检查是否需要输出聚合日志
        self._check_aggregate_log()
    ```
    ```python
    with self._lock:
        self._total_frame_times.append(total_ms)
        # 只保留最近 120 帧的数据
        if len(self._total_frame_times) > 120:
            self._total_frame_times = self._total_frame_times[-120:]
    ```
  - 影响: `_tick_times`（每 UI tick 调用，见 video_playback.py:4639/4748/4760/4772）、`_process_times`、`_render_submit_times`、`_queue_depths`、`_renderer_queue_depths`、`_held_frames`、`_cache_hits`、`_frame_paths`、`_decode_times`、`_upload_times` 等仅在 start_session/clear 时清空，会话内无任何上限。小时级 60fps 会话将累积数十万元素（每个 VideoPlaybackStats 含 25+ 字段），且 `_check_aggregate_log` 每秒在持锁状态下对全量数据 sorted/求和，锁持有时间随时长线性增长，阻塞解码与播放线程——与模块宣称的"低开销"相悖。
  - 建议: 仿照 `_total_frame_times` 的 120 帧截断，将各统计列表改为 `collections.deque(maxlen=N)` 环形缓冲。
  - 置信度: 高

- [AUD-35405] [Major] clear()/reset() 清理不完整：清空后聚合统计返回陈旧数据，降级级别与早退返回值不一致
  - 位置: src/shared/media/performance_monitor.py:1416-1439（clear）、1441-1476（reset），对照 start_session 308-364；早退点 728-729
  - 证据:
    ```python
    def clear(self) -> None:
        ...
        with self._lock:
            self._decode_times.clear()
            self._upload_times.clear()
            self._frame_drops.clear()
            self._total_frames = 0
            ...
            self._cache_hits.clear()
            self._last_aggregate_time = time.monotonic()
    ```
    ```python
    # 需要有足够的数据（至少 10 个 tick）
    if len(self._tick_times) < 10:
        return 0
    ```
  - 影响: `clear()` 未清理 `_renderer_queue_depths`、`_dropped_preview_frames`、`_held_frames`、`_plane_prepare_times`、`_texture_write_times`、`_yuv_render_times`、`_frame_paths`、零拷贝计数、`_decode_path_counts`、`_render_frame_path_counts`、`_degradation_level`、`_prefetch_lookahead_reduction`、`_total_frame_times`、代理阶梯状态；`reset()` 同样遗漏全部 S5.B/YUV/零拷贝/降级字段。生产调用点 video_playback.py:4905（`reset_perf_stats`）触发后：`get_aggregate_stats()`（video_playback.py:4886/HUD）仍上报旧 renderer_queue/zero_copy/proxy_ladder 数据；更严重的是 tick_times 清空后 `check_tick_performance_and_degrade()` 走 `< 10` 早退恒返回 0，而 `_degradation_level` 保持旧值，`get_degradation_level()`/`get_prefetch_lookahead_reduction()` 与之不一致，预读削减可能滞留。
  - 建议: 提取 `_clear_session_data()` 私有方法，clear()/reset()/start_session() 统一调用，保证字段清理对齐。
  - 置信度: 高

- [AUD-35406] [Suggestion] check_tick_performance_and_degrade 存在未使用变量 current_time，注释阈值与实现不符
  - 位置: src/shared/media/performance_monitor.py:725, 744-746
  - 证据:
    ```python
    with self._lock:
        # 计算最近 1 秒的 tick p95
        current_time = time.monotonic()
    ```
    ```python
    # 检查是否连续超过阈值超过一定次数（约 1 秒）
    # 假设 tick 间隔约 16ms (60fps)，60 次 ≈ 1 秒
    if self._consecutive_slow_ticks >= 30:
    ```
  - 影响: `current_time` 计算后从未使用（死变量）；注释称"60 次 ≈ 1 秒"，代码实际 30 次（约 0.5 秒），文档与行为不符会误导后续调参。
  - 建议: 删除死变量；修正注释为 30 次 ≈ 0.5 秒（或按真实间隔用时间戳判定）。
  - 置信度: 高

- [AUD-35407] [Suggestion] check_proxy_ladder 含永不可达的 elif 分支与死变量 frame_budget_ms，docstring 判定口径与实现不符
  - 位置: src/shared/media/performance_monitor.py:899-915
  - 证据:
    ```python
    # 帧预算：默认 30fps = 33.3ms
    frame_budget_ms = 33.3
    ...
    if self._consecutive_slow_frames >= 60:
        # 需要降一级
        if current_index < len(PROXY_LADDER_RUNGS) - 1:
            new_index = current_index + 1 if current_index >= 0 else 0
            reason = f"slow_frames={self._consecutive_slow_frames},total_p95={total_p95:.1f}ms"
        elif current_index == -1:
            # 从原始分辨率降到第一级阶梯
            new_index = 0
    ```
  - 影响: `current_index == -1` 时首分支条件 `-1 < 3` 恒真并已将 new_index 置 0，`elif current_index == -1` 永不可达（死代码）；`frame_budget_ms` 赋值后从未参与判定（total_p95 仅进入 reason 字符串），实际升降级由 `track_frame_total_time` 内逐帧比较驱动的计数器决定，与 docstring 所述"total_p95 > frame_budget * 1.25 连续 60 帧"口径不一致。
  - 建议: 删除死分支与死变量，或真正实现 p95 对帧预算的判定；同步修正 docstring。
  - 置信度: 高

- [AUD-35408] [Suggestion] track_zero_copy_frame 中 `fallback_reason or "non_zero_copy_path"` 恒取右值，or 表达式冗余
  - 位置: src/shared/media/performance_monitor.py:681-684
  - 证据:
    ```python
    if fallback_reason:
        self._last_fallback_reason = fallback_reason
    elif not is_zero_copy:
        self._last_fallback_reason = fallback_reason or "non_zero_copy_path"
    ```
  - 影响: 进入 elif 时 `fallback_reason` 必为 falsy，`or` 恒返回 "non_zero_copy_path"，表达式一半是死逻辑，暗示作者原意可能是其他默认值，易在维护中引入误解。
  - 建议: 简化为 `self._last_fallback_reason = "non_zero_copy_path"` 并加注释。
  - 置信度: 高

- [AUD-35409] [Suggestion] get_aggregate_stats 对同一数据重复执行 list 拷贝与排序（decode/upload 各 4 次），且每秒在锁内运行
  - 位置: src/shared/media/performance_monitor.py:1059-1062, 1069-1072（另 1081-1082/1089-1090/1094-1095/1100-1112 同模式）
  - 证据:
    ```python
    'decode_avg_ms': self._calculate_stats(list(self._decode_times.values()))['avg'],
    'decode_p50_ms': self._calculate_stats(list(self._decode_times.values()))['p50'],
    'decode_p95_ms': self._calculate_stats(list(self._decode_times.values()))['p95'],
    'decode_max_ms': self._calculate_stats(list(self._decode_times.values()))['max'],
    ```
  - 影响: 每个指标独立调用 `_calculate_stats`（内含 sorted + 两次遍历），同一 dict 被拷贝、排序各 4 次；该函数经 `_check_aggregate_log → _output_aggregate_log` 每 1 秒在已持有的 RLock 内执行，与 AUD-35404 的无界增长叠加后显著放大监控器自身开销。
  - 建议: 每类数据取一次 `values = list(...)`、调用一次 `_calculate_stats` 后复用结果字典。
  - 置信度: 高

- [AUD-35410] [Suggestion] record_playback_stats/get_playback_stats 为全库无调用方的死 API，且与 track_decode_time 双重累计 _total_frames
  - 位置: src/shared/media/performance_monitor.py:963-975，对照 387-389
  - 证据:
    ```python
    with self._lock:
        self._playback_stats.append(stats)
        self._total_frames += 1
    ```
    ```python
    with self._lock:
        self._decode_times[frame_index] = decode_ms
        self._total_frames += 1
    ```
  - 影响: grep src/ 与 tests/ 均无 `record_playback_stats`/`get_playback_stats` 调用点（纯死代码）；若未来被采用且与逐帧 `track_decode_time`（playback_clock.py:589/609/665/705 每帧调用）并存，`_total_frames` 将双倍计数，污染 `frames_processed`/`effective_fps`/`drop_rate`。
  - 建议: 删除该 API，或文档注明二者互斥并在入口去重计数。
  - 置信度: 高

- [AUD-35411] [Suggestion] 六个计时上下文管理器缺返回类型注解；_calculate_stats 返回注解与 'count' 实际类型不符
  - 位置: src/shared/media/performance_monitor.py:1601, 1624, 1651, 1674, 1698, 1721；1506
  - 证据:
    ```python
    @contextmanager
    def track_decode_time(monitor: VideoPerformanceMonitor, frame_index: int):
    ```
    ```python
    def _calculate_stats(self, values: List[float]) -> Dict[str, float]:
    ```
  - 影响: 上下文管理器无 `-> Iterator[None]` 注解（需 `from typing import Iterator`，当前仅导入 Optional/Dict/List/Tuple/Any）；`_calculate_stats` 声明 `Dict[str, float]` 但 `'count': n` 为 int，类型检查误报/漏报。
  - 建议: 补齐返回注解并将 count 拆出或改注解为 `Dict[str, float | int]`。
  - 置信度: 高

### 通过项
- 线程安全：所有 track_*/get_*/状态方法均在 `threading.RLock` 内访问共享状态；`_check_aggregate_log → get_aggregate_stats` 的锁重入因选用 RLock 而无死锁；全局单例 `get_performance_monitor` 双重检查锁定正确
- GPU 资源生命周期：不持有任何 ModernGL/纹理资源
- GLSL·HDR / 信号防递归 / 状态提交一致性：不涉及（无着色器、无 Qt 信号、无 undo/revision 耦合）
- 异常与取消路径：全部六个 @contextmanager 均以 try/finally 包裹，异常路径下耗时仍被记录
- 输入验证（部分）：`set_aggregate_log_interval` 钳制到 [0.5, 60]；`set_original_resolution` 的非正值经 `_original_long_edge <= 0` 检查安全降级为 "no_video"
- `_calculate_stats` 的 p50/p95 索引均有 `min(idx, n-1)` 或等效边界保护，无 IndexError 路径
- 降级/恢复滞回（0.5s 降级、3s 逐级恢复）与 `_update_lookahead_reduction` 映射表自洽
