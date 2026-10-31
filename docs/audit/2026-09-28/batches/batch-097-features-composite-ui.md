# 批次报告：batch-097 — src/features/composite/ui//

编号区间：AUD-9700–AUD-9799（本报告使用 AUD-9700–AUD-9716，共 17 条：Critical 0 / Major 6 / Suggestion 11）

## src/features/composite/ui/composite_viewport.py (行数: 11512)

### 发现

- [AUD-9700] [Major] PAINT 工具笔画无法绘制也无法提交——press 从未 start_drag，move/release 分发不可达
  - 位置: src/features/composite/ui/composite_viewport.py:8730
  - 证据:
    ```python
    # _handle_paint_press 尾部（8730-8734）：只记录首点，未调用 _interaction_state.start_drag(...)
    self._paint_drag_points = [[pos[0], pos[1]]]
    self._paint_drag_node_id = str(self.get_selected_node())
    return True

    # mouseMoveEvent（7686-7688）：仅 is_dragging 时才分发 _handle_tool_move → _handle_paint_move
    view_pos = QPointF(event.pos())
    if self._interaction_state.is_dragging:
    ```
    且 `_handle_tool_release`（7982，唯一调用 `_handle_paint_release`/`_handle_roi_release` 的分发器）在全文件中没有任何调用点；`paint_stroke_committed.emit`（8779）只存在于不可达的 `_handle_paint_release` 内（grep 全仓确认 page 只连接该信号、无其他发射方）。
  - 影响: C2-3 画笔功能整体失效：按下后拖动不追加笔迹点（`_handle_paint_move` 不可达），松开后 `paint_stroke_committed` 永不发射，页面 `_on_paint_stroke_committed` 永不触发，无任何笔划被写入节点或 undo。
  - 建议: `_handle_paint_press` 中调用 `self._interaction_state.start_drag(tool=ViewerToolMode.PAINT, ...)`（或为 PAINT/ROI 增加独立的 move/release 分发路径），并恢复 `_handle_tool_release` 在 `mouseReleaseEvent` 中的调用。
  - 置信度: 高（`ViewerInteractionState.is_dragging` 仅由 `start_drag` 置 True，已在 viewer_tool_system.py 核实；`_handle_tool_release` 无调用点已 grep 核实）

- [AUD-9701] [Major] ROI 工具交互式框选死路——drag 矩形永为 0 尺寸且提交路径不可达
  - 位置: src/features/composite/ui/composite_viewport.py:8664
  - 证据:
    ```python
    def _handle_roi_press(self, view_pos: QPointF) -> bool:
        """Start dragging a new user ROI rectangle."""
        img_pos = self.get_image_pos_at_view_pos(view_pos, check_bounds=False)
        if img_pos is None:
            return False
        self._user_roi_drag_rect = (img_pos.x(), img_pos.y(), 0.0, 0.0)
        return True
    ```
    `_handle_roi_move`（8672）仅由 `_handle_tool_move`（7977）调用、而后者被 `is_dragging` 门控；`_handle_roi_release`（8684）仅由不可达的 `_handle_tool_release`（8005）调用。
  - 影响: Shift+R 进入 ROI 工具后，拖拽矩形不会更新（`_user_roi_drag_rect` 停留在 w=h=0），松开永不提交 `_apply_user_roi`；B1 的"框选渲染区域"交互完全不可用（仅剩 Ctrl+Shift+R 清除与工程恢复路径）。
  - 建议: press 中调用 `_interaction_state.start_drag(...)` 使 move/release 走通；或在 mouseMove/mouseRelease 中为 ROI 增加基于 `_user_roi_drag_rect is not None` 的独立分支。
  - 置信度: 高

- [AUD-9702] [Major] 像素检查器 UV 映射未扣除工具栏/播放条占位区，读取的像素位置系统性偏移
  - 位置: src/features/composite/ui/composite_viewport.py:9598
  - 证据:
    ```python
    # _compute_uv_at_mouse（9581-9616）：用全视口居中推导图像矩形
    tex_aspect = tw / th
    vp_aspect = vp_w / vp_h
    if tex_aspect > vp_aspect:
        sy = vp_aspect / tex_aspect
        bx0, bx1 = -1.0, 1.0
        by0, by1 = -sy, sy

    # 而 _compute_vertices（11134-11137）按内容区（扣除顶部工具栏/底部条）排布：
    dpr = self.devicePixelRatio()
    left_px, top_px, right_px, bottom_px = self._content_rect_pixels(dpr)
    content_w = max(1.0, right_px - left_px)
    content_h = max(1.0, bottom_px - top_px)
    ```
  - 影响: 实际绘制四边形中心相对全视口中心下移约 (top+bottom_reserved)/2 像素，而 `_compute_uv_at_mouse`（仅被 `_update_inspector_from_framebuffer` 使用）假设图像在全视口居中：像素检查器读取/显示的像素相对光标纵向偏移（1x 缩放下约偏 14+ 像素，底部播放条可见时更大），QC 读数指向错误坐标；命中测试路径（`get_image_pos_at_view_pos`）用 `_compute_vertices` 与绘制一致，唯独检查器不一致。
  - 建议: 让 `_compute_uv_at_mouse` 复用 `_compute_vertices(vp_w, vp_h, tw, th)` 的结果做 NDC→UV 反算，删除重复的居中推导。
  - 置信度: 高（两函数同文件并存，差异仅为内容区扣减；工具栏 `CompositeViewerBar` 常驻可见）

- [AUD-9703] [Major] GL 上下文重建时仅重置 ODT LUT 句柄，其余 owned 纹理/FBO 保留陈旧 ID
  - 位置: src/features/composite/ui/composite_viewport.py:3038
  - 证据:
    ```python
    # A context rebuild invalidates every GL handle: without this
    # reset, _ensure_odt_lut would short-circuit on a stale key and
    # bind a dead texture id (black frames after context loss).
    self._odt_lut_tex_id = 0
    self._odt_lut_key = None
    self._odt_lut_failed_key = None
    ```
    而 `_owned_display_tex_id/_owned_aov_tex_ids/_owned_secondary*_tex_id/_owned_quad_tex_ids/_copy_read_fbo/_copy_draw_fbo/_current_tex_id` 均不在 initializeGL 重置；`_copy_shared_texture_to_owned_display`（1604-1617）对非 None 的陈旧 ID 直接复用且尺寸相同则跳过 `glTexImage2D`。
  - 影响: 浮动/停靠视口（`_toggle_dock_float` 改变父窗口）或窗口跨屏等触发 QOpenGLWidget 上下文重建后，owned 纹理绑定到无存储的自动新建纹理、blit 进 incomplete texture 报 GL 错误，`set_texture` 返回 False；表现为黑帧/降级到 CPU 读回路径，直到纹理尺寸变化才自愈。注释本身已确认"上下文重建使所有 GL 句柄失效"，却只修了 LUT 一处。
  - 建议: 在 `initializeGL` 开头把上述全部 owned 纹理 ID、FBO ID 与 `_current_tex_id` 清零（`_owned_*_tex_size` 一并清空），使下一帧走全新分配路径。
  - 置信度: 中（触发依赖上下文重建场景；代码证据确凿，具体症状严重度未实测）

- [AUD-9704] [Major] set_state 恢复持久化数值无类型校验，畸形工程数据将中断整个恢复流程
  - 位置: src/features/composite/ui/composite_viewport.py:11015
  - 证据:
    ```python
    self._zoom_level = state.get("zoom_level", 1.0)
    self._pan_x = state.get("pan_x", 0.0)
    self._pan_y = state.get("pan_y", 0.0)
    self.set_channel_mode(state.get("channel_mode", 0))
    self.set_exposure(state.get("exposure", 0.0))
    self.set_gamma(state.get("viewer_gamma", 1.0))
    ```
    `set_exposure` 内 `max(-10.0, min(10.0, round(stops, 1)))`（9102）对字符串 raise TypeError；调用方 `main_window_adapter.restore_viewer_state`（main_window_adapter.py:668 `self._runtime.viewer.set_state(state_data)`）无 try/except。
  - 影响: 工程文件（外部数据）被手改/损坏后（如 `"exposure": "0.5"`、`"zoom_level": null`），`set_state` 抛 TypeError 逐层上传中断 `restore_viewer_state`，违反"外部数据须验证"铁律；同类字段 zoom/pan/gamma/target_peak 均无防护（对照：`user_roi`、`split_difference_scale` 已有 try/clamp）。
  - 建议: 对数值字段统一 `float(state.get(...) or default)` + clamp/try 的恢复辅助函数，非法值回退默认并记 warning。
  - 置信度: 中（需畸形输入才触发；代码路径与调用方无防护已核实）

- [AUD-9705] [Major] A/B 快照在 makeCurrent 之前调用 glIsTexture，无当前上下文时 GL 调用先行失败
  - 位置: src/features/composite/ui/composite_viewport.py:3866
  - 证据:
    ```python
    if not source_tex_id or not source_size:
        return False
    width, height = source_size
    if not _gl.glIsTexture(source_tex_id):      # ← 尚未 makeCurrent
        return False

    made_current = False
    try:
        if self._gl_initialized:
            self.makeCurrent()
    ```
  - 影响: `store_ab_snapshot`（Ctrl+Shift+A/B 或工具栏菜单）与 `set_split_view(True)` 都在非 paint 阶段进入此函数；若此刻上下文非 current，PyOpenGL 的 `glIsTexture` 抛 GLError 被 except 捕获后整个快照静默失败（仅 warning 日志），分屏对比无法建立。对照同文件 `set_texture`（1382-1383）先 makeCurrent 再做 GL 查询的正确顺序。
  - 建议: 把 `glIsTexture` 校验移到 `makeCurrent()` 之后的 try 块内。
  - 置信度: 中（取决于 Qt/PyOpenGL 在事件处理器阶段是否保持上下文 current；调用顺序问题本身确定）

- [AUD-9706] [Suggestion] get_state/set_state 在同一类中定义两次，前一对被完全遮蔽
  - 位置: src/features/composite/ui/composite_viewport.py:2929
  - 证据:
    ```python
    2929:    def get_state(self) -> dict:
    2961:    def set_state(self, state: dict) -> None:
    ...
    10975:    def get_state(self) -> dict:
    11012:    def set_state(self, state: dict):
    ```
  - 影响: Python 后定义覆盖前定义：2929/2961 版本是死代码且与生效版本行为已有分叉（前者不 emit `user_roi_changed`、无 magnifier/resolution 恢复），后续维护者改错版本会"改了不生效"。
  - 建议: 删除 2929-3026 的前一对定义（或合并两者差异）。
  - 置信度: 高

- [AUD-9707] [Suggestion] _probe_context_sharing 引用从未挂到 self 的属性，一旦调用必抛 AttributeError
  - 位置: src/features/composite/ui/composite_viewport.py:1054
  - 证据:
    ```python
    # __init__（1053-1055）：声明的是局部变量
    # Context sharing probe state
    _sharing_probe_passed: bool | None = None  # None = not tested, True/False
    _sharing_probe_attempted: bool = False

    # 11394-11397：
    if self._sharing_probe_attempted:
        return self._sharing_probe_passed or False
    self._sharing_probe_attempted = True
    ```
  - 影响: `_probe_context_sharing` 首行即访问不存在的 `self._sharing_probe_attempted` → AttributeError（方法自身无外层 try）。目前全仓无调用点（死代码），但任何人接线即坏。
  - 建议: 删除该方法与 `__init__` 中的两个死局部变量；或改为 `self._sharing_probe_*` 实例属性。
  - 置信度: 高（grep 全仓确认无调用点）

- [AUD-9708] [Suggestion] cleanup() 无任何调用方，且 VBO/VAO/Program/Checker 纹理删除不带 makeCurrent 保护
  - 位置: src/features/composite/ui/composite_viewport.py:3762
  - 证据:
    ```python
    if self._quad_vbo:
        vbo = ctypes.c_uint(self._quad_vbo)
        _gl.glDeleteBuffers(1, ctypes.byref(vbo))
        self._quad_vbo = None
    if self._vao:
        vao = ctypes.c_uint(self._vao)
        _gl.glDeleteVertexArrays(1, ctypes.byref(vao))
    ```
    对照同函数内 `_delete_owned_display_texture()` 等均含 `makeCurrent()/doneCurrent()`；grep 全仓无 `viewer.cleanup()` 调用。
  - 影响: 显式 GPU 资源释放路径是死代码（仅靠上下文销毁兜底回收）；若未来被调用，这些裸删除在无 current 上下文时无效或作用到错误上下文。
  - 建议: 在页面对应 teardown 处接入 cleanup()，并为裸删除段统一加 `_gl_initialized` 检查与 makeCurrent/doneCurrent。
  - 置信度: 高（死代码事实）/ 中（风险后果）

- [AUD-9709] [Suggestion] set_state 校验 render_pass 时引用本类从未定义的 `_available_render_passes`
  - 位置: src/features/composite/ui/composite_viewport.py:11047
  - 证据:
    ```python
    selected_render_pass = str(state.get("render_pass", "beauty") or "beauty")
    if selected_render_pass in getattr(self, "_available_render_passes", {"beauty"}):
        self._selected_render_pass = selected_render_pass
    ```
    该属性仅存在于 `CompositeRenderDialog`（composite_render_dialog.py:136），CompositeViewer 从未赋值。
  - 影响: getattr 默认值 `{"beauty"}` 恒生效，持久化的非 beauty AOV 通道（depth/normal 等）重开工程后被静默丢弃，恢复不到保存时的查看通道。
  - 建议: 用 `self._toolbar._render_pass_actions` 的键集合或 `self._owned_aov_tex_ids`（并允许在 AOV 尚未到达时延后恢复）做校验。
  - 置信度: 高（属性不存在已 grep 核实）

- [AUD-9710] [Suggestion] 多处 `except Exception: pass` 吞掉领域错误，违反宽泛 except 禁令
  - 位置: src/features/composite/ui/composite_viewport.py:1428
  - 证据:
    ```python
    try:
        self.user_roi_changed.emit(self.get_user_roi_bbox())
    except Exception:
        pass
    ```
    同类：`_cancel_3d_interaction_state`（2775-2803 三段 `except Exception: pass`）、`_collect_node_icons`（6164-6165）、`_select_node_by_id`（6307-6308）、`_handle_icon_drag_move/release`（6389-6390、6420-6421）。
  - 影响: `get_user_roi_bbox` 的坐标换算/`BBox` 构造错误、页面槽内异常被静默吞掉，ROI 失效或图标拖动半途失败均无任何日志，违背 `.clinerules`"不得宽泛 except 掩盖领域错误"。
  - 建议: 至少 `logger.debug/exception` 记录后再降级；对可预期异常收窄类型（TypeError/ValueError/GL 错误）。
  - 置信度: 高

- [AUD-9711] [Suggestion] `_space_pressed` 恒返回 False，文档承诺的 Space+左键平移分支不可达
  - 位置: src/features/composite/ui/composite_viewport.py:11078
  - 证据:
    ```python
    def _space_pressed(self):
        """Check if Space key is currently held."""
        ...
        # This doesn't work for Space. Use a simpler approach:
        # We rely on the parent CompositePage to relay Space state if needed.
        return False
    ```
    mousePressEvent 7542-7546 仍保留 `if event.button() == Qt.MouseButton.LeftButton and self._space_pressed():` 分支。
  - 影响: docstring 与 mousePressEvent 注释宣称的"Space + Left: Pan"永远不触发（死分支）；用户按文档操作无效。
  - 建议: 用 keyPressEvent/keyReleaseEvent 维护 `_space_down` 布尔实现，或删除相关分支与文档避免误导。
  - 置信度: 高

- [AUD-9712] [Suggestion] `_should_show_safe_areas` 中 if/else 两分支完全相同（死条件）
  - 位置: src/features/composite/ui/composite_viewport.py:5071
  - 证据:
    ```python
    if active_cam_id:
        # 活动相机优先；其余也保留（任一启用即显示）
        cam_nodes = upstream_cams
    else:
        cam_nodes = upstream_cams
    ```
  - 影响: `active_cam_id` 的区分逻辑未实现（或丢失了优先排序），死分支造成阅读误导。
  - 建议: 实现注释宣称的"活动相机优先"排序，或折叠为单语句。
  - 置信度: 高

- [AUD-9713] [Suggestion] 工具栏曝光步进从 QLabel 文本反解析当前值，脆弱耦合
  - 位置: src/features/composite/ui/composite_viewport.py:732
  - 证据:
    ```python
    def _adjust_exposure(self, delta):
        """Adjust exposure by delta stops, emit the new absolute value."""
        try:
            current = float(self.exposure_label.text().replace("Exp: ", ""))
        except ValueError:
            current = 0.0
    ```
  - 影响: 标签格式（i18n、前缀改动、本地化数字）一旦变化即静默回退 0.0，曝光步进基准错误；viewer 才是 `_exposure` 的真源。
  - 建议: 从 `self._viewer._exposure`（或经 getter）读取真源值，标签仅做展示。
  - 置信度: 高

- [AUD-9714] [Suggestion] 3D 模式每帧重复扫描节点图并按象限重复合并全局配置
  - 位置: src/features/composite/ui/composite_viewport.py:5253
  - 证据:
    ```python
    for view_kind in QUAD_VIEW_ORDER:            # 每帧 ×4 象限
        ...
        _qr, _qg, _qb = self._viewport_bg_color_rgb()   # → _viewer3d_overlay_settings() 配置合并
    ```
    另 `_collect_node_icons`（6075 `for node in graph.nodes.values():`）在 `_draw_node_icons_overlay` 与 `_hit_test_node_icon` 中每帧/每次 hit 重建全部图标参数。
  - 影响: 四视图模式下每帧 4 次配置合并 + 每帧全图遍历（含 float 解析），与 4K/60fps 基准相悖；节点数增多时叠加开销线性放大。
  - 建议: 背景色在 paintGL 入口取一次；图标数据按 `node.param_revision`/图修订号缓存，拖动时增量失效。
  - 置信度: 高（重复调用事实）/ 中（实际开销量级未测）

- [AUD-9715] [Suggestion] CPU 回退上传路径把 HDR 线性数据钳制并量化为 8-bit，且像素检查器仍标注 "linear"
  - 位置: src/features/composite/ui/composite_viewport.py:1544
  - 证据:
    ```python
    # Renderer readback returns BGR float32 in 0..1.
    if np.issubdtype(rgb.dtype, np.floating):
        rgb = (np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8)
    else:
        rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    ```
  - 影响: 上下文共享失败的回退路径丢失 >1.0 的 HDR 信息（显示用可接受但无用户提示）；随后 `_read_linear_pixel_from_texture` 从这张 8-bit 纹理读数并标 `space="linear"`，检查器读数被钳制且分辨率退化，用户无从分辨。
  - 建议: 该路径改用 RGBA16F/HALF_FLOAT 上传（数据已是 float），或至少在检查器 space 标注中区分 "linear(8bit)"。
  - 置信度: 高

- [AUD-9716] [Suggestion] GL 未初始化时 `set_texture` 直接采用渲染器纹理 ID，违反自述的 owned-copy 不变式
  - 位置: src/features/composite/ui/composite_viewport.py:1381
  - 证据:
    ```python
    display_tex_id = tex_id
    actual_size = (int(width), int(height))
    if tex_id and tex_id > 0 and self._gl_initialized:
        self.makeCurrent()
        ...
    elif tex_id <= 0:
        self._delete_owned_display_texture()
    ```
    GL 未就绪分支落空后 1409-1411 仍把 `display_tex_id`（渲染器所有）写入 `_current_tex_id`。
  - 影响: `initializeGL` 完成到下一次 `set_texture` 之间，paintGL 可能采样渲染器 owned（可能已被环形回收的）纹理，短暂违背 `set_render_pass` docstring 声明的不变式；表现为偶发花屏一帧。
  - 建议: `_gl_initialized` 为 False 时保留尺寸/overlay 元数据但将 `_current_tex_id` 置 0（等 GL 就绪后的下一帧再上屏）。
  - 置信度: 中（窗口期窄、依赖页面每帧重发）

- [AUD-9717] [Suggestion] 大量公共/私有方法缺少返回类型注解，信号载荷用裸 object
  - 位置: src/features/composite/ui/composite_viewport.py:1709
  - 证据:
    ```python
    def fit_to_window(self):
        """Reset zoom and pan to fit the texture in the viewport."""
    # 同类：zoom_in/zoom_out(1735-1739)、showEvent/resizeEvent(3633/3396)、
    # mousePressEvent/mouseReleaseEvent(7471/7728)、event(7788)、
    # set_quad_textures(5361)、_draw_* 系列等；信号如 tool_mode_changed = pyqtSignal(object)
    ```
  - 影响: 369 个方法中相当比例无返回注解（含 Qt 覆写），`pyqtSignal(object)` 载荷类型仅靠注释，IDE/静态检查无法校验页面侧连接的载荷契约。
  - 建议: 按项目强类型约定补齐 `-> None`/具体类型；枚举载荷可用 `pyqtSignal(object)` + docstring 明示（PyQt6 限制内）。
  - 置信度: 高

### 通过项

- 线程安全：本文件全部 GL 调用（set_texture/set_aov_textures/set_quad_textures/pick_object_at/各 _delete_*）均由 UI 线程执行，makeCurrent/doneCurrent 在 try/finally 中配对（AUD-9705 为顺序问题而非线程问题）
- GPU 资源生命周期：渲染器共享纹理统一经 viewer-owned 纹理 blit 隔离；删除辅助函数带 `_gl_initialized` 守卫与异常吞噬；initializeGL 失败经 `_cleanup_partial_init` 释放半建资源（context 重建场景见 AUD-9703）
- GLSL/HDR 正确性：显示着色器浮点字面量均带 `.0`/科学计数、无循环；sRGB/Rec.709/PQ/HLG/ACES 常量与分段函数正确；BT.709→BT.2020/P3 矩阵按列主序书写且有转置说明；OCIO LUT 域内 clamp 有明确注释理由；差异对比两纹理走同一 `process_display` 管线
- 信号防递归：工具栏程序化同步均有 `isChecked()`/`isChecked() != enabled` 状态守卫（set_tool_mode/set_component_mode/update_magnifier/update_resolution_mode/update_ab_compare_slot 等）；`store_ab_snapshot→split_btn.setChecked→set_split_view` 有同态短路守卫；`set_resolution_mode/set_background_mode` 值未变即不发信号
- 状态提交一致性：3D 导航/gizmo/图标拖动统一经 scene3d_nav_started/preview/committed 走 page 合并 undo；`_gizmo_drag_before` 记录 undo 前快照；`set_state` 恢复后重发 user_roi_changed 触发 ROI 重评估；A/B 快照明示不持久化
- 异常与取消路径：Esc 取消拖动并清 ROI 橡皮筋残留（`_clear_drag_overlay_state` 消除吸附线残留有注释论证）；ODT LUT 烘焙失败记忆化 `_odt_lut_failed_key` 避免每帧重试并保底 builtin ODT；纹理尺寸不匹配/查询失败有明确 warning 与 False 返回供 page 降级
- 输入验证：`set_aov_textures` 对 descriptor 的 texture_id/width/height 做 int/范围校验；`set_state` 对 user_roi（try/float）、split_difference_scale（clamp 0..20）、magnifier_zoom（量化 4/8）、tool_mode（ValueError 回退）有防护（遗漏字段见 AUD-9704）
- 性能模式：F6-04 拖拽 LOD（100ms 延迟启用）+ 16ms 节流 + 最新位置合并（`_pending_drag_*`）实现正确；3D 导航 preview 16ms QTimer 节流；B2-1 auto-proxy 交互 600ms 空闲定时器闭环
- 工具几何一致性：hit-test 与绘制共用 `_tool_handles` 单一数据源；Text/Transform 的 bbox/输入尺寸、CornerPin to/from 对齐、旋转手柄跟随鼠标等均复用同源计算函数并在拖拽期间基于 original_params 防漂移
