# F7.3 · 复杂节点图、缓存与 Render-in-Place — 验证台账

- 日期：2026-10-24；计划条目：§F7.3
- 测试：tests/composite/test_bake_service.py（15，含 F7.3 新 6）＋
  cache/scheduler 套件 158 全绿

## 1. 现状核验

- **缓存**：节点缓存键含 node_type/node_id/params/disabled/render_mode
  + 预览/导出缓存隔离（test_composite_cache_key 锁定）；帧保留策略/
  策略/命中测试族既有；参数变化只重算受影响子树——render_scheduler
  dirty cone 计划测试既有（dirty_node_ids 精确域断言）。
- **RIP（bake_service）**：plan/render/rewrite/undo/cancel/失败部分
  文件报告既有（AUD-4600/4602/4603：半接线拒绝/二级端口拒绝/标签
  保全）；unbake 往返。
- **缺口**：烘焙未绑定源签名——上游参数变化后**悄悄播放旧烘焙**
  （计划验收明文禁止）。

## 2. 本批交付

| 组件 | 内容 |
|---|---|
| 烘焙源签名 | `_bake_source_signature`：目标 + 上游锥（沿连接逆向闭包）的 {id,type,params,enabled} SHA256 指纹；烘焙时写入 bake 节点 `_bake_source_signature` 参数（随序列化往返） |
| 过期检测 | `bake_is_stale(graph, bake_node_id)`：上游任何参数/启用态变化 → True（旧烘焙不再被信任）；锥外变化 → False（精确失效域）；无签名旧烘焙 → None（不冒称）；目标已删 → None（unbake 语义另走） |

## 3. 验收 → 证据

| 验收 | 证据 |
|---|---|
| 更新原图时不悄悄播放旧烘焙 | `test_upstream_param_change_marks_stale`/`test_deep_upstream_change_marks_stale`（目标与上游锥变化 → stale）；`test_unrelated_node_change_not_stale`（下游/锥外不误报）；序列化往返（重开可用）；旧烘焙 None 不冒称 |
| 参数变化只重算受影响子树 | render_scheduler dirty cone 既有测试（dirty_node_ids 精确域） |
| 烘焙绑定图版本/源 hash | 源签名 = 上游锥参数指纹（时间域/格式绑定在序列 pattern+帧范围参数既有） |
| 缓存坏/模型缺失/后台烘焙取消可恢复 | bake 取消/失败部分文件报告既有 + RIFE 缺模型显式降级（F7.2） |
| RIP 与实时/导出一致 | bake 渲染经同一 evaluate 回调 + 预览/导出缓存隔离（键含 render_mode） |
| 不为扩节点数增加未接线注册项 | 核验声明：节点注册表无空壳项（ai_inpaint 占位已在 F7.1 声明） |
| 内存/显存有界 | 帧保留策略/缓存策略测试族既有 |

## 4. 登记的后续片（不阻塞验收）

1. 过期检测的 UI 呈现（bake 节点徽标/求值警告日志——核心检测已就绪）。
2. 大型真实节点图的交互性能基准（dirty cone/缓存命中率实测——计划
   "用现有典型复杂图测"，测试图覆盖语义，性能数字待真实工程）。
3. 嵌套 comp/alpha/AOV 的 RIP 支持说明文档（能力边界声明）。
