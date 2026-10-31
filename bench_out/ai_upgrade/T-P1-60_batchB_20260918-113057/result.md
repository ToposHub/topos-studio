# T-P1-60 批次 B：Composite 图结构 AI 操作

- 日期：2026-09-18；edit_service 图操作从 5 种扩展到 9 种：
  add_node / remove_node / connect / disconnect / set_param +
  **duplicate_node / set_output_target / group / ungroup**（后两者协议层
  此前已声明但服务层不实现——"声明可执行应用必炸"缺陷修复）。
- 协议对齐：composite_plan.SUPPORTED_NODE_OPS 同步扩展；服务层 ⊆ 协议
  契约测试钉住（M3.1 同一真相）。
- 语义：duplicate 克隆类型+全部参数、不克隆连接（可预测计划）、位置
  偏移 40px；set_output_target 镜像页面 viewer 状态键（normalize 后）；
  group/ungroup 走 graph.create_group/remove_group（节点保留）。
- 单 plan 多 op 单 undo 组（复用既有 begin_group 机制），失败整体回滚。
- 测试 8 项（tests/composite/test_graph_ai_ops_extended.py）+ 协议回归
  144 项通过（script_graph_ops/edit_service/commands/undo）。
