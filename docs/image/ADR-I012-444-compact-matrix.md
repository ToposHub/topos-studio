# ADR-I012：444 图片体积矩阵

日期：2026-09-07  
状态：已实施

## 背景

真实 1920×1080 帧测试显示，GBR 4:4:4 12-bit 的 flat 矩阵输出约为
7.00–7.64 MB/帧。它接近视觉无损，超过图片工作流希望的约 4 MB/帧预算。
提高 qp 已到 63 的基础上限后，标准矩阵仍约 4.82 MB，继续压缩需要专门
的频率权重。

## 决策

新增 `qmatrix_id=2`（444 Compact），只允许 `pixel_format=2`、12-bit、
profile 5/6。该矩阵保留 DC 为 16，在 Standard 的频率形状上提高中高频
量化步长（luma 约 1.8 倍、chroma 约 1.5 倍）。它只用于图片 CLI 的两个
444 档；视频 MOV 导出继续使用 qmatrix 1。

图片档位调整为：

| 档位 | qmatrix | qp | 1080p 实测 |
|---|---:|---:|---:|
| 444 High | 2 | 63 | 4.18 MB |
| 444 Ultra | 2 | 58 | 4.46 MB |

两档仍保持 Ultra 的质量高于 High。实测素材为 `2K_h264.mp4` 第 0 帧，
源为 8-bit H.264，经 `gbrp12le` 转换后编码；整体 PSNR 分别约 52.4 dB
和 54.2 dB（12-bit 峰值 4095）。真实 12-bit 素材的体积和 PSNR 需要另行
标定，不能由这张 8-bit 上转换素材外推。

## 影响

- `qmatrix_id=2` 写入帧头，旧解码器会明确拒绝，不会误读为 Standard；当前
  项目按开发期策略直接采用新矩阵。
- 解码使用现有量化表查找和逆变换路径，没有新增解码分支或额外像素拷贝。
- `toos verify --deep`、图片 tier 回归和 qmatrix 单元测试覆盖新矩阵；矩阵
  数值同步登记在 `native/topos_codec/docs/bitstream_spec_v1.md` 附录 A.3。
