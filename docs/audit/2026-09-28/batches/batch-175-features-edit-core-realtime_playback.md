# batch-175 审查报告：src/features/edit/core/realtime_playback//

批次：batch-175 [P1]（编号区间 AUD-17500–AUD-17599）
审查日期：2026-09-28
文件：2 个，713 行（timeline_gpu_compositor.py 399 行 / types.py 314 行）

---

## src/features/edit/core/realtime_playback/timeline_gpu_compositor.py (行数: 399)

### 发现

- [AUD-17500] [Major] attach_backend 部分连接失败不回滚已建立的信号连接，detach_backend 依据 _connected 跳过断连导致连接与对象泄漏
  - 位置: src/features/edit/core/realtime_playback/timeline_gpu_compositor.py:159
  - 证据:
    ```python
    self._backend = gpu_renderer
    if not self._connected:
        try:
            gpu_renderer.frame_ready.connect(self._on_backend_frame_ready)
            if hasattr(gpu_renderer, "present_frame_ready"):
                gpu_renderer.present_frame_ready.connect(self._on_backend_present_frame_ready)
            gpu_renderer.render_error.connect(self._on_backend_render_error)
            self._connected = True
        except Exception as e:
            logger.debug(f"attach_backend signal connect failed: {e}")
    ```
    而 detach_backend（170–180 行）只在 `if self._backend is not None and self._connected:` 时才 disconnect。
  - 影响: 若 `frame_ready.connect` 成功后 `render_error.connect` 抛出（信号属性缺失或底层 QObject 已销毁），`_connected` 保持 False 但 frame_ready 已连接。调用方（src/features/edit/core/timeline_preview_renderer.py:6611-6613 检测 `_connected` 失败后调用 `compositor.shutdown()`）→ detach_backend 因 `_connected=False` 跳过全部 disconnect：backend 长期持有 compositor 槽引用（对象泄漏），且渲染线程每帧仍向僵尸 compositor 做跨线程空转发。失败仅 debug 级日志，问题不可见。
  - 建议: 连接前先探测全部所需信号再逐个连接；异常路径主动断开已建立的连接（回滚）；断连处单独捕获 TypeError/RuntimeError 并记 warning，而非依赖 `_connected` 单旗标。
  - 置信度: 高

- [AUD-17501] [Major] _build_backend_layer 兜底分支为 backend 必然拒绝的 payload 类型构造任务，submit 返回 True 但帧永不产出且 miss 归因误导
  - 位置: src/features/edit/core/realtime_playback/timeline_gpu_compositor.py:324
  - 证据:
    ```python
    return {
        "decoded_frame": payload,
        "frame_type": frame.payload_type,
        "track_info": track_info,
    }
    ```
    backend 只接受受限的 frame_type（src/features/edit/core/edit_gpu_preview_renderer.py:2197 `elif decoded_frame is not None and frame_type in ('planar', 'vt_planar')`），其余落入 2312 行 `reason=f"unsupported_type:{frame_type}"` 的 skip 分支。
  - 影响: `FrameHandle.payload_type` 为 "cpu_ndarray" 或 "placeholder"（types.py:155 注明的合法值）时，`submit()` 返回 True，但 backend 必然 track_skip_unsupported → render_error("unsupported_type:cpu_ndarray") → `classify_miss_reason` 因 "unsupported" 子串归为 UNSUPPORTED_EFFECT → policy 永不 rebuffer（policy.py:319-321），只能永久 hold+警告；且错误归因（"不支持的效果"）与真实原因（payload 类型无实时路径）不符，排障困难。现有测试仅覆盖 planar_yuv payload（tests/edit/test_gpu_compositor_real_attach.py:53）。生产当前主路径走 submit_backend_task 未触发，但这是本模块对外的 scheduler-facing 契约（submit/_build_backend_layer）。
  - 建议: 在 submit()/`_build_backend_layer` 对非 gpu_texture/planar_yuv 的 payload 显式拒绝并返回语义化 miss reason；或在契约文档与类型上固化仅支持两种 payload。
  - 置信度: 高

- [AUD-17502] [Suggestion] gpu_composite_frames 统计语义错位：计数的是提交被接受而非合成成功
  - 位置: src/features/edit/core/realtime_playback/timeline_gpu_compositor.py:237
  - 证据:
    ```python
    self._submitted += 1
    try:
        ok = self._backend.submit(backend_task)
        if ok:
            self._gpu_composite_count += 1
    ```
    stats() 中以 `"gpu_composite_frames"` 暴露（389–390 行），文件头注明用于"§13.3 验收"。
  - 影响: backend 接受后渲染失败、deadline 丢弃、错误帧均被计入"已 GPU 合成"，验收指标被高估；frame_ready 转发路径（349–354 行）不计数，submitted 与实际产出帧无法对账。
  - 建议: 在 `_on_backend_frame_ready` 中计数完成帧，或拆分 submitted / accepted / completed 三项指标。
  - 置信度: 高

- [AUD-17503] [Suggestion] reset_generation 的 _generation 为只写死状态，backend 重置失败被无日志吞掉
  - 位置: src/features/edit/core/realtime_playback/timeline_gpu_compositor.py:334
  - 证据:
    ```python
    def reset_generation(self, generation_id: int) -> None:
        """seek 后重置 generation，丢弃旧任务结果。"""
        self._generation = int(generation_id)
        if self._backend is not None:
            reset_fn = getattr(self._backend, "set_generation_id", None)
            if callable(reset_fn):
                try:
                    reset_fn(generation_id)
                except Exception:
                    pass
    ```
  - 影响: `self._generation`（141 行定义）全文件无任何读取；docstring 声称的"丢弃旧任务结果"实际完全依赖 backend 的 generation fence（edit_gpu_preview_renderer.py:909-935），本类转发处理器 `_on_backend_frame_ready` 不做过滤。backend `set_generation_id` 异常被 `except Exception: pass` 完全吞掉且无日志，违反"宽泛 except 不得掩盖领域错误"铁律——generation 同步失效后 seek 可能短暂显示旧代帧且无从发现。
  - 建议: 删除死状态或用其在转发处过滤 generation；委托异常至少 logger.warning。
  - 置信度: 高

- [AUD-17504] [Suggestion] 未使用导入：QOpenGLContext、PlaybackMode、SourceKind
  - 位置: src/features/edit/core/realtime_playback/timeline_gpu_compositor.py:29
  - 证据:
    ```python
    from PyQt6.QtGui import QOpenGLContext

    from .types import FrameHandle, PlaybackMode, SourceKind
    ```
    （文件内 QOpenGLContext / PlaybackMode / SourceKind 均无其他引用，grep 全文确认。）
  - 影响: 引入不必要的 PyQt6.QtGui 模块加载；误导读者以为本模块使用 GL context 或播放模式逻辑。
  - 建议: 删除三个未用导入（FrameHandle 仅出现在字符串化注解中，可保留或改 typing 引用）。
  - 置信度: 高

- [AUD-17505] [Suggestion] 统计计数器跨线程读写无锁：worker 线程自增、GUI/telemetry 线程读取与清零
  - 位置: src/features/edit/core/realtime_playback/timeline_gpu_compositor.py:233
  - 证据:
    ```python
    self._submitted += 1
    try:
        ok = self._backend.submit(backend_task)
        if ok:
            self._gpu_composite_count += 1
    ```
    提交发生在 worker 线程（timeline_preview_renderer.py:2587 `compositor_submit(task)`），stats()/reset_stats() 经 gpu_pipeline_stats（timeline_preview_renderer.py:7360-7370）由 GUI 侧周期采样调用。
  - 影响: CPython 下 `+=` 仍非原子，与 reset_stats 并发时计数可能丢失或瞬时错位；仅影响 telemetry 准确性，无功能后果。
  - 建议: 加 threading.Lock 保护，或在注释中明确 counters 为 advisory-only 语义。
  - 置信度: 高

- [AUD-17506] [Suggestion] submit() 输入验证不足：opacity 未 clamp/NaN 检查，输出尺寸未验证正值
  - 位置: src/features/edit/core/realtime_playback/timeline_gpu_compositor.py:283
  - 证据:
    ```python
    track_info = {
        "opacity": float(layer.opacity),
    ```
    submit() 仅校验 layers 非空（200–202 行）；GpuLayerInput.opacity（57 行）与 TimelineGpuRenderTask.output_width/height（79–80 行）无范围校验。
  - 影响: 越界或 NaN opacity 直接进入 backend 混合路径，可能产生全透明/异常混合的渲染错误且难归因；非正尺寸会传给 backend 的 FBO 创建路径。
  - 建议: submit 入口对 opacity 做 [0,1] clamp 与 NaN 拒绝、对尺寸做正值校验，失败走 render_error 信号。
  - 置信度: 中

### 通过项
- GPU 资源生命周期：本模块不创建/持有任何纹理或 GL 对象，GpuFrameHandle.release 回调归 GpuFramePool 调用方管理
- 线程安全（主体）：compositor 自身不调用 OpenGL API（符合"UI 线程不得操作 GL"铁律），GL 全部委托 backend；backend→compositor 信号为跨线程 queued 转发
- 信号防递归：backend→compositor→consumer 单向转发，无回馈路径，无递归风险
- GLSL·HDR：无 shader 代码，未引入 clamp(0,1)
- 状态提交一致性：不涉及 undo/redo/revision/持久化
- 异常与取消路径：submit_backend_task 将 backend.submit 异常转为 render_error 信号并返回 False；worker 侧对未提交任务释放 layer 数据（timeline_preview_renderer.py:2591-2595）
- 输入验证（基础项）：空 layers、backend 缺失均有显式拒绝路径（不足处见 AUD-17506）
- 类型注解：公开方法签名注解完整（backend/transform/crop 为 Any 属迁移期设计，docstring 已说明）

---

## src/features/edit/core/realtime_playback/types.py (行数: 314)

### 发现

- [AUD-17507] [Suggestion] classify_miss_reason 子串匹配顺序敏感且 "effect" 单词级宽匹配，存在持续性失败被误归为瞬态/反之的误分类风险
  - 位置: src/features/edit/core/realtime_playback/types.py:284
  - 证据:
    ```python
    if (
        "not ready" in text
        or "warming" in text
        or "prefetch" in text
        or "no layers" in text
        or "source_frame" in text
        or "stream" in text
    ):
        return MissReason.CACHE_WARMING
    if "unsupported" in text or "no realtime" in text or "effect" in text:
        return MissReason.UNSUPPORTED_EFFECT
    ```
    CACHE_WARMING 短语先于 DECODE 短语（288–295 行的 "open failed"/"decode"/"deadline"）检查。
  - 影响: policy.should_rebuffer 对 UNSUPPORTED_EFFECT 永不 rebuffer（policy.py:319-321）。含 "effect" 的解码失败串（如 "effect asset decode failed"）会被截胡归为 UNSUPPORTED_EFFECT → 永久 hold；含 "stream" 的持久性失败（如 "open failed ... stream"）被归为瞬态 CACHE_WARMING。现有已知生产串（"clip-A: stream not ready" 等，timeline_decode_scheduler.py:543/556）已被 tests/edit/test_realtime_playback_policy.py:297 锁定行为正确，但新增生产串无顺序保护，且 `for member in MissReason` 的 value 子串命中同样受枚举定义顺序支配。
  - 建议: 生产方改为结构化 reason（枚举/前缀协议）而非子串启发；至少为顺序敏感的复合串补回归测试。
  - 置信度: 中

- [AUD-17508] [Suggestion] FrameHandle 默认 dataclass eq/hash 语义对 ndarray payload 是潜在陷阱
  - 位置: src/features/edit/core/realtime_playback/types.py:123
  - 证据:
    ```python
    @dataclass
    class FrameHandle:
        ...
        payload: object
        payload_type: str  # "gpu_texture" | "planar_yuv" | "cpu_ndarray" | "placeholder"
    ```
  - 影响: 默认 eq=True 生成逐字段 `__eq__`——payload 为 ndarray 时 `bool(handle1 == handle2)` 抛 "truth value is ambiguous"；同时 `__hash__` 被置 None 不可哈希，`LayerFrameBundle`（frozen，含 Tuple[FrameHandle,...]）生成的 `__hash__` 一旦被 set/dict 使用即 TypeError。当前生产代码未对 FrameHandle 使用 ==/hash（frame_ring_buffer 按 int 键，已核实），属未来调用面的潜在陷阱。
  - 建议: 显式 `eq=False`（以 identity 判等）或自定义 `__eq__`/`__hash__` 只基于 FrameIdentity，并在 docstring 固化约束。
  - 置信度: 高（机制确定，影响为潜在）

### 通过项
- 线程安全：纯 dataclass/enum 无共享可变状态，不依赖 Qt（与模块 docstring 声明一致）
- GPU 资源生命周期：无 GL 资源持有或释放逻辑
- GLSL·HDR：无 shader 代码、无 clamp(0,1) 引入
- 信号防递归：无 Qt 信号
- 状态提交一致性：不涉及 undo/redo/revision/持久化
- 异常与取消路径：classify_miss_reason 对 None/空串有显式兜底（DELEGATE_MISS），不抛异常
- 输入验证：PlaybackMode/SourceKind 的模式语义与规格书一致（REALTIME 禁 master、is_realtime_ready 排除 memory_proxy/master、allows_blocking 限定 PAUSED/STEP/EXPORT）
- 缓存键设计：FrameIdentity frozen 且 key 含 clip_id，符合 §5.3"同素材多 clip 不共用 decode cursor"要求；LayerFrameBundle.is_stale generation 语义正确
- 类型注解：完整（proxy_info/render_cache_info 为 object 属策略层多态设计，已注明）
