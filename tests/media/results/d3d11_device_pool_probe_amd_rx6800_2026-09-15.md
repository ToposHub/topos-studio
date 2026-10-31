# D3D11 device + 纹理池真机门禁（amd_rx6800）

日期 2026-09-15 ｜ 原始数据：`d3d11_device_pool_probe_amd_rx6800_2026-09-15.json`

| 门禁 | 结果 |
|---|---|
| device_created | ✅ |
| adapter_luid_present | ✅ |
| nv12_format_support | ✅ |
| pool_1000_frame_no_growth | ✅ |
| pool_concurrent_ring_bounded | ✅ |

## 环境

- feature_level: 0xc100
- adapter: AMD Radeon RX 6800 (LUID 000111A0-00000000)
- format_support nv12: ok=True bits=0x2e6f3f0
- format_support p010: ok=False bits=0x0 reason=gpu_probe_failed:check_format_support fmt=100 hr=0x80004005
- format_support bgra8: ok=True bits=0x33fef3f3

## 结论

device 创建、LUID 走查、NV12 格式支持、定长池 1000 帧无增长、并发环有界——计划步骤 1 门禁在本机全部通过。
