# batch-100 审查报告 — src/features/composite/ui/

- 编号区间：AUD-10000–AUD-10099
- 审查文件：import_report_panel.py / keyframe_editor.py / keyframes_model.py
- 统计：Critical 0 · Major 4 · Suggestion 8
- 交叉验证：src/shared/keyframe.py（KeyframeTrack.get_value 空 track 安全、Keyframe.__lt__ 存在、InterpolationType 恰有 6 成员）、src/features/composite/ui/node_properties_panel.py（两处 KeyframeEditorWidget 宿主均为一次性对话框且未连接 keyframes_changed）、src/features/composite/render3d/model_import_report.py（from_summary_dict 缺省字段为 0，格式化安全）。

## src/features/composite/ui/import_report_panel.py (行数: 438)

### 发现

- [AUD-10012] [Suggestion] update_report 内函数级 `import os`，应上移到模块顶部
  - 位置: src/features/composite/ui/import_report_panel.py:346
  - 证据:
    ```python
        # 路径只显示文件名（完整路径太长）
        import os
        path = report.source_path
        self._path_label.setText(os.path.basename(path) if path else "—")
    ```
  - 影响: 每次刷新报告都执行一次 import 查询（sys.modules 命中，开销小但属反模式）；与项目其余模块的导入组织方式不一致。
  - 建议: 将 `import os` 移到文件头部 import 区。
  - 置信度: 高

### 通过项

- 线程安全：面板只接收基础类型 / dict 快照（update_import_state/update_summary 明确为 Qt 主线程设计），不持有 Future 或 GL 对象。
- GPU 资源生命周期：不涉及任何 ModernGL / OpenGL 资源。
- GLSL·HDR：不涉及（纯 Qt 展示面板）。
- 信号防递归：面板不发射任何信号，纯被动刷新，无递归风险。
- 异常路径：`_format_bytes`（TypeError/ValueError）、`update_import_state` 的 progress 解析均有窄异常处理，无宽泛 except。
- 输入验证：update_resource_estimate / update_format_probe / update_diagnostics / update_summary 均做 isinstance 与字段级校验；progress 钳制到 [0,1]。
- 类型注解：公开方法与辅助函数注解完整（Optional[QWidget]、Optional[dict] 等）。

## src/features/composite/ui/keyframe_editor.py (行数: 911)

### 发现

- [AUD-10001] [Major] 选中关键帧时程序化更新 spin/combo 无状态锁，重入处理器并发出冗余 keyframes_changed 提交
  - 位置: src/features/composite/ui/keyframe_editor.py:821-837（触发点 641/651/612 的 valueChanged/currentIndexChanged 连接；重入落点 871-879、881-897）
  - 证据:
    ```python
    def _on_keyframe_selected(self, frame: int) -> None:
        kf = self._track.get_keyframe_at(frame) if self._track else None
        if kf:
            self._frame_spin.setValue(kf.frame)
            self._value_spin.setValue(kf.value)
            self._curve_widget.set_selected_frame(frame)
            ...
            self._interp_combo.setCurrentIndex(interp_idx)
    ```
    重入路径（无值变化仍提交）：
    ```python
    def _on_value_spin_changed(self, value: float) -> None:
        if self._track and self._timeline_widget.selected_keyframe():
            selected = self._timeline_widget._selected_frame
            kf = self._track.get_keyframe_at(selected)
            if kf:
                kf.value = value
                self._curve_widget.set_track(self._track)
                self._emit_keyframes_changed()
    ```
  - 影响: Qt 的 setValue/setCurrentIndex 程序化调用同样触发 valueChanged/currentIndexChanged。每次在时间线点击选中一个关键帧，`_on_value_spin_changed` 与 `_on_interpolation_changed` 都会重入执行并调用 `_emit_keyframes_changed()`——纯选择动作产生 2 次无数据变化的提交信号（违反"程序化更新控件须状态锁防递归信号"铁律）。当前两个宿主（node_properties_panel.py:4744/5582）按 F4 原子 OK/Cancel 模式故意不连接 keyframes_changed，故暂无线上故障；但类 docstring 明确承诺"Emits keyframe_changed signal when keyframes are modified"，任何按此约定接线的新宿主会在每次选择时触发无谓重渲染 / 潜在 undo 条目污染。
  - 建议: 引入 `self._updating_ui` 状态锁（在 `_on_keyframe_selected`/`set_parameter` 程序化设值前后置位/复位），处理器入口处 early-return；或在 `_on_value_spin_changed`/`_on_interpolation_changed` 中比对新旧值无变化则跳过提交。
  - 置信度: 高（重入机制确定；现网影响被宿主布线缓解，属潜伏缺陷）

- [AUD-10002] [Major] set_parameter 的 setChecked 程序化触发 _on_animation_toggled，以"旧参数 track + 新参数名"的混合状态执行快照与提交
  - 位置: src/features/composite/ui/keyframe_editor.py:677-681（触发）；742-784（处理器）
  - 证据:
    ```python
        self._disabled_backup = None        # 677
        ...
        self._is_animated = param_def.animated if param_def else False
        self._anim_checkbox.setChecked(self._is_animated)   # 681：stateChanged → _on_animation_toggled
    ```
    此时 `self._track` 仍是上一个参数的 track（set_parameter 在 683 行之后才重建），而 `_param_name`/`_param_def` 已指向新参数：
    ```python
        else:
            if self._track and self._track.keyframes:
                import copy
                self._disabled_backup = copy.deepcopy(self._track.keyframes)
                self._track.keyframes.clear()
        # Emit change
        self._emit_keyframes_changed()
    ```
  - 影响: 当复用同一编辑器切换参数且 checkbox 状态翻转时：(1) 关闭侧会把旧参数的关键帧快照进 `_disabled_backup`（677 行的清空被处理器覆盖，备份与当前参数不符），并以新参数名 emit `(param, [])`——按本文件 A-6 语义会删除新参数的节点级轨道；(2) 开启侧在旧 track 已清空时会用新参数的 default 播种两点默认 track 并 emit，随后 683 行才用真实关键帧重建内部 track，形成"已提交默认种子 vs 编辑器显示真实曲线"的分叉。跨参数动画数据串扰属数据丢失/渲染错误级后果。当前两个宿主均为每次弹窗新建编辑器（此时 `_track` 为 None，处理器分支全部跳过且 `_emit_keyframes_changed` 因 track 为 None 不发射），故为潜伏缺陷。
  - 建议: 为 `setChecked` 加状态锁（同 AUD-10001）；或把"读取 param_def → 重建 track → 再同步 checkbox"的顺序调整为先建 track 再设 UI，并保证 set_parameter 全程不触发提交信号。
  - 置信度: 高（重入与混合状态为代码事实；数据串扰场景依赖宿主复用编辑器，当前仓库内无此用法）

- [AUD-10003] [Major] 帧号 spinbox 移动关键帧：无同帧碰撞检查、无 total_frames 钳制、移动后选择状态不同步
  - 位置: src/features/composite/ui/keyframe_editor.py:862-869（处理器）；637-638（spin 范围）；802-819（_on_keyframe_moved 无去重）；对比 304-328（时间线拖拽有 _find_free_frame）
  - 证据:
    ```python
    def _on_frame_spin_changed(self, frame: int) -> None:
        """Handle frame spinbox change."""
        # Move selected keyframe to new frame
        if self._track and self._timeline_widget.selected_keyframe():
            selected = self._timeline_widget._selected_frame
            kf = self._track.get_keyframe_at(selected)
            if kf and frame != selected:
                self._on_keyframe_moved(selected, frame, kf.value)
    ```
    spin 范围与移动实现：
    ```python
    self._frame_spin.setRange(0, 100000)
    ...
    kf.frame = new_frame
    self._track.keyframes.sort()      # 不去重、不钳制
    ```
  - 影响: (1) 输入与现有关键帧相同的帧号会产生同帧重复关键帧——src/shared/keyframe.py 的 add_keyframe 文档明确同帧重复会导致插值不稳定，且 get_keyframe_at 只返回第一个匹配，后续对该帧的值/插值编辑命中歧义对象；(2) 帧号可超出 total_frames-1（spin 上限 100000），关键帧被移出可见时间线与合成范围；(3) 移动后 `_timeline_widget._selected_frame` 仍指向旧帧，`get_keyframe_at(旧帧)` 返回 None，随后的 Value/插值编辑静默失效（用户改值无响应）。三个问题都在当前弹窗内即可复现。
  - 建议: `_on_frame_spin_changed` 中先 `min(frame, self._total_frames - 1)` 钳制，再复用 `_find_free_frame` 去碰撞，移动成功后同步更新 timeline 的 `_selected_frame` 与各控件。
  - 置信度: 高

- [AUD-10004] [Suggestion] 曲线/时间线拖拽期间每个 mouseMove 触发一次完整提交信号，无节流或"拖拽结束才提交"的批处理
  - 位置: src/features/composite/ui/keyframe_editor.py:528-535（曲线拖值）；262-274（时间线拖帧）；899-911（_emit_keyframes_changed）
  - 证据:
    ```python
        if self._dragging and self._selected_frame >= 0:
            new_value = self._y_to_value(pos.y())
            if self._track:
                new_value = max(self._track.min_val, min(self._track.max_val, new_value))
                self.keyframe_value_changed.emit(self._selected_frame, new_value)
    ```
    （`_on_value_changed` 收到后逐次 `_emit_keyframes_changed()`）
  - 影响: 一次拖拽产生数十至数百次 keyframes_changed。当前宿主未连接该信号（原子提交），影响潜伏；但 current_frame_changed（播放头拖动）已按设计逐 move 连接到宿主重求值，同类模式若被 keyframes_changed 采用会造成 4K 场景下每像素一次的提交/重渲染与 undo 粒度爆炸。
  - 建议: 拖拽中只更新本地 track 与重绘，mouseRelease 时一次性 emit；或对 emit 做合帧（QTimer 单触发）。
  - 置信度: 高（代码事实明确，影响取决于宿主连接方式）

- [AUD-10005] [Suggestion] 13 个未使用的导入
  - 位置: src/features/composite/ui/keyframe_editor.py:16-27
  - 证据:
    ```python
    from PyQt6.QtWidgets import (
        QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
        QComboBox, QFrame, QScrollArea, QToolButton, QMenu, QSpinBox,
        QDoubleSpinBox, QSizePolicy, QCheckBox, QSlider
    )
    from PyQt6.QtCore import Qt, pyqtSignal, QPointF, QRectF, QTimer
    from PyQt6.QtGui import (
        QColor, QPainter, QPen, QBrush, QPainterPath, QFont,
        QPolygonF, QCursor, QMouseEvent, QKeySequence
    )
    from typing import Dict, Any, Optional, List, Tuple
    ```
  - 影响: 逐一统计文件内出现次数，QPushButton/QFrame/QScrollArea/QToolButton/QMenu/QSizePolicy/QSlider/QRectF/QTimer/QCursor/QKeySequence/Dict/Tuple 仅出现在 import 行（各 1 次）。死导入增加阅读噪音并掩盖真实依赖。
  - 建议: 删除上述未使用导入，保留 QLabel/QCheckBox/QComboBox/QSpinBox/QDoubleSpinBox/QWidget/布局类、QPointF、QColor/QPainter/QPen/QBrush/QPainterPath/QFont/QPolygonF/QMouseEvent、Any/Optional/List。
  - 置信度: 高（grep 计数验证）

- [AUD-10006] [Suggestion] 多个方法缺少类型注解
  - 位置: src/features/composite/ui/keyframe_editor.py:77、344、574（`def __init__(self, parent=None)`）；160、416（`def paintEvent(self, event)`）；253、507（mousePressEvent 无返回注解）；286、550（`def mouseReleaseEvent(self, event)`）；589（`def _init_ui(self)`）
  - 证据:
    ```python
    def __init__(self, parent=None):
        super().__init__(parent)
    ...
    def paintEvent(self, event):
        """Paint the timeline."""
    ```
  - 影响: 与项目强类型约束（所有函数有类型注解）不符；同类文件（import_report_panel.py）均已完整注解。
  - 建议: 统一补 `parent: Optional[QWidget] = None`、`event: QPaintEvent` / `-> None` 等注解。
  - 置信度: 高

### 通过项

- 线程安全：纯 Qt 主线程 widget，无跨线程调用，无 OpenGL 触碰（符合铁律）。
- GPU 资源生命周期：不涉及。
- GLSL·HDR：不涉及着色器；值域钳制沿用 track 的 [min_val, max_val]，未见对 HDR 数据的无理由 clamp（该值域来自 ParamDef 定义）。
- 空轨道取值安全：KeyframeCurveWidget.paintEvent 对空 keyframes 调 get_value（keyframe_editor.py:501）——已验证 src/shared/keyframe.py:221-222 空 keyframes 返回钳制默认值，不会抛异常。
- keyframes.sort() 可行：`_on_keyframe_moved` 的 F5 原位移动依赖 Keyframe.__lt__（src/shared/keyframe.py:114-116 存在），排序正确且保留插值/贝塞尔手柄（对比旧 remove+new 方案的修复注释成立）。
- InterpolationType 映射完整：combo 的 6 项映射（829-836）恰好覆盖枚举全部 6 个成员（src/shared/keyframe.py:23-30），不会把未知插值静默压成 LINEAR。
- S5 快照/恢复逻辑：单一参数弹窗内 OFF→ON 快照恢复（742-784）行为正确（含 A-6 空 list 语义）。
- 播放头拖动（P1）：`_seek_to_x` 仅 emit 标量帧号，`_on_frame_clicked` 钳制范围后转发，无信号回环（宿主 set_current_frame 不反向发信号进本 widget）。

## src/features/composite/ui/keyframes_model.py (行数: 1000)

### 发现

- [AUD-10007] [Major] 11 处宽泛 `except Exception` 静默吞掉领域错误，违反"不得宽泛 except 掩盖领域错误"铁律
  - 位置: src/features/composite/ui/keyframes_model.py:263-265、394-396、475-477、812-813、819-821、826-829、837-840、857-859、873-875、879-881、904-905
  - 证据（代表性两处）:
    ```python
        if callable(active_layers_fn):
            try:
                active_layer_ids = set(active_layer_fn())
            except Exception:
                active_layer_ids = None
    ```
    ```python
        @staticmethod
        def _node_color(node: Any) -> str:
            try:
                return node.effective_header_color()
            except Exception:
                return getattr(node, "header_color", "#555555") or "#555555"
    ```
    其余为 `_ordered_nodes` 的 nodes.values()/sort、`_node_label` 的 display_title、constant 分支的 keyframe_parameter_specs、两处 get_param_defs、`_param_label` 的 i18n tr_or_none，以及 rebuild_layers 内 graph.get_node/main_stack 的同类写法。
  - 影响: AttributeError/KeyError/TypeError 等真实缺陷（拼错的属性、控制器接口变更、i18n 键异常）会被降级为"行为静默改变"——行丢失、颜色回退、参数列表为空、活动层过滤失效——无日志无告警，排障困难；`active_layer_ids_for_output` 抛错时甚至把"应显示哪些层"这一语义悄悄切换成"显示全部"。
  - 建议: 逐处收窄为预期的具体异常（AttributeError/KeyError/TypeError 等）；无法收窄的至少记录 debug 日志。对控制器/图接口的调用优先用 hasattr/isinstance 预检而非整体 try。
  - 置信度: 高（位置与代码确定；定级 Major 依据项目铁律将此模式列为明确风险）

- [AUD-10008] [Suggestion] rebuild_layers 每层对 graph.connections 全量线性扫描，复杂度 O(层数×节点×连接) 且无缓存
  - 位置: src/features/composite/ui/keyframes_model.py:495-498（visit 内）；488-507（遍历结构）；触发点 keyframes_panel.py:1483（面板每次刷新重建）
  - 证据:
    ```python
        def visit(node_id: str) -> None:
            if not node_id or node_id in visited:
                return
            visited.add(node_id)
            node = graph.get_node(node_id)
            if node is None:
                return
            incoming = [c for c in getattr(graph, "connections", [])
                        if c.dst_node_id == node_id]
    ```
  - 影响: 每层调用 `_layer_effect_nodes`，其 visit 对每个可达节点都完整扫描一次 connections 列表；层数多、图大时（数百节点×上千连接×数十层）单次重建达千万级比较，Keyframes 面板刷新（选中/图变更即触发）出现可感知卡顿。
  - 建议: 在 rebuild_layers 入口一次性构建 `dst_node_id -> [connections]` 邻接索引传给各层的遍历；或缓存投影结果、以图版本号失效。
  - 置信度: 中（复杂度为代码事实；卡顿程度取决于图规模与刷新频率）

- [AUD-10009] [Suggestion] 层模式下 KeyframesTrackVisual.row_id 恒为 node: 前缀，与实际存储键 layer: 前缀不一致
  - 位置: src/features/composite/ui/keyframes_model.py:397-398（以 `layer_row.row_id` 即 `f"layer:{layer_id}"` 为键）；964-966（visual 内 row_id 固定 `f"node:{node.id}"`）
  - 证据:
    ```python
            self._tracks[layer_row.row_id] = self._build_node_track(
                node, color, source_node=source_node)
    ```
    ```python
        return KeyframesTrackVisual(
            row_id=f"node:{node.id}",
            range_bar=range_bar, color=color, node_color=color,
            source_total=source_total)
    ```
  - 影响: 同一 visual 的 `row_id` 字段与其在 `_tracks` 中的键不同。已核对全部消费方（keyframes_timeline_view.py 的 track_for/tracks().items()）均以 dict 键为准，未读取 `track.row_id`，故当前无故障；但任何未来按 `track.row_id` 反查行/选中映射的代码会在层模式错位到不存在的行。
  - 建议: `_build_node_track` 增加 `row_id` 参数由调用方传入，或删去 visual 上冗余的 row_id 字段，保证键与字段一致。
  - 置信度: 高

- [AUD-10010] [Suggestion] 未使用的导入 KeyframeTrack 与死常量 _PARAM_CHANNEL_COLORS
  - 位置: src/features/composite/ui/keyframes_model.py:25；179
  - 证据:
    ```python
    from src.shared.keyframe import KeyframeTrack
    ...
    _PARAM_CHANNEL_COLORS: Dict[str, str] = {}
    ```
  - 影响: 全文件仅此一处出现 KeyframeTrack（本模型直接读 node.keyframe_tracks 原始 dict，不使用该类）；`_PARAM_CHANNEL_COLORS` 定义后无任何引用，`_channel_color_for` 并未使用它。死代码误导维护者以为存在可配置的通道色表。
  - 建议: 删除该导入与死常量（或若原计划做配置表，则补齐实现）。
  - 置信度: 高

- [AUD-10011] [Suggestion] 个别函数缺少类型注解
  - 位置: src/features/composite/ui/keyframes_model.py:843（`def _animatable_params(node: Any):` 无返回注解）；815（`def _key(n):`）
  - 证据:
    ```python
        @staticmethod
        def _animatable_params(node: Any):
    ...
        def _key(n):
            x, y = getattr(n, "position", (0.0, 0.0)) or (0.0, 0.0)
    ```
  - 影响: 与模块内其余方法（均有 `-> List[...]`/`-> str` 等注解）不一致；`_animatable_params` 实际返回 `List[Tuple[str, Any]]`。
  - 建议: 补 `-> List[Tuple[str, Any]]` 与 `-> Tuple[float, float]`（入参 `n: Any`）。
  - 置信度: 高

### 通过项

- 线程安全：纯数据投影层（无 QWidget/QPainter），单线程使用，明确 GUI-free 设计。
- GPU 资源生命周期：不涉及。
- GLSL·HDR：不涉及；KeyframePointVisual.value 保持原类型不强制转 float（P1-1 保持定义域，注释与实现一致）。
- 信号防递归：模型不持有/不发射任何 Qt 信号。
- 状态提交一致性：模型为按需重建的只读投影（无增量 diff），expansion/filter/selection 状态与行数据同步（set_expanded/expand_all/set_selected_node 均同步行字段）；set_comp_range 原地刷新 default bar 与重建流程一致。
- 输入验证（关键路径）：`_media_total_frames` 以 TypeError/ValueError 收窄并 max(0,·) 钳制；`_build_param_track` 对畸形关键帧逐条窄异常跳过且保留 extra 字段（P1-1）；FrameMapper.set_zoom 双端钳制。
- `_values_equal` 对 numpy 数组的真值比较做了 bool/all/回退三级处理，避免 ValueError。
- `_build_merge_owner_index` 以有序 live 层列表重建 merge 归属，规避了 A 连接推断在 M1+ 上的失效（注释所述问题处理正确）。
