# Halation_Extended 插件卡片

| 项 | 值 |
|---|---|
| plugin_id | `Halation_Extended` |
| 文件 | `plugins/Halation_Extended.tglsl`（v1.0，Topos Team） |
| 显示名 | Halation Extended / 扩展光晕 |
| 分类 | 高光扩散 · 晕影（glow / halation） |
| 色彩空间 | scene-linear 进 / scene-linear 出（HDR 安全，float16） |
| 性能 | 13 纹理 tap/像素、无三角函数、首行早退出；未声明 preview_cost（低于未降级声明的 bilateral 64-tap 基准） |

## 一句话

高光扩散晕影：软膝阈值挑出高光，ADR 交替方向环核向外扩散，
HSV 色相偏移旋钮把经典红晕转到任意色调。

## 参数速览

| 参数 | 默认 | 范围 | 说明 |
|---|---|---|---|
| Strength 强度 | 0.5 | 0.0–1.0 | 晕染发光强度（1-(1-s)^1.5 曲线，峰值增益 1.5） |
| Radius 扩散半径 | 0.5 | 0.0–1.0 | 图像宽度 0.4%–3.0%（4K 等效 15–115 px），分辨率无关 |
| Hue Shift 晕染色相偏移 | 0.0 | -0.5–0.5 | 0 = 经典胶片红锚点；±0.5 覆盖全色环（HSV hue 重锚定） |
| Highlight Threshold 高光阈值 | 0.55 | 0.0–1.0 | 仅高亮的软膝阈值（scene-linear luma；HDR > 1.0 满强度） |

## 与 Film_Halation 的差异定位

两者同属 halation 题材但**实现路线完全独立，可共存于同一效果链**：

- **模糊核**：Film_Halation 的规划路线是内置 `fast_blur` + `blur_radius`
  （盒式映射）；Halation_Extended 用自研 **ADR 交替方向环核**——3 环 × 4 tap
  方向交替（轴 → 对角 → 轴）去相关采样格点，环权重前置加载
  （1.0 / 0.65 / 0.45），紧核心 + 长扩散尾，单遍 13 tap。
- **色调路径**：Film_Halation 是 `enable_tint` 开关 + 固定红染；
  Halation_Extended 是 **HSV 连续可调**：色相锚定经典红后按 `hue_shift`
  旋转，饱和度下限 0.55 保证读作"有色晕影"而非白 bloom，HDR 能量原样通过。
- **高光选择**：Film_Halation 无阈值控制；Halation_Extended 提供
  `threshold` 软膝（smoothstep，无硬截止振铃），并逐 tap **先掩蔽后累加**，
  让光能量从亮区携带进暗区。
- **强度**：Film_Halation 无独立强度；Halation_Extended 的 `strength` 走
  曲线整形 + 增量式加性光（delta 输出，叠加不破坏上游调色）。

## 适用场景与组合建议

- 夜景霓虹、车灯、逆光人像的高光溢出氛围；数字素材加"胶片感"晕影。
- 想要非经典色调时把 Hue Shift 转到暖橙（-0.05 ~ -0.1）或青蓝（±0.5 附近）
  得到风格化冷晕。
- 建议置于调色链后段；与 warm_tint 组合可做整体暖化 + 局部晕染分层。
