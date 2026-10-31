# batch-025 — src/app/controllers/deliver_controller.py 静态审查

审查范围：交付/导出页控制器（单文件导出、快速导出、LUT、视频、批量导出、队列持久化）。
重点维度：2（导出任务生命周期）、6（异常与取消路径）、7（输入验证）、8（性能模式）。

## src/app/controllers/deliver_controller.py (行数: 1901)

### 发现

- [AUD-2500] [Major] Composite 导出 fallback 在 UI 线程直接创建 GL context/纹理执行 GPU 操作，且 context 从不释放、makeCurrent 后无 doneCurrent
  - 位置: src/app/controllers/deliver_controller.py:530-548, 608-609
  - 证据:
    ```python
        # 确保 GL context current
        if self.canvas and self.canvas.gl_viewport:
            self.canvas.gl_viewport.makeCurrent()

        try:
            import moderngl
            ctx = moderngl.create_context()
            ...
            color_tex = ctx.texture((w, h), 3, data=tex_data.tobytes(), dtype='f2')
        ...
            finally:
                color_tex.release()
    ```
  - 影响: 当 CompositeRenderer 未启动或 `is_running()` 为 False 时（render-thread 分支行 476-484 判定失败），单文件/面板/批量导出都会落到该 fallback，在 UI 线程执行 OpenGL（违反 `.clinerules` "UI 不得阻塞 / UI 线程不调 GPU"；注释行 466-470 自己承认这一点）。此外 `ctx` 没有 `release()`，`makeCurrent()` 后也没有 `doneCurrent()`；批量导出多文件时每个文件泄漏一个 GL context，与 QOpenGLWidget 的 surface 状态互相干扰，存在崩溃/渲染状态损坏风险。
  - 建议: fallback 仅限无 UI 的 CLI/测试路径；UI 路径上 render thread 不可用时直接报错并回退 Color 输出；若保留 fallback，使用 `with ctx:` 或显式 `ctx.release()` 并在结束处 `doneCurrent()`。
  - 置信度: 高

- [AUD-2501] [Major] 视频导出无防重入：直接覆盖 `_video_exporter` 引用
  - 位置: src/app/controllers/deliver_controller.py:933-934
  - 证据:
    ```python
        # 持有引用避免被 GC（导出结束后 cleanup 时清掉）
        self._video_exporter = VideoExporter(parent=self)
    ```
  - 影响: `perform_video_export` 未检查 `getattr(self, "_video_exporter", None)` 是否仍在 `is_exporting`（`_cancel_video_export` 行 738-739 说明该状态可用）。导出进行中再次触发快速导出会创建第二个 worker 并覆盖引用：旧 QThread 失联（`_cleanup_video_exporter` 只清理新的），两个 ffmpeg 可能同时写同一输出文件导致输出损坏，且旧 worker 的临时文件/句柄无人清理。
  - 建议: 启动前检查 `if getattr(self, "_video_exporter", None) is not None and self._video_exporter.is_exporting: QMessageBox 提示并 return`，或在导出期间禁用导出入口。
  - 置信度: 高

- [AUD-2502] [Major] 未知 codec 在 UI 事件处理器中直接 `raise ValueError`，无用户可见反馈
  - 位置: src/app/controllers/deliver_controller.py:861-865；调用链 export_file_as:1071-1079
  - 证据:
    ```python
        codec_enum = codec_map.get(codec_str)
        if codec_enum is None:
            raise ValueError(
                f"Video Export: 未知 codec {codec_str!r}（合法："
                f"{sorted(codec_map)}）——拒绝静默回退 H.264")
    ```
    调用方（行 1076-1077）`elif export_type == "video": self.perform_video_export(settings)` 无 try/except。
  - 影响: codec 字符串来自对话框 settings，属于外部输入。校验失败时异常沿 `export_file_as` 一路抛进 Qt 信号分发，用户看不到任何弹窗/状态栏提示（仅 stderr traceback），且后续的 Delivery Matrix 确认、进度条等全部跳过。输入校验（维度 7）应给出用户可见错误而非异常。
  - 建议: 改为 `QMessageBox.critical(self, ...)` + `return`，与行 895-898 `config.validate()` 的处理方式保持一致。
  - 置信度: 高

- [AUD-2503] [Major] VideoExporter 的进度/成功/失败回调直接操作 QWidget，若在 QThread 中被调用即跨线程 UI 操作
  - 位置: src/app/controllers/deliver_controller.py:936-947, 949-977
  - 证据:
    ```python
        def on_progress(current, total, percent):
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(int(percent))
            ...
            self.status_bar.showMessage(msg, 0)
            QApplication.processEvents()
        ...
        def on_error(error_msg):
            self.progress_bar.hide()
            ...
                QMessageBox.critical(self, ...)
    ```
    docstring（行 788-790）声明"在后台 QThread 中运行 TimelineExporter"。
  - 影响: 这些闭包被直接传给 worker（行 979-987）。若 `VideoExporter.export` 在工作线程里直接调用回调（回调式 API 而非 Qt signal），从非 GUI 线程操作 widget/processEvents/QMessageBox 属未定义行为，可致崩溃。本文件无法确认 worker 是否已用信号 marshal 回主线程，需跨文件核实。
  - 建议: 核实 `VideoExporter`；若回调在工作线程触发，改为 Qt 信号（自动排队到主线程）或 `QMetaObject.invokeMethod` 派发。
  - 置信度: 中

- [AUD-2504] [Major] RAW 批量导出白平衡提取失败时静默使用魔术值 [2.0, 1.0, 1.5, 1.0]，`except Exception: pass` 吞掉全部错误
  - 位置: src/app/controllers/deliver_controller.py:1647-1656
  - 证据:
    ```python
                camera_wb = [2.0, 1.0, 1.5, 1.0]
                try:
                    import rawpy
                    with rawpy.imread(file_path) as raw:
                        camera_wb = FileIO._get_normalized_camera_wb(raw)
                except Exception:
                    pass
                self.engine.current_raw_metadata = {"camera_wb": camera_wb}
    ```
  - 影响: rawpy 缺失/RAW 文件损坏/提取抛错时，该 RAW 文件以 R×2、B×1.5 的假想白平衡参与渲染，导出明显偏色（渲染错误），且无任何日志；同时该 except 也会吞掉与 WB 无关的意外异常（维度 6）。
  - 建议: fallback 至少退回中性 [1,1,1,1] 并 `logger.warning(..., exc_info=True)`；区分 ImportError 与解码错误。
  - 置信度: 高

- [AUD-2505] [Major] 批量导出在 UI 线程同步执行 `FileIO.load_image`（含 rawpy RAW 解码），阻塞主线程
  - 位置: src/app/controllers/deliver_controller.py:1590-1593（调度链 1329-1330 / 1565 的 QTimer.singleShot 均在 UI 线程）
  - 证据:
    ```python
        # 1. 加载图像
        if not self._batch_checkpoint():
            return None
        full, high, low = FileIO.load_image(file_path)
    ```
  - 影响: `_process_next_batch_file` 由 UI 事件循环驱动，`load_image` 是纯同步调用（大 JPEG/4000 万像素 RAW 解码可达数秒），期间 UI 完全冻结（`_batch_checkpoint` 只在阶段间泵事件，覆盖不到这里）。同样在 UI 线程的还有行 248/281/306 的 Lanczos4 大图 resize。违反 `.clinerules` "UI 不得阻塞"（维度 8）。
  - 建议: 把每个文件的 load+decode 移入 worker 线程（与 full-res render 相同的 AsyncRenderer 模式），UI 仅收结果；至少为 RAW 解码提供线程化路径。
  - 置信度: 高

- [AUD-2506] [Major] 批量导出完成后的状态恢复用 `is not None` 判断，导出前为 None 的字段会残留最后一个批文件的状态
  - 位置: src/app/controllers/deliver_controller.py:1771-1790（快照来源 1279-1295）
  - 证据:
    ```python
            if self._batch_original_layers is not None:
                self.engine.layers = self._batch_original_layers
            ...
            if self._batch_original_file_path is not None:
                self.engine.current_file_path = self._batch_original_file_path
            self.engine.is_current_image_srgb = self._batch_original_is_srgb
            ...
            if self._batch_original_image is not None:
                self.engine.img_full_original = self._batch_original_image
    ```
  - 影响: 若批量导出启动时未打开任何图（`current_file_path`/`layers`/`img_full_original` 均为 None），导出完成后 engine 的 `current_file_path`（行 1628 设置）、`layers`（行 1606/1611/1617 设置）、raw 元数据等停留在队列最后一个文件上；后续用户 Ctrl+S 会把当前（可能是空的）调色状态写进那个源文件的 .topos，造成数据覆盖。
  - 建议: 用哨兵区分"未快照"与"快照值为 None"（如启动时记录 `_had_file` 标志），恢复时无条件回写 None/空列表。
  - 置信度: 中

- [AUD-2507] [Major] float32 图像导出 `.webp` 缺少 dtype 转换分支，保存失败被误报为渲染失败
  - 位置: src/app/controllers/deliver_controller.py:151-160, 204-210, 1722-1731（ext 列表缺 '.webp'）；1736-1739 与 1526-1528
  - 证据:
    ```python
        if full_res_img.dtype == np.float32:
            if ext in ['.jpg', '.jpeg', '.png', '.bmp']:
                full_res_img = (np.clip(full_res_img, 0.0, 1.0) * 255.0).astype(np.uint8)
            elif ext in ['.tif', '.tiff']:
                full_res_img = (np.clip(full_res_img, 0.0, 1.0) * 65535.0).astype(np.uint16)
    ```
    批量命名却支持 WebP（行 1745：`fmt_map = {"JPEG": ".jpg", "PNG": ".png", "OpenEXR": ".exr", "TIFF": ".tif", "WebP": ".webp"}`）；保存失败时行 1528 统一记 `item.error = "render returned no output"`。
  - 影响: 选择 WebP 输出时 float32 图不经过 uint8 量化直接传 `FileIO.save_image`，OpenCV WebP 编码不接受 float32，保存失败；批量路径把真实原因（保存失败）掩盖成 "render returned no output"，排障方向被误导（维度 6/7）。
  - 建议: 把 '.webp' 加入 uint8 转换分支；`save_image` 返回 False 时记录具体 IO/编码错误而非笼统的 "render returned no output"。
  - 置信度: 中

- [AUD-2508] [Major] `_encode_composite_linear_to_srgb` 用宽泛 except 把色彩管理意外错误静默降级为 sRGB
  - 位置: src/app/controllers/deliver_controller.py:64-82
  - 证据:
    ```python
        try:
            from src.shared.color.color_space_descriptor import (...)
            from src.shared.color.color_transform_provider import transform_color
            dst = descriptor_from_config(config_manager, "output", "image", is_video=False)
            return transform_color(img, LINEAR_SCENE, dst, config_manager)
        except Exception as e:
            logger.warning(f"preference-driven image encode unavailable, using sRGB fallback: {e}")
    ```
  - 影响: except 本意是兜底 import/配置缺失，但 `transform_color` 内部任何 bug（如 LUT 加载失败、数组形状错误）也会被吞掉并静默退回内置 sRGB OETF；用户设置了 Rec.709 等输出偏好时导出与预览色彩不一致，仅一条 warning，违反 "导出与预览语义一致" 与 "不得宽泛 except 掩盖错误"。
  - 建议: 仅捕获 ImportError 与配置解析类异常；`transform_color` 的失败应显式失败或至少 `logger.warning(..., exc_info=True)` 并在 UI 提示降级发生。
  - 置信度: 中

- [AUD-2509] [Major] 批量导出无防重入：二次启动会覆盖引擎状态快照并重置进度
  - 位置: src/app/controllers/deliver_controller.py:1279-1306
  - 证据:
    ```python
        self._batch_original_image = self.engine.img_full_original.copy() if self.engine.img_full_original is not None else None
        self._batch_original_layers = None
        ...
        self._batch_settings = settings
        self._batch_files = files
        ...
        self._batch_index = 0
        self._batch_success_count = 0
    ```
  - 影响: `perform_batch_export` 未检查 `_batch_file_processing`/既有 `_batch_files` 是否仍在进行。若第一次批量导出运行中再次触发，快照会记录"已被第 N 个文件污染的引擎状态"（层列表、raw 参数、file_path 均是批处理中间态），最终 `_batch_export_complete` 恢复到错误状态；`_batch_index=0` 还会让两条 singleShot 链交叉处理同一文件列表。控制器层缺少 `_video_exporter` 式的 `is_exporting` 防护。
  - 建议: 入口处 `if getattr(self, '_batch_files', None) and not <已完成>: 提示并 return`（可复用 `_batch_file_processing`/完成标志），或由 export_panel 禁用开始按钮。
  - 置信度: 中

- [AUD-2510] [Suggestion] 8bit/16bit 量化用截断而非四舍五入，系统性偏暗约 0.5 LSB
  - 位置: src/app/controllers/deliver_controller.py:155, 157, 208, 210, 1726, 1728
  - 证据:
    ```python
        if ext in ['.jpg', '.jpeg', '.png', '.bmp']:
            full_res_img = (np.clip(full_res_img, 0.0, 1.0) * 255.0).astype(np.uint8)
    ```
  - 影响: `astype(np.uint8)` 向零截断，等效于对每个像素减去平均 0.5/255 的偏置，与预览（通常 round）存在细微不可感知但系统性的偏差；维度 3（量化一致性）。
  - 建议: 统一为 `(np.clip(x,0,1) * 255.0 + 0.5).astype(np.uint8)` 或 `np.rint`，抽成单一工具函数。
  - 置信度: 高

- [AUD-2511] [Suggestion] dtype/格式转换逻辑在三个导出路径中重复三份
  - 位置: src/app/controllers/deliver_controller.py:151-160, 204-210, 1722-1731
  - 证据: 三段几乎相同的 `if dtype == np.float32: ... elif np.uint16: ...` 分支（仅变量名不同）。
  - 影响: AUD-2507 的 WebP 遗漏正是三份拷贝各漏一份的典型后果；后续新增格式需改三处。
  - 建议: 提取 `_convert_export_dtype(img, ext)` 工具函数，三处调用。
  - 置信度: 高

- [AUD-2512] [Suggestion] `_restored_queue_settings` 写后从未读取（死状态）
  - 位置: src/app/controllers/deliver_controller.py:1315, 1879
  - 证据:
    ```python
            self._restored_queue_items = None
            self._restored_queue_settings = None      # 行 1315：仅置空
        ...
            self._restored_queue_settings = settings   # 行 1879：仅赋值
    ```
  - 影响: 恢复队列时的持久化 settings 被缓存后从未参与续渲（行 1298 直接用当次传入的 settings），恢复后用户若修改导出设置再点开始，将以新设置渲染旧队列而无提示；至少是无用状态。
  - 建议: 要么在 `is_resume` 分支用 `_restored_queue_settings` 校验/合并（提示用户设置已变化），要么删除该字段。
  - 置信度: 高

- [AUD-2513] [Suggestion] 批量导出未注入 `preserve_metadata` 配置，与单文件导出行为不一致
  - 位置: src/app/controllers/deliver_controller.py:1736-1737（对比 100-102, 183-185）
  - 证据:
    ```python
        options = self._batch_settings.get("options", {})
        if FileIO.save_image(output_path, full_res_img, options=options):
    ```
    而单文件路径先做 `settings["options"]["preserve_metadata"] = _config_bool(self.config_manager.get("preserve_metadata"), default=True)`。
  - 影响: 同一 `preserve_metadata` 配置在单张导出生效、批量导出不生效（取决于 FileIO 默认值），导出语义不一致（维度 3/5）。
  - 建议: 批量初始化 options 时同样注入该键。
  - 置信度: 中

- [AUD-2514] [Suggestion] 两处宽泛 except 静默吞错：`_open_export_folder` 的 `except Exception: pass` 与 render-thread 分支日志缺 traceback
  - 位置: src/app/controllers/deliver_controller.py:622-630, 526-528
  - 证据:
    ```python
        try:
            if platform.system() == 'Darwin':
                subprocess.Popen(['open', folder])
            ...
        except Exception:
            pass
    ```
    以及 `except Exception as e: logger.error(f"Render-thread composite export error: {e}")`（对比 fallback 分支行 613-614 有 `traceback.print_exc()`）。
  - 影响: 打开文件夹失败用户完全无感知；composite 导出主路径（render-thread 分支）的异常只有消息字符串，无堆栈，排障困难。
  - 建议: 前者至少 `logger.debug(..., exc_info=True)`；后者加 `exc_info=True`。
  - 置信度: 高

- [AUD-2515] [Suggestion] 主要导出入口缺少参数/返回类型注解
  - 位置: src/app/controllers/deliver_controller.py:92, 174, 225, 252, 787, 1013, 1083, 1252, 1588
  - 证据: `def perform_panel_export(self, settings):`、`def perform_quick_export(self, settings):`、`def perform_video_export(self, settings):`、`def perform_batch_export(self, settings):`、`def export_file_as(self):` 等均无注解（对比 `_render_composite_full_res`、`_video_export_elapsed` 已有完整注解）。
  - 影响: settings dict 的结构（path/options/codec/files 等）只能靠读实现才能得知，易在多处 get 默认值间漂移（本文件已出现 AUD-2513 的键不一致）。
  - 建议: 为公开入口补 `settings: dict` / `-> None` 注解，更进一步定义 TypedDict 描述导出 settings。
  - 置信度: 高

- [AUD-2516] [Suggestion] 批量暂停用 `processEvents + sleep(0.01)` 忙等轮询
  - 位置: src/app/controllers/deliver_controller.py:1465-1471
  - 证据:
    ```python
        QApplication.processEvents()
        while getattr(self, "_batch_paused", False) and not getattr(
            self, "_batch_cancelled", False
        ):
            QApplication.processEvents()
            time.sleep(0.01)
    ```
  - 影响: 暂停期间以 100Hz 持续泵事件+轮询，占用 CPU；嵌套 `processEvents` 也放大了暂停期间的用户重入面（可再次触发导出/打开对话框）。
  - 建议: 改为 QEventLoop + pause/resume 信号 quit，或 QTimer 低频轮询（如 100ms）。
  - 置信度: 高

- [AUD-2517] [Suggestion] `export_srt` 全部对话框以 None 为 parent
  - 位置: src/app/controllers/deliver_controller.py:1090, 1097, 1112-1117, 1133, 1138, 1142
  - 证据:
    ```python
        QMessageBox.warning(None, "Subtitle Export",
                            "No active timeline. ...")
        ...
        file_path, _ = QFileDialog.getSaveFileName(
            None,
            "Export Subtitles (SRT)",
    ```
  - 影响: 无父窗口的对话框可能落在主窗体之下（z-order），看起来像"没反应"。
  - 建议: 统一传 `self` 作为 parent。
  - 置信度: 高

- [AUD-2518] [Suggestion] composite 导出结果经过 sRGB 编码后再进入 `convert_for_export`，存在双重编码风险，需复核
  - 位置: src/app/controllers/deliver_controller.py:130-149（panel）/ 190-202（quick）
  - 证据:
    ```python
                composite_img = self._render_composite_full_res(
                    full_res_img, keep_linear=keep_linear
                )
                if composite_img is not None:
                    full_res_img = composite_img
        ...
        source_space = getattr(self, '_source_color_space', None)
        full_res_img = self.color_manager.convert_for_export(full_res_img,
                                                             source_space=source_space)
    ```
  - 影响: `_render_composite_full_res` 在非 keep_linear 时已把结果编码为显示空间（行 520-524），随后 `convert_for_export` 又以 `_source_color_space`（源图空间，可能是 linear）为输入假设再转换一次；若 convert_for_export 对 float32 显示编码数据不设防，则导出比预览偏亮（双重 OETF）。本文件无法确认 `convert_for_export` 的内部判断，需跨文件核实。
  - 建议: 为 composite 路径跳过或修正 `source_space`（告知 convert 输入已是输出编码空间），并加一个已知值的回归测试。
  - 置信度: 低

### 通过项

- 视频导出取消路径：cancel 后禁用取消按钮并提示"正在取消"（行 764-775）；`EXPORT_CANCELLED` 与真实失败区分，取消不弹错误框（行 962-977）。
- 批量导出取消语义：取消的项回置 STATUS_PENDING 供续渲重试而非误记 FAILED（行 1518-1533）；取消转发至 `engine.cancel_full_resolution()` 及时中断当前渲染（行 1368-1381）。
- 批量队列持久化：启动、每项渲染前后均 `save_batch_queue`（行 1322, 1505, 1547），closeEvent 兜底（行 1889-1901）；全部成功才 `clear_batch_queue`，有未完成/失败项保留供恢复（行 1802-1808）。
- 输入验证（部分）：批量导出校验文件列表与导出目录有效性（行 1254-1262）；视频 range_end < range_start 时警告并回退整段（行 820-827）；`config.validate()` 错误以对话框展示（行 894-898）。
- composite 导出帧号与预览一致：`_resolve_export_frame` 单文件路径取播放头当前帧而非静默第 0 帧（行 375-401）；批量临时图与 UI composite_page 隔离（行 555 附近，`_normalize_standalone_graph_for_batch` 只改内存临时图）。
- HDR/线性保持正确：EXR 与 .toos 浮点档 `keep_linear` 贯穿 panel/quick/batch 三条路径（行 127-128, 260-261, 1687-1690），显示格式统一走 OETF 编码。
- 信号防递归：本文件不含 programmatic 更新控件再触发信号的链路（仅单向 set 显示），无状态锁需求（维度 4 不适用）。
- `_auto_save_current_file_before_batch` 的宽泛 except 有日志、有明确的降级理由注释（行 1364-1366），属可接受降级。
- SRT 导出全程 try/except + `logger.exception`，错误不吞（行 1125-1143）。
- `_build_timeline_info` 的兜底 except 返回空结构并 log warning（行 1244-1246），降级安全。
- 批量暂停/恢复状态机：`_resume_batch_export` 检查 `_batch_file_processing` 防止重入双链（行 1449-1454）。

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 10 | AUD-2500 ~ AUD-2509 |
| Suggestion | 9 | AUD-2510 ~ AUD-2518 |
