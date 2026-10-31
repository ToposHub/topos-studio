# batch-102 — src/features/composite/ui//

任务书：docs/audit/2026-09-28/assignments/batch-102.md（编号区间 AUD-10200–AUD-10299）
审查对象：`src/features/composite/ui/keyframes_timeline_view.py`（1,880 行，全文通读）
交叉验证：`keyframes_model.py`（tracks()/visible_rows()/FrameMapper 语义）、`keyframes_panel.py`（range_changed/track_changed 宿主槽）、`timeline_layout.py`、`src/shared/ui/style_sheet.py`（全部颜色 token 均存在）。

## src/features/composite/ui/keyframes_timeline_view.py (行数: 1880)

### 发现

- [AUD-10200] [Major] 单击（无拖动）范围条即提交 range 编辑，把默认图层条固化为显式 time_range 并产生多余 undo 记录
  - 位置: src/features/composite/ui/keyframes_timeline_view.py:738-739, 1240-1256
  - 证据:
    ```python
    elif self._drag_mode in ("keyframe", "range_edge", "range_body"):
        self._commit_drag_edit()
    ...
    if self._drag_mode in ("range_edge", "range_body") and track.range_bar:
        ...
        self.range_changed.emit(row.node_id, {
            "comp_start": track.range_bar.start_frame,
            "comp_end": track.range_bar.end_frame,
            "enabled": True,
            "edit": edit,
        })
    ```
  - 影响: `_commit_drag_edit` 对 range 拖拽没有"是否移动过"的判定（对比 keyframe 分支用 `_drag_affected_rows` 过滤无操作行）。在范围条上按下后原地释放（一次普通单击）也会发射 `range_changed`，且 `enabled: True` 会把 AE 风格 default 图层条物化为显式 time_range；宿主 `keyframes_panel._on_timeline_range_changed` 无去重，直接上抛 CompositePage 写节点并推 undo。用户 merely 点击图层条就会污染文档历史。
  - 建议: 在 `_do_range_edge_drag/_do_range_body_drag` 中记录"本次手势是否实际改值"（如比较 press 时的 start/end），仅在变化时于 release 提交。
  - 置信度: 高

- [AUD-10201] [Major] dropEvent 用宽泛 `except Exception` 静默吞掉媒体拖放处理器的一切错误
  - 位置: src/features/composite/ui/keyframes_timeline_view.py:160-163
  - 证据:
    ```python
    try:
        handled = bool(handler(event.mimeData(), float(pos.y())))
    except Exception:
        handled = False
    ```
  - 影响: 违反项目铁律"不得宽泛 except 掩盖领域错误"。MediaPool drop 处理器内的任何 bug（类型错误、图写入失败等）都被无声丢弃，drop 表现为"没反应"，无日志、无告警，问题不可诊断。
  - 建议: 捕获具体异常类型并记录日志（项目 logger），或至少在 except 中输出异常信息再置 `handled = False`。
  - 置信度: 高

- [AUD-10202] [Major] 双击添加关键帧绕过行锁定（P9），且双击已有菱形会重复发射未变化的 track
  - 位置: src/features/composite/ui/keyframes_timeline_view.py:763-776, 1422-1427
  - 证据:
    ```python
    hit = self._hit_param_row(float(pos.y()))
    if hit is None:
        return
    row_id = hit
    row = self._row_for_id(row_id)
    if row is None or row.row_type != "param":
        return
    frame = int(round(self._fm.x_to_frame(float(pos.x()), _ORIGIN_X)))
    self._add_keyframe(row.node_id, row.param_name, frame)
    ...
    def _hit_param_row(self, y: float) -> Optional[str]:
        idx = self._row_index_at_y(y)
        ...
        return rows[idx].row_id
    ```
  - 影响: 单击路径 `_hit_keyframe`/`_hit_range_bar`（1370-1371、1394-1395）都检查 `getattr(row, "locked", False)` 并拒绝拾取，`_hit_param_row` 没有该检查——锁定的 param 行仍可双击写入关键帧，锁定约束被绕过。另外双击命中已有菱形时 `add_keyframe_at` 走 existing 分支、value_hint 为 None，仍无条件 `track_changed.emit(...)` 未变化数据（1661-1667），产生一次空操作 undo 记录。
  - 建议: `_hit_param_row` 增加 locked 检查（或 `mouseDoubleClickEvent` 中校验 `row.locked`）；`add_keyframe_at` existing 分支在值未变化时跳过 emit。
  - 置信度: 高（绕锁） / 中（空 undo 记录，取决于宿主是否去重）

- [AUD-10203] [Major] Alt+单击（未拖动）提交同帧重复关键帧，破坏"一轨一帧"不变量
  - 位置: src/features/composite/ui/keyframes_timeline_view.py:1080-1082, 1106-1123, 1146-1148, 1268-1276
  - 证据:
    ```python
    if self._drag_copy:
        self._duplicate_selected_in_place()
    ...
    for src in selected:
        dup = KeyframePointVisual(
            frame=src.frame, ... selected=True, ...)
        track.keyframes.append(dup)
    ...
    delta = cur_frame - self._multi_drag_press_frame
    if delta == 0:
        return
    ...
    for row_id in sorted(self._drag_affected_rows):
        ...
        pending.append((row.node_id, row.param_name, kfs))
    self._drag_affected_rows = set()
    self._emit_track_batch("drag", pending)
    ```
  - 影响: 复制在 press 时就写入可视模型并把行加入 `_drag_affected_rows`；`_do_multi_keyframe_drag` 在 delta==0 时提前返回（不移动），但 release 时 `_commit_drag_edit` 仍会把含"同帧两条关键帧"的轨道整体 emit 给宿主写入节点图。用户只是 Alt+点了一下（没有拖动）就得到重复关键帧 + undo 记录，下游插值/求值对同帧重复点的行为未定义。
  - 建议: `_duplicate_selected_in_place` 延迟到首个非零 delta 再执行（惰性复制），或在 commit 时检测"副本仍在源帧"则回滚副本。
  - 置信度: 高

- [AUD-10204] [Major] scale/interpolation 对隐藏行（折叠、过滤后不可见）先改模型后跳过提交，编辑静默丢失
  - 位置: src/features/composite/ui/keyframes_timeline_view.py:1546-1561, 1849-1866, 1354-1358
  - 证据:
    ```python
    for row_id, track in pending_rows.items():
        for point in track.keyframes:
            new_frame = proposed.get((row_id, int(point.frame)))
            if new_frame is not None:
                point.frame = new_frame
        track.keyframes.sort(key=lambda point: point.frame)

    pending = []
    for row_id, track in pending_rows.items():
        row = self._row_for_id(row_id)
        if row is None or row.row_type != "param":
            continue
    ...
    def _row_for_id(self, row_id: str):
        for r in self._model.visible_rows():
            if r.row_id == row_id:
                return r
    ```
    （`set_selected_interpolation` 1849-1860 同型：先改 `kf.interpolation` 再用 `_row_for_id` 过滤）
  - 影响: `self._model.tracks()` 含所有轨道（含 `visible_rows()` 因折叠/过滤排除的行），而 `_row_for_id` 只在可见行里找。选中（如 Ctrl+A 全选后折叠节点、或过滤切换）的隐藏行会被就地改帧/改插值，但不进入 pending、不 emit——可视模型与节点图分叉，下次 rebuild 静默还原，用户看到"改了又弹回"。对比 `_delete_selected_keyframes`（1739-1742 先跳过再改）顺序正确。
  - 建议: 两处都改为先解析 row、仅对可提交行计算并应用变更；或隐藏行直接排除在候选之外。
  - 置信度: 中（需要"选中后行被隐藏"的触发序列，但代码路径确定存在）

- [AUD-10205] [Suggestion] Left/Right 步进未钳到非负，frame 0 处按 Left 发出 current_frame_changed(-1)
  - 位置: src/features/composite/ui/keyframes_timeline_view.py:919-930
  - 证据:
    ```python
    if key == Qt.Key.Key_Left:
        new_frame = self._current_frame - step
        self.set_current_frame(new_frame)
        self.current_frame_changed.emit(new_frame)
    ```
  - 影响: 本视图其余域均为非负（关键帧 `max(0, ...)`、标尺标签 clamp 到 0、workarea start ≥ 0），-1 帧落在无刻度区域；面板侧 `seek_to_frame` 有钳制但此直发路径绕过它，宿主若不钳制会 seek 到 -1。
  - 建议: `new_frame = max(0, ...)`，或统一委托面板 `seek_to_frame`。
  - 置信度: 中

- [AUD-10206] [Suggestion] 标尺普通单击会双次发射 current_frame_changed（press 一次 + release 强制一次）
  - 位置: src/features/composite/ui/keyframes_timeline_view.py:664-666, 962-963, 740-749
  - 证据:
    ```python
    if y <= _RULER_H:
        self._start_drag("playhead", x)   # press 即 _do_playhead_drag(x) → emit
        return
    ...
    elif self._drag_mode == "playhead":
        ...
        self._do_playhead_drag(float(event.position().x()), force=True)  # release 再 emit
    ```
  - 影响: T48 注释声称"释放后只发布一次"，但 press 时已发布过一次；同帧双发会让宿主收到两次相同 seek，若渲染层无合并则多一次重绘请求。
  - 建议: press 时只记录位置不发射（或 release 时值未变则跳过强制发射）。
  - 置信度: 中

- [AUD-10207] [Suggestion] update_keyframe_at 不校验 interpolation 合法性
  - 位置: src/features/composite/ui/keyframes_timeline_view.py:1723-1725
  - 证据:
    ```python
    point.frame = new_frame
    point.value = value
    point.interpolation = str(interpolation or point.interpolation)
    ```
  - 影响: 同文件 `set_selected_interpolation`（1847）用 `_INTERPOLATIONS` 白名单校验，此内联编辑入口接受任意字符串并写入节点轨道，未知模式会持久化到图数据。
  - 建议: 与 `_INTERPOLATIONS` 集合比对，非法值回退原值或返回 False。
  - 置信度: 高

- [AUD-10208] [Suggestion] work-area 拖拽每次 mouseMove 无节流发射 play_range_changed
  - 位置: src/features/composite/ui/keyframes_timeline_view.py:1312-1340
  - 证据:
    ```python
    if (start, end) == self._play_range:
        return
    self._play_range = (start, end)
    self.update()
    self.play_range_changed.emit(start, end)
    ```
  - 影响: 播放头拖拽专门做了 `_SEEK_THROTTLE_MS` ≈60fps 节流（T48），work-area 拖边/拖体却逐 move 上抛面板并逐级转发到 CompositePage，高频鼠标下产生不必要的信号风暴。
  - 建议: 复用 `_drag_last_emit` 节流（release 时 force），或仅在值稳定后发布。
  - 置信度: 中

- [AUD-10209] [Suggestion] 多个公开/内部方法缺少类型注解
  - 位置: src/features/composite/ui/keyframes_timeline_view.py:122-124, 647, 709, 733, 1354-1358, 1360-1362, 1384-1386, 493-494
  - 证据:
    ```python
    def set_media_drop_handler(self, handler) -> None:
    def mousePressEvent(self, event) -> None:
    def _row_for_id(self, row_id: str):
    def _hit_keyframe(self, x: float, y: float):
    def _draw_row(self, p: QPainter, row, track: Optional[KeyframesTrackVisual],
                  y: int, w: int) -> None:
    ```
  - 影响: 违反项目强类型约定（.clinerules：所有函数有类型注解）；`_row_for_id`/`_hit_keyframe`/`_hit_range_bar` 的返回结构（Optional[KeyframesRow] / Optional[Tuple[str, int]]）只能靠读实现才能得知。
  - 建议: 补齐 `handler: Callable[[Any, float], bool]`、`event: QMouseEvent`、`row: KeyframesRow` 及各返回类型。
  - 置信度: 高

- [AUD-10210] [Suggestion] 死代码、冗余导入与重复的 row_id 格式串
  - 位置: src/features/composite/ui/keyframes_timeline_view.py:707, 1054-1056, 1105, 1657, 1653/1689/1710, 317-319
  - 证据:
    ```python
    self._start_drag("playhead", x)          # 707: 不可达——y<=RULER_H 与 y>RULER_H 各分支均已 return
    ...
    def _do_keyframe_drag(self, x: float) -> None:   # 1054: 无调用方的 legacy shim
        """Legacy single-keyframe drag shim — forwards to the multi drag."""
    ...
    from .keyframes_model import KeyframePointVisual   # 1105/1657: 模块顶部 26-29 已导入
    ...
    row_id = f"param:{node_id}:{param_name}"           # 1653/1689/1710 三处手写
    ```
  - 影响: 死分支与未用 shim 增加维护噪音；row_id 格式与 `keyframes_model` 的 `f"param:{node.id}:{pname}"` 是隐式契约，模型侧改动会让这三处静默失配（`track_for` 返回 None → 静默 no-op）。另：模块 docstring 宣称标尺"supports negative"（第 4 行），实现却把 start_frame 钳到 0（318-319），文档与行为漂移。
  - 建议: 删除 707 与 `_do_keyframe_drag`，去掉局部重复导入，row_id 构造收敛为模型提供的 helper，修正 docstring。
  - 置信度: 高

### 通过项
- 线程安全：纯 QWidget，仅在 UI 线程访问模型与 FrameMapper，无 OpenGL/渲染线程交互，无跨线程数据竞争面。
- GPU 资源生命周期：不涉及 ModernGL/纹理/缓冲，无资源泄漏面。
- GLSL·HDR 正确性：不涉及着色器与 HDR 数据通路（`KeyframePointVisual.value` 原样透传，无 clamp）。
- 信号防递归（主体）：多轨提交先快照后发射（`_commit_drag_edit` 1262-1276、`paste_keyframes` 1830-1838 注释明确）；`_emit_track_batch` 用 try/finally 保证 about_to_commit/committed 配对；`_set_display_mode` 与面板 `set_display` 无回环。
- 性能（paint）：标尺刻度步长按 1-2-5 自适应（`_tick_steps`），帧循环有界；行绘制有视口剔除（281-282）；关键帧绘制有 ±10px 屏幕外剔除（578-579）。
- 异常与取消路径（部分）：`scale_selected_keyframes` 对 factor 做 isfinite/>0 校验（1503-1509）；`wheelEvent` 对 delta==0 / Alt 冒泡处理正确；contextMenu 各动作引用均在定义分支内。
- 输入验证（部分）：关键帧多选拖拽有 all-or-nothing 冲突守卫（1149-1166），帧钳 `max(0, ...)`；视频条拖拽钳到源长 `[0, total-1]`（1196-1205、1222-1225）；`FrameMapper.set_zoom` 双端钳制（0.05–256）防退化，`_tick_steps` 对 ppf 取 `max(...,1e-6)` 防 log10 域错误。
- 持久化一致性（部分）：copy/paste 复用 `to_keyframe_dict()` 保留 handle/tangent extra 字段（P1-1），paste 前快照避免宿主同步重建丢数据。
