# batch-078 审查报告：src/features/composite//

- 任务书：docs/audit/2026-09-28/assignments/batch-078.md
- 编号区间：AUD-7800–AUD-7899
- 审查日期：2026-09-28
- 文件数：1（397 行）

## src/features/composite/preferences.py (行数: 397)

Composite 模块偏好页（CompositePreferencePage）：摇动断开灵敏度、导出并行帧数、色彩管理（工作/显示空间、颜色引擎）、节点磁盘缓存容量与载荷格式。控件创建后按 config key 存入 `self._widgets`，由 PreferencesDialog 委托调用 load/apply/restore_defaults。已核实调用点：`load(cfg)` 收到 dict（preferences_dialog.py:1899-1903，`cfg.get('log_file_path', '')` 两参用法）；`apply(cfg)` 收到 ConfigManager（preferences_dialog.py:2053-2059，随后 `cfg.save_config()`）；`restore_defaults(defaults)` 收到 `config_manager.DEFAULT_CONFIG`（preferences_dialog.py:2118, 2249-2255）。

### 发现

- [AUD-7800] [Suggestion] Color Engine 默认值文案与实际默认 "ocio" 矛盾（tooltip 与注释声称 builtin 是默认）
  - 位置: src/features/composite/preferences.py:29-31, 174, 196-197（对照 :50 与 src/shared/config/config_manager.py:177）
  - 证据:
    ```python
    # preferences.py:29-31 注释
    # Display space controls the viewer's
    # default ODT (linear→sRGB encode on/off). Color engine selects the
    # transform backend: "builtin" (default, fast) or "ocio" (OpenColorIO,
    # preferences.py:50 实际常量
    COLOR_ENGINE_DEFAULT = "ocio"  # matches DEFAULT_CONFIG factory default
    # preferences.py:174 用户可见 tooltip
    "Built-in uses fast sRGB↔linear math (default). OCIO uses "
    # config_manager.py:177 工厂默认
    "composite.color_engine": "ocio",
    ```
  - 影响: 三处文案（模块注释、Color Engine 下拉 tooltip、Colour Management 提示文本 "Built-in is the fast default..."）均声称 builtin 为默认，而 DEFAULT_CONFIG 与 `COLOR_ENGINE_DEFAULT` 均为 "ocio"。用户被误导以为出厂引擎是 builtin；注释会误导后续维护者。无功能性后果。
  - 建议: 更新 tooltip/hint/注释为「默认 OCIO（必需依赖），builtin 为快速回退」；或若产品意图是 builtin 默认，则改 DEFAULT_CONFIG 与常量并同步验证下游（color_transform_provider.py:795 的 "ocio" 回退值）。
  - 置信度: 高

- [AUD-7801] [Suggestion] OCIO 不可用时禁用项仍可被程序化选中，UI 显示的引擎与实际生效引擎不一致
  - 位置: src/features/composite/preferences.py:180-189, 309-320
  - 证据:
    ```python
    if not is_ocio_available():
        # Disable the OCIO option when PyOpenColorIO isn't installed.
        model = ce_combo.model()
        item = model.item(1)
        if item is not None:
            item.setEnabled(False)
    ```
    ```python
    # _set_combo_by_data：按 itemData 匹配后无条件 setCurrentIndex，
    # 不检查 item.isEnabled()
    for i in range(combo.count()):
        if combo.itemData(i) == value:
            combo.setCurrentIndex(i)
            return
    ```
  - 影响: 若已保存配置为 "ocio" 但 PyOpenColorIO 未安装，`load()`/`_set_combo_by_data` 仍会把已禁用的 OCIO 项设为当前项（Qt 中 setCurrentIndex 不受 item disabled 限制）。下拉框显示 OCIO，实际运行时 color_transform_provider 回退 builtin（color_transform_provider.py:793-815，行为不破坏）。展示层误导：用户看到 A 实际跑 B；apply() 还会把 "ocio" 原样写回配置。
  - 建议: `_set_combo_by_data` 匹配后检查该项是否可选（`model.item(i)` 的 flags 含 IsEnabled），不可选则回落 default（builtin），或在选中禁用项时于 UI 上注明「OCIO 不可用，当前生效 builtin」。
  - 置信度: 高

- [AUD-7802] [Suggestion] restore_defaults 缺少与 load() 一致的类型容错，异常会被对话框静默吞掉导致恢复默认部分生效
  - 位置: src/features/composite/preferences.py:366-367, 371-372（对照 load() 的 264-267, 283-285）
  - 证据:
    ```python
    # restore_defaults —— 无 try/except
    par_spin.setValue(int(defaults.get(
        EXPORT_PARALLEL_FRAMES_KEY, EXPORT_PARALLEL_FRAMES_DEFAULT)))
    ...
    nc_spin.setValue(float(defaults.get(
        NODE_CACHE_CAPACITY_KEY, NODE_CACHE_CAPACITY_DEFAULT)))
    ```
    ```python
    # load() —— 有容错
    try:
        slider.setValue(int(val))
    except (TypeError, ValueError):
        slider.setValue(SHAKE_SENSITIVITY_DEFAULT)
    ```
    ```python
    # preferences_dialog.py:2250-2254 调用方吞异常
    try:
        page_obj.restore_defaults(defaults)
    except Exception:
        pass  # Module page may not be visible
    ```
  - 影响: `defaults` 来自内部 DEFAULT_CONFIG，当前值合法且缺键走 `.get(key, 默认)` 回退，现实风险低（已核实 `composite.node_cache_capacity_gb`、`composite.export_parallel_frames` 均不在 config_manager.py 的 DEFAULT_CONFIG 中，恒走模块常量）。但若未来默认表出现 None/字符串脏值，`int()/float()` 抛 TypeError 后被对话框 `except Exception: pass` 吞掉，composite 页恢复默认从中断处截断（前面的控件已重置、后面的未动），且用户无任何提示。
  - 建议: restore_defaults 与 load() 对齐，用 try/except (TypeError, ValueError) 包裹 int/float 转换并回退默认值；顺带将两个缓存/并行键补进 DEFAULT_CONFIG，使默认值单一来源。
  - 置信度: 高（不一致性确凿；触发条件为假设）

- [AUD-7803] [Suggestion] `model().item()` 依赖 QComboBox 默认 QStandardItemModel 的实现细节，类型层面不成立
  - 位置: src/features/composite/preferences.py:184-187
  - 证据:
    ```python
    model = ce_combo.model()
    item = model.item(1)
    if item is not None:
        item.setEnabled(False)
    ```
  - 影响: PyQt6 将 `QComboBox.model()` 标注为 `QAbstractItemModel`，该类型没有 `.item()`——运行时恰好可用是因为 QComboBox 默认模型是 QStandardItemModel（已核实 ChevronComboBox 未 setModel 自定义模型）。mypy/pyright 会报 AttributeError；若日后 ChevronComboBox 或本页换成代理/自定义模型，`.item()` 直接抛 AttributeError 而非返回 None，`if item is not None` 防不住。
  - 建议: 显式 `from PyQt6.QtGui import QStandardItemModel` 并 `isinstance(model, QStandardItemModel)` 判断后再取 item；或改用 `QStandardItemModel.model().itemFromIndex(model.index(1, 0))` 等带类型检查的路径。
  - 置信度: 高

- [AUD-7804] [Suggestion] 类型注解不完整（基类接口对齐）
  - 位置: src/features/composite/preferences.py:64, 67, 358（对照 src/shared/preferences/__init__.py:36 基类 `create_widget(self, parent: "QWidget") -> "QWidget"`）
  - 证据:
    ```python
    def __init__(self):
        self._widgets: dict[str, QWidget] = {}

    def create_widget(self, parent) -> QWidget:
    ```
    ```python
    def restore_defaults(self, defaults: dict) -> None:
    ```
  - 影响: `__init__` 缺 `-> None`；`create_widget` 的 `parent` 形参无注解（基类为 QWidget，本实现忽略该参数也未用 `_` 前缀标明）；`defaults` 用裸 `dict` 未参数化（应为 `dict[str, Any]`）。违反项目「所有函数有类型注解」约束，仅静态检查/可读性影响。
  - 建议: 补 `-> None`；`parent: QWidget`（或 `parent: QWidget | None`）；`defaults: dict[str, Any]`。`load/apply` 的 `Any` 参数可进一步收紧为 Mapping / ConfigManager 协议。
  - 置信度: 高

### 通过项

- 线程安全：纯 UI 线程控件构建与读写，无 OpenGL 调用、无跨线程共享状态
- GPU 资源生命周期：页面不创建/持有任何 ModernGL 或纹理资源（导出并行帧数仅是数值透传，worker 上下文在渲染侧管理）
- GLSL/HDR 正确性：文件不含着色器代码；色彩空间/引擎键为字符串透传，无浮点字面量、无 HDR 数据 clamp 风险
- 信号防递归：唯一信号连接 `valueChanged→label.setText`（:104）无回写环路；load/restore 的程序化 setValue 只触发标签同步，属期望行为，无需状态锁
- 状态提交一致性：apply() 全部经 `set(..., auto_save=False)` 暂存，由对话框统一 `save_config()`（已核实调用点与 ConfigManager.set 签名匹配）；取消路径由对话框快照回滚，职责不在本页
- 输入验证（load 路径）：int/float 转换用窄 `except (TypeError, ValueError)` 回退默认，无宽泛 except；slider 0-100、并行帧 1-4、缓存 0.5-2000 均由控件范围钳制；combo 值经 `_set_combo_by_data` 回退默认（可选项 AUD-7801 指出禁用项边角）
- 性能模式：QFont/I18N/is_ocio_available 均为函数内延迟导入或模块级缓存标志（color_transform_provider.py:588-590 直接返回 `_OCIO_IMPORTABLE`），页面仅对话框打开时构建一次，无热路径
- 资源引用：`_widgets` 与页面对象生命周期同属对话框会话，旧控件随 widget 树销毁，无泄漏路径
