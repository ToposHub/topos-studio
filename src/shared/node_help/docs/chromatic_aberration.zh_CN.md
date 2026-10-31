# Chromatic Aberration 镜头色差

径向 RGB 通道错位模拟真实镜头的色散：R/B 通道沿径向独立偏移（G 不动）。`边缘增强` 让偏移随离心距离增大（中心保持干净）；为 0 时偏移恒定。tile padding 按最大偏移声明。

- **R 偏移 / B 偏移**（像素，可负=反向）：预览模式自动减半。
- **中心 X / Y**：径向中心（UV）。
- 与 lens_distortion / vignette / lens_flare 组合成"镜头模拟链"。
