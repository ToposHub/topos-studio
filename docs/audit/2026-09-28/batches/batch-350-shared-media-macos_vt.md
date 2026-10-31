# batch-350 审查报告 [P2] — src/shared/media/macos_vt//

- 任务书: docs/audit/2026-09-28/assignments/batch-350.md
- 编号区间: AUD-35000–AUD-35099（实际使用 AUD-35001–AUD-35012）
- 文件数: 7（1,971 行）
- 汇总: Critical 0 / Major 3 / Suggestion 9
- 审查方式: 全文件逐行 Read；跨模块契约（GpuFrameRelease 幂等性、VtSession.probe_hardware_support 自清理、MetalSubmissionScheduler.submit 返回 None 语义、GpuEncodeSurfaceDescriptor.__post_init__ 校验、prewarm 调用点）已另行 Read/grep 验证。

---

## src/shared/media/macos_vt/__init__.py (行数: 7)

### 发现

无发现（纯 re-export）。

### 通过项

- 通过（无逻辑）：仅导出 `MacOSZeroCopyCapability`、`get_zero_copy_capability`、`is_zero_copy_available`（capability）与 `MacOSVtPlanarSource`、`MacOSVtZeroCopySource`（source，已确认文件存在且顶层无 PyObjC eager import，包导入不会在非 macOS 上炸）。

---

## src/shared/media/macos_vt/capability.py (行数: 621)

### 发现

- [AUD-35001] [Suggestion] `get_zero_copy_capability` 持 `_capability_lock` 运行最长 30s 的 probe 子进程，并发查询被串行阻塞
  - 位置: src/shared/media/macos_vt/capability.py:435、362-385
  - 证据:
    ```python
    with _capability_lock:            # 435
        if _cached_capability is not None:
            return _cached_capability
    ...
    proc = subprocess.run(            # 362
        [sys.executable, "-m", "src.shared.media.macos_vt.vt_zero_copy_probe", ...],
        timeout=timeout,              # 默认 30（_probe_int_env PROBE_TIMEOUT）
    )
    ```
  - 影响: auto 模式首次探测期间，任何线程调用 `is_zero_copy_available()`（factory.py:1036、edit_gpu_decode_manager.py:2511 均在播放路径）会在锁上阻塞至 probe 完成（一般约 1s，异常机器可达 30s 超时）。已有 `prewarm_zero_copy_capability` 缓解（app 层确有调用，含 blocking=True 与非阻塞两种），且串行化本身是防重复 probe 的有意设计，故降为 Suggestion；但 prewarm 未完成时的首次播放查询仍会同步等待。
  - 建议: 可接受现状；若要进一步优化，提供"probe 进行中快速返回 False + 完成后失效"的快路径，或将 probe 结果以 future 形式发布而非持锁等待。
  - 置信度: 高

- [AUD-35002] [Suggestion] `except (ImportError, Exception)` 冗余且过宽，吞掉领域实现缺陷
  - 位置: src/shared/media/macos_vt/capability.py:472、485
  - 证据:
    ```python
    except (ImportError, Exception) as e:
        cap.reason = f"vt_module_import_error: {e}"
        cap.available = False
    ```
  - 影响: `ImportError` 在该元组中冗余（`Exception` 已覆盖）；`is_vt_available()`/`is_vt_decode_implemented()` 内的 AttributeError 等编程错误也会被静默折叠为能力不可用原因，掩盖真实 bug（违反 .clinerules“不得宽泛 except 掩盖领域错误”）。fail-closed 意图本身正确。
  - 建议: 收窄为 `except (ImportError, AttributeError)` 或至少去掉冗余元组并 logger.debug 记录 traceback。
  - 置信度: 高

- [AUD-35003] [Suggestion] `clear_capability_cache` 与 `invalidate_capability_cache` 完全重复
  - 位置: src/shared/media/macos_vt/capability.py:262-266、617-621
  - 证据:
    ```python
    def clear_capability_cache() -> None:
        """清除能力缓存，使下次查询重新读取配置。"""
        global _cached_capability
        with _capability_lock:
            _cached_capability = None
    ```
    （617-621 行 `invalidate_capability_cache` 逐字符相同，仅 docstring 措辞不同。）
  - 影响: 双入口同一行为，维护时易只改其一造成语义漂移。
  - 建议: 保留一个，另一个做别名（`invalidate_capability_cache = clear_capability_cache`）。
  - 置信度: 高

- [AUD-35004] [Suggestion] `supported_codecs: set` / `supported_pixel_formats: set` 裸 set 注解
  - 位置: src/shared/media/macos_vt/capability.py:53-54
  - 证据:
    ```python
    supported_codecs: set = field(default_factory=lambda: {'h264', 'hevc', 'h265', 'prores'})
    supported_pixel_formats: set = field(default_factory=lambda: {'nv12', 'p010'})
    ```
  - 影响: 类型检查器无法推断元素类型（应为 `set[str]`）。
  - 建议: 改为 `set[str]`。
  - 置信度: 高

### 通过项

- 持久化缓存正确性：schema_version/TTL/cache_key（平台+机型+Python+probe 文件指纹）校验完整；负结果仅缓存 color-verification 失败（与 load 173 行判定、store 201 行判定互为镜像，一致）。
- 原子写：tmp 文件 + `os.replace`，无半写状态。
- probe 判定 `_probe_result_is_zero_copy_proven` 严格（zero_copy、IOSurface、plane 绑定、无 CPU download/texture write 逐项验证）。
- 子进程隔离 probe：崩溃/超时转为干净 unavailable；超时后 finally 清理临时 JSON。
- `_find_default_probe_file` 指定文件缺失时返回 None 而非回退 fixture（fail-closed）。
- prewarm 线程去重正确（`_prewarm_lock` + is_alive 检查，与 `_capability_lock` 无嵌套死锁风险）。
- 非 macOS / force_cpu_upload / mode=off 均显式 fail-closed 并缓存。

---

## src/shared/media/macos_vt/cv_metal_bridge.py (行数: 672)

### 发现

- [AUD-35005] [Major] `CVMetalVideoBridge.initialize` 无锁，跨线程首次导入竞态双建 CVMetalTextureCache，泄漏一个 CF 对象
  - 位置: src/shared/media/macos_vt/cv_metal_bridge.py:401-403、424-430、384-386
  - 证据:
    ```python
    # 384-386（类 docstring）: 线程模型：Metal 无 context 概念，本桥可跨线程使用
    def initialize(self) -> bool:
        """创建 CVMetalTextureCache。失败显式诊断（不静默）。"""
        if self._initialized:
            return True
    ```
    ```python
    # 424-430
    rc = int(_cv.CVMetalTextureCacheCreate(None, None, dev_ptr, None, ctypes.byref(cache)))
    if rc != 0 or not cache.value:
        ...
    self._cache = cache
    self._initialized = True
    ```
  - 影响: 类明确声明可跨线程使用，但 `initialize` 无实例锁：两个线程同时通过 `_initialized` 检查会各建一个 `CVMetalTextureCacheRef`，后赋值覆盖前者且永不 `CFRelease` → 每次竞态泄漏一个持有 Metal 资源的 CF 对象（GPU 资源生命周期维度）。另 `_bind_count`/`_fail_count`（563、594-596）无锁自增（仅统计，影响轻微）。
  - 建议: 加实例级 `threading.Lock` 做双重检查初始化；计数器改用 `itertools.count` 或并入同一锁。
  - 置信度: 高（机制确定；触发窗口仅首次初始化，频率低）

- [AUD-35006] [Major] `destroy()` 与并发 `import_pixel_buffer` 竞态：已过检的 `self._cache` 可变为 None 并以 NULL 传入 CoreVideo FFI
  - 位置: src/shared/media/macos_vt/cv_metal_bridge.py:456、510-513、577-583
  - 证据:
    ```python
    if not self._initialized and not self.initialize():   # 456，此后不再复查
    ...
    rc = int(_cv.CVMetalTextureCacheCreateTextureFromImage(
        None, self._cache, pb_ptr, None, mtl_fmt,          # 510-512
        plane_w, plane_h, plane_idx, ctypes.byref(out),
    ))
    ```
    ```python
    # 577-583
    def destroy(self) -> None:
        if self._cache is not None and _cf is not None:
            _cf.CFRelease(self._cache)
            self._cache = None
        self._initialized = False
    ```
  - 影响: 456 行检查与 510 行使用之间若 `destroy()`（经 `VideoToolboxGpuSurfaceProvider.close()`，gpu_encode_surface.py:153-154）并发执行，`self._cache` 为 None，ctypes 对 `c_void_p` 形参把 None 转成 NULL 传入 `CVMetalTextureCacheCreateTextureFromImage`——轻则 CVReturn 错误被记为 import 失败，重则 CoreVideo NULL 解引用崩溃。本模块在 `_pb_ptr`（169-174）中特意防“垃圾指针进 CV 函数”，此处属同类疏漏。
  - 建议: initialize/destroy/import 共用实例锁；import 在 FFI 调用前快照 `cache = self._cache`，为 None 时走 `_record_failure("destroyed")` 返回 None。
  - 置信度: 中（竞态需 close 与 acquire 并发；NULL 下崩溃还是错误返回取决于 CoreVideo 实现）

- [AUD-35007] [Major] `MetalVideoFrameRing` 按 frame_index 值匹配而非条目身份，重复 index 会跨提交提前置完成，导致 GPU 完成前提前释放
  - 位置: src/shared/media/macos_vt/cv_metal_bridge.py:288-304、313-325
  - 证据:
    ```python
    selected = (
        {int(index) for index in frame_indices}
        if frame_indices is not None
        else {int(e["frame_index"]) for e in self._entries}
    )
    for e in self._entries:
        if int(e["frame_index"]) in selected:
            e["submitted"] = True
    ...
    for e in self._entries:
        if e["submitted"] and (
            selected is None or int(e["frame_index"]) in selected
        ):
            e["completed"] = True
    ```
  - 影响: 若同一 `frame_index` 同时被 track 两次（回放/seek 循环重放序号 0..7，而旧条目仍在最多 8 深的 ring 内未 release），`on_gpu_complete(selected)` 会把两次提交的条目一并置 completed → 提交 A 的 `GpuFrameRelease` 在其 command buffer 完成前触发 → CVMetalTexture/CVPixelBuffer 被 CFRelease 时 GPU 仍可能读取（use-after-free / 画面损坏）。`GpuFrameRelease` 的幂等只防重复释放，不防早释放。若消费方在 seek 时总是 `release_all()`（362-370 行 docstring 提示该用法）则不触发——本模块内无法完全确认所有调用方行为。
  - 建议: `track` 返回不透明 token（条目身份），`mark_submitted`/`on_gpu_complete` 按 token 匹配而非 frame_index 值。
  - 置信度: 中（依赖重复 frame_index 与未 flush 的调用方时序）

- [AUD-35008] [Suggestion] 诊断/转换函数直接访问 `metal_ctx._metal`/`_queue` 私有成员；`"np.ndarray"` 字符串注解无模块级 np 导入
  - 位置: src/shared/media/macos_vt/cv_metal_bridge.py:610、649、658-660、667
  - 证据:
    ```python
    metal = metal_ctx._metal
    cb = metal_ctx._queue.commandBuffer()
    ...
    region = metal_ctx._metal.MTLRegionMake2D(0, 0, int(width), int(height))
    ```
    另 610/649 行签名使用 `params: "np.ndarray"`，而 numpy 仅在 653 行函数体内导入。
  - 影响: 与 `MetalComputeContext` 内部实现强耦合（重命名单测不可见）；类型检查器无法解析 `np`。非热路径（诊断/测试），故仅 Suggestion。
  - 建议: 在 `MetalComputeContext` 上提供 `read_texture`/`synchronize` 公共方法；`np` 导入移到 `TYPE_CHECKING` 块。
  - 置信度: 高

### 通过项

- CVReturn 判定纪律：所有 rc 显式 `== 0`/`!= 0` 判断，规避 -6661 真值坑（200-202、224-226、425、514 均正确）。
- 生命周期契约：CVPixelBuffer `CFRetain`（493）与失败路径 `_fail` 统一平衡（499-501）；cv texture +1 引用在 `_release_cb` 中先于 buffer 释放（553-557），顺序正确。
- `_fail` 闭包仅触发一次（每条失败路径立即 return），无双重 CFRelease。
- FourCC 校验（475-481）先于 retain，防止为不支持的 buffer 无谓 +1。
- MTLPixelFormat 从 Metal 模块权威解析，无手写魔数（48-64）。
- `read_texture_rgba16f` 的 bpr=8B/px、`<f2` 视图、Managed 存储显式 blit 同步均正确（646-672）；HDR RGBA16F 读回无 clamp。
- ring 的 `release_completed`/`release_all` 在锁外执行 release 回调，无死锁；`submit_pending` 对 scheduler.submit 抛异常与返回 None 均正确回滚 `_unmark_submitted`（339-351），与 MetalSubmissionScheduler.submit 的 Optional 返回契约一致。
- `_pb_ptr` 对 None/裸 int/PyObjC 三形态防御正确，杜绝垃圾地址进 FFI。
- 外部纹理 plane 尺寸按 x_div/y_div 计算，仅支持 4:2:0 双平面，不伪装 422/10bit 直通。

---

## src/shared/media/macos_vt/external_texture.py (行数: 36)

### 发现

无发现（纯数据描述符）。

### 通过项

- 通过（无逻辑）：frozen dataclass 非所有权描述符，`glo`/`width`/`height` 属性正确；不含任何 GL 调用或资源管理，无泄漏面。

---

## src/shared/media/macos_vt/gpu_encode_surface.py (行数: 154)

### 发现

- [AUD-35009] [Suggestion] `acquire` 内 `import Quartz` 未纳入受控失败面，非 macOS 路径裸抛 ImportError 而非模块错误类型
  - 位置: src/shared/media/macos_vt/gpu_encode_surface.py:88-89、24-25
  - 证据:
    ```python
    def acquire(self, *, width: int, ... ) -> GpuEncodeSurface:
        """Create one IOSurface-backed surface without CPU pixel staging."""
        import Quartz
    ```
    模块定义了 `class VideoToolboxSurfaceError(RuntimeError)`，其余失败路径（113-116、124-126）均以该类型抛出。
  - 影响: 调用方按 `VideoToolboxSurfaceError` 做 CPU 回落处理时，非 macOS/PyObjC 缺失场景会漏接 ImportError，异常处理面不一致（实际触发概率低——上游能力门禁通常已挡）。
  - 建议: 将 `import Quartz` 包进 try/except ImportError 转 `VideoToolboxSurfaceError("pyobjc_not_available")`，或移到 `__init__` 提前失败。
  - 置信度: 高

### 通过项

- 输入验证完善：宽高必须为正偶数（93-94）、格式白名单 nv12/p010（95-96）、CVPixelBufferCreate 状态码显式判断（113-116）。
- 失败闭环：import 失败带 `last_fallback_reason` 诊断（124-126）；`submit` 无 submitter 时 fail-closed（144-149），与模块 docstring 承诺一致。
- 生命周期：import 成功获得的 `release` 正确移交 `GpuEncodeSurface`；描述符字段（bit_depth 8/10、device_identity 非空、planes 非空）与 `GpuEncodeSurfaceDescriptor.__post_init__` 校验兼容，构造不会中途抛异常造成 retain 泄漏（已核对构造顺序）。
- P010 正确映射 `420YpCbCr10BiPlanarVideoRange` + bit_depth=10；ring_size 默认 8 有 T-P0-31 依据注释。
- 类型注解完整。

---

## src/shared/media/macos_vt/gpu_export_bridge.py (行数: 143)

### 发现

- [AUD-35010] [Suggestion] `close()` 的 `_closed` 防重入标志无锁；`samples: Any` 弱类型
  - 位置: src/shared/media/macos_vt/gpu_export_bridge.py:124-127、111-116
  - 证据:
    ```python
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
    ```
    ```python
    def write_audio_samples(
        self,
        samples: Any,
        sample_rate: int = 48000,
    ) -> None:
    ```
  - 影响: 两个线程同时 close 均见 False、均执行关闭链；下游（VtSession.destroy 有 `_destroyed` 守卫、bridge.destroy 幂等）大概率兜底，故仅 Suggestion，但防线不在本层。`samples: Any` 丢失 ndarray 形状约束。
  - 建议: `_closed` 检查与置位放入一把实例锁；`samples` 注解为 `np.ndarray`（TYPE_CHECKING 导入）。
  - 置信度: 高

### 通过项

- 构造失败路径干净：session 半建/失败时先 `session.close()` 再 `muxer.close()` 后 re-raise（80-84），无半初始化残留。
- close 顺序正确且用嵌套 finally 保证三段（session→provider→muxer）全关（128-134）；上下文管理器成对。
- 会话/表面/muxer 像素格式一致性（T-P0-31 注释，63-74），修复过 P010 喂 8-bit 会话的历史缺陷。
- `fps = max(1, int(fps))` 防御；`packet_count` 走 int 强转。
- provider 暴露方式（property 而非复制构造）保持单一所有权。

---

## src/shared/media/macos_vt/hardware_capability.py (行数: 338)

### 发现

- [AUD-35011] [Suggestion] 持久化 verdict 的读-改-写在 `_hw_lock` 之外，并发探测丢失更新；`_machine_fingerprint` 全局写入亦无锁
  - 位置: src/shared/media/macos_vt/hardware_capability.py:260-277、79-88
  - 证据:
    ```python
    entries = _load_hw_cache()          # 锁外读文件
    ...
    entries[cache_key_json] = verdict
    _store_hw_cache(entries)            # 锁外整体覆写
    ```
    另 `_machine_fingerprint()` 在 79-88 行无锁读写模块级 `_hw_machine_fingerprint`，而 `clear_hardware_capability_cache`（327-329）持锁将其置 None。
  - 影响: 两个线程同时为新 codec 探测 → 后写者整体覆盖文件，先写者的 verdict 丢失 → 下次同格式播放重复探测（多付一次 demux + VT 会话创建，~ms 级）；fingerprint 竞态仅为重复计算，均非正确性破坏，故 Suggestion。
  - 建议: 把 load→probe→store 的缓存文件更新整体纳入 `_hw_lock`（或文件级合并写）；`_machine_fingerprint` 用锁或 `functools.lru_cache`。
  - 置信度: 高（竞态机制确定，后果轻）

- [AUD-35012] [Suggestion] 跨模块导入私有函数 `_infer_bit_depth`
  - 位置: src/shared/media/macos_vt/hardware_capability.py:311-312
  - 证据:
    ```python
    from src.shared.media.ffmpeg_source import _infer_bit_depth
    bit_depth = _infer_bit_depth(str(pix_fmt or ''))
    ```
  - 影响: 依赖他模块下划线私有 API，重构 ffmpeg_source 时此处静默破坏（ImportError 只在运行到该策略分支时暴露）。
  - 建议: 在 ffmpeg_source 将其提为公共函数，或在本模块内联一份 pix_fmt→位深映射。
  - 置信度: 高

### 通过项

- probe 资源清理正确：`probe_vt_hardware_decode_for_file` 的 demuxer 在 finally 关闭；`VtSession.probe_hardware_support` 自带 finally destroy（已核对 vt_session.py:806-852），且 VtSession 有 `__del__` 兜底，无会话泄漏。
- -12906 硬解池瞬时耗尽场景有单次重试（210-223），避免把瞬时失败固化为"软解"判定。
- 三值语义（True/False/None）清晰且 fail-open 正确：probe 基础设施不可用时返回 None 不影响既有 VT 路径。
- 8-bit 快路径跳过探测（313-316），首播零探测成本。
- 持久化缓存：schema/TTL/机型指纹（CPU brand + mac_ver）校验完整，tmp+os.replace 原子写，PYTEST_CURRENT_TEST 下默认禁用（91-98），环境变量覆盖（force_vt/force_ffmpeg/TOPOS_VT_HW_PROBE）齐备。
- 播放策略仅作用于 H.264/H.265 且仅顺序播放（scrub 不走该策略），与模块 docstring 声明一致。

---

## 汇总统计

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 3 | AUD-35005, AUD-35006, AUD-35007 |
| Suggestion | 9 | AUD-35001–35004, AUD-35008–35012 |
