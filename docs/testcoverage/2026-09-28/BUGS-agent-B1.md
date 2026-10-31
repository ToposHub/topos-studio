# BUGS-agent-B1 — composite_viewport.py 补测过程中发现的疑似 Bug

- 模块：`src/features/composite/ui/composite_viewport.py`（11,512 行）
- 发现日期：2026-09-28
- 发现方式：为补测试（tests/composite/test_composite_viewport_coverage.py）用真实
  Qt 鼠标事件（`mousePressEvent` / `mouseMoveEvent` / `mouseReleaseEvent`）驱动
  ROI / PAINT 工具时复现；既有测试均直接调用 `_handle_tool_press/_move/_release`
  私有方法，绕过了事件分发层，因此该路径从未被测试覆盖。
- 复现环境：PyQt6 6.10.2 offscreen（QT_QPA_PLATFORM=offscreen），无需 GL。
- 处理约定：按任务规则 **不修改 src**；两个行为缺陷已以 `xfail(strict=True)`
  回归用例固化在
  `tests/composite/test_composite_viewport_coverage.py::TestMouseEventDispatchRegression`
  （修复后会翻成 XPASS 失败，提醒同步移除标记）。

---

## BUG-B1-01 ROI 工具：真实鼠标事件无法完成 ROI 拖拽（不提交、不发信号）

- 位置：
  - `CompositeViewer._handle_roi_press`（~L8664）
  - `CompositeViewer.mouseMoveEvent` / `mouseReleaseEvent`（~L7606 / ~L7728）
- 根因：`_handle_roi_press` 只设置 `_user_roi_drag_rect` 并 `return True`，
  从不调用 `self._interaction_state.start_drag(...)`。而
  `mouseMoveEvent` 只在 `self._interaction_state.is_dragging` 为 True 时才分发
  `_handle_tool_move`；`mouseReleaseEvent` 同样只在 `is_dragging` 时才调用
  `_handle_tool_release`。结果：ROI 拖拽的实时矩形更新与松手提交路径
  （`_handle_roi_move` / `_handle_roi_release` → `_apply_user_roi` →
  `user_roi_changed` 信号）在真实鼠标交互下永远不可达。
- 用户可见症状：
  1. Shift+R 进入 ROI 工具后按住左键拖拽，画面无任何反馈（无拖拽框）；
  2. 松手后 ROI 不生效，`user_roi_changed` 信号 0 次发射，渲染区域不变；
  3. `_user_roi_drag_rect` 残留为按压时的 (x, y, 0, 0)，直到按 Esc 或下次
     press 才被覆盖。
- 复现脚本（offscreen）：
  ```python
  v.set_tool_mode(ViewerToolMode.ROI); v.set_texture(5, 1000, 1000)
  v.mousePressEvent(QMouseEvent(...LeftButton, QPointF(100,100), ...))
  v.mouseMoveEvent(QMouseEvent(...Move, QPointF(300,250), buttons=LeftButton, ...))
  v.mouseReleaseEvent(QMouseEvent(...Release, LeftButton, QPointF(300,250), ...))
  # 期望：_user_roi_rect 非空、user_roi_changed 发射 1 次
  # 实际：_user_roi_rect 为 None、信号 0 次、_user_roi_drag_rect 残留
  ```
- 既有测试为何没拦住：`tests/composite/test_composite_viewer_roi.py::
  test_roi_drag_interaction_commits` 直接调用 `viewer._handle_tool_press/_move/
  _release`，绕过了事件分发门控。
- 固化用例：`test_roi_drag_commits_through_real_mouse_events`（xfail strict）。
- 修复方向（供参考，未实施）：`_handle_roi_press` 成功时调用
  `start_drag(tool=ROI, ...)`（或让 mouseMove/Release 对 ROI/PAINT 这类
  “无 interaction_state 拖拽” 工具按各自活动标志分发）。

## BUG-B1-02 PAINT 工具：真实鼠标事件下笔画永不提交

- 位置：
  - `CompositeViewer._handle_paint_press`（~L8712）
  - `CompositeViewer.mouseMoveEvent` / `mouseReleaseEvent`（同上）
- 根因：与 BUG-B1-01 同源。`_handle_paint_press` 只写
  `_paint_drag_points`（首点）/`_paint_clone_src` 并 `return True`，不调用
  `start_drag()`；`is_dragging` 恒为 False，因此
  - move 事件不分发到 `_handle_paint_move` → 笔画永远只有 1 个点；
  - release 事件不分发到 `_handle_paint_release` →
    `paint_stroke_committed` 信号 0 次发射（页面无法组装 undo），
    `_paint_drag_points` / `_paint_drag_node_id` 松手后残留不清。
- 部分路径仍然可达的原因：Alt+click 克隆源（`_paint_clone_src`）与
  `[` / `]` 笔刷快捷键发生在 press/keyPress 阶段，不受该门控影响。
- 复现结论（offscreen 实测）：press 后 `_paint_drag_points == [[...]]`（1 点），
  两次 move 后仍为 1 点，release 后 `strokes == 0`、
  `_paint_drag_points` 仍非 None。
- 既有测试为何没拦住：`tests/composite/test_viewer_paint_tool.py` 全部直接调用
  `_handle_paint_press/_move/_release`。
- 固化用例：`test_paint_stroke_commits_through_real_mouse_events`（xfail strict）。

## BUG-B1-03（低风险，代码健康）get_state / set_state 在同类中重复定义，前一份是死代码

- 位置：
  - `get_state` L2929、`set_state` L2961（前一份）
  - `get_state` L10975、`set_state` L11012（后一份，运行时生效）
- 说明：Python 取后一定义，前一份永不执行。两份实现并不等价（前一份缺少
  `resolution_mode` / `magnifier_*` / `background_mode` / legacy
  `show_bbox` 别名等键，且 `set_state` 不走 `set_channel_mode` 等 setter）。
  行为当前正确（后一份生效），但任何人修改前一份都会静默失效；且
  `tests/composite/test_composite_viewer_compare.py::test_viewer_compare_mode_round_trip`
  读取的键恰好在两份里都存在，掩盖了差异。
- 建议：删除 L2929–L3026 的死代码副本（未实施）。

---

### 测试执行结论

- 新增：`tests/composite/test_composite_viewport_coverage.py`
  （121 passed + 2 xfailed(strict)，`-p no:cacheprovider`）。
- 既有 viewport 相关测试回归无干扰（详见任务汇报）。
