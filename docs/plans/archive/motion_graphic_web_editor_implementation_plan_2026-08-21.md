# Motion Graphic 网页式编辑器实施计划（2026-08-21）

## 1. 目标与范围

本计划将当前 Motion Graphic（MG）编辑器升级为**网页优先的可视化动画编辑器**：用户在画布上创建文字、形状、布局和动画；底层保持为 HTML、CSS、内联 SVG 和受控 JavaScript，而不是将编辑结果绘制成一张图片。

编辑器的设计目标不是复刻 Illustrator、Photoshop 或 Figma 的完整矢量绘图系统，而是提供适合 MG 模板制作的网页式场景搭建能力：

```text
用户工具操作 / 属性面板 / 时间线
                ↓
TeachingSceneDocument（唯一可保存、可撤销的场景真相）
                ↓
DOM Materializer（HTML + CSS + SVG + 动画运行时）
                ↓
Chromium 实时预览 ─────────────→ 时间线视频缓存 / 最终导出
```

### 1.1 本期明确包含

- HTML 文字、内联 SVG 形状、图片/视频资源和模板 HTML 的统一图层体系。
- 在 Chromium 网页预览中直接绘制和编辑，而不停止背景的 Canvas/JavaScript 动画。
- 网页布局（Absolute、Flex、Grid、约束）与基础响应式画幅能力。
- 可编辑的样式 Token、动画关键帧、组件和可迁移模板目录。
- 预览、缓存、导出的一致渲染链路及高质量缓存策略。

### 1.2 本期明确不包含

- 位图画笔、像素级修图、图像合成或 Photoshop 式图层蒙版系统。
- 将任意第三方 HTML/CSS 无损反向解析为可编辑图层树。
- 在 UI 线程中直接进行 OpenGL、ModernGL、编码或 Playwright 捕获。
- 用屏幕截图作为文字或 SVG 形状的最终表达形式。

### 1.3 成功标准

1. 在 HTML 粒子背景上拖出文字或 SVG 图形时，背景持续动画且不会重新加载。
2. 保存并重新打开工程后，图层、参数、动画、模板参数和 HTML 场景保持一致。
3. 编辑器预览、时间线缓存与最终导出的每一帧来自相同的 Chromium DOM 语义。
4. 新建模板只需复制一个目录；Effects 面板可播放其 GIF 悬停预览。
5. 用户不需要理解 HTML/CSS 也能完成常见 MG；高级用户可以在受控范围内编写代码。

---

## 2. 当前实现基线与约束

本计划必须建立在现有 MG 实现上，而非另起一套并行架构。

| 已有能力 | 当前模块 | 在后续计划中的定位 |
|---|---|---|
| 场景唯一数据模型 | `src/features/teaching_animation/core/model.py` 中的 `TeachingSceneDocument`、`SceneElement`、`SceneAnimation` | 所有可视编辑均通过不可变场景文档提交；不得让 UI 或 HTML 字符串成为另一份真相。 |
| 兼容与校验 | `core/migration.py`、`core/validator.py` | Schema 变更必须提升版本并补迁移；未知字段不可静默丢失。 |
| HTML 自由图层 | `core/html_layer.py` 的 `FreeformHtmlMotionLayer` | 承载模板作者的受控 HTML/CSS/JavaScript 背景及代码组件。 |
| Chromium 预览 | `ui/html_browser_preview.py` | 编辑器中的网页运行时；所有 DOM 更新必须增量执行，不能调用全页 `setHtml`。 |
| Chromium 导出桥 | `render/html_scene_renderer_v2.py`、`render/html_renderer_service.py` | 缓存、导出复用固定浏览器后端；失败必须结构化上报。 |
| 主渲染路由 | `render/scene_renderer_router.py` | HTML 场景通过 HTML renderer；纯场景继续走 SVG renderer。 |
| 原生网页叠层 | `render/html_native_overlay.py` | 将 `SceneElement` 材化为真实 DOM 文本和 inline SVG，而非 QPainter 位图。 |
| 编辑器窗口与命令 | `ui/motion_graphic_editor_window.py` 及现有 command/service 入口 | 工具栏、参数、图层、时间线只能通过既有编辑提交路径修改场景。 |
| 模板目录 | `templates/motion_graphic/<template-id>/` | 每个模板目录包含 `manifest.json`、`template.html`、`preview.gif`。 |

### 2.1 必须遵守的工程约束

- 场景、工程和时间线变更通过既有不可变模型与 command/service 入口提交，保证 undo/redo、revision、预览和持久化一致。
- UI 层只维护状态、信号和控件；Chromium 捕获、GPU 和编码保持在已有后端线程/任务队列中。
- HTML 场景无浏览器后端时必须显式报结构化错误，不能悄悄降级为 SVG 而丢失网页内容。
- HTML / SVG 材化必须白名单化并转义；禁止将用户自由 HTML 当作可信 SVG 属性拼接。
- Schema、模板清单、项目保存和导出行为变更均须有 focused tests。

---

## 3. 目标数据与运行时架构

### 3.1 三层职责边界

| 层 | 责任 | 允许直接编辑 | 不允许承担 |
|---|---|---|---|
| 场景模型层 | 页面、图层、元素、样式、动画、资源、Marker、组件实例、可撤销历史 | 编辑器工具、属性面板、时间线 | 直接运行浏览器脚本、保存临时 DOM 状态 |
| DOM 材化层 | 将场景节点转为 HTML、SVG、CSS 变量、WAAPI/CSS 动画 | 只由 materializer 控制 | 成为项目的第二份可保存真相 |
| 自定义代码层 | 模板 HTML、模板 CSS、受控 JS、代码组件 | Source / DevTools 的明确受控区域 | 覆盖或破坏编辑器拥有的 DOM 节点 |

### 3.2 元素类型与输出规则

| 场景元素 | DOM 输出 | 优先样式/动画技术 | 备注 |
|---|---|---|---|
| Text | `div`、`p`、`h1` 等 | CSS 字体、颜色、字距、行高、`transform`、`opacity` | 文本内容必须是可选择的 HTML 字符。 |
| Shape | inline `<svg>` 内的 `rect`、`ellipse`、`line`、`path`、`polygon` | SVG 属性、CSS、`transform` | 矩形、圆、箭头、多边形、星形和路径都不是图片。 |
| Image / Video | `img`、`video` | `object-fit`、裁切、透明度、transform | 资源通过项目 asset resolver 绑定，禁止保存机器绝对路径。 |
| Frame / Group | `div` 容器 | Absolute/Flex/Grid、padding、gap、约束 | Frame 是响应式布局和组件的基础。 |
| HTML Component | 受控宿主 `div` + 模板代码 | CSS variables、受控 Props | 代码组件边界内可以自行运行，但不参与任意反解析。 |
| Canvas / WebGL Effect | 模板已有 `canvas` 或受控 effect host | JavaScript / Canvas / WebGL | 仅用于粒子、复杂效果，不用于基本文字和形状。 |

### 3.3 节点标识与 DOM 所有权

每个可视节点必须使用稳定 `element_id`，材化时输出：

```html
<div data-topos-native-element="stable-element-id">...</div>
```

DOM 必须划分为以下所有权区域：

```text
document
├─ 模板作者拥有：template.html / component hosts
├─ 编辑器拥有：#topos-native-vector-overlay
│  ├─ data-topos-native-element="..." 的文字节点
│  └─ data-topos-native-element="..." 的 SVG 节点
└─ 临时交互拥有：#topos-native-draw-surface
```

`#topos-native-draw-surface` 只用于指针采样和绘制临时引导，不得写入项目 HTML；完成拖拽后创建 `SceneElement`，由场景刷新正式叠层。读取“作者 HTML”时必须排除两类编辑器拥有节点，避免把临时 DOM 误保存入模板。

---

## 4. 实施工作包与顺序

执行顺序不可跳跃：P0 工作包完成且验证后，才能扩大到后续能力。

### WP-0：稳定基线与验收护栏

优先级：P0；依赖：无。

| 工作项 | 实施内容 | 主要文件 | 验收与测试 |
|---|---|---|---|
| 现有变更收口 | 整理当前 HTML 原生叠层、浏览器绘制桥、工具栏、图层/参数面板改动；禁止引入第二个图层模型。 | `ui/motion_graphic_editor_window.py`、`ui/html_browser_preview.py`、`render/html_native_overlay.py` | HTML 场景可预览，纯 SVG 场景不回归。 |
| 路由一致性 | 主预览、缓存、导出均经 `scene_renderer_router.py` 选择正确 renderer。 | `render/scene_renderer_router.py` | HTML 场景无后端时得到结构化错误；不允许静默空白。 |
| 回归矩阵 | 固化 HTML、SVG、模板、工程读写、缓存、取消、窗口关闭测试清单。 | `tests/render/`、`tests/ui/` | 本文档第 10 节的 P0 测试全部通过。 |
| UI 稳定性 | MG 窗口在激活、切换应用、恢复 Dock 后保持前台可见；面板可由菜单恢复。 | `ui/motion_graphic_editor_window.py` | 手工验证：切到其他应用并切回，窗口不会落在主窗口后方。 |

完成定义：当前 MG 能稳定打开、关闭、保存、重开；浏览器预览和渲染链路可诊断。

### WP-1：网页原生元素闭环

优先级：P0；依赖：WP-0。

| 工作项 | 实施内容 | 数据变更 | 验收标准 |
|---|---|---|---|
| Text 节点完善 | 建立文字内容、字体、字号、粗细、行高、对齐、字距、颜色、透明度的参数 schema。 | `SceneElement.style/content` 保持 JSON 安全；先不升级 schema。 | 新建、修改、复制、保存后的文字均为 DOM 文本。 |
| Shape 节点完善 | 完成 rect、rounded_rect、ellipse、line、arrow、triangle、polygon、star、path 的材化与属性面板。 | 统一 `content.kind`；style 包含 fill/stroke/stroke_width/fill_opacity。 | 每种形状在 Chromium 和导出帧中一致。 |
| Browser 绘制桥 | 在 HTML 场景中通过透明交互层采样鼠标，发射形状类型和设计坐标；落笔后走既有场景 command。 | 无 | 拖入形状时背景粒子不断帧、不重载。 |
| 图层同步 | 画布选中、图层选中、Parameters 显示的元素 ID 必须一致。 | 无 | 点击图层和点击 DOM/SVG 选中同一个节点。 |
| 编辑器工具状态 | 工具栏显示选中状态、Escape 回到选择工具、光标随工具变化。 | 无 | HTML 场景和非 HTML 场景都能正确绘制/回退。 |

实施注意：不得把 SVG 先绘制为 `QImage` 再压入 HTML；`html_native_overlay.py` 是唯一的原生 DOM/SVG 材化出口。

完成定义：用户可在粒子网页背景上创建文字和基础 SVG 图形；它们可在 Layers 中管理、在 Parameters 中修改、在导出视频中出现。

### WP-2：统一属性面板与选中编辑

优先级：P0；依赖：WP-1。

| 区块 | 统一交互 | 具体实现 |
|---|---|---|
| Properties 控件 | 完全复用剪辑页面 Properties 的 QSS、滑条、数值框、下拉框、折叠 Section 和重置交互。 | 抽取或复用现有项目控件，不在 MG 中维护第二套样式。 |
| Transform | x/y、宽高、缩放、旋转、锚点、锁定比例。 | 数值范围按设计画布和属性类型限制，拖拽避免无穷大范围。 |
| Appearance | 填充、描边、圆角、透明度、阴影、模糊、混合模式。 | Color 使用项目取色器和吸管；颜色值可绑定 Token。 |
| Text | 内容、字体、字号、字重、对齐、行高、字距、文本溢出。 | 双击画布进入原地文本编辑。 |
| Layout | 位置模式、尺寸模式、约束、padding、margin、gap、对齐。 | 仅在容器/子元素的适用场景显示。 |
| Animation | 入场/出场、关键帧、缓动、循环、延迟。 | 先显示现有动画，WP-5 扩展。 |
| 多选 | 多元素共同字段可批量修改；不一致值显示混合状态。 | 变更必须成为一个 undo command。 |

完成定义：MG 所有控件与项目 Properties 视觉/键盘交互一致；无中文硬编码残留在英文界面。

### WP-3：Frame、布局与响应式设计

优先级：P0；依赖：WP-1、WP-2。

建议在本工作包开始时提升场景 schema，并在 `core/migration.py` 中提供向后迁移。新增字段必须保留未知未来字段。

| 能力 | 模型建议 | DOM/CSS 输出 | 验收标准 |
|---|---|---|---|
| Frame / Group | 新增容器元素类型或兼容的 `group` 语义；保存 children 顺序与 parent ID。 | `div` 容器。 | 子元素可随 Frame 一起移动、显隐、锁定。 |
| Absolute 布局 | `layout.mode = absolute`。 | `position:absolute`。 | 保持现有模板/场景兼容。 |
| Flex 布局 | `layout.mode = flex-row/flex-column`，方向、gap、padding、对齐。 | CSS Flexbox。 | 多个元素可自动排列和重新分布。 |
| Grid 布局 | `layout.mode = grid`，列/行模板、gap、对齐。 | CSS Grid。 | 卡片/数据模块可快速搭建。 |
| 尺寸策略 | 固定 px、百分比、fill、hug content。 | width/height/min/max。 | 改变父 Frame 尺寸后符合策略。 |
| 约束 | left/right/top/bottom/center/scale。 | 由 layout resolver 生成。 | 改画幅不导致标题全部偏位。 |
| 断点 | Desktop、Tablet、Mobile 的 override。 | `@media` 或运行时断点类。 | 16:9 与 9:16 可各自保存布局。 |

完成定义：用户能搭建一个具有容器、标题、数据卡片和按钮的响应式 MG 页面，不必逐个元素绝对定位。

### WP-4：样式 Token 与设计系统

优先级：P1；依赖：WP-2。

| 工作项 | 实施内容 | 验收标准 |
|---|---|---|
| Token 模型 | 在场景或模板 metadata 中增加 versioned `design_tokens`：color、typography、spacing、radius、shadow。 | Token 读写、克隆模板、工程保存都保持稳定。 |
| CSS Variables | 将 Token 编译为作用域 CSS 变量，如 `--topos-color-accent`。 | 修改 Accent 后所有绑定节点即时更新。 |
| 绑定与覆盖 | 元素属性可保存 `token_ref`，也可保存局部值覆盖。 | UI 清晰显示“继承 Token / 本地覆盖 / 恢复 Token”。 |
| 样式复制 | 支持复制/粘贴样式、应用到选中节点。 | 只复制允许的视觉属性，不覆盖内容/资源。 |
| 主题 | 模板可提供 light/dark 或品牌主题 Token 集。 | 一键切换主题而不改结构或动画。 |

完成定义：粒子模板的 accent、文字色、背景色、圆角可由少量 Token 统一控制。

### WP-5：时间线与网页动画

优先级：P1；依赖：WP-1，WP-3 建议完成。

| 工作项 | 实施内容 | 实现规则 |
|---|---|---|
| 属性关键帧 | position、scale、rotation、opacity、color、SVG path 等可产生关键帧。 | 沿用 `SceneAnimation`，补齐受支持属性清单和验证。 |
| 动画材化 | 简单 transform/opacity 优先用 CSS/WAAPI；复杂模板动画保持受控 JS。 | 不能在参数改动时重载网页。 |
| 缓动 | Linear、预设 ease、Bezier、Spring。 | 序列化为显式曲线数据，保证缓存/导出确定性。 |
| 页面与转场 | Page 默认时长、页面转场、转场时长合入 Parameters 的 Page 折叠区。 | UI 只保留一个 Apply 按钮，且应为橙色主操作。 |
| Timeline 控制 | 空格播放/暂停、上一页、下一页、循环、时间码、Marker。 | 所有用户向显示秒/时间码；帧数仅高级诊断使用。 |
| Marker 工具 | 添加、重命名、跳转、删除当前、清空全部。 | “Delete All Markers” 必须有确认并能撤销。 |

完成定义：一个文字和 SVG 图形的入场动画可在预览、缓存和导出中逐帧一致。

### WP-6：组件与模板生产体系

优先级：P1；依赖：WP-3、WP-4。

| 工作项 | 实施内容 | 主要位置 |
|---|---|---|
| 组件定义 | 选中 Frame/Group 可创建组件，暴露文本、色彩、图片、数字、布尔参数。 | `core/` 组件模型与 UI component library。 |
| 组件实例 | 组件实例引用定义并保存 override；断开链接后转为普通节点。 | 工程 JSON 中需有版本与兼容规则。 |
| 模板目录 | 一个文件夹一个模板：`manifest.json`、`template.html`、`preview.gif`、可选 `assets/`。 | `templates/motion_graphic/`。 |
| 模板加载 | 严格验证 manifest、相对路径和文件大小；无效模板单独诊断，不阻止其他模板。 | 模板库 loader / registry。 |
| GIF 悬停预览 | Effects → Video 内 MG Section 使用 `QMovie`，enter 播放，leave 复位。 | `edit_effects_panel.py` 与模板元数据。 |
| 用户/工程模板分层 | 内置模板、用户模板、工程内动态模板分开；工程载入不得清空内置模板。 | registry 需表明来源和优先级。 |

完成定义：用户复制模板目录到模板库后即可看到模板、查看 GIF、拖到时间线并编辑其暴露参数。

### WP-7：受控代码、Source 与 DevTools

优先级：P1；依赖：WP-1、WP-6。

| 工作项 | 实施内容 | 验收标准 |
|---|---|---|
| Source 标签 | 显示生成后的只读场景 DOM/CSS，以及明确的可编辑模板代码区。 | 文本不截断；Source 与 DevTools 为独立标签。 |
| Code Component | 提供带 Props schema 的受控 HTML/CSS/JS 组件宿主。 | 组件在编辑预览、缓存、导出使用同一运行时。 |
| DevTools | 默认暗色 Chromium DevTools；按钮采用短文本并完整可见。 | 打开、应用、丢弃有明确状态提示。 |
| DOM 应用策略 | 只导入可映射的编辑器拥有节点/属性；不支持的变更保留为代码组件或拒绝并说明。 | 绝不以任意 HTML 覆盖 `TeachingSceneDocument`。 |
| 快照 | 应用 DOM/CSS 前自动创建可撤销快照。 | 应用错误可以一键恢复。 |

完成定义：高级模板作者可调试和扩展代码，而普通用户不会因 DevTools 修改破坏图层和工程。

### WP-8：渲染质量、缓存与性能

优先级：P0；可与 WP-3/5 并行，但必须在公开使用前完成。

| 工作项 | 实施内容 | 验收标准 |
|---|---|---|
| 缓存编码预设 | Proxy、Balanced、High Quality 三档；高质量目标平均 20–40 Mbps（按分辨率/fps动态计算）。 | 缓存信息展示编码、实际平均码率、尺寸、时长和文件体积。 |
| 渐变/色带 | 为渐变提供 dither/noise 或高位深/高码率路线；允许模板改用纯色。 | 深色背景无明显分块或给出质量告警。 |
| 增量 DOM | 属性变化通过 DOM patch/CSS variable 更新，不调用整页 reload。 | 修改图层时 Canvas 粒子持续运行。 |
| 动画性能 | 首选 `transform` 与 `opacity`；避免逐帧读回、同步布局和不必要截图。 | 1080p 模板的实时预览保持可交互。 |
| 后端诊断 | 浏览器、GPU、缓存和编码失败显示根因及恢复动作。 | 不再只留下段错误或空白预览。 |
| 取消清理 | 取消缓存/导出、关闭编辑器时关闭会话并释放临时资源。 | 无后台 Chromium/编码任务泄漏。 |

完成定义：MG 视频缓存可选择高质量预设，导出与预览无语义偏差，且能定位性能/后端故障。

---

## 5. UI 信息架构目标

### 5.1 编辑器布局

```text
Menu bar: File | Edit | Scene | View | Preview | Export
Preview header: Select | Text | Rect | Rounded Rect | Ellipse | Line | Arrow | Polygon | Star | Path

Main area
├─ Chromium webpage preview / native scene preview
└─ Right inspector
   ├─ Parameters（始终第一位）
   ├─ Layers（固定在 Parameters 下方，可折叠/拖动高度）
   ├─ Scenes
   ├─ Source
   └─ DevTools

Bottom compact control strip
├─ Page tabs
├─ seek bar
└─ previous page | play/pause | next page | timecode | loop | marker menu
```

### 5.2 UI 规则

- Parameters 与 Page 参数属于同一个 Parameters 区，不再制造二级标签。
- Page、Color/Token、Transform、Layout、Animation 等全部是可折叠 Section；当前选中元素决定显示内容。
- Layers 不是主 Tab，而是 Parameters 下的持续上下文，避免“选完层再找参数”。
- 所有标题、元信息、工具提示默认透明背景，避免突兀的深色矩形；只有真正可点击 Tab/按钮才有背景状态。
- 标签使用项目现有面板合并样式；空间不足时优先缩短名称或使用 tooltip，不允许显示为 `...` 而失去可识别性。
- 所有可见文字使用 i18n key；英文 UI 中不得出现中文硬编码。
- 右侧面板隐藏后，菜单 `View → Show Parameters Panel` 必须可恢复；窗口激活时主动 `raise_()` / `activateWindow()`。

---

## 6. 数据迁移与兼容策略

| 变更类型 | 要求 |
|---|---|
| `TeachingSceneDocument` 字段新增 | 提升 `SCENE_SCHEMA_VERSION`，更新 `core/migration.py`、`validator.py`、序列化 round-trip 测试。 |
| 新元素类型 | 更新 `SUPPORTED_ELEMENT_TYPES`、validator、renderer、UI inspector 和迁移默认值。 |
| 新样式/布局字段 | 使用可选字段和安全默认值，旧工程继续以 Absolute 布局工作。 |
| Token/组件 | 采用 versioned metadata，不覆盖未知 metadata 字段。 |
| 模板 manifest | schema version 递增但 loader 兼容旧版本；未知字段保留或明确警告。 |
| 资源路径 | 仅保存相对路径、资产 ID 或可重建引用，禁止绝对用户目录和临时缓存路径。 |
| 工程恢复 | 项目模板注册不能清空内置/文件系统模板；来源必须可区分。 |

每一次 schema 变化必须附带：旧 JSON fixture、迁移测试、迁移后 validator 测试、再次保存后字段保留测试。

---

## 7. 性能与安全红线

| 红线 | 原因 | 执行方法 |
|---|---|---|
| 不在 UI 线程渲染/编码 | 会造成界面假死并触发 GPU/Chromium 竞争。 | 使用既有 HtmlRendererService、渲染任务队列和 command 信号。 |
| 不重载网页来更新单个属性 | 会停止 Canvas 粒子、视频和 JS 动画。 | 使用 CSS variables、`style` patch 或按 `element_id` 更新节点。 |
| 不用 QPainter 截图表达网页图层 | 会损失可编辑性、透明度和导出一致性。 | Text 输出 HTML；Shape 输出 inline SVG。 |
| 不信任自由 HTML | 模板、AI 和外部文件都可能含危险内容。 | 模板 HTML 经现有安全管线；native overlay 严格 escape/白名单。 |
| 不反向解析任意页面 | HTML/CSS 可表达能力远超过编辑器模型，强行解析会丢语义。 | 只双向同步编辑器拥有节点；其余进入 Code Component。 |
| 不重复压缩缓存 | 深色渐变和粒子会出现严重色带/块效应。 | 保留中间缓存或提高码率/编码等级，展示真实码率。 |

---

## 8. 交付节奏与里程碑

| 里程碑 | 包含工作包 | 可交付结果 | 上线门槛 |
|---|---|---|---|
| M1：网页图层闭环 | WP-0、WP-1 | 文本与 SVG 图形可在 HTML 背景上创建、选择、保存、导出。 | 核心 render/UI tests 通过；粒子不停止。 |
| M2：专业编辑体验 | WP-2 | 参数、图层、工具栏、语言、窗口恢复达到项目 Properties 一致性。 | 英文/中文 UI 人工检查；无控件截断。 |
| M3：可设计的网页页面 | WP-3、WP-4 | Frame/Flex/Grid、响应式、Token 与主题。 | 16:9/9:16 样例都可用。 |
| M4：可动画、可交付 | WP-5、WP-8 | 关键帧、时间线、统一视频缓存与高质量输出。 | 预览/缓存/导出逐帧抽样一致。 |
| M5：可扩展生态 | WP-6、WP-7 | 组件、模板目录、GIF 预览、受控代码组件、DevTools 工作流。 | 模板复制迁移与回滚完整。 |

推荐执行顺序：**M1 → M2 → M3 → M4 → M5**。不要先做任意 HTML 双向编辑或复杂路径编辑；它们会在基础布局、数据模型和渲染一致性未稳定时放大维护成本。

---

## 9. 每个工作包的实施检查表

每个 PR/提交必须逐项满足：

- [ ] 先确认受影响的场景模型、renderer、UI、工程保存和测试调用点。
- [ ] 修改通过场景 command/service，而非只修改 QWidget 或浏览器 DOM。
- [ ] 新函数具备完整类型注解；外部数据经过验证。
- [ ] UI 程序化更新有状态锁，避免递归信号。
- [ ] 新的 scene/template schema 有版本、迁移和 backward fixture。
- [ ] HTML/SVG 输出有转义、白名单和 path/颜色/字体等字段校验。
- [ ] 修改浏览器预览后验证 DOM patch 不触发页面 reload。
- [ ] 修改渲染后验证 prepare、render、cancel、cleanup 与无 Chromium 后端失败路径。
- [ ] 修改 UI 后检查英文和中文 locale，键盘操作与 tooltip。
- [ ] 为行为变更增加 focused tests，并执行相应渲染/UI 回归集。
- [ ] `git diff --check` 无空白错误；不夹带无关用户修改。

---

## 10. 测试与验收矩阵

| 类别 | 必测场景 | 建议测试位置 |
|---|---|---|
| 场景模型 | Text/Shape/Frame/Token/Animation 序列化与迁移。 | `tests/core/` 或既有 teaching-animation model tests。 |
| DOM/SVG | 文本输出为 HTML；所有形状输出有效 inline SVG；危险属性被拒绝/转义。 | `tests/render/test_motion_graphic_vector_shapes.py`。 |
| HTML 渲染路由 | HTML 场景使用 Chromium adapter；非 HTML 场景仍使用 Svg renderer；无后端失败可诊断。 | `tests/render/test_scene_renderer_router.py`。 |
| 浏览器预览 | 叠层添加、更新、移除不 reload 页面；绘制 bridge 坐标正确。 | `tests/ui/test_editor_html_preview.py`。 |
| 命令/撤销 | 创建、移动、样式修改、删除 Marker、组件 override 可 undo/redo。 | `tests/ui/test_motion_graphic_manual_controls.py` 及 command tests。 |
| 工程兼容 | 旧场景/模板打开、迁移、再次保存、未知字段保留。 | migration/project IO tests。 |
| 模板库 | manifest 无效、路径越界、缺 preview、重复 id/version、复制目录导入。 | template loader tests。 |
| 缓存质量 | 预设码率、输出尺寸、帧数、平均码率信息、取消与清理。 | render/export integration tests。 |
| UI | Parameters/Layers 位置、标签完整显示、深色 DevTools、面板恢复、窗口前台。 | pytest-qt + 手工 smoke checklist。 |
| 端到端 | 粒子 HTML 背景 + SVG + 文本 + 动画 → timeline cache → final export。 | `tests/render/test_html_s0_end_to_end.py` 或新增 e2e fixture。 |

建议每次涉及上述模块的合并前至少执行：

```bash
./.venv/bin/python -m pytest \
  tests/render/test_scene_renderer_router.py \
  tests/render/test_html_s0_end_to_end.py \
  tests/render/test_motion_graphic_vector_shapes.py \
  tests/ui/test_motion_graphic_manual_controls.py \
  tests/ui/test_editor_html_preview.py -q
git diff --check
```

---

## 11. 首个可交付样例：粒子流光背景

现有“粒子流光背景”应作为本路线的基准样例，而不只是一个单独模板。

| 层 | 实现要求 |
|---|---|
| 背景 | `template.html` 中的 Canvas 粒子、深色纯色/低风险背景和可控 accent；避免造成严重色带的高对比渐变。 |
| 参数 | Accent、Particle Density、Motion Speed 作为模板 Token/参数，可通过 Parameters 修改。 |
| 叠层 | 新建标题、数据卡片、箭头和形状均以 DOM/SVG 放在 Canvas 上方。 |
| 动画 | Canvas 粒子一直运行；叠层的出现/离场由时间线动画控制。 |
| 缓存 | 生成视频缓存时浏览器中的 Canvas 与 SVG/DOM 同帧捕获；高质量预设验证细小粒子和深色背景。 |
| 迁移 | 模板文件夹可复制到另一台机器，包含 manifest、HTML、GIF 与所需资源。 |

此样例通过后，再扩展数据图表、字幕标题、产品卡片和多页讲解模板。它将验证整个“网页优先 MG 编辑器”架构，而不只是验证单一 UI。

---

## 12. 后续评审节奏

每完成一个里程碑进行一次架构评审，确认以下问题后再继续：

1. 场景模型是否仍是唯一真相，是否出现了 UI/DOM/项目保存三处数据不一致。
2. HTML 场景是否在 preview、cache、export 走同一条语义链路。
3. 新增字段是否已有迁移、校验和工程兼容测试。
4. 所有实时修改是否为增量更新，是否导致背景动画、视频或浏览器状态重置。
5. 是否有功能应留给 Code Component，而不是错误地扩张为原生绘图工具。
6. 该里程碑是否已经满足可用性、性能、稳定性和多语言验收，而非只完成视觉 UI。

只有上述检查通过，才进入下一阶段。这样 MG 编辑器会稳定演进为网页设计与视频交付融合的工具，而不会变成两套相互竞争的渲染/数据系统。

---

## 13. 实施进度记录（2026-08-21，fix-ui 分支）

以下工作包已在 WP-0 基线（606faeb2）之上按顺序落地，每个提交独立可回归：

| 工作包 | 提交 | 交付内容 |
|---|---|---|
| WP-1 收尾 | `1c4e2f0f` | 浏览器内点击 native 文本/SVG 选中同一图层；Escape 回到选择工具；选中高亮不误报 DevTools 脏状态。 |
| WP-2 | `33a6d9cc` | Text 排版参数（字号/字重/对齐/行高/字距）、Appearance（透明度/模糊/混合模式/阴影）、Transform 宽高；滑条手势=一条撤销命令；双击原地编辑文本；状态栏全部 i18n 化。 |
| WP-3 | `db651e9f` | Schema v3：`frame` 元素 + `layout` 映射（flex-row/column/grid/absolute、gap/padding/justify/align、尺寸策略）；迁移 v2→v3；overlay 将 frame 物化为 CSS 容器并嵌套子层；SVG 渲染 frame 背板；Layers 新增 Frame；Inspector Layout 区块。 |
| WP-4 | `49f0c87b` | 场景级 Token 存储 + 主题叠加；overlay 解析 `$palette.*` 引用并注入 `--topos-*` CSS 变量；Color Token 绑定/本地覆盖 UI；Copy/Paste Style；Scene > Theme 菜单（可撤销）。 |
| WP-5 | `ba5cfb17` | native 层关键帧动画物化为可擦洗的 paused CSS（采样与渲染器共用 get_value）；时间线每帧只写一个 `--topos-time` 变量；捕获路径逐帧 evaluate 后物化（预览=缓存=导出）；Remove All Markers 确认+可撤销。 |
| WP-6 | `057e2645` | 模板包尺寸上限（manifest 256K/HTML 4M/GIF 32M）；DetachComponentInstanceCommand（覆写烘焙+断开链接，单条撤销）；修复两个预存模板测试失败。 |
| WP-7 | `f2e31650` | Source 标签新增只读"生成的场景输出"视图（DOM overlay + token CSS + 动画 CSS），与可编辑模板源码同屏。 |
| WP-8 | `542cd951` + `e7eeb3b5` | 缓存质量三档（Proxy 8M / Balanced 20M / High Quality 40M @1080p，按像素面积缩放）；设置持久化并由 worker 尊重；编辑器 Preview > Cache Quality 菜单；提交状态显示档位与目标码率；模板参数值 script 剥离 + slots 保序。 |

### 验收状态

- 计划第 10 节 P0 矩阵（router / S0 e2e / vector shapes / manual controls / html preview）：**83 passed**。
- 广泛回归（render + editor html + cache + migration/validator/hierarchy 选择集）：**1053+ passed**；`tests/render/html_layer` 全部 35 通过。
- 已知预存失败（与 MG 无关、干净树同样失败）：`tests/composite` 的 2 个 schema 版本断言（COMPOSITE_SCHEMA_VERSION 已被提升至 10 而测试仍期待 9）。

### 后续建议（下一里程碑）

1. WP-3 剩余：断点（Desktop/Tablet/Mobile override）与约束（left/right/center/scale）尚未落地；建议随 9:16 画幅样例一起做。
2. WP-5 剩余：Spring/Bezier 缓动的 CSS 精确映射（当前采样式 CSS 已保证语义一致）。
3. 第 11 节"粒子流光背景"基准样例的端到端验收（HTML 背景 + SVG + 文本 + 动画 → 缓存 → 导出）应在真实工程上人工过一遍。
4. composite 的 2 个预存测试失败建议单独修复（提升版本时未同步测试断言）。
