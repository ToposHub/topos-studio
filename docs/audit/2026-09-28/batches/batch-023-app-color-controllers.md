# Batch 023 — App 层 Color 控制器静态审查

- 审查日期: 2026-09-28
- 审查范围: `src/app/controllers/color_controller.py`、`src/app/controllers/color_workspace_controller.py`（只读静态审查）
- 审查维度: 线程安全 / GPU 资源生命周期 / GLSL-HDR 语义 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解
- 问题编号区间: AUD-2300 – AUD-2399

---

## src/app/controllers/color_controller.py (行数: 1841)

### 发现

- [AUD-2301] [Critical] brush 工具遍历根级遮罩时对 MaskGroup 访问 `.type` 抛 AttributeError，槽内未捕获致进程崩溃
  - 位置: src/app/controllers/color_controller.py:1174-1177（on_brush_toggled）、src/app/controllers/color_controller.py:1246-1249（on_brush_stroke_added）
  - 证据:
    ```python
    # on_brush_toggled, 1170-1177
    layer = self.engine.get_active_layer()
    if layer:
        brush_shape = None
        for s in layer.mask_shapes:
            if s.type == "brush":
                brush_shape = s
                break
    ```
    `on_brush_stroke_added`（1245-1249）含相同循环。而 MaskGroup 定义（src/features/color/core/layer.py:242-273）仅有类属性 `TYPE = "group"`，`__init__` 不设置实例 `type`，文件中也无 `__getattr__` 兜底（已 rg 验证）。对照组：同文件 `_iter_trackable_shapes`（278-281）与 `_tracker_display_name`（286-287）均用 `getattr(shape, "type", ...)` 防御式访问，说明作者已知部分遮罩对象无 `type`。
  - 影响: 用户先创建群组（on_mask_group_added 1029-1055 支持空群组；树拖拽 1057-1114 支持把任意形状收进群组）后开启画笔或落一笔，循环对 MaskGroup 求 `s.type` 即抛 AttributeError；PyQt6 槽内未捕获 Python 异常默认触发 qFatal 终止进程，属可复现崩溃。
  - 建议: 改用递归扁平化查找（复用 `_iter_trackable_shapes` 的 flatten 逻辑）+ `getattr(s, "type", None) == "brush"`；或统一改走 `layer` 的递归查找 API。
  - 置信度: 高

- [AUD-2302] [Major] 遮罩增/删/清空/层级重组均不写 undo 栈，"清空全部遮罩"将永久销毁所有图层的 AI matte 且无确认
  - 位置: src/app/controllers/color_controller.py:35-76（on_mask_shape_added）、78-92（on_mask_shape_removed）、183-225（on_all_masks_cleared）、1057-1114（on_mask_hierarchy_updated）、468-489（_on_tracker_pattern_committed）
  - 证据:
    ```python
    # on_all_masks_cleared, 194-204
    # Clear masks for ALL layers, not just the active one
    for layer in self.engine.layers:
        # Clear all mask shapes from the layer (including Bitmap AI masks from BiRefNet/SAM)
        layer.mask_shapes.clear()
        # Clear AI matte data (legacy BiRefNet/SAM results stored directly)
        layer.ai_matte = None
        layer.ai_matte_enabled = False
    ```
    全程无 `history.push_state`、无确认对话框。对照组：同文件 on_duplicate_mask_shape（118-120）与 on_copy_masks_to_layer_requested（165-167）均先 `self.engine.history.push_state(..., copy_image=False)`。
  - 影响: 误点 reset 按钮会一次性、不可撤销地删除所有图层的全部几何遮罩与 BiRefNet/SAM 计算的位图 matte（昂贵计算结果，等于数据丢失）；新增/删除/拖拽重组层级同样不可撤销，与复制操作相比 undo 覆盖严重不一致。`history.push_state` 已支持 `_serialize_layers` 深序列化（src/features/color/core/history.py:44-52），这些操作本可低成本入栈。
  - 建议: 在上述入口先 `push_state(..., copy_image=False)`；对 on_all_masks_cleared 追加 QMessageBox 确认。
  - 置信度: 高

- [AUD-2303] [Major] 三处视频降噪参数下发用 `except Exception: pass` 静默吞错，且越层直连 renderer 绕过 engine 统一入口
  - 位置: src/app/controllers/color_controller.py:1441-1445（on_video_denoise_changed）、1457-1461（on_video_denoise_history_reset）、1470-1475（on_video_denoise_debug_overlay_toggled）
  - 证据:
    ```python
    # on_video_denoise_changed, 1440-1445
    renderer = getattr(self.engine, 'renderer', None)
    if renderer is not None and hasattr(renderer, 'set_video_denoise_params'):
        try:
            renderer.set_video_denoise_params(params)
        except Exception:
            pass
    ```
    另两处（1458-1461、1472-1475）为同样的 `except Exception: pass`。而 image_engine 已提供带日志的统一入口 `set_video_denoise_state`（src/features/color/core/image_engine.py:855-864，失败时 `logger.debug(..., exc_info=True)`）。
  - 影响: 参数下发失败时 `engine.video_denoise_params`（记录态）与渲染器实际状态静默分歧——面板显示已开启降噪而画面未生效，且无任何日志可排查；直接违反 `.clinerules` "不得用宽泛 except 掩盖领域错误"。同时 app 层控制器越过 features 层 engine 直取 shared 层 renderer 对象，破坏分层。
  - 建议: 改调 `engine.set_video_denoise_state` 或在 engine 上补等价公共方法；至少将 `pass` 改为 `logger.warning(..., exc_info=True)`。
  - 置信度: 高

- [AUD-2304] [Major] denoise/deblur 完成回调与重置在 UI 线程同步执行全图拷贝、双次 resize 与 EXR 磁盘写
  - 位置: src/app/controllers/color_controller.py:1508-1527（on_denoise_finished）、1611-1626（on_deblur_finished）、1562-1568 / 1662-1667（两个 reset）
  - 证据:
    ```python
    # on_denoise_finished, 1514-1527
    low = FileIO._resize_to_fit(denoised_img, FileIO.LOW_RES_SIZE)
    high = FileIO._resize_to_fit(denoised_img, FileIO.HIGH_RES_SIZE)
    self.engine.set_image(denoised_img, high, low, is_new_file=False)
    ...
    bake_file_name = f"source_bake_{uuid.uuid4()}.exr"
    FileIO.save_baked_layer(bake_file_name, denoised_img.astype(np.float32))
    ```
    `FileIO.save_baked_layer` 为同步 EXR(DWAB 压缩) 写盘（src/shared/io/file_io.py:55-60）；`_resize_to_fit` 为同步 `cv2.resize`（file_io.py:825-828）。reset 路径（1563-1565）同样先 `img_source_full.copy()` 再两次 resize。
  - 影响: 4K float32 图像两次 `cv2.resize`、一次 `astype(np.float32)` 全量拷贝与 EXR 压缩写盘全部发生在 UI 线程的完成回调里，任务结束瞬间界面冻结可达秒级，属"UI 同步等待重计算/IO"反模式。
  - 建议: bake 写盘移入后台任务或渲染线程队列；`denoised_img` 已是 float32（1505 行注释自证），`astype(np.float32)` 拷贝可直接去掉。
  - 置信度: 高

- [AUD-2305] [Major] denoise/deblur 异步完成回调不校验任务上下文，结果可能落到切换后的文件/图层
  - 位置: src/app/controllers/color_controller.py:1504-1539（on_denoise_finished）、1608-1638（on_deblur_finished）
  - 证据:
    ```python
    def on_denoise_finished(self, denoised_img):
        import uuid
        self.engine.history.push_state(self.engine.img_full_original, self.engine.layers,
                                       self.engine.active_layer_index)
        ...
        self.engine.set_image(denoised_img, high, low, is_new_file=False)
    ```
    回调对 `self.engine` 的"当前状态"直接生效，无发起时的文件路径/图层索引/token 比对。对照组：同文件 tracker 流程用 `_tracker_task_matches`（880-882）以 task_key 匹配防止过期任务串扰。
  - 影响: 长耗时降噪/去模糊运行期间用户打开新文件或切换图层后回调触发：降噪结果会 `set_image` 到新上下文、`push_state` 污染新文件的 undo 栈、bake EXR 写入并挂到错误的工程（`source_bake_file_name`），且旧 bake 文件已被删除（1521-1522），难以恢复。
  - 建议: 发起时记录 (文件路径, 图层索引) 或任务 generation，回调先校验一致再应用，不一致则丢弃结果并释放资源。
  - 置信度: 中

- [AUD-2306] [Major] 激活 crop 时静默把工作图像替换为未 bake 的原图，退出（含取消）无恢复路径
  - 位置: src/app/controllers/color_controller.py:1697-1704（on_crop_toggled）
  - 证据:
    ```python
    # When activating crop tool, show the original image for re-adjusting crop box
    if self.engine and self.engine.img_source_full is not None:
        # Reload source image to show original for crop adjustment
        from src.shared.io.file_io import FileIO
        high = FileIO._resize_to_fit(self.engine.img_source_full, FileIO.HIGH_RES_SIZE)
        low = FileIO._resize_to_fit(self.engine.img_source_full, FileIO.LOW_RES_SIZE)
        self.engine.set_image(self.engine.img_source_full, high, low, is_new_file=False)
        self.update_canvas()
    ```
    关闭分支（1706-1707）仅 showMessage，无恢复逻辑；crop_panel 侧 `_set_crop_active_internal(False)` 自动 emit `crop_requested`（src/features/color/ui/crop_panel.py:429-445），仅当 `canvas.get_crop_data()` 非空才会走 on_crop_applied 重建管线。
  - 影响: 存在 denoise/deblur source bake 时进入 crop 会话，`img_full_original` 被替换为原始未处理图；若用户 Esc 取消且 crop_data 为空，视口停留在未 bake 图像，而 `engine.source_bake_file_name/type` 与 layer_panel 的 "denoise"/"deblur" 标记不变——显示态与持久化态分裂，保存工程再载入后又变回 bake 版。
  - 建议: 进入 crop 前保存当前 bake 上下文，退出（apply 与 cancel 两条路径）统一恢复；或 crop 预览改走独立预览通道，不覆盖 `img_full_original`。
  - 置信度: 中

- [AUD-2307] [Major] LUT 反编译同步执行且用 `QApplication.processEvents()` 刷 UI，异常路径状态栏消息永久残留
  - 位置: src/app/controllers/color_controller.py:1415-1428（on_decompile_lut_requested）
  - 证据:
    ```python
    self.status_bar.showMessage(f"Decompiling LUT: {os.path.basename(path)}...", 0)
    QApplication.processEvents()

    results = self.engine.decompile_lut(path)
    if results:
        ...
    else:
        self.status_bar.showMessage("LUT Decompile Failed.", 5000)
    ```
  - 影响: (1) `decompile_lut` 为耗时同步调用，期间 UI 冻结；(2) `processEvents` 允许重入——等待期间用户可触发关闭文件/切换页面，反编译随后继续写 engine 产生竞态；(3) 仅处理了"返回空结果"分支，若 `decompile_lut` 抛出异常则无任何捕获，"Decompiling LUT..." 消息（timeout 0，永不消失）残留且异常上抛到 Qt 事件循环。
  - 建议: 改为异步任务 + 完成/失败信号；至少 try/except 恢复状态栏并 `logger.exception`；移除 `processEvents`。
  - 置信度: 中

- [AUD-2308] [Suggestion] on_brush_params_changed 硬下标访问 params，缺键即 KeyError
  - 位置: src/app/controllers/color_controller.py:1205-1208
  - 证据:
    ```python
    if s.type == "brush" and s.id == self.canvas.active_shape_id:
        s.brush_size = params["size"]
        s.brush_feather = params["feather"]
        s.brush_opacity = params["opacity"]
        s.brush_flow = params["flow"]
    ```
    对照组：on_brush_stroke_added（1267-1271）对同样的信号参数全部使用 `params.get('size', 50)` 防御式访问。
  - 影响: 信号载荷若缺任一键即抛 KeyError；两种取值风格并存易在演进中埋雷。
  - 建议: 统一改为 `.get(key, default)`。
  - 置信度: 高

- [AUD-2309] [Suggestion] 控制器直接调用 FileIO / Layer / TrackerPanel 的私有成员
  - 位置: src/app/controllers/color_controller.py:1007（`layer._remove_item_recursive`）、1514-1515、1564-1565、1614-1615、1663-1664、1701-1702（`FileIO._resize_to_fit`）、846（`tracker_panel.graph.view._current_frame`）
  - 证据:
    ```python
    layer._remove_item_recursive(layer.mask_shapes, item.id)          # 1007
    low = FileIO._resize_to_fit(denoised_img, FileIO.LOW_RES_SIZE)    # 1514
    frame = int(getattr(self.tracker_panel.graph.view, "_current_frame", 0))  # 846
    ```
  - 影响: app 层穿透 features/shared 层私有实现，上游任何私有重构都会静默破坏控制器；`graph.view._current_frame` 更是三层深挖 UI 内部状态。
  - 建议: 在 FileIO / Layer / TrackerPanel 上提供公共 API（如 `FileIO.make_pyramid(img)`、`TrackerPanel.current_frame` 属性）。
  - 置信度: 高

- [AUD-2310] [Suggestion] denoise 与 deblur 的四组处理器近乎完全复制（约 200 行）
  - 位置: src/app/controllers/color_controller.py:1482-1545（on_denoise_requested/finished/error）vs 1586-1644（on_deblur_requested/finished/error）；1547-1580（on_denoise_reset_requested）vs 1646-1679（on_deblur_reset_requested）
  - 证据: 两套 finished/reset 函数体仅 `"denoise"` / `"deblur"` 字符串、面板引用与日志文案不同，其余逐行相同（如 1508-1539 与 1611-1638）。
  - 影响: 修一处漏一处（AUD-2304/2305 因此需要在两个位置分别修复）；文件无谓膨胀。
  - 建议: 抽取 `_apply_source_bake(kind, image)` / `_reset_source_bake(kind)` 公共辅助，请求侧同理。
  - 置信度: 高

- [AUD-2311] [Suggestion] 大量处理器缺类型注解；on_mask_shape_selected 与 on_panel_shape_selected 逻辑重复
  - 位置: src/app/controllers/color_controller.py:35（`def on_mask_shape_added(self, shape_type):`）、78、227、1434 等 40 余处；231-244 与 246-258 为两份几乎相同的选中同步代码
  - 证据:
    ```python
    def on_mask_shape_updated(self, shape_id, params):        # 227
    def on_mask_shape_selected(self, shape_id):               # 231
    def on_panel_shape_selected(self, shape_id):              # 246
    ```
  - 影响: 可维护性与 IDE 检查能力受损；两个选中处理器仅差一处 `mask_panel.select_shape` 调用，后续改面板同步逻辑极易漏改其一。
  - 建议: 补参数/返回类型注解；合并为公共 `_sync_mask_panel_from_shape(shape)`。
  - 置信度: 高

### 通过项
- 线程安全（维度 1）：全文无 OpenGL/ModernGL API 调用，未持有/释放任何 GPU 对象（维度 2 一并通过）；未发现 UI 线程直接操作渲染资源（renderer 直调仅限参数 setter，见 AUD-2303 的分层问题）。
- GLSL/HDR 语义（维度 3）：denoise/deblur 入参转换正确区分 uint8(/255)、uint16(/65535)，float 图像原样透传不 clamp（1497-1500、1601-1604）；EXR bake 保留 float32（1526、1625）。
- 信号防递归（维度 4）：`mask_panel.set_params` / `set_transform_params` 内部均有 blockSignals 防护（src/features/color/ui/mask_panel.py:646-653、831-833、912-914、965-967），故 process_mask_update（1374-1389）与 on_mask_shape_selected 的程序化回写不会成环；on_canvas_brush_params_updated（1217-1236）显式 blockSignals 同步 brush 控件；mask 参数更新走 pending dict + QTimer 防抖（227-229），无信号风暴。
- tracker 异步任务（维度 6 部分）：启动/重跟踪失败有 FAILED 状态恢复（776-781、826-830），取消回 EMPTY（930-933），任务用 task_key 匹配防串扰（880-882）；`_on_color_tracking_frame_changed` 的异常捕获带 exc_info 日志且不阻塞播放管线（456-460）。
- 输入验证（维度 7 部分）：`_region_from_geometry` / `_tracker_search_from_pattern` 对几何做 0-1 clamp（520-528、543-556）；`_ensure_tracker_binding` 校验 clip/media 缺失并抛领域错误（652-654）；`_tracker_params_to_engine_factory` 对 feature_count/min_confidence 做边界钳制（621-627）。
- denoise/deblur 完成回调恢复了按钮可用性与进度条（1531-1533、1630-1632），error 回调亦恢复（1541-1545、1640-1644）。

---

## src/app/controllers/color_workspace_controller.py (行数: 257)

### 发现

- [AUD-2312] [Suggestion] 三处宽泛 `except Exception` 吞错且无日志，注册表读取失败时 workspace 功能静默失效
  - 位置: src/app/controllers/color_workspace_controller.py:31-34（_color_workspace_panel_descriptors）、163-166（_refresh_color_workspace_menu）、189-192（_workspace_status）
  - 证据:
    ```python
    # _color_workspace_panel_descriptors, 31-34
    try:
        descriptors = get_panel_registry().get_all()
    except Exception:
        return []
    ```
    另两处：
    ```python
    try:
        canvas.refresh_color_workspace_menu()
    except Exception:
        pass                      # 163-166
    try:
        self.statusBar().showMessage(message, 4000)
    except Exception:
        pass                      # 189-192
    ```
  - 影响: 面板注册表读取一旦异常，`_color_workspace_docks` 返回空 → 应用/保存/高亮整个 Color workspace 功能静默失效，菜单为空且无日志可排查；违反 `.clinerules` 宽泛 except 禁令。因均有 fallback 行为且触发概率低，定为 Suggestion。
  - 建议: 至少 `logger.warning(..., exc_info=True)`；注册表失败可考虑向状态栏提示一次。
  - 置信度: 高

- [AUD-2313] [Suggestion] 公共方法缺返回类型注解
  - 位置: src/app/controllers/color_workspace_controller.py:65（get_color_workspace_presets）、152（color_workspace_menu_items）
  - 证据:
    ```python
    def get_color_workspace_presets(self):
        """Builtin + user workspace presets, in menu order."""
    ...
    def color_workspace_menu_items(self):
        """(preset_id, display_name, is_user) rows for the View > Workspace menu."""
    ```
  - 影响: 可维护性；返回结构只能靠 docstring 推断。
  - 建议: 补 `-> List[WorkspacePreset]` 与 `-> List[Tuple[str, str, bool]]`。
  - 置信度: 高

### 通过项
- 线程安全（维度 1）：纯 UI 线程的 dock 可见性/菜单操作，无 OpenGL、无跨线程信号、无 GPU 对象（维度 2 通过）。
- 状态提交一致性（维度 5）：不直接改序列化数据，保存/删除全部委托 `features.color.core.workspace_presets`（127-147），分层正确。
- 异常路径（维度 6 部分）：`apply_color_workspace` 捕获异常并 `logger.warning` + return False（121-123），调用侧给出状态栏反馈（194-206）。
- 输入验证（维度 7）：preset 应用前经 `sanitize_preset` 按注册表面板 id、feature 开关与 work_mode 过滤（108-109、43-47），越权面板 id 不会生效。
- 交互安全：删除前有列表选择 + QMessageBox 确认（230-252），保存失败（空名）有反馈（221-225）。
- 菜单接线集中且判空防御（setup_color_workspace_menu 171-183）。
