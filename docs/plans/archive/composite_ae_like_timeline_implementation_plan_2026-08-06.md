# Composite AE 式 Keyframes 时间线升级实施方案

日期：2026-08-06<br>
适用模块：`src/features/composite/`、`src/shared/keyframe.py`、Composite 页面、AI/脚本编辑接口<br>
状态：实施基线方案<br>
关联基础方案：`docs/composite_davinci_keyframes_tool_plan_2026-06-28.md`、`docs/composite_keyframes_spline_optimization_plan_2026-06-28.md`

---

## 0. 方案结论

Topos Color 可以把现有 Keyframes 面板升级成 AE 用户熟悉的“合成时间线”，并让它成为所有动画操作的主入口：

```text
Keyframes 时间线：负责时间、图层/节点、关键帧、表达式、批量操作
Spline 曲线编辑器：负责数值曲线、贝塞尔手柄、切线和精细缓入缓出
属性面板：负责参数数值输入，并作为时间线的快速入口
AI / Script：调用同一套动画编辑命令，不绕过 UI 数据层
渲染器：只读取同一份权威动画快照
```

目标不是复制 AE 的私有实现或图标资产，而是复制 AE 用户已经形成的交互心智模型：

- 看见图层/节点，展开后看属性。
- 通过秒表/关键帧按钮判断属性是否动画化。
- 在同一个时间线中添加、移动、复制、删除关键帧。
- 在同一个位置进入表达式编辑。
- 用快捷键快速显示 Position、Scale、Rotation、Opacity 等常用属性。
- 用 `U` 只显示已动画属性，用 `UU` 显示已修改属性。
- 时间线、曲线编辑器、属性面板和渲染结果始终保持同步。

实现完成后，熟悉 AE 的用户不需要重新学习“关键帧应该在哪里、如何选中、如何移动、如何进入表达式”，只需要学习 Topos Color 特有的节点和参数名称。

---

## 1. 当前代码基线

### 1.1 已有模块和可复用能力

| 模块 | 当前职责 | 本方案处理方式 |
|---|---|---|
| `src/features/composite/ui/keyframes_panel.py` | Keyframes 顶层面板、工具栏、播放、工作区、上下文状态 | 保留顶层容器，改造成 AE 式工具栏和命令分发器 |
| `src/features/composite/ui/keyframes_model.py` | 节点行、参数行、可见行、时间状态 | 扩展为统一的 Timeline Row/Track ViewModel |
| `src/features/composite/ui/keyframes_tree_view.py` | 左侧节点/参数树绘制和选择 | 增加 AE 式列、层级状态和筛选显示 |
| `src/features/composite/ui/keyframes_timeline_view.py` | 标尺、范围条、关键帧点、拖拽、多选、复制粘贴 | 继续作为无 QWidget 关键帧绘制区域，增加命令、表达式行和吸附 |
| `src/features/composite/ui/keyframe_editor.py` | 属性面板内部的小型关键帧编辑器 | 作为兼容入口，最终复用统一 TimelineCommand，不再维护独立动画逻辑 |
| `src/features/composite/ui/spline_panel.py` | Spline 顶层面板 | 保留为曲线编辑器，和时间线共享选择及轨道数据 |
| `src/features/composite/ui/spline_model.py` | 曲线数据模型和关键帧曲线 | 改成读取统一的 `AnimationTrackSnapshot` |
| `src/features/composite/ui/spline_view.py` | 曲线绘制、手柄操作 | 增加选中状态、切线模式和 undo 命令 |
| `src/shared/keyframe.py` | `Keyframe`、`KeyframeTrack`、插值和外推 | 作为唯一关键帧数学与序列化基础，不另建第二套关键帧类型 |
| `src/features/composite/edit_service.py` | 节点、参数、关键帧、表达式、切线的统一编辑服务 | 扩展为所有 UI/AI/脚本操作的唯一变更入口 |
| `src/features/composite/ui/node_properties_panel.py` | 参数输入、关键帧按钮、表达式编辑器 | 保留参数输入；关键帧/表达式操作转发到统一命令 |
| `src/features/scripting/composite_api.py` | 脚本化关键帧和表达式操作 | 扩展为与 UI 同名同语义的命令接口 |
| `src/features/edit/ai_editing/capability_manifest.py` | AI 能力声明 | 增加时间线查询、选择、表达式和批量动画能力 |

### 1.2 当前已经具备的基础

现有代码已经包含以下基础能力，后续应优先复用，不要重写成互相独立的实现：

- 节点 → 参数 → 关键帧的树状模型。
- 关键帧水平拖拽、多选、框选、复制、粘贴和删除。
- Linear、Bezier、Hold、Ease In、Ease Out、Ease In/Out 等插值类型。
- 贝塞尔 handle、tangent mode 和外推模式的数据字段。
- 播放、逐帧、首帧、尾帧、前后关键帧跳转。
- `Ctrl/Cmd + K` 添加关键帧，`I/O` 设置工作区；默认 `J/K` 保留 AE 的前后关键帧导航语义。
- 参数右键表达式入口，以及关键帧按钮 `Alt/Option + 左键` 表达式入口。
- AI/脚本访问关键帧、插值、Bezier 手柄、tangent mode 和 expression 的接口。
- Spline 与关键帧曲线的基础联动。

### 1.3 本方案要解决的主要缺口

| 缺口 | 用户看到的症状 | 解决方向 |
|---|---|---|
| 时间线仍偏 Fusion 风格 | AE 用户不知道哪些按钮对应 AE 的 stopwatch、属性行和层级 | 重新设计行、列、工具栏和快捷键映射 |
| 表达式入口分散 | 用户需要回属性面板才能管理表达式 | 在时间线参数行直接显示表达式状态并支持右键/Alt-click |
| 关键帧和表达式没有统一命令 | UI、AI、脚本可能各自修改状态 | 所有修改统一走 `TimelineCommand` / `CompositeEditService` |
| 多维参数操作不够 AE 化 | Scale X/Y、Position X/Y 等通道的选择和联动不够明确 | 引入 group track、channel track、linked/separated 状态 |
| Spline 选择可能与时间线不一致 | 两个面板需要重复选取同一组关键帧 | 建立共享 `AnimationSelectionModel` |
| 层级状态不完整 | 缺少锁定、隐藏、筛选、修改状态和表达式状态 | 增加 track state 和持久化/会话态边界 |
| 大量关键帧时容易重绘过多 | 拖拽或滚动时性能下降 | 保持 QPainter 绘制，禁止每个关键帧创建 QWidget，增加可见范围绘制 |

---

## 2. 产品目标和边界

### 2.1 必须达到的目标

1. Keyframes 面板成为 Composite 中所有动画的主时间线。
2. 新建、删除、移动、复制、粘贴、批量修改关键帧都可以在时间线完成。
3. 表达式可以直接从参数行打开、编辑、验证、启用和移除。
4. 属性面板、Keyframes、Spline、AI、脚本和渲染器共享同一份动画状态。
5. 所有编辑都有可预测的 Undo/Redo、保存/加载和错误提示。
6. AE 用户可以通过熟悉的快捷键完成大部分高频操作。
7. 所有新增功能可被 AI 发现、查询和调用，而不是只提供不可见的内部方法。

### 2.2 明确不做的事情

- 不复制 AE 的品牌图标、私有源码、专有文件格式或未经许可的资产。
- 不把所有节点强行伪装成 AE 图层。节点仍然是 Topos Color 的核心对象；只复用 AE 的动画交互模型。
- 不让 UI 直接修改渲染线程对象。UI 只生成命令，渲染器只读取不可变快照。
- 不让 Spline 另存一份关键帧。Spline 只编辑统一轨道中的曲线字段。
- 不在第一阶段实现完整的 AE 表达式语言。先使用项目已有的安全表达式运行时，再逐步扩展函数。
- 不为了兼容 AE 而引入无法解释的隐式数值转换。每个参数都必须有明确的 scalar、int、bool、color、vec2、vec3、enum 类型策略。

---

## 3. 目标 UI 规格

## 3.1 整体布局

Keyframes 面板仍然停靠在 Composite 页面底部，但视觉和交互改成 AE 时间线的三个区域：

```text
┌────────────────────────────────────────────────────────────────────────────┐
│ Keyframes | search | filter | transport | keyframe tools | zoom | menu      │
├──────────────────────────────┬─────────────────────────────────────────────┤
│ layer / property columns      │ ruler / work area / timeline tracks         │
│ lock  hide  name  value  fx   │ range bars, expressions, keyframes, playhead│
│ node                          │                                             │
│   Transform                   │                                             │
│     Position                  │          ◆────────◆                          │
│       X                       │             ◆────────◆                       │
│       Y                       │             ◆────────◆                       │
│     Scale                     │          ◆────────────◆                      │
│       X                       │                                             │
│       Y                       │                                             │
│     Rotation                  │                                             │
├──────────────────────────────┴─────────────────────────────────────────────┤
│ view controls | horizontal scroll | time/frame mode | current frame          │
└────────────────────────────────────────────────────────────────────────────┘
```

默认尺寸：

| 项目 | 建议值 |
|---|---:|
| 默认 Dock 高度 | 300 px |
| 最小高度 | 180 px |
| 工具栏 | 32 px |
| 标尺 | 30 px |
| 行高 | 24 px；表达式错误行可扩展到 32 px |
| 底部状态栏 | 36 px |
| 左侧列默认宽度 | 330 px |
| 左侧列最小宽度 | 240 px |
| 左右分隔线 | 1 px |
| 关键帧菱形 | 7×7 px，命中半径 10 px |
| 播放头 | 1 px 竖线，顶部 8 px 三角形 |

左侧区域和右侧时间线必须共享垂直滚动 offset。用户滚动鼠标滚轮时，参数名和右侧轨道同步移动；左右不允许出现一个滚动、另一个不滚动的状态。左右的标尺头部固定，只有内容区滚动。

## 3.2 顶部工具栏按钮规格

所有按钮使用 `QToolButton` 或 `QAction`，不使用大面积文字按钮。按钮尺寸建议 `24×22 px`，图标尺寸 `14×14 px`，按钮之间保留 2-4 px 间距。

### A. 传输控制组

顺序：首帧、上一帧、播放/暂停、下一帧、尾帧。

| 按钮 | 图标建议 | Tooltip | 快捷键 | 状态 |
|---|---|---|---|---|
| 首帧 | `fast-backward` | `转到工作区起点` | `Home` | 普通按钮 |
| 上一帧 | `step-backward` | `上一帧` | `←` | 普通按钮 |
| 播放 | `play` / `pause` | `播放/暂停` | `Space` | checkable，播放时显示 pause |
| 下一帧 | `step-forward` | `下一帧` | `→` | 普通按钮 |
| 尾帧 | `fast-forward` | `转到工作区终点` | `End` | 普通按钮 |

播放按钮必须是唯一明显的 checkable 主按钮。播放时使用橙色或蓝色高亮，但不要使用会刺眼的纯红色。

### B. 关键帧导航组

| 按钮 | 图标建议 | Tooltip | 行为 |
|---|---|---|---|
| 上一个关键帧 | `backward` 或菱形左箭头 | `上一个关键帧` | 以当前选中参数为优先；无选中时搜索全局 |
| 下一个关键帧 | `forward` 或菱形右箭头 | `下一个关键帧` | 与上一个关键帧相反 |
| 添加关键帧 | `diamond-plus`，没有现成图标时用菱形 + 叠加加号绘制 | `在当前帧添加关键帧（Ctrl/Cmd+K）` | 对选中参数添加；无选中时对选中节点的动画参数添加 |
| 删除关键帧 | `diamond-minus` | `删除当前帧的关键帧` | 删除当前帧命中的选中轨道关键帧 |

关键帧按钮使用菱形意象，不使用普通 `+/-` 代替，避免用户误认为是在添加参数或节点。

### C. 属性显示组

| 控件 | 样式 | 行为 |
|---|---|---|
| Search | 180-220 px 的 `QLineEdit`，左侧放搜索图标 | 按节点名、参数显示名、表达式文本过滤 |
| Filter | `QToolButton` + 下拉菜单 | `全部属性`、`仅动画属性`、`仅已修改属性`、`仅表达式属性`、`仅选中节点` |
| 展开全部 | 叠层/向下箭头图标 | 展开所有节点的属性组 |
| 折叠全部 | 叠层/向上箭头图标 | 折叠所有节点 |
| 显示表达式 | `fx` 不是常驻按钮，而是菜单项/状态图标 | 显示或隐藏表达式行 |

`fx` 不再作为每个参数右侧的常驻大按钮。正常状态只显示一个很小的状态标记：有表达式时显示 `fx` 或 `=`，没有表达式时不占位置。编辑入口放在参数名右键菜单和关键帧按钮 `Alt/Option + 左键` 中。

### D. 视图和缩放组

| 按钮 | Tooltip | 行为 |
|---|---|---|
| 适配全部 | `将整个合成范围适配到视图` | 显示节点时间范围和全部关键帧 |
| 适配选中 | `将选中关键帧适配到视图` | 以选中关键帧为范围 |
| 放大 | `时间线放大` | 以鼠标位置或播放头为中心 |
| 缩小 | `时间线缩小` | 以鼠标位置或播放头为中心 |
| 居中播放头 | `将播放头居中` | 不改变当前帧，只改变水平 scroll |
| 吸附 | 磁铁图标，checkable | 吸附到整数帧、其他关键帧、工作区边缘 |

### 3.3 左侧列规格

左侧采用固定列 + 可伸缩名称列，避免参数名、色块、状态图标互相重叠。

| 列 | 默认宽度 | 说明 |
|---|---:|---|
| Lock | 22 px | 节点/参数轨道锁定；锁定后所有时间线编辑拒绝执行 |
| Visibility | 22 px | 节点的时间线显示状态，不等于渲染可见性；后续可映射到节点 bypass |
| Expand | 18 px | 节点和参数组的展开箭头 |
| Color | 8 px | 节点色条，不放在文字上；颜色条始终位于独立固定列 |
| Name | 自适应 | 节点名/参数名，过长使用省略号，Tooltip 显示完整名称 |
| Value | 72 px | 当前帧值；不可编辑时显示灰色，双击进入数值编辑 |
| Expression | 22 px | 只有存在表达式、表达式错误或 hover 时显示状态图标 |

重要规则：

- 参数名左侧色块与文字至少间隔 6 px。
- 状态图标不叠加在文字上。
- 节点名使用用户可读名称，不显示内部随机 ID、哈希或 UUID。
- 内部 ID 只用于模型、日志、AI/脚本 API 和 Tooltip 的调试信息。
- 节点名称冲突时，在第二行或 Tooltip 中显示类型，不把 ID 直接拼到名称后面。
- 参数行必须支持足够的左内边距：节点参数缩进 18-24 px，通道行再缩进 16 px。

### 3.4 行样式

| 行 | 背景 | 文字 | 交互 |
|---|---|---|---|
| 普通节点行 | `C.BG_PANEL` | `C.TEXT_SECONDARY` | 单击选节点，双击聚焦，右键节点菜单 |
| 选中节点行 | `C.BG_ACTIVE` | `C.TEXT_PRIMARY` | 保持选中高亮 |
| 普通参数行 | `C.BG_PANEL_ALT` | `C.TEXT_LABEL` | 单击选轨道，双击编辑当前值，右键参数菜单 |
| 选中参数行 | `C.BG_SELECTED` | `C.TEXT_PRIMARY` | 右侧轨道显示选中色 |
| 锁定行 | 原背景 + 低透明度 | `C.TEXT_HINT` | 关键帧变暗，不允许拖动 |
| 有表达式行 | 原背景 | 参数名右侧显示小 `fx` | 单击状态图标显示表达式摘要 |
| 表达式错误行 | 深红透明背景或红色左边框 | 参数名和错误图标使用 `C.ERROR` | Tooltip 显示错误，不阻塞其他轨道 |

建议用项目已有 `COMPOSITE_KEYFRAMES_*` 样式常量扩展，不在各个 widget 中散落硬编码颜色。

### 3.5 右侧时间线规格

右侧包含四种视觉元素：

1. 时间标尺。
2. 节点范围条。
3. 参数/表达式轨道。
4. 当前播放头和工作区。

时间标尺：

- 自动选择 1/2/5/10 倍数的主刻度。
- 当前显示模式可切换为帧、秒、时间码。
- 标尺标签不能互相重叠；宽度不足时自动减少标签密度。
- 鼠标滚轮默认水平缩放/平移，垂直滚轮只影响面板行滚动。
- `Alt/Option + 滚轮`：以鼠标位置为中心缩放时间线。
- `Shift + 滚轮`：水平滚动。

节点范围条：

- 使用节点颜色，但饱和度和明度降低，避免盖住关键帧。
- 左端显示起始帧，右端显示结束帧；文字宽度不足时隐藏文字。
- 拖动左边缘只修改 in frame，拖动右边缘只修改 out frame。
- 拖动中间区域整体移动范围；按住 `Shift` 保持长度，按住 `Alt/Option` 复制范围（后置功能）。

关键帧：

- 默认使用实心菱形，选中后变为橙色/白色描边菱形。
- Hold 使用方块或菱形内横线表示。
- Bezier 使用菱形加小弧线状态，不改变命中区域。
- 表达式驱动且没有显式关键帧的参数，在参数行显示表达式轨道背景，不绘制虚假的关键帧。
- 选中多个关键帧时，全部使用统一选择色，拖动任意一个移动整组。

播放头：

- 顶部是向下三角形，颜色使用项目强调色。
- 竖线覆盖标尺和所有轨道。
- 可以直接拖动三角形或竖线。
- 拖动播放头时不触发昂贵的 undo；鼠标释放后只发布一次当前帧变化。

---

## 4. 交互与快捷键规格

### 4.1 鼠标操作

| 操作 | 目标 | 行为 |
|---|---|---|
| 单击节点行 | 节点 | 选中节点，清空参数选择，右侧使用节点高亮 |
| 单击参数行 | 参数 | 选中参数轨道，作为 `K`、插值和表达式命令目标 |
| `Shift` 单击参数/关键帧 | 行或关键帧 | 加入多选 |
| `Ctrl/Cmd` 单击关键帧 | 关键帧 | 切换单个选择状态 |
| 框选 | 时间线空白区域 | 选中矩形内所有关键帧 |
| 拖动关键帧 | 单个或多选 | 水平移动；按吸附设置落到整数帧/邻近关键帧 |
| `Alt/Option` 拖动关键帧 | 关键帧 | 复制关键帧后拖动，保留原关键帧 |
| 双击空轨道 | 参数轨道 | 在当前帧添加关键帧，值取当前参数值 |
| 双击关键帧 | 关键帧 | 打开关键帧值编辑器或定位属性面板 |
| 右键关键帧 | 关键帧 | 插值、切线、复制、删除、设置时间和值 |
| 右键参数名 | 参数 | 编辑表达式、分离/合并维度、显示曲线、清除关键帧 |
| `Alt/Option + 左键` 关键帧按钮 | 参数行的关键帧按钮 | 打开表达式编辑器，不显示常驻 `fx` 按钮 |
| 拖动范围条左/右边缘 | 节点范围 | 修改入点/出点 |
| 拖动范围条中间 | 节点范围 | 整体移动节点有效范围 |
| 点击标尺 | 时间线 | 播放头跳转到对应帧 |
| 拖动播放头 | 播放头 | 连续 seek 当前帧，释放后同步预览 |

### 4.2 键盘快捷键

快捷键必须支持 macOS 的 `Command/Option` 和 Windows/Linux 的 `Ctrl/Alt` 对应。

| 快捷键 | AE 习惯 | Topos Color 行为 |
|---|---|---|
| `Space` | 播放 | 播放/暂停工作区 |
| `Home` / `End` | 首尾 | 跳到工作区起点/终点 |
| `←` / `→` | 逐帧 | 后退/前进一帧 |
| `J` / `K` | 前后关键帧 | 默认 AE 模式：`J` 跳上一个、`K` 跳下一个关键帧；在文本编辑框中不拦截 |
| `Ctrl/Cmd + K` | 添加关键帧 | 当前选中参数在播放头添加/更新关键帧；避免与 AE 的 `K` 导航冲突 |
| `I` / `O` | 工作区 In/Out | 设置工作区起点/终点 |
| `B` / `N` | Work Area | 设置工作区起点/终点的备用快捷键 |
| `U` | 已动画属性 | 只显示存在关键帧或表达式的属性 |
| `UU` | 已修改属性 | 显示不同于默认值或存在动画/表达式的属性 |
| `P` | Position | 在选中节点下展开并聚焦 Position/Translate |
| `S` | Scale | 展开并聚焦 Scale |
| `R` | Rotation | 展开并聚焦 Rotation |
| `A` | Anchor Point | 展开并聚焦 Pivot/Anchor |
| `T` | Opacity | 展开并聚焦 Opacity/Alpha |
| `EE` | Expressions | 按 AE 习惯只显示带表达式的属性；单独的 `E` 可作为 Topos 快速展开命令，但不替代 `EE` |
| `Ctrl/Cmd + A` | 全选 | 选中当前过滤结果中的关键帧 |
| `Ctrl/Cmd + C/V` | 复制/粘贴 | 复制和粘贴关键帧，粘贴锚定到播放头 |
| `Delete/Backspace` | 删除 | 删除选中关键帧 |
| `Ctrl/Cmd + Z/Y` | Undo/Redo | 使用 Composite undo stack |
| `F` | Fit | 适配选中关键帧或节点范围 |
| `+/-` | Zoom | 时间线放大/缩小 |
| `Enter` | 编辑 | 打开选中关键帧或当前值编辑器 |
| `Esc` | 取消 | 取消拖拽/编辑/表达式修改 |

快捷键实现要求：

- 使用 `QShortcut` 或 QAction shortcut，统一注册在 `KeyframesPanel`。
- 文本编辑、表达式编辑、数值 spinbox 获得焦点时，不拦截文本输入相关快捷键。
- 快捷键目标通过 `TimelineSelectionModel` 计算，不依赖当前 widget 的私有选中变量。
- 所有快捷键命令都必须有可测试的无 UI 方法。

### 4.3 上下文菜单

参数行右键菜单：

```text
编辑表达式…
清除表达式
显示/隐藏表达式行
────────────
分离维度 / 合并维度
显示到 Spline
────────────
在当前帧添加关键帧
删除当前帧关键帧
清除该参数全部关键帧
────────────
锁定轨道
仅显示该参数
```

关键帧右键菜单：

```text
编辑值…
复制
删除
────────────
插值
  Linear
  Bezier
  Hold
  Ease In
  Ease Out
  Ease In/Out
────────────
切线
  Auto
  Smooth
  Broken
  Linear
  Flat
  Step
────────────
打开 Spline 编辑器
设置前后外推…
```

表达式状态图标右键菜单：

```text
编辑表达式…
复制表达式
禁用表达式
清除表达式
查看错误详情
```

---

## 5. 统一数据模型和架构

### 5.1 唯一权威数据源

动画数据必须继续存储在节点实例级 `node.keyframe_tracks` 和 `node._expressions` 中。禁止把动画状态写回节点类级 `PARAM_DEFS`，避免两个同类节点互相污染。

渲染读取链：

```text
Node.keyframe_tracks / Node._expressions
        ↓
CompositeEditService / GraphSnapshot
        ↓
AnimationTrackSnapshot
        ↓
KeyframeEvaluator / ExpressionParser
        ↓
NodeGraph.snapshot(frame_number=...)
        ↓
Composite renderer / preview / export
```

UI 读取链：

```text
NodeGraph snapshot / lightweight editor state
        ↓
KeyframesModel
        ↓
TimelineRowViewModel + AnimationTrackViewModel
        ↓
KeyframesTreeView + KeyframesTimelineView + SplineView
```

### 5.2 ViewModel 数据结构

建议新增 `src/features/composite/ui/timeline_view_model.py`，不要把所有状态继续塞进 `keyframes_panel.py`。

概念数据结构：

```python
@dataclass
class TimelineRow:
    row_id: str
    node_id: str
    parameter_name: str | None
    row_type: Literal["node", "group", "parameter", "channel", "expression"]
    display_name: str
    parent_row_id: str | None
    depth: int
    color: str
    locked: bool
    visible: bool
    expanded: bool
    is_animated: bool
    has_expression: bool
    expression_error: str | None
    current_value: object | None
    channels: tuple[str, ...]

@dataclass
class AnimationSelection:
    keyframes: set[tuple[str, int]]
    rows: set[str]
    primary_row_id: str | None
    primary_frame: int | None

@dataclass
class TimelineViewState:
    current_frame: int
    start_frame: int
    end_frame: int
    work_in: int | None
    work_out: int | None
    pixels_per_frame: float
    horizontal_scroll: float
    vertical_scroll: int
    filter_mode: str
    show_expression_rows: bool
    snap_enabled: bool
    display_mode: str
```

这些类应当是纯数据或轻量状态对象，不依赖 QPainter 和 QWidget，便于测试、AI 查询和离屏验证。

### 5.3 通道和多维参数

参数分为三层：

```text
Scale (group track)
├── X (channel track)
└── Y (channel track)
```

规则：

- 标量参数只有一个参数轨道。
- vec2/vec3/颜色参数可以显示 group row 和 channel row。
- `linked` 状态表示用户在属性面板中修改一个通道时是否同步其他通道；这不是复制关键帧数据的借口。
- 每个 channel 仍然有自己的 `KeyframeTrack`，group 行只负责汇总显示和批量选择。
- Spline 可以显示单通道或同时显示多个通道，颜色使用固定通道色：X 红/橙、Y 绿、Z 蓝、Alpha 灰白。
- 分离/合并维度只改变 UI 组织方式和编辑策略，不丢弃已有通道数据。

### 5.4 表达式和关键帧优先级

统一定义：

1. 没有表达式时，参数值来自关键帧轨道或静态参数值。
2. 有表达式且表达式有效时，表达式结果覆盖关键帧结果。
3. 表达式中的 `value` 表示“该帧关键帧/静态值求值后的结果”，因此可以在表达式中继续加工动画值。
4. 表达式无效时，保留表达式文本，显示错误状态，并回退到关键帧/静态值；不让错误表达式把整个合成渲染变黑。
5. 表达式不自动删除关键帧。用户可以明确选择“保留关键帧并启用表达式”或“清除关键帧并启用表达式”。
6. UI 必须清楚显示当前结果是 `Keyframes`、`Expression` 还是 `Fallback`。

表达式行视觉：

- 有表达式：显示在参数行下方，行高 22 px，左侧显示 `fx`，右侧显示表达式摘要。
- 表达式过长只显示一行省略文本，Tooltip 显示完整文本。
- 错误：表达式行左边框红色，右侧显示错误图标；错误内容在 Tooltip 和上下文菜单中可见。
- 表达式行不创建常驻大按钮，不让面板宽度被 `fx` 控件占满。

---

## 6. 命令、Undo、AI 和脚本统一设计

### 6.1 TimelineCommand

建议新增 `src/features/composite/animation_commands.py` 或将命令扩展到现有 `edit_service.py`，核心要求是所有入口使用同一种 operation schema。

最小命令集合：

| 命令 | 参数 | 作用 |
|---|---|---|
| `timeline.add_keyframe` | node、parameter、frame、value | 添加/更新关键帧 |
| `timeline.remove_keyframe` | node、parameter、frame | 删除一个关键帧 |
| `timeline.move_keyframes` | selection、delta_frame | 批量移动关键帧 |
| `timeline.copy_keyframes` | selection | 放入内部剪贴板 |
| `timeline.paste_keyframes` | target_frame、target_node/parameter | 以播放头为锚点粘贴 |
| `timeline.set_interpolation` | selection、mode | 修改插值 |
| `timeline.set_tangent_mode` | selection、mode | 修改切线模式 |
| `timeline.set_bezier_handles` | keyframe、left/right | 修改贝塞尔手柄 |
| `timeline.set_extrapolation` | track、pre/post | 修改轨道前后外推 |
| `timeline.set_expression` | node、parameter、expression | 设置表达式 |
| `timeline.remove_expression` | node、parameter | 删除表达式 |
| `timeline.set_track_lock` | row、locked | 锁定/解锁轨道 |
| `timeline.set_node_range` | node、start、end | 修改节点有效范围 |
| `timeline.set_parameter_value` | node、parameter、frame/value | 设置当前值或关键帧值 |

每个命令必须：

- 在执行前验证节点、参数、类型、范围、frame 和权限。
- 返回 `before`、`after` 或等价的可恢复状态。
- 支持一次命令修改多个轨道，避免多选拖拽产生几十个分散 undo。
- 执行失败时不改变部分状态。
- 给 UI、AI 和脚本返回稳定的错误代码，而不是只返回字符串。

### 6.2 Undo 分组

以下操作各生成一个 undo 单元：

- 一次鼠标拖动移动的一组关键帧。
- 一次框选后批量删除。
- 一次插值模式批量修改。
- 一次表达式编辑器提交。
- 一次范围条整体移动。
- 一次粘贴关键帧。

鼠标拖动期间：

```text
press → capture before state
move  → only update preview state, no undo push
release → create one command and push undo
cancel/Esc → restore before state
```

### 6.3 AI/脚本可见能力

能力清单至少需要包含：

- `timeline.list_nodes()`
- `timeline.list_properties(node_id, filter=...)`
- `timeline.get_track(node_id, parameter_name)`
- `timeline.get_selection()`
- `timeline.set_selection(...)`
- `timeline.add_keyframe(...)`
- `timeline.move_keyframes(...)`
- `timeline.set_interpolation(...)`
- `timeline.set_bezier_handles(...)`
- `timeline.set_expression(...)`
- `timeline.remove_expression(...)`
- `timeline.set_work_area(...)`
- `timeline.query_at_frame(frame)`
- `timeline.open_spline(node_id, parameter_name)`（UI 请求，不修改数据）

AI 不应该通过模拟鼠标点击来修改动画。AI 可以模拟用户意图，但最终必须调用和 UI 相同的命令服务，这样才能保证：

- 参数验证一致。
- Undo 一致。
- 锁定状态一致。
- 保存和脏状态一致。
- 错误提示一致。
- 脚本、AI 和手工操作结果一致。

---

## 7. Spline 联动方案

### 7.1 角色划分

| 工具 | 主要职责 |
|---|---|
| Keyframes | 关键帧时间、选择、批量操作、表达式和工作区 |
| Spline | 值-时间曲线、Bezier 手柄、切线模式和外推曲线 |
| 属性面板 | 当前帧数值和快速参数设置 |

三者不允许分别拥有一套 selection 或 track copy。

### 7.2 双向联动

- 在 Keyframes 中选中参数：Spline 自动显示对应曲线。
- 在 Keyframes 中选中多个关键帧：Spline 只显示这些关键帧，或按设置显示整条曲线并高亮选择。
- 在 Spline 中选中/拖动手柄：Keyframes 高亮对应关键帧，数据通过同一命令更新。
- 在 Keyframes 中移动关键帧时间：Spline 的横坐标同步变化。
- 在属性面板中添加/删除关键帧：两个面板刷新，但不重置 selection。
- 当前帧改变：三个面板的播放头同步。

### 7.3 Spline UI 按钮

Spline 顶部保留与 Keyframes 相同的播放、适配、缩放和选择工具，同时增加：

| 按钮 | 样式 | 行为 |
|---|---|---|
| Auto Tangent | 曲线自动图标 | 自动计算平滑手柄 |
| Smooth | 连续切线图标 | 两侧手柄角度连续 |
| Broken | 断开切线图标 | 两侧手柄独立 |
| Linear | 直线图标 | 线性插值 |
| Flat | 水平线图标 | 平坦切线 |
| Step | 台阶图标 | Hold/阶梯效果 |
| Reset Handles | 回退箭头 | 恢复自动手柄 |

手柄命中测试优先于关键帧点命中测试，拖动手柄不得意外移动关键帧。按住 `Shift` 保持角度，按住 `Alt/Option` 只修改一侧手柄。

---

## 8. 分阶段实施计划

以下阶段按依赖顺序执行。每个阶段都必须通过自己的验收标准，才能开始下一阶段。

### P0：冻结基线和行为契约

目标：确定当前行为、避免大改造成不可定位的回归。

任务：

- 建立 Keyframes、Spline、属性面板、表达式、AI/脚本的现有测试基线。
- 记录当前面板的快捷键、信号和 `track_changed` 数据格式。
- 为所有关键帧操作补充命令级日志。
- 确认当前 `snapshot(frame_number=...)` 的所有调用路径。
- 明确项目支持的参数类型和多维参数清单。

涉及文件：

- `src/features/composite/ui/keyframes_panel.py`
- `src/features/composite/ui/keyframes_model.py`
- `src/features/composite/ui/keyframes_timeline_view.py`
- `src/features/composite/ui/spline_panel.py`
- `src/features/composite/ui/node_properties_panel.py`
- `src/features/composite/edit_service.py`
- `tests/ui/`
- `tests/composite/`

验收：现有相关测试全绿，形成可比较的基线报告。

### P1：统一动画轨道和 selection 数据层

目标：先统一数据，不先做外观。

任务：

- 新增 `TimelineRow`、`AnimationSelection`、`TimelineViewState`。
- 让 `KeyframesModel` 生成节点、参数、通道和表达式行。
- 把 timeline view、tree view、spline view 的 selection 接到共享模型。
- 将多维参数拆分为 group/channel 关系。
- 统一当前帧、工作区、显示模式和 zoom state。
- 保持旧 API 的兼容 wrapper，避免一次性破坏现有测试。

验收：

- 同一个关键帧在 Keyframes 和 Spline 中有稳定 row_id。
- 选中、清除、多选和主选择状态可以在两个面板之间同步。
- 参数行名称不会携带 node ID 或哈希。

### P2：建立统一 TimelineCommand 和 Undo 分组

目标：所有用户操作走同一条修改路径。

任务：

- 扩展 `CompositeEditService`。
- 实现关键帧移动、复制、粘贴、删除、插值和切线命令。
- 将现有 `track_changed` 转换成统一命令或兼容适配器。
- 为拖拽实现 press/move/release/cancel 生命周期。
- 将锁定轨道、表达式参数和不可动画参数验证放在服务层。
- 补充 before/after 状态和一次操作一个 undo 单元。

验收：

- 鼠标、快捷键、属性面板和脚本修改同一个参数后的结果完全一致。
- 拖动 20 个关键帧只生成一个 undo。
- `Esc` 取消拖动时数据恢复原状。

### P3：AE 式时间线布局和视觉重构

目标：先让面板结构像目标时间线，并消除叠加问题。

任务：

- 重构 `KeyframesPanel._build_toolbar()`。
- 扩展 `style_sheet.py` 中的 Keyframes 样式常量。
- 将左侧树改成固定列布局，色块、参数名、值、表达式状态分别占列。
- 保持标尺、左树表头和时间线横向对齐。
- 固定左右共享的垂直滚动 offset。
- 增加表达式状态行，但不增加常驻大 `fx` 按钮。
- 所有图标从 `icon_manager` 获取；缺失图标由项目内 QPainter 绘制简单几何图标。

验收：

- 参数名、色块和状态图标无重叠。
- 左右滚动同步，参数名不会因右侧滚动而永久跑出视图。
- 面板在 800 px、1200 px、1920 px 宽度下可用。
- 关键帧点、范围条和播放头不被行边界裁切。

### P4：AE 式高频交互

目标：让 AE 用户可以不依赖属性面板完成日常动画。

任务：

- 实现 `P/S/R/A/T/U/UU/EE/J/K/I/O/B/N`，并把 `Ctrl/Cmd+K` 定义为添加关键帧；`Shift+F3` 切换 Graph/Spline 视图。
- 实现当前帧双击加关键帧。
- 实现 `Alt/Option` 拖动复制关键帧。
- 实现框选、Shift/Ctrl 多选、整组拖动。
- 实现关键帧右键插值和切线菜单。
- 实现参数名右键表达式、分离/合并维度、显示 Spline。
- 实现当前值双击编辑和关键帧值编辑。
- 实现吸附开关和以鼠标为中心的时间缩放。

验收：

- 新用户仅看 Tooltip 就能完成添加、移动、复制、删除关键帧。
- 关键帧按钮普通点击、右键、Alt-click 三种行为互不冲突。
- 文本框内输入不被时间线快捷键干扰。

### P5：表达式完整进入 Keyframes

目标：表达式的添加、编辑、禁用、清除和错误查看都在时间线完成。

任务：

- 参数行显示无表达式/有表达式/错误三种状态。
- 右键参数名打开表达式编辑器。
- `Alt/Option + 左键` 关键帧按钮打开同一编辑器。
- 表达式行支持展开、折叠和摘要显示。
- 表达式编辑器增加自动完成、函数说明、当前参数和当前帧上下文提示。
- 表达式执行错误回退到关键帧值，并在行上显示错误。
- 增加表达式启用/禁用而不删除文本的状态。
- 通过统一命令进入 Undo 和 dirty state。

验收：

- 表达式可以只通过 Keyframes 面板创建和删除。
- 关键帧和表达式同时存在时结果遵循明确优先级。
- 错误表达式不会阻塞其他节点渲染。

### P6：Spline 深度联动

目标：Spline 成为真正的曲线编辑器，而不是独立的第二套动画工具。

任务：

- Spline 使用共享 selection。
- 选中关键帧后显示实际 Bezier 手柄。
- 支持 Auto、Smooth、Broken、Linear、Flat、Step。
- 支持拖动手柄、Shift 保持角度、Alt 只改一侧。
- 所有手柄操作通过统一命令进入 Undo。
- 时间线移动关键帧后曲线横坐标立即同步。
- 增加曲线和手柄的命中测试、越界处理和最小 handle 长度。

验收：

- Keyframes 与 Spline 显示同一关键帧的 frame/value/handle 数据。
- 手柄移动后预览、保存、加载、AI 查询结果一致。
- 多个通道可以独立显示和编辑。

### P7：AI/脚本完整自动化

目标：AI 和脚本可以完全操作时间线能力。

任务：

- 在 `composite_api.py` 增加 Timeline API。
- 在能力 manifest 中声明所有命令、参数类型和错误码。
- 提供查询接口：节点、参数、轨道、关键帧、表达式、当前值、工作区。
- 提供批量操作和 transaction，保证多个命令一起撤销。
- AI 修改前可以获取锁定状态和参数能力。
- AI 修改后返回变更摘要和受影响行。
- 脚本和 AI API 使用同一份 schema 文档。

验收：

- AI 可以在不模拟 UI 的情况下完成一段 Scale 动画。
- 脚本可以创建表达式、设置 Bezier 手柄并撤销。
- API 错误与 UI 错误使用同一套语义。

### P8：性能、持久化和兼容性

目标：面板在真实合成和大型工程中稳定工作。

任务：

- 对可见行和可见时间区域做绘制裁剪。
- 避免每个关键帧生成 QWidget。
- 关键帧拖动时只重绘受影响行和时间区。
- 对超大轨道启用关键帧点聚合/密度提示。
- 保存节点级轨道、表达式、切线、外推和 UI 偏好。
- 为旧项目提供 migration，不丢失旧的关键帧和表达式。
- 确认 render range 可以从关键帧和节点范围正确推导。

验收：

- 100 个节点、每节点 20 个参数、每参数 100 个关键帧时滚动和拖动可用。
- 保存/加载后 frame、value、interpolation、handles、expression 完整一致。
- 旧项目打开后不产生重复关键帧或错误表达式。

### P9：正式发布和交互锁定

目标：将 AE 式时间线作为正式产品能力，而不是实验面板。

任务：

- 更新中英文菜单、Tooltip 和快捷键帮助。
- 增加“AE 习惯快捷键”设置开关，允许用户关闭冲突快捷键。
- 编写用户文档和快捷键图。
- 增加首次使用提示：`J/K`、`Ctrl/Cmd+K`、`Alt/Option + 关键帧按钮`、右键菜单。
- 将旧属性小编辑器标记为兼容入口，并逐步迁移到统一逻辑。
- 做 macOS、Windows、Linux 三平台快捷键和字体测试。

验收：产品测试人员可以只通过 Keyframes 完成从添加动画到表达式和曲线微调的完整流程。

---

## 9. 文件级实施清单

### 9.1 需要重点修改的文件

| 文件 | 修改内容 |
|---|---|
| `src/features/composite/ui/keyframes_panel.py` | 工具栏、快捷键、命令路由、时间线状态、selection 连接 |
| `src/features/composite/ui/keyframes_model.py` | 行模型、channel/group、表达式行、过滤结果 |
| `src/features/composite/ui/keyframes_tree_view.py` | 固定列、行绘制、上下文菜单、状态图标、滚动同步 |
| `src/features/composite/ui/keyframes_timeline_view.py` | 关键帧/范围条/表达式行绘制、命中测试、拖拽、吸附、批量选择 |
| `src/features/composite/ui/node_properties_panel.py` | 参数行右键、Alt-click、统一命令调用、维度入口 |
| `src/features/composite/ui/spline_panel.py` | 共享 selection、统一命令、Spline 工具栏 |
| `src/features/composite/ui/spline_model.py` | 统一轨道数据、通道模型和曲线可见性 |
| `src/features/composite/ui/spline_view.py` | 手柄编辑、命中测试、切线工具 |
| `src/features/composite/ui/keyframe_editor.py` | 迁移为统一 command/track 适配器，避免双重逻辑 |
| `src/shared/keyframe.py` | 类型转换、插值、手柄、外推、序列化兼容 |
| `src/features/composite/edit_service.py` | 命令验证、批量修改、Undo/Redo、表达式和轨道状态 |
| `src/shared/ui/style_sheet.py` | 统一时间线颜色、按钮、行、状态和菜单样式 |
| `src/features/scripting/composite_api.py` | Timeline API 和 transaction |
| `src/features/edit/ai_editing/capability_manifest.py` | AI 能力声明和 schema |
| `src/features/composite/locales/zh_CN.json` | 中文 Tooltip、菜单、快捷键帮助 |
| `src/features/composite/locales/en_US.json` | 英文 Tooltip、菜单、快捷键帮助 |

### 9.2 建议新增的文件

| 文件 | 用途 |
|---|---|
| `src/features/composite/ui/timeline_view_model.py` | 纯数据 ViewModel、行和 selection |
| `src/features/composite/ui/timeline_commands.py` | UI 侧命令、拖拽 transaction 和 selection action |
| `src/features/composite/ui/timeline_context_menu.py` | 参数、关键帧、表达式上下文菜单构建 |
| `src/features/composite/ui/timeline_icons.py` | 菱形、表达式、锁、吸附等缺失图标的统一绘制 |
| `tests/ui/test_ae_timeline_interaction.py` | 快捷键、鼠标和 selection 测试 |
| `tests/ui/test_timeline_layout.py` | 离屏尺寸、列对齐、无重叠测试 |
| `tests/composite/test_timeline_commands.py` | 命令、事务、Undo、锁定和错误码测试 |
| `tests/composite/test_timeline_expression.py` | 表达式优先级、错误回退和保存/加载测试 |
| `tests/composite/test_timeline_spline_sync.py` | 时间线/Spline 双向同步测试 |
| `tests/edit/ai_editing/test_timeline_capabilities.py` | AI/脚本能力覆盖测试 |

---

## 10. 测试和验收矩阵

### 10.1 数据和渲染

- 单个 scalar 参数两点动画在第一个、中间、最后一个 frame 求值正确。
- int 参数最终值按参数类型转换，不出现 UI 显示和渲染值不一致。
- vec2/vec3 通道独立动画不互相覆盖。
- Hold 不插值，Linear 线性插值，Bezier 与 Spline 曲线一致。
- Ease In/Out 的结果在 UI 曲线、渲染和 AI 查询中一致。
- 表达式使用 `value` 时读取的是当前帧关键帧结果。
- 表达式报错时回退值正确，错误状态可见。
- 外推模式在轨道范围之外求值一致。

### 10.2 UI 布局

离屏测试至少覆盖：

- 800×180。
- 1200×300。
- 1920×420。
- 左侧树宽度 240、330、480 px。
- 中文长参数名、英文长参数名、重复节点名。
- 有/无表达式、表达式错误、锁定轨道。
- 0 个节点、1 个节点、100 个节点。

布局断言：

- 色块矩形与文字矩形不相交。
- 参数名与值列、表达式图标不相交。
- 标尺文字不发生严重重叠。
- 左树和右时间线行中心 y 坐标一致。
- 固定表头高度一致。
- 垂直滚动到最大值后，左侧参数仍然和右侧轨道对齐。

### 10.3 交互

- 普通左键关键帧按钮仍然只负责添加/删除关键帧。
- `Alt/Option + 左键` 关键帧按钮打开表达式编辑器。
- 右键参数名能打开对应 X/Y/Z 表达式入口。
- `U`、`UU`、`E` 过滤结果正确。
- `P/S/R/A/T` 只影响当前选中节点和当前面板焦点。
- `Shift/Ctrl` 多选不丢失已有 selection。
- 框选和拖动可以跨多个参数轨道。
- `Alt/Option` 复制拖动保留原关键帧。
- Delete、复制、粘贴、插值和切线修改均可撤销。
- 锁定轨道不允许任何鼠标、快捷键、AI、脚本修改。

### 10.4 Spline 同步

- 时间线选中关键帧后 Spline 高亮同一关键帧。
- Spline 拖动 handle 后 Keyframes 的状态标记和数值同步。
- 时间线移动 keyframe 后 Spline 横坐标同步。
- Spline 修改切线模式后右键菜单显示正确状态。
- 保存/加载后两面板仍指向同一轨道。

### 10.5 AI/脚本

- 能列出所有可动画参数及能力。
- 能查询当前帧和关键帧轨道。
- 能批量创建动画并使用一次 transaction 撤销。
- 能设置表达式并得到验证结果。
- 能修改 interpolation、tangent、handles、extrapolation。
- AI 不会写入内部随机 ID 到用户可见名称。
- 锁定参数、不可动画参数、非法 frame 和非法 expression 都返回稳定错误码。

---

## 11. 性能和线程安全要求

### 11.1 绘制性能

- 关键帧使用 QPainter 绘制，不为每个点创建 QWidget。
- 只绘制可见行和当前时间视口内的关键帧。
- 鼠标拖动时只刷新受影响行、旧位置、新位置和播放头区域。
- 标尺刻度根据 zoom 缓存，避免每个 mouse move 重建文字布局。
- 大量重叠关键帧可以聚合显示为密度柱或带数量标记的点。
- UI 线程只操作 ViewModel 和不可变/受控编辑状态，不直接调用 OpenGL。

### 11.2 渲染一致性

- 预览、播放、导出统一传入明确的 `frame_number`。
- UI 拖动期间可以使用轻量预览值，但提交后必须重新生成正式 snapshot。
- 表达式计算在明确的求值边界执行，禁止表达式直接修改 Node。
- 表达式异常不能污染 node 的持久状态。

### 11.3 线程安全

- CompositeEditService 的正式提交在 UI/graph 所属线程执行。
- 渲染线程只读快照，不读正在拖动的 mutable track。
- AI/脚本线程通过主线程调度命令，不直接写 UI 控件。
- Selection 是 UI 状态，不进入渲染快照；必要时作为会话偏好保存，不影响项目渲染结果。

---

## 12. 持久化和兼容性策略

项目文件需要保存：

- 节点级 `keyframe_tracks`。
- 每个关键帧的 frame、value、interpolation、tangent_mode、handles。
- track 的 pre/post extrapolation。
- 节点/轨道的表达式文本和启用状态。
- 节点有效范围和颜色。
- 可选的 UI 会话状态：展开行、筛选、时间单位、最后 zoom。

不保存或单独保存：

- 当前选中关键帧，默认作为工作区会话状态，不影响项目内容。
- 当前鼠标位置、拖动中间状态、临时剪贴板。
- 表达式错误文本；错误应在加载后重新验证生成。

迁移规则：

1. 旧的类级或 legacy keyframe 数据迁移到对应节点实例。
2. 同一 frame 的重复关键帧按最后写入或明确的迁移优先级合并，不允许迁移后继续重复。
3. 缺失 `tangent_mode` 设为 `auto`。
4. 缺失 handle 从 legacy bezier 或自动切线推导。
5. 旧表达式如果无法通过安全 parser 验证，保留原文本并显示错误，不静默删除。
6. 保存新版本后写入 schema version，旧版本仍可由 migration 读取。

---

## 13. 风险和规避方案

| 风险 | 后果 | 规避 |
|---|---|---|
| 一次性重写全部 UI | 回归难定位，用户无法使用 | 按 P0-P9 分阶段，每阶段保留兼容入口 |
| UI 和 AI 各自维护逻辑 | 同一操作结果不同，Undo 不一致 | 所有修改进入 CompositeEditService |
| Spline 复制轨道数据 | 两面板数据分叉 | 共享 track snapshot 和 selection |
| 把 node ID 显示给用户 | UI 复杂、用户困惑 | 名称层只显示可读名称，ID 仅内部使用 |
| 常驻 fx 控件过多 | 参数行拥挤、文字重叠 | 只显示状态图标，编辑入口走右键/Alt-click |
| 表达式错误阻断渲染 | 整个合成失效 | 表达式错误回退到关键帧/静态值 |
| 拖动每一帧写 undo | Undo 栈爆炸 | press/move/release 合并为单个命令 |
| 关键帧过多导致重绘卡顿 | 时间线不可用 | 可见裁剪、缓存标尺、点聚合、无 QWidget 点 |
| AE 快捷键和编辑器快捷键冲突 | 用户无法正常输入 | 按焦点上下文过滤快捷键 |
| 追求外观完全相同 | 受制于 AE 私有设计和资产 | 只实现公开、通用的交互范式和自有视觉系统 |

---

## 14. 推荐开发顺序和每次提交范围

建议按以下小步提交，每个提交都可以独立测试：

1. `timeline: add shared row and selection view model`
2. `timeline: route keyframe edits through composite edit service`
3. `timeline: rebuild left columns and expression status layout`
4. `timeline: add AE-style keyboard shortcuts`
5. `timeline: add direct expression editing and error state`
6. `timeline: add keyframe value/interpolation/tangent context actions`
7. `spline: share timeline selection and edit commands`
8. `timeline: expose scripting and AI capabilities`
9. `timeline: add large-project rendering optimizations`
10. `timeline: complete migration, docs, localization and release checks`

每次提交前必须运行：

```bash
python3 -m compileall src/features/composite src/features/edit/ai_editing src/features/scripting
pytest -q tests/project tests/composite tests/ui tests/edit/ai_editing
git diff --check
```

如果涉及 UI，额外运行离屏测试并检查：

- 无常驻冗余 `fx` 按钮。
- 列和行无重叠。
- 关键帧点 hit-test 与绘制位置一致。
- 左树和右时间线滚动保持一致。
- Alt/Option、右键和普通点击入口都可触发正确命令。

---

## 15. 最终完成定义

只有同时满足以下条件，才可以把 Keyframes 面板称为“AE 式可用时间线”：

- 用户可以只在 Keyframes 中完成一段 Transform/Blur/Sharpen/Ramp/Noise 等常用节点动画。
- 用户可以从参数行右键或 `Alt/Option + 关键帧按钮` 创建和编辑表达式。
- 用户可以在时间线上选择、拖动、复制、粘贴、删除关键帧，并一次性撤销。
- 用户可以用 `P/S/R/A/T/U/UU/J/K` 完成高频导航，并用 `Ctrl/Cmd+K` 添加关键帧。
- 用户可以在 Spline 中调整曲线手柄，并在 Keyframes 中看到同步结果。
- AI 和脚本可以查询和修改相同的轨道、表达式和手柄。
- 保存/加载、预览、播放和导出使用相同的动画求值结果。
- 参数名、颜色块、值列、表达式状态和关键帧轨道不会互相重叠。
- 大型合成中时间线仍然可滚动、可缩放、可拖拽。
- 所有关键功能均有自动化测试和至少一组真实素材手工验收。

这套完成定义代表的是“交互逻辑高度接近 AE，同时保留 Topos Color 节点式合成特色”，而不是简单换皮。

---

## 16. 面向无图片模型的 AE 截图文字复刻规范

本节是执行 UI 任务时的强制参考。后续执行者即使无法读取截图，也必须按照本节的空间关系、控件顺序、尺寸比例和颜色语义实现。截图只是视觉参考，本节才是可执行规格。

### 16.1 参考截图的画布和区域坐标

参考图大约为 `1242×394 px`。坐标原点在左上角，x 向右，y 向下。下面的坐标是参考尺寸下的近似值，实际实现要使用比例和布局约束适配窗口大小。

```text
y=0    ┌──────────────────────────────────────────────────────────────┐
       │ A. 面板标签/标题栏                                             │ 约 25 px
y=25   ├──────────────────────────────────────────────────────────────┤
       │ B. 时间显示、搜索、开关按钮工具栏                               │ 约 31 px
y=56   ├─────────────────────────────┬────────────────────────────────┤
       │ C. 左侧列标题/开关列          │ D. 时间标尺、工作区顶栏           │ 约 26 px
y=82   ├─────────────────────────────┼────────────────────────────────┤
       │ E. 图层/属性列表内容          │ F. 图层条、属性轨道、关键帧       │ 约 292 px
y=374  ├─────────────────────────────┴────────────────────────────────┤
       │ G. 底部状态栏、视图按钮、水平滚动、渲染时长                     │ 约 20 px
y=394  └──────────────────────────────────────────────────────────────┘
```

参考图的左侧和右侧分隔线约在 `x=537`，即左侧占总宽度约 43%。实际实现使用可拖动 splitter，默认比例 43:57；当窗口宽度小于 900 px 时，左侧最小宽度为 310 px，右侧最小宽度为 420 px。

### 16.2 A 区：面板标签/标题栏

参考图最顶部不是工具按钮，而是宿主面板的标签区域。它必须和时间线主体区分开，不能把标题直接塞进工具栏。

从左到右的结构：

1. 当前面板标签，例如 `渲染队列`，位于 x=0-118，文字为浅灰色 11 px。
2. 标签右侧有小 `×` 关闭按钮，命中区域 18×18 px，图标线宽 1 px。
3. 中间有一个小色块或面板图标，约 10×10 px，用于表示面板类型。
4. 合成标签，例如 `合成 1`，包含小图标、标题和一个菜单/面板选项按钮。
5. 标签区域底色为近黑色，底部使用 1 px 深灰边框。
6. 当前激活标签文字使用白色，非激活标签使用 `C.TEXT_HINT`。
7. 标签 hover 时只改变背景，不出现大面积高亮。

Topos Color 的对应实现：

- 如果 Composite 页面已经有自己的 tab/dock 容器，复用宿主 tab，不在 Keyframes 内再创建第二个 tab bar。
- 如果面板独立浮动，显示 `Keyframes` 标题和当前合成名称，例如 `Keyframes · Comp 1`。
- 当前合成名称只显示用户可读名称；内部 ID 不显示。
- 标签关闭、菜单和浮动/停靠属于宿主窗口行为，不要和关键帧命令混在一起。

### 16.3 B 区：时间显示、搜索和顶部开关

参考图第二行左侧从 x=0 到 x≈537，右侧时间线从 x≈537 开始。

#### B1. 当前时间显示

左上角显示两行信息：

```text
0:00:03:04   ← 大号蓝色时间码，约 17-18 px，等宽字体
00049 (29.97) ← 小号蓝色帧号/帧率，约 10 px
```

实现要求：

- 当前时间显示宽度固定在 104-112 px，避免数字变化时搜索框跳动。
- 时间码使用等宽字体；帧号使用 10 px 等宽字体。
- 可点击进入编辑状态，输入合法时间码或帧号后回车跳转。
- 时间码无效时使用 `C.ERROR` 边框，不修改当前帧。
- 当前帧变更信号只在提交或拖动节流后发出，不在每个字符输入时修改合成。
- 时间显示必须支持帧、秒、时间码三种模式，和底部单位选择保持同步。

#### B2. 搜索框

搜索框位于当前时间显示右侧，参考尺寸约 `280×20 px`。

- 背景比工具栏略亮，约 `#17181b` 或 `C.BG_INPUT`。
- 1 px 圆角边框，圆角不超过 3 px。
- 左侧 16 px 区域放放大镜图标。
- placeholder：`搜索属性或图层`。
- `Esc` 清空搜索；`Ctrl/Cmd+A` 只在搜索框内全选文本。
- 搜索匹配节点名、显示名称、参数名称和表达式摘要。
- 搜索结果保持父节点路径，避免只显示一个无法定位的参数。
- 无结果时在左侧内容区显示 `没有匹配的属性`，右侧不绘制假轨道。

#### B3. 开关/模式按钮

搜索框右侧是窄图标按钮组。参考图中的按钮属于时间线显示开关，不是关键帧编辑按钮。建议实现以下顺序：

| 顺序 | 图标几何 | 含义 | 默认状态 |
|---:|---|---|---|
| 1 | 三个连接点/流线 | 显示父子连接或节点关系 | 开启 |
| 2 | 小锁 | 仅显示已锁定/允许锁定列 | 开启 |
| 3 | 小人/独奏圆点 | 独奏当前节点或只显示选中节点 | 关闭 |
| 4 | 叠层圆形 | 显示合成/图层开关列 | 开启 |
| 5 | 曲线网格 | 打开 Spline 或曲线预览 | 关闭 |

每个按钮：

- 视觉尺寸 20×20 px，图标 12-14 px。
- normal 为灰色线稿，hover 背景为 `C.BG_HOVER`。
- checked 使用蓝色或橙色细边框，不使用整块高饱和背景。
- tooltip 必须明确“显示什么”，例如 `显示表达式轨道`，不能只写 `开关`。

### 16.4 C 区：左侧列标题和开关列

参考图的 y≈56-82 是列标题行。左侧不是空白，它包含 AE 用户熟悉的图层开关列。

从左到右建议固定为：

```text
0-20    Eye       是否参与预览/显示
20-40   Solo      独奏
40-60   Lock      锁定
60-80   Shy       隐藏该行/简化视图
80-100  Label     节点颜色标签
100-122 Index     用户可读层序号，可选
122-... Name     图层/节点名
...     Value     当前帧值
...     Expr      表达式状态
```

对 Topos Color 的适配：

- `Eye` 不应直接假装成 AE 图层渲染可见性。第一阶段定义为“时间线显示/参与预览”，并在 tooltip 中写明语义。
- `Solo` 第一阶段只控制时间线显示；当 Composite 渲染器支持节点 bypass/solo 后再映射到渲染。
- `Shy` 用于隐藏不重要的行，不能删除模型数据。
- `Lock` 必须是真正的轨道编辑锁，鼠标、快捷键、AI、脚本都要受到服务层约束。
- `Label` 是独立 8 px 色条，不允许绘制在文字底下或文字左侧重叠。
- `Index` 不是内部 ID；它是可选的用户序号，不保存随机 UUID。

列标题图标的绘制规则：

- 眼睛：椭圆轮廓 + 中央小圆点，线宽 1 px。
- 独奏：实心小圆或耳机/人形简化线稿。
- 锁：8×7 px 锁体 + 4 px 半圆锁梁，线宽 1 px。
- 隐藏：小斜杠或“眼睛带斜线”，不要使用与 Lock 相同图标。
- 标签：6×8 px 实心色块，边框 1 px。
- 父子连接：弯曲线/拾取鞭图标，命中范围至少 20×20 px。

### 16.5 D 区：右侧时间标尺和工作区顶栏

右侧时间线从左侧分隔线开始，不能在 y=56 的标尺位置绘制第一行关键帧。

参考图结构：

1. y≈56-64：顶部蓝色工作区条。
2. y≈64-82：时间刻度和数字。
3. 播放头顶部有蓝色倒三角形，尖端指向轨道。
4. 工作区条两端有可拖动的小手柄或竖向边缘线。
5. 时间数字采用 10-11 px 等宽字体，如 `00s`、`02s`、`04s`。
6. 竖向主网格线使用低对比度深灰；次网格线更暗。

工作区绘制：

```text
时间线顶部
┌────蓝色工作区范围──────────────────────────┐
│                                            │
└────────────────────────────────────────────┘
           ▼ 播放头
          │
00s   02s   04s   06s   08s   10s   12s
```

- 工作区外区域使用深灰遮罩，透明度约 25-35%，不完全遮住轨道。
- 工作区内保持原背景，不使用大面积蓝色填充。
- 播放头使用 `C.ACCENT_BLUE`；选中/拖动时可以加亮，不改变宽度。
- 时间标尺的主刻度线长度 7 px，次刻度线长度 3-4 px。
- 当前帧附近的刻度可以使用稍亮文字，但不能遮挡播放头三角形。
- 双击标尺空白区域可编辑工作区起点/终点，或通过 `I/O`、`B/N` 修改。

### 16.6 E/F 区：图层行、属性行和时间轨道

#### E1. 节点/图层行

参考图第一层节点行高度约 24 px，颜色条贯穿右侧时间区域，类似一条薄的素材条。

- 左侧节点行显示折叠箭头、节点颜色、名称和可选状态。
- 右侧节点范围显示为高度 16-18 px 的横条，垂直居中，左右各留 3 px。
- 横条颜色取节点色，但降低饱和度；选中时增加 1 px 高亮边框。
- 横条左上角显示起始帧或 `0`，右上角/右端显示结束帧；宽度不足时隐藏文字。
- 节点横条不是关键帧轨道，不绘制菱形关键帧。
- 点击节点横条选中节点；拖动横条中间移动范围；拖动边缘改变范围。

#### E2. 属性组行

例如 `Transform`：

- 左侧显示向下小三角形、秒表/动画状态图标、`Transform` 文本和 `重置` 操作。
- 右侧默认没有关键帧点，只在有 group track 或子通道时显示汇总范围。
- group 行点击名称只展开/折叠，不改变数值。
- group 行右键提供显示全部子属性、分离维度、显示 Spline。

#### E3. 属性行

例如 `锚点`、`位置`、`缩放`、`旋转`、`不透明度`：

```text
┌─▾─◷─位置─────────────── 960.0,540.0 ── ◎ ─┬──────◆────────◆──┐
│   ├ X                  960.0             │      ◆────────◆   │
│   └ Y                  540.0             │      ◆────────◆   │
```

- 秒表/动画按钮是参数行的主要状态入口；它应当是小型 14×14 图标，不是文字按钮。
- 无关键帧时显示灰色空心秒表/菱形；有关键帧时显示蓝色或橙色激活状态。
- 参数名和数值之间至少留 10 px 空间。
- 值列右对齐或使用等宽字体，确保小数点对齐。
- 表达式 pick whip/状态图标只在表达式行存在时出现，正常情况下不占用大量空间。
- 多维参数默认显示合并值；展开后显示 X/Y/Z 子行，每行独立轨道。
- 位置、缩放、锚点等参数可显示链接图标，但链接状态必须与实际数据模型一致。

#### E4. 表达式行

参考图中表达式行位于属性行下面，使用较暗的行背景，表达式文本以灰白色等宽字体显示。

- 左侧缩进与属性行一致再增加 16 px。
- 第一列显示 `=` 或小 `fx`，尺寸不超过 14 px。
- 中间显示表达式摘要，例如 `transform.anchorPoint`，过长时省略。
- 右侧显示错误图标、禁用图标或打开编辑器按钮。
- 表达式行高度 20-22 px；错误详情展开时最多 36 px。
- 不绘制每个表达式一个大按钮；双击表达式文本、右键和 Alt-click 都打开同一个编辑器。

#### F1. 关键帧形状

关键帧必须通过形状表达状态，不依赖颜色 alone：

| 状态 | 形状 | 边框 | 填充 |
|---|---|---|---|
| 普通关键帧 | 45° 菱形，7×7 px | 1 px 深色边 | 参数/节点强调色 |
| 选中关键帧 | 45° 菱形，9×9 px | 1 px 白色或强调色 | 强调色 |
| Hover | 45° 菱形，9×9 px | 1 px `C.TEXT_PRIMARY` | 变亮 |
| Hold | 方形或菱形内横线 | 1 px | 参数色 |
| 表达式驱动值 | 不绘制虚假菱形 | 行背景或 `=` 标记 | 无 |
| 锁定关键帧 | 普通形状 | 低透明度 | 低透明度 |

不要使用只有颜色差异的状态，因为深色主题和色觉差异会降低可识别性。

### 16.7 G 区：底部状态栏和水平滚动

参考图底部是一条非常窄的状态栏，不应该变成第二个工具栏。

从左到右：

1. 面板/视图切换小按钮，约 24×18 px。
2. 可选的层级/曲线/关键帧显示按钮。
3. 文本状态，例如 `帧渲染时间：0 毫秒`。
4. 中间是水平滚动条，滑块高度 8-10 px。
5. 右侧显示当前显示范围，例如 `Frames 0-98 (99)`。
6. 最右显示当前帧数字和时间单位下拉框。

样式：

- 背景比主编辑区略亮或相同，顶部 1 px 分隔线。
- 状态文字 10-11 px，颜色 `C.TEXT_HINT`。
- 水平滚动条轨道不可过亮，滑块使用 `C.BORDER_MID`，hover 使用 `C.TEXT_HINT`。
- 当前帧数字可点击编辑，宽度固定，数值变化不能让右侧控件跳动。
- 窗口宽度不足时优先保留水平滚动和当前帧，隐藏非关键状态文字。

### 16.8 颜色和字体 token

以下是基于参考图的近似视觉 token。实现时优先映射到 `C.*` 全局 token；若项目中不存在对应 token，再新增语义化常量，不要在绘制函数中直接散落十六进制颜色。

| 语义 | 参考色 | 用途 |
|---|---|---|
| `timeline_bg` | `#17181a` | 右侧时间轨道背景 |
| `timeline_bg_alt` | `#1d1f22` | 表达式行/次级背景 |
| `header_bg` | `#24262a` | 工具栏、标尺、列标题 |
| `left_panel_bg` | `#1c1d20` | 左侧列表 |
| `grid_major` | `#303338` | 主网格 |
| `grid_minor` | `#24262a` | 次网格 |
| `border` | `#3a3c42` | 1 px 分隔线 |
| `text_primary` | `#d4d7dc` | 当前名称和重要数字 |
| `text_secondary` | `#9ba0a8` | 普通文字 |
| `text_hint` | `#666b73` | 次级说明、禁用状态 |
| `accent_blue` | `#21a7ff` | 当前帧、时间码、工作区 |
| `accent_orange` | `#e8872f` | 关键帧选中、激活编辑 |
| `error` | `#e55353` | 表达式错误、非法输入 |

字体：

- 节点名称：系统 UI 字体 10-11 px。
- 参数名称：系统 UI 字体 10 px。
- 时间码、帧号、表达式摘要、数值：等宽字体 10-11 px。
- 工具栏标题：系统 UI 字体 11-12 px，半粗体。
- 不要在关键帧点、三角形或按钮内使用字体图标替代几何图标。

### 16.9 无图标字体时的 QPainter 绘制说明

如果 `icon_manager` 没有 AE 类似图标，新增 `timeline_icons.py`，使用 QPainter 绘制以下几何图标。每个图标都必须有 normal、hover、checked、disabled 四种颜色输入。

| 图标 | 绘制步骤 |
|---|---|
| 关键帧菱形 | 以按钮中心为原点，绘制四点 `(0,-4),(4,0),(0,4),(-4,0)` 的 `QPolygonF` |
| 关键帧加号 | 先绘制 7×7 菱形，再在右下角绘制 3 px 横线和竖线 |
| 播放 | 绘制三点三角形，左边垂直，右尖角；暂停时绘制两条 2 px 竖线 |
| 首帧/尾帧 | 播放三角形 + 一条 1 px 边界线 |
| 锁 | 绘制圆角矩形锁体，再绘制半圆锁梁；两者线宽一致 |
| 眼睛 | 绘制扁椭圆和中心点；隐藏状态叠加一条斜线 |
| 展开 | 绘制两条 1 px 折线，避免使用粗 Unicode 三角形 |
| 吸附 | 绘制磁铁 U 形轮廓和两端短竖线 |
| 表达式 | 正常用 `=` 两条线；错误状态加右上角 2 px 圆点 |
| 播放头 | 标尺上绘制 8 px 高倒三角，下面延伸 1 px 竖线 |
| Bezier | 两个小圆点、关键帧中心点和两条 1 px 手柄线 |

所有图标都要使用 device pixel ratio 进行整数像素对齐，避免 macOS Retina 下出现模糊半像素线。

---

## 17. 原子化实施任务表

本节将 P0-P9 拆成可以逐个完成和测试的任务。每个任务只解决一个主题；任务完成后必须运行对应验收，不要在一个任务中同时重写数据、UI 和渲染。

### 17.1 基线和数据契约任务

| ID | 任务 | 修改/新增文件 | 完成标准 |
|---|---|---|---|
| T00 | 记录当前 Keyframes/Spline/属性面板测试基线 | `tests/`、`docs/` | 测试命令、数量、失败项有记录 |
| T01 | 盘点所有动画参数和多维参数 | `base_node.py`、节点定义、测试 | 输出 scalar/int/bool/color/vec2/vec3/enum 清单 |
| T02 | 盘点所有 snapshot 当前帧调用点 | `node_engine.py`、`composite_page.py`、renderer | 每个渲染入口都有明确 `frame_number` 来源 |
| T03 | 固定用户可见名称和内部 ID 规则 | `keyframes_model.py`、节点显示模型 | UI 不显示哈希/UUID，API 仍保留稳定 ID |
| T04 | 定义统一 row_id 规则 | 新增 `timeline_view_model.py` | node/group/channel/expression 的 row_id 稳定且可逆 |
| T05 | 定义表达式和关键帧优先级 | `expression_parser.py`、snapshot、测试 | value、expression、fallback 行为有测试 |
| T06 | 定义 AE/Topos 快捷键模式 | `keyframes_panel.py`、设置模型 | 默认 AE 模式；旧 K 添加行为有兼容开关或迁移说明 |

### 17.2 统一 ViewModel 和 Selection 任务

| ID | 任务 | 修改/新增文件 | 完成标准 |
|---|---|---|---|
| T07 | 新建 `TimelineRow` 纯数据类 | `ui/timeline_view_model.py` | 不导入 QWidget/QPainter，可独立单测 |
| T08 | 新建 `AnimationSelection` | `ui/timeline_view_model.py` | 支持主选择、行选择、跨轨道关键帧选择 |
| T09 | 新建共享 `TimelineViewState` | `ui/timeline_view_model.py` | 当前帧、工作区、zoom、filter、scroll 集中管理 |
| T10 | 将现有 KeyframesModel 输出适配到 ViewModel | `keyframes_model.py` | 旧测试继续通过，新增 row 类型测试 |
| T11 | 让 TreeView 读取共享行模型 | `keyframes_tree_view.py` | 左侧显示节点/group/channel/expression 行 |
| T12 | 让 TimelineView 读取共享行模型 | `keyframes_timeline_view.py` | 右侧行 y 坐标与 TreeView 一一对应 |
| T13 | 让 SplinePanel 订阅共享 selection | `spline_panel.py`、`spline_view.py` | Keyframes 选中后 Spline 自动切换轨道 |
| T14 | 保留旧 API 兼容适配器 | `keyframes_panel.py`、`keyframe_editor.py` | 旧属性编辑器仍能操作同一条轨道 |

### 17.3 命令和 Undo 任务

| ID | 任务 | 修改/新增文件 | 完成标准 |
|---|---|---|---|
| T15 | 定义 `TimelineCommand` schema | `edit_service.py` 或 `timeline_commands.py` | 命令字段、错误码、before/after 明确 |
| T16 | 实现 add/remove keyframe 命令 | `edit_service.py` | UI/API 添加结果一致，同帧不重复 |
| T17 | 实现 move keyframes transaction | `edit_service.py` | 多选拖动一次 undo，支持取消恢复 |
| T18 | 实现 copy/paste command | `keyframes_timeline_view.py`、edit service | 粘贴以播放头为锚点，保留 interpolation/handles |
| T19 | 实现 interpolation/tangent command | `edit_service.py` | 批量修改可撤销，锁定轨道拒绝修改 |
| T20 | 实现 expression command | `edit_service.py` | 设置、禁用、删除均可撤销，错误不污染节点 |
| T21 | 实现 node range command | `edit_service.py` | 范围条移动一次 undo，边界和最小长度验证正确 |
| T22 | 将属性面板接入 command | `node_properties_panel.py` | 属性面板和时间线写入同一轨道 |
| T23 | 将 AI/脚本接入 command | `composite_api.py`、AI manifest | UI/API/AI 使用同一 schema |

### 17.4 AE 外观和布局任务

| ID | 任务 | 修改/新增文件 | 完成标准 |
|---|---|---|---|
| T24 | 重做顶部面板标题区域 | `keyframes_panel.py`、宿主 dock | 有标题/合成名称/关闭菜单的明确区域 |
| T25 | 实现当前时间码和帧号控件 | `keyframes_panel.py` | 双行蓝色时间显示，可编辑并校验 |
| T26 | 实现搜索框 | `keyframes_panel.py`、`keyframes_model.py` | 搜索节点、参数、表达式并保留父路径 |
| T27 | 实现显示开关按钮组 | `keyframes_panel.py`、`timeline_icons.py` | 关系、锁、独奏、曲线等状态独立可见 |
| T28 | 实现左侧固定列标题 | `keyframes_tree_view.py` | Eye/Solo/Lock/Shy/Label/Name/Value/Expr 对齐 |
| T29 | 实现节点行 | `keyframes_tree_view.py`、timeline view | 节点色条、名称和范围条不重叠 |
| T30 | 实现 group/channel 属性行 | `keyframes_tree_view.py`、timeline view | Transform/Scale/Position 可展开 |
| T31 | 实现表达式行布局 | `keyframes_tree_view.py`、timeline view | fx 状态只占固定小列，不再常驻大按钮 |
| T32 | 实现 AE 式时间标尺 | `keyframes_timeline_view.py` | 主次刻度、标签间距、工作区顶栏正确 |
| T33 | 实现工作区蓝色条和句柄 | `keyframes_timeline_view.py` | In/Out 拖动、外部状态同步 |
| T34 | 实现底部状态栏 | `keyframes_panel.py` | 水平滚动、显示范围、当前帧和单位不跳动 |
| T35 | 实现所有 token 和按钮状态 | `style_sheet.py`、`timeline_icons.py` | normal/hover/pressed/checked/disabled 一致 |
| T36 | 进行 800/1200/1920 宽度离屏布局检查 | `tests/ui/test_timeline_layout.py` | 色块、文字、值列、fx 列没有相交矩形 |

### 17.5 AE 交互任务

| ID | 任务 | 修改/新增文件 | 完成标准 |
|---|---|---|---|
| T37 | 实现 `J/K` 前后关键帧 | `keyframes_panel.py` | 以主选择为优先，空选择时全局搜索 |
| T38 | 实现 `Ctrl/Cmd+K` 添加关键帧 | `keyframes_panel.py` | 不和 `K` 导航冲突，文本框内不拦截 |
| T39 | 实现 `P/S/R/A/T` 属性聚焦 | `keyframes_panel.py`、model | 只展开当前选中节点对应属性 |
| T40 | 实现 `U/UU/EE` 过滤 | `keyframes_model.py` | 动画、已修改、表达式过滤结果正确；`EE` 与表达式错误筛选可区分 |
| T41 | 实现关键帧框选和多选 | `keyframes_timeline_view.py` | Shift/Ctrl 和框选组合行为稳定 |
| T42 | 实现 Alt/Option 复制拖动 | `keyframes_timeline_view.py`、command | 原关键帧保留，复制项生成一次 undo |
| T43 | 实现参数行双击值编辑 | `keyframes_tree_view.py` | 当前帧无 keyframe 时按明确策略添加/修改 |
| T44 | 实现关键帧右键菜单 | `timeline_context_menu.py` | 值、插值、切线、复制、删除菜单可用 |
| T45 | 实现参数名右键菜单 | `timeline_context_menu.py` | 表达式、维度、Spline、清除轨道可用 |
| T46 | 实现时间线吸附 | `keyframes_timeline_view.py` | 整数帧、邻近关键帧、工作区边缘可切换 |
| T47 | 实现以鼠标为中心的 zoom/pan | `keyframes_timeline_view.py` | 缩放后鼠标下的 frame 尽量保持不跳 |
| T48 | 实现播放头拖动节流 | `keyframes_timeline_view.py`、page | 拖动流畅，释放只产生一次提交状态 |

### 17.6 表达式任务

| ID | 任务 | 修改/新增文件 | 完成标准 |
|---|---|---|---|
| T49 | 参数行显示表达式状态 | `keyframes_model.py`、tree view | 无表达式/有效/错误/禁用四态可见 |
| T50 | 右键打开表达式编辑器 | `timeline_context_menu.py`、properties panel | 统一编辑器，带节点/参数/frame 上下文 |
| T51 | Alt/Option 点击关键帧按钮 | `node_properties_panel.py` | 单维和多维参数入口正确 |
| T52 | 表达式行摘要和折叠 | `keyframes_timeline_view.py` | 长文本省略，Tooltip 完整显示 |
| T53 | 表达式错误回退和诊断 | `expression_parser.py`、snapshot | 单参数失败不影响其他节点 |
| T54 | 表达式自动完成和帮助 | `node_properties_panel.py` 或独立 editor | 函数、上下文变量、valueAtTime 可提示 |
| T55 | 保存/加载表达式启用状态 | graph serialization/migration | 文本、状态、错误重新验证完整 |

### 17.7 Spline 和曲线任务

| ID | 任务 | 修改/新增文件 | 完成标准 |
|---|---|---|---|
| T56 | Spline 显示时间线主选择 | `spline_panel.py`、`spline_model.py` | 选择同步不重置 zoom |
| T57 | 绘制实际 Bezier 手柄 | `spline_view.py` | handle、keyframe、曲线命中区域互不混淆 |
| T58 | 实现 Auto/Smooth/Broken | `spline_view.py`、edit service | 切线模式修改可撤销 |
| T59 | 实现 Linear/Flat/Step | `spline_view.py`、keyframe evaluator | 曲线显示和渲染求值一致 |
| T60 | 实现手柄拖动约束 | `spline_view.py` | Shift/Alt 行为可测试，不能误移动关键帧 |
| T61 | 实现时间线/Spline 双向刷新 | 两个 view + selection model | frame/value/handles 一致 |

### 17.8 AI、持久化和性能任务

| ID | 任务 | 修改/新增文件 | 完成标准 |
|---|---|---|---|
| T62 | 声明 Timeline 查询能力 | `capability_manifest.py` | AI 能列节点、参数、轨道和表达式 |
| T63 | 声明 Timeline 修改能力 | `capability_manifest.py`、API | AI 能创建动画、插值、表达式和手柄 |
| T64 | 实现批量 transaction | `composite_api.py`、edit service | 一批 AI 操作可以一次撤销 |
| T65 | 实现锁定状态的服务层验证 | `edit_service.py` | UI/AI/脚本均不能绕过锁 |
| T66 | 完成节点级动画迁移 | graph migration/serialization | 旧项目不丢动画、不生成重复关键帧 |
| T67 | 完成 UI session 状态 | Composite page/dock state | 展开、filter、zoom 可选保存，渲染数据不受污染 |
| T68 | 实现可见区域裁剪 | timeline view | 只绘制可见行和可见 frame 区域 |
| T69 | 实现大轨道关键帧聚合 | timeline view | 高密度轨道不创建 QWidget、不明显卡顿 |
| T70 | 完成高 DPI 检查 | `timeline_icons.py`、离屏测试 | Retina/非 Retina 图标清晰、线条不模糊 |
| T71 | 完成本地化 | `zh_CN.json`、`en_US.json` | Tooltip、菜单、错误和快捷键帮助完整 |
| T72 | 完成真实素材验收 | `tests/` 和手工测试记录 | Transform、Blur、Sharpen、Constant、Merge、Ramp、Noise 均可动画 |
| T73 | 实现时间线 Marker/Region | `keyframes_timeline_view.py`、timeline model | 可添加、移动、重命名、删除标记；不影响关键帧数据 |
| T74 | 明确 Parent/Child 和 Bypass 语义 | 节点模型、timeline view、edit service | 关系列显示与渲染语义分离，未实现的开关不会伪装成已生效 |
| T75 | 实现列显示配置 | `keyframes_panel.py`、session state | 第一版固定列可隐藏/恢复，列宽可拖动且不破坏最小宽度 |
| T76 | 完成主题 token 迁移 | `style_sheet.py`、timeline icons | 新增颜色全部使用语义 token，深色主题下无硬编码散色 |
| T77 | 完成键盘和可访问性检查 | UI tests、Tooltip、focus policy | 图标按钮有 tooltip/accessible name，键盘可聚焦，颜色不是唯一状态提示 |
| T78 | 完成跨平台输入和窗口检查 | macOS/Windows/Linux 测试记录 | Command/Option 与 Ctrl/Alt、DPI、字体和鼠标行为符合平台习惯 |
| T79 | 评估 AE 数据导入/导出 | 独立 format mapping 文档和测试 | 只在确认需求后实现映射；不把内部 schema 伪装为 AE 文件 |

### 17.9 每个原子任务的固定完成模板

执行任意 Txx 任务时，必须在任务记录中写明：

```text
任务 ID：Txx
目标：只描述一个行为
依赖：前置 Txx 列表
修改文件：绝对路径或项目相对路径
数据变化：是否改变持久化 schema
UI 变化：按钮/行/快捷键变化
测试：新增或更新的测试
验收：可观察、可执行的完成条件
回退：失败时如何恢复
```

如果一个任务同时需要修改渲染器、UI、数据格式和 AI API，应拆成多个 Txx，不能继续扩大任务范围。

---

## 18. 二次遗漏审查清单

在补充截图规格和原子任务后，再次检查整个方案，以下内容必须在实现前明确：

### 18.1 已补齐的遗漏

- 面板标签/标题区与工具栏的分界。
- 当前时间码、当前帧号和帧率的双行显示。
- 搜索框的尺寸、位置、placeholder、清空和匹配范围。
- AE 风格左侧 Eye/Solo/Lock/Shy/Label/Name/Value/Expression 列。
- 节点颜色条的固定列和名称不显示随机 ID 的规则。
- 时间线顶部工作区蓝色条、工作区外遮罩和拖动手柄。
- 时间标尺主刻度、次刻度、标签密度和播放头三角形。
- 节点范围条、Transform group 行、channel 行和表达式行的具体高度与缩进。
- 关键帧菱形、Hold、选中、锁定和表达式驱动状态的几何区别。
- 底部状态栏、水平滚动、显示范围和当前帧输入。
- normal/hover/pressed/checked/disabled 的按钮状态。
- 没有图标资源时的 QPainter 几何绘制方案。
- Retina/高 DPI 下的整数像素对齐。
- `J/K` 与添加关键帧快捷键冲突的解决方案。
- 宿主 dock tab、Composite tab、Keyframes 内容区三者的边界。
- node graph 语义和 AE layer 开关语义不一致时的适配规则。
- AI/脚本/鼠标/快捷键都必须通过同一命令服务。
- Undo transaction、拖动取消、锁定验证和错误回退。
- 离屏布局、命中测试、性能、大项目、真实素材和跨平台测试。

### 18.2 仍然需要在编码前确认但不阻塞 UI 方案的事项

这些事项不应被遗忘，但可以在对应 Txx 任务中确认，不需要提前暂停整个计划：

- Composite 节点是否最终需要真正的 parent/child 变换关系；在此之前只显示关系列，不修改渲染语义。
- `Eye/Solo/Shy` 是只影响时间线显示，还是以后映射到节点 bypass；两者必须由设置或数据模型明确区分。
- Marker/Region 已作为独立 `T73`，不能混入关键帧基础任务。
- 是否需要音频波形；Composite 当前没有音频数据模型，不能只为模仿 AE 添加空波形。
- 是否需要导入/导出 AE 数据；如果确认需要，使用独立 `T79` 格式映射任务，不能把内部 schema 直接伪装成 AE 文件。
- 用户自定义列顺序和列宽由 `T75` 处理；第一版先固定列顺序，再允许隐藏/调整宽度。
- 主题切换由 `T76` 处理；所有新增颜色必须走语义 token，为未来主题切换保留空间。
- 键盘导航、accessible name、DPI 和跨平台组合键由 `T77/T78` 处理，不能等到发布后才补。
- 是否需要完整表达式语言兼容；当前先保证安全 parser、value、time、frame、comp 上下文稳定。

### 18.3 最终执行闸门

每完成一个任务，按以下顺序验收：

1. 纯数据测试。
2. 命令/Undo 测试。
3. 离屏 UI 测试。
4. 真实 Qt 交互测试。
5. Composite 预览/播放测试。
6. 保存/加载测试。
7. AI/脚本调用测试。
8. `git diff --check` 和回归测试。

只有前一个 Txx 的验收通过，才开始下一个依赖它的任务。这样即使未来使用没有识图能力的模型，也可以依靠本方案的坐标、组件、命令、文件和验收条件逐步完成整个 AE 式时间线。

---

## 19. Adobe 官方行为核对和 Topos Color 映射

本节根据 Adobe 官方 After Effects 文档补充，不是对截图的主观猜测。实现时应优先遵循“交互语义”，再使用 Topos Color 自己的颜色、图标和节点名称。

### 19.1 AE 时间线的基本分区

Adobe 对 Timeline 的定义是：左侧是图层控制列，右侧是时间图，包括时间标尺、标记、关键帧、表达式、图层持续时间条和 Graph Editor；当前时间同时显示在左上角的 current-time display，CTI 是时间图中的垂直线。

Topos Color 的强制映射：

| AE 概念 | Topos Color 对应 | 实现规则 |
|---|---|---|
| Layer | Composite node | 用户看到 node display name，不看到内部 ID |
| Property | Node parameter | 参数有 ParamDef、类型、动画能力和表达式能力 |
| Property group | 参数组/通道组 | 例如 Transform、Scale、Position、Blur |
| A/V switches | Node/track switches | 必须在 tooltip 中说明哪些只影响时间线，哪些影响渲染 |
| Time graph | Keyframes timeline | QPainter 绘制，和左侧行模型共享 row y |
| CTI | Current frame playhead | 同步 CompositePage、预览、播放和 Spline |
| Layer bar mode | Timeline mode | 显示节点范围条、参数行、表达式行和关键帧 |
| Graph Editor | Spline/Graph mode | 显示 value/speed graph 和手柄 |

官方参考：

- [After Effects composition basics — Timeline panel overview](https://helpx.adobe.com/after-effects/desktop/work-with-compositions/composition-settings/composition-basics.html)

### 19.2 Layer switches、锁定、Solo、Shy 和颜色标签

官方语义不能简单等同为“左边画几个按钮”：

- Lock 会阻止图层被选择或编辑；Topos 的 Lock 必须由服务层强制执行，不能只在 UI 上变灰。
- Solo 会排除同类型的其他层参与预览和最终输出；Topos 第一版可以只实现“时间线独奏”，但按钮和 tooltip 必须明确说明尚未映射到渲染，不能产生误导。
- Shy 只隐藏时间线中的行，仍然参与预览和最终渲染；它和 Eye/Visibility 不能共用一个状态。
- Label 是组织层、合成、素材和关键帧的颜色标签；Topos 至少要支持节点标签色和关键帧/轨道色的区分。
- 官方允许显示/隐藏列，并且可以重新排列列；Topos 第一版可以固定顺序，但必须留下 ColumnModel 和列配置入口。
- AE 支持对相邻行拖动开关以批量改变状态；Topos 可以在后期加入列拖刷，但不能把单击状态和拖拽批量状态混为一谈。

Topos 的数据拆分：

```text
NodeRenderState
├── render_enabled       # Eye/Video，影响渲染
├── solo_enabled         # Solo，是否参与独奏渲染
├── timeline_shy         # Shy，只影响时间线显示
├── timeline_locked      # Lock，禁止编辑
└── label_color          # 组织颜色

TrackEditorState
├── selected
├── expression_visible
├── graph_editor_enabled
└── channel_visible
```

官方参考：

- [Managing layers in After Effects](https://helpx.adobe.com/after-effects/desktop/work-with-layers/manage-layers/layers.html)
- [Adjust layer properties and parenting in After Effects](https://helpx.adobe.com/after-effects/desktop/work-with-layers/layer-properties/layer-properties.html)

### 19.3 属性显示、搜索和 Reveal 逻辑

AE 的属性显示不是简单的“展开全部”：

| AE 操作 | 语义 | Topos 方案 |
|---|---|---|
| `U` | 显示选中层中存在关键帧或表达式的属性 | 只显示有 keyframe track 或 expression 的参数 |
| `UU` | 显示选中层中被修改过的属性 | 显示非默认值、动画、表达式或节点范围改变的参数 |
| `EE` | 显示表达式属性 | 只显示有 expression 的属性；可以新增错误子过滤 |
| Search | 搜索名称、属性组、表达式内容 | 命中表达式文本时必须保留父组和节点路径 |
| Show Selected Properties | 只显示用户选中的属性 | 对应 Timeline selection 的 row set |
| Show Animated Properties | 显示有动画的属性 | 对应 `U`，不要把静态表达式误判为 keyframe |
| Show Graph Editor Set | 显示被 graph switch 标记的属性 | 对应 track 的 `graph_editor_enabled` |

搜索和过滤的结果必须是“可操作上下文”，不能只返回匹配行：

- 如果子参数命中，父节点和参数组必须保留。
- 如果表达式文本命中，表达式行和所属参数必须保留。
- 如果过滤后没有可见关键帧，右侧仍可显示表达式行或当前值，不绘制空菱形。
- `U/UU/EE` 的过滤状态显示在工具栏，避免用户不知道当前为何看不到参数。

官方参考：

- [Adjust layer properties in After Effects](https://helpx.adobe.com/after-effects/desktop/work-with-layers/layer-properties/layer-properties.html)
- [Editing expressions and Timeline search](https://helpx.adobe.com/after-effects/desktop/work-with-expressions/edit-expressions/edit-expressions.html)

### 19.4 AE 关键帧插值不是一组互相独立的表达式按钮

官方说明指出，AE 的插值方法都基于 Bezier；Linear、Hold 等是受约束的形式。对于空间属性和时间属性，AE 还区分不同的插值维度。

因此 Topos 的数据模型不能只用一个 `interpolation: "ease_in"` 字符串来表达完整状态。建议增加：

```python
@dataclass
class KeyframeInterpolation:
    temporal_in: str       # linear | bezier | hold | auto_bezier | continuous_bezier
    temporal_out: str      # linear | bezier | hold | auto_bezier | continuous_bezier
    spatial_in: str | None # spatial properties only
    spatial_out: str | None
    tangent_mode: str      # auto | smooth | broken | linear | flat | step
    roving: bool           # spatial property timing policy
    ease_in: float | None
    ease_out: float | None
    influence_in: float | None
    influence_out: float | None
```

兼容策略：

- 现有 `InterpolationType` 保留作为旧文件和旧 API 的简写。
- UI 新建关键帧时默认生成完整 temporal/spatial 状态。
- `Hold` 必须明确是 outgoing temporal interpolation；它表示从当前关键帧到下一个关键帧保持不变。
- Linear、Bezier、Auto Bezier、Continuous Bezier、Hold 需要作为第一等菜单项。
- `Ease In/Out` 在 UI 中可以作为快捷预设，但底层必须转成可解释的 temporal Bezier/speed/influence 数据，而不是只写一个“ease”标签。
- 对 Position、Anchor Point 等空间参数，必须同时决定 motion path/spatial interpolation 和 value/speed graph 的显示方式。
- 对 Blur、Opacity 等时间标量，默认使用 value graph。

官方参考：

- [Keyframe interpolation in After Effects](https://helpx.adobe.com/after-effects/desktop/animate-in-after-effects/animation-keyframes/keyframe-interpolation.html)
- [Controlling speed between keyframes](https://helpx.adobe.com/ca/after-effects/using/speed.html)

### 19.5 Roving、Easy Ease、Influence 和速度图

这是原计划中最容易遗漏、但 AE 用户非常依赖的高级动画能力。

#### Roving keyframes

- Roving keyframe 不固定自己的时间，而是根据前后关键帧调整时间，使空间运动速度更平滑。
- 它只对合适的空间属性开放，不能对所有 scalar 参数显示。
- UI 在关键帧右键菜单显示 `Rove Across Time`、`Lock To Time` 和 `Current Settings`。
- Roving 后时间线上的 frame 可能改变，必须在 UI、保存、Spline 和渲染中保持一致。
- AI/脚本修改 roving 必须返回前后 frame 变化摘要。

#### Easy Ease

- Easy Ease 是一个速度/影响力预设，不是表达式。
- UI 可以提供 `Easy Ease`、`Ease In`、`Ease Out`、`Easy Ease In/Out` 四个快捷按钮。
- 应用预设后，菜单仍然允许用户进入详细的 speed/influence 对话框。
- 预设必须可逆，Undo 可以恢复应用前的 handle/influence/temporal 状态。

#### Graph handle

- Value Graph 中上下移动关键帧改变值。
- Speed Graph 中上下移动关键帧或 handle 改变速度/加速度。
- 手柄远离关键帧表示更大的 influence，靠近关键帧表示更小的 influence。
- Alt/Option 拖动可以拆分/合并入射和出射 handle。
- 自动缩放曲线高度时，必须保留当前选中关键帧和鼠标位置，不让曲线跳出视图。

### 19.6 Graph Editor/Spline 必须有明确的图类型

AE 根据属性自动选择 graph type：Position 等空间属性默认 speed graph，Opacity 等时间属性默认 value graph；用户可以手动切换，也可以显示未选中类型的 reference graph。

Topos Spline 的顶部或底部必须增加一个 Graph Type 菜单：

```text
Graph Type
├── Auto Select
├── Edit Value Graph
├── Edit Speed Graph
├── Show Reference Graph
├── Show Post-Expression Graph
├── Show Layer In/Out Points
└── Show Markers
```

状态和绘制：

- Value Graph 纵轴是参数值，X 轴是时间。
- Speed Graph 纵轴是每秒变化速度，X 轴是时间。
- 多通道曲线使用固定通道色：X 红/橙、Y 绿、Z 蓝、Alpha 灰白。
- 未选中的 reference graph 使用低透明度灰色。
- Post-Expression Graph 使用高亮颜色，pre-expression graph 使用暗色，避免用户误以为关键帧被修改。
- Position/Anchor 等空间属性提供 Separate Dimensions 操作；分离后 X/Y/Z 是独立 row 和 track。
- Graph Editor 视图切换使用 `Shift+F3`，但 Topos 可以保留一个明确的图标按钮。

官方参考：

- [Animation basics and Graph Editor options](https://helpx.adobe.com/after-effects/desktop/animate-in-after-effects/animation-basics/animation-basics.html)
- [Control speed with the Speed Graph](https://helpx.adobe.com/ca/after-effects/using/speed.html)

### 19.7 表达式编辑器应接近 AE 的“属性行下方编辑”逻辑

官方 AE 表达式编辑行为包括：表达式字段位于属性下方；可调整字段高度；有 Expression Language 菜单；表达式内容可搜索；可以只 reveal expression errors；Graph Editor 里可以显示 expression graph 和 post-expression graph。

Topos 实施要求：

- 表达式字段属于参数行的子行，不弹出一个完全脱离时间线的永久独立窗口。
- 双击表达式摘要进入编辑；进入编辑时默认全选，但点击文本中间后可以插入。
- `Enter` 提交，`Esc` 取消；多行编辑器支持拖动上下边缘调整高度。
- 提供 `Expression Language` 菜单，至少包含项目实际支持的函数、上下文变量和示例片段。
- 提供 pick whip 或“插入参数引用”按钮，但必须生成安全、可解析的项目表达式，不直接插入不可支持的 AE 对象。
- `EE` 显示所有表达式属性；额外提供 `Reveal Expression Errors`。
- 表达式错误保留源文本，回退到 keyframe/static value，并给出行级错误。
- 显示 pre-expression value graph 和 post-expression result graph 时，使用不同明度和图例。
- 明确区分 script 和 expression：script 改变应用状态，expression 只求值当前参数。

Topos 表达式兼容表：

| AE 概念 | Topos 第一阶段 | 后续扩展 |
|---|---|---|
| `value` | 当前帧关键帧/静态值 | 完善 valueAtTime 采样 |
| `time` / `frame` | 支持 | 子帧时间 |
| `thisComp.width/height` | 支持 comp context | duration、frameDuration |
| `thisLayer` | 映射到 node context | 节点名称和参数路径 |
| `wiggle` | 支持安全实现 | seed/loop 兼容 |
| `linear/ease` | 支持安全实现 | 更完整的数组/颜色类型 |
| pick whip | 生成 Topos parameter reference | 跨节点依赖图和循环检测 |
| Expression Language menu | 项目自有函数菜单 | AE 语法兼容子集 |

官方参考：

- [Editing expressions in After Effects](https://helpx.adobe.com/after-effects/desktop/work-with-expressions/edit-expressions/edit-expressions.html)
- [Understanding the expression language](https://helpx.adobe.com/in/after-effects/using/expression-language.html)
- [Expression language reference](https://helpx.adobe.com/in/after-effects/using/expression-language-reference.html)

### 19.8 Marker、吸附和时间参考点

AE 同时支持 composition markers 和 layer markers；marker 可以是点，也可以是有 duration 的区间，并可作为拖动关键帧、CTI、图层范围条时的吸附目标。

Topos 需要区分：

```text
Composition Marker
└── 位于合成时间标尺上，所有节点可见

Node Marker
└── 位于某个节点范围条上，只对该节点有语义
```

每个 marker 至少包含：

- frame/time。
- duration，可为 0。
- comment/name。
- optional color。
- optional metadata dictionary，为以后章节点、链接、AI 标记留接口。

交互：

- 标尺上用小倒三角形绘制 composition marker。
- 节点范围条上用半菱形/竖线绘制 node marker。
- 双击或右键打开 marker 设置。
- 拖 marker in 改时间，拖 marker out 改 duration，不能一次拖动同时修改两者。
- Shift 拖动关键帧、播放头、范围条时吸附到 marker、keyframe、In/Out、work area 和 composition 边缘。
- 同一 composition frame 不允许多个 composition marker；新增时替换或提示，规则必须固定。

官方参考：

- [Layer and composition markers in After Effects](https://helpx.adobe.com/after-effects/desktop/work-with-layers/work-with-layer-and-composition-markers/layer-markers-composition-markers.html)

### 19.9 图层 In/Out/Duration/Stretch 和节点范围

AE 时间线不仅显示 duration bar，也可以在列中显示 In、Out、Duration、Stretch，并允许直接编辑数字；Alt/Option 点击数字可以把 In/Out 移到当前时间。

Topos 当前节点有 `time_range`，因此需要把它拆成：

| AE 字段 | Topos 字段 | 第一版行为 |
|---|---|---|
| In | `time_range.start` | 可编辑数字、可拖边缘、可吸附 |
| Out | `time_range.end` | 可编辑数字、可拖边缘、可吸附 |
| Duration | `end-start+1` | 只读计算值或可通过边缘修改 |
| Stretch | 节点时间重映射 | 暂不实现，预留列和命令 |

对于媒体输入节点，后续可增加 source in/out 和 remap；对于纯效果节点，不显示 Stretch 假控件。

官方参考：

- [Selecting and arranging layers / In and Out points](https://helpx.adobe.com/after-effects/using/selecting-arranging-layers.html)

### 19.10 AE 复制粘贴和关键帧值编辑语义

官方行为中，复制跨多个图层的关键帧时不新建图层；粘贴到目标层对应属性，最早关键帧锚定到当前时间，其他关键帧保持相对时间，并且粘贴后关键帧仍保持选中。

Topos 的实现规则：

- 内部剪贴板数据带有 source node/parameter display path、relative frame、value、type、interpolation、handles、expression metadata。
- 粘贴到同类型目标参数时保留相对时间和动画数据。
- 粘贴到不同类型参数时给出类型不匹配提示，不能静默截断 vec3 或把颜色当 scalar。
- 粘贴后新关键帧成为当前 selection。
- 复制/粘贴跨节点时不复制节点本身，除非未来提供单独“复制节点和动画”命令。
- 右键关键帧菜单第一项可以显示 `Edit Value`，显示该关键帧的时间和值摘要。
- 双击关键帧打开值编辑器时，当前播放头位置不应影响所编辑的关键帧。

官方参考：

- [Editing, moving, and copying keyframes](https://helpx.adobe.com/after-effects/desktop/animate-in-after-effects/animation-keyframes/editing-moving-copying-keyframes.html)

### 19.11 Parent/Child 和 Pick Whip 的边界

AE 的 Parent 关系不是普通分组：child 的 Transform 会相对 parent 计算，影响 Position、Scale、Rotation、Orientation，但不影响 Opacity；一个 child 只能有一个 parent，一个 parent 可以有多个 child；建立/移除 parent 不是可动画属性。

因此 Topos 只有在节点评估器真正支持 parent transform 时，才能把 `Parent` 列标记为有效：

- 未实现前只显示“关系/引用”列，不伪装成 AE Parent。
- 实现后用 pick whip 或下拉菜单选择 parent。
- 赋值/移除 parent 必须有保持世界变换与跟随 parent 两种策略。
- Alt/Option 赋值或移除 parent 可以表示“保持当前空间结果”，并进入一个 undo。
- 检测 parent cycle，拒绝造成环的操作。
- Parent 关系不随时间变化，不能作为普通 keyframe track。
- AI/脚本查询 parent graph，修改后返回 cycle/transform compensation 结果。

官方参考：

- [Parenting layers in After Effects](https://helpx.adobe.com/ca/after-effects/using/layer-properties.html)

### 19.12 官方行为与 Topos 目标的优先级

当 Topos 的节点语义和 AE 的图层语义冲突时，按以下优先级：

1. 数据和渲染结果正确。
2. 用户能预测按钮会影响什么。
3. 快捷键和时间线操作接近 AE。
4. 视觉布局接近 AE。
5. 图标形状接近 AE，但使用 Topos 自有实现。

任何按钮如果暂时只能影响时间线视图，必须在 tooltip、菜单或状态栏写明“仅影响时间线显示”，避免用户以为已经改变渲染。

---

## 20. 基于 Adobe 官方文档新增的原子任务

以下任务是在 T00-T79 之后新增的 AE 行为补全任务。它们可以并行调研，但编码依赖关系仍按表中顺序执行。

| ID | 任务 | 修改/新增文件 | 完成标准 |
|---|---|---|---|
| T80 | 实现可显示/隐藏/重排的 ColumnModel | `timeline_view_model.py`、tree view、session state | 列设置不破坏最小宽度；右键列标题可恢复默认 |
| T81 | 分离 Lock/Solo/Shy/Eye 数据语义 | node model、edit service、tree view | Lock 阻止所有编辑；Shy 只影响列表；Solo 语义明确 |
| T82 | 实现 AE Reveal 逻辑 | `keyframes_model.py`、shortcut registry | `U/UU/EE` 和搜索保留父路径，表达式错误可单独筛选 |
| T83 | 实现 Graph Editor mode 切换 | `spline_panel.py`、`spline_view.py` | `Shift+F3` 和按钮双向同步，选择不丢失 |
| T84 | 增加 temporal/spatial interpolation 字段 | `src/shared/keyframe.py`、migration | 旧 interpolation 可迁移，新字段可保存/加载 |
| T85 | 实现 Roving keyframe 状态 | keyframe evaluator、Spline、edit service | 仅空间属性可 rove，frame 改变可追踪、可撤销 |
| T86 | 实现 Easy Ease 预设 | Spline、context menu、edit service | 预设写入可解释 handles/influence，而不是 expression |
| T87 | 实现 value graph/speed graph | `spline_view.py`、curve model | Position 默认 speed，Opacity/Blur 默认 value |
| T88 | 实现 handle influence 和 Auto Zoom | `spline_view.py` | handle 远近影响速度/影响力，越界后曲线仍可见 |
| T89 | 实现 Expression Language menu | expression editor、manifest | 菜单只展示项目已支持的函数和变量，插入结果可解析 |
| T90 | 实现 Expression Error Reveal 和 Post-Expression Graph | model、spline view、expression parser | `EE`、错误过滤、pre/post graph 有不同视觉图例 |
| T91 | 实现 Composition/Node Marker | timeline model、timeline view、serialization | 点/区间 marker、双击编辑、Shift 吸附、保存/加载完整 |
| T92 | 增加 In/Out/Duration 列 | tree view、edit service | 数值编辑、拖边缘、Alt/Option 到当前帧行为明确 |
| T93 | 对齐 AE 复制粘贴语义 | timeline clipboard、edit service | 最早关键帧锚定当前帧，粘贴后保持选择，类型不匹配有错误 |
| T94 | 评估并实现 Parent/Pick Whip | node evaluator、tree view、edit service | 仅在 parent transform 真正可求值后开放，拒绝环 |
| T95 | 支持关键帧图标/索引显示模式 | timeline settings、timeline view | 可在菱形和数字索引之间切换，selection/hit-test 一致 |
| T96 | 完善 Shift Snap 目标集合 | timeline view、snap model | 可吸附 keyframe、marker、CTI、In/Out、work area、comp 边界 |
| T97 | 增加 Graph Editor selection transform box | spline view、edit service | 多关键帧可整体移动时间和值，操作一次 undo |
| T98 | 增加 selected keyframe Info summary | timeline status、spline view | 显示 layer/node、property、time、value、interpolation、speed |
| T99 | 增加官方行为回归用例 | `tests/ui/`、`tests/composite/` | 每个 T80-T98 至少有一个纯数据或 UI 自动化测试 |

---

## 21. 官方来源索引

以下链接是本方案采用的主要 Adobe 官方资料。后续模型如果需要重新核对 AE 行为，应优先读取这些页面，而不是依赖第三方教程：

1. [Timeline panel and composition basics](https://helpx.adobe.com/after-effects/desktop/work-with-compositions/composition-settings/composition-basics.html)
2. [Managing layers, switches, shy, solo, lock and labels](https://helpx.adobe.com/after-effects/desktop/work-with-layers/manage-layers/layers.html)
3. [Layer properties and parenting](https://helpx.adobe.com/after-effects/desktop/work-with-layers/layer-properties/layer-properties.html)
4. [Keyframe interpolation](https://helpx.adobe.com/after-effects/desktop/animate-in-after-effects/animation-keyframes/keyframe-interpolation.html)
5. [Editing, moving and copying keyframes](https://helpx.adobe.com/after-effects/desktop/animate-in-after-effects/animation-keyframes/editing-moving-copying-keyframes.html)
6. [Animation basics and Graph Editor](https://helpx.adobe.com/after-effects/desktop/animate-in-after-effects/animation-basics/animation-basics.html)
7. [Speed Graph and influence](https://helpx.adobe.com/ca/after-effects/using/speed.html)
8. [Editing expressions](https://helpx.adobe.com/after-effects/desktop/work-with-expressions/edit-expressions/edit-expressions.html)
9. [Expression language basics](https://helpx.adobe.com/in/after-effects/using/expression-language.html)
10. [Expression language reference](https://helpx.adobe.com/in/after-effects/using/expression-language-reference.html)
11. [Layer and composition markers](https://helpx.adobe.com/after-effects/desktop/work-with-layers/work-with-layer-and-composition-markers/layer-markers-composition-markers.html)
12. [After Effects keyboard shortcuts](https://helpx.adobe.com/after-effects/desktop/get-started/keyboard-shortcuts/keyboard-shortcuts-reference.html)

资料使用原则：

- 官方文档用来确定 AE 的公开交互语义和快捷键，不代表 Topos 必须复制 AE 的内部数据结构。
- 如果官方页面后续变更，先更新本节和 Txx 任务，再修改代码。
- 版本相关、平台相关或用户可配置的快捷键必须以应用设置为准，不能硬编码成不可变行为。

---

## 22. Graph Editor 模式的具体实现方法

本节是把 Spline 整合进 Keyframes 右侧区域的工程实现规范。目标是让用户点击顶部的 Graph Editor 按钮后，左侧属性树保留不变，右侧从“时间线绘制器”切换成“图表绘制器”；两种模式共享 selection、current frame、work area、track 和 edit command。

### 22.1 模式切换不是创建第二个面板

KeyframesPanel 内部只保留一个主编辑区域和一个共享状态：

```text
KeyframesPanel
├── Header / toolbar
├── Shared left outline
│   └── TimelineTreeView
├── Shared right editor host
│   ├── TimelineCanvas       # TimelineMode
│   └── GraphCanvas          # GraphMode
└── Footer / horizontal navigation
```

实现方式有两种，推荐第一种：

1. 推荐：`QStackedLayout` 或一个 `QWidget` editor host 在 `TimelineCanvas` 和 `GraphCanvas` 之间切换。左侧 tree、splitter、footer、selection model 和 timeline state 不销毁。
2. 备选：同一个 QPainter widget 根据 `view_mode` 分支绘制。只有在两个 canvas 的事件模型高度一致时使用，否则容易让 timeline hit-test 和 graph hit-test 互相污染。

切换规则：

- 点击 Graph Editor 按钮：`view_mode = "graph"`。
- 再次点击：`view_mode = "timeline"`。
- `Shift+F3`：在两个模式间切换，焦点位于 Keyframes 面板时生效。
- 切换模式不改变当前帧、工作区、左侧展开状态、当前过滤器和关键帧 selection。
- 进入 Graph 模式时，如果当前没有参数 selection，按优先级选择：当前选中的 parameter → 当前选中节点的第一个 animated property → 全局第一个 animated property。
- 进入 Graph 模式时，如果当前参数没有关键帧但有表达式，显示 expression result graph；如果既没有关键帧也没有表达式，右侧显示空状态说明。
- Graph 模式只显示当前选中的可绘图参数；多选参数可以同时显示，但每条曲线必须有图例和颜色。

### 22.2 顶部 Graph Editor 按钮

按钮位置：放在 Keyframes 顶部工具栏的视图模式组，靠近适配/缩放按钮，不放在最左侧播放控制中。

建议布局：

```text
[播放组] [关键帧导航组] [Timeline ▾] [Graph Editor] [Fit] [Zoom] [Snap] [More]
```

按钮样式：

| 状态 | 样式 |
|---|---|
| Timeline 未选中 | 深色透明背景，时间尺/关键帧小图标，文字或 tooltip `Timeline Mode` |
| Graph 未选中 | 深色透明背景，折线图标，文字或 tooltip `Graph Editor (Shift+F3)` |
| Graph 选中 | `C.BG_ACTIVE` 背景、1 px `C.ACCENT_BLUE` 或 `C.ORANGE` 边框、折线高亮 |
| Hover | `C.BG_HOVER`，图标变亮 |
| Disabled | 当前没有可绘图参数时变灰，tooltip 说明 `选择一个可动画参数后启用` |

为了兼顾窄面板：

- 宽度 ≥ 1100 px 时显示分段按钮文字。
- 宽度 < 1100 px 时只显示两个图标，使用 tooltip。
- 宽度 < 700 px 时保留 Graph 图标，隐藏非关键工具栏文字。
- 不能用一个普通“Spline”文字按钮代替 Graph Editor 状态，否则 AE 用户无法理解它是右侧视图模式。

### 22.3 GraphCanvas 的纯数据结构

不要直接在 `spline_view.py` 的 `paintEvent()` 中读取 Node 或修改 KeyframeTrack。建议新建 `src/features/composite/ui/graph_model.py`，或者将现有 `spline_model.py` 重构为以下结构：

```python
@dataclass
class GraphPoint:
    row_id: str
    node_id: str
    parameter_name: str
    channel: str | None
    frame: float
    value: float
    speed: float | None
    keyframe_index: int | None
    selected: bool
    interpolation_in: str
    interpolation_out: str
    handle_left: tuple[float, float] | None
    handle_right: tuple[float, float] | None
    tangent_mode: str

@dataclass
class GraphSeries:
    row_id: str
    display_name: str
    channel: str | None
    color: str
    is_spatial: bool
    has_keyframes: bool
    has_expression: bool
    points: list[GraphPoint]
    samples: list[tuple[float, float]]
    pre_expression_samples: list[tuple[float, float]]
    post_expression_samples: list[tuple[float, float]]

@dataclass
class GraphViewport:
    frame_start: float
    frame_end: float
    value_min: float
    value_max: float
    pixels_per_frame: float
    pixels_per_value: float
    auto_zoom_time: bool
    auto_zoom_value: bool
    graph_type: str  # auto | value | speed

@dataclass
class GraphOptions:
    show_reference_graph: bool
    show_post_expression_graph: bool
    show_expression_editor: bool
    show_transform_box: bool
    show_layer_in_out: bool
    show_markers: bool
    separate_dimensions: bool
    snap_enabled: bool
```

Graph model 的输入只允许是不可变 snapshot 或通过 edit service 取得的只读数据；GraphCanvas 只发出：

```text
select_graph_point
select_graph_segment
move_graph_points
scale_graph_points
move_bezier_handle
set_graph_option
seek_current_frame
```

正式数据修改必须转成 `TimelineCommand`，不能在 QPainter 事件中直接设置 `keyframe.value`。

### 22.4 Value Graph 和 Speed Graph 的坐标系

#### Value Graph

Value Graph 的横轴是 frame/time，纵轴是参数值：

```text
pixel_x = graph_left + (frame - frame_start) * pixels_per_frame
pixel_y = graph_bottom - (value - value_min) * pixels_per_value
```

要求：

- 标量、颜色通道、Blur、Opacity、Rotation 默认使用 Value Graph。
- vec2/vec3 可以按通道绘制多条 value curve。
- X/Y/Z 使用独立颜色，但 selection 使用统一的选中描边。
- 0 值线、当前帧线和 major grid 不能被曲线遮挡，绘制顺序必须固定。
- value bounds 默认由可见 keyframe 和采样值计算，并留 8%-12% 上下边距。
- `auto_zoom_value` 开启时，拖 handle 超出当前范围后重新计算 bounds，但保持鼠标附近数据坐标稳定。

#### Speed Graph

Speed Graph 的横轴仍然是 frame/time，纵轴是参数变化速度：

```text
speed(frame) = d(value(frame)) / d(time_seconds)
pixel_y = graph_bottom - (speed - speed_min) * pixels_per_speed
```

要求：

- Position、Anchor Point、空间 effect control point 默认使用 Speed Graph。
- Speed 必须按秒计算，不是“每 frame 的值差”；`speed = dv / (dt_frames / fps)`。
- Hold 区间速度为 0，并且曲线不能画出虚假的斜坡。
- Linear 段速度为常数；Bezier 段速度随 handle/influence 变化。
- 速度正负以零线区分，零线使用比普通网格稍亮的颜色。
- Speed Graph 中拖动关键帧默认改变时间；拖动 handle 改变速度和 influence。
- 多通道 speed graph 使用同一纵轴时，图例必须显示单位/通道，避免把 X 和 Y 的数值误认为同一曲线。

#### Auto Select

`Auto Select` 根据 ParamDef 的 `is_spatial` 或 channel type 选择：

```text
is_spatial(Position/Anchor/Point) → speed graph
scalar/temporal(Opacity/Blur/Rotation/Color) → value graph
unknown → value graph，并给出可切换提示
```

如果用户手动选择 Value/Speed，Graph 模式切换或重新选择参数时不应偷偷覆盖用户选择；只有用户重新点击 Auto Select 才恢复自动策略。

### 22.5 曲线采样和绘制算法

绘制不能只连关键帧直线，否则看不出实际插值。建议分三层：

1. `KeyframePoint`：绘制关键帧、选中状态和编辑 handle。
2. `InterpolationCurve`：根据 keyframe track 的 temporal/spatial interpolation 生成曲线。
3. `ExpressionCurve`：在需要时对表达式结果采样，显示 post-expression graph。

采样规则：

- 采样范围为当前 GraphViewport 的可见 frame 范围，前后各扩展一个关键帧区间用于平滑边缘。
- 默认每 2-4 个像素一个 sample，不能固定每 frame 一个 sample，否则高 zoom 时浪费，低 zoom 时曲线锯齿。
- 关键帧附近强制加入 sample，确保尖点、Hold 跳变和 handle 转折不丢失。
- 曲线数据可缓存，cache key 至少包含 track revision、expression revision、graph type、frame range、sample density、fps。
- 拖动期间只更新受影响 series；释放后提交正式 track revision。
- 表达式曲线按 subframe/time 采样，不能把一个表达式结果粗暴复制给整段 block。

Bezier 曲线：

- Value Graph 使用 frame/value 平面上的控制点。
- Speed Graph 使用时间/速度平面；handle 的垂直位置表达 speed/influence，不能直接复用 value graph 的 y 值。
- 每段曲线绘制为 cubic Bezier 或项目已有 evaluator 的等价结果。
- evaluator 和 GraphCanvas 必须共享同一数学函数；不能让 UI 自己近似一套曲线、渲染器使用另一套曲线。
- Hold 段绘制水平线，并在下一关键帧处绘制垂直跳变提示或明确的断点。

### 22.6 Graph Editor 绘制顺序

`GraphCanvas.paintEvent()` 采用固定绘制顺序：

```text
1. graph background
2. work-area overlay and layer In/Out braces
3. minor grid
4. major grid and zero line
5. markers
6. reference graph / pre-expression graph
7. post-expression graph
8. unselected interpolation curves
9. selected interpolation curves
10. keyframe points
11. Bezier direction handles
12. transform box and anchor
13. current-time indicator
14. hover tooltip / drag preview
15. bottom graph controls overlay
```

绘制细节：

- 网格线不能覆盖曲线颜色；曲线也不能覆盖播放头的高亮。
- pre-expression graph 使用低透明度，post-expression graph 使用高亮；右下角或底部提供图例。
- 当前播放头线贯穿整个 GraphCanvas，但不要覆盖左侧属性树。
- 选中曲线的宽度为 2 px，普通曲线 1 px，参考曲线 1 px + 低透明度。
- 曲线端点超出视图时进行 clip，不要因为一条曲线破坏整个 widget 的 repaint region。
- 关键帧的 hit radius 10-12 px，handle hit radius 8 px；重叠时 handle 优先于 keyframe，keyframe 优先于 curve segment。

### 22.7 Graph Editor 底部控制栏

参考 AE Graph Editor 底部控制栏，Topos Graph 模式底部从左到右建议为：

| 控件 | 图标/样式 | 行为 |
|---|---|---|
| Show Properties | 三条属性线 | Selected / Animated / Graph Set |
| Graph Type | 曲线图 | Auto / Value / Speed |
| Reference Graph | 双层曲线 | 显示未选中的 graph type |
| Expression Graph | 前后两条曲线 | 显示 pre/post expression |
| Separate Dimensions | X/Y/Z 分叉 | Position/vec 参数拆分通道 |
| Show Transform Box | 方框四角 | 多选关键帧的移动/缩放/锚点 |
| Snap | 磁铁 | 吸附 keyframe/marker/CTI/In/Out/work area |
| Auto Zoom Height | 上下箭头 | handle 越界时自动扩展 value/speed 范围 |
| Fit Graph | 四角箭头 | 适配可见曲线和关键帧 |
| Zoom slider | 细滑块 | 时间/值轴缩放，保持鼠标/播放头锚点 |
| Show Markers/In-Out | 标尺括号/标记 | 显示时间参考线 |

窄面板策略：

- 宽度不足时，Show Properties 和 Graph Type 保留，其他按钮收进 More 菜单。
- 当前 Graph Type 和 Show Properties 必须始终可见，避免用户不知道坐标系。
- 每个图标按钮必须有 tooltip、accessible name 和 checked 状态。

### 22.8 Graph 模式的鼠标交互

| 操作 | 结果 |
|---|---|
| 单击曲线关键帧 | 选中 keyframe，左侧参数行和时间线对应点同步高亮 |
| Shift 单击/框选 | 加入/移除多选 |
| Alt/Option 单击 segment | 选中该 property 的全部关键帧 |
| 单击曲线 segment | 选中 segment，Info 区显示当前区间、速度和插值 |
| 拖动关键帧 Value Graph | 同时移动时间和值 |
| 拖动关键帧 Speed Graph | 默认移动时间；值修改通过 handle/速度操作完成 |
| Shift 拖动 | 约束水平或垂直方向 |
| 拖动 Transform Box 内部 | 移动全部选择，保持相对距离 |
| 拖动 Transform Box 边/角 | 缩放选中的时间和值范围 |
| Ctrl/Cmd 拖 Transform Box | 以 anchor 为中心缩放 |
| Alt/Option 拖角点 | 只移动该边/角，保持其他边界 |
| 拖动 Transform Box anchor | 改变缩放中心，不直接改变关键帧值 |
| 拖动 Bezier handle | 修改手柄/速度/influence |
| Alt/Option 拖 keyframe/handle | 拆分或合并入射/出射 handle |
| Shift 拖动 | 吸附到关键帧、marker、CTI、In/Out、work area、comp 边界 |
| 双击 keyframe | 打开该关键帧值和时间编辑器 |
| 右键 keyframe | 插值、切线、roving、编辑值、选择相邻关键帧 |
| 空白处拖动 | 框选；点击空白取消选择 |
| 鼠标滚轮 | 垂直滚动/时间轴缩放，按 modifier 规则处理 |
| 中键/空格拖动 | 平移 Graph viewport，不修改数据 |

所有拖动均采用：

```text
press → hit-test → capture selection and before snapshot
move → update preview GraphViewport/preview points
release → emit one TimelineCommand transaction
Esc → restore before snapshot
```

### 22.9 多关键帧 Transform Box

官方 Graph Editor 允许多选关键帧后显示 free-transform bounding box；Value Graph 中可以同时改变时间和值，Speed Graph 中主要改变时间。Topos 应实现一个轻量、可测试的版本：

```text
selected points
┌─────────────────────────┐
│                         │
│            + anchor     │
│                         │
└─────────────────────────┘
```

数据规则：

- bounding box 是 selection 的派生显示对象，不写入项目文件。
- box 的 left/right 是选中 frame min/max，top/bottom 是选中 value/speed min/max。
- 内部拖动产生 `delta_frame` 和 `delta_value`。
- 边/角拖动产生 `scale_time` 和 `scale_value`，以 anchor 为中心计算。
- Speed Graph 中 `scale_value` 默认禁用或显示为调整速度范围的选项，不能误改关键帧值。
- 关键帧不得拖出参数允许的时间范围；值超出 min/max 时遵循 ParamDef 的 clamp/allow overshoot 规则。
- 所有选中关键帧必须保持原相对顺序和通道关系。
- 同一属性内允许批量值变换；跨不同参数只允许批量移动时间，禁止把 Blur 数值缩放到 Position。

### 22.10 Handle 编辑和 influence

Graph Editor 中 handle 不只是“两个控制点”：

- Value Graph handle 改变曲线形状。
- Speed Graph handle 改变速度进入/离开关键帧。
- handle 距离 keyframe 越远，influence 越大；越近，influence 越小。
- 默认 joined handle：拖一侧时另一侧保持连续关系。
- Split handle：两侧独立，使用 Alt/Option 拖动或 Broken tangent mode。
- Auto/Smooth 要保证曲线连续；Linear/Flat/Step 必须隐藏或禁用不适用的 handle。
- 拖 handle 时显示辅助线、当前 influence/速度数值和吸附提示。
- 右键 handle 可切换 joined/split，不需要用户必须精确点击一个很小的按钮。

handle 坐标不要使用单一归一化 `0..1` 值。建议存储数据空间的相对 frame/value 或相对 frame/speed：

```text
Value Graph:  handle = (frame_delta, value_delta)
Speed Graph:  handle = (frame_delta, speed_delta/influence)
```

序列化仍然保留 legacy bezier 字段，但 Graph 模式修改必须写入 authoritative handle 字段。

### 22.11 Expression Graph 的前后结果

当一个参数同时有 keyframes 和 expression：

```text
pre-expression curve  = keyframe/static evaluation
expression evaluation  = expression(value, time, frame, context)
post-expression curve  = final property result
```

绘制要求：

- pre 曲线使用暗灰/低透明度。
- post 曲线使用参数色/高亮色。
- 关键帧菱形始终标识 pre-expression keyframe，不要把表达式采样点画成关键帧菱形。
- 表达式字段在 Graph 模式底部显示为可调整高度的单行/多行编辑框；多参数选中时默认只显示 primary property，避免多个编辑器重叠。
- 表达式错误时 post 曲线显示 fallback 结果，并带红色错误标记。
- Show Post-Expression Graph 关闭时，只显示 pre 曲线，减少视觉噪音。

### 22.12 Graph 模式的性能策略

- GraphCanvas 只绘制当前可见 frame/value viewport 内的 series。
- series sample cache 使用 track revision 和 expression revision 失效，不依赖每次 repaint 都重新求值。
- 拖动 keyframe/handle 时使用低密度 preview sampling；鼠标释放后使用正式密度重算。
- expression graph 默认采用节流刷新，例如 30-60 Hz，不能让每个 mouse move 同步阻塞 UI。
- 多参数曲线超过阈值时只显示 selected/primary series，工具栏提示已隐藏的 series 数量。
- 关键帧点、handle、transform box 仍然由 QPainter 绘制，不创建 QWidget。
- Graph mode 与 Timeline mode 共享 `FrameMapper`，避免切换模式时当前 frame 产生偏移。

### 22.13 Graph 模式的错误处理

| 错误 | UI 处理 | 数据处理 |
|---|---|---|
| 无可绘图属性 | 右侧显示空状态和 `选择可动画参数` | 不创建虚假 track |
| 参数类型不支持 graph | 显示 `仅支持数值/颜色/向量参数` | 禁止进入值编辑 |
| 表达式采样失败 | 显示 post graph fallback 和错误图标 | 保留表达式文本，不污染 track |
| handle 数据非法 | 显示警告并回退自动 handle | 写入前验证，拒绝 NaN/Inf |
| speed graph 无法求导 | 回退 value graph，显示提示 | 不伪造速度曲线 |
| 多通道范围冲突 | 分通道显示或显示单位 | 不隐式合并不同类型 |
| 拖动越界 | 光标/边界提示 | release 时拒绝或 clamp，进入一次 undo |

---

## 23. Graph Editor 专项原子任务

以下任务接在 T00-T99 之后，专门实现 Keyframes 右侧 Graph 模式。每项都可以独立完成、测试和回退。

| ID | 任务 | 修改/新增文件 | 完成标准 |
|---|---|---|---|
| T100 | 增加 `view_mode` 状态和 Graph Editor 按钮 | `keyframes_panel.py`、toolbar style | Timeline/Graph 可切换，左侧和 current frame 不重置 |
| T101 | 建立 GraphCanvas host | `keyframes_panel.py`、`graph_view.py` | GraphCanvas 可挂载到右侧区域，模式切换不销毁 shared model |
| T102 | 建立 GraphPoint/GraphSeries/GraphViewport | `graph_model.py` | 纯数据可单测，不依赖 QWidget |
| T103 | 实现 property selection → GraphSeries | `graph_model.py`、`spline_model.py` | 左侧选一个参数，右侧只显示对应 series |
| T104 | 实现 Auto Select Graph Type | `graph_model.py`、ParamDef | Position/Anchor 默认 speed，Blur/Opacity 默认 value |
| T105 | 实现 Value Graph 坐标变换 | `graph_view.py` | frame/value 与 pixel 坐标互转，零线和 bounds 正确 |
| T106 | 实现 Speed Graph 坐标变换 | `graph_view.py`、evaluator | speed 使用 fps 换算，Hold/Linear/Bezier 测试正确 |
| T107 | 实现曲线采样和缓存 | `graph_model.py`、evaluator | cache key 含 track/expression revision 和 viewport |
| T108 | 实现 Graph 背景、grid、zero line、legend | `graph_view.py`、style_sheet.py | 绘制顺序和视觉 token 固定 |
| T109 | 实现 Graph 曲线和关键帧点绘制 | `graph_view.py` | 普通/选中/表达式前后/多通道状态可区分 |
| T110 | 实现 Graph 底部控制栏 | `graph_view.py`、`keyframes_panel.py` | Show Properties、Graph Type、Fit、Snap 可用 |
| T111 | 实现 Graph viewport zoom/pan | `graph_view.py` | 鼠标/播放头锚点稳定，滚轮和中键行为可测 |
| T112 | 实现 keyframe hit-test 和框选 | `graph_view.py` | keyframe/segment/handle 优先级正确 |
| T113 | 实现 Graph 选中同步 | shared selection model | Graph 选中更新左树和 Timeline，反向也同步 |
| T114 | 实现 Value Graph 单点拖动 | `graph_view.py`、edit service | frame/value 更新一次 undo，支持 snap |
| T115 | 实现 Speed Graph 单点拖动 | `graph_view.py`、edit service | 时间/速度语义不误改原始 value |
| T116 | 实现 Bezier handle 绘制和拖动 | `graph_view.py`、edit service | joined/split、Alt/Option、influence 正确 |
| T117 | 实现 Transform Box | `graph_view.py`、edit service | 移动、缩放、anchor、Shift/Ctrl/Alt 行为可测 |
| T118 | 实现 Separate Dimensions | graph model、edit service | Position/vec2/vec3 可拆分和合并，不丢数据 |
| T119 | 实现 Reference/Post-Expression Graph | expression evaluator、graph view | pre/post 曲线和图例正确，错误可回退 |
| T120 | 实现 Graph Expression Editor | expression editor、graph view | 底部可伸缩，多选只显示 primary property |
| T121 | 实现 Marker/In-Out/Work Area overlay | graph view、timeline model | Graph 与 Timeline 使用同一参考点和吸附目标 |
| T122 | 实现 Graph Info summary | `graph_view.py`、footer | 显示 property、frame、value、speed、interpolation |
| T123 | 实现 Graph mode undo transaction | edit service、CompositePage | 每次拖动/批量变换一次 undo，Esc 恢复 |
| T124 | 实现 Graph mode 离屏布局测试 | `tests/ui/test_graph_editor_layout.py` | 多尺寸、多通道、空状态、长名称无重叠 |
| T125 | 实现 Graph mode 交互回归测试 | `tests/ui/test_graph_editor_interaction.py` | 选择、框选、缩放、拖动、handle、Transform Box 全覆盖 |
| T126 | 实现 Graph 数值一致性测试 | `tests/composite/test_graph_evaluator_consistency.py` | Graph 曲线采样与 renderer/evaluator 数值一致 |
| T127 | 实现 Graph 表达式回归测试 | `tests/composite/test_graph_expression.py` | pre/post graph、错误回退、valueAtTime 一致 |
| T128 | 实现 Graph 大项目性能测试 | `tests/benchmarks/` | 多参数、多曲线和表达式采样不产生明显 UI 阻塞 |
| T129 | 将独立 Spline 入口改为 Graph 模式入口 | `spline_panel.py`、Composite page | 旧入口仍可用，但只打开共享 Graph 模式，不创建第二套状态 |

---

## 24. Graph Editor 专项验收流程

### 24.1 Value Graph 验收

使用一个 `Blur` 或 `Opacity` 参数创建三个关键帧：低值 → 高值 → 低值。

必须验证：

1. Graph 模式默认显示 Value Graph。
2. 三个关键帧位置和时间线菱形一致。
3. Linear 曲线是直线段。
4. Hold 曲线是水平段并在下一关键帧处跳变。
5. Bezier handle 会改变曲线形状，但不会改变关键帧 frame/value。
6. 拖动曲线关键帧改变值后，属性面板、时间线、Spline 和渲染结果一致。
7. 多选 Transform Box 可以同时改变时间和值。

### 24.2 Speed Graph 验收

使用 `Transform.Position` 或 `Anchor Point` 创建三个空间关键帧。

必须验证：

1. Auto Select 默认进入 Speed Graph。
2. Speed 单位按 fps 正确换算。
3. Linear 段速度近似常数。
4. Bezier handle 改变加速/减速和 influence。
5. Hold 段速度为 0。
6. Graph 中移动时间不会偷偷修改原始空间值。
7. Transform Box 在 Speed Graph 中遵循“主要改变时间”的规则。

### 24.3 表达式 Graph 验收

给 `Scale` 或 `Blur` 添加关键帧，再添加会改变幅度的表达式。

必须验证：

1. pre-expression graph 仍然显示关键帧求值结果。
2. post-expression graph 显示表达式最终结果。
3. 两条曲线有清晰图例和不同明度。
4. 表达式错误时 post graph 显示 fallback，不影响其他节点。
5. `EE` 能只显示带表达式的参数。
6. Graph 底部表达式框可以调整高度。
7. 右键、Alt/Option-click、Graph expression editor 打开的内容相同。

### 24.4 AE 行为验收

- `Shift+F3` 在 Timeline/Graph 之间切换。
- `J/K` 跳到可见时间标尺中的上一个/下一个关键帧、marker 或工作区边界。
- Shift 拖动关键帧可吸附到 CTI、marker、In/Out、work area 和 composition 边界。
- Shift/Ctrl/Alt/Option 的多选、约束、拆分手柄和 Transform Box 语义稳定。
- 复制粘贴后最早关键帧锚定当前帧且 selection 保留。
- Graph 模式下右键关键帧可以修改 temporal/spatial interpolation、roving 和 handle 状态。

### 24.5 共享数据验收

同一个 Transform/Blur 参数在四个入口中编辑：

```text
Keyframes Timeline → Graph Editor → 属性面板 → AI/Script
```

每次修改都必须验证：

- 轨道数据相同。
- 渲染结果相同。
- 保存/加载相同。
- Undo/Redo 相同。
- expression/handle/interpolation 没有被另一个入口覆盖。

---

## 25. Graph Editor 官方参考补充

本次专项核对使用以下 Adobe 官方页面：

1. [Keyframe interpolation and Graph Editor handles](https://helpx.adobe.com/after-effects/desktop/animate-in-after-effects/animation-keyframes/keyframe-interpolation.html)
2. [Editing, moving, copying keyframes and Graph Editor Transform Box](https://helpx.adobe.com/after-effects/desktop/animate-in-after-effects/animation-keyframes/editing-moving-copying-keyframes.html)
3. [Animation basics and Graph Editor graph types](https://helpx.adobe.com/uk/after-effects/using/animation-basics.html)
4. [Speed Graph and direction handle influence](https://helpx.adobe.com/ca/after-effects/using/speed.html)
5. [Expression graph, Post-Expression Graph and Shift+F3](https://helpx.adobe.com/after-effects/desktop/work-with-expressions/edit-expressions/edit-expressions.html)
6. [Graph Editor keyframe selection and Transform Box](https://helpx.adobe.com/uk/after-effects/using/setting-selecting-deleting-keyframes.html)
7. [Motion paths and spatial properties](https://helpx.adobe.com/after-effects/desktop/animate-in-after-effects/assorted-animation-tools/assorted-animation-tools.html)

这些资料补充了原计划中关于 Graph Editor 的实现细节：Graph 模式不是静态曲线预览，而是带坐标类型、采样、关键帧编辑、handle、Transform Box、表达式前后结果、吸附和 Undo 的完整编辑器。
