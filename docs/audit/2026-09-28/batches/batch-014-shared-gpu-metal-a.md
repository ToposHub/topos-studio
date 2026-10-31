# Batch 014 — src/shared/gpu/metal/ 静态审查报告

- 审查日期: 2026-09-28
- 审查范围: macOS Metal compute 后端（PyObjC 路线）8 个文件
- 问题编号区间: AUD-1400 – AUD-1499
- 严重度统计: **Critical 0 / Major 3 / Suggestion 15**
- 跨文件验证: 已核对 `src/features/composite/render/metal_compute_bridge.py`（shared kernel group 传递）、`tests/shared/gpu/metal/test_metal_kernels_rans.py`（status 消费），确认 rans2/DCT/enc kernel 当前仅有测试消费方，宿主尚未接入。

---

## src/shared/gpu/metal/__init__.py (行数: 17)
### 发现
（无）
### 通过项
- 导出面与 context.py 实现一致，无循环导入、无导入副作用。
- `__all__` 与实际导出符号一致。

---

## src/shared/gpu/metal/context.py (行数: 541)
### 发现

- [AUD-1401] [Major] dispatch 异常路径静默丢弃整个累积 command buffer，已成功编码的 kernel 调用一并丢失且调用方无感知
  - 位置: src/shared/gpu/metal/context.py:366-385
  - 证据:
    ```python
    with self._lock:
        if self._current_cb is None:
            self._current_cb = self._queue.commandBuffer()
        cb = self._current_cb
        try:
            enc = cb.computeCommandEncoder()
            ...
            enc.dispatchThreads_threadsPerThreadgroup_(size, tg)
            enc.endEncoding()
        except Exception:
            # 丢弃半编码的 buffer：未 endEncoding 的 encoder 之后无法
            # 再挂新 encoder，提交会触发校验错误——宁可弃置不提交。
            self._current_cb = None
            raise
    ```
  - 影响: `dispatch()` 的契约是多次调用累积编码进同一 command buffer（例如 bridge 的 blur 两趟）。若第 N 个 kernel 编码时抛异常（PyObjC 层参数错误、pipeline 状态失效等），前 N-1 个已合法编码进 `_current_cb` 的 kernel 调用随 buffer 一起被静默丢弃。调用方捕获异常后调用 `submit()` 会得到 `None`（无 pending 工作）而不是错误，若上层只重试失败的那一个 kernel，则输出缺少前序计算，渲染结果错误。
  - 建议: 丢弃时设置一个 `self._dropped_work = True` 标记，让后续 `submit()`/`dispatch()` 抛出「已丢弃未提交工作，需重建整条 kernel 链」的显式错误；或提供 `has_pending_work()` 让调用方区分「无工作」与「工作被丢弃」。
  - 置信度: 中（丢弃逻辑本身正确且注释了动机；风险在于丢失后无任何信号通知调用方，部分重试场景是否存在于上层无法在本批确认）

- [AUD-1402] [Major] PyObjC 路径全程无 autoreleasepool，工作线程上每次 dispatch/submit/read_texture 泄漏 autoreleased Metal 对象
  - 位置: src/shared/gpu/metal/context.py:366-380（dispatch）、387-424（submit）、451-495（read_texture）
  - 证据:
    ```python
    with self._lock:
        if self._current_cb is None:
            self._current_cb = self._queue.commandBuffer()   # autoreleased
        cb = self._current_cb
        try:
            enc = cb.computeCommandEncoder()                  # autoreleased
    ```
    以及 read_texture:
    ```python
    cb = self._queue.commandBuffer()
    blit = cb.blitCommandEncoder()
    blit.synchronizeResource_(texture)
    blit.endEncoding()
    cb.commit()
    cb.waitUntilCompleted()
    ```
  - 影响: PyObjC 遵循 Cocoa 内存约定，`commandBuffer()`/`computeCommandEncoder()` 等返回 autoreleased 对象。在没有 runloop 的工作线程（QThread 不自动创建 NSAutoreleasePool）上调用这些方法，autoreleased 对象不会释放，PyObjC 会输出 "no autorelease pool in place" 警告并慢性泄漏。逐帧 Composite 效果 / 批量导出路径（`metal_compute_bridge.py` 每次效果调用 dispatch×2 + submit + read）会持续积累，长会话内存增长。
  - 建议: 在 `dispatch`/`submit`/`read_texture`/纹理与 buffer 创建等所有产生 PyObjC 对象的公共入口用 `objc.autorelease_pool()` 上下文管理器包裹（`import objc; with objc.autorelease_pool(): ...`）。
  - 置信度: 中（PyObjC 无 pool 线程的 autorelease 行为是确定的；实际泄漏量取决于调用线程是否已有 pool，本批无法确认上层线程模型）

- [AUD-1403] [Suggestion] `_last_gpu_time_ms` 在 submit 锁外读写，并发提交时诊断数据竞态
  - 位置: src/shared/gpu/metal/context.py:400-417
  - 证据:
    ```python
    with self._lock:
        cb = self._current_cb
        self._current_cb = None
    if cb is None:
        return None
    self._last_gpu_time_ms = None          # 锁外写
    ...
    if wait:
        cb.waitUntilCompleted()
        self._last_gpu_time_ms = self._measure_gpu_time_ms(cb)   # 锁外写
    ```
  - 影响: 两个线程并发 submit 时，线程 A 完成等待并写入测量值后，线程 B 的 `= None` 或自己的测量值可能覆盖，`last_gpu_time_ms` 返回的不是「最近一次等待提交」的值。仅影响诊断/基准数据，不影响渲染正确性。
  - 建议: 用局部变量承载测量结果，仅在锁内（或用简单赋值的原子性说明）最后写回；或在 docstring 声明该属性仅在单线程使用时有效。
  - 置信度: 高（代码事实；影响范围限于诊断字段）

- [AUD-1404] [Suggestion] `create_buffer_from_numpy` 实为双重拷贝，与「零额外拷贝语义」注释不符
  - 位置: src/shared/gpu/metal/context.py:185-196
  - 证据:
    ```python
    def create_buffer_from_numpy(self, arr: np.ndarray) -> Any:
        """用 numpy 数组内容创建共享存储 MTLBuffer（零额外拷贝语义）。"""
        data = np.ascontiguousarray(arr)
        byte_length = data.nbytes
        buf = self._device.newBufferWithBytes_length_options_(
            data.tobytes(),          # 拷贝 1：ndarray → bytes
            byte_length,
            ...
    ```
  - 影响: `tobytes()` 产生一次完整拷贝，`newBufferWithBytes` 内部再拷贝一次进 MTLBuffer。4K RGBA16F 一帧 134MB，staging 桥（blur/erode 每次效果 2 个输入 buffer）每效果 2×134MB 额外瞬时分配，加剧内存峰值与耗时。注释声称的语义会误导后续优化判断。
  - 建议: 修正注释，或改用 `newBufferWithBytesNoCopy_length_options_deallocator_` / 从 `arr.ctypes.data` 直接构造消除一次拷贝；至少在 docstring 如实描述两次拷贝。
  - 置信度: 高

- [AUD-1405] [Suggestion] `compile_kernel` 以 kernel 名为缓存键，同名传入不同 MSL 源时返回陈旧 kernel
  - 位置: src/shared/gpu/metal/context.py:138-144
  - 证据:
    ```python
    def compile_kernel(self, name: str, msl_source: str) -> MetalKernel:
        """编译 MSL 并缓存 compute pipeline（同名重复编译返回缓存）。"""
        source_key = hashlib.sha256(msl_source.encode("utf-8")).hexdigest()
        with self._lock:
            cached = self._kernels.get(name)
            if cached is not None:
                return cached
    ```
  - 影响: 当前 MSL 源是模块级常量（进程内不变），无实际触发；但一旦出现热重载/版本升级/测试内重定义源码，调用方会以为新源已编译而实际拿到旧 pipeline。`_libraries` 已按 source_key 缓存，说明作者考虑过源差异，但 kernel 层的 name 键使 source_key 失效。
  - 建议: 缓存键改为 `(name, source_key)`，或在命中 name 缓存时校验 source_key 一致、不一致抛 `MetalComputeError`。
  - 置信度: 高（代码事实；当前常量源下无影响）

- [AUD-1406] [Suggestion] `dispatch` 不校验 threads 正值与 group ≤ maxTotalThreadsPerThreadgroup，非法值直接落入 Metal 校验断言
  - 位置: src/shared/gpu/metal/context.py:353-361
  - 证据:
    ```python
    threads = (int(threads[0]), int(threads[1]), int(threads[2]))
    if group is None:
        max_t = kernel.max_threads_per_group
        if threads[1] <= 1 and threads[2] <= 1:
            group = (min(threads[0], max_t), 1, 1)
        else:
            gx = max(1, min(16, max_t // 16))
            group = (gx, max(1, max_t // gx), 1)
    ```
  - 影响: 调用方传入 `threads=(0,...)`、负数或 `group` 乘积超过 PSO 的 maxTotalThreadsPerThreadgroup 时，错误由 Metal 驱动层以校验断言/编码失败形式暴露（配合 AUD-1401 还会丢弃整个 command buffer），报错位置远离根因。默认 group 计算本身正确（gx·gy ≤ max_t 已保证）。
  - 建议: dispatch 入口校验 `threads` 各维 > 0、`group` 各维 > 0 且乘积 ≤ `kernel.max_threads_per_group`，违规抛 `MetalComputeError`（带 kernel 名与数值）。
  - 置信度: 高

- [AUD-1407] [Suggestion] 模块级重复 `import numpy as np`，`read_texture` 内再次函数级导入
  - 位置: src/shared/gpu/metal/context.py:31-33、477
  - 证据:
    ```python
    import numpy as np

    import numpy as np
    ```
    以及 read_texture 内：
    ```python
    def read_texture(self, ...):
        ...
        import numpy as np
    ```
  - 影响: 无功能影响；代码卫生问题，暗示合并编辑未清理。
  - 建议: 删除重复导入与函数内导入。
  - 置信度: 高

- [AUD-1408] [Suggestion] `write_buffer` 对 Shared 存储 buffer 也调用 `didModifyRange_`（探测的是 selector 存在性而非 storage mode）
  - 位置: src/shared/gpu/metal/context.py:251-259
  - 证据:
    ```python
    # 独显 macOS 的 Managed buffer 需要显式标记 CPU 修改；Shared
    # buffer 没有该 selector 的必要，但 PyObjC/Fake 都允许安全探测。
    did_modify = getattr(buffer, "didModifyRange_", None)
    if callable(did_modify):
        did_modify((int(offset), int(payload.nbytes)))
    ```
  - 影响: `didModifyRange:` 按文档仅对 Managed 资源有意义；对 Shared/Private buffer 调用属于未定义行为，Metal 验证层开启时可能产生诊断噪音。当前实测路径（Apple Silicon Shared）通常无害，但注释「安全探测」描述的是 PyObjC selector 可达性，不是 Metal 语义合法性。
  - 建议: 按 buffer 的 storage mode 分支（`buffer.storageMode() == MTLStorageModeManaged` 才调用），或记录该调用依赖驱动容忍行为。
  - 置信度: 低（未实测验证层是否报错；语义上属误用，实际危害不确定）

### 通过项
- 命令队列创建失败显式抛错（`_queue is None` 检查，line 88-89）。
- kernel 编译 / 缓存 / get_cached_kernel 全程持 `self._lock`，kernel 对象编译后只读，跨线程共享安全。
- MSL 编译失败与 pipeline 创建失败都抛 `MetalComputeError` 并附带 `localizedDescription`，不静默降级（line 150-172）。
- `write_buffer` 有负 offset 与越界校验（line 242-250），异常包装保留 `from exc` 因果链。
- 纹理存储模式按 `hasUnifiedMemory()` 正确分派 Shared/Managed（create_texture_from_numpy / create_texture_2d），与 docstring 的独显 Managed 语义一致。
- `read_texture` 的 blit `synchronizeResource` 使用独立 command buffer（不复用 `_current_cb`），保证排在未提交 compute 编码之后，Managed 显存同步语义正确。
- `submit` 的 completion handler 能力探测失败显式抛错而非静默忽略（line 407-412）；错误检查区分 wait/非 wait 语义（status >= 4 = Completed/Error）。
- `availability_reason` 的 `except Exception` 仅用于进程级缓存的能力探测并有注释理由，非吞领域错误。
- `_measure_gpu_time_ms` 拒绝用墙钟替代 GPU 时间，访问器能力探测 + 异常收窄（TypeError/ValueError/RuntimeError），返回 None 语义清晰。

---

## src/shared/gpu/metal/kernels.py (行数: 355)
### 发现

- [AUD-1409] [Suggestion] `make_blur_params` / `make_erode_dilate_params` 无 isfinite 与数值域校验（与 kernels_color 的 build_* 标准不一致）
  - 位置: src/shared/gpu/metal/kernels.py:274-291
  - 证据:
    ```python
    def make_blur_params(w: int, h: int, radius: float, edge_policy: int = 0) -> np.ndarray:
        params = np.zeros(1, dtype=BLUR_PARAMS_DTYPE)
        params["width"] = w
        params["height"] = h
        params["edge_policy"] = edge_policy
        params["radius"] = radius
        return params
    ```
    对比 kernels_color.py:223-226：
    ```python
    scalars = [intensity, size, shadows, midtones, highlights, seed, ...]
    if not all(np.isfinite(v) for v in scalars):
        raise ValueError(f"film_grain: 参数含非有限值 {scalars!r}")
    ```
  - 影响: `radius=NaN/inf` 传入时 kernel 内 `max(p.radius, 0.1)` 的 NaN 行为在 MSL 中未定义（实测通常传播），sigma→NaN、权重→NaN、整幅输出 NaN 且编译/执行表面正常。kernels_color 已按 .clinerules:11 建立 isfinite 防线，本文件缺失同类防线。
  - 建议: 与 `build_film_grain_params` 对齐，增加 `np.isfinite(radius)` 与 w/h>0 校验。
  - 置信度: 中（NaN 是否真能从 UI/调用链到达未验证；防线不一致是代码事实）

- [AUD-1410] [Suggestion] `make_erode_dilate_params` 的 operation 非法字符串静默映射为 dilate
  - 位置: src/shared/gpu/metal/kernels.py:288
  - 证据:
    ```python
    params["operation"] = 0 if operation == "erode" else 1
    ```
  - 影响: `operation="dilaet"` 之类的拼写错误不会报错，腐蚀意图被静默执行为膨胀，视觉结果反向且难以排查。
  - 建议: 显式校验 `operation in ("erode", "dilate")`，违规抛 ValueError。
  - 置信度: 高

- [AUD-1411] [Suggestion] `gaussian_blur_reference` 权重以 float64 计算后 cast float32，与 kernel 的 fp32 `exp` 存在 ulp 级偏差
  - 位置: src/shared/gpu/metal/kernels.py:309-310
  - 证据:
    ```python
    weights = np.exp(-(offsets.astype(np.float64) ** 2) / (2.0 * sigma * sigma))
    weights = weights.astype(np.float32)
    ```
    对比 MSL（kernels.py:54-56）：
    ```c
    static inline float gaussian_weight(float x, float sigma) {
        return exp(-(x * x) / (2.0 * sigma * sigma));
    }
    ```
  - 影响: 若该参考被用于逐位对拍（docstring 称「GLSL/MSL 高斯语义的 numpy 参考」，测试门禁类型未知），f64→f32 舍入与 kernel 的 fp32 求值会有 ulp 差，可能造成对拍误报或掩盖真实偏差；若按容差对拍则无影响。
  - 建议: 参考 实现 内全程以 `_F32(...)` 精确模拟 fp32 求值（同 kernels_color.py 的参考做法），或在 docstring 标注仅适用于容差对拍。
  - 置信度: 中（测试使用方式未在本批范围内确认）

### 通过项
- blur 边缘策略 clamp/wrap 越界安全：wrap 的负数回绕正确（`m < 0 ? m + width : m`），clamp 落在首/末 texel；与 GLSL 声明的语义一致（含「策略 2 等效 clamp」的说明）。
- 所有 MSL 循环边界均为常量（-64..64、-50..50）；浮点字面量全部带 `.0`。
- half4 输入 → float4 累加 → half4 写回，精度语义与 GLSL rgba16f 管线一致；归一下限 `max(weight_sum, 0.00001)` 与 docstring 声明的 1e-5 一致。
- `blur_h_shared`/`blur_v_shared` 的 threadgroup barrier 无分歧：越界守卫在 barrier 之后，tile 协作加载先行且 coord 经 clamp/wrap 保证读合法数据；64-lane 假定与 bridge 传入的 group=(64,1,1)/(1,64,1) 匹配（已核对 metal_compute_bridge.py:153-158）。
- erode/dilate 的 `erode_dilate_shared` 与 `erode_dilate` 同实现的理由（threadgroup 存储上限 93KB）在 docstring 中论证成立，同名同语义保证 GL/Metal 画质一致。
- `erode_dilate_reference` 跳过 (0,0) 与 kernel 以中心像素作初值等价。

---

## src/shared/gpu/metal/kernels_color.py (行数: 584)
### 发现

- [AUD-1412] [Suggestion] `build_film_grain_params` 只校验 finite，不校验 tex_size > 0 与 visible_rect 数值域；tex_w=0 时 kernel 产生全图 NaN
  - 位置: src/shared/gpu/metal/kernels_color.py:223-227（校验）、143-144（kernel 消费）
  - 证据:
    ```python
    scalars = [intensity, size, shadows, midtones, highlights, seed,
               *visible_rect, *tex_size]
    if not all(np.isfinite(v) for v in scalars):
        raise ValueError(f"film_grain: 参数含非有限值 {scalars!r}")
    ```
    而 kernel 中：
    ```c
    float uvx = (float(gid.x) + 0.5) / p.tex_w;
    float uvy = (float(gid.y) + 0.5) / p.tex_h;
    ```
  - 影响: `tex_size=(0.0, 0.0)` 或负值是有限值、能通过校验；除以 0/负值得 ±inf，经 hash 链（fract 级联对 inf/NaN 敏感）传播为整幅 NaN 颗粒，且参考实现 `film_grain_reference` 同样会输出 NaN（行为一致但双方都错）。
  - 建议: 校验 `tex_size[0] > 0 and tex_size[1] > 0` 以及 `rect_w/rect_h` 非负。
  - 置信度: 中（默认值 (2048, 2048) 下无触发；防御缺口是代码事实）

### 通过项
- HDR 正确性核心断言成立：film_grain 与 soft_clip 的 MSL 全程无 `clamp(0,1)`，输出显式保留负值/超 1 值（`dst.write(float4(c.r + off_r, ...), gid)`、`dst.write(float4(res, c4.a), gid)`），与 docstring「全程不 clamp、测试有回归断言」一致。
- MSL 浮点字面量全部带小数点；`#pragma clang fp contract(off)` 与哈希链显式展开（dot/mix/smoothstep 展开）与 numpy 参考逐项同序，位级一致性论证完整。
- 有限性校验存在（对比 kernels.py 的缺失）；dtype 布局与 MSL struct 逐字段对齐（13×4=52、8×4=32 字节，无对齐空洞）。
- `film_grain` 的 `intensity <= 0.0` 直通分支与参考实现一致；`smooth_step` 自定义命名与 metal_stdlib 的 `smoothstep` 不冲突。
- `soft_clip_reference` 用 `np.errstate` 包裹未选中分支的发散运算并注释原因，不属于吞错误。
- 参考实现的 f32 单精度模拟（`_F32` 包裹除法避免双舍入）与 kernel 求值序对齐，注释标明「不得单侧修改」。

---

## src/shared/gpu/metal/kernels_dct.py (行数: 309)
### 发现

- [AUD-1413] [Suggestion] 全部 DCT 变体的 y 维度（gid.y / grp.y）无越界守卫，x 维度守卫不对称存在，依赖调用方派发精确 y 网格
  - 位置: src/shared/gpu/metal/kernels_dct.py:45（forward_dct）、134（forward_dct_tg）、169（forward_dct_prod）、215（forward_dct_tg4）、251（forward_dct_m8）
  - 证据（forward_dct 与 tg 代表）：
    ```c
    const uint cols = (uint)params[0];
    const int mid = params[1];
    if (gid.x >= cols) { return; }
    const uint bx = gid.x, by = gid.y;
    const size_t stride = (size_t)cols * 8u;
    ...
    const device ushort* row = src + (size_t)(by * 8u + (uint)y) * stride + ...
    ```
    ```c
    if (grp.x >= cols) { return; }   // 整组一致返回：barrier 无分歧
    const uint bx = grp.x, by = grp.y;
    ```
  - 影响: `dispatchThreads` 保证 gid 不超过调用方设置的 threads 网格，因此 threads=(cols·8, rows·8) 精确时安全；但 params 不含 rows，kernel 无法自校验 y 界。代码库中已存在「threads 向上取整到组倍数」的派发先例（metal_compute_bridge.py:154 `threads = (((w + 63) // 64) * 64, h, 1)`），若未来 DCT 宿主沿用该模式把 threads.y 取整到组倍数，y 越界线程会以 `by*stride` 越界读 src / 越界写 dst（GPU 段错误或静默损坏）。x 维度守卫的存在说明作者考虑过网格多派发，防护不对称。
  - 建议: params 增加 rows（或在 kernel 内通过 buffer 长度不可行时的最小改动：宿主侧强校验 threads.y == rows*8），kernel 补 `if (gid.y >= rows)` / `grp.y >= rows` 守卫（tg 系变体需保持整组一致以护 barrier）。
  - 置信度: 中（当前宿主未接入、测试派发精确；风险为防御缺口 + 代码库先例模式）

### 通过项
- 全部循环边界为常量 8 次（`#pragma unroll 8`），无动态循环边界问题。
- 整数域位精确论证成立：level shift 后 |x'| < 2^12，单项乘积 ≤ 2^19，|F| ≤ 2^25 < int32 上限；tg4 的 4 累加链拆分依据整数加法结合律，位不变。
- tg/prod/m8 的 `short` threadgroup 暂存域安全（|x'| < 2^12）；barrier 前守卫整组一致返回（`grp.x >= cols` 在 barrier 之前），无 barrier 分歧。
- `forward_dct_m8` 尾组双重守卫正确：加载侧样本级 `sx < cols * 8u`（越界填 0），store 侧块级 `bx0 + b < cols`；越界块的暂存值只进对应 accs[b]，不会污染有效块。
- `forward_dct_rows` 的 ushort4 向量载入 16B 对齐论证成立（stride=cols·8 样本=cols·16B，MTLBuffer 基址 16B 对齐）。
- 纯整数路径：无浮点字面量、无 HDR clamp 风险；与 transform.c 规范 scalar 的位等价契约由三方对拍测试钉死（docstring 声明）。

---

## src/shared/gpu/metal/kernels_dec.py (行数: 550)
### 发现

- [AUD-1414] [Major] slice 版 `idx > 63` 发出未声明的 status=93u，与 docstring 状态集 {0,4,9,11} 及 segment 版同位置的 9u 不一致
  - 位置: src/shared/gpu/metal/kernels_dec.py:267（对照 524）
  - 证据:
    ```c
    const uint idx = pos + run_sym;
    if (idx > 63u) { status[sid] = 93u; return; }
    ```
    而 docstring（kernels_dec.py:21）声明：
    ```
    status 每 slice 一枚：0=OK；4=TRUNCATED；9=MALFORMED；11=CHECKSUM。
    ```
    segment 版同语义位置（kernels_dec.py:524）：
    ```c
    const uint idx = pos + run_sym;
    if (idx > 63u) { status[sid] = 9u; return; }
    ```
  - 影响: MALFORMED 数据触发 zigzag 索引越界时，slice 版写入 93u。宿主（尚未接入，当前仅测试消费）按文档的 {0,4,9,11} 分派状态时 93u 落入未知分支——可能被当作非零「有错」侥幸兜住，也可能在严格枚举校验/重试/conceal 分派中走错路径（例如宿主把 !=11 的非零都当 TRUNCATED 重试）。两个 kernel 版本对同一错误类别发不同码，属于明确的契约缺陷。
  - 建议: 将 93u 改为 9u（MALFORMED），并考虑为状态码建常量表防歓名字魔法数漂移。
  - 置信度: 高（代码与 docstring 的不一致是直接读到的事实）

- [AUD-1415] [Suggestion] `st.size = me[4] - prefix`（及 segment 的 `me[4] - 4u`）无符号下溢无防护，畸形 smeta 导致 GPU 越界读
  - 位置: src/shared/gpu/metal/kernels_dec.py:222、485
  - 证据:
    ```c
    rans_state st;
    st.buf = payload + prefix;
    st.size = me[4] - prefix;
    ```
    以及 segment 版：
    ```c
    v8_rans_state st;
    st.buf = stream + 4u;
    st.size = me[4] - 4u;
    ```
  - 影响: `me[4]`（payload_size/stream_len）小于 prefix（最大可到 1+5·29+64+5·28=350）时 uint 减法回绕成 ~4e9，重整化循环以 `st.pos < st.size` 为界从 payload+prefix 处向后越界读 GPU 内存（进程崩溃或解码出垃圾后由域检查部分拦截）。docstring 仅声明宿主「已验 ≥ 4 字节」，未覆盖 prefix。正常宿主生成的 smeta 自洽（编码器把 prefix 写进了 payload），触发前提是宿主 bug 或容器元数据损坏。
  - 建议: kernel 入口加 `if (me[4] < prefix + 4u) { status[sid] = 9u; return; }`（segment 版对应 `me[4] < 4u`）。
  - 置信度: 中（下溢路径是代码事实；能否触发取决于尚未编写的宿主打包逻辑）

- [AUD-1416] [Suggestion] 模块 docstring 内 smeta 布局自相矛盾：12×u32 与 13×u32 并存（实际为 13）
  - 位置: src/shared/gpu/metal/kernels_dec.py:22-25
  - 证据:
    ```
    布局：buffer0 bytes(payload 连接) / 1 smeta(12×u32: C 9 项 + plane_cols,
    dc0, off0, pairs0) / 2 tabs(709×u16) / ... threads=(n,1,1)。smeta 每 slice
    13×u32：C 导出 9 项 + plane_cols, dc0, off0, pairs0（宿主几何预建）。
    ```
  - 影响: 9+4=13，第一处「12×u32」是笔误；kernel 实际按 `smeta + sid * 13u` 取行（line 186）。未来宿主作者按第一句打包 12 项会使所有 slice 的 smeta 错位一位，解码全错。
  - 建议: 修正第一处为 13×u32。
  - 置信度: 高

### 通过项
- rANS 状态机域约束完整保留：`st.x < TC_L` 初始检查、`m > TC_M_MAX_DC/AC` 拒绝、`idx > 63` 拒绝、DC 域 ±2^25 检查、`st.pos != st.size` 尾字节精确耗尽检查。
- conceal 契约正确：CRC 失败或解码错误路径下，退出前已把本 slice/段的 CSR 段（off、dc）清零，GPU 重建 mid 语义成立。
- `tc_sym_search` 二分边界正确（lo<hi、上取中、答案由 Σfreq=4096 保证 freq>0），与 C LUT 等价性论证成立。
- `for (;;)` 符号环有两个保证退出的路径（run_sym==63 break / 重整化耗尽返回 4u），无死循环风险。
- DC 预测的 round-half-up 符号拆分用 int64 中间量，无溢出；slice 版 has_top=0 时 top 引用不发生。
- slice 与 segment 两版的表布局（709 u16 行内偏移 145/150/64/65/140）逐项一致。

---

## src/shared/gpu/metal/kernels_enc.py (行数: 92)
### 发现

- [AUD-1417] [Suggestion] `quant_token` 的 grp.y 无越界守卫（与 AUD-1413 同类），依赖调用方 threads.y 精确等于带高×8
  - 位置: src/shared/gpu/metal/kernels_enc.py:52-53
  - 证据:
    ```c
    const uint cols = (uint)params[0];
    const uint y0 = (uint)params[1];
    if (grp.x >= cols) { return; }   /* 整组一致返回：barrier 无分歧 */
    const size_t gidx = (size_t)(y0 + grp.y) * (size_t)cols + (size_t)grp.x;
    ```
  - 影响: 与 AUD-1413 相同的防御不对称：x 维度守卫存在、y 维度（gidx 的行分量）无守卫。若宿主把 threads.y 向上取整到组倍数（代码库已有此类先例），越界组会越界读 `Fp[gidx*64 + nat]` 并越界写 `dc_raw[gidx]`/`npair[gidx]`/`pairs` 槽。
  - 建议: params 增加 rows 并补 `grp.y >= rows` 的整组一致守卫（守卫需在 barrier 之前）。
  - 置信度: 中（宿主未接入；防御缺口与代码库派发先例是事实）

### 通过项
- 量化 clamp 到 ±2^25 是 C 侧 `tc_quant_block_ctx` 契约的复刻（DCT 合法域约束），不是 HDR 无理由 clamp；与位等价要求一致。
- fastdiv 魔法数运算全为 64 位整数：`(ulong)ne * fdm1` 乘积 < 2^57 无溢出，移位/掩码括号明确，与 C fastdiv 同式（docstring 声明由三方对拍钉死）。
- zigzag 环的 pos 上限受 `idx = pos + run_sym ≤ 63` 约束（宿主守卫同 dec），`out[cnt++]` 计数上限 63 与 pairs 槽 gidx×63 预留匹配，npair u16 无截断风险。
- barrier 前整组一致返回，threadgroup `qn[64]` 暂存无 bank conflict 问题；lane0 扫描与 64-lane 量化的相 A/相 B 划分正确。
- `lm` 编码（`q*2` / `-q*2-1`）与 run 打包 `(ulong)lm << 6 | run` 的位域约束（lm < 2^26、run ≤ 62）成立。

---

## src/shared/gpu/metal/kernels_histogram.py (行数: 159)
### 发现

- [AUD-1418] [Suggestion] `histogram_cdf` 每线程重复线性扫描全通道 256 桶两次（total + cum），1024 线程共约 52 万次冗余读
  - 位置: src/shared/gpu/metal/kernels_histogram.py:75-79
  - 证据:
    ```c
    uint32_t channel_total = 0u;
    for (uint i = 0u; i < 256u; i++) { channel_total += hist[ch * 256u + i]; }
    if (channel_total == 0u) { return; }
    uint32_t cum = 0u;
    for (uint i = 0u; i <= bin; i++) { cum += hist[ch * 256u + i]; }
    ```
  - 影响: 4 通道 × 256 bin 的每线程扫描使总读放大 ~256×；单次 kernel 绝对耗时小（亚毫秒级），但 Normalize 直方图链路每次参数调整都会重跑，属可消除的常数开销。
  - 建议: 网格改为每通道单线程块内归约（或每通道 1 线程做 prefix sum 写 cdf，再单独归约百分位），将复杂度从 O(bins²/ch) 降到 O(bins)。
  - 置信度: 高（冗余是代码事实；量级评估为小）

### 通过项
- 装箱 `clamp(c, 0, 1) * 255` 后截断转 uint 是与 GLSL `_HISTOGRAM_COMPUTE` 一致的有意行为（docstring 明示 HDR>1 截到 255、负值截到 0），不是无理由 HDR clamp；numpy 参考用相同截断语义（`(bins * 255.0).astype(np.uint32)`）。
- `total_count` 对全部像素无条件 +1 与 GLSL 版一致（channel_mask 排除通道不计各自直方图但计入总数）。
- atomic 全部使用 relaxed，对计数/最小值归约语义充分；各线程写各 (ch, bin) 槽无写竞争。
- 百分位高桶的 `255 - bin` 编码规避 atomic_max 哨兵问题，解码逻辑（`decode_percentile_bins`）与哨兵 → 端点默认的映射一致；`PERCENTILE_BIN_SENTINEL` 常量单一真相。
- PERCENTILE_PARAMS_DTYPE（2×f4+u4=12B）与 MSL PercentileParams 布局一致，无空洞。
- `channel_mask` 排除通道跳过 CDF 写入，与 `cdf_reference` 的全零行语义对齐。

---

## 总体结论

本批整体质量高：MSL 源码的浮点字面量规范、常量循环边界、HDR 不 clamp 契约（color 内核）、整数位精确论证（DCT/enc/dec）均有明确的 docstring 论证与测试门禁引用。无 Critical 问题。

3 个 Major 集中在两点：(1) `context.py` 的异常/内存管理边界——dispatch 异常丢弃整条已编码命令链且无信号、PyObjC 对象在工作线程缺少 autoreleasepool；(2) `kernels_dec.py` 的 status=93u 违反自身声明的状态契约。三者都不改变正常路径的正确性，但分别构成「部分计算静默丢失」「长会话慢性内存增长」「宿主接入后错误分派错路」的明确风险。

值得注意的结构性观察：DCT/enc/dec 内核目前只有测试消费方（宿主桥未接入），其「x 维守卫、y 维无守卫」的防御不对称在宿主按代码库既有「threads 向上取整」模式接入时会成为越界读写的实际诱因（AUD-1413/1417），建议在宿主接入前统一补齐。
