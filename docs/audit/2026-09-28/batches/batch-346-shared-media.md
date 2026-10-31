# batch-346 [P2] src/shared/media//

编号区间：AUD-34600–AUD-34699。审查文件 1 个：`src/shared/media/factory.py`（1,296 行，已通读全文）。
以下发现均基于真实行号与摘录代码；跨文件事实（构造函数签名、父类行为）已只读核验。

## src/shared/media/factory.py (行数: 1296)

### 发现

- [AUD-34600] [Major] probe 完整回退路径异常时已打开的媒体源未关闭（PyAV 容器泄漏）
  - 位置: src/shared/media/factory.py:757
  - 证据:
    ```python
    try:
        source = cls.create(path)
        source.open()
        metadata = source.probe()
        source.close()
    except Exception as e:  # noqa: BLE001 — 完整探测失败按原语义返回 None
        logger.warning(f"完整探测失败: {e}")
        return MediaProbeResult(
            path=path, signature=sig, metadata=None, backend="none",
    ```
  - 影响: `source.open()` 成功后若 `source.probe()`（或 open 自身中途）抛异常，`source.close()` 被跳过，except 分支也不做清理。`FFmpegMediaSource.open` 打开 av 容器后再失败不会自行关闭（已核验 ffmpeg_source.py:983 起），打开的容器/解码线程将滞留至 GC。批量导入/缩略图探测大量损坏文件时（每次失败都走此回退），会累积未关闭的 PyAV 资源。对照 `_probe_topos_native`（factory.py:791-795）已正确使用 finally close，此处为遗漏。
  - 建议: 改为 `try: ... finally: try: source.close() except Exception: pass`，或复用 `_probe_topos_native` 的 finally 模式。
  - 置信度: 高

- [AUD-34601] [Major] create(auto_open=True) 打开失败后未清理已创建的 source 即切换下一后端
  - 位置: src/shared/media/factory.py:314
  - 证据:
    ```python
    for backend_name in cls._get_backend_order(selected_backend, path):
        try:
            source = cls._create_with_backend(backend_name, path, target_size, preserve_planes)
            logger.info(f"MediaSourceFactory: 使用后端 {backend_name} 创建 {path}, preserve_planes={preserve_planes}")
            # P3.A: 不自动 open，由调用方决定何时打开
            if auto_open:
                source.open()
            return source
        except Exception as e:
            errors.append(f"{backend_name}: {str(e)}")
            logger.warning(f"后端 {backend_name} 失败: {e}")
            continue
    ```
  - 影响: `source.open()` 抛异常（容器已部分打开、解码器建立失败等）时进入 except 直接 continue，已创建且部分打开的 source 从未调用 close()。与同类入口 `open_source()`（factory.py:456-465，失败时显式 close）行为不一致。多后端回退 + 多文件批量加载场景下放大泄漏。
  - 建议: 在 except 分支中对 `source` 做 best-effort `close()`（try/except 包裹），与 open_source() 对齐。
  - 置信度: 中

- [AUD-34602] [Major] 类级探测/嗅探缓存与失败记录集合无上限、无淘汰策略（长会话内存增长）
  - 位置: src/shared/media/factory.py:166
  - 证据:
    ```python
    # T-P0-10：内容嗅探与 probe 结果缓存（键 = 文件路径，值带文件签名；
    # 签名变化自动失效）。
    _BACKEND_DECISION_CACHE: dict[str, tuple[FileSignature, MediaBackendDecision]] = {}
    _PROBE_RESULT_CACHE: dict[str, tuple[FileSignature, MediaProbeResult]] = {}
    # native 失败只记录一次（键 = path + signature + reason_code）
    _NATIVE_FAILURE_LOGGED: set[tuple[str, FileSignature, str]] = set()
    ```
  - 影响: 三个类级容器只增不减（唯一清理入口是测试用的 `clear_media_probe_cache()`，factory.py:841-846）。签名失效只覆盖旧值，不删除条目；`_NATIVE_FAILURE_LOGGED` 对同一文件的每次修改（新签名）都会追加新键。probe 被 import、缩略图、导出、Composite、AI 分析共享调用（文件 docstring 自述），长编辑会话中浏览/导入上千素材或 AI 批量扫描目录时内存单调增长，且 `_NATIVE_FAILURE_LOGGED` 的键持有完整 detail 字符串路径。
  - 建议: 为缓存加 LRU 上限（如 OrderedDict/collections.lru 裁剪）或按签名数量/时间淘汰；`_NATIVE_FAILURE_LOGGED` 可并入带容量限制的结构。
  - 置信度: 高

- [AUD-34603] [Suggestion] 无效 backend 名称被静默按 auto 处理，docstring 承诺的 ValueError 契约落空
  - 位置: src/shared/media/factory.py:331
  - 证据:
    ```python
    # 如果指定了具体后端，直接使用
    if backend == 'ffmpeg':
        return backend
    if backend == 'topos':
        return backend
    if backend == 'gstreamer':
        ...
    # auto 模式：检查用户偏好
    ```
  - 影响: `create()` docstring（factory.py:282）声明 `ValueError: 无效的后端名称`，但 `_select_backend` 对 'ffmpeg'/'topos'/'gstreamer' 之外的任意字符串（如拼写错误 'ffmgeg'）静默落入 auto 分支；且 `_create_with_backend` 的 ValueError（factory.py:430）在 create() 的调用图上实际不可达（auto 顺序里的后端名都来自注册表）。用户配置错误后端名时得到的是静默行为差异而非报错。
  - 建议: 在 `_select_backend` 末尾对未识别的 backend 值 `raise ValueError`（或至少 logger.warning），使契约成立。
  - 置信度: 高

- [AUD-34604] [Suggestion] toos_image 后端工厂静默丢弃 target_size 与 preserve_planes
  - 位置: src/shared/media/factory.py:524
  - 证据:
    ```python
    def create_toos_image(
        path: str,
        target_size: Optional[tuple[int, int]],
        preserve_planes: bool = False
    ) -> MediaSource:
        from .topos_image_source import ToposImageSource
        return ToposImageSource(path)
    ```
  - 影响: `ToposImageSource.__init__(self, path, codec=None)`（topos_image_source.py:87，已核验）不接受这两个参数，工厂直接丢弃。经 `create(path, target_size=(w,h))` 请求代理缩放的 toos 静态图会始终全分辨率解码，4K 图预览/代理路径失去缩放收益；对照 `create_toos_sequence` 已通过 `SequenceOptions(target_size=target_size)` 正确传递。参数被接受却无效果，属于静默契约违约。
  - 建议: 为 ToposImageSource 增加 target_size/preserve_planes 支持，或在丢弃时 logger.warning 提示调用方。
  - 置信度: 高

- [AUD-34605] [Suggestion] 硬解 open 失败回退 CPU 后 HardwareDecodeStatus.decode_path 残留硬解路径标识
  - 位置: src/shared/media/factory.py:950
  - 证据:
    ```python
                        status.hwaccel_enabled = False
                        status.decoder_type = 'cpu'
                        status.fallback_occurred = True
                        status.fallback_reason = (
                            f"hwaccel_open_failed:{type(exc).__name__}:{exc}"
                        )
    ```
    （随后 CPU 路径 factory.py:969-970 也只重置 `hwaccel_enabled`/`decoder_type`，未触碰 `decode_path`）
  - 影响: `decode_path` 在硬解创建成功时已被置为 'macos_vt_iosurface_zero_copy' / 'hwaccel_download_planar_yuv' / 'hwaccel_download_packed_rgb'（factory.py:1043/1163/1203）；open 失败软降级 CPU 后该字段保留旧值，调用方按 status 统计的解码路径与实际 CPU 解码不符，误导性能归因与诊断。
  - 建议: 在 open 失败回退分支和 CPU 路径统一补 `status.decode_path = 'cpu'`。
  - 置信度: 高

- [AUD-34606] [Suggestion] create_with_hwaccel CPU 路径 auto_open 失败抛出原生异常，未包装为 MediaOpenError
  - 位置: src/shared/media/factory.py:975
  - 证据:
    ```python
    if auto_open:
        source.open()

    logger.info(f"MediaSourceFactory: 使用 CPU 解码创建 {path}")
    return source, status
    ```
  - 影响: docstring（factory.py:897-898）只承诺 `Raises: MediaOpenError`；此分支 `source.open()` 的任意底层异常（av.AVError 等）原样逸出。同函数内硬解分支（factory.py:934-955）对 open 失败有完整软降级处理，错误契约不一致，调用方难以统一捕获。
  - 建议: 包装为 MediaOpenError（带 source_path），或与硬解分支对齐降级语义。
  - 置信度: 中

- [AUD-34607] [Suggestion] _probe_topos_native 中 ToposMediaSource 构造位于 try 之外，构造异常会逸出 probe API
  - 位置: src/shared/media/factory.py:784
  - 证据:
    ```python
    try:
        from .topos_source import ToposMediaSource
    except Exception as exc:  # noqa: BLE001
        return cls._topos_native_failure(
            path, sig, "TOPOS_SOURCE_IMPORT_FAILED", str(exc))
    source = ToposMediaSource(str(path))
    try:
        source.open()
    ```
  - 影响: import 失败被分流，但 `ToposMediaSource(...)` 构造本身抛出的异常（路径/库初始化问题）不在任何 try 内，会穿透 `probe_video_metadata` 直达调用方；probe_video_metadata 被 project.py、quick_project_dialog、node_editor_widget 等 UI/导入路径直接调用，未预期构造异常。
  - 建议: 把构造移入下方 try（或在构造处单独 try/except 并走 `_topos_native_failure` 分流）。
  - 置信度: 中

- [AUD-34608] [Suggestion] _classify_topos_open_failure 在 except 处理链内执行未受保护的绝对 import，可掩盖原始异常
  - 位置: src/shared/media/factory.py:803
  - 证据:
    ```python
    from src.shared.codec.topos_binding import (
        LIB_RESOLUTION_INCOMPATIBLE,
        LIB_RESOLUTION_NOT_FOUND,
        resolve_topos_codec_library,
    )
    text = str(exc)
    resolution = resolve_topos_codec_library()
    ```
    （调用点位于 factory.py:788-789 的 `except Exception as exc: reason, detail = cls._classify_topos_open_failure(exc)`）
  - 影响: 该方法在 `_probe_topos_native` 的 except 处理器内执行。若绝对导入失败（包重命名/打包环境缺模块）或 `resolve_topos_codec_library()` 抛错，新异常会替换原始的 open 失败异常向上传播，破坏 probe 的"结构化失败、不伪装成功"语义。
  - 建议: 对 import 与 resolve 调用再加一层 try/except，失败时降级为 `("TOPOS_FILE_OPEN_FAILED", text)`。
  - 置信度: 中

- [AUD-34609] [Suggestion] macOS VT 零拷贝路径：open 抛异常时已构造的 vt_source 未 close
  - 位置: src/shared/media/factory.py:1056
  - 证据:
    ```python
    vt_source = MacOSVtZeroCopySource()
    if vt_source.open(path):
        ...
        return vt_source, status
    else:
        reason = vt_source.fallback_reason or "unknown"
        ...
        vt_source.close()
    except Exception as e:
        logger.debug(f"MediaSourceFactory: macOS VT zero-copy attempt failed: {e}")
    ```
  - 影响: open 返回 False 的分支有显式 close，但 `vt_source.open(path)` 中途抛异常时走 except 分支仅记日志，已构造、可能部分初始化（IOSurface/解码会话）的 vt_source 未清理，只能等 GC。
  - 建议: except 分支中对已绑定的 vt_source 做 best-effort close（需把构造提前到 try 外或用局部变量判空）。
  - 置信度: 中

- [AUD-34610] [Suggestion] 工厂直接写 FFmpegHwaccelSource 私有属性 _target_size 传递 target_size
  - 位置: src/shared/media/factory.py:1194
  - 证据:
    ```python
    source = FFmpegHwaccelSource(path, hwaccel_config)

    # 如果有 target_size，需要设置（FFmpegHwaccelSource 继承自 FFmpegMediaSource）
    if target_size is not None:
        source._target_size = target_size
    ```
  - 影响: `FFmpegHwaccelSource.__init__(self, path, config=None)`（hwaccel.py:646，已核验）不接受 target_size，父类构造用默认值，工厂随后戳私有属性 `_target_size` 补设。绕过公共构造契约，父类一旦改名/在 open 中快照该值（ffmpeg_source.py:473 在 __init__ 赋值、1877 起在读取路径使用），此补设可能失效或被忽略，导致代理尺寸静默丢失。
  - 建议: 给 FFmpegHwaccelSource.__init__ 增加 target_size 形参并显式传给 super()，工厂删除私有属性写入。
  - 置信度: 高

- [AUD-34611] [Suggestion] fps 探测失败时静默默认 24.0，元数据可能失真
  - 位置: src/shared/media/factory.py:668
  - 证据:
    ```python
    # 帧率
    fps_value = 24.0
    if hasattr(stream, 'average_rate') and stream.average_rate:
        fps_value = float(stream.average_rate)
    elif hasattr(stream, 'base_rate') and stream.base_rate:
        fps_value = float(stream.base_rate)
    fps_rational = Rational.from_float(fps_value, max_den=1001)
    ```
  - 影响: average_rate/base_rate 均缺失或为 0 的流（部分容器/原始流）会得到伪装成探测结果的 24fps 元数据，下游时间线时长、总帧数换算随之出错，且调用方无法区分"真实 24fps"与"默认值"。
  - 建议: 缺失时置 None 或在 VideoMetadata 中携带 fps_is_default 标记，由调用方决定回退。
  - 置信度: 中

- [AUD-34612] [Suggestion] 类级可变状态与延迟后端注册无锁，跨线程并发依赖 GIL 原子性
  - 位置: src/shared/media/factory.py:156
  - 证据:
    ```python
    # 后端注册表（延迟加载）
    _BACKEND_REGISTRY: dict[str, Callable[[str, Optional[tuple[int, int]]], MediaSource]] = {}

    # 后端可用性缓存
    _BACKEND_AVAILABLE: dict[str, bool] = {}
    ```
    （`_ensure_backends_registered` factory.py:471-560 与 `register_backend` factory.py:228-245 均无锁）
  - 影响: probe/create 被 import、缩略图、edit_gpu_decode_manager、video_playback 等多线程路径调用；check-then-set 的缓存填充与首次注册在并发下会重复执行（重复探测、check_available 被多次调用）。单键 dict 操作在 CPython 下原子，属良性竞态，不会损坏数据，但行为不可重复且随 PyPy/自由线程 Python 的引入会升级为真实风险。
  - 建议: 加一把模块级 threading.Lock 保护注册与缓存填充（命中路径可保持无锁快读），或在类 docstring 明示线程模型。
  - 置信度: 中

### 通过项

- GPU 资源生命周期（GL 对象）：本文件不创建/持有任何 ModernGL/OpenGL 资源，无 UI 线程 GL 调用（铁律无违反）
- GLSL·HDR 正确性：无 GLSL 代码；位深探测覆盖 10/12/16 le/be 与 p010 常见 HDR pix_fmt，未发现无理由 clamp 或位深降级
- 信号防递归：不适用（无 Qt 控件/信号连接）
- 状态提交一致性：不涉及 undo/redo/revision/持久化；探测/嗅探缓存以 (size, mtime_ns) 文件签名自动失效，文件变更后不会读到陈旧判定
- 异常与取消路径（主设计）：逐后端回退并汇总错误信息、硬件失败软降级 CPU、probe 失败返回结构化 reason_code 不伪装成功；except Exception 均带 noqa 注明分流理由，符合"不掩盖领域错误"的例外用法
- 外部数据访问防御：pix_fmt 经 stream.format 安全通道读取（规避 PyAV 17 对 AV_PIX_FMT_NONE 的 C 层段错误）、average_rate/base_rate/frames/codec_context 均有 hasattr 守卫；topos:// 虚拟素材在 create/create_with_hwaccel/probe 三处一致提前拒绝
- 性能模式：probe/嗅探结果带签名共享缓存（T-P0-10），native 失败日志按 (path, 签名, 原因) 去重防刷屏；_try_create_hwaccel_source 的二次 probe 命中缓存不重复探测
- 类型注解：公开类与方法均有完整参数/返回类型注解（含 TYPE_CHECKING 前置声明），仅个别内部局部变量缺注解
