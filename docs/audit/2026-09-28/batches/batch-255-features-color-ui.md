# 批次报告：batch-255 [P2] src/features/color/ui//

- 审查日期：2026-09-28
- 文件数：4（2,710 行）
- 编号区间：AUD-25500–AUD-25599（实际使用 AUD-25500–AUD-25518）
- 计数：Critical 0 / Major 7 / Suggestion 11
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失

---

## src/features/color/ui/mask_panel.py (行数: 980)

### 发现

- [AUD-25500] [Major] DragValue 交互信号未转发，遮罩参数拖拽缺少 undo 快照边界
  - 位置: src/features/color/ui/mask_panel.py:579
  - 证据:
    ```python
    drag = DragValue(label_text, min_v, max_v, def_v, decimals=decimals, suffix=suffix, compact=compact)
    drag.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    drag.valueChanged.connect(self.on_value_changed)
    return drag
    ```
    而 `DragValue` 本身定义了 `interaction_started` / `interactionEnded` 信号（src/shared/ui/widgets/drag_value.py:80-82），且 main_window 已连接面板级信号：`self.mask_panel.interaction_started.connect(self.on_interaction_started)`（src/app/main_window.py:1576），该处理器负责压入撤销历史 `self.engine.history.push_state(...)`（src/app/controllers/menu_toolbar_controller.py:1617）。
  - 影响: 面板声明了 `interaction_started`/`interaction_ended` 信号但除 reset_all 外从不发射 `interaction_started`；拖拽羽化/不透明度/变换等参数时 `history.push_state` 从不执行，遮罩参数修改不可撤销（或撤销分组错误），破坏 undo 一致性。对比 mixer_panel/plugin_panel 均正确转发了 ParamSliderRow 的交互信号。
  - 建议: 在 `create_param` 中将 `drag.interaction_started` / `drag.interaction_ended` 连接到面板同名信号；在 `_emit_pending_params` 前后保持配对发射。
  - 置信度: 高

- [AUD-25501] [Major] 树重建不保留原选中项并强制选中最后一项，延迟执行还会覆盖控制器的显式选中
  - 位置: src/features/color/ui/mask_panel.py:662,796-807
  - 证据:
    ```python
    def update_shapes_list(self, shapes: List):
        """更新遮罩列表（延迟执行以避免信号冲突）。"""
        self._pending_shapes = list(shapes)
        QTimer.singleShot(0, self._do_update_shapes_list)
    ...
    def _auto_select_last_item(self) -> None:
        if self.tree_widget.topLevelItemCount() > 0:
            last_top = self.tree_widget.topLevelItem(self.tree_widget.topLevelItemCount() - 1)
            if last_top:
                if last_top.childCount() > 0:
                    last_child = last_top.child(last_top.childCount() - 1)
                    if last_child:
                        self.tree_widget.setCurrentItem(last_child)
    ```
    `_do_update_shapes_list` 只保存/恢复展开状态（`_get_expanded_ids`/`_restore_expanded_state`），不保存选中项；重建后 `_auto_select_last_item` 在 `blockSignals(False)` 之后调用 `setCurrentItem`，触发 `itemSelectionChanged → shape_selected` → 控制器 `on_panel_shape_selected`（改画布 active shape、重绑 Tracker）。调用方（如 color_controller.py:1021-1023）在 `update_shapes_list` 之后同步调用的 `canvas.set_active_shape(group.id)` / `mask_panel.select_shape(group.id)` 先于延迟的重建执行，随后即被自动选中覆盖。
  - 影响: 任何列表刷新（AI mask 创建、SAM 确认、控制器各处 `update_shapes_list` 调用、undo/redo 后）都会把选中跳到最后一个窗口并发射 `shape_selected` 副作用，用户正在编辑的窗口选中态丢失，参数面板/画布/Tracker 被切到无关窗口。
  - 建议: 重建前记录当前选中 shape_id 并在恢复展开状态后一并恢复（找不到时再回退自动选中）；或仅在"新增窗口"场景自动选中末项。
  - 置信度: 高（机制明确；触发频率取决于 mask_shapes_changed 的发射时机）

- [AUD-25502] [Suggestion] 节流待发参数不随选中切换/删除取消，且 `_pending_shape_id` 发射后不清空
  - 位置: src/features/color/ui/mask_panel.py:591-602
  - 证据:
    ```python
    def _schedule_param_update(self, shape_id: str, params: dict) -> None:
        self._pending_shape_id = shape_id
        self._pending_params.update(params)
        if not self._param_timer.isActive():
            self._param_timer.start(16)  # 16ms ≈ 60 FPS

    def _emit_pending_params(self) -> None:
        if self._pending_shape_id and self._pending_params:
            self.shape_updated.emit(self._pending_shape_id, self._pending_params)
            self._pending_params = {}
    ```
  - 影响: `reset_all`（957-960 行）正确停止了定时器，但单个窗口删除/选中切换路径没有：16ms 窗口内删除 shape 后定时器仍会发射 `shape_updated(已删id, ...)`，落入控制器 `pending_mask_updates` 成为死条目；`_pending_shape_id` 发射后残留旧值。当前消费方只是字典入队（color_controller.py:227-229），未崩溃，故降为 Suggestion。
  - 建议: `_emit_pending_params` 成功发射后同时清空 `_pending_shape_id`；在 shape 删除/选中切换时停止 `_param_timer` 并清空 pending。
  - 置信度: 中

- [AUD-25503] [Suggestion] 任一控件变化即全量打包提交所有参数（含未同步的变换值）
  - 位置: src/features/color/ui/mask_panel.py:621-640
  - 证据:
    ```python
    params = {
        "feather": self.feather_drag.value(),
        "opacity": self.opacity_drag.value() / 100.0,
        "blur": self.blur_drag.value(),
        "clean_black": self.clean_black_drag.value() / 10.0,
        "clean_white": self.clean_white_drag.value() / 10.0
    }
    # Transform
    if self.transform_group.isVisible():
        params["center"] = [self.pos_x_drag.value(), self.pos_y_drag.value()]
    ```
  - 影响: 拖动单个控件会把其余控件的当前值一并提交；若目标 shape 的控件尚未被 `set_params` 同步（选中切换后的 16ms 节流窗口内），会把上一个窗口的残留值写入新选中窗口。窗口极小但语义上应只提交变化项。
  - 建议: 按 sender() 识别来源控件，仅打包该控件的键；或至少在选中切换时先取消 pending（与 AUD-25502 一并处理）。
  - 置信度: 中

- [AUD-25504] [Suggestion] reset_all 中的 no-op 样式语句与未配对的 interaction_ended
  - 位置: src/features/color/ui/mask_panel.py:969,980
  - 证据:
    ```python
    self.btn_birefnet.setStyleSheet(self.btn_birefnet.styleSheet())
    self.btn_sam.setChecked(False)
    ...
    self.all_masks_cleared.emit()
    self.interaction_ended.emit()
    ```
  - 影响: `setStyleSheet(styleSheet())` 把样式表原样写回，纯无效代码；`interaction_ended` 没有对应的 `interaction_started`（整个类从未发射 started），触发 `idle_render_timer.start()` 与 `_trigger_autosave()` 的不对称调用，且清除操作本身因此没有 undo 快照。
  - 建议: 删除 no-op 行；reset_all 用 interaction_started/ended 成对包裹（或由 all_masks_cleared 处理器自行压栈）。
  - 置信度: 高

- [AUD-25505] [Suggestion] 死导入、硬编码英文文案与缺失类型注解
  - 位置: src/features/color/ui/mask_panel.py:2-17,83,109,140 等
  - 证据:
    ```python
    from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                                 QLabel, QFrame, QGridLayout, QSizePolicy, ...)
    from PyQt6.QtGui import QIcon, QPixmap
    from src.shared.assets.icon_manager import (..., IconColors)
    ...
    self.invert_btn.setToolTip("Invert Mask")
    ```
  - 影响: `QFrame`/`QGridLayout`/`QIcon`/`QPixmap`/`IconColors` 全文件未使用；多个按钮 tooltip 硬编码英文（"Invert Mask"/"Delete Mask Window"/"Disable Mask"/"Mode: ..."）未走 I18N，与其余 I18N.tr 用法不一致；`init_ui`/`reset_all`/`get_brush_params`/`_show_tree_context_menu(self, pos)` 等方法缺返回类型与参数注解。
  - 建议: 清理未使用导入；tooltip 统一走 `I18N.tr`；为公共方法补类型注解。
  - 置信度: 高

### 通过项
- GPU 资源生命周期：文件不接触 ModernGL/OpenGL，仅发信号由控制器驱动渲染，无 UI 线程 GL 调用
- 线程安全：全部控件操作在 UI 线程，节流用 QTimer(singleShot)，无跨线程共享可变状态
- 信号防递归：`_do_update_shapes_list`/`select_shape`/`set_params`/`set_transform_params`/`set_brush_params` 均正确 blockSignals；`_build_tree` 在阻塞窗口内完成
- GLSL·HDR 正确性：opacity/clean_black/clean_white 仅做 UI 量程映射（/100、/10），无对 HDR 数据的无理由 clamp
- 输入验证（内部路径）：`_on_group_btn_clicked`/`on_item_clicked` 对空 id/空树有 None 守卫
- 异常路径：`reset_all` 停止 pending 定时器防止向已删 shape 发信号（见 AUD-25502 的其余路径缺口）

---

## src/features/color/ui/mixer_panel.py (行数: 288)

### 发现

- [AUD-25506] [Suggestion] reset_group/reset_all 直接发射内部 self.params 引用，与节流路径的深拷贝语义不一致
  - 位置: src/features/color/ui/mixer_panel.py:266,283（对比 165）
  - 证据:
    ```python
    self._syncing_ui = False
    self.mixer_changed.emit(self.params)
    ...
    self._syncing_ui = False
    self.mixer_changed.emit(self.params)
    self.reset_clicked.emit()
    ```
    而节流路径为拷贝：`self._pending_params = {color: values[:] for color, values in self.params.items()}`（165 行）。
  - 影响: 同一信号两条路径载荷语义不同：reset 路径把面板内部可变 dict 原样交给接收方，接收方若缓存/修改该 dict 会直接污染面板状态，后续 set_params 前的读数被篡改。
  - 建议: reset 路径同样发射深拷贝 `{c: v[:] for c, v in self.params.items()}`。
  - 置信度: 中（当前消费方若只读则无实际故障）

- [AUD-25507] [Suggestion] 多数方法缺失类型注解
  - 位置: src/features/color/ui/mixer_panel.py:54,113,155,229,248,268
  - 证据:
    ```python
    def create_mixer_tab(self, param_idx, label_prefix):
    ...
    def update_param(self, color, idx, val):
    ...
    def set_params(self, params):
    ```
  - 影响: `create_mixer_tab`/`create_color_row`/`update_param`/`set_params`/`reset_group`/`reset_all` 均无参数与返回类型注解，不符合项目强类型约束。
  - 建议: 补齐 `-> None`、`param: dict` 等注解；`_normalize_params` 已有 `-> dict` 可作范本。
  - 置信度: 高

### 通过项
- 信号防递归：`_syncing_ui` 状态锁在 update_param/_schedule_param_update 双重守卫，set_params/reset 用 try/finally 复位；slider 交互正确转发 interactionStarted/Ended 并在释放时 flush pending
- 状态提交一致性：set_params/reset 均先 `_param_timer.stop()` 并清空 `_pending_params`，无节流残留
- 输入验证：`_normalize_params` 完整处理缺色/字典格式/短列表/非数值/越界（`_clamp_value` 用精确异常 TypeError/ValueError，非宽泛 except）
- 性能模式：16ms 单发节流 + 释放即 flush，符合 60FPS 拖拽规范
- 线程安全 / GPU 生命周期 / GLSL·HDR：纯 UI 面板无 GL 调用、无跨线程状态、无 HDR 数据处理，无问题

---

## src/features/color/ui/plugin_panel.py (行数: 758)

### 发现

- [AUD-25508] [Major] `_create_param_widget` 污染共享插件元数据 `param['instance_id']`，多实例下 select 回调错路由且泄漏进新实例默认参数
  - 位置: src/features/color/ui/plugin_panel.py:425,585,740
  - 证据:
    ```python
    # Add instance_id to param dict for select widgets
    param['instance_id'] = instance_id
    ...
    combo.currentIndexChanged.connect(lambda idx, p=param: self._on_select_changed(p, idx))
    ...
    def _on_select_changed(self, param, index):
        instance_id = param.get('instance_id')
    ```
    `param` 来自 `get_plugin_manager().get_plugin(plugin_id)` 返回的管理器缓存 dict（plugin_manager.py:498 `return self.plugins.get(plugin_id)`），同一插件的多个实例共享同一批 param dict；`set_layer` 每建一个实例就覆写一次 `instance_id`。同时 color_controller.py:1806-1808 基于同一被污染的 dict 构建 `default_params[param['var_name']] = param.get('default', 0.0)` 所在列表——注意 `add_plugin_to_current_layer` 遍历的 `plugin.get('params', [])` 已含被塞入的 `instance_id` 键的 dict（键名恰好不进 default_params，但污染持续存在）。
  - 影响: 图层上挂同一插件两个实例时，前一个实例的 select 下拉变更会写入最后一个实例的 params（错路由）；共享元数据被 UI 会话污染，违反"外部数据不可变"约定，跨图层/跨会话累积。
  - 建议: 不修改 param dict，select 回调改用创建时捕获的 `instance_id`（与 slider/checkbox/color 的 lambda 默认参数做法一致，见 467/502/533 行）。
  - 置信度: 高

- [AUD-25509] [Major] reset/toggle/delete 等离散操作无 interaction 边界，缺少 undo 快照
  - 位置: src/features/color/ui/plugin_panel.py:615-643,645-677,605-613（对比 540-552）
  - 证据:
    ```python
    def _reset_plugin(self, instance_id, plugin_id):
        ...
        for param in plugin_data.get('params', []):
            var_name = param['var_name']
            default_val = param.get('default', 0.0)
            plugin_inst['params'][var_name] = default_val
        ...
        self.params_changed.emit()
    ```
    对比 `_on_color_changed`（545-552 行）正确包裹：`self.interaction_started.emit()` ... `self.params_changed.emit()` ... `self.interaction_ended.emit()`（注释明确"保证恰好一次 undo 快照"）。undo 压栈发生在 `on_interaction_started`（menu_toolbar_controller.py:1617）。
  - 影响: 重置参数/启停插件/删除效果都不会先压入撤销历史：reset 前的参数值丢失、不可撤销；delete 依赖 `engine.remove_plugin` 内部是否补救（未验证），toggle 同理。与 color 路径行为不一致。
  - 建议: 为 `_reset_plugin`/`_toggle_plugin`/`_delete_plugin` 补 interaction_started/ended 包裹（离散操作各产生恰好一次快照）。
  - 置信度: 中

- [AUD-25510] [Suggestion] 右键菜单每次打开都在 UI 线程同步重扫插件目录
  - 位置: src/features/color/ui/plugin_panel.py:149-155
  - 证据:
    ```python
    def _show_context_menu(self, pos):
        menu = QMenu(I18N.tr("effects.add_effect", "Add Effect"), self)
        # Scan and add all available plugins
        get_plugin_manager().scan_plugins()
        plugins = get_plugin_manager().get_all_plugins()
    ```
  - 影响: 每次右键都触发文件系统遍历 + 解析全部 .tglsl/清单（UI 线程阻塞）；插件目录大或位于网络卷时菜单明显延迟。
  - 建议: 菜单里提供显式 "Reload" 动作触发扫描，或仅在缓存为空/文件 mtime 变化时重扫。
  - 置信度: 中

- [AUD-25511] [Suggestion] 死代码与未使用导入
  - 位置: src/features/color/ui/plugin_panel.py:12-15,24-25,35,52,592-601,688-694
  - 证据:
    ```python
    from PyQt6.QtWidgets import (QWidget, ..., QSlider, QScrollArea, QFrame,
                                 QSizePolicy, ...)
    from PyQt6.QtGui import QIcon, QAction
    from src.shared.assets.icon_manager import icon_reset, IconColors
    ...
    # Slider resolution (number of steps for float mapping)
    SLIDER_STEPS = 1000
    ...
    def _float_to_int(self, value, min_val, max_val):
    def _int_to_float(self, int_val, min_val, max_val):
    def _on_slider_pressed(self):
    def _on_slider_released(self):
    ```
  - 影响: `QSlider`/`QSizePolicy`/`QIcon`/`IconColors` 未使用；`_float_to_int`/`_int_to_float`/`SLIDER_STEPS`/`_on_slider_pressed`/`_on_slider_released` 是旧 QSlider 方案遗留，全文件无调用。
  - 建议: 删除遗留方法与未使用导入。
  - 置信度: 高

- [AUD-25512] [Suggestion] `_delete_plugin` 通过类名嗅探向上访问 MainWindow 并直接刷新 layer_panel，破坏分层解耦
  - 位置: src/features/color/ui/plugin_panel.py:665-686
  - 证据:
    ```python
    main_window = self._find_main_window()
    if main_window and hasattr(main_window, 'layer_panel'):
        main_window.layer_panel.update_layers(
            self.engine.layers,
            getattr(self.engine, 'active_layer_index', 0)
        )
    ...
    def _find_main_window(self):
        widget = self.parent()
        while widget:
            if widget.__class__.__name__ == 'MainWindow':
                return widget
    ```
  - 影响: features 层面板按类名字符串硬编码攀爬到 app 层 MainWindow 并直接调用其子面板，绕过信号解耦；MainWindow 重构/改名后静默失效（fx 指示器不更新），且与项目 products→features→shared→app 分层约束冲突。
  - 建议: 增加面板级信号（如 `layer_stack_changed`）由控制器转发刷新。
  - 置信度: 高（架构问题；当前功能可用）

### 通过项
- GPU 资源生命周期：删除效果走 `engine.remove_plugin` 统一资源清理（deferred-release 归引擎管），UI 层不直接触碰 GL 对象
- 信号防递归：select 构建期 `combo.blockSignals(True)` 包裹 setCurrentIndex；slider/checkbox/color 回调用 lambda 默认参数绑定 id 避免闭包晚绑定（select 除外，见 AUD-25508）
- 输入验证：`_create_color_widget` 对 current 值做 isinstance/len 校验后才取用；未知参数类型渲染占位行并记日志（P1-COLOR-14），不静默吞掉
- 异常路径：`_create_effect_block` 对缺失插件显示错误 GroupBox 而非抛异常
- 性能模式：参数变更直接 `params_changed` 由外部节流渲染；`resizeEvent` 的可见性批量应用开销可接受
- 线程安全：无跨线程状态；`_clear_content` 用 deleteLater 安全清理动态控件

---

## src/features/color/ui/presets_panel.py (行数: 684)

### 发现

- [AUD-25513] [Major] PresetDelegate.paint 每次重绘都从磁盘解码缩略图 QPixmap，无任何缓存
  - 位置: src/features/color/ui/presets_panel.py:553-576（及 583 的 zip 元数据读取）
  - 证据:
    ```python
    toporos_path = index.data(Qt.ItemDataRole.UserRole)
    if topos_path:
        preset_name = os.path.splitext(os.path.basename(topos_path))[0]
        thumb_path = os.path.join(os.path.dirname(topos_path), f"{preset_name}.thumb.jpg")
        if os.path.exists(thumb_path):
            pixmap = QPixmap(thumb_path)
            if not pixmap.isNull():
    ```
  - 影响: 悬停高亮、选中、滚动、窗口 resize 都触发整格重绘；每次 paint 都 `os.path.exists` + `QPixmap(path)` 磁盘读取与 JPEG 解码。512px 缩略图 × 数十条目时 UI 线程持续做重复 I/O/解码，Gallery 滚动与调色主窗口 resize 明显卡顿，违背 4K 实时性能基准。
  - 建议: 按 topos_path 缓存 QPixmap（QPixmapCache 或 dict + LRU）；`exists` 结果随扫描缓存。
  - 置信度: 高

- [AUD-25514] [Major] 用户预设根目录绑定 `os.getcwd()`，换启动目录后预设"丢失"
  - 位置: src/features/color/ui/presets_panel.py:41-43,49-52
  - 证据:
    ```python
    self.presets_root = os.path.join(os.getcwd(), "Presets")
    self._ensure_dirs()
    ...
    def _ensure_dirs(self):
        for sub in ["User", "System"]:
            os.makedirs(os.path.join(self.presets_root, sub), exist_ok=True)
    ```
  - 影响: 从 Finder、快捷方式、cron/launchd 或任意其他工作目录启动时 CWD 不同，面板读写的是另一份空的 `Presets/`；用户已有静帧/预设不可见，新抓取的 still 也写进临时位置，换回原目录后表现为"数据丢失"。只读 CWD 下 `os.makedirs` 还会在构造函数直接抛 PermissionError（启动崩溃）。
  - 建议: 使用稳定的应用数据目录（QStandardPaths.AppDataLocation 或可配置的用户目录），首次启动迁移旧 cwd/Presets。
  - 置信度: 高

- [AUD-25515] [Major] create_folder/_rename_preset 未验证用户输入的名称：路径注入与未捕获 OSError
  - 位置: src/features/color/ui/presets_panel.py:344-364,403-428
  - 证据:
    ```python
    if ok and name:
        name = name.strip()
        if not name:
            return
        folder_path = os.path.join(self._current_folder, name)
        ...
        os.makedirs(folder_path, exist_ok=True)
    ...
    if ok and new_name and new_name != old_name:
        topos_path = item.data(Qt.ItemDataRole.UserRole)
        folder = os.path.dirname(topos_path)
        new_topos = os.path.join(folder, f"{new_name}.topos")
        ...
        os.rename(topos_path, new_topos)
    ```
  - 影响: 名称可含 `../`、`/`、空字节等：`create_folder("../../../somewhere")` 在 Presets 之外建目录；`_rename_preset` 用 `"a/b"` 时目标子目录不存在 → `os.rename` 抛未捕获 FileNotFoundError（UI 崩溃/错误弹窗），`"../x"` 则把预设连同 sidecar 移出预设区。违反"外部数据须验证"铁律。
  - 建议: 校验名称仅含安全字符集（拒绝路径分隔符、`..`、控制字符），rename 包 try/except OSError 并回滚已改名的 sidecar（当前改名无事务性，中途失败会留下 .topos 与 sidecar 不同名）。
  - 置信度: 高

- [AUD-25516] [Suggestion] `_get_preset_meta` 用裸 `except Exception: pass` 掩盖读取错误
  - 位置: src/features/color/ui/presets_panel.py:271-279
  - 证据:
    ```python
    try:
        with zipfile.ZipFile(topos_path, 'r') as zf:
            if "project.json" in zf.namelist():
                with zf.open("project.json") as f:
                    data = json.load(f)
    except Exception:
        pass
    ```
  - 影响: 损坏的 zip、坏 JSON、权限错误全部静默为空 dict 并写入缓存（永不再读），指示点/元数据无声缺失且无日志，违反"不得宽泛 except 掩盖领域错误"。
  - 建议: 至少 `except (OSError, zipfile.BadZipFile, json.JSONDecodeError)` 并 logger.warning 记录路径与原因。
  - 置信度: 高

- [AUD-25517] [Suggestion] scan_presets 在 UI 线程同步做逐条磁盘/压缩包 I/O，_export_thumbnail 的 copy2 无异常保护
  - 位置: src/features/color/ui/presets_panel.py:287-330,509-516
  - 证据:
    ```python
    for name in sorted(os.listdir(folder)):
        ...
        meta = load_preset_meta(topos_path)
        item.setData(Qt.ItemDataRole.UserRole + 1, meta)
        ...
        if os.path.exists(thumb_path):
            pixmap = QPixmap(thumb_path)
    ...
    if save_path:
        shutil.copy2(thumb_path, save_path)
    ```
  - 影响: 扫描对每个 .topos 读 sidecar/解包（load_preset_meta）、每个缩略图再解码一次 QPixmap 仅用于 setIcon（delegate 又自行解码，双重成本）；大库时面板构造/刷新冻结 UI。copy2 遇目标只读/磁盘满抛未捕获 OSError。
  - 建议: 扫描结果与图标构建挪到后台线程（QtConcurrent/worker）或至少惰性化；copy2 包 try/except 并提示用户。
  - 置信度: 高（性能缺陷；量级取决于库大小）

- [AUD-25518] [Suggestion] 未使用导入与缺失类型注解
  - 位置: src/features/color/ui/presets_panel.py:1-17,526,666
  - 证据:
    ```python
    import shutil
    import zipfile
    import tempfile
    from datetime import datetime
    ...QApplication, QDockWidget)
    ...QFont, QAction, QIcon)
    ...
    def paint(self, painter, option, index):
    ...
    def sizeHint(self, option, index):
    ```
  - 影响: `tempfile`、`datetime`、`QApplication`、`QModelIndex`、`QPoint`、`QFont`、`QAction` 全文件未使用；delegate 的 `paint`/`sizeHint`/`editorEvent` 等无参数与返回类型注解。
  - 建议: 清理导入；补 `-> None`/`-> QSize` 等注解。
  - 置信度: 高

### 通过项
- 线程安全：所有文件与 UI 操作都在 UI 线程，无跨线程共享状态
- GPU 资源生命周期 / GLSL·HDR：面板不接触 GL 与 HDR 数据（纯文件/列表 UI），无问题
- 信号防递归：`_populate_folder_combo` 全程 blockSignals 后恢复选中；folder_combo 变更走显式 `_on_folder_changed`
- 状态提交一致性：删除/改名/建目录后统一 `scan_presets()` 重建视图，sidecar 随 .topos 同步搬移/清理（`_SIDECAR_SUFFIXES`）
- 删除路径：删除前有 QMessageBox 确认，路径 None/非文件有守卫；`_delete_preset_files` 对每个文件包 OSError
- 搜索过滤：`_on_search_changed` 仅重放过滤不重扫磁盘，条目 hidden 状态在 addItem 后设置（规避视图重置）

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 7 | AUD-25500, AUD-25501, AUD-25508, AUD-25509, AUD-25513, AUD-25514, AUD-25515 |
| Suggestion | 11 | AUD-25502–25507, AUD-25510–25512, AUD-25516–25518 |

跨文件共性问题：交互边界（interaction_started/ended）在四个面板中只有 mixer/plugin-slider/plugin-color 三处做对，mask 全量与 plugin 的 reset/toggle/delete 均缺失，undo 粒度不一致；磁盘 I/O 直接发生在 UI 线程（presets 扫描/绘制、plugin 右键重扫）是第二大主题。
