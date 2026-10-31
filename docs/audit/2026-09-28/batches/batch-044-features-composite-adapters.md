# 批次报告：batch-044 — src/features/composite/adapters//

- 审查日期：2026-09-28
- 编号区间：AUD-4400–AUD-4499
- 文件数：3（774 行）
- 统计：Critical 0 / Major 2 / Suggestion 8

---

## src/features/composite/adapters/__init__.py (行数: 5)

### 发现

（无）

### 通过项

- 通过（无逻辑）：纯 re-export 模块，导出 `CompositeRuntime`（来自 runtime）、`MainWindowAdapter`、`CaptionCompositeInputAdapter`，与 `__all__` 一致，无执行逻辑。

---

## src/features/composite/adapters/caption_input.py (行数: 35)

### 发现

- [AUD-4409] [Suggestion] as_node_inputs 对 word_timings/style 仅做浅拷贝，嵌套可变对象仍与源共享
  - 位置: src/features/composite/adapters/caption_input.py:29-31
  - 证据:
    ```python
    return {
        "caption_cue_id": value.caption_cue_id,
        "text": value.text,
        "start": value.start,
        "end": value.end,
        "word_timings": [dict(item) for item in value.word_timings],
        "language": value.language,
        "style": dict(value.style),
    }
    ```
  - 影响: 类文档声称提供 "read-only cue snapshot"，但 `dict(item)`/`dict(value.style)` 是一层浅拷贝；若 style 或 word_timing 项内含嵌套 list/dict（字段类型为 `dict[str, Any]`/`tuple[dict[str, Any], ...]`，类型上允许），图侧消费方修改嵌套值会写穿回 Edit 侧源数据，破坏"图不得写回源"的契约。
  - 建议: 使用 `copy.deepcopy`，或为 word_timings/style 的嵌套层级逐层拷贝；也可在类型上收窄为纯标量 dict 以固化契约。
  - 置信度: 高（浅拷贝事实确定；当前 resolve_style/word_timings 产出多为扁平结构，实际危害有限）

### 通过项

- 异常与取消路径：`build_composite_input` 对不存在的 cue 抛 `ValueError`（graphics.py:641-643），两个真实调用方（timeline_preview_renderer.py:3285-3289、timeline_export.py:10586-10590）均以 `try/except (ValueError, TypeError)` 捕获并降级为 warning，异常契约闭环。
- 输入验证：cue 不存在时显式报错而非返回半空对象；`caption_metadata` 在 SubtitleClip 上有 default_factory（timeline_v2.py:1355），无 None 访问风险。
- 只读契约：适配器不接受任何图侧文本/时间写入，与文档声明一致。
- 线程安全/GPU/信号/状态提交：纯同步只读转换，无涉及。
- 类型注解：方法签名均有参数与返回类型注解；`timeline: Any` 为避免跨 feature 循环导入的刻意选择（有文档说明），不计为缺失。

---

## src/features/composite/adapters/main_window_adapter.py (行数: 734)

### 发现

- [AUD-4400] [Major] restore_graph 对外部工程数据的反序列化全程无异常防护，损坏数据会中断整个工程打开流程
  - 位置: src/features/composite/adapters/main_window_adapter.py:650
  - 证据:
    ```python
    restored_graph = NodeGraph.from_dict(graph_data)
    self._runtime.page._node_graph = restored_graph
    if hasattr(self._runtime.page, "set_asset_project_dir"):
        self._runtime.page.set_asset_project_dir(project_dir)
    ...
    self._runtime.page._node_editor.build_from_graph(restored_graph)
    ```
    下游 `NodeGraph.from_dict`（node_engine.py:2170-2176）对连接数据直接下标访问：
    ```python
    for conn_data in data.get("connections", []):
        graph.connect(
            conn_data["src_node_id"],
    ```
  - 影响: `graph_data` 来自持久化的工程文件（外部数据，经 file_controller.py:795 调入，调用处同样无 try/except）。单条 connection 缺 key、迁移函数（migrate_graph）或某个 `node_cls.from_dict` 遇到畸形数据抛 KeyError/TypeError/ValueError 时，异常沿 restore_graph → file_controller 打开工程主流程裸传播，导致整个工程（含 Layer/RAW 等其余部分）无法打开，而非仅跳过 composite 区段。另注意 641-643 行：`nodes` 为空时静默 return，页面保留的是上一个工程的图（与 page.load_composite_data 的同名语义不同，那边有明确注释说明保留默认图），存在跨工程残留风险。
  - 建议: 在 restore_graph 内对 from_dict/build_from_graph 包裹 try/except（窄化为具体异常或至少 KeyError/TypeError/ValueError），失败时 logger.exception + 回退到默认图并继续打开流程；对空 nodes 显式重置页面图而非保留旧图。
  - 置信度: 高（传播路径已逐行核实；触发概率取决于工程文件损坏率，标中）

- [AUD-4401] [Major] Media Pool 海报发布在每帧异步回读路径上触发全图序列化 get_composite_data()
  - 位置: src/features/composite/adapters/main_window_adapter.py:586（入口 538）
  - 证据:
    ```python
    # _on_preview_image_ready（每个回读帧到达时）
    if isinstance(data, np.ndarray):
        self._publish_comp_clip_media_pool_preview(data, width, height)
    ...
    # _publish_comp_clip_media_pool_preview
    try:
        graph_data = page.get_composite_data()
    ```
    `get_composite_data`（composite_page.py:2263-2277）每次执行 `save_ui_state()` + `self._node_graph.to_dict()` 全图序列化。
  - 影响: 当纹理共享不可用（上下文共享降级）时，预览走 READBACK_PRESENT_ASYNC 路径，最高约 24fps；每帧在 UI 线程做一次 O(节点数) 的全图序列化 + UI 状态保存 + 全幅 `np.clip`/通道交换/QImage 转换。对含大量节点（尤其 3D 场景）的合成，UI 线程每帧额外毫秒级到几十毫秒开销，违背 4K 实时基准的性能模式要求，造成回放掉帧。
  - 建议: `comp_asset_id` 只在图变更（composite_graph_changed / user_edit）时刷新缓存，帧到达时复用缓存 id；或对海报发布做独立节流（如 ≥1s 一次）；poster 缩放可在下采样后转换而非全幅 clip。
  - 置信度: 高（调用链与 get_composite_data 实现已核实；仅影响回读降级路径这一前提已注明）

- [AUD-4402] [Suggestion] 海报 QImage 构造未校验 data 形状与传入 width/height 一致性
  - 位置: src/features/composite/adapters/main_window_adapter.py:590-598
  - 证据:
    ```python
    rgb8 = (np.clip(data[:, :, :3], 0.0, 1.0) * 255.0).astype(np.uint8)
    rgb8 = rgb8[:, :, [2, 1, 0]]  # renderer readback is BGR
    image = QImage(
        rgb8.data.tobytes(),
        int(width),
        int(height),
        int(width) * 3,
        QImage.Format.Format_RGB888,
    ).copy()
    ```
  - 影响: 步长按信号参数 `width` 计算，像素缓冲按 `data` 实际形状生成；二者一旦不一致（尺寸协商变更、上层传错），QImage 会按错误步长解释缓冲，轻则花屏海报，重则读越界。违反"外部数据须验证"铁律（data/width/height 跨线程来自渲染回读）。
  - 建议: 构造前校验 `data.ndim == 3 and data.shape[0] == height and data.shape[1] == width`（或以 shape 为准覆盖 width/height，同 viewer.set_image_data 的做法）。
  - 置信度: 中（当前生产者同源发送一致值；属防御缺失而非已触发缺陷）

- [AUD-4403] [Suggestion] _absolutize_paths 原地修改传入的工程数据字典，产生调用方不可见的副作用
  - 位置: src/features/composite/adapters/main_window_adapter.py:681-683
  - 证据:
    ```python
    if fp and not os.path.isabs(fp):
        abs_path = os.path.normpath(os.path.join(project_dir, fp))
        if os.path.exists(abs_path):
            params["file_path"] = abs_path
    return graph_data
    ```
  - 影响: `graph_data` 是 file_controller 传入的 `project_data['composite_graph']` 引用；原地改写使 project_data 与磁盘文件内容分叉。当前保存路径会重新序列化所以未爆雷，但任何未来复用 project_data 的逻辑（对比、二次写回、诊断）都会读到被改写的路径。方法名与返回值暗示纯函数，实际有突变。
  - 建议: 先 `copy.deepcopy(graph_data)` 再改（migrate_graph 已采用此约定），或至少在 docstring 标注 in-place 语义。
  - 置信度: 高（突变事实确定，当前危害低）

- [AUD-4404] [Suggestion] 多处宽泛 except Exception 且以 debug/warning 级日志吞掉，违反"不得宽泛 except 掩盖领域错误"
  - 位置: src/features/composite/adapters/main_window_adapter.py:300-303, 559-562, 600-601, 710-713
  - 证据:
    ```python
    try:
        self._mw.enter_page_hook("Composite")
    except Exception as e:
        logger.warning(f"Composite on_color_output_changed enter_page_hook failed: {e}")
    ```
    ```python
    try:
        panel.update_gpu_scope(frame)
    except Exception as exc:
        logger.debug("Composite GPU scope present failed: %s", exc)
    ```
    ```python
    except Exception:
        logger.debug("Failed to publish Comp Clip Media Pool preview", exc_info=True)
    ```
  - 影响: scope 上屏、海报发布、启动 enter 钩子失败均被降为 debug/warning 静默吞掉（默认日志级别下不可见），持续性功能失效（scope 黑屏、海报不更新）无法从日志定位，领域错误（类型/键错误）与瞬时错误不可区分。
  - 建议: 收窄异常类型；至少将持续性路径的失败提升到 warning/error 级并附堆栈（logger.exception），或对连续失败做一次性上报。
  - 置信度: 高

- [AUD-4405] [Suggestion] handle_frame_ready 在页面守卫之前无条件按帧打 INFO 日志
  - 位置: src/features/composite/adapters/main_window_adapter.py:389-394
  - 证据:
    ```python
    current_page = getattr(self._mw, 'current_page', '')
    logger.info(
        f"[COMP-PLAY] handle_frame_ready: req={request_id}, tex={tex_id}, "
        f"size={width}x{height}, page={current_page}"
    )
    if current_page == "Composite" and self._runtime.viewer:
        self._present_frame_to_viewer(request_id, tex_id, width, height)
    ```
  - 影响: 回放期间该处理器每帧执行：即便不在 Composite 页也先付出 f-string 构造 + INFO 级 I/O；60fps 下这是热路径上的固定开销，且刷屏日志掩盖其他 INFO 信息。
  - 建议: 先做页面/viewer 守卫再决定是否记日志；或降为 logger.debug 并用惰性 %s 参数。
  - 置信度: 高

- [AUD-4406] [Suggestion] 适配器深度访问对端私有成员，契约脆弱
  - 位置: src/features/composite/adapters/main_window_adapter.py:63-64, 96-100, 271-272, 650-663
  - 证据:
    ```python
    if composite_module and hasattr(composite_module, '_page'):
        existing_page = composite_module._page
    ...
    if self._runtime._node_editor:
        editor = self._runtime._node_editor
    ...
    self._runtime.page._node_graph = restored_graph
    ...
    if hasattr(self._runtime.page, '_undo_stack'):
        self._runtime.page._undo_stack.clear()
    ```
  - 影响: 模块 `_page`、runtime `_node_editor`、页面 `_is_active/_evaluate_graph/_renderer/_last_texture_id/_node_graph/_undo_stack` 等私有成员被外部直接读写；对端任何内部重构（改名、改由方法维护不变量，如 _node_graph 的 revision/dirty 同步）都会静默破坏适配器，且编译期无任何提示。restore_graph 直接赋值 `_node_graph` 也绕过了页面可能维护的脏标记/重渲染不变量。
  - 建议: 在 CompositeRuntime/CompositePage 上补公开访问器（restore_graph、node_editor、undo_stack 等），适配器仅走公开 API；至少为已约定的私有依赖补注释锚点。
  - 置信度: 高（事实确定；属可维护性风险而非现行 bug）

- [AUD-4407] [Suggestion] _wire_signals 的一次性 _connected 标志与 runtime 重建不匹配，重建后信号失联
  - 位置: src/features/composite/adapters/main_window_adapter.py:72-75, 109
  - 证据:
    ```python
    def _wire_signals(self) -> None:
        """Connect Composite page signals to MainWindow handler methods."""
        if not self._runtime.page or self._connected:
            return
    ...
        self._connected = True
    ```
  - 影响: `CompositeRuntime.create()` 每次都会重建 viewer/scope_panel 等面板（runtime.py:78-84），而 `_connected` 置位后永不复位；若未来 create() 被再次调用（feature 重载/重建场景，适配器文档明确以"可禁用/删除 Composite feature"为设计目标），新的 scope_panel.demand_changed 将永远接不上，scope 静默失效。当前仅 dock_layout_controller.py:846 单次调用，属潜伏缺陷。
  - 建议: `_connected` 改为按已连接对象记忆（如记录已连接的 page/scope_panel 引用，对象变化时重连），或在 runtime.create 重建面板时通知适配器复位标志。
  - 置信度: 低（失联机理确定，但现有调用路径不触发）

- [AUD-4408] [Suggestion] 类型注解缺失/过宽
  - 位置: src/features/composite/adapters/main_window_adapter.py:43, 132, 255-259, 545
  - 证据:
    ```python
    self._latest_gpu_scope_demand = None          # 无 Optional[ScopeDemand] 注解
    ...
    docks = {}                                    # dict 无值类型
    ...
    def _get_graph_data(self):                    # 无返回注解
        return self._runtime.get_graph_data()
    ...
    def _on_gpu_scope_ready(self, frame) -> None: # frame 无类型
    ```
  - 影响: SaveRouter 回调与 dock 结构的返回类型不可静态检查，跨模块消费方（file_controller、dock_layout_controller）失去类型保障。
  - 建议: 补 `-> Optional[dict]`、`dict[str, QDockWidget]`、`frame: ScopeGpuFrame`（TYPE_CHECKING 导入）等注解。
  - 置信度: 高

### 通过项

- 线程安全：所有信号处理器均在 UI 线程执行；重活通过 `renderer.submit(READBACK_PRESENT_ASYNC / RENDER_GPU_SCOPE)` 丢给渲染线程异步完成，UI 不阻塞；`_evaluate_graph` 为 16ms 防抖提交（composite_page.py:4286-4294）；on_leave_page 的 `engine.render_pipeline` 为异步提交型 API。
- GPU 资源生命周期：零拷贝路径 `viewer.set_texture` 先查询纹理尺寸校验共享有效性，失败优雅降级（composite_viewport.py:1381-1401）；`tex_id<=0` 时显式清空 viewer（413-419）；scope 帧经 `is_frame_valid_for_demand` 的 revision/source_revision 双重校验拒绝陈旧纹理（557，scope_types.py:153-171）；UI 线程的 GL 操作发生在 viewer 自有 QOpenGLWidget 上下文内且 makeCurrent/doneCurrent 配对。
- GLSL·HDR 正确性：本文件无 GLSL；`np.clip(0.0,1.0)` 仅用于 8-bit Media Pool 海报缩略图（显示目的明确），不构成对内部 HDR 数据的无理由 clamp；viewer 侧 float 通路不受影响。
- 信号防递归：`demand_changed` 处理只读 panel 状态（demand_state/gpu_demand_revision）不回写控件；`update_gpu_scope` 不 emit demand_changed（scope_panel.py:954-972 已核）；scope 提交有 15Hz 节流 + latest-wins；`_on_scope_visibility_changed` 仅复位节流时间戳。
- 状态提交一致性：restore 后清空 undo 栈（662-663）防止跨工程 undo；get_save_callbacks 提供 graph/viewer 序列化入口与 composite_id 回调；预览回读用单调 request_id 拒绝陈旧帧（535-536、542-543）；`_on_preview_image_ready` 的 media pool 发布在陈旧检查之后。
- 异常与取消路径：`_inject_source_image_size` 用窄异常集（AttributeError/IndexError/TypeError）保护 shape 解包（364-367）；composite_startup_fallback 的 `request_color_output` 有 except + logger.exception（731-734）；回读节流与 renderer 端合并防风暴。
- 输入验证：on_enter/on_leave/handle_frame_ready 有 is_available/page/current_page 守卫；`_is_scope_visible` 双重 isinstance + try 保护；`_inject_source_image_size` 校验 w>0/h>0 后才写入；`_maybe_submit_gpu_scope_demand` 校验 renderer/panel 存在；`demand_state()` 键集与消费字段一一对应（scope_panel.py:920-930 已核）。
- 架构边界：适配器未反向引入 Composite UI 类的模块级导入（仅 TYPE_CHECKING + 函数内惰性导入），符合 products→features 解耦意图。
