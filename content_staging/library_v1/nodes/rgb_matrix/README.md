# C02 lib1_rgb_matrix —— 3×3 通道矩阵节点

> 状态：**component_ready**（节点类草案 + marker；dispatch 未接线——接线前该
> marker 在 base_evaluator 属"未知 lib1_*"显式拒绝，不发布）。
> 台账：L00 §3 C02 行——Composite 无 3×3 串扰系数节点（Shuffle/CopyChannel/
> ChannelMerge 无串扰系数），.tglsl Channel_Mixer 有矩阵核可对照。

## 交付物

- `lib1_rgb_matrix_node.py`：Node 类草案（node_type `lib1_rgb_matrix`；marker
  协议与 lib1_nodes.py 逐字同构；9 矩阵系数 row{0,1,2}_{r,g,b} + bias vec3 +
  strength；tile_padding=0 点处理）。
- `references/rgb_matrix_ref.py`：CPU oracle（out = M·rgb + bias，strength 混合）。

## 算法 / 恒等语义

- 线性变换：out.r = row0_r·r + row0_g·g + row0_b·b + bias.r（逐行同构）；
  final = rgb + (out − rgb)·strength。
- **恒等**：默认 identity 矩阵 + bias 0 → 任意 strength 精确恒等；
  strength=0 → 任意 M/bias 精确恒等（含 HDR，无 clamp）。

## 验证现状

- marker 协议/参数快照/无效输入返回 {} /oracle↔手算（≥3 独立向量）/恒等
  ——已测（tests/content_library_v1/test_c02_rgb_matrix.py）。
- **未验**：evaluator dispatch、shader_library、NODE_REGISTRY、capability、
  真实渲染（全部 L04 接线期）。
