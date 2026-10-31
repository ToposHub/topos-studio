# 真实素材 TOPOS 编解码基准

测试日期：2026-09-07。素材：[2K_h264.mp4](/Users/testuser/Documents/vscode/topos-color-multitask%20/topos-color02/tests/test_videos/2K_h264.mp4)，1920×1080、25 fps、1572 帧、62.88 秒，H.264 High、4:2:0 8-bit，原文件 52,513,775 bytes。

本次使用当前工作区 Release 动态库 `native/topos_codec/build/real-bench-release/topos_codec.0.1.0.dylib`，以 `proxy` 档将素材完整转换为 [2K_h264_topos_proxy.mov](/Users/testuser/Documents/vscode/topos-color-multitask%20/topos-color02/bench_out/real_2k_h264_20260907/2K_h264_topos_proxy.mov)。输入先由 FFmpeg 解码并转换为 TOPOS 当前的 `yuv422p10le` 平面格式，再进入产品 `ToposVideoEncoder`。

| 项目 | 结果 |
|---|---:|
| H.264 → TOPOS 端到端编码 | 16.362 s / 96.08 fps |
| TOPOS 编码调用累计（剥离 FFmpeg 读帧） | 9.823 s / 160.04 fps |
| TOPOS 完整解码 | 4.129 s / 380.69 fps |
| TOPOS 解码平均单帧 | 2.627 ms |
| TOPOS 输出 | 781,505,543 bytes / 99.43 Mbps |
| 相对 H.264 体积 | 14.88× |

与项目已有的 2026-09-03 基准作趋势参考：旧报告中的 2K `proxy` sized 编码约 115 fps，本次真实长片段的 TOPOS 编码调用约 160 fps；旧报告中的 2K `proxy` native 解码约 429 fps，本次真实文件完整解码约 381 fps。两次测试的素材、帧数、构建提交和计时窗口不同，不能把差值当成严格回归或单项优化收益，但当前结果至少没有显示真实素材上的编码/解码内核退化。

产品 wrapper 的 120 帧口径为 135.6 fps（p50 6.12 ms）；预取 packet、复用输出平面的 native 口径为 335.6 fps（p50 2.68 ms）。完整文件的流式 native 解码为 380.69 fps。

同一台机器上，原始 H.264 的对照结果是 FFmpeg 1079.0 fps，VideoToolbox 路径 304.0 fps。由此可见，TOPOS 当前 native 解码比这次 VideoToolbox 测量高约 25%，但比 FFmpeg 软件解码低约 65%；产品 wrapper 因 Python/ctypes/封装开销明显低于 native 内核。三者的像素格式和硬件路径不同，这个比较用于定位瓶颈，不作为编码器之间的严格质量排名。

质量检查将 TOPOS 解码帧与同一 H.264 解码后、转换成 `yuv422p10le` 的输入逐平面对比：Y 平面 PSNR 54.201 dB，U/V 分别为 61.972/62.325 dB，1572 帧均成功解码。这个结果说明当前 `proxy` 档对这段素材的往返误差较小；比较基准是 H.264 解码结果，因此不能证明相对原始拍摄源无损。

结论是：当前计划中已有的解码内核优化在真实素材上能够稳定超过实时播放线，但体积目标没有达成。TOPOS 是纯帧内编码，面对已经使用 inter/motion 的 H.264，输出比源文件大 14.88 倍；这正是下一阶段必须落地帧间预测/参考帧和更强熵编码的实证依据。解码速率下一步应优先收窄 wrapper 与 native 的差距（packet 批量读取、零拷贝 frame view、C 侧批量解码），再评估 slice 线程和 SIMD 的收益。

原始机器可读数据见 [result.json](/Users/testuser/Documents/vscode/topos-color-multitask%20/topos-color02/bench_out/real_2k_h264_20260907/result.json) 与 [quality.json](/Users/testuser/Documents/vscode/topos-color-multitask%20/topos-color02/bench_out/real_2k_h264_20260907/quality.json)。
