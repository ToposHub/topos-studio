# W1-02 WGL_NV_DX_interop 真机探测报告（AMD RX 6800）

日期：2026-09-12 ｜ 真机：AMD Radeon RX 6800（0x1002/0x73BF，驱动 26.8.1.260806）+ Win11 26200
脚本：`scripts/probe_wgl_interop_windows.py` ｜ 原始数据：`wgl_interop_probe_amd_rx6800.json`
对应计划：`docs/windows_optimization_plan_2026-09-11.md` W1-02、K-W2、开放问题 2/3

## 结论（只信探测，不按 vendor 猜）

| 探测层 | 结果 |
|---|---|
| DXGI 适配器 | AMD Radeon RX 6800（主）+ Microsoft Basic Render Driver |
| ModernGL standalone 上下文 | 创建成功，渲染器 = RX 6800（WGL 后端）|
| WGL 扩展字符串 | **`WGL_NV_DX_interop` 存在**（共 23 个 WGL 扩展）|
| `wglGetProcAddress("wglDXOpenDeviceNV")` | 函数指针存在 |
| `wglDXOpenDeviceNV(D3D11 device)` | **返回非 NULL → interop 打开成功** |

**verdict: `interop_open_ok`** —— 扩展名虽带 NV，AMD Windows 驱动真实实现了它；
K-W2 的「interop 可行门」在 AMD 侧通过，W3（zero-copy）不被本条否决。

## 附带发现（两处实现级 bug / 风险）

1. **`capability.py::_detect_wgl_nv_dx_interop` 修复**：`wglGetExtensionsStringEXT`
   不是 opengl32.dll 的直接导出，必须经 `wglGetProcAddress` 向当前 ICD 获取。
   旧实现 `CDLL("opengl32.dll").wglGetExtensionsStringEXT` 直接抛
   AttributeError → 一律返回 False → **所有机器都会误报
   `wgl_nv_dx_interop_unavailable`**。已改为经 `wglGetProcAddress` 获取
   （本次探测即用它确认扩展存在）。
2. **开放问题 3（GL 上下文版本）仍未完全关闭**：ModernGL standalone
   （glcontext WGL 后端）显式请求 4.6 Core 仍只得到
   `3.3.0 Core Profile Context 26.8.1.260806`。这不是 interop 的拦路虎
   （扩展在 3.3 core 上下文可见），但直接关系 W4-01（GL compute 需要
   4.3+）：**应用内 Qt QSurfaceFormat 4.5 Core 实际拿到什么版本，必须在
   真机 app 内验证**（standalone 结论不可外推到 Qt 上下文）。

## 对后续阶段的含义

- W3-02（WGL_NV_DX_interop GL 侧桥）：可行性证据 +1；但 `OpenDevice`
  成功 ≠ `RegisterObjectNV/MapTextures` 成功，W3-01/02 的契约实现仍需
  逐级验证（ring、同步、同 adapter 约束——W1-03）。
- 本次探测的 GL 与 D3D11 设备都落在 RX 6800 上（默认 adapter 一致），
  未覆盖双适配器笔记本场景（W1-03 保持未验证）。
- 本报告只覆盖 AMD 行；NVIDIA/Intel 行按 K-W5 保持「无真机不写已验证」。
