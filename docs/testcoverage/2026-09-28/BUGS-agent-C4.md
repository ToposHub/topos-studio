# BUGS-agent-C4：color_controller.py 补测发现的疑似缺陷

- 日期：2026-09-28
- 模块：`src/app/controllers/color_controller.py`
- 来源：Plan-02 补测（`tests/app/test_color_controller_coverage.py`，111 用例）
- 处置原则：**只报告，不修改 src**。以下问题的"当前行为"均已用锁定断言
  （bug pin）写进测试（见各条"锁定用例"），修复后应把断言翻转为正确预期。

严重度定义：high（用户可见数据/状态错误）> medium（状态不一致，特定路径才暴露）
> low（健壮性缺口）。

---

## BUG-C4-01（high）：遮罩增/删/清空/建组/层级拖拽全部绕过 undo 栈

- 位置：`on_mask_shape_added` / `on_mask_shape_removed` / `on_all_masks_cleared` /
  `on_mask_group_added` / `on_mask_hierarchy_updated` / `on_brush_stroke_added`
  （color_controller.py L35-92、L183-225、L968-1114、L1238-1287）。
- 现象：这些方法直接改动 `layer.mask_shapes` 数据树并置 dirty、重渲染，但从不调用
  `self.engine.history.push_state(...)`。对照同文件的
  `on_duplicate_mask_shape`（L118-120）和 `on_copy_masks_to_layer_requested`
  （L165-167）——窗口复制类操作在改动前会以 `copy_image=False` 提交快照，
  说明遮罩结构的"可撤销"语义在代码库中是存在的，只是上述 6 个入口没有接上。
- 后果（用户可见）：
  1. 误删一个精心调好的 Power Window 后 **Ctrl+Z 无法恢复**（撤销栈里没有
     删除前快照，回退的是上一次调色参数）；
  2. 误点 "Clear All Masks" 清空整页窗口后同样不可撤销；
  3. 树形面板拖拽改层级（`on_mask_hierarchy_updated`）不可撤销，配合
     BUG-C4-03（结构缺失 id 被静默丢弃）会造成不可逆的数据丢失。
- 锁定用例：`test_mask_add_remove_clear_group_never_push_undo_state_bug_pin`
  ——把 add → remove → clear → group → hierarchy 五类操作跑一遍后断言
  `engine.history.pushed == []`，并对照 duplicate 操作会 push（`copy_image=False`）。
  注释写明修复后应改为逐操作断言 `pushed` 非空。
- 建议修复方向：与 `on_duplicate_mask_shape` 一致，在各结构改动入口先
  `push_state(img_full_original, layers, active_layer_index, copy_image=False)`
  再改数据树；注意 `process_mask_update`（参数微调）刻意不入栈是对的，
  不要顺手加上。

## BUG-C4-02（high，审查 Critical 项）：根级 MaskGroup 使三条 Brush 流程 AttributeError 崩溃

- 位置：`on_brush_toggled` L1174（`if s.type == "brush"`）、
  `on_brush_params_changed` L1204、`on_brush_stroke_added` L1246——三处都只遍历
  根级 `layer.mask_shapes` 并直接读 `s.type`。
- 现象：`MaskGroup`（src/features/color/core/layer.py L242-273）**没有 `type`
  实例属性**（只有类常量 `TYPE = "group"`，序列化时才写入 dict）。根级列表自
  P1-COLOR-09 起可包含群组，因此任何"先把若干窗口打包成组、再开 Brush 工具"的
  操作序列都会 `AttributeError: 'MaskGroup' object has no attribute 'type'`。
- 崩溃路径（已锁定）：
  1. 选中 2 个窗口 → Masks 面板 "Group"（或拖拽建组）→ 根级出现 MaskGroup；
  2. 点 Brush 按钮 → `on_brush_toggled(True)` 遍历根级 → 读 `group.type` → 崩溃；
  3. 即使先有 Brush 形状再建组，之后画笔参数变化（`on_brush_params_changed`）
     或落笔（`on_brush_stroke_added`）同样崩溃。
  另注：三处的遍历还只查根级、不递归群组——即使 MaskGroup 补上 `type`，
  组内的 brush 形状也永远找不到（现有 brush 形状会被重复创建）。
- 对照（安全写法已在同文件）：`_iter_trackable_shapes`（L263-281）用
  `isinstance(item, MaskGroup)` 递归展开 + `getattr(shape, "type", "")` 兜底，
  是群组安全的遍历范式。
- 锁定用例：`test_brush_flows_crash_on_root_level_mask_group_bug_pin`
  （参数化 3 条流程，`pytest.raises(AttributeError)` 锁定当前崩溃）；安全路径由
  `test_iter_trackable_shapes_is_group_and_type_safe`（getattr 兜底 + 递归展开）
  与 `test_brush_toggled_on_creates_brush_shape_when_missing` 等正向用例覆盖。
- 建议修复方向：三处遍历改用 `_iter_trackable_shapes` 风格的递归展开（brush
  也是叶节点类型），或最小修复：遍历时 `isinstance(s, MaskGroup)` 跳过 /
  `getattr(s, "type", None)`。修复后把 3 个 pin 用例翻转为行为断言
  （群组在场时 brush 开关/参数/落笔正常）。

## BUG-C4-03（medium）：on_mask_hierarchy_updated 丢弃结构中缺失的遮罩（不可逆数据丢失）

- 位置：`on_mask_hierarchy_updated` L1084-1103（`build_from_struct`）。
- 现象：重建数据树时只接收 `structure` 里出现的 id；原树中存在但新结构缺失的
  遮罩被静默丢掉（不报错、不进 undo——叠加 BUG-C4-01）。
- 后果：Qt 树控件如果发出一份不完整的拖拽结构（或上游面板 bug），用户会直接
  丢失窗口且无提示无撤销。保守做法应是把缺失项追加到根级（或至少校验
  结构完整性后拒绝应用）。
- 锁定用例：`test_mask_hierarchy_updated_rebuilds_tree_and_drops_unknown_ids_bug_pin`
  ——传入缺一项的结构，断言被丢的 id 在 `get_mask_item_by_id` 中已查不到。
- 建议修复方向：`build_from_struct` 完成后，把 `all_items` 中未被消费的项
  append 回根级（保持顺序），或完整性校验失败时整体回滚。

## BUG-C4-04（medium）：_tracker_frame_context 的 total_frames 回退是死代码，无时间线剪辑时跟踪范围塌缩为 [0,0]

- 位置：`_tracker_frame_context` L606-611。
- 现象：

  ```python
  frame_start, frame_end = 0, 0
  ...clip 解析（无剪辑时 frame_start/frame_end 保持 0/0）...
  if frame_end < frame_start:          # 0 < 0 恒为 False
      total_frames = ...video_controller.total_frames
      frame_end = max(frame_start, int(total_frames) - 1)
  ```

  `frame_end < frame_start` 在无时间线剪辑时永远为 False（双双默认 0），因此
  `total_frames` 回退分支（L607-611）**不可达**（本次覆盖率运行显示这 5 行
  0 命中，与推断一致）。
- 后果：Color 会话有 clip_id 但 `_current_timeline_sequences` 解析不到剪辑时
  （如会话恢复早于时间线加载），`_ensure_tracker_binding` 把
  `source_frame_start/end` 写成 0/0、参考帧被钳到 0：
  1. Tracker 面板 "单帧/首帧/末帧" 导航全部失真（L606 之后的
     `reference = max(frame_start, min(reference, frame_end))` 把任何参考帧钳为 0）；
  2. 跟踪任务范围只覆盖第 0 帧，表现为"跟踪跑一下就完成、无结果"。
- 锁定用例：`test_tracker_frame_context_resolves_timeline_and_total_frames`
  ——无剪辑 + `total_frames=11` 时断言当前返回 `("c1", "/m.mov", 0, 0, 0)`
  （参考帧被钳到 0），注释写明修复后应为 `(..., 3, 0, 10)`。
- 建议修复方向：把守卫改成"剪辑缺失时"而不是"frame_end < frame_start"，
  例如 `if clip is None:` 时用 `total_frames` 推导 `frame_end`；或初始
  `frame_end = -1` 使现有比较生效。修复后翻转上述 pin 断言，并同步检查
  `tests/app/test_color_controller_coverage.py` 中
  `test_start_tracker_*` / `test_ensure_tracker_binding_creates_and_updates`
  使用的 time-line fixture（它们当前靠剪辑提供真实帧范围来绕开此缺陷）。

---

### 附：非缺陷观察（不单独立 BUG）

- `on_crop_applied(panel_params=None)` 的 `panel_params` 参数完全未使用
  （几何一律取 `canvas.get_crop_data()`）；是死参数/坏味道，可在下次触碰时清理。
- `on_mask_group_added` 在传入的全是失效 id 时仍会创建一个空群组并提示
  "Packed 0 items"；行为可用但略反直觉，测试
  `test_mask_add_remove_clear_group_never_push_undo_state_bug_pin` 顺带覆盖了
  该分支。
