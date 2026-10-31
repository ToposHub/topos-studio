# batch-357 审查报告：src/shared/media/

- 批次：batch-357 [P2] 主题 src/shared/media//
- 编号区间：AUD-35700–AUD-35799
- 文件：seek.py (465) / source.py (360) / thumbnail_provider.py (1,972)，合计 2,797 行，全部通读
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解

## src/shared/media/seek.py (行数: 465)

### 发现

- [AUD-35701] [Major] estimate_seek_cost 对反向小距离跳转系统性低估 GOP seek 成本，且分支注释与条件矛盾
  - 位置: src/shared/media/seek.py:342-352
  - 证据:
    ```python
    if current_frame <= keyframe_before_target:
        # 当前帧在 keyframe 之后，需要 seek + decode
        return max(1, decode_forward)
    else:
        # 当前帧在目标帧之前，继续 forward decode
        # 但如果超过 gop_size，需要 seek
        if frame_distance > gop_size:
            return max(1, decode_forward)
        else:
            # 继续顺序解码更高效
            return frame_distance
    ```
  - 影响: GOP 编解码器无法反向顺序解码。例：current=100、target=95、gop=12 时 keyframe_before_target=84、decode_forward=11，但 frame_distance=5 ≤ 12 走 `return frame_distance` 返回 5；实际必须 seek 回 keyframe 84 再解码 11 帧。该值被 `src/features/edit/core/edit_gpu_decode_manager.py:808`（交互解码 deadline 估算，`gap = frame_index - current_frame` 反向 scrub 时为负）和 `:2255`（seek-vs-顺序决策）消费，反向拖动播放头时 deadline 被低估最多 gop_size-1 帧 → 交互解码超时/掉帧/降质。另注：第一个分支的注释「当前帧在 keyframe 之后」与条件 `current_frame <= keyframe_before_target`（当前帧在 keyframe 之前）正好相反。测试 test_ffmpeg_seek.py 仅覆盖正向跳转（100→105）。
  - 建议: 反向（target < current）时一律返回 `max(1, decode_forward) + seek 开销常数`；或至少 `return max(frame_distance, decode_forward)`；同步修正注释。
  - 置信度: 高

- [AUD-35702] [Suggestion] frame_to_pts / pts_to_frame 用浮点除法做整数 PTS 换算，存在 off-by-one 风险
  - 位置: src/shared/media/seek.py:127-128、163-164
  - 证据:
    ```python
    pts_per_frame = fps.den * tb_den / (fps.num * tb_num)
    return int(frame_index * pts_per_frame)
    ...
    pts_per_frame = fps.den * tb_den / (fps.num * tb_num)
    frame = int(pts / pts_per_frame)
    ```
  - 影响: `pts_per_frame` 是浮点近似，`int()` 向零截断。当精确值为整数而浮点结果略小时（大帧号 / 特定 fps·time_base 组合），PTS/帧号差 1，seek 落到相邻帧。整数算术 `frame_index * fps.den * tb_den // (fps.num * tb_num)` 可完全避免。经 grep 确认生产代码（ffmpeg_source.py 等）用的是各自实例方法 `_frame_to_pts`，本函数仅被 `__init__.py` 再导出与测试引用，实际影响受限，故降为 Suggestion。
  - 建议: 改用整数分子分母交叉相乘后 `//`；`pts_to_frame` 同理。
  - 置信度: 中

- [AUD-35703] [Suggestion] find_keyframe_before 的 GOP 分支把 target PTS 本身当作 keyframe 返回，语义错误且无任何调用方
  - 位置: src/shared/media/seek.py:233-243
  - 证据:
    ```python
    if hasattr(ctx, 'gop_size'):
        gop_size = ctx.gop_size
        if gop_size and gop_size > 0:
            # 假设 GOP 大小固定，估算 keyframe PTS
            # 这是一个近似值，可能不准确
            return target_pts

    # 默认返回 None，让 seek 函数自行处理
    return None
    ```
  - 影响: 固定 GOP 假设下 keyframe 应为 `target - target % gop`，返回 `target_pts` 等于断言目标位置恰是关键帧，绝大多数情况下为假；若未来调用方据此 seek，将落到非 keyframe PTS，解码器跳到其后第一个 keyframe → 取错帧。grep 全库（src/ + tests/）确认无调用方（`__init__.py` 的导出列表也不含它），属于带陷阱的死代码。
  - 建议: 删除该函数，或改为返回 `target_pts - (target_pts % gop_pts_interval)` 并明确标注近似；side_data 空循环一并清理。
  - 置信度: 高

- [AUD-35704] [Suggestion] frame_to_pts docstring 示例输出错误
  - 位置: src/shared/media/seek.py:110-114
  - 证据:
    ```python
    Examples:
        >>> fps = Rational(24000, 1001)  # 23.976fps
        >>> frame_to_pts(100, fps)  # 第 100 帧 PTS
        100
    ```
  - 影响: `time_base is None` 分支返回 `frame_index * fps.den` = 100 × 1001 = 100100，不是 100。误导使用者与 doctest。
  - 建议: 示例改为 `100100` 或补充说明。
  - 置信度: 高

### 通过项
- 线程安全：纯函数无共享可变状态（GOP_CODECS/INTRA_CODECS 为模块级 frozenset 只读）
- GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性：均不涉及（无 GPU、无 Qt 信号、无 undo/revision）
- estimate_keyframe_interval 正确优先真实 interval（`real_interval > 0` 才生效），有 tests/media/test_gop_index.py 覆盖
- choose_seek_strategy 逻辑简单正确（仅测试引用，无生产调用方）
- 类型注解完整

## src/shared/media/source.py (行数: 360)

### 发现
（无）

### 通过项
- 通过（无逻辑）：纯 Protocol 接口定义文件。导出 MediaSourceError/MediaOpenError/MediaSeekError/MediaDecodeError 异常类与 @runtime_checkable Protocol（MediaSource、VideoSource、AudioSource），方法体全部为 `...`，无运行时逻辑
- 类型注解完整，签名与 docstring 一致；异常类携带 source_path 便于定位
- GPU/线程/信号/持久化维度均不涉及

## src/shared/media/thumbnail_provider.py (行数: 1972)

### 发现

- [AUD-35711] [Major] RAW 位图缩略图分支缺少 RGB→BGR 转换，缩略图红蓝通道互换
  - 位置: src/shared/media/thumbnail_provider.py:719-727
  - 证据:
    ```python
    try:
        thumb = raw.extract_thumb()
        if thumb.format == rawpy.ThumbFormat.JPEG:
            img = cv2.imdecode(np.frombuffer(thumb.data, np.uint8), cv2.IMREAD_COLOR)
        else:
            img = thumb.data # bitmap
    except:
        # Fallback to half-size postprocess
        img = raw.postprocess(use_camera_wb=True, half_size=True, no_auto_bright=True, bright=1.0)
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    ```
  - 影响: rawpy `extract_thumb()` 返回 BITMAP 格式时 `thumb.data` 是 RGB ndarray（DNG/部分 NEF 常见），该分支未转 BGR；而 JPEG 分支产出 BGR、except 分支显式转 BGR。随后 `run()` 第 668 行统一 `cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)`，对 RGB 输入等于再做一次交换 → 媒体池中这类 RAW 文件缩略图红蓝反转。
  - 建议: else 分支补 `img = cv2.cvtColor(thumb.data, cv2.COLOR_RGB2BGR)`（BITMAP 情况），保持三路出口色彩空间一致。
  - 置信度: 高

- [AUD-35712] [Major] 多处裸 `except:` 违反「不得宽泛 except」铁律，把任意异常当作"目录不可写"改写缓存路径
  - 位置: src/shared/media/thumbnail_provider.py:603、628、640、724
  - 证据:
    ```python
        try:
            if not os.path.exists(local_cache):
                os.makedirs(local_cache, exist_ok=True)
            test_file = os.path.join(local_cache, '.write_test')
            with open(test_file, 'w') as f: f.write('ok')
            os.remove(test_file)
        except:
            # 源目录不可写，使用平台规范缓存目录
            from src.shared.config.config_manager import ConfigManager
            cache_dir = os.path.join(ConfigManager.get_platform_cache_dir(), "thumbnails")
    ```
  - 影响: 裸 `except:` 连 KeyboardInterrupt/SystemExit 及任意代码 bug 一并吞掉，并静默改道缓存目录（行为与真实原因脱钩）；628/640 行的 `except: pass` 使 listdir 的权限/IO 错误完全不可见；724 行 rawpy 缩略图提取的任意错误（含编码 bug）都退化为整图 postprocess（慢一个数量级）。违反 .clinerules「不得宽泛 except 掩盖领域错误」。
  - 建议: 写测试改为 `except OSError:`；listdir 改为 `except OSError: continue`；rawpy 分支改 `except Exception:` 并 log warning。
  - 置信度: 高

- [AUD-35713] [Major] _get_cache_path 每次请求对缓存目录做 O(变体数×文件数) 全列扫描并逐文件 NFC 规范化
  - 位置: src/shared/media/thumbnail_provider.py:613-641
  - 证据:
    ```python
        filename_variants = [
            filename + '_lg.webp',        # large: photo.jpg_lg.webp (优先)
            filename + '.webp',           # small: photo.jpg.webp
            filename + '_preview.webp',   # preview: photo.jpg_preview.webp
        ]
        # 检查本地 .cache 目录
        if os.path.exists(local_cache):
            for variant in filename_variants:
                variant_nfc = unicodedata.normalize('NFC', variant)
                try:
                    for f in os.listdir(local_cache):
                        if unicodedata.normalize('NFC', f) == variant_nfc:
                            return os.path.join(local_cache, f)
    ```
  - 影响: 3 个变体 × 2 个目录（local + global，638-639 行同样模式），每个变体都重新 `os.listdir` 全目录并对每个文件名做 `unicodedata.normalize`。`.cache` 随媒体数增长（每媒体一个 webp），媒体池刷新 N 项即 O(N²) 次 listdir+normalize，全部挤在 maxThreadCount=1 的单工作线程上 → 大库（数千项）缩略图后台任务长时间占满线程池，其余缩略图明显延迟。
  - 建议: 每目录只 listdir 一次并构建 `{normalize(f): f}` 映射后查 3 个变体；或直接对 NFC 规范化后的确定路径做 `os.path.exists`。
  - 置信度: 高

- [AUD-35714] [Suggestion] _cleanup_file_lock 定义后从未调用，per-file 锁字典 _cv_locks 只增不减
  - 位置: src/shared/media/thumbnail_provider.py:27-30
  - 证据:
    ```python
    def _cleanup_file_lock(path: str) -> None:
        """Remove per-file lock after use to prevent unbounded growth."""
        with _cv_locks_guard:
            _cv_locks.pop(path, None)
    ```
  - 影响: grep 全库确认无调用方。长会话浏览大量不同视频后 `_cv_locks` 每路径一个 Lock 永久驻留（单条目很小，属慢泄漏），函数本身是死代码。
  - 建议: 在 `_extract_video_frames`/`_load_video_poster_frame` 的 finally 中于池归还后调用，或删除该函数。
  - 置信度: 高

- [AUD-35715] [Suggestion] EXR/HDR 缩略图对 scene-linear 数据直接 ×255 clip，无显示编码，预览近乎全黑
  - 位置: src/shared/media/thumbnail_provider.py:732-738
  - 证据:
    ```python
    elif ext == '.exr' or ext == '.hdr':
        img_data = np.fromfile(path, dtype=np.uint8)
        img = cv2.imdecode(img_data, cv2.IMREAD_ANYCOLOR | cv2.IMREAD_ANYDEPTH)
        if img is not None and img.dtype == np.float32:
            # Simple tone mapping for preview
            img = np.clip(img * 255, 0, 255).astype(np.uint8)
    ```
  - 影响: OpenCV 以线性 float32 读 EXR，场景线性值多数远小于 1.0，×255 后再无 sRGB/OETF 编码 → 缩略图严重偏暗。同文件 `.toos` 路径（711 行注释）明确做「linear→sRGB 显示编码」，HDR 预览处理不一致。属 HDR 显示正确性缺陷（缩略图层面，非渲染管线）。
  - 建议: 复用 `_decode_rgb` 的显示编码，或最小实现 `((img * exposure) ** (1/2.2))` 后量化。
  - 置信度: 高

- [AUD-35716] [Suggestion] 解码失败的缩略图无负缓存，损坏文件每次刷新都重新入队解码
  - 位置: src/shared/media/thumbnail_provider.py:1015-1035、1122-1124
  - 证据:
    ```python
        if path in self.pending_requests:
            return None
        # Request background load
        self.pending_requests.add(path)
        request = ThumbnailRequest(path, self._on_thumbnail_loaded, icon_size)
        self.pool.start(request)
        return None
    ...
        else:
            # Handle failure - maybe a default icon?
            pass
    ```
  - 影响: 失败仅移除 pending、不记录失败结果；面板每次刷新/重绘调用 `get_thumbnail` 都重新走完整磁盘读取+解码尝试。单个线程池被损坏/不支持文件反复占用，拖慢其它缩略图。
  - 建议: 失败路径写入带 TTL 的负缓存（或按 path 记录 fail count，超过阈值跳过）。
  - 置信度: 中

- [AUD-35717] [Suggestion] _get_bridge 跨线程无锁重建存在竞态，重建后会脱离 provider 已连接的 bridge
  - 位置: src/shared/media/thumbnail_provider.py:143-154
  - 证据:
    ```python
    def _get_bridge() -> _ThumbnailSignalBridge:
        global _bridge
        if not _qobject_is_alive(_bridge):
            _bridge = _ThumbnailSignalBridge()
        return _bridge
    ```
  - 影响: 该函数被多个 worker 线程并发调用：check-then-act 无锁，两线程可能各建一个 bridge；而 `ThumbnailProvider.__init__`（929-931 行）connect 的是当时的 bridge，晚到 worker 在新 bridge 上 emit 的结果不会投递（缩略图静默丢失）。另在 worker 线程创建 QObject 本身不合 Qt 线程亲和性约定。触发条件苛刻（仅 Qt teardown 后的迟到结果），实际风险低。
  - 建议: 用 module-level `threading.Lock` 保护重建；或仅在主线程重建（worker 经 QMetaObject invoke 到主线程处理）。
  - 置信度: 中

- [AUD-35718] [Suggestion] ThumbnailProvider.instance() / ClipThumbnailCache 单例无锁，且构造函数创建 QTimer/QThreadPool 要求主线程
  - 位置: src/shared/media/thumbnail_provider.py:888-892、468-478
  - 证据:
    ```python
    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = ThumbnailProvider()
        return cls._instance
    ```
  - 影响: check-then-act 无锁；模块级便捷函数 `request_clip_thumbnails_async`（1934 行）可从任意线程调用，若首次调用发生在非 GUI 线程，`__init__` 中的 `QTimer(self)`（916 行）在无 QApplication/非主线程创建 → Qt 告警或行为异常。grep 确认当前全部 20+ 调用点均为 UI 侧（main_window/panel/strip 等），现实风险低，属防御性缺口。
  - 建议: instance() 加锁并断言 `QThread.currentThread() == QApplication.instance().thread()`，或统一由主线程懒初始化。
  - 置信度: 高（代码事实）/ 影响低

- [AUD-35719] [Suggestion] 类型注解缺失与死代码：Optional[callable] 非法类型、QMetaObject/Q_ARG 死导入、ThumbnailImageResult 死 DTO、callback 参数从未使用
  - 位置: src/shared/media/thumbnail_provider.py:40/52、567-571、76-77、86-97、1178、1511、1936
  - 证据:
    ```python
    def _get_capture(path: str):          # 40 行，无返回注解
    ...
    class ThumbnailRequest(QRunnable):
        def __init__(self, path, callback, icon_size):   # 567 行，参数无注解
    ...
        def request_clip_thumbnails(
            self,
            request: ClipThumbnailRequest,
            callback: Optional[callable] = None          # 1178 行
    ```
  - 影响: `callable`（小写 builtin）不是类型，`Optional[callable]` 使静态检查失效（应为 `Callable[[str, ClipThumbnailSet], None]`）；`QMetaObject, Q_ARG`（77 行）导入后未使用；`ThumbnailImageResult`（86-97 行）全库无引用；`ThumbnailRequest.callback` 与 `FrameThumbnailRequest.callback`（570/1511 行）存储后从未被调用（结果实际经 bridge 投递）。LRUCache 全类无类型参数。
  - 建议: 统一 `Callable[...]` 注解；删除死导入、死 DTO 与死参数；LRUCache 泛型化。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：不持有 ModernGL/OpenGL 资源；QPixmap 严格只在主线程创建（worker 产出 QImage，经 _ThumbnailSignalBridge 队列连接投递，QImage.scaled 线程安全）
- 信号防递归：回调单向投递（worker→bridge→主线程回调），无同信号重入回路；`clip_thumbnail_set_ready` 缓存命中同步 emit 属正常 UI 通知
- 取消/异常路径：generation + cooperative `cancelled()` 双保险；`set_playback_active`/`invalidate_pending_work` 清理 pending、conversion 队列并对每个 key 回调释放 UI 状态（回调 try/except 隔离）；`_on_clip_thumbnails_loaded` 丢弃过期代次结果且 pending 已由取消方清理，无泄漏
- VideoCapture 池：取出/归还在 per-file lock 内进行，同文件串行；异常路径 finally 归还，`_return_capture` 释放未打开句柄；池上限（每文件 2 / 总 20）防句柄失控
- Topos 分支资源释放：probe_src 与 source 均 try/finally close；`read_topos_frame_rgb` 的 target_size 缩放解码正确传递（G8）
- 输入验证：`topos://` 虚拟素材提前返回（get_thumbnail 1021 行、ClipThumbnailWorker 1680 行）；窄 clip（<30px）跳过；帧号 `max(0, min(...))` 夹取；compute_thumbnail_frames 对 duration<=0 与 num_thumbs==1 均有分支
- 缓存设计：FrameThumbnailKey 含 mtime_ns 实现同路径替换自动失效；MemoryAwareLRUCache 双预算（条数+字节）驱逐正确（重复 put 先减旧值）；ClipThumbnailCache get_compatible 防缩放闪烁；_cap_pool 空目录项清理
- 主线程节流：clip QPixmap 转换每 GUI tick 4 张（PIXMAP_CONVERSIONS_PER_GUI_TICK），播放期间清空队列优先交互
- QImage 构造统一 `.copy()` 解耦 numpy 缓冲，避免悬垂引用

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 4 | AUD-35701, AUD-35711, AUD-35712, AUD-35713 |
| Suggestion | 9 | AUD-35702, AUD-35703, AUD-35704, AUD-35714, AUD-35715, AUD-35716, AUD-35717, AUD-35718, AUD-35719 |
