# C20 lib1_halation —— 高光散射节点（复用 F06）

> 状态：**component_ready**（节点类草案 + marker；dispatch 未接线，不发布）。
> 台账：L00 §3 C20 行——Glow 节点基础可用，专用 halation 复用 F06（与普通
> Glow 差量一致）。

## 交付物

- `lib1_halation_node.py`：Node 类草案（参数表与 F06 oracle DEFAULTS 逐项
  一致；tile_padding = R_px 4K 上界近似）。

## 复用关系 / 恒等语义

- **单一事实源**：`references/halation_ref.py` 只再导出 F06
  `references/multiscale_halation_ref.py` 的 `evaluate_degraded_single_scale`
  （**GPU 单 pass 等价口径**：单尺度 12-tap 黄金角环 + 逐 tap 软膝门控）；
  节点 marker 参数快照直接喂 F06 oracle 有效（透传已测）。
- **真多尺度多 pass 版**（E 场提取 → 双尺度扩散）待原生 renderer 多 pass
  接线后启用（known_limits 声明；oracle 侧 `evaluate_multiscale` 留备查）。
- 红重着色 scatter = (halo.r·(1+w), halo.g, halo.b·(1−0.5w))；加性叠加；
  R_px = max(2, W·(0.004+0.026·radius))。
- **恒等**：strength=0 精确恒等（含 HDR）。

## 验证现状

- marker 协议/参数透传至 F06 oracle/恒等/暗场零扩散手算——已测
  （tests/content_library_v1/test_c20_halation.py；F06 全量语义见
  test_f06_multiscale_halation.py，不重复）。**未验**：L04 接线全家。
