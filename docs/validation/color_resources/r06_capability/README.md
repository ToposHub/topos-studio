# R06 留档：GL/CPU/Metal/bake 能力与输出一致（资源维度，2026-11-04）

## 实现（资源维度增量；执行器/租约/池化主体为前轮 B16.5 交付）

- **快照资源指纹**（`color_graph_snapshot.py`）：`compute_resource_fingerprint`
  ——每个 Corrector 的**有序**插件实例 (plugin_id/instance_id/enabled/params/
  注册表内容哈希) + LUT 引用/强度，canonical JSON sha256；进
  `ColorGraphSnapshot.cache_key()`。插件文件磁盘热更（同 graph_revision）
  现在驱动 plan/纹理缓存失效（此前只靠 revision，漏资源内容轴）。
- **能力预检**（`color_graph_preflight.py`）：逐 Corrector 资源可用性
  （resource_unknown / shader_missing / ai_model_missing / lut_unresolvable /
  instance_malformed），机器可读码，不执行渲染；R11 诊断/N01 出口门控消费。
- **bake 异步协议**（`color_bake_protocol.py`）：`ColorBakeRequest`（typed
  target + graph/source revision + spec）+ `ColorBakeBroker`（submit /
  cancel_for_graph(revision 前进) / cancel_for_source / 显式 cancel /
  accept_result(verify 晚到守卫——接 R04 令牌)。执行体（ONNX/离线 worker）
  由宿主后续接入——协议/取消/接受契约本轮固化并有测试。
- **GL 消费**：执行器 `prepare_corrector` 走完整 composition（R03–R05 的
  实例/强度/顺序投影直接生效）；bypass 直通、取消/失败无半帧发布、
  deferred release/lease/池化沿用 B16.5；CPU/Metal 对插件/HSL/LUT/空间
  效果维持显式拒绝（B12.1 目录 + production_layer_domain_errors）。

## 测试（tests/color/test_color_graph_resource_execution.py，13 项）

- 指纹：实例增/参数/重排/启用翻转/LUT 引用与强度各自驱动指纹变化；
  **插件内容热更（同 revision）失效缓存**，无关插件哈希不影响；快照
  cache_key 含指纹。
- 预检：未知插件/AI 缺模型/LUT 不可解析/畸形载荷 → 对应码；干净图零诊断。
- bake：submit/accept/spec 共用；图 revision 前进取消低 revision 在途任务；
  源内容变更取消；已取消/晚到/verify 失败一律丢弃（幂等）。
- GL：**两个非交换 FX（通道交换 ⊕ 仅 R 加倍）两种实例顺序输出不同**（数值
  级非交换证明）；重复实例执行两次；坏 shader 源 → `shader_compile_failed`
  fail-visible 无半帧。
- 回归：gl_execution/cancel_resources/backend_capabilities/plan_cache +
  本文件 = 39 passed；原生验证 passed。

## 尚未支持（后续轮次）

- bake 执行体（ONNX 管道）接入与 bake 资源随 command/工程的生命周期（N01/R12
  门槛件）；CPU/Metal LUT3D/插件域（N14，按 R05 规格实现）；全出口矩阵（N01）。
