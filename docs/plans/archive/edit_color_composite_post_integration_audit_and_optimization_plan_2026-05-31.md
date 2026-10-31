# Edit / Color / Composite 打通后审计与下一轮优化计划

创建日期：2026-05-31

适用范围：

- Edit 时间线片段进入 Composite
- Color 调色结果作为 Composite 输入
- Comp Clip 媒体池资产、时间线资产、工程保存
- Composite 主视图、节点缩略图、Edit 时间线视觉反馈
- 后续渲染缓存与导出接入

## 0. 总结结论

当前实现已经把很多骨架搭起来了：

- `MediaAsset.media_type` 已支持 `comp_clip`
- 已新增 `CompClipData`
- 已新增 `CompositeGraphData` 的 Comp Clip 字段
- 已新增 `TimelineClipInputNode`
- 媒体池已经能显示 Comp Clip、双击打开 Composite、创建空白 Comp Clip
- Edit clip 已有 `composite_id`
- Edit / Color 的调色栈链路已有较好的基础

但是现在还不能认为 Edit / Color / Composite 已经真正打通。当前有多个 P0 级问题，其中最明显的就是：

> Composite 节点预览图能显示，但主视图黑屏，很可能不是 Viewer 本身坏了，而是默认图的数据连接、TimelineClipInput 求值链路、runtime provider 链路没有真正通。

必须先修 P0 黑屏与数据绑定，再做 UI 体验优化。否则用户看到的 Comp Clip 标签、重置、渲染缓存都会建立在不稳定的基础上。

## 1. 已确认的 P0 问题

| 编号 | 问题 | 影响 | 相关文件 | 优先级 |
|---|---|---|---|---|
| P0-1 | 默认 Composite 图连接到不存在的 Output 端口 | 主视图黑屏，UI 看似有连接但数据模型没有连接 | `src/features/edit/core/composite_graph_data.py` | 最高 |
| P0-2 | `load_composite_data()` 不检查 `NodeGraph.connect()` 返回值 | 失败连接仍绘制视觉连接线，造成“节点链路看起来正确但渲染为空”的错觉 | `src/features/composite/ui/composite_page.py` | 最高 |
| P0-3 | `timeline_clip_input` 没有在 render thread 创建 render-side node | 调度器认为它是 source，但评估时拿不到节点实例，输出为空 | `src/features/composite/evaluators/base_evaluator.py` | 最高 |
| P0-4 | `TimelineClipInputNode` 需要 runtime provider，但当前 `ctx` 是 ModernGL context | `runtime_context` / `timeline_clip_gpu_provider` 永远拿不到 | `src/features/composite/nodes/timeline_clip_input_node.py` | 最高 |
| P0-5 | `TimelineClipFrameProvider.get_timeline_clip_texture()` 仍是 stub，固定返回 `None` | 即使节点被创建，也无法拿到时间线帧纹理 | `src/features/edit/core/timeline_clip_frame_provider.py` | 最高 |
| P0-6 | 进入 Composite 时仍依赖 `engine.color_output`，不是稳定的 Edit timeline clip frame provider | 从 Edit 切到 Composite 时未必已经有正确 clip 的 Color 输出 | `src/app/controllers/composite_controller.py`, `src/app/controllers/page_controller.py` | 最高 |
| P0-7 | 打开普通 clip 会立即写入 `clip.composite_id`，但没有创建真正 Comp Clip；首次编辑时的懒创建也没有接线 | 用户添加节点后，媒体池、时间线、工程数据不一定进入一致的 Comp Clip 状态 | `src/app/controllers/edit_controller.py`, `src/features/composite/ui/composite_page.py` | 最高 |
| P0-8 | 懒创建 Comp Clip 会新建 graph_id，可能和用户正在编辑的 temporary composite 分裂 | 用户编辑 A graph，但 clip 被指向 B graph，保存/回放会错位 | `src/app/controllers/edit_controller.py` | 最高 |
| P0-9 | `create_comp_clip_from_timeline_clip()` 没有读取真实 timeline clip 和原始 asset，写入空 `original_asset_id` | 时间线预览和导出对 Comp Clip 回源会失败或跳过 | `src/features/edit/core/project.py` | 最高 |
| P0-10 | 保存 Composite 到 EditProject 时重建 `EditProject` 丢失 `comp_clips` 和 `active_comp_clip_id` | 保存一次 Composite 可能把 Comp Clip 表清掉 | `src/shared/project/save_router.py` | 最高 |
| P0-11 | 切换到 Composite 时没有稳定设置 `_current_composite_id` | SaveRouter 可能保存到旧 composite、空 composite，甚至自动生成新 ID 且不回写 clip | `src/app/controllers/composite_controller.py`, `src/app/controllers/page_controller.py` | 最高 |
| P0-12 | Edit 时间线预览/导出当前只是把 `comp_clip` 解析成原始素材，未应用 Composite graph | 回到 Edit 播放 Comp Clip 时看不到合成结果 | `src/features/edit/core/timeline_preview_renderer.py`, `src/features/edit/core/timeline_export.py` | 最高 |

## 2. 黑屏根因说明

### 2.1 默认图端口写错

`OutputNode` 的输入端口名是：

```python
Port("image", "input", "image")
```

但 `CompositeGraphData.create_default_for_clip()` 和 `create_default_empty_comp_clip()` 创建的连接使用：

```json
"dst_port_name": "input"
```

这会导致：

1. `NodeGraph.connect(src, "output", output, "input")` 返回 `False`
2. 数据模型没有真实连接
3. `load_composite_data()` 仍然调用 `add_connection_visual(...)`
4. 节点编辑器里看起来有连接线
5. 渲染调度里 Output 没有输入
6. 主视图输出为空或被清黑

验证脚本已经能复现：默认图加载后 `connections == 0`，`has_connected_output() == False`。

### 2.2 TimelineClipInput 没有渲染侧实例

`timeline_clip_input` 已经注册在：

- `NODE_REGISTRY`
- `NODE_CATEGORIES`
- `node_capabilities`
- `render_scheduler._GPU_STATEFUL_TYPES`

但 `BaseEvaluatorMixin._sync_render_nodes()` 只创建：

```python
("constant", "image_input", "video_frame")
```

没有创建 `timeline_clip_input`。因此 `_evaluate_node()` 进入 source 分支时：

```python
node = self._render_nodes.get(ns.node_id)
if node is None:
    return {}
```

最终 Output 没有输入。

### 2.3 runtime context 没有进入节点求值

`TimelineClipInputNode._do_evaluate()` 试图从 `ctx` 读取：

```python
runtime = getattr(ctx, "runtime_context", None)
provider = getattr(ctx, "timeline_clip_gpu_provider", None)
```

但当前 `node.evaluate(...)` 传入的 `ctx` 是 `moderngl.Context`，不是业务求值上下文。这个路径永远拿不到 provider。

### 2.4 Provider 仍未实现

`TimelineClipFrameProvider.get_timeline_clip_texture()` 当前明确写着第一阶段返回 `None`。所以就算前面全部接通，主输入也没有纹理。

## 3. 必须先执行的 P0 修复顺序

不要并行乱改。建议按下面顺序执行。

### Phase 0：建立失败复现测试

目标：先把当前黑屏的根因锁进测试，防止 AI 修一个漏一个。

新增测试：

```text
tests/composite/test_comp_clip_default_graph_contract.py
tests/composite/test_timeline_clip_input_render_contract.py
tests/project/test_comp_clip_save_integrity.py
tests/edit/test_comp_clip_promotion_lifecycle.py
```

测试 1：默认图端口必须可连接。

实施：

1. 调用 `CompositeGraphData.create_default_for_clip(...)`
2. 用 `NODE_REGISTRY` 真实创建 `NodeGraph`
3. 遍历 `connections`
4. 每次 `graph.connect(...)` 都必须返回 `True`
5. `graph.has_connected_output()` 必须为 `True`

验收：

```bash
.venv/bin/python -m pytest tests/composite/test_comp_clip_default_graph_contract.py -q
```

测试 2：空白 Comp Clip 默认图也必须可连接。

实施：

1. 调用 `CompositeGraphData.create_default_empty_comp_clip(...)`
2. 同样检查连接返回值和 connected output

测试 3：`load_composite_data()` 不允许视觉连接和数据连接不一致。

实施：

1. 构造一个错误端口名的 graph
2. 调用 `CompositePage.load_composite_data(...)`
3. 断言数据连接失败时不会添加视觉连接线
4. 断言 status/log 能说明端口不匹配

测试 4：保存 Composite 后 Comp Clip 表不丢失。

实施：

1. 创建含 `comp_clips` 的 `EditProject`
2. 调用 SaveRouter 的 Composite 保存路径
3. 断言 `updated_project.comp_clips` 原样保留
4. 断言 `active_comp_clip_id` 原样保留

### Phase 1：修复默认图与连接校验

目标：先让默认图真正连上 Output。

实施文件：

- `src/features/edit/core/composite_graph_data.py`
- `src/features/composite/ui/composite_page.py`
- `tests/composite/test_comp_clip_default_graph_contract.py`

具体步骤：

1. 修改 `CompositeGraphData.create_default_for_clip()`：

```python
"dst_port_name": "image"
```

2. 修改 `CompositeGraphData.create_default_empty_comp_clip()`：

```python
"dst_port_name": "image"
```

3. 修改 `CompositePage.load_composite_data()`：

当前逻辑：

```python
self._node_graph.connect(...)
self._node_editor.add_connection_visual(...)
```

改为：

```python
success = self._node_graph.connect(...)
if success:
    self._node_editor.add_connection_visual(...)
else:
    logger.warning(...)
    self.status_message.emit(...)
```

4. 对端口不匹配给出可见提示：

- 状态栏显示：`Invalid composite connection: <src>.<port> -> <dst>.<port>`
- 不要画假的连接线
- 不要静默吞掉错误

验证：

```bash
.venv/bin/python -m pytest tests/composite/test_comp_clip_default_graph_contract.py -q
.venv/bin/python -m pytest tests/edit/test_composite_graph_data.py -q
```

手动验证：

1. 在 Edit 右键普通视频 clip
2. Open in Composite
3. 默认图应显示 `Timeline Clip Input -> Output`
4. Output 节点必须有真实连接
5. 断开连接后主视图可黑屏，但状态要说明 output disconnected
6. 重新连接后主视图恢复

### Phase 2：接通 TimelineClipInput 渲染侧实例

目标：`timeline_clip_input` 必须像 `color_input/image_input/constant` 一样被 render thread 同步。

实施文件：

- `src/features/composite/evaluators/base_evaluator.py`
- `src/features/composite/core/node_capabilities.py`
- `src/features/composite/nodes/timeline_clip_input_node.py`
- `tests/composite/test_timeline_clip_input_render_contract.py`

具体步骤：

1. 修改 `_sync_render_nodes()`，不要再手写少量 source 类型。

推荐实现：

```python
from src.features.composite.core.node_capabilities import is_source_node_type

if is_source_node_type(ns.node_type) and ns.node_type != "color_input":
    existing = self._render_nodes.get(nid)
    if existing is None:
        cls = NODE_REGISTRY.get(ns.node_type)
        if cls:
            existing = cls(node_id=nid)
            self._render_nodes[nid] = existing
    if existing:
        sync parameters and disabled
```

2. 保留 `color_input` 的特殊逻辑，因为它要注入 `snapshot.color_texture_id`。

3. 确保 `timeline_clip_input` 的参数同步：

- `timeline_id`
- `clip_id`
- `asset_id`
- `input_stage`

4. 测试 render-side node 创建：

```python
snapshot = graph.snapshot(...)
renderer._sync_render_nodes(snapshot)
assert input_node_id in renderer._render_nodes
assert renderer._render_nodes[input_node_id].node_type == "timeline_clip_input"
```

验证：

```bash
.venv/bin/python -m pytest tests/composite/test_timeline_clip_input_render_contract.py -q
.venv/bin/python -m pytest tests/composite/test_render_scheduler.py -q
```

### Phase 3：建立 CompositeEvalContext，不再把业务上下文挂在 ModernGL ctx 上

目标：Composite 节点求值同时拿到：

- OpenGL context
- framebuffer manager
- resource pool
- runtime context
- timeline clip GPU provider

新增文件建议：

```text
src/features/composite/core/eval_context.py
```

新增数据结构：

```python
@dataclass
class CompositeEvalContext:
    gl: Any
    runtime_context: CompositeRuntimeContext | None = None
    timeline_clip_gpu_provider: Any | None = None
```

实施步骤：

1. 扩展 `RenderRequest`：

```python
runtime_context: CompositeRuntimeContext | None = None
timeline_clip_gpu_provider: Any | None = None
```

2. `CompositePage._evaluate_graph()` 创建 request 时带上当前 composite context。

3. `CompositeRenderer._handle_evaluate_graph()` 把 request 上下文传给 `_evaluate_snapshot(...)`。

4. `BaseEvaluatorMixin._evaluate_snapshot()` / `_evaluate_node()` 改为传入 `CompositeEvalContext`。

5. 兼容旧节点：

大多数节点只需要 ModernGL context。为了降低改动风险，可以让 `CompositeEvalContext` 暴露：

```python
eval_ctx.gl
```

然后逐步把 evaluator 中的 `ctx` 替换为 `eval_ctx.gl`。

短期兼容方案：

- source nodes 或 TimelineClipInput 使用 `eval_ctx`
- 其他节点暂时仍传 `eval_ctx.gl`

但最终目标是统一接口，避免再把业务字段挂到 ModernGL context 上。

验证：

```bash
.venv/bin/python -m pytest tests/composite/test_timeline_clip_input_render_contract.py -q
.venv/bin/python -m pytest tests/composite/test_composite_node_contract.py -q
```

### Phase 4：实现 TimelineClipFrameProvider 的首版可用链路

目标：Composite 页面打开 Edit timeline clip 时，主视图能显示正确帧。

实施文件：

- `src/features/edit/core/timeline_clip_frame_provider.py`
- `src/features/composite/nodes/timeline_clip_input_node.py`
- `src/app/controllers/composite_controller.py`
- `src/app/controllers/page_controller.py`

首版实现要求：

1. 输入：

```python
runtime_context.timeline_id
runtime_context.clip_id
runtime_context.timeline_frame
runtime_context.source_frame
runtime_context.input_stage
```

2. 支持 `input_stage="after_color"`。

3. 获取帧优先级：

```text
已缓存的 Edit/Color GPU texture
  -> 当前 clip 的 ColorStack GPU 输出
  -> source_raw GPU decoded frame
  -> CPU fallback placeholder only for debug
```

4. 如果拿不到真实纹理，不要静默黑屏。

返回一个明显的 debug placeholder：

- 深灰底
- 红色小条
- metadata 写明 `TimelineClipInput missing texture`

这样用户能区分“素材是黑画面”和“管线没拿到纹理”。

5. `TimelineClipInputNode` 不允许自己打开视频文件。

禁止：

```python
MediaSourceFactory.create(...)
VideoFrameNode._decode_frame(...)
np.ndarray 作为主路径
```

验证：

```bash
.venv/bin/python -m pytest tests/edit/test_clip_timeline_color_frame_mapping.py -q
.venv/bin/python -m pytest tests/composite/test_timeline_clip_input_render_contract.py -q
```

手动验证：

1. 时间线放入一段视频
2. 进入 Color 做明显曝光变化
3. 回 Edit
4. 右键该 clip 打开 Composite
5. 默认 TimelineClipInput -> Output 主视图应显示调色后的当前帧
6. 改变 playhead，再进入 Composite，应显示对应时间线帧

## 4. Comp Clip 生命周期重构

### 4.1 当前问题

当前 `_on_open_in_composite_requested()` 对普通 clip 的流程是：

1. 创建 temporary composition
2. 立即写 `clip.composite_id`
3. 切到 Composite
4. 等首次编辑时再 `_ensure_comp_clip_for_current_composite()`

问题：

- `clip.composite_id` 已经存在，但媒体池没有 Comp Clip，用户会误以为已经启用合成。
- `_ensure_comp_clip_for_current_composite()` 没有被 CompositePage 的节点新增、连接、参数修改等信号调用。
- `_ensure_comp_clip_for_current_composite()` 会新建 graph_id，和用户正在编辑的 temporary composition 分裂。
- `create_comp_clip_from_timeline_clip()` 没有真实读取 clip 和 asset，写入空 source。

### 4.2 推荐新规则

普通 clip 打开 Composite 时：

- 可以创建临时 graph 用于预览，但不要把 clip 标记为正式 Comp Clip。
- 一旦用户做出真实编辑，立刻把当前 graph 原地提升为 Comp Clip。
- 原地提升必须保留当前 graph_id，不能新建第二个 graph。

真实编辑包括：

- 新增节点
- 删除节点
- 连接节点
- 断开节点
- 修改节点参数
- Viewer transform handle 改参数
- 粘贴/复制/批量删除节点
- 添加 backdrop 不算启用合成，除非产品决定把 UI 注释也视为编辑

### 4.3 新增项目方法

在 `EditProject` 增加：

```python
promote_composition_to_comp_clip(
    composite_id: str,
    timeline_id: str,
    clip_id: str,
    name: str | None = None,
) -> tuple[EditProject, str, str]
```

返回：

```text
(new_project, comp_clip_id, comp_asset_id)
```

实现要求：

1. 查找 `compositions[composite_id]`，必须存在。
2. 从 `timeline_sequences` 或调用方传入的 `TimelineSequence` 找到真实 clip。
3. 读取真实 `original_asset_id = clip.asset_id`。
4. 读取真实原素材：

```python
original_asset = project.assets[original_asset_id]
```

5. 读取真实 duration：

```python
duration_frames = clip.timeline_duration
```

6. 读取真实 resolution/fps：

优先 timeline，其次 source metadata，最后默认值。

7. 创建 `MediaAsset(media_type="comp_clip")`。
8. 创建 `CompClipData`。
9. 更新原 `CompositeGraphData`：

```python
comp_clip_id=...
comp_asset_id=...
source_kind="timeline_clip_upgrade"
source_refs={
    "original_asset_id": original_asset_id,
    "original_clip_id": clip_id,
    "original_timeline_id": timeline_id,
}
```

10. 不要新建 graph_id。
11. 返回新 project。

验证：

```bash
.venv/bin/python -m pytest tests/edit/test_comp_clip_promotion_lifecycle.py -q
```

### 4.4 CompositePage 发出 dirty/edit 信号

在 `CompositePage` 新增信号：

```python
composite_user_edit_started = pyqtSignal()
composite_graph_dirty = pyqtSignal(dict)
```

触发位置：

- `_on_node_added`
- `_on_node_removed`
- `_on_nodes_connected`
- `_on_nodes_disconnected`
- `_on_parameter_changed`
- `_on_parameters_changed_batch`
- `_on_viewer_interaction_ended`
- `_on_nodes_pasted`
- `_on_nodes_duplicated`
- `_on_nodes_batch_removed`

MainWindow / EditController 监听：

```python
composite_page.composite_user_edit_started.connect(
    self._ensure_comp_clip_for_current_composite
)
```

要求：

- 同一个 composite 只 promote 一次。
- promote 后更新当前 context：

```python
context.composite_id = graph_id
context.asset_id = comp_asset_id
```

- 更新 `_current_composite_id = graph_id`
- 刷新 timeline view
- 刷新 media pool

### 4.5 打开 Composite 时设置当前 composite id

修改：

- `_switch_to_composite_page(composite_id)`
- `_handle_enter_composite_with_context(context)`
- `_on_open_comp_clip_in_composite(asset_id)`

必须设置：

```python
self._current_composite_id = composite_id
context.composite_id = composite_id
```

否则 SaveRouter 无法知道当前保存目标。

验证：

1. 打开 clip 到 Composite
2. 添加 Blur
3. 保存项目
4. 断言 `.topos` 中 `composite/graphs.json` 的目标 graph_id 是 clip 当前 graph_id
5. 重新打开项目，clip 仍指向同一个 graph

## 5. 保存与 `.topos` 工程文件修复

### 5.1 SaveRouter 不得丢失 Comp Clip 字段

当前 `_save_composite_to_edit_project()` 重建 `EditProject` 时没有传：

- `comp_clips`
- `active_comp_clip_id`

必须补齐。

实施文件：

- `src/shared/project/save_router.py`

修改重建字段：

```python
comp_clips=edit_project.comp_clips,
active_comp_clip_id=edit_project.active_comp_clip_id,
```

同时检查所有 `EditProject(...)` 重建点，凡是新增字段都必须保留。

验证：

```bash
.venv/bin/python -m pytest tests/project/test_comp_clip_save_integrity.py -q
.venv/bin/python -m pytest tests/project/test_save_router.py -q
```

### 5.2 EditProjectSerializer 必须保存 compositions

`EditProjectSerializer._project_to_dict()` 当前保存了 `comp_clips`，但没有保存 `compositions`。如果这个 serializer 仍被任何路径使用，会造成合成图丢失。

实施：

1. 增加：

```python
"compositions": dict(project.compositions)
```

2. `_dict_to_project()` 增加：

```python
compositions=data.get("compositions", {})
```

3. 增加 roundtrip 测试。

### 5.3 `.topos` 建议新增 `composite/comp_clips.json`

当前 Comp Clip 数据主要存在 `EditProject.comp_clips` 和 media metadata。为了工程结构清晰，建议 `.topos` 中增加：

```text
composite/comp_clips.json
```

内容：

```json
{
  "comp_clip_id": {
    "comp_clip_id": "...",
    "asset_id": "...",
    "graph_id": "...",
    "name": "...",
    "source_kind": "timeline_clip_upgrade",
    "original_asset_id": "...",
    "original_timeline_id": "...",
    "original_clip_id": "..."
  }
}
```

实施：

1. 扩展 `ToposProjectIO.save(...)` 参数：

```python
full_comp_clips_data: dict[str, Any] | None = None
```

2. 写入：

```python
payload.set_module("composite/comp_clips.json", full_comp_clips_data)
```

3. `load_full_data()` 读取该模块。

4. `EditProjectIO.save_project()` 传入 `project.comp_clips`。

5. `EditProjectIO.load_project()` 合并：

```text
优先 composite/comp_clips.json
其次 project/edit legacy comp_clips
```

项目还在开发中，不需要旧项目兼容，但建议写清楚迁移边界，避免未来 AI 混用旧字段。

## 6. Edit 时间线 UI 交互优化

### 6.1 时间线 clip 必须有 Composite 状态

当前 `ClipItem` 没有 `composite_id` 字段，也没有 `is_comp_clip` / `comp_status` 状态。用户无法从时间线看出哪个片段启用了合成。

新增字段：

```python
composite_id: str | None = None
is_comp_clip: bool = False
comp_enabled: bool = False
comp_bypassed: bool = False
comp_cache_status: str = "none"  # none, dirty, rendering, ready, failed
```

`TimelineView` 创建 `ClipItem` 时传入：

```python
composite_id=clip.composite_id
is_comp_clip=(asset.media_type == "comp_clip")
```

### 6.2 视觉设计

当 clip 是 Comp Clip 或有 `composite_id` 时：

1. 背景色与普通视频略有区别。
2. 左上角类型标记显示 `C` 或 `COMP`。
3. 名称栏右侧显示小 `COMP` 标签。
4. 底部增加一条细线表示缓存状态：

```text
none      不显示
dirty     橙色细线
rendering 蓝色动画线
ready     绿色细线
failed    红色细线
```

5. 禁止大面积强颜色，避免影响剪辑判断。

修改文件：

- `src/features/edit/ui/timeline_items.py`
- `src/features/edit/ui/timeline_view.py`

建议方法：

```python
def _draw_comp_clip_badge(self, painter, clip_rect): ...
def _draw_comp_cache_status(self, painter, clip_rect): ...
```

调用位置：

在 `_draw_clip_type_icon()`、`_draw_effects_badge()` 附近统一绘制，避免和速度/透明度标签重叠。

### 6.3 右键菜单增加 Composite 子菜单

对 video/image/comp_clip 显示：

```text
Composite
  Open in Composite
  Convert to Comp Clip
  Bypass Composite
  Reset Composite
  Reveal Comp Clip in Media Pool
  Render Cache This Clip
```

规则：

- 普通 clip 无 composite：
  - `Open in Composite`
  - `Convert to Comp Clip`
- 普通 clip 有 temporary composite 但未 promote：
  - `Open in Composite`
  - `Convert to Comp Clip`
  - `Discard Composite Draft`
- Comp Clip：
  - `Open in Composite`
  - `Bypass Composite`
  - `Reset Composite`
  - `Reveal Comp Clip in Media Pool`
  - `Render Cache This Clip`

### 6.4 Reset Composite 语义

Reset 需要分两种：

#### A. Reset Clip Composite

用于从普通素材升级来的 Comp Clip。

效果：

1. 当前时间线 clip 恢复 `asset_id = original_asset_id`
2. `clip.composite_id = None`
3. 如果关联 Comp Clip asset 没有被其他 timeline clip 或媒体池手动保留引用，可以删除：
   - `MediaAsset(comp_clip)`
   - `CompClipData`
   - `CompositeGraphData`
4. 刷新 media pool 与 timeline

#### B. Reset Comp Clip Graph

用于媒体池中的 Comp Clip。

效果：

1. 不删除 MediaAsset
2. 不改变 timeline clip asset
3. 重置 graph 为默认图：
   - timeline source comp：`TimelineClipInput -> Output`
   - empty comp：`Constant -> Output`
4. 清空 render cache

两个命令都必须支持 undo/redo，至少第一阶段要通过项目 command stack 或明确记录不可 undo。

验证：

```bash
.venv/bin/python -m pytest tests/edit/test_comp_clip_reset.py -q
```

手动验证：

1. 普通 clip 打开 Composite
2. 添加 Blur，确认时间线出现 COMP 标签
3. 右键 Reset Composite
4. 标签消失，画面恢复普通视频
5. 媒体池中临时 Comp Clip 被删除或标记未使用
6. 保存重开后状态一致

## 7. Composite 主视图与节点预览图性能

### 7.1 当前风险

`CompositeRenderer._on_frame_ready()` 后会调用：

```python
self._request_thumbnails_for_visible_nodes()
```

节点缩略图读取逻辑会走：

- cache lookup
- GPU downsample
- `fbo.read(...)`
- numpy reshape
- bytes signal
- UI QPixmap 更新

这对静态编辑可以接受，但对播放、拖动参数、节点多时会明显影响性能。

### 7.2 优化规则

节点缩略图必须有预算：

```text
播放中：默认不更新节点缩略图
参数拖动中：最多更新 selected node，频率 <= 5 fps
静止状态：更新 visible nodes，频率 <= 10 fps
节点很多：优先 selected、viewer node、Output 上游 1 层
```

### 7.3 实施步骤

新增配置：

```python
CompositeThumbnailPolicy:
    enabled: bool = True
    update_during_playback: bool = False
    max_updates_per_second_idle: int = 10
    max_updates_per_second_interaction: int = 5
    max_nodes_per_tick: int = 4
    selected_first: bool = True
```

新增状态：

```python
self._thumbnail_last_request_at
self._thumbnail_pending_nodes
self._thumbnail_playback_suspended
```

修改：

1. `_request_thumbnails_for_visible_nodes()` 加时间节流。
2. 每次只提交有限数量节点。
3. 缓存 key 未变化时不 readback。
4. 播放时只读已有 thumbnail cache，不触发 readback。
5. 参数拖动时只更新选中节点。
6. Output 主视图渲染优先级高于 thumbnail readback。

验证：

```bash
.venv/bin/python -m pytest tests/composite/test_node_thumbnail_policy.py -q
.venv/bin/python -m pytest tests/composite/test_composite_cache.py -q
```

手动性能验证：

1. 创建 20 个节点的 graph
2. 开启节点缩略图
3. 拖动 Blur radius
4. 主视图不得明显卡住
5. 播放 Edit 时间线时，Composite 节点缩略图不得持续 readback

## 8. Edit 回放与导出必须使用 Composite 输出

当前 `timeline_preview_renderer.py` 和 `timeline_export.py` 对 `comp_clip` 的处理是：

```text
解析 original_asset_id -> 使用原始素材帧
```

这只能保证不崩溃，但不会应用 Composite graph。最终用户回到 Edit 播放时看不到合成效果。

### 8.1 新增 CompositeFrameRenderer 服务

新增：

```text
src/features/composite/render/composite_frame_renderer.py
```

职责：

```python
render_comp_clip_frame(
    graph_id: str,
    timeline_id: str | None,
    clip_id: str | None,
    timeline_frame: int,
    quality: str,
) -> TextureResult
```

要求：

- 不依赖 CompositePage 是否打开
- 不依赖 UI viewer
- 使用同一套 GraphEvaluator
- 使用 TimelineClipFrameProvider 作为输入
- 输出 GPU texture

### 8.2 Edit timeline preview 集成

修改 `TimelinePreviewRenderer`：

1. 遇到普通 video/image clip 且 `clip.composite_id` 存在：

```text
source frame -> ColorStack -> CompositeFrameRenderer -> timeline compositor
```

2. 遇到 `asset.media_type == "comp_clip"`：

```text
comp_asset.metadata.graph_id -> CompositeFrameRenderer
```

3. 如果缓存可用，优先使用 render cache。

4. 如果 graph 无法实时渲染：

```text
显示上一帧 cache 或 debug placeholder
同时标记 cache dirty
```

### 8.3 Export 集成

修改 `timeline_export.py`：

- `comp_clip` 不得直接回源 original asset 作为最终输出。
- Export 必须调用 CompositeFrameRenderer 的 full/export 模式。
- 允许在内部使用 source asset 作为 TimelineClipInput 的输入，但最终像素必须经过 Composite graph。

验证：

```bash
.venv/bin/python -m pytest tests/edit/test_comp_clip_timeline_preview.py -q
.venv/bin/python -m pytest tests/edit/test_comp_clip_timeline_export.py -q
```

手动验证：

1. 在 Composite 给 clip 加一个明显 Transform 或 Blur
2. 回 Edit 播放
3. Program Viewer 必须显示合成结果
4. 导出单帧必须显示合成结果

## 9. Render Cache 状态接入

这是后续性能优化，但 UI 状态要先预留。

新增数据：

```python
RenderCacheStatus:
    clip_id: str
    graph_id: str | None
    status: "none" | "dirty" | "rendering" | "ready" | "failed"
    cached_range: tuple[int, int] | None
    codec: str
    resolution: tuple[int, int]
```

触发 dirty：

- ColorStack 修改
- Composite graph 修改
- timeline clip trim/move/speed 修改
- source asset relink/proxy 改变
- render cache preferences 改变

Edit 时间线显示：

- dirty：橙色
- rendering：蓝色
- ready：绿色
- failed：红色

Preferences 后续补：

- codec
- container
- pixel format
- resolution policy
- cache directory
- max cache size
- playback auto-cache mode

## 10. AI 执行顺序建议

按这个顺序让 AI 实施，避免越修越乱：

1. P0 默认图端口 + load connection 校验
2. P0 render-side `timeline_clip_input` 实例化
3. P0 current composite id/save target 修复
4. P0 SaveRouter 保留 `comp_clips`
5. P0 `promote_composition_to_comp_clip()` 原地提升
6. P0 CompositePage edit signal -> promote
7. P0 TimelineClipFrameProvider 首版可显示纹理
8. P1 时间线 COMP 标签、菜单、重置
9. P1 节点缩略图节流
10. P1 CompositeFrameRenderer 服务
11. P1 Edit playback/export 使用 Composite 输出
12. P2 Render Cache 状态线与 Preferences

每完成一个阶段必须跑对应测试，不要一次性大改。

## 11. 最小验收清单

### 黑屏修复验收

- 从 Edit 打开普通视频 clip 到 Composite，默认图主视图不黑屏。
- 断开 Output 输入，主视图可以清空，但状态栏提示 output disconnected。
- 重新连接 Output，主视图恢复。
- 节点编辑器显示的连接线数量和 `NodeGraph.connections` 数量一致。

### Comp Clip 生命周期验收

- 仅打开 Composite 不创建媒体池 Comp Clip。
- 添加第一个真实节点后，时间线 clip 变成 Comp Clip。
- 媒体池出现 Comp Clip。
- 当前 clip 显示 `COMP` 状态。
- 保存重开后 Comp Clip 仍存在。
- Reset Composite 后状态按规则恢复。

### Color / Composite 链路验收

- Color 页面对同一 clip 做曝光变化。
- Composite 默认输入显示调色后的画面。
- Composite 加 Blur。
- 回 Edit 播放看到调色 + Blur 后的结果。

### 性能验收

- 播放 Edit 时间线时不持续触发 Composite 节点缩略图 readback。
- 拖动 Composite 参数时主视图优先更新，缩略图节流。
- 20 节点 graph 的 UI 不明显卡顿。

### 保存验收

`.topos` 至少包含：

```text
edit/media_pool.json
edit/timeline_sequences.json
color/color_stacks.json
composite/graphs.json
composite/comp_clips.json
```

重新打开后：

- timeline clip 的 `asset_id`
- `composite_id`
- Comp Clip asset metadata
- `CompClipData`
- graph_id
- ColorStack

全部一致。

## 12. 不建议现在做的事

这些可以晚一点，不要阻塞 P0：

- 全部节点都做完实时 GPU 优化
- 完整 Render Cache 编码器
- Deliver 完整复用 Comp Clip cache
- 复杂 Fusion 风格多输入 MediaIn/MediaOut 管理
- 旧项目兼容迁移

当前项目还在开发中，不需要旧兼容。优先把新设计的数据闭环做正确。

