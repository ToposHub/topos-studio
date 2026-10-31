# batch-361 审查报告 — src/shared/media/windows_hw//

- 批次：batch-361 [P2]，编号区间 AUD-36100–AUD-36199
- 文件数：8（1,765 行），全部 Read 通读
- 汇总：Critical 1 / Major 7 / Suggestion 12
- 关联契约文件（只读核对，不属于本批次）：`d3d11_frame.py`、`gpu_encode_surface.py`、`src/shared/export/gpu_trail.py`、`reasons.py`

---

## src/shared/media/windows_hw/d3d11_pipeline.py (行数: 240)

### 发现

- [AUD-36100] [Critical] done-callback 异常逃逸会泄漏信号量槽且 outcome 永不完成，run() 无超时等待导致导出线程永久死锁
  - 位置: src/shared/media/windows_hw/d3d11_pipeline.py:104-120（及 169）
  - 证据:
    ```python
    def _render_done(done: Future[D3D11RenderResult]) -> None:
        try:
            render_result = done.result()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            ...
            frame.release()
            self._finish_failed(outcome, f"render:{exc}")
            return
        ...
        if not render_result.ok or render_result.target is None:
            frame.release()            # 不在任何 try 内
            self._finish_failed(outcome, render_result.reason or "render_failed")
    ```
    以及 `run()` 中 `completed = sum(1 for future in futures if future.result())`（169 行，无 timeout）。
  - 影响: 除 `(OSError, RuntimeError, TypeError, ValueError)` 之外的异常（如 render 抛 `AttributeError`/`KeyError`/`MemoryError`），或 117-119 行的 `frame.release()` 自身抛 `ValueError`（lease 重复归还在 `d3d11_texture_pool.py:113` 就是抛 ValueError）时，异常逃出 `_render_done`；`Future.add_done_callback` 只吞掉并记日志。此后 `_finish_failed` 永不执行：`_slots.release()` 不被调用（容量永久 -1），`outcome` 永无结果，`run()` 的 `future.result()` 无超时 → 整条导出管线挂死，只能杀进程。信号量逐帧损失也会让后续 `submit` 全部走 `surface_pool_timeout`。
  - 建议: 回调整体包一层兜底 `except BaseException`（记日志后强制走 `_finish_failed(outcome, ...)`）；或改用"每帧 finally 归还槽位"的结构；`run()` 的 `future.result()` 加超时并按失败计。
  - 置信度: 高

- [AUD-36101] [Major] render 结果"target 非空但带 reason"时 target 的 lease 不释放，渲染目标池槽位泄漏
  - 位置: src/shared/media/windows_hw/d3d11_pipeline.py:117-120
  - 证据:
    ```python
    if not render_result.ok or render_result.target is None:
        frame.release()
        self._finish_failed(outcome, render_result.reason or "render_failed")
        return
    ```
    而 `D3D11RenderResult.ok` 定义为 `self.target is not None and not self.reason`（d3d11_render_graph.py:144-146），`_render_result_from_mapping`（d3d11_render_graph.py:317-328）允许构造 target 与非空 reason 并存的结果。
  - 影响: 原生桥返回 `{"target": ..., "reason": "..."}`（映射归一化路径明确支持）时，该分支只归还 frame lease，`render_result.target.lease` 被丢弃不归还 → 渲染目标纹理池 in_use 只增不减，最终 `acquire` 超时、`close()` 拒绝关闭。同文件 122-129 行的 fence 违约路径都记得双释放，唯独此分支漏了。
  - 建议: 该分支补 `if render_result.target is not None: render_result.target.lease.release()`。
  - 置信度: 高

- [AUD-36102] [Major] 遥测缺陷：render_waits 恒为 0，encode_waits 实际统计的是 surface 池超时
  - 位置: src/shared/media/windows_hw/d3d11_pipeline.py:84-85, 91-94
  - 证据:
    ```python
    self._render_waits = 0
    self._encode_waits = 0
    ...
    if not self._slots.acquire(timeout=max(0.0, timeout_s)):
        with self._lock:
            self._encode_waits += 1
    ```
    全仓 grep 确认 `_render_waits` 只有初始化与读取，无任何自增点。
  - 影响: `D3D11PipelineTelemetry.render_waits` 永远报告 0；唯一自增点把"surface 池等待超时"记成 `encode_waits`。该遥测是 ExportGpuTrail/门禁证据的消费对象，等待类证据失真会掩盖反压瓶颈（比如把编码反压误诊为解码过快）。
  - 建议: 语义拆开：池超时计入独立计数（如 `pool_timeouts`），render/encode 真实等待点（render_future/encode_future 完成前的阻塞）分别计数。
  - 置信度: 高

- [AUD-36103] [Suggestion] `submit()` 的 closed 检查与 executor 提交之间存在 TOCTOU 竞态
  - 位置: src/shared/media/windows_hw/d3d11_pipeline.py:88-102
  - 证据:
    ```python
    if self._closed:
        raise RuntimeError("D3D11PipelineScheduler 已关闭")
    if not self._slots.acquire(...):
    ...
    render_future = self._render_executor.submit(self._render, frame)
    ```
  - 影响: 另一线程在此间隙 `close()` 时 `executor.submit` 抛 `RuntimeError`，`run()` 未捕获直接中断；且此时 frame lease 与信号量槽的归还路径不明确（异常发生在记账之后、回调注册之前）。
  - 建议: 把 `executor.submit` 包进 try/except，失败时按 `_finish_failed` 语义归还槽位并释放 frame；或用锁保护 closed+submit 原子性。
  - 置信度: 中

### 通过项
- 线程安全：计数器与 trail 阶段去重均持 `self._lock`，单工作线程 executor 串行化 render/encode
- GPU 资源生命周期：submit 超时、render 异常、fence 违约、encode 失败各路径的 frame/target lease 归还在正常异常族内完整（除上述两处缺陷）
- 输入验证：`surface_depth` 4–8、`frame_count >= 1` 均显式拒绝
- 异常与取消路径：`close()` shutdown(wait=True) 且不 cancel 在途任务；trail.set_fallback_reason 的 ValueError 已容忍
- 状态提交一致性：不涉及 undo/revision；trail 阶段登记幂等（ValueError 容忍注释明确）
- 类型注解：公共 API 与回调注解完整

---

## src/shared/media/windows_hw/d3d11_passthrough_chain.py (行数: 204)

### 发现

- [AUD-36104] [Major] `release_after` 异常路径泄漏共享 lease（decode 池槽位永久损失）
  - 位置: src/shared/media/windows_hw/d3d11_passthrough_chain.py:175-180
  - 证据:
    ```python
    try:
        surface.release_after(fence)
    except (RuntimeError, ValueError) as exc:
        reason = f"encode_submit:{FallbackReason.GPU_PROBE_FAILED.value}:{exc}"
        self._trail.set_fallback_reason(reason)
        return self._outcome(completed, False, reason)
    ```
    而 `GpuEncodeSurface` 与来源帧共享同一 lease（gpu_encode_surface.py:46-51"handoff 后 lease 的唯一责任人变为编码路径……来源帧对象此后不得再单独 release"）。
  - 影响: `release_after` 抛 ValueError（如 fence 缺 `wait()`，d3d11_texture_pool.py:120-121）时，surface/共享 lease 均未归还即返回失败 → 解码纹理池 in_use 永久 +1，池最终耗尽且 `close()` 被拒。对比 d3d11_pipeline.py:122-129 对同类错误的正确双释放处理，本链漏了对称处理。
  - 建议: except 分支补 `surface.release()`（fence 违约时尚未进入异步回收，直接归还安全；若已判定 fence 无效则不会产生后台线程）。
  - 置信度: 高

- [AUD-36105] [Major] `record_stage` 未防御 trail 重复登记 ValueError：同一 trail 二次 run() 即抛出，且已读 frame 泄漏
  - 位置: src/shared/media/windows_hw/d3d11_passthrough_chain.py:126-149
  - 证据:
    ```python
    def record_stage(name: str, backend: str, gpu_ms: object) -> None:
        if name in recorded_stages:
            return
        recorded_stages.add(name)
        self._trail.record_stage(
            TrailStage(name=name, backend=backend, ...)
        )
    ...
    frame, why = self._decode_source.read_frame()
    ...
    record_stage("decode", "d3d11va", frame.gpu_decode_ms)
    ```
    而 `ExportGpuTrail.record_stage` 对重复段名抛 `ValueError("trail 段重复登记: ...")`（gpu_trail.py:252-256）；`recorded_stages` 是 run() 的局部变量，只防本帧循环内的重复。
  - 影响: 同一 trail 对象第二次调用 `run()`（或调用方曾预登记过同名阶段——d3d11_pipeline.py:232-234 的注释明确承认这种注入场景）时，第一帧的 `record_stage` 即抛 ValueError，`run()` 中断且刚 `read_frame()` 到的 frame 未 release → 解码池槽泄漏 + 异常裸传调用方。
  - 建议: 仿照 `_record_trail_stage`（d3d11_pipeline.py:218-234）用 try/except ValueError 包裹；或在 prepare() 阶段校验 trail 尚无像素驻留段。
  - 置信度: 高

- [AUD-36106] [Suggestion] `record_stage` 参数 `gpu_ms: object` 弱类型且 `float(gpu_ms)` 无类型守卫
  - 位置: src/shared/media/windows_hw/d3d11_passthrough_chain.py:126-136
  - 证据:
    ```python
    def record_stage(name: str, backend: str, gpu_ms: object) -> None:
        ...
        gpu_submit_ms=(float(gpu_ms) if gpu_ms is not None else None),
    ```
  - 影响: 若上游帧对象误填字符串时间戳，`float()` 抛 ValueError 逃出到 run() 调用方；注解 `object` 失去静态检查价值。
  - 建议: 签名改 `gpu_ms: float | None`，或在入口做 isinstance 拒绝。
  - 置信度: 高

### 通过项
- 输入验证：`frame_count < 1` 显式拒绝；未 prepare 即 run() 走 fail-closed 返回而非异常
- 线程安全：单线程顺序消费，无共享可变状态（trail 自身线程安全）
- GPU 资源生命周期：decode 失败、surface 校验失败、submit 失败、fence 缺失各路径的 lease 归还正确（除 AUD-36104）
- 状态提交一致性：LUID 三段一致性校验与 ADAPTER_MISMATCH 词表 fail-closed，符合计划 §3.3
- 类型注解：PassthroughOutcome/Protocol 定义完整

---

## src/shared/media/windows_hw/d3d11_render_graph.py (行数: 328)

### 发现

- [AUD-36107] [Major] `D3D11TimelineRenderer` 状态无锁：release() 与 render_frame() 并发时 AttributeError 逃出声明的异常契约
  - 位置: src/shared/media/windows_hw/d3d11_render_graph.py:273-293, 301-314
  - 证据:
    ```python
    def render_frame(self, frame: D3D11GpuFrame) -> D3D11RenderResult:
        if self._plan is None or self._backend is None:
            ...
        try:
            raw = self._backend.render_frame(
                self._plan.to_dict(), frame.texture_ptr, ...
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
    ```
    而 `release()` 会把 `self._backend = None; self._plan = None`（313-314 行）。
  - 影响: 检查（274 行）与使用（285-286 行）之间被并发 `release()` 置空时，`self._plan.to_dict()` 抛 `AttributeError`——不在 except 元组内，逃出到渲染执行线程；经 d3d11_pipeline.py 的 `_render_done`（同样不捕 AttributeError）直接触发 AUD-36100 的死锁路径。`_plans_prepared`/`_frames_rendered` 的非原子自增也在此无锁上下文内。
  - 建议: 方法入口快照 `plan = self._plan; backend = self._backend` 后仅操作局部变量，并对 None 快照走 fail-closed 返回；或加实例锁串行化 prepare/render/release。
  - 置信度: 中（需要并发 release，正常先 prepare→render→release 的用法不触发；但该类面向多线程渲染线程设计）

- [AUD-36108] [Suggestion] `D3D11RenderTarget` 校验不全；transition 词表与 shader 注册表双重维护
  - 位置: src/shared/media/windows_hw/d3d11_render_graph.py:127-134, 198-204
  - 证据:
    ```python
    def __post_init__(self) -> None:
        if not self.texture_ptr or self.width <= 0 or self.height <= 0:
            raise ValueError("D3D11 render target 无效")
        if not self.adapter_luid:
            raise ValueError("D3D11 render target 缺少 adapter LUID")
    ...
        if name not in {"wipe", "iris", "slide", "user"}:
            raise ValueError(f"D3D11 transition 未声明: {name}")
    ```
  - 影响: `dxgi_format`（任意 int 可过）、`lease`/`completion_fence` 类型未校验，坏值延迟到消费端才爆；`{"wipe","iris","slide","user"}` 与 `D3D11_SHADER_SOURCES` 的 `d3d11.transition.*` 键重复维护，新增过渡需改两处，漏改则运行期 ValueError。
  - 建议: 校验 `dxgi_format in (DXGI_FORMAT_NV12, DXGI_FORMAT_P010)`；transition 集合改为从 shader 注册表推导（前缀过滤）。
  - 置信度: 高

### 通过项
- GLSL·HDR 正确性：全管线中间格式 rgba16f，decode/effect/blend 均在浮点域，无 clamp(0,1)；NV12/P010 输出走独立 shader 键，P010 用 `RWTexture2D<uint>` 保持 10-bit MSB 对齐
- 输入验证：pass 名/格式白名单、shader 存在性、layers/intermediate_slots 约束在 `__post_init__` 全量 fail-fast；effect 名经 `D3D11_EFFECTS` 词表校验
- GPU 资源生命周期：计划编译零资源分配；`release()` 对 backend 释放失败容忍且保持 fail-closed（注释明确不掩盖导出结果）
- 异常与取消路径：prepare/render 的原生异常族收敛为 (bool, reason)，fail-closed 返回不抛裸异常（除 AUD-36107 的并发窗口）
- 状态提交一致性：plan 绑定 timeline_revision/renderer_version，cache_key 含全部决定性字段
- 类型注解：dataclass 字段与 Protocol 完整

---

## src/shared/media/windows_hw/d3d11_texture_pool.py (行数: 467)

### 发现

- [AUD-36109] [Major] fence 回收线程在 `wait` 返回 False 或抛出 OSError 时静默丢失池槽；fence 永不 signal 时阻塞线程无限堆积
  - 位置: src/shared/media/windows_hw/d3d11_texture_pool.py:126-137（External 变体同构 178-189）
  - 证据:
    ```python
    def _wait_and_release() -> None:
        try:
            if bool(wait(None)):
                self.release()
        except (RuntimeError, ValueError) as exc:
            logger.warning("D3D11 fence lease 回收失败: %s", exc)

    threading.Thread(
        target=_wait_and_release,
        name="topos-d3d11-lease-reclaimer",
        daemon=True,
    ).start()
    ```
  - 影响: `wait(None)` 返回 False（fence 出错/被丢弃）时既不 release 也无任何日志——纹理槽无声泄漏，累计到 depth 次后 `acquire` 全超时、`close()` 因 in_use>0 拒绝关闭且无诊断线索；fence 永不 signal 时每个在途帧留一个永久阻塞的守护线程（1000 帧门禁场景即 1000 个死线程）；`wait` 抛 OSError（不在捕获族）时线程带异常死亡、槽同样丢失。
  - 建议: False 分支至少 `logger.error` 并计数暴露到 stats；捕获族扩为 `(OSError, RuntimeError, ValueError)`；考虑带超时的 wait + 重试/告警上限。
  - 置信度: 高（代码路径确定；实际触发依赖 fence 实现的可靠性）

- [AUD-36110] [Suggestion] 每次 fence 归还新建一个 Thread，违背"逐帧零分配"的池设计精神
  - 位置: src/shared/media/windows_hw/d3d11_texture_pool.py:133-137, 185-189
  - 证据:
    ```python
    threading.Thread(
        target=_wait_and_release,
        name="topos-d3d11-lease-reclaimer",
        daemon=True,
    ).start()
    ```
  - 影响: 4K/60fps 目标下每秒 ~60 次线程创建/销毁（含内核态开销与 name 字符串拼接），与模块头部"逐帧不得创建纹理或等待 GPU"的零分配门禁方向相悖；高帧率长导出下是可测的 CPU 抖动源。
  - 建议: 单一常驻回收线程 + 待回收队列（fence, lease），或复用 ThreadPoolExecutor(max_workers=1)。
  - 置信度: 高（性能模式判断；量级属 Suggestion）

- [AUD-36111] [Suggestion] `_return_texture` 在持有 `self._condition` 时调用外部 releaser，与 `close()` 的锁外释放策略不一致
  - 位置: src/shared/media/windows_hw/d3d11_texture_pool.py:361-369
  - 证据:
    ```python
    def _return_texture(self, texture_ptr: int) -> None:
        with self._condition:
            ...
            if self._state.closed:
                self._state.in_use -= 1
                self._releaser(texture_ptr)   # 持锁调用注入回调
                return
    ```
    对比 `close()`（387-390 行）先把 to_release 拷出锁外再释放。
  - 影响: 关闭后每笔在途归还都在池锁内执行 COM Release；releaser 阻塞/抛错会拖住所有 `acquire`/`stats`，抛错还会把异常送进 fence 回收线程。
  - 建议: 与 close() 对齐——锁内只改状态并收集指针，锁外调用 releaser。
  - 置信度: 高

- [AUD-36112] [Suggestion] `D3D11TextureLease` 与 `D3D11ExternalTextureLease` 防御不一致；`_PoolState` 字段缺类型参数
  - 位置: src/shared/media/windows_hw/d3d11_texture_pool.py:100-108, 191-196, 227-237
  - 证据:
    ```python
    class D3D11TextureLease:
        @property
        def released(self) -> bool:
            return self._released        # 无锁读，External 变体持 _release_lock

    @dataclass
    class _PoolState:
        free: set = field(default_factory=set)   # 无元素类型
    ```
    External 变体有 `__enter__/__exit__`，池内 lease 没有。
  - 影响: 同一 Protocol 的两个实现锁纪律与上下文管理器支持不一致，调用方容易写出只对一个变体成立的生命周期代码；`set`/`list` 裸注解损失静态检查。
  - 建议: 统一两变体（锁、上下文管理器、released 语义）；`free: set[int]`。
  - 置信度: 高

- [AUD-36113] [Suggestion] `create_d3d11_texture_pool` 只捕 RuntimeError/ValueError，注入工厂抛 OSError 时违反 `(None, reason)` 契约
  - 位置: src/shared/media/windows_hw/d3d11_texture_pool.py:451-467
  - 证据:
    ```python
    try:
        pool = D3D11TexturePool(...)
    except RuntimeError as exc:
        return None, param(FallbackReason.GPU_PROBE_FAILED, f"create_texture:{exc}")
    except ValueError as exc:
        return None, param(FallbackReason.GPU_PROBE_FAILED, f"invalid_pool:{exc}")
    ```
  - 影响: docstring 承诺"失败返回 (None, reason)"；若注入的 texture_factory 抛 OSError（ctypes WinError 即常见 OSError），异常穿透便捷入口直达调用方，绕过词表回退。
  - 建议: 增捕 OSError（归入 create_texture 分支），保持入口契约封闭。
  - 置信度: 中（默认工厂只抛 RuntimeError，风险仅限注入路径）

### 通过项
- 线程安全：Condition 全程护住 free/in_use/统计；acquire 超时用单调钟剩余时间，无忙等
- GPU 资源生命周期：构造期建满+封口、失败整体回滚不交付残池；close() 在途 lease fail-closed 拒绝；跨池/重复归还在词表层显式报错
- 输入验证：depth/格式白名单（NV12/P010）/宽高正偶数全量校验；空指针工厂产物拒绝
- 性能模式：定长池 + try/acquire 双原语、超时计数可审计，符合反压设计
- 类型注解：公共 API（TextureFactory、stats、lease Protocol）注解完整

---

## src/shared/media/windows_hw/d3d11_gl_bridge.py (行数: 127)

### 发现

- [AUD-36114] [Major] `destroy()` 清空 `_active_slots` 与 in-flight `bind_texture` 竞态，可致同一 ring 槽双重占用
  - 位置: src/shared/media/windows_hw/d3d11_gl_bridge.py:76-94, 110-114
  - 证据:
    ```python
    def bind_texture(self, ...):
        ...
        with self._lock:
            if slot in self._active_slots:
                ...
            self._active_slots.add(slot)
        try:
            planes, native_release = self._interop_factory(...)

    def destroy(self) -> None:
        self._initialized = False            # 锁外写
        with self._lock:
            self._active_slots.clear()
    ```
  - 影响: bind 已占槽、工厂调用进行中时 destroy 清空集合：该槽可被下一次 bind 再次分配，同一 ring 槽出现两个活跃原生 interop 对象，破坏环化复用的核心不变量（旧对象的 `_release_slot` 之后 discard 的也是新绑定的槽记录，计数进一步漂移）。`_initialized` 锁外写与 67 行锁外读同病。
  - 建议: destroy 置位 `closed` 标志（锁内），bind 的占用检查同时校验 closed；或 destroy 等待在途绑定完成（引用计数/事件）。
  - 置信度: 中（窗口窄，但该类存在目的就是维护此不变量）

- [AUD-36115] [Suggestion] 诊断状态锁纪律松散：`_last_fallback_reason` 锁外写、`fail_count`/`_initialized` 无锁读；`frame_index` 未验证
  - 位置: src/shared/media/windows_hw/d3d11_gl_bridge.py:62-65, 75, 117-126
  - 证据:
    ```python
    def _count_failure(reason: str) -> None:
        with self._lock:
            self._fail_count += 1
        self._last_fallback_reason = reason      # 锁外
    ...
    slot = frame_index % self._ring_size          # 负数 frame_index 未拒绝
    ```
  - 影响: 并发失败时 reason 可能被乱序覆盖，诊断口径与计数不一致；负 `frame_index` 产生负槽键，环语义失效且永不与正常槽冲突，问题被掩盖。
  - 建议: reason 写入并入锁内；`frame_index` 加非负校验。
  - 置信度: 高

### 通过项
- 输入验证：空 d3d_texture、工厂返回无效 plane/release 均显式拒绝并计入 reason
- 异常与取消路径：工厂异常族收敛为 (None) + 词表 reason，槽位在失败路径正确回收（`_active_slots.discard`）
- GPU 资源生命周期：release 回调 try/finally 保证槽归还幂等；destructor_policy="warn_only" 明示
- 类型注解：InteropFactory/属性注解完整

---

## src/shared/media/windows_hw/gpu_capability.py (行数: 302)

### 发现

- [AUD-36116] [Suggestion] `check_adapter_match` 把两个空 LUID 判为匹配（"" == ""）
  - 位置: src/shared/media/windows_hw/gpu_capability.py:270-275
  - 证据:
    ```python
    if render_adapter_luid == decode_adapter_luid:
        return True, ""
    return False, param(FallbackReason.ADAPTER_MISMATCH, ...)
    ```
  - 影响: 未探测的两侧（空串对空串）会得到 `match=True`，跨适配器硬前置被绕过——正是 W3 interop 最需要防的"未探测即放行"。本文件内 `decode_render_adapter_report` 有前置空值守卫，但该函数是公共 API，其他调用方（如 d3d11 链路直接复用）无守卫即踩坑。
  - 建议: 函数入口拒绝空 LUID（返回 False + `*_unprobed` reason）。
  - 置信度: 高（逻辑确定；风险取决于外部调用方）

- [AUD-36117] [Suggestion] `_probe_driver_version_nvml` 宽 `except Exception` 且 NVML 返回码全部未检查
  - 位置: src/shared/media/windows_hw/gpu_capability.py:187-197
  - 证据:
    ```python
    try:
        nvml = ctypes.CDLL("nvml.dll")
        nvml.nvmlInit_v2()
        version = ctypes.create_string_buffer(80)
        nvml.nvmlSystemGetDriverVersion(version, len(version))
        nvml.nvmlShutdown()
        return version.value.decode("utf-8", errors="replace")
    except Exception:
        return ""
    ```
  - 影响: `nvmlInit_v2`/`GetDriverVersion` 失败时返回非零码而不抛异常，缓冲区残留内容被当作驱动版本返回（脏数据进诊断 JSON）；宽 except 也会掩盖 `MemoryError` 等无关错误，与"不得宽泛 except 掩盖领域错误"铁律相悖。
  - 建议: 检查两次调用的 nvmlReturn_t（0=NVML_SUCCESS 才采信）；except 收窄为 `(OSError, AttributeError)`。
  - 置信度: 高

- [AUD-36118] [Suggestion] 适配器枚举中途异常时仍报 `probe_ok=True`，与 `factory_failed` 的语义不一致
  - 位置: src/shared/media/windows_hw/gpu_capability.py:213-234
  - 证据:
    ```python
    except Exception as e:
        logger.warning(f"DXGI adapter enumeration failed: {e}")
    finally:
        _com_release(factory)

    cap = WindowsGpuCapability(
        probe_ok=True,
        reason="ok" if adapters else "no_adapter",
    ```
  - 影响: 枚举在首个适配器即抛错时（adapters 为空）得到 `probe_ok=True, reason="no_adapter"`——把"探测过程失败"伪装成"探测成功但无适配器"，下游门禁会做出错误归因。
  - 建议: 记录枚举是否完整完成，失败时 probe_ok=False + `enum_failed:<...>`。
  - 置信度: 高

- [AUD-36119] [Suggestion] 类型注解缺失与 logger 即时 f-string 格式化
  - 位置: src/shared/media/windows_hw/gpu_capability.py:80, 84, 63, 224, 235
  - 证据:
    ```python
    adapters: list = field(default_factory=list)
    def to_json(self) -> str: ...
    def to_dict(self) -> dict: ...
    logger.warning(f"DXGI adapter enumeration failed: {e}")
    logger.info(f"WindowsGpuCapability: {cap.to_json()}")
    ```
  - 影响: `list`/`dict` 裸注解失去静态检查；info 级别也无条件做 JSON 序列化（热路径外的探针影响小，属风格）。
  - 建议: `list[AdapterInfo]`、`dict[str, object]`；logger 改 `%s` 惰性格式化。
  - 置信度: 高

### 通过项
- 线程安全：无共享可变状态，纯函数式探针
- GPU 资源生命周期：factory/adapter COM 指针 finally 释放（含枚举失败路径），vtable 索引与 IID 按 SDK 钉死并留真机修正记录
- 输入验证：`parse_adapter_desc` 对短字节防御性补零；LUID 有符号转无符号掩码正确
- 异常与取消路径：非 Windows 诚实拒绝（not_windows）；dxgi 加载/工厂创建失败收敛为 reason
- HDR/GLSL、信号防递归、undo/redo：不适用（无渲染与 Qt 交互）

---

## src/shared/media/windows_hw/d3d11_shaders.py (行数: 46)

### 通过项
- 通过（无逻辑）：纯 HLSL 片段注册表，导出 `D3D11_SHADER_SOURCES` 与 `required_shader_ids()`；片段仅含结构体/资源绑定声明，无数值字面量、无 clamp、无循环，无 HDR 正确性风险；效果变体键与 render_graph 的 effect 词表经 `D3D11_EFFECTS` 间接对齐

---

## src/shared/media/windows_hw/d3d11_sync.py (行数: 51)

### 通过项
- 通过（无逻辑）：线程安全原语——`ImmediateD3D11Fence`（测试用恒真）、`EventD3D11Fence`（`threading.Event` 封装，wait/signal 原子）；`D3D11Fence` runtime_checkable Protocol 契约清晰；无状态泄漏、无输入面

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-36100 |
| Major | 7 | AUD-36101, 36102, 36104, 36105, 36107, 36109, 36114 |
| Suggestion | 12 | AUD-36103, 36106, 36108, 36110–36113, 36115–36119 |

核心风险链：d3d11_render_graph 的无锁 release（36107）→ 逃逸的 AttributeError 进入 d3d11_pipeline 的窄异常族回调（36100）→ 信号量槽泄漏 + run() 无超时死锁；纹理池的 fence 回收（36109）与直通链/调度器的 lease 漏释放（36104/36101）构成第二条容量耗尽路径。
