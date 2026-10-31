# EFR 复查：composite/ui（E-rollup 模块行）

- 复查对象：`src/features/composite/ui/` ｜ 提交 `2b71e383a28ce97900020f186f1da83dfde0e304`
- 执行报告：`fixes/reports/efr-composite-ui.md`（连带披露另见 `fixes/reports/ef-06.md` 补充披露节）
- 复查日期：2026-10-01 ｜ 复查方式：只读（git show/grep/py_compile），未运行 pytest
- 复查人：GLM5.3 复查会话

## 七项判定

| # | 检查项 | 判定 | 说明 |
|---|--------|------|------|
| 1 | 越界 | ✅ PASS（带披露核对） | 46 文件 = 42 个 `src/features/composite/ui/*.py`（+700/−803，与报告分毫一致）+ 本批报告（+108）+ ef-06.md 补充披露（+10）+ `tests/composite/test_fix_wd4g28_aud.py`（+18/−8，tests/ 白名单内且已披露）+ `src/features/composite/render3d/primitives.py`（范围外 1 行，报告 §5 已披露为"范围外改动"——**但披露内容与 diff 不符，见发现 1**）；REMEDIATION_PLAN.md 未触碰 |
| 2 | 未用导入删除抽查 | ✅ PASS | 抽 13 名字（含高危面：composite_viewport 删除的 viewer_tool_system 10 个函数/类型导入），当前全仓零 `composite_viewport.<名>` 引用、零文件内残留 |
| 3 | 死代码删除抽查 | ✅ PASS | 3 个被删方法（`_add_to_port`/`_remove_from_port`/`_do_keyframe_drag`）零引用；`_do_keyframe_drag` 其余命中确属其他类同名方法（graph_editor_view.py:816、teaching_animation），报告预判准确；保留项 `_update_param_widget_value` 确有测试直调（test_node_properties_panel_coverage.py:984） |
| 4 | 注解零行为证明 | ✅ PASS | 627 处 `-> None` + 7 参数注解抽验均为声明级；spline_commands 的 `key: "SplineKey"` 为字符串注解（TYPE_CHECKING 导入，运行时零开销） |
| 5 | 红线 | ✅ PASS | diff 中 12 处含 `pyqtSignal` 的行全部为 import 行剔除未用名，`.connect(` 零改动；裸 `except:` 当前 0；AUD-11310 断言更新方向与 HEAD 代码契约一致（viewer_handles.py "matching the Transform tool's Fusion-style default"）且属 ef-06 批既定修复的测试跟进，非本批行为改动 |
| 6 | py_compile | ✅ PASS | 当前 HEAD 抽 10 个改动文件（composite_page/composite_viewport/node_editor_widget/node_properties_panel/keyframe_editor/spline_view/connection_item/viewer_handle_drawing/keyframes_timeline_view/backdrop_item）全部通过；tests/composite/test_fix_wd4g28_aud.py 亦编译通过 |
| 7 | 综合结论 | **PASS-with-notes** | 机械清理本体全部通过；两处披露口径瑕疵（发现 1/2）建议中心知悉，不构成回退理由 |

## 抽查明细

### 未用导入（13 名字）

| 被删名字 | 所在文件 | 当前验证 | 判定 |
|----------|---------|---------|------|
| `apply_angle_constraint` / `apply_aspect_ratio_constraint` / `compute_constrained_delta` / `compute_reformat_rect_from_handles` / `get_cursor_for_handle` / `snap_to_format_edge` / `snap_to_format_center` / `TOOL_MODE_LABELS` / `HandleType` / `SnapConfig` | composite_viewport.py（自 viewer_tool_system 导入） | `composite_viewport.<名>` 全仓零命中；10 名在定义处 viewer_tool_system.py 均保留（函数未删） | 安全 |
| `NODE_REGISTRY` 等 11 名 | node_editor_widget.py | 文件内残留消费 0 | 安全 |
| `make_param_row_id`（函数级导入） | keyframes_panel.py | 报告披露；定义处保留 | 安全 |

包级再导出面：`src/features/composite/ui/__init__.py` 本批未改动；test_fix_wd4g28_aud.py 经包导入的仅是子模块引用（ts/ttd/tvm/vhd/vts），无被删名字。

### 死代码（抽 5/12 项）

| 被删项 | 全仓 grep | 判定 |
|--------|-----------|------|
| `_remove_from_port` / `_add_to_port`（connection_item） | 零命中 | 安全 |
| `_do_keyframe_drag`（keyframes_timeline_view legacy shim） | keyframes_timeline_view.py 内零残留；其余命中为 graph_editor_view.py / teaching_animation 各自同名方法（有各自调用方，未动） | 安全 |
| `GL_TRUE = 1`（viewer_handle_drawing 本地常量） | viewer_handle_drawing.py 零残留；composite_viewport 等处的 GL_TRUE 为各自 OpenGL 导入 | 安全 |
| `_PARAM_CHANNEL_COLORS`（keyframes_model） | 零命中 | 安全 |
| `_was_moved` 只写属性（backdrop_item） | 赋值行全删、零读取 | 安全 |

### AUD-11310 断言更新（tests/composite/test_fix_wd4g28_aud.py）核对

- diff 内容：`TestAud11306` 内 3 个测试更新——① `..._shift_locks_uniform` 改名 `..._plain_drag_is_uniform` 且断言反转（plain=等比、Shift=释放轴向）＋新增 Qt 枚举用例；② `..._without_shift_free_axes` 由 `..._qt_enum_still_accepted` 取代（保留 AUD-11306 枚举守卫本意）；③ roto 委托用例 modifiers 从 ShiftModifier 翻转为 0。
- 与 ef-06.md 补充披露（"3 例断言更新至新契约 + 新增 1 用例"）内容一致；与 HEAD 代码（viewer_handles.py:1651 起 compute_mask_delta_params / :1905 compute_roto_delta_params 的 Fusion 式默认等比契约）方向一致。
- 中心已统一复跑测试全绿（本复查未跑 pytest）。

## 发现

1. **（Suggestion）primitives.py 披露与 diff 不符**：提交信息与报告 §4.4/§5 均称该文件改动为"恢复被并行误删的 `field` 导入"，但实际 diff 为 `from typing import Mapping, Tuple` → `from typing import Mapping`（**删除未用 `Tuple`**）；`from dataclasses import dataclass, field` 在父提交 f0b117124 中已原样在位（field 的"误删-恢复"未进入本提交的 git 轨迹，仅存在于工作树叙事）。实际改动本身无害（当前文件 `Tuple` 零使用、`field` 在 :44-45 活跃使用，py_compile 通过），但**提交信息描述的改动内容与 diff 不一致、真实改动（Tuple 删除）未按其类别披露**。建议中心记录：多机并行场景下，提交信息应以最终 diff 为准复核后再写。
2. **（Note）ef-06.md 措辞**："新增 `test_mask_corner_qt_enum_still_accepted`"实为对 `test_mask_corner_without_shift_free_axes` 的改名取代（净测试数不变），非净新增。语义无损，措辞精度问题。
3. **（Note）两份报告的因果叙述拼接**：efr-composite-ui.md §4.2 以影子树证明 3 处失败"与本次改动无关"（对机械清理成立），ef-06.md 补充披露则确认根因是 ef-06 批 AUD-11310 契约反转——两者合读因果闭环，最终以断言更新收口，处理正确。
4. **（Note）统计精确**：42 个 ui 文件 +700/−803 与报告逐字一致；范围外 4 文件（2 报告 + 1 测试 + 1 primitives）全部可对应披露条目。

## 结论

**PASS-with-notes**。机械清理本体（导入/死代码/注解/红线/py_compile）六项全过；AUD-11310 测试跟进披露内容与 diff 一致。唯一实质瑕疵是 primitives.py 的改动内容描述失真（发现 1），属披露质量问题而非代码质量问题——已核实该行改动安全。建议勾选该 E-rollup 行，并将发现 1 记入中心多机协同注意事项。
