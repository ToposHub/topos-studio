# batch-177 — src/features/edit/core/render_cache_worker.py

编号区间：AUD-17700–AUD-17799。审查方式：通读全文件（873 行），并交叉核对
`render_cache_encoder.py`、`timeline_preview_renderer.py`（`PreviewRenderWorker._composite_layers` /
`_get_decoded_frame_for_cache_passthrough` / `cleanup_clips`）、`render_cache.py`（manifest）、
`timeline_v2.py`（`TimelineSequence`/`ClipInstance` 均为 `@dataclass(frozen=True)`）、
`edit_controller.py`（worker 创建/inflight 登记/终止回调）以验证每条发现的实际可达性。

## src/features/edit/core/render_cache_worker.py (行数: 873)

### 发现

- [AUD-17700] [Major] run() 在 try 块之前的初始化代码抛异常时不发 failed 信号，该 clip 的缓存渲染在本次会话内被永久锁死
  - 位置: src/features/edit/core/render_cache_worker.py:388-417（对照 541-544）
  - 证据:
    ```python
    fps_num, fps_den = self._parse_fps(self._manifest.timeline_fps)   # 388
    ...
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)   # 397
    ...
    encoder = RenderCacheEncoder(encoder_config)                      # 414

    try:                                                              # 417
    ```
    `try:` 之前只有 `duration <= 0`（383-385）走 `failed.emit`。`_parse_fps` 只捕获
    `(ValueError, IndexError)`（872），`timeline_fps` 为 `None`（`from_dict` 对
    `"timeline_fps": null` 透传，render_cache.py:278）时 `'/' in fps_str`（868）抛
    TypeError；`os.makedirs` 对非法/锁定路径抛 OSError；这些异常直接逃出 `run()`，
    541-544 的 `except → encoder.abort() → failed.emit` 永不执行。
  - 影响: 控制器在 `edit_controller.py:7810-7811` 登记 inflight `(timeline_id, clip_id)`，
    且只在 `failed`/`finished_ok` 终止回调（7889-7896 / 7971+）清除；发不出信号则
    7493-7496 的 inflight 守卫对本会话内该 clip 的每次请求都返回 "already
    rendering"——渲染功能被锁死直至重启，且错误对用户完全不可见（仅 stderr 回溯），
    RENDERING 状态被持久化进工程。
  - 建议: 将 388-414 全部纳入 try/except（或把 try 提到 run() 顶部），并为
    `_parse_fps` 增加 None/str 强制转换；保证 `run()` 任何退出路径都发出终止信号。
  - 置信度: 高

- [AUD-17701] [Suggestion] motion_graphic 第二分支与 `_render_motion_graphic_frames` 为不可达死代码
  - 位置: src/features/edit/core/render_cache_worker.py:447-460、789-848、427-430
  - 证据:
    ```python
    if self._manifest.render_kind == "motion_graphic":      # 425
        scene = self._motion_graphic_scene()
        ...
        self._render_motion_graphic_recording(scene, output_path)
        return                                              # 442
    # Open encoder
    encoder.open()                                          # 445
    if self._manifest.render_kind == "motion_graphic":      # 447 — 恒 False
        self._render_motion_graphic_frames(encoder)
    ```
    425 分支对全部 motion_graphic 情形要么 raise（→ 541 捕获）要么 442 return；
    447 的同名条件永假。另 `_motion_graphic_scene` 找不到场景时直接 raise（559），
    故 427-430 的 `if scene is None` 亦为死分支。
  - 影响: 60 行的逐帧浏览器采集路径（789-848，注释称已被实时录制替代）与
    `TimelineSceneFrameProvider` 等导入长期滞留，误导后续维护者以为该路径仍会执行；
    445 的 `encoder.open()` 也白白排在永假检查之前。
  - 建议: 删除 447-460 与 `_render_motion_graphic_frames`（或如果保留旧路径是
    有意的回退设计，用独立 render_kind 值/显式开关表达）。
  - 置信度: 高

- [AUD-17702] [Suggestion] `_parse_fps` 未处理 None/TypeError，小数 fps 有损取整
  - 位置: src/features/edit/core/render_cache_worker.py:865-873
  - 证据:
    ```python
    try:
        if '/' in fps_str:
            parts = fps_str.split('/')
            return int(parts[0]), int(parts[1])
        return int(float(fps_str)), 1
    except (ValueError, IndexError):
        return 24, 1
    ```
    - `fps_str=None`（外部工程数据）→ `'/' in None` 抛 TypeError，不在捕获列表
      （后果并入 AUD-17700）。
    - 小数字符串 `"29.97"` → `(30, 1)`、`"23.976"` → `(24, 1)`：0.1% 的时间基误差，
      缓存回放速度与时间线错拍（长片段累积漂移）。
  - 影响: 畸形 fps 静默崩溃（见 AUD-17700）；合法小数 fps 被四舍五入，缓存时长/速度失真。
  - 建议: 捕获 TypeError 并先做 `str(fps_str or "")` 规整；小数 fps 用十进制逼近
      （如 `Fraction(Decimal(fps_str)).limit_denominator(1001)`）转精确分数。
  - 置信度: 中（机制确定；触发依赖手工编辑/旧工程的非法或小数 fps）

- [AUD-17703] [Suggestion] HTML 实时录制将浏览器视口设为 manifest 尺寸而非场景 authored 尺寸
  - 位置: src/features/edit/core/render_cache_worker.py:683-687（对照 636-639 与死代码注释 812-819）
  - 证据:
    ```python
    result = record_html_video(
        document,
        temp_path,
        width=max(1, int(self._manifest.width)),    # 686 — 浏览器视口=缓存尺寸
        height=max(1, int(self._manifest.height)),  # 687
    ```
    而被替代的确定路径注释明确警告（812-819）："shrinking the browser viewport
    before layout makes absolute HTML designs crop and lose their intended
    composition"，故其 RenderContext 用 `scene.width/height`；实时路径的
    RenderContext（636-639）也用场景尺寸，仅录制视口用 manifest 尺寸。
  - 影响: 场景 authored 宽高比与 manifest 宽高比不一致（如竖版场景进横版时间线）时，
    HTML 在错误几何下重排/裁剪，缓存画面构图失真。
  - 建议: 录制视口与 RenderContext 统一取场景 authored 尺寸，编码阶段再缩放到
    manifest 尺寸；或在场景/时间线宽高比不一致时显式失败。
  - 置信度: 低（控制器侧 cache 尺寸由 `motion_graphic_preview_size` 等比缩放而来，
    常规场景与时间线同几何时无影响；仅在宽高比错配时触发）

- [AUD-17704] [Suggestion] 实时录制结果的帧数未与 manifest 时长校验
  - 位置: src/features/edit/core/render_cache_worker.py:713-728
  - 证据:
    ```python
    if result.cancelled or self._cancelled:
        ...
        return
    if not temp_path.is_file() or temp_path.stat().st_size <= 0:
        raise RuntimeError("real-time HTML cache recorder produced no video")
    os.replace(str(temp_path), str(output_path))
    ```
    完成校验只查文件存在与大小 > 0；`result.frame_count` 仅用于日志（727），未与
    `duration_frames`（或 `round(duration_seconds * fps)`）比对。
  - 影响: Chromium 实时采集掉帧/提前结束时产出比 manifest 短的缓存，播放到尾部
    越界，行为取决于消费端解码器的越界钳制。
  - 建议: `abs(result.frame_count - duration_frames)` 超容差（如 >1 帧）时报
    failed 而非安装残缺缓存。
  - 置信度: 低（未核实 `record_html_video` 内部是否已保证帧数/时长）

- [AUD-17705] [Suggestion] dtype 分派 else 分支把非 uint8/float32/float64 帧按 uint8 直送编码器
  - 位置: src/features/edit/core/render_cache_worker.py:512-516
  - 证据:
    ```python
    if result.dtype in (np.float32, np.float64):
        frame_u8 = np.clip(result * 255.0, 0, 255).astype(np.uint8)
    else:
        frame_u8 = result
    ```
    `write_frame_bgr` 无条件声明 `dtype=np.dtype('uint8')`（render_cache_encoder.py:134），
    uint16/int16/float16 等帧会被按 uint8 重新解释，静默产出花屏缓存。
  - 影响: 当前 `_composite_layers` 文档返回 BGR uint8（timeline_preview_renderer.py:2660），
    属潜在缺陷；一旦上游改产 10bit/float16，缓存内容静默损坏且无报错。
  - 建议: else 分支收紧为 `assert result.dtype == np.uint8`（或显式转换 + 失败上报）。
  - 置信度: 中（机制确定；现有调用链不触发）

- [AUD-17706] [Suggestion] 资源清理路径 `except Exception: pass` 完全无日志
  - 位置: src/features/edit/core/render_cache_worker.py:327-337、545-550
  - 证据:
    ```python
    finally:
        if cpu_composite_renderer is not None:
            try:
                cpu_composite_renderer.close()
            except Exception:
                pass
    ```
    `_CpuCompClipRenderer.close` 内部对 `cleanup_clips()` 之外的
    `_clear_direct_preview_sources` / `_decode_executor.shutdown` 同样裸 pass（331-337）。
  - 影响: 解码线程池/直连解码源清理失败时无任何痕迹，后台线程或句柄泄漏不可诊断
    （违背 .clinerules "不得宽泛 except 掩盖领域错误" 的精神；清理场景可容忍吞异常，
    但应留痕）。
  - 建议: 统一改为 `except Exception: logger.debug("...", exc_info=True)`。
  - 置信度: 高

- [AUD-17707] [Suggestion] `_resolve_source_clip` 在引用 timeline 缺失时静默回退到任意第一条 timeline
  - 位置: src/features/edit/core/render_cache_worker.py:139-141
  - 证据:
    ```python
    timeline = timelines.get(timeline_id) if timeline_id else None
    if timeline is None and timelines:
        timeline = next(iter(timelines.values()))
    ```
  - 影响: manifest/graph 携带过期 `timeline_id` 时不报错，而是拿不确定顺序的
    timeline 继续解析 clip；宽进了外部数据验证（clip_id 为 uuid，实际错配概率低）。
  - 建议: 回退命中后 `logger.warning` 记录实际使用的 timeline_id，或仅在
    `timelines` 恰有一条时才回退。
  - 置信度: 高（行为确定，影响小）

- [AUD-17708] [Suggestion] 旧版相对路径回退以进程 CWD 为根，缓存文件可能落到任意目录
  - 位置: src/features/edit/core/render_cache_worker.py:392-397
  - 证据:
    ```python
    if not os.path.isabs(output_path):
        # Legacy manifests used ``Cache/...``. ...
        output_path = os.path.join("Cache", os.path.basename(output_path))
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    ```
  - 影响: 相对路径 manifest（旧工程）下，`Cache/` 目录建在应用启动时的 CWD
    （如 Home 目录），缓存文件散落且难以随工程迁移；控制器侧对 cache_root 有
    工程目录锚定（edit_controller.py:7585-7589），worker 侧回退未对齐。
  - 建议: 回退时锚定到工程文件目录（与控制器逻辑同源），或对相对路径直接报
    failed 提示重新生成 manifest。
  - 置信度: 高（行为确定；新 manifest 均为绝对路径，仅旧数据触发）

- [AUD-17709] [Suggestion] 跨类访问 `PreviewRenderWorker` 私有成员
  - 位置: src/features/edit/core/render_cache_worker.py:116-119、269、299-307、331-335
  - 证据:
    ```python
    self._worker._active_decode_target_size = (...)          # 116-119 写入
    return self._worker._composite_layers((layer,), (), sample_frame)  # 269
    asset_path = worker._asset_resolver.get_asset_path(...)  # 299
    self._worker._clear_direct_preview_sources()             # 331
    self._worker._decode_executor.shutdown(...)              # 335
    ```
  - 影响: 六处下划线私有成员被外部类读写；`PreviewRenderWorker` 内部重构（改名/语义
    变化）会静默破坏缓存渲染，且无接口契约保护。
  - 建议: 在 `PreviewRenderWorker` 上提供公共 API（如
    `set_decode_target_size()` / `composite_for_cache()` / `release()`），worker 改调公共方法。
  - 置信度: 高

- [AUD-17710] [Suggestion] 类型注解缺失：裸 `dict` 泛型与未注解参数
  - 位置: src/features/edit/core/render_cache_worker.py:360-366
  - 证据:
    ```python
    def __init__(
        self,
        manifest: RenderCacheManifest,
        edit_project: Any,
        timeline_sequences: dict,     # 裸 dict，缺键值类型
        parent=None,                  # 无类型注解
    ) -> None:
    ```
  - 影响: 与库内其余签名（如 `timeline_sequences: dict[str, Any]`，本文件 65 行）不一致，
    弱化静态检查。
  - 建议: 改为 `timeline_sequences: dict[str, Any]`、`parent: QObject | None = None`。
  - 置信度: 高

### 通过项
- 线程安全：worker 全部工作在自身 QThread 的 `run()` 内；传入的 `TimelineSequence`/`ClipInstance` 为 `@dataclass(frozen=True)` 不可变快照（timeline_v2.py:207、2925），无跨线程可变共享；`_cancelled` 为 GIL 下原子布尔，UI 线程 `cancel()` 与 worker 轮读安全；信号跨线程发射为 Qt 队列投递。
- GPU 资源生命周期：刻意 headless——`_CpuCompClipRenderer` 构造时拒绝 GPU-only 图节点（87-91），`PreviewRenderWorker` 以 `video_renderer=None` 构造且从不 `start()`；`finally` 调 `close()` 释放解码 clip、直连源与 `_decode_executor` 线程池。
- GLSL·HDR：文件无 GLSL；float→uint8 的 `np.clip(result*255,0,255)` 属显示编码路径且有注释依据（缓存为显示帧）；常量帧 linear→sRGB 公式与 CPU readback 一致（196-202），8-bit 封装下的 clamp 有理由。
- 信号防递归：worker 只发射 `progress_changed/finished_ok/failed`，不触碰任何 UI 控件，无递归环路。
- 状态提交一致性：worker 不改项目/manifest 状态（状态迁移全部由控制器终止回调完成）；编码器 temp→`os.replace` 原子落盘，失败中途保留旧缓存（render_cache_encoder.py:177-184）；实时路径同样 `os.replace`（722）。
- 异常路径（run() 主体）：主循环内 try/except 完整，`encoder.abort()` 对未打开/已中止编码器幂等安全（render_cache_encoder.py:193-217），异常均转 `failed` 信号。
- 取消路径：逐帧检查（482-485）、HTML 录制回调返回 True 中止（679-681）、录制完成后复查（713-719）三处齐全，取消均清理临时文件且不发伪错误。
- 输入验证（核心路径）：`duration <= 0` 拒绝；图节点白名单；planar 直通前依次校验调色/效果/混合模式/资产路径/解码帧/逐像素几何/平面格式白名单（288-320），任一不满足回退合成路径，绝不静默产出不一致缓存；DecodedFrame 几何与 manifest 不符时显式 raise（501-507）。
- 性能模式：planar 直通省去两次整帧格式转换并消除 8-bit 量化损失（271-321，正向优化）；`_active_decode_target_size` 锚定 manifest 分辨率避免预览质量缩放泄漏进缓存（114-119）；进度信号按帧节流符合惯例。
