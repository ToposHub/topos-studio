# BUGS-agent-B2 — node_properties_panel.py 补测过程中发现的疑似 Bug

- 模块：`src/features/composite/ui/node_properties_panel.py`（7,683 行）
- 发现日期：2026-09-28
- 发现方式：为补测试（`tests/composite/test_node_properties_panel_coverage.py`）
  通读全模块 + 用真实 `set_node`/`set_nodes` → 控件 `setValue`/`setCurrentIndex`
  信号路径驱动参数编辑时复现。多选路由缺陷即审查所指「多选路由写错节点」。
- 复现环境：PyQt6 6.10.2 offscreen（QT_QPA_PLATFORM=offscreen），无需 GL。
- 处理约定：按任务规则 **不修改 src**；两个行为缺陷已以 `xfail(strict=True)`
  回归用例固化：
  - `TestMultiSelectionRoutingRegression`（BUG-B2-01，2 例）
  - `TestPresetSnapshotRegression`（BUG-B2-02，1 例）
  修复后会翻成 XPASS 失败，提醒同步移除标记。

---

## BUG-B2-01 多选：编辑第二个节点 section 的参数被路由/写入第一个节点

- 位置：
  - `NodePropertiesPanel._on_param_changed`（~L4418-4433）
  - 与 `set_nodes` 的交互（~L1884-1891：`_current_node_id` 固定为
    `selected_ids[0]`，即第一个选中节点）
- 根因：所有参数行（`_create_param_row_for_section` 等工厂）把控件信号接到
  面板级的 `_on_param_changed`，而该方法用 **面板当前** 的
  `self._current_node_id` / `self._current_parameters` 做路由、相等性短路和
  section 同步。多选（`set_nodes` 传 ≥2 个节点）时 `_current_node_id` 恒为
  第一个选中节点，因此：
  1. 在 n2 的 section 里改控件 → `parameter_changed("n1", key, value)` 发给
     n1（CompositePage 会把参数写到 n1 并以 n1 推 undo/渲染）；
  2. `_current_parameters[key] = value` 污染 n1 的参数缓存，
     `section.parameters[key] = value` 写进 **n1 的 section**；
  3. n2 自己的 `section.parameters` 与面板显示保持旧值（面板与图数据脱节）；
  4. 相等性短路 `if self._current_parameters.get(key) == value: return` 比较
     的是 **n1 的缓存值**——若 n2 的新值恰好等于 n1 的现值，编辑被 **完全
     吞掉**（0 次信号发射）。
- 用户可见症状：框选两个同类节点，在下方（第二个）节点的参数面板里拖
  mix/scale 等，实际变化的是最上方第一个节点；某些值下改了没反应。
- 复现脚本（offscreen）：
  ```python
  panel.set_nodes([data_n1(gain=1.0), data_n2(gain=1.0)])
  panel.parameter_changed.connect(log)
  panel._sections["n2"]._param_widgets["gain"]["widget"].setValue(0.25)
  assert log.calls == [("n1", "gain", 0.25)]        # 期望 ("n2", ...)
  assert panel._sections["n2"].parameters["gain"] == 1.0   # n2 未更新
  ```
- 修复方向建议：行工厂在闭包里捕获本 section 的 node_id（或把 node_id 作为
  sender property 随信号传递），`_on_param_changed` 按 section 路由并读写该
  section 的 parameters；相等性短路也应对该节点的参数做。
- 固化用例：`TestMultiSelectionRoutingRegression`（2 例，strict xfail）。

### 附带观察（同根因，记录备查）BUG-B2-01a

- `_current_node_ids` / `_common_parameters` / `_param_values_per_node` 只在
  `__init__` 初始化，`set_nodes` 从不填充；`_compute_common_parameters` /
  `_create_multi_selection_params_group` / `_create_multi_selection_param_row`
  / `_on_multi_param_changed`（`parameters_changed_batch` 批量信号，
  CompositePage L6225 有连接、L6881 有处理器）在现行面板流程中 **不可达**
  （死代码）。多选批量编辑 + 批量 undo 的整条链路实际依赖上面的错误路由
  （逐个 `parameter_changed`）在工作。
- `_create_multi_param_changed` 本身的载荷契约（node_ids + 每节点旧值字典）
  是正确的，已由 `test_legacy_batch_signal_payload` 直接锁定，修复路由时可
  复用。

---

## BUG-B2-02 预设快照：combo 参数存的是显示文案而非原始选项值

- 位置：`NodeSectionWidget._collect_current_values`（~L1445-1449）
  ```python
  elif kind in ("combo", "channel", "bool_channel"):
      data = widget.currentData()
      values[key] = data if data is not None else widget.currentText()
  ```
- 根因：`_create_combo_row` 用 `combo.addItems(display_items)` 只放 **显示
  标签**（`_option_display_label` → `option_value.title()`），从不 `setItemData`，
  因此 `currentData()` 恒为 None，回退到 `currentText()` —— 是 `"Gaussian"`
  而不是原始值 `"gaussian"`。change 信号走的是 index→options[idx] 的原始值
  映射（正确），但「保存预设」(`_on_save_preset` → `_collect_current_values`)
  走的是显示文案：预设里存的值无法匹配任何原始 option，重新应用后 combo
  无法定位、参数值与合法枚举脱节。
- 受影响面：所有 option 参数且原始值不是 Title-Case 的节点预设（blur_type、
  filter、format_mode、pivot_mode 等大量小写原始值）。
- 附带小问题（同一方法）：`kind in ("float", "int")` 分支
  `values[key] = widget.value()` 返回 DragValue 的 float —— int 参数（如
  samples/width）在预设里变成 16.0 而非 16，类型漂移（多数下游能容忍，记录
  备查）。
- 修复方向建议：`addItems` 时用 `setItemData(i, raw_value)`；或
  `_collect_current_values` 对 combo 通过 `options[currentIndex()]` 反查原始值
  （与 change 处理器同一条路径）。
- 固化用例：`TestPresetSnapshotRegression::test_collect_current_values_combo_returns_raw_option`
  （strict xfail）。

---

## 汇总

| 编号 | 严重度 | 一句话 | 固化用例 |
|------|--------|--------|----------|
| BUG-B2-01 | 高（数据写错节点） | 多选时第二节点 section 的编辑被路由/写入第一个节点，相等值时被吞 | TestMultiSelectionRoutingRegression（2 xfail） |
| BUG-B2-01a | 观察 | 批量多选 UI/信号链路为死代码，`_current_node_ids` 永不填充 | test_legacy_batch_signal_payload 锁载荷契约 |
| BUG-B2-02 | 中（预设损坏） | 预设快照存 combo 显示文案而非原始选项值；int 漂移为 float | TestPresetSnapshotRegression（1 xfail） |

（未修改任何 src 文件。）
