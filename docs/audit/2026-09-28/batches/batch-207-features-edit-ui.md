# 批次报告：batch-207 [P1] src/features/edit/ui//

编号区间：AUD-20700–AUD-20799。审查范围：场景切割检测的图形/预览两个纯 QPainter UI 控件文件。

---

## src/features/edit/ui/scene_cut_graph_widget.py (行数: 1120)

### 发现

- [AUD-20700] [Major] MiniScrubber.set_total_frames 无状态锁，范围缩小时 Qt 钳制 value 泄漏 frame_changed，引发意外跳帧
  - 位置: src/features/edit/ui/scene_cut_graph_widget.py:743-751（触发点另见 887-888、935-939）
  - 证据:
    ```python
    def set_total_frames(self, total_frames: int) -> None:
        ...
        self._total_frames = max(100, total_frames)
        self.setMaximum(self._total_frames)

    def _on_value_changed(self, value: int) -> None:
        if not self._updating:
            self.frame_changed.emit(value)
    ```
  - 影响: QSlider.setMaximum 在新最大值小于当前值时会钳制 value 并发出 valueChanged；此时 `_updating=False`，链条 `_on_value_changed → _on_scrubber_frame_changed(968-974) → frame_clicked` 直达对话框 `_seek_to_frame`（scene_cut_dialog.py:1615）。`SceneCutGraphWidget.clear()`（938，经 dialog.clear() 调用）把 max 归位 1000 时，任何播放位置 >1000 帧的长片都会在"清空"过程中被 seek 到第 1000 帧；换载更短视频经 set_candidates(888) 同理。
  - 建议: set_total_frames 内以 `_updating` 锁包住 setMaximum（必要时随后校正 value），或 blockSignals 后手动同步。
  - 置信度: 高

- [AUD-20701] [Suggestion] total_frames 强制下限 100，短于 100 帧的媒体时间轴失真
  - 位置: src/features/edit/ui/scene_cut_graph_widget.py:320-321、750
  - 证据:
    ```python
    self._candidates = candidates
    self._total_frames = max(100, total_frames)
    ```
  - 影响: 少于 100 帧（24fps 下约 4 秒）的素材，图形 x 轴与 scrubber 范围被拉伸到 100 帧：末帧标签显示 100、播放头位置被压缩失真；点击可映射到媒体外的帧号（下游 `_seek_to_frame` 会再钳制，风险有限）。
  - 建议: 下限改为 1，仅在 total_frames<=0 时回退默认值。
  - 置信度: 高

- [AUD-20702] [Suggestion] `_updating` 状态锁未用 try/finally，异常路径会使锁永久悬挂
  - 位置: src/features/edit/ui/scene_cut_graph_widget.py:760-765、896-918、957-974
  - 证据:
    ```python
    self._updating = True
    self.setValue(frame)
    self._updating = False
    ```
  - 影响: 更新途中抛异常（如 Qt C++ 对象已销毁时 setValue 抛 RuntimeError）会让 `_updating` 永久为 True，此后所有程序化 set_frame/set_threshold/set_current_frame 被静默丢弃，控件"冻结"且无任何报错。
  - 建议: try/finally 或 contextmanager 封装锁的置位/复位。
  - 置信度: 中（需要异常路径才触发）

- [AUD-20703] [Suggestion] 阈值入口未 clamp，各层 get_threshold 可能返回越界值
  - 位置: src/features/edit/ui/scene_cut_graph_widget.py:897-903、1089-1097（对照 332）
  - 证据:
    ```python
    self._updating = True
    self._threshold = threshold          # 原值存入，无 clamp
    self._graph_area.set_threshold(threshold)   # GraphArea 内部 332 行才 clamp
    ```
    ThresholdSlider.set_threshold 同样存原值 `self._threshold = threshold`，而滑杆范围 35-75（setValue 静默钳制）。
  - 影响: 外部传入文档值域之外（0..1 / 0.35-0.75）的阈值时，get_threshold() 与实际显示的阈值线/滑杆位置不一致，层级间状态分歧。
  - 建议: 在 widget 入口统一 clamp 到文档值域后再存储与下发。
  - 置信度: 高

- [AUD-20704] [Suggestion] 死代码：sensitivity_changed 信号从不发射；9 个导入未使用
  - 位置: src/features/edit/ui/scene_cut_graph_widget.py:818；40-76
  - 证据:
    ```python
    sensitivity_changed = pyqtSignal(float)  # threshold value
    ```
    （注释称保留兼容，全文件无 emit 点。）AST 检出未使用导入：Tuple(40)、Enum(41)、QObject/QSize/QPoint/QTimer(52 起)、QLinearGradient(62 起)、SceneCutKind/SceneCutDecision(73-77)。
  - 影响: 公开信号可被订阅但永无来源，误导调用方；未使用导入为维护噪音。
  - 建议: 移除未用导入；确认无外部订阅后删除信号或标注 deprecated。
  - 置信度: 高

- [AUD-20705] [Suggestion] 父控件直接读取子控件私有成员 _current_frame
  - 位置: src/features/edit/ui/scene_cut_graph_widget.py:929-933
  - 证据:
    ```python
    self._graph_area.set_current_candidate_index(index)

    # Update scrubber to match
    self._graph_area.set_current_frame(
        self._graph_area._current_frame
    )
    self._scrubber.set_frame(self._graph_area._current_frame)
    ```
  - 影响: 破坏封装；且 set_current_candidate_index 已把 `_current_frame` 设为目标值，随后的 set_current_frame 调用是冗余重绘。
  - 建议: 在 SceneCutGraphArea 提供 current_frame() 公共访问器并删除冗余调用。
  - 置信度: 高

- [AUD-20706] [Suggestion] Qt 事件参数用 Any 注解而非具体事件类型
  - 位置: src/features/edit/ui/scene_cut_graph_widget.py:427、434、1111
  - 证据:
    ```python
    def resizeEvent(self, event: Any) -> None:
    def paintEvent(self, event: Any) -> None:
    def set_i18n_resolver(self, resolver: Any) -> None:
    ```
  - 影响: 与同文件 mousePressEvent 正确使用 QMouseEvent 的做法不一致，弱化类型检查收益。
  - 建议: 改用 QResizeEvent/QPaintEvent；resolver 用 Protocol 描述。
  - 置信度: 高

### 通过项
- 线程安全: 全部逻辑位于 UI 线程（QWidget 事件 + QPainter），无跨线程共享状态，未触碰 OpenGL（符合 .clinerules 铁律）
- GPU 资源生命周期: 不涉及 ModernGL/纹理/缓冲；QPainter 在 paintEvent 内局部创建并显式 end()（442-447 对 scale 为 None 的早退也已 end）
- GLSL·HDR 正确性: 不适用——纯 2D QPainter 绘制，无着色器、无 HDR 数据变换/clamp 决策
- 信号防递归: MiniScrubber.set_frame / ThresholdSlider.set_threshold 的程序化 setValue 前置 `_updating`、处理器内校验，且 setValue 相同值不重发，常规路径无回环（范围变更遗漏已单列 AUD-20700）
- 状态提交一致性: 控件不持有 undo/revision/持久化状态，阈值与帧变更一律经信号上抛由对话框裁决
- 异常与取消路径: 无外部 IO 与资源获取；无宽泛 except 掩错
- 输入验证: GraphArea.set_threshold(332)、set_current_frame(368)、mousePress 帧换算(621-622)均有 clamp
- 性能: paint 为 O(候选数) 单遍遍历，悬停/点击为线性命中测试，常规候选量级下可接受；无高频定时器

---

## src/features/edit/ui/scene_cut_preview_widget.py (行数: 826)

### 发现

- [AUD-20710] [Major] set_candidate 时间码路径硬编码 24fps，非 24fps 媒体 TC 与 before/after 偏移错误
  - 位置: src/features/edit/ui/scene_cut_preview_widget.py:565、570、576；715-717；691
  - 证据:
    ```python
    self._before_panel.set_timecode(self._format_timecode(candidate.seconds - self._get_frame_duration()))
    ...
    def _get_frame_duration(self) -> float:
        """Get frame duration in seconds (default 24fps)."""
        return 1.0 / 24.0
    ```
    `_format_timecode(self, seconds: float, fps: float = 24.0)`（691）。
  - 影响: 对话框持有真实 `_playback_fps`（scene_cut_dialog.py:2136 取 frame_provider.fps）且 set_frame_preview 已带 fps 参数，但候选路径完全忽略实际帧率：25/30/60fps 媒体的 before/after 偏移被当作 ±1/24s（60fps 下偏差 2.5 帧），时间码帧位数字按 24fps 换算，与面板显示的 Frame: N-1/N+1 不一致。
  - 建议: 为 set_candidate 增加 fps 参数（或注入 fps provider），与 set_frame_preview 统一换算。
  - 置信度: 高

- [AUD-20711] [Major] _format_timecode 混用 float fps 与 int(fps) 截断，分数帧率时间码出现分钟级偏差
  - 位置: src/features/edit/ui/scene_cut_preview_widget.py:705-711
  - 证据:
    ```python
    total_frames = int(seconds * fps + 0.5)
    hours = total_frames // (3600 * int(fps))
    remaining = total_frames % (3600 * int(fps))
    minutes = remaining // (60 * int(fps))
    ```
  - 影响: fps=29.97 时 total_frames 按 29.97 计、除数按 29 计——60 秒素材被显示为 00:01:02:00（正确应为 00:01:00:00）；NTSC 分数帧率素材随时长漂移放大。
  - 建议: 统一使用同一（四舍五入后的整数或全程 float）帧率做换算，避免混用。
  - 置信度: 中（代码行为确定；实际触发依赖分数帧率媒体进入该路径）

- [AUD-20712] [Major] warning/accepted/rejected/normal 四个样式方法内容完全相同，状态高亮功能为空操作
  - 位置: src/features/edit/ui/scene_cut_preview_widget.py:719-753（分派点 579-587）
  - 证据:
    ```python
    def _set_warning_highlight(self) -> None:
        """Set warning highlight style."""
        self._current_panel.setStyleSheet(f"""
            SinglePreviewPanel {{
                background-color: {COLORS["bg_panel"]};
                border: none;
            }}
        """)
    ```
    四个方法（719-753）样式串逐字节一致；COLORS 中已定义的 "warning"/"accepted"/"rejected" 颜色全文未被使用。
  - 影响: set_candidate 按候选状态分派高亮，但任何状态都产生相同外观——告警/接受/拒绝状态在预览面板中不可见，承诺的功能缺失。
  - 建议: 分别应用边框/背景色（直接使用已定义的 COLORS 键），或删除这套无效分派。
  - 置信度: 高

- [AUD-20713] [Major] frame_clicked 信号声明并文档化"面板点击跳转"，但全文无任何鼠标事件处理，信号永不发射
  - 位置: src/features/edit/ui/scene_cut_preview_widget.py:458（文档 433-435、449-450）
  - 证据:
    ```python
    frame_clicked = pyqtSignal(object, int)  # PreviewPanelType, frame_number
    ```
    SinglePreviewPanel、PreviewImageWidget、SceneCutPreviewWidget 三个类均无 mousePressEvent/mouseReleaseEvent 实现。
  - 影响: 类文档与 Usage 示例承诺的"点击面板跳帧"交互不存在，任何订阅者永远收不到信号——未实现的公开 API。
  - 建议: 在面板实现 mousePressEvent 发射 (panel_type, frame_number)，或删除信号及文档承诺。
  - 置信度: 高

- [AUD-20714] [Suggestion] ThumbnailLoader.request_thumbnail 在调用线程同步执行 loader_func，与"异步加载"文档相悖；_pending_requests 无完成清理
  - 位置: src/features/edit/ui/scene_cut_preview_widget.py:788-822
  - 证据:
    ```python
    key = (panel_type, frame)
    self._pending_requests[key] = True

    if loader_func is not None:
        # Direct load (synchronous)
        image = loader_func(frame)
        self.thumbnail_loaded.emit(panel_type, frame, image)
    ```
  - 影响: 若传入帧解码型 loader（模块 docstring 即设想"generated on demand"），会在 UI 线程同步阻塞；加载成功后不从 `_pending_requests` 删除条目（仅 cancel_request/clear_pending 清理），字典随使用增长且"pending"语义失真。当前该类仅被 `__init__.py` 再导出，无在用调用方，属潜在风险。
  - 建议: 完成时删除 key；重命名以明确同步语义，或接入后台线程/队列后再宣称异步。
  - 置信度: 高（代码事实）；影响为潜在

- [AUD-20715] [Suggestion] set_frame_preview 不更新 _current_candidate 与高亮状态，遗留陈旧状态
  - 位置: src/features/edit/ui/scene_cut_preview_widget.py:618-655（对照 666-668）
  - 证据: set_frame_preview 全程仅写三个面板内容，未触碰 `self._current_candidate`，也未调用 `_set_normal_style()`。
  - 影响: 自由寻帧（画面与候选无关）后 `get_current_candidate()` 仍返回旧候选，误导调用方；上一候选遗留的（未来才会生效的）高亮样式持续残留。
  - 建议: set_frame_preview 中置 `_current_candidate = None` 并恢复 normal 样式。
  - 置信度: 高

- [AUD-20716] [Suggestion] SinglePreviewPanel.set_score 只存储不显示
  - 位置: src/features/edit/ui/scene_cut_preview_widget.py:286-293（调用点 572）
  - 证据:
    ```python
    def set_score(self, score: Optional[float]) -> None:
        ...
        self._score = score
    ```
    之后无任何 UI 更新或读取。
  - 影响: 死状态字段；候选置信度未呈现给用户，docstring 承诺的 score 展示缺失。
  - 建议: 在 info bar 中显示 score，或删除该方法。
  - 置信度: 高

- [AUD-20717] [Suggestion] set_candidate/set_frame_preview/clear 的 _updating 锁无 try/finally
  - 位置: src/features/edit/ui/scene_cut_preview_widget.py:554-589、631-655、659-664
  - 证据:
    ```python
    self._updating = True
    ...
    self._updating = False
    ```
  - 影响: 中途异常（如 Qt 对象销毁后的 RuntimeError）会使 `_updating` 永久为 True，之后所有 set_candidate/set_frame_preview/clear 被静默吞掉，预览永久停止更新且无报错。
  - 建议: try/finally 包裹锁的置位/复位。
  - 置信度: 中

- [AUD-20718] [Suggestion] 未使用导入（QSize/QTimer/QBrush/SceneCutKind）与 paintEvent 的 Any 注解
  - 位置: src/features/edit/ui/scene_cut_preview_widget.py:47-68、358
  - 证据: AST 检出未使用导入：QSize(47 起)、QTimer(47 起)、QBrush(55 起)、SceneCutKind(65-68)；另 `def paintEvent(self, event: Any) -> None:`（358）。
  - 影响: 维护噪音；事件类型注解弱化类型检查。
  - 建议: 清理未用导入，paintEvent 改用 QPaintEvent。
  - 置信度: 高

### 通过项
- 线程安全: 纯 UI 线程逻辑，无跨线程共享状态；QImage→QPixmap 转换均在 UI 线程完成；无 OpenGL 调用
- GPU 资源生命周期: 不涉及 GPU 资源；QPixmap 交由 Qt 引用计数管理，clear() 置 None 释放引用
- GLSL·HDR 正确性: 不适用——QPainter 显示路径，无 HDR 数据处理/clamp 决策
- 信号防递归: 三个写入口全程持 `_updating` 锁，面板自身无对外信号发射路径（锁健壮性见 AUD-20717）
- 状态提交一致性: 只读展示组件，不产生 undo/redo/revision/持久化状态（陈旧 candidate 见 AUD-20715）
- 异常与取消路径: 无 IO/资源获取；ThumbnailLoader 提供 cancel/clear 钩子（不完善处见 AUD-20714）
- 性能: aspect-fit 绘制 O(1)，按需重绘，无轮询定时器；letterboxing/占位符路径无额外开销

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 5 | AUD-20700, AUD-20710, AUD-20711, AUD-20712, AUD-20713 |
| Suggestion | 11 | AUD-20701–20706, AUD-20714–20718 |

两文件均为纯 QPainter 的叶子 UI 控件，无 OpenGL、无跨线程状态，故无 Critical 级问题；主要缺陷集中在时间码帧率假设（AUD-20710/20711）、无效的状态高亮与死信号（AUD-20712/20713）以及 scrubber 范围变更的信号泄漏（AUD-20700）。
