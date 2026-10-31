# 复查报告：efr-edit-ui（E-rollup 机械清理批）

- 复查对象：提交 `b2977dcb9a0599584bc95b2ab9fcb5ceb3dfb746`（模块行 `src/features/edit/ui/`，173 条 process 级）
- 执行报告：`fixes/reports/efr-edit-ui.md`
- 复查日期：2026-10-01（GLM5.3 复查会话，只读复核，未运行 pytest，未做任何 git 写操作）

## 七项判定

| # | 检查项 | 判定 | 依据 |
|---|--------|------|------|
| 1 | 越界 | **PASS** | 30 个 .py 全部在 `src/features/edit/ui/` + 1 个报告文件；tests/ 零触碰；REMEDIATION_PLAN.md 零触碰 |
| 2 | 未用导入删除 | **PASS** | 声称 125 条：38 个单行名脚本核验（2 残留均为注释）+ 35 个多行名跨 8 文件手工核验，全部零代码引用 |
| 3 | 死代码删除 | **PASS** | 净删除 def/class 恰为 `_color_to_qcolor`/`_color_swatch_style` 2 个；常量 `_IDENTITY`/`_ASPECT_RATIOS` 及孤儿导入 `linear_rgba_to_qcolor` 全仓零引用 |
| 4 | 注解零行为 | **PASS** | 98 处全声明级；新注解引用的类型名（QTreeWidgetItem/QListWidgetItem/QEvent/QPoint/QPointF）在文件内均有 import；惰性类用引号注解 |
| 5 | 红线 | **PASS** | 信号零触碰（pyqtSignal 仅出现在 import 行重建，pyqtSignal 本名全部保留；`.connect(` 增删均为 0）；零裸 except 新增；GL/paint 热路径仅声明级注解 |
| 6 | py_compile | **PASS** | 全部 30 个改动源文件在当前 HEAD 下 `py_compile` 通过 |
| 7 | 综合结论 | **PASS** | — |

## 抽查明细

### 未用导入（125 条，8 个代表文件 35 名手工核验）

| 文件 | 抽验名（当前 HEAD 文件内词边界命中数） |
|------|--------------------------------------|
| media_pool_panel（12 条最多） | QDesktopServices/QAction/icon_settings/icon_new_folder/check_media_availability/get_offline_assets/EDIT_COLORS/get_size/panel_style/input_style/tool_button_style/is_media → 全部 **0** |
| program_viewer | GL_RGB/GL_INFO_LOG_LENGTH/QDialogButtonBox/QScreen → 0 |
| edit_properties_panel | QTreeWidget/STYLE_FIELDS/LINK_ICON_COLOR_OFF → 0 |
| timeline_view | SnapConfig/RippleDeleteCommand/TimelineHit → 0；QMimeData 仅 1 处命中且为 L13131 注释文字 |
| timeline_panel | TimelineEditTool/QTimeEdit → 0（TRACK_HEADER_WIDTH 保留属实） |
| scene_cut_dialog | get_active_tasks_registry/CancelToken/QItemSelectionModel → 0 |
| source_viewer | get_icon/GL_TRUE → 0 |
| caption_workspace | CaptionRole/CaptionStatus/EditTrack/UpdateCaptionMetadataCommand → 0（SubtitleClip/TimelineSequence 保留属实） |

单行名脚本核验 38 名，残留仅 2 处：stem_export_dialog `Qt`（`# noqa — Qt 命名` 注释）、timeline_items `EDIT_COLORS`（`# Colors from EDIT_COLORS` 注释）——均散文，零行为。

信号红线细查：5 组 `pyqtSignal` 相关行均为多名字 import 行重建（如 `Qt, pyqtSignal` → `pyqtSignal`，删 Qt 留 pyqtSignal；`..., QRectF,` 续行删 QRectF），**pyqtSignal 全部保留，信号定义与 connect 零触碰**。

### 死代码（4 条 + 孤儿导入 1）

| 项 | 全仓 .py 引用 |
|----|--------------|
| `_color_to_qcolor` / `_color_swatch_style`（comp_clip_settings_dialog） | 0 |
| 孤儿导入 `linear_rgba_to_qcolor` | 家模块为 `src/shared/ui/widgets/color_picker_control.py`；teaching_animation/scene_inspector 的使用是从**家模块自己的 import**（L20），与本文件删除无交集 |
| `_IDENTITY`（clip_transform_handle_controller） | 0（tests 中 `_IDENTITY_TRANSFORM`/`_IDENTITY_CROP` 词边界不匹配，与报告取证一致） |
| `_ASPECT_RATIOS`（new_timeline_dialog） | 0（`PIXEL_ASPECT_RATIOS` 在用，未误删） |

### 注解（98 处）

- 91 个 `-> None`：program_viewer 的 `initializeGL/resizeGL/paintGL/_paintGL_impl` 等 Qt 覆写加 `-> None` 属声明级（PyQt 不从返回注解推导元数据，与 `@pyqtSlot` 不同）；edit_properties_panel 42 处同理。
- 3 处手判返回类型：`_decode -> tuple[np.ndarray | None, int, int]`、`_ensure_sheet -> "TakeSheet"`、`take_sheet -> "TakeSheet | None"`（引号+future-annotations 双保险，惰性导入不受影响）。
- 9 参数注解类型名可用性逐一确认：media_pool_panel QTreeWidgetItem(26 处在用)、ai_preferences_page QListWidgetItem(7)、edit_effects_panel QEvent(5)/QPoint(11)、timeline_view QPointF(18)——无"注解引用了已删导入"的运行时 NameError 风险。

## 发现

- **Critical：0；Suggestion：0；Note：1**
- Note：timeline_view.py L13131 注释仍提及 QMimeData（描述历史 fallback 行为），与导入删除后状态无冲突，纯注释，不要求整改。

## 结论

**PASS**。125 条导入删除、4+1 条死代码全部零引用可证；98 处注解纯声明级且类型名全部在档；信号/GL/except 三条红线零触碰；30 文件编译通过。报告声称的"约 90 处复杂注解按宁缺勿错放弃"与 diff 实况一致，无打勾造假。
