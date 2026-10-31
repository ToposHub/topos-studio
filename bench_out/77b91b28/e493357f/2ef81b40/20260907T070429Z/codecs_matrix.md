# Topos × ProRes × DNxHR 对照组（自动生成）

## 2K / 44410（96 帧 @25fps，画质测前 96 帧）

| codec | variant | bd | Mbps | Y-PSNR | SSIM-Y | enc ms/f | dec ms/f |
|---|---|---|---|---|---|---|---|
| topos | pro444 | 10 | 214.1 | 50.97 | 0.99482 | 12.8 | 5.8 |
| prores | 4444 | 10 | 290.4 | 56.77 | 0.99880 | 86.8 | 3.8 |
| dnxhr | 444 | 10 | 367.0 | 60.20 | 0.99941 | 14.0 | 2.7 |

同码率插值 Δ（topos − 参照，dB；仅同位深主行）：
- pro444: vs ProRes n/a dB，vs DNxHR n/a dB（214 Mbps）

配对速度比（耗时比，>1 = topos 更慢）：
- pro444: enc 0.147×ProRes / 0.911×DNxHR，dec 1.548×ProRes / 2.132×DNxHR

## 2K / 44412（96 帧 @25fps，画质测前 96 帧）

| codec | variant | bd | Mbps | Y-PSNR | SSIM-Y | enc ms/f | dec ms/f |
|---|---|---|---|---|---|---|---|
| topos | extreme | 12 | 427.4 | 58.10 | 0.99902 | 15.9 | 8.3 |

附加参照（DNxHR 8-bit，位深不同，不进 Δ 比较）：

| codec | variant | bd | Mbps | Y-PSNR | SSIM-Y | enc ms/f | dec ms/f |
|---|---|---|---|---|---|---|---|
| prores | 4444xq | 10 | 428.2 | 61.23 | 0.99958 | 98.6 | 5.0 |

同码率插值 Δ（topos − 参照，dB；仅同位深主行）：
- extreme: vs ProRes n/a dB，vs DNxHR n/a dB（427 Mbps）

配对速度比（耗时比，>1 = topos 更慢）：
- extreme: enc 0.161×ProRes / n/a×DNxHR，dec 1.655×ProRes / n/a×DNxHR
