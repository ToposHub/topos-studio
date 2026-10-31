# batch-314 — src/shared/actions/

编号区间：AUD-31400–AUD-31499。审查日期：2026-09-28。

## src/shared/actions/__init__.py (行数: 92)

### 发现
- [AUD-31401] [Suggestion] 未使用的导入：dataclasses.field、typing.Callable/Any/List
  - 位置: src/shared/actions/__init__.py:10-11
  - 证据:
    ```python
    from dataclasses import dataclass, field
    from typing import Callable, Optional, Any, List
    ```
    （文件正文仅使用 `dataclass` 与 `Optional[str]`；field、Callable、Any、List 均无引用。）
  - 影响: 死导入增加阅读噪声，误导读者以为描述符含可调用字段；未过 lint 的仓库会积累此类残留。
  - 建议: 精简为 `from dataclasses import dataclass` 与 `from typing import Optional`。
  - 置信度: 高

- [AUD-31402] [Suggestion] ActionRegistry.__init__ 缺少返回类型注解
  - 位置: src/shared/actions/__init__.py:49
  - 证据:
    ```python
        def __init__(self):
            self._actions: dict[str, MenuActionDescriptor] = {}
    ```
  - 影响: 同文件其余方法（register/get_actions/get_action/get_menu_paths）均有注解，唯 __init__ 缺 `-> None`，不符合项目强类型约束。
  - 建议: 补 `-> None`。
  - 置信度: 高

### 通过项
- 线程安全：注册只发生在应用启动阶段（app/main_window.py:348-349 构建 ActionRegistry、features 各 module 的 register_menu_actions，如 features/color/module.py:135-148），此后菜单构建（app/menu/menu_builder.py）只读；单写多读模型下无锁可接受。
- GPU 资源生命周期 / GLSL·HDR / 信号防递归：不涉及。
- 状态提交一致性：重复 action_id 注册被显式忽略且有注释说明（register 54-56 行），描述符为 frozen dataclass，注册后不可被篡改；get_actions 排序键 (menu_path, order, action_id) 确定性强，菜单顺序稳定。
- 异常与取消路径：无吞异常的宽泛 except；不存在静默失败路径（重复注册是文档化行为）。
- 输入验证：action_id 冲突检测存在；字段类型由 dataclass 声明约束（调用方以关键字传参）。
- 性能模式：get_actions/get_menu_paths 每次构建新列表，注册表规模为几十个描述符，无性能问题。
- 设计说明核对：enabled_when 以字符串条件（如 feature id 表达式）延迟到 menu_builder 求值，checkable 的回调首参语义在 docstring 中有明确文档，与 menu_builder 的消费方式一致。
