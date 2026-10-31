# Film Grain（胶片颗粒）

- 插件文件：`plugins/Film_Grain.tglsl`（v1.0，Topos Team）
- 规格：`docs/plugins/2026-09-28/specs/Film_Grain.md`

## 用途

在 scene-linear 画面上叠加按亮度加权的单色胶片颗粒：噪声幅度在中间调
最高（感光乳剂密度响应），可通过参数把颗粒重新分配到阴影区和高光区。
用于给数字画面增加胶片质感、统一调色链质感、弱化渐变条带。

## 参数说明

| 参数 | 默认 | 范围 | 作用 |
|---|---|---|---|
| Strength | 0.35 | 0.0–1.0 | 颗粒总强度（峰值幅度 = 强度 × 0.12 scene-linear）；0 时插件零开销早退出 |
| Grain Size | 1.5 | 1.0–8.0 | 颗粒尺寸，单位为输出像素（格子采样步长）；1.0 为逐像素白噪声，增大得到团簇状粗颗粒 |
| Shadow Bias | 0.25 | 0.0–1.0 | 阴影偏置：往暗部追加 `(1-l)²` 加权颗粒，欠曝冲洗风格 |
| Highlight Bias | 0.15 | 0.0–1.0 | 高光偏置：往亮部追加 `l²` 加权颗粒，过曝乳剂堆积风格 |

## 典型用法

1. **细腻胶片感**（默认即为此档）：Strength 0.25–0.4、Grain Size 1.5、
   Shadow 0.2 / Highlight 0.1——中间调为主的自然颗粒。
2. **粗颗粒 archival 风格**：Grain Size 3.0–5.0、Strength 0.6，
   Shadow Bias 提到 0.4 让暗部颗粒明显。
3. **夜景/欠曝质感**：Shadow Bias 0.7–1.0、Highlight Bias 0.0，
   颗粒集中于阴影。
4. **叠加顺序**：置于调色链末端（颗粒不应被后续调色二次缩放）；与
   warm_tint、Film_Halation 组合成完整胶片模拟。

## 性能备注

- 纯 ALU：4 次 Dave Hoskins hash（无 sin/cos/pow）+ 双线性插值，
  **零纹理采样**，每像素约 30 条指令，远轻于 bilateral（32 tap），
  4K@60FPS 无压力，故未声明 `@preview_cost`。
- 首行早退出（`strength < 0.001`），关闭强度即零成本。
- deterministic：无时间输入，同帧结果恒定，DCTL 包装路径友好。
- 已知限制：无引擎时间 uniform，颗粒为静态图案（视频不逐帧跳动）；
  详见规格文档 §5。
