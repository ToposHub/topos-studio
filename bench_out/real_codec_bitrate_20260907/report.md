# TOPOS 与 ProRes 多规格码率对比

测试日期：2026-09-07。测试分辨率为 1920×1080、25 fps，每档取 96 帧。422 档使用 `2K_prores422HQ.mov` 解码后的 `yuv422p10le` 帧；444 档使用 `4K_prores4444.mov` Lanczos 缩放到 1920×1080 后的 4:4:4 帧。每一行的 TOPOS 与 ProRes 都使用同一组帧。

TOPOS 使用当前 `encode_sized` 档位目标；ProRes 使用 FFmpeg `prores_ks` 的对应 profile。码率按最终 MOV 文件大小和 96/25 秒计算，MOV 容器开销已包含。

| 对应规格 | 像素格式 | TOPOS Mbps | ProRes Mbps | TOPOS / ProRes | TOPOS bpp | ProRes bpp | TOPOS QP 范围 |
|---|---|---:|---:|---:|---:|---:|---:|
| Proxy | 422 10-bit | 48.140 | 12.693 | 3.79× | 0.9286 | 0.2448 | 63–63 |
| LT | 422 10-bit | 59.550 | 40.922 | 1.46× | 1.1487 | 0.7894 | 60–63 |
| Standard / 422 | 422 10-bit | 92.166 | 60.618 | 1.52× | 1.7779 | 1.1693 | 57–59 |
| HQ / 422 HQ | 422 10-bit | 136.844 | 106.465 | 1.29× | 2.6397 | 2.0537 | 54–56 |
| Pro444 / 4444 | 444 10-bit TOPOS / 12-bit ProRes 输出 | 298.761 | 290.435 | 1.03× | 5.7631 | 5.6025 | 63–63 |
| Extreme / 4444 XQ | 444 12-bit | 584.608 | 428.804 | 1.36× | 11.2772 | 8.2717 | 63–63 |

结论：当前 TOPOS 在所有规格上都比对应 ProRes 大；422 档大约 1.29–3.79 倍，444 档大约 1.03–1.36 倍。Pro444 这一行的 ProRes 编码输出被 FFmpeg 标记为 `yuv444p12le`，因此它与 TOPOS 10-bit 输出的位深并不完全相同，不能把这一行当作严格同质量比较。

Proxy、Pro444 和 Extreme 都出现 QP=63 的地板，且实际码率仍高于档位目标：Proxy 实际 0.9286 bpp 对目标 0.6051，Pro444 实际 5.7631 bpp 对目标 4.1106，Extreme 实际 11.2772 bpp 对目标 6.7630。这与此前计划中记录的“QP63 地板超支”一致；它不是码率配置小误差，而是当前帧内 VLC 内核在这些规格上的压缩能力上限。

机器可读结果见 [result.json](/Users/testuser/Documents/vscode/topos-color-multitask%20/topos-color02/bench_out/real_codec_bitrate_20260907/result.json)。生成的 MOV 和 raw 中间文件保留在同一目录，MOV 已由仓库的 `bench_out/**/*.mov` 规则忽略，不进入 Git。

复现命令：

```bash
LIB="$PWD/native/topos_codec/build/real-bench-release/topos_codec.0.1.0.dylib"
TOPOS_CODEC_LIB="$LIB" .venv/bin/python tools/bench_real_bitrates.py \
  --frames 96 \
  --out-dir bench_out/real_codec_bitrate_20260907 \
  --lib "$LIB"
```
