# batch-038 审查报告 — src/app/ui//

任务书：docs/audit/2026-09-28/assignments/batch-038.md
编号区间：AUD-3800–AUD-3899
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性 / 异常与取消 / 输入验证 / 性能 / 类型注解
统计：Critical 1 · Major 5 · Suggestion 7

---

## src/app/ui/__init__.py (行数: 27)

### 发现

（无）

### 通过项
- 通过（无逻辑）：纯 re-export 包入口，导出 ResizeDialog / ShortcutDialog / InfoPanel / LibraryPanel / FilesPanel / AudioMixerPanel / ScriptPanel / PreferencesDialog / WelcomeDialog，`__all__` 与导入一致，无循环导入风险（各子模块均不回导本包）。

---

## src/app/ui/audio_mixer.py (行数: 5)

### 发现

（无）

### 通过项
- 通过（无逻辑）：AudioMixerPanel 的 app 级 facade，单行 re-export 自 src.shared.ui.audio_mixer，实现归 shared 层批次审查。

---

## src/app/ui/files_panel.py (行数: 409)

### 发现

- [AUD-3804] [Major] 依赖缺失时 `_load_url` 无限重试，错误页加载成功会重置重试计数，绕过 [CRASH-6] 的有上限退避设计
  - 位置: src/app/ui/files_panel.py:170-178（配合 180-188）
  - 证据:
    ```python
    def _load_url(self):
        """延迟加载 URL，确保服务器已启动"""
        if not self.adapter.start_server():
            self._show_error_page()
            QTimer.singleShot(4000, self._load_url)
            return
    ```
    以及 `_on_page_loaded` 的成功分支：
    ```python
    if success:
        self._page_loaded = True
        self._load_retries = 0
    ```
  - 影响: `start_server()` 因依赖缺失持续返回 False 时（adapter.py:219-226，这正是错误页设计针对的场景），每 4 秒 setHtml 重载一次错误页且永不停止；每次 setHtml 触发 `loadFinished(True)`，把 `_page_loaded=True`、`_load_retries=0`，[CRASH-6] "重试有上限" 的设计在此路径完全失效。同时 `_page_loaded=True` 使后续 `set_root_path` 向错误页发 JS（只留一条告警日志），且 `_wait_server_healthy` 的探测被短路。
  - 建议: `_load_url` 的启动失败分支引入独立的失败计数上限（复用 `_load_retries` 且不在错误页 loadFinished 中重置，或对 `_show_error_page` 置 `_is_error_page` 标志让 `_on_page_loaded` 忽略其成功回调）；超限后停表并保留错误页。
  - 置信度: 中（机制确定；触发依赖 file_explorer 持续不可用）

- [AUD-3806] [Suggestion] `_pending_path` 一次性消费：前端 API 未就绪时目录同步静默丢失且不重试
  - 位置: src/app/ui/files_panel.py:185-188（配合 296-304）
  - 证据:
    ```python
    if self._pending_path:
        self._set_path_js(self._pending_path)
        self._pending_path = None
    ```
    而 JS 侧有就绪守卫：
    ```python
    "const api = window.__FE; "
    "if (!api || typeof api.loadPath !== 'function') return null; "
    ```
  - 影响: loadFinished 时若前端 `window.__FE` 尚未初始化（异步模块加载的应用常见），`_set_path_js` 仅返回 null 触发一条 warning，`_pending_path` 已被清空——LibraryPanel 与 FilesPanel 的目录同步丢失，用户看到两面板路径不一致且无自愈路径。
  - 建议: `_run_js` 回调结果为 null 且 purpose 为 set_path 时保留 `_pending_path` 并以短退避重试（上限 2-3 次），或改为等待前端就绪事件。
  - 置信度: 中

- [AUD-3807] [Suggestion] 多数方法缺类型注解
  - 位置: src/app/ui/files_panel.py:79-369（如 `__init__(self, parent=None)`、`_load_url(self)`、`_on_page_loaded(self, success)`、`set_root_path(self, path)`、`refresh_file_rating(self, path, rating)`、`_show_context_menu(self, pos)`）
  - 证据:
    ```python
    def _on_page_loaded(self, success):
        """页面加载完成后的回调"""
    ```
  - 影响: 违反项目强类型约束（.clinerules / AGENTS.md 要求所有函数有类型注解），`success: bool`、`path: str`、`rating: int` 等契约只能靠读实现推断。
  - 建议: 为公开方法与 Qt 回调补充参数/返回值注解；`_run_js`、`_invalidate_fe_dir_cache`、`_get_top_level_widget` 已是良好范例。
  - 置信度: 高

### 通过项
- 线程安全：健康探针（_ServerHealthProbe）在守护线程发 `probe_finished`，QObject 信号跨线程自动队列投递回 GUI 线程，`_probe_inflight` 防并发；缓存失效在守护线程只读 adapter 属性并访问 HTTP，不触 GUI/OpenGL。
- 信号防递归：与 LibraryPanel 的双向目录同步经 JS `loadPath(..., skipBridge=true)` 断链（fe-actions.js:123 及 SyncBridge Hook 段），Python→JS 方向不回发 `onDirectoryChanged`，无乒乓循环；`loadFinished` 仅连接一次。
- 输入验证 / 注入防护：路径进 JS 前一律 `json.dumps(str(path))` 字面量化（[SEC-7]），rating 强制 `int(rating)`。
- 异常与取消路径：`shutdown()` 幂等且捕获异常记录（MainWindow.closeEvent:1934 确认调用）；`renderProcessTerminated` 有恢复重载；`runJavaScript` 带回调使失败可见化（[ARCH-4]）；`stop_server` 3s 等待后 terminate 兜底。
- GPU 资源生命周期 / GLSL·HDR：本文件无 OpenGL 与着色器路径，无违例。
- 性能模式：健康探活替代盲等（[ARCH-7]）、指数退避（[CRASH-6]）方向正确（缺陷仅见 AUD-3804 的重置问题）。

---

## src/app/ui/info_panel.py (行数: 176)

### 发现

- [AUD-3805] [Major] `update_metadata` 不清空缺失字段，切换图片后残留上一张的 EXIF 值
  - 位置: src/app/ui/info_panel.py:160-176
  - 证据:
    ```python
    def update_metadata(self, metadata: dict):
        if not metadata:
            return
        normalized: dict[str, str] = {}
        for src_key, value in metadata.items():
            panel_key = self._KEY_MAP.get(src_key)
            if panel_key:
                normalized[panel_key] = str(value)
        for key, label in self.info_labels.items():
            if key in normalized:
                label.setText(normalized[key])
    ```
  - 影响: 只更新新元数据中出现的键。从"有镜头/ISO 信息的图 A"切到"无这些字段的图 B"（如 RAW 无 Lens EXIF、rawpy 走 snake_case 回退时部分键缺失）后，面板继续显示图 A 的镜头、ISO 等旧值——向用户展示错误的拍摄参数。且 `if not metadata: return` 使调用方无法用空字典清空面板。
  - 建议: 循环改为 `label.setText(normalized.get(key, "-"))`；空 metadata 时也应重置为 "-"（或提供 `clear()`）。
  - 置信度: 高

- [AUD-3808] [Suggestion] 用户勾选的可选字段（visible_keys）不持久化
  - 位置: src/app/ui/info_panel.py:119-125
  - 证据:
    ```python
    def toggle_field(self, key: str):
        """切换字段的显示状态"""
        if key in self.visible_keys:
            self.visible_keys.remove(key)
        else:
            self.visible_keys.add(key)
        self.update_visible_rows()
    ```
  - 影响: 与 LibraryPanel（last_library_path 入 ConfigManager）等其他面板的偏好持久化行为不一致，用户每次重启需重新勾选可选字段。
  - 建议: toggle 后将 `visible_keys - default_keys` 写入 ConfigManager（如 `info_panel_optional_fields`），构造时读回。
  - 置信度: 高

### 通过项
- 信号防递归：面板不发射任何信号，右键菜单 lambda 用 `k=key` 默认参数正确捕获循环变量。
- 线程安全：仅 GUI 线程使用，无跨线程状态。
- 异常与取消路径：无 IO/外部调用，无需错误路径。
- 输入验证：`_KEY_MAP` 同时兼容 Title Case 与 snake_case（rawpy 回退）键，未知键安全忽略。
- GPU 资源生命周期 / GLSL·HDR / 性能：静态 QWidget 展示，无相关风险；`update_visible_rows` 仅遍历 14 个固定行。

---

## src/app/ui/library_adapter.py (行数: 15)

### 发现

（无）

### 通过项
- 通过（无逻辑）：向后兼容 re-export，转发 `FileExplorerAdapter as LibraryAdapter` 及 PROJECT_ROOT / external_path / file_explorer_path / file_explorer_app / find_available_port / make_server / read_port_config / FlaskWorker 共 8 个符号，均带 noqa: F401 注明，指向 src/app/integrations/file_explorer/adapter。
- 线程安全：adapter 单例的 `get_server_url` 只读属性，守护线程调用可接受。

---

## src/app/ui/library_panel.py (行数: 612)

### 发现

- [AUD-3800] [Critical] `rename_item` 目标已存在时 `os.rename` 静默覆盖同名文件，无冲突检查
  - 位置: src/app/ui/library_panel.py:599-603
  - 证据:
    ```python
    def rename_item(self, path):
        name, ok = QInputDialog.getText(self, "Rename", "New Name:", text=os.path.basename(path))
        if ok and name:
            try: os.rename(path, os.path.join(os.path.dirname(path), name))
            except Exception as e: QMessageBox.critical(self, "Error", str(e))
    ```
  - 影响: POSIX `rename(2)` 语义下，目标为已存在的普通文件时被原子替换且无任何提示（macOS/Linux 均如此）。用户在对话框输入一个已存在的文件名（如去掉序号时与相邻文件重名），被覆盖的文件内容直接丢失，且原图像与其 `.topos` 工程的关联一并破坏——不可恢复的数据丢失。
  - 建议: 执行前 `if os.path.exists(new_path): QMessageBox.question(...)` 要求确认或直接拒绝；或改用 `os.replace` 之外的带冲突检测方案（finder 风格自动追加 " 2" 后缀）。
  - 置信度: 高（覆盖机制确定；触发需用户输入已存在名称，文件管理器同类操作均做冲突检查）

- [AUD-3801] [Major] paint 热路径每项执行 stat 调用并每次新建 SQLite 连接查询评分，缩略图风暴放大为整屏重绘
  - 位置: src/app/ui/library_panel.py:59-60、93-94、164、222、251（放大器：34-36）
  - 证据:
    ```python
    file_path = index.data(QFileSystemModel.Roles.FilePathRole)
    is_dir = os.path.isdir(file_path)
    ...
    has_project = not is_dir and os.path.exists(project_path)
    ...
    rating = self.db.get_file_rating(file_path)
    ```
    评分查询底层（src/shared/db/database.py:110-116）：
    ```python
    def get_file_rating(self, path):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT rating FROM file_meta WHERE path = ?', (_meta_key(path),))
        row = cursor.fetchone()
        conn.close()
    ```
    以及每个缩略图到达触发全 viewport 重绘（library_panel.py:34-36）：
    ```python
    def on_thumbnail_ready(self, path, pixmap):
        if self.view:
            self.view.viewport().update()
    ```
  - 影响: 每次重绘对每个可见项做 2 次 stat（isdir + exists）加一次"connect+query+close"的 SQLite 往返；进入大目录时每个缩略图经队列信号回到主线程都触发一次整 viewport 重绘（thumbnail_provider.py:1121 逐 path 发射），N 个文件 ≈ N 次全量重绘 × 每项 DB 连接——GUI 线程明显卡顿，网络盘/外置盘上 stat 延迟进一步放大，违背 4K 实时性能基准。
  - 建议: 评分在目录加载时批量读入内存 dict（`SELECT path, rating FROM file_meta WHERE path IN (...)`）由 delegate 查缓存；`is_dir` 改用 `QFileSystemModel` 已有的角色/flags 判断；`.topos` 存在性结果按路径缓存并在 `directoryLoaded`/文件系统变更时失效；`on_thumbnail_ready` 用 `viewport().update()` 之外可仅 update 对应 index 区域或做节流。
  - 置信度: 高

- [AUD-3802] [Major] 按 Rating 排序时 `lessThan` 每次比较新建 SQLite 连接，大目录排序冻结 UI
  - 位置: src/app/ui/library_panel.py:328-343
  - 证据:
    ```python
    if self.sort_criteria == "rating":
        r1 = self.db.get_file_rating(left_path)
        r2 = self.db.get_file_rating(right_path)
        return r1 < r2
    ```
  - 影响: `get_file_rating` 每次 connect/execute/close（database.py:110-116）。QSortFilterProxyModel 排序 N 项约 O(N log N) 次比较、每次 2 个连接——2000 个文件 ≈ 4 万+ 次连接建立与查询，全部在 GUI 线程同步执行，切换到 Rating 排序即长 freezes。
  - 建议: `set_sort_criteria("rating")` 时一次性把当前目录的 `{path: rating}` 读入 dict（单连接批量），`lessThan` 查 dict；目录变更时重建。
  - 置信度: 高

- [AUD-3803] [Major] 重命名/删除不迁移 `.topos` 工程边车文件与 DB 评分记录，编辑关联静默断裂
  - 位置: src/app/ui/library_panel.py:599-609
  - 证据:
    ```python
    try: os.rename(path, os.path.join(os.path.dirname(path), name))
    ...
    if os.path.isdir(path): import shutil; shutil.rmtree(path)
    else: os.remove(path)
    ```
    而徽章/调色均按路径关联边车（本文件 93、234、292 行）：
    ```python
    project_path = f"{os.path.splitext(file_path)[0]}.topos"
    has_project = not is_dir and os.path.exists(project_path)
    ```
  - 影响: 重命名 `IMG_001.arw` → `IMG_002.arw` 后，`IMG_001.topos` 成为孤儿：编辑徽章消失、中键"应用调色"失效、用户此前保存的调色工程与新文件失去关联；`file_meta` 中的评分行同样按旧路径残留。删除文件时 `.topos` 边车与 DB 行也不清理，磁盘与数据库累积孤儿数据。
  - 建议: rename 时同步 `os.rename` 对应 `.topos`（存在时）并迁移 DB 行（`UPDATE file_meta SET path=... WHERE path=...`，注意 NFC 键约定）；delete 时询问是否连同删除 `.topos` 并清理 DB 行。
  - 置信度: 中（未发现其他模块补偿此逻辑；MainWindow 仅接线信号，未见 rename hook）

- [AUD-3809] [Suggestion] `delete_item` 用 `shutil.rmtree` 永久删除目录，确认框只显示 basename
  - 位置: src/app/ui/library_panel.py:604-609
  - 证据:
    ```python
    if QMessageBox.question(self, "Delete", f"Delete {os.path.basename(path)}?") == QMessageBox.StandardButton.Yes:
        try:
            if os.path.isdir(path): import shutil; shutil.rmtree(path)
    ```
  - 影响: 整棵目录树不经回收站直接删除，且确认文案只有末级目录名，用户容易低估影响范围（误删大量素材且不可恢复，叠加 AUD-3800 的数据丢失面）。
  - 建议: 改用 `send2trash`（或 QDesktopServices 到废纸篓的等价方案）；确认框显示完整路径与目录内项目数。
  - 置信度: 高

- [AUD-3810] [Suggestion] `set_root_path` 未校验 isdir，且每次导航同步写配置文件
  - 位置: src/app/ui/library_panel.py:540-545
  - 证据:
    ```python
    def set_root_path(self, path):
        if os.path.exists(path):
            self.list_view.setRootIndex(self.proxy_model.mapFromSource(self.model.index(path)))
            self.path_edit.setText(path); self.config.set("last_library_path", path); self.config.save_config()
            self.directory_changed.emit(path)
    ```
  - 影响: (1) 该方法被 MainWindow 直连 `files_panel.directory_changed`（main_window.py:1509），若上游传文件路径，`os.path.exists` 通过、`setRootIndex` 落在文件索引上显示空视图，并向 FilesPanel 发出错误同步；(2) 每次目录导航（含双击进入子目录）都同步 `save_config()` 写盘，快速浏览目录树时产生不必要的 IO 抖动。同类入口 `on_path_entered`/`go_up` 均做了 `os.path.isdir` 校验，唯独公共入口没有。
  - 建议: `os.path.exists` 改为 `os.path.isdir`；`save_config` 延迟节流（如 QTimer 1s 单发）或仅在面板关闭/应用退出时保存。
  - 置信度: 高

- [AUD-3811] [Suggestion] 排序下拉、上下文菜单与对话框字符串硬编码英文，绕过 I18N
  - 位置: src/app/ui/library_panel.py:415、575-579、595、600、605
  - 证据:
    ```python
    self.sort_combo.addItems(["Name", "Size", "Date", "Rating"])
    ...
    rm = menu.addMenu("Set Rating")
    ... rm.addAction(f"{i} Stars" if i > 0 else "Clear")
    menu.addAction("Rename").triggered.connect(...)
    ```
  - 影响: 同文件 `zoom_lbl` 用了 `I18N.tr("library.size", "大小")`，files_panel 也走 I18N，本面板其余用户可见字符串未本地化，中英文界面混杂，违反项目国际化约束。
  - 建议: 统一走 `I18N.tr(...)` 并补充语言键。
  - 置信度: 高

- [AUD-3812] [Suggestion] 模块尾部残留 `__main__` 直跑测试代码；大量方法缺类型注解
  - 位置: src/app/ui/library_panel.py:611-612；51-609（如 `paint(self, painter, option, index)`、`set_view_mode(self, mode)`、`on_sort_changed(self, text)`、`show_context_menu(self, pos)`、`set_root_path(self, path)`）
  - 证据:
    ```python
    if __name__ == "__main__":
        app = QApplication(sys.argv); panel = LibraryPanel(); panel.show(); sys.exit(app.exec())
    ```
  - 影响: 测试入口无 db/config 环境保护，import 之外的副作用路径留在生产模块；`mode: str`、`text: str`、`path: str` 等契约未标注，违反 .clinerules 强类型要求。
  - 建议: 将 smoke-test 移到 tests/ 或 scripts/；为公开方法与 Qt 回调补注解。
  - 置信度: 高

### 通过项
- 线程安全：delegate、Database、ThumbnailProvider 的使用全部在 GUI 线程；`thumbnail_ready` 经 `_ThumbnailSignalBridge` 队列连接回主线程后才转 QPixmap（thumbnail_provider.py:114-129），delegate 的 `on_thumbnail_ready` 只在主线程执行，无跨线程 GUI 调用。
- 信号防递归：评分双向同步接线（main_window.py:1515/1518）两端 handler（refresh_file_rating）均不回发信号，无回环；`set_root_path` 中 `path_edit.setText` 不触发 `returnPressed`；目录双向同步经 JS `skipBridge=true` 断链（见 files_panel 通过项）。
- GPU 资源生命周期 / GLSL·HDR：无 OpenGL / 着色器 / HDR 数据路径，无违例。
- 输入验证（JS 注入面）：本面板不执行拼 JS；`create_folder`/`rename_item` 的 OSError 经 QMessageBox 呈现给用户而非静默吞掉。
- 拖拽实现：`DraggableListView.startDrag` 经 `mapToSource` 正确穿透 proxy model 取路径，仅拖拽常规文件。
- 模型使用：QFileSystemModel 懒加载 + Batched 布局（batchSize 100）+ `setUniformItemSizes(True)`，基础性能设置合理。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 1 | AUD-3800 |
| Major | 5 | AUD-3801, AUD-3802, AUD-3803, AUD-3804, AUD-3805 |
| Suggestion | 7 | AUD-3806 – AUD-3812 |
