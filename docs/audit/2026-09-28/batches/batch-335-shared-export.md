# batch-335 审查报告：src/shared/export（P2）

编号区间：AUD-33500–AUD-33599。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 /
信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。
两文件均已全文 Read（227 + 2,439 行），交叉引用（encoder.py、topos_encoder.py、
timeline_export.py、composite_render_output.py、media/frame.py、channel_layout.py）已核实。

---

## src/shared/export/topos_rate_control.py (行数: 227)

### 发现

- [AUD-33501] [Suggestion] anchored() 会覆盖本帧 _account 新置位的 reseed_hint
  - 位置: src/shared/export/topos_rate_control.py:185-190
  - 证据:
    ```python
    def anchored(self, qp_used: int, packet_size: int) -> None:
        """复杂度重锚（编码器前瞻/切换检测触发的 sized 搜索结果）：
        只更新 qp，不清预算累计/窗口/债务（与首帧 seed 的区别）。"""
        self.qp = min(self.qp_max, max(self.qp_min, int(qp_used)))
        self._account(packet_size)
        self.reseed_hint = False
    ```
    而 `_account`（134-146 行）内 `is_cut` 与超封顶都会 `self.reseed_hint = True`。
  - 影响: 若重锚帧自身又是场景切换/超 lend 上限帧，`_account` 刚置位的
    "下一帧应重锚"信号被无条件清掉。经核实 topos_encoder.py:2736-2777 的消费侧有
    8 帧 `_REANCHOR_COOLDOWN` 兜底（重锚后下一帧本就不再重锚），且有独立的
    复杂度前瞻 ratio_cut 通道，实际影响被缓解为"语义耦合脆弱"而非错误行为。
  - 建议: anchored() 只在进入时清除旧 hint（`self.reseed_hint = False` 置于
    `_account` 之前），或改为 `if not 新 cut: 清除`，使本帧新信号不被吞。
  - 置信度: 中（机制确定；因冷却兜底实际后果有限）

- [AUD-33502] [Suggestion] 全零字节历史可使 _next_qp 触发 math.log2 定义域异常
  - 位置: src/shared/export/topos_rate_control.py:158-164
  - 证据:
    ```python
    win_term = 0.0
    if len(self._win) >= max(4, self.window // 2):
        win_term = 8.0 * math.log2(
            (sum(self._win) / len(self._win)) / t)
        win_term = max(-6.0, min(6.0, win_term))
    integ = 0.0
    if self.frames > 4:
        integ = 8.0 * math.log2(self.total / (t * self.frames))
    ```
  - 影响: `prop` 项对 size 做了 `max(int(size), 1)` 钳位，但 `win_term`/`integ`
    没有——若历史帧 packet_size 全为 0（异常但类型合法的输入），`log2(0.0)` 抛
    ValueError 击穿码控。真实编码器出 0 字节包的概率极低。
  - 建议: 与 prop 项一致地对求和/总量做 `max(..., 1)` 钳位。
  - 置信度: 低

- [AUD-33503] [Suggestion] cut_factor/lend_cap_factor 无符号校验；telemetry() 返回类型未参数化
  - 位置: src/shared/export/topos_rate_control.py:79-83, 211
  - 证据:
    ```python
    self.window = max(2, int(window))
    self.cut_factor = float(cut_factor)
    self.lend_cap_factor = float(lend_cap_factor)
    self.repay_window = max(1, int(repay_window))
    ```
    ```python
    def telemetry(self) -> dict:
    ```
  - 影响: `window/repay_window` 有下界钳位，但 `cut_factor ≤ 0` 或
    `lend_cap_factor ≤ 0` 会被原样接受（负 cut_factor 使每帧都判为切换、负
    lend_cap 使所有超支都触发强修正信号），属于调用方编程错误时的静默错误配置。
    `-> dict` 缺类型参数（铁律：类型注解完整）。
  - 建议: 构造期对两个浮点参数做 `<= 0` 显式 ValueError；telemetry 标注
    `dict[str, Any]` 或 TypedDict。
  - 置信度: 中

### 通过项
- 线程安全：每编码器实例一份、仅在主编码线程消费（topos_encoder.py 单线程调用链），无跨线程可变共享
- GLSL·HDR 正确性：不涉及（纯码率控制数学）
- 信号防递归：不涉及（无 Qt 控件/信号）
- 状态提交一致性：纯帧级反馈状态，无 undo/revision/持久化职责
- 异常与取消路径：未定标即 note() 显式 RuntimeError；target_bytes/预算参数非法显式 ValueError；预算超限经 over_budget 供 close 硬裁决
- 性能模式：中位数走小数组排序（窗口 ≤24）、遥测封顶 4096、控制律 O(1)，无热点
- 确定性：无随机源/无时间依赖，qp 序列是字节历史的纯函数（与模块 docstring 声明一致）

---

## src/shared/export/video_encoder.py (行数: 2,439)

### 发现

- [AUD-33504] [Critical] VP9 编码参数（crf/bitrate/preset/gop_size）全部未下发给编码器
  - 位置: src/shared/export/video_encoder.py:1286-1287（对照 476-478、439-441）
  - 证据:
    ```python
    # GOP codec 特定参数
    if codec_lower in ("h264", "hevc", "av1"):
    ```
    而 `_is_gop_codec` 与 GOP 默认值把 vp9 当 GOP codec：
    ```python
    def _is_gop_codec(self) -> bool:
        """判断是否为 GOP 编解码器"""
        return self.codec.lower() in ("h264", "hevc", "vp9", "av1")
    ```
    ```python
    if self.gop_size is None and self._is_gop_codec():
        self.gop_size = int(self.fps.to_fps() * 2)
    ```
  - 影响: `_configure_encoder` 的三个分支（h264/hevc/av1、prores、dnxhr）均不匹配
    vp9，crf/bitrate/preset/gop_size 一个都不设置——libvpx-vp9 落到 FFmpeg 默认
    目标码率 256 kb/s。该路径真实可达：composite_render_dialog.py:66 提供
    ("vp9","VP9","webm")，composite_render_output.py:388-402 带 crf 构造
    FFmpegEncoderConfig 后交本类编码。结果是无报错的严重画质劣化（渲染错误）。
  - 建议: 在 `_configure_encoder` 为 vp9 增加 libvpx 选项映射（crf→`crf`、
    bitrate→`b`/`maxrate`/`bufsize`、gop_size→`g`），或至少显式拒绝
    "vp9 不支持 crf/bitrate 配置" 而非静默忽略。
  - 置信度: 高

- [AUD-33505] [Major] 音频码率选项键 'b:a' 会被 avcodec_open2 静默忽略
  - 位置: src/shared/export/video_encoder.py:2234-2235
  - 证据:
    ```python
    if bitrate:
        self._audio_stream.options = {'b:a': f'{bitrate}k'}
    ```
    本文件自己的文档（1235-1238 行）已实证同一机制：
    ```python
    注意键名必须是 ``b``（AVCodecContext AVOption 正名）：
    ``b:v`` 是 ffmpeg CLI 的流指定符语法，经 PyAV 的 stream.options
    → avcodec_open2 会被**静默忽略**（2026-09-12 RTX 4090 真机
    实证：请求 20Mbps 输出与不设码率的对照组逐字节一致）。
    ```
  - 影响: timeline_export.py:2892/3735 以 `bitrate=config.audio_bitrate` 调用，
    AAC 导出的用户指定音频码率完全不生效，落编码器默认码率——与视频侧已修复的
    `b:v` 缺陷同根因。
  - 建议: 改为 `{'b': f'{bitrate}k'}`。
  - 置信度: 高

- [AUD-33506] [Major] close() 异常路径：容器未显式关闭、半成品文件不清理、不可重试
  - 位置: src/shared/export/video_encoder.py:2339-2347（对照 open() 的 981-985 清理）
  - 证据:
    ```python
    except Exception as e:
        logger.exception(f"Error closing encoder: {e}")
        raise

    finally:
        self._container = None
        self._stream = None
        self._audio_stream = None
        self._is_open = False
    ```
    open() 失败路径有显式半成品清理，close() 没有：
    ```python
    try:
        if os.path.exists(self._output_path):
            os.remove(self._output_path)
    except OSError:
        pass
    ```
  - 影响: flush/drain/close 中途抛异常（如转换超时 1723-1725、容器 mux 错误）时，
    (1) 容器引用被置 None 而未走正常 close，trailer 是否写出依赖 PyAV dealloc
    兜底，磁盘上遗留损坏文件且上层若无清理则以"半成品"存在；(2) `_is_open=False`
    使再次 close() 直接 return，文件句柄/会话只能靠 GC 释放。
  - 建议: finally 中对未关闭容器做 best-effort `close()`；异常路径与 open() 对齐，
    删除或显式标记半成品输出，交由导出 outcome 统一裁决。
  - 置信度: 高（代码行为确定；PyAV dealloc 兜底行为不确定，故不定为 Critical）

- [AUD-33507] [Major] RGB 序 16-bit 4 通道 packed 帧落入 uint8 'rgba' 格式，转换必失败
  - 位置: src/shared/export/video_encoder.py:1914-1918（对照 BGR 分支 1901-1905 的正确处理）
  - 证据:
    ```python
    else:
        temp_format = 'rgb48le' if is_high_depth else (
            'rgb24' if data.shape[-1] == 3 else 'rgba'
        )
        temp_frame = av.VideoFrame.from_ndarray(data, format=temp_format)
    ```
    BGR 分支对同类输入显式用 16-bit 承载：
    ```python
    if channels == 4:
        temp_frame = av.VideoFrame.from_ndarray(
            data[..., [2, 1, 0, 3]],
            format='rgba64le' if is_high_depth else 'rgba'
        )
    ```
  - 影响: `is_high_depth=True`（uint16 数据）且 4 通道、源为 RGB 序（非 bgr）时，
    `from_ndarray` 以 'rgba'（uint8）接收 uint16 数据会抛错，带 alpha 的高位深
    RGB 导出失败。BGR 路径已处理同一场景，RGB 路径漏了。缓解因素：时间线主路径
    （timeline_export.py:10226 起）在送编码器前已把高位深 RGB 降为 uint8 BGR，
    当前触达面有限，属不对称的潜在缺陷。
  - 建议: 4 通道高位深时 temp_format 取 'rgba64le'（与 BGR 分支对齐）。
  - 置信度: 中

- [AUD-33508] [Major] 多转换线程下 _converted_map 无界，注释宣称的内存上界不成立
  - 位置: src/shared/export/video_encoder.py:1706-1708, 1716-1719（契约注释 363-364、队列 794-796）
  - 证据:
    ```python
    with self._convert_cond:
        self._converted_map[pts] = entry
        self._convert_cond.notify_all()
    ```
    ```python
    with self._convert_cond:
        ready = self._convert_cond.wait_for(
            lambda: self._next_encode_pts in self._converted_map,
            timeout=60.0 if block else 0.0,
        )
    ```
    而配置注释宣称：`in-flight 内存上界 ≈ (workers+1) 帧输入 + workers 帧输出`。
  - 影响: 背压只作用于 `_convert_queue`（maxsize=workers+1，794-796 行）。worker
    取走帧后队列即腾位，转换完成的帧进 `_converted_map` 等待按 pts 序编码；若某
    早期帧转换慢（如 packed swscale ~45ms）而后续帧快（planar 直填 ~5ms），map
    中积压的已转换 4K 帧（每帧 ~50MB）可达 (W-1)×成本比 量级，极端布局下内存尖峰。
    另注：close() 2315 行 `while self._converted_map:` 在未持 `_convert_cond` 的
    情况下读 dict 真值（join 超时线程仍存活时有轻微竞态）。
  - 建议: 给 map 设容量上界（worker 放入前 wait），或以"map 大小 ≥ k 时 worker
    阻塞"实现输出侧背压；close 排空循环的判空移入锁内。
  - 置信度: 中（机制成立；需转换耗时显著倾斜才显现）

- [AUD-33509] [Major] 音频流声道布局与帧声道布局两套推导不一致
  - 位置: src/shared/export/video_encoder.py:2227-2232 与 2270-2271
  - 证据:
    ```python
    try:
        from src.features.edit.core.channel_layout import ChannelLayout
        self._audio_stream.layout = ChannelLayout.from_channel_count(channels).av_layout_name
    except (ValueError, ImportError):
        # 非标准声道数或模块不可用：回退到 stereo/mono（保持向后兼容）
        self._audio_stream.layout = 'stereo' if channels == 2 else 'mono'
    ```
    ```python
    channels = samples.shape[0]
    layout = 'stereo' if channels == 2 else ('mono' if channels == 1 else f'{channels}c')
    ```
  - 影响: 流侧用规范名（如 6 声道 → '5.1'；非标准声道数回退 'mono'），帧侧用
    `'6c'`/`'3c'` 未知序布局。编码帧布局与 codec 上下文布局不一致时 libavcodec
    拒绝或错序映射；非标准声道数（3/4/5…）时流被降为 mono 而帧仍是多通道，
    必然失配。另 `write_audio_samples` 未校验 dtype（docstring 约定 float32，
    float64 输入 from_ndarray 直接抛错）。
  - 建议: 帧布局与流布局同源（同一 ChannelLayout 推导，单一真相）；非标准
    声道数在 add_audio_stream 显式报错而非回退 mono；入口校验 samples.dtype。
  - 置信度: 中（两套推导的不一致确凿；具体失败形态依赖 libavcodec 版本）

- [AUD-33510] [Suggestion] _create_planar_av_frame 的打包分支硬编码 'rgb24'（当前不可达）
  - 位置: src/shared/export/video_encoder.py:2166-2169
  - 证据:
    ```python
    if frame.is_packed and frame.data is not None:
        # 有打包数据，直接使用
        temp_frame = av.VideoFrame.from_ndarray(frame.data, format='rgb24')
        return temp_frame.reformat(format=format_name)
    ```
  - 影响: 经核实 media/frame.py：`is_packed = data is not None`，而本函数只从
    `_convert_planar_frame`（前置条件 `frame.is_planar and frame.planes` 且已排除
    packed 路径）到达，data 必为 None——该分支当前是死代码；若未来被直接调用，
    RGBA/16-bit packed 数据会被按 rgb24 uint8 误读（颜色错乱或抛错）。
  - 建议: 删除该分支，或按 frame.pixel_format 选真实格式。
  - 置信度: 高（不可达性经 frame.py 语义核实）

- [AUD-33511] [Suggestion] 硬编探针 CodecContext 未显式关闭
  - 位置: src/shared/export/video_encoder.py:1124-1144
  - 证据:
    ```python
    ctx = av.codec.CodecContext.create(encoder_name, "w")
    ...
    ctx.open()
    list(ctx.encode(av.VideoFrame(self._config.width,
                                  self._config.height, pix_fmt)))
    return True
    ```
  - 影响: 探针打开的真实硬件编码会话（AMF/NVENC/VT）与分配的帧缓冲靠引用计数
    回收释放；CPython 下延迟有限，但非确定性，连续多次导出/多编码器实例时硬件
    会话占用窗口变长。
  - 建议: try/finally 中显式 `ctx.close()`（PyAV CodecContext 支持），或用
    contextlib 等价物。
  - 置信度: 中

- [AUD-33512] [Suggestion] FFmpegEncoderConfig 缺 fps/crf/threads 等构造期验证
  - 位置: src/shared/export/video_encoder.py:408-452
  - 证据:
    ```python
    def __post_init__(self) -> None:
        """验证配置并自动推断参数"""
        # 验证分辨率
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"Invalid resolution: {self.width}x{self.height}")
    ```
  - 影响: `__post_init__` 只验证分辨率/预算/gif 颜色数。fps ≤ 0 或 den=0 要到
    open()/探针才以 ZeroDivisionError/AttributeError 形态暴露（1129 行
    `Fraction(1, 1) / Fraction(fps)`）；CPU 路径 crf 原样透传（1305 行
    `str(self._config.crf)`，负值/超 51 由 x264 在 open 时报错）；threads 负值
    落入 1364-1367 的 `except Exception: pass` 被静默吞掉。违反"外部数据须验证"。
  - 建议: 构造期校验 fps 为正有理数、crf ∈ [0,51]、threads ≥ 0。
  - 置信度: 中

- [AUD-33513] [Suggestion] yuv420p + has_alpha 被静默降级为无 alpha，仅告警
  - 位置: src/shared/export/video_encoder.py:429-432
  - 证据:
    ```python
    # 自动推断像素格式
    if self.pix_fmt == "yuv420p" and self.has_alpha:
        logger.warning("Alpha requested but pixel format is yuv420p, no alpha support")
        self.has_alpha = False
    ```
  - 影响: 调用方声明需要 alpha 输出但像素格式不支持时，配置被静默改写——导出
    成品缺 alpha 通道而用户无感知（日志级告警不触达 UI）。对"带 alpha 交付"是
    隐性数据缺失。
  - 建议: 提供 strict 开关或直接 ValueError，让调用方显式选择放弃 alpha 或改
    yuva 系格式。
  - 置信度: 中

- [AUD-33514] [Suggestion] 类型注解不完整（未参数化的容器类型）
  - 位置: src/shared/export/video_encoder.py:786-790
  - 证据:
    ```python
    self._convert_queue: Optional["queue.Queue"] = None
    self._converted_queue: Optional["queue.Queue"] = None
    self._convert_threads: list = []
    self._converted_map: Optional[dict] = None
    ```
  - 影响: 模块自称"完整类型注解"（docstring 设计原则），queue.Queue/list/dict
    未带元素类型，IDE/静态检查无法校验队列元素 `(frame, pts)`、map 值
    `(av_frame, error)` 的结构。
  - 建议: 参数化为 `queue.Queue[tuple]`/`list[threading.Thread]`/
    `dict[int, tuple]` 等精确类型。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：open() 失败清理容器并置空；x265 10-bit 段错误防线（N06）显式关闭容器、删除半成品文件；选项级硬编失败经 broken_container.close() 后整体 CPU 重建，无泄漏路径
- GLSL·HDR 正确性：BT.2020 矩阵绕开 swscale 枚举缺失走 numpy 精确转换（系数/limited-range 位移/p010 MSB 对齐均正确，BT.2020+alpha 显式拒绝而非静默丢通道）；HDR10 静态元数据经 x265-params 合并保留既有码控参数；硬编通道跳过 x265-params 避免非法选项；x265 10-bit 确定性段错误有显式防线
- 信号防递归：不涉及（无 Qt 控件/信号）
- 状态提交一致性：不涉及（导出编码单向流，无 undo/revision 状态）
- 线程安全（编码主链路）：encode/mux 严格限定主调用线程；单 worker FIFO 保序、多 worker 持 `_convert_cond` 访问 map；转换异常经队列/map 传递到主线程统一抛出（BaseException 捕获含 KeyboardInterrupt 语义）；daemon 线程 + join(60s) + 超时告警
- 硬编路由：vendor 探测带缓存、auto 限 SDR、探针失败与选项级失败均有结构化 `encoder_hw_unavailable:<vendor>:<detail>` 回退诊断；`b`/`maxrate`/`bufsize` AVOption 正名在视频侧使用正确
- 异常与取消路径：open() 失败清理资源并区分 RuntimeError/IOError；零转换快路不破坏全局 pts 保序；close() 保序先 drain 再 flush
- 性能模式：零转换快路、BGR 直填、plane.update buffer 协议直传、流水线转换重叠等优化均有实测注释支撑；`_hw_vendor_cache`/probe 缓存避免重复探测
- 输入验证（已覆盖部分）：分辨率/预算三元组/gif 颜色数在构造期显式 ValueError；DNxHR/VT 码率映射有界钳位
