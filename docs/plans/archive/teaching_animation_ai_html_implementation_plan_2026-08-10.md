# Topos 教学动画与 AI 驱动网页场景实施计划

版本：v1.0  
日期：2026-08-10  
状态：规划中  
适用产品：Topos Studio / Topos Edit / Topos Composite

---

## 1. 文档目的

本计划用于把“老师通过 AI 对话生成简单教学演示动画”的想法落地到 Topos 现有架构中。

本计划综合了以下设计结论：

1. 简单教学演示视频的核心不是让 AI 直接生成像素视频，而是生成结构化教学场景、页面和动画时间线。
2. HTML/CSS/SVG/Canvas/WebGL 可以作为场景渲染层；项目的真实数据应当是可验证、可保存、可撤销的 JSON 场景模型，而不是一段不可控的 HTML 字符串。
3. 产品体验类似现代化的 Flash：舞台、元件、素材库、时间轴、关键帧和补间动画，但最终输出 MP4/WebM/图片序列，也可以保留网页预览能力。
4. 教学动画的制作应放在 Composite 侧；Edit 负责把生成的图形片段与视频、音频、字幕编排成完整课程视频。
5. AI Assistant 应是应用级共享 Dock，复用同一个面板和会话，根据当前页面切换上下文，不应在 Edit 和 Composite 各维护一份独立助手状态。
6. “加入时间线”不应默认把场景扁平化成一个普通 MP4；应优先创建带场景引用和渲染缓存关联的 Motion Graphic Clip，同时提供“渲染为普通视频素材”的扁平化选项。
7. 教学动画应作为独立的可选 Feature Module 内置，而不是把大量教学专用代码散落到现有 Edit、Composite 和 AI Assistant 面板中；关闭或移除该模块时，已有基础剪辑/合成能力仍必须正常工作。

---

## 2. 最终产品形态

### 2.1 老师的目标工作流

```text
老师在 AI Assistant 中描述课程内容
        ↓
AI 生成脚本、场景结构、图形组件和动画参数
        ↓
Teaching Animation 独立编辑窗口中实时预览
        ↓
老师修改文字、公式、图表、颜色、时长和动画
        ↓
点击“加入时间线”
        ↓
生成 Motion Graphic Clip 与渲染缓存
        ↓
Clip 进入 Edit 页的 graphics 轨道
        ↓
与视频、配音、音乐、字幕一起剪辑
        ↓
现有 TimelineExporter + FFmpeg 输出视频
```

### 2.2 用户界面分工

| 页面/面板 | 主要职责 | 不负责的内容 |
|---|---|---|
| AI Assistant | 对话、生成方案、解释当前工程、提交可审阅的操作 | 直接绕过命令系统修改底层对象 |
| Teaching Animation 模块/编辑窗口 | 创建和编辑一个教学动画场景 | 多素材剪辑、完整节目编排 |
| Composite 专业节点模式 | 复杂合成、节点图、遮罩、3D、GPU 效果 | 面向普通老师的全部复杂性 |
| Edit | 编排多个视频、音频、字幕、Motion Graphic Clip | 编辑教学场景内部的每个 SVG/HTML 元素 |
| Deliver | 最终编码、封装、响度和交付检查 | 教学场景创作 |

### 2.3 推荐的用户可见入口

建议把 Teaching Animation 作为独立可插拔模块入口，提供一个独立的非模态编辑窗口：

```text
Topos
├── Composite（专业合成）
└── Teaching Animation（独立编辑窗口）
```

教学动画编辑窗口不固定在 Composite 或 Edit 的 Dock 中，用户可以拖到第二块屏幕。它使用独立的 Teaching Scene 数据模型和渲染器，但通过 Composite facade 使用专业合成能力，通过 Edit commands facade 把结果加入主时间线。

Composite 页面仍然保留“添加 Motion Graphic Node”的入口；Edit 时间线仍然保留“创建 Motion Graphic Clip”的入口。两个入口最终都打开同一个编辑窗口和同一个场景模型。

### 2.4 AI 生成后必须保持可编辑

AI 的职责是生成一个可编辑初稿，而不是直接生成不可逆的 MP4。老师需要能够像在 Figma 中修改设计稿一样，对 AI 结果进行局部修正：

```text
AI 生成场景
    ↓
老师选择页面或元素
    ↓
修改文字/颜色/布局/动画
    ↓
实时预览
    ↓
确认后加入 Edit 时间线
```

编辑能力分为三层：

| 编辑层级 | 典型操作 | 实施要求 |
|---|---|---|
| 内容编辑 | 修改标题、段落、公式、图表文字、替换图片 | 文本和资源必须保留为结构化字段，不能只保留截图 |
| 视觉编辑 | 修改颜色、字体、背景、描边、圆角、阴影、位置、缩放和层级 | 样式使用可编辑参数和主题 token，不把颜色写死在最终 HTML 中 |
| 动画编辑 | 修改入场方式、延迟、持续时间、方向、缓动、出现顺序和关键帧 | 动画以项目 frame 为基准，支持简单参数和高级关键帧两种入口 |

第一版面向老师提供简化控件：

- 入场：淡入、滑入、缩放、打字机、绘制；
- 出场：淡出、滑出、缩小；
- 延迟、持续时间、方向、缓动；
- 文字颜色、背景色、强调色；
- 元素拖动、缩放、层级调整；
- 公式逐项显示和箭头延迟出现。

高级用户可以进入关键帧编辑器，直接调整 `opacity`、`position`、`scale`、`rotation` 等参数。

这意味着项目必须保存：

```text
TeachingSceneDocument
 ├── element content
 ├── style parameters
 ├── animation parameters
 ├── keyframe tracks
 └── asset references
        ↓
HTML/SVG/Canvas/ModernGL Renderer
        ↓
实时预览 / 缓存 / 最终视频
```

不能采用“AI → HTML → MP4”后只保留视频文件的方案。加入时间线后的 Motion Graphic Clip 仍需保存 `scene_id` 和场景版本，使老师可以从时间线返回独立编辑窗口继续修改。

例如：

```text
“把速度改成加速度”
→ 只更新文本元素，不重新生成整段视频

“这个箭头出现太快，延长到 1 秒并改成蓝色”
→ 只更新箭头的颜色和动画轨道，其他元素和时间线位置保持不变
```

AI 对已经存在的场景进行修改时，应生成局部 diff，并允许用户预览、Apply 和 Undo；修改后只使受影响的场景缓存失效，不应无条件重渲染整个项目。

---

### 2.5 可插拔模块定位

教学动画不是现有 Composite 的强制子功能，而是一个可选的内置 Feature Module：

```text
Topos
├── Edit（现有）
├── Composite（现有）
├── AI Assistant（现有共享服务）
└── Teaching Animation（新增，可启用/禁用/移除）
```

模块启用时：

- Composite 页面出现“添加 Motion Graphic Node”入口；
- AI Assistant 注册教学场景上下文和命令；
- Edit 时间线注册 Motion Graphic Clip 能力；
- 项目保存/读取注册教学动画 payload。

模块禁用或从构建中移除时：

- Composite 专业节点图仍能正常使用；
- Edit 普通视频、音频和字幕流程不受影响；
- AI Assistant 仍可处理已有的 Edit/Composite 能力；
- 没有教学动画的旧项目完全不感知该模块；
- 包含教学动画的项目保留未知 payload 和已有渲染缓存，并显示“缺少 Teaching Animation 模块”的可恢复提示，不能静默删除数据。

该设计允许不同产品变体选择不同默认值：

| 产品 | 默认状态建议 |
|---|---|
| Topos Studio | 可选启用 |
| Topos Composite | 可选启用 |
| Topos Edit | 默认不显示入口，但可以加载已存在的 Motion Graphic Clip |
| Topos Color | 默认禁用 |

### 2.6 模块目录和边界

建议新增独立目录：

```text
src/features/teaching_animation/
├── __init__.py
├── module.py                 # FeatureModule 注册入口
├── facade.py                 # 对 app/Edit/Composite 暴露的稳定接口
├── runtime.py                # 可选运行时和服务生命周期
├── core/
│   ├── model.py              # TeachingSceneDocument 等纯数据模型
│   ├── commands.py           # 场景修改、Apply、Undo
│   ├── validator.py          # schema 和能力校验
│   ├── template_registry.py  # 教学组件模板
│   └── migration.py          # 场景 schema 迁移
├── render/
│   ├── evaluator.py
│   ├── scene_renderer.py
│   ├── svg_renderer.py
│   ├── html_preview_renderer.py
│   ├── frame_adapter.py
│   ├── render_worker.py
│   └── cache.py
├── ui/
│   ├── motion_graphic_editor_window.py
│   ├── editor_window_registry.py
│   ├── storyboard_panel.py
│   ├── scene_view.py
│   ├── scene_inspector.py
│   └── scene_timeline.py
├── ai/
│   ├── context_provider.py
│   ├── commands.py
│   └── result_builder.py
├── locales/
└── tests/
```

模块允许依赖：

- `src.shared.*`；
- `src.features.composite.facade`；
- `src.features.edit.commands_facade`；
- AI Assistant 规定的稳定 provider/command 接口。

模块不允许：

- 从 `src.features.edit.ui.*` 直接导入 Edit UI 私有实现；
- 从 `src.features.composite.core.*` 直接依赖 Composite 内部节点实现；
- 把教学动画状态写进 AI Assistant Panel；
- 让 `src.shared` 反向依赖 `src.features.teaching_animation`。

所有跨模块操作都必须通过 `teaching_animation.facade`、Composite facade、Edit commands facade 或 AI provider contract 完成。

---

## 3. 当前项目能力基线

### 3.1 已有的可复用能力

| 现有文件/模块 | 已有能力 | 计划中的复用方式 |
|---|---|---|
| [`src/features/edit/core/timeline_v2.py`](../src/features/edit/core/timeline_v2.py) | `TimelineSequence`、视频/音频/字幕轨、FPS、分辨率、In/Out、序列化 | Motion Graphic Clip 的时间线承载和保存 |
| [`src/shared/keyframe.py`](../src/shared/keyframe.py) | Keyframe、KeyframeTrack、Linear/Bezier/Ease/Hold 插值 | 教学场景动画参数和编辑器曲线 |
| [`src/features/edit/core/timeline_render_plan.py`](../src/features/edit/core/timeline_render_plan.py) | 纯数据逐帧渲染计划 | 把图形片段作为新类型的可渲染层 |
| [`src/features/edit/core/timeline_export.py`](../src/features/edit/core/timeline_export.py) | 后台逐帧合成、编码、字幕烧录、音频导出 | 教学场景最终交付 |
| [`src/features/edit/core/captions/graphics.py`](../src/features/edit/core/captions/graphics.py) | 字幕与图形绑定、模板注册、参数同步、版本化 | 抽象出通用 Motion Graphic Template 思路 |
| [`src/features/composite/ui/composite_page.py`](../src/features/composite/ui/composite_page.py) | 节点图、撤销、预览、合成图生命周期 | MotionGraphicNode 的专业合成宿主 |
| [`src/features/composite/ui/keyframes_panel.py`](../src/features/composite/ui/keyframes_panel.py) | 关键帧列表、动画参数和时间范围 UI | 复用交互原则，独立编辑窗口提供简化版 |
| [`src/features/composite/render/composite_renderer.py`](../src/features/composite/render/composite_renderer.py) | 独立 GPU 渲染线程和异步预览 | 复杂效果和最终合成 |
| [`src/features/composite/render/composite_graph_render_service.py`](../src/features/composite/render/composite_graph_render_service.py) | CPU/GPU 合成服务接口、Composite 帧输出 | 教学场景输出接入 Composite/Export |
| [`src/app/ui/files_panel.py`](../src/app/ui/files_panel.py) | `QWebEngineView`、`QWebChannel`、Python/JavaScript 双向桥 | 参考 Web UI 嵌入和桥接方式 |
| [`src/app/controllers/dock_layout_controller.py`](../src/app/controllers/dock_layout_controller.py) | 可停靠、浮动、Tab 化 Dock 管理 | 让 AI Assistant 成为跨页面共享 Dock |
| [`src/features/edit/ui/ai_edit_assistant_panel.py`](../src/features/edit/ui/ai_edit_assistant_panel.py) | AI 对话 UI、上下文摘要、上下文检查器、结果状态 | 复用唯一实例，不复制会话 |
| [`src/app/controllers/composite_controller.py`](../src/app/controllers/composite_controller.py) | Edit ↔ Composite 上下文切换、Composite 数据持久化 | 教学场景与 Edit Clip 之间的上下文桥 |

### 3.2 当前需要特别注意的限制

1. `QWebEngineView` 是 Qt UI/Chromium 对象，不应直接在现有后台导出线程中当作普通渲染器调用。
2. [`src/main.py`](../src/main.py) 已经针对 QtWebEngine 与 OpenGL 冲突做了兼容处理；教学场景不能无条件把 WebEngine GPU 上下文和 ModernGL 上下文混在同一个渲染线程。
3. `CompositeGraphRenderService.render_gpu()` 当前仍是未完全激活的接口，不能把它当作已经完成的 HTML/WebGL GPU 导出后端。
4. HTML 直接作为项目文件格式会带来版本迁移、任意脚本、字体、资源路径和 AI 生成不稳定等问题。
5. 预览与最终导出必须共享同一套时间采样规则；不能只依赖浏览器的实时 CSS 动画时钟，否则会出现预览和视频逐帧不一致。

---

## 4. 总体架构

```text
┌──────────────────────────────────────────────────────────────┐
│ AI Assistant Dock                                             │
│ - 共享会话                                                     │
│ - 当前页面上下文                                               │
│ - 提案/确认/撤销                                               │
└───────────────────────┬──────────────────────────────────────┘
                        │ semantic commands
                        ▼
┌──────────────────────────────────────────────────────────────┐
│ Teaching Scene Domain                                          │
│ - TeachingSceneDocument                                        │
│ - SceneElement                                                  │
│ - SceneAnimation                                                │
│ - SceneAssetRef                                                 │
│ - SceneTemplateRegistry                                         │
└───────────────┬───────────────────────────┬───────────────────┘
                │ preview                   │ timeline binding
                ▼                           ▼
┌──────────────────────────┐      ┌─────────────────────────────┐
│ Scene Preview Renderer    │      │ Motion Graphic Clip Adapter │
│ HTML/CSS/SVG/Canvas       │      │ ClipInstance + scene_id     │
│ QWebEngine/Qt preview     │      │ composite_id/cache_id       │
└──────────────┬───────────┘      └──────────────┬──────────────┘
               │                                  │
               ▼                                  ▼
┌──────────────────────────┐      ┌─────────────────────────────┐
│ Deterministic Frame       │      │ TimelineRenderPlan           │
│ Renderer                  │─────▶│ TimelineExporter             │
│ SVG/CPU/ModernGL          │      │ FFmpeg                       │
└──────────────────────────┘      └─────────────────────────────┘
```

### 4.1 核心原则

- JSON 场景模型是唯一事实源；HTML 是渲染实现。
- 所有动画都以项目 FPS 下的整数 frame 为基准。
- 预览、缓存和最终导出使用同一套 `evaluate(frame)` 结果。
- AI 只生成结构化对象和命令，不直接执行任意 Python、GLSL 或 JavaScript。
- UI 只发信号/命令，渲染线程不操作 QWidget、QWebEngine 或 UI-owned OpenGL 对象。
- 生成场景默认保持可编辑；只有用户明确选择“渲染为普通视频”时才扁平化。
- Edit 与 Composite 通过现有 facade/command 边界连接，禁止跨 feature 直接导入内部实现。

---

## 5. 数据模型设计

### 5.1 `TeachingSceneDocument`

建议新增一个纯 Python、无 PyQt 依赖的场景模型：

```python
@dataclass(frozen=True)
class TeachingSceneDocument:
    scene_id: str
    schema_version: int
    name: str
    width: int
    height: int
    fps_num: int
    fps_den: int
    duration_frames: int
    background: dict[str, Any]
    elements: tuple[SceneElement, ...]
    animations: tuple[SceneAnimation, ...]
    markers: tuple[SceneMarker, ...] = ()
    asset_refs: tuple[SceneAssetRef, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
```

### 5.2 场景元素类型

第一版只实现可控、可验证的元素：

| 元素 | 典型教学用途 | 第一版支持 |
|---|---|---|
| `text` | 标题、定义、讲解文字 | 是 |
| `formula` | 数学公式、物理公式 | 是；可先用 SVG/文本 fallback |
| `shape` | 圆、矩形、线、箭头 | 是 |
| `image` | 插图、照片、教师上传素材 | 是 |
| `svg` | 流程图、坐标系、图标 | 是 |
| `chart` | 柱状图、折线图、饼图 | 第二阶段 |
| `video` | 教学视频片段 | 第二阶段，复用现有素材引用 |
| `canvas_effect` | 手绘、粒子、笔刷 | 第三阶段 |
| `webgl_effect` | 高级光效、复杂粒子 | 第三阶段 |

每个元素至少包含：

```text
element_id
type
z_index
visible
layout/transform
style
content or asset_ref
local_animation_tracks
```

### 5.3 动画模型

不要让场景直接依赖 CSS 的 wall-clock 动画。每个动画应转换为项目帧：

```json
{
  "element_id": "formula_01",
  "property": "opacity",
  "keyframes": [
    {"frame": 0, "value": 0.0, "interpolation": "ease_out"},
    {"frame": 12, "value": 1.0, "interpolation": "linear"}
  ]
}
```

建议复用 [`src/shared/keyframe.py`](../src/shared/keyframe.py) 的插值语义，并在场景层增加：

- `position.x/y`
- `scale.x/y`
- `rotation`
- `opacity`
- `color`
- `stroke_progress`
- `text_reveal_progress`
- `clip_path_progress`
- `chart_value`

### 5.4 场景与时间线的关联

建议新增一个场景引用/缓存合同，而不是只保存导出的 MP4：

```text
MotionGraphicClip
├── clip_id
├── scene_id
├── template_id
├── timeline_start
├── timeline_duration
├── scene_in_frame
├── scene_out_frame
├── composite_id (可选)
├── render_cache_id (可选)
└── source_snapshot (可选)
```

第一版可以先复用 `ClipInstance` 的以下字段：

- `asset_id`
- `animated_params`
- `timeline_start`
- `timeline_duration`
- `composite_id`
- `render_cache_id`
- `effects`

然后在图形资产或 `format_settings` 的受控命名空间中保存 `scene_id`。如果后续图形片段越来越多，再升级为显式 `MotionGraphicClipData`。

---

## 6. 渲染路线

### 6.1 第一阶段：SVG 优先

推荐 MVP 采用：

```text
TeachingSceneDocument
        ↓
SVG Scene Renderer
        ↓
QImage / numpy frame
        ↓
现有 TimelineExporter
```

原因：

- 文字、公式、箭头、流程图和几何图形足够清晰；
- 逐帧结果更容易测试；
- 不依赖浏览器实时动画时钟；
- 便于后续接入 ModernGL；
- 更容易解决字体和跨平台一致性问题。

### 6.2 第二阶段：HTML/CSS 预览

HTML/CSS 用于编辑体验和复杂排版预览：

- 使用现有 QtWebEngine/QWebChannel 方式进行 Python ↔ JavaScript 通信；
- Python 推送当前 `scene_json` 和当前 frame；
- JavaScript 只根据显式 frame 值渲染，不使用 `requestAnimationFrame` 作为时间源；
- 页面内禁止任意外部网络脚本和未审核脚本；
- WebEngine 预览与最终渲染结果必须通过 golden frame 测试比对。

### 6.3 第三阶段：Canvas/WebGL/ModernGL

- Canvas 用于图表、手绘和轻量粒子；
- WebGL 仅用于浏览器侧特殊效果；
- 最终合成优先考虑复用现有 ModernGL 和 Composite renderer，避免同时维护两套完整 GPU 后端；
- 若必须使用 WebGL 导出，使用独立 headless Chromium 子进程，不在 TimelineExporter 工作线程中直接创建 QWebEngine 对象。

### 6.4 预览与导出一致性合同

所有 renderer 必须实现类似接口：

```python
class SceneRenderer(Protocol):
    def prepare(self, scene: TeachingSceneDocument, context: RenderContext) -> None: ...
    def render_frame(self, frame: int) -> RenderedFrame: ...
    def cleanup(self) -> None: ...
```

要求：

- `frame=0` 和任意边界 frame 可重复渲染；
- 同一场景、同一字体、同一资源下输出稳定；
- 不把 UI 播放器的时间戳作为渲染依据；
- 取消任务后释放浏览器/GL/图片资源；
- 失败必须返回明确的错误和降级信息，不能静默输出空画面。

---

## 7. 独立 Motion Graphic 编辑窗口和双入口

### 7.1 窗口形态

教学动画不做成固定 Dock，而是做成可跨屏幕移动的非模态顶层编辑窗口：

```text
src/features/teaching_animation/ui/
├── motion_graphic_editor_window.py
├── storyboard_panel.py
├── scene_view.py
├── scene_inspector.py
└── scene_timeline.py
```

窗口应使用 `show()` 等非阻塞方式打开，不能使用阻塞式 `exec()`。建议使用 `QMainWindow` 或非模态 `QDialog`，并满足：

- 可以拖到第二块屏幕；
- 主窗口和编辑窗口可以同时操作；
- 记住窗口位置、尺寸和屏幕信息；
- 同一个 `scene_id` 同时只打开一个窗口；
- 再次双击同一 Clip/Node 时让已有窗口获得焦点；
- 关闭时处理 dirty 状态、保存、取消渲染和未完成 AI 操作。

窗口布局：

```text
┌──────────────────────────────────────────────────────────────┐
│ 场景列表/故事板 │ 教学画布预览                │ 属性检查器   │
│                  │ HTML/SVG/Canvas            │ 文字         │
│ 1 标题           │                            │ 样式         │
│ 2 概念           │                            │ 布局         │
│ 3 公式           │                            │ 动画         │
│ 4 例题           │                            │ 资源         │
├──────────────────────────────────────────────────────────────┤
│ 当前场景局部时间线 / 关键帧 / 播放控制 / 保存 / 加入时间线    │
└──────────────────────────────────────────────────────────────┘
```

### 7.2 三个核心对象

```text
MotionGraphicScene  ← 真正可编辑的场景文档
        ↑
MotionGraphicClip   ← Edit 时间线中的实例
MotionGraphicNode   ← Composite 节点图中的实例
```

`MotionGraphicClip` 和 `MotionGraphicNode` 都只保存 `scene_id`、实例参数和上下文引用，不能各自复制一份 HTML 或场景 JSON。

推荐的命名和职责：

- `MotionGraphicScene`：文字、颜色、元素、动画、模板和资源引用；
- `MotionGraphicClip`：时间线位置、时长、裁剪、外部变换和缓存引用；
- `MotionGraphicNode`：Composite 输入/输出、图层合成和图节点参数；
- `MotionGraphicEditorWindow`：场景编辑、预览、关键帧和保存。

### 7.3 入口一：时间线右键创建

```text
Edit graphics 轨道右键
        ↓
新建 Motion Graphic Clip
        ↓
创建唯一 scene_id 和默认场景
        ↓
插入当前播放头或选定 In/Out 范围
        ↓
打开 MotionGraphicEditorWindow
```

创建失败或用户取消空场景时，需要撤销临时 Clip 和临时 scene；已有内容的场景取消编辑时则保留原版本。

### 7.4 入口二：Composite 添加节点

```text
Composite 节点图添加 Motion Graphic Node
        ↓
创建 MotionGraphicScene 和 MotionGraphicNode
        ↓
节点输出当前 frame 的 RGBA 图像/纹理
        ↓
双击节点打开 MotionGraphicEditorWindow
```

节点可以连接到现有的 Merge、Transform、Glow、Blur 和 Output。节点注册必须通过可选模块的动态 registry hook 完成；不能把教学节点硬编码成 Composite 的强制依赖。

### 7.5 双击和窗口复用规则

时间线 Clip 或 Composite Node 双击时统一调用：

```python
open_motion_graphic_editor(
    scene_id=scene_id,
    source_context=source_context,
)
```

`source_context` 至少包含：

- `project_id`；
- `timeline_id`；
- `clip_id`（如果来自时间线）；
- `composite_id` / `node_id`（如果来自 Composite）；
- 当前 frame；
- 当前场景版本和缓存状态。

编辑器窗口通过 `scene_changed(scene_id, revision)`、`render_status_changed(...)` 和 `close_requested(...)` 与宿主通信。它不能直接操作 `TimelineView`、NodeGraph 私有对象或 QWidget-owned renderer。

### 7.6 场景复用和副本策略

初版新建 Clip/Node 时默认创建独立 `scene_id`，避免一个场景被多个位置引用后修改产生意外联动。后续再提供：

- 复用已有场景；
- 创建独立副本；
- 断开链接并保存 snapshot；
- 查看同一场景的所有使用位置。

如果多个实例确实共享同一个场景，编辑器必须在保存前显示“修改会影响 N 个实例”的提示。

### 7.7 教学组件库和专业 Composite 的关系

第一批模板包括标题页、章节标题、定义卡片、公式演示、流程图、箭头步骤、坐标系、对比表格、图表、例题推导和总结页。每个模板包含默认布局、可编辑字段、公开动画参数、关键帧能力、资源 fallback、`template_id`/版本和预览/导出能力声明。

普通老师只使用编辑窗口中的模板和属性控件。高级用户可以通过“在专业 Composite 中打开”查看或继续处理 `MotionGraphicNode`，但专业节点图不应成为教学动画的默认入口。

---

## 8. AI Assistant 跨页面实施设计

### 8.1 面板策略

当前 AI Assistant 已经作为一个 `QDockWidget` 创建，并支持浮动、移动和 Tab 化。实施时应：

1. 保留一个全局面板实例；
2. 将面板注册从 Edit-only 改为应用级共享 AI feature，或至少允许 Edit、Composite 和 Teaching Animation 上下文使用；
3. 不在 Composite 再创建第二份面板；
4. 保留同一会话、消息历史、模型 Profile 和上下文压缩状态；
5. 页面/编辑窗口切换时只替换 ContextBundle 和可用命令集合；
6. 编辑窗口可以位于第二块屏幕，主窗口中的 AI Assistant 仍然通过当前 `scene_id` 操作该窗口对应的场景。

### 8.2 上下文提供器

```text
EditContextProvider
├── 当前项目/时间线
├── 播放头和选中片段
├── 素材元数据
├── 字幕/音频状态
└── 可执行的剪辑命令

CompositeContextProvider
├── 当前合成 ID
├── 当前节点图摘要
├── 当前节点/参数/关键帧
├── 当前帧和输出尺寸
└── 可执行的合成命令

TeachingSceneContextProvider
├── scene_id
├── 场景 JSON 摘要
├── 选中的页面和元素
├── 当前模板能力
├── 渲染缓存状态
└── 可执行的教学动画命令
```

### 8.3 AI 命令

建议第一批实现：

| 命令 | 说明 | 是否需要确认 |
|---|---|---|
| `create_teaching_scene` | 根据课程主题创建场景 | 是 |
| `add_scene_page` | 添加标题、公式、流程或例题页 | 是 |
| `update_scene_element` | 修改文字、颜色、布局 | 可直接预览 |
| `set_scene_animation` | 添加/修改关键帧和缓动 | 可直接预览 |
| `reorder_scene_pages` | 调整页面顺序 | 是 |
| `render_scene_preview` | 生成预览缓存 | 否 |
| `insert_motion_graphic_clip` | 加入 Edit 时间线 | 是 |
| `replace_motion_graphic_clip` | 用新版本替换旧片段 | 是 |
| `render_flattened_video` | 渲染成普通视频素材 | 是 |

所有修改都应先形成可审阅的 diff/result card，再由用户 Apply；必须支持 Undo。

### 8.4 对话示例

```text
老师：讲解牛顿第二定律，做成 60 秒的高中物理演示。
AI：生成 6 个场景，包含标题、概念、公式、受力图、例题和总结。
老师：公式先显示 F，再显示 ma，最后用箭头连接起来。
AI：更新公式场景的 reveal 动画，预览第 3 页。
老师：确认，放到当前时间线播放头位置。
AI：创建 Motion Graphic Clip，预计 60 秒，已加入 graphics 轨道。
```

---

## 9. 加入 Edit 时间线的设计

### 9.1 两种输出模式

#### A. 动态图形片段（默认推荐）

```text
时间线 Clip
 ├── scene_id
 ├── scene_json/version
 ├── composite_id（如果经过 Composite graph）
 ├── render_cache_id
 └── timeline transform/opacity/keyframes
```

优点：可以回到独立编辑窗口修改，重新渲染缓存后更新时间线。

#### B. 扁平化视频素材

生成 MP4/MOV/ProRes 后作为普通素材加入 Media Pool 和时间线。

适用场景：

- 需要交给外部软件；
- 需要冻结版本；
- 场景使用了不支持可编辑回溯的特殊效果；
- 用户明确选择“渲染为普通视频”。

### 9.2 插入逻辑

建议通过 Edit 的命令/Facade 完成：

```text
MotionGraphicEditorWindow
        ↓
Teaching Animation Facade: export_scene_reference()
        ↓
Edit Commands Facade: insert_motion_graphic_clip()
        ↓
TimelineSequence / graphics track
        ↓
TimelineView refresh + preview request
```

禁止独立编辑窗口直接修改 `TimelineView` 内部对象或直接导入 Edit UI 私有实现。

Composite 的另一条入口不经过时间线插入命令：

```text
MotionGraphicEditorWindow
        ↓ save(scene_id)
Teaching Animation Facade
        ↓
MotionGraphicNode 输出当前 frame 的 RGBA 图像/纹理
        ↓
Composite NodeGraph
```

两条路径共享同一个 `MotionGraphicScene`，但 `MotionGraphicClip` 负责时间线实例，`MotionGraphicNode` 负责合成图实例。

### 9.3 图形轨道

现有字幕图形绑定已经使用 graphics track 语义。建议扩展为：

- `track_role="graphics"`；
- `graphic_kind="caption" | "teaching_scene" | "shape" | "chart"`；
- 图形片段和普通视频片段共享时间线坐标；
- 图形片段必须参与 `TimelineRenderPlan` 和导出 preflight；
- 时间线中显示专用颜色、图标和场景名称。

---

## 10. 分阶段实施计划表（顺序锁定）

以下阶段必须按顺序推进。后一阶段不得绕过前一阶段的验收标准直接开始；每个阶段完成后都要运行对应测试，并确认旧功能没有回归。

### Phase 0：可插拔模块骨架与跨模块合同

| 编号 | 任务 | 主要位置 | 依赖 | 交付物 | 验收标准 |
|---|---|---|---|---|---|
| P0-01 | 创建 `TeachingAnimationModule` | `src/features/teaching_animation/module.py` | 无 | FeatureModule 注册入口 | 模块可发现、可启用、可禁用 |
| P0-02 | 创建稳定 facade | `src/features/teaching_animation/facade.py` | P0-01 | scene/preview/insert/export 接口 | 外部模块不访问内部目录 |
| P0-03 | 增加 feature flag 和产品 preset | feature registry/products | P0-01 | 启用/禁用配置 | 关闭模块后基础产品启动不变 |
| P0-04 | 定义缺模块 fallback 合同 | project IO/manifest | P0-02 | unknown payload、offline clip 状态 | 数据和缓存不被静默删除 |
| P0-05 | 增加 Composite 动态节点注册 hook | Composite facade/node registry | P0-02 | register/unregister node provider | 未安装模块时 Composite 不导入 MotionGraphicNode |
| P0-06 | 定义 Scene JSON、RenderedFrame、RenderError 合同 | teaching_animation/core + facade | P0-02 | 跨模块接口文档 | 取消、失败、降级都可诊断 |
| P0-07 | 定义 frame-based animation contract | 复用 `src/shared/keyframe.py` | P0-06 | frame/easing 规范 | 同一 frame 结果稳定 |
| P0-08 | 做 SVG 逐帧技术验证 | `src/features/teaching_animation/render/` | P0-06 | 标题+箭头 demo | 预览帧与导出帧一致 |
| P0-09 | 做 WebEngine 预览桥接验证 | `src/features/teaching_animation/ui/` | P0-06 | 本地 preview demo | 不影响现有 FilesPanel/ModernGL |
| P0-10 | 补充模块边界和移除测试 | `tests/feature_availability/` | P0-01 | import guard/fallback 测试 | 删除模块后普通项目仍可打开、保存、导出 |

### Phase 1：教学场景数据层

| 编号 | 任务 | 主要文件 | 依赖 | 交付物 |
|---|---|---|---|---|
| P1-01 | 创建 `TeachingSceneDocument` | `src/features/teaching_animation/core/model.py` | P0 | 场景、元素、动画、资源模型 |
| P1-02 | 创建 `MotionGraphicClip`/`MotionGraphicNode` 引用模型 | `core/model.py`、facade | P1-01 | scene/clip/node 三者关联合同 |
| P1-03 | 创建模板注册表 | `core/template_registry.py` | P1-01 | 模板 ID、版本、默认参数、能力声明 |
| P1-04 | 创建 JSON validator | `core/validator.py` | P1-01 | 类型、范围、引用和动画校验 |
| P1-05 | 创建 schema migration | `core/migration.py` | P1-01 | v1 → 后续版本迁移入口 |
| P1-06 | 接入独立 project payload | shared project + facade | P1-05 | 教学场景单独保存，不污染旧 timeline schema |
| P1-07 | 实现缺模块 payload 保留 | project IO/manifest | P1-06 | 原始场景和缓存可保留 |
| P1-08 | 实现 Undo-safe scene commands | `core/commands.py` | P1-01 | 添加元素、改参数、改顺序、改时长 |
| P1-09 | 编写数据层测试 | `src/features/teaching_animation/tests/` | P1-01~08 | round-trip、migration、validator、undo/redo |

### Phase 2：确定性场景渲染器

| 编号 | 任务 | 主要文件 | 依赖 | 交付物 |
|---|---|---|---|---|
| P2-01 | 实现 scene evaluator | `render/evaluator.py` | Phase 1 | `evaluate(scene, frame)` |
| P2-02 | 实现 SceneRenderer protocol | `render/scene_renderer.py` | P2-01 | prepare/render_frame/cleanup |
| P2-03 | 实现 SVG renderer | `render/svg_renderer.py` | P2-02 | text/shape/formula/image/svg |
| P2-04 | 实现字体和资源 preflight | `render/asset_resolver.py` | P2-03 | 缺字体/图片阻断或明确降级 |
| P2-05 | 实现 HTML/CSS preview renderer | `render/html_preview_renderer.py` | P2-02 | JSON → 安全 HTML/SVG 预览 |
| P2-06 | 实现 RGBA frame/texture adapter | `render/frame_adapter.py` | P2-03 | Scene frame 可被 Composite/Timeline 消费 |
| P2-07 | 实现 golden frame 测试 | `tests/render/` | P2-03 | 标题、公式、箭头、淡入、缩放基准 |
| P2-08 | 测试取消和资源释放 | `tests/render/` | P2-02 | 连续渲染/取消无泄漏 |

### Phase 3：独立 Motion Graphic 编辑窗口

| 编号 | 任务 | 主要文件 | 依赖 | 交付物 |
|---|---|---|---|---|
| P3-01 | 创建非模态编辑窗口 | `ui/motion_graphic_editor_window.py` | Phase 2 | 可跨屏移动的独立窗口 |
| P3-02 | 创建场景列表/故事板 | `ui/storyboard_panel.py` | P3-01 | 新增、复制、删除、排序场景 |
| P3-03 | 创建教学画布预览 | `ui/scene_view.py` | P3-01/P2 | frame scrubbing、缩放、适配画布 |
| P3-04 | 创建属性检查器 | `ui/scene_inspector.py` | P1/P3-03 | 文本、颜色、字体、布局、资源 |
| P3-05 | 创建场景局部时间线 | `ui/scene_timeline.py` | P1/P3-01 | 播放、标记、关键帧和范围 |
| P3-06 | 实现元素拖动和层级编辑 | scene view/controller | P3-03/P3-04 | 位置、缩放、旋转、层级可撤销 |
| P3-07 | 实现简化动画参数 | scene inspector/timeline | P3-05 | 淡入、滑入、缩放、打字机、延迟、时长、缓动 |
| P3-08 | 接入高级关键帧编辑 | 复用 `src/shared/keyframe.py` 交互原则 | P3-05 | opacity/position/scale/rotation |
| P3-09 | 实现单窗口注册表 | `ui/editor_window_registry.py` | P3-01 | 同一 scene_id 只打开一个窗口 |
| P3-10 | 实现 dirty/save/close 生命周期 | editor controller | P1/P3-01 | 关闭提示、保存、取消渲染、恢复窗口几何 |
| P3-11 | 实现局部 scene diff 预览 | scene commands/result state | P1/P3-04 | 修改文字/颜色/动画只更新受影响元素 |
| P3-12 | 编写编辑窗口测试 | `tests/ui/` | P3-01~11 | 多屏几何、双击复用、撤销、关闭、恢复 |

### Phase 4：双入口宿主接入

这一阶段先打通手动操作闭环，不接 AI。只有老师可以手动创建、双击、编辑、预览并回到宿主页面后，才进入 AI 阶段。

| 编号 | 任务 | 主要位置 | 依赖 | 交付物 |
|---|---|---|---|---|
| P4-01 | 时间线 graphics 轨道右键创建 Clip | Edit commands facade | Phase 3 | 创建默认 scene + Clip + 打开窗口 |
| P4-02 | 时间线 Clip 双击打开窗口 | TimelineView/Controller | P4-01 | 通过 scene_id 找到并聚焦已有窗口 |
| P4-03 | 定义 scene reference manifest | Edit/Composite facade | P1/P4-01 | scene/version/cache/资源引用 |
| P4-04 | 创建 graphics Clip 插入命令 | Edit commands facade | P4-01 | 播放头、In/Out、覆盖、追加 |
| P4-05 | 接入 TimelineRenderPlan | `timeline_render_plan.py` adapter | P2/P4-03 | timeline frame 返回图形层 |
| P4-06 | 接入时间线预览帧提供器 | Edit preview/frame provider | P4-05 | 播放、拖动、缩略图显示场景 |
| P4-07 | 注册 MotionGraphicNode | teaching module + Composite dynamic registry | P0-05/P2 | 模块启用时节点可添加 |
| P4-08 | Composite 节点双击打开窗口 | Composite controller/facade | P4-07 | Node 与 Clip 共用 scene_id |
| P4-09 | 实现节点 RGBA 输出 | `render/frame_adapter.py` + Composite adapter | P2/P4-07 | 可连接 Merge/Transform/Glow/Blur/Output |
| P4-10 | 实现场景变更同步 | facade signals | P3/P4-01/P4-07 | scene_changed 刷新 Clip、Node 和窗口 |
| P4-11 | 接入缩略图、撤销、保存和重开 | TimelineView/project IO | P4-04~10 | Clip 可移动、裁剪、撤销、恢复 |
| P4-12 | 实现双入口集成测试 | tests/integration | P4-01~11 | 右键创建和节点创建都能打开同一编辑器 |

### Phase 5：共享 AI Assistant 接入

| 编号 | 任务 | 主要位置 | 依赖 | 交付物 |
|---|---|---|---|---|
| P5-01 | 将 AI provider 注册改为可选 | AI harness/feature registry | Phase 0 | 模块关闭时不注册教学命令 |
| P5-02 | 保持唯一 AI Assistant Dock | dock layout/controller | 现有 AI 面板 | Edit/Composite/编辑窗口共用会话 |
| P5-03 | 定义 ContextProvider protocol | AI harness/facade | P0/P4 | 当前项目、scene、Clip、Node 上下文 |
| P5-04 | 实现 TeachingScene context provider | `ai/context_provider.py` | P3/P4 | scene JSON 摘要、选中元素、能力列表 |
| P5-05 | 增加创建和修改命令 | `ai/commands.py` | P1/P3 | create/add/update/reorder/set_animation |
| P5-06 | 增加预览、加入时间线、替换 Clip 命令 | AI command registry | P4 | render_preview/insert/replace |
| P5-07 | 增加 diff/confirm/Apply/Undo | result builder + commands | P1/P3/P5-05 | AI 不静默覆盖人工修改 |
| P5-08 | 增加模型无关 dry-run | AI harness tests | P5-05 | 无模型也能验证命令合同 |
| P5-09 | 编写 AI 端到端测试 | tests/ai | P5-01~08 | 生成初稿→人工改→加入时间线 |

### Phase 6：缓存、后台渲染和最终导出

| 编号 | 任务 | 主要位置 | 依赖 | 交付物 |
|---|---|---|---|---|
| P6-01 | 创建 scene render cache manifest | `render/cache.py` | P2/P4 | renderer/version/scene/font/asset hash |
| P6-02 | 实现场景级缓存失效 | cache manager | P3/P5 | 改文字只失效相关场景/元素 |
| P6-03 | 实现后台 render worker | `render/render_worker.py` | P2/P6-01 | 不阻塞 UI、可取消、可恢复 |
| P6-04 | 直接输出 RGBA 帧供 Composite/Timeline | frame adapter | P2/P4 | 不强制先生成 MP4 |
| P6-05 | 接入现有 FFmpeg 导出 | TimelineExporter/Deliver | P4/P6-03 | MP4/MOV/WebM/ProRes |
| P6-06 | 支持扁平化视频素材 | export facade | P6-05 | 用户明确选择时输出普通素材 |
| P6-07 | 实现 HTML headless export | optional subprocess | P2/P6-03 | 不在 QWebEngine UI 线程导出 |
| P6-08 | 处理透明背景、字体和资源 preflight | export preflight | P2/P6-05 | 缺资源不静默产出黑帧 |
| P6-09 | 加入进度、取消和错误回报 | UI/controller | P6-03 | 帧数、阶段、缓存命中率可见 |
| P6-10 | 加入最终质量门 | Deliver/export tests | P6-05~09 | 帧数、时长、分辨率、音画同步正确 |
| P6-11 | 实现无模块 Clip fallback | Edit facade/timeline renderer | P0/P4/P6 | 缺模块时显示缓存视频或 offline 状态 |
| P6-12 | 隔离高级 HTML/headless 依赖 | packaging/optional extras | P6-07 | 未启用模块不增加基础产品负担 |

### Phase 7：Canvas/WebGL/高级能力

只有 Phase 0~6 的垂直闭环稳定后才开始：

| 编号 | 任务 | 说明 |
|---|---|---|
| P7-01 | Canvas 图表和手绘组件 | 以显式 frame 驱动，不依赖实时钟 |
| P7-02 | 公式排版增强 | MathJax/KaTeX 或本地 SVG 转换，资源必须离线可封装 |
| P7-03 | ModernGL 特效节点 | glow、粒子、遮罩接入现有 Composite renderer |
| P7-04 | WebGL 预览效果 | 可选后端，失败回退 SVG/CPU |
| P7-05 | 图表数据驱动 | AI 根据 CSV/JSON 生成 chart 元素 |
| P7-06 | 语音、字幕和口播同步 | 复用 Edit 音频/字幕系统，场景提供 cue/marker |
| P7-07 | 多语言版本 | scene graph 切换文本语言并重新做字体布局 preflight |

---

### 10.8 阶段门槛和不可跳过的依赖

每个门槛都必须通过后，才能进入下一阶段：

| 门槛 | 必须先完成 | 通过条件 | 才能进入 |
|---|---|---|---|
| Gate 0 | Phase 0 | 模块可启用/禁用；普通项目不受影响；跨模块只走 facade | Phase 1 |
| Gate 1 | Phase 1 | 场景 JSON 可保存、重开、迁移；命令可 Undo/Redo | Phase 2 |
| Gate 2 | Phase 2 | 同一个 frame 在预览、时间线和导出中结果一致；取消不泄漏 | Phase 3 |
| Gate 3 | Phase 3 | 独立窗口可跨屏移动；文字、颜色、时长和关键帧可人工修改 | Phase 4 |
| Gate 4 | Phase 4 | 时间线 Clip 和 Composite Node 都能创建、双击并引用同一场景 | Phase 5 |
| Gate 5 | Phase 5 | AI 生成结果可审阅、Apply、Undo；人工修改不会被静默覆盖 | Phase 6 |
| Gate 6 | Phase 6 | 缓存、后台渲染、取消、缺模块 fallback 和最终导出稳定 | Phase 7 |

以下顺序不可调换：先有结构化场景，再有确定性渲染；先有独立窗口和人工编辑，再接宿主入口；先打通手动闭环，再接 AI；最后才扩展高级渲染后端。

---

## 11. 测试和验收矩阵

### 11.1 数据和命令

- 新建、复制、删除场景后 JSON 可 round-trip。
- 旧项目打开时不存在教学动画也不报错。
- 新版本读取旧场景时执行明确 migration。
- AI command 的 dry-run 不改变工程。
- Apply 后可以通过 Undo 完整恢复。
- AI 修改只影响声明的 scene/element，不污染其他时间线对象。

### 11.2 渲染一致性

- 标题、公式、箭头、图片、流程图在 frame 0、中间帧、结束帧正确。
- SVG 预览与导出帧在允许的像素误差内一致。
- 预览播放头和时间线播放头使用同一 frame。
- FPS 为 24、25、29.97、30、60 时动画时长不漂移。
- 缺字体、缺图片、损坏 SVG 会显示错误，不输出静默空画面。
- 连续渲染 10 分钟场景后内存和句柄不会无限增长。

### 11.3 时间线

- 从 Composite 点击加入时间线后 Clip 出现在正确的 graphics track。
- 插入、覆盖、追加、移动、裁剪和删除均可撤销。
- 保存项目、关闭、重开后 scene_id、场景版本和缓存关系保持。
- 双击 Motion Graphic Clip 可以重新打开对应教学场景。
- 重新渲染后替换原片段，位置、时长和选择状态符合预期。
- 普通视频、图形、字幕和音频混合导出无层级错误。

### 11.4 AI Assistant

- Edit 和 Composite 使用同一个会话历史。
- 页面切换后上下文摘要正确变化。
- AI 能明确区分“修改当前场景”和“修改主时间线”。
- AI command 失败时返回结构化错误，不留下半完成状态。
- Assistant Dock 浮动、停靠、Tab 化、关闭和重新打开均正常。
- 面板隐藏时不触发不必要的场景抓取和渲染。

### 11.5 线程和资源

- UI 线程不执行长时间逐帧导出。
- 导出 worker 不访问 QWidget/QWebEngineView。
- ModernGL 对象只在拥有上下文的渲染线程创建和释放。
- WebEngine 预览与 ModernGL 共存时不引入新的上下文冲突。
- 取消渲染后浏览器进程、临时文件、纹理、PBO 和 encoder 都能清理。

### 11.6 AI 结果的可编辑性

- AI 生成的标题、段落、公式和图表文字可以直接在检查器中修改。
- AI 生成的颜色、背景、描边、字体和布局可以局部修改。
- AI 生成的入场方式、延迟、持续时间、方向和缓动可以通过简化控件调整。
- 高级用户可以打开关键帧视图修改 opacity、position、scale 和 rotation。
- 修改一个元素不会重置其他元素的内容、层级和动画。
- 修改一个场景不会改变 Edit 时间线中其他 Clip 的位置和内容。
- AI 修改显示局部 diff，Apply 后可以 Undo。
- 修改后的场景重新打开时保留老师的人工修改，不会被旧 AI 草稿覆盖。
- 修改文字后，关联 Motion Graphic Clip 可以重新渲染并替换缓存，而不必重新生成整个课程视频。
- 只有用户选择“渲染为普通视频素材”时，才允许明确丢失场景编辑链接。

---

## 12. 性能目标

第一版目标不应追求电影级复杂特效，而应保证老师制作简单课程时响应稳定：

| 场景 | 目标 |
|---|---|
| 单个教学页面预览 | 1080p 代理分辨率下交互无明显卡顿 |
| 简单标题/公式/箭头动画 | 拖动播放头时能快速显示最新帧 |
| 60 秒教学片段 | 后台可取消、可显示进度、不会阻塞主窗口 |
| 缓存命中 | 只修改一个场景时不重新渲染整个项目 |
| 时间线缩放/滚动 | 不因图形 Clip 数量增加而逐帧重算整个时间线 |
| AI 修改 | 先更新结构化场景和预览，用户确认后才启动完整导出 |

后续再根据真实样本决定是否把 SVG renderer、ModernGL 和 headless browser 做并行渲染。

---

## 13. 主要风险与应对

| 风险 | 影响 | 应对方案 |
|---|---|---|
| HTML 直接作为项目格式 | 难以迁移、校验和撤销 | JSON scene graph 为事实源 |
| QWebEngine 与 ModernGL 冲突 | 启动、预览或导出不稳定 | UI 只预览，导出独立进程或 SVG/CPU |
| 浏览器 CSS 动画和逐帧导出不一致 | 视频与预览不同步 | 所有动画由 frame 显式驱动 |
| 字体缺失导致布局变化 | 公式、字幕和教学文字错位 | 字体 preflight、字体绑定和 fallback |
| AI 生成无效 HTML/脚本 | 崩溃、越权或不可复现 | 模板白名单、schema validator、命令确认 |
| 图形片段扁平化后不可编辑 | 用户修改成本高 | 默认保存 scene reference + cache |
| AI 结果只能整体重生成 | 小修改成本高、用户不信任结果 | 场景元素/样式/动画结构化保存，支持局部 diff 和局部缓存失效 |
| 人工修改被后续 AI 覆盖 | 用户失去控制感 | 修改版本、字段级 Apply、人工修改优先级和 Undo |
| Composite 专业节点模式对普通老师太复杂 | 学习成本高 | 独立编辑窗口与专业节点模式分离 |
| 场景渲染线程泄漏资源 | 长视频导出失败 | worker 生命周期、取消测试、telemetry |
| 跨 feature 直接导入内部模块 | 架构回归 | 通过 facade/commands facade 接入并跑 boundary tests |

---

## 14. 推荐实现顺序

不要一开始同时开发完整 HTML、Canvas、WebGL、AI 和时间线。建议严格按以下顺序：

```text
Phase 0  可插拔模块骨架、feature flag 和跨模块合同
Phase 1  TeachingSceneDocument、JSON schema、保存/迁移/Undo
Phase 2  确定性 SVG/RGBA renderer、预览和导出帧一致性
Phase 3  独立 Motion Graphic 编辑窗口、画布、检查器和局部时间线
Phase 4  时间线 Clip 与 Composite Node 两个手动入口
Phase 5  共享 AI Assistant 上下文、命令、diff、Apply 和 Undo
Phase 6  场景缓存、后台渲染、缺模块 fallback 和 FFmpeg 导出
Phase 7  HTML/CSS 高级能力、Canvas、WebGL/ModernGL、图表和多语言
```

第一个可交付垂直切片应当是：

实施时先用默认场景和手动命令打通前半段，完成 Gate 4 后再接入 AI；下面是双入口和 AI 都具备后的完整用户闭环：

```text
老师说“讲解一个物理公式”
 → AI 创建标题 + 公式 + 两个箭头
 → 打开 Motion Graphic 独立编辑窗口预览
 → 老师修改公式颜色和淡入时长
 → 点击加入时间线
 → Edit graphics 轨道出现一个可回溯的 Motion Graphic Clip
 → 导出为 MP4
```

只有这个闭环稳定后，才继续增加图表、Canvas、WebGL、复杂 AI 编排和多语言。

其中“可编辑闭环”必须包含一个真实的人工修正步骤：AI 生成文字和动画后，老师至少能够修改一处文字、一处颜色和一处动画时长，再将修改后的场景加入时间线。不能只验证“AI 能生成并导出视频”。

## 15. 阶段完成定义（Definition of Done）

某个阶段只有同时满足以下条件才算完成：

1. 有对应的数据/接口文档。
2. 有最小可运行示例。
3. 有单元测试和至少一个集成测试。
4. 预览、保存、重开和导出链路都能工作。
5. 不违反模块边界和线程安全约束。
6. 失败、取消、缺资源和降级状态可被用户看到。
7. 不依赖隐藏的 UI 全局变量。
8. AI 产生的修改可以审阅、应用和撤销。
9. 新功能不影响没有教学动画的旧项目和普通剪辑流程。
10. 模块可以通过 feature flag、产品 preset 或构建配置关闭。
11. 移除模块后，普通项目可以打开、编辑、保存和导出。
12. 缺少模块但项目包含教学动画时，数据和缓存可保留，并有明确的恢复路径。

---

## 16. 可插拔模块专项验收

### 16.1 启用模块

- Feature Registry 能发现并注册 `TeachingAnimationModule`。
- Composite 页面出现“添加 Motion Graphic Node”入口。
- 独立编辑窗口、场景 renderer、AI provider 和 Edit 插入命令按需初始化。
- 未打开教学动画功能时，不启动 WebEngine 预览服务、不创建额外 renderer worker。

### 16.2 禁用模块

- 现有 Edit 页面、Composite 专业节点页和 AI Assistant 不报 ImportError。
- 没有教学动画数据的项目打开、保存、导出结果与模块启用前一致。
- 面板菜单不会显示失效的教学动画动作。
- AI 不会继续发送无法执行的教学动画命令。

### 16.3 移除模块

- 从构建包中移除 `src/features/teaching_animation` 后，基础产品仍可启动。
- 项目文件中的教学动画 payload 不被删除或覆盖。
- 已经渲染的缓存素材可以作为 offline/flattened fallback 使用。
- 时间线中对应 Clip 显示“缺少模块”状态，而不是黑帧或静默消失。
- 模块重新安装后，可以根据 `scene_id` 和版本重新接回场景。

### 16.4 版本和依赖隔离

- 教学动画专用依赖尽量放在 optional extra 或独立运行时中。
- WebEngine/headless browser 不应成为 Color 产品的强制启动依赖。
- 场景 schema、renderer version、template version 和 font hash 写入 manifest。
- 模块升级失败时，旧缓存和旧场景数据仍可读取或降级为静态素材。

---

## 17. 最终建议

这项功能的最佳定位不是“在 Topos 里嵌一个网页播放器”，而是：

> 以独立可插拔模块提供一个面向老师的、AI 驱动的 Motion Graphic 编辑窗口；用结构化场景模型描述类似 Flash 的舞台、元件和时间轴；用 HTML/CSS/SVG/Canvas/ModernGL 负责渲染；同时以 MotionGraphicClip 进入 Edit 时间线、以 MotionGraphicNode 接入 Composite。

AI 只负责生成初稿和提出局部修改，老师始终拥有对文字、颜色、布局和动画参数的最终控制权。教学动画作为独立可插拔模块内置，既可以与 Composite、Edit 和共享 AI Assistant 协作，也可以在不需要时关闭或从构建中移除。这样既利用了 Topos 已有的时间线、关键帧、Composite、缓存和导出能力，也避免把项目变成不可维护的“AI 随机生成 HTML + 截屏”系统。
