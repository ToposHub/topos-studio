# W4-01 GL compute 现状确认（AMD RX 6800 真机）——预期被证伪 + 门控修复

日期：2026-09-12 ｜ 真机：AMD Radeon RX 6800（Adrenalin 26.8.1.260806）+ Win11 26200
脚本：`scripts/probe_gl_compute_windows.py`（Qt offscreen 4.5 Core 应用同形态上下文）
原始数据：`gl_compute_probe_amd_rx6800.json`

## 核心发现（只信探测）

| 项 | 结果 |
|---|---|
| Qt QSurfaceFormat(4.5 Core) 实际拿到 | **4.5 Core**（AMD ICD；请求 4.6 亦得 4.6）|
| GLSHADING_LANGUAGE_VERSION 字符串 | 4.60 |
| VERTEX / FRAGMENT / GEOMETRY / TESS_CTRL / TESS_EVAL 着色器创建 | ✅ 全部正常 |
| **glCreateShader(GL_COMPUTE_SHADER)** | ❌ **返回 0 + GL_INVALID_ENUM** |
| 跨形态验证（4.6/4.3 Core、4.5/2.1 Compat） | ❌ 全部拒收 COMPUTE_SHADER |
| glDispatchCompute 等函数指针 | 存在（但 shader 对象无法创建，无实际意义）|

**结论：本机 AMD 驱动（Adrenalin 26.8.1）的桌面 GL 上下文虽通告 4.5/4.6
+ GLSL 4.60，但不暴露 compute shader 阶段。** W4-01 验收预期
（"blur/erode_dilate/histogram 不触发 fragment 回退"）在本机**被证伪**。

## 附带发现 1：开放问题 3 关闭（应用侧）

Qt 请求即所得：4.5 Core 请求 → 4.5 Core 实际上下文（4.6 同理）。
W1-02 standalone 上下文拿到的 "3.3 Core" 是 glcontext 后端的请求方式问题，
**不可外推到 Qt 应用**。应用内 GL 版本以 Qt QSurfaceFormat 请求为准。

## 附带发现 2（工程修复）：版本门控不可靠 → 已改探测式

`compute_shader_manager._check_availability()` 旧逻辑：版本 ≥4.3 直接置
可用、不做测试编译（探测编译仅在版本未知时进行）。在本机这类
「通告 4.5/4.6 却无 compute」的驱动上会**误判可用**，效果运行期逐个
编译失败。已修复：版本 ≥4.3 也强制测试编译一次（版本 <4.3 仍短路拒绝）。
回归钉：`tests/composite/test_compute_shader_probe_gating.py`（15 项相关测试全过）。

## 对 K-W4 与 W4 后续的影响

- **K-W4（compute 替换门）前提变化**：本机 GL compute 不可用 ⇒
  「CUDA/DirectML 须 A/B ≥15% 跑赢 GL compute」的比较基线在本机不存在。
  W4-02（CUDA）本就 NVIDIA 专属、跳过；**W4-03（DirectML）语境增强**——
  它成为 AMD 机器上 compute 类负载（ONNX AI 模型）的现实通道，但仍受
  开放问题 6（onnxruntime-directml 依赖决策）约束，未立项。
- fragment 回退是本机 compute 类效果的**现实路径**（且按 P4-05 经验，
  这正是性能上的正确默认）。
