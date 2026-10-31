# C08 lib1_crt —— CRT 扫描线/荫罩节点（参照 CRT_Scanlines）

> 状态：**component_ready**（节点类草案 + marker；dispatch 未接线，不发布）。
> 台账：L00 §3 C08 行——复用 `plugins/CRT_Scanlines.tglsl`；差量：辉光
> 组合+显示域声明未纳入（本批最小口径）。

## 交付物

- `lib1_crt_node.py`：Node 类草案（mode/scan_count/scan_strength/
  mask_strength；tile_padding=0 纯代数场）。
- `references/crt_ref.py`：CPU oracle（与 CRT_Scanlines.tglsl 逐式同构）。

## 算法 / 恒等语义

- 扫描线场（Scanlines/Full）：period = max(H/scan_count, 1)；tri =
  (1−|2·fract(px_y/period)−1|)²；scan_gain = 1−scan_strength·0.6·(1−tri)。
- 荫罩场（RGB Grille/Full）：3px 周期、相位互错 1/3 的三通道三角波；
  gain_c = 1 + (tri_c − 0.5)·(mask_strength·0.8)——连续域均值恰中性
  （tri 均值 0.5）；像素中心采样的离散相位集 s ∈ {1/6, 1/2, 5/6} 给出
  离散均值 1 + k/18（测试钉住该口径）；扫描线 tri² 均值 1/3
  （.tglsl 实际行为，oracle 镜像）。
- out = rgb·scan_gain·gain（乘性，HDR 安全不 clamp；alpha 透传）。
- **恒等**：scan_strength=mask_strength=0 精确恒等（含 HDR；.tglsl 首行
  早退同值）。

## 验证现状

- marker 协议/恒等/三角波锚点手算/荫罩离散均值钉住/模式开关差异——已测
  （tests/content_library_v1/test_c08_crt.py）。**未验**：L04 接线全家。
