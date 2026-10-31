# Batch 189 审查报告 — src/features/edit/core/timeline_preview_renderer.py

审查范围：`src/features/edit/core/timeline_preview_renderer.py`（11,621 行，单文件批次）。
编号区间：AUD-18900–AUD-18999。九维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 /
信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解。

## src/features/edit/core/timeline_preview_renderer.py (行数: 11621)

### 发现

- [AUD-18900] [Critical] `_GuardedPreviewSource` 的 acquire/retire 为无锁 check-then-act，UI 线程 retire 可关闭正在被 worker 读取的 PyAV 源
  - 位置: src/features/edit/core/timeline_preview_renderer.py:447-470
  - 证据:
    ```python
    def acquire(self) -> Any:
        """登记一个在飞读者，返回底层源（仅未关闭时）。"""
        if self.closed:
            return None
        self.inflight += 1
        return self.source

    def retire(self) -> None:
        """标记关闭；空闲则立即释放。"""
        self.closed = True
        if self.inflight <= 0:
            try:
                self.source.close()
    ```
  - 影响: `acquire()` 的 `closed` 检查与 `inflight += 1` 之间无锁。`_clear_direct_preview_sources()`
    在 UI 线程执行（`set_viewport_size` / `set_preview_quality` / 时间线重建路径，见 :1313-1322、:4028-4044），
    若 `retire()` 恰在 worker 读到 `closed=False` 之后、`inflight += 1` 之前执行：retire 看到
    `inflight==0` 立即 `source.close()`，worker 随后对已释放的 PyAV 容器调用 `read_frame()`
    ——这正是该类 docstring 自述曾 ~1/3 复现的 avcodec 段错误场景（:430-438），保护本身留有竞态窗口。
  - 建议: 在句柄内加 `threading.Lock`，acquire/release/retire 全部持锁执行
    （acquire 持锁检查 closed 并递增 inflight；retire 持锁判定是否立即 close），
    消除 check-then-act 窗口；close 幂等化防双关。
  - 置信度: 中（竞态机制由代码确证，触发窗口窄但 retire 在每次时间线重建/视口变化时高频发生）

- [AUD-18901] [Major] GPU 路径 solid 分支缩进缺陷：`sc` 在素材缺失时未定义（NameError），或沿用上一层 solid 的旧颜色
  - 位置: src/features/edit/core/timeline_preview_renderer.py:2073-2081
  - 证据:
    ```python
    if asset_type == 'solid':
        solid_asset = self._asset_resolver.get_asset(asset_id)
        solid_rgb = (0.0, 0.6, 0.8)
        gen_params = None
        if solid_asset is not None:
            sc = getattr(solid_asset, "solid_color", None)
        if sc is not None:
            solid_rgb = sc
    ```
  - 影响: `if sc is not None:` 与 `if solid_asset is not None:` 同级。当 solid 素材解析失败
    （`get_asset` 返回 None）且本次调用未曾给 `sc` 赋值时抛 `NameError`（整帧落入 render_error）；
    若本帧先渲染过另一个 normal solid 层，`sc` 会从上一层泄漏过来，第二个 solid 层渲染成前一层的颜色
    （渲染错误，且静默）。
  - 建议: 将 `sc` 读取与判空移入同一缩进块：
    `if solid_asset is not None: sc = getattr(...); if sc is not None: solid_rgb = sc`。
  - 置信度: 高

- [AUD-18902] [Major] `PreviewRenderWorker.stop()` 5 秒等待超时后仍强制关闭解码管理器与线程池，复活其注释自认的在飞解码 SIGSEGV 路径
  - 位置: src/features/edit/core/timeline_preview_renderer.py:1288-1303
  - 证据:
    ```python
    if self.isRunning():
        if not self.wait(5000):
            logger.warning(
                "PreviewRenderWorker.stop: render thread did not exit within 5s"
            )
    # 层解码线程池先于解码管理器关闭（futures 可能正解码同一 VT 源）
    try:
        self._decode_executor.shutdown(wait=False, cancel_futures=True)
    ...
    if self._gpu_decode_manager is not None:
        try:
            self._gpu_decode_manager.close_all()
    ```
  - 影响: :1283-1287 注释明确说明"不等线程退出就 close_all() 会销毁正在使用的 VT 会话 → SIGSEGV"，
    但 `wait(5000)` 超时后仅记 warning 就继续执行 close_all()；且 `_decode_executor.shutdown(wait=False)`
    允许仍在运行的解码 future 继续持有 VT 源，与随后的 close_all 并发。慢源（网络盘/长 GOP 反走）下
    解码超过 5 秒即触发该崩溃窗口（退出或切换工程时段错误）。
  - 建议: 超时后不要继续销毁共享解码资源——或将 close_all 推迟到渲染线程真正退出之后
    （例如由线程自身的 cleanup 阶段关闭），至少 `shutdown(wait=True)` 等待在飞 future。
  - 置信度: 高（代码路径确证；实际触发依赖 >5s 的在飞解码）

- [AUD-18903] [Major] CPU 回退把 limited-range yuv420p 直接按 full-range 矩阵转 BGR，黑位/白位错误
  - 位置: src/features/edit/core/timeline_preview_renderer.py:5535-5544
  - 证据:
    ```python
    # 注意: yuvj420p 是 full range (0-255), yuv420p 是 limited range (16-235)
    if pixel_format == 'yuvj420p':
        # Full range JPEG YUV
        bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR)
    else:
        # Limited range YUV (需要映射到 full range)
        # 简化处理: 直接转换
        bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR)
    ```
  - 影响: `cv2.COLOR_YUV2BGR` 按 full-range (JPEG) 矩阵转换；limited-range 源（广播 H.264/HEVC
    常见的 yuv420p）的黑位 16、白位 235 被当作 0/255 显示——对比度压扁、黑不黑白不白，
    与 GPU 路径解码结果肉眼可见不一致（CPU 回退/导出侧色彩错误）。
  - 建议: limited-range 分支改用 `cv2.COLOR_YUV2BGR` 前先做 16-235 → 0-255 的线性拉伸，
    或使用带色彩范围的专用转换（COLOR_YUV2BGR + 前置 LUT / `cv2.cvtColor(yuv*219/255+16 ...)` 反向映射）。
  - 置信度: 高（代码与注释自认简化；视觉影响取决于该回退路径命中率）

- [AUD-18904] [Suggestion] worker 侧 `_playback_session_emitted` 恒为 False，realtime CPU 回退遥测门（RP-P0-10 验收指标）为死代码
  - 位置: src/features/edit/core/timeline_preview_renderer.py:1616-1619
  - 证据:
    ```python
    if self._current_render_realtime and getattr(self, '_playback_session_emitted', False):
        self._realtime_cpu_fallback_count = (
            getattr(self, '_realtime_cpu_fallback_count', 0) + 1
        )
    ```
  - 影响: `_playback_session_emitted = True` 只在 `TimelinePreviewRenderer._mark_frame_emitted`
    （:7017）设置；本处 `self` 是 `PreviewRenderWorker`，属性永远未定义 → 条件恒 False，
    `worker.realtime_cpu_fallback` trace 永不发射，"验收 realtime cpu_qimage_frames == 0"
    的指标静默失效，回退回归无法被观测。
  - 建议: 由 renderer 通过既有 `set_playback_active` / trace hook 通道同步该标志到 worker，
    或改为只判 `self._current_render_realtime`。
  - 置信度: 高

- [AUD-18905] [Suggestion] `_composite_layers` 保存/恢复 `_current_render_timeline_frame` 无 try/finally，异常时帧号泄漏到后续渲染
  - 位置: src/features/edit/core/timeline_preview_renderer.py:2669-2670, 2934-2940
  - 证据:
    ```python
    previous_timeline_frame = getattr(self, "_current_render_timeline_frame", None)
    self._current_render_timeline_frame = frame
    ...
    if previous_timeline_frame is None:
        try:
            delattr(self, "_current_render_timeline_frame")
        except AttributeError:
            pass
    else:
        self._current_render_timeline_frame = previous_timeline_frame
    ```
  - 影响: 合成循环中途抛异常（被 `_execute_render` 兜底捕获）时恢复逻辑不执行，
    本帧的 timeline_frame 残留；下一次渲染前，`_get_motion_graphic_render_cache_frame`（:4250）
    与 comp 缓存键会用陈旧帧号索引，可能读到错误缓存帧（嵌套序列递归渲染同样受影响）。
  - 建议: 用 try/finally 包裹合成主体，在 finally 中恢复/删除该属性。
  - 置信度: 中（泄漏路径存在；需要合成循环抛异常才触发）

- [AUD-18906] [Suggestion] `_mg_static_frames` UI 线程 clear() 与 worker `next(iter(...))` 竞态可抛 StopIteration 丢一帧
  - 位置: src/features/edit/core/timeline_preview_renderer.py:4119-4121, 679-682
  - 证据:
    ```python
    if len(self._mg_static_frames) >= self._mg_static_frames_max:
        self._mg_static_frames.pop(next(iter(self._mg_static_frames)), None)
    self._mg_static_frames[memo_key] = frame
    ...
    # set_playback_active (UI 线程):
    if not active:
        self._mg_static_frames.clear()
    ```
  - 影响: worker 在 `len()>=32` 判定后、`next(iter(...))` 前被 UI 线程（播放停止）clear()，
    `next(iter(空dict))` 抛 StopIteration，沿 `_get_layer_frame` 上抛到 `_execute_render`
    兜底 → 该帧 render_error（MG 层播放→暂停切换瞬间丢一帧）。
  - 建议: 用 `try: oldest = next(iter(d)) except StopIteration: oldest = None` 防御，
    或淘汰逻辑改为持锁/加锁保护（与 set_playback_active 共用一把锁）。
  - 置信度: 中

- [AUD-18907] [Suggestion] 去重集合截断用 `list(set)[-N:]`，set 无序导致"保留最新"语义失效
  - 位置: src/features/edit/core/timeline_preview_renderer.py:9847-9850, 9880-9883
  - 证据:
    ```python
    if len(self._preposition_scheduled_keys) > 512:
        self._preposition_scheduled_keys = set(
            list(self._preposition_scheduled_keys)[-256:]
        )
    ...
    if len(self._prebuffer_completed_keys) > 128:
        self._prebuffer_completed_keys = set(
            list(self._prebuffer_completed_keys)[-64:]
        )
    ```
  - 影响: set 迭代顺序由哈希决定（非插入序），`[-N:]` 保留的是任意 N 个元素：
    可能丢弃刚加入的键（重复预置位、浪费解码）同时保留陈旧键。generation 已含在键中，
    无正确性问题，但去重/预算语义与注释意图不符。
  - 建议: 改用 `OrderedDict`/`collections.deque` 按插入序截断（`dict(itertools.islice(d, N, None))`）。
  - 置信度: 高

- [AUD-18908] [Suggestion] `setup_comp_gl_context` 异常路径不恢复主 GL context 的 current surface
  - 位置: src/features/edit/core/timeline_preview_renderer.py:3071-3073, 3103-3109
  - 证据:
    ```python
    current_surface = main_context.surface()
    if current_surface:
        main_context.doneCurrent()
    ...
    except Exception as e:
        logger.error(f"PreviewRenderWorker: setup_comp_gl_context failed: {e}", exc_info=True)
        self._comp_gl_context = None
        ...
        return False
    ```
  - 影响: `doneCurrent()` 之后若 `QOpenGLContext()` 构造/`create()`/`moveToThread` 抛异常，
    外层 except 不执行 `main_context.makeCurrent(current_surface)`（成功路径 :3097-3099 与
    create-失败路径 :3083-3085 都有恢复），主 context 保持无 current 状态——后续 viewer 的
    GL 调用失败，直到下一次显式 makeCurrent（渲染异常）。
  - 建议: 把恢复逻辑放进与 doneCurrent 配对的 finally，或在 except 分支补 `if current_surface: main_context.makeCurrent(current_surface)`。
  - 置信度: 中（需要 Qt 对象构造/create 抛异常才触发）

- [AUD-18909] [Suggestion] `_comp_gl_context` / `_comp_gl_surface` 创建后无任何销毁路径（GL 资源泄漏）
  - 位置: src/features/edit/core/timeline_preview_renderer.py:3066-3088, 1304-1311
  - 证据:
    ```python
    self._comp_gl_surface = QOffscreenSurface()
    self._comp_gl_surface.setFormat(main_context.format())
    self._comp_gl_surface.create()
    ...
    self._comp_gl_context = QOpenGLContext()
    ...
    self._comp_gl_context.moveToThread(self)
    self._comp_gl_ready = True
    ```
    `stop()`（:1273-1311）与 `TimelinePreviewRenderer.shutdown()`（:6832-6903）均未对其
    `destroy()`/删除。
  - 影响: 每次 renderer 生命周期（工程打开/页面重建）泄漏一个共享 QOpenGLContext +
    QOffscreenSurface（驱动侧 GL 对象），长会话多工程切换下累积。
  - 建议: 在 worker 线程退出后（stop() 尾部，context 已无 current）调用
    `_comp_gl_surface.destroy()` 并置空两个对象；surface 需回到 GUI 线程销毁。
  - 置信度: 高

- [AUD-18910] [Suggestion] `update_sequence_silent` 不递增 generation，在飞旧渲染结果可短暂上屏/入缓存
  - 位置: src/features/edit/core/timeline_preview_renderer.py:10127-10142, 10639-10661
  - 证据:
    ```python
    def update_sequence_silent(self, sequence: Any) -> None:
        ...
        self._sequence = sequence
        self._render_plan = None
        self._invalidate_preview_frame_caches()
        ...
        self._render_in_flight = False
        if self._worker:
            self._worker._timeline_sequence = sequence
    ```
    （对照 `update_color_stacks` :10308 有 `self._generation_id += 1`）
  - 影响: 拖动 Transform 期间，上一 tick 派发的渲染任务仍携带旧序列的 plan；完成后
    `_on_frame_rendered` 的 generation gate（gen 未变）放行，旧像素帧被 emit 并
    `self._cache.put(frame, result)` 入缓存——每 tick 可能闪现一帧滞后画面/污染缓存条目，
    直至本 tick 渲染覆盖。设计上有意避免冷启动，但缺少对在飞结果的失效手段。
  - 建议: 引入轻量 revision 计数随任务携带（不动 decode generation），或在
    `_on_frame_rendered` 比对任务快照的 sequence 标识后丢弃。
  - 置信度: 中

- [AUD-18911] [Suggestion] 类型注解缺失/不一致（含 `if False:` 类型提示块与未使用的 `_mutex`）
  - 位置: src/features/edit/core/timeline_preview_renderer.py:672, 201-203, 3041, 4950, 10008, 670
  - 证据:
    ```python
    def set_render_trace_hook(self, hook) -> None:          # hook 无注解
    ...
    if False:                                               # 运行时死代码作类型提示
        from src.shared.video.frame_provider_service import VideoFrameProviderService, FrameQuality
    ...
    def _get_comp_frame_renderer(self):                     # 返回类型缺失
    ...
    def _occlusion_facts_for_layer(self, layer: ClipLayerInfo):   # 返回类型缺失
    ...
    def set_sequence_resolver(self, resolver) -> None:      # resolver 无注解
    ...
    self._mutex = QMutex()                                  # PreviewRenderWorker 内从未使用
    ```
  - 影响: 与 .clinerules 强类型约束不符；`if False:` 应改 `TYPE_CHECKING`（文件顶部已有
    TYPE_CHECKING 块，两套并存）；`_mutex` 为死属性（PreviewFrameCache 自带锁）易误导读者。
  - 建议: 补齐参数/返回注解（`Callable[[int, float, float, float, int], None]`、
    `"CompositeFrameRenderer"`、`Callable[[str], Optional[TimelineSequence]]` 等）；
    合并类型提示块；删除未使用的 `_mutex`。
  - 置信度: 高

### 通过项
- 信号防递归：engine 派发经 `_render_from_engine` 标志防重入（:7690-7694）；缓存命中→advance 的同步递归用 `QTimer.singleShot(0)` 打断（:9141，#crash-fix 注释）
- GPU 解码资源释放：`_try_gpu_render` 失败/submit 拒绝路径逐层 `release_layer_data`/`release_decoded_resource`（含 comp GPU handle 租约，:1936-1966、:2507-2522、:2589-2594）；RuntimeError 重解路径先归还已产出资源（:2475-2484）
- present slot 防泄漏：stale/迟到 GpuPresentFrame 各丢弃分支均 `_ack_present_slot_safe`（:6734、:6826-6830、:11502 等）；begin_scrub 统一回收遗留槽（:8054-8058）
- generation/单调帧守卫：CPU 与 GPU 结果双 gate（generation + 单调 + 播放 stale + 暂停播放头比对，:10639、:11212、:11397、:11438），seek/方向切换重置语义完整
- 异常路径：worker 主循环与 `_execute_render` 均有兜底 except 并发 render_error + trace（:836-838、:1733-1740）；scrub 单飞/定时器双看门狗自愈（:8326-8358、:7199-7209）
- 取消/latest-only：队列满按优先级协议裁决且窥视持 queue.mutex 无重入（:842-865、:989-1043）；scrub 令牌 + 距离阈值放弃策略（:1138-1158）
- 输入验证：env 解析 try/except + 下限钳制（:277-282、:226-235）；`request_render` 对 frame 做 `max(0, int)`（:8949）；直连源失败负缓存防逐帧重试（:4426-4430）；解码帧号 mismatch>1 拒收（:5197-5228）
- HDR/色彩正确性（本文件内）：comp float32 结果走线性→sRGB OETF 而非直接 ×255（:3356-3367）；`_plane_to_uint8` 浮点平面同样做 OETF（:5418-5423）；8-bit 显示路径的 clamp 均有注释理由
- 缓存预算：PreviewFrameCache 条数+字节双预算、单帧超限拒收（:337-375）；comp_clip LRU 字节预算 + 非 OrderedDict 重建记账（:3383-3412、:3495-3515）；各暂存字典均有界（trace ids ≤8/16、offline ≤64、scrub started ≤32）
- 线程锁序：`_scrub_release_lock → _pending_mutex` 单向获取，未发现反序死锁；`enqueue_background_if_idle` 的 empty→put 注释论证了为何不能持 queue.mutex
