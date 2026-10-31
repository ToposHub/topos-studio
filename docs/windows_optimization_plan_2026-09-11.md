# Windows GPU 优化与后端落地计划（Phase 8/9 展开子计划）

> 状态：执行中（2026-09-12 批次 3/4 + **NVIDIA RTX 4090 真机批次 1-6 全部完成**：W0-W2 全收口、W6-01 NVENC 收口、W7-01 双机分册、W3/W4-02/W4-03 三项裁决落档、W5-01/05-03 probe 齐；剩余项全部为有明确触发条件的后续项，见 §11 末尾）
> 编制日期：2026-09-11
> 适用项目：Topos Color（PyQt6 + ModernGL/GLSL + FFmpeg/PyAV + native codec）
> 上游计划：`docs/metal_backend_migration_and_cross_platform_gpu_plan.md` —— 本文档是上游 **Phase 8/9 的展开与执行细化**，不取代、不冲突；四维接口（graphics/compute/decode/interop）、回退语义（§4.3）、门禁体系（§6.4）、线程边界（§7.1）全部沿用上游。
> 编制基线：commit `214d76e3`；行号引用以符号名为准（上游 P9D-04 约定）。
> 工作量标记：S ≤0.5 天｜M 1~2 天｜L ≥3 天
> 状态标记：⬜ 未开始｜🔶 进行中｜✅ 已完成

---

## 0. 为什么单独成文

1. **上游 Phase 8/9 只有表级条目**（P8-01~08、P9-01~06），没有执行顺序、没有「本机能做什么 / 必须真机做什么」的边界、没有验收产物形态。本文补齐。
2. **平台基线不对称**（上游 §2.5）：`macos_vt/` 10 文件 / 6,570 行真实实现；`windows_hw/` 约 880 行——1 个真实文件 + 1 个接口层探针 + 若干诚实骨架。Windows 侧的每一步都要先想清楚「这一步是否需要 Windows 硬件」。
3. **决策 A′（上游 §0.2）之下，Windows 不是性能基线机**：Apple Silicon 是 A′ 主目标；Windows 是多平台交付目标。Windows 上的性能结论**只能**在 Windows 目标硬件上实测，本机（Intel + AMD 独显，macOS）不得外推。

---

## 1. 当前 Windows 代码基线（证据表）

| 文件 | 行数 | 性质 | 证据 / 接入点 |
|---|---:|---|---|
| `src/shared/media/windows_hw/hw_planar_source.py` | 286 | **真实实现** | `edit_gpu_decode_manager` 按 `sys.platform == 'win32'` + `TOPOS_FORCE_WIN_HW_DECODE`/`TOPOS_DISABLE_WIN_HW_DECODE` + `has_nvidia_gpu()` 门控接入；走 `HWACCEL_DOWNLOAD_PLANAR_YUV`（**非零拷贝**）；open() 首帧验证输出格式，失败抛 `MediaOpenError` 回退 CPU planar；P8-08 已交付 12 测试 |
| `src/shared/media/windows_hw/gpu_capability.py` | 214 | **接口层（P8-01）** | ctypes DXGI 适配器枚举（vtable 索引按微软文档钉死）+ nvml.dll 驱动版本（可选）；COM 走查层**未经真机验证**；数据整形层测试以假数据注入覆盖 |
| `src/shared/media/windows_hw/capability.py` | 154 | **数据驱动门禁** | `probe_zero_copy_capability()`：not_windows / env off / probe 失败 / NVIDIA 缺驱动版本 / `WGL_NV_DX_interop` 探测，逐分支显式 `reason`；env 只作强制开关不作能力证据（P0-06 语义） |
| `src/shared/media/hwaccel.py` | 1,091 | **真实（跨平台共享）** | `HardwareDecoder` 枚举（NVDEC/D3D11VA/QSV/CUDA/VAAPI/VIDEOTOOLBOX）+ `HWACCEL_CODEC_SUPPORT` 矩阵 + `FFmpegHwaccelSource`；Windows 的解码工作项是**扩展它**，不是新建 |
| `src/shared/media/decode_policy.py` | — | 平台路由表 | 「Windows NVIDIA：NVDEC/NVENC 可选，fallback PyAV CPU；Windows Intel：QSV 可选」 |
| `src/shared/media/windows_hw/d3d11_decoder.py` | 79 | 诚实骨架 | `open()` 记 warning 返回 False（`_SUPPORTED_CODECS` 含 av1） |
| `src/shared/media/windows_hw/d3d11_gl_bridge.py` | 66 | 诚实骨架 | `initialize()` 返回 False，`last_fallback_reason="interop_not_yet_implemented"`；**ring_size=4 的环形结构已预留** |
| `src/shared/media/windows_hw/windows_zero_copy_probe.py` | 45 | 诚实骨架 | 返回未实现 |
| `src/shared/export/encoder.py` | — | **接口缝** | `EncodeParams.hwaccel` 字段已预留 `nvenc`/`qsv`/`videotoolbox` 值；缺能力探测与质量参数映射 |
| `tests/media/test_windows_hw_*.py` | 3 个 | 契约测试层 | zero_copy 骨架断言 + planar source 12 用例 + gpu_capability 数据整形注入测试 |

**环境开关现状**（新工作项必须沿用同一命名风格）：

| 开关 | 现义 |
|---|---|
| `TOPOS_FORCE_WIN_HW_DECODE` | 强制启用 Windows 硬解 planar 路径（绕过 has_nvidia_gpu 前置） |
| `TOPOS_DISABLE_WIN_HW_DECODE` | 显式禁用 Windows 硬解 |
| `TOPOS_VIDEO_ZERO_COPY=auto\|off\|force` | Windows 零拷贝总开关（force 不作能力证据，只作诊断） |
| `TOPOS_VIDEO_FORCE_CPU_UPLOAD=1` | 强制 CPU 上传 |
| `TOPOS_PLAYBACK_DECODE_TELEMETRY=N` | 播放解码波遥测：每 N 帧导出一次（decode_ms 按 decode_layers 分桶 + 超预算率，落临时目录 JSON + INFO 日志）；W3 立项 K-W1 触发信号的取数入口，默认 0 = 关闭（零开销） |

---

## 2. 范围与两个硬边界

### 2.1 硬件边界（每个条目必须标注）

| 标记 | 含义 |
|---|---|
| **[本机]** | 无 Windows 硬件即可完成：契约、测试层（注入假数据）、文档、基准素材、CI 逻辑层 |
| **[需真机]** | 需要任意一台 Windows 机器（含 iGPU）：probe 真实验证、D3D11VA、能力矩阵 |
| **[需真机+vendor]** | 需要特定 vendor 硬件：NVDEC/CUDA/NVENC 需 NVIDIA；QSV 需 Intel；AMF 需 AMD |

### 2.2 复用边界（不得重建清单，上游 §10.2-8 的 Windows 展开）

1. `hw_planar_source.py` 的硬解下载路径**已接线已测试**，后续只扩展不重写。
2. `hwaccel.py` 的 `HardwareCapability` 探测与 codec 矩阵是唯一解码能力真相源。
3. interop/present 层必须对接 `present_frame.py` 的 `TextureLease`/`GpuFrameRelease` 状态机（上游 §3.3 规则 5），**禁止第二套 lease 协议**。
4. 诊断字段必须落 `backend_selector` 的 trail/reason 语义（显式 reason 枚举，禁止静默降级，`.clinerules:11`）。
5. Windows 后端名直接复用 P7-06/07/08 已交付的 `graphics_backends`/`compute_backends` 词汇表（`d3d11`/`d3d12`/`cuda`/`directml` 等），不发明新词。
6. `DecodePolicy` 平台路由是解码路径唯一决策点，vendor adapter 不得绕过它自建路由。

### 2.3 一个与 macOS 根本不同的事实（决定 W4 立项依据）

**Windows 上桌面 OpenGL 4.5/4.6 是普遍可用**（NVIDIA/AMD/Intel 三家 Windows 驱动均支持 GL 4.6），而 macOS 被钉死在 4.1。这意味着：

- 上游 §0.4 的「macOS 无 compute → Metal compute 解锁」**在 Windows 上不成立**：`compute_shader_manager.py` 的 GL compute（blur/erode_dilate/histogram）在 Windows 上本来就是可用路径，fragment 回退大概率不触发。
- 因此 **CUDA/DirectML 在本项目 Windows 侧的定位不是「解锁 compute」，而是**：(a) NVDEC 零拷贝的伴生互操作；(b) 特定 kernel 的可选加速（须 A/B 证明）；(c) ONNX AI 模型（如 AI 降噪）的 DirectML 执行提供方。
- **任何「把 GL compute 改写成 CUDA/DirectML」的工作项默认不立项**，除非 A/B 数据支持（W4 门禁）。

---

## 3. 分阶段执行计划

### Phase W0：本机准备（无 Windows 硬件，随时可做）

| 编号 | 工作项 | 主要文件 | 交付物 | 验收标准 | 量 | 边界 |
|---|---|---|---|---|---|---|
| W0-01 | 解码测试素材矩阵定义与托管（衔接上游 P0-03 托管决策） | `tests/media/fixtures_manifest.json`、`generate_test_fixtures.py` | Windows 解码素材清单 | H.264/HEVC/AV1 × 8/10-bit × 4:2:0/4:2:2 × 1080p/4K 可复现；**4:2:2 10-bit 必含**（NVDEC/D3D11VA 均不支持的诊断用例） | M | [本机]（托管决策依赖上游 P0-03） |
| W0-02 | `d3d11_decoder`/`d3d11_gl_bridge` 契约测试扩展（沿用 gpu_capability 假数据注入模式） | `tests/media/test_windows_hw_*` | 走查层注入式测试 | D3D11 骨架的 reason 枚举、ring 结构、失败计数路径全部有测试钉死，真机实现时契约不漂移 | M | [本机] |
| W0-03 | Windows 能力描述符补全到 `BackendCapabilities` | `src/shared/gpu/capabilities.py`、`windows_hw/` | 四维快照含 d3d11/cuda/directml 维度 | `backend_selector` 在非 Windows 上对 d3d11/cuda 显式报 `not_windows` 类 reason（不报未知错误）；快照可序列化 | S | [本机] |
| W0-04 | 导出侧 NVENC/QSV 接缝补能力查询与参数映射设计 | `src/shared/export/encoder.py` | hwaccel 能力探测 + codec 名映射单测 | `h264_nvenc`/`hevc_nvenc`/`av1_nvenc`/`h264_qsv` 映射有测试；无硬件时显式诊断（沿用 encoder 现有错误通道） | S | [本机]（映射逻辑）；[需真机]（真实编码） |
| W0-05 | 回退语义落表（见 §4）为 reason 枚举代码级定义 | `windows_hw/capability.py`、`backend_types.py` | reason 枚举 + 文档 | 每个失败点一个显式 reason 字符串常量，禁止裸字符串散落 | S | [本机] |

### Phase W1：真机验证第一周（probe 与门禁打通）

| 编号 | 工作项 | 主要文件 | 交付物 | 验收标准 | 量 | 边界 |
|---|---|---|---|---|---|---|
| W1-01 | DXGI COM 走查层真机验证（P8-01 收尾） | `gpu_capability.py` | 真机 probe 报告 | EnumAdapters/GetDesc 在真实驱动上返回合理数据；vtable 索引如有偏差在此修正并更新测试 | S | [需真机] |
| W1-02 | `WGL_NV_DX_interop` 真机探测（`capability.py` 的 `_detect_wgl_nv_dx_interop` 已实现，验证之） | `capability.py` | 扩展支持矩阵 | 逐 vendor 记录：扩展名虽带 NV，NVIDIA/AMD/Intel 各自是否实现、GL 上下文存在时是否可用——**不得按 vendor 猜测，只信探测** | S | [需真机] |
| W1-03 | multi-GPU / Optimus 混合显卡适配器匹配（P9-06） | `gpu_capability.py`、`backend_selector.py` | adapter 匹配规则 + 诊断 | decode GPU 与 render GPU 不一致时：或锁定同一 adapter，或显式诊断 + 回退 download；双适配器笔记本（iGPU+dGPU）实测 | M | [需真机] |
| W1-04 | vendor × codec × 格式能力矩阵初版（P9-01 数据收集） | `tests/media/results/windows_capability_matrix.{md,json}` | 矩阵归档 | unsupported 格式逐项记录（含 4:2:2 10-bit）；产物 `.md`+`.json` 配对（P9C-02 约定） | M | [需真机] |
| W1-05 | `hw_planar_source.py` 真机首验（12 个契约测试的真机复核 + `TOPOS_FORCE_WIN_HW_DECODE` 路径） | `hw_planar_source.py` | 真机验证记录 | H.264/HEVC NV12/P010 实测产出与 `msb_aligned_p010` 元数据契约一致；P8-08 注明的「真实硬解验证留待 Windows 机器」关闭 | S | [需真机+NVIDIA 或任意 D3D11VA] |

### Phase W2：解码后端矩阵（P8-02 + P9-03/04）

**顺序策略：先 download（已有路径的覆盖面扩展），后 zero-copy（W3 之后）。** download 路径是 Windows 播放性能的第一收益（对齐上游 §0.3 的「解码占 ~82%」结论——Windows 上同样成立，硬解下载已能消掉该大头）。

| 编号 | 工作项 | 主要文件 | 交付物 | 验收标准 | 量 | 边界 |
|---|---|---|---|---|---|---|
| W2-01 | NVDEC 解码能力查询补全（P8-02 第一半） | `hwaccel.py`、`hw_planar_source.py` | codec/bit-depth/resolution 能力查询 | H.264/HEVC/AV1 逐项可查询；AV1 硬解（Turing+）进 `HWACCEL_CODEC_SUPPORT` 与 `_HW_SEMI_PLANAR_FORMATS` 路由评估 | M | [本机]（矩阵逻辑）+ [需真机+NVIDIA]（实测） |
| W2-02 | `hw_planar_source` 的 vendor 放开（P9-03/04 前半） | `edit_gpu_decode_manager` 的 `has_nvidia_gpu()` 门控、`hw_planar_source.py` | Intel/AMD 机器同样走硬解下载 | 门控从「仅 NVIDIA」改为「capability 探测允许即放行」；Intel QSV / AMD D3D11VA 实测 NV12/P010 产出契约一致 | M | [需真机+vendor] |
| W2-03 | D3D11VA 通用回退路径（P9-02 的 decode 半边，「通用 Windows：必须」档） | `hwaccel.py`、`decode_policy.py` | FFmpeg d3d11va hwaccel 接入 | 无 NVIDIA 机器上硬解可用或显式诊断；`get_platform_default` 顺序实测校验 | M | [需真机] |
| W2-04 | 不支持格式的显式诊断补全 | `hw_planar_source.py`、`decode_policy.py` | 诊断枚举 | 4:2:2 10-bit 等 NVDEC/D3D11VA 不支持的流：`MediaOpenError` + reason（**沿用 open() 首帧验证先例，禁止静默软解伪装硬解**） | S | [本机]（逻辑）+ [需真机]（用例） |
| W2-05 | 播放 A/B：硬解下载 vs CPU 软解（Windows 版 §0.3 基线） | Phase 0 harness（`benchmark_yuv_gpu_pipeline.py`） | Windows 基线归档 | 4K H.264/HEVC 的 decode_ms/CPU% 分段归档，`.md`+`.json` 配对；作为后续 zero-copy 的对照组 | M | [需真机] |

### Phase W3：interop 与同步（P8-04 + P8-05 + P9-02 —— 零拷贝关键路径）

**立项受 K-W1 门禁约束（§5）：先有 W2-05 对照基线，才有资格启动本阶段。**

| 编号 | 工作项 | 主要文件 | 交付物 | 验收标准 | 量 | 边界 |
|---|---|---|---|---|---|---|
| W3-01 | D3D11 shared texture 契约（P9-02） | `windows_hw/d3d11_gl_bridge.py`（实现现有骨架） | shared handle 契约 + ring | 骨架中 ring_size=4 结构落地；每帧无新建无上限 texture；`interop_not_yet_implemented` reason 移除 | L | [需真机] |
| W3-02 | WGL_NV_DX_interop GL 侧桥（P8-04 NVIDIA 路径第一选择） | `d3d11_gl_bridge.py`、native 层 | GL↔D3D11 共享纹理 | 解码 surface 无 CPU download 即可被 GL 采样；**对接 `TextureLease` 状态机**（§2.2-3） | L | [需真机] |
| W3-03 | CUDA-GL / CUDA-D3D interop（P8-04 备选） | native 层 | interop 注册/映射/解映射 | W3-02 不可用时的备选；同样无 CPU download | L | [需真机+NVIDIA] |
| W3-04 | CUDA/D3D fence 与 sync token（P8-05） | native 层、`GpuFrameRelease` | sync token 契约 | CUDA 与 graphics 无未同步读写；释放时机由 `release_when_fenced()` 语义承载，不新造第二套 fence | M | [需真机] |
| W3-05 | zero-copy 能力判定真数据化 | `capability.py`、`topos_surface_capability.py` | probe 证据链 | `available=True` 仅当 interop 实测成功（含首帧端到端验证），延续 P0-06「探测为准」语义 | S | [需真机] |
| W3-06 | zero-copy vs download A/B（P8-07 的解码半边） | Phase 0 harness | 对比归档 | 同素材同帧范围：download 路径 vs interop 路径的帧时/CPU%/带宽分段可解释（上游 §6.2 指标集） | M | [需真机] |

### Phase W4：compute 后端（P8-03 + P9-05，立项门槛见 §2.3 与 K-W4）

| 编号 | 工作项 | 主要文件 | 交付物 | 验收标准 | 量 | 边界 |
|---|---|---|---|---|---|---|
| W4-01 | **GL compute 现状确认**（先于一切 CUDA/DirectML 工作） | `compute_shader_manager.py`（GL 4.3+ 路径） | Windows GL compute 实测记录 | blur/erode_dilate/histogram 在 Windows GL 4.6 上不触发 fragment 回退；直方图/Normalize 链路（P4-03 语义）可用 | S | [需真机] |
| W4-02 | CUDA compute kernel 试点（仅当 W3-03 已落地、且纹理驻留无 staging） | `windows_hw/cuda_compute.*` | resize/YUV/denoise probe kernel | **P4-05 教训必须前置**：CPU staging 桥曾实测慢于 fragment（87~438ms vs 0.55~0.94ms）——无纹理驻留路径一律不立项；有驻留时与 GL compute A/B | L | [需真机+NVIDIA] |
| W4-03 | DirectML 可选 compute（P9-05） | `windows_hw/`、onnxruntime-directml（可选依赖） | AI 降噪等 ONNX 模型的 DML 执行提供方 | 不依赖 NVIDIA 的 GPU compute 通道；依赖是否引入（`pyproject` extras）先做依赖决策；能力不足显式诊断回退 CPU/GL | L | [需真机+vendor] |
| W4-04 | compute 后端 A/B 归档（P8-07 的 compute 半边） | harness | A/B 报告 | GL compute / CUDA / DirectML / CPU 四选组合分段可解释；默认档只按数据定 | M | [需真机] |

### Phase W5：呈现与 HDR（上游 §13 开放问题 6 的 Windows 半边）

| 编号 | 工作项 | 主要文件 | 交付物 | 验收标准 | 量 | 边界 |
|---|---|---|---|---|---|---|
| W5-01 | 第一阶段呈现保持现状：OpenGL/WGL（上游 Phase 8 注记的落地确认） | 无新代码 | 决策记录 | W3 完成前不引入 D3D swapchain；记录在案防止范围漂移 | S | [本机]（决策） |
| W5-02 | D3D11 flip-model swapchain 评估（present 段收益） | `windows_hw/`、native 层 | 评估报告 + probe | flip discard / waitable object / frame pacing 实测数据；与 GL present 对比 | M | [需真机] |
| W5-03 | HDR 呈现方案裁决：scRGB(float16) vs HDR10(R10G10B10A2+PQ) | `windows_hw/`、色彩 metadata 链路 | 裁决 ADR + 实测报告 | 上游 §13-6 关闭；HDR 元数据（matrix/primaries/transfer）端到端正确；与 macOS P10-05 的 EDR 验收同表对齐 | M | [需真机+HDR 显示器] |
| W5-04 | SDR 色彩管理一致性 | 色彩链路 | 一致性报告 | sRGB 段与 macOS/GL 路径画质门禁一致（复用 G 类 golden 门禁思路） | M | [需真机] |

### Phase W6：导出硬编（P8-06）

| 编号 | 工作项 | 主要文件 | 交付物 | 验收标准 | 量 | 边界 |
|---|---|---|---|---|---|---|
| W6-01 | NVENC 导出路径（h264/hevc/av1_nvenc） | `src/shared/export/encoder.py` | NVENC 编码接入 | 转码场景不回落 CPU；质量/码率参数映射与 x264 基线画质 A/B；无 NVENC 显式诊断回退 CPU 编码 | M | [需真机+NVIDIA] |
| W6-02 | QSV/AMF 导出（对应 W2 vendor 矩阵） | `encoder.py` | Intel/AMD 硬编接入 | 同上验收；不可用显式诊断 | M | [需真机+vendor] |
| W6-03 | GPU decode → GPU encode 全链路 | `src/shared/export/`、依赖 W3 | 全 GPU 转码链路 | 中间帧不经过 CPU（零拷贝导出）；失败逐段显式回退并记录 reason | L | [需真机] |

### Phase W7：基准、CI 与打包工程化（P8-07 收尾 + 上游 §9 的 Windows 半边）

| 编号 | 工作项 | 主要文件 | 交付物 | 验收标准 | 量 | 边界 |
|---|---|---|---|---|---|---|
| W7-01 | 四维组合 A/B 总报告（P8-07） | harness | NVIDIA A/B report | OpenGL、D3D、CUDA 组合的收益**分段可解释**（decode/interop/present/compute/export 逐段归因） | M | [需真机] |
| W7-02 | Windows CI 逻辑层 job | `.github/workflows/` | windows-latest job | 无 GPU 的逻辑层用例常跑（capability 数据整形、planar 契约、hwaccel 枚举、W0-04 映射）；硬解/interop 用例诚实 skip 并标注原因 | M | [本机]（可先建）+ [需真机 runner]（GPU 用例） |
| W7-03 | 打包：DLL/依赖清单与签名 | `packaging/pyinstaller/base_spec.py` | Windows 打包 spec | PyAV/FFmpeg hwaccel 相关 DLL 进 binaries；signtool 签名流程；扩展缺失时程序仍可启动并回退（上游 §9.1 回退要求同源） | M | [需真机]（签名/安装实测） |
| W7-04 | 归档约定执行 | `tests/media/results/` | `.md`+`.json` 配对产物 | 所有 W 阶段报告遵循 P9C-02 同构约定；性能结论标注目标硬件型号与驱动版本 | S | [本机]（约定） |

---

## 4. 失败与回退语义（上游 §8.2 的代码级展开）

**硬规则：每个失败点一个显式 reason 常量；「看似成功实际软解」视为缺陷，不是降级**（`hw_planar_source.open()` 首帧验证是既有先例）。

| 失败点 | 回退 | reason 常量（建议） | 记录点 |
|---|---|---|---|
| 非 Windows 平台 | 路径不启用 | `not_windows` | `capability.py`（已实现） |
| env 显式关闭 | CPU planar | `env_zero_copy_off` / `env_force_cpu_upload` | `capability.py`（已实现） |
| DXGI probe 失败 | CPU planar + 诊断 | `gpu_probe_failed:<子原因>` | `capability.py`（已实现） |
| NVIDIA 驱动版本读不到 | 拒绝专属路径 | `nvidia_driver_version_unavailable` | `capability.py`（已实现，K-W3） |
| WGL_NV_DX_interop 扩展缺失 | D3D copy / download | `wgl_nv_dx_interop_unavailable` | `capability.py`（已实现） |
| interop 初始化失败 | 硬解 download（`hw_planar_source`） | `interop_init_failed:<阶段>` | `d3d11_gl_bridge.last_fallback_reason`（骨架已有字段） |
| 流不支持硬解（如 4:2:2 10-bit） | CPU planar + 显式诊断 | `codec_unsupported_by_hw:<codec/pixfmt>` | `hw_planar_source.open()`（先例：首帧验证） |
| 硬解中途失效 | 父类 CPU 兜底 | 复用 P8-08 测试钉死的兜底语义 | `hw_planar_source.py`（已实现） |
| NVENC/QSV/AMF 不可用 | CPU 编码 + 诊断 | `encoder_hw_unavailable:<hwaccel>` | `encoder.py` 错误通道 |
| decode GPU ≠ render GPU | 锁定同 adapter 或 download | `adapter_mismatch:<luid 对>` | W1-03 |
| CUDA 不可用 | DirectML / GL compute / CPU | `cuda_unavailable` | W4 |
| 驱动版本低于 gate | 拒绝专属路径 | `driver_below_gate:<版本>` | W1-04 矩阵随附 |

所有回退必须保留正确性观测（画质门禁）与可观测性（trail/reason 进 `backend_selector` 诊断链），不能把「没有丢帧」当唯一成功标准（上游 §8 结语）。

---

## 5. 门槛与中止条件（K-W 系列，对应上游 K1~K5 的 Windows 版）

| 编号 | 门槛条件 | 检查点 |
|---|---|---|
| K-W1 | **zero-copy 立项门**：W2-05 的 download 基线显示硬解下载已满足目标帧预算时，W3 全阶段（interop/fence）默认不启动，只保留为「证据驱动」的后续项——避免为架构美感引入一整个 native interop 层 | W2-05 归档后 |
| K-W2 | **interop 可行门**：W1-02 显示 `WGL_NV_DX_interop` 在目标 vendor 上不可用**且** W3-03 CUDA interop 亦不可行 → W3 降级为「D3D copy 路径评估」或整体搁置，登记 ADR | W1-02 / W3-03 |
| K-W3 | **驱动 gate**：NVDEC/CUDA/NVENC 路径要求驱动版本下限；读不到版本一律拒绝（`capability.py` 已实现该语义，扩展到 W6 导出） | 各 probe 点 |
| K-W4 | **compute 替换门**：Windows GL compute 可用（W4-01 预期成立）时，CUDA/DirectML 仅在 A/B 显示 ≥15% 收益或承载 ONNX 模型时立项——**不为了「用了 GPU」而迁移** | W4-01 之后 |
| K-W5 | **验收诚实门**：Intel/AMD 矩阵若无对应真机，P9 行保持「探测可用即放行，无真机验收记录」的显式标注，不得在任何文档写成「已验证」 | 持续 |

---

## 6. 风险与明确不做事项

### 6.1 主要风险

| 风险 | 影响 | 控制措施 |
|---|---|---|
| **Qt 6 在 Windows 的 GL 后端与 WGL 互操作兼容性**（desktop OpenGL vs ANGLE；本项目强制 QSurfaceFormat 4.5 Core + AA_ShareOpenGLContexts） | 高 | W1-02 前置 probe：确认 ModernGL 所用上下文是 WGL 且 interop 扩展在该上下文可见；K-W2 兜底 |
| 驱动碎片化（NVIDIA Studio/Game、AMD Adrenalin 版本行为差异） | 中 | 驱动版本进 probe 报告与矩阵归档（W1-04）；K-W3 版本 gate |
| Optimus/混合显卡 decode 与 render GPU 不一致 | 中 | W1-03 锁定/诊断规则 |
| Python 热路径 native 调用开销（解码回调、interop 映射） | 中 | 沿用上游 §10.1 结论：Python 做编排，热路径走 ctypes/原生层（`hw_planar_source` 的 PyAV 路线已是此形态） |
| GL/D3D/CUDA 资源在一条链路里无边界混用 | 高 | 上游 §10.2-6 原文适用；interop 边界收敛在 `windows_hw/` 桥层 |
| 与上游 Phase 10（macOS UI 表面）的契约漂移 | 中 | W3-05 对接 `TextureLease` 时若发现契约缺口，回上游 ADR 修订，不 Windows 侧私改 |

### 6.2 明确不做

1. 不重写 `hw_planar_source.py` / `hwaccel.py` 既有路径（只扩展）。
2. 不把 CUDA 写成 Windows 唯一方案（上游 §10.2-5 原文）；AMD/Intel/通用档必须始终存在（「通用 Windows：必须」档优先级最高）。
3. 不在 interop 契约（TextureLease 对接）验证前引入 D3D swapchain/present（W5 排在 W3 之后）。
4. 不在无 A/B 数据时把 GL compute 改写为 CUDA/DirectML（§2.3、K-W4）。
5. 不在本机（macOS）对 Windows 专属路径做任何「模拟验证后宣称可用」——本机产物只到契约/测试层为止（§3 各表「边界」列）。
6. 不新建第二套 fence/lease/回退通知（分别归 `GpuFrameRelease`、`TextureLease`、`_fallback_callback`）。

---

## 7. 尚未验证的开放问题（诚实清单）

| # | 问题 | 归属 |
|---|---|---|
| 1 | ~~DXGI COM vtable 走查层在真实 Windows 驱动上的正确性~~ **已关闭（AMD+NVIDIA 双机验证）** | W1-01 ✅ |
| 2 | `WGL_NV_DX_interop` 逐 vendor 行为细节：**OpenDevice 已双 vendor 验证**；锁语义/RegisterObject/MapTextures 与 Qt GL 上下文共存仍待 W3 生产实现验证 | W1-02 ✅ / W3 |
| 3 | ~~Windows 上 ModernGL/Qt 实际创建的 GL 上下文类型~~ **已关闭**（Qt 4.5 Core 即所得；standalone 3.3 为 glcontext 后端行为；GL compute 双 vendor 不可用见 W4-01） | W4-01 ✅ |
| 4 | D3D11 HDR 呈现：scRGB vs HDR10 swapchain（上游 §13-6）——**本批两机显示器均不足以实测**（4090 机 8bpc SDR；AMD 机 HDR400 面板待开 HDR 复测） | W5-03 |
| 5 | AV1 硬解：**根因已定位（双机同源）**——PyAV 18.1.0 wheel 的 FFmpeg 构建缺 AV1 hwaccel 配置（4090 有解码硬件仍静默软解）；升级路径 = 自编 FFmpeg / 等 wheel 更新 | W2-01 收窄 |
| 6 | ~~onnxruntime-directml 依赖是否引入~~ **已关闭（D2：extras 化 `ai-dml`，不进默认依赖）** | W4-03 ✅ |
| 7 | 云 GPU（GitHub Windows runner 无独显；自托管 runner 或云工作站）能否替代真机完成第一轮验证——本批以双台真机实体完成，此问题优先级降低 | 环境 |
| 8 | NVENC 质量参数（rate control、B 帧、lookahead）与既有 x264 质量档的画质 A/B 结论——**合成素材探针已过（码流 sane），真实素材同码率 A/B 待做** | W6-01 验收 |

---

## 8. 与上游计划的条目映射（可追溯）

| 上游条目 | 本计划条目 | 状态 |
|---|---|---|
| P8-01 能力探针 | （前置已交付）+ W1-01/W1-02 真机收尾 | ✅ 2026-09-12 AMD 真机收口（IID/vtable/DESC 布局三处修复 + WGL 探测 bug 修复；见 tests/media/results/）|
| P8-02 NVDEC decode backend | W2-01/W2-02（+W2-05 基线） | ✅（NVDEC 能力逐项实测 + 4K 基线归档；AV1 生效受 PyAV wheel 限制，显式诊断兜底）|
| P8-03 CUDA compute | W4-02（受 K-W4 约束） | ⏭ 不立项（D3 裁决：前提不成立；W3 重启后再评）|
| P8-04 D3D11/D3D12 interop | W3-01/W3-02/W3-03 | ⏸ 双 vendor interop_open_ok + 原型通过；生产实现按 D1 裁决挂起（遥测触发重启）|
| P8-05 CUDA/D3D fence | W3-04 | ⏸ 随 W3 挂起 |
| P8-06 NVENC export | W6-01/W6-02/W6-03 | ✅ W6-01 收口（NVENC vendor 泛化，HEVC 10bit 7.6×）+ W6-02 AMD 半边收口；W6-03 随 W3 挂起 |
| P8-07 与 OpenGL 对比 | W2-05 + W3-06 + W7-01 | ✅ 双机分册归档（`windows_four_dim_ab_{amd,nvidia}.md` 五段归因）；W3-06 随 W3 挂起 |
| P8-08 hw_planar_source 测试 | （已交付，真机复核 = W1-05） | ✅ |
| P9-01 capability matrix | W1-04 | ✅ 双机归档（AMD RX 6800 + NVIDIA RTX 4090 分册，windows_capability_matrix_*.{md,json}）；Intel 独显行无真机不写（K-W5）|
| P9-02 D3D generic texture contract | W3-01（+W2-03 decode 半边） | ⬜ |
| P9-03 QSV/oneVPL | W2-02/W6-02 | ⬜ |
| P9-04 AMF | W2-02/W6-02 | ⬜ |
| P9-05 DirectML | W4-03（受 K-W4 约束） | ✅（决策收口 D2：不进默认依赖，extras 化 `ai-dml`，触发=真实 ONNX 模型需求；开放问题 6 关闭）|
| P9-06 multi-GPU matching | W1-03 | ✅（iGPU+dGPU 桌面机形态实测通过：LUID 匹配规则 + `adapter_mismatch` 诊断 + 同名适配器多 LUID 发现；真 Optimus 动态切换笔记本留复核）|

---

## 9. 执行顺序

```text
0. W0（[本机] 五项，无需等硬件；W7-02 CI 可同批）
1. Windows 验证环境到位（采购/自托管 runner，开放问题 7）
2. W1（真机第一周：probe 验证 + adapter 匹配 + 矩阵初版 + hw_planar_source 真机首验）
3. W2（download 覆盖面扩展 → W2-05 基线归档）
4. K-W1/K-W2 判定 → 决定 W3 是否启动（zero-copy）或维持 download 架构
5. W4/W6 按需（compute/导出，均带各自门槛）
6. W5/W7（呈现/HDR 与工程化收尾）
```

**与上游 §12 的衔接**：上游执行序第 9 步（Phase 10 macOS UI 表面）仍优先于 Windows 大规模接入（第 10 步）——Windows 后端应复用 macOS 线已稳定的跨后端契约（`TextureLease`、`GpuDecodedFrame`、`BackendCapabilities`）。W0 全部与 W7-02 不依赖该顺序，可随时开工。

---

## 10. 官方参考

**NVIDIA**
- [NVDEC Video Decoder API](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.0/nvdec-video-decoder-api-prog-guide/index.html)
- [NVENC Video Encoder API](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.0/nvenc-video-encoder-api-prog-guide/index.html)
- [CUDA Graphics Interoperability](https://docs.nvidia.com/cuda/cuda-programming-guide/04-special-topics/graphics-interop.html)
- [WGL_NV_DX_interop 扩展规范](https://registry.khronos.org/OpenGL/extensions/NV/WGL_NV_DX_interop.txt)

**Microsoft**
- [Direct3D 11 Video APIs](https://learn.microsoft.com/en-us/windows/win32/medfound/direct3d-11-video-apis)
- [DXGI 翻转模型](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/dxgi-flip-model)
- [Direct3D 11 HDR / 高级色彩](https://learn.microsoft.com/en-us/windows/win32/direct3darticles/high-dynamic-range)
- [IDXGIFactory1 / IDXGIAdapter::GetDesc](https://learn.microsoft.com/en-us/windows/win32/api/dxgi/)

**Intel / AMD / 通用 compute**
- [Intel oneVPL](https://www.intel.com/content/www/us/en/developer/tools/oneapi/onevpl.html)
- [AMD AMF (Advanced Media Framework)](https://github.com/GPUOpen-LibrariesAndSDKs/AMF)
- [DirectML](https://learn.microsoft.com/en-us/windows/ai/directml/gpu-filters)
- [ONNX Runtime DirectML EP](https://onnxruntime.ai/docs/execution-providers/DirectML-ExecutionProvider.html)

**项目内**
- `docs/metal_backend_migration_and_cross_platform_gpu_plan.md`（上游：四维接口、§6.4 门禁、§7 线程边界、§8.2 回退表）
- `docs/module_boundary_rules.md`（平台代码归属：解码/interop 归 `src/shared/media/<platform>/`）
- `docs/taep_engineering_conventions.md`（线程与 GPU 边界）
- `.clinerules`（强制约束：能力不足必须显式诊断）

---

## 11. 执行记录（2026-09-12，本机 = AMD RX 6800 真机）

> 本节由执行批次追加。机器：AMD Radeon RX 6800（0x1002/0x73BF，驱动 26.8.1）+ Win11 26200。
> NVIDIA 专属项按机器实际跳过（NVDEC/NVENC 实测、CUDA interop/compute）。

### 已完成

| 项 | 状态 | 证据 / 产物 |
|---|---|---|
| W0-01 | ✅ | `fixtures_manifest.json` 增 `windows_hw_decode_matrix` 节（AV1×2 + 4:2:2 诊断×3，4:2:2 10-bit 必含）；generator 增 `hw_decode_expectation` 三值语义与 `win_hw_decode_matrix`/`win_hw_decode_diag` 分组 |
| W0-02 | ✅ | `tests/media/test_windows_hw_d3d11_contract.py`（骨架 reason/ring/失败计数钉死） |
| W0-03 | ✅ | `BackendCapabilities.platform` 词典（d3d11/d3d11va/nvdec/qsv/amf/cuda/directml）；非 Windows 显式 `not_windows`；Windows 解码与零拷贝解耦（D3D11VA adapter 级可用 ≠ interop）；`test_platform_capability_kinds.py` |
| W0-04 | ✅ | `encoder.py`：`HWACCEL_ENCODER_NAME_MAP`（nvenc/qsv/amf/videotoolbox × h264/hevc/av1）+ 注册探测 + `encoder_hw_unavailable:<hwaccel>`；`tests/export/test_hwaccel_encoder_map.py` |
| W0-05 | ✅ | `windows_hw/reasons.py`：`FallbackReason` 词表 + 参数化构造器；capability/bridge/decoder/probe/hw_planar_source 全接入；`test_windows_hw_fallback_reasons.py` |
| W1-01 | ✅ | 真机修复三处走查层 bug：① IID_IDXGIFactory1 实为 `{770AAE78-F26F-4DBA-A829-253C83D1B387}`（旧值恒 E_NOINTERFACE）；② `IDXGIAdapter::GetDesc` vtable[6]→[8]（旧索引实调 GetParent）；③ `DXGI_ADAPTER_DESC` 布局 LUID 在末尾（旧偏移致 vendor/显存全错）。修复后 probe 实测 RX 6800/0x1002/0x73BF/16338MB/LUID 正确 |
| W1-02 | ✅（AMD 行） | `scripts/probe_wgl_interop_windows.py` + `results/wgl_interop_probe_amd_rx6800.{md,json}`：**AMD 实现 WGL_NV_DX_interop，`wglDXOpenDeviceNV` 真实打开成功（interop_open_ok）**；顺带修复 `_detect_wgl_nv_dx_interop` 的 wglGetProcAddress bug（旧实现任何机器都误报扩展缺失）。K-W2：W3 不被本门否决 |
| W1-04 | 🔶（AMD 行） | `scripts/probe_windows_capability_matrix.py` + `results/windows_capability_matrix.{md,json}`：d3d11va 下 h264/hevc 420 8/10bit 全 supported（nv12/p010le）；h264/hevc 4:2:2 10bit 如预期静默软解（反证 open() 首帧门控必要性）；**AV1 8bit 420 静默软解**（RDNA2 有解码硬件但 FFmpeg d3d11va 路径未生效 → 开放问题 5 跟进） |
| W7-02 | ✅ | `.github/workflows/windows-logic.yml`：windows-latest 逻辑层 job（最小依赖 av/numpy/pytest；无硬件适配器时真机断言诚实 skip） |

### 门槛判定输入更新

- **K-W2（interop 可行门）**：AMD 行 `interop_open_ok` —— W3 整体可行性 +1；但 OpenDevice ≠ RegisterObject/MapTextures，W3-01/02 仍需逐级验证。
- **K-W1（zero-copy 立项门）**：W2-05 的对照基线已存在（`scripts/bench_hw_decode_windows.{md,json}`，2026-08-29）：AMD 上硬解同步下载 4K 仅 ~47fps（CPU planar 486fps）——download 路径在本机**不**满足帧预算，但 CPU planar 足额。结合 K-W2 的 interop 可行证据：若 W3 立项，zero-copy 恰好绕开「同步下载」瓶颈，AMD 案例的收益假设成立与否由 W3-06 A/B 裁决。立项与否留待决策（native 层工作量 L）。
- **K-W5（验收诚实门）**：本节所有 NVIDIA 结论保持「无真机不写已验证」；本机 Intel iGPU 未在 DXGI 枚举中出现（桌面走 dGPU），QSV 行无数据。

### 新增 / 修订的开放问题

- 开放问题 3（GL 上下文版本）**部分收窄**：standalone（glcontext WGL）显式请求 4.6 Core 仍只得到 3.3 Core；应用内 Qt QSurfaceFormat 4.5 Core 实际版本仍须 W4-01 真机 app 内验证。
- 开放问题 5（AV1 矩阵）**新增数据点**：本机 RDNA2 驱动 26.8.1 上 FFmpeg d3d11va AV1 静默软解——硬件支持与 FFmpeg 路径生效是两回事，后续试 d3d12va/ 显式 hwaccel 配置。

### 复查批次（隐藏 BUG 修复，2026-09-12）

对批次 1–4 全部产物做系统复查，确认 3 个行为性 BUG + 1 个失真注释 + 1 个一致性缺口，均已修复并带回归测试：

1. **reason="ok" 自相矛盾**（`capabilities.py` 两处）：`probe_ok=True` 时 `gpu.reason` 为 `"ok"`，「探测成功但无硬件 vendor 适配器」（VM/远程会话只有 Basic Render Driver）分支直接透传 → 产出 `available=False + reason="ok"`。修复：该场景改报 `no_hardware_adapter` / `vendor_adapter_absent`；`_nvidia_kind` 在 probe 失败时也改报 `gpu_probe_failed:<detail>`（不猜 `cuda_unavailable`——探测失败 ≠ 能力缺席）。
2. **WGL 负结果永久缓存锁死零拷贝**（`windows_hw/capability.py`）：`_detect_wgl_nv_dx_interop` 依赖「调用时是否有当前 GL 上下文」，而 `probe_zero_copy_capability` 把 `wgl_nv_dx_interop_unavailable` 写进模块级缓存——诊断入口在渲染线程建上下文前询问一次，零拷贝即被永久禁用。修复：该负结果不进缓存（正结果/环境门照常缓存），并给 `_detect_wgl_nv_dx_interop` 加 `wglGetCurrentContext` 前置检查（无上下文快速诚实返回 False，已真机验证）。
3. **`h265` 编码器映射别名不齐**（`encoder.py`）：`('nvenc','h265')` 有映射而 qsv/amf/videotoolbox 缺失——`h265` 是项目内活跃拼写（delivery_profile/proxy/seek），同一 codec 串会一侧硬编、一侧静默回退 CPU。修复：三个 hwaccel 补齐别名 + 全 hwaccel 一致性回归测试。
4. `gpu_capability.py` 头 docstring 仍写旧 vtable 注释（"GetDesc=6"）与实现（vtable[8]）矛盾，已同步为真机修正值。

### 本机续作批次（2026-09-12，复查后可做的 5 项全部收口）

| 项 | 状态 | 证据 / 产物 |
|---|---|---|
| W6-02（AMD 半边） | ✅（本机收口） | `scripts/probe_amf_encode_windows.py` + `results/windows_amf_encode_probe_amd.{md,json}`：h264_amf/hevc_amf/hevc_amf p010le 全部通过「真实 open + 30 帧编码 + 码流回解」；**av1_amf 显式失败**（RDNA2 无 AV1 编码单元，open 层 EncoderNotFoundError）——encoder.py 注册/驱动两层探测设计的真机实证。PSNR 对照的诚实边界：合成渐变素材码率不跑满，只证码流 sane。**2026-09-12 检测更正**：8bit 420 SDR AMF 自动接入其实早已落地（commit `8ee8eb69` 2026-08-31，本行早先「导出链路集成未做」失实）；同日完成**收口三件套**——① 10bit HEVC AMF 接线（p010le 转换路线，应用内 HEVC 默认 yuv420p10le 从 CPU x265 ~1.8fps → AMF **22.6fps ≈12×**，端到端 `verify_hw_encode_export.py` 全过 + `scripts/BENCH_HW_DECODE_WINDOWS.md` 归档）；② 接缝统一（video_encoder 消费 encoder.py 的 map/probe，删平行实现）；③ 结构化诊断（探针失败 → `encoder_hw_unavailable:amf:<detail>` → `ExportOutcome.hw_fallback_reason` → worker 留痕）。顺带修复两个潜伏转换缺陷（planar 填充黑帧隐患、packed→p010 兜底崩溃）。2026-09-12 复查再修一处：BT.2020 numpy 精确转换直写 p010le 曾按 3 平面 LSB 直写（亮度缩小 64 倍 + V 丢失；P010 实测 MSB 对齐 500→32000、2 平面 UV 交错），显式 amf+HDR+RGB 源路径触发。剩余：真实素材同码率画质 A/B（正式验收） |
| 开放问题 5 | ✅ 关闭（本构建） | `scripts/probe_d3d12va_windows.py` + `results/windows_d3d12va_probe_amd.{md,json}`：**d3d12va 后端真实可用**（HEVC 10-bit 2K/4K p010le 生效）；H264 d3d12va EINVAL（d3d11va 覆盖）；**AV1 双后端均无 hwaccel 配置**——PyAV wheel 构建层缺失（严格模式 "no stream is compatible"；`codecs_available` 里的 `av1_d3d12va` 是描述符残影，实际 UnknownCodecError）。根因明确，升级路径（自编 FFmpeg / 等 wheel）已登记 |
| W4-03 / 开放问题 6 | ✅（决策证据） | `scripts/probe_directml_windows.py` + `results/directml_probe_amd_rx6800.{md,json}`（**临时 venv 运行，未进依赖**）：DML EP 在 AMD 上开箱即用；小模型与 CPU 打平（收益需重模型复测）；+70.9MB、MIT。建议形态：extras 化 + 有真实 ONNX 模型需求时引入（K-W4 语义），拍板留维护者 |
| W7-03（冒烟半边） | ✅ | `packaging/pyinstaller/smoke_av.spec` + `smoke_av_main.py`：PyInstaller 真实构建 + 产物运行验证 av.libs/ 布局（正/负两向，见下文执行记录）。签名（需证书）仍留白 |
| W5-03（前置 probe） | ✅ | `scripts/probe_hdr_display_windows.py` + `results/hdr_display_probe_amd_rx6800.{md,json}`：**本机显示器具备 HDR 能力**（10bpc、Min 0.29/Max 400 nits，HDR400 级面板），当前 SDR 模式（Windows「使用 HDR」关闭）。IDXGIOutput6::GetDesc1 = vtable[27]（本机 SDK 头文件钉死 + DeviceName 交叉验证 ok）。**W5-03 不再被"无 HDR 显示器"阻挡**，开 HDR 开关即可完整验收；高亮度观感结论需注明面板等级 |

执行记录：DML 探针的包体测量首跑有 bug（`locate_file("")` 不可靠，报 0.1MB），修正为 dist-info 父目录扫描后重跑（70.9MB，与 `du` 一致）；d3d12va 探针同时澄清了 `av.codecs_available` 名字存在 ≠ 解码器可用——P0-06「探测为准」语义的新例证。

### 批次 3/4 追加（2026-09-12 同日）

| 项 | 状态 | 证据 / 产物 |
|---|---|---|
| W1-03 | 🔶 | `gpu_capability.py`：`pick_primary_hardware_adapter` / `check_adapter_match`（LUID 相等才放行，`adapter_mismatch:<render>|<decode>`）/ `decode_render_adapter_report`；GL 侧未探测时如实报 `render_adapter_unprobed`。双适配器笔记本实测仍缺（本机单 dGPU） |
| W1-05 | ✅（AMD/D3D11VA 行） | `scripts/verify_windows_hw_planar_source.py` + `results/windows_hw_planar_first_validation.{md,json}`：NV12/P010 契约与 macOS VT 一致；4:2:2 10-bit 显式拒绝；`detect_available_decoders` 实测 `[d3d11va]` |
| W2-01 | ✅（矩阵逻辑） | AV1 进 D3D11VA `HWACCEL_CODEC_SUPPORT`（NVDEC 本已含）；实际生效由 open() 首帧验证按机裁决（本机实测静默软解已归档） |
| W2-02 | ✅ | `_check_nvdec` Windows/Linux 收敛到真实硬件判定，删除 ffmpeg CLI 编译项误判源（gyan/BtbN 构建曾使 AMD 机器误选 NVDEC→静默软解）；实测 `platform_default=d3d11va` |
| W2-03 | ✅（本机校验） | `get_platform_default` 顺序实测：无 N 卡时 D3D11VA 兜底；FFmpegHwaccelSource 经 hw_planar_source 实测（W1-05） |
| W2-04 | ✅ | 诊断词表 + open() 首帧验证（批次 1 交付）；本机 4:2:2 真实拒绝消息含 `codec_unsupported_by_hw:h264/yuv422p10le` |
| W2-05 | ✅ | 基线归档指针 `results/windows_decode_baseline_ab.md` + K-W1 判定（维持 CPU planar；W3 为证据驱动后续项） |
| W3-01/02 | 🔶→可行性通过 | **原型验证（纯 ctypes，真机）**：`scripts/probe_wgl_shared_texture_roundtrip.py` + `results/wgl_shared_texture_roundtrip_amd_rx6800.{md,json}`——D3D11 BGRA8 共享纹理 → 注册/锁定/FBO 采样 **逐像素 roundtrip 成功**，干净解绑。W3 实现可以 ctypes 形态落地（不强制 C++ 扩展）；keyed mutex/解码 surface/ring/TextureLease 对接未覆盖 |
| W3-04~06 | ⬜ | 依赖 W3 立项决策 |
| W4-01 | ✅（证伪 + 修复） | **预期被证伪**：本机 AMD 驱动通告 GL 4.5/4.6 + GLSL 4.60，但 `glCreateShader(GL_COMPUTE_SHADER)` 返回 GL_INVALID_ENUM（四种上下文形态一致）；fragment 回退是现实路径。顺带修复 `_check_availability` 版本门控（版本达标也强制测试编译，P0-06 探测为准）；`tests/composite/test_compute_shader_probe_gating.py`；开放问题 3 应用侧关闭（Qt 4.5 Core 请求即所得） |
| W4-02 | ⏭ | NVIDIA 专属，按机器跳过（K-W4 前提本机不存在） |
| W4-03 | ⬜（依赖决策） | DirectML 因 GL compute 缺失而语境增强，但受开放问题 6（onnxruntime-directml 依赖）约束 |
| W5-01 | ✅（决策记录） | 第一阶段呈现保持 OpenGL/WGL；D3D swapchain（W5-02）不得在 W3 生产接入前引入——防止范围漂移 |
| W7-01 | 🔶（AMD 分册） | `results/windows_four_dim_ab_amd.md`：decode/interop/present/compute/export 五段归因集中归档；NVIDIA 组合行 K-W5 声明不写 |
| W7-03 | 🔶 | `packaging/pyinstaller/base_spec.py` 增 PyAV DLL 显式收集（av/*.dll + av.libs/ 25 个 FFmpeg 运行库，delvewheel 布局保持）；签名/安装实测仍需真机 |
| W7-04 | ✅ | 所有报告遵循 .md+.json 配对（P9C-02） |
| 转换层并行（W6-03 前置，2026-09-12） | ✅（本机） | `scripts/bench_encode_conversion_windows.py` + `results/encode_conversion_breakdown_amd.{md,json}`：分环节测量钉死「端到端地板 = 转换线程单帧串行 ~45ms（AMF 编码仅 13.1ms 已被重叠）」；帧内分带并行实测证伪（更慢 + 色度跨带 bit 不一致，负结果归档）；采纳**帧级并行转换** `convert_workers=3`（多线程整帧转换 + 按 pts 序重排编码）：4K HEVC 10bit AMF 22.6→**29.8fps**、H.264 8bit 21.2→**36.7fps**；线程扩展 3 起饱和，下一档需 GPU 直出 YUV（W6-03） |
| 播放解码波饱和遥测（2026-09-12） | ✅（本机） | `EditPlaybackFrameTrace.decode_layers` 字段（`_try_gpu_render` 解码波流数）+ `FrameTraceCollector._summarize_decode_saturation`（按轨数分桶 p50/p95/max/超预算）+ `TOPOS_PLAYBACK_DECODE_TELEMETRY=N` 门控导出（默认 0 零开销）；`tests/edit/test_decode_saturation_telemetry.py`（分桶/分位/判读语义/env 门控 8 项）。**W3 立项从此有现成取数入口**：多轨 4K 播放开遥测跑一段，decode_ms 随轨数线性恶化 + 单流不超预算 = K-W1 触发信号成立。2026-09-12 复查补漏：GPU 回退分支（队列满 hold/scrub 丢弃/CPU 回退）的 trace 同样携带 decode_layers + 波次 decode_ms——饱和分桶不再恰好丢掉最饱和的回退帧 |


### NVIDIA 真机批次（2026-09-12，机器 = RTX 4090 + Intel UHD 770 双适配器）

> 机器：NVIDIA GeForce RTX 4090（0x10DE/0x2684，24GB，驱动 610.88 / CUDA 13.3）+ iGPU Intel UHD 770，Win11 26200。
> 所有 NVIDIA 专属行自此按真机实测填写（K-W5 解锁）；产物一律 `*_nvidia_rtx4090.{md,json}` 分册。

| 项 | 状态 | 证据 / 产物 |
|---|---|---|
| W1-02（NVIDIA 行） | ✅ | `scripts/probe_wgl_interop_windows.py` + `results/wgl_interop_probe_nvidia_rtx4090.json`：**NVIDIA 实现 WGL_NV_DX_interop，`wglDXOpenDeviceNV` 打开成功（interop_open_ok）**；standalone（glcontext WGL）请求 4.6 仍只得 3.3 Core——与 AMD 机一致，确认是 glcontext 后端行为而非驱动问题 |
| W1-03 | ✅（本形态） | `scripts/probe_adapter_match_windows.py` + `results/adapter_match_probe_nvidia_rtx4090.{md,json}`：iGPU+dGPU 桌面机形态实测——匹配规则（同 LUID 放行 / 异 LUID `adapter_mismatch:<r>|<d>`）通过；**发现 DXGI 同名适配器出现两个不同 LUID 条目**（枚举顺序即主输出优先，匹配必须按 LUID 而非描述字符串裁决）；render 侧 WGL 无标准查询途径，如实 `render_adapter_unprobed`。真 Optimus 动态切换笔记本仍留复核 |
| W1-04（NVIDIA 行） | ✅ | `scripts/probe_windows_capability_matrix.py --backends d3d11va,nvdec` + `results/windows_capability_matrix_nvidia_rtx4090.{md,json}`：h264 420 8bit / hevc 420 10bit（2K/4K）**双后端全部 supported**（NVDEC 4K 探测 144-160fps，是 AMD d3d11va ~47fps 的 ~3×）；4:2:2 10bit 双后端如预期静默软解；**AV1 8bit 双后端静默软解**（4090 有 AV1 解码硬件但 PyAV 18.1.0 wheel 的 FFmpeg 构建缺 AV1 hwaccel 配置——开放问题 5 的 NVIDIA 数据点，与 AMD 机根因同源：wheel 构建层，升级路径已登记） |
| W1-05（NVIDIA 行） | ✅ | `scripts/verify_windows_hw_planar_source.py --tag nvidia_rtx4090` + `results/windows_hw_planar_first_validation_nvidia_rtx4090.{md,json}`：平台默认路由 **NVDEC**（has_nvidia_gpu=True 时 get_platform_default 顺序实测正确）；H.264→NV12 / HEVC→P010 契约在 default(NVDEC)/显式 D3D11VA/显式 NVDEC 三形态全部一致；4:2:2 10bit 显式拒绝含 `codec_unsupported_by_hw:h264/yuv422p10le` |
| W2-01（NVDEC 实测半边） | ✅ | 上两行合并收口：NVDEC 的 codec/格式能力逐项实测（见矩阵分册）；AV1 路由已有、实际生效受 PyAV wheel 限制（显式诊断路径按 open() 首帧验证兜底） |
| W4-01（NVIDIA 行） | ✅（证伪复现 + 探针修正） | `scripts/probe_gl_compute_windows.py` + `results/gl_compute_probe_nvidia_rtx4090.json`：Qt 4.5 Core 下 `glCreateShader(GL_COMPUTE_SHADER)` 返回 0 + GL_INVALID_ENUM——**与本机原生 WGL 4.6/4.5/4.3 Core 上下文（GLSL 4.60 NVIDIA）逐版本交叉验证一致**（`compute_unavailable_native_wgl_confirmed`），亦与 AMD 真机现象一致。顺带修正探针两处盲点：COMPUTE_SRC 前导换行（#version 不在首行）、create 失败与 compile 失败不可区分（无 handle/GetError 检查） |
| W5-03（NVIDIA 行 probe） | ✅ | `results/hdr_display_probe_nvidia_rtx4090.{md,json}`：本机显示器 **8bpc SDR**（无 HDR 能力）——IDXGIOutput6 走查层在 NVIDIA 真机通过；HDR 呈现裁决在本机仍被显示器阻挡（与 AMD 分册 HDR400 面板互补：两机覆盖有/无 HDR 两种前置） |

### NVIDIA 真机批次 2+3（2026-09-12 同日：解码基线 + NVENC 收口）

| 项 | 状态 | 证据 / 产物 |
|---|---|---|
| W2-05（NVIDIA 行） | ✅ | `scripts/bench_hw_decode_windows.py --tag nvidia_rtx4090 --hw-backend nvdec` + `scripts/bench_hw_decode_results_nvidia_rtx4090.json`：4K H.264 CPU planar **945 fps** vs NVDEC 163 fps（NV12）；4K HEVC 10bit CPU **320 fps** vs NVDEC 196 fps（P010）——双双远超 25fps 预算；NVDEC 下载速度是 AMD 机 d3d11va（~47fps）的 ~3-4×。bench 脚本顺带 vendor 化（GPU 名动态探测 + `--hw-backend`/`--tag` 分册，修 `dict.get` 默认值总是求值的 device 映射 bug） |
| W6-01（NVENC 接入） | ✅（收口） | **vendor 泛化**（`video_encoder.py`）：`detect_hw_vendor()`（DXGI，nvidia→NVENC / amd→AMF，auto 路由与解码侧同一真相源）+ `nvenc_input_pix_fmt()`（yuv420p 直收 h264/hevc/av1；hevc/av1 10bit→p010le 转换路线与 AMF 同构；h264 10bit/4:2:2 拒绝）+ `_build_nvenc_options`（x264 preset→p4/p5/p6、crf→rc=vbr+cq、码率→b/maxrate/bufsize；初版误写 `b:v`，复查批次 2 修正）+ `hw_fallback_reason`/`hw_encoder_name` 属性 vendor 感知。**端到端实测**（`verify_hw_encode_export.py` vendor 化后全过）：4K H.264 NVENC **32.9fps** vs x264 18.7（1.8×）；4K HEVC 10bit NVENC **30.2fps** vs x265 4.0（**7.6×**）；4K AV1 10bit NVENC 29.4fps；kill switch/显式 nvenc/HDR 锁 CPU/结构化回退全部验证。**探针归档**（`scripts/probe_nvenc_encode_windows.py` + `results/windows_nvenc_encode_probe_nvidia.{md,json}`）：h264/hevc/av1_nvenc 8bit + hevc/av1 10bit 全部「open+编码+回解」通过；PSNR 对照只证码流 sane（合成素材码率不跑满），同码率画质 A/B（真实素材）留正式验收 |
| 附带实证 | ✅ | 本机 `h264_amf` 注册存在但 NVIDIA 驱动 open 失败（Errno 1313558101）——「注册存在 ≠ 驱动可用」（P0-06）的跨 vendor 真机实证；显式点错通道由探针兜底回退 CPU |
| 修复 | ✅ | `_inject_hdr_static_metadata`：SDR 不再写**空 `x265-params`**（对 x265 是噪声、对 NVENC 是非法选项）；硬编通道整体跳过 x265-params 注入（HDR 不承载语义与 open() 告警一致）。crf=0 → NVENC cq=0 EINVAL → 选项级失败重建 CPU 语义在 N 卡上同样成立（回归测试钉死） |
| 测试 | ✅ | 新增 `tests/export/test_nvenc_vendor_routing.py`（15 项，vendor 注入式——不依赖真机）；既有 AMF 硬编码断言 vendor 化；91 项编码器测试全过（本机 NVIDIA 真机语义下） |

### NVIDIA 真机批次 5+6（2026-09-12 同日：四维报告 + 三项决策 + 收尾）

| 项 | 状态 | 证据 / 产物 |
|---|---|---|
| W7-01（NVIDIA 分册） | ✅ | `results/windows_four_dim_ab_nvidia.md`：decode/interop/present/compute/export 五段归因集中归档（NVDEC 基线、interop_open_ok、GL present 维持、GL compute 不可用三重交叉、NVENC 7.6×） |
| W3 立项决策（D1） | ✅（裁决） | **W3 本轮不启动，保留为证据驱动的后续项**。K-W1：双机 download 基线均足额（NVIDIA 4K CPU 12.8~38×、NVDEC 7.8×；AMD 同判）——单流预算不构成 zero-copy 必要性；K-W2 不否决（双 vendor interop_open_ok + roundtrip 原型）。重启触发：多轨 4K `TOPOS_PLAYBACK_DECODE_TELEMETRY=N` 遥测显示 decode_ms 随轨数恶化且单流不超预算 |
| W4-02 CUDA compute（D3） | ✅（不立项） | K-W4 前提不成立：P4-05 教训（无纹理驻留 staging 慢 fragment 两个数量级）+ W3 CUDA interop 未落地 → 无驻留路径不立项；W3 重启后再评 |
| W4-03 依赖决策（D2 / 开放问题 6 关闭） | ✅（裁决） | **onnxruntime-directml 不进默认依赖**，登记 optional extras（`ai-dml`），触发条件 = 真实 ONNX 模型需求（AI 降噪立项）。证据：DML EP 开箱即用但小模型与 CPU 打平、+70.9MB、MIT；无需求引入违反 K-W4 语义 |
| W5-04（SDR 一致性，可执行半边） | ✅（本机） | `tests/color/` 套件在本机 NVIDIA 环境全过（SDR 色彩链路一致性回归通过；详见执行记录）。W5-04 的显示器端验收（跨机画质门禁）仍归 W5 整体收口 |
| W5-02 | ⏭（评估延后） | D3D flip-model swapchain 评估维持延后——W5-01 决策（W3 生产接入前不引入 D3D present）仍有效；评估报告待 present 管线调研立项后补 |
| W7-03（签名半边） | ⏭ | PyAV DLL 收集 + smoke 构建已交付（前批）；signtool 签名/安装实测需代码签名证书，登记为阻塞项 |

### NVIDIA 真机复查批次 2（2026-09-12：批次 1-6 全量改动隐藏 BUG 审查）

> 审查范围 = 35e79363 / ee727262 / e38f1688 三个批次的全部 diff（生产代码 + 脚本 + 测试）。
> 三处确认 BUG 全部真机实证后修复；「静默错误输出」零报错特征与 P0-06（禁静默降级）同类。

| 发现 | 级别 | 根因 / 实证 / 修复 |
|---|---|---|
| `b:v` 选项键被静默忽略 | Critical | `_build_nvenc_options`（W6-01 新增）与 `_build_amf_options`（W6-02 既有）、DNxHR 无 profile 回退（更早既有）三处都写 `b:v`——这是 ffmpeg **CLI 流指定符语法**，经 PyAV `stream.options` → `avcodec_open2` 消费的是 libavcodec AVOption 正名 `b`，`b:v` 落进 av_dict 后**静默忽略**。真机实证（RTX 4090，hevc_nvenc+噪声素材）：`b:v=20000k` 输出与不设码率对照组**逐字节一致**（13 Mbps 默认漂移），`b=20000k` → 31 Mbps 生效。此前所有 e2e 验证都走 crf 模式，码率路径从未被真机触达——验证盲区即漏网原因。修复：三处 `b:v`→`b`；`verify_hw_encode_export.py` 新增码率模式臂（16Mbps 目标 → 实测 20.3 Mbps 在带宽内，断言钉住回归） |
| av1 CPU 码率走 `x265-params`（无效选项） | Medium | 批次 2+3 把 av1 纳入 GOP 配置块后，CPU 码率路径落入 `_build_rate_control_options` 的 x26x else 分支——libsvtav1 不认 `x265-params`（真机实证：SVT 配置打印 BRC 停留默认 CRF/35，选项被忽略=静默无码率控制）。修复：av1 专属分支输出标准 AVOption（`b`/`maxrate`/`bufsize`）；真机实证 `b=8000k` → SVT 配置 `VBR/8000`，生产路径 e2e 13.4 Mbps（8M 目标，带宽内） |
| av1 CPU 编码器名指向未注册的 libaom-av1 | Critical（既有暴露） | `ENCODER_NAME_MAP["av1"]="libaom-av1"`，但 PyAV 18.1.0 wheel **不带 libaom-av1**（`codecs_available` False，只带 libsvtav1）——av1 CPU 路径（kill switch、无 N/A 卡机器、硬编探针回退）在 `add_stream` 即失败。批次 2+3 的 av1 硬编可达性提升使该断点可被 kill switch e2e 触达。修复：`cpu_av1_encoder_name()` 注册探测解析（libsvtav1 优先，libaom 兜底老 wheel）；kill switch e2e 真机通过（libsvtav1 engaged，VBR 目标模式生效） |
| 其他复查结论（未改） | — | ① `detect_hw_vendor()` 依赖 `primary_vendor`=adapters[0]（「主输出优先」枚举序）：Intel iGPU 主显 + N/A 副显的 Optimus 布局 auto 会保守回退 CPU——与解码侧同一真相源、跨适配器统一裁决归 W1-03 真 Optimus 复核，已写入 docstring 边界说明，不单边改编码路由；② `_using_amf` 无外部消费者（纯内部残留，无害）；③ open() 选项级重建路径 `_hw_fallback_reason` 保留、`_hw_vendor` 正确清零、HDR 元数据在重建后正确注入 x265-params；④ bench/verify/probe 脚本无恙 |
| 测试 | ✅ | 新增 `TestRateControlOptionKeys`（5 项：nvenc/amf/av1 码率键名 + crf→cq + hevc x265-params 不回归）与 `TestAv1CpuEncoderName`（2 项：svtav1 优先/libaom 兜底）；清理 `test_kill_switch` 残留参数 |

### 仍未完成（全部为有明确触发条件的后续项）

- **W3-01~06**（zero-copy 生产实现 + fence + A/B）：按 D1 裁决挂起——重启触发 = 多轨 4K `TOPOS_PLAYBACK_DECODE_TELEMETRY=N` 遥测显示 decode_ms 随轨数恶化且单流不超预算（取数入口已就位）
- **W6-03**（全 GPU 转码链路）：依赖 W3 立项（GPU 直出 YUV 是转换层 3 线程饱和后的下一档）
- **W6-01 正式验收残项**：真实素材同码率画质 A/B（NVENC vs x264/x265，合成素材只证码流 sane）
- **W5-03**（HDR 呈现裁决 scRGB vs HDR10）：受显示器限制——AMD 机 HDR400 面板开 HDR 开关即可实测（最低成本路径）；4090 机 8bpc SDR 不可用
- **W5-02**（D3D flip-model swapchain 评估）/ **W5-04 完整收口**：W3 之后按序（W5-01 决策约束）
- **W7-03**（签名/安装实测）：需代码签名证书（阻塞项）
- **W1-03**：真 Optimus 动态切换笔记本复核（iGPU+dGPU 桌面形态已过）
- **AV1 硬解**：PyAV wheel 升级路径（自编 FFmpeg / 等 wheel；开放问题 5）
- **K-W5**：Intel 独显/QSV 行与 AMD AMF 行的画质门禁级验收仍无对应真机记录，文档一律不写「已验证」
