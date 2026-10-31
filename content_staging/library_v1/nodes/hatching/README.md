# C07 lib1_hatching —— 排线节点（复用 F14）

> 状态：**component_ready**（节点类草案 + marker；dispatch 未接线，不发布）。
> 台账：L00 §3 C07 行——无排线节点；复用 F14，像素比例/分辨率一致。

## 交付物

- `lib1_hatching_node.py`：Node 类草案（参数表与 F14 oracle DEFAULTS
  逐项一致；tile_padding=0 程序几何）。

## 复用关系 / 恒等语义

- **单一事实源**：oracle 直接复用 `references/hatching_ref.py`（F14 共享核，
  不复制实现）；节点 marker 参数快照直接喂 F14 oracle 有效（透传已测）。
- 墨密度 D = 1−clamp(luma,0,1)；层 1 沿 angle 方向条带、层 2 正交半密度；
  cov = 1−smoothstep(h−AA, h+AA, |fract(b)−0.5|)；out = mix(paper, ink, cov)。
- **恒等**：amount=0 精确恒等。

## 验证现状

- marker 协议/参数透传至 F14 oracle/恒等/极暗满墨锚点——已测
  （tests/content_library_v1/test_c07_hatching.py；F14 全量语义见
  test_f14_hatching.py，不重复）。**未验**：L04 接线全家。
