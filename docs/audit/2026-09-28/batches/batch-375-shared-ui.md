# batch-375 审查报告：src/shared/ui（AUD-37500–AUD-37599）

审查人维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。

## src/shared/ui/__init__.py (行数: 2)

### 发现

- [AUD-37501] [Suggestion] 缺少 __all__，与兄弟包导出风格不一致
  - 位置: src/shared/ui/__init__.py:1-2
  - 证据:
    ```python
    from src.shared.ui.shortcut_manager import ShortcutManager, ShortcutAction
    from src.shared.ui.style_sheet import DARK_THEME, TAB_STYLE, LAYER_LIST_STYLE, apply_combo_dropdown_style
    ```
  - 影响: 兄弟包（src/shared/logging、src/shared/paths、src/shared/utils 的 `__init__.py`）均显式定义 `__all__`；本文件未定义，`from src.shared.ui import *` 的导出集合由导入名隐式决定，包的公共表面无显式清单，后续增删导出易失控。
  - 建议: 补 `__all__ = ["ShortcutManager", "ShortcutAction", "DARK_THEME", "TAB_STYLE", "LAYER_LIST_STYLE", "apply_combo_dropdown_style"]`。
  - 置信度: 高

### 通过项
- 通过（无逻辑）：纯 re-export，无分支/状态/副作用。
- 导出名真实性已核实：ShortcutAction（shortcut_manager.py:53）、ShortcutManager（shortcut_manager.py:101）、apply_combo_dropdown_style（style_sheet.py:926）、DARK_THEME（style_sheet.py:1009，1661-1662 行做纯字符串替换）、TAB_STYLE（1667）、LAYER_LIST_STYLE（1703）均存在。
- import 期副作用安全：style_sheet 的 `DARK_THEME.replace(...)` 为纯字符串操作，不依赖 QApplication 实例；shortcut_manager 仅类定义导入，无控件实例化。
- 线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常路径 / 性能：不涉及（该文件仅两行 import）。
