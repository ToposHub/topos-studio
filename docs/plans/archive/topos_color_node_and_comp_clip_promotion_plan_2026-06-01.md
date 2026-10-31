# Topos Color 节点化与 Comp Clip 自动升级实施计划

创建日期：2026-06-01

适用范围：

- Edit 时间线片段进入 Composite
- Composite 默认图从 `COLOR -> Output` 改为稳定的素材源图
- 将当前 `COLOR` 节点升级/改名为 `Topos Color` 节点
- 支持多个 `Topos Color` 节点，作为调色栈/调色预设处理节点
- Composite 页面发生真实合成编辑后，将当前时间线片段升级为 `Comp Clip`
- 保证直接从 Edit 切到 Composite 也一定有画面

## 0. 核心结论

这个设计是合理的，而且应该作为 Edit / Color / Composite 最终打通的主方向。

当前 `COLOR -> Output` 的设计有一个根本缺陷：

- `COLOR` 节点依赖 Color 页面当前缓存的 `ColorOutput texture`
- 如果用户没有先打开 Color 页面，或 Color 当前显示的不是这个时间线片段，Composite 就可能拿不到正确画面
- 多时间线、多 clip、后台导出、渲染缓存都不应该依赖“当前页面刚好有一个缓存 texture”

新的稳定设计应该是：

```text
无调色：
Timeline Clip Input -> Output

有调色：
Timeline Clip Input -> Topos Color -> Output

有合成：
Timeline Clip Input -> Topos Color -> Composite Nodes -> Output
```

其中：

- `Timeline Clip Input` 是时间线片段素材的唯一入口
- `Topos Color` 是一个普通 Composite 节点，负责应用调色栈或调色预设
- Color 页面是编辑 `ColorStack` 的 UI，不再是 Composite 的唯一输入源
- Composite 页面做任何真实合成处理后，当前时间线片段自动升级为 `Comp Clip`

## 1. 目标体验

### 1.1 从 Edit 直接进入 Composite

用户在 Edit 时间线选择普通视频 clip，点击进入 Composite。

预期：

```text
Timeline Clip Input -> Output
```

主预览立即显示该 clip 当前帧，不要求用户先进入 Color 页面。

### 1.2 已调色片段进入 Composite

如果该 clip 已有 `color_stack_id`：

```text
Timeline Clip Input -> Topos Color -> Output
```

`Topos Color` 节点读取 clip 的 `ColorStack`，在 Composite render thread 中执行调色。

### 1.3 多个 Topos Color 节点

用户可以在 Composite 里创建多个 `Topos Color` 节点：

```text
Timeline Clip Input -> Topos Color A -> Blur -> Topos Color B -> Output
```

用途：

- 第一个 `Topos Color` 表示 clip 原始调色
- 后续 `Topos Color` 可以作为风格化调色预设
- 其它素材、图片、生成节点也可以连接到 `Topos Color`

### 1.4 删除 Topos Color 节点

必须区分两个动作：

1. 删除节点：只从当前 Composite 图中移除该处理节点，图会自动重连上下游，素材仍然显示。
2. Reset Grade / 删除调色数据：明确清空或删除对应 `ColorStack`。

默认删除节点不要直接删除 `ColorStack` 数据，避免误删用户调色。

### 1.5 Composite 编辑自动升级 Comp Clip

当用户在 Composite 页面对当前 Edit clip 做真实合成编辑：

- 添加非默认节点
- 删除节点
- 修改节点参数
- 修改连接
- 粘贴节点
- 添加 mask/roto/transform/keyer 等

当前时间线 clip 自动升级为 `Comp Clip`。

升级后：

- 时间线片段显示 `Comp Clip` 标签
- 片段颜色/角标与普通视频不同
- 媒体池出现对应 `Comp Clip` 资产
- 双击媒体池 Comp Clip 可打开 Composite
- Edit 回放该 clip 时应走 Comp Clip 渲染缓存或 Composite frame renderer

## 2. 需要调整的概念模型

### 2.1 废弃旧 COLOR 节点语义

当前 `COLORNode` / `color_input` 的语义是：

```text
读取 Color 页面当前输出 texture
```

这个语义不适合时间线工程，应废弃。

### 2.2 新增 Topos Color 节点

新节点建议：

```python
node_type = "topos_color"
node_title = "Topos Color"
```

输入输出：

```text
input:
  image: image

output:
  result: image
```

参数建议：

```json
{
  "stack_source": "clip_stack | preset | embedded",
  "color_stack_id": "",
  "preset_id": "",
  "embedded_stack": null,
  "bypass": false,
  "intensity": 1.0
}
```

说明：

- `clip_stack`：使用当前 timeline clip 的 `color_stack_id`
- `preset`：使用媒体池/项目里的调色预设
- `embedded`：节点内部保存一份独立调色栈快照
- `intensity`：可选，控制调色混合强度

### 2.3 Timeline Clip Input 节点

继续保留：

```python
node_type = "timeline_clip_input"
node_title = "Timeline Clip Input"
```

它只负责输出原始素材帧或指定阶段帧。

建议第一阶段固定为：

```json
{
  "input_stage": "source_raw"
}
```

不要再让 `Timeline Clip Input` 自己输出 `after_color`，否则职责会和 `Topos Color` 节点重叠。

### 2.4 Color 页面与 Topos Color 节点的关系

Color 页面不是渲染输入源，而是编辑 `ColorStack` 的 UI。

关系如下：

```text
Edit Clip.color_stack_id
        |
        v
Color Page edits ColorStack
        |
        v
Topos Color node applies ColorStack inside Composite renderer
```

## 3. 数据模型实施步骤

### Phase 1：新增 Topos Color 节点数据结构

目标：建立节点类型，不接渲染也可以先序列化/反序列化。

涉及文件：

- `src/features/composite/nodes/`
- `src/features/composite/core/node_engine.py`
- `src/features/composite/core/node_capabilities.py`
- `src/features/composite/ui/node_search_popup.py`
- `src/features/composite/ui/graph_node_search_popup.py`

实施步骤：

1. 新建文件：

```text
src/features/composite/nodes/topos_color_node.py
```

2. 定义 `ToposColorNode(Node)`：

```python
class ToposColorNode(Node):
    node_type = "topos_color"
    node_title = "Topos Color"
    header_color = "#cc6600"
```

3. 定义端口：

```python
self.inputs = [Port("image", "input", "image")]
self.outputs = [Port("result", "output", "image")]
```

4. 定义参数：

```python
self.parameters = {
    "stack_source": "clip_stack",
    "color_stack_id": "",
    "preset_id": "",
    "embedded_stack": None,
    "bypass": False,
    "intensity": 1.0,
}
```

5. 注册节点：

在 `NODE_REGISTRY` 中添加：

```python
"topos_color": ToposColorNode
```

6. 在节点分类中添加到 Color/Adjustments 分类。

7. 在 `node_capabilities.py` 中声明：

- 不是 source 节点
- 是 GPU effect 节点
- 输入 image，输出 image
- 拥有或不拥有输出 texture，按现有 effect 节点规则设置

验证方法：

- 新建空 NodeGraph，添加 `ToposColorNode`，保存为 dict，再 from_dict 恢复。
- 节点搜索中能找到 `Topos Color`。
- 节点 UI 中显示输入 `image` 和输出 `result`。

建议测试：

```text
tests/composite/test_topos_color_node_contract.py
```

测试项：

- 默认参数正确
- 端口正确
- to_dict/from_dict 不丢字段
- NODE_REGISTRY 能创建节点

## 4. Composite 默认图改造

### Phase 2：普通 clip 默认图

目标：从 Edit 切到 Composite 时，不再创建 `COLOR -> Output`。

涉及文件：

- `src/features/edit/core/composite_graph_data.py`
- `src/features/composite/core/node_engine.py`
- `src/app/controllers/page_controller.py`
- `src/app/controllers/composite_controller.py`

规则：

如果 clip 没有调色栈：

```text
Timeline Clip Input.output -> Output.image
```

如果 clip 有调色栈：

```text
Timeline Clip Input.output -> Topos Color.image
Topos Color.result -> Output.image
```

实施步骤：

1. 修改 `CompositeGraphData.create_default_for_clip(...)`。
2. 默认创建 `timeline_clip_input` 节点。
3. 判断 `color_stack_id` 是否存在。
4. 如果存在，创建 `topos_color` 节点：

```json
{
  "type": "topos_color",
  "parameters": {
    "stack_source": "clip_stack",
    "color_stack_id": "<clip.color_stack_id>",
    "bypass": false,
    "intensity": 1.0
  }
}
```

5. 如果不存在，不创建 `topos_color`。
6. 确保 Output 输入端口使用 `"image"`，不能写成 `"input"`。

验证方法：

- 未调色 clip 创建 Composite，图中只有 `timeline_clip_input` 和 `output`。
- 已调色 clip 创建 Composite，图中有 `timeline_clip_input`、`topos_color`、`output`。
- 所有连接都能通过 `NodeGraph.connect()` 返回 `True`。
- `NodeGraph.has_connected_output()` 为 `True`。

建议测试：

```text
tests/composite/test_default_graph_topos_color_contract.py
```

测试项：

- no color stack -> source -> output
- has color stack -> source -> topos_color -> output
- graph reload 后连接仍存在
- output 端口名必须为 `image`

## 5. Topos Color 渲染实现

### Phase 3：Topos Color GPU 求值

目标：让 `Topos Color` 节点在 Composite renderer 中真正应用 ColorStack。

涉及文件：

- `src/features/composite/nodes/topos_color_node.py`
- `src/features/composite/evaluators/base_evaluator.py`
- `src/features/composite/evaluators/color_evaluator.py`
- `src/features/color/core/shader_compiler.py`
- `src/features/color/core/image_engine.py`
- `src/features/edit/core/project.py`
- `src/features/edit/core/color_stack_data.py` 或实际 ColorStack 定义文件

建议实现方式：

第一阶段不要把整个 Color 页面搬进 Composite。

应抽出一个纯服务：

```text
ColorStackRenderService
```

职责：

- 输入：ModernGL context、input TextureResult、ColorStack 数据、render size
- 输出：TextureResult
- 不依赖 QWidget
- 不依赖当前 Color 页面 canvas
- 不依赖 `engine.color_output`

建议文件：

```text
src/features/color/core/color_stack_render_service.py
```

接口建议：

```python
class ColorStackRenderService:
    def render_stack(
        self,
        ctx,
        input_texture: TextureResult,
        color_stack: Any,
        fb_manager,
        resource_pool=None,
        intensity: float = 1.0,
        render_mode: str = "preview",
    ) -> TextureResult:
        ...
```

实施步骤：

1. 找出现有 Color 页面应用调色的核心路径。
2. 将“根据 ColorStack 编译 shader / 设置 uniforms / 渲染到 texture”的逻辑抽到纯服务。
3. `ToposColorNode._do_evaluate()` 不直接渲染复杂逻辑，只返回 effect marker 或调用统一 evaluator。
4. 在 evaluator 中处理 `topos_color`：

```text
input image -> ColorStackRenderService -> result
```

5. `stack_source=clip_stack` 时，通过 runtime context 找当前 clip 的 `color_stack_id`。
6. `stack_source=preset` 时，从项目 preset registry 找 `preset_id`。
7. `stack_source=embedded` 时直接读取节点内 `embedded_stack`。
8. `bypass=True` 时直接返回输入。

验证方法：

- 一个红色/灰阶测试图经过简单曝光调色后输出像素变化正确。
- `bypass=True` 时输出与输入一致。
- 同一个源后面串联两个 `Topos Color` 节点，两个节点都生效。
- 没有打开 Color 页面时，Composite 仍能渲染调色。

建议测试：

```text
tests/composite/test_topos_color_render_service.py
tests/composite/test_topos_color_node_evaluation.py
```

## 6. 调色预设与多个 Topos Color 节点

### Phase 4：Topos Color 作为调色预设节点

目标：用户可以把 `Topos Color` 节点作为调色预设加载到任何素材链路中。

数据模型建议：

```json
{
  "color_presets": {
    "preset_001": {
      "preset_id": "preset_001",
      "name": "Warm Film",
      "stack": {}
    }
  }
}
```

实施步骤：

1. 在项目模型中增加 `color_presets` 字段，或复用现有 preset 系统。
2. `Topos Color` 节点属性面板增加：

- Stack Source 下拉：Clip Stack / Preset / Embedded
- Preset 选择器
- Save as Preset
- Bake to Embedded Stack
- Open in Color Page

3. 当用户选择 Preset：

```json
"stack_source": "preset",
"preset_id": "preset_001"
```

4. 当用户选择 Embedded：

```json
"stack_source": "embedded",
"embedded_stack": { ...完整 ColorStack 快照... }
```

验证方法：

- 同一个 preset 被两个 Topos Color 节点引用，修改 preset 后两个节点一起变化。
- Embedded 节点不受 preset 修改影响。
- 删除 preset 时，引用该 preset 的节点显示 Missing Preset 状态，但不崩溃。

## 7. 删除、绕过、重置语义

### Phase 5：删除 Topos Color 节点时保持画面

目标：删除调色节点后，素材仍然连接到 Output。

交互规则：

如果删除的 `Topos Color` 节点满足：

```text
上游只有一个 image 输入
下游有一个或多个 image 输出连接
```

删除时自动重连：

```text
Before:
A -> Topos Color -> B

After:
A -> B
```

实施步骤：

1. 在 node graph 删除节点命令中增加 `delete_and_reconnect` 逻辑。
2. 仅对单输入单输出 image 节点自动重连。
3. 对复杂多输入节点不自动重连，只删除连接。
4. 删除 `Topos Color` 节点时弹出轻量确认或提供 undo。
5. 右键菜单提供：

- Delete Node
- Bypass Node
- Reset Grade Data
- Open in Color Page

数据语义：

- `Delete Node`：不删除 `ColorStack`
- `Bypass Node`：设置 `bypass=True`
- `Reset Grade Data`：清空目标 `ColorStack`

验证方法：

- 删除默认 `Topos Color` 节点后，图变为 `Timeline Clip Input -> Output`。
- 删除后主预览仍显示素材。
- Undo 后 `Topos Color` 节点和连接恢复。
- Reset Grade 后 Color 页面对应 clip 回到默认状态。

## 8. Composite 编辑自动升级 Comp Clip

### Phase 6：定义“真实合成编辑”

目标：Composite 页面发生真实编辑时，当前 Edit clip 自动升级为 `Comp Clip`。

必须触发升级的操作：

- 添加节点
- 删除节点
- 修改非 viewer 状态的节点参数
- 创建/删除/修改连接
- 粘贴节点
- 添加 backdrop/group/sticky note 是否触发：建议不触发 Comp Clip，可只标记 graph dirty
- 只移动节点位置是否触发：建议不触发 Comp Clip，因为只是 UI 布局
- 只缩放 viewer/pan 是否触发：不触发

特殊规则：

- 如果用户只打开 Composite，但没有改任何节点，不升级为 Comp Clip。
- 如果用户只查看已有默认图，不升级。
- 如果用户在 Composite 里添加 `Topos Color`、Blur、Transform 等处理节点，升级。

实施步骤：

1. 在 `CompositePage` 中区分两类 dirty：

```python
graph_layout_dirty
graph_processing_dirty
```

2. 只有 `graph_processing_dirty=True` 时发出：

```python
composite_user_edit_started(composite_id)
```

3. `MainWindow / CompositeController` 接收信号后：

- 确认当前 context 有 `clip_id`
- 如果 clip 还不是 Comp Clip，则调用 promote
- 创建/更新 `CompClipData`
- 创建/更新 media pool asset
- 将 timeline clip 的 `asset_id` 指向 comp clip asset
- 保留 `original_asset_id`
- 保留 `source_clip_id`
- 保留 `color_stack_id`

4. 如果已经是 Comp Clip，只更新 graph modified time。

验证方法：

- 进入 Composite 不操作，不生成 media pool Comp Clip。
- 移动节点位置，不生成 Comp Clip。
- 添加 Blur 节点，生成 Comp Clip。
- 修改 Topos Color 参数，生成 Comp Clip。
- 删除处理节点后，如果图回到默认状态，仍保持 Comp Clip，除非用户执行“Convert Back to Source Clip”。

## 9. 时间线与媒体池 UI

### Phase 7：Comp Clip 可见反馈

目标：用户能清楚知道当前 clip 已经有合成。

时间线片段 UI：

- Comp Clip 使用独立颜色或顶部色条
- 显示 `Comp Clip` 标签
- 显示小图标
- 右键菜单增加：

```text
Open in Composite
Reset Composite
Render Cache
Convert Back to Source Clip
Reveal Comp Clip in Media Pool
```

媒体池：

- Comp Clip 作为 asset 显示
- 双击打开 Composite
- 可重命名
- 可显示缩略图
- 可拖回时间线

验证方法：

- 普通 clip 添加合成后，时间线 UI 立即变化。
- 媒体池出现 Comp Clip。
- 重启项目后状态仍保留。
- Reset Composite 后 UI 回到普通 clip 或保留 Comp Clip 但显示无效果，按产品决策执行。

## 10. 渲染缓存与播放接入

### Phase 8：Comp Clip 回到 Edit 后可播放

目标：Edit 时间线播放 Comp Clip 时，不实时完整计算复杂 Composite，而是优先使用缓存。

建议流水线：

```text
Decode source frame
-> Topos Color nodes
-> Composite graph
-> Render Cache
-> Edit timeline playback
```

实施步骤：

1. Preferences 增加 Render Cache 设置：

- 格式：ProRes / DNxHR / H.264 proxy / image sequence
- 分辨率：Full / Half / Quarter
- 色彩格式：8-bit / 10-bit / half-float
- 缓存目录
- 自动缓存开关

2. Comp Clip dirty 后标记缓存失效。
3. Edit 播放时：

- 如果缓存有效，直接播放缓存
- 如果缓存无效且复杂度低，尝试实时预览
- 如果无法实时，显示未缓存状态并提示渲染缓存

4. Deliver 导出时：

- 可选择使用缓存
- 或强制重新渲染最高质量 Composite

验证方法：

- 添加 Composite 节点后缓存失效。
- 渲染缓存后 Edit 播放使用缓存。
- 修改 Topos Color 后缓存重新失效。
- 删除 Composite 效果后缓存清理。

## 11. 工程文件保存

### Phase 9：保存 Topos Color 节点和 Comp Clip

目标：`.topos` 工程能完整保存新图结构。

必须保存：

- `CompositeGraphData.nodes`
- `CompositeGraphData.connections`
- `topos_color` 节点参数
- `color_stacks`
- `color_presets`
- `comp_clips`
- `media_assets` 中的 comp clip asset
- timeline clip 的 `asset_id` / `original_asset_id` / `composite_id` / `color_stack_id`

验证方法：

- 创建 clip，调色，进入 Composite，添加节点，保存项目。
- 关闭重开后：
  - Edit 时间线仍显示 Comp Clip
  - Composite 图仍是 `TimelineClipInput -> Topos Color -> nodes -> Output`
  - Topos Color 节点仍引用正确 ColorStack
  - 媒体池仍显示 Comp Clip

## 12. 迁移与清理

项目仍在开发中，不要求旧项目兼容，但建议保留一次性迁移，避免当前测试工程全坏。

迁移规则：

旧图：

```text
COLOR -> Output
```

迁移为：

```text
Timeline Clip Input -> Topos Color -> Output
```

如果找不到 timeline clip context：

```text
Timeline Clip Input -> Output
```

代码清理：

1. 停止在默认图中创建 `color_input`。
2. `COLORNode` 可保留为 deprecated alias，但不要在新图中使用。
3. 节点搜索中隐藏旧 `COLOR`。
4. 所有注释从 “Color page output texture” 改为 “Topos Color stack processor”。

验证方法：

- 新建项目不会出现 `color_input` 节点。
- 旧测试图可被迁移或明确失败。
- `rg "COLOR -> Output"` 不再出现在新逻辑说明中。

## 13. 推荐开发顺序

不要一次性全改。推荐按下面顺序给 AI 执行：

1. 新增 `ToposColorNode`，完成注册和纯数据测试。
2. 修改默认 graph 创建逻辑：source -> output / source -> topos color -> output。
3. 将 `TimelineClipInput` 固定为 source_raw 输入，确保直接进 Composite 有画面。
4. 抽 `ColorStackRenderService`，让 Topos Color 真正渲染调色。
5. 实现删除 Topos Color 自动重连。
6. 实现 Composite processing dirty 与 Comp Clip 自动升级。
7. 补时间线/媒体池 UI 标签和右键菜单。
8. 接入 render cache。
9. 完成保存/加载测试。
10. 清理旧 `COLORNode` 语义。

## 14. 验收清单

功能验收：

- [ ] 从 Edit 直接进入 Composite，未调色 clip 显示原素材。
- [ ] 已调色 clip 进入 Composite，自动显示 `Topos Color` 节点。
- [ ] 删除 `Topos Color` 节点后，素材仍连接到 Output。
- [ ] Reset Grade 会清空调色数据。
- [ ] 可以手动添加多个 `Topos Color` 节点。
- [ ] 其它图片/素材节点可以连接到 `Topos Color`。
- [ ] 添加 Composite 处理节点后，时间线 clip 自动升级为 Comp Clip。
- [ ] 媒体池显示 Comp Clip。
- [ ] 保存重开后 graph、ColorStack、Comp Clip 全部保留。
- [ ] Edit 播放 Comp Clip 时不崩溃，有缓存时走缓存。

技术验收：

- [ ] Composite 不再依赖 Color 页面当前 `color_output texture` 作为唯一输入。
- [ ] Composite renderer 可以在没有打开 Color 页面时应用调色。
- [ ] `Topos Color` 节点渲染不依赖 QWidget。
- [ ] 默认图所有连接端口真实存在。
- [ ] 所有新增节点都有序列化测试。
- [ ] 所有 Comp Clip promotion 操作可 undo 或至少可 reset。

## 15. 给执行 AI 的注意事项

1. 不要把 Color 页面当前 framebuffer 当作 Composite 输入。
2. 不要让 `TimelineClipInput` 同时负责 source 和 color，调色必须由 `Topos Color` 节点负责。
3. 不要在删除 `Topos Color` 节点时默认删除 ColorStack 数据。
4. 不要因为用户进入 Composite 页面就立刻升级 Comp Clip，必须发生真实处理编辑。
5. 不要破坏图片 standalone Color 流程；图片仍可单独调色。
6. 每个 Phase 完成后必须跑对应测试，再进入下一 Phase。

