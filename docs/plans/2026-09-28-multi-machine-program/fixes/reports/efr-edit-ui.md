# EFR 修复报告 — src/features/edit/ui（E-rollup 模块行，173 条 process 级改进）

日期：2026-10-01 ｜ 分支：dev（未做任何 git 写操作，工作树留改动） ｜ 性质：**纯机械清理，零行为变化**

## 总览

| 类别 | 处理量 | 结果 |
|------|--------|------|
| 1. 未使用 import 删除 | **125 条**（模块级，AST + 全仓 grep 双重核实） | 全部删除，复扫归零；29 个文件 |
| 2. 死代码删除 | **4 条**（全仓零引用才删）+ 1 个孤儿 import | 全部删除 |
| 3. 类型注解补全 | **98 处函数签名**（91 返回 + 9 参数） | 8 个文件 |
| 4. 命名收敛 | 0 | 无安全候选（见"剩余量"） |
| 5. 小清理（unused locals / 重复字面量） | 0 | 未使用局部变量扫描零命中；重复字面量提取按"最小 diff"红线放弃 |

- 改动文件：**30 个**（`git diff --stat`：+143 / −238，插入行全部为注解改写，无逻辑行变更）。
- 信号定义/连接、GL 调用、except 语义：**一律未动**。paint/mouse 热路径仅做声明级注解（如 `paintEvent(self, event: QPaintEvent)`），未触碰任何逻辑。

## 核实方法（每条删除的证据链）

1. **AST 扫描**（模块级 import，跳过 `from __future__ import annotations`；函数体内惰性 import 单独豁免）：`Name` / `Attribute` 根名收集 + 字符串注解 / `__all__` / `__getattr__` 映射表字符串匹配 → 得 126 候选（含 1 条误报已排除，见下）。
2. **全仓 rg 核实**（src/tests/scripts/tools/apps/plugins/benchmarks）：
   - `mod.NAME` 属性访问（含 `import mod as alias` 场景）→ 0 命中；
   - `from ...edit.ui.<mod> import NAME` 再导出 → 0 命中；
   - 引用该模块的文件内 `"NAME"` 字符串引用（防 `getattr`/`monkeypatch.setattr`）→ 0 命中。
   - 唯一 SUSPECT：`caption_workspace.py` 的 `EditTrack` —— 命中的是其他模块自己的 `timeline_v2.EditTrack`，与本文件 import 无关，确认可删。
3. `__all__` 声明核对：14 个含 `__all__` 的文件逐一比对，无候选在列。
4. 删除后：复扫归零（模块级非 `__future__` 未使用 import = 0）+ `ast.parse` 逐文件验证 + 30 模块带 QApplication 导入冒烟全过。

### 删除抽样（代表性证据）

| 文件 | 删除项 | 核实要点 |
|------|--------|---------|
| `media_pool_panel.py` | `QDesktopServices` `QAction` `icon_settings` `icon_new_folder` `check_media_availability` `get_offline_assets` `EDIT_COLORS` `get_size` `panel_style` `input_style` `tool_button_style` `is_media`（12 条，全模块最多） | 全仓无 `media_pool_panel.X` 访问、无字符串引用；tests 无 monkeypatch |
| `program_viewer.py` | `QPushButton` `QApplication` `QLineEdit` `QDialog` `QDialogButtonBox` `QScreen` `GL_RGB` `GL_INFO_LOG_LENGTH`（8 条） | Qt/OpenGL 常量零使用 |
| `edit_properties_panel.py` | `QTreeWidget` `QTreeWidgetItem` `QAbstractItemView` `LINK_ICON_COLOR_OFF` `STYLE_FIELDS` `Tuple`（6 条） | `STYLE_FIELDS` 整行 import 删除 |
| `timeline_view.py` | `QMimeData` `QByteArray` `QUrl` `QGraphicsRectItem` `QStyleOption` `SnapConfig` `SnapPoint` `RippleDeleteCommand` `TRACK_RESIZE_HANDLE_HEIGHT` `TimelineHit` `LAYOUT_*`（11 条） | 保留 4 处函数内惰性 import（见下） |
| `timeline_panel.py` | `QPushButton` `QSplitter` `QSpinBox` `QTimeEdit` `QStyle` `QIcon` `QKeyEvent` `QFocusEvent` `QAction` `TimelineEditTool`（10 条） | `EditTool as TimelineEditTool` 别名删除，`TRACK_HEADER_WIDTH` 保留 |
| `scene_cut_dialog.py` | `Callable` `Enum` `QLabel` `QItemSelectionModel` `QColor` `QIcon` `get_active_tasks_registry` `CancelToken`（8 条） | |
| `source_viewer.py` | `QPushButton` `QRectF` `QBrush` `QFont` `get_icon` `GL_TRUE` `GL_INFO_LOG_LENGTH`（7 条） | |
| `caption_workspace.py` | `Tuple` `UpdateCaptionMetadataCommand` `CaptionCueMetadata` `CaptionRole` `CaptionStatus` `EditTrack`（6 条） | `SubtitleClip`/`TimelineSequence` 保留 |

其余 22 个文件各删 1–5 条，清单见 `git diff`（工作树未提交）。

## 死代码（全仓 grep 零引用，逐条证据）

| 位置 | 标识符 | 证据 |
|------|--------|------|
| `comp_clip_settings_dialog.py:70` | `def _color_to_qcolor` | rg 全仓仅命中定义行；删除后顺带删孤儿 import `linear_rgba_to_qcolor`（其唯一使用者即该函数） |
| `comp_clip_settings_dialog.py:80` | `def _color_swatch_style` | 同上（`_qcolor_to_rgba` 在用，保留） |
| `clip_transform_handle_controller.py:44` | `_IDENTITY` 常量 | tests 中 `_IDENTITY_TRANSFORM`/`_IDENTITY_CROP` 为不同标识符（词边界核实），本体零引用 |
| `new_timeline_dialog.py:90` | `_ASPECT_RATIOS` 常量 | 模块内仅 `PIXEL_ASPECT_RATIOS` 被使用；全仓零引用 |

## 类型注解（98 处签名，全部保守判定）

- **91 个返回注解**：仅对"无可值 return / 无 yield / 非纯 raise / 非纯 pass 桩"的函数补 `-> None`（AST 判定）；87 处集中在 `edit_properties_panel.py`(42)、`program_viewer.py`(36，含 `initializeGL`/`resizeGL`/`paintGL`/`_paintGL_impl`)、`source_viewer.py`(9)、`timeline_view.py`(1)；另有 3 处手判：`adr_take_sheet_panel._decode -> tuple[np.ndarray | None, int, int]`、`_ensure_sheet -> "TakeSheet"`、`take_sheet -> "TakeSheet | None"`（惰性导入类用字符串注解 + `from __future__ import annotations` 双保险）。
- **9 个参数注解**：仅在类型已 import 或确定时补——`edit_effects_panel.leaveEvent(event: QEvent)`、`_show_effect_context_menu(pos: QPoint)`；`timeline_view._show_empty_area_track_menu(global_pos: QPoint, scene_pos: QPointF)`、`_trim_monitor() -> "TrimMonitorDialog"`；`media_pool_panel._find_item(parent: QTreeWidgetItem, ...) -> QTreeWidgetItem | None`；`edit_properties_panel.eventFilter(obj: object, event: QEvent)`（对齐同文件 748 行既有签名）、`_rgba_to_hex(r: float, g: float, b: float) -> str`；`ai_preferences_page._on_profile_selected(_previous: QListWidgetItem | None)`。
- 安全校验：改后 AST 复查 —— 所有新标 `-> None` 的函数均无带值 `return`/`yield`（检查器报出的 28 条全部为 HEAD 既有注解的嵌套 helper 误报，逐一比对确认非本次改动）。

## 红线遵守

- 惰性 import 防护**全部保留**：`edit_page.py`(~2832 `import uuid`)、`timeline_panel.py`(~1815 `QDockWidget...`)、`timeline_view.py`(~7303/~7798) 及 scene_cut 系列函数内 import 未动，本报告在此注明。
- `from __future__ import annotations`（40 处被朴素扫描误报）**全部保留**。
- 未做任何 git add/commit；未触碰 `REMEDIATION_PLAN.md`；未改任何公共 API/信号名。

## 验证输出

1. `py_compile`：`src/features/edit/ui/*.py` 全部 62 文件 → **OK**。
2. 导入冒烟：30 个改动模块带 `QApplication` 逐一 `importlib.import_module` → **ALL OK**（唯一输出为 `viewport_toolbar_base.py` 既有 SyntaxWarning，与本次无关）。
3. 定向测试（7 个文件，**127 passed, 0 failed**；未整目录跑 tests/edit）：
   - `tests/edit/test_adr_take_sheet_ui.py` → 20 passed
   - `tests/edit/test_edit_properties_panel_layout.py` → 19 passed
   - `tests/edit/test_media_pool.py` → 40 passed
   - `tests/ui/test_ai_assistant_composer.py` → 36 passed
   - `tests/ui/test_ai_timeline_diff.py` → 8 passed
   - `tests/edit/test_scene_cut_graph_widget.py` → 3 passed
   - `tests/edit/test_scene_cut_dialog_formatting.py` → 1 passed
4. `grep -rn "except:" src/features/edit/ui/` → **零命中**（本模块无既有裸 except，无需记录）。

## 剩余量说明（未处理项与理由）

- **注解剩余约 90 处**（`edit_properties_panel` ~40、`timeline_view` ~20、`program_viewer` ~19 及零星）：多为 ①带默认参绑定的嵌套闭包（`def on_drag(v, k=key, ...)`，类型可推但收益低）、②Qt 事件处理器其事件类未在本文件 import（补 import 会扩大 diff 且被 lint 判"未使用"，如 `adr_take_sheet_panel.paintEvent`）、③返回类型不确定的惰性工厂（`source_viewer._open_cpu_decode_source`）。按"拿不准就不加"放弃。
- **命名收敛 0 条**：私有标识符全仓零引用者即死代码（已删 4 条），其余均有内部引用，重命名收益低于回归风险，全部放弃；公共 API/信号名一律未动。
- **重复字面量提取**：未做——提取需新增模块常量行，与"最小 diff、不重排"冲突，且字符串字面量多处承担 dict key/格式化双重语义，机械提取有行为风险。
- **冗余中间变量**：unused-locals AST 扫描全模块**零命中**，无安全处理对象（"被使用的中间变量"需语义判断，不属机械清理范围）。
- 函数级惰性 import（大量分布于 scene_cut_* 与超大三件套）为既定架构（见 `__init__.py` docstring），不在清理范围。
