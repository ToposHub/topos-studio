# batch-107 审查报告 — src/features/composite/ui/node_properties_panel.py

审查范围：`src/features/composite/ui/node_properties_panel.py`（7,683 行，已全文通读）。
编号区间：AUD-10700–AUD-10799。本文件为纯 UI 面板（PyQt6），无 OpenGL 调用、无 GLSL、
无渲染线程交互；GPU/线程维度按"不涉及"处理。跨文件证据（composite_page.py、
base_node.py、drag_value.py）均已实际读取核实。

## src/features/composite/ui/node_properties_panel.py (行数: 7683)

### 发现

- [AUD-10700] [Critical] 多选/pinned 场景下编辑非首个 section 的参数会写入错误的节点
  - 位置: src/features/composite/ui/node_properties_panel.py:3011（同类：2995、3279、3292、4427-4450）；根因在 1887 与 4418-4445
  - 证据:
    ```python
    # 1886-1888 (set_nodes): _current_node_id 只指向第一个选中节点
    if selected_ids:
        first_data = data_by_id.get(selected_ids[0], {})
        self._current_node_id = selected_ids[0]
    ```
    ```python
    # 3010-3011 (控件信号在"用户交互时"才触发，此时 _current_node_id 是面板级首个节点)
    else:
        drag.valueChanged.connect(lambda v, k=key: self._on_param_changed(k, v))
    ```
    ```python
    # 4430-4445 (_on_param_changed 以 _current_node_id 为路由目标)
    if self._current_parameters.get(key) == value:
        return
    self._current_parameters[key] = value
    if self._current_node_id:
        ...
        self.parameter_changed.emit(self._current_node_id, key, value)
    ```
  - 影响: `_create_param_row_for_section` 等仅在"构建期间"临时把 `_current_node_id` 换成
    section 的节点（3657/3673），但控件信号在用户拖动时触发，路由用的是面板级
    `_current_node_id`（=第一个选中节点）。已核实 composite_page.py:3505-3526 会把**全部**
    选中节点的 nodes_data 传给 `set_nodes`，且页面 `_on_parameter_changed`（composite_page.py:6720）
    完全信任 node_id 直接 `set_node_parameter`。后果：多选时编辑第 2..N 个 section（或 pinned
    section 在选中其它节点时）会把修改写到**第一个节点**上（错误节点被改动 → 渲染错误/用户
    数据破坏）；且去重比较用的是第一节点的 `_current_parameters`，第二节点的编辑值若与第一
    节点当前值相同会被静默丢弃。◆ 关键帧按钮（`_toggle_keyframe_at_current_frame`、
    `_autokey_if_animated` 均读 `self._current_node_id`）同样路由到错误节点。
  - 建议: 控件构建时将 node_id 通过默认参数绑定进每个回调（如 `lambda v, k=key, n=node_id:
    self._on_param_changed_for(n, k, v)`），`_on_param_changed` 增加 node_id 参数；或让
    NodeSectionWidget 拥有自己的参数变更发射路径（如同 trim_range 的做法，6937-6940）。
  - 置信度: 高

- [AUD-10701] [Major] `_show_parameter_context_menu` 读取面板上未定义的 `_group_promote_hook`，右键参数名菜单必然抛 AttributeError
  - 位置: src/features/composite/ui/node_properties_panel.py:5370
  - 证据:
    ```python
    # B3-2: "Promote to Group" on the first numeric entry of the row
    # when the page provides a resolver hook.
    if self._group_promote_hook is not None:
    ```
    （`NodePropertiesPanel.__init__` 1570-1645 从未设置该属性；全库 grep 仅 `_ParamLabel`
    326/334-336 定义了同名成员；composite_page.py:6216 用 `hasattr(panel, "set_group_promote_hook")`
    判定后安装，而 `NodePropertiesPanel` 并没有 `set_group_promote_hook` 方法 → 永远不会安装。）
  - 影响: 右键任意数值/XY/XYZ/RGB 参数名标签（contextMenuEvent → builder →
    `_show_parameter_context_menu`）在 menu.exec 之前就抛 `AttributeError`，"编辑表达式/
    分离合并维度"菜单完全不出现（表达式入口仅此一处，功能整体不可用）。`_show_dim_menu`
    （5405）同路径同病。
  - 建议: 在 `NodePropertiesPanel.__init__` 中初始化 `self._group_promote_hook = None`，并补上
    `set_group_promote_hook(self, hook)` 方法使 composite_page.py:6216 的 hasattr 分支生效。
  - 置信度: 高

- [AUD-10702] [Major] `set_nodes` 置位 `_block_signals` 后无 try/finally，构建异常会永久锁死参数变更通路
  - 位置: src/features/composite/ui/node_properties_panel.py:1813、1915
  - 证据:
    ```python
    self._block_signals = True
    ...（中途大量 section 创建/更新/重建，无任何 early-return 保护、无 try/finally）...
    self._block_signals = False
    ```
  - 影响: section 创建/更新链路（`_create_section` → `_build_parameters` → 各 custom builder，
    如 7049 `params["mode"]` 直接下标取值）一旦抛异常，`_block_signals` 停在 True，
    `_on_param_changed`（4420）从此永远 early-return，整个 Properties 面板对所有参数编辑失活，
    直到下一次成功执行到 1915 行。同类方法 `_handle_blur_type_change`（5743-5747）已正确使用
    try/finally，本处遗漏。
  - 建议: 将 1813-1915 主体包入 `try: ... finally: self._block_signals = False`。
  - 置信度: 高（结构性缺陷）；触发概率中（需构建期异常，如旧工程 Constant 缺 "mode"）。

- [AUD-10703] [Major] `*_for_section` 构建器的"上下文换入/换出"无异常保护，失败后面板上下文指向错误节点
  - 位置: src/features/composite/ui/node_properties_panel.py:3649-3678（同型：3692-3719、3730-3757、3766-3792）
  - 证据:
    ```python
    saved_node_id = self._current_node_id
    ...
    self._current_node_id = node_id
    self._current_parameters = section.parameters
    self._param_widgets = {}
    row = self._create_param_row(key, value, ...)   # 异常点：无保护
    ...
    self._current_node_id = saved_node_id            # 异常时不会执行
    ```
  - 影响: 行构建中途抛异常（畸形参数值、pdef 缺失导致 `float(val)` 失败等）时，
    `_current_node_id/_param_defs/_param_options/_param_widgets` 停留在 section 的临时上下文，
    后续所有参数编辑按错误节点路由（叠加 AUD-10700 的后果），且面板级 `_param_widgets` 变成
    残缺的 `{}`。
  - 建议: 用 `try/finally` 保证上下文恢复；或改为显式传参（node_id、param_defs）而非交换实例状态。
  - 置信度: 高（结构）；触发中。

- [AUD-10704] [Major] `_update_section_param_value` 缺少 file/hdri/channel/vec2/vec3/text/curve/color(无 update) 分支，外部更新对多类控件静默失效
  - 位置: src/features/composite/ui/node_properties_panel.py:3794-3891；注册点 2586、2593、2599、2936、3232、3554、6568
  - 证据:
    ```python
    # 分支仅有：combo / bool / float,int / checker / str / color(仅当有 'update') /
    # custom / trim_range；'file' 行注册为：
    self._param_widgets[key] = {'type': 'file', 'widget': file_picker}   # 2593
    # 'text' 行注册为（无分支读取其 'update'）：
    section._param_widgets["text"] = {'type': 'text', 'widget': editor,
                                      'update': lambda t: ...}           # 6568-6572
    # 通用 color 按钮注册时不带 'update'：
    self._param_widgets[key] = {'type': 'color', 'widget': color_btn}    # 2586
    ```
  - 影响: `update_parameters` 增量路径只调 `_update_section_param_value`；键集不变时（undo/redo、
    页面回写、vec/color 参数的播放头采样同步 `sync_animated_values`→5226）这些类型的控件不会
    被刷新——Text 内容、file_path、HDRI 选择、vec2/vec3、通道选择、通用 hex 色块在撤销后显示
    旧值，面板与图状态失一致。
  - 建议: 补齐缺失分支（file→findChild(QLineEdit).setText、text→调用注册的 update 回调、
    vec2/vec3→遍历 spinbox、color→无 update 时直接 setStyleSheet/setText），或将注册统一改为
    携带 `update` 可调用对象并在这里统一调用。
  - 置信度: 高（机制确凿）；具体影响面中。

- [AUD-10705] [Major] 已删除节点的 pinned section 与 `_pinned_node_ids`/`_split_dims`/`_scene3d_toggle_buttons` 永不清理
  - 位置: src/features/composite/ui/node_properties_panel.py:1819-1821、1600、1605、6749-6751；清理路径 1827-1832 只删不在 visible_ids 的 section
  - 证据:
    ```python
    visible_ids = list(selected_ids)  # preserve selection order
    for pid in self._pinned_node_ids:
        if pid not in visible_ids:
            visible_ids.append(pid)          # 已删节点的 pinned id 仍被加回
    ...
    data = data_by_id.get(nid)               # 已删节点 → None → section 不更新但也不移除
    ```
  - 影响: 用户 pin 一个节点后将其从图中删除：pinned id 永远存在于 `_pinned_node_ids`，
    其 section 因在 visible_ids 中而从不进入 `to_remove`，面板长期挂着已删节点的僵尸 section
    （其编辑还会经 AUD-10700 路由到当前节点）。`_split_dims`、`_scene3d_toggle_buttons`
    （2008-2021 遍历时仅 skip 不删除，持有已销毁按钮引用）同样只增不减。
  - 建议: 提供 `remove_node(node_id)` 公共 API 供页面在 node-removed 时调用，统一清
    `_sections/_pinned_node_ids/_split_dims/_image_sizes/_scene3d_toggle_buttons`；
    `set_nodes` 中对 `data is None 且 nid 不在本次选中集` 的 pinned id 予以剔除。
  - 置信度: 中（本文件内确无任何清理路径；不排除页面整体重建面板，但未见证据）

- [AUD-10706] [Major] `sync_animated_values` 把播放头采样值写进 `section.parameters`/`_current_parameters`，预设/重建会固化"当前帧值"而非基准参数
  - 位置: src/features/composite/ui/node_properties_panel.py:5222-5225；消费点 `_collect_current_values` 1431-1438、`_on_save_preset` 1476-1481
  - 证据:
    ```python
    if param_name in section.parameters:
        section.parameters[param_name] = value
    if self._current_node_id == node_id:
        self._current_parameters[param_name] = value
    self._update_section_param_value(param_name, value, param_widgets)
    ```
    （`value = track.get_value_float(float(frame), default=base)`，5215-5216；而
    `_collect_current_values` 以 `values = dict(self.parameters)` 起步，1438）
  - 影响: 播放头移动后保存节点预设（_on_save_preset）会把"当前播放帧的动画插值"当作静态
    参数写进 preset；此后任何以 `section.parameters` 为源的重建（搜索过滤、模式切换重建）
    也以采样值为初值。节点真实 base 参数虽未被污染，但 UI 侧快照与预设数据失真。
  - 建议: 采样值只写控件显示（`_update_section_param_value`），不回写
    `section.parameters/_current_parameters`；`_collect_current_values` 改从 `_get_live_node`
    的真实 parameters 读取。
  - 置信度: 高（机制）；影响中。

- [AUD-10707] [Major] 颜色写入的 sRGB/线性边界不一致：RGB 三合一行与 Keyer 直接写显示值，Constant 路径则做一次 sRGB→linear 转换
  - 位置: src/features/composite/ui/node_properties_panel.py:3521-3529、6061-6064（写显示值）vs 7579-7588（转换后再写）
  - 证据:
    ```python
    # _create_rgb_param_row: 直接把 QColor 的 8bit 显示码写进参数
    self._on_param_changed(r_key, color.red() / 255.0)
    self._on_param_changed(g_key, color.green() / 255.0)
    self._on_param_changed(b_key, color.blue() / 255.0)
    ```
    ```python
    # _create_constant_color_button: 注释明确"UI-to-graph 边界恰好转换一次"
    # QColor/Color Inspector values are sRGB display codes.  Constant
    # stores scene-linear channels, so convert exactly once ...
    r_linear, g_linear, b_linear, a = qcolor_to_linear_rgba(color)
    ```
  - 影响: 同一面板内两套约定并存。若 `*_color_r/g/b`（如灯光/填充色）按线性语义参与 HDR
    合成，直接写 sRGB 码会使颜色整体偏暗（gamma 2.2 量级）；Keyer screen_color 同样写入
    `color.redF()` 显示值。属于 HDR 边界正确性风险（本文件无 GLSL，但污染上游参数）。
  - 建议: 统一在 UI→graph 边界做一次 `qcolor_to_linear_rgba`（或按各参数声明空间显式标注），
    RGB 行与 Keyer 行与 Constant 行对齐。
  - 置信度: 中（文件内不一致客观成立；各参数期望空间需节点侧确认）

- [AUD-10708] [Suggestion] 死代码簇：无人调用的 `_update_param_widget_value`（且 curve 分支会在容器控件上调用不存在的 setCurve）、永不赋值的 legacy 多选状态、永不发射的 `node_reset_requested` 连接、`_ParamLabel.set_group_promote_hook`
  - 位置: src/features/composite/ui/node_properties_panel.py:4265-4379、1592-1596、1950-1952 与 746、326-336
  - 证据:
    ```python
    # 4265 定义 _update_param_widget_value —— 全库 grep 无任何调用者；
    # 其 curve 分支（4356-4363）拿到的其实是 _create_curve_control 返回的容器 QWidget：
    self._param_widgets[key] = {'type': 'curve', 'widget': curve_widget}  # 2630（容器）
    curve_widget.blockSignals(True); curve_widget.setCurve(curve_data)    # 4361-4362（容器无 setCurve）
    ```
    ```python
    # 1592-1596: _current_node_ids/_is_multi_selection/_param_values_per_node 仅初始化，从未赋值
    # 1950-1952: section.node_reset_requested.connect(...) —— NodeSectionWidget 从未 emit 该信号
    #            （_on_reset 1396-1403 改为逐参数 emit parameter_changed）
    ```
  - 影响: 约 250 行不可达代码（含 `_compute_common_parameters`/`_create_multi_selection_params_group`
    2040-2397 的主体依赖这些死状态）；curve 容器误注册属潜伏崩溃点，一旦有人调用即 AttributeError。
  - 建议: 删除或在 `_update_section_param_value` 中收编；curve 注册改存 CurveWidget 本体并给
    `_update_section_param_value` 加 curve 分支（与 AUD-10704 一并处理）。
  - 置信度: 高

- [AUD-10709] [Suggestion] 搜索框每个按键全量重建所有 section；custom builder 节点完全不响应搜索过滤
  - 位置: src/features/composite/ui/node_properties_panel.py:2491-2496、1337-1340、1081-1117
  - 证据:
    ```python
    def _on_search_changed(self, text: str):
        self._search_filter = text
        for section in self._sections.values():
            section.set_search_filter(text)      # → _build_parameters() 全量重建
    ```
  - 影响: 每敲一个字符，所有 section 的全部控件被销毁重建（camera3d/text 等大 section 卡顿）；
    且 constant/blur/keyer/text/output/scene3d/camera3d/video_frame/tracker 走 custom builder
    分支（1081-1117）直接 return，搜索框对这些节点形同虚设，行为不一致。
  - 建议: 引入 QTimer 防抖（如 200ms）；custom builder 至少在构建前用 `matches_search` 过滤
    参数键，或禁用这些节点的搜索并给 UI 提示。
  - 置信度: 高

- [AUD-10710] [Suggestion] int 型参数全程被写入 float 值
  - 位置: src/features/composite/ui/node_properties_panel.py:3011、3279、3292（`_on_param_changed(k, v)` 直接透传）；src/shared/ui/widgets/drag_value.py:80（`valueChanged = pyqtSignal(float)`）
  - 证据:
    ```python
    # drag_value.py:80
    valueChanged = pyqtSignal(float)
    # 面板数值行（含 int 行，decimals=0）：
    drag.valueChanged.connect(lambda v, k=key: self._on_param_changed(k, v))
    ```
  - 影响: width/height/font_size 等 int 参数在 parameters 中变成 1920.0 之类的 float，
    序列化体积与严格类型校验（ptype=="int" 的 keyframe `_normalize_keyframe_value` 之外）
    存在不一致风险；XY 链动 `push_to_peer` 仅在 `isinstance(peer_value, int)` 时转 int，
    而 DragValue 恒发 float，该分支实际永不命中。
  - 建议: 在 `_on_param_changed` 前按 `_numeric_meta` 的 is_int 将值 `int(round(v))`，
    或让 DragValue 在 decimals==0 时发射 int。
  - 置信度: 高（信号类型已核实）；影响低

- [AUD-10711] [Suggestion] CurveWidget 拖动约束在相邻点间距 <0.002 时失效，可产生乱序控制点
  - 位置: src/features/composite/ui/node_properties_panel.py:626-632
  - 证据:
    ```python
    min_x = 0.0 if self._dragging_index == 0 else self._points[self._dragging_index - 1][0] + 0.001
    max_x = 1.0 if self._dragging_index == len(self._points) - 1 else self._points[self._dragging_index + 1][0] - 0.001
    new_x = max(min_x, min(max_x, new_point[0]))
    ```
  - 影响: 相邻两点 x 距离 ≤0.002 时 min_x > max_x，`max(...)` 取 min_x 可越过右邻，破坏
    `_points` 按 x 有序的不变量，绘制与插值出现折返；另外 `mousePressEvent` 未调用
    `event.accept()`，且按下非命中区域时也立即置 `_dragging_index`（双击删除前同一位置会先
    插入一个点，双重命中）。
  - 建议: `new_x = max(min_x, min(max_x, x))` 后再 `new_x = min(new_x, max_x)`（或先计算
    `hi = max(min_x, max_x)`）；插入点时同样应用 0.001 间隙约束。
  - 置信度: 中

- [AUD-10712] [Suggestion] 节点颜色实时预览取消后不回滚，且每个 live tick 都发 `node_color_changed`
  - 位置: src/features/composite/ui/node_properties_panel.py:1381-1394
  - 证据:
    ```python
    def _on_live_header(color: QColor):
        hex_color = color.name()
        self.node_color = hex_color
        self._update_header_style(); self._update_color_indicator_style()
        self.node_color_changed.emit(self.node_id, hex_color)   # live 期间持续入图
    result = get_color(parent=self, initial=current, on_live=_on_live)
    if result is not None: ...                                   # 取消时无回滚
    ```
  - 影响: 用户拖动选色后按 Esc 取消：节点颜色停留在最后一次 live 值（与参数实时预览的
    "取消即还原"预期不符），并可能留下多次 live 发射产生的多条颜色记录。
  - 建议: 在打开选择器前保存原色，`result is None`（取消）时回写原色并 emit 一次还原。
  - 置信度: 中

- [AUD-10713] [Suggestion] `_on_save_preset` 对 ImportError 静默返回，按钮无任何反馈
  - 位置: src/features/composite/ui/node_properties_panel.py:1462-1467
  - 证据:
    ```python
    try:
        from PyQt6.QtWidgets import QInputDialog, QMessageBox
        from src.features.composite.utils.preset_manager import PresetManager
    except ImportError:
        return
    ```
  - 影响: 依赖缺失时点击 Save Preset 毫无反应（无对话框、无日志），用户无从判断失败原因。
    （保存失败路径 1484-1489 有 QMessageBox，此处不对称。）
  - 建议: ImportError 分支至少弹提示或记日志。
  - 置信度: 高

- [AUD-10714] [Suggestion] `_build_constant_ui` 直接下标 `params["mode"]`，旧数据缺键即 KeyError
  - 位置: src/features/composite/ui/node_properties_panel.py:7049（对照 7002 的 `.get("mode","solid")`）
  - 证据:
    ```python
    mode = section.parameters.get("mode", "solid")   # 7002：有默认
    ...
    mode_row = self._create_param_row_for_section(
        section.node_id, "mode", params["mode"],     # 7049：直接下标
    ```
  - 影响: 旧版本工程/手工 JSON 缺 "mode" 键时，选中 Constant 节点即 KeyError（并经 AUD-10702
    把 `_block_signals` 卡死）。违反"外部数据须验证"红线（严重度取决于数据来源可靠性）。
  - 建议: 与 7002 一致使用 `params.get("mode", "solid")`。
  - 置信度: 高

- [AUD-10715] [Suggestion] 类型注解失实与缺失：`_param_widgets: Dict[str, QWidget]` 实存 dict；大量槽函数/构建器无注解
  - 位置: src/features/composite/ui/node_properties_panel.py:1588（以及 1050、1180、3338、5749、5928、6164、6902、6997 等构建器仅部分注解；`mousePressEvent/paintEvent` 等无返回注解）
  - 证据:
    ```python
    self._param_widgets: Dict[str, QWidget] = {}  # param_name -> widget
    # 实际存入：{'type': 'combo', 'widget': combo, 'options': [...], ...}（2160、2220、2864 等）
    ```
  - 影响: 注解与真实结构不符误导调用方与静态检查；与 .clinerules 强类型要求不符。
  - 建议: 改为 `Dict[str, Dict[str, Any]]`；为公共方法与事件处理补齐注解（事件处理器标注
    `-> None`）。
  - 置信度: 高

### 通过项

- 线程安全：文件为纯 UI 线程 QWidget 代码，无 OpenGL/渲染线程直接调用，无跨线程信号；未发现 UI 线程操作 GPU 资源。
- GPU 资源生命周期：本文件不创建/持有任何 ModernGL 对象（无纹理/FBO/VAO），不涉及 deferred-release。
- GLSL/HDR 正确性：无着色器代码；swatch 显示侧 clamp(0,1) 属显示域合理使用（唯一 HDR 边界问题见 AUD-10707）。
- 信号防递归（常规路径）：`set_enabled`/`set_camera_active`/`set_view_mode`/`TrimRangeEditor.set_values`/`_update_section_param_value`/`_refresh_*_combo` 等程序化更新均正确 blockSignals；`_handle_*_change` 重建均带 `_block_signals` try/finally（set_nodes 一处遗漏见 AUD-10702）。
- 控件销毁与 teardown：`prepare_for_deletion` 幂等保护 + `_is_torn_down` 守卫，`update_parameters/set_enabled/set_node_color/set_read_only` 均先查 torn-down；`_refresh_text_size_combo` 对已删 C++ 对象 RuntimeError 有防御；eyedropper 以实例属性防 GC。逻辑正确。
- 异常与取消路径（局部）：`_build_video_frame_ui`/`_build_tracker_ui` 对畸形参数有 try/except + 默认值；`_autokey_if_animated`/`_toggle_keyframe_at_current_frame` 对 `_normalize_keyframe_value` 的 TypeError/ValueError 有捕获；表达式编辑器提交前先 validate（结构性遗漏见 AUD-10702/10703）。
- 输入验证（局部）：`_build_video_frame_ui` 对 total_frames/source_in/source_out 全部 int() 包 try；`_normalize_keyframe_value` 对向量维度/类型严格校验；HDRI 扫描目录有 try/except。
- 取消路径：关键帧编辑器改为非模态 + OK 原子提交（不连 keyframes_changed，Cancel 不写图），表达式"清除"显式 emit None，逻辑正确。
- undo/revision 协作：`_apply_keyframes_to_node` 写前 deepcopy before 快照供页面 push undo；参数去重避免冗余信号（错误节点场景除外，见 AUD-10700）。
- `group_xy_pairs` 纯函数的配对/三合一/RGB 分组边界（连续键、同 group、同 float ptype）逻辑正确，i+2<n 越界保护正确。
