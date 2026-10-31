"""
Windows D3D11/GL Zero-Copy Module
Windows D3D11VA / NVDEC / QSV -> OpenGL texture interop 零拷贝。

## 职责

- 原生 D3D11 device 生命周期与 LUID 身份（d3d11_device.py，计划步骤 1）
- D3D11 定长纹理池与 lease 所有权（d3d11_texture_pool.py，计划步骤 1）
- D3D11 GPU 驻留帧契约（d3d11_frame.py，计划 §3.3）
- GpuEncodeSurface 契约与编码 surface 提供者（gpu_encode_surface.py）
- T0 零回读直通链编排器（d3d11_passthrough_chain.py，计划步骤 2）
- D3D11 解码器封装（d3d11_decoder.py，诚实骨架待原生桥）
- D3D11 texture -> GL bridge（d3d11_gl_bridge.py，诚实骨架）
- 零拷贝分层 probe 和 benchmark（windows_zero_copy_probe.py）
- 能力缓存 gate（capability.py）
- 失败/回退显式 reason 词表（reasons.py，计划 §4 代码级展开）

## 注意

device / 纹理池 / 帧契约 / 直通链校验层已在 Windows 真机（AMD RX 6800）
验证并归档；解码 surface 与硬编 surface 提交依赖原生桥，落地前按
reasons 词表 fail-closed，绝不静默走 CPU 路径伪装。
"""

from .capability import WindowsZeroCopyCapability  # noqa: F401 — re-export（src.shared.media 聚合导出）
