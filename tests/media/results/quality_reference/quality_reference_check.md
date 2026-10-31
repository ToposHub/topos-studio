# 画质参考帧检查（P0-05）

> 2026-09-29T21:05:21.374411  git `bc120ecdb847`  GL `AMD Radeon RX 6900 XT OpenGL Engine`

| 用例 | 上传模式 | max/255 | mean/255 | rmse/255 | 结果 |
|---|---|---:|---:|---:|---|
| sdr_8bit_709_limited_420 | r8 | 4.87 | 0.057 | 0.144 | PASS |
| sdr_8bit_601_full_444 | r8 | 0.25 | 0.050 | 0.072 | PASS |
| sdr_10bit_709_limited_422 | r16ui | 0.25 | 0.054 | 0.076 | PASS |
| sdr_12bit_709_limited_444 | r16ui | 0.25 | 0.053 | 0.076 | PASS |
| wide_10bit_2020_limited_420 | r16ui | 0.25 | 0.054 | 0.076 | PASS |
| alpha_10bit_709_limited_444 | r16ui | 0.25 | 0.053 | 0.076 | PASS |
