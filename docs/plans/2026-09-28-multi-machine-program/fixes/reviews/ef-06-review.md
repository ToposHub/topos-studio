# EF-06 复查报告（GLM5.3）—— src/features/composite/ui 21 条

- 复查日期：2026-10-02 ｜ 复查会话：GLM5.3（只读复核，未改任何 src/tests/计划文件，未跑 pytest）
- 批提交：`ba0a770f7e913391cc436c02c4776fb714cc3e3a`（dev 分支）
- 白名单：`src/features/composite/ui/`（∪ tests/ ∪ fixes/reports/）
- 关联跨批提交：`2b71e383a`（efr-composite-ui 批，含本批 AUD-11310 契约反转的既有断言更新 + ef-06 报告补充披露）

## 最终结论：**PASS**（附 3 条 Note，无 Critical、无 ISSUE）

---

## §6 逐项判定

### 1. 越界检查 —— 通过

`git show --stat ba0a770f7` 共 17 个文件：

| 类别 | 文件 | 判定 |
|----|------|------|
| 源码 15 个 | keyframes_tree_view / material_panel / node_graph_commands / perf_panel / scene_outliner_panel / timeline_expression / timeline_expression_editor / timeline_shortcuts / viewer_handles / viewport3d_state / `__init__` / composite_page / composite_viewport / graph_model / graph_view（均 .py） | 全部 ⊆ 白名单 |
| 测试 1 个 | tests/composite/test_ef06_aud.py（新建，726 行 27 测试） | 允许 |
| 报告 1 个 | fixes/reports/ef-06.md | 允许 |

- REMEDIATION_PLAN.md / e_flash_batches.json / FIXLOG.md 均未出现在 diff 中。
- **AUD-11310 跨批披露核实（2b71e383a）**：该提交对 `tests/composite/test_fix_wd4g28_aud.py::TestAud11306` 的改动与披露完全一致——
  - `test_mask_corner_accepts_qt_enum_and_shift_locks_uniform` → 改名 `..._plain_drag_is_uniform`，断言反转为 plain=等比 / Shift=自由轴；
  - `test_mask_corner_without_shift_free_axes` → 改造为 `test_mask_corner_qt_enum_still_accepted`（保留 AUD-11306 的 Qt 枚举归一化守卫本意）；
  - `test_roto_delegation_accepts_qt_enum` 的 modifiers 由 Shift 改为 0（roto 委托路径同受新契约影响）。
  - 共 3 个既有测试更新，与"3 个既有断言更新"披露吻合；ef-06.md 的"补充披露（2026-10-02）"段落存在且内容相符。**判定：披露一致，越界豁免成立。**
  - 同提交还含 `src/features/composite/render3d/primitives.py`（-1/+1）。净 diff 为删除未用 `Tuple` 导入（`field` 在上下文行保留——即"恢复被并行代理误删的 field"后的最终净态）。该文件属 efr 批范围、非 EF-06 提交内容，报告已披露；Tuple 确认全文件零引用，删除安全。**记为关联披露，不构成本批越界。**

### 2. 真实修复（逐条独立核实，全部 CONFIRMED）

对每条 ID 均执行了：grep REMEDIATION_PLAN.md 指控 → 读 ef-06.md 结论 → 当前 HEAD 源码 file:line 独立核对。逐 ID 表见下节。

**重点条目独立判断：**

- **AUD-11310 契约反转方向 = 真修复，非测试迁就 bug。**
  - 审计原文（batch-113:183-195）指控的本质是"同一软件内 Transform 与 Mask 修饰键语义相反"，建议方向明确写为"**建议向 Transform 的 Fusion 式默认看齐**"（即普通拖拽=等比、Shift=解锁双轴）。
  - 当前源码核实：`viewer_handles.py:1797` `free_axes = (modifiers & _SHIFT_MODIFIER_MASK) != 0`；`:1833-1838` 非 free_axes 时 `avg_scale = (width/orig_w + height/orig_h) / 2.0` 折叠双轴——scale 平均法与 Transform 工具（审计证据所引 1012 行 `avg = (sx_new + sy_new) / 2.0`）一致，实现正确。
  - 既有测试 TestAud11306 的更新是追随审计建议的新契约（且保留了 AUD-11306 枚举守卫的测试本意），不是掩盖缺陷。**CONFIRMED。**
- **AUD-11307 INVALID 独立验证成立。** 全库 grep 复核六个导出：`get_transform_handle_definitions` / `find_nearest_handle` 被 `src/features/edit/ui/clip_transform_handle_controller.py`（:35/:223/:238）与 `subtitle_handle_controller.py`（:45/:268/:280）**生产调用**；`hit_test_handle_screen` 被 tests/ui/test_viewer_tool_system.py（:326-340）锁定；`get_mask_handle_definitions` / `get_roto_handle_definitions` 被 tests/composite/test_composite_mask_roto_handles.py 锁定。审计"全库 0 调用方"的核心事实不成立（审计时点 2026-09-28，edit/ui 调用方为审计后引入）。唯一真零引用的 `compute_cornerpin_delta_params` 已加 `DORMANT(2026-10-01 审计 AUD-11307)` 标头（viewer_handles.py:1050-1053），与 viewer_handle_drawing.py 先例一致。**INVALID + 附带 DORMANT 标注的处置正确。**

### 3. 红线扫描 —— 通过

对 `git show ba0a770f7` 全部新增行扫描：

- 裸 `except:`：0 处新增。
- 吞错 `except Exception: pass`：0 处新增。新增捕获仅两处且均收窄——graph_view.py `_forward_canvas_signals` 的 `(TypeError, RuntimeError)`（连接失败防御，注释留痕）；timeline_expression.py 的 `(TypeError, ValueError, RecursionError)` + `logger.warning` 留痕。
- UI 线程 ModernGL：无。AUD-9714 仅把 UI 线程 paint 内的纯 Python 配置合并调用提出循环，无 GL 对象操作。
- HDR 钳制：无新增数值钳制；AUD-11310 等比缩放沿用 scale 平均算法。
- 601/709 矩阵：未触碰。
- Note A（非新增）：perf_panel.py `_trace_snapshots()` 内存留一个既有 `except Exception: pass`（约 :103-107），本批未触碰（diff 中为上下文行）。最小改动原则下可接受，建议后续批次顺手收窄。

### 4. 并发 / 资源 / 信号 —— 通过

- AUD-10309/10311 即信号防递归锁的 try/finally 化（material_panel.py:247-295、361-371 核实）。
- AUD-10610 copy-on-write 消除锁外改写（node_graph_commands.py Move/Resize/Modify 六处 redo/undo 全部核实，`dict(current)` + `set_backdrop()`）。
- AUD-9916 占位页 `deleteLater` 释放、AUD-10305 滚出视口控件 pop+deleteLater——Qt 对象生命周期修复到位。
- 无锁内重活新增。

### 5. 测试质量 —— 通过

tests/composite/test_ef06_aud.py（726 行，27 测试）逐 ID 覆盖（AUD-11310 双测试）。抽查断言质量：

- `test_aud9508_publish_is_generation_gated`：以 `to_dict_calls` 计数断言"同 generation 不重复序列化、bump 后恰好一次"——精确体现门控语义。
- `test_aud11310_mask_corner_plain_drag_is_uniform` / `..._shift_frees_axes`：纵横比保持 / 数值级独立轴断言（含期望值注释推导）。
- `test_aud10816_filter_reveals_full_ancestor_chain`：断言直接父级可见+展开（连带修复旧实现差一问题）。
- `test_aud9714_quad_clear_color_resolved_outside_quadrant_loop`：AST 级断言循环体内无配置合并调用。
- 弱断言评估：AUD-9711 / 11307 / 9409 / 11311 / 11410 / 11027 为源码文本/docstring 断言——对"删除不可达死代码 / 文档对齐 / DORMANT 标注"类修复，行为不可达或无运行时差异，文本断言是合理手段，且 11027 同时附键事件行为断言（plain Enter 不提交、Ctrl+Enter 提交）。无可疑造假断言。

### 6. 回退验证建议（随机挑 2 条，供中心会话抽查回归有效性）

1. **AUD-11021**：文件 `src/features/composite/ui/timeline_expression.py`，删除或注释 :197-204 的空文本短路块（当前原文 `if not text.strip(): return ExpressionStatus(state=EXPRESSION_ERROR, ..., error_code=EXPR_ERR_EMPTY, ...)`），恢复为直接进入 parser 校验路径 → 应失败节点：`tests/composite/test_ef06_aud.py::test_aud11021_empty_expression_yields_expr_empty`（同时 `tests/composite/test_timeline_expression.py` 中空文本相关用例若断言 EXPR_ERR_EMPTY 亦应失败）。
2. **AUD-9508**：文件 `src/features/composite/ui/composite_page.py`，将 :5611-5625 的 generation 门控改回无条件 `graph_data = self._node_graph.to_dict()` + 深比较（删除 `graph_generation != getattr(self, "_last_published_graph_generation", -1)` 外层条件）→ 应失败节点：`tests/composite/test_ef06_aud.py::test_aud9508_publish_is_generation_gated`（第二次调用后 `to_dict_calls` 将由 1 变 2）。

## 逐 ID 判定表（21/21）

| ID | 报告结论 | 复查判定 | 当前 HEAD 证据（file:line） |
|----|---------|---------|---------------------------|
| AUD-10305 | FIXED | CONFIRMED | keyframes_tree_view.py:574-585（pop+hide+deleteLater，AUD 注释在） |
| AUD-10309 | FIXED | CONFIRMED | material_panel.py:246-295（`_guard=True` 整段 try/finally，finally 复位） |
| AUD-10311 | FIXED | CONFIRMED | material_panel.py:361-371（程序化 setCurrentIndex(0) 持 guard try/finally） |
| AUD-10610 | FIXED | CONFIRMED | node_graph_commands.py:822-842/850-870/883-903（六处 copy-on-write） |
| AUD-10809 | FIXED | CONFIRMED | perf_panel.py:25-27（`_TRACE_COUNT_REFRESH_INTERVAL=60`）、41-44（缓存字段）、201-211（低频刷新）、110-118（export 前精确刷新 `refresh_trace_count`） |
| AUD-10816 | FIXED | CONFIRMED | scene_outliner_panel.py:314-341（单遍递归 `_visit`，O(n)，自底向上折叠+顺带修差一） |
| AUD-11020 | FIXED | CONFIRMED | timeline_expression.py:226-234（直接 `ExpressionParser().validate`，收窄 `(TypeError, ValueError, RecursionError)` + logger.warning；崩溃映射 EXPR_ERR_UNKNOWN） |
| AUD-11021 | FIXED | CONFIRMED | timeline_expression.py:197-204（空文本短路 EXPR_ERR_EMPTY "Expression is empty"） |
| AUD-11027 | FIXED | CONFIRMED | timeline_expression_editor.py:9/27/60-63 三处文档统一为 Ctrl/Cmd+Enter 提交（实现零改动，测试附行为断言） |
| AUD-11108 | FIXED | CONFIRMED | timeline_shortcuts.py:148-155（`command_for` 别名回退）、181-215（`_swap_cmd_ctrl`；精确匹配优先，双拼写可独立绑定） |
| AUD-11307 | INVALID + DORMANT | CONFIRMED（INVALID 事实独立复核成立） | viewer_handles.py:1050-1053 DORMANT 标头；调用方 grep 证据见 §6.2 |
| AUD-11310 | FIXED | CONFIRMED（方向=审计建议的 Fusion 式统一，见 §6.2 重点判断） | viewer_handles.py:1677-1680/1787-1797/1833-1838 |
| AUD-11311 | FIXED | CONFIRMED | viewer_handles.py:1186-1189、1380-1382（feather 文档改 0-0.5，与 ParamDef max_val=0.5 一致） |
| AUD-11410 | FIXED | CONFIRMED | viewport3d_state.py:49-52（PREVIEW 文档改为与 FINAL 同参表述） |
| AUD-9409 | FIXED | CONFIRMED | `__init__.py`:1-12（docstring 只列真实导出） |
| AUD-9508 | FIXED | CONFIRMED | composite_page.py:274-277（门控字段）、5607-5625（generation 比对门控，保留 payload 等值守卫）、844/2139（reset 置 -1）、3050-3053（发布后同步门控） |
| AUD-9711 | FIXED | CONFIRMED | composite_viewport.py：`_space_pressed` stub 及恒真分支已删（11087 留注释说明；全文件无 `def _space_pressed`），docstring 同步 |
| AUD-9714 | FIXED | CONFIRMED | composite_viewport.py:5168-5171（`_viewport_bg_color_rgb()` 提至 QUAD_VIEW_ORDER 循环外，每帧 4→1 次） |
| AUD-9908 | FIXED | CONFIRMED | graph_model.py:546-577（按索引排序 + 散射回原顺序，docstring 同步） |
| AUD-9916 | FIXED | CONFIRMED | graph_view.py:105-119（None 分支与安装路径一致的 removeWidget+setParent(None)+deleteLater） |
| AUD-9917 | FIXED | CONFIRMED | graph_view.py:126-131/151-200（`_forward_canvas_signals` 统一两画布接线；六信号；id 集合幂等；异常收窄） |

## 红线发现汇总

无 Critical。1 条非新增 Note（perf_panel 既有 `except Exception: pass` 未顺手收窄，属最小改动原则内的遗留）。

## 值得中心注意的事项（Note）

1. **AUD-11310 是用户可见的交互行为变更**：Mask/Roto 角点拖拽默认行为从"自由缩放"反转为"等比"（Shift 语义反转）。审计原文为"与产品确认后统一（建议向 Transform 看齐）"，执行按审计首选方向实施且已充分披露、测试同步。建议产品侧确认该默认值，如需回转仅涉及 viewer_handles.py:1797 一处布尔语义 + 3 个测试文件断言。
2. **AUD-11307 的 INVALID 事实**说明 2026-09-28 审计的"零调用"结论在 edit/ui 功能落地后已过时——后续批次遇到"死代码删除"类指控时，执行/复查双方都应重跑调用方 grep（本批执行方做了，值得保持）。
3. AUD-9714 中审计提及的另一证据点（`_collect_node_icons` 修订号缓存）报告已声明为独立较大改造未纳入本条，边界划分合理，不在本批判定范围。
