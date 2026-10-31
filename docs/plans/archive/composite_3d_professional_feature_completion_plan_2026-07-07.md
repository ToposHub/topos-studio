# Topos Color Composite 3D 专业功能补全总纲

日期：2026-07-07  
前置计划：`docs/composite_3d_viewport_performance_master_plan_2026-07-07.md`  
目标：在完成 3D viewport 性能优化之后，系统性补齐影视专业级 3D 合成软件所需能力，让 Composite 3D 模块从“能导入和预览模型”升级为“可用于镜头级 3D 合成、资产检查、材质调整、动画预览和专业交付”的完整系统。

---

## 0. 当前状态判断

### 0.1 简短结论

| 功能 | 当前状态 | 结论 |
|---|---|---|
| 蒙皮 Skinning | 有 `SkinData`、skinned shader、bone matrix 上传雏形 | **未达到可用级** |
| 骨骼 Skeleton | 数据结构不完整；没有骨架视图、骨骼层级 UI、骨骼调试 | **基本缺失** |
| 动画 Animation | 有 `AnimationClip` 和采样函数；Model3D 有 anim 参数 | **只算雏形** |
| FBX 骨骼/动画 | 当前 pyassimp 对用户 FBX 会 NULL pointer；CLI fallback 静态化 | **不支持可靠 FBX 动画** |
| glTF 骨骼/动画 | trimesh metadata best effort，覆盖率有限 | **不可靠** |
| 材质导入 | 已支持 baseColor/normal/ORM/emissive 贴图提取和绑定 | **基础可用** |
| 材质编辑 | 只有少量参数和 texture override 端口 | **不具备专业材质编辑器** |
| 点/线/面显示 | 只有 component mode 状态雏形；无真实点线面 overlay | **缺失** |
| 点/线/面选择 | 无 GPU picking / component selection / selection set | **缺失** |
| Wireframe / shaded wire | 没有完整 viewport 显示模式 | **缺失** |
| 法线 / 切线显示 | MeshData 能算 tangent，但导入模型 VAO 未接 tangent；无 overlay | **缺失** |
| 非破坏变形器 / 效果器 | 没有 Deformer Stack、Field、Falloff、Effector 节点 | **缺失** |
| 摄影机 / 灯光导入 | 目前用户 FBX 无 camera/light；系统也未完整导入外部 camera/light | **缺失** |
| 专业渲染通道 | color/depth 有基础；normal/position/object id/material id/motion vector 缺 | **大部分缺失** |
| 资产检查工具 | 无 polygon count panel、UV viewer、texture inspector、scene hierarchy | **缺失** |

### 0.2 定位

当前 3D 系统已经具备：

1. `Scene3D / Model3D / Geo3D / Camera3D / Light3D` 节点基础。
2. FBX/OBJ/GLB 静态模型导入。
3. 基础 PBR 材质贴图显示。
4. 四视图和基础 gizmo。
5. 单位、坐标轴、UV V 翻转、`.fbm` 贴图提取。

但距离影视专业级 3D 合成软件还缺：

1. **资产真实还原**：骨骼、动画、材质、相机、灯光、层级都必须可靠读取。
2. **专业检查视图**：wireframe、点线面、UV、法线、切线、骨架、权重、通道视图。
3. **镜头合成输出**：AOV、deep/depth、object ID、motion vector、shadow catcher、holdout。
4. **可编辑性**：材质编辑、动画片段控制、对象/组件选择、transform/pivot、层级管理。
5. **可交付性**：缓存、代理、版本化、路径重链、诊断、错误提示、批量验证。

---

## 1. 最终目标：影视专业级 3D 合成能力

### 1.1 对标对象

| 软件 | 需要学习的能力 |
|---|---|
| Nuke 3D / ScanlineRender | 3D 合成节点图、Camera/Light/Geometry/Scene 渲染、AOV/depth |
| Fusion 3D | 节点化 3D 场景、材质、Renderer3D、多 pass |
| Cinema 4D | 四视图、对象层级、材质管理、组件显示、坐标/单位/轴 |
| Blender | 资产导入、骨骼动画、材质节点、viewport overlays |
| Unreal Engine | 高性能 viewport、材质实例、LOD、资产缓存、实时反馈 |
| Maya | 骨架、蒙皮、动画曲线、变形器、组件编辑 |

### 1.2 专业级完成标准

当以下条件满足时，可以认为 Composite 3D 功能达到专业级基础：

1. FBX / GLB / OBJ / USD 基础资产能可靠导入。
2. 模型层级、transform、材质、贴图、相机、灯光能还原。
3. 骨骼蒙皮模型能显示正确姿态。
4. 动画片段可播放、暂停、循环、裁剪、重定时。
5. Viewport 有 solid / textured / wireframe / shaded wire / normals / UV / skeleton 等显示模式。
6. 支持对象选择、点/边/面选择、outline、transform gizmo。
7. 支持材质编辑，包括 PBR 参数、贴图槽、颜色空间、normal 格式、UV set、材质 override。
8. 支持非破坏式 Deformer Stack：弯曲、扭曲、FFD、膨胀、噪波、包裹、碰撞、Field/Falloff 控制。
9. 支持渲染 AOV：beauty、depth、normal、position、UV、object ID、material ID、motion vector、mask。
10. 支持 shadow catcher、holdout、matte object、object mask 输出。
11. 大模型/动画资产在性能优化计划完成后仍保持流畅。

---

## 2. 功能路线总览

| 阶段 | 名称 | 目标 | 前置 |
|---|---|---|---|
| F0 | 资产能力审计与基线 | 明确当前导入资产能保留什么 | 性能 P0 |
| F1 | Scene Hierarchy / Outliner | 有对象树、mesh、material、skeleton、camera、light 列表 | 性能 P1 |
| F2 | Mesh 显示模式 | solid、wireframe、shaded wire、points、normals、UV debug | 性能 P1/P2 |
| F3 | 对象与组件选择 | object / point / edge / polygon 选择、outline、selection set | 性能 P2 |
| F4 | 材质编辑系统 | PBR 材质面板、贴图槽、材质 override、材质库 | 性能 P3 |
| F5 | Tangent / Normal Map 完整化 | 导入模型 tangent VAO、normal map 正确显示 | 性能 P3 |
| F6 | 骨架 / 蒙皮 | skeleton 导入、骨骼显示、GPU skinning 稳定 | 性能 P4/P5 |
| F7 | 动画系统 | clip 列表、播放控制、重定时、缓存、motion sample | 性能 P5 |
| F8 | Camera / Light 导入与匹配 | 外部相机、灯光、FOV、焦距、裁剪面、曝光 | 性能 P5 |
| F9 | 专业 AOV / Render Pass | depth/normal/position/id/uv/motion vector/matte | 性能 P6 |
| F10 | Shadow Catcher / Holdout | 影视合成必备遮挡、阴影、matte workflow | F9 |
| F11 | USD / Alembic / 资产交换 | 镜头级资产交换和缓存格式 | F6/F7 |
| F12 | 专业 UI / 工作流 | Inspector、UV viewer、Material editor、Animation panel | 全阶段 |
| F13 | 非破坏 Deformer / Effector | 像 C4D 一样通过变形器改变形状，不破坏原模型 | F2/F3/F4 |

---

## 3. F0：资产能力审计与基线

### 3.1 目标

在继续补功能前，先明确导入器对不同格式的实际保真度。不能只看“模型显示了”，要知道：

1. mesh 是否完整。
2. UV 是否正确。
3. material slot 是否正确。
4. texture 是否可找到。
5. skeleton 是否存在。
6. skin weights 是否存在。
7. animation clips 是否存在。
8. camera/light 是否存在。
9. coordinate/unit 是否正确。
10. embedded/external textures 是否路径稳定。

### 3.2 实施任务

| 编号 | 任务 | 文件 | 验收 |
|---|---|---|---|
| F0-1 | 新增 `ModelImportReport` | `render3d/model_import_report.py` | 每次导入生成结构化报告 |
| F0-2 | 报告 mesh/material/texture/skeleton/animation 数量 | `model_loader.py` | UI 可显示 |
| F0-3 | 记录丢失项和 fallback 原因 | `model_loader.py` | pyassimp 失败、CLI fallback 明确 |
| F0-4 | 测试资产矩阵 | `tests/assets/3d/` 或外部本地路径 | FBX/GLB/OBJ 样例覆盖 |
| F0-5 | 资产验收表 | `docs/benchmarks/3d_import/` | 每个格式有报告 |

### 3.3 输出示例

```text
ModelImportReport
- source_path
- loader_backend: pyassimp / assimp_cli_glb / trimesh / future_sdk
- mesh_count
- triangle_count
- material_count
- texture_count
- embedded_textures_extracted
- skeleton_count
- bone_count
- skin_weight_count
- animation_clip_count
- camera_count
- light_count
- warnings
- lost_features
```

---

## 4. F1：Scene Hierarchy / Outliner

### 4.1 目标

专业 3D 软件必须让用户看到导入资产内部结构：

1. root node。
2. transform nodes。
3. mesh nodes。
4. material assignments。
5. skeleton / bones。
6. cameras。
7. lights。
8. animation clips。

### 4.2 实施任务

| 编号 | 功能 | 文件 | 说明 |
|---|---|---|---|
| F1-1 | 扩展 SceneGraphRoot metadata | `scene_graph.py` | cameras/lights/skeleton/material table |
| F1-2 | Model3D Outliner payload | `scene_payload.py` | 输出层级摘要 |
| F1-3 | UI Outliner 面板 | `ui/model3d_outliner_panel.py` | tree view 显示层级 |
| F1-4 | 节点选中同步 | `composite_page.py`, `composite_viewport.py` | outliner 选中同步 viewport |
| F1-5 | 显隐/solo/lock | `Model3DNode` 参数或 viewport state | 先 viewport-only，再可持久化 |
| F1-6 | 统计信息 | Outliner footer | tris、materials、textures、bones |

### 4.3 验收

1. 导入人物 FBX 后能看到 mesh 名称。
2. 如果文件有 bones，能看到 skeleton tree。
3. 如果文件有 camera/light，能看到并可选择。
4. outliner 选中对象后 viewport 有 outline。

---

## 5. F2：Mesh 显示模式

### 5.1 目标

补齐专业 viewport 常用显示：

| 模式 | 用途 |
|---|---|
| Solid | 快速检查形体 |
| Textured | 查看材质 |
| Wireframe | 查看拓扑 |
| Shaded Wire | 带材质/灰模叠加线框 |
| Points | 查看顶点 |
| Edges | 查看边 |
| Normals | 查看法线方向 |
| Tangents | 调试 normal map |
| UV Stretch | 查看 UV 拉伸 |
| Bounds | 大模型快速显示 |
| Skeleton | 查看骨架 |
| Weights | 查看蒙皮权重 |

### 5.2 实施任务

| 编号 | 任务 | 文件 | 说明 |
|---|---|---|---|
| F2-1 | `ViewportDisplayMode` enum | `viewport3d_state.py` | solid/textured/wire/shaded_wire/points |
| F2-2 | 线框渲染 pass | `scene3d_node.py` 或 `viewport3d_renderer.py` | OpenGL polygon mode 或 edge buffer |
| F2-3 | Edge buffer 生成 | `primitives.py`, `model_loader.py` | 从 indices 生成 unique edges |
| F2-4 | Point cloud overlay | `viewport_overlays.py` | 顶点点显示 |
| F2-5 | Normal/Tangent overlay | `viewport_overlays.py` | 线段 overlay |
| F2-6 | UV debug shader | `shaders.py` | `color = vec3(uv, 0.0)` |
| F2-7 | UV stretch heatmap | `render3d/uv_diagnostics.py` | 面积比热力图 |
| F2-8 | Skeleton overlay | `skeleton_renderer.py` | 骨骼线段 |

### 5.3 风险

1. OpenGL polygon mode 在多 pass 下状态容易泄漏，必须严格恢复。
2. Shaded wire 需要 depth bias，避免 z-fighting。
3. Edge buffer 对大模型可能很大，应缓存。

---

## 6. F3：对象与点线面选择

### 6.1 目标

实现专业的选择系统：

1. Object mode：选择 mesh/object。
2. Point mode：选择顶点。
3. Edge mode：选择边。
4. Polygon mode：选择面。
5. 框选、加选、减选、反选。
6. selection outline / highlight。
7. selection set 可保存。

### 6.2 技术方案

| 方案 | 用途 | 说明 |
|---|---|---|
| GPU ID buffer | object / face picking | 渲染 id 到 integer/encoded color buffer |
| CPU raycast BVH | 精确点线面选择 | 大模型需 BVH |
| Screen-space selection | 框选组件 | 投影到屏幕后判断 |
| Selection overlay | 高亮显示 | 单独 overlay pass |

### 6.3 实施任务

| 编号 | 任务 | 文件 | 说明 |
|---|---|---|---|
| F3-1 | Object ID pass | `viewport3d_renderer.py` / `scene3d_node.py` | 每对象唯一 id |
| F3-2 | Face ID pass | `shaders.py` | face id 需要额外 buffer 或 draw range |
| F3-3 | Mesh BVH | `render3d/mesh_bvh.py` | CPU raycast 加速 |
| F3-4 | Component selection model | `render3d/selection.py` | selected vertices/edges/faces |
| F3-5 | Selection overlay renderer | `viewport_overlays.py` | 高亮点线面 |
| F3-6 | UI component mode toolbar | `viewport3d_toolbar.py` | object/point/edge/polygon |
| F3-7 | Transform target resolution | `viewport3d_controller.py` | 对象/组件不同 transform |

### 6.4 验收

1. 点击人物模型能选中对象。
2. 切到 polygon mode 点击可选中面。
3. wireframe 下可看到选中的边/点/面。
4. 选择不影响最终 Scene3D 输出，只影响 viewport overlay。

---

## 7. F4：专业材质编辑系统

### 7.1 当前状态

当前 Model3D 支持：

1. 导入 baseColor / normal / ORM / AO / emissive 贴图。
2. `material_mode`：imported / override / mix。
3. `normal_format`：OpenGL / DirectX。
4. `texture_filter` / `texture_wrap`。
5. 少量 emissive intensity 参数。

但专业材质编辑还缺：

1. 材质列表。
2. per-material 参数编辑。
3. 贴图槽 UI。
4. 贴图颜色空间。
5. normal/roughness/metallic 通道 remap。
6. 多 UV set。
7. 材质预设和材质库。
8. 材质 override 节点化。

### 7.2 目标材质模型

| 材质字段 | 说明 |
|---|---|
| base_color | RGB + texture |
| metallic | scalar + texture/channel |
| roughness | scalar + texture/channel |
| specular / IOR | 高级 PBR |
| normal | texture + format + strength |
| bump | texture + height scale |
| displacement | texture + scale |
| ao | texture/channel |
| emissive | color + texture + intensity |
| opacity | scalar + texture + alpha mode |
| transmission | 透明/玻璃 |
| clearcoat | 高级材质 |
| sheen | 布料 |
| subsurface | 皮肤/蜡 |
| two_sided | 双面 |
| cull_mode | back/front/none |
| uv_set | 贴图使用 UV |
| tiling/offset/rotation | 贴图变换 |

### 7.3 实施任务

| 编号 | 任务 | 文件 | 说明 |
|---|---|---|---|
| F4-1 | MaterialData 扩展 | `scene_graph.py` | 增加完整 PBR 字段 |
| F4-2 | Material table | `SceneGraphRoot` | 独立材质数组，mesh 引用 material id |
| F4-3 | Material editor panel | `ui/material3d_panel.py` | per-material UI |
| F4-4 | Texture slot widget | `ui/widgets/texture_slot.py` | 显示路径/颜色空间/channel |
| F4-5 | Channel packing UI | material panel | roughness=G、metallic=B 等 |
| F4-6 | UV transform | shader + MaterialData | offset/scale/rotation |
| F4-7 | Material override node | `Material3DNode` | 节点化材质 |
| F4-8 | Material library | `assets/materials/` 或 user library | 预设 |
| F4-9 | Material assignment | outliner/viewport | 给 mesh 分配材质 |

### 7.4 验收

1. 用户能在 UI 里看到模型所有材质。
2. 修改某个材质颜色只影响对应 mesh。
3. 替换 baseColor 贴图后 viewport 更新。
4. 调 roughness/metallic 能实时反馈。
5. 支持 normal strength 和 DirectX/OpenGL 切换。

---

## 8. F5：Tangent / Normal Map 完整化

### 8.1 当前状态

当前导入模型 normal map 已提取，但因为导入模型 VAO 还没有 tangent attribute，渲染时暂时关闭 normal map 采样。

### 8.2 目标

1. 导入模型生成 tangents。
2. VBO layout 支持 tangent。
3. skinned mesh 支持 tangent。
4. normal map 正确显示。
5. 支持 normal strength 和 OpenGL/DirectX。

### 8.3 实施任务

| 编号 | 任务 | 文件 | 验收 |
|---|---|---|---|
| F5-1 | MeshData tangent 持久化 | `primitives.py` | tangent 不只是临时 compute |
| F5-2 | Model3D 上传 tangent VAO | `model3d_node.py` | layout `3f 3f 2f 3f` |
| F5-3 | Skinned tangent layout | `model3d_node.py`, shaders | bone ids/weights + tangent |
| F5-4 | Scene3D 正确启用 normal map | `scene3d_node.py` | `u_has_normal_map` 可为 1 |
| F5-5 | Tangent debug overlay | `viewport_overlays.py` | 可显示 tangent |
| F5-6 | normal strength shader | `shaders.py` | 强度可调 |

### 8.4 验收

1. 带 normal map 的模型不再发黑或错光。
2. DirectX normal 翻转正确。
3. 四视图和透视视图都一致。

---

## 9. F6：骨架与蒙皮

### 9.1 目标

让带骨骼的角色模型能：

1. 正确显示 bind pose。
2. 正确显示当前动画姿态。
3. 显示骨架。
4. 查看每根骨骼名称和层级。
5. 查看 skin weights。
6. GPU skinning 稳定。

### 9.2 需要补的核心数据

| 数据 | 当前风险 | 需要 |
|---|---|---|
| bone hierarchy | 不完整 | parent/children/rest matrix/local/world |
| inverse bind matrices | 有字段但提取不可靠 | 从可靠 loader 获取 |
| vertex joint indices | 有字段但提取不可靠 | 支持 uint/int attribute |
| vertex weights | 有字段但提取不可靠 | normalize + top4 influences |
| skeleton root | 缺 | skin 与 skeleton 关联 |
| bind pose / rest pose | 缺 UI | 可显示/重置 |
| bone names | 有字段 | 需要唯一化和映射 |

### 9.3 Loader 选择

| 格式 | 短期 | 中期 | 长期 |
|---|---|---|---|
| glTF/GLB | 使用 pygltflib 或 tinygltf 风格解析 | 完整支持 skin/animation | 推荐作为标准格式 |
| FBX | 当前 pyassimp 不可靠 | 调研 Blender headless / Autodesk FBX SDK / ufbx | 推荐 ufbx 或 Blender 转换 |
| USD | 后续引入 | USD Python API | 镜头级交换 |
| Alembic | 后续引入 | Alembic cache | 动画几何缓存 |

### 9.4 实施任务

| 编号 | 任务 | 文件 | 说明 |
|---|---|---|---|
| F6-1 | SkeletonData 数据结构 | `scene_graph.py` | bone hierarchy/rest pose |
| F6-2 | SkinData 重构 | `scene_graph.py` | skin 引用 skeleton |
| F6-3 | glTF skin 正式解析 | `model_loader_gltf.py` | 不依赖 trimesh metadata |
| F6-4 | FBX skin 可靠路径调研 | docs + spike | ufbx / Blender / FBX SDK |
| F6-5 | GPU skinning 完整测试 | `scene3d_node.py`, shaders | 多骨骼、超过 64 骨骼策略 |
| F6-6 | Skeleton overlay | `skeleton_renderer.py` | bones 线段显示 |
| F6-7 | Weight display mode | `shaders.py` | 按骨骼显示权重热力图 |
| F6-8 | Skeleton outliner | `model3d_outliner_panel.py` | 骨骼树 |

### 9.5 验收

1. glTF skinned character 正确显示 bind pose。
2. 动画播放时角色不爆炸、不扭曲。
3. 可切换 skeleton overlay。
4. 选择骨骼后可显示受影响权重。

---

## 10. F7：动画系统

### 10.1 目标

支持专业动画预览和镜头级控制：

1. clip 列表。
2. clip 选择。
3. 播放 / 暂停 / 循环。
4. 起止帧裁剪。
5. speed / time offset。
6. time remap。
7. root motion 处理。
8. animation cache。
9. motion vector 输出。

### 10.2 动画类型

| 类型 | 说明 |
|---|---|
| node transform animation | object/bone transform |
| skeletal animation | joints driving skinned mesh |
| morph target animation | blendshape |
| camera animation | camera transform/FOV |
| light animation | light transform/intensity |
| visibility animation | object visibility |
| material animation | material parameters |
| Alembic geometry cache | per-frame geometry |

### 10.3 实施任务

| 编号 | 任务 | 文件 | 说明 |
|---|---|---|---|
| F7-1 | AnimationClip 数据扩展 | `scene_graph.py` | channels 类型更完整 |
| F7-2 | clip list UI | `model3d_node.py` panel | 下拉选择动画 |
| F7-3 | timeline frame 映射 | `Model3DNode` | comp frame -> anim time |
| F7-4 | loop / pingpong / once | Model3D 参数 | 播放模式 |
| F7-5 | animation cache | `render3d/animation_cache.py` | 采样结果缓存 |
| F7-6 | morph target support | loader + shader | 后续 |
| F7-7 | camera/light animation import | loader + payload | 和 F8 联动 |
| F7-8 | motion vector data | Scene3D pass | 和 F9 联动 |

### 10.4 验收

1. 多 clip 文件能列出 clip 名称。
2. 可选择 clip 并播放。
3. timeline 拖动时动画帧正确。
4. loop/speed/time offset 生效。

---

## 11. F8：Camera / Light 导入与匹配

### 11.1 目标

影视合成中，外部 3D 软件导出的相机和灯光非常关键。需要支持：

1. 导入 camera transform。
2. 导入 focal length / FOV / aperture / sensor size。
3. 导入 near/far clipping。
4. 导入 camera animation。
5. 导入 directional/point/spot/area light。
6. 导入 light color/intensity/range/cone。
7. 创建对应 Camera3D / Light3D 节点或作为 Model3D 内部 scene item。

### 11.2 实施任务

| 编号 | 任务 | 文件 | 说明 |
|---|---|---|---|
| F8-1 | CameraData / LightData | `scene_graph.py` | 结构化相机/灯光 |
| F8-2 | loader 提取 camera/light | `model_loader_*` | glTF/FBX |
| F8-3 | 拖拽导入时按需创建节点 | `node_editor_actions.py` | 文件有 camera/light 才创建 |
| F8-4 | camera matching | `camera3d_node.py` | focal/sensor/FOV |
| F8-5 | light unit conversion | `light3d_node.py` | intensity 单位策略 |
| F8-6 | animation import | F7 联动 | camera/light 动画 |

### 11.3 验收

1. 带相机的 glTF/FBX 导入后可生成 Camera3D。
2. 视角与原软件基本匹配。
3. 文件无相机/灯光时不创建多余节点。

---

## 12. F9：专业 AOV / Render Pass

### 12.1 目标

专业 3D 合成不能只有 beauty。需要输出多种 pass：

| Pass | 用途 |
|---|---|
| beauty | 主图 |
| depth / z | 景深、深度合成 |
| normal | relight、边缘处理 |
| position / world position | 3D mask、重投影 |
| UV | 贴图调试、后期替换 |
| object ID | 对象 matte |
| material ID | 材质 matte |
| motion vector | 运动模糊 |
| diffuse direct | 分层合成 |
| diffuse indirect | 分层合成 |
| specular direct | 分层合成 |
| specular indirect | 分层合成 |
| emission | 发光 |
| ambient occlusion | AO |
| shadow | 阴影合成 |
| reflection/refraction | 高级 |

### 12.2 实施任务

| 编号 | 任务 | 文件 | 说明 |
|---|---|---|---|
| F9-1 | Scene3D 多输出端口 | `scene3d_node.py` | beauty/depth/normal/id 等 |
| F9-2 | AOV shader variants | `shaders.py` | normal/position/uv/id |
| F9-3 | FBO 多附件策略 | `scene3d_node.py` | MRT 或多 pass |
| F9-4 | object/material id 分配 | `scene_payload.py` | 稳定 id |
| F9-5 | AOV viewer selector | UI | 选择查看 pass |
| F9-6 | output node 支持 AOV | downstream | 合成节点读取 |

### 12.3 验收

1. Scene3D 可输出 depth/normal/object id。
2. 用户可在 viewer 切换 pass。
3. object id 可用于生成 matte。

---

## 13. F10：Shadow Catcher / Holdout / Matte Workflow

### 13.1 目标

影视合成中经常需要：

1. CG 模型投影到实拍地面。
2. 实拍物体遮挡 CG。
3. 某些 3D 对象只产生 matte，不显示 beauty。
4. 输出阴影 pass 和遮挡 pass。

### 13.2 功能

| 功能 | 说明 |
|---|---|
| Shadow Catcher | 接收阴影但自身透明 |
| Holdout Object | 遮挡后方 CG |
| Matte Object | 输出 mask/id |
| Reflection Catcher | 接收反射 |
| Contact Shadow | 轻量实时接触阴影 |
| Ground Plane Helper | 快速创建地面 catcher |

### 13.3 实施任务

| 编号 | 任务 | 文件 |
|---|---|---|
| F10-1 | Object render flags | `scene_payload.py` |
| F10-2 | Shadow pass | `scene3d_node.py`, `shaders.py` |
| F10-3 | Catcher material | `MaterialData` |
| F10-4 | Holdout depth/matte pass | Scene3D |
| F10-5 | UI flags | outliner/material panel |
| F10-6 | Merge workflow docs | docs |

---

## 14. F11：USD / Alembic / 高级资产交换

### 14.1 目标

FBX 对影视流水线不够稳定。专业级需要逐步支持：

1. USD / USDA / USDC / USDZ。
2. Alembic `.abc`。
3. glTF/GLB 完整。
4. OBJ 静态基础。
5. FBX 尽力，但不作为长期唯一核心。

### 14.2 格式定位

| 格式 | 定位 |
|---|---|
| glTF/GLB | 实时 PBR 和静态/骨骼资产主力 |
| USD | 镜头级场景交换、层级、材质、动画 |
| Alembic | 几何缓存、变形动画 |
| FBX | DCC 互通，但需可靠解析方案 |
| OBJ | 静态 mesh fallback |

### 14.3 实施任务

| 编号 | 任务 | 说明 |
|---|---|---|
| F11-1 | 格式能力矩阵 | 每种格式支持哪些 feature |
| F11-2 | USD loader spike | 调研 USD Python API |
| F11-3 | Alembic loader spike | 调研可用 Python/CLI |
| F11-4 | 导入器插件接口 | loader backend 可插拔 |
| F11-5 | 场景单位/轴/时间码统一 | 格式间一致 |
| F11-6 | asset relink | 贴图/缓存路径重链 |

---

## 15. F12：专业 UI / 工作流

### 15.1 需要的面板

| 面板 | 功能 |
|---|---|
| 3D Outliner | 层级、显隐、锁定、solo、选择 |
| Material Editor | 材质参数、贴图槽、预设 |
| Texture Inspector | 查看贴图、颜色空间、分辨率、路径 |
| UV Viewer | 查看 UV layout、选中面对应 UV |
| Animation Panel | clip 列表、播放、范围、速度 |
| Skeleton Panel | 骨骼树、显示、权重 |
| AOV Panel | pass 开关、输出端口 |
| Import Report Panel | 导入警告、丢失项、cache 状态 |
| Performance Panel | FPS、draw calls、triangles、binds |

### 15.2 Viewport 工具栏

| 工具 | 状态 |
|---|---|
| Select | 必须 |
| Move / Rotate / Scale | 必须 |
| Object / Point / Edge / Polygon | 必须 |
| Axis / Pivot edit | 必须 |
| Snap / Quantize | 必须 |
| Display mode | 必须 |
| Shading mode | 必须 |
| Frame selected | 必须 |
| Isolate selected | 推荐 |
| Show skeleton / normals / UV | 推荐 |

---

## 16. F13：非破坏 Deformer / Effector 节点系统

### 16.1 为什么这对 3D 合成很重要

C4D 的 Deformer 思路非常适合 Composite 3D：它不直接修改原始模型的点、线、面和贴图，而是在渲染和预览阶段通过一个可开关、可排序、可动画、可限制作用范围的变形栈改变形状。

对合成而言，这类功能价值很高：

1. 资产不用回到 DCC 软件重改。
2. 镜头里可以快速做轮廓修正、比例修正、穿帮修正。
3. 可以给静态模型增加运动感、呼吸感、风吹感、冲击感。
4. 可以用 mask、falloff、field 只影响局部区域。
5. 可以和时间线、表达式、音频、粒子、图像 mask 联动。
6. 可以在不破坏 UV 和材质的前提下调整外形。

### 16.2 核心设计原则

| 原则 | 说明 |
|---|---|
| 非破坏 | 原始 MeshData 不被改写，变形结果来自运行时 evaluate |
| 可叠加 | 一个 Model3D 可以挂多个 deformer，按顺序执行 |
| 可开关 | 每个 deformer 有 enable、strength、visibility |
| 可限制 | 支持 selection set、vertex group、weight map、texture mask、field |
| 可动画 | 所有关键参数都能随 comp timeline 动画 |
| 可缓存 | 静态 deformer 可缓存结果，动态 deformer 按帧缓存 |
| 可降级 | viewport 操作时可用低分辨率 proxy 或 bounds 预览 |
| 材质安全 | UV、material slot、texture binding 默认保持不变 |

### 16.3 Deformer Stack 数据流

```text
Model3D source mesh
  -> unit / axis correction
  -> imported animation / skinning base pose
  -> Deformer Stack
      -> Bend
      -> Twist
      -> FFD
      -> Noise
      -> Collision
      -> Custom Field
  -> viewport LOD / proxy
  -> material binding
  -> Scene3D render passes
```

关键点：

1. Deformer 不应该破坏模型源数据。
2. Deformer 的结果需要进入 viewport、Scene3D、AOV、object id、shadow catcher。
3. 对 skinned mesh，要明确执行顺序：通常是 skeleton skinning 后再做 object-space deformer，或者支持 pre-skin / post-skin 两种模式。
4. 对 tangent / normal map，要在形变后更新 normal，必要时重建 tangent 或使用近似 normal matrix。

### 16.4 节点类型规划

| 节点 | 类似 C4D 功能 | 合成用途 | 优先级 |
|---|---|---|---|
| `DeformerStack3D` | Deformer 列表容器 | 管理一个模型上的多个变形器 | P0 |
| `Bend3D` | Bend | 弯曲物体、匹配弧形透视、修正道具姿态 | P0 |
| `Twist3D` | Twist | 扭转布料、肢体、能量物体、抽象元素 | P0 |
| `Taper3D` | Taper | 局部变粗变细，快速修比例 | P0 |
| `Bulge3D` | Bulge | 膨胀、呼吸、肌肉感、冲击鼓包 | P0 |
| `Stretch3D` | Stretch | 夸张拉伸、速度感、镜头变形 | P0 |
| `Shear3D` | Shear | 透视修正、风格化倾斜、镜头匹配 | P1 |
| `Spherify3D` | Spherify | 球化、圆润化、广告包装效果 | P1 |
| `FFD3D` | FFD lattice | 影视合成最实用的局部修形 | P1 |
| `NoiseDeform3D` | Displacer / Noise | 风吹、布料扰动、能量波、表面抖动 | P1 |
| `Displace3D` | Displacer | 使用贴图或程序纹理置换形状 | P1 |
| `ShrinkWrap3D` | Shrink Wrap | 让模型贴合目标表面、投影包装 | P2 |
| `CollisionDeform3D` | Collision | 压痕、接触变形、简单挤压 | P2 |
| `Melt3D` | Melt | 融化、下垂、破坏感 | P2 |
| `Jiggle3D` | Jiggle | 二级运动、弹性抖动、软体感 | P2 |
| `SmoothDeform3D` | Smoothing | 柔化扫描/低质模型局部 | P2 |
| `CorrectiveShape3D` | Pose morph / corrective | 镜头级修形和表情补正 | P3 |

### 16.5 合成最常用效果器节点

这些不是纯几何变形器，而是用于驱动 transform、材质、可见性、权重或 deformer 强度的 Effector / Field 节点。

| 效果器 | 用途 | 可驱动内容 | 优先级 |
|---|---|---|---|
| `RandomEffector3D` | 随机位置、旋转、缩放、颜色、强度 | transform/material/deformer strength | P0 |
| `StepEffector3D` | 按序号递增变化 | 阵列、克隆、分层动画 | P0 |
| `PlainEffector3D` | 固定偏移和统一控制 | 批量调整 transform 或参数 | P0 |
| `TimeEffector3D` | 按时间做循环/波动/延迟 | animation time、strength、visibility | P0 |
| `DelayEffector3D` | 缓入缓出、拖尾、弹性 | transform/deformer response | P1 |
| `FormulaEffector3D` | sin/cos/noise 表达式驱动 | 任何数值参数 | P1 |
| `ShaderField3D` | 用图像或程序纹理当权重 | deformer mask/material/visibility | P1 |
| `LinearField3D` | 线性范围控制 | deformer falloff、effector weight | P0 |
| `RadialField3D` | 球形/圆形范围控制 | 局部膨胀、冲击波、遮罩 | P0 |
| `BoxField3D` | 盒体范围控制 | 局部修形、区域控制 | P0 |
| `SplineField3D` | 曲线范围控制 | 沿路径弯曲、扫描、流动 | P2 |
| `VertexWeightField3D` | 顶点权重控制 | 精确局部影响 | P2 |
| `SelectionField3D` | 选择集控制 | 点/边/面选择驱动 | P2 |
| `CameraDistanceField3D` | 按镜头距离控制 | LOD、透明、效果强度 | P2 |
| `ObjectDistanceField3D` | 按目标物距离控制 | 碰撞前效果、接近触发 | P2 |

### 16.6 最小可用版本

第一版不要一口气做完整 C4D。建议先做 6 个最实用、最容易验证的节点：

1. `DeformerStack3D`：挂载和排序 deformer。
2. `Bend3D`：最常用，也最能证明非破坏形变链路。
3. `Twist3D`：验证轴向、坐标系、falloff。
4. `Bulge3D`：验证局部膨胀和 mask。
5. `NoiseDeform3D`：验证时间动画和程序噪波。
6. `LinearField3D / RadialField3D`：验证局部影响范围。

### 16.7 实施任务

| 编号 | 任务 | 文件 | 验收 |
|---|---|---|---|
| F13-1 | Deformer 基类 | `render3d/deformers/base.py` | 统一 evaluate 接口 |
| F13-2 | DeformerStack 数据结构 | `scene_graph.py` / `scene_payload.py` | Model3D 可挂多个 deformer |
| F13-3 | CPU deformer prototype | `render3d/deformers/cpu.py` | Bend/Twist/Bulge 可运行 |
| F13-4 | GPU deformer shader path | `shaders.py` / `scene3d_node.py` | 大模型 viewport 不阻塞 |
| F13-5 | Deformer node UI | `nodes/deformer3d_node.py` | 参数面板、enable、strength |
| F13-6 | Stack UI | `ui/deformer_stack_panel.py` | 排序、开关、删除、solo |
| F13-7 | Field / Falloff 数据结构 | `render3d/fields.py` | linear/radial/box falloff |
| F13-8 | Weight / mask 绑定 | material/texture loader | 贴图 mask 控制强度 |
| F13-9 | Deformed mesh cache | `render3d/deformer_cache.py` | 静态缓存、动态按帧缓存 |
| F13-10 | Normal 更新策略 | `primitives.py` / shader | 形变后光照正常 |
| F13-11 | Selection set 联动 | `render3d/selection.py` | 只影响选中点/面 |
| F13-12 | AOV 联动 | `scene3d_node.py` | deformed geometry 输出 depth/normal/id |
| F13-13 | 测试资产 | `tests/assets/3d/deformers/` | 基础 cube/character/cloth proxy |
| F13-14 | 性能基准 | `docs/benchmarks/3d_deformers/` | 10 万/50 万/100 万面耗时 |

### 16.8 Deformer 参数规范

每个 Deformer 节点至少应有：

| 参数 | 说明 |
|---|---|
| enable | 是否启用 |
| strength | 总强度 |
| space | object / world / local axis |
| axis | X / Y / Z |
| center | 变形中心 |
| size / range | 作用范围 |
| falloff | none / linear / smooth / radial / custom |
| weight source | none / selection / vertex group / texture / field |
| time mode | static / animated / procedural |
| viewport quality | full / proxy / bounds |

### 16.9 与节点图的连接方式

推荐两种同时支持：

1. **Model3D 内部 Deformer Stack**：最像 C4D，适合用户选中模型后添加变形器。
2. **显式节点链路**：`Model3D -> Bend3D -> NoiseDeform3D -> Scene3D`，适合合成节点图用户清楚控制数据流。

最终行为：

1. 拖拽模型进来，默认没有变形器。
2. 用户右键模型或节点面板添加 Bend/Twist/FFD。
3. Deformer 不改变原始模型文件。
4. 关闭 Deformer 后模型回到原始形状。
5. 导出/渲染时按当前 Deformer Stack 输出最终结果。

### 16.10 验收

1. 同一个 FBX/GLB 模型添加 Bend 后，材质和 UV 不错位。
2. 关闭 Bend 后模型恢复原始形状。
3. Bend + Twist + Noise 可按顺序叠加，排序变化会改变结果。
4. Linear/Radial Field 可以限制局部范围。
5. 变形后的模型在 perspective、top、front、right 四视图都一致。
6. 变形后的 depth/normal/object id pass 正确。
7. 50 万面模型使用 proxy 或 GPU path 时 viewport 仍可交互。

---

## 17. 推荐执行顺序

性能优化完成后，建议按以下顺序开始，不要一上来就做最难的骨骼动画：

### 第一轮：先把专业检查能力补齐

1. F0：资产能力审计与导入报告。
2. F1：Scene hierarchy / outliner。
3. F2：wireframe、points、normals、UV debug。
4. F3：object picking 和 outline。

原因：这些功能能立刻帮助判断“模型到底导入对不对”，也是后续骨骼/材质/动画调试的基础。

### 第二轮：材质系统专业化

1. F4：Material editor。
2. F5：Tangent / normal map。
3. Texture inspector / UV viewer。

原因：材质问题是用户最容易感知的，也是 3D 合成最常用的调整入口。

### 第三轮：非破坏变形器和效果器

1. F13：DeformerStack3D。
2. Bend3D / Twist3D / Bulge3D。
3. NoiseDeform3D。
4. LinearField3D / RadialField3D。
5. Deformer cache 和 viewport proxy。

原因：变形器对后期合成非常实用，可以在不回 DCC、不破坏资产的情况下快速修形、做局部动态、匹配镜头。它比完整骨骼动画更容易先落地，也能反过来验证 mesh cache、normal 更新和大模型性能。

### 第四轮：骨骼与动画

1. F6：Skeleton / Skinning。
2. F7：Animation clips。
3. F8：Camera / Light 导入和动画。

原因：骨骼动画牵涉 loader、数据结构、shader、UI、timeline，必须在性能和调试视图稳定后做。

### 第五轮：影视合成输出

1. F9：AOV。
2. F10：Shadow catcher / holdout。
3. F11：USD / Alembic。

原因：这些是从“3D 预览工具”迈向“影视 3D 合成系统”的关键。

---

## 18. 验收总矩阵

| 能力 | 最小验收 | 专业验收 |
|---|---|---|
| 静态模型 | OBJ/FBX/GLB 正确显示 | 层级/材质/贴图/单位/轴完整 |
| 材质 | baseColor/roughness/metallic 可调 | 完整 PBR + 贴图槽 + 材质库 |
| UV | textured 正确 | UV viewer + stretch debug + per UV set |
| Wireframe | 显示线框 | shaded wire + component highlight |
| 点线面 | 显示组件 | 可选择/编辑/transform |
| 变形器 | Bend/Twist/Bulge 可用 | Deformer Stack + Field + cache + AOV 联动 |
| 效果器 | Random/Time/Field 基础驱动 | 类 C4D procedural control，可驱动 transform/material/deformer |
| 骨骼 | 显示 skeleton | GPU skinning + 权重查看 |
| 动画 | 播放 clip | 多 clip / retime / motion vectors |
| 相机 | 手动 Camera3D | 导入匹配 DCC 相机动画 |
| 灯光 | Light3D 基础 | 导入灯光 + 类型/单位匹配 |
| AOV | depth/normal/id | 完整 beauty 分层 + motion vector |
| 合成 | beauty merge | shadow catcher / holdout / matte |
| 性能 | 50 万面可操作 | 百万面 + LOD + viewport cache |

---

## 19. 关键风险

| 风险 | 说明 | 应对 |
|---|---|---|
| FBX SDK/pyassimp 不可靠 | 用户当前 FBX 已触发 NULL pointer | 调研 ufbx/Blender/USD/glTF 主路径 |
| 骨骼动画复杂度高 | 数据结构、shader、UI、timeline 都受影响 | 先 glTF，再 FBX；先 skeleton，再 animation |
| 材质系统膨胀 | 节点化材质会复杂 | 先 per-material editor，再 Material3DNode |
| Deformer 性能 | CPU 逐顶点形变会拖慢大模型 | GPU path + static cache + proxy + LOD |
| Deformer 顺序复杂 | skinning、morph、deformer、normal 更新顺序会影响结果 | 明确 pre-skin/post-skin/object-space 三种模式 |
| Field 系统膨胀 | Field、mask、selection、texture 都会扩展复杂度 | 先 linear/radial，再 selection/texture/custom |
| 组件选择性能 | 大模型点线面 picking 昂贵 | GPU ID + BVH + LOD |
| AOV 多 pass 性能 | pass 越多越慢 | 和性能计划的 profile/cache 联动 |
| OpenGL 4.1 限制 | macOS compute 不可用 | 使用 GL 3.3/4.1 可行方案 |
| UI 复杂度 | 面板过多会混乱 | 先 Outliner + Material + Import Report 三个核心面板 |

---

## 20. 下一步建议

等 viewport 性能计划完成后，第一步不要直接冲骨骼动画。建议先做：

1. `ModelImportReport`：把每个模型到底导入了什么说清楚。
2. `3D Outliner`：能看到 mesh/material/skeleton/camera/light。
3. `Wireframe / Points / Normals / UV Debug`：能检查模型拓扑和 UV。
4. `Object Picking + Outline`：能专业地选择对象。
5. `DeformerStack3D + Bend3D`：验证非破坏形变链路。

这些完成后，再进入材质编辑、tangent/normal map、更多 Deformer/Effector、骨骼蒙皮和动画。这样后续遇到 FBX 或 glTF 资产问题时，系统自己就能告诉我们“丢了什么、错在哪里”，不会再靠截图猜。

---

## 21. AI 落地实施手册

本节是给 AI 编码代理使用的具体执行方案。以后可以把任意任务卡直接交给 AI，例如：“实现 F13-1 到 F13-3，严格按照本计划，不改无关代码，完成后跑对应测试。”

### 21.1 全局实施规则

每个任务都按以下顺序执行：

1. 先读相关文件，不凭空重写架构。
2. 先补数据结构和单元测试，再接 UI 和 OpenGL 渲染。
3. 所有核心数据结构放在 `src/features/composite/render3d/`，保持纯 Python / numpy，不依赖 PyQt 和 ModernGL。
4. 所有节点类放在 `src/features/composite/nodes/`。
5. 所有 viewport 状态和交互放在 `src/features/composite/ui/`。
6. `scene3d_node.py` 只做渲染聚合和 GL 资源管理，不塞资产解析逻辑。
7. loader 只负责把外部资产转成内部 `SceneGraphRoot`，不创建 UI 节点。
8. 所有 GL 对象只能在渲染线程或已有 GL 上下文路径里创建。
9. 先支持 glTF / GLB 的完整路径，再处理 FBX 的不稳定路径。
10. 每个阶段必须有最小测试，避免只靠人工截图判断。

### 21.2 推荐给 AI 的通用 Prompt

```text
你正在 Topos Color 项目中实现 Composite 3D 专业功能。
技术栈：PyQt6 + ModernGL + numpy + GLSL。
请严格遵守项目现有架构，不做无关重构，不回退用户改动。

目标任务：[填写 F 编号和任务名]
参考计划：docs/composite_3d_professional_feature_completion_plan_2026-07-07.md
相关文件：[填写本任务卡列出的文件]

要求：
1. 先读相关文件，列出影响范围。
2. 先写或更新测试。
3. 实现最小可用功能。
4. 保持数据结构纯净，GL 资源不进入可序列化数据。
5. 完成后运行指定 pytest。
6. 输出变更摘要、测试结果、已知限制。
```

### 21.3 F0 资产能力审计：具体实现方法

目标：每次导入模型后生成结构化报告，让用户知道哪些内容被成功读取，哪些内容丢失。

新增文件：

| 文件 | 用途 |
|---|---|
| `src/features/composite/render3d/model_import_report.py` | 定义导入报告数据结构 |
| `tests/composite/test_model_import_report.py` | 单测报告合并、warning、lost feature |

修改文件：

| 文件 | 修改点 |
|---|---|
| `src/features/composite/render3d/model_loader.py` | loader 返回或附带 `ModelImportReport` |
| `src/features/composite/render3d/scene_graph.py` | `SceneGraphRoot` 增加 `import_report` 可选字段 |
| `src/features/composite/nodes/model3d_node.py` | Model3D 参数面板显示报告摘要 |

建议数据结构：

```python
@dataclass
class ModelImportReport:
    source_path: str
    loader_backend: str
    mesh_count: int = 0
    triangle_count: int = 0
    material_count: int = 0
    texture_count: int = 0
    embedded_textures_extracted: int = 0
    skeleton_count: int = 0
    bone_count: int = 0
    skin_weight_count: int = 0
    animation_clip_count: int = 0
    camera_count: int = 0
    light_count: int = 0
    unit_scale: float = 1.0
    axis_mode: str = "auto"
    warnings: list[str] = field(default_factory=list)
    lost_features: list[str] = field(default_factory=list)
```

实现步骤：

1. 在 `model_loader.py` 的每个 backend 分支创建 report。
2. pyassimp 成功时记录 mesh/material/texture/skeleton/animation 数量。
3. assimp CLI fallback 时写入 `loader_backend="assimp_cli_glb"`，并把丢失的 bones/animation/camera/light 写入 `lost_features`。
4. `.fbm` 提取成功时记录 `embedded_textures_extracted`。
5. `SceneGraphRoot` 上挂 `import_report`，由 `Model3DNode` 传到 UI。
6. 失败时不要只抛异常，要把 fallback 原因写入 report warning。

验收测试：

```bash
python -m pytest tests/composite/test_model_import_report.py tests/composite/test_scene_graph.py
```

### 21.4 F1 Scene Hierarchy / Outliner：具体实现方法

目标：用户能看到导入模型内部结构，包括 mesh、material、camera、light、skeleton。

新增文件：

| 文件 | 用途 |
|---|---|
| `src/features/composite/render3d/scene_hierarchy.py` | 把 `SceneGraphRoot` 转成轻量 tree summary |
| `src/features/composite/ui/model3d_outliner_panel.py` | Outliner 面板 |
| `tests/composite/test_scene_hierarchy.py` | 层级摘要测试 |

修改文件：

| 文件 | 修改点 |
|---|---|
| `scene_graph.py` | `SceneNode` 增加 stable id、node type、visibility metadata |
| `scene_payload.py` | `ModelPayload` 增加 hierarchy summary |
| `model3d_node.py` | 产出 hierarchy payload |
| `composite_viewport.py` | 选中对象后显示 outline |

实现方法：

1. 给 `SceneNode` 增加 `node_id: str`，用导入路径 + 节点路径生成稳定 id。
2. 新增 `SceneHierarchyItem`，字段为 `id/name/type/children/mesh_count/material_name/visible/locked/bounds`。
3. loader 保持原始层级；不要在 loader 里直接扁平化丢失层级。
4. UI 用 `QTreeView` 或现有树控件显示 hierarchy summary。
5. Outliner 选中 item 后，把 `selected_scene_object_id` 写入 viewport state。
6. Scene3D 渲染时如果 object id 匹配 selected id，额外渲染 outline pass。

最小验收：

1. 导入多 mesh 模型后能看到多个 mesh item。
2. 点击 Outliner item，viewport 只高亮对应 mesh。
3. hide/solo/lock 先作为 viewport-only 状态，不立即写回模型文件。

### 21.5 F2 Mesh 显示模式：具体实现方法

目标：补齐 solid、textured、wireframe、shaded wire、points、normals、UV debug。

新增文件：

| 文件 | 用途 |
|---|---|
| `src/features/composite/render3d/mesh_diagnostics.py` | edge buffer、normal lines、UV stretch |
| `tests/composite/test_mesh_display_modes.py` | buffer 生成和状态测试 |

修改文件：

| 文件 | 修改点 |
|---|---|
| `viewport3d_state.py` | 增加 `display_mode`、`overlay_flags` |
| `viewport3d_toolbar.py` | 增加显示模式按钮 |
| `viewport_overlays.py` | 绘制点、边、法线、切线、骨架 |
| `scene3d_node.py` | 根据 display mode 选择 shader/pass |
| `shaders.py` | 增加 solid、uv debug、id、wire overlay shader |

实现方法：

1. `ViewportDisplayMode = textured / solid / wireframe / shaded_wire / points / uv_debug / normal_debug`。
2. `mesh_diagnostics.build_unique_edges(indices)` 从三角形索引生成去重 edge list。
3. wireframe 最小版用 edge buffer + GL lines，不依赖 `glPolygonMode`，避免状态泄漏。
4. shaded wire 先正常 textured pass，再叠加 edge lines，使用 depth test 和轻微 depth bias。
5. points pass 用顶点 buffer draw points，点大小由 viewport 参数控制。
6. normal/tangent overlay 用 CPU 生成 line segments，缓存到 mesh content hash。
7. UV debug shader 输出 `vec3(uv.x, uv.y, 0.0)`。

测试重点：

1. edge 去重结果稳定。
2. 不同 display mode 进入 Scene3D content signature。
3. 切换模式不会重建不必要的模型导入缓存。

### 21.6 F3 对象与点线面选择：具体实现方法

目标：支持 object / point / edge / polygon 选择，后续给变形器、材质分配、局部编辑使用。

新增文件：

| 文件 | 用途 |
|---|---|
| `src/features/composite/render3d/selection.py` | SelectionState、SelectionSet、component ids |
| `src/features/composite/render3d/mesh_bvh.py` | CPU raycast BVH |
| `tests/composite/test_3d_selection.py` | 选择状态和 raycast 测试 |

修改文件：

| 文件 | 修改点 |
|---|---|
| `viewport3d_state.py` | 增加 component mode、selection state |
| `viewport3d_controller.py` | mouse click/drag 转 picking request |
| `scene3d_node.py` | 增加 object id / face id pass |
| `shaders.py` | 增加 id encode shader |

实现方法：

1. Object picking 第一版使用 GPU ID buffer：每个 draw call 写入 object id。
2. Face picking 第一版可以按 mesh draw range 或 CPU raycast；大模型再引入 BVH。
3. Point/edge picking 用 screen-space 距离判断：把候选点/边投影到屏幕，找最近项。
4. SelectionState 存 `selected_object_ids`、`selected_vertex_ids`、`selected_edge_ids`、`selected_face_ids`。
5. Shift 加选、Alt/Option 减选、空白点击清选。
6. selection overlay 独立 pass 绘制高亮点线面，避免污染材质 shader。

验收：

1. 点击模型能选中 object。
2. 切到 polygon mode 能选中单个面并高亮。
3. 选择集能被 Deformer 的 `SelectionField3D` 使用。

### 21.7 F4 材质编辑系统：具体实现方法

目标：从“导入后显示贴图”升级为“可检查、可替换、可覆盖、可保存预设”的材质系统。

新增文件：

| 文件 | 用途 |
|---|---|
| `src/features/composite/render3d/material_model.py` | 完整 PBR 材质 schema |
| `src/features/composite/ui/material3d_panel.py` | 材质编辑面板 |
| `src/features/composite/ui/widgets/texture_slot_widget.py` | 贴图槽控件 |
| `src/features/composite/nodes/material3d_node.py` | 节点化材质 override |
| `tests/composite/test_material3d_model.py` | 材质序列化和 override 测试 |

实现方法：

1. 保留 `scene_graph.MaterialData`，但逐步扩展为完整 schema。
2. 材质字段包括：base color、metallic、roughness、specular、ior、alpha、emissive、normal strength、uv transform。
3. TextureDescriptor 增加 `color_space`、`wrap_s/t`、`filter`、`channel_mapping`、`uv_set`。
4. Material editor 选择当前 mesh 的 material，修改后生成 material override，不改原始导入材质。
5. `Material3DNode` 可连接到 `Model3D` 或 `Scene3D`，按 material id/name 覆盖参数。
6. 贴图路径使用绝对路径存运行时，保存工程时走项目路径重链策略。
7. 所有贴图槽显示：路径、分辨率、颜色空间、是否丢失、role。

验收：

1. 替换 baseColor 贴图后 viewport 和 Scene3D 一致。
2. 调整 roughness/metallic 后材质明显变化。
3. 工程保存再打开，material override 不丢。

### 21.8 F5 Tangent / Normal Map：具体实现方法

目标：让 normal map 真正进入导入模型渲染路径。

修改文件：

| 文件 | 修改点 |
|---|---|
| `primitives.py` | `MeshData` 持久化 tangent，支持 skinned tangent layout |
| `model3d_node.py` | 上传带 tangent 的 VAO |
| `scene3d_node.py` | normal map sampling 开关 |
| `shaders.py` | TBN normal mapping |

实现方法：

1. 当 material 有 normal texture 时，调用 `mesh.compute_tangents()`。
2. VBO layout 从 `3f 3f 2f` 升级为可选 `3f 3f 2f 3f`。
3. shader 中构建 TBN：`T = normalize(mat3(model) * tangent)`，`N = normalize(mat3(model) * normal)`，`B = normalize(cross(N, T))`。
4. normal texture 读取后从 `[0,1]` 转到 `[-1,1]`。
5. 增加 `normal_strength`，默认 1。
6. 如果没有 tangent，normal map 自动禁用并写入 import report warning。

验收：

1. 同一模型 normal map 开关前后光照细节明显变化。
2. tangent debug overlay 显示方向。
3. 没有 UV 的模型不会崩溃。

### 21.9 F6 骨架与蒙皮：具体实现方法

目标：让带骨骼权重的模型能以正确姿态显示，并能显示骨架。

新增文件：

| 文件 | 用途 |
|---|---|
| `src/features/composite/render3d/skeleton.py` | SkeletonData、BoneData、pose 计算 |
| `src/features/composite/render3d/skinning.py` | CPU/GPU skinning helper |
| `src/features/composite/render3d/model_loader_gltf.py` | glTF skin 正式解析 |
| `tests/composite/test_skeleton_skinning.py` | 骨骼矩阵和权重测试 |

实现方法：

1. 先以 glTF/GLB 为主路径实现，不先攻 FBX。
2. 定义 `BoneData(name, parent_index, local_bind, inverse_bind, rest_world)`。
3. 定义 `SkeletonData(bones, root_bone_index)`，和 `SkinData` 分离。
4. loader 读取 joints、inverseBindMatrices、weights、joint indices。
5. 每帧计算 `bone_matrix = current_world @ inverse_bind_matrix`。
6. shader 上传 `u_bone_matrices[MAX_BONES]`；超过 64 根骨骼先分批或 fallback CPU skinning。
7. Skeleton overlay 用 bone current world position 连父节点。
8. Weight display mode 读取 vertex bone weight，显示热力图。

FBX 策略：

1. 短期：FBX 继续走静态 fallback，但 report 明确写 lost bones/animation。
2. 中期：调研 `ufbx`、Blender background conversion、FBX SDK 三条路线。
3. 长期：推荐用户动画资产转 GLB/USD/Alembic 作为稳定主路径。

### 21.10 F7 动画系统：具体实现方法

目标：支持 clip 列表、播放、循环、重定时、缓存，并驱动 skeleton/camera/light/deformer。

新增文件：

| 文件 | 用途 |
|---|---|
| `src/features/composite/render3d/animation.py` | clip sampling、插值、pose 输出 |
| `src/features/composite/render3d/animation_cache.py` | 按 clip/time 缓存 |
| `tests/composite/test_3d_animation.py` | TRS 插值和 loop 测试 |

实现方法：

1. AnimationChannel 支持 path：translation / rotation / scale / weights / camera / light。
2. rotation 使用 quaternion slerp，不用线性插值。
3. Model3D 参数增加 `animation_clip`、`animation_time_mode`、`speed`、`loop_mode`、`frame_offset`。
4. comp frame 转动画时间：`time = (frame - in_frame) / fps * speed + offset`。
5. loop mode：once / loop / pingpong / clamp。
6. animation cache key 包含 model asset hash、clip name、sample time、deformer stack signature。
7. 动画先更新 scene node transforms，再 flatten，再 skinning，再 deformer。

验收：

1. GLB 动画模型可播放。
2. 切换 clip 后姿态变化正确。
3. 暂停时不重复采样动画。

### 21.11 F8 Camera / Light 导入与匹配：具体实现方法

目标：外部文件有 camera/light 时才创建或显示对应 Camera3D/Light3D，不存在时不自动创建。

新增/修改：

| 文件 | 修改点 |
|---|---|
| `scene_graph.py` | 增加 `CameraData`、`LightData` |
| `model_loader.py` | 提取 camera/light 数据 |
| `node_editor_actions.py` 或拖拽入口 | 按导入报告决定是否创建节点 |
| `camera3d_node.py` | focal length、sensor、lens shift、clip range |
| `light3d_node.py` | 类型、颜色、强度、单位转换 |

实现方法：

1. loader 读取 camera/light 到 `SceneGraphRoot.cameras/lights`。
2. 拖拽导入时：如果 report camera_count > 0，才创建 Camera3D；如果 light_count > 0，才创建 Light3D。
3. 相机匹配优先使用 focal length + sensor size；没有则使用 FOV。
4. 灯光类型映射 directional / point / spot / area，其中 area 第一版可 fallback 为 point/spot 并警告。
5. 导入动画相机时，Camera3D 通过 AnimationClip 驱动。

验收：

1. 没有 camera/light 的模型只创建 Model3D。
2. 有 camera/light 的测试 GLB 能显示 camera/light 列表。
3. 导入相机 FOV 与 DCC 画面匹配。

### 21.12 F9 AOV / Render Pass：具体实现方法

目标：Scene3D 不只输出 beauty，还输出合成必需的 depth、normal、position、uv、id、motion vector。

新增/修改：

| 文件 | 修改点 |
|---|---|
| `scene3d_node.py` | 多输出端口、多 FBO attachment 或多 pass |
| `shaders.py` | AOV shader variants |
| `scene_payload.py` | object/material stable id |
| `output_node.py` / 下游节点 | 支持读取 AOV |

实现方法：

1. 第一版使用多 pass，比 MRT 更容易调试。
2. Beauty pass 保持现有路径。
3. Depth pass 输出 linear depth，范围由 camera near/far 控制。
4. Normal pass 输出 world normal encode 到 `[0,1]`。
5. Position pass 输出 world position，HDR float texture。
6. Object ID / Material ID 使用 stable integer id encode 到 RGBA。
7. Motion vector 需要上一帧 world/proj/view 矩阵，先在动画稳定后实现。
8. Scene3D 节点 UI 增加 AOV 开关，未开启不渲染对应 pass。

验收：

1. Scene3D 节点有 beauty/depth/normal/id 输出端口。
2. 下游 Shuffle/Viewer 能查看 AOV。
3. deformed/skinned geometry 的 AOV 和 beauty 对齐。

### 21.13 F10 Shadow Catcher / Holdout：具体实现方法

目标：支持影视合成常用的接影、遮挡、matte workflow。

实现方法：

1. 在 `SceneObjectRenderFlags` 中增加：visible_to_camera、visible_to_shadow、holdout、shadow_catcher、matte_id。
2. Shadow catcher 材质只输出阴影贡献和 alpha，不输出自身 diffuse。
3. Holdout object 写 depth/matte，beauty 中扣 alpha。
4. Matte pass 按 object id/material id 输出 mask。
5. Outliner 和 material panel 都能设置这些 flags。

验收：

1. 平面接住模型阴影，但平面本身在 beauty 中可透明。
2. holdout 物体能遮住后方模型并输出 matte。
3. object mask 可用于下游 Grade/Blur/Keyer。

### 21.14 F11 USD / Alembic / 资产交换：具体实现方法

目标：给专业镜头资产交换留出稳定扩展接口。

实现方法：

1. 抽象 `ModelLoaderBackend`：`can_load(path)`、`load(path, options) -> SceneGraphRoot`。
2. 现有 pyassimp/trimesh/assimp_cli 都包装成 backend。
3. 新增 backend registry，按格式和优先级选择。
4. USD 第一阶段只做调研和静态 mesh/camera import。
5. Alembic 第一阶段只做 mesh cache 和 camera cache，不做材质。
6. 所有 backend 都必须返回 `ModelImportReport`。

验收：

1. loader backend 可插拔。
2. FBX fallback 不影响 GLB 主路径。
3. report 显示具体 backend 和丢失功能。

### 21.15 F12 专业 UI / 工作流：具体实现方法

目标：把功能聚合成专业但不混乱的 UI。

实现方法：

1. 面板优先级：Import Report、Outliner、Material Editor、Deformer Stack、Animation Panel、AOV Panel。
2. 每个面板都只编辑当前选中 Model3D 或 Scene3D，不做全局隐式状态。
3. Viewport toolbar 放常用显示/选择/transform，不放长说明文字。
4. 所有复杂参数放 Inspector/Panel。
5. 错误和丢失功能在 Import Report 面板显示，不弹一堆模态框。
6. 面板状态进入项目文件，viewport 临时选择状态可不持久化。

验收：

1. 用户不用打开日志也能知道模型丢了哪些贴图/动画/骨骼。
2. 用户能在一个位置管理材质、变形器、动画。
3. UI 不遮挡 viewport 操作。

### 21.16 F13 Deformer / Effector：具体实现方法

目标：实现 C4D 风格非破坏变形器和合成常用效果器。

新增文件：

| 文件 | 用途 |
|---|---|
| `src/features/composite/render3d/deformers/__init__.py` | deformer package |
| `src/features/composite/render3d/deformers/base.py` | DeformerSpec、DeformerResult、基类 |
| `src/features/composite/render3d/deformers/cpu.py` | Bend/Twist/Bulge/Noise CPU 实现 |
| `src/features/composite/render3d/deformers/cache.py` | deformed mesh cache |
| `src/features/composite/render3d/fields.py` | Linear/Radial/Box/Texture field |
| `src/features/composite/nodes/deformer3d_node.py` | 通用 Deformer 节点 |
| `src/features/composite/ui/deformer_stack_panel.py` | stack UI |
| `tests/composite/test_3d_deformers.py` | 形变数学和 stack 测试 |

核心数据结构：

```python
@dataclass
class FieldSpec:
    field_type: str
    center: np.ndarray
    axis: np.ndarray
    radius: float = 1.0
    size: np.ndarray = field(default_factory=lambda: np.ones(3, dtype=np.float32))
    falloff: str = "smooth"
    invert: bool = False

@dataclass
class DeformerSpec:
    deformer_type: str
    enabled: bool = True
    strength: float = 1.0
    space: str = "object"
    axis: str = "y"
    center: np.ndarray = field(default_factory=lambda: np.zeros(3, dtype=np.float32))
    params: dict[str, float] = field(default_factory=dict)
    field: FieldSpec | None = None
    selection_set_id: str = ""

@dataclass
class DeformerStack:
    deformers: list[DeformerSpec] = field(default_factory=list)
```

执行顺序：

```text
source MeshData
  -> imported animation transform
  -> skinning or morph base pose
  -> deformer stack
  -> normal/tangent update
  -> VBO upload/cache
  -> render passes
```

CPU Bend 算法：

1. 选择主轴，例如 Y。
2. 对每个顶点计算相对 center 的轴向距离 `t`。
3. 根据 angle 和 height 计算弯曲角 `theta = t / height * angle * weight`。
4. 在垂直平面中旋转顶点。
5. normal 用同样近似旋转，或形变后重算法线。

CPU Twist 算法：

1. 沿 axis 计算高度参数 `t`。
2. `theta = t * twist_angle * weight`。
3. 围绕 axis 旋转横截面坐标。

CPU Bulge 算法：

1. 计算顶点到 center 的距离。
2. field 给出 weight。
3. 沿 normal 或 radial direction 推出：`p += dir * strength * weight`。

NoiseDeform 算法：

1. 使用 deterministic noise，输入为 position + time。
2. 输出沿 normal 或指定 direction 位移。
3. cache key 必须包含 time 和 noise seed。

Field 权重：

1. Linear field：按点在轴向的位置映射 0-1。
2. Radial field：按距离 center 的半径映射 0-1。
3. Box field：盒内 1，盒外按 smooth falloff 衰减。
4. Selection field：选中顶点/面为 1，其余 0。
5. Texture field：使用 UV 采样灰度贴图作为 weight。

缓存策略：

1. 静态 deformer：key = mesh content hash + deformer stack signature。
2. 动态 deformer：key = mesh hash + stack signature + frame/time。
3. viewport 操作中优先用 proxy mesh；鼠标松开后计算 full mesh。
4. 如果顶点数超过阈值，CPU path 自动降级为 proxy 或提示需要 GPU path。

GPU path：

1. 第一版可以只做 CPU，保证正确性。
2. 第二版把 Bend/Twist/Bulge/Noise 写进 vertex shader。
3. GPU deformer 适合预览，但如果后续 pass 需要 deformed position/id/depth，必须保证所有 pass 使用同一 deformer uniform。
4. 如果需要 CPU picking 变形后几何，仍需 CPU cache 或 GPU readback 替代方案。

节点连接：

1. 内部 stack 模式：Model3D 参数里持有 DeformerStack。
2. 显式节点模式：`Model3D -> Bend3D -> Twist3D -> Scene3D`。
3. 两种模式最后都转成同一个 `DeformerStack`，不要实现两套执行逻辑。

最小实现顺序：

1. `base.py` + `fields.py` + 单测。
2. CPU `Bend3D`，支持 strength/axis/center/range。
3. `DeformerStack` 串联多个 deformer。
4. `ModelPayload` 增加 deformer stack signature。
5. Scene3D 在上传 VBO 前获取 deformed mesh。
6. 加 `Twist3D` 和 `Bulge3D`。
7. 加 `LinearField3D` 和 `RadialField3D`。
8. 加 stack UI。
9. 加 cache 和 proxy。

验收命令：

```bash
python -m pytest tests/composite/test_3d_deformers.py tests/composite/test_scene_payload.py tests/composite/test_3d_phase7_performance.py
```

### 21.17 阶段提交建议

建议 AI 每完成一个小阶段就提交，不要攒成巨型提交。

| 提交 | 内容 |
|---|---|
| commit 1 | F0 ImportReport 数据结构和 loader report |
| commit 2 | F1 hierarchy summary 和 Outliner 基础 |
| commit 3 | F2 display modes 和 edge/point overlay |
| commit 4 | F3 object picking 和 selection state |
| commit 5 | F4 material editor 数据层 |
| commit 6 | F5 tangent/normal map 渲染 |
| commit 7 | F13 deformer base + Bend CPU |
| commit 8 | F13 Twist/Bulge/Field/cache |
| commit 9 | F6 skeleton/skinning glTF |
| commit 10 | F7 animation sampling |
| commit 11 | F8 camera/light import |
| commit 12 | F9/F10 AOV + shadow catcher |

### 21.18 每次任务完成后的自检清单

AI 完成任一任务后必须检查：

1. 是否误改了无关文件。
2. 是否引入 PyQt/ModernGL 到纯数据模块。
3. 是否有 GL 对象进入 dataclass signature 或项目序列化。
4. 是否给大模型路径增加了缓存或降级策略。
5. 是否给 UI 改动补了最小状态测试。
6. 是否更新 import report 或 warning，让用户知道功能限制。
7. 是否跑了对应 pytest。
8. 是否记录了已知限制。

### 21.19 优先推荐的第一批 AI 任务

性能计划完成后，第一批最适合交给 AI 的任务：

1. F0-1 到 F0-5：导入报告。风险低，收益高。
2. F2-1 到 F2-4：显示模式、wire、points。能立刻改善模型检查能力。
3. F13-1 到 F13-3：Deformer 基类、Field、CPU Bend。能验证非破坏形变方向。
4. F4-1 到 F4-4：材质数据层和贴图槽 UI。能继续解决材质可控性。
5. F5-1 到 F5-4：tangent 和 normal map。补齐当前材质显示短板。

不建议一开始就交给 AI 的任务：

1. 完整 FBX 骨骼动画导入。
2. USD/Alembic 全功能。
3. 完整 GPU deformer compute pipeline。
4. 完整 motion vector。
5. 节点化材质图。

这些任务牵涉外部格式、渲染架构和性能路径，应该等前面的调试视图、报告、缓存稳定后再做。
