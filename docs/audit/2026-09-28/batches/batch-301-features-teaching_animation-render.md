# batch-301 审查报告 — src/features/teaching_animation/render/

- 任务书：docs/audit/2026-09-28/assignments/batch-301.md（编号区间 AUD-30100–AUD-30199）
- 文件：4 个 / 1,575 行，全部通读；跨模块引用（core/model.py、core/design_tokens.py、core/vector_shapes.py、render/advanced.py、render/svg_renderer.py、render/html_renderer_spike/playwright_backend.py、src/shared/keyframe.py、调用方 export_service.py 与 render_cache_worker.py）已核实
- 计数：Critical 0 / Major 7 / Suggestion 11

---

## src/features/teaching_animation/render/html_native_overlay.py (行数: 618)

### 发现

- [AUD-30100] [Major] CSS 动画名直接内插未转义的 element_id，可注入/破坏整份 overlay 样式
  - 位置: src/features/teaching_animation/render/html_native_overlay.py:446（同时影响 452、456、484、486）
  - 证据:
    ```python
    selector = f'[data-topos-native-element="{escape(element.element_id, quote=True)}"]'
    for prop in _ANIMATABLE_SINGLE:
        if prop in tracks:
            groups[f"topos-a-{element.element_id}-{prop}"] = ((prop,), selector)
    ```
    ```python
    blocks.append(f"@keyframes {name}{{{''.join(frames_css)}}}")
    blocks.append(
        f"{selector}{{animation:{name} {duration_seconds:.4f}s linear both paused;"
    ```
  - 影响: 选择器里的 id 做了 `escape(quote=True)`，但 keyframes 名用了原始 id。`SceneElement.from_dict`（core/model.py:176-184）接受任意非空字符串 id（`__post_init__` 仅查空，model.py:150-151），导入/持久化的场景文件中含空格、引号、`{}`、`;` 等字符的 id 会生成非法 CSS——轻则整份 `<style>` 解析失败导致所有原生层动画失效（渲染错误），重则注入任意 CSS 规则到 Chromium 预览/采集页（CSP 只限制资源加载，不限制样式规则本身）。
  - 建议: 将动画名改为对 id 的确定性安全映射（如 `re.sub(r'[^A-Za-z0-9_-]', '_', id)` 或哈希后缀），或校验/拒绝含 CSS 元字符的 element_id。
  - 置信度: 高（代码事实确定；实际触发依赖外部场景数据）

- [AUD-30101] [Major] `native_overlay_css_variables` 把未校验的 token 名/值直接拼进 CSS 规则
  - 位置: src/features/teaching_animation/render/html_native_overlay.py:609
  - 证据:
    ```python
    def native_overlay_css_variables(tokens: Any) -> str:
        """A trusted style rule exposing scene tokens as --topos-* variables."""
        from ..core.design_tokens import css_variables
        declarations = ";".join(f"{name}:{value}" for name, value in css_variables(tokens).items())
        return f"#topos-native-vector-overlay{{{declarations}}}" if declarations else ""
    ```
    而 `DesignTokenSet.from_dict._colors`（core/design_tokens.py:78-81）为 `{str(k): str(v) for k, v in dict(values or {}).items() if isinstance(v, str)}`，键值均无字符集/hex 校验。
  - 影响: 场景 metadata / 品牌主题文件属外部数据，恶意的 palette 名（含 `}`/`;`）或值（如 `red} *{...}`）会逃出 `--topos-*` 声明注入 CSS 或破坏整条规则。与模块 docstring "all values come from the validated scene model / browser-safe" 的声明相悖；对比同文件对元素样式值均走 `_color()`/`_HEX_COLOR` 白名单。
  - 建议: 在 `css_variables` 或此处对名称做 CSS ident 白名单（`[A-Za-z0-9_-]`），对 palette 值复用 `_HEX_COLOR` 校验，不匹配则丢弃该声明。
  - 置信度: 中（机制确定；利用受 CSP 限制，主要是渲染破坏面）

- [AUD-30102] [Suggestion] chart/draw 内容解析失败静默返回空串，图层无声消失
  - 位置: src/features/teaching_animation/render/html_native_overlay.py:340
  - 证据:
    ```python
    try:
        chart = ChartData.from_records(content.get("records", [])) if "records" in content \
            else ChartData.from_json(json.dumps(content.get("data", content)))
        ...
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    ```
  - 影响: 坏数据下 chart 层直接从导出/预览中消失且无诊断。同文件对缺失图片资产显式实现了"visible placeholder, never silently drop"（299-307 行注释与实现），策略不一致。
  - 建议: 与图片占位策略对齐——返回一个可见的"invalid chart data"占位 SVG（或至少在 debug 通道记录原因）。
  - 置信度: 高

- [AUD-30103] [Suggestion] 死变量 `previous`
  - 位置: src/features/teaching_animation/render/html_native_overlay.py:462
  - 证据:
    ```python
    frames_css: list[str] = []
    previous: tuple[float, ...] | None = None
    values: list[tuple[int, tuple[float, ...], tuple[float, ...]]] = []
    ...
    del previous
    ```
  - 影响: `previous` 赋值后从未参与逻辑，仅被 `del`；属残留代码，干扰阅读 compact 去重逻辑。
  - 建议: 删除该变量及 `del previous`。
  - 置信度: 高

### 通过项
- 线程安全：纯字符串生成函数，无共享可变状态，可任意线程调用
- GPU 资源生命周期 / GLSL·HDR：不涉及 OpenGL 资源与着色器（HTML/SVG 文本输出路径），N/A
- 信号防递归 / 状态提交一致性：非 Qt 对象、无 undo/持久化职责，N/A
- 输入验证：文本内容经 `html.escape` 转义；颜色（`_HEX_COLOR`）、字体（`_FONT`）、字重、对齐、blend 均白名单校验；数值一律 `_clamp`/`_number` 容错
- 层级渲染对父缺失/成环安全：环上元素不落入 roots 即不渲染，递归沿 children 边有限展开，无无限递归
- 性能：z 排序 O(n log n)；keyframe 采样后做 hold 压缩，动画 CSS 体量受控
- 图片资产缺失有可见占位（不静默丢层）；`scene_asset_uri_lookup` 的宽 except 有 noqa 注释且限定在诊断路径，渲染不因资产解析失败中断

---

## src/features/teaching_animation/render/html_preview_renderer.py (行数: 37)

### 发现

- [AUD-30110] [Suggestion] `_context` 为死状态：赋值后从未被读取
  - 位置: src/features/teaching_animation/render/html_preview_renderer.py:14
  - 证据:
    ```python
    def __init__(self) -> None:
        self._renderer = SvgSceneRenderer()
        self._context: RenderContext | None = None

    def prepare(self, scene: SceneJson, context: RenderContext) -> None:
        self._context = context
        self._renderer.prepare(scene, context)
    ```
    （`cleanup` 中仅置 `self._context = None`，全文件无读取点）
  - 影响: 保存的 context 从未使用，属冗余状态；读者会误以为本类持有渲染上下文生命周期。
  - 建议: 删除 `_context` 字段及相关赋值，或让其真正参与 `render_html`（例如背景/尺寸取自 context）。
  - 置信度: 高

### 通过项
- 薄适配层（37 行）：`render_frame`/`render_html` 直通内部渲染器，无状态突变、无自持资源；`cleanup` 正确委托并复位
- 线程安全 / GPU / 信号 / 异常路径：无相应职责，N/A；导出用 SVG 字节即预览用字节，契约一致

---

## src/features/teaching_animation/render/html_realtime_recorder.py (行数: 650)

### 发现

- [AUD-30120] [Major] VP8 路径 ffmpeg stderr=PIPE 全程不排水，长录制可致导出线程永久死锁
  - 位置: src/features/teaching_animation/render/html_realtime_recorder.py:292（Popen stderr=PIPE）；351、353（阻塞 put）；377-378（先 wait 后读 stderr）
  - 证据:
    ```python
    process = subprocess.Popen(
        [
            ffmpeg, "-y",
            "-f", "image2pipe", "-vcodec", "png",
            ...
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    ```
    ```python
    if not cancelled and last_frame is not None:
        while selected_frames < expected_frames:
            frame_queue.put(last_frame)
            selected_frames += 1
    frame_queue.put(None)
    ...
    process.wait()
    stderr = process.stderr.read() if process.stderr is not None else b""
    ```
  - 影响: 命令未加 `-nostats`/`-v error`，ffmpeg 默认向 stderr 持续输出 stats/日志。stderr 只在 `process.wait()` 之后才读（377-378），无人并发排水；当累计字节超过 OS 管道缓冲（Linux 64KB / macOS 默认 16KB）时 ffmpeg 阻塞在 stderr 写 → 停止消费 stdin → writer 线程阻塞 → frame_queue（maxsize=48）填满 → 结尾的阻塞 `put`（351/353）在主线程上永久挂起，导出线程死锁，无异常无进度。较长时长/较低编码速度的导出风险最高。
  - 建议: stderr 交给独立线程排水（或在 writer 线程里同时读 stderr）；命令加 `-nostats -v error`；`process.wait()` 设超时并 kill 兜底。
  - 置信度: 中（机制确定；触发时长取决于平台管道缓冲与日志量）

- [AUD-30121] [Major] writer 线程死亡后，收尾的阻塞 put 在无消费者的队列上永久挂起
  - 位置: src/features/teaching_animation/render/html_realtime_recorder.py:245（except 吞错后退出整个消费循环）；349-353（阻塞 put）
  - 证据:
    ```python
    def write_frames() -> None:
        assert process is not None and process.stdin is not None
        try:
            while True:
                payload = frame_queue.get()
                if payload is None:
                    break
                process.stdin.write(payload)
            process.stdin.close()
        except BaseException as exc:  # pragma: no cover - process failure path
            writer_errors.append(exc)
    ```
    ```python
    if not cancelled and last_frame is not None:
        while selected_frames < expected_frames:
            frame_queue.put(last_frame)
            selected_frames += 1
    frame_queue.put(None)
    ```
  - 影响: 若 ffmpeg 提前退出（如 libvpx 缺失、输出路径非法），writer 第一次 `stdin.write` 抛 BrokenPipeError 后即终止消费；队列随即填满（48 帧），录制结束时填充循环或 `put(None)` 阻塞在满队列上——`writer_errors` 分支（380 行 RuntimeError）永远不可达，导出线程挂死而非报错。仅当 writer 死亡后推入帧数 < 48 时错误才能浮出。
  - 建议: 收尾统一改用带超时的 put（或 `put_nowait` + 检测 writer 存活/`writer_errors` 非空即放弃排队）；发现 writer 已死时直接 kill ffmpeg 并抛 RuntimeError。
  - 置信度: 高（纯代码逻辑，触发依赖 ffmpeg 提前退出）

- [AUD-30122] [Major] 录制守卫移除失败被静默吞掉，可导出整片隐藏（空白）视频且报告成功
  - 位置: src/features/teaching_animation/render/html_realtime_recorder.py:125（吞异常）；97-99（守卫样式 `html{visibility:hidden}`）；520-521（默认路径使用）
  - 证据:
    ```python
    def _remove_recording_guard(page: object) -> None:
        try:
            page.evaluate(
                """() => {
                  const node = document.getElementById('topos-realtime-recording-guard');
                  if (node) node.remove();
                }"""
            )
        except Exception:
            # The visible page is still authoritative; if the guard cannot be
            # removed, the subsequent screenshot/video validation will fail.
            pass
    ```
    ```python
    if not native_webm:
        _remove_recording_guard(page)
    ```
  - 影响: 守卫把整页设为 `visibility:hidden`。注释声称“后续 screenshot/video 校验会失败”，但本模块结束后只检查文件存在与大小（383-384 / 639-640），直接调用方 export_service.py 也仅检查 `result.cancelled` 后即报成功——并不存在所述校验。`evaluate` 一旦失败（页面崩溃等），默认 ProRes/VP9/H264 路径会录制整段隐藏页面并交付“成功”的空白视频。
  - 建议: 移除失败时验证守卫确已不存在（重读 DOM），否则中止录制并抛错/返回失败，而不是依赖不存在的下游校验。
  - 置信度: 中（触发罕见——页面存活时 evaluate 少见失败；后果为静默错误交付）

- [AUD-30123] [Suggestion] `transparent` 参数被接受但两条录制路径均未使用
  - 位置: src/features/teaching_animation/render/html_realtime_recorder.py:406
  - 证据:
    ```python
    allow_gpu_apis: bool = False,
    transparent: bool = False,
    ffmpeg_binary: str | None = None,
    ```
    （`_record_high_quality_vp8` 的实参列表 433-452 不含 transparent；两路径滤镜均为 `format=yuv420p`，无 alpha 通道）
  - 影响: 调用方一旦传 `transparent=True` 会静默得到不透明视频（当前两处调用方恰好都传 False，暂无实际损害）。契约与实现不符，属埋雷参数。
  - 建议: 要么实现（WebM VP8/VP9 带 alpha 的 `yuva420p` 路径），要么显式 `if transparent: raise ValueError` 或从签名移除。
  - 置信度: 高

- [AUD-30124] [Suggestion] `_probe_duration` / `_probe_output_fps_and_frames` 的 `ffmpeg_binary` 参数被忽略
  - 位置: src/features/teaching_animation/render/html_realtime_recorder.py:128-131（同 145-150）
  - 证据:
    ```python
    def _probe_duration(ffmpeg_binary: str, video: Path) -> float | None:
        """Read the recorder's actual duration before trimming its warm-up."""
        ffprobe = find_ffprobe()
        if not ffprobe:
            return None
    ```
  - 影响: 形参从未使用，函数内部重新定位 ffprobe；调用方传入 `options.ffmpeg_binary` 的自定义工具链路径时探测工具可能不一致（ffmpeg 用了指定路径、ffprobe 用全局查找结果），误导维护者。
  - 建议: 删除该参数，或据此参数推导同目录 ffprobe。
  - 置信度: 高

- [AUD-30125] [Suggestion] duration/fps 非正值静默钳制，与 width/height 的显式 ValueError 不一致
  - 位置: src/features/teaching_animation/render/html_realtime_recorder.py:425
  - 证据:
    ```python
    if width <= 0 or height <= 0:
        raise ValueError("recording surface must be positive")
    duration = max(0.001, float(duration_seconds))
    target_fps = max(0.001, float(fps))
    ```
  - 影响: 传错 duration/fps（如 0 或负数）不会报错，而是产出 1 帧的“成功”视频，问题被掩盖到交付侧。
  - 建议: 对 duration/fps 同样显式校验并抛 ValueError。
  - 置信度: 高

- [AUD-30126] [Suggestion] `import shutil` 未使用
  - 位置: src/features/teaching_animation/render/html_realtime_recorder.py:20
  - 证据: `import shutil`（全文件无第二处引用）
  - 影响: 死导入，微小噪音。
  - 建议: 删除。
  - 置信度: 高

### 通过项
- 输入验证：width/height 显式 ValueError；bitrate 有 max(100,…) 下限
- 取消路径：progress_callback 返回真值即停止等待循环并返回 cancelled 结果（VP8 路径 kill 进程；通用路径不再执行 ffmpeg），行为一致
- 安全：UrlPolicy + `backend._intercept` 路由拦截、`_inject_csp_meta` 注入 CSP、Math.random 种子化保证预览/导出一致
- 线程模型：同步 Playwright 单线程驱动，回调在同线程分发，`selected_frames/last_index/last_frame` 无数据竞争
- 资源清理：TemporaryDirectory 上下文 + finally 中 `context.close()`；`_thread_browser` 线程本地浏览器按设计保留
- 通用（非 VP8）ffmpeg 路径用 `communicate()` 并发排水 stderr，无同类管道死锁
- 帧数契约：VP8 路径 `-frames:v expected_frames` + 缺槽填充保证输出帧数精确等于 duration×fps

---

## src/features/teaching_animation/render/html_renderer_service.py (行数: 270)

### 发现

- [AUD-30130] [Major] `release()` 在 `render_frame` 的 finally 中调用 `self._factory()`，工厂异常会覆盖返回值且使池永久缩水
  - 位置: src/features/teaching_animation/render/html_renderer_service.py:118（release 内 factory 调用）；204-205（finally 调 release）
  - 证据:
    ```python
    finally:
        self.release(worker, healthy=healthy)
    ```
    ```python
    if healthy:
        self._idle.put(worker)
    else:
        # Discard the poisoned worker; the factory supplies a fresh one so
        # the pool size stays constant across crashes.
        _close_quietly(worker)
        self._idle.put(self._factory())
    ```
  - 影响: worker 崩溃（healthy=False）时若替换工厂抛异常（浏览器启动失败），异常从 finally 中升起，替换掉 try 块已构造好的 `CaptureFailure` 返回值——违反模块自述的 fail-closed 契约（"returns the structured CaptureFailure ... never a stale or blank frame"）；且 `_close_quietly` 已丢弃旧 worker、新 worker 未入池，池永久少一个成员，后续该槽位的等待必然超时。
  - 建议: `release` 内用 try/except 包住 factory：失败时记录并保持池不缩水（如惰性重建标记），绝不让异常逃出 finally；或把工厂重建移出 finally。
  - 置信度: 高

- [AUD-30131] [Major] `close()` 与 `release()` 之间存在竞态，可泄漏整个 worker（Chromium 进程）
  - 位置: src/features/teaching_animation/render/html_renderer_service.py:105-113（release 无锁读 `_closed` 后 put）；210-223（close 置位后 drain）
  - 证据:
    ```python
    def release(self, worker: FrameCaptureBackend, *, healthy: bool = True) -> None:
        """Return a worker to the pool, or replace it if it crashed."""
        if self._closed:
            _close_quietly(worker)
            return
        ...
        if healthy:
            self._idle.put(worker)
    ```
    ```python
    with self._lock:
        if self._closed:
            return
        self._closed = True
    drained: list[FrameCaptureBackend] = []
    while True:
        try:
            drained.append(self._idle.get_nowait())
        except queue.Empty:
            break
    ```
  - 影响: `release` 读取 `self._closed` 不持锁。时序：在飞行捕获的 release 通过了 closed 检查 → `close()` 完整执行（置位 + drain 空池）→ release 把 worker `put` 回 `_idle`。该 worker 从此无人关闭——每个池成员是一个浏览器后端，结果是进程/句柄泄漏，且服务已标记 closed 无法补救。
  - 建议: `release` 的 检查+put 全程持 `self._lock`（`close` 的 drain 亦在锁内重查队列），或 put 后复查 `_closed` 并立即 `_close_quietly`。
  - 置信度: 中（机制确定；窗口为并发关闭时在飞行渲染完成瞬间）

- [AUD-30132] [Suggestion] 服务并发关闭时 `acquire()` 的 RuntimeError 可逃出 fail-closed 契约
  - 位置: src/features/teaching_animation/render/html_renderer_service.py:97（acquire 抛 RuntimeError）；166-169（仅捕获 TimeoutError）
  - 证据:
    ```python
    def acquire(self, timeout: float | None = None) -> FrameCaptureBackend:
        """Take an idle worker, blocking up to ``timeout`` if the pool is empty."""
        if self._closed:
            raise RuntimeError("HtmlRendererService is closed")
    ```
    ```python
    try:
        worker = self.acquire(timeout=min(remaining, _QUEUE_POLL_S))
    except TimeoutError:
        continue
    ```
  - 影响: 队列循环每轮先自查 `self._closed`（156 行返回 CaptureFailure），但与 `acquire` 之间存在窗口：关闭恰好落在检查之后、acquire 之内时，RuntimeError 从 `render_frame` 抛出而非返回 `CapturedFrame | CaptureFailure` 联合类型，按联合类型编写的调用方可能未捕获而线程崩溃。
  - 建议: 队列循环同时捕获 RuntimeError（closed）并转换为 `CaptureFailureCode.CRASH`/`CANCELLED` 失败对象。
  - 置信度: 中

- [AUD-30133] [Suggestion] `backend_build` 属性无超时阻塞获取 worker
  - 位置: src/features/teaching_animation/render/html_renderer_service.py:88
  - 证据:
    ```python
    @property
    def backend_build(self) -> str:
        """The pinned build of the pooled workers (stable across the pool)."""
        # All workers come from the same factory, so any one reports the build.
        worker = self.acquire()
    ```
  - 影响: `acquire()` 默认 `timeout=None` 即无限阻塞。池中 worker 全忙（并发导出进行中）时，读取该属性（常用于日志/诊断）会卡到某次采集结束，与服务其余部分“有界等待”的设计相悖。
  - 建议: 缓存构建号（首个 worker 创建时记录一次），或 `acquire(timeout=...)` 失败时返回占位/抛 TimeoutError。
  - 置信度: 高

- [AUD-30134] [Suggestion] `__init__` 中工厂中途失败时已创建 worker 泄漏
  - 位置: src/features/teaching_animation/render/html_renderer_service.py:74-75
  - 证据:
    ```python
    for _ in range(pool_size):
        self._idle.put(self._factory())
    ```
  - 影响: `pool_size > 1` 且第 N 个 factory 调用抛异常时，前 N-1 个已创建的浏览器后端无人关闭，构造异常直接外抛，进程泄漏。
  - 建议: 包 try/except，失败时逐个 `_close_quietly` 已建 worker 再重抛。
  - 置信度: 高

- [AUD-30135] [Suggestion] 未使用的 `Any` 导入
  - 位置: src/features/teaching_animation/render/html_renderer_service.py:35
  - 证据: `from typing import Any, Callable, Mapping`（`Any` 全文件未使用）
  - 影响: 死导入。
  - 建议: 移除 `Any`。
  - 置信度: 高

### 通过项
- 线程安全核心：池经 `queue.Queue` 分发，队列等待按 0.2s 轮询切片，取消/超时感知及时
- fail-closed 主路径：捕获后端异常并归类 CRASH、替换 worker，不返回陈旧/空白帧
- 资源隔离：`_bind_asset_resolver`/`_clear_asset_resolver` 在每次调用绑定、release 时清空，防止跨请求资产泄漏（并已核实 PlaywrightChromiumBackend 读取同名实例属性）
- `close()` 幂等（锁保护置位）；`__enter__`/`__exit__` 上下文协议正确
- `inspect.signature` 探测 `allow_uniform` 参数保持对旧协议后端兼容，宽 except 限定 (TypeError, ValueError) 且有注释说明
- GPU/GLSL/HDR、信号防递归、状态提交：本模块为进程池编排层，相应维度 N/A
