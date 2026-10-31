# TOPOS 真实素材编解码速度（新电脑）

**测试日期：** 2026-09-13　**平台：** macOS 15.3.1 / Intel x86_64

**硬件：** Mac Pro（MacPro7,1），Intel Core i9-12900T，16 核 / 24 线程，64 GB 内存；TOPOS native slice pool 使用 16 线程。AMD Radeon RX 6800 未参与此次 CPU codec benchmark。
**Native 构建：** 0.1.0 / x86_64-AppleClang，AVX2 + FMA (native CPU flags=9)。

编码计时覆盖 TOPOS 原生编码调用（含产品码控与输出 packet 生成），不含 ProRes/DNxHR 源片解码、平面打包和 MOV mux 写入；解码计时使用常驻 native decoder 与真实解码输出平面，预热调用不计时。码率按测得的 packet 字节数折算为 25 fps，含 TOPOS 每帧码流头、不含 MOV 容器开销。每项使用单次 120 帧样本；输入是各源片前 120 帧（4.8 秒），10-bit 4:2:2 四档来自 ProRes 422 HQ，10/12-bit 4:4:4 两档来自 DNxHR 444。

| 分辨率 | TOPOS 档位 | 输入格式 | 编码 fps | 解码 fps | 码率 @25fps | bpp | QP 首帧→均值 |
|---|---|---:|---:|---:|---:|---:|---:|
| 2K | 422 Proxy | `yuv422p10le` | 382.1 | 622.3 | 15.1 Mb/s | 0.291 | 87 → 83.0 |
| 2K | 422 LT | `yuv422p10le` | 246.5 | 359.0 | 51.5 Mb/s | 0.993 | 79 → 75.6 |
| 2K | 422 Standard | `yuv422p10le` | 185.6 | 254.2 | 81.5 Mb/s | 1.573 | 75 → 73.0 |
| 2K | 422 HQ | `yuv422p10le` | 132.1 | 210.1 | 133.7 Mb/s | 2.579 | 72 → 70.0 |
| 2K | 4444 (10-bit) | `yuv444p10le` | 65.5 | 123.3 | 221.3 Mb/s | 4.269 | 75 → 73.3 |
| 2K | 4444 XQ (12-bit) | `yuv444p12le` | 50.4 | 94.7 | 329.2 Mb/s | 6.350 | 76 → 74.1 |
| 4K | 422 Proxy | `yuv422p10le` | 161.4 | 211.3 | 60.5 Mb/s | 0.292 | 76 → 74.4 |
| 4K | 422 LT | `yuv422p10le` | 102.5 | 129.4 | 200.3 Mb/s | 0.966 | 69 → 67.3 |
| 4K | 422 Standard | `yuv422p10le` | 78.6 | 94.6 | 321.9 Mb/s | 1.552 | 66 → 64.6 |
| 4K | 422 HQ | `yuv422p10le` | 53.2 | 69.8 | 531.4 Mb/s | 2.563 | 64 → 61.5 |
| 4K | 4444 (10-bit) | `yuv444p10le` | 24.9 | 35.4 | 884.6 Mb/s | 4.266 | 71 → 70.9 |
| 4K | 4444 XQ (12-bit) | `yuv444p12le` | 21.4 | 31.3 | 1301.7 Mb/s | 6.277 | 69 → 69.3 |

4K 25 fps 编码观察：422 Proxy/LT/Standard/HQ 均高于 25 fps；4444 为 24.9 fps，接近实时；4444 XQ 为 21.4 fps，低于实时编码速度。以上编码速度未包含源素材解码和文件写入，因此 4K XQ 的完整转码吞吐会更低。所有 2K 档位均高于 25 fps。

## 现有 TOPOS MOV 完整顺序解码

使用 `ToposMediaSource.read_next_frame()` 顺序读取现有文件的全部帧；关闭 read-ahead，native decoder 分配 16 workers。计时包含 MOV packet 读取与帧输出，不包含 `open()` 的索引/元数据解析。文件来自测试素材目录，和上面重新编码的 120 帧档位样本是不同文件。

| 文件 | 分辨率 / 时长 | 帧数 | 文件大小 | 文件平均码率 | 解码速度 |
|---|---:|---:|---:|---:|---:|
| `2K_topos.mov` | 1920×1080 / 116.48s | 2912 | 2.71 GiB | 200.2 Mb/s | 134.6 fps |
| `4K_topos.mov` | 3840×2160 / 12.00s | 300 | 0.89 GiB | 639.3 Mb/s | 74.6 fps |

## 结果文件

- [完整 12 档位编解码 JSON](results.json)
- [现有 TOPOS MOV 完整解码 JSON](full_mov_decode.json)

编码样本从当前 `tests/test_videos` 源文件重新解出；临时 raw 文件已清理。源视频未修改。
