# batch-252 审查报告（src/features/color/ui/canvas.py）

- 审查日期：2026-09-28
- 编号区间：AUD-25200–AUD-25299
- 审查方式：全文通读（6,123 行）+ 只读 grep 交叉验证（viewport_toolbar_base.py、image_engine.py、color_controller.py、main_window.py 仅作旁证，未修改任何文件）

## src/features/color/ui/canvas.py (行数: 6123)

### 发现

- [AUD-25200] [Major] 曲线逐点羽化手柄缩放基准 `scene_width` 属性不存在，恒回退 512，手柄距离与引擎实际羽化像素不符
  - 位置: src/features/color/ui/canvas.py:1701（同 1914）
  - 证据:
    ```python
    # Feather scale: same as image_engine
    canvas_w = self.canvas.scene_width if hasattr(self.canvas, 'scene_width') else 512
    feather_scale = 4.0 * (canvas_w / 5.0) / 100.0
    ```
  - 影响: 全库 grep 确认 `Canvas` 从未定义/赋值 `scene_width`（仅 edit/timeline_view.py 有同名局部变量），`hasattr` 恒为 False，feather_scale 恒为 4.096 px/单位。引擎侧公式为 `4.0 * (target_w / 5.0) / 100.0`（image_engine.py:2247，target_w 为渲染目标宽度，如 1920 时为 15.36）。曲线上每点的 fi/fo 手柄视觉距离比实际蒙版羽化小约 3.75 倍（以 1080p/4K 为例），用户拖动手柄得到的羽化量与显示的把手位置严重不匹配。
  - 建议: Canvas 暴露真实场景宽度（如 `self.scene_width = float(self.logical_size.width())` 并在 logical_size 更新处同步），或直接读取 `self.canvas.logical_size.width()`；两处（1701、1914）统一。
  - 置信度: 高

- [AUD-25201] [Major] 裁切合法性检查使用被平移边界扩展过的 sceneRect，"限制在画内"约束失效
  - 位置: src/features/color/ui/canvas.py:921
  - 证据:
    ```python
    # Check if all 4 corners are inside the scene rect (image bounds)
    scene_bounds = self.canvas.scene.sceneRect()
    # Task 1 Fix: Use 2px tolerance buffer to prevent floating-point edge locking
    expanded_bounds = scene_bounds.adjusted(-2.0, -2.0, 2.0, 2.0)
    ```
    而 `set_image`（3822）每次都调用 `_update_scene_pan_bounds`（3958–3971）：
    ```python
    pad_x = max(img_w * 4.0, view_w_scene * 2.0, 2048.0)
    ...
    self.scene.setSceneRect(target)
    ```
  - 影响: `_update_scene_pan_bounds` 为允许平移把 sceneRect 扩展为图像四周各 4 倍图像尺寸（或 2048px）的大矩形。`_is_valid_crop` 文档声明"within the image bounds"，实际校验的是这个巨大的矩形，`CropRectItem.itemChange`（933–938）与 `on_handle_moved`（660）据此放行——`_constrain_to_image=True`（默认）时裁切框仍可被拖/拉出图像边界，产生类文档声明要防止的透明边缘；与 `_apply_crop_constraints`（按 img_w/img_h 真实边界钳制）互相矛盾。
  - 建议: `_is_valid_crop` 改用图像真实边界 `QRectF(0, 0, logical_size.width(), logical_size.height())`（可保留 2px 容差），不要用 scene.sceneRect()。
  - 置信度: 高

- [AUD-25202] [Major] RAW 取色把 "Linear" gamma 与 "Gamma 2.2" 同样施加 1/2.2 编码，与引擎 Linear=0（不做 OETF）语义冲突
  - 位置: src/features/color/ui/canvas.py:4972-4973
  - 证据:
    ```python
        elif gamma in ["Gamma 2.2", "Linear"]: 
            color = np.power(color, 1.0/2.2)
        elif gamma == "Gamma 2.4":
            color = np.power(color, 1.0/2.4)
    ```
    引擎侧映射（image_engine.py:452）：`"Linear": 0, "sRGB": 1, "Rec.709": 2, "Gamma 2.2": 3, ...`，Linear 传 0 即不做 gamma 编码。
  - 影响: 用户在 RAW 面板选 "Linear" 输出时，画布取色器（`_handle_color_pick` → `_apply_raw_color_management`）对像素额外做 2.2 编码，HSL 读数与画面显示不一致，基于取色加的控制点会偏亮；仅 "Gamma 2.4"/"Gamma 2.6" 分支缺失（raw_panel.py:107 有 "Gamma 2.6" 选项，canvas 侧无对应分支，会落入无编码）。
  - 建议: "Linear" 单独分支直通（不编码）；补齐 "Gamma 2.6" 分支 `pow(1/2.6)`，与 engine 的 gamma_map 对齐。
  - 置信度: 高

- [AUD-25203] [Major] 颜色检查器不做人眼坐标→原图坐标缩放映射、不应用 RAW 色彩管理，与两个取色器路径不一致
  - 位置: src/features/color/ui/canvas.py:4984-4991
  - 证据:
    ```python
    scene_pos = self.mapToScene(pos)
    orig_h, orig_w = self.engine.img_full_original.shape[:2]
    img_x = int(scene_pos.x())
    img_y = int(scene_pos.y())
    if 0 <= img_x < orig_w and 0 <= img_y < orig_h:
        pixel = self.engine.img_full_original[img_y, img_x]
    ```
    对比 `_handle_color_pick`（5048–5052）：`scale_x = orig_w / float(disp_w) ...; img_x = int(scene_pos.x() * scale_x)`，且 5076 行调用 `self._apply_raw_color_management(c)`。
  - 影响: 当 `_display_scene_size_override` 生效（时间线预览/监看覆盖，logical_size ≠ 原图尺寸）时，检查器读错像素或直接越界显示 (0,0,0)；RAW 文件下检查器显示未处理原始值，而取色器显示处理后的值，两处读数互相矛盾（"所见即所得"承诺只对取色器成立）。
  - 建议: 检查器复用 `_handle_color_pick` 的缩放映射与 `_apply_raw_color_management`，最好抽公共的 `scene_pos → rgb(处理链一致)` 工具函数供三处调用。
  - 置信度: 高（不一致为代码事实；越界触发依赖 override 场景）

- [AUD-25204] [Major] `set_crop_params` 使用裸 `except:` 吞掉一切异常并将宽高比静默置为 Free
  - 位置: src/features/color/ui/canvas.py:5768-5775
  - 证据:
    ```python
            try:
                parts = aspect_str.split(':')
                if len(parts) == 2:
                    self.crop_item.aspect_ratio = float(parts[0]) / float(parts[1])
                else:
                    self.crop_item.aspect_ratio = float(aspect_str)
            except:
                self.crop_item.aspect_ratio = None
    ```
  - 影响: 违反 .clinerules「不得宽泛 except 掩盖领域错误」。裸 except 连 KeyboardInterrupt/SystemExit/面板传来的任意类型错误一并吞掉，宽高比被静默重置为 None（Free），用户选择的 16:9 等比例可能莫名失效且无任何日志。
  - 建议: 收窄为 `except (ValueError, AttributeError, IndexError) as e:` 并 `logger.warning` 记录原始 aspect_str；除数为零（`float(parts[1])`==0）也应显式处理。
  - 置信度: 高

- [AUD-25205] [Major] `set_tool_mode('eraser')` 只改光标不改 `_brush_params["is_eraser"]`，橡皮擦工具实际画出加色笔触
  - 位置: src/features/color/ui/canvas.py:4224-4227（对照 5893-5896、5925）
  - 证据:
    ```python
        elif mode == 'eraser':
            self.brush_mode_active = True
            self.brush_cursor.set_eraser_mode(True)
            self.brush_cursor.setVisible(True)
    ```
    而落笔时（5925）`is_eraser = bool(is_shift or self._brush_params.get("is_eraser", False))`，正确路径在 `set_brush_eraser_mode`（5893-5896）里才会写 `_brush_params["is_eraser"]`。
  - 影响: 通过 `set_tool_mode('eraser')` 进入橡皮擦时，光标显示为擦除模式，但 `_handle_brush_press` 读取的 `is_eraser=False`，整笔被记为 additive；`_finish_brush_stroke` 的 `final_params["is_eraser"]`（5991）同样错误——蒙版方向反了（数据级错误）。当前外部调用仅见 'none'/'inspector'（menu_toolbar_controller.py:857-859、layer_controller.py:1828），属公共 API 上的潜在缺陷。
  - 建议: 'eraser' 分支调用 `self._brush_params["is_eraser"] = True`（并考虑 'brush' 分支显式复位 False），或直接复用 `set_brush_mode(True)+set_brush_eraser_mode(True)`。
  - 置信度: 中

- [AUD-25206] [Major] 笔刷覆盖层缓存被每次 `update_from_data` 全量作废并在 paint() 内整幅重建，增量渲染分支为死码
  - 位置: src/features/color/ui/canvas.py:1560-1567（作废）、1383-1422（重建）、1389（死增量）
  - 证据:
    ```python
        if brush_strokes is not None:
            self.brush_strokes = brush_strokes
            # 【关键】收到新笔刷数据时，强制清空局部包围盒缓存
            self._brush_bbox = None
            self._brush_cache_image = None
            self._brush_cache_pixmap = None
            self._rendered_strokes_count = 0
    ```
    paint 内 `strokes_to_draw = self.brush_strokes[self._rendered_strokes_count:] if self._rendered_strokes_count > 0 else self.brush_strokes`（1389）。
  - 影响: `update_mask_overlay`（5308-5341）每次刷新都传入 `brush_strokes=getattr(shape, 'brush_strokes', None)`，对笔刷形状即每次全量清缓存 → 下一次 paint 在 UI 线程同步重 rasterize 全部历史笔触（O(总笔触)）。跟踪/参数变化导致的频繁 overlay 刷新会使每帧都全量重建，越画越卡；1389 行的增量绘制逻辑因 count 恒被清 0 永远走全量分支，属死代码。
  - 建议: 仅当笔触列表对象引用或长度减少/首尾变化时清缓存；新增笔触走 1389 的增量分支追加绘制；至少在 `len(brush_strokes) >= self._rendered_strokes_count` 且引用相同/前缀一致时保留缓存。
  - 置信度: 中高

- [AUD-25207] [Major] 笔刷缓存 QImage 在 paint() 内分配且上限 16384×16384（ARGB32 约 1GB），极端场景可致 UI 卡死/内存耗尽
  - 位置: src/features/color/ui/canvas.py:1371-1385
  - 证据:
    ```python
                    # 防止极端放大情况下 QImage 超出 Qt 安全内存限制
                    if local_w > 16384 or local_h > 16384:
                        local_w = min(local_w, 16384)
                        local_h = min(local_h, 16384)
                    ...
                        self._brush_cache_image = QImage(local_w, local_h, QImage.Format.Format_ARGB32_Premultiplied)
    ```
  - 影响: 16384×16384×4B ≈ 1.07GB 的分配发生在 paint 事件内（UI 线程同步）；画布扩展模式下笔触可画到图外、bbox 加上 max_brush_size+20 的安全余量后，4K 大图也轻易达到数千像素边长（数百 MB）。分配失败时 QImage 为 null，QPainter 无法 begin，笔刷预览静默消失。
  - 建议: 用图像实际尺寸（而非 16384 上限）做硬上限并设更小的像素预算（如 ≤ 64MP）；分配后检查 `isNull()` 回退到直接 path 绘制；将重建移出 paint（数据更新时机）。
  - 置信度: 中

- [AUD-25208] [Major] `sync_menu_state` 程序化 setChecked 无状态锁，且把「覆盖层可见性」与「引擎 matte 预览」两个概念混接
  - 位置: src/features/color/ui/canvas.py:2367-2385（另 2340-2350）
  - 证据:
    ```python
        # Sync mask state
        if hasattr(canvas, '_show_mask_overlays'):
            self.mask_btn.setChecked(canvas._show_mask_overlays)
            if hasattr(self, 'mask_act'):
                self.mask_act.setChecked(canvas._show_mask_overlays)
    ```
    base 类（viewport_toolbar_base.py:146）`self.mask_btn.toggled.connect(self._on_mask_toggled)` → 发 `mask_toggled` → Canvas `_on_mask_preview_toggled`（4154-4162）→ `self.engine.set_mask_preview_mode(checked)`。`set_image(is_new_source=True)` 在 3820-3821 调用 `sync_menu_state()`。
  - 影响: `_show_mask_overlays` 是画布 overlay 可见性，engine mask preview 是 matte 预览模式，两者被同一个 checkbox 绑定：加载新图时若两者状态不一致，程序化 setChecked 触发 toggled → 把引擎 matte 预览强制改成 overlay 可见性的值（例如用户经 qualifier 面板开着的 matte 预览被悄悄关闭）。同时该程序化更新未用 blockSignals/状态锁，违反 .clinerules 防递归信号规范（当前恰好因数值多数时候相同而无环）。
  - 建议: `sync_menu_state` 中 setChecked 前后 blockSignals；并拆分 overlay 可见性与 matte 预览两个状态源，checkbox 只反映 matte 预览。
  - 置信度: 中

- [AUD-25209] [Suggestion] 一批未使用的导入残留
  - 位置: src/features/color/ui/canvas.py:2-4, 6-7, 19
  - 证据:
    ```python
    from PyQt6.QtWidgets import (QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
                                 QGraphicsRectItem, QGraphicsPathItem, QWidget,
                                 QHBoxLayout, QVBoxLayout, QComboBox, ...
                                 QGraphicsItemGroup, QApplication, QMenu, QMenuBar, ...)
    from src.shared.assets.icon_manager import (
        icon_mask, icon_crop, get_icon, IconColors, ...)
    from src.shared.gpu.gl_backend import GLContextManager
    ```
  - 影响: grep 全文件确认 `QGraphicsPathItem`、`QComboBox`、`QMenuBar`、`QGraphicsItemGroup`、`icon_mask`、`icon_crop`、`get_icon`、`GLContextManager` 均无使用（icon_mask/get_icon 仅在基类 viewport_toolbar_base.py 中使用，这里属于多余再导入）。增加阅读负担与误导（让人以为本文件直接操作 GL 上下文）。
  - 建议: 删除未使用项；可配 ruff/pyflakes F401 检查。
  - 置信度: 高

- [AUD-25210] [Suggestion] 死代码：从未发射的 `stroke_finished` 信号、未用的 `is_panning` 属性、残缺的 `_update_curve_preview`
  - 位置: src/features/color/ui/canvas.py:3521, 3605, 5490-5505
  - 证据:
    ```python
        stroke_finished = pyqtSignal(list, tuple, int)   # 3521，全文件无 emit
        self.is_panning = False                           # 3605，平移逻辑全部使用 _is_panning
        def _update_curve_preview(self):
            if not getattr(self, '_curve_mode_active', False):   # 从未赋值
                if hasattr(self, 'curve_preview_path') and self.curve_preview_path:  # 从未创建
    ```
  - 影响: grep 确认 `_curve_mode_active`/`curve_preview_path`/`_curve_preview_nodes` 在本文件（及全库 color 域）从未赋值，函数只有"隐藏"分支没有"更新"分支，调用即无操作；`stroke_finished` 被 `brush_stroke_added` 取代后遗留。两个平移状态名（`is_panning`/`_is_panning`）易致后续维护误用。
  - 建议: 删除 `stroke_finished`、`is_panning`、`_update_curve_preview` 及相关 hasattr 防御块。
  - 置信度: 高

- [AUD-25211] [Suggestion] `MaskShapeItem.rotation` 实例属性遮蔽 QGraphicsItem.rotation() 方法
  - 位置: src/features/color/ui/canvas.py:968, 139, 1551-1553, 2065
  - 证据:
    ```python
        self.rotation = 0.0                 # __init__，遮蔽 Qt 的 rotation() 方法
        ...
        parent.rotation = angle             # MaskHandle.mouseMoveEvent
        parent.setRotation(angle)
        ...
        "rotation": self.rotation,          # _sync_back
    ```
  - 影响: 本文件内一致地当 float 用，但任何外部/未来代码按 Qt API 调 `item.rotation()` 会得到 "float is not callable" TypeError；与 `setRotation()` 并存的同名异构状态（还有 itemChange 中 `parent.rotation = angle` 直接绕过 update_from_data）增加出错面。
  - 建议: 重命名为 `rotation_deg`（或仅依赖 Qt 的 `rotation()`/`setRotation()`），保持单一事实来源。
  - 置信度: 中

- [AUD-25212] [Suggestion] workspace 菜单重建时裸吞 provider 异常且无日志
  - 位置: src/features/color/ui/canvas.py:2296-2300
  - 证据:
    ```python
        if provider is not None:
            try:
                rows, active_id = provider()
            except Exception:
                rows, active_id = [], None
    ```
  - 影响: provider（读工作区配置）抛错时菜单静默变空，用户与开发者都无从得知（同文件其他兜底如 `_monitor_synced_viewport_size` 均有 `logger.debug(..., exc_info=True)`）。
  - 建议: 补 `logger.debug("workspace provider failed", exc_info=True)`。
  - 置信度: 高

- [AUD-25213] [Suggestion] `set_brush_params` 对外部驱动的更新也无条件回发 `brush_params_updated`（信号回声），自身无状态锁
  - 位置: src/features/color/ui/canvas.py:5898-5909
  - 证据:
    ```python
    def set_brush_params(self, params: dict):
        self._brush_params.update(params)
        self.brush_cursor.set_params(...)
        if "show_overlay" in params:
            self.brush_stroke_preview.set_show_overlay(params["show_overlay"])
            self.scene.update()
        self.brush_params_updated.emit(self._brush_params)
    ```
  - 影响: 面板→canvas.set_brush_params→回发→main_window/color_controller.on_canvas_brush_params_updated（color_controller.py:1217-1230 目前用 blockSignals 护栏写回面板）——环路是否成立完全取决于下游是否记得 blockSignals，属脆弱模式；`_brush_params.update(params)` 对外部 params 也未做键过滤/类型校验。
  - 建议: 增加来源标志或仅在本文件内发起变化（快捷键）时回发；对外部 params 做键白名单与数值 clamp。
  - 置信度: 高（模式事实；当前下游有护栏，暂无实际死循环）

- [AUD-25214] [Suggestion] `_set_node_type` 未做 handle.index 越界检查（同类方法均有）
  - 位置: src/features/color/ui/canvas.py:4702-4715
  - 证据:
    ```python
    def _set_node_type(self, handle, node_type):
        parent = handle.parentItem()
        if parent and hasattr(parent, 'bezier_points'):
            parent.bezier_points[handle.index]['type'] = node_type
    ```
  - 影响: 右键菜单 `menu.exec` 为模态阻塞，期间若引擎异步刷新/撤销导致 `bezier_points` 缩短，点菜单项时 IndexError 崩溃（对比 `on_handle_moved` 1786、`on_node_double_clicked` 1966 均有边界检查）。
  - 建议: 与其他方法一致加 `if 0 <= handle.index < len(parent.bezier_points)` 守卫。
  - 置信度: 中

- [AUD-25215] [Suggestion] 快捷键处理不检查修饰键：Ctrl+C 会切换裁切模式
  - 位置: src/features/color/ui/canvas.py:4372-4381
  - 证据:
    ```python
        # Handle C key for toggling crop tool
        if event.key() == Qt.Key.Key_C:
            # Toggle crop mode
            if self.crop_item:
    ```
  - 影响: `event.key()` 对 Ctrl+C / Cmd+C 同样返回 Key_C，且此处 return 吞掉事件——画布聚焦时系统复制类快捷键被劫持为打开/关闭裁切；空格分支（4332）同样不区分修饰键。
  - 建议: 加 `if event.modifiers() == Qt.KeyboardModifier.NoModifier`（或排除 Ctrl/Meta/Alt）再触发工具快捷键。
  - 置信度: 中

- [AUD-25216] [Suggestion] `reset_crop_to_image` 用全新 dict 整体替换 engine.crop_params，可能丢弃引擎侧其他键
  - 位置: src/features/color/ui/canvas.py:5709-5714
  - 证据:
    ```python
        if self.engine:
            self.engine.crop_params = {
                "rect": (0.0, 0.0, 1.0, 1.0),
                "rotation": 0.0,
            }
    ```
  - 影响: 与本文件其他位置按键更新（`self.engine.crop_params['rotation'] = ...`，887/5790/5736）不一致；若引擎在 crop_params 中维护其他键（如 aspect/constrain 状态），重置时被静默清空。
  - 建议: 改为按键更新 `self.engine.crop_params.update({"rect": ..., "rotation": 0.0})`（并保留既有键）。
  - 置信度: 中

- [AUD-25217] [Suggestion] `_apply_raw_color_management` 对引擎 RAW 状态与外部 WB 数据无 None/长度防护
  - 位置: src/features/color/ui/canvas.py:4871-4907
  - 证据:
    ```python
        params = self.engine.current_raw_params
        metadata = self.engine.current_raw_metadata
        ...
        baseline_wb = metadata.get("camera_wb", [2.0, 1.0, 1.5, 1.0])
        last_wb = last_decode.get('user_wb', baseline_wb)
        curr_wb = params.get('user_wb', last_wb)
        gains = np.array([
            curr_wb[0] / max(1e-4, last_wb[0]),
    ```
  - 影响: `current_raw_params`/`current_raw_metadata`/`_last_applied_raw_params` 任一为 None 即 AttributeError；`user_wb` 若少于 3 元素（外部 RAW 元数据，铁律要求外部数据须验证）IndexError；`params.get('gain', 1.0)` 也假定标量。取色路径在 mouseMove 中触发，异常会打断取色。
  - 建议: 入口做 None/长度校验并回退默认值；WB 向量统一 `[:3]` 截取与长度断言。
  - 置信度: 中

- [AUD-25218] [Suggestion] 大量公开方法缺少类型注解
  - 位置: src/features/color/ui/canvas.py:35（`_inject_menu_shortcuts(menu)`）、56（`MaskHandle.__init__(self, handle_type, parent=None, index=-1, sub_type=None)`）、3787（`set_image(self, cv_image, is_new_source=False)`）、4209（`set_tool_mode(self, mode)`）、5248（`update_mask_overlay(self, layer)`）等
  - 证据:
    ```python
    def _inject_menu_shortcuts(menu):
        """将 QAction 的快捷键以 \\t 格式追加到文本，确保弹出菜单中可见。"""
    ```
  - 影响: 项目规范要求强类型；`set_image`/`update_mask_overlay`/`set_tool_mode` 等跨模块接口无参数/返回注解，IDE 与静态检查无法捕获调用侧错误（文件后段新代码已有注解，风格不统一）。
  - 建议: 按文件后段标准补齐注解（cv_image: np.ndarray, is_new_source: bool -> None 等）。
  - 置信度: 高

- [AUD-25219] [Suggestion] 检查器分支异常日志无上下文
  - 位置: src/features/color/ui/canvas.py:4250-4253
  - 证据:
    ```python
            try:
                self._update_color_inspector(event.pos())
            except Exception as e:
                logger.error(f"Error: {e}")
    ```
  - 影响: 消息仅为 "Error: ..."，无 traceback、无位置标记，排障价值低；且宽泛捕获掩盖 `_update_color_inspector` 内的领域错误（同文件惯例是 `exc_info=True`）。
  - 建议: `logger.error("color inspector update failed", exc_info=True)`。
  - 置信度: 高

- [AUD-25220] [Suggestion] `fit_to_window` 对零尺寸 logical_size 无防护
  - 位置: src/features/color/ui/canvas.py:3977-3981
  - 证据:
    ```python
        img_w, img_h = self.logical_size.width(), self.logical_size.height()
        # Fit to fill the entire viewport height and width (no padding)
        scale = min(view_rect.width() / img_w, view_rect.height() / img_h)
    ```
  - 影响: 退化视频/异常图像导致 w 或 h 为 0 时 ZeroDivisionError（多数上游以正尺寸构造 QSize，但 `_display_scene_size_for_source` 允许 override 路径，防御成本低）。
  - 建议: `if img_w <= 0 or img_h <= 0: return`。
  - 置信度: 低

- [AUD-25221] [Suggestion] 空点击 MaskHandle 也会强制 `_sync_back`，产生无变化的引擎往返
  - 位置: src/features/color/ui/canvas.py:230-245
  - 证据:
    ```python
    def mouseReleaseEvent(self, event):
        self._is_dragging = False
        super().mouseReleaseEvent(event)
        if self.parentItem():
            parent = self.parentItem()
            if hasattr(parent, '_sync_timer'):
                parent._sync_timer.stop()
            if hasattr(parent, '_sync_back'):
                parent._sync_back()
    ```
  - 影响: 未产生位移的单击（press→release）也 emit `mask_shape_changed`，引擎侧若据此置脏/记版本，会污染修订计数与潜在的撤销粒度；高频点击下有无谓的信号-渲染往返。
  - 建议: 记录 press 时几何，release 时仅在有变化时 `_sync_back`（或由引擎侧幂等去重）。
  - 置信度: 中

### 通过项
- GPU 资源生命周期：本文件不直接创建 ModernGL/GL 对象；drawBackground 仅呈现已完成 front texture，且经 `_should_present_renderer_front_texture` 检查 `video_texture_pending`，`beginNativePainting/endNativePainting` 配对，`_in_draw_background` 标志防 render_pipeline 重入。
- 信号防递归（多数路径）：`set_current_frame` 用 blockSignals 同步双滑条；`set_active_shape` 用 scene.blockSignals；`_sync_back` 有 `_is_updating_handles` 早退，`update_from_data` 在句柄拖拽期间拒绝后端旧数据回写（防拖拽回弹/回环）。
- 拖拽同步节流设计：`_sync_timer`（16ms 单发）节流 + release 强制终同步，笔刷/旋转/控制点路径已落实。
- SAM 输入验证：点击点做归一化范围检查（0≤nx≤1、0≤ny≤1），`sam_ready` 防误触，右键删除有点存在性与索引校验。
- 取色器像素读取：位深分支（uint8/uint16/int16/float）、边界检查、BGR→RGB、Alpha 剥离处理完整；HDR 仅做等比保护而非硬 clamp（符合铁律意图）。
- 异常兜底惯例：`_monitor_synced_viewport_size`、`_refresh_video_frame_view`、`_sync_reference_menu_states` 均为窄化场景 + `logger.debug(..., exc_info=True)`，无静默吞错。
- 撤销/重做显示一致性：笔刷数据更新强制清缓存（正确性优先，性能问题另计 AUD-25206）；曲线控制点 `_rebuild_curve_handles` 支持工程加载后恢复；节点删除用 singleShot 延迟重建避免事件处理中销毁对象崩溃。
- 场景/句柄安全清理：`update_mask_overlay` 移除形状前先置 `_is_updating_handles`、解鼠标抓取、不清空 handles 字典（依赖 Qt 父子自动清理），`_update_handle_positions` 的动态句柄先隐藏再摘除。
- 时间线预览：`begin/end_timeline_preview` 的 pixmap 可见性与 fit 顺序处理（先 fit 后显示）避免闪烁与陈旧变换。
- 平移状态机：`mouseReleaseEvent` 无条件解除 `_is_panning` 防死锁；滚轮缩放的锚点漂移补偿（`_center_on_scene_point_without_drift`）实现正确且有界。
