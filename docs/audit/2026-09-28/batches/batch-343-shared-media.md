# batch-343 [P2] src/shared/media/ 审查报告

- 审查日期：2026-09-28
- 编号区间：AUD-34300–AUD-34399（实际使用 AUD-34300–AUD-34318）
- 文件：`__init__.py` (478)、`adapter.py` (733)、`asset_proxy_manager.py` (413)、`cache.py` (521)，共 4 文件 2,145 行，全部通读
- 交叉验证（只读）：`frame.py`、`ffmpeg_source.py`、`topos_source.py`、`factory.py`、`gpu_frame.py`、`proxy.py`、`proxy_manifest.py`、`disk_proxy_service.py`、`metadata.py`
- 汇总：Critical 0 / Major 5 / Suggestion 14
- 备注：`asset_proxy_manager.py` 文件头标注 DORMANT（暂无生产入口），其发现标注"休眠代码，接线后触发"；`adapter.py` 的 `VideoDecoderAdapter` 在 `src/features/composite/nodes/video_frame_node.py:603` 作为回退路径使用（生产可达）。

## src/shared/media/__init__.py (行数: 478)

### 发现

- [AUD-34308] [Suggestion] 私有名进入 `__all__`、`ExtendedPixelFormatInfo` 漏出 `__all__`
  - 位置: src/shared/media/__init__.py:172-173, 382-383, 117
  - 证据:
    ```python
    from .ffmpeg_source import (
        ...
        _is_gop_codec,
        _is_intra_codec,
    ```
    ```python
        '_is_gop_codec',
        '_is_intra_codec',
    ```
    ```python
    from .pixel_format import (
        ...
        PixelFormatInfo as ExtendedPixelFormatInfo,
    ```
  - 影响: 私有实现名成为包级公共 API（且公共等价物 `is_gop_codec`/`is_intra_codec` 已由 `seek` 导出），破坏封装；`ExtendedPixelFormatInfo` 已导入但不在 `__all__`（脚本核对为唯一遗漏项），`from src.shared.media import *` 拿不到它，公共面与声明不一致。
  - 建议: 从导入和 `__all__` 移除 `_is_gop_codec`/`_is_intra_codec`；将 `ExtendedPixelFormatInfo` 加入 `__all__` 或删除其导入。
  - 置信度: 高

### 通过项
- 纯 re-export 模块，无执行逻辑；导出 metadata/timecode/timeline/frame/pixel_format/decode_policy/frame_path/gpu_frame/source/errors/probe/ffmpeg_source/topos_source/hwaccel/seek/factory/adapter/decode_queue/frame_cache/color_decode/proxy(含 disk proxy)/proxy_generator/proxy_manager/cache/error_handling/macos_vt/windows_hw 约 30 个子模块的公共符号
- 全部 `from .xxx import` 目标模块均实际存在（含 `macos_vt`/`windows_hw` 为子包，已核对目录）
- `__all__` 与导入名一致（除 AUD-34308 一项）、无重复条目（脚本核对）
- 重依赖惰性化：PyAV (`import av`) 在 `ffmpeg_source.py`/`hwaccel.py` 中均为函数内导入，包导入不触发；`macos_vt`/`windows_hw` 顶层仅导入内部子模块
- `FrameCache` 命名冲突已用 `DualPathFrameCache` 别名显式消解，两处导出无覆盖
- 模块 docstring 的用法示例与实际 API 签名一致（`Rational.seconds_to_frame`/`frame_to_pts` 等已核对存在）

## src/shared/media/adapter.py (行数: 733)

### 发现

- [AUD-34300] [Major] 平面格式帧依赖的 `to_rgb()` 对 planar 恒返回 None，planar 后端（topos）素材经适配器取帧恒为 None
  - 位置: src/shared/media/adapter.py:215-228, 372-388（证据交叉：src/shared/media/frame.py:450-466、src/shared/media/topos_source.py:2070-2084）
  - 证据:
    ```python
    # adapter.py _convert_to_bgr8
    elif frame.is_planar and frame.planes:
        logger.warning(f"平面格式 {frame.pixel_format} 需要完整转换，返回 fallback")
        # 尝试使用 to_rgb 方法
        rgb_data = frame.to_rgb()
        if rgb_data is not None:
            return np.flip(rgb_data, axis=-1)
    ...
    raise ValueError(f"无法转换帧格式: {frame.pixel_format}, dtype={frame.dtype}")
    ```
    ```python
    # frame.py DecodedFrame.to_rgb —— 仅处理 packed
    def to_rgb(self) -> Optional[np.ndarray]:
        if self.is_packed and self.data is not None:
            ...
        return None
    ```
    ```python
    # topos_source.py read_frame 返回 planar 帧
    return DecodedFrame(
        ...
        data=None,
        planes=tuple(planes),
    ```
  - 影响: `ToposMediaSource.read_frame` 输出 planar uint16 帧（`data=None`），`VideoDecoderAdapter.get_frame` 的 8-bit 与 `preserve_depth=True` 两条路径最终都落到 `to_rgb()`→None→`raise ValueError`→被 `except` 吞掉返回 None。后端路由到 topos 的素材在旧接口/回退路径（`video_frame_node.py:603` 的 fallback）一帧都取不到，且 `preserve_depth` 的位深保留承诺对 planar 素材完全无法兑现；日志注释"返回 fallback"与实际抛错行为不符。
  - 建议: 在适配器中实现真正的 planar YUV/GBR→BGR 转换（或复用 `color_decode.ColorConverter`），`to_rgb()` 至少应抛 `NotImplementedError` 而非静默 None；转换失败时区分领域异常类型上报而非统一 None。
  - 置信度: 高

- [AUD-34301] [Major] `total_frames/fps/width/height` 属性静默吞掉一切异常并返回默认值，无任何日志
  - 位置: src/shared/media/adapter.py:467-501
  - 证据:
    ```python
    @property
    def fps(self) -> float:
        """获取视频帧率"""
        try:
            metadata = self._source.probe()
            return metadata.fps.to_fps()
        except Exception:
            return 30.0
    ```
    ```python
    @property
    def width(self) -> int:
        try:
            metadata = self._source.probe()
            return metadata.width
        except Exception:
            return 0
    ```
  - 影响: 违反".clinerules 不得宽泛 except 掩盖领域错误"铁律。`MediaOpenError`/`MediaDecodeError` 或源内部 bug 一律被吞；尤其 `fps` 静默回退 30.0，调用方（播放调度、时长计算 `duration`）会以错误帧率工作且无任何痕迹——对 23.976/25fps 素材直接造成播放速率错误。
  - 建议: 捕获具体领域异常并记录 warning 日志；探测失败时缓存失败状态并让上层显式感知（返回 0.0 或抛出），不要伪造 30.0。
  - 置信度: 高

- [AUD-34302] [Suggestion] `duration` 属性无异常保护，`release()` 后访问抛 AttributeError，与兄弟属性行为不一致
  - 位置: src/shared/media/adapter.py:503-512（对照 447-454 的 `release()` 置 `self._source = None`）
  - 证据:
    ```python
    @property
    def duration(self) -> float:
        """获取视频时长（秒）"""
        metadata = self._source.probe()   # release() 后 self._source 为 None
    ```
    ```python
    def release(self) -> None:
        if self._source:
            ...
            self._source = None
    ```
  - 影响: `release()` 后查询 `total_frames/fps/width/height` 均优雅降级（因有 except），唯独 `duration` 直接 `AttributeError: 'NoneType' object has no attribute 'probe'` 逃逸到调用方；关闭/清理时序中先 release 后读元数据的代码会被此不一致打断。
  - 建议: `duration` 采用与兄弟属性一致的 try/except（或改为在 `release()` 前缓存 `probe()` 结果，兼顾性能）。
  - 置信度: 高

- [AUD-34303] [Suggestion] `get_frame` 宽泛 except 把领域错误统一折叠为 None；Wrapper 回调 `except Exception: pass` 完全静默
  - 位置: src/shared/media/adapter.py:162-168, 714-718
  - 证据:
    ```python
        except ValueError as e:
            logger.warning(f"帧号超出范围: {frame_number}, 错误: {e}")
            return None
        except Exception as e:
            logger.error(f"获取帧失败: {frame_number}, 错误: {e}")
            return None
    ```
    ```python
        if frame is not None and self._frame_loaded_callback:
            try:
                self._frame_loaded_callback(frame_number)
            except Exception:
                pass
    ```
  - 影响: `MediaDecodeError`/`MediaSeekError` 等领域异常与编程错误（AttributeError/TypeError）被同一分支吞掉，调用方无法区分"帧不存在"与"解码器坏了"；回调失败完全无日志，问题不可诊断。接口文档写明"失败返回 None"，但按铁律应区分领域错误类型。
  - 建议: except 分支按 `MediaSeekError`/`MediaDecodeError`/`IndexError` 分类处理；回调处至少 `logger.debug` 记录异常。
  - 置信度: 高

- [AUD-34304] [Suggestion] `_detect_actual_bit_depth` 用整帧 `max()` 推断位深：暗帧误判 + 每帧全量扫描
  - 位置: src/shared/media/adapter.py:274-311
  - 证据:
    ```python
        # 检查最大值判断位深度
        max_value = data.max()

        if max_value <= 1023:
            return 10
        elif max_value <= 4095:
            return 12
        else:
            return 16
    ```
  - 影响: 16-bit 素材的暗场景帧（最大码值 ≤1023，如夜戏、黑场淡入）会被判为 10-bit，`last_frame_depth` 上报错误元数据；同时 `data.max()` 对 4K 帧（约 830 万采样）逐帧 O(N) 遍历，在 `preserve_depth=True` 的逐帧路径上是纯性能税。
  - 建议: 位深应来自 `plane_infos`/`pixel_format`（如 `yuv422p10le`）或 metadata，一次解析；`max()` 仅作最后回退。
  - 置信度: 高

- [AUD-34305] [Suggestion] `probe_video_info` 异常路径未释放已创建的 adapter，依赖 `__del__` 兜底
  - 位置: src/shared/media/adapter.py:562-575
  - 证据:
    ```python
        try:
            adapter = VideoDecoderAdapter(path)
            info = {
                ...
            }
            adapter.release()
            return info
        except Exception as e:
            logger.warning(f"探测视频信息失败: {e}")
            return None
    ```
  - 影响: 若 `adapter.width`/`duration` 等属性在 `release()` 之前抛出（构造成功但 probe 链路异常），已打开的解码器句柄要等到 GC 触发 `__del__` 才关闭；批量探测坏文件时句柄/内存释放时机不确定。
  - 建议: 用 `try/finally` 或 `contextlib.closing` 包住 adapter 生命周期。
  - 置信度: 高

- [AUD-34306] [Suggestion] `__init__` 早期失败时 `__del__`→`release()` 访问未绑定的 `_source`
  - 位置: src/shared/media/adapter.py:98-110, 447-461
  - 证据:
    ```python
        self._source: MediaSource = MediaSourceFactory.create(
            path,
            backend=backend,
            target_size=target_size
        )
        self._source.open()
    ```
    ```python
    def __del__(self) -> None:
        """析构函数"""
        self.release()
    ```
  - 影响: `create()` 抛 `MediaOpenError`（如文件不存在）或 `open()` 失败时实例属性未建全，半成品对象被 GC 时 `release()` 里 `if self._source:` 触发 `AttributeError`，解释器向 stderr 打印 `Exception ignored in __del__` 噪音（错误本身已被 `__init__` 正常抛出，无功能损害）。
  - 建议: `__init__` 最前面先 `self._source = None`，或 `release()` 用 `getattr(self, '_source', None)`。
  - 置信度: 中

- [AUD-34307] [Suggestion] `VideoDecoderWrapper.VIDEO_EXTENSIONS` 硬编码扩展名表与 Factory 双源维护，且含 `.dng` 静态图片格式
  - 位置: src/shared/media/adapter.py:722-733
  - 证据:
    ```python
    # 视频格式扩展列表（与 VideoDecoder.VIDEO_EXTENSIONS 一致）
    VIDEO_EXTENSIONS = [
        '.mp4', '.mov', '.avi', '.mkv', '.flv', '.wmv', '.webm', '.m4v',
        ...
        '.r3d', '.braw', '.ari', '.cine', '.dng',
    ]

    @staticmethod
    def is_video_file(path: str) -> bool:
        return any(path.lower().endswith(ext) for ext in VideoDecoderWrapper.VIDEO_EXTENSIONS)
    ```
  - 影响: 同一判断存在两套实现（`MediaSourceFactory.is_video_file` 与此表），注释宣称"一致"但无同步机制，必然漂移；基类 `probe_video_info` 走 `VideoDecoderAdapter.is_video_file`（factory 版）而子类实例走本表，同一文件在不同调用点判定可能不同；`.dng` 是 RAW 静态图格式，混入视频表会把图片当视频探测。
  - 建议: 删除本表，统一委托 `MediaSourceFactory.is_video_file`；若需保留 RAW 序列支持，明确其判定入口。
  - 置信度: 高

### 通过项
- 类型注解：类、方法、属性返回类型注解完整（含 `Optional[tuple[int, int]]` 等现代语法）
- 线程安全：实例按独占使用设计（无跨线程共享可变状态），`_metadata_cache`/`_last_frame_depth` 仅在取帧路径单线程更新，符合旧 VideoDecoder 使用惯例
- GPU 资源生命周期：本模块纯 CPU 数据转换，不触 OpenGL；`release()` 关闭源有 try 保护
- GLSL·HDR 正确性：float16/32→uint8 的 `np.clip(0.0,1.0)` 有明确目标格式理由（旧接口契约就是 8-bit 显示格式），`_convert_preserving_depth` 对 float 数据转 float32 不做额外量化，未违反 HDR 不无故 clamp 铁律
- 状态提交一致性：模块不参与 undo/revision，无持久化职责（通过）
- 输入验证：`get_frame` 的帧号越界由 `ValueError` 分支处理并记日志；`is_video_file`/`probe_video_info` 先做扩展名/存在性预检
- `get_metadata` 失败时返回降级 dict 并记日志（有日志的降级，可接受）；正常路径缓存 `_metadata_cache` 避免重复 probe

## src/shared/media/asset_proxy_manager.py (行数: 413)

> 文件头标注：`DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口`。以下发现均在接线后触发；生产当前经 `source_resolver.py` 的 `DiskProxyServiceAdapter` 绕开本类。

### 发现

- [AUD-34309] [Major] `_pending_generates` 去重 guard 在 `existing is None` 时形同虚设，重复派发生成
  - 位置: src/shared/media/asset_proxy_manager.py:242-247
  - 证据:
    ```python
        # 2. 去重 guard：同源正在 ensure 中，不重复触发
        with self._lock:
            if self._pending_generates.get(source_path):
                if existing is not None:
                    return existing
            self._pending_generates[source_path] = True
    ```
  - 影响: 竞态窗口内（线程 A 已置 pending、尚未 `upsert_entry`，线程 B 读到 `existing is None`）或 entry 被 `remove_entry`/重载清掉后，B 不会 return，继续往下重建 GENERATING entry（重置 `created_at`）、重复 emit 两个信号、再次 `_dispatch_generation`。类注释的关键不变量"同一 source_path 的多次调用只生成一次"被打破（实际重复转码由 DiskProxyService 的 GENERATING 检查兜底，但 manager 层的 entry/信号抖动不受保护）。
  - 建议: guard 命中时无论 `existing` 是否为 None 都应 return（返回内存中 entry 或重读一次 manifest）；用 `setdefault` 语义实现"占用即返回"。
  - 置信度: 高

- [AUD-34310] [Major] 取消/回调被吞的路径使 manifest 永久滞留 GENERATING、pending 永不清理，代理无法再生成
  - 位置: src/shared/media/asset_proxy_manager.py:347-356, 386-399（交叉证据：src/shared/media/disk_proxy_service.py:363-367、635-641）
  - 证据:
    ```python
    # disk_proxy_service.generate_proxy —— 已在生成中：吞掉本次回调直接返回
                if info.status == ProxyStatus.GENERATING:
                    logger.info(f"Proxy already generating: {source_path}")
                    # 可以添加进度回调
                    if on_progress:
                        self._progress_callbacks.append(on_progress)
                    return None
    ```
    ```python
    # disk_proxy_service._on_generation_complete —— 取消分支不调 on_complete/on_error、不分发状态回调
            elif result.cancelled:
                with self._lock:
                    info = self._info_cache.get(source_path)
                    if info:
                        self._info_cache[source_path] = info.with_status(ProxyStatus.MISSING)
                logger.info(f"Proxy generation cancelled: {source_path}")
    ```
    ```python
    # asset_proxy_manager —— GENERATING 只有这两条出路：on_complete / _mark_failed
        def _on_generation_complete(self, ...):
            self._sync_from_disk_proxy_info(source_path, info)
            with self._lock:
                self._pending_generates.pop(source_path, None)

        def _mark_failed(self, source_path: str, error: str) -> None:
            ...
            entry = self._manifest.get_entry(source_path)
            if entry is None:
                return                      # ← pending 未清理即返回
    ```
  - 影响: ① 生成被取消时：service 侧置 MISSING 但从不通知 manager，manifest entry 永远停在 GENERATING → `is_proxy_ready` 恒 False，且 `ensure_proxy_async` 第 1 步"key 一致且 generating → 返回"使该源永久无法重新生成，直到重启；② 并发 ensure 撞上 already-GENERATING 分支时本次 `on_complete` 被吞，`_pending_generates` 残留 True；③ `_mark_failed` 在 entry 为 None 时（如刚被 invalidate/remove）早退且不清 pending。叠加 AUD-34309 后，残留 pending + STALE entry 会让 ensure 永远返回 STALE 而不重建代理。
  - 建议: manager 侧对 GENERATING entry 加超时/自愈（`updated_at` 超阈值即按 MISSING 处理并清 pending）；`_mark_failed` 先清 pending 再查 entry；取消路径也应回调或经 status callback 广播。
  - 置信度: 高

- [AUD-34311] [Major] `_sync_from_disk_proxy_info` 读取 `DiskProxyInfo` 上不存在的 `width/height`，READY entry 尺寸恒 0x0，`open_proxy_source` 固定按 1920x1080 打开
  - 位置: src/shared/media/asset_proxy_manager.py:373-374, 317（交叉证据：src/shared/media/proxy.py:833-857 `DiskProxyInfo` 字段表）
  - 证据:
    ```python
            entry = ProxyManifestEntry(
                key=key,
                status=info.status,
                proxy_path=info.proxy_path,
                width=info.width if hasattr(info, "width") else 0,
                height=info.height if hasattr(info, "height") else 0,
                ...
    ```
    ```python
            target_size = (entry.width or 1920, entry.height or 1080)
            return MediaSourceFactory.create(
                path=entry.proxy_path,
                target_size=target_size,
            )
    ```
    （`DiskProxyInfo` 实际字段为 `proxy_resolution: Optional[Tuple[int,int]]`，没有 `width`/`height` 属性，`hasattr` 恒 False。）
  - 影响: 所有经回调同步出的 READY entry `width/height` 恒为 0，`proxy_resolution` 信息被丢弃；`open_proxy_source` 于是永远传 `target_size=(1920, 1080)`——对竖屏/非 16:9 代理（如 607x1080），解码侧 `reformat(width=1920, height=1080)` 会把代理拉伸变形，渲染出错误几何。
  - 建议: 从 `info.proxy_resolution` 取 `(w, h)` 填 entry；`open_proxy_source` 在 entry 尺寸未知时不传 `target_size`（保持代理原生几何）而非伪造 1920x1080。
  - 置信度: 高

- [AUD-34312] [Suggestion] `_sync_from_disk_proxy_info` 用 `self._config` 而非 ensure 时生效的 `effective_config` 重建 key/codec
  - 位置: src/shared/media/asset_proxy_manager.py:366, 375（对照 213-214）
  - 证据:
    ```python
        # ensure_proxy_async 中：
        effective_config = config or self._config
        key = build_manifest_key(source_path, effective_config, self._app_version)
    ```
    ```python
        # _sync_from_disk_proxy_info 中：
        key = build_manifest_key(source_path, self._config, self._app_version)
        ...
            codec=_default_codec_policy(self._config),
    ```
  - 影响: 调用方传自定义 `DiskProxyConfig` 时，GENERATING entry 用 effective_config 的 key，完成后却被默认 config 的 key 覆盖 → 下一次同参数 ensure 发现 `existing.key != key`，再次走 GENERATING→dispatch→READY 的整轮循环：entry 状态反复抖动、双信号重复 emit、manifest 重复落盘；两种自定义配置之间还会互相顶掉对方的 entry。
  - 建议: 回调闭包携带 `effective_config`（`_on_generation_complete` 已带 config，但 sync 未用），或统一只允许 manager 级配置。
  - 置信度: 高（代码事实）/ 中（当前无自定义 config 调用方）

- [AUD-34313] [Suggestion] manifest 未配置且源文件存在时落入 `assert self._manifest is not None` 崩溃，违背自身注释契约
  - 位置: src/shared/media/asset_proxy_manager.py:201-211, 231
  - 证据:
    ```python
        if self._manifest is None:
            # 未配置时返回占位 entry（不应该发生，调用方应先 configure）
            key = build_manifest_key(source_path, config or self._config, self._app_version)
            if key is None:
                return ProxyManifestEntry(..., status=ProxyStatus.MISSING, ...)
            # ← key 非 None 时无 return，直落到下方
        ...
        assert self._manifest is not None
    ```
  - 影响: 未 configure 且文件存在（`build_manifest_key` 返回非 None）时，注释承诺"返回占位 entry"却执行到 assert：常规运行抛 AssertionError；`python -O` 下 assert 被剥离，变成 `AttributeError: 'NoneType' object has no attribute 'get_entry'`，两种模式崩溃形态还不一致。
  - 建议: `if self._manifest is None` 分支内无条件返回占位 entry（key 可用时带上真实 key）。
  - 置信度: 高

- [AUD-34314] [Suggestion] 后台线程 emit 携带可变 entry 引用 + `_mark_failed` 原地改 entry，跨线程接收方可能读到已变更状态
  - 位置: src/shared/media/asset_proxy_manager.py:384, 392-394, 343-345
  - 证据:
    ```python
        self._manifest.upsert_entry(entry)
        self._manifest.save()
        self.proxy_state_changed.emit(source_path, entry)
    ```
    ```python
        entry.status = ProxyStatus.FAILED
        entry.error = error
        entry.updated_at = time.time()
    ```
  - 影响: 完成回调在 DiskProxyService 的后台线程执行（经 `queue_proxy_generation` 的 worker），信号以 queued 方式投递到主线程时传递的是同一个可变 `ProxyManifestEntry` 引用；`_mark_failed` 等路径原地改字段，UI 槽函数执行时看到的 status/error 可能已不是 emit 时刻的快照（渲染/播放决策据此切换代理路径会抖动）。
  - 建议: emit 前做 entry 的浅拷贝（dataclasses.replace），或 entry frozen 化统一以新对象表示状态迁移。
  - 置信度: 中

### 通过项
- 查询热路径 `get_proxy_entry`/`is_proxy_ready` 纯内存、无磁盘 IO，符合 <1ms 规格；`ProxyManifest` 内部 RLock + tmp/replace 原子保存（已核对 proxy_manifest.py:186-267）
- 线程安全：`_pending_generates`/回调注册的读写均在 `self._lock`（RLock）内；manifest 的并发读写由其内部锁保护
- 输入验证：`build_manifest_key` 返回 None（源文件不存在）时返回 FAILED 占位 entry 并带 error 说明，不抛异常
- 类型注解完整；DORMANT 头标注清晰，休眠理由与生产替代路径（DiskProxyServiceAdapter）可追溯
- `ensure_proxy_async` 不把 GENERATING 当 ready（第 1 步显式区分 is_ready/is_generating），符合"不把生成中的 proxy 当 ready"规格
- `configure()` 显式要求 cache_dir 并立即 load manifest、注入 service 时注册回调，解绑路径留有 `_registered_callback` 防重复注册

## src/shared/media/cache.py (行数: 521)

### 发现

- [AUD-34315] [Suggestion] `put()` 替换已存在帧号时先按"满"淘汰 LRU、后删旧条目，无谓多淘汰一帧
  - 位置: src/shared/media/cache.py:182-209
  - 证据:
    ```python
            while (
                self._current_memory + size_bytes > self.policy.max_memory_mb * 1024 * 1024
                or len(self._cache) >= self.policy.max_frames
            ):
                if not self._evict_one():
                    ...
                    return False
            ...
            # 如果已存在，先删除旧的
            if frame_index in self._cache:
                old_entry = self._cache[frame_index]
                self._current_memory -= old_entry.size_bytes
                del self._cache[frame_index]
    ```
  - 影响: 替换已存在的 `frame_index` 本不增加条目数/净内存，但 while 条件把旧条目也计入（`len >= max_frames`、内存含旧 size），会先把 LRU 头（往往是相邻播放帧）逐出一个，再删旧条目——缓存长期欠填一帧、邻近帧被误逐，播放时多一次解码往返；内存判定同样先加新再减旧，夸大占用。
  - 建议: 先 `if frame_index in self._cache` 删旧并扣减内存，再进入淘汰 while。
  - 置信度: 高

- [AUD-34316] [Suggestion] 过期判定基于创建时间，`touch()` 维护的 `last_access` 被忽略，长暂停后热帧被判过期
  - 位置: src/shared/media/cache.py:106-118, 244-247
  - 证据:
    ```python
    def touch(self) -> None:
        """更新访问信息"""
        self.access_count += 1
        self.last_access = time.time()

    def is_expired(self, ttl: float) -> bool:
        """是否过期"""
        return self.age > ttl        # age = now - timestamp（创建时间）
    ```
    ```python
            if entry.is_expired(self.policy.ttl_seconds):
                self._remove_entry(frame_index)
                self._miss_count += 1
                return None
    ```
  - 影响: 暂停观看超过 ttl（proxy 30s）后，刚刚还命中过的帧因"创建时间"超龄被逐出，恢复播放/逐帧步进时全部重新解码——`last_access` 字段维护了却与淘汰/过期语义脱节，白付一次 O(1) 写。
  - 建议: `is_expired` 基于 `last_access`（滑动 TTL），或明确文档化"自创建起过期"并删除误导性的 last_access 维护。
  - 置信度: 高

- [AUD-34317] [Suggestion] `_estimate_size` 对未知类型（含 GPU 零拷贝帧）返回 0，内存上限失真且无 GPU 释放钩子
  - 位置: src/shared/media/cache.py:318-338
  - 证据:
    ```python
        if isinstance(frame_data, DecodedFrame):
            if frame_data.data is not None:
                return frame_data.data.nbytes
            elif frame_data.planes:
                return sum(p.nbytes for p in frame_data.planes)
            return 0
        elif isinstance(frame_data, np.ndarray):
            return frame_data.nbytes
        else:
            # 未知类型，估算为 0
            return 0
    ```
  - 影响: `GpuDecodedFrame`（非 `DecodedFrame` 子类，planes 为 `GpuFramePlane`、无 `nbytes`，已核对 gpu_frame.py）一旦被误存入本缓存：计入 0 字节 → `max_memory_mb`/`max_frames` 完全失守，GPU resident 帧可无限堆积；且淘汰路径只做 dict 删除，从不调用 `safe_release_gpu_frame`，texture 归还只能靠 GC 时机，违反延迟释放契约。
  - 建议: 未知类型记 warning 并按保守值计费（或直接拒收）；对 GPU 帧显式拒收（抛 TypeError 引导走 decode_queue 的 GPU 通路）。
  - 置信度: 中（潜在误用面，当前 DualFrameCache 消费方 playback_clock 存 CPU 帧）

- [AUD-34318] [Suggestion] `get_missing_frames` 持锁全量扫描大区间；`prefetch_hint` 可产生负帧号、区间参数无校验
  - 位置: src/shared/media/cache.py:393-398, 419-434
  - 证据:
    ```python
        with self._lock:
            missing = []
            for i in range(start_frame, end_frame + 1):
                if i not in self._cache:
                    missing.append(i)
            return missing
    ```
    ```python
        elif direction == 'backward':
            return [
                current_frame - i
                for i in range(1, prefetch_count + 1)
            ]
    ```
  - 影响: 长时间线（如 2 小时 24fps ≈ 17 万帧）在 RLock 内做十几万次成员判断并建大列表，播放/预读线程调用时会顶住解码线程的 put/get；`backward` 预读在片头会返回负帧号（下游需自行过滤），`start > end` 时静默返回空列表掩盖调用错误。
  - 建议: 大区间分批或只返回前后 N 个缺口；`prefetch_hint` 过滤 `max(0, ...)`；`get_missing_frames` 对 `start > end` 或超长区间记 warning/截断。
  - 置信度: 中

### 通过项
- 线程安全：所有读写路径（put/get/contains/remove/clear/stats/cached_range/missing）均在 `threading.RLock` 内，统计计数也在锁内更新
- 内存计费与淘汰的一致性：除 AUD-34315 的替换次序外，`_remove_entry`/`_evict_one`/`clear` 均同步扣减 `_current_memory`，无泄漏口径
- 超限保护：单帧超过总限时 `_evict_one` 返回 False → 拒绝存入并记 warning，不会死循环
- GPU 资源生命周期：本模块不触 OpenGL，纯 numpy/DecodedFrame 容器（GPU 帧风险见 AUD-34317）
- 信号防递归：无 Qt 信号，不适用
- 状态提交一致性：无 undo/revision 职责，缓存为易失层（通过）
- 类型注解完整；`CachePolicy` frozen 数据类、`CacheEntry` 默认工厂正确（`field(default_factory=time.time)`）
- `DualFrameCache` master/proxy 策略分离合理（4 帧/256MB vs 16 帧/512MB），透传方法无额外状态
