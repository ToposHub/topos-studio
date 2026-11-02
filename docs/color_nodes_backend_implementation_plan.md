# 调色节点后端实施与接线计划

日期：2026-11-02。状态：**计划，后端尚未实施**。适用产品：Topos Studio / Topos Color；技术栈：PyQt6 + ModernGL + GLSL，兼顾 Metal 与 CPU 导出。

本轮已实现节点 UI 和本地交互：`node_editor_state.py` 提供 `NodeDraft/ConnectionDraft/GraphDraft/NodeEditorState`；`node_editor_items.py` 提供 `NodeCardItem/NodePortItem/ConnectionItem`；`node_editor_view.py` 提供 `ColorNodeView`；`node_editor_panel.py` 提供 `ColorNodePanel`（绝对路径见E34–E37）。节点图具有独立的 presentation state、本地undo和intent signals。宿主已经将原图层dock内容替换成节点面板，同时保留旧 `self.layer_panel` 给原controller。所有新的 edit/selection/scope/preview 信号均未连接工程或渲染后端；LUT、color_space、gamma、cache的选择目前只更新draft值。图像、调色工程和导出仍由原后端控制，不能用UI草图冒充已提交的调色工程。本计划中的领域图、command、GPU执行器和接线均为后续工作。

用户随后提供 `tests/test_videos/测试素材01.mov` 并要求实测缩略图，因此本轮额外完成了**只读源素材缩略图展示**：`src/app/controllers/color_node_preview.py` 的 `ColorNodeSourcePreview` 复用既有异步 `ThumbnailProvider`，把源素材静态缩略图送入 `ColorNodeView.set_source_thumbnail()`。这项展示接线已经实现，不属于待办；它不执行节点、不应用draft参数、不保存节点工程、不改变导出。卡片tooltip明确显示“Source preview”，所有节点暂用同一源素材图；逐节点处理后的缩略图与实时刷新仍由B09/B11/B13完成。

实施遵守 `/Users/heng/Documents/vscode/topos-studio/.clinerules`：UI 不执行 GPU 工作；状态修改通过 command/service；保留 undo、revision 与持久化一致性；释放失败/取消路径资源；HDR 不擅自裁到 0–1；未知字段保留；插件身份和后端能力不满足时明确诊断。后续施工前逐文件检查 `git status` 与 diff，区分本轮UI改动和其他工作，不覆盖无关修改。

## 1. 已核查的现状与文件索引

下表“现有”路径已在工作区检查存在。后文使用文件编号，避免将新建文件误认为现有实现。行号会随本轮 UI 或其他工作变化，实施时按函数名定位。

| 编号 | 已存在的绝对路径 | 现状与后续接点 |
|---|---|---|
| E01 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/core/layer.py` | `Layer` 是完整调色参数载体，含曲线、LUT、qualifier、Window、插件、bake 与关键帧；复用其参数 schema，逐步解除“数组索引就是身份”的假设。 |
| E02 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/core/image_engine.py` | `_layer_render_data`、`render_pipeline`、`bake_layer`、`_validate_baked_prefixes`、完整分辨率旧路径均以 layers 为入口。 |
| E03 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/core/async_renderer.py` | `_do_render_impl` 按 layers 顺序 ping-pong；`_generate_matte`、`_do_remove_layer` 维护按 layer ID 的纹理。 |
| E04 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/core/shader_compiler.py` | `ShaderPipeline.build_composition_shader` 是完整单层算子，计算 grade 后用 matte×opacity 与输入混合，保留输入 alpha。 |
| E05 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/core/composition_uniforms.py` | 现有单层 uniform 准备逻辑，应保留为统一算子服务的组成部分。 |
| E06 | `/Users/heng/Documents/vscode/topos-studio/src/shared/project/color_stack.py` | `ColorStackData` 当前 schema 为 4；`layers` / `active_layer_index` / `raw_params` 是完整权威状态；平铺 grading 是兼容视图。 |
| E07 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/adapters/color_stack_hydrator.py` | `apply_stack_to_engine`、`collect_stack_from_engine_and_panels` 恢复/捕获完整 layers 与 bitmap、跟踪绑定。 |
| E08 | `/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/commands/color_commands.py` | `UpdateColorStackCommand.apply/undo/create` 等承担工程提交；当前字段以 stack/layers 为主。 |
| E09 | `/Users/heng/Documents/vscode/topos-studio/src/app/controllers/layer_controller.py` | `LayerOpsMixin.on_layer_selected`、`update_from_layer`、`on_grading_changed`、`on_capture_still`、`on_apply_grade_from_preset`、`on_layer_bake` 等接图层 UI。 |
| E10 | `/Users/heng/Documents/vscode/topos-studio/src/app/controllers/color_controller.py` | Window/qualifier/tracker 等仍使用当前活动层和 shape ID。 |
| E11 | `/Users/heng/Documents/vscode/topos-studio/src/shared/project/save_router.py` | `_save_from_color`、`_save_color_to_edit_project`、`_extract_full_color_data_from_engine`、`_extract_color_stacks_from_engine` 都须覆盖图。 |
| E12 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/core/history.py` | `HistoryManager.push_state/undo/redo` 序列化 layers；不能再与项目 command 各保留一套互相不同步的图历史。 |
| E13 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/core/color_session.py` | `ColorSessionManager.set_current_session` 等决定 clip/asset/session 所有权。 |
| E14 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/core/color_stack_render_service.py` | `resolve_grading_params` 当前返回平铺参数，不执行完整 layers；名称不等于完整图渲染服务。 |
| E15 | `/Users/heng/Documents/vscode/topos-studio/src/features/composite/evaluators/color_evaluator.py` | `_apply_topos_color`、`_render_topos_grade_pass` 当前主要执行 primary grade；不得直接认定可无损执行 Color 图。 |
| E16 | `/Users/heng/Documents/vscode/topos-studio/src/features/composite/core/node_engine.py` | `NodeGraph.connect/topological_sort/snapshot` 可参考类型检查、成环检测和 snapshot 模式；不要直接向 Color 引入整个 Composite domain。 |
| E17 | `/Users/heng/Documents/vscode/topos-studio/src/features/composite/core/graph_snapshot.py` | 不可变 snapshot、port revision 模式可抽取，不共享 UI 活 Node 实例到渲染线程。 |
| E18 | `/Users/heng/Documents/vscode/topos-studio/src/features/composite/nodes/blend_node.py` | `MergeNode` 是 Nuke 式 premult alpha 合成，不是现成的调色 Parallel Mixer。 |
| E19 | `/Users/heng/Documents/vscode/topos-studio/src/shared/node_canvas/graph_ui_protocol.py` | 共享 Protocol 与视觉 token，不存在已接通的通用 GPU 调色画布。 |
| E20 | `/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/timeline_preview_renderer.py` | `_get_preview_color_grade`、`_apply_preview_color_grade_unstabilized`、`update_color_stacks` 须消费同一图语义。 |
| E21 | `/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/timeline_export.py` | `_get_clip_color_grade`、`_apply_color_grade_unstabilized`、`_gpu_grade_uniforms` 和 grade LUT fast path 须先判定是否完整支持图。 |
| E22 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/core/msl_composition.py` | Metal 单层 composition 生成逻辑；存在单层 MSL 能力不等于完整节点图已支持。 |
| E23 | `/Users/heng/Documents/vscode/topos-studio/src/shared/gpu/metal/kernels_color.py` | Metal 图算子实现/能力校验接点。 |
| E24 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/core/lut_exporter.py` | `LutExporter.export_lut`、`LutExportWorker.generate_lut_cpu` 目前以 layers 为输入。 |
| E25 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/core/grade_clipboard.py` | `GradeClipboard.copy_grade/paste_onto_layers` 需扩展成图/子图复制语义。 |
| E26 | `/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/color_versions.py` | `SaveColorVersionCommand`、`ActivateColorVersionCommand`、`CopyColorVersionCommand`、`_stack_from_payload` 需完整携带图、共享引用和插件 manifest。 |
| E27 | `/Users/heng/Documents/vscode/topos-studio/src/features/edit/core/color_chain.py` | `resolve_color_chain`、`chain_for_stack`、`chain_stack_ids` 决定组级/clip 级链段顺序，不能迁图后重复应用。 |
| E28 | `/Users/heng/Documents/vscode/topos-studio/src/features/scripting/api.py` | `ToposAPI.add_layer/remove_layer/select_layer/batch/grade_snapshot/capture_still` 是旧脚本兼容入口。 |
| E29 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/facade.py` | `ColorFacade.request_render/get_current_revision` 等对外出口，新增 graph API 不暴露内部 GPU。 |
| E30 | `/Users/heng/Documents/vscode/topos-studio/src/features/shared/effects/effect_catalog.py` | 效果能力与插件可用性必须经统一 catalog。 |
| E31 | `/Users/heng/Documents/vscode/topos-studio/src/features/shared/effects/effect_spec.py` | 后端能力、参数 schema、版本声明接点。 |
| E32 | `/Users/heng/Documents/vscode/topos-studio/src/features/edit/ai_editing/capability_catalog.py` | `CapabilityCatalog.register`、`create_default_capability_catalog` 负责AI可发现的能力声明。 |
| E33 | `/Users/heng/Documents/vscode/topos-studio/src/features/edit/ai_editing/action_contract.py` | `ActionContract`、`evaluate_postconditions` 负责变更前置条件、结果验证与补偿。 |
| E34 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/ui/node_editor_state.py` | 本轮已有纯Python draft/state与本地历史；不是领域图。 |
| E35 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/ui/node_editor_items.py` | 本轮已有节点卡片、端口和连线的Qt绘制对象。 |
| E36 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/ui/node_editor_view.py` | 本轮已有 `ColorNodeView`、画布手势、菜单和intent投递。 |
| E37 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/ui/node_editor_panel.py` | 本轮已有 `ColorNodePanel`、scope独立草图与compound导航。 |
| E38 | `/Users/heng/Documents/vscode/topos-studio/src/app/main_window.py` | `create_panels` 创建 `self.color_node_panel`，仍创建旧 `self.layer_panel`。 |
| E39 | `/Users/heng/Documents/vscode/topos-studio/src/app/controllers/dock_layout_controller.py` | `create_docks` 将原 `dock_layers` 内容换成新面板，保留 objectName `dockLayers`。 |
| E40 | `/Users/heng/Documents/vscode/topos-studio/scripts/preview_color_nodes.py` | 纯UI预览脚本，serial/branches/compound演示，不加载调色后端。 |
| E41 | `/Users/heng/Documents/vscode/topos-studio/tests/ui/test_color_node_editor_state.py` | 本轮已有纯状态交互测试。 |
| E42 | `/Users/heng/Documents/vscode/topos-studio/tests/ui/test_color_node_editor_view.py` | 本轮已有真实Qt输入事件测试，无engine/project/GPU。 |
| E43 | `/Users/heng/Documents/vscode/topos-studio/src/app/controllers/menu_toolbar_controller.py` | `_create_color_node_menu`、`_register_color_node_shortcuts`、`_delete_color_node_selection`、`_route_color_node_history` 已将宿主菜单/局部快捷键路由到UI draft。 |
| E44 | `/Users/heng/Documents/vscode/topos-studio/tests/ui/test_color_node_host_menu.py` | 本轮已有宿主菜单/快捷键边界与焦点历史路由测试。 |

关键语义：E03 目前 qualifier 默认采样 RAW preview 输出，不是前面所有调色后的结果；grade pass 则采样上一层输出。烘焙保存的是截至该层的完整前缀，播放时替换当前画面。最后一个启用 AI 插件结果会替代该层 matte 来源和调色输入。这三项必须单独迁移测试。

## 2. 目标模型与实施边界

最终调用链：UI intent → ColorGraphController → ColorGraphService → project command/transaction → ColorStackData.color_graph → immutable ColorGraphSnapshot → ColorGraphRenderPlan → backend executor → ColorOutput。UI receives authoritative projection / rejection，禁止由 UI 自己写 engine.layers 再补保存。

### 2.1 拟新建文件（尚未实现）

| 编号 | 拟新建绝对路径 | 建议公开接口 |
|---|---|---|
| N01 | `/Users/heng/Documents/vscode/topos-studio/src/shared/project/color_graph.py` | `ColorGraphData`、`ColorNodeData`、`ColorEdgeData`、`ColorPortSpec`，纯数据、验证、版本编解码。 |
| N02 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/core/color_graph_service.py` | `apply_intent`、`validate_graph`、`build_projection`、`resolve_shared_definitions`；无 QWidget/GL。 |
| N03 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/core/color_graph_migration.py` | `migrate_legacy_stack`、`can_project_to_legacy_layers`、`project_legacy_layers`。 |
| N04 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/core/color_graph_snapshot.py` | `ColorGraphSnapshot`、`freeze_graph`、typed image/key references。 |
| N05 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/core/color_graph_compiler.py` | `compile_graph`、`validate_capabilities`、`compute_liveness`、`ColorGraphRenderPlan`。 |
| N06 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/core/corrector_render_service.py` | `prepare_corrector`、`render_corrector_gl`、`render_key_gl`，复用 E04/E05。 |
| N07 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/core/color_graph_executor.py` | `execute_plan`、`cancel_request`、`release_revision`；仅渲染线程执行。 |
| N08 | `/Users/heng/Documents/vscode/topos-studio/src/app/controllers/color_graph_controller.py` | `on_graph_intent`、`on_projection_requested`、`on_selection_changed`、`bind_parameter_panels`。 |
| N09 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/core/color_graph_cpu.py` | CPU oracle / capability-limited execution，不进行隐式近似。 |
| N10 | `/Users/heng/Documents/vscode/topos-studio/src/features/color/core/color_graph_metal.py` | 按同一 render plan 执行 Metal 算子，能力检查与 texture lease。 |
| N11 | `/Users/heng/Documents/vscode/topos-studio/src/features/scripting/color_graph_api.py` | 带 revision/transaction 的公开脚本图接口。 |

新建文件名可在实际实现时细调，但模块职责和公开契约必须先锁定。共享的 project 模型不得 import color UI、Composite NodeGraph、moderngl。

### 2.2 schema 与权威性

建议将 E06 的 `COLOR_STACK_SCHEMA_VERSION` 从当前 4 升到实施时最新版本 +1；若实施前已有新 schema，不能硬写回 5。`color_graph.schema_version` 独立从 1 开始，保存 node-type 版本与 operator semantics version。

建议内容：`graph_id`、`nodes`（稳定 UUID、type、type_version、params/operator_ref、enabled）、`edges`（edge ID、src/dst node+port）、`source_node_id`、`output_node_id`、`working_color_context`、`shared_definition_refs`、`graph_revision`、`semantic_version`、未知字段。参数不能包含 GPU ID、Qt 对象或机器绝对路径。资源使用工程 asset/payload 引用。

`ui_state` 单列保存位置、折叠、标签、缩放、视口、多选、active_node_id；移动节点不增加 render revision，不触发像素重算。模型/布局都可撤销，但选中/平移不污染内容 dirty。`active_node_id` 不是图求值顺序。外部 projection 带 session/stack/revision；UI 不得把其他 clip 的迟到投影覆盖当前 clip。

加载优先级：合法 `color_graph` → 完整 legacy layers 迁移 → 旧平铺字段迁移。存在但不合法的新图必须报诊断，不能静默退回 layers 导致用户看到旧效果。未知 node type 保留原始 payload，并在输出依赖锥内阻断最终导出；允许只读浏览、补装插件后恢复。未知字段 load→save 保留。

新图是唯一权威来源。兼容 layers 只能在纯串行、无外部 key/共享定义/混合器且能精确表达时派生；不能把复杂 DAG 强行压平。新工程不可供旧版完整读取时保存 `minimum_reader_version` 和明确诊断，禁止旧程序静默丢分支。

### 2.3 typed ports 与操作语义

| 节点 | 输入 / 输出 | 必须锁定的语义与 UI 命令 |
|---|---|---|
| Source | 无 → image；可提供 legacy_raw key source reference | 解码/RAW/输入色彩变换只执行一次；输出显示变换不能提前烘入每个分支。禁止删除唯一 Source。 |
| Corrector / Serial | image、可选 key_in → image、key_out | 一节点是一套完整 Layer 能力；serial 是插入命令，不是新算法类型。默认 key 来源对新节点定义为 node_input；旧节点保留 legacy_raw。 |
| Output | image → frame | 唯一主输出；选择别的节点只改变 viewer target，不改变导出 output。 |
| Alpha Output | key → export alpha target | 可选独立alpha终端；只在显式连接并受导出能力支持时改变输出alpha。加入/删除不能替换主Output；RGB/HDR保持原输出语义。 |
| Parallel Mixer | base image、多个 branch image/key → image | 固定基础图像、分支结果及有效 key 参与计算；必须先出数学规格与 CPU oracle，不能用 Nuke over/average 冒充。分支输入按稳定 slot ID，不能按节点屏幕位置排序。 |
| Layer Mixer | 有序 image/key slots → image | 明确由下到上的顺序、混合模式、mask、opacity 与 straight/premult 约定；槽位重排是像素变更。 |
| Key Mixer | 多个 key → key | add/subtract/intersect 等模式，key 有界 0–1，image HDR 不受这个限制；决定是否先逐分支反转再混合。 |
| Outside | image、原节点 key_out → image/key_out | outside key 为被引用有效 key 的补集；颜色输入显式连接；不是新建无关联 qualifier。删除原节点要拒绝或显式修复依赖，不允许悬空变白。 |
| Splitter / Combiner | image → channels / channels → image | 第一版明确 RGB 三分/三合，固定通道含义与颜色域；不可和任意分支 Parallel 混为一种操作。UI 的成对插入必须形成一次原子事务。 |
| Compound | 外部 typed ports ↔ 内部子图映射 | 保留成员 IDs、边界 mapping、位置、内部 source/output 约束；展开必须逐边恢复，嵌套深度和展开节点数有限制。 |
| Shared Corrector | image/key → image/key | 共享参数定义，不共享输入或 GPU 结果；instance 有自己的输入和缓存。make local 深复制定义并解除引用；跨 clip 更新通过依赖索引 invalidation。 |

合法性：禁止 image→key 的隐式转换；需要专门提取通道/亮度节点或明示转换操作。image/key 边共同参与成环检测；compound 展开和 shared definition 引用也需查循环。单输入端口只能有一个连接；multi slot 按明确次序；Source/Output 数量、孤立节点、缺失资源、空分支、缺失端口、引用越界、schema/算子版本、无限递归都在提交前验证。UI 的暂态连线允许不完整，工程提交必须返回明确错误与保持旧图。

## 3. 里程碑与任务表

任务状态均为 TODO。依赖栏是硬顺序；单任务必须先实现最小闭环、运行对应验证并保存证据，再推进后续任务。下面命令约定在 `/Users/heng/Documents/vscode/topos-studio` 运行，使用项目可用 Python（以下 `python` 表示项目虚拟环境解释器；系统只有 `python3` 时改用已安装项目依赖的 `python3`，不可临时换到缺依赖的解释器）；新增测试路径都是计划文件，不声称现已存在。

| 里程碑 | 任务 | 进入条件 | 退出条件 |
|---|---|---|---|
| M0 基线与接口 | B00–B01 | 本轮UI源码可检查；工作区修改归属已确认 | legacy reference、消费者调用图、逐项UI intent清单冻结。 |
| M1 权威模型与旧工程 | B02–B04 | M0完成 | 新schema/合法性/legacy迁移通过；所有结构编辑有原子service语义。 |
| M2 工程编辑闭环 | B05–B06 | M1完成 | command、undo、面板与clip切换一致；此时仍不能宣称新图已渲染。 |
| M3 真实串行图渲染 | B07–B09 | B00/B03/B04完成；工程提交接线需M2 | 完整Corrector等价、snapshot、GL执行、取消和资源所有权通过。 |
| M4 分支与复用能力 | B10–B11 | M3及B05完成 | mixer/key/outside/splitter/compound/shared具有可测试的像素语义。 |
| M5 跨后端与全出口 | B12–B13 | M4完成 | 后端能力矩阵、Color/Edit/Composite/导出一致；不支持路径有明确诊断。 |
| M6 工程生态与自动化 | B14–B15 | M5及B05完成 | 保存、stills、LUT、版本、组链、脚本与AI原子提交全部覆盖。 |
| M7 发布验收 | B16 | M2–M6完成 | 第7节所有硬门通过，实机证据齐备，重复legacy生产路径清理。 |

| ID | 依赖 | 具体文件与函数改动、实施步骤 | 验证命令 / 数据 / 断言 | 交付与验收条件 |
|---|---|---|---|---|
| B00 现状冻结 | 无 | E02/E03/E07/E09/E11/E20/E21：记录每个 layers producer/consumer 和当前未提交 diff；E21已出现逐层 `layers_payload` 与 `has_advanced_operations` 降级硬门，须保留并核实，不能将当前导出描述为只读取单一平铺层；对旧引擎请求 DTO、clip flush、still/save/export 路由保存调用图。建立 `tests/color/fixtures/color_graph/` 下 synthetic fixture generator 与 reference manifest。 | `python -m pytest tests/color/test_image_engine_layers.py tests/color/test_color_state_roundtrip.py tests/color/test_color_stack_hydrator.py tests/app/test_color_stack_switch_persistence.py -q`；用 64×64 HDR ramp、透明边、两个非交换调色层、Window、qualifier、bake 与 AI stub 保存原结果。 | 基线带代码版本、工作树摘要、颜色上下文、尺寸、后端、误差定义；失败被记录修复后再建立“等价”基线，不将当前错误定成产品规格。 |
| B01 UI 契约冻结 | B00 | N08、E09/E10 与本轮节点 UI：登记实际模型和信号；只设计 adapter，冻结 intent envelope、ack/reject、projection 与 selection ownership。定义 disconnected→hydrating→ready→pending→failed 状态。 | 计划新增 `tests/color/test_color_graph_ui_contract.py`，`python -m pytest tests/color/test_color_graph_ui_contract.py -q`；fake backend 记录一次 add/delete/connect/undo intent；断言无 image_engine/GPU 调用、拒绝可恢复。 | 逐个菜单/按钮对应一个命令或明确只影响布局；没有无语义按钮；本轮 UI 演示数据不写入工程。 |
| B02 图 schema | B01 | N01：实现 frozen dataclasses、严格 parse/serialize、extra fields 与 type-version；E06 新增可选 graph 字段、`with_color_graph` 和迁移版本；活动节点/布局独立。 | `python -m pytest tests/color/test_color_graph_schema.py -q`；空图、合法串行图、future fields、未知节点、NaN/非法数字/重复 UUID/超大输入；断言 roundtrip 无损、拒绝 malformed 不污染实例。 | 共享层纯数据无 UI/GL imports；deepcopy/freeze 阻断提交后的外部原地修改；render revision 不因纯位置移动变化。 |
| B03 legacy 无损迁移 | B00,B02 | N03 `migrate_legacy_stack`：按原 layers 执行顺序逐层映射稳定 IDs；迁 qualifier `legacy_raw`、AI source override、prefix bake；E07 `apply_stack_to_engine` 先解析图再构建会话。保留原 payload 供诊断，禁止双权威。 | `python -m pytest tests/color/test_color_graph_migration.py -q`；v1 平铺、v2–v4 layers、mask bitmap、tracking、重复名称、隐藏层、空 layers、缺 LUT、旧 bake 无 digest、future extras；断言迁移幂等，ID/资源/关键帧守恒且像素与 B00 等价。 | 所有旧 fixture 只迁移一次；失败明确定位 stack/node/resource；复杂图不允许反向投影成 layers。 |
| B04 验证与编辑 service | B02,B03 | N02 `validate_graph/apply_intent`；补 N01 port catalog。实现 add/remove/serial insert、connect/disconnect、bypass、rename、order、copy/paste、outside、splitter pair、compound、shared 操作，先纯数据。成环校验 image+key+展开子图。 | `python -m pytest tests/color/test_color_graph_service.py tests/color/test_color_graph_validation.py -q`；随机小 DAG + 手写成环/悬空/key错型/compound循环；断言 rejected intent 图完全不变，每次事务确定性序列化。 | 每一 UI intent 有 service handler；一次复合操作一个 undo 单元；明确删除后 reconnection 策略，无静默断效果。 |
| B05 项目 command / undo | B04 | E08 新增 `ApplyColorGraphCommand` 或扩展 Update 命令的 graph 字段，保留旧 command 反序列化；N08 调 service→command；E12 在 standalone 引入相同图 command adapter，禁止项目/engine双写；更新锁定 stack 与 shared fanout。 | `python -m pytest tests/color/test_color_graph_commands.py tests/app/test_color_stack_switch_persistence.py -q`；连续拖参数50次一个事务、add-parallel、undo/redo、锁栈、stale revision；断言 undo 恢复字节等价图、revision单调且无重复历史。 | 同一动作只提交一次；revision mismatch 返回 conflict；schema/共享定义/资源引用随 undo 一起恢复。 |
| B06 面板与 session 接线 | B01,B05 | N08 `on_selection_changed/bind_parameter_panels`；E09 `on_layer_selected/update_from_layer/on_grading_changed` 改为 facade 提供的 active Corrector；E10 tracker/window/qualifier 绑定 node ID+shape ID。E07 `collect_stack_from_engine_and_panels` 改取权威图；E13 切 clip flush 当前事务再 hydrate。 | `QT_QPA_PLATFORM=offscreen python -m pytest tests/color/test_color_graph_panel_binding.py tests/app/test_color_graph_clip_switch.py -q`；A/B clip 快切、选 Mixer/Source、单选/多选、播放拖参数、迟到 ack；断言选节点显示对应参数，Mixer不误改前一 Corrector，程序更新无递归信号。 | 旧面板修改新图，node ID稳定；所有晚到事件带 session token 被丢弃；切 clip/关闭时事务完成或取消有明确结果。 |
| B07 完整 Corrector 算子提取 | B00,B03 | N06 `prepare_corrector/render_corrector_gl/render_key_gl`：从 E03 单层循环拆出 mask/qualifier、AI、shader、uniform/aux bind、grade blend；复用 E04/E05/E30/E31，E02 legacy full-res 改调同一实现。先保留 legacy runner 比较入口。 | `python -m pytest tests/color/test_corrector_operator_parity.py tests/color/test_color_engine_triple_golden.py -q`（GL需真实上下文）；全部曲线/LUT/warper/mixer/soft clip/plugin/Window/alpha/HDR 正交与组合 fixture；断言 neutral恒等、alpha政策稳定、legacy像素容差内。 | 不复制一份精简 grade；插件 missing/compile fail 给 node 诊断，最终导出阻断；texture创建/释放只在渲染线程。 |
| B08 snapshot / render plan | B04,B07 | N04 `freeze_graph` 深复制资源元数据，N05 `compile_graph/compute_liveness`：展开 compound、解析 shared、拓扑排序、求输出依赖锥、生成 typed passes 和 last-consumer 表。图 revision、frame、working context、viewer target固定到请求。 | `python -m pytest tests/color/test_color_graph_compiler.py -q`；diamond DAG、旁路、outside/key独立边、断开的昂贵插件、shared多实例；断言只编译目标依赖、无循环、上游共享只执行应有次数、峰值纹理有上界。 | 编译阶段不触 GPU；渲染线程不访问 UI 活模型；计划能解释每个输出来自哪个 pass/port。 |
| B09 GL 执行与取消 | B08 | N07 `execute_plan/cancel_request/release_revision` 与 E03 `_do_render_impl`：由线性循环切换 graph plan；保持 RAW前处理、降噪、防抖、全局颗粒、显示变换在定义位置且只执行一次；保留 front output lease。 | `python -m pytest tests/color/test_color_graph_gl_execution.py tests/color/test_color_graph_cancel_resources.py -q`；中途取消、resize、删节点、关闭、shader失败、陈旧frame；断言取消不发布半帧，front仍可显示，池与FBO/fence无泄漏。 | 串行图与旧链等价；render request可追踪session/graph/frame；GPU同步不阻塞UI，输出revision仅成功发布时递增。 |
| B10 Mixer / key / splitter | B07,B09 | N02/N05/N06/N07、N09：落实2.3表数学规格；新建独立 mixer/key shader，由catalog声明能力。Parallel与Layer分开实现；outside复用key_out；RGB splitter/combiner正确映射。 | `python -m pytest tests/color/test_color_graph_mixers.py tests/color/test_color_graph_keys.py tests/color/test_color_graph_splitter.py -q`；基色patch、无重叠/重叠key、全黑/全白key、非交换layer模式、HDR/负值；断言CPU oracle一致、layer次序有效、key补集准确、split+combine恒等。 | UI所有分支操作有真实像素语义；不把蒙版写进image alpha导致后续alpha合成改变；数学规格和golden一起提交。 |
| B11 Compound / Shared | B05,B08,B10 | N02/N05/E06/E08：实现compound端口mapping、深层展开、共享定义表、依赖索引；shared参改→全部引用栈失效，make local保留当前结果。复制子图重写内部ID，外部引用按明确策略保留或detach。 | `python -m pytest tests/color/test_color_graph_compound_shared.py -q`；两clip共享、一clip两实例输入不同、组内outside、跨组key、嵌套递归、undo共享参数；断言group/ungroup像素不变，共享参数变全部目标，本地副本隔离。 | 不复用某实例GPU输出作另一实例结果；保存/版本/复制保持引用一致，删除定义有引用保护。 |
| B12 CPU 与 Metal 能力 | B07–B11 | N09/N10、E22/E23/E30/E31：按相同plan补CPU oracle与Metal算子；每节点/插件声明exact/fallback/unsupported，不以存在shader函数代表整图可执行；明确临时fallback成本。 | `python -m pytest tests/color/test_color_graph_backend_capabilities.py tests/color/test_color_graph_cpu_parity.py tests/shared/gpu/metal/test_color_graph_metal.py -q`；同fixture在CPU、GL、Metal实机；断言能力预检一致、unsupported阻断最终导出、无静默少节点。 | 后端矩阵带机器/驱动/颜色上下文和误差；缺硬件标为NOT RUN，不记PASS；允许调度到有完整支持的后端。 |
| B13 Edit / export / Composite统一 | B09–B12 | E14 新增 `resolve_render_plan`；E15 `_apply_topos_color` 执行完整图而非平铺primary；E20/E21先解析图能力再选择fast path，复杂图禁止只取活动层或LUT近似。Color/Timeline/Composite使用相同plan语义与外部输入适配。 | `python -m pytest tests/color/test_color_graph_e2e_matrix.py tests/color/test_color_e2e_consistency_matrix.py tests/app/test_color_graph_export_preview.py -q`；同frame单图/视频/Timeline/Composite、图不打开也导出、CPU/GPU路径；断言像素等价且output仅应用一次。 | Viewer、Edit、最终导出一致；缺插件/后端不支持时明确失败或用户选择的完整fallback；错误不能变“成功但少效果”。 |
| B14 保存/版本/still/LUT/组链 | B05,B11,B13 | E11所有save边界、E07 `collect_stack_from_engine_and_panels`、E25 clipboard、E26 version命令、E27 chain resolver、E24 export_lut、E09 capture/apply preset：完整携图及共享定义闭包；LUT先判定pointwise可表达性。 | `python -m pytest tests/color/test_color_graph_persistence.py tests/color/test_color_graph_stills_versions.py tests/color/test_color_graph_lut_policy.py tests/app/test_project_save_routing.py -q`；save/reopen/另存、bitmap、未注册插件、两版本、组pre/clip/post、图still粘贴、局部mask；断言资源不丢、顺序正确、LUT拒绝空间/时序依赖。 | 一键保存后的重开与屏幕一致；version恢复含节点布局/定义/身份；不把不支持的复杂图导成“看似成功”的cube。 |
| B15 脚本与AI API | B04,B05,B13 | N11、E28/E29/E32/E33：在 `CapabilityCatalog.register/create_default_capability_catalog` 注册图工具参数与证据能力，以 `ActionContract/evaluate_postconditions` 验证提交结果和失败补偿；新增 `get_graph/apply_graph_edits/validate_graph/inspect_capabilities`；旧layers API对可线性投影图映射，对复杂DAG返回显式兼容错误；AI命令走同一service/transaction并做expected_revision。 | `python -m pytest tests/color/test_color_graph_scripting_api.py tests/app/test_color_graph_ai_transactions.py -q`；batch 100编辑、参数越界、成环、stale revision、旧layer脚本、AI失败回滚；断言脚本/UI同图同undo，工具schema可见可执行能力一致。 | 公共API不访问UI控件/GL；批量事务原子；每个操作返回稳定node/edge IDs与诊断，可复现。 |
| B16 性能、回归与移除旧入口 | B06,B13–B15 | E02/E03/E09/E20/E21清理生产重复链路；旧layers只留版本迁移与兼容API。新增 `scripts/bench_color_graph.py`、验证ledger和引用扫描。默认启用前跑各平台真实介质与故障矩阵。 | `python -m pytest tests/color tests/app/test_color_graph_clip_switch.py tests/app/test_color_graph_export_preview.py -q`；`python scripts/bench_color_graph.py --fixture-set standard --sizes 1920x1080,3840x2160 --frames 300 --report docs/validation/color_nodes/backend_bench.json`（计划脚本）；断言无旧链双执行、无资源持续增长、报告p50/p95/p99与丢帧。 | 每项能力READY/UNSUPPORTED/NOT RUN清晰；达到第7节门槛才标“后端接通”；保留可验证的工程格式回滚/只读恢复策略。 |

### 3.1 可独立提交的子步骤

下列每个子步骤建议独立提交；文件与验收命令继承对应Bxx主任务表，不重复发明另一套完成标准。涉及当前UI时，先运行E41/E42守住本地交互，再添加后端测试。一个提交不得跨过未完成依赖去接真实渲染。

**B00：冻结可比较基线**

1. **B00.1** 检查当前diff，记录E01–E42版本；用 `rg` 列出 `layers/active_layer_index/color_stacks` 的写入点，整理producer、command、save、preview、export、Composite调用表。
2. **B00.2** 为F01/F02/F03编写确定性生成器，manifest记录颜色上下文、参数、插件stub身份和资源hash；生成旧引擎float reference。
3. **B00.3** 为F06/F07补关键帧、bitmap、离线bake、AI迟到完成和clip切换的原行为用例，区分已知错误与必须保持的兼容语义。
4. **B00.4** 跑主表基线命令并保存结果；采集F08同机p50/p95、live texture与内存基线。无GL硬件的项记NOT RUN，不能建立伪golden。

**B01：冻结已实现UI的接线边界**

1. **B01.1** 对E34–E37逐个枚举菜单/快捷键和 `apply_edit` action，与第6.3节核对；补意图coverage测试，新增菜单未登记就失败。
2. **B01.2** 在N08定义panel envelope→领域intent规范化器，保留scope/compound_path；添加session/stack/expected_revision/transaction ID，不让UI直接import项目模型。
3. **B01.3** 用fake service实现ack/reject/projection，验证 `load_graph` 无intent回流、stale session被拒绝、pending编辑不覆盖新clip；此步不连engine。
4. **B01.4** 为 `move_and_insert`、Shift+Alt+S、compound关闭和draft undo冻结规范化规则，确保每次手势对应一个未来command。

**B02：领域schema与编解码**

1. **B02.1** 在N01实现ID、node type版本、typed ports、edges、共享定义引用、scope owner；参数用可序列化的不可变快照，写严谨parse错误路径。
2. **B02.2** 在E06加入graph字段、未知键集合、`with_color_graph`、to/from dict与schema升级；确认保留现有video_stabilization等字段。
3. **B02.3** 将ui_state/layout revision与render revision分离，定义 `number` 仅显示、node UUID为身份，源资源采用工程引用。
4. **B02.4** 添加deepcopy/future字段/非法端口/非法数值/递归大小上限测试；确认新字段经所有with_*工厂更新仍保留。

**B03：旧图层迁移**

1. **B03.1** N03按执行顺序生成Source→Correctors→Output；Layer ID映射稳定UUID，`source_layer_id`只作迁移追踪，初次迁移后不靠数组位置重建身份。
2. **B03.2** 转移完整Layer参数、Window/shape IDs、bitmap资源、tracking和keyframes；显式记录legacy_raw、AI override与bake prefix语义。
3. **B03.3** E07按graph优先级加载；空legacy层和平铺旧数据各有明确fallback，存在损坏新图时返回错误而非悄悄换旧图。
4. **B03.4** 完成v1–v4 fixture幂等迁移与像素对照；实现严格 `can_project_to_legacy_layers`，复杂DAG必须拒绝压平。

**B04：结构编辑与验证service**

1. **B04.1** N02实现typed port和combined image/key边的成环检测，校验compound/shared引用闭包与terminal规则；失败保留原图。
2. **B04.2** 实现serial before/after、add node、connect/rewire/disconnect；先完整验证新连接，成功后才移除旧边。
3. **B04.3** 实现parallel/layer/outside/splitter pair/key mixer/alpha output的原子结构操作，并给新slot分配稳定ID。
4. **B04.4** 实现删除、extract/reconnect、copy/paste/duplicate、morph、compound结构与shared定义操作；对无法安全重连的多分支删除返回diagnostic。
5. **B04.5** 随机小图验证+手写反例覆盖，逐action断言失败原子性和一次事务，补service诊断code与UI目标映射。

**B05：command、revision与历史**

1. **B05.1** E08实现graph前/后快照command、expected_revision和schema序列化，旧command载荷继续可读。
2. **B05.2** 参数拖动、move+insert、branch创建和compound成员更新采用transaction ID合并；布局command独立render revision。
3. **B05.3** N08接项目command栈；standalone经E12适配同一command语义，接通模式禁用draft undo直接恢复权威数据的行为。
4. **B05.4** 实现stack锁、shared定义fanout和undo还原依赖索引；undo/redo后统一投影而非局部改控件。
5. **B05.5** 验证50次参数拖动只一个undo、stale revision冲突不改图、连续100次undo/redo完整恢复资源引用。

**B06：现有面板与会话**

1. **B06.1** N08建立 `(session,scope owner,compound_path,node_id)`→Corrector句柄，接 `active_node_changed`；显示序号绝不能作为 `engine.layers[index]`。
2. **B06.2** E09的 `update_from_layer/on_grading_changed` 拆成节点参数读取与command提交；E10按句柄绑定Window/qualifier/tracker，使用同步锁。
3. **B06.3** 选择Mixer/Source/AlphaOutput时显示对应属性或只读状态；旧面板不得继续修改隐藏的上一层。
4. **B06.4** E13切clip/asset/project时结束或取消当前事务、递增session generation、hydrate全部scope；丢弃晚到selection/ack/thumbnail/AI事件。
5. **B06.5** 对照第6.5节逐条替换旧layers slot并留legacy adapter测试；确认隐藏旧LayerPanel不再成为工程权威写入源后，才移除旧signals。

**B07：完整Corrector提取**

1. **B07.1** 给E03单层阶段建立明确输入输出：grade_input、key_source、external_key、aux assets与lease；先不改数学次序。
2. **B07.2** N06提取matte生成与grade执行；复用E04/E05/插件binding，处理曲线、LUT、warper、soft clip、blend/opacity和alpha。
3. **B07.3** E03 legacy单层循环和E02全分辨率旧路均调用N06，保留对照入口；只改变结构，不顺手改AI/key/bake语义。
4. **B07.4** 增加compile失败、缺LUT/插件、aux上传失败和关闭清理；最终输出依赖锥失败必须阻断，不能少一节点继续导出。
5. **B07.5** 跑B00全部单层/组合golden，记录float误差和资源计数；不满足等价时不切新graph执行。

**B08：快照与执行计划**

1. **B08.1** N04冻结嵌套参数/数组/资源描述，frame与working context一次求值；测试UI继续编辑不改变正在执行的snapshot。
2. **B08.2** N05展开compound、解析shared definitions，取viewer或主Output依赖锥并排序；image/key共同参与依赖。
3. **B08.3** 生成typed passes、consumer count、最后使用点、alias约束和峰值bytes；分叉输入在所有消费者结束前保持live。
4. **B08.4** 加编译cache与operator/backend能力预检，导出可解释plan JSON；测试diamond、孤立昂贵插件、旁路与shared多输入实例。

**B09：GL执行、取消与输出发布**

1. **B09.1** N07执行串行plan并接E03调度，保留RAW/降噪/防抖/颗粒/显示的既定位置。
2. **B09.2** 每pass检查取消和generation，失败统一退栈释放lease；缓存与front资源归owner管理，不能随scratch归还。
3. **B09.3** 以request/session/revision/frame匹配结果，成功终态才发布ColorOutput；scopes/Composite读者通过lease与fence消费。
4. **B09.4** 对resize、删节点、关闭、上下文丢失、OOM/compile failure测试全路径清理；取消前画面仍有效。
5. **B09.5** 串行新旧路径同frame对照通过后切Color预览；全分辨率与视频播放仍须复用同plan，不留单独数组循环。

**B10：分支数学与key语义**

1. **B10.1** 冻结parallel/layer/key、outside、split/combine和alpha output的工作域/公式/alpha/空输入/旁路规格；用N09写可手算CPU oracle。
2. **B10.2** N06/N07实现parallel与layer的独立pass，stable slot映射与layer次序命令；不同于E18 Nuke Merge的部分显式实现。
3. **B10.3** 实现key_out/key_in、outside互补和key mixer，明确反转/blur/clean/gain时序，image alpha不作为隐含matte存储。
4. **B10.4** 实现RGB splitter/combiner恒等、alpha output terminal及后端能力预检；格式不支持alpha时明确阻断或显式选择输出策略。
5. **B10.5** 以F02/F04逐公式和组合golden验证，改变节点位置不改像素，改变layer slot次序按预期改变像素。

**B11：compound与shared执行**

1. **B11.1** 为compound边界建立显式typed mappings与稳定成员身份，group/expand在一个command中重写边并验证前后像素。
2. **B11.2** 接嵌套compound_path编辑，验证父外部wire仍可解析，拒绝孤立边界而不是关闭时丢外部连接。
3. **B11.3** 建共享定义表、版本和引用索引；参数共享不等于输入/缓存共享，定义变更按所有consumer失效。
4. **B11.4** make local深复制定义与插件/资源引用，明确解除shared引入的锁而不解除独立用户锁；copy/version/undo维持闭包。
5. **B11.5** F05跨clip多实例、嵌套key与循环反例验证；实例不同输入必须不同像素cache key。

**B12：CPU/Metal实现与能力声明**

1. **B12.1** 在E30/E31建立图node+参数组合能力矩阵，区分exact、完整fallback与unsupported；从实际operator支持生成UI可选项。
2. **B12.2** N09实现基础Corrector与branch/key oracle；复用现有CPU数学，插件无CPU实现即保留明确unsupported。
3. **B12.3** N10/E22/E23按plan实现Metal pass与lease，保证shader/HDR/颜色域/key/bypass语义一致。
4. **B12.4** 各平台运行F01–F07及故障矩阵；记录误差、缺硬件项和fallback成本，能力声明与实测不符就降级为未就绪。

**B13：统一预览与最终出口**

1. **B13.1** E14增加解析完整render plan的入口，定义外部input与scope chain适配，不再把平铺grading误当全部图。
2. **B13.2** E20接Timeline预览完整图；E21在选择GPU/CPU/LUT fast path前检查整图能力和pointwise资格。
3. **B13.3** E15 `_apply_topos_color` 接同service/operator，Composite未打开Color页仍可渲染；禁重复clip/group grade。
4. **B13.4** F01–F07同frame通过Color、Timeline、Composite、full-res、最终文件对照，验证alpha output和metadata；失败报告明确node/port/backend。
5. **B13.5** 验证带图的batch/后台导出、取消和离线bake，最后清理仅对平铺参数有效的误用fast path。

**B14：工程、still、LUT与版本**

1. **B14.1** E07/E11保存权威graph和定义/资源闭包；重开、另存、跨产品保存、future fields均不丢，禁止保存UI默认示例。
2. **B14.2** E09/E25捕获still/preset/子图clipboard，区分RGB参考图与可还原完整grade graph，分类粘贴按operator域合并。
3. **B14.3** E26版本命令携完整图/布局/shared定义版本，activate/copy/duplicate恢复一致身份与资源，缺插件不静默替换。
4. **B14.4** E27解析group pre→clip→group post→timeline scope顺序，纳入统一plan与cache依赖；换group/锁stack正确刷新。
5. **B14.5** E24先证明图pointwise可表达，再执行LUT采样；Window、跟踪、时域、空间滤镜/AI等明确拒绝或要求显式排除，不自动丢弃。
6. **B14.6** 跑保存/版本/still/LUT策略矩阵及F07离线、损坏、未知版本回归，断言重开与原屏幕同graph同像素。

**B15：公开脚本与AI**

1. **B15.1** N11定义graph inspect/validate/apply edits事务API，返回稳定IDs/revision/diagnostics，避免暴露UI draft和GL对象。
2. **B15.2** E28旧layer API接精确linear兼容adapter，复杂图返回稳定unsupported错误；`select_layer(index)`不可映射到节点number。
3. **B15.3** E32登记图工具schema与真实能力，E33加expected_revision、后置条件和补偿；AI与UI同service/command。
4. **B15.4** 脚本batch/AI多步事务以一个undo提交，循环/越界/stale修改全回滚；验证100项编辑结果与同等UI操作一致。

**B16：发布与旧入口退役**

1. **B16.1** 汇总action/provider/backend能力表，检索旧layers写入口，保留迁移/旧API适配而移除生产双写；hidden LayerPanel只能过渡使用。
2. **B16.2** 编写F08基准脚本并冻结300帧工作负载，同机对照串行性能/显存，报告decode/compile/graph/present分段。
3. **B16.3** 运行跨平台golden、clip切换、导出、save/reopen、失败/取消矩阵，NOT RUN项不能被总PASS吞掉。
4. **B16.4** 补操作帮助、schema兼容与能力诊断；逐UI菜单检查后端或layout-only契约，provider未实现的选项必须如实禁用/诊断。
5. **B16.5** 满足第7节门槛后才默认启用接通模式；保留旧工程只读恢复/迁移诊断，记录发布证据和仍需比对的Resolve交互差异。

## 4. 必须先落实的特殊语义

### 4.1 legacy key source

旧图迁移节点参数明确写 `key_source_mode=legacy_raw`；新节点用 `node_input`，外部key接入用显式key_in。`raw`指现有legacy代码实际使用的RAW preview/色彩管理结果，不是摄像机原始Bayer数据。不能改变名称就换了采样颜色域。

N06分开接收 `grade_input`、`key_source_image`、`external_key`。key_out明确包含哪些处理：qualifier、Window形状树、AI matte、反转、clean black/white、blur、key gain顺序。key_in与内部key的交互（替代/相交）必须是可序列化选项。外部key默认与内部有效key相乘可作为初始规格，但要在B10数学规格中正式冻结，不能靠信号连接顺序决定。Outside引用的是既定的effective key，不是重新从输出画面再抠一次。

蒙版缓存key至少包括node ID、key source内容revision、source frame、采样颜色上下文、尺寸/ROI、qualifier/shape/AI与后处理参数摘要、跟踪frame/绑定revision。改变上游图像时 `node_input` key必须重算；legacy_raw key不应因不相关的前置调色无故重算。

### 4.2 bake 与 AI

旧bake导入为明确的“冻结该节点完整上游依赖输出”状态，而不是把已有EXR当作该节点局部效果。保留原图，记录cache resource及dependency digest，播放从冻结节点结果读取。依赖变化时自动失效；源离线时保护唯一bake，不自动删除不可重建内容。旧无digest的cache保持兼容策略并给出诊断；不能凭新算法误判就删除。

新digest覆盖上游依赖子图所有图像/key输入、shared定义版本、资源内容、颜色上下文、source epoch/frame范围、operator版本。最终保存依赖所需的冻结资源由工程resource resolver管理，GPU临时缓存不写入工程。重新bake/取消/解冻分配generation，迟到AI和EXR完成事件只有同时匹配session/node/input digest/generation才可提交。

迁移初期保留“最后一个启用AI插件结果替代输入”的既有语义。后续拆成显式AI节点时做新type_version迁移与前后对照，不在提取Corrector过程中顺便改变。AI缓存按实际输入图像内容revision，而非仅node ID；剪辑A任务不能把结果提交到剪辑B同位置的节点。

### 4.3 mixer 数学规格的发布条件

B10必须单独提交说明输入范围、工作色域、key权重、重叠区、分支priority、缺失输入、alpha、opacity、bypass。产品是采用自定义明确公式还是追求特定版本Resolve输出兼容，需在实现前决定；本计划不声称现有Merge或任意average与达芬奇像素相同。若要求Resolve兼容，提供授权可用的同素材节点图及导出reference作为对照，记录其版本与色彩管理设置，再确定容差。

## 5. 线程、快照、资源与能力契约

1. UI线程只创建intent/绘制presentation；模型service纯CPU。事务提交产生新immutable graph，不持有UI对象。`freeze_graph`冻结所有参数容器，bitmap/数组按不可变资源引用或owned copy处理；`frozen=True`不等于嵌套dict已冻结。
2. 每个render request携session_id、stack_id、graph_revision、source_revision、source_frame、request_id、viewer_target、quality、ROI、output size、color context。执行前/每pass间/AI等待返回/发布前检查取消和generation；只发布完整可用结果。
3. 编译缓存key包含拓扑、operator schema/插件版本、后端能力；像素缓存还包含输入content revision、frame、关键帧求值结果、尺寸/ROI、色彩管理、质量。移动节点不失效像素；修改某支仅失效其下游及依赖的key。source texture ID复用不能代表内容没变。
4. 资源按执行计划的最后消费者释放，分叉输入在最后分支结束前不得归还池。前台ColorOutput、scopes、Composite消费者使用lease/refcount/fence，不与scratch提前复用。一个3840×2160 RGBA16F纹理约63.3 MiB，不能每个节点永久缓存一张4K图；计划应记录峰值live bytes并施加预算和缓存淘汰。
5. 失败/取消时释放本请求所有scratch/FBO/temporary aux，缓存资源归其owner，front output仍有效。跨revision资源在fence/consumer lease结束后deferred release。关闭窗口、切项目、GL上下文丢失、Metal command失败均有终止清理。
6. 时域降噪、防抖、RAW/输入/输出变换、film grain属于明确的管线位置，不因迁成每个Corrector就重复执行。group pre/clip/post由同一plan串联；工作域与显示域分开，scopes说明取样点。
7. CPU/GL/Metal能力是节点、参数组合、插件与图结构的联合能力。预检选择完整可执行后端；允许用户理解并接受的降速fallback，不允许“不支持就跳过”。CPU oracle不等于全部插件已具备CPU实现。

## 6. UI intent / projection 接线契约

### 6.1 本轮已存在的接口与宿主状态

E34是无Qt/engine/project依赖的draft层。`NodeDraft` 含ID、kind、number、位置、grade、source_layer_id、cache_mode、shared_id和compound mappings；`GraphDraft` 含nodes/connections/active_id/selected_ids/next_number。`NodeEditorState.snapshot()` 深复制；`load_graph()` 校验并清空本地历史；本地 `undo/redo` 仅恢复UI草图。当前大小边界为compound深度16、总节点4096、总边32768；领域限制需另由N01/N02验证，不能只信草图校验。

E35的 `NodeCardItem/NodePortItem/ConnectionItem` 只绘制Qt卡片/端口/连线。E36的 `ColorNodeView` 暴露 `edit_requested(str, object)`、`active_node_changed(str)`、`graph_changed(object)`（值为GraphDraft）、`zoom_changed(float)`、`compound_open_requested(str)`、`parent_requested()`、`preview_requested(str)`。`load_graph(graph, fit=False)` 只hydrate；快照通过 `view.state.snapshot()` 获取，View本身不另设snapshot方法。`set_thumbnail(node_id, pixmap)` 接受宿主在GUI线程提供的缩略图，不执行GPU读回。

E37的 `ColorNodePanel` 暴露 `load_graph(graph, scope='clip')`、`snapshot(scope=None)`、`set_group_available(bool)`，以及 `edit_requested(action, object)`、`active_node_changed(str)`、`scope_changed(str)`、`preview_requested(str)`。普通edit内容为：

```text
edit_requested(action, {
  'scope': 'clip' | 'timeline' | 'group_pre_clip' | 'group_post_clip',
  'compound_path': [outer_compound_node_id, ...],
  'payload': {'result': operation_result, 'arguments': optional_args, 'draft': GraphDraft}
})
```

`close_compound` 是当前特殊事件，其内层payload为 `{'node_id': ..., 'draft': parent_graph}`，不含普通 `result/arguments`。N08必须区分事件类型，不能盲读统一字段。`graph_changed` 不是工程提交信号；禁止同时连 `edit_requested` 与 `graph_changed` 各提交一次。

E38 `create_panels` 创建 `self.color_node_panel` 和仍被旧controller使用的 `self.layer_panel`。E39 `create_docks` 保留 `self.dock_layers` 与objectName `dockLayers`，内容已换成 `color_node_panel`，从而保留dock布局标识。原 `layer_panel.layer_*` signals仍连接E09/engine旧slots；新面板的edit/selection/scope/preview signals**均未接后端**。可见节点草图不是当前真实engine.layers的镜像，选择新节点也不会选择/创建真实调色层。

E43已将 Edit → Nodes 的宿主菜单改为新view/state调用，保留 `menu_layer` 与旧局部变量名只为兼容装配函数参数，菜单不再调用旧layer controller。14个shortcut ID为 `color_nodes.add_serial/delete/label/enable/enable_all/reset/copy/paste/duplicate/add_before/add_parallel/add_layer/add_outside/splitter_combiner`，category为Nodes，QAction parent与addAction均挂到view，`force_widget_shortcut=True`；即使用户全局快捷键选项开启，仍为WidgetShortcut。菜单/隐藏toolbar undo与redo由 `_route_color_node_history` 在焦点处于panel子树时作用于draft，否则沿用宿主原undo/redo；`_refresh_color_node_history_actions` 在Edit菜单aboutToShow时按draft的can_undo/can_redo更新启用状态，非节点焦点恢复原 `update_undo_redo_actions` 行为。未来后端接线须替换这个draft分支和历史可用性provider，不能只改右键菜单而遗漏宿主入口。

### 6.2 后续adapter与事务约束

N08将现有envelope规范化为带 `intent_id/session_id/scope_owner_id/stack_id/expected_graph_revision/transaction_id/compound_path` 的领域intent，再调用N02。`draft` 仅作受校验的提案/乐观显示与测试诊断，不能整包覆盖工程；领域服务重做类型、资源、引用、能力和revision验证。

现有若干action的 `arguments` 是None或只含位置，接线前必须补齐：操作前target IDs、active ID、selected IDs、new kind、before/after、stable slot、被插入edge与资源ID。不能只根据操作后的active/selection逆猜原目标。`result` 也不能作为未经验证的工程ID来源；可以由command分配后ack重映射，或预分配UUID并在事务中验证。

特别规则：

- `move_nodes` 是一次释放鼠标后的布局提交；`move_and_insert` 同时包含positions、node ID、被插入edge，必须一个command、一个undo与一次revision，不先move提交再insert提交。Alt拖拽duplicate同样一次事务。
- 右键“Add Serial Before”发 `add_serial_before`；当前键盘Shift+Alt+S仍通过 `add_serial` 调用 `add_serial(before=True)`。接线前须统一携带 `before=True`，不能让快捷键和菜单在后端变成两种结果。
- 当前Undo/Redo会先执行draft history再发 `undo/redo`。接通模式要由project/session command栈直接执行，再 `load_graph` hydrate权威projection；不能先回退draft再回退项目，不能在切clip时拿旧scope本地历史undo新clip。
- ack携带权威graph、revision与ID映射；reject携带stable diagnostic code、目标node/port和原revision。UI回滚到最近ack，保持当前选择/viewport可恢复；load/hydrate有锁，不再发新intent。异步ack必须匹配当前session/scope owner/compound path。
- 布局/标签/颜色是持久化UI元信息，可进入布局历史但不变render revision；lock是编辑权限元信息，影响后续command能否提交而不直接改变像素。cache是执行策略元信息，不应改变输出像素但应失效执行策略计划。

### 6.3 实际菜单与手势action完整映射

下表逐项来自E36 `build_context_menu/keyPressEvent/mouseReleaseEvent`、E37 `build_options_menu` 和E43宿主Nodes菜单。宿主的serial/delete/label/enable/enable_all/reset/copy/paste/duplicate/before/parallel/layer/outside/splitter均复用表中同名action；宿主delete可一次 `disconnect_many` 删除多条选中线，需在一个command校验并提交edge集合，而非只处理第一条。拟后端command统一表示为 **B05 `ApplyColorGraphCommand(operation=...)`**，括号内为N02 operation；尚未实现。layout-only使用同一项目布局事务或会话UI状态，不执行GPU。

| 实际入口 / action或signal | 后续处理与必须补齐的参数 | 像素/历史类别 |
|---|---|---|
| Add Serial / `add_serial`（Alt+S） | `insert_corrector`：active node、after；Source/Output附近合法插入点由service确定。 | 结构command。 |
| Add Serial Before / `add_serial_before`（Shift+Alt+S菜单） | `insert_corrector(before=True)`；键盘目前action仍为add_serial，按6.2补统一参数。 | 结构command。 |
| Add Parallel / `add_parallel`（Alt+P） | `add_parallel_branch`：branch origin、active branch、mixer slots、完整新增node/edge集合。 | 原子结构command。 |
| Add Layer / `add_layer`（Alt+L） | `add_layer_branch`：显式layer slot顺序与基底输入。 | 原子结构command。 |
| Add Outside / `add_outside`（Alt+O） | `add_outside_corrector`：原key_out引用和image输入，不通过后端再抠一次。 | 结构+key依赖。 |
| Add Splitter Combiner / `add_splitter_combiner` | `add_splitter_pair`：成对节点、RGB mapping、原上下游边。 | 一次command。 |
| Add Key Mixer / `add_key_mixer` | `add_key_mixer`：明确key slots与原key source。 | 结构command。 |
| Add Node子菜单 Corrector、Parallel Mixer、Layer Mixer、Key Mixer、Splitter、Combiner / `add_node` | `add_node(kind,position)`：现有action通用，后端必须收到kind，不以result number推断类型。未连接节点允许保留，导出只取依赖锥。 | 结构command。 |
| Add Alpha Output / `add_alpha_output` | `add_alpha_output`：独立key terminal，主Output不变；导出格式能力预检。 | 结构/alpha语义。 |
| Alpha Output专用菜单Remove Alpha Output / `remove_alpha_output` | `remove_alpha_output`：显式断开alpha目标，不能删除主RGB Output。 | 结构command。 |
| 连线专用菜单Delete Connection / `disconnect`；Delete选中连线 | `disconnect_edge`：完整src/dst typed endpoint或edge ID。 | 结构command。 |
| 拖线/重新接线 / `connect`；输入拖到空白 / `disconnect` | `connect_ports`先验证新edge再替换旧edge；失效/成环保留旧图。 | 结构command。 |
| 移动 / `move_nodes` | `update_layout(positions)`：稳定ID，不改变slot顺序。 | layout-only，一手势一undo。 |
| 拖节点插在线上 / `move_and_insert` | `move_and_insert(positions,node_id,edge_id)`，现有arguments只有positions，接线前补node/edge。 | 布局+结构一次事务。 |
| Alt拖拽 / `duplicate_nodes` | `duplicate_subgraph`：原selected IDs、offset、边界引用策略；重写所有内部ID。 | 一次command。 |
| Node Label / `label_node`（F2） | `set_node_metadata(label)`：label不是node identity。 | 元信息，不render。 |
| Node Color / `node_color` | Default、Red、Orange、Yellow、Green、Cyan、Blue、Purple、Pink逐值保存UI元信息。 | layout-only，不render。 |
| Enable Node / `set_enabled`（Cmd+D）；Cmd+Alt+D / `set_all_enabled` | `set_bypass/enabled`：明确目标IDs、旁路port mapping；Mixer不能任意透传第一个input。 | 像素command。 |
| Lock Node / `lock_node` | `set_edit_lock`：与stack锁/shared锁分别存储，command也验证。 | 权限元信息，不render。 |
| Reset Node Grade / `reset_grade` | `reset_corrector_parameters(ids)`；非Corrector类型按其schema定义可重置项，不能抹掉连接。 | 参数command。 |
| Copy / `copy_grade`（Cmd+C，无edit intent） | 读取完整operator域的grade clipboard；同时复制子图草图供Paste Nodes。未来从权威图生成clipboard。 | 读取，不command。 |
| Paste / `paste_grade`（Cmd+V） | `paste_grade_domains`：完整grade资源/关键帧/类别策略，不能只信draft grade字典。 | 参数command。 |
| Paste Nodes / `paste_nodes` | `paste_subgraph`：重写ID，校验外部资源、shared定义与compound闭包。 | 结构command。 |
| Duplicate Node / `duplicate_nodes` | `duplicate_subgraph`：与Alt拖拽同service，UI offset不同而已。 | 结构command。 |
| Delete Node / `delete_nodes`（Backspace/Delete） | `delete_nodes`：显式重连策略，保护Source/主Output和compound边界。 | 结构command。 |
| Extract Node / `extract_nodes`（Alt+Backspace） | `extract_nodes`：原上下游按定义重连，被提取节点的保留/断开由service对齐本地交互语义。 | 结构command。 |
| Create Compound Node / `make_compound` | `group_subgraph`：边界mapping及内部IDs保留，外部edge重写一次。 | 一次结构command。 |
| Show Compound Node / `compound_open_requested` | 进入child projection；没有像素变更；检查root session与compound_path。 | 导航。 |
| Back / `parent_requested`；关闭后 `close_compound` | 见6.4：已提交child编辑不在关闭时二次提交。当前特殊payload必须规范化。 | 导航/待提交事务收束。 |
| Decompose Compound Node / `expand_compound` | `ungroup_subgraph`：展开mapping，结果与未展开前像素等价。 | 结构command。 |
| Save as Shared Node / `save_shared` | `create_shared_definition`：参数域、版本、消费者索引、已有锁的来源；不能把UI随机shared_id当成已存在工程定义。 | 定义+引用command。 |
| Convert to Corrector Node / `unlink_shared` | `make_local_corrector`：深复制当前定义、解除引用；区分shared引入的锁与用户锁。 | 定义引用command。 |
| Invert Key Input / `set_node_option(name='key_invert')` | `patch_key_settings`：在固定key处理顺序执行，映射到领域schema键而非直接转发字典。 | 像素参数command。 |
| LUT → No LUT / `set_node_option(name='lut_path', value='')` | 清除LUT资源引用；neutral路径不加载旧资源。 | 参数command。 |
| LUT → Browse… / `set_node_option(name='lut_path', value=path)` | provider校验格式/域/大小/hash，导入工程资源并替换绝对路径为resource ref；失败无提交。当前只记draft path。 | 资源+参数command。 |
| Color Space / `set_node_option(name='color_space')` | Use Timeline、Rec.709、Rec.2020、P3-D65、ACES AP0、ACES AP1、DaVinci Wide Gamut；provider返回规范descriptor和实际支持项，不能字符串即变换。 | 颜色上下文command。 |
| Gamma / `set_node_option(name='gamma')` | Use Timeline、Linear、Gamma 2.2、Gamma 2.4、sRGB、ST2084、HLG、DaVinci Intermediate；与primaries联合验证，gamma字符串不写进轮子的gamma tuple。 | 颜色上下文command。 |
| Mixer → Add One Input / `add_input` | `add_mixer_slot`：分配稳定slot ID并校验上限；当前只是input_count+1。 | 结构command。 |
| Morph into Layer/Parallel Mixer / `morph_mixer` | `change_mixer_type`：保留兼容edges和slot次序，转换参数需要明确映射。 | 像素/结构command。 |
| Layer Mixer → Composite Mode / `set_node_option(name='blend_mode')` | Normal、Add、Subtract、Multiply、Screen、Overlay、Soft Light、Hard Light、Difference、Color、Luminosity；operator catalog映射到正式mode IDs。 | 像素参数command。 |
| Node Cache → Auto / On / Off / `node_cache` | `set_cache_policy`：后端cache policy provider接预算与失效规则；cache On不是bake，不冻结图像，也不改变图像内容。 | 执行策略元信息。 |
| Cleanup Node Graph / `auto_layout`（右键与Options） | `update_layout`：稳定ID+positions，不重排Layer Mixer slots。 | layout-only。 |
| Zoom to Fit / `fit_graph`（Home、右键、Options） | view变换。 | 仅presentation。 |
| Reset Zoom / `set_zoom(1.0)`；滑块/滚轮 | view变换，`zoom_changed(float)` 同步滑块。 | 仅presentation。 |
| Show Grid / `_toggle_grid`；Show Thumbnails / `_toggle_thumbnails` | UI显示偏好；缩略图实际内容来自宿主provider，不能在开关回调同步GPU读回。 | 仅presentation。 |
| Options/快捷键 Undo / `undo`、Redo / `redo` | 接项目/session command栈，返回projection；接通后不使用draft快照替换工程。 | 项目/会话历史。 |
| Reset Grades and Keep Nodes / `reset_grades` | `reset_graph_parameters`：保留结构/labels/布局；对shared定义明确本地重置还是影响全部引用，不暗改他clip。 | 参数事务。 |
| Reset All Grades and Nodes / `reset_all` | `reset_graph`：保留Source/Output所需绑定和scope owner，解除图内引用但不删其他clip仍用的定义。 | 整图一次事务。 |
| Pointer/Hand、Space/中键拖动、Esc取消连线/拖动 | UI交互模式；Esc撤销未提交手势，不取消别的已提交job。 | 仅presentation。 |
| 点击/框选/Cmd+A、Alt+左右选择相邻节点 | `active_node_changed(str)`，只绑定面板；相邻按UI number导航，不能当执行顺序。 | 选择状态。 |
| 非Compound双击 / `preview_requested(node_id)` | viewer target请求，带root session/path找计划输出；主Output/导出不变。 | Viewer，不改grade。 |
| Clip/Timeline单选与scope菜单 / `scope_changed` | 解析真实scope owner与图投影，group不可用保持禁用，不新造group。 | 会话导航。 |

其中 `set_node_option` 是key_invert、lut_path、color_space、gamma、blend_mode共用的实际action，必须按name分派到不同领域schema，不能一个通用dict update绕过验证。列表中的provider和renderer能力均是后续工作：当前LUT/颜色空间/gamma/cache菜单只存draft值，不代表文件已解析、颜色已变换、缓存已生成。无能力时仍可浏览草图，但接通模式不可接受“已应用”假状态。

### 6.4 scope与嵌套compound

当前Panel以 `_scope_states: dict[str, NodeEditorState]` 分别保存 `clip/timeline/group_pre_clip/group_post_clip` 草图；Clip/Timeline单选按钮和scope菜单切换这些本地state。`set_group_available(False)` 会使group scope回到clip。后续必须把真实owner纳入投影cache key：clip→clip的color_stack_id；group_pre/post→当前clip真实group ID与阶段；timeline→timeline ID及其grade owner。当前同名scope草图不能跨clip复用；切session时重置/重hydrate全部scope，非当前scope也不能残留上一clip数据。

`scope_changed(str)` 只说明UI所选scope，N08需从E13/项目查询归属和锁定状态，再异步加载 `panel.load_graph(graph, scope=...)`；不可把当前draft保存到新scope。group成员变化同时更新 `set_group_available` 和scope owner；E27/B14将group pre→clip→group post→timeline顺序编入统一plan，避免scope UI切换导致重复执行或漏timeline。

当前compound通过 `_parents` 保存父state/node ID，打开时构建child NodeEditorState；child编辑先记录本地，关闭时 `replace_compound_children` 验证所有外部mapped边界仍存在且typed port方向一致。被占用边界若缺失，关闭被拒绝并留在child。之后发 `close_compound`。这是UI草图保护，不是工程子图事务已经实现。

后端建议采用**立即提交嵌套intent**：N08用compound_path定位root graph内child，验证并一次替换root immutable图；ack带完整root projection与当前child projection。`close_compound` 仅导航与校验，不再提交已经逐项ack的child草图。若未来采用“关闭才提交”，则child编辑必须统一暂存、关闭一次原子提交，不能混合两种模式。本计划B11以立即提交为验收路线。

多层compound路径必须包含root session/scope owner并逐层解析；成员node ID不按显示number定位。删除或更换父scope时，未ack child事务先结束/回滚再导航；迟到child ack不能复活已删除compound。边界变更须同步验证父外部edges，child局部typed-valid不能代替root-valid。

### 6.5 替换旧layers slots与保留回归

E38当前的 `layer_panel.layer_*` 接线仍供旧后端使用。后续不得将新节点number减1直接调用旧slot；只有N03记录的 `source_layer_id` 允许在纯legacy串行兼容模式查真实Layer ID，查到后也须检查当前stack/session。复杂图使用Corrector句柄和参数服务，不制造虚拟数组索引。

| 当前接点 | 替换方案 | legacy回归断言 |
|---|---|---|
| `layer_selected→on_layer_selected(index)`、`update_from_layer` | `active_node_changed(node_id)`→N08解析Corrector参数；读模型projection，程序更新使用锁。 | 同一旧Layer迁移后显示同参数；重编号/拖动不切错层。 |
| `layer_added/removed/reset/moved` | 使用N02结构/参数command；move_nodes是位置变化，不是旧stack reorder；Layer Mixer slot order另设command。 | 纯串行增删、reset与旧工程结果相同；DAG不能调用旧数组move。 |
| `layer_visibility_changed/renamed` | 映射enabled/bypass和metadata命令。 | disable旁路有类型定义；rename不改变像素/资源ID。 |
| `layer_blend_mode_changed→engine.set_layer_blend_mode`、opacity直连 | 断开新模式直写，按active Corrector/mixer句柄patch；command统一触发render revision与save。 | blend/opacity roundtrip与undo正确，不漏导出。 |
| `layer_copy/paste/duplicate` | E25扩展grade域及subgraph clipboard，走B14资源/定义闭包与N02重写ID。 | bitmap、LUT、插件、关键帧不丢；duplicate与原图独立。 |
| `layer_bake/unbake`、`engine.layer_baking_finished` | job按node UUID+dependency digest+generation定位；旧index完成信号仅legacy adapter处理。 | 旧prefix bake等价；删节点/切clip后的完成事件不串写。 |
| E09 `on_grading_changed` 与其他panel参数slots | 先取得scoped Corrector handle，再发参数事务；Source/Mixer选择不保留旧active layer写权限。 | 旧单层手调结果一致；选Mixer再拖轮子不误改上一Corrector。 |
| E10 shape/qualifier/tracker与matte preview | 使用node ID+shape ID+source frame绑定和key输出；scope/node切换重新hydrate。 | 跟踪目标身份、legacy_raw取色域和mask preview不变。 |
| E07/E11 capture与save、E12 history、E20/E21 grade payload | 读取唯一graph权威，复杂图不能从隐藏LayerPanel/engine.layers覆盖；旧数组只在严格适配器内。 | 旧save/reopen、undo、clip切换保持；新DAG所有出口不丢分支。 |

接线分阶段：先权威projection替换 `default_graph` 示例 → selection/属性只读 → command与undo → 参数panel → 串行渲染 → 分支/资源provider → 保存/所有出口。每阶段保留E41/E42和原E09/E07/clip/save测试；只有后端闭环可用才移除hidden旧LayerPanel及其旧接线。`dockLayers` objectName继续保留以兼容现有布局，不因内部widget换代重置用户工作区。

### 6.6 当前UI验证与后续接线验证

现有纯UI测试命令：`QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/ui/test_color_node_editor_state.py tests/ui/test_color_node_editor_view.py tests/ui/test_color_node_host_menu.py -q`。E40预览：`.venv/bin/python scripts/preview_color_nodes.py --scenario branches`；离屏截图：`QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/preview_color_nodes.py --scenario compound --screenshot /tmp/color-nodes-compound.png`。这些仅证明草图/Qt交互和外观，不能证明渲染、工程持久化或Resolve全量等价。

后续B01/B05/B06测试用QSignalSpy/fake backend断言：一次右键/宿主菜单或move+insert只一个intent/command；load/hydrate不产生edit；标准payload与close_compound特例都能规范化；scope/compound/session晚到事件被丢弃；项目undo只执行一次；拒绝后原graph/revision恢复；选非Corrector不修改隐藏Layer；焦点在其他页面/输入控件时Nodes快捷键不越界。缩略图provider按session/node/revision过滤并在GUI线程调用 `set_thumbnail`，不得从UI同步解码/读回GPU。

### 6.7 已实现的源素材预览与后续逐节点预览迁移

已有文件：`/Users/heng/Documents/vscode/topos-studio/src/app/controllers/color_node_preview.py`、`/Users/heng/Documents/vscode/topos-studio/scripts/validate_color_node_media.py`、`/Users/heng/Documents/vscode/topos-studio/tests/ui/test_color_node_source_preview.py`、`/Users/heng/Documents/vscode/topos-studio/tests/ui/test_color_node_editor_panel.py`。

- `MainWindow.create_panels` 构建 `ColorNodeSourcePreview(self)`，400ms定时器只观察当前媒体路径，不解码、不取GPU像素；无素材或面板隐藏不排队，已有缩略图不重复请求。实际提取通过共享 `ThumbnailProvider.get_thumbnail(path,160)` 的工作池完成，已有缓存可直接复用。
- `thumbnail_ready(path,QPixmap)` 回GUI线程后再次匹配当前路径，丢弃离开素材的迟到结果；路径改变清除旧图。该路径只定义一张素材静态预览，不承诺当前playhead帧、颜色管理等价、Window/key结果或每个节点的grade结果。
- `ColorNodeView.set_source_thumbnail(pixmap,description)` 为Corrector/Compound提供源图fallback；`set_thumbnail(node_id,pixmap)` 为后端真实节点图预留，已有单节点图不被source fallback覆盖；缩略图开关只改变显示。新增节点可继承源图，图像不进入draft、clipboard或undo。
- B09.1：增加 `NodeThumbnailRequest(session_id,scope_owner,compound_path,node_id,source_frame,render_revision,display_context,size)`；以真实图输出为采样对象，任务进入现有渲染队列，UI线程不调用texture.read。
- B09.2：在节点pass完成后按需求取160px以内的小图，限频、去重、取消和资源租约；必须执行同一显示变换，不能把scene-linear浮点数组直接转uint8。完成结果带完整请求身份，由主线程生成QPixmap。
- B09.3：N08匹配session/node/revision后调用 `set_thumbnail`；源图fallback仅在真实结果未到时显示。收到真实结果更新tooltip，未匹配/失败结果保留诊断，不能把源图写成“已处理”。切clip、compound导航、同路径不同in点、删除节点和设备丢失分别测迟到拒绝。
- B09.4：移除临时 `ColorNodeSourcePreview` 的路径轮询，改为session/selection/frame事件驱动；fallback仍可复用媒体缩略图服务。增加LRU/显存与CPU图像预算，节点删除和scope卸载清缓存；真实图应覆盖Corrector、Compound和其他支持缩略图的节点，静态源图不是验收替代品。
- 验证：用本次素材在两个Corrector中分别设置显著相反的曝光/饱和度，断言两张真实节点图与对应执行plan输出一致且不同；seek后帧号一致；改变viewer display transform同步变化；播放时UI无同步decode/readback。前端本轮仅通过“源图可显示”断言，尚不能执行这些逐节点断言。

本轮实机复现命令：`.venv/bin/python scripts/validate_color_node_media.py tests/test_videos/测试素材01.mov --output docs/validation/color_nodes --keep-open`。脚本使用隔离临时配置，调用现有媒体池导入、首素材时间线创建/插入与 `open_timeline_clip_in_color` 服务；不改用户现有工程，不伪造MediaAsset。`--keep-open` 留测试工程供查看；省略该参数在报告后退出。

2026-11-02实测：导入1920×1080、25fps、124帧H.264素材，打开Color并生成两个Corrector；两张源素材缩略图均非空，RGB标准差66.625，尺寸512×288（复用已有共享缓存）。`media_preview_report.json`、`source_thumbnail.png`、`nodes_with_media.png`、`studio_with_media.png` 保存在 `docs/validation/color_nodes/`。报告明确 `per_node_rendering=not_implemented`。启动日志存在现有FFmpeg探测/GL共享上下文fallback提示；本项通过不代表全后端/Scopes/播放性能通过。

完整本轮回归命令：`QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/ui/test_color_node_editor_state.py tests/ui/test_color_node_editor_view.py tests/ui/test_color_node_editor_panel.py tests/ui/test_color_node_host_menu.py tests/ui/test_color_node_source_preview.py tests/ui/test_factory_default_layout.py tests/app/test_color_stack_switch_persistence.py tests/color/test_color_state_roundtrip.py -q`；本轮结果112 passed。编译检查与 `git diff --check` 通过；当前虚拟环境未安装ruff，因此没有声称ruff检查通过。

### 6.7.1 顶栏与断线/重连交互补充（2026-11-02）

本轮对照用户截图与本机Resolve Color页：顶栏30px，左侧Pointer/Hand工具区64px后接1px分隔线，Clip/Timeline两圆点保持面板水平居中；右侧scope菜单、118px滑杆与35px更多菜单，菜单前1px分隔线，上下边界各1px。颜色仍引用项目C主题常量；按用户要求滑杆手柄为橙色。各按钮保留tooltip、互斥状态和已有菜单/缩放信号。

在Resolve实际验证输入端拖到空白处断开、从输出拖回输入端重连，测试后恢复连接，未保存参考工程。Topos UI草图支持对应手势；输入拖线期间隐藏待断开的旧连线，Esc恢复且不提交；松到空白提交一次disconnect，重连提交一次connect，分别可撤销；无效类型/成环保持旧连接。按下再原地松开不应误断线。

接线阶段B05/N08必须把connect/disconnect intent转为一次领域事务，沿用typed端口与原子替换验证，reject恢复权威图。新的拖动反馈不能提前提交删除，更不能由UI直接改engine。回归使用 `tests/ui/test_color_node_editor_panel.py` 中响应式顶栏位置断言和 `tests/ui/test_color_node_editor_view.py` 中断开→重连→双撤销、Esc取消测试；真实后端接线后还需断言输出失效、工程保存与撤销一致。

### 6.8 示波器现状排查与节点输出接入（2026-11-02补充）

**依赖结论**：现有示波器不需要等待节点后端完成才能显示。当前链路为 E09 `on_frame_ready` → `_submit_gpu_scope_demand` → E02 `request_gpu_scope` → E03 `_do_render_gpu_scope` → `gpu_scope_ready` → E09 `on_gpu_scope_ready` → `ScopePanel.update_gpu_scope` → `GpuScopeWidget`。它分析旧引擎完整画面输出，不读取节点UI草图。新节点的增删、连线、选择目前不会改变此输出；要让示波器反映真实节点处理结果，需要B08/B09的执行与发布闭环。二者必须分别验收，不能将当前黑屏、卡帧或贴图异常直接归因为“节点后端未完成”。

现场检查：2026-11-02当前原生应用窗口的测试素材主画面与Waveform均已有图像输出；只证明此刻单波形有输出，未证明所有模式、刷新、数值和共享纹理生命周期正常。用户报告的异常尚未定位到确定根因，不能标记已修复。现有刷新、源选择、多视图UI与GPU pipeline回归125项通过（offscreen，不能替代原生GL验证）。

相关现有文件（下列路径均已核实存在）：

- `/Users/heng/Documents/vscode/topos-studio/src/features/color/core/scope_types.py`：`ScopeDemand/ScopeGpuFrame` 与revision校验。
- `/Users/heng/Documents/vscode/topos-studio/src/features/color/ui/scope_panel.py`：模式、源、需求和GPU/CPU展示路由。
- `/Users/heng/Documents/vscode/topos-studio/src/shared/gpu/gpu_scope_engine.py`：GPU分析与多模式输出资源。
- `/Users/heng/Documents/vscode/topos-studio/src/shared/ui/widgets/gpu_scope_widget.py`：Qt共享纹理呈现、`glIsTexture`及FBO校验。
- E09/E03/E38：提交、调度、回传及宿主信号连接。

以下S00–S02可独立于节点后端先完成；S03–S07纳入B09/B13，作为B16的硬门。全部任务状态为TODO。

| ID / 依赖 | 实施方法 | 验证方法与完成条件 |
|---|---|---|
| S00 复现与分类 / 无 | 使用测试素材01.mov，记录异常类型（空白/静止/错图/数值不符）、播放帧、模式、输入源graded/source/reference、测量域、停靠/浮动、窗口尺寸及设备信息。保留原生截图与日志；分别检查暂停、播放、seek、切clip、隐藏后再打开。reference无still时为空不能判作GPU故障。 | 每个故障写出最短复现步骤、预期/实际与时间戳；同时保存正常对照，不用节点源图缩略图作为示波器输入正确的证据。 |
| S01 分段诊断 / S00 | 在E09提交/回传、E03 GPU_SCOPE队列与执行、ScopePanel接收、GpuScopeWidget呈现处添加可关闭的结构化诊断，关联source_revision/demand revision、mode、纹理ID/尺寸。分别统计不可见、关闭GPU lane、无front、shader失败、过期丢帧、mutex忙、纹理非共享、FBO不完整，避免静默空白难以定位。GL查询只在已有正确上下文中执行。 | 用fake demand验证各拒绝原因可识别；原生运行区分“未请求、未执行、回传被丢弃、呈现失败”。诊断不得逐帧刷屏、同步读回图像或阻塞UI。 |
| S02 修复现有链路 / S01 | 按证据只修命中的层：需求可见性/节流/revision，或渲染输出失效，或共享上下文/纹理生命周期/FBO呈现。核对E38仍连接engine信号。保留GPU-only实时路径；不能为遮盖故障开启逐帧CPU readback。若涉及核心修改，先核查调用方、失败/取消和release路径。 | 增加对应原因的focused regression；原生验证waveform/parade/luma/vectorscope/histogram/cie/falsecolor及双/四/全部模式，切换、浮动、resize后不黑屏、不交叉覆盖；播放有刷新、暂停稳定，保存修复前后证据。未完成该矩阵不能宣称现有示波器已恢复。 |
| S03 定义图取样契约 / B08,S00 | 以主Output的完整画面作为graded默认输入；节点选择只导航，不隐式改成单节点测量。若提供选中节点/Key测量，必须独立显式模式与输入标签。明确取样在group pre→clip→group post→timeline及输出变换中的位置；记录transfer/primaries/HDR单位、alpha和Window权重规则。保留source/reference独立语义。 | ramp、色条、HDR超1.0与透明边fixture：测量域与标签一致；拖动viewer/缩放不改变全画面分布；只选节点不改变默认graded波形。 |
| S04 接通真实图输出 / B09,S03 | 将E03 scope完整画面缓存/重渲染入口接到同一immutable graph snapshot与render plan，避免scope仍循环旧layers。复用GpuScopeEngine；输入lease/fence跨scope采样存活，scope输出也在presenter消费前保持有效，不得归入可立即复用的scratch。 | 两个非交换Corrector、分支Mixer及Compound结果与CPU oracle/独立单图参考一致；删除/旁路/调序后波形随真实Output变化；不能仅将旧信号接到UI就视为完成。 |
| S05 新鲜度与失效 / B06,B09,S04 | 在现有revision契约上补充session/stack owner、graph render revision、frame、颜色上下文及显式取样目标身份，取消旧scope任务并拒绝晚到结果。布局移动不失效像素；参数、连线、shared定义、组链、版本、seek必须失效完整画面缓存。 | 延迟/乱序结果注入：切clip、版本或compound后旧图不闪回；连续seek最终帧匹配；undo/redo各一次更新；同路径不同in点不混用；关闭/取消无悬挂lease。 |
| S06 颜色与多视图一致 / S04,S05,B10 | 同一取样帧一次采样供所有模式；各模式独立输出，CIE/假色使用正确颜色metadata；节点Key权重按S03处理。不重复应用显示变换，不无理由clamp HDR。 | CPU参考验证bin/peak/色度位置及误差；同帧单视图与多视图相应输出对照；检测共享纹理被覆盖、旧mask残留、颜色域切换后缓存未失效。 |
| S07 实机与性能验收 / S02,S06,B13 | 使用测试素材和4K素材录制暂停/播放/拖轮子/seek/节点编辑/切页/浮动全过程；记录主预览与scope耗时、队列等待、丢弃率、峰值资源与关闭后回收。不把scope pass耗时当整体FPS。 | 执行下述现有回归与新增图接入测试；原生共享上下文下保存单/多模式截图、日志、性能表到 `docs/validation/color_nodes/scopes/`（待生成）。确保无UI同步decode/GL readback，曲线直方图播放冻结策略仍符合现有契约；B16签收前全部完成。 |

现有回归命令（2026-11-02执行结果125 passed）：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/color/test_scope_live_refresh.py tests/color/test_scope_input_source.py tests/color/test_scope_multiview_ui.py tests/color/test_gpu_scope_pipeline.py -q
```

后续新增 `/Users/heng/Documents/vscode/topos-studio/tests/color/test_color_graph_scope_output.py` 与 `/Users/heng/Documents/vscode/topos-studio/tests/color/test_color_graph_scope_lifecycle.py`（尚未创建），覆盖S03–S06身份、图执行与lease边界。原生验证另参考已有 `/Users/heng/Documents/vscode/topos-studio/tests/color/test_gpu_scope_presenter_native.py`、`/Users/heng/Documents/vscode/topos-studio/tests/color/test_gpu_scope_multiview.py`；先核查其运行条件再执行，offscreen或跳过不能计作原生通过。

## 7. 验证数据、误差与发布门槛

### 7.1 固定数据集（计划生成，无需外部下载）

| 编号 | 数据 | 用途 / 必须断言 |
|---|---|---|
| F01 | 64×64/256×256 float32 ramp，值覆盖−0.25、0、0.18、1、4、16 | neutral恒等、HDR不截断、负值不无故归零、GL/Metal/CPU对照。 |
| F02 | RGB/CMY与肤色patch、透明边，独立alpha梯度 | qualifier输入域、split/combine、key与image alpha分离、alpha一致。 |
| F03 | 三个非交换Corrector + 曲线/LUT/插件stub + enabled/bypass | 串行次序、legacy迁移、undo/save/export一致；插件错误必须可见。 |
| F04 | 两分支、重叠Window、outside、key mixer、layer顺序 | 分支不是串行；outside严格互补；改变节点位置不改变图像，slot次序改变应按规格变。 |
| F05 | diamond DAG、嵌套compound、两clip/shared definition | 子图可逆、依赖去重、跨clip共享失效、本地化隔离和循环拒绝。 |
| F06 | 12帧程序化移动形状序列、关键帧、tracking与AI completion stub | source-frame映射、clip快切、迟到结果丢弃、temporal cache隔离。 |
| F07 | legacy v1–v4 stack、bitmap payload、bake EXR、缺源/缺LUT/未知插件/未知字段 | 无损迁移、离线资源保护、未知数据保留与失败诊断。 |
| F08 | 1080p/4K本地测试素材，固定300帧，多种链/8分支/32节点 | 性能与显存预算，真实GPU帧耗时、UI响应、无持续资源增长。素材来源和hash写manifest。 |

误差规则：纯数据roundtrip/ID/命令原子性采用精确比较；CPU float32 oracle基础算子建议abs≤1e−5（若数学实现允许），GL/Metal float16路径对规范化测试值初始采用abs≤2e−3并记录relative/max/mean误差，高HDR使用relative≤2e−3与明确near-zero规则。实际阈值由B00/B07测量冻结，不能为了通过测试无依据放宽；不使用单张8-bit截图判定HDR一致。legacy同后端迁移优先尽量逐像素等价，发生差异需有定位与规格理由。

### 7.2 故障与生命周期矩阵

所有渲染后端覆盖：开始前取消、执行第n个pass取消、readback前取消、AI完成后已切clip、shared更新后旧请求回调、节点删除后bake保存完成、输出resize、项目关闭、上下文丢失、FBO/texture分配失败、shader编译失败、缺插件、不可解码素材、离线bake、保存失败、损坏图、未来版本、100次undo/redo。断言为：不发布半帧、不串clip、不丢工程、不自动抹未知数据、不泄漏资源、不把失败标成成功。

QThread/GL实机测试与纯CPU/Qt离屏测试分别记录；`QT_QPA_PLATFORM=offscreen`不能证明真实GPU正确。测试不得悄悄skip然后将整套能力记PASS。需要硬件的结果必须列运行机器、GPU、OS、驱动/Metal版本、Python依赖与代码快照。

### 7.3 发布门槛

- 功能：本轮UI全部有对应语义或明确能力诊断；serial/parallel/layer/key/outside/splitter/compound/shared的最小可用闭环全部通过；Source/Output不会被误删或变多。
- 工程：旧fixture迁移、保存重开、clip切换、stills、版本、共享、undo/redo、插件未知字段全通过；复杂DAG不能被旧平铺视图覆盖。
- 图像：同一frame在Color/Timeline/Composite/最终导出使用一致plan；每个可执行后端通过golden matrix；不支持项预检阻断，不能跳效果。
- 生命周期：上述取消/失败矩阵通过；100次结构编辑/播放/切clip/关闭后资源回到允许基线，front输出lease有效。
- 性能：以同机旧串行B00为基准，identity/8-corrector串行p95耗时退化不超过10%或有测量与专项优化说明；4K60目标的16.67ms预算分解到decode/graph/present，报告真实达标/未达标，不保证所有重AI/32节点图60FPS。UI事件p95目标<50ms；图编译/解码不得在交互热路径同步阻塞。显存/统一内存预算按机器报告，不能无上限缓存每节点4K纹理。
- 记录：证据落 `/Users/heng/Documents/vscode/topos-studio/docs/validation/color_nodes/`（拟新建），含manifest、pytest结果、reference与差异图、backend capability JSON、GPU资源/性能日志；只有实测通过的能力标READY。

## 8. 自审与后续接线清单

文档自审要求：引用路径区分现有与拟新建；每个B任务有依赖、函数/文件改法、步骤、命令/fixture/断言与验收；检查端口类型、key/bake/AI、颜色域、共享身份、资源生命周期、图保存和所有出口；确认没有把本轮UI写成已接后端。

2026-11-02文档独立自审已完成：逐项复核上述契约；现有文件索引已扩至E01–E44，B00–B16均拆成可独立提交子步骤；纠正现有类名为 `LayerOpsMixin`、捕获函数为 `collect_stack_from_engine_and_panels`，保留导出侧已有逐层payload与能力硬门的现状。已按E34–E44实际源码登记draft/state、view/panel信号、宿主dock/menu、快捷键、菜单action、scope与compound边界，不再留下“UI接口待定”的占位说明。这里仅验证计划文档，没有执行尚未创建的后端测试或宣称硬件验证通过。

实际施工时每个任务完成填写：代码提交/工作树摘要、实现文件、测试命令/结果、未跑硬件、能力限制、下一依赖。当前B00–B16均待实施。本轮presentation UI可独立验收，后端完成度不得随UI完成度自动上调。

对照限制：本轮已观察达芬奇20的节点区视觉与Alt+P/Alt+L分支交互；其他菜单细节、嵌套操作、各状态行为仍需继续逐项比对。未完成达芬奇全部菜单、快捷键、像素结果或工作流的全量验证，不能声称100%视觉、像素和交互一致。现有preview截图与纯UI测试不替代这项对照，更不替代后端图像正确性验收。
