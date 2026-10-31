# ADR-I015：O2 专业画质 Profile 与无损边界

日期：2026-09-08  
状态：v1 contract/reference 完成；大规模真实素材质量矩阵 deferred 到 O0 corpus 完整生成

## 固定语义

| 词汇 | 允许的含义 |
| --- | --- |
| Lossless | 解码样本逐位等于参考样本；v1 无 Lossless profile（profile 3 已于 2026-09-13 改派 RAW，ADR-I008——本文档写作时点的 reserved 陈述已被取代） |
| Visual-Lossless | 质量报告中的产品档位描述，必须附 PSNR/SSIM/ΔE2000、最差样本和 Alpha 误差，不能替代 bit-exact |
| Lossy/quantized | 允许样本误差，并报告误差与体积；422/444 tiers 属此类产品质量预设 |

当前 v1 的数学无损入口是显式 `--qp 28`（不进入 tier 菜单、不写成
`image_profile=3`）；`--tier` 只表达质量/体积预设。Profile、tier、qmatrix、
QP、像素格式和 reserved 边界由 `docs/image/capability_manifest.json` 统一登记。

## 可重现工具

- `scripts/validate_topos_image_profiles.py`：检查 Lossless reserved、422 档位单调性、
  444 GBR 约束和必需字段。
- `scripts/measure_topos_image_quality.py reference candidate`：输出 bit-exact、PSNR、
  全局 SSIM、RGB 最大误差、ΔE2000、Alpha 误差和 quality class；数学无损的 PSNR
  用 JSON `null + bit_exact=true` 表示，避免非法 Infinity。

## 门禁

随机 10-bit GBR 样本在 `--qp 28` 下通过逐样本比较；`image_profile=3` 明确返回
reserved/unsupported。当前仍需按 O0 真实 1080p/2K/4K corpus 补齐每个 tier 的最差
样本、冷/热缓存、编码时间和码率报告后，才可把 Visual-Lossless 作为发布承诺。
