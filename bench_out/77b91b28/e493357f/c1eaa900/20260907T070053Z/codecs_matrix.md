# Topos × ProRes × DNxHR 对照组（自动生成）

## 2K / 422（96 帧 @25fps，画质测前 96 帧）

| codec | variant | bd | Mbps | Y-PSNR | SSIM-Y | enc ms/f | dec ms/f |
|---|---|---|---|---|---|---|---|
| prores | proxy | 10 | 12.7 | 54.06 | 0.99720 | 26.4 | 0.8 |
| topos | proxy | 10 | 32.6 | 57.86 | 0.99853 | 5.3 | 1.6 |
| prores | lt | 10 | 40.9 | 58.75 | 0.99879 | 38.4 | 1.0 |
| topos | lt | 10 | 59.5 | 60.72 | 0.99920 | 6.7 | 2.4 |
| prores | standard | 10 | 60.6 | 61.26 | 0.99930 | 36.4 | 1.3 |
| topos | standard | 10 | 91.8 | 62.97 | 0.99950 | 8.1 | 3.1 |
| prores | hq | 10 | 106.5 | 65.89 | 0.99976 | 44.1 | 1.7 |
| topos | hq | 10 | 136.8 | 65.47 | 0.99972 | 8.6 | 4.0 |
| dnxhr | hqx | 10 | 183.5 | 65.03 | 0.99968 | 4.4 | 1.2 |

附加参照（DNxHR 8-bit，位深不同，不进 Δ 比较）：

| codec | variant | bd | Mbps | Y-PSNR | SSIM-Y | enc ms/f | dec ms/f |
|---|---|---|---|---|---|---|---|
| dnxhr | lb | 8 | 37.7 | 53.72 | 0.99783 | 1.3 | 0.7 |
| dnxhr | sq | 8 | 121.2 | 53.72 | 0.99783 | 1.3 | 0.8 |
| dnxhr | hq | 8 | 183.5 | 54.15 | 0.99808 | 1.5 | 0.9 |

同码率插值 Δ（topos − 参照，dB；仅同位深主行）：
- proxy: vs ProRes +0.50 dB，vs DNxHR n/a dB（33 Mbps）
- lt: vs ProRes -0.39 dB，vs DNxHR n/a dB（59 Mbps）
- standard: vs ProRes -1.44 dB，vs DNxHR n/a dB（92 Mbps）
- hq: vs ProRes n/a dB，vs DNxHR n/a dB（137 Mbps）

配对速度比（耗时比，>1 = topos 更慢）：
- proxy: enc 0.203×ProRes / n/a×DNxHR，dec 2.0×ProRes / n/a×DNxHR
- lt: enc 0.173×ProRes / n/a×DNxHR，dec 2.49×ProRes / n/a×DNxHR
- standard: enc 0.224×ProRes / n/a×DNxHR，dec 2.326×ProRes / n/a×DNxHR
- hq: enc 0.195×ProRes / 1.975×DNxHR，dec 2.383×ProRes / 3.289×DNxHR
