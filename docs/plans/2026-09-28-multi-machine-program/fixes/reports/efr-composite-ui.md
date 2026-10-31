# EFR — composite/ui 机械清理报告（E-rollup：`src/features/composite/ui/`）

- 日期：2026-10-02
- 范围：`src/features/composite/ui/`（对应 REMEDIATION_PLAN §2.E-rollup 第 4 行：163 条 process 级改进：类型注解/命名/小清理）
- 性质：纯机械清理，零行为变更。工作树留改动，未做任何 git add/commit。
- 分支：dev（保持不变）

## 1. 分类统计

| 类别 | 数量 | 说明 |
|------|------|------|
| 删除未使用 import | 103 个名字 / 33 文件 | AST 检出 + 全仓 re-export/属性引用双重复核；`__future__ import annotations` 一律保留 |
| 死代码删除 | 8 项（AUD 挂账）| 全仓 grep（含 tests/字符串反射/docs）零运行期引用后才删 |
| 补类型注解 | 627 个 `-> None` + 7 个参数注解 | 仅对"无可证伪风险"的签名补注；参数类型仅在 AUD 明确给出契约时补 |
| 小清理 | 4 项 | 冗余变量、重复字面量、函数级 import 上移、无用常量 |
| 命名收敛 | 0 项 | 未发现可安全改名的模块内私有标识符（审计未挂具体改名项；宁缺勿错） |
| 保留不改（有证据） | 6 项 | 见 §4 |

改动合计：42 个文件，+700 / −803 行（含注解）。

## 2. 抽样证据

### 2.1 未使用 import（AST + 全仓复核）

方法：自写 stdlib AST 检测器（收集 `Import/ImportFrom` 别名 → 对全文件 Name 节点 + 字符串常量 token + `__all__` 求交集）；
每个候选再经两道全仓 grep 复核（src/tests/plugins/scripts）：
1. `from <mod> import <name>` 形式的外部 re-export → 0 命中；
2. `<mod>.<name>` 属性式引用 → 0 命中。

样例（共 103 个名字）：
- `keyframe_editor.py`：13 个（AUD-10005：QFrame/QMenu/QPushButton/QScrollArea/QSizePolicy/QSlider/QToolButton/QFrame、QCursor/QKeySequence、QRectF/QTimer、Dict/Tuple）
- `composite_viewport.py`：15 个（含 viewer_tool_system 的 `HandleType/SnapConfig/TOOL_MODE_LABELS/apply_angle_constraint/apply_aspect_ratio_constraint/compute_constrained_delta/compute_reformat_rect_from_handles/get_cursor_for_handle/snap_to_format_edge/snap_to_format_center`、`get_icon/icon_mask`、`QSlider/QVBoxLayout`、`GL_STATIC_DRAW`）
- `node_editor_widget.py`：11 个（`copy/QMimeData/QByteArray/QFont/QColorDialog/QGraphicsProxyWidget/QBrush/QCursor/QDragEnterEvent/QDropEvent/NODE_REGISTRY`，AUD-10511）
- `spline_view.py`：3 个（含函数级死导入 `distance_point_to_segment as _dps`、`TANGENT_BROKEN`——被导入名在 spline_model 中均存在，故删除不改变 ImportError 行为）
- `keyframes_panel.py:649`：函数级 `from .timeline_view_model import make_param_row_id`（函数体内从未使用）
- `graph_layout.py`：TYPE_CHECKING 块内 `NodeItem`（块内另有 NodeGraph/Node 导入，结构保留）
- `timeline_commands.py`：`CompositeApplyResult/Keyframe/KeyframeTrack`

### 2.2 死代码（AUD 挂账，逐项验证后删除）

| AUD | 文件 | 内容 | 验证 |
|-----|------|------|------|
| AUD-10010 | keyframes_model.py | `_PARAM_CHANNEL_COLORS` 死常量 | 全仓唯一出现即定义行 + 审计文档；`_channel_color_for` 未引用它 |
| AUD-10307 | keyframes_tree_view.py | `_draw_solo`/`_draw_shy` 不可达 else（`"checked" if solo else "normal"` 等） | 上方 `if not solo/shy: return` 保证恒真；收敛为字面量，绘制输出不变 |
| AUD-9802 | connection_item.py | `_remove_from_port`/`_add_to_port` 无调用方 | 全仓 grep 仅定义行（docstring 自述 "no longer used"） |
| AUD-9903 | graph_layout.py | `node_ids`、`layer_height` 死变量 | 纯表达式（set 推导/乘法），无副作用 |
| AUD-9913 | graph_node_search_popup.py | `Ctrl+Up/Down` 分支不可达 | 前置 Up/Down 分支不区分修饰键先行 return；连同孤儿 `mods` 局部变量一并删除 |
| AUD-10210 | keyframes_timeline_view.py | 不可达 `self._start_drag("playhead", x)` fallthrough、无调用方 legacy shim `_do_keyframe_drag`、2 处冗余函数级 `KeyframePointVisual` 导入 | `y<=_RULER_H` 与 `y>_RULER_H` 两分支穷尽且均 return；shim 全仓无调用（其余命中为其他类的同名方法）；顶部 28 行已有同名导入 |
| AUD-10916 | spline_view.py | `_sv_mouseDoubleClickEvent` 中 `idx` 计算块死代码 | `idx` 从未被读取（后续用 sort 后的 `new_idx`） |
| AUD-10917 | spline_view.py | `if ep.widthF() and False: pass` | 恒假 no-op，整句删除 |
| AUD-9513 | composite_page.py | `_pending_disconnect_data` 两处只写不读；`_pf_vmo` 计算结果从未读取 | `_apply_view_mode_overrides` 其余 4 处调用保留；`_ai_pending` 只读不写与 `_node_graph._nodes` 私有穿透**未动**（改动会变更行为/迭代语义） |
| AUD-9712 | composite_viewport.py | `_should_show_safe_areas` 中 if/else 两分支完全相同 | 收敛为单赋值；孤儿 `active_cam_id` 读取一并删除（纯 dict.get） |
| AUD-9406 | backdrop_item.py | `_was_moved` 只写不读（4 处） | 全仓零读取点，仅删赋值行 |
| AUD-11206 | viewer_handle_drawing.py | `GL_TRUE = 1` 常量全文件未使用 | 全仓引用均为其他模块各自的 OpenGL 导入 |

### 2.3 类型注解

- 自动化补 `-> None`：目标筛选为"无既有返回注解 + 函数体无 `return <值>`/无 yield + 非纯 raise/`...` 桩"。共 **627** 个签名（37 文件），插入点经括号深度扫描定位签名收尾冒号，幂等（二次运行 +0）。
- AUD 明确给出的参数契约（手工补）：
  - `spline_commands.py`（AUD-10900）：`from_key`/`apply_to` 的 `key: "SplineKey"`（TYPE_CHECKING 导入，运行时零开销）
  - `composite_page.py`（AUD-9514）：`on_enter(color_texture_id: Optional[int] = None, color_width/height/revision: int = 0)`、`_on_nodes_connected(...: str)`、`_on_parameter_changed/set_parameter` 的 `value: Any`
- 有意不做：Qt 事件参数的具体类型（QGraphicsSceneMouseEvent vs QMouseEvent 依赖宿主，宁缺勿错）、含值返回的函数、`NotImplementedError` 桩。

### 2.4 小清理

- `import_report_panel.py`：`update_report` 内 `import os` 上移模块顶部（AUD-10012）
- `keyframes_timeline_view.py`：3 处重复 `row_id = f"param:{node_id}:{param_name}"` 提取为模块常量 `_PARAM_ROW_ID_FMT`（AUD-10210）
- `spline_commands.py`：`Optional` 死导入删除（AUD-10900）

## 3. 保留不改（有证据，防误删）

| AUD | 项 | 保留理由 |
|-----|----|---------|
| AUD-10708 | `_update_param_widget_value` | `tests/composite/test_node_properties_panel_coverage.py:984-994` 直接调用（非死代码）；`node_reset_requested` 为公共信号（公共 API 红线） |
| AUD-10511 | `_viewing_node_id` 未在 `__init__` 声明 | 补声明会把 AttributeError 变 None——行为变化 |
| AUD-9513 | `_node_graph._nodes` 私有穿透、`_ai_pending` 只读 | 改用 `nodes` 属性可能改迭代语义；删读取需重构逻辑 |
| AUD-11404 | `SnapConfig.threshold_pixels` 未使用 | dataclass 字段=构造器公共 API |
| AUD-11409 | `on_mmb_release(event)` 未使用参数 | Qt 处理器签名约定 |
| AUD-9408 | role slug 显示未本地化 | 行为/显示变更，越界 |

## 4. 验证输出

1. **py_compile**：全部 42 个改动文件通过（`0` 非零退出）。
2. **定向测试**（共 11 个既有测试文件，未整目录跑）：
   - `tests/composite/test_asset_dependency_panel.py`、`test_backdrop.py`、`test_composite_graph.py`
   - `tests/ui/test_composite_keyframes_panel.py`、`test_composite_keyframes_interaction.py`、`test_composite_keyframes_polish.py`、`test_composite_page_selection_attach.py`、`test_keyframes_panel_toolbar.py`、`test_graph_bugfix_polish.py`
   - `tests/composite/test_fix_wd4g28_aud.py`、`test_node_properties_panel_coverage.py`、`test_3d_phase1_correctness.py`
   - 结果：**296 passed / 1 xfailed / 3 failed**。
   - 3 个 failed 全部是 `test_fix_wd4g28_aud.py::TestAud11306`（mask corner / roto delegation，值断言 0.9≠0.5）。**已证明与本次改动无关**：将 `src/features/composite/ui` 整体回退到 HEAD 建立影子树（其余目录保持现状）后运行，同样 3 个用例以相同断言值失败 → 失败源于其他模块的并行改动（本会话未触碰 `viewer_handles.py`/`viewer_tool_system.py`，二者相对 HEAD 无 diff）。
3. **裸 except**：`grep -rn "except:" src/features/composite/ui/` → 0 命中。
4. 附带说明：验证过程中发现并行会话在 `render3d/primitives.py`、`composite/core/media_drop_payload.py`、`composite/core/tile_padding_computer.py` 误删 `field` 导入（`field(default_factory=...)` 仍在用），导致 composite 测试大面积收集失败；已对 `primitives.py` 做一行恢复（其余两处并行会话已自行修复）。该修复不属于本模块范围，仅用于解除验证阻塞，如 render3d 会话需要可自行重做。

## 5. 改动文件清单（42，均在 `src/features/composite/ui/`）

asset_dependency_panel 未改（其 AUD-9408 为行为项，保留）。
backdrop_item.py, composite_page.py, composite_render_dialog.py, composite_viewport.py,
connection_item.py, graph_editor_view.py, graph_layout.py, graph_node_search_popup.py,
group_commands.py, import_report_panel.py, keyframe_editor.py, keyframes_model.py,
keyframes_panel.py, keyframes_timeline_view.py, keyframes_tree_view.py, minimap_widget.py,
node_editor_actions.py, node_editor_widget.py, node_graph_commands.py, node_item.py,
node_properties_panel.py, node_search_popup.py, node_thumbnail.py, perf_panel.py,
port_item.py, preview_connection_item.py, project_settings_section.py, scene_outliner_panel.py,
spline_commands.py, spline_model.py, spline_view.py, sticky_note_item.py, timeline_commands.py,
timeline_expression.py, timeline_icons.py, timeline_layout.py, timeline_shortcuts.py,
timeline_time_display.py, viewer_handle_drawing.py, viewport3d_controller.py, viewport3d_toolbar.py,
__init__.py 以外共 42 个（见 `git diff --name-only -- src/features/composite/ui/`）。

范围外改动（1 个，解除验证阻塞）：`src/features/composite/render3d/primitives.py`（恢复 `field` 导入，见 §4.4）。
