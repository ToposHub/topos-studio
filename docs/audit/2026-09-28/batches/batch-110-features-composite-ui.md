# batch-110 审查报告 — src/features/composite/ui/（sticky note / timeline commands / expression / icons）

编号区间：AUD-11000–AUD-11099（实际使用 AUD-11001–AUD-11030）
审查日期：2026-09-28
文件数：5，共 2,823 行，全部 Read 通读。

## src/features/composite/ui/sticky_note_item.py (行数: 667)

### 发现

- [AUD-11001] [Major] "Change Color" 拾色对话框在 `show()` 后随局部变量被 GC 销毁，功能完全不可用
  - 位置: src/features/composite/ui/sticky_note_item.py:586-597
  - 证据:
    ```python
    def _choose_color(self):
        """Open color picker dialog with real-time color preview."""
        dlg = QColorDialog(self._color, None)
        dlg.setWindowTitle("Choose Sticky Note Color")
        dlg.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog)
        def _on_live(color: QColor):
            if color.isValid():
                self.setColor(color.name())
        dlg.currentColorChanged.connect(_on_live)
        dlg.show()
    ```
  - 影响: 无父对象、无引用保持的非模态 QColorDialog 在 `_choose_color` 返回后即被 Python 引用计数回收（已用项目 venv + offscreen 平台实测复现：函数返回 300ms 后 `topLevelWidgets()` 中 QColorDialog 数量为 0）。用户右键 "Change Color" 时对话框闪现即消失，功能 100% 不可用。`backdrop_item.py:707-718` 存在完全相同的模式（跨批次，仅交叉备注）。
  - 建议: 将对话框存为实例属性（`self._color_dlg = dlg`）或给定 parent（如 `self.scene().views()[0]`）；关闭后置 None。
  - 置信度: 高

- [AUD-11002] [Major] 拾色器实时预览把每个中间颜色各推一步 undo（拖一次颜色 = N 个 undo 步），且取消不回滚
  - 位置: src/features/composite/ui/sticky_note_item.py:279-285, 211-224, 592-594
  - 证据:
    ```python
    def setColor(self, color: str):
        """Set the background color. Text color is independent and unchanged."""
        self._begin_change()
        self._color = QColor(color)
        self._apply_fill()
        self.update()
        self._notify_changed()
    ...
    def _on_live(color: QColor):
        if color.isValid():
            self.setColor(color.name())
    ```
  - 影响: `_begin_change`/`_notify_changed` 配对在每次 `currentColorChanged` 中各自独立成立（old 快照在每次 `_notify_changed` 后被置 None），用户在色域中拖动时 `currentColorChanged` 连续触发几十次 → `_modified_handler` 每次都向宿主推送一条 `sticky_note_modified` undo 记录（composite_page._on_sticky_note_modified 每条 push 一个 UndoAction），undo 栈被污染；点 Cancel 时已应用的颜色也不回滚。对比：move/resize 与编辑对话框都正确聚合为单步。
  - 建议: 拾色器路径改为"预览不通知 + 对话框 accept 后一次性 `_begin_change`/`_notify_changed`"，或在 `_choose_color` 打开期间设置聚合锁、关闭时统一提交一次。
  - 置信度: 高

- [AUD-11003] [Major] `from_dict` 对外部工程数据硬索引 `id`/`position`/`size`，缺键或值畸形直接抛 KeyError/TypeError 崩溃加载
  - 位置: src/features/composite/ui/sticky_note_item.py:651-664
  - 证据:
    ```python
    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "StickyNoteItem":
        """Create sticky note from dict."""
        return StickyNoteItem(
            note_id=data["id"],
            ...
            position=QPointF(data["position"][0], data["position"][1]),
            size=QRectF(0, 0, data["size"][0], data["size"][1]),
    ```
  - 影响: 该数据来自工程文件持久化（node_editor_widget.py:608 `add_sticky_note_visual` 只校验了 `"id" in data`）。旧版本/手工编辑/损坏的工程缺 `position`/`size` 键或值为 None/字符串时，加载即抛未捕获异常，工程打不开。违反"外部数据须验证"铁律。
  - 建议: `position`/`size` 改为 `.get(...)` + 数值/长度校验 + 默认值兜底（与 `font_size`/`opacity` 的 `.get` 风格一致）。
  - 置信度: 高（崩溃路径确定；触发依赖异常外部数据）

- [AUD-11004] [Suggestion] 构造参数 `position: QPointF = None` / `size: QRectF = None` 为隐式 Optional 注解
  - 位置: src/features/composite/ui/sticky_note_item.py:48-49
  - 证据:
    ```python
        position: QPointF = None,
        size: QRectF = None,
    ```
  - 影响: 默认值为 None 但类型未标 `Optional[QPointF]`，严格类型检查（no-implicit-optional）报错，违反项目强类型约束。
  - 建议: 改为 `Optional[QPointF] = None` / `Optional[QRectF] = None`。
  - 置信度: 高

### 通过项
- 线程安全：纯 UI 线程 QGraphicsItem，无跨线程访问，无 OpenGL 调用（维度不适用）
- GPU 资源生命周期：不涉及
- GLSL·HDR 正确性：不涉及（非渲染元素）
- 信号防递归：`_applying_state` 状态锁在 `apply_state`/编辑对话框提交路径使用正确（try/finally 恢复）；move/resize 的 no-op 丢弃（old==new）正确避免纯点击推 undo
- boundingRect 覆盖了边框+选中光环外扩（5px margin），无脏区残留问题
- 交互状态（_resizing/_move_start_position）press/release 配对完整，Esc 取消由场景处理

## src/features/composite/ui/timeline_commands.py (行数: 951)

### 发现

- [AUD-11010] [Major] `commit_move` 未删除被拖拽关键帧的原帧 —— 拖动产生"复制"而非"移动"；preview 累积语义还会把中间帧一并落盘
  - 位置: src/features/composite/ui/timeline_commands.py:445-523（核心 489-511）
  - 证据:
    ```python
            kfs: List[Dict[str, Any]] = []
            for kf in (track_before or {}).get("keyframes", []):
                # Drop a source keyframe if the preview moves it (it will be
                # re-added at its new frame). The view guarantees a unique
                # new frame per dragged key; collisions validate downstream.
                kfs.append(dict(kf))
            ...
            merged_frames: Dict[int, Dict[str, Any]] = {
                int(kf.get("frame")): kf for kf in kfs}
            for new_frame, value in preview_map.items():
                if new_frame in merged_frames:
                    merged_frames[new_frame]["value"] = value
                else:
                    merged_frames[new_frame] = {
                        "frame": new_frame, "value": value,
                        "interpolation": "linear", "tangent_mode": "auto"}
    ```
  - 影响: 注释声明"Drop a source keyframe if the preview moves it"，但循环体原样 append 全部关键帧，丢弃逻辑未实现。拖 0→5、10→15 后轨道为 {0,5,10,15,20}：旧帧 0/10 仍在（edit_service 的 `set_keyframes` 为整轨替换，`add_keyframe` 仅同帧去重，旧帧必然落盘）——关键帧被复制而非移动，动画数据损坏；且新帧 upsert 使用 linear/auto 默认，丢失原关键帧的插值/手柄。另外 `update_move_preview` 按"累积"存储（`preview.setdefault(...)[int(new_frame)] = float(value)`），文档称"Safe to call many times during one drag"——若视图按每次鼠标移动传入新帧，拖动路径上的每个中间帧都会成为关键帧。测试 `test_move_press_move_release_one_undo_per_track` 只断言新帧存在、未断言旧帧消失，存在盲区。缓解：TimelineCommandBuilder 目前仅有测试调用，未接入生产视图；一旦接线即为 Critical 级数据丢失。
  - 建议: `begin_move` 记录每个被选关键帧的源帧；`commit_move` 先从 kfs 中剔除被移动的源帧再 upsert 新帧（并携带原关键帧的 interpolation/tangent/handles）；`update_move_preview` 改为同键覆盖语义（按被拖关键帧 id 而非帧号索引）。
  - 置信度: 高

- [AUD-11011] [Major] 表达式命令的 undo 记录不完整：`remove_expression` 不产生任何记录，`set_expression` 的 undo 无法恢复表达式文本
  - 位置: src/features/composite/ui/timeline_commands.py:686-727
  - 证据:
    ```python
    def remove_expression(self, node_id: str, param_name: str, ...
        return self._apply(plan_id, [{
            "op": "remove_expression", "node_id": node_id,
            "param_name": param_name,
        }], [])          # affected=[] → 不产生 TrackChange
    ...
    if result.ok and before_track is not None:
        after_track = self._snapshot_track(node, param_name)
        change = TrackChange(node_id=node_id, param_name=param_name,
                             before=before_track, after=after_track)
    ```
  - 影响: 模块自述为"单一编辑入口"，宿主通过 `record_track_change`/`record_node_range_change` 钩子记 undo（TimelineHost 协议仅此两个钩子，无表达式钩子）。`remove_expression` 完全无记录 → 依赖本层的宿主无法 undo；`set_expression` 只记录 keyframe 轨道 before/after，undo 恢复轨道后表达式仍存在，产生"轨道+表达式并存"的 A-15 互斥违规状态。缓解：生产宿主 composite_page 目前有自己的 `_restore_expression_state` 双向 undo 路径，本模块未接线。
  - 建议: 为表达式操作扩展结果对象（如 `ExpressionChange(node_id, param, before_text, after_text, before_track, after_track)`），或文档明确要求宿主自行记录表达式 undo。
  - 置信度: 中

- [AUD-11012] [Major] `paste_keyframes`（及 `commit_move`）的值不经范围归一化，越界值静默落盘
  - 位置: src/features/composite/ui/timeline_commands.py:610-632
  - 证据:
    ```python
        for entry in self.clipboard.entries:
            frame = int(anchor_frame) + entry.relative_frame
            by_frame[frame] = {
                "frame": frame, "value": entry.value,
                "interpolation": entry.interpolation,
                ...
        return self._apply(plan_id, [{
            "op": "set_keyframes", "node_id": target_node_id,
    ```
  - 影响: 粘贴类型检查只比较 ptype 字符串，不比较 min/max。edit_service 的 `set_keyframes` 分支直接 `track.add_keyframe(Keyframe.from_dict(dict(data)))`，不像 `add_keyframe` 分支那样调用 `_normalize_typed_value`（edit_service.py:770-782 对比），因此从 min/max 不同的同 ptype 参数粘贴越界值（如 rotation 45° 粘到 max=10 的参数）静默提交，污染动画数据。
  - 建议: paste 前按目标 pdef 的 min/max clamp 或拒绝（复用 ERR_VALUE_OUT_OF_RANGE）；或在 edit_service 的 set_keyframes 分支统一走 `_normalize_typed_value`。
  - 置信度: 高（本文件与 edit_service 双侧核验）

- [AUD-11013] [Suggestion] `commit_move` 中 min_val/max_val 计算后从未使用（死代码，且印证范围钳制未实现）
  - 位置: src/features/composite/ui/timeline_commands.py:483-487
  - 证据:
    ```python
            pdef = node.get_param_defs().get(param_name) if node else None
            min_val = (track_before or {}).get(
                "min_val", pdef.min_val if pdef else 0.0)
            max_val = (track_before or {}).get(
                "max_val", pdef.max_val if pdef else 1.0)
    ```
  - 影响: 两个局部变量无任何后续引用；拖动后的新值从未被 min/max 约束。
  - 建议: 删除或用于 clamp（与 AUD-11012 一并处理）。
  - 置信度: 高

- [AUD-11014] [Suggestion] `_classify_error` 依赖错误消息字符串前缀/子串匹配映射稳定错误码，脆弱耦合
  - 位置: src/features/composite/ui/timeline_commands.py:794-821
  - 证据:
    ```python
    m = message.lower()
    if m.startswith("node not found"):
        return ERR_NODE_NOT_FOUND
    ...
    if "_handle" in m or "handle_" in m:
        return ERR_HANDLE_INVALID
    ```
  - 影响: edit_service 的消息措辞一变即误分类为 ERR_PLAN_FAILED，AI/script 按稳定错误码分支的行为随之漂移，且无测试能兜住跨模块字符串契约。
  - 建议: CompositeEditError 增加 code 字段，_classify_error 优先读结构化码、字符串匹配仅作后备。
  - 置信度: 高

- [AUD-11015] [Suggestion] `apply_video_range_edit` 中 `source_out` 取值缺 None 防护（与 `source_in` 的 `or 0` 不对称）
  - 位置: src/features/composite/ui/timeline_commands.py:911
  - 证据:
    ```python
        old_in = int(params.get("source_in", 0) or 0)
        old_out = int(params.get("source_out", -1))
        new_out = old_in + (new_end - new_start)
    ```
  - 影响: 若工程数据中 `source_out` 存储为 None（键存在值为 None 时 `.get` 默认值不生效），`int(None)` 抛 TypeError；调用方 composite_page.py:8180-8186 用宽泛 `except Exception` 吞掉后视频源 trim_out 联动静默失效，无任何日志。节点默认值为 -1，触发概率低。
  - 建议: 改为 `int(params.get("source_out", -1) or -1)` 并与 source_in 风格统一；调用方至少记录 warning。
  - 置信度: 中

### 通过项
- 线程安全：无共享可变状态跨线程；clipboard/locks 为每 builder（每页面）所有，UI 线程使用
- GPU 资源生命周期 / GLSL·HDR：不涉及（纯数据层，无 QWidget/QPainter）
- 异常与取消路径：`_apply` 对 CompositeEditError/泛化异常双兜底不崩 UI；`cancel_move` 不触图；begin_move 遇锁/缺节点整体中止无半途状态
- 输入验证：paste 类型不匹配拒绝（ERR_PASTE_TYPE_MISMATCH）、set_node_range 端到端顺序校验、锁在所有变更命令前置检查
- 状态提交一致性：_apply 的 before/after 快照 + record 钩子形状与宿主 _push_keyframe_track_undo 对齐；锁为会话态不持久化（设计明确）
- 类型注解：完整（Protocol/dataclass/静态方法均有注解）

## src/features/composite/ui/timeline_expression.py (行数: 360)

### 发现

- [AUD-11020] [Suggestion] `derive_expression_status` 语法校验路径用宽泛 `except Exception` 把解析器崩溃当作 VALID
  - 位置: src/features/composite/ui/timeline_expression.py:206-212
  - 证据:
    ```python
    try:
        from src.features.composite.utils.expression_parser import ExpressionParser
        result = ExpressionParser().validate(text)
    except Exception:
        return ExpressionStatus(
            state=EXPRESSION_VALID, text=text, enabled=True,
            source_value=source_value)
    ```
  - 影响: 解析器自身缺陷（如异常的 AST）被掩盖并显示为"有效"，用户在接线后得不到任何错误提示；违反"不得宽泛 except 掩盖领域错误"。try 还同时包住 import，意图应是只包 validate。
  - 建议: 仅包 validate，捕获 (ExpressionError, ValueError, TypeError) 等具体异常，解析器崩溃返回 ERROR + EXPR_ERR_UNKNOWN。
  - 置信度: 高

- [AUD-11021] [Suggestion] `EXPR_ERR_EMPTY` 为死常量：文档承诺空表达式映射该码，但无任何路径产出
  - 位置: src/features/composite/ui/timeline_expression.py:124-150
  - 证据:
    ```python
    EXPR_ERR_EMPTY = "expr_empty"
    ...
    def classify_expression_error(error):
        """...An empty/whitespace-only expression yields
        EXPR_ERR_EMPTY (a distinct code so the UI can prompt "enter an
        expression" instead of showing a generic syntax error)."""
        if error is None:
            return "", ""
        code = EXPRESSION_ERROR_CODES.get(error.error_type, EXPR_ERR_UNKNOWN)
    ```
  - 影响: 全库 grep 无其他引用；空文本走 SYNTAX/UNKNOWN 分类，UI 无法按文档区分"请输入表达式"提示，AI manifest 若声明该码则永不触发。
  - 建议: 在 classify 或 derive 入口对 `not text.strip()` 显式返回 EXPR_ERR_EMPTY，或删除常量并改文档。
  - 置信度: 高

- [AUD-11022] [Suggestion] `_node_*` 鸭子类型访问器统一吞掉节点实现的真实异常
  - 位置: src/features/composite/ui/timeline_expression.py:297-343
  - 证据:
    ```python
    def _node_has_expression(node: Any, param_name: str) -> bool:
        fn = getattr(node, "has_expression", None)
        if callable(fn):
            try:
                return bool(fn(param_name))
            except Exception:
                return False
    ```
  - 影响: `node.has_expression` 等因真实缺陷抛错时被静默当作"无表达式"，状态显示错误且无日志（文档说明是为容忍测试替身，属有意为之，但同样掩盖领域错误）。
  - 建议: 至少在 except 中 logger.debug/record，或缩窄异常类型。
  - 置信度: 中

### 通过项
- 线程安全 / GPU 生命周期 / GLSL·HDR / 信号防递归：不涉及（纯数据层）
- 状态机正确性：四态推导分支完整；`resolve_precedence` 的 valid/error-fallback/disabled/none 四路径与 §5.4 优先级一致；frozen dataclass + 默认 ExpressionPolicy() 不可变安全
- 类型注解：完整

## src/features/composite/ui/timeline_expression_editor.py (行数: 300)

（文件头部标注 DORMANT(2026-08-19)：已实现有测试但无生产入口。以下发现为接线时生效的缺陷。）

### 发现

- [AUD-11025] [Major] 实时校验预览不反映正在输入的文本：provider 只见 (node_id, param_name)，读的是已存表达式
  - 位置: src/features/composite/ui/timeline_expression_editor.py:248-278, 186-211
  - 证据:
    ```python
    if self._status_provider is not None:
        try:
            status = self._status_provider(self._node_id, self._param_name)
        except Exception:
            status = None
        if isinstance(status, ExpressionStatus):
            self._apply_status(status, text)
            return
    ```
    且 `_apply_status(self, status: ExpressionStatus, text: str)` 的 `text` 参数在函数体内从未使用。
  - 影响: `_on_text_changed` 每次按键轮询 provider，但 provider 签名拿不到编辑器草稿文本，返回的是节点上**已提交**表达式的状态——输入非法表达式仍显示旧的 "Valid — overrides ..."，与模块文档"live status preview (valid / error)"直接矛盾；`_apply_status` 未使用的 text 参数是该设计缺口的直接证据。
  - 建议: provider 签名改为 `(node_id, param, draft_text) -> ExpressionStatus`（内部用 `ExpressionParser().validate(draft)` 或临时挂载文本推导），或在编辑器内直接调用 derive_expression_status。
  - 置信度: 高

- [AUD-11026] [Suggestion] `close()` 覆盖返回 None，破坏 `QWidget.close() -> bool` 契约
  - 位置: src/features/composite/ui/timeline_expression_editor.py:214-217
  - 证据:
    ```python
    def close(self) -> None:  # type: ignore[override]
        self._node_id = None
        self._param_name = None
        super().close()
    ```
  - 影响: 调用方 `if editor.close():` 永远为假；`super().close()` 的返回值被丢弃。已用 type: ignore 承认不匹配。
  - 建议: 改名 `reset()`/`hide_editor()` 清状态，或保持 `-> bool` 并返回 super() 结果。
  - 置信度: 高

- [AUD-11027] [Suggestion] 文档与实现的提交快捷键矛盾：文档称 "Enter / Ctrl+Enter → commit"，实现为普通 Enter 换行、仅 Ctrl/Cmd+Enter 提交
  - 位置: src/features/composite/ui/timeline_expression_editor.py:8-9, 26-27 对比 69-83
  - 证据:
    ```
    * Enter / Ctrl+Enter → commit; emit expression_committed(...). Esc → cancel.
    ...
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if mods & (Qt.KeyboardModifier.ControlModifier
                       | Qt.KeyboardModifier.MetaModifier):
                self.commit_requested.emit()
    ```
  - 影响: 按模块文档直接按 Enter 的用户会插入换行而非提交，交互预期不符（类 docstring 与 _ExpressionTextArea docstring 互相一致地描述了另一种行为）。
  - 建议: 统一为 Ctrl/Cmd+Enter 提交（更新模块/类 docstring），或单行模式下让 Enter 直接提交。
  - 置信度: 高

### 通过项
- 线程安全 / GPU 生命周期 / GLSL·HDR：不涉及（纯 Qt UI 线程控件）
- 信号防递归：`_disable_btn` 连接的是 `clicked`（非 `toggled`），`open_for`→`_refresh_disable_btn`→`setChecked` 的程序化路径不会重入 `_on_disable_toggled`、不产生伪信号（已用 offscreen 实测：open_for(enabled=False/True) 两种情况下 expression_disabled 零发射）
- 状态提交一致性：commit 只发信号由宿主走 set_expression 统一路径（undo+锁），编辑器自身不直接改图
- 异常路径：provider 调用有 try/except 兜底；无 provider 时有空文本提示降级
- 类型注解：完整

## src/features/composite/ui/timeline_icons.py (行数: 545)

### 发现

- [AUD-11030] [Suggestion] `_draw_graph_editor` 函数内重复局部导入顶层已有的 QPainterPath
  - 位置: src/features/composite/ui/timeline_icons.py:463-464
  - 证据:
    ```python
    def _draw_graph_editor(p: QPainter, cx: float, cy: float, ...
        # Curve.
        from PyQt6.QtGui import QPainterPath
        path = QPainterPath()
    ```
  - 影响: 顶层 `from PyQt6.QtGui import ... QPainterPath ...`（第 28 行）已导入；同文件 `_draw_eye` 直接使用顶层导入，风格不一致，每次绘制多一次 import 查询（paint 路径热区，开销极小）。
  - 建议: 删除局部 import，直接使用顶层导入。
  - 置信度: 高

### 通过项
- 线程安全：draw_icon/make_qicon 均在 UI 线程（QPixmap 要求 GUI 线程，调用方 keyframes_tree_view/keyframes_panel 均为 paint/构建期）
- GPU 资源生命周期：不涉及
- GLSL·HDR 正确性：不涉及
- 信号防递归 / 状态提交：不涉及（无状态绘制函数）
- 异常路径：draw_icon 用 try/finally 保证 painter.save/restore 配对，未知图标名安全跳过；make_qicon 保证 p.end()
- 输入验证：`_align` 半像素对齐、`h = max(2.0, half)`/`node_r = max(1.8, ...)` 下限保护、make_qicon `size = max(8, ...)` 下限
- 性能：纯几何绘制无昂贵运算；keyframes_tree_view paint 路径调用为轻量多边形
- 类型注解：完整

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 7 | AUD-11001, AUD-11002, AUD-11003, AUD-11010, AUD-11011, AUD-11012, AUD-11025 |
| Suggestion | 10 | AUD-11004, AUD-11013, AUD-11014, AUD-11015, AUD-11020, AUD-11021, AUD-11022, AUD-11026, AUD-11027, AUD-11030 |

跨文件备注（非本批次发现，供后续批次参考）：`backdrop_item.py:707-718` 与 AUD-11001 相同的非模态 QColorDialog GC 模式；`edit_service.py` set_keyframes 不做值归一化（AUD-11012 根因之一）。
