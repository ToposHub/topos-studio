# batch-209 审查报告 — src/features/edit/ui//

- 批次: batch-209 [P1]，编号区间 AUD-20900–AUD-20999
- 文件数: 3（共 2,895 行），全部完整 Read 通读
- 汇总: Critical 2 / Major 3 / Suggestion 10
- 审查维度: 线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解

---

## src/features/edit/ui/source_viewer.py (行数: 2332)

### 发现

- [AUD-20900] [Critical] paintGL 将渲染线程池纹理 ID 写入 `_current_tex_id`，close_source→clear_texture 会删除外部（OutputSlotPool）拥有的纹理
  - 位置: src/features/edit/ui/source_viewer.py:701（别名建立）；982-991（删除）；1387（触发点）
  - 证据:
    ```python
    # paintGL (698-703)
    tex_id = self._shared_tex_id
    if tex_id is not None:
        # GPU pipeline: shared texture from render thread (no upload needed)
        self._current_tex_id = tex_id
        self._tex_width = self._shared_tex_width
        self._tex_height = self._shared_tex_height
    ```
    ```python
    # clear_texture (983-989)
    if self._current_tex_id is not None:
        _gl.glDeleteTextures([self._current_tex_id])
        self._current_tex_id = None
    # Clear shared texture reference (do NOT delete — owned by render thread)
    self._shared_tex_id = None
    ```
  - 影响: 只要有过一帧 GPU 共享纹理被绘制，`_current_tex_id` 即别名渲染线程 OutputSlotPool 的纹理。`close_source()`（关闭素材、切换素材 open_asset 时同样先调 close_source）调用 `viewer.clear_texture()` 后，该池纹理被 UI 线程删除；随后 close_source:1395 将该 slot ack 回池，池把一个已销毁的纹理名重新派发给后续渲染任务——渲染写入已删除纹理（GL 错误/黑帧），且纹理名被 viewer 之后 `glGenTextures` 复用时会发生跨模块别名写入。注释"do NOT delete — owned by render thread"的意图被别名覆盖击穿。
  - 建议: `paintGL` 不应把共享纹理 ID 写入 `_current_tex_id`（渲染时局部引用即可）；`_current_tex_id` 只允许存 viewer 自建纹理。参照同库 program_viewer.py:1225-1236 的做法：共享纹理复制进 viewer 自有显示纹理，绝不直接持有池纹理 ID。
  - 置信度: 高

- [AUD-20901] [Critical] CPU 回退上传路径按尺寸匹配复用 `_current_tex_id`，会 glTexSubImage2D 写入（或 glTexImage2D 重定义）渲染线程池纹理
  - 位置: src/features/edit/ui/source_viewer.py:929-943（复用写入）；872-885（重定义路径）；974（保留别名并切断共享引用）
  - 证据:
    ```python
    # set_texture_from_qimage_with_display_size (929-943)
    size_matches = (
        self._current_tex_id is not None
        and self._tex_width == width
        and self._tex_height == height
    )
    if size_matches:
        _gl.glBindTexture(GL_TEXTURE_2D, self._current_tex_id)
        _gl.glTexSubImage2D(
            GL_TEXTURE_2D, 0, 0, 0, width, height,
            GL_RGBA, GL_UNSIGNED_BYTE, pixel_data
        )
    ```
    ```python
    # (973-974) 上传后保留 _current_tex_id（可能即池纹理），仅切断共享引用
    # Clear shared texture reference
    self._shared_tex_id = None
    ```
  - 影响: 共享纹理显示后 `_current_tex_id`/`_tex_width`/`_tex_height` 均来自池纹理；GPU watchdog 超时（100ms）或 GPU 路径不可用时触发 CPU 回退，代理解码尺寸与池纹理尺寸同源（同为媒体显示尺寸封顶 1920），大概率尺寸相等——CPU 像素被直接写进被 ack 协议钉住（pinned）的池槽纹理；之后新 GPU 帧到达时该槽被 ack 回收、渲染器复用同一名字再写，而 viewer 仍以该 ID 显示当前画面，可见帧被后台渲染原地改写（program_viewer.py:1227-1229 注释明确指出此危害并以复制规避）。`set_texture_from_qimage`（872-885）在同状态下则会用 glTexImage2D 重定义池纹理。
  - 建议: CPU 路径只允许写入 viewer 自建纹理：当 `_current_tex_id` 来自共享路径时必须重新 glGenTextures 分配；根治方案与 AUD-20900 相同——显示纹理 viewer 私有、共享纹理复制或只读引用。
  - 置信度: 高

- [AUD-20902] [Major] 纹理上传/删除在 paintGL/initializeGL/resizeGL 之外执行且全程无 makeCurrent；`_pending_qimage` 延迟上传机制为死字段
  - 位置: src/features/edit/ui/source_viewer.py:852-890（上传）；982-995（删除）；调用点 2013、2029、2190、2208、2273、2285；死字段 532 / 705-709
  - 证据:
    ```python
    # set_texture_from_qimage (871-885) — 由 _load_frame 等 UI 槽直接调用
    if self._current_tex_id is None:
        self._current_tex_id = _gl.glGenTextures(1)
    _gl.glBindTexture(GL_TEXTURE_2D, self._current_tex_id)
    ...
    _gl.glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, width, height, 0,
                     GL_RGBA, GL_UNSIGNED_BYTE, pixel_data)
    ```
    ```python
    # paintGL (705-709) — 本应承担延迟上传，但 _pending_qimage 从未被赋真实图像
    pending = self._pending_qimage
    if pending is not None and not pending.isNull():
        self.set_texture_from_qimage(pending)
        self._pending_qimage = None
    ```
  - 影响: Qt 文档规定在三个 paint 回调之外调用 GL 函数必须先 `makeCurrent()`（本文件 0 处）。上下文是否恰好仍 current 取决于平台与调度：PyOpenGL 错误检查会在无当前上下文时抛 GLError，被 `_load_video_frame` 的 except 捕获后记为 cpu_decode_error，表现为永久黑帧；某些驱动上则是未定义行为/崩溃。对照同库：src/features/color/ui/gl_viewport.py:45 在 paint 之外显式 `self.makeCurrent()`；program_viewer.py:3253 由控制器只写 `_pending_qimage`、上传统一发生在 paintGL（1232-1236）——本文件保留同名字段却从未赋值，偏离了库内既有安全模式。
  - 建议: 恢复 `_pending_qimage` 延迟上传模式（UI 槽只暂存 QImage，paintGL 内完成 glGen/glTexImage/glTexSubImage）；`clear_texture` 改为标记待删并在 paintGL（或 makeCurrent/doneCurrent 包裹）内删除。
  - 置信度: 高（违规事实与库内对照确凿；实际后果平台相关）

- [AUD-20903] [Major] CPU 回退为 UI 线程同步解码（关键帧 seek + 前向解码），watchdog 超时与播放 tick 直接阻塞界面
  - 位置: src/features/edit/ui/source_viewer.py:2161-2169（同步 read_frame）；2220-2233（watchdog 触发）；2255-2261（request_cpu_fallback 路径）
  - 证据:
    ```python
    try:
        # Create decoder if needed — use media's native display size, NOT viewer size
        if self._video_decoder is None:
            self._video_decoder = self._open_cpu_video_decoder()
        # Get frame
        decode_t0 = _time.perf_counter()
        decoded = self._video_decoder.read_frame(frame)
    ```
    ```python
    # _on_gpu_timeout (2230-2233)
    logger.debug(f"SourceViewerWidget: GPU timeout for frame {timed_out_frame}, falling back to CPU")
    self._error_message = "gpu_timeout"
    self._cpu_fallback_decode(timed_out_frame)
    ```
  - 影响: GPU 路径未在 100ms 内应答（重代理/长 GOP 素材常见）或无 GPU provider 时，每个播放 tick / scrub 都在 UI 线程同步跑 FFmpeg 解码，长 GOP 大分辨率素材单帧可达数百毫秒，与 4K 实时基准冲突，表现为播放/刮擦卡顿与界面冻结。
  - 建议: 将 CPU 回退解码移入后台线程（现有 decode worker 已存在 queued 通道 request_cpu_fallback，可将解码本身也移过去，仅上传留在 UI 线程）；至少对 CPU 路径复用现有 LRU 缓存预取邻帧。
  - 置信度: 中（阻塞事实确凿，卡顿幅度取决于素材）

- [AUD-20904] [Suggestion] `_render_texture` 每帧 glGetUniformLocation 并以 GL_STATIC_DRAW 全量重传顶点
  - 位置: src/features/edit/ui/source_viewer.py:773-785
  - 证据:
    ```python
    tex_loc = _gl.glGetUniformLocation(self._prog, b'u_texture')
    _gl.glUniform1i(tex_loc, 0)
    vertices = np.array([...], dtype=np.float32)
    _gl.glBindBuffer(GL_ARRAY_BUFFER, self._quad_vbo)
    _gl.glBufferData(GL_ARRAY_BUFFER, vertices, GL_STATIC_DRAW)
    ```
  - 影响: uniform location 与 VAO 布局在 link 后即固定，每帧查询+重传属纯开销；GL_STATIC_DRAW 语义与每帧更新相悖（应为 GL_DYNAMIC_DRAW 或把显示矩形改 uniform/UBO，VBO 完全静态）。
  - 建议: 初始化时缓存 uniform location；顶点 VBO 固定为单位 quad，显示矩形经 uniform 传入顶点着色器。
  - 置信度: 高

- [AUD-20905] [Suggestion] 帧缓存命中路径使用全量 glTexImage2D 的旧接口，比未命中路径更贵且不更新 last-good 状态
  - 位置: src/features/edit/ui/source_viewer.py:2011-2014
  - 证据:
    ```python
    # Check cache first
    if frame in self._frame_cache:
        self.viewer.set_texture_from_qimage(self._frame_cache[frame])
        return
    ```
  - 影响: `set_texture_from_qimage` 不做尺寸匹配复用（每次 glTexImage2D 重新分配），而未命中的视频路径走 `set_texture_from_qimage_with_display_size`（glTexSubImage2D 复用）——反向刮擦/回放的高频命中路径反而更慢；且命中分支不刷新 `_last_good_qimage/_last_good_frame`，后续解码失败时可能回退到更旧的帧。
  - 建议: 命中路径统一走 `set_texture_from_qimage_with_display_size` 并同步 last-good 状态。
  - 置信度: 高

- [AUD-20906] [Suggestion] 宽泛 except 静默吞错（close_source 内 `except Exception: pass` 无任何日志）
  - 位置: src/features/edit/ui/source_viewer.py:1374-1379（无日志）；1796-1799、1806-1809、1827-1828、1851-1852 等全部 logger.debug
  - 证据:
    ```python
    if self._video_decoder:
        try:
            self._video_decoder.close()
        except Exception:
            pass
        self._video_decoder = None
    ```
  - 影响: 解码器关闭失败（句柄泄漏、文件锁未释放）完全不可见；音频路径领域错误被压到 debug 级别，违背"不得宽泛 except 掩盖领域错误"铁律，排障时无迹可循。
  - 建议: close 失败至少 logger.warning（含异常信息）；音频操作失败区分 debug（预期不可用）与 warning（真实错误）。
  - 置信度: 高

- [AUD-20907] [Suggestion] close_source 以 `in_point_changed.emit(0)`/`out_point_changed.emit(0)` 表达"已清空"，与合法帧号 0 语义二义
  - 位置: src/features/edit/ui/source_viewer.py:1428-1433
  - 证据:
    ```python
    # Notify listeners (EditPage) to sync TimelinePanel edit button state
    self.in_point_changed.emit(0)
    self.out_point_changed.emit(0)
    ```
  - 影响: 实际状态是 None，信号却携带 0——监听方无法区分"In 点被清除"与"In 点设置在第 0 帧"；当前 EditPage 以重新读取属性兜底（edit_page.py:2772-2786），但任何只消费信号值的监听者会得到错误状态。
  - 建议: 信号改为携带 `Optional[int]`（或增加 cleared 专用信号），与属性状态一致。
  - 置信度: 高（二义性确凿；现有监听方恰好未受影响）

- [AUD-20908] [Suggestion] 死代码：`_on_insert/_on_overwrite/_on_append` 无任何连接；`_can_edit` 只写不读；`_pending_qimage` 死字段
  - 位置: src/features/edit/ui/source_viewer.py:1958-1986（三个 handler）；415（`_can_edit` 赋值）；532（死字段）
  - 证据:
    ```python
    def _on_insert(self) -> None:
        """Handle Insert request."""
        if self._current_asset and self._in_point is not None and self._out_point is not None:
            self.insert_requested.emit(
                self._current_asset.asset_id,
                self._in_point,
                self._out_point
            )
    ```
    （全库 grep：`_on_insert/_on_overwrite/_on_append` 仅此三处定义，无连接点；插入按钮已迁至 TimelinePanel，见 1283-1284 注释）
  - 影响: 约 30 行不可达代码；`_can_edit` 为只写状态（注释称 EditPage 桥接，实际外部无人读取）；`_pending_qimage` 机制弃用（见 AUD-20902）——维护者易误以为这些路径生效。
  - 建议: 删除三个孤儿 handler 与 `_can_edit`，或将桥接逻辑真正接上；`_pending_qimage` 按 AUD-20902 建议复活或删除。
  - 置信度: 高

- [AUD-20909] [Suggestion] 类型注解缺失（类构造器与若干方法无参数/返回注解）
  - 位置: src/features/edit/ui/source_viewer.py:188、442、508、1137（`__init__(self, parent=None)`）；2061（`_open_cpu_video_decoder(self)` 无返回注解）；2092（`_decoded_frame_to_rgb24(self, decoded)` 参数与返回均未注解）；456（`paintEvent(self, event)`）
  - 证据:
    ```python
    def __init__(self, parent=None):        # SourceViewerBar:188
    def __init__(self, parent=None):        # SourceViewerWidget:1137
    def _open_cpu_video_decoder(self):
    def _decoded_frame_to_rgb24(self, decoded) -> Optional[np.ndarray]:
    ```
  - 影响: 违背库内"所有函数有类型注解"约束；解码器返回类型不明确（Topos source 与 FFmpegMediaSource 多态）增加调用侧误用风险。
  - 建议: 补 `parent: Optional[QWidget] = None`、解码器返回统一协议类型注解。
  - 置信度: 高

### 通过项
- GLSL 正确性：两段内置着色器均为 `#version 330`，浮点字面量全部显式 `.0`，无循环、无隐式类型转换
- 信号防递归：slider 程序化更新使用 `blockSignals`（1755-1758）；播放 tick/seek 的 `slider.setValue` 依赖值相等短路（1706-1712），无递归回路
- 状态提交一致性：组件不参与 undo/redo/revision；in/out 状态经信号外发，消费侧 EditPage 校验 `in < out`（edit_page.py:2772-2777）
- GPU 结果陈旧防护：generation + 帧尾随窗口（±_GPU_STALE_WINDOW）+ slot ack 协议在 set_gpu_frame/request_cpu_fallback 中实现完整自洽
- 输入验证：帧号统一经 `_clamp_source_frame` 钳制；GPU provider 返回 dict/tuple 双协议均校验后再用；`fps or 24.0`、`total_frames or 1` 兜底
- HDR 边界：CPU 显示路径输出 RGB888 属显示端转换，内部管线经共享纹理（浮点）零拷贝传递，未见无理由 clamp

---

## src/features/edit/ui/stem_export_dialog.py (行数: 417)

### 发现

- [AUD-20910] [Major] reject/close 的 3 秒 join 超时后导出线程仍在后台运行，对话框销毁时存在 QThread destroyed-while-running 崩溃风险
  - 位置: src/features/edit/ui/stem_export_dialog.py:392-408（join 与覆盖）；9-12（取消粒度文档）
  - 证据:
    ```python
    def _cancel_and_join_worker(self, timeout_ms: int = 3000) -> None:
        """请求取消并等待 worker 退出，避免线程悬空。"""
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(timeout_ms)

    def reject(self) -> None:
        self._cancel_and_join_worker()
        super().reject()
    ```
    （模块头注：`取消语义：worker 在每个 stem 渲染开始前轮询取消标志，粒度为单个 stem`）
  - 影响: 取消只在 stem 边界生效，单个 stem（如长工程 Master 混音）渲染常超 3 秒——`wait(3000)` 超时返回 False 后 `super().reject()` 照常关闭对话框，worker（parent=对话框）继续后台写文件、信号仍发往已隐藏 UI；若期间用户再次触发导出入口创建第二个 worker，会与旧 worker 并发写同一输出目录；对话框最终销毁（如应用退出、入口侧不缓存实例）时触发 Qt 致命错误 "QThread: Destroyed while thread is still running"。
  - 建议: 超时后进入"等待后台完成"状态（禁用 Close 并显示提示），或对 `failed/finished_ok` 未收到且已 reject 的 worker 保存全局引用并在 app 退出前统一 join；入口控制器侧保证同一时间仅一个对话框/worker 实例。
  - 置信度: 中（崩溃需对话框销毁时序；后台残留与并发写目录路径明确）

- [AUD-20911] [Suggestion] 文件名前缀未做路径安全清洗，`/`、`..` 等字符可使命名结果越出输出目录
  - 位置: src/features/edit/ui/stem_export_dialog.py:88（清洗）；137-143（拼接）
  - 证据:
    ```python
    self._prefix = (prefix or "").strip().rstrip("_- ")
    ...
    src = os.path.join(self._output_dir, f"{name}.wav")
    dst = os.path.join(self._output_dir, f"{self._prefix}_{name}.wav")
    ```
  - 影响: 前缀含 `/` 或 `../` 时 `os.path.join` 产生目录外路径：`a/b_` 指向不存在子目录（rename 失败、stem 被误标 ✘），`../x_` 则把文件重命名到输出目录之外，违背"外部数据须验证"。
  - 建议: 对前缀做白名单过滤（仅保留字母数字、`-`、`_`、汉字等安全字符）或 `os.path.basename` 归一，非法字符替换并在 UI 提示。
  - 置信度: 高（逻辑确凿，本机用户输入影响有限）

- [AUD-20912] [Suggestion] `_STEM_TYPE_LABELS` 硬编码中文标签绕过 I18N 体系
  - 位置: src/features/edit/ui/stem_export_dialog.py:54-59（定义）；185-189（经 `.get()` 拼入 I18N 模板参数）
  - 证据:
    ```python
    _STEM_TYPE_LABELS = {
        StemType.MASTER: "Master 混音",
        StemType.TRACK: "单轨",
        StemType.BUS: "Bus",
        StemType.GROUP: "分组",
    }
    ```
  - 影响: 同文件其余文案均走 `I18N.tr`，这四项固定中文，英文/多语言界面下勾选列表出现混杂语言。
  - 建议: 改为 `I18N.tr("edit.stem_export.kind.master", "Master 混音")` 等 keyed 条目。
  - 置信度: 高

- [AUD-20913] [Suggestion] 类型注解泛化为 Any（sequence/project/result 均有真实类型却不引用）
  - 位置: src/features/edit/ui/stem_export_dialog.py:74-77、165、354、405
  - 证据:
    ```python
    def __init__(
        self,
        sequence: Any,
        project: Any = None,
        parent: Optional[QWidget] = None,
    ) -> None:
    ```
  - 影响: `TimelineSequence`、`StemExportResult` 等类型已在 core 中定义，全部标 Any 使 `_on_finished` 内 `result.stems/all_succeeded` 等属性访问失去静态检查；`closeEvent(self, event: Any)` 同理。
  - 建议: 在 TYPE_CHECKING 块导入真实类型并注解（避免运行时循环导入）。
  - 置信度: 高

### 通过项
- 线程安全：worker 经 pyqtSignal（queued）回 UI 线程；`_cancel_requested` 单写多读标志在 GIL 下安全；解码器上下文在 worker 线程内创建并在 run() 的 finally 中 close（97-117），符合"解码器上下文线程私有"约束
- 异常与取消路径：worker 边界 except 带 `logger.exception` 后以 failed 信号回 UI（123-125，noqa 有据）；export_all_stems 逐 stem 轮询 cancel_check，已完成文件保留，语义与模块头注一致
- 输入验证：输出目录非空、至少勾选一个 stem、`_start_export` 以 isRunning 防重入；空 sequence/规划异常进入空态不崩溃（176-201）
- 信号防递归：无程序化改写输入控件引发的反馈环（运行期仅 setEnabled/setVisible）
- GPU/GL/undo：不涉及 OpenGL 与撤销栈，无违规面

---

## src/features/edit/ui/speech_to_text_dialog.py (行数: 146)

### 发现

- [AUD-20914] [Suggestion] 跨模块导入 shared/export 面板的下划线私有助手，形成对无关特性的私有依赖
  - 位置: src/features/edit/ui/speech_to_text_dialog.py:24-27
  - 证据:
    ```python
    from src.shared.export.export_panel import _apply_compact_button_style, _make_param_label
    from src.shared.i18n.i18n import I18N
    from src.shared.ui.widgets import ChevronComboBox
    ```
  - 影响: 语音转文字对话框依赖导出面板模块级私有函数（export_panel.py:251/282），既绕过公共接口又把导出面板模块拖进导入图；对 `_make_param_label` 的任何内部改动都会静默破坏本对话框外观。
  - 建议: 将这两个通用样式助手提升到 `src.shared.ui.widgets` 公共层，双方改引公共 API。
  - 置信度: 高

### 通过项
- 输入验证：`settings()` 对每个字段做类型强转 + `or` 默认值兜底（134-143），combo data 均为非空常量
- 线程安全 / GPU 生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性：纯配置对话框，无对应逻辑面，无违规
- 异常路径：无 IO/渲染调用，无需取消路径
- 类型注解：完整（含返回 `dict[str, Any]`）
