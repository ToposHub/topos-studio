# 批次报告：batch-345 — src/shared/media/

- 审查日期：2026-09-28
- 编号区间：AUD-34500–AUD-34599
- 文件数：4（2,545 行）
- 交叉验证参考（只读）：`src/shared/media/proxy.py`、`src/shared/media/proxy_manager.py`、`src/shared/media/factory.py`、`src/shared/media/source.py`、`src/shared/media/ffmpeg_source.py`、`src/shared/media/__init__.py`、`src/shared/video/frame_provider_service.py`、`src/features/edit/ui/media_pool_panel.py`

---

## src/shared/media/decode_queue.py (行数: 621)

模块级背景：`DecodeQueue` 在 `src/shared/media/__init__.py:224/422` 作为公共 API 导出，但仓库内未发现活跃调用方（仅 `topos_image_sequence.py:340` 注释提及接线）。以下缺陷当前为潜伏态，严重度按公共 API 契约评定。

### 发现

- [AUD-34500] [Major] seek() 与预读线程竞态：清空已解码队列后，正在解码的旧帧会重新入队
  - 位置: src/shared/media/decode_queue.py:297-312（清队）、546-560（解码后入队）、584-596（弹出不回插）
  - 证据:
    ```python
    with self._request_lock:
        # 取消所有未完成的请求
        for request in self._requests:
            if not request.cancelled:
                request.cancel()
        ...
        # 清空已解码帧队列
        self._clear_decoded_queue()
    ```
    ```python
    # 执行解码
    try:
        frame = self._source.read_frame(request.frame_index)
        ...
        try:
            self._decoded_queue.put(entry, timeout=0.1)
    ```
  - 影响: 预读线程在 `_pop_next_request` 中已将请求移出 `_requests` 后，seek 的取消循环再也触及不到它；该请求解码完成后，旧位置帧被 put 进刚被 seek 清空的 `_decoded_queue`。结合 `get_frame` 的"不匹配放回队尾"逻辑（AUD-34502），seek 后旧帧滞留队列反复循环，可占满 maxsize=64 的队列，触发 `frames_dropped` 丢帧并使预读循环反复等待重插。`stop()` 超时后线程仍在解码时同样存在"清队后再写入"的残留帧问题（decode_queue.py:216-235）。
  - 建议: 为每帧请求携带 generation/epoch 号（seek 时自增），解码完成入队前校验 epoch，过期帧直接丢弃；或对 in-flight 请求加"已弹出待解码"登记表，seek 时一并标记取消。
  - 置信度: 高

- [AUD-34501] [Major] request_frame 返回的请求句柄可能不在队列中，cancel_request 对其无效
  - 位置: src/shared/media/decode_queue.py:451-456、266-284、388-398
  - 证据:
    ```python
    # 检查是否已有相同帧号的请求
    for existing in self._requests:
        if existing.frame_index == request.frame_index and not existing.cancelled:
            # 已存在，更新优先级
            existing.priority = min(existing.priority, request.priority)
            return
    ```
  - 影响: 对同一帧号重复 `request_frame`（播放中极常见）时，新建的 `FrameRequest` 被静默丢弃，返回给调用方的是另一个不在队列里的对象。调用方随后 `request.cancel()` / `cancel_request(request)` 只置位了孤儿对象的 `cancelled` 标志，队列中真正待解码的请求不受影响，取消静默失效；request_id 计数与 `__repr__` 状态也随之失真。
  - 建议: 去重路径应返回队列中既有的 `existing` 对象（更新其优先级后），或在 `FrameRequest` 上增加"已合并/失效"标志并让 `cancel_request` 按帧号在队列中定位目标。
  - 置信度: 高

- [AUD-34502] [Suggestion] get_frame 帧号不匹配时将条目放回队尾并返回 None，打乱队列次序且行为未在文档说明
  - 位置: src/shared/media/decode_queue.py:339-346（get_frame_entry 在 375-381 重复同一逻辑）
  - 证据:
    ```python
    # 检查是否为期望的帧
    if frame_index is not None and entry.frame_index != frame_index:
        # 不是期望的帧，放回队列
        try:
            self._decoded_queue.put(entry, block=False)
        except queue.Full:
            # 队列满，丢弃
            self._stats['frames_dropped'] += 1
        return None
    ```
  - 影响: 期望帧在队列深处时，调用方每次只消费一个不匹配条目并拿到 None，需多次重试且每轮重排队列（优先级/时间序信息丢失）；队满时未过期帧被永久丢弃并计入 `frames_dropped`。docstring（329-330 行）只写"队列为空或超时"返回 None，未提不匹配也返回 None。两处实现完全重复。
  - 建议: 改为在 `_request_lock` 下对 `_decoded_queue._queue` 做原地扫描取出目标条目（或维护 dict[frame_index, entry] 旁路缓存）；至少在文档补充不匹配语义，并抽取 get_frame/get_frame_entry 的公共实现。
  - 置信度: 高

- [AUD-34503] [Suggestion] 解码失败以 None 哨兵入队：错误与"暂无帧"不可区分，且可能占满队列
  - 位置: src/shared/media/decode_queue.py:571-580、335-336/371-372
  - 证据:
    ```python
    except Exception as e:
        logger.error(
            f"DecodeQueue: 解码失败 frame={request.frame_index}, "
            f"error={e}"
        )
        # 解码失败，放入 None 表示错误
        try:
            self._decoded_queue.put(None, timeout=0.1)
        except queue.Full:
            pass
    ```
  - 影响: 调用方无法区分"解码失败"与"队列为空/超时"（都返回 None），无法走 `MediaErrorHandler` 差异化提示；持续失败时 None 哨兵消耗 maxsize=64 的队列槽位，阻塞后续正常帧解码。`except Exception` 宽泛捕获也违反项目"不得宽泛 except 掩盖领域错误"铁律（此处仅 log，未区分 DecodeError 等领域异常类型）。
  - 建议: 入队一个携带异常对象的错误条目（如 `DecodedFrameEntry(frame=None, error=exc)`）或在 DecodeQueue 上提供失败回调/状态查询；except 至少分别处理 `MediaDecodeError` 与意外异常。
  - 置信度: 高

- [AUD-34504] [Suggestion] probe() 失败时 total_frames=0，预读上界不受总帧数约束
  - 位置: src/shared/media/decode_queue.py:477-489
  - 证据:
    ```python
    try:
        metadata = self._source.probe()
        total_frames = metadata.total_frames or 0
    except Exception:
        total_frames = 0
    ...
        end_frame = frame_index + self._window_size
        if total_frames > 0:
            end_frame = min(end_frame, total_frames - 1)
    ```
  - 影响: probe 失败或 total_frames 未知时，`end_frame` 可越过片尾，窗口内每个越界请求都会触发 read_frame 异常 → 叠加 AUD-34503 的 None 洪泛。另外 `probe()` 在调用方线程执行，与预读线程的 `read_frame` 共享同一 MediaSource；FFmpeg 后端 probe 有缓存（ffmpeg_source.py:594-595）故窗口很小，但首次未缓存时的并发访问无锁保护。
  - 建议: probe 失败时收敛窗口（如仅当前帧 ±3）；把 probe 结果缓存到 DecodeQueue 自身，避免每次 request_frame 都触碰 source；为 source 访问加队列级串行化说明。
  - 置信度: 中

- [AUD-34505] [Suggestion] _stats 计数器在预读线程无锁自增，与 get_stats 的加锁读取不一致
  - 位置: src/shared/media/decode_queue.py:561、568、578（无锁写）；408-414（加锁读）
  - 证据:
    ```python
    try:
        self._decoded_queue.put(entry, timeout=0.1)
        self._stats['frames_decoded'] += 1
    ```
    对比 get_stats：
    ```python
    with self._request_lock:
        return {
            **self._stats,
    ```
  - 影响: `dict[key] += 1` 是多字节码非原子操作，预读线程与 UI/调用方线程并发时统计可能丢失更新；不影响主数据结构正确性，仅统计漂移。
  - 建议: 所有 `_stats` 写入统一置于 `_request_lock` 内（或改用 `collections.Counter` + 锁）。
  - 置信度: 中

- [AUD-34506] [Suggestion] 原位下调既有请求优先级后队列不重排；dataclass(order=True) 首比较字段是 frame_index 而非 priority
  - 位置: src/shared/media/decode_queue.py:455、71-92
  - 证据:
    ```python
                existing.priority = min(existing.priority, request.priority)
                return
    ```
    ```python
    @dataclass(order=True)
    class FrameRequest:
        frame_index: int
        is_master: bool = False
        priority: int = field(default=FramePriority.NORMAL.value)
    ```
  - 影响: `existing.priority` 变小后仍留在原位，破坏"按 priority 有序"的列表不变式，后续 `_insert_request_sorted` 的二分式插入位置随之错乱，调度降级为近似有序。另外 order=True 生成的比较序是 (frame_index, is_master, priority, ...)，与文档"priority 越小优先级越高"的排序语义不符——当前代码未用 sorted() 故未触发，属于埋雷。
  - 建议: 更新优先级后将 existing 移除并重新插入（或直接用 heapq / priority 队列）；若保留 dataclass 排序，把 priority 调整为首字段并 `compare=False` 排除 frame_index。
  - 置信度: 中

### 通过项
- GPU 资源生命周期：模块不持有 ModernGL/纹理/缓冲对象，无 GPU 资源泄漏面。
- GLSL·HDR 正确性：无着色器代码；帧数据以 DecodedFrame 透传，无 clamp(0.0,1.0)。
- 信号防递归：无 Qt 信号，纯 threading/queue 原语，无递归风险。
- 状态提交一致性：无 undo/redo/revision/持久化职责。
- 输入验证（部分）：帧号越界由 MediaSource.read_frame 层校验（ffmpeg_source.py:1191-1196 抛出越界错误），负帧号经错误路径转为 None 哨兵（见 AUD-34503 的错误不可区分问题）。
- 线程安全（主结构）：`_requests` 的增删查均在 `_request_lock` 内；`_decoded_queue` 为线程安全 queue.Queue。
- 类型注解：公共方法均有完整注解（numpy/DecodedFrame 经 TYPE_CHECKING 引入）。

---

## src/shared/media/disk_proxy_service.py (行数: 887)

模块级背景：本服务为活跃代码，`src/features/edit/ui/timeline_panel.py`、`media_pool_panel.py`、`scene_cut_dialog.py` 均通过 `get_disk_proxy_service()` 使用。

### 发现

- [AUD-34507] [Major] get_disk_proxy_info 缓存后永不复验，docstring 承诺的 STALE（源文件已修改）检测未实现
  - 位置: src/shared/media/disk_proxy_service.py:246-306（尤其 263-267 与 306）
  - 证据:
    ```python
        with self._lock:
            info = self._info_cache.get(source_path)

            # 如果没有缓存信息，检查代理是否已存在
            if info is None:
                proxy_path = self._manager.get_valid_proxy_path(source_path)
    ```
    ```python
                else:
                    # 没有代理
                    info = DiskProxyInfo(status=ProxyStatus.MISSING)
                    self._info_cache[source_path] = info

            return info
    ```
    docstring 承诺（259-261 行）："如果代理过期（源文件修改），返回 STALE 状态"。
  - 影响: 一旦条目进入 `_info_cache`，后续调用无条件原样返回——`get_valid_proxy_path` 的 mtime/size 有效性校验（仅缓存未命中时走）不再执行。源文件在外部被修改/替换后，服务持续返回 READY，时间线/媒体池继续用过期代理播放（画面与源不一致），`generate_proxy` 也因 `info.is_ready` 直接短路不会重新生成。`update_config()`（789-806 行）同样不清缓存，配置变更后旧代理仍按 READY 返回。
  - 建议: 缓存命中时用 `get_source_file_info(source_path)` 与 `info.source_mtime/source_size` 比对，不一致降级 STALE 并清缓存；`update_config` 后将现有 READY 条目标记待复验。
  - 置信度: 高

- [AUD-34508] [Major] 磁盘发现路径用硬编码 (3840, 2160) 推算 proxy_resolution，非 4K 源得到错误分辨率
  - 位置: src/shared/media/disk_proxy_service.py:276-279
  - 证据:
    ```python
                        # 探测代理分辨率（简化处理，假设与配置一致）
                        proxy_res = self._config.resolution.calculate_size(
                            (3840, 2160)  # 假设 4K，实际需要探测
                        )
    ```
  - 影响: 对 1080p、竖屏或任意非 4K 源，探测出的 `proxy_resolution` 完全错误（如 1080p 源被记为 HALF(4K)=1920x1080，恰好翻倍）；该错误信息写入 `_info_cache` 并经 `has_ready_proxy`/状态回调分发给 UI 与 `frame_provider_service`（后者在 frame_provider_service.py:2133-2136 将 proxy_resolution 附加到帧上）。
  - 建议: 从源文件 metadata（probe）或代理文件本身探测分辨率；探测失败时置 None 而非虚构值。
  - 置信度: 高

- [AUD-34509] [Major] GENERATING 分支把 on_progress 挂到全局回调列表且永不移除（泄漏+跨文件串扰），on_complete/on_error 被静默丢弃
  - 位置: src/shared/media/disk_proxy_service.py:362-368；分发侧 737-744
  - 证据:
    ```python
        # 检查是否正在生成
        if info.status == ProxyStatus.GENERATING:
            logger.info(f"Proxy already generating: {source_path}")
            # 可以添加进度回调
            if on_progress:
                self._progress_callbacks.append(on_progress)
            return None
    ```
  - 影响: (1) 回调泄漏——每次对生成中文件调用 generate_proxy 都向 `_progress_callbacks` 追加且无任何移除路径（也没有 unregister_progress_callback 的对应调用约定），闭包及其引用的 UI 对象被服务永久持有；(2) 串扰——`_dispatch_progress_callbacks` 不按 source_path 过滤，该回调会收到所有文件的进度事件；(3) 该分支下 on_complete/on_error 均不执行也不传递给任务，调用方永远收不到完成/失败通知（docstring 348-353 行却把三个回调描述为有效参数）。
  - 建议: 仿 `_on_generation_progress` 把 on_progress 绑定到具体 source_path 的分发（或按 path 建立回调注册表，完成/失败/取消时清理）；当前调用方（media_pool_panel.py:10072）未传回调，但公共 API 契约已破。
  - 置信度: 高

- [AUD-34510] [Suggestion] generate_proxy 的 GENERATING 检查与置位之间存在 TOCTOU 竞态
  - 位置: src/shared/media/disk_proxy_service.py:354-372
  - 证据:
    ```python
        # 检查是否已有就绪的代理
        info = self.get_disk_proxy_info(source_path)
        if info.is_ready:
            ...
        # 检查是否正在生成
        if info.status == ProxyStatus.GENERATING:
            ...
        # 更新状态为 GENERATING
        with self._lock:
            self._info_cache[source_path] = info.with_status(ProxyStatus.GENERATING)
    ```
  - 影响: 两个线程可同时通过检查并先后置位/入队。`ProxyManager.queue_proxy_generation` 的 `_source_to_task` 去重（proxy_manager.py:229-235）会阻止重复生成，但第二个任务的回调被静默丢弃并返回既有 task_id——语义上"成功"却无回调可达。
  - 建议: 将"检查-置位-入队"整体移入单次加锁临界区，或依赖 manager 去重返回值统一处理回调挂接。
  - 置信度: 中

- [AUD-34511] [Suggestion] _on_generation_complete 的 cancelled/else 分支为死代码，且 else 分支会丢弃用户错误回调
  - 位置: src/shared/media/disk_proxy_service.py:635-646
  - 证据:
    ```python
        elif result.cancelled:
            # 用户取消
            with self._lock:
                info = self._info_cache.get(source_path)
                if info:
                    self._info_cache[source_path] = info.with_status(ProxyStatus.MISSING)

            logger.info(f"Proxy generation cancelled: {source_path}")

        else:
            # 失败
            self._on_generation_error(source_path, result.error_message, None)
    ```
  - 影响: 经核对 proxy_manager.py:616-632，任务完成时分发逻辑为 success→on_complete、cancelled→无回调、failed→on_error，即本方法只会在 success 时被调用，cancelled/else 分支不可达。若未来 manager 语义变化走到 else 分支，传入的 user_callback=None 会丢弃 generate_proxy 绑定的 on_complete/on_error（当前正确路径由 378-387 行的 lambda 直接绑 on_error，不受影响）。
  - 建议: 删除死分支或在 else 分支传入正确的用户回调；在两模块间用类型/契约注释固定"on_complete 仅 success"的约定。
  - 置信度: 高

- [AUD-34512] [Suggestion] _update_frame_provider_service 跨模块访问另一服务的私有成员 `_lock/_path_to_clips/_clips`
  - 位置: src/shared/media/disk_proxy_service.py:762-783
  - 证据:
    ```python
            # 查找已打开的剪辑
            with service._lock:
                clip_ids = service._path_to_clips.get(source_path, [])
                if clip_ids:
                    clip_id = clip_ids[0]
                    session = service._clips.get(clip_id)
    ```
  - 影响: 破坏封装：frame_provider_service 内部重构（改名/换锁粒度）会让代理更新静默失效——外层 `except Exception`（782-783 行）只记 warning，会掩盖 AttributeError 一类编程错误，与"不得宽泛 except 掩盖领域错误"的约束相悖。
  - 建议: 在 frame_provider_service 上提供公开的 `find_open_clips_by_path(source_path)` / `update_proxy_for_path(...)` API；except 至少收窄为 AttributeError/KeyError 并升级日志。
  - 置信度: 高

- [AUD-34513] [Suggestion] asset_proxy_to_disk_proxy_info 将 source_mtime/source_size 置 0，且默认格式与 topos-first 默认策略不一致
  - 位置: src/shared/media/disk_proxy_service.py:134-148
  - 证据:
    ```python
        proxy_format: Optional[ProxyFormat] = None
        if hasattr(asset, 'proxy_format'):
            proxy_format = getattr(asset, 'proxy_format')
        else:
            # 默认 ProRes Proxy
            proxy_format = ProxyFormat.PRORES_PROXY
    ```
    ```
    ```python
    return DiskProxyInfo(
        status=ProxyStatus.READY,
        ...
        source_mtime=0.0,
        source_size=0,
    )
    ```
  - 影响: (1) mtime/size 为 0 使该 DiskProxyInfo 无法参与任何基于源文件指纹的过期判断（下游若比对必然判"不匹配"或永远判"有效"），docstring 自认"需要实际文件检查（此处设为 0）"；(2) asset 无 proxy_format 时默认 PRORES_PROXY，与 `_proxy_format_from_config`/`get_disk_proxy_service` 的 topos-first 默认（TOPOS_PROXY，822-850、868-872 行）不一致，同一代理在不同入口拿到不同格式口径；(3) 参数 `asset: Any` 类型过宽。
  - 建议: 构造时用 `get_source_file_info(asset.path)` 填充真实 mtime/size；默认格式统一走 `_proxy_format_from_config()`；asset 参数收敛为 Protocol 或具体类型。
  - 置信度: 中（字段消费方依赖下游实现，格式不一致为确定事实）

- [AUD-34514] [Suggestion] get_proxy_stats 在锁外读取统计字段；_total_bytes_saved 把"发现磁盘上已有代理"累计为节省字节
  - 位置: src/shared/media/disk_proxy_service.py:548-579（562 行起在 with 块外取 self._total_generated 等）、290
  - 证据:
    ```python
        with self._lock:
            manager_stats = self._manager.get_stats()
            ...
        return {
            ...
            'service_stats': {
                'total_generated': self._total_generated,
                'total_failed': self._total_failed,
                'total_bytes_saved': self._total_bytes_saved,
            },
    ```
    ```python
                        self._info_cache[source_path] = info
                        self._total_bytes_saved += proxy_size
    ```
  - 影响: 统计字典在释放锁后读取计数，后台完成回调并发写入时数值可能不一致（轻微）；"bytes_saved" 在 get_disk_proxy_info 的磁盘发现路径累计，且 `cleanup_all_proxies` 清零后（536 行）再次发现会重复累计，口径与命名（节省）不符。
  - 建议: 把 return 的取值整体放进临界区；明确 bytes_saved 只在生成完成路径累计，或改名为 total_proxy_size。
  - 置信度: 中

### 通过项
- 线程安全（主体）：`_info_cache`/回调列表/计数的读写均在 `self._lock`（RLock）内（例外见 AUD-34510/34514）。
- GPU 资源生命周期：不涉及 OpenGL/ModernGL 对象。
- 信号防递归：无 Qt 信号，回调分发采用"锁内拷贝列表、锁外调用"的正确模式（716-723、737-744）。
- 取消路径（主体）：cancel_generation 按 source_path 查任务并同步更新缓存状态（411-439）；cancel_all_generations 批量回写 MISSING（441-457）。
- 全局单例：`get_disk_proxy_service`/`reset_disk_proxy_service` 由 `_SERVICE_LOCK` 保护，reset 前取消所有任务（853-887）。
- 异常处理（配置读取）：`_proxy_format_from_config` 的宽泛 except 带 noqa 注明理由并回退默认，符合例外豁免写法（831-835）。
- 类型注解：公共 API 均有注解（asset: Any 除外，见 AUD-34513）。

---

## src/shared/media/error_handling.py (行数: 940)

### 发现

- [AUD-34515] [Suggestion] 模板查找为精确类型匹配，任何子类异常都会落入"未知错误"模板
  - 位置: src/shared/media/error_handling.py:279-283
  - 证据:
    ```python
        # 获取错误类型
        error_type = type(error)

        # 查找模板
        template = self._templates.get(error_type, DEFAULT_ERROR_TEMPLATE)
    ```
  - 影响: `ERROR_MESSAGE_TEMPLATES` 按精确异常类做键匹配，不走 isinstance/MRO。后端一旦抛出模板类的子类（含 `errors.PermissionError` 这类已定义但未注册的类，见 AUD-34520），用户将看到"未知错误/联系技术支持"而非对应的分类提示与 can_retry/can_fallback 建议。当前仓库未发现模板类的子类定义，故为潜伏缺陷。
  - 建议: 按 `type(error).__mro__` 依序查表，或遍历 `self._templates` 用 isinstance 匹配（注意先匹配更具体的类）。
  - 置信度: 高（机制确定；现网触发条件当前不存在）

- [AUD-34516] [Suggestion] FallbackStrategy.max_attempts 从未使用，attempt() 不参考 get_available_fallbacks() 的错误相关方案
  - 位置: src/shared/media/error_handling.py:517-530、537-603
  - 证据:
    ```python
        self.max_attempts = max_attempts
        self.verbose = verbose
    ```
    attempt() 随后无条件依次执行三个策略：
    ```python
        # 1. 尝试切换后端
        result = self._try_backend_switch(path, original_backend)
        ...
        # 2. 尝试代理分辨率
        result = self._try_proxy_resolution(path)
        ...
        # 3. 尝试最小兼容模式
        result = self._try_minimal_mode(path)
    ```
  - 影响: `max_attempts=3` 参数是无效配置；无论错误类型如何（如 FileCorruptedError 明确不可恢复）都会做三轮打开尝试，与 `get_available_fallbacks`（742-784 行，按错误类型过滤方案）给出的建议互相脱节，浪费打开耗时并产生误导性日志。
  - 建议: attempt 接受 fallbacks=get_available_fallbacks(error) 的结果并受 max_attempts 截断，或删除失效参数。
  - 置信度: 中

- [AUD-34517] [Suggestion] 调用 MediaSourceFactory 的私有方法 `_get_backend_order`
  - 位置: src/shared/media/error_handling.py:620-625
  - 证据:
    ```python
        try:
            backends_to_try = MediaSourceFactory._get_backend_order('auto', path).copy()
        except Exception:
            backends_to_try = self._backend_order.copy()
    ```
  - 影响: 跨模块依赖下划线私有 API，factory 内部重构（重命名/签名变化）将静默落入 except 分支、退化为单一 ffmpeg 兜底，Topos MOV 的 topos→ffmpeg 路由（R-17）失效而无告警。
  - 建议: 在 MediaSourceFactory 上提供公开的 `get_backend_order(path)` 查询方法。
  - 置信度: 高

- [AUD-34518] [Suggestion] ErrorSeverity 枚举与模板中的裸字符串 severity 互不关联
  - 位置: src/shared/media/error_handling.py:73-75、78-89、93-165
  - 证据:
    ```python
        severity: str = 'warning'  # 'error', 'warning', 'info'
    ```
    ```python
    class ErrorSeverity(Enum):
        ERROR = 'error'
        WARNING = 'warning'
        INFO = 'info'
    ```
  - 影响: 模板与 UserMessage 全程用字符串，`ErrorSeverity` 枚举无任何使用者；拼写错误（如 'warn'）不会有类型错误，`get_severity_icon` 将静默返回 '❓'。
  - 建议: ErrorMessageTemplate.severity 与 UserMessage.severity 改用 ErrorSeverity 类型，展示层再转字符串。
  - 置信度: 高

- [AUD-34519] [Suggestion] 降级尝试中 open() 失败时，已 create 的 MediaSource 未关闭
  - 位置: src/shared/media/error_handling.py:637-649、678-679、720-725
  - 证据:
    ```python
                source = MediaSourceFactory.create(path, backend=backend)
                source.open()

                return FallbackResult(
                    success=True,
    ```
  - 影响: 若 `create()` 构造出的对象在 `open()` 失败前已分配了需释放的资源（如临时句柄/后台线程），该对象被直接丢弃无 close；是否实际泄漏取决于各后端 create/open 的实现，故低置信度。
  - 建议: open 失败的 except 分支中 best-effort 调用 `source.close()`（忽略二次异常）。
  - 置信度: 低

### 通过项
- 线程安全：MediaErrorHandler/FallbackStrategy 无共享可变状态（字段仅在 __init__ 赋值），实例可安全跨线程使用。
- GPU 资源生命周期：不涉及 OpenGL；MediaErrorContext.__exit__ 对成功打开的 source 统一 close（922-926）。
- 信号防递归：无 Qt 信号。
- 异常路径（上下文管理器）：__exit__ 仅抑制 MediaError（929-935），其他异常正常传播，符合设计文档。
- 输入验证：attempt() 先做 os.path.exists 检查（559-565），缺失文件直接返回明确错误。
- _customize_solution/_extract_technical_details 对各异常属性做了 None 防护（323-331、389-407）。
- 类型注解：完整。

---

## src/shared/media/errors.py (行数: 97)

### 发现

- [AUD-34520] [Suggestion] PermissionError(MediaError) 遮蔽内建异常名，且与 error_handling 的模板键（内建 PermissionError）永不匹配
  - 位置: src/shared/media/errors.py:91-96；对照 src/shared/media/error_handling.py:157-164
  - 证据:
    ```python
    class PermissionError(MediaError):
        """权限问题错误。

        当无法访问文件（如权限不足、文件被锁定）时抛出。
        UI 应提示用户检查文件权限或关闭占用程序。
        """
        pass
    ```
    error_handling.py 中模板键为内建 PermissionError（该文件未从 .errors 导入此类）：
    ```python
        # 权限问题
        PermissionError: ErrorMessageTemplate(
            title='权限不足',
    ```
  - 影响: 本模块内 `PermissionError` 名被遮蔽（模块内若 raise PermissionError 会得到 MediaError 子类而非内建 OSError 体系）；经 grep 全仓无任何导入方（`__init__.py:154-161` 的导出清单也不含它），是死代码；一旦被使用，error_handling 的精确类型匹配（AUD-34515）使其永远拿不到"权限不足"模板而显示"未知错误"。
  - 建议: 删除该类，或改名 MediaPermissionError 并在 ERROR_MESSAGE_TEMPLATES 注册。
  - 置信度: 高

### 通过项
- 通过（其余部分）：MediaError/UnsupportedCodecError/DecodeError/SeekError/FileCorruptedError 层次清晰，构造函数均正确设置属性并调用 super().__init__；ColorMetadataWarning 继承 Warning 语义恰当；无状态、无逻辑分支。
