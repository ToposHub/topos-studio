# X17 kaleido_bridge —— 角域折叠桥接（L14）

> 状态：**component_ready**（暂存 CPU 核；未接宿主）。
> 台账：L00 §2 X17 行——`plugins/Kaleido_Mirror.tglsl` 是**单输入**折叠镜像
  非转场（对照排除）；本核是双源转场且**两端原画语义**钉住。

## 算法

```
极角（宽高比修正，同 X01 口径）：θ = atan2(v-cy, (u-cx)·(W/H))，r 不变
扇区角 A_s = 2π/N；t = θ - radians(rotation)
折叠：m = mod(t, 2A_s)；a = min(m, 2A_s - m)（镜像折叠进 [0, A_s]）
      θ_f = rotation + a → 折叠采样点 (u_f, v_f) = 中心 + r·(cosθ_f, sinθ_f)
折叠强度：s_eff = strength · w(p)，w = 1-|2p-1|（三角窗：两端 0、p=0.5 峰）
  layer_X = (1-s_eff)·X(u,v) + s_eff·X(u_f, v_f)   （A/B 同窗同式）
换源窗：w_b = clamp(2p-1, 0, 1)（"B 侧自 p>0.5 淡入"：前半程纯 A 层）
  out = (1-w_b)·layer_A + w_b·layer_B
```

- **两端原画语义（钉住）**：p=0 → s_eff=0、w_b=0 → 精确 A（early return
  比特级）；p=1 → s_eff=0、w_b=1 → 精确 B。**中段折叠可见**：p∈(0,1)
  s_eff>0（如 p=0.25 时 0.5·strength）；p=0.5 折叠峰值恰在 A→B 交接。
  "不能始终残留万花筒"= 端点 s_eff 精确 0（w 窗保证）。
- **strength=1 且 p=0.5**：A 层 = 纯折叠采样 A(u_f)（无原位项）——测试用它
  做折叠公式的独立重算对拍。
- **采样域**：折叠点可出 [0,1]（远离中心/低 N）→ 双线性 clamp 声明。
- **单调（换源）**：w_b 随 p 单调；常色源逐像素值单调不回退。
- **几何保真**：折叠保持 r（径距不变），仅角向镜像——与 Kaleido_Mirror 的
  差量是双源 + 端点归零窗。

## 参数

| 参数 | 默认 | 范围 | 说明 |
|---|---|---|---|
| sectors | 6 | 3..12（int） | 扇区数 N（折叠进 2π/N） |
| rotation | 0.0 | -180..180（度） | 扇区基准角 |
| center_x / center_y | 0.5 / 0.5 | 0..1 | 折叠中心 |
| strength | 0.8 | 0..1 | 折叠强度峰值 |

## 交付物

`kaleido_bridge_core.py` / `README.md` / `hand_vectors.json`（1 组全帧）/
`component_manifest.json`。

## 验证现状

- CPU 核 ↔ 独立标量手算全帧对拍（N/rotation/center/strength 组合 diff ≤
  4e-8）；端点比特级；中段折叠可见、p<0.5 无 B 掺入、strength=0 dissolve
  退化、折叠点独立重算测试通过。
- **未验**：GPU/GLSL/MSL、预览/导出链。
