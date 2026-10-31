# batch-123 审查报告 — src/features/deliver/ui

批次：batch-123 [P1]；编号区间 AUD-12300–AUD-12399；审查日期 2026-09-28。
审查文件 1 个。外部依赖已核实：`deliver_workspace.py` 的接线（job_progress→update_job_row、queue_running_changed→set_queue_running、jobs_changed→refresh_from_queue）、`ExportQueue` API（jobs/get_job/is_running/is_paused）、`VideoExportJob` 字段与 `VideoJobStatus`（纯 str 常量类）、`C` 调色板常量均存在且匹配。

## src/features/deliver/ui/video_batch_export_panel.py (行数: 1134)

### 发现

- [AUD-12300] [Major] `_on_context_menu` 调用 `QScrollArea.indexAt()`（不存在的方法），且该槽从未被连接
  - 位置: src/features/deliver/ui/video_batch_export_panel.py:1053
  - 证据:
    ```python
    def _on_context_menu(self, pos) -> None:
        index = self._view.indexAt(pos)
        if not index.isValid():
            return
        job = self._model.get_job_at(index.row())
    ```
    而 `self._view` 是 `QScrollArea`（第 706 行 `self._view = QScrollArea()`）；`indexAt` 是 `QAbstractItemView` 的 API，`QAbstractScrollArea` 分支上不存在。
  - 影响: 表格视图迁移为卡片栈后遗留的断链：右键菜单功能实际不可达（全库 grep 无任何 `customContextMenuRequested` 连接到本面板），若未来有人重新接线将直接 `AttributeError` 崩溃；同文件 `_on_double_clicked`（1094 行）同样处于未连接的孤儿状态。
  - 建议: 迁移到 `self._cards_host` 的 `customContextMenuRequested` + 按 `QWidgetAt(pos)` 反查卡片，或彻底删除 `_on_context_menu`/`_on_double_clicked`（其能力已由卡片按钮与菜单覆盖）。
  - 置信度: 高

- [AUD-12301] [Suggestion] `VideoQueueDelegate` 全类死代码，且内部含不可达分支与不可靠 sizeHint
  - 位置: src/features/deliver/ui/video_batch_export_panel.py:261
  - 证据:
    ```python
    elif job.status in (VideoJobStatus.FAILED, VideoJobStatus.CANCELLED):
        text = "↻ " + I18N.tr("deliver.queue.action_retry", "Retry")
    elif job.is_terminal:
        text = "✕ " + I18N.tr("deliver.queue.action_remove", "Remove")
    ```
    `is_terminal` 定义为 DONE/FAILED/CANCELLED 三态（video_export_job.py:463-464），前两个分支已消费 FAILED/CANCELLED、更上面已消费 DONE，故 `elif job.is_terminal` 永不可达。另 `sizeHint` 返回 `QSize(option.rect.width(), self._ROW_HEIGHT)`（276 行），sizeHint 阶段 `option.rect` 通常无效/为零宽。
  - 影响: 全库无任何 `VideoQueueDelegate` 实例化（视图已是 QScrollArea 卡片栈），约 125 行死代码持续误导维护者以为存在 item-view 渲染路径；不可达分支掩盖"Remove 提示永远不显示"的逻辑错误。
  - 建议: 删除 `VideoQueueDelegate`，或在启用前修复分支顺序与 sizeHint（用固定列宽/样式度量而非 option.rect）。同类死代码可一并清理：`_toggle_dock_floating`/`_dock_back_to_main_window`（1124-1134 行，More 菜单实际走 `detach_requested`/`return_to_deliver_requested` 信号由 workspace 处理）、`_apply_queue_density` 空壳（769-771 行）、`_toolbar_compact` 只写不读（875 行）。
  - 置信度: 高

- [AUD-12302] [Suggestion] 未使用的导入与 logger
  - 位置: src/features/deliver/ui/video_batch_export_panel.py:41
  - 证据:
    ```python
    from PyQt6.QtWidgets import (
        QAbstractItemView,   # 全文件仅此一次出现
        QComboBox,           # 同上
        QHeaderView,         # 同上
        QTreeView,           # 同上
    ...
    from src.features.deliver.core.export_queue import ExportQueue, ExportJob, JobStatus
    ...
    logger = logging.getLogger(__name__)
    ```
    grep 计数：`QAbstractItemView`/`QComboBox`/`QHeaderView`/`QTreeView`/`ExportJob`/`JobStatus`/`logger` 在本文件各恰好出现 1 次（即定义行本身）。
  - 影响: 表格 UI 时代的遗留导入；`ExportJob`/`JobStatus` 未用、模块级 `logger` 从未记录任何日志，徒增启动导入与阅读噪音。
  - 建议: 删除上述未用符号；若保留 logger 则至少在 `_on_context_menu` 等恢复路径中使用或移除。
  - 置信度: 高

- [AUD-12303] [Suggestion] `update_row` 越界回退用旧快照刷新自身，更新实际被丢弃
  - 位置: src/features/deliver/ui/video_batch_export_panel.py:204
  - 证据:
    ```python
    if 0 <= row < len(self._jobs):
        self._jobs[row] = job
        ix = self.index(row, 0)
        ...
    else:
        self.refresh(self._jobs)
    ```
  - 影响: 当 `row` 越界时，回退执行 `refresh(self._jobs)` 只是把旧列表重新装回，新 `job` 永远不会出现在模型里；docstring 承诺"callers don't need to be defensive"，实际回退是无效操作。当前唯一调用方 `update_job_row`（1003-1007 行）先做 job_id 匹配故不触发，属受保护下的潜在缺陷。
  - 建议: 回退时记录日志并要求调用方走 `refresh`，或直接改为断言/忽略，避免制造"已更新"的假象。
  - 置信度: 高

- [AUD-12304] [Suggestion] 每次 `jobs_changed` 全量销毁并重建整个卡片栈，且对同一批控件双份 `deleteLater`
  - 位置: src/features/deliver/ui/video_batch_export_panel.py:957
  - 证据:
    ```python
    for card in self._cards.values():
        card.deleteLater()
    self._cards.clear()

    while self._cards_layout.count() > 0:
        item = self._cards_layout.takeAt(0)
        if item.widget() is not None:
            item.widget().deleteLater()
    ```
    控制器在每次 enqueue/complete/remove 后都 `jobs_changed.emit()`（video_batch_export_controller.py:248、288），workspace 将其接到 `refresh_from_queue` → `_rebuild_cards`。
  - 影响: 批量添加 N 个任务会触发 N 次全量重建（N×N 次卡片构造，每个卡片含 432-464 行的 f-string 样式表解析）；首个循环已对每张卡 `deleteLater`，布局清扫循环对同一控件再次 `deleteLater`（幂等但冗余）。大队列下 UI 出现可感知卡顿与闪烁风险。
  - 建议: 以 `job_id` 为键做差分更新——已存在的卡片走 `_refresh_card`（该函数已存在但仅用于进度路径），只增删变化的卡片；删除双重 deleteLater 之一。
  - 置信度: 高

- [AUD-12305] [Suggestion] 每个进度 tick 与每次 resize 都重设样式表/全量刷新卡片
  - 位置: src/features/deliver/ui/video_batch_export_panel.py:586
  - 证据:
    ```python
    self._status_label.setStyleSheet(
        f"font-size: 10px; color: {status_color}; background: transparent;"
    )
    ...
    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self.update_job(self._job, self._number)
    ```
    `update_job_row`（workspace 第 217 行接线）每个进度信号都调用 `card.update_job`，其中包含上述 `setStyleSheet`；交互式拖拽分隔条时 `resizeEvent` 连发，同样走全量 `update_job`（含样式表重解析）。
  - 影响: `setStyleSheet` 即使值未变也触发样式重新解析与 re-polish；4K 实时导出时进度信号高频到达，叠加 resize 风暴会放大主线程开销。
  - 建议: 仅当 `status_color` 变化时才重设样式表（缓存上次值）；resizeEvent 中只重算 `_elide` 文本，其余字段跳过。
  - 置信度: 高

- [AUD-12306] [Suggestion] 类型注解缺失/不一致：事件参数无注解、builtin generics 与 typing.List 混用、返回类型引用仅局部导入的类
  - 位置: src/features/deliver/ui/video_batch_export_panel.py:400
  - 证据:
    ```python
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override   # 400 行
    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override  # 629、755 行
    def _on_context_menu(self, pos) -> None:                            # 1052 行
    ...
    self._cards: dict[str, _RenderQueueCard] = {}                       # 703 行
    ...
    def _find_enclosing_dock(self) -> Optional[QDockWidget]:            # 1112 行
        from PyQt6.QtWidgets import QDockWidget                         # 1114 行
    ```
  - 影响: 事件/位置参数无类型；703 行用 PEP 585 `dict[...]` 而全文件其余处用 `typing.List/Optional`，风格不一致；`_find_enclosing_dock` 返回注解依赖函数体内的局部导入（因 `from __future__ import annotations` 运行时安全，但对类型检查器不友好）。
  - 建议: 统一 generics 风格；为 `event: QResizeEvent/QPaintEvent`、`pos: QPoint` 补注解；`QDockWidget` 提升到模块级 `TYPE_CHECKING` 导入。
  - 置信度: 高

- [AUD-12307] [Suggestion] 百分比标签绑定 `isVisible()`，祖先未显示时会误判为隐藏
  - 位置: src/features/deliver/ui/video_batch_export_panel.py:592
  - 证据:
    ```python
    self._progress.setVisible(job.status in (VideoJobStatus.RUNNING, VideoJobStatus.DONE) or progress > 0)
    self._percent_label.setText(f"{round(progress * 100)}%" if self._progress.isVisible() else "")
    ```
    而 `refresh_from_queue` 中 `_rebuild_cards(jobs)`（952 行）先于 `_view.setVisible(len(jobs) > 0)`（955 行）执行。
  - 影响: Qt 的 `isVisible()` 在任一祖先隐藏时返回 False（即使刚对该控件 `setVisible(True)`），卡片在滚动区尚未显示时构建，RUNNING/有进度的任务百分比会短暂为空，直到下一次 update 才自愈；逻辑意图（刚设置的可见性）与实际查询不一致。
  - 建议: 把第 591 行的布尔意图存入局部变量/成员，第 592 行直接复用，或改用 `isVisibleTo(self)` / `not isHidden()`。
  - 置信度: 中（行为确凿，但现有调用时序下多为瞬态、可自愈）

### 通过项
- 线程安全：面板为纯被动视图，所有更新经 workspace→controller（主线程 QObject）的 auto/queued 信号送达（deliver_workspace.py:217-219、281），无跨线程直调 UI、无渲染线程耦合。
- GPU 资源生命周期：文件不创建/持有任何 ModernGL 或纹理资源，无泄漏面。
- GLSL·HDR 正确性：纯 UI 面板，无 shader/HDR 数据路径（不适用）。
- 信号防递归：程序化更新仅 setText/setValue/setVisible/setEnabled，不回发面板命令信号；命令信号只由用户动作触发，无反馈回路。
- 状态提交一致性：面板无自有持久化/undo 状态，仅镜像 controller 拥有的队列快照，模型 reset/update 通知成对（beginResetModel/endResetModel、dataChanged）。
- 异常与取消路径：无宽泛 except；`update_job_row` 对 `self._queue is None`、`get_job` 返回 None 均有守卫；取消/重试/移除均以信号上抛由 controller 处置。
- 输入验证：进度在委托（355 行）与卡片（589 行）两处 clamp 到 [0,1]；`ExportQueue.enqueue` 拒绝重复 job_id（export_queue.py:297-298），`_cards` 字典键无碰撞风险；`getattr(job, "project_name"/"export_fps", ...)` 对恢复数据做了防御。
- 调色板/文案：所用 `C.*` 常量（BG_DEEPEST/BG_ACTIVE/ORANGE_PRESS 等）均存在于 style_sheet.C；`VideoJobStatus` 为 str 常量类，`.capitalize()/.replace()` 调用安全。
