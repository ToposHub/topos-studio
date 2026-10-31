# Composite AE 式时间线 — P0 基线与行为契约报告

日期：2026-08-06
对应方案：`docs/composite_ae_like_timeline_implementation_plan_2026-08-06.md`（第 8 节 P0、第 17.1 节 T00-T06）
状态：P0 基线已冻结。本报告是后续 P1-P9 / T07-T129 的回归对照基准。

---

## 0. 目的

P0 的目标是“确定当前行为、避免大改造成不可定位的回归”。本报告完成 T00-T06：

- T00 现有测试基线（命令、数量、结果）。
- T01 动画参数和多维参数清单（scalar/int/bool/color/vec2/vec3/enum）。
- T02 所有 `snapshot(frame_number=...)` 调用点。
- T03 用户可见名称 vs 内部 ID 规则。
- T05 表达式与关键帧优先级（当前行为）。
- T06 当前快捷键模式与 AE/Topos 冲突点。

后续任务若导致相关测试或行为变化，必须在本报告对应小节追加“变更前后”记录。

---

## 1. T00 — 现有测试基线

### 1.1 测试命令

```bash
python3 -m pytest \
  tests/composite/test_composite_edit_service.py \
  tests/composite/test_composite_keyframe_cache_invalidation.py \
  tests/composite/test_composite_keyframe_evaluation.py \
  tests/composite/test_composite_keyframe_frame_number.py \
  tests/composite/test_composite_keyframes_model.py \
  tests/composite/test_composite_keyframes_node_color.py \
  tests/composite/test_composite_keyframes_perf.py \
  tests/composite/test_composite_keyframes_save_load.py \
  tests/composite/test_composite_keyframes_serialization.py \
  tests/composite/test_spline_bezier_math.py \
  tests/composite/test_spline_keyframe_curve_consistency.py \
  tests/composite/test_spline_model.py \
  tests/ui/test_composite_keyframe_preview_seek.py \
  tests/ui/test_composite_keyframes_dock.py \
  tests/ui/test_composite_keyframes_interaction.py \
  tests/ui/test_composite_keyframes_panel.py \
  tests/ui/test_composite_keyframes_polish.py \
  tests/ui/test_composite_keyframes_undo.py \
  tests/ui/test_keyframes_button_and_playrange_sync.py \
  tests/ui/test_keyframes_panel_ae_style.py \
  tests/ui/test_keyframes_panel_animation_e2e.py \
  tests/ui/test_keyframes_panel_multiselect.py \
  tests/ui/test_keyframes_panel_transport_linkage.py \
  tests/ui/test_keyframes_scroll_zoom.py \
  tests/ui/test_spline_axis_modes.py \
  tests/ui/test_spline_extrapolation.py \
  tests/ui/test_spline_multi_select.py \
  tests/ui/test_spline_panel.py \
  tests/ui/test_spline_snap.py \
  tests/ui/test_spline_tangent_input.py \
  tests/ui/test_spline_tree_interaction.py \
  tests/ui/test_spline_view_hit_test.py \
  tests/ui/test_spline_view_menu_hotkeys.py \
  tests/ui/test_spline_view_render.py \
  tests/ui/test_spline_view_zoom_pan.py \
  tests/project/test_composite_persistence.py \
  tests/project/test_expression_ae_runtime.py \
  tests/project/test_expression_parser.py \
  tests/project/test_keyframe.py \
  tests/edit/ai_editing/test_keyframe_sampler.py \
  tests/edit/test_edit_keyframe_effect_commands.py \
  tests/edit/test_effect_keyframe_coordinate_contract.py \
  tests/edit/test_keyframe_easing.py \
  -q
```

### 1.2 基线结果（2026-08-06）

| 分组 | 测试文件数 | 用例数 | 结果 |
|---|---:|---:|---|
| composite 数据层 | 12 | 115 | 全绿 |
| ui keyframes/spline | 23 | 216 | 全绿 |
| project + edit | 8 | 137 | 全绿 |
| **合计** | **43** | **468** | **全绿** |

环境：Python 3.13.5 / pytest 9.0.3 / macOS 24.3.0。

> 后续 P1+ 提交前必须重新跑这条命令并保持 468 全绿，或在本报告第 6 节登记新增/变更用例。

### 1.3 命令级日志与 `track_changed` 数据格式（当前）

`KeyframesTimelineView` / `SplineView` 的所有编辑最终通过 `track_changed(node_id, param_name, keyframes_list)` 上抛，列表元素是 `Keyframe.to_keyframe_dict()` 的字典。`CompositePage._on_keyframes_panel_track_changed`（`composite_page.py:4624`）是统一落盘点：深拷贝 before/after → `node.set_keyframe_track`/`remove_keyframe_track` → `_push_keyframe_track_undo`（`composite_page.py:3694`，`action_type="keyframe_track_change"`）→ `_emit_user_edit_once` → `_evaluate_graph` → 刷新属性面板与动画面板。

范围条走 `range_changed(node_id, time_range_dict)`（`composite_page.py:4760`）；工作区走 `play_range_changed`（`composite_page.py:4800`）。

---

## 2. T01 — 动画参数与多维参数清单

### 2.1 ParamDef 定义

`src/features/composite/nodes/base_node.py:152-241`。关键字段：

- `ptype: str`（`base_node.py:169` 注释列出全部取值）
- `animated: bool = False`（声明态，几乎未用，见 2.4）
- `allow_expression: bool = True`
- `linked: Optional[str]`（X/Y 联动 peer 名，用于链式 UI）

`ptype` 合法值：`float`、`int`、`str`、`bool`、`option`（即 enum）、`color`、`file`、`vec2`、`vec3`、`channel`、`hdri`。

### 2.2 实际在用的 ptype 分布（67 个节点文件，583 条 ParamDef）

| ptype | 数量 | 可动画 |
|---|---:|---|
| `float` | 321 | ✅ |
| `int` | 56 | ✅ |
| `option`（enum） | 110 | ❌ |
| `bool` | 54 | ❌ |
| `str` | 36 | ❌ |
| `string` | 4 | ❌ |
| `file` | 2 | ❌ |

**可动画参数 = 全部 `float` + `int` = 377 条。** 判定能力来源是 `ParamDef.is_animatable()`（`base_node.py:197-199`）：`self.ptype in ("float", "int")`。

### 2.3 多维参数的真相

> **关键发现：原生多维 ptype（vec2/vec3/color/channel/hdri）当前没有任何节点使用**，它们只是 schema 注释里的死条目。多维数值一律拆成同前缀的兄弟标量参数，靠 `group` 字段分组。

约定：

- 2D 点/位置：`*_x` / `*_y`（如 `translate_x/translate_y`、`pivot_x/pivot_y`、`center_x/center_y`、`start_x/start_y`、`end_x/end_y`、`from1_x..to4_x`）。
- 3D 向量：`*_x` / `*_y` / `*_z`（如 `cam_eye_x/y/z`、`object_pos_x/y/z`、`light_dir_x/y/z`）。
- RGBA：`*_r` / `*_g` / `*_b` / `*_a`（常配隐藏 `*_hex` str，如 `color_hex`、`shadow_hex`）。
- 色彩校正：`lift/gamma/gain/offset` 各自的 `_r/_g/_b`。

**动画数据模型严格是“一条标量 = 一条 `KeyframeTrack`”**（`Keyframe.value: float`，`src/shared/keyframe.py:58`）。动画一个 Position 等于在 `node.keyframe_tracks["translate_x"]` 与 `["translate_y"]` 各建一条独立轨道。

### 2.4 能力判定方法

- `ParamDef.is_animatable()`（`base_node.py:197`）—— 类型能力（仅 float/int）。
- `Node.is_param_animated(param_name)`（`base_node.py:485`）—— 实例是否真有关键帧。
- 权威存储：`Node.keyframe_tracks: Dict[str, Dict]`（`base_node.py:364`）。

### 2.5 Transform 类参数（AE P/S/R/A/T 目标）—— 命名不一致

| AE 快捷键 | 概念 | 代表参数（file:line） |
|---|---|---|
| P | Position | `transform_node.py:38-39` `translate_x/y`；`text_node.py:110-111`；`mask_node.py:57-60` `center_x/y`（group="Position"）；3D `object_pos_x/y/z`、`cam_target/eye_x/y/z`、`light_pos_x/y/z` |
| S | Scale | `transform_node.py:40-41` `scale_x/y`；`text_node.py:112-113`；`mask_node.py:62-65` `width/height`（group="Size"） |
| R | Rotation | `transform_node.py:42` `rotation`；`text_node.py:114`；`mask_node.py:67-68`；3D `object_rot_x/y/z`、`cam_orbit_yaw/pitch` |
| A | Anchor/Pivot | `transform_node.py:43-45` `pivot_mode/pivot_x/pivot_y`；`text_node.py:115-117`。**全代码无 `anchor_*`。** |
| T | Opacity | `blend_node.py:136-137` 同时有 `opacity` 与 `mix`；`deep_merge_node.py:65` `opacity`；多数 filter 用 `mix`（blur/color_correct/dissolve/huecorrect/glow/grade/lut） |

> **对 P4（T39）的影响**：P/S/R/A/T 不能映射到单一参数名，必须按节点类型建别名表（translate vs center vs object_pos；opacity vs mix；pivot，无 anchor）。

### 2.6 数据质量小瑕疵（不阻塞，记录在案）

- 4 条 ParamDef 用了未文档化的 `"string"`（应 `"str"`）：`clamp_node.py:51`、`grade_node.py:72`、`noop_node.py:36`、`normalize_node.py:65`。它们都显式传了 default，行为正常，但属于 schema 不一致。
- 只有 `mask_node.py` 5 处显式 `animated=True`，其余靠 ptype 推断。
- `blend_node` 同节点暴露 `opacity` 和 `mix`，语义重叠。

---

## 3. T02 — `snapshot(frame_number=...)` 调用点清单

### 3.1 定义

`NodeGraph.snapshot(...)`：`src/features/composite/core/node_engine.py:1092-1206`。

```python
def snapshot(self, color_texture_id=0, color_width=0, color_height=0,
             color_revision=0, frame_number: int = 0, fps: float = 24.0) -> GraphSnapshot
```

- `frame_number`：动画插值用当前帧（docstring L1105），写入返回的 `GraphSnapshot.frame_number`（L1205）。
- **默认值：`frame_number=0`、`fps=24.0`。任何不显式传帧的调用都会在“第 0 帧”做动画求值。**
- 帧消费入口：`_evaluate_snapshot_parameters`（`node_engine.py:957-1090`）→ 关键帧先于表达式求值（见第 5 节）。
- 实际关键帧插值落点：`BaseEvaluatorMixin._interpolate_parameters(ns, frame_number)`（`core/evaluators/base_evaluator.py:2067-2068`，缓存键 L543）。`evaluators/keyframe_evaluator.py` 只是空 mixin 占位。

### 3.2 预览入口（5 处，全部显式传帧）

帧来源统一是 `CompositePage._current_keyframe_frame()`（`composite_page.py:2165-2188`），优先级：`runtime_context.clip_local_frame` → `timeline_frame` → `self._composite_current_frame` → `0`；fps 取 `comp_clip_fps()`（`composite_page.py:2335`）。

| file:line | 方法 | 帧来源 |
|---|---|---|
| `composite_page.py:840` | `_submit_render_request` | `_current_keyframe_frame()` |
| `composite_page.py:916` | `_retry_renderer_init` | `_current_keyframe_frame()` |
| `composite_page.py:1071` | `update_color_output` | `_current_keyframe_frame()` |
| `composite_page.py:3092` | `_evaluate_graph_now`（防抖） | `_current_keyframe_frame()` |
| `composite_page.py:4041` | `_trigger_prefetch` | `_current_keyframe_frame()` |

播放头移动通过 `set_composite_current_frame` 写入 `self._composite_current_frame`，再经上述入口，无独立 snapshot 站点。

### 3.3 导出入口

| file:line | 路径 | 帧来源 |
|---|---|---|
| `core/composite_frame_renderer.py:309`（`_build_snapshot`，L288） | 时间线渲染 | `render_comp_clip_frame(..., timeline_frame=...)` 的入参，由时间线循环逐帧传入。✅ |
| **`deliver_controller.py:419`**（`_render_composite_full_res`，L370） | 单文件静帧合成导出 | **⚠️ 不传 `frame_number`，默认 0；也不传 `fps`，默认 24.0。** |

调用 `render_comp_clip_frame`（间接到 snapshot）的上游：`composite_render_output.py:659`、`composite_graph_render_service.py:104`、`timeline_export.py:2888`、`timeline_preview_renderer.py:2107`、`render_cache_worker.py:112` —— 都从各自循环/播放头传 `timeline_frame`，正确。

### 3.4 ⚠️ 唯一的静默分歧风险

**`deliver_controller.py:419-423`**（`_render_composite_full_res`，由 `_export_composite` L249 → L291 触发，也由面板导出 L1292 触发）：

```python
ng.snapshot(color_texture_id=..., color_width=..., color_height=...)
# frame_number 与 fps 均缺省
```

对任何带动画（关键帧轨道或 `time`/`valueAtTime` 表达式）的合成，导出的静帧会落在第 0 帧，与预览/时间线不一致。

> **后续动作**：记入 T02 验收项。修复属于 P0/P8 范畴（渲染一致性），但本报告只冻结基线、不改代码。修复时改为传 `_current_keyframe_frame()` 的等价值（导出场景下通常是合成当前帧或用户指定帧），并新增一条 `tests/composite/` 用例锁定。

### 3.5 不在范围内的 snapshot（易混淆，记录排除）

这些是 `CompositeEditService.snapshot()` / `CompositeScriptAPI.snapshot()`，返回编辑协议用的纯 dict，**没有 `frame_number` 参数**，与渲染无关：`ai_edit_host_bridge_impl.py:313`、`ai_editing_mixin.py:981`、`scripting/composite_api.py:25`。

### 3.6 渲染线程帧消费（只读，无自己的 snapshot 调用）

- `render_scheduler.py`：脏跟踪比较 `snapshot.frame_number`（L1028-1029、L1102/1115/1139），按节点 `requires_frame_number` 标脏；辅助 `_snapshot_node_needs_frame`（L33-46）。
- `cache_manager.py` L172/278-279/360-387、`ai_cache_manager.py` L61/107：把 `frame_number` 折进每节点缓存键。

---

## 4. T03 — 用户可见名称 vs 内部 ID 规则（当前已合规）

### 4.1 显示名来源

`KeyframesModel._node_label(node)`（`keyframes_model.py:352-361`）—— 只用 `node.display_title`（回退 `node_title` → `node_type`）。**绝不拼接 `node.id`/哈希/UUID。** 内部 id 仅留在 `KeyframesRow.node_id` / `row_id`。

> 代码注释（L354-356）明确：UUID 类 id 是内部图键，不进用户可见文本。

### 4.2 树视图与时间线

- `keyframes_tree_view.py:136-141`：绘制 `row.label` 的省略文本，无 id。
- `keyframes_timeline_view.py:423-442`：范围条文字是 `bar.label`（来自 `_node_label`）+ 帧号，无 id。
- 两处都未 `setToolTip(...)`，也未拼接内部 id。

### 4.3 display_title 生成与同名冲突

`BaseNode.display_title`（`base_node.py:297-323`）：`node_title`（可选 i18n）→ 有 `instance_label` 则拼成 `"Camera1"`。**无 `effective_name()`、无 `.label` 属性。**

冲突风险：`instance_label` 仅对 `core/node_engine.py:308-323` `_INSTANCE_NUMBERED_TYPES` 内的类型分配（`max_index+1`）。**不在集合内的同类型多实例，显示名完全相同**（如两个 "Blur"）。数据操作靠 `node_id` 不混淆，但可见标签重复。

> **后续动作（P1/T03 验收）**：保留现状即可（符合“不显示 id”），但 P3 视觉重构时若引入搜索/独奏，需考虑同名消歧（类型放第二行或 tooltip）。

### 4.4 node_id 性质

`BaseNode.__init__`（`base_node.py:326`）：`self.id = node_id or str(uuid.uuid4())[:8]` —— 截断 8 位随机 UUID，**非稳定可读 slug、非确定性**。仅作图键与 row_id，从不绘制。符合方案要求。

---

## 5. T05 — 表达式与关键帧优先级（当前行为）

### 5.1 表达式上下文变量

`src/features/composite/utils/expression_parser.py`：

- 简单名变量 `_eval_variable`（L512-549）：`frame`、`time`、`fps`、**`value`（= `context.current_value`，L521-522）**、`width`、`height`、`pi`、`e`。
- 属性访问 `_eval_attribute`（L551-594）：`this.*`（本节点参数）、`input.*` / `comp.*` / `thisComp.*`、`NodeName.param`。
- 数学/插值函数 `SAFE_MATH_FUNCTIONS`（L123-165）：三角、`sqrt/abs/floor/ceil/round/min/max/log/exp/pow`、`clamp/lerp/linear/ease/easeIn/easeOut/easeInOut/step/smoothstep/mod/fract/radians/degrees`。
- 运行时函数 `_function_map`（L620-670）：`valueAtTime`、`wiggle`、`random`、`seedRandom`、`noise`、可选 `loopIn/loopOut`。
- 上下文结构 `ExpressionContext`（L799-837）。

### 5.2 求值顺序（已符合目标优先级）

`node_engine._evaluate_snapshot_parameters`（`node_engine.py:957-1090`）：

1. **关键帧先求值**（L974-990）：对每条 `keyframe_tracks`，用 `track.get_value_float(frame_number, default=...)` 覆盖 `raw_params[param_name]`。
2. **表达式后求值**（L1069 → `ExpressionManager.evaluate_all`，`expression_parser.py:994-1038`）：每条表达式的 `context.current_value` 取**已关键帧求值的值**（L1008），即方案要的 `value = 关键帧结果`。
3. **错误回退**：`evaluate_all` 仅在 `eval_result.success and value is not None` 时覆盖（L1035-1036）；任何错误保留关键帧/静态值。第二道护栏（`node_engine.py:1085-1089`）把 NaN/Inf 换回原始值。

→ 数值管线本身 = *关键帧/静态值 → 表达式有效则覆盖 → 错误回退*，符合 §5.4 目标。

### 5.3 ⚠️ 关键契约缺口：表达式与关键帧互斥

`base_node.py`：

- `set_expression`（L557-561）：设表达式时 `self.remove_keyframe_track(key)`。
- `set_keyframe_track`（L468-471）：加轨道时 `self.remove_expression(param_name)`。

**当前一个参数上表达式与关键帧不能共存（rule "A-15"）。** 因此实践中表达式看到的 `value` 永远是**静态值**，而方案 §5.4 期望“表达式可建立在关键帧之上”。这是 P5（T49-T55）必须解决的行为差异，本报告仅记录。

### 5.4 表达式存储

`BaseNode._expressions: Dict[str, str]`（`base_node.py:356`）。方法：`set_expression`（L526-566，含 `ExpressionParser().validate`）、`get_expression`（L568-577）、`has_expression`（L579-588）、`remove_expression`（L590-603）、`get_expression_dependencies`（L605-634）。序列化：写入 `data["expressions"]`（L1039-1040），`from_dict` 回读（L1063、L1084-1085，且丢弃与新增关键帧冲突的表达式）。

快照期实际运行的是新建的 `ExpressionManager`（`expression_parser.py:840-1148`，`node_engine.py:965`），带依赖图与拓扑求值。

---

## 6. T06 — 当前快捷键模式与 AE 冲突

### 6.1 `keyframes_panel.py`（panel 级 QShortcut，`WidgetWithChildrenShortcut`）

| 键 | line | 行为 |
|---|---|---|
| `K` | 104-107 | **添加关键帧**（`add_keyframe_at_playhead`） |
| `I` | 110-113 | 设置工作区 In |
| `O` | 114-117 | 设置工作区 Out |

工具栏 QAction（`_build_toolbar` L173-269、footer L271-315）**都是按钮、未 `setShortcut`**：传输 first/prev/play/next/last、prev_kf/next_kf、add_kf/del_kf、fit/zoom_in/expand_all/collapse_all。

**panel 级缺失**：`J`、`Space`、`Home`、`End`、方向键、`Delete`、`Ctrl/Cmd+K`、`U/UU/P/S/R/A/T/EE/F2`。

### 6.2 `keyframes_timeline_view.py` keyPressEvent（L722-770，StrongFocus）

| 键 | 行为 |
|---|---|
| `Ctrl+C` | 复制选中关键帧 |
| `Ctrl+V` | 粘贴（锚定播放头） |
| `Ctrl+A` | 全选所有轨道关键帧 |
| `Delete/Backspace` | 删除选中 |
| `Left/Right` | 播放头 ±1 帧（`Shift` = ±10） |
| `Home/End` | 跳到全局首/末关键帧（`_goto_boundary`） |

### 6.3 `spline_view.py` `_sv_keyPressEvent`（L1220-1255）

`Delete/Backspace` 删、`F` 适配、`Ctrl+A` 全选、`Ctrl+D` 取消全选、`Ctrl+C` 复制、`Ctrl+Shift+C`/`Ctrl+X` 剪切、`Ctrl+V` 粘贴、`Left/Right` 微移（Shift=10）。`spline_panel.py` 仅工具栏按钮，无快捷键。

### 6.4 ⚠️ AE 冲突点（P4/T38 必须处理）

1. **`K` 当前=添加关键帧**，与 AE 的 `K`=下一个关键帧**直接冲突**。代码注释（L103）只写 `# K → add keyframe`，**未标注 AE 冲突**。
2. **`J` 未映射**（AE: 上一个关键帧）。
3. **`Ctrl/Cmd+K` 未注册**（方案要的添加关键帧键）。
4. 上一个/下一个关键帧导航目前**仅工具栏按钮**（`_goto_prev/next_keyframe`，panel L572-582），无 `J/K` 键。
5. `I/O` 已符合 AE。✅

### 6.5 焦点过滤（隐式，非显式）

三处快捷键**都没有显式的 QLineEdit/QSpinBox 焦点护栏**（无 `focusWidget()` 判断），仅靠 `WidgetWithChildrenShortcut` 上下文与 Qt 默认传播。实践中聚焦在输入框时字母会被输入框先消费，但这是 Qt 隐式行为，非显式抑制。

> **后续动作（P4/T38）**：实现快捷键时必须加显式焦点上下文过滤，避免在搜索框/数值框里输入字母时误触发时间线命令。

---

## 7. P0 验收结论

| 项 | 状态 |
|---|---|
| 现有相关测试全绿 | ✅ 468/468 |
| 测试命令与用例数有记录 | ✅ §1 |
| 动画/多维参数清单 | ✅ §2 |
| snapshot 调用点与帧来源 | ✅ §3（含 1 处分歧风险） |
| 名称/ID 规则 | ✅ §4（当前已合规） |
| 表达式/关键帧优先级 | ✅ §5（数值管线合规，但**互斥**是缺口） |
| 快捷键与 AE 冲突 | ✅ §6（`K` 冲突、`J`/`Cmd+K` 缺失、焦点过滤隐式） |

**冻结为基线。** 已登记 3 个非阻塞观察项，供后续阶段处理：

1. `deliver_controller.py:419` 静帧导出缺 `frame_number`（→ P8/T02 修复 + 测试）。
2. 表达式与关键帧互斥（→ P5/T49-T55 决策“共存 or 互斥”）。
3. `K` 添加 vs AE 导航冲突 + `J`/`Ctrl+Cmd+K` 缺失 + 焦点过滤隐式（→ P4/T37-T38）。

下一阶段进入 **P1（T04, T07-T14）统一 ViewModel 与 Selection 数据层**。

---

## 8. 基线问题关闭记录（实施后回填）

以下 3 个观察项在后续批次中已全部关闭，回填于此作为审计追溯。

### 8.1 ✅ T02 静帧导出 `frame_number` 分歧（关闭于 Batch 9 / commit `d4451a54`）

**问题**：`deliver_controller._render_composite_full_res` 调用 `NodeGraph.snapshot(...)` 时未传 `frame_number`/`fps`，静帧导出落到第 0 帧，与预览/时间线分歧。

**修复**：
- `_render_composite_full_res` 新增 `frame_number`/`fps` 可选参数，传入 `ng.snapshot(frame_number=..., fps=...)`。
- 新增 `_resolve_export_frame` / `_resolve_export_fps`，优先级：显式入参 → 单文件路径用 `composite_page._current_keyframe_frame()`/`comp_clip_fps()` → 批量路径（反序列化临时图）回退到第 0 帧 / 24fps。
- 容错：缺访问器或 getter 抛异常时安全回退到 0/24.0；非数值或非正 fps 夹到 ≥1.0。
- 测试：`tests/app/test_deliver_composite_frame.py`（15 用例覆盖每个优先级分支 + 安全回退）。

### 8.2 ✅ T49-T55 表达式与关键帧优先级建模（关闭于 Batch 5 / commit `e80e2009`）

**问题**：A-15 互斥使表达式看到的 `value` 永远是静态值，方案 §5.4 期望“表达式可建立在关键帧之上”。

**修复**（建模层，宿主侧落地待后续批次）：
- 新增 `timeline_expression.py`：`ExpressionPolicy(allow_keyframe_expression_coexistence)` 默认 False 保留 A-15（旧测试不破），翻为 True 即启用 §5.4 分层模型。
- `derive_expression_status` 四态（none/valid/error/disabled）+ `resolve_precedence` 实现 §5.4 优先级（关键帧/静态 → 表达式 → 错误回退）。
- 稳定错误码 `EXPR_ERR_*`（T53），每个 `ExpressionErrorType` 都映射。
- 测试：`tests/composite/test_timeline_expression.py`（32 用例）。

### 8.3 ✅ `K` 添加 vs AE 导航冲突（关闭于 Batch 4 + 8a / commits `4793c4da`,`b09abe32`）

**问题**：plain `K` 旧=添加关键帧，AE 用户期望 `K`=下一个关键帧；`J`/`Ctrl+Cmd+K` 未注册；焦点过滤隐式。

**修复**：
- Batch 4 `timeline_shortcuts.py`：`ShortcutRegistry` 默认 AE 模式映射 `K`→nav.next、`J`→nav.prev、`Ctrl/Cmd+K`→add；`disable_ae_nav_conflicts()` 恢复 legacy；显式焦点上下文过滤（QLineEdit/QSpinBox 等聚焦时抑制字符类快捷键，但不抑制 Ctrl+V/Ctrl+A）。
- Batch 8a `keyframes_panel.py`：`_rebuild_shortcuts()` + `set_ae_nav_enabled()` 把 AE 导航实际绑到面板，plain `K` 在 AE 模式导航、legacy 模式添加；`Ctrl/Cmd+K` 双模式都可用；I/O 重建后存活。
- 测试：`tests/composite/test_timeline_shortcuts.py`（28 用例，纯逻辑）+ `tests/ui/test_keyframes_panel_ae_nav.py`（11 用例，面板集成）。

### 8.4 累计基线变化

| 时点 | 用例数 | 状态 |
|---|---:|---|
| P0 冻结（§1.2） | 468 | 全绿 |
| Batch 2-9 累计 | 725 | 全绿（468 旧基线全绿 + 257 新增） |

新增测试覆盖：T04/T07-T10/T14（37）、T15-T21/T65（32）、T37-T40（28）、T49/T53/T55（32）、T62-T65（14）、T35/T76（26）、T06/T37/T38 视图（11）、T49 视图（13）、T02（15）+ 49 项 manifest 邻接回归。下一阶段进入 **P1 视图残余（T11-T13）+ P3 AE 布局（T24-T34）**。
