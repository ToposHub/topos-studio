# RD5-04：Topos 平面 GPU 语义统一

## 目标

让 Topos 的平面输入在 GPU 上传和 YUV→RGB 阶段保持同一套几何、色度位置和通道语义，避免高分辨率预览因为奇数尺寸、色度相位或 GBR 平面顺序产生边缘错位和颜色错误。

## 已落地

- 4:2:2 平面宽度按 `ceil(luma_width / 2)` 验证并创建纹理，覆盖奇数可见宽度。
- `topos_chroma_sampling_params()` 集中处理 Topos 4:2:2 的色度位置：left/top-left 使用半个亮度采样的归一化相位，center 保持零相位；4:4:4 和 GBR 不做色度相位修正。
- float/R8 路径继续使用 GPU 纹理采样，R16UI 路径继续使用 `usampler2D` 读取原始 `uint16` 码值；没有新增 CPU RGB 中间帧。
- GBR 平面按 Topos 的 `G/B/R` 顺序在 shader 中重排为 `R/G/B`，并绕过 YUV range/matrix 变换。
- YUV 的 matrix、range、bit depth 和 alpha 处理保持原有路径，不因 Topos 语义分支而改变。

## 边界与后续

本项只统一平面 GPU 语义，不声称已经完成最终 V7-B bitstream 的零拷贝 IOSurface 导入。外部 GPU 平面仍需由平台 capability gate 和 surface ownership protocol 保证生命周期；不满足条件时继续走受控 staging/copy 路径。
