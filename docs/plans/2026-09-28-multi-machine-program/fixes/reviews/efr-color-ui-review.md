# EFR 复查：color/ui（E-rollup 模块行）

- 复查对象：`src/features/color/ui/` ｜ 提交 `844d679081d825d894ff3595e11c50296171982f`
- 执行报告：`fixes/reports/efr-color-ui.md`
- 复查日期：2026-10-01 ｜ 复查方式：只读（git show/grep/py_compile），未运行 pytest
- 复查人：GLM5.3 复查会话

## 七项判定

| # | 检查项 | 判定 | 说明 |
|---|--------|------|------|
| 1 | 越界 | ✅ PASS | 28 个文件 = 27 个 `src/features/color/ui/*.py` + 本批报告；REMEDIATION_PLAN.md 未触碰 |
| 2 | 未用导入删除抽查 | ✅ PASS | 抽 10+ 名字（canvas 3、presets_panel 7、mask_panel 3），当前全仓零残留、零模块限定引用 |
| 3 | 死代码删除抽查 | ✅ PASS | 9 个被删函数全仓零引用；"发现但未删"公共 API 候选抽验仍在（保守正确）；`stroke_finished` 未 emit 信号按红线保留，实测 canvas.py:3685 定义仍在 |
| 4 | 注解零行为证明 | ✅ PASS | 399 处均为 `-> None` 追加（AST 无带值 return/无 yield 才加）；8 处浮点字面量规范化逐条核验等值 |
| 5 | 红线 | ✅ PASS | diff 中 12 处含 `pyqtSignal` 的行全部为 import 行剔除未用名（`Qt` 等），`pyqtSignal` 名均保留，`.connect(` 零改动；裸 `except:` 当前 0 命中；无 class 行改动；公共 API 零改名 |
| 6 | py_compile | ✅ PASS | 当前 HEAD 抽 10 个改动文件（canvas/color_wheel/crop_panel/mask_panel/plugin_panel/presets_panel/tracker_panel/timeline_clip_strip/layer_panel/raw_panel）全部通过 |
| 7 | 综合结论 | **PASS** | 机械清理成立；信号零触碰的声称经逐行核验为真 |

## 抽查明细

### 未用导入（抽 13 名字）

| 被删名字 | 所在文件 | 当前全仓 grep | 判定 |
|----------|---------|--------------|------|
| `QGraphicsPathItem` / `QComboBox` / `GLContextManager` | canvas.py | `canvas.<名>` 限定零命中；再导出通道 `from ...color.ui.canvas import` 仅取 `Canvas` 类 | 安全 |
| `tempfile` / `datetime` / `QStyleOptionViewItem` / `QListView` | presets_panel.py | 文件内残留 0（含 `QApplication/QPoint/QModelIndex/QFont/QAction` 复核 grep 计数 0） | 安全 |
| `MaskShape` / `LAYER_LIST_STYLE` / `IconColors` | mask_panel.py | `mask_panel.<名>` 零命中；`MaskShape` 在 canvas.py 定义且经 `__init__.py` 由 mask_panel 正常再导出的路径未受影响 | 安全 |
| `QApplication` / `QPoint` 等 | presets_panel.py | 同上 | 安全 |

`from src.features.color.ui.<mod> import` 全仓扫描：仅引入保留的类（Canvas/GradingPanel/MaskPanel/PluginPanel/PresetsPanel/TrackerPanel/TimelineClipStrip + `_color_stack_has_grade`），无一被删名字。

### 死代码（9 函数 + 2 只写属性）

| 被删定义 | 全仓 grep | 判定 |
|----------|-----------|------|
| `_update_curve_preview` | 零命中；其声称的失效依赖 `_curve_mode_active`/`curve_preview_path` 在当前 canvas.py 亦零出现（确为从未赋值） | 安全 |
| `_draw_bezel` / `_draw_polar_grid` | 零命中 | 安全 |
| `_setup_action_buttons` / `_on_rotation_changed` | 零命中（后者引用不存在的 `rot_value_label`，调用即崩，删除正确） | 安全 |
| `_float_to_int` / `_int_to_float` | 零命中 | 安全 |
| `_format_frame_range`（模块级） | 零命中；main_window.py:1473 从 timeline_clip_strip 仅导入 `_color_stack_has_grade` | 安全 |
| `_vertical_separator` | 零命中；保留的 `_separator` 未动 | 安全 |
| `self.is_panning` / `self._pending_grading_params`（只写属性） | canvas.py 当前零出现 | 安全 |

### 浮点字面量规范化（8 处）逐条核验

- `self.changed.emit(0, 0, 0)` → `(0.0, 0.0, 0.0)`：信号为 `pyqtSignal(float, float, float)`，int 实参本被 PyQt 强制转 float——值恒等。
- `set_params` 默认值 `= 0` → `= 0.0`（color_wheel vibrance/mid_det/hue；mask_panel blur/clean_black/clean_white）：参数本就注解为 `float`，`0 == 0.0` 恒真、算术恒等；仅当值被原样文本序列化时 repr 不同（`0` vs `0.0`），语义无差。判定：可接受（见发现 1）。

## 发现

1. **（Suggestion 级备忘）** `= 0` → `= 0.0` 属"值级"而非"纯声明级"改动，严格口径下超出"零字节行为变化"，但为 float 注解参数的等值规范化且已披露、定向测试全绿（中心已复跑），不构成回退理由。后续同类批建议将默认值规范化单列披露类别。
2. **（Note）** 报告与 git stat 一致：27 文件 +453/−573（586−133 报告行 / 573）。
3. **（Note）** 死代码删除的"公共 API 未删候选"清单（crop_panel.set_crop_aspect 等 11 方法 + 8 property）抽验仍在源码中——按红线保守保留并留人工裁决，处理正确。

## 结论

**PASS**。信号与 connect 零触碰经逐行核验成立；删除项证据充分。建议勾选该 E-rollup 行。
