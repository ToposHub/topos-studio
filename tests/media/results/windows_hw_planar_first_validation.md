# W1-05 WindowsHwPlanarSource 真机首验（AMD RX 6800 / D3D11VA）

日期：2026-09-12T06:39:29 ｜ 原始数据：`windows_hw_planar_first_validation.json`

| 检查 | 结果 | 备注 |
|---|---|---|
| platform_routing | ✅ | {'has_nvidia_gpu': False, 'available_decoders': ['d3d11va'], 'platform_default': 'd3d11va'} |
| contract:h264 | ✅ | nv12 |
| contract:hevc | ✅ | p010le |
| diag_422_10bit_explicit_reject | ✅ | codec_unsupported_by_hw:h264/yuv422p10le: hardware decoder produced yuv422p10le  |

## 结论

- 平台路由：本机无 NVIDIA（`has_nvidia_gpu=False`），平台默认 D3D11VA（W2-02 vendor 修正后 NVDEC 不再被 ffmpeg CLI 编译项误触发）。
- H.264 8-bit → NV12、HEVC 10-bit → P010（p016le 载体，MSB 对齐原样上传）：DecodedFrame 契约（双紧凑平面 / decode_path / msb_aligned_p010）与 macOS VT planar 源一致，P8-08 注明的「真实硬解验证留待 Windows 机器」就此关闭（AMD 行）。
- 4:2:2 10-bit 诊断流：open() 以 `codec_unsupported_by_hw` 显式拒绝，无静默软解伪装（计划 §4 硬规则）。
- NVDEC 行为 K-W5 保持未验证（无 N 卡）。
