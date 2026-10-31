# 批次报告：batch-036 [P1] src/app/menu//

- 编号区间：AUD-3600–AUD-3699
- 审查文件：2 个（267 行）
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失

## src/app/menu/__init__.py (行数: 2)

### 发现

（无）

### 通过项
- 通过（无逻辑）：仅包级 docstring，无 re-export、无代码；调用方直接 `from src.app.menu.menu_builder import MenuBuilder`（见 src/app/controllers/menu_toolbar_controller.py:1680）。

## src/app/menu/menu_builder.py (行数: 265)

### 发现

- [AUD-3600] [Major] 顶层菜单未注册时整组 action 仅以 DEBUG 级静默跳过，实际已导致 5 个 View/Timeline 菜单项从未出现
  - 位置: src/app/menu/menu_builder.py:96-103
  - 证据:
    ```python
    menu = self._resolve_menu(menu_path)
    if menu is None:
        logger.debug(
            "MenuBuilder: no menu registered for path %r, skipping %d actions",
            menu_path,
            len(descs),
        )
        continue
    ```
  - 影响: 跨模块验证：src/features/edit/module.py:287-331 注册了 5 个 `menu_path="View/Timeline"` 的 action（zoom_in / zoom_out / fit / playback HUD / export_perf_report），而调用方 src/app/controllers/menu_toolbar_controller.py:1692-1701 只注册了 File/Edit/Color/Composite/Deliver 五个顶层菜单（应用也不存在 "View" 顶层菜单，见同文件 44-435 行的 addMenu 清单）。这 5 项被静默丢弃，用户在 UI 中永远看不到这些入口；生产日志默认级别下完全不可见，配置错误无法被发现。机制层面置信度高；若属调用方有意裁剪，也仍属可见性缺陷。
  - 建议: 未注册路径至少提升为 warning（或提供 strict 模式返回跳过清单供调用方告警/断言），并在调用方补注册 "View" 或修正 edit 模块的 menu_path。
  - 置信度: 高

- [AUD-3601] [Major] 动态创建的中间子菜单标题未经过 i18n 翻译，中文界面显示英文 "Timeline"
  - 位置: src/app/menu/menu_builder.py:163
  - 证据:
    ```python
    # Create intermediate sub-menu.
    sub_menu = current.addMenu(sub)
    self._menus[full_path] = sub_menu
    current = sub_menu
    ```
  - 影响: `sub` 是 menu_path 的原始英文段（如 "Edit/Timeline" 的 "Timeline"），未经 `I18N.tr` 翻译。手写菜单全部走 i18n（如 menu_toolbar_controller.py:235 `I18N.tr("menu.layer", "&Layer")`），且 zh_CN.json 已有 `"timeline": "时间线"` 的现成词条；edit 模块 13 个 `menu_path="Edit/Timeline"` 的 action（src/features/edit/module.py，grep 统计 13 处）会在 Edit 菜单下产生未本地化的 "Timeline" 子菜单，违反项目 i18n 规范。
  - 建议: 约定 path 段对应的翻译键（如 `menu.<segment>`）并 `current.addMenu(I18N.tr(f"menu.{sub}", sub))`；或在 descriptor 中显式提供子菜单标题键。
  - 置信度: 高

- [AUD-3602] [Suggestion] clear() 清理不彻底：中间子菜单、`_menus` 缓存与 ShortcutManager 内的 QAction 引用均未处理
  - 位置: src/app/menu/menu_builder.py:115-126
  - 证据:
    ```python
    def clear(self) -> None:
        """Remove all actions created by this builder."""
        for action in self._actions.values():
            parent = action.parent()
            if isinstance(parent, QMenu):
                parent.removeAction(action)
            elif action.associatedWidgets():
                for w in action.associatedWidgets():
                    if isinstance(w, QMenu):
                        w.removeAction(action)
                        break
        self._actions.clear()
    ```
  - 影响: (a) `_resolve_menu` 动态创建并缓存于 `self._menus` 的子菜单（及其中 QMenu 分隔结构）不会被移除；(b) `_apply_shortcut` 已把 action 注册进 ShortcutManager（或 `update_action_reference` 重指向），clear 后 ShortcutManager 仍持有已摘除菜单的陈旧 QAction 引用，快捷键对话框会继续列出失效条目；(c) 循环 `break` 只从第一个 QMenu 摘除，若 action 被额外加入其它控件（如控制器 `_scope_timeline_delete_shortcuts` 把 action 加进 timeline_view）会残留。目前全库无 clear() 调用方（已 grep 验证），故降为 Suggestion。
  - 建议: clear 时同步从 ShortcutManager 注销/清引用，删除自建子菜单并清空 `_menus` 中自建项；文档注明当前限制。
  - 置信度: 高

- [AUD-3603] [Suggestion] I18N 导入未使用，且类 docstring 声称 "translated label" 与实现不符
  - 位置: src/app/menu/menu_builder.py:30, 41
  - 证据:
    ```python
    from src.shared.i18n.i18n import I18N
    ...
    2. Creates a ``QAction`` with the translated label.
    ```
  - 影响: 文件内无任何 `I18N` 引用（标签翻译实际由 feature 模块注册时完成，如 src/features/color/module.py:142）；docstring 误导后续维护者以为 builder 负责翻译，也掩盖了 AUD-3601 中子菜单标题无翻译的缺口；未使用导入触发 lint 告警。
  - 建议: 删除该导入或真正用于子菜单标题翻译（配合 AUD-3601），并修正 docstring。
  - 置信度: 高

- [AUD-3604] [Suggestion] 构造参数 menu_bar 被存储但从未使用
  - 位置: src/app/menu/menu_builder.py:52, 57
  - 证据:
    ```python
    menu_bar: QMenuBar,
    ...
    self._menu_bar = menu_bar
    ```
  - 影响: `_menu_bar` 在类内无任何读取（全文件仅此一处赋值）；强制调用方传入却无效，属死属性/误导性 API 契约。
  - 建议: 删除该属性或在 `_resolve_menu` 中用于兜底创建顶层菜单（需产品确认）。
  - 置信度: 高

- [AUD-3605] [Suggestion] 构造参数 shortcut_manager 缺少类型注解
  - 位置: src/app/menu/menu_builder.py:55
  - 证据:
    ```python
    shortcut_manager=None,
    ```
  - 影响: 其余参数均有注解，此参数仅默认值 None；IDE/类型检查无法推导 `sm.get_action` / `sm.register` / `sm.update_action_reference` 的可用性，也掩盖了隐式协议依赖（src/shared/ui/shortcut_manager.py:101 ShortcutManager）。
  - 建议: 标注为 `Optional['ShortcutManager']`（TYPE_CHECKING 导入避免循环依赖）。
  - 置信度: 高

- [AUD-3606] [Suggestion] `_resolve_menu` 中 `if not parts` 为不可达死代码
  - 位置: src/app/menu/menu_builder.py:145-147
  - 证据:
    ```python
    parts = menu_path.split("/")
    if not parts:
        return None
    ```
  - 影响: `str.split("/")` 至少返回一个元素，该分支永不执行（空串 path 会走 `top_name=""` 查不到而返回 None），误导阅读者以为存在空路径专门处理。
  - 建议: 删除该分支，改为对空 `menu_path` 的显式校验（如 `if not menu_path: return None`）。
  - 置信度: 高

- [AUD-3607] [Suggestion] `_derive_default_shortcut` 硬编码单条默认快捷键映射，与模块侧注册存在漂移风险
  - 位置: src/app/menu/menu_builder.py:256-259
  - 证据:
    ```python
    _DEFAULTS = {
        "deliver.export": "Ctrl+E",
    }
    return _DEFAULTS.get(desc.shortcut_id, "")
    ```
  - 影响: 各 feature 模块已通过 `register_shortcuts` 向 ShortcutManager 注册默认值（menu_toolbar_controller.py:1662-1672），builder 侧再维护一份单条映射属重复信息源；新增模块默认键时此处易漏，fallback 返回 "" 会让后注册的 action 无默认快捷键且难以察觉。
  - 建议: 将默认键统一收敛到 descriptor（如增加 `default_shortcut` 字段）或完全依赖 ShortcutManager 侧注册，删除本映射。
  - 置信度: 中

### 通过项
- 线程安全：纯 UI 线程对象，无共享可变状态跨线程访问；唯一生产调用点（menu_toolbar_controller.py:1674-1716）在 UI 线程 setup 阶段执行。
- GPU 资源生命周期：不涉及 OpenGL/ModernGL 资源。
- GLSL·HDR 正确性：不涉及着色器与像素数据。
- 信号防递归：仅单向 `triggered.connect(callback)`（menu_builder.py:211-212），无程序化 setChecked/setChecked 回写，不存在递归触发路径；checkable 初值同步由调用方 `_sync_checkable_module_actions` 负责。
- 状态提交一致性：不接触 undo/redo/revision/持久化。
- 异常与取消路径：无宽泛 except；callback 缺失时返回 None 跳过（menu_builder.py:187-197）属预期占位逻辑且注释明确。
- 输入验证（主要路径）：`_resolve_menu` 对未注册顶层路径安全返回 None；`before_action in menu.actions()` 成员校验避免跨菜单 insert（menu_builder.py:215）；ActionRegistry 侧已按 action_id 去重。
- 性能模式：一次性构建，`menu.actions()` 的 O(n) 查找在菜单规模下可忽略；无热路径。
- 其余类型注解：公开方法返回值与参数注解完整（`build`/`clear`/`get_action`/`_resolve_menu`/`_build_action` 等）。
