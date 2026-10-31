# Topos × ProRes × DNxHR 对照组（自动生成）

## 2K / 422（96 帧 @25fps，画质测前 96 帧）

| codec | variant | bd | Mbps | Y-PSNR | SSIM-Y | enc ms/f | dec ms/f |
|---|---|---|---|---|---|---|---|
| prores | proxy | 10 | 12.7 | 54.06 | 0.99720 | 35.0 | 0.8 |
| prores | lt | 10 | 40.9 | 58.75 | 0.99879 | 53.5 | 1.3 |
| topos | proxy | 10 | 48.1 | 57.86 | 0.99853 | 7.8 | 2.0 |
| prores | standard | 10 | 60.6 | 61.26 | 0.99930 | 49.8 | 1.5 |
| topos | lt | 10 | 85.4 | 60.72 | 0.99920 | 10.9 | 2.6 |
| prores | hq | 10 | 106.5 | 65.89 | 0.99976 | 62.7 | 2.0 |
| topos | standard | 10 | 129.2 | 62.87 | 0.99951 | 11.9 | 3.3 |
| dnxhr | hqx | 10 | 183.5 | 65.03 | 0.99968 | 6.0 | 2.0 |
| topos | hq | 10 | 191.2 | 65.46 | 0.99972 | 13.3 | 4.0 |

附加参照（DNxHR 8-bit，位深不同，不进 Δ 比较）：

| codec | variant | bd | Mbps | Y-PSNR | SSIM-Y | enc ms/f | dec ms/f |
|---|---|---|---|---|---|---|---|
| dnxhr | lb | 8 | 37.7 | 53.72 | 0.99783 | 1.8 | 1.1 |
| dnxhr | sq | 8 | 121.2 | 53.72 | 0.99783 | 1.9 | 1.3 |
| dnxhr | hq | 8 | 183.5 | 54.15 | 0.99808 | 2.1 | 1.4 |

同码率插值 Δ（topos − 参照，dB；仅同位深主行）：
- proxy: vs ProRes -1.81 dB，vs DNxHR n/a dB（48 Mbps）
- lt: vs ProRes -3.04 dB，vs DNxHR n/a dB（85 Mbps）
- standard: vs ProRes n/a dB，vs DNxHR n/a dB（129 Mbps）
- hq: vs ProRes n/a dB，vs DNxHR n/a dB（191 Mbps）

配对速度比（耗时比，>1 = topos 更慢）：
- proxy: enc 0.224×ProRes / n/a×DNxHR，dec 2.592×ProRes / n/a×DNxHR
- lt: enc 0.203×ProRes / n/a×DNxHR，dec 2.062×ProRes / n/a×DNxHR
- standard: enc 0.238×ProRes / n/a×DNxHR，dec 2.123×ProRes / n/a×DNxHR
- hq: enc 0.212×ProRes / 2.227×DNxHR，dec 2.025×ProRes / 1.966×DNxHR

## 2K / 44410（96 帧 @25fps，画质测前 96 帧）

| codec | variant | bd | Mbps | Y-PSNR | SSIM-Y | enc ms/f | dec ms/f |
|---|---|---|---|---|---|---|---|
| prores | 4444 | 10 | 290.4 | 56.77 | 0.99880 | 131.0 | 4.5 |
| topos | pro444 | 10 | 307.7 | 50.97 | 0.99485 | 23.6 | 5.4 |
| dnxhr | 444 | 10 | 367.0 | 60.20 | 0.99941 | 19.2 | 3.7 |

同码率插值 Δ（topos − 参照，dB；仅同位深主行）：
- pro444: vs ProRes n/a dB，vs DNxHR n/a dB（308 Mbps）

配对速度比（耗时比，>1 = topos 更慢）：
- pro444: enc 0.181×ProRes / 1.231×DNxHR，dec 1.196×ProRes / 1.437×DNxHR

## 2K / 44412（96 帧 @25fps，画质测前 96 帧）

| codec | variant | bd | Mbps | Y-PSNR | SSIM-Y | enc ms/f | dec ms/f |
|---|---|---|---|---|---|---|---|
| topos | extreme | 12 | 584.6 | 58.10 | 0.99902 | 26.6 | 6.9 |

附加参照（DNxHR 8-bit，位深不同，不进 Δ 比较）：

| codec | variant | bd | Mbps | Y-PSNR | SSIM-Y | enc ms/f | dec ms/f |
|---|---|---|---|---|---|---|---|
| prores | 4444xq | 10 | 428.2 | 61.23 | 0.99958 | 140.9 | 5.8 |

同码率插值 Δ（topos − 参照，dB；仅同位深主行）：
- extreme: vs ProRes n/a dB，vs DNxHR n/a dB（585 Mbps）

配对速度比（耗时比，>1 = topos 更慢）：
- extreme: enc 0.189×ProRes / n/a×DNxHR，dec 1.184×ProRes / n/a×DNxHR

## 4K / 422（96 帧 @25fps，画质测前 48 帧）

| codec | variant | bd | Mbps | Y-PSNR | SSIM-Y | enc ms/f | dec ms/f |
|---|---|---|---|---|---|---|---|
| prores | proxy | 10 | 78.4 | 53.96 | 0.99527 | 121.0 | 2.9 |
| prores | lt | 10 | 286.8 | 58.73 | 0.99824 | 217.3 | 5.3 |
| topos | proxy | 10 | 311.3 | 57.09 | 0.99756 | 34.4 | 5.9 |
| topos | lt | 10 | 353.9 | 57.39 | 0.99766 | 36.5 | 6.2 |
| prores | standard | 10 | 413.6 | 60.91 | 0.99894 | 251.7 | 7.5 |
| topos | standard | 10 | 524.2 | 59.13 | 0.99838 | 37.9 | 7.9 |
| prores | hq | 10 | 648.3 | 65.33 | 0.99966 | 313.0 | 9.6 |
| dnxhr | hqx | 10 | 728.3 | 64.79 | 0.99965 | 28.6 | 8.4 |
| topos | hq | 10 | 771.0 | 61.03 | 0.99897 | 42.6 | 9.6 |

附加参照（DNxHR 8-bit，位深不同，不进 Δ 比较）：

| codec | variant | bd | Mbps | Y-PSNR | SSIM-Y | enc ms/f | dec ms/f |
|---|---|---|---|---|---|---|---|
| dnxhr | lb | 8 | 149.9 | 52.20 | 0.99574 | 7.1 | 3.0 |
| dnxhr | sq | 8 | 481.7 | 52.45 | 0.99627 | 7.0 | 3.9 |
| dnxhr | hq | 8 | 728.3 | 52.63 | 0.99660 | 7.9 | 4.6 |

同码率插值 Δ（topos − 参照，dB；仅同位深主行）：
- proxy: vs ProRes -2.07 dB，vs DNxHR n/a dB（311 Mbps）
- lt: vs ProRes -2.49 dB，vs DNxHR n/a dB（354 Mbps）
- standard: vs ProRes -3.86 dB，vs DNxHR n/a dB（524 Mbps）
- hq: vs ProRes n/a dB，vs DNxHR n/a dB（771 Mbps）

配对速度比（耗时比，>1 = topos 更慢）：
- proxy: enc 0.284×ProRes / n/a×DNxHR，dec 2.073×ProRes / n/a×DNxHR
- lt: enc 0.168×ProRes / n/a×DNxHR，dec 1.166×ProRes / n/a×DNxHR
- standard: enc 0.151×ProRes / n/a×DNxHR，dec 1.058×ProRes / n/a×DNxHR
- hq: enc 0.136×ProRes / 1.486×DNxHR，dec 0.998×ProRes / 1.141×DNxHR

## 4K / 44410（96 帧 @25fps，画质测前 48 帧）

| codec | variant | bd | Mbps | Y-PSNR | SSIM-Y | enc ms/f | dec ms/f |
|---|---|---|---|---|---|---|---|
| prores | 4444 | 10 | 1149.9 | 61.59 | 0.99953 | 442.6 | 19.1 |
| topos | pro444 | 10 | 1168.2 | 56.83 | 0.99831 | 59.8 | 13.4 |
| dnxhr | 444 | 10 | 1457.4 | 65.46 | 0.99977 | 60.8 | 14.8 |

同码率插值 Δ（topos − 参照，dB；仅同位深主行）：
- pro444: vs ProRes n/a dB，vs DNxHR n/a dB（1168 Mbps）

配对速度比（耗时比，>1 = topos 更慢）：
- pro444: enc 0.135×ProRes / 0.983×DNxHR，dec 0.701×ProRes / 0.903×DNxHR

## 4K / 44412（96 帧 @25fps，画质测前 48 帧）

| codec | variant | bd | Mbps | Y-PSNR | SSIM-Y | enc ms/f | dec ms/f |
|---|---|---|---|---|---|---|---|
| topos | extreme | 12 | 1866.1 | 61.52 | 0.99937 | 72.7 | 17.8 |

附加参照（DNxHR 8-bit，位深不同，不进 Δ 比较）：

| codec | variant | bd | Mbps | Y-PSNR | SSIM-Y | enc ms/f | dec ms/f |
|---|---|---|---|---|---|---|---|
| prores | 4444xq | 10 | 1378.2 | 66.39 | 0.99983 | 433.0 | 20.8 |

同码率插值 Δ（topos − 参照，dB；仅同位深主行）：
- extreme: vs ProRes n/a dB，vs DNxHR n/a dB（1866 Mbps）

配对速度比（耗时比，>1 = topos 更慢）：
- extreme: enc 0.168×ProRes / n/a×DNxHR，dec 0.854×ProRes / n/a×DNxHR
