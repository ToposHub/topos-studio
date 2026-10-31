# MotionGraphicClip 系统化优化计划（Remotion 设计启发，原生实现）

版本：v1.1
日期：2026-08-12
状态：核心实现已完成 — W0-A / W0-B / W1 / W2 / W3 / W4 / W5 / W6 已收口；发布前仍需按环境执行真实浏览器/GPU 与人工视觉验收（见 §5.1.2）
范围：`src/features/teaching_animation/`、Edit 时间线、Composite、共享 Effect Catalog 与 AI Assistant
不采用：嵌入或依赖 Remotion 运行时

> 退出门证据见 §5.1.2（代码入口 / 真实测试 / CI / 证据 / 阻塞项）。本节取代旧"规划中"状态：每个波次按可复现的代码与测试记录，而非规划描述。

---

## 1. 决策与目标

### 1.1 产品决策

Topos 保持 `MotionGraphicScene` / `MotionGraphicClip` 为唯一的工程资产容器：它可被 AI 生成、在独立编辑器中修改、放入 Edit 时间线、作为 Composite 节点输入，并在 preview 与 export 使用同一帧语义。**不将场景退化为 MP4，也不把任意 JavaScript/React 项目当作可执行工程真源。**

结构化元素并非唯一内容形态。为获得大语言模型在网页视觉设计上的表达力，场景可以承载一个受控的 `FreeformHtmlMotionLayer`：其已净化的 HTML/CSS 是该 layer 的版本化源数据，但仍由 `MotionGraphicScene` 持有 `scene_id`、素材引用、渲染 profile、命令历史与项目兼容责任。它不是 Remotion 兼容层，也不运行模型生成的 JavaScript。

本计划借鉴 Remotion 的动画语言和模板化方法，不复制其 React/Chromium 运行时：

| Remotion 设计 | Topos 等价设计 | 采用原因 |
|---|---|---|
| 当前帧 + FPS 是所有动效的唯一时间输入 | `FrameContext` / 显式局部时间域 | 可随机访问任意帧；preview/export/缓存一致 |
| `interpolate()`、easing、clamp | `MotionValue`、多段映射与外推策略 | 让 AI/模板使用稳定、可组合的动画原语 |
| `spring()`、`measureSpring()` | 物理弹簧曲线和可预先测量的 settle frame | “弹出感”可控，且时长能纳入节拍编排 |
| `Sequence` / `Series` | 层级 `TimeRange`、`SequenceGroup`、`SeriesGroup` | 局部时间从零开始；顺序片段、重叠、裁剪明确 |
| `TransitionSeries` | scene/beat/clip 级 transition contract | 转场从特殊效果升级为有输入/输出/时长的资产 |
| 参数化 Composition | 参数 schema + 模板实例 + 品牌 token | AI 选模板并填参数，不自由拼不可维护的场景 |
| 丰富的媒体、字体和视觉包 | 受控 Asset Provider + native renderer capability | 扩展素材能力且不牺牲离线、可审计和导出一致性 |

参考：Remotion 的 [`spring`](https://www.remotion.dev/docs/spring)、[`interpolate`](https://www.remotion.dev/docs/interpolate)、[`Sequence`](https://www.remotion.dev/docs/sequence)、[`Series`](https://www.remotion.dev/docs/series) 与 [Transitions](https://www.remotion.dev/docs/transitions/presentations) 文档。它们是设计参考，不是 API 兼容承诺。

### 1.2 目标体验

用户输入：

> 创建一个 15 秒、16:9、中文、深色科技风的“AI Agent 四步骤”动画；标题弹入，四张卡片依次出现，步骤切换有转场，插入当前 Graphics 轨道。

系统应能：

1. 生成可阅读的 brief、节拍表、模板候选和低清预览；
2. 以结构化的原语、模板实例、元素层级与关键帧保存，而不是保存提示词或位图；
3. 支持用户说“第二张卡晚 8 帧，弹性更弱，标题改为品牌蓝”；
4. 在 Edit、Composite、独立编辑窗口和最终导出中得到同一帧的像素语义；
5. 允许把复杂视觉效果交给 native ModernGL/Composite 后端，同时对不支持的能力明确降级或阻断；
6. 在 4K 项目中保持可用的代理预览、局部失效、后台渲染、取消和缓存策略。

### 1.3 非目标与边界

| 不做 | 原因 / 替代 |
|---|---|
| 执行任意 HTML/JS 或开放任意网页 URL | `FreeformHtmlMotionLayer` 仅允许净化的 HTML/CSS；没有 script、事件处理器、iframe、网络与本机路径访问 |
| 在 UI 线程进行 SVG/GL 渲染 | 违反现有线程与 GPU 边界；渲染只走现有 worker/scheduler |
| 为了“效果多”把每个 effect 私有化 | 全部 effect 必须经 `EffectSpec` / Capability / 版本化契约 |
| 一次重写全部渲染器 | 先建立 renderer-neutral scene IR，再渐进引入 native GPU pass |
| 让 LLM 直接写内部模型或 GL shader | LLM 只发受限命令；模型、验证、撤销和资源生命周期由核心层控制 |
| 把 Motion Graphic Clip 做成任意网页/原型编辑器 | 优先支持线性、可帧寻址的视频动效；prototype 交互和任意 JavaScript 不进入导出合同 |

### 1.4 Freeform HTML Motion 的产品策略

当前阶段优先验证“AI 自由设计是否能显著改善视觉质量”，**不以模板数量为前置条件**。因此提供实验性 Freeform HTML Motion 模式：AI 可自由组合受支持的 HTML5 与 CSS（包括 Flex/Grid、渐变、阴影、mask、filter、`@keyframes`、SVG 图标），Topos 负责净化、按帧 seek、离屏渲染和质量校验。

| 项目 | 第一阶段策略 | 后续收敛策略 |
|---|---|---|
| 页面结构 | AI 自由生成受限 HTML/CSS，不要求每个 DOM 节点映射为原生元素 | 从高质量生成结果中提炼可执行模板和原生 SceneGraph 组件 |
| 用户编辑 | 支持替换文本、图片 asset slot、CSS custom properties、动画时长/延迟，以及带 `data-topos-node-id` 的节点直接选择/变换；不承诺任意 DOM 的可视化编辑 | 高频模板逐步提供完整 Inspector、约束布局和原生元素编辑 |
| 动画 | 允许 CSS `@keyframes`、transform、opacity、filter；由受信任渲染桥暂停并 seek | 逐步把常用动画编译/迁移为 `interpolate`、spring、preset、transition |
| 渲染 | 受控离屏浏览器输出 RGBA frame；作为独立 renderer capability | 常用效果转入 ModernGL/Composite，以获得更好的性能和合成控制 |
| 失败处理 | sanitizer、资源、字体、超时或 CSS capability 失败时给可诊断错误卡 | 对已产品化模板提供 native fallback；自由页面不伪造等价结果 |

自由度的边界是“**自由设计、受控执行**”：模型可设计页面，但不获得网络、文件系统、脚本执行、任意 npm 包或 GPU API 权限。

### 1.5 Figma 式动效编辑器的产品策略

Motion Graphic Clip 应升级为“**Figma 式画布与属性编辑 + 专业时间线**”，而非另做一个泛用网页制作器。借鉴 Figma 的直接选择、图层树、检查器、自动布局、约束、组件实例和变量，但所有持久化状态仍写入 Topos 的 scene command；HTML DOM 与嵌入式 Web 画布只是可交互的投影，**不是工程真源，也不是绕过命令、撤销和迁移的后门**。

| 编辑对象 | 第一阶段（Freeform HTML） | 结构化场景 / 后续模板 | 明确边界 |
|---|---|---|---|
| 直接选择 | 仅选择拥有稳定 `data-topos-node-id` 的节点；未标记 DOM 作为整个 HTML layer 选择 | 可选择 Group / Element / Instance / Precomp，并支持层级面包屑 | 不根据任意网页像素反向猜测可编辑结构 |
| 内容与样式 | 改文本、asset slot、CSS variable、允许的 slot 属性和时长/延迟 | 完整 Inspector 可改排版、fill、stroke、effect、layout、motion | 首版不开放“任意 CSS 规则编辑器”或任意 DOM 重写 |
| 直接操作 | 拖动、缩放、旋转、对齐、吸附生成可撤销 patch/scene command | 同上，并支持 group、锚点、约束、motion path | 不让 UI 直接修改 iframe/WebView 内 DOM 后失去工程记录 |
| 响应式布局 | 仅由 AI 标注/导出的 editable slot 和 CSS token 驱动 | stack/grid、auto layout、约束、safe-area、横竖屏变体 | 不承诺把每个自由页面自动转换成结构化布局 |
| 复用 | 确认的 HTML 可提升为参数化 template | 组件/实例/variant/override 与品牌 token | 组件主定义更新必须可预览影响并可回退 |
| 交互状态 | 仅支持确定性的时间状态与动画轨道 | 可扩展为有限 state track / 条件变体 | 不把 Figma prototype 点击流或任意 JS 交互当作视频渲染逻辑 |

建议界面保持 Qt 工程壳、时间线、资产与导出控制；将 WebView/浏览器画布用于 HTML 预览和直接选择。这样能立刻使用浏览器排版能力，又不会把现有项目、渲染调度和离线导出重写成一个 Web 应用。

### 1.6 审查结论：先证明渲染闭环，再扩大模型与 UI

本计划原先的能力清单完整，但实施上有一个必须前置的风险：当前 `WebEnginePreviewBridge` 仅把已有 SVG 放入 `QWebEngineView` 作 GUI 预览，它不是 HTML/CSS 的离屏 RGBA renderer；而既有 `RenderWorker` 也只实例化 `SvgSceneRenderer`。因此不能直接在后台线程中“换成浏览器截图”并假定其能用于导出。

Qt 的 `QWebEngineView` 本质是 QWidget，WebEngine 的页面渲染/JavaScript 在独立 WebEngine Process 中执行；这很适合交互预览，却不足以证明固定帧 RGBA readback、无窗口运行、崩溃恢复和 export 吞吐已可用。[Qt WebEngine Overview](https://doc.qt.io/qt-6/qtwebengine-overview.html) [QWebEngineView](https://doc.qt.io/qt-6/qwebengineview.html)

因此，先执行 **W0-A 浏览器渲染可行性闸门**。闸门未通过前，以下内容均不得进入正式 schema 或对用户承诺：`FreeformHtmlMotionLayer`、HTML 直接编辑、HTML 最终导出、GPU/HTML 混合 renderer。若 Qt WebEngine 路径无法满足确定性/离屏 RGBA/资源隔离，允许选择独立、固定版本的 headless browser renderer；但不允许为赶进度退化为“播放中的 WebView 截图”。

---

## 2. 当前基线与主要缺口

### 2.1 已有基础（应复用，不推翻）

| 能力 | 现有位置 | 结论 |
|---|---|---|
| 版本化场景、元素、关键帧、素材引用、场景/Clip/Node 分离 | `core/model.py` | 正确的源资产边界，应继续演进 |
| 逐帧确定性求值 | `render/evaluator.py` | 已有 transform/opacity 基础；需升级到通用 property/motion graph |
| SVG 场景生成与 Qt 离屏 RGBA 适配 | `render/svg_renderer.py`、`render/frame_adapter.py` | 可保留为兼容/轻量后端，不应承担全部视觉上限 |
| Edit/Composite 通过共享 `scene_id` 取帧 | `host_adapters.py`、timeline preview/export | 正确，后续 renderer 变化不能破坏此合同 |
| Scene command stack、AI 受限命令、审批/版本/质量门 | `core/commands.py`、`ai/command_catalog.py`、`ai/quality_gate.py` | 已具备安全闭环；新增能力必须走同一命令、审批和测试链路 |
| 创意模板、品牌 token、交付 profile | `ai/creative_templates.py` 等 | 目前偏 metadata；需要可执行模板与视觉资产实现 |
| 共享 ModernGL effect catalog 与 Composite GPU 调度 | `features/shared/effects/`、`features/composite/` | 是高表现力动效的复用基础，不能在 Teaching 模块另起 GPU 管线 |

### 2.2 差距清单

| 领域 | 当前限制 | 用户可见后果 | 根因 |
|---|---|---|---|
| 时间 | 主要是平铺的关键帧轨 | “依次出现”“局部从 0 开始”“重叠转场”编排笨重 | 没有层级时间域与 beat/sequence 模型 |
| 动效 | 只有线性/基础 easing/bezier 轨 | 动效机械，AI 需要堆大量 keyframe | 缺少统一 `interpolate`、spring、stagger、reveal 等原语 |
| 场景树 | 元素是扁平列表，`group` 只是 z-index 约定 | 不可整体移动/遮罩/继承样式/复用实例 | 缺少真实 parent/group/precomp/instance |
| 样式与排版 | SVG 白名单有限，文字模型简单 | 字距、换行、富文本、阴影、渐变、布局质量不足 | 缺少 text/layout/style contract、字体度量和渲染后端能力 |
| 视觉效果 | SVG 后端不适合 blur、glow、粒子、位移、复杂混合 | 与网页动效或专业包装相比质感不足 | 场景元素无法声明并委派 GPU effect pass |
| 素材 | image/SVG/chart/draw 基础存在 | 缺少 Lottie/Rive/GIF/视频贴图/音频驱动等可控资产 | Asset Provider 与帧准确解码合同不足 |
| 模板 | registry 主要保存描述和默认参数 | 模板没有“可实例化、可替换、可迁移”的实现物 | template schema 与 scene compiler 未闭环 |
| HTML/CSS | 当前所谓 HTML preview 只是包裹 SVG，不是真正网页布局/动画 | LLM 擅长的网页排版、滤镜、卡片和 CSS 动画被压缩成简单 SVG | 没有 HTML layer schema、sanitizer、离屏浏览器按帧渲染与安全资源桥 |
| 编辑器 | 画布/检查器/局部时间线已有雏形 | 动效不能以节拍、层级、曲线、预设的方式高效编辑 | 缺少 motion UI view-model 与对命令的完整映射 |
| 性能 | 每帧可构建 SVG 并栅格化 | 高分辨率、长片段或复杂场景的播放与导出上限低 | 没有 retained render graph、增量失效、代理层级与 GPU cache |
| 质量 | 已有多帧/文本/安全区等质量门 | 无法检测节奏、过冲、转场边界、字体替换、GPU 降级 | 质量门未理解 motion/template/render capability |

### 2.3 本轮审查补充的关键遗漏

| 遗漏 | 风险 | 必须补上的设计决定 | 所属批次 |
|---|---|---|---|
| 浏览器后端仅有预览桥 | Qt WebEngine GUI 预览与无窗口 RGBA 导出不是同一能力，后台 `ThreadPoolExecutor` 也不能直接托管 QWidget | W0-A 用固定 HTML fixture 验证 load → seek → RGBA readback → cancel → crash/restart；明确选定 renderer process、capture API、字体/DPR 与生命周期所有者 | W0-A |
| HTML 的“确定性”定义不够精确 | 浏览器/GPU/字体版本不同很难承诺跨机器逐像素相同，CSS 动画还可能依赖墙钟 | 定义同一 `renderer_build + viewport + DPR + font set + asset hash` 下逐像素/严格 hash；跨构建只做 perceptual tolerance。所有 CSS/Web Animation 由 bridge 暂停、seek、两次 rAF 后 capture | W0-A / W2 |
| HTML 拖拽与 Flex/Grid 冲突 | 直接写 `transform` 会破坏自动布局或使 Inspector 与 DOM 状态分离 | HTML 节点显式声明 `edit_mode=content|absolute_transform|token_only`；只对 `absolute_transform` 注入 `data-topos-transform` wrapper，Flex/Grid 节点只能改 token/slot，不能假装任意拖拽都可用 | W2 / W3 |
| 安全策略只写 sanitizer | CSS 仍可能借资源、导航、弹窗、下载、存储或浏览器能力逃逸 | 叠加 CSP、deny-by-default URL request interceptor / scheme handler、禁下载/新窗口/DevTools/权限、ephemeral profile、存储清理、响应大小/解码像素限制和审计 | W0-A / W2 |
| 字体与素材可复现性不足 | 同一工程在另一机器换字、远端 URL 失效或素材版本变化会改变换行/画面 | Font registry 必须锁定 family/version/file hash/授权；所有 HTML 资源经 `topos-asset://` 解析；导出前 materialize/verify，工程包只引用 project asset id | W0-B / W2 |
| 坐标与色彩合同未冻结 | CSS px、scene coordinate、DPR、premult alpha、sRGB/linear 在 renderer 间易产生偏移、黑边和色差 | 定义 `CanvasSpace`（design px、origin、safe area）、`RenderSurface`（physical px/DPR）、transform origin、pixel snapping、straight/premult alpha 及 colorspace conversion 的唯一合同 | W0-B |
| 失败和降级策略不完整 | 无 WebEngine/renderer crash/不支持 filter 时可能静默空白或影响普通工程 | HTML layer 记录 capability；preview 显示可诊断 placeholder，export fail-closed；结构化/SVG 场景不受影响；恢复后仅重试未完成 render job | W0-A / W2 |
| 音频仅是 cue，未是时间系统的一部分 | 带旁白/音乐的动效难以在独立 scene、Edit 和导出保持同步 | 定义 scene-owned `AudioClip`（range、gain、fade、loop、ducking intent）与 `AudioRenderPlan`；实际混音仍委托 Edit/Export 服务，MG 不另建音频引擎 | W1 / W5 |
| 质量与性能缺少“测量环境” | 目标数字会因浏览器/GPU/字体和样本变化而无意义 | 每个 benchmark 固定 OS、CPU/GPU/driver、renderer build、字体集、profile、fixture 和统计方法；性能门在 W0-A 基线之后才冻结 | W0-A / W4 |

---

## 3. 目标架构

```text
MotionGraphicSceneDocument  (可保存、可迁移的真源)
 ├─ SceneGraph: Group / Element / Instance / Precomp / Mask
 ├─ TimeGraph: Beat / Sequence / Series / TimeRange / Transition
 ├─ MotionGraph: Track / interpolate / spring / expression(受限)
 ├─ StyleSystem: tokens / layout / typography / colorspace / blend
 ├─ AssetRefs: image / svg / video / audio / lottie / rive / font
 ├─ TemplateInstance: template id + version + JSON parameters
 └─ FreeformHtmlMotionLayer: sanitized HTML/CSS + editable slots/tokens
                    ↓  FrameContext(frame, fps, size, color, proxy tier)
                 SceneEvaluator（无 UI、确定性、可随机访问）
                    ↓  EvaluatedScene + RenderPlan
 ┌───────────────┼────────────────┬──────────────────┬──────────────────┐
 │ SVG fallback  │ HTML renderer  │ ModernGL scene   │ Composite bridge │
 │ lightweight   │ offscreen RGBA │ raster/effects   │ node output RGBA │
 └───────────────┴────────────────┴──────────────────┴──────────────────┘
                    ↓
         shared cache / preview / export / quality evidence

EditorState（仅 UI 选择、hover、工具、临时辅助线；不可替代 SceneDocument）
 ├─ CanvasInteraction: hit-test → node id → command / HTML patch
 ├─ LayerTree: hierarchy / lock / visibility / instance breadcrumb
 ├─ Inspector: content / token / layout / motion / effect
 └─ Timeline: beat / range / keyframe / curve / marker / audio cue
```

### 3.1 不变合同

1. `scene_id` 是 Clip 和 Composite Node 的稳定引用；实例不得复制场景 JSON。
2. `FrameContext` 的 `frame/fps/resolution/color/alpha/proxy_tier` 必须完整参与 cache key。
3. 任一元素、模板、effect、asset provider 声明 preview/export/backend 能力；不支持时显示诊断，绝不静默丢弃。
4. 所有写入经 immutable scene command、undo/redo、revision 与迁移；AI 不得绕过。
5. GPU 对象只在既有 render thread 中创建、使用、释放；UI 只发状态和信号。
6. 预览与导出共享 `SceneEvaluator` 和 `RenderPlan`；允许质量/分辨率不同，但不得改变时间、图层、色彩和 alpha 语义。
7. Freeform HTML 使用 Topos 自己提供的受信任 frame bridge；模型生成物不得包含脚本、可执行 URL、网络请求或访问本机路径的能力。
8. Web 画布的 hit-test、拖拽和文本修改必须解析为 schema-validated `SceneCommand` 或 versioned `HtmlPatch`；每次修改均支持 dry-run、undo/redo、保存/重开和 preview/export 一致性验证。

### 3.2 W0 必须冻结的最小接口

以下接口先以纯数据/Protocol 实现，禁止在 W0 直接改大段 UI、SVG 或 ModernGL 代码。字段可扩展但未知字段必须 round-trip 保留。

```text
FrameContext
  frame: int                       # scene-local, half-open [0, duration)
  fps_num/fps_den: int             # 禁止仅存 float
  canvas: CanvasSpace               # design width/height, safe-area, origin
  surface: RenderSurface            # output px, DPR, colorspace, alpha mode, proxy tier
  revision/render_profile_hash: str

RenderPlan
  ordered RenderPass[]              # source -> transform/layout -> effect -> composite
  required_capabilities[]           # html_renderer / gpu_effect / font / asset provider ...
  cache_key_inputs[]                # scene + assets + fonts + renderer + surface + profile
  fallback/fail_policy              # preview placeholder | export block

SceneRenderer Protocol
  prepare(scene, context) -> RendererSession
  render(session, frame_context, cancel_token) -> RgbaFrame + RenderEvidence
  close(session)                    # idempotent; owns all renderer resources

HtmlPatch
  base_revision + target_node_id + operation + typed value + precondition hash
  operation ∈ replace_text | set_asset | set_token | set_slot_attr |
              transform_node | set_animation_timing
```

`RenderEvidence` 至少带 renderer id/build、HTML sanitizer version、font/asset resolution、capability fallback、render duration、frame hash 与 diagnostics。它既是 cache 解释依据，也是 preview/export 不一致时的定位证据。

### 3.3 HTML renderer 的实现边界

HTML renderer 采用单独的 `HtmlRendererService`，而不是向 `RenderWorker` 的 SVG 循环插入 WebEngine 对象。服务的 worker/process 拥有 browser session；Qt UI 仅通过异步 request/response 获得 preview frame，GPU/CPU frame 再进入现有合成路径。若采用 Qt WebEngine，必须由 W0-A 实验明确其对象线程、窗口/离屏策略和 RGBA capture 链路；若采用独立 browser，则其二进制版本、启动参数和 sandbox profile 都是 renderer build 的一部分。

每一次 HTML render 固定执行：

1. 创建或从隔离池借用 page/profile，安装 CSP、request interceptor 与只读 `topos-asset://` handler；
2. 验证 sanitized document/source hash，绑定 assets/fonts，设置 design viewport 和 DPR；
3. 注入受信任 bridge，暂停动画，设置 `frame/fps`、`--topos-frame/time/progress`，seek Web Animations/CSS animations；
4. 等待 layout/font readiness 与两次 animation frame，再做 RGBA capture；
5. 记录 evidence，归还/销毁 page；取消、超时、renderer crash 和资源超限均产生结构化 failure，而非返回旧帧。

不把模型生成 HTML 中的 JavaScript 当成 bridge 的一部分；bridge 是 Topos 打包、版本化和测试的受信任脚本。

---

## 4. 系统能力优化表

### 4.1 SceneGraph、布局与排版

| ID | 优先级 | 缺口 | 实施内容 | 核心位置 | 验收标准 |
|---|---:|---|---|---|---|
| SG-01 | P0 | 无真实层级 | 引入 `GroupElement`、parent id、局部 transform、可见性/锁定/solo；禁止循环与跨 scene 引用 | `core/model.py`、migration、validator、commands | 移动父组可稳定影响所有子元素；撤销/保存/重开不漂移 |
| SG-02 | P0 | 不能复用复杂对象 | 引入 `Precomp` 与 `SceneInstance`：公开参数、局部 in/out、override、版本依赖 | model/template registry/Composite bridge | 一个标题包装可在 10 个 Clip 中实例化，单实例覆盖不改模板 |
| SG-03 | P0 | 没有布局约束 | 增加 anchors、margin、align/distribute、safe-area、stack/grid、content-fit；布局先于动画求值 | layout core、scene inspector、design validator | 16:9 场景切 9:16 后关键文字仍在安全区且不重叠 |
| SG-04 | P1 | 遮罩与裁切弱 | 增加 vector/alpha/luma mask、track matte、clipping path、feather/invert 与明确 alpha 规则 | model/render plan/Composite | 卡片 reveal、圆形 wipe、文本遮罩在 preview/export 一致 |
| SG-05 | P1 | 排版模型简单 | 富文本 span、行高、字距、字重、对齐、自动换行、文本路径、ellipsis；接入确定性字体度量 | text layout service、asset font registry | CJK/英文混排换行稳定；缺字/字体替换可诊断 |
| SG-06 | P1 | style 缺少组合语义 | fill/stroke/gradient/shadow/opacity/blend/matte 为版本化 style schema；色彩空间与 premult 状态明确 | style schema、render plan | 同一 style 在 SVG、GPU、Composite 中透明边缘无黑边 |
| SG-07 | P2 | 需要更丰富图形 | 扩展 shape/path/boolean/rounded rect/arrow/callout/connector；连线可绑定节点锚点 | graphics primitives、editor | 流程图节点移动后连接线自动重算且可动画 |
| SG-08 | P0 | 坐标/DPR/alpha 语义隐含 | 建立 `CanvasSpace`、`RenderSurface`、transform origin、pixel snapping、sRGB/linear 与 straight/premult alpha 合同；所有 renderer 只接受该合同 | core contracts/render plan/host adapters | 结构化与 HTML 元素在 1x/2x DPR 和透明合成下无半像素漂移、黑边或色彩双转换 |

### 4.2 时间与动效原语（最高投入回报）

| ID | 优先级 | 缺口 | 实施内容 | 核心位置 | 验收标准 |
|---|---:|---|---|---|---|
| MT-01 | P0 | 关键帧是唯一时间模型 | 定义 `TimeRange(start, duration, trim_in, freeze, enabled)`、局部 frame、嵌套 time transform；所有元素/组/实例可拥有 range | model/evaluator/commands | 任意嵌套对象的局部帧计算可单测，首/尾帧无 off-by-one |
| MT-02 | P0 | 无 `Sequence`/`Series` | 支持并行 `SequenceGroup`、顺序 `SeriesGroup`、negative overlap、gap、trim、freeze、可见区间 | time graph、scene timeline | 4 个步骤以固定节拍顺序出现，改前一段时长后后续自动重排 |
| MT-03 | P0 | 动效公式分散 | 建立纯函数 `interpolate(value, input_range, output_range, easing, extrapolate_left/right)`；支持数字、颜色、向量，不支持时显式失败 | `motion/interpolate.py`、AI catalog | 任何帧可复算；clamp/extend/identity 行为都有 golden tests |
| MT-04 | P0 | 无物理动效 | 建立确定性 `spring(frame, fps, mass, damping, stiffness, overshoot_clamp, delay, duration)` 与 `measure_spring()` | `motion/spring.py` | 相同输入跨 preview/export 得到相同值；可精确压缩到指定时长 |
| MT-05 | P0 | 缺少常用动效组合 | 内置 `stagger`、`delay`、`loop`、`ping_pong`、`reveal`、`typewriter`、`draw_path`、`count_up`、`follow_path`、`wiggle(seed)` | motion library/template compiler | AI 用 1 条高层命令生成依次弹出，不能靠 40 个手工 keyframe |
| MT-06 | P1 | 曲线表达有限 | 支持 cubic-bezier、steps、back、elastic、bounce、custom sampled curve；统一溢出与速度限制 | shared keyframe/motion | 编辑器曲线与最终求值完全一致，非法曲线被 validator 拒绝 |
| MT-07 | P1 | 没有属性绑定 | `MotionBinding` 支持一个 driver 同时驱动 position/scale/opacity/color，并用 mapping/offset 变换 | evaluator/command catalog | 一条 spring 可以同时驱动卡片 scale、y 和 opacity |
| MT-08 | P1 | 无节拍语义 | `Beat`、marker、narration cue、BPM/timecode 对齐；模板对 beat 暴露可编辑时长预算 | model/AI brief/audio plan | 旁白改长 1 秒时可定位受影响 beat 并提出重排方案 |
| MT-09 | P2 | 无受控表达式 | 只允许无副作用、纯函数、白名单 AST（math、frame、seed、marker）；禁止文件/网络/反射 | expression evaluator/validator | 表达式可序列化、可缓存、可限制复杂度，不能执行 Python |
| MT-10 | P2 | 无 motion blur | 在 GPU renderer 提供速度驱动的可控 motion blur，代理预览可降级 | RenderPlan/Effect Catalog | 可声明预览降级，导出不静默失效 |
| MT-11 | P1 | audio cue 不可参与 scene 时间语义 | 引入 scene-owned `AudioClip`（range/gain/fade/loop/ducking intent）及 `AudioRenderPlan`；混音委托既有 Edit/Export，MG 不重复造音频后端 | model/evaluator/host adapter/export bridge | 独立编辑器、Clip、export 的音频起止与 beat 对齐；缺音频/不支持混音均有诊断 |

### 4.3 模板、动效库与 AI 命令

| ID | 优先级 | 缺口 | 实施内容 | 关联位置 | 验收标准 |
|---|---:|---|---|---|---|
| TM-01 | P0 | 模板只有 metadata | 定义 `ExecutableTemplate`：schema、scene compiler、默认 beat、style/motion token、asset slots、capability、migration | `core/template_registry.py`、`ai/creative_templates.py` | 给定 JSON 参数可以确定性生成 scene，模板升级可迁移旧实例 |
| TM-02 | P0 | AI 命令偏低层 | 新增高层命令：`apply_template`、`set_beat_timing`、`apply_motion_preset`、`stagger_elements`、`set_transition`、`replace_asset_slot`、`set_brand_theme` | `ai/command_catalog.py` | 每条命令可 dry-run、生成 SceneDiff、进 command stack 且可撤销 |
| TM-03 | P0 | 无统一动效预设 | `MotionPresetRegistry`：fade/slide/pop/scale/overshoot/draw/reveal/word-highlight；预设是参数化 recipe，非不透明 keyframe blob | new `motion/presets.py` | 预设版本化；AI 指定“柔和弹入”只产生受控的标准参数 |
| TM-04 | P1 | 模板数量和风格覆盖不足 | 首批 15 个可执行模板：title、lower third、section divider、steps、process flow、data chart、comparison、quote、formula proof、timeline、checklist、tutorial callout、social card、product feature、CTA | `templates/` + tests/assets | 每个模板有横/竖屏、中文、缺素材、品牌覆盖和 3 帧质量样例 |
| TM-05 | P1 | 缺少设计 token 约束 | 定义 palette、type scale、spacing、radius、shadow、motion、safe-area、logo policy；品牌 token 有优先级、fallback、冲突诊断 | creative templates/design validator | 修改品牌主色会一致更新所有引用，不误改手动锁定的元素 |
| TM-06 | P1 | AI 不理解可用能力 | 从 registry 自动生成 command schema、template schema、effect capability、示例与错误码；prompt 不手写重复列表 | AI context provider | 下线 capability 后 AI 不再生成该命令，且旧场景有迁移/诊断 |
| TM-07 | P2 | 缺少可复用的视觉素材包 | 引入受审计的 `MotionAssetPack`（icon/shape/noise/gradient/Lottie/Rive）及许可证、版本、hash、降级策略 | asset provenance/template registry | 素材包升级不破坏旧项目；无许可证素材阻断最终交付 |

### 4.4 渲染、效果与媒体能力

| ID | 优先级 | 缺口 | 实施内容 | 关联位置 | 验收标准 |
|---|---:|---|---|---|---|
| RD-01 | P0 | renderer 只输出 SVG | 引入 renderer-neutral `RenderPlan`：draw items、clip/mask、blend、effect chain、asset decode、color/alpha；SVG 先实现可支持子集 | contracts/evaluator/render | 任何后端对同一 plan 都可报告支持/降级，不各自重算场景语义 |
| RD-02 | P0 | 高质量效果无法复用 | 为 scene layer 接入共享 `EffectSpec` / `EffectPassRunner`，先支持 blur、glow、shadow、color matrix、displacement、gradient/noise | teaching host adapter + shared effects | effect 参数可动画；preview/export capability 与错误状态可见 |
| RD-03 | P0 | 没有 retained GPU scene renderer | 在既有 render worker/scheduler 中增加 `ModernGLSceneRenderer`，缓存 geometry、glyph atlas、textures/FBO；严格走 render thread 和 deferred release | render worker、Composite scheduler | 修改一个元素只使其 bbox/effect chain 失效；UI 线程无 GL 调用 |
| RD-04 | P1 | SVG 后端能力不透明 | 实现 SVG fallback capability matrix：哪些 element/style/effect 精确、近似、不可用；不可用内容显示 placeholder + diagnostic | SVG renderer/capability | 代理降级有明确 badge，导出不能悄悄少滤镜 |
| RD-05 | P1 | 图像素材类型有限 | Asset Provider 支持 raster、SVG、animated GIF、image sequence、视频帧、音频 waveform；指定帧准确 seek、色彩/alpha/版权 metadata | asset resolver/shared media | 预览/导出引用同一 source frame，无绝对路径进入 `.topos` |
| RD-06 | P1 | 字体不可控 | Font registry：嵌入/项目字体、fallback 链、glyph coverage、字体 hash、license；文字 shaping/layout 统一 | font service/text renderer | 缺失 CJK glyph 或字体替换成为阻断/警告，不能悄悄变版 |
| RD-07 | P1 | Lottie/Rive 不能受控使用 | 做 provider adapter：输入帧、资源 sandbox、fallback still/sequence、capability 与 license；先只读播放，后续暴露安全参数 | assets/render providers | 未安装 provider 不会让项目崩溃；导出有可复现 fallback |
| RD-08 | P2 | 3D/粒子需要产品化 | 优先复用 Composite 3D scene / camera / light / geometry node；MotionGraphic 只持有受限 `Precomp` 引用和公开参数 | Composite facade/node provider | 3D 标题可在 MG 内使用且只经 Composite render lane 执行 |
| RD-09 | P2 | 缺少高级混合 | 逐步增加 blend modes、matte、displacement、turbulence、particle emitter、light streak、film grain；每项有 GPU cost/capability | Effect Catalog | 4K 性能预算、代理降级、导出质量门与资源释放均有测试 |
| RD-10 | P3 | 颜色与 HDR 管理未显式化 | 继承项目 color pipeline，定义 scene working space、tone map、premult alpha、SDR/HDR delivery behavior | color/composite bridge | HDR scene 经过 MG→Composite→Export 不丢高光，SDR 交付有显式 transform |
| RD-11 | P0 | renderer 生命周期不统一 | 定义 renderer session/pool、page/process ownership、frame capture、timeout/cancel/crash recovery、memory/asset budgets 和 `RenderEvidence`；SVG/HTML/GPU 都实现同一 Protocol | render service/scheduler/cache | 连续 render、取消、进程 crash、工程关闭和重开均无旧帧、句柄泄漏或 UI 阻塞 |

### 4.5 Freeform HTML Motion（先释放 AI 视觉能力）

| ID | 优先级 | 缺口 | 实施内容 | 关联位置 | 验收标准 |
|---|---:|---|---|---|---|
| FH-01 | P0 | 缺少网页内容层 | 定义 `FreeformHtmlMotionLayer`：sanitized `html/css`、layer id、duration/fps、asset refs、editable slots/tokens、renderer/sanitizer version、source hash | `core/model.py`、migration、validator | `.topos` 可保存/重开/迁移；HTML layer 仍由 `scene_id` 引用而非独立临时文件 |
| FH-02 | P0 | 未定义安全边界 | 建立 allowlist sanitizer：允许安全标签、CSS 属性、`@keyframes`、内联 SVG，以及解析后的 `url(topos-asset://...)`；拒绝 script、事件属性、iframe、form、`@import`、远端 URL、绝对路径和超限 data URI | new `render/html_sanitizer.py` | 恶意 HTML/CSS fixture 不可执行/联网/读取本地资源，且返回精确诊断 |
| FH-03 | P0 | CSS 动画不是帧确定性 | `HtmlFrameBridge` 由 Topos 注入：设置 viewport/scale，暂停 animation，按 `frame/fps` seek Web Animation/CSS animation；暴露只读 `--topos-frame/time/progress` | new `render/html_frame_bridge.py` | 同一 HTML 在任意帧顺序渲染得到相同 RGBA；首尾无依赖墙钟的跳变 |
| FH-04 | P0 | 无离屏浏览器后端 | 在独立 render worker 中实现 `HtmlSceneRenderer`：受限离屏 browser page、超时/取消、RGBA readback、进度与资源清理；绝不运行在 UI 线程 | render worker/scheduler | 可渲 1080p HTML CSS 动画到 RGBA，取消后 browser/page/临时资源均释放 |
| FH-05 | P0 | 资源不可控 | `topos-asset://<asset_id>` 受控资源协议，映射项目内相对素材；字体/图像/SVG 走 Asset/Font registry，不允许 HTML 指定路径或 URL | asset resolver/font registry | 离线工程可复现；未解析资产/字体在 preview 前报告而非网络下载 |
| FH-06 | P1 | 只有全代码编辑或不可编辑两端 | HTML inspector 的最小编辑面：替换文本节点、asset placeholder、CSS custom property、`data-topos-editable` slot、animation duration/delay；修改生成 patch/revision | UI inspector/commands | 不理解 DOM 的用户仍能改标题、颜色、图片和节奏，并可 undo/redo |
| FH-07 | P1 | 自由 CSS 可能过载 | CSS complexity budget：DOM 深度/节点数、stylesheet 长度、keyframe 数、filter/shadow 层、SVG path 数据、render time；超限要求 AI 简化 | validator/scheduler/quality gate | 超限页面不会冻结 UI 或无限渲染；反馈包含可操作的压缩建议 |
| FH-08 | P1 | 无 preview/export 证据 | 记录 browser engine/version、viewport、DPR、font/asset/source hash 与 sample frame artifacts；HTML renderer 同样参与 cache key 和 consistency test | cache/preview/export | 同一 profile 下 preview/export 抽样帧一致；版本变化明确触发 cache miss |
| FH-09 | P2 | 自由设计难沉淀 | `promote_html_to_template`：把用户确认的结果冻结为受审计模板，抽取文本/asset/CSS variable/motion slots；不强行自动转原生元素 | template tooling | 作品可从自由页面升级为参数模板，保留原始 layer 作为可回退版本 |
| FH-10 | P0 | DOM 直接编辑会破坏确定性与安全边界 | 定义 `HtmlPatch`（replace_text / set_asset / set_token / set_slot_attr / transform_node / set_animation_timing）及 precondition/source revision；禁止通用 innerHTML/stylesheet 字符串写入 | core commands/HTML patch validator/undo | patch 只能作用于已声明 node/slot/token；冲突、过期 revision、非法属性可诊断且不会部分写入 |
| FH-11 | P0 | browser 后端方案未经证实 | W0-A standalone spike：固定 HTML fixture 在候选 renderer 上完成 no-network load、font lock、seek、RGBA readback、乱序帧、cancel、crash/restart；产出 ADR 与 benchmark baseline | new `render/html_renderer_spike/`、ADR、test fixture | 不满足任一项则禁止写入正式 HTML layer schema；通过后固定 renderer build 与 support matrix |
| FH-12 | P0 | sanitizer 外缺少运行时隔离 | Browser sandbox：CSP、ephemeral profile、URL request denylist、`topos-asset://` scheme handler、禁导航/新窗/下载/权限/API、resource size/decode budget | HTML renderer service/security tests | HTML/CSS attack corpus 不能联网、读文件、保留 cookie/storage、下载、弹窗或逃逸到非允许 scheme |
| FH-13 | P1 | 所有节点拖拽会破坏响应式 HTML | HTML authoring contract：`data-topos-node-id` + `data-topos-edit-mode`；absolute node 由 transform wrapper 编辑，content/token node 只经 slot/token patch 编辑 | AI schema/sanitizer/bridge/inspector | Flex/Grid 卡片可改内容而不失布局；仅声明 absolute 的对象可在画布移动/缩放/旋转 |

### 4.6 编辑、预览、缓存与交付

| ID | 优先级 | 缺口 | 实施内容 | 关联位置 | 验收标准 |
|---|---:|---|---|---|---|
| UX-01 | P0 | 时间线只编辑低层 keyframe | Scene timeline 显示 group、beat、range、transition、audio cue、marker；支持拖拽、snap、ripple、roll、stretch | `ui/scene_timeline.py` + command facade | 拖动 beat 后局部时间、关键帧和预览同步且可撤销 |
| UX-02 | P0 | 不易发现可用动效 | Inspector 提供 motion preset、spring playground、curve editor、stagger controls、模板参数表单 | inspector/controller | 用户不写代码也能完成“依次弹出、转场、改弹性” |
| UX-03 | P1 | 画布交互受限 | selection、parent breadcrumbs、anchor/constraint handles、safe area、mask path、motion path、onion skin | scene view/editor | 复杂 group/instance 可直接选择和编辑，锁定层不被误改 |
| UX-04 | P1 | 无代理与局部渲染策略 | 定义 draft/interactive/preview/final render profiles；dirty range + dirty bbox + dependency graph + memory budget | cache/render scheduler | 修改文字不重渲无关 beat；4K 项目可快速获得低清反馈 |
| UX-05 | P1 | 缓存缺少可解释性 | cache key = scene/template/asset/font/effect/render-profile/renderer-version hash；提供命中/失效原因与一键清理 | scene cache/artifacts | 资源或字体变更必然失效；无效缓存不污染工程 payload |
| UX-06 | P1 | 时间线/Composite 往返不清晰 | 双击 Clip 打开 scene；Composite Node 回链同一 scene；面板显示实例 override、缓存与来源 | facade/host adapters/UI | 同一 scene 的 Clip 和 Node 修改后同时更新、不会复制 JSON |
| UX-07 | P2 | 导出能力未分层 | 支持 MP4/WebM、透明 ProRes/PNG sequence、可选 flatten；输出 manifest、hash、缩略图、frame probes | export service/timeline exporter | 输出 profile 不支持 alpha/effect 时提前阻断并给替代方案 |
| UX-08 | P0 | 缺少 Figma 式基础画布工作流 | 建立工具状态：select/pan/text/shape、hover、框选、多选、缩放/平移、复制/删除、z-order、锁定/隐藏、对齐/分布、像素/对象吸附、参考线与安全区；UI 状态和工程状态分离 | scene editor/view-model/command facade | 所有画布编辑落为可读命令，Esc/undo/redo/保存重开后 selection 不影响工程、工程变更不丢失 |
| UX-09 | P0 | HTML 画布无法可靠选中和修改 | AI HTML 输出规范要求 editable 节点含唯一 `data-topos-node-id`；bridge 返回安全 hit-test 路径、节点 bbox 和可编辑 capability；Canvas 将选择映射到 `HtmlPatch` 而非直接写 DOM | HTML schema/frame bridge/inspector/commands | 点选标题即可在图层与 Inspector 定位同一 slot；未标记 DOM 仅能选择父 HTML layer，不发生错误拆解 |
| UX-10 | P0 | 图层结构和当前选择不透明 | 实现可搜索 Layer Tree、层级展开、parent breadcrumb、lock/hide/solo、拖放重排/嵌套、instance 标识与只读来源提示；HTML slot 显示为 layer 的受控子项 | editor UI/view-model/commands | 通过画布、图层树或 timeline 选择同一对象，状态双向同步；锁定项不能被误拖或被 AI 命令改写 |
| UX-11 | P1 | 属性修改仍依赖代码或散乱 CSS | Inspector 按 Content / Style / Layout / Motion / Effect 分组；Freeform 仅展示 schema 声明的 text、asset、CSS token、slot 与 animation 参数，结构化元素展示强类型属性 | inspector schema/HTML patch validator | 修改品牌色、字重、圆角、阴影、入场时刻或 spring 参数均可预览、撤销和通过 validator |
| UX-12 | P1 | 没有可视化布局系统 | 引入 Frame/Auto Layout：horizontal/vertical/grid、padding/gap、hug/fill/fixed/min/max、safe-area；再加入 left/right/top/bottom/center/scale constraint | layout core/inspector/scene commands | 标题卡或步骤列表换文案、切 16:9/9:16 时按合同重排，且同帧 preview/export 一致 |
| UX-13 | P1 | 组件复用和风格控制弱 | 引入 Component 主定义、Instance、override、variant、可编辑 slot 与 design/motion token；编辑主定义前展示受影响实例和可回退 diff | component registry/model/migration/UI | 修改标题卡主组件可预览同步多个实例；单实例改文案/色彩不污染主定义 |
| UX-14 | P1 | 直接操作与动画关系不清 | 在当前帧拖拽/缩放时提供“改静态值 / 在当前帧写 keyframe / 绑定已有 motion track”选择；显示关键帧菱形、motion path、curve/velocity 与受控 auto-key | canvas/timeline/command facade | 画布调整不会意外毁掉动画；选择 auto-key 后只影响当前 property/当前帧，且命令可逆 |
| UX-15 | P2 | 跨画幅设计和状态变体缺失 | 提供 16:9/9:16/1:1 画板、responsive variant、token mode 与有限确定性 state track；可比较布局差异并复用同一 scene/组件 | scene model/layout/variant UI | 同一品牌组件针对横竖屏可显式 override；不引入任意点击原型或不可导出的运行时状态 |

### 4.7 AI、质量、兼容与测试

| ID | 优先级 | 缺口 | 实施内容 | 关联位置 | 验收标准 |
|---|---:|---|---|---|---|
| AQ-01 | P0 | AI 缺少 motion design plan | Proposal 增加 beat sheet、motion preset、层级结构、transition、素材/字体、风险和 render cost | module/proposal cards | 用户确认前能知道每一段何时出现、何时消失、会影响哪些元素 |
| AQ-02 | P0 | 高层命令无法安全落地 | 每个 motion/template command 要有 JSON schema、静态 validator、dry-run SceneDiff、复杂度限制与原子 transaction | command catalog/core commands | 非法 frame/parent/cycle/effect/asset 在写入前被拒绝，绝不半写入 |
| AQ-03 | P0 | 质量门不检查动效本身 | 增加 motion checks：首/中/尾/marker 帧、transition overlap、spring 未收敛、overshoot 超界、strobing、font fallback、effect capability、cache mismatch | quality gate/design validator | 15 秒片段首帧可渲之外，还能发现空镜、闪烁和过冲文字 |
| AQ-04 | P1 | 没有视觉回归证据 | golden scene fixture：导出关键帧 PNG + perceptual diff；backend capability matrix；文字/alpha/color metrics | tests/fixtures | renderer/effect 改动能检出视觉回归，而不只测 JSON |
| AQ-05 | P1 | preview/export 一致性不可量化 | 针对抽样帧比较 dimensions、alpha、color metadata、hash/perceptual tolerance；差异必须记录 render profile | preview/export tests | 同一 scene revision 的两条路径通过一致性阈值，否则交付失败 |
| AQ-06 | P1 | schema 演进风险 | `scene_schema_version`、template/effect/provider versions、migrations、unknown-field preservation、deprecation policy | model/migration/project package | 升级后旧 MotionGraphicClip 可打开、修改、导出，缺 provider 有诊断和 fallback |
| AQ-07 | P1 | 性能不可控 | 设定 1080p/4K 的交互、首帧、局部更新、导出吞吐 SLO；benchmark 包含文本、图表、mask、glow、视频、Lottie | benchmark/SLO | 每次渲染变更有基线；超过预算不能静默合入 |
| AQ-08 | P2 | 没有可测的创意质量 | 为教学解释、数据图表、品牌标题、社媒卡片建立 rubric：可读性、节奏、层级、品牌、字幕/音频、重复性 | eval rubric/golden projects | 同一 prompt 多次生成至少可在结构与质量阈值下稳定交付 |
| AQ-09 | P0 | 自由 HTML 生成缺少受控协议 | AI 系统提示和 schema 明确只输出 HTML/CSS，不输出 JS/URL；生成后 sanitizer → resource bind → static analysis → low-res preview → explicit approval | module/command catalog/preview | 生成失败返回可修正原因；未经批准的 HTML 不写工程、不插时间线 |
| AQ-10 | P1 | HTML 质量门缺失 | 质量门增加 unsafe content、asset/font resolve、CSS budget、frame determinism、viewport overflow、flash/flicker、browser crash/timeout 检查 | quality gate | “漂亮但不可交付”的页面在确认前或导出前被明确阻断 |
| AQ-11 | P1 | 直接编辑的正确性无证据 | 新增 editor interaction fixtures：hit-test → node id、drag/resize/rotate、multiselect、layer reorder、auto-key、HTML patch conflict、undo/redo、保存重开与 preview/export 对照 | UI integration tests/command tests | 交互不产生脱离 SceneDocument 的 DOM 状态，且同一操作在 reload 后保持视觉与时间语义 |

---

## 5. 分阶段实施计划

### 5.1 实施波次

| 波次 | 目标 | 包含项 | 主要交付 | 退出门 |
|---|---|---|---|---|
| W0-A：浏览器可行性闸门 | 证明 HTML 能安全、确定性地输出 RGBA；不改项目格式 | FH-11、FH-12、RD-11、AQ-09 的 attack fixture | 独立 renderer spike、ADR、support matrix、性能/内存基线、固定 browser/font build | 通过 seek/readback/cancel/crash/no-network/font-lock 全部测试；否则只保留 SVG/原生路线，不开始 HTML layer |
| W0-B：冻结核心合同 | 不再继续向扁平 SVG 直接加特例，建立可迁移语义骨架 | RD-01、MT-01、SG-01、SG-08、AQ-06 | `FrameContext`、`CanvasSpace`、`RenderSurface`、`RenderPlan`、renderer Protocol、scene/time schema v2、迁移/capability model | 全部旧场景 round-trip；preview/export 抽样帧无阻断回归；未知字段保留；不引入任何 HTML 内容 |
| W1：动效语言的原生垂直切片 | 先让结构化 Scene 获得可测的 Remotion 式时间与动效 | MT-02～MT-05、TM-02～TM-03、UX-01～UX-02、AQ-02 | sequence/series、interpolate、spring、stagger、motion preset、AI 高层命令、S1 | “四卡依次弹出”不靠堆 keyframe，能修改时长/回弹并完整 undo/redo；所有边界帧 golden 通过 |
| W2：Freeform HTML 可交付闭环 | 在 W0-A 已证明的后端上释放 AI 页面设计能力 | FH-01～FH-05、FH-10～FH-12、UX-08～UX-10、AQ-09、RD-11 的 HTML consumer、S0 | HTML schema/sanitizer/frame bridge/renderer service、asset/font bridge、最小图层/Inspector/HtmlPatch | 3 个自由 HTML 场景在固定 profile 乱序帧稳定输出；可编辑标题/asset/token；无 JS/网络/路径越权；HTML export 与 preview 同帧一致 |
| W3：布局、Figma 式编辑与模板沉淀 | 只从已交付自由作品提炼可复用结构，不抢跑模板数量 | TM-01、TM-04、TM-05、SG-03、SG-05、FH-06/FH-09/FH-13、UX-11～UX-15、AQ-01/AQ-11 | Inspector、Auto Layout、组件/实例、HTML slot promotion、首批少量高质量 template | 横竖屏/CJK/品牌 fixture 都通过；Flex/Grid 内容编辑不坏布局；直接操作不破坏关键帧；模板升级可回退 |
| W4：原生加速与一致性 | 将高频、高成本效果从 SVG/HTML 有选择地提升到共享 GPU 路径 | RD-02～RD-04、SG-06、UX-04～UX-05、AQ-04/05/07 | ModernGL scene renderer、effect chain、代理/缓存、视觉/性能回归基线 | 每项效果有 capability/fallback；同一 profile preview/export 通过一致性门；资源释放、取消与缓存失效有压力测试 |
| W5：素材、音频与合成 | 补齐真正可编排的专业输入和宿主互操作 | RD-05～RD-08、SG-02/04/07、MT-06～MT-08/11、UX-03/06/07、FH-07/08、AQ-10 | 字体/视频/Lottie/Rive provider、AudioRenderPlan、precomp/mask/transition、Composite bridge、HTML 质量预算 | MG 可携透明 RGBA/同步音频进入 Edit/Composite；缺素材/字体/许可/效果均有可操作报告 |
| W6：专业能力 | 只在基础链路稳定、量化收益明确后增加高成本效果 | MT-09/10、RD-09/10、AQ-08 | 受控表达式、motion blur、粒子、HDR、创意评测集 | 每项能力独立立项，有 capability、性能预算、迁移、失败/取消/回收、golden 与发布门 |

### 5.1.1 每个批次的实施步骤、代码边界与验证

| 批次 | 必须按顺序完成 | 首要代码边界 | 代码完成后的验证（全部通过才进入下一批） |
|---|---|---|---|
| W0-A | 1) 写 3 个静态/动画 HTML fixture 与 20+ 恶意 fixture；2) 实现最小候选 renderer；3) 接 no-network asset/font handler；4) 执行 seek/readback/cancel/crash 实验；5) 写 ADR 固定方案或停止 HTML 路线 | 新 `render/html_renderer_spike/`；不得改 `core/model.py`、时间线或 UI | 同一帧重复 20 次 hash 相同；乱序 0/中/尾/关键帧一致；断网/URL/file/data 攻击均失败；cancel 后无 page/process 残留；进程崩溃后下一 Job 可成功；记录 baseline 而非先写死性能承诺 |
| W0-B | 1) 定义纯数据合同；2) 新旧 JSON 双读/单写迁移；3) 让现有 SVG renderer 适配 `RenderPlan`；4) 将 host preview/export 都经同一 evaluator 调用 | `core/contracts.py`、`core/model.py`、`core/migration.py`、`render/*` facade；不改 UI 行为 | v1 fixture semantic round-trip；旧 Clip/Node 重开；duration 的首/尾/越界帧测试；1x/2x、alpha/color fixture；preview/export 抽样帧和旧基线无阻断差异 |
| W1 | 1) `TimeRange` 局部 frame；2) series/sequence；3) interpolate；4) spring/measure；5) preset/command；6) 只为 S1 增加必要 timeline/Inspector | 新 `motion/` 纯函数层、evaluator、command facade、`ui/scene_timeline.py` | 公式 golden + property/fuzz（NaN、反向、delay、边界）；S1 的 SceneDiff/undo/redo；改前段时长后后段重排；相同输入多进程值相同；不允许 AI 直接写低层 track |
| W2 | 1) 根据 W0-A ADR 定义 HTML schema；2) sanitizer + runtime sandbox；3) `topos-asset://`/font registry；4) frame bridge；5) renderer service；6) S0 preview/export；7) 再接最小 Canvas/LayerTree/Inspector patch | `render/html_*`、asset/font registry、`core/commands.py`、最小 editor view-model；不重写完整编辑器 | HTML attack corpus；font/asset missing；固定 renderer build 的 frame hash；source/patch revision conflict；HTML scene 保存/重开；preview/export consistency；timeout/cancel/crash；S0 端到端插入 Graphics 轨与一次 Undo |
| W3 | 1) 强类型结构化 Auto Layout；2) HTML edit-mode/wrapper；3) Inspector；4) auto-key/curve 选择；5) component/instance；6) 从 S0 成品提升少量模板 | layout core、template compiler、inspector、HTML bridge；模板只通过 compiler 产 scene | 内容变更/换画幅布局 fixture；Flex/Grid 不被 transform 破坏；multi-select/lock/reorder；component override/migration；模板参数 schema fuzz；画布/图层/时间线三向同步；CJK/字体替换/安全区验证 |
| W4 | 1) 选 3 个有测量收益的高频效果；2) 每项先实现 RenderPlan capability/fallback；3) GPU pass；4) proxy/cache；5) visual regression；6) 逐项灰度 | shared Effect Catalog、Composite facade、render scheduler；禁止 Teaching 私有 shader registry | GPU/CPU/SVG/HTML capability matrix；alpha/color/transparent edge diff；4K 冷/热缓存和 dirty range benchmark；连续取消/显存资源检查；GPU 不可用时 preview placeholder/export block 符合策略 |
| W5/W6 | 以一个 provider/一个 transition/一个专业效果为一个独立小批次；每次先 schema/capability，再 renderer，再 UI，最后 AI command | Asset Provider、AudioRenderPlan、Composite bridge、专业 effect | provider 缺失/版本不兼容/许可不明；音频与帧边界；MG→Composite→Export；长项目压力、工程重开、manifest/provenance；人工创意 rubric 与性能 SLO 同时达标 |

### 5.1.2 退出门证据（实施状态，取代"规划中"）

每个波次按可复现的**代码入口 / 真实测试 / CI / 证据 / 阻塞项**记录。无证据的项标注"未开始"。

#### W0-A：浏览器可行性闸门 — ✅ 通过

- **代码入口**：`src/features/teaching_animation/render/html_renderer_spike/playwright_backend.py`（`PlaywrightChromiumBackend`，固定 `playwright==1.62.0` + chromium headless shell 151.0.7922.34）；`ADR 0001`（Qt WebEngine 离屏判定为不可用）、`ADR 0002`（固定后端落定）。
- **真实测试**：`tests/render/html_spike/test_html_spike_playwright_gate.py`（5× byte-identical、乱序 0/中/尾帧、cancel、24 条恶意 fixture 语料、backend_build 断言）+ `tests/render/html_spike/test_html_spike_security.py`。**已在真实 Chromium 上运行通过（5/5，无跳过）**，`backend_build = playwright-chromium/py1.62.0/chrome-151.0.7922.34`。
- **CI**：`.github/workflows/html-renderer-gate.yml`（独立 gate job，装 `.[dev,html-renderer]` + `playwright install chromium`，先断言 `is_available()==True` 再跑 `-m html_renderer`，使测试无法跳过）。
- **证据**：本机 `pytest -m html_renderer` = 5 passed（含 `test_w0a_gate_passes_on_pinned_backend` 全套确定性/安全检查）。
- **阻塞项**：无。（原"真实浏览器验收不可复现"已由 CI gate + `pyproject.toml` 的 `html-renderer` extra 关闭。）

#### W0-B：冻结核心合同 — ✅ 通过

- **代码入口**：`core/contracts.py`（`FrameContext` / `CanvasSpace` / `RenderSurface` / `RenderPlan` / `SceneRendererV2` Protocol）；`render/v2_adapter.py`（`SvgSceneRendererV2` 字节等价适配）；`core/migration.py`。
- **真实测试**：`tests/render/test_w0b_render_contracts.py`（V2 字节等价、未知字段保留、alpha/DPR/proxy fixture）。
- **CI**：主测试套件（pytest.ini）。
- **证据**：v1 场景 round-trip；preview/export 抽样帧与旧基线无阻断差异。
- **阻塞项**：无。

#### W1：动效语言原生垂直切片 — ✅ 通过

- **代码入口**：`src/features/teaching_animation/motion/`（series/sequence/interpolate/spring/preset）；command facade。
- **真实测试**：`tests/motion/test_w1_s1_vertical_slice.py`（S1"四卡弹出"golden + undo/redo + 多进程同值）。
- **阻塞项**：无。

#### W2：Freeform HTML 可交付闭环 — ✅ 收口（P0+P1 完成；P2 #1/#2/#3 已闭环，#4 已验证）

- **代码入口（主链路由）**：`render/scene_renderer_router.py`（`select_scene_renderer` 唯一决策点 + `HtmlSceneRendererV1Adapter` 把 V1 主链桥接到 V2 HTML renderer + `backend_for_scene` 区分缓存键）。已接入 6 个宿主入口：`host_adapters.py`（时间线 preview+export）、`render/render_worker.py`（导出服务+缓存预热）、`composite/nodes/motion_graphic_node.py`（合成节点）、`ui/editor_controller.py`、`facade.py:preview_frame`、`ai/service.py:render_preview`。
- **代码入口（HTML 链）**：`core/html_layer.py`（`FreeformHtmlMotionLayer` schema 落盘，SCENE_SCHEMA_VERSION 仍为 2，additive 可选字段）、`core/html_layer_commands.py`（`SetHtmlLayerCommand` 可 undo）、`ai/html_generation_protocol.py`（生成管线 sanitize→static→preview→approve）、`render/html_renderer_service.py`（池化、crash-replacement、fail-closed、排队阶段 cancel/timeout）、`render/html_scene_renderer_v2.py`（V2 adapter）。
- **真实测试**：
  - `tests/render/test_scene_renderer_router.py`（9 项：路由、V1 adapter、host/worker/facade 集成、fail-closed）— 纯逻辑，不需要浏览器。
  - `tests/render/test_html_s0_end_to_end.py`（真实 Chromium 主链端到端：HTML-only 走主链、2×DPR CSS viewport、透明 alpha、bound asset 解析真实字节、resolver 不跨请求泄漏、**主链从 asset_root 解析 bound image**、导出逐帧 byte-identical、Undo 恢复 SVG 路由）+ `TestResolveBoundImageBytes`（4 项纯逻辑：真实字节/无 asset_root/跳过非 image/缺失降级）。
  - `tests/render/test_html_export_e2e.py`（真实 Chromium + FFmpeg：rgba_sequence 与 mp4 导出，manifest 断言 viewport/DPR/renderer_build/font_set_hash/sanitizer）。
  - `tests/render/test_html_renderer_service.py`、`tests/core/test_fh01_html_layer.py`、`tests/core/test_html_layer_commands.py`、`tests/ai/test_html_generation_protocol.py`。
- **CI**：`.github/workflows/html-renderer-gate.yml`（同 W0-A，覆盖 W2 的真实后端用例）。`pyproject.toml` 的 `html-renderer` extra + `scripts/bootstrap_html_renderer.sh`。
- **证据**：HTML-only 场景经主链产出真实 RGBA 内容（红 div 到达时间线/导出）；2×DPR 下 100 CSS-px div = 200 物理像素；透明 capture 空角 alpha=0；导出两轮逐帧 byte-identical。本机 `pytest -m html_renderer` = 5 passed；S0 真实用例 8/8 通过。
- **阻塞项 / P2 跟进**（不阻断闭环，属保真度与元数据完善）：
  1. ✅ **真实素材字节**（已闭环）：`HtmlSceneRendererV1Adapter.prepare` 经 `resolve_bound_image_bytes(scene, context)` 把 `asset_refs` 中 bound image 解析为真实字节注入 `session.asset_resolver`；fonts 仍由 font lock 注入；缺失/未声明资产降级为后端占位图而非崩溃。验证：`test_host_resolves_bound_image_from_asset_root`（真实 Chromium，主链端到端）+ `TestResolveBoundImageBytes`（4 项纯逻辑）。
  2. ✅ **导出 manifest provenance**（已闭环）：`export_service._provenance` 记录 viewport/DPR/backend，并以 `_effective_html_renderer_build` 取渲染时真实 pinned build（layer stamp 或 shared service 的 `backend_build`）；`html_layer` 子表记录 renderer_build/font_set_hash/sanitizer/source/csp。验证：`tests/render/test_html_export_e2e.py`（rgba_sequence + mp4 sidecar，断言 viewport/DPR/renderer_build）。
  3. ✅ **编辑器内实时 HTML 画布预览**（FH-06/09）：已由异步后台捕获、请求合并、取消旧帧和 rgba8 画布显示闭环；真实 Chromium 捕获仍由独立 html-renderer CI gate 执行。
  4. ✅ W0-A/W2 真实后端 CI matrix 已含 `ubuntu-latest`（Linux 沙箱需 `playwright install --with-deps`，已在 workflow 内）；本机 macOS 验证通过。
- **W2 期内确定性修复（附带，关键）**：
  1. **字体锁实际生效（最关键）**：字体锁原本完全失效——`@font-face src:url(topos-asset://font/...)` 但 Chromium **不会**从自定义 scheme 抓取 `@font-face`（请求从不发出），故 `__topos_sans` 一直静默回退系统字体，跨机确定性从未真正成立。改为把打包字体以 base64 `data:` URI 内联进锁 CSS（`default_font_lock_css`，`lru_cache` 缓存），并让渲染器在 render 时把 `font-src data:` 并入 CSP（`ensure_font_lock_csp`），与 URL policy/resolver 一致。内联用 **woff2** 交付格式（~832KB，较 TTF 的 2.4MB 减 65%；`font_set_hash` 仍以 TTF 源计算，故切换压缩不扰动缓存）。验证（真实 Chromium）：`document.fonts.check('__topos_sans')==True`；锁定渲染 ≠ 系统 sans-serif 参考；CSP 无 font-src 的 layer 仍能加载（merge 失效时该用例 FAIL）。
  2. **`topos-asset://` asset_id 解析**：原 `split("/",1)[0]` 丢弃命名空间段，使 `image/logo` 等带 `/` 的 id 永不命中 allowlist（namespaced image 渲染为占位图）。改为只剥离 query/fragment 后保留全路径（注：此修复对 fonts 无效——见上条，Chromium 根本不发请求）。
  3. **缓存键完整性**：`RenderCacheManifest.cache_key` 增加 `renderer_build` 与 `font_set_hash`，`render_worker` 透传所选 renderer 的 build，HTML 场景由 `build_cache_manifest` 填充 `font_set_hash`——满足不变式 §3.1（renderer/font 升级失效旧帧）。当前为内存进程级缓存（同进程内 build/font 恒定，故为前瞻性结构修复）；持久化缓存落地时即生效。
- **W2 审计后加固与接通（A+B 闭环，2026-08-12）**——三处 Explore 审计发现"已建"与"已接通"是两回事：渲染链生产级，但 AI 生成闭环、扩展命令、模板、动效原语多为有单测但无生产调用方的孤儿库。本轮按 A（加固"已声称闭环"）+ B（接通"已建未接"）交替推进：
  - **A-determinism**：`RenderContext` 增 `dpr` 字段、`RenderCacheManifest.cache_key` 纳入 `dpr`（确定性合同 §3.1 明列 DPR 但此前未强制）；`composite/nodes/motion_graphic_node.py` 报告真实 alpha（HTML renderer 捕获 PREMULT，原硬编码 straight 致合成黑边）、HTML 渲染异常 fail-soft（不崩 GL 循环）、删除死 import；`export_service._provenance` 记录 `asset_hashes`/`font_hashes`/`bound_asset_ids`（复用 `build_cache_manifest` 与缓存键一致），`dpr` 取 context 真值。
  - **A-security/integrity**：`TeachingAnimationExportService.preflight` 对 HTML 场景重跑 `run_html_quality_checks`（含 sanitizer fixed-point re-scan、CSS budget、viewport overflow、asset 解析），ERROR 级 fail-closed（AQ-10/§7.2 #3）——此前质量门只在 AI 生成期跑，加载/迁移/手改的 `.topos` 直送渲染器。`RenderCacheManifest` 增 `declared_asset_ids`（scene.asset_refs ∪ html_layer.bound_asset_ids），防止非严格预览路径下缺素材占位帧缓存中毒（审计 D-4）。
  - **B-wiring（让功能实际可达）**：`ExtendedCommandRegistry` 补齐 list-aware `dry_run`/`apply`/`undo_applied`（`TeachingAIProposal` 增 `compiled_counts` 使 replace_element/group_elements 的多命令展开 undo 平衡），成为基础 registry 的超集；`module.py` 三处（`get_ai_command_registry`、初始构建、编辑预览/确认）全部切到扩展 registry，解锁 16 条扩展命令（原仅 add_element/set_animation 通过）。`TeachingAnimationAIService.generate_html`/`apply_html_layer` 接通 AI→HTML 闭环（`generate_html_with_llm`→`SetHtmlLayerCommand`，此前无生产调用方）；`HtmlGenerationOutcome` 增 `authored_source` 以承载 FH-01 溯源。`apply_template`/`set_brand_theme`/`replace_asset_slot` 进 AI 命令目录（TM-02），15 个可执行模板与品牌 token 系统对模型可达。
  - 验证：`tests/ai/test_html_generation_service.py`（5 项，假 LLM 客户端，无需浏览器）、`test_motion_graphic_b2_execution.py`（drop-in registry + 模板命令）、`test_teaching_animation_phase6.py`（DPR/declared_asset_ids 缓存键、export HTML 门）。955 项 touched-area 测试通过。
  - **W3 前置缺口（已在后续批次闭环）**：动效原语、设计 token、模板参数 schema、Hierarchy、HtmlPatch 和编辑器 Inspector 均已接入生产路径；以下 W3 记录保留作为实现证据。
  - **W3 地基落地（2026-08-12，续）**——上述清单已全部闭环：
    - **动效原语进渲染路径（MT-05/TM-03）**：`apply_motion_preset`/`stagger_elements` 进 AI 命令目录，经 `compile_preset_to_track` 把 spring/preset 采样成 `KeyframeTrack`，evaluator 直接读取——弹入/stagger 首次真正影响像素（不动 evaluator）。
    - **设计 token 渲染期解析（TM-05）**：`evaluate()` 对每个元素 style 跑 `resolve_style`，`$palette.*` 不再渲染成字面量；`__locked__` 受尊重。
    - **Hierarchy 进 evaluator（SG-01）**：携带 Hierarchy 的场景，子元素世界变换 = 父链合成（移动父组带动子元素）；隐藏祖先零后代 opacity；无 Hierarchy 场景字节一致（fast path）。
    - **`schema_for_ai` 导出（TM-06）**：context provider 输出完整命令词汇表（25 条）+ 每个模板的参数 schema + 动效 preset id（`lru_cache`，registry 变化自动达 prompt）。
    - **HtmlPatch apply 半边（FH-10）**：`apply_html_patch`（stdlib 栈式标签扫描定位节点，6 种操作，re-sanitize 安全网）+ `ApplyHtmlPatchCommand`（可 undo）；附带 sanitizer 修复：CSS 自定义属性（`--brand`）不再被 allowlist 误删（否则 token 系统失效）。
    - **Auto Layout 渲染期 pass（SG-03/UX-12）**：`SceneLayout` 持久化于 `scene.metadata`，`evaluate()` 每帧对 live canvas 重跑 `resolve_stack/resolve_grid`；group 内元素位置/尺寸取自解析矩形，scale/rotation/opacity 仍可动画（入场效果叠加在布局框上）；非 group 元素与无 layout 场景字节一致。16:9↔9:16 自适应重排已验证。
    - 验证：1098 项 render/core/motion/ai 测试通过；AI 命令词汇表 2→25 条并自动告知模型。
    - **Figma 式编辑器地基（2026-08-12，纯新功能构建）**——按"最优选择，自动切换"逐步落地：
      - **图层树（UX-10）**：`SetHierarchyCommand`（lock/hide/solo 可 undo）+ Qt-free `build_layer_rows`（z-order 降序、HTML layer 只读行）+ `LayerTreePanel`（eye/lock/solo 图标、拖拽重排、与画布/Inspector 三向选择同步）+ `SceneView` hit-test 跳过 locked 元素。
      - **Auto-key（UX-14）**：`SceneEditorController.auto_key` 标志；开启时 `commit_preview` 在当前帧为变动的 transform 属性写关键帧（宽 range 防 clamp），第二次拖拽扩展同一 track（插值）。toolbar "Auto-Key"（K）切换。
      - **HTML Inspector（FH-06）**：`HtmlInspector` 从 `editable_slots`/`editable_tokens` 构建表单（token 用当前值预填），编辑发 edit 意图 → 窗口对 live layer 构 `HtmlPatch` → `ApplyHtmlPatchCommand`（可 undo、re-sanitize）。`QStackedWidget` 在结构化 Inspector 与 HTML Inspector 间切换（选中 HTML layer 行触发）。
      - **健壮性**：编辑器 `_refresh_scene` 的预览渲染 `except` 从 `(TypeError, ValueError)` 扩到 `Exception`——HTML 场景（Playwright 缺失/崩溃）的 `HtmlRenderError` 不再崩编辑器，状态栏提示而 Inspector/图层树/时间线照常可用。
      - 验证：23 项 UI 测试（图层树/Auto-key/HTML Inspector）+ 1098 项 render/core/motion/ai 测试通过；端到端：HTML 行选中切 Inspector、token 编辑 apply+undo、HTML 场景不崩。
    - **HTML→模板沉淀引擎（FH-09 核心，2026-08-12）**——把"已验证的自由 HTML"沉淀为可复用模板的第一步（纯逻辑、无浏览器/Qt 依赖）：
      - `HtmlTemplate`（`core/html_template.py`）+ `HtmlTemplateRegistry`：镜像 `ExecutableTemplate` 的公开形状（`template_id`/`version`/`display_name`/`params_schema`/`default_params`/`validate_params`/`schema_for_ai(kind="html")`），但它的"形状"是带占位符的 sanitized HTML 而非结构化元素——这是 15 个内置模板之外的**第一类**可复用 HTML 制品。带 `source_hash`/`provenance_layer_revision` 审计链（FH-09 "保留原 layer 作 fallback"）。
      - `promote_html_layer`（`render/html_layer/template_promote.py`）：纯变换，读 `editable_slots`（text→`{{slot:NODE_ID}}`、asset→`{{asset:NODE_ID}}`）+ `editable_tokens`（color/string 分类→`{{token:NAME}}`）的**当前值**，替换为占位符并生成 `ParamSpec`（default=当前值）；缺失节点/token 优雅跳过；原 layer 不变（fallback）。`apply_html_template` 反向：参数合并→单遍替换（值不再被二次扫描）→**re-sanitize**（防注入：`<script>` 参数被剥离）→重建 layer。
      - 顺带修复 `_locate_element`：原目标搜索跳过 void 元素，导致 `<img data-topos-node-id>` 上的 asset slot 不可定位（`_op_set_asset` 在 img 上同样失效的潜在 bug）；现仅跳过闭合标签，void 元素可作为目标。
      - 验证：34 项纯逻辑测试（promote/apply/registry/serialization/security）；1161 项 render/core/motion/ai 测试通过。
      - **后续 sub-batch**：context_provider 把会话内 html 模板并入能力清单 —— **前提**：先让 `TeachingAnimationAIService` 在生产实例化 + 建模型→service 派发（见下"审计发现"），否则往清单里塞会误导模型。
      - **FH-09 sub-batch 2：AIService 接通（2026-08-12）**——把 promote/apply 引擎接进 `TeachingAnimationAIService`，对齐已有 `generate_html`/`apply_html_layer` 的服务方法模式（会话级 registry，不走命令目录——promotion 不改 scene，是 service 操作）：`promote_html_template(template_id,display_name,...)` 把当前 scene 的 html_layer 蒸馏成 `HtmlTemplate` 并注册到 `self.html_templates`（原 layer 不动，作 fallback；无 layer 报 ValueError）；`apply_html_template(template_id,params,*)` 查注册表→`validate_params`→`apply_html_template` 引擎（替换占位符→re-sanitize→重建）→`SetHtmlLayerCommand` 落 scene（可 undo，与 `apply_html_layer` 同路径）；`list_html_templates()` 暴露会话注册表。验证：11 项 wiring 测试（promote 注册/不改源/无 layer 报错/版本、apply 覆盖/默认/未知模板/必填参数/几何保留、注册表会话隔离），无浏览器/Qt。
      - **FH-09 sub-batch 3：编辑器接通（2026-08-12）**——`MotionGraphicEditorWindow` 加窗口级 `HtmlTemplateRegistry` + toolbar "Save as Template"/"Apply Template…"。逻辑/弹窗分离可测：`_save_as_template(name)`/`_apply_template(id)` 干活（无模态），`*_triggered()` 开弹窗转发；promotion 走纯引擎，re-apply 落 `SetHtmlLayerCommand`（同 undo 路径）。动作启用态随 scene 有无 html_layer / 注册表有无模板刷新（顺带修 init 顺序：`_create_actions` 必须在首次 `_refresh_scene` 前）。验证：7 项 Qt 级 wiring 测试（promote 注册/无 layer/启用态、apply 替换+undo/未知模板/默认参数、动作启用态）。
      - **审计发现（2026-08-12，Explore）**：`TeachingAnimationAIService` 在**生产环境从未实例化**——`generate_html`/`apply_html_layer`/`promote_html_template`/`apply_html_template` 全部零生产调用方（仅测试）；模型输出仅经 `_handle_model_scene_edit_response`/`_build_scene_from_commands` 走 base-4+16 扩展命令白名单，**无任何分支**触达 service 方法；`capabilities` 元组里的 `generate_html` 等是"仅展示无派发"的标签。结论：在 service 进生产 + 建派发路径前，往能力清单塞 HTML 操作会误导模型。这是 W3 里最大的"已建未接通"缺口，单独立项。
      - **#4 实现设计（2026-08-12，Explore 决策级地图）**——HTML 管线**本就正交**于命令编译路径（独立 pipeline、独立不可变命令 `SetHtmlLayerCommand`、不在 `EXTENDED_COMMAND_NAMES`），故最小切片是**纯增量**：新增 chat_mode + pending dict + handler 对，复用现有 card+confirm 机制，**不碰** `_build_scene_from_commands`/`accepted_names`/`command_catalog`。
        - **(a) service 唯一生产构造点**：`TeachingAnimationModule` 加惰性 `_get_html_service_for(scene_id)`（近 `_resolve_scene_for_edit`），从 `get_shared_scene_store()` 取 scene 构 `TeachingAnimationAIService(scene)`，按 scene_id 缓存到 `self._html_services`。scene-scoped，不做单例。
        - **(b) 派发挂钩**（镜像 build 的 3 处）：`attach_main_window` 注入 `set_teaching_html_generate_response_handler`→新 `_handle_model_html_generate_response`；controller 加 `chat_mode="teaching_html_generate"` + `start_html_generate_chat` + `_on_chat_finished/_on_chat_failed/_on_chat_cancelled` 三个分支；module 加 `_trigger_html_generate` + `_is_html_generate_request`（接进 `_handle_ai_request`）。
        - **(c) 审批门复用**：预览轮 `generate_html(approved=False)` 暂存 non-writable outcome + `set_motion_graphic_proposal` 卡片（详情露 `outcome.blocking_reasons`）；确认轮 `_is_confirmation_text`→新 `_confirm_html_generate_request` 重跑 `generate_html(approved=True)` 再 `apply_html_layer`（其内 `outcome.writable` 是硬门，`SetHtmlLayerCommand` 落 scene 带 undo）。`_handle_ai_request` 的 `pending` 元组加第三 kind，`_confirm_motion_graphic_plan`/`_cancel_motion_graphic_request` 各加第三分支。
        - **(d) 可测面**：新增 `_FakeLlmClient` 集成测试（镜像 phase5 的 `test_model_scene_edit_response_is_reviewed...`：raw html → 卡片 → confirm → `scene.html_layer` 置位 + `source_hash==sha256(raw)`；以及 `_handle_ai_request` 路由测试，确保新意图不吞 motion-graphic/scene-edit 文本）。
        - **风险护栏**：3 个打破向量——`_teaching_request_handler` 短路不能吞既有意图；`pending` 尾逻辑要认第三 kind；`_confirm_motion_graphic_plan` 二分支要改三分支。属热路径（module.py + ai_edit_controller.py），多文件，需独立聚焦批次实施 + 集成测试，勿在长会话末尾草率。
      - **多选 phase-1（UX-08，2026-08-12）**——选择模型从单 id 升级为 `frozenset`，窗口是唯一选择 owner：`_set_selection(ids,*,source)` 单一 chokepoint，canvas 普通点击=替换、shift-点击=toggle（SceneView 加 `elementSelectionToggled` 信号，shift 不启拖拽），`LayerTreePanel` 切 `ExtendedSelection` + `selectionChanged(tuple)` + `set_selection(集合)` + `selected_row_ids`；Inspector 仍单元素（显示 primary=首个选中，HTML layer 行优先），其 `elementUpdateRequested`/`keyframeRequested` 不动。批量删除：树删除按钮对每个选中元素 emit `removeRequested`。三向同步靠 `source` 防 echo（树来的不再回 set_selection）。关键 Qt 细节：`setCurrentItem(item, NoUpdate)` 否则 ExtendedSelection 下会 ClearAndSelect 抹掉其它选中行。验证：8 项多选测试（canvas 替换/shift 进出、树镜像/不 echo、inspector primary、批量删除）+ 38 项编辑器 UI 全过；535 项 render/core 不退步。
      - **多选 phase-2：drag-moves-all（UX-08，2026-08-12）**——拖一个选中元素带动所有选中元素同 delta 位移：`_on_drag_started` 把每个选中元素的原始 (x,y) 存进 `_drag_anchors` 并对每个 `begin_preview`；`_element_dragged` 按 delta=(拖动元素当前位)−(其锚点) 平移所有锚定元素；`_on_drag_finished` 对每个 commit（每元素一条 `UpdateElementCommand`，可逐个 undo）。配合：普通点击已处在多选中的元素**保持**多选（不替换），否则后续拖拽无法带动全部。单选拖拽行为不变。验证：3 项拖拽测试（单选拖一个、多选拖全部同 delta、点多选中元素保持集）+ 编辑器 UI 41 项全过。
      - **多选 phase-3a：align/distribute（UX-08/SG-03，2026-08-12）**——`ui/align.py` 纯几何（`compute_alignment` 8 模式：left/center-h/right/top/middle-v/bottom + distribute-h/v，按 `width*scale_x` 算有效盒，仅改 x/y 保留 w/h/scale/rotation）；窗口 toolbar "Align" 弹出菜单，≥2 元素选中时启用，`_align_selection` 走新 `CompositeCommand`（多 `UpdateElementCommand` 一次 apply/逆序 undo）→ **单步 undo** 还原全部。验证：13 项纯逻辑 + 2 项窗口（双元素 align_left 单 undo 还原、单选中 no-op）；编辑器 UI 56 项全过。
      - **HTML live preview phase-1（按需，2026-08-12）**——`SceneView` 从纯 SVG 扩到支持 `rgba8`：`set_frame` 按 `pixel_format` 分支（svg→`QSvgRenderer`，rgba8→`QImage(data,w,h,w*4,RGBA8888).copy()` 脱离 Python bytes 生命周期），`paintEvent` 对应分支（drawImage vs svg render）。HTML 场景的浏览器捕获是 ~0.2–0.9s/帧同步调用，**不能**每次 scrub 都跑（会冻 UI），故 `_refresh_scene` 对 HTML 场景跳过同步渲染（SVG 照旧逐帧），改由 toolbar "Render Preview" 动作**按需**渲染当前帧（走已有 `editor_controller.render()` → router → HTML renderer → Playwright，fail-safe 在窗口层把 `HtmlRenderError`/无后端转状态栏提示）。验证：5 项 SceneView 栅格测试 + 5 项窗口测试（HTML 启用/SVG 禁用、成功显示 RGBA、失败转状态不崩、HTML refresh 不调 render）；编辑器 UI 66 项全过。**未验证**：真实 Playwright 捕获（本环境无后端，由 e2e skip 覆盖）。
    - **W3/W4/W5/W6 收口状态（2026-08-13）**：前述 live-scrub 后台线程、多选框选/批量编辑、AI service 生产派发、组件 master/variant、跨画幅 variant、reparent 嵌套均已实现并分别提交；motion blur、粒子和色彩/HDR 也已进入声明、RenderPlan、能力矩阵、命令与宿主桥。

#### W3：布局、Figma 式编辑与模板沉淀 — ✅ 核心实现完成

- **提交证据**：`07b89136`（AI HTML 生产派发）、`41699089`（HTML 异步预览）、`5b95dc50`（框选/批量编辑/reparent）、`08b07d99`（组件 master/instance/variant 与响应式变体）。
- **代码证据**：编辑器选择/图层/Inspector/组件/响应式/模板链路均走 scene command；后台预览具备 latest-request 合并、取消和 stale-result 丢弃。
- **验证**：编辑器 UI、渲染/核心/AI focused tests 通过；真实 Chromium 用例继续由 W0-A/W2 CI gate 执行。

#### W4：原生加速与一致性 — ✅ 核心实现完成

- **提交证据**：`8bd79f46`（声明式效果与 SVG fallback）、`4dbfea72`（Composite shared Effect Catalog GPU bridge）。
- **代码证据**：SVG/GPU/HTML capability matrix、preview/final profile、RenderPlan effect pass、unsupported export block、资源 cleanup 与诊断均已接通；motion blur 复用 Composite 的 motion shader variant。
- **验证**：W4 capability/consistency 与 shared effect/composite focused tests 通过；真实 ModernGL 压力/显存门依赖带 GPU 的 CI job。

#### W5：素材、音频与合成 — ✅ 核心实现完成

- **提交证据**：`82fdb895`（AudioRenderPlan 与宿主映射）、`bfefac77`（带 hash/seek/license 元数据的 LocalAssetProvider）、`5e35aed4`（precomp/instance、vector mask、composition command）。
- **代码证据**：音频范围/源偏移/缺失诊断、项目相对路径与安全校验、场景引用、遮罩与 precomp 均可序列化、撤销并进入 SVG/Composite/Export 计划。
- **验证**：W5 audio/asset/composition 与 phase2 render focused tests 通过；真实音频混音和长项目压力属于宿主发布门。

#### W6：专业能力 — ✅ 合同与最小闭环完成

- **提交证据**：`af9ac39f`（professional capability contracts）。受控表达式已有 `motion/expression.py` 与 W6 单测。
- **代码证据**：`SceneColorPipeline`、粒子 emitter、确定性粒子位置、`SetProfessionalCapabilitiesCommand`、motion blur capability/Composite 映射、SVG 粒子 fallback、RenderPlan/cache key、Composite texture color/HDR 诊断均已接通。
- **能力边界**：Teaching SVG 输出是 8-bit，因此 HDR 场景明确报告 `svg_8bit_tonemap`；真实 HDR 保真需由 Composite 的 float16/float32（`f2`）路径和最终交付格式承接。粒子当前提供确定性 SVG 简化路径，GPU 精确路径保留在共享 Composite 能力边界内，没有伪造第二套 Teaching shader。
- **验证**：W6 professional/effect 34 项、表达式 25 项、W0-B 合同 28 项及能力可用性 focused tests 通过；无 GPU 环境下的实际 shader/显存与人工创意评测仍是外部发布门。

#### 发布前仍需完成的环境门 — ⏳ 外部验收

- 在 CI/目标机器执行真实 Chromium、ModernGL/GPU、连续取消/重启/资源回收和 4K cold/warm cache 压力测试；本地无相应后端时只能报告 skip，不能冒充通过。
- 按 §7.1.1 的 L4/L6 执行 S0～S3 人工视觉验收，并锁定机器、字体集、renderer build、DPR、asset hash 和输出 manifest。
- 若产品要求“Teaching 自己直接输出 HDR/真 GPU 粒子”，这属于后续能力扩展；当前实现已经完成可审计的 fallback/阻断合同，不将 SVG 8-bit 结果标为 HDR 等价输出。



```text
W0-A browser renderer spike / ADR / support matrix
        ↓（通过后才允许 HTML 路线）
schema / migration / CanvasSpace / FrameContext / RenderPlan / renderer Protocol
        ↓
TimeGraph + interpolate + spring + command transaction
        ↓
Freeform HTML schema + sanitizer + runtime sandbox + asset/font bridge + offscreen renderer service
        ↓
EditorState + HTML node identity + HtmlPatch + canvas/layer/inspector command mapping
        ↓
ExecutableTemplate + MotionPreset + layout/text contract（从优秀结果收敛）
        ↓
GPU scene renderer + shared Effect Catalog + proxy/cache
        ↓
assets/precomp/mask/transition + Composite bridge
        ↓
3D/particle/expression/HDR（每项独立立项）
```

不得倒置：W0-A 未通过前，不能把浏览器实时播放/截图当成导出，不能落盘 HTML layer；没有 `RenderPlan` 和 capability model 前，不能把 blur/glow/粒子直接塞入 SVG renderer；没有 sanitizer、runtime sandbox 与 resource bridge 前，不能把模型输出直接送进 browser；没有 HTML node identity、edit-mode 与 `HtmlPatch` contract 前，不能把 WebView DOM 直接当作可保存编辑结果；没有 `TimeGraph` 前，不能以 AI 批量生成 keyframe 伪造 Sequence；没有模板 compiler 前，不能把模板逻辑堆进 `module.py`。

---

## 6. 首批垂直切片与验收场景

### S-00：HTML renderer 可行性探针（W0-A 完成门）

输入：固定 1920×1080 HTML fixture（静态排版、CSS keyframes、`topos-asset://` 图片/字体）以及一组恶意 HTML/CSS fixture。

- 候选 renderer 在固定 build、viewport、DPR、字体集下，对 0/1/中间/末尾/关键帧执行乱序渲染；每个目标帧重复 20 次，RGBA hash 一致；
- bridge 仅由 Topos 注入，暂停所有动画后按 `frame/fps` seek，等待 layout/font ready 和两次 rAF 再 capture；禁止按墙钟或播放进度截图；
- CSP、request interceptor、ephemeral profile 和 `topos-asset://` handler 同时开启；远端 URL、`file:`、`data:` 超限、导航、新窗口、下载、存储和权限请求均被阻断并留诊断；
- renderer 在 timeout、取消、异常终止、连续 100 次渲染和工程关闭时无旧帧返回、无资源残留，且下一任务可重新启动；
- 产出 ADR：所选 backend、版本/启动参数、capture 方法、线程/进程所有权、支持 CSS 子集、已知性能基线和不支持能力。任一项失败则 HTML 相关波次停止，不以 GUI 预览替代导出。

### S0：自由生成的 HTML 科技标题页（W2 完成门）

输入：让 AI 自由设计 10 秒、1920×1080 的深色科技标题页，包含渐变背景、标题/副标题、三张信息卡和 CSS stagger 动画。

- AI 只输出 HTML/CSS；所有 JS、远端 URL、iframe、事件属性、外部字体和本机路径均被 sanitizer + runtime sandbox 拒绝；
- Topos 以固定 viewport 和 `frame/fps` 精确 seek，乱序请求同一帧得到相同 RGBA；
- 可通过 Inspector 改标题、卡片文字、品牌色 CSS variable、动画时长与延迟，不要求编辑全部 DOM；
- 在画布点选带 `data-topos-node-id` 的标题或卡片，可定位图层树和 Inspector；只有 `data-topos-edit-mode="absolute_transform"` 的节点可拖拽，其他 DOM 只能以内容/token/整体 layer 操作；
- 在 0/中间/末尾和动画关键帧生成预览；确认后插入 Graphics 轨；
- 资源、字体、CSS 预算、超时和动画闪烁问题都在质量报告中可见。

### S1：四步骤教学流程（W1 完成门）

输入：15 秒，四张步骤卡，标题、依次 spring pop、每步 12 帧 stagger、结尾 CTA。

- 使用 `SeriesGroup` + `stagger` + `spring` + `interpolate`，不手写大量 keyframe；
- 任意一个 beat 时长改变后，后续 beat 自动重排；
- AI 可执行“第 3 卡晚 8 帧、去掉回弹、CTA 改为品牌蓝”；
- 画布、时间线、Edit preview、导出在 0/中间/末尾/marker 帧一致；
- 所有操作都可撤销，且生成的 SceneDiff 可读。

### S2：品牌数据解释（W2/W3 完成门）

输入：30 秒，柱状图递增、数字计数、渐变卡片、中文/英文标题、深浅两套品牌主题。

- 切换主题只改 token，不破坏实例 override；
- 字体缺失、文本超出、安全区冲突在确认和导出前均可发现；
- GPU glow/shadow/blur 有 proxy 降级与导出精确路径；
- 1080p 交互预览与 4K 最终导出有明确 profile 和缓存命中证据。

### S3：屏幕内容替换与 HUD（W4 完成门）

输入：把一个 MG 标题和 HUD 作为 Composite 输入放入已跟踪的手机屏幕。

- 共享同一 `scene_id`，不复制 MG 场景；
- 透明/预乘 alpha 正确，字体与图标资源可解析；
- Clip 改文字后只使该 scene 的 cache、Composite dirty range 和最终输出失效；
- 任一缺失 provider/资源/效果都产生可操作诊断。

---

## 7. 验收、发布与治理

### 7.1 每个 PR 的最低验证

| 变更类型 | 必须验证 |
|---|---|
| scene/time/motion schema | migration round-trip、未知字段保留、command undo/redo、边界帧 |
| evaluator / interpolation / spring | 单元测试、固定输入 golden value、FPS/延迟/反向/外推边界 |
| renderer / effect | preview/export 抽样帧、alpha/color、GL resource release、取消路径、视觉 diff |
| Freeform HTML | sanitizer attack fixture、无网络/路径访问、固定帧重复渲染、viewport/font/asset hash、browser crash/timeout/release |
| Figma 式编辑器 | hit-test/node identity、画布/Layer Tree/Inspector 同步、选择与锁定、拖拽/对齐/缩放、`HtmlPatch` 冲突、auto-key、undo/redo、保存重开 |
| template / asset provider | schema validation、缺素材/缺字体/许可失败、横竖屏、CJK、版本迁移 |
| AI command | JSON schema、dry-run diff、invalid input、stale approval、重复 apply 幂等 |
| cache / scheduler | dirty range、cache invalidation、预算超限、取消/重试、无 UI 阻塞 |

### 7.1.1 完整验证金字塔与完成定义

“代码完成”不等于单元测试通过。每个波次的 PR 先运行其 focused tests，波次出口再运行下列完整层级；任何失败均回到拥有该合同的波次修复，不在后续波次以 fallback 掩盖。

| 层级 | 验证对象 | 关键断言 | 触发时机 |
|---|---|---|---|
| L0：静态合同 | dataclass/schema/JSON/command/AI input | schema version、未知字段保留、非法 enum/NaN/循环/越界被拒、patch precondition、序列化稳定 | 每个 PR |
| L1：纯函数 | time transform、layout、interpolate、spring、cache key、quality rules | 无 wall clock/随机隐式输入；边界、反向、FPS、颜色、浮点容差固定；property/fuzz 不崩且不生成非法值 | 每个 PR |
| L2：renderer component | SVG/HTML/GPU 的 renderer Protocol 与 asset/font resolver | 取消、超时、资源释放、fallback、RGBA dimensions/alpha/colorspace、evidence 完整；HTML 还验证 seek 与运行时隔离 | 每个 PR |
| L3：跨层集成 | SceneStore → command → evaluator → renderer → Clip/Node → preview/export | 相同 revision/profile 的抽样帧一致；undo/redo、保存/重开、cache invalidation、缺 capability 均可解释 | 每个波次出口 |
| L4：视觉与交互 | golden PNG/alpha/color metrics、感知 diff、Figma 式操作录制 | S0～S3 的关键帧、CJK/横竖屏/安全区；画布/图层/Inspector/时间线三向同步；不接受“肉眼大概一样” | 每个波次出口 |
| L5：安全与韧性 | HTML attack corpus、crash/restart、soak、并发/低资源 | 无网络/文件/下载/权限逃逸；100+ 次 render/取消/重开无泄漏或旧帧；项目损坏/缺资源 fail-closed | W0-A、W2、W4、发布前 |
| L6：人工验收 | 设计、剪辑、性能、可访问性 | 每个垂直切片由非实现者按脚本完成；记录浏览器/驱动/字体/机器、截图、问题和通过版本 | 每个波次出口与发布前 |

建议测试目录以能力命名（例如 `tests/teaching_animation/test_timegraph_*`、`test_html_security_*`、`test_renderer_consistency_*`、`test_editor_interaction_*`），而不是继续按“phaseN”堆文件。保留现有 phase 测试作回归入口，新增测试不得依赖网络、当前时间、机器已安装字体或真实 GPU。

本仓库的默认 pytest 配置以 `tests/` 为根。每个波次至少执行：

```bash
python -m pytest tests/feature_availability/test_teaching_animation_phase*.py tests/render/test_teaching_animation_phase*.py -q
python -m pytest tests/feature_availability/test_teaching_animation_webengine.py -q  # W0-A/W2 仅在受支持环境
python -m pytest tests -q  # 波次出口与发布前全量回归
git diff --check
```

若浏览器/GPU 测试需要特定环境，CI 应把它们标记为独立 `html_renderer` / `gpu_renderer` job，并在缺少能力时报告 **skipped with reason**；不得把失败静默标为通过。发布 artifact 还需在一台“无开发缓存”的干净机器上打开工程、渲染抽样帧并导出 S0～S3。

### 7.2 发布门

任何版本声称“可交付 Motion Graphic”前，至少满足：

1. 目标 render profile 下首/中/尾/marker 帧可渲；
2. 已声明的字体、素材、effect、template/provider 均可解析且许可证状态通过；
3. Freeform HTML 已通过 sanitizer、资源协议和复杂度预算；不包含 JS、网络、iframe 或未声明的可执行能力；
4. 文本无溢出，关键内容在 safe area，动画无阻断性 blank/flicker/未收敛状态；
5. preview/export 在规定抽样帧的时间、图层、alpha、色彩元数据一致；
6. 失败、取消、缓存失效和重开工程后，场景、时间线、browser worker 和 GPU 资源均保持一致；
7. 交付包含 manifest（scene/template/HTML sanitizer/font/asset/effect/render profile/hash）。
8. 已声明可编辑的 HTML 节点和结构化元素可从画布、图层树、Inspector 一致定位；编辑结果全部以 command/patch 保存，不依赖残留 WebView DOM 状态。
9. 若含 HTML layer，交付 manifest 锁定 renderer build、启动参数、viewport/DPR、font set、HTML/sanitizer/frame-bridge 版本；不同 build 不可声称逐像素复现。
10. 若含 scene audio，`AudioRenderPlan` 与输出音频的起止、采样率、响度/ducking 结果经过预检；无法混音时必须阻断或明确输出静音策略，不能静默丢音。

### 7.3 量化成功指标

| 指标 | W1 | W2 | W4 |
|---|---:|---:|---:|
| 可完成任务的自由/模板作品 | 5 个原生动效样例 | 3 个自由 HTML 场景 | 15 个模板 + 3 个 renderer |
| 1080p 常见场景交互预览首帧 | < 1 s（目标） | < 2 s（目标，browser cold start 单列测量） | < 0.5 s（目标，warm/cache） |
| 4K 单元素修改后的无关区域重渲 | 不适用 | 0 | 0 |
| preview/export 抽样帧不一致 | 0 个阻断差异 | 0 个阻断差异 | 0 个阻断差异 |
| AI 高层动效编辑的 command 成功率 | >= 95% fixture | >= 97% fixture | >= 98% fixture |
| 可交付前质量门漏检的阻断问题 | 0 | 0 | 0 |

性能数字必须在基准机器、样本场景、render profile 固定后写入实际 SLO；上述仅是产品目标，不是未经测量的承诺。

---

## 8. 近期执行清单（建议从这里开始）

1. **W0-A，先写探针而非 schema：** 创建静态/动画/恶意 HTML fixture，验证候选 renderer 的 no-network load、固定字体、seek、RGBA capture、乱序帧、cancel、crash/restart；写 ADR。没有通过就停止 HTML 路线，不修改工程格式。
2. **W0-B，冻结最小合同：** 定义 `CanvasSpace`、`RenderSurface`、`FrameContext`、`RenderPlan`、renderer Protocol 与 `RenderEvidence`；再做 v1→v2 migration、未知字段 round-trip 和 SVG renderer/host adapter 兼容。
3. **W1，只做原生动效 S1：** 建立 `motion/` 纯函数层：`TimeRange`、`Sequence/Series`、`interpolate`、`spring`、`measure_spring`；配齐 golden/property tests，最后才扩展 `command_catalog` 和最小 timeline/Inspector。
4. **W2，做安全 HTML 的最短闭环：** 依据 ADR 实现 sanitizer + runtime sandbox、`topos-asset://`/font registry、`HtmlFrameBridge`、独立 `HtmlRendererService`；完成 S0 preview/export/插轨/Undo，再实现 `HtmlPatch` 的内容、asset、token 编辑。
5. **W3，才扩展 Figma 式编辑：** `EditorState`、node identity/edit-mode、Layer Tree、Inspector、absolute transform wrapper、Auto Layout、auto-key、组件/实例；Flex/Grid 内容节点不得被直接拖拽破坏。
6. **W4，用数据选择原生 GPU：** 先从 W2/W3 的性能和质量 evidence 选出 3 个高频效果，再通过共享 `EffectCatalog` 实现 blur/glow/shadow、代理/缓存和 visual regression；不创建第二套 shader registry。
7. **W5/W6，小批量扩展：** 每次只接一个素材 provider、一个 transition 或一个专业效果，完成 schema/capability → renderer → UI → AI command → 全量验证；再处理音频、Composite、HDR、粒子和受控表达式。

这条路线的核心是：**先以受控 Freeform HTML/CSS 释放 AI 的网页设计能力，并以 Figma 式的直接编辑把用户纳入闭环；再把验证成功的结构和动效沉淀为 Topos 自己的时间、动效、模板和原生渲染语言。** 这样既能快速接近 Remotion 的设计体验，也保留 Topos 在专业时间线、合成、颜色和工程资产上的差异化。
