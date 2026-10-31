# W3-01/02 interop 原型验证报告：D3D11 共享纹理 → GL roundtrip（AMD RX 6800）

日期：2026-09-12 ｜ 真机：AMD Radeon RX 6800（驱动 26.8.1.260806）+ Win11 26200
脚本：`scripts/probe_wgl_shared_texture_roundtrip.py`
原始数据：`wgl_shared_texture_roundtrip_amd_rx6800.json`
GL 上下文：ModernGL standalone（WGL 后端，3.3 Core——见 W1-02 报告的版本注记）

## 结论：全链路真实可用（verdict: shared_texture_roundtrip_ok）

| 步骤 | 结果 |
|---|---|
| D3D11CreateDevice（BGRA_SUPPORT，FL 11_0） | ✅ |
| ID3D11Device::CreateTexture2D（vtable[5]，BGRA8 64×64 + CPU 图案） | ✅ |
| wglDXOpenDeviceNV | ✅ |
| wglDXRegisterObjectNV（GL texture ← D3D texture，READ_ONLY） | ✅ |
| wglDXLockObjectsNV | ✅ |
| FBO attach（GL_FRAMEBUFFER_COMPLETE） | ✅ |
| **glReadPixels 像素 roundtrip** | ✅ **逐像素一致**（64×64 全阵列比对；GL 侧按 BGR 字节序解释，`gl[0,0]=[0,0,128,255]` ↔ `d3d[0,0]=[128,0,0,255]`） |
| wglDXUnlockObjectsNV / Unregister / CloseDevice | ✅（干净解绑，无泄漏路径） |

## 对 W3 的含义（计划§3 Phase W3 立项依据更新）

1. **可行性风险解除**：注册/锁定/采样三步在 AMD 驱动上全部成立；
   K-W2 的「OpenDevice ≠ RegisterObject」存疑点已被真机证据覆盖。
2. **实现形态重估**：整条链路用纯 ctypes（无 C++/Rust 扩展）跑通——
   `d3d11_gl_bridge.py` 的 W3-01/02 落地可以沿用
   `gpu_capability.py` 的 ctypes 走查层模式，不强制引入 native 扩展层。
   （Python 热路径开销仍需按计划 §6.1 用 W3-06 A/B 验证。）
3. **已在探测中钉死的 vtable/参数契约**（真机实测）：
   - `ID3D11Device` 实序第 3 个方法 = CreateTexture2D → vtable[5]；
     GetFeatureLevel 第 35 个 → vtable[37]（健全性检查先行，防错位崩溃）。
   - interop 纹理创建参数：BGRA8 + `D3D11_BIND_RENDER_TARGET|SHADER_RESOURCE`、
     `D3D11_USAGE_DEFAULT`、MiscFlags=0 → 注册成功。
   - 读回通道序：GL 读 BGRA 纹理为字节序原样（swapped 解释成立）。
4. **未覆盖**（后续 W3 真正实现时补齐）：keyed mutex 同步、解码 surface
   （NV12/P010 multi-plane）而非测试纹理、ring 轮转、`TextureLease`
   状态机对接、decode GPU 与 render GPU 同适配器断言（本机二者同为
   RX 6800，双适配器笔记本场景仍未验证——W1-03 保持开放）。

## K-W1/K-W2 判定输入（汇总）

- K-W2（可行门）：✅ 通过（W1-02 OpenDevice + 本报告 Register/Map/Sample）。
- K-W1（立项门）：AMD 上「同步下载是瓶颈」+「interop 全链路可行」两条
  证据齐备；zero-copy 收益假设成立与否由 W3-06 A/B 裁决。立项决策留待维护者。
