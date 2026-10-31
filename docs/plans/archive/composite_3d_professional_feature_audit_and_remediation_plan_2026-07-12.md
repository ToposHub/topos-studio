# Composite 3D 专业功能审计与修复计划

日期：2026-07-12  
审计对象：`docs/composite_3d_professional_feature_completion_plan_2026-07-07.md`  
审计范围：F0–F13 的代码、UI 接线、渲染路径、测试与交付物；未修改任何功能代码。

## 1. 结论

原计划**没有逐项完成**。目前已经完成了不少纯 Python 数据模型、局部渲染能力和单元测试，但多项计划要求的“用户可用闭环”仍断在 payload、UI、Scene3D 输出或实际 importer 上。因此现阶段应定位为：**3D 专业功能的基础设施/原型阶段，不应宣称为镜头级专业交付完成**。

验证结果：

- 运行 17 个 3D 专项测试文件：`466 passed in 2.36s`。
- 这些测试主要验证数据结构、数学和状态机；没有覆盖真实资产、PyQt 端到端交互或 ModernGL 多 pass 输出。
- 当前工作区已有用户未提交修改：`.zcode/plans/plan-sess_5b55a96e-dc93-428e-85ba-1741d1ee352a.md`；本审计不触碰该文件。

状态定义：`完成` = 功能路径和验收交付物均存在；`部分完成` = 有数据层/局部实现但不可完整使用；`未完成` = 无实现或只有计划/能力声明。

## 2. 原计划逐阶段核对

| 阶段 | 状态 | 已验证的实现 | 未闭环的原计划任务 |
|---|---|---|---|
| F0 资产审计 | 部分完成 | `ModelImportReport`、loader 统计/告警、Import Report 面板类及测试存在 | Import Report 面板未在应用代码中实例化；F0-4 测试资产矩阵、F0-5 各格式验收报告目录均不存在；没有真实 FBX/GLB/OBJ 回归资产 |
| F1 Outliner | 部分完成（不可用） | `SceneGraphRoot` 元数据、`scene_hierarchy.py` 及统计模型存在 | F1-2 payload 未实际填充；F1-3 面板文件不存在；F1-4~F1-6 没有 selection 同步、显隐/solo/lock 或 footer UI |
| F2 Mesh 显示模式 | 部分完成（未接线） | enum、edge/normal/tangent/UV 诊断算法、骨架 renderer 存在 | `Scene3DNode` 未消费 `display_mode`；没有线框/点/法线/切线/UV stretch 的实际 pass 和工具栏入口 |
| F3 对象/组件选择 | 部分完成 | GPU object-ID pass、object 点击选择、SelectionState、CPU face-picking 算法存在 | face ID/BVH/selection overlay/组件 transform 未完成；Point/Edge 按钮禁用，Polygon 虽启用但点击流程未调用 component picker |
| F4 材质编辑 | 部分完成（数据原型） | `MaterialEditModel`、override、preset 数据模型和现有 Model3D 少量材质参数存在 | 没有 `material3d_panel.py`、texture-slot widget、Material3DNode、材质表、材质赋值或 shader 端完整 PBR 编辑应用 |
| F5 Tangent/Normal map | 部分完成 | 静态 mesh tangent VBO 与 Scene3D normal-map 启用路径存在 | skinned tangent layout 未实现（`interleaved_skinned()` 无 tangent）；tangent overlay 和 normal-strength 参数/UI 均未完成 |
| F6 骨架/蒙皮 | 部分完成 | SkeletonData、best-effort trimesh 提取、GPU skinning、skeleton overlay 已有专项测试 | 未实现原计划指定的正式 glTF loader；仍依赖 trimesh metadata；没有骨架 Outliner、bone selection、weight display UI，FBX 可靠路径未落地 |
| F7 动画 | 部分完成 | clip 选择、loop/pingpong/once/clamp、offset/range、AnimationPanel 类、基础采样缓存存在 | AnimationPanel 未在应用代码中实例化；morph target、camera/light 动画导入、motion-vector 数据均未完成；没有真实动画资产端到端回归 |
| F8 Camera/Light 导入 | 部分完成（数据未落地） | CameraData/LightData 和 assimp 提取 helper 存在 | glTF 完整提取、导入后创建 Camera3D/Light3D 节点、camera matching、单位转换和动画接线没有完成 |
| F9 AOV | 未完成（仅数据模型） | AOV enum/config/ID allocator 存在 | Scene3D 只输出 `display` image；没有 normal/position/UV/object/material ID/motion-vector 输出、MRT/multi-pass、viewer selector 或下游端口 |
| F10 Shadow Catcher/Holdout | 未完成（仅数据模型） | render flags、CatcherMaterialData、workflow 描述存在 | 没有 shadow/holdout/catcher 渲染 pass、UI flags、输出或 workflow 文档 |
| F11 USD/Alembic | 部分完成（能力声明） | format capability matrix、loader interface、单位工具存在 | USD/Alembic backend 均为 unknown；registry 没有具体 backend；asset relink 未完成 |
| F12 专业 UI/工作流 | 部分完成 | Performance 面板与基础 3D toolbar 已接入；Import Report、Animation 面板类存在 | Import Report/Animation 面板未在应用代码中挂载；Outliner、Material Editor、Texture Inspector、UV Viewer、Skeleton、AOV、Deformer Stack 面板均缺失；工具栏的组件模式也未闭环 |
| F13 Deformer/Effector | 部分完成（CPU prototype） | DeformerStack/Field 数据模型，Bend/Twist/Bulge/Taper/Stretch/Noise CPU 算法，Deformer3D 串联节点与缓存存在 | 无内部 stack UI、Field/mask/selection UI、GPU deformer path、skinned mesh 支持、AOV 联动、性能基准/测试资产；Effector 节点整体未实现 |

## 3. 已确认问题与缺陷清单

### P0：先修复功能断链和错误宣称

| ID | 问题与证据 | 影响 | 修复与验收 |
|---|---|---|---|
| P0-01 | F1 定义了 `ModelPayload.hierarchy`，但 `model3d_node.py` 构造 payload 时没有调用 `build_hierarchy()` 或传入 `hierarchy`；同时 `ui/model3d_outliner_panel.py` 不存在 | Outliner 完全无法交付，导入层级、材质/骨架检查不可用 | 接入 hierarchy payload，新增树面板和 selection 同步；导入多 mesh 层级资产后可选中、隐藏、solo，且 viewport 正确响应 |
| P0-02 | F2 的 `ViewportDisplayMode` 和诊断函数没有被 `Scene3DNode` 消费；全局搜索未发现 display-mode 到渲染 pass 的接线 | wireframe、points、normals、UV debug 等界面承诺实际无效 | 先实现 edge-buffer wire/shaded-wire、point、normal、UV debug 的独立 pass；每个模式用截图/像素断言验证 |
| P0-03 | Component toolbar 将 Point/Edge 禁用；Polygon 启用，但 `_handle_3d_left_press()` 只执行 gizmo/icon/object-ID picking，未调用 `mesh_picking.pick_face()` | F3 的点/边/面选择不可达，SelectionState 只是孤立模型 | 将 component mode 分派到面/点/边拾取，增加高亮 overlay 和 modifier 选择；未完成前禁用 Polygon，避免假功能 |
| P0-04 | F9 的 AOV 只在 `render3d/aov.py` 定义数据；`Scene3DNode.outputs` 只有 `display` | 所有深度/normal/position/UV/ID/motion-vector 合成输出均不可用 | 先落地 depth、normal、object-ID 三个端口及可视化；验证下游可读、尺寸/色彩空间正确且 cache key 包含 AOV 配置 |
| P0-05 | F10 Catcher/Holdout 同样只有数据类；没有被 `Scene3DNode` 或 shader 使用 | 影视遮挡/阴影 matte 工作流不可用 | 在 F9 基础上实现 holdout depth/matte 与 shadow catcher；提供最小 ground-plane 集成测试和示例工程 |
| P0-06 | `ImportReportPanel` 和 `AnimationPanel` 只被各自测试实例化；应用内没有 `ImportReportPanel(` 或 `AnimationPanel(` 调用 | 已写的检查/播放 UI 对用户不可见，F0/F7/F12 的 UI 验收不成立 | 选择合适 dock/inspector 容器接入两面板，绑定 Model3D selection 与参数变更；补 page-level Qt 测试 |
| P0-07 | Deformer 对 `is_skinned=True` 的 mesh 直接透传；且 Scene3D 只渲染第一个有效 ModelPayload。原始 Model3D 与 Deformer3D 若同时直连 Scene3D，常会显示未变形的原模型 | 用户调任意 Deformer 参数都看不到变化，尤其是角色资产 | 按第 7 节 D0 重建单一 geometry chain、增加状态诊断与 skinned 策略；未支持的路径必须在 UI 明示为“不支持”，不能静默透传 |

### P1：修复现有链路的正确性与资产保真风险

| ID | 问题与证据 | 影响 | 修复与验收 |
|---|---|---|---|
| P1-01 | `Deformer3DNode` 新建 ModelPayload 时没有透传 `skeletons`、`hierarchy`、`model_error`；它还明确对 skinned mesh 透传 | 经过 Deformer 后 skeleton overlay/未来 Outliner 信息丢失；骨骼资产无法满足 F13 的联动承诺 | 完整透传非几何 payload 字段；添加“Model3D → Deformer → Scene3D skeleton overlay”回归测试 |
| P1-02 | Deformer 是 CPU 重新计算后上传 VBO，不是计划中的 GPU deformer；没有 proxy/帧缓存/基准资产 | 大模型交互可能卡顿，无法满足 50 万面验收 | 先增加计时、静态/动态 cache 和 proxy；再评估 GL 4.1 可行的 transform-feedback/vertex-shader 路径；提交 10万/50万/100万面基准 |
| P1-03 | F5 的 skinned vertex layout 是 pos/normal/uv/bone id/weight，不含 tangent；计划 F5-3 未实现 | 带 normal map 的角色不能可靠渲染 | 增加 skinned+tangent layout/shader，验证 OpenGL/DirectX normal map 与动画均正确 |
| P1-04 | glTF skin 骨架仍由 trimesh metadata best-effort 提取，未见 `model_loader_gltf.py`；原计划要求正式 parser | 不同 trimesh 版本/复杂 glTF 可能丢父子关系、inverse bind 或动画 | 实现并测试独立 glTF skin/animation loader；使用带多 skin、重复骨名、非平凡 bind matrix 的真实资产 |
| P1-05 | F0 没有版本化的真实资产矩阵和 `docs/benchmarks/3d_import/` 验收记录 | “支持格式/保真度”无法复现，后续修复无基线 | 纳入可再分发小型 OBJ/GLB/FBX 或明确外部 fixture 获取方式；CI 生成 import report snapshot |
| P1-06 | FormatCapability 对 glTF/GLB 标为 full，但实现仍是 trimesh 路径且 F6/F8 多项未闭环 | UI 可能夸大实际导入能力 | 以真实 automated fixture 结果更新 capability；未实现能力降为 partial，并在 Import Report 明示 |

### P2：专业编辑器与导入工作流缺口

| ID | 问题 | 修复范围 |
|---|---|---|
| P2-01 | 完整材质数据模型未进入 MaterialData、shader 和 UI；没有逐材质 assignment | 完成 F4-1~F4-9，先 per-material baseColor/metallic/roughness/normal 槽，后高级 PBR/库 |
| P2-02 | Camera/Light 虽有数据结构/assimp helper，未转为节点或 scene item；无 DCC match | 完成 F8 导入桥接、focal/sensor/clip 与实际 Camera3D/Light3D 创建；为无 camera/light 资产做负例测试 |
| P2-03 | Skeleton、weight、UV、texture、AOV 等专业面板缺失 | 以 Outliner → Material → UV/Texture → Skeleton/Weight → AOV 的顺序补 UI，所有更新走 signal、不能从 UI 调 GL |
| P2-04 | USD/Alembic 是 capability matrix 占位，loader registry 没有实现 backend | 明确依赖策略和安装检测，再做 USD/Alembic spike；未完成前在导入 UI 灰显而非宣称支持 |
| P2-05 | FBX 的可靠 skin/animation path 仍未选择/验证 | 完成 ufbx 与 Blender-headless spike 对比，设定主路径、fallback 和 lost_features；避免 CLI 静态化后仍标注动画可用 |

### P3：后续能力，不应抢在 P0/P1 前实现

| ID | 范围 | 前置 |
|---|---|---|
| P3-01 | BVH、框选、point/edge 精确拾取、selection-set 持久化与组件 transform | P0-03、真实大模型性能基线 |
| P3-02 | Morph target、camera/light animation、motion vector | P1-04、P0-04 |
| P3-03 | FFD、Displace、Collision、Jiggle、Effector/Field 全套 | P1-02、P3-01 |
| P3-04 | USD/Alembic 镜头级交换、asset relink、缓存与版本化 | P2-04、P2-05 |

## 4. 推荐修复顺序

1. **建立可复现基线**：完成 P1-05、P1-06，所有导入能力以真实资产报告为准。
2. **让检查能力真的可用**：P0-01、P0-02、P0-03。先 Outliner、wire/normal/UV、object/face selection；这是后续资产问题的诊断基础。
3. **让合成输出有最小闭环**：P0-04 后接 P0-05。不要在 AOV 端口真实可用前继续扩展其数据类。
4. **收紧资产和变形正确性**：P1-01 至 P1-04，特别是 skinned+tangent、正式 glTF parser、Deformer metadata 透传。
5. **补专业编辑器**：P2-01 至 P2-05；每次只交付一个可用垂直切片并补 UI/渲染/真实资产测试。
6. **最后再扩展高级能力**：P3 项，按性能基准决定 GPU/缓存设计，避免再堆积未接线的数据模型。

## 5. 每项修复的统一验收要求

1. 数据层单元测试之外，至少有一个真实资产或 GPU offscreen/viewport 集成测试。
2. UI 可达性必须被测试：按钮状态、信号、payload、Scene3D 渲染结果需形成闭环。
3. 功能不可用时应禁用或标识为实验性；不能只因为 enum、dataclass 或测试 stub 存在就标记完成。
4. 修改 `scene3d_node.py`、`model3d_node.py`、loader、shader 后执行 `.clinerules` 的 GLSL/线程/资源释放检查，并运行相关 pytest。
5. 更新 format capability 和 Import Report 的文案，使其仅描述已验证的能力。

## 6. 后续任务卡建议

首个修复任务建议拆为两个独立任务，避免范围过大：

1. `P0-01`: Model3D hierarchy payload + Outliner 面板 + viewport object selection 同步。
2. `P0-02`: 先只交付 wireframe/shaded-wire/normal debug 三个显示模式；不要同时引入 UV stretch、points 和 skeleton weight。

每个任务结束后先用真实 GLB fixture 验证，再进入下一项。

## 7. Deformer 专项紧急修复与控制手柄实施手册

本节覆盖用户当前反馈：“调节任何 Deformer 参数没有任何反应，视图也没有控制手柄”。本节优先级高于新增 FFD、Effector 等功能；在 D0–D4 完成前，禁止继续增加新的 Deformer 类型。

### 7.1 已确认根因和必须修复的行为

| ID | 已确认代码行为 | 为什么用户会看到“完全无反应” | 修复决定 |
|---|---|---|---|
| D0-01 | `Deformer3DNode._deform_flat_meshes()` 对 `is_skinned=True` 直接使用原 mesh | 人物/角色这类常见资产带 skin；参数不会参与几何结果 | 第一版 UI 对 skinned mesh 显示红色诊断“当前版本不支持 skinned deformer”，禁止假装可用；D4 后实现 post-skin GPU/CPU 路径 |
| D0-02 | `Scene3DNode` 收集多个 ModelPayload 后只取第一个有效项渲染 | 若 `Model3D → Scene3D` 与 `Model3D → Deformer3D → Scene3D` 同时存在，原模型可能被优先显示 | 建立并验证单一链路 `Model3D → Deformer3D → Scene3D`；Scene3D 检测同源重复 payload 并告警，后续升级为真正多几何聚合 |
| D0-03 | 当前变形器只有通用 transform gizmo，只有 `center_x/y/z`；没有 angle/radius/strength/falloff 形状手柄 | 用户看不到 Bend/Twist/Bulge 应有的可视化控制器，无法直觉操作参数 | 不再把通用 gizmo 当作 Deformer UI；按 D2/D3 实现专用 `DeformerGizmoRenderer` |
| D0-04 | 现有 Deformer gizmo 注释明确将对象空间近似为世界空间 | Model3D 有 translate/rotate/scale 时，中心手柄位置和实际变形中心不一致，拖动会写入错误坐标 | 所有绘制/拾取都使用 `world_from_local` 与其逆矩阵，禁止再使用“模型在原点”的近似 |
| D0-05 | Deformer 的缓存签名理论上会包含参数，但用户界面没有“本帧执行/跳过/失败”可见状态 | 图拓扑、源 mesh、GL 上传或缓存失效任何一步出错时，用户只能看到无变化 | 添加 `DeformerEvaluationStatus`、日志和 Inspector 状态；失败必须显示原因和 source/mesh 统计 |

### 7.2 D0：先使参数变化成为可观察、可诊断的闭环

#### D0-1 图拓扑和输入规则

1. 规定最小正确节点链：`Model3D.display → Deformer3D.in → Scene3D.geometry_in`。
2. 删除或自动断开同一 Model3D 到同一 Scene3D 的直接连接；第一版不允许“原始 + 变形后”两条同源链并存。
3. `Deformer3DNode` 接收到多个 upstream ModelPayload 时，第一版直接报诊断错误并停止，而不是静默选择第一个；后续多模型功能必须显式合并。
4. Scene3D 收到来自同一 source path/mesh revision 的原始与变形 payload 时，输出 warning：`duplicate model source: use only the deformed chain`。
5. 在 Node Editor 的 Deformer 节点标题下显示状态灯：灰=未连接、黄=等待上游、绿=本帧已变形、红=失败/不支持。

#### D0-2 参数变更、缓存和渲染线程

1. 每个参数变化必须：写入 node parameter → 增加 Deformer content revision → 请求 graph re-evaluate → 让 Scene3D content signature 改变 → 重新渲染。
2. Deformer cache key 至少包含：upstream `mesh.content_hash`、world matrix、skin state、全部 Deformer 参数、Field 参数、selection revision、frame/time、viewport quality。
3. 只允许渲染线程创建/释放 VBO、IBO、VAO；UI 拖拽只发不可变参数快照和 `params_changed` 信号。
4. 每次 cache miss 记录 `input_vertices`、`output_vertices`、耗时、cache hit/miss、是否跳过、异常信息；将最后状态写入 `DeformerEvaluationStatus`。
5. 发生异常时不得静默用原 mesh 替代。保留最后一次有效变形结果，并在 UI 显示错误；只有用户明确关闭节点才回到原 mesh。

建议新增纯数据结构：

```python
@dataclass(frozen=True)
class DeformerEvaluationStatus:
    node_id: str
    source_mesh_count: int
    deformed_mesh_count: int
    input_vertex_count: int
    skipped_reason: str  # "" / "skinned_not_supported" / "no_input" / "upload_failed"
    cache_hit: bool
    evaluation_ms: float
    frame: int
    error_message: str = ""
```

#### D0-3 先写的测试

1. 单元测试：Bend、Twist、Bulge、Taper、Stretch、Noise 每种参数变化都使顶点 hash 改变；`enabled=False` 才允许不变。
2. 节点链集成测试：构造 `Model3D → Deformer3D → Scene3D`，调整 angle/amount/strength 后验证 Scene3D input signature、deformed mesh hash、render revision 三者均变化。
3. 重复链测试：原 Model 与 Deformer output 同时连 Scene3D 必须产生 warning 或拒绝执行，绝不能悄悄渲染其中一个。
4. skinned 测试：当前版本必须得到 `skinned_not_supported` 状态；D4 完成后再改为验证实际顶点变化。
5. GPU offscreen 测试：cube 以 Bend 0° 和 90° 输出的 silhouette/depth 像素必须不同。

### 7.3 D1：定义手柄系统的数据契约

新增文件建议：

| 文件 | 责任 |
|---|---|
| `render3d/deformer_gizmo.py` | 纯 Python：手柄枚举、局部几何描述、参数映射、射线/约束数学；不能依赖 PyQt/ModernGL |
| `render3d/deformer_gizmo_renderer.py` | ModernGL：动态 VBO、line/triangle/ID-pick draw pass、资源 release |
| `ui/deformer_gizmo_controller.py` | PyQt：鼠标状态机、signal、undo transaction、屏幕坐标和 renderer pick result 的转换 |
| `ui/deformer_stack_panel.py` | 选择/排序/开关 Deformer，显示 D0 status；不是 GL 渲染器 |
| `tests/composite/test_deformer_gizmo_math.py` | 坐标转换、hit test、拖拽约束、参数映射 |
| `tests/composite/test_deformer_gizmo_integration.py` | Qt 鼠标交互、参数提交、undo、offscreen render 断言 |

核心数据结构必须显式区分坐标空间：

```python
class DeformerHandleKind(str, Enum):
    CENTER_X = "center_x"; CENTER_Y = "center_y"; CENTER_Z = "center_z"
    BEND_ANGLE = "bend_angle"; TWIST_ANGLE = "twist_angle"
    BULGE_AMOUNT = "bulge_amount"; TAPER_AMOUNT = "taper_amount"
    STRETCH_AMOUNT = "stretch_amount"; NOISE_AMPLITUDE = "noise_amplitude"
    FIELD_RADIUS = "field_radius"; FIELD_SIZE_X = "field_size_x"
    FIELD_SIZE_Y = "field_size_y"; FIELD_SIZE_Z = "field_size_z"

@dataclass(frozen=True)
class DeformerGizmoState:
    node_id: str
    deformer_type: str
    axis: str
    params: Mapping[str, float | str | bool]
    world_from_local: np.ndarray      # 4x4，上游 Model3D 的真实世界变换
    local_from_world: np.ndarray      # world_from_local 的安全逆
    camera_view: np.ndarray
    camera_proj: np.ndarray
    viewport_size_px: tuple[int, int]
    perspective: bool
    selected: bool
```

规则：Deformer 的 `center`、angle、field、mesh vertices 都先在**模型局部空间**计算；只在渲染/拾取时用 `world_from_local` 映射。拖拽鼠标反算出的 world point 必须先乘 `local_from_world`，再写回 `center_x/y/z`。这条规则能消除现有 transform 后“手柄漂移”。

### 7.4 D2：各 Deformer 手柄应如何绘制

公共视觉规则：

1. X/Y/Z 轴使用红/绿/蓝，当前 hover 为黄，active 为白；被遮挡时画半透明虚线，不能完全消失。
2. 手柄以约 10–14 屏幕像素为目标大小，而不是固定世界尺寸。透视相机中：`world_scale = target_px * 2 * distance * tan(fov / 2) / viewport_height`；正交相机中：`world_scale = target_px * ortho_height / viewport_height`。
3. Overlay 在 beauty render 之后绘制两遍：第一遍 depth-test 开启以表达遮挡；第二遍 depth-test 关闭、低 alpha，确保关键控制点可见。绘制结束恢复全部 GL state。
4. 每个可交互图元都必须有稳定 `handle_id`；第一版 CPU hit test 用它维护拖拽状态，后续 ID-pick pass 再以唯一颜色编码它。禁止按绘制数组下标临时猜测。

| 类型 | 可见图元 | 必须可拖拽的手柄 | 参数映射 |
|---|---|---|---|
| 通用 Center | 三轴箭头 + 平面方块 + 中心球 | X/Y/Z 轴和平面 | `center_x/y/z`，写回 local space |
| Bend | 沿主轴的虚线 baseline、作用范围盒、与弯曲平面一致的圆弧 | 圆弧末端的 angle handle；中心 tripod | `angle = signed_angle(start_vector, dragged_vector)`，钳到 -360..360° |
| Twist | 主轴虚线、上下两个圆环、环间螺旋线 | 顶端圆环的 angle handle；中心 tripod | 在垂直于 axis 的平面计算 `atan2`，与 drag-start angle 相减，写 `angle` |
| Bulge | center 的 wire sphere、沿 normal/radial 的双箭头 | sphere 半径 handle、幅度箭头、中心 tripod | sphere handle 改 Field radius；箭头沿 initial normal/radial 的有符号距离改 `strength` 或 amount |
| Taper | 沿主轴的 wire truncated cone/cage，上下两个缩放环 | 顶端环的 X/Z（或对应横截面）方向手柄；中心 tripod | 横截面半径相对 base radius 的比例映射到 `amount`，限制在参数范围 |
| Stretch | 沿主轴的胶囊/box cage、两端平面 | 正负端的轴向 handle | 拖动投影到主轴，长度变化映射到 `amount`；对称拖动保持 center |
| Noise | mesh bounds wire box、幅度箭头、频率刻度线 | 幅度箭头、频率滑块/环、中心 tripod | `amplitude` 是沿 normal 的有符号距离；`frequency` 用对数映射，避免高频区过灵敏 |
| Field（D3） | Linear=双向箭头+两端 plane；Radial=wire sphere；Box=wire box | radius/size 端点和 invert toggle | 写 `FieldSpec.radius/size/center/axis/invert`，不直接改 Deformer 强度 |

各类型的初始尺寸必须来自上游 mesh 的 local bounding box：

1. `bbox_center = (min + max) * 0.5`，`bbox_extent = max(max - min, epsilon)`。
2. 默认 center 为 bbox_center，不得硬编码 `(0, 0, 0)`；默认 axis length 为对应轴 extent。
3. 在没有 upstream mesh 时显示灰色 placeholder，不允许接受拖拽；状态为 `no_input`。
4. 每次 source mesh、world matrix、deformer type、axis 或参数变化都重建 `DeformerGizmoState`，但可缓存静态 line/arc topology。

### 7.5 D3：手柄拾取和拖拽的精确步骤

#### D3-1 拾取

第一版建议采用 CPU 屏幕空间 hit test，原因是手柄数量小、易于测试；大场景/重叠手柄再增加 ID FBO 兜底。

1. 对每个 handle 的 local control point/polyline 乘 `world_from_local`，再乘 `camera_proj @ camera_view`，投影为 widget 像素。
2. point/circle handle：鼠标到屏幕点距离 `<= 10 px` 命中；line/arc：鼠标到每条 projected segment 的最短距离 `<= 8 px` 命中。
3. 若多个命中，按 `active > hover > center point > parameter handle > axis line` 的固定优先级选择；同优先级选屏幕距离最小者。
4. 命中后记录 `DragContext`：handle kind、拖拽起点 mouse ray、初始参数、初始 center、world/local matrices、约束 plane/axis、undo-before snapshot。
5. 没有命中时不修改 Deformer 参数，保持普通 object selection 行为。

#### D3-2 鼠标射线与约束

```text
widget pixel → NDC (x, y, -1, 1)
→ inverse(proj @ view) → world-space ray
→ local_from_world → local-space ray
→ 与本手柄约束轴/平面求交
→ 新 local parameter → clamp → signal
```

1. **轴向 center/length/amount**：求 camera ray 与 local axis 的最近点；取轴参数 `t`，新值为 `initial_value + (t - t_start)`。
2. **平面 center**：ray 与固定 local plane 求交；使用 plane 内两个坐标更新 center。
3. **角度 handle**：ray 与“垂直于 rotation axis、过 center”的 local plane 求交；用 `atan2(dot(p, v), dot(p, u))` 得到角度，减去 start angle，归一化到 `[-180, 180]`。
4. **radius/box size**：同样求 local plane 交点，取到 center 的长度；size 使用绝对值并下限 `epsilon`，radius 下限 `epsilon`。
5. 每次 move 只 emit 参数差异；按 60 Hz 节流 preview。release 时合并为一个 undo command；Escape 恢复 undo-before。
6. Shift=细调（乘 0.1），Ctrl/Cmd=按项目 snap step，Alt=关于 center 对称调整；所有 modifier 的含义写入 tooltip 和测试。

#### D3-3 不允许的实现方式

1. 不能直接用 widget 像素 delta 写 `center_x/y/z`；这会在相机旋转、透视和对象 transform 后错误。
2. 不能从 UI 线程创建 ModernGL 资源或调用 Scene3D render；UI 只更新参数并请求 evaluator。
3. 不能只画手柄而没有 pick/drag test，也不能让不可交互的图元看起来像可拖拽手柄。
4. 不能在拖动时原地修改 source `MeshData`；始终由 Deformer output/cache 生成新结果。

### 7.6 D4：Skinned Deformer 的阶段化实施

1. **D4-A（立即）**：保持禁用，但明确显示 `skinned_not_supported`，显示受影响 mesh 数和原因；不要静默透传。
2. **D4-B（正确性优先）**：实现 post-skin CPU prototype：先计算当前骨骼姿态顶点，再按 local/world 明确约定执行 deformer，最后重算 normal/tangent。只允许小 mesh，作为真值参考。
3. **D4-C（性能）**：实现 GL 4.1 可用的 GPU vertex deformation。推荐在 skinning vertex shader 后执行 deformer 函数，或用 transform feedback 生成 deformed VBO；不得依赖 macOS 不支持的 compute shader。
4. **D4-D（验收）**：带 normal map 的 100-bone GLB，在 bind pose 和播放动画中 Bend/Twist 均可见；骨架 overlay、depth、normal、object-ID 与最终几何一致。

### 7.7 Deformer 交付拆分和验收顺序

| 任务卡 | 范围 | 完成条件 |
|---|---|---|
| D0 | 数据流、状态、单一节点链、skinned 明示诊断 | 参数改变确实改变 static cube 的 mesh hash 和 offscreen silhouette；不支持状态用户可见 |
| D1 | `DeformerGizmoState`、坐标变换、通用 center tripod | Model3D 平移/旋转/缩放后，center 拖拽仍写正确 local 坐标；undo/redo 正确 |
| D2 | Bend 与 Twist 专用绘制、CPU hit test | 圆弧/圆环可见且准确跟随局部轴；拖 angle 可立即改变模型 |
| D3 | Bulge/Taper/Stretch/Noise、Field 手柄与 modifier | 所有可见手柄可拾取、拖动、取消、undo；无 GL state 泄漏 |
| D4 | skinned post-skin/GPU 路径、normal/tangent | 真实角色资产变形稳定，性能基准达标 |

每张任务卡都必须同时交付：数据层单测、Qt 交互测试、一个 offscreen GPU 测试、一个真实静态 GLB（D4 再加真实 skinned GLB）验收资产。没有这四项，不得标记 Deformer 完成。
