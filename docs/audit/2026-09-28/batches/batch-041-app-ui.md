# batch-041 [P1] src/app/ui//

- 编号区间：AUD-4100–AUD-4199
- 文件：`src/app/ui/shortcut_dialog.py` (865)、`src/app/ui/welcome_dialog.py` (453)
- 汇总：Critical 0 / Major 3 / Suggestion 12

依赖核实：`src/shared/ui/shortcut_manager.py`（`set_shortcut`/`remove_shortcut`/`reset_all`/`import_preset` 是否即时持久化）、`src/shared/config/config_manager.py`（非单例、`set(auto_save=True)` 同步落盘）、`src/app/main_window.py:432`（`ShortcutManager.initialize`）、`src/app/controllers/menu_toolbar_controller.py:732,891`、`src/app/ui/preferences_dialog.py:1474`、`src/shared/app_restart.py`、`src/shared/i18n/i18n.py`（`tr` 占位符行为）均已 Read 核实。

## src/app/ui/shortcut_dialog.py (行数: 865)

### 发现

- [AUD-4100] [Major] Cancel 按钮无法回退：所有修改经 ShortcutManager 即时写入 config.json 并生效于 QAction
  - 位置: src/app/ui/shortcut_dialog.py:264-271
  - 证据:
    ```python
    self._cancel_btn.clicked.connect(self.reject)
    ...
    self._save_btn.clicked.connect(self.accept)
    ```
    而编辑提交（:73-74 `self._dialog._apply_shortcut(action_id, shortcut_str)` → :844 `self._manager.set_shortcut(action_id, key_sequence)`）、`:746 self._manager.remove_shortcut(action_id)`、`:760 self._manager.reset_all()`、`:342 manager.import_preset(data, mode=mode)` 均在对话框会话内直接改 manager。
  - 影响: ShortcutManager.set_shortcut 内部调用 `_save_custom_shortcuts()` → `save_config()`（shortcut_manager.py:282,496-500），即每次编辑、Reset、Import 立刻持久化到 config.json 并同步到 QAction。UI 呈现 Save/Cancel 二元语义，但用户点 Cancel 后所有改动仍然保留且无法撤销（无 undo 快照），违反状态提交一致性预期（menu_toolbar_controller.py:736-738 的注释表明这是已知设计，但用户契约仍失真）。
  - 建议: 进入对话框时深拷贝 `dict(manager._custom_shortcuts)` 作为草稿，编辑只写草稿，accept 时一次性提交，reject 时丢弃；或去掉 Cancel 改为单一 Close。
  - 置信度: 高

- [AUD-4101] [Major] 未设置 WA_DeleteOnClose，父窗口存活期间每次打开累积一份完整对话框 widget 树
  - 位置: src/app/ui/shortcut_dialog.py:101-104
  - 证据:
    ```python
    def __init__(self, manager: ShortcutManager, parent=None,
                 enabled_features: Optional[set] = None) -> None:
        super().__init__(parent)
        self._manager = manager
    ```
    （构造中无 `setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)`）
  - 影响: 两个调用点（menu_toolbar_controller.py:732、preferences_dialog.py:1474）均以主窗口/首选项为 parent 创建局部 `dialog` 并 `dialog.exec()`；PyQt 中有父对象的 QObject 由 C++ 父对象持有，Python 引用消失不会销毁对话框。长会话内反复打开快捷键设置会持续累积 QDialog+QTableWidget+全表 item（对比 welcome_dialog.py:62-64 同类问题已以 [R2-2] 修复）。
  - 建议: 构造时 `self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)`，或调用点改为无 parent + exec 后显式 deleteLater。
  - 置信度: 高

- [AUD-4102] [Suggestion] 直接读取 ShortcutManager 私有成员 `_custom_shortcuts` 判断自定义状态
  - 位置: src/app/ui/shortcut_dialog.py:655
  - 证据:
    ```python
    current_shortcut = self._manager.get_shortcut(sa.id)
    is_custom = (sa.id in self._manager._custom_shortcuts)
    ```
  - 影响: 跨越封装边界，ShortcutManager 内部重构（改名/改存储结构）会直接破坏对话框；且绕过了 `get_shortcut` 的单一查询口径。
  - 建议: 在 ShortcutManager 增加公开 API（如 `is_custom(action_id) -> bool`），对话框改用之。
  - 置信度: 高

- [AUD-4103] [Suggestion] `_updating_table` 状态锁只写不读，是无效的死标志
  - 位置: src/app/ui/shortcut_dialog.py:643,689
  - 证据:
    ```python
    self._updating_table = True
    self._table.setRowCount(len(actions))
    ...
    self._updating_table = False
    ```
  - 影响: 项目铁律要求"程序化更新控件须状态锁防递归信号"；此处锁从未被任何处理器检查（文件内也无 itemChanged 连接），给人已防护的假象，实际防护为零。当前恰好无递归路径，属死代码而非活性 bug。
  - 建议: 删除该标志，或连接 `itemChanged` 并真正在填充期间短路。
  - 置信度: 高

- [AUD-4104] [Suggestion] 启动时 `_on_category_changed` 被触发两次，初始表格填充两遍
  - 位置: src/app/ui/shortcut_dialog.py:630-635
  - 证据:
    ```python
    root = self._category_tree.topLevelItem(0)
    if root:
        self._category_tree.setCurrentItem(root)
        self._on_category_changed(root, None)
    ```
    （:175 已连接 `currentItemChanged.connect(self._on_category_changed)`，`setCurrentItem` 会先经信号触发一次）
  - 影响: 打开对话框即做两次全表填充（含逐行 find_conflicts 冲突扫描），白白加倍启动开销。
  - 建议: 删除显式的第二行调用，仅依赖信号；或只保留显式调用并临时断开信号。
  - 置信度: 高

- [AUD-4105] [Suggestion] 表格刷新为 O(行数×注册表) 的逐行冲突扫描，且搜索每键入一个字符就整表重建
  - 位置: src/app/ui/shortcut_dialog.py:668-669
  - 证据:
    ```python
    if current_shortcut and self._show_conflicts:
        conflicts = self._manager.find_conflicts(current_shortcut, sa.id)
    ```
    连同 :720-727 `_on_search_changed` 中每次 textChanged 都 `_populate_table(actions)`。
  - 影响: `find_conflicts`（shortcut_manager.py:445-449）线性遍历整个注册表，每行一次即 O(N²)；快捷键数量增长后在搜索打字时会产生可感知的 UI 卡顿。
  - 建议: 填充前用一次遍历构建 `sequence -> [action_id]` 反向索引再逐行查表；搜索可加 150-250ms QTimer 防抖。
  - 置信度: 中

- [AUD-4106] [Suggestion] Export/Import 使用 `ShortcutManager.instance()` 单例而非构造注入的 `self._manager`
  - 位置: src/app/ui/shortcut_dialog.py:283,310
  - 证据:
    ```python
    manager = ShortcutManager.instance()
    if manager is None:
        return
    ```
    （`_on_export_preset` 与 `_on_import_preset` 两处相同，而对话框其余全部操作走 `self._manager`）
  - 影响: 当前生产路径 main_window.py:432 以 `ShortcutManager.initialize` 创建并传入，两引用恰好同一对象，无活性 bug；但注入非单例 manager（如测试或未来多实例）时，导入会写入另一个 manager，随后 `_refresh_table()`（:374）从 `self._manager` 读到旧状态，界面与实际生效配置分叉。
  - 建议: 两处改为 `manager = self._manager`（构造已保证非 None）。
  - 置信度: 高（不一致确凿；生产触发概率低）

- [AUD-4107] [Suggestion] 构造函数内新建 ConfigManager 实例仅为读取一个布尔配置
  - 位置: src/app/ui/shortcut_dialog.py:111-113
  - 证据:
    ```python
    from src.shared.config.config_manager import ConfigManager
    cfg = ConfigManager()
    self._show_conflicts = self._config_bool(cfg.get("show_shortcut_conflicts"), default=True)
    ```
  - 影响: ConfigManager 不是单例（无 `_instance` 机制），新实例会完整解析 config.json 一次；而 ShortcutManager 自身已持有 `_config_manager`。多余的一次磁盘 IO + 对象分配，且与 manager 的配置视图可能短暂不一致。
  - 建议: 通过 manager 暴露的 config 引用读取，或作为构造参数传入。
  - 置信度: 高

- [AUD-4108] [Suggestion] 导入 preset 触发逐条 `save_config()` 同步磁盘写，大 preset 卡 UI
  - 位置: src/app/ui/shortcut_dialog.py:342
  - 证据:
    ```python
    report = manager.import_preset(data, mode=mode)
    ```
  - 影响: `import_preset` 对每个 applied 条目调用 `set_shortcut`（shortcut_manager.py:416-420），后者每次都 `_save_custom_shortcuts()` → `save_config()`（含读盘合并 + 写盘，config_manager.py:616-634）。数百条快捷键的 preset 意味着数百次同步 JSON 读写全部发生在 UI 线程，期间对话框冻结。
  - 建议: 为 import 路径提供批量提交接口（一次修改 `_custom_shortcuts` + 单次落盘 + 单次 shortcuts_changed）。
  - 置信度: 高（路径确凿；卡顿程度依赖 preset 规模）

- [AUD-4109] [Suggestion] 多处参数/返回值类型注解缺失或不精确
  - 位置: src/app/ui/shortcut_dialog.py:37,41,94,101-102,856
  - 证据:
    ```python
    def __init__(self, dialog: 'ShortcutDialog', parent=None) -> None:
    def createEditor(self, parent: QWidget, option: QStyleOptionViewItem, index) -> QWidget:
    def _config_bool(value, default: bool = False) -> bool:
    def __init__(self, manager: ShortcutManager, parent=None,
                 enabled_features: Optional[set] = None) -> None:
    def keyPressEvent(self, event) -> None:
    ```
  - 影响: `parent`、`option`、`index`、`value`、`event` 均无注解；`Optional[set]` 未参数化为 `Optional[set[str]]`，与项目强类型要求不符，削弱静态检查价值。
  - 建议: 补齐 `Optional[QWidget]`、`QStyleOptionViewItem`、`QModelIndex`、`object`、`QKeyEvent` 等注解并参数化 set。
  - 置信度: 高

### 通过项
- 线程安全：全部逻辑在 UI 线程，无 OpenGL/渲染线程交互，无跨线程数据竞争面。
- GPU 资源生命周期：本文件不创建/持有任何 ModernGL 资源。
- GLSL/HDR 正确性：不涉及。
- 信号防递归（活性风险）：`currentItemChanged`/`textChanged` 处理器内的控件写操作不会再次触发自身信号，无递归路径（死标志见 AUD-4103）。
- 异常与取消路径：导出捕获 OSError 并告警（:297-302）；导入捕获 OSError/ValueError（:324-330）；preset schema/版本/未知 id/内部冲突由 ShortcutManager.import_preset 严格校验并结构化上报（:344-373 如实展示 report）。
- 输入验证（导入面）：外部 JSON 经 manager 校验（schema、version、shortcuts 必须为 dict、重复序列判定）后才应用。
- 冲突处理流程正确：用户确认覆盖后先清空冲突动作再赋值（:838-844），拒绝时刷新还原显示。

## src/app/ui/welcome_dialog.py (行数: 453)

### 发现

- [AUD-4110] [Major] `recent_files`/`recent_projects` 列表元素未做类型验证，非字符串条目可致欢迎页构造崩溃
  - 位置: src/app/ui/welcome_dialog.py:192,209-212
  - 证据:
    ```python
    recent = self._config.get("recent_files") or []
    ...
    for filepath in recent:
        basename = os.path.basename(filepath)
        dirname = os.path.dirname(filepath)
    ```
  - 影响: config.json 属外部可编辑数据（项目铁律要求外部数据须验证），若某条目为数字/字典等非 str，`os.path.basename` 抛 TypeError，`_populate_recent` 在 `_setup_ui`（:161）内执行 → WelcomeDialog 构造失败 → 启动路径 `_show_welcome_dialog`（menu_toolbar_controller.py:888-895）中断。同因 `recent_projects` 的非 str 条目会进入 :33-34 `os.path.exists(path)`，在 ThreadPool 工作线程内抛 TypeError（见 AUD-4111）。代码仅做了 `or []` 兜底（列表为空），未校验元素类型。
  - 建议: 两处循环前过滤 `if not isinstance(p, str): continue`（并可顺带截断到合理上限）。
  - 置信度: 高（代码路径确定；触发依赖配置文件被外部改写，概率低）

- [AUD-4111] [Suggestion] `_ProjectClassifyWorker.run()` 无异常兜底，任何异常都会使 `done` 不再发射、"Loading projects…" 占位行永久停留
  - 位置: src/app/ui/welcome_dialog.py:31-40
  - 证据:
    ```python
    def run(self) -> None:
        matching = []
        for path in self._projects:
            if not os.path.exists(path):
                continue
            kind = WelcomeDialog._classify_project(path)
            ...
        self.signals.done.emit(matching)
    ```
  - 影响: `run()` 内任一未捕获异常（如 :34 `os.path.exists` 收到非 str 条目；`_classify_project` 自身虽有兜底但 :34 在其之前）会使 QRunnable 中止，`done` 永不发射，`_on_projects_classified` 不执行，用户看到永久 "Loading projects…"（:242-246 的占位行）却无任何错误提示；异常仅在 stderr 打印。
  - 建议: `run()` 主体包 try/except，except 分支也应 emit（空列表或错误标志），让 UI 端总能离开占位状态。
  - 置信度: 高

- [AUD-4112] [Suggestion] 三处宽泛 `except Exception` 静默吞错，掩盖配置/保存阶段错误
  - 位置: src/app/ui/welcome_dialog.py:200-201,238-239,444-447
  - 证据:
    ```python
    try:
        from src.shared.feature_flags import get_work_mode
        if get_work_mode(self._config) == "image":
            ...
    except Exception:
        pass
    ```
    以及 :445-447 重启前 `except Exception: pass` 吞掉 `save_config()` 失败。
  - 影响: 项目铁律禁止宽泛 except 掩盖领域错误。前两处的回退行为（不过滤视频 / 回退 video 模式）下游有二次防线（menu_toolbar_controller.py:1174-1181 再次校验），后果轻微；但 :445 处吞掉保存失败后仍执行重启，若落盘失败用户重启后模式未切换而 UI 已承诺切换，产生难排查的状态不一致（注：:425 `set(auto_save=True)` 已同步落盘一次，此处属二次冗余保存，风险被稀释）。
  - 建议: 收窄捕获范围并 logger.warning 记录；重启前保存失败应提示用户而非静默重启。
  - 置信度: 高

- [AUD-4113] [Suggestion] `item.setForeground(item.foreground())` 是无效赋值，缺失文件行未按预期置灰
  - 位置: src/app/ui/welcome_dialog.py:214-218
  - 证据:
    ```python
    if not os.path.exists(filepath):
        item.setForeground(item.foreground())
        font = item.font()
        font.setItalic(True)
        item.setFont(font)
    ```
  - 影响: 把前景色设回其当前值，纯无效语句；缺失文件的条目只剩斜体标识，原意（灰色弱化）未实现，可辨识性下降。
  - 建议: `item.setForeground(QColor("#555"))` 或删除该行。
  - 置信度: 高

- [AUD-4114] [Suggestion] 类型注解缺失或不精确
  - 位置: src/app/ui/welcome_dialog.py:25,57,60
  - 证据:
    ```python
    def __init__(self, projects: list, current_mode: str):
    def _config_bool(value, default: bool = False) -> bool:
    def __init__(self, config_manager, parent=None) -> None:
    ```
  - 影响: `config_manager`（实际为 ConfigManager）、`value`、`parent` 无注解；`projects: list` 未参数化为 `list[str]`，不满足项目强类型要求。
  - 建议: 补齐 `"ConfigManager"`、`object`、`Optional[QWidget]` 注解并参数化 list。
  - 置信度: 高

### 通过项
- 线程安全（R0-7）：zip/manifest 分类在 ThreadPool 工作线程执行，worker 不触碰任何 GUI 对象，结果经 `done` 信号队列连接回 UI 线程填充；对话框提前关闭时 Qt 依据 receiver 销毁自动断连，无 dangling 调用。
- 资源生命周期：`WA_DeleteOnClose`（:64，[R2-2]）防止对话框随打开次数累积。
- GLSL/HDR、GPU 资源：不涉及。
- 输入验证（打开路径）：双击处理器校验 `os.path.exists` 且下游控制器二次校验视频/图片工作模式。
- 异常回退设计：`_classify_project` 的 `except Exception: return "video"` 有明确文档说明的保守回退（坏档案交由正式加载报错），属合理用法。
- 持久化：`show_welcome_on_startup` 与 `work_mode` 均以 `auto_save=True` 即时落盘（[R2-7]/[R2-6]），重启走统一 `restart_application()`（detached subprocess + 正常退出链）。
- 信号防递归：模式按钮用 `clicked` + 显式 `setChecked` 校正，无信号回环。
- i18n：占位符 `{mode}` 在无翻译/有翻译两条路径下均能被 `.format(mode=...)` 正确填充（I18N.tr 无 kwargs 时返回原始串）。
