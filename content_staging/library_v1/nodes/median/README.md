# C15 lib1_median —— 小核中值/百分位节点（复用 F19）

> 状态：**component_ready**（节点类草案 + marker；dispatch 未接线，不发布）。
> 台账：L00 §3 C15 行——无中值节点；依赖 F19 共享核（再导出，禁双份漂移）。

## 交付物

- `lib1_median_node.py`：Node 类草案（参数表与 F19 oracle DEFAULTS 逐项
  一致；tile_padding = 核半径 1/2）。

## 复用关系 / 恒等语义

- **单一事实源**：`references/median_ref.py` 只再导出 F19
  `references/median_rank_ref.py`（不复制实现）；节点 marker 参数快照
  直接喂 F19 oracle 有效（透传已测）。
- 3x3/5x5 邻域排序取第 k 个（k = round(percentile·(n−1))，edge 边界）；
  RGB 逐通道 / Luma 档比例缩放；Delta 混合。
- **恒等**：amount=0 精确恒等（含 HDR）；平坦场任意模式恒等。

## 验证现状

- marker 协议/参数透传至 F19 oracle/恒等/椒盐脉冲手算——已测
  （tests/content_library_v1/test_c15_median.py；F19 全量语义见
  test_f19_median_rank.py，不重复）。**未验**：L04 接线全家。
