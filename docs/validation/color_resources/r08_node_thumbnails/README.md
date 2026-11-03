# R08 留档：逐节点已处理缩略图（2026-11-04）

## 实现

- `src/app/controllers/node_thumbnail_evaluator.py`（`ColorNodeOutputPreview`）：
  - **demand 驱动**：可见 Corrector/Compound 卡片（上限 12）+ 去抖合并 +
    限频；缓存键 = (node_id, graph_revision, size)（资源指纹随 revision
    轴间接覆盖；上游编辑 → revision 前进 → 失效重算，不冒旧图）；
  - **独立 worker 线程 + 独立 standalone GL 上下文**：GUI 线程零 GL。
    低分辨率（320×180，源经 bilinear 降采样）按 `viewer:<node>` 编译依赖锥
    （既有 compile_graph）+ ColorGraphExecutor 执行；LUT3D aux 纹理按内容
    缓存提供（R05 强度 uniform 走通用上传），插件经 ShaderPipeline 真实
    编译执行；Compound 目标重映射到内部链尾（编译器内联展开语义）；
  - **世代守卫**：`set_identity` 身份不变时幂等（周期驱动安全）；切
    clip/换栈世代 +1，在途与晚到全部丢弃；失败 `evaluation_failed`
    （fail-visible 日志 + UI fallback 到源缩略图——视图层 per-node 永不
    覆盖、源图不被冒充）；
  - **与 R07 分离**：本通道只做 node-output，LUT 浏览器候选图不混入。
- 主窗口接线：600ms 定时驱动（面板可见 + 源缩略图就绪 + 权威图存在时按
  权威节点 id 过滤请求）；`node_thumbnail_ready` → `view.set_thumbnail`。
- 验证脚本新增 `node_thumbnails` 阶段：等待已评估缩略图，替换
  `per_node_rendering` 验收状态。

## 测试与证据

- `tests/app/test_node_thumbnail_evaluator.py`（4 项）：非 identity LUT 节点
  vs 明显 grade 节点 vs 源三方可区分；同 revision 重请求全缓存命中（零
  worker）；上游编辑失效重算；世代切换丢弃晚到；无图输入 fail-visible。
- 原生验证：`per_node_rendering=evaluated_node_thumbnails`、
  `node_thumbnail_count=2`、status passed（21.5s；scope 无致命事件）——
  **R08 完成后替换 source_thumbnail_only 验收状态**（计划 R08 出口）。
- 回归：node editor view/panel + live UI 38 passed。

## 尚未支持

- 独立分支的指纹级缓存复用（当前 revision 轴保守失效——正确性优先，
  优化随 N06 节点缓存策略统一）；执行器有效输出 lease 直读（AsyncRenderer
  内联通道，N01 全出口时评估）。
