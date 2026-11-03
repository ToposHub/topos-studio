# R10 留档：清理旧 UI/backend 写入与用户库偏好（2026-11-04）

## 实现

- **旧入口图模式路由**（R00 审计 A1–A5）：`ColorControllerMixin.
  _graph_resource_route()`——图会话 READY 时返回正式命令适配器：
  - `add_plugin_to_current_layer`（旧 Effects 面板 A1 + 顶栏菜单 A2）→
    `activate_effect`（add_effect 命令）；
  - `apply_lut_from_path`（A3）→ 注册为工程资源（R02 会话导入）→
    `set_lut` 命令；
  - `on_lut_intensity_changed`（A4）→ `set_lut_strength` 命令；
  - `on_lut_reset`（A5）→ `remove_lut` 命令。
  旧模式（无图会话/preview 模式）保留 legacy 行为；**图模式下 legacy
  engine 零触碰（无双写）**。
- **库偏好**（用户配置域，不进工程 undo）：两浏览器新增
  `browser_state()/set_browser_state()/missing_favorite_ids()`——收藏/
  分类折叠/视图模式/缩略图缩放；主窗口启动装载、closeEvent 保存
  （`color_effects_browser_state`/`color_lut_browser_state`；不含任何像素
  缓存）；缺失收藏（重命名/卸载）可诊断。
- **D1 清除**：tools_panel 死代码（注释掉的 lut_panel 接线）删除。

## 测试（tests/app/test_color_resource_routing.py，5 项）

- add_plugin 路由：1 命令、图内实例落正确节点、engine 双写哨兵不触发；
- LUT 应用/强度/重置路由：3 命令、最终 remove 后无 lut_ref；
- 无图会话回退 None（legacy 域）；
- 偏好 roundtrip + 缺失收藏诊断 + 非法载荷容错；
- D1 源码断言清除。
- 回归：控制器/手势/浏览器/preview/live UI 50 passed；原生验证 passed
  （per_node 保持 evaluated_node_thumbnails）。

## 剩余（如实登记）

- **D2（旧 plugin_panel/lut_panel 控件实例删除）未完成**：读路径
  （layer_controller 选中回显、hydrator `_lut_to_panel`）迁 projection
  后才可删——留待 N03（Tracker/Qualifier 面板同批 typed handle 化）一起
  收口；当前旧面板保持隐藏不接信号（无双写风险）。
- 脚本 API（A8）与预设入口的图模式路由随 N11/N03 批次；中英文/窄宽/
  200% 缩放的 UI 矩阵归 R12。
