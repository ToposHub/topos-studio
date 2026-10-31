# 提案：L04 节点执行/能力接缝（C01/C17 试点，给 Mac 主集成者）

> 状态：提案文本 + 节点类草案（component_ready），未改任何生产文件。

## 现状接缝（L00 盘点冻结）

- 未知 `_effect_type` 警告后**透传返回输入**（base_evaluator.py:2312-2314）——
  "只写 Node 类"会静默 no-op 冒充完成（计划红线）。
- 节点类 → GPU 的完整链：nodes/<x>_node.py marker → shaders.py 常量 →
  evaluators/shader_library.py 登记 → 具体 mixin `_apply_x` →
  base_evaluator `_dispatch_effect` 分支 → NODE_REGISTRY/NODE_CATEGORIES →
  `_NODE_CAPABILITY_TABLE`。
- dispatch 顺序陷阱：生成器/多输入效果必须排在单输入门（L2173）之前；
  C01/C17 是单输入，排在门后即可；C12/C21 合入时另开多输入/多输出子门。

## C01 lib1_gradient_map（单输入，复用 F16 核）

1. 共享核：F16 分段求值 GLSL 放 shared effects 层
   （`src/features/shared/effects/`，或由集成者定稿摆放——上层不导入 Composite 内部）；
   Composite 侧 `_LIB1_GRADIENT_MAP_FRAGMENT` 引用同一分段语义
   （4 停靠标量 uniform：u_stop_0..3 vec3、u_pos_1/u_pos_2 float、
   u_interp int、u_mix float——**不引入数组**，与 .tglsl 试点 ABI 一致）。
2. evaluators/shader_library.py 登记 `("lib1_gradient_map", (vertex, fragment))`；
   filter_evaluator 加 `_apply_lib1_gradient_map`；`_dispatch_effect` 单输入段加分支
   （`elif effect_type == "lib1_gradient_map":`）。
3. node_engine.NODE_REGISTRY（"lib1_gradient_map": Lib1GradientMapNode）+
   NODE_CATEGORIES["Filter"]；node_capabilities._NODE_CAPABILITY_TABLE 加条目：
   action_type=effect、roi_policy=identity、tile_padding=0、fusible=True（点处理）。
4. 节点类本体取 `content_staging/library_v1/nodes/gradient_map/lib1_gradient_map_node.py`
   （发布路径 plugins/node_plugins/topos_library_v1/ 或 nodes/ 静态合入，由集成者定）。

## C17 lib1_edge_extend（单输入 + alpha 语义）

1. shader：`_LIB1_EDGE_EXTEND_FRAGMENT` = 每轮扩张一个 pass
   （nodes/edge_extend/edge_extend.glsl 草案）；evaluator 乒乓 FBO 跑
   `int(radius)` 轮（radius 0–8 有界常量上限），原 alpha 另存一张纹理，
   output pass 按 output_mode 组装。solid 标志复用 alpha 通道（0/1 整数语义）。
2. dispatch 分支同 C01；capability：roi_policy=identity、
   **tile_padding=radius（动态）**——tile 分块由 tile_padding_computer 消费；
   fusible=False（乒乓多 pass 不参与融合）。
3. 验收差量样片：alpha_fringe_hair 夹具去黑边/白边前后对比
   （tests/content_library_v1 已有 CPU oracle 语义测试，GPU 对拍进 L04 集成测试）。

## 纪律：未知 lib1_* marker 不允许透传

`_dispatch_effect` 的未知分支保持既有"警告+返回输入"（兼容旧工程），但增加
**白名单校验**：`_effect_type.startswith("lib1_")` 且不在已实现 dispatch 集合时
**升级为精确报错**（raise/渲染诊断，节点保留、参数保留、输出标记失败），
防止"类注册了、dispatch 没接"的静默 no-op（计划："无实现精确报错，不能以
透传冒充完成"）。

## 发布顺序

先 C01（依赖 L02 F16 点处理子门——同一核已过 GL 编译）→ 后 C17（alpha 子门，
需乒乓 FBO 调度与 tile_padding 动态值验证）。C12/C21 等 L17 批次再开
多输入/多输出子门。
