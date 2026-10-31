# 四维组合 A/B 总报告——NVIDIA RTX 4090 分册（W7-01 / P8-07）

> 日期：2026-09-12 ｜ GPU：NVIDIA GeForce RTX 4090（24GB，驱动 610.88 / CUDA 13.3）+ Intel UHD 770
> 机器：Win11 26200 ｜ PyAV 18.1.0 ｜ Python 3.13
> 上游 P8-07 要求：OpenGL / D3D / CUDA 组合的收益**分段可解释**
> （decode / interop / present / compute / export 逐段归因）。
> AMD 对照分册：`windows_four_dim_ab_amd.md`。

## 五段归因（本机实测）

### 1. decode（解码）

| 路径 | 4K H.264 8bit | 4K HEVC 10bit | 来源 |
|---|---|---|---|
| CPU planar（生产） | **945 fps** | **320 fps** | bench_hw_decode_results_nvidia_rtx4090.json |
| NVDEC planar（硬解+下载） | 163 fps（NV12） | 196 fps（P010） | 同上 |
| CPU planar（AMD 6800 机） | 486 fps | 172 fps | bench_hw_decode_results.json |

- NVDEC 真实生效（输出 nv12/p010le 裁决，防静默软解协议）；
  4:2:2 10bit 双后端显式拒绝；AV1 受 PyAV wheel 构建限制静默软解
  （`windows_capability_matrix_nvidia_rtx4090.md`）。
- 归因：**decode 段已不是瓶颈**（单流 25fps 预算的 7.8~38 倍）。

### 2. interop（零拷贝关键路径）

- `WGL_NV_DX_interop`：**NVIDIA 实现且 `wglDXOpenDeviceNV` 打开成功**
  （`wgl_interop_probe_nvidia_rtx4090.json`，interop_open_ok）——与 AMD 行
  同判。双 vendor 可行 → K-W2 不否决 W3。
- 共享纹理 roundtrip 原型（注册/锁定/FBO 采样）已在 AMD 机通过；
  NVIDIA 机的逐像素 roundtrip 未单独跑（原型同构，生产实现按 W3 立项决策挂起）。
- 归因：**interop 可行性成立，但生产实现未启动**（见下方 W3 决策）。

### 3. present（呈现）

- 维持 OpenGL/WGL（W5-01 决策记录）；D3D flip-model swapchain 评估
  （W5-02）未启动——W3 生产接入前不引入（防范围漂移）。
- 本机 GL 上下文：Qt 4.5 Core（应用形态）✅；standalone 3.3 Core（glcontext
  后端行为，非驱动限制）。
- 归因：present 段无变化，无收益可归因（维持现状是有意决策）。

### 4. compute

- **GL compute 不可用（三重交叉确认）**：Qt 4.5 Core + 原生 WGL
  4.6/4.5/4.3 Core（GLSL 4.60 NVIDIA）下 `glCreateShader(GL_COMPUTE_SHADER)`
  一致返回 INVALID_ENUM（`gl_compute_probe_nvidia_rtx4090.json`，
  `compute_unavailable_native_wgl_confirmed`）——与 AMD 真机现象一致，
  指向会话环境级裁剪而非驱动能力缺失。
- 后果：blur/erode_dilate/histogram 走 fragment 回退是 Windows 现实路径。
- CUDA compute（W4-02）：**不立项**。K-W4 的「替换门」前提（GL compute
  可用时需 A/B ≥15%）在本机形态下退化为「从无到有」；而 P4-05 教训
  （无纹理驻留的 CPU staging 桥实测慢 fragment 两个数量级）+ W3 CUDA
  interop 未落地（无纹理驻留路径）→ 立项前提不成立。触发条件：W3
  CUDA interop 生产化后重评。

### 5. export（导出）

| 路径 | fps | 对照 | 来源 |
|---|---|---|---|
| 4K HEVC 10bit（生产语义 workers=3） | **30.2（hevc_nvenc）** | x265 4.0 → **7.6×** | BENCH_HW_DECODE_WINDOWS.md NVIDIA 节 |
| 4K H.264 8bit（workers=3） | **32.9（h264_nvenc）** | x264 18.7 → 1.8× | 同上 |
| 4K AV1 10bit（workers=3） | **29.4（av1_nvenc）** | — | 同上 |

- NVENC 自动接入已收口（W6-01，vendor 泛化路由）；
  同码率画质 A/B（真实素材）留 W6-01 正式验收项。
- 归因：**export 段是 NVIDIA 专属收益最大段**（HEVC 10bit 场景 7.6×）。

## 结论（NVIDIA 组合收益分段）

| 段 | 收益 | 状态 |
|---|---|---|
| decode | CPU planar 已足额；NVDEC 可用但单流不必要 | ✅ 已归档 |
| interop | 可行性成立，生产实现挂起（W3 决策） | 🔶 |
| present | 维持 GL（决策） | ✅ |
| compute | GL compute 不可用；CUDA 不立项（前提不成立） | ✅ 已裁决 |
| export | NVENC 已接线，HEVC 10bit 7.6× | ✅ 已收口 |

## 附：三项决策记录（本分册一并归档）

### D1 W3 立项决策（K-W1/K-W2 门槛裁决）

**决定：W3（zero-copy interop 生产实现）本轮不启动，保留为证据驱动的后续项。**

- K-W1（zero-copy 立项门）：两台真机的 download 基线均足额——
  NVIDIA 4K CPU planar 320-945fps（预算 12.8~38×）、NVDEC 163-196fps
  （7.8×）；AMD CPU planar 172-486fps。单流帧预算不构成 zero-copy 的
  必要性证据。
- K-W2（interop 可行门）：不否决——双 vendor `interop_open_ok` +
  AMD 机 roundtrip 原型通过。技术风险已在原型级排除大半。
- 重启触发条件：多轨 4K 播放的 `TOPOS_PLAYBACK_DECODE_TELEMETRY=N`
  遥测显示 decode_ms 随轨数线性恶化且单流不超预算（K-W1 触发信号，
  取数入口已就位）→ 重启 W3-01~06。
- 期间 zero-copy 相关产物维持原型/探针级，不进生产分支。

### D2 W4-03 DirectML 依赖决策（开放问题 6 关闭）

**决定：不引入 onnxruntime-directml 为默认依赖；登记 optional extras
（如 `[project.optional-dependencies] ai-dml`），触发条件 = 出现真实
ONNX 模型需求（如 AI 降噪立项）。**

- 证据（AMD 机 probe + 本机裁决）：DML EP 开箱即用但小模型与 CPU 打平
  （收益需重模型复测）；+70.9MB 包体（对默认安装显著）；MIT 许可无分发
  障碍；GL compute 在 Windows 不可用使 DML 语境增强，但无模型需求时
  「为了用 GPU 而引入」违反 K-W4 语义。
- 能力不足时显式诊断回退 CPU/GL 的路径由既有回退词表承载。

### D3 W4-02 CUDA compute 试点裁决（K-W4）

**决定：不立项。** P4-05 教训（CPU staging 慢 fragment 两个数量级）+
W3 CUDA interop 未落地（无纹理驻留路径）→ 立项前提不成立。W3 重启
且 CUDA interop 生产化后再评（A/B ≥15% 或承载 ONNX 模型）。
