# Edit / Color / Composite 打通实施方案：Comp Clip 成为工程内一等素材

日期：2026-05-31  
适用项目：Topos Color  
技术栈：PyQt6 + ModernGL + GLSL  
目标读者：后续执行开发任务的 AI / 工程师  

---

## 0. 总目标

当前 Edit 与 Color 已经基本打通，下一步要把 Composite 从“独立节点页面”升级为“时间线与媒体池共同管理的合成片段编辑器”。

最终产品规则：

1. **Edit 时间线仍然是视频时序的唯一真相源。**
   - 视频素材必须先进入 Edit 工程。
   - 视频调色、视频合成、视频导出都从 `timeline_id + clip_id + frame` 出发。
   - Composite 页面不能再创建自己的视频时间线。

2. **Color 是 clip 的调色阶段。**
   - 视频 Color 只处理时间线上的 `ClipInstance`。
   - 图片可以继续 standalone 打开 Color。
   - 时间线图片 clip 也可以按 `timeline_id + clip_id` 调色。

3. **Composite 是 Comp Clip 的节点编辑器。**
   - 用户对普通时间线 clip 进入 Composite 并真正添加节点、修改节点、保存合成时，该 clip 自动升级为 `Comp Clip`。
   - `Comp Clip` 是媒体池中的一等素材，可以重命名、显示缩略图、双击打开 Composite、拖到时间线。
   - 媒体池可以创建空白 `Comp Clip`。

4. **图片不默认变成 Comp Clip。**
   - 普通图片仍然是 `ImageAsset`。
   - 单独打开图片调色时继续使用 standalone image `.topos`。
   - 只有图片进入 Composite 节点合成流程时，才生成 `Comp Clip`。

5. **不考虑旧项目兼容。**
   - 项目仍在开发中，允许升级 `.topos` schema。
   - 可以删除旧的、不兼容的 `single_asset video composite` 语义。
   - 只保留清晰的新模型。

---

## 1. 术语定义

### 1.1 Source Asset

原始素材，来自用户导入的文件。

示例：

- `VideoAsset`：视频文件。
- `ImageAsset`：图片文件。
- `AudioAsset`：音频文件。
- `ImageSequenceAsset`：图片序列。

当前代码中对应：

- `src/features/edit/core/project.py::MediaAsset`
- `media_type in ("image", "video", "audio", "image_sequence")`

### 1.2 Timeline Clip

时间线上的片段实例。

当前代码中对应：

- `src/features/edit/core/timeline_v2.py::ClipInstance`

核心字段：

- `clip_id`
- `asset_id`
- `timeline_start`
- `timeline_duration`
- `source_start`
- `source_duration`
- `speed`
- `color_stack_id`
- `composite_id`

### 1.3 Comp Clip

新增概念。Comp Clip 是媒体池中的一等素材，代表一个可剪辑的合成片段。

它不是普通文件，不一定有真实磁盘源文件。它的画面来自 Composite node graph 的输出。

Comp Clip 可以有两种来源：

1. 从时间线普通 clip 升级而来。
2. 在媒体池中新建空白合成片段。

### 1.4 Comp Clip Asset

Comp Clip 在媒体池中的资产记录。

建议扩展 `MediaAsset.media_type`，新增：

```python
"comp_clip"
```

Comp Clip Asset 只作为媒体池索引，完整图数据仍然保存在：

```text
composite/graphs.json
```

### 1.5 Composite Graph

合成节点图。

当前代码中对应：

- `src/features/edit/core/composite_graph_data.py::CompositeGraphData`
- `src/features/composite/core/node_engine.py::NodeGraph`
- `src/features/composite/ui/composite_page.py`

### 1.6 Standalone Image Project

图片单独调色工程。

它仍然允许独立保存 `.topos`，但只用于图片。

视频不允许 standalone Color，也不允许 standalone Composite。

---

## 2. 最终对象关系

### 2.1 普通视频 clip 升级为 Comp Clip

升级前：

```text
MediaPool
  asset_v001: media_type="video", path="/shot.mov"

Timeline V1
  clip_001.asset_id = asset_v001
  clip_001.color_stack_id = stack_001
  clip_001.composite_id = None
```

用户在 `clip_001` 上打开 Composite 并添加节点后：

```text
MediaPool
  asset_v001: media_type="video", path="/shot.mov"
  comp_asset_001: media_type="comp_clip", graph_id="graph_001"

Timeline V1
  clip_001.asset_id = comp_asset_001
  clip_001.color_stack_id = stack_001 或新的 comp local stack
  clip_001.composite_id = graph_001

Composite Graph
  graph_001.source_kind = "timeline_clip_upgrade"
  graph_001.comp_asset_id = comp_asset_001
  graph_001.source_refs.original_asset_id = asset_v001
  graph_001.source_refs.original_clip_id = clip_001
```

说明：

- 时间线 clip 变成引用 `comp_asset_001`。
- 原始视频素材仍然保留在媒体池，不删除。
- Composite graph 内部用 `TimelineClipInput` 或 `MediaIn` 节点读取原始 clip 的已调色画面。

### 2.2 媒体池空白创建 Comp Clip

```text
MediaPool
  comp_asset_002: media_type="comp_clip", graph_id="graph_002"

Composite Graph
  graph_002.source_kind = "empty_comp_clip"
  graph_002.width = project.width
  graph_002.height = project.height
  graph_002.fps = project.fps
  graph_002.duration_frames = default_duration
```

默认图：

```text
Background / EmptyInput
  -> MediaOut
```

用户可将 `comp_asset_002` 拖到时间线，形成普通 `ClipInstance`。

### 2.3 图片处理规则

普通图片：

```text
MediaAsset(media_type="image")
```

用途：

- 可单独打开 Color。
- 可保存 standalone image `.topos`。
- 可拖到时间线。
- 可作为 Composite 输入。
- 不自动变成 Comp Clip。

图片进入 Composite 后：

```text
MediaAsset(media_type="comp_clip")
source_refs.original_asset_id = image_asset_id
```

---

## 3. 当前代码基础与必须修正的问题

### 3.1 已有基础

可复用的现有模块：

- `src/features/edit/core/project.py`
  - `MediaAsset`
  - `EditProject`
  - `EditProject.compositions`

- `src/features/edit/core/timeline_v2.py`
  - `ClipInstance.composite_id`
  - `ClipInstance.with_composite_id(...)`

- `src/features/edit/core/composite_graph_data.py`
  - `CompositeGraphData`
  - `create_default_for_clip(...)`

- `src/features/composite/ui/composite_page.py`
  - `load_composite_data(...)`
  - `get_composite_data(...)`

- `src/shared/project/topos_project.py`
  - `CompositionRef`
  - `active_composition_id`
  - `composite/graphs.json`

- `src/shared/project/save_router.py`
  - Composite 保存入口。

### 3.2 必须优先修正的问题

#### P0-1：`create_composition()` 的 composite_id 不一致风险

当前 `EditProject.create_composition(composite_id=...)` 接收外部 ID，但 `CompositeGraphData.create_default_for_clip()` 内部重新生成 `composite_id`。

风险：

```text
edit_project.compositions["A"] = {"composite_id": "B"}
```

后果：

- 保存时 active composition 错乱。
- 媒体池 Comp Clip 绑定错误。
- 双击媒体池打开时找不到正确 graph。

修复要求：

1. 修改 `CompositeGraphData.create_default_for_clip(...)`，增加参数：

```python
composite_id: str | None = None
```

2. 返回对象时使用传入 ID：

```python
composite_id=composite_id or str(uuid.uuid4())
```

3. 修改 `EditProject.create_composition(...)`，传入外部 `composite_id`。

4. 增加测试：

```python
project = project.create_composition(composite_id="comp_A", ...)
assert project.compositions["comp_A"]["composite_id"] == "comp_A"
```

#### P0-2：`MediaAsset.media_type` 不支持 `comp_clip`

当前 `MediaAsset.__post_init__` 只允许：

```python
("image", "video", "audio", "image_sequence")
```

修复要求：

1. 增加 `"comp_clip"`。
2. 所有 UI badge、拖拽、时间线投放、序列化都识别 `comp_clip`。
3. `comp_clip` 在时间线中应被当作视频类素材，可以放到视频轨。

#### P0-3：Composite 默认图不能继续使用 `image_input` 作为视频主输入

当前 `create_default_for_clip()` 默认生成：

```text
image_input -> output
```

这会绕过：

- timeline frame 到 source frame 的映射。
- clip in/out。
- speed。
- ColorStack。
- 代理/原片解码策略。

修复要求：

新增节点：

```text
timeline_clip_input
```

或者：

```text
comp_media_in
```

第一阶段推荐命名：

```python
TimelineClipInputNode
node_type = "timeline_clip_input"
```

默认图改为：

```text
TimelineClipInput -> Output
```

空白 Comp Clip 默认图改为：

```text
EmptyInput / Background -> Output
```

#### P0-4：Composite 页面不能把当前 Color 页面缓存当作唯一输入

当前 `COLORNode` 读取 Color 页面当前输出纹理。这种方式只适合临时页面预览，不适合时间线多 clip。

问题：

- 当前 Color 页面只可能有一个当前 clip。
- Edit 时间线多轨播放时不能依赖页面状态。
- 导出时可能没有打开 Color 页面。

修复要求：

建立运行时输入服务：

```python
CompositeRuntimeContext
TimelineClipFrameProvider
```

Composite 节点请求输入帧时，必须通过 context 获取：

```text
timeline_id
clip_id
timeline_frame
clip_local_frame
source_frame
quality
```

---

## 4. 新数据模型设计

### 4.1 扩展 MediaAsset

文件：

```text
src/features/edit/core/project.py
```

新增 media_type：

```python
"comp_clip"
```

建议新增辅助属性：

```python
@property
def is_comp_clip(self) -> bool:
    return self.media_type == "comp_clip"
```

Comp Clip Asset 的 `path` 字段处理：

由于 `MediaAsset.__post_init__` 当前要求 `path` 非空，Comp Clip 没有真实文件路径。第一阶段可使用稳定虚拟路径：

```text
topos://comp_clip/{asset_id}
```

metadata 必须包含：

```python
{
    "graph_id": "graph_001",
    "width": 1920,
    "height": 1080,
    "fps": 24.0,
    "duration_frames": 240,
    "source_kind": "timeline_clip_upgrade" | "empty_comp_clip" | "image_comp_clip",
    "original_asset_id": "...",
    "original_clip_id": "...",
    "original_timeline_id": "...",
}
```

### 4.2 新增 CompClipData 或 CompClipRef

为了让媒体池和 graph 关系更清楚，建议新增数据类：

文件：

```text
src/features/edit/core/comp_clip_data.py
```

内容：

```python
@dataclass(frozen=True)
class CompClipData:
    comp_clip_id: str
    asset_id: str
    graph_id: str
    name: str
    width: int
    height: int
    fps: float
    duration_frames: int
    source_kind: str
    original_asset_id: str | None = None
    original_timeline_id: str | None = None
    original_clip_id: str | None = None
    created_at: float = 0.0
    modified_at: float = 0.0
```

第一阶段如果不想新增顶层表，也可以将这些字段放在 `MediaAsset.metadata`。但计划推荐新增 `CompClipData`，因为后续媒体池、缩略图、导出、重命名会更稳定。

### 4.3 扩展 EditProject

文件：

```text
src/features/edit/core/project.py
```

新增字段：

```python
comp_clips: dict[str, CompClipData] = field(default_factory=dict)
active_comp_clip_id: str | None = None
```

新增方法：

```python
def create_empty_comp_clip(...)
def create_comp_clip_from_timeline_clip(...)
def get_comp_clip_by_asset_id(...)
def get_comp_clip_by_graph_id(...)
def update_comp_clip(...)
def remove_comp_clip(...)
```

注意：

- 每个方法必须返回新的 `EditProject`，保持 frozen dataclass 风格。
- 新建 Comp Clip 时必须同步创建：
  - `MediaAsset(media_type="comp_clip")`
  - `CompClipData`
  - `CompositeGraphData`
  - 必要时 `CompositionRef`

### 4.4 扩展 CompositeGraphData

文件：

```text
src/features/edit/core/composite_graph_data.py
```

新增字段：

```python
comp_clip_id: str | None = None
comp_asset_id: str | None = None
source_kind: str = "timeline_clip_upgrade"
timeline_id: str | None = None
clip_id: str | None = None
source_refs: dict[str, Any] = field(default_factory=dict)
fps: float = 24.0
duration_frames: int = 0
```

允许的 `source_kind`：

```text
timeline_clip_upgrade
empty_comp_clip
image_comp_clip
external_media_node
```

不再使用旧的：

```text
single_asset video composite
```

### 4.5 更新 ToposProject / project.json 索引

文件：

```text
src/shared/project/topos_project.py
```

建议新增：

```python
CompClipRef
ToposProject.comp_clips: dict[str, CompClipRef]
ToposProject.active_comp_clip_id: str | None
```

`CompClipRef` 存放轻量索引：

```python
@dataclass(frozen=True)
class CompClipRef:
    comp_clip_id: str
    asset_id: str
    graph_id: str
    name: str
    width: int
    height: int
    fps: float
    duration_frames: int
    source_kind: str
```

完整数据可以保存到：

```text
composite/comp_clips.json
```

完整图仍保存到：

```text
composite/graphs.json
```

---

## 5. `.topos` 新存储结构

建议升级后结构：

```text
project.topos
├── manifest.json
├── project.json
├── edit/
│   ├── media_pool.json
│   ├── bins.json
│   ├── timelines.json
│   └── timeline_sequences.json
├── color/
│   └── color_stacks.json
├── composite/
│   ├── comp_clips.json
│   ├── graphs.json
│   └── thumbnails/
├── deliver/
│   └── presets.json
└── ui/
    └── workspace_state.json
```

### 5.1 project.json

必须包含：

```json
{
  "assets": {
    "comp_asset_001": {
      "asset_id": "comp_asset_001",
      "path": "topos://comp_clip/comp_asset_001",
      "media_type": "comp_clip",
      "display_name": "Comp Clip 1",
      "metadata": {
        "graph_id": "graph_001",
        "duration_frames": 240,
        "width": 1920,
        "height": 1080,
        "fps": 24.0
      }
    }
  },
  "comp_clips": {
    "comp_clip_001": {
      "asset_id": "comp_asset_001",
      "graph_id": "graph_001",
      "name": "Comp Clip 1"
    }
  },
  "active_comp_clip_id": "comp_clip_001"
}
```

### 5.2 composite/comp_clips.json

完整 Comp Clip 数据：

```json
{
  "comp_clip_001": {
    "comp_clip_id": "comp_clip_001",
    "asset_id": "comp_asset_001",
    "graph_id": "graph_001",
    "name": "Comp Clip 1",
    "width": 1920,
    "height": 1080,
    "fps": 24.0,
    "duration_frames": 240,
    "source_kind": "timeline_clip_upgrade",
    "original_asset_id": "asset_v001",
    "original_timeline_id": "timeline_001",
    "original_clip_id": "clip_001"
  }
}
```

### 5.3 composite/graphs.json

完整 CompositeGraphData：

```json
{
  "graph_001": {
    "composite_id": "graph_001",
    "comp_clip_id": "comp_clip_001",
    "comp_asset_id": "comp_asset_001",
    "source_kind": "timeline_clip_upgrade",
    "timeline_id": "timeline_001",
    "clip_id": "clip_001",
    "source_refs": {
      "original_asset_id": "asset_v001",
      "original_timeline_id": "timeline_001",
      "original_clip_id": "clip_001"
    },
    "nodes": [],
    "connections": []
  }
}
```

---

## 6. Composite 输入节点设计

### 6.1 新增 TimelineClipInputNode

文件：

```text
src/features/composite/nodes/timeline_clip_input_node.py
```

节点定义：

```python
class TimelineClipInputNode(Node):
    node_type = "timeline_clip_input"
    node_title = "Timeline Clip Input"
```

参数：

```python
parameters = {
    "timeline_id": "",
    "clip_id": "",
    "asset_id": "",
    "input_stage": "after_color",
}
```

输出：

```python
Port("image", "output", "image")
```

`input_stage` 允许：

```text
source_raw
after_color
after_clip_effects
```

第一阶段只实现：

```text
after_color
```

### 6.2 节点求值规则

`TimelineClipInputNode` 不直接打开视频文件，也不直接创建自己的解码器。

它必须从 Composite runtime context 获取当前帧：

```python
ctx.timeline_clip_provider.get_frame(
    timeline_id=timeline_id,
    clip_id=clip_id,
    timeline_frame=ctx.timeline_frame,
    input_stage="after_color",
    quality=ctx.quality,
)
```

返回值应是 GPU texture：

```python
TextureResult(
    texture_id=...,
    width=...,
    height=...,
    revision=...,
    format=...,
    color_space=...
)
```

### 6.3 注册节点

文件：

```text
src/features/composite/core/node_engine.py
```

添加：

```python
from src.features.composite.nodes.timeline_clip_input_node import TimelineClipInputNode

NODE_REGISTRY["timeline_clip_input"] = TimelineClipInputNode
NODE_CATEGORIES["Input"].append(("timeline_clip_input", TimelineClipInputNode))
```

### 6.4 默认图生成

从时间线 clip 创建 Comp Clip 默认图：

```text
TimelineClipInput -> Output
```

空白 Comp Clip 默认图：

```text
Background / Constant -> Output
```

图片创建 Comp Clip 默认图：

```text
ImageInput 或 TimelineClipInput -> Output
```

如果图片是媒体池图片但未放入时间线：

```text
ImageInput -> Output
```

如果图片来自时间线 still clip：

```text
TimelineClipInput -> Output
```

---

## 7. Runtime Context 与渲染链路

### 7.1 新增 CompositeRuntimeContext

文件：

```text
src/features/composite/core/runtime_context.py
```

建议定义：

```python
@dataclass(frozen=True)
class CompositeRuntimeContext:
    project_id: str
    graph_id: str
    comp_clip_id: str | None
    timeline_id: str | None
    clip_id: str | None
    timeline_frame: int
    clip_local_frame: int
    source_frame: int
    fps: float
    width: int
    height: int
    quality: str  # preview | proxy | master
    input_stage: str = "after_color"
```

### 7.2 新增 TimelineClipFrameProvider

文件：

```text
src/features/edit/core/timeline_clip_frame_provider.py
```

职责：

1. 根据 `timeline_id + clip_id + timeline_frame` 找到 clip。
2. 计算 `clip_local_frame`。
3. 计算 `source_frame`。
4. 解码源帧。
5. 应用 clip color stack。
6. 返回 GPU texture 给 Composite。

第一阶段实现可以简化：

- 只支持当前活动时间线。
- 只支持单个 clip 主输入。
- 只支持 `after_color`。
- 先复用现有 Color/Video GPU upload 代码。

严禁：

- 在 Composite 节点里直接 `VideoDecoder(path)`。
- 在 Composite 页面里创建单独播放时间线。
- 在导出时依赖 UI 当前页面纹理缓存。

### 7.3 Edit 预览渲染流程

目标流程：

```text
TimelineRenderPlan(frame)
  -> ClipLayerInfo[]
  -> for each clip:
       if clip.asset.media_type == "comp_clip":
           render_comp_clip_asset(...)
       elif clip.composite_id:
           render_clip_composite(...)
       else:
           render_clip_color_or_source(...)
  -> multi-track composite
  -> ProgramViewer
```

需要修改：

```text
src/features/edit/core/edit_gpu_preview_renderer.py
src/features/edit/core/timeline_preview_renderer.py
src/features/edit/core/timeline_render_plan.py
```

要求：

- `ClipLayerInfo` 增加或可访问 `composite_id`、`asset.media_type`。
- `comp_clip` 被当作视频层参与多轨合成。
- 有 composite graph 的 clip 输出必须替代原 source 输出。

### 7.4 Deliver 导出流程

Deliver 不能导出 Composite 页面当前缓存。

导出 timeline 时必须复用同一条逻辑：

```text
Timeline frame
  -> Clip Color
  -> Clip Composite / Comp Clip
  -> Multi-track timeline composite
  -> Encoder
```

需要检查：

```text
src/app/controllers/deliver_controller.py
src/shared/export/*
src/features/edit/core/timeline_export.py
```

验收：

- 不打开 Composite 页面也能导出包含 Comp Clip 的时间线。
- 切换页面不影响导出结果。

---

## 8. UI 交互设计

### 8.1 时间线右键菜单

文件：

```text
src/features/edit/ui/timeline_items.py
src/features/edit/ui/timeline_view.py
src/features/edit/ui/edit_page.py
src/app/controllers/edit_controller.py
```

普通视频/图片 clip 菜单：

```text
Open in Color
Open in Composite
Convert to Comp Clip
```

已有 Comp Clip 菜单：

```text
Open Comp Clip
Rename Comp Clip
Reveal in Media Pool
Decompose to Source Clip
```

第一阶段必须实现：

- `Open in Composite`
- 自动懒创建 Comp Clip
- `Reveal in Media Pool`

`Decompose to Source Clip` 可放到后续阶段。

### 8.2 懒创建规则

不要在用户仅仅切换 Composite 页面时创建 Comp Clip。

触发创建的时机：

1. 用户点击 `Convert to Comp Clip`。
2. 用户在普通 clip 的 Composite 页面里第一次执行以下操作：
   - 添加节点。
   - 删除节点。
   - 修改节点参数。
   - 连接/断开节点。
   - 保存 Composite graph。

实现建议：

Composite 页面进入时，如果普通 clip 没有 Comp Clip：

```text
进入临时 graph preview mode
```

一旦有编辑动作：

```text
ensure_current_comp_clip()
```

该函数负责：

- 创建 Comp Clip Asset。
- 创建 CompClipData。
- 创建 CompositeGraphData。
- 更新当前 timeline clip 的 `asset_id` 或 `composite_id`。
- 刷新媒体池。
- 刷新时间线。
- 设置 dirty。

### 8.3 媒体池显示

文件：

```text
src/features/edit/ui/media_pool_panel.py
```

新增：

- `comp_clip` 图标或 badge。
- 类型显示为 `Comp Clip`。
- 双击 `comp_clip` 打开 Composite。
- 右键菜单：
  - Open in Composite
  - Rename
  - Duplicate
  - Delete
  - Reveal Usage in Timeline

第一阶段必须实现：

- 显示。
- 双击打开 Composite。
- 拖到视频轨。
- 重命名。

### 8.4 媒体池创建空白 Comp Clip

入口：

```text
Media Pool 右键 -> New Comp Clip
```

弹窗字段：

- Name
- Width
- Height
- FPS
- Duration
- Background: Transparent / Black

第一阶段可以不做复杂弹窗，使用当前项目默认参数：

```text
Name: Comp Clip 1
Width/Height/FPS: active timeline
Duration: 5s
Background: transparent
```

### 8.5 Composite 页面标题与上下文

Composite 页面必须显示当前编辑对象：

```text
Comp Clip: Shot_010_Comp
Timeline: Main Timeline
Source: clip_001
```

不允许只有“Composite”页面标题，否则用户无法知道正在改哪个 Comp Clip。

---

## 9. 图片处理完整规则

### 9.1 普通图片 standalone Color

用户从文件系统打开图片到 Color：

```text
SourceKind = standalone_image
保存为 standalone image .topos
不创建 EditProject timeline
不创建 Comp Clip
```

### 9.2 图片导入媒体池

用户把图片导入 Edit 项目：

```text
MediaAsset.media_type = "image"
```

不创建：

- Comp Clip
- Composite Graph
- Timeline

### 9.3 图片拖到时间线

图片成为 still clip：

```text
ClipInstance.asset_id = image_asset_id
timeline_duration = 用户设置或默认 still duration
```

可以：

- Open in Color。
- Open in Composite。

### 9.4 时间线图片进入 Composite

如果用户对图片 timeline clip 进入 Composite 并编辑节点：

```text
创建 Comp Clip
source_kind = "image_comp_clip"
source_refs.original_asset_id = image_asset_id
source_refs.original_clip_id = clip_id
```

此时媒体池出现新的 `comp_clip`。

### 9.5 媒体池图片直接进入 Composite

建议第一阶段不支持媒体池图片直接 Open in Composite，避免图片未确定时间长度。

如果必须支持：

1. 弹出对话框要求用户确认：
   - 分辨率。
   - fps。
   - duration。
2. 创建 `image_comp_clip`。
3. 不修改原 ImageAsset。

---

## 10. 实施阶段计划表

### Phase 0：测试基线与风险隔离

目标：确认当前基础测试可运行，避免后续大改没有参照。

执行步骤：

1. 运行现有相关测试：

```bash
.venv/bin/python -m pytest tests/edit/test_composite_graph_data.py tests/composite tests/project -q
```

2. 记录失败项。
3. 不在 Phase 0 修复无关失败，只记录到计划尾部。

验收：

- 得到一份当前测试基线。
- 后续 Phase 修改只解释新增失败或修复失败。

### Phase 1：Schema 与数据模型升级

目标：让工程能表达 Comp Clip。

修改文件：

```text
src/features/edit/core/project.py
src/features/edit/core/comp_clip_data.py
src/features/edit/core/composite_graph_data.py
src/shared/project/topos_project.py
src/shared/project/schema_v1_spec.py
src/features/edit/core/serialization.py
src/features/edit/core/project_serializer.py
src/features/edit/core/edit_project_io.py
src/shared/project/topos_project_io.py
src/shared/project/topos_project_read_service.py
```

实施步骤：

1. `MediaAsset.media_type` 增加 `"comp_clip"`。
2. 新增 `CompClipData`。
3. `EditProject` 增加 `comp_clips` 与 `active_comp_clip_id`。
4. 增加创建/更新/删除 Comp Clip 的 immutable 方法。
5. `CompositeGraphData` 增加 Comp Clip 归属字段。
6. 修复 `create_default_for_clip()` composite_id 不一致问题。
7. `.topos` 写入 `composite/comp_clips.json`。
8. `.topos` 读取恢复 `comp_clips`。

新增测试：

```text
tests/edit/test_comp_clip_data.py
tests/project/test_topos_comp_clip_schema.py
```

验收：

- 能创建空白 Comp Clip。
- 能从时间线 clip 创建 Comp Clip。
- 保存 `.topos` 后重新打开，`MediaAsset(comp_clip)`、`CompClipData`、`CompositeGraphData` 全部存在。
- `graph_id`、`comp_clip_id`、`asset_id` 三者互相一致。

### Phase 2：Composite 默认图与输入节点

目标：Composite 不再直接用 `image_input` 读取主视频。

修改文件：

```text
src/features/composite/nodes/timeline_clip_input_node.py
src/features/composite/core/node_engine.py
src/features/composite/core/node_capabilities.py
src/features/composite/core/tile_padding_computer.py
src/features/edit/core/composite_graph_data.py
```

实施步骤：

1. 新增 `TimelineClipInputNode`。
2. 注册到 `NODE_REGISTRY`。
3. 注册到节点菜单 Input 分类。
4. 修改 `CompositeGraphData.create_default_for_clip()`：

```text
TimelineClipInput -> Output
```

5. 新增 `create_default_empty_comp_clip()`：

```text
Constant/Background -> Output
```

6. 确保节点序列化/反序列化支持。

新增测试：

```text
tests/composite/test_timeline_clip_input_node.py
tests/edit/test_comp_clip_default_graph.py
```

验收：

- 默认时间线 Comp Clip 图里没有 `image_input` 主节点。
- `timeline_clip_input` 节点能保存、读取、连接、显示。
- 未提供 runtime context 时节点返回空结果或明确错误，不崩溃。

### Phase 3：Composite Runtime Context

目标：让 Composite 节点能从 Edit 时间线获取当前帧。

修改文件：

```text
src/features/composite/core/runtime_context.py
src/features/edit/core/timeline_clip_frame_provider.py
src/features/composite/render/composite_renderer.py
src/features/composite/evaluators/*
src/features/composite/nodes/timeline_clip_input_node.py
```

实施步骤：

1. 新增 `CompositeRuntimeContext`。
2. 新增 `TimelineClipFrameProvider`。
3. 实现 `timeline_frame -> clip_local_frame -> source_frame`。
4. 第一阶段仅实现 `after_color`：
   - 如果 clip 有 `color_stack_id`，应用 color stack。
   - 如果没有，返回源画面。
5. Composite 渲染请求必须携带 runtime context。
6. `TimelineClipInputNode._do_evaluate()` 从 context 获取帧。

新增测试：

```text
tests/composite/test_composite_runtime_context.py
tests/edit/test_timeline_clip_frame_provider.py
```

验收：

- 时间线第 N 帧打开 Composite，输入节点拿到正确 source frame。
- 修改 clip `source_start` 后，Composite 输入帧同步变化。
- 修改 clip `speed` 后，Composite 输入帧同步变化。
- ColorStack 修改后，Composite 输入画面版本号或 revision 更新。

### Phase 4：Edit -> Composite 页面切换与懒创建

目标：用户从时间线普通 clip 进入 Composite，可在第一次编辑时升级成 Comp Clip。

修改文件：

```text
src/app/controllers/edit_controller.py
src/app/controllers/composite_controller.py
src/app/controllers/page_controller.py
src/app/controllers/file_controller.py
src/features/composite/ui/composite_page.py
src/features/edit/ui/timeline_items.py
src/features/edit/ui/timeline_view.py
src/features/edit/ui/edit_page.py
```

实施步骤：

1. `PageContext` 增加：

```python
comp_clip_id: str | None
comp_asset_id: str | None
graph_id: str | None
timeline_frame: int
clip_local_frame: int
source_frame: int
```

2. `switch_from_edit_to_composite()` 必须传：

```text
timeline_id
clip_id
asset_id
current timeline_frame
```

3. `CompositePage` 增加模式：

```text
existing_comp_clip
temporary_clip_composite
empty_comp_clip
```

4. `CompositePage` 监听节点编辑动作：
   - add node
   - remove node
   - connect
   - disconnect
   - parameter change

5. 对 `temporary_clip_composite` 第一次编辑时调用：

```python
ensure_current_comp_clip()
```

6. `ensure_current_comp_clip()` 完成：
   - 创建 Comp Clip Asset。
   - 创建 CompClipData。
   - 创建 CompositeGraphData。
   - 更新当前 timeline clip。
   - 刷新媒体池。
   - 刷新时间线。
   - 设置 dirty。

新增测试：

```text
tests/app/test_edit_to_composite_comp_clip_creation.py
tests/composite/test_composite_lazy_comp_clip_creation.py
```

验收：

- 仅打开 Composite 页面不创建媒体池 Comp Clip。
- 添加一个节点后自动创建 Comp Clip。
- 媒体池出现 Comp Clip。
- 时间线 clip 状态更新。
- 保存工程后重新打开仍可双击 Comp Clip 进入 Composite。

### Phase 5：媒体池支持 Comp Clip

目标：Comp Clip 像普通素材一样可见、可拖、可打开。

修改文件：

```text
src/features/edit/ui/media_pool_panel.py
src/features/edit/core/commands.py
src/app/controllers/file_controller.py
src/app/controllers/edit_controller.py
```

实施步骤：

1. `AssetListModel` 支持 `media_type="comp_clip"`。
2. 缩略图 badge 显示 `COMP` 或 `Comp Clip`。
3. 双击 Comp Clip：

```text
打开 Composite 页面并加载对应 graph
```

4. 拖拽 Comp Clip 到时间线：
   - 只能落到视频轨。
   - 生成 `ClipInstance(asset_id=comp_asset_id)`。
   - `source_duration = comp_clip.duration_frames`。

5. 媒体池右键新增：

```text
New Comp Clip
Open in Composite
Rename
Delete
```

6. 第一阶段删除规则：
   - 如果 Comp Clip 正被时间线使用，禁止删除或弹确认。
   - 确认删除时同时删除 `CompClipData` 与 `CompositeGraphData`。

新增测试：

```text
tests/edit/test_media_pool_comp_clip.py
tests/edit/test_drag_comp_clip_to_timeline.py
```

验收：

- 新建空白 Comp Clip 后媒体池立即显示。
- 双击打开正确 graph。
- 拖到视频轨后时间线显示一个 clip。
- 保存打开后仍存在。

### Phase 6：Edit 预览渲染接入 Comp Clip

目标：时间线播放时 Comp Clip 输出真实参与多轨合成。

修改文件：

```text
src/features/edit/core/timeline_render_plan.py
src/features/edit/core/edit_gpu_preview_renderer.py
src/features/edit/core/timeline_preview_renderer.py
src/features/composite/render/composite_renderer.py
```

实施步骤：

1. `TimelineRenderPlan` 输出层信息时保留：
   - `asset_id`
   - `asset.media_type`
   - `composite_id`
   - `comp_clip_id`
2. Edit GPU preview renderer 判断：

```python
if asset.media_type == "comp_clip":
    render_comp_clip_asset(...)
elif clip.composite_id:
    render_clip_composite(...)
else:
    render_normal_clip(...)
```

3. `render_comp_clip_asset()` 通过 graph_id 调用 Composite evaluator。
4. 每一帧必须传入 `CompositeRuntimeContext`。
5. 预览质量使用 `proxy` 或 `preview`。
6. 缓存 key 必须包含：

```text
graph_id
timeline_frame
source_frame
color_stack_revision
graph_revision
quality
```

新增测试：

```text
tests/edit/test_timeline_preview_comp_clip.py
tests/edit/test_comp_clip_multitrack_composite.py
```

验收：

- 含 Comp Clip 的时间线可以播放。
- Comp Clip 在 V2 上能覆盖 V1。
- 普通 clip 与 Comp Clip 混合时顺序正确。
- 修改 Comp Clip 节点后 Edit 预览刷新。

### Phase 7：Color 与 Composite 的顺序打通

目标：Comp Clip 输入默认使用已调色画面。

修改文件：

```text
src/features/edit/core/timeline_clip_frame_provider.py
src/app/controllers/color_controller.py
src/shared/project/save_router.py
src/features/composite/nodes/timeline_clip_input_node.py
```

实施步骤：

1. `TimelineClipInputNode.input_stage` 默认 `after_color`。
2. `TimelineClipFrameProvider` 渲染主输入时先应用 `ColorStackData`。
3. Color 保存后通知：
   - 对应 clip preview dirty。
   - 对应 graph input revision dirty。
4. Composite 页面当前打开该 clip 时自动刷新。

新增测试：

```text
tests/integration/test_color_to_composite_clip_input.py
```

验收：

- 在 Color 改曝光，切到 Composite，输入画面变化。
- 在 Composite 加节点，切回 Edit，时间线画面包含 Color + Composite。
- 不打开 Composite 页面，导出也包含 Color + Composite。

### Phase 8：Deliver 导出接入

目标：最终导出使用完整链路。

修改文件：

```text
src/app/controllers/deliver_controller.py
src/shared/export/sequence_exporter.py
src/shared/export/export_source.py
src/features/edit/core/timeline_export.py
```

实施步骤：

1. `ExportSource.TIMELINE` 必须支持 Comp Clip。
2. 导出逐帧渲染调用同一条 Timeline renderer。
3. 导出质量传 `master`。
4. 禁止依赖 Composite UI 当前纹理。

新增测试：

```text
tests/export/test_export_timeline_with_comp_clip.py
```

验收：

- 关闭 Composite 页面后导出仍正确。
- 打开不同 Comp Clip 后导出当前时间线仍正确。
- 导出结果包含 ColorStack 和 Composite graph。

### Phase 9：缩略图与性能

目标：Comp Clip 媒体池缩略图和时间线预览不拖慢播放。

修改文件：

```text
src/shared/media/thumbnail_provider.py
src/features/edit/ui/media_pool_panel.py
src/features/color/ui/timeline_clip_strip.py
src/features/edit/ui/timeline_items.py
src/features/edit/core/edit_gpu_preview_renderer.py
```

实施步骤：

1. Comp Clip 缩略图异步生成。
2. 播放期间只读缓存，不触发新缩略图渲染。
3. 缩略图 cache key：

```text
comp_clip_id
graph_revision
source_revision
thumbnail_frame
size
```

4. 媒体池只为可见项请求缩略图。
5. 时间线 filmstrip 对 Comp Clip 降级：
   - 缩小时只显示首帧。
   - 播放时不刷新。

新增测试：

```text
tests/media/test_comp_clip_thumbnail_cache.py
tests/edit/test_comp_clip_thumbnail_no_playback_decode.py
```

验收：

- 播放时没有后台大量 graph render。
- 快速滚动媒体池不会卡 UI。
- 修改节点后缩略图能失效并更新。

---

## 11. 关键实现细节

### 11.1 普通 clip 升级为 Comp Clip 的数据操作

伪代码：

```python
def create_comp_clip_from_timeline_clip(project, timeline_id, clip_id, name):
    timeline = project.timelines[timeline_id]
    clip, track = find_clip(timeline, clip_id)
    original_asset = project.assets[clip.asset_id]

    comp_clip_id = uuid4()
    comp_asset_id = uuid4()
    graph_id = uuid4()

    comp_asset = MediaAsset(
        asset_id=comp_asset_id,
        path=f"topos://comp_clip/{comp_asset_id}",
        media_type="comp_clip",
        display_name=name,
        metadata={
            "comp_clip_id": comp_clip_id,
            "graph_id": graph_id,
            "width": timeline.width,
            "height": timeline.height,
            "fps": timeline.fps,
            "duration_frames": clip.timeline_duration,
            "source_kind": "timeline_clip_upgrade",
            "original_asset_id": original_asset.asset_id,
            "original_timeline_id": timeline_id,
            "original_clip_id": clip_id,
        },
    )

    comp_clip = CompClipData(...)

    graph = CompositeGraphData.create_default_for_clip(
        composite_id=graph_id,
        comp_clip_id=comp_clip_id,
        comp_asset_id=comp_asset_id,
        timeline_id=timeline_id,
        clip_id=clip_id,
        asset_id=original_asset.asset_id,
        asset_path=original_asset.path,
    )

    updated_clip = clip.with_asset_id(comp_asset_id).with_composite_id(graph_id)

    return updated_project
```

如果当前 `ClipInstance` 没有 `with_asset_id()`，需要新增。

### 11.2 是否保留 ClipInstance.composite_id

建议保留。

原因：

- 方便快速判断 timeline clip 是否有 composite graph。
- 对从普通 clip 升级但还没有完全替换 asset 的过渡阶段有帮助。

但最终以 `asset.media_type == "comp_clip"` 为主。

推荐规则：

```text
普通素材 clip:
  asset_id = video/image asset
  composite_id = None

普通素材 clip 临时挂 graph:
  asset_id = video/image asset
  composite_id = graph_id

正式 Comp Clip:
  asset_id = comp_clip asset
  composite_id = graph_id
```

第一阶段创建正式 Comp Clip 时直接替换 `asset_id`。

### 11.3 Comp Clip 与 ColorStack 的关系

从普通 clip 升级为 Comp Clip 时，ColorStack 不要丢。

规则：

1. 如果原 clip 有 `color_stack_id`，继续保留在 timeline clip 上。
2. `TimelineClipInputNode` 默认读取 `after_color`，也就是原素材经过 `color_stack_id` 后的画面。
3. Comp Clip 自己作为媒体池素材后，如果再次在时间线上复制多份，每份可以有自己的 `color_stack_id`。

最终链路：

```text
Original Source
  -> Original Clip ColorStack
  -> Comp Clip Graph
  -> Timeline Clip ColorStack 可选
  -> Timeline Track Composite
```

第一阶段为了避免双重调色混乱，建议：

- 从普通 clip 升级时保留原 `color_stack_id`，作为 graph input 前置调色。
- 新生成的 Comp Clip timeline instance 不自动创建第二个 color stack。
- 如果用户之后在 Color 页面打开 Comp Clip，再为 Comp Clip timeline instance 创建新的 color stack。

### 11.4 递归 Comp Clip

必须防止递归：

```text
Comp A graph 引用 Comp A 自己
Comp A -> Comp B -> Comp A
```

第一阶段规则：

- `TimelineClipInputNode` 不允许引用当前 graph 对应的 comp asset。
- 媒体节点添加 Comp Clip 作为输入时，检查 graph 依赖。

新增测试：

```text
tests/composite/test_comp_clip_recursion_guard.py
```

### 11.5 删除策略

删除媒体池 Comp Clip 时：

1. 查找所有时间线引用 `comp_asset_id` 的 clip。
2. 如果存在引用：
   - 第一阶段直接阻止删除。
   - UI 提示“Comp Clip is used in timeline”。
3. 如果无引用：
   - 删除 `MediaAsset`。
   - 删除 `CompClipData`。
   - 删除 `CompositeGraphData`。
   - 删除缩略图缓存。

---

## 12. 必须新增的自动化测试清单

### 12.1 数据模型测试

```bash
.venv/bin/python -m pytest \
  tests/edit/test_comp_clip_data.py \
  tests/edit/test_composite_graph_data.py \
  tests/project/test_topos_comp_clip_schema.py \
  -q
```

覆盖：

- `media_type="comp_clip"` 合法。
- 创建空白 Comp Clip。
- 从 timeline clip 创建 Comp Clip。
- 保存读取 `.topos`。
- graph_id 一致。

### 12.2 Composite 节点测试

```bash
.venv/bin/python -m pytest \
  tests/composite/test_timeline_clip_input_node.py \
  tests/composite/test_composite_runtime_context.py \
  tests/composite/test_comp_clip_recursion_guard.py \
  -q
```

覆盖：

- `timeline_clip_input` 序列化。
- 缺 context 不崩溃。
- context 正确返回帧。
- 禁止递归。

### 12.3 UI 控制器测试

```bash
.venv/bin/python -m pytest \
  tests/app/test_edit_to_composite_comp_clip_creation.py \
  tests/composite/test_composite_lazy_comp_clip_creation.py \
  tests/edit/test_media_pool_comp_clip.py \
  -q
```

覆盖：

- 打开不创建。
- 第一次编辑才创建。
- 媒体池显示。
- 双击打开。
- 时间线 clip 更新。

### 12.4 预览与导出测试

```bash
.venv/bin/python -m pytest \
  tests/edit/test_timeline_preview_comp_clip.py \
  tests/edit/test_comp_clip_multitrack_composite.py \
  tests/export/test_export_timeline_with_comp_clip.py \
  -q
```

覆盖：

- Edit 播放包含 Comp Clip。
- 多轨合成顺序正确。
- 导出不依赖 UI 页面缓存。

### 12.5 性能测试

```bash
.venv/bin/python -m pytest \
  tests/media/test_comp_clip_thumbnail_cache.py \
  tests/edit/test_comp_clip_thumbnail_no_playback_decode.py \
  -q
```

覆盖：

- 播放期间缩略图不触发 graph render。
- 可见项外不请求缩略图。
- graph 修改后缩略图失效。

---

## 13. 手动验收脚本

### 13.1 视频 clip 升级为 Comp Clip

步骤：

1. 新建 Edit 项目。
2. 导入一个视频。
3. 拖到 V1。
4. 选中 clip，右键 `Open in Composite`。
5. 不做任何修改，回到 Edit。

预期：

- 媒体池没有新增 Comp Clip。
- `.topos` 不新增 CompClipData。

继续：

6. 再次进入 Composite。
7. 添加 `Blur` 或 `Transform` 节点。
8. 回到 Edit。

预期：

- 媒体池出现 Comp Clip。
- 时间线 clip 显示 Comp 标识。
- 播放画面包含 Composite 效果。
- 保存并重新打开后仍一致。

### 13.2 媒体池空白 Comp Clip

步骤：

1. 媒体池右键 `New Comp Clip`。
2. 双击新 Comp Clip。
3. 在 Composite 添加 Background + Text。
4. 保存。
5. 拖到 V2。

预期：

- V2 显示该 Comp Clip。
- 播放时覆盖 V1。
- 保存打开后节点图存在。

### 13.3 图片 standalone 与 Comp Clip 分离

步骤：

1. 从文件系统打开图片到 Color。
2. 调色并保存 standalone `.topos`。

预期：

- 不创建 Comp Clip。
- 没有 timeline。

继续：

3. 新建 Edit 项目。
4. 导入同一张图片到媒体池。
5. 拖到 V1。
6. Open in Composite。
7. 添加 Text 节点。

预期：

- 新增 Comp Clip。
- 原 ImageAsset 仍保留。
- 时间线 clip 变为 Comp Clip 或挂 graph。

### 13.4 Color -> Composite -> Edit 链路

步骤：

1. 视频拖到时间线。
2. Open in Color，调高曝光。
3. Open in Composite，添加 Transform。
4. 回到 Edit 播放。

预期：

- Edit 画面同时包含曝光变化和 Transform。
- 保存打开后仍一致。

### 13.5 Deliver

步骤：

1. 使用包含 Comp Clip 的时间线。
2. 切到 Library 页面或 Edit 页面。
3. 不打开 Composite。
4. 导出时间线。

预期：

- 导出结果包含 Comp Clip 效果。
- 导出不依赖当前 Composite viewer。

---

## 14. 开发顺序建议

强烈建议按以下顺序执行，不要一次性全改：

1. Phase 1：Schema 与数据模型。
2. Phase 2：默认图与 `TimelineClipInputNode`。
3. Phase 4：Edit -> Composite 懒创建。
4. Phase 5：媒体池显示与双击。
5. Phase 3：Runtime Context。
6. Phase 6：Edit 预览渲染。
7. Phase 7：Color -> Composite revision 刷新。
8. Phase 8：Deliver。
9. Phase 9：缩略图性能。

原因：

- 先有数据模型，UI 才有可靠对象可操作。
- 先能创建和保存 Comp Clip，再处理复杂 GPU runtime。
- 预览和导出最后接入，风险最低。

---

## 15. 后续 AI 执行注意事项

1. 每个 Phase 单独提交或至少单独验证。
2. 不要把图片 standalone `.topos` 删除。
3. 不要让视频 standalone 进入 Color 或 Composite。
4. 不要让 Composite 节点直接打开主 clip 视频文件。
5. 不要依赖 UI 当前页面缓存做导出。
6. 所有新 dataclass 保持 frozen 风格。
7. 所有 project 更新返回新对象，不原地改 frozen dataclass。
8. 新增 `media_type` 后必须检查所有 `if asset.media_type in (...)` 分支。
9. Comp Clip 删除必须检查时间线引用。
10. 缩略图生成必须异步且播放期间只读缓存。

---

## 16. 最终验收标准

功能完成后必须满足：

1. 普通视频 clip 可以升级为 Comp Clip。
2. 普通图片 clip 可以升级为 Comp Clip。
3. 媒体池可以创建空白 Comp Clip。
4. Comp Clip 在媒体池中可见、可重命名、可双击打开。
5. Comp Clip 可以拖到视频轨。
6. Edit 播放能显示 Comp Clip 效果。
7. Color 修改能进入 Composite 输入。
8. Composite 修改能回到 Edit 播放。
9. Deliver 导出包含完整效果。
10. `.topos` 保存打开后完整恢复。
11. 视频不能通过 standalone Color/Composite 绕开时间线。
12. 图片 standalone `.topos` 工作流仍可用。
13. 播放期间缩略图不会触发大量后台渲染。
14. 同一原素材生成多个 Comp Clip 时效果互不污染。
15. 同一 Comp Clip 多次拖到时间线时共享 graph，但每个 timeline instance 可拥有独立 ColorStack。

---

## 17. Comp Clip 渲染缓存与三条渲染管线协调

### 17.0 前置结论：必须先补齐 Composite 全 GPU 链路

在实现稳定的 Comp Clip 播放缓存之前，必须先把 Composite 的主输入与主输出升级为可被 Edit 时间线调用的全 GPU 链路。

当前 Composite 不是完全 CPU 渲染，它已经具备部分 GPU 基础：

- `CompositeRenderer` 有独立共享 OpenGL context。
- Composite 节点间使用 `TextureResult(texture_id=...)` 传递 GPU texture。
- `GraphEvaluatorMixin` / 各 evaluator 已经有 GPU shader/FBO 求值路径。
- `GPUResourcePool` 支持 preview/export 不同精度的 GPU texture。
- `CacheManager` 已经能按节点参数、输入 texture metadata、render mode 建 cache key。

但它还没有达到 Edit/Color 那种“时间线视频帧全 GPU 直通”的标准：

1. `VideoFrameNode` 仍以 `np.ndarray` 作为主要中间结果。
   - `request_frame_for_node(...)` 返回 decoded frame 后会转成 RGBA float32。
   - 再上传到 Composite 自己的 GPU texture。
   - 这不是 VideoToolbox/D3D11/zero-copy -> GPU shader -> graph 的直通。

2. `ImageInputNode` 仍是 CPU decode -> GPU upload。
   - 图片这条路径可以接受，因为图片不是高频视频帧。
   - 但视频主输入不能走这种模式。

3. `COLORNode` 依赖 Color 页面当前输出 texture。
   - 它适合临时页面预览。
   - 不适合 Edit 时间线多 clip、多轨、后台缓存和 Deliver 导出。

4. Composite 输出当前主要服务 CompositeViewer。
   - Edit timeline playback 还不能稳定把 Composite graph 当作 clip layer provider。
   - Deliver 也不能依赖 CompositeViewer 的最后一帧。

因此优先级必须调整为：

```text
先做 Composite 全 GPU 输入/输出契约
  -> 再做 Comp Clip
  -> 再做 Render Cache
  -> 再做 Deliver master 复用
```

否则会出现：

- Comp Clip 每帧 CPU decode + CPU 转 RGBA + GPU upload，播放无法稳定。
- Render Cache 生成速度很慢。
- 缓存文件可能缓存了错误色彩空间或错误帧映射。
- Edit/Color/Composite 三条 GPU 管线仍然是“页面级拼接”，不是“服务级管线”。

### 17.0.1 Composite 全 GPU 链路目标

最终目标：

```text
Edit Timeline Frame
  -> Shared GPU decoded frame / GPU uploaded source frame
  -> ColorStack GPU shader chain
  -> CompositeGraph GPU evaluator
  -> TimelineCompositor GPU blend
  -> ProgramViewer / RenderCacheEncoder / DeliverEncoder
```

任何视频主输入都不应该落回：

```text
GPU frame -> CPU ndarray -> GPU upload
```

允许例外：

- 图片静态 decode。
- 缩略图后台生成。
- CPU-only fallback。
- final export 必要的 encoder readback，直到实现 GPU encoder interop。

### 17.0.2 新增 CompositeGpuFrameProvider

建议新增：

```text
src/features/composite/core/composite_gpu_frame_provider.py
```

职责：

1. 从 Edit 的 `TimelineClipFrameProvider` 获取当前 clip 已调色 GPU texture。
2. 不创建独立视频 decoder。
3. 不读取 Color 页面 UI 当前纹理。
4. 不返回 `np.ndarray`。
5. 返回 Composite 标准 `TextureResult`。

接口示例：

```python
class CompositeGpuFrameProvider:
    def get_timeline_clip_texture(
        self,
        runtime_context: CompositeRuntimeContext,
    ) -> TextureResult:
        ...
```

返回内容：

```python
TextureResult(
    texture_id=...,
    width=...,
    height=...,
    revision=...,
    format=...,
    bbox=...,
    color_space=...,
    bit_depth=...,
    frame_metadata=...,
)
```

### 17.0.3 TimelineClipInputNode 必须只吃 GPU texture

`TimelineClipInputNode` 的实现要求：

- 禁止直接打开视频文件。
- 禁止直接调用 `VideoFrameNode._decode_frame()`。
- 禁止将主视频帧转成 `np.ndarray`。
- 只能从 `CompositeGpuFrameProvider` 获取 `TextureResult`。

伪代码：

```python
def _do_evaluate(self, inputs, ctx, fb_manager, resource_pool=None, render_mode="preview"):
    runtime = getattr(ctx, "runtime_context", None)
    provider = getattr(ctx, "timeline_clip_gpu_provider", None)
    if runtime is None or provider is None:
        return {}
    tex = provider.get_timeline_clip_texture(runtime)
    return {"image": tex}
```

如果当前 evaluator 只传 `moderngl.Context` 作为 `ctx`，需要新增一个轻量 render context 对象，不要把所有 runtime 字段塞到 ModernGL context 上。

建议：

```text
CompositeEvalContext
  - gl: moderngl.Context
  - runtime_context: CompositeRuntimeContext
  - timeline_clip_gpu_provider: CompositeGpuFrameProvider
```

第一阶段为了少改代码，也可以保持 evaluator 参数名为 `ctx`，但实际传入对象需要有 `.gl` 或兼容属性。

### 17.0.4 Composite 节点 GPU 化分级

不是所有 Composite 节点都必须一次性完全 GPU 化。按优先级：

#### P0：主链路必须 GPU

必须先完成：

- `timeline_clip_input`
- `output`
- `merge/blend`
- `transform`
- `crop`
- `reformat`
- `grade/color_correct`
- `blur/glow`
- `keyer/matte` 的基础 GPU 路径

这些决定 Comp Clip 是否能作为视频片段播放。

#### P1：可降级或缓存

- 高半径 blur。
- glow 高质量模式。
- 复杂 keyer。
- roto/mask 大面积栅格化。

播放时可降级；cache/render/export 时完整质量。

#### P2：允许 CPU/AI 后台

- AI denoise。
- AI inpaint/remove/expand。
- 复杂 ML matte。

这些不要求实时全 GPU，但必须：

- 不阻塞 UI。
- 有明确 cache。
- 播放时显示 cached 或 stale。
- 未缓存时可跳过或显示代理结果。

### 17.0.5 Composite 输出必须能被 Edit 作为 layer texture 使用

新增接口：

```text
src/features/composite/export_provider.py 或新的 composite_frame_renderer.py
```

建议：

```python
class CompositeFrameRenderer:
    def render_graph_to_texture(
        self,
        graph_id: str,
        runtime_context: CompositeRuntimeContext,
        render_mode: str,
    ) -> TextureResult:
        ...
```

要求：

- 不依赖 CompositePage 是否打开。
- 不依赖 CompositeViewer。
- 可以被 Edit preview、Render Cache Worker、Deliver 调用。
- 支持 preview/full/export mode。
- 支持取消旧请求。

### 17.0.6 GPU 链路验收标准

新增测试：

```text
tests/composite/test_timeline_clip_input_gpu_only.py
tests/composite/test_composite_frame_renderer_no_ui.py
tests/edit/test_comp_clip_gpu_path_no_cpu_roundtrip.py
tests/edit/test_timeline_color_composite_gpu_chain.py
```

重点断言：

1. `TimelineClipInputNode` 不调用 `VideoFrameNode._decode_frame()`。
2. `TimelineClipInputNode` 不返回 `np.ndarray`。
3. Comp Clip preview 命中 GPU path 时不触发 CPU RGB upload。
4. Edit 不打开 Composite 页面也能渲染 graph。
5. ColorStack 修改后，Composite 输入 texture revision 改变。
6. Composite graph 输出是 `TextureResult`，可直接进入 timeline compositor。
7. playback preview 和 render cache 使用同一个 graph renderer，只是 render mode 不同。

手动验收：

1. 打开 4K/6K 视频。
2. 放入时间线。
3. 转成 Comp Clip。
4. Color 做明显曝光调整。
5. Composite 加 Transform + Blur。
6. 回 Edit 播放。

预期：

- 日志显示主输入走 timeline GPU provider。
- 不出现每帧 `np.ndarray` RGBA 转换日志。
- GPU texture revision 随 Color/Composite 修改变化。
- 未缓存时可降级但不崩溃。
- 生成 Render Cache 后播放稳定。

### 17.1 为什么必须做渲染缓存

Comp Clip 的实时播放不能假设每帧都现场完成：

```text
Source decode
  -> ColorStack 调色
  -> Composite node graph
  -> Timeline 多轨合成
  -> Viewer
```

只要 Composite 图里出现以下任一情况，就很难稳定实时播放：

- 多个 blur/glow/keyer/roto/mask 节点。
- AI denoise / AI matte / inpaint / remove。
- 高分辨率素材。
- 多个 Comp Clip 叠在多轨。
- Color 与 Composite 同时有较重效果。

因此必须设计 **timeline render cache**，不是只做内存 frame cache。

渲染缓存目标：

1. 播放时优先读取已渲染缓存。
2. 没有缓存时可降级实时预览或显示未缓存状态。
3. 用户停止操作后后台自动渲染缓存。
4. Color 或 Composite 任一改动都会让相关缓存失效。
5. Deliver 导出默认不用 preview cache，但可以选择复用 master cache。

### 17.2 缓存类型分层

必须区分三类缓存，不要混用：

#### A. Decode / Proxy Cache

已有基础：

```text
src/shared/media/disk_proxy_service.py
src/shared/media/proxy_manager.py
src/shared/video/frame_provider_service.py
```

用途：

- 缓存源素材的低分辨率代理。
- 只解决“解码太慢”。
- 不包含 ColorStack。
- 不包含 Composite graph。

#### B. Graph Evaluation Cache

已有基础：

```text
src/features/composite/evaluators/cache_evaluator.py
src/features/composite/render/render_scheduler.py
tests/composite/test_composite_cache_key.py
```

用途：

- Composite 节点图内部临时复用 GPU 中间结果。
- 通常是内存/GPU 级缓存。
- 适合交互预览。
- 不适合作为时间线播放稳定缓存。

#### C. Timeline Render Cache

新增核心能力。

用途：

- 缓存某个时间线范围最终可播放结果。
- 可以是 clip 级，也可以是 timeline segment 级。
- 必须写入磁盘。
- 播放时可以直接解码缓存文件，不重新跑 Color/Composite。

推荐第一阶段实现：

```text
Comp Clip Render Cache
```

第二阶段扩展：

```text
Timeline Segment Render Cache
```

### 17.3 新增 RenderCacheSettings

文件建议：

```text
src/shared/render/render_cache_settings.py
```

数据类：

```python
@dataclass(frozen=True)
class RenderCacheSettings:
    enabled: bool = True
    mode: str = "smart"  # off | smart | user
    codec: str = "prores_proxy"
    container: str = "mov"
    pixel_format: str = "yuv422p10le"
    resolution_policy: str = "timeline_half"  # timeline_full | timeline_half | custom
    custom_width: int = 1920
    custom_height: int = 1080
    quality: str = "preview"  # preview | high | master
    cache_directory: str = ""
    max_cache_size_gb: int = 100
    use_cache_for_export: bool = False
```

默认建议：

```text
mode = smart
codec = prores_proxy 或 h264_intra
resolution_policy = timeline_half
quality = preview
use_cache_for_export = False
```

说明：

- macOS 上可以优先 `prores_proxy` / `prores_422lt`。
- Windows/Linux 如果编码器不可用，回退到 `h264_intra` 或 `dnxhd/dnxhr`。
- 导出默认不用 preview cache，避免质量损失。

### 17.4 Preferences 面板新增设置

文件：

```text
src/features/edit/preferences.py
src/shared/config/config_manager.py
```

当前 `EditPreferencePage` 已有视频播放设置：

- `video_pipeline_mode`
- `video_preview_drop_frames`
- `video_preview_max_queue`
- `video_use_yuv_gpu_upload`

需要新增 Render Cache 分组：

```text
Render Cache
  [x] Enable render cache
  Mode: Off / Smart / User
  Cache format: ProRes Proxy / ProRes LT / DNxHR LB / H.264 Intra
  Resolution: Timeline Full / Timeline Half / Custom
  Custom Width / Height
  Cache folder
  Max cache size
  [ ] Use master render cache for export
```

配置键建议加入 `ConfigManager.DEFAULT_CONFIG`：

```python
"render_cache_enabled": True,
"render_cache_mode": "smart",
"render_cache_codec": "prores_proxy",
"render_cache_container": "mov",
"render_cache_pixel_format": "yuv422p10le",
"render_cache_resolution_policy": "timeline_half",
"render_cache_custom_width": 1920,
"render_cache_custom_height": 1080,
"render_cache_quality": "preview",
"render_cache_directory": "",
"render_cache_max_size_gb": 100,
"render_cache_use_for_export": False,
```

验收：

- Preferences 能保存和恢复这些设置。
- 重启应用后设置不丢。
- Render cache 生成器读取的是同一份配置。

### 17.5 新增 TimelineRenderCoordinator

这是三条渲染管线的协调中心。

文件建议：

```text
src/features/edit/core/timeline_render_coordinator.py
```

职责：

1. 接收 Edit 播放请求。
2. 查询 TimelineRenderPlan。
3. 判断当前帧是否命中 render cache。
4. 命中：直接返回缓存帧。
5. 未命中：决定实时渲染、降级渲染、或显示缺缓存状态。
6. 调度后台 cache render。
7. 统一调用 Color pipeline 与 Composite pipeline。

核心接口：

```python
class TimelineRenderCoordinator:
    def request_preview_frame(self, timeline_id: str, frame: int) -> PreviewFrameResult:
        ...

    def schedule_cache_for_range(self, timeline_id: str, start: int, end: int) -> None:
        ...

    def invalidate_for_color_stack(self, stack_id: str) -> None:
        ...

    def invalidate_for_comp_graph(self, graph_id: str) -> None:
        ...

    def invalidate_for_timeline_edit(self, timeline_id: str, changed_range: FrameRange) -> None:
        ...
```

### 17.6 统一帧渲染阶段

必须明确一帧最终怎么生成。

建议统一为：

```text
TimelineRenderCoordinator
  -> TimelineRenderPlan(frame)
  -> for each ClipLayerInfo:
       ClipFramePipeline.render_clip_layer(...)
  -> TimelineCompositor.composite_layers(...)
  -> Viewer / Cache Encoder / Export Encoder
```

新增：

```text
src/features/edit/core/clip_frame_pipeline.py
```

接口：

```python
class ClipFramePipeline:
    def render_layer(
        self,
        project: EditProject,
        timeline: TimelineSequence,
        layer: ClipLayerInfo,
        frame: int,
        mode: str,  # playback | cache | export
        quality: str,  # preview | high | master
    ) -> TextureResult:
        ...
```

内部顺序：

```text
1. Resolve source asset / comp asset
2. Decode source frame or read comp source
3. Apply ColorStack
4. Apply CompositeGraph if present
5. Apply clip transform/crop/opacity as layer metadata
6. Return GPU TextureResult
```

### 17.7 Comp Clip Render Cache 数据模型

新增：

```text
src/features/edit/core/render_cache_data.py
```

数据类：

```python
@dataclass(frozen=True)
class RenderCacheEntry:
    cache_id: str
    target_kind: str  # comp_clip | timeline_range
    timeline_id: str | None
    clip_id: str | None
    comp_clip_id: str | None
    graph_id: str | None
    start_frame: int
    end_frame: int
    cache_path: str
    codec: str
    width: int
    height: int
    fps: float
    quality: str
    fingerprint: str
    status: str  # missing | queued | rendering | ready | stale | failed
    progress: float = 0.0
    error: str = ""
    created_at: float = 0.0
    modified_at: float = 0.0
```

`EditProject` 或 `.topos` 中应保存索引：

```text
render_cache/index.json
```

注意：

- 缓存视频文件本身可以放在用户 cache directory，不一定打包进 `.topos`。
- `.topos` 只保存 cache index 和 fingerprint。
- 如果缓存文件丢失，状态变为 `missing`，可重新生成。

### 17.8 Render Cache Fingerprint

缓存命中必须靠 fingerprint，不能只靠帧号。

Comp Clip cache fingerprint 至少包含：

```text
project_id
timeline_id
clip_id
comp_clip_id
graph_id
graph_revision / graph hash
color_stack_id
color_stack_hash
source_asset_id
source_asset_path
source_asset_mtime / media fingerprint
source_start
source_duration
speed
timeline fps
cache codec
cache resolution
cache quality
```

Timeline range cache 还必须包含：

```text
all visible clip ids
all visible clip versions
track order
track mute/solo/enable states
transitions
timeline resolution
```

新增工具：

```text
src/features/edit/core/render_cache_fingerprint.py
```

验收：

- 改 Color 参数，旧缓存变 stale。
- 改 Composite 节点，旧缓存变 stale。
- 改 clip 入点、速度、长度，旧缓存变 stale。
- 只移动无关 clip，不影响该 Comp Clip cache。

### 17.9 Render Cache Manager

新增：

```text
src/features/edit/core/render_cache_manager.py
```

职责：

1. 查询 cache entry。
2. 判断 fingerprint 是否匹配。
3. 标记 stale。
4. 启动后台渲染任务。
5. 管理磁盘空间。
6. 删除过期缓存。

核心接口：

```python
class RenderCacheManager:
    def get_ready_cache(self, target, frame: int, fingerprint: str) -> RenderCacheHit | None:
        ...

    def mark_stale_by_graph(self, graph_id: str) -> None:
        ...

    def mark_stale_by_color_stack(self, stack_id: str) -> None:
        ...

    def enqueue_render(self, target, frame_range, settings) -> str:
        ...

    def cleanup(self) -> None:
        ...
```

### 17.10 Cache Renderer Worker

新增：

```text
src/features/edit/core/render_cache_worker.py
```

职责：

- 后台逐帧渲染 Comp Clip 或 timeline range。
- 调用 `TimelineRenderCoordinator.render_frame_for_cache(...)`。
- 将帧送入 encoder。
- 进度回传 UI。

要求：

- 不阻塞 UI。
- 可取消。
- 用户继续修改 graph 时取消旧任务。
- 任务完成后刷新时间线 cache 状态。

### 17.11 Cache Encoder

新增：

```text
src/shared/export/render_cache_encoder.py
```

可以复用现有：

```text
src/shared/export/video_encoder.py
src/shared/export/encoder.py
```

但要避免把“导出成品”和“缓存中间文件”混在同一套 UI 参数里。

编码要求：

- 支持中间编码格式。
- 支持 alpha 的格式预留：
  - ProRes 4444
  - PNG sequence
  - EXR sequence
- 第一阶段可只实现无 alpha preview cache。

### 17.12 播放时的决策逻辑

播放 Comp Clip 时：

```text
if render_cache hit:
    decode cache file frame
elif render_cache queued/rendering:
    use degraded realtime preview if possible
    show cache status line = red/yellow
else:
    if mode == smart:
        enqueue background cache
        use degraded realtime preview
    elif mode == user:
        show "unrendered" status until user render
    elif mode == off:
        realtime/degraded only
```

UI 渲染线颜色建议：

```text
Blue/Green: cache ready
Yellow: cache rendering / partial
Red: cache missing or stale
Gray: cache disabled
```

### 17.13 时间线 UI 需要显示缓存状态

修改：

```text
src/features/edit/ui/timeline_view.py
src/features/edit/ui/timeline_items.py
src/features/edit/ui/timeline_panel.py
```

新增显示：

- Comp Clip 顶部缓存状态线。
- 右键菜单：
  - Render Cache for Clip
  - Delete Render Cache
  - Render Cache for Selection
  - Render Cache for Timeline Range
- 状态提示：
  - Cached
  - Rendering 45%
  - Stale
  - Missing

第一阶段最低要求：

- Comp Clip clip item 顶部显示红/绿状态线。
- 右键 `Render Cache for Clip`。
- 渲染过程中显示进度。

### 17.14 Color / Composite 改动如何失效缓存

Color 保存时：

```text
SaveRouter / ColorController
  -> RenderCacheManager.mark_stale_by_color_stack(stack_id)
  -> TimelineRenderCoordinator.invalidate_for_color_stack(stack_id)
```

Composite 节点变更时：

```text
CompositePage graph changed
  -> graph_revision += 1
  -> RenderCacheManager.mark_stale_by_graph(graph_id)
  -> TimelineRenderCoordinator.invalidate_for_comp_graph(graph_id)
```

Timeline 剪辑改动时：

```text
Timeline command applied
  -> changed_range
  -> RenderCacheManager.mark_stale_by_timeline_range(...)
```

必须覆盖：

- 调色参数。
- LUT。
- 曲线。
- Composite 节点参数。
- 节点连接。
- clip speed。
- clip source_start/source_duration。
- clip transform/crop/opacity。
- track enable/lock/mute/solo。
- transition。

### 17.15 Cache 与 Export 的关系

默认规则：

```text
Preview playback 使用 render cache。
Final export 默认重算 master。
```

Preferences 可选：

```text
[ ] Use master render cache for export
```

只有满足以下条件才允许 export 复用 cache：

- cache quality == master
- cache resolution == timeline full
- cache codec 是可接受中间格式
- fingerprint 完全匹配
- cache status == ready

如果不满足，导出必须重算。

### 17.16 GPU 管线接入原则

三条管线不能互相直接调用 UI 页面状态。

错误做法：

```text
Edit playback 直接读 Color 当前 canvas texture
Edit playback 直接读 CompositeViewer texture
Deliver 直接读 Composite 页面最后一帧
```

正确做法：

```text
TimelineRenderCoordinator
  -> ColorFramePipeline（无 UI）
  -> CompositeGraphEvaluator（无 UI）
  -> TimelineCompositor（无 UI）
```

页面只负责：

- 发起请求。
- 显示结果。
- 修改参数。
- 标记 dirty。

渲染服务负责：

- 计算。
- 缓存。
- 失效。
- 导出。

### 17.17 实施阶段：Render Cache 专项

建议插入到原计划 Phase 6 前后。

#### Phase RC-1：Preferences 与配置

修改：

```text
src/shared/config/config_manager.py
src/features/edit/preferences.py
```

验收：

- Render Cache 设置可保存。
- 重启后不丢。

#### Phase RC-2：RenderCacheData / Fingerprint

新增：

```text
src/features/edit/core/render_cache_data.py
src/features/edit/core/render_cache_fingerprint.py
```

验收：

- 同 graph 同参数 fingerprint 一致。
- 改 Color/Composite/clip 后 fingerprint 改变。

#### Phase RC-3：RenderCacheManager

新增：

```text
src/features/edit/core/render_cache_manager.py
```

验收：

- 能登记 cache entry。
- 能标记 stale。
- 能处理 cache 文件丢失。
- 能按大小清理。

#### Phase RC-4：TimelineRenderCoordinator

新增：

```text
src/features/edit/core/timeline_render_coordinator.py
src/features/edit/core/clip_frame_pipeline.py
```

验收：

- Preview 请求先查 cache。
- 无 cache 时走实时/降级。
- 能调度后台 cache。

#### Phase RC-5：Cache Worker / Encoder

新增：

```text
src/features/edit/core/render_cache_worker.py
src/shared/export/render_cache_encoder.py
```

验收：

- 能把一个 Comp Clip 范围渲染成缓存文件。
- 可取消。
- 完成后 timeline 状态变 ready。

#### Phase RC-6：Timeline UI 状态线

修改：

```text
src/features/edit/ui/timeline_items.py
src/features/edit/ui/timeline_view.py
src/features/edit/ui/timeline_panel.py
```

验收：

- Comp Clip 未缓存显示红线。
- 渲染中显示黄线/进度。
- 缓存完成显示绿线。
- 改节点后变 stale。

### 17.18 Render Cache 自动化测试

新增测试：

```text
tests/edit/test_render_cache_settings.py
tests/edit/test_render_cache_fingerprint.py
tests/edit/test_render_cache_manager.py
tests/edit/test_timeline_render_coordinator_cache_priority.py
tests/edit/test_comp_clip_cache_invalidation.py
tests/edit/test_render_cache_worker.py
```

重点断言：

1. Preferences 配置能保存读取。
2. cache key 包含 ColorStack hash。
3. cache key 包含 Composite graph hash。
4. cache key 包含 clip speed/source_start。
5. cache ready 时播放不调用 Composite evaluator。
6. cache stale 时播放不误用旧缓存。
7. 删除缓存文件后状态变 missing。
8. 导出默认不使用 preview cache。

### 17.19 手动验收：Render Cache

步骤：

1. 视频拖到时间线。
2. 转成 Comp Clip。
3. Color 调色。
4. Composite 添加 blur/glow。
5. 回 Edit 播放。

预期：

- 未缓存时显示红线。
- 播放可能降级或掉帧，但不崩溃。

继续：

6. 右键 Comp Clip -> Render Cache for Clip。
7. 等待完成。
8. 播放该范围。

预期：

- 状态线变绿。
- 播放优先读缓存。
- 不再实时跑 Composite graph。

继续：

9. 回 Color 修改曝光。

预期：

- Comp Clip cache 变 stale。
- 状态线变红或橙。

继续：

10. 重新 Render Cache。

预期：

- 新缓存生成。
- 播放显示新调色与合成结果。

---

## 18. 一句话架构原则

**Edit 管理时间，Color 管理镜头调色，Composite 管理 Comp Clip 内部画面结构；Comp Clip 是媒体池中的正式素材，也是 Edit、Color、Composite 三者真正打通的工程对象。**
