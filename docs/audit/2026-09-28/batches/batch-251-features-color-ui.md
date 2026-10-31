# batch-251 审查报告：src/features/color/ui/

- 批次: batch-251 [P2]，编号区间 AUD-25100–AUD-25199
- 文件数: 4（共 833 行），全部通读
- 交叉验证: `src/shared/ui/widgets/param_slider_row.py`（reset/setValue/blockSignals/collect_rows 实现）、`src/app/main_window.py`、`src/features/color/ui/tools_panel.py`（生产接线）、`src/app/controllers/layer_controller.py`（AutoColorDialog 调用点）
- 汇总: Critical 0 / Major 2 / Suggestion 10

---

## src/features/color/ui/__init__.py (行数: 31)

### 发现
- [AUD-25100] [Suggestion] 模块 docstring 未列出导出的 ColorMappingPanel
  - 位置: src/features/color/ui/__init__.py:3-11
  - 证据:
    ```python
    This module contains all UI panels and widgets related to color grading:
    - ColorWheel: Primary color grading wheel (Lift/Gamma/Gain)
    - HSLCurvesPanel: HSL curve adjustments
    - LUTPanel: LUT file loading and management
    ```
    而导出列表含 `"ColorMappingPanel"`（`from .color_mapping_panel import ColorMappingPanel`）。
  - 影响: 文档与 `__all__` 不一致，维护者易遗漏 ColorMappingPanel 属于本包职责。
  - 建议: 在 docstring 面板清单中补充 ColorMappingPanel 一行。
  - 置信度: 高

### 通过项
- 通过（纯 re-export，无逻辑）：导出 ColorWheel、ColorMappingPanel、HSLCurvesPanel、LutPanel、MaskPanel、QualifierPanel、SoftClipPanel、RawPanel 八个面板类
- 线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交 / 异常取消 / 性能：无涉及，无问题

---

## src/features/color/ui/adjustment_panel.py (行数: 104)

背景: 第 1 行标注 `# SUPERSEDED(2026-08-19 审计): 已被新实现取代且无生产引用，仅存档。`；全库 grep 确认无生产 import（仅 `src/shared/ui/style_sheet.py` 的样式表字符串提及类名）。据此全部发现降级为 Suggestion。

### 发现
- [AUD-25101] [Suggestion] reset_single_param 中 row.reset() 未屏蔽信号，values_changed 双重发射
  - 位置: src/features/color/ui/adjustment_panel.py:75-77
  - 证据:
    ```python
    def reset_single_param(self, row):
        row.reset()
        self.on_value_change()
    ```
    （`ParamSliderRow.reset()` → `setValue(default)`，且 `src/shared/ui/widgets/param_slider_row.py:147-149` 的 setValue 不屏蔽信号，`valueChanged` 会触发第 66 行的 lambda → `on_value_change()`。）
  - 影响: 单参数重置一次，`values_changed` 被发射两次（内容相同）；下游若有增量/revision 逻辑会重复记账。
  - 建议: 重置前 `row.blockSignals(True)` … `row.reset()` … `row.blockSignals(False)` 后只保留一次显式 `on_value_change()`。
  - 置信度: 高
- [AUD-25102] [Suggestion] I18N 导入未使用，五个参数标签硬编码英文
  - 位置: src/features/color/ui/adjustment_panel.py:4, 28-34
  - 证据:
    ```python
    from src.shared.i18n.i18n import I18N
    ...
    params = [
        ("Brightness", "Brightness"),
        ("Contrast", "Contrast"),
        ("Saturation", "Saturation"),
        ("Blur", "Blur"),
        ("Sharpen", "Sharpen")
    ]
    ```
  - 影响: 与全项目 I18N.tr(...) 规范不一致；卸载 I18N 依赖时此处仍残留死导入。
  - 建议: 标签改走 `I18N.tr`，或删除未使用导入。
  - 置信度: 高
- [AUD-25103] [Suggestion] 全部方法缺少类型注解
  - 位置: src/features/color/ui/adjustment_panel.py:13-103（`__init__`/`init_ui`/`create_param_row`/`on_value_change`/`reset_values`/`resizeEvent`）
  - 证据:
    ```python
    def __init__(self, parent=None):
        super().__init__(parent)
        self.init_ui()

    def init_ui(self):
    ```
  - 影响: 违反项目强类型约束（.clinerules），IDE/静态检查无法校验。
  - 建议: 补齐参数与返回类型注解（如 `def init_ui(self) -> None:`）。
  - 置信度: 高

### 通过项
- 信号防递归: `reset_values`（87-98 行）正确对每行 blockSignals 包裹 reset，且 `ParamSliderRow.blockSignals` 会连带屏蔽内部子控件信号，批量重置无双发射
- 线程安全: 纯 UI 线程控件，无 OpenGL/渲染线程交互
- GPU 资源生命周期 / GLSL·HDR: 不涉及
- 异常与取消路径: 无 except，无掩盖领域错误
- 性能模式: resizeEvent 的 collect_rows 为 O(N) 一次性遍历，本面板行数固定为 5，且 `slider_auto_hide=False` 使面板级显隐调用为必要路径

---

## src/features/color/ui/auto_color_dialog.py (行数: 129)

### 发现
- [AUD-25104] [Suggestion] suggestion 参数无类型注解且 items() 结果直接按键访问，无键校验
  - 位置: src/features/color/ui/auto_color_dialog.py:40-43, 52-57, 58
  - 证据:
    ```python
    def __init__(self, parent: Optional[QWidget],
                 suggestion, before_metrics: Dict[str, float],
                 after_metrics: Dict[str, float],
                 title: str = "") -> None:
        ...
        values = suggestion.items()
        summary = (
            f"Temp {_fmt(values['temp'])}   Tint {_fmt(values['tint'])}   "
    ```
  - 影响: `suggestion` 为鸭子类型（依赖 `.items()`/`.absolute`），五个键任一缺失即 KeyError、缺 `.absolute` 即 AttributeError，对话框构造直接崩溃；自动校色引擎与对话框的隐式契约无静态保护。
  - 建议: 为 suggestion 定义/引用具体类型（如 AutoColorSuggestion dataclass）并注解；或构造前用 `.get(key, 0.0)` + 显式回退，至少在文档字符串写明必需键集。
  - 置信度: 中（调用方 layer_controller.py:830-832 传入引擎内部产物，当前应含全部键；属契约脆弱而非现行错误）

### 通过项
- 线程安全: 纯查询 UI，模态 `dialog.exec()`，全部 UI 线程，无 OpenGL 操作
- 信号防递归: 无程序化回写控件的路径（复选框仅响应用户点击），无需状态锁
- 状态提交一致性: 按设计不修改工程状态，应用由控制器以单一 undo 完成（模块 docstring 明确且实现相符）
- 异常与取消路径: Cancel/拒绝路径正确返回 None（127-129 行），OK/取消语义清晰
- 性能模式: 静态对话框一次性构建，无热路径
- i18n: 标签、分组、按钮均走 `I18N.tr`，仅作为默认值回退的中文文案可接受

---

## src/features/color/ui/blur_sharpen_panel.py (行数: 569)

生产引用: `src/app/main_window.py:1179,1248`、`src/features/color/ui/tools_panel.py:104`（后者 `update_from_layer` → `set_params_dict(layer.effects_params)`，`reset_all` → `effects_panel.reset_all()`），发现按在用代码评定。

### 发现
- [AUD-25105] [Major] set_params_dict 屏蔽信号恢复 mode/blur_mode 时未同步 stack/blur_stack 页面索引，状态恢复后 UI 页面与参数不符
  - 位置: src/features/color/ui/blur_sharpen_panel.py:469-470, 483-484（对照 317-331）
  - 证据:
    ```python
    mode = params.get('mode', 0)
    self.mode_combo.setCurrentIndex(mode)
    ...
    blur_mode = params.get('blur_mode', 0)
    self.blur_mode_combo.setCurrentIndex(blur_mode)
    ```
    而 `on_mode_changed`（317-320 行）才是唯一更新页面的地方：`self.params['mode'] = index; self.stack.setCurrentIndex(index)`；set_params_dict 在 465-466 行对 `mode_combo`/`blur_mode_combo` `blockSignals(True)`，解除屏蔽后（498-509 行）没有任何 `self.stack.setCurrentIndex(...)` / `self.blur_stack.setCurrentIndex(...)`。
  - 影响: 图层切换/undo/redo/preset 恢复 `mode=1`（高级）或 `blur_mode=1..3` 时，下拉框显示"高级/光学虚化…"而堆栈仍停留在"基础/质感模糊"页；用户在错误页面调参，所见参数与生效参数（`get_params_dict` 按 combo 索引读取）不一致。
  - 建议: 在 set_params_dict 解除信号屏蔽后显式调用 `self.stack.setCurrentIndex(self.mode_combo.currentIndex())` 与 `self.blur_stack.setCurrentIndex(self.blur_mode_combo.currentIndex())`（或封装一个"同步页面"辅助方法，reset 路径复用）。
  - 置信度: 高
- [AUD-25106] [Major] reset_all/reset_blur 在屏蔽信号下 setCurrentIndex(0)，堆栈页面同样不归位
  - 位置: src/features/color/ui/blur_sharpen_panel.py:527, 555
  - 证据:
    ```python
    for row in all_rows:
        row.reset()
    self.mode_combo.setCurrentIndex(0)      # reset_all，信号已被 blockSignals(True)
    ...
    self.blur_mode_combo.setCurrentIndex(0)  # reset_blur，同样被屏蔽
    ```
    `on_mode_changed`/`on_blur_mode_changed` 因此不触发，`self.stack`/`self.blur_stack` 保持原页面（如高级/动感模糊页）。
  - 影响: 用户在高级模式下点"重置"后，combo 显示"基础"但页面仍展示高级参数行；再次切模式前 UI 状态自相矛盾（与 AUD-25105 同根因、不同代码路径）。另注意 reset_all 会 emit `reset_clicked`（536 行）而 reset_blur 不 emit，两路 reset 的 undo/状态通知语义不一致，修复时可一并统一。
  - 建议: 与 AUD-25105 共用"同步页面索引"辅助方法，在两处 reset 解除屏蔽后调用；并明确 reset_blur 是否也应发 reset_clicked。
  - 置信度: 高
- [AUD-25107] [Suggestion] 基础/高级两个锐化滑条初始默认值不同步（config 默认非 0 时高级页显示 0）
  - 位置: src/features/color/ui/blur_sharpen_panel.py:122-124, 142, 156
  - 证据:
    ```python
    _cfg_sharpen = ConfigManager().get("default_sharpen_amount") or 0
    self.sharpen_slider = self._create_row(..., 0, 100, _cfg_sharpen)
    ...
    self.adv_sharpen_slider = self._create_row(..., 0, 100, 0)
    ...
    self._sync_rows(self.sharpen_slider, self.adv_sharpen_slider)
    ```
    `_sync_rows` 仅靠 `valueChanged` 触发同步，创建时双方均未发信号。
  - 影响: 当 `default_sharpen_amount` 配置为非 0 时，启用锐化后切到高级页看到"锐化 0"，而生效值（`get_params_dict` 第 379 行固定读 `sharpen_slider`）为配置默认值；显示与生效参数不一致，直到用户首次拖动任一滑条才自愈。
  - 建议: 创建 adv 滑条时默认值同样用 `_cfg_sharpen`，或在 `_sync_rows` 后立即执行一次单向同步。
  - 置信度: 高
- [AUD-25108] [Suggestion] self.params 为只写死状态，与实际读取路径脱节
  - 位置: src/features/color/ui/blur_sharpen_panel.py:24-40, 318, 329
  - 证据:
    ```python
    self.params = {
        'enabled': False,
        'mode': 0,  # 0 = Basic, 1 = Advanced
        ...
    def on_mode_changed(self, index):
        self.params['mode'] = index
    ```
    全文件无任何 `self.params` 读取（`get_params_dict` 359-441 行全部直接读控件重建字典）。
  - 影响: 误导维护者以为它是参数真源；屏蔽信号路径下（set_params_dict/reset）它还会与控件状态失同步（如 25105/25106 场景）。
  - 建议: 删除 `self.params` 及两处写入，或改为从 `get_params_dict()` 派生的只读缓存。
  - 置信度: 高
- [AUD-25109] [Suggestion] resizeEvent 每次触发都 findChildren 全树遍历，与行内建自动显隐逻辑重复
  - 位置: src/features/color/ui/blur_sharpen_panel.py:565-568
  - 证据:
    ```python
    def resizeEvent(self, event):
        super().resizeEvent(event)
        ParamSliderRow.apply_visibility_to_all(
            ParamSliderRow.collect_rows(self), self.width())
    ```
    `collect_rows` 即 `parent.findChildren(ParamSliderRow)`（param_slider_row.py:237-240）；且 `_create_row` 未传 `slider_auto_hide`，默认 True（param_slider_row.py:53），每行自身 resizeEvent（211-220 行）已做同样判断。
  - 影响: 交互式拖拽窗口大小时每个 resize 事件多一次 O(N) 子树遍历 + N 次冗余 setVisible；本面板行数固定（14 行），量级小但属重复工作模式。
  - 建议: 依赖行内建自动显隐即可，删除面板级 resizeEvent 逻辑；或缓存 collect_rows 结果。
  - 置信度: 中
- [AUD-25110] [Suggestion] set_params_dict 对 mode/blur_mode 索引无范围校验即 setCurrentIndex
  - 位置: src/features/color/ui/blur_sharpen_panel.py:469-470, 483-484
  - 证据:
    ```python
    mode = params.get('mode', 0)
    self.mode_combo.setCurrentIndex(mode)
    ```
    参数来源于 `layer.effects_params`（tools_panel.py:225），属持久化/外部恢复数据；越界值会使 currentIndex 变 -1，随后 `get_params_dict` 读取 `currentIndex()` 得 -1 传播给渲染端（blur 分支 408-419 行的四个 elif 均不匹配，等效全部清零）。数值型参数因控件 setRange 钳制而无此问题。
  - 影响: 损坏或版本不匹配的工程/预设文件可使 mode/blur_mode 传入 -1，渲染端收到非法枚举值（不崩溃但行为未定义）。
  - 建议: 恢复前 `mode = min(max(int(mode), 0), 1)`、`blur_mode = min(max(int(blur_mode), 0), 3)` 钳制并记录告警。
  - 置信度: 中
- [AUD-25111] [Suggestion] 全部方法缺少类型注解；QCheckBox 导入未使用
  - 位置: src/features/color/ui/blur_sharpen_panel.py:3, 22-48, 50-61, 274-285, 333-357, 359, 443, 511, 538, 565
  - 证据:
    ```python
    from PyQt6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QPushButton,
                                 QStackedWidget, QLabel,
                                 QCheckBox)          # QCheckBox 全文件未使用
    ...
    def __init__(self, parent=None):
    ...
    def get_params_dict(self):
    ```
  - 影响: 违反项目强类型约束；effects_changed(dict) 的字典键集也仅靠约定，建议用 TypedDict 固化契约；死导入增加阅读噪音。
  - 建议: 补齐类型注解（`get_params_dict(self) -> dict`、`set_params_dict(self, params: dict, emit: bool = False) -> None` 等），删除未使用导入。
  - 置信度: 高

### 通过项
- 线程安全: 全部 UI 线程，无 OpenGL 调用；QTimer(42-44 行) 仅作 UI 线程内节流
- GPU 资源生命周期: 不涉及 GPU 对象
- GLSL·HDR 正确性: 本文件只产出参数字典，不对 HDR 数据做 clamp；数值均以 float 传出
- 信号防递归: `_syncing_ui` 状态锁在 set_params_dict/reset_all/reset_blur/_emit_pending_params 全路径一致使用；`_sync_rows`(287-310) 采用标志位 + blockSignals 双保险，无双滑条互锁回环；`ParamSliderRow` 自身 `_updating` 锁亦验证无误
- 性能模式: 16ms 单发 QTimer 节流 + `_pending_params` 快照覆盖 + 交互结束 `_flush_pending_params` 立即冲刷（281-284, 339-357 行），拖动期间不会风暴发射 effects_changed
- 异常与取消路径: 无宽泛 except；无异步取消路径
- 状态提交一致性: interaction_started/ended 成对转发（供 undo 合并交互），set_params_dict(emit=True) 与 reset 均同步冲刷定时器后发射，无残留 pending 旧值（449-450, 514, 541 行均先 stop timer）
