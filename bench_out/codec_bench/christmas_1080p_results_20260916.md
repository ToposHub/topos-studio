# Christmas.mp4 → Topos 1080P 六档编解码速率（2026-09-16）

**素材**：`/Users/testuser/Desktop/Christmas.mp4`（H.264 8-bit 4:2:0，
1920×1080 @ 29.97fps，901 帧 ≈ 30.05s）——源即 1080P，无缩放。

**机器**：Apple M1 MacBook Air（4P+4E，8GB，无风扇），
Release dylib（git `c39da5949`，本机 `native/topos_codec/build/release/`），
8 切片线程，熵编码 rans2（产品默认）。

**口径**（对齐 `docs/codec/topos_codec_speed_matrix_2026-09-15.md`）：
- 编码走产品导出路径 `ToposVideoEncoder`（首帧 sized 搜索 + 帧间反馈单遍
  码控），计时只累计 codec 调用；源 H.264 解码、pix_fmt 转换
  （yuv422p10le / yuv444p10le / yuv444p12le）、MOV 封装不计入。
- 解码为常驻 `ToposDecoder` + 复用输出平面（`decode_views`），
  包读取不计入。
- 峰值 = 4 轮取最优；典型 = 轮次中位。无风扇机身持续满载降频
  35–65%（复验实测），且测试时机器有日常负载（ToDesk/浏览器等），
  轮次间波动 ±10–15% 属正常。

| 档位 | 像素格式 | 编码峰值 fps | 编码典型 fps | 解码峰值 fps | 解码典型 fps | 码率（文件实占） | 平均 qp | 文件大小 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 422 Proxy | yuv422p10le | **185.0** | 171 | **89.3** | 86 | 18.3 Mbps | 77.9 | 68.6 MB |
| 422 LT | yuv422p10le | **122.8** | 120 | **80.3** | 70 | 59.3 Mbps | 62.8 | 223.0 MB |
| 422 Standard | yuv422p10le | **104.7** | 103 | **69.3** | 66 | 91.8 Mbps | 55.3 | 345.1 MB |
| 422 HQ | yuv422p10le | **88.3** | 84 | **65.8** | 63 | 151.5 Mbps | 45.2 | 569.2 MB |
| 4444 | yuv444p10le | **53.5** | 50 | **42.3** | 40 | 249.6 Mbps | 39.4 | 938.0 MB |
| 4444 XQ | yuv444p12le | **46.0** | 42 | **38.6** | 36 | 363.7 Mbps | 32.4 | 1366.7 MB |

**观察**：
- 六档 1080P 编码全部高于 29.97fps 实时（最重的 4444XQ ≈ 1.5× 实时）；
  解码全部高于实时播放（4444XQ ≈ 1.3×）。
- 码率与档位 target_bpp 定标吻合（Proxy +1.3% ~ XQ −6.9%，
  档位反馈码控长期均值特性）。
- 编码确定性：4 轮全档输出文件逐字节同尺寸、同 qp 序列。

**产物**：`christmas_1080p_<tier>.mov`（6 个，共 ≈3.5GB，可删）；
原始数据 `christmas_1080p_results_20260916.json`；脚本
`bench_christmas_1080p.py` / `bench_bestof.py`（同目录）。
