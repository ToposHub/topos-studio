# C04 lib1_solarize —— 阈值折返节点（复用 F17）

> 状态：**component_ready**（节点类草案 + marker；dispatch 未接线，不发布）。
> 台账：L00 §3 C04 行——Composite 无 solarize（只有 Invert）；复用 F17。

## 交付物

- `lib1_solarize_node.py`：Node 类草案（参数表与 F17 oracle
  `references/solarize_ref.py` 的 DEFAULTS 逐项一致；tile_padding=0）。

## 复用关系 / 恒等语义

- **单一事实源**：oracle 直接复用 `references/solarize_ref.py`（F17 共享核，
  不复制实现）；节点 marker 参数快照直接喂 F17 oracle 有效（透传已测）。
- 折返：fold(v) = mix(v, 2·thr−v, smoothstep(thr−h, thr+h, v))，Luma 档亮度
  折返 / RGB 档逐通道；线性域无 clamp（HDR 可折出负值，显式声明）。
- **恒等**：amount=0 精确恒等（含 HDR）。

## 验证现状

- marker 协议/参数透传至 F17 oracle/折返锚点手算——已测
  （tests/content_library_v1/test_c04_solarize.py；F17 全量语义见
  test_f17_solarize.py，不重复）。**未验**：L04 接线全家。
