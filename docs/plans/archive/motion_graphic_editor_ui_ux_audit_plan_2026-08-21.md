# Motion Graphic 编辑器全量 UI/UX 审计与落地实施计划

审计日期：2026-08-21
审计分支：`fix-ui`
审计对象：`src/features/teaching_animation/ui/` 下的 Motion Graphic 编辑器，以及 Effects 面板、时间线和 HTML 预览的交互入口
审计基线：HEAD `1392e4a1`
计划性质：本文件是下一阶段的 UI/交互实施计划；本次只做审计和计划记录，不在本轮修改业务代码。

相关技术计划：

- [MG 编辑器实施总计划](motion_graphic_web_editor_implementation_plan_2026-08-21.md)
- [剩余技术优化计划](motion_graphic_web_editor_remaining_optimization_plan_2026-08-21.md)

---

## 0. 结论先行

当前 MG 编辑器的底层交互链路已经比早期版本完整：真实 Qt 控件可以创建文字、形状和 Frame，可以编辑参数、嵌套图层、绑定 Token、添加关键帧、撤销/重做、保存加载，并且预设库已经可以用 GIF 预览、双击插入和拖拽插入。

这次审计发现的主要问题已经从“有没有功能”转移到了“用户能不能马上看懂、能不能知道当前处于什么模式、操作后能不能确认结果”。优先级最高的不是再增加按钮，而是建立清晰的工作模式、明确的选择上下文、统一的状态反馈和可重复的真实桌面验收。

下一阶段建议按以下顺序推进：

```text
UX-00 真实桌面基线与截图验收
  ↓
UX-01 信息架构/模式指示 + UX-02 窗口焦点
  ↓
UX-03 工具可发现性 + UX-04 参数/图层上下文
  ↓
UX-05 页面/时间轴 + UX-06 模板/预设库
  ↓
UX-07 Source/DevTools 所有权 + UX-08 错误反馈
  ↓
UX-09 多语言/可访问性 + UX-10 响应式/视觉一致性
  ↓
UX-11 回归测试、截图基线、真实项目验收
```

---

## 1. 审计方法与可信边界

### 1.1 已执行的真实 Qt 交互测试

测试不是只实例化类或调用私有函数，而是创建真实 `QApplication`、真实编辑器窗口和真实 Qt 控件，驱动点击、键盘、滑条交互、菜单 action、拖放事件、GIF 播放和文件对话框。

| 测试 | 结果 | 覆盖内容 | 结论 |
|---|---:|---|---|
| `scripts/e2e_mg_user_journey.py --offscreen` | **16 passed, 0 failed, 2 skipped** | 启动、创建 Text/Shape/Frame、键盘输入、参数、Flex、嵌套、Token、主题、关键帧、撤销/重做、样式复制、缓存档位、渲染、保存加载 | Qt 结构和数据链路通过；真实 Chromium 部分因无图形表面跳过 |
| `scripts/e2e_mg_preset_library.py --offscreen` | **7 passed, 0 failed** | 预设浏览、分类、搜索、GIF、双击插入、真实 `QDropEvent`、撤销/重做、另存、ZIP 导入导出、截图 | 预设库的基本交互链路通过 |
| 主 MG/HTML/渲染回归集 | **341 passed, 1 skipped** | HTML/SVG、模板、参数、预览、缓存、DevTools 面板、拖放和重开 | 主要功能没有发现回归 |
| Teaching Animation phase 回归集 | **86 passed** | 导入、registry、retime、UI/AI phase | 模型和编辑器基础链路通过 |
| 预设库补充回归集 | **69 passed** | Library、GIF、模板、手动控制、形状 | 预设库和控件细节通过 |
| 完整 `tests/composite` | **未通过** | 项目级 Composite/Qt/GPU 页面 | 首个失败是 3D Polygon 按钮状态断言；继续执行还复现 Qt 清理阶段段错误，不能把整项目 UI 标记为通过 |

### 1.2 当前环境不能替代的真实桌面检查

本机本轮执行时：

```text
DISPLAY=
WAYLAND_DISPLAY=
QT_QPA_PLATFORM=
```

因此 Qt 使用了 `offscreen` 路径。下面这些现象不能被标记成“已经真实通过”：

- MG 窗口在 macOS 主项目窗口、其他应用和 Dock 之间切换时是否始终回到前台。
- 拖动右侧 splitter 把参数/图层区域压到最小后，菜单恢复、窗口激活和焦点是否符合预期。
- Qt WebEngine 的真实 Chromium Elements、Console、Sources、Network 操作。
- 在 Chromium 预览中点击元素、双击编辑文字、拖拽画形状、拖动时间轴的真实鼠标坐标行为。
- 粒子 HTML 在添加 SVG/文字后是否仍保持动画，以及真实缓存的视频码率、色带和视觉压缩质量。
- 不同 DPI、窗口尺寸、系统字体、英文/中文 locale 下的截断和布局。

离屏运行出现的以下警告属于环境限制，不能直接当成桌面 UI 缺陷；但要在真实桌面验收时逐项复核：

```text
This plugin does not support raise()
This plugin does not support propagateSizeHints()
Sans Serif font alias / C locale switched to UTF-8
```

### 1.3 审计证据的标记规则

- **已确认**：由源代码、Qt 控件状态或自动化测试直接证明。
- **高风险**：结构上存在明显风险，但需要真实桌面截图/操作确认最终视觉表现。
- **待人工**：当前环境不能执行，必须在 macOS 实际窗口中验收。

后续实施不能把“offscreen 通过”写成“视觉通过”；每个验收结果必须写 `passed`、`failed` 或 `manual pending`。

---

## 2. 当前信息架构与用户操作路径

### 2.1 当前布局

```text
MotionGraphicEditorWindow
├─ 顶部 QToolBar
│  ├─ File
│  ├─ Edit
│  ├─ Scene
│  ├─ View
│  ├─ Preview
│  └─ Export
├─ 主区水平 QSplitter
│  ├─ 左侧 Preview
│  │  ├─ Preview 标题 + 当前渲染状态
│  │  ├─ Select / Rectangle / Ellipse / Line / Arrow / ... 工具条
│  │  └─ SceneView 或 HtmlBrowserPreview（二选一）
│  └─ 右侧 QTabWidget
│     ├─ Parameters：Inspector 在上，Layers 在下
│     ├─ Scenes：场景列表
│     ├─ Source：生成 HTML 源码
│     ├─ DevTools：Chromium DevTools + Apply/Discard
│     └─ Library：结构化预设库、GIF、拖拽/插入
└─ 底部垂直 QSplitter
   ├─ Page strip：页面按钮
   └─ Transport：上一页 / 播放 / 下一页 / 时间码 / 时长 / 循环 / Marker
```

### 2.2 当前主要用户路径

| 路径 | 当前入口 | 预期结果 | 主要风险 |
|---|---|---|---|
| 新建图层 | Layers 底部 `+` 或 Preview 工具条 | 图层创建并自动选中 | 用户可能不知道两处入口是否等价 |
| 编辑参数 | Parameters → Inspector | 当前选择同步更新画布 | 多选时只有“n selected”文字，主选择和批量范围不够明显 |
| 画形状 | Preview 工具条选工具后拖动画布 | 创建 SVG/CSS 原生图层 | 工具是图标-only，没有常驻操作提示 |
| 编辑 HTML | Source 或 DevTools | 生成 HTML/DOM 更新并可撤销 | Source、DevTools、HTML 预览的边界需要更明确 |
| 插入模板 | Library 双击/拖拽，或菜单 Apply Template | 预设插入或 HTML 模板应用 | “Template”和“Preset”概念容易被当成同一类资产 |
| 调页面时间 | Page 区域或底部页面条 | 页面时长/转场更新 | 页面参数和时间轴运输控制靠得很近，窄窗口可能难辨 |
| 播放/定位 | 底部 Transport、空格键 | 播放、跳页、时间码定位 | 需要明确焦点、快捷键和播放状态 |
| 切回项目 | 关闭/切换主窗口 | MG 窗口不被项目窗口遮挡 | `WindowStaysOnTopHint` 和 `raise_()` 尚未桌面验证 |

---

## 3. 全量问题清单

优先级定义：

- **P0**：阻断理解、阻断核心工作流或会造成数据/窗口不可恢复。
- **P1**：不阻断功能，但显著增加学习成本、误操作或返工。
- **P2**：视觉一致性、效率、可访问性和长期维护问题。

### F-01｜缺少真实桌面视觉验收基线（P0，待人工）

**依据**：本轮只能使用 `offscreen`；真实 Chromium Journey B 被脚本明确跳过，窗口焦点行为也无法由离屏插件证明。

**问题**：当前自动化能证明控件和数据存在，不能证明用户看到的层级、间距、截断、焦点、窗口前后台和鼠标 hit-test 正确。

**实施方式**：

1. 在 macOS 图形会话中运行不带 `--offscreen` 的 `scripts/e2e_mg_user_journey.py`。
2. 增加固定窗口尺寸矩阵：`1024×720`、`1280×800`、`1440×900`、`1920×1080`，并分别测试英文和中文。
3. 每个尺寸保存 Preview、Parameters、Layers、Scenes、Source、DevTools、Library、Timeline 八张截图。
4. 为窗口切换、隐藏/恢复面板、拖拽形状、双击文字、拖动时间轴录制短视频或操作日志。
5. 将人工结果附加到 `artifacts/ui_audit/2026-08-21/`，不把截图放进源码包。

**涉及文件/工具**：`scripts/e2e_mg_user_journey.py`、新增 `scripts/e2e_mg_ui_visual_audit.py`、`motion_graphic_editor_window.py`。

**验收**：每个尺寸的核心操作都能完成；无标签截断、无遮挡、无不可见焦点、无面板永久丢失；真实浏览器 Journey B 全部执行而不是 skipped。

### F-02｜窗口前后台策略可能过强或不稳定（P0，已确认结构风险，待人工）

**依据**：窗口同时设置 `Qt.Tool` 和 `WindowStaysOnTopHint`，并在参数刷新时调用前台恢复逻辑；离屏环境提示 `raise()` 不受支持。

**问题**：窗口可能跑到项目主窗口后面，也可能因为始终置顶而遮挡其他应用。用户从其他应用切回时，焦点归属、键盘空格播放和关闭行为需要可预期。

**实施方式**：

1. 只保留“拥有者窗口激活”策略：打开编辑器时记录 owner，参数刷新时使用 `showNormal()`、`raise_()`、`activateWindow()`，但不默认全局置顶。
2. 增加 View → `Keep Above Project` 二态选项，默认只高于项目主窗口，不高于其他应用。
3. 在 `focusInEvent`、`focusOutEvent`、`changeEvent(WindowStateChange)` 中记录窗口状态，不在每次参数更新时强制抢焦点。
4. 空格键播放只在编辑器或时间轴拥有焦点时生效，文本输入框正在编辑时不拦截空格。
5. 关闭主项目窗口时明确关闭或隐藏子编辑器，不留下孤立窗口。

**涉及文件**：`motion_graphic_editor_window.py`、主窗口打开 MG 的 controller/adapter、`scene_timeline.py`。

**验收**：打开 MG → 切换到 Finder/浏览器 → 切回 → 编辑器回到用户预期层级；输入文字空格不会误播放；在项目窗口和其他应用之间切换 20 次无丢窗、无抢焦点循环。

### F-03｜主工作区和高级工具混在同一级标签（P1）

**依据**：右侧当前是 `Parameters / Scenes / Source / DevTools / Library` 五个同权重标签。

**问题**：Parameters、Layers、Scenes 是日常设计工作；Source、DevTools 是高级代码调试；Library 是资产浏览。它们现在都看起来像同一层的“页面”，用户不容易判断下一步应去哪儿。

**实施方式**：

1. 保留用户要求的无二级标签原则，不再套一层 tab。
2. 将右侧标签分为三个概念区域，但不增加二级标签：
   - `Design`：Parameters（内部含 Layers）和 Scenes。
   - `Assets`：Library。
   - `Code`：Source 和 DevTools。
3. 用分隔线/组标题或统一的 View 菜单分组表达层级；不要用三个点省略文字。
4. 记住上次选择的 tab，但在新场景打开时默认回到 Parameters。
5. 当当前场景没有 HTML 时，Source/DevTools 显示明确的空状态和“为什么不可用”，而不是空白。

**涉及文件**：`motion_graphic_editor_window.py` `_build_layout()`、`_create_actions()`、`motion_graphic_style.py`、`html_source_editor.py`、`html_devtools_panel.py`。

**验收**：新用户只看标签即可区分设计、资产和代码；Source/DevTools 不再与普通参数产生概念混淆；英文和中文下标签完整显示。

### F-04｜Parameters 与 Layers 虽已纵向合并，但选择上下文不足（P1）

**依据**：`Parameters` 页内部通过 `_attributes_splitter` 放置 `inspector_stack` 和 `layer_tree`；Inspector 多选时主要显示 `n selected — edits apply to all`。

**问题**：选择图层后，用户看得到控件，却不一定知道“正在编辑哪个节点、父级是谁、哪些字段是批量修改、哪些字段来自 HTML 层”。嵌套层级越深，误改概率越高。

**实施方式**：

1. 在 Inspector 顶部增加紧凑 breadcrumb：`Page 1 / Frame / Heading`，每一级可点击回选。
2. 增加选择上下文条：元素类型图标、名称、`1 selected`/`3 selected`、HTML/native 标识。
3. 多选时所有非一致值显示 `Mixed`，编辑前明确“修改将应用到全部选中层”。
4. Layers 中当前行、画布选框和 Inspector breadcrumb 使用同一橙色选中态。
5. 图层树的嵌套拖拽增加插入线、禁止区域提示和撤销提示。
6. 没有选择时，Inspector 显示三步引导：选择图层、在画布点击、或从 Library 插入预设。

**涉及文件**：`scene_inspector.py`、`layer_tree_panel.py`、`scene_view.py`、`motion_graphic_editor_window.py`、`layer_tree_model.py`。

**验收**：任何时刻都能从三处判断当前选择；多选/嵌套/HTML 层不会产生“改了但不知道改到谁”的情况；空画布不会显示大块无说明空白。

### F-05｜绘制工具可用，但可发现性和操作反馈不足（P1）

**依据**：Preview 工具条有 Select、Rectangle、Rounded Rectangle、Ellipse、Line、Arrow、Triangle、Polygon、Star、Pen Path；按钮是固定 26×24 的图标-only 控件。

**问题**：用户必须先猜图标含义，再依赖 tooltip；选中工具后没有持续显示“拖动画布创建 Rectangle / Esc 返回选择”，绘制完成后也不一定知道新层是否创建成功。

**实施方式**：

1. 工具条保留紧凑图标，但增加工具分组间距：选择、基础形状、路径/特殊形状。
2. 工具条下方增加一行非侵入式 context hint，显示当前工具、快捷键、鼠标动作和 Esc 行为。
3. 选中绘制工具时，Preview 画布显示十字光标/起点吸附/拖动尺寸预览；无效拖动显示原因。
4. 绘制完成后：新图层自动选中、Layers 行闪烁一次、状态栏显示“Created Rectangle — Undo”。
5. 为每个工具提供 `QAction` shortcut 和 Command Search 可检索名称；不要只依赖 icon。
6. 统一 Preview 工具条和主项目时间线工具条的 icon、hover、pressed、checked、disabled 色值。

**涉及文件**：`motion_graphic_editor_window.py` `_build_preview_tool_strip()`、`scene_view.py`、`html_browser_preview.py`、`motion_graphic_style.py`、`layer_tree_panel.py`。

**验收**：第一次打开编辑器，用户不看文档也能在 10 秒内创建矩形、文字和 Pen Path；绘制、取消、越界和完成后的状态都可见；键盘 Esc/快捷键可靠。

### F-06｜Native SceneView 与 Chromium HTML Preview 的模式切换不够显式（P1）

**依据**：Preview 使用 `QStackedWidget` 在 `SceneView` 和 `HtmlBrowserPreview` 间切换，动作分散在 Render Preview、HTML Preview、DevTools 等菜单/按钮。

**问题**：用户可能不知道当前是在“原生矢量编辑模式”还是“真实网页运行模式”；相同的选择、绘制和拖拽动作在两种模式下的能力不同。

**实施方式**：

1. Preview 标题旁增加模式徽标：`Native Canvas` / `Live HTML`，并显示当前文档是否有 HTML layer。
2. 模式徽标带 tooltip，说明可用工具、数据来源和切换成本。
3. 模式切换时显示一次非阻塞提示；禁止在有未应用 Source/DevTools 草稿时静默切换。
4. Render Preview 操作拆成“打开 Live HTML”和“生成缓存”两个明确动作，避免一个按钮承担两种预期。
5. 在 HTML 模式中把可编辑 DOM 元素以轻量 outline 标出；在 Native 模式中显示“此层由场景模型驱动”。

**涉及文件**：`motion_graphic_editor_window.py`、`html_browser_preview.py`、`scene_view.py`、`html_source_editor.py`、`html_devtools_panel.py`。

**验收**：用户能在 1 秒内判断当前模式；切换不会丢草稿、不会让工具按钮看似可用但实际无效；两种模式的选择状态可以回环同步。

### F-07｜图层操作入口分散，层级关系的可视反馈不够强（P1）

**依据**：LayerTreePanel 提供新建、删除、上移、下移、复制、粘贴样式、锁定/可见性和 Nest；画布也支持选择和拖动。

**问题**：同一动作可能在画布、图层底部按钮、Edit/Scene 菜单出现；嵌套关系、锁定、不可见、HTML layer 和 native layer 的区别需要用户自己记忆。

**实施方式**：

1. Layers 行统一显示：类型图标、名称、可见、锁定、父级缩进、HTML/native 标签。
2. 将低频操作放入右键菜单；底部只保留 `+ / Delete / Duplicate / Move Up / Move Down` 五个最常用动作。
3. `+` 菜单按 `Text / Shape / Frame / HTML asset` 分组，菜单名称和 Preview 工具条一致。
4. Nest 操作使用明确的“放入 Frame”目标提示，不允许把父级放入自身/子级。
5. 选择、重命名、删除、移动、嵌套均显示可撤销反馈，并支持 Cmd/Ctrl+Z。

**涉及文件**：`layer_tree_panel.py`、`layer_tree_model.py`、`motion_graphic_editor_window.py`、`motion_graphic_style.py`。

**验收**：用户不需要在画布和菜单之间来回寻找常用动作；图层树可以单独完成创建、选择、重命名、嵌套、排序、删除；所有状态在画布和 Inspector 同步。

### F-08｜参数区的密度、值状态和恢复能力不够明确（P1）

**依据**：Inspector 有 Content、Text、Layout、Appearance、Shape、Transform 多个折叠区，使用 `ParamSliderRow`、`DragValue`、`ChevronComboBox` 和颜色控件。

**问题**：参数很多，但缺少“默认值/已修改/混合值/单位/重置”的统一视觉语言；大范围拖动可能让用户误改，修改后没有快速恢复单个字段的入口。

**实施方式**：

1. 所有数值行统一为：标签、控件、数值框、单位、重置图标；范围和 step 从同一字段 schema 读取。
2. 滑条只负责合理范围，数值框允许精确输入；越界时显示 inline validation，不静默夹断。
3. 已修改字段显示小橙点；与默认值相同时隐藏；点击橙点恢复默认值并形成一个 undo step。
4. 多选不同值显示 `Mixed`；批量输入前在行尾显示“applies to N layers”。
5. 颜色统一使用项目自带 ColorPickerControl，并在颜色值旁保留 Token/吸管/重置三种明确动作。
6. 折叠状态和滚动位置按场景类型记忆，但打开新场景不沿用不相关的滚动位置。

**涉及文件**：`scene_inspector.py`、`html_inspector.py`、`page_timing_panel.py`、`src/shared/ui/widgets/` 下的控件、`motion_graphic_style.py`。

**验收**：任何数值都能精确输入、合理拖动、恢复默认和撤销；英文/中文标签都不截断；多选修改不会覆盖未编辑字段。

### F-09｜Page、Transport、Marker 的职责靠得太近（P1）

**依据**：`PageNavigationBar` 和 `SceneTimeline` 被压缩到最大 64px；播放、上一页/下一页、时间码、时长、循环、Marker 共用一行或相邻行。

**问题**：在宽窗口中紧凑有效，但在窄窗口或英文长文本下，页面按钮、时间码和 Marker 可能争抢空间；用户容易把“页面跳转”和“播放头定位”混为一回事。

**实施方式**：

1. Page strip 左侧只负责页面选择和页数摘要；Transport 右侧只负责播放头和播放控制。
2. 为 Page 与 Transport 加轻量分组标签/分隔线，不再使用大面积背景框。
3. 时间码统一显示 `HH:MM:SS:FF`，hover 显示秒数和当前 FPS；所有业务参数保持秒，只有显示层保留帧信息。
4. Marker 改为图标 + 数量 + 下拉菜单；菜单中固定放置 Add、Rename、Remove、Remove All，并显示确认/撤销反馈。
5. 空格键、J/K/L、左右方向键、Shift+左右方向键的行为写入 tooltip/快捷键帮助；文本输入时不抢键盘。
6. 窄窗口时将低频动作收进 `…` 菜单，而不是压缩到不可读。

**涉及文件**：`scene_timeline.py`、`page_navigation.py`、`page_timing_panel.py`、`motion_graphic_editor_window.py`、`motion_graphic_style.py`。

**验收**：1024×720 下按钮文字和时间码完整；播放、跳页、拖动、加 Marker、清空 Marker 互不混淆；键盘和鼠标行为一致。

### F-10｜Template、Preset、Library 的用户概念未完全统一（P1）

**依据**：编辑器同时存在 HTML Template Registry/Effects 入口、菜单中的 Save/Apply Template、右侧结构化 Preset Library；二者底层用途不同，但 UI 名称接近。

**问题**：用户可能不知道“模板”是 HTML 文件夹、“预设”是结构化图层片段，或者在错误的库中寻找资产。

**实施方式**：

1. 产品文案明确区分：`HTML Templates`（网页背景/完整 HTML）与 `Layer Presets`（可插入的结构化图层片段）。
2. Library 页面顶部增加类型筛选或两个并列入口，但不再复制同一份卡片。
3. 卡片显示来源徽标 `Built-in / User / Project`、类型、版本、尺寸、时长、预览状态。
4. 双击、拖拽、Insert 三条路径使用同一插入结果；插入后状态栏说明插入了多少图层、是否可撤销。
5. 不在普通 tooltip 中显示本机绝对文件夹路径；路径只放在“打开所在目录/诊断信息”里。
6. 空库状态提供 `Import ZIP`、`Open Library Folder`、`Create Preset` 三个下一步动作。

**涉及文件**：`asset_library_panel.py`、`motion_graphic_editor_window.py`、`edit_effects_panel.py`、`motion_graphic_generator_registry.py`、`motion_graphic_style.py`。

**验收**：用户能在 5 秒内判断要找 HTML 模板还是图层预设；卡片预览、拖拽、双击和 Insert 结果一致；不泄露绝对机器路径。

### F-11｜Source/DevTools 的草稿所有权和保存状态不够显式（P1）

**依据**：Source 与 DevTools 各自有 Apply/Discard，窗口内部有 `_source_draft_pending`、`_devtools_draft_pending` 状态；状态主要显示在各自面板。

**问题**：用户可能在 Source 改了一份、在 DevTools 改了另一份，最后不知道哪一份是当前真相；窗口标题也没有统一的 dirty 标识。

**实施方式**：

1. 建立一个编辑器级 `DocumentState`：clean、native dirty、source draft、devtools draft、rendering、error。
2. 标题显示 `Motion Graphic — name *`，关闭前统一询问 Save / Discard / Cancel。
3. Source 和 DevTools 进入互斥编辑锁：一个存在草稿时，另一个显示“先 Apply 或 Discard”。
4. Apply 前显示变更摘要：HTML 长度、节点数、样式变更数；失败时保留草稿并提供错误定位。
5. DevTools 面板顶部固定显示 Chromium 是否连接、DOM 是否有未应用变更、Apply/Discard 作用范围。

**涉及文件**：`motion_graphic_editor_window.py`、`html_source_editor.py`、`html_devtools_panel.py`、`editor_controller.py`、`commands.py`。

**验收**：任何未保存修改都能在窗口级别被看见；关闭、切换场景、打开 Library、切换 Preview 模式不会静默丢草稿；Apply/Discard 都能撤销或恢复。

### F-12｜错误和成功反馈过度依赖短暂 statusBar（P1）

**依据**：工具选择、绘制提示和部分操作通过 `statusBar().showMessage(..., timeout)`；Library 也主要使用状态标签。

**问题**：状态栏在窄窗口不可见或很快消失；渲染失败、导入失败、路径拒绝、缓存失败需要用户知道“发生了什么”和“下一步怎么做”。

**实施方式**：

1. 增加统一 `InlineNotice` 组件，分 info/success/warning/error 四级，支持复制详情、重试、打开日志。
2. 对导入、保存、渲染、模板加载、DevTools Apply 使用面板内提示；statusBar 只做补充。
3. 错误信息分为用户可读摘要和可展开诊断详情，禁止只显示 Python exception 文本。
4. 所有异步操作显示 idle/running/success/failure/cancelled 五态，禁止按钮点击后无反馈。
5. 失败后保留当前编辑内容，不用清空画布表示错误。

**涉及文件**：新增 `src/features/teaching_animation/ui/inline_notice.py`，以及 `motion_graphic_editor_window.py`、`asset_library_panel.py`、`html_source_editor.py`、`html_devtools_panel.py`、渲染 callback。

**验收**：故意导入无效 HTML、缺失 GIF、无效 ZIP、渲染失败和 DevTools Apply 失败时，用户都能看到原因、下一步和是否可重试；成功也有明确确认。

### F-13｜多语言覆盖还需要全量审计（P1）

**依据**：多数新控件使用 `I18N.tr`，但 `edit_effects_panel.py` 仍有硬编码 `MG 生成器`，部分 fallback、工具 tooltip、图层菜单和预设分类需要逐项核对。

**问题**：项目切换英文后，MG 编辑器可能出现中文标签、混合语言、按钮截断或中英文长度导致布局变化。

**实施方式**：

1. 扫描 `src/features/teaching_animation/ui/` 和 `edit_effects_panel.py` 中所有用户可见字符串，建立 key 清单。
2. 为中文、英文补齐同一组 key；禁止把机器名、异常栈、文件夹路径直接作为 UI 文案。
3. 以最长英文/中文字符串做布局测试，不用单一语言的固定宽度掩盖截断。
4. 对颜色、形状、Page、Transport、Marker、Library、Source、DevTools、菜单和快捷键帮助加 locale 测试。
5. 语言切换后刷新打开的 MG 窗口，已存在的动态菜单和 tooltip 也要更新。

**涉及文件**：`src/shared/i18n/`、`edit_effects_panel.py`、`motion_graphic_editor_window.py` 及所有 MG UI 文件。

**验收**：英文模式不出现中文；中文模式不出现不必要的英文 fallback；所有固定宽度控件在两种语言下均不截断。

### F-14｜键盘、焦点和可访问性不足（P2，部分为高风险）

**依据**：大量操作使用 icon-only `QToolButton`；tooltip 是主要名称来源，部分按钮没有可见文字；当前自动化主要验证程序化点击。

**问题**：键盘用户、触控板用户和第一次使用者很难发现工具；焦点环、tab 顺序、禁用原因和屏幕阅读器名称没有统一规范。

**实施方式**：

1. 每个 icon-only 按钮设置 accessible name、accessible description、tooltip 和对应 QAction。
2. 规定 Tab 顺序：菜单 → Preview 工具 → 画布 → Parameters → Layers → Timeline；不让隐藏控件抢焦点。
3. 增加可见 focus ring，不能只依赖 hover。
4. 为常用动作提供快捷键帮助面板；冲突时显示当前绑定。
5. 复选框、折叠区、Tab、滑条和时间轴支持 Space/Enter/方向键，并保留鼠标行为。
6. 检查高对比度、100%/150%/200% DPI 和无鼠标操作。

**涉及文件**：所有 MG UI 文件、`motion_graphic_style.py`、共享 icon/control 组件。

**验收**：仅用键盘可以完成创建、选择、编辑、播放、保存、撤销；Qt accessibility inspector 能读出按钮和当前值；焦点不会在隐藏面板中丢失。

### F-15｜窗口尺寸、Splitter 和固定高度存在响应式风险（P2，高风险）

**依据**：主 splitter 初始尺寸为 `[920, 520]`，右侧最小宽度 `420`，底部 timeline `maximumHeight=64`，多处按钮/数值框使用固定宽度。

**问题**：在 1024 宽、英文长文案、较大系统字体或窄屏上，标签、时间码、Page 和 Library 预览会争抢空间；用户拖动 splitter 后也缺少恢复布局入口。

**实施方式**：

1. 建立宽度断点：宽屏双栏、普通宽度缩短工具条、窄屏临时折叠右侧面板。
2. 使用 size policy 和最小可用宽度替代过多 fixed width；只给时间码/数值框设必要最小宽度。
3. View 菜单增加 `Reset Panel Layout`，保存 splitter sizes 和 tab，但提供一键恢复。
4. Timeline 在高度不足时自动收起 Page 详情，仅保留 Transport；恢复时保留播放状态。
5. Library 卡片在窄宽度切换为列表模式，预览区可隐藏但不丢选择。

**涉及文件**：`motion_graphic_editor_window.py`、`motion_graphic_style.py`、`scene_timeline.py`、`page_navigation.py`、`asset_library_panel.py`。

**验收**：四个窗口尺寸矩阵下没有文字截断、按钮重叠或不可恢复的空白；拖动/重启后布局可恢复；英文模式不比中文模式多出结构性缺陷。

### F-16｜MG 视觉语言与项目 Properties/时间线工具条还需要像素级统一（P2）

**依据**：MG 使用 `MOTION_GRAPHIC_EDITOR_QSS`，而项目 Properties、Effects、时间线有自己的颜色、圆角、滑条和按钮样式。

**问题**：即使功能已统一，用户仍可能看到滑条轨道、按钮高度、橙色 active 态、折叠标题、输入框和 icon 风格不一致，造成“像另一个软件”的感觉。

**实施方式**：

1. 建立共享 design tokens：背景、边框、active orange、hover、disabled、text、focus ring、radius、control height、spacing。
2. 让 MG Inspector 直接复用项目 Properties 的 `ParamSliderRow`、`DragValue`、`ColorPickerControl`、折叠 section 样式。
3. Preview/Timeline icon 使用同一个 icon manager 和颜色状态；不要在单个 panel 内重新绘制相似 icon。
4. 用截图对比而不是“看起来差不多”：以 Properties、Solid Color、时间线 Transport 为 reference，记录尺寸和 RGB 色值。
5. 修复深色 DevTools 的外壳与编辑器背景的边界；DevTools 内部主题交给 Chromium，但外围按钮/状态必须一致。

**涉及文件**：`motion_graphic_style.py`、`src/shared/ui/style_sheet.py`、`src/shared/ui/widgets/`、`scene_inspector.py`、`scene_timeline.py`、`html_devtools_panel.py`。

**验收**：相同控件在 Properties 与 MG 中的高度、圆角、滑条、橙色 active 和 focus ring 可用截图/像素测量证明一致；英文和中文不因固定高度截断。

### F-17｜空状态和首次使用引导不够主动（P2）

**依据**：空 Scene 的 Inspector 主要是空白；Library 无结果时显示 “No presets match the current filter”；Source/DevTools 有基础 placeholder。

**问题**：第一次打开编辑器时，用户不知道是先导入 HTML、创建文字、绘制形状还是打开 Library；空状态没有把用户带到下一步。

**实施方式**：

1. Preview 空画布显示三项首选动作：`Create Text`、`Draw Shape`、`Open Library`。
2. Parameters 无选择时显示“选择图层后编辑参数”的简短说明和快捷键。
3. Library 无结果时提供清除筛选、导入 ZIP、打开目录、创建预设。
4. Source/DevTools 无 HTML 时解释当前场景为什么不可用，并提供 `Import HTML`/`Create HTML Layer`。
5. 首次提示只出现一次，可从 Help/Preferences 再次查看，不弹出连续模态对话框。

**涉及文件**：`motion_graphic_editor_window.py`、`scene_inspector.py`、`asset_library_panel.py`、`html_source_editor.py`、`html_devtools_panel.py`。

**验收**：新建空场景后，用户不看文档可以在 10 秒内完成第一项有效操作；没有“什么都没有但也没有解释”的大块空白。

### F-18｜保存、关闭和异步操作的状态没有统一到一处（P1）

**依据**：保存、Source draft、DevTools draft、渲染缓存、模板/预设写盘各有自己的状态字段和提示。

**问题**：用户可能不知道当前内容是否已保存、缓存是否仍在生成、预设是否已经写入磁盘；关闭窗口时不同状态的处理规则不一致。

**实施方式**：

1. 在窗口标题/Preview 状态区增加统一状态：`Saved`、`Unsaved`、`Rendering`、`Saving`、`Error`。
2. 所有异步任务有取消按钮和完成回调；关闭时根据任务状态询问等待、取消或继续后台。
3. 保存、Apply、Render Cache、Save Preset 的结果写入同一 activity log，用户可展开查看。
4. 崩溃/异常前尽可能保留 autosave draft，下一次打开提供恢复选择。

**涉及文件**：`motion_graphic_editor_window.py`、`editor_controller.py`、渲染 callback、preset/library service、文件 controller。

**验收**：任何时刻用户都能回答“我现在是否保存、是否在渲染、是否有未应用草稿”；异常关闭后不会静默丢失最近一次可恢复内容。

---

## 4. 落地工作包

### UX-00：建立真实桌面和视觉基线（P0）

**目标**：把视觉问题从主观感受变成可重复证据。

**实施步骤**：

1. 新增 `scripts/e2e_mg_ui_visual_audit.py`，复用现有 Journey A/B/C 的场景工厂和操作 helper。
2. 新增 `--sizes`、`--locale`、`--capture-dir`、`--keep-open` 参数。
3. 运行真实 macOS session，不设置 `QT_QPA_PLATFORM=offscreen`。
4. 逐个捕获空场景、选中文字、选中形状、多选、HTML 模式、Library、DevTools、窄宽度和时间轴状态。
5. 输出 JSON：窗口尺寸、当前 tab、选择 id、焦点 widget、splitter sizes、页面、时间码、dirty state。
6. 由人工检查视觉层级，由脚本检查控件是否越界、文本是否被 elide、按钮是否可用。

**验收门槛**：所有图片和 JSON 归档；Journey B 不再 skipped；每项问题都有截图编号和复现步骤。

### UX-01：重做信息架构和模式指示（P0/P1）

**实施步骤**：

1. 在 `MotionGraphicEditorWindow._build_layout()` 提取 `DesignPanel`, `AssetPanel`, `CodePanel` 的概念分组。
2. 不增加二级 tab；只调整标签文案、分隔和 View 菜单分组。
3. 添加 Preview 模式徽标、当前场景类型和 HTML/native 状态。
4. 添加无 HTML/无选择/无 Library 结果的空状态 CTA。
5. 为场景切换和 tab 切换写入 session settings，并提供 Reset Panel Layout。

**自动化**：`tests/ui/test_motion_graphic_editor_information_architecture.py`。

**验收**：打开任何场景后，主要工作路径不超过三次点击；高级代码工具不遮蔽设计入口；空状态有下一步。

### UX-02：修复窗口焦点、前台恢复和快捷键焦点（P0）

**实施步骤**：

1. 把置顶策略从隐式 flag 改成 owner-scoped policy。
2. 把前台恢复集中到一个 `bring_to_front(reason)`，禁止每个参数刷新点自己 `raise_()`。
3. 建立窗口切换、项目窗口关闭、重新打开、Dock 点击、其他 App 切回的状态机。
4. 增加编辑器级 `QShortcut`，但在 `QLineEdit/QTextEdit/QPlainTextEdit` 编辑状态暂停空格播放。
5. 在真实 macOS 运行 20 次切换压力测试。

**自动化**：离屏验证状态机；真实桌面脚本验证焦点和 z-order，不能用离屏结果替代。

### UX-03：工具条和绘制反馈（P1）

**实施步骤**：

1. 为工具建立结构化 schema：id、label、icon、shortcut、cursor、hint、allowed modes。
2. Preview 工具条从 schema 生成，Layer `+` 菜单也从同一 schema 生成，避免名称漂移。
3. 新增 context hint、绘制预览、取消状态、创建后选中和可撤销 notice。
4. 为路径工具补充节点编辑/取消/完成操作说明。
5. 为每个 tool 写鼠标和键盘测试。

**自动化**：`tests/ui/test_motion_graphic_tools_discoverability.py`、扩展 `tests/render/test_motion_graphic_vector_shapes.py`。

### UX-04：Inspector 与 Layers 上下文统一（P1）

**实施步骤**：

1. 增加 selection context model，不再只传 primary element。
2. Inspector 顶部加入 breadcrumb、类型徽标、选择数量和 HTML/native 状态。
3. 统一 mixed value、modified dot、reset、unit、validation 和 undo gesture。
4. Layer tree 行加入父级、状态、拖拽目标和右键菜单。
5. 没选择时显示引导；HTML 层与 native 层切换时保留选择语义。

**自动化**：多选、mixed、嵌套、锁定、隐藏、reset、undo/redo、焦点顺序。

### UX-05：Page、Transport、Marker 重排（P1）

**实施步骤**：

1. 保持 Page 左、Transport 右的布局，但给两组明确边界。
2. 统一时间码显示和秒数 tooltip；模型仍使用 frame，所有 UI 参数使用 seconds。
3. Marker 菜单固定四项操作，Remove All 使用可撤销确认而不是隐式清空。
4. 增加键盘帮助和焦点样式；窄宽度收纳低频动作。
5. 记录播放状态、当前页和循环状态的可视反馈。

**自动化**：扩展 `tests/ui/test_motion_graphic_manual_controls.py` 和 `tests/ui/test_motion_graphic_editor_focus.py`。

### UX-06：模板、预设和 Library 统一（P1）

**实施步骤**：

1. 先确定产品术语和数据类型，不直接把两个 registry 合并成不可区分的一套。
2. Library 卡片增加类型、来源、版本、尺寸、时长、预览状态。
3. 移除普通 tooltip 的绝对路径；诊断页显示安全处理后的来源。
4. 统一双击/Insert/QDropEvent 的插入命令和状态反馈。
5. 增加导入失败、重复 id、无 GIF、无效 manifest 的用户级提示。

**自动化**：扩展 `tests/ui/test_mg_asset_library.py`、`scripts/e2e_mg_preset_library.py`。

### UX-07：Source/DevTools 双向编辑协议（P1）

**实施步骤**：

1. 建立 editor-level dirty state 和 source/devtools 互斥草稿锁。
2. Apply 前生成变更摘要，失败保留草稿；Discard 说明影响范围。
3. 标题加 `*`，关闭/切换场景统一询问。
4. 真实 Chromium 中测试 Elements style、DOM 文本、Console/Source 诊断和 Apply/Discard。
5. DevTools 使用深色模式，但外围控件复用项目主题。

**自动化/人工**：`tests/ui/test_html_inspector.py`、`tests/ui/test_html_devtools_panel.py`；真实 WebEngine Journey B/C 必须通过。

### UX-08：通知、错误和异步状态统一（P1）

**实施步骤**：

1. 新增 `InlineNotice` 和 activity log。
2. 把模板、预设、Source、DevTools、Render Cache、Import HTML 的提示接入同一服务。
3. 每个异步动作补齐取消、失败、重试、保留草稿路径。
4. 用无效输入和断网/缺文件 fixture 做失败测试。

**验收**：无静默失败；所有错误有用户摘要、详情和下一步。

### UX-09：多语言、键盘和可访问性（P1/P2）

**实施步骤**：

1. 扫描并注册所有用户可见字符串。
2. 增加中英文 widget text、tooltip、accessible name 快照测试。
3. 建立 Tab order、focus ring、shortcut conflict、screen reader smoke 测试。
4. 在 150%/200% DPI 和最长文案下截图。

**验收**：英文模式不出现中文；键盘可以完成核心流程；所有 icon-only 控件都有可访问名称。

### UX-10：响应式和视觉 token（P2）

**实施步骤**：

1. 从 `motion_graphic_style.py` 和项目 Properties 提取共享 token。
2. 建立尺寸断点与 Layout State：wide、standard、compact。
3. 添加 Reset Panel Layout 和持久化。
4. 逐项测量按钮高度、滑条轨道、圆角、边框、间距、active orange 和 focus ring。
5. 对 Properties、Solid Color、时间线、MG Inspector、DevTools 外壳做截图差异检查。

**验收**：视觉差异有数据依据；窄窗口不会靠字体缩小解决问题。

### UX-11：全量回归和发布门槛（P0）

**实施步骤**：

1. 每个 UX 工作包必须同时添加 focused test 和至少一个真实用户路径。
2. 保留当前 341+、69、86 和两个 E2E 结果作为回归基线。
3. 真实 WebEngine/GPU/桌面行为单独标记，不因 offscreen 通过而关闭。
4. 在清洁环境验证 locale、资源、模板、GIF、字体、缓存和窗口恢复。
5. 所有 P0 通过后才进入视觉微调；P1/P2 不得绕过 P0。

---

## 5. 真实用户手工测试脚本

以下脚本用于 macOS 图形环境，执行时记录每一步的 `passed/failed/manual pending`、截图编号和复现视频。除非写明，否则操作都按普通用户方式进行，不调用私有函数。

### A. 启动、窗口和空状态

1. 从项目主窗口打开一个已有 MG clip。
2. 确认编辑器默认显示 Preview + Parameters，窗口位于项目窗口预期层级。
3. 关闭编辑器，再次打开同一 clip，确认上次的 tab、splitter 和时间码是否按设计恢复。
4. 新建空 MG 场景，确认 Preview、Inspector、Layers、Timeline 都有下一步引导。
5. 切换到 Finder、浏览器和其他 Qt 窗口，再切回；重复 20 次。
6. 拖动右侧 splitter 让 Parameters/Layers 接近最小，使用 View 菜单恢复布局。

### B. 创建、选择和参数

7. 从 Layers `+` 创建 Text，输入英文和中文长文本，确认自动选中。
8. 从 Preview 工具条创建 Rectangle、Ellipse、Pen Path，分别拖动小尺寸、正常尺寸和越界尺寸。
9. 用 Esc、工具按钮和快捷键切回 Select。
10. 在画布、Layers、Inspector 三处交替选择同一层，确认高亮和 breadcrumb 一致。
11. Ctrl/Cmd 选择多层，确认 mixed value、批量修改范围和 undo step。
12. 将文字和形状嵌套进 Frame，拖出、排序、隐藏、锁定、删除，再撤销/重做。
13. 修改颜色、Token、Opacity、Transform、Layout、Shape 参数；测试拖动、精确输入、重置和非法值。

### C. HTML、页面和时间轴

14. 打开带 HTML layer 的场景，确认 Preview 模式徽标显示 Live HTML。
15. 选择 Page 1/Page 2，修改页面时长、转场和总时长，确认均以秒显示。
16. 使用上一页/播放/下一页、空格键、拖动播放头、时间码和循环。
17. 添加 Marker、重命名、删除一个、Remove All，检查确认、撤销和时间轴可见性。
18. 切换 Native Canvas/Live HTML，确认无未应用草稿时可切换，有草稿时有明确拦截。

### D. 模板、预设、代码

19. 在 Library 中分类、搜索、hover/播放 GIF、双击 Insert、拖拽 Insert，比较三种结果。
20. 保存为 Preset，填写名称/分类/标签/描述，确认 GIF 和目录写入成功。
21. 导出 ZIP、删除用户预设、重新导入，确认来源徽标和状态提示。
22. 在 Source 修改 CSS/HTML，观察 dirty、Apply、Discard、Undo、Close confirmation。
23. 在 DevTools Elements 修改 DOM/CSS，确认深色主题、Apply/Discard、与 Source 的互斥提示。
24. 故意导入坏 HTML、坏 ZIP、缺 GIF、超大文件和越界路径，确认错误提示不清空已有画面。

### E. 视觉与可访问性

25. 在英文/中文、100%/150%/200% DPI、1024/1280/1440/1920 宽度下重复 A-D 的核心路径。
26. 只用键盘完成创建、选择、参数修改、播放、Marker、保存和关闭。
27. 检查所有 icon-only 控件 tooltip、accessible name、focus ring、disabled reason。
28. 对照项目 Properties、Solid Color 和时间线工具条，检查高度、圆角、滑条、active orange、边框和间距。

---

## 6. 计划新增的测试文件和测试点

| 测试文件 | 必测内容 |
|---|---|
| `tests/ui/test_motion_graphic_editor_information_architecture.py` | tab 分组、默认 tab、空状态 CTA、模式徽标、Reset Panel Layout |
| `tests/ui/test_motion_graphic_editor_focus.py` | owner 激活、焦点转移、空格键、文本输入不抢快捷键、关闭/重开 |
| `tests/ui/test_motion_graphic_tools_discoverability.py` | 工具 schema、名称/tooltip/accessible name、checked 状态、Esc、创建后选中 |
| `tests/ui/test_motion_graphic_selection_context.py` | breadcrumb、单选、多选、mixed、HTML/native 标识、Layers/Canvas/Inspector 同步 |
| `tests/ui/test_motion_graphic_inspector_controls.py` | 单位、范围、step、reset、非法输入、modified dot、undo gesture |
| `tests/ui/test_motion_graphic_transport_layout.py` | Page/Transport 分组、timecode、秒/帧显示、Marker 菜单、Remove All |
| `tests/ui/test_motion_graphic_library_ux.py` | 类型筛选、来源徽标、路径不泄露、空状态、双击/Insert/drop 一致 |
| `tests/ui/test_html_editor_dirty_state.py` | Source/DevTools 草稿锁、标题 dirty、Apply/Discard、关闭确认 |
| `tests/ui/test_motion_graphic_i18n_accessibility.py` | 中英文文本、最长文案、accessible name、Tab order、tooltip |
| `tests/ui/test_motion_graphic_responsive_layout.py` | 1024/1280/1440/1920、splitter 恢复、控件越界、Library 列表模式 |
| `scripts/e2e_mg_ui_visual_audit.py` | 真实桌面截图、焦点、窗口切换、WebEngine DOM、人工检查清单 |

### 测试实现要求

- 所有 focused test 都必须使用真实 Qt widget，不用 mock 替代控件信号。
- 浏览器行为必须区分 `offscreen skipped` 与桌面 `passed`。
- 每个写入动作都检查 undo/redo；每个异步动作都检查 success/failure/cancel。
- 每次 UI 改动都跑英文和中文；不能只测默认 locale。
- 截图测试只用于固定视觉基线，不把平台字体差异误判成业务失败；同时保留结构性断言。

---

## 7. 实施顺序与依赖

| 顺序 | 工作包 | 原因 | 交付门槛 |
|---:|---|---|---|
| 1 | UX-00 | 没有桌面基线就不能判断视觉问题 | 有真实截图、焦点日志和尺寸矩阵 |
| 2 | UX-01 + UX-02 | 模式和窗口层级是所有操作的前提 | 无丢窗、无误切换、主路径清晰 |
| 3 | UX-03 + UX-04 | 创建/选择/参数是最频繁操作 | 工具可发现、选择上下文完整、undo 正确 |
| 4 | UX-05 | 页面和播放是时间轴核心 | Page/Transport/Marker 不混淆、秒/时间码一致 |
| 5 | UX-06 + UX-07 | 模板和代码工作流容易产生概念/数据冲突 | 术语清晰、草稿不丢、Apply 可回滚 |
| 6 | UX-08 + UX-09 | 错误、语言和键盘决定可用性 | 中英文、错误恢复和键盘路径通过 |
| 7 | UX-10 | 在结构稳定后统一视觉和响应式 | 四个尺寸、两种语言截图通过 |
| 8 | UX-11 | 最后锁定发布质量 | 全量回归、清洁环境、真实桌面验收通过 |

禁止的实施顺序：先改颜色/圆角，再去解决模式、焦点、空状态和保存状态；这样只会让问题更好看，但不会更可用。

---

## 8. 每个工作包的完成定义

合并任何一个 UX 工作包前，必须同时提交：

- [ ] 影响文件、调用链和数据所有权说明。
- [ ] 中英文文案 key 与 fallback。
- [ ] focused Qt/UI tests。
- [ ] 至少一条真实用户路径或桌面手工记录。
- [ ] 成功、取消、失败、关闭和恢复路径。
- [ ] 相关 Preview、HTML、缓存、导出一致性测试（如涉及渲染）。
- [ ] `git diff --check` 和对应回归集结果。
- [ ] 明确标注 `passed`、`failed` 或 `manual pending`，不把 offscreen 结果冒充视觉通过。

### 最终完成标准

MG 编辑器达到以下状态才算 UI/交互审计闭环：

1. 用户能看懂当前处于 Native Canvas、Live HTML、Source 或 DevTools 哪一种工作模式。
2. 用户能从画布、图层树和 Inspector 三处确认当前选择、父级和批量范围。
3. 文字、形状、参数、页面、播放、Marker、模板和代码操作都有明确的可见反馈。
4. 所有未保存草稿、渲染任务、错误和恢复路径都在窗口级别可见。
5. 中文/英文、窄/宽窗口、键盘/鼠标和真实 macOS 前后台行为一致。
6. 视觉设计与项目 Properties、Solid Color 和时间线工具条共享同一套控件和状态语言。
7. 自动化测试、真实桌面操作和截图基线三者结论一致，浏览器/GPU/窗口焦点不再被跳过。

本计划完成后，下一步才进入具体代码实施；实施时应按 UX-00 → UX-11 分批提交，避免再次一次性修改整个编辑器而无法判断是哪一项引入了问题。
