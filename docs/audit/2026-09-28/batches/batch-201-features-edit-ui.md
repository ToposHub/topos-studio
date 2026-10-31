# batch-201 审查报告 — src/features/edit/ui//

- 批次: batch-201 [P1]，编号区间 AUD-20100–AUD-20199
- 文件: `src/features/edit/ui/edit_style.py` (806 行)、`src/features/edit/ui/interchange_dialog.py` (172 行)
- 审查维度: 线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失
- 汇总: Critical 0 · Major 1 · Suggestion 4

---

## src/features/edit/ui/edit_style.py (行数: 806)

Edit 页设计 Token（颜色/尺寸字典）+ QSS 样式表生成函数集合。纯常量与无状态字符串生成，不涉及线程、GPU、GLSL、信号、undo/redo 与外部数据。

### 发现

- [AUD-20100] [Suggestion] EDIT_COLORS 混入非颜色值（int 宽度/alpha），`get_color() -> str` 类型契约对部分键不成立
  - 位置: src/features/edit/ui/edit_style.py:53
  - 证据:
    ```python
    "clip_separator": "#0A0A0C",    # 片段间隔暗缝色
    "clip_gap_width": 2,            # 片段间隔宽度 (px)
    ...
    "highlight_edge_alpha": 40,
    "inner_shadow_alpha": 100,
    ```
    以及（edit_style.py:777–790）：
    ```python
    def get_color(key: str) -> str:
        """
        ...
        Returns:
            十六进制颜色字符串
        """
        return EDIT_COLORS[key]
    ```
  - 影响: `get_color("clip_gap_width")` 返回 int `2` 而非十六进制字符串，违反 docstring 与返回注解；若调用方将其用于 QSS 拼接或 `QColor(...)` 构造会抛 TypeError。全库 grep 确认这三个键当前无任何消费者（属潜伏问题）。另外 `EDIT_COLORS` / `EDIT_SIZES` 两个模块级字典缺少类型注解（实际类型为 `dict[str, str | int]` 与 `dict[str, int]`）。
  - 建议: 将 `clip_gap_width` 移入 EDIT_SIZES；alpha 值改为 `"40"` 字符串或独立 token；为两个字典补充精确类型注解。
  - 置信度: 高

- [AUD-20101] [Suggestion] 七个样式生成函数全库零调用者（死代码），且 `clip_item_style` 使用普通 QWidget 不会命中的 `:selected` 伪状态
  - 位置: src/features/edit/ui/edit_style.py:508
  - 证据:
    ```python
    QWidget:selected {{
        background-color: {_color("accent")};
        border: 2px solid {_color("accent_hover")};
    }}
    ```
    全库 grep（src/ 全部 .py）确认以下函数无任何调用者（仅本文件定义）: `timeline_style`(374)、`track_header_style`(437)、`clip_item_style`(482)、`danger_button_style`(597)、`toolbar_container_style`(633)、`scroll_area_style`(660)、`checkbox_style`(725)。实际被使用的入口为 `tool_button_style/panel_style/tree_style/input_style/button_style/accent_button_style/get_color/get_size` 及 `EDIT_COLORS/EDIT_SIZES`。
  - 影响: 维护面扩大——修改 Token 时这些死代码既不受益也无法验证；若日后有人启用 `clip_item_style`，其选中态样式不会生效：QSS 的 `:selected` 伪状态只对 item view 条目（或自定义控件 style option 携带 State_Selected）生效，普通 QWidget 选择器永远匹配不到，选中反馈将缺失。
  - 建议: 删除未使用函数；如需保留片段选中态视觉，改在时间线自定义绘制（timeline_items 已用 `*_selected` Token 自绘）中实现。
  - 置信度: 高（死代码部分，grep 验证）；中（`:selected` 不生效部分，基于 Qt QSS 文档化行为推断）

- [AUD-20102] [Suggestion] tree_style 选中色硬编码 `rgba(226, 146, 39, …)` 与 accent Token `#E29227` 数值耦合
  - 位置: src/features/edit/ui/edit_style.py:263
  - 证据:
    ```python
    QTreeWidget::item:selected, QTreeView::item:selected {{
        background-color: rgba(226, 146, 39, 0.25);
        color: {_color("text")};
    }}
    ```
    而 accent 定义（edit_style.py:32）：
    ```python
    "accent": "#E29227",        # 主题橘黄色（激活/选中）
    ```
  - 影响: `rgba(226,146,39,…)` 正是 `#E29227` 的 rgb 分量，但以字面量重复。一旦调整 accent Token（品牌色迭代），树视图选中色不会跟随，产生主题色漂移；同类硬编码还有 `#b87024`(587)、`#b84d4d`(622)、`rgba(255,255,255,0.05)`(260)。
  - 建议: 从 accent 十六进制值程序化派生 rgba 字符串（hex→r,g,b 工具函数），消除重复字面量。
  - 置信度: 高

### 通过项
- 线程安全：纯静态字典 + 无状态函数，无共享可变状态，无跨线程访问
- GPU 资源生命周期：不涉及任何 ModernGL/OpenGL 资源
- GLSL·HDR 正确性：不涉及着色器与 HDR 数据
- 信号防递归：无信号定义或连接
- 状态提交一致性：不涉及 undo/redo/revision/持久化
- 异常与取消路径：`_color/_size/get_color/get_size` 对未知键 KeyError fail-fast，且 docstring 已声明该契约
- 输入验证：无外部数据输入面
- 性能模式：QSS 生成是一次性 f-string 拼接，无热点路径

---

## src/features/edit/ui/interchange_dialog.py (行数: 172)

互操作（FCPXML/OTIO）导入/导出报告对话框，纯展示 + confirm_mode 确认语义，由 `src/app/controllers/file_controller.py`（6965/6995/7016/7072/7300 行）调用。

### 发现

- [AUD-20103] [Major] 诊断消息未做 HTML 转义即拼入富文本 QLabel，外部数据中的特殊字符导致显示残缺或被吞
  - 位置: src/features/edit/ui/interchange_dialog.py:97
  - 证据:
    ```python
    for diag in report.diagnostics:
        severity = diag.get("severity", "info")
        color = "#ff6b6b" if severity == "error" else "#ffb454"
        msg = diag.get("message", "")
        code = diag.get("code", "")
        counts_layout.addWidget(QLabel(f'<span style="color:{color}">[{severity}] {msg} ({code})</span>'))
    ```
    上游诊断 message 直接携带外部文件异常文本（src/features/edit/adapters/fcpxml_adapter.py:129-130）：
    ```python
    diagnostics=[{"severity": "error", "code": "TAEP_INTERCHANGE_LOSSY",
                  "message": f"FCPXML parse failed: {exc}"}],
    ```
    以及 otio_adapter.py:83 `"message": f"OTIO read failed: {exc}"`（捕获任意 Exception）。
  - 影响: QLabel 默认 `AutoText` 检测到 `<span>` 后整串按富文本解析。`FileNotFoundError`/`OSError` 的消息含文件路径——路径中合法的 `&`、`<` 字符（如 `A & B/x.fcpxml`）会被当作 HTML 标记/实体，导致诊断行渲染残缺、内容截断甚至整段消失。此时用户恰好是在导入失败后最需要看到失败原因的时刻，违反"外部数据须验证"铁律。不致崩溃，故定 Major 而非 Critical。
  - 建议: 对 `severity/msg/code` 先 `html.escape()` 再拼富文本；或改用 `Qt.TextFormat.PlainText` + 前景色着色方案。
  - 置信度: 高（机制与外部数据路径均已核实）

- [AUD-20104] [Suggestion] `_CATEGORY_LABELS` 在模块导入期固化 I18N.tr 翻译，运行时切换语言后类别标签与对话框其余文本语言不一致
  - 位置: src/features/edit/ui/interchange_dialog.py:36
  - 证据:
    ```python
    _CATEGORY_LABELS: dict[InterchangeEntryCategory, tuple[str, str]] = {
        InterchangeEntryCategory.PRESERVED: (
            I18N.tr("edit.interchange.cat_preserved", "保留"), "#5ad06e",
        ),
    ```
    而应用支持运行时切换语言：menu_toolbar_controller.py:679 调用 `I18N.load_locale(locale_code)` 后仅弹出可跳过的重启提示（"Later" 继续当前会话）。
  - 影响: 本模块为惰性导入（file_controller 函数内 import）。若用户先打开过一次互操作对话框（触发导入、固化当前语言的标签），再切换语言并选择稍后重启，则同一会话内再次打开对话框时：表头/按钮/标题（每次构造时 `I18N.tr`）是新语言，而四类统计标签仍是旧语言，出现中英混排。重启后自愈。
  - 建议: 把类别标签查询移入对话框构造时（模块级仅保留键→(i18n key, color) 映射）。
  - 置信度: 中

### 通过项
- 线程安全：仅在 UI 线程槽函数中构造并 exec，无跨线程访问
- GPU 资源生命周期：不涉及
- GLSL·HDR 正确性：不涉及
- 信号防递归：仅 accepted→accept / rejected→reject 单向连接，无递归风险
- 状态提交一致性：纯展示层；confirm_mode 的 preview→commit 语义由调用方正确实现（file_controller.py:7016-7025，拒绝即中止、接受才 `_apply_interchange_import`），对话框本身无状态可污染
- 异常与取消路径：diagnostics 字典键用 `.get` 带默认值（`InterchangeReport.diagnostics: tuple[dict[str, Any], ...]` 已核实为 dict，用法安全）；confirm_mode 下 Esc/Cancel 正确走 reject；非确认模式 OK-only 合理
- 输入验证：`_CATEGORY_LABELS.get(entry.category, (entry.category.value, "#aaaaaa"))` 有兜底（Category 为 str Enum，`.value` 安全）；表格条目经 `QTableWidgetItem` 纯文本渲染，无富文本风险——除 AUD-20103 的诊断行外通过
- 性能模式：一次性模态构建，表格填充 O(n)，`ResizeToContents` 仅列级、可接受
- 类型注解缺失：公开接口（`__init__` 参数、返回值）均有注解

---

## 审查方法说明

- 两文件均全文通读（Read 全行号）。
- 交叉验证（只读 grep/Read）: `interchange_types.py`（diagnostics/Enum 结构）、`fcpxml_adapter.py`/`otio_adapter.py`（诊断 message 来源）、`file_controller.py` 6950-7080/7248-7312（对话框调用与 preview→commit 流程）、`i18n.py` + `menu_toolbar_controller.py`（运行时语言切换）、edit_style 全部导出符号的全库消费情况。
- 本批次未使用 AUD-20105 之后编号。
