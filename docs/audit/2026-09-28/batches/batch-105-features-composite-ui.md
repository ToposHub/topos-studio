# 批次报告：batch-105 — src/features/composite/ui

审查日期：2026-09-28 · 编号区间：AUD-10500–AUD-10599 · 任务书：assignments/batch-105.md

审查方式：全文通读 `node_editor_widget.py`（5128 行，offset 分段读完），并交叉只读核验
`composite_page.py`、`node_item.py`、`node_editor_actions.py`、`backdrop_item.py`、`sticky_note_item.py`、
`media_pool_panel.py`、`src/shared/media/factory.py` 中的关联实现。

统计：Critical 0 · Major 7 · Suggestion 8。

## src/features/composite/ui/node_editor_widget.py (行数: 5128)

### 发现

- [AUD-10500] [Major] Output/COLOR 节点保护与 Output 查找均基于显示标题，重命名后 Delete 键可删除 Output 节点
  - 位置: src/features/composite/ui/node_editor_widget.py:2132（另见 1496、4798）
  - 证据:
    ```python
    # 2130-2132（_delete_selected）
    selected_ids = [nid for nid, item in self._node_items.items()
                   if item.isSelected() and item.node_title not in ("COLOR", "Output")]
    ```
    ```python
    # 1493-1497（_find_output_node_item）
    for item in self._node_items.values():
        if getattr(item, "node_title", "") == "Output":
            return item
    ```
    ```python
    # 4797-4799（begin_node_rename，仅检查锁定/重命名态，无节点类型限制）
    item = self._node_items.get(str(node_id))
    if item is None or item.is_locked or item.is_renaming:
        return
    ```
  - 影响: `node_title` 是含 `user_label` 的显示标题（`set_title` 会改写它，node_item.py:174-180）。用户用 F2/右键重命名 Output 节点后：(1) 标题过滤器放行，`_delete_selected` → `actions.delete_selected_nodes()`（node_editor_actions.py:306-308 同样按标题过滤）直接 `remove_node_visual` 删除 Output——绕过 `delete_node`（actions 195-215）里基于 `node.node_type` 的守卫——合成图失去最终输出，导出/渲染链断裂；(2) `_find_output_node_item` 匹配失败，节点预览管线静默失效。
  - 建议: 身份判定一律用 `node.node_type in ("output", "color_input")`（或 `node_id` 常量），不用显示标题；`begin_node_rename` 对 output/color_input 类型直接拒绝。
  - 置信度: 高

- [AUD-10501] [Major] Delete 键批量删除 backdrop 后未发射 graph_changed，项目 dirty 标志可能缺失
  - 位置: src/features/composite/ui/node_editor_widget.py:2094-2107
  - 证据:
    ```python
    # 2101-2106（backdrop 批量删除路径，无 graph_changed）
    for backdrop_id in selected_backdrop_ids:
        self.remove_backdrop_visual(backdrop_id, emit_signal=False)
    # Emit batch delete signal for undo
    self.backdrops_deleted.emit(selected_backdrop_ids, deleted_data)
    return
    ```
    对比 sticky 路径（2123-2127）与右键单删路径（1731-1734）均有 `graph_changed.emit()`：
    ```python
    self.sticky_notes_deleted.emit(selected_sticky_note_ids, deleted_data)
    self.graph_changed.emit()
    ```
  - 影响: `remove_backdrop_visual` 已修改图模型（`_node_graph.remove_backdrop`，563-564 行），但此路径既无 widget 级也无 page 级 dirty 通知（composite_page.py:4156 `_on_backdrops_deleted` 只 push undo）。文件自身注释（595 行）明确 "graph_changed keeps project dirty/save working"——用户 Delete 删除 backdrop 后若无后续编辑，保存提示/自动保存可能不触发，删除不落盘。
  - 建议: 在 backdrop 批删 return 前补 `self.graph_changed.emit()`，与 sticky/单删路径对齐。
  - 置信度: 高

- [AUD-10502] [Major] dropEvent 在 UI 线程同步探测视频元数据（PyAV/ffmpeg），多文件拖放冻结界面
  - 位置: src/features/composite/ui/node_editor_widget.py:986（实现 935-960）
  - 证据:
    ```python
    elif ext in _VIDEO_EXTENSIONS:
        node_type = "video_frame"
        params = {"file_path": path}
        # Probe real media metadata so the Keyframes panel can show
        # the full source length immediately ...
        probe = self._probe_video_metadata(path)
    ```
    ```python
    # 943-944
    from src.shared.media.factory import MediaSourceFactory
    meta = MediaSourceFactory.probe_video_metadata(path)
    ```
    factory 侧 `_probe_video_metadata_ffmpeg`（factory.py:623-626）为同步 `import av; container = av.open(path)`。
  - 影响: 每个拖放的视频文件都在 UI 线程同步打开 demuxer（外加 `check_ffmpeg_available`）；一次拖入多个视频、网络盘或损坏文件时界面可感知卡死（秒级），违背实时交互基准。
  - 建议: 探测移到后台线程/任务队列，节点先以 `file_path` 创建，探测结果回填参数（文件注释本身说明渲染线程稍后会自行发现长度，UI 侧即时探测并非必需）。
  - 置信度: 高

- [AUD-10503] [Major] P7.2 assets_dropped 信号从未发射，dragEnterEvent 仅接受 URL 拖拽——媒体池资产拖放到节点画布功能未接线
  - 位置: src/features/composite/ui/node_editor_widget.py:233（常量 157；处理器 916-932）
  - 证据:
    ```python
    # P7.2: Asset drop signal for creating asset-backed nodes
    assets_dropped = pyqtSignal(list, tuple)  # asset_ids, drop_position (x, y)
    ```
    ```python
    # 916-925（dragEnterEvent 只认 URL）
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    if _is_media(url.toLocalFile()):
                        event.acceptProposedAction()
                        return
        event.ignore()
    ```
  - 影响: 全库 grep 无任何 `assets_dropped.emit`；composite_page 也未安装替代 drop 处理。媒体池拖拽（media_pool_panel.py:200-236 携带自定义 MIME + 可选 URL fallback，且其注释明确要求"先查自定义 MIME 再查 hasUrls，否则内部拖拽被误路由为文件导入"）落到节点画布时：有本地路径的资产被降级为普通文件导入（丢失 asset 关联、P7.2 asset-backed 节点从不创建），无本地路径的资产直接被 dragEnter 拒绝。声明的功能为死代码。
  - 建议: dragEnter/dragMove/drop 中先解析 `MIME_TYPE_MEDIA_POOL_ASSETS`（可复用 `src/features/composite/core/media_drop_payload.py` 的解析器），命中则发射 `assets_dropped(asset_ids, scene_pos)`，未命中再走 URL 文件导入。
  - 置信度: 高

- [AUD-10504] [Major] Ctrl+点击多选与 Qt 场景默认选择处理双重应用，手动发射的选择信号可与画布状态失同步
  - 位置: src/features/composite/ui/node_editor_widget.py:1300-1303（super 转发在 1139）
  - 证据:
    ```python
    if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
        # Toggle selection with Ctrl
        node_item.set_selected(not node_item.isSelected())
        self._emit_selection_signal()
    ```
    ```python
    # 1139（节点点击路径最终仍调用）
    super().mousePressEvent(event)
    ```
    NodeItem 设有 `ItemIsSelectable`（node_item.py:145）、`set_selected` 直接 `setSelected`（node_item.py:769-773），且 NodeItem/PortItem 均未覆写 `mousePressEvent`。
  - 影响: 手动 toggle 并发射 `nodes_selected`/`node_selected` 后，`super().mousePressEvent` 把同一事件交给场景，Qt 按当前（已被手动翻转的）状态再次应用 Ctrl=toggle 语义，净效果为视觉选择回弹到原状，而面板已收到"已加入选择"的信号——面板选中态与画布选中态失同步；`_apply_preview_mode` 也按回弹后的实际状态重算，预览缩略图与信号不一致。
  - 建议: 二选一：手动选择路径消费事件（节点点击分支 `return`，不调 super 的场景分发），或去掉手动 Ctrl/Shift 处理、统一依赖 Qt 语义 + `selectionChanged` 派发信号。
  - 置信度: 中（双重处理结构确定；Qt 压力测试下的精确表现未执行验证）

- [AUD-10505] [Major] `_add_new_node` 的 `_batch_mode` 置位无 try/finally，异常路径将永久卡在批量模式
  - 位置: src/features/composite/ui/node_editor_widget.py:2891-2913
  - 证据:
    ```python
    needs_batch = source_node_id is not None
    if needs_batch:
        self._batch_mode = True
    ...
    new_node = self._actions.add_node(node_type, pos_x, pos_y)
    ...
    if (new_node and source_node_id
            and getattr(new_node, "node_type", None) not in _VIEWER3D_OBJECT_TYPES):
        self._auto_connect_after(source_node_id, new_node)

    if needs_batch:
        self._batch_mode = False
    ```
  - 影响: `add_node` 或 `_auto_connect_after` 抛异常（未知节点类型、注册表错误、连接校验失败等）时 `_batch_mode` 永久为 True；此后 `add_node_visual`（503-507 行）永远跳过 containment 刷新与 preview 应用，`_on_sticky_note_changed` 等路径的 graph_changed 被抑制，画布停止自我刷新且难以归因。同类用法在 `build_from_graph`（877-882）与 `_execute_shake_disconnect`（3077-3084）均正确使用 try/finally，唯此处缺失。
  - 建议: 与其他两处对齐，用 `try/finally` 包裹 add_node/_auto_connect_after，finally 中复位 `_batch_mode`。
  - 置信度: 中（结构缺失确凿；触发依赖异常）

- [AUD-10506] [Major] `set_viewport_state` 未校验外部反序列化数据的类型，损坏的 viewport 状态可在建图时抛 TypeError
  - 位置: src/features/composite/ui/node_editor_widget.py:4940-4953
  - 证据:
    ```python
    zoom = state.get("zoom", 1.0)
    center_x = state.get("center_x", 0.0)
    center_y = state.get("center_y", 0.0)

    # Clamp zoom to valid range
    zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, zoom))
    ...
    self.setTransform(QTransform().scale(zoom, zoom))
    ...
    self.centerOn(QPointF(center_x, center_y))
    ```
  - 影响: `state` 来自持久化项目文件（`graph._viewport_state`）。键存在但值为 null/字符串（手改或损坏的项目 JSON）时：键存在时 `.get` 默认值不生效，`zoom=None` → `min(float, None)` 抛 TypeError；该调用位于 `build_from_graph` 主路径（893-896），异常会中断整个合成页建图。违反"外部数据须验证"铁律（同函数内 locked_node_ids 有 isinstance 过滤，zoom/center 却没有）。
  - 建议: 读取后做 `isinstance(v, (int, float)) and not isinstance(v, bool)` 校验并回退默认值，或用 try/except 降级。
  - 置信度: 中（取决于上游加载器是否已消毒，未验证）

- [AUD-10507] [Suggestion] `_on_backdrop_changed` 的"已删除"分支不发射 undo 信号也不发 graph_changed，与 sticky 对应处理器不对称
  - 位置: src/features/composite/ui/node_editor_widget.py:2682-2687
  - 证据:
    ```python
    if item.scene() is None:
        if self._node_graph is not None:
            self._node_graph.remove_backdrop(item.backdrop_id)
        self._backdrop_items.pop(item.backdrop_id, None)
        return
    ```
    对比 `_on_sticky_note_changed`（646-652）在同样情形发射 `sticky_note_deleted` 供 undo（StickyNoteItem 确有自删路径 sticky_note_item.py:463-465；BackdropItem 目前无自删路径，该分支为防御性代码）。
  - 影响: 一旦 BackdropItem 将来获得自删能力（结构上与 sticky 对称），此路径删除的 backdrop 无 undo 记录、无 dirty 通知，Ctrl+Z 无法恢复。
  - 建议: 与 sticky 对齐：删除分支发射 `backdrop_deleted(id, data)` 并 `graph_changed.emit()`；若确认死代码则删除该分支并注释说明。
  - 置信度: 高（不对称确凿；当前无实际触发路径）

- [AUD-10508] [Suggestion] `_flash_node_highlight` 每次导航新建 QTimer(self) 且从不销毁；闪烁通过切换真实选中态实现
  - 位置: src/features/composite/ui/node_editor_widget.py:2353-2381
  - 证据:
    ```python
    flash_timer = QTimer(self)
    flash_timer.setSingleShot(True)
    ...
    def do_flash():
        ...
        if flash_count % 2 == 1:
            item.set_selected(True)
    ```
  - 影响: 每次 Jump-to-Node/搜索导航泄漏一个挂在 self 下的 QTimer（C++ 对象随父存活，Python 包装失效后仍不回收）；反复导航累积。且闪烁用 `set_selected(True/False)` 实现，每次翻转触发场景 `selectionChanged` → `_apply_preview_mode` 全表重算，预览缩略图可能随闪烁抖动。
  - 建议: 用无副作用的视觉高亮（自绘 glow 属性 + update），计时器结束后 `deleteLater()` 或复用单一成员计时器。
  - 置信度: 高

- [AUD-10509] [Suggestion] 预设名从列表显示文本 `split(" (")[0]` 反解析，名称含 " (" 时操作错误对象
  - 位置: src/features/composite/ui/node_editor_widget.py:3915-3916（同模式 3935-3936、3967-3968、3994-3995）
  - 证据:
    ```python
    display_text = current_item.text()
    preset_name = display_text.split(" (")[0]
    ```
  - 影响: 显示格式为 `f"{name} ({n} nodes)"`。用户命名 "My (cool) preset" 时解析得 "My"：加载/导出发"不存在"警告；若恰好存在名为 "My" 的预设，删除/重命名/导出会作用到错误预设（删除为不可撤销的用户数据损失）。
  - 建议: QListWidget 用 `Qt.ItemDataRole.UserRole` 存真实预设名，取 `current_item.data(...)` 而非解析文本。
  - 置信度: 高

- [AUD-10510] [Suggestion] 多处 `except Exception: pass` 静默吞掉配置/注册表/分组查询错误，无日志
  - 位置: src/features/composite/ui/node_editor_widget.py:1390-1391（同模式 1409-1410、1891-1892、2230-2231、3103-3104、3465-3466、3531-3532）
  - 证据:
    ```python
    # _resolve_preview_mode
        try:
            cm = getattr(self.window(), "config_manager", None)
            if cm is not None:
                val = cm.get(self.PREVIEW_CONFIG_KEY)
                if val in self.PREVIEW_MODES:
                    self._preview_mode = val
        except Exception:
            pass
    ```
  - 影响: 违反 .clinerules"不得宽泛 except 掩盖领域错误"：配置系统故障（如 ConfigManager 抛底层异常）被完全静默，用户设置永不生效且无诊断线索；`_group_member_ids` 吞掉分组查询异常后返回空集，进入组视图会显示空画布而无任何报错。
  - 建议: 至少 `logger.debug(..., exc_info=True)`；对可预期异常收窄类型（AttributeError/KeyError/ValueError）。
  - 置信度: 高

- [AUD-10511] [Suggestion] 未使用导入（copy/QMimeData/QByteArray/QFont/QColorDialog）；`_viewing_node_id` 未在 `__init__` 声明
  - 位置: src/features/composite/ui/node_editor_widget.py:16-33（属性见 4963、4990）
  - 证据:
    ```python
    from PyQt6.QtWidgets import (
        QGraphicsView, QGraphicsScene, QGraphicsItem,
        QMenu, QGraphicsProxyWidget, QDockWidget,
        QInputDialog, QColorDialog            # QColorDialog 未使用
    )
    from PyQt6.QtCore import Qt, QPointF, QRectF, pyqtSignal, QTimer, QMimeData, QByteArray, QEvent  # QMimeData/QByteArray 未使用
    ...
    import copy                              # copy 未使用
    ```
    ```python
    "viewing_node_id": getattr(self, '_viewing_node_id', None),  # 动态属性兜底
    ```
  - 影响: 死导入增加阅读负担并掩盖 AUD-10503 这类"声明未接线"问题（QMimeData 导入暗示本应有 MIME 处理）；`_viewing_node_id` 靠 getattr 兜底说明状态字段未集中声明。
  - 建议: 删除未用导入；`__init__` 中显式 `self._viewing_node_id: Optional[str] = None`。
  - 置信度: 高

- [AUD-10512] [Suggestion] `clear_all` 不复位 `_selected_node_id`，重建后可能返回悬空节点 ID
  - 位置: src/features/composite/ui/node_editor_widget.py:836-855
  - 证据:
    ```python
    def clear_all(self):
        for conn_data in self._connection_items:
            conn_data[0].stop_flow_animation()
        ...
        self._node_items.clear()
        ...
        self._scene.clear()
    ```
  - 影响: 代码中共四处将 `_selected_node_id` 置 None（1083、1094、1109、1332），唯独清空/重建路径不复位；`build_from_graph` 后若 `restore_ui_state` 无保存的 viewer state，`get_selected_node_id()` 返回已不存在节点的 ID，面板/导航历史（`_record_navigation_point` 保存 `_selected_node_id`）引用悬空 ID。
  - 建议: `clear_all` 中补 `self._selected_node_id = None`（以及 `_preview_selection_source = "none"`）。
  - 置信度: 高

- [AUD-10513] [Suggestion] 事件覆盖方法与部分工具函数缺少类型注解
  - 位置: src/features/composite/ui/node_editor_widget.py:137、727、916、927、962、1005、1805、2425、2540、2575、2834
  - 证据:
    ```python
    def dragEnterEvent(self, event):          # 缺 -> None 与事件类型
    def dropEvent(self, event):
    def scrollContentsBy(self, dx, dy):
    def drawBackground(self, painter, rect):
    def contextMenuEvent(self, event):
    def eventFilter(self, obj, event):
    def _shake_params_from_sensitivity(sensitivity) -> tuple[int, float]:  # sensitivity 无注解
    def _find_non_overlapping_position(x: float, y: float) -> tuple:       # 裸 tuple
    ```
  - 影响: 项目规范要求全量类型注解；裸 `tuple` 返回与无参注解降低可读性与静态检查覆盖。
  - 建议: 补齐事件类型（`QDragEnterEvent` 等）、返回注解与 `sensitivity: float`；tuple 细化为 `tuple[float, float]`。
  - 置信度: 高

- [AUD-10514] [Suggestion] `_execute_shake_disconnect` 的 `_shake_disconnecting = False` 位于 try/finally 之外，异常时标志卡 True
  - 位置: src/features/composite/ui/node_editor_widget.py:3077-3086
  - 证据:
    ```python
        try:
            self._extract_node_from_chain(shaken_node_id)
        finally:
            self._batch_mode = False
            self.setUpdatesEnabled(True)
            self.graph_changed.emit()

        self._shake_disconnecting = False
    ```
  - 影响: `_extract_node_from_chain` 抛异常时（mouseMoveEvent 内执行），`finally` 恢复了 batch/updates 但 `_shake_disconnecting` 永久为 True，本次拖拽内 shake 检测被锁死（直至 mouseReleaseEvent 1263 行兜底复位），且异常继续传出 mouseMoveEvent 进入 Qt 事件循环。
  - 建议: 将 `self._shake_disconnecting = False` 移入 finally。
  - 置信度: 高

### 通过项
- 线程安全：纯 UI 线程组件，无 OpenGL/渲染线程调用（诊断注入 `set_node_diagnostics` 由页面 UI 侧调用，composite_page.py:1633）
- GPU 资源生命周期：不接触 ModernGL/纹理/FBO，无 GPU 资源管理面
- GLSL·HDR 正确性：不涉及 GLSL 与 HDR 数据处理（纯画布 UI）
- 信号防递归（预览连接）：`set_preview_node`/`_clear_preview_connection` 幂等；`clear_all` 先清 Python 引用再 `scene.clear()`，规避 selectionChanged 重入解引用已删对象（843-854 注释成立）
- 节点移动 undo 顺序：`mouseReleaseEvent` 先发 `nodes_moved` 再 `_update_graph_positions()`（1255-1258）看似与布局函数（先更新后发信号）不一致，但已核实接收方 `composite_page._on_nodes_moved` 从可视 `item.pos()` 取新位置（composite_page.py:4094-4095），不依赖模型即时同步，无 undo 数据错误
- drop-on-connection 插入异常路径：`_execute_connection_insertion`（3334-3451）具备完整 try/finally、失败回连、重入保护与高亮清理
- `build_from_graph` 的 `_batch_mode` 与 `_navigation_restore_active` 均用 try/finally 正确复位（877-896）
- 连接拖拽类型与锁定校验：正向/反向完成时均校验 data_type、方向、双端锁定（2790-2826）；snap 搜索校验容量/类型/自连（4268-4343）
- `_extract_node_from_chain` 先构建快照再变更、删除列表在变更后重建（3006-3040），无迭代中修改集合问题
- 锁定 ID 外部输入过滤：`build_from_graph`/`set_viewport_state` 对 locked_node_ids 做 isinstance + 存在性双重过滤（863-867、4931-4936）
- 性能模式（整体）：BspTreeIndex 空间索引（274）、端口/节点命中 O(1) 查找（2593-2625）、snap 搜索视口裁剪（4298-4310）、近邻检测 80ms 时间节流 + 8px 位移早退 + 包围盒预筛（3228-3260）、拖拽期间 flow 动画慢速模式（1127-1129/1276-1278）
- 拖放文件路由统一引用 format_registry 的扩展名集合（180-185），无硬编码漂移
