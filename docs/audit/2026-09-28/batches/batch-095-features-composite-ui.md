# Batch 095 审查报告：src/features/composite/ui/composite_page.py

批次：batch-095 [P1] 主题 src/features/composite/ui//
编号区间：AUD-9500–AUD-9599
日期：2026-09-28

---

## src/features/composite/ui/composite_page.py (行数: 10,083)

### 发现

- [AUD-9500] [Critical] `_retry_renderer_init` 引用未定义的 `viewer_node_id`，重试路径必抛 NameError 崩溃
  - 位置: src/features/composite/ui/composite_page.py:1562
  - 证据:
    ```python
    def _retry_renderer_init(self):
        ...
        self.initialize_renderer()
        if self._renderer:
            import time as _time
            rid = self._request_id_gen.next_id()
            ...
            request = RenderRequest(
                snapshot=_snap,
                request_id=rid,
                timestamp=_time.monotonic(),
                runtime_context=self._runtime_context,
                timeline_clip_gpu_provider=self._timeline_clip_gpu_provider,
                edit_project=self._project,
                viewer_node_id=viewer_node_id,
    ```
    函数体内（1532–1588）从未给 `viewer_node_id` 赋值；唯一定义处在 `on_enter`（1417–1422）的局部作用域，与此处无关（已 grep 全文件确认：1562 是唯一无定义使用点）。
  - 影响: 页面在 GL context 未就绪时进入（on_enter 走 `self._renderer_retry_timer.start()`，1463），300ms 后定时器触发本函数；若此刻 context 已可用，`initialize_renderer()` 成功创建 renderer 后构建 request 即 NameError。异常从 QTimer 槽逃逸，PyQt6 默认行为是打印回溯后 qFatal 终止进程——首次进入 Composite 页的时序竞态可直接崩溃整个应用。
  - 建议: 改为 `viewer_node_id=self._resolve_viewer_node_id()`（与 1755 行 `update_color_output` 同路径），或仿 on_enter 1417–1422 的 getattr 兜底写法；同时补一条回归测试覆盖“renderer 为 None 时 on_enter → 定时器重试成功”路径。
  - 置信度: 高

- [AUD-9501] [Major] `_add_ai_generate_node` 使用未导入的 `AIGenerateNode`，一旦被调用即 NameError（当前为埋雷死代码）
  - 位置: src/features/composite/ui/composite_page.py:2003
  - 证据:
    ```python
    def _add_ai_generate_node(self):
        self._add_node_by_type(AIGenerateNode, (100, 400))
    ```
    文件头部 import（17–90 行）无 `AIGenerateNode`；全仓 grep 确认该名字只在 `node_engine.py`（`NODE_REGISTRY` 已注册 "ai_generate"）定义，本文件从未导入，也没有任何调用点。
  - 影响: 任何未来接线（菜单/快捷键/测试）触发该方法都会 NameError → qFatal 崩溃；当前不可达，属潜伏缺陷而非现存功能故障。
  - 建议: 要么删除该方法，要么改为 `from src.features.composite.core.node_engine import AIGenerateNode`（局部导入，与文件内其他惰性导入风格一致）并接入 UI。
  - 置信度: 高

- [AUD-9502] [Major] 参数防抖累加器跨参数/跨节点混包：80ms 内修改第二个参数会沿用第一个参数的 old_value，且首条修改丢失 undo 记录
  - 位置: src/features/composite/ui/composite_page.py:6760-6768（同型逻辑 6707–6712 trim 路径共用同一累加器）
  - 证据:
    ```python
    if self._pending_param_change is None:
        # First tick of a new drag — record the original value
        self._pending_param_change = (node_id, param_name, old_value, value)
        # Begin merge coalescing mode (for slider dragging)
        self._undo_stack.begin_merge_coalescing()
    else:
        # Subsequent ticks — keep original old, update new
        _, _, orig_old, _ = self._pending_param_change
        self._pending_param_change = (node_id, param_name, orig_old, value)
    ```
  - 影响: else 分支假设“后续 tick 属于同一 (node_id, param_name)”，但不校验。若 80ms 防抖窗口内发生不同参数（或不同节点）的第二次修改（联动控件、表达式回写、面板重建触发的二次 parameter_changed 等），新元组变为 `(nodeB, paramB, old_of_A, valueB)`：undo 会把 paramB 写成 paramA 的旧值（图状态损坏），且 paramA 的那次真实修改永远没有 undo 记录。`_on_parameters_changed_batch`（6904–6910）同型但限定单参数多节点，风险较低。
  - 建议: 累加前比较 `(node_id, param_name)`，不一致时先 flush 旧条目（或直接改为 keyed dict 累加，flush 时逐条 push）；`_flush_param_changes` 同步适配。
  - 置信度: 高（代码行为确定；触发需 80ms 内异参数编辑，概率中等）

- [AUD-9503] [Major] `_undo_add_node` / `_redo_remove_node` 删除节点未提交 CLEANUP_NODE，渲染线程侧 GPU 资源在 undo/redo 路径泄漏
  - 位置: src/features/composite/ui/composite_page.py:7152-7157, 7210-7215
  - 证据:
    ```python
    def _undo_add_node(self, data):
        node_id = data["node_id"]
        self._clear_deleted_node_viewer_state(node_id)
        self._node_graph.remove_node(node_id)
        self._node_editor.remove_node_visual(node_id)
        self._evaluate_graph()
    ```
    对比直接删除路径 `_on_node_removed`（2536–2538）与 `_on_nodes_batch_removed`（2695–2696）均有：
    ```python
    if self._renderer:
        self._renderer.submit(CompositeCommand.CLEANUP_NODE, {'node_id': node_id})
    ```
  - 影响: “加节点 → Ctrl+Z 撤销”或“删节点 → Ctrl+Shift+Z 重做删除”时，渲染线程按 node_id 持有的实例/GPU 资源不会收到清理指令；反复增删节点（交互式调图常见）在渲染线程累积泄漏，违背 deferred-release 模式的逐节点清理约定。除非渲染线程对消失节点有代际回收兜底（未见证据），这是明确的资源生命周期不一致。
  - 建议: 两个回调补 `self._renderer.submit(CompositeCommand.CLEANUP_NODE, ...)`（带 `if self._renderer` 守卫）；并审计其余绕过 `_on_node_removed` 的删除点（如 `_delete_nodes_with_owned_layers` 已按 before/after diff 补交，是对的）。
  - 置信度: 中

- [AUD-9504] [Major] `_properties_panel` 从不在 `__init__` 初始化，`on_enter`/`_on_project_settings_requested` 等直接属性读取，绑定前即 AttributeError
  - 位置: src/features/composite/ui/composite_page.py:1477, 2735, 2742（唯一赋值点 6212 属性 setter、7446 `set_properties_panel`）
  - 证据:
    ```python
    # on_enter (1477) — 直接读，无 hasattr 防御：
    if self._properties_panel is not None and self._node_graph is not None:
        QTimer.singleShot(0, lambda: self._show_default_project_settings())
    ```
    而同类访问大多有防御：属性 getter（6207–6208）用 `getattr(self, '_properties_panel', None)`；`_on_node_selected`（3321）用 `hasattr(self, '_properties_panel')`——代码自身承认该属性可能不存在。
  - 影响: 任何宿主（测试替身、插件式集成、MainWindow 绑定时序变化）在设置面板前调用 `on_enter()` / 按 S 键触发 `_on_project_settings_requested()`，AttributeError 从 Qt 槽逃逸 → qFatal 崩溃。防御风格不一致掩盖了这一前提。
  - 建议: `__init__` 中显式 `self._properties_panel = None`；直接读取点统一为 `getattr` 或依赖初始化保证。
  - 置信度: 中（崩溃前提是“未绑定即调用”，主窗口当前时序可能掩盖）

- [AUD-9505] [Major] `cleanup()` 不停止各单发/防抖定时器，cleanup 后 `_renderer_retry_timer` 触发会重建并启动新的渲染线程（或先撞上 AUD-9500）
  - 位置: src/features/composite/ui/composite_page.py:1872-1909（cleanup 主体）；重试链 1303-1304、1463、1582-1583
  - 证据:
    ```python
    def cleanup(self):
        """Stop playback, panel timers, renderer and release resources."""
        self.comp_pause()
        ...
        if self._renderer:
            stopped = self._renderer.stop_thread()
            if stopped:
                self._renderer = None
    ```
    cleanup 停了播放/tracker/AI/keyframes/renderer，但 `_renderer_retry_timer`、`_eval_debounce_timer`、`_prefetch_timer`、`_param_debounce_timer` 均未 stop。`_is_active` 在 cleanup 中也不清 False。
  - 影响: cleanup（页面拆除/会话重置）时若 `_renderer_retry_timer` 处于武装状态（on_enter 1463 或 `_ensure_renderer_initialized` 1304 启动），300ms 后触发 `_retry_renderer_init`：renderer 已置 None 且 `_is_active` 仍 True → `initialize_renderer()` 用缓存的 `_main_gl_context` 重新创建并 `start()` 一个孤儿 CompositeRenderer 线程，GL 资源泄漏；若走到 request 构建则先触发 AUD-9500 的 NameError。`_eval_debounce_timer` 后触发相对无害（renderer 为 None 时直接返回）。
  - 建议: cleanup 开头统一 `for t in (self._renderer_retry_timer, self._eval_debounce_timer, self._prefetch_timer, self._param_debounce_timer, self._batch_param_debounce_timer, self._minimap_timer, self._interaction_debounce_timer): t.stop()`，并置 `self._is_active = False`。
  - 置信度: 中

- [AUD-9506] [Major] `load_composite_data` 对外部持久化数据的 fps/duration 直接 float()/int() 转换，非数值即抛异常逃逸 Qt 槽
  - 位置: src/features/composite/ui/composite_page.py:2232-2233
  - 证据:
    ```python
    self._composite_duration_frames = int(composite_data.get("duration_frames", 0) or 0)
    self._composite_fps = float(composite_data.get("fps", 24.0) or 24.0)
    self._node_graph.fps = self._composite_fps
    ```
    `composite_data` 来自 .topos 工程文件反序列化（外部数据）。若 fps 为 `"24fps"` 之类字符串，`float("24fps")` 抛 ValueError，沿调用链（槽/加载流程）逃逸。
  - 影响: 损坏或手改过的工程文件在打开 Comp Clip 时抛未捕获 ValueError；若经信号槽调用则 qFatal 崩溃而非拒绝加载。违反“外部数据须验证”铁律。
  - 建议: 用 try/except (TypeError, ValueError) 包裹并回退默认值（同文件 1786–1791 `set_source_image_size` 已是正确范例）；或在上游 CompositeGraphData 加载处集中校验。
  - 置信度: 中（取决于上游加载器是否已先行校验；本函数自身无防护是确定的）

- [AUD-9507] [Suggestion] `QTimer.singleShot(0, lambda: ...)` 违反本文件自述的 T-P1-71 lambda-定时器禁令
  - 位置: src/features/composite/ui/composite_page.py:1478
  - 证据:
    ```python
    if self._properties_panel is not None and self._node_graph is not None:
        QTimer.singleShot(0, lambda: self._show_default_project_settings())
    ```
    同文件 350–355 行注释明确记录：“必须连接绑定方法，不能连 lambda——lambda 槽不带接收者上下文，页面 deleteLater 与 Python 包装生命周期错位时……段错误”。对比 1367 行用的是绑定方法 `self._node_editor.center_on_nodes`。
  - 影响: 0ms 窗口极小，但按本文件自己的事故记录，页面析构与回调错位时是 SIGSEGV 而非 Python 异常；模式不一致也会被后续代码复制到更长延迟场景（如 5827 行的 300ms singleShot 用的是绑定方法，是对的）。
  - 建议: 改为 `QTimer.singleShot(0, self._show_default_project_settings)`（方法已自带 `_is_active` 守卫）。同类 lambda 信号连接（622–624 minimap、627–629）挂在子控件上、随页销毁，风险低，可顺带统一。
  - 置信度: 高（模式违规确定；实际触发概率低）

- [AUD-9508] [Suggestion] `_build_render_request` 每次求值都对全图执行 `to_dict()` 序列化做变更检测，UI 线程热路径 O(图规模) 开销
  - 位置: src/features/composite/ui/composite_page.py:5484-5491
  - 证据:
    ```python
    graph_data = self._node_graph.to_dict()
    if (
        self._current_composite_id
        and graph_data != getattr(self, "_last_published_graph_data", None)
    ):
        self._refresh_asset_dependency_panel()
        self.composite_graph_changed.emit(self._current_composite_id, graph_data)
        self._last_published_graph_data = graph_data
    ```
  - 影响: 该函数被 16ms 防抖求值、3D 导航的 `_evaluate_graph_now()`（每次 mouse-move，8874）、每个解码完成帧（4318）等高频路径调用；每次都全图序列化 + 深比较。大图（数百节点/关键帧）交互拖拽时这是纯 UI 线程开销，与 4K 实时基准目标相悖。发布判定本身没必要走完整序列化——NodeGraph 已有 `_generation` 修订号可作脏信号。
  - 建议: 用 `self._node_graph._generation`（或专门暴露的 `revision`）跳过未变图的 to_dict；仅在确有变更时序列化发布。同时 `_refresh_asset_dependency_panel()`（含 `check_fs=True` 的文件系统扫描）也不应挂在每次求值路径上。
  - 置信度: 高（调用路径确定；量级影响取决于图规模，未实测）

- [AUD-9509] [Suggestion] 热路径逐帧 INFO 级调试日志刷屏
  - 位置: src/features/composite/ui/composite_page.py:1734, 5828-5829
  - 证据:
    ```python
    logger.info(f"[COMPOSITE-DEBUG] update_color_output: submitting eval, tex={texture_id}, rev={color_revision}")
    ...
    logger.info(f"[COMPOSITE-DEBUG] _on_frame_ready: req={request_id}, tex={tex_id}, "
                 f"size={width}x{height}, is_active={self._is_active}")
    ```
  - 影响: `_on_frame_ready` 在播放/交互期间每完成一帧执行一次 INFO 日志（含 f-string 格式化开销与 I/O），日志文件迅速膨胀并拖慢 UI 线程；命名自带 DEBUG 字样却用 INFO 级别。
  - 建议: 降为 `logger.debug`。
  - 置信度: 高

- [AUD-9510] [Suggestion] bypass/enabled 切换改变持久化状态但无 undo 记录、无编辑起始信号
  - 位置: src/features/composite/ui/composite_page.py:3543-3551, 6267-6277
  - 证据:
    ```python
    def _on_node_bypass_toggled(self, node_id: str):
        """Handle bypass toggle — update data model and re-evaluate."""
        if _page_node_is_locked(self, node_id):
            return
        item = self._node_editor._node_items.get(node_id)
        if item:
            self._node_graph.set_node_disabled(node_id, item._disabled)
            self._evaluate_graph()
    ```
    `_on_props_node_enabled_toggled` 同型：直接 `set_node_disabled` + 改视觉，无 UndoAction、无 `_emit_user_edit_once()`。
  - 影响: `disabled` 随图持久化（to_dict），但 Ctrl+Z 无法撤销一次误触的 bypass；该真实编辑也不触发 Comp Clip promotion（与加/连/参数修改等其他编辑路径不一致）。
  - 建议: 仿 `_on_node_rename_requested` 的 apply-then-push 模式补 `create_param_change_action`（param="disabled"）；并调用 `_emit_user_edit_once()`。
  - 置信度: 高（行为确定；是否属设计取舍存疑，故列 Suggestion）

- [AUD-9511] [Suggestion] 三类真实用户编辑缺 `_emit_user_edit_once()`，不触发 Comp Clip promotion
  - 位置: src/features/composite/ui/composite_page.py:9411-9464（paint stroke）, 6881-6920（batch param）, 9040-9068（active camera）
  - 证据:
    ```python
    def _on_paint_stroke_committed(self, node_id: str, geometry: dict) -> None:
        ...
        self._undo_stack.push(UndoAction(
            "paint_add_stroke",
            ...
        ))
        self._evaluate_graph_now()
    ```
    该方法推送了 undo（确认是用户编辑语义）但未调 `self._emit_user_edit_once()`；`_on_parameters_changed_batch` 与 `_on_active_camera_requested` 同样缺失（后者还持久化 `active_camera_id` 参数）。
  - 影响: 首个编辑若是画笔描边/多选批量调参/激活相机，`composite_user_edit_started` 不发射，EditController 的 Comp Clip 提升流程被跳过，后续保存/提升语义可能不一致。
  - 建议: 三处入口补 `self._emit_user_edit_once()`。
  - 置信度: 高

- [AUD-9512] [Suggestion] 多处宽泛 `except Exception: pass` 静默吞错，违反 .clinerules“不得宽泛 except 掩盖领域错误”
  - 位置: src/features/composite/ui/composite_page.py:2498-2515（_prepare_nodes_for_removal 三连）, 4577-4578, 5136-5137, 8182-8184, 1867-1870, 6524-6525
  - 证据:
    ```python
    try:
        if hasattr(self._viewer, "clear_selection"):
            self._viewer.clear_selection()
        elif hasattr(self._viewer, "set_selected_node"):
            self._viewer.set_selected_node(None)
        ...
    except Exception:
        pass
    ```
    以及 `_on_keyframes_panel_time_range_changed` 中：
    ```python
    try:
        ...
        video_param_edit = apply_video_range_edit(
            node, self._node_graph, layers_map, time_range)
    except Exception:
        video_param_edit = None
    ```
  - 影响: `apply_video_range_edit` 承担真实的 trim 语义计算（领域逻辑），其异常被无声吞掉后节点仍落入“merged 范围已写、视频源参数未联动”的半更新状态，undo 记录也不含 video 分量——正是铁律针对的“掩盖领域错误”场景。其余 pass 块同样无任何日志，故障不可诊断。
  - 建议: 至少降级为 `logger.debug(..., exc_info=True)`；对 `apply_video_range_edit` 应区分失败原因，失败时跳过整次 range 写入而非半提交。
  - 置信度: 高

- [AUD-9513] [Suggestion] 死代码与私有成员穿透
  - 位置: src/features/composite/ui/composite_page.py:2328（`_pending_disconnect_data` 只写不读）, 294/1660-1661（`_ai_pending` 只读不写）, 7063（`_pf_vmo` 计算后未用）, 9883（`_node_graph._nodes` 私有穿透）, 6327（读 `_properties_panel._pending_keyframe_before` 私有 stash）
  - 证据:
    ```python
    # 2328：写入后全文件无任何读取点（grep 确认仅 641 初始化 + 2328 赋值）
    self._pending_disconnect_data = (src_node_id, src_port, dst_node_id, dst_port)
    # 7063：_pf_vmo 赋值后从未使用
    _pf_vmo = self._apply_view_mode_overrides(_pf_snap)
    self._prefetch_scheduler.trigger_prefetch(prediction, _pf_snap)
    # 9883：绕过公开的 nodes 属性
    for node_id, node in self._node_graph._nodes.items():
    ```
  - 影响: 误导后续维护（以为断线分组/手动缩略图覆盖/AI pending 映射仍有作用）；穿透私有成员在 NodeGraph/面板内部结构演化时静默失效。
  - 建议: 删除死代码；`_nodes` 改用公开 `nodes`；`_pending_keyframe_before` 改为面板公开方法（如 `pop_pending_keyframe_before()`）。
  - 置信度: 高

- [AUD-9514] [Suggestion] 类型注解缺失（Suggestion 级维度）
  - 位置: src/features/composite/ui/composite_page.py:490（`_init_ui`）, 1351（`on_enter`）, 2291（`_on_nodes_connected`）, 6720（`_on_parameter_changed`）, 1590（`on_leave`）等
  - 证据:
    ```python
    def on_enter(self, color_texture_id=None, color_width=0, color_height=0,
                 color_revision=0):
        """Called when switching TO the Composite page."""
    ```
    大量公开槽/生命周期方法（`on_enter`、`on_leave`、`cleanup`、`_init_ui`、`_on_nodes_connected`、`_on_parameter_changed`、`set_parameter` 等）无参数/返回类型注解，与文件内新代码（如 `set_asset_search_roots(self, roots: tuple[str, ...] | list[str], *, persist: bool = True) -> None`）风格断层。
  - 影响: 违反项目“所有函数有类型注解”约束；`color_texture_id: Optional[int]` 等契约只能靠 docstring 传达，宿主（MainWindow/page_controller）集成易错。
  - 建议: 按现有新代码风格补注解，优先公开 API 与信号槽签名。
  - 置信度: 高

### 通过项

- 线程安全（UI 线程未直接调用 OpenGL；渲染经 renderer.submit 命令队列；帧/元数据/缩略图回调经信号自动队列回 UI 线程；`_FrameReadyTrampoline` 弱引用蹦床正确处理 VideoContext 单例悬空回调，T-P1-71）
- GPU 资源生命周期（直接删除路径均提交 CLEANUP_NODE；`_delete_nodes_with_owned_layers` 按 before/after 差集补交清理；`cleanup` 对 tracker/AI worker 先 cancel+wait 再 deleteLater；renderer stop 失败时保留引用防包装被回收——例外见 AUD-9503/9505）
- 信号防递归（`_syncing_composite_frame` 状态锁覆盖 set_composite_current_frame/_apply_composite_frame，try/finally 正确复位；防抖定时器全部连接绑定方法，符合 T-P1-71 注释要求——例外见 AUD-9507）
- 状态提交一致性主干（add/remove/connect/disconnect/paste/duplicate/rename/version/keyframe/expression/time_range/paint/tracker 等均有 apply-then-push 的对称 undo 记录；播放帧推进不触发 graph_changed 持久化重复发布；`_last_published_graph_data` 去重）——例外见 AUD-9502/9510/9511
- 异常与取消路径（tracker worker cancel/wait；播放 seek 时 `stop_playback_drive` 带 AttributeError/RuntimeError 捕获并降级日志；`stabilize_transforms` NaN 防护；media drop 失败整组回滚 undo）——例外见 AUD-9512
- 输入验证（`set_source_image_size` 的 int 容错、`_animation_clip_specs`/`_update_model_panels_from_diagnostics` 对渲染线程元数据的逐字段 isinstance 校验、asset relink/collect 的领域异常捕获、unknown 节点类型降级 UnknownPlaceholderNode）——例外见 AUD-9506
- GLSL·HDR 正确性（本文件无 GLSL 代码；缩略图 readback 使用与 viewer 一致的 linear→display 变换，未对线性数据无理由 clamp；浮点字面量带 .0）
- 性能模式（16ms 求值防抖、缩略图三级限速策略委托纯函数、导航 profile 降质 + 150ms 恢复 timer 去抖动、渲染线程自驱播放消除双往返回路）——例外见 AUD-9508/9509
