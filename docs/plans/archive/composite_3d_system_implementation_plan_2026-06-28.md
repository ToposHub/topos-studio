# Topos Color — 3D 合成系统实施计划

> **日期**：2026-06-28
> **状态**：阶段一 ✅ / 阶段二 ✅ / 阶段三-六待实施
> **目标**：在 composite 节点图中加入真实 3D 合成能力（对齐 Nuke ScanlineRender / Element 3D），在现有 ModernGL 栈内实现，输出 RGBA + 深度纹理流入下游合成节点。

## 核心决策（已确认）

1. **单节点起步**——用一个 `Scene3D` 大节点内聚场景（相机/灯光/几何/材质都是其参数）
2. **程序化几何先行**——第一版用内置 cube/plane/grid/sphere 跑通管线，glTF/FBX 放后期
3. **交互**——C4D 风格 Alt+三键相机导航 + 基础移动 Gizmo
4. **渲染栈**——ModernGL（OpenGL），**不引入 WebGPU**

## 渲染栈选型：为什么是 ModernGL 而非 WebGPU

经代码调研确认，composite 的整套渲染管线基于 **ModernGL**：
- 节点求值在渲染线程 `moderngl.Context` 内完成（`composite_renderer.py:282`）
- 纹理以 OpenGL glo 直传，零拷贝（`base_node.py:76` `TextureResult.texture_id`）
- 资源池 `GPUResourcePool`、FBO `FramebufferManager`、Shader 库 `ShaderLibrary` 都为 OpenGL 设计

引入 wgpu-py (WebGPU) 会破坏纹理共享（WebGPU 纹理无法变成 OpenGL 纹理喂给下游），**因此 3D 系统在现有 ModernGL context 内实现**。

## 架构核心：Scene3D 节点 = "带持久 GL 资源的源节点"

仿现有 `ConstantNode`（`src/features/composite/nodes/constant_node.py`）的"自管 GL 资源源节点"模式：
- `action_type="source"`，由 evaluator 的 `_sync_render_nodes` 维护持久实例（`base_evaluator.py:241-255`）
- `_do_evaluate` 内执行真实 mesh draw call（不走 marker/effect 分发）
- `owns_color_texture=False`（避免 CacheManager 双重释放，同 ConstantNode）
- 程序/VBO/VAO 懒建并缓存于实例（`_ensure_*` 模式）
- `requires_frame_number=True`（相机可挂动画，换帧需重渲染）

---

## 阶段一：Scene3D 节点骨架 + 程序化几何渲染 ✅ 已完成

**目标**：节点图出现 Scene3D 节点，加载立方体，输出 HDR 图到下游 Merge，viewer 看到画面。

### 新建文件（已完成）
1. ✅ `src/features/composite/render3d/__init__.py` — 包初始化
2. ✅ `src/features/composite/render3d/math_utils.py` — numpy 矩阵库（perspective/look_at/translate/rotate/scale/compose/matrix_to_tuple/orbit_to_eye）
3. ✅ `src/features/composite/render3d/primitives.py` — 程序化几何（cube/plane/grid/sphere + MeshData + get_primitive）
4. ✅ `src/features/composite/nodes/scene3d_node.py` — Scene3DNode 节点本体

### 修改文件（已完成）
5. ✅ `src/features/composite/core/shaders.py` — 新增 `_MESH_VERTEX` + `_MESH_BASIC_FRAGMENT`（#version 330）
6. ✅ `src/features/composite/evaluators/shader_library.py` — `STANDARD_NAMES` + `_SHADER_SOURCES` 注册 `mesh_basic`
7. ✅ `src/features/composite/core/node_engine.py` — `NODE_REGISTRY["scene3d"]` + `NODE_CATEGORIES["3D"]`
8. ✅ `src/features/composite/core/node_capabilities.py` — `_NODE_CAPABILITY_TABLE["scene3d"]`（source / requires_frame_number / owns_color_texture=False）
9. ✅ `src/features/composite/render/render_scheduler.py` — `_GPU_STATEFUL_TYPES` 加入 scene3d（避免调度器误判）
10. ✅ `src/features/composite/core/tile_padding_computer.py` — `_SOURCE_TYPES` 加入 scene3d（一致性）

### 验证结果（headless standalone context 全部通过）
- ✅ 数学库：矩阵运算正确，列主序转换（translate 的 tuple[12,13,14]=1,2,3）
- ✅ 几何：cube(24顶点/36索引)、plane、grid、sphere 全部正确
- ✅ 节点注册：NODE_REGISTRY + NODE_CATEGORIES["3D"]
- ✅ 能力表：source + requires_frame_number=True + 非 placeholder
- ✅ Shader 编译：GLSL 330 在 Mac GL 4.1 上编译成功
- ✅ **depth_renderbuffer 兼容性**（计划头号风险点）：Mac 上工作正常
- ✅ 端到端渲染：立方体可见，亮度 [0.200, 1.314]，HDR >1.0 保留
- ✅ 光照正确：Lambertian 漫反射 + 3/4 布光立体感（std=0.3351）
- ✅ 缓存机制：相同参数复用纹理（content_sig 比较）
- ✅ 下游集成：Scene3D 纹理被下游节点成功采样（零拷贝）
- ✅ 现有测试：136 passed（无回归，scene3d 警告已消除）

### 交付物
- 节点搜索菜单出现 "Scene3D"（在 "3D" 分类下）
- 创建 Scene3D → 连到 Merge → viewer 显示带光照的立方体
- 相机/物体/灯光参数（25 个，其中 23 个可动画）可调，画面实时更新
- 所有数值参数可挂关键帧动画

---

## 阶段二：3D 视口 + C4D 风格相机导航 ✅ 已完成（融合方案）

**目标**：在现有 `CompositeViewer` 内融合 2D/3D 双模式（Nuke 式单视口），选中 Scene3D 节点自动切 3D 模式，Alt+左键旋转/中键平移/右键缩放。导航结果通过信号回写 Scene3D 节点的相机参数。

> **设计决策（重构）**：不再用独立 3D 视口 dock，而是融合进现有 viewer。
> 原因：3D 视口的"显示"本质就是显示一张 2D 纹理（3D 渲染输出），这正是 2D viewer 已在做的事；真正属于 3D 的只有 Alt+三键导航这一小段。独立视口等于复制了一份显示管线，是过度工程。融合方案更符合 Nuke 行业标准（Tab 切换 2D/3D），代码量也更少。

### 修改文件
1. ✅ `src/features/composite/ui/composite_viewport.py` — `CompositeViewer` 融合 3D 模式：
   - 新增 4 个信号：`view_mode_changed` / `scene3d_nav_started` / `scene3d_nav_preview` / `scene3d_nav_committed`
   - 新增 `_view_mode` 字段 + 3D 导航状态（`_orbit_yaw/_orbit_pitch/_cam_dist` + `_nav_mode` + 节流 timer）
   - `mousePressEvent`/`mouseMoveEvent`/`mouseReleaseEvent`/`wheelEvent` 加 3D 模式分支（3D 模式走 Alt+三键导航，2D 模式逻辑不变）
   - `keyPressEvent` 加 **Tab 切换** 2D/3D 模式
   - `set_node_info` 检测 scene3d 节点时 `sync_scene3d_params`
   - 新增 8 个方法：`set_view_mode`/`get_view_mode`/`sync_scene3d_params`/`_handle_3d_press`/`_handle_3d_move`/`_handle_3d_release`/`_emit_nav_preview`/`_emit_nav_committed`
2. ✅ `src/features/composite/ui/composite_page.py` — `set_viewer` 连接 3D 导航信号；`_on_node_selected` 选中 Scene3D → `viewer.set_view_mode("3d")`，选其他 → `"2d"`；导航回调三件套（preview 写图 + committed 合并 undo）。

### 交互方式
| 操作 | 2D 模式（不变） | 3D 模式（新增） |
|---|---|---|
| Tab | — | 切换 2D/3D |
| Alt+左键拖 | — | 轨道旋转（yaw/pitch） |
| Alt+中键拖 | — | 平移 |
| Alt+右键拖 / 滚轮 | — | 缩放（dolly） |
| 选中节点 | 默认 2D | 选中 Scene3D 自动切 3D |

### 验证结果
- ✅ 独立视口文件已删除，runtime/adapter/dock_layout/module 回退干净
- ✅ CompositeViewer 融合 4 信号 + 8 方法就位
- ✅ 6 个修改文件语法编译通过
- ✅ 176 个测试通过，无回归

### 交付物
- 选中 Scene3D 节点 → viewer 自动切 3D 模式（同一视口，不弹新 dock）
- **Tab** 手动切换 2D/3D 模式
- Alt+左键轨道旋转，Alt+中键平移，Alt+右键/滚轮缩放
- 导航结果回写节点参数，每次导航合并成一条可撤销的 undo
- 导航中实时预览（16ms 节流触发重渲染）

### 已知限制（阶段三解决）
- pan 当前用细微轨道模拟（真正的平移需要 target offset，留阶段三）
- Gizmo（红绿蓝三轴）和物体拾取未实现（阶段三）

---

## 阶段三：基础 Transform Gizmo + 物体拾取（第 5-6 周）

**目标**：3D 视口在物体原点画红绿蓝三轴，拖单轴移动物体；点选物体。

### 任务
1. 新建 `src/features/composite/render3d/raycast.py` — `screen_to_ray()` / `ray_axis_intersect()` / `ray_mesh_intersect()`（Möller–Trumbore）。
2. 新建 `src/features/composite/render3d/gizmo_renderer.py` — `GizmoRenderer.draw()` 独立小 program 绘制三轴。
3. 修改 `composite_viewport_3d.py` — mousePress 优先级：Alt 导航 > Gizmo 轴拾取 > 物体拾取。独立 `keyPressEvent` 接管 `W`(移动)。

---

## 阶段四：PBR 材质 + 多灯光 + HDRI 环境（第 7-8 周）

**目标**：法线着色升级为 PBR，多灯光，HDRI 环境反射（IBL）。

### 任务
1. `shaders.py` — 新增 `_MESH_PBR_FRAGMENT`（cook-torrance BRDF + IBL）。
2. 新建 `src/features/composite/render3d/hdri.py` — `.hdr/.exr` 加载 + equirect→cubemap 卷积（irradiance/prefilter/BRDF LUT，compute shader `#version 430`）。
3. Scene3D PARAM_DEFS — 增补材质（albedo_r/g/b、metallic、roughness）+ 4 灯光槽位（light0..3）。

---

## 阶段五：glTF/FBX 导入 + 场景层级（第 9-10 周）

**目标**：导入真实模型，支持父子变换联动。

### 任务
1. `src/shared/media/format_registry.py:21` — 加 `MODEL_3D_EXTENSIONS`，`get_media_type()` 返回 "model"。同步改 media_pool_panel.py / node_editor_widget.py。
2. 新建 `src/features/composite/render3d/model_loader.py` — `trimesh`（glTF/OBJ）+ `pyassimp`（FBX 后备）。
3. 新建 `src/features/composite/render3d/scene_graph.py` — `flatten_scene(root)` 场景树遍历。
4. Scene3D PARAM_DEFS — 加 `model_path`(file)、`use_imported_model`(bool)。

---

## 阶段六：实例化 + 视锥剔除 + 深度合成（第 11-12 周）

**目标**：对齐 Element 3D 复制器，打通深度合成。

### 任务
1. 实例化渲染 — Scene3D 加 instance_count/grid_x/grid_z/spacing；`vao.render(TRIANGLES, instances=N)`。
2. 视锥剔除 — flatten_scene 后 CPU numpy 包围盒剔除。
3. GPU 资源复用 — 相同 mesh 按哈希共享 VBO。
4. 深度合成验证 — Scene3D 输出 depth_texture_id，验证现有 deep_merge 节点消费。

---

## 节点清单说明

| 用户问的节点 | 方案 |
|---|---|
| **3D 渲染节点** | = Scene3D 节点（阶段一已完成），主节点 |
| **相机节点** | Scene3D 的参数组（cam_fov/cam_dist/cam_orbit_yaw/pitch），非独立节点 |
| **灯光节点** | Scene3D 的参数组（light_dir_x/y/z/intensity），非独立节点 |
| **3D Merge 节点** | 现有 deep_merge 节点（消费 Scene3D 的 depth_texture_id），无需新建 |

单节点方案避免引入新 port 类型（scene_data），工程量减半，且符合 Nuke ScanlineRender 单节点哲学。
