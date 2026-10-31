# Motion Graphic 网页式编辑器剩余优化计划

审计日期：2026-08-21
审计分支：`fix-ui`
审计对象：当前工作区 HEAD `1392e4a1` 及其之前的 MG 编辑器实现

本计划只记录**尚未完成、部分完成、需要真实设备验证或被测试暴露的问题**。已经通过测试并达到可用状态的工作不再重复拆解，避免后续开发反复改动稳定区域。

---

## 1. 本次真实测试结论

### 1.1 自动测试

| 测试范围 | 结果 | 结论 |
|---|---:|---|
| HTML/SVG/DOM/模板/UI 主回归集 | **341 passed, 1 skipped** | 当前网页原生图层、形状材化、模板、参数、DevTools 面板基础功能没有发现回归。 |
| MG 预设库/手动控制/HTML 模板补充集 | **69 passed** | 预设库、GIF、分类搜索、模板保存、参数控制、页面参数和图层交互通过。 |
| Teaching Animation phase/registry/retime 集 | **86 passed** | 场景模型、HTML 导入、生成器 registry、重定时、AI phase 相关链路通过。 |
| 真实窗口预设库 E2E | **7/7 passed** | 13 个预设、6 个分类、GIF、双击插入、真实 QDropEvent、undo/redo、GIF 另存、ZIP 导入导出通过。 |
| `tests/render/html_layer` | **35 passed** | HTML 安全、模板 promote/apply 和 patch 链路通过。 |
| 完整 `tests/composite` | **未通过** | 先复现到 3D Polygon 按钮状态断言失败；继续运行时出现 Qt 清理阶段 `Segmentation fault`。这是项目级问题，不应标记为 MG 通过。 |

### 1.2 真实窗口 E2E 的边界

预设库 E2E 使用了真实 `QApplication`、真实窗口、真实 QDropEvent、真实 QMovie 和真实文件 ZIP；但运行在 `--offscreen` 模式。以下内容仍未完成真实 macOS 桌面验收：

- 切换到其他应用再切回时 MG 窗口是否始终位于前台。
- 拖动右侧 splitter 隐藏 Parameters 后，菜单恢复、窗口激活和 Dock 状态是否符合用户预期。
- Qt WebEngine DevTools 的真实 Elements/Console/Sources 操作与 Apply/Discard 往返。
- 粒子流光 HTML 模板在真实 Chromium 中添加 SVG/文字后仍持续运动。
- 1080p/4K 实际视频缓存的文件码率、色带和视觉质量。

---

## 2. 已完成能力清单（冻结，不再重复开发）

以下能力已经有代码和测试支撑，后续只允许修复回归，不再重新设计基础架构：

- `TeachingSceneDocument` 作为场景唯一真相，schema v3 与 v2→v3 迁移。
- Text、基础 Shape、Frame 的 DOM/inline SVG 材化。
- HTML 浏览器预览中的 native draw surface、原生元素选择、Escape 回选择工具。
- Parameters 与 Layers 的纵向布局、统一控件、英文/中文基础界面、页面参数折叠。
- Flex Row、Flex Column、Grid、Absolute 基础布局及固定/fill/hug 尺寸策略。
- 场景 Token、主题叠加、Token 绑定、本地覆盖、Copy/Paste Style。
- 基础关键帧的预览/缓存/导出采样一致性和 Remove All Markers。
- 模板目录加载、安全路径检查、大小限制、GIF 悬停预览。
- 结构化 MG Preset Library：分类、搜索、GIF、拖拽、双击插入、另存、ZIP 迁移。
- Source 的生成场景输出视图、DevTools Open/Apply/Discard 基础工作流。
- Proxy/Balanced/High Quality 缓存档位与 8/20/40 Mbps 目标码率配置。
- 主要渲染路由、HTML 导入、工程保存与现有缓存桥接。

---

## 3. 剩余工作总览

| 编号 | 剩余项目 | 优先级 | 当前状态 | 依赖 |
|---|---|---|---|---|
| R1 | 响应式断点与约束布局 | P0 | 未完成 | WP-3 基础布局已完成 |
| R2 | 动画属性覆盖与 Bezier/Spring 精确一致性 | P0 | 部分完成 | 现有关键帧材化 |
| R3 | 网页元素覆盖完整化（图片、视频、SVG、图表、公式、路径） | P0 | 部分完成 | R2、资产解析 |
| R4 | HTML 模板与结构化 Preset Library 的统一管理 | P0 | 部分完成 | 当前两套库已各自可用 |
| R5 | MG 全量多语言和模板名称本地化 | P0 | 部分完成 | 当前部分面板已 i18n |
| R6 | 粒子流光模板真实 Chromium/视频端到端验收 | P0 | 未完成 | R2、R3 |
| R7 | 实际缓存文件码率、色带和质量验收 | P0 | 部分完成 | 编码器和 ffprobe |
| R8 | DevTools/Source 受控双向代码工作流 | P1 | 部分完成 | R3、所有权协议 |
| R9 | 组件生产、实例化和代码组件参数化 | P1 | 部分完成 | R4、R8 |
| R10 | 窗口焦点、Dock 恢复和 macOS 手工稳定性 | P0 | 自动测试不足 | 真实桌面环境 |
| R11 | 项目级 Composite 回归失败与段错误 | P0 | 已复现，未修复 | Composite 团队/Qt GPU 环境 |
| R12 | 清洁机器、打包资源和发布验收 | P1 | 未完成 | R4、R6、R7 |

---

## 4. R1：响应式断点与约束布局

### 当前缺口

当前场景支持 Frame、Flex、Grid 和基本尺寸策略，但还没有完整的 Desktop/Tablet/Mobile override，也没有 left/right/top/bottom/center/scale 约束。改变画布比例时，用户仍可能需要手工重新摆放元素。

### 实施任务

| 任务 | 具体落地 |
|---|---|
| 断点模型 | 在 `scene.metadata` 或 versioned canvas payload 中增加 `breakpoints`：名称、最小宽度、最大宽度、画布尺寸。 |
| 属性覆盖 | 元素 layout/style 允许按 breakpoint 保存 override；没有 override 时继承基准值。 |
| 约束模型 | 支持 `left/right/top/bottom/center_x/center_y/scale/keep_aspect`，并规定冲突优先级。 |
| 解析器 | 新建纯函数 layout resolver，根据基准画布、断点、父 Frame 和约束计算最终 SceneTransform。 |
| DOM 输出 | 允许编译为 CSS media query；对需要视频帧确定性的缓存路径使用同一解析结果。 |
| UI | Parameters 增加 Layout Mode、Breakpoint、Constraints 折叠区；提供 16:9/9:16/1:1 快速预览。 |
| 迁移 | 旧 schema 默认 `absolute`、无断点、无约束，不改变已有项目画面。 |

### 验收

- 同一场景在 1920×1080、1080×1920、1080×1080 下保持标题、卡片和按钮在安全区内。
- 修改断点下的属性只影响该断点。
- 预览、缓存、导出使用同一个解析结果。
- 断点切换、约束修改均可撤销/重做。

---

## 5. R2：动画属性覆盖与 Bezier/Spring 精确一致性

### 当前缺口

当前 `native_animation_css()` 主要覆盖 x、y、width、height、opacity、scale 和 rotation；颜色、填充、描边、模糊、阴影、SVG path 等属性尚未完整材化。已有 Bezier 轨道在 native CSS 中退化为 `linear`，而 Spring 主要通过采样后的关键帧表达，尚未成为可检查的统一动画语义。

### 实施任务

| 任务 | 具体落地 |
|---|---|
| 动画属性注册表 | 将可动画属性集中定义：transform、opacity、fill、stroke、font_size、blur、shadow、path 等，并声明 DOM/SVG 输出方式。 |
| CSS 颜色动画 | 对合法颜色值生成 CSS keyframes 或逐帧 CSS variable；保证透明度与颜色空间策略明确。 |
| SVG Path 动画 | 只允许相同命令结构的 path 插值；结构不同则回退为离散切换并发出诊断。 |
| Bezier 映射 | 将 `Keyframe.bezier_in/out` 或绝对 handle 转换为 CSS `cubic-bezier`；无法表达的 overshoot 采用确定性逐帧采样，并记录 fallback 原因。 |
| Spring 语义 | 明确 Spring 是高层 motion source 还是烘焙轨道；保存参数、settle frame、overshoot policy，preview/export 使用同一 evaluator。 |
| 时间线编辑 | 在时间线或 Inspector 显示 easing/spring 类型与参数，禁止只显示一串无法解释的采样 keyframe。 |
| 性能 | 静态轨道不注入 CSS；高密度采样做 compact，但不得改变像素语义。 |

### 验收

- 对 linear、ease、Bezier、hold、Spring 各生成一组 golden frame，预览与 Chromium capture 逐帧误差在明确阈值内。
- Bezier 不再无条件走 linear fallback。
- Spring 的 settle frame、overshoot 和导出帧数在保存/重开后不变。
- fill、stroke、path、blur 至少各有一条真实 HTML/SVG 动画 E2E。

---

## 6. R3：网页元素覆盖完整化

### 当前缺口

原生 overlay 当前稳定支持 text、shape、frame；image、video、svg、chart、formula、draw 等 SceneElement 类型不能全部在 HTML 场景中直接生成对应 DOM。它们仍可能只在纯 SVG SceneView 中表现正常，形成“编辑器能看、网页导出缺失”的风险。

### 实施任务

| 元素 | 目标实现 |
|---|---|
| Image | 输出 `<img>`，通过 `asset_ref` 解析项目资源，缺失时显示明确 placeholder/诊断。 |
| Video | 输出受控 `<video>`，明确 currentTime、循环、静音、跨域和缓存捕获策略。 |
| SVG | 保留可信 inline SVG；非可信 SVG 走 sanitizer，不允许属性注入。 |
| Formula | 在 HTML 场景选择 MathML、受控 SVG 或预渲染数学节点，并保持可编辑内容。 |
| Chart | 将结构化 chart 数据生成 SVG/HTML，参数和动画走场景模型。 |
| Draw / Path | 统一为 SVG Path 节点；补节点编辑、闭合、描边和路径动画。 |
| Layer ordering | 明确 HTML authored content、native overlay、媒体和临时 draw surface 的层级。 |

### 验收

- 每个 `SUPPORTED_ELEMENT_TYPES` 都有 HTML scene fixture、DOM 结构断言和至少一帧导出断言。
- 缺失资产不会产生空白或静默丢层；诊断能指出 asset ID 和修复动作。
- 页面脚本、粒子和视频在插入上述元素后继续运行。

---

## 7. R4：统一 HTML 模板与结构化 Preset Library

### 当前缺口

项目当前存在两套可复用内容：

- `templates/motion_graphic/<template>/`：`manifest.json + template.html + preview.gif`，用于 HTML/CSS/Canvas 生成器。
- `presets/library/<category>/<preset>/`：`build.py/preset.json + scene.json + preview.gif`，用于结构化 SVG/文字预设。

两套库都能工作，但用户会看到两个概念、两套加载/保存/迁移入口；“每个模板一个文件夹、统一管理”尚未真正统一。

### 实施任务

| 任务 | 具体落地 |
|---|---|
| 统一 manifest | 定义 `mg_package` schema，声明 `kind: html | scene | hybrid`、参数、预览、资源、版本和兼容能力。 |
| 统一 registry | Effects、MG Editor Library、AI capability context、Save/Apply Template 全部查询同一个 registry。 |
| 统一预览协议 | 两类包都暴露 preview.gif；缺失时阻止发布或生成一次明确的 placeholder。 |
| 统一拖拽协议 | HTML 模板和结构化预设都使用相同的 MG payload，明确 drop 后创建 HTML layer 还是 SceneElement。 |
| 统一保存 | Save as Template / Save as Preset 合并为一个入口，用户只选择“HTML 模板”或“结构化网页场景”。 |
| 迁移 | 现有 HTML 包和 preset 包自动转换到 v2 manifest；旧目录继续只读兼容。 |
| 冲突策略 | `template_id + version + origin` 明确内置、用户、工程来源和优先级。 |

### 验收

- Effects、Library、AI 能看到同一份模板清单，不出现一边可用一边不可用。
- 复制一个目录即可迁移；导入后参数、预览、资源和版本完整。
- 内置模板不会因工程恢复 `registry.clear()` 被清掉。

---

## 8. R5：MG 全量多语言与模板名称本地化

### 当前缺口

部分编辑器标签已使用 i18n，但 Effects 的 MG Section 仍存在硬编码 `MG 生成器`、中文双击提示，粒子模板 manifest 只有中文 `display_name/description`。在英文模式下，MG 内容不一定全部英文。

### 实施任务

| 任务 | 具体落地 |
|---|---|
| Manifest locale | 支持 `display_name_i18n`、`description_i18n`，至少包含 `zh_CN` 和 `en_US`；保留 display_name 作为 fallback。 |
| Effects | Section 名称、tooltip、info/status、拖拽提示全部使用 locale key 或模板本地化字段。 |
| Library | 分类名称、预设名称、标签和描述支持 locale fallback。 |
| 参数 | 参数 label、description、单位、错误消息走 i18n；保留稳定的内部参数名。 |
| 测试 | `en_US` 下扫描 MG 可见文本，禁止中文硬编码；`zh_CN` 下检查中文 fallback 正常。 |

### 验收

- 英文模式进入 Effects、MG Editor、Library、DevTools、Page Parameters，所有用户可见文本完整英文。
- 切换语言不改变 template_id、参数名或项目保存结构。
- 缺少某个模板翻译时只回退该模板默认语言，不影响整个面板。

---

## 9. R6：粒子流光背景真实端到端验收

### 当前缺口

当前真实窗口 E2E 验证的是结构化预设库，不是 HTML 粒子流光背景。首个基准样例“粒子 HTML 背景 + SVG + 文本 + 动画 → 缓存 → 导出”仍没有完整真实链路证据。

### 实施任务

新增 `scripts/e2e_mg_particle_flow.py` 和对应测试/报告，必须完成：

1. 从 `particle-flow-background` manifest 加载真实 HTML 模板。
2. 在真实 `HtmlBrowserPreview` 中确认 Canvas 已创建且动画帧持续变化。
3. 添加文字、矩形、圆形和一条关键帧动画，不触发整页 reload。
4. 读取两个时间点的 Canvas/DOM 截图或像素摘要，证明粒子不是静帧。
5. 导出至少 3 秒 1920×1080 WebM/MP4，抽取首/中/尾帧。
6. 检查 SVG/HTML 图层存在、动画进度正确、背景粒子仍存在。
7. 保存、关闭、重开工程，再重复抽样。

### 验收

- 粒子背景在加图形、调参数、切页、拖时间线时不冻结。
- 预览、缓存和导出抽样帧结构一致。
- 真实 Chromium 可用时测试必须执行；不可用时只能明确 skipped with reason，不能默认为 passed。

---

## 10. R7：实际缓存码率、色带和视觉质量

### 当前缺口

代码已经设置 8/20/40 Mbps 目标和最低码率，但当前测试主要验证计算值和 encoder 参数，没有对真实生成的 MG 缓存文件做 `ffprobe` 平均码率、帧数、时长和视觉质量验收。

### 实施任务

| 任务 | 具体落地 |
|---|---|
| 实际码率探针 | 缓存完成后读取 container duration、bit rate、video stream bit rate、frames、codec、pixel format。 |
| 目标误差 | 对 1080p High Quality 设定 40 Mbps 目标和可接受误差；Balanced 20 Mbps；Proxy 8 Mbps。 |
| 低复杂度画面 | 粒子/纯色场景容易被编码器压到很低，必须验证是否执行 minimum bitrate padding 或报告实际有效码率。 |
| 色带检测 | 对深色背景生成固定测试帧，计算相邻色阶/块状伪影指标，并保存人工对比图。 |
| 分辨率缩放 | 验证 720p、1080p、4K 的面积缩放目标不会超过合理上限。 |
| 缓存状态 | UI 显示目标码率与实际码率；缓存 manifest 记录两者。 |

### 验收

- 每档质量至少有一个真实文件报告，而不是只有函数断言。
- High Quality 1080p 实际平均码率和目标关系可解释；若因内容复杂度低而低于目标，必须有 minimum policy 证据。
- 纯色/渐变/粒子三类样例均有人眼检查记录。

---

## 11. R8：DevTools 与 Source 的受控双向工作流

### 当前缺口

Source 现在能显示生成场景输出，DevTools 也具备 Open/Apply/Discard；但“编辑器拥有 DOM”和“模板作者拥有 HTML”之间的细粒度冲突策略还不够明确，Apply 仍可能以整份 authored DOM 替换 HTML layer，而不是把可映射属性安全回写到场景模型。

### 实施任务

- 为 generated overlay、authored HTML、temporary draw surface 建立 DOM ownership contract。
- Apply 时只读取模板作者拥有区域；native overlay 继续从 `TeachingSceneDocument` 生成。
- 对 DOM/CSS 可映射变更生成 `SetElementPropertyCommand`；不可映射内容保留为 Code Component 或显示拒绝原因。
- DevTools Apply 前创建场景快照；Discard 恢复浏览器与保存层。
- Source 面板区分“Generated Scene Output（只读）”与“Template Source（可编辑）”。
- Code Component 增加 Props schema、参数校验、版本和安全 profile。

### 验收

- 在 DevTools 修改模板 CSS，不会删除 native SVG/文字图层。
- 在 DevTools 修改 native overlay，不会被错误写回 authored HTML。
- Apply、Discard、Undo/Redo、保存/重开后状态可预测。
- 代码组件在预览、缓存、导出使用相同 Props 和脚本安全策略。

---

## 12. R9：组件生产与网页组件体系

### 当前缺口

当前已经有 ComponentMaster、ComponentInstance、Override、Variant 和 Detach 命令，但需要进一步验证完整用户路径：选择 Frame/Group → 创建组件 → 暴露参数 → 插入实例 → 修改实例 → 修改 Master → 断开实例。

### 实施任务

- 从选中 Frame/Group 创建组件时自动记录拥有的元素、Token 引用和可暴露属性。
- 组件库显示真实预览、参数 schema 和版本。
- 组件实例在 Layers 中显示来源与 override 状态。
- Master 更新传播到实例；实例局部覆盖不被覆盖。
- Detach 后场景变为自包含元素，且只有一条 undo command。
- 组件可保存为统一 MG package，与 R4 registry 共用。

### 验收

- 组件完整生命周期通过真实窗口 E2E。
- 组件中包含粒子背景 + DOM/SVG 叠层时，实例化不会冻结脚本。
- 工程重开、模板迁移和 detach 后不丢元素或动画。

---

## 13. R10：窗口焦点、Dock 恢复和 macOS 手工稳定性

### 当前缺口

当前设置了 `WindowStaysOnTopHint`、`raise_()`、`activateWindow()` 和面板恢复入口；offscreen 测试只能证明调用路径，不能证明 macOS 窗口管理器中的实际行为。E2E 日志也显示 offscreen 下 Qt 不支持 raise/propagateSizeHints，这是环境限制，不等于桌面验证通过。

### 手工测试脚本

在 macOS 真正显示环境执行：

1. 从时间线选择 MG Clip，打开编辑器。
2. 双击参数、调节滑条、切换 Parameters/Layers。
3. 切到 Finder、浏览器和另一款编辑器，再切回 Topos。
4. 用 splitter 拖到隐藏 Parameters；使用菜单 View → Show Parameters Panel 恢复。
5. 关闭并重新打开编辑器，验证尺寸、分割比例、当前页面和时间码。
6. 打开 DevTools，再切换应用，确认不会跑到主窗口后面。
7. 在英文模式和中文模式各执行一次。

### 验收

- 记录真实 macOS 版本、Qt 版本、GPU、窗口状态截图和复现步骤。
- 若 `WindowStaysOnTopHint` 妨碍用户切换其他窗口，改为 owner/activation 策略，不以永久置顶掩盖焦点问题。
- Dock state 必须可恢复且不损坏主窗口布局。

---

## 14. R11：项目级 Composite 回归与段错误

这不是 MG 新功能缺失，但它会影响“真实测试整套项目”的可信度，必须单独修复或隔离。

### 已复现问题

1. `tests/composite/test_3d_phase6_components_snap.py::TestComponentModeUI::test_point_edge_polygon_disabled` 失败：测试认为 Polygon 应禁用，但当前 `viewport3d_toolbar.py` 已将 Polygon 设为 enabled。
2. `tests/composite/test_global_3d_scene_settings.py::test_bumps_schema_version_to_9` 等断言仍期待 schema 9，而当前 `src/features/composite/core/graph_migration.py` 为 10。
3. 完整 composite 回归继续执行时，在 `composite_page.py` / pytest-qt 事件处理阶段复现 `Segmentation fault`。

### 处理方案

- 先确认 Polygon 是已交付功能还是测试陈旧；若已交付，更新测试并补面级 picking 回归；若未交付，恢复禁用状态。
- 将所有 Composite schema 断言统一到当前迁移版本，并增加 v9→v10 fixture。
- 单独建立 Qt/GPU smoke job，定位 composite_page 生命周期、WebEngine/OpenGL 上下文和 fixture teardown 的资源释放问题。
- 在该段错误未定位前，完整项目测试报告必须标记为“MG 通过、项目全量未通过”。

---

## 15. R12：清洁机器与发布验收

### 当前缺口

开发环境已有模板 HTML、JSON、GIF 和预设目录，但还没有在没有 Python 缓存、没有本地浏览器 profile、没有用户模板的干净环境上执行完整验收。

### 实施任务

- 构建 wheel/app package，检查 `templates/motion_graphic/**/*`、locales、preset preview 是否都进入发布包。
- 在干净临时用户目录启动，确认内置模板、GIF、locale 和 renderer 能用。
- 复制一个第三方模板目录，验证路径安全、大小上限、无效 manifest 诊断和刷新。
- 打开旧 schema 工程，迁移、保存、重开、缓存、导出。
- 记录 Chromium、FFmpeg、字体和 GPU 能力诊断。

### 验收

- 无开发目录引用、无绝对机器路径、无缺失 GIF/HTML/JSON。
- 清洁环境完成 R6 粒子 E2E 和 R7 实际码率报告。
- 发布包可恢复失败原因，而不是显示空白画布或静默丢模板。

---

## 16. 推荐执行顺序

```text
R11 项目级崩溃隔离
        ↓
R1 响应式/约束 + R3 元素覆盖
        ↓
R2 动画精确一致性
        ↓
R4 模板统一 + R5 多语言
        ↓
R6 粒子真实 E2E + R7 实际码率/色带
        ↓
R8 DevTools 所有权 + R9 组件生产
        ↓
R10 macOS 手工稳定性 + R12 清洁发布验收
```

优先做 R11 的原因是：项目级段错误会污染后续所有真实测试结果。随后先补网页设计器的响应式、元素覆盖和动画语义，再做模板/代码生态；只有这样，粒子模板的缓存码率和 DevTools 往返测试才具有代表性。

---

## 17. 每个剩余项目的交付门槛

每个 R 项目合并前必须同时提供：

- [ ] 影响文件和数据流说明。
- [ ] schema/manifest 变化、版本和迁移策略（如适用）。
- [ ] 纯逻辑 focused tests。
- [ ] Qt/UI 或浏览器测试。
- [ ] preview/cache/export 一致性测试（如涉及渲染）。
- [ ] cancel、failure、close、missing asset 路径验证。
- [ ] 英文/中文 i18n 验证。
- [ ] `git diff --check` 和相关回归集结果。
- [ ] 对真实浏览器/GPU/桌面行为明确标注“passed / skipped with reason / manual pending”。

最终完成标准不是“所有模块都有代码”，而是：网页节点、参数、动画、模板、浏览器运行时和视频缓存都由同一份场景语义驱动，并且在真实工程中可复现、可迁移、可恢复。
