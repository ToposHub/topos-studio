# 批次报告：batch-257 [P2] src/features/color/ui//

- 审查日期：2026-09-28
- 编号区间：AUD-25700–AUD-25799（实际使用 AUD-25700–AUD-25711）
- 文件数：3（共 2,514 行），全部通读
- 统计：Critical 0 / Major 4 / Suggestion 8

跨文件验证说明：
- `src/shared/media/thumbnail_provider.py` 确认公共信号（`thumbnail_ready` / `frame_thumbnail_ready`）在主线程完成 QImage→QPixmap 转换后才发射，跨线程经 Qt 自动排队连接落地，strip 的信号路由无线程安全问题。
- `src/features/color/ui/tools_panel.py` 头部标注 SUPERSEDED（2026-08-19），经全库 grep 验证 src/ 与 tests/ 中确无引用。
- `TimelineClipStrip` 唯一构造点为 `src/app/main_window.py:1295`（无参构造）。

---

## src/features/color/ui/timeline_clip_strip.py (行数: 1802)

### 发现

- [AUD-25700] [Major] 播放期缩略图抑制机制从未接线：`set_playback_active` 全库无调用方
  - 位置: src/features/color/ui/timeline_clip_strip.py:1380
  - 证据:
    ```python
    def set_playback_active(self, active: bool) -> None:
        """Notify the strip whether playback is currently active.

        When active, thumbnail requests are suppressed to avoid competing
        with real-time video decode.
        """
        self._playback_active = active
    ```
    （`self._playback_active: bool = False` 初始化于 :957；全库 grep 确认无任何调用点——Edit 页的 `timeline_thumbnail_scheduler`、`edit_gpu_decode_manager`、`timeline_preview_renderer` 均各自接线，唯独 Color 页 clip strip 未接。）
  - 影响: Color 页播放期间用户滚动 clip strip 时，`_request_visible_thumbnails` 仍会发起视频帧缩略图解码请求，与实时播放解码竞争，在 4K 实时基准下可能引起播放卡顿；专为该场景设计的防护分支（:1325-1338）实际是死代码。
  - 建议: 仿照 Edit 页 timeline_thumbnail_scheduler 的接线方式，在 Color 页播放开始/结束时调用 `strip.set_playback_active(True/False)`。
  - 置信度: 高（无调用方为验证事实；卡顿影响为推断）

- [AUD-25701] [Major] 卡片时间码硬编码 24 fps，无法适配项目帧率
  - 位置: src/features/color/ui/timeline_clip_strip.py:954
  - 证据:
    ```python
    self._fps = 24.0
    ```
    ```python
    def _format_timecode(frame: int, fps: float = 24.0) -> str:
        ...
        total_seconds = frame / fps
    ```
    （`self._fps` 在 :1721 传给每张卡片用于 :702 的时间码叠层；本文件无 setter，main_window.py:1295 为无参构造。）
  - 影响: 25/30/50/60 fps 项目的时间码叠层显示错误（例如 30 fps 下第 1800 帧显示 `00:01:15:00`，应为 `00:01:00:00`），误导镜头定位。
  - 建议: 增加公共 `set_fps()` 接口，并在项目/时间线加载时由控制器传入实际帧率。
  - 置信度: 高

- [AUD-25702] [Major] `_fit_cards_to_viewport` 在 viewport 高度 ≤ 0 时以 0 ms 定时器无限重试，绕过重试计数
  - 位置: src/features/color/ui/timeline_clip_strip.py:1148
  - 证据:
    ```python
    viewport_height = self._scroll_area.viewport().height()
    if viewport_height <= 0:
        self._geometry_update_timer.start(0)
        return
    ```
    （对比 :1177-1179 的 follow-up 路径会检查并递减 `self._geometry_followup_passes`，本路径完全不检查该计数。）
  - 影响: 若 strip 所在 dock 处于隐藏/零高度状态且存在卡片（edit_controller.py:1849 的空闲刷新可在 Color 页未打开时触发 `_rebuild_cards`），0 ms 单发定时器每次触发后自行续期，事件循环持续空转，白耗 CPU。
  - 建议: 为该重试路径增加次数上限或退避间隔（复用 `_geometry_followup_passes` 计数即可）。
  - 置信度: 中（代码路径确定存在；持续触发依赖隐藏 dock 的几何状态，未运行验证）

- [AUD-25703] [Suggestion] `_prime_card_from_thumbnail_cache` 使用宽泛 `except Exception` 静默吞错
  - 位置: src/features/color/ui/timeline_clip_strip.py:1774
  - 证据:
    ```python
        try:
            provider = ThumbnailProvider.instance()
            ...
                cached = provider.get_cached_frame_pixmap_any(
                    entry.asset_path, frame_num,
                    target_height=144, asset_id=entry.asset_id,
                )
        except Exception:
            return
    ```
  - 影响: 违反 .clinerules「不得宽泛 except 掩盖领域错误」；provider API 演化产生的 AttributeError 等编程错误被静默吞掉，表现为缩略图无提示缺失，难以排查。
  - 建议: 缩小捕获范围（AttributeError/KeyError 等）或至少 `logger.debug` 记录异常。
  - 置信度: 高

- [AUD-25704] [Suggestion] 多处 UI 文案硬编码英文，未走 I18N
  - 位置: src/features/color/ui/timeline_clip_strip.py:1066
  - 证据:
    ```python
    self._empty_label = QLabel("No timeline clips")
    ```
    （同类硬编码还有 :1211 "Preview Frame"、:1221 "Enable Previews (Large Timeline)"、:1230 "Clip Name"、:1235 "Video Codec"、:1266 "Copy Grade"、:1285 "Float Panel"、:1286 "Close Panel"、:721 tooltip "Color graded"；而同文件 :1243-1247 的镜头过滤菜单已使用 `I18N.tr`。）
  - 影响: 同文件国际化不一致，非英文 locale 下这些菜单/标签无法随语言切换。
  - 建议: 统一改用 `I18N.tr(key, fallback)`。
  - 置信度: 高

- [AUD-25705] [Suggestion] 公共 API 与事件处理器参数缺类型注解
  - 位置: src/features/color/ui/timeline_clip_strip.py:445
  - 证据:
    ```python
    def build_clip_entries(
        project,
        timeline,
        timeline_id: str,
    ) -> list[ColorClipEntry]:
    ```
    （同类：:1498 `set_project_and_timeline(self, project, timeline_sequence)`、:884 `mousePressEvent(self, event)`、:559 `paintEvent(self, event)`、:884 事件参数均未注解。）
  - 影响: 与项目强类型规范不一致，IDE/类型检查无法校验调用方。
  - 建议: 至少为公共 API 补齐参数注解（内部模型对象可用对应类型或 `object`）。
  - 置信度: 高

- [AUD-25706] [Suggestion] `_has_meaningful_value` 的 bool/None 分支不可达（bool 是 int 子类）
  - 位置: src/features/color/ui/timeline_clip_strip.py:314
  - 证据:
    ```python
    if isinstance(value, (int, float)):
        return abs(float(value)) > 1e-6
    if value is None or isinstance(value, bool):
        return bool(value)
    ```
  - 影响: `isinstance(True, int)` 为 True，bool 值永远在前一分支返回（结果恰好一致），后一分支的 bool 部分为死代码，仅 `None` 走得到；易让后续维护者误以为 bool 有独立语义。
  - 建议: 将 bool 判断移到 int/float 之前，或删除冗余分支并加注释。
  - 置信度: 高

- [AUD-25707] [Suggestion] 未使用的导入 `QPen`
  - 位置: src/features/color/ui/timeline_clip_strip.py:21
  - 证据:
    ```python
    from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap, QFont, QResizeEvent
    ```
    （全文件仅此一处出现 `QPen`。）
  - 影响: 冗余导入，轻微增加启动开销并降低可读性。
  - 建议: 从导入列表移除 `QPen`。
  - 置信度: 高

### 通过项
- 线程安全：ThumbnailProvider 公共信号在主线程完成 QPixmap 转换后发射，strip 槽仅在 UI 线程执行，无跨线程 UI 操作
- GPU 资源生命周期：本文件不涉及任何 OpenGL/ModernGL 资源
- 信号防递归：上下文菜单 → 设置/重建链路无反馈回路；`triggered` lambda 均以默认参数正确捕获（:1216、:1253）
- 缩略图信号去重：generation token + pending maps 在 `_rebuild_cards` 时统一递增/清空（:1690-1692），被 `deleteLater` 的卡片无悬挂引用；`destroyed` 时断开 provider 连接防泄漏（:1463-1475）
- `_on_strip_frame_thumbnail_ready` 容差回退路径先收集后变更，无迭代中修改字典问题
- 大时间线降级（≥500 强制 NO_PREVIEW、≥200 阈值分支）与用户显式开启回退逻辑正确（:1700-1710）
- 调色预览缓存有 16 项 FIFO 上限（:1560-1561）；QSettings 读取均有 ValueError 回退默认（:147-150、:161-164）
- `_metadata_int` 仅捕获 (TypeError, ValueError)，外部元数据访问防御良好；`_effective_clip_source_range` 对子片段元数据缺省有兜底
- 状态提交一致性：本文件只读项目模型（color_stacks/grade 判断均为只读探测），无 undo/revision 写路径

---

## src/features/color/ui/tools_panel.py (行数: 239)

### 发现

- [AUD-25708] [Major] `lut_panel` 创建代码被注释，但 `init_ui` 仍连接其信号——类一旦实例化立即 AttributeError
  - 位置: src/features/color/ui/tools_panel.py:161
  - 证据:
    ```python
    # self.lut_panel = LutPanel()
    # self.mid_tabs.addTab(self.lut_panel, I18N.tr("panel.lut", "3D LUT"))
    ...
    self.lut_panel.lut_selected.connect(self.lut_selected.emit)
    self.lut_panel.interaction_started.connect(self.interaction_started.emit)
    self.lut_panel.interaction_ended.connect(self.interaction_ended.emit)
    ```
    （:228-229 `update_from_layer` 中 `self.lut_panel.set_selected_lut(...)` 同样引用不存在的属性。）
  - 影响: `ToolsPanel()` 构造必然抛 AttributeError，该类当前完全不可用。文件头标注 SUPERSEDED（2026-08-19）且经 grep 验证 src/tests 均无引用，故今日无生产影响；但任何恢复引用的操作（或测试误导入实例化）都会即刻崩溃。另外 `reset_all`（:196-201）也未覆盖 lut/mask 面板。
  - 建议: 既然已归档且无引用，直接删除该文件；若保留作存档，应同步注释掉 :161-165 与 :228-229 的 lut_panel 引用，避免"看似可用实则必崩"的状态。
  - 置信度: 高

### 通过项
- 纯 UI 组合面板（已废弃归档），无 GPU/线程/持久化/undo 逻辑可查
- 其余面板（grading/mixer/warper/effects/curves/mask）的信号透传连接方式本身正确（信号→信号直连，无递归风险）

---

## src/features/color/ui/tracker_graph.py (行数: 473)

### 发现

- [AUD-25709] [Suggestion] 类型注解引用了未导入的 `QPoint`
  - 位置: src/features/color/ui/tracker_graph.py:123
  - 证据:
    ```python
    from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
    ...
    self._hover: Optional[QPoint] = None
    ```
  - 影响: 因文件顶部 `from __future__ import annotations`（:12）注解惰性求值，运行时不崩溃；但任何 `typing.get_type_hints()` 调用或静态类型检查都会 NameError。
  - 建议: 在 QtCore 导入中补上 `QPoint`（`mouseMoveEvent` 里 `event.position().toPoint()` 确实返回 QPoint）。
  - 置信度: 高

- [AUD-25710] [Suggestion] 失败帧断线逻辑只断"入线"未断"出线"，断开意图只实现一半
  - 位置: src/features/color/ui/tracker_graph.py:273
  - 证据:
    ```python
    prev: Optional[QPointF] = None
    frames = {f for f in self._failed_frames}
    for f, v in pts:
        p = QPointF(self.frame_to_x(f), self.value_to_y(v, vmin, vmax))
        if prev is not None and f not in frames:
            painter.drawLine(prev, p)  # 失败帧处断开，不画假连续
        prev = p
    ```
  - 影响: 失败帧 f 处仅跳过 prev→f 线段，但 `prev = p` 仍指向失败帧点，下一段（失败帧→下一好帧）照画——曲线仍从失败帧点"延伸"出去，注释宣称的"不画假连续"只在一侧成立，跟踪失败区间的视觉语义不完整。
  - 建议: 失败帧处将 `prev` 置为 `None`（并在下一好帧重新起笔），实现双侧断开。
  - 置信度: 中（代码事实确定；是否为有意设计无注释佐证）

- [AUD-25711] [Suggestion] `TrackerGraph.set_markers` 与 `__init__` 的 parent 参数缺类型注解
  - 位置: src/features/color/ui/tracker_graph.py:458
  - 证据:
    ```python
    def set_markers(self, keyframes, manual, failed) -> None:
        self.view.set_markers(keyframes, manual, failed)
    ```
    （同类：:415 `def __init__(self, parent=None, show_controls: bool = True)`；对比 `_GraphView.set_markers`（:147）已有完整注解。）
  - 影响: 透传 API 无类型约束，调用方传错类型只能在运行时暴露。
  - 建议: 补齐 `List[int]` 参数与 `Optional[QWidget]` parent 注解，与 `_GraphView` 保持一致。
  - 置信度: 高

### 通过项
- 纯 QPainter 自绘控件，无 GPU 资源、无后台线程、无持久化/undo 交互
- 性能模式：曲线绘制统一降采样至 1500 点（:271）、pen 使用 cosmetic、悬停线性扫描仅限鼠标移动事件，量级可接受
- 信号防递归：复选框 toggled → `set_component_visible` → `update()`，纯单向刷新，无回路
- 边界防护：`value_range` 空数据返回 (0,1)、平区间 ±0.5 展开（:68-73）；`frame_to_x`/`x_to_frame`/`value_to_y` 对 `hi<=lo`、`rect.width()<=0`、`vmax<=vmin` 均有除零防护；滚轮缩放有 `half >= 1.0` 下限（:388）
- `_GraphView` 交互（F 适配 / Home 回参考帧 / 点击发 frame_clicked）与 TrackerPanel 装配（tracker_panel.py:266 `show_controls=False`）职责清晰
