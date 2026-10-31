# Metal 与跨平台 GPU 后端渐进迁移优化计划

> 状态：提案 v2（架构保留；排序、基线、工作量已按实测数据修订）
> 编制日期：2026-09-11
> 修订日期：2026-09-11（v2）
> 适用项目：Topos Color（PyQt6 + ModernGL + GLSL + VideoToolbox + native codec）
> 核心目标：在完整保留 OpenGL/ModernGL 的前提下，逐步接入 Metal，并为未来 Windows 的 NVIDIA、Intel、AMD GPU 优化保留稳定扩展点。
> 审计基线：commit `a3064858`（分支 `bugfix`）；行号基于该时点，后续改动会漂移，以符号名为准。
> 工作量标记：S ≤0.5 天｜M 1~2 天｜L ≥3 天

---

## 修订说明（v1 → v2）

v1 的架构判断成立并全部保留。以下问题在 v2 中修复，每条都附实测或代码证据。

| # | v1 的问题 | v2 的处理 | 证据 |
|---|---|---|---|
| 1 | 未声明目标硬件平台 | 新增 §0.1/§0.2，并列为**阻塞性前置决策** | 开发机实测为 Intel 末代平台，见 §0.1 |
| 2 | 收益排序与实测帧预算相反 | 重排：Metal compute 能力解锁提前为第一收益，视频零拷贝后移并条件化 | 见 §0.3 帧预算表 |
| 3 | Phase 0 约七成是重新提议已有设施 | Phase 0 改为「复用 + 补维」，只补真正缺的 | `tests/benchmark/`、`tools/benchmarks/playback_benchmark.py` |
| 4 | 在 probe 之前就冻结后端契约 | probe 前移为 Phase 1，契约冻结后移为 Phase 2 | 前作 `docs/4k-playback-optimization.md:413` |
| 5 | §2.1 清点遗漏大量既有模块 | §2.1 重写，并新增 §2.2 既有抽象清单 | 见 §2.1/§2.2 |
| 6 | 完全未提 GL 上下文共享组 | 新增 §2.3，并独立为 Phase 10 | `src/main.py:200`，227 处 `.glo` |
| 7 | Phase 5/6 无工作项编号、无工作量估算 | Phase 6/7 补全编号、主要文件、交付物、验收标准与工作量 | 见 §5 Phase 6/7 |
| 8 | v1 §6.1/§6.2 重复造轮子 | 改为扩展既有 `EffectSpec` / `EffectCapability` | `src/features/shared/effects/` |
| 9 | v1 §6.3 与 §4.1 把四维重新压回单一枚举 | 拆为四个独立 flag | 与 §1.2 自洽 |
| 10 | v1 §10 与 §11 第 8 条顺序矛盾 | Phase 8/9 拆分，验收条目改为「接口存在」而非「实现完成」 | 见 §11 |
| 11 | 无中止条件 | 新增 §1.4 kill criteria | — |
| 12 | 无打包/签名/依赖/CI 章节 | 新增 §9 | `packaging/pyinstaller/base_spec.py` |
| 13 | §12 残留 OpenCL 引用 | §14 删除并补 MSL/D3D/Qt RHI 参考 | — |
| 14 | P0-04 引用不存在的文件路径 | 修正为 `tests/benchmark/benchmark_yuv_gpu_pipeline.py` | 见 §5 Phase 0 |
| 15 | 帧预算表未声明实测渲染档位，易被误读为 4K | §0.3 标明为 1920×1080 proxy 档，并补 4K 档缺失结论 | 归档 `sizes` 三档实测 |
| 16 | 「12 个 compute shader」实为 `local_size_x` 出现次数 | §2.4 精确清点：10 个常量 / 7 个已注册 / 3 个零引用 / 2 个探测桩 | `shaders.py:3527-4479` |
| 17 | 未发现 macOS 上 compute 能力的**静默**缺口 | 新增 §0.4，并把 P4-03 改为 Normalize 百分位链路（最高价值项） | `histogram_manager.py:240-248,431-432` |
| 18 | 由 §0.3 的 0.2 ms 直接推出「调色迁移收益上限 0.2ms」——**错误推广** | 新增 §0.3.1：实测 4K 单层全工具调色 +6.91ms（1080p 播放路径的 30 倍）；Phase 6 立项依据改为"待测的同负载 A/B" | `tools/bench_color_tools.py` 实测 |
| 19 | 「macOS 15 是末代 Intel」有误 | 勘误为 macOS 26（Tahoe） | 见 §0.1 |

---

## 0. 目标硬件与收益前提（阻塞性前置）

### 0.1 目标硬件与实测基线

#### 目标硬件实测（本开发机）

```text
机型      MacPro7,1（黑苹果）
CPU       Intel Core i9-9900K @ 3.60GHz
OS        macOS 15.3.1 (24D70) / x86_64
GPU       AMD Radeon RX 6900 XT（Metal 3，16GB）
显示      3840x2160 @ 60Hz

OpenGL 实测  4.1 ATI-6.1.13   ← macOS 上限即为 4.1
              renderer = "AMD Radeon RX 6900 XT OpenGL Engine"
              version_code = 410
Metal        可用（MTLCreateSystemDefaultDevice → AMD Radeon RX 6900 XT）
```

**macOS 26（Tahoe）是最后一个支持 Intel 的系统版本；macOS 27 起仅支持 Apple Silicon。** 本机在 15.3.1，尚有一次升级余量，此后 Metal 驱动行为永久冻结，Xcode/Metal 工具链也不再前进。一台支持窗口已关闭的机器不构成长期基准，因此 §0.2 必须先决策。

> 勘误：早期稿写作「macOS 15 是最后一个支持 Intel 的版本」，低估了一代。

#### VideoToolbox 硬解实测（项目自带探针）

用 `src/shared/media/macos_vt/hardware_capability.py` 的 `vt_hardware_decode_available()` 实测：

| 素材 | 结果 |
|---|---|
| H.264 8-bit | `True`（有硬解） |
| HEVC 10-bit | `True`（有硬解） |
| **ProRes 10-bit** | **`False`（无硬解，VT 只能软解）** |

该项目自己的模块文档早已记录此类风险（`hardware_capability.py:6-9`，模块 docstring 全长 `:1-35`）：

> 10-bit HEVC on Intel integrated graphics has no fixed-function decoder, so a "working" VT session decodes 4K at ~42ms/frame where FFmpeg frame-threading manages ~4ms.

且项目已有路由策略 `playback_should_prefer_ffmpeg()`（`:286`）：**当 VT 无硬解时，顺序播放主动改走 FFmpeg planar 路径，而不是 VT 零拷贝路径。**

---

### 0.2 目标平台决策（需先回答，否则后续排序无依据）

三选一，或明确组合与优先级。**在此项确定前，Phase 4 之后不启动。**

| 选项 | 含义 | 代价 |
|---|---|---|
| A. Apple Silicon 为主 | 目标 M 系列；Intel/黑苹果降级为「尽力而为」 | 需要一台 M 系列基准机，否则所有性能结论都不可迁移 |
| B. 继续 Intel/黑苹果 | 基准机保持现状 | 只能升到 macOS 26；此后无系统更新，Metal/驱动行为随系统终止而冻结 |
| **A′. 组合（推荐）** | **Apple Silicon 作主目标机；本机保留为 AMD + 「无 ProRes 硬解」降级路径的回归机** | 需购机；但本机并非废置——它覆盖 Apple Silicon 永远测不到的降级分支 |
| C. Windows/NVIDIA 为长期主目标 | macOS 仅维持现状，投入转向 Phase 8/9 | Metal 投入的优先级需大幅下调；可能只做 compute 解锁 |

**决策项 D（与 A/B/C 独立）**：Metal 的立项理由是「能力解锁」还是「性能加速」？
- 若是**能力解锁**（推荐）：Phase 4 是最短路径，见 §0.3 与 §0.4。
- 若是**性能加速**：必须先接受 §0.3 的实测结论——当前帧预算里 GPU 不是瓶颈。

> **决策记录（2026-09-11，用户指示继续完成计划全部剩余项时按推荐默认采纳）**：
> - §0.2 选 **A′**（Apple Silicon 主目标机 + 本 Intel/AMD 机保留为「无 ProRes 硬解」降级路径回归机）。**代码架构在 A/B/A′ 下完全一致**，A′ 只影响性能基线在哪台机器上测：凡涉及端到端性能结论的项（Phase 5 门禁、P6 A/B），在 Apple Silicon 机器到位后需复测一次；本机测得的结果一律标注「Intel + AMD 6900XT 降级路径」。
> - 决策项 D = **能力解锁**（Phase 4 已按此完成并产出实测门禁）。
> - 诚实性备注：Apple Silicon 基准机尚未到位，A′ 的「主力机」半边暂缺——期间所有性能门禁在本机执行，只作不退化判定，不作绝对性能结论。

---

### 0.3 收益前提的实测校验（决定排序）

`tests/media/results/prores_yuv_gpu_r16ui_*.json` 四个归档。**输入素材是 4K ProRes `yuv444p12le`（3840×2160），但归档实测的渲染目标是 proxy 尺寸**——每个归档含 `1920x1080`/`1280x720`/`960x540` 三档，全部 `frame_path = r16ui_direct`。**下表取 `1920x1080` 档，v1/v2 早期稿未声明这一点，属于会误导排序的省略。**

| 阶段（1920×1080 档） | p95 区间 | 占帧预算 |
|---|---:|---:|
| `decode_ms` | **20.6 – 24.8 ms** | **~82–84%** |
| `texture_write_ms` | 3.6 – 6.0 ms | ~15–18% |
| `plane_prepare_ms` | **0.0008 – 0.0015 ms** | ~0% |
| **`render_ms`（调色 shader）** | **0.16 – 0.33 ms** | **<1%** |
| `total_ms` | 25.1 – 28.2 ms | — |
| `effective_fps` | 42.9 – 44.9 | — |

**关键观察：`decode_ms` 与输出尺寸无关。** 三档的 `decode_ms` 均值分别为 1080p ≈19.2ms、720p ≈18.4ms、540p ≈17.0ms——因为解码的是同一个 4K 源。而 `texture_write_ms` 随尺寸线性下降（1080p ≈3.8ms → 540p ≈0.8ms）。**结论：即使在 540p 的小窗口下，4K ProRes 的 CPU 解码仍占绝对多数。**

四条硬结论：

1. **`plane_prepare` 瓶颈已解决**（历史值为几十毫秒级，现为 ~0.001ms）——v1 §2.3 引用的历史结论已过时，R16UI 直传路径已落地。
2. **调色 shader 在播放路径上只占帧预算不到 1%（0.2ms）——但这个数字不能外推。** 见下方 §0.3.1：真正的调色工作在 4K 上是 **6.9 ms**，是这里的 30 倍。Phase 6 **不应仅凭本表被降级**。

   > 勘误（对本文档自身）：早期稿由 §0.3 这一行直接得出「对 shader 做后端迁移收益上限就是 0.2ms」，**这是错误的推广**——那条数据来自 1080p、近似恒等调色的播放基准，与"用户在 4K 上做多层调色/合成"不是同一负载。§0.3.1 是修正后的结论。
3. **占 ~82% 的 CPU ProRes 解码，是 Metal / CUDA / D3D / CVMetalTextureCache 全部碰不到的**——没有硬件解码器就没有零拷贝的源头（§0.1 实测 ProRes 无 VT 硬解）。
4. **4K 全分辨率的端到端数据目前不存在**（归档只有 proxy 档）。因此本计划只能冻结"不退化"门禁，**不能**据此承诺 4K 目标的绝对帧时；补测见 P0-04/§13 第 8 条。

**唯一被数据支持的 Metal 收益是能力解锁**：macOS OpenGL 4.1 无 compute shader，而项目已内置 compute→fragment 自动回退（`src/features/composite/render/compute_shader_manager.py:6,9`），回退时日志原文承认「Large-radius effects will use fragment shader fallback (**slower**)」（`:180`）。**即：今天大半径效果就在跑被自己标注为"更慢"的路径。** 这是已存在、已用户可见的性能缺口，且回退通知机制（`_fallback_callback`）现成可复用。

---

### 0.3.1 调色负载的真实成本（新增，实测 2026-09-11）

§0.3 测的是**播放路径 + 近似恒等调色**。调色/合成的真实成本需另测，用项目自带的 `tools/bench_color_tools.py`（真实素材、真实 `AsyncRenderer` GPU 管线、`video_render_sync_mode='finish'` 强制计入 GPU）：

| | 2K 1920×1080 | 4K 3840×2160 | 4K/2K 比 |
|---|---:|---:|---:|
| 解码上限（单独报告） | 227.5 fps（4.4 ms） | **55.3 fps（18.1 ms）** | — |
| **baseline**（恒等调色，= 上传+present+sync 地板） | 1.74 ms | **5.46 ms** | 3.1× |
| `combo_full`（单层全工具） | 3.85 ms | **12.37 ms** | 3.2× |
| **→ 调色本身（Δ vs baseline）** | **+2.11 ms** | **+6.91 ms** | 3.3× |

最贵的 4K 场景（Δ vs baseline）：

| 场景 | 4K ms/f | Δ | 性质 |
|---|---:|---:|---|
| `combo_full` 单层全工具 | 12.37 | +6.91 | 综合 |
| `usm_heavy` USM mode1 r=5 iters=4 | 10.87 | +5.41 | 多次采样 |
| `plugin_skin` DCTL 双边滤波 64 采样 | 9.55 | +4.09 | 大采样数 fragment |
| `qualifier_fuzz` 限定器+羽化+去噪 | 8.39 | +2.93 | 动态循环 |
| `blur_optical` 光学虚化 bokeh r=10 | 7.59 | +2.13 | 大半径 |
| `layers_3` 三层 | 7.01 | +1.54 | 多图层 |

**修正后的四条结论：**

1. **调色成本随像素数近似线性**（Δ 的 4K/2K 比 = 3.3×，接近 4×），**且不可忽略**：4K 单层全工具调色 6.91 ms，相当于解码 18.1 ms 的 **38%**。§0.3 的「<1%」不可外推。
2. **但最大单项是 5.46 ms 的"地板"（上传+present+sync），不是 shader 本身。** 地板与调色 delta 大致相当，二者需分别归因：
   - 地板 = 上传/呈现/同步开销。**Metal 只对 present 段有把握，上传段要靠零拷贝（Phase 5）**。
   - 调色 delta = 纯 fragment GPU 工作，跑在 macOS 已弃用的 OpenGL 兼容层上。**Metal 是否更快、快多少，本文档没有数据——这正是 P1-05。**
3. **加速比的下限已被数据锁定：** 4K 解码 18.1 ms 是硬地板，即使调色降到 0，4K 播放也只能到 55 fps。**所以调色后端迁移的收益应计在"交互延迟 / 多层 / 导出"，不是播放帧率。**
4. **行为不变的是能力缺口**：`blur_optical`/`usm_heavy`/`plugin_skin`/`qualifier_fuzz` 正是 compute + 共享内存最擅长的负载，而 macOS GL 4.1 **完全没有 compute**。**这一项的收益不需要等 P1-05 就成立**（见 §0.4）。

**尚未测量、且决定 Phase 6 是否值得的空白**：

| 空白 | 影响 |
|---|---|
| 同一 shader 在 Metal 与 macOS OpenGL 上的 A/B | **决定性问题**；P1-05。若 <15% 则 Phase 6 仅剩架构理由（K1） |
| Composite 页（节点图）的成本结构 | 本表是 **Color 页 `AsyncRenderer`**；Composite 走 `node_engine` + `compute_shader_manager`，**无对等基准数据**。而 §0.4 的能力缺口正落在这里 |
| 4K **导出**（非预览） | 导出无交互约束，调色占比更高 |

> **不要用本表宣布"调色不重要"。** 它宣布的是：调色成本真实（4K ~7ms），但**尚未测量的 Metal/OpenGL 同负载 A/B 才是 Phase 6 的立项依据**。

---

### 0.4 macOS 上真实的 compute 能力缺口（新增，Phase 4 的直接立项依据）

承接 §0.3 的「能力解锁」结论。清点后发现缺口**不止于"更慢"**，还有一处是**静默失效**：

| 依赖 compute 的能力 | GL 4.3+ 行为 | **macOS GL 4.1 行为** | 是否静默 |
|---|---|---|---|
| 大半径模糊/腐蚀膨胀 | MSL/GLSL compute（共享内存加速） | 回退到 fragment shader，**自认为"更慢"** | 有日志（`:180`，每 capability key 一次） |
| **Normalize 百分位归一化** | compute + SSBO 原子直方图 | **`compute_histogram()` 返回全零直方图**（`histogram_manager.py:240-248`） | **完全静默**：`find_percentile_values()` 对全零 CDF 返回 `(0.0, 1.0)`（`:431-432`），等价于"不做归一化" |

即：**macOS 用户打开 Normalize 的 percentile 模式，得到的是一个与"未启用"无法区分的空操作，且没有任何提示。** 这违反 `.clinerules:11`（能力不足必须返回明确诊断，不得静默降级），也说明「Metal compute 能力解锁」不是锦上添花。

补充事实（修正 v1 的规模估计）：直方图链路**在 GL 4.3+ 上也是半 GPU 的**——GPU 只算直方图（`_HISTOGRAM_COMPUTE` + SSBO `atomicAdd`），**CDF 与百分位仍在 CPU 计算**（`histogram_manager.py:10-11`、`:296`）。因此 P4-03 的目标应写为「把 CDF/百分位也搬到 GPU，并让 macOS 不再返回空直方图」，而不是"新增 atomics 直方图"。

另外，`src/shared/gpu/gpu_scope_engine.py`（27KB）中**完全没有** `compute_shader`/`local_size`/SSBO/`atomicAdd`——scope 显示与本节直方图是两条不同链路，不要混为一谈。

---

### 0.5 本机 Metal 能力实测（已完成，2026-09-11）

为回答 §0.2「能否在本机继续 Metal 开发」，已在本开发机跑通完整 probe（Python + PyObjC/ctypes，无工程改动）：

| 能力 | 结果 |
|---|---|
| `MTLCreateSystemDefaultDevice` | ✅ `AMD Radeon RX 6900 XT` |
| Metal 3 / `MTLGPUFamilyMac2` / `Common3` | ✅（`Apple7` ❌，符合预期） |
| `hasUnifiedMemory` | ❌ False（独显，PCIe） |
| `maxBufferLength` | 3.8 GB |
| **`argumentBuffersSupport`** | **1（Tier 1）** —— Apple Silicon 为 Tier 2，**见下方影响** |
| MSL `newLibraryWithSource` 编译 | ✅ |
| compute pipeline + dispatch 正确性 | ✅ `[1,2,3,4] → [2,3,4,5]` |
| `CVMetalTextureCacheCreate`（ctypes） | ✅ rc=0 |
| NV12 → `MTLTexture` | ✅ 1920×1080 `R8Unorm`，IOSurface-backed，`storageMode=1`(Managed) |
| **P010 10-bit → `MTLTexture`** | ✅ 1920×1080 `R16Unorm`，IOSurface-backed |
| BGRA → `MTLTexture` | ✅ 1920×1080 `BGRA8Unorm` |
| `CAMetalLayer` + `wantsExtendedDynamicRangeContent` | ✅ |
| 工具链 | Xcode 16.4 + `metal` 编译器（`air64-apple-darwin24.3.0`） |

**结论一：P1-01 / P1-02 在本机可以完成，Phase 0–4 无障碍。** 本机**不需要**为"能否做 Metal 开发"而换机。

**结论二（P1-04 已知陷阱已被实证）**：同一调用走 PyObjC 返回 **`-6661`（`kCVReturnInvalidArgument`）**，走 **ctypes 返回 0 并产出有效纹理**。§5 Phase 1 P1-04 的「沿 ctypes 层声明 CoreVideo 函数、不在回调边界混用两套 FFI」决策由此**从推测升级为实测结论**。

> **踩坑记录（务必写进 probe 代码）**：`-6661` 是非零整数，在 Python 里**为真**。用 `if tex:` 判断成功会把失败当成功、并继续跑出看似正常的输出。所有 CVReturn 必须显式 `== 0` 判断。本次首轮 probe 即因此误报过"NV12/P010 OK"。

**结论三（新发现的架构约束，v1/v2 均未预见）**：本机 argument buffer 仅 **Tier 1**（无嵌套 argument buffer、资源上限低）。P7-02「用 argument buffer 替代 129 处 f-string uniform 注入」若按 Tier 1 设计，迁到 Apple Silicon 需重新设计。**该工作项应以 Tier 2 为目标设计、在 Tier 1 上降级验证。**

---

## 1. 决策摘要

### 1.1 总体决策

采用"共享上层语义 + 多个专属后端 + 能力探测 + 会话级切换"的架构：

```text
UI / Project / Color Model / RenderCommand / Frame Metadata
                              │
                    Renderer Orchestrator
                              │
       ┌──────────────────────┼──────────────────────┐
       │                      │                      │
 OpenGL Backend          Metal Backend        DirectX/CUDA Backend
 ModernGL + GLSL         MTLDevice + MSL      D3D11/12 + CUDA/NVDEC
       │                      │                      │
       └─────────────── GPU Frame Contract ──────────┘
                              │
                    presentation / export
```

OpenGL/ModernGL 永久保留，承担以下职责：

1. 当前完整功能实现和跨平台基线。
2. Metal 功能未覆盖时的回退。
3. OpenGL 与 Metal 的画质一致性参考实现。
4. Windows/Linux 的现有渲染后端。

Metal 不是对现有 OpenGL 代码的"一次性替换"，而是逐步实现同一组渲染语义。

**范围量级（实测，用于工作量判断）**：`src/` 共 1,397 个 `.py` / 775,879 行，其中 **56 个文件 / 82,633 行（10.7%）触碰 OpenGL/ModernGL**。这是本次迁移的真实爆炸半径。

### 1.2 必须区分的四类接口

CUDA、Metal、OpenGL、D3D 不能全部塞进一个 `backend` 枚举。拆为四个相互独立的能力维度：

| 维度 | 典型实现 | 职责 | 已有先例 |
|---|---|---|---|
| GraphicsBackend | OpenGL、Metal、D3D11、D3D12、Vulkan | 纹理、FBO/RenderTarget、光栅渲染、显示 | `src/shared/gpu/gl_backend.py` |
| ComputeBackend | GLSL fragment、Metal Compute、CUDA、DirectML、CPU | 降噪、Scope、直方图、缩放、图像计算 | `compute_shader_manager.py`（含回退） |
| DecodeBackend | CPU、VideoToolbox、NVDEC、D3D11VA、QSV/oneVPL、AMF | 压缩视频解码 | **`DecoderBackend` ABC，见 §2.2** |
| InteropBackend | IOSurface、CVMetalTextureCache、D3D Shared Handle、CUDA Interop | 解码资源与 GPU 渲染资源之间的共享 | `iosurface_gl_bridge.py`（GL 侧） |

例如未来 NVIDIA Windows 链路可能是：

```text
graphics_api = d3d12
compute_api  = cuda
decode_api   = nvdec
interop_api  = cuda_d3d12
```

CUDA 是 NVIDIA 计算/视频处理生态，不应被当作完整的窗口图形后端。

### 1.3 与既有计划的关系（v1 缺失）

本计划**不取代**以下文档，而是接续其未完成部分：

| 文档 | 关系 |
|---|---|
| `docs/4k-playback-optimization.md` | **上游**。其 §9 优先级表的 P0（benchmark 字段）由本计划 Phase 0 承接；其 §10 最终验收标准仍然有效。其 `:413` 的「probe 先行」顺序被本计划 Phase 1/2 明确采纳；`:614` 的「Metal 是长期目标，当前不应作为第一步」由本计划 §0.2 决策后解除。 |
| `docs/system_check_plan_2026-09-04.md` | **当前有效计划指针**（见 `docs/plans/archive/README.md:5`）。其 ARCH-2（DecoderBackend ABC）已完成，见 §2.2。 |
| `docs/professional_nle_realtime_playback_optimization_plan.md`、`docs/optimization_plan.md` | 并行计划，本计划不修改其范围；若冲突以本计划的四维接口为准并记录到 §9.4 ADR。 |
| `docs/windows_optimization_plan_2026-09-11.md` | **下游子计划（2026-09-11 新增）**：本计划 Phase 8/9 的展开与执行细化——W0~W7 阶段表、「本机可做 vs 需真机」边界标注、reason 枚举级回退表（§8.2 展开）、K-W 系列门槛、与 P8/P9 条目的映射表。条目冲突时以本计划的四维接口与回退语义为准。 |
| `docs/plans/archive/**` | 仅历史参考，行号与结论可能过时，不作为依据。 |

**§0.4 的 Normalize 百分位静默缺口不在上述任何文档中**——它是本次清点新发现的，未见于 `docs/4k-playback-optimization.md` 或 `system_check_plan_2026-09-04.md`。因此它不"接续"任何既有计划，是独立的新工作项。

### 1.4 中止条件（v1 缺失）

本计划**不是无条件推进**的。以下任一条件成立即**停止 Metal 主线，保留 OpenGL**，并把结论写入 ADR：

| 编号 | 中止条件 | 检查点 |
|---|---|---|
| K1 | §0.2 决策项 D 若选择「性能加速」，而 Phase 1 probe 显示 Metal render-only 加速 < 15% | Phase 1 |
| K2 | Phase 5 门禁未达：Metal 端到端 p95 相比 OpenGL 基线无改善（±5% 以内） | Phase 5 |
| K3 | Phase 10 的 Qt 表面共存方案需要改动 `QOpenGLWidget` 语义，导致 5 个既有 viewport 类均需分叉 | Phase 1 probe 后即可预判 |
| K4 | Metal 原生扩展的打包/签名/公证在目标分发渠道不可行 | Phase 1 |
| K5 | Metal compute 解锁在 Phase 4 未能覆盖 `compute_shader_manager.py` 的 **2 个效果入口**与 `histogram_manager.py` 的 Normalize 链路（§0.4），即收益不落地 | Phase 4 |

---

## 2. 当前项目基线

### 2.1 已核实的 GPU 结构（重写并扩充 v1 §2.1）

当前 checkout **未发现** OpenCL kernel、`pyopencl`、OpenCL context/queue 或 `.cl` 文件。主渲染链路实际是 OpenGL/ModernGL/GLSL。

v1 的 8 条引用**全部准确**，保留；下表补充 v1 遗漏但对决策有影响的模块。

| 当前事实 | 文件证据 | 对迁移的影响 |
|---|---|---|
| 启动时强制 Qt Quick 使用 OpenGL | `src/main.py:170-171` | 早期 Metal 阶段不要修改全局 Qt 图形路径 |
| **全局开启 GL 上下文共享** | **`src/main.py:200`** | **见 §2.3，最大 UI 层阻塞** |
| 全局 QSurfaceFormat 4.5 Core | `src/main.py:173-197` | macOS 实际只给 4.1，见 §0.1 |
| 依赖 ModernGL 和 PyOpenGL | `pyproject.toml:10-20`、`requirements.txt:33-37` | 当前 OpenGL 后端必须保留 |
| Color 渲染在 QThread 中使用 GL context | `src/features/color/core/async_renderer.py:89`（6,011 行） | Metal 需要独立 command queue/资源线程模型 |
| **第二个 renderer** | **`src/shared/video/video_async_renderer.py`（1,046 行）** | §3.1 编排层抽取必须覆盖两个 renderer |
| **第三个 GPU renderer（共享 GL context）** | **`src/features/edit/core/edit_gpu_preview_renderer.py`** | 同样需要后端化 |
| 调色 shader 动态生成 GLSL | `src/features/color/core/shader_compiler.py:142-575`、`:575-816`（**非** v1 所引 `:542-555`，那里只是 vertex 字面量） | 见 §2.4 语料实测 |
| **present/fence/lease 协议已存在** | **`src/shared/gpu/present_frame.py`（16KB）** | 见 §2.2 |
| **GL 后端 facade 部分已存在** | **`src/shared/gpu/gl_backend.py`、`gl_context_hub.py`、`gpu_scope_engine.py`（27KB）** | P3-02 是扩展而非新建 |
| **GPU self-test 已存在** | **`src/shared/video/yuv_gpu_self_test.py`（332 行）** | §4.2 切换流程可直接复用 |
| **VT 硬解探针 + 播放路由策略已存在** | **`src/shared/media/macos_vt/hardware_capability.py`** | 直接约束 Phase 5 前提 |
| **FFmpeg hwaccel 解码已存在** | **`src/shared/media/hwaccel.py`（`HardwareDecoder` 枚举、`FFmpegHwaccelSource`）** | Phase 8/9 是改造既有 |
| macOS VT 零拷贝当前导入 OpenGL | `src/shared/media/macos_vt/iosurface_gl_bridge.py:760-930` | Metal 目标应改为 CVMetalTextureCache 路径 |
| 已有 GPU 帧抽象和 release callback | `src/shared/media/gpu_frame.py:128-255` | 可作为 Metal/D3D 帧契约起点 |
| GPU API 枚举已经预留 metal/d3d11/cuda | `src/shared/media/gpu_frame.py:119` | 该 Literal **无任何代码分支依赖**，可安全演进 |
| native codec 当前为 CPU C + SIMD | `native/topos_codec/CMakeLists.txt`（C11） | 不纳入第一阶段 Metal 迁移 |
| **Metal 实现为零行** | 全仓库无 `MTLDevice`/`CVMetalTextureCache`/`MTLTexture`/`.metal` | Phase 1 是真正的起点，不是改造 |

### 2.2 已存在的后端抽象（v1 完全遗漏，直接影响 Phase 2/7 设计）

**这四项已实现的能力，Phase 2/7 必须扩展而非重建：**

| 既有抽象 | 位置 | 已覆盖本计划的什么 |
|---|---|---|
| `GpuPresentFrame` + `TextureLease` + `OutputSlotPool` | `src/shared/gpu/present_frame.py:82,230,276` | **fence + lease 状态机 + viewer ack**：`begin_writing/mark_ready/begin_viewer_use/ack/release`。模块文档明文禁止裸 `tex_id` 传递与每帧 `glFinish`。这是 P2-03/P2-04 最难的部分，已完成 |
| `EffectSpec` + `ShaderKind` | `src/features/shared/effects/effect_spec.py:66` | 自述为 *"Pure-data description of an effect, shareable across renderers"*，以纯数据持有 GLSL 源 + `ParamDef` + `shader_kind`。**Phase 7「Shader 迁移规则」的 "Shader IR" 应扩展它** |
| `EffectCapability` + `EffectDiagnosticState` + `classify_for_export()` | `src/features/shared/effects/effect_capability.py:49` | `gpu_preview/cpu_preview/gpu_export/cpu_export/supports_hdr/supports_alpha/deterministic`。**Phase 7「插件能力」的 `hdr_safe`/`fallback_policy`/「绝不静默丢效果」已存在，只需加 `graphics_backends` 轴** |
| `.tglsl` Capability Manifest | `docs/PLUGIN_DEV_GUIDE.md:102-129`、`src/features/color/core/plugin_manager.py:182-185` | `@precision`/`@determinism`/`@export_support`/`@threading`/`@input_space`/`@output_space` |
| **`DecoderBackend` ABC** | **`external/file_explorer/plugins/video_pro/base_player.py:143-168`** | decode 轴先例：抽象属性 + `frame_rgb(index,size)` + 幂等 `close()`，含线程/生命周期契约。ARCH-2 已完成（2026-09-06，工作量 L），两个实现（`ToposMovieSession`、`_LibavSession`） |

**关于 `DecoderBackend` 的重要限定（勿直接复用）**：其核心方法 `frame_rgb(index, size) -> (H,W,3) uint8` 是 **CPU readback 接口**，与 §3.3 要消灭 CPU download 的方向相反，且其作用域是 file_explorer 的视频预览播放器，不是主渲染管线。

> **结论：可复用其「模式与生命周期契约定型」（ABC + 幂等 close + 资源释放跟随解码线程退出 + use-after-free 防线），不可沿用其方法签名。** 主管线的 decode/interop 轴必须用 GPU surface 描述符，不能用 RGB 回读。

### 2.3 GL 上下文共享组：隐藏的关键路径（v1 完全未提）

```python
# src/main.py:200
# 必须开启全局 OpenGL 上下文共享，否则 QOpenGLWidget 无法读取渲染线程的纹理
QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
```

这是承重结构：**纹理靠 GL name 在 context 之间传递**，`src/shared/gpu/gl_context_hub.py` 专门用 2×2 像素测试做 `probe_sharing()`。

实测规模：

```text
.glo（GL texture name）交接     227 处，分布在 42 个文件
QOpenGLWidget 引用              42 处 / 16 个文件
QOpenGLWidget 具体子类          5 个（+1 个二级子类）
运行时活动实例                  12 个（4 个内容视口 + 8 个 scope 控件）
```

| 子类 | 位置 | 用途 |
|---|---|---|
| `GLCanvasViewport` | `src/features/color/ui/gl_viewport.py:14` | Color 主视口 |
| `CompositeViewer` | `src/features/composite/ui/composite_viewport.py:940`（11,491 行） | Composite 节点图视口，**106 处 ctypes + PyOpenGL `_GLWrapper`（`:193`）自建 GL 绑定** |
| `ProgramViewer` | `src/features/edit/ui/program_viewer.py:719`（3,278 行） | Edit 节目监视器 |
| `SourceViewer` | `src/features/edit/ui/source_viewer.py:491`（2,313 行） | Edit 源监视器 |
| `GpuScopeWidget`（×7 实例） | `src/shared/ui/widgets/gpu_scope_widget.py:110` | Scope 显示 |
| `CieGpuScopeWidget` | `src/shared/ui/widgets/cie_scope_widget.py:163` | CIE 色度图 |

**对迁移的含义**：

1. 一个 `CAMetalLayer` **无法加入** `AA_ShareOpenGLContexts` 组，Qt 也不会把 Metal layer 合成进有 `QOpenGLWidget` 的同一窗口。
2. 那 227 处 `.glo` 交接、42 个文件，必须迁移到后端无关句柄。**这不是 Phase 2 的一行工作项，而是 Phase 6/7/10 的关键路径。**
3. 5 个 viewport 类都需要 Metal 对应实现（NSView/CAMetalLayer），其中 `CompositeViewer` 单文件 11,491 行。
4. 控件层之外还有 **17 处 `from OpenGL import GL`** 与 **`src/features/composite/render/gpu_resource_pool.py:100,154,187,1063` 的 4 处 `ctypes.CDLL(None)` 直连 GL**（全仓库 `ctypes.CDLL(None)` 共 5 处，另一处在同文件 `:1051` 的注释里），同样要过一遍。

**勘误**：缩略图与节点编辑器预览**不**使用 GL（`src/shared/media/thumbnail_provider.py` 等为纯 CPU/QPainter），无需迁移。

### 2.4 Shader 语料实测（v1 仅一句话带过）

在 `src/**/*.py` 中实测：

| 指标 | 实测值 |
|---|---:|
| `#version` 指令总数 | **222**（30 个文件） |
| ├ `#version 330` 系 | 198（196 + `330 core` + `330 fragment shader`） |
| ├ `#version 430` | 13 |
| └ `#version 410 core` | 10 |
| `.glsl` 独立文件 | 10 个 / 2,816 行 |
| `.program(` 编译点 | 90 处 |
| `usampler2D`（10/12-bit 整数路径） | 45 处 |
| `imageStore` | 9 处 |
| 单一最大 shader 文件 | `src/features/composite/core/shaders.py` = 7,337 行 / 102 个 `#version` |

**compute shader 精确清点（v1 记为「12 个」，是 `local_size_x` 的出现次数，不是 shader 数）**：

| 项 | 实测值 |
|---|---:|
| `_*_COMPUTE` 源码常量 | **10 个**，全部在 `src/features/composite/core/shaders.py:3527-4479` |
| ├ 经 `_COMPUTE_SOURCES` 注册可编译 | **7 个**：`blur_h`/`blur_v`/`blur_h_shared`/`blur_v_shared`/`erode_dilate`/`erode_dilate_shared`/`histogram`（`compute_shader_manager.py:88-97`） |
| └ 已定义但**零引用** | **3 个**：`_BLUR_H_COMPUTE`(:3527)、`_BLUR_V_COMPUTE`(:3637)、`_CDF_COMPUTE`(:4479) |
| 能力探测桩（`local_size_x = 1` + 空 `main`） | **2 处**：`compute_shader_manager.py:188`、`histogram_manager.py:156`——**不是真 compute shader** |
| `local_size_x` 出现总数 | 12（= 10 真 shader + 2 桩） |

10 个常量名为 `_BLUR_H/V_COMPUTE`、`_BLUR_H/V_COMPUTE_SSBO`、`_BLUR_H/V_COMPUTE_SHARED`、`_ERODE_DILATE_COMPUTE(_SHARED)`、`_HISTOGRAM_COMPUTE`、`_CDF_COMPUTE`。**真正的 compute 效果入口只有 2 个**：`apply_blur_compute`(:347)、`apply_erode_dilate_compute`(:461)，调用点 2 个（`blur_evaluator.py:151`、`filter_evaluator.py:75`）；外加直方图 1 条独立链路（见 §0.4）。

**关键结论：不存在可供二次发射的 IR。** `shader_compiler.py`（1,082 行）通过 **186 处 `shader_code.append()`（其中 129 处为 f-string）** 逐层生成 uniform 名，并用**正则改写源码**做插件命名空间隔离：

```python
re.sub(r'\bprocess\s*\(', f'process_L{i}_P{p_idx}(', code)              # :395
re.sub(r'\b'+re.escape(var_name)+r'\b', f'L{i}_P{p_idx}_{var_name}', code)  # :401（:734/:738 同）
```

这套机制依赖 DCTL ABI 的**文本同一性**（`vec3 process(vec3 color, vec2 uv)` + 隐式全局 `u_input_tex`/`u_tex_size`/`u_aspect_ratio`，见 `docs/PLUGIN_DEV_GUIDE.md:133-156`）。**MSL 没有等价的隐式全局契约，文本 `re.sub` 移植不成立。**

> 因此 Phase 7 的目标不是"加一个 MSL emitter"，而是**把 `shader_compiler.py` 改造成以 `EffectSpec` 为输入的双发射架构**（含 argument buffer 或生成式 struct 替代 129 处 f-string 注入）。这是架构性重写。

**插件实际规模**：`plugins/*.tglsl` 4 个（其中 `Film_Halation.tglsl` 仅 11 行，函数体为 `return color;`，是占位符，无需移植）；`plugins/*.tai` 1 个；`plugins/node_plugins/` 2 个；内建 `EffectSpec` 7 个；Composite 节点类型 90 个。

### 2.5 平台基线不对称（v1 按同一时间线顺次推进，是结构性误判）

| | macOS `src/shared/media/macos_vt/` | Windows `src/shared/media/windows_hw/` | Metal |
|---|---|---|---|
| 文件 / 行数 | 10 / **6,570** | 6 / **609** | **0** |
| 性质 | 真实、已接线、有硬件证据 | 4 个诚实骨架 + 1 个真实文件 | 无 |
| 原生绑定 | 真实 ctypes + PyObjC | **无**（0 处 ctypes/COM/DLL） | 无 |
| 活跃调用点 | 9+ | 1 | 0 |

- **macOS 必须扩展，不可替换**：`vt_session.py` 有 `_VT_DECODE_IMPLEMENTED=True`，`iosurface_gl_bridge.py` 有 `_IOSURFACE_BRIDGE_IMPLEMENTED=True`，`capability.py` 要求**探针证据**才报可用。
- **Windows 骨架是诚实的**：`windows_hw/capability.py:90-91` 写着 `# TODO: 实现 D3D11 interop probe` → `reason="interop_not_yet_implemented"`；`d3d11_decoder.py:70`、`d3d11_gl_bridge.py:41`、`windows_zero_copy_probe.py:45` 均返回未实现。
- **Windows 唯一的真实文件是 `hw_planar_source.py`（286 行）**，已接入 `edit_gpu_decode_manager.py:2392`，走的是 `HWACCEL_DOWNLOAD_PLANAR_YUV`（**非零拷贝**）。**Phase 8/9 不得重新实现它。**
- **Windows 测试近乎为零**：`tests/media/test_windows_hw_zero_copy.py` 只断言骨架仍是骨架，且 `hw_planar_source.py` **没有任何测试**。

### 2.6 当前必须保留的路径

```text
CPU DecodedFrame
  → R8/R16UI/float16 OpenGL upload
  → GLSL YUV conversion
  → GLSL grading
  → OpenGL output

VideoToolbox CVPixelBuffer
  → IOSurface/CGL OpenGL texture bridge
  → GLSL YUV conversion
  → existing grading pipeline
```

Metal 通过新路径接入，不删除以上任何路径。

### 2.7 当前性能重点（按实测重写）

见 §0.3。要点：`plane_prepare` 已解决；调色 shader 占 <1%；**瓶颈是 CPU ProRes 解码（~82%），而非 GPU**；且 `decode_ms` 与输出尺寸无关（4K 源解码，见 §0.3）。

因此每个 benchmark 必须区分：

```text
API 性能：同一 GPU 输入纹理下，OpenGL 与 Metal 的渲染差异
产品链路：VideoToolbox/NVDEC → GPU texture → 调色 → 输出的完整差异
```

**Metal compute 本身不会加速软件 CPU 解码。** 时间线导出现已在 VideoToolbox 支持的 H.264/HEVC 4:2:0 输入上直接消费 NV12/P010 Metal texture，减少 CPU 像素搬运；不支持的编码/格式仍走原解码器，不能把该硬解路径外推成通用解码加速。proxy ladder、磁盘代理和 native codec 线程化仍由 `docs/4k-playback-optimization.md` 讨论。

---

## 3. 目标架构

### 3.1 上层编排层

`RendererOrchestrator` 保持与具体 GPU API 无关，负责：

- RenderCommand 队列和优先级。
- 视频播放帧合并、丢弃过期帧。
- seek、clip switch、revision 和 generation。
- 色彩 metadata 传递。
- backend 能力选择。
- 统一性能追踪。
- backend 切换时的 quiesce、release、rebuild。

**修正（v1 遗漏）**：必须覆盖**三个** renderer，而非一个：

| renderer | 位置 |
|---|---|
| `AsyncRenderer`（6,011 行） | `src/features/color/core/async_renderer.py:89` |
| `video_async_renderer`（1,046 行） | `src/shared/video/video_async_renderer.py` |
| `edit_gpu_preview_renderer` | `src/features/edit/core/edit_gpu_preview_renderer.py`（与 `ProgramViewer` 共享 GL context） |

建议逐步提取，不一次性重写。

### 3.2 后端接口（修正：扩展既有，不新建平行接口）

| 接口 | 必需能力 | 落地方式 |
|---|---|---|
| `GraphicsBackend` | initialize、create_texture、create_target、render_pass、present、release | 扩展 `src/shared/gpu/gl_backend.py` |
| `ComputeBackend` | compile_kernel、dispatch、binding、fence | 扩展 `compute_shader_manager.py` 的回退契约 |
| `VideoDecodeBackend` | open、decode_next、seek、capability query、close | 参照（**不沿用**）`DecoderBackend` 的模式，见 §2.2 |
| `GpuInteropBackend` | import decoded surface、export/import handle、resource ownership | 扩展 `gpu_frame.py` + `present_frame.py` |
| `BackendCapabilities` | pixel format、bit depth、HDR、zero-copy、compute、interop、feature family | 扩展既有 capability 模块，勿新建 |
| 性能追踪 | CPU/GPU 时间、带宽、掉帧、队列深度、fallback 原因 | 复用 `performance_monitor.py` + `frame_path.py` |

**`present`/`vsync` 归属需明确**（v1 未分配）：`present` 属于 `GraphicsBackend`，但**只允许由 backend render thread 调用**（§7.1）；UI 线程只提交 descriptor。

### 3.3 GPU 帧契约

现有 `GpuFramePlane.texture_id: int`（`gpu_frame.py:150`）过于 OpenGL 化，应演进为后端无关描述：

```text
GpuDecodedFrame
  frame_index / pts / time_seconds
  width / height / visible_rect
  pixel_format / bit_depth
  color_range / color_matrix / primaries / transfer
  graphics_api / decode_api / interop_api
  planes[] / native_resource_handle / sync_token
  release_callback / zero_copy
```

单个 plane 至少包含：

```text
component: Y / UV / U / V / R / G / B / A
native_handle: 后端对象，不暴露给上层解释
resource_format: r8Unorm / r16Unorm / rg8Unorm / nv12 等
target / width / height / stride
ownership: borrowed / retained / renderer_owned
```

规则：

1. 上层只读取 descriptor，不调用 GL、Metal、CUDA、D3D API。
2. `native_handle` 只能由对应 backend 解读。
3. frame release 必须由拥有资源的 backend 在 fence 安全后执行。
4. 不能因为对象暂时没有 CPU 数组就直接宣称 zero-copy；必须记录实际路径。
5. **（新增）present 路径复用 `present_frame.py` 的 `TextureLease` 状态机，不新建第二套 lease 协议。**

### 3.4 分层归属（v1 缺失，受 `docs/module_boundary_rules.md` 硬约束）

`docs/module_boundary_rules.md:42-45` 规定：**`src/shared` 不得 import `src.features.*` 或 `src.app.*`**，由 `tests/architecture/test_boundary_guardrails.py` 强制。

| 新增模块 | 归属 | 理由 |
|---|---|---|
| `capabilities.py`、`backend_selector.py`、`metal/` | `src/shared/gpu/` | 不依赖 feature |
| `OpenGLRenderBackend` facade | **`src/features/color/core/`**（非 `src/shared/`） | 需包装 feature 侧 renderer，放 shared 会违反边界 |
| `MetalVideoInteropBackend` | `src/shared/media/macos_vt/` | 与既有 VT 层同侧 |

**v1 的 P2-02 写作「`src/shared/gpu/opengl_backend.py` 或 `src/features/color/core/`」，是边界违规的模糊表述，此处定为后者。**

`module_boundary_rules.md` 目前**未规定平台/后端专属代码的归属**，需要在 §9.4 的 ADR 中一并补规则。

---

## 4. 后端选择与切换策略

### 4.1 选择模式（修正：四维独立，不再压回单一枚举）

```text
graphics_api = auto | opengl | metal | d3d11 | d3d12
compute_api  = auto | glsl_fragment | metal_compute | cuda | directml | cpu
decode_api   = auto | cpu | videotoolbox | nvdec | d3d11va | qsv | amf
interop_api  = auto | none | iosurface | cvmetal | d3d_shared | cuda_interop
```

每种维度支持 `force_*` 前缀用于回归与故障排查。**强制模式能力不足时必须返回明确诊断，不静默丢效果**（`.clinerules:11`）。

### 4.2 切换粒度

第一阶段只支持启动级或项目会话级切换。不要在单个 GPU command 尚未完成时切换 backend。

安全切换流程（复用既有 self-test，见 §2.1）：

```text
暂停播放
 → 停止提交新 command
 → drain 当前 backend 队列
 → 等待或轮询最后一个 fence
 → release backend-owned resources
 → 创建目标 backend
 → 重新创建 pipeline / texture / sampler
 → 重新导入当前帧
 → 运行 yuv_gpu_self_test
 → 恢复播放
```

### 4.3 fallback 层级

```text
目标 GPU zero-copy
  → 同一 API 的 CPU upload fallback
  → 当前平台的另一 GPU backend
  → CPU decode + CPU upload + 基础渲染
  → 明确提示不可用功能
```

任何 fallback 都必须记录：

```text
requested_backend / selected_backend
requested_decode_path / selected_decode_path
fallback_reason / unsupported_format / driver_or_probe_error
```

**（新增）复用既有回退通知机制**：`compute_shader_manager.py` 的 `_fallback_callback(shader_name, error_msg)` 已实现「回退发生 → 通知 UI」且每 capability key 只记一次日志（`:64`）。新后端的回退应接入同一通道，避免出现第二套回退语义。

---

## 5. 分阶段落地计划

### Phase 0：基线复用与补齐（不新建基准设施）

**目标**：在不改变现有行为的前提下，把 OpenGL 基线固化下来。**v1 将本节七成内容当作新建工作，实为已有设施。**

| 编号 | 工作项 | 主要文件 | 交付物 | 验收标准 | 量 |
|---|---|---|---|---|---|
| P0-01 | 后端与路径清点，**范围必须含 `src/`、`external/`、`native/`** | 全仓库 | backend inventory（含 file:line） | v1 §2.1 的 8 条 + §2.2 的 5 项既有抽象全部入表 | S |
| P0-02 | **复用**现有 harness，补四维字段 | `tests/benchmark/benchmark_yuv_gpu_pipeline.py`（504 行）、`tools/benchmarks/playback_benchmark.py`（446 行）、`src/shared/media/performance_monitor.py`、`frame_path.py` | 统一 trace 字段 | 每个视频帧都能知道真实四维路径；不新建第二套统计 | M |
| P0-03 | **解决基准素材来源（阻塞项）** | `tests/media/fixtures_manifest.json`、`generate_test_fixtures.py` | 4K H.264/HEVC/ProRes 422/4444 可用样本 | 同一帧范围可重复运行 | M |
| P0-04 | 固化 OpenGL 基线快照，**并补测 4K 全分辨率档**（现有归档只有 proxy 档，见 §0.3） | 同上 harness | JSON baseline | 输出 p50/p95/max、有效 FPS、掉帧、CPU/GPU 分段时间；**已含 `git_sha`**；**必须含 3840×2160 档** | M |
| P0-05 | 增加画质参考帧 | 新增 benchmark 工具 | RGBA16F/线性空间 reference | 覆盖 SDR、HDR、8/10/12-bit 和 Alpha | M |
| P0-06 | **修复既有 zero-copy 诚实性缺口** | `src/shared/media/topos_surface_capability.py:113-146` | 能力判定改为真实探测 | Windows/Linux 分支**只靠环境变量即可报 `zero_copy=True`**，必须先修，否则 §6.4 门禁不可执行 | M |

**P0-03 阻塞说明（v1 未识别）**：`fixtures_manifest.json` 声明的 4K/ProRes 素材（最大 800MB）**本机全部不存在**，`TOPOS_VIDEO_FIXTURES_PATH` 未设置，`download_base_url` 为占位符 `https://example.com/topos-fixtures`。现存样本仅 5 个小文件（最大 6.8MB）与 `synthetic/`。`generate_test_fixtures.py` 可合成 4K/ProRes，但**合成素材对解码与零拷贝的性能评估价值有限，不能替代真实素材做门禁**——需要明确素材托管决策。

**验收基线**沿用 `docs/4k-playback-optimization.md:42-52`（§3.1 播放预算）与 `:9-16`（§1 复查结论）：4K 30fps 播放总帧 p95 < 33ms，ProRes proxy p95 < 40ms，`plane_prepare_p95 < 3ms`，播放热路径不做每帧 CPU readback。

> **两套目标体系需调和（v1 未处理）**：`tests/media/performance_targets.json` 用的是 `max_ms`/`min_fps`/`max_drop_rate` 单值模型，**全文没有任何百分位类型**。而本计划的验收用 p95。P0-02 应在其 `target_types` 中补 `max_p95_ms` 等类型，而不是并行维护两套。

### Phase 1：独立 probe（前移；契约冻结之前）

**目标**：用实测回答开放问题，不接主 UI。**顺序依据：`docs/4k-playback-optimization.md:413`「两个 probe 都成功后，再决定是否做正式 `GpuDecodedFrame` 接口和后端抽象」。**

| 编号 | 工作项 | 主要文件 | 交付物 | 验收标准 | 量 |
|---|---|---|---|---|---|
| P1-01 | Metal device/queue 创建与能力查询 | `tools/probe_metal_capability.py` | `MetalDevice` | ✅ **已完成**（§0.5）：device/queue/pipeline/dispatch 全部跑通 | ✅ |
| P1-02 | **CVMetalTextureCache 格式矩阵 probe** | `tools/probe_metal_capability.py` | 格式支持矩阵 | ✅ **已完成 NV12/P010/BGRA**（§0.5）；**余项：4:2:2 10-bit、ProRes**（本机无 ProRes 硬解，见 §0.2 A′） | ✅/M |
| P1-03 | **Qt 表面共存 probe** | `tools/probe_qt_metal_coexistence.py` | 结论报告 | ✅ **已完成**（2026-09-11）：同窗共存可行，窗口级重叠时 Metal 覆盖 GL，vsync 60Hz 锁定，resize 需手动 re-frame；EDR 未测（P10-05）——`tests/media/results/qt_metal_coexistence.md` | ✅ |
| P1-04 | **ctypes↔PyObjC 桥接验证** | `tools/probe_metal_capability.py` | 桥接方案 | ✅ **已完成**（§0.5）：ctypes rc=0，PyObjC `-6661`，决策已实证 | ✅ |
| P1-05 | MSL 编译与 render-only 性能对比 | `tools/bench_render_ab_metal_gl.py` | A/B 数据 | ✅ **已完成**（2026-09-11）：Metal +81~85%（四组合一致），K1 不触发——`tests/media/results/backend_compare_render_ab.md` | ✅ |
| P1-06 | probe 总报告与 **K1/K3/K4 门禁判定** | `tests/media/results/phase1_probe_report.md` + `docs/adr/0003` | 结构化 JSON + `.md` | ✅ **已完成**（2026-09-11）：K1/K3 不触发，K4 条件性通过（原生扩展路线的签名/公证矛盾在 Phase 4 前复核） | ✅ |

**P1-04 说明（v1 未预料）**：`src/shared/media/macos_vt/vt_session.py:31` 明文记录：

> `VTDecompressionSessionCreate 通过 ctypes 调用（PyObjC 无法正确注册回调）`

即解码回调是 ctypes `CFUNCTYPE`，回调内拿到裸 `CVImageBufferRef`（`c_void_p`）。而 `CVMetalTextureCacheCreateTextureFromImage` 由 PyObjC 的 `Quartz` 模块暴露（已确认符号存在：`CVMetalTextureCacheCreate`、`CVMetalTextureCacheCreateTextureFromImage`、`CVMetalTextureGetTexture`）。**把 ctypes 的 `void_p` 传入 PyObjC 调用是真实集成陷阱。**

> **决策：沿既有 ctypes 层用 ctypes 声明 CoreVideo 的 CVMetalTextureCache 函数，不在回调边界上混用两套 FFI。** P1-04 负责证实该路线可行。

**绑定方案（v1 的 P3-06 保留）**：正式实现优先使用小型 Objective-C++/Swift 扩展，Python 侧只负责配置、提交 descriptor 和读取统计。PyObjC 可用于早期 probe，但不宜长期承担完整视频渲染热路径。**注意 `pyobjc-framework-Metal` / `-MetalPerformanceShaders` 当前不在 `pyproject.toml [macos]` extras 中，需按 §9.2 固定版本。**

### Phase 2：后端能力和资源契约（probe 通过后冻结）

**目标**：加入后端抽象，但仍然只运行现有 OpenGL。

| 编号 | 工作项 | 主要文件 | 交付物 | 验收标准 | 量 |
|---|---|---|---|---|---|
| P2-01 | 四维 kind 枚举（graphics/compute/decode/interop） | `src/shared/gpu/backend_types.py` | 类型和序列化定义 | ✅ **已完成**（2026-09-11）：四枚举 + force_ 前缀解析 + from_dict/from_env 序列化，非法值显式 ValueError | ✅ |
| P2-02 | `BackendCapabilities`（扩展既有，勿新建） | `src/shared/gpu/capabilities.py` | 能力矩阵 | ✅ **已完成**（2026-09-11）：四维快照全委托既有探测（VT/windows_hw/surface gate），zero_copy 只信 probe 证据 | ✅ |
| P2-03 | `GpuFramePlane.texture_id` → opaque handle | `GpuPlaneResource`（gpu_frame.py）+ `TextureLease.from_resource` | 新帧资源契约 | ✅ **已完成**（2026-09-11）：非 GL api 必须携带 resource（强制校验）；TextureLease 同一状态机扩展；旧 GL 构造器零改动兼容 | ✅ |
| P2-04 | backend-owned fence/release contract | `GpuFrameRelease.owner_backend` + `release_when_fenced()` | 资源生命周期协议 | ✅ **已完成**（2026-09-11）：fence 未确认绝不释放，与 release() 共享 exactly-once 位；drop/seek/cancel/close 路径全覆盖测试 | ✅ |
| P2-05 | 会话级后端配置 **+ `.topos` schema 版本与迁移** | `config_manager.py` `backend` 段 + `_migrate_backend_config` | 四维配置项 | ✅ **已完成**（2026-09-11）：默认全 auto（行为不变）；段缺失补齐持久化、坏值重置 auto + warning | ✅ |
| P2-06 | 选择与诊断服务 | `src/shared/gpu/backend_selector.py` | selector + diagnostic | ✅ **已完成**（2026-09-11）：auto 沿优先级链回退并记录 trail/reason（§4.3 字段）；显式/force 不可用时保持 requested + 诊断，不静默切换 | ✅ |

**P2-05 补充（v1 缺失）**：四维后端配置若写入工程文件，属 `.topos` schema 变更；同时需定义 **backend 切换对 undo/redo 与工程状态的影响**（是否使既有 GPU 资源失效、是否强制重渲染）。`RenderFramePath` 枚举的改名同样会破坏 `tests/media/results/*.json` 中已序列化的路径字符串，**影响与 v1 基线的可比性**，需保留旧值别名。

### Phase 3：封装现有 OpenGL 后端

**目标**：把当前完整 OpenGL 实现变成第一个正式 backend，功能不变。

| 编号 | 工作项 | 主要文件 | 交付物 | 验收标准 | 量 |
|---|---|---|---|---|---|
| P3-01 | 保留 `AsyncRenderer` 作为 orchestrator（含三个 renderer） | `src/features/color/core/async_renderer.py`、`src/shared/video/video_async_renderer.py` | 调度层边界 | ✅ **已完成**（2026-09-11）：docstring 声明 orchestrator 边界 + `test_orchestrator_boundary.py` 公开 API 面（20 方法）/RenderCommand 再导出/7 lane 冻结；lane_merge 58 用例与全套件回归验证行为不变 | ✅ |
| P3-02 | 创建 `OpenGLRenderBackend` facade | **`src/features/color/core/`**（见 §3.4） | GL backend facade | ✅ **已完成**（2026-09-11）：`opengl_render_backend.py` 契约实现（渲染线程守卫、render_pass 委托 submit、present 只读、release 台账模式）；契约落为 `src/shared/gpu/graphics_backend.py`（对 §3.2「扩展 gl_backend.py」的勘误：Metal 需无 moderngl 依赖 import 契约）；资源仍由渲染线程按原有时机创建/释放 | ✅ |
| P3-03 | 上传、FBO、shader、scope、denoise 资源注册到 GL backend | `src/shared/video/`、`src/shared/gpu/` | GL resource registry | ✅ **已完成**（2026-09-11）：`src/shared/gpu/gl_resource_registry.py` 台账（组→资源、release_fn LIFO 编排供 §4.2 切换流程）；run() 初始化 + scope/present 懒创建点登记五组资源 + grading/present 辅助组；`get_gpu_backend_report()` 诊断；GPU 单测全绿（tests/color+shared/gpu 1416 passed） | ✅ |
| P3-04 | `RenderFramePath` 明确区分 graphics backend | `src/shared/media/frame_path.py` | 路径枚举（含旧值别名） | ✅ **已完成**（2026-09-11）：`opengl_/metal_external_*` 四值 + `qualify_external()` 生产者切换 + `from_serialized()` 旧值别名；消费方零拷贝判定走别名兼容 | ✅ |
| P3-05 | 保留旧入口一段时间 | 相关调用方 | compatibility adapter | ✅ **已完成**（2026-09-11）：P3-02/03 全部为增量，旧调用方零修改兼容；`VideoAsyncRenderer` docstring 标注 v2 遗留状态（生产 v3 由 AsyncRenderer 承担）；`RenderCommand` 再导出与 `from_serialized` 别名未动；顺手修复 `gl_backend.py` 未定义 `logger` 的潜在 NameError | ✅ |

### Phase 4：Metal compute 能力解锁（**第一收益**，v1 排在最末）

**目标**：解决 §0.4 实测确认的、已被项目自己标注为"更慢"的回退路径，以及 macOS 上静默失效的 Normalize 百分位链路。

| 编号 | 工作项 | 主要文件 | 交付物 | 验收标准 | 量 |
|---|---|---|---|---|---|
| P4-01 | Metal compute pipeline 基础（kernel 编译、dispatch、buffer/texture binding、fence） | `src/shared/gpu/metal/`、native layer | `MetalComputeContext` | ✅ **已完成**（2026-09-11）：PyObjC 路线（K4 复核：compute 不在视频热路径，微秒级 dispatch 开销可忽略；原生扩展留给 Phase 5+）；dispatch 仅编码、finish 才 commit+wait——无每帧全局阻塞；19 个测试（fake 层 + 真机层） | ✅ |
| P4-02 | 大半径模糊/腐蚀膨胀的 MSL compute 实现 | 对齐 `compute_shader_manager.py` 的 **2 个效果入口**（`apply_blur_compute:347`、`apply_erode_dilate_compute:461`）与其 7 个已注册 shader 名 | MSL kernel 集 | ✅ **已完成**（2026-09-11）：6 个 kernel 名与 `_COMPUTE_SOURCES` 逐名对齐（histogram 归 P4-03）；numpy 参考数值一致（高斯 σ/kernel_size/权重/边缘、形态学 min/max）；blur shared 为正确 threadgroup tile 重设计（GLSL 原版协作加载有跨行覆写隐患）；CPU staging 桥 + `TOPOS_METAL_COMPUTE` 门控（P4-05 实测 fragment 更快，默认保持 fragment，见下） | ✅ |
| P4-03 | **Normalize 百分位链路（最高价值项）** | `src/features/composite/evaluators/histogram_manager.py`（579 行） | macOS 可用的 GPU 直方图 + GPU CDF | ✅ **已完成**（2026-09-11）：①GL 4.1 上 percentile 返回真实区间（旧路径恒 (0,1) 静默空操作）；②histogram+CDF 双 kernel 单 commit，CDF/百分位桶 GPU 归约（高桶以 (255-bin) 编码 atomic_min——atomic_max 无合法初值）；③两者皆不可用时 warn-once + `get_diagnostic()`；`is_available()` 解除 color_evaluator:506 短路；p95 12~29ms；顺带修复该路径暴露的两个既有潜在 bug（simple_vertex_array varargs、vao.render 图元模式） | ✅ |
| P4-04 | 接入既有回退通知机制 | `compute_shader_manager.py` 的 `_fallback_callback` | 统一回退通道 | ✅ **已完成**（2026-09-11）：Metal 失败经同一 `_fallback_callback` 通知 UI 并返回 None 落 fragment fallback；默认关闭状态也经 warn-once 显式记录——无第二套回退语义 | ✅ |
| P4-05 | A/B 门禁与收益报告 | Phase 0 基线 | Metal compute A/B report | ✅ **已完成**（2026-09-11）：`tests/media/results/metal_compute_ab.{json,md}`。**关键实测**：CPU staging 桥模糊全链路 p95 87~438ms vs fragment fallback 0.55~0.94ms（staging 往返主导）——故 Metal 模糊路径默认关闭、`TOPOS_METAL_COMPUTE=1` 显式启用；Normalize 链路 12~29ms 且为正确性解锁（默认开启）。K5（覆盖落地）未触发。收益结论：**Phase 4 的实际收益在 Normalize 正确性 + MSL kernel 资产；模糊性能收益需待 Phase 5+ Metal 原生纹理互通（去 staging）后重测** | ✅ |

### Phase 5：Metal 视频零拷贝垂直链路（**后移并条件化**）

**目标**：仅对 **VT 真有硬解** 的格式迁移。**依据 §0.1 实测：本机 ProRes 无 VT 硬解，该格式不适用。**

```text
VideoToolbox（仅硬件解码可用的格式）
  → CVPixelBuffer / IOSurface
  → CVMetalTextureCache
  → NV12/P010 Metal textures
  → MSL YUV conversion
  → RGBA16F output
```

| 编号 | 工作项 | 主要文件 | 交付物 | 验收标准 | 量 |
|---|---|---|---|---|---|
| P5-01 | `MetalVideoInteropBackend` | `src/shared/media/macos_vt/`、native layer | CVPixelBuffer import | ✅ **已完成**（2026-09-11）：`cv_metal_bridge.py` 的 `CVMetalVideoBridge`——ctypes CoreVideo 路线（P1-04 实测决策），NV12 导入 MTLTexture plane（`GpuPlaneResource(api='metal')`），FourCC 实测 420v/420f | ✅ |
| P5-02 | P010/10-bit 支持 | Metal bridge + MSL | P010 import | ✅ **已完成**：P010（FourCC 实测为 **x420**，非字面 'p010'）→ R16Unorm/RG16Unorm plane，全程无 numpy float16 转换；真机 parity 门禁通过 | ✅ |
| P5-03 | 色彩 metadata 传递 | `gpu_frame.py`、color metadata | matrix/range/transfer binding | ✅ **已完成**：`GpuDecodedFrame` 元数据 → `build_yuv_convert_params`（8/10-bit × limited/full × bt709/601/2020）→ kernel 常量缓冲；full-range 与 limited 真机 parity 均通过 | ✅ |
| P5-04 | Metal YUV shader | MSL shader | NV12/P010 conversion | ✅ **已完成**：`kernels_video.py` 的 `yuv_to_rgba`（compute）+ numpy 逐条对齐参考；**画质门禁**：合成图案→导入→转换→读回与参考逐像素一致（8-bit ≤2.5/255，10-bit ≤10/1023，AMD 6900XT） | ✅ |
| P5-05 | Metal ring buffer | Metal resource manager | 3-5 frame ring | ✅ **已完成**：`MetalVideoFrameRing`（有界 in-flight；`mark_submitted`→`on_gpu_complete`（finish 后）→`release_completed`，fence 确认前绝不释放；exactly-once 由 `GpuFrameRelease` 保证）；桥对每帧 CFRetain/CFRelease 自平衡 | ✅ |
| P5-06 | 接入 benchmark，不接默认主播放 | Phase 0 harness | Metal probe report | ✅ **已完成**：`tools/bench_metal_video_zero_copy.py` + 三份归档（`metal_video_zero_copy_bench*.json/.md`）。本机实测：**导入 p50 0.08~0.10ms**（对比 §0.3 CPU 上传 3.6~6.0ms）；真实 H.264 file 模式端到端 3.4ms/帧（decode 2.55 + import 0.11 + encode 0.23 + gpu 0.63）。报告标注 Intel/AMD 降级路径，A′ 主目标机到位后复测 | ✅ |
| P5-07 | 接入 `GpuDecodedFrame` | `src/shared/video/frame_provider_service.py` | Metal GPU frame | ✅ **已完成（范围调整）**：接入点为 `MacOSVtZeroCopySource.import_pixel_buffer_frame_metal` / `read_next_frame_metal`（并行于 GL 导入路径）；产物 `GpuDecodedFrame(decode_path='macos_vt_cvmetal_zero_copy', zero_copy=True)`，路径枚举按 `metal_external_yuv` 限定（P3-04 契约测试钉死），无 `to_ndarray()`/`Texture.write()`。2026-09-14 新增 `request_metal_frame`，并由时间线 Metal 导出直接消费其 NV12/P010 texture；交互式 GL 预览路径不变 | ✅ |
| P5-08 | Metal 视频预览开关 | 配置/诊断 UI | developer-only toggle | ✅ **已完成**：`TOPOS_METAL_VIDEO_PREVIEW` 环境开关（默认关）+ `metal_zero_copy_gate()`（开关/Metal 可用性/格式矩阵/**VT 硬解探测**四条逐项诊断，ProRes 显式拒绝）；导入失败返回 None + `fallback_reason`，调用方回退 GL/planar——不黑屏。UI 面板留给 Phase 10 | ✅ |

**ProRes 422/4444 处理（v1 判断正确，此处以实测强化）**：本机 ProRes **无 VT 硬解码器**，因此**不存在可零拷贝的硬件 surface**。保留 R8/R16UI CPU upload 和 proxy ladder；同时**遵从既有 `playback_should_prefer_ffmpeg()` 路由策略**，不要绕过它强行走 VT。

### Phase 6：Metal 核心调色链路（v1 无编号/无验收，此处补全）

**目标**：架构统一，**不是收益来源**（实测调色 shader 占帧预算 <1%，见 §0.3）。保持 OpenGL 完整可用。

| 编号 | 模块 | 迁移内容 | 主要文件 | 关键风险 | 验收标准 | 量 |
|---|---|---|---|---|---|---|
| P6-01 | YUV/Raw Preview | MSL fragment | `src/shared/video/yuv_gpu_upload.py`（3,243 行）、`src/features/color/core/shader_nodes.py`（930 行） | range、矩阵、坐标原点、`usampler2D` 整数路径（45 处） | ⚠️ **部分完成**（2026-09-11）：NV12/P010→RGBA16F 的 MSL 转换 + 画质门禁已随 Phase 5 交付（`kernels_video.py`，compute 形态）；接入渲染预览路径（fragment 形态 + `yuv_gpu_upload.py` 消费）待 Phase 6 渲染管线接入 | 🔶 |
| P6-02 | 基础调色 | lift/gamma/gain、曝光、对比度、饱和度 | `shader_compiler.py`、`shader_nodes.py` | HDR clamp、浮点精度、**129 处 per-layer f-string 注入的替代方案** | SDR/HDR golden 一致 | L |
| P6-03 | LUT/Curves | 3D texture、曲线纹理 | `shader_compiler.py:323,693`（`sampler3D`） | 纹理格式、采样边界 | LUT corpus 一致 | M |
| P6-04 | Qualifier/Mask | HSV、matte、形态学 | `shader_nodes.py`（`QUALIFIER_SNIPPET`、`MASK_SNIPPET`） | 性能、动态循环 | 关键 matte 样本一致 | L |
| P6-05 | Blur/Denoise | **改为消费 Phase 4 的 compute 结果** | `src/features/color/core/video_denoise_shaders.py`（559 行） | temporal history、边界 | 🔶 **改判（2026-09-11 审计）**：v2 改标「消费 Phase 4 compute」的前提不成立——denoise 全部 pass 为**联合双边**（`SPATIAL_SEPARABLE_FRAGMENT` 逐 tap 亮度/色度 range 权重 `rat_w`）、多尺度 + 细节恢复 + 运动补偿时域累积（ping-pong 历史纹理），与 Phase 4 内核集（高斯/形态学）**无语义交集**，无可消费点；强迁 compute 反而引入 staging 桥双拷贝（P4-05 实测慢于 fragment）并破坏 temporal history 的纹理语义。denoise 的 Metal 化归入 P6-02~04 双发射器重构后的 per-effect 迁移；「画质与帧时延」A/B 依赖 Metal 渲染管线存在且按 A′ 应在 Apple Silicon 主机器测量 | 🔶 |
| P6-06 | Film grain/Soft clip | MSL 实现 | `shader_nodes.py`（`FILM_GRAIN_SNIPPET`、`SOFT_CLIP_SNIPPET`） | 随机数和 HDR 语义 | ✅ **已完成**（2026-09-11）：`src/shared/gpu/metal/kernels_color.py`（METAL_FILM_GRAIN_MSL + METAL_SOFT_CLIP_MSL + numpy 参考 + 参数结构）+ `tests/shared/gpu/metal/test_metal_kernels_color.py`（30 测试：结构层无需 Metal，真机层真机验证）。**固定 seed 一等的实测契约**：哈希链内建 `dot()` 必须展开（否则整格翻转，65536 点实测 13 处），展开后位级一致；`#pragma clang fp contract(off)` 实测必要；残余 ≤1 fp16 ulp 来自 AIR 后端对 lerp 形状的代数重写（contract/noinline 均不可阻止，实测记录于 kernels_color 模块文档）；soft clip pow 差异 ≤1 ulp（门禁 2）。HDR 语义守护：MSL 源级禁 `clamp(`，恒等参数位级直通，负值/HDR>1 保留。渲染管线接入随 P6-02~04 | ✅ |
| P6-07 | GPU Scope | **改为消费 Phase 4 的 compute 结果** | `gpu_scope_engine.py`（27KB） | 原子操作和精度 | ✅ **验收已达成（2026-09-11 审计判定）**：「无 CPU readback 播放」由 2026-08 GPU scope 实现满足——`AsyncRenderer._do_render_gpu_scope` 驱动共享 `GpuScopeEngine`（sample→point-splat 加法混合累积→display 全 GPU，从不 `read()`，产物以 `ScopeGpuFrame` 纹理 id 发布）；legacy CPU readback（`_do_render_scope_snapshot`）已 deprecated 且播放期门控关闭、仅暂停态作 oracle，shader 编译失败时显式禁用该 lane 而非静默回退 CPU。风险项「原子操作和精度」被 point-splat 设计规避（无 atomics）。「消费 Phase 4 compute」**否决**：Phase 4 直方图内核服务于 Normalize 百分位链（256 bins/atomicAdd），scope 直方图为 512-bin 显示直方图（splat 累积），迁移需 staging 桥（P4-05 实测慢于 GL）无收益；scope 管线的 Metal 视口化归 Phase 10 P10-03 | ✅ |

**范围提示（v1 严重低估）**：Phase 6 的实际对象是 **222 个 `#version` / 90 个编译点 / 10 个 `.glsl` 文件（2,816 行）**，而不是 v1 所列的「7 个优先模块」。**Phase 6 应在 Phase 4/5 之后按效果逐个推进，并在 ADR 中冻结覆盖率门禁。**（✅ 2026-09-11：门禁已在 `docs/adr/0004-dual-emitter-refactor-and-coverage-gates.md` 冻结，台账 `0004a` 随建；SNIPPET 实数 11 个——P7-03 原清单漏 `LUT3D_SNIPPET`，以 ADR 数据修偏为准。）

### Phase 7：Shader、插件和 Composite/3D（v1 无编号，此处补全）

#### Shader 迁移规则（修正：扩展 `EffectSpec`，不新建 IR）

```text
EffectSpec（已有，src/features/shared/effects/effect_spec.py:66）
       ├── GLSL 330 emitter（已有）
       └── MSL emitter（新增）
```

**关键改造**：`shader_compiler.py` 的正则命名空间机制（`:395/:401/:734/:738`）依赖 DCTL ABI 的文本同一性，MSL 无等价契约。需要：

1. 把 `EffectSpec` 升为权威输入，插件 shader 以结构化形式（参数名 + uniform 映射）进入，而非正则改写源码；
2. 用 argument buffer 或生成式 `struct` 替代 129 处 f-string uniform 注入；
3. 共享语义清单：颜色矩阵与 range、HDR 线性空间、LUT 坐标与边界、Alpha/premultiplied、UV/chroma siting/visible rect、插件参数 schema 与版本。

| 编号 | 工作项 | 主要文件 | 交付物 | 验收标准 | 量 |
|---|---|---|---|---|---|
| P7-01 | `EffectSpec` 升为双后端权威输入 | `effect_spec.py`、`shader_compiler.py` | 重构后的编译入口 | ✅ **已完成**（2026-09-11，commit `e842aeb3`）：编译入口只接受 `PluginBinding`（EffectSpec 权威 manifest：参数名+类型+uniform 映射+structural；原始 dict → TypeError，G5）；`GLSL_UNIFORM_TYPES` 与 DCTL ABI 单测对齐防漂移；正则改写降级为 GL emitter 内部细节（MSL emitter 禁止依赖）；**验收达成**——G1 corpus 24 条基线（含重构前归档的 4 条真实插件用例）位级一致；顺带裁决删除 build_shader/update_uniforms/COMPILE_PIPELINE 死链（台账 0004a）；16 条 manifest 单测 | ✅ |
| P7-02 | uniform 注入机制替换 | `shader_compiler.py` | 结构化 uniform 绑定 | ✅ **已完成**（2026-09-11，commit `d8fda7a3`）：`composition_uniforms.py` 单一 manifest（name → glsl_type/msl_type/components/offset）驱动 GL 声明生成（G1 24 条基线位级一致）与 MSL struct 布局（P6-06 对齐规则）；值提取与上传唯一化（async/image_engine 两套手写实现合一，插件 uniform 名/索引与编译侧 manifest 对齐——验收「N 层 × M 插件无正则改写」达成：uniform 绑定零正则，正则仅存于 GL emitter 内部的插件函数体命名空间）；随附 fx 空组残留串参数修复 | ✅ |
| P7-03 | MSL emitter | 新增 | MSL 生成器 | 🔶 收尾中（2026-09-12 起，SNIPPET 实数 **11 个**见 ADR 0004 数据修偏）：**emitter 骨架已交付**（`msl_composition.py`——struct 由 P7-02 manifest 生成、未迁移效果显式 ValueError 不静默跳过；fragment 纹理槽位契约 texture(0)=input/1=curves/2=hsl/3=lut3d/4=warper/5=matte）；**SNIPPET 翻译全部完成**（10 个翻译 + MASK_SNIPPET 死代码删除）：grading、curves/hsl_curve、lut3d、mixer/warper、blur/sharpen（抖动噪声门）、blend modes（19 模式 + main 尾部 matte/opacity 语义）、soft_clip/film_grain fragment 形态（grain 走 P6-06 参考门 ≤2 fp16 ulp + contract(off) 仅 grain 激活时注入）；全部 G2 实测通过（台账 0004a `msl_verified`）；qualifier 归属裁决为 matte 生成侧（P6-02~04）；**仅剩插件 manifest MSL** | 🔶 |
| P7-04 | Composite shader 迁移 | `src/features/composite/core/shaders.py`（7,337 行/102 `#version`） | MSL 版本 | 逐个登记覆盖率；**单项最大工作量** | L |
| P7-05 | compute shader 迁移（**10 个常量 / 7 个已注册**，另 3 个零引用） | 同 Phase 4 P4-02/P4-03 | MSL compute | ✅ **已完成**（2026-09-11）：与 P4-02/P4-03 合并（MSL kernel 集已交付）；**零引用的 3 个常量（`_BLUR_H_COMPUTE`/`_BLUR_V_COMPUTE`/`_CDF_COMPUTE`）已按计划删除**（词边界核查全仓库零引用，~289 行；commit `dfdcd83d`） | ✅ |

#### 插件能力（修正：扩展 `EffectCapability`，不新建 schema）

**已存在**：`EffectCapability{gpu_preview,cpu_preview,gpu_export,cpu_export,supports_hdr,supports_alpha,deterministic}` + `EffectDiagnosticState{UNSUPPORTED,EXPORT_BLOCKED}` + `classify_for_export()`（`effect_capability.py:49`），以及 `.tglsl` 的 Capability Manifest（`@precision`/`@determinism`/`@export_support`/`@threading`）。

| 编号 | 工作项 | 主要文件 | 交付物 | 验收标准 | 量 |
|---|---|---|---|---|---|
| P7-06 | 给 `EffectCapability` 加 `graphics_backends` / `compute_backends` 轴 | `effect_capability.py` | 扩展后的能力模型 | ✅ **已完成**（2026-09-11）：新增两轴（默认 `{opengl}`/`{glsl_fragment}` 保持旧调用方零改动兼容；未知效果=空集）+ `supports_graphics()`/`supports_compute()`（含 fragment 自动回退与 CPU 兜底语义，§0.4/§4.3）；复用 `backend_types` 四维枚举词汇表，未新建平行 schema | ✅ |
| P7-07 | `.tglsl` manifest 增加后端声明 | `src/features/color/core/plugin_manager.py`、`docs/PLUGIN_DEV_GUIDE.md` | 扩展指令集 | ✅ **已完成**（2026-09-11）：`@graphics_backends` / `@compute_backends`（逗号分隔）+ 缺省兜底（旧插件缺声明按 OpenGL-only 处理）+ `parse_backend_declaration()`（未知值显式 ValueError，`auto` 非法）+ 开发指南文档与 tooltip 标签 | ✅ |
| P7-08 | 未支持 effect 的诊断与回退 | `effect_catalog.py`、UI | 诊断路径 | ✅ **已完成（数据层）**（2026-09-11）：`check_backend_support()`——按会话 graphics/compute 后端检查效果，不可用时返回带已声明后端清单的 `UNSUPPORTED` 诊断（绝不静默丢效果）；UI 面板接线随 Phase 10 | ✅ |

**说明**：4 个 `.tglsl` 中 `Film_Halation.tglsl` 为 11 行占位（`return color;`），无需移植；实际需移植的内建 `EffectSpec` 为 7 个。**v1 把插件能力声明当成主要工程量是定位错误——真正的量在 `composite/core/shaders.py`。**

#### Composite/3D

Composite/3D 等复杂内容在核心颜色链路完成后再迁移。不要为了迁移 Metal 早期版本而同时重写 NodeGraph、项目模型和 UI。Composite 节点类型 90 个（`node_engine.py:250`），`CompositeViewer` 单文件 11,491 行。

### Phase 8：Windows NVIDIA 后端

**目标**：为 NVIDIA Windows 建立 NVDEC + CUDA + D3D 的可扩展链路，同时保留 OpenGL。**注意 §2.5：Windows 已有 `hwaccel.py` + `hw_planar_source.py` 的真实硬解下载路径，不得重新实现。**

推荐链路：

```text
FFmpeg demux / parser
  → NVDEC
  → CUDA surface 或 D3D11/D3D12 texture
  → CUDA post-process（可选）
  → D3D11/D3D12 presentation
```

| 编号 | 工作项 | 主要文件 | 交付物 | 验收标准 | 量 |
|---|---|---|---|---|---|
| P8-01 | NVIDIA/GPU/driver capability probe（替换现有 stub） | `src/shared/media/windows_hw/capability.py:90-91` | vendor capability | ✅ **已完成（接口层，2026-09-11）**：新增 `gpu_capability.py`——ctypes DXGI 适配器枚举（COM vtable 索引按文档钉死）+ nvml.dll 驱动版本（可选）+ vendor 映射；`probe_zero_copy_capability` 改为数据驱动判定（NVIDIA 缺驱动版本拒绝 §8.2、interop 检测为真时 available=True）——**不再恒为 False**。⚠️ COM 走查层与 interop 探测的真实 Windows 验证待硬件（§11 第 9 条「接口层存在」已满足）；测试以走查层假数据注入覆盖数据整形与门禁判定 | ✅ |
| P8-02 | NVDEC decode backend（在既有 `hwaccel.py` 之上扩展） | `windows_hw/`、`src/shared/media/hwaccel.py` | NVDEC frame source | H.264/HEVC/AV1 能查询 codec/bit-depth/resolution 能力 | L |
| P8-03 | CUDA compute backend | `windows_hw/cuda_compute.*` | CUDA kernel dispatch | 可运行 resize、YUV conversion、denoise probe | L |
| P8-04 | D3D11/D3D12 interop | `windows_hw/d3d*_interop.*` | shared texture contract | 无不必要 CPU download | L |
| P8-05 | CUDA/D3D fence | native layer | sync token | CUDA 与 graphics 不发生未同步读写 | M |
| P8-06 | NVENC export path（后续） | `src/shared/export/`、native layer | GPU decode→GPU encode | 转码场景不回落 CPU | L |
| P8-07 | 与 OpenGL 对比 | Phase 0 harness | NVIDIA A/B report | OpenGL、D3D、CUDA 组合的收益可分段解释 | M |
| P8-08 | **为 `hw_planar_source.py` 补测试** | `tests/media/` | 测试 | ✅ **已完成**（2026-09-11）：`test_windows_hw_planar_source.py` 12 用例——格式路由表、open 门控（无硬解/静默回退软件格式/无帧均显式拒绝）、NV12/P010 契约（data=None + 双平面 + `msb_aligned_p010` 元数据 + 颜色缺省）、硬解中途失效的父类兜底。真实硬解验证留待 Windows 机器 | ✅ |

不要默认 CUDA 一定是 NVIDIA Windows 的显示路径。第一阶段可以保留现有 OpenGL 做显示，让 CUDA/NVDEC 先用于解码或计算；正式零拷贝显示再选择 D3D11/D3D12。

### Phase 9：Windows Intel/AMD 与通用回退

| 平台 | DecodeBackend | ComputeBackend | GraphicsBackend | 优先级 |
|---|---|---|---|---:|
| NVIDIA | NVDEC | CUDA | D3D11/D3D12 或 OpenGL | 高 |
| Intel | QSV/oneVPL、D3D11VA | DirectML、OpenVINO 或 CPU | D3D11/D3D12 或 OpenGL | 中 |
| AMD | AMF、D3D11VA | DirectML、HIP（可选）或 CPU | D3D11/D3D12 或 OpenGL | 中 |
| 通用 Windows | FFmpeg hwaccel download | CPU | OpenGL | 必须 |

| 编号 | 工作项 | 交付物 | 验收标准 | 量 |
|---|---|---|---|---|
| P9-01 | Intel/AMD capability matrix | vendor/codec/format matrix | unsupported format 明确记录 | M |
| P9-02 | D3D11/D3D12 generic texture contract | D3D texture backend | CPU fallback 可用 | L |
| P9-03 | QSV/oneVPL adapter | Intel decode path | NV12/P010 路径可测 | L |
| P9-04 | AMF adapter | AMD decode/encode path | 至少硬解能力探测和回退 | M |
| P9-05 | DirectML optional compute | generic compute path | 不依赖 NVIDIA CUDA 的基础 GPU compute | L |
| P9-06 | multi-GPU adapter matching | device selector | decode GPU、graphics GPU 不一致时回退或诊断 | M |

### Phase 10：UI 表面迁移（v1 完全缺失，独立成阶段）

**目标**：解决 §2.3 的 GL 上下文共享组问题。**这是本计划的关键路径，不是细节。**

| 编号 | 工作项 | 主要文件 | 交付物 | 验收标准 | 量 |
|---|---|---|---|---|---|
| P10-01 | `.glo` 交接的后端无关化 | 42 个文件 / 227 处 | opaque handle 全量替换 | 不再有裸 GL name 跨模块传递 | **L（最大单项）** |
| P10-02 | `CAMetalLayer` 宿主方案定型 | native layer | 宿主 view 抽象 | 与 `QOpenGLWidget` 的共存策略确定（含 P1-03 结论） | L |
| P10-03 | 5 个 viewport 类的 Metal 对应实现 | `gl_viewport.py`、`composite_viewport.py`、`program_viewer.py`、`source_viewer.py`、`gpu_scope_widget.py` | Metal viewport | 逐个迁移；`CompositeViewer`（11,491 行）单独排期 | L |
| P10-04 | 控件层之外的 GL 直连清理 | 17 处 `from OpenGL import GL`、`src/features/composite/render/gpu_resource_pool.py:100,154,187,1063` | 后端化调用 | 无遗留直接 GL FFI | L |
| P10-05 | EDR/HDR 呈现验证 | native layer | 呈现报告 | HDR 元数据与色彩空间在 Metal 路径正确 | M |

---

## 6. 性能与画质对比体系

### 6.1 三种测试模式

| 模式 | 用途 | 是否适合测性能 |
|---|---|---|
| Exclusive A/B | OpenGL、Metal、D3D 分别独占运行同一帧范围 | 是，主性能模式 |
| Shadow compare | 同帧双渲染，比较输出像素 | 否，GPU 工作量会接近翻倍 |
| Session switch | 用户切换 backend，验证重建和回退 | 只测切换开销与稳定性 |

### 6.2 必须记录的指标

```text
环境：OS、GPU、GPU family、driver、应用 commit
输入：codec、pixel format、bit depth、resolution、fps、color metadata
配置：graphics_api、compute_api、decode_api、interop_api、zero-copy、quality tier
阶段：demux、decode、plane_prepare、import、upload、command_encode、GPU render、present
结果：p50/p95/max、effective FPS、late frames、dropped frames、queue depth、CPU%、memory、power
画质：max abs、mean abs、RMSE、HDR linear error、alpha error
```

**补充（v1 缺失）**：GPU 显存占用、shader 编译/pipeline state 创建时间、首帧延迟、scrub 延迟、持续降频时间。

### 6.3 公共 benchmark 命令形态（修正为四维）

```bash
python tools/benchmark_backend_compare.py \
  --graphics-a opengl --graphics-b metal \
  --compute-a glsl_fragment --compute-b metal_compute \
  --decode-a videotoolbox --decode-b videotoolbox \
  --interop-a iosurface_gl --interop-b cvmetal \
  --file input.mov --frames 120 --warmup 30 \
  --verify-color \
  --output tests/media/results/backend_compare.json
```

工具应支持：

- `--render-only`：使用同一组 GPU resident input，比较图形/计算后端。
- `--pipeline`：各自运行完整 decode→render 链路。
- `--shadow`：同帧双渲染，仅用于画质验证。
- 多次重复并输出中位数和 p95，避免单次结果误导。

> **v1 写作 `--backend opengl|metal|d3d11|d3d12|cuda|cpu`，与 §1.2 的四维拆分自相矛盾，此处修正为四组独立 flag。** 注意该工具与 `--backend` flag 目前**均不存在**（全仓库 0 处）。

### 6.4 性能门禁

迁移一个模块后至少通过：

| 门禁 | 初始标准 |
|---|---|
| 正确性 | OpenGL/Metal 参考帧误差在批准阈值内 |
| zero-copy | 声明 zero-copy 时无 CPU download/upload，**且能力判定不得仅依赖环境变量（见 P0-06）** |
| 性能 | 迁移后目标场景总帧 p95 不得比 GL 基线退化超过约定阈值 |
| 播放 | 无倒退帧、无黑屏、无异常尺寸变化 |
| 生命周期 | 30 分钟播放无资源持续增长，seek/取消/关闭无泄漏 |
| fallback | probe、格式、驱动失败时自动回退并记录原因 |
| 插件 | 不支持的 effect 明确诊断，不静默丢效果 |

Metal 的绝对加速比不预设。必须分别报告：

```text
Metal render-only speedup
Metal end-to-end speedup
CPU reduction
upload elimination
dropped-frame reduction
power/thermal change
```

**数值门禁必须在 ADR 中冻结**（参照 `native/topos_codec/docs/ADR-C026-m8-acceptance-gates.md` 把 G1–G6 与「达成/未达成」判定写死在同一张表的做法——该 ADR 明确记有「达 V1-kernel（G1+G2）但未达 ProRes 正式门（G3/G6 未过）」的中间态，正是本计划需要的形态），否则出现「报告未达而总表完成」的状态。

---

## 7. 资源、线程和同步约束

### 7.1 线程边界

```text
UI thread
  只更新状态、发信号、提交 command

Decode thread
  只产生 CPU frame 或 retained GPU surface descriptor

Backend render thread
  唯一执行 OpenGL/Metal/D3D/CUDA resource binding 和 command encoding 的线程
```

Metal、D3D、CUDA 的 native 对象不能被 UI 线程直接访问。OpenGL 现有规则继续有效（`.clinerules:7`、`docs/taep_engineering_conventions.md:88-89`「UI 线程不得调用 ModernGL」）。

### 7.2 生命周期

```text
acquire
 → decode/import
 → renderer owns lease
 → encode GPU commands
 → submit fence
 → fence complete
 → release CVPixelBuffer/MTLTexture/D3D/CUDA resource
```

要求：

1. 资源 ring buffer 有界，建议初始为 3-5 帧。
2. 被丢弃的 GPU frame exactly once release。
3. 不使用每帧 `glFinish()` 或全局 GPU 阻塞来掩盖同步问题。
4. backend 切换必须先 quiesce，再销毁资源。
5. `__del__` 不直接执行可能包含 GPU API 的 release callback。
6. **（新增）present 生命周期复用 `present_frame.py` 的 `TextureLease` 状态机**（`FREE→WRITING→READY→IN_VIEWER→RELEASABLE→FREE`），不新建第二套。

---

## 8. 失败与回退策略

### 8.1 Metal

| 失败点 | 回退 |
|---|---|
| Metal device 不可用 | OpenGL |
| CVMetalTextureCache import 失败 | Metal CPU upload 或 OpenGL VT bridge |
| NV12/P010 格式不匹配 | CPU planar + Metal upload，或 OpenGL |
| ProRes surface 不满足条件 | R16UI/R8 upload + proxy（**本机常态，见 §0.1**） |
| Metal shader 编译失败 | OpenGL；报告 shader/effect ID |
| GPU memory/fence 异常 | 丢弃当前帧并重建 backend，必要时回 OpenGL |
| Metal compute 不可用 | **既有 fragment 回退 + `_fallback_callback` 通知**（勿新建通道） |

### 8.2 Windows

| 失败点 | 回退 |
|---|---|
| NVDEC 能力不足 | D3D11VA/QSV/AMF 或 FFmpeg CPU |
| CUDA 不可用 | DirectML/CPU/OpenGL shader |
| D3D adapter 与 decode adapter 不一致 | 选择统一 adapter 或 CPU fallback |
| D3D interop 失败 | D3D copy、OpenGL upload 或 CPU |
| 驱动版本不满足 | capability gate 拒绝专属路径 |

所有回退路径都必须保留正确性和可观测性，不能把"没有丢帧"作为唯一成功标准。

---

## 9. 交付工程化（v1 完全缺失）

### 9.1 打包与签名

Metal 原生扩展进入分发需要：

| 项 | 要求 | 现状 |
|---|---|---|
| 构建 | CMake/Obj-C++，进 `packaging/pyinstaller/base_spec.py` 的 `binaries` | 目前只打包 `libtopos_codec`（`base_spec.py:162-165`） |
| 架构 | universal2（arm64 + x86_64） | 未定 |
| MSL | 构建期预编译 `.metallib` 或随包发布源码 | 未定 |
| 签名 | 代码签名 + hardened runtime | 未定 |
| 公证 | notarization（macOS 分发必需） | 未定 |
| 回退 | 扩展缺失时程序仍可启动并回退 OpenGL | 必须 |

**K4 中止条件即源于此**：若签名/公证在目标分发渠道不可行，Metal 主线应中止。

### 9.2 依赖

`pyproject.toml [macos]` extras 当前只含 `pyobjc-framework-VideoToolbox`、`-CoreMedia`、`-Quartz`、`-IOSurface`（`:48-57`）。

需要新增并**固定版本**：

```text
pyobjc-framework-Metal
pyobjc-framework-MetalPerformanceShaders（若使用 MPS）
```

本机恰好装有 pyobjc 12.1 才能通过 probe，**不可依赖环境偶然性**。

### 9.3 CI

**现状：`.github/workflows/` 共 7 个 workflow，`grep "bench|playback"` → 0 命中。§6.4 的七条门禁目前无自动化落点，只能人工执行。**

需要新增（参照既有 `html-renderer-gate.yml` 的形态）：

| 编号 | 工作项 | 验收标准 | 量 |
|---|---|---|---|
| P9C-01 | 渲染/GPU 门禁 job（macOS GPU runner） | 至少跑通画质 golden 与 zero-copy 断言 | L |
| P9C-02 | 门禁产物按项目约定输出 `.md` + `.json` 配对 | 与 `docs/codec/preview_decode_gate_*.md/.json` 同构 | S |
| P9C-03 | benchmark 结果随 commit 归档 | 可追溯，支持跨 commit 比较 | M |

### 9.4 ADR 与文档

本计划涉及**永久双后端架构**与**跨平台 GPU 契约**，属于应用级架构决策，必须落 ADR。

| 编号 | 工作项 | 交付物 | 验收标准 | 量 |
|---|---|---|---|---|
| P9D-01 | 撰写 `docs/adr/0003-*.md` | ADR | 记录：四维接口决策、被否决方案（Qt RHI 单路径、Vulkan/MoltenVK、彻底重写）及理由、K1–K5 中止条件、冻结的数值门禁 | M |
| P9D-02 | 声明与既有计划的关系 | 更新 §1.3 对应文档 | ✅ **已完成**（2026-09-11）：`4k-playback-optimization.md` §5.5 方向抉择与 §9 优先级两处加状态更新——v2 接续「后端抽象」方向并修正排序（Metal compute 提前、零拷贝条件化、Qt RHI/Vulkan 否决） | ✅ |
| P9D-03 | 补 `module_boundary_rules.md` 的平台/后端代码归属规则 | 规则文档 | ✅ **已完成**（2026-09-11）：新增「Platform / Backend Code Ownership」章节——`src/shared/gpu/`（含 `metal/`）放后端无关契约与平台基础设施；包装 feature renderer/资源的具体后端实现必须放 feature 侧（`opengl_render_backend.py`、`metal_compute_bridge.py`）；平台解码/interop 归 `src/shared/media/<platform>/` | ✅ |
| P9D-04 | 本计划入库 | git | 已提交（`docs(gpu): Metal 与跨平台 GPU 后端迁移计划 v2`）；**审计基线 `a3064858` 之后的行号会随后续改动漂移，引用以符号名为准** | ✅ 已完成 |

**ADR 事实格式参照**（`docs/adr/` 目前只有 0001/0002，无模板无索引；codec 序列 `native/topos_codec/docs/ADR-C###` 有更成熟形态）：标题 + `Status/Date/Plan ref/Supersedes/Implementation` + `Context/Decision/Consequences/Support matrix` + **证据表**。

---

## 10. 风险和明确不做事项

### 10.1 主要风险

| 风险 | 影响 | 控制措施 |
|---|---|---|
| **GL 上下文共享组无法与 Metal 共存** | **极高** | Phase 1 P1-03 先实测；Phase 10 独立排期；K3 中止条件 |
| GLSL/插件机制依赖文本正则改写 | 高 | `EffectSpec` 升为权威输入 + 结构化 uniform 绑定（P7-01/P7-02） |
| `composite/core/shaders.py` 单文件 102 个 shader | 高 | 单独排期，逐个登记覆盖率 |
| **CPU 解码瓶颈被错误归因给 GPU** | 高 | §0.3 实测；Metal 不承诺改善 CPU 解码 |
| **调色成本被反向错误归因（认为"调色不重要"）** | 高 | §0.3.1 实测：4K 单层全工具调色 +6.91ms，是解码的 38%。**两个方向的错误归因都要防** |
| **ProRes 在本机无 VT 硬解** | 高 | Phase 5 条件化；遵从 `playback_should_prefer_ffmpeg()` |
| **macOS 上 compute 类能力缺失且部分静默**（Normalize 百分位退化为空操作，§0.4） | 高 | Phase 4 立项依据；P4-03 要求"无法支持时必须明确诊断" |
| 目标硬件为 Intel 末代平台 | 高 | §0.2 阻塞性决策 |
| 多 GPU adapter 不匹配 | 中 | 启动时锁定 device，记录 vendor/adapter |
| Python per-frame native 调用开销 | 中 | Python 做编排，native 扩展承担热路径 |
| Metal/Windows 后端功能分叉 | 高 | 共享颜色语义、golden tests、统一 capability matrix |
| 打包/签名不可行 | 中 | Phase 1 预判；K4 中止条件 |

### 10.2 第一阶段不做

1. 不删除或重写现有 OpenGL/ModernGL 后端。
2. 不立即把 Qt Widgets 全部改成 Qt RHI/Metal。
3. 不优先迁移 `native/topos_codec` 的 CPU 编解码算法到 GPU。
4. 不在没有 benchmark 和画质 reference 的情况下修改核心 shader 语义。
5. 不把 CUDA 写成所有 NVIDIA/Windows 的唯一方案。
6. 不在一条播放链路里无边界地混合 OpenGL texture、Metal texture、D3D texture。
7. **（新增）不在 §0.2 决策完成前启动 Phase 4 之后的工作。**
8. **（新增）不重新实现已有的 `present_frame.py` lease 协议、`EffectSpec`、`EffectCapability`、`DecoderBackend` 模式与 `hwaccel.py`。**

---

## 11. 最终验收定义

当以下条件全部满足时，才允许把 macOS 默认后端切换为 Metal：

1. OpenGL/ModernGL 后端仍然可以通过强制模式启动并完成完整 Color/Edit/Composite 基本流程。
2. **Phase 1 的 K1/K3/K4 门禁全部通过，且结论已写入 ADR。**
3. Metal 至少覆盖：VT 硬解格式的 NV12/P010 视频、YUV 转换、Raw Preview、基础调色、LUT、**以及 Phase 4 的 compute 类效果（大半径模糊/腐蚀膨胀、Normalize 百分位直方图）**。
4. OpenGL 与 Metal 的 SDR/HDR/10-bit 画质对比通过。
5. Metal zero-copy 路径无 CPU download/upload 证据，**且能力判定来自真实探测而非环境变量**。
6. 4K 目标素材的 p95、掉帧、CPU 占用达到项目门禁（§5 Phase 0）。
7. seek、clip switch、取消、窗口关闭、backend 切换无黑屏和资源泄漏。
8. 不支持的插件和像素格式有明确诊断和可靠回退。
9. **Windows 的 graphics/compute/decode/interop 四类接口已经存在（接口层，非实现完成）；Intel/AMD 的正式实现可分阶段完成。**

> **v1 的第 8 条与 §10 顺序矛盾已修复**：原第 8 条要求「Windows 四类接口已经存在」才允许 macOS 切 Metal，但 Phase 7/8 排在 Phase 6 之后，永远无法在该时点满足。现改为「接口层存在」（由 Phase 2 交付，早于 Phase 6），与顺序自洽。

---

## 12. 近期执行顺序

| 顺序 | 任务 | 结果 |
|---:|---|---|
| 0 | **完成 §0.2 目标平台决策** | ✅ 完成（2026-09-11：**A′ 工作决策采纳**，见 §0.2 决策记录；Apple Silicon 基准机到位前，性能门禁在本机执行、仅作不退化判定） |
| 1 | Phase 0：复用既有 harness，补四维字段 + 修 P0-06 诚实性缺口 | ✅ 完成（2026-09-11，基线 `p0_opengl_baseline.md`） |
| 2 | **解决基准素材来源（P0-03）** | ⚠️ 部分完成：合成 4K fixture 已建、**真实 4K 素材托管决策仍待定** |
| 3 | Phase 1：独立 probe（P1-01…P1-06）+ K1/K3/K4 判定 | ✅ 完成（2026-09-11，K1/K3 不触发、K4 条件性通过——ADR 0003） |
| 4 | Phase 2：契约冻结（probe 通过后） | ✅ 完成（2026-09-11，四维接口 + opaque handle + selector） |
| 5 | Phase 3：封装现有 OpenGL 后端 | ✅ 完成（2026-09-11，P3-01~05 全部：facade + 资源台账 + orchestrator 钉死 + 旧入口兼容） |
| 6 | **Phase 4：Metal compute 能力解锁** | ✅ 完成（2026-09-11，P4-01~05：MetalComputeContext + MSL kernel 集 + Normalize 解锁 + 统一回退 + A/B 报告；K5 未触发；模糊路径按实测默认门控） |
| 7 | Phase 5：VT 硬解格式的 Metal 零拷贝（条件化） | ✅ 完成（2026-09-11，P5-01~08：CVMetalVideoBridge + MSL 转换 + ring + benchmark 归档 + 开发者门禁；ProRes 按 §0.1 实测显式拒绝；K2 端到端对比留待与 Phase 6 渲染接入后合并评估） |
| 8 | Phase 6/7：调色链路与 shader/插件 | 🔶 进行中（2026-09-11）：P6-01 转换 kernel 随 Phase 5 交付、P7-05 完成（死常量删除 + MSL 集合随 P4）、**P7-06/07/08 完成**（EffectCapability 后端轴 + manifest 声明 + 会话后端诊断）、**P6-06 完成**（film grain/soft clip MSL 内核 + numpy 参考 + 固定 seed 实测契约，30 测试）、**P6-07 审计判定验收达成**（GPU scope 管线无 CPU readback，Metal 消费被实测否决）、**P6-05 审计改判**（denoise 与 Phase 4 内核无语义交集，归入 P6-02~04）；**P7-01/P7-02 完成**（编译入口 EffectSpec manifest 权威化 + 结构化 uniform 绑定：单一 manifest 驱动 GL 声明与上传、MSL struct 布局就绪；G1 24 条基线两批位级一致；死链裁决 + 串参数 bug 修复，台账 0004a 登记）；**P7-03/04 + P6-02~04 的前置 ADR 已冻结**（`docs/adr/0004-*.md`：G1~G5 门禁 + 台账 `0004a`），实施按效果逐个推进；**P7-03 SNIPPET 翻译全部完成**（10 个翻译 + MASK_SNIPPET 死代码删除；grading/curves/hsl_curve/lut3d/mixer/warper/blur_sharpen 抖动噪声门/blend_modes 19 模式/soft_clip/film_grain fragment 形态，G2 实测通过，台账 `msl_verified`；qualifier 归属 matte 生成侧），仅剩插件 manifest MSL |
| 9 | Phase 10：UI 表面迁移 | 解除 share group 阻塞 |
| 10 | Phase 8/9：Windows NVIDIA / Intel / AMD | 🔶 进行中（2026-09-11）：**P8-01 接口层完成**（DXGI 探针 + 数据驱动门禁，真实 Windows 验证待硬件）、**P8-08 完成**（hw_planar_source 12 测试）；P8-02~07 / P9-01~06 为后续大项——其中 P8-04 interop 与 P8-02 NVDEC 需 Windows 硬件，本机仅可做契约/测试层。**执行细化已独立成文：`docs/windows_optimization_plan_2026-09-11.md`（W0~W7 阶段 + 本机/真机边界 + K-W 门槛；其 W0 五项与 W7-02 CI 无需 Windows 硬件，可先行）** |

**注意：本表的第 9、10 步有意把 Phase 10（UI 表面迁移）排在 Phase 8/9（Windows）之前**，与 Phase 编号顺序不同。理由：Phase 10 决定 macOS 主线能否成立（K3），而 Windows 后端应复用已稳定的跨后端契约。**这不是笔误，是排序决策。**

**第一阶段完成之前，`src/main.py` 中的全局 Qt OpenGL 设置（`:170-171`、`:200`）、现有 `QOpenGLWidget` 和 ModernGL 资源生命周期不应被改动。**

### 12.1 实现后复核（2026-09-11 第二轮，隐藏 bug 审计）

对 20 个实现提交的全部生产代码（~4,400 行）做过一轮完整复查，发现并已修复 6 处隐藏问题（修复均附回归测试）：

| # | 问题 | 位置 | 修复 |
|---|---|---|---|
| 1 | **遥测回归（P3-04 落地引入）**：`_debug_frame_path` 是自由格式诊断串（`'unknown'`/`'planar_yuv_r8_upload_gpu_failed'`），喂给严格枚举解析 `from_serialized` 抛 ValueError 被外层 except 吞掉 → `track_frame_path`/`track_zero_copy_frame` 整段静默跳过 | `async_renderer.py` 渲染遥测点 | 新增 `RenderFramePath.try_from_serialized`（未知值返回 None），遥测一律走安全解析 |
| 2 | **GL 版本解析脆弱**：Mesa 风格 `"4.6 (Core Profile) …"` 使 `int(split('.')[1])` 抛 ValueError → GL 4.3+ 机器被判不可用且跳过试编译（静默能力降级）；旧代码在 macOS 上恰因该异常吞掉而"碰巧正确" | `histogram_manager._check_availability` | 与 `ComputeShaderManager` 同规则的正则解析；版本可解析且 <4.3 时直接 False 不再试编译 |
| 3 | **CDF 缓冲未初始化**：`create_buffer` 返回未初始化内存，mask 外通道 kernel 提前 return 不写 → 读回 GPU 垃圾，破坏 `cdf.sum()==0 → (0,1)` 兜底语义（新分配页常为零，故测试长期绿灯——潜伏而非确定性） | `_MetalHistogramPath.compute` | cdf 缓冲零初始化 + 契约回归测试（mask 外通道 cdf 必须精确为零） |
| 4 | **dispatch 部分持锁**：编码器创建/绑定在锁外，双线程可交错挂 encoder 或让 `finish()` 提交他人仍在编码的 buffer（Metal 校验错误）；桥的线程安全声明与实现不符 | `MetalComputeContext.dispatch` | 编码全程持锁（微秒级无性能影响）；编码异常时弃置半编码 buffer（未 endEncoding 的 encoder 之后无法再挂新 encoder） |
| 5 | **`create_texture_from_numpy` 三重坏死**：调用了 MTLDevice 上**不存在**的 selector（`newTextureWithDescriptor_slice_bytes_bytesPerRow_`）+ Shared 纹理存储在独显 macOS 非法 + 全仓库零调用方——从未执行过故未暴露 | `MetalComputeContext.create_texture_from_numpy` | 改为 `newTextureWithDescriptor_` + `replaceRegion` 上传；存储模式按 `hasUnifiedMemory` 选 Shared/Managed；补真机回归测试（本机 AMD 6900XT 验证通过） |
| 6 | **过时注释**：capabilities 写"Metal compute 在 Phase 4 落地前报不可用"——Phase 4 已落地，运行时真值是按效果分别门控（模糊默认关、Normalize 默认开），单一 DimensionCapability 表达不了该拆分 | `capabilities.detect_compute_backend` docstring | 注释改为描述 Phase 5+ 统一门控前的现状（无行为变更） |

复核同时确认无问题的关键点：`acquire_texture` 池默认 `dtype='f2'`（下采样格式正确）；`GpuFramePlane.__post_init__` 校验不破坏唯一生产者（`api='opengl'`）；导出渲染器为持久实例（Metal kernel 编译非每帧成本）；`vt_hardware_decode_available` 签名匹配；blur/形态学 kernel 的 tile 越界与哨兵解码逐条核对一致。

### 12.2 时间线导出选择 Metal（2026-09-14）

偏好设置的「时间线渲染输出」现在支持 Auto、OpenGL、Metal。Auto 仍走现有 OpenGL 路径；显式选 Metal 时，导出器读取 `backend.graphics` 设置并使用 Metal compute renderer。此改动只选择时间线导出渲染后端，不更改预览视口，也不更改解码器或编码器选择。

`backend.graphics` 同时供共享后端诊断读取；它报告通用 graphics capability，而 `TimelineExporter` 启动日志 `Timeline export graphics backend` 报告本次导出的实际选择。Auto 对时间线导出明确采用 OpenGL，以免缺少完整覆盖的 Metal 路径被自动启用。

Metal 时间线 renderer 当前支持普通 video/image 层的 packed BGR、CPU planar YUV 和 VideoToolbox NV12/P010 Metal texture；源转换、primary grade、缩放、opacity、normal 多层混合、cross dissolve、dip-to-color 及可选的 BT.709 limited-range YUV420P 输出在 Metal 上执行。单个 clip 的 Box/Gaussian Blur（每轴 half-size ≤16、RGB 全通道）、当前 CPU 导出语义下的 Sharpen（高斯核 half-size ≤16）和 Glow（亮部阈值、高斯扩散、screen 混合；quality fast/normal/high 对应 8/16/32 samples）进入 MSL。此前多效果链会整帧回退；2026-09-14 起，单独受支持的 Blur、Sharpen、Glow 可按时间线顺序在 Metal buffer 上串联，效果之间不做 CPU 回读。更大的 Blur/Sharpen 卷积核和其它 Blur 变体仍回退 CPU。radius 大于 5 时，MSL 先做横向卷积，再在纵向采样/合成阶段完成滤镜；half-float 中间缓冲按层栈复用。真实 1080p→720p 120 帧 NV12 radius 16 Gaussian Blur 完整导出由单次二维卷积的 5.4 FPS 提升至 31.8 FPS（120/120 帧 Metal、零 fallback）；radius 6 与 radius 8 Gaussian Blur 分别为 31.4 与 30.9 FPS，Sharpen radius 16 为 31.7 FPS，Glow radius 16 为 29.6 FPS。相同素材、输出规格、Glow 参数和 VideoToolbox 硬件编码器的 CPU compositor A/B 为 7.49 FPS / 16.02 秒；Metal 为 29.65 FPS / 4.05 秒，约 4 倍吞吐。真实 P010 radius 16 Blur 与 Sharpen 分别为 29.7 与 30.4 FPS；以上完整导出均为 120/120 帧经 Metal、零 renderer error/fallback。新路径与 CPU 参考帧的画质对比平均误差低于 0.75/255，最大误差不超过 3/255；真实 NV12/P010 纹理上的 radius 5/16 Blur、radius 1/16 Sharpen 和 radius 16 Glow 也经过 CPU 对照回归。macOS 上存在 VT/CoreVideo Metal bridge 时，NV12/P010 解码纹理直接交给 MSL，不经过 CPU 像素回读或重新上传。P010 full-range CoreVideo FourCC 为 `xf20`（video-range 为 `x420`）；二者现都映射到 Metal bridge，full-range 还通过合成 CoreVideo buffer 的真机导入与画质测试。导出器仍将最终 BGR/YUV 平面读回，交给 FFmpeg/VideoToolbox 编码；尚未实现编码器原生 IOSurface 输入，因此还不是解码到编码的全程零拷贝。

（下列是 §12.2 最初实现时的覆盖状态；§12.5–12.7 已扩展效果覆盖，空间转场的当前状态见 §12.13。）不在当时 Metal 子集内的效果类型/参数、裁切、视频降噪、非 identity transform、frame-blend retime、空间转场、非 normal blend、alpha 或高精度输出仍回到 CPU 合成，并记录 Metal fallback 原因。当时 CPU effect export 只实现 blur、sharpen、glow；不能据此视为后续章节迁移的效果仍未覆盖。

本机真实素材验证使用 `bench_out/metal_timeline_export/run_real_h264_ab.py`，输入来自 `tests/test_videos`，输出为 1280×720 H.264、VideoToolbox hardware encoder。120 帧 1920×1080 H.264 NV12 的 Metal 无效果导出为 35.1 FPS / 3.42 秒，120/120 帧且 120 帧都经过 Metal 零拷贝输入；同一时间线 OpenGL 为 26.6 FPS / 4.50 秒。60 帧双轨 normal 混合 Metal 导出为 19.8 FPS，60/60 帧通过。120 帧 2K H.265 Main10 P010 导出为 34.9 FPS，120/120 帧通过。新一轮真实 NV12 Gaussian Blur 120 帧导出：radius 5 为 30.1 FPS / 3.99 秒，radius 6 为 27.4 FPS / 4.38 秒；Sharpen radius 6 为 27.9 FPS / 4.31 秒。三组均为 120/120 帧走 Metal、无渲染错误或 fallback；radius 6 因此纳入当前原生上限。先前 CPU-only Blur radius 5 为 11.2 FPS / 10.73 秒。真实 NV12 Sharpen radius 1 的既有基准为 35.8 FPS，120/120 帧通过。真实 NV12 原始帧输出相对 CPU 解码参考的误差平均为 1.48/255、99 百分位为 7；Metal Gaussian Blur 与 Sharpen 对 CPU 效果参考的平均误差均低于 2/255、99 百分位不高于 8。P010 MSL 输出对独立 FFmpeg 10-bit YUV 参考最大误差为 1 code value。

同输入、scale、BT.709 元数据、VideoToolbox encoder 和约 2.4 Mbit/s 码率的直接 FFmpeg 命令用时 0.86 秒（约 140 FPS）。4K 实测仍有明显差距：Topos Metal 导出 30 帧约 5.8 FPS，直接 FFmpeg VideoToolbox 同尺寸导出约 23 FPS。当前 720p blur/sharpen 工作集已达到 25fps 时间线速度，但高分辨率和复杂多轨还未接近 FFmpeg；逐帧同步/读回、Metal kernel 吞吐和导出固定开销仍是主要优化方向。Metal 已比本机当前 OpenGL 时间线导出快约 32%；Auto 继续选择 OpenGL，直到更多时间线形状和效果都有代表性 A/B 结果。

### 12.3 多效果链与吞吐复测（2026-09-14）

（本节效果清单是早期阶段快照；后续迁移见 §12.5–12.7。）Metal 当前支持的 Blur、Sharpen、Glow 组合可按 `EffectInstance.order` 排列执行。首个 compute kernel 在 GPU 内完成 VideoToolbox NV12/P010 或 planar/packed 源转换和 primary grade，随后两个 BGR8 Metal buffers ping-pong 承接各效果，最终仍留在 GPU 做层合成与 YUV420P 输出；效果链中间没有 CPU 读回。Effect Catalog capability 仅声明已有 Metal 导出实现的效果，不会把未迁移效果或插件误报为 Metal 已支持。

真实链路 A/B 使用 `tests/test_videos/2K_h264.mp4`，1920×1080 H.264 经 VideoToolbox NV12 texture 解码、缩放至 1280×720，VideoToolbox 硬件编码：120 帧 Blur(radius 3、mix 0.8)→Sharpen(radius 1、amount 0.8)→Glow(fast、radius 3、intensity 0.4、mix 0.7) 导出 4.410 秒（**27.213 FPS**），120/120 帧通过 Metal，无错误与 fallback。相同效果链的 CPU compositor 为 34.696 秒（**3.459 FPS**），Metal 吞吐约 **7.9 倍**；两者输出均核对为 120 帧。MSL 链式帧与按相同顺序执行的 CPU 效果参考通过像素回归，真实 NV12 texture 链也通过独立参考测试。

同一真实输入和 1280×720 VideoToolbox 编码下，480 帧无效果 Metal 导出用时 3.378 秒（**142.081 FPS**），480/480 帧使用 Metal texture、无 fallback；对应直接 FFmpeg scale + VideoToolbox 导出用时 1.55 秒（约 **310 FPS**），输出也核对为 480 帧。两者的编码质量参数和像素舍入路径并非逐项一致，因此该比较用于吞吐量级判断：当前 Metal 时间线全链路仍约为直接 FFmpeg 的 46%，还没有达到硬件极限。480 帧 Metal renderer 本身累计耗时 0.460 秒（含 dispatch/sync 与最终共享 buffer 读取），当前下一项瓶颈应优先查逐帧 CPU 编排、VideoToolbox 帧交付和逐帧同步；最终 Metal→CPU 编码平面读回仍在。

后续相同机器的 1920×1080 原尺寸测试使用同一 H.264 输入和 480 帧：Metal 完整导出 4.270 秒（**112.404 FPS**），480/480 帧输入为 Metal texture、GPU 成功 480 次、renderer errors/fallback 均为 0；直接 FFmpeg + VideoToolbox 用时 2.34 秒（约 **205 FPS**），Topos 约为直接导出的 55%。Metal GPU render 累计 0.705 秒，约占总墙钟的 16.5%，因此单纯继续压缩 shader 算术不会消除当前主要差距。P010 输入另以 `2K_H265.mov` 导出 120 帧：Metal 端到端 2.624 秒（45.729 FPS），120/120 帧走 Metal texture；相同分辨率的直接 FFmpeg 为 1.19 秒（约 101 FPS）。短测包含编码器开关固定耗时，仅用于同样帧数的端到端比较。

Blur 的 clamp/wrap/mirror 现在由 Metal kernel 消费，CPU fallback 也按同一参数处理；Box Blur 的 wrap 采用周期 padding（OpenCV box filter 不接受 `BORDER_WRAP`）。新增半径 8 的边界像素 CPU/Metal 回归，覆盖 separable 水平/垂直路径。将半径 3 Blur 强行改成两次 separable dispatch 的真实测试从 36.68 FPS 降至 34.49 FPS，故保留小半径 direct kernel、只对 half-size 大于 5 的核拆分；这是本轮实测后撤回的优化尝试。

效果链 direct kernel 不再逐像素计算 Gaussian `exp()` 权重，改为每帧在 CPU 侧生成一次并传入 Metal buffer。真实三效果链 120 帧连续两次为 27.032 / 27.069 FPS，GPU render 计时 2.343 / 2.338 秒；边缘修复后、权重预计算前的一次相同链测为 26.242 FPS / 2.381 秒，当前约有 3% 的初步提升。样本数量较少，性能收益需要后续多轮长测确认；画质与运行路径回归已通过。

### 12.4 独显中间缓冲与核函数开销复测（2026-09-14）

当前基准机为 MacPro7,1、AMD Radeon RX 6900 XT（16 GB VRAM、Metal 3）、8 个物理 CPU 核。此前 timeline 合成累积、效果链 ping-pong 和水平卷积 scratch 都使用 CPU 可见的 Shared `MTLBuffer`。现在只把 GPU 内部中间资源切到 Private storage；CPU 上传的素材/参数和最终交给编码器的输出仍保持 CPU 可访问。MSL 卷积循环也按实际 radius 迭代，避免 radius 1–5 每个像素空转 33 或 65 次循环。OpenCV 的 sigma=0 小高斯核（radius 1–4）已和 Metal 对齐为相同离散系数；CPU 参考对每种尺寸/核/边缘策略预热一次，规避本机 OpenCV 首次优化派发错误采用 border mode 的不稳定行为。

同一真实 H.264 输入 `tests/test_videos/2K_h264.mp4`、480 帧、Metal NV12 纹理解码、1280×720 输出、VideoToolbox 硬件编码，Blur(radius 3、mix 0.8)→Sharpen(radius 1、amount 0.8)→Glow(fast、radius 3、intensity 0.4、mix 0.7) 效果链连续两次分别为 **124.734 FPS / 3.848 秒**与 **124.259 FPS / 3.863 秒**；Metal renderer 分别为 **1.258 秒**与 **1.273 秒**，480/480 帧 Metal 合成成功、无错误/fallback，输出均为 480 帧 H.264。改动前同一长测为 42.296 FPS / 11.349 秒，GPU render 9.117 秒。新旧两份成片逐帧解码比较为 480/480 完全相同（平均/最大 code-value 差均为 0），因此这是存储位置优化，没有改变效果结果。

同一素材 480 帧、1920×1080、无效果 Metal 导出为 **112.783 FPS / 4.256 秒**，Metal renderer 0.711 秒，解码 1.991 秒，零 fallback。直接 FFmpeg + VideoToolbox 对同输入缩放至 1280×720 的无效果基线为 313.34 FPS；该值与三效果输出仅作吞吐参照，滤镜工作量不同，不能视为同条件速度比。此前同分辨率 1080p 直接 FFmpeg 无效果基线约 205 FPS，Metal 的无效果时间线约为其 55%；复杂三效果链现在约 124 FPS，但整体仍未达到机器吞吐上限。当前 Metal 效果链瓶颈已从 Shared 中间资源迁移到滤镜计算、逐帧 decode/encode 编排及最终 CPU 可见输出，后续需要继续拆分计时与减小同步/读回成本。

### 12.5 Erode/Dilate 时间线 Metal 接入与真实素材复测（2026-09-14）

时间线 Erode/Dilate 已接入 Metal 单效果和有序效果链。MSL 对 `channels=all`、`edge_mode=extend` 执行逐通道 min/max；单效果可直接读取 VideoToolbox NV12/P010 Metal textures，多效果链先在 GPU 上 materialize 到私有 BGR buffer，再使用私有 A/B ping-pong。半径大于 1 时采用水平形态学 pass 加垂直/合成 pass；多次相同操作折合为半径 `(size // 2) * iterations` 的方形邻域。测试验证了该折合与重复 CPU 形态学逐像素相同。

Metal 参数门控现覆盖 schema 内整型 `size=1..50`、`iterations=1..10`（最大合成半径 250），支持 `channels=all` 与 `edge_mode=extend/reflect/wrap`。时间线 opaque BGR 的 alpha-only 和 size 1 是 identity。效果能力表现已将该效果列为 CPU/Metal 可导出；预检检查可导出类型，运行时仍按每帧参数检查 Metal 子集，不支持参数不会静默跳过。Erode/Dilate 仍无 CPU 预览实现，预览覆盖不因这次时间线导出改动而改变。

像素回归覆盖了真实 `tests/test_videos/2K_h264.mp4` 的 NV12 和 `2K_H265.mov` 的 P010 Metal textures：erode size 3/1 iteration、dilate 偶数 size 4/2 iterations、单效果边界输出，以及 Blur → Erode → Sharpen texture-backed Metal 链；对同一 Metal 解码/颜色转换基准帧经 CPU morphology 生成的参考，平均误差小于 0.5 code value、最大差不超过 2；链式参考最大差不超过 3。另有 ndarray 输入、重复迭代与分离 pass 的严格逐像素回归。Metal 效果、能力/预检和时间线导出 focused suite 共 **152 项通过**；相关 CPU/GL 时间线导出回归 **97 项通过、5 项因环境条件跳过**。

真实导出基准输入为 1920×1080 H.264 NV12，输出为 1280×720 H.264/yuv420p，480 帧，VideoToolbox 解码与硬件编码，Metal `dilate size=5 / iterations=1 / channels=all`：两次总用时 **3.839 秒 / 125.036 FPS**、**3.817 秒 / 125.754 FPS**；每次 480/480 帧经过 Metal、480/480 使用 Metal 解码纹理、renderer error/fallback 均为 0。相同设置关闭 GPU compositor 的 CPU 输出耗时 **11.167 秒 / 42.982 FPS**，Metal 约 **2.9 倍**。作为吞吐参照，直接 FFmpeg 在 gbrp 上两次 3×3 dilation 后缩放并使用 VideoToolbox 编码 480 帧，软件解码为 **2.635 秒 / 182.2 FPS**；启用 FFmpeg VideoToolbox 解码则为 **3.606 秒 / 133.1 FPS**。此参考仅用于速度量级判断，RGB/YUV 转换和 timeline 编排不完全相同。Metal 仍低于软件解码 FFmpeg 参考；现有时间拆分显示 GPU render 总计约 0.82–0.88 秒，而完整导出 3.82–3.84 秒，后续瓶颈仍包括逐帧编排、解码/编码交付与最终读回。

以上是 §12.5 时的阶段清单；Edge Detect kernel_size 5/7、crop/仿射变换和 15 种非 normal blend 已在后续实现，详见 §12.12。当前剩余边界仍包括未提供 Metal kernel 的插件效果、alpha/高精度交付、frame-blend retime、空间转场、clip-level temporal denoise，以及 velocity-map Motion 输入。

### 12.6 Edge Detect 时间线 Metal Sobel 路径与速度对照（2026-09-14）

Edge Detect 时间线 Metal 当前覆盖 Sobel、Prewitt、Laplacian 和双阈值 Canny-like 输出的 3×3 核；含 `preblur=0..10`、亮度/R/G/B/Alpha 通道、方向/输出模式、反相、阈值和 output strength。kernel_size 5/7 仍走 CPU handler。Metal 边界采样为 clamp。CPU 与 Metal 的二值阈值比较加入 `1e-6` float32 tie tolerance，避免 GPU 融合乘加舍入在临界阈值翻转少量边缘像素；该值远小于 UI 阈值步长。

像素回归包括合成 BGR 帧下 edges/gradient/direction、两个方向分量、反相、完整有序效果链，以及实际 VideoToolbox NV12/P010 Metal textures；真实 NV12/P010 输入上的 edges 输出与从同一 Metal 颜色转换帧生成的 CPU 参考平均差不超过 0.1 code value、最大差不超过 1。对 float32 阈值边界加的 `1e-6` tie tolerance 消除了先前 NV12 测试里单个阈值相等像素的二值翻转。CPU/export、capability、预检聚焦测试 **86 项通过**；Metal Sobel 合成与链式测试 **4 项通过**；NV12/P010 真实纹理测试 **2 项通过**。最终合并的效果覆盖、Metal renderer、效果预检与时间线导出回归 **261 项通过、5 项因环境条件跳过**。

真实导出使用 `tests/test_videos/2K_h264.mp4`（1920×1080 H.264/NV12）、480 帧、输出 1280×720 H.264/yuv420p，Sobel 3×3 边缘检测后由 VideoToolbox 硬件编码。Metal 两次分别为 **3.629 秒 / 132.262 FPS**、**3.489 秒 / 137.593 FPS**，平均 **134.9 FPS**；两次均为 480/480 帧 Metal decode texture、480/480 GPU compositor success、Metal renderer 0 errors、无 fallback。最终 H.264 输出经 ffprobe 验证为 480 帧，解码抽样帧为灰度边缘图。相同设置 CPU compositor 用时 **39.303 秒 / 12.213 FPS**，Metal 约 **11.0 倍**。FFmpeg `edgedetect=low=0.1:high=0.3:mode=wires` + bilinear scale + VideoToolbox encode 的 480 帧参考用时 **23.629 秒 / 20.31 FPS**；FFmpeg 此处走软件解码/滤镜，且 `edgedetect` 算法与 Topos Sobel 阈值路径不同，只作工作量相近的吞吐参照。该特定滤镜负载下 Metal 高于 FFmpeg CPU filter；此前无效果直接 FFmpeg 仍明显快于 Topos 的端到端导出，不能把此结果外推为全项目都超过 FFmpeg。Metal render 累计约 0.84–0.86 秒，导出剩余时间仍主要消耗在解码、CPU/编码器帧交付、VideoToolbox 与读回。

### 12.7 Denoise 时间线 Metal 双边滤波与真实素材复测（2026-09-14）

时间线 Denoise 节点已加入 CPU 导出参考和 Metal 导出实现。Metal 覆盖节点 schema 中的 strength 0–1、radius 1–10、全 RGB / luminance-only / chroma-only 模式及两标志同时开启时 luminance 优先的行为。MSL 在 GPU 上直接从当前像素邻域计算空间与颜色距离权重，执行 clamp 边界双边滤波；每帧空间高斯权重只在 CPU 侧预计算并上传，滤波像素和颜色权重都留在 GPU。BT.709 luminance/chroma-only 模式按节点 GLSL 做分离、滤波和 RGB 重建。VideoToolbox NV12/P010 源纹理先在 GPU 上转换到私有 BGR 工作缓冲，效果链再以 Metal 私有缓冲继续处理，没有逐效果 CPU 回读；最终输出仍读回给编码器。

测试覆盖 CPU 双边滤波 alpha 行为、luminance/chroma-only 通道保持、参数门控、identity strength、Metal 与 CPU 对照的多个 radius/mode、Denoise 在有序多效果链中的位置，以及真实 VideoToolbox NV12 与 P010 Metal 纹理输入。聚焦 CPU/capability/preflight 测试 **86 项通过**；本地真实纹理测试 **2 项通过**；合成帧与效果链 Metal 对照测试 **6 项通过**。测试输入与输出比较复用同一 Metal 颜色转换基准帧，消除解码器色彩路径差异。

真实 H.264 导出使用 `tests/test_videos/2K_h264.mp4`，1920×1080 NV12 输入、1280×720 H.264/yuv420p 输出、VideoToolbox 硬件编码、Denoise strength 0.5/radius 3、每次 480 帧。两次端到端分别为 **3.791 秒 / 126.601 FPS** 与 **3.446 秒 / 139.300 FPS**，合并吞吐 **132.647 FPS**；两轮都由 Metal 成功合成 480/480 帧、480/480 帧使用 Metal decode texture，renderer 0 errors/fallback，`ffprobe` 均确认 H.264 输出为 480 帧。Metal renderer 计时分别为 1.059 与 1.051 秒，解码计时分别为 0.963 与 0.956 秒；两轮导出均 render/encode pipeline eligible。端到端约有 10% 波动，GPU renderer 计时相对稳定。相同尺寸无效果基线此前为 142.081 FPS，当前双边滤波负载吞吐约为其 93%；此轮没有跑等效 CPU Denoise 长测，不能据此量化 CPU/Metal 加速比。直接 FFmpeg 的无效果基线约 313 FPS，工作量不同，只能作为端到端吞吐参照。成片分别位于 `/tmp/topos_metal_denoise_20260914/metal_sequential_denoise_480f_1280x720.mp4` 和 `/tmp/topos_metal_denoise_repeat_20260914/metal_sequential_denoise_480f_1280x720.mp4`。

本节记录 Denoise Metal 完成时的覆盖快照；后续 Edge Detect kernel_size 5/7、裁切/仿射变换和非 normal blending 已迁入 Metal（§12.12）。插件效果、alpha、frame-blend retime、空间转场与 clip-level temporal denoise 仍可能走 CPU；Topos 高位深交付现已支持 Metal 直接生成 YUV422P10 平面，完整 Metal 化与端到端性能目标尚未达成。

### 12.8 Defocus 时间线 separable Metal 路径与真实素材复测（2026-09-14）

Defocus 已加入 CPU 导出 handler 和时间线 Metal 参数路径。CPU `separable` 参考按焦点与焦距计算逐像素 blur radius、以 31 个水平 tap 执行变量 sigma 高斯滤波，并覆盖 clamp/wrap/mirror 与 process-alpha 开关；`mipmap` 参考构造 clamped、逐级量化的 2×2 box pyramid 并按 `log2(radius + 1)` 做分数 LOD 混合，供 Metal parity 测试使用。

Metal 当前覆盖 `mode=separable` 与 `mode=mipmap` 的节点参数范围（focal_x/y、focal_size、max_radius 0–100、三种边界策略、process_alpha）。separable 每像素在 Metal 内求焦点距离与 Gaussian 权重；mipmap 金字塔逐层在 GPU 下采样并在 GPU 做分数 LOD 双线性/三线性采样。VT NV12/P010 纹理在 GPU 上 materialize 到 private BGR 后执行 Metal kernel，单效果及有序多效果链中间都没有 CPU 回读。

回归覆盖 CPU separable/mipmap、alpha 语义与参数范围；Metal 三种边缘策略、Defocus→Sharpen 链及 MSL 参数打包；真实 VT NV12/P010 输入与同一 Metal 解码基准帧生成的 CPU Defocus 对照。Metal/CPU 合成帧差异平均不超过 0.2 code value、最大不超过 1；真实 NV12/P010 缩放输出平均差不超过 1.0，99 百分位不超过 4。合并效果覆盖、Metal renderer、能力/预检与时间线导出回归 **288 项通过、5 项环境条件跳过**。

真实素材 `tests/test_videos/2K_h264.mp4`（1920×1080 H.264/NV12）导出为 1280×720 H.264/yuv420p，VideoToolbox 硬件编码、separable Defocus、每次 480 帧：两轮总用时 **3.770 秒 / 127.318 FPS** 与 **3.456 秒 / 138.871 FPS**，合并吞吐 **132.843 FPS**；两次均 480/480 帧使用 Metal 解码纹理且 GPU compositor 全部成功，Metal renderer 0 errors/fallback；`ffprobe` 均核实输出为 480 帧。Metal render 累计 **0.837/0.840 秒**，波动主要在端到端其它阶段。吞吐约为此前同尺寸无效果时间线基线 142.081 FPS 的 93%；直接 FFmpeg 无效果 313 FPS 的工作量不同，只作上限量级参照。成片位于 `/tmp/topos_metal_defocus_20260914/metal_sequential_defocus_480f_1280x720.mp4` 与 `/tmp/topos_metal_defocus_repeat_20260914/metal_sequential_defocus_480f_1280x720.mp4`。

本节记录 Defocus Metal 完成时的覆盖快照；§12.11 已扩展 Blur Motion/Bokeh 和形态学范围，§12.12 又覆盖 Edge Detect 5/7、crop/仿射变换与非 normal blending。插件效果、alpha、clip-level temporal denoise、frame-blend retime 和空间转场仍未全迁入；Metal 输出仍需读回 CPU 后交给编码器，因此尚不能认定全时间线 Metal 化或达到机器端到端极限。

### 12.9 Erode/Dilate reflect/wrap 边缘策略的 Metal 扩展与真实导出（2026-09-14）

形态学 Metal 路径现覆盖 schema 的 extend、reflect、wrap 三种边缘策略，映射分别为 clamp、含边界镜像、周期采样；当前支持整段 size/iterations schema、opaque BGR 和 `channels=all`，alpha-only 在不透明视频上是 identity。单效果和效果链共用相同 Metal edge-coordinate helper，水平/垂直分离 pass 与 direct pass 使用同一策略。

合成帧回归对 erode/dilate × 三种边缘策略与 CPU OpenCV handler 做逐像素比较，六种组合全部完全相同。真实 VT NV12/P010 测试也加入 reflect erode 和 wrap dilate，均以同一 Metal 解码参考帧对照 CPU 输出。Metal morphology border-mode 门控/合成测试 **12 项通过**，真实 NV12/P010 texture 测试 **2 项通过**。

真实 `tests/test_videos/2K_h264.mp4` 480 帧，1920×1080 NV12 输入、1280×720 H.264/yuv420p VideoToolbox 硬件编码，执行 `dilate size=5 / iterations=1 / channels=all / edge_mode=wrap`：端到端 **3.445 秒 / 139.336 FPS**，480/480 Metal texture 输入与 GPU compositor 成功，renderer 0 errors/fallback，`ffprobe` 确认 480 帧；Metal render 累计 **0.850 秒**。成片位于 `/tmp/topos_metal_morph_wrap_20260914/metal_sequential_erode_dilate_wrap_480f_1280x720.mp4`。半径超 16 和 alpha-only 之外的所有时间线功能尚未因此完成迁移，最终 encoder readback 也仍存在。

### 12.10 Blur bilateral 与 Defocus mipmap 的 GPU 覆盖和实测（2026-09-14）

时间线 Blur 的 bilateral 变体已加入 Metal effect kind 9。Metal 使用 OpenCV CPU 参考一致的 8-bit 3 通道 L1 颜色距离、圆形邻域和空间高斯权重；范围覆盖 schema 半径 0.5–100（tap 直径按既有 21 上限截断）、edge-preserve、mix 与独立 RGB 通道掩码。opaque BGR 没有 alpha 时 alpha-only 为 identity；有限参数、Metal YUV texture、单效果与链式处理由 GPU 完成。Box/Gaussian 保留原 separable Metal pass；Motion/Bokeh 在 §12.11 迁入。

测试在随机 BGR 帧上对 edge-preserve 0.4/0.9、部分 RGB mask 与混合强度做 Metal/OpenCV 对照，平均误差不超过 0.5 code value、最大差不超过 3；NV12/P010 VT 纹理对照、效果链和导出器参数门控亦纳入回归。Edge Detect 的 CPU `channel=a` 在 opaque BGR 上等价于全白，Metal 已补齐该 identity 语义。

真实素材 `tests/test_videos/2K_h264.mp4` 的 Defocus `mode=mipmap` 480 帧导出已完成：1920×1080 H.264/NV12 输入、1280×720 H.264/yuv420p 输出、VideoToolbox 硬件编码，端到端 **3.834 秒 / 125.201 FPS**；480/480 Metal texture 输入、480/480 GPU compositor success，renderer 0 errors/fallback，`ffprobe` 确认 H.264 共 480 帧。Metal render 累计 **0.911 秒**。文件为 `/tmp/topos_metal_defocus_mipmap_20260914/metal_sequential_defocus_mipmap_480f_1280x720.mp4`。它略慢于 separable 路径的两轮均值约 132.8 FPS，当前 mip pyramid 构建开销未换来本测试参数下吞吐提升；GPU 路径保证避免 CPU effect fallback，不代表对所有参数都更快。端到端仍低于此前直接 FFmpeg 无效果约 313 FPS，最终 GPU→CPU 编码帧交付和逐帧流水线是主要待优化项。

### 12.11 Blur Motion/Bokeh 与完整 Erode/Dilate 范围（2026-09-14）

Metal Blur 现在覆盖 Box、Gaussian、Bilateral、Motion 和 Bokeh 五种模式。Motion 的 angle/shutter 与 quality（8/16/32 samples）、Bokeh 的 circle/5/6/8-blade aperture、anamorphic、highlight gain、edge policy、RGB mask 和 mix 均在专用 MSL pass 中执行；旧版 directional 值映射为 Motion。Velocity-map Motion 暂未覆盖，因为时间线 effect 输入不携带 velocity image；此参数组合明确回到现有 CPU 路径。像素对照以共享 GLSL 单 pass 变体的独立采样参考为准，Motion 和 Bokeh 平均误差低于 0.35 code value、最大误差不超过 1；同时由真实 Metal renderer 测试确保新内核编译、链路派发和无 CPU fallback。

Erode/Dilate 的 native 参数范围扩展到整个节点 schema：size 1–50、iterations 1–10（合成半径最大 250），并继续支持 extend/reflect/wrap。半径大于 16 使用 GPU 水平与垂直分离 min/max pass；未使用固定 65-tap Gaussian 权重表，避免超出边界读写。size 35 与 size 50/10 iterations 的 Metal 输出对 CPU OpenCV 参考逐像素完全一致。

真实素材测试使用 `tests/test_videos/2K_h264.mp4`，1920×1080 H.264/NV12 输入、1280×720 H.264/yuv420p 输出、VideoToolbox 硬件解码/编码、480 帧。Motion radius 8 导出 **3.905 秒 / 122.930 FPS**，Metal render 累计 **0.975 秒**；Bokeh radius 8 导出 **3.630 秒 / 132.229 FPS**，Metal render 累计 **1.092 秒**。两次都为 480/480 Metal texture 输入和 GPU compositor success，renderer 0 errors/fallback，ffprobe 确认 480 帧。Bokeh 文件在 `/tmp/topos_metal_bokeh_20260914/metal_sequential_blur_r8_bokeh_480f_1280x720.mp4`，Motion 文件在 `/tmp/topos_metal_motion_20260914/metal_sequential_blur_r8_motion_480f_1280x720.mp4`。同日无效果 Metal 完整导出 480 帧为 140.461 FPS；直接 FFmpeg VideoToolbox 解码 + `scale_vt` + VideoToolbox H.264 编码参考约 232 FPS，输出也经 ffprobe 确认 480 帧。Motion/Bokeh 实际效果路径稳定进入 Metal，但项目导出端到端还只有这条 FFmpeg 参考的约 53–57%，尚未接近机器极限。Metal 仍读回最终 YUV 平面交给 PyAV/VideoToolbox；decoder/render/encoder 之间的像素缓冲交接与帧调度是后续主要方向。

### 12.12 Metal 图层几何、混合模式、Edge Detect 大核与多轨解码并行（2026-09-14）

Metal 图层合成现支持非 identity 仿射变换、源裁切与输出画布裁切/羽化 matte；几何采样通过逆仿射映射回源纹理坐标，再在 Metal layer pass 内合并 alpha。ClipBlendMode 的 16 种模式（normal 及 15 种非 normal 模式）均有 Metal 实现，包括 HSL 类 Hue/Saturation/Color/Luminosity，RGB 通道顺序已与 CPU 参考对齐。Edge Detect 参数门控现接受 3/5/7 kernel size，MSL 处理 Sobel、Prewitt、Laplacian 和 Canny-like 分支；5×5 与 7×7 不再因 kernel size 单独回退。当前测试以 CPU handler 对照了 transform/crop 的硬边与 feather 两种情况、15 种非 normal blend，以及 Sobel-5、Canny-5、Laplacian-5、Prewitt-7；Metal renderer suite（含懒编译测试）**103 项通过**，效果能力/共享 Metal context suite **89 项通过**。

为多轨导出减少逐素材预取串行等待，VideoToolbox Metal read-ahead worker 上限从 1 提至 4。每个 decoder session 自带 VT/decode 锁，同一素材仍串行推进，独立视频素材可以并行预取；新增并发回归验证两个 session 的预取会重叠，prefetch suite **3 项通过**。Metal compute context 按 MSL 源哈希复用同一已编译 library；timeline renderer 还改为按真实调用路径懒创建 pipeline state，避免普通单层导出预先构建整套效果 PSO。

真实双素材实测使用 `tests/test_videos/2K_h264.mp4`（H.264/NV12）与 `tests/test_videos/2K_H265.mov`（HEVC/P010），各 1920×1080，480 帧、1280×720 H.264/yuv420p 输出、top track opacity 55%、Overlay blend、VideoToolbox 编码。并行预取前两轮为 **90.802 / 91.279 FPS**；并行后连续三轮为 **101.090 / 101.062 / 101.647 FPS**，均值 **101.266 FPS**，较前两轮均值提升 **11.2%**。优化后三次均为 960/960 次源帧请求返回 Metal texture、480/480 帧 GPU composite 成功，renderer 0 errors、fallback 原因为空；Metal render 累计约 **0.91–0.95 秒**，两个解码器请求累计耗时约 **2.25 秒**。输出均经 ffprobe 确认为 480 帧 H.264。文件为 `/tmp/topos_metal_blend_overlay_parallel_20260914/metal_stack_none_overlay_480f_1280x720.mp4`、`/tmp/topos_metal_blend_overlay_parallel_repeat_20260914/metal_stack_none_overlay_480f_1280x720.mp4` 和 `/tmp/topos_metal_blend_overlay_final_20260914/metal_stack_none_overlay_480f_1280x720.mp4`。懒 pipeline 修改后的单素材无效果 Metal 实导为 **146.331 FPS**（480 帧，GPU render 0.470 秒，无 fallback）。

相同输入、分辨率、Overlay opacity 与 VideoToolbox encoder 的直接 FFmpeg filtergraph 用软件解码为 **2.90 秒 / 165.5 FPS**，用 VideoToolbox 硬件解码为 **3.82 秒 / 125.7 FPS**；两份 FFmpeg 输出也都是 480 帧。Metal 双轨当前约为软件解码 FFmpeg 的 61%、VT 解码 FFmpeg 的 81%，尚未接近本机端到端吞吐上限。Metal 的 shader 渲染耗时不到 1 秒，主要差距在解码/帧调度、CPU/Metal 交接与 encoder 输入；最终 GPU→CPU YUV 读回仍存在。尚未迁入 Metal 的主要时间线功能是插件 shader、clip-level temporal denoise、frame-blend retime、空间转场 mask 生成、复合片段及 velocity-map Motion；透明 alpha 交付仍未支持，高位深 Topos 交付已支持原生 YUV422P10 平面。不能把当前覆盖或 FPS 作为“所有效果全 Metal”或“达到机器极限”的验收。

### 12.13 空间转场进入 Metal 图层栈（2026-09-15）

内置 wipe、iris、slide 及已注册的 spatial user transition 现在可进入 Metal 时间线渲染器：每侧 source conversion、primary grade、已支持的 clip effect chain、仿射变换、裁切 matte 和 opacity 由 Metal 图层 pass 执行，双方结果再由 Metal mask-composite kernel 合成。转场 mask 仍由现有 CPU `compute_transition_mask` 逐帧生成并上传；这项 GPU 化尚未完成。无效果/几何层栈与单独及后续普通图层的 wipe 已在本机 Metal 对 OpenGL 像素对照通过（最大误差 1 code value）；另有 exporter 路由测试验证空间转场携带两侧效果、变换和裁切进入 Metal renderer。真实素材端到端转场导出尚未取得有效结果：本机 VideoToolbox/Metal 驱动在测试期间挂起，不能据此报告该场景 FPS。


### 12.14 真实时间线效果耗时与 Metal 纹理预转换优化（2026-09-15）

用 `bench_out/metal_timeline_export/run_real_h264_ab.py` 对 `tests/test_videos/2K_h264.mp4` 做了 480 帧完整导出：1920×1080 H.264/NV12 输入，1280×720 H.264/yuv420p 输出，VideoToolbox 硬件编码。每次均为 480/480 帧使用 Metal 纹理、480/480 GPU compositor success、renderer 0 errors / fallback；ffprobe 均核实输出为 480 帧。该测试包含解码、时间线渲染、编码、复用与收尾，FPS 不是 shader-only 速度。

| 效果 | 参数 | Metal 渲染累计 | 完整导出 FPS |
|---|---:|---:|---:|
| 无效果基线 | — | 0.470 秒 | 132.976 |
| Box | radius 8 | 1.278 秒 | 115.327 |
| Gaussian（优化后，两轮） | radius 8 | 1.208–1.276 秒 | 113.196–121.246 |
| Bilateral（threadgroup cache，两轮） | radius 8 | 1.450–1.492 秒 | 119.439–121.128 |
| Motion | radius 8 | 1.332 秒 | 110.119 |
| Bokeh（Metal 偏移预计算，两轮） | radius 8 | 1.287–1.349 秒 | 114.322–122.929 |
| Denoise（threadgroup cache，两轮） | strength 0.5 / radius 3 | 1.016–1.031 秒 | 130.948–131.427 |
| Edge Detect | Sobel 3×3 | 0.989 秒 | 128.080 |
| Defocus | separable 默认参数 | 1.142 秒 | 122.345 |
| Glow（优化后，两轮） | radius 8 | 1.455–1.474 秒 | 113.793–116.394 |
| Sharpen（优化后，两轮） | radius 1 | 1.029–1.034 秒 | 126.044–126.739 |
| 效果链 | Gaussian + Sharpen + Glow | 2.117 秒 | 102.808 |

发现若干单效果直接从 VT YUV 纹理读取，邻域中的每个采样点都会重复做 YUV→RGB 和 primary-grade 转换。现在 Metal 在进入任一单效果前先把源帧转换一次到 RGB buffer，再由后续 Metal pass 采样；数据不回 CPU。NV12 与 P010 的 Metal texture 集成测试覆盖了 Gaussian、Glow 和 Sharpen 路径。

半径 8 Gaussian 在预转换前的单轮为 **1.823 秒 GPU 渲染 / 106.821 FPS**；优化后的两轮为 **1.208 / 1.276 秒、113.196 / 121.246 FPS**，码流文件大小与优化前一致。Glow 优化前 **2.707 秒 / 93.204 FPS**，优化后两轮 **1.455 / 1.474 秒、113.793 / 116.394 FPS**，输出大小一致。Sharpen radius 1 优化前 **2.585 秒 / 95.844 FPS**，优化后两轮 **1.029 / 1.034 秒、126.044 / 126.739 FPS**，输出大小一致。三种成片优化前后各 480 帧解码像素逐项相同。Bokeh 每个像素过去都会重复计算相同的 spiral/aperture offset；现在每帧由 Metal 小 kernel 生成最多 48 个偏移并写入 GPU buffer，偏移计算不回 CPU。优化前 **1.510 秒 GPU 渲染 / 110.712 FPS**，当前实现两轮 **1.287 / 1.349 秒、114.322 / 122.929 FPS**。新旧 Bokeh 成片逐帧比较的平均绝对差为 **0.062 code value**，p99 为 2、p99.9 为 5、最大差 31；新实现的两轮成片逐像素相同。Motion 的方向三角函数也已从每像素移动到帧参数构造阶段，但版本间 GPU 累计耗时均约 1.33 秒，尚无可证明的速度收益。

Denoise strength 0.5 / radius 3 原路径为 **1.478 秒 Metal 渲染 / 112.432 FPS**。现在由 16×16 Metal threadgroup 协作读取含 10 像素 halo 的 RGB tile，组内复用 7×7 邻域；两轮为 **1.016 / 1.031 秒 GPU 渲染、130.948 / 131.427 FPS**。优化前后 480 帧解码图像的逐帧哈希完全一致；现有 CPU 对照测试覆盖 radius 1–10 与 luminance/chroma 模式。

Bilateral radius 8 原路径为 **2.982 秒 Metal 渲染 / 86.040 FPS**。新路径由 16×16 Metal threadgroup 协作读取含 10 像素 halo 的 RGB tile，组内复用邻域数据；两轮为 **1.450 / 1.492 秒 GPU 渲染、119.439 / 121.128 FPS**。优化前后 480 帧解码像素逐项相同。8-bit range lookup 与快速指数试验没有收益，已撤回。全链路目标仍未完成：本 renderer 在 GPU 最终输出后要把 YUV 平面读回 CPU，再送入 PyAV/VideoToolbox；解码调度、同步和编码面交接仍限制端到端速度。不同轮次的解码、编码和系统负载会波动，因此应优先看多轮 GPU render 累计与输出完整性，端到端 FPS 只报告实测范围。

#### Topos 422 Proxy 原生编解码实测

基准脚本现支持 `--codec topos_proxy`，并用 `MediaSourceFactory`/`ToposMediaSource` 探测输出（FFmpeg `ffprobe` 不认识 TPIC）。在 `tests/test_videos/2K_topos.mov`（1920×1080、Topos 422、10-bit、25fps）上，缩放到 1280×720 输出 Topos 422 Proxy，完整导出均为 480/480 帧、`yuv422p10le`、Metal renderer 0 errors、无 fallback。2026-09-15 已将 master 422 的 native decode 接到 IOSurface→Metal 三平面路径；下表同时保留改造前 CPU planar 上传基线，便于复核收益：

| 场景 | Metal GPU render | 完整导出 FPS | 输出 |
|---|---:|---:|---|
| 无效果（CPU planar 上传基线） | 1.927 秒 | 44.517 | 480 帧 Topos Proxy，16.16 MB |
| 无效果（IOSurface→Metal 三平面直通，manifest 异步哈希） | **1.464 秒** | **93.819** | 480 帧 Topos Proxy，16.16 MB；缓存 Metal device/IOSurface textures 后复测 |
| Denoise strength 0.5 / radius 3 | 2.576 秒 | 43.231 | 480 帧 Topos Proxy，16.08 MB（旧同步 manifest 口径） |
| Gaussian + Sharpen + Glow | 3.440 秒 | 40.202 | 480 帧 Topos Proxy，16.15 MB（旧同步 manifest 口径） |
| 两轨 normal（100% + 55%，新异步 manifest 口径） | **0.493 秒** | **50.163** | 120 帧 Topos Proxy，3.99 MB |

Topos 的压缩码流熵解码/重建仍由自研 native CPU binding 执行，但 master 422 现在直接写入 `ToposIOSurfacePool` 的三平面，Metal 导入为 `R16Uint` 纹理；时间线 Metal 负责 10-bit 色彩转换、调色、缩放、效果、双轨合成及最终 YUV422P10 打包，CPU 只把最终平面提交给 Topos native 编码器。此前完整导出计时还包含同步扫描 2.7 GB 源文件的 manifest SHA-256，导致 GPU 端到端收益被掩盖；现改为先写 metadata manifest，后台再原子替换完整哈希版本。随后将 `MTLCreateSystemDefaultDevice()` 和每个 IOSurface 槽的三平面纹理导入改为按源生命周期/环槽缓存；同一份 480 帧真实素材复测，解码阶段由 **2.710 s（177.09 FPS）降至 2.474 s（194.06 FPS）**，端到端由 **5.259 s（91.27 FPS）降至 5.116 s（93.819 FPS）**，输出仍为 `metal_texture_frames=480`、`topos_metal_surface_frames=480`、`gpu_yuv422_frames=480`、renderer 0 errors。单帧直通输出与 CPU 上传参考逐平面最大差值为 0，完整 MOV 与此前 CPU planar 基线字节级相同。当前仍不是 GPU 熵解码，也不是 IOSurface 直接喂给编码器的完整零拷贝路径；Topos 编码器本身没有硬件编码标志。新增真实测试输出位于 `/tmp/topos_codec_compare_project_20260915_run3/`，120 帧直接编解码与项目对比保存在 `tests/media/results/topos_codec_vs_project_20260915.json`，复测脚本为 `bench_out/codec_bench/compare_topos_direct_vs_project.py`。

#### 调色页与合成页状态

调色页的实时画面、主调色、Scopes 和 LUT 采样仍由独立 OpenGL render thread 完成；正常预览不回读像素，Scopes 走 GPU texture descriptor，旧的 CPU scope snapshot 只是暂停/诊断降级。合成页的 `CompositeRenderer` 也在独立 OpenGL context 中完成节点求值和纹理环；但 `CompositeFrameRenderer` 的 headless 导出会在最后一步读回 BGR，`CompositeGraphRenderService.render_gpu()` 仍是 dormant 接口。也就是说，这两个页面目前是 GPU 预览链路，尚未达到 Metal 全链路交付。下一阶段应按同一顺序推进：先让 Color/Composite 的导出接口返回 GPU-owned texture/plane lease，再接入 Metal backend 与 Topos encoder，最后用相同 480 帧素材矩阵验证无 CPU 中间帧、帧数、画质 golden 和端到端 FPS；在此完成前不把页面标成“全 Metal”。

---

## 13. 尚未验证的开放问题（诚实清单）

以下问题**本文档无法回答**，必须靠 Phase 1 probe 实测，不应在文档中写成断言：

| # | 问题 | 归属 |
|---|---|---|
| 1 | `CVMetalTextureCache` 对 P010 双平面、4:2:2 10-bit、ProRes surface 各自的支持边界 | ✅ 已实测（2026-09-11）：NV12/P010/BGRA/2vuy 直通；**v210 无 10-bit 直通**（仅 8-bit 重解释导入，Metal 无原生 10-bit 422 格式）——见 ADR 0003 |
| 2 | Qt 6 `QOpenGLWidget` 与独立 `CAMetalLayer` 共存的实际约束（图层合成、Z 序、vsync/DisplayLink、resize、EDR/HDR 色彩空间） | ✅ 已实测（2026-09-11）：同窗共存可行，窗口级重叠时 Metal 覆盖 GL，vsync 60Hz 锁定，resize 需手动 re-frame；**EDR/HDR 未测**（留给 P10-05）——`tests/media/results/qt_metal_coexistence.md` |
| 3 | ~~ctypes `void_p` 与 PyObjC 调用边界的可行桥接方式~~ **已实测解决（§0.5）：ctypes 可行，PyObjC 返回 -6661** | ✅ |
| 4 | PyObjC 每帧编码 command buffer 的开销量级（决定正式实现必须走 Obj-C++/Swift 的临界点） | P1-04/P1-05 |
| 5 | ~~Metal vs OpenGL 在本机的 render-only 实际差异~~ **已实测（2026-09-11）：Metal +81~85%（四组合一致，K1 不触发）**——`tests/media/results/backend_compare_render_ab.md` | ✅ |
| 10 | ProRes surface 的 `CVMetalTextureCache` 导入（本机无 ProRes 硬解，需在有 ProRes 引擎的机器上验证；见 §0.2 A′）；**叠加 v210 无 10-bit 直通的结论，双重不可行** | Phase 5 |
| 6 | D3D11/D3D12 HDR 呈现方案（scRGB vs HDR10 swapchain） | Phase 8 |
| 7 | ~~`topos_surface_capability.py` Windows/Linux 分支的环境变量缺口在修复前的实际影响面~~ **已修复（P0-06，commit 7c147ac9）：zero_copy 判定改为真实探测，env 标志降级为诊断；影响面本就极小（模块仅测试引用）** | ✅ |
| 8 | 4K **全分辨率**（非 proxy）的端到端 p95 是否可达标——合成 4K 档已建基线（wall 63.4 FPS，见 `p0_opengl_baseline.md`），**真实 4K 素材仍缺（P0-03 托管决策阻塞）** | Phase 0 |
| 9 | macOS 上 Normalize 百分位模式的静默空操作（§0.4）影响面：有多少已保存工程依赖了 Normalize 的 percentile 结果 | P4-03 |

---

## 14. 官方参考

**Metal / MSL**
- [Apple Metal Overview](https://developer.apple.com/metal/)
- [Metal Shading Language Specification](https://developer.apple.com/metal/Metal-Shading-Language-Specification.pdf)
- [Apple CVMetalTextureCache](https://developer.apple.com/documentation/corevideo/cvmetaltexturecache-q3j)
- [Apple Metal Performance Shaders](https://developer.apple.com/documentation/metalperformanceshaders)
- [Apple CAMetalLayer](https://developer.apple.com/documentation/quartzcore/cametallayer)
- [Apple: Displaying HDR content in a Metal layer](https://developer.apple.com/documentation/metal/displaying-hdr-content-in-a-metal-layer)

**Qt**
- [Qt QOpenGLWidget](https://doc.qt.io/qt-6/qopenglwidget.html)
- [Qt Graphics and RHI](https://doc.qt.io/qt-6/topics-graphics.html)
- [Qt QRhi](https://doc.qt.io/qt-6/qrhi.html)
- [Qt: OpenGL and Qt Quick 2.x（`QSG_RHI_BACKEND`）](https://doc.qt.io/qt-6/qtquick-visualcanvas-scenegraph-renderer.html)

**Windows / NVIDIA**
- [NVIDIA NVDEC Video Decoder API](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.0/nvdec-video-decoder-api-prog-guide/index.html)
- [NVIDIA CUDA Interoperability](https://docs.nvidia.com/cuda/cuda-programming-guide/04-special-topics/graphics-interop.html)
- [Microsoft: Direct3D 11 Video APIs](https://learn.microsoft.com/en-us/windows/win32/medfound/direct3d-11-video-apis)
- [Microsoft: High Dynamic Range (HDR) rendering](https://learn.microsoft.com/en-us/windows/win32/direct3darticles/high-dynamic-range)

**PyObjC**
- [PyObjC Framework Metal](https://pyobjc.readthedocs.io/en/latest/apinotes/Metal.html)
- [PyObjC CoreVideo / Quartz](https://pyobjc.readthedocs.io/en/latest/apinotes/Quartz.html)

**项目内相关文档**
- `docs/4k-playback-optimization.md`（上游计划：性能目标、分段耗时、优先级）
- `docs/system_check_plan_2026-09-04.md`（当前有效计划指针）
- `docs/module_boundary_rules.md`（分层约束）
- `docs/taep_engineering_conventions.md`（线程与 GPU 边界）
- `docs/PLUGIN_DEV_GUIDE.md`（插件契约与 Capability Manifest）
- `.clinerules`（强制约束）
