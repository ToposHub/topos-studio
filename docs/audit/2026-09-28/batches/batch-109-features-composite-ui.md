# 批次报告：batch-109 — src/features/composite/ui（Spline 曲线编辑器）

- 审查范围：`spline_commands.py` (85)、`spline_model.py` (492)、`spline_panel.py` (741)、`spline_view.py` (1,543)，共 2,861 行
- 编号区间：AUD-10900–AUD-10999
- 结论摘要：Critical 1 / Major 10 / Suggestion 8。无 GPU/GLSL 代码（QPainter 自绘 + 纯数据层），GPU 资源与 GLSL·HDR 两维度全部通过；问题集中在交互逻辑缺陷、信号接线缺口与共享选择模型的封装破坏。

---

## src/features/composite/ui/spline_commands.py (行数: 85)

文件头标注 DORMANT(2026-08-19)：已实现且有测试、暂无生产入口，纯数据记录层。

### 发现
- [AUD-10900] [Suggestion] `from_key`/`apply_to` 的 `key` 参数无类型注解，且 `Optional` 导入未使用
  - 位置: src/features/composite/ui/spline_commands.py:14,28,38
  - 证据:
    ```python
    from typing import Any, Dict, List, Optional
    ...
        @classmethod
        def from_key(cls, key) -> "KeySnapshot":
            return cls(
                frame=key.frame,
    ...
        def apply_to(self, key) -> None:
    ```
  - 影响: 快照捕获/恢复的输入类型不可见，误传非 SplineKey 对象只会在运行期 AttributeError；未使用导入违反项目整洁约束。
  - 建议: `key: "SplineKey"`（TYPE_CHECKING 导入）或定义最小 Protocol；删除 `Optional`。
  - 置信度: 高

### 通过项
- 纯数据记录（dataclass），无执行逻辑；DORMANT 标注明确
- 快照字段完整覆盖 frame/value/interpolation/tangent_mode/双侧 handle，undo/redo 恢复无遗漏
- 无线程、GPU、GLSL、信号维度风险

---

## src/features/composite/ui/spline_model.py (行数: 492)

### 发现
- [AUD-10901] [Major] 曲线可见性状态在 rebuild 后全部丢失（`_visible_ids` 语义缺陷）
  - 位置: src/features/composite/ui/spline_model.py:397-398,485-492
  - 证据:
    ```python
                    visible=(f"{node.id}:{pname}" in self._visible_ids)
                            if self._visible_ids else True,
    ...
        def set_visible(self, curve_id: str, visible: bool) -> None:
            c = self.curve_by_id(curve_id)
            if c is not None:
                c.visible = visible
                if visible:
                    self._visible_ids.add(curve_id)
                else:
                    self._visible_ids.discard(curve_id)
    ```
  - 影响: `_visible_ids` 只记录"显式显示"的曲线，隐藏操作对空集 discard 后集合仍为空，rebuild 时 `if self._visible_ids` 为假 → 所有曲线强制 `visible=True`。用户隐藏任意曲线（或点工具栏 Hide all）后，下一次 `set_node_graph → rebuild`（每次编辑都会触发，见 spline_panel.py:559-562 注释）就把全部曲线重新显示，隐藏状态静默丢失。
  - 建议: 改为记录 hidden 集合（默认可见），或 `Dict[curve_id, bool]` 三态记忆。
  - 置信度: 高

- [AUD-10902] [Major] `rebuild`/`_node_color` 宽泛 `except Exception` 掩盖领域错误，失败时曲线已先被清空
  - 位置: src/features/composite/ui/spline_model.py:366-377,403-408
  - 证据:
    ```python
        def rebuild(self, node_graph: Any) -> None:
            self._curves.clear()
            try:
                nodes = list(node_graph.nodes.values())
            except Exception:
                return
            try:
                nodes.sort(key=lambda n: (n.position[1], n.position[0]))
            except Exception:
                pass
    ```
  - 影响: 违反 .clinerules「不得宽泛 except 掩盖领域错误」。`node_graph` 异常（None、nodes 属性抛错）时 `self._curves` 已在 try 前 clear，编辑器静默变空且无任何诊断；排序与 `_node_color` 的裸 except 同样吞掉 position/颜色结构变更的真实错误。
  - 建议: 缩窄为 `(AttributeError, TypeError)` 并记录日志；节点枚举成功后再 clear 旧曲线。
  - 置信度: 高

- [AUD-10903] [Suggestion] 在 dataclass 实例上动态注入 `_had_saved_handles` 未声明属性
  - 位置: src/features/composite/ui/spline_model.py:448-449,459
  - 证据:
    ```python
                    k._had_saved_handles = (hl is not None or hr is not None
                                            or 'bezier_in' in kf or 'bezier_out' in kf)
    ...
                had_saved = getattr(k, "_had_saved_handles", False)
    ```
  - 影响: 依赖 SplineKey 无 `__slots__` 的实现细节；类型检查器/IDE 不可见，后续加 slots 即 AttributeError。
  - 建议: 在 SplineKey 上声明 `had_saved_handles: bool = False` 字段（序列化时排除）。
  - 置信度: 高

### 通过项
- GLSL·HDR 维度不适用（纯 Qt 数据层，无渲染代码）；键值钳制到参数 [min,max] 属参数域约束，合理
- 贝塞尔数学（cubic_bezier_xy/_ease_progress/auto_slope/apply_smooth_opposite/distance_point_to_segment）正确，含除零与退化保护（dx<=0、长度 1e-9 下限）
- `_build_keys` 对外部 keyframe 数据逐键窄异常校验（TypeError/ValueError/IndexError）并排序；handle 读取 handle_left/right 优先、bezier_in/out 回退
- `to_dict` 双写新旧字段，序列化往返一致
- 线程安全：仅 UI 线程使用，无跨线程共享

---

## src/features/composite/ui/spline_panel.py (行数: 741)

### 发现
- [AUD-10904] [Major] 树行选中/可见性切换调用 `view.refresh()` 重置视口，与「selection does not reset zoom」设计矛盾
  - 位置: src/features/composite/ui/spline_panel.py:678-681,683-696,437-441（spline_view.py:273-275）
  - 证据:
    ```python
        def _on_curve_visibility_toggled(self, _curve_id: str,
                                         _visible: bool) -> None:
            """Tree checkbox toggled → repaint the chart so the curve hides/shows."""
            self._view.refresh()
    ...
        def _on_curve_selected(self, curve_id: str) -> None:
            self._view.refresh()
    ```
    而 `SplineView.refresh()` 为 `self._viewport = None  # viewport depends on curves → recompute`。
  - 影响: 树中点击曲线行或勾选可见性都会丢弃用户缩放/平移、重新 fit 全部可见曲线。本文件 `attach_to_shared_selection`（589-592 行）明确引用 plan §7.2「selection does not reset zoom」并用 `update()` 保视口——同一面板两条选择路径行为相反。
  - 建议: 改为 `self._view.update()`；可见性切换若确需 refit，仅在可见曲线从无到有时 fit。
  - 置信度: 高

- [AUD-10905] [Major] 视图级 `current_frame_changed` 信号从未连接，标尺拖动 seek 失效
  - 位置: src/features/composite/ui/spline_panel.py:229-239（spline_view.py:806-811 发射端）
  - 证据:
    ```python
        self._view = SplineView(self._model, parent=self._splitter)
        self._view.keyframe_changed.connect(self.keyframe_changed)
        self._view.curve_settings_changed.connect(self.curve_settings_changed)
        self._view.curve_selection_changed.connect(self._on_curve_selected)
        self._view.key_selection_changed.connect(
            self._on_key_selection_changed)
    ```
    视图标尺拖动会发射（spline_view.py:808-811）：`self.set_current_frame(frame)` + `self.current_frame_changed.emit(frame)`。全库 grep 确认无人连接 `self._view.current_frame_changed`；CompositePage 只连接面板级信号（composite_page.py:7854-7855）。
  - 影响: 在标尺上拖动播放头只改视图内部 `_current_frame`，合成时间轴不 seek；面板下一次 `set_current_frame` 同步会把播放头弹回原帧——标尺拖动整体失效且伴随回跳。
  - 建议: `_init_ui` 中 `self._view.current_frame_changed.connect(self.current_frame_changed)`（或转发进 `seek_to_frame` 以获得范围钳制后再 emit）。
  - 置信度: 高

- [AUD-10906] [Major] `_on_key_selection_changed` 直接改写共享选择模型私有 `_selection` 并调用私有 `_notify()`，绕过公共 API
  - 位置: src/features/composite/ui/spline_panel.py:729-741
  - 证据:
    ```python
            shared.begin_batch()
            if pairs:
                shared._selection.keyframes = pairs
                shared._selection.rows = {row_id for row_id, _ in pairs}
                first = sorted(pairs)[0]
                shared._selection.primary_row_id = first[0]
                shared._selection.primary_frame = first[1]
                shared._notify()
            else:
                shared.clear_keyframes()
        finally:
            shared.end_batch()
    ```
  - 影响: `AnimationSelectionModel.set_keyframes()`（timeline_view_model.py:485-494）提供完全相同的公共逻辑，此处却改私有字段并整体替换 `rows`（公共 API select_row/set_keyframes 均不整体覆写 rows，语义偏离）；批内显式 `shared._notify()` 因 `_batch_depth>0` 实际是 no-op（真正的通知由 `end_batch` 补发），代码意图误导。共享模型内部重构会静默破坏 Keyframes/Spline 两个面板的选中同步。
  - 建议: 改用 `shared.set_keyframes(pairs)`；若需 rows 联动属真实需求，在 AnimationSelectionModel 增加公共方法。
  - 置信度: 高

- [AUD-10907] [Major] 曲线树 hover 索引按未过滤全集计算，与 paintEvent 的过滤子集不一致
  - 位置: src/features/composite/ui/spline_panel.py:118-127（对照 74 行 `rows = self._filtered_curves()`）
  - 证据:
    ```python
        def mouseMoveEvent(self, event):
            """Track the hovered row so paintEvent can highlight it."""
            y = int(event.position().y())
            row_h = 22
            idx = y // row_h
            if not (0 <= idx < len(self._model.curves)):
                idx = None
    ```
  - 影响: 输入过滤词后，悬停高亮按全集行号绘制而列表按过滤子集绘制 → 高亮带落在错误曲线行（或残留旧位），与点击行为（143 行正确用过滤列表）不一致。
  - 建议: `mouseMoveEvent` 改用 `len(self._filtered_curves())`。
  - 置信度: 高

- [AUD-10908] [Suggestion] 工具栏切线按钮只作用于第一个选中 key，与右键菜单"作用于全部选中"不一致
  - 位置: src/features/composite/ui/spline_panel.py:666-676（对照 spline_view.py:1224-1229）
  - 证据:
    ```python
        def _set_tangent(self, mode: str) -> None:
            """Apply a tangent mode to the selected key of the selected curve."""
            for c in self._model.curves:
                if not c.selected:
                    continue
                for i, k in enumerate(c.keys):
                    if k.selected:
                        set_key_tangent_mode(self._view, c, i, mode)
    ```
  - 影响: 多选 key 时工具栏按钮只改第一条选中曲线的第一个 key，视图右键菜单 `_set_selected_tangent` 却改全部——同一操作两条入口行为不同。
  - 建议: 复用 `_set_selected_tangent(self._view, mode)` 或遍历全部选中 key。
  - 置信度: 高

- [AUD-10909] [Suggestion] 多处公开/槽方法参数缺类型注解
  - 位置: src/features/composite/ui/spline_panel.py:427,557,587
  - 证据:
    ```python
        def _on_selected_key_changed(self, payload) -> None:
        ...
        def set_node_graph(self, node_graph) -> None:
        ...
        def attach_to_shared_selection(self, selection_model) -> None:
    ```
  - 影响: 违反项目强类型约束；payload 结构（Optional[Tuple[int, float]]）只能靠读发射端才知道。
  - 建议: 补齐注解；selection_model 用 `"AnimationSelectionModel"` 延迟引用。
  - 置信度: 高

### 通过项
- 信号防递归：`set_playing_state` 用 blockSignals 程序化更新播放按钮；`_syncing_shared_selection` 再入防护覆盖正/反向同步，Keyframes↔Spline 选择环路闭合无死循环
- `set_playback_controller` 先断旧槽再连新槽，无重复连接
- 线程安全：全部 UI 线程；`_play_timer` 为 UI 对象成员
- 无 OpenGL 调用（铁律「UI 线程不得操作 OpenGL」不涉及）
- showEvent 重定向启发式有 `_initial_show_done` + `_dock_hosted` 双保险，避免启动期误触发 Graph 重定向
- `set_node_graph` 正确保留视口与选中曲线（Batch 7 修复）——该路径无问题（问题在树交互路径，见 AUD-10904）

---

## src/features/composite/ui/spline_view.py (行数: 1543)

### 发现
- [AUD-10910] [Critical] 右键菜单 Delete/Select all 直接 connect 未绑定模块函数，`triggered(bool)` 把 False 当 self → AttributeError → PyQt6 槽内未处理异常终止进程
  - 位置: src/features/composite/ui/spline_view.py:1310-1311,1348-1349（函数定义 1021-1028,1065-1074）
  - 证据:
    ```python
        act_del = menu.addAction(I18N.tr("keyframes.delete_kf",
                                         "Delete keyframes"))
        act_del.triggered.connect(_delete_selected_keys)
    ...
        act_all = menu.addAction(I18N.tr("keyframes.select_all", "Select all"))
        act_all.triggered.connect(_select_all_keys)
    ```
    被连接的是模块级函数 `def _delete_selected_keys(self):` / `def _select_all_keys(self):`（arity=1），`QAction.triggered(bool)` 的 checked=False 会作为唯一实参传入 → `self=False` → `self._model` 立即 AttributeError。
  - 影响: PyQt6 对槽内未处理异常的默认行为是打印回溯后 abort 终止应用。右键菜单"Delete keyframes"（有选中 key 时可用）或"Select all"一点即崩。Spline 面板在生产中接线（main_window_adapter.py:184-205 创建 dock），用户可达。同文件其他菜单项用 `lambda: fn(self)` 是正确写法，仅这两处遗漏。
  - 建议: 改为 `lambda: _delete_selected_keys(self)`、`lambda: _select_all_keys(self)`（与 1301/1303/1307 行一致）。
  - 置信度: 高

- [AUD-10911] [Major] `_do_multi_key_drag` 的 `sel_set` 基于按下时刻帧快照，首个移动事件后键不再匹配 → 多选拖拽冻结在第一帧增量
  - 位置: src/features/composite/ui/spline_view.py:887-899
  - 证据:
    ```python
        for cc, ki, f0, v0, hl0, hr0 in self._multi_drag_origins:
            by_curve.setdefault(id(cc), (cc, {}))[1][f0] = (ki, v0, hl0, hr0)
        for cc, origin_map in by_curve.values():
            keys = sorted(cc.keys, key=lambda k: k.frame)
            sel_frames = sorted(origin_map.keys())
            sel_set = set(sel_frames)
            for k in keys:
                if k.frame not in sel_set:
                    continue
    ```
  - 影响: origins 快照 f0 为按下时帧号。第一个产生非零 `dframe` 的 mouseMove 把 `k.frame` 移离快照值后，后续所有 mouseMove 中 `k.frame not in sel_set` 恒成立 → 键被 continue 跳过。多选拖拽只生效一次（水平方向），之后不再跟随鼠标，松手提交滞后位置（若下一 tick 侥幸命中还会因 `k.frame + 总delta` 双重计数）。
  - 建议: 以 id(key) 或原索引标记归属（而非"当前帧在快照集合中"），或每 tick 先按 origins 重置再应用总 delta。
  - 置信度: 高

- [AUD-10912] [Major] `_nudge_selected_keys` 正向 nudge 升序边遍历边改帧，相邻多选键右移被撕裂
  - 位置: src/features/composite/ui/spline_view.py:1180-1199
  - 证据:
    ```python
        for c in self._model.visible_curves():
            keys = sorted(c.keys, key=lambda k: k.frame)
            for i, k in enumerate(keys):
                if not k.selected:
                    continue
                lo = keys[i - 1].frame + 1 if i > 0 else 0
                hi = (keys[i + 1].frame - 1 if i + 1 < len(keys)
                      else 10 ** 9)
                new_frame = max(lo, min(hi, k.frame + dframe))
    ```
  - 影响: 注释宣称 "Collect first, mutate after"，实现却在升序遍历中即时改 `k.frame`。右移（dframe>0）时前键被尚未移动的右邻钳住不动、后键照常右移：相邻选中键 (10,11) 右移一步得 (10,12)，块被撕开而非整体平移（左移顺序恰好正确，掩盖了问题）。
  - 建议: dframe>0 时降序遍历，或先统一计算全部目标帧再原子应用。
  - 置信度: 高

- [AUD-10913] [Major] 键垂直拖动/改值时手柄 y 不随值平移（x 平移、y 不平移），贝塞尔曲线畸变
  - 位置: src/features/composite/ui/spline_view.py:939-943（同型：907-909、1456-1458）
  - 证据:
    ```python
        delta_frame = new_frame - key.frame
        key.frame = new_frame
        key.value = _clampf(new_value, self._drag_curve.min_value, self._drag_curve.max_value)
        key.handle_left = QPointF(key.handle_left.x() + delta_frame, key.handle_left.y())
        key.handle_right = QPointF(key.handle_right.x() + delta_frame, key.handle_right.y())
    ```
  - 影响: 手柄是数据空间绝对坐标。水平拖动时手柄 x 随键平移（意图=手柄跟随键移动），但 value 变化后手柄 y 留在旧高度——只平移一半。对 broken/bezier 手工手柄的键，上下拖动后手柄不再贴合键，产生非预期过冲/扭结（auto/smooth 键因 rebuild 重算幸免）。`_do_multi_key_drag` 与 `_edit_key_dialog` 同型。
  - 建议: 手柄 y 同步加 `delta_value`（完整平移保持手柄形状），或手柄改存相对键的偏移。
  - 置信度: 中

- [AUD-10914] [Major] `_select_only_key` 不清除其他曲线的 `selected`，旧曲线残留选中态且 `curve_selection_changed` 发出旧曲线 → Keyframes 高亮错误参数行
  - 位置: src/features/composite/ui/spline_view.py:759-765,979-986,994-1002
  - 证据:
    ```python
                if not k.selected:
                    # Clicking an unselected key → becomes the sole selection.
                    c.selected = True
                    self._select_only_key(c, i)
    ...
    def _select_only_key(self, curve, key_idx):
        for c in self._model.visible_curves():
            for k in c.keys:
                k.selected = False
        curve.keys[key_idx].selected = True
        _emit_curve_selection(self)
    ```
    `_emit_curve_selection` 取可见曲线中第一条 `c.selected` 的 curve_id。
  - 影响: 点击曲线 B 的键只清了所有"键"的选择、置 B 选中，却不清旧曲线 A 的 `curve.selected`：A 继续按选中曲线绘制手柄/键；且 `_emit_curve_selection` 发出列表中第一条选中（常为旧的 A）的 curve_id，T61 回同步使 Keyframes 面板高亮错误参数行。树点击是互斥单选，此处语义应一致。
  - 建议: `_select_only_key` 内同时将其他曲线 `selected = False`。
  - 置信度: 高

- [AUD-10915] [Suggestion] 右键同时用作 pan 起点与 context menu 触发源，平台相关冲突
  - 位置: src/features/composite/ui/spline_view.py:715-721,1294-1352
  - 证据:
    ```python
        if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton):
            sp = QPointF(event.position().x(), event.position().y())
            self._drag_mode = "pan"
    ...
        def _sv_contextMenuEvent(self, event):
            """Right-click menu (batch 2): edit ops, interpolation, tangent, view."""
    ```
  - 影响: macOS 上右键按下即触发 contextMenuEvent，右键 pan 不可用或拖动后弹出菜单；Windows/Linux 上右键拖动后松手弹菜单，行为不一致。
  - 建议: pan 仅保留中键（AE 惯例），右键专用于菜单。
  - 置信度: 中

- [AUD-10916] [Suggestion] `_sv_mouseDoubleClickEvent` 中插入位置 `idx` 为死代码
  - 位置: src/features/composite/ui/spline_view.py:1406-1416
  - 证据:
    ```python
        idx = 0
        for j, k in enumerate(sorted(sel_curve.keys, key=lambda kk: kk.frame)):
            if frame < k.frame:
                idx = j
                break
        else:
            idx = len(sel_curve.keys)
        sel_curve.keys.append(nk)
        sel_curve.keys.sort(key=lambda kk: kk.frame)
        new_idx = next(ii for ii, kk in enumerate(sel_curve.keys)
                       if kk.frame == frame)
    ```
  - 影响: `idx` 计算后从未使用（实际用 append+sort+next 重定位），误导阅读者以为插入逻辑依赖它。
  - 建议: 删除 idx 循环，保留 sort+next。
  - 置信度: 高

- [AUD-10917] [Suggestion] `_draw_extrapolation` 死分支 `if ep.widthF() and False`，注释与代码不符
  - 位置: src/features/composite/ui/spline_view.py:549-553
  - 证据:
    ```python
        ep = QPen(color)
        ep.setStyle(Qt.PenStyle.DashLine)
        ep.setWidthF(1.0 if selected else 0.8)
        if ep.widthF() and False:  # keep alpha consistent with main curve
            pass
    ```
  - 影响: 永不执行的残留代码；注释宣称保持 alpha 一致但实际未做任何处理。
  - 建议: 删除该分支；如需 alpha 一致应对 color 做 setAlpha。
  - 置信度: 高

- [AUD-10918] [Suggestion] 模块尾部以 monkey-patch 方式绑定 24 个方法到 SplineView 类
  - 位置: src/features/composite/ui/spline_view.py:1463-1493
  - 证据:
    ```python
    # Bind interaction methods onto SplineView (kept out of the class body for
    # painting focus, but functionally identical to inline methods).
    SplineView._begin_key_drag = _begin_key_drag
    ...
    SplineView.mousePressEvent = _sv_mousePressEvent
    SplineView.contextMenuEvent = _sv_contextMenuEvent
    ```
  - 影响: 类型检查器/IDE 看不到这些成员（违反 .clinerules 强类型约束）；模块函数与方法的 self 语义混用正是 AUD-10910 崩溃的温床；绑定顺序还要求模块尾执行先于任何实例化。
  - 建议: 移回类体定义；或至少补 `if TYPE_CHECKING:` 存根声明。
  - 置信度: 高

### 通过项
- GPU 资源生命周期 / GLSL·HDR 维度不适用（QPainter 自绘控件，无 ModernGL、无着色器）
- 线程安全：仅 UI 线程交互，无跨线程状态
- viewport 数学：`expand_degenerate` 防除零；`_zoom_at` 因子钳制 [0.05,20] 防范围反转/塌缩；`_pan_by`/锚点平移正确
- 拷贝/粘贴：按 rel_frame 重锚、同帧替换、目标曲线缺失跳过、按曲线逐条 emit 提交（CompositePage 持久化+undo）
- `_edit_key_dialog` 帧钳制到邻域、值钳制到 [min,max]；`paste_keys_at_frame` 对外部数据 try/except 窄校验
- 剪贴板/删除/全选键盘路径（`_sv_keyPressEvent`）以正确的 `self` 直接调用，无 AUD-10910 问题
- snap 逻辑按屏幕像素判定、Shift 精确模式、拖拽结束清理参考线
- `set_key_tangent_mode` 六种模式的手柄重算正确，改后经 `_commit_key_edit` 提交
