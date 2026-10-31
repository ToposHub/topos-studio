# batch-347 审查报告 — src/shared/media/ffmpeg_source.py

批次：batch-347（AUD-34700–AUD-34799）｜审查日期：2026-09-28
文件已全文通读（1–3020 行，无跳读）。本文件为 PyAV CPU 解码源（无 GLSL/Qt 信号/undo 状态），
九个维度中信号防递归与状态提交一致性不适用，其余维度均有检查。

## src/shared/media/ffmpeg_source.py (行数: 3020)

### 发现

- [AUD-34703] [Critical] planar proxy 回退到 packed RGB 后仍按平面布局切片，输出只含每行前 1/3 样本的错乱平面
  - 位置: src/shared/media/ffmpeg_source.py:2311（回退入口，另见 2336-2366、2576-2579、2664-2672）
  - 证据:
    ```python
    # Fallback: 依次尝试 fallback chain 中的中间格式
    target_rgb_fmt = 'rgba64le' if self._metadata.has_alpha else 'rgb48le'
    fallback_chain = self._get_reformat_fallback_chain(
        original_pix_fmt, target_rgb_fmt
    )
    ```
    ```python
    stride_samples = line_size // itemsize
    samples = raw.view(sample_dtype).reshape(buffer_height, stride_samples)
    visible = samples[:plane_height, :plane_width]
    ```
    ```python
    else:
        component = f'P{i}'
        plane_width = av_plane.width if hasattr(av_plane, 'width') else width
    ```
  - 影响: `_convert_frame_planar` 在 planar 缩放失败或格式未映射时（14/16-bit YUV 如 yuv420p16le、
    12-bit YUVA 如 ProRes 4444 XQ 的 yuva444p12le 均不在 `_get_planar_format_for_scaling` 映射表内）
    回退到 packed rgb48le/rgba64le/rgb24，随后 `_extract_planes_from_frame` 按平面布局提取：packed 帧
    只有一个平面，line_size = 6×width 字节（rgb48le），而 plane_width = av_plane.width = width，
    切片 `samples[:, :plane_width]` 只取每行前 1/3 样本（仅 R 分量）。GPU planar 上传路径
    （edit_gpu_decode_manager.py:2105/2678 实际调用 read_frame_planar）会拿到单平面错乱数据——
    渲染错误，甚至 planes[1]/planes[2] 索引崩溃。
  - 建议: 回退到 packed RGB 时不要走 planar 提取；应改走 `_convert_frame` 的 packed 路径并在
    DecodedFrame 上如实标注，或在提取前 reformat 回同族 planar 格式。
  - 置信度: 高（代码逻辑确定；触发需 planar 模式 + target_size + 未映射/失败格式）

- [AUD-34700] [Major] total_frames 估算公式把 time_base 当除数，帧数虚高约 1/tb² 倍
  - 位置: src/shared/media/ffmpeg_source.py:792
  - 证据:
    ```python
    if stream.duration and stream.time_base:
        duration_pts = stream.duration
        fps = self._extract_fps(stream, container)
        total_frames = int(duration_pts * fps.num / (fps.den * float(stream.time_base)))
        return total_frames if total_frames > 0 else None
    ```
  - 影响: `stream.duration` 以 time_base 为单位，正确公式是 `duration_pts * time_base * fps`
    （秒 × 帧率）。现式除以 time_base：tb=1/1000 的 MKV（`stream.frames` 常为 0，必走此分支）
    10 秒 24fps 视频会算出 2.4 亿帧。total_frames 虚高使 read_frame/seek_frame 的帧号范围校验
    （1191-1197、1343-1349）形同虚设，时间线长度/总帧数展示严重失真。
  - 建议: 改为 `int(duration_pts * float(stream.time_base) * fps.num / fps.den)`。
  - 置信度: 高

- [AUD-34701] [Major] _decode_to_target overshoot 静默返回，read_frame 交付错误帧内容且帧号错标
  - 位置: src/shared/media/ffmpeg_source.py:1736（对照 _seek_gop_session:1645-1651 的正确处理）
  - 证据:
    ```python
    if self._current_frame >= target_frame:
        # 到达或超过目标帧，缓存目标帧
        if self._current_frame == target_frame:
            self._pending_av_frame = frame
        else:
            logger.warning(
                f"Seek overshoot: target={target_frame}, got={self._current_frame}"
            )
        return
    ```
  - 影响: VFR 或帧率元数据偏差使落点帧 pts 超过目标（overshoot）时仅 warning 即 return：
    `_current_frame` 停在超过值且无 pending 帧，seek_frame 正常返回（日志 "Seek complete"）。
    随后 read_next_frame 解出的是 overshoot+1 帧内容，且被 `_convert_frame` 以 `_current_frame`
    标注错误 frame_index——静默交付错帧，调色/审查场景下用户在错误的画面上做判断。同情形在
    `_seek_gop_session` 中显式抛 SeekError，两路径语义不一致。
  - 建议: overshoot 分支同样 raise SeekError，或按实际落点重算最近可达帧并上报调用方。
  - 置信度: 高

- [AUD-34702] [Major] _main_resync_pts 不随 seek 会话续读更新，会话耗尽/出错时主会话回卷重放已播帧
  - 位置: src/shared/media/ffmpeg_source.py:1658（写入后从不更新；另见 1660-1678、1680-1711）
  - 证据:
    ```python
    # 与主会话 seek 相同的落地语义：缓存目标帧，标记主会话待回同步
    self._pending_av_frame = landed
    self._current_frame = frame_index
    self._frame_iterator = None
    self._gop_seek_iterator = iterator
    self._main_resync_pts = target_pts
    ```
    ```python
    try:
        for frame in self._gop_seek_iterator:
            return frame
    except StopIteration:
        pass
    ```
  - 影响: `_main_resync_pts` 固定为 seek 目标 pts（帧 N），`_pull_next_frame_from_seek_session`
    顺序服务 N+1..M 期间不更新它。一旦会话迭代器耗尽（读到 EOF）或解码出错，`read_next_frame`
    走 `_resync_main_session_if_needed`，把主会话 seek 回 N 并从 N+1 重新供帧——已播放片段被
    重放（帧倒卷），`_current_frame` 计数与实际内容错位。
  - 建议: `_pull_next_frame_from_seek_session` 每次成功取帧时更新
    `_main_resync_pts = frame.pts`（pts None 归一为 0），使回同步点始终是"最后已服务帧"。
  - 置信度: 中（代码路径确定；触发需 HEVC 类 seek 后顺序读至会话耗尽/出错）

- [AUD-34704] [Major] planar 模式对 nv12/p010 半平面格式按纯 planar 提取，色度错乱且缺 V 平面
  - 位置: src/shared/media/ffmpeg_source.py:392（声明支持；另见 2547-2550、2692-2699）
  - 证据:
    ```python
    # NV12/P010 semiplanar formats (Y plane + interleaved UV)
    if pix_fmt_lower in ('nv12', 'nv21', 'p010le', 'p010be', 'p016le', 'p016be'):
        return True
    ```
    ```python
    elif i == 1:
        component = 'U'
        plane_width = width // chroma_width_div
        plane_height = height // chroma_height_div
    ```
    ```python
    # 默认
    return 2 if 'yuv' in lower_fmt else 1
    ```
  - 影响: `_is_yuv_pixel_format` 对 nv12/p010 等返回 True 使 planar 模式开启，但无 target_size 时
    不做 reformat，`_extract_planes_from_frame` 只懂 planar 布局：交错 UV 平面（行内 U/V 交替）被
    按 `width//2` 切片当作 U 平面，得到 UV 交替截断的错乱数据；且半平面帧只有 2 个平面，V 分量
    缺失，下游按 (Y,U,V) 消费会出错或 IndexError。`_get_chroma_width_divisor('nv12')` 走默认分支
    返回 1（应为 2），进一步证明该路径未考虑半平面。CPU 文件流多为 planar 格式，触发面窄，但
    映射表显式声明支持这些格式。
  - 建议: planar 模式排除 semiplanar（返回 False），或提取前 reformat 到对应 planar 格式
    （复用 `_get_planar_format_for_scaling` 的 nv12→yuv420p 映射），并修 divisor 表。
  - 置信度: 中

- [AUD-34705] [Suggestion] probe() 构造的 VideoMetadata 漏传 chroma_siting，与 open 路径元数据不一致
  - 位置: src/shared/media/ffmpeg_source.py:682（对照 2943-2951、2988 的 open 路径）
  - 证据:
    ```python
    self._metadata = VideoMetadata(
        width=width,
        height=height,
        fps=fps,
        total_frames=total_frames,
        codec_name=codec_name,
        codec_profile=codec_profile,
        pixel_format=pix_fmt,
    ```
  - 影响: `_extract_metadata_from_container` 提取并传入 `chroma_siting=chroma_siting`（2988 行），
    probe() 的构造漏掉该字段。先 probe() 后 open() 时 `_metadata` 已缓存（open 仅在 None 时重取，
    1061-1062），chroma_siting 保持 None——GPU 端 4:2:0 色度采样位置信息丢失，两路径元数据不一致。
  - 建议: probe() 同样提取并传入 chroma_siting（逻辑已在 2943-2951 实现，可抽公共函数复用）。
  - 置信度: 高

- [AUD-34706] [Suggestion] _seek_intra seek 后无帧时谎报 current_frame，错误以 End-of-stream 呈现
  - 位置: src/shared/media/ffmpeg_source.py:1435
  - 证据:
    ```python
    except StopIteration:
        pass

    # Seek 后没有帧
    self._current_frame = frame_index
    ```
  - 影响: for 循环耗尽不抛 StopIteration（该 except 为死代码，`_pull_next_frame_from_seek_session`
    1672-1673 同款）。seek 后迭代器无帧时把 `_current_frame` 直接置为目标帧，seek 假装成功；
    随后 read_next_frame 以 StopIteration("End of video stream") 呈现而非 SeekError，错误语义
    误导调用方（把定位失败当作自然结尾）。
  - 建议: 无帧时 raise SeekError(target_frame=frame_index, actual_frame=None)。
  - 置信度: 高

- [AUD-34707] [Suggestion] read_frame_planar/_read_next_frame_planar 缺 P2-1 同步解码断言
  - 位置: src/shared/media/ffmpeg_source.py:2160（对照 read_frame:1177-1185）
  - 证据:
    ```python
    if not self._is_open:
        raise RuntimeError("Media source not open. Call open() first.")

    # 验证帧号范围
    if self._metadata is not None and self._metadata.total_frames is not None:
    ```
    （read_frame/read_next_frame/seek_frame 三个入口均有
    `from src.shared.video.video_playback import _assert_no_sync_decode_in_playback` 防护，
    planar 两个入口没有。）
  - 影响: P2-1「播放 tick 不得同步解码」的违规检测在 planar 读路径缺失，而该路径正被播放解码
    实际使用（edit_gpu_decode_manager.py:2105/2205/2678 调 read_frame_planar），违规调用不会暴露。
  - 建议: 两个 planar 入口补同样的 try-import 断言。
  - 置信度: 高

- [AUD-34708] [Suggestion] alpha 提取辅助函数按原始字节取数且全仓库无调用（死代码）
  - 位置: src/shared/media/ffmpeg_source.py:2746
  - 证据:
    ```python
    # YUVA 格式有 4 个平面：Y, U, V, A
    if len(frame.planes) >= 4:
        alpha_plane = frame.planes[3]
        alpha_data = np.array(alpha_plane, copy=True)
        return alpha_data
    ```
  - 影响: `np.array(VideoPlane)` 得到的是按 line_size 展开的原始字节（一维 uint8），既不裁剪
    stride padding 也不按位深解释（10-bit YUVA 的 alpha 会得到双倍宽度的字节流）。grep 验证
    `_extract_alpha_from_yuva`/`_handle_prores_4444_alpha`/`_get_alpha_plane` 在仓库内仅彼此调用、
    无外部调用方——当前为死代码，一旦被复用将产生错位 alpha。文件内已有正确实现
    `_extract_visible_plane_data`。
  - 建议: 删除这三个死方法，或改用 `_extract_visible_plane_data` 并补测试。
  - 置信度: 高（无调用方已 grep 验证）

- [AUD-34709] [Suggestion] GOP 索引后台线程跨线程写状态无同步，close() 不 join
  - 位置: src/shared/media/ffmpeg_source.py:1527
  - 证据:
    ```python
    self._gop_index_thread = threading.Thread(
        target=_build,
        name=f"gop-index-{Path(self._path).name}",
        daemon=True,
    )
    self._gop_index_thread.start()
    ```
  - 影响: 后台线程写 `self._gop_index`（1523 行），与 seek 路径读取无任何同步；GIL 下属性赋值
    原子、竞态良性（最多晚一次 seek 受益）。close() 不 join 该线程，对象关闭后线程仍可能写
    `self._gop_index` 并落盘缓存——不崩溃但生命周期边界模糊（`_close_gop_seek_session` 的注释
    表明作者对部分构造/析构场景敏感）。
  - 建议: 用 `threading.Event` 标记就绪；close() 可选带超时 join 或在写回前检查关闭标志。
  - 置信度: 中

- [AUD-34710] [Suggestion] output_format 校验失败时 __del__→close() 访问未初始化属性抛 AttributeError
  - 位置: src/shared/media/ffmpeg_source.py:476（对照 1120-1123、3018-3020）
  - 证据:
    ```python
    if output_format not in (None, "rgb24", "rgba"):
        raise ValueError(f"Unsupported forced output format: {output_format!r}")
    ```
    ```python
    # 清理迭代器和缓冲区
    self._frame_iterator = None
    self._decoder_buffer = []
    self._pending_av_frame = None
    ```
  - 影响: ValueError 在 `_frame_iterator` 等属性赋值（486 行起）之前抛出；对象析构时
    `__del__ → close()` 直接访问 `self._frame_iterator` 触发 AttributeError（解释器打印
    "Exception ignored in __del__"）。`_close_gop_seek_session`（1144-1146 注释）已为此用
    getattr 防御，close() 本体没有。
  - 建议: close() 对这些属性同样用 getattr(self, "_frame_iterator", None)，或把参数校验挪到
    任何赋值之前。
  - 置信度: 高

- [AUD-34711] [Suggestion] warm_gop_index 文档承诺失败返回 None，实际异常直接上抛
  - 位置: src/shared/media/ffmpeg_source.py:1540
  - 证据:
    ```python
    Returns:
        就绪的索引；失败返回 None。
    """
    built = get_or_build_gop_index(self._path, force=force)
    if built is not None:
        self._gop_index = built
    ```
  - 影响: docstring 承诺"失败返回 None"，但 `get_or_build_gop_index` 抛出的异常（IO/解析错误）
    会直接上抛给调用方，与 `_ensure_gop_index` 的全吞策略（1511、1524）行为不一致，测试/预热
    调用方易被文档误导。
  - 建议: 捕获异常记 warning 后返回 None，或修正 docstring 明示会抛。
  - 置信度: 高

- [AUD-34712] [Suggestion] 最终兜底 to_ndarray 对 planar 源取 shape[:2] 得到错误宽高
  - 位置: src/shared/media/ffmpeg_source.py:1938（另见 1946-1948）
  - 证据:
    ```python
    try:
        data = frame.to_ndarray()
        target_format = src_fmt
        output_width = data.shape[1] if data.ndim >= 2 else self._metadata.width
        output_height = data.shape[0] if data.ndim >= 1 else self._metadata.height
    ```
    ```python
    # 确定数据类型和尺寸
    dtype = data.dtype
    height, width = data.shape[:2]
    ```
  - 影响: 全部 reformat 失败后的兜底路径中，planar 源经 PyAV `to_ndarray` 得到形状
    (1, H', W)（PyAV 对 planar 帧的文档化行为），`data.shape[:2]` 取出 (1, H')，
    `data.shape[1]` 把 H' 当宽——DecodedFrame 宽高颠倒/失真（height=1）。仅极冷门格式会走到
    此路径，但一旦走到下游尺寸全错。
  - 建议: 兜底路径用 frame.width/frame.height，或按 ndim 分派形状解析。
  - 置信度: 中（取决于 PyAV to_ndarray 对 planar 的 (1,H,W) 形状，此为文档化行为）

- [AUD-34713] [Suggestion] 类型注解不精确与残留死 import
  - 位置: src/shared/media/ffmpeg_source.py:2006（另见 748、2991-2995）
  - 证据:
    ```python
    @staticmethod
    def _get_reformat_fallback_chain(source_pix_fmt: str, target_format: str) -> list[tuple[str, ...]]:
    ```
    ```python
    import av
    ```
    （`_extract_fps` 函数体内 748 行，该函数未使用 `av`。）
  - 影响: 返回值元素恒为二元组 `(inter_fmt, final_fmt)`，注解应为 `list[tuple[str, str]]`，
    变长元组注解误导调用方；`_extract_fps` 内 `import av` 是死代码（每次调用多做一次模块查找）；
    `read_audio_samples -> Optional[Any]` 等于无注解。
  - 建议: 修正返回注解、删除死 import、预留接口改为明确的占位类型或注释说明。
  - 置信度: 高

### 通过项
- 信号防递归：本文件无 Qt 控件/信号，不适用
- 状态提交一致性：无 undo/revision 状态；GOP 索引持久化由 gop_index 模块负责，本文件懒加载/后台构建入口行为正确
- GPU 资源生命周期：CPU 解码路径无 OpenGL 资源；hw 帧有 `_transfer_hw_frame_if_needed` 防御性下载；主/seek 会话容器关闭均有 try/except/finally 防护
- GLSL·HDR 正确性（packed 路径）：按位深输出 rgb48le/rgba64le 保留高位深，未对 HDR 数据做无理由 clamp；`_safe_stream_pix_fmt` 正确规避 PyAV 17 对 AV_PIX_FMT_NONE 的段崩溃
- 异常与取消路径：probe finally 关闭容器；open 失败显式 close() 释放句柄；MediaOpenError/MediaDecodeError/SeekError 分类清晰；BRAW/R3D 缺解码器有专门提示
- 输入验证：output_format 白名单校验、frame_index 负数与上限校验、check_ffmpeg_available 前置检查齐全
- 性能模式：GOP 索引后台构建不阻塞首次 seek；decode-forward walk 上界由真实 keyframe 距离收紧；SLICE/AUTO 线程策略按 codec 实测数据选择并留有注释依据
