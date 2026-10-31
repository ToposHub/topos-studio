# 批次报告：batch-391 [P2] src/shared/video//

编号区间：AUD-39100–AUD-39199
审查文件：`src/shared/video/frame_provider_service.py`（2,376 行，全文通读）
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解

---

## src/shared/video/frame_provider_service.py (行数: 2376)

### 发现

- [AUD-39100] [Critical] 关闭剪辑会话不持有 decode_lock 释放媒体源，与在途解码构成 use-after-close 竞态
  - 位置: src/shared/video/frame_provider_service.py:1230、2043-2073（对照 2118-2122）
  - 证据:
    ```python
    # close_clip（锁外）：
    # 在锁外关闭媒体源
    self._close_clip_sources(session)

    # _close_clip_sources：
    if session.master_source is not None:
        try:
            session.master_source.close()
        ...
    # _decode_frame：
    with session.decode_lock:
        frame = source.read_frame(frame_index)
    ```
  - 影响: `_close_clip_sources` 关闭 master/proxy/disk_proxy 源时不获取 `session.decode_lock`（仅 vt_source 用 vt_lock）。渲染线程/prefetch 正在 `decode_lock` 内执行 `source.read_frame()` 时，UI 线程调用 `close_clip`/`shutdown`（1230-1231、2332-2333）或 `update_disk_proxy_info`（1047-1054 锁外 close、1023 持 self._lock 但不持 decode_lock close proxy_source）会并发释放 PyAV 解码器/容器，C 层 use-after-free 可致进程崩溃。decode_lock 的注释（2119-2120）明确"必须串行化"，但 close 路径游离在该协议之外。
  - 建议: `_close_clip_sources` 关闭 master/proxy/disk_proxy 源前获取 `session.decode_lock`；或在 close 前等待在途解码完成（decode_waiters 判定）。关闭路径与解码路径必须共享同一把锁协议。
  - 置信度: 高（竞态窗口由代码结构直接证明；C 层崩溃后果依赖 PyAV 实现，但属公认风险）

- [AUD-39101] [Major] AUTO 质量缓存桶错位：proxy 源解码的帧写入 master 缓存，被后续 MASTER 请求命中
  - 位置: src/shared/video/frame_provider_service.py:388-391、407-410（对照 291-294、140）
  - 证据:
    ```python
    # get_cached_frame / cache_frame：
    if quality == FrameQuality.PROXY:
        return self.frame_cache_proxy.get(frame_index)
    else:
        return self.frame_cache_master.get(frame_index)

    # get_source_for_quality（AUTO, preview 模式）：
    if self.playback_mode in ('paused', 'export'):
        return self.master_source
    else:
        return self.get_active_proxy_source() or self.master_source
    ```
  - 影响: AUTO 质量（preview 模式）从 proxy 源解码低分辨率帧，但 `cache_frame` 把一切非 PROXY 质量写入 `frame_cache_master`。此后 `quality='master'`（暂停/导出）请求同帧会命中该缓存，拿到 proxy 分辨率帧——渲染分辨率错误。另外 `from_string` 把未知字符串静默映射为 AUTO（140 行 `mapping.get(quality_str.lower(), FrameQuality.AUTO)`），输入拼写错误不报错即触发此路径。当前未发现使用 `'auto'` 的调用方，属潜在缺陷。
  - 建议: 在 `get_cached_frame`/`cache_frame`/`evict_old_frames` 中先解析 AUTO 为实际目标质量（按 playback_mode 决定 PROXY/MASTER 桶），或禁止 AUTO 写缓存；`from_string` 对未知值记 warning。
  - 置信度: 高（逻辑链确定；触发依赖 AUTO/错误字符串输入）

- [AUD-39102] [Major] open_clip 打开失败清理只移除映射，不释放已打开的源 → master/VT 源泄漏
  - 位置: src/shared/video/frame_provider_service.py:851-867（对照 1833、1842、1904-1911、1924）
  - 证据:
    ```python
    except Exception as e:
        # 打开失败，清理
        with self._lock:
            self._clips.pop(clip_id, None)
            if path in self._path_to_clips:
                self._path_to_clips[path] = [ ... ]
        from ..media.source import MediaOpenError
        raise MediaOpenError(...) from e
    ```
  - 影响: `_open_clip_sources` 在 1833 行已给 `session.master_source` 赋值、1924 行可能已附加 `vt_source` 之后，若内存 proxy 打开失败（1904-1911 `open_source` 抛出）、`validate_disk_proxy` 抛出（1842 行未包 try）或 1934 行 `metadata.fps.to_fps()` 在 fps 为 None 时抛 AttributeError，异常传到 `open_clip` 的 except 只从字典移除 session 并重新抛出，从不调用 `_close_clip_sources` → 已打开的 master 源（文件句柄/解码器）与 VT 源泄漏。`open_clip_with_disk_proxy`（953-969）同样。
  - 建议: 失败清理路径调用 `self._close_clip_sources(session)` 后再抛 MediaOpenError。
  - 置信度: 高

- [AUD-39103] [Major] 并发 open_clip 共享路径在源打开完成前返回 clip_id，打开失败时吞掉引用并留下死 clip_id
  - 位置: src/shared/video/frame_provider_service.py:753-767（对照 779、831、851-861）
  - 证据:
    ```python
    with self._lock:
        if share_by_path and path in self._path_to_clips:
            existing_clip_ids = self._path_to_clips[path]
            if existing_clip_ids:
                clip_id = existing_clip_ids[0]
                session = self._clips[clip_id]
                session.add_ref()
                ...
                return clip_id
        ...
        self._clips[clip_id] = session   # 源尚未打开即入表
    # 在锁外打开媒体源（避免长时间阻塞）
    try:
        self._open_clip_sources(session)
    ```
  - 影响: session 在源打开前就加入 `_clips`。线程 A 创建 session 后锁外缓慢打开；线程 B 同路径 open_clip 在锁内看到 existing → add_ref 并立即返回，但此时 `is_open=False`；若线程 A 打开失败，except 无视 ref_count>0 直接 pop 会话，线程 B 持有的 clip_id 永久失效（后续 request_frame 得 "Clip not found"），其 add_ref 的引用计数随对象一起丢失。
  - 建议: 会话入表时增加 `opening` 状态，B 等待打开完成（Event）后再返回；或失败清理时仅当 ref_count 归零才移除并通知等待方。
  - 置信度: 高

- [AUD-39104] [Major] request_gpu_frame 的 CPU 回退不转发 allow_master_fallback，播放热路径可被同步母版解码拖死
  - 位置: src/shared/video/frame_provider_service.py:1472-1477（对照 1274-1294）
  - 证据:
    ```python
    # CPU fallback
    frame = self.request_frame(
        clip_id=clip_id,
        frame_index=frame_index,
        quality=quality,
    )
    ```
  - 影响: `request_frame` 的契约是"实时播放热路径必须传 allow_master_fallback=False（规格书 §6 / Stage C2），避免 proxy miss 时同步解母版拖死播放线程"。`request_gpu_frame` 未暴露该参数，CPU 回退固定走默认 True。实际调用方 `src/features/edit/core/timeline_clip_frame_provider.py:951` 在播放热路径 `request_gpu_frame(vid_clip_id, source_frame, quality='proxy')`（同文件 994 行的 CPU 直连路径正确传了 False），其 VT miss 后的 CPU 回退即违反规格：proxy 不可用时同步解 4K 母版阻塞渲染线程。
  - 建议: `request_gpu_frame` 增加 `allow_master_fallback` 参数并透传给 CPU 回退的 `request_frame`；交互式（interactive=True）路径默认 False。
  - 置信度: 高（调用方证据确凿）

- [AUD-39105] [Major] 导出路径 request_metal_frame 优先使用 VT 源，违反同文件"export 恒走 CPU 全质量"策略，ProRes 色度下采样可进导出
  - 位置: src/shared/video/frame_provider_service.py:1485-1514（对照 1968-1973）
  - 证据:
    ```python
    # request_metal_frame（自称 export-oriented）：
    vt_source = session.vt_source
    if vt_source is not None:
        with session.vt_lock:
            gpu_frame = vt_source.read_frame_metal(int(frame_index))

    # _try_open_vt_source 注释：
    # 质量取舍：VT 输出 p010（4:2:0 10bit），预览色度从 4:2:2 下采样；
    # export 恒走 CPU 4:2:2 planar 全质量路径（VideoFrameNode is_export
    # 不请求 VT），导出零妥协。
    ```
  - 影响: `open_clip`（share_by_path=False 也走 `_open_clip_sources`）会对 ProRes/H.264/HEVC 附加 VT 源。`timeline_export.py:9587-9600` 的 `_request_video_metal_frame` 经全局服务 open_clip 后调用 `request_metal_frame(clip_id, frame, quality="master")`，它首先命中 `vt_source.read_frame_metal` → ProRes 4:2:2 素材的导出帧来自 VT 的 4:2:0 色度下采样输出，与本文件写明的"导出零妥协"不变量直接矛盾（导出色度质量受损）。
  - 建议: `request_metal_frame` 对 export 语义跳过 VT 源（或按 codec/pix_fmt 白名单只对本来就 4:2:0 的素材走 VT）；至少在文件内统一注释与行为。
  - 置信度: 中（本文件内路径与策略矛盾确证；最终像元格式取决于 VT 会话配置，未逐帧验证）

- [AUD-39106] [Major] update_disk_proxy_info/_create_memory_proxy_source 对可能已被关闭的会话赋新源，新源永不释放
  - 位置: src/shared/video/frame_provider_service.py:1002-1060、1086-1111
  - 证据:
    ```python
    with self._lock:
        session = self._clips.get(clip_id)
        ...
        session.disk_proxy_info = disk_proxy_info
    # 处理状态变化（锁外继续使用 session）
    if disk_proxy_info.is_ready and session.disk_proxy_source is None:
        ...
        with self._lock:
            session.disk_proxy_source = disk_proxy_source

    # _create_memory_proxy_source：
    with self._lock:
        session.proxy_source = proxy_source   # 未校验 session 仍在 _clips
    ```
  - 影响: 锁释放后继续读写 session 字段；若 `close_clip` 在此期间已 pop 并关闭该会话，新打开的 disk_proxy_source/proxy_source 被赋到已死对象上，永远不会被 close → 文件句柄/解码器泄漏。对照 `_schedule_deferred_memory_proxy_source` 的 worker（1152-1158 重查 `_clips` 且校验 master_source）做了正确防护，此处缺失。
  - 建议: 赋值前在同一锁内校验 `self._clips.get(clip_id) is session`，否则立即关闭新建的源。
  - 置信度: 高

- [AUD-39107] [Major] _on_proxy_complete 持有 self._lock 执行媒体源打开（阻塞 I/O），且跳过磁盘代理校验、不关闭被替换的内存代理
  - 位置: src/shared/video/frame_provider_service.py:2231-2275（对照 1005-1017、1842-1872、1022-1024）
  - 证据:
    ```python
    with self._lock:
        ...
        for clip_id in clip_ids:
            ...
            if disk_proxy_info.is_ready and session.disk_proxy_source is None:
                if disk_proxy_info.proxy_path:
                    try:
                        from ..media.factory import MediaSourceFactory
                        disk_proxy_source = MediaSourceFactory.create(
                            path=disk_proxy_info.proxy_path, ...)
                        MediaSourceFactory.open_source(disk_proxy_source)
                        session.disk_proxy_source = disk_proxy_source
    ```
  - 影响: (1) `create+open_source`（文件打开/probe 级阻塞 I/O）在服务锁内执行，期间所有 `request_frame`/`open_clip`/`close_clip`（均需 self._lock）被卡一次文件打开时长——该回调来自后台代理生成线程，直接把播放热路径的锁等排队拉长；对照 `update_disk_proxy_info` 特意在锁外打开。(2) 此路径不做 `validate_disk_proxy`/`get_disk_proxy_rejection_reason` 校验（对照 `_open_clip_sources` 1842-1872 的实时播放拒绝逻辑），分辨率不匹配的坏代理可被直接挂上播放路径。(3) 切换到磁盘代理后不关闭 `session.proxy_source`（对照 1022-1024）。
  - 建议: 复用 `update_disk_proxy_info` 的锁外打开+校验流程，删除 `_on_proxy_complete` 内的重复实现。
  - 置信度: 高

- [AUD-39108] [Suggestion] 全局统计计数无锁递增，并发下丢失更新
  - 位置: src/shared/video/frame_provider_service.py:832、1232、1330-1331、1357、1379-1381
  - 证据:
    ```python
    self._total_cache_hits += 1
    self._total_frames_served += 1
    ...
    self._total_cache_misses += 1
    ```
  - 影响: `+=` 非原子，播放线程与导出线程并发请求时计数丢失，`cache_hit_rate` 失真（仅诊断数据受损，不影响帧数据）。
  - 建议: 用 ` itertools.count`/`threading.Lock` 或在已持有的锁段内更新；或改用每线程累计。
  - 置信度: 高

- [AUD-39109] [Suggestion] has_vt_source/request_gpu_frame/request_metal_frame 未持 self._lock 访问 _clips，且不检查 is_open
  - 位置: src/shared/video/frame_provider_service.py:1397、1434、1499
  - 证据:
    ```python
    def has_vt_source(self, clip_id: str) -> bool:
        session = self._clips.get(clip_id)
        return session is not None and session.vt_source is not None
    ...
    session = self._clips.get(clip_id)   # request_gpu_frame / request_metal_frame
    ```
  - 影响: 本文件其余方法均以 self._lock 保护 `_clips`，这三处锁外 `dict.get` 破坏锁纪律；快照的 `vt_source` 可能在使用前被 close 置 None（异常被捕获、优雅回退，故仅一致性风险）。CPython GIL 下单次 dict.get 原子，无崩溃风险。
  - 建议: 统一改为 `with self._lock:` 快照 session 引用与必要字段。
  - 置信度: 高

- [AUD-39110] [Suggestion] _decode_frame 将越界帧号静默钳制，调用方映射错误被掩盖
  - 位置: src/shared/video/frame_provider_service.py:2107-2110
  - 证据:
    ```python
    # Clamp 帧号
    total_frames = session.metadata.total_frames if session.metadata else 0
    if total_frames > 0:
        frame_index = max(0, min(frame_index, total_frames - 1))
    ```
  - 影响: 请求负帧号被钳到 0、超界帧号被钳到末帧并正常返回，而非 None——上层时间轴→源帧映射的边界 bug（如变速/循环播放越界）表现为"显示错误帧"而非可检测的失败，且无任何日志。
  - 建议: 越界时记 warning 日志；或对显著越界（如负值）返回 None 让上层走 missing/hold 路径。
  - 置信度: 中

- [AUD-39111] [Suggestion] prefetch 实为同步执行且范围期间缓存无界增长，docstring 与行为不符
  - 位置: src/shared/video/frame_provider_service.py:1569-1636
  - 证据:
    ```python
    """
    预取帧范围。
    在后台预解码帧，为后续请求做准备。
    """
    ...
    for frame_idx in frames_to_prefetch:
        ...
        with session.decode_lock:
            ...
            frame = self._decode_frame(session, frame_idx, quality_enum)
            if frame is not None:
                session.cache_frame(frame_idx, frame, quality_enum)

    center_frame = (start + end) // 2
    with session.decode_lock:
        session.evict_old_frames(center_frame, quality_enum)
    ```
  - 影响: 循环在调用线程内同步逐帧解码；若被 UI/渲染线程直接调用（docstring 声称"后台"会诱导这种用法）将冻结该线程。evict 仅在整个范围结束后执行一次，大范围（如数百帧 master）预取期间缓存增长到范围大小，4K master 帧造成内存尖峰。
  - 建议: docstring 改为明确"调用方须在工作线程执行"；evict 移入循环内（同一 decode_lock 下安全）按 max_*cache 滚动淘汰。
  - 置信度: 中

- [AUD-39112] [Suggestion] 类型注解缺失/不准确（request_gpu_frame、request_metal_frame、request_frame_window）
  - 位置: src/shared/video/frame_provider_service.py:1400-1410、1485-1490、1642-1652
  - 证据:
    ```python
    def request_gpu_frame(
        self,
        clip_id: str,
        frame_index: int,
        quality: str = "proxy",
        *,
        interactive: bool = False,
        deadline_s: Optional[float] = None,
        is_stale: Optional[callable] = None,
        coarse: bool = False,
    ):
    ```
  - 影响: `request_gpu_frame`/`request_metal_frame`/`request_frame_window` 均无返回类型注解（实际返回 GpuDecodedFrame | DecodedFrame | None / FrameWindow）；`is_stale: Optional[callable]` 使用小写内建 `callable` 而非 `Callable[[], bool]`，类型检查器无法校验签名。
  - 建议: 补返回注解（TYPE_CHECKING 下引用 GpuDecodedFrame/FrameWindow），`is_stale` 改为 `Optional[Callable[[], bool]]`。
  - 置信度: 高

### 通过项
- GLSL·HDR 正确性：本文件无 GLSL；帧数据全程原样传递（planar/HDR 无 clamp(0.0,1.0)、无像素处理），通过
- 信号防递归：无 Qt 信号/控件程序化更新，不适用
- 状态提交一致性（undo/redo/revision/持久化）：不参与撤销栈与持久化，clip 生命周期由显式 open/close/ref_count 管理，逻辑自洽，通过
- request_frame 解码合并正确：decode_lock 内复查缓存，并发同帧 miss 合并为一次解码（1341-1376）
- 前台让路机制正确：decode_waiters 计数让 prefetch 在前台等待时终止本轮扫描（1341-1342、1617-1623）
- 快路径无锁读的安全性有据：get_cached_frame 纯 dict.get，GIL 下原子（1323-1328 注释论证）
- VT 源 close 持 vt_lock，与 VT 读路径（request_gpu_frame/request_metal_frame/prefetch_vt_sequential）互斥（2067-2073）
- defer memory proxy 后台线程对会话消亡防护正确：重查 _clips、finally 关闭未采用的新源、复位 pending 标志（1123-1185）
- prefetch 每帧持 decode_lock 复查缓存，避免与前台请求重复解码（1624-1629）
- 共享源设计（同路径复用、share_by_path=False 独立游标）与导出 LRU 会话（调用方）配合正确
- 异常路径总体不吞错：open/decode 失败均转为 MediaOpenError/None + 日志，无宽泛 except 掩盖领域错误的实例（能力探测类 except 均有回退语义）
