# 批次报告：batch-211 [P1] src/features/edit/ui

- 审查日期：2026-09-28
- 任务书：docs/audit/2026-09-28/assignments/batch-211.md
- 编号区间：AUD-21100–AUD-21199
- 审查方式：全文通读 11,028 行（分块 Read，无跳读），并对可疑点做只读 grep/调用方核对（timeline_view.py 等，src/ 全程只读）

## src/features/edit/ui/timeline_items.py (行数: 11028)

### 发现

- [AUD-21100] [Critical] Retime 段速度菜单引用未导入的 `I18N`，点击即抛 NameError（PyQt6 默认 qFatal 可致应用崩溃）
  - 位置: src/features/edit/ui/timeline_items.py:5234（方法 5205–5248，调用点 4469）
  - 证据:
    ```python
    def _show_retime_segment_speed_menu(self, screen_pos: QPointF, x: float) -> None:
        """Open the Resolve-style percentage menu for the clicked segment."""
        from PyQt6.QtWidgets import QMenu
    ...
        menu.addSeparator()
        smooth_action = menu.addAction(
            I18N.tr("edit.clip.smooth_ramp", "Smooth Ramp"))
    ```
    模块级导入（22–76 行）与该方法内均无 `from src.shared.i18n.i18n import I18N`（同文件其它菜单均在函数内导入，如 4589、6367、7998）。
  - 影响: 显示 Retime Controls 后点击百分比标签进入 `_show_retime_segment_speed_menu`，构建菜单时抛未捕获 `NameError`。仓库无全局 `sys.excepthook`，PyQt6 默认对事件处理器内未处理异常调用 `qFatal` 终止进程——最坏情况应用直接崩溃；即使运行时仅打印回溯，Smooth Ramp / 段速度编辑也完全不可用。
  - 建议: 在方法内补充 `from src.shared.i18n.i18n import I18N`（与同文件其它菜单一致），并补充针对该菜单的回归测试。
  - 置信度: 高（代码路径确定；崩溃与"仅功能失效"取决于 PyQt6 运行时默认的 qFatal 行为）

- [AUD-21101] [Major] Audio Gain 菜单项在 `menu.exec()` 之后才 addAction，入口永远不可达
  - 位置: src/features/edit/ui/timeline_items.py:5064（exec 位于 5024）
  - 证据:
    ```python
    # Execute menu
    action = menu.exec(screen_pos)
    ...
    # C3: 音频片段的 Audio Gain 对话框入口
    if self._media_type == "audio":
            audio_gain_action = menu.addAction(
                I18N.tr("edit.clip.audio_gain", "Audio Gain…"))
            audio_gain_action.setEnabled(not self._is_track_locked)
    ```
  - 影响: `menu.exec` 是模态阻塞调用，菜单关闭后才向已销毁的菜单添加动作——"Audio Gain…" 从不出现在右键菜单中，5129 行的 `selected(audio_gain_action)` 永假，C3 片段级增益对话框功能整条入口失效。
  - 建议: 把该 addAction 移到 5024 行 `menu.exec` 之前与其它动作一同添加（放在 `if self._media_type == "audio"` 的波形显示分组附近）。
  - 置信度: 高

- [AUD-21102] [Major] TransitionItem 无轨道锁状态，锁定轨道上的转场仍可修剪/移动/删除
  - 位置: src/features/edit/ui/timeline_items.py:6315（对照 ClipItem:4456；view 侧 handler timeline_view.py:10762 起也无锁守卫）
  - 证据:
    ```python
    def mousePressEvent(self, event: Any) -> None:
        """Handle mouse press for selection and trim/move start (Phase 4.2)."""
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.pos()
            ...
            if pos.x() < HANDLE_SIZE:
                self._drag_mode = "trim_left"
    ```
    ClipItem 同类入口有 `if self._is_track_locked: ... return`（4456–4460），TransitionItem 全类无 `_is_track_locked`/`set_track_locked`；timeline_view.py 的 `_on_transition_trim_left_finished` 等仅检查 `self._updating`，未检查锁轨。
  - 影响: 用户锁定轨道后仍可通过转场卡修剪时长、拖动位置、右键删除，锁定语义被绕过（与 clip 行为不一致，属权限/状态一致性缺陷）。
  - 建议: 为 TransitionItem 增加 `set_track_locked()`，在 mousePressEvent/context menu 中拒绝编辑；或至少在 view 侧 finished-handler 加 `_is_track_locked(track_id)` 守卫。
  - 置信度: 高

- [AUD-21103] [Major] ClipItem.hoverMoveEvent 每次鼠标移动无条件 update()，导致 DeviceCoordinateCache 整卡重建
  - 位置: src/features/edit/ui/timeline_items.py:4410（各分支 4310/4321/4330/4348/4367/4380/4391 同样先 update 再 return）
  - 证据:
    ```python
        if pos.x() < HANDLE_SIZE:
            self._hover_edge = "left"
            ...
        else:
            self._hover_edge = None
            self.setToolTip(f"{self._asset_name}\nFrame: {self._start_frame}")
            self.setCursor(Qt.CursorShape.OpenHandCursor)

        self.update()
        super().hoverMoveEvent(event)
    ```
    构造函数开启 `setCacheMode(DeviceCoordinateCache)`（718–719），任何 `update()` 都使缓存失效并触发含 filmstrip 缩略图/波形/徽章的全量重绘。
  - 影响: 鼠标划过片段时以事件频率（≥60Hz）连续重建设备像素缓存并整卡重绘，还伴随每次 f-string 组 tooltip；4K 多轨时间线上悬停/拖动会明显掉帧，违背项目 4K 实时性能基准。
  - 建议: 记录旧 `_hover_edge`/`_hover_fade_handle`，仅当值实际变化时才 `update()`（可参照 TrackHeaderItem.hoverMoveEvent 7685–7687 的差异检测写法）；tooltip 同样只在变化时设置。
  - 置信度: 高

- [AUD-21104] [Major] 轨道头上下文菜单 target/sync 动作先改本地状态再发信号，违反 P4 提交一致性约定
  - 位置: src/features/edit/ui/timeline_items.py:8210–8217
  - 证据:
    ```python
        elif action == target_action:
            self._is_target = target_action.isChecked()
            self.target_track_changed.emit(self._track_id)
            self.update()
        elif action == sync_action:
            self._is_sync_locked = sync_action.isChecked()
            self.sync_lock_changed.emit(self._track_id, self._is_sync_locked)
            self.update()
    ```
    同类按钮均遵循 `_toggle_mute` 的 P4 注释（7820 起）："Only emits the requested state — does NOT update _is_muted ... preventing UI/model desync on command failure or undo"。
  - 影响: 命令执行失败或被撤销时，header 的 target/sync-lock 视觉状态与模型脱节（本地已先行置位），出现假指示。
  - 建议: 与 mute/solo/lock/visibility 一致——只 emit 请求，由宿主命令成功后回写 `set_target()`/`set_sync_locked()`。
  - 置信度: 高

- [AUD-21105] [Major] DragPreviewItem._draw_thumbnail 对零尺寸 QPixmap 无防护，paint 内除零
  - 位置: src/features/edit/ui/timeline_items.py:9962–9965
  - 证据:
    ```python
    # 计算缩放比例，保持比例
    thumb_scale = min(
        self.THUMBNAIL_WIDTH / pixmap.width(),
        (height - self.ITEM_MARGIN * 2 - self.LABEL_HEIGHT) / pixmap.height()
    )
    ```
    对照 ClipItem._draw_single_thumbnail_at 的防护（2711–2712）：`scale_x = thumb_w / pixmap.width() if pixmap.width() > 0 else 999`。
  - 影响: 拖拽预览的缩略图来自外部媒体池（外部数据），若传入 null/0 尺寸 QPixmap（提取失败常见），paint 阶段抛 `ZeroDivisionError`；paint 内未捕获异常同样可能触发 qFatal/绘制中断。违反"外部数据须验证"铁律。
  - 建议: 除法前检查 `pixmap.width() > 0 and pixmap.height() > 0`，否则跳过缩略图只画标签。
  - 置信度: 中（除零路径确定；是否会出现 null pixmap 取决于上游）

- [AUD-21106] [Suggestion] 多处死代码与从未写入的缓存：`_cache_key`、`_static_cache_key`、`_waveform_start_offset_samples`、`_draw_loading_thumbnails`、`ClipItem._thumbnail_cache`、`TimelineCoordinateManager` 全无有效调用
  - 位置: src/features/edit/ui/timeline_items.py:1360、1374、1428、2738、578、329–384
  - 证据:
    ```python
    _thumbnail_cache: dict[str, QPixmap] = {}          # 578：仅被 clear()/del，从无写入
    ...
    def _waveform_start_offset_samples(self) -> int:   # 1428：无调用（且硬编码 44100，
        sample_rate = 44100                             # 与 TimecodeRulerItem 默认 48000 不一致）
    ...
    class TimelineCoordinateManager:                    # 329：标注 DEPRECATED，
        """DEPRECATED: Use TimelineLayoutEngine ..."""  # get/set_coord_manager 全仓库无外部引用
    ```
    grep 全文件确认：`_cache_key(`、`_static_cache_key(`、`_waveform_start_offset_samples`、`_draw_loading_thumbnails` 无调用点；`_waveform_pixmap` 仅被置 None。
  - 影响: 误导后续维护（如误以为 `_thumbnail_cache` 生效而跳过真实缓存 `ClipThumbnailCache`）；`invalidate_cache` 对死缓存的遍历为无效逻辑。
  - 建议: 删除这些死方法/死缓存与 deprecated 坐标管理器及其全局单例，或接线到真实调用方。
  - 置信度: 高

- [AUD-21107] [Suggestion] 多处宽泛 `except Exception` 吞错，违反 .clinerules 异常铁律
  - 位置: src/features/edit/ui/timeline_items.py:984–985、1044–1045、1290–1291、4664–4668、4679–4683
  - 证据:
    ```python
    try:
        from src.features.edit.core.effect_registry import (
            AUDIO_MIXER_EFFECT_TYPES,
        )
    except Exception:
        return []          # 静默禁用效果后波形，无日志
    ...
    try:
        renderer = ClipEffectWaveformRenderer.instance()
    except Exception:
        return             # 1044
    ```
  - 影响: 真实错误（拼写、注册表初始化失败、场景访问异常）被静默吞掉，效果波形/缓存模式静默降级，排障困难。
  - 建议: 缩窄为预期异常类型（ImportError/RuntimeError 等）并 `logger.warning` 记录降级原因。
  - 置信度: 高

- [AUD-21108] [Suggestion] 未使用/重复导入：`QGraphicsLineItem`、`WaveformCache`、`get_size` 及函数内重复导入 `is_waveform_pending`
  - 位置: src/features/edit/ui/timeline_items.py:35、62、59、3441
  - 证据:
    ```python
    from PyQt6.QtWidgets import (
        QGraphicsItem, QGraphicsObject, QGraphicsRectItem,
        QGraphicsLineItem, ...)           # 35：全文件无使用
    ...
    from src.features.edit.core.audio_waveform import (
        WaveformData, WaveformCache, ...) # 62：仅出现在 3469 行注释中
    ...
    from src.features.edit.core.audio_waveform import is_waveform_pending  # 3441：模块级 65 行已导入
    ```
  - 影响: 命名噪音与 lint 负担；重复导入掩盖依赖关系。
  - 建议: 清理未用导入，删除 3441 行局部重复导入。
  - 置信度: 高

- [AUD-21109] [Suggestion] MarkerItem 事件方法注解引用未导入的 `QMouseEvent`
  - 位置: src/features/edit/ui/timeline_items.py:10636、10661、10680、10699
  - 证据:
    ```python
    def mousePressEvent(self, event: QMouseEvent) -> None:
    ...
    def mouseMoveEvent(self, event: QMouseEvent) -> None:
    ```
    模块 QtGui 导入列表（45–57）不含 `QMouseEvent`；实际传入的是 `QGraphicsSceneMouseEvent`。因 `from __future__ import annotations` 不触发运行时错误。
  - 影响: 类型检查（mypy/IDE）报未定义名，注解语义错误（应为 QGraphicsSceneMouseEvent）。
  - 建议: 从 PyQt6.QtGui 导入 `QGraphicsSceneMouseEvent` 并替换四处注解。
  - 置信度: 高

- [AUD-21110] [Suggestion] 禁用/锁定片段的透明度被提前重置，名称条与波形未呈现半透明
  - 位置: src/features/edit/ui/timeline_items.py:1668–1670（置 0.5 在 1582–1583）
  - 证据:
    ```python
    # Apply transparency for disabled/muted states
    if is_disabled or is_locked:
        painter.setOpacity(0.5)
    ...
    # Draw diagonal stripe pattern for disabled/muted
    if is_disabled or is_locked:
        painter.setOpacity(1.0)  # Reset for pattern
        self._draw_disabled_pattern(painter, clip_rect)
    # 随后继续以 opacity=1.0 绘制波形(1675)与名称条(1691+)
    ```
  - 影响: 禁用/锁定片段只有渐变底与缩略图变暗，波形和底部名称条仍全亮，视觉状态指示不一致（疑似未用 save/restore 包裹纹理绘制）。
  - 建议: 用 `painter.save()/restore()` 包裹 `_draw_disabled_pattern`，保持 0.5 透明度贯穿后续元素。
  - 置信度: 中（基于注释意图推断）

- [AUD-21111] [Suggestion] GapIndicatorItem/TransitionItem 纹理绘制按整宽迭代，未按 exposedRect 裁剪
  - 位置: src/features/edit/ui/timeline_items.py:10257（Gap）、6103（Transition hatch）
  - 证据:
    ```python
    # 绘制斜线（45度，从左上到右下）
    for offset in range(-height, width + height, spacing):
    ...
    hatch_span = int(card_rect.height())
    for i in range(-hatch_span, int(self._width) + hatch_span, 8):
    ```
  - 影响: 大间隙（长空段 × 高缩放，宽度可达数万像素）时每次局部重绘仍执行 O(width/4) 次 Python 循环（Qt 会裁剪绘制但裁不掉 Python 迭代），滚动时 CPU 浪费明显；同类模式在 ClipItem._draw_video_thumbnails 已用 exposedRect 优化。
  - 建议: 用 `option.exposedRect` 收窄循环起止（并考虑预生成 tile 纹理，如 `_draw_disabled_pattern` 的做法）。
  - 置信度: 高

- [AUD-21112] [Suggestion] 音量拖拽提交失败无回滚，header 视觉与模型可能脱节
  - 位置: src/features/edit/ui/timeline_items.py:7936–7948
  - 证据:
    ```python
    def _finish_volume_drag(self) -> None:
        self._is_dragging_volume = False
        self.ungrabMouse()
        final_volume = self._volume
        if final_volume != self._volume_drag_start:
            self.volume_changed.emit(self._track_id, final_volume)
    ```
    拖拽期间 `_update_volume_visual_from_pos` 已直接改 `self._volume`（7932–7934），无失败恢复路径。
  - 影响: 若宿主 SetTrackVolumeCommand 执行失败（或信号未被处理），header 停留在未提交的音量值，直到下一次外部 set_volume 才纠正；与 P4 注释承诺的"命令成功后回写"闭环不完整。
  - 建议: 保留 `_volume_drag_start`，由宿主在命令失败/undo 时调用新增的 `revert_volume()` 恢复。
  - 置信度: 中

- [AUD-21113] [Suggestion] paint 路径内的函数级导入与不稳定 hash 占位波形
  - 位置: src/features/edit/ui/timeline_items.py:1734–1735、3365
  - 证据:
    ```python
    # Use font metrics to elide text that doesn't fit
    from PyQt6.QtGui import QFontMetrics          # 每次 paint 执行模块查找
    ...
    seed_hash = hash(self._clip_id) % 100          # str hash 进程间随机化
    ```
  - 影响: 前者在高频重绘路径增加每次 import 开销；后者使占位波形的伪随机形态每次启动都不同（同项目渲染不一致，纯视觉）。
  - 建议: QFontMetrics 提到模块级导入；占位种子改用稳定哈希（如 `zlib.crc32(clip_id.encode())`）。
  - 置信度: 高

### 通过项
- 线程安全：全文件为 UI 线程 QGraphicsItem/QGraphicsObject，无 OpenGL 调用、无跨线程直取共享状态；异步结果（波形/缩略图/效果波形）一律经 Qt 信号回到 UI 线程。
- GPU 资源生命周期：本文件不持有 ModernGL 资源；QPixmap/QPainterPath 缓存有界（`_waveform_cache_shared` LRU 256，1421–1426）。
- 信号防递归：`dispose()`（883–941）幂等且正确断开三类共享单例信号，防止已移除 item 收到回调；`TrackHeaderItem` 按钮 toggle 均只发信号不改状态（除 AUD-21104 所列两处）。
- 状态提交一致性（拖拽主路径）：mouseRelease 先重置 `_is_dragging` 再发命令信号（5400–5408 注释明确）；rate_stretch/ripple/slide/slip 释放前还原几何与源区间（5467–5587），命令失败不会留下错位预览。
- 异常与取消路径：波形提取失败置 `_waveform_failed` 且有防每帧重试守卫（3435–3444）；效果波形支持播放抢占（DEFERRED）后重排（1076–1079）；缩略图在 provider 暂停期清 pending 等待恢复重绘（2436–2447）。
- 输入验证（主要路径）：fade 钳制 `_clamp_fade_pair`（1161–1166）；音量钳制 0.0–2.0（6863、7871）；`_get_frame_at_pos` 钳制到时长（8646–8655）；`set_cache_segments` 归一化并过滤非法段（8678–8698）；marker 颜色 `QColor.isValid()` 回退（10378–10380）。
- 性能模式（整体）：LOD 分级（is_narrow/medium/wide、缩略图 4 档模式）、`ItemUsesExtendedStyleOption` + exposedRect 波形/标尺局部重绘、拖拽 move 轻量卡片预览（1822–1848）、缩略图密度桶缓存与超宽 clip 关闭设备缓存护栏（1278–1302）。
- 类型注解：除 AUD-21109 所列四处错误注解外，公开方法/属性均有返回类型与参数注解，`from __future__ import annotations` 使用规范。
