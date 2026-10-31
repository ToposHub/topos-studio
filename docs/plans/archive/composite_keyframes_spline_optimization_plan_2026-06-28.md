# Composite Keyframes / Spline 完成度审计与优化计划

审计日期：2026-06-28  
审计范围：Composite 关键帧数据系统、Keyframes 面板、Spline 面板、保存/加载、渲染求值、缓存、测试覆盖。

## 0. 当前结论

当前项目已经完成了相当多基础设施，不是空壳：

- 节点实例级 `keyframe_tracks` 已建立，避免了旧方案污染同类型所有节点。
- `Node.to_dict/from_dict`、`NodeGraph.snapshot()`、graph migration 已覆盖关键帧、`time_range`、节点颜色。
- 渲染 evaluator 已有 `_interpolate_parameters()`，能按 `GraphSnapshot.frame_number` 计算关键帧值。
- Keyframes 面板已注册为 Composite 底部 dock，并具备基础绘制、节点/参数行、关键帧点、range bar、拖动、双击加点、Delete 删除。
- Spline 面板已注册为 dock，并具备基础曲线绘制、贝塞尔采样、点/手柄拖拽、切换 tangent mode 的初版。
- 关键测试目前通过：关键帧模型/保存加载 84 个，Keyframes/Spline UI 与数学 78 个，缓存 111 个。

但仍存在一批会影响真实使用的高优先级问题。最重要的是：当前帧变化没有完整驱动预览重渲染和缓存失效；Spline 里看到/拖动的贝塞尔曲线与渲染时实际插值曲线可能不一致；undo/redo 基本还是占位；`time_range` 目前主要是 UI/导出范围信息，还没有真正控制节点在某帧是否生效。

本计划按“先修关键帧系统，再补 UI 工具”的顺序执行。

## 1. 审计时已运行的验证

```bash
.venv/bin/python -m pytest tests/project/test_keyframe.py tests/composite/test_composite_keyframes_serialization.py tests/composite/test_composite_keyframe_evaluation.py tests/composite/test_composite_keyframe_frame_number.py tests/composite/test_composite_keyframes_node_color.py tests/composite/test_composite_keyframes_save_load.py tests/composite/test_composite_keyframes_model.py tests/composite/test_composite_keyframes_perf.py -q
```

结果：84 passed。

```bash
.venv/bin/python -m pytest tests/ui/test_composite_keyframes_dock.py tests/ui/test_composite_keyframes_panel.py tests/ui/test_composite_keyframes_interaction.py tests/ui/test_composite_keyframes_polish.py tests/composite/test_spline_model.py tests/composite/test_spline_bezier_math.py tests/ui/test_spline_view_render.py tests/ui/test_spline_view_hit_test.py tests/ui/test_spline_panel.py -q
```

结果：78 passed。

```bash
.venv/bin/python -m pytest tests/composite/test_composite_cache_key.py tests/composite/test_cache_strategy.py tests/composite/test_composite_cache.py -q
```

结果：111 passed。

```bash
.venv/bin/python -m py_compile src/shared/keyframe.py src/features/composite/nodes/base_node.py src/features/composite/core/node_engine.py src/features/composite/core/graph_snapshot.py src/features/composite/evaluators/base_evaluator.py src/features/composite/render/cache_manager.py src/features/composite/render/composite_render_output.py src/features/composite/ui/keyframes_model.py src/features/composite/ui/keyframes_panel.py src/features/composite/ui/keyframes_timeline_view.py src/features/composite/ui/keyframes_tree_view.py src/features/composite/ui/spline_model.py src/features/composite/ui/spline_view.py src/features/composite/ui/spline_panel.py src/features/composite/ui/spline_commands.py src/features/composite/ui/composite_page.py src/features/composite/ui/node_properties_panel.py
git diff --check
```

结果：通过。

## 2. P0 必修问题：播放帧、预览、缓存失效

### P0-1：Keyframes/Spline 播放头移动后，预览不一定重渲染

现状：

- `KeyframesPanel.current_frame_changed` 和 `SplinePanel.current_frame_changed` 只连接到 `CompositePage.set_composite_current_frame()`。
- `set_composite_current_frame()` 只更新 `_composite_current_frame` 和属性面板 timeline state，没有调用 `_evaluate_graph()`。
- Keyframes 面板内部拖动 playhead 时，面板自己的红线会动，但 Composite viewer 可能不会立即显示该帧的关键帧结果。

涉及文件：

- `src/features/composite/ui/composite_page.py`
- `src/features/composite/ui/keyframes_panel.py`
- `src/features/composite/ui/spline_panel.py`

解决方法：

1. 修改 `CompositePage.set_composite_current_frame(frame)`：
   - 使用 `frame = int(frame or 0)`。
   - 若 frame 改变，更新 `_composite_current_frame`。
   - 同步 `NodePropertiesPanel.set_timeline_state()`。
   - 同步 `KeyframesPanel.set_current_frame(frame)`，但要避免从 panel 发出的信号再次递归触发。
   - 同步 `SplinePanel.set_current_frame(frame)`。
   - 调用 `_evaluate_graph()`，让 viewer 立刻请求当前帧。
2. 增加内部防递归标记，例如 `_syncing_composite_frame = True`：
   - panel 发出 frame changed → page 更新 → page 回写 panel 时不要重复 emit。
3. `KeyframesPanel._goto_prev_keyframe()`、`_goto_next_keyframe()` 当前只调用 `set_current_frame(frame)`，需要同时 emit `current_frame_changed(frame)`，否则工具栏上一帧/下一关键帧按钮不会驱动 viewer。
4. Spline 面板增加 playhead 交互后，也走同一条 `current_frame_changed` 链路。

必须新增测试：

- `tests/ui/test_composite_keyframe_preview_seek.py`
- 覆盖：调用 `CompositePage.set_composite_current_frame(10)` 后 `_evaluate_graph()` 被调用一次。
- 覆盖：Keyframes panel 发出 `current_frame_changed(10)` 后 page 的 `_composite_current_frame == 10`，Keyframes/Spline/Properties 三处 UI 状态同步。
- 覆盖：`_goto_next_keyframe()` 会 emit seek 信号。

### P0-2：静态素材上的关键帧动画可能被缓存复用，导致后续帧不更新

现状：

- `CacheManager.compute_key()` 支持 `frame_number`。
- 但 `GraphEvaluatorMixin._evaluate_snapshot()` 调用 `cache_mgr.compute_key(...)` 时没有传 `frame_number=snapshot.frame_number`。
- `RenderScheduler._compute_dirty_nodes()` 没有记录上一帧 frame，也没有在 `snapshot.frame_number` 改变时把带关键帧的节点标为 dirty。
- 结果：静态图 + Transform 关键帧这类场景，第一帧算完后，下一帧可能直接拿旧缓存。

涉及文件：

- `src/features/composite/evaluators/base_evaluator.py`
- `src/features/composite/render/render_scheduler.py`
- `src/features/composite/render/cache_manager.py`

解决方法：

1. 在 `RenderScheduler.__init__()` 增加：
   - `self._prev_frame_number: Optional[int] = None`
2. 在 `_compute_dirty_nodes(snapshot, node_plans)` 中：
   - 检查 `frame_changed = self._prev_frame_number is not None and snapshot.frame_number != self._prev_frame_number`。
   - 如果 frame 改变，只把真正时间相关的节点标 dirty：
     - `ns.keyframe_tracks` 非空的节点。
     - `requires_frame_number(ns.node_type)` 的节点。
     - `DYNAMIC_SOURCE_NODE_TYPES` 仍沿用已有逻辑。
   - 然后通过现有 BFS 向下游传播 dirty。
3. 在 `_update_tracking()`、`reset_dirty_tracking()`、`seed_from()` 中维护 `_prev_frame_number`。
4. 在 `GraphEvaluatorMixin._evaluate_snapshot()` 调用 `cache_mgr.compute_key()` 时传：
   - `frame_number=snapshot.frame_number`
5. 为避免静态非动画节点缓存因为帧号变化全部失效，可以做一个 helper：
   - `_node_needs_frame_cache_key(ns)` 返回 `True` 当 `ns.keyframe_tracks` 非空、节点类型 requires frame、或属于动态源下游。
   - 第一版可先全量传 `snapshot.frame_number` 保证正确，再做性能优化。

必须新增测试：

- `tests/composite/test_composite_keyframe_cache_invalidation.py`
- 构造静态 source → transform(keyframed) → output。
- 第一次 snapshot frame 0 渲染后，再用 frame 10 生成 plan，断言 transform/output 在 dirty 集合里。
- monkeypatch `CacheManager.compute_key`，断言 evaluator 传入了 `frame_number=snapshot.frame_number`。
- 断言无关键帧的静态图在 frame 变化时不被误标 dirty。

### P0-3：`_current_keyframe_frame()` 对第 0 帧判断错误

现状：

- 代码使用 `if local:` / `if tf:` 判断 runtime_context 的帧号。
- 当 `clip_local_frame == 0` 或 `timeline_frame == 0` 时会被当成 False，然后落到后续来源，可能返回错误帧。

涉及文件：

- `src/features/composite/ui/composite_page.py`

解决方法：

1. 改成显式 None 判断：
   - `if local is not None: return int(local)`
   - `if tf is not None: return int(tf)`
2. `_total_frames_for_node()` 也检查是否有类似 truthy duration 判断，0 和 None 要分开。

必须新增测试：

- runtime_context.clip_local_frame = 0 时，`_current_keyframe_frame()` 必须返回 0。
- runtime_context.timeline_frame = 0 且 `_composite_current_frame = 50` 时，也必须返回 0。

## 3. P1 必修问题：Spline 曲线与实际渲染插值不一致

### P1-1：当前 `Keyframe.bezier_in/out` 格式与 Spline 手柄格式不统一

现状：

- `src/shared/keyframe.py` 里 `Keyframe.bezier_in/out` 注释是归一化控制点。
- `SplineModel` 把 `bezier_in/out` 当作“相对当前 key 的数据空间 delta”使用。
- `SplineKey.to_dict()` 又把绝对 handle 转成 delta 写回 `bezier_in/out`。
- `SplineModel._build_keys()` 会对所有 key 调用 `apply_auto_tangent()`，可能覆盖保存下来的手柄。

风险：

- Spline 里拖出来的曲线与 evaluator 实际插值不一致。
- 关闭重开项目后，曲线手柄可能被自动 tangent 重算，用户编辑丢失。
- Keyframes 面板移动点时会只保留 frame/value/interpolation，丢失贝塞尔手柄。

解决方法：

1. 定义唯一的磁盘格式，不再混用语义。建议新增字段：

```json
{
  "frame": 10,
  "value": 45.0,
  "interpolation": "bezier",
  "tangent_mode": "smooth",
  "handle_left": {"frame": 7.0, "value": 35.0},
  "handle_right": {"frame": 13.0, "value": 55.0}
}
```

2. `Keyframe` dataclass 增加：
   - `tangent_mode: str = "auto"`
   - `handle_left: Optional[Tuple[float, float]] = None`
   - `handle_right: Optional[Tuple[float, float]] = None`
3. `Keyframe.from_dict()` 兼容旧数据：
   - 如果有 `handle_left/right`，直接使用。
   - 如果只有旧 `bezier_in/out`，按旧规则转换成合理的 handle。
   - 如果都没有，则根据相邻 key 在构建 segment 时生成 auto handle，不写死到 key 上。
4. `SplineModel._build_keys()`：
   - 有保存手柄时必须保留。
   - 只有 `tangent_mode == "auto"` 且没有保存手柄时才调用 `apply_auto_tangent()`。
   - 不允许每次 rebuild 都覆盖用户手柄。
5. `SplineKey.to_dict()`：
   - 写入 `tangent_mode`、`handle_left/right`。
   - 旧 `bezier_in/out` 可暂时保留用于兼容，但不再作为主格式。
6. `KeyframesTimelineView` 移动关键帧时：
   - 必须保留所有未知字段。
   - 移动 frame 时同步平移该 key 的 `handle_left/right.frame`。
   - 不要只输出 `{frame, value, interpolation}`。

必须新增测试：

- Spline 手柄保存后 rebuild 不变。
- Keyframes 面板拖动 key 后，`handle_left/right` 仍存在且 frame delta 同步平移。
- 旧 `bezier_in/out` 数据能迁移到新 handle 字段。

### P1-2：渲染 evaluator 的 Bezier 计算忽略 x 控制点

现状：

- `src/shared/keyframe.py::bezier_interpolate()` 只使用控制点 y 值，没有根据当前 frame 解 cubic Bezier 的 x。
- Spline 视图绘制的是 2D cubic curve，包含 x/y 手柄。
- 因此 Spline 曲线形状和实际播放参数曲线可能不同。

解决方法：

1. 为每段 keyframe 构建数据空间 cubic：
   - `P0=(kf0.frame, kf0.value)`
   - `P1=kf0.handle_right`
   - `P2=kf1.handle_left`
   - `P3=(kf1.frame, kf1.value)`
2. 给定当前 `frame`，先解 `x(t)=frame`：
   - 因手柄 x 已被 clamp 到 `[kf0.frame, kf1.frame]`，x 单调。
   - 使用二分法 20 次即可稳定，避免 Newton 在平坦区域发散。
3. 用解出的 `t` 计算 `y(t)` 作为插值值。
4. 对 `linear/hold/ease_in/ease_out/ease_in_out` 保持现有行为，或把 ease 也转成标准手柄再走同一 cubic。
5. 结果仍按 `track.min_val/max_val` clamp。

必须新增测试：

- evaluator Bezier 与 Spline `sample_segment()` 在同一 frame 上给出同一 value。
- 非对称 x 手柄能改变时间分布。
- handle x 越界时被 clamp，不会产生倒退时间。
- hold/linear 不受影响。

## 4. P2 必修问题：Undo/Redo 目前不可依赖

### P2-1：关键帧、range bar、Spline 编辑没有完整 before/after undo

现状：

- `_on_keyframes_panel_track_changed()` 文档写了 push undo，但实际没有 push。
- `_on_keyframes_panel_time_range_changed()` 没有 undo。
- `_on_props_keyframes_changed()` push 了 undo，但捕获的是“已经改完之后”的 new_track。
- `_undo_keyframe_track()` 目前只是 `_evaluate_graph()`，没有恢复旧状态。
- `spline_commands.py` 有 command dataclass，但没有真正接入 CompositePage 的 undo 栈。

涉及文件：

- `src/features/composite/ui/composite_page.py`
- `src/features/composite/ui/node_properties_panel.py`
- `src/features/composite/ui/keyframes_timeline_view.py`
- `src/features/composite/ui/spline_view.py`
- `src/features/composite/ui/spline_commands.py`

解决方法：

1. 统一所有关键帧编辑入口：
   - Properties ◆ 编辑。
   - Keyframes 面板移动/新增/删除关键帧。
   - Spline 点/手柄/tangent mode 编辑。
2. 在 CompositePage 写入节点前捕获：
   - `old_track = deepcopy(node.keyframe_tracks.get(param_name))`
   - `new_track = deepcopy(...)`
3. 新建通用 command：

```python
@dataclass
class KeyframeTrackEdit:
    node_id: str
    param_name: str
    before: Optional[dict]
    after: Optional[dict]
```

4. undo 恢复 `before`，redo 恢复 `after`。
5. range bar 使用类似结构：

```python
@dataclass
class NodeTimeRangeEdit:
    node_id: str
    before: Optional[dict]
    after: Optional[dict]
```

6. Properties panel 当前会先写节点再发 signal。建议改成：
   - 如果已绑定 CompositePage/NodeGraph，由 CompositePage 作为唯一写入者。
   - Panel 只发出编辑 intent，包含 `param_name` 与 `keyframes`。
   - 或者 signal 增加 `before_track`，保证 CompositePage 能得到旧值。
7. Spline 拖动过程中只做本地视觉反馈；mouse release 时发送完整 track before/after，CompositePage 统一 push undo。

必须新增测试：

- Properties ◆ 添加关键帧后 undo 能删除 track，redo 能恢复。
- Keyframes 面板移动关键帧后 undo 恢复原 frame/value/handles。
- Spline 手柄拖动后 undo 恢复手柄坐标。
- range bar 拖动后 undo 恢复 `time_range`。

## 5. P3 必修问题：`time_range` 目前没有真正控制节点时间长度

### P3-1：Keyframes 面板 range bar 主要只是显示/保存，没有参与节点求值

现状：

- `Node.time_range` 会保存、snapshot、KeyframesModel 显示。
- `resolve_video_range()` 会用它推导导出长度。
- 但 evaluator 没有根据当前 frame 判断节点是否在 range 内，也没有 source frame remap。

风险：

- 用户在 Keyframes 面板里拖动节点长度，看起来像改变了素材长度，但实际播放时节点仍一直生效。

解决方法：

1. 明确定义 `time_range` 数据结构：

```json
{
  "enabled": true,
  "comp_start": 0,
  "comp_end": 120,
  "source_in": 0,
  "source_out": 120,
  "offset": 0,
  "outside_mode": "transparent"
}
```

2. 在 evaluator 创建临时节点或评估节点前增加 frame gate：
   - `current = snapshot.frame_number`
   - 如果 `time_range.enabled` 且 current 不在 `[comp_start, comp_end]`：
     - source 节点输出透明/空。
     - effect 节点根据节点类型选择 transparent、bypass 或 hold。
     - output 节点不 gate。
3. 对视频/序列源节点，把 comp frame 映射到 source frame：
   - `source_frame = source_in + (current - comp_start) + offset`
   - clamp 到 `[source_in, source_out]` 或按 outside/loop 模式处理。
4. KeyframesTimelineView range bar 编辑不能只提交 comp_start/end；需要保留原有 source_in/source_out/offset/enabled/outside_mode。
5. 如果当前阶段不实现 source remap，也必须在 UI 上禁用/隐藏 source range 编辑，避免用户误解。

必须新增测试：

- frame 在 range 外时节点输出为空/透明或 bypass，行为按节点类型固定。
- range bar 编辑后保存加载仍保留完整字段。
- `resolve_video_range()` 与 evaluator range gate 使用同一 inclusive end 规则。

## 6. P4 UI 完整度问题：Spline 工具还不够可用

### P4-1：Spline 左侧曲线树只有绘制，没有点击选择/显隐

现状：

- `SplineCurveTree` 声明了 `curve_visibility_toggled`、`curve_selected`。
- 但没有 mousePressEvent，用户不能点击曲线、不能切换显示。
- `SplinePanel.set_node_graph()` 默认只选择第一条曲线，多曲线编辑基本不可控。

解决方法：

1. 给 `SplineCurveTree` 加行命中测试：
   - 左侧 0-20px 是 checkbox/visibility。
   - 20px 之后点击选择曲线。
2. 绘制真实 checkbox：
   - visible = 勾选。
   - hidden = 空框，文字灰。
3. 连接 signal：
   - `curve_visibility_toggled` → `SplineModel.set_visible()` → `SplineView.refresh()`。
   - `curve_selected` → 设置单选/多选状态 → `SplineView.refresh()`。
4. 支持按节点分组：
   - 一级 node row，二级 param row。
   - 复用 KeyframesTreeView 的 row 高度、展开箭头、颜色点样式。
5. 增加 keyboard：
   - Space 切换曲线显隐。
   - Up/Down 切换曲线。

必须新增测试：

- 点击曲线行后 selected curve 改变。
- 点击 checkbox 区域后 visible 状态改变，SplineView 不再绘制该 curve。

### P4-2：Spline playhead 没有交互

现状：

- `SplineView` 会画红色 playhead。
- 但 mousePressEvent 背景点击默认开始 box select，没有 ruler/playhead drag 逻辑。

解决方法：

1. 在 SplineView 顶部 ruler 区域点击/拖动时：
   - 转换 screen x → frame。
   - 更新 `_current_frame`。
   - emit `current_frame_changed(frame)`。
2. 鼠标靠近 playhead 时 cursor 改成水平 resize/seek cursor。
3. 背景 box select 只在 plot 区域且不在 ruler 区触发。

必须新增测试：

- 在 Spline ruler 区 mousePress/move/release 后 emit current_frame_changed。
- 不影响 box select。

### P4-3：Keyframes 面板缺少真正的滚动条/完整 DaVinci-like 控件

现状：

- footer 有 pan/fit 和时间单位 combo，但没有实际 horizontal scrollbar。
- vertical scroll 只通过内部 `_scroll_y` 同步，目前缺少用户滚轮/滚动条入口。
- toolbar “more menu”等 DaVinci 风格按钮尚未完成。

解决方法：

1. 加 `QScrollBar(Qt.Horizontal)` 控制 `FrameMapper.scroll_offset`。
2. 加 `QScrollBar(Qt.Vertical)` 或统一 wheelEvent 驱动 tree/timeline `_scroll_y`。
3. Fit 后更新 scrollbar range/value。
4. 鼠标滚轮：
   - 普通滚轮：竖向滚动。
   - Shift + 滚轮：横向平移。
   - Ctrl/Cmd + 滚轮：缩放时间轴，以鼠标位置为 anchor。
5. toolbar 补齐：
   - fit selected / fit all。
   - show animated only。
   - collapse/expand。
   - more menu。

必须新增测试：

- wheelEvent 改变 `_scroll_y`，tree/timeline 同步。
- horizontal scrollbar 改变 frame_to_x 映射。
- zoom 以鼠标 frame 为中心，不让 playhead 跳走。

### P4-4：图标样式需要替换 Unicode lock

现状：

- `KeyframesTreeView._draw_lock()` 使用 Unicode `🔒/🔓`。
- 项目 UI 规范倾向使用 icon_manager/lucide/fontawesome 图标，不建议直接画 emoji。

解决方法：

1. 使用 `src.shared.assets.icon_manager.get_icon()` 获取 lock/unlock 图标。
2. 在 QWidget paint 中可缓存 QPixmap 后绘制。
3. 保持颜色来自全局样式 `C.TEXT_HINT` / `C.ORANGE`。

## 7. P5 同步与状态一致性问题

### P5-1：节点颜色变化只刷新 Keyframes，不刷新 Spline

现状：

- `_refresh_keyframes_for_color()` 只调用 Keyframes panel `set_node_graph()`。
- SplineModel 也依赖节点颜色，但颜色变化后不会同步刷新 Spline，除非之后发生完整 rebuild。

解决方法：

1. `_refresh_keyframes_for_color(node_id)` 改名为 `_refresh_animation_panels_for_node_style(node_id)`。
2. 同时刷新：
   - KeyframesPanel。
   - SplinePanel。
3. undo/redo 节点颜色时也走同一方法。

测试：

- 改节点颜色后，Spline curve color 立即变化。

### P5-2：`set_spline_panel()` 绑定后没有立即 seed 当前 graph

现状：

- `set_keyframes_panel()` 会调用 `_refresh_keyframes_panel()`。
- `set_spline_panel()` 只连接 signal，不主动 `panel.set_node_graph(self._node_graph)`。
- 运行时当前顺序通常不会立刻暴露，但单独重建 dock 或后创建 Spline dock 时可能空白。

解决方法：

1. 在 `set_spline_panel(panel)` 末尾调用：
   - `panel.set_node_graph(self._node_graph)` 如果 graph 存在。
   - `panel.set_current_frame(self._current_keyframe_frame())`。
2. `_refresh_keyframes_panel()` 可保留同步 Spline 的逻辑，但不要作为唯一入口。

测试：

- 先加载有动画的 graph，再绑定 SplinePanel，面板立即出现曲线。

### P5-3：表达式与关键帧互斥在 load 路径仍可能留下双状态

现状：

- `Node.set_keyframe_track()` 会清理表达式。
- `Node.set_expression()` 会清理 keyframe track。
- 但 `Node.from_dict()` 先 load expressions，再直接赋值 `node.keyframe_tracks = copy.deepcopy(kfs)`，没有清理对应 expression。

解决方法：

1. `Node.from_dict()` 恢复 keyframes 时调用 `node.set_keyframe_track()` 或手动删除相同 param 的 expression。
2. migration v6 中如果同一个 param 同时有 expression 和 keyframes，按规则 keyframes 优先，删除 expression。

测试：

- 项目文件同时含 expression/keyframe，加载后只保留 keyframe。

## 8. P6 测试补齐计划

必须先写失败测试，再修实现。新增测试建议：

1. `tests/composite/test_composite_keyframe_cache_invalidation.py`
   - frame 改变时 animated node dirty。
   - evaluator compute_key 收到 frame_number。
   - 无动画静态节点不因 frame 改变 dirty。
2. `tests/ui/test_composite_keyframe_preview_seek.py`
   - panel playhead → CompositePage → `_evaluate_graph()`。
   - prev/next keyframe toolbar emit seek。
   - external timeline frame → Keyframes/Spline/Properties 同步。
3. `tests/composite/test_spline_keyframe_curve_consistency.py`
   - Spline cubic sample 与 `KeyframeTrack.get_value(frame)` 一致。
   - 保存/加载后 handles 不变。
4. `tests/ui/test_composite_keyframes_undo.py`
   - Properties、KeyframesPanel、SplinePanel、range bar 四条入口 undo/redo。
5. `tests/composite/test_composite_time_range_evaluation.py`
   - frame 在 `time_range` 外时节点行为正确。
6. `tests/ui/test_spline_tree_interaction.py`
   - 曲线选择、显隐、颜色同步。
7. `tests/ui/test_keyframes_scroll_zoom.py`
   - 横向 scrollbar、纵向滚动、Ctrl/Cmd wheel zoom。

## 9. 推荐实施顺序

### 第一阶段：修 P0 播放和缓存正确性

目标：关键帧动画在静态素材、视频素材、独立 comp clip 中都能按当前帧实时刷新。

任务：

1. 修 `set_composite_current_frame()`：同步 UI + 触发 `_evaluate_graph()`。
2. 修 `_current_keyframe_frame()` 的 0 帧判断。
3. 修 RenderScheduler frame dirty tracking。
4. 修 evaluator cache key 传 `frame_number`。
5. 补 P0 测试并跑通。

验收：

```bash
.venv/bin/python -m pytest tests/composite/test_composite_keyframe_cache_invalidation.py tests/ui/test_composite_keyframe_preview_seek.py tests/composite/test_composite_keyframe_frame_number.py -q
```

### 第二阶段：统一 Spline / Keyframe Bezier 数据模型

目标：Spline 里看到的曲线，就是播放和导出实际使用的曲线。

任务：

1. 扩展 `Keyframe` 序列化格式。
2. 兼容迁移旧 `bezier_in/out`。
3. `KeyframeTrack.get_value()` 改用真实 2D cubic solve。
4. `SplineModel` rebuild 保留已保存手柄。
5. Keyframes 面板移动 key 时保留 handle 字段。
6. 补一致性测试。

验收：

```bash
.venv/bin/python -m pytest tests/composite/test_spline_keyframe_curve_consistency.py tests/composite/test_spline_bezier_math.py tests/composite/test_spline_model.py tests/ui/test_spline_view_hit_test.py -q
```

### 第三阶段：实现可靠 Undo/Redo

目标：所有关键帧相关编辑都可撤销/重做，并恢复精确状态。

任务：

1. 统一 CompositePage 写入入口。
2. command 捕获 before/after。
3. 接入 Properties / Keyframes / Spline / range bar。
4. 删除占位 `_undo_keyframe_track()`。
5. 补 undo 测试。

验收：

```bash
.venv/bin/python -m pytest tests/ui/test_composite_keyframes_undo.py tests/ui/test_composite_keyframes_panel.py tests/ui/test_spline_view_hit_test.py -q
```

### 第四阶段：让 `time_range` 真正影响节点

目标：Keyframes 面板调节节点长度后，播放/导出结果真的改变。

任务：

1. 固定 `time_range` schema。
2. evaluator 根据 frame gate 节点。
3. source frame remap。
4. range bar 编辑保留完整字段。
5. 补 time_range evaluation 测试。

验收：

```bash
.venv/bin/python -m pytest tests/composite/test_composite_time_range_evaluation.py tests/composite/test_composite_keyframes_save_load.py -q
```

### 第五阶段：补齐 Spline/Keyframes UI 交互

目标：Keyframes/Spline 面板达到可日常使用，而不是只通过测试。

任务：

1. Spline 左树选择/显隐/分组。
2. Spline playhead 交互。
3. Keyframes 横向/纵向滚动、wheel zoom。
4. toolbar 完整行为。
5. icon 替换 Unicode lock。
6. 节点颜色同步刷新 Spline。

验收：

```bash
.venv/bin/python -m pytest tests/ui/test_spline_tree_interaction.py tests/ui/test_keyframes_scroll_zoom.py tests/ui/test_composite_keyframes_polish.py tests/ui/test_spline_panel.py -q
```

## 10. 最终回归命令

每个阶段结束后至少运行：

```bash
.venv/bin/python -m pytest tests/project/test_keyframe.py tests/composite/test_composite_keyframes_serialization.py tests/composite/test_composite_keyframe_evaluation.py tests/composite/test_composite_keyframe_frame_number.py tests/composite/test_composite_keyframes_node_color.py tests/composite/test_composite_keyframes_save_load.py tests/composite/test_composite_keyframes_model.py tests/composite/test_composite_keyframes_perf.py -q
.venv/bin/python -m pytest tests/ui/test_composite_keyframes_dock.py tests/ui/test_composite_keyframes_panel.py tests/ui/test_composite_keyframes_interaction.py tests/ui/test_composite_keyframes_polish.py tests/composite/test_spline_model.py tests/composite/test_spline_bezier_math.py tests/ui/test_spline_view_render.py tests/ui/test_spline_view_hit_test.py tests/ui/test_spline_panel.py -q
.venv/bin/python -m pytest tests/composite/test_composite_cache_key.py tests/composite/test_cache_strategy.py tests/composite/test_composite_cache.py -q
git diff --check
```

手动 QA 必做：

1. 静态图片接 Transform，给 rotation/center/scale 打关键帧，拖 Keyframes playhead，viewer 必须实时变化。
2. 4K 视频源接 Transform，在 2K comp 下静止和播放尺寸必须一致，关键帧动画不能被缓存卡住。
3. Spline 拖贝塞尔手柄，保存项目，重开，曲线形状必须不变。
4. Spline 曲线某一帧读数与实际参数面板/渲染结果一致。
5. Keyframes range bar 缩短节点后，range 外帧播放/导出结果必须符合定义。
6. 所有关键帧编辑都能 undo/redo，且不会丢失贝塞尔手柄。

## 11. 给后续实现模型的注意事项

- 不要先继续堆 UI。第一优先级是 P0：播放帧、预览刷新、缓存失效。
- Spline 的贝塞尔不能只“画得像”，必须和 `KeyframeTrack.get_value()` 使用同一套数学。
- 不要再让 `ParamDef.animated/keyframes` 成为权威数据源；权威源必须是节点实例 `node.keyframe_tracks`。
- 新增关键帧字段必须兼容旧项目文件。
- 每次编辑 keyframe track 时都要保留未知字段，避免后续新增字段被 UI 面板无意删除。
- range bar 如果没有真正控制求值，就不要在 UI 上暗示它已经能剪辑节点长度。
