# N03 — Tracker/Qualifier 及非 primary 工具 typed handle（2026-11-04）

计划任务 N03（P0）：Tracker/Qualifier/窗口等非 primary 工具的 legacy
层读写全部改 typed handle——参数实际落正确 node（不按 root
active_index 猜）；含 D2 旧控件实例删除。

## 交付内容（5 个提交）

### N03-a 服务端图命令（13b9f0c）
- `ColorGraphService.op_set_node_qualifier`：Qualifier 域 merge patch
  （`params["qualifier"]`；`_DICT_PARAM_OVERLAYS` 投影并入
  `qualifier_params`）——该域的第一个写入者。
- `op_update_node_window_shape`：单形状属性 patch（群组 children 递归
  定位；身份不变）。窗口域存 `legacy_payload.mask_shapes`（bitmap
  引用等路径只在迁移载体豁免）。
- `op_set_node_window_shapes`：整域替换（缺 id 补新、给定 id 保留）。
- 3 个 ACTION_CONTRACTS 登记（set_qualifier / update_window_shape /
  set_windows）。
- **顺带修复 schema 潜在缺陷**：`freeze_params` 的 list 分支未传递
  `allow_paths`——legacy 载体内嵌套列表中的路径字符串会被错误拒绝
  （此前未被触发：旧 LUT 路径都在 dict 顶层）。修复后 legacy 载体在
  任意嵌套深度豁免路径（迁移语义），params 域仍恒禁。

### N03-b Qualifier 面板路由（a95cffe）
- `main_window`：`qualifier_panel.params_changed` 直连
  `engine.set_qualifier_params` → `on_qualifier_params_changed` 统一
  入口（图模式 typed handle 命令 / legacy 直写）。
- 手势：lazy begin + 权威投影 diff（未变化键不提交）+ 400ms 自冲刷
  定时器（吸管/部分滑条无 interaction 包裹，杜绝在途事务悬挂）+
  水合守卫（程序化回填回流零命令零双写）。
- `_on_color_graph_handle` 改读 `panel_layer_payload`（合并投影）——
  图命令写入的 qualifier 域在面板可见（裸 legacy 载体会显示旧值）。
- **修复 R10 遗留 bug**：`add_plugin_to_current_layer` 分支内局部
  import 使 `get_plugin_manager` 绑定到函数作用域，legacy 路径必然
  UnboundLocalError（test_color_controller_coverage 2 例在改动前已红）。

### N03-c Window 写路径路由（ab7cae4）
- `process_mask_update`（唯一参数汇聚点）：图模式活对象同值更新
  （画布即时反馈）+ patch 并入手势事务（一次拖动一次 undo）。
- 结构操作（增/删/复制/建组/层级/清空/画笔落笔）：图模式跳过 legacy
  history 压栈，改提交 `set_window_shapes` 图命令（undo 完整恢复）。
- 跨节点窗口复制：目标 legacy 层经 `node_id_for_legacy_layer` 反查
  节点，命令落**目标**节点。
- 图模式清空限定活动节点（legacy 语义仍清全部层）。

### N03-d Tracker typed handle（97cb360）
- `TrackingBinding` 新增 `node_id / compound_path / session_id`（默认
  空，旧工程载荷向后兼容；序列化往返携带）。
- `_ensure_tracker_binding` 创建/更新时捕获图节点身份。
- `_iter_trackable_shapes`（可跟踪窗口枚举）图模式读选中节点合并
  投影（compound 内成员正确）。
- `_tracker_result_layer`：异步任务完成后按**绑定节点身份**解析同步
  层——切 clip/换选中后结果不再同步到错误层（无身份/身份失效回落
  活动层）。

### N03-e D2 旧控件实例删除
- `main_window` 删除 `lut_panel`/`plugin_panel` 实例、
  `_legacy_color_resource_controls` 兼容容器及全部信号连接。
- dock 宿主固定为新浏览器（`color_lut_browser`/`color_effects_browser`）。
- 移除 layer/color controller 的旧面板水合回填；LUT 目录的旧画廊投影
  来源删除（唯一来源=资源会话目录）；节点工具标记刷新信号源换
  `color_lut_browser.resource_activated`，读投影换 `panel_layer_payload`。
- 说明：plugin_panel 的 AI 处理按钮自 R10 起已在隐藏容器中（生产不可
  达），删除无功能损失；AI 蒙版处理入口仍在 mask_panel。

## 验证

- 新测试：tests/color/test_color_graph_tool_domains.py（7）、
  tests/app/test_tool_domain_routing.py（5）、
  tests/app/test_tool_domain_routing_windows.py（5）、
  tests/app/test_tool_domain_routing_tracker.py（3）——覆盖：merge/
  投影、嵌套群组 patch、身份稳定、结构替换、schema 安全（NaN
  fail-closed、路径双载体分界）、compound 下行、图/legacy 双模式、
  水合守卫、undo 恢复、跨节点复制、绑定身份往返与结果同步。
- 回归：window/mask/tracker/qualifier/plugin 相关既有套件 400+ 项全绿
  （tests/color/test_window_*、test_tracking_*、test_qualifier_*、
  tests/app/test_color_controller_coverage 等）。
- 原生验证：
  `TOPOS_COLOR_GRAPH_RENDER=1 ... validate_color_node_media.py
  tests/test_videos/测试素材01.mov --workflow --output <本目录>`
  → status=passed（project_history/grade_commit/topos_save_reload/
  disconnect_reconnect 全过，JSON 存本目录）。

## 已知边界（如实登记）

- 图模式 mask 关键帧（P1-COLOR-09）与 grade 关键帧同属 N10 债务：
  图命令只写窗口/qualifier 域，legacy 工作层上的关键帧在重水合后不
  保留（与 B06.2 grade 手势的既有语义一致）。
- bitmap 蒙版的位图数据经投影序列化不保留（`to_dict(None)` 语义，
  既有行为）；正式位图资源化归 R09 闭包后续。
- `active_grade_node_id` 的 root active layer 反查兜底仍服务 flag 模式
  （无节点选中）；显式选中（含 compound）路径一律 handle。
