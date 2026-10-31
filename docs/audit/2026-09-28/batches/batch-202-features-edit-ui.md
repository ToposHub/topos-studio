# 批次报告：batch-202 — src/features/edit/ui//

- 审查范围：`src/features/edit/ui/media_pool_panel.py`（10,671 行，单文件批次）
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性 / 异常与取消 / 输入验证 / 性能 / 类型注解
- 编号区间：AUD-20200–AUD-20299
- 统计：Critical 0 · Major 7 · Suggestion 13

---

## src/features/edit/ui/media_pool_panel.py (行数: 10671)

### 发现

#### Major

- [AUD-20200] [Major] Quick Look 的 Space 切换永远找不到已打开的对话框，重复弹窗并泄漏
  - 位置: src/features/edit/ui/media_pool_panel.py:9157
  - 证据:
    ```python
    # If a Quick Look is already open, close it (toggle).
    existing = self.findChild(QuickLookDialog)
    if existing is not None:
        existing.close()
        existing.deleteLater()
        return
    ...
    dlg = QuickLookDialog(asset, self._get_top_level_widget())
    dlg.show()
    ```
  - 影响: 对话框的 QObject 父对象是 `_get_top_level_widget()`（主窗口），而 `self.findChild(QuickLookDialog)` 只搜索面板自身子树（面板嵌在 dock 中，对话框不是面板的后代）。每次按 Space 都新开一个 QuickLookDialog，旧对话框永不关闭（WA_DeleteOnClose 已显式关闭为 False），形成窗口/内存泄漏，且"再按 Space 关闭"的 Finder 语义失效。
  - 建议: 用面板自己的成员变量（`self._quick_look_dialog`）跟踪当前实例，或把对话框父对象改为 `self`；关闭时统一 `close()+deleteLater()` 并清引用。
  - 置信度: 高（父子关系与 findChild 语义均已核对；面板独立作为顶层窗口时才碰巧可用）

- [AUD-20201] [Major] hover-scrub 帧缓存 `_hover_frames` 无界增长（内存泄漏）
  - 位置: src/features/edit/ui/media_pool_panel.py:3412
  - 证据:
    ```python
    def _on_hover_frame_ready(self, path: str, frame_index: int, pixmap: QPixmap) -> None:
        """Cache a frame thumbnail delivered by the provider and repaint."""
        if pixmap is None:
            return
        self._hover_frames.setdefault(path, {})[frame_index] = pixmap
    ...
    def leaveEvent(self, event: Any) -> None:
        self._hover_path = None
        self._hover_frame_index = None
    ```
  - 影响: 鼠标在长视频卡片上往返刮擦时，每个经过的帧索引都会缓存一张全尺寸 QPixmap（icon_size ≥128px）。`_hover_frames`（3241 行声明）从未清理（leaveEvent 只清 `_hover_path`/`_hover_frame_index`），长会话下每个刮擦过的视频累积数 MB～数十 MB，多素材池中持续增长不释放。
  - 建议: 限制每 path 的缓存条数（LRU/只保留当前帧±N），或 `_hover_path` 切换/leaveEvent 时清空旧 path 的帧缓存。
  - 置信度: 高

- [AUD-20202] [Major] 5 秒定时器在 UI 线程对全部素材同步执行 `os.path.exists`（网络盘卡顿）
  - 位置: src/features/edit/ui/media_pool_panel.py:4579
  - 证据:
    ```python
    self._offline_refresh_timer = QTimer(self)
    self._offline_refresh_timer.setInterval(5000)
    self._offline_refresh_timer.timeout.connect(self._refresh_offline_status)
    self._offline_refresh_timer.start()
    ...
    def _refresh_offline_status(self) -> None:
        if self._updating or self._project is None:
            return
        checked_project = update_offline_status(self._project)
    ```
    （`update_offline_status` 实现见 src/features/edit/core/project.py:5065：`is_offline = not os.path.exists(asset.path)`，逐素材同步检查。）
  - 影响: 素材位于网络卷/断开的移动盘时，每个 `os.path.exists` 可阻塞数百 ms～数秒，5 秒一次全量扫描会周期性冻结 UI 线程，违背"实时 4K 交互"基准；素材越多越严重。
  - 建议: 将可用性扫描移到 worker 线程（复用 MediaImportWorker 模式），UI 线程只应用结果；或按 mtime/退避策略降低扫描频率并在检测到慢路径时自动降频。
  - 置信度: 高

- [AUD-20203] [Major] `_update_disk_proxy_services` 直接锁其它服务的私有锁并改写私有缓存
  - 位置: src/features/edit/ui/media_pool_panel.py:10358
  - 证据:
    ```python
    try:
        disk_proxy_service = get_disk_proxy_service()
        with disk_proxy_service._lock:
            disk_proxy_service._info_cache[source_path] = proxy_info
    ...
    try:
        frame_service = get_frame_provider_service()
        with frame_service._lock:
            clip_ids = frame_service._path_to_clips.get(source_path, [])
    ```
  - 影响: UI 面板绕过服务公共 API，直接持有 `DiskProxyService._lock`/`FrameProviderService._lock` 并写入 `_info_cache`、读取 `_path_to_clips`/`_clips`。破坏封装：服务内部结构一旦调整即静默失效（本处已被宽泛 except 吞掉）；且在 UI 线程抢服务锁，服务 worker 长时间持锁时会阻塞界面。
  - 建议: 在两个服务上提供公共方法（如 `disk_proxy_service.mark_ready(...)`、`frame_service.refresh_disk_proxy(source_path, info)`），面板只调用公共接口。
  - 置信度: 高（私有属性已在服务源码中核实存在）

- [AUD-20204] [Major] 批量标记把评级 0（未评级）当作"无操作"丢弃
  - 位置: src/features/edit/ui/media_pool_panel.py:9090
  - 证据:
    ```python
    params = dialog.batch_params()
    if not any(params.values()):
        return
    cmd = BatchUpdateAssetLogCommand.create(ids, ...)
    ```
    其中 `batch_params()` 返回 `rating=self._rating_combo.currentData()`（可为 0），其余默认 `None`/空元组。
  - 影响: 用户在批量标记对话框中仅把评级改为"未评级(0)"时，`any(((), (), 0, None, None, None))` 为 False，直接 return，命令不创建——无法批量清除评级，功能缺陷。
  - 建议: 判空改为显式语义检查（如 `if all(v in (None, (), "") for v in params.values()): return`），rating 用 `is None` 判断"不变"。
  - 置信度: 高

- [AUD-20205] [Major] 代理生成无重复触发防护，pending 状态可并发启动多个 ffmpeg 转码写同一文件
  - 位置: src/features/edit/ui/media_pool_panel.py:10171
  - 证据:
    ```python
    # Update status to pending
    new_project = update_asset_proxy(self._project, asset_id, None, "pending")
    ...
    worker = ProxyGeneratorWorker(asset)
    ...
    QThreadPool.globalInstance().start(worker)
    ```
    菜单入口仅按 `asset.has_proxy` 分支（8077-8086 / 8438-8448）；`has_proxy` 定义（src/features/edit/core/project.py:219）为 `proxy_status == "ready" and proxy_path is not None`。
  - 影响: 生成进行中（status="pending"）时 `has_proxy` 仍为 False，菜单继续显示 "Generate Proxy"，再次点击会启动第二个 `ProxyGeneratorWorker`；两个 ffmpeg 进程并发转码并写同一个代理输出路径，可能产生损坏的代理文件且浪费大量 CPU/磁盘。`_proxy_jobs[asset_id]` 被覆盖后旧任务完成时的陈旧路径校验也随之失真。
  - 建议: 入口处检查 `proxy_status in ("pending", "generating")` 或 `asset_id in self._proxy_jobs` 时拒绝/禁用菜单项；完成回调里也要原子地 pop。
  - 置信度: 高（has_proxy 语义与菜单分支均已核实）

- [AUD-20206] [Major] 拖拽事件中同步递归遍历目录树，拖入大目录时 UI 冻结
  - 位置: src/features/edit/ui/media_pool_panel.py:6446
  - 证据:
    ```python
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            file_paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
            if self._filter_valid_media_files(file_paths):   # 递归 _walk 整个目录
                event.acceptProposedAction()
    ...
    def _walk(path: str) -> None:
        if os.path.isdir(normalized):
            for entry in sorted(os.listdir(normalized)):
                _walk(os.path.join(normalized, entry))
    ```
  - 影响: 从 Finder 拖一个大目录（或网络卷目录）进入面板时，dragEnter/dragMove 路径上同步 `listdir` 递归全树；文件多或网络慢时拖拽过程中界面冻结数百 ms～数秒，drop 后 `_import_files_async` 又会再走一遍同样的同步遍历。
  - 建议: dragEnter 只做浅层嗅探（顶层扩展名/单一目录判断）决定是否接受；完整遍历留给已有的后台 MediaImportWorker 流程。
  - 置信度: 高

#### Suggestion

- [AUD-20207] [Suggestion] 大块死代码：同步导入链与未使用的绘制/树函数（约 300+ 行）
  - 位置: src/features/edit/ui/media_pool_panel.py:6304
  - 证据:
    ```python
    def _do_import_with_progress(self, file_paths: List[str]) -> None:   # 无调用者
        ...
    def _do_import(self, file_paths: List[str]) -> None:                 # 仅被上者调用
    def _import_single_file(self, path: str) -> bool:                    # 仅被上两者调用
    ```
    另有无调用者的 `_add_asset_item`(7551)、`_draw_badges`(2321)、`_draw_overlay`(2538)、`_draw_timeline_overlay`(2634)、`GridDelegate._format_duration`(2700)（grep 全文件确认无引用）。
  - 影响: `_import_single_file` 在 UI 线程同步 `probe_media_metadata`，一旦被误用即退化为卡 UI 的旧路径；死绘制函数与 `_add_asset_item_direct`/`_draw_compact_indicators` 并存造成维护漂移。
  - 建议: 删除死代码或在 docstring 标注保留原因。
  - 置信度: 高（grep 验证无外部调用者）

- [AUD-20208] [Suggestion] 文件夹导入结构镜像逐条 execute 命令，产生 N 个 undo 步骤
  - 位置: src/features/edit/ui/media_pool_panel.py:6694
  - 证据:
    ```python
    cmd = CreateBinCommand.create(part, parent_id)
    self._project = self._command_stack.execute(cmd, self._project)
    new_id = cmd._created_bin_id
    ...
    cmd = MoveAssetToBinCommand.create(asset.asset_id, target_bin_id)
    self._project = self._command_stack.execute(cmd, self._project)
    ```
  - 影响: 一次"导入文件夹"会为每个子文件夹、每个文件各生成一条 undo 记录；用户需按几十次 Ctrl+Z 才能回退一次导入（与批量删除等其它路径"单 macro 单 undo"不一致）。另读取命令对象的私有属性 `cmd._created_bin_id`，脆弱。
  - 建议: 收集全部子命令后用 `ProjectMacroCommand` 一次执行；`_created_bin_id` 提升为公共属性或由 create 返回。
  - 置信度: 高

- [AUD-20209] [Suggestion] `_on_thumbnail_ready` 每张缩略图到达即全树扫描，整体 O(n²)
  - 位置: src/features/edit/ui/media_pool_panel.py:6027
  - 证据:
    ```python
    for i in range(self.tree.topLevelItemCount()):
        top_level_item = self.tree.topLevelItem(i)
        ...
        for asset_item in candidates:
            asset_id = asset_item.data(0, Qt.ItemDataRole.UserRole)
            asset = self._project.assets.get(asset_id)
            if asset is not None and asset.path == path:
                asset_item.setIcon(0, QIcon(icon_pixmap))
    ```
  - 影响: 每个异步缩略图完成都对树做全量遍历 + `QPixmap.scaled(32,32)`；数千素材导入后是 O(n²) 的重复扫描与缩放，列表视图下刷新可感知卡顿。
  - 建议: 维护 path→tree item 的索引（在 `_add_asset_item_direct` 填充），或至少把 32×32 图标缓存按 path 复用（QIcon(pixmap) 每次重建）。
  - 置信度: 高

- [AUD-20210] [Suggestion] `_get_proxy_resolution` 在 UI 线程同步 av.open + ffprobe 子进程（超时 5s）
  - 位置: src/features/edit/ui/media_pool_panel.py:10396
  - 证据:
    ```python
    with av.open(proxy_path) as container:
        ...
    result = subprocess.run([ffprobe_bin, ..., proxy_path], capture_output=True, text=True, timeout=5)
    ```
  - 影响: 代理完成回调（UI 线程）里做媒体探测，正常几十 ms，但慢盘/杀毒软件拦截时可阻塞 UI 最长 5 秒。
  - 建议: 探测移入 worker（可与 ProxyGeneratorWorker 合并，完成时一并回报分辨率）。
  - 置信度: 高

- [AUD-20211] [Suggestion] 多处宽泛 except 吞错（含静默 pass）
  - 位置: src/features/edit/ui/media_pool_panel.py:5900
  - 证据:
    ```python
    except Exception:  # noqa: BLE001
        pass
    ```
    同类：`_restore_sort_prefs`(5925-5926)、`_maybe_auto_ensure_disk_proxy`(10077-10079，仅 debug 级日志)、`_get_proxy_resolution` 两处 `except Exception: pass`(10406, 10432)。
  - 影响: 违背 .clinerules "不得宽泛 except 掩盖领域错误"：QSettings 损坏、代理策略评估异常、ffprobe 失败等被静默吞掉，故障不可诊断（debug 级在生产日志里通常不可见）。
  - 建议: 收窄异常类型（OSError/ValueError 等），至少统一 `logger.warning(..., exc_info=True)`。
  - 置信度: 高

- [AUD-20212] [Suggestion] 列表视图双击未跳过 Generator 素材，与网格视图/Enter 键行为不一致
  - 位置: src/features/edit/ui/media_pool_panel.py:7828
  - 证据:
    ```python
    elif item_type == "asset" and item_id:
        if self._project:
            asset = self._project.get_asset_by_id(item_id)
            if asset and asset.is_comp_clip:
                self.open_comp_clip_in_composite_requested.emit(item_id)
                return
        self.asset_double_clicked.emit(item_id)      # 不检查 is_adjustment / solid
    ```
    对比网格双击（6144-6151）与 Enter 键（2786-2790）均显式跳过 `is_adjustment` / `media_type == "solid"`。
  - 影响: 列表视图双击 Adjustment/Solid 素材会打开 Source Viewer 并交给解码器一个不可解码的虚拟路径，报错或空白预览；三处入口行为不一致。
  - 建议: 在 `_on_tree_double_clicked` 中复用同一 Generator 判断。
  - 置信度: 高

- [AUD-20213] [Suggestion] `_update_timeline_highlight_list` 查找的 "timelines" 顶级节点已不存在，函数永远空转
  - 位置: src/features/edit/ui/media_pool_panel.py:5408
  - 证据:
    ```python
    for i in range(self.tree.topLevelItemCount()):
        item = self.tree.topLevelItem(i)
        if item and item.data(0, Qt.ItemDataRole.UserRole + 1) == "timelines":
    ```
    而 `_delayed_refresh` 中已明确不再渲染 timelines 节点（6913-6917 注释："时间线节点：不再在媒体池主列表显示"）。
  - 影响: 列表视图的"激活时间线高亮"功能实际失效（永远找不到目标节点），且每次切换时间线都白白扫一遍树；误导后续维护者以为高亮生效。
  - 建议: 删除该函数及其调用，或在列表视图恢复渲染 timeline 行时再启用。
  - 置信度: 高

- [AUD-20214] [Suggestion] `_on_replace_media` 使用位置计算重复且控制流混乱
  - 位置: src/features/edit/ui/media_pool_panel.py:9839
  - 证据:
    ```python
    locations = []
    try:
        sequences = self._collect_timeline_sequences()
        index = build_asset_usage_index(sequences, self._project) if sequences else None
        if index is not None:
            locations = list(index.locations_for(asset_id))
    except Exception:
        index = None
    if index is not None:
        locations = index.locations_for(asset_id)   # 与 try 内重复计算
    ```
  - 影响: try 块内的 `locations` 计算是死结果，紧接着又无条件重算一遍；except 还会吞掉非预期错误。逻辑冗余、可读性差。
  - 建议: 精简为一次构建 + 一次读取，异常仅记日志。
  - 置信度: 高

- [AUD-20215] [Suggestion] 批量重链接统计用哨兵 `MediaAsset` 伪对象兜底
  - 位置: src/features/edit/ui/media_pool_panel.py:10003
  - 证据:
    ```python
    online_count = sum(
        1 for aid in relink_map
        if not new_project.assets.get(aid, MediaAsset(
            asset_id="", path="/__invalid__", media_type="video",
            display_name="", metadata={}
        )).is_offline
    )
    ```
  - 影响: 为 `.get` 默认值构造整个伪 MediaAsset；若 MediaAsset 构造签名变化此处会崩，且语义上"不存在的 asset 视为在线"本身存疑。
  - 建议: `(new_project.assets.get(aid) is not None and not ...is_offline)` 显式判断。
  - 置信度: 高

- [AUD-20216] [Suggestion] 箭头图标以固定文件名写入共享系统临时目录
  - 位置: src/features/edit/ui/media_pool_panel.py:534
  - 证据:
    ```python
    _tmp = tempfile.gettempdir()
    collapsed_path = os.path.join(_tmp, "topos_arrow_right.png")
    expanded_path = os.path.join(_tmp, "topos_arrow_down.png")
    ...
    pm_r.save(collapsed_path)
    ```
  - 影响: 多个面板实例/同机多进程共用同一文件名互相覆盖（内容相同故多数时候无害，但属共享可变状态）；写入失败（只读临时目录）时 `save` 返回 False 也不检查，样式将引用不存在的文件。
  - 建议: 用 `QPixmap.cacheKey()`/进程内缓存 + `QIcon`，或文件名加 pid/随机后缀并检查 save 结果。
  - 置信度: 高

- [AUD-20217] [Suggestion] `_pending_thumbnails`/`_pending_subclip_frames` 无失败清理，解码失败后卡片永久停在占位符
  - 位置: src/features/edit/ui/media_pool_panel.py:1456
  - 证据:
    ```python
    self._pending_thumbnails: set = set()
    self._pending_subclip_frames: dict[tuple[str, int], set[str]] = {}
    ...
    if asset.path not in self._pending_thumbnails:
        self._pending_thumbnails.add(asset.path)
        self.thumb_provider.get_thumbnail(asset.path, max(self.thumb_size))
    ```
  - 影响: 成功路径会从 pending 集合移除（1540-1541 / 1527-1529），但解码失败（信号从不发出）时条目永不移除：该路径/子clip 永远不再重试，卡片一直显示 "…"，且集合随失败累积。
  - 建议: 为 pending 请求加时间戳超时重置，或 ThumbnailProvider 增加失败回调以清理。
  - 置信度: 中（依赖 provider 失败时不发信号的行为；成功路径已核实）

- [AUD-20218] [Suggestion] 类型注解缺失或过宽（Any/无注解）
  - 位置: src/features/edit/ui/media_pool_panel.py:802
  - 证据:
    ```python
    def _on_context_menu(self, pos: Any) -> None:          # 802, 应为 QPoint
    def dragLeaveEvent(self, event: Any) -> None           # 913/3016/3646, 应为 QDragLeaveEvent
    def __init__(self, parent=None) -> None                # 4265 ConsolidateOptionsDialog, 缺类型
    def policy_values(self) -> Any:                        # 4249, 应为 ProxyPolicySettings
    def _find_item(parent, target_id: str):                # 10634 reveal_asset 内嵌函数, parent 无注解
    ```
  - 影响: 违背 .clinerules 强类型约束，降低 IDE 检查与可读性。
  - 建议: 补齐具体类型（QPoint/QDragLeaveEvent/Optional[QWidget]/ProxyPolicySettings/QTreeWidgetItem）。
  - 置信度: 高

- [AUD-20219] [Suggestion] 树/网格两套上下文菜单约 300 行复制，且功能项已出现不对称
  - 位置: src/features/edit/ui/media_pool_panel.py:7989
  - 证据:
    ```python
    # _on_tree_context_menu 面板级菜单项：
    collect_act = menu.addAction(I18N.tr("edit.media_pool.collect_project", "收集工程…"))
    consolidate_act = menu.addAction(...)
    proxy_policy_act = menu.addAction(...)
    ```
    而 `_on_grid_context_menu`（8292-8600）的条目菜单（8567-8591）没有 collect/consolidate/proxy_policy，仅空白区菜单有 proxy_policy（8320-8323）；素材条目动作（relink/replace/proxy/rename/…）两处整段复制。
  - 影响: 同一功能因视图模式不同而有无（列表视图右键能"收集工程/整理工程/代理策略"，网格视图右键不能）；复制块后续单侧修改会持续放大漂移。
  - 建议: 抽取公共的"素材条目动作"/"面板级动作"构建函数，两菜单复用并统一项集。
  - 置信度: 高

### 通过项

- GPU 资源生命周期：文件不直接创建/持有 ModernGL 对象（纯 Qt Widgets + QPixmap/QPainter），无 UI 线程 OpenGL 调用、无 GPU 资源泄漏面。
- GLSL·HDR 正确性：不涉及 GLSL 着色器；唯一的数值 clamp（2190-2194 solid 色块 QColor.fromRgbF 前 clamp 0..1）属于 UI 显示路径而非 HDR 内部数据，符合规范。
- 信号防递归：`_updating` 状态锁在 set_project/_delayed_refresh/视图切换/选择处理各处一致使用；`select_bin`、`_restore_pool_item_selection`、sort_combo 同步均正确 blockSignals；进度对话框的 setValue 重入有 `_progress_update_in_flight`/延迟一拍收尾防护。
- 状态提交一致性：删除/移动/重命名/拖入 bin 等均统一走 `ProjectCommandStack` 并以 `ProjectMacroCommand` 合并为单 undo 单元；批量删除时间线前同步抓取 TimelineSequence 快照（timeline_command_snapshot_requested + `_store_timeline_snapshots`/`consume_timeline_snapshot`）保障 undo 后轨道/剪辑可恢复；导入绕过 undo 栈属设计决定（导入不做 undo）。
- 异常与取消路径：导入批次有 batch_id/project_id 陈旧结果双重校验、取消标志（worker.cancel + `_import_cancelled`）、对话框 close→deleteLater 生命周期防护；命令执行 ValueError 均弹窗反馈；代理完成/失败回调对已删除素材与换源素材有陈旧路径校验。
- 输入验证：导入/拖入文件经扩展名白名单（format_registry 四类集合）+ `os.path.isfile` + `os.access(R_OK)` 校验，目录递归有 seen 去重与 OSError 捕获；MIME JSON 解析失败有 text/plain fallback 与异常捕获；外部 metadata 的 int/frame 解析均有 try/except(TypeError, ValueError) 兜底；frame_index 对 total_frames 有 clamp。
- 性能模式（正面项）：刷新防抖 100ms（UI-33）、grid 选择更新 16ms 合帧、视口外项跳过 marquee 相交测试、缩略图异步加载带 pending 去重。
