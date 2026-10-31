# 批次报告 batch-351 [P2] — src/shared/media/macos_vt/

编号区间：AUD-35100–AUD-35199。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解。
交叉验证文件（只读）：`src/shared/media/gpu_frame.py`（GpuFrameRelease/GpuDecodedFrame 语义）、`src/shared/media/macos_vt/vt_session.py`（decode_packet None 语义、_CV_LIB 原型）、`tests/media/test_macos_vt_zero_copy.py`、`src/shared/video/frame_provider_service.py`（close() 调用点）。已用项目 venv（PyAV 17.0.1）确认 `Packet.buffer_ptr/buffer_size` 存在、`container.seek(stream=...)` 签名仍受支持。

## src/shared/media/macos_vt/packet_demux.py (行数: 324)

### 发现

- [AUD-35101] [Major] packets() 的 max_packets 守卫在包已取出后才判断，多消耗并永久丢弃一个压缩包
  - 位置: src/shared/media/macos_vt/packet_demux.py:249-252
  - 证据:
    ```python
    count = 0
    for packet in self._container.demux(self._stream):
        if max_packets is not None and count >= max_packets:
            break
    ```
  - 影响: 守卫位于 demux 取包之后。调用方 `source._read_next_decoded_pixel_buffer`（source.py:648）以 `packets(max_packets=1)` 反复续读，当某个包解码无输出时（source.py:658-659 `continue` 继续迭代），生成器从 demux 再取出下一个包后才 break，该包被静默丢弃——下一次 `packets()` 从其后继续，码流跳包/丢帧且无任何日志。触发条件是 VT 对某包无输出（vt_session.py:1721-1724 输出队列 0.5s 取不到即返回 None：重排延迟、解码错误、会话异常）。
  - 建议: 在取出包之前判定配额（如 `while count < max` 配合显式 next），或在生成器终止时不丢弃已取出的包；至少对"取出即弃"路径记录 warning。
  - 置信度: 高（丢弃机制确定；实际触发频率取决于 VT 无输出包的出现频率）

- [AUD-35102] [Suggestion] 像素格式→位深启发式把 12-bit 与 8-bit 4:2:2 流标记为 10-bit
  - 位置: src/shared/media/macos_vt/packet_demux.py:187-190
  - 证据:
    ```python
    if '10' in self._info.pixel_format or 'p010' in self._info.pixel_format.lower():
        self._info.bit_depth = 10
    elif '12' in self._info.pixel_format or '422' in self._info.pixel_format.lower():
        self._info.bit_depth = 10
    ```
  - 影响: `'12' in pixel_format`（如 yuv422p12le/yuv444p12le，12-bit ProRes 4444 XQ）与 `'422' in pixel_format`（如 yuv422p，8-bit XDCAM HD422）都得到 bit_depth=10。下游 `source.open` 以 bit_depth>8 选 P010 会话并透传位深元数据：12-bit 内容被截到 10-bit 容器、8-bit 4:2:2 被误报为 10-bit（影响能力门禁、UI 位深显示与导出决策）。
  - 建议: 用 PyAV/FFmpeg pix_desc 精确解析位深，或至少 12→12、去掉对 `'422'` 子串的 10-bit 默认。
  - 置信度: 高（映射错误确定；影响路径已核对 source.py:228）

- [AUD-35103] [Suggestion] _ffmpeg_colorspace_name 把 AVCOL_SPC_BT2020_CL(10) 映射为 'bt2020nc'，与自身 docstring 矛盾
  - 位置: src/shared/media/macos_vt/packet_demux.py:204-217
  - 证据:
    ```python
    """FFmpeg AVCOL_SPC_* 枚举 → 规范矩阵名（空串=未标记）。

    0=RGB, 1=bt709, 5=bt470bg, 6=smpte170m, 7=smpte240m,
    9=bt2020nc, 10=bt2020c。未标记(0/2=unspecified)时返回空串，
    ...
    return {
        ...
        9: 'bt2020nc',
        10: 'bt2020nc',
    }.get(int(value or 0), '')
    ```
  - 影响: docstring 声明 10=bt2020c，代码却返回 'bt2020nc'。BT.2020 CL 素材被标记为 NCL。鉴于下游 `gpu_frame.VALID_COLOR_MATRICES` 不含 'bt2020c' 且 VT 输出为 NV12/P010（NCL 型平面），当前映射可能是刻意归一，但注释与代码不一致会误导后续维护，且 CL 语义信息丢失。
  - 建议: 明确意图——若刻意归一为 NCL 请修正 docstring 并注明理由；否则映射 10→'bt2020c' 并在下游别名表补充该值。
  - 置信度: 高（不一致确定；实际色彩影响低）

- [AUD-35104] [Suggestion] open() 的 codec_name 回退分支引用刚判定不存在的属性
  - 位置: src/shared/media/macos_vt/packet_demux.py:155
  - 证据:
    ```python
    codec_name=stream.codec_context.name if hasattr(stream, 'codec_context') else str(stream.codec_context),
    ```
  - 影响: `hasattr` 为 False 时回退表达式 `str(stream.codec_context)` 必然 AttributeError——守卫与回退自相矛盾。PyAV Stream 实际始终有 codec_context，属不可达死分支缺陷，一旦 PyAV API 变化即显性崩溃。
  - 建议: 回退改为安全值（如 `''`）或直接取 `stream.codec_context.name`。
  - 置信度: 高

### 通过项
- 输入验证：无视频流时关闭容器并抛 ValueError（136-139）；extradata/尺寸/时间基提取不触发解码。
- 验收约束遵守：全程未调用 container.decode()/packet.decode()/frame.to_ndarray()。
- pixel_format 经 stream.format 安全通道读取，规避 PyAV 17 codec_context.pix_fmt 段崩溃（166-172 注释与实现一致）。
- 线程模型：单线程使用契约明确，无共享可变状态；无 GL/UI 交互。
- seek：backward 关键帧语义与时间基换算正确（PyAV 17 `seek(stream=...)` 签名已用 venv 验证仍存在）。
- 类型注解完整；CompressedPacket/VideoStreamInfo 为强类型 dataclass。

## src/shared/media/macos_vt/metal_capability_report.py (行数: 151)

### 发现

- [AUD-35110] [Suggestion] _atomic_write_text 写失败时残留临时文件，replace 前无 fsync
  - 位置: src/shared/media/macos_vt/metal_capability_report.py:120-128
  - 证据:
    ```python
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(content)
    temporary.replace(path)
    ```
  - 影响: `handle.write` 抛异常（磁盘满、编码错误）时 `delete=False` 的临时文件遗留目录且无人清理；写后未 `flush()+fsync` 即 rename，进程崩溃/掉电时目标文件可能为空（rename 的原子性只保证可见性一致性，不保证崩溃后内容完整——对"probe 结果留档"这一用途是实际风险）。
  - 建议: try/finally 中失败时 unlink 临时文件；成功路径写后 flush + os.fsync(fileno) 再 replace。
  - 置信度: 高（行为确定；影响低）

### 通过项
- build_report 构造 JSON-safe payload，full_chain_gpu_claim 恒为 False 并附显式 reason，防能力探测被误读为全链路 GPU 证明。
- write_report 异常统一转带 `probe_output_write_failed:` 前缀的 OSError，无静默吞错（宽 except 但有显式 re-raise 与 reason code）。
- render_markdown 对空 device/空 matrix/空 reasons 均有显式占位输出，不隐藏 unsupported。
- 输入验证/线程安全/类型注解：模块无共享可变状态，注解完整（Mapping/Path/str | None）。

## src/shared/media/macos_vt/iosurface_gl_bridge.py (行数: 1198)

### 发现

- [AUD-35120] [Major] IOSurfaceGLBridge.destroy() 无属主线程守卫，跨线程调用会在无 GL 上下文线程上执行 glDeleteTextures 并静默泄漏
  - 位置: src/shared/media/macos_vt/iosurface_gl_bridge.py:1152-1163（bridge.destroy）、490-505（ring.destroy 的 `except Exception: pass`）、对照 689-696（bind 的线程守卫）
  - 证据:
    ```python
    def destroy(self) -> None:
        """销毁 bridge 和 ring buffer"""
        if self._ring_buffer is not None:
            self._ring_buffer.destroy()
            self._ring_buffer = None
    ```
    ring.destroy 内：
    ```python
                    try:
                        glDeleteTextures([tex_id])
                    except Exception:
                        pass
    ```
    而 bind 路径有严格守卫（689-691）：
    ```python
        if self._owner_thread_id != threading.get_ident():
            self._last_fallback_reason = "bridge_thread_mismatch"
    ```
  - 影响: bridge 由 `source.import_pixel_buffer_frame`（须在渲染线程调用）创建并记录 `_owner_thread_id`，但 `source.close()`（frame_provider_service.py:2002/2070 由服务/工作线程调用）→ `self._bridge.destroy()` → 在无 current context 的线程执行 glDeleteTextures：PyOpenGL 抛错被 `pass` 吞掉，全部 texture 名静默泄漏；与 `__del__`（537-540 行）声明的"GL 对象必须在属主 context current 时释放"直接矛盾。同文件的 initialize/bind 都做了线程 fail-closed，唯独 destroy 没有。
  - 建议: destroy() 复用 `_owner_thread_id` 守卫（非属主线程时拒绝并记录、或经 deferred-release 队列转交渲染线程执行）；glDeleteTextures 失败至少记 warning 便于发现泄漏。
  - 置信度: 中（类内守卫不一致确定；是否实际跨线程取决于调用方线程上下文）

- [AUD-35121] [Suggestion] get_next_textures 游标取模用初始 _ring_size，与运行期扩容后的 ring 不一致
  - 位置: src/shared/media/macos_vt/iosurface_gl_bridge.py:344（`self._textures[self._current_index % self._ring_size]`），对照 438 行 `ring.extend(self._allocate_texture_ring(count=1))`
  - 证据:
    ```python
        textures = self._textures[self._current_index % self._ring_size]
        self._current_index += 1
        return list(textures)
    ```
  - 影响: `acquire_textures_for_target` 扩容后 `_textures`（与 `_textures_by_key['2d']` 同一 list）长度可超过 `_ring_size`，legacy `get_next_textures` 永远访问不到新增槽位，游标语义与 `get_next_textures_for_target`（`% len(ring)`，396 行）不一致。当前 src 内无调用方，属遗留 API 的潜伏不一致。
  - 建议: 改为 `% len(self._textures)` 与另一 API 对齐，或删除无调用方的 legacy 接口。
  - 置信度: 高（逻辑确定；无现实调用方，影响低）

### 通过项
- GPU 资源生命周期：per-frame `_retain_cv_buffer` 与 release 回调精确配对（961-983）；GpuFrameRelease 幂等（gpu_frame.py:247-260 已核对），杜绝 CVPixelBuffer 双重释放。
- 纹理 ring buffer：slot 家族隔离（首次 bind 锁定 target）、跨族迁移显式拒绝（356-374）、失效 slot 隔离且 retired 名保留到 destroy 防泄漏（463-488）。
- 多 binding profile 降级链完整且每步记录 bind_errors，最终 fallback reason 显式（955-959）。
- 陈旧 GL 错误标记清理（281-284、863）与无 context 时禁止真实 GL 调用的守卫，避免 mock/无上下文环境段错误。
- `__del__` 双双 no-op，杜绝 GC 线程 GL 调用（537-540、1165-1168）。
- 线程安全：initialize/bind 属主线程守卫 fail-closed。
- HDR/GLSL 正确性：P010 使用 GL_R16/GL_RG16 normalized + UNSIZED 降级组合，符合 10-bit 采样语义；NV12/P010 profile 分派与 bit_depth 联动正确。
- initialize 失败路径完整回滚状态（325-332）。

## src/shared/media/macos_vt/source.py (行数: 1278)

### 发现

- [AUD-35130] [Major] _read_next_decoded_pixel_buffer 单包提交且把"VT 暂无输出"当作 EOF，错误/重排延迟路径提前终止解码并叠加丢包
  - 位置: src/shared/media/macos_vt/source.py:648-659（单包迭代与 continue 放弃）、499-501（`decoded is None` 当 EOF break）
  - 证据:
    ```python
        for pkt in self._demuxer.packets(max_packets=1):
            if self._vt_session is None:
                return None

            decoded = self._vt_session.decode_packet(
                packet_data=pkt.data,
                pts=pkt.pts,
                is_keyframe=pkt.is_keyframe,
            )

            if decoded is None:
                continue
    ```
    配合 `_decode_until_present_frame`（499-501）：
    ```python
            decoded = self._read_next_decoded_pixel_buffer()
            if decoded is None:
                break
    ```
    而 vt_session._decode_packet_impl（vt_session.py:1721-1724）在输出队列 0.5s 取不到帧时返回 None（"no decoded frame available"），并非 EOF。
  - 影响: 任一包无输出（B 帧重排延迟、损坏包、会话瞬时异常）时：`continue` 结束单包生成器 → 本函数返回 None → `_decode_until_present_frame` 以 EOF 语义 break。顺序播放路径（read_next_pixel_buffer，无 deadline/nearest 兜底语义失真）表现为提前停止或反复近似帧；与 AUD-35101 叠加还会丢弃紧随其后的压缩包。VT 正常"逐包一发"时此路径仅在错误时触发，但一旦触发即静默（无 warning 日志）。
  - 建议: 在同一迭代器内循环取包直到取得输出帧或 demux 真正 EOF（`while` + `next(demux_iter, None)`），区分"无输出"与"无输入"；对连续多包无输出记录 warning 并设上限。
  - 置信度: 中（机制与代码路径确定；日常触发频率取决于 VT 是否逐包必有输出）

- [AUD-35131] [Suggestion] read_frame_pixel_buffer 顺序/快路径无异常兜底，与 seek 分支策略不一致
  - 位置: src/shared/media/macos_vt/source.py:968-983（顺序分支无 try）、对照 989-1007（seek 分支 try/except 返回 None）
  - 证据:
    ```python
        # 如果请求当前帧，直接顺序读取
        if frame_index == self._current_frame:
            return self.read_next_pixel_buffer()
        ...
        if (
            frame_index > self._current_frame
            and frame_index - self._current_frame <= max_forward_without_seek
        ):
            return self._decode_until_present_frame(frame_index)
    ```
  - 影响: demux 迭代（`container.demux`）在流损坏/IO 错误时抛 av 异常，seek 分支会捕获并返回 None + warning，顺序分支则直接向上冒泡到共享帧服务 worker；同一公开 API 的失败语义不一致，依赖外层兜底。
  - 建议: 顺序分支同样包 try/except 返回 None 并记录 fallback 原因，或统一在 `_read_next_decoded_pixel_buffer` 内捕获 demux 异常。
  - 置信度: 中（异常源确定；实际冒泡取决于调用方兜底）

- [AUD-35132] [Suggestion] MacOSVtPlanarSource.open 签名与基类不兼容（LSP 违反）
  - 位置: src/shared/media/macos_vt/source.py:1084-1086，对照基类 177 行 `open(self, path, *, require_zero_copy=True, require_hardware=False)`
  - 证据:
    ```python
        def open(self, path: str) -> bool:
            """Open a VT decode session without requiring the GL zero-copy proof."""
            return super().open(path, require_zero_copy=False)
    ```
  - 影响: 通过基类引用对子类实例调用 `open(path, require_hardware=...)`（能力探测/工厂路径使用的关键字）将 TypeError；类型上子类收窄了公开接口。
  - 建议: 子类签名补 `*, require_hardware: bool = False` 并透传。
  - 置信度: 高（签名事实确定；触发依赖是否存在多态调用点）

- [AUD-35133] [Suggestion] 关键解码函数缺返回类型注解；deferred 帧契约靠动态属性传递
  - 位置: src/shared/media/macos_vt/source.py:638（`def _read_next_decoded_pixel_buffer(self):` 无返回注解）、873（`wrap_pixel_buffer_deferred(self, frame)` 的 frame 形参无类型）、902-903（动态属性）
  - 证据:
    ```python
        def _read_next_decoded_pixel_buffer(self):
        ...
        def wrap_pixel_buffer_deferred(self, frame) -> GpuDecodedFrame | None:
        ...
        gpu_frame.deferred_import = 'macos_vt_pixel_buffer'
        gpu_frame.deferred_vt_frame = frame
    ```
  - 影响: `deferred_import/deferred_vt_frame` 不在 GpuDecodedFrame 字段中（gpu_frame.py:293-343 已核对），契约仅靠 duck-typing，跨模块消费方（async_renderer/video_frame_node）拼写错误无法被静态检查发现；`frame` 形参与解码函数返回值缺注解削弱类型保障。
  - 建议: 在 GpuDecodedFrame 增加可选字段（`deferred_import: str | None`、`deferred_vt_frame: object | None`），并为上述函数补全注解（返回 `VtDecodedPixelBuffer | None`、形参 `frame: VtPixelBufferFrame`）。
  - 置信度: 高

### 通过项
- 线程安全：GL 导入仅在渲染线程（bridge 属主守卫）；解码线程产出 VtPixelBufferFrame 不触 OpenGL（96-134 设计与实现一致）；VtPixelBufferFrame.release() 以锁保证 exactly-once（128-134）。
- reorder 缓冲：prune（floor=±2）+ 硬上限 16 防 4K CVPixelBuffer 无限滞留（392-417），seek 全清（368-378），dup 帧释放（385-388）。
- 交互取消：is_stale/deadline 在追帧循环、seek 前多点检查（478-498、548-577、613-617）。
- 色彩元数据：流级 color_space/color_range 透传并 normalize（273-280），帧级回退链正确（683-701），修复 BT.601/full-range 色偏路径与调色页对齐。
- close() 顺序正确：先清 reorder（释放帧）再销毁 session/demuxer/bridge（293-312）。
- planar 回退：Lock/UnlockBaseAddress 配对（1240-1275）、借道 ctypes view 单次拷贝去 padding、P010 高 10 位 >>6 对齐下游期望、bit_depth/dtype/PlaneInfo 与 windows_hw/hw_planar_source.py:254 约定一致；失败 fail-closed 返回 None。
- 验收约束：read 路径不调用 to_ndarray()/transfer()，zero_copy 标记仅用于真 external texture 路径（deferred 包装显式 zero_copy=False，899 行）。
- capability gate：metal_zero_copy_gate 逐条显式拒绝理由，无静默降级（58-93）。

## 汇总

| 严重度 | 数量 | 编号 |
|---|---|---|
| Critical | 0 | — |
| Major | 3 | AUD-35101, AUD-35120, AUD-35130 |
| Suggestion | 8 | AUD-35102, 35103, 35104, 35110, 35121, 35131, 35132, 35133 |
