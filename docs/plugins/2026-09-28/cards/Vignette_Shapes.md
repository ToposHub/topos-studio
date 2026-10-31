# 效果卡片：Vignette Shapes（多形状暗角）

| | |
|---|---|
| plugin_id | `Vignette_Shapes`（plugins/Vignette_Shapes.tglsl，v1.0） |
| 分类 | 遮罩 / 暗角（纯逐像素数学，零纹理采样） |
| 一句话 | 椭圆 / 圆角矩形 / 真圆三种形状的电影感暗角，smoothstep 羽化 + 乘性强度混合 + 均值保持亮度补偿 |
| 色彩管线 | scene-linear in / scene-linear out；HDR 安全（乘性增益，全程零 clamp） |
| 预览成本 | `@preview_cost low`（ALU-only，无纹理 tap、无超越函数） |
| i18n | `plugins.Vignette_Shapes.name` + `.params.<var>`（片段：`../i18n/Vignette_Shapes.json`，zh_CN + en_US） |

## 参数

| 参数 | 控件 | 默认 | 说明 |
|---|---|---|---|
| Shape（形状） | select | Ellipse | Ellipse（随画面比例的内切椭圆）/ Rectangle（画面边框圆角矩形，角半径随羽化联动）/ Circle（aspect 纠正的几何真圆） |
| Feather（羽化） | slider 0–1 | 0.5 | 映射过渡半宽 w = mix(0.08, 0.55, feather)；过渡 50% 点恒在名义边界上，形状读数不随羽化漂移 |
| Strength（强度） | slider 0–1 | 0.5 | 乘性变暗：weight = 1 − strength·t；默认下椭圆角点全强度、边中点约 25% 变暗、中心为 0 |
| Luma Compensation（亮度补偿） | slider 0–1 | 0.0 | 0 = 经典暗角；1 = 按 E[t] 闭合式施加增益 1/(1 − strength·E[t])，整帧均值基本保持（实测偏差 ≤ 2%） |

## 算法要点

- 三形状统一"d = 1.0 名义边界"度量：椭圆 `length(q)`（q 空间，天然随画面比例）、
  矩形 `1 + 圆角盒SDF`（aspect 纠正空间，圆角像素级为真圆弧）、真圆
  `length(q.x·A, q.y)`（红线：径向效果乘 `u_aspect_ratio`）。
- select 分发为两条早返回 if 链（shape_distance / falloff_mean 各一），末分支
  else 兜底 = Circle；索引作函数参数传入（Radial_Blur_Pro 同款惯例）。
- 亮度补偿标定：E[t] 闭合式 = 椭圆 `0.207+0.131w`、矩形 `0.181(1+1/A)w`、
  真圆 `1−π/(4A)` 硬项 + 分段斜率项；开发期 1800² 网格数值积分标定，最坏误差
  ~0.036（竖幅中段），换算增益误差 ≤ 5%。
- 首行早退出 `if (strength < 0.001) return color;`；增益分母下限 0.25
  （gain ≤ 4）；smoothstep 边距下限 0.08（edge0 < edge1 恒成立）。

## 性能备注

- **零纹理 tap、零 for 循环、零 sin/cos/exp/log/pow**：每像素 1 次 length（或
  圆角盒 SDF：abs/max/min/length）+ 1 次 smoothstep + 十几次标量四则。
- 与同类对比：warm_tint 同为纯数学档；HighPass_Retouch 2 tap、
  bilateral_skin_smooth 32 tap × 两遍。本插件为 12 个新插件中最轻档，
  `@preview_cost low` 如实声明。
- 分辨率无关：效果只依赖 uv 与 `u_aspect_ratio`，预览与导出（任意分辨率）一致。

## 已验证

- headless 解析 + 数值验证 42/42 通过（`../tools/validate_Vignette_Shapes.py`）：
  正则镜像 plugin_manager 实态；`falloff_mean` 的 12 个标定常数从着色器文本
  正则提取后驱动逐像素仿真（与实现不脱钩）；E[t] 精度 max|Δ| = 0.018、
  均值保持 max|mean−1| = 2.0%；8 类人为破坏负向对照全部命中。
