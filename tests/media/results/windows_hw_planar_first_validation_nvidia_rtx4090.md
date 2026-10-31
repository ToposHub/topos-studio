# W1-05 WindowsHwPlanarSource 真机首验（nvidia_rtx4090 分册）

日期：2026-09-12T12:04:08 ｜ 原始数据：`windows_hw_planar_first_validation_nvidia_rtx4090.json`

| 检查 | 结果 | 备注 |
|---|---|---|
| platform_routing | ✅ | {'has_nvidia_gpu': True, 'available_decoders': ['d3d11va', 'nvdec', 'qsv'], 'platform_default': 'nvdec', 'expected_default': 'nvdec'} |
| contract:h264:default | ✅ | nv12 |
| contract:h264:d3d11va | ✅ | nv12 |
| contract:h264:nvdec | ✅ | nv12 |
| contract:hevc:default | ✅ | p010le |
| contract:hevc:d3d11va | ✅ | p010le |
| contract:hevc:nvdec | ✅ | p010le |
| diag_422_10bit_explicit_reject | ✅ | codec_unsupported_by_hw:h264/yuv422p10le: hardware decoder produced yuv422p10le  |

## 结论

- 平台路由：`has_nvidia_gpu=True`，平台默认 nvdec（NVDEC 优先，与 get_platform_default 顺序一致；D3D11VA 显式指定同样全程可用作 vendor 无关回退）。
- H.264 8-bit → NV12、HEVC 10-bit → P010（p016le 载体，MSB 对齐原样上传）：DecodedFrame 契约（双紧凑平面 / decode_path / msb_aligned_p010）与 macOS VT planar 源一致——NVDEC 与 D3D11VA 两条硬解路径同契约。
- 4:2:2 10-bit 诊断流：open() 以 `codec_unsupported_by_hw` 显式拒绝，无静默软解伪装（计划 §4 硬规则）。
