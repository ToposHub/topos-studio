# d3d12va 解码实测（开放问题 5 跟进，本机 AMD RX 6800）

日期：2026-09-12T07:45:28 ｜ PyAV 18.1.0 ｜ RDNA2 有 AV1 硬解硬件

| 路线 | 后端 | 素材 | codec | 位深 | 分辨率 | 裁决 | 输出格式 | fps |
|---|---|---|---|---|---|---|---|---|
| hwaccel_api | d3d12va | 2K_h264.mp4 | h264 | 8 | 1920×1080 | error | — | — |
| hwaccel_api | d3d12va | 4K_H264.mov | h264 | 8 | 3840×2160 | error | — | — |
| hwaccel_api | d3d12va | 2K_H265.mov | hevc | 10 | 1920×1080 | supported | p010le | 141.2 |
| hwaccel_api | d3d12va | 4K_H265.mov | hevc | 10 | 3840×2160 | supported | p010le | 45.0 |
| hwaccel_api | d3d12va | diag_av1_8bit_720p_d3d12.mp4 | av1 | 8 | 1280×720 | silent_sw_fallback | yuv420p | 700.9 |
| hwaccel_api | d3d12va | diag_av1_10bit_720p_d3d12.mp4 | av1 | 10 | 1280×720 | silent_sw_fallback | yuv420p10le | 1694.9 |
| hwaccel_api | d3d11va | 2K_h264.mp4 | h264 | 8 | 1920×1080 | supported | nv12 | 150.1 |
| hwaccel_api | d3d11va | 4K_H264.mov | h264 | 8 | 3840×2160 | supported | nv12 | 62.8 |
| hwaccel_api | d3d11va | 2K_H265.mov | hevc | 10 | 1920×1080 | supported | p010le | 149.6 |
| hwaccel_api | d3d11va | 4K_H265.mov | hevc | 10 | 3840×2160 | supported | p010le | 47.9 |
| hwaccel_api | d3d11va | diag_av1_8bit_720p_d3d12.mp4 | av1 | 8 | 1280×720 | silent_sw_fallback | yuv420p | 677.4 |
| hwaccel_api | d3d11va | diag_av1_10bit_720p_d3d12.mp4 | av1 | 10 | 1280×720 | silent_sw_fallback | yuv420p10le | 1724.6 |
| hwaccel_strict | d3d12va | diag_av1_8bit_720p_d3d12.mp4 | av1 | 8 | 1280×720 | error | — | — |
| hwaccel_strict | d3d12va | diag_av1_10bit_720p_d3d12.mp4 | av1 | 10 | 1280×720 | error | — | — |
| wrapper_decoder | h264_d3d12va | 2K_h264.mp4 | h264 | 8 | 1920×1080 | error | — | — |
| wrapper_decoder | hevc_d3d12va | 2K_H265.mov | hevc | 10 | 1920×1080 | error | — | — |
| wrapper_decoder | av1_d3d12va | diag_av1_8bit_720p_d3d12.mp4 | av1 | 8 | 1280×720 | error | — | — |

## detail

- hwaccel_api/d3d12va/2K_h264.mp4: ArgumentError: Invalid argument: 'avcodec_send_packet()' returned 22
- hwaccel_api/d3d12va/4K_H264.mov: ArgumentError: Invalid argument: 'avcodec_send_packet()' returned 22
- hwaccel_api/d3d12va/diag_av1_8bit_720p_d3d12.mp4: session ok but output yuv420p (software)
- hwaccel_api/d3d12va/diag_av1_10bit_720p_d3d12.mp4: session ok but output yuv420p10le (software)
- hwaccel_api/d3d11va/diag_av1_8bit_720p_d3d12.mp4: session ok but output yuv420p (software)
- hwaccel_api/d3d11va/diag_av1_10bit_720p_d3d12.mp4: session ok but output yuv420p10le (software)
- hwaccel_strict/d3d12va/diag_av1_8bit_720p_d3d12.mp4: RuntimeError: Hardware accelerated decode requested but no stream is compatible
- hwaccel_strict/d3d12va/diag_av1_10bit_720p_d3d12.mp4: RuntimeError: Hardware accelerated decode requested but no stream is compatible
- wrapper_decoder/h264_d3d12va/2K_h264.mp4: UnknownCodecError: h264_d3d12va
- wrapper_decoder/hevc_d3d12va/2K_H265.mov: UnknownCodecError: hevc_d3d12va
- wrapper_decoder/av1_d3d12va/diag_av1_8bit_720p_d3d12.mp4: UnknownCodecError: av1_d3d12va
## 结论（开放问题 5 推进）

1. **AV1 硬解在本 PyAV 构建（18.1.0 / FFmpeg 8.x）+ 本驱动上不可用，根因
   收窄到构建层**：d3d12va 严格模式报 "no stream is compatible"——该 FFmpeg
   构建未注册 `av1_d3d12va` hwaccel 配置（d3d11va 同样无）。`av.codecs_available`
   里的 `av1_d3d12va` 名字是描述符残影，`CodecContext.create` 实际抛
   UnknownCodecError——**名字存在 ≠ 解码器可用**（P0-06 探测为准的新例证）。
2. **意外收获：d3d12va 后端真实可用**——HEVC 10-bit 2K/4K 硬解生效
   （p010le 输出）。d3d12va 从「理论存在」升级为「本机实测可用」，可作
   hw_planar_source 的备选后端评估输入；H264 在 d3d12va 报
   avcodec_send_packet EINVAL（d3d11va 正常覆盖，不阻塞）。
3. d3d11va 对照行与 W1-04 归档完全一致（h264/hevc supported、AV1 双位深
   静默软解）——跨脚本一致性确认，旧结论不受环境漂移影响。
4. **开放问题 5 状态更新**：从「AV1 静默软解原因未知」推进为「PyAV wheel
   构建不含 AV1 d3d11va/d3d12va hwaccel 配置，属构建层缺失」。后续若要
   AV1 硬解，路径是自编 FFmpeg（带 av1 d3d11va/d3d12va hwaccel）或等待
   PyAV 官方 wheel 变更——两者都超出当前 PyAV 依赖的现实边界，记为
   **关闭（本构建不可用，根因明确）+ 升级路径已登记**。
