# batch-104 审查报告（src/features/composite/ui//）

批次：batch-104 [P1]；编号区间 AUD-10400–AUD-10415；日期 2026-09-28。
审查文件：`src/features/composite/ui/node_editor_actions.py`（1,508 行，全文通读）。
交叉验证：`node_editor_widget.py`、`composite_page.py`、`node_engine.py`、`base_node.py`、`scene3d_node.py`、`dot_node.py`、`graph_migration.py`、`group_commands.py`、`preset_manager.py`、`port_item.py`（只读核对信号契约与模型行为）。

## src/features/composite/ui/node_editor_actions.py (行数: 1508)

### 发现

- [AUD-10400] [Critical] 使用未定义的 `logger`，粘贴 Topos Color 节点时抛 NameError 使粘贴中断/崩溃
  - 位置: src/features/composite/ui/node_editor_actions.py:796
  - 证据:
    ```python
    logger.info(
        f"[Phase6] Topos Color copied: source={source_stack_id}, "
        f"target={new_stack_id}"
    )
    ```
    全文件仅有 `from typing import ...`、`import uuid`、`import copy`（第 25–29 行），无 `import logging`，也无任何 `logger =` 赋值；grep 确认 `logger` 仅在 796 行出现一次。
  - 影响: `paste_nodes` 是 Ctrl+V 主路径（node_editor_widget.py:2021/3593–3596 直接委托本方法）。当处于 clip 上下文（`target_clip_id` 非空，即正常运行中的合成页）粘贴复制的 topos_color 节点时，必然触发 `NameError`；Qt 槽内未捕获异常（PyQt6 默认 qFatal）可直接致应用崩溃，且粘贴进行到一半（前面的节点已入图、后续连接未建）造成图状态不一致。
  - 建议: 模块顶部 `logger = logging.getLogger(__name__)`，或改用已存在的日志设施；同时为 paste 主体加异常保护，失败时回滚已创建节点。
  - 置信度: 高

- [AUD-10401] [Major] `connect()` 双重提交：先改模型再发信号，页面处理器再次 connect，多输入端口产生重复连接与重复视觉线
  - 位置: src/features/composite/ui/node_editor_actions.py:490-509
  - 证据:
    ```python
    # Create in graph model
    success = self._graph.connect(src_node_id, src_port, dst_node_id, dst_port)
    if not success:
        return False
    ...
    self._editor.add_connection_visual(
        src_node_id, src_port, dst_node_id, dst_port, data_type
    )
    self._editor.node_connected.emit(src_node_id, src_port, dst_node_id, dst_port)
    ```
    而 composite_page.py:2302 的 `_on_nodes_connected` 会再次执行 `self._node_graph.connect(...)` 并再次 `add_connection_visual(...)`（2326 行注释明确"NodeEditorWidget no longer does this"——视觉/模型同步是信号接收方的职责）。node_engine.py:1194-1209 的 `NodeGraph.connect` 不做精确去重，多输入端口（scene3d_node.py:140 `Port("in", "input", "scene", max_connections=8)`）直接 append。
  - 影响: 拖拽 3D 模型（widget dropEvent → `create_3d_scene_with_children` → `connect(Model3D.display → Scene3D.in)`）、粘贴/加载含多输入连接的预设时，模型 `connections` 中出现完全重复的边（保存后持久化），画布出现叠加的重复连线；当端口恰好被第一次 connect 填满时，页面第二次 connect 返回 False 走 early-return（composite_page.py:2303-2304），跳过 `_push_connect_undo` 与 `_evaluate_graph()` → 该连接无 undo 记录且渲染不刷新。
  - 建议: `NodeEditorActions.connect` 只做视觉前置去重检查后仅 emit `node_connected`，模型与视觉提交统一交给页面处理器；或 `NodeGraph.connect` 增加精确重复检查。
  - 置信度: 高

- [AUD-10402] [Major] `disconnect()`/`delete_selected_connections()` 自行移除视觉后又发信号，页面按 dst 再删一条，多输入端口误删无辜连线视觉
  - 位置: src/features/composite/ui/node_editor_actions.py:529-536（另见 346-354）
  - 证据:
    ```python
    for conn_data in list(self._editor._connection_items):
        conn_item, sn, sp, dn, dp = conn_data
        if sn == src_node_id and sp == src_port and dn == dst_node_id and dp == dst_port:
            self._editor._remove_connection_item(conn_item)
            self._editor.node_disconnected.emit(src_node_id, src_port, dst_node_id, dst_port)
    ```
    composite_page.py:2324-2326 的处理器随后执行 `remove_connection_visual_by_dst(dst_node_id, dst_port)`——按 (dst_node, dst_port) 删除第一条匹配视觉，不校验 src。
  - 影响: 对多输入端口（如 Scene3D.in，max=8，多摄影机/灯光/模型共接）断开某一源时：本方法的视觉已删，页面又删掉"第一条匹配 dst"的视觉——即另一条无辜连线的视觉被移除，而其模型连接仍在（幽灵边，保存重载后重现、用户无法再选中该线缆）。`delete_selected_connections`（346-354 行）对同一多输入端口选中多条线时级联误删并对已移除 item 重复 `decrement_connection`（port_item.py:90 有 0 下限钳制，不会负数，但会把仍存活连线的端口计数归零、显示为未连接态）。
  - 建议: 遵循信号契约——只 emit `node_disconnected`，视觉删除交给页面；并推动页面处理器改为按 (src,dst) 四元组精确删除视觉。
  - 置信度: 高（机制；实际误删需多输入端口场景，Scene3D 为主路径）

- [AUD-10403] [Major] 删除节点前先 `_reconnect_around_node` 断开全部连接，导致 `_push_remove_undo` 捕获空连接列表，单步 undo 恢复的节点丢失全部连线
  - 位置: src/features/composite/ui/node_editor_actions.py:226-229（另见 324-331）
  - 证据:
    ```python
    # Reconnect chain before removing the node
    self._reconnect_around_node(node_id)

    self._editor.remove_node_visual(node_id)
    self._editor.node_removed.emit(node_id)
    ```
    `_reconnect_around_node`（283-290 行）先连 bypass 边再逐一 `disconnect` 原边（每条都触发页面 `_push_connect_undo`/`_push_disconnect_undo`）；之后页面 `_on_node_removed`（composite_page.py:2526-2528）才调用 `_push_remove_undo`，其 7178-7185 行在此时读取 `get_connections_for_node(node_id)`——已经为空。
  - 影响: Delete 键主路径（widget:2135 → `delete_selected_nodes`；单删同理）下，Ctrl+Z 一次仅恢复"裸节点"（`_undo_remove_node` 用空 conns 重建，composite_page.py:7194-7207），原图连线需再按多次 undo 逐条恢复，中间态图形破损；redo 语义同样错乱。对比 `cut_selected_nodes`（不经 `_reconnect_around_node`）捕获的 conns 完整，两路径行为不一致。
  - 建议: 删除前先捕获连接快照（或先 emit `node_about_to_be_removed` 让页面在断边前完成捕获，页面 2345 行注释显示 Layer 路径正是用快照解决同类问题）；或把 bypass 重连 + 删除合并为一个成组 undo 事务。
  - 置信度: 高

- [AUD-10404] [Major] `_rename_node_safe` 写入 `node._id`，但 `Node.id` 是普通属性，重命名实际未生效且 `_node_items` 未重键，图/序列化/视觉全面脱节
  - 位置: src/features/composite/ui/node_editor_actions.py:177-193
  - 证据:
    ```python
    nodes_map = self._graph.nodes
    nodes_map[new_id] = nodes_map.pop(old_id)
    node._id = new_id  # type: ignore[attr-defined]
    # 更新涉及该节点的连接（src/dst）
    for conn in self._graph.connections:
        if conn.src_node_id == old_id:
            conn.src_node_id = new_id
    ```
    base_node.py:411 为 `self.id: str = node_id or str(uuid.uuid4())[:8]`——不存在 `_id` 属性；`to_dict`（base_node.py:1186）序列化 `"id": self.id`。
  - 影响: 传入 `base_id` 时：graph 字典 key 变为 new_id，但 `node.id` 仍为 old_id（`_id` 是死属性）→ `to_dict` 保存旧 id 而连接已改指 new_id → 保存文件出现悬空连接（重载数据损坏）；`_node_items` 仍按 old_id 键（widget:496），后续 `connect`/`add_connection_visual` 用 new_id 查不到 item 静默失败（无边视觉）。当前唯一调用点 widget:992 未传 base_id，属潜伏缺陷，一旦启用即触发。
  - 建议: 删除节点不应改名；确需重命名时提供 `NodeGraph.rename_node()` 统一改 `node.id`、字典 key、`_node_items`、锁定集合与所有引用，并考虑禁止对已入图节点改 id。
  - 置信度: 高（缺陷确定；当前无调用方传 base_id，触发为潜伏）

- [AUD-10405] [Major] `save_selected_as_preset` 借道 `copy_selected_nodes` 覆写编辑器剪贴板，保存预设后 Ctrl+V 粘贴出预设源节点
  - 位置: src/features/composite/ui/node_editor_actions.py:1338（副作用在 699）
  - 证据:
    ```python
    # 先复制选中节点（获取节点数据）
    nodes_data = self.copy_selected_nodes()
    ```
    而 `copy_selected_nodes` 末尾（699 行）: `self._editor._clipboard = clipboard`
  - 影响: 用户复制节点 A 后，对节点 B 执行"保存为预设"，剪贴板被静默替换为 B；随后 Ctrl+V 粘贴出 B 而非 A，用户数据流被意外注入节点（若 B 为 topos_color 还叠加 AUD-10400 崩溃路径）。
  - 建议: 抽取无副作用的序列化函数（只产出 nodes/connections dict 不写 `_clipboard`），`save_selected_as_preset` 与 `copy_selected_nodes` 各自调用。
  - 置信度: 高

- [AUD-10406] [Major] 粘贴 topos_color 生成的 `new_stack_id` 从未真正创建对应 color stack，节点引用不存在的调色栈
  - 位置: src/features/composite/ui/node_editor_actions.py:783-791
  - 证据:
    ```python
    # Create new color_stack for target if needed
    # Note: This is a simplified implementation
    # Full implementation would copy parameters from source_stack
    new_stack_id = f"{target_clip_id}_grade_{source_stack_id[:8]}"

    # Mark parameters
    params["stack_source"] = "preset_copy"
    params["color_stack_id"] = new_stack_id  # New local stack
    ```
    本方法及 774-817 行整段没有任何向 project/timeline 注册新 stack 的调用（776-781 行的 timeline 查找循环是空转死代码：`timeline = None` 后 break，从未使用）。
  - 影响: 粘贴出的 topos_color 节点 `color_stack_id` 指向不存在的栈：原节点调色不会被复制，新节点渲染回落/丢失 grade，用户以为复制了调色实际数据丢失。
  - 建议: 实现真正的栈复制（从 `source_color_stack_id` 深拷贝参数到新 stack 并注册到 project），或复制期保持引用源栈并明确标注；同时删除 776-781 死代码。
  - 置信度: 高

- [AUD-10407] [Major] `create_3d_scene_with_children` 每个子节点 emit `node_added`，页面逐个推 undo 并整图求值，一次拖拽产生 N 步 undo
  - 位置: src/features/composite/ui/node_editor_actions.py:156-174
  - 证据:
    ```python
    self._editor._batch_mode = True
    try:
        for cn in child_nodes:
            ...
            self._graph.add_node(child)
            self._editor.add_node_visual(child)
            self._editor.node_added.emit(child.id, cn["type"])
    ```
    `_batch_mode` 只抑制 `graph_changed`（509 行），不抑制 `node_added`；页面 `_on_node_added`（composite_page.py:2544-2559）对每次 emit 执行 `_push_add_undo(node)` + `_evaluate_graph()`，且无 begin_group。
  - 影响: 拖入一个 3D 模型（Scene3D+Model3D）或含多子节点时，undo 栈出现多条独立"add node"记录：Ctrl+Z 一次只删一个节点，需多次撤销且中间态残缺；每次 emit 还触发一次整图求值（渲染浪费）。
  - 建议: 仿照 `paste_nodes` 的 `nodes_pasted` 模式：批量建子节点后一次性 emit 汇总信号（如复用 `nodes_pasted`/新增 `nodes_batch_added`），由页面 begin_group 成组推 undo、末尾统一 `_evaluate_graph`。
  - 置信度: 高

- [AUD-10408] [Major] 删除保护判定不一致：单删按 `node_type`，多删/剪切/复制按可变的 `node_title`，重命名可绕过 Output/COLOR 保护
  - 位置: src/features/composite/ui/node_editor_actions.py:215（对照 308、856、886）
  - 证据:
    ```python
    # Cannot delete COLOR or Output nodes
    if node.node_type in ("color_input", "output"):
        return False
    ```
    而 `delete_selected_nodes`/`cut_selected_nodes`/`duplicate_selected_nodes` 用 `item.node_title not in ("COLOR", "Output")`（308/856/886 行）；node_item.py:180 `self.node_title = new_title` 表明标题可被用户重命名。
  - 影响: 用户把 Output/COLOR 节点重命名（B4 画布重命名功能）后，框选删除/剪切/复制会绕过保护：Output 节点被删除导致整图无输出（渲染链断裂、保存的工程缺输出节点）。
  - 建议: 全部统一按 `node.node_type`（向 graph 查询）判定保护节点；标题仅用于显示。
  - 置信度: 高（机制确定；触发需用户重命名保护节点，标中）

- [AUD-10409] [Major] `insert_dot_on_connection` 先断开原连接，后续 connect 失败不回滚，链路断裂且 Dot 悬挂
  - 位置: src/features/composite/ui/node_editor_actions.py:621-642
  - 证据:
    ```python
    # Disconnect the original connection
    self.disconnect(old_src_node, old_src_port, old_dst_node, old_dst_port)

    dot_node = self.add_node("dot", insert_x, insert_y)
    if not dot_node:
        # Restore original connection if Dot creation failed
        self.connect(old_src_node, old_src_port, old_dst_node, old_dst_port)
        return None

    ...
    self.connect(actual_src_node, actual_src_port, dot_node.id, "input")
    ...
    self.connect(dot_node.id, "output", actual_dst_node, actual_dst_port)
    ```
    回滚只覆盖"Dot 创建失败"一种情况；两处 `connect` 的返回值被忽略。DotNode 端口固定为 `"image"` 类型（dot_node.py:38-41）。
  - 影响: 对非 image 类型线缆（如 scene 类型的 Camera3D→Scene3D.in，插入点点击入口 widget:3188/4518 均可触达）插入 Dot 时，`connect(src→dot.input)` 因类型不匹配返回 False 且无提示：原边已断、Dot 孤立、下游断流——渲染链路损坏（数据流丢失）。
  - 建议: 捕获两次 connect 的返回值，任一失败则删除已建 Dot 并重连原边（复用现有恢复逻辑）；或插入前校验线缆 data_type 与 Dot 端口兼容。
  - 置信度: 高

- [AUD-10410] [Major] `load_preset` 对外部预设 JSON 的 `position` 形状未验证，畸形数据触发 IndexError/TypeError
  - 位置: src/features/composite/ui/node_editor_actions.py:1388-1389、1412-1416
  - 证据:
    ```python
    # 计算模板中的参考点（左上角）
    ref_x = min(n.get("position", [0, 0])[0] for n in nodes_template)
    ref_y = min(n.get("position", [0, 0])[1] for n in nodes_template)
    ...
    orig_pos = node_data.get("position", [0, 0])
    ```
    `PresetManager._validate_preset`（preset_manager.py:100-105）仅校验 nodes/connections 是 list，不校验元素结构；预设为磁盘 JSON（可手改/第三方导入）。
  - 影响: `position: []`、`position: 100`（标量）或单元素列表时抛 IndexError/TypeError，槽内未捕获异常直接中断加载（PyQt6 下可致崩溃），违反"外部数据须验证"铁律。
  - 建议: 解析前逐节点校验 `position` 为长度≥2 的数值列表，非法则回退 `[0, 0]` 并告警；对 `type`/`parameters` 同样做形状校验。
  - 置信度: 中（触发需畸形外部文件；代码路径确定）

- [AUD-10411] [Suggestion] `copy_selected_nodes` 索引映射与 `nodes_data` 脱同步（被跳过节点仍占索引）
  - 位置: src/features/composite/ui/node_editor_actions.py:670-681
  - 证据:
    ```python
    for i, (nid, item) in enumerate(selected):
        node = self._graph.get_node(nid)
        if not node:
            continue
        data = node.to_dict()
        ...
        nodes_data.append(data)
        id_to_idx[nid] = i
    ```
  - 影响: 若某个选中 item 在 graph 中无对应节点（模型/视觉短暂失配），`i` 仍自增：`nodes_data` 实际下标与 `id_to_idx` 记录值错位，连接按错误索引记录，粘贴/存预设后内部连接静默丢失。
  - 建议: 用 `len(nodes_data)` 或独立计数器作为索引，保证 `id_to_idx` 值与 `nodes_data` 下标一致。
  - 置信度: 高（逻辑缺陷确定；触发需失配状态，概率低）

- [AUD-10412] [Suggestion] `paste_nodes` 位置偏移含魔法数 `-50` 且 x/y 不对称
  - 位置: src/features/composite/ui/node_editor_actions.py:744-747
  - 证据:
    ```python
    data_copy["position"] = [
        center.x() + data["position"][0] + offset_x - 50,
        center.y() + data["position"][1] + offset_y,
    ]
    ```
  - 影响: 与 docstring"offset from reference position"不符：x 方向恒左移 50px，y 不移；连续粘贴的堆叠偏移不均，用户需手动对位。
  - 建议: 移除 `-50` 或将其并入默认 `offset_x`，x/y 对称处理。
  - 置信度: 高

- [AUD-10413] [Suggestion] `_batch_mode` 直接置位/复位不平铺（嵌套被提前关闭），且 `paste_nodes`/`load_preset` 未用批量模式致 O(N²) 刷新与逐连接求值
  - 位置: src/features/composite/ui/node_editor_actions.py:154-173、271、735-835
  - 证据:
    ```python
    self._editor._batch_mode = True
    try:
        ...
    finally:
        self._editor._batch_mode = False
    ```
    `paste_nodes`（735-835）逐节点 `add_node_visual`（widget:503-507 每次全量 `_refresh_all_backdrop_containment()` + `_apply_preview_mode()`）且逐连接 `connect`（每次触发页面 `_evaluate_graph`）。
  - 影响: 若外层已处于批量模式，finally 会提前关闭之；大预设粘贴时背景板包含关系全量刷新 O(N²)、每条连接一次整图求值，4K 实时场景下明显卡顿。
  - 建议: 进入时保存旧值、退出恢复旧值；`paste_nodes`/`load_preset` 包裹批量模式并复用 AUD-10407 的成组信号。
  - 置信度: 高

- [AUD-10414] [Suggestion] `get_preset_manager` 返回注解 `"PresetManager"` 在模块级不可解析（未在 TYPE_CHECKING 导入）
  - 位置: src/features/composite/ui/node_editor_actions.py:1453
  - 证据:
    ```python
    def get_preset_manager(self) -> "PresetManager":
    ```
    `PresetManager` 仅在函数体内局部 import（1459 行），模块 TYPE_CHECKING 块（31-35 行）未包含。
  - 影响: 运行时因 `from __future__ import annotations` 不报错，但静态类型检查无法解析该前向引用，注解形同虚设。
  - 建议: 在 TYPE_CHECKING 块补充 `from src.features.composite.utils.preset_manager import PresetManager`。
  - 置信度: 高

- [AUD-10415] [Suggestion] `toggle_bypass`/`set_bypass` 返回值语义二义（False 既表示未找到也表示新状态为启用）
  - 位置: src/features/composite/ui/node_editor_actions.py:932-935
  - 证据:
    ```python
    item = self._editor._node_items.get(node_id)
    if not item:
        return False

    new_state = not item._disabled
    ```
  - 影响: docstring 声明返回"New bypass state"，但节点不存在与"切换后为启用"同为 False，调用方无法区分失败与成功，易把异常路径当成功吞掉。
  - 建议: 失败路径返回 `Optional[bool]`（None）或拆分出显式存在性检查。
  - 置信度: 高

### 通过项

- 线程安全：全部操作运行于 Qt 主线程（QGraphicsScene 交互），不触碰 OpenGL；模型变更经 `NodeGraph` 内部锁（node_engine.py:1171/1221 `with self._lock`）串行化，无跨线程数据竞争面。
- GPU 资源生命周期：本文件不直接持有 ModernGL 对象；节点删除经 `node_removed`/`nodes_batch_removed` 由页面提交 `CompositeCommand.CLEANUP_NODE` 至渲染线程按 deferred-release 清理（composite_page.py:2537-2538、2695-2696），无 UI 线程 GL 调用。
- GLSL/HDR 正确性：不涉及着色器与 HDR 数据路径。
- 信号防递归：move/select/bypass/set_parameter 的信号处理器（如 `_on_nodes_moved`，composite_page.py:4089-4115）仅读取位置并推送可合并 undo（merge_window_ms=500），无回写控件回路，未发现递归触发链。
- `add_node` 的实例序号分配时序正确：`assign_instance_index` 在 `add_node` 入图前调用且算法排除自身（node_engine.py:399-401），粘贴/拖拽 3D 子节点路径同样调用。
- `move_nodes`/`move_nodes_by_delta` 对长度不匹配返回 False、逐节点捕获 old_position 并同步 `node.position`，与页面 undo 契约一致。
- `set_zoom` 对 level 做 0.1–5.0 截断后写 `_current_zoom` 并 emit `zoom_changed`，与 widget 缩放状态一致。
- `select_upstream`/`select_downstream` 使用 widget 提供的遍历辅助（widget:3703/3721）并以 set 去重，无环风险。
- `delete_selected_connections` 对锁定节点（`is_node_locked`）做了双向端点检查，锁定节点连线不可删。
- group 系列委托 `group_commands` 的 Command 对象（签名核对一致：`create_group_from_selection(graph, ids, title, color)` 等），并统一 emit `graph_changed`。
- `import_preset`/`export_preset`/`delete_preset`/`list_presets` 完全委托 `PresetManager`，签名匹配（preset_manager.py:109/155/166/208/270/314）。
