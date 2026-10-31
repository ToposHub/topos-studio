# F1.4 · 快捷键、工作区与硬件控制效率验收——台账

> 日期：2026-10-23；基线 commit：`a50417ec2`（feat/f-plan）。输入：专项只读审计（快捷键三路由/布局/Tangent/双屏全覆盖摸底）。

## 1. 既有能力验收结论（框架层齐备，复用不重建）

| 验收点 | 结论 | 证据 |
|---|---|---|
| 预设导入导出/冲突/自定义 | 完备（schema v1 + merge/replace + 严格校验 + UI 按钮） | tests/edit/test_shortcut_preset.py 14 用例 |
| MG 浮窗作用域 | 完备（guard + 全仓正则门禁 + Cmd 转发 + 「可见未激活不抢」） | tests/edit/test_shortcut_scope_gates.py、tests/ui/test_mg_shortcut_customization.py |
| 跨产品/跨页工作区保存 | 完备（LayoutStore 按 (product_mode,page) 隔离 + 跨产品隔离测试 + Color/Edit/Deliver 预设专测） | tests/architecture/test_layout_store.py、test_layout_integration.py |
| Tangent 断线重连/无设备降级 | 完备（退避 0.5→5s、休眠 30s 探测、wake、不可用单次提示） | tests/shared/control_surfaces/test_tangent_hub_client.py |

## 2. 本批修复/补齐

| 项 | 内容 | 测试 |
|---|---|---|
| **实际缺陷修复**：Edit JKL/Shift+B 无文本框屏蔽 | 字幕查找/轨道重命名等 QLineEdit 内输入 j/k/l 会触发窗口级变速。新增共享 `shortcut_scope.text_input_focused()`（与 main_window 四处 isinstance 屏蔽同源）；`_transport_context_active` 与 `toggle_edit_mode` 接入 | test_jkl_silent_while_text_input_focused、test_shift_b_edit_mode_silent_while_text_input_focused（新增 2 用例） |
| Tangent「一手势一 Undo」端到端边界 | 连续 5 次调整=单 start；防抖到期=end；新手势=新 start | test_one_gesture_one_undo_entry |
| Tangent 切层 rebase 反馈 | 切层后 `sync_from_panel` 重推当前 grade 到 Hub | test_sync_from_panel_rebases_display_after_layer_switch |
| 副屏消失找回（具名用例） | 保存于已拔副屏的几何 → 恢复回落主屏（钳制机制既有，补 F1.4 具名断言） | test_geometry_from_vanished_secondary_monitor_recovers |

本计划（Plan F）截至目前**未新增用户级快捷键动作**（黄金驱动/压力驱动非用户入口），故「新增动作注册」条目为空集；未来 F2.x 新增动作时按 shortcut_manager.py L16-44 注册法接入。

## 3. 同键跨页路由的覆盖方式（登记）

同一按键（Space/K/A 等）在 Edit/Color/Composite/MG 命中各自目标的保证来自**三个独立消费层**：main_window eventFilter 首轮拒绝链（按 current_page + 焦点分发）、WindowShortcut+guard（handler 内窗口归属+文本屏蔽）、控件级 keyPressEvent（焦点即作用域）。各层分别有测试（transport/crop/composite dispatcher/MG）；未新建跨三页的单体集成测试（需实例化完整 MainWindow，成本与稳定性不匹配既有分层覆盖）——以分层覆盖 + 全仓正则门禁作为验收口径。

## 4. 未验证/已知缺口（如实登记）

- **Tangent 真机（H 级）**：本机无 Tangent Hub/硬件——桥接/协议/手势单元测试全绿（29+4 用例），真机反馈时序/轮感未验证。
- **DPI 变化**：全仓无 screenChanged/logicalDotsPerInch 处理代码，仅靠尺寸钳制间接兜底——登记为已知缺口（新增 DPI 处理属新功能，另行立项，不在本验收包）。
- **运行时拔屏监听**（screenAdded/removed）：未实现；恢复时回落已验证（§2）。
- **双屏真实布局（H 级）**：单机单屏，仅逻辑级钳制验证。
- LayoutProfile.floating_panels 死字段：登记（清理属 F6.x 归档包顺手项）。

## 5. 测试汇总

本批影响域 87 passed（快捷键预设/门禁/transport/Tangent/composite dispatcher/crop/MG/几何钳制）。
