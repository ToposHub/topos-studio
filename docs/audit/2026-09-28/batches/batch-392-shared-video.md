# 批次报告：batch-392 [P2] src/shared/video/

编号区间：AUD-39200–AUD-39299。审查日期：2026-09-28。
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解。
本批无 GLSL 着色器代码与 Qt 信号逻辑，相关维度按不适用处理。

---

## src/shared/video/frame_queue.py (行数: 829)

### 发现

- [AUD-39200] [Major] master 队列容量上限永不可能生效，可无界增长
  - 位置: src/shared/video/frame_queue.py:445-468（路由依据 344-348）
  - 证据:
    ```python
    # enqueue() 的路由：只有 can_drop=False 的帧才会进入 master 队列
    if entry.can_drop:
        return self._enqueue_proxy(entry)
    else:
        return self._enqueue_master(entry)
    ```
    ```python
    while len(self._master_queue) > self._max_master_size:
        # 尝试找一个可丢弃的帧（如果有意外混入）
        for i, e in enumerate(self._master_queue):
            if e.can_drop:
                dropped = self._master_queue.pop(i)
                ...
                break
        else:
            # 没有可丢弃帧，保留队列（不强制丢弃）
            logger.warning(
                f"Master queue overflow {len(self._master_queue)}/{self._max_master_size}, "
                f"no droppable frames available"
            )
            break
    ```
  - 影响: master 队列中的元素全部是 `can_drop=False`（enqueue 按 can_drop 路由），溢出处理里 `if e.can_drop` 的查找永远失败，`max_master_size` 从不被执行。长时间播放/导出中若消费端取帧不及时，master 帧（含 GPU 零拷贝帧）无限累积，内存无界增长；且每次溢出入队都会打一条 warning 日志造成刷屏。
  - 建议: 溢出时丢弃最旧 master 帧（丢弃前 safe_release_gpu_frame），或向上游反压；至少应移除这段永假的可丢弃帧查找逻辑，避免"看起来有上限实则没有"的误导。
  - 置信度: 高

- [AUD-39201] [Major] 在任意调用线程同步触发 GPU 帧 release 回调，违反渲染线程释放契约
  - 位置: src/shared/video/frame_queue.py:309-316（同类调用另见 249-250、340-341、373-374、392-393、451-452、651-652、773-781、795-800）
  - 证据:
    ```python
    # 移除过期帧，GPU frame 需要释放
    for entry in stale_entries:
        if is_gpu_decoded_frame(entry.frame):
            safe_release_gpu_frame(entry.frame)
            logger.debug(
                f"Released stale GPU frame {entry.frame_index} "
                f"(gen={entry.generation_id} < current={current_gen})"
            )
    ```
    而 `src/shared/media/gpu_frame.py:247-253` 的 `GpuFrameRelease.release()` 契约明确："必须由渲染线程在安全时机调用，不能由 UI tick 调用"，且 release_callback 是在调用线程直接执行。`increment_generation()` 由 `src/shared/video/decode_worker.py:1402` 与 `src/shared/video/session.py:773`（seek 路径）调用，线程不确定。
  - 影响: D3D11/GL interop 等场景的 release 回调（如 `d3d11_gl_bridge._release_slot` → `native_release.release()`）可能在 UI/解码线程执行 GL/interop 资源释放，与 deferred-release 铁律冲突，存在驱动级崩溃或资源泄漏风险；同时回调在持有 `self._lock` 期间执行，回调耗时（fence 等待、桥接锁）会阻塞所有队列操作。
  - 建议: 队列丢弃/清除时只把 GPU 帧移入待释放列表，交由渲染线程的 deferred-release 队列统一释放（或使用 `release_when_fenced`），不要在队列锁内同步调用回调。
  - 置信度: 中（回调是否线程安全取决于各 backend 注入的实现；契约文本与调用线程不匹配是确定的）

- [AUD-39202] [Major] peek/get_latest/get_latest_before_or_at 返回仍在队列中的 entry 引用，无所有权保障
  - 位置: src/shared/video/frame_queue.py:569-573（peek）、589-595（get_latest）、711-714（get_latest_before_or_at）
  - 证据:
    ```python
    if frame_index is not None:
        for entry in queue:
            if entry.frame_index == frame_index:
                return entry
        return None
    ```
    实际消费方式（src/shared/video/session.py:1218-1221）：
    ```python
    entry = self._frame_queue.peek(is_master=True, frame_index=frame_index)
    if entry is not None:
        return entry.frame
    ```
  - 影响: 调用方拿到 `entry.frame` 后帧仍留在队列中；另一线程并发执行 `clear()`/`increment_generation()`/同帧号替换时会对同一 GPU 帧调用 `safe_release_gpu_frame`，造成 use-after-release——纹理被释放或被环形槽位复用，引发渲染错误或崩溃。
  - 建议: 为 peek 类接口提供 retain/release 引用计数语义，或返回脱队的浅拷贝 entry，或在文档中强制要求调用方在消费完成前不得触发任何可能清除队列的操作（并加断言）。
  - 置信度: 中（竞态窗口真实存在，但需要 seek/清除与消费并发）

- [AUD-39203] [Suggestion] QueueEntry.is_stale 恒返回 False，是误导性死代码
  - 位置: src/shared/video/frame_queue.py:135-142
  - 证据:
    ```python
    @property
    def is_stale(self) -> bool:
        """
        是否为过期帧（基于 generation_id）。
        ...
        """
        return False  # 实际判断在队列层面
    ```
  - 影响: 公开属性恒为 False，调用方若依赖它判断过期会得到错误结论。
  - 建议: 删除该属性，或改为 `is_stale(current_generation: int) -> bool` 形式的显式比较。
  - 置信度: 高

- [AUD-39204] [Suggestion] 过期帧/更旧帧清除使用 list.remove，O(n²) 复杂度
  - 位置: src/shared/video/frame_queue.py:306-316、647-652
  - 证据:
    ```python
    older_entries = [e for e in queue if e.frame_index < best_frame]
    older_count = len(older_entries)
    for e in older_entries:
        queue.remove(e)
    ```
  - 影响: 每次删除都要线性扫描列表；AUD-39200 导致 master 队列可能很长时开销被放大。
  - 建议: 单次遍历重建列表（`queue[:] = [e for e in queue if ...]`），在遍历中统一 release 被剔除的 GPU 帧。
  - 置信度: 高

### 通过项
- 队列结构操作（enqueue/dequeue/peek/clear/stats）全部在 threading.Lock 保护下，队列自身结构线程安全
- stats 属性返回副本，避免外部直接修改内部字典
- enqueue 对过期 generation 帧拒绝入队并释放 GPU 资源，无泄漏路径
- set_max_proxy_size 缩容时正确释放被丢弃的 GPU 帧
- 类型注解完整（含属性注解），无缺失
- 无 GLSL/HDR 处理逻辑、无 Qt 信号、无持久化状态，相关维度不适用

---

## src/shared/video/frame_request.py (行数: 268)

### 发现

- [AUD-39205] [Suggestion] priority 与 reason 无一致性校验，可构造自相矛盾的请求
  - 位置: src/shared/video/frame_request.py:135-140
  - 证据:
    ```python
    def __post_init__(self) -> None:
        """验证参数有效性"""
        if self.frame_index < 0:
            raise ValueError(f"frame_index must be >= 0, got {self.frame_index}")
        if self.generation_id < 0:
            raise ValueError(f"generation_id must be >= 0, got {self.generation_id}")
    ```
  - 影响: 直接构造 `VideoFrameRequest(reason=EXPORT, priority=PLAYBACK)` 不会被拦截，调度器按低优先级排队导出帧，导出帧可能被当作可丢弃请求处理前的排序错乱；便捷工厂方法均正确，但 dataclass 本身未防护。
  - 建议: `__post_init__` 中校验 `priority == RequestPriority.get_default(self.reason)`（或允许显式 override 参数），不一致时抛 ValueError。
  - 置信度: 高

### 通过项
- frozen=True 不可变数据类，调度过程中无状态突变风险，天然线程安全
- frame_index/generation_id 非负校验到位（输入验证）
- can_drop 语义与 reason 映射正确（EXPORT/COMPOSITE 不可丢帧）
- 类型注解完整；无 GPU/信号/持久化逻辑，相关维度不适用

---

## src/shared/video/frame_trace_schema.py (行数: 159)

### 发现

- [AUD-39206] [Suggestion] segment_budgets_ms 返回裸 dict 注解，priority/backend 为自由字符串未对齐词表
  - 位置: src/shared/video/frame_trace_schema.py:98-99、143-148
  - 证据:
    ```python
    def segment_budgets_ms(deadline_ns: int) -> dict:
    ```
    ```python
    @dataclass(frozen=True)
    class FrameTraceContext:
        trace_id: int
        timeline_frame: int
        generation_id: int
        deadline_ns: int = 0
        mode: str = ""
        priority: str = ""
        backend: str = ""
    ```
  - 影响: 模块定义了 TASK_PRIORITY_LEVELS / GRAPHICS_BACKENDS 词表用于"避免各模块各写一套字符串"，但 FrameTraceContext 的 priority/backend 字段不做任何词表校验，拼写错误的值会静默进入 trace 导出，破坏对拍；`-> dict` 也丢失了 `dict[str, float]` 的类型信息。
  - 建议: 返回注解改为 `dict[str, float]`；FrameTraceContext 增加 `__post_init__` 校验（空串放行，非空时必须在词表内）。
  - 置信度: 高

### 通过项
- trace id 分配器用 threading.Lock + itertools.count(1)，线程安全且单调，0 保留语义明确
- SEGMENT_BUDGET_RATIOS 比例合计 1.0（0.40+0.15+0.20+0.05+0.15+0.05）
- deadline_ns <= 0 时返回空 dict 的 fail-closed 语义正确
- GPU 时间不可用显式标记（GPU_TIME_UNAVAILABLE）符合"不得用 0.0 冒充"约定
- 无 GPU 资源/信号/持久化逻辑，相关维度不适用

---

## src/shared/video/performance_budgets.py (行数: 261)

### 发现

- [AUD-39207] [Major] 多个 media 文件匹配同一预算 glob 时只裁决第一个，其余文件逃过门禁
  - 位置: src/shared/video/performance_budgets.py:159-169
  - 证据:
    ```python
    matched = [m for m in media_entries
               if fnmatch.fnmatch(Path(str(m.get("file", ""))).name,
                                  b.media_glob)]
    value: Optional[float] = None
    sample_file = ""
    for m in matched:
        v = _resolve_metric(m, b.metric)
        if v is not None:
            value = v
            sample_file = str(m.get("file", ""))
            break
    ```
  - 影响: media_glob 如 `*.mp4` 匹配多个素材时，只有列表顺序第一个的指标参与裁决——若它快而其它文件超 hard_max，hard_breach 被静默漏检，门禁结论还依赖报告里 media 的排列顺序。这违背该模块作为合入门禁的核心职责（防静默漏检的 missing 语义都专门设计了，多文件却漏掉）。
  - 建议: 对 matched 逐文件取值后取最差值（延迟类取 max）裁决，并在 verdict 中记录被裁决的文件数；或要求 glob 必须唯一匹配否则报错。
  - 置信度: 中（单文件报告不受影响；多文件漏检逻辑确定存在）

- [AUD-39208] [Suggestion] iter_budgets 未校验 soft_max_ms <= hard_max_ms
  - 位置: src/shared/video/performance_budgets.py:94-114
  - 证据:
    ```python
    b = Budget(
        budget_id=str(raw["budget_id"]),
        ...
        hard_max_ms=float(raw["hard_max_ms"]),
        soft_max_ms=float(raw["soft_max_ms"]),
    ```
  - 影响: 注册表写反 soft/hard 时（soft > hard），`value > hard_max_ms` 先命中，soft 门永远不会触发，软门回归告警静默失效；文档口径是"软门更紧"，与数据无交叉验证。
  - 建议: iter_budgets 中加 `if b.soft_max_ms > b.hard_max_ms: raise ValueError(...)`（注册表必须干净的原则一致）。
  - 置信度: 高

### 通过项
- schema 版本校验严格（不匹配即抛错），坏 state 抛错，坏条目不静默跳过
- missing / not_applicable / known_breach 的门禁语义实现与文档一致，known_breach 降级不阻止
- compare_reports 只做同机（platform 一致）裁决，跨机记 cross_machine 不裁决，reference<=0 跳过，符合 D-04 口径
- gate_summary 聚合逻辑正确（hard/missing=2，soft/regression=1）
- DEFAULT_BUDGETS_PATH 支持 env 覆盖便于 CI；类型注解完整
- 无 GPU/信号/GLSL 逻辑，相关维度不适用

---

## src/shared/video/monitor_output.py (行数: 1,240)

### 发现

- [AUD-39209] [Major] ffmpeg 子进程 stderr=PIPE 从不读取，stderr 写满管道缓冲后监控输出冻结
  - 位置: src/shared/video/monitor_output.py:643-649（读取方缺失于整个 _run 循环 656-666）
  - 证据:
    ```python
    self._process = self._process_factory(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    ```
    `_run` 循环只做 `self._drain_latest_frame()` + `self._write_frame(self._last_frame)`（阻塞写 stdin），无任何 stderr 读取。
  - 影响: DeckLink 输出在丢帧/补帧时会持续向 stderr 打 warning；约 64KB 管道缓冲填满后 ffmpeg 阻塞在 stderr 写→停止读 stdin→写线程阻塞在 `stdin.write(frame)`→监控画面冻结且 `_stop_event` 长时间得不到检查，`stop()` 的 join(1.0) 超时（最终靠 terminate 兜底恢复）。典型 subprocess 死锁模式。
  - 建议: stderr 重定向 DEVNULL（loglevel 已是 warning），或起 dedicated drain 线程消费 stderr 并限频记录。
  - 置信度: 中（取决于 ffmpeg stderr 输出量；管道不读这一事实确定）

- [AUD-39210] [Major] start() 在调用线程同步执行最长约 10-15 秒的 ffmpeg 子进程探测，阻塞 UI 线程
  - 位置: src/shared/video/monitor_output.py:561-572（`ffmpeg_supports_decklink` 定义于 179-196，`list_decklink_devices` 定义于 1203-1230，二者各含 timeout=5.0 的 subprocess.run，且 list_decklink_devices 内部再次调用 ffmpeg_supports_decklink）
  - 证据:
    ```python
    if not ffmpeg_supports_decklink(ffmpeg_path):
        logger.warning(...)
        return False
    ...
    if not config.device:
        devices = list_decklink_devices(ffmpeg_path)
    ```
    调用链：`src/app/controllers/file_controller.py:2519 _submit_program_monitor_frame`（播放帧到达路径）→ `:3619-3627 _ensure_program_monitor_output` → `existing.start()`。
  - 影响: UI 线程首次启动外接监看或设备未配置时，同步等待最多 3 次 subprocess.run（各 5s 超时），界面卡死最长约 15 秒；与"UI 线程不得执行长阻塞操作"的分层约束冲突（虽非 OpenGL 调用，但同属 UI 线程阻塞）。
  - 建议: 设备发现/能力探测移到后台线程，完成后再启动输出线程；或缓存探测结果避免每帧提交路径重复探测。
  - 置信度: 中（file_controller 调用线程推断为 UI 线程，未逐帧验证）

- [AUD-39211] [Major] YUV→BGR 转换硬编码 BT.601 全量程系数，HD 素材外部监看颜色偏移
  - 位置: src/shared/video/monitor_output.py:439-452
  - 证据:
    ```python
    u -= 128.0
    v -= 128.0
    b = y + 1.8556 * u
    g = y - 0.1873 * u - 0.4681 * v
    r = y + 1.5748 * v
    ```
  - 影响: 1.8556/0.1873/0.4681/1.5748 是 BT.601 矩阵系数，且按全量程（full-range）处理（未做 16-235 limited range 的 y=(y-16)*255/219 拉伸）。本模块面向 1080p（默认 1920x1080/Hi50）HD 监看，HD 视频惯例是 BT.709 limited range，色相/饱和度会系统性偏移（典型偏绿/偏品红），外接监看与程序内预览颜色不一致。
  - 建议: 依据帧携带的矩阵元数据选择 601/709 系数；limited range 先做 range 变换；无元数据时对 HD 分辨率默认 709 limited。
  - 置信度: 中（系数为 601 full-range 确定无疑；实际偏差取决于素材的矩阵/量程）

- [AUD-39212] [Suggestion] 多处宽泛 except 静默吞掉转换错误，无任何日志
  - 位置: src/shared/video/monitor_output.py:231-238、299-302、365-368、402-409、453-454
  - 证据:
    ```python
    except Exception:
        return None
    ```
    （decoded_frame_to_bgr_array 的 planes 转换整段被吞；rgb_array_to_bgra_frame/ndarray_to_bgra_frame 的 cv2 回退同样静默返回黑帧）
  - 影响: 监看输出黑屏/停更时完全无法诊断是哪一步转换失败，违反"不得宽泛 except 掩盖领域错误"铁律；即便 best-effort 路径也应留痕。
  - 建议: 至少 logger.debug 记录异常类型与输入格式，或对同类错误限频 warning 一次。
  - 置信度: 高

- [AUD-39213] [Suggestion] DeckLinkMonitorConfig.from_config 对非数值配置值直接抛未捕获 ValueError
  - 位置: src/shared/video/monitor_output.py:83-94
  - 证据:
    ```python
    width=int(read("decklink_monitor_width", 1920) or 1920),
    height=int(read("decklink_monitor_height", 1080) or 1080),
    fps=float(read("decklink_monitor_fps", 25.0) or 25.0),
    ...
    queue_size=int(read("decklink_monitor_queue_size", 2) or 2),
    ```
  - 影响: 配置文件（外部数据）写成 `"abc"` 等非数值时，`int("abc")` 抛 ValueError 未被捕获，从 from_config 一路炸到 UI；read() 的 try/except 只保护了 get 调用本身，不保护类型转换。
  - 建议: 每项转换包 try/except 回退默认值，或提供 _to_int/_to_float 辅助函数。
  - 置信度: 高

- [AUD-39214] [Suggestion] _dropped_frames 跨线程非原子自增，计数可能丢失
  - 位置: src/shared/video/monitor_output.py:892-897（提交线程）与 933-937（Native 提交线程）
  - 证据:
    ```python
    while self._queue.full():
        try:
            self._queue.get_nowait()
            self._dropped_frames += 1
    ```
    ```python
    while True:
        try:
            latest = self._queue.get_nowait()
            self._dropped_frames += 1
    ```
  - 影响: NativeDeckLinkSDKMonitorOutput 的丢帧计数在提交调用方线程与 submitter 线程并发 `+= 1`（读-改-写竞态），高丢帧率时统计偏小；仅影响诊断指标，不影响功能。
  - 建议: 用 threading.Lock 或 itertools/queue 计数保护，或改用 queue.Queue 的 unfinished_tasks 类原子手段。
  - 置信度: 高

- [AUD-39215] [Suggestion] Native 后端 start() 失败时已创建的 output 未调用 stop() 清理
  - 位置: src/shared/video/monitor_output.py:822-827
  - 证据:
    ```python
    except Exception as exc:
        logger.warning("Native DeckLink SDK backend unavailable: %s", exc)
        self._output = None
        self._running = False
        self._supports_gpu_texture = False
        return False
    ```
  - 影响: factory 成功创建 output 但后续 `start()`/属性读取抛异常时，原生 SDK 对象被直接置 None 丢弃，未调用其 stop() 释放设备句柄，重复开关监看可能耗尽 DeckLink 设备资源。
  - 建议: except 分支中先 `stop = getattr(self._output, "stop", None)`，callable 则 try 调用后再置 None。
  - 置信度: 中（取决于扩展模块 stop 的必要性）

- [AUD-39216] [Suggestion] ndarray 路径无 cv2 且需缩放时直接返回黑帧，与 QImage 路径回退不对称
  - 位置: src/shared/video/monitor_output.py:294-302
  - 证据:
    ```python
    try:
        import cv2
        ...
        fitted = cv2.resize(bgr, (draw_w, draw_h), interpolation=interpolation)
    except Exception:
        fitted = np.ascontiguousarray(bgr)
        if (src_w, src_h) != (draw_w, draw_h):
            return black_bgra_frame(width, height)
    ```
  - 影响: cv2 不可用且源分辨率与目标 letterbox 尺寸不一致（几乎必然，如非 16:9 素材）时，ndarray 载荷输出全黑；而 qimage_to_bgra_frame（232-238）有 QImage.scaled 回退。视频帧走 ndarray/DecodedFrame 路径，恰是最常被打到黑帧的路径。
  - 建议: 无 cv2 时用 numpy 最近邻插充（步长切片采样）兜底，或至少按比例中心裁剪/填充。
  - 置信度: 高

### 通过项
- 帧写入走独立稳定时钟线程（DeckLinkMonitorOutput），不在 UI 线程做转换/IO，符合"帧路径 out-of-band"设计
- submit_qimage 对 QImage 做 copy() 后跨线程传递，避免共享底层缓冲
- 命令行用参数列表构造（无 shell），device 字符串无注入面
- resolve_ffmpeg_path 兼容 POSIX/Windows 显式路径与打包 bin/ 回退
- _write_frame 捕获 BrokenPipeError/OSError/ValueError，进程死亡时线程能退出
- _terminate_process 具备 terminate→wait→kill 兜底，poll 防止对已退出进程重复发信号
- submit_frame_payload 队列满时丢旧保新并计数，不阻塞提交方
- rgba_bytes_to_bgra_frame 对 reshape 失败回退黑帧；GL oriented flipud 处理正确
- rgb_array_to_bgra_frame cover 模式裁剪后校验形状再使用，无越界写
- 浮点字面量均带 .0；调试日志按事件限频，不会刷屏
- MonitorRGBAFrame/DeckLinkMonitorConfig 冻结数据类，配置经 replace 更新

---

## 汇总

| 文件 | Critical | Major | Suggestion |
|------|----------|-------|------------|
| frame_queue.py | 0 | 3 | 2 |
| frame_request.py | 0 | 0 | 1 |
| frame_trace_schema.py | 0 | 0 | 1 |
| performance_budgets.py | 0 | 1 | 1 |
| monitor_output.py | 0 | 3 | 5 |
| 合计 | 0 | 7 | 10 |

最高优先修复建议：AUD-39200（master 队列无界增长）、AUD-39201/39202（GPU 帧释放的线程与所有权契约）、AUD-39209（stderr 管道死锁）、AUD-39210（UI 线程阻塞探测）。
