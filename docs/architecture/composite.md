# Composite 节点合成系统架构

> 文档版本：2026-09-28（计划 04）。信息全部来自当前 dev 分支源码通读；佐证审查报告见 `docs/audit/2026-09-28/batches/` batch-044 ~ batch-115（composite 主题系列）。
> 路径约定：本文所有相对路径均相对于仓库根目录 `/Users/testuser/Documents/vscode/topos-color-multitask /topos-color01/`。

---

## 1. 系统概览

Composite（合成）页是一个 **Nuke/Fusion 风格的节点图合成器**，与 Color 页共享 HDR GPU 管线。其核心设计决策有四条：

1. **快照驱动的双线程模型**：UI 线程持有可变的 `NodeGraph`，渲染线程只消费不可变的 `GraphSnapshot`，二者以 `RenderRequest` 队列通信，渲染线程评估期间不持有 UI 锁。
2. **单一求值管线复用**：交互预览（`CompositeRenderer`）与导出（`CompositeExportRenderer`）都由同一个 `GraphEvaluatorMixin`（13 个 mixin 组合）驱动，从源头保证"所见即所得"。
3. **内容寻址缓存 + 修订跟踪**：节点的脏检测依赖 `param_revision` / 端口 revision / 参数内容哈希三级信号，缓存键由 `CacheManager.compute_key()` 统一产生，GPU 纹理由 `GPUResourcePool` 按用途分类管理。
4. **Schema 版本化迁移**：工程图以纯 dict 序列化，带 `composite_schema_version`，老版本图经 `graph_migration` 链式前向迁移后加载。

```
                     ┌─────────────────────────── UI 线程 ────────────────────────────────┐
                     │  CompositePage / GraphView            NodeGraph (RLock 保护)       │
                     │    │ 编辑(加/删/连/参数/关键帧/表达式)     │ to_dict/from_dict        │
                     │    │                                   │   ↕ graph_migration v1→v11│
                     │    └── NodeGraph.snapshot() ──(持锁、解析关键帧+表达式、扁平化 Group)──┘
                     │                │ GraphSnapshot（不可变：nodes/connections/eval_order/
                     │                │   generation/topology_revision/frame_number/门控）
                     │                ▼
                     │        RenderRequest（request_id/render_mode/viewer_roi/proxy_scale/
                     │           quality_profile/view_signature/3D 上下文…）
                     └────────────────────────────┬───────────────────────────────────────┘
                                                  │ submit() 队列合并 newest-wins
                     ┌────────────────────────────▼───────── 渲染线程 ─────────────────────┐
                     │  CompositeRenderer (QThread, 共享 GL context)                       │
                     │   ├─ RenderScheduler.plan() ──► RenderPlan（静态分析，无 GPU）      │
                     │   │     分类 source/effect/output/passthrough/placeholder、        │
                     │   │     脏检测、ROI 反传、LOD、依赖层并行组、tiling 预估            │
                     │   ├─ GraphEvaluatorMixin._evaluate_snapshot()                      │
                     │   │     按 eval_order 逐节点：门控→脏跳过→缓存键→命中/执行→        │
                     │   │     tile/ROI 裁剪→IDT 线性化→AOV 元数据回填→CacheEntry 入池   │
                     │   ├─ CacheManager（LRU 内容寻址）  GPUResourcePool（用途分池）      │
                     │   └─ _render_to_output()：三缓冲 ring + frame_ready(tex_id) 信号   │
                     └────────────────────────────┬───────────────────────────────────────┘
                                                  │ frame_ready / metadata_ready (Qt 信号)
                                                  ▼
                                     CompositeViewport（显示、叠加层）

     ───────────────────────────── 导出（后台 worker 线程） ─────────────────────────────
     CompositeRenderExporter ─► CompositeRenderWorker(QThread)
        └► CompositeRenderOutput（IMAGE/VIDEO 模式检测、帧范围解析、色彩编码）
             ├─ render_image()          ──► CompositeExportProvider.render_full_res（主线程，需活 Color 纹理）
             └─ render_video() / render_image_sequence()
                  └► _render_one_frame ─► CompositeFrameRenderer（每 worker 独立 moderngl context，
                       NodeGraph 反序列化缓存、provider 复用、async PBO）
                       └► CompositeExportRenderer（同一 GraphEvaluatorMixin；独立 Cache/池；
                          >2048px 走 tile 渲染；ExportResult 含耗时归因与节点诊断）
                            └─► FFmpegVideoEncoder(PyAV) / FileIO.save_image

     ─────────────────────── Effect Catalog（Edit 页效果复用桥） ───────────────────────
     EffectCatalog（单例，纯 Python）
       ├─ 来源 1：内置 Composite Filter 节点（复用 PARAM_DEFS + core/shaders.py GLSL）
       ├─ 来源 2：node_plugins（NodePluginManager，~/.topos/plugins + <app>/plugins）
       └─ 来源 3：.tglsl DCTL 插件（Color PluginManager → dctl_wrapper 包装）
       └─► EffectSpec ─► EffectPassRunner（Edit 页效果面板 / 导出能力判定 / 动画教学桥）
```

---

## 2. 节点模型（`src/features/composite/nodes/base_node.py`）

### 2.1 核心数据结构

| 结构 | 职责 | 要点 |
|---|---|---|
| `TextureResult` | 节点求值的 GPU 输出契约 | `texture_id`（GL 名）、`revision`（内容版本）、`format`（画幅）/`bbox`（数据窗口）、`alpha_premultiplied`、`color_space`、`bit_depth`、`frame_metadata`（VideoMetadata，宽色域 IDT 依赖）、深度通道（`depth_texture_id`/`has_deep_data`）、AOV 附加通道（`aov_texture_ids`/`aov_dimensions`/`aov_semantic`）、`render_diagnostics`。辅助方法 `with_updates()`（替换纹理时保全元数据）、`aov_signature()`（缓存身份）、`memory_bytes()`（显存估算，含深度/AOV） |
| `ParamDef` | 参数定义 + UI 生成元数据 | 见 2.2 |
| `Port` | 输入/输出端口 | `name` / `direction` / `data_type`（`"image"`、`"float"`、`"vec2"`、`"vec3"`、`"int"`、`"bool"`、`"scene"`）/ `max_connections`（输入端口缺省 1；多输入端口满载时**拒绝**新连接而非清空） |
| `Connection` | 一条边 | 四元组 `src_node_id/src_port_name/dst_node_id/dst_port_name` |

### 2.2 ParamDef 参数契约

- `ptype` 取值：`float / int / str / bool / option / color / file / vec2 / vec3 / channel / hdri`；`option` 类型由 `labels` 提供候选。
- 数值元数据：`default / min_val / max_val / step / decimals`；分组显示：`group / hidden`。
- **动画**：`animated + keyframes`（序列化的 `KeyframeTrack`）。`is_animatable()` 覆盖 float/int/color/vec2/vec3/bool/option；字符串/文件/通道参数不可动画。节点实例上的动画权威存储是 `Node.keyframe_tracks`（P1 起改为节点实例级，不再写类级 `PARAM_DEFS`，避免同类型所有实例被污染）。
- **表达式**：`allow_expression + expression_hint`；`is_expression_compatible()` 仅 float/int。表达式与关键帧**互斥**（A-15：设其一自动清另一）。
- **X/Y 链锁**：`linked` 指向配对参数名（如 `radius_x ↔ radius_y`），Edit 页效果面板据此渲染带 ⛓ 的一行。
- `make_default_parameters()` 按 ptype 产出安全默认值，`Node._init_params_from_defs()` 只填缺失键（加载旧工程时新参数获得默认值）。

### 2.3 修订跟踪协议（脏传播的根基）

节点实例上维持三层递增信号，任何一层变化都必须走对应入口：

| 信号 | 初始值 | 谁递增 | 用途 |
|---|---|---|---|
| `param_revision` | 1 | `set_parameter()` / `set_expression()` / `set_keyframe_track()` / `set_node_parent()` 等 | 调度器脏检测第 2 条规则 |
| `_port_revisions[port]` | 1 | `bump_output_revision()`（上述入口内部调用；求值成功后 evaluator 也会对产出的端口 +1） | 端口级脏检测（第 6 条规则）：下游比对 `upstream_port_revisions` 缓存 |
| `_state`（NodeState） | DIRTY | `state` setter（PLACEHOLDER 状态除外） | UI 状态展示 |

绕过 `set_parameter()` 直接改 `node.parameters` 会漏掉全部三层信号——除非随后显式 `bump_generation()`（快照生成号会变，调度器的内容哈希比对（P1-8）能兜底，但 dirty 标志不会）。

### 2.4 求值接口与 Effect Marker 协议

- `evaluate(inputs, ctx, fb_manager, resource_pool, render_mode)`：入口统一处理 `disabled`（按 `BYPASS_MAPPING` 旁路，缺省"首输入→首输出"），子类实现 `_do_evaluate()`。`render_mode` 为 `"preview"/"full"/"export"`，节点可据此自适应质量。
- **EFFECT MARKER 协议**：GPU 效果节点在 `_do_evaluate()` 中可以不直接产出纹理，而是返回瞬态标记 dict——声明端口映射到 `texture_id=-1` 的占位 `TextureResult`，外加 `_effect_type`（稳定 snake_case 分发键）、`_effect_params`、`_effect_input`（单输入效果的主输入）、`_effect_mask`（可选遮罩）等私有键。`GraphEvaluatorMixin._resolve_output()` 消费标记并调用 `_dispatch_effect()` 替换为真实 `TextureResult`。标记键**不得**被序列化、暴露为输出端口或在 `_resolve_output()` 之后残留。
- **ROI 接口**（纯分析、无 GPU，供调度器 plan 阶段调用）：`compute_output_bbox()`（默认透传首输入 bbox）、`compute_input_roi()`（默认全部输入同 ROI，可按 `requires_roi_expansion()` 外扩——Blur/Glow 正值）、`requires_roi_expansion()`。
- **scene 数据旁路**（3D 双轨制）：`_emit_scene_payload()` 把 `ScenePayload` 写入 `ctx.scene_payloads[node_id][port]`；Scene3D 聚合器用 `_collect_connected_scene_payloads()` 按 `ctx.scene_connections` 连接拓扑收集（v8 起单一 `in` 端口、值为列表支持多连接），未启用映射时才退化为全局类型扫描。旁路每帧清空（payload 含 GL 对象，不可跨帧复用）。
- `params_require_frame_cache()` 类钩子：节点读 `ctx.frame_number` 但参数未关键帧化时（如 Text 逐字动画）声明帧依赖，保证缓存键含帧号。

### 2.5 序列化（to_dict / from_dict）

- 持久化字段：`id / type / position / parameters / disabled / expressions / expression_enabled / keyframe_tracks / parameter_bindings / parent_id / time_range / node_color / user_label`；`parameters` 深拷贝防下游（路径相对化）污染活状态。
- **前向兼容**：`from_dict()` 把未知字段收进 `_unknown_fields`，`to_dict()` 原样回写——旧宿主可无损往返新版本节点/插件数据。
- 加载时关键帧与表达式同参数冲突则**关键帧胜出**（P5-3/A-15）。

---

## 3. 图引擎与序列化迁移

### 3.1 NodeGraph（`src/features/composite/core/node_engine.py`）

- 容器：`nodes`（id→Node）、`connections`、UI 元数据（backdrops、sticky_notes、`_node_groups`）、AE 式图层栈（`_layer_stacks`/`_layers`，纯状态、不参与求值，由 `layer_stack_compiler` 翻译成连接）、viewport/viewer 状态、`fps/duration_frames`。
- 线程安全：单把 `RLock` 保护全部读写；`_generation`（每次变异 +1，快照身份）、`_topology_revision`（连接变化 +1）、`_force_full_eval`。
- 内置 `evaluate()`（LRU 100 的整图求值）仅用于非 GPU/测试路径——生产渲染走渲染线程的 `_evaluate_snapshot()`（需内联处理 effect marker）。

### 3.2 图操作

- `connect()`：完整校验（端口存在、`data_type` 兼容、`_would_create_cycle()` DFS 成环检测）**先于**替换旧连接；单输入端口满载替换、多输入端口满载拒绝；成功后 `_mark_dirty_downstream()`。
- `remove_node_and_reconnect()`：处理节点删除的主路径——单图像输入 + ≥1 图像输出时自动桥接上下游（Topos Color/Grade/Blur 等）。
- 表达式管理：`set_node_expression()`（解析校验 → `ExpressionManager` 依赖登记 → 环依赖检测 → `_propagate_expression_change()` 传递置脏）；pick-whip 参数绑定 `set_parameter_binding()` 生成 `n_<node_id>.<param>` 别名表达式复用既有依赖排序与环拒绝；AE 式父子 `set_node_parent()`（自环/环链拒绝）。
- `snapshot()`：**必须**在 UI 线程调用。流程：`_evaluate_snapshot_parameters()` 无变异地解析关键帧插值 + 表达式（含 `n_` 别名、NaN/Inf 回退安全值、父子 world-transform 合成、图层命名空间）→ 逐节点深拷贝为 `NodeSnapshot`（含 param/port revision、动画轨道、表达式及错误、time_range、Group 边界契约）→ `_flatten_group_boundaries()` 把 Group 宏边界改写为成员直连（活图不动，仅快照）→ 快照拓扑排序 → 预计算 `layer_frame_gates`（图层 in/out 门）与 `frame_overrides`（时间节点帧重映射）→ 返回 `GraphSnapshot`。

### 3.3 序列化与迁移（`core/graph_migration.py`）

`to_dict()` 写入 `composite_schema_version`、`fps/duration_frames`、`graph_revision`（= `_generation`，供无头 Comp Clip 预览做廉价失效）、`topology_revision`、nodes/connections/UI 元数据/图层栈。`serialize_to_bytes()` 另支持 msgpack + gzip + 增量 delta。

`from_dict()` 首先调用 `migrate_graph()`：检测存储版本（缺失视为 1），链式执行纯 dict 变换直至当前版本 `COMPOSITE_SCHEMA_VERSION = 11`，不修改输入。迁移链历史：

| 版本 | 变更 |
|---|---|
| v1→v2 | `blend` 类型别名到 `merge`；补参数默认；`image_input` 增 `color_space` |
| v2→v3 | Transform 由 UV 空间改像素空间（pivot_mode 派生、translate 换算） |
| v3→v4 | Crop 参数改名（`reformat→affect_format`、`outside→outside_policy`） |
| v4→v5 | premult/unpremult/clamp 输出端口 `image→result`（连接同步改写） |
| v5→v6 | 动画从 parameters 内嵌 dict 提升为节点级 `keyframe_tracks`；图级旧存储分发到节点 |
| v6→v7 | Scene3D 拆分：相机/灯光/几何/模型参数 spawn 成 Camera3D/Light3D/Geo3D/Model3D 子节点（`spawn_3d_scene_children` 共享工厂，与实时创建路径同构） |
| v7→v8 | Scene3D 三个 scene 输入端口合并为单一 `in` |
| v8→v9 | Scene3D 场景环境参数上移到图级 `viewer_state["default_3d_scene"]`（全局 3D Scene Settings，首个 Scene3D 的值作默认） |
| v9→v10 | 引入可选 `layer_stacks`/`layers`（纯增量规整） |
| v10→v11 | 节点级可选 `user_label`（画布重命名） |

`from_dict()` 在迁移后还执行两条**运行时**迁移（非 schema）：`migrate_color_input_to_topos_color()`（废弃 `color_input` → `timeline_clip_input → topos_color`，仅限已有时间线源的图，独立图像合成不迁）与 `migrate_output_format_mode()`（Output 节点切 `format_mode="custom"`，由它独占"合成分辨率"权威；宽高从上游正向尺寸播种，0 尺寸不采信）。

### 3.4 节点注册

- `NODE_REGISTRY`（约 100 个内置类型）+ `NODE_CATEGORIES`（菜单分类）；未注册/占位类型反序列化时落 `UnknownPlaceholderNode` 保图不丢。
- `DEFERRED_NODE_TYPES` 声明"故意为 None"的可选特性节点（如 `motion_graphic` 由 teaching_animation 模块启用时注册）。
- 运行时注册面：`register_node_type()/unregister_node_type()/register_plugin_nodes()`（对接 `NodePluginManager`：`~/.topos/plugins/node_plugins/` 与 `<app>/plugins/node_plugins/`）；统一工厂 `create_node_by_type()`（registry 优先、插件兜底，调度器 ROI 分析亦用它建临时实例）。
- `get_categories_for_mode(image_mode)` 在图像模式隐藏时间驱动节点（timeline_clip_input/video_frame/time_offset/retime/frame_hold）。
- 节点分类的**单一事实源**是 `core/node_capabilities.py`（`is_source_node_type()`/`is_output_node_type()`/`is_placeholder_node_type()`/`PASSTHROUGH_NODE_TYPES`/`requires_frame_number()` 等），调度器与求值器均从它读取（`render_scheduler._GPU_STATEFUL_TYPES` 保留为一致性校验用的遗留镜像）。

---

## 4. 求值管线

### 4.1 RenderScheduler（`src/features/composite/render/render_scheduler.py`）

调度层位于 UI 线程与渲染线程之间，职责是"请求跟踪 + 计划生成 + 诊断"，不做任何 GPU 工作。

**RenderRequest**（不可变）：`snapshot`、`request_id`（单调）、`render_mode`（PREVIEW/FULL/EXPORT）、`viewer_roi`、`proxy_scale`（预览代理分辨率 → LOD 强制档，导出恒 1.0）、`quality_profile`（draft/preview/final 语义档，full/export 强制 FINAL）、`output_name/output_node_id`（命名终端输出锥）、`graph_generation/color_revision`（时效检测）、`viewer_node_id/port`（任意节点预览上屏）、`view_signature`（视图态签名，Tab 切换等不改拓扑的视图变化靠它强制重渲）、`force_refresh`、`interaction_active`（拖拽手柄时抬高缓存预算防分配抖动）、`cancel_on_supersede`（时间线播放允许在途帧完成）、`playback_drive`（渲染线程自驱播放）、3D 上下文（quad_view/single_ortho_view/selected/isolate/global_3d_scene_settings/view_mode_overrides）。

**plan() 生成 RenderPlan**（纯静态分析）：

1. 逐节点（按 `snapshot.eval_order`）产出 `NodePlan`：action 分类（output > source > group(passthrough) > placeholder > disabled(passthrough) > effect）、`params_hash`（参数 JSON 的 MD5[:12]）、inputs 预解析；
2. 依赖层计算 + `parallel_groups`（同层可并行）；
3. **脏检测** `_compute_dirty_nodes()`——七条规则：新节点 / `param_revision` 变化 / **参数内容哈希变化**（P1-8：新反序列化的图 revision 从 1 重启，靠内容比对兜底）/ disabled 变化 / 拓扑 revision 变化时比对输入映射 / color_revision 变化脏 color_input / 动态源节点恒脏 / **帧号变化只脏帧依赖节点**（`_snapshot_node_needs_frame`：关键帧、time_range、表达式、能力声明、`params_require_frame_cache`、动态源；帧重映射与图层门控节点也在此列）/ 上游端口 revision 变化（端口级细粒度）；最后沿连接 BFS 向下游传播；
4. ROI 反向传播（`_propagate_roi`，仅 PREVIEW 且无代理降档）：从 Output 出发逆拓扑，用节点 `compute_input_roi()` 求各输入所需区域、多消费者取 union、**不做二次外扩**（防止 Blur 链重复扩边）；顺带前向估算 bbox、按 ROI 与视口相交赋 LOD（0 全分辨率 / 2 四分之一）；
5. tiling 预估：`GPUResourcePool.estimate_render_memory()` 静态估算显存，超 1024MB 预算时给出建议 tile 尺寸（1024/512/256 或大图 2048 档）。

`update_tracking` 参数是关键纪律：**只有驱动求值的那次 plan() 调用才推进跟踪状态**；只读探针（如读回渲染器的 tiling 探测）必须传 `False`，否则会"消费"脏状态导致参数编辑不再重渲（P1-8 事故修复）。`prepare_for_export()` 从交互 scheduler 单向拷贝跟踪状态，使导出首计划能识别"交互期间真正变过的节点"。

请求时效：`advance_request_id()`（submit 时 UI 线程调用，令在途求值可见新请求并中途取消）、`mark_current()`、`is_stale()/is_stale_request()`、`should_cancel_evaluation()`（求值循环内逐节点轮询）。

### 4.2 BaseEvaluatorMixin 求值协议（`src/features/composite/evaluators/base_evaluator.py`）

`GraphEvaluatorMixin`（`graph_evaluator_mixin.py`）把 `BaseEvaluatorMixin` 与 blur/color/transform/merge/keyer/channel/filter/cache/tile/histogram/keyframe/expression/ai 共 13 个 mixin 组合成完整求值 API，供 `CompositeRenderer`（异步交互）与 `CompositeExportRenderer`（同步一次性）共用。依赖注入面：`self.ctx / fb_manager / utility_shaders / _render_nodes / _cache_manager / _resource_pool / _snapshot`。

`_evaluate_snapshot()` 主循环（按 `plan.eval_order` 逐节点）：

1. **帧准备**：`cache_mgr.begin_frame()`（清 pin 集合）、`set_frame_number()`（LRU 帧戳）、scene 旁路清空、构建 `scene_connections` 端口感知映射（scene3d 无连接也显式建空表——阻止未连接 Camera 抢占虚拟 Viewer 相机）；`interaction_active` 幂等切换缓存预算；
2. `plan = scheduler.plan(...)`（proxy_scale → force_lod：0.5→lod1、0.25→lod2；EXPORT 守卫恒全分辨率）+ **pass fusion** 分析（可融合链，模式受 `pass_fusion_mode` 偏好约束）；
3. 逐节点执行前的一串门：
   - 命名输出**依赖锥裁剪**（`selected_output_dependency_ids`：无关分支不求值、不占缓存）；
   - 图层 in/out 帧门 / 源节点 time_range 门（用**重映射后**的请求帧判定，P0-04）→ 门关则产出空；
   - **脏跳过**：不在 `dirty_set` 的节点直接取缓存输出并 re-pin；
   - **stale 取消**：`should_cancel_evaluation()` 为真则释放本帧临时纹理并返回 None；
   - placeholder 节点：把首个有效输入直通到输出端口（防"未实现节点把下游全断成黑帧"），AI 占位缺模型包时记结构化诊断 `MODEL_UNAVAILABLE`；
   - 融合链首节点：整链一次执行，链尾结果入缓存；
4. 输入聚合 → LOD 应用（lod>0 下采样；lod=0 但输入小于估算 bbox 一半时上采样补齐）→ **缓存键** `cache_mgr.compute_key()`（节点身份 + 参数 + 上游纹理元数据（id/revision/format/bbox/alpha/color_space/AOV 签名）+ render_mode/ROI/LOD/quality + **仅帧依赖节点**含 frame_number + 帧重映射签名 + scene 输入签名 + 运行时输入签名（字幕文本、文件 mtime））；
5. 命中（持久缓存 / 动态绕过节点除外 / A6 节点级磁盘缓存 opt-in `cache_to_disk`）则跳过执行；未命中执行 `_evaluate_node()`；
6. 源节点输出后处理：tile/ROI 裁剪（`_crop_to_tile_roi`，裁剪/IDT 新增纹理归入缓存条目防泄漏）→ **IDT** 输入设备变换（sRGB 源解码到场景线性，宽色域原色从 `frame_metadata` 双形态读取）→ `propagate_auxiliary_metadata()`（单目运算后回填深度/AOV 契约）；
7. 产出有效则 `CacheEntry(outputs, owned_texture_ids)` 入池并 pin；自管源节点标记 `_cache_invalidated_needed` 时做**含下游锥**的缓存失效（释放过的 glo 可能被下游缓存条目引用，只清本节点会绑到回收名上段错误）；动态绕过节点（`timeline_clip_input/video_frame/motion_graphic`：像素随快照之外的运行态变化且求值有副作用）不入持久缓存，纹理记入 `_transient_texture_ids_to_release`；
8. 循环后：更新快照上的端口 revision（产出端口 +1，上游 revision 写回 `upstream_port_revisions`）、性能记录、诊断日志；
9. **节点预览**：`viewer_node_id` 存在时从本轮 `cache` dict（覆盖持久与绕过两类）取该节点第一个图像端口返回，否则返回 Output 的 `display`。

`_evaluate_node()` 分派顺序：source（`_render_nodes` 持久实例 + `CompositeEvalContext` 业务上下文）→ passthrough → AI 节点（异步缓存优先）→ effect（`_create_ephemeral_node()` 临时实例：注入快照参数 + 关键帧插值（int 参数插值后回整，S7 用 ParamDef 元数据兜底 min/max 防错夹）+ time_range 越界置 disabled）→ output（持久实例）。

`_resolve_output()` 解析 effect 标记：`_effect_type` → `_dispatch_effect()`（A5：transform 先尝试子帧运动模糊）→ `_filter_marker_keys()` 滤掉 `_` 前缀键 → `_normalize_output_ports()` 把唯一 TextureResult 别名到**声明的**输出端口（分发器约定 `result`，但连接按声明端口名取纹理，键名不齐会静默断边）。

### 4.3 GPUResourcePool（`src/features/composite/render/gpu_resource_pool.py`）

- **用途分池**：`TextureUsage.TEMP / CACHE / OUTPUT / TILE / EXTERNAL`，各自 `acquire_temp_texture/acquire_cache_texture/acquire_output_texture/acquire_tile_texture`；`register_external_texture()` 把节点自管纹理纳入预算监控。
- **格式分层**：preview/full 用 RGBA16F（f2），export 用 RGBA32F（f4）（`acquire_tiered_texture(render_mode=...)`）。
- **驱逐策略**：LRU + 内存预算强制 + 成本驱逐（size × age）+ **pin**（活跃纹理如显示输出、本帧缓存命中条目受保护）；预算可用 `VRAMMonitor` 动态查询（GL_NVX_gpu_memory_info / GL_ATI_meminfo 扩展，1 秒节流，不可用回退用户配置）。
- **跨帧槽复用**：`VideoSlotPool`（begin_frame/end_frame + 按 `(w,h,components,dtype,slot_id)` 复用视频帧纹理）。
- **fence 感知**：释放中的纹理等 GPU fence 完成才回池复用（防 in-flight 复用）；`insert_fence()` 由渲染器在输出 blit 后调用。
- `estimate_render_memory()` 静态估算（调度器 tiling 决策复用）；`force_cleanup()` 供离页/关工程/内存压力。
- 与 `CacheManager`（`cache_manager.py`，LRU 128 条目 + `CacheEntry.owned_texture_ids` 持有池纹理 + 类别感知 + 交互模式抬预算 + pin/begin_frame 帧循环）构成"缓存条目 → 纹理所有权 → 池回收"的完整生命周期：**缓存驱逐负责释放缓存纹理，stale 请求只抑制上屏不释放**（否则会把仍被缓存条目引用的纹理提前回池，引发绿帧/驱动崩溃）。

---

## 5. Effect Catalog（`src/features/shared/effects/effect_catalog.py`）

`EffectCatalog` 是进程级单例（`get_effect_catalog()`），纯 Python、无 PyQt 依赖、RLock 线程安全，可在渲染线程与无头工具使用；**懒构建**（首次访问才 import 6000+ 行的 `shaders.py` GLSL，保证启动速度）。它把 Composite 页的效果能力打包成 `EffectSpec`，让 **Edit 页**以与 Composite 属性面板完全一致的参数面板（同一份 `ParamDef`）使用同一份 GLSL。

`EffectSpec`（`effect_spec.py`）字段：`effect_type`（稳定 id，插件为 `plugin:<id>`）、`category`、`param_defs`（**直接复用节点类的 `PARAM_DEFS` 元组**——这是"两页面板一致"的契约）、`shader_kind`（SINGLE_PASS / SEPARABLE_PASSES / DCTL_PROCESS）、`shader_sources`（复用 `core/shaders.py` 的既有 GLSL 常量）、`dispatch_key`、`param_to_uniform`（仅直通映射；打包知识如 `blur_type→u_kernel_mode`、`channel_r/g/b/a→u_channel_mask` 属于 `EffectPassRunner` 的 per-shader-kind 逻辑）、`structural_param_names`（进程序缓存键、变化触发重编译）、插件元数据（content/schema hash、`plugin_capabilities`——`export_support` 声明制且 fail-closed：未声明导出能力的 .tglsl 只有 `gpu_preview`，导出 preflight 拒绝而非静默丢弃；`background_independent` 遮挡裁剪声明）。

三个来源的聚合方式（`_ensure_built()`）：

1. **内置 Composite Filter 节点**（Phase 1/2 已接）：blur、sharpen、denoise、edge_detect、erode_dilate、defocus、glow 七个 spec。构建器逐一取节点类的 `PARAM_DEFS` + 对应 shader 源（如 blur 的可分离 H/V 对 + bilateral/motion/bokeh 单 pass 变体；glow 的 V pass 双纹理绑定）；defocus 在 packer 层弥合历史 schema/实现错位（`max_radius→u_iris_size`）。
2. **node_plugins**（`NodePluginManager` 发现于 `~/.topos/plugins/node_plugins/` 与 `<app>/plugins/node_plugins/`）：catalog 头注声明为 Phase 3 接入位；外部可经线程安全的 `register()` 注入/替换 spec（插件 reload 复用同一入口，`_register_locked` 保证分类索引不重复）。
3. **.tglsl DCTL 插件**（Phase 3 已接）：委托 Color 页 `PluginManager.scan_plugins(<root>/plugins)` 扫描（单一发现源），逐个经 `dctl_wrapper.wrap_dctl_process()` 把插件 `process(vec3, vec2)` 包装成完整 fragment shader；`@param` 语法映射到 `ParamDef`（slider→float、checkbox→bool、select→option、color→vec3 语义、separator 跳过）；`select` 参数视为结构性（进程序缓存键）。单插件解析失败仅告警跳过，不毒化整个 catalog。

消费方：`edit/ui/edit_effects_panel.py`、`edit/ui/edit_properties_panel.py`、`edit/core/commands.py`（加效果/参数读写）、`deliver/core/delivery_capability.py`（可交付能力判定）、`features/shared/effects/effect_capability.py`（执行能力解析）、`teaching_animation/render/composite_effect_bridge.py`。`refresh()` 清空重建（插件发现后调用）。

---

## 6. 渲染输出链：预览与导出的一致性

### 6.1 交互预览路径（`render/composite_renderer.py`）

`CompositeRenderer(GraphEvaluatorMixin, QThread)`：

- **GL 归属**：GUI 线程创建 `QOffscreenSurface` + 与主上下文 share 的 `QOpenGLContext` 后 moveToThread；`run()` 中 makeCurrent 并用 moderngl 包装，创建自己的 `FramebufferManager`、`GPUResourcePool(1024MB)`、`CacheManager(128)`、`AsyncPBOReadback`、`ImageLoader`。命令循环每条命令持 `GLContextManager.mutex`。
- **命令队列**（`CompositeCommand`）：`submit()` 对 EVALUATE_GRAPH 做队列合并（只保留最新请求，保留非求值命令）；交互编辑 latest-wins 且可取消在途（`cancel_on_supersede=True` 时 `advance_request_id`），时间线播放则允许在途帧完成上屏（防持续掉帧冻结 viewer）。
- **`_handle_evaluate_graph()`**：请求签名跳过检查（generation、color_revision、render_mode、viewer_roi、color 纹理三元组、proxy_scale、quality、output、`view_signature`、`force_refresh`——任何一个不匹配都要重渲，否则视图态切换会被 generation-only 判等掩盖）→ `mark_current` → `_sync_render_nodes()`（源/输出持久实例的增删改 + 参数同步，注意 `fps=0` 是 UI 侧未初始化哨兵不回写）→ 构建 `CompositeEvalContext` → `_evaluate_snapshot()` → 结果 stale 则仅抑制上屏 → `_render_to_output()`。
- **`_render_to_output()`**：三缓冲 FBO/纹理 ring（写指针轮转，退役输出纹理经 fence 延迟释放，跨上下文共享前 `ctx.finish()`——macOS 共享上下文不隐式同步）；ROI 裁剪链的子图按 bbox 重组回全帧（显示、像素探针、叠加层路径不变）；发 `frame_ready(request_id, tex_id, w, h)` 与 `metadata_ready`（format/bbox/正交视图/ID picking/AOV/渲染诊断）。
- **特化命令**：`RENDER_3D_VIEW`（相机导航快速路径：缓存签名命中时只重画 Scene3D，绕过整图求值；任何不一致降级全量）、`MODEL_IMPORT_READY`（模型后台导入完成后清缓存强制同快照刷新）、`playback_drive` 自驱播放（渲染线程按 wall-clock 推帧，消灭"解码→notify→UI 快照→重提交"双往返）、`READBACK_PRESENT_ASYNC`（PBO 异步预览读回）、`RENDER_EXPORT`（复用渲染线程上下文的一次性导出）、`RENDER_GPU_SCOPE`（QC 示波器全 GPU 管线）。

### 6.2 导出路径（`render/composite_render_worker.py` → `composite_render_output.py` → `composite_frame_renderer.py` → `composite_export_renderer.py`）

- **`CompositeRenderWorker`**（QObject + QThread，`CompositeRenderExporter` 协调）：主线程捕获 settings/context（graph_data、edit_project、timeline_sequences、comp_clip、config_manager），QueuedConnection 投递 `run()`；worker 在自己线程构造 `CompositeRenderOutput` 并调 `render_video()/render_image_sequence()`，进度/完成/错误经 Qt 信号回主线程；协作式取消（`threading.Event`，零帧取消删除半成品文件）。
- **`CompositeRenderOutput`**：模式检测 `detect_composite_mode()`（存在时间依赖节点 / 任意关键帧 / time_range 即 VIDEO，否则 IMAGE）；帧范围优先级 comp_clip 元数据 → video_frame 节点 → 最远关键帧兜底。`render_video()` 逐帧 `_render_one_frame()` 得场景线性 BGR float32，`transform_color(LINEAR_SCENE → output_descriptor)` 编码到目标色彩空间（HDR PQ/HLG 与宽色域 SDR 自动升 10 位并写色彩元数据）；视频导出仅接受 beauty pass（辅助 AOV 会破坏 ID 语义）；支持多 worker 并行帧。`render_image()`（单帧）在主线程走 `CompositeExportProvider.render_full_res()`——需要活的 Color 页纹理。
- **`CompositeFrameRenderer`**：无头逐帧渲染器（Edit 时间线预览与导出共用）；每线程独立 moderngl 上下文（不绑主线程）；按 `(revision token, graph_data id)` 缓存 NodeGraph 反序列化（跳过每帧 from_dict 的 5-15ms 纯 Python 开销）；复用 `TimelineClipFrameProvider`（避免每帧重建解码会话）；可选 async PBO 读回 lane；`COMPOSITE_GRAPH_RENDERER_VERSION` 常量进帧级缓存键——**渲染器实现变化而图不变时必须递增**，否则同键命中旧实现渲染的缓存帧。
- **`CompositeExportRenderer`**：一次性全分辨率求值（同一 `GraphEvaluatorMixin`）；**缓存隔离**——自建 CacheManager/GPUResourcePool，绝不碰交互渲染器的缓存；可接收 `interactive_scheduler` 播种脏跟踪做增量；>2048px 走 tile 渲染（每 tile 独立求值 + 空间效果 padding + GPU/CPU 拼接策略选择）；`ExportResult` 携带 eval/readback 分段耗时、预览策略归因、帧 trace 与节点级结构化诊断。

### 6.3 一致性保证要点

| 维度 | 机制 |
|---|---|
| 求值语义 | 预览与导出共用 `GraphEvaluatorMixin`；同一 `NodeGraph.from_dict` + `snapshot()` 数据源 |
| 分辨率 | 导出路径 `force_lod` 恒 0、proxy_scale 恒 1.0、quality 强制 FINAL；纹理分层 export=RGBA32F |
| 色彩 | 统一 IDT→场景线性工作空间；导出端 `transform_color` 到目标描述符；`config_manager` 决定 builtin/OCIO 引擎且全链透传 |
| 缓存隔离 | 导出独立 Cache/池 + `prepare_for_export` 单向播种，无共享可变状态 |
| 实现版本 | `COMPOSITE_GRAPH_RENDERER_VERSION` 参与帧缓存键 |

---

## 7. 关键调用链

**A. 交互预览一帧**

```
CompositePage(参数拖动/播放头)
  → NodeGraph.set_node_parameter()          [UI 线程, RLock; _generation+1, param_revision+1, 端口 revision+1]
  → NodeGraph.snapshot(frame, fps)          [持锁; 解析关键帧/表达式; Group 扁平化; 帧门/重映射预算]
  → RenderRequest(request_id++, view_signature…)
  → CompositeRenderer.submit(EVALUATE_GRAPH) [队列合并; advance_request_id]
  → 渲染线程 run() 循环 → _handle_evaluate_graph()
      签名跳过检查 → _sync_render_nodes → CompositeEvalContext
      → _evaluate_snapshot()
          → RenderScheduler.plan()          [分类/脏检测/ROI/LOD/并行组/tiling]
          → 逐节点: 门控 → 脏跳过 → compute_key → 缓存命中?
              miss: _evaluate_node → _resolve_output(_dispatch_effect)
                    → tile/ROI 裁剪 → IDT → AOV 回填 → CacheEntry put+pin
          → 端口 revision 更新
      → _render_to_output()                 [三缓冲; ctx.finish; fence]
  → frame_ready(tex_id) / metadata_ready → CompositeViewport
```

**B. Effect Catalog 聚合（Edit 页复用）**

```
Edit 页 edit_effects_panel → get_effect_catalog().get_specs_by_category("filter")
  ← 懒构建 _ensure_built():
      内置节点: BlurNode.PARAM_DEFS + shaders.py GLSL ─┐
      .tglsl: Color PluginManager.scan → wrap_dctl_process ─┼─► EffectSpec[]（register 可再注入 node_plugins）
  → EffectPassRunner 按 shader_kind 编译/执行（param 打包、变体选择、程序缓存键=源 hash+结构性参数）
```

**C. 合成视频导出**

```
CompositeRenderDialog → CompositeRenderExporter.export(settings, context)
  → CompositeRenderWorker.run() [worker QThread]
  → CompositeRenderOutput.render_video()
      detect_composite_mode / resolve_video_range
      逐帧 _render_one_frame → CompositeFrameRenderer.render_comp_clip_frame
          (NodeGraph 反序列化缓存; provider 复用)
          → CompositeExportRenderer.render_and_readback [同 mixin; 独立 Cache/池; 可 tile]
      → transform_color(LINEAR→目标) → FFmpegVideoEncoder(PyAV)
  → finished/error/progress 信号回主线程
```

---

## 8. 改动注意事项（红线清单）

1. **线程与 GL 边界**：`NodeGraph.snapshot()` 只能在 UI 线程调用；一切 OpenGL 调用只在渲染线程（上下文 makeCurrent 后）；`NodeGraph.remove_node()` 故意不做 GPU 清理——释放必须经渲染线程调度（`CLEANUP_NODE` 命令）。跨上下文共享输出纹理前必须 `ctx.finish()` + fence。
2. **缓存键纪律**：任何新增的"会影响像素的输入/模式/元数据"必须进 `CacheManager.compute_key()`，否则脏检查与内容寻址双失效。同时 `render_scheduler._snapshot_node_needs_frame()` 与 `base_evaluator._node_needs_frame_cache()` 必须保持逐条同步（策略漂移会让 dirty-skip 在精确键生效前就复用旧帧）。
3. **修订信号不可绕过**：改参数一律走 `set_parameter()`/`set_expression()`/`set_keyframe_track()`；直接写 `node.parameters` 会漏 `_dirty`/`param_revision`/端口 revision（P1-8 内容哈希只兜底 generation 层）。
4. **plan() 的 `update_tracking`**：只读分析调用必须传 `False`；驱动求值的调用传 `True`。只有一份跟踪状态，消费错位直接导致参数编辑不重渲。
5. **Effect Marker 键**（`_effect_type` 等）是私有瞬态协议：不得序列化、不得暴露为输出端口、`_resolve_output()` 之后不得残留。效果节点输出端口命名遵守全局规范（源/生成节点可用 `image`/`matte`/`rgba`，效果节点统一 `result`；`_normalize_output_ports` 只是别名兜底不是豁免）。
6. **Schema 变更流程**：改图结构/参数语义时 `bump COMPOSITE_SCHEMA_VERSION` + 新增 `migrate_vN_to_vN+1()` **纯 dict 变换**（不实例化节点）+ 更新 `Node.from_dict` 的 `known_fields`（保护 `_unknown_fields` 往返）；涉及运行时结构改写（如 color_input 迁移）放 `from_dict` 后置迁移，别塞进 schema 链。
7. **缓存纹理生命周期**：stale/被超越的请求只抑制上屏，**不得**释放仍被 CacheEntry 持有的纹理（缓存驱逐是唯一释放路径）；自管纹理源节点重建 GL 纹理后必须触发含下游锥的失效（`_cache_invalidated_needed` 机制）。
8. **GPU 资源配对**：`acquire_*` 与 `release_*` 严格配对；求值期间新分配的裁剪/IDT 纹理要并入 `owned_texture_ids`；导出路径自建池，勿引用交互渲染器的池/缓存。
9. **新增效果的三处同步**（Effect Catalog 路径）：节点 `PARAM_DEFS`（参数契约）、`core/shaders.py`（GLSL）、`effect_catalog.py` 的 spec 构建 + `EffectPassRunner` 的参数打包；Edit 面板一致性依赖 PARAM_DEFS 原样复用，勿另写一份参数表。
10. **请求跳过签名**：给 `RenderRequest` 新增会影响输出的字段时，必须同时纳入 `_handle_evaluate_graph()` 的签名比对（或并入 `view_signature`），否则该变化会被"无变化"误判跳过。
11. **动态源节点**（timeline_clip_input/video_frame/motion_graphic）恒脏并绕过持久缓存是设计而非缺陷；不要把"下游也绕过"加回来（缓存键已含上游纹理 id+revision，播放持帧时下游应命中缓存）。
12. **渲染器实现变更**：改变像素输出的求值/着色器修改需递增 `CompositeFrameRenderer.COMPOSITE_GRAPH_RENDERER_VERSION`，否则导出帧缓存命中旧实现的帧。

---

## 9. 依据的源文件

以下文件均在本文撰写时逐段通读核对（行号以当前 dev 分支为准）：

**节点模型与图引擎**
- `src/features/composite/nodes/base_node.py`（TextureResult/ParamDef/Port/Connection/Node，修订跟踪与 Effect Marker 协议、ROI 接口、scene 旁路、序列化）
- `src/features/composite/core/node_engine.py`（NODE_REGISTRY/NODE_CATEGORIES、NodeGraph 图操作、snapshot()、to_dict/from_dict、运行时迁移、动态注册）
- `src/features/composite/core/graph_migration.py`（COMPOSITE_SCHEMA_VERSION=11 与 v1→v11 迁移链、spawn_3d_scene_children）
- `src/features/composite/core/graph_snapshot.py`（NodeSnapshot/ConnectionSnapshot/GraphSnapshot 不可变快照契约）
- `src/features/composite/core/node_capabilities.py`（节点能力表与分类单一事实源）
- `src/features/composite/core/node_plugin.py`（NodePluginManager 发现目录）

**求值与调度**
- `src/features/composite/render/render_scheduler.py`（RenderRequest/RenderPlan/NodePlan、plan()、七条脏检测规则、ROI/LOD/tiling、请求时效）
- `src/features/composite/evaluators/base_evaluator.py`（_evaluate_snapshot 主循环、_evaluate_node 分派、_resolve_output、TileContext、动态源绕过、节点预览）
- `src/features/composite/evaluators/graph_evaluator_mixin.py`（13 个 mixin 的组合层）
- `src/features/composite/render/gpu_resource_pool.py`（用途分池、格式分层、VRAMMonitor、VideoSlotPool、fence、估算）
- `src/features/composite/render/cache_manager.py`（compute_key 键构成、CacheEntry/LRU/pin，节选核对）

**Effect Catalog**
- `src/features/shared/effects/effect_catalog.py`（三来源聚合、EffectSpec 构建、DCTL 参数映射、单例 API）

**渲染输出链**
- `src/features/composite/render/composite_renderer.py`（渲染线程生命周期、命令队列、_handle_evaluate_graph 签名跳过、_render_to_output 三缓冲、自驱播放、3D 快速路径）
- `src/features/composite/render/composite_render_worker.py`（CompositeRenderWorker/Exporter 线程模型）
- `src/features/composite/render/composite_render_output.py`（模式检测、范围解析、render_video/render_image_sequence、色彩编码）
- `src/features/composite/render/composite_frame_renderer.py`（无头逐帧渲染、反序列化缓存、渲染器版本键）
- `src/features/composite/render/composite_export_renderer.py`（一次性求值、缓存隔离、tile 策略、ExportResult）

**佐证审查报告（2026-09-28 批次，batch-044 ~ batch-115 composite 系列）**
- core：`docs/audit/2026-09-28/batches/batch-047-features-composite-core.md`（graph_migration 等）~ `batch-055-features-composite-core.md`（node_engine 见 batch-051）
- evaluators：`batch-057` ~ `batch-061-features-composite-evaluators.md`
- nodes：`batch-063` ~ `batch-077-features-composite-nodes.md`（base_node 见 batch-064）
- render：`batch-079` ~ `batch-085-features-composite-render.md`（renderer/scheduler/pool/output/worker 分布于 batch-079 ~ batch-084）
