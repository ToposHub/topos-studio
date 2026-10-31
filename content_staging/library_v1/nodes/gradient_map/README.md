# C01 lib1_gradient_map —— 多停靠亮度映射节点（试点）

> 状态：**component_ready**（节点类草案 + marker；dispatch 未接线——见
> `patches/proposal_l04_node_seams.md`，合入前该 marker 落"未知类型透传"，不发布）。
> 台账：L00 §3 C01 行——Composite 无节点入口，复用 F16 共享核。

## 交付物

- `lib1_gradient_map_node.py`：Node 类草案（node_type `lib1_gradient_map`，
  marker 协议与 sharpen_node 等现有单输入效果逐字一致；PARAM_DEFS 与
  F16 manifest 参数一一对应；tile_padding=0 点处理）。
- `component_manifest.json`：L01 契约（oracle 复用 F16 共享核参考与手算向量）。

## 验证现状

- marker 协议/参数快照/无效输入返回 {} /参数喂 F16 oracle 有效——已测。
- **未验**：evaluator dispatch、shader_library 登记、NODE_REGISTRY/CATEGORIES、
  capability 表、graph 保存/迁移、CompositeExportRenderer 真实渲染（全部 L04）。
