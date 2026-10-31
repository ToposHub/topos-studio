# C03 lib1_despill —— 独立绿幕去溢色节点

> 状态：**component_ready**（节点类草案 + marker；dispatch 未接线，不发布）。
> 台账：L00 §3 C03 行——算法已存在于 Keyer 内（despill_strength/method，
> hidden），差量=独立节点化；对照 keyer_node.py / `_KEYLIGHT_FRAGMENT::
> remove_spill`（subtract=mix(主导通道, 其他通道最大值, strength)；
> scale=按幕色占比通道增益）。

## 交付物

- `lib1_despill_node.py`：Node 类草案（method/limit/strength；tile_padding=0）。
- `references/despill_ref.py`：CPU oracle（标准绿幕口径）。

## 算法 / 恒等语义

- m = max(r, b)；Δ = g − m（绿剩余，正=溢色）；
- **Subtract**：g' = g − strength·clip(Δ, 0, limit)——仅削峰、绝对量受
  limit 封顶（任务式 g' = g − g_spill·strength，g_spill = max(0, g−max(r,b))）；
- **Scale**：g' = g·(1 − strength·ratio)，ratio = clip(clip(Δ, −limit,
  limit)/max(g, 1e-6), −1, 1)——乘性双向拉向 max(r,b)（Keylight subtract
  mix 语义；g≈0 由 ratio∈[−1,1] 守护）；
- 两模式在 0 ≤ Δ ≤ limit 区**代数同值**（乘性展开即减法）；差异区：
  Δ<0（绿不足：Subtract 不动 / Scale 抬升）与 Δ>limit / g≈0 守护——
  测试显式钉住该等价与差异；
- **恒等**：strength=0 精确恒等（任意 method/limit，含 HDR）；r/b 与
  alpha 透传、无 clamp。

## 验证现状

- marker 协议/恒等/两模式差异与等价区/limit 封顶/ε 守护——已测
  （tests/content_library_v1/test_c03_despill.py）。**未验**：L04 接线全家
  （同 C02）。
