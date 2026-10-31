# batch-394 审查报告 [P2] src/shared/video//

编号区间：AUD-39400–AUD-39499。审查日期：2026-09-28。
四个文件全部 Read 通读（长文件分两次读完），跨文件事实（回调挂接、断言行为、消费者）已用只读 grep/sed 核实。

---

## src/shared/video/playback_state.py (行数: 1,023)

### 发现

- [AUD-39400] [Major] 状态回调在持有 `_session_lock` 时同步触发，Qt 直连槽一旦重入协调器属性即自死锁
  - 位置: src/shared/video/playback_state.py:403-423（load）、459-464（unload）、490-495（start_playback）
  - 证据:
    ```python
    with self._session_lock:
        ...
        result = self._session.load(path)   # session 内部会同步触发 _notify_state_change
    ```
    session.load 在 LOADING/READY/ERROR 三处同步回调（session.py:416、526、541）；协调器回调 `_on_session_state_change`（playback_state.py:965-978）直接调用外部回调；实际挂接的回调 `EnhancedVideoPlaybackEngine._on_coordinator_state_change` 同步 `play_state_changed.emit(...)`（video_playback.py:2596-2605），直连槽（layer_controller.py:1720、file_controller.py:5404、canvas.py:3112）在调用线程内联执行。
  - 影响: 任一直连槽查询协调器属性（`current_frame`/`is_playing`/`pause()` 等都会 `with self._session_lock`）即在 UI 线程上与非重入 `threading.Lock` 死锁，界面永久冻结。当前已挂接的槽（图标更新、音频桥读取普通属性 `engine.current_frame`）不重入，属潜伏风险；本文件 `get_video_info`（885-888 行）的注释已明确承认这一死锁模式。
  - 建议: 把 session 层的 V-P0-11 模式上移到协调器层——`_session_lock` 只保护 session 引用的读写，命令执行（含其内部回调链）移到锁外；或回调统一经 QueuedConnection/延迟 emit 投递。
  - 置信度: 高（锁范围与回调链确定；触发依赖槽实现，故列 Major 而非 Critical）

- [AUD-39401] [Major] 加载失败后 `is_loaded` 仍返回 True（ERROR 状态未被排除）
  - 位置: src/shared/video/playback_state.py:288-294
  - 证据:
    ```python
    @property
    def is_loaded(self) -> bool:
        """是否已加载视频"""
        with self._session_lock:
            if self._session is not None:
                from .session import PlaybackState
                return self._session.state != PlaybackState.UNLOADED
            return False
    ```
    session.load 失败后 `_state = PlaybackState.ERROR` 且协调器不清空 `self._session`（session.py:535-542；playback_state.py:403-440）。
  - 影响: 消费者 video_playback.py:3079 的 `COORDINATOR_LOAD_FAILED` 分支与 3612 的 `SESSION_INVALID` fallback 分支被跳过，引擎继续走 start_playback 再被 session 拒绝，错误处理路径失真（返回状态与真实失败原因不一致）。
  - 建议: `is_loaded` 改为枚举白名单（READY/PLAYING/PAUSED/SEEKING/SCRUBBING/STOPPING）；或协调器在 `load()` 返回 False 时置 `self._session = None`。
  - 置信度: 高

- [AUD-39402] [Suggestion] `_session_lock` 在阻塞操作全程被持有（300ms 轮询 / 最长 2s）
  - 位置: src/shared/video/playback_state.py:604-638（blocking seek → `get_master_frame_blocking` 300ms 轮询 + 超时后同步解码 fallback）、442-475（unload，timeout=2.0）、389-440（load 全程）
  - 证据:
    ```python
    with self._session_lock:
        if self._session is None:
            return False
        result = self._session.seek_to_frame(
            frame, blocking=blocking, churn_generation=churn_generation
        )
    ```
  - 影响: 持锁期间所有协调器属性、`tick()`、`get_stats()` 在其他线程上被阻塞；监控/统计线程最坏可卡 2 秒，播放 tick 被推迟造成卡顿。
  - 建议: 锁内只读取 session 引用到局部变量，长操作在锁外用该引用执行。
  - 置信度: 高（锁范围确定；实际争用取决于是否存在跨线程调用方）

- [AUD-39403] [Suggestion] `_tick_callback`/`_tick_lock` 为死代码；`cleanup()` 不清空 tick 回调且回调字段写入不持锁
  - 位置: src/shared/video/playback_state.py:258-260、937-945（set_tick_callback）、1003-1013（cleanup）、919-926（set_on_frame_ready，`_on_frame_ready` 全文件无调用方）
  - 证据:
    ```python
    self._tick_callback: Optional[Callable[[], None]] = None
    self._tick_lock: threading.Lock = threading.Lock()
    ...
    def cleanup(self) -> None:
        self.unload()
        self._on_frame_ready = None
        self._on_state_change = None
    ```
  - 影响: `_tick_callback` 从未被读取（S0.B 后遗留）；cleanup 漏清该字段且未复用 `_tick_lock`，锁纪律不一致。
  - 建议: 删除 `_tick_callback`/`_tick_lock`/`set_tick_callback`，或接入实际 tick 源；cleanup 统一在对应锁内清空回调。
  - 置信度: 高

- [AUD-39404] [Suggestion] 未使用导入与弱类型注解；`CoordinatorStats` 无锁更新
  - 位置: src/shared/video/playback_state.py:59（`import os` 全文件无 `os.` 使用）、67（TYPE_CHECKING 导入 `QObject, pyqtSignal` 未被任何注解引用）、255-256/755/782-838（回调与帧返回类型为 `Any`）、248/159-189（`_stats` 从多线程命令路径无锁读写）
  - 证据:
    ```python
    import os
    ...
    if TYPE_CHECKING:
        from PyQt6.QtCore import QObject, pyqtSignal
    ```
  - 影响: 死导入增加噪音；`Any` 丢失 `DecodedFrame` 类型信息；统计计数在并发下可能丢失（仅诊断影响）。
  - 建议: 删除未用导入；返回类型改 `Optional[DecodedFrame]`（TYPE_CHECKING 已具备）；`CoordinatorStats` 更新纳入现有命令锁或改 `threading.Lock`。
  - 置信度: 高

### 通过项
- 线程安全：所有属性对 `_session` 的访问均在 `_session_lock` 内；`get_video_info` 明确规避了锁重入（885-900 行注释与实现一致）
- 信号防递归：本类无 Qt 信号本体，回调异常均被捕获并记日志（974-978）
- 输入验证：`seek_to_time`/`last_frame` 的边界（负时间、0 总帧）经 session 侧 clamp 安全；`set_speed` 由 session clamp
- 异常与取消：load/unload 均收敛为 bool 返回并记录统计，无未捕获路径
- GPU 资源生命周期 / GLSL·HDR：本文件无 GPU/GLSL 代码（不适用）
- 状态提交一致性：命令历史固定 10 条且持 `_command_history_lock`，无 undo/持久化职责（不适用）

---

## src/shared/video/render_commands.py (行数: 45)

### 发现

（无）

### 通过项
- 通过（无逻辑）：纯字符串常量类 `RenderCommand`，无状态、无分支、无 I/O；仅导出渲染命令标识符（UPLOAD_*、RENDER_*、UPDATE_UNIFORMS、视频降噪 SET/RESET/WINDOW 等）
- 注释质量良好：COMPILE_PIPELINE 删除原因（台账 0004a）、RENDER_FULL_RESOLUTION 不可合流的临界语义、RENDER_SCOPE 弃用去向均有交代，与 .clinerules 无冲突

---

## src/shared/video/scrub_stride.py (行数: 64)

### 发现

（无）

### 通过项
- 输入验证：`update_velocity` 对 `dt_s <= 0` 有守卫；`display_stride` 对 `velocity/sustainable_rate <= 0` 返回 1；`quantize_frame` 对 `stride <= 1` 直通并 `max(1, int(stride))` 归一
- 性能模式：EMA 平滑 + 2 的幂步进 + 1.25x 滞回，`MAX_STRIDE=16` 封顶，防止 stride 抖动与无限增长；`quantize_frame` 的负数输入会向 -inf 取整（如 -1→-16），但唯一下游 session.seek_to_frame 会 clamp 到 `[0, total_frames-1]`（session.py:756-758），当前调用链安全
- 纯函数无状态、无线程/GPU/信号/HDR 维度（不适用）；类型注解完整

---

## src/shared/video/session.py (行数: 1,507)

### 发现

- [AUD-39405] [Major] `get_frame_at(is_master=True)` 缺 PLAYING 态同步解码防护且共享帧服务模式下无 fallback（与 `get_master_frame_blocking` 不对称）
  - 位置: src/shared/video/session.py:1189-1238（重点 1223-1225），对照 1085-1092
  - 证据:
    ```python
    # 超时，从源管理器直接获取
    if self._source_manager is not None:
        return self._source_manager.get_master_frame(frame_index)
    ```
    而同职责的 `get_master_frame_blocking` 在 fallback 前有守卫：
    ```python
    if self._state == PlaybackState.PLAYING:
        logger.debug(... skipping SourceManager fallback during playback)
        return None
    ```
    入口的 `_assert_no_sync_decode_in_playback` 只记日志、从不抛异常（video_playback.py:325-335，`raise AssertionError` 被注释掉），且其播放判定是全局任意引擎（`_is_any_engine_playing()`）。
  - 影响: (1) 本 session 未播放但另一引擎播放中时，主线程仍会走 300ms 轮询 + 同步 master 解码（4K master 可达数百 ms），违反 P2-1 且卡 UI；(2) 共享帧服务模式下 `_source_manager` 恒为 None（session.py:437-439），超时后直接返回 None，尽管 `_frame_provider_adapter.get_master_frame` 在协议中存在（session.py:245-252 日志列出的协议清单含 get_master_frame），fallback 能力静默缺失。
  - 建议: 补 PLAYING 守卫（与 1087 行一致）；fallback 顺序改为 `_source_manager` → `_frame_provider_adapter`；可参照 `get_master_frame_blocking` 先检查 `master_worker.is_running()` 再决定是否空等 300ms。
  - 置信度: 高

- [AUD-39406] [Major] `seek_to_frame` 无状态校验且恒返回 True（UNLOADED/ERROR/LOADING 下皆"成功"）
  - 位置: src/shared/video/session.py:728-824（756-758 仅 clamp 帧号，824 无条件 `return True`）
  - 证据:
    ```python
    total_frames = self.total_frames
    if total_frames is not None:
        frame = max(0, min(frame, total_frames - 1))

    with self._state_lock:
        old_state = self._state
        self._state = PlaybackState.SEEKING
    ...
    return True
    ```
    UNLOADED 态下 `_playback_clock`/`_frame_queue` 均为 None，blocking 分支 `get_master_frame_blocking` 返回 None，仍执行 `self._current_frame = frame`、`self._stats.update_seek()` 并返回 True。
  - 影响: 加载失败后（session 残留 ERROR 态且未被协调器清空，见 AUD-39401）UI 每次 seek 都"成功"，进度条移动但画面不动，seek 统计被污染；调用方无法据此触发兜底逻辑。
  - 建议: 入口校验 `state not in (UNLOADED, ERROR, LOADING)` 返回 False；或至少在 blocking 且未取得任何帧时返回 False。
  - 置信度: 高

- [AUD-39407] [Suggestion] `load()` 两段式状态切换非原子，与并发 `unload()` 存在中间态窗口
  - 位置: src/shared/video/session.py:408-416（锁内仅置 LOADING）、417-533（锁外执行 open/queue/worker 创建）、544-580（unload 对 LOADING 态不设 STOPPING 直接清理）
  - 证据:
    ```python
    with self._state_lock:
        if self._state not in (PlaybackState.UNLOADED, PlaybackState.ERROR):
            ...
        self._state = PlaybackState.LOADING
    # 此处释放锁，进行长初始化 ...
    with self._state_lock:
        self._state = PlaybackState.READY
    ```
  - 影响: 若线程 B 在初始化中途调用 `unload()`（LOADING 非 PLAYING，不设 STOPPING），`_cleanup()` 会把 `_frame_queue`/`_worker_manager` 置 None，线程 A 随后继续使用已置空组件或把状态改回 READY，产生不一致。当前生产路径经协调器 `_session_lock` 串行化，未见直接并发调用方，属潜伏竞态。
  - 建议: `unload()` 拒绝或等待 LOADING 态；或 load 全程持一把专用初始化锁。
  - 置信度: 中（窗口真实存在；未发现绕过协调器的实际调用方）

- [AUD-39408] [Suggestion] `_notify_state_change` docstring 与全部调用点矛盾（"Must be called while _state_lock is held"）
  - 位置: src/shared/video/session.py:1336-1342，对照调用点 416、526、541、577、643、682、710、822（均为 V-P0-11 锁外调用）
  - 证据:
    ```python
    def _notify_state_change(self) -> None:
        """通知状态变化

        Note: Must be called while _state_lock is held. Uses self._state
        directly instead of self.state property to avoid reentrant lock
        acquisition deadlock.
        """
    ```
  - 影响: 误导后续维护者按旧约定在锁内调用，恰好会复活 V-P0-11 修掉的回调重入死锁。
  - 建议: 更新 docstring 为"必须在 `_state_lock` 外调用（V-P0-11）"。
  - 置信度: 高

- [AUD-39409] [Suggestion] `unload()` 忽略 `stop_workers` 返回值，恒返回 True
  - 位置: src/shared/video/session.py:566-580
  - 证据:
    ```python
    if self._worker_manager is not None:
        self._worker_manager.stop_workers(timeout=timeout)

    self._cleanup()
    ...
    return True
    ```
  - 影响: 工作线程未能在超时内退出时调用方仍获知"成功"，掩盖线程泄漏风险，违背"stop/unload 后线程 2 秒内退出"的设计验收口径。
  - 建议: 记录并传播 `stop_workers` 的返回值（或至少 logger.warning）。
  - 置信度: 高（返回值被忽略确定；stop_workers 内部语义未展开）

- [AUD-39410] [Suggestion] `_get_performance_monitor` 宽泛 `except Exception` 静默吞错，无任何日志
  - 位置: src/shared/video/session.py:1494-1498
  - 证据:
    ```python
    try:
        from src.shared.media.performance_monitor import get_performance_monitor
        return get_performance_monitor()
    except Exception:
        return None
    ```
  - 影响: 违反 .clinerules"不得宽泛 except 掩盖领域错误"——性能监控初始化/导入失败完全不可见，`apply_playback_degradation` 的 proxy 阶梯信息随之静默缺失。
  - 建议: 至少 `logger.debug/warning` 记录异常；如仅预期 ImportError，收窄为 `except ImportError`。
  - 置信度: 高

- [AUD-39411] [Suggestion] `get_stats` 返回类型注解与实际结构不符；`_get_performance_monitor` 缺返回注解
  - 位置: src/shared/video/session.py:1381-1401、1485
  - 证据:
    ```python
    def get_stats(self) -> dict[str, dict[str, float | int]]:
        ...
        stats['generation'] = self.generation          # int，非 dict
        stats['state'] = self.state.name               # str，非 dict[str, float|int]
        return stats
    ```
  - 影响: 注解误导调用方与静态检查；`_get_performance_monitor(self)` 完全无注解（.clinerules 强类型要求）。
  - 建议: 注解改为 `dict[str, Any]` 或精确的 TypedDict；补 `-> Optional[Any]` 级别注解。
  - 置信度: 高

- [AUD-39412] [Suggestion] `load()` 文档/注释与配置默认值矛盾（"默认共享帧服务" vs 实际默认 False）
  - 位置: src/shared/video/session.py:404-407、421-424（"默认使用 ColorFrameProviderAdapter"/"共享帧服务模式（默认）"），对照 231-252（"V-P1-4: 协议测试全绿前默认禁用"、`value if value is not None else False`、init 日志提示"Consider enabling"）
  - 证据:
    ```python
    Note:
        P7.A: 默认使用 ColorFrameProviderAdapter，与 Composite 共享帧服务。
    ```
    而实际：`use_shared_frame_provider = value if value is not None else False`
  - 影响: 误导维护者对默认路径的判断（独立 SourceManager 才是当前默认），排障时走错分支。
  - 建议: 统一文档口径，标明当前默认为 False、共享模式需配置开启。
  - 置信度: 高

### 通过项
- 线程安全：状态通知遵守 V-P0-11 锁外回调；`_increment_generation` 持 `_generation_lock` 同步队列与 worker；`_state_lock` 临界区极短（仅赋值/判断）
- 异常与取消：load 异常路径完整（cleanup + ERROR + 通知 + False）；`_request_current_proxy_frame_if_missing` 的宽泛 except 有 debug 日志且仅影响请求分支
- 输入验证：`set_speed` clamp 0.1–4.0；`apply_playback_degradation` clamp level 0–3、lookahead 0.0–1.0；`next_frame`/`prev_frame` 边界正确；seek 帧号 clamp（total_frames 已知时）
- 性能模式：tick 不做同步解码、精确匹配 → latest-before-or-at → hold 三级回退；迟到帧丢弃与跨片段 hold-frame 防闪守卫（969-985）逻辑正确；`stop_playback` 用 `pause_workers` 保留热解码器（V-P0-06）符合性能约束
- tick 丢帧/播放统计更新路径自洽（exact/fallback/hold 三分支均有 update_drop/update_play 对应）
- GPU 资源生命周期 / GLSL·HDR / 信号防递归 / undo·revision·持久化：本文件无直接 GPU、GLSL、Qt 信号与撤销栈代码（不适用；GPU 纹理由渲染层经 RenderCommand 管理）

---

## 汇总

| 严重度 | 数量 |
|--------|------|
| Critical | 0 |
| Major | 4（AUD-39400、39401、39405、39406） |
| Suggestion | 9（AUD-39402、39403、39404、39407、39408、39409、39410、39411、39412） |

无 Critical：未发现可直接导致崩溃、数据丢失或渲染错误的路径；两个最高风险项（回调持锁死锁、播放态主线程同步解码）均为条件触发的明确风险，符合 Major 定级。
