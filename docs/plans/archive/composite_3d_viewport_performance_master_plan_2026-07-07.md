# Topos Color Composite 3D Viewport 性能优化总计划

日期：2026-07-07  
范围：Composite 页面 3D viewport、Scene3D / Model3D 渲染路径、四视图、材质贴图、交互导航、模型导入缓存、GPU 资源生命周期。  
目标：让 Topos Color 的 3D 预览体验逐步接近 Blender / Cinema 4D / Unreal Engine 的常规视图流畅度。

---

## 0. 目标定义

### 0.1 体验目标

优化完成后，用户在 Composite 3D 页面中操作 50 万面到 200 万面级别模型时，应达到以下体验：

| 场景 | 目标体验 |
|---|---|
| 单透视视图旋转 / 平移 / 缩放 | 接近实时响应，鼠标拖动无明显停顿 |
| 四视图模式 | 活动视图流畅；非活动视图可降频、降质或暂停 |
| 模型材质预览 | 停止操作时显示完整材质；操作中保留必要材质信息 |
| 大模型首次导入 | 可接受等待，但必须有进度、缓存和二次打开加速 |
| 重复打开同一模型 | 不重复解析、不重复提取贴图、不重复上传可复用资源 |
| 视图操作与合成输出 | 视图交互不应该触发整条合成图的高质量最终渲染 |

### 0.2 量化指标

第一阶段不追求绝对等同 UE5，而是用可测指标逐步逼近：

| 指标 | 当前风险 | 第一目标 | 中期目标 | 长期目标 |
|---|---|---:|---:|---:|
| 50 万三角单视图 orbit | 明显卡顿 | >= 30 FPS | >= 45 FPS | 60 FPS |
| 50 万三角四视图 orbit | 4 视图全量渲染压力大 | 活动视图 >= 30 FPS | 活动视图 >= 45 FPS | 活动视图 60 FPS |
| 200 万三角单视图 orbit | 高风险 | >= 20 FPS | >= 30 FPS | >= 45 FPS |
| 视图导航输入延迟 | 不可见 | < 50 ms | < 25 ms | < 16 ms |
| 停止导航后恢复高质量 | 无明确机制 | < 300 ms | < 180 ms | < 120 ms |
| 同模型二次打开 | 仍可能重复工作 | 比首次快 2x | 快 5x | 接近即时显示低 LOD |
| Python 每帧 draw 循环耗时 | 未量化 | 有统计 | < 4 ms | < 1.5 ms |
| texture bind 次数 | 未量化 | 有统计 | 减少 50% | 按材质批处理 |

---

## 1. 当前问题判断

### 1.1 为什么 Blender / C4D / UE5 更流畅

主流 3D 软件的视图不是每次鼠标移动都做最终质量渲染。它们通常同时拥有：

1. 独立 viewport renderer，不走最终输出管线。
2. GPU scene 常驻：mesh、material、texture、draw state 长期保留在 GPU。
3. 交互态 LOD：拖动视图时降低材质、阴影、后处理、采样、分辨率。
4. 资源和状态批处理：减少 per-frame 的 CPU / Python 调用。
5. 多级缓存：导入缓存、mesh 缓存、材质缓存、贴图缓存、管线缓存。
6. 视图刷新调度：只刷新必要视图，非活动视图降频。
7. 可见性裁剪：视锥裁剪、对象层级 BVH、实例裁剪。
8. 异步导入和后台准备：首帧先显示低质量，再渐进恢复。

Topos Color 目前的 3D 预览还处在“合成节点渲染器兼任 viewport”的阶段。正确性已经逐步补齐，但性能架构还需要从合成输出思路切到 DCC viewport 思路。

### 1.2 当前架构中的主要瓶颈

| 瓶颈 | 表现 | 根源 | 优先级 |
|---|---|---|---|
| 视图和最终 Scene3D 输出共用路径 | 导航时也走高质量节点渲染 | viewport renderer 未独立 | P0 |
| 四视图全量 draw | quad view 下明显更重 | Perspective + Top + Right + Front 多次绘制 | P0 |
| 缺少 navigation LOD | 拖动时仍绑定材质 / PBR / IBL | 没有交互态质量档位 | P0 |
| 缺少性能计数器 | 不知道慢在 CPU、GPU、材质还是四视图 | 无 profiler 元数据 | P0 |
| Python per-mesh draw 成本 | 多 mesh / 多材质时 CPU 开销大 | 每帧大量 uniform / bind / render 调用 | P1 |
| 材质贴图重复状态设置 | 纹理 bind/filter/wrap 重复 | 缺少 material state cache | P1 |
| 模型 fallback 转换无持久缓存 | FBX 首次和再次加载都重 | assimp CLI GLB 临时转换 | P1 |
| 无 viewport LOD mesh | 50 万面以上视图操作吃力 | 未生成低面数代理 | P1 |
| normal map 未接 tangent VAO | 暂时禁用 normal map | VBO layout 未升级 | P2 |
| 四视图材质刚补齐，性能负担增加 | Top/Right/Front 可显示材质但更重 | 需要 active-only textured 策略 | P0 |

---

## 2. 总体技术路线

### 2.1 原则

1. **先测量，再优化**：先加入 profiler，不靠感觉改。
2. **交互优先，质量延后**：鼠标拖动时先保响应，松手后恢复画质。
3. **viewport 与 final output 分层**：最终合成要准确，viewport 要快。
4. **GPU 资源常驻**：相机移动不应重新构建 mesh、贴图或材质。
5. **渐进式显示**：大模型先低质量显示，再后台准备高质量。
6. **四视图按活动视图优先**：非活动视图降频或 solid 显示。
7. **不破坏现有合成语义**：Scene3D 节点输出仍保持可用于合成的高质量图像。

### 2.2 渲染质量档位

建议引入统一的 viewport quality profile：

| Profile | 用途 | 材质 | IBL/PBR | MSAA | 分辨率 | 四视图 |
|---|---|---|---|---|---|---|
| `final` | Scene3D 输出 / 停止操作后 | 完整 | 完整 | 用户设置 | 1.0x | 全视图按设置 |
| `preview` | 普通静止 viewport | baseColor + ORM | 简化 PBR | 0/2x | 1.0x | 可完整 |
| `navigation` | 鼠标拖动视图 | baseColor 或 solid | diffuse / matcap | 0 | 0.5x-0.75x | 只活动视图高频 |
| `fast_solid` | 大模型极限交互 | solid 灰模 / 顶点色 | 无 | 0 | 0.5x | 非活动视图默认 |
| `bounding` | 超大模型 / 加载中 | bbox / proxy | 无 | 0 | 0.5x | 全视图可用 |

---

## 3. 阶段计划总览

| 阶段 | 名称 | 目标 | 预期收益 | 风险 |
|---|---|---|---|---|
| P0 | 性能基线与诊断 | 知道每帧慢在哪里 | 后续优化不盲目 | 低 |
| P1 | Navigation LOD | 拖动视图立刻变顺 | 高 | 中 |
| P2 | 四视图调度优化 | quad view 不拖垮主视图 | 高 | 中 |
| P3 | 材质与纹理状态缓存 | 降低 CPU bind 开销 | 中高 | 中 |
| P4 | 模型导入持久缓存 | 二次打开 / 切换更快 | 中高 | 中 |
| P5 | Viewport LOD mesh | 大模型操作更顺 | 高 | 中高 |
| P6 | 独立 viewport scene cache | 根治合成输出和交互耦合 | 很高 | 高 |
| P7 | GPU picking / outline / component | DCC 操作完整性 | 中 | 高 |
| P8 | 深度 GPU 优化 | 接近主流 DCC 上限 | 高 | 高 |

---

## 4. P0：性能基线与诊断

### 4.1 目标

先建立 viewport 性能可见性。每次优化都必须能回答：

1. 当前帧用了多少毫秒？
2. CPU draw loop 多少毫秒？
3. GPU draw 估算多少毫秒？
4. 绘制了多少 mesh / draw call / triangle？
5. 绑定了多少 texture / material？
6. 四视图每个 view 各自耗时多少？
7. 是否处于 navigation LOD？
8. 是否命中模型 / 贴图 / VBO 缓存？

### 4.2 实施任务

| 编号 | 任务 | 文件 | 说明 | 验收 |
---|---|---|---|---|
| P0-1 | 新增 `Viewport3DPerfStats` dataclass | `src/features/composite/render3d/perf_stats.py` | 存放 frame_ms、draw_calls、triangles、texture_binds 等 | 单测 dataclass 可序列化 |
| P0-2 | Scene3D 每帧记录 stats | `scene3d_node.py` | 在 `_do_evaluate` 和 `_render_ortho_views` 中记录 | TextureResult metadata 有 stats |
| P0-3 | 四视图分 view 计时 | `scene3d_node.py` | `perspective_ms/top_ms/right_ms/front_ms` | quad view 有每视图统计 |
| P0-4 | 材质绑定计数 | `scene3d_node.py`, `material_texture_loader.py` | 每次 bind/upload/filter/wrap 计数 | stats 可显示 texture_binds |
| P0-5 | 模型规模统计 | `model3d_node.py`, `scene_payload.py` | mesh count、triangle count、material count、texture count | payload 携带规模 |
| P0-6 | Viewer 状态显示 | `composite_viewport.py` | 角落小字或 debug overlay，可开关 | 用户能看到 FPS / ms |
| P0-7 | 日志节流 | `render3d/perf_stats.py` | 避免每帧刷日志 | debug 开关下每秒输出一次 |
| P0-8 | benchmark fixture | `tests/benchmark/` | 小/中/大模型模拟或 synthetic mesh | CI 可跑轻量版本 |

### 4.3 指标字段建议

```text
frame_id
quality_profile
viewport_layout
active_view
total_cpu_ms
perspective_ms
top_ms
right_ms
front_ms
draw_calls
triangles_submitted
meshes_visible
meshes_culled
texture_binds
texture_uploads
material_switches
shader_switches
uniform_sets
fbo_switches
model_cache_hit
texture_cache_hit
vbo_cache_hit
navigation_active
lod_level
internal_resolution_scale
```

### 4.4 验收标准

1. 视图右上角可显示 `FPS / frame ms / tris / draw calls`。
2. 切换四视图后能看到每个 view 的渲染耗时。
3. 拖动视图时能看到 `navigation_active=True`。
4. 所有 stats 只在 debug 开启时显示，不污染最终输出。

---

## 5. P1：Navigation LOD

### 5.1 目标

视图操作时优先保证响应。鼠标拖动 / 滚轮 / Alt 导航开始后进入 `navigation` profile；停止 100-200ms 后恢复 `preview/final`。

### 5.2 触发条件

| 事件 | 行为 |
|---|---|
| Alt+LMB orbit 开始 | 进入 navigation profile |
| Alt+MMB pan 开始 | 进入 navigation profile |
| Alt+RMB dolly 开始 | 进入 navigation profile |
| 滚轮连续缩放 | 进入 navigation profile，延迟恢复 |
| gizmo 拖拽 | 进入 navigation profile |
| 鼠标释放 | 不立刻恢复，启动恢复 timer |
| 100-200ms 无输入 | 恢复 preview/final profile |

### 5.3 Navigation Profile 降载策略

| 项 | 静止 | 导航中 |
|---|---|---|
| MSAA | 用户设置 | 0 |
| IBL | 用户设置 | 关闭或简化 |
| PBR | 完整 | diffuse / half PBR |
| baseColor | 开 | 可开 |
| ORM | 开 | 可关或只 roughness 常量 |
| normal map | 后续支持 | 关 |
| emissive | 开 | 可关 |
| 网格 | 完整 | 降低线宽 / 降低 opacity / 可关 |
| 四视图 | 全部更新 | 只活动视图高频 |
| 输出分辨率 | 1.0x | 0.5x-0.75x |
| LOD mesh | 高 | 低 / 中 |

### 5.4 实施任务

| 编号 | 任务 | 文件 | 说明 | 验收 |
|---|---|---|---|---|
| P1-1 | 增加 `ViewportQualityProfile` enum | `viewport3d_state.py` | `FINAL/PREVIEW/NAVIGATION/FAST_SOLID` | 单测 enum |
| P1-2 | CompositeViewer 维护 navigation 状态 | `composite_viewport.py` | 鼠标按下/移动/释放更新状态 | 拖动时状态为 navigation |
| P1-3 | RenderRequest 携带 quality profile | `render_scheduler.py`, evaluator context | 传到 Scene3D | Scene3D 能读到 |
| P1-4 | Scene3D 根据 profile 降载 | `scene3d_node.py` | 关 MSAA、简化材质、降分辨率 | stats 显示 profile |
| P1-5 | 恢复 timer | `composite_viewport.py` | 100-200ms 后恢复高质量 | 停止后自动恢复 |
| P1-6 | UI 状态提示 | `composite_viewport.py` | 可显示 `Navigation LOD` | 不影响输出 |
| P1-7 | 添加用户设置 | 3D scene settings | 是否启用交互降载、延迟、分辨率比例 | 可配置 |

### 5.5 注意点

1. Navigation LOD 是 viewport 状态，不应该写回项目文件。
2. Scene3D 最终输出不能因为 navigation profile 被错误缓存成低质量。
3. content signature 必须包含 quality profile，或者 navigation 结果必须走独立 preview cache。
4. 不能在 UI 线程调用 ModernGL。

---

## 6. P2：四视图调度优化

### 6.1 目标

四视图下不能因为 Top/Right/Front 都完整材质渲染而拖垮活动视图。

### 6.2 四视图显示策略

| 模式 | Perspective | Top | Right | Front | 用途 |
|---|---|---|---|---|---|
| `all_textured` | textured | textured | textured | textured | 静止观察 |
| `active_textured` | 活动视图 textured | 非活动 solid | 非活动 solid | 非活动 solid | 默认推荐 |
| `all_solid_navigation` | solid/baseColor | solid | solid | solid | 超大模型导航 |
| `active_only_update` | 活动视图实时 | 非活动保持上一帧 | 非活动保持上一帧 | 非活动保持上一帧 | 最流畅 |
| `low_freq_ortho` | 活动实时 | 5-10 FPS | 5-10 FPS | 5-10 FPS | 平衡 |

### 6.3 实施任务

| 编号 | 任务 | 文件 | 说明 | 验收 |
|---|---|---|---|---|
| P2-1 | 增加 active ortho view 状态 | `viewport3d_state.py`, `composite_viewport.py` | 鼠标在哪个象限，哪个 view active | active border 正确 |
| P2-2 | RenderRequest 带 active view | scheduler / eval context | 传到 Scene3D | Scene3D stats 可见 |
| P2-3 | `_render_ortho_views` 支持 per-view quality | `scene3d_node.py` | 非活动视图可 solid / skip | 导航时只有活动视图高频 |
| P2-4 | 正交视图降频 | `scene3d_node.py` 或 viewport cache | 非活动视图每 N ms 更新一次 | stats 显示 skipped views |
| P2-5 | 非活动视图保留上一帧 | `composite_viewport.py` / `eval_ctx.ortho_view_textures` | 不清空旧纹理 | 不闪烁 |
| P2-6 | UI 选项 | 3D scene settings | `Quad View Quality: All Textured / Active Textured / Solid` | 可切换 |

### 6.4 默认建议

默认使用：

```text
静止：active_textured
导航：active_only_update + 非活动视图保留上一帧
超大模型：all_solid_navigation
```

---

## 7. P3：材质与纹理状态缓存

### 7.1 目标

减少每帧、每视图、每 mesh 的重复 texture bind、filter/wrap、uniform 设置。

### 7.2 当前风险

当前每个 Model3D mesh 绘制时都会：

1. 设置 PBR 标量 uniform。
2. 调 `_bind_null_material_samplers`。
3. 调 `_bind_material_textures`。
4. 设置 `u_has_*_map`。
5. render 后解绑。

四视图下这些操作会重复多次。Python 调用和 OpenGL state change 都可能成为瓶颈。

### 7.3 实施任务

| 编号 | 任务 | 文件 | 说明 | 验收 |
|---|---|---|---|---|
| P3-1 | 定义 `MaterialStateKey` | `render3d/material_state.py` | material id + texture paths + params | hash 稳定 |
| P3-2 | Scene3D 材质状态缓存 | `scene3d_node.py` | 同材质连续绘制不重复 bind | texture_binds 降低 |
| P3-3 | filter/wrap 只设置一次 | `material_texture_loader.py` | cache key 包含 filter/wrap 或 texture 内记录 | 不重复 glTexParameteri |
| P3-4 | sampler null binding 缓存 | `scene3d_node.py` | program 初始化时设置 sampler location | 每帧少调用 |
| P3-5 | texture atlas 采样默认 | `model3d_node.py` | 已默认 linear/clamp，继续保持 | 测试覆盖 |
| P3-6 | 按材质排序可选 | `scene3d_node.py` | 静态模型可按 material 分组 draw | 需保持透明/选择语义 |

### 7.4 风险

1. 材质排序可能改变透明物体绘制顺序，第一阶段只对不透明 mesh 开启。
2. 多个 Model3D payload 共享 program 时，需要避免 state cache 串场。
3. texture override 来自外部节点，必须纳入 cache key。

---

## 8. P4：模型导入持久缓存

### 8.1 目标

大模型第二次打开不再重复：

1. assimp CLI 转 GLB。
2. 提取 `.fbm` 贴图。
3. trimesh flatten。
4. 生成 CPU mesh 数据。
5. 生成 LOD。

### 8.2 缓存目录建议

| 类型 | 位置 |
|---|---|
| 用户可见贴图 | 模型同目录 `<model>.fbm/` |
| 内部转换缓存 | `~/Library/Caches/ToposColor/model_import_cache/` |
| LOD mesh 缓存 | `~/Library/Caches/ToposColor/model_lod_cache/` |
| 统计 metadata | cache json sidecar |

### 8.3 缓存 key

```text
source_path
source_mtime_ns
source_file_size
import_version
axis_mode
uv_v_mode
unit_mode
assimp_version
trimesh_version
lod_settings
```

### 8.4 实施任务

| 编号 | 任务 | 文件 | 说明 | 验收 |
|---|---|---|---|---|
| P4-1 | 导入 cache manifest | `render3d/model_import_cache.py` | path/mtime/settings 到 cache entry | 单测 |
| P4-2 | FBX fallback GLB 持久化 | `model_loader.py` | 不再每次 temp GLB | 二次加载明显变快 |
| P4-3 | `.fbm` 贴图 manifest | `model_loader.py` | 已提取贴图复用，不重复写 | 文件稳定 |
| P4-4 | CPU SceneGraph pickle/npz 缓存 | `model_loader.py` | 注意版本兼容 | 二次 flatten 更快 |
| P4-5 | cache invalidation | `model_import_cache.py` | 源文件变化自动失效 | 测试 mtime |
| P4-6 | UI cache status | `Model3D` diagnostics | 显示 cache hit/miss | 用户可见 |

### 8.5 风险

1. Python pickle 有安全风险，不要加载不可信 cache；优先 npz/json。
2. 缓存版本必须随数据结构升级递增。
3. 源文件同路径覆盖后必须可靠失效。

---

## 9. P5：Viewport LOD Mesh

### 9.1 目标

让 50 万面、200 万面模型在导航中使用低面数代理，停止后恢复高质量。

### 9.2 LOD 等级

| LOD | 面数比例 | 用途 |
|---|---:|---|
| LOD0 | 100% | final / 静止细看 |
| LOD1 | 50% | preview |
| LOD2 | 25% | navigation |
| LOD3 | bbox / convex proxy | 超大模型 / 加载中 |

### 9.3 实施选择

| 方案 | 优点 | 缺点 | 建议 |
|---|---|---|---|
| trimesh simplify | Python 集成简单 | 质量和依赖不稳定 | 可先调研 |
| meshoptimizer | 游戏引擎常用 | 需绑定或外部库 | 中长期推荐 |
| assimp postprocess | 已有 assimp | 控制较弱 | 可作为 fallback |
| 自研 vertex clustering | 可控 | 质量一般 | 可用于 LOD3 |

### 9.4 实施任务

| 编号 | 任务 | 文件 | 说明 | 验收 |
|---|---|---|---|---|
| P5-1 | LOD 数据结构 | `scene_graph.py`, `scene_payload.py` | mesh 可携带 lod variants | 单测 |
| P5-2 | LOD 生成器接口 | `render3d/lod_builder.py` | 输入 MeshData 输出多级 MeshData | synthetic mesh 测试 |
| P5-3 | Model3D 生成/加载 LOD | `model3d_node.py` | 首次后台生成，缓存 | 导航用低 LOD |
| P5-4 | Scene3D 按 profile 选 LOD | `scene3d_node.py` | navigation 使用 LOD2 | stats 显示 lod |
| P5-5 | LOD 切换去闪烁 | `scene3d_node.py` | 停止后延迟切回 LOD0 | 视觉稳定 |
| P5-6 | LOD UI | Model3D 参数 | auto / force LOD0/1/2 | 可调试 |

### 9.5 注意点

1. LOD 必须保留 UV 和材质索引，否则材质会错。
2. 角色模型、硬表面模型和带透明材质模型需要不同简化策略。
3. 蒙皮模型的 LOD 更复杂；FBX fallback 当前静态化，先处理静态 mesh。

---

## 10. P6：独立 Viewport Scene Cache

### 10.1 目标

这是追赶主流 3D 软件的关键架构阶段：让 viewport 不再完全依赖 Scene3D 节点评估的最终输出路径。

### 10.2 当前问题

相机动一下，本质只需要更新 view/projection matrix；但如果通过合成节点求值链路，可能引发：

1. Scene3D content signature 更新。
2. FBO 重绘。
3. 四视图重绘。
4. eval context 和 preview 管线刷新。
5. 下游合成节点可能被调度。

主流 DCC 会让 viewport 持有自己的 scene cache，相机移动只发一组 GPU uniform。

### 10.3 新架构建议

| 模块 | 职责 |
|---|---|
| `viewport3d_scene_cache.py` | 维护 viewport 专用 GPU scene：mesh VBO、textures、materials、bounds |
| `viewport3d_renderer.py` | 只为交互视图渲染，不写项目输出 |
| `viewport3d_controller.py` | 管理相机、四视图、工具、active view |
| `scene3d_node.py` | 保持最终合成输出，使用高质量 profile |
| `Model3DNode` | 负责导入和提供资源引用，不直接驱动 viewport 每帧解析 |

### 10.4 分阶段迁移

| 子阶段 | 内容 | 风险 |
|---|---|---|
| P6-A | 仍由 Scene3D 渲染，但引入独立 quality profile | 低 |
| P6-B | viewport 读取 Scene3D payload，独立渲染到 viewer texture | 中 |
| P6-C | viewport scene cache 常驻，Scene3D final 只在需要输出时渲染 | 高 |
| P6-D | 支持多 Scene3D / 多 preview target | 高 |

### 10.5 验收

1. 相机 orbit 不触发下游合成节点重新求值。
2. Viewport FPS 明显提升。
3. Scene3D 节点最终输出仍可渲染完整材质和深度。
4. 2D composite viewer 不受 3D viewport cache 影响。

---

## 11. P7：DCC 交互完整性

虽然用户当前关注性能，但主流 3D 软件的“流畅感”还来自完整交互反馈。

### 11.1 功能清单

| 功能 | 性能关联 | 说明 |
|---|---|---|
| GPU ID picking | 中 | 快速选中对象，不做 CPU 全场 raycast |
| selected outline | 中 | 明确反馈，避免反复操作 |
| object bounds overlay | 低 | 大模型导航时可用 bbox |
| component mode | 高 | 点/边/面选择必须有加速结构 |
| transform gizmo GPU overlay | 中 | 避免 helper 几何进入主渲染 |
| orientation widget | 低 | 提升 DCC 感 |
| frame selected | 中 | 快速定位模型，减少用户操作 |
| isolate selected | 高 | 只绘制选中对象，大幅提速 |

### 11.2 实施顺序

1. Object ID picking。
2. Selected outline。
3. Frame selected / focus selected。
4. Isolate selected。
5. Component selection。
6. Component edit LOD / topology acceleration。

---

## 12. P8：深度 GPU 优化

### 12.1 批处理与 draw call 优化

| 优化 | 说明 |
|---|---|
| 按 shader/material 分组绘制 | 减少 program switch 和 texture bind |
| 合并静态 mesh | 多 mesh 同材质可合并 VBO |
| MultiDrawIndirect | OpenGL 支持有限，需评估 macOS GL 4.1 |
| instancing | 已有部分支持，扩展到更多 Model3D 场景 |
| persistent mapped buffer | ModernGL 支持需评估 |

### 12.2 Culling

| 类型 | 优先级 | 说明 |
|---|---|---|
| object frustum culling | 已有部分 | 继续完善 stats |
| per-mesh BVH | P2 | 大模型内部裁剪 |
| occlusion culling | P3 | OpenGL query 复杂，谨慎 |
| small object culling | P2 | 远距离极小物体跳过 |
| backface / two-sided policy | P1 | 默认可背面剔除，提高速度 |

### 12.3 Shader 优化

| 项 | 导航中 | 静止 |
|---|---|---|
| PBR BRDF | 简化 Lambert / half Lambert | 完整 |
| IBL | 关闭或低 mip | 完整 |
| normal map | 关闭 | 接 tangent 后开启 |
| emissive | 可选 | 完整 |
| tone/exposure | 简化 | 完整 |
| pow | 尽量减少 | 可接受 |

### 12.4 平台限制

当前日志显示 macOS OpenGL 4.1，compute shader 需要 4.3，因此：

1. 不能依赖 compute shader 做核心 viewport 优化。
2. 优先使用 OpenGL 3.3/4.1 可用能力。
3. 避免规划必须依赖 Vulkan/WebGPU 的短期任务。
4. 如果未来引入 Metal/WebGPU backend，应作为长期渲染后端替换计划，而不是当前修复依赖。

---

## 13. 具体文件影响范围

### 13.1 高优先级文件

| 文件 | 优化职责 |
|---|---|
| `src/features/composite/nodes/scene3d_node.py` | Scene3D final render、quad view、quality profile、stats、material bind |
| `src/features/composite/nodes/model3d_node.py` | 模型导入、LOD、payload metadata、材质参数 |
| `src/features/composite/render3d/model_loader.py` | FBX/GLB/OBJ 加载、缓存、贴图提取、UV/axis/unit |
| `src/features/composite/render3d/material_texture_loader.py` | 贴图上传、采样状态、缓存 |
| `src/features/composite/render3d/scene_payload.py` | payload signature、统计数据、LOD 引用 |
| `src/features/composite/ui/composite_viewport.py` | 导航状态、四视图 active view、debug overlay |
| `src/features/composite/ui/composite_page.py` | 预览目标、刷新调度、viewer 状态传递 |
| `src/features/composite/render/render_scheduler.py` | RenderRequest 携带 profile / active view |

### 13.2 建议新增文件

| 文件 | 目的 |
|---|---|
| `src/features/composite/render3d/perf_stats.py` | 性能统计 dataclass 和计时工具 |
| `src/features/composite/render3d/material_state.py` | 材质状态 key/cache |
| `src/features/composite/render3d/model_import_cache.py` | 模型导入持久缓存 |
| `src/features/composite/render3d/lod_builder.py` | LOD 生成 |
| `src/features/composite/ui/viewport3d_state.py` | viewport 状态 / profile enum |
| `src/features/composite/ui/viewport3d_controller.py` | 3D 导航与四视图控制 |
| `src/features/composite/render3d/viewport3d_scene_cache.py` | 独立 viewport GPU scene cache |
| `src/features/composite/render3d/viewport3d_renderer.py` | 独立 viewport renderer |

---

## 14. 测试计划

### 14.1 单元测试

| 测试 | 覆盖 |
|---|---|
| `test_viewport3d_perf_stats.py` | stats dataclass、序列化、计数器 |
| `test_scene3d_quality_profile.py` | profile 影响 MSAA / material / resolution |
| `test_quad_view_quality.py` | active view / non-active view 策略 |
| `test_material_state_cache.py` | 材质状态 key、bind 去重 |
| `test_model_import_cache.py` | cache key、mtime invalidation |
| `test_lod_builder.py` | LOD 生成接口和 fallback |
| `test_viewport3d_state.py` | navigation 状态和恢复 timer |

### 14.2 集成测试

| 测试 | 场景 |
|---|---|
| Scene3D 单视图 50 万面 | stats 正确，帧时间低于目标 |
| Quad view 50 万面 | 活动视图更新，非活动视图可跳过 |
| Model3D 材质 + navigation | 导航中低质量，停止恢复材质 |
| 重复加载 FBX | 第二次命中 cache |
| 切换 `uv_v_mode` / `axis_mode` | cache 正确失效 |
| 切换 2D/3D view | 不污染最终输出 |

### 14.3 手动验收模型

| 模型 | 目的 |
|---|---|
| 用户当前 FBX，约 50 万面 | 材质、UV、四视图、navigation 基准 |
| 简单 OBJ + 外部贴图 | 路径贴图与 UV |
| GLB + 内嵌贴图 | glTF 原生路径 |
| 100 万面静态 mesh | LOD 和 navigation |
| 多材质硬表面模型 | material state cache |
| 多对象场景 | culling / selection / isolate |

### 14.4 benchmark 输出

建议每次 benchmark 生成：

```text
docs/benchmarks/3d_viewport/YYYY-MM-DD_modelname_profile.json
docs/benchmarks/3d_viewport/YYYY-MM-DD_summary.md
```

记录：

1. 机器信息。
2. OpenGL 版本。
3. 模型路径、面数、贴图数量。
4. 单视图 / 四视图 FPS。
5. navigation / preview / final profile。
6. draw calls、texture binds、CPU ms。

---

## 15. 用户可见设置建议

### 15.1 3D Scene Settings

| 设置 | 默认 | 说明 |
|---|---|---|
| `Interactive LOD` | On | 拖动视图时降质 |
| `Navigation Resolution Scale` | 0.65 | 导航中内部分辨率 |
| `Navigation Restore Delay` | 150 ms | 停止后恢复高质量 |
| `Quad View Quality` | Active Textured | 四视图默认策略 |
| `Inactive View Update Rate` | 8 FPS | 非活动视图降频 |
| `Show Perf Overlay` | Off | 显示性能统计 |
| `Large Model Auto LOD` | On | 大模型自动低 LOD |
| `Large Model Triangle Threshold` | 500k | 超过即启用 LOD 策略 |

### 15.2 Model3D 参数

| 设置 | 默认 | 说明 |
|---|---|---|
| `size_mode` | real_world | 真实单位 |
| `source_unit` | m | 项目世界单位为米 |
| `axis_mode` | auto | 自动 Z-up/Y-up |
| `uv_v_mode` | auto | FBX/OBJ 自动 V flip |
| `texture_filter` | linear | atlas 友好 |
| `texture_wrap` | clamp | atlas 友好 |
| `Viewport LOD` | auto | 自动选择 LOD |
| `Force Solid During Navigation` | auto | 大模型自动 solid |
| `Cache Converted Model` | on | 持久缓存 |

---

## 16. 分阶段交付清单

### Milestone A：可测量

完成后：

1. 视图角落能看到 FPS / ms / tris / draw calls。
2. stats 可写入 metadata。
3. 四视图每个 view 有耗时。
4. 用户当前 FBX 有基准数据。

建议提交：

```text
Add 3D viewport performance diagnostics
```

### Milestone B：拖动明显变顺

完成后：

1. 拖动时自动 navigation profile。
2. MSAA / IBL / PBR / 分辨率按 profile 降载。
3. 停止后恢复高质量。
4. 用户当前 FBX 单视图 orbit 明显更顺。

建议提交：

```text
Add navigation LOD for Scene3D viewport
```

### Milestone C：四视图可用

完成后：

1. 活动视图高频更新。
2. 非活动视图保留上一帧或低频更新。
3. quad view 不再把主视图拖慢。
4. 可选 All Textured / Active Textured / Solid。

建议提交：

```text
Optimize quad-view 3D rendering scheduling
```

### Milestone D：重复导入变快

完成后：

1. FBX fallback GLB 持久缓存。
2. `.fbm` 贴图不重复提取。
3. CPU mesh cache 命中。
4. 二次打开明显加速。

建议提交：

```text
Add persistent Model3D import cache
```

### Milestone E：大模型交互可接受

完成后：

1. 自动生成 LOD。
2. navigation 使用低 LOD。
3. 50 万面四视图活动视图 >= 30 FPS。
4. 200 万面单视图 >= 20 FPS。

建议提交：

```text
Add viewport LOD meshes for Model3D
```

### Milestone F：DCC 级 viewport 架构

完成后：

1. 独立 viewport scene cache。
2. 相机导航不触发完整 composite graph。
3. Viewport renderer 和 Scene3D final renderer 分层。
4. 为 selection / outline / component edit 打基础。

建议提交：

```text
Introduce dedicated 3D viewport scene cache
```

---

## 17. 风险清单

| 风险 | 影响 | 应对 |
|---|---|---|
| macOS OpenGL 4.1 限制 | compute shader 不可用 | 不把 compute 作为短期依赖 |
| Python draw loop 成本 | 多 mesh 场景 CPU 瓶颈 | 材质批处理、state cache、独立 renderer |
| LOD 破坏 UV / 材质 | 模型花纹错 | LOD 生成必须保 UV 和材质边界 |
| navigation cache 污染 final | 输出低质量 | profile 纳入签名或分离 viewport cache |
| 四视图纹理生命周期 | 非活动视图闪烁 / 空白 | 保留上一帧 texture，明确释放时机 |
| 多线程导入与 GL 线程 | 崩溃 | 解析在线程，GL 上传只在 render thread |
| 贴图缓存路径 | 用户移动模型后丢贴图 | `.fbm` 相邻目录 + manifest + relink |
| 透明材质排序 | 批处理后视觉错 | 第一阶段不排序透明材质 |
| 骨骼动画 FBX fallback | 当前静态化 | 动画/蒙皮作为单独大任务 |

---

## 18. 推荐执行顺序

最建议从以下 6 个任务开始，收益最大、风险相对可控：

1. **P0-1 到 P0-6：性能诊断与 overlay**  
   没有这个，后续优化无法量化。

2. **P1-1 到 P1-5：Navigation LOD**  
   这是最直接提升“拖动变顺”的任务。

3. **P2-1 到 P2-5：四视图 active-only 更新**  
   当前四视图是明显负载点。

4. **P3-1 到 P3-4：材质状态缓存**  
   降低每帧 Python/OpenGL 状态切换。

5. **P4-1 到 P4-4：FBX fallback 持久缓存**  
   大模型工作流会立刻舒服很多。

6. **P5-1 到 P5-4：viewport LOD**  
   真正支撑百万面级别交互。

---

## 19. 完成标准

这轮优化不以“代码写完”为完成标准，而以以下体验达成为准：

1. 用户当前 50 万面 FBX 单视图 orbit 明显流畅。
2. 四视图下活动视图不会被非活动视图拖慢。
3. 拖动视图时可自动降质，停止后恢复材质。
4. 性能 overlay 能解释当前瓶颈。
5. 重复打开同一模型不再重复重转换和重提取。
6. 大模型可自动使用 LOD。
7. Scene3D 最终输出和 viewport 交互质量不互相污染。
8. 每个阶段都有测试和 benchmark 记录。

---

## 20. 下一步建议

下一次实施建议从 **Milestone A：性能诊断** 开始：

1. 新增 `render3d/perf_stats.py`。
2. 给 `Scene3DNode._do_evaluate()` 和 `_render_ortho_views()` 加计时和计数。
3. 在 `TextureResult.metadata` 或 eval context 中带出 stats。
4. 在 `CompositeViewer` debug overlay 显示 FPS / ms / tris / draw calls。
5. 用当前 FBX 记录第一份 benchmark。

只有先把真实瓶颈点照亮，后续才能有把握地追赶 Blender / C4D / UE5 的流畅度。
