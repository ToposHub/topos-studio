# Topos Color 合成页面专业化差距评估与优化计划

**评估日期**：2026-08-27
**适用范围**：Topos Color 合成页面、节点图、时间线/关键帧、图层栈、2D/3D 视口、合成渲染、缓存、导出及项目持久化
**当前判断**：高级原型 / 生产化早期阶段
**计划状态**：待分阶段执行

## 1. 结论摘要

当前合成系统已经具备一套相当完整的基础骨架：节点图编辑、撤销/重做、图结构持久化、节点级时间范围、基础关键帧与曲线、图层栈模型、轨道遮罩/父子层数据、2D 视口工具、部分 3D 场景渲染、缓存、后台渲染和多种导出格式均已存在。因此，下一阶段不应继续重复堆叠零散节点，而应集中解决“语义正确、工作流闭环、生产稳定性和专业可控性”。

与 Nuke、Fusion、After Effects 等专业合成软件相比，目前最影响实际生产的不是单个滤镜数量，而是以下六类系统性缺口：

1. **时间语义尚未闭环**：time_offset、frame_hold、retime 目前仍是直通占位；图层 in/out 帧字段已经保存，但尚未真正参与图层编译和评估。
2. **图执行缺少生产级预检**：validate 主要检查环路和 Output 输入，尚未系统检查孤立节点、不可达节点、占位节点、缺失素材、输出契约、颜色/格式冲突和图层引用错误。
3. **复杂图组织能力还停留在编辑器层**：Backdrop、便签、自动布局等体验已有，但 GroupNode 仍是占位/直通语义，尚不能作为真正的子图、宏或可复用模块。
4. **2D 合成工具链不完整**：已有基础遮罩、Roto、Keyer、Transform、CornerPin，但缺少专业级跟踪、平面跟踪、稳定、边缘处理、Despill、Light Wrap、运动模糊和系统化 Matte 诊断。
5. **3D 能力有渲染基础但缺生产入口和输出闭环**：3D 相机、多灯、MSAA、深度/ID/描边、四视图等已实现；但 Outliner、材质编辑、Animation/Import Report 面板、AOV 输出、完整骨骼/蒙皮变形、专业导入与重链接流程仍不完整。
6. **生产可靠性不足**：聚焦回归测试通过，但合成测试全量单进程仍存在 Qt/ModernGL 相关段错误，关键帧范围测试存在 pytest 下挂起风险；这会直接阻止长期运行、批量渲染和持续交付。

### 建议的优先顺序

先修复正确性和稳定性，再扩展专业工具：

**P0：稳定性、预检、时间/图层语义、占位节点治理**
→ **P1：节点组/图层工作流、2D 专业工具、缓存/资产/交付、3D 生产闭环**
→ **P2：性能、可观测性、插件化和体验完善**
→ **P3：AI 节点、重型 3D 格式和高级协作能力**

## 2. 评估依据与边界

本评估以当前工作区代码、测试和已有审计文档为准，重点检查了：

- 合成页面与运行时挂载：
  /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py
  /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/node_editor_widget.py
  /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/runtime.py
- 节点图、节点能力和持久化：
  /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_graph.py
  /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_engine.py
  /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_capabilities.py
  /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/graph_migration.py
- 图层、关键帧和曲线：
  /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/layer_stack_controller.py
  /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/layer_stack_compiler.py
  /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/keyframes_panel.py
  /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/spline_panel.py
- 评估、调度、缓存、渲染和导出：
  /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/evaluators/base_evaluator.py
  /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/render_scheduler.py
  /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/cache_manager.py
  /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_export_renderer.py
  /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_render_dialog.py
- 视口和 3D：
  /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_viewport.py
  /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render3d/scene_payload.py
  /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render3d/aov.py
  /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render3d/mesh_picking.py
  /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/scene3d_node.py
  /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/deformer3d_node.py
- 现有合成测试和稳定性记录：
  /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/tests/composite
  /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/docs/r11_composite_segfault_findings_2026-08-21.md

### 当前验证结果

- 针对节点能力、图层栈、时间范围、关键帧/曲线、3D 能力、调度器、评估器、节点预览和关键帧 UI 的聚焦回归：**555 passed**。
- 测试有一条能力注册表一致性警告：topos_color 被标记为 fusible，但未出现在 pass_fusion.FUSIBLE_TYPES 中。
- 合成测试全量单进程运行仍有 pytest-qt teardown 阶段的段错误风险；现有文档记录了进程隔离运行方式，但根因尚未修复。
- tests/composite/test_keyframes_composition_range.py 在 pytest 下存在约 60 秒挂起风险，直接构造对象则可运行，说明事件循环/定时器生命周期仍需专项处理。

本计划表覆盖功能正确性、生产工作流、性能、稳定性和可维护性；不把已存在的基础能力重复列为开发任务，也不把单纯的视觉样式问题作为当前最高优先级。

## 3. 当前能力盘点

| 领域 | 当前状态 | 已有能力 | 与专业软件的主要差距 |
|---|---|---|---|
| 节点图基础 | 基础可用，架构较好 | 节点/端口连接校验、环路检测、撤销/重做、复制粘贴、自动布局、Backdrop、便签、最小地图、视口状态保存 | 缺生产级预检、真正子图/宏、渲染树诊断、节点锁定/书签/模块化复用 |
| 时间线与关键帧 | 部分闭环 | 节点时间范围、关键帧缓存、Bezier 曲线、图层/节点两种观察模式 | 时间节点未真正改写源帧；图层 in/out 未生效；可动画参数类型有限；表达式能力有限 |
| 图层与节点混合 | 模型已有，工作流未完全闭环 | 图层启用、solo、锁定、shy、父子层、轨道遮罩、顺序、透明度和混合模式字段 | 缺完整的图层-节点来源映射、层范围执行、轨道遮罩诊断、嵌套合成编辑体验 |
| 2D 合成 | 基础可用 | Transform、Crop、Mask、Roto、CornerPin、Merge、Blur、颜色和基本 Keyer/Matter 节点 | 缺专业跟踪/稳定、边缘与 Despill、Light Wrap、Matte 诊断、运动模糊和高阶抠像工作流 |
| Viewer/QC | 基础较强 | RGBA/通道查看、曝光/伽马、ODT、像素检查、分屏、辅助线、安全框、2D/3D 操作 | 缺 Waveform、Vectorscope、Parade、Histogram、真正 A/B Compare、渲染层/AOV 浏览和一致的诊断叠加 |
| 3D 场景 | 渲染原型到中等基础 | 相机模式、多灯、MSAA、深度/ID/描边、四视图、模型材质纹理上传、基础动画和选择 | 缺 Outliner、完整导入/动画入口、材质编辑、AOV 输出、骨骼/蒙皮闭环、专业格式和高级合成交互 |
| 色彩与 Alpha | 基础设施存在 | 颜色空间、位深、Alpha premultiplied、ODT/曝光等元数据 | 缺全节点一致性测试、OCIO/项目策略闭环、Premult/Unpremult 诊断、多通道交付一致性 |
| 缓存与性能 | 能运行，但偏内存和预览导向 | LRU 内容寻址缓存、预取、调度器、三缓冲/PBO、导出分块和取消 | 缺用户可控磁盘缓存、代理/分辨率策略、缓存可视化、节点级性能剖析和稳定的视图快速路径 |
| 渲染与交付 | 单任务基本可用 | 图片、序列、视频导出；后台 worker；进度和取消；全局 Deliver 有队列 | 合成页缺持久化渲染队列、优先级、断点续渲、依赖图、预检阻断和多输出管理 |
| 项目与素材 | 持久化基础可用 | 图迁移、相对路径相关基础处理、节点/图层数据保存 | 节点未知扩展字段会丢失；缺资产依赖图、重链接面板、离线报告、可移植打包 |
| 稳定性与 QA | 需要专项治理 | 聚焦测试覆盖面较大 | 全量单进程段错误、pytest 事件循环挂起、真实 GPU/媒体/长时渲染回归仍不足 |

## 4. 问题与优化计划总表

### 4.1 P0：必须先解决的正确性和稳定性问题

P0 的判断标准：如果不解决，会导致结果错误、项目无法可靠打开/导出、批量处理不可信，或阻碍后续功能开发。

| ID | 领域 | 当前问题与证据 | 专业影响 | 优化方向 | 主要文件 | 验收标准 | 状态 |
|---|---|---|---|---|---|---|---|
| P0-01 | 稳定性 | 合成测试全量单进程在 pytest-qt teardown 阶段存在段错误；现有记录将风险指向 Qt/ModernGL 生命周期或堆破坏，根因未定位 | 长时间编辑、切换页面、批量测试和批量渲染不可信 | 用 ASan、MallocStackLogging、最小复现和 GPU 资源生命周期日志定位；统一线程退出、上下文销毁、纹理/FBO/PBO/缓冲释放顺序 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/docs/r11_composite_segfault_findings_2026-08-21.md；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render | 全量合成测试单进程重复运行无段错误；页面反复进入/退出、取消渲染和关闭窗口无资源异常 | 待做 |
| P0-02 | 稳定性 | 关键帧 composition range 测试在 pytest 下可挂起，直接构造对象则可运行 | 自动化测试可能永久占用 worker；用户关闭/切换时也可能遇到隐性定时器阻塞 | 梳理 Qt timer、信号、worker 和事件循环所有权；为测试和生产路径提供显式 stop/dispose；禁止析构期间回调 UI | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/tests/composite/test_keyframes_composition_range.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/keyframes_panel.py | 测试在固定超时内结束；面板关闭后无回调、无悬挂线程、无重复 timer | 待做 |
| P0-03 | 图预检 | GraphPreflight 已统一检查环路、可达性、输出、占位节点、素材、端口契约和图层引用，并接入最终导出前检查 | 预检错误可在开始最终渲染前阻断，警告和修复建议可定位到节点 | 保持诊断码、定位信息和导出模式策略稳定；继续补齐更复杂的 AOV/表达式契约 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color03/src/features/composite/core/graph_preflight.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color03/src/features/composite/render/composite_export_controller.py | 对每类非法状态有稳定诊断码；预检结果可跳转节点；error 状态不能开始最终渲染 | 已完成 |
| P0-04 | 时间语义 | `TimeRemap` 已将 time_offset、frame_hold、retime 统一映射为 source frame，并由评估器、动态源和缓存签名共同使用 | 预览和导出使用同一帧语义，边界、循环和缺帧策略有纯函数测试 | 继续补充真实视频源的跨帧 golden/回归样本 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color03/src/features/composite/core/time_remap.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color03/src/features/composite/evaluators/base_evaluator.py | 时间偏移、保持帧和速度变化在预览/导出/缓存命中上结果一致；首尾帧、负速率和缺帧有测试 | 已完成 |
| P0-05 | 图层时间 | `LayerStackCompiler` 已把层 in/out、enabled/solo 和节点时间门控纳入 live predicate 与评估快照 | 图层在入点前、有效区间、出点后会稳定屏蔽，并有组合测试覆盖 | 继续补真实素材的图层/轨道遮罩 golden 样本 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color03/src/features/composite/core/layer_stack_compiler.py；/Users/testuser/Documents/vscode/topos-color03/src/features/composite/core/time_remap.py | 每层在入点前、有效区间、出点后均有 golden test；solo、父层、轨道遮罩和嵌套合成组合正确 | 已完成 |
| P0-06 | 占位能力 | AI 节点、Compare、GroupNode、FileOutput 等仍标记为 placeholder 或直通；部分节点可以被添加/连接，但用户不一定能在图上看到“不可用于最终渲染” | 占位结果容易被误认为真实结果，严重影响项目可信度和协作 | 在节点能力、UI、预检、预览和导出中统一 Placeholder/Unsupported 状态；明确禁用、降级、警告和替代路径；未实现节点不能静默产出空结果 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_capabilities.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes | 占位节点有明显标识和诊断；最终渲染默认阻断或要求用户明确确认；Compare 不再伪装成 A/B 输出 | 待做 |
| P0-07 | 输出契约 | 当前主要以 display image 为单一结果；3D AOV 虽有数据定义，但尚未形成端口、MRT、Viewer 选择和导出多通道闭环 | 无法稳定支持深度、法线、物体 ID、运动向量等专业合成回传 | 定义输出端口和 RenderPass/AOV 契约；区分 beauty、alpha、depth、normal、ID、motion 等；让缓存、预览、导出共享同一契约 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render3d/aov.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/scene3d_node.py | AOV 能从节点输出到 Viewer 和 EXR/序列；无效/不支持 AOV 在预检阶段可见；缓存 key 包含 pass | 待做 |
| P0-08 | GPU 资源 | 已建立 render-thread deferred release/flush，并覆盖取消、异常、页面切换和窗口关闭的资源归属；长时 GPU soak 与段错误根因仍需专项验证 | 资源释放路径已收敛，但生产稳定性不能仅由单元测试证明 | 完成真实 GPU 下数百次预览/取消/切页和 context loss 压力验证；保留无 GPU 环境的生命周期单测 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color03/src/features/composite/render/composite_renderer.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color03/src/features/composite/render/render_scheduler.py | 连续执行数百次预览/取消/切页后显存和线程数稳定；异常路径也完成释放；无跨线程 OpenGL 调用 | 部分完成 |
| P0-09 | 能力注册表 | 旧的 pass_fusion 列表与集中能力表已完成一致性收敛；聚焦测试不再出现 topos_color 漂移警告 | 调度、ROI、融合、缓存或优化对同一节点做出不一致判断的风险已降低 | 保持能力注册表为融合资格、动态性、占位状态、ROI 和缓存策略的唯一来源；以测试阻止漂移 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color03/src/features/composite/core/node_capabilities.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color03/src/features/composite/utils/pass_fusion.py | 警告清零；每个节点的 capability 在调度器、评估器、导出器和 UI 中一致 | 已完成 |
| P0-10 | 3D 变形 | Model3D/Deformer3D 已传递 skeletons、hierarchy、model_error 等纯数据元数据，并完成 skinned mesh 的姿态/变形链路；缺失模型路径此前会首帧静默退化 | 动画模型和导入失败原因需要可追溯，不能把素材错误伪装成空场景 | 保持完整模型元数据契约、GPU skinning/CPU fallback、骨骼层级、权重、动画采样和缓存失效；缺失路径立即产生稳定诊断码 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color03/src/features/composite/nodes/deformer3d_node.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color03/src/features/composite/nodes/model3d_node.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color03/src/features/composite/render3d/scene_payload.py | 蒙皮模型在时间变化时顶点、法线、包围盒和选取结果一致；缺失/导入失败原因显示在 payload、UI 和预检报告 | 部分完成 |

### 4.2 P1：达到专业生产工作流所需的核心能力

| ID | 领域 | 当前问题与证据 | 专业影响 | 优化方向 | 主要文件 | 验收标准 | 状态 |
|---|---|---|---|---|---|---|---|
| P1-01 | 节点组 | GroupNode 标记为 placeholder；现有 grouping 主要是 UI 容器，不改变渲染语义 | 复杂图无法封装成可复用子图，也无法安全暴露输入/输出 | 实现真正 Group/SubGraph：边界端口、内部图、局部命名空间、接口参数、版本、嵌套撤销/重做、递归预检和实例缓存 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/group_commands.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/group_node.py | 组内节点可独立编辑；组输入/输出可连接；保存、复制、嵌套、升级和错误定位均稳定 | 待做 |
| P1-02 | 嵌套合成 | Precomp 已有边界和部分 child LayerStack 编译，但尚未形成完整的可编辑嵌套图、实例隔离、递归时间与缓存契约 | 大型项目无法分镜/分层组织；预合成修改风险高 | 明确 Precomp 与 Group 的职责；提供进入子合成、实例参数、递归帧范围、循环检测、缓存和依赖图 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/precomp_node.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/layer_stack_compiler.py | 子合成可独立打开、预览和导出；父子时间范围、缓存失效和错误堆栈正确 | 待做 |
| P1-03 | 图层/节点混合 | 图层属性和节点图分别存在，但缺少专业软件常见的层来源、根节点、自动 Merge 链和手动连接之间的清晰映射 | 用户难以判断某一层最终由哪些节点生成，修改可能破坏自动编译结果 | 建立 layer provenance、managed/manual 标识、生成节点只读边界、层到节点双向定位和冲突修复 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/layer_stack_compiler.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/keyframes_panel.py | 从图层可跳到来源节点，从节点可追溯所属层；手动修改自动链有明确警告和修复操作 | 待做 |
| P1-04 | 轨道遮罩/父子层 | 数据模型、控制器和部分 UI 已有 track matte/parent 字段，但完整的图层范围、遮罩来源、反相、循环引用和结果诊断未形成一条闭环 | 专业抠像和动画依赖精确的 Matte 关系；错误时难以排查 | 增加专用关系视图、遮罩源高亮、无效引用预检、循环检测、Alpha/Luma/对象通道说明和结果预览 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/layer_stack_controller.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/layer_stack_compiler.py | 轨道遮罩关系可视化；所有模式、反相、层范围和嵌套组合都有测试及诊断 | 部分完成 |
| P1-05 | 关键帧 | ParamDef 当前主要支持 float、int、color 动画；vec2/vec3、bool、option 和复杂 mask path 参数不完整 | Transform、摄像机、灯光、Bezier 路径和开关动画无法达到专业可控程度 | 扩展统一动画值类型、逐通道编辑、离散值插值、路径顶点关键帧、关键帧复制/粘贴和时间重映射 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/param_def.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/keyframes_panel.py | vec2/vec3、颜色、离散参数和 Mask 顶点在时间线上可编辑；插值规则和序列化有测试 | 部分完成 |
| P1-06 | 表达式 | 当前表达式主要面向数值参数；缺少成熟的时间/层/节点命名空间、依赖图、错误状态、沙箱和可预测缓存策略 | 高级动画控制、联动和程序化合成难以实现；表达式错误可能难以定位 | 定义表达式语言和上下文 API；构建依赖/环路检测；提供错误节点状态、静态检查、超时和禁用策略 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_graph.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite | 表达式错误不崩溃且可定位；循环依赖被预检阻断；相同输入、帧和上下文结果可复现 | 待做 |
| P1-07 | 运动模糊 | 合成 2D/3D 管线尚未形成统一 shutter、sample count、速度来源和质量档位 | 快速运动、摄像机运动和 3D 变形会产生不专业的硬边或闪烁 | 建立统一 motion vector/shutter 语义；2D Transform、Camera、Geometry、Deformer 和导出共用采样策略 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render3d | 2D/3D 运动模糊随质量档位可控；预览与最终渲染采样一致；性能可测 | 待做 |
| P1-08 | Mask/Roto | 当前已有基本 Mask、Roto 和视口句柄，但缺少专业的多形状栈、路径逐点动画、切线/羽化控制、边缘扩展和追踪绑定 | 精细抠像、人物边缘和复杂遮罩制作效率低 | 支持多 Mask 层、合并模式、逐点/逐切线关键帧、可变羽化、扩张/收缩、软边和动画路径追踪 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_viewport.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes | 路径编辑、羽化、合并和逐点动画在时间线与 Viewer 中双向同步；复杂路径性能可接受 | 部分完成 |
| P1-09 | Keying/Matte | 已有 Luma、Chroma、Difference、Keyer、Erode/Dilate 等基础节点，但缺少 Despill、Screen Balance、Edge Detail、Matte 诊断和 Light Wrap | 绿幕/蓝幕、发丝和半透明物体难以达到交付质量 | 建立专业 Keyer 组合、边缘处理、去溢色、Matte/Edge/Final 三视图、统计和预设 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_viewport.py | 典型绿幕样片具备可重复验收；能独立查看 matte、edge、despill 和最终合成 | 待做 |
| P1-10 | Tracking | Composite 页面未形成 2D point tracking、planar tracking、stabilizer 到 Transform/Mask 的闭环 | 手工跟踪耗时，镜头替换、屏幕合成和稳定镜头无法生产化 | 增加跟踪数据模型、特征点/平面求解、遮挡处理、质量曲线、关键帧烘焙和绑定节点 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite | 点跟踪和平面跟踪能驱动 Transform/CornerPin/Mask；可暂停、修正、烘焙和回退 | 待做 |
| P1-11 | Viewer/QC | 已有通道/曝光/ODT/像素检查/分屏，Waveform、Vectorscope、RGB Parade、Histogram 和独立异步 QC readback 已接入；Composite Scopes 已注册为独立 Dock，并直接复用 Color 页同一份专业 ScopePanel（8 种模式、齿轮设置、输入源、波形标尺、色域选项和多语言均统一） | 可在合成页检查曝光、色偏、黑白场，并在同一套 ScopePanel 交互下查看当前 Viewer pass；高分辨率分析仍限流到缩略采样，不阻塞 Viewer | 继续补真正 A/B 版本源和 AOV/渲染层浏览，以及 Composite 可用的独立 Source/Reference 帧 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color03/src/features/color/ui/scope_panel.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color03/src/features/composite/ui/composite_scope_widgets.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color03/src/features/composite/adapters/main_window_adapter.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color03/src/features/composite/render/composite_renderer.py | Composite Scopes 可从 Window > Panels 控制并在旧布局中自动出现；与 Color 页的模式、按钮、选项和多语言完全一致；scopes 与当前 viewer pass 同步；高分辨率下不阻塞交互 | 部分完成 |
| P1-12 | 节点图体验 | 已有搜索、Backdrop、便签、最小地图和自动布局；现已补齐视图书签、前进/后退导航、节点锁定/属性只读、依赖高亮、分支隔离和节点级预检诊断 badge/tooltip | 大型节点图仍缺局部模块视图、渲染树/AOV 依赖浏览、可折叠模块和更细的性能诊断 | 增加局部视图/渲染树、分支 solo 与依赖追踪的更多入口、性能标签、错误覆盖层和可折叠模块 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color03/src/features/composite/ui/node_editor_widget.py；/Users/testuser/Documents/vscode/topos-color03/src/features/composite/ui/node_item.py | 已可在大型图中保存视图、回溯导航、隔离依赖分支、避免误移动/删改/连线，并将预检问题定位到节点 | 部分完成 |
| P1-13 | 缓存 | 当前 CacheManager 仍以 GPU 内存 LRU 为主，已有预取、内容签名和命中统计；资产使用索引已提供依赖分析基础，但磁盘缓存/代理/缓存图仍未完成 | 4K/8K 长片重启后仍会重复计算，用户也无法按项目控制缓存空间 | 增加按项目/合成/节点/帧的磁盘缓存，代理分辨率和质量档位，缓存命中率/占用 UI，过期和手动清理 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color03/src/features/composite/render/cache_manager.py；/Users/testuser/Documents/vscode/topos-color03/src/features/edit/core/asset_usage_index.py | 重启后可复用合法缓存；图或参数变更只失效受影响分支；缓存空间可控且不污染项目文件 | 部分完成 |
| P1-14 | 资产管理 | 有相对路径基础处理和纹理加载，但缺少完整依赖图、离线素材报告、批量重链接、素材替换和可移植打包 | 项目换机器、归档和交接时容易出现断链或隐性依赖 | 建立 AssetRegistry/DependencyGraph；保存相对项目路径；提供重链接、搜索目录、复制依赖和离线报告 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render3d/material_texture_loader.py | 可列出所有素材、用途、缺失状态和路径；一键重链接并保持项目可移植 | 待做 |
| P1-15 | 持久化兼容 | Graph 级 schema migration 已有，但 Node.to_dict/from_dict 只处理已知字段，未知节点扩展字段可能被丢弃 | 插件升级、第三方节点和未来版本往返保存存在数据损失 | 增加 unknown_fields/raw_extension 保留；节点 schema 与 registry 版本化；迁移保留未知字段并给出兼容警告 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_engine.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/graph_migration.py | 新旧版本往返保存不丢未知字段；无法执行的节点仍可安全显示和重新连接 | 待做 |
| P1-16 | 合成渲染队列 | 合成页有单次 Render Dialog，全局 Deliver 有批量队列，但合成页缺少持久化任务、优先级、依赖、断点和批量预检闭环 | 专业项目通常需要同时管理多个版本、镜头和交付格式 | 增加 Composite Render Queue 或与 Deliver 统一任务模型；记录配置快照、日志、失败原因、取消/重试/断点状态 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_render_dialog.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/products/topos_deliver.py | 关闭应用后任务可恢复；任务使用不可变配置快照；失败可重试且不污染已完成帧 | 待做 |
| P1-17 | 导出交付 | 已支持图片、序列和多种视频编码，但缺少多通道 EXR/AOV、Alpha/Premult 策略提示、元数据、确定性逐帧校验和断点恢复 | 导出结果可能与 Viewer 或下游软件的颜色/Alpha解释不一致 | 建立交付模板、格式能力矩阵、通道映射、色彩/Alpha 报告、帧校验、失败重试和 checkpoint | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_export_renderer.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_render_dialog.py | 每次交付生成配置和校验摘要；中断后可从安全边界继续；目标格式能力不足时预检阻断 | 待做 |
| P1-18 | 3D Outliner | 3D 有对象 ID、选择和场景 payload，但运行时未挂载完整 Outliner/层级面板；ModelPayload 的 hierarchy 使用不完整 | 复杂场景无法按层级、可见性、锁定、solo、标签和实例快速管理 | 增加场景树、对象搜索、层级重排、可见/渲染/选择开关、对象属性和 Viewer 双向选取 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/runtime.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/model3d_node.py | Outliner 与 ID pass、Viewer 选取、场景 payload 同步；层级和可见性影响渲染 | 待做 |
| P1-19 | 3D 导入入口 | ImportReportPanel 和 AnimationPanel 文件存在且有测试，但当前 runtime 未将它们挂载到合成工作区 | 用户看不到导入诊断、动画列表和时间采样控制，已有能力无法使用 | 将面板接入 runtime/dock；导入错误、单位、材质、骨架、动画剪辑和兼容性报告统一呈现 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/runtime.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/import_report_panel.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/animation_panel.py | 从导入到面板可见、可选动画、可定位错误；关闭/重开页面状态保持 | 待做 |
| P1-20 | 3D AOV | aov.py 已定义 depth/normal/position/UV/object/material ID/motion 等类型，但 Scene3D 尚未暴露 AOV 端口、MRT 和 Viewer 选择 | 3D 不能充分服务专业合成、抠像、景深、重光照和 ID 选择 | 完成 AOV buffer 生命周期、端口/命名、Viewer AOV 浏览、EXR 多层导出和缓存隔离 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render3d/aov.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/scene3d_node.py | 至少 depth/normal/position/object ID/motion 可预览、连接、缓存和导出；缺失能力有诊断 | 待做 |
| P1-21 | 3D 合成 | shadow catcher、holdout、depth merge、cryptomatte 等生产常用能力尚未形成完整渲染/节点/导出链 | 真实场景与实拍素材融合、遮挡关系和对象级选取受限 | 先实现 depth/holdout/shadow catcher 垂直切片，再扩展 cryptomatte 和多层 EXR | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render3d；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes | 实拍背景、CG 阴影、前后景遮挡和对象级 matte 有可复现样片及回归图 | 待做 |
| P1-22 | 3D 选择 | Point 和 Edge 工具目前禁用；mesh_picking 明确为 O(triangle_count)，BVH 尚未实现 | 大模型选取卡顿，组件级建模/遮罩/编辑能力不足 | 实现 BVH/GPU picking、Point/Edge/Polygon 一致选取、遮罩/变换联动和选择缓存 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render3d/mesh_picking.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_viewport.py | 百万三角形模型选择响应可接受；三种组件模式结果稳定；选取与 ID pass 一致 | 待做 |
| P1-23 | 3D 材质 | MaterialTextureLoader 已能上传 base color/normal/ORM/AO/emissive 并缓存，但缺少材质编辑器、材质槽映射和 per-material override UI | 用户能加载材质但难以在合成中进行可控修改或替换 | 增加材质/纹理面板、PBR 参数、纹理槽、颜色空间、UV/采样、替换和实例级覆盖 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render3d/material_texture_loader.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite | 每个材质可独立查看/修改/还原；纹理缺失、颜色空间和路径问题有报告 | 部分完成 |
| P1-24 | 3D 动画/变形 | Deformer 目前以 CPU flat mesh 处理为主，skinned deformation 未完成；骨骼/层级/动画元数据传递也不完整 | 角色、机械和复杂模型动画无法达到生产质量和实时性 | 建立 skeleton/hierarchy/clip 的统一数据结构；实现 GPU skinning、morph target、动画采样缓存和 motion vector | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/deformer3d_node.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render3d | 多骨骼、多动画剪辑、morph 和逐帧缓存结果稳定；GPU/CPU 结果有误差阈值测试 | 待做 |
| P1-25 | 3D 格式 | render3d/format_capabilities.py 标记了部分能力，但文件注明该能力仍处于 dormant/未接生产入口；USD/Alembic 等专业格式后端也未形成 | 能力矩阵与实际入口不一致，用户容易误判支持范围 | 建立真实格式能力矩阵、导入版本/单位/坐标轴契约、后台导入、错误报告和重链接；按阶段接 glTF/FBX/USD/Alembic | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render3d/format_capabilities.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite | UI 显示的支持状态与实际测试矩阵一致；每种格式有样本、失败类型和限制说明 | 部分完成 |
| P1-26 | 3D 相机/灯光 | 已有相机模式和多灯基础，但缺少专业 filmback、焦距/传感器、单位/坐标轴、相机匹配、灯光阴影/衰减/IES 等完整控制 | 实拍匹配、精确灯光复现和摄影机工作流受限 | 增加 camera match、filmback、clipping、单位转换、灯光物理参数、阴影质量和可视化 gizmo | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/camera3d_node.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/light3d_node.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_viewport.py | 相机/灯光参数在导入、Viewer、渲染和导出中一致；单位和坐标轴转换有样例测试 | 部分完成 |
| P1-27 | 色彩/Alpha | TextureResult 已记录颜色空间、位深和 premultiplied，但全节点、3D AOV、缓存、Viewer 和导出的策略仍需一致性验证 | 预览正确而导出错误是后期制作中的高风险问题 | 统一项目色彩管理、OCIO/内建策略、premult 规则、线性/显示空间边界、AOV 位深和交付 metadata | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite | 典型 RGBA、HDR、半透明、AOV、EXR 和视频导出有 golden tests；Viewer 与导出数值一致 | 待做 |
| P1-28 | 动画面板 | 关键帧和曲线基础面板存在，但运行时能力、层/节点筛选、批量编辑、关键帧操作和错误提示仍不如专业时间线 | 动画制作效率低；用户需要频繁在属性面板和图层之间切换 | 统一 Timeline/Dope Sheet/Spline 数据模型；支持范围、标记、吸附、批量偏移、剪辑和可见性筛选 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/keyframes_panel.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/spline_panel.py | 节点/图层/参数可筛选；关键帧移动、缩放、复制、粘贴和曲线编辑可撤销且不丢数据 | 部分完成 |

#### 4.2.1 状态校正（2026-08-28）

上方总表保留了最初审计的历史描述；以下覆盖表以当前 `topos-color03` 工作区和已提交测试为准，后续拆任务时优先参考本节：

| ID | 当前状态 | 当前证据与剩余范围 |
|---|---|---|
| P0-01 | 部分完成 | 当前 `.venv` 下完整 `tests/composite` 可重复通过，上一轮段错误未在本地回归中重现；但仍缺真实 GPU 下的反复进出页面、取消、关闭和诊断工具验证，不能视为根因已完全关闭。 |
| P0-02 | 当前范围已完成 | Keyframes 已具备显式 playback timer 停止、`dispose()` 幂等和 `closeEvent` 清理，composition range 测试在完整回归中可结束；仍需在真实窗口关闭/切页压力下补一次验收。 |
| P0-06 | 部分完成 | 能力表、UnknownPlaceholderNode、预检和导出阻断/确认已接通；Compare、GroupNode、FileOutput 已脱离 placeholder 治理。AI 节点仍待真实 backend，见 P3-01。 |
| P0-07 | 部分完成 | `TextureResult`、Scene3D、Viewer、FileOutput 已闭环 depth/normal/position/UV/object ID/material ID/motion vector 子集；Scene3D 现已提供对应可连接 image 输出端口，GraphPreflight 也会检查连接的 AOV 端口是否启用；完整 MRT、多层 EXR 和更多 AOV 仍待完成。 |
| P0-08 | 部分完成 | render-thread deferred release/flush 已覆盖取消、异常、页面切换和窗口关闭，并有生命周期单测；真实 GPU 下数百次预览/取消/切页、context loss 和显存稳定性仍待验证。 |
| P1-06 | 部分完成 | 表达式解析、引用检查、依赖图和循环预检已完成；运行时上下文、沙箱、超时和缓存策略仍待完善。 |
| P1-07 | 部分完成 | shutter、采样、速度来源和 2D/3D motion vector 基础已接入；真实样片 golden、质量基准和全导出一致性仍待验证。 |
| P1-09 | 部分完成 | Keyer 的 screen balance/despill/matte output、Light Wrap、Matte Cleanup 和 Alpha/Matte QC 已接入；Edge Detail、预设和真实绿幕验收仍待完成。 |
| P1-14 | 部分完成 | 已新增 `AssetDependencyReport`、`AssetRelinkService` 和 `AssetCollectService`，覆盖媒体、模型、LUT、HDRI、IES、材质覆写贴图的缺失/解析/候选重链接、内容校验、依赖收集与可撤销安全写回，并接入 GraphPreflight；Composite 已挂载资产依赖面板，支持刷新、选择替换、收集到目录，以及将资产搜索目录持久化到 `viewer_state`；完整项目归档和外部媒体池替换联动仍待完成。 |
| P1-15 | 已完成 | Node 未知顶层字段/参数字段可往返保存，UnknownPlaceholderNode 可安全显示和连接，已有 round-trip 测试。 |
| P1-18 | 部分完成 | Outliner 已挂载并能同步层级/隐藏状态与 payload；现已补充可撤销、可持久化的 solo/锁定状态，并让 solo 与 beauty/AOV/ID picking 共用有效可见集合；层级拖拽编辑和更完整双向选取仍待完成。 |
| P1-19 | 部分完成 | ImportReportPanel 已挂载到合成工作区；Animation 的剪辑/时间采样职责已并入 Keyframes 的 AE 式 Layer 视图，旧 AnimationPanel 文件与 dock 入口已删除；导入进度/错误/格式探测可见，且 Composite 保存前会写回 NodeEditor 的选中节点与视口状态；dock 布局跨页面持久化和更细错误定位仍待完善。 |
| P1-20 | 部分完成 | AOV 后端、Viewer 选择、已实现 pass 导出和导出前能力/来源/开关预检已可用；depth/normal/position/UV/object ID/material ID/motion vector 现可按需生成、选择并从 Scene3D 端口连接；`CompositeExportRenderer`/`CompositeExportProvider` 现支持一次图求值批量读取多个 beauty/AOV，避免每个通道重复求值；更多枚举、真正 MRT 和文件层多层 EXR 写入仍待完成。 |
| P1-21 | 部分完成 | depth merge、holdout、shadow catcher 已有垂直切片；Scene3D 现会附带基于 MurmurHash3-32 的 manifest-only object matte 元数据并明确 object-ID 来源；真正 Cryptomatte GPU 多层写入、多层交付和真实样片仍待完成。 |
| P1-22 | 部分完成 | `TriangleBVH`、组件级 pick cache 和 Point/Edge/Polygon 查询基础已实现；组件描述现在携带与 GPU object-ID pass 对齐的 `pick_id`，多实例使用独立 mesh/cache 键，并新增 `scripts/benchmark_composite_picking.py`。默认约百万三角形基准（999,698 faces、128 rays）在本机为 build 6.69s、query p50 0.642ms、p95 1.026ms；真实 GPU pixel smoke、完整交互压力和更大场景仍待验证。 |
| P1-23 | 部分完成 | Model3D 已输出材质摘要并通过 `MaterialPanel` 写回非破坏 scalar override；本批补齐 base color/normal/ORM/AO/emissive 五个当前后端槽位的路径替换、颜色空间选择、文件选择和还原，并沿原有参数/渲染桥接生效；UV 变换、更多 PBR 字段、实例级覆盖和真实纹理样片仍待完成。 |
| P1-24 | 部分完成 | skeleton/hierarchy/animation cache、GPU skinning、morph 和 motion metadata 已接入；beauty/depth/AOV/ID/shadow/motion pass 现通过同一 joint-matrix 上传契约，并对骨骼截断、坏索引和非归一化权重输出稳定 diagnostics；Keyframes 的 3D Layer 可承载 Model3D 动画参数，合成当前帧统一驱动预览/播放/拖动采样；复杂多剪辑 golden、真实 GPU/CPU 误差门槛和长时动画仍待验证。 |
| P1-25 | 部分完成 | `format_capabilities.py` 已形成 glTF/FBX/OBJ/STL/PLY/DAE/3DS/BLEND/USD/Alembic 能力矩阵，`probe_import` 与后台 ModelImportService 会按实际后端/文件状态返回稳定失败码；本批将 probe 摘要接入 Model3D diagnostics 和 ImportReportPanel，planned/后端不可用/ready 状态可见；针对 pyassimp wrapper 不兼容的 FBX，assimp CLI GLB fallback 现可恢复动画/蒙皮 accessor；真实格式样本矩阵、单位/坐标轴差异验证、USD/Alembic 后端和重链接闭环仍待完成。 |
| P1-28 | 部分完成 | Keyframes 现默认使用 AE 式 Layer 视图，3D 模型可从 Media Pool 拖入成为单一 3D 图层，Model3D 动画参数会展开在图层下；Space 播放和播放头拖动共用 Composite 时间轴，Spline 暂保持独立；批量编辑、剪辑/标记/吸附和更完整错误提示仍待完成。 |
| P2-03 | 部分完成 | 模型导入已有后台服务、取消和稳定 payload 交接；已补齐 queued/probe/parse/handoff/ready 等阶段化进度，并接入 Scene3D diagnostics 与 ImportReportPanel；本批增加基于源文件大小的 CPU/GPU 资源预估和显式预算超限阻断，加载期间 UI 可见预估结果；分块 GPU 上传、部分结果预览和大模型 soak 仍待完成。 |
| P2-06 | 部分完成 | 新增 JSON-safe `RuntimeDiagnostic`/`DiagnosticBus`，并将模型导入错误、格式 probe、蒙皮诊断和资源预算统一归一化为 `diagnostic_records`；ImportReportPanel 展示错误码、来源语义和修复建议；预检、Viewer、Render Queue 的统一总线接入仍待完成。 |
| P2-07 | 部分完成 | `scripts/run_composite_tests.sh` 现提供进程隔离的 `--fast`、`--smoke`、`--gpu`、`--media`、`--full`、`--soak` 分层入口；pytest 自动标记真实 ModernGL、真实视频和 smoke 文件。本机已验证 smoke 75 passed、GPU 181 passed、真实视频 17 passed、双轮短 soak 4 个文件通过；真实窗口压力、长时 GPU soak、golden image 和 CI 矩阵仍待补齐。 |
| P2-04 | 部分完成 | 新增 `CompositeQualityProfile` 的 draft/preview/final 语义：请求可由 render mode/proxy scale 自动解析，质量标签进入 `CompositeEvalContext`、Scene3D shading 选择、渲染跳过签名和 CacheManager key；最终/导出请求强制 final，半分辨率预览归入 draft；项目级质量面板、代理素材、节点能力协商和真实画质/性能基准仍待完成。 |
| P2-05 | 部分完成 | Output 节点新增可持久化的 `output_name`；RenderRequest 支持按名称或 node id 选择终端目标，CompositePage 提供目标列表/菜单并保存选择；本批新增 JSON-safe 的直接/传递输出依赖图、重名预检诊断，并让已解析目标只求值其依赖锥、避免无关分支进入本次缓存。未命名旧项目继续使用 `display`；独立输出缓存的更细粒度复用、批量导出和文件层多层 EXR 接入仍待完成。 |
| P2-01 | 部分完成 | `RENDER_3D_VIEW` 已从骨架回退实现为受图代际/颜色输入/预览目标门控的 view-only 路径：默认虚拟相机导航会复用渲染线程中的 Scene3D payload、几何、材质、FBO 和 AOV 资源，并在缓存不兼容时回退完整求值；真实 GPU 4K/复杂场景帧率基准、外接 Camera3D 快速路径和更细的 draw-only 抽离仍待完成。 |
| P2-02 | 部分完成 | PerfPanel 现可从 PerfMonitor 保留有界 completed-frame trace，并导出包含原始帧记录、热点节点、缓存计数、丢帧、显存/纹理峰值和 3D 汇总的 JSON 报告；GPU timer query、ROI/tile 细分、缓存失效原因和跨帧对比 UI 仍待完成。 |

### 4.3 P2：性能、可观测性和长期维护

| ID | 领域 | 当前问题与证据 | 专业影响 | 优化方向 | 主要文件 | 验收标准 | 状态 |
|---|---|---|---|---|---|---|---|
| P2-01 | 3D 视图性能 | CompositeRenderer._handle_render_3d_view 当前仍会降级到完整 _handle_evaluate_graph，视图只改变相机时未实现真正 fast path | 只旋转视图也可能触发完整合成，交互帧率低 | 分离 camera/view transform 与 graph evaluation；复用场景 payload、几何和材质，只更新视图矩阵与必要 pass | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render/composite_renderer.py | 仅旋转相机不重新执行无关节点；4K/复杂场景交互帧率和 GPU 时间有基准提升 | 待做 |
| P2-02 | 性能剖析 | 有 PerfPanel 和调度基础，但缺少稳定的每节点耗时、GPU/CPU、缓存命中、显存和 ROI/tile 统计 | 难以定位 4K/8K 慢点，也难证明优化收益 | 增加 frame trace、node span、GPU timer query、内存峰值、缓存命中率和导出报告；支持按帧对比 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/perf_panel.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render | 同一 comp 可导出性能报告；热点节点、缓存失效原因和显存峰值可追溯 | 部分完成 |
| P2-03 | 3D 加载 | 模型、纹理和部分导入流程仍可能与交互线程耦合；大模型、纹理和动画后台化、取消和进度需要统一 | 打开大文件会冻结 UI，取消后可能残留资源 | 采用后台解析、分阶段上传、可取消任务、占位预览和资源预算；导入结果通过不可变 payload 交接 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render3d；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/runtime.py | 大模型加载期间 UI 可操作；取消/失败不泄漏；部分结果能安全预览 | 待做 |
| P2-04 | 预览质量 | 已有 preview quality 等基础，但缺少项目级 preview/final 策略、代理、降采样和节点能力协商 | 预览速度、最终质量和缓存复用难以平衡 | 定义 draft/preview/final 配置、节点质量档位、代理素材和最终锁定策略；把质量加入缓存 key | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/render；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_page.py | 每个质量档位的行为可预期；从预览切最终不使用低质量缓存；质量改变可解释地失效缓存 | 待做 |
| P2-05 | 图输出 | 单一 display 输出限制了多分支交付、AOV、代理输出和多画布 | 专业项目需要一个图同时服务多个 viewer、layer、pass 和交付目标 | 引入命名输出、输出组、Render Target/Pass Browser 和输出依赖图；保持默认 display 兼容 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/nodes/output_node.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_graph.py | 多输出可预览、单独缓存、单独导出；未连接/冲突输出有预检信息 | 待做 |
| P2-06 | 错误体验 | 部分模型/纹理/IBL/导入错误可能保留在内部状态或回退路径，未统一成为用户可见的结构化诊断 | 用户看到黑帧、默认材质或直通结果时无法判断原因 | 统一 DiagnosticBus；在节点、Viewer、Import Report、预检和 Render Queue 展示错误、警告、来源和修复建议 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/import_report_panel.py | 失败不静默；每个错误可定位到节点/素材/帧；日志与 UI 使用相同诊断码 | 待做 |
| P2-07 | 测试策略 | 当前聚焦测试覆盖广，但仍以纯 Python、模拟对象和组件测试为主；真实 GPU、媒体、长时渲染和像素回归不足 | 逻辑测试通过不代表真实帧、颜色和资源生命周期可靠 | 建立分层测试：纯逻辑、Qt 组件、单 GPU smoke、媒体样本、golden image、长时 soak、取消/关闭压力 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/tests/composite；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/scripts/run_composite_tests.sh | CI 和本地均有明确 fast/full/GPU 分层；关键样片和资源生命周期有回归门槛 | 待做 |
| P2-08 | 插件/宏 | 节点能力和 schema 已有基础，但缺少面向用户的宏、预设、节点包版本、依赖声明和兼容检查闭环 | 专业团队无法建立可复用工具集，升级会破坏历史项目 | 定义 node package/manifest、参数 schema、版本迁移、依赖、能力声明、预设和沙箱 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/node_capabilities.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/core/graph_migration.py | 第三方节点缺失时项目仍可查看；升级/降级有兼容报告；预设可导入导出 | 待做 |
| P2-09 | Viewer 导航 | 2D/3D 已有缩放、平移、工具、四视图和基础 gizmo，但缺少更完整的 pivot/axis/orientation、吸附、量化、局部/世界坐标切换 | 精确布局、摄影机匹配和重复操作效率低 | 完善变换坐标系、pivot 编辑、网格/吸附/量化、方向指示器、相机书签和导航历史 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui/composite_viewport.py | 常用导航和精确变换均可键盘/鼠标完成；状态在重开项目后保留 | 部分完成 |
| P2-10 | 快捷键/可访问性 | Composite module 注册的命令主要覆盖加节点、旁路、渲染、删除、复制、分组；复杂工作流的命令发现、快捷键一致性和可访问性仍不足 | 高级用户操作速度低，键盘工作流和无障碍使用受限 | 统一 command registry、快捷键提示/自定义、焦点管理、可访问名称和高对比度状态 | /Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/module.py；/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02/src/features/composite/ui | 所有高频操作可搜索、可重映射；键盘完成节点图/时间线核心流程 | 待做 |

### 4.4 P3：扩展能力和长期竞争力

| ID | 领域 | 当前问题与证据 | 优化方向 | 验收标准 | 状态 |
|---|---|---|---|---|---|
| P3-01 | AI 节点 | AIGenerate、AIInpaint、AIExpand、AIRemove、AIDenoise 目前为 PlaceholderNode | 在明确后端、模型、缓存、隐私和离线失败策略后，逐个实现可插拔 AI backend；不再让未配置节点静默参与最终渲染 | 节点能力、模型版本、耗时、缓存和失败状态可追溯；无后端时预检清晰阻断 | 待做 |
| P3-02 | 专业 3D 格式 | USD/Alembic、复杂材质、morph、先进阴影和高质量 motion vector 尚未形成统一生产路径 | 以格式能力矩阵和样片为基础，按 USD/Alembic、动画、材质、AOV、阴影分阶段交付 | 每种格式有明确支持范围、样本、性能和错误报告，不以“能打开”代替生产支持 | 待做 |
| P3-03 | 脚本与自动化 | 尚未形成面向合成的稳定 Python/CLI 批处理、参数遍历和项目检查 API | 为批量镜头、版本化、农场渲染和自动 QC 提供受控 API；API 与 UI 使用同一 command/service 层 | 可无 UI 打开项目、预检、渲染、导出和读取诊断；API 版本化且不绕过模型约束 | 待做 |
| P3-04 | 协作与审计 | 当前计划未覆盖多用户锁、变更审计、版本比较和合成图差异审查 | 在项目持久化、命令和诊断稳定后，再增加版本快照、图结构 diff、渲染对比和资产审计 | 能比较两个 comp 的节点、参数、连接、素材和渲染结果变化 | 待做 |

## 5. 分阶段实施路线

### 阶段 A：稳定性和正确性底座

目标是让“打开、编辑、预览、取消、关闭、保存、重开、导出”全部可重复。

- 完成 P0-01、P0-02、P0-08：解决段错误、挂起和 GPU 生命周期。
- 完成 P0-03、P0-06：上线 Graph Preflight 和占位/不支持节点治理。
- 完成 P0-04、P0-05：完成 time_offset/frame_hold/retime 和 layer in/out 的真实执行。
- 完成 P0-09、P0-10：收敛能力注册表，修正 3D payload/变形元数据契约。
- 建立 fast、full、GPU smoke、golden image、soak 五类测试入口。

阶段出口：预检能在最终渲染前阻断关键错误；时间/图层边界结果正确；单进程合成测试稳定；取消和关闭路径不泄漏。

### 阶段 B：专业图组织和动画工作流

- 完成 P1-01、P1-02、P1-03：真正 Group/SubGraph、Precomp 和图层-节点来源追踪。
- 完成 P1-04、P1-05、P1-06、P1-28：轨道遮罩、父子层、复杂参数动画、表达式和统一时间线。
- 完成 P1-12：书签、只读、分支隔离、诊断和大型图导航。
- 完成 P1-15：未知字段保留与节点 schema 兼容。

阶段出口：大型 comp 可拆分、复用、嵌套和安全交接；修改图层或子图时可追溯影响范围，且撤销/重做/保存/重开一致。

### 阶段 C：2D 专业合成和 Viewer/QC

- 完成 P1-07、P1-08、P1-09、P1-10：运动模糊、Roto/Mask、Keying/Matte、跟踪/稳定。
- 完成 P1-11：Scopes、A/B Compare、Matte/Alpha 诊断。
- 完成 P1-27：统一色彩与 Alpha 规则及 golden image。

阶段出口：常见绿幕、屏幕替换、稳定、二维跟踪、复杂遮罩和颜色检查可在合成页独立完成，不需要绕到其他产品页面。

### 阶段 D：资产、缓存和交付生产化

- 完成 P1-13、P1-14：磁盘缓存、代理、依赖图、重链接和可移植项目。
- 完成 P1-16、P1-17：持久化渲染队列、断点、重试、交付模板和多通道输出。
- 完成 P2-02、P2-04、P2-06：性能剖析、质量档位和结构化错误中心。

阶段出口：项目可跨机器交接；长片和高分辨率项目可恢复渲染；每次交付均有可审计配置、诊断和校验结果。

### 阶段 E：3D 生产闭环

- 先完成 P1-18、P1-19、P1-20：Outliner、导入报告/Keyframes 动画图层、AOV。
- 再完成 P1-21、P1-22、P1-23、P1-24：Holdout/Shadow Catcher、选择/BVH、材质编辑、GPU Skinning。
- 最后完成 P1-25、P1-26 和 P3-02：真实格式矩阵、摄影机/灯光专业控制和重型格式。
- 同步完成 P2-01、P2-03：3D 视图 fast path 和异步资产加载。

阶段出口：3D 场景可以完成导入、诊断、层级管理、动画、材质、AOV、合成、预览和导出，而不仅是“能渲染一个 beauty pass”。

### 阶段 F：扩展生态

- 完成 P2-08、P3-01、P3-03、P3-04。
- 在核心 schema、命令、诊断和渲染契约稳定后，再开放插件、AI、脚本和协作能力。

## 6. 建议的技术验收门槛

每个后续功能都应同时满足功能、数据、渲染和生命周期四类验收，而不是只验证按钮是否出现。

| 门槛 | 要求 |
|---|---|
| 图正确性 | 环路、非法端口、占位节点、缺素材、无效层引用、输出冲突和表达式循环可预检；错误能定位到节点/端口/帧 |
| 时间正确性 | 预览、缓存、导出、暂停、取消和重开项目使用相同帧语义；节点 time_range 与 layer in/out 边界有 golden tests |
| 颜色/Alpha | 线性空间、显示变换、premultiplied Alpha、HDR、AOV、EXR 和视频交付有数值或像素回归 |
| 可恢复性 | 取消、异常、窗口关闭、上下文丢失、素材离线和导出失败均释放资源并保留可重试状态 |
| 性能 | 以 4K 合成、长片、复杂节点图和中大型 3D 场景建立基准；报告 CPU、GPU、显存、缓存命中和帧耗时 |
| 持久化 | 新旧 schema 往返保存不丢未知字段；相对路径、缺素材、插件缺失和版本不兼容有明确报告 |
| 测试 | 聚焦测试保持通过；全量单进程不段错误；关键 UI 测试不挂起；真实 GPU/媒体路径有 smoke 和 soak |
| 生产交付 | 渲染队列保存配置快照；断点/重试不破坏已完成帧；输出文件携带颜色、Alpha、AOV 和帧范围信息 |

## 7. 已有能力：不要重复列为“从零开发”

以下能力已经存在或已有较可靠基础，后续应重点做闭环、接入和专业化，而不是重新设计一套平行系统：

- 节点图连接时的数据类型、端口存在性、输入容量和环路校验。
- 节点图撤销/重做、复制粘贴、重复、自动布局、Backdrop、便签、最小地图和视口状态。
- 节点级 time_range、调度器 dirty propagation、关键帧缓存、Bezier 曲线基础。
- 图层 enabled、solo、locked、shy、透明度、混合模式、父子层和轨道遮罩的数据模型。
- 基础 2D Transform、Crop、Mask、Roto、CornerPin、Merge、Blur、颜色和 Keyer 节点。
- Viewer 的通道查看、曝光/伽马、ODT、像素检查、分屏、辅助线和安全区域。
- 3D 的相机模式、多灯、MSAA、深度/对象 ID/描边、四视图、基础模型材质纹理上传和部分动画。
- 合成渲染线程、取消/合并请求、内存缓存、PBO/三缓冲、分块导出、图片/序列/视频导出和后台进度。
- Graph schema migration、项目状态保存和部分相对路径处理。

## 8. 最终产品定位建议

短期内，产品不应把自己定位为已经全面替代 Nuke/Fusion/After Effects 的成熟合成平台。更准确的定位是：

**具备专业节点合成架构和 GPU 渲染基础、正在补齐时间语义、2D 生产工具链、资产/交付闭环及 3D 生产入口的合成工作站。**

如果只选择一条最重要的主线，应优先把以下垂直切片做完整：

**素材输入 → 图层入/出点 → 节点处理 → Mask/Keyer → Viewer scopes → 预检 → 缓存 → 可取消队列 → 带 Alpha/颜色元数据的确定性导出。**

这条链一旦稳定，后续 Group、Tracking、AOV、3D 和 AI 才能建立在可信的执行与交付基础之上。
