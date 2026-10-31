# batch-040 [P1] src/app/ui// 审查报告

- 任务书：docs/audit/2026-09-28/assignments/batch-040.md（编号区间 AUD-4000–AUD-4099）
- 文件：project_manager_dialog.py、resize_dialog.py、script_panel.py（共 3 个，2,770 行，全部通读）
- 说明：关键结论均经真实代码行核对；script_panel 的路径缺陷、菜单泄漏、编辑操作行为另用 offscreen Qt 实测复现（未创建任何文件，仅 stdin 运行）。

## src/app/ui/project_manager_dialog.py (行数: 1,107)

### 发现

- [AUD-40001] [Major] 搜索/排序每次变更都在 UI 线程逐项目同步解压 .topos 封面，抵消 [R0-7] 异步扫描优化
  - 位置: src/app/ui/project_manager_dialog.py:268, 745-746, 626, 639-643
  - 证据:
    ```python
    self.search_box.textChanged.connect(self._on_search_changed)   # L268
    ...
    def _on_search_changed(self, text: str) -> None:
        self._apply_filter(text)                                   # L745-746
    ...
    item.setIcon(QIcon(self._project_preview(meta)))               # L626 (_add_list_item)
    ...
    cover = extract_cover_thumbnail(meta.path)                     # L639 (_project_preview)
    if cover:
        pixmap = QPixmap()
        if pixmap.loadFromData(cover):
            return self._fit_preview(pixmap)
    ```
  - 影响: `extract_cover_thumbnail`（src/shared/project/library.py:457-468）每次调用都 `zipfile.ZipFile(path)` 打开归档并读取成员。`_apply_filter` 在搜索框每个按键、每次排序变化时整表重建，对每个匹配项目在 UI 线程重新开 zip、解码 JPEG、SmoothTransformation 缩放。项目库较大时每个按键触发数十次归档 I/O+解码，启动屏明显卡顿；这正是 [R0-7] 把扫描挪进线程池想要消除的阻塞。
  - 建议: 在 `_on_scan_done` 后按 `meta.path` 缓存已解码的封面 QPixmap（刷新时失效），或将封面提取并入扫描 worker / ThumbnailProvider 管线；`_fit_preview` 结果同样可按 path 缓存。
  - 置信度: 高

- [AUD-40002] [Major] `_apply_filter` 无条件信任 `ProjectMeta.name` 为 str，畸形 project.json 可致启动屏列表构建崩溃
  - 位置: src/app/ui/project_manager_dialog.py:577-578, 593
  - 证据:
    ```python
    if self.sort_box.currentData() == "name":
        projects.sort(key=lambda meta: meta.name.casefold())       # L578
    ...
    searchable = " ".join([meta.name, *tags]).lower()              # L593
    ```
  - 影响: 生产端 `read_project_meta`（src/shared/project/library.py:384）为 `name = project.get("name") or <文件名>`，未验证类型——归档内 project.json 若 name 为数字/列表等真值非 str，则按名称排序抛 AttributeError、任意搜索按键抛 TypeError，槽内未捕获异常导致列表停留在清空状态，整个项目管理器不可用。`.topos` 是用户可手工编辑的外部数据，违反"外部数据须验证"。排序键 `meta.modified_at or ...`（L583/588）同理信任数值类型。
  - 建议: 在 `read_project_meta` 出口做类型收敛（str(x) / float(x) with fallback），或在本文件排序/搜索前统一 `str(meta.name)` 归一。
  - 置信度: 中（触发需畸形归档，代码路径已核实存在）

- [AUD-40003] [Suggestion] 删除项目后清理 recent/favorites/tags 用裸 `except Exception: pass` 静默吞掉持久化错误
  - 位置: src/app/ui/project_manager_dialog.py:1047-1066
  - 证据:
    ```python
        try:
            recent = list(self._config.get("recent_projects") or [])
            ...
            self._config.set("recent_projects", recent, auto_save=True)
            self._config.set("favorite_projects", ...)
            ...
            self._config.set("project_tags", tags, auto_save=True)
        except Exception:
            pass
    ```
  - 影响: 三次 config 写盘（auto_save=True）中任何失败（磁盘满、配置损坏、值类型异常）都被无日志吞掉；已删项目可能永久残留于 Recent/Favorites/Tags 且无从排查。违反 .clinerules"不得宽泛 except 掩盖领域错误"。
  - 建议: 缩窄为预期异常并 `logger.warning` 记录失败键名。
  - 置信度: 高

- [AUD-40004] [Suggestion] `favorite_projects`/`recent_projects` 配置值未做类型校验，字符串值会被按字符迭代并回写垃圾数据
  - 位置: src/app/ui/project_manager_dialog.py:536-540, 1048, 1054
  - 证据:
    ```python
    def _favorite_paths(self) -> set[str]:
        try:
            values = self._config.get("favorite_projects") or []
        except Exception:
            values = []
        return {os.path.abspath(str(path)) for path in values if path}
    ```
  - 影响: config.json 若被手工编辑或旧版本写坏成字符串（如 `"favorite_projects": "path"`），`for path in values` 逐字符迭代生成单字符路径集合；随后 `_set_favorite` 以 `list(self._favorite_paths())` 起底（L551）把垃圾集合持久化回配置，收藏数据损坏。`recent_projects`（L1048 `list(... or [])`）同理，str 会被 `list()` 拆成字符。
  - 建议: 读取后先 `isinstance(values, list)` 过滤，仅保留非空 str 元素再使用。
  - 置信度: 高（行为确定；触发依赖外部坏数据）

- [AUD-40005] [Suggestion] `last_project` 读取两处裸 `except Exception: pass`，无日志
  - 位置: src/app/ui/project_manager_dialog.py:308-311, 481-485
  - 证据:
    ```python
        last_project = ""
        try:
            last_project = self._config.get("last_project") or ""
        except Exception:
            pass
    ```
  - 影响: 配置异常时"继续上次项目"按钮静默不出现（L312 条件不满足），用户与开发者均无感知原因。
  - 建议: 捕获具体异常并 `logger.debug`；或依赖 ConfigManager.get 本身不抛的特性去掉 try。
  - 置信度: 高

- [AUD-40006] [Suggestion] 库扫描失败在 UI 上呈现为"暂无项目"，无任何错误反馈
  - 位置: src/app/ui/project_manager_dialog.py:89-91, 597-600
  - 证据:
    ```python
        except Exception:
            logger.exception("project library scan failed")
            projects = []
    ...
        self.empty_label.setText(
            I18N.tr("pm.no_projects", "No projects yet. Click \"New Project\" to create one.")
        )
        self.empty_label.setVisible(self.list_widget.count() == 0)
    ```
  - 影响: 扫描异常（权限、库目录损坏）与真空库显示同一文案，用户误以为项目全部丢失；仅有日志无界面提示。
  - 建议: worker 结果携带错误标志，`_on_scan_done` 区分"扫描失败/空库"两种空态文案。
  - 置信度: 高

- [AUD-40007] [Suggestion] 死代码：`_elide_name` 与 `_mode_tag` 无任何调用且 `_mode_tag` 忽略参数恒返回 "Video"
  - 位置: src/app/ui/project_manager_dialog.py:692-696, 719-721
  - 证据:
    ```python
    @staticmethod
    def _mode_tag(project_mode: str) -> str:
        return I18N.tr("pm.tag_video", "Video")
    ```
  - 影响: 误导后续维护（看似按 project_mode 区分标签）；全库 grep 无引用（已验证）。
  - 建议: 删除两者，或实现 `_mode_tag` 的真实分支。
  - 置信度: 高

- [AUD-40008] [Suggestion] 多选状态下右键菜单 "Open" 静默无效
  - 位置: src/app/ui/project_manager_dialog.py:826, 865-868
  - 证据:
    ```python
    act_open = menu.addAction(I18N.tr("pm.open", "Open"))     # L826 无条件展示
    ...
    if chosen == act_open:
        if len(selected_paths) == 1 and os.path.exists(path):  # L866 多选时为假
            register_project_library_dir(self._config, os.path.dirname(path))
            self.project_selected.emit(path)
    ```
  - 影响: 多选后右键点 Open 无任何动作与提示，也无禁用，用户以为功能失效。
  - 建议: 多选时禁用 act_open，或改为打开选中集合中的光标项。
  - 置信度: 高

- [AUD-40009] [Suggestion] 计数文案 `f"{count} projects"` 绕过 I18N
  - 位置: src/app/ui/project_manager_dialog.py:1013
  - 证据:
    ```python
    name = os.path.basename(paths[0]) if count == 1 else f"{count} projects"
    ```
  - 影响: 中文等语言环境下确认对话框混入英文复数文案；其余同类文案均已走 `I18N.tr`（如 pm.selected_count）。
  - 建议: 复用 `I18N.tr("pm.selected_count", ...)` 风格键。
  - 置信度: 高

- [AUD-40010] [Suggestion] 多个方法/参数缺类型注解
  - 位置: src/app/ui/project_manager_dialog.py:178 (`config_manager`), 509 (`projects`), 814 (`pos`), 864 等
  - 证据:
    ```python
    def _on_scan_done(self, projects) -> None:
    ...
    def _on_context_menu(self, pos) -> None:
    ```
  - 影响: 与项目强类型约定不一致；`projects` 实为 list[ProjectMeta] 未标注。
  - 建议: 补齐 `config_manager: ConfigManager`、`projects: list[ProjectMeta]`、`pos: QPoint` 等。
  - 置信度: 高

### 通过项
- 线程安全：库扫描在 QThreadPool 执行、结果经信号回 UI 线程（L72-92, L505-507）；并发刷新以 `_scan_inflight`/`_scan_pending` 合并防重入（L494-497, L513-515）；`thumbnail_ready` 由 ThumbnailProvider 主线程回调发出
- 信号防递归：搜索→过滤→`list_widget.clear()`→`itemSelectionChanged` 链路无回路；程序化刷新不写回 search_box/sort_box，无需状态锁
- 缩略图订阅生命周期：WA_DeleteOnClose 销毁时 Qt 自动断开单例 ThumbnailProvider 连接（L182-192 注释及实现）
- GPU 资源生命周期 / GLSL·HDR 正确性：本文件不涉及 GL 对象与着色器
- 状态提交一致性：重命名迁移收藏/标签并同步活动项目路径（L944-953）；删除仅清理确认已消失的路径，部分回收站失败不误删记录（L1049-1053）；恢复备份后清除旧 autosave 防止再次弹恢复提示（L906-913）
- 异常与取消路径：删除逐项 try/except 汇总失败（L1035-1046）；Reveal 子进程 OSError 捕获并告警（L921-929）；删除当前打开项目有防护（L1004-1011）
- 输入验证：`_selected_paths`/`_selected_path` 对 UserRole 数据做 isinstance(str) 校验（L725-738）；`_format_time` 捕获 OSError/ValueError

## src/app/ui/resize_dialog.py (行数: 69)

### 发现

- [AUD-40011] [Suggestion] 宽高联动用 `int()` 截断，往返修改产生 1px 漂移
  - 位置: src/app/ui/resize_dialog.py:54-66
  - 证据:
    ```python
    def on_width_changed(self, new_width):
        if self.ratio_check.isChecked():
            new_height = int(new_width / self.aspect_ratio)
            ...
    def on_height_changed(self, new_height):
        if self.ratio_check.isChecked():
            new_width = int(new_height * self.aspect_ratio)
    ```
  - 影响: 截断导致 W→H→W 往返逐步缩小（如 1920x1080 下 100→56→99），反复调整后偏离用户预期尺寸。
  - 建议: 改用 `round()`，并在联动源头记录基准值防累积漂移。
  - 置信度: 高

- [AUD-40012] [Suggestion] 方法缺类型注解（`__init__` 参数、`on_width_changed`、`get_dimensions`）
  - 位置: src/app/ui/resize_dialog.py:7, 54, 61, 68
  - 证据:
    ```python
    def __init__(self, current_width, current_height, parent=None):
    ...
    def get_dimensions(self):
        return self.width_spin.value(), self.height_spin.value()
    ```
  - 影响: 与项目强类型约定不一致。
  - 建议: 标注 `current_width: int, current_height: int` 及 `-> tuple[int, int]`。
  - 置信度: 高

### 通过项
- 信号防递归：联动 setValue 前后用 `blockSignals(True/False)` 正确包裹（L57-59, L64-66），无递归触发；初始化 setValue（L23/L29）先于 connect（L51-52），无初始副作用
- 输入验证：`current_height > 0` 除零防护（L12）；QSpinBox 范围 1-10000 钳制越界值；实际调用方传入 numpy shape 正整数（src/app/controllers/menu_toolbar_controller.py:583 `h, w = self.engine.img_full_original.shape[:2]`）
- 线程安全 / GPU 生命周期 / GLSL·HDR / 持久化：模态对话框内纯 UI，不涉及

## src/app/ui/script_panel.py (行数: 1,594)

### 发现

- [AUD-40013] [Major] 内置示例目录指向不存在的 `src/app/scripting/examples`，Scripts 菜单 "Examples" 永远为空（实测复现）
  - 位置: src/app/ui/script_panel.py:884-887
  - 证据:
    ```python
    def _discover_builtin_examples():
        """Discover built-in example scripts from src/scripting/examples/."""
        examples_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripting", "examples")
        examples_dir = os.path.normpath(examples_dir)
    ```
  - 影响: 本文件位于 `src/app/ui/`，拼出的目录是 `src/app/scripting/examples`——该目录不存在（已验证）；真实示例在 `src/features/scripting/examples/`（advanced_workflow.py、basic_grade.py 等已确认存在）。offscreen 实测 `_discover_builtin_examples()` 返回 `{}`，菜单永远显示 "(No examples)"，全部内置示例功能对用户不可见；backward-compat 入口 `_load_example`（L1535）同样失效。
  - 建议: 改为定位 `src/features/scripting/examples`（相对本文件 `../../features/scripting/examples`），或由 scripting 包导出示例目录常量，避免 UI 层硬编码相对路径。
  - 置信度: 高（路径计算与实测双重确认）

- [AUD-40014] [Major] `_get_project_root` 少一级 `..`，用户 Scripts 目录错误落在源码树 `src/Scripts` 而非项目根（实测复现）
  - 位置: src/app/ui/script_panel.py:871-881
  - 证据:
    ```python
    def _get_project_root():
        """Get the project root directory (where run_topos.command lives)."""
        # src/app/ui/script_panel.py -> src/app/ui -> src/app -> src -> project root
        return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

    def _get_scripts_dir():
        """Get the user Scripts directory path, creating it if needed."""
        scripts_dir = os.path.join(_get_project_root(), "Scripts")
        os.makedirs(scripts_dir, exist_ok=True)
    ```
  - 影响: 从 `src/app/ui` 上跳两级得到 `<root>/src`（offscreen 实测返回 `.../topos-color01/src`），而 `run_topos.command` 在仓库根。注释自述需要三级跳转却只写两个 `..`。用户脚本被写入/读取源码树内的 `<root>/src/Scripts`（该空目录已被此代码自动创建），违背文档意图、污染 src 树；打包/冻结环境下 `__file__` 指向 bundle 内只读或临时目录，`os.makedirs` 可在打开菜单或 "Save to Scripts" 时抛 OSError。
  - 建议: 补第三级 `..`；更稳妥的是把用户脚本目录改到用户数据目录（QStandardPaths）而非源码相对路径。
  - 置信度: 高（实测 `_get_project_root()` 返回 `<root>/src`；`src/Scripts/` 存在且为空）

- [AUD-40015] [Major] Scripts 菜单每次打开都新建 QMenu 且旧菜单不销毁，无限累积泄漏（实测复现）
  - 位置: src/app/ui/script_panel.py:1296-1301（配合 1237-1240, 1252, 1270）
  - 证据:
    ```python
    def _update_scripts_menu(self):
        """Rebuild the Scripts menu (called before showing)."""
        new_menu = self._build_scripts_menu()
        self.btn_scripts.setMenu(new_menu)
        # Connect aboutToShow on the new menu so it refreshes next time too
        new_menu.aboutToShow.connect(self._update_scripts_menu)
    ```
  - 影响: `_build_scripts_menu` 以 `QMenu(self)` 创建（父对象为面板），每个 action 的 lambda 捕获完整脚本文本（L1252 `c=code`、L1270）；`setMenu` 不接管也不删除旧菜单。offscreen 实测连续 5 次触发后面板 children 中 QMenu 从 1 涨到 6。长会话反复打开菜单会无上限地累积菜单+动作+全部脚本内容，且每次重建还重读磁盘上所有示例/用户脚本。
  - 建议: 复用单个 QMenu（clear 后重建动作），或 `self.btn_scripts.menu().deleteLater()` 后再 setMenu。
  - 置信度: 高（实测计数 1→6）

- [AUD-40016] [Suggestion] 菜单"每次打开前重建"实际展示的是上一代内容，新保存脚本需开两次才可见
  - 位置: src/app/ui/script_panel.py:1095-1096, 1296-1301
  - 证据:
    ```python
    # Rebuild menu each time it's about to show (to reflect file changes)
    self.btn_scripts.menu().aboutToShow.connect(self._update_scripts_menu)
    ```
  - 影响: `aboutToShow` 在弹出动作发生时才触发，此时即将显示的仍是旧菜单对象；槽内 `setMenu(new_menu)` 只影响下一次打开。用户本次看到的内容是上一次打开时构建的快照，"Save to Scripts" 刚保存的脚本本菜单中不可见，与注释声明的刷新意图相悖。
  - 建议: 结合 AUD-40015 改为单菜单 clear+rebuild（aboutToShow 内清空再填），即可同时解决滞后问题。
  - 置信度: 中（Qt 弹出时序推断，未 GUI 实测弹出过程）

- [AUD-40017] [Suggestion] 未使用的导入：`glob as glob_mod`、`QToolBar`、`QSizePolicy`、`QBrush`
  - 位置: src/app/ui/script_panel.py:10, 17, 23
  - 证据:
    ```python
    import glob as glob_mod          # L10，全文件无 glob_mod 引用（grep 验证）
    QSplitter, QFileDialog, QMenu, QToolBar, QSizePolicy, QApplication,  # L17
    QBrush, QPolygonF,               # L23，QBrush 无引用
    ```
  - 影响: 冗余依赖与噪音；`QPolygonF` 在函数内重复局部导入（L301, 320, 337, 355）亦属同类问题。
  - 建议: 删除未用导入，将函数内 `from PyQt6.QtGui import QPolygonF` 提升到文件头（已有）。
  - 置信度: 高

- [AUD-40018] [Suggestion] 工具栏提示承诺的快捷键未实现：F5 运行、Ctrl+N 新建
  - 位置: src/app/ui/script_panel.py:1059, 1067, 1104
  - 证据:
    ```python
    self.btn_new.setToolTip("New script (Ctrl+N)")    # L1059，无 Ctrl+N QAction
    self.btn_open.setToolTip("Open script (Ctrl+O)")  # L1067，Ctrl+O 已接线（L1149-1152）
    self.btn_run.setToolTip("Run script (F5)")        # L1104，全 src 无 F5 绑定（grep 验证）
    ```
  - 影响: 用户按 F5/Ctrl+N 无响应，功能看似损坏；Ctrl+O 实际可用，行为不一致。
  - 建议: 为 Run/New 补 QAction 快捷键（注意与主窗口全局快捷键的冲突处理，参照 L1146-1148 对 Cmd+S 的处理注释），或修正 tooltip。
  - 置信度: 高（grep 全库确认 F5 与 Ctrl+N 无绑定）

- [AUD-40019] [Suggestion] 大量用户可见字符串绕过 I18N 硬编码英文
  - 位置: src/app/ui/script_panel.py:1059, 1067, 1075, 1091-1096, 1113, 1189, 1318, 1482-1489, 1504-1505, 1518-1523 等
  - 证据:
    ```python
    reply = QMessageBox.question(
        self,
        "Unsaved Changes",
        "The current script has unsaved changes.\n"
        "Discard and continue?",                       # L1481-1485 硬编码
    ...
    QMessageBox.critical(self, "Open Error", f"Failed to open file:\n{e}")  # L1318
    ```
  - 影响: 同文件已混用 `I18N.tr`（script.find、script.run 等），未覆盖字符串在非英文环境下保持英文，i18n 覆盖不完整。
  - 建议: 统一走 `I18N.tr`（含 SearchBar 各 tooltip、上下文菜单 Float/Dock/Copy 等）。
  - 置信度: 高

- [AUD-40020] [Suggestion] `logger.debug(f"...")` 无条件做字符串格式化
  - 位置: src/app/ui/script_panel.py:1462, 1466, 1469
  - 证据:
    ```python
    logger.debug(f"_on_run called, code length={len(code)}, empty={not bool(code.strip())}")
    ...
    logger.debug(f"emitting run_clicked signal")
    ```
  - 影响: 每次 Run 点击无论日志级别都执行格式化；且当前 debug 级日志属开发残留（含拼写问题 "empty= False"）。
  - 建议: 改 `logger.debug("...%s...", arg)` 惰性形式或删除残留调试日志。
  - 置信度: 高

- [AUD-40021] [Suggestion] 类型注解缺失（`_build_scripts_menu`、`_do_search`、`_do_replace`、`_on_save`、`_add_recent_file` 等大量槽与辅助方法）
  - 位置: src/app/ui/script_panel.py:1237, 1335, 1364, 1381-1385, 1517 等
  - 证据:
    ```python
    def _do_search(self, text, forward):
    ...
    def _do_replace(self, find_text, replace_text, replace_all):
    ...
    def _on_save(self):
    ```
  - 影响: 与项目强类型约定不一致；公开槽 `append_output`/`set_running` 等已标注，私有部分大面积缺失。
  - 建议: 按现有公开 API 风格补齐参数与返回注解。
  - 置信度: 高

### 通过项
- 线程安全：run/stop 经 `run_clicked`/`stop_clicked` 信号交给后台 QThread 的 ScriptEngine，输出经 `output_ready`/`error_output_ready` 信号回 UI 线程（src/app/main_window.py:1704-1707 连接），本面板无跨线程直调
- 信号防递归：编辑器/控制台菜单与搜索均为单向信号流，无需状态锁
- 编辑操作正确性（offscreen 实测）：多行 Tab 缩进（含空行/短行）、Shift+Tab 反缩进、Ctrl+/ 注释切换均产生正确结果；`_do_replace` replace_all 对自包含替换（"aa"→"aab"、"a"→"aa"）无死循环
- 状态一致性：`document().setModified(False)` 在 new/open/save/example/recent 各路径统一维护；未保存更改有确认对话框且覆盖全部加载入口（L1475-1491）；最近文件经 QSettings 持久化并截断至 10 条
- 异常与取消路径：文件读写捕获具体异常（IOError/OSError/UnicodeDecodeError）并弹窗告知；运行中编辑器置只读、Run/Stop 按钮状态互斥（L1422-1426）；Stop 重复点击被禁用防抖（L1471-1473）
- 性能模式：控制台 `setMaximumBlockCount(5000)` 限制无限增长（L1220）；控制台搜索 200ms 去抖 QTimer（L1209-1212）
- GPU 资源生命周期 / GLSL·HDR 正确性：本文件不涉及

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 5 | AUD-40001, AUD-40002, AUD-40013, AUD-40014, AUD-40015 |
| Suggestion | 16 | AUD-40003–40012, AUD-40016–40021 |

其中 AUD-40013 / AUD-40014 / AUD-40015 /（及通过项中的编辑操作行为）均以 offscreen Qt 实测复现，非纯静态推断。
