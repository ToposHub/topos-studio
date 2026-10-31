# batch-353 审查报告 [P2] — src/shared/media/macos_vt//

编号区间：AUD-35300–AUD-35399。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。

---

## src/shared/media/macos_vt/vt_session.py (行数: 1891)

### 发现

- [AUD-35301] [Major] VTDecompressionSession 原生对象销毁时只 Invalidate 从不 CFRelease，每个 session 泄漏一个原生解码器
  - 位置: src/shared/media/macos_vt/vt_session.py:1839-1842
  - 证据:
    ```python
    # Step 2: Invalidate ctypes session
    if self._ctypes_session is not None and self._ctypes_session.value:
        if _VT_LIB is not None:
            _VT_LIB.VTDecompressionSessionInvalidate(self._ctypes_session)
        self._ctypes_session = _ctypes.c_void_p()
    ```
  - 影响: `VTDecompressionSessionCreate` 按_Create 规则_返回 +1 引用的对象，`Invalidate` 只是终止会话，必须再 `CFRelease` 才释放。此处丢弃指针后原生 session（含硬件解码器实例与 IOSurface 池）永不释放。反复打开/关闭视频（每次 `MacOSVtZeroCopySource.open` 新建 VtSession、`close` 销毁）会持续泄漏硬件解码器槽位——同目录 hardware_capability.py:205-210 的注释已证实"硬解池可被存活会话暂时耗尽"这一现实后果，最终导致后续硬解创建失败、静默落软解。
  - 建议: 在 destroy() Step 2 中 Invalidate 后调用 `_CF_LIB.CFRelease(self._ctypes_session)`（文件内已为 sample buffer 使用过同一手法），再清空指针。
  - 置信度: 高

- [AUD-35302] [Major] create() 从不重置 `_destroyed`，destroy 后同一实例再 create 返回 True 但会话永久不可用
  - 位置: src/shared/media/macos_vt/vt_session.py:775-779, 706-715, 842-854
  - 证据:
    ```python
    # create() 开头只重置这些：
    self._width = width
    self._height = height
    self._codec = codec_lower
    self._codec_tag = codec_tag
    self._frame_index = 0
    # is_created 却先看 _destroyed：
    if self._destroyed:
        return False
    ```
  - 影响: `destroy()` 置 `_destroyed=True` 后，对同一实例再次 `create()` 会成功重建 ctypes session 并返回 True，但 `is_created` 恒为 False，`decode_packet`/`submit_packet` 全部静默失效。`probe_hardware_support()`（842-854 行）内部就执行 create→destroy，调用它之后该实例即被永久"毒化"。当前仓库内调用方（source.py、hardware_capability.py、probe）恰好都每次新建实例，故为潜伏缺陷；任何复用实例的新调用（如换文件重开）都会踩中。
  - 建议: `create()` 成功路径上重置 `self._destroyed = False`（并在失败清理时保持一致），或文档化"实例不可复用"并在 create 入口显式拒绝。
  - 置信度: 高（代码逻辑确定；现存调用方均未触发）

- [AUD-35303] [Major] 异步 API `submit_packet` 的 `pts` 参数被完全忽略，异步取帧路径丢失时间戳
  - 位置: src/shared/media/macos_vt/vt_session.py:1746-1773, 1726-1731
  - 证据:
    ```python
    def submit_packet(
        self,
        packet_data: bytes,
        pts: int | None = None,
        is_keyframe: bool = False,
    ) -> bool:
        ...
        try:
            return self._submit_packet_raw(packet_data)
    ```
  - 影响: `_submit_packet_raw` 只转发 packet_data；构建的 CMSampleBuffer 无 timing 信息，回调里 `pts.value` 取到的是无效 CMTime（通常 0）；同步路径 `decode_packet` 在 1730-1731 行用 demuxer PTS 覆盖补救，但异步路径（submit_packet + get_decoded_frame）返回的 `VtDecodedPixelBuffer.pts` 恒为无效值、`time_seconds` 恒 0.0，下游按 PTS 排帧/定位会错乱。当前 src/ 内无调用方，属公共 API 的契约缺陷（潜伏）。
  - 建议: 把 pts 存入 per-frame refCon 或经 context 传递，在 `_on_ctypes_decoded`/`get_decoded_frame` 侧回填；或在注释与签名中显式声明该限制。
  - 置信度: 高（代码路径确定）

- [AUD-35304] [Major] avcC 解析出的 NALU 长度字段宽度被丢弃，格式描述硬编码 4 字节长度前缀
  - 位置: src/shared/media/macos_vt/vt_session.py:1327-1329, 1195-1202, 1223-1230
  - 证据:
    ```python
    # lengthSizeMinusOne (NALU length size - 1)
    nalu_length_size = (extradata[idx] & 0x03) + 1
    idx += 1
    ...
    err, format_desc = CMVideoFormatDescriptionCreateFromH264ParameterSets(
        kCFAllocatorDefault,
        num_params,
        all_params,
        sizes,
        4,  # NAL unit length size
        None,
    )
    ```
  - 影响: `nalu_length_size` 解析后从未使用，两条 H.264 分支都向 VT 声明 4 字节长度前缀。若码流 avcC 声明 2 或 3 字节长度（extradata 首字节为 1 即进入该分支），VT 将按 4 字节切分 packet 数据，解码全部错位/失败且报 OSStatus 错误，表现为该文件硬解全灭。
  - 建议: 将解析到的 `nalu_length_size` 传入 `CMVideoFormatDescriptionCreateFromH264ParameterSets`（并确保提交的 packet 数据长度前缀与声明一致）。
  - 置信度: 中

- [AUD-35305] [Suggestion] 用 `hash(sample_buffer)` 提取 CF 原始指针，依赖 PyObjC `hash==指针` 的实现细节，与同文件 `objc.pyobjc_id()` 用法不一致
  - 位置: src/shared/media/macos_vt/vt_session.py:1679-1688
  - 证据:
    ```python
    # Get raw pointer from PyObjC CF object.
    # PyObjC CF types' hash() returns the raw CF object pointer value.
    sample_buf_ptr = hash(sample_buffer)

    # Extra CFRetain to keep the CF object alive through the decode.
    _CF_LIB.CFRetain(_ctypes.c_void_p(sample_buf_ptr))
    ```
  - 影响: 同文件 909/962/978 行对 format description、attributes、decoder spec 均使用 `objc.pyobjc_id()`。`hash()` 走 ObjC `-hash`/CFHash 语义，一旦 PyObjC 对该 CF 类型改为内容哈希，`CFRetain` 将作用于非指针整数——原生崩溃。当前实测可用，但属脆弱实现。
  - 建议: 统一改为 `objc.pyobjc_id(sample_buffer)`。
  - 置信度: 中

- [AUD-35306] [Suggestion] `put_pixel_buffer` 在 retain 成功后、入队前的异常路径不释放已 retain 的 buffer
  - 位置: src/shared/media/macos_vt/vt_session.py:427-469
  - 证据:
    ```python
    if isinstance(pixel_buffer, int):
        _CV_LIB.CVPixelBufferRetain(_ctypes.c_void_p(pixel_buffer))
    else:
        CVPixelBufferRetain(pixel_buffer)
    ...
    self._queue.put(decoded, timeout=0.1)
    ...
    except queue.Full:
        ... CVPixelBufferRelease ...
    except Exception as e:
        logger.error(f"VtDecodeOutputQueue: put error: {e}")
        return False
    ```
  - 影响: 只有 `queue.Full` 分支做了配对 release；retain 之后若 `CVPixelBufferGetIOSurface`、构造 dataclass 或 `put` 抛出其他异常，宽泛 `except Exception` 直接返回 False，retain 的 CVPixelBuffer/IOSurface 泄漏。
  - 建议: 宽泛 except 分支同样按 int/对象双路径 release，或将 retain 移到确认可入队之后。
  - 置信度: 高（路径确定；触发概率低）

- [AUD-35307] [Suggestion] `_parse_hvcc_extradata` 读取 numNalus 时缺少 `idx+1` 越界检查，靠上层宽泛 except 兜底
  - 位置: src/shared/media/macos_vt/vt_session.py:1426-1435
  - 证据:
    ```python
    for _ in range(num_arrays):
        if idx >= len(extradata):
            break
        # array_completeness, NAL_unit_type
        nalu_type = extradata[idx] & 0x3f
        idx += 1
        # numNalus
        num_nalus = (extradata[idx] << 8) | extradata[idx + 1]
        idx += 2
    ```
  - 影响: extradata 是外部文件数据（.clinerules 铁律"外部数据须验证"）。截断的 hvcC 在 `idx == len-1` 或 `idx == len` 时触发 IndexError，被 `_create_hevc_format_description` 的宽泛 `except Exception` 掩盖为笼统的创建失败日志，违反"不得宽泛 except 掩盖领域错误"。同函数后续 nalu 读取（1439-1440 行）反而有 `idx + 2 > len` 检查，此处不一致。
  - 建议: 读取 numNalus 前检查 `idx + 2 > len(extradata)` 则 break。
  - 置信度: 高

- [AUD-35308] [Suggestion] `_parse_annex_b_extradata` 计算当前 NAL 结束位置用的是当前 start code 长度而非下一个的
  - 位置: src/shared/media/macos_vt/vt_session.py:1495-1500
  - 证据:
    ```python
    for idx, (start, start_code_len) in enumerate(nalu_starts):
        # 找到下一个 NAL unit 的开始或数据结束
        if idx + 1 < len(nalu_starts):
            end = nalu_starts[idx + 1][0] - start_code_len
        else:
            end = len(extradata)
    ```
  - 影响: `nalu_starts[idx+1][0]` 是下一个 NAL 的数据起点，回退下一个 NAL 自己的 start code 长度（`nalu_starts[idx+1][1]`）才得到当前 NAL 的真实结尾；这里减的是当前 NAL 的长度。混合 3/4 字节 start code 的 extradata 中，SPS/PPS 尾部会多含或少含一个 0x00 字节，可能污染传给 `CMVideoFormatDescriptionCreateFrom*ParameterSets` 的参数集。
  - 建议: `end = nalu_starts[idx + 1][0] - nalu_starts[idx + 1][1]`。
  - 置信度: 高（逻辑错误确定；实际码流多为统一长度 start code，影响概率低）

- [AUD-35309] [Suggestion] `VtDecodeOutputQueue._lock` 创建后从未使用，跨线程计数器无保护
  - 位置: src/shared/media/macos_vt/vt_session.py:380-384, 453-454, 463
  - 证据:
    ```python
    def __init__(self, max_size: int = 8) -> None:
        self._queue: queue.Queue = queue.Queue(maxsize=max_size)
        self._lock = threading.Lock()
        self._dropped_count: int = 0
        self._total_count: int = 0
    ...
        self._queue.put(decoded, timeout=0.1)
        self._total_count += 1
    ```
  - 影响: `_dropped_count`/`_total_count` 在 VT 回调线程 `+=`（读-改-写，非原子），渲染/管理线程经 property 读取；`_lock` 是死代码，给人已加锁的错觉。计数仅用于诊断，误差概率极低。
  - 建议: 删除死锁对象，或用 `_lock` 保护两个计数器（queue.Full 分支同样在回调线程）。
  - 置信度: 高

- [AUD-35310] [Suggestion] 死代码与未使用导入：PyObjC NSObject 回调类从未实例化、`_callback` 属性从未赋值、`time/field/Callable/List/Dict/Any` 未使用
  - 位置: src/shared/media/macos_vt/vt_session.py:36-42, 529-605, 694
  - 证据:
    ```python
    import time                       # 未使用
    from typing import Optional, Callable, List, Dict, Any   # 后四个未使用
    ...
    if _PYOBJC_AVAILABLE:
        class VtDecompressionOutputCallback(NSObject):   # 全文件无实例化点
    ...
        self._callback: VtDecompressionOutputCallback | None = None   # 从未赋值/读取
    ```
  - 影响: `_create_session_impl` 明确"PyObjC 无法注册回调，必须 ctypes"（934-936 行注释），该 NSObject 类与 `_callback` 属性成为死代码；类级共享字段（`_output_queue` 等挂在类上）若被误用会跨实例串队列。未使用导入增加噪声。
  - 建议: 删除 NSObject 回调类（或注释保留原因）、`_callback` 属性及未使用导入；`configuration_version`（1321 行）同为读取后未用。
  - 置信度: 高

- [AUD-35311] [Suggestion] destroy() 中 Step 1 等待失败时 Step 4 仍在 try 块外无条件释放 pending refs，保活内存块可能被提前撤除
  - 位置: src/shared/media/macos_vt/vt_session.py:1832-1856
  - 证据:
    ```python
    try:
        # Step 1: Wait for all async frames to complete
        ...
    except Exception as e:
        logger.warning(f"VtSession: destroy error: {e}")

    # Step 4: Release pending decode refs.
    ...
    self._release_pending_decode_refs()
    ```
  - 影响: 若 Step 1 的 WaitForAsynchronousFrames 抛异常（如会话已被外部 invalidate），`_release_pending_decode_refs` 仍执行：CFRelease 我们额外保留的 sample buffer 引用并清空 `_pending_decode_bufs`。此时 VT 可能仍持有同一 sample buffer 在解码，其 CMBlockBuffer 引用的 `data_buf` 内存（kCFAllocatorNull、由本方配对保活）被提前释放 → VT 读取已释放内存（use-after-free 崩溃）。`flush()`（1810-1823 行）把 release 放在 try 内 wait 之后，写法正确，两处不一致。触发概率低。
  - 建议: 把 Step 4 移入 try 块、紧随等待成功之后（与 flush() 对齐）。
  - 置信度: 高（结构确定；触发概率低）

### 通过项
- 线程职责分离设计成立：ctypes 回调（_on_ctypes_decoded）只 retain + 入队，不触碰 OpenGL，符合"UI/回调线程不得操作 GL"约束
- ctypes 回调与 refCon 保活正确：`_ctypes_callback`、`_ctypes_callback_context_ref` 均存实例变量防 GC，且 destroy 时在原生 invalidate 之后才清引用
- sample buffer / 内存块配对保活与批量释放（_pending_decode_refs + _pending_decode_bufs，阈值 128 分摊 WaitForAsynchronousFrames）设计自洽，CMBlockBuffer 的 kCFAllocatorNull 所有权声明正确
- GLSL·HDR 正确性：文件无 GLSL 与 HDR 数据路径（仅像素格式名映射 nv12/p010），无违规 clamp
- 信号防递归：无 Qt 控件/信号，维度不适用
- 状态提交一致性：无 undo/redo/revision/持久化职责，维度不适用
- 平台与依赖守卫完备：非 macOS / PyObjC 缺失时全部 API 安全降级返回 False/None，模块导入期框架加载有 try 保护

---

## src/shared/media/macos_vt/vt_zero_copy_probe.py (行数: 826)

### 发现

- [AUD-35312] [Major] decode-only 阶段解码帧从不调用 release_decoded_frame，每帧泄漏一个 retain 的 CVPixelBuffer/IOSurface
  - 位置: src/shared/media/macos_vt/vt_zero_copy_probe.py:566, 713-722
  - 证据:
    ```python
    # Bridge stage: bind IOSurface to GL textures
    if stage in ("bridge-only", "full") and bridge is not None and pb.pixel_buffer is not None:
        ...
                    if ctx is not None:
                        ctx.finish()
                    gpu_release.release()
                    session.release_decoded_frame(pb)
                else:
                    session.release_decoded_frame(pb)

            decoded_count += 1
    ```
  - 影响: `decode_packet` 路径中 `put_pixel_buffer` 已对 CVPixelBuffer 执行 retain 并出队；release 调用只存在于 bridge 分支内部。`stage == "decode-only"` 时整块被跳过，`pb`（raw int 指针，GC 无法回收引用计数）逐帧泄漏，单次 128 帧 probe 泄漏 128 个 IOSurface；`session.destroy()` 也不会回收已出队的 buffer。
  - 建议: decode-only 分支（pb 非 None 且未走 bridge 时）同样调用 `session.release_decoded_frame(pb)`。
  - 置信度: 高

- [AUD-35313] [Major] VT session 创建失败的早退路径不清理 bridge 与 standalone GL context
  - 位置: src/shared/media/macos_vt/vt_zero_copy_probe.py:498-502
  - 证据:
    ```python
        if not created:
            result.fallback_reason = "vt_session_creation_failed"
            return result
    ```
  - 影响: bridge-only/full 阶段在 demuxer.open 之前已创建 standalone GL context 与 IOSurfaceGLBridge（445-459 行）。此早退仅靠 finally 关闭 demuxer，`bridge.destroy()` 与 `ctx.release()` 均未执行——GL 上下文与其 IOSurface 纹理环、bridge 原生资源整体泄漏。对比同函数其余失败分支（455-457、463-468、473-478 行）都做了配对清理。
  - 建议: 该分支返回前补 `bridge.destroy()`（若非 None）与 `ctx.release()`，或统一改用 finally 清理。
  - 置信度: 高

- [AUD-35314] [Major] `--output-json` 传纯文件名（无目录部分）时 `os.makedirs('')` 抛 FileNotFoundError
  - 位置: src/shared/media/macos_vt/vt_zero_copy_probe.py:812-816
  - 证据:
    ```python
    if args.output_json:
        import os
        os.makedirs(os.path.dirname(args.output_json), exist_ok=True)
        with open(args.output_json, 'w') as f:
            json.dump(result_dict, f, indent=2)
    ```
  - 影响: `python -m ... --output-json result.json` 这类合法用法中 `os.path.dirname` 返回 `''`，`os.makedirs('', exist_ok=True)` 直接抛 `FileNotFoundError`，probe 结果丢失且退出码不是预期的 1（未捕获异常）。该入口被 capability.py 以 `--output-json <tmp>.json` 子进程方式调用（tmp 路径含目录），当前未触发，但 CLI 契约破损。
  - 建议: `out_dir = os.path.dirname(args.output_json); if out_dir: os.makedirs(out_dir, exist_ok=True)`。
  - 置信度: 高

- [AUD-35315] [Major] 循环内 IOSurface 检测硬编码绝对导入 `src.shared...`，相对导入环境下被静默吞掉导致 probe 误判非零拷贝
  - 位置: src/shared/media/macos_vt/vt_zero_copy_probe.py:540-554
  - 证据:
    ```python
    try:
        import ctypes as _ctypes
        if isinstance(pb.pixel_buffer, int):
            from src.shared.media.macos_vt.vt_session import _CV_LIB
            io_ptr = _CV_LIB.CVPixelBufferGetIOSurface(_ctypes.c_void_p(pb.pixel_buffer))
            if io_ptr is not None and io_ptr != 0:
                iosurface_count += 1
        ...
    except Exception:
        pass
    ```
  - 影响: 函数头部依赖（417-428 行）采用"相对导入优先、绝对导入兜底"双路径，说明两种环境都可能；此处只写绝对导入。包以相对方式加载（sys.path 无 `src` 父目录）时 ImportError 被 `except Exception: pass` 吞掉，`iosurface_count` 恒 0 → `cvpixelbuffer_iosurface_backed=False`、`iosurface_present=False`、`zero_copy=False`，probe 给出错误的否定结论。同文件 `_pixel_buffer_center_rgb_reference`（139-141 行）就正确处理了双路径。
  - 建议: 与 139-141 行一致，相对导入失败时回退 `from src.shared.media.macos_vt.vt_session import _CV_LIB`。
  - 置信度: 中

- [AUD-35316] [Suggestion] 错误清理路径使用两个裸 `except:`，会吞掉 KeyboardInterrupt/SystemExit
  - 位置: src/shared/media/macos_vt/vt_zero_copy_probe.py:774-780
  - 证据:
    ```python
    # Cleanup on error
    if bridge is not None:
        try: bridge.destroy()
        except: pass
    if ctx is not None:
        try: ctx.release()
        except: pass
    ```
  - 影响: 违反 .clinerules"不得宽泛 except 掩盖领域错误"；裸 except 连 Ctrl-C 的 KeyboardInterrupt 都会被吞，且清理失败无任何日志，排障困难。
  - 建议: 改为 `except Exception:` 并 `logger.debug` 记录异常。
  - 置信度: 高

- [AUD-35317] [Suggestion] 两份 shader 中 `BT601/BT709/BT2020` mat3 常量为死代码，且 BT2020 数值是 BT.709 的复制品
  - 位置: src/shared/media/macos_vt/vt_zero_copy_probe.py:207-223, 271-285
  - 证据:
    ```glsl
    // BT.2020 coefficients (UHD)
    const mat3 BT2020 = mat3(
        1.164,  1.164,  1.164,
        0.0,   -0.164,  2.112,
        1.793, -0.533,  0.0  // simplified
    );
    ```
  - 影响: `main()` 只使用 if/else 链的显式系数（其中的 BT.2020 分支系数 1.4746/-0.1646/-0.5714/1.8814 才是正确值），三个 mat3 常量从未被引用；其中 BT2020 的值实为 BT.709 系数，极易误导后续维护者"启用"它造成 BT.2020 素材色偏。
  - 建议: 删除两组未使用的 mat3 常量，或改为与 if/else 一致的正确值。
  - 置信度: 高

- [AUD-35318] [Suggestion] 辅助函数缺少返回值/参数类型注解
  - 位置: src/shared/media/macos_vt/vt_zero_copy_probe.py:332-351, 354-373, 376-383
  - 证据:
    ```python
    def _create_standalone_gl_context():
        """创建 standalone ModernGL context（headless）"""
        ...
    def _create_yuv_shader(ctx, rectangle: bool = False):
        ...
    def _create_quad_vao(ctx, prog):
        ...
    ```
  - 影响: 与文件内 `run_probe(...) -> ZeroCopyProbeResult`、`_percentile(values: list[float], p: float) -> float` 的注解风格不一致，`ctx`/`prog` 类型只能靠文档推断（类型注解缺失维度）。
  - 建议: 补 `-> Optional["moderngl.Context"]`、`prog: moderngl.Program` 等注解（可用 TYPE_CHECKING 导入避免运行时依赖）。
  - 置信度: 高

- [AUD-35319] [Suggestion] `verify_color` 时每帧都执行 CPU 端 reference 读取（Lock/Unlock + 裸指针读），但结果只在第一帧渲染后使用一次
  - 位置: src/shared/media/macos_vt/vt_zero_copy_probe.py:556-563, 671
  - 证据:
    ```python
    expected_center_rgb = None
    if verify_color and pb.pixel_buffer is not None:
        expected_center_rgb = _pixel_buffer_center_rgb_reference(
            pb.pixel_buffer, width=info.width, height=info.height,
            pixel_format=pixel_format,
        )
    ...
    if verify_color and frames_rendered_count == 1:
    ```
  - 影响: `_pixel_buffer_center_rgb_reference` 对每帧做 CVPixelBufferLockBaseAddress/Unlock 与 ctypes 寻址读取，而 `frames_rendered_count == 1` 保证只消费第一帧的值；其余 119 次读取纯属浪费（probe 本身是量测工具，多余的每帧开销还会轻微污染 decode/total 计时口径）。
  - 建议: 仅在首帧（`decoded_count == 0` 或 frames_rendered_count == 0 时）计算 reference。
  - 置信度: 高

- [AUD-35320] [Suggestion] rectangle 分支未检查 `len(gpu_planes) > 1` 即访问 `gpu_planes[1]`
  - 位置: src/shared/media/macos_vt/vt_zero_copy_probe.py:649-661
  - 证据:
    ```python
    uv_mode = 0
    if len(gpu_planes) > 1 and gpu_planes[1].internal_format == 'GL_LUMINANCE_ALPHA':
        uv_mode = 1
    ...
    if use_rectangle:
        active_prog['y_size'].value = (
            float(gpu_planes[0].width), float(gpu_planes[0].height),
        )
        active_prog['uv_size'].value = (
            float(gpu_planes[1].width), float(gpu_planes[1].height),
        )
    ```
  - 影响: 650 行显式防御了单 plane 情形，659 行却在同一假设下无条件索引 `[1]`；若 bridge 返回仅 Y 平面的 rectangle 组合，IndexError 会冒泡到外层 except 把整次 probe 记为 `probe_error`。当前 NV12/P010 恒为双平面，触发概率低。
  - 建议: rectangle 分支前同样判断 `len(gpu_planes) > 1`，不满足则跳过 uv_size 设置并记录。
  - 置信度: 高（结构确定；触发概率低）

### 通过项
- GLSL 正确性：两份内联 shader 浮点字面量均显式带小数点，无循环（不涉循环常量边界约束），int uniform 与整数字面量比较类型匹配
- HDR/clamp 合规：shader 输出目标为 f1 8bit 验证纹理，`clamp(r, 0.0, 1.0)` 属显示域转换而非 HDR 内部数据截断，不违反铁律
- 线程安全：单线程 probe 流程，无共享可变状态竞争；GL 上下文由本函数独占创建与释放（bridge/ctx 各失败分支除 AUD-35313 外均配对清理）
- 零拷贝判定口径自洽：禁用 `frame.to_ndarray()`/`frame.transfer()`/`texture.write`（cpu_download_frames/texture_write_frames 恒 0），zero_copy 由 IOSurface+无 CPU 拷贝+绑定成功+颜色校验联合判定
- 基准统计实现正确：_percentile 有空列表与越界钳制，_stats_dict 空值返回全零结构，JSON 输出用 asdict 序列化一致
- 平台守卫：非 macOS / VT 不可用 / GL 上下文失败均安全早退并写 fallback_reason，不抛异常
- 退出码契约：probe 失败或非零拷贝时 `sys.exit(1)`，供 capability.py 子进程判定
