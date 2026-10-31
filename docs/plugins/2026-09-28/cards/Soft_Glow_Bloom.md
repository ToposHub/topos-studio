# Soft Glow Bloom · 柔光泛光

> 阈值式柔光/泛光插件卡片 · `plugins/Soft_Glow_Bloom.tglsl` · v1.0 · Topos Team

**一句话**：提取高于亮度阈值的高光，扩散成柔和光晕后按强度叠加回画面——夜景点光源辉光、逆光轮廓光、高光柔化一步到位。

| | |
|---|---|
| 类别 | 光学/氛围 · Glow & Bloom |
| 色彩空间 | scene-linear 输入/输出（HDR 安全，全程不 clamp） |
| 参数 | Threshold（阈值）· Glow Radius（泛光半径）· Blend Intensity（混合强度） |
| 性能 | ≤33 纹理 tap · ≤8 三角函数 · 1 pow · `@preview_cost medium` |
| 兼容 | OpenGL / glsl_fragment · float16 · deterministic |

## 参数速查

| 参数 | 默认 | 范围 | 效果 |
|---|---|---|---|
| Threshold 阈值 | 0.5 | 0.0–1.0 | 提取门限（映射到线性亮度 0.25–2.5，含 HDR 超白段）；调高只让更亮的高光发光 |
| Glow Radius 泛光半径 | 0.5 | 0.0–1.0 | 扩散距离（图像宽度 0.4%–3.0%，4K 约 15–115 px）；同时自动增减模糊 pass 数（1–4） |
| Blend Intensity 混合强度 | 0.5 | 0.0–1.0 | 泛光能量；0 时插件近乎零开销直通 |

## 使用建议

- **夜景霓虹/点光源**：Threshold 0.55–0.7，Radius 0.6–0.8，Intensity 0.4–0.6。
- **逆光人像轮廓光**：Threshold 0.6–0.8，Radius 0.3–0.5，Intensity 0.3–0.5。
- **整体梦幻柔化**：Threshold 0.25–0.4（低门限让更多中亮部参与），Radius 0.4–0.6，Intensity 0.3。
- 与对比度/曲线类上游调色叠加安全：效果以增量（delta）方式应用，不覆盖已有调色。
- 泛光自带 25% 去饱和整形，高饱和光源不会产生刺眼的色边 halo。

## 技术要点

- 亮部提取用 Rec.709 线性亮度上的**软膝 smoothstep**，无硬阈值带状伪影；
  负亮度自然权重为 0，超白（>1.0）高光按原始能量完整进入泛光。
- 模糊为**常量循环上限的多 pass 环形近似**：`const int MAX_BLUR_PASSES = 4`
  硬上限，radius 滑杆通过 per-pass sub-radius 与 break 收敛控制有效宽度；
  黄金角相位旋转的 8-tap 环形 pass，8 tap 达到接近 16 tap 的圆盘覆盖。
- 分辨率无关：半径按图像宽度百分比映射，预览缩略图与 4K 导出视觉一致。
- 相关规格/验证：`docs/plugins/2026-09-28/specs/Soft_Glow_Bloom.md`。
