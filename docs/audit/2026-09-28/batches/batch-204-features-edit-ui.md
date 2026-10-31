# 批次报告：batch-204 [P1] src/features/edit/ui//

- 任务书：docs/audit/2026-09-28/assignments/batch-204.md
- 编号区间：AUD-20400–AUD-20499
- 审查范围：1 个文件，共 3,414 行
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解

---

## src/features/edit/ui/program_viewer.py (行数: 3414)

### 发现

- [AUD-20400] [Critical] CPU 上传路径忽略 QImage 行跨距（bytesPerLine 4 字节对齐），特定宽度下预览帧整体错位
  - 位置: src/features/edit/ui/program_viewer.py:1050
  - 证据:
    ```python
    bits = qimage.bits()
    bytes_per_pixel = 3  # RGB888
    bits_size = width * height * bytes_per_pixel
    bits.setsize(bits_size)
    pixel_data = np.frombuffer(bits, dtype=np.uint8, count=bits_size).reshape(height, width, 3)
    ```
  - 影响: QImage 扫描行按 32 位对齐（`bytesPerLine()` 是 4 的倍数）。当 `width * 3 % 4 != 0`（如宽 1366、854、1002 等常见媒体宽度）时，`reshape(height, width, 3)` 按紧凑行距解释带填充的数据，导致每行错位 1–2 字节、整幅图像倾斜错乱——CPU 预览路径（`set_qimage` → `_set_frame_from_qimage`，拖拽/scrub/非 GPU 管线时的主路径）渲染错误。仓库内其他读取 QImage 的代码均正确处理：`src/shared/video/monitor_output.py:217`、`src/app/controllers/page_controller.py:2704` 都用 `bytesPerLine()`；且本文件 `_paint_subtitle_overlay`（L2888–2894）也用了 `strides[0]`，唯独此处遗漏。
  - 建议: `stride = qimage.bytesPerLine()`；用 `np.frombuffer(bits, dtype=np.uint8, count=height*stride).reshape(height, stride)[:, :width*3].reshape(height, width, 3)` 或 `np.lib.stride_tricks.as_strided` 处理行跨距。
  - 置信度: 高（机制确定；是否显现取决于帧宽，常见 1920/1080 宽度恰为 4 整除所以未暴露）

- [AUD-20401] [Major] 错误状态/时间码 overlay 绘制方法从未被调用（死代码），"Media Offline"、渲染/解码错误、R-8 ghost 等错误反馈 UI 完全失效
  - 位置: src/features/edit/ui/program_viewer.py:2458（另 2612、2654；触发链 1128–1191）
  - 证据:
    ```python
    def _paint_error_overlay(self, painter: QPainter) -> None:
        """绘制错误状态 overlay (called from paintGL with active QPainter)"""
    ```
    而 `_paintGL_impl`（L1221–1285）只调用 `_draw_guides_overlay()`、`_paint_subtitle_overlay`、两个 handle controller 的 `draw_handles`，没有任何 `_paint_error_overlay` / `_paint_timecode_overlay` / `_paint_qimage_overlay` 调用；全仓库 grep（src/ + tests/）无任何调用点。`set_error_state` 仅设状态并重绘：
    ```python
    self._error_state = state
    self._error_message = message or ""
    ...
    # 触发重绘
    self.update()
    ```
    同时 `_show_source_ghost`（L800 初始化、L1185 赋值）只写不读，`_overlay_qimage`（L3254 赋值）仅被死方法 `_paint_qimage_overlay` 读取。
  - 影响: `set_error_state("no_clip"/"render_error"/"decode_error")`、`set_offline_media`（精心实现的 "Media Offline" 红色警告，L2480–2537）、`set_composite_error`（R-8 ghost 模式）设置状态后画面无任何可见变化——媒体离线/渲染失败时用户只看到黑屏或残留帧，无排障线索；canvas 内时间码 overlay 同样缺失（底部 transport bar 有替代显示，影响较小）。
  - 建议: 在 `_paintGL_impl` 的 GL 绘制完成后按 `_error_state` 调用 `_paint_error_overlay(painter)`（QPainter 在 paintGL 内直接叠加即可，参考 guides overlay 画法），并补齐 `_show_source_ghost` 的降透明度残留帧绘制；删除或接通 `_paint_timecode_overlay` / `_paint_qimage_overlay`。
  - 置信度: 高

- [AUD-20402] [Major] clear_texture 在 GL 回调之外直接调用 glDeleteTextures，未确保上下文 current、也未走 deferred-release
  - 位置: src/features/edit/ui/program_viewer.py:1113
  - 证据:
    ```python
    def clear_texture(self) -> None:
        """清除当前纹理和错误状态"""
        self._ack_pending_present_without_display()
        # 删除自上传纹理（共享纹理由渲染线程管理，不在这里删除）
        if self._current_tex_id is not None:
            _gl.glDeleteTextures([self._current_tex_id])
    ```
    非回调调用点：`set_qimage` 空图（L3237）、`set_composite_error(show_ghost=False)`（L1189）、`set_frame` 未知结果类型（L3384）、`ProgramViewerWidget.clear_texture`（L3192–3194，公开 API 可被任意槽调用）。
  - 影响: QOpenGLWidget 的上下文仅在 initializeGL/paintGL/resizeGL 回调内保证 current。上述调用发生在信号槽上下文（如 `renderer.frame_ready → set_frame`，file_controller.py:2494 连接），此刻 current 的可能是同窗口其他 QOpenGLWidget（如 SourceViewer）的上下文——纹理名按上下文（组）隔离，会在错误上下文里删除名字（错删他人纹理/自身泄漏）；若无任何 current 上下文，PyOpenGL 直接抛 GLError。违反项目“GPU 资源按 deferred-release 模式释放”铁律。
  - 建议: `clear_texture` 只置空标记（`_current_tex_id = None`），把删除排队到下一次 paintGL 开头执行；或显式 `self.makeCurrent()` → 删除 → `self.doneCurrent()`。
  - 置信度: 高（违规确定；实际后果取决于同窗口 GL widget 布局，中）

- [AUD-20403] [Major] present-frame ack 只在 blit 成功路径触发，源纹理无效或 blit 异常时 renderer slot 悬挂
  - 位置: src/features/edit/ui/program_viewer.py:1729
  - 证据:
    ```python
    if not _gl.glIsTexture(source_tex):
        playback_trace(
            "program_viewer.copy_shared_invalid",
            tex_id=source_tex, width=width, height=height,
        )
        self._shared_tex_id = None
        return
    ```
    ack 只在 L1837–1846（blit 完成后）触发：
    ```python
    # Batch 1: blit 完成后触发 ack，释放 renderer slot（防 slot 被复用导致混帧）。
    if present is not None:
        ack_cb = self._pending_present_ack
        if ack_cb is not None:
            try:
                ack_cb(getattr(present, 'slot_id', -1))
    ```
    且 L1773–1807 的 try/finally 只恢复 FBO/纹理绑定，不含 ack——blit 抛异常同样跳过。
  - 影响: present 协议下 renderer 侧 slot 等 ack 释放。早退/异常路径悬挂 ack 后，slot 只能等下一次 `set_present_frame` / `set_shared_texture` 等入口经 `_ack_pending_present_without_display` 补偿；若失效帧是暂停/停止前最后一张，slot 长期占用甚至耗尽输出池，后续渲染等待、播放卡顿。
  - 建议: 在 glIsTexture 失败早退分支和 blit 的 finally 中统一调用 `_ack_pending_present_without_display()`（保证恰好一次触发）。
  - 置信度: 中（renderer 侧 slot 释放语义不在本批次文件内核实）

- [AUD-20404] [Major] 字幕 overlay 的 QPainter 绘制调用被包在 beginNativePainting/endNativePainting 之间，属 Qt 文档定义的未定义行为
  - 位置: src/features/edit/ui/program_viewer.py:1265
  - 证据:
    ```python
    if self._subtitle_overlays or show_subtitle_handles:
        painter = QPainter(self)
        try:
            painter.beginNativePainting()
            self._paint_subtitle_overlay(painter)
            # 绘制选中字幕的变换手柄
            if show_subtitle_handles:
                self._subtitle_handle_controller.draw_handles(painter)
            painter.endNativePainting()
        finally:
            painter.end()
    ```
  - 影响: Qt 规定 begin/endNativePainting 之间只允许原生 GL 调用，QPainter 操作（`_paint_subtitle_overlay` 内大量 setTransform/setOpacity/drawImage）结果是未定义的——变换、混合、平滑缩放状态可能丢失或错乱，跨驱动/平台行为不可预期。同文件 L1276–1285 的 clip transform handles 特意注明"keep them outside beginNativePainting"，两处模式自相矛盾，字幕路径（依赖 opacity/reveal/SmoothPixmapTransform）受影响面最大。
  - 建议: 去掉 begin/endNativePainting 包裹，QPainter 在 paintGL 内可直接叠加在 GL 内容之上（guides overlay L1383 即此画法）；两处统一为同一模式。
  - 置信度: 中（违反 Qt 文档确定；当前平台疑似"碰巧能渲染"）

- [AUD-20405] [Suggestion] 每帧 glGetUniformLocation + glBufferData(GL_STATIC_DRAW) 重建顶点缓冲
  - 位置: src/features/edit/ui/program_viewer.py:1348
  - 证据:
    ```python
    # Update VBO (use glBufferData like SourceViewer does)
    _gl.glBindBuffer(GL_ARRAY_BUFFER, self._quad_vbo)
    _gl.glBufferData(GL_ARRAY_BUFFER, vertices, GL_STATIC_DRAW)

    # Use shader program
    _gl.glUseProgram(self._prog)
    ...
    tex_loc = _gl.glGetUniformLocation(self._prog, b'u_texture')
    _gl.glUniform1i(tex_loc, 0)
    ```
  - 影响: uniform location 查询和 16 字节顶点重分配在每次 paintGL 执行，4K 60fps 基准下是纯冗余驱动调用。
  - 建议: 初始化后缓存 `u_texture` 位置；VBO 用 GL_DYNAMIC_DRAW 且仅在 display_rect 变化时更新。
  - 置信度: 高

- [AUD-20406] [Suggestion] CPU 上传路径每帧最多 3 次全帧级拷贝（convert + concatenate + ascontiguousarray）
  - 位置: src/features/edit/ui/program_viewer.py:1039
  - 证据:
    ```python
    fmt = qimage.format()
    if fmt != QImage.Format.Format_RGB888:
        qimage = qimage.convertToFormat(QImage.Format.Format_RGB888)
    ...
    alpha_channel = np.full((height, width, 1), 255, dtype=np.uint8)
    rgba_data = np.concatenate([pixel_data, alpha_channel], axis=2)
    rgba_data = np.ascontiguousarray(rgba_data)
    ```
  - 影响: 4K 帧约 75MB/帧（RGB），每帧最多三次全帧分配+拷贝，scrub/拖拽 CPU 路径每帧上百 MB 内存流量，挤占 UI 线程时间片。
  - 建议: 预分配可复用 RGBA 缓冲，`buf[..., :3] = pixel_data; buf[..., 3] = 255` 原地写；或直接上传 GL_RGB（内部格式 RGB8）纹理，彻底免去 alpha 拼接。
  - 置信度: 高

- [AUD-20407] [Suggestion] set_frame 递归保护用 print 输出警告，深度计数用动态属性
  - 位置: src/features/edit/ui/program_viewer.py:3283
  - 证据:
    ```python
    self._set_frame_depth = getattr(self, '_set_frame_depth', 0) + 1
    if self._set_frame_depth > 3:
        print(f"[RECURSION WARNING] set_frame called {self._set_frame_depth} times, possible signal loop!")
        self._set_frame_depth -= 1
        return
    ```
  - 影响: print 绕过 logging 配置（生产环境不可见）；`_set_frame_depth` 未在 `__init__` 声明，依赖 getattr 魔法。
  - 建议: 改 `logger.warning`；在 `__init__` 显式 `self._set_frame_depth: int = 0`。
  - 置信度: 高

- [AUD-20408] [Suggestion] 类型注解缺失/误用：`Optional[callable]`（内建函数当类型）、bare `list`、多处方法无注解
  - 位置: src/features/edit/ui/program_viewer.py:790
  - 证据:
    ```python
    self._pending_present_ack: Optional[callable] = None
    self._gl_fence_wait_fn: Optional[callable] = None
    ```
    （另 L1620 `ack_cb: Optional[callable]`、L1669 `wait_fn: Optional[callable]`、L2689 `clips: list`）
  - 影响: `callable` 是内建函数不是类型，运行期注解不求值所以不崩，但 mypy/IDE 全量报错；`set_present_frame` 的 `present_frame`、`set_selected_subtitle_clip` 的 `clip/style`、`initializeGL/paintGL/paintEvent/_create_quad_vbo/enter_fullscreen` 等均无注解，与 .clinerules 强类型要求不符。
  - 建议: 改 `Optional[Callable[[int], None]]`；补齐公开方法参数/返回注解。
  - 置信度: 高

- [AUD-20409] [Suggestion] 跨对象私有属性访问：widget 直写 viewer 内部状态、viewer 直读控制器 `_active_handle`
  - 位置: src/features/edit/ui/program_viewer.py:3253
  - 证据:
    ```python
    self._viewer._pending_qimage = image
    self._viewer._overlay_qimage = image
    self._viewer._tex_width = image.width()
    self._viewer._tex_height = image.height()
    ```
    （另 `ctrl._active_handle` L2340/2352/2370/2377–2379、`self._viewer._error_state` L3196–3197）
  - 影响: `ProgramViewerWidget.set_qimage/clear_texture` 与鼠标事件绕过公开 API 直接改内部字段，被访问方私有字段改名即静默损坏，破坏封装边界。
  - 建议: 为 ProgramViewer 提供 `set_pending_qimage()`、为 handle controller 提供 `has_active_drag()` 等公开方法。
  - 置信度: 高

- [AUD-20410] [Suggestion] `except Exception: pass` 宽泛吞掉 GL 异常
  - 位置: src/features/edit/ui/program_viewer.py:1685
  - 证据:
    ```python
    def _reset_copy_gl_state(self) -> None:
        """Reset GL state that can make clear/blit update only part of a texture."""
        try:
            _gl.glDisable(GL_SCISSOR_TEST)
            _gl.glDisable(GL_DEPTH_TEST)
            _gl.glDisable(GL_BLEND)
            _gl.glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE)
        except Exception:
            pass
    ```
    （另 L1866–1870 PBO 删除处同样 `pass`）
  - 影响: 违反 .clinerules “不得宽泛 except 掩盖领域错误”：GL 状态重置失败被静默吞掉，后续 clear/blit 只更新部分纹理这类问题将无法定位。
  - 建议: 至少 `logger.debug("reset GL state failed: %s", exc, exc_info=True)` 记录异常。
  - 置信度: 高

- [AUD-20411] [Suggestion] 每帧每字幕的循环体内执行 import
  - 位置: src/features/edit/ui/program_viewer.py:2940
  - 证据:
    ```python
    from src.features.edit.core.captions.render_plan import from_clip
    tf = from_clip(clip, texture.x, texture.y,
                   float(texture.width), float(texture.height),
                   style=render_style)
    ```
    （位于 `for item in self._subtitle_overlays:` 循环体内；L2908 的 graphics 导入同病）
  - 影响: 播放时每帧 × 每条字幕执行 import 语句（sys.modules 命中也有查找开销），纯热路径损耗。
  - 建议: 提升到模块顶部或方法开头一次性导入。
  - 置信度: 高

- [AUD-20412] [Suggestion] exit_fullscreen 无原父级分支 deleteLater 全屏窗口时可能连 viewer 一起销毁
  - 位置: src/features/edit/ui/program_viewer.py:2243
  - 证据:
    ```python
    else:
        logger.warning("ProgramViewer: No saved parent, cannot restore from fullscreen")
        # Close fullscreen window anyway
        if self._fullscreen_window:
            self._fullscreen_window.close()
            self._fullscreen_window.deleteLater()
            self._fullscreen_window = None
    ```
  - 影响: 此分支执行时 viewer 仍是 `_fullscreen_window` 的子控件，`deleteLater()` 会连 viewer 一并销毁；外部持有的 Python 引用再访问即 RuntimeError（C++ 对象已删除）。当前构造路径始终有父级（L3046），触发需 parentless viewer，属潜在隐患而非现行缺陷（有父分支先 setParent 再等 deferred delete，安全）。
  - 建议: 该分支先 `self.setParent(None)` 再 deleteLater，或干脆拒绝无父全屏。
  - 置信度: 高（代码路径确定、触发条件罕见）

### 通过项
- GLSL 正确性：`#version 330` 着色器字面量均带 `.0`，无循环、无隐式类型转换，attribute/uniform 使用正确（例外维度外的纹理数据问题见 AUD-20400）
- HDR：本文件为 8bit 显示/外监转换层；字幕 rgba→uint8 采用 `clip*255+0.5` 正确舍入（L2889），无内部 HDR 数据无理由 clamp
- 信号防递归：`ProgramTimelineSelector.set_timeline_choices` 用 QSignalBlocker + finally；`set_loop_checked` 用 blockSignals 包裹；`set_transform_tool_active` 同状态早退防回环；zoom_changed 链单向终止于 label 更新
- 状态提交一致性：本文件不做状态提交，字幕/clip 变换拖拽经 started/updated/ended 信号携带 original 参数交由上层构造 undo 命令，职责边界正确
- 异常与取消路径：`set_frame` 对 None/None 值 present dict/present 对象/QImage/裸 tuple/未知类型分支完备；REALTIME 模式拒绝裸 tuple 防混帧（L3346–3355）；无效 tex_id 拒绝并回错误态
- 输入验证：`set_offline_media` 过滤非 dict 项并归一化；`set_qimage` 空图/格式检查；`set_shared_texture`/`set_present_frame` 对 tex_id None/0 拒绝；`_pbo_rgba_to_qimage` reshape 异常保护返回 None
- GPU 资源生命周期（部分通过）：共享纹理不删除（归 renderer 管理）；`_ensure_monitor_pbos` 尺寸变化时删除旧 PBO；`_render_texture` 用 glIsTexture 防御已删纹理；blit 后 finally 恢复 default framebuffer 绑定（例外见 AUD-20402/20403）
- 线程安全（部分通过）：GL 调用集中于 initializeGL/paintGL 并以 enter/exit_gui_gl_stack 包裹；`set_frame` 经 renderer 信号（AutoConnection 队列到 UI 线程，src/app/controllers/file_controller.py:2494）调用，无跨线程直呼（例外见 AUD-20402）
- 性能模式（部分通过）：外监读回有 25fps 节流 + 双 PBO 轮转 + 兜底直读；字幕 QImage 有 16 项缓存避免每帧转换（例外见 AUD-20405/20406/20411）
