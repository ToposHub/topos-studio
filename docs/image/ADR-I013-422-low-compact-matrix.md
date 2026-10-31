# ADR-I013：422 Low 轻度体积矩阵

日期：2026-09-07
状态：已实施

## 背景

422 Low 使用 Standard 矩阵、qp=63 时已经达到编码器的 qp 上限，1080p 实拍
帧约 1.117 MB。Medium 使用同一矩阵、qp=61，约 1.365 MB。两档虽然大小
有差异，但 Low 的高频细节和 Medium 的观感接近，且 Low 仍有少量可压缩的
中高频系数。

## 决策

新增 `qmatrix_id=3`（422 Low Compact），只允许 YUV 4:2:2、10-bit、profile 3。
矩阵保留 DC=16，在 Standard 的频率形状上做约 1.08 倍 luma、1.05 倍 chroma
的轻度加权。422 Low 改为 `qm3 qp63`；Medium、High 继续使用 `qm1 qp61/58`。

同一张 1920×1080 H.264 第 0 帧的实测结果：

| 档位 | qmatrix | qp | 文件大小 | PSNR（10-bit 峰值） |
|---|---:|---:|---:|---:|
| 422 Low（原） | 1 | 63 | 1.117 MB | 46.93 dB |
| 422 Low（当前） | 3 | 63 | 1.081 MB | 46.49 dB |
| 422 Medium | 1 | 61 | 1.365 MB | 50.02 dB |

Low 比原实现小 3.2%，与 Medium 的体积差从 0.248 MB 增加到 0.284 MB；画质
损失约 0.44 dB，解码仍使用同一量化表查找、逆量化、逆变换和 slice 路径，
没有增加解码分支或像素拷贝。

## 影响与验证

- `qmatrix_id=3` 写入帧头，格式约束在 frame-header 校验层固定，其他像素格式
  使用该 id 会返回 `TC_ERR_UNSUPPORTED_MATRIX`。
- `toos encode --tier 422-low` 自动使用 qm3；显式 `--qmatrix` 仍可覆盖，
  便于 A/B 和回归测试。
- 已覆盖矩阵查表、frame-header 合法组合、未知矩阵拒绝、CLI tier 参数映射，
  并用 `toos verify --deep` 验证实测输出。
