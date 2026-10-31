# batch-083 [P1] src/features/composite/render//

编号区间：AUD-8300–AUD-8399。文件 5 个，共 2,999 行。
汇总：Critical 0 / Major 8 / Suggestion 12。
交叉验证：`FramebufferManager._fbo_cache/_active_textures/ctx` 属性已核对存在（src/shared/gpu/gl_backend.py:76-79）；`allocate_texture` 在全库无定义（仅 gl_utils.py 调用）；`insert_fence` 生产调用方为 composite_renderer.py:1784（渲染线程）；`begin_video_frame/acquire_video_slot/force_cleanup/can_render_full_frame/suggest_tile_size/get_available_budget_mb` 在 src/ 下无生产调用方（video slot 与 tiling 估算 API 为休眠代码）。

## src/features/composite/render/disk_cache_store.py (行数: 479)

### 发现

- [AUD-8300] [Major] store() 中 os.path.getsize 无异常保护，并发 clear() 可使其抛未捕获 OSError
  - 位置: src/features/composite/render/disk_cache_store.py:258
  - 证据:
    ```python
    manifest = {
        "key": key,
        ...
        "bytes": os.path.getsize(payload_path),
    }
    ```
    而 clear() 会删除已落盘的载荷：
    ```python
    for name in names:
        if (name.endswith((".npz", ".toos", ".json", ".tmp"))
                or ".writing" in name):
            self._safe_unlink(os.path.join(self._root, name))
    ```
  - 影响: payload 经 os.replace 落盘（第 213/234 行）与 getsize（第 258 行）之间若另一线程执行 clear()（用户在偏好页清空缓存时导出 worker 正在写入），getsize 抛 FileNotFoundError（OSError 子类），第 268 行的 try 只捕获 OSError 于 manifest 写入段、第 258 行在 try 之外，异常直接逃逸进 evaluator 调用链。类注释（第 225-227 行）自证并行写是预期场景。
  - 建议: 将 manifest 构造纳入 try/except OSError，或 getsize 失败时回退 topos_manifest["bytes"]/估算值并按写失败路径清理。
  - 置信度: 中

- [AUD-8301] [Suggestion] store() 的 json.dumps 未捕获 TypeError，metadata 含不可序列化值时异常逃逸
  - 位置: src/features/composite/render/disk_cache_store.py:268-277
  - 证据:
    ```python
    try:
        meta_bytes = json.dumps(manifest, ensure_ascii=False,
                                indent=1).encode("utf-8")
        if not self._atomic_write(self._meta_path(key), meta_bytes):
    ...
    except OSError as exc:
        logger.warning("DiskCacheStore manifest write failed: %s", exc)
        return False
    ```
  - 影响: manifest 混入 metadata 的 node_id/frame 等外部值；若为 numpy 标量等不可序列化对象，TypeError 不在捕获范围。对照 `_touch()`（第 424 行）显式捕获 `(OSError, TypeError, ValueError)`，说明作者已意识到该风险但 store() 未对齐。
  - 建议: except 元组补 TypeError/ValueError，或在入 manifest 前对 metadata 值做 str() 归一。
  - 置信度: 中

- [AUD-8302] [Suggestion] _touch 每次缓存命中全量重写 manifest 且强制 fsync，热路径 I/O 放大
  - 位置: src/features/composite/render/disk_cache_store.py:415-425
  - 证据:
    ```python
    def _touch(self, key: str) -> None:
        meta = self.load_metadata(key)
        if not meta:
            return
        meta["last_used"] = time.time()
        try:
            meta_bytes = json.dumps(meta, ensure_ascii=False,
                                    indent=1).encode("utf-8")
            self._atomic_write(self._meta_path(key), meta_bytes)
    ```
    `_atomic_write` 内含 `os.fsync(handle.fileno())`（第 436 行）。
  - 影响: load()（第 324 行）每次命中都触发 _touch；视频回放/预览时每帧、每命中节点一次 fsync + 临时文件 + rename。last_used 仅用于 LRU，崩溃后稍失真无碍，不需要持久化强一致；4K 实时基准下这是可观的每帧延迟。
  - 建议: _touch 去掉 fsync（写临时文件后直接 replace），或改为内存节流（如 N 秒/退出时批量回写 last_used）。
  - 置信度: 中

- [AUD-8303] [Suggestion] 同 key 切换载荷格式时旧格式文件不清理，磁盘泄漏直至驱逐
  - 位置: src/features/composite/render/disk_cache_store.py:224-244
  - 证据:
    ```python
    if payload_path is None:
        tmp_path = (payload_path := self._payload_path(key)) + (
            f".writing.{os.getpid()}.{threading.get_ident()}")
        try:
            _write_payload(tmp_path, rgba)
            os.replace(tmp_path, payload_path)
    ```
    npz 分支从不 `self._safe_unlink(self._topos_payload_path(key))`，TOPOS 分支（第 213 行）也从不清理旧 `.npz`。
  - 影响: 跨进程格式配置变化（TOPOS_NODE_CACHE_FORMAT / 偏好键）或 TOPOS 编码失败回退后，同 key 可能同时存在 `.toos` 与 `.npz`；读取按 manifest format 选择正确文件，但孤儿文件占用磁盘，且 total_bytes 只计 manifest 口径、实际占用偏大。
  - 建议: store() 成功写 manifest 前 unlink 兄弟格式文件。
  - 置信度: 中

- [AUD-8304] [Suggestion] 共享单例与统计计数器无锁，注释自证的并行写场景下统计丢失/双实例
  - 位置: src/features/composite/render/disk_cache_store.py:279, 458-473
  - 证据:
    ```python
    self._writes += 1
    self.evict_if_needed()
    ```
    ```python
    def get_disk_cache_store() -> DiskCacheStore:
        global _STORE_INSTANCE
        if _STORE_INSTANCE is None:
            ...
            _STORE_INSTANCE = DiskCacheStore(root, capacity)
        return _STORE_INSTANCE
    ```
  - 影响: 第 225-227 行注释明确"parallel export workers (or GUI + CLI) may write the same key concurrently"。`+=` 与单例懒初始化在无锁下可交错：统计丢更新（仅诊断失真）、首并发调用可建出两个实例（根目录相同，原子替换保证数据安全，仅浪费句柄）。正确性无碍，属低风险并发瑕疵。
  - 建议: 模块级 threading.Lock 保护 get_disk_cache_store；计数器用 itertools.count 或接受误差并注释。
  - 置信度: 高（行为确定，影响轻微）

### 通过项
- 原子写契约正确：临时文件 + os.replace，has() 要求 manifest+payload 双存在，半写条目对读者不可见（异常与取消路径）
- f16 存储前 clip ±65504 有充分理由与注释（防 inf 毒化下游），不属于无理由 clamp（HDR 正确性）
- load() 对损坏条目自愈删除，不会反复命中坏载荷
- 禁用态（capacity<=0 / TOPOS_DISABLE_DISK_CACHE）完全不读写
- 输入验证：store() 校验 rgba.ndim==3 且 shape[2]>=4
- 无 UI/OpenGL 交互，纯磁盘 + numpy
- 类型注解完整

## src/features/composite/render/frame_trace.py (行数: 163)

### 发现

- [AUD-8305] [Suggestion] to_dict 硬编码丢弃 gpu_time_ms 字段值，未来赋值将被静默忽略
  - 位置: src/features/composite/render/frame_trace.py:102, 134
  - 证据:
    ```python
    gpu_time_ms: Optional[float] = None
    ...
    "gpu_time_ms": None,
    "gpu_time_unavailable": self.gpu_time_unavailable,
    ```
  - 影响: dataclass 定义了 gpu_time_ms 字段，但序列化路径固定输出 None。当前后端无 GPU 时间戳（文档说明成立），但若后续接线为字段赋值，emit_trace 输出仍为 None 且无告警，形成静默失真。
  - 建议: 输出 `_round(self.gpu_time_ms)` 并保留 gpu_time_unavailable 语义；或删掉字段只留常量。
  - 置信度: 高（不一致确定，当前无实际错误）

### 通过项
- sink 引用在锁内拷贝、锁外调用；sink 异常捕获且不影响渲染（emit_trace never raises 契约成立）
- GPU 时间未测量显式标记 gpu_time_unavailable，不伪造 0；传输计量 None/0 语义一致
- new_trace_id 基于 monotonic_ns + itertools.count，进程内唯一且并发安全
- 类型注解完整

## src/features/composite/render/gl_utils.py (行数: 292)

注：文件头标注 DORMANT（2026-08-19 审计，暂无生产入口），以下缺陷均为接线后生效的明确风险。

### 发现

- [AUD-8306] [Major] upload_numpy_texture 调用不存在的 resource_pool.allocate_texture，池化路径永不命中且兜底纹理泄漏
  - 位置: src/features/composite/render/gl_utils.py:250-254, 262-268
  - 证据:
    ```python
    if resource_pool is not None and hasattr(resource_pool, 'allocate_texture'):
        tex = resource_pool.allocate_texture(w, h, owner="ai_node",
                                              components=components, dtype='f4')
        tex.write(upload_data.tobytes())
        return tex.glo
    ...
    # Create standalone texture (no pooling)
    tex = ctx.texture((w, h), components, data=upload_data.tobytes(), dtype='f4')
    ```
  - 影响: 已核对 GPUResourcePool（gpu_resource_pool.py）只定义 acquire_texture/acquire_temp_texture 等，全库无 allocate_texture 定义——hasattr 恒 False，传入 resource_pool 时静默落到 fb_manager 或直建路径。直建路径只返回 glo（int），调用方无释放句柄，AI 推理输出纹理每次泄漏一块 VRAM（4K f4 约 127MB/次）。
  - 建议: 改调 resource_pool.acquire_texture(...) 并保留对象引用供释放；或返回 (glo, tex)。
  - 置信度: 高

- [AUD-8307] [Major] download_texture 声称保存原 FBO 却从未跟踪，finally 无条件恢复到 ctx.screen 破坏调用方绑定
  - 位置: src/features/composite/render/gl_utils.py:104-105, 195-199
  - 证据:
    ```python
    original_viewport = ctx.viewport
    original_fbo = None  # Will track current FBO binding
    ...
    try:
        # Restore to default framebuffer (screen)
        ctx.screen.use()
    except Exception:
        pass
    ```
  - 影响: original_fbo 声明后无任何赋值/使用；真实调用点若在离屏 FBO 渲染中途（composite 管线常态），下载完成后当前绑定被强制切回屏幕帧缓冲，后续未显式 use() 的绘制写错目标——渲染错误。viewport 有恢复，FBO 没有。
  - 建议: 用 glGetIntegerv(GL_FRAMEBUFFER_BINDING)（或 ModernGL 等价）保存并恢复；或要求调用方自行恢复并在 docstring 声明。
  - 置信度: 高（缺陷确定；实际影响取决于接线时的调用上下文）

- [AUD-8308] [Major] download/upload 声称 BGR/BGRA 但无任何通道交换，单侧使用即红蓝反转
  - 位置: src/features/composite/render/gl_utils.py:87, 160, 216
  - 证据:
    ```python
    Returns:
        BGR float32 numpy array (h, w, 3 or 4) in [0, 1] range.
    ...
    # Return BGR (drop alpha if not needed)
    return arr[:, :, :3]  # BGR
    ```
    着色器为纯直通（`frag_color = texture(u_texture, v_uv);`），readback 得到的是 RGBA；上传侧同样 `tex.write(upload_data.tobytes())` 原样写入，无 `[:, :, ::-1]`。
  - 影响: download↔upload 成对使用可自洽往返，但与管线其余 RGBA 约定不符：只取 download 结果送 AI 推理（或只经 upload 写回管线纹理）时红蓝通道互换，产生错误着色结果。docstring 与实现二选一必有一错。
  - 建议: 明确契约为 RGBA 并修正 docstring/注释；若确需 BGR（如 OpenCV 系模型），在边界处显式交换并双向对称。
  - 置信度: 高（无交换是事实；下游影响取决于接线）

- [AUD-8309] [Major] 失败路径宽泛 except 静默返回全零数组，GL 错误被黑图掩盖
  - 位置: src/features/composite/render/gl_utils.py:201-204, 270-273
  - 证据:
    ```python
    except Exception as e:
        logger.warning(f"download_texture failed: {e}")
        # Return zero array as fallback
        return np.zeros((h, w, 3), dtype=np.float32)
    ```
  - 影响: docstring 只承诺 mock 路径返回零数组；生产路径任何异常（上下文丢失、尺寸不符、着色器编译失败）也返回形状合法的黑图，仅一条 warning。AI 推理输入被静默替换为黑帧，产出错误结果且无错误传播——违反"不得宽泛 except 掩盖领域错误"铁律。
  - 建议: 生产路径失败时抛出（或返回 None 并要求调用方处理），零数组仅保留给显式 mock 分支。
  - 置信度: 中

### 通过项
- 临时资源 finally 释放完整（vao/vbo/prog/readback_fbo/readback_tex 逐个 try-release）
- GLSL 字面量均显式 .0（0.0/1.0），无隐式类型问题
- float32 直通无 clamp(0,1)（HDR 正确性）
- _is_mock_context 使测试路径不触真实 GL；文件头声明 GL 操作限渲染线程
- 类型注解完整

## src/features/composite/render/gpu_resource_pool.py (行数: 1869)

### 发现

- [AUD-8310] [Major] 小池 LRU 驱逐把 pinned 纹理弹出 _pool_lru 后不恢复，纹理滞留 _pooled_textures 成不可驱逐孤儿
  - 位置: src/features/composite/render/gpu_resource_pool.py:1260-1265, 1321-1324
  - 证据:
    ```python
    if len(self._pool_lru) <= 3:
        while bytes_freed < bytes_needed and self._pool_lru and iterations < max_iterations:
            iterations += 1
            glo, info = self._pool_lru.popitem(last=False)
            if self._try_evict_texture(glo, info):
                bytes_freed += info.bytes
    ```
    ```python
    def _try_evict_texture(self, glo: int, info: TextureInfo) -> bool:
        if glo in self._pinned_texture_ids:
            return False
    ```
  - 影响: pinned 条目先被 popitem 移出 `_pool_lru`，`_try_evict_texture` 因 pinned 返回 False 后无人把它放回；该纹理仍留在 `_pooled_textures[key]` 列表里，但 cost 驱逐（`_find_highest_cost_texture` 只扫 `_pool_lru`）与统计（pooled_memory_bytes 只算 `_pool_lru`）都看不见它——内存滞留且口径漏计，直到 force_cleanup/evict_all_pooled 才被回收。触发需要 pin_texture 作用于已归还池中的 glo（公共 API 允许）且池 ≤3 项。对照 cost 路径（第 1305-1306 行 continue 不弹出）处理是对的，简单路径漏了对齐。
  - 建议: `_try_evict_texture` 返回 False 且原因为 pinned 时将 info 写回 `_pool_lru[glo]`；或 popitem 前先查 pinned。
  - 置信度: 中

- [AUD-8311] [Major] acquire_video_slot 按 render_mode 取 dtype，release/clear 硬编码 'f2'，key 不匹配导致 EXPORT 槽位无法释放
  - 位置: src/features/composite/render/gpu_resource_pool.py:801-819, 325-328
  - 证据:
    ```python
    return self._video_slot_pool.acquire_slot(width, height, components,
                                               'f2', slot_id, render_mode)
    ...
    def release_video_slot(self, width, height, components=4, slot_id=0):
        if self._video_slot_pool:
            self._video_slot_pool.release_slot(width, height, components, 'f2', slot_id)
    ```
    acquire_slot 内 `if render_mode is not None: dtype = get_dtype_for_mode(render_mode)`（EXPORT→'f4'），key 含 dtype。
  - 影响: 以 render_mode=EXPORT 获取的槽位 dtype='f4'，随后 release_video_slot/clear_video_slot 以 'f2' 构造 key，`_acquired_this_frame.discard` 与 `_slots.pop` 全部落空，f4 纹理永久滞留槽位（VRAM 泄漏）且 get_memory_bytes 继续计费。当前全库无生产调用方（休眠代码），属接线即触发的明确风险。
  - 建议: 记录 slot_id→实际 key 的映射，或 release/clear 同样接受 render_mode 参数推导 dtype。
  - 置信度: 高（不匹配确定；触发依赖未来接线）

- [AUD-8312] [Major] VideoSlotPool 槽位只增不减：end_frame 不回收未用槽，无数量/尺寸上限
  - 位置: src/features/composite/render/gpu_resource_pool.py:300-302, 338-348
  - 证据:
    ```python
    def end_frame(self):
        """End frame processing. Slots remain allocated for reuse."""
        self._in_frame = False
    ...
        tex = self._resource_pool.acquire_temp_texture(width, height, components, dtype)
        self._slots[key] = tex
    ```
  - 影响: 槽位以 (w,h,components,dtype,slot_id) 为键永久持有纹理（在资源池口径中恒为 active，驱逐不可回收）；分辨率/格式变化序列（如预览缩放、时序分辨率切换）会线性累积滞留纹理，只有显式 clear_all_video_slots/force_cleanup 能清。`_acquired_this_frame` 已收集了每帧使用情况却无消费者做淘汰。当前无生产调用方（休眠），接线后为 VRAM 增长风险。
  - 建议: end_frame 时对连续 N 帧未 acquire 的槽位执行 clear_slot；并设槽位数量/内存上限。
  - 置信度: 中

- [AUD-8313] [Suggestion] VRAMMonitor 经 ctypes 直调 GL 无线程防护，公共预算查询 API 可在非渲染线程触发 glGetIntegerv
  - 位置: src/features/composite/render/gpu_resource_pool.py:153-172, 1859-1869
  - 证据:
    ```python
    gl = ctypes.CDLL(None)
    result = ctypes.c_int(4)
    ...
    gl.glGetIntegerv(self.GL_GPU_MEMORY_INFO_CURRENT_AVAILABLE_VIDMEM_NVX,
                     ctypes.byref(result))
    ```
    `get_available_budget_mb`/`can_render_full_frame`/`suggest_tile_size`/`get_memory_budget_mb` 均调用 `_update_vram_budget()` → 上述 GL 查询。
  - 影响: GL 查询隐含"当前线程持有当前 GL 上下文"前提。渲染路径内调用安全，但这些带缓存/规划性质的公共 API 无线程契约标注，若被 UI/规划线程调用即违反"UI 线程不得操作 OpenGL"铁律（无上下文时 macOS 上结果未定义）。当前无生产调用方，属潜伏风险。
  - 建议: 在这些 API 的 docstring 标注"仅渲染线程"；或把 VRAM 采样收敛到 insert_fence 同一渲染线程节拍，查询结果供他线程只读。
  - 置信度: 中

- [AUD-8314] [Suggestion] _stats_format_distribution/_stats_by_usage 初始化后从未更新（死统计字段）
  - 位置: src/features/composite/render/gpu_resource_pool.py:552-553, 1523-1530
  - 证据:
    ```python
    self._stats_format_distribution: Dict[str, int] = {'f2': 0, 'f4': 0}
    self._stats_by_usage: Dict[TextureUsage, int] = {}
    ```
    get_stats() 实际用局部变量现算：
    ```python
    f2_count = sum(1 for info in self._active_textures.values() if info.dtype == 'f2')
    usage_counts = {}
    ```
  - 影响: 两个字段全类无写入点（除 force_cleanup 清零），纯死状态，误导维护者以为有累计统计。
  - 建议: 删除字段，或改为在 acquire/release 处真正维护。
  - 置信度: 高

- [AUD-8315] [Suggestion] VideoSlotPool._in_frame/_acquired_this_frame 记录后无任何消费者（死状态机）
  - 位置: src/features/composite/render/gpu_resource_pool.py:288-289, 295-302, 335, 360
  - 证据:
    ```python
    self._in_frame: bool = False
    self._acquired_this_frame: Set[Tuple] = set()
    ...
    def release_slot(self, ...):
        key = (width, height, components, dtype, slot_id)
        self._acquired_this_frame.discard(key)
    ```
  - 影响: begin/end_frame 与 acquire/release_slot 维护的帧内状态从未被读取（无淘汰、无断言），release_slot 实质是空操作，给调用方"已释放"的假象。
  - 建议: 要么实现基于该集合的帧末回收（配合 AUD-8312），要么删除状态并让 release_slot 直接 clear_slot。
  - 置信度: 高

- [AUD-8316] [Suggestion] 成本驱逐每轮全量扫描 _pool_lru，O(n²) 复杂度
  - 位置: src/features/composite/render/gpu_resource_pool.py:1267-1278, 1303-1319
  - 证据:
    ```python
    while bytes_freed < bytes_needed and self._pool_lru and iterations < max_iterations:
        iterations += 1
        best_glo, best_info, best_cost = self._find_highest_cost_texture()
        if self._try_evict_texture(best_glo, best_info):
    ...
        for idx, (glo, info) in enumerate(self._pool_lru.items()):
    ```
  - 影响: 每驱逐一个纹理都重新遍历整个 LRU 计算成本；大池（数百纹理）压力场景下驱逐风暴为平方级，发生在渲染线程的分配路径上（_check_budget），与 4K 实时目标相悖。
  - 建议: 一次排序（或堆）批量取出候选后再逐个尝试。
  - 置信度: 高（复杂度确定；触发依赖池规模）

- [AUD-8317] [Suggestion] _destroy_texture 直访 FramebufferManager 私有成员 _fbo_cache/_active_textures
  - 位置: src/features/composite/render/gpu_resource_pool.py:924-931
  - 证据:
    ```python
    fbo = self.fb_manager._fbo_cache.pop(tex, None)
    ...
    self.fb_manager._active_textures.discard(tex)
    ```
  - 影响: 跨对象私有成员访问（属性已核对存在，当前可运行），FramebufferManager 内部重构（改名/换结构）会静默破坏销毁路径；release_texture_by_id（第 988、999 行）同样模式。
  - 建议: 在 FramebufferManager 上提供 forget_texture(tex)/destroy_texture(tex) 公共方法，池侧改调。
  - 置信度: 高（耦合确定，属质量改进）

- [AUD-8318] [Suggestion] VRAMMonitor 存在死分支与无意义初值
  - 位置: src/features/composite/render/gpu_resource_pool.py:143-144, 155
  - 证据:
    ```python
    if self._vendor not in ("nvidia", "amd") and self._vram_available_kb > 0:
        return self._vram_available_kb
    ...
    result = ctypes.c_int(4)
    ```
  - 影响: `_supported` 仅在 vendor 为 nvidia/amd 时置 True，而本方法开头 `if not self._supported: return 0`，故第 143-144 行永不可达；`c_int(4)` 的初值 4 无语义（若 glGetIntegerv 未写入会误报 4KB 可用）。
  - 建议: 删除死分支；初值改 0 或 -1 并在查询后校验。
  - 置信度: 高

### 通过项
- deferred-release 模式正确：fence 未 signaled 时驱逐/强退走 _deferred_releases，回调只捕获渲染侧对象，全部 GL 销毁留在渲染线程（GPU 资源生命周期）
- glFenceSync/glClientWaitSync/glDeleteSync 的 ctypes restype/argtypes 修正到位（c_void_p），并有历史缺陷记录（线程安全/GL 正确性）
- 复用先于预算检查、预算口径用 resident（active+pooled+deferred），注释充分，避免稳态重复分配与静默超支（性能模式）
- force_cleanup 先 ctx.finish 再 drain(block=True)，外部纹理不被销毁、fence 全清理
- TEMP 纹理跳过 fence 的决策有完整文档说明
- acquire_output_texture 自动 pin、release 时统一 unpin，pin/unpin 对称
- 单渲染线程无锁契约在类 docstring 明确（insert_fence 生产调用方在渲染线程）
- 状态提交一致性/信号防递归维度不适用（无 UI 信号、无 undo/持久化职责）
- 类型注解基本完整（个别内部辅助方法缺参数注解，见 AUD-8317 一并改进即可）

## src/features/composite/render/metal_compute_bridge.py (行数: 196)

### 发现

- [AUD-8319] [Suggestion] _kernel 检查-编译-写入非原子，并发首用同名 kernel 重复编译并覆盖
  - 位置: src/features/composite/render/metal_compute_bridge.py:59-67
  - 证据:
    ```python
    def _kernel(self, name: str) -> MetalKernel:
        with self._lock:
            kernel = self._kernels.get(name)
        if kernel is not None:
            return kernel
        kernel = self._ctx.compile_kernel(name, METAL_COMPUTE_SOURCES[name])
        with self._lock:
            self._kernels[name] = kernel
        return kernel
    ```
  - 影响: render/export 线程并发首用同一 kernel 时（类 docstring 明确该并发契约），两个线程都通过检查、各自编译一次，后写覆盖先写；被覆盖的 MetalKernel 对象若持有 GPU 资源则滞留至 GC。功能结果仍正确，属重复开销与小泄漏。
  - 建议: 编译段也持锁（compile 在锁内，代价仅首用一次），或写回用 setdefault 并复用胜者。
  - 置信度: 高（竞态确定，后果轻微）

### 通过项
- 线程契约文档明确：与 ComputeShaderManager 一致，Metal 端由 context 内锁保护（线程安全）
- _read_texture 校验读回字节数（h*w*4*2）防格式/尺寸错配（输入验证）
- RGBA16F 全程直通、无 clamp(0,1)（HDR 正确性）
- 异常路径统一包装为 MetalComputeError 域错误并保留 cause 上抛，不吞错（异常路径）
- resource_pool.acquire_texture(width, height, components, dtype) 调用签名与池 legacy API（gpu_resource_pool.py:832-838）核对匹配
- GPU 中转设计（读回/上传成本）有计划依据与 A/B 判定说明（性能模式）
- 类型注解完整（gl_ctx/tex 用 Any 属跨库边界合理放宽）
