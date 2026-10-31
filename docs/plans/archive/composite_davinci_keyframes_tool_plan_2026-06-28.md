# Composite 页面 DaVinci Fusion 风格 Keyframes 工具实现计划表

日期：2026-06-28

目标：为 Topos Color 的 Composite 页面增加一个类似 DaVinci Resolve Fusion 页底部 Keyframes 面板的工具。该工具用于集中查看和编辑合成节点的时间范围、媒体长度、参数关键帧位置、关键帧插值曲线和当前播放头位置。UI 布局、按钮位置、轨道层级和行样式按 DaVinci Fusion Keyframes 面板复刻；具体配色、按钮 hover/pressed 状态和字体颜色使用本项目全局暗色主题与现有控件样式。

读者说明：后续执行该计划的大模型可能无法查看截图，因此本文用纯文字详细描述目标 UI。不要只参考现有 `src/features/composite/ui/keyframe_editor.py`，那个文件是参数属性里的小型关键帧编辑器，不等价于本计划要新增的 Fusion 风格全局 Keyframes 面板。

## 0. 执行总原则：先跑通关键帧系统，再创建 Keyframes 面板

不要一开始就写 Keyframes 面板。Keyframes 面板只是关键帧系统的可视化入口；如果底层关键帧系统没有跑通，面板会出现“能画、能拖，但渲染不对、保存丢失、Undo 不对、播放不同步”的问题。

本计划必须按以下闸门推进：

| 闸门 | 范围 | 完成标准 | 未通过时禁止做什么 |
|---|---|---|---|
| Gate 1：关键帧数据权威源 | `Node.keyframe_tracks`、`time_range`、`node_color`、schema migration | 节点级关键帧、时间范围、颜色能保存/加载，两个同类节点互不污染 | 禁止开始 `KeyframesPanel` UI |
| Gate 2：渲染和当前帧求值 | `NodeGraph.snapshot()`、`GraphSnapshot.frame_number`、`GraphEvaluatorMixin`、preview/export | 预览、播放、导出都按同一帧号插值，int/float/X/Y 参数结果正确 | 禁止开始轨道绘制 |
| Gate 3：属性面板和 Undo/Redo | `NodePropertiesPanel`、`CompositePage`、`UndoStack` | 属性面板 `◆` 能创建/删除/修改节点级轨道；Undo/Redo 和保存状态正确 | 禁止开始复杂交互 |
| Gate 4：系统测试全绿 | composite/project/ui 相关测试 | 新增关键帧系统测试和既有 composite 测试全部通过 | 禁止进入 Keyframes 面板正式实现 |
| Gate 5：Keyframes 面板 | 可视化、拖拽、颜色同步、滚动缩放 | 只在 Gate 1-4 全部通过后开始 | - |
| Gate 6：Spline 曲线编辑 | 曲线图表、贝塞尔控制柄、曲线插值、曲线视图测试 | Keyframes 面板基础交互全绿后再开始 | 禁止在 Keyframes 面板基础交互完成前做 Spline |

执行顺序必须是：P0 → P1 → P6 的属性面板关键帧修复 → P7 的保存/加载/Undo → P8 的 frame_number/播放同步 → P10A 的关键帧系统测试全绿 → P2/P3/P4/P5 的 Keyframes 面板模型和 UI → P10B 全量回归 → P11/P12 的 Spline 曲线编辑。

## 1. 当前项目状态审计

| 项目 | 当前状态 | 结论 |
|---|---|---|
| 参数关键帧底层类型 | `src/shared/keyframe.py` 已有 `Keyframe`、`KeyframeTrack`、`InterpolationType` | 可复用，不要重新发明关键帧数据结构 |
| 旧兼容导入 | `src/features/composite/utils/keyframe.py` 只是转发到 `src/shared/keyframe.py` | 新代码应直接使用 `src/shared/keyframe.py` |
| 参数小编辑器 | `src/features/composite/ui/keyframe_editor.py` 已有单参数 timeline/curve/dialog | 可作为局部控件参考，但 UI 不符合 Fusion 全局 Keyframes 面板 |
| 节点属性面板 | `NodePropertiesPanel` 的数值参数行已有 `◆` 关键帧按钮 | 需要和新 Keyframes 面板双向同步 |
| 渲染插值 | `GraphEvaluatorMixin._interpolate_parameters()` 会按 `GraphSnapshot.frame_number` 插值 | 需要保证 snapshot 能拿到每个节点自己的关键帧轨道 |
| 快照字段 | `NodeSnapshot.keyframe_tracks` 已存在 | 数据源要从每个节点实例读取，而不是从类级 `PARAM_DEFS` 读取 |
| 潜在架构风险 | 目前 `ParamDef` 来自节点类级 `PARAM_DEFS`，若直接写 `pdef.animated/keyframes`，可能导致多个同类节点共享关键帧状态 | 必须先做“每个节点独立动画轨道”改造 |
| Dock 注册 | `CompositeModule.register_panels()` 已注册 Node Graph 和 Performance 面板 | 新增 Keyframes 面板应走同一个 PanelRegistry |

### 1.1 本次代码审计发现的缺失和必须修复项

以下结论来自对 `src/shared/keyframe.py`、`src/features/composite/ui/keyframe_editor.py`、`src/features/composite/ui/node_properties_panel.py`、`src/features/composite/ui/composite_page.py`、`src/features/composite/core/node_engine.py`、`src/features/composite/nodes/base_node.py`、`src/features/composite/render/composite_render_output.py` 和相关测试的检查。

| 编号 | 严重性 | 发现 | 影响 | 必须写入实施任务 |
|---|---|---|---|---|
| A-1 | Critical | 当前关键帧状态主要写在 `ParamDef.animated/keyframes`，而 `ParamDef` 来自节点类级 `PARAM_DEFS` | 同类节点可能共享/污染关键帧；无法可靠保存每个节点自己的动画 | P1 必须迁移到节点实例级 `node.keyframe_tracks` |
| A-2 | Critical | `Node.to_dict()` 不保存关键帧轨道，`Node.from_dict()` 不恢复关键帧轨道 | 项目保存/重启后关键帧会丢失或只能依赖不可控类级状态 | P1/P7 必须补序列化、反序列化和旧数据迁移 |
| A-3 | Critical | `NodeGraph.snapshot()` 从 `node.PARAM_DEFS` 收集关键帧，不从节点实例收集 | 渲染线程拿不到真实节点级关键帧；多个节点动画不可隔离 | P1 必须改 snapshot 数据源 |
| A-4 | Critical | `CompositePage.properties_panel` setter 没有连接 `NodePropertiesPanel.keyframes_changed` | 属性面板 `◆` 改关键帧后不会进入 NodeGraph、Undo、保存或重新渲染 | P6 必须新增 `_on_props_keyframes_changed` |
| A-5 | Critical | `CompositePage._do_evaluate_graph()`、`on_enter()`、`update_color_output()` 等 preview 路径调用 `snapshot()` 时没有传 `frame_number` | 即使导出可能按帧插值，UI 预览/播放时关键帧很可能一直按 0 帧求值 | P8 必须定义当前合成帧并传入所有 snapshot |
| A-6 | High | `KeyframeEditorWidget` 关闭 Animated 时仍会 emit 默认 keyframes；Dialog accept 也会把未启用动画的默认两点轨道写回 | 用户无法真正取消动画，误点 OK 可能凭空创建关键帧 | P6 必须修复 disabled animation 语义：关闭时 emit 空轨道或删除轨道 |
| A-7 | High | X/Y pair 的 shared `◆` 编辑器使用 X 轴轨道并把同一套数值镜像到 Y 轴 | position/scale 等二维参数会把 Y 值错误改成 X 值 | P6 必须做二维轨道模型，支持 linked 和 separated 两种模式 |
| A-8 | High | `KeyframeTrack.add_keyframe()` 允许重复 frame，只有 `validate_track()` 会报告错误 | 外部 API 或后续 Keyframes 面板可能产生重复关键帧，插值结果不稳定 | P1/P5 必须定义“同帧替换”或显式禁止重复 |
| A-9 | High | `detect_composite_mode()` 只检测 legacy 参数 dict 和 graph-level `keyframe_tracks`，不检测计划中的 per-node `keyframe_tracks` | 有关键帧的 graph 可能被误判成 still image，导致导出范围错误 | P7 必须更新 render-output mode detection |
| A-10 | High | `resolve_video_range()` 不参考 per-node keyframes/time_range | 独立 composite 有动画但没有 video node 时，导出范围可能错误 | P7 必须从关键帧和节点范围推导默认渲染区间 |
| A-11 | High | 节点颜色修改只更新内存 `node.header_color` 和 NodeItem，没有保存到 `Node.to_dict()`，也没有 Undo/dirty 发布 | 自定义节点颜色重启后丢失；Keyframes 面板无法持久同步图层颜色 | P1/P6/P7 必须保存 `node_color/header_color` 并发出颜色变更刷新 |
| A-12 | Medium | `NodeSectionWidget` 的 lock 只是属性面板 UI 状态，不是节点/轨道级持久状态 | Keyframes 面板的锁定列无法和现有锁定可靠同步 | P2/P5 必须设计 `track_locks` 或 UI-only lock state |
| A-13 | Medium | `NodePropertiesPanel.set_timeline_state()` 存在，但未发现 CompositePage 播放/seek 路径调用 | 属性面板小 keyframe editor 打开时可能拿不到正确总帧和当前帧 | P8 必须统一 `CompositeTimelineState` 并同步到属性面板 |
| A-14 | Medium | `shared/keyframe.py` 只存 scalar float value；int 参数求值后未统一 round/cast，多维参数没有一等模型 | int 参数动画可能得到 float；vec2/vec3 只能拆散处理 | P1/P6 必须定义类型转换和多维策略 |
| A-15 | Medium | 表达式和关键帧共存没有明确优先级 | 同一参数同时有 expression 和 keyframes 时结果不可预期 | P1 必须定义优先级：建议 expression 优先或互斥，并在 UI 禁止冲突 |
| A-16 | Medium | 现有测试集中在 `ParamDef` 和小按钮，不覆盖节点级保存、snapshot、属性面板信号、颜色同步 | 重构后容易回归 | P10 必须新增集成测试并更新旧测试期望 |

## 2. 产品边界

新增的是 Composite 页面全局 Keyframes 工具，不是属性面板内的小弹窗。

必须支持：

| 能力 | 说明 |
|---|---|
| 查看所有节点层级 | 左侧树显示节点，节点下可展开 animatable 参数 |
| 查看节点/媒体时长 | 右侧时间区显示每个节点的有效范围条，可拖动开始/结束/整体偏移 |
| 查看参数关键帧 | 每个参数行显示关键帧点、曲线段和当前帧值 |
| 调整关键帧位置 | 拖拽关键帧水平移动，吸附到帧整数，可多选移动 |
| 调整视频片段长度 | 对媒体节点显示源入点/出点和合成范围，支持拖动边缘 |
| 默认居中 | 面板打开时播放头居中或尽量在可视范围中间，时间范围显示当前合成片段 |
| 双向同步 | 属性面板 `◆` 按钮、新 Keyframes 面板、渲染评估、保存/加载保持一致 |
| Undo/Redo | 移动关键帧、改时长、增删关键帧都进入 Composite undo stack |
| 性能 | 大量节点/关键帧时拖拽流畅，绘制虚拟化，不为每个 keyframe 创建 QWidget |

暂不做或后置：

| 能力 | 原因 |
|---|---|
| 完整 Spline 曲线编辑器 | 第三阶段实现；必须等关键帧系统和 Keyframes 面板基础都跑通 |
| 复杂贝塞尔手柄可视编辑 | 第三阶段实现；必须先完成贝塞尔数学测试和离屏绘制测试 |
| 音频波形 | Composite 节点图暂不处理音频 |
| 完全照搬 DaVinci 私有图标资产 | 使用项目现有 `icon_manager` 或绘制等义图标，不引入外部版权素材 |

第三阶段后置能力：

| 能力 | 前置条件 |
|---|---|
| Spline 曲线图表编辑器 | 关键帧系统 P10A 全绿，Keyframes 面板基础 P2-P5/P10B 全绿 |
| 贝塞尔控制柄可视编辑 | 节点级 keyframe track 已支持 per-keyframe `bezier_in/bezier_out` 且保存/加载/Undo 全绿 |
| 自动/平滑/断开/线性/阶梯切线模式 | 基础曲线绘制、命中测试、拖拽测试全绿后再打开 |

## 3. 目标 UI 详细规格

### 3.1 面板整体结构

目标面板是一个横向很宽、纵向较矮的底部 Dock，默认停靠在 Composite 页面底部，类似 Fusion 页底部 Keyframes 区。

整体从上到下分为三层：

| 层级 | 高度建议 | 内容 |
|---|---:|---|
| 顶部标题/工具栏 | 30 px | 左侧标题 `Keyframes`，右侧一排图标按钮和缩放滑条 |
| 主编辑区 | 自适应，建议默认 230-320 px | 左侧节点树，右侧时间标尺和关键帧轨道 |
| 底部状态/滚动栏 | 36-40 px | 左侧视图切换按钮，中间水平滚动条，右侧时间单位下拉框和数值框 |

默认面板尺寸：

| 属性 | 建议值 |
|---|---:|
| 默认 Dock 高度 | 300 px |
| 最小高度 | 180 px |
| 左侧树宽度 | 232 px |
| 左侧树最小宽度 | 180 px |
| 左右分隔线宽度 | 1 px |
| 行高 | 24 px |
| 标尺高度 | 30 px |
| 底部栏高度 | 38 px |

面板必须有一个可拖动的左右分隔条，用户可调整左侧树宽。分隔条颜色使用 `C.BORDER_DARK` 或项目 splitter 现有颜色。

### 3.2 顶部标题/工具栏

截图参考布局：

左侧：

| 区域 | 位置 | 说明 |
|---|---|---|
| 标题 | 左上角，距左 8 px | 文本 `Keyframes`，白灰色，粗体或半粗体，字号约 12 px |

右侧从左到右：

| 顺序 | 按钮/控件 | 外观 | 行为 |
|---:|---|---|---|
| 1 | 上一个/下一个关键帧组合按钮 | 一个小型双向尖括号/菱形导航图标，16-22 px 宽 | 跳到选中轨道的上一个/下一个关键帧；若没有选中轨道，跳到全局最近关键帧 |
| 2 | 时间缩放滑条 | 80-100 px 宽，灰色细轨道，小圆点滑块 | 调整每帧像素宽度，保持播放头位置不跳变 |
| 3 | 适配时间范围按钮 | 方框对角箭头图标 | 把所有节点范围和关键帧 fit 到可视区 |
| 4 | 放大按钮 | 放大镜加号图标 | 时间横向放大一级 |
| 5 | 展开/折叠层级按钮 | 上下双箭头或层级图标 | 展开/折叠所有节点参数行 |
| 6 | 更多菜单 | 三个横向圆点 | 打开菜单：显示全部参数、只显示动画参数、显示曲线、吸附、复制/粘贴关键帧 |

按钮视觉：

| 状态 | 样式要求 |
|---|---|
| normal | 背景透明或 `C.BG_DARK`，边框 `C.BORDER_DARK`，图标 `C.TEXT_HINT` |
| hover | 背景 `C.BG_HOVER`，图标 `C.TEXT_SECONDARY` |
| pressed/checked | 背景 `C.BG_ACTIVE`，边框/图标使用 `C.ORANGE` |
| disabled | 图标 `C.TEXT_HINT` 透明度降低，不响应 |

不要使用带文字的大按钮。顶部工具栏按钮都应是图标按钮，并提供 tooltip。

### 3.3 主编辑区左侧节点树

左侧树像一个窄表格。截图中左侧列背景为深灰，右侧时间区更深，两者之间有明显竖线。

列结构：

| 列 | 宽度 | 内容 |
|---|---:|---|
| 锁定列 | 24 px | 小锁图标，表示该节点/参数轨道是否锁定 |
| 展开列 | 18 px | 节点行有展开/折叠箭头，参数行为空 |
| 名称列 | 剩余 | 节点名或参数名 |

行类型：

| 行类型 | 缩进 | 字体 | 示例 | 行为 |
|---|---:|---|---|---|
| 节点行 | 名称 x=52 px 左右 | 11-12 px，`C.TEXT_SECONDARY` | `MediaIn1`、`Blur1`、`ColorCorrector1` | 可展开/折叠；点击选中节点；右键菜单 |
| 参数行 | 名称 x=72-78 px | 10-11 px，`C.TEXT_LABEL` | `Blur Size`、`色相`、`饱和度`、`色调角度`、`色调强度` | 点击选中参数轨道；右键菜单 |
| 空白/分组行 | 可选 | 更暗 | 用于节点组分隔 | 不参与编辑 |

树的展开规则：

| 状态 | 图标 | 显示 |
|---|---|---|
| 折叠节点 | 右箭头 | 只显示节点行 |
| 展开节点 | 下箭头 | 显示节点行和该节点可动画参数 |
| 无参数节点 | 无箭头或灰箭头 | 只显示节点行 |

锁图标：

| 状态 | 外观 | 行为 |
|---|---|---|
| 未锁 | 线框小锁，灰色 | 可编辑 |
| 已锁 | 闭合小锁，橙色或亮灰 | 该行关键帧和范围不可拖动 |
| 继承锁 | 参数行父节点锁定时显示更暗锁 | 参数不可编辑 |

树的滚动：

| 条件 | 行为 |
|---|---|
| 行数超过可视高度 | 左树和右时间区垂直滚动必须同步 |
| 滚动条位置 | 右侧主编辑区最右显示一条垂直滚动条；左树不单独显示第二条滚动条 |
| 悬停 | 悬停行左右两侧都高亮同一行 |

### 3.4 主编辑区右侧时间区

右侧时间区由一个标尺和多条轨道行组成。时间坐标横向连续，左到右为帧号或时间码。

标尺：

| 元素 | 外观 |
|---|---|
| 背景 | 比左树更深，接近 `C.BG_DARKEST` |
| 主刻度 | 竖线，颜色 `C.BORDER_DARK`，贯穿标尺和轨道区 |
| 次刻度 | 更短或更淡，仅在缩放较高时显示 |
| 文本 | 小号灰色数字，位于标尺上方或中线附近 |
| 示例标记 | `-100`、`0`、`100`、`200`、`300`、`400`、`500`、`600` |

时间区必须支持负帧显示，因为 Fusion 面板截图中左侧可见 `-100`。时间零点不一定在最左侧。

播放头：

| 部件 | 外观 |
|---|---|
| 垂直线 | 红色，1-2 px 宽，从标尺顶部贯穿到底部轨道区 |
| 顶部帽 | 红色小倒三角/水滴形标记，位于标尺顶端 |
| 当前帧标签 | 拖动时可显示小灰色浮层，如截图中的 `362` |
| 交互 | 拖动红线或顶部帽可 seek 当前帧；拖动时实时刷新 Composite preview |

轨道行内容：

| 轨道类型 | 颜色/形态 | 用途 |
|---|---|---|
| 素材/节点时长条，选中 | 使用该节点当前 `header_color/node_color` 的填充色，边框提高亮度，条内白字或黑字自适应 | 显示媒体或节点有效范围；支持拖动整体和左右边缘 |
| 素材/节点时长条，未选中 | 使用该节点当前 `header_color/node_color` 的低饱和/低透明版本，浅灰或同色边框 | 显示未选中节点范围；颜色必须和节点图中的素材节点颜色一致 |
| 参数曲线，绿色 | 细线，1 px | 数值参数曲线，默认使用节点颜色或参数通道颜色 |
| 参数曲线，蓝色/黄色 | 细线 | 多个参数轨道区分颜色，例如 hue/saturation/angle/strength |
| 关键帧点 | 小菱形或短白色竖线，选中为橙色 | 显示可拖拽关键帧 |
| 曲线控制点 | 细白竖线，中心有小点或短横 | 缩放足够大时显示 |

时长条文本布局：

| 位置 | 内容 |
|---|---|
| 左内边距 4 px | 起始帧数字，如 `0` |
| 中间 | 媒体名或节点名，如 `TT 补调色 2.0.mov` |
| 右内边距 4 px | 结束帧数字，如 `472` |

时长条交互命中区：

| 区域 | 宽度 | 鼠标形状 | 行为 |
|---|---:|---|---|
| 左边缘 | 6 px | 水平 resize | 改 start/source_in/comp_in |
| 右边缘 | 6 px | 水平 resize | 改 end/source_out/comp_out |
| 中间 | 剩余 | open hand/closed hand | 整体移动 offset，不改变长度 |
| 空白轨道 | - | cross/arrow | 点击定位播放头，双击可新增关键帧 |

### 3.5 底部状态/滚动栏

截图底部左侧有两个小按钮，中间有水平滚动条，右侧有下拉框 `时间` 和一个小输入框。

从左到右：

| 顺序 | 控件 | 外观 | 行为 |
|---:|---|---|---|
| 1 | 水平适配/平移按钮 | 小图标，类似左右箭头和竖线 | 切换 pan/fit 或跳到播放头 |
| 2 | 表格/列表按钮 | 小网格图标 | 切换 Keyframes 列表视图/轨道视图，第一版可禁用但保留按钮位置 |
| 3 | 水平滚动条 | 灰色滑块，居中偏下 | 横向滚动时间区 |
| 4 | 时间单位下拉框 | 小型 combo，文本默认 `时间` | 选项：`时间`、`帧`、`秒`、`时间码` |
| 5 | 数值/过滤输入框 | 深色输入框，无默认文字 | 可输入当前帧或过滤内容；第一版可作为当前帧输入 |

底部栏背景使用 `C.BG_PANEL`，顶部用 1 px 边框和主编辑区分隔。

## 4. 视觉风格映射到项目全局样式

用户要求面板配色、按钮样式直接使用项目全局配色与样式。因此实现中不要硬编码大量 DaVinci 色值，而是用 `src/shared/ui/style_sheet.py` 的 `C` 和现有 QSS。

推荐映射：

| UI 语义 | 推荐项目 token |
|---|---|
| 面板标题背景 | `C.BG_DARK` |
| 主编辑区背景 | `C.BG_DARKEST` |
| 左侧树背景 | `C.BG_PANEL` |
| 标尺背景 | `C.BG_DARK` |
| 普通边框/网格 | `C.BORDER_DARK` 或 `C.BORDER` |
| 文本主色 | `C.TEXT_SECONDARY` |
| 弱文本 | `C.TEXT_HINT` |
| 选中/关键帧高亮 | `C.ORANGE` |
| 播放头 | 固定红色，可新增 `KEYFRAME_PLAYHEAD` 常量；不要复用橙色 |
| 素材/节点时长条 | 默认使用对应节点的 `header_color/node_color`；选中时提高亮度和边框对比 |
| 未选中素材/节点时长条 | 对应节点颜色的低透明/低饱和版本，不能退化成统一灰色 |
| 曲线色 | 节点 header_color、通道色或固定绿/蓝/黄 |

需要新增样式常量时，放在 `src/shared/ui/style_sheet.py`，命名建议：

| 常量 | 用途 |
|---|---|
| `COMPOSITE_KEYFRAMES_PANEL_STYLE` | 整个 Keyframes 面板 |
| `COMPOSITE_KEYFRAMES_TOOLBAR_STYLE` | 顶部栏 |
| `COMPOSITE_KEYFRAMES_TREE_STYLE` | 左侧树 |
| `COMPOSITE_KEYFRAMES_COMBO_STYLE` | 底部下拉框 |
| `COMPOSITE_KEYFRAMES_ICON_BTN_STYLE` | 图标按钮 |

节点颜色同步要求：

| 场景 | 要求 |
|---|---|
| 新建节点 | Keyframes 面板该节点行和素材/节点时长条使用节点类默认 `header_color` |
| 在 Properties 面板修改节点颜色 | Node Graph 的节点头、Properties section 头、Minimap、Keyframes 面板行色/范围条颜色必须实时同步 |
| 保存项目 | 自定义节点颜色随 `Node.to_dict()` 保存 |
| 加载项目 | `Node.from_dict()` 恢复自定义节点颜色，Keyframes 面板首次构建即使用恢复后的颜色 |
| Undo/Redo 颜色修改 | Keyframes 面板颜色随 undo/redo 回退或重做 |

## 5. 数据模型设计

### 5.1 每个节点独立保存动画轨道

必须新增每个节点实例自己的动画轨道字段，避免直接修改类级 `PARAM_DEFS`。

建议在 `src/features/composite/nodes/base_node.py` 的 `Node` 中新增：

```python
self.keyframe_tracks: Dict[str, Dict[str, Any]] = {}
self.time_range: Optional[Dict[str, Any]] = None
self.custom_header_color: Optional[str] = None
```

序列化结构：

```python
{
    "id": "...",
    "type": "transform",
    "position": [100, 200],
    "node_color": "#7a6a9a",
    "parameters": {
        "position_x": 0.5,
        "position_y": 0.5
    },
    "keyframe_tracks": {
        "position_x": {
            "param_name": "position_x",
            "keyframes": [
                {"frame": 0, "value": 0.5, "interpolation": "linear"},
                {"frame": 72, "value": 0.8, "interpolation": "ease_in_out"}
            ],
            "min_val": -10.0,
            "max_val": 10.0
        }
    },
    "time_range": {
        "enabled": true,
        "comp_start": 0,
        "comp_end": 472,
        "source_in": 0,
        "source_out": 472,
        "offset": 0
    },
    "disabled": false
}
```

注意：

| 字段 | 说明 |
|---|---|
| `keyframe_tracks` | 每个参数一条轨道，使用 `KeyframeTrack.to_dict()` 格式 |
| `time_range` | 节点在合成中的有效时间范围，媒体节点还映射到源入点/出点 |
| `node_color` | 用户自定义节点颜色；缺失时使用节点类默认 `header_color` |
| `parameters` | 保留当前帧或默认值，不把关键帧列表塞进普通参数值 |
| `PARAM_DEFS` | 只负责参数元数据，不保存用户关键帧状态 |

命名约束：

| 旧/候选名称 | 最终建议 | 说明 |
|---|---|---|
| `animation_tracks` | 不建议 | 容易和 Edit 页面 animated params 混淆 |
| `keyframe_tracks` | 推荐 | 与 `NodeSnapshot.keyframe_tracks`、保存字段同名 |
| `node_color` | 保存字段推荐 | 面向项目数据，表达用户看到的节点颜色 |
| `header_color` | 运行时属性保留 | 兼容现有 NodeItem/PropertiesPanel 读取 |

### 5.2 GraphSnapshot 来源改造

当前 `NodeGraph.snapshot()` 从 `node.PARAM_DEFS` 收集 keyframe。应改为：

| 步骤 | 要求 |
|---|---|
| 1 | 遍历 `node.keyframe_tracks` |
| 2 | 对每条轨道补齐 `min_val/max_val`，来源为对应 `ParamDef` |
| 3 | 填入 `NodeSnapshot.keyframe_tracks` |
| 4 | `GraphEvaluatorMixin._interpolate_parameters()` 保持使用 `NodeSnapshot.keyframe_tracks` |
| 5 | 若当前帧在节点 `time_range` 外，对普通效果节点应 bypass 或 disabled，对源节点应返回空/黑/hold，具体按节点类型策略 |

### 5.3 时间范围策略

节点时长条不等于参数关键帧。它定义节点何时生效或媒体读取范围。

| 节点类型 | 默认范围 | 拖动左/右边缘时写入 |
|---|---|---|
| `timeline_clip_input` | 当前 Edit 时间线 clip 的持续帧数 | 第一版只写入 Composite `time_range`，不直接改 Edit 时间线 clip 长度 |
| `video_frame` | `source_in` 到 `source_out`，若 `source_out=-1` 使用 `total_frames-1` | 写 `source_in/source_out` 和 `time_range` |
| `image_input` | 静帧默认覆盖合成范围；序列使用 `frame_start/frame_end` | 静帧写 `time_range`，序列写 `frame_start/frame_end` |
| `retime` | 输入范围 | 第一版只显示范围，后续可映射 speed ramp |
| `time_offset` | 输入范围 | 拖动中间可写 `frame_offset` |
| 普通效果节点 | 合成全范围 | 写 `time_range`，范围外 bypass |
| `output` | 合成全范围 | 只读或隐藏时长条 |

### 5.4 当前帧和总帧数来源

新增一个轻量状态对象，供 Keyframes 面板和属性面板共享：

```python
@dataclass
class CompositeTimelineState:
    start_frame: int = 0
    end_frame: int = 100
    current_frame: int = 0
    fps: float = 24.0
    display_mode: str = "time"  # time, frame, seconds, timecode
```

来源优先级：

| 优先级 | 来源 |
|---:|---|
| 1 | Edit 时间线 clip context：clip duration、source_frame、timeline fps |
| 2 | Composite runtime context：当前 frame_number |
| 3 | VideoFrameNode metadata：`total_frames/fps` |
| 4 | 默认 0-100 帧，24fps |

当前帧规范：

| 场景 | `GraphSnapshot.frame_number` 应使用 |
|---|---|
| Edit 时间线中的 Comp Clip | `clip_local_frame`，也就是合成片段内部帧号 |
| TimelineClipInput 读取素材帧 | 继续用 `runtime_context.source_frame` 读取素材，不要用它驱动参数关键帧 |
| 独立 Composite / VideoFrameNode | Keyframes 面板当前帧 |
| 导出 | 导出循环传入的合成帧号，和预览使用同一坐标系 |

所有 `CompositePage` 内部调用 `self._node_graph.snapshot(...)` 的路径都必须通过一个 helper，例如 `_current_keyframe_frame()`，统一传入 `frame_number`。至少包括：`on_enter()`、`_retry_renderer_init()`、`update_color_output()`、`_do_evaluate_graph()`、prefetch snapshot、viewer interaction flush。

### 5.5 多维参数和表达式策略

| 场景 | 规则 |
|---|---|
| X/Y 参数保持组合状态 | UI 可以显示一个共享 `◆`，但数据层必须保存 X、Y 两条独立轨道，不能把 X 的 value 复制给 Y |
| X/Y 参数 linked | 新增/移动关键帧时两条轨道的 frame 同步，但 value 使用各自当前值或各自输入值 |
| X/Y 参数 separated | 两条轨道完全独立，Keyframes 面板显示两个参数行 |
| int 参数 | 插值后在进入节点参数前 round/cast 为 int，并按 min/max clamp |
| float 参数 | 保持 float |
| vec2/vec3 参数 | 第一版拆成多个 scalar 子轨道；后续可做一等多维轨道 |
| expression + keyframes | 必须定义互斥或优先级；建议默认互斥：启用 expression 时禁用关键帧按钮，启用 keyframes 时提示清除 expression |

## 6. 文件与模块规划

新增文件：

| 文件 | 责任 |
|---|---|
| `src/features/composite/ui/keyframes_panel.py` | 顶层面板，组装 toolbar、tree、timeline、footer |
| `src/features/composite/ui/keyframes_model.py` | 从 NodeGraph 生成树行、轨道行、关键帧点、时间范围数据 |
| `src/features/composite/ui/keyframes_timeline_view.py` | 右侧时间标尺/轨道绘制与鼠标交互 |
| `src/features/composite/ui/keyframes_tree_view.py` | 左侧节点树绘制与展开/锁定/选择 |
| `src/features/composite/ui/keyframes_commands.py` | Undo/Redo 命令：移动关键帧、修改范围、增删关键帧 |
| `src/features/composite/ui/spline_panel.py` | 第三阶段 Spline 顶层面板，组装曲线树、曲线视图、底部工具栏 |
| `src/features/composite/ui/spline_model.py` | 第三阶段 Spline 曲线数据模型，负责 keyframe_tracks → curves/segments/handles |
| `src/features/composite/ui/spline_view.py` | 第三阶段 Spline 自绘曲线图、贝塞尔手柄、命中测试 |
| `src/features/composite/ui/spline_commands.py` | 第三阶段 Spline Undo/Redo：拖点、拖柄、切线模式 |
| `tests/composite/test_composite_keyframes_model.py` | 模型和序列化测试 |
| `tests/ui/test_composite_keyframes_panel.py` | UI 状态、信号和基础交互测试 |
| `tests/composite/test_spline_model.py` | Spline 曲线模型测试 |
| `tests/composite/test_spline_bezier_math.py` | 贝塞尔曲线数学和采样测试 |
| `tests/ui/test_spline_view_hit_test.py` | Spline 命中测试 |
| `tests/ui/test_spline_view_render.py` | Spline 离屏绘制测试 |

修改文件：

| 文件 | 修改点 |
|---|---|
| `src/features/composite/nodes/base_node.py` | 新增节点级 `keyframe_tracks/time_range`，序列化和反序列化 |
| `src/features/composite/core/node_engine.py` | snapshot 从节点实例读取 keyframe/time_range；to_dict/from_dict 兼容；frame_number 一致传递 |
| `src/features/composite/core/graph_migration.py` | schema bump，迁移 legacy ParamDef/参数 dict 关键帧、自定义 node_color/time_range |
| `src/features/composite/evaluators/base_evaluator.py` | 当前帧外的节点范围处理；关键帧插值保持现有入口；int 参数 cast |
| `src/features/composite/render/composite_render_output.py` | `detect_composite_mode()` 和 `resolve_video_range()` 支持 per-node keyframe_tracks/time_range |
| `src/features/composite/ui/keyframe_editor.py` | 修复 Animated 关闭仍写入默认 keyframes、重复帧、多维值策略 |
| `src/features/composite/ui/node_properties_panel.py` | `◆` 按钮改为读写节点级动画轨道，和新面板同步；颜色/锁定状态通知 |
| `src/features/composite/ui/composite_page.py` | 创建/绑定 KeyframesPanel 和第三阶段 SplinePanel，转发当前帧、选择、图变更、undo、node color changed |
| `src/features/composite/module.py` | 注册 `composite.keyframes` 和第三阶段 `composite.spline` PanelDescriptor |
| `src/app/controllers/dock_layout_controller.py` | 如 PanelRegistry 不能自动创建，需要新增底部 dock 布局 |
| `src/features/composite/locales/zh_CN.json`、`en_US.json` | 新增面板标题、tooltip、菜单文本 |
| `tests/project/test_keyframe.py` | 更新旧 ParamDef keyframe 测试，补节点级 keyframe 测试 |
| `tests/composite/test_node_properties_panel_xy.py` | 更新 X/Y keyframe 预期：共享按钮不等于共享 value |

## 7. 分阶段实施计划表

| 阶段 | 目标 | 主要任务 | 交付物 | 验收标准 |
|---|---|---|---|---|
| P0 | 架构审计和防回归 | 按 A-1 到 A-16 建立缺陷清单；锁定旧 ParamDef 关键帧入口、信号断点、frame_number 断点、颜色保存断点 | 审计说明和失败测试草案 | 每个已知缺口都有对应修复任务和测试 |
| P1 | 节点级动画数据 | 为 `Node` 增加 `keyframe_tracks/time_range/node_color`；序列化；迁移 legacy 数据；snapshot 改读节点实例；更新模式检测 | 数据模型和单测 | 两个 Transform 节点同名参数关键帧互不污染；自定义节点颜色可保存恢复 |
| P6 | 与属性面板同步 | 属性面板 `◆` 按钮新增/删除关键帧后，Keyframes 面板刷新；Keyframes 面板编辑后，属性面板状态刷新；节点颜色修改实时同步到 range bar | 双向信号绑定 | 两边状态始终一致，不需要重新选择节点；颜色即时同步 |
| P7 | 保存、加载、Undo | 关键帧、time_range、node_color 随 Composite graph 保存；撤销/重做恢复 UI 和渲染 | 保存加载测试 | 重启项目后关键帧、展开状态、范围条、节点颜色仍正确 |
| P8 | Timeline/Playback 同步 | 当前帧来自 Edit/Composite 播放状态；所有 preview snapshot 传 `frame_number`；播放时红色播放头实时移动；默认居中 | 当前帧同步逻辑 | 播放、暂停、seek 后 Keyframes 面板位置正确，预览按关键帧变化 |
| P10A | 关键帧系统测试闸门 | 跑通节点级存储、属性面板、snapshot、preview/export、保存加载、Undo/Redo、颜色同步测试 | 测试报告 | 关键帧系统全绿后，才允许开始 Keyframes 面板 |
| P2 | Keyframes 数据模型 | 实现 `KeyframesModel`，从 NodeGraph 生成节点行、参数行、range bar、keyframe points，并把 range bar color 绑定到 node_color | `keyframes_model.py` 和测试 | 展开/折叠、过滤动画参数、选中节点和颜色输出正确 |
| P3 | 面板 Dock 和静态 UI | 新建 `KeyframesPanel`，注册到 Composite page；完成顶部栏、左树、右标尺、底部栏的静态布局 | 可打开的底部 Keyframes dock | Composite 页面可显示/隐藏面板，尺寸和布局接近截图 |
| P4 | 轨道绘制 | 用单个 QWidget/QPainter 绘制标尺、网格、播放头、范围条、曲线、关键帧点；支持滚动和缩放 | `keyframes_timeline_view.py` | 100 个节点、1000 个关键帧滚动不卡顿 |
| P5 | 基础交互 | 点击/拖动播放头；拖动关键帧；拖动范围条边缘/主体；多选关键帧；右键菜单 | 可实际编辑关键帧和时长 | 拖动时实时更新预览，松手后进入 undo |
| P9 | UI 细节 polish | tooltip、i18n、键盘快捷键、hover/selection、菜单、空状态、锁定状态 | 视觉完善 | 非多模态验收清单全部通过 |
| P10B | 全量回归和性能测试 | pytest、手动 4K/长时间线测试、拖拽流畅度测试 | 测试报告 | 不破坏 Composite 现有节点编辑和渲染 |
| P11 | Spline 曲线模型和绘制 | 新增 SplineModel、曲线视图、贝塞尔曲线采样/绘制、切线模式数据 | Spline 数据模型和只读曲线视图 | 曲线能准确显示 keyframe track，贝塞尔段不变形、不反向、不跳线 |
| P12 | Spline 曲线交互 | 关键帧点/控制柄拖拽、切线模式、框选、多曲线显示、Undo/Redo | 可编辑 Spline 面板 | 拖拽控制柄后预览/导出插值一致，测试全绿 |

## 8. 具体任务拆解

### P0 详细任务：现有关键帧系统修复清单

| 编号 | 任务 | 文件 | 验收 |
|---|---|---|---|
| P0.1 | 写一个失败测试证明两个同类节点不能共享关键帧状态 | `tests/composite/test_composite_keyframes_serialization.py` | 当前代码失败，P1 后通过 |
| P0.2 | 写一个失败测试证明 `NodePropertiesPanel.keyframes_changed` 必须进入 `CompositePage` | `tests/ui/test_composite_keyframes_panel.py` | 修改属性面板 keyframe 后 NodeGraph 有轨道、renderer 被请求 |
| P0.3 | 写一个失败测试证明关闭 Animated 应删除轨道，而不是写入默认两点轨道 | `tests/composite/test_node_properties_panel_xy.py` | 取消动画后按钮 unchecked，轨道为空 |
| P0.4 | 写一个失败测试证明 X/Y shared button 不应复制 X value 到 Y value | `tests/composite/test_node_properties_panel_xy.py` | X/Y frame 可同步，但 value 保持各自轨道 |
| P0.5 | 写一个失败测试证明 preview snapshot 必须携带当前 frame_number | `tests/composite/test_composite_keyframe_evaluation.py` | `_do_evaluate_graph()` 传入 runtime frame |
| P0.6 | 写一个失败测试证明 node_color 保存/加载后保持一致 | `tests/composite/test_composite_keyframes_serialization.py` | 自定义颜色恢复后 KeyframesModel 输出同色 |

### P1 详细任务：数据模型

| 编号 | 任务 | 文件 | 备注 |
|---|---|---|---|
| P1.1 | 在 `Node.__init__` 增加 `self.keyframe_tracks = {}` 和 `self.time_range = None` | `base_node.py` | 使用唯一命名 `keyframe_tracks`，不要再引入 `animation_tracks` |
| P1.2 | `Node.to_dict()` 序列化非空 `keyframe_tracks/time_range/node_color` | `base_node.py` | 深拷贝，避免保存过程修改 live state |
| P1.3 | `Node.from_dict()` 读取上述字段，并把 `node_color` 应用到 `node.header_color` | `base_node.py` | 缺失字段兼容旧项目 |
| P1.4 | 添加 `Node.set_keyframe_track(param_name, track_dict)` | `base_node.py` | 内部 bump `param_revision` |
| P1.5 | 添加 `Node.remove_keyframe_track(param_name)` | `base_node.py` | 删除后同步 UI |
| P1.6 | 修改 `NodeGraph.snapshot()` | `node_engine.py` | 从节点实例而不是 `PARAM_DEFS` 收集轨道 |
| P1.7 | 单测两个同类节点互不影响 | `tests/composite/test_composite_keyframes_model.py` | 必测 |
| P1.8 | 修改 `KeyframeTrack.add_keyframe()` 或新增安全 API，定义同帧替换/拒绝重复策略 | `src/shared/keyframe.py` | 不允许产生重复 frame |
| P1.9 | 更新 `detect_composite_mode()`、`resolve_video_range()` | `composite_render_output.py` | per-node keyframe/time_range 可让 still graph 被识别为 video |
| P1.10 | bump composite schema，迁移旧格式 | `graph_migration.py` | 老项目正常打开，新字段默认合理 |
| P1.11 | 定义 expression/keyframe 互斥策略 | `base_node.py`、`node_properties_panel.py` | 同一参数不会同时有 expression 和 keyframe 生效 |

### P2 详细任务：KeyframesModel

模型输出行结构建议：

```python
@dataclass
class KeyframesRow:
    row_id: str
    node_id: str
    param_name: str | None
    label: str
    row_type: Literal["node", "param"]
    depth: int
    expanded: bool
    locked: bool
    selected: bool
    visible: bool
```

轨道结构建议：

```python
@dataclass
class KeyframesTrackVisual:
    row_id: str
    range_bar: TimeRangeBar | None
    keyframes: list[KeyframePointVisual]
    curve_segments: list[CurveSegmentVisual]
    color: str
    node_color: str
```

任务表：

| 编号 | 任务 | 要点 |
|---|---|---|
| P2.1 | 从 NodeGraph 按拓扑/编辑器顺序生成节点行 | 顺序应接近节点图视觉顺序，第一版可按 `node.position.y/x` 排序 |
| P2.2 | 只为 animatable 参数生成参数行 | `ParamDef.is_animatable()` 为真，或已有 keyframe_tracks |
| P2.3 | 实现筛选 | 全部节点、只显示有动画参数、只显示选中节点 |
| P2.4 | 实现展开状态持久化 | 放在 panel UI state，不写入渲染参数 |
| P2.5 | 输出帧到 x 坐标映射 | 支持负帧、缩放、滚动 |
| P2.6 | 输出节点颜色 | 每个节点行和 range bar 都带 `node.header_color`；颜色变更后模型能局部刷新 |
| P2.7 | 输出锁定状态 | 第一版可 UI-only，但模型必须有 row/track locked 字段 |

### P3/P4 详细任务：绘制 UI

绘制建议：

| 组件 | 实现方式 | 原因 |
|---|---|---|
| 左侧树 | 自绘 QWidget 或 QTreeView + 自定义 delegate | 行数可能多；自绘更容易和右侧同步 |
| 右侧时间区 | 自绘 QWidget/QPainter | 关键帧多时性能更好 |
| 滚动 | QScrollBar 控制虚拟偏移 | 不为每行创建 widget |
| 缩放 | float `pixels_per_frame` | 例如 0.25 到 40 px/frame |
| 播放头 | 每次 paint 最后绘制 | 保证覆盖在所有条/曲线之上 |

绘制顺序：

| 顺序 | 内容 |
|---:|---|
| 1 | 背景 |
| 2 | 行 hover/selection 背景 |
| 3 | 竖向网格和标尺 |
| 4 | 节点时长条 |
| 5 | 参数曲线 |
| 6 | 关键帧点 |
| 7 | 播放头 |
| 8 | 拖拽浮层和 frame tooltip |

### P5 详细任务：交互

| 操作 | 行为 |
|---|---|
| 点击节点行 | 选中对应 Composite 节点，并让属性面板显示该节点 |
| 点击参数行 | 选中参数轨道，不改变节点选择或同时选中父节点 |
| 单击时间空白 | seek 到对应帧 |
| 拖播放头 | 实时 seek；节流到 16 ms；当前帧标签跟随鼠标 |
| 双击参数轨道空白 | 在该帧添加关键帧，值取当前插值值 |
| 选中关键帧后 Delete | 删除关键帧 |
| 删除最后一个关键帧 | 删除整条轨道并取消属性面板 `◆` checked 状态 |
| 拖动关键帧 | 横向移动帧；按 Shift 禁用吸附；默认吸附整数帧 |
| 框选 | 多选关键帧 |
| 拖动多个关键帧 | 保持相对距离，遇到边界整体 clamp |
| 拖动时长条左边缘 | 修改开始帧或 source_in |
| 拖动时长条右边缘 | 修改结束帧或 source_out |
| 拖动时长条中间 | 修改 offset，整体移动 |
| 右键关键帧 | 菜单：Delete、Copy、Paste、Interpolation 子菜单 |
| 右键轨道 | 菜单：Add Keyframe、Show Only Animated、Lock Track |
| 修改节点颜色 | Keyframes 面板不打开颜色选择器，但必须监听外部颜色变更并立即重绘对应 range bar |

### P6/P7 详细任务：同步和撤销

| 事件来源 | 应通知 |
|---|---|
| Keyframes 面板增删关键帧 | NodeGraph 数据、属性面板 `◆`、Composite 预览、undo stack、save dirty |
| 属性面板 `◆` 弹窗修改 | Keyframes 面板模型刷新、Composite 预览、undo stack、save dirty |
| 属性面板修改节点颜色 | NodeGraph `node.header_color/node_color`、NodeItem、Minimap、Keyframes 面板 range bar、undo stack、save dirty |
| 节点选择改变 | Keyframes 面板行选择/滚动到节点 |
| 播放头改变 | 属性面板 current frame、Keyframes 面板红线、Composite renderer frame_number |
| Graph load | Keyframes 面板重建模型 |
| Undo/Redo | Keyframes 面板刷新，属性面板刷新，renderer 重新 evaluate |

Undo action 建议：

| Action | 数据 |
|---|---|
| `CompositeMoveKeyframesAction` | node_id、param_name、old_frames、new_frames |
| `CompositeSetKeyframeTrackAction` | node_id、param_name、old_track、new_track |
| `CompositeSetNodeTimeRangeAction` | node_id、old_range、new_range |
| `CompositeSetNodeColorAction` | node_id、old_color、new_color |
| `CompositeBulkKeyframesAction` | 多个 action 合并，用于框选拖动 |

必须新增的 CompositePage slots/signals：

| 名称 | 责任 |
|---|---|
| `_on_props_keyframes_changed(node_id, node_type, param_name, keyframes)` | 接收属性面板 `◆` 变更，写入节点级轨道，push undo，evaluate，刷新 KeyframesPanel |
| `_on_keyframes_panel_track_changed(...)` | 接收 KeyframesPanel 编辑，写入 NodeGraph，刷新 PropertiesPanel |
| `_on_keyframes_panel_time_range_changed(...)` | 写入节点 time_range 或媒体节点 source_in/out |
| `_on_props_node_color_changed(...)` | 现有函数需扩展：保存颜色、push undo、发 dirty、通知 KeyframesPanel |
| `keyframe_frame_changed(int)` | 当前合成帧改变时，通知属性面板和 KeyframesPanel |

### P11/P12 第三阶段：Spline 曲线图表编辑器

第三阶段目标：实现一个类似 DaVinci Fusion `Spline` 面板的曲线图表编辑器，用来精确编辑关键帧之间的插值曲线、贝塞尔切线和数值变化。Spline 面板不是 Keyframes 面板的替代品：Keyframes 面板负责“时间层级、范围条、关键帧位置”，Spline 面板负责“参数值随时间变化的曲线形状”。

硬性前置：

| 前置项 | 要求 |
|---|---|
| P10A | 关键帧系统底层测试全绿 |
| P10B | Keyframes 面板基础显示/选择/拖动/保存回归全绿 |
| 数据层 | `Keyframe` 的 `bezier_in/bezier_out` 能保存、加载、Undo/Redo |
| 当前帧 | Spline 面板和 Keyframes 面板使用同一 `CompositeTimelineState` |

#### P11.1 Spline UI 布局文字规格

整体布局参考 DaVinci Fusion Spline 面板：

| 区域 | 位置 | 内容 |
|---|---|---|
| 顶部标题栏 | 最上方 30 px | 左侧标题 `Spline`；右侧是缩放/fit/更多菜单等图标按钮 |
| 左侧曲线树 | 左侧约 235 px | 节点层级、参数曲线勾选框、曲线颜色点 |
| 右侧曲线图 | 主区域 | 横轴是时间/帧，纵轴是参数值，显示网格、曲线、关键帧点、贝塞尔手柄、播放头 |
| 底部工具栏 | 底部约 36 px | 切线模式按钮、选择/框选、fit、时间/数值下拉框、当前值输入框 |

左侧树：

| 行类型 | 例子 | 外观 | 行为 |
|---|---|---|---|
| 节点行 | `Blur1`、`ColorCorrector1` | 左侧展开箭头 + checkbox + 节点名 | checkbox 控制该节点全部曲线显示 |
| 参数行 | `Blur Size` | 缩进，checkbox + 参数名 + 右侧小色点 | checkbox 控制该参数曲线显示；色点显示曲线颜色 |
| 未启用曲线 | `ColorCorrector1` 折叠/暗色 | 文本更暗 | 不绘制曲线 |

右侧曲线图：

| 元素 | 外观 |
|---|---|
| 背景 | 使用 `C.BG_DARKEST`，比左侧树更深 |
| 纵横网格 | 主网格 `C.BORDER_DARK`，次网格更淡 |
| 时间标尺 | 顶部显示 frame/time，例如 `-50`、`0`、`50`、`100` |
| 数值标尺 | 左侧显示参数值，例如 `-2`、`0`、`2`、`4`、`14` |
| 播放头 | 红色垂直线，顶部红色倒三角 |
| 曲线 | 默认使用参数/节点颜色，选中曲线更亮更粗 |
| 关键帧点 | 小方点或小菱形；选中为白边/橙色填充 |
| 贝塞尔手柄 | 细灰白连线 + 小方形控制点；选中时更亮 |

底部工具栏按钮顺序建议：

| 顺序 | 按钮 | 图标语义 | 行为 |
|---:|---|---|---|
| 1 | Smooth | 平滑弧线 | 设置选中 keyframe 为 smooth/auto tangent |
| 2 | Linear | 直线 | 设置选中段或点为 linear |
| 3 | Ease | 虚线弧线 | 设置 ease in/out |
| 4 | Step/Hold | 阶梯线 | 设置 hold/step |
| 5 | Break Tangents | 断开的两段柄 | 断开左右切线 |
| 6 | Join Tangents | 连续切线 | 重新联动左右切线 |
| 7 | Free Handle | 笔/手柄 | 自由拖动控制柄 |
| 8 | Box Select | 虚线矩形 | 框选点和手柄 |
| 9 | Fit Curves | 框选适配 | 适配显示选中曲线 |
| 10 | Time 下拉 | `Time` | 横轴显示模式 |
| 11 | Value 下拉 | `Value` | 纵轴显示模式 |
| 12 | 当前值输入 | 数字框 | 编辑选中 keyframe value |

#### P11.2 数据模型：不要把 Spline 画法写死在 UI 里

新增建议文件：

| 文件 | 责任 |
|---|---|
| `src/features/composite/ui/spline_model.py` | 把节点级 `keyframe_tracks` 转成曲线、点、控制柄、可见范围 |
| `src/features/composite/ui/spline_view.py` | 自绘曲线图、网格、点、手柄、播放头 |
| `src/features/composite/ui/spline_panel.py` | 顶层 Spline 面板，组装树、view、toolbar |
| `src/features/composite/ui/spline_commands.py` | Undo/Redo 命令：拖点、拖柄、切线模式 |

Spline 数据结构建议：

```python
@dataclass
class SplineCurve:
    curve_id: str                  # f"{node_id}:{param_name}"
    node_id: str
    param_name: str
    label: str
    color: str
    visible: bool
    selected: bool
    min_value: float
    max_value: float
    keyframes: list[SplineKey]

@dataclass
class SplineKey:
    frame: int
    value: float
    interpolation: str             # linear, bezier, hold, ease_in, ease_out, ease_in_out
    tangent_mode: str              # auto, smooth, broken, linear, flat, step
    handle_left: QPointF           # data-space absolute point: (frame, value)
    handle_right: QPointF          # data-space absolute point: (frame, value)
    selected: bool = False
```

重要：`handle_left/handle_right` 在 SplineModel 内部使用“数据空间绝对坐标”，不要在模型中保存屏幕像素坐标。保存到 `Keyframe.to_dict()` 时再转换为相对归一化或相对 frame/value delta。

保存格式建议继续兼容 `src/shared/keyframe.py`：

```python
{
    "frame": 100,
    "value": 12.0,
    "interpolation": "bezier",
    "tangent_mode": "smooth",
    "bezier_in": [-33.3333, -1.2],
    "bezier_out": [33.3333, 1.2]
}
```

`bezier_in/bezier_out` 推荐改为“数据空间 delta”：`bezier_in = handle_left - key_point`，`bezier_out = handle_right - key_point`。如果为了兼容旧格式仍使用 0-1 normalized，则必须在 SplineModel 入口统一转换成 absolute handle，并在保存时统一转换回去。不要让绘制层同时处理两种格式。

#### P11.3 坐标系和变换：绘制前必须先统一

定义两个坐标系：

| 坐标系 | x | y | 用途 |
|---|---|---|---|
| 数据空间 data | frame | parameter value | keyframe、曲线、控制柄的真实值 |
| 屏幕空间 screen | widget pixel x | widget pixel y，向下为正 | QPainter 绘制 |

SplineView 必须维护 viewport：

```python
@dataclass
class SplineViewport:
    frame_min: float
    frame_max: float
    value_min: float
    value_max: float
    plot_rect: QRectF
```

转换函数必须集中实现：

```python
def data_to_screen(p: QPointF) -> QPointF:
    x = plot.left() + (p.x() - frame_min) / (frame_max - frame_min) * plot.width()
    y = plot.bottom() - (p.y() - value_min) / (value_max - value_min) * plot.height()
    return QPointF(x, y)

def screen_to_data(p: QPointF) -> QPointF:
    frame = frame_min + (p.x() - plot.left()) / plot.width() * (frame_max - frame_min)
    value = value_min + (plot.bottom() - p.y()) / plot.height() * (value_max - value_min)
    return QPointF(frame, value)
```

边界要求：

| 情况 | 处理 |
|---|---|
| `frame_max == frame_min` | 扩展为 `frame_min-1` 到 `frame_max+1` |
| `value_max == value_min` | 扩展为 `value_min-1` 到 `value_max+1` |
| plot 宽高小于 1 | 不绘制曲线，直接返回 |
| value 超出视图 | 允许曲线被裁剪，不要 clamp 曲线数据 |
| frame 超出视图 | 可跳过完全不可见段，部分可见段正常绘制 |

#### P11.4 贝塞尔曲线数学模型

每两个相邻关键帧形成一个 segment。设：

```python
K0 = QPointF(frame0, value0)
K1 = QPointF(frame1, value1)
P0 = K0
P1 = K0.handle_right
P2 = K1.handle_left
P3 = K1
```

三次贝塞尔公式：

```python
B(t) = (1 - t)^3 * P0
     + 3 * (1 - t)^2 * t * P1
     + 3 * (1 - t) * t^2 * P2
     + t^3 * P3
```

其中 `t` 从 0 到 1。实现函数：

```python
def cubic_bezier(p0: QPointF, p1: QPointF, p2: QPointF, p3: QPointF, t: float) -> QPointF:
    u = 1.0 - t
    return (
        p0 * (u * u * u)
        + p1 * (3.0 * u * u * t)
        + p2 * (3.0 * u * t * t)
        + p3 * (t * t * t)
    )
```

如果 Qt 的 `QPointF` 不支持直接乘法，就显式算 x/y：

```python
def cubic_bezier_xy(p0, p1, p2, p3, t):
    u = 1.0 - t
    b0 = u * u * u
    b1 = 3.0 * u * u * t
    b2 = 3.0 * u * t * t
    b3 = t * t * t
    return QPointF(
        b0 * p0.x() + b1 * p1.x() + b2 * p2.x() + b3 * p3.x(),
        b0 * p0.y() + b1 * p1.y() + b2 * p2.y() + b3 * p3.y(),
    )
```

贝塞尔控制点约束：

| 约束 | 原因 |
|---|---|
| `frame0 <= P1.x <= frame1` | 防止曲线在时间轴上回头 |
| `frame0 <= P2.x <= frame1` | 防止曲线自交或 loop |
| `P1.x <= P2.x` 推荐但不强制 | 保持参数随时间单调采样更稳定 |
| value 不强制 clamp | 允许 overshoot，但 fit view 要能包含 overshoot |
| hold/step 段不使用 P1/P2 | 阶梯绘制独立处理 |

拖动手柄时要 clamp x：

```python
handle_right.x = clamp(mouse_data.x, key.frame, next_key.frame)
handle_left.x = clamp(mouse_data.x, prev_key.frame, key.frame)
```

#### P11.5 贝塞尔绘制：必须按这个顺序实现

不要直接在 `paintEvent()` 里边遍历边猜。必须拆成这些纯函数，先测试纯函数，再接 QPainter：

```python
def build_segment_points(curve: SplineCurve) -> list[Segment]:
    # 输出相邻 keyframe segment，不处理绘制

def segment_to_screen_path(segment: Segment, viewport: SplineViewport) -> QPainterPath:
    # 把 data-space P0/P1/P2/P3 转换成 screen-space，再 path.cubicTo

def sample_segment_data(segment: Segment, sample_count: int) -> list[QPointF]:
    # 用于 hit-test、fit overshoot、测试；不要只依赖 QPainterPath
```

绘制每个 segment：

```python
path = QPainterPath()
s0 = data_to_screen(P0)
s1 = data_to_screen(P1)
s2 = data_to_screen(P2)
s3 = data_to_screen(P3)
path.moveTo(s0)
path.cubicTo(s1, s2, s3)
painter.drawPath(path)
```

关键点：因为 `data_to_screen()` 是 affine transform，先把控制点转屏幕坐标再 `cubicTo()` 是正确的。不要先用数据坐标创建 QPainterPath 再随手 scale painter，除非非常清楚 painter transform 和 y 轴翻转，否则容易导致曲线倒置、手柄偏移、命中测试不一致。

绘制顺序：

| 顺序 | 内容 | 细节 |
|---:|---|---|
| 1 | 背景 | 填充整个 widget |
| 2 | plot rect | 填充曲线区，比面板背景略深 |
| 3 | 网格 | 先画垂直 frame 网格，再画水平 value 网格 |
| 4 | 轴标签 | frame 标签在顶部，value 标签在左侧 |
| 5 | 非选中曲线 | 细线，alpha 160-200 |
| 6 | 选中曲线 | 2 px，更亮，置于非选中曲线上方 |
| 7 | 控制柄连线 | 只有选中曲线/选中点显示，灰白细线 |
| 8 | 控制柄点 | 小方块，选中为亮色 |
| 9 | 关键帧点 | 小方块或菱形，选中高亮 |
| 10 | 播放头 | 红线最后画，覆盖曲线 |
| 11 | 拖拽框/tooltip | 最后画 |

QPainter 设置：

```python
painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
painter.setClipRect(plot_rect.adjusted(0, 0, 1, 1))
```

不要把文字绘制放在 clip rect 内，否则边缘标签可能被裁掉。绘制曲线时开启 clip，绘制标签时关闭或设置完整 widget clip。

#### P11.6 不同插值类型的绘制规则

| interpolation | 绘制方式 | 注意 |
|---|---|---|
| `linear` | `path.lineTo(K1)` | 不显示贝塞尔手柄，或显示为禁用 |
| `hold` / `step` | 先水平到 `K1.frame`，再垂直到 `K1.value` | 阶梯拐角是直角，不使用 `cubicTo` |
| `bezier` | `path.cubicTo(P1, P2, P3)` | 显示左右控制柄 |
| `ease_in` | 自动生成靠近 K1 的慢入控制点 | 可转成 bezier segment 绘制 |
| `ease_out` | 自动生成靠近 K0 的慢出控制点 | 可转成 bezier segment 绘制 |
| `ease_in_out` | 自动生成两侧平滑控制点 | 可转成 bezier segment 绘制 |

自动 ease 控制点建议：

```python
dx = frame1 - frame0
P1 = QPointF(frame0 + dx * 0.3333, value0)
P2 = QPointF(frame1 - dx * 0.3333, value1)
```

这会产生常见 ease-in-out 的 S 型起始/结束平滑。后续用户拖动控制柄后，把该段 interpolation 改为 `bezier`，并保存真实手柄。

#### P11.7 自动切线和平滑切线

切线模式：

| tangent_mode | 行为 |
|---|---|
| `linear` | 左右手柄落在 key 点上，segment 用直线 |
| `flat` | 手柄水平，value 等于 key value |
| `smooth` | 左右手柄共线，拖一侧会保持另一侧反向共线 |
| `broken` | 左右手柄独立，拖一侧不影响另一侧 |
| `auto` | 根据相邻 key 自动计算平滑切线 |
| `step` | 不显示手柄，使用 hold 绘制 |

Auto tangent 算法建议第一版使用“安全有限差分”，不要一开始写复杂曲线拟合：

```python
def auto_slope(prev_key, key, next_key):
    if prev_key is None or next_key is None:
        return 0.0
    dx = next_key.frame - prev_key.frame
    if dx <= 0:
        return 0.0
    return (next_key.value - prev_key.value) / dx
```

生成左右手柄：

```python
left_dx = (key.frame - prev_key.frame) / 3.0 if prev_key else 0.0
right_dx = (next_key.frame - key.frame) / 3.0 if next_key else 0.0
slope = auto_slope(prev_key, key, next_key)
handle_left = QPointF(key.frame - left_dx, key.value - slope * left_dx)
handle_right = QPointF(key.frame + right_dx, key.value + slope * right_dx)
```

防 overshoot 规则：

| 条件 | 处理 |
|---|---|
| `key.value` 是局部最大或局部最小 | slope = 0，生成 flat tangent |
| 相邻 frame 间距 <= 0 | handle 贴 key 点 |
| handle value 超出相邻两个 key 的 min/max 太多 | 第一版允许 overshoot，但 fit view 必须包含；第二版可加 clamp |

局部极值判断：

```python
if prev_key and next_key:
    if (key.value - prev_key.value) * (next_key.value - key.value) <= 0:
        slope = 0.0
```

#### P11.8 命中测试和拖拽

命中优先级从高到低：

| 优先级 | 对象 | 半径 |
|---:|---|---:|
| 1 | 选中控制柄点 | 9 px |
| 2 | 关键帧点 | 8-10 px |
| 3 | 未选中控制柄点 | 7 px |
| 4 | 曲线本身 | 5 px 到 sampled polyline |
| 5 | 空白区域 | - |

曲线命中不要直接用 `QPainterPath.contains()`，因为线没有面积，容易点不中。必须采样为 polyline 后算点到线段距离：

```python
def distance_point_to_segment(p, a, b):
    ab = b - a
    denom = dot(ab, ab)
    if denom <= 1e-9:
        return length(p - a)
    t = clamp(dot(p - a, ab) / denom, 0.0, 1.0)
    closest = a + ab * t
    return length(p - closest)
```

采样数量：

```python
screen_dx = abs(data_to_screen(K1).x() - data_to_screen(K0).x())
sample_count = clamp(int(screen_dx / 8), 12, 160)
```

拖关键帧点：

| 方向 | 行为 |
|---|---|
| 横向 | 改 frame，默认吸附整数帧；不能穿过相邻 key |
| 纵向 | 改 value，按参数 min/max clamp |
| Shift | 可暂时禁用 frame 吸附 |
| Alt/Option | 可只改 value，不改 frame |

拖控制柄：

| 模式 | 行为 |
|---|---|
| broken | 只改当前 handle |
| smooth | 同时更新另一侧 handle，使两边共线、长度可独立或按比例 |
| flat | 拖动后切到 broken 或 smooth，具体按工具按钮状态 |
| auto | 用户拖动后自动切到 broken/smooth，避免下一次刷新覆盖手动手柄 |

Smooth 反向手柄计算：

```python
drag_vec = dragged_handle - key_point
opposite_dir = normalize(-drag_vec)
old_len = length(opposite_handle - key_point)
opposite_handle = key_point + opposite_dir * old_len
```

如果希望两边长度也一致：

```python
opposite_handle = key_point - drag_vec
```

第一版建议“方向共线、长度保留”，更接近专业曲线工具手感。

#### P11.9 Fit View 和数值范围

Fit selected curves 时必须包含：

| 内容 | 是否包含 |
|---|---|
| keyframe 点 | 必须 |
| 贝塞尔手柄 | 必须 |
| 曲线采样后的 overshoot 点 | 必须 |
| 当前播放头 | 可选，建议包含 |

算法：

```python
points = []
for curve in visible_curves:
    for segment in curve.segments:
        points.extend([P0, P1, P2, P3])
        points.extend(sample_segment_data(segment, sample_count=64))
frame_min = min(p.x for p in points)
frame_max = max(p.x for p in points)
value_min = min(p.y for p in points)
value_max = max(p.y for p in points)
add padding: frame_pad = max(5, (frame_max-frame_min)*0.05)
add padding: value_pad = max(0.1, (value_max-value_min)*0.1)
```

不要只 fit keyframe 点，否则贝塞尔 overshoot 会被裁掉，用户会以为曲线断了。

#### P11.10 Spline 与 Keyframes 面板的关系

| 行为 | 同步规则 |
|---|---|
| Keyframes 面板移动关键帧 frame | Spline 曲线点横向同步移动 |
| Spline 面板移动关键帧 frame/value | Keyframes 面板关键帧位置同步，属性面板当前值刷新 |
| Spline 修改手柄 | KeyframeTrack 保存 `bezier_in/out`，预览重新 evaluate |
| Spline 修改插值类型 | Keyframes 面板关键帧图标/颜色可显示对应 interpolation |
| 属性面板 `◆` 新增/删除关键帧 | Spline 曲线立即刷新 |
| 节点颜色修改 | Spline 左侧色点和曲线颜色同步更新 |

#### P11.11 Spline 测试计划

新增测试：

| 文件 | 覆盖 |
|---|---|
| `tests/composite/test_spline_model.py` | keyframe_tracks → SplineCurve 转换、颜色、visible、fit bounds |
| `tests/composite/test_spline_bezier_math.py` | cubic bezier 采样、端点、控制点、hold/linear/ease 绘制点 |
| `tests/ui/test_spline_view_hit_test.py` | 关键帧点、控制柄、曲线命中测试 |
| `tests/ui/test_spline_view_render.py` | QImage 离屏绘制非空、播放头、关键帧点、曲线颜色 |
| `tests/composite/test_spline_undo.py` | 拖点/拖柄/切线模式 Undo/Redo |

必须测试的贝塞尔数学用例：

| 用例 | 断言 |
|---|---|
| `t=0` | 返回 P0 |
| `t=1` | 返回 P3 |
| P0/P1/P2/P3 共线 | 采样点都在直线上 |
| hold segment | 输出水平线 + 垂直跳变，不调用 cubicTo |
| linear segment | 只用 lineTo，不显示手柄 |
| handle x 被拖出相邻 key 范围 | 被 clamp 回合法范围 |
| 局部极值 auto tangent | slope 为 0，曲线不过度 overshoot |
| fit bounds | 包含 key 点、手柄点、采样 overshoot 点 |

离屏绘制测试建议：

```python
image = QImage(960, 320, QImage.Format.Format_ARGB32)
image.fill(Qt.GlobalColor.transparent)
painter = QPainter(image)
view.render_to_painter(painter, QRectF(0, 0, 960, 320))
painter.end()
assert image_has_non_background_pixels(image)
assert pixel_near_curve_color_exists(image, curve_color)
assert pixel_near_playhead_red_exists(image)
```

不要只测函数返回，不测绘制结果。多数曲线工具 bug 都出在坐标翻转、clip rect、控制点屏幕转换和绘制顺序。

## 9. 面板注册与布局要求

PanelRegistry：

```python
panel_registry.register(PanelDescriptor(
    panel_id="composite.keyframes",
    title_key="panel.keyframes",
    fallback_title="Keyframes",
    feature_id="composite",
    category="Composite",
    page_ids=("Composite",),
    dock_attr="dock_composite_keyframes",
    order=150,
    default_visible=True,
))

# 第三阶段：Spline 面板。只有 P11/P12 开始时再注册。
panel_registry.register(PanelDescriptor(
    panel_id="composite.spline",
    title_key="panel.spline",
    fallback_title="Spline",
    feature_id="composite",
    category="Composite",
    page_ids=("Composite",),
    dock_attr="dock_composite_spline",
    order=160,
    default_visible=False,
))
```

布局：

| 项目 | 要求 |
|---|---|
| 默认位置 | Composite 页面底部 Dock 区 |
| 与 Node Graph 关系 | 不替代 Node Graph；Keyframes 在底部，Node Graph 可在上方或同一底部区域按项目现有布局决定 |
| 与 Properties 关系 | Properties 仍在右侧 Dock；Keyframes 不放进 Properties |
| Window > Panels | 必须能显示/隐藏 `Keyframes` |
| Spline 面板 | 第三阶段注册为单独 `Spline` panel，默认可隐藏，不要阻塞 Keyframes 面板基础功能 |
| 快捷键 | 可后置，建议 `Shift+K` 显示/隐藏 |

## 10. 非多模态 UI 验收清单

实现者不看截图时，可按以下清单自检：

| 编号 | 验收项 |
|---|---|
| UI-1 | 面板左上角标题是 `Keyframes`，标题栏高度约 30 px |
| UI-2 | 标题栏右侧依次有关键帧导航、缩放滑条、fit、zoom、展开折叠、更多菜单 |
| UI-3 | 主区域左侧是 232 px 左右的节点树，右侧是时间轨道 |
| UI-4 | 左侧每行有锁图标列、展开箭头列、名称列 |
| UI-5 | 节点行可展开，参数行缩进更深 |
| UI-6 | 右侧顶部有帧号标尺，能显示负帧和正帧 |
| UI-7 | 右侧有贯穿全高的红色播放头，顶部有红色倒三角帽 |
| UI-8 | 媒体/节点范围条是横向矩形，颜色来自对应节点的 `node_color/header_color`，选中时提亮和加亮边框 |
| UI-9 | 范围条左中右分别显示开始帧、名称、结束帧 |
| UI-10 | 参数行有细曲线和关键帧点，关键帧可拖动 |
| UI-11 | 左右区域垂直滚动完全同步 |
| UI-12 | 底部有水平滚动条，右下角有 `时间` 下拉框 |
| UI-13 | 所有按钮样式使用项目全局暗色主题，未出现浅色系统默认按钮 |
| UI-14 | 打开面板后默认当前播放头在可视区域中间或尽量居中 |
| UI-15 | 选择节点后对应行高亮；移动关键帧后不需要重新选择节点 |
| UI-16 | 修改素材节点颜色后，Keyframes 面板该节点行和范围条颜色无需重开面板即可同步 |

## 11. 功能验收清单

| 编号 | 验收项 |
|---|---|
| F-1 | 新建两个 Transform 节点，只给第一个加关键帧，第二个不会出现关键帧 |
| F-2 | 保存项目、重启、加载后，关键帧仍存在 |
| F-3 | 播放 Composite 时间线时，红色播放头实时移动 |
| F-4 | 拖动播放头时，Composite viewer 实时刷新当前帧 |
| F-5 | 拖动关键帧水平移动后，渲染结果在新帧生效 |
| F-6 | 拖动视频媒体条右边缘后，片段结束帧变化 |
| F-7 | 撤销拖动关键帧，关键帧位置和渲染都恢复 |
| F-8 | 属性面板 `◆` 添加关键帧后，Keyframes 面板立即出现该关键帧 |
| F-9 | Keyframes 面板删除最后一个关键帧后，属性面板 `◆` 状态取消 |
| F-10 | 锁定节点行后，不能拖动其范围条和参数关键帧 |
| F-11 | 只显示动画参数模式下，无关键帧参数行隐藏 |
| F-12 | 1000 个关键帧情况下拖播放头不卡顿 |
| F-13 | 属性面板关闭 Animated 后，节点级轨道被删除，`◆` 取消选中，保存后不再出现该动画 |
| F-14 | X/Y 组合参数共享按钮时，两条轨道 frame 可同步，但 X/Y value 不会互相覆盖 |
| F-15 | Preview 播放、拖动播放头、导出三条路径都使用同一 `frame_number` 坐标系 |
| F-16 | 修改节点颜色、保存、重启后，Node Graph 和 Keyframes 面板范围条颜色都保持一致 |

## 12. 测试计划

| 测试类型 | 文件建议 | 覆盖 |
|---|---|---|
| 单元测试 | `tests/composite/test_composite_keyframes_model.py` | 行模型、frame/x 映射、范围条编辑、过滤 |
| 序列化测试 | `tests/composite/test_composite_keyframes_serialization.py` | keyframe_tracks/time_range 保存加载 |
| 渲染插值测试 | `tests/composite/test_composite_keyframe_evaluation.py` | frame_number 插值、节点范围外 bypass |
| UI 信号测试 | `tests/ui/test_composite_keyframes_panel.py` | 面板打开、选择、滚动同步、按钮状态 |
| 性能测试 | `tests/benchmark/test_composite_keyframes_panel_perf.py` | 大量节点/关键帧 paint 和拖拽 |
| 旧测试迁移 | `tests/project/test_keyframe.py`、`tests/composite/test_node_properties_panel_xy.py` | 从 ParamDef 存储期望迁移到节点级轨道期望 |
| 颜色同步测试 | `tests/composite/test_composite_keyframes_node_color.py` | node_color 保存/加载、PropertiesPanel 修改、KeyframesModel 输出同步 |
| 当前帧测试 | `tests/composite/test_composite_keyframe_frame_number.py` | CompositePage preview snapshot、export snapshot 使用一致 frame_number |
| Spline 数学测试 | `tests/composite/test_spline_bezier_math.py` | cubic bezier、hold、linear、ease、auto tangent、fit bounds |
| Spline 模型测试 | `tests/composite/test_spline_model.py` | keyframe_tracks 转 curve/segment/handle，颜色同步，visible/selected |
| Spline UI 测试 | `tests/ui/test_spline_view_hit_test.py`、`tests/ui/test_spline_view_render.py` | 命中测试、离屏绘制非空、曲线颜色、播放头 |

必须跑的基础命令：

```bash
.venv/bin/python -m pytest tests/composite tests/ui -q
.venv/bin/python -m py_compile src/features/composite/ui/keyframes_panel.py src/features/composite/ui/keyframes_model.py src/features/composite/ui/keyframes_timeline_view.py src/features/composite/ui/keyframes_tree_view.py
git diff --check
```

P10A 关键帧系统闸门必须先跑通：

```bash
.venv/bin/python -m pytest \
  tests/project/test_keyframe.py \
  tests/composite/test_composite_keyframes_serialization.py \
  tests/composite/test_composite_keyframe_evaluation.py \
  tests/composite/test_composite_keyframe_frame_number.py \
  tests/composite/test_composite_keyframes_node_color.py \
  tests/composite/test_node_properties_panel_xy.py \
  -q
git diff --check
```

如果这些测试未全部通过，不要创建 `keyframes_panel.py`、`keyframes_timeline_view.py`、`keyframes_tree_view.py`。可以先创建模型层测试，但不要进入可视化面板开发。

第三阶段 P11/P12 Spline 必跑测试：

```bash
.venv/bin/python -m pytest \
  tests/composite/test_spline_model.py \
  tests/composite/test_spline_bezier_math.py \
  tests/ui/test_spline_view_hit_test.py \
  tests/ui/test_spline_view_render.py \
  tests/composite/test_spline_undo.py \
  -q
.venv/bin/python -m py_compile \
  src/features/composite/ui/spline_model.py \
  src/features/composite/ui/spline_view.py \
  src/features/composite/ui/spline_panel.py \
  src/features/composite/ui/spline_commands.py
git diff --check
```

如果贝塞尔数学测试或离屏绘制测试未通过，不要继续做 Spline 交互。曲线工具必须先保证“画得对”，再保证“拖得动”。

## 13. 实施注意事项

| 注意事项 | 原因 |
|---|---|
| 不要把每个关键帧做成 QWidget | 性能会很差，拖动和滚动会卡 |
| 不要把用户关键帧写进类级 `PARAM_DEFS` | 会污染同类节点的所有实例 |
| 不要让 Keyframes 面板直接调用 OpenGL | 它只改模型和请求 evaluate |
| 拖动中节流 evaluate 到约 16 ms | 保持流畅，避免提交过多渲染任务 |
| 拖动结束再 push undo action | 避免 undo 栈爆炸 |
| 保存格式必须向后兼容 | 老项目没有 `keyframe_tracks/time_range` 时应正常打开 |
| 节点颜色必须随 graph 保存 | 否则 Keyframes 面板颜色同步只能在当前会话生效，重启后会丢 |
| UI 文本走 i18n | 中英文界面都要有 tooltip 和菜单文字 |
| 使用项目全局 QSS | 避免和其它面板风格割裂 |
| 属性面板和 Keyframes 面板不能各存一份关键帧 | 唯一权威数据源必须是节点实例的 `keyframe_tracks` |
| 预览和导出必须使用同一关键帧帧号坐标系 | 否则用户在 UI 看到的动画和导出结果会不一致 |

## 14. 推荐实现顺序

第一阶段只修关键帧系统，不创建 Keyframes 面板：

1. 完成 P0，先写失败测试，明确当前系统坏在哪里。
2. 完成 P1，建立节点级 `keyframe_tracks/time_range/node_color`，并迁移保存格式。
3. 完成 P6 中“属性面板 `◆` → CompositePage → NodeGraph”的连接，保证现有属性面板已经能正确创建、删除、修改关键帧。
4. 完成 P7，关键帧、time_range、node_color 必须可保存、加载、Undo/Redo。
5. 完成 P8，所有 preview/playback/export snapshot 必须传入同一坐标系的 `frame_number`。
6. 完成 P10A，跑通关键帧系统测试。此时即使没有 Keyframes 面板，属性面板小关键帧编辑器也必须已经真实可用。

第二阶段才开始 Keyframes 面板：

7. 完成 P2，做 KeyframesModel，把已经跑通的底层数据转成可绘制行和轨道。
8. 完成 P3，做静态 Dock 和 UI 布局。
9. 完成 P4，做自绘标尺、范围条、曲线、关键帧点。
10. 完成 P5，做拖播放头、拖关键帧、拖范围条。
11. 完成 P9，做视觉 polish、i18n、快捷键、空状态。
12. 完成 P10B，跑全量回归和性能测试。

第三阶段才开始 Spline 曲线图表编辑：

13. 完成 P11 的 SplineModel 和贝塞尔数学测试，先保证曲线采样、fit bounds、hold/linear/bezier/ease 都正确。
14. 完成 SplineView 的只读绘制，先跑离屏绘制测试，确认曲线、点、手柄、播放头都能正确画出来。
15. 完成 P12 的交互：拖关键帧点、拖贝塞尔手柄、切换切线模式、框选、多曲线显示。
16. 完成 Spline Undo/Redo、保存/加载、预览/导出一致性测试。

硬性要求：P10A 之前不要实现 Keyframes 面板 UI；P10B 之前不要实现 Spline 面板；P11 贝塞尔数学和离屏绘制测试未通过之前不要实现 Spline 交互。这个工具的核心风险是数据所有权、保存加载、当前帧求值和属性面板同步。如果这些没先解决，后面 UI 越像 DaVinci，隐藏 bug 越难定位。
