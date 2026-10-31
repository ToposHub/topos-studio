# batch-030 [P1] src/app/controllers//

任务书：docs/audit/2026-09-28/assignments/batch-030.md
编号区间：AUD-3000–AUD-3099
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解

## src/app/controllers/menu_toolbar_controller.py (行数: 1883)

### 发现

- [AUD-3000] [Major] `QLabel` 未导入，GPU 信息标签永远创建失败且被宽泛 except 静默吞掉
  - 位置: src/app/controllers/menu_toolbar_controller.py:1566
  - 证据:
    ```python
    if self.config_manager.get("show_gpu_info"):
        try:
            from src.shared.ai_models.onnx_env import get_gpu_provider_info
            gpu_info = get_gpu_provider_info()
            gpu_label = QLabel(f"GPU: {gpu_info.get('description', 'N/A')}")
            gpu_label.setStyleSheet("color: #888; font-size: 11px; margin-right: 8px;")
            self.status_bar.addPermanentWidget(gpu_label)
        except Exception:
            pass
    ```
    文件头 import（第 15–20 行）只导入 QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QToolBar, QStatusBar, QMessageBox, QMenu, QInputDialog, QApplication, QProgressBar, QToolButton, QSizePolicy, QComboBox, QDialog，全文无 QLabel 导入（第 1025 行的 "QLabel" 只是样式表字符串）。
  - 影响: 开启 `show_gpu_info` 后第 1566 行必然抛 `NameError`（NameError 是 Exception 子类），被 `except Exception: pass` 吞掉——GPU 信息标签从不显示，功能静默失效；同时违反「不得宽泛 except 掩盖领域错误」铁律，缺陷被掩盖至今。
  - 建议: 在 QtWidgets 导入列表中加入 QLabel；except 改为 `logger.debug(...)` 记录失败原因。
  - 置信度: 高

- [AUD-3001] [Major] `panel_opacity` 预览对停靠（非浮动）dock 无效，且该设置全库无任何启动应用路径
  - 位置: src/app/controllers/menu_toolbar_controller.py:816-820
  - 证据:
    ```python
    elif key == 'panel_opacity':
        # Apply panel opacity preview
        opacity = value / 100.0 if isinstance(value, (int, float)) else 1.0
        for dock in getattr(self, 'all_docks', []):
            dock.setWindowOpacity(opacity)
    ```
    全库 grep：`setWindowOpacity` 仅此一处调用；`panel_opacity` 在 preferences_dialog.py 中只做读取/保存（1947 行 `cfg.set('panel_opacity', ...)`）与预览回显，没有任何启动时应用逻辑。
  - 影响: 两层问题：(1) 停靠在 QMainWindow 内的 QDockWidget 不是顶层窗口，Qt 的 windowOpacity 属性对其无效，预览大概率无视觉效果；(2) 用户在偏好设置中保存的 panel_opacity 值除这段瞬时预览外永远不会被应用（重启后也不生效），设置项形同虚设。
  - 建议: 对停靠面板改用 QGraphicsOpacityEffect（逐面板）或在面板样式表中实现透明度；并在启动时统一应用该配置项。
  - 置信度: 中（「无启动应用路径」「全库唯一调用点」已 grep 验证为高；Qt 对非顶层窗口 windowOpacity 无效的行为基于 Qt 文档，未运行时实测）

- [AUD-3002] [Suggestion] `_sync_checkable_module_actions` 新建 `ConfigManager()` 而非复用 `self.config_manager`
  - 位置: src/app/controllers/menu_toolbar_controller.py:1766-1774
  - 证据:
    ```python
    if config is None:
        try:
            from src.shared.config.config_manager import ConfigManager

            config = ConfigManager()
        except Exception:  # noqa: BLE001
            config = False
    if not config:
        continue
    ```
  - 影响: 主窗口明明持有 `self.config_manager`（本文件内引用 22 处），这里却重新实例化并从磁盘再读一遍——若主窗口内存态配置尚未落盘则读到旧值；构造失败时用 `False` 哨兵静默跳过整个同步逻辑，无日志。
  - 建议: 直接使用 `self.config_manager`；失败分支至少 `logger.debug`。
  - 置信度: 高（设计缺陷确凿，当前功能影响低）

- [AUD-3003] [Suggestion] `show_preferences_dialog` 等多处 `except Exception: pass` 无日志吞错
  - 位置: src/app/controllers/menu_toolbar_controller.py:748-751、776-779（同类：1234-1238、1876-1883）
  - 证据:
    ```python
    try:
        module_pages.extend(mod.register_preference_pages())
    except Exception:
        pass
    ...
    if hasattr(self, "_setup_ai_editing"):
        try:
            self._setup_ai_editing()
        except Exception:
            pass  # 装配失败不阻塞；AI 面板保持原状态
    ```
  - 影响: 模块偏好页注册失败、AI 子系统重装配失败时完全无痕（1876-1883 一处已有 debug 日志，其余没有），违反宽泛 except 规范，线上排障困难。
  - 建议: 统一改为 `except Exception: logger.exception(...)`（或至少 debug 级）。
  - 置信度: 高

- [AUD-3004] [Suggestion] lambda 内以 `or` 链接多个调用，依赖返回值 None 的脆弱写法
  - 位置: src/app/controllers/menu_toolbar_controller.py:231、297
  - 证据:
    ```python
    crop_act.triggered.connect(lambda: self.dock_crop.setVisible(True) or self.dock_crop.raise_() or self.crop_panel.set_crop_active(True))
    ...
    show_panel_act.triggered.connect(lambda: self.dock_plugin.setVisible(True) or self.dock_plugin.raise_())
    ```
  - 影响: 依赖 `setVisible`/`raise_` 返回 None（falsy）才继续求值后继调用；任一 API 将来返回真值即短路跳过剩余步骤，且单行嵌套可读性差。
  - 建议: 改为具名方法或普通顺序调用（def 内三行）。
  - 置信度: 高

- [AUD-3005] [Suggestion] `disable_all_layers` 绕过 `on_layer_visibility_changed` 直接改 `layer.visible`，逻辑重复
  - 位置: src/app/controllers/menu_toolbar_controller.py:632-639
  - 证据:
    ```python
    def disable_all_layers(self):
        """关闭所有图层的可见性。"""
        for i, layer in enumerate(self.engine.layers):
            layer.visible = False
        self.layer_panel.update_layers(self.engine.layers, self.engine.active_layer_index)
        self.request_render()
        self._trigger_autosave()
    ```
  - 影响: 与 layer_controller.py:111 `on_layer_visibility_changed`（`layers[index].visible = visible; request_render(); _trigger_autosave()`）写入路径重复，两处需人工保持一致。另注：可见性变更（单层与批量均如此）不进 undo 历史，Disable All 之后无法 Ctrl+Z 恢复——与现状单层切换行为一致，属一致性缺口而非回归。
  - 建议: 循环改调 `self.on_layer_visibility_changed(i, False)`（或批量后统一刷新一次面板）；若产品要求可撤销，可在批量前 `history.push_state`。
  - 置信度: 高（现状描述）；中（是否应可撤销属产品决策）

- [AUD-3006] [Suggestion] 未使用导入 `sys`/`subprocess`/`time`；工具栏 undo/redo 为永不触发的死 action
  - 位置: src/app/controllers/menu_toolbar_controller.py:9-13、1308-1312
  - 证据:
    ```python
    import os
    import sys
    import subprocess
    import time
    ...
    # Actions for Undo/Redo (Hidden from toolbar but exist for shortcuts)
    self.undo_action = QAction("Undo", self)
    self.undo_action.triggered.connect(self.undo)
    self.redo_action = QAction("Redo", self)
    self.redo_action.triggered.connect(self.redo)
    ```
  - 影响: `sys`/`subprocess`/`time` 全文未使用（grep 验证仅 `os`、`logging` 被用到）；`self.undo_action`/`self.redo_action` 既未 setShortcut 也未加入任何 widget/菜单，注释所称"exist for shortcuts"不成立，永远不会被触发（真实入口是菜单里的 undo_act/redo_act）。
  - 建议: 删除未用导入与死 action（如外部无引用；菜单版已注册 ShortcutManager）。
  - 置信度: 高

- [AUD-3007] [Suggestion] 状态栏与对话框文案硬编码英文（及个别硬编码中文默认值），未走 I18N.tr
  - 位置: src/app/controllers/menu_toolbar_controller.py:587、591、595、620、639、1572、1540、1551
  - 证据:
    ```python
    self.status_bar.showMessage(f"Resized to {nw}x{nh}", 3000)
    self.status_bar.showMessage("Image Rotated", 2000)
    name, ok = QInputDialog.getText(self, "Rename Layer", "New Name:", text=layer.name)
    self.status_bar.showMessage("All layers disabled", 2000)
    self.status_bar.showMessage("Ready")
    ...
    self._video_export_pause_btn = QPushButton(
        I18N.tr("export.pause", "暂停"))
    ```
  - 影响: 中文界面下这些提示/对话框仍显示英文；而 export.pause 等的默认文案却是中文——本地化方向不一致，切语言后混排。
  - 建议: 统一走 `I18N.tr` 并补齐 zh/en 词条。
  - 置信度: 高

- [AUD-3008] [Suggestion] `_inject_shortcut_text` 追加的 `\t快捷键` 文本在用户自定义快捷键后不会刷新
  - 位置: src/app/controllers/menu_toolbar_controller.py:499-509
  - 证据:
    ```python
    seq = action.shortcut()
    if not seq.isEmpty() and '\t' not in action.text():
        action.setText(
            action.text() + '\t' +
            seq.toString(QKeySequence.SequenceFormat.NativeText)
        )
    ```
  - 影响: 仅在 `setup_menus` 末尾执行一次；之后 ShortcutDialog 修改快捷键只更新 QAction 的 shortcut，不重写 action.text() 尾部的 `\t` 显示，菜单（尤其 toolbar 弹出菜单）展示旧快捷键；text 被改写也可能影响任何按文本匹配 action 的逻辑。
  - 建议: 在快捷键变更回调后重跑注入（先剥离旧 `\t` 段），或改用 Qt 原生 shortcut 显示机制。
  - 置信度: 中

- [AUD-3009] [Suggestion] `_build_panels_menu` 循环体内重复计算 `registry.get_categories(...)`
  - 位置: src/app/controllers/menu_toolbar_controller.py:1832、1843
  - 证据:
    ```python
    for category in registry.get_categories(enabled_features, work_mode):
        descriptors = grouped[category]
        ...
        if len(registry.get_categories(enabled_features, work_mode)) <= 2:
    ```
  - 影响: 每个分类迭代都重新计算一次完整类别列表（含过滤），纯冗余；菜单构建为一次性路径，影响轻微。
  - 建议: 循环前提取 `categories = registry.get_categories(...)` 复用。
  - 置信度: 高

- [AUD-3010] [Suggestion] `on_dependency_report_requested` 在 UI 线程同步扫描 LUT/插件/AI 模型依赖
  - 位置: src/app/controllers/menu_toolbar_controller.py:1604-1605
  - 证据:
    ```python
    report = build_dependency_report(project, base_dir=base_dir)
    DependencyReportDialog(self, report).exec()
    ```
  - 影响: `build_dependency_report` 同步遍历工程引用的 LUT/插件/AI 模型文件系统，库目录较大或网络卷路径时会冻结 UI（菜单触发、用户直接感知）。
  - 建议: 移到后台线程构建（复用项目现成的任务队列模式），完成后在 UI 线程弹对话框；期间可显示进度。
  - 置信度: 中（扫描规模未实测，方向性风险）

- [AUD-3011] [Suggestion] 最近项目文件不存在时点击无任何反馈
  - 位置: src/app/controllers/menu_toolbar_controller.py:1288-1295
  - 证据:
    ```python
    def _open_recent_project(self):
        """Open a project from the Recent Projects menu."""
        action = self.sender()
        if not action:
            return
        filepath = action.data()
        if filepath and os.path.exists(filepath):
            self._open_project_path(filepath)
    ```
  - 影响: 项目包被移动/删除后，用户点击 Recent Projects 菜单项完全无响应、无提示，看起来像功能失灵。
  - 建议: 不存在时提示"文件已被移动或删除"并可提供从列表移除入口。
  - 置信度: 高

- [AUD-3012] [Suggestion] 公开方法普遍缺少参数/返回类型注解
  - 位置: 全文件（示例：40 `def setup_menus(self):`、579 `def show_resize_dialog(self):`、645 `def toggle_fullscreen(self):`、1301 `def setup_toolbar(self):`）
  - 证据:
    ```python
    def setup_menus(self):
    def show_resize_dialog(self):
    def rotate_image(self, clockwise):
    def toggle_fullscreen(self):
    ```
  - 影响: 项目规范要求强类型（AGENTS.md / .clinerules）；本混入文件绝大多数方法无注解，仅 `_register_all_shortcuts`、`change_language`、`_scope_timeline_delete_shortcuts` 等少数方法有。
  - 建议: 补齐参数与返回类型注解（UI 方法多为 `-> None`）。
  - 置信度: 高

### 通过项
- 线程安全：纯 UI 线程控制器；渲染一律经 `engine.render_pipeline(...)`（内部走 AsyncRenderer，见 image_engine.py:2698 起的说明）或 `self.request_render()`，`reset_view_to_actual` 用的 `render_reason='viewport_refresh'` 为既有异步管线入口，未发现 UI 线程直接操作 OpenGL。
- GPU 资源生命周期：本文件不创建/持有任何 ModernGL 对象、纹理或缓冲区，无泄漏面。
- GLSL/HDR 正确性：无着色器代码与像素数据处理，不适用（通过）。
- 信号防递归：`toggle_mask_overlay` 的 `mask_act.setChecked → mask_toggled → main_window lambda → mask_overlay_act.setChecked` 链为幂等设置（setChecked 不发 triggered），不会回环（已核对 canvas.py:2208-2211 与 main_window.py:544-545）；page 按钮与布局 combo 无程序化回写路径。
- 状态提交一致性（undo/redo/持久化）：resize/rotate/flip 的 undo 由 engine 侧自行 `push_state`（image_engine.py:4232、4241、4252 已核实）；`on_interaction_started` 的 `push_state(..., copy_image=False)` 符合 history.py:33-35 对不可变源帧的文档约定，且 `_serialize_layers` 经 `layer.to_dict()` 快照、引擎几何操作均为替换而非原地修改，无引用别名问题；最近文件/项目列表与 ConfigManager 同步重建、去重、限量。
- 异常与取消路径：ResizeDialog / New Project / Welcome / Project Manager 的取消分支均检查返回值；`_new_project_from_manager` 创建或保存失败有 QMessageBox 明确报错并保留管理器对话框；启动兜底 `_auto_create_edit_project` 失败时记日志并 `QApplication.quit()`。
- 输入验证：新项目对话框对空名称禁用 Create、空名返回、目录不存在时 `mkdir(parents=True)` 且 OSError 回退库目录、重名自动追加序号；`_add_to_recent_projects` 空路径剔除、绝对路径去重、max 数量截断。
- 性能模式：菜单/Scripts/布局/Workspaces 子菜单采用 aboutToShow 懒重建；`_next_untitled_project_name` 的 while 循环以有限集合为界必然终止。
- 外部引用完备性：文件引用的 `_open_project_path`、`_auto_create_edit_project`、`_reject_video_in_image_mode`、`_get_work_mode` 等混入方法均存在于 file_controller.py（grep 核实）。
