# batch-081 审查报告 — src/features/composite/render//

编号区间：AUD-8100–AUD-8199（实际使用 AUD-81001–AUD-81012）
审查日期：2026-09-28
审查范围：
- `src/features/composite/render/composite_render_output.py` (1,240 行)
- `src/features/composite/render/composite_render_worker.py` (286 行)

交叉验证（只读）：`parallel_frame_render.py`（render_frames_parallel 语义、join(timeout=5)）、`composite_frame_renderer.py`（cleanup/warm_up 无锁实现）、`shared/media/metadata.py`（Rational 允许 num=0）、`shared/export/video_encoder.py`（bgr24/bgr48le 直填路径存在）。

统计：Critical 0 / Major 5 / Suggestion 7

---

## src/features/composite/render/composite_render_output.py (行数: 1240)

### 发现

- [AUD-81001] [Major] 串行与并行导出路径的"中途取消"语义相反：串行保留部分产物并报 success，并行删除产物并误报 "Render failed"
  - 位置: src/features/composite/render/composite_render_output.py:517-532（串行）与 957-998（并行视频）、1076-1104（并行序列）
  - 证据（串行，保留产物+成功）:
    ```python
    cancelled = (
        is_cancelled is not None and is_cancelled() and written == 0
    )
    success = (written > 0) and not cancelled
    # ... A fully-rendered file (written > 0) is kept even if cancel
    # arrived mid-stream ...
    ```
    证据（并行视频，删除产物+失败）:
    ```python
    cancelled = (
        is_cancelled is not None and is_cancelled() and written == 0
    )
    if ok and not cancelled:          # render_frames_parallel 取消时 ok=False
        ...
    # written>0 的中途取消走下面：close → _safe_remove(output_path)
    return RenderOutputResult(
        success=False, ...,
        error="EXPORT_CANCELLED" if cancelled
        else (state["error"] or "Render failed"),   # cancelled=False → "Render failed"
    )
    ```
  - 影响: 同一用户动作（渲染中途点取消）结果相反：N=1 时保留半成品文件并 `finished.emit`（UI 报"导出成功"）；N≥2 时删除文件并以 `"Render failed"` 报错（丢失用户已渲染等待的产物，且错误文案误导）。`_run_parallel_sequence_stage` 同病：中途取消删除已写帧、报 "Render failed"，与串行序列路径（保留+成功）相反。954-956 行注释声称 "Serial-path parity"，与 521-524 行串行注释/行为直接矛盾。
  - 建议: 统一两条路径的取消契约（建议：written>0 的取消保留产物并按取消/成功-with-warning 上报，或两侧都删除），并确保并行路径中途取消时 error 为 "EXPORT_CANCELLED"。
  - 置信度: 高

- [AUD-81002] [Major] render_image_sequence 无异常路径清理：渲染/保存抛异常时已写帧残留在磁盘，与 render_video 的失败清理不对称
  - 位置: src/features/composite/render/composite_render_output.py:632-692（串行循环无 try/except）、1061-1071（并行 stage 的 finally 只清理 GL renderer）
  - 证据: render_video 有完整兜底（533-540 行）:
    ```python
    except Exception as e:
        logger.exception("Video render failed")
        try:
            encoder.close()
        except Exception:
            pass
        self._safe_remove(output_path)
        return RenderOutputResult(success=False, error=str(e))
    ```
    而 render_image_sequence 的串行循环（632 行起）与并行 stage 调用（609-631 行）均未包裹 try；并行 stage 内:
    ```python
        try:
            ok = render_frames_parallel(...)
        finally:
            self._cleanup_parallel_renderers()   # 不清理已写文件
    ```
  - 影响: 序列导出中途抛异常（GL 错误、`FileIO.save_image`/目录 IO 异常等）时，异常直接冒泡到 worker（报 error 信号），但已写入的部分帧文件（written_paths）不会被 `_safe_remove` 清理，用户得到报错 + 半套残留序列。
  - 建议: 仿照 render_video，为序列路径（含并行 stage）增加异常兜底：清理 written_ref 中已写文件并尝试移除空目录后返回失败结果。
  - 置信度: 高

- [AUD-81003] [Major] GL 安全网 `_cleanup_parallel_renderers` 在调用者线程销毁可能仍被 worker 线程使用的 renderer（join 仅等 5 秒），cleanup() 无任何并发防护
  - 位置: src/features/composite/render/composite_render_output.py:847-856（安全网）、964/983/990（每条路径都会执行）；联动 src/features/composite/render/parallel_frame_render.py:261-262（`t.join(timeout=5.0)`）
  - 证据:
    ```python
    def _cleanup_parallel_renderers(self) -> None:
        """Safety net: release renderers whose worker never tore down."""
        for renderer in list(getattr(self, "_parallel_renderers", {}).values()):
            try:
                renderer.cleanup()
            except Exception as e:
                logger.debug(f"Parallel renderer cleanup error: {e}")
    ```
    parallel_frame_render 的 worker join 有超时:
    ```python
        finally:
            stop_event.set()
            collector.finish()
            for t in threads:
                t.join(timeout=5.0)
    ```
    且 `CompositeFrameRenderer.cleanup()`（composite_frame_renderer.py:571-595）逐字段置 None，无锁、无线程亲和校验。
  - 影响: 取消或收尾时若某 worker 仍在渲染超过 5 秒的重帧（4K 复杂合成常见），join 超时后 caller 线程对正在 `render_comp_clip_frame` 中使用的 renderer 执行 `cleanup()`：跨线程释放 GL 上下文/纹理 → 段错误或未定义行为（daemon 线程随后继续使用已释放资源）。本文件自身在 833-839 行明确写明 GL 释放是线程亲和的，安全网却违反该约束且并非仅异常路径才执行。
  - 建议: 安全网仅对"确认 worker 已退出"（join 返回 True / teardown 已回调）的 renderer 兜底；对 join 超时的 worker 记录泄漏并跳过跨线程 cleanup，或在 renderer 内加活跃渲染计数/锁后再释放。
  - 置信度: 中（需要 >5s 的收尾/重帧窗口才触发，但代码路径确凿、cleanup 无防护已核实）

- [AUD-81004] [Major] 渲染为 None 的帧被静默跳过且 pts 连续重排，输出视频时长缩短、后续帧时间轴前移（音画失步风险）
  - 位置: src/features/composite/render/composite_render_output.py:466-512（串行）、915-930（并行 _sink）
  - 证据:
    ```python
    if frame_img is None:
        logger.warning(
            f"Frame {frame_no} rendered None — skipping"
        )
        if progress_cb is not None:
            progress_cb(written, total)
        continue          # written 不递增
    ...
    decoded = DecodedFrame(
        frame_index=written,
        pts=written,      # 后续帧整体前移，压缩时间轴
        ...
    ```
    并行 _sink 同样跳过:
    ```python
    def _sink(frame_no: int, packed) -> None:
        nonlocal written
        if packed is None:
            return  # frame rendered None — skip, matching serial path
    ```
  - 影响: 某帧渲染失败（返回 None）时导出仍报 success=True；视频比请求范围短（总时长 = (total-失败数)/fps），失败帧之后的所有帧 pts 前移，与音频/时间码失步；用户只在一行 warning 中得知丢帧。
  - 建议: 对导出场景把 None 帧视为错误（失败/中止并清理），或以占位帧保序（如复制上一帧）并在结果中上报 dropped 计数。
  - 置信度: 中（skip 是有意设计，但时间轴压缩/静默 success 的后果明确）

- [AUD-81005] [Suggestion] `_render_one_frame` 存在死代码：局部 renderer/timeline_id/clip_id/asset_id 计算后全部弃用，与 `_render_one_frame_with` 内重复实现
  - 位置: src/features/composite/render/composite_render_output.py:696-729
  - 证据:
    ```python
    renderer = self._get_frame_renderer()

    timeline_id = (
        graph_data.get("timeline_id")
        or graph_data.get("composite_id")
        or ""
    )
    ...（clip_id/asset_id 同样计算）
    return self._render_one_frame_with(
        self._get_frame_renderer(), graph_data=graph_data, ...
    ```
  - 影响: 706-723 行的 4 个局部变量从未使用（ID 解析在 `_render_one_frame_with` 743-758 行重复一遍）；维护时两处易失同步，`renderer` 变量具有误导性。
  - 建议: 删除死变量，`_render_one_frame` 直接委托 `_render_one_frame_with(self._get_frame_renderer(), ...)`；ID 解析保留单份。
  - 置信度: 高

- [AUD-81006] [Suggestion] `ext` 未做输入验证，且 613 行 walrus 重复归一化导致空/非法 ext 时串行与并行路径产出文件名不一致
  - 位置: src/features/composite/render/composite_render_output.py:604、613、661
  - 证据:
    ```python
    ext_norm = ext.lower().lstrip(".")
    ...
    stage = self._run_parallel_sequence_stage(
        ...
        ext=ext_norm if (ext_norm := ext.lower().lstrip(".")) else "png",
    ```
    串行路径文件名:
    ```python
    fname = f"{base_name}_{str(seq_index + 1).zfill(pad_width)}.{ext_norm}"
    ```
  - 影响: `ext` 为 "" 或 "." 时，并行 stage 用 "png"，但 `ext_norm`（walrus 赋回空串）在串行回退路径生成 `comp_0001.` 这类无扩展名文件；两条路径对同一输入产出不同结果。ext 非法值应尽早校验而非静默分叉。
  - 建议: 入口统一 `ext_norm = ext.lower().lstrip(".")`，为空/不含白名单时返回失败或默认 "png"，删除 613 行的 walrus 重复计算。
  - 置信度: 高（行为可由代码直接推出，触发需调用方传非法 ext）

- [AUD-81007] [Suggestion] `_fps_to_rational` 对 fps≤0 无防护，0/负帧率会生成 Rational(0,1)/负有理数直传编码器
  - 位置: src/features/composite/render/composite_render_output.py:1153-1157（调用点 395）
  - 证据:
    ```python
    rounded = round(fps)
    if abs(fps - rounded) < 1e-3 and rounded > 0:
        return Rational(int(rounded), 1)
    return Rational(int(round(fps * 1000)), 1000)
    ```
  - 影响: `rounded > 0` 只保护整数分支；fps=0.0 落入兜底分支得 Rational(0,1)（Rational 仅校验 den≠0，num=0 合法），负 fps 得负有理数，随后进入 FFmpegEncoderConfig.fps 产生非法流或深层晦涩报错。函数内 `time_seconds` 有 `if fps > 0` 守卫（505 行），说明 0 帧率并非不可能到达此处。
  - 建议: 在 render_video 入口校验 `fps > 0`（宽高同理），或 `_fps_to_rational` 对 fps≤0 抛出明确 ValueError。
  - 置信度: 中（fps 来源于上层设置，正常 UI 不易传 0；防护缺失确凿）

- [AUD-81008] [Suggestion] 逐帧循环内的重复 import 与串行/并行两份重复的转换打包代码
  - 位置: src/features/composite/render/composite_render_output.py:488-500（串行循环内 import + 转换）、880-894（并行 _convert 重复同样逻辑）、1037-1038 与 1054（每帧/每 sink import 自身模块函数与 FileIO）
  - 证据:
    ```python
    for frame_no in range(start_frame, end_frame + 1):
        ...
        from src.shared.color.color_transform_provider import transform_color
        rgb_linear = np.ascontiguousarray(frame_img[..., ::-1])
        ...
        frame_out = np.clip(frame_enc * 65535.0, 0, 65535).astype(np.uint16)
    ```
    并行闭包内 import 自身模块:
    ```python
    from src.features.composite.render.composite_render_output import (
        _convert_for_image_ext)
    ```
  - 影响: 每帧执行 sys.modules 查找（微小但无意义的每帧开销）；BGR↔RGB 翻转、HDR 16bit/SDR 8bit 打包逻辑存在两份（串行/并行），改动（如新增描述符逻辑）易只改一处造成两路径色彩不一致。
  - 建议: import 提升到方法/模块顶部或 stage 初始化时执行一次；提取 `_encode_frame_for_video(frame_img, is_hdr)` 供两路共用。
  - 置信度: 高

- [AUD-81009] [Suggestion] `_run_parallel_video_stage` 的 `start_frame` 形参从未使用；两个并行 stage 辅助方法缺参数与返回值类型注解
  - 位置: src/features/composite/render/composite_render_output.py:858-863、1000-1005
  - 证据:
    ```python
    def _run_parallel_video_stage(
        self, graph_data, encoder, frames, start_frame, fps, is_hdr,
        output_descriptor, edit_project, timeline_sequences, comp_clip,
        render_pass, worker_count, is_cancelled, progress_cb,
        output_path: str = "",
    ):
    ```
  - 影响: `start_frame` 传入后函数体内无引用（误导调用方）；两个 stage 是本文件最复杂的方法却无任何参数/返回类型注解（返回 RenderOutputResult | None 的联合语义只能靠 docstring），不符合项目强类型约束。
  - 建议: 删除未用形参；补全 `-> Optional[RenderOutputResult]` 与参数注解。
  - 置信度: 高

### 通过项
- GLSL·HDR 正确性：HDR(PQ/HLG) 自动升级 yuv420p10le、宽色域 SDR 升 10bit；bt2020→bt2020nc、P3→bt709 矩阵符合规范；bgr48le 16bit 满量程打包与 swscale 语义注释一致；EXR 保持 scene-linear 不做无理由 clamp（clip 仅发生在 transfer 编码后的显示域量化，合理）
- 线程安全：progress 等经回调/信号跨线程；`_parallel_renderers` 在 stage 启动前初始化，dict 单键写入在 CPython 下安全；`_cleanup_parallel_renderers` 用 list() 快照避免迭代中变更
- 信号防递归：本文件无 UI 控件/信号回路，不适用（无问题）
- 状态提交一致性：纯导出编排，不触碰 undo/redo/revision/持久化，无耦合风险
- GPU 生命周期（常规路径）：`_make_parallel_render_fn` 工厂失败时显式 cleanup 防上下文泄漏（808-819）；teardown_fn 保证 GL 资源在属主线程销毁；serial `_frame_renderer` 在 finally 清理
- 异常边界：render_video 顶层 except 记录堆栈、关闭编码器并清理产物，未吞错
- render_image 主线程执行并依赖活动纹理，符合"UI 线程不操作 OpenGL"铁律的既定设计（渲染在主线程同步完成的既有路径）

---

## src/features/composite/render/composite_render_worker.py (行数: 286)

### 发现

- [AUD-81010] [Major] worker 线程在首个任务结束即 quit，而 export() 只防并发不防复用：第二次 export 的任务会静默丢失（甚至可能在已 deleteLater 的 QObject 上 invokeMethod）
  - 位置: src/features/composite/render/composite_render_worker.py:187-191、220-236
  - 证据:
    ```python
    self._worker.finished.connect(self._worker_thread.quit)
    self._worker.error.connect(self._worker_thread.quit)
    self._worker_thread.finished.connect(self._worker.deleteLater)
    ...
    def export(self, ...):
        if self._is_exporting:
            logger.warning("CompositeRenderExporter.export() called while busy")
            return
        ...
        QMetaObject.invokeMethod(
            self._worker, "run",
            Qt.ConnectionType.QueuedConnection, ...)
    ```
  - 影响: 第一次导出的 finished/error 会退出 QThread 事件循环并 deleteLater worker；此后再调用 export()（`_is_exporting` 已复位为 False，守卫不拦截）把 run 事件投递到已退出/正在退出的线程——任务不执行、无任何错误上报（或在 worker 已被销毁时对悬空 QObject 调用 invokeMethod）。类提供 `is_exporting`/`cancel()` 且构造即启动线程，天然诱导复用；仅 docstring 提示"完成后 cleanup"，未做任何防护。
  - 建议: 要么在 finished/error 后禁止再次 export 并显式报错（置 `_terminated` 标志），要么不连接 quit、由 cleanup() 统一控制线程生命周期。
  - 置信度: 中（取决于调用方是否复用；Qt 事件丢弃/悬空指针语义未在本仓库实测，但代码路径明确）

- [AUD-81011] [Suggestion] `_cancel_requested` 布尔字段只写不读，为死状态
  - 位置: src/features/composite/render/composite_render_worker.py:65、148-149、153-154
  - 证据:
    ```python
    self._cancel_requested = False
    self._cancel_event = threading.Event()
    ...
    def request_cancel(self) -> None:
        self._cancel_requested = True
        self._cancel_event.set()
    ```
  - 影响: 实际取消判断全部走 `_cancel_event.is_set`，布尔字段无任何读取点，误导维护者以为存在双通道状态。
  - 建议: 删除 `_cancel_requested`，仅保留 threading.Event（本身线程安全且可跨线程 set/clear）。
  - 置信度: 高

- [AUD-81012] [Suggestion] run() 对 settings/context 直接下标取键，缺键时报错信息不可读且无前置校验
  - 位置: src/features/composite/render/composite_render_worker.py:98-126
  - 证据:
    ```python
    result = renderer.render_video(
        graph_data=context["graph_data"],
        output_path=settings["output_path"],
        width=settings["width"],
        height=settings["height"],
        fps=settings["fps"],
    ```
  - 影响: settings 来自对话框 get_settings()，若键缺失（如版本演进/调用方拼装不全），异常冒泡到 142 行 except 后 error.emit(str(e)) 只含 `"'output_path'"` 之类的 KeyError 文案，用户无法定位；不符合"外部数据须验证"的项目约束精神。
  - 建议: run() 开头校验必需键（kind/output_path/graph_data/fps/宽高/帧区间等），缺失时 emit 带字段名的明确错误。
  - 置信度: 高

### 通过项
- 线程安全：progress/finished/error 经 Qt 信号跨线程转发（queued），未在工作线程直接触碰 UI；`_cancel_event`（threading.Event）的 set/clear/is_set 均线程安全；invokeMethod 用 QueuedConnection 正确投递到 worker 线程
- GPU 资源生命周期：GL 上下文由 CompositeRenderOutput/CompositeFrameRenderer 在 worker 线程内创建与销毁，本文件不接触 OpenGL API（符合铁律）
- 信号防递归：worker 信号只单向汇报，UI 侧回调异常被捕获且不再回流触发渲染，无递归回路
- 异常与取消路径：run() 顶层 try/except 记录堆栈并 emit error，不会让异常静默杀线程；cancel 为协作式（下一帧生效）符合设计
- 状态提交一致性：不涉及 undo/redo/revision/持久化
- GLSL·HDR 正确性：颜色编码全部委托 CompositeRenderOutput/ColorTransformProvider，本文件无色彩处理逻辑
