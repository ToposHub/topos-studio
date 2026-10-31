# batch-352 审查报告 [P2] src/shared/media/macos_vt//

编号区间：AUD-35200–AUD-35299
审查文件：2 个（topos_surface.py 203 行；vt_compression.py 935 行）
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失

---

## src/shared/media/macos_vt/topos_surface.py (行数: 203)

### 发现

- [AUD-35200] [Major] IOSurface 以 kIOSurfaceIsGlobal=True 创建，向全系统暴露共享表面
  - 位置: src/shared/media/macos_vt/topos_surface.py:113
  - 证据:
    ```python
    surface = IOSurface.IOSurfaceCreate({
        IOSurface.kIOSurfaceWidth: self.width,
        IOSurface.kIOSurfaceHeight: self.height,
        IOSurface.kIOSurfacePlaneInfo: plane_info,
        IOSurface.kIOSurfaceIsGlobal: True,
    })
    ```
  - 影响: 全局 IOSurface 可被任意进程凭 surface ID 映射读写，本池承载的是解码像素数据；该标志在同进程 Metal 导入场景并非必需，属安全面扩大且为 Apple 明示不建议的用法（全库仅此处使用）。
  - 建议: 去掉 kIOSurfaceIsGlobal（或置 False）；如确需跨进程共享再显式说明并收敛到受控通道。
  - 置信度: 中

- [AUD-35201] [Major] 以请求值假定实际行距，未回查 IOSurfaceGetBytesPerRowOfPlane
  - 位置: src/shared/media/macos_vt/topos_surface.py:98-107,144-151
  - 证据:
    ```python
    row_bytes = tuple(_aligned_row_bytes(w) for w in plane_widths)
    ...
    IOSurface.kIOSurfacePlaneBytesPerRow: stride,
    ...
    row_bytes = slot.row_bytes[plane]
    base = IOSurface.IOSurfaceGetBaseAddressOfPlane(slot.surface, plane)
    mapped = base.as_buffer(row_bytes * height)
    ...
    views[plane].stride = row_bytes // ctypes.sizeof(ctypes.c_uint16)
    ```
  - 影响: kIOSurfacePlaneBytesPerRow 是对框架的请求值，系统可能因对齐约束上调；若实际行距大于请求值，映射窗口偏小且交给解码器的 stride 错误，逐行读取偏移错位 → 画面斜移/撕裂（渲染错误）。当前 64 字节对齐大概率被原样接受，故为条件性缺陷。
  - 建议: 创建后用 IOSurfaceGetBytesPerRowOfPlane / GetPlaneCount 回读实际 stride 与平面参数，存入 _SurfaceSlot 供映射与 stride 使用。
  - 置信度: 中

- [AUD-35202] [Suggestion] release_surface 在 mapped 状态只记日志不回收槽位，防御路径下环形槽永久流失
  - 位置: src/shared/media/macos_vt/topos_surface.py:167-172
  - 证据:
    ```python
    def release_surface() -> None:
        with self._lock:
            if slot.mapped_buffers is not None:
                logger.error("ToposIOSurfacePool: release while mapped")
                return
            slot.leased = False
    ```
  - 影响: 经 DecodeSurfaceLease 公共状态机（release 仅允许 UNMAPPED/FENCED）此分支不可达，属防御代码；但若调用方绕过 lease 直接触发回调，该槽 leased 永久为 True 且 IOSurfaceLock 仍持有，池容量静默缩水直至 acquire 饿死（调用方 topos_source.py 已因此类泄漏踩坑并打了 C18 补丁）。
  - 建议: 防御分支至少强制 IOSurfaceUnlock + 回收槽位（或标记槽位 poison 并计入可观测指标），而非仅日志后放任泄漏。
  - 置信度: 高

- [AUD-35203] [Suggestion] close()/__exit__ 对在租槽位抛 RuntimeError，可能掩盖原始异常
  - 位置: src/shared/media/macos_vt/topos_surface.py:192-203
  - 证据:
    ```python
    def close(self) -> None:
        with self._lock:
            if any(slot.leased for slot in self._slots):
                raise RuntimeError("cannot close ToposIOSurfacePool with leased slots")
            self._slots.clear()

    def __exit__(self, *_exc: Any) -> None:
        self.close()
    ```
  - 影响: with 块内已有异常时 __exit__ 再抛 RuntimeError 会替换原异常，干扰排障；close 失败后 _slots 保留、对象残留。
  - 建议: __exit__ 内吞掉次级异常保留原始 traceback（或仅在显式 close() 上抛），并记录泄漏槽数量。
  - 置信度: 高

- [AUD-35204] [Suggestion] unmap_surface 先清 mapped_buffers 再检查解锁状态，失败时状态不一致且 lease 卡死
  - 位置: src/shared/media/macos_vt/topos_surface.py:159-165
  - 证据:
    ```python
    def unmap_surface() -> None:
        status = IOSurface.IOSurfaceUnlock(slot.surface, 0, None)
        slot.mapped_buffers = None
        if isinstance(status, tuple):
            status = status[0]
        if int(status) != 0:
            raise ToposSurfaceUnavailable(f"IOSurfaceUnlock failed: {status}")
    ```
  - 影响: 解锁失败时 mapped_buffers 已被清空，lease.unmap() 抛错后状态停留 MAPPED，公共状态机既不能 release 也不能 abort → 槽位与 CPU 锁永久泄漏。
  - 建议: 仅在 status==0 时清 mapped_buffers；失败路径提供可恢复语义（如允许重试 unmap 或显式强制回收）。
  - 置信度: 中

### 通过项
- 输入验证：width/height/pixel_format(0,1,2)/plane_count(3,4)/ring_size 全部前置校验，创建失败路径逐槽清理并抛领域异常
- GPU/原生资源生命周期：mapped memoryview 由 slot.mapped_buffers 持有保活至 unmap；map 中途异常路径补 IOSurfaceUnlock 后重抛，不泄漏 CPU 锁
- 线程安全：槽位预约、release、close 均持池锁；lease 侧自带 RLock 状态机（frame_path.py 已核）
- fence 契约：以恒非 None 的包装 closure 满足 memory_type=1 强制 fence 要求，未伪造同步原语（注释与 frame_path 校验一致）
- GLSL·HDR：不适用（无 GLSL）；uint16 平面直通解码，无 clamp(0,1) 路径
- 信号防递归 / 状态提交一致性：不适用（无 Qt 信号、无 undo/revision/持久化状态）
- 类型注解：模块级与函数级注解完整，闭包返回值明确

---

## src/shared/media/macos_vt/vt_compression.py (行数: 935)

### 发现

- [AUD-35205] [Critical] _native_pointer 以 hash() 兜底伪造原生指针送入 VideoToolbox，可致进程崩溃
  - 位置: src/shared/media/macos_vt/vt_compression.py:185-198
  - 证据:
    ```python
        elif isinstance(value, int):
            pointer = value
        else:
            try:
                import objc
                pointer = int(objc.pyobjc_id(value))
            except Exception:
                try:
                    pointer = int(hash(value))
                except Exception as exc:  # pragma: no cover - defensive ABI guard
                    raise VideoToolboxCompressionError(...)
    ```
  - 影响: macOS 上若环境无 PyObjC（仅 PyAV 可用即满足本类的最小依赖），native_handle 非 int/c_void_p 时落入 hash() 分支，把任意哈希整数当 CVPixelBufferRef 传给 VTCompressionSessionEncodeFrame → 原生空引用/野指针解引用 → 进程级崩溃（ctypes 回调无法拦截）。hash 还可能为负，经 c_void_p 回绕成非法地址。注释自称 "defensive ABI guard"，实际是伪造指针而非防御。
  - 建议: 删除 hash() 兜底；无法识别的 handle 直接抛 VideoToolboxCompressionError（"not_pointer_like"），必要时校验指针落在合理区间。
  - 置信度: 中（触发依赖非预期 handle 类型/缺 PyObjC 的环境；一旦触发必崩）

- [AUD-35206] [Major] submit/complete 在锁外调用原生 API，与 close() 的 invalidate/清空 session 存在 TOCTOU 竞态
  - 位置: src/shared/media/macos_vt/vt_compression.py:521-542,558-567,595-602
  - 证据:
    ```python
    with self._lock:
        if self._closed or not self._session.value:
            raise VideoToolboxCompressionError("videotoolbox_session_closed")
        ...
    status = _VT_LIB.VTCompressionSessionEncodeFrame(
        self._session, ...)          # 锁外，重读 self._session
    ...
    with self._lock:                  # close():
        session = self._session
        self._closed = True
        self._session = _ctypes.c_void_p()
    if session.value:
        _VT_LIB.VTCompressionSessionInvalidate(session)
    ```
  - 影响: submit/complete 的状态检查持锁但原生调用在锁外且按属性重取 self._session；并发 close() 可先清空（EncodeFrame(NULL) 崩溃）或先 invalidate（使用已失效 session，悬垂句柄崩溃）。当前调用链（gpu_export_bridge 由导出线程串行 submit/close）暂未并发，但 VT 回调本就多线程、类设计面向跨线程，取消导出一旦走别的线程即触发。
  - 建议: 在锁内捕获 session 局部引用并在"编码窗口"内持有同一把锁（或独立的 invalidate 闸门/引用计数），保证 invalidate 与 EncodeFrame/CompleteFrames 互斥。
  - 置信度: 高（竞态代码路径确定；实际可触发性取决于上层线程模型）

- [AUD-35207] [Major] Muxer 构造先 av.open/add_stream 后校验 pixel_format，失败路径泄漏容器与输出文件句柄
  - 位置: src/shared/media/macos_vt/vt_compression.py:759-775
  - 证据:
    ```python
    self._container = av.open(
        output_path,
        mode="w",
        format=container_format,
    )
    self._stream = self._container.add_stream(
        f"{normalized}_videotoolbox", rate=Fraction(max(1, int(fps)), 1),
    )
    ...
    normalized_pixel_format = str(pixel_format).strip().lower()
    if normalized_pixel_format not in {"nv12", "p010"}:
        raise VideoToolboxCompressionError(
            f"videotoolbox_packet_pixel_format_unsupported:{pixel_format}")
    ```
  - 影响: pixel_format 非法（或 add_stream 在该 PyAV 构建缺少 *_videotoolbox 包装器而抛错）时，容器已打开、输出文件已创建/截断，异常路径无 close → 文件描述符泄漏 + 磁盘残留空/坏输出文件；桥接层（gpu_export_bridge）因对象未构造完成也无法代为关闭。
  - 建议: 校验前置到 av.open 之前；或用 try/except 包裹构造段，失败时 self._container.close() 再重抛。
  - 置信度: 高

- [AUD-35208] [Major] 回调内丢包仅记 last_error，导出静默缺帧且 last_error 无任何消费者
  - 位置: src/shared/media/macos_vt/vt_compression.py:638-643
  - 证据:
    ```python
    if self._on_packet is not None:
        self._on_packet(packet)
    except Exception as exc:  # pragma: no cover - native callback guard
        with self._lock:
            self._last_error = f"videotoolbox_callback_extract_failed:{exc}"
        logger.exception("VideoToolbox packet callback failed")
    ```
  - 影响: on_packet（即 muxer.write_packet）或包提取失败时该包被静默丢弃，导出产物缺帧仍"成功"完成；grep 全库确认 session.last_error 在 gpu_export_bridge/timeline_export/export 均无消费点，错误状态到不了导出结果。属输出数据缺失风险。
  - 建议: 回调失败应计入失败计数并在 close()/complete() 后由上层强制失败导出（或把 last_error 升格为可查询的 failed 状态，桥接层断言）。
  - 置信度: 高（丢包机制确定；"必致缺帧"依赖失败真实发生）

- [AUD-35209] [Suggestion] _set_profile_level 创建的 CFString 从未 CFRelease，每会话原生堆泄漏
  - 位置: src/shared/media/macos_vt/vt_compression.py:471-481
  - 证据:
    ```python
    key_ref = _ctypes.c_void_p(cf.CFStringCreateWithCString(
        None, b"ProfileLevel", 0x08000100))
    value_ref = _ctypes.c_void_p(cf.CFStringCreateWithCString(
        None, level.encode("ascii"), 0x08000100))
    ...
    return int(_VT_LIB.VTSessionSetProperty(
        _ctypes.c_void_p(self._session.value), key_ref, value_ref,
    ))
    ```
  - 影响: CFStringCreateWithCString 产生调用方持有的 CFStringRef，返回后未 CFRelease；每次编码会话泄漏两个小 CF 对象，长期反复导出累积原生堆占用。
  - 建议: 用 CFRelease（或 cf.CFRelease 配好签名）在 finally 中释放 key/value 两个引用。
  - 置信度: 高

- [AUD-35210] [Suggestion] _length_prefixed_to_annex_b 为死代码
  - 位置: src/shared/media/macos_vt/vt_compression.py:336-351
  - 证据:
    ```python
    def _length_prefixed_to_annex_b(data: bytes, nal_length_size: int) -> bytes:
        """Convert VideoToolbox's AVCC packet payload to muxer-friendly Annex B."""
        ...
    ```
    （全库 grep 仅此一处定义，无任何调用）
  - 影响: 维护噪音；读者会误以为存在 Annex-B 转换路径，实际 muxer 直写 AVCC。
  - 建议: 删除，或在保留时注明"供 TS 流导出预留"并接入调用点。
  - 置信度: 高

- [AUD-35211] [Suggestion] _extract_parameter_sets 静默返回空集，把"能力缺失"推迟成首包处的误导性失败
  - 位置: src/shared/media/macos_vt/vt_compression.py:214-215,221-222,237-238,281-284
  - 证据:
    ```python
    if _CM_LIB is None or not format_description:
        return (), 4
    function = getattr(_CM_LIB, function_name, None)
    if function is None:
        return (), 4
    ...
    if int(status) != 0 or not first_pointer.value:
        return (), 4
    ...
    if len(parameter_sets) < 2:
        raise VideoToolboxCompressionError("h264_parameter_sets_incomplete")
    ```
  - 影响: CoreMedia 缺导出函数或 format_description 为空时静默返回 ()，直到 muxer 首包才抛 "parameter_sets_incomplete"——此时编码管线已跑完若干帧，错误信息指向"参数集不全"而非"能力缺失"，且失败点晚期（整段导出报废在写包阶段）。
  - 建议: 区分"函数缺失/描述缺失"并在会话首个回调即结构化上抛（或写入 last_error 供 AUD-35208 的失败通道使用）。
  - 置信度: 高

- [AUD-35212] [Suggestion] write_audio_samples 的帧布局回退可能与 add_audio_stream 的流布局不一致（>2 声道）
  - 位置: src/shared/media/macos_vt/vt_compression.py:892-901（对照 855-866）
  - 证据:
    ```python
    layout = (
        "stereo" if channels == 2
        else "mono" if channels == 1
        else f"{channels}c"
    )
    for start in range(0, int(array.shape[1]), 1024):
        frame = av.AudioFrame.from_ndarray(
            block, format="fltp", layout=layout
        )
    ```
    而 add_audio_stream 侧：`stream.layout = ChannelLayout.from_channel_count(int(channels)).av_layout_name`（如 6 声道 → "5.1"）。
  - 影响: 声道数 >2 时帧布局为未定序 "Nc"、流布局为具名布局（5.1 等），两者不一致可能导致 PyAV 编码报错或声道序错误。与 src/shared/export/video_encoder.py 的既有同款模式一致，属跨文件一致风险。
  - 建议: write 侧复用 add_audio_stream 记录的流布局（缓存 av_layout_name），保证帧/流布局同源（两处一并修）。
  - 置信度: 低（未实测 >2 声道路径；失败模式依赖 PyAV/FFmpeg 对未定序布局的比较行为）

### 通过项
- 输入验证：会话与 muxer 的 codec 白名单（fourcc 匹配）、尺寸正数、pixel_format 白名单、surface 后端/尺寸/格式逐项校验，错误码结构化可诊断
- GPU 资源生命周期：pending 表按 submission_id 追踪 GpuEncodeSurface，EncodeFrame 异常/失败路径均 _remove_pending_surface；与 GpuEncodeSurface.submit 的失败自释放幂等合流无双释放；close 顺序为 complete → 等 pending 排空 → invalidate → 强释剩余（T-P0-31 尾帧修复正确）
- HDR/位深正确性：P010 强制 HEVC Main10 profile，硬件拒绝时结构化失败上抛而非静默 8-bit 造假；hvcC bit_depth_minus_8 与 p010 对应，chromaFormat=1 正确；无非法 clamp
- avcC/hvcC 构造：字段布局核对无误（sps[1:4]、lengthSizeMinus_one、numOfArrays=3、profile_tier_level 偏移 3 长 12）
- 关键帧判定：H264 NAL type5 / HEVC 19-21 解析正确，长度前缀边界防御完备
- 线程安全（回调侧）：回调持 RLock 操作 pending/last_error；CFUNCTYPE/py_object 引用均在属性上保活至 invalidate 后才置 None
- 异常与取消路径：muxer 音频 flush 失败仍 finally 关容器；音频流必须在首个视频包前（_has_muxed_packet 门）；__del__/close 幂等且吞解释器关闭期异常
- 性能模式：回调仅拷贝压缩字节不碰像素平面；submit 返回 gpu_submit_ms=None 明示非 GPU 时间，无虚假指标
- 信号防递归 / 状态提交一致性：不适用（无 Qt 信号、无 undo/revision/持久化状态）
- 类型注解：公开 API 与内部函数注解完整
