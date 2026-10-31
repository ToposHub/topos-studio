# EFR Color UI 机械清理报告（E-rollup 模块行 src/features/color/ui/）

- 日期：2026-10-01
- 范围：`src/features/color/ui/`（36 个 .py 文件，约 23,276 行）
- 性质：纯机械清理，零行为变化；分支保持 dev；无任何 git 写操作
- 结果：**27 个文件修改，+453 / -573 行**

---

## 一、分类统计

| 类别 | 数量 | 说明 |
|------|------|------|
| 1. 未使用 import 删除 | 77 条（23 个文件） | AST 全量扫描 + 全仓引用核实后删除 |
| 2. 死代码删除 | 11 项（6 个文件） | 9 个零引用函数 + 2 个只写不读的属性 |
| 3. 类型注解补齐 | 399 处（24 个文件） | 仅补可证明的 `-> None`；参数注解按"拿不准就不加"原则跳过 |
| 4. 命名收敛 | 0 项 | 所有候选均为公共 API 表面（set_*/get_*/property），按红线全部不动 |
| 5. 小清理 | 8 处（2 个文件） | 浮点字面量规范化 + 只写属性清理 |
| 合计 | ~495 项 | |

### 1. 未使用 import（77 条）

判定流程：`ast` 解析模块级 import → 收集全部 `ast.Name` 使用点 → 剔除 `__future__`、`__init__.py` 再导出、函数内惰性导入（如 `layer_panel.py:383` 的 `Layer` 惰性导入**保留**）→ 全仓 AST 交叉核实无 `from src.features.color.ui.<mod> import <name>` 形式的再导出使用 → 删除。

抽样证据（删除前后）：

```python
# canvas.py（AUD-25209 佐证）
-     QGraphicsRectItem, QGraphicsPathItem, QWidget,      # QGraphicsPathItem 未用
-     QHBoxLayout, QVBoxLayout, QComboBox, ...            # QComboBox 未用
- from src.shared.assets.icon_manager import (icon_mask, icon_crop, get_icon, ...)
- from src.shared.gpu.gl_backend import GLContextManager   # 未用（基类持有 GL 上下文）

# presets_panel.py
- import tempfile / from datetime import datetime / QStyleOptionViewItem / QListView
- QApplication / QPoint / QModelIndex / QFont / QAction                    # 全部零引用

# mask_panel.py
- from typing import ... Union / QLabel 行内 QFrame, QGridLayout / QIcon, QPixmap
- MaskShape / LAYER_LIST_STYLE / IconColors
```

文件分布：adjustment_panel(4)、blur_sharpen_panel(3)、canvas(8)、color_wheel(4)、crop_panel(2)、denoise_panel(1)、dependency_report_dialog(1)、film_grain_panel(2)、hsl_curves_panel(1)、layer_panel(5)、lut_panel(1)、mask_panel(8)、mixer_panel(3)、plugin_panel(5)、presets_panel(9)、qualifier_panel(3)、raw_panel(6)、scope_panel(1)、soft_clip_panel(5)、timeline_clip_strip(2)、tools_panel(1)、viewer_guides(1)、warper_panel(1)。

### 2. 死代码（11 项，删除前逐项全仓 grep 复核 = 仅 def 自身）

| 文件 | 删除项 | 行数 | 佐证 |
|------|--------|------|------|
| canvas.py | `_update_curve_preview`（依赖从未赋值的 `_curve_mode_active`/`curve_preview_path`，调用即空操作） | 16 行 | AUD-25210 + 全仓零引用 |
| canvas.py | `self.is_panning = False`（平移逻辑全部使用 `_is_panning`，此属性只写不读） | 1 行 | AUD-25210 |
| color_wheel.py | `_draw_bezel`、`_draw_polar_grid`（paintEvent 不调用） | 33 行 | AUD-25307 |
| color_wheel.py | `self._pending_grading_params = None` ×3（仅赋值从未读取） | 3 行 | AUD-25307 |
| crop_panel.py | `_setup_action_buttons`（空壳 pass）、`_on_rotation_changed`（引用不存在的 `self.rot_value_label`，一旦调用即崩溃） | 7 行 | AUD-25312 |
| plugin_panel.py | `_float_to_int`、`_int_to_float` | 10 行 | batch-255 + 零引用 |
| timeline_clip_strip.py | `_format_frame_range`（模块级，零调用） | 4 行 | 零引用 |
| tracker_panel.py | `_vertical_separator`（模块级，零调用；`_separator` 有 4 处调用**保留**） | 7 行 | 零引用 |

删除脚本内置安全门：删除时再次 `grep -rwn --include=*.py <name> src tests scripts` 断言除 def 行外零命中，命中即中止。

**发现但未删除**（公共 API 表面/存档文件，谨慎保留，供后续人工裁决）：
- 零引用公共方法：`crop_panel.set_crop_aspect` / `get_aspect_ratio_value`（batch-253 通过项提及）、`on_reset_crop_rect_clicked`、`delivery_matrix_dialog.report_summary_line` / `show_report`（classmethod 构造器）、`denoise_panel.get_video_params_dict`、`layer_panel.set_offline_mode`、`mask_panel.is_brush_active`、`tracker_graph.set_controls_visible`、`tracker_overlay.clear_homography_corners`、`tracker_panel.set_tracking_result`（AUD-25807 按活代码评述）/ `set_homography_diagnostics` / `clear_homography_diagnostics`、`viewer_guides.set_active_guide`（batch-258 通过项提及）
- 零引用 property：`soft_clip_panel` 的 7 个 slider property、`tracker_overlay.tracking_mode`；`tracker_panel.graph_color`（staticmethod）
- `adjustment_panel.py` 整文件头部标注 SUPERSEDED 仅存档（batch-251 引用），仅清理其 import，不删文件
- `canvas.stroke_finished` 信号从未 emit（AUD-25210），但红线规定信号定义不动，**保留**

### 3. 类型注解（399 处 `-> None`，24 个文件）

判定条件（全部满足才补）：AST 验证函数体无带值 `return`、非生成器（无 yield）、无装饰器（property/staticmethod/pyqtSlot 等一律跳过）。插入方式：tokenize 定位签名冒号（括号深度感知），在冒号前插入 ` -> None`，逐文件插入后 `ast.parse` 安全门，失败即整文件回退。参数注解一律不加（避免猜测 Qt 类型，"拿不准就不加"）。

抽样：
```python
-def _inject_menu_shortcuts(menu):
+def _inject_menu_shortcuts(menu) -> None:
-def set_image(self, cv_image, is_new_source=False):
+def set_image(self, cv_image, is_new_source=False) -> None:
```
分布：canvas 135、layer_panel 34、color_wheel 28、plugin_panel 24、crop_panel 23、mask_panel 23、raw_panel 14、qualifier_panel 11、mixer_panel 10、film_grain/gl_viewport 各 9、presets/blur_sharpen 各 18、其余 ≤7。

### 4. 命名收敛：0 项

所有零引用/命名问题候选均为公共 API 表面（跨模块可见的 set_*/get_*/property/信号 adjacent），按"绝不改公共 API"红线跳过。

### 5. 小清理（8 处，AUD-25308 佐证）

- `color_wheel.py`：`self.changed.emit(0, 0, 0)` → `self.changed.emit(0.0, 0.0, 0.0)`（信号签名为 `pyqtSignal(float,float,float)`，PyQt6 传递值不变）
- `color_wheel.py`：`set_params` 浮点默认值 `= 0` → `= 0.0`（vibrance/mid_det/hue）
- `mask_panel.py`：`set_params` 浮点默认值 `= 0` → `= 0.0`（blur/clean_black/clean_white）
- 冗余只写属性 `_pending_grading_params`/`is_panning` 清理计入死代码类

---

## 二、红线自查

- **信号定义与连接**：diff 中含 `pyqtSignal` 的行仅为同一 import 行剔除未用名；`.connect(` 零改动。`stroke_finished` 未删。
- **裸 except**：`grep -rn "except:" src/features/color/ui/` = **0 命中**（AUD-25204 提到的 canvas.py:5768 裸 except 在本分支此前已被修复，本次未涉及）。
- **UI 线程/GL**：未触碰任何渲染/GL 代码路径（canvas.py 的 GLContextManager import 删除仅是去掉未用名）。
- **最小 diff**：无函数重排、无行为语句改动；`__init__.py` 再导出、惰性导入、`__future__` 全部保留。

## 三、验证输出

1. **py_compile**：27 个改动文件全部通过（`PY_COMPILE ALL OK`）。
2. **定向测试**（4 个 tests/color/ 既有测试文件，未跑大目录、未跑 tests/edit）：
   ```
   tests/color/test_timeline_clip_strip_sync.py  12 passed   （覆盖死代码删除文件 timeline_clip_strip.py）
   tests/color/test_tracker_panel_states.py      35 passed   （覆盖 tracker_panel/tracker_graph/tracker_toolbar）
   tests/color/test_scope_input_source.py        13 passed   （覆盖 scope_panel）
   tests/color/test_power_windows.py             12 passed   （覆盖 canvas.MaskShapeItem/mask_panel）
   ============================== 72 passed in 4.46s ==============================
   ```
   无新增失败（72/72 通过）。
3. **裸 except 扫描**：`grep -rn "except:" src/features/color/ui/` → 0 命中（exit 1）。
4. **注解后完整性**：每个文件插入后 `ast.parse` 复析通过；import 删除后全模块 rescan 零残留未用 import。

## 四、改动文件清单（27 个）

```
src/features/color/ui/adjustment_panel.py      src/features/color/ui/mixer_panel.py
src/features/color/ui/blur_sharpen_panel.py    src/features/color/ui/plugin_panel.py
src/features/color/ui/canvas.py                src/features/color/ui/presets_panel.py
src/features/color/ui/color_status_bar.py      src/features/color/ui/qualifier_panel.py
src/features/color/ui/color_wheel.py           src/features/color/ui/raw_panel.py
src/features/color/ui/crop_panel.py            src/features/color/ui/scope_panel.py
src/features/color/ui/deblur_panel.py          src/features/color/ui/soft_clip_panel.py
src/features/color/ui/denoise_panel.py         src/features/color/ui/timeline_clip_strip.py
src/features/color/ui/dependency_report_dialog.py  src/features/color/ui/tools_panel.py
src/features/color/ui/film_grain_panel.py      src/features/color/ui/tracker_panel.py
src/features/color/ui/gl_viewport.py           src/features/color/ui/viewer_guides.py
src/features/color/ui/hsl_curves_panel.py      src/features/color/ui/warper_panel.py
src/features/color/ui/layer_panel.py           src/features/color/ui/lut_panel.py
src/features/color/ui/mask_panel.py
```

合计：+453 / -573 行。工作树保留改动，未 git add/commit。
