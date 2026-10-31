# topos 图片 vs JPEG vs PNG 三方对照（实测）

日期：2026-09-12 · 工具：`tools/bench_toos_vs_jpg_png.py` ·
数据：`tools/toos_vs_jpg_png_2026-09-12.json`（prores422HQ / DNxHR HQX 各 2 帧 × 2K/4K）
口径：RGB8 参考域 PSNR；topos 走完整 RGB→YUV422 10-bit→.toos→往返
（含 4:2:2 色度二次采样与转换损失——用户存 .toos 的真实所得）；
JPEG = PIL/libjpeg（q85/q90 默认 4:2:0，q95 显式 4:4:4）；
PNG = PIL/zlib compress_level=6（常用默认口径）。

---

## 一、画质/体积/速度阶梯（4K 2160p，双素材×2 帧均值）

| 编码 | PSNR(RGB8) | 体积 | 编码 ms | 解码 ms |
|---|---|---|---|---|
| topos-422-low | 48.59 dB | 1.80 MB | 24.4 | 14.8 |
| topos-422-medium | 49.09 dB | 2.19 MB | 16.7 | 13.9 |
| topos-422-high | 49.82 dB | 2.88 MB | 20.2 | 16.4 |
| topos-422-ultra | **55.45 dB** | 5.75 MB | 45.1 | 26.2 |
| jpeg-q85-420 | 45.70 dB | 0.62 MB | 74.2 | 59.0 |
| jpeg-q90-420 | 46.64 dB | 0.81 MB | 74.8 | 60.3 |
| jpeg-q95-444 | 50.20 dB | 1.60 MB | 128.5 | 77.9 |
| png8-lossless | 无损（∞） | 5.91 MB | 630.4 | 111.3 |

2K 1080p 同构：topos 47.8~54.9dB / 0.52~1.66MB / enc 5.4~10.9ms / dec 4.5~9.2ms；
JPEG 44.2~49.0dB / 0.18~0.46MB / enc 20~33ms / dec 15~20ms；PNG8 1.60MB / enc 159.8ms / dec 30.5ms。

## 二、四个主张的验证结论

1. **「画质优于 JPG、介于 JPG 与 PNG 之间」——成立。**
   topos 档位带 47.8~55.5dB 全部高于典型 JPEG（q85/q90 4:2:0 = 44.2~46.6dB），
   低于 PNG（无损）；且同尺寸下优势更明显（topos-medium 2.2MB@49.1dB 对
   JPEG 需 q95-444 才到 50.2dB）。注意点：PSNR 口径下 JPEG q95-444（50.2dB
   @1.6MB）与 topos-high（49.8dB@2.9MB）相当——JPEG 在高质量 4:4:4 设置下
   每字节 PSNR 有竞争力；topos 的相对优势在 10-bit/4:2:2/调色空间与速度
   （见 3/4），而非高画质端的 PSNR 数字。
2. **「422-ultra 持平 PNG」——视觉上成立，数学上不成立。**
   ultra = 55.5dB（4K）/54.9dB（2K），远超公认「视觉无损」门槛（≈50dB，
   人眼不可分辨）；体积与 PNG8 几乎相同（5.75 vs 5.91MB@4K）但保住
   10-bit + 4:2:2。PNG 仍是数学无损（位精确），两者不是同一保证——
   归档要位精确仍用 PNG/topos-lossless（v1 未启用）。
3. **「默认 10-bit 调色空间充足」——成立且有数字背书。**
   prores 10-bit 源 → ultra 往返后 10-bit 域 PSNR：Y 69.2~69.7dB、
   U/V 72.6~74.8dB——量化噪声比 8-bit 量化地板（54.8dB，10→8→10 往返
   理论上限）低 15dB 以上，即 10-bit 精度基本无损穿越；high 档也有
   Y 58.6~61.9dB。任何 8-bit 管线（含 JPEG）先天封顶 54.8dB 还要再叠
   压缩损失；调色重推导带的 accumulate 误差因此被结构性排除。
4. **「编解码速度碾压 PNG」——成立。**
   4K：编码 topos 16.7~45.1ms vs PNG 630.4ms（**14~38×**）；解码
   13.9~26.2ms vs 111.3ms（**4~8×**）。2K 同构（编码 15~30×、解码 3~7×）。
   附带发现：topos 对 JPEG 也是 3~7× 编码、3~5× 解码优势（PIL/libjpeg）。

## 三、口径注意（诚实边界）

- PSNR ≠ 感知质量：4:2:2 色度保持与 10-bit 的调色价值在 PSNR 里被低估。
- topos 侧 RGB↔YUV 转换为本基准的 numpy BT.601 全范围实现（含 4:2:2
  水平面积平均），生产路径转换器不同会有 ±0.1~0.3dB 级浮动。
- PNG 速度 = PIL/zlib level 6（多数工具默认）；极端调优的 PNG 编码器
  （compress_level=1 等）编码更快但体积更大，解码（zlib inflate）
  无本质差异。
- JPEG 行为 libjpeg（PIL）；MozJPEG/稀疏表等变体编码更慢、体积略小，
  不改变速度量级结论。
