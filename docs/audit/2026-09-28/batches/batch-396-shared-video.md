# batch-396 审查报告（src/shared/video）

批次：batch-396 [P2] 主题 src/shared/video//
编号区间：AUD-39600–AUD-39699
审查文件：`src/shared/video/video_playback.py`（4,982 行，已全文通读）
交叉验证（只读）：`src/shared/video/fallback_types.py`、`src/shared/video/pipeline_decision.py`、`src/shared/video/playback_state.py`、`src/app/controllers/file_controller.py`、`src/features/color/core/async_renderer.py`（仅用于确认跨模块事实）

---

## src/shared/video/video_playback.py (行数: 4982)

### 发现

- [AUD-39600] [Critical] 高位深 BGR packed 帧转 uint8 缺少 *255 缩放，16-bit BGR 帧显示为全黑
  - 位置: src/shared/video/video_playback.py:3499
  - 证据:
    ```python
    if pixel_format in ('bgr24', 'bgra', 'bgra64'):
        # BGR 格式，直接使用
        if data.dtype == np.uint8:
            return data[..., :3] if data.shape[-1] == 4 else data
        elif data.dtype == np.uint16:
            # 高位深，转为 uint8
            return (data[..., :3] / (1 << frame.bit_depth)).astype(np.uint8)
    ```
    同函数 RGB 分支（L3508-3510）是正确写法：`rgb8 = (data[..., :3] / 65535.0 * 255.0).astype(np.uint8)`；本文件 `_process_frame`（L667）同样正确。
  - 影响: uint16 BGR 数据除以 `1 << bit_depth`（16bit 时 65536、10bit 时 1024）后全部落在 [0,1)，`astype(np.uint8)` 截断后全为 0 —— 首帧/HQ seek/静态预览路径（`_process_coordinator_frame`、`render_static_preview_frame`、`_show_first_frame_enhanced` 均调用本函数）输出纯黑画面，属渲染错误。另外 `frame.bit_depth` 为直接属性访问（同文件其他处用 `getattr(frame, 'bit_depth', 8)`），属性缺失时抛 AttributeError。
  - 建议: 改为 `(data[..., :3] / 65535.0 * 255.0 + 0.5).astype(np.uint8)`（或按 bit_depth 归一化到 [0,1] 后 *255 再取整），并统一用 `getattr(frame, 'bit_depth', 16)` 兜底。
  - 置信度: 高（数学错误确凿；实际触发依赖解码器输出 bgra64 等 uint16 BGR 格式）

- [AUD-39601] [Major] 基类多个公共方法引用从未在基类初始化的 `self._playback_clock`（及 `_proxy_prefetch_worker`），直接实例化基类即 AttributeError
  - 位置: src/shared/video/video_playback.py:2254, 2278, 2291, 2331, 2345-2346, 2228
  - 证据:
    ```python
    def get_current_time(self) -> float:
        """获取当前播放时间（秒）"""
        fps = self._playback_clock.fps if self._playback_clock else 0.0
        if fps > 0:
            return self.current_frame / fps
        return 0.0
    ```
    基类 `VideoPlaybackEngine.__init__`（L718-794）没有任何 `_playback_clock` 赋值；该属性只在子类 L2417 `self._playback_clock: Optional[Any] = None` 初始化。基类 `_check_proxy_ladder`（L2228）同样引用仅子类初始化的 `self._proxy_prefetch_worker`。
  - 影响: `VideoPlaybackEngine` 自称"基类（UI 信号壳层）"，但其 `seek_to_time`/`last_frame`/`set_playback_speed`/`get_current_time`/`get_time_string`/`_check_proxy_ladder` 对基类实例（或忘记初始化该属性的子类）全部抛 AttributeError 崩溃。当前 src 内仅 `EnhancedVideoPlaybackEngine` 被实例化（file_controller.py），故为潜伏缺陷。
  - 建议: 在基类 `__init__` 补 `self._playback_clock = None` 与 `self._proxy_prefetch_worker = None`（与 `_video_renderer` 等属性的初始化方式一致）。
  - 置信度: 高

- [AUD-39602] [Major] `set_playback_speed` 未验证 speed：传 0 触发 ZeroDivisionError，负值导致 8ms 高频定时器
  - 位置: src/shared/video/video_playback.py:2294
  - 证据:
    ```python
    if self.is_playing:
        fps = self._playback_clock.fps if self._playback_clock else 30.0
        if fps <= 0:
            return
        interval = int(1000 / (fps * speed))
        interval = max(8, int(interval * 0.8))
        self._play_timer.setInterval(interval)
    ```
  - 影响: speed=0 时 `1000 / (fps * 0)` 抛 ZeroDivisionError（外部输入未验证，违反铁律"外部数据须验证"）；speed 为负时 interval 为负、被 `max(8, ...)` 钳到 8ms，播放定时器以 125Hz 狂转空耗 CPU/GPU，且 `self._playback_clock.set_speed(负值)` 行为未定义。
  - 建议: 入口校验 `if not isinstance(speed, (int, float)) or speed <= 0: logger.warning(...); return`，或对 speed 取 `max(min_speed, speed)` 钳制。
  - 置信度: 高

- [AUD-39603] [Major] `_init_dual_path_components` 异常路径丢弃已 open() 的 SourceManager 而不 close()，泄漏解码器与文件句柄
  - 位置: src/shared/video/video_playback.py:2859-2864
  - 证据:
    ```python
    except Exception as e:
        logger.error(f"Failed to init dual path: {e}")
        traceback.print_exc()
        self._use_dual_path = False
        self._source_manager = None
        self._playback_clock = None
    ```
    上方 L2829-2830 `if not self._source_manager.open():` 成功后，后续 PlaybackClock/FrameScheduler 创建若抛异常即进入此分支，已打开的 SourceManager 引用被直接置 None。
  - 影响: 打开状态的媒体源（FFmpeg/AVFoundation 解码上下文、文件句柄、代理缓存）失去引用且未显式关闭，依赖 GC 时机；反复触发（如配置切换重试）会累积泄漏底层 C 资源。
  - 建议: 异常路径先 `try: self._source_manager.close() except Exception: log` 再置 None。
  - 置信度: 高

- [AUD-39604] [Major] Enhanced.unload_video 中 `coordinator.unload()`/`source_manager.close()` 无异常保护，一旦抛出将跳过后续纹理清空与标志复位
  - 位置: src/shared/video/video_playback.py:3550-3559
  - 证据:
    ```python
    if self._coordinator:
        self._coordinator.unload()
        self._coordinator = None

    # 关闭双路径组件（向后兼容）
    if self._source_manager:
        self._source_manager.close()
        self._source_manager = None
        self._playback_clock = None
        self._frame_scheduler = None
    ```
    而函数后半段（L3580-3592）负责清空 `renderer.current_video_texture/current_video_planes`、复位 `_last_submitted_video_reason` 等跨片段残留标志——其注释明确说明不清空会导致"按播放后闪一下上一片段的画面"。
  - 影响: 卸载途中任一异常（解码器关闭失败很常见）会中断卸载流程，残留旧片段纹理与提交标志，触发本函数注释里描述的闪旧画面缺陷；且 `self._coordinator` 未置 None，后续误用半卸载状态的 coordinator。
  - 建议: 两处卸载各自 try/except 记录日志，保证后续清理与置 None 必然执行（可将清理段放入 finally）。
  - 置信度: 中

- [AUD-39605] [Major] 多处宽泛 `except Exception` + pass/仅 debug 日志，掩盖领域错误（违反铁律）
  - 位置: src/shared/video/video_playback.py:2688-2690, 4213-4215, 4430-4432, 3430-3431, 4277-4282
  - 证据:
    ```python
    # withdraw_static_preview_pending (L2688-2690)
    try:
        engine.render_pipeline(render_reason='viewport_refresh')
    except Exception:
        pass
    ```
    ```python
    # submit_static_preview_array (L4430-4432)
    except Exception as exc:
        logger.debug("submit_static_preview_array failed: %s", exc)
        return False
    ```
    另 `request_static_preview_frame` L4213-4215 `except Exception: frame = None`、`_select_backend_for_path` L3430-3431 `except Exception: pass`、`render_static_preview_frame` L4279-4282 连续两层兜底全部吞掉。
  - 影响: 静态预览/视口刷新失败时既无可见日志也无告警，调色页黑屏/冻结类问题难以定位；探针失败被吞后专业格式可能静默走错 backend。项目中同类场景已区分 `except (AttributeError, RuntimeError)` 精确捕获（如 L2693-2696），说明宽泛版可收窄。
  - 建议: 按预期错误类型收窄（TypeError/RuntimeError/AttributeError），其余至少 logger.warning 带 exc_info。
  - 置信度: 高（实例确凿；实际后果取决于被吞异常的种类）

- [AUD-39606] [Major] UI 线程置空 `renderer.current_video_texture/current_video_planes` 可能成为 ModernGL 纹理的最后引用，使释放发生在 UI 线程
  - 位置: src/shared/video/video_playback.py:3579-3581（unload_video）
  - 证据:
    ```python
    renderer = getattr(engine, "renderer", None) if engine else None
    if renderer is not None:
        renderer.current_video_texture = None
        renderer.current_video_planes = None
    ```
    渲染器自身清空同一属性发生在渲染线程（async_renderer.py L4192、L4634、L6070 均在命令处理路径内）。
  - 影响: 若渲染线程的纹理池/帧结构不再持有该纹理引用，UI 线程的置空操作触发引用计数归零，ModernGL Texture 的释放（glDelete）在 UI 线程执行，违背"GPU 资源按 deferred-release 模式释放"与"UI 线程不得操作 OpenGL"，存在上下文错误风险。若纹理池始终持引用则无实际影响。
  - 建议: 改为向渲染线程提交一条清空命令（或调用渲染器既有的 deferred release 通道），UI 线程只置 Python 侧的"逻辑标志"。
  - 置信度: 低（取决于渲染线程是否保留其他引用，未能在本批次范围内完全确认）

- [AUD-39607] [Suggestion] 模块级重复导入：`Enum`/`dataclass` 各导入两次，`VideoPipelineFallbackReason` 从两个模块各导入一次
  - 位置: src/shared/video/video_playback.py:63-64 vs 117-118；121-131 vs 134-146
  - 证据:
    ```python
    from dataclasses import dataclass      # L63
    from enum import Enum                  # L64
    ...
    from enum import Enum, auto            # L117
    from dataclasses import dataclass       # L118
    ```
    L122 与 L138 先后从 `fallback_types` 与 `pipeline_decision` 各导入一次 `VideoPipelineFallbackReason`（已验证 pipeline_decision L89 从 fallback_types 转发导入，二者同一对象，仅冗余非遮蔽）。
  - 影响: 无运行时错误，但阅读时易误以为存在两套枚举，掩盖真实依赖来源；`from __future__` 语义下双重导入属代码噪音。
  - 建议: 删除 L117-118 与 L122 处的重复项，统一从 pipeline_decision 导入（或统一从 fallback_types）。
  - 置信度: 高

- [AUD-39608] [Suggestion] `load_video` 尾部存在不可达代码
  - 位置: src/shared/video/video_playback.py:3021-3026
  - 证据:
    ```python
    else:
        # 首帧显示成功（MASTER_OK 或 PROXY_OK 或 FRAME_PATH_DEGRADED）
        logger.info(
            f"首帧显示成功（{result.status}）: "
            ...
        )
        return True

    logger.info(
        f"EnhancedVideoPlaybackEngine (P5.A) 加载成功: "
        f"path={path}, backend={backend}, "
        f"fps={fps}, total_frames={total_frames}"
    )
    return True
    ```
    上方 `if result.should_fallback_v2 / elif needs_placeholder / else` 三个分支全部 `return True`，末尾的汇总日志与 return 永不执行。
  - 影响: 无功能影响；这段"加载成功汇总日志"（含 backend/fps/total_frames）实际从不打印，削弱了本应有的加载诊断信息。
  - 建议: 删除死代码，或把汇总日志并入 else 分支 return 之前。
  - 置信度: 高

- [AUD-39609] [Suggestion] `_show_first_frame_via_coordinator` 恒假死分支；`FRAME_PATH_DEGRADED` 最终状态不可达但被 `is_displayable` 计为可显示
  - 位置: src/shared/video/video_playback.py:3214-3220, 3148, 211-215
  - 证据:
    ```python
    # v2 GStreamer 链路已移除：永远不回退 v2，保持 v3 状态
    should_fallback = False

    if should_fallback:
        result.fallback_reason = VideoPipelineFallbackReason.CODEC_UNSUPPORTED_BY_V3
        result.error_message = f"Codec/Pixel format 可能不被 v3 支持: ..."
        logger.warning(f"首帧获取失败（致命错误）: {result.error_message}, fallback v2")
    ```
    L3148 在 master 处理失败时置 `result.status = FirstFrameStatus.FRAME_PATH_DEGRADED` 后继续走 Level 2，最终必被 PROXY_OK/TIMEOUT_PROXY/PLACEHOLDER 覆盖；而 `FirstFrameResult.is_displayable`（L211-215）把 FRAME_PATH_DEGRADED 列为可显示。
  - 影响: 死分支与不可达状态使三级首帧逻辑的真实语义（降级 vs 失败）与枚举注释、调用方判断（load_video 的 else 分支按"显示成功"处理）不一致，后续维护易误判。
  - 建议: 删除 `should_fallback` 恒假分支；若 FRAME_PATH_DEGRADED 想表达"降级后仍显示"，应在 proxy 成功时保留该状态而非覆盖。
  - 置信度: 高

- [AUD-39610] [Suggestion] `ProxyPrefetchWorker` 多数互斥锁的 lock/unlock 无 try/finally，锁内抛异常将永久锁死队列
  - 位置: src/shared/video/video_playback.py:601-649, 575-580, 619-628
  - 证据:
    ```python
    def _add_frame(self, frame_index, frame_array, generation) -> None:
        """添加帧到就绪队列"""
        self._ready_frames_mutex.lock()
        # 如果队列满，丢弃最旧的帧
        if len(self._ready_frames) >= self._max_queue_size:
            min_idx = min(self._ready_frames, key=lambda x: x[0])[0]
    ```
    对比同类 `get_ready_frame`（L510-540）已正确使用 `try/finally: unlock`；`_has_frame`/`_add_frame`/`_remove_frame`/`_maybe_drop_old_frames`/`get_queue_depth` 均为裸 lock/unlock，且 `_has_frame` 依赖循环内提前 unlock + return 的手写路径。
  - 影响: 锁内代码（如 `min()` 对畸形元组）一旦抛异常，`_ready_frames_mutex` 永久持有，UI tick 的 `get_ready_frame` 与工作线程全部阻塞死锁（QMutex 非递归，同线程重入也直接死锁）。
  - 建议: 统一改为 `locker = QMutexLocker(...)` 或 try/finally 模式。
  - 置信度: 高（模式缺陷确凿；触发需锁内异常，概率低）

- [AUD-39611] [Suggestion] 已加载视频时 `enable_dual_path` 会另建并打开第二个 SourceManager，与 coordinator 的重复
  - 位置: src/shared/video/video_playback.py:2786-2834（L2797-2799 与 L2822-2834）
  - 证据:
    ```python
    if self._coordinator is not None:
        self._source_manager = self._coordinator.source_manager
        self._playback_clock = self._coordinator.session.playback_clock if self._coordinator.session else None
        ...
        self._source_manager = SourceManager(
            path=self.current_path,
            proxy_config=config,
            viewport_size=viewport_size,
            preferred_backend=preferred_backend
        )
    ```
    先同步 coordinator 的 source_manager，随后无条件新建并 open() 一个独立 SourceManager 覆盖之（coordinator.load 时已为同一文件创建过自己的源）。
  - 影响: 同一媒体文件双份解码上下文/缓存；旧架构路径（`_on_play_tick` 旧分支、`render_static_preview_frame` 回退 getter）与新 coordinator 并存时行为混乱。已验证当前生产调用（file_controller.py L4663）仅在构造期、`current_path` 为 None 时调用，故为潜伏问题。
  - 建议: coordinator 存在时跳过重复创建，或将 `_init_dual_path_components` 的自建段限制在无 coordinator 的旧架构分支。
  - 置信度: 高（代码行为确凿，触发路径当前不可达）

- [AUD-39612] [Suggestion] 类型注解缺失：`engine` 参数无类型、多个方法缺返回注解
  - 位置: src/shared/video/video_playback.py:718, 2403, 1277, 1293, 1032, 1017, 2095, 2104, 2258-2281 等
  - 证据:
    ```python
    def __init__(self, engine):          # L718，engine 无类型
    ...
    def _show_frame_hq(self, frame_num: int, is_new_file: bool = False):   # L1277，无返回注解
    ...
    def _submit_frame_playback(self, frame, frame_num: int, *, ...):        # L1293，无返回注解
    ```
    同类缺返回注解：`unload_video`(1017)、`_init_video_renderer`(1032)、`start_playback`(2095)、`stop_playback`(2104)、`next_frame/prev_frame/first_frame/last_frame`(2258-2281)、`_update_proxy_size`(1106) 等。
  - 影响: 与项目强类型规范不符；`engine` 的实际协议（viewport_size/renderer/layers/scene_size...）无文档化类型，重构与静态检查困难。
  - 建议: 补 `engine: "ImageEngine"`（TYPE_CHECKING 前向引用）与 `-> None`/`-> bool` 返回注解。
  - 置信度: 高

- [AUD-39613] [Suggestion] 每次平面帧转换新建 ColorConverter；活跃图层循环内重复赋值不 break
  - 位置: src/shared/video/video_playback.py:3883, 2040-2042
  - 证据:
    ```python
    # 使用 ColorConverter 进行转换
    converter = ColorConverter()
    rgb = converter.yuv_to_rgb(...)
    ```
    ```python
    for i, L in enumerate(self.engine.layers):
        if hasattr(self.engine, '_active_layer_index'):
            params['active_layer_index'] = self.engine._active_layer_index
    ```
  - 影响: `_convert_planar_to_rgb` 在每次 HQ seek/静态预览时重复构造转换器（若构造含表初始化则为纯浪费）；图层循环每个图层都执行同一赋值且 `i` 未使用，意图（找到即停）未表达。
  - 建议: 模块级或实例级缓存 ColorConverter；循环改为取到即 `break`（或直接删循环，只读一次属性）。
  - 置信度: 中（性能影响取决于 ColorConverter 构造成本，未在本批次范围内测量）

### 通过项
- 线程安全（锁序）: ProxyPrefetchWorker 的 seek/generation/ready_frames 三把锁均单独获取释放、无嵌套倒序，`get_ready_frame` 持 ready_frames 锁时再取 generation 锁的方向无反向持锁者，无死锁序
- 线程安全（回调）: 已验证 PlaybackCoordinator 按 S0.B 设计不再调用 `set_on_frame_ready` 回调（playback_state.py L767-778），`_on_coordinator_frame_ready` 为预留路径，无工作线程直呼 UI 提交的竞态
- 线程安全（状态注册）: P2-1 全局播放注册表 `_global_playback_engines` 全程持锁访问；V-P0-4 仅在启动成功后注册 True，失败路径无误报残留（id(self) 复用风险可忽略）
- GPU 资源生命周期（命令面）: 引擎不直接创建 GL 对象，纹理上传/YUV 转换全部经 RENDER_VIDEO_FRAME/RENDER_VIDEO_GPU_FRAME 命令在渲染线程执行；GpuDecodedFrame 零拷贝路径 payload 完整传递 matrix/range/decode_path
- GLSL/HDR 正确性（色彩元数据）: `_get_yuv_matrix`/`_get_color_range` 覆盖 bt709/bt2020(nc)/smpte170m/bt470bg 与 limited/full(tv/pc)，默认值 BT709+LIMITED 合理；`_convert_planar_to_rgb` 的 RGB→BGR 翻转注释与实现一致，避免红蓝互换
- HDR 策略: 播放热路径 planar YUV 保持平面直传 GPU，无 UI 线程 CPU 转换；uint8→uint8 的 P1-8 直传与 proxy 降分辨率均属文档化的预览策略，非无理由 clamp
- 信号防递归: 引擎不持有/程序化设置任何 UI 控件，frame_changed/frame_ready/play_state_changed 均为单向状态通知信号，不存在需状态锁的递归回路
- 状态提交一致性: `_render_params_generation` 单调递增，`invalidate_cached_render_params` 同步清缓存并调用 renderer 的 discard 钩子；tick 中 `needs_grade_refresh` 保证调参后 held frame 恰好重渲染一次，逻辑自洽（本模块不涉及 undo/redo/持久化）
- 异常与取消路径（worker）: ProxyPrefetchWorker 主循环异常被捕获并限速重试，stop_flag 检查在循环头；`_stop_proxy_prefetch_worker` 有 wait(2000) 超时兜底
- 输入验证: `set_display_scene_size` 对 None/非正尺寸回退并同步 engine.scene_size；video_info 的数值字段做 isinstance+>0 校验；`_safe_stat_int` 防 MagicMock；`_presentation_size_for_frame` 对 fallback 尺寸 max(1,...) 钳制
- 性能模式: 播放 tick 无同步解码（P2-1 断言 + 各分支验证）；渲染队列 backpressure 每 200ms 探测一次而非每 tick 提交；4K packed 警告与 PlaybackClock 重启警告均有节流；`_get_playback_render_params` 缓存图层参数仅刷新视口相关字段
- 不可变/桩路径: 基类 `load_video`/`start_playback`/`_show_frame_hq` v2 桩方法行为明确（报错/no-op），`_on_play_tick` 基类桩自动 stop，无双路径泄漏执行
