# Batch 024 — app/controllers/composite_controller.py 静态审查

- 审查日期：2026-09-28
- 审查方式：只读静态审查（Read 全文 1,352 行），未运行任何代码
- 审查范围：src/app/controllers/composite_controller.py（CompositeControllerMixin，合成页/节点编辑控制器）
- 问题编号区间：AUD-2400–AUD-2499（实际使用 AUD-2401–AUD-2415）
- 严重度统计：Critical 0 · Major 7 · Suggestion 8

## src/app/controllers/composite_controller.py (行数: 1352)

### 发现

- [AUD-2401] [Major] 视频/序列渲染回调闭包直接操作 Qt 控件，跨线程调用安全性完全依赖 exporter 实现
  - 位置: src/app/controllers/composite_controller.py:1215-1270（另见 1053、1206）
  - 证据:
    ```python
    def on_progress(current: int, tot: int) -> None:
        self.progress_bar.setRange(0, max(1, tot))
        self.progress_bar.setValue(current)
        ...
        self.status_bar.showMessage(msg, 0)
        QApplication.processEvents()

    def on_error(error_msg: str) -> None:
        self.progress_bar.hide()
        ...
            QMessageBox = self._import_qmessagebox()
            QMessageBox.critical(
                self,
    ```
  - 影响: `CompositeRenderExporter.export()` 接收的是裸 Python 回调而非 Qt 信号。若 exporter 在 worker 线程直接调用这些回调，`progress_bar`/`status_bar`/`QMessageBox.critical` 将被跨线程访问（违反 .clinerules UI 线程约束），可致崩溃或未定义行为——此情形应升级为 Critical。即使在 UI 线程调用，每帧 `QApplication.processEvents()`（1225）与渲染前 processEvents（1053、1206）也构成重入窗口：用户可在同步图像渲染期间再次触发菜单/关闭窗口。
  - 建议: 回调经 Qt 信号（queued connection）或 `QMetaObject.invokeMethod` 封送回 UI 线程；渲染期间用忙碌态屏蔽重复触发，去掉逐帧 processEvents。
  - 置信度: 中（exporter 在本批次受审文件之外，调用线程无法在本文件内证实）

- [AUD-2402] [Major] 重复启动渲染会静默覆盖 `_composite_render_exporter`，旧任务回调会清掉新任务的取消入口
  - 位置: src/app/controllers/composite_controller.py:1209、1240、1262、1278-1297
  - 证据:
    ```python
    # Hold a reference to avoid GC; cleared in cleanup.
    self._composite_render_exporter = CompositeRenderExporter(parent=self)
    ...
    self._composite_render_exporter.export(
        settings=settings,
        context=context,
        on_finished=on_finished,
        on_error=on_error,
        on_progress=on_progress,
    )
    ```
    而 `on_finished`/`on_error` 末尾均执行 `self._cleanup_composite_render_exporter()`。
  - 影响: `_run_composite_video_render` 启动前没有 `is_exporting` 守卫（`cancel_composite_render` 1275 行明明有该属性可用）。第一次渲染进行中再次点 Render 并确认对话框，会直接覆盖引用：旧 exporter 仅靠 Qt parent 保活、其完成回调随后调用 `_cleanup_composite_render_exporter()`，清掉的是新渲染的取消过滤器与 exporter 引用——新渲染失去取消入口，两个导出任务并发竞争输出路径/进度条。
  - 建议: 启动前检查 `getattr(self, "_composite_render_exporter", None)?.is_exporting`，正在导出时拒绝或先 `cancel()` + 清理旧实例；回调闭包捕获并比对本次 exporter 实例再执行清理。
  - 置信度: 高

- [AUD-2403] [Major] `exporter.export()` 同步抛异常时进度条、取消过滤器、光标与状态栏消息全部残留
  - 位置: src/app/controllers/composite_controller.py:1182-1270（关键 1264-1270）；对照图像路径的 finally（1115-1116）
  - 证据:
    ```python
    self._composite_render_exporter = CompositeRenderExporter(parent=self)
    ...
    self._composite_render_exporter.export(
        settings=settings,
        context=context,
        on_finished=on_finished,
        on_error=on_error,
        on_progress=on_progress,
    )
    ```
    （此前 1190-1205 已执行 `progress_bar.show()`、setCursor、`installEventFilter`；此处无 try/except/finally。）
  - 影响: `export()` 在派发 worker 前因非法 settings/IO/初始化错误同步抛出时，UI 停留在“正在渲染合成视频...”+可见进度条+手形光标+点击即取消过滤器（此后任何点击进度条都会触发对已失效 exporter 的 cancel），且 `_composite_render_exporter` 已被赋值但清理回调不会被调用。图像路径有 `finally: self.progress_bar.hide()`，视频路径缺失等价物。
  - 建议: 将 1264-1270 包入 try/except，异常时走与 `on_error` 相同的状态复位（或至少调用 `_cleanup_composite_render_exporter()` + 隐藏进度条）。
  - 置信度: 高

- [AUD-2404] [Major] 多处宽泛 except 吞错（含裸 pass 与 debug 级日志），可掩盖用户编辑丢失与旧帧缓存残留
  - 位置: src/app/controllers/composite_controller.py:178-181、203-207、415-435、1095-1098（另见 1283-1290 teardown）
  - 证据:
    ```python
    try:
        self.timeline_preview_renderer.invalidate_comp_clip_cache(composite_id)
    except Exception:
        pass
    ...
    try:
        graph_data = page.get_composite_data()
    except Exception as e:
        logger.debug(f"[CompClip] Cannot serialize composite on leave: {e}")
    ...
    try:
        self.color_manager.embed_icc(path)
    except Exception:
        pass
    ```
  - 影响: 直接违反 `.clinerules` “不得宽泛 except 掩盖错误”。最严重的两处：(1) `_flush_composite_before_leave` 中序列化失败仅记 debug 且随后 `if not graph_data: return`——用户切页时合成图编辑静默丢失（数据丢失级后果）；(2) 缓存失效失败被 pass 后，正是 176 行注释描述的“循环播放命中旧帧、内容/色彩跳变”bug 会无诊断地复现。
  - 建议: 至少收窄异常类型；序列化/缓存失效失败需 warning 级日志并通知用户（状态栏或弹窗），不允许静默 return。
  - 置信度: 高

- [AUD-2405] [Major] `_last_metadata` 属性存在但为 None 时，点击渲染输出即 AttributeError
  - 位置: src/app/controllers/composite_controller.py:924
  - 证据:
    ```python
    available_render_passes = {"beauty"}
    last_aovs = getattr(self.composite_page, "_last_metadata", {}).get("aov", {})
    if isinstance(last_aovs, dict):
        available_render_passes.update(str(name) for name in last_aovs)
    ```
  - 影响: `getattr(..., {})` 的默认值只在属性缺失时生效；若 CompositePage 将 `_last_metadata` 初始化为 `None`（未收到首帧元数据的常见初始态），表达式变成 `None.get("aov", {})`，在 Output 节点 Render 按钮处理器内直接抛 AttributeError，渲染输出功能不可用。
  - 建议: `meta = getattr(self.composite_page, "_last_metadata", None) or {}` 后再取 `meta.get("aov", {})`。
  - 置信度: 中（CompositePage 的初始化值不在本文件，但该防御模式明显不覆盖 None）

- [AUD-2406] [Major] 工程文件图数据的数值字段未校验即 int() 强转，损坏数据可致页面切换/渲染输出崩溃
  - 位置: src/app/controllers/composite_controller.py:610-611、657-658、1307-1310
  - 证据:
    ```python
    if isinstance(graph_data, dict):
        width = int(graph_data.get("width", width) or width)
        height = int(graph_data.get("height", height) or height)
    ...
    clip_duration = int(graph_data.get('duration_frames', 0) or 0)
    ...
    if params.get("format_mode") in ("custom", "project"):
        w = int(params.get("width", 1920) or 1920)
    ```
  - 影响: `or width` 只挡住 None/0，挡不住字符串。旧版/手改/损坏工程文件中 `width: "1920"` 或 `"3840px"` 会在进入合成页（`_setup_timeline_clip_context`，无外层 try）或渲染输出（`_resolve_output_resolution`）抛 ValueError，页面切换失败（维度 7 输入验证）。
  - 建议: 抽一个 `_safe_int(value, default)` 工具（try int() + 范围钳制）统一处理工程文件来源的数值。
  - 置信度: 中（触发依赖损坏输入，但代码路径确定无防护）

- [AUD-2407] [Major] `keep_linear=(ext == "exr")` 把 EXR 之外的一切格式都按显示编码处理，浮点 HDR 格式的线性语义被破坏
  - 位置: src/app/controllers/composite_controller.py:1061-1068、1084-1089
  - 证据:
    ```python
    # EXR keeps scene-linear (HDR); display formats get linear→sRGB
    # encoded inside _render_composite_full_res so the export matches
    # the preview.
    ext = settings.get("image_format", "png").lower()
    export_img = self._render_composite_full_res(
        color_img, keep_linear=(ext == "exr"),
    ```
  - 影响: HDR 保持线性的判定被硬编码为仅 EXR。若输出格式为 `.hdr` 等 float 格式（dtype 转换表 1085-1089 不含 hdr，保持 float32），场景线性数据会先被 sRGB 显示编码再存成浮点文件——HDR 语义破坏、下游合成读到的是已显示编码的值（维度 3）。
  - 建议: 以格式能力表驱动（`LINEAR_FORMATS = {"exr", "hdr", ...}`）而非单点字符串比较；与导出对话框的格式列表对齐。
  - 置信度: 中（对话框是否提供 .hdr 不在本文件；条件触发）

- [AUD-2408] [Suggestion] 图数据加载路径无节点类型/连线完整性校验（渲染路径有 preflight，加载路径没有）
  - 位置: src/app/controllers/composite_controller.py:37-109、529-543（对照 961-1003 的 `run_preflight_data`）
  - 证据:
    ```python
    if composite_id and self._current_edit_project:
        composite_data = self._current_edit_project.get_composition_by_id(composite_id)
        if composite_data and self.composite_page:
            composite_data = self._prepare_composite_data_for_open(
                composite_id, composite_data)
            self.composite_page.load_composite_data(composite_data)
    ```
  - 影响: `_prepare_composite_data_for_open` 只补 identity 元数据字段，不校验 nodes/connections 结构；未知节点类型、引用不存在节点 ID 的损坏连线会直接进入 `load_composite_data` 与后续图求值（维度 7）。渲染出口有 preflight 兜底，加载出口没有。
  - 建议: 加载前对 graph_data 跑一次轻量结构校验（节点类型在注册表中、连线端点存在、无自环），失败时提示并拒绝/修复加载。
  - 置信度: 中（CompositePage 内部可能校验，本文件层确认无）

- [AUD-2409] [Suggestion] 图变更经直接 `replace()` 写入 EditProject，不经 command 入口，本层不可见 undo/redo 覆盖
  - 位置: src/app/controllers/composite_controller.py:341-372
  - 证据:
    ```python
    merged = dict(existing)
    for key in graph_keys:
        if key in graph_data:
            merged[key] = graph_data[key]
    ...
    self._current_edit_project = replace(
        self._current_edit_project,
        compositions=new_compositions,
        modified_at=time.time(),
    )
    self.edit_project_dirty = True
    ```
  - 影响: 节点增删/连线/参数变更通过 `_on_composite_graph_changed` 直接替换内存模型并置脏，控制器层无 command/事务对象（维度 5）。不可变式更新本身做得对（新 dict + dataclass replace + 变更检测），但若 undo 栈存在于 node_graph 内部，项目层快照与其一致性完全依赖 undo 时信号重放；本文件无法证实节点级操作可撤销。
  - 建议: 明确 undo 责任层并在控制器注释固化契约；或为图→项目同步引入 command 包装，使 `_flush_composite_before_leave` 这类“最后补救”路径（其存在本身说明信号顺序脆弱）变得不必要。
  - 置信度: 中

- [AUD-2410] [Suggestion] 属性面板接收节点 `parameters` 的可变引用，`set_node` 程序化更新在本层无状态锁
  - 位置: src/app/controllers/composite_controller.py:459-468
  - 证据:
    ```python
    self._node_properties_panel.set_node(
        node_id=node.id,
        node_type=node.node_type,
        node_title=node.node_title,
        parameters=node.parameters,
        ...
        enabled=not getattr(node, 'disabled', False),
    ```
  - 影响: 传入的是节点实时 `parameters` 字典而非副本。若面板在编辑时就地修改该 dict，则绕过序列化/命令路径直接改节点状态（维度 4/5 交叉）；同时选中→`set_node` 的程序化更新未见防递归锁，若面板控件在 set_node 期间发出 valueChanged 且被回连到图，将形成选中→设值→图变更→(重选中) 的环。是否安全取决于面板内部实现，本层无保障。
  - 建议: 传入 `dict(node.parameters)`（或深拷贝）；确认/在面板内加 programmatic-update 状态锁，并在本层注释指向该契约。
  - 置信度: 中

- [AUD-2411] [Suggestion] 8/16bit 量化用截断而非四舍五入，且未处理 NaN
  - 位置: src/app/controllers/composite_controller.py:1085-1089
  - 证据:
    ```python
    if export_img.dtype == np.float32:
        if ext in ("jpg", "jpeg", "png", "bmp", "webp"):
            export_img = (np.clip(export_img, 0.0, 1.0) * 255.0).astype(np.uint8)
        elif ext in ("tif", "tiff"):
            export_img = (np.clip(export_img, 0.0, 1.0) * 65535.0).astype(np.uint16)
    ```
  - 影响: `astype` 向零截断造成全图约 0.5 级系统性偏暗；`np.clip` 不清除 NaN，NaN 传入 `astype(np.uint*)` 结果未定义，HDR 图中一个坏像素即污染导出帧。
  - 建议: `np.rint(...)` 后 clip 到 [0,255]/[0,65535] 再 astype；转换前 `np.nan_to_num`。
  - 置信度: 高

- [AUD-2412] [Suggestion] Edit 页时每次图变更全量 `set_project` + `load_timeline` + 请求预览帧，无节流
  - 位置: src/app/controllers/composite_controller.py:401-437（触发链入口 372）
  - 证据:
    ```python
    self._current_edit_project = replace(...)
    self.edit_project_dirty = True
    self._refresh_edit_preview_after_composite_change()
    ...
    self.timeline_preview_renderer.set_project(self._current_edit_project)
    ...
    self.timeline_panel.set_project(self._current_edit_project)
    timeline_view = self.timeline_panel.get_timeline_view()
    if current_timeline is not None and timeline_view:
        timeline_view.load_timeline(current_timeline)
    ```
  - 影响: 每次 `graph_changed`（含每次参数微调）在 current_page == "Edit" 时都整时间线重载 + 重建预览（维度 8）。多数编辑发生在 Composite 页（该分支早退只置标志），但程序化图变更/撤销发生在 Edit 页时会放大为全量刷新；`load_timeline` 全量重建 UI 还可能再发选区信号，存在与合成页回切的潜在环（本文件内无直接证据，列为风险）。
  - 建议: 对刷新做合批/防抖（如 100ms QTimer），并在数据未变时跳过 `load_timeline`。
  - 置信度: 高（代码路径确定；高频触发条件为中等）

- [AUD-2413] [Suggestion] 控制器越层访问 Feature 页面私有成员（`_renderer`、`node_graph`、`node_editor`、`_current_composite_id`）
  - 位置: src/app/controllers/composite_controller.py:131-138、447、856-858、938
  - 证据:
    ```python
    if hasattr(self.composite_page, '_renderer') and self.composite_page._renderer:
        ...
        self.composite_page._renderer.submit(
            CompositeCommand.READBACK_PRESENT_ASYNC,
    ...
    node = self.composite_page.node_graph.get_node(node_id)
    ...
    if hasattr(self.composite_page, 'node_editor') and self.composite_page.node_editor:
        self.composite_page.node_editor.save_ui_state()
    return self.composite_page.node_graph.to_dict()
    ```
  - 影响: app 层控制器直接驱使 composite 页的渲染器提交命令、序列化其内部 node_graph，绕过页面公开接口（同文件 896 行明明有 `get_composite_data()` 公开方法）。接口重构时这些散点会静默失效（hasattr 掩盖）。
  - 建议: 在 CompositePage 暴露 `submit_async_readback(request_id)`、`get_node(id)`、`serialize_graph()` 等公开方法，控制器只调公开 API。
  - 置信度: 高

- [AUD-2414] [Suggestion] `replace()` 产生新 EditProject 后，`_timeline_clip_frame_provider` 持有的旧引用未刷新
  - 位置: src/app/controllers/composite_controller.py:366-372 对照 692-693、418-426
  - 证据:
    ```python
    self._current_edit_project = replace(
        self._current_edit_project,
        compositions=new_compositions,
        modified_at=time.time(),
    )
    ...
    if self._current_edit_project:
        provider.set_project(self._current_edit_project)   # 仅在进入页面时设置一次
    ```
  - 影响: 每次图同步都 `replace()` 出新项目对象；`_refresh_edit_preview_after_composite_change` 只刷新 preview_renderer 与 timeline_panel，帧提供者仍持有进入页面时的旧对象（维度 5 的“只改一层”变体：引用层不一致）。若 provider 后续需要读 compositions/comp_clips，将读到陈旧数据。
  - 建议: 在 `_sync_composite_graph_to_edit_project` 或刷新函数中同步 `provider.set_project(self._current_edit_project)`。
  - 置信度: 中（provider 是否读取 compositions 不在本文件）

- [AUD-2415] [Suggestion] 多个序列化/解析辅助方法缺少返回类型注解
  - 位置: src/app/controllers/composite_controller.py:851、861、1301
  - 证据:
    ```python
    def _get_composite_graph_data(self):
        """Get serialized composite graph data if available."""
    ...
    def _get_composite_state_data(self):
    ...
    def _resolve_output_resolution(self, graph_data: dict):
    ```
  - 影响: `.clinerules` 要求全量类型注解；这三个方法返回 `dict | None` / `tuple[int, int]` 却未标注，`composite_id`、`comp_clip` 等局部也有弱类型（`_resolve_current_comp_clip` 返回值未标注）。
  - 建议: 补 `-> dict | None`、`-> tuple[int, int]` 等返回注解。
  - 置信度: 高

### 通过项

- 线程安全（帧路径）：`_on_composite_frame_ready` 优先共享纹理（tex_id 整数），无纹理时提交异步 readback 到渲染线程，明确避免 UI 线程同步读回（115-143）
- GPU 资源生命周期：控制器全程只传递 tex_id 整数与命令字典，不持有/释放任何纹理、上下文等 GPU 对象（115-143、135-138）
- 跨线程信号：帧提供者回调经 `_video_node_frame_ready` Qt 信号队列回 UI 线程并 16ms 防抖（687-691），tex_id/meta 以值/新对象传递，未见可变对象跨线程共享写入
- 信号防递归（图变更链）：`_on_composite_graph_changed → 项目同步 → Edit 预览刷新` 在本文件内为单向，未见面内可证实的成环路径
- 状态提交一致性：`_sync_composite_graph_to_edit_project` 采用变更检测跳过 + 新 dict + dataclass `replace()` 的不可变式更新，且合并而非整体覆盖以保留 comp clip/格式元数据（304-370）
- 异常与取消：图像渲染有 try/except/finally 完整复位（1055-1116）；视频渲染有进度条点击取消过滤器（1193-1205）与 `EXPORT_CANCELLED` 独立分支（1242-1251）；`_cleanup_composite_render_exporter` 统一摘除过滤器/光标/tooltip
- 输入验证：`_prepare_composite_data_for_open` 对非 dict 数据、缺失 identity 字段均有回退且异常类型收窄（49-60）；渲染出口有 `run_preflight_data` 硬错误/占位节点确认双分支（961-1003）
- 渲染前快照：打开对话框前先快照 graph_data，用户继续编辑不影响本次渲染（894-896）
- `_switch_to_composite_page` 的 `_suppress_composite_auto_bind_once` 即用即清，注释明确处理了 early-return 场景，无残留标志（752-761）
- `_make_render_cancel_filter` 在 UI 线程事件过滤器中仅调用 `cancel_composite_render()`，无阻塞操作（16-27、1272-1276）
