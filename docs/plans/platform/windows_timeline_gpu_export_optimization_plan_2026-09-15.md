# Windows 时间线全链路 GPU 导出优化与真实素材验收计划

> 状态：计划已建立；步骤 0–2 的可离线部分（测量协议、基线、device/纹理池/帧契约、T0 直通链校验层）已在 AMD RX 6800 真机执行并归档（见 §11 执行记录），解码 surface 与硬编 surface 提交仍待原生桥。
>
> 日期：2026-09-15  
> 适用范围：Topos Color 的 Windows 时间线导出（H.264 / HEVC / AV1；NVIDIA、AMD、Intel）  
> 关联文档：[既有 Windows 后端计划](windows_optimization_plan_2026-09-11.md)、[Metal 迁移与实测记录](metal_backend_migration_and_cross_platform_gpu_plan.md)

## 1. 目标和边界

目标是让**未压缩视频帧**在 Windows 上遵循下面的链路：

```text
硬件解码 D3D11 surface
  -> D3D11 纹理池
  -> HLSL 时间线渲染（色彩、合成、转场、滤镜）
  -> GPU 内 NV12 / P010 输出纹理
  -> NVENC / AMF / oneVPL 硬件编码 surface
  -> 压缩码流 -> CPU 封装器 -> 磁盘
```

CPU 可以负责时间线调度、音频、压缩码流封装和文件写入；但从解码完成到硬编提交之间，任何一帧原始像素都不能映射成 `numpy`、`VideoFrame` 或 CPU staging buffer。实现上以 D3D11 为三家显卡共同的纹理和同步边界；CUDA 只可作为 NVIDIA 的可选优化，DirectML 只负责 AI 模型，均不能成为基础时间线导出路径。

这份计划不承诺必然超过同机同设置的 FFmpeg。FFmpeg 已能直接调用同一块硬件编解码单元；Topos 的可验收目标是删除自身额外的回读、格式转换和同步等待，并以同素材、同编码设置的 FFmpeg GPU 链路为参照逼近硬件上限。

## 2. 当前事实：已有硬编，不等于全 GPU 时间线导出

| 模块 / 证据 | 已有结果 | 对本计划的含义 |
|---|---|---|
| `src/shared/media/windows_hw/hw_planar_source.py` | Windows 硬解后下载 NV12/P010 到 CPU 的真实路径 | 是“硬解 + CPU 像素”的加速，不能计作零回读。 |
| `src/shared/export/encoder.py` 和既有 Windows 真机报告 | NVENC、AMF 已有硬编及真实吞吐结果 | 证明编码单元可用；若输入来自 CPU 转换，仍不构成全链路。 |
| `src/shared/media/windows_hw/d3d11_decoder.py` | 诚实骨架，`open()` 返回不可用 | 尚没有可交付的 D3D11 解码 surface。 |
| `src/shared/media/windows_hw/d3d11_gl_bridge.py` | 诚实骨架 | 不能把 WGL 互操作探测结果当成生产级 surface 链路。 |
| `src/features/edit/core/timeline_export.py` | 图形后端目前只选择 `metal` 或 `opengl` | Windows 尚无 D3D11 时间线渲染器，也没有 Windows 导出偏好项。 |
| 2026-09-12 Windows 双机归档 | AMD RX 6800 与 RTX 4090 的解码、编码、能力矩阵已存档 | 可复用为基线；不能外推为效果、合成或转场的全 GPU 导出结果。 |

既有结果中，AMD 的 CPU 像素格式转换曾是导出下限，三路并行才提升吞吐。这是一个明确的优先级信号：先消除 CPU 转换和回读，再微调编码预设或增加线程。

## 3. 选定架构

### 3.1 为什么选择 D3D11

Media Foundation 可以把应用创建的 D3D11 device 通过 DXGI Device Manager 交给解码器；Intel oneVPL 也要求共享同一 `ID3D11Device` 才能避免外部分配模式退回系统内存。NVENC 支持以 DirectX device 创建会话；AMF 提供 `ID3D11Texture2D` 的原生 surface。D3D11 因此是 NVIDIA、AMD、Intel 都能落在同一 GPU surface 上的最小共同层。[Microsoft Media Foundation D3D11 解码说明](https://learn.microsoft.com/en-us/windows/win32/medfound/supporting-direct3d-11-video-decoding-in-media-foundation)、[Intel oneVPL 硬件加速说明](https://intel.github.io/libvpl/latest/programming_guide/VPL_prg_hw.html)、[NVIDIA NVENC 编程指南](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.1/nvenc-video-encoder-api-prog-guide/)、[AMD AMF SDK](https://github.com/GPUOpen-LibrariesAndSDKs/AMF)。

`WGL_NV_DX_interop` 可作为复用现有 OpenGL 渲染器的过渡实验，不能作为目标架构：它依赖上下文和驱动实现，且不能覆盖所有 vendor。D3D12 也不先行；只有 D3D11 完整链路验收后，且目标编码器、解码器和效果路径均有可测收益时，才单独评估。

### 3.2 Windows 后端选择策略

Windows 继续把 OpenGL 保留为重要选项。它依赖显卡供应商的 OpenGL ICD，适合继续承载现有 ModernGL/GLSL 预览、已验证的 GPU 滤镜、旧硬件或驱动的兼容路径；不能因为 macOS 的弃用状态而把 Windows 的 OpenGL 路径删除。

| 用户选择 | 适用场景 | `Auto` 的选择规则 | 导出声明边界 |
|---|---|---|---|
| `Auto` | 默认选项 | 预览优先选择已通过本机能力探测的稳定路径；导出只有在完整 D3D11 surface probe、效果覆盖和性能门禁都通过时才选择 D3D11。 | 所选后端、硬解、回读和回退原因必须显示在 `ExportGpuTrail`。 |
| `OpenGL` | 预览、现有 ModernGL/GLSL 效果、插件兼容、D3D11 不可用时的 GPU 回退 | 在用户显式选择时固定使用；`Auto` 在 D3D11 未验收、驱动不满足或效果尚未迁移时可选用。 | OpenGL 本身可以是 GPU 渲染；若需要 GL↔D3D 桥接或 CPU 像素复制，则不能标记为“全链路 GPU 导出”。 |
| `D3D11` | Windows 真实素材导出、NV12/P010、HDR、硬解 surface 直连 NVENC/AMF/oneVPL | 仅在 decoder、renderer、encoder 同 adapter LUID，且零像素回读计数通过时启用。 | 这是 Windows 全 GPU 导出的主路径。能力不足时回退到 OpenGL 或 CPU 路径，并保留原因。 |

因此，Windows 的目标不是“用 D3D11 替换 OpenGL”，而是让两者各自承担合适职责：OpenGL 维持成熟的实时渲染和兼容性；D3D11 负责与 Windows 视频编解码 surface 原生衔接的高性能导出。

### 3.3 需要新增的稳定契约

| 契约 | 内容 | 硬规则 |
|---|---|---|
| `GpuDecodedFrame` | `ID3D11Texture2D`、DXGI 格式、颜色元数据、时间戳、adapter LUID、fence / release lease | 不暴露 CPU planes；跨 adapter 立即失败并给出 reason。 |
| `D3D11TimelineRenderer` | 输入 texture -> 输出 float / NV12 / P010 texture 的批量渲染 API | 所有资源来自定长池；逐帧不得创建纹理或等待 GPU。 |
| `GpuEncodeSurface` | texture、格式、编码器、fence、提交 / 归还所有权 | 编码器拿到的必须是同一 device 的 GPU texture。 |
| `ExportGpuTrail` | 每段后端、回读字节数、上传字节数、等待时间、回退 reason | 任何“全 GPU”结论必须由该 trail 证明。 |

设备以 DXGI LUID 为唯一身份。解码、渲染、编码不在同一 adapter 时，默认回退当前已验证的下载路径并记录 `adapter_mismatch`，绝不静默跨卡复制。

### 3.4 NVIDIA 与 AMD 的最大化利用规则

三家通用的策略是：应用在 Windows 图形设置中优先选定高性能 GPU，但运行时仍以实际的 adapter LUID 为准；同一条导出任务只能在同一块 GPU 上解码、渲染和编码。HLSL / DirectCompute 承担所有通用色彩、缩放、合成、转场和内建滤镜，使 NVIDIA 和 AMD 共享同一套效果实现。4–8 个有界 in-flight surface 让解码、渲染、编码并行；禁止逐帧 `Map`、`Flush`、`CopyResource` 到 CPU staging 或等待编码完成。

| GPU 路径 | 必须做的事 | 仅在实测证明收益后做的事 | 禁止的伪优化 |
|---|---|---|---|
| NVIDIA | 按素材能力使用 NVDEC / D3D11VA 解码，D3D11/HLSL 渲染，注册 D3D11 texture 直接给 NVENC；输出 shader 直接写 NV12 或 P010。 | CUDA 仅用于 AI 降噪、光流、推理或经 A/B 证明快于 HLSL 的专项；前提是 D3D11↔CUDA 无 staging、同步和回读。 | 为普通 Blur、调色或 resize 先搬到 CUDA 再搬回 D3D11；这会让互操作成本吞掉收益。 |
| AMD | 使用 AMF 的 DX11 decode surface 和 AMF DX11 encode surface，中间仍由 D3D11/HLSL 处理；优先增加有界 surface pool，而不是打开会降低解码性能的 decoder surface copy。 | 在支持的 APU+dGPU 组合上探测 SmartAccess Video，并以 T3/T6 实测决定是否启用其多 VCN 资源分配。 | 依赖 OpenCL 或 CPU 格式转换作为通用效果主链；二者都不能替代 DX11 surface 直连。 |

每个厂商、驱动、codec、bit-depth 组合必须独立跑 T0–T6。报告记录 GPU driver、adapter LUID、GPU-resident frame 比例、CPU 上传/回读字节、各阶段 GPU 时间与编码队列深度；没有这些证据时，不能把“GPU 使用率高”称为性能优化完成。NVIDIA 的 NVDEC/NVENC 可以与 GPU 后处理并行，AMD AMF 可在 DX11 texture 上完成编解码；但每项能力均以运行时 probe 和真实素材结果为准。[NVIDIA Video Codec SDK](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.1/index.html)、[AMD AMF 视频解码 API](https://github.com/GPUOpen-LibrariesAndSDKs/AMF/blob/master/amf/doc/AMF_Video_Decode_API.md)。

## 4. 真实素材测试规程（先做，再优化）

### 4.1 基准矩阵

素材优先使用 `tests/test_videos`，并在 Windows 机器上记录每个文件的 `ffprobe` 信息、SHA-256、帧数和时间码。固定输出 480 帧，先预热 60 帧；每组跑三次，报告中位 FPS、p50/p95 帧时，不用单次平均数得出结论。

| 用例 | 时间线内容 | 目的 |
|---|---|---|
| T0 | 单段 H.264，直通 | 解码 -> 编码的最低开销与 surface 连通性。 |
| T1 | 单段 H.264/HEVC，resize + 主级调色 + 色彩转换 | 验证最常见的渲染链。 |
| T2 | 单段，Blur / Sharpen / Glow / 形态学 / Edge / Denoise / Defocus 代表组合 | 将 Metal 已覆盖的内建效果逐项迁移、逐项测量。 |
| T3 | 两层 H.264 + HEVC，位置、缩放、裁剪、透明度、16 种混合模式 | 验证多源解码和复合。 |
| T4 | 两层空间转场（wipe / iris / slide / 用户空间转场） | 验证过渡期资源、遮罩、并行提交。 |
| T5 | P010 / HDR 输入，HEVC Main10 输出 | 验证 10-bit、色彩元数据和 GPU P010 写入。 |
| T6 | 四轨 4K 压力时间线 | 找到资源池深度、解码并行度、显存和硬编队列瓶颈。 |

每个用例必须有四个对照组：CPU 基线；现有 Windows 硬解下载 + 当前时间线；Topos D3D11 GPU 路径；同分辨率、同编码器、同码率 / 质量档的 FFmpeg GPU 命令。效果语义不能逐像素对齐 FFmpeg 时，FFmpeg 只作吞吐参照；画质则对 Topos 的确定性 CPU reference 做抽帧比较。

### 4.2 必须采集的数据

每次真实导出都输出 `tests/media/results/windows_timeline_export_<机器>_<日期>.json` 和同名 `.md`，内容至少包括：

- Windows build、CPU、内存、GPU、adapter LUID、驱动、FFmpeg/PyAV/原生 bridge 版本；
- 输入 / 输出 probe、完整帧数、耗时、FPS、编码设置和输出文件 SHA-256；
- `decode`、GPU render、GPU YUV conversion、encode submit、encode wait、mux / disk 各段 CPU 和 GPU 耗时；
- `cpu_upload_bytes`、`cpu_download_bytes`、GPU-resident frame 比例、资源池峰值、GPU / CPU 占用；
- 回退 reason，以及不支持效果是否发生 CPU 路径；
- 参考帧的 PSNR / SSIM、色彩元数据、8-bit 和 10-bit 的码值差异。

Direct3D timestamp query 是 GPU 时间的主证据；系统级 GPU 引擎利用率只用于交叉核验。仅凭任务管理器中“GPU 使用率很高”不能认定没有回读。

### 4.3 验收门槛

| 类别 | 门槛 |
|---|---|
| 正确性 | 输出帧数、PTS、方向、alpha、色彩矩阵 / primaries / transfer 正确；CPU reference 抽帧画质门禁通过。 |
| GPU 驻留 | 已宣布支持的完整 GPU 用例中 `cpu_upload_bytes == 0` 且 `cpu_download_bytes == 0`；只有压缩码流允许回到 CPU 供封装写盘。 |
| 回退透明度 | 任一素材、效果、编码器不支持时，UI、日志和报告显示准确的段级 reason；不得伪报硬解或全 GPU。 |
| 资源与同步 | 480 帧后资源池不增长；无逐帧 `Map`、`CopyResource` 到 staging、`Flush` 或 GPU 完成等待。 |
| 性能 | T0/T1 的中位吞吐至少达到同机同参数 FFmpeg GPU 基准的 85%；低于该值时必须先提交分段归因和修复，再增加新效果。T2-T6 对等效果基准可用时同样比较；无对等语义时，以前一版本为基线，逐段 GPU 时间不得回归。 |

## 5. 可借鉴 DaVinci Resolve 的工程能力

Blackmagic 公开确认 Resolve Studio 在 Windows/Linux 使用 CUDA 或 OpenCL 加速、支持多 GPU 和 H.264/H.265 硬件编解码；同时提供 Optimized Media、Render Cache、Smart Cache。[Resolve Studio](https://www.blackmagicdesign.com/cn/products/davinciresolve/studio)、[缓存与优化媒体手册](https://documents.blackmagicdesign.com/UserManuals/DaVinci_Resolve_15_Reference_Manual.pdf)。其未公开实现细节，因此以下把可观察能力转化为 Topos 可验证的工程要求，而不假定其私有实现。

| 可借鉴能力 | Topos 的具体实现 | 验收边界 |
|---|---|---|
| 多阶段并行流水线 | 让 decode、HLSL render、GPU YUV conversion、hardware encode 分别在有界队列中并行；fence 归还 surface，CPU 只调度。 | 每一阶段有时间、队列深度和 wait reason；逐帧同步即失败。 |
| 编译后的时间线图 | 每次时间线或参数版本变化时编译 `RenderPlan`：可见图层、效果 pass、输入输出格式、资源寿命和依赖；帧循环只提交 plan。 | 480 帧导出期间不重复解析效果图、创建 shader 或分配 transient texture。 |
| 专用视频单元 | 将 NVDEC/D3D11VA/AMF/oneVPL 输出 surface 直接交给 D3D11，再把 NV12/P010 texture 直接提交 NVENC/AMF/oneVPL。 | `ExportGpuTrail` 的 CPU 像素上传和回读均为零。 |
| 分层缓存 | 分为短生命周期 GPU source / intermediate pool、按片段失效的 render cache、仅用于预览的 optimized/proxy media。 | 冷缓存和热缓存必须分开报告；缓存命中不能计作纯渲染 FPS。 |
| 多 GPU | 先优化单张最快 GPU 的完整链路；仅在分配独立、无跨卡帧复制并实测受益时启用第二张 GPU。 | 混合 GPU 默认检查 LUID；慢卡、显存小卡和跨卡复制不能拖慢主链路。 |
| 质量档 | 把 preview proxy、interactive export、delivery quality 设为不同 profile；码率、B-frame、lookahead、降噪与缓存策略均受 profile 控制。 | 同一 delivery profile 只能在图像和元数据门禁通过后参与速度比较。 |

缓存只复用没有变化的工作，不改变最终交付的画面：`SegmentRenderKey` 至少包含源文件指纹、入/出点和 retime、图层顺序、效果及参数、grade、色彩管理、输出格式、renderer/backend 版本。任一项变化都使对应片段失效。代理媒体默认只服务预览；最终导出默认仍用原素材，除非用户明确选择代理交付。

## 6. 重新排序后的实施步骤

现有计划中“偏好设置”早于 D3D11 真实链路，“缓存”也没有明确落点。下面的顺序已纠正这两个问题：每一步的门禁未通过，不能进入下一步；任何性能优化都必须回到同一套 T0–T6 真实素材报告。

| 顺序 | 先做什么 | 主要改动 / 产物 | 完成后如何检查，才可继续 |
|---:|---|---|---|
| 0 | 固化测量协议和当前基线 | `scripts/bench_timeline_export_windows.py`、JSON/Markdown schema；为 T0–T6 建立 CPU、现有硬解下载、FFmpeg GPU 三组冷缓存基线。 | 同一素材连续三次、480 帧、60 帧预热；保存 FPS p50/p95、阶段时间、输出 probe、hash 与画质参考。没有基线不改性能。 |
| 1 | 建立原生 D3D11 device 和生命周期 | Windows native bridge、adapter LUID probe、`GpuDecodedFrame` / lease / fence / 定长 texture pool 契约和注入式测试。 | NVIDIA、AMD 实机各做 device/surface probe；跨 LUID 必须得到 `adapter_mismatch`，1,000 帧循环无资源增长。 |
| 2 | 打通最短的零回读 T0 链路 | H.264/HEVC decode surface -> 同 GPU 的 NV12/P010 encode surface；先不加效果，压缩码流回 CPU mux。 | `cpu_upload_bytes == 0`、`cpu_download_bytes == 0`；帧数/PTS/码流可播放。若不能通过，禁止开始 HLSL 效果迁移。 |
| 3 | 先做最小 D3D11 render，再接硬编 | HLSL 的 NV12/P010 读取、copy、GPU RGB/YUV 转换和 NV12/P010 输出；D3D11 renderer 只实现 T1 的 resize、主级调色、色彩元数据。 | 与 CPU reference 抽帧比对；T1 中无 CPU YUV，GPU timestamp 覆盖 render/YUV/encode 三段。 |
| 4 | 让基础链路真正并行 | 4–8 surface ring、decode prefetch、render/encode 异步提交、fence 驱动回收、反压和 `ExportGpuTrail`。 | T0/T1 达到 FFmpeg GPU 基线的 85%，p95 无逐帧 wait 尖峰；未达到时按第 7 节定位并修复。 |
| 5 | 编译时间线 render graph 和常用合成 | `RenderPlan`、资源别名和 pass 合并；transform/crop/opacity、主级调色、16 种混合模式、两轨合成。以 `TimelineMetalExportRenderer` 的语义和 CPU reference 为准。 | T3 逐帧画质、PTS 和资源池检查通过；帧循环中无 graph rebuild、shader compile 或 transient texture 新建。 |
| 6 | 迁移空间转场 | wipe、iris、slide 和用户空间转场；GPU mask 与 compositing，统一 release/fence 语义。 | T4 的过渡首尾帧和中间帧与 reference 对齐；多源队列没有死锁或 frame lease 泄漏。 |
| 7 | 迁移内建效果，先正确后提速 | 按真实使用率实现 Blur 全变体、Sharpen、Glow、erode/dilate、edge detect、denoise、defocus；每项保留 HLSL golden test。 | 每项先通过 T2 画质和色彩门禁，再记录冷缓存 FPS；未迁移效果必须显示 OpenGL/CPU fallback，不能计入 GPU 覆盖率。 |
| 8 | 引入达芬奇式缓存 | 先实现导出期 GPU frame/intermediate reuse，再实现 `SegmentRenderKey` 驱动的磁盘 render cache；最后加入仅供预览的 optimized/proxy media。 | 分别跑 cold、warm hit、一次参数变更后的 partial miss；验证仅受影响片段重渲染，最终 delivery 不误用 proxy。 |
| 9 | 厂商专项和多 GPU，只做有收益的项 | NVIDIA CUDA 专项、AMD SmartAccess Video、Intel oneVPL、独立任务的多 GPU 策略。 | 在通用 D3D11 链路已过 T0–T6 后 A/B；收益必须不低于 15%，且没有 staging / 跨卡复制，否则不启用。 |
| 10 | 产品接入与发布验证 | 偏好设置 `Auto / OpenGL / D3D11`、能力说明、回退提示、自托管 Windows GPU 回归任务。 | 只有所有声明支持的矩阵均有真实报告时，`Auto` 才默认选 D3D11；其他情况保持 OpenGL 或 CPU 回退。 |

### 6.1 每轮实现的固定验证顺序

1. 先跑契约、shader/reference、fallback reason 单元测试，保证不破坏 CPU/OpenGL/Metal 路径。
2. 在一台 NVIDIA 和一台 AMD Windows 真机跑目标用例；功能测试与冷缓存性能测试均使用 `tests/test_videos`。
3. 用 `ffprobe` 核对帧数、PTS、编码 profile、bit-depth 和 HDR metadata；将抽帧与确定性 CPU reference 比较 PSNR/SSIM 和码值差。
4. 读取 `ExportGpuTrail`、Direct3D timestamps、CPU upload/download 字节和 surface pool 峰值，确认没有伪零拷贝。
5. 连续三次导出取中位数和 p95，与同机同设置 FFmpeg GPU 基线及上一个 Topos revision 比较。
6. 只有画质、稳定性、零回读和性能门禁同时通过，才合并该步并进入下一步；否则按第 7 节的症状表修复后重跑。

### 6.2 效果与插件策略

内建效果以 Metal renderer 的语义和 CPU reference 为共同标准，避免两套平台分别“看起来差不多”。任意 GLSL 插件不能假定可自动迁移到 HLSL：需要一个可测试的跨后端 shader contract，或明确标记为 OpenGL GPU 过渡路径。只有在 D3D11 HLSL 实现和真实导出均通过后，才可称为 Windows 全 GPU 效果。

## 7. 用测试定位慢点，再做对应修复

| 观测 | 首要怀疑点 | 验证方式 | 修复方向 |
|---|---|---|---|
| `cpu_download_bytes > 0` 或 copy engine 异常高 | 解码 surface 被下载 / 跨设备复制 | trail + DXGI format / LUID + GPU 时间 | 统一 D3D11 device，直接传递 decoder texture。 |
| 编码前 CPU 占用高、YUV conversion 耗时高 | RGB/BGR 先回读，再由 CPU 转 NV12/P010 | conversion 段时间和 `Map` 次数 | HLSL 输出 NV12/P010 target，直接提交硬编 surface。 |
| GPU render 低而 Python CPU 高 | 每帧 Python 调度、资源新建或同步调用 | CPU sampling、resource counter、GPU timestamp | 批量 command、固定纹理池、把热路径放入 native bridge。 |
| render p95 尖峰、GPU compute 高 | shader group、临时纹理、带宽或分支 | 逐 pass timestamp、GPU capture | 合并 pass、调整 thread group、复用 intermediate texture。 |
| decode 或 encode submit 阻塞 | surface 环太浅、fence 归还太晚、B-frame 队列 | queue depth、wait reason、encoder event | 增加有界 in-flight ring，按 fence 归还，禁止每帧 wait。 |
| 两轨 / 四轨吞吐骤降 | 源预取串行、显存压力、decoder session 竞争 | 各源 decode 时间、显存峰值、surface lease | 独立 decode queue、限额预取、按时间线淘汰缓存。 |
| 输出色彩或 10-bit 不一致 | matrix/range/transfer 或 P010 bit 对齐错误 | raw reference、metadata probe、码值差 | 先修色彩契约，速度结果作废后重测。 |

## 8. 平台矩阵和发布规则

| 目标机 | 已有证据 | 本计划仍需补的真实测试 |
|---|---|---|
| NVIDIA RTX 4090 + Intel UHD 770 | 既有 NVDEC/NVENC 基线和能力报告 | T0-T6 的 D3D11 surface 时间线导出、跨 adapter / iGPU-dGPU 选择。 |
| AMD RX 6800 | 既有 D3D11VA/AMF 基线和能力报告 | T0-T6、AMF D3D11 surface、驱动差异和 HDR P010。 |
| Intel Arc 或 QSV iGPU | 无本计划要求的端到端验收 | oneVPL D3D11 decode / encode、T0/T1/T5，随后 T2-T6。 |
| Optimus / 混合显卡笔记本 | 仅有 adapter 规则与桌面机证据 | 电源状态、GPU 选择变化和 LUID mismatch 的真实回退。 |

CI 只运行能力契约、fallback reason、结果 schema 与 deterministic CPU/HLSL reference 测试。硬解、互操作和真实性能必须在自托管 Windows GPU 机器上跑；GitHub 的通用 Windows runner 不能作为 GPU 性能证据。

## 9. 可追溯的完成定义

Windows 全 GPU 时间线导出完成，必须同时满足：

1. T0-T6 在每个宣布支持的 vendor / codec / bit-depth 组合有可复现真实报告；
2. `ExportGpuTrail` 证明像素帧从解码完成到硬编提交零 CPU upload/download；
3. 内建效果、混合、转场、HDR 的画质门禁通过，未支持项在产品中清晰展示；
4. T0/T1 的吞吐达到本计划门槛，所有低于基线的项目已有段级归因和修复记录；
5. 多 adapter、驱动缺能力和编码器失败均安全回退，不影响导出正确性。

## 10. 官方资料

- [Microsoft：Media Foundation 中的 Direct3D 11 视频解码](https://learn.microsoft.com/en-us/windows/win32/medfound/supporting-direct3d-11-video-decoding-in-media-foundation)
- [NVIDIA Video Codec SDK：NVENC 编程指南](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.1/nvenc-video-encoder-api-prog-guide/)
- [AMD Advanced Media Framework](https://github.com/GPUOpen-LibrariesAndSDKs/AMF) 和 [AMF 与 FFmpeg 的 D3D11 加速示例](https://github.com/GPUOpen-LibrariesAndSDKs/AMF/wiki/FFmpeg-and-AMF-HW-Acceleration)
- [Intel oneVPL：Windows Direct3D 硬件加速](https://intel.github.io/libvpl/latest/programming_guide/VPL_prg_hw.html)

## 11. 执行记录（2026-09-15，AMD RX 6800 真机）

本机：Windows 11 + Python 3.12 + AMD Radeon RX 6800（adapter LUID `00011E23-00000000`，FL 0xC100）。所有产物有 JSON/Markdown 配对归档与 pytest 契约测试钉死。

### 11.1 步骤 0：测量协议与基线 —— 基础已建立

| 产物 | 位置 | 状态 |
|---|---|---|
| `ExportGpuTrail` 证据契约 | `src/shared/export/gpu_trail.py` + `tests/export/test_gpu_trail.py` | 完成（段级后端/GPU 时间/CPU 上传回读字节/回退 reason；"全 GPU"声明必须由 trail 证明） |
| 时间线导出基准脚本 | `scripts/bench_timeline_export_windows.py` + `tests/media/test_bench_timeline_export_windows.py` | 完成（T0–T6 用例矩阵；三组对照 cpu_pipeline / hw_download / ffmpeg_gpu_ref；预热 60 + 3×480 帧中位数协议；T2–T6 未就绪用例诚实声明 blocking 原因） |
| T0 真机基线 | `tests/media/results/windows_timeline_export_amd_rx6800_2026-09-15.{json,md}` | 完成（三组全通，输出 SHA 三次稳定） |
| T1 基线 | 同脚本 `--cases T1` | 部分接线：resize + yuv420p 色彩转换可跑；主级调色的效果实例程序化构建未接线（coverage 字段显式披露，不冒充完整 T1） |
| T2–T6 | 同脚本声明 | `declared_not_run`，blocking 原因逐项记录 |

T0 基线数字（中位 FPS，2K H.264 → H.264 20 Mbps VBR，音频关闭）：

| 组 | 中位 FPS | 说明 |
|---|---|---|
| cpu_pipeline | 22.87 | 纯 CPU（`TOPOS_DISABLE_WIN_HW_DECODE/ENCODE`） |
| hw_download | 22.99 | 生产默认（硬解下载 + AMF），仅比纯 CPU 快 0.5% |
| ffmpeg_gpu_ref | 149.99 | `gpu_surface` 模式（d3d11va surface → h264_amf）一次成功 |

**现状结论**：现有 Topos 时间线导出只达到同机 FFmpeg GPU 链路的 **15.3%**，且 hw_download ≈ cpu_pipeline——当前时间线被 CPU 渲染/转换卡死，硬解硬编的收益被 CPU 中段吞掉。这就是本计划要消除的部分（§4.3 的 85% 门禁适用于未来 D3D11 路径；此值是现状基线）。

附带修复：`ffmpeg_locator` 仓库根解析 bug（`parent.parent` → `parents[2]`，仓库根 `bin/` 下 ffmpeg 永远找不到）；`bin/ffmpeg.exe`（gyan.dev 9.0.1）作为机器本地工具链放入并加入 `.gitignore`。

### 11.2 步骤 1：D3D11 device 生命周期与纹理池 —— 本机门禁通过

| 产物 | 位置 |
|---|---|
| device 生命周期 + LUID 走查 + 格式支持预检 | `src/shared/media/windows_hw/d3d11_device.py` |
| 定长纹理池（封口/lease/反压/审计） | `src/shared/media/windows_hw/d3d11_texture_pool.py` |
| D3D11 版 GpuDecodedFrame 契约 | `src/shared/media/windows_hw/d3d11_frame.py` |
| 真机门禁 probe | `scripts/probe_d3d11_device_pool_windows.py` → `tests/media/results/d3d11_device_pool_probe_amd_rx6800_2026-09-15.{json,md}` |
| 契约测试 | `tests/media/test_windows_hw_d3d11_device_pool.py`（注入式 + 真机类） |

真机发现（已固化进代码）：

1. `D3D11CreateDevice` 不传 FL 列表时最高只给 11.0（0xB000）——必须显式请求 12.1→11.0 阶梯，本机得 0xC100；
2. vtable 实序含 `GetImmediateContext=[27]`：`OpenSharedResource=28`、`CheckFormatSupport=29`、`GetFeatureLevel=37`（创建后健全性检查钉死，错位 fail-closed）；
3. `CheckFormatSupport(P010)` 与手动 `CreateTexture2D(P010)` 在本机全部失败（E_FAIL / E_INVALIDARG）——**本机 AMD 驱动不支持手动创建 P010 纹理**；全链路 P010 surface 必须来自解码器/编码器自有分配，池的 P010 路径 fail-closed；
4. `BIND_DECODER|SHADER_RESOURCE` 组合创建被拒（E_INVALIDARG）——池纹理默认只用 `SHADER_RESOURCE`；`MISC_SHARED_KMT` 可用（后续编码互操作备用）。

门禁结果：device 创建 + LUID 走查 ✅；NV12 池 1,000 帧 created 恒 8 / peak≤8 ✅；深度 6 四线程并发环有界 ✅。**NVIDIA RTX 4090 实机的同套 probe 待自托管机器执行**（`scripts/probe_d3d11_device_pool_windows.py` 可直接复跑）。

### 11.3 步骤 2：零回读 T0 链路 —— 校验层完成，原生桥边界 fail-closed

| 产物 | 位置 | 状态 |
|---|---|---|
| `GpuEncodeSurface` 契约 + 编码 surface 提供者 | `src/shared/media/windows_hw/gpu_encode_surface.py` | 提交前校验真实（同卡 / 格式 / 尺寸 / 词表）；`submit_to_encoder` fail-closed `not_yet_implemented` |
| T0 直通链编排器 | `src/shared/media/windows_hw/d3d11_passthrough_chain.py` | 三段 LUID 一致性 + 逐帧 surface 流转 + trail 计数真实；解码源与硬编提交是显式 fail-closed 边界 |
| 分层 zero-copy probe | `windows_zero_copy_probe.py` 升级 | device/格式/纹理池层真机执行；`zero_copy` 保持 False 直到解码层真实可用 |
| 契约测试 | `tests/media/test_windows_hw_t0_chain.py` | LUID mismatch、submit 边界、解码耗尽、所有权边界（链只归还其拿到的帧）全部钉死 |

**步骤 2 的真实验收（`cpu_upload_bytes == 0 && cpu_download_bytes == 0` 的可播放码流）尚未达成**——解码 surface（MF D3D11VA）与硬编 surface 提交（AMF/NVENC 互操作）需要原生桥。按 §6 步骤 2 的禁止条件：**在 T0 通过前不得开始 HLSL 效果迁移（步骤 3+）**。

### 11.4 下一步（按 §6 顺序）

1. **原生桥 `topos_hwdecode_windows`**（`native/` 下新建 CMake 工程，范式复制 topos_codec）：MF D3D11VA 解码 surface 导出 + AMF DX11 surface 提交接口，Python 侧经 `DecodeSurfaceSource` 协议与 `submit_to_encoder` 边界接入；
2. NVIDIA RTX 4090 实机复跑 `probe_d3d11_device_pool_windows.py` 与 `bench_timeline_export_windows.py`，补齐双机矩阵；
3. T0 全链路验收（零回读 + 码流可播放）→ 达成后才进入步骤 3（最小 D3D11 render）。

### 11.5 导出提速：h264 yuv420p 平面直通（2026-09-15 第二轮）

针对"现有时间线导出仅 15.3% FFmpeg 参照"的差距，在现有管线内打通了
**单层无效果时间线的平面直通**（解码 yuv420p 平面 → 编码器同格式直填，
消除 NV12→BGR→yuv420p 双重 CPU 转换）：

| 改动 | 位置 | 内容 |
|---|---|---|
| 直通门格式参数化 | `timeline_export.py` `_try_render_planar_passthrough` | `_PASSTHROUGH_FORMAT_SPECS` 白名单（topos+yuv422p10le 既有语义不变；新增 h264+yuv420p 8bit 3 平面 uint8/420 形状契约）；h264 未标注 chroma siting 按 left(0) 缺省（与 ProRes 豁免同构，x264/AMF 约定） |
| burn_subtitles 逐帧判据 | 同门 | 该开关默认 True（惰性语义），改为与 `_burn_in_subtitles` 实际改帧判据逐帧一致：无活跃字幕的帧允许直通，查询异常 fail-closed |
| 编码器零转换内联快路 | `video_encoder.py` `encode_frame` | 平面帧与 `_encode_pix_fmt` 同格式时同步直填+编码（转换队列的线程交接与 pts 重排只服务需要转换的帧）；`_is_zero_conversion` 与 `_convert_planar_frame` 直填条件同构，首个非直填帧起回归队列路径，全局保序 |
| 平面填充去中间拷贝 | `_create_planar_av_frame` | buffer 协议直传 plane.update，省 tobytes() 整帧拷贝 |

真机复测（AMD RX 6800，T0/T1 全协议归档于
`windows_timeline_export_amd_rx6800_2026-09-15.{json,md}`）：

| 指标 | 优化前 | 优化后 | 变化 |
|---|---|---|---|
| T0 生产路径（hw_download 组，中位） | 22.99 fps | **139.02 fps** | **6.0×** |
| T0 vs FFmpeg GPU 参照比值 | 15.3% | **91.7%** | 超过计划 85% 门禁 |
| 4K 真实导出（demo，240 帧） | 8.57 fps | **33.47 fps** | 3.9× |
| T1（resize，须走合成路径） | — | 27.1 fps | 未变（属步骤 3+ 范畴） |

**边界与诚实标注**：纯编码吞吐实测 161 fps（6.21 ms/帧，直填+AMF+mux），
与 FFmpeg CLI 的 150 fps 同为 AMF 硬件+上传路径的物理天花板；端到端
139 的剩余差距是解码/胶水的 GIL 开销与 AMF 包装层的逐帧 CPU→GPU 上传。
要完全追平并超越 FFmpeg（消除上传），唯一路径是计划步骤 2–4 的原生桥
（D3D11 decode surface 直送 AMF encode surface）——契约层已就绪
（§11.3）。T2+（效果/多层/转场）不适用直通，仍走渲染路径。

### 11.6 HEVC 8-bit 平面直通 + CPU/GPU 自适应选路（2026-09-16 第三轮）

按"平面直通格式扩展 → CPU/GPU 自适应"的既定顺序（§11.5 边界标注的
T2+ 渲染路径优化前置项），在现有管线内完成两项：

| 改动 | 位置 | 内容 |
|---|---|---|
| HEVC 8-bit 交付档 | `timeline_export.py` `_get_codec_settings`/`_normalize_hevc_profile` | `pixel_format` 显式覆盖限 `yuv420p`/`yuv420p10le` 域透传；未设置保持 10-bit 默认（直配 TimelineExportConfig 的调用方行为不变）；profile 按 pix_fmt 归一（main/main10，"auto" 占位与冲突值不再直传 avcodec） |
| HEVC 直通规格 | `_PASSTHROUGH_FORMAT_SPECS` | 新增 `hevc/yuv420p/8bit/420` 规格且 `scalar_delivery=True`；`supports_scalar_delivery`/`resize_delivery` 从硬编码 `codec=="h264"` 泛化为 spec 字段——HEVC 同享 YUV 域 resize 与 opacity/fade；未标注 siting 按 left(0) 缺省豁免（x265/AMF/NVENC 与 x264 同约定）；`_encode_frame` 白名单与 GPU 直出门 `_gpu_yuv420_output_allowed` 同步扩展 |
| HEVC profile 修正 | `video_encoder.py` `_infer_profile` | hevc 8-bit 推断 main（原返回 "high" 是 H.264 专属名，libx265 不识别）；AMF/NVENC/VideoToolbox 的 hevc+yuv420p 输入表既有即支持，零转换快路格式无关 |
| deliver 位深档 | `video_export_job.py` `_hevc_pixel_format_override` + 设置面板 | HEVC 域内透传 pixel_format（历史队列 profile=main10 语义优先）；pix_fmt 选择器复用于 HEVC 8/10-bit 档（与 profile main/main10 双向联动，补齐 R7 遗留的 combo 联线）；hevc_uhd 预设本即 yuv420p/profile main——引擎侧从此尊重该意图 |
| CPU/GPU 自适应 | `timeline_export.py` `_adaptive_gpu_composite_skip_reason` | 主机可见源帧（OpenGL 后端）仅在帧内有 GPU 值得的逐像素工作（多层/转场/调色/特效/降噪/几何变换/混合模式/帧混合变速）时走 GPU 合成；"直通形状但源契约不满足"的单层帧直接 CPU 合成，跳过 host↔GPU 往返（依据 §11.1：hw_download≈cpu_pipeline、合成路径 27fps vs 直通 139fps）；Metal native surface 不受影响；`TOPOS_EXPORT_GPU_RENDER=1` 显式强制时让位；跳过原因进 GPU trail（`adaptive_policy:cpu_preferred_*`） |

测试：`test_timeline_export_planar_passthrough.py` 38 用例（HEVC 正/负例：
untagged 豁免、YUV resize、opacity/fade、10-bit 目标 fail-closed、untagged
h264 源拒绝、center siting 拒绝、full range 拒绝）、GPU 门/编码分派与自适
应策略 41 用例、deliver 透传与 UI 选择器用例、`_infer_profile` 位深档用
例全绿；`tests/edit`+`tests/video` 全量 8372 通过，失败集合与基线
（c0ccc208）逐字节一致（均为 libtopos_codec 本地库过期/real-media 探针
等预存问题）。真机 HEVC 素材的端到端回读（色彩/帧数/PTS）与基准复测
待按 §4 规程在 AMD/NVIDIA 双机补录。

#### §11.6.1 真实素材 A/B 复测（2026-09-17，AMD RX 6800）

按 §4 规程在真实素材上做改动前（c0ccc208 worktree）vs 改动后（05e40e30）
同脚本 A/B（`scripts/bench_export_effects_matrix.py`，28 配置 × 每配置
30 帧预热 + 2×240 帧计时取中位，子进程隔离，20 Mbps VBR，输出 PyAV
probe 钉死 codec/profile/pix_fmt/帧数）。素材：`2K_h264.mp4`（h264 8bit）、
`2K_H265.mov`（hevc Main10 10bit）、`.cache/2K_hevc8_gen.mp4`（bin/ffmpeg
x265 生成的 8-bit 420 源，600 帧）。

| 主张 | 配置（source/codec/scenario） | baseline | dev | 增益 |
|---|---|---|---|---|
| **HEVC 8-bit 直通** | hevc8/hevc8/identity | 21.1 | **190.1** | **9.0×** |
| **HEVC 8-bit 直通** | hevc8/hevc8/resize(720p) | 26.8 | **276.4** | **10.3×** |
| 自适应选路 | h264/hevc10/identity | 20.8 | 38.9 | 1.87× |
| 自适应选路 | h264/hevc8/identity | 22.4 | 35.5 | 1.59× |
| 自适应选路 | h264/hevc10/resize | 24.1 | 38.0 | 1.58× |
| 自适应选路 | hevc8/h264/identity | 25.6 | 37.1 | 1.45× |
| 自适应选路 | hevc8/h264/resize | 25.2 | 35.7 | 1.41× |
| 自适应选路 | h264/hevc8/resize | 26.4 | 35.6 | 1.35× |
| 控制组（应不变） | h264/h264/identity | 130.5 | 131.7 | 1.01× |
| 控制组 | h264/h264/grade（GPU 值得） | 24.1 | 23.9 | 1.00× |
| 控制组 | h264/h264/two_layer | 14.2 | 14.5 | 1.02× |
| 控制组 | h264/h264/transition | 67.0 | 67.2 | 1.00× |
| 控制组 | h264/h264/blur | 9.4 | 9.1 | 0.97× |

结论与诚实边界：

1. HEVC 8-bit 直通在 HEVC 8-bit 源上兑现 9–10×（190/276 fps），输出
   probe 全部 `hevc/Main/yuv420p`，帧数 240 精确。
2. 自适应策略对"直通形状但源契约不满足"的导出（跨 codec/跨位深源）
   兑现 1.35–1.87×——跳过的是 GL 上传/读回而非跳过渲染本身；带逐像素
   工作的帧（grade/blur/two_layer/transition）两侧同为 GPU 路径（0.97–
   1.08×），策略未误伤。
3. 预存问题（非本轮引入，与 baseline 同值）：`TOPOS_EXPORT_GPU_RENDER=0`
   时 grade 导出 1.7 fps（纯 CPU 调色路径病态慢，值得单独立项）；blur
   导出在三种 GPU 组下均 ~9 fps（`_CPU_EFFECT_HANDLERS` 主导，GPU 无关）。
4. 位深/codec 门按设计 fail-closed：10-bit HEVC 源 → 8-bit 档（19.8 fps
   合成）、h264 源 → HEVC 档（untagged siting 豁免按 spec 隔离）均拒绝
   直通。跨 codec untagged 豁免（h264↔hevc 源）是后续可选扩展。
5. 预览帧率：本轮全部改动位于导出管线（直通白名单/编码档/自适应），
   预览路径零改动，预览帧率按设计不变；预览渲染器与 UI 绘制管线深度
   绑定，无头环境无法公平测帧率，不做不可信数字。
6. 单次测量噪声实测可达 4×（首次冷缓存），协议固定为 2 次取中位 +
   子进程隔离；数据与配对 JSON/MD 归档于
   `tests/media/results/export_effects_matrix_{dev,baseline,ab}_2026-09-17.*`。

#### §11.6.2 第二轮四项修复（2026-09-17，perf/export-round2）

基于 §11.6.1 暴露的问题逐项修复，每项独立提交并用同一基准协议
（240 帧 ×2 取中位，RX 6800 + AMF）真机复测：

| 提交 | 修复 | 实测增益（当日同机对照） |
|---|---|---|
| 1e87153f | 跨编码器 untagged 豁免 + hevc 10→8bit YUV 域降位直通 | h264→hevc8 identity 35.5→146.9（4.1×）、resize 35.6→179.8（5.1×）、opacity_fade 19.3→75.9（3.9×）；hevc10→hevc8 19.8→72.8（3.7×）；hevc8→h264 37.1→121.9（3.3×） |
| 4fe0f9a0 | OpenGL 导出渲染器 pass2.5：blur 经 EffectPassRunner GPU 化（与 Metal 门同构 fail-closed） | blur 9.1→20.8（2.3×；此前 GPU 开关三组同速，根因是 OpenGL 分支一刀切整帧回落 CPU） |
| 719c2dfa | apply_grading 保持输入 dtype + 削减全帧临时（CPU-only grade 病态慢的根因：float64 升级 + ~25 个全帧临时） | apply_grading 589→335ms（1.76×），CPU-only grade 1.55→2.41fps（1.55×） |
| f997a418 | `_gpu_yuv420_output_allowed` 的 burn_subtitles 从硬拒绝改为与直通门一致的逐帧判据（默认惰性语义此前把所有 GPU 合成导出锁死在 BGR 回读 + CPU swscale） | grade 23.9→28.0（1.17×）、two_layer 13.5→16.1（1.19×）、blur 20.8→25.5（累计 2.8×）；identity/transition 同档 |

正确性证据：直通 probe 全部正确（codec/profile/pix_fmt/帧数）；blur
GPU 输出 Laplacian 方差 864（源）→10.4，与 CPU 输出 MAD 2.7/255（预览
同源 GLSL）；GPU YUV 直出 vs CPU 合成 MAD ~1/255；apply_grading 新旧
实现逐分支/全组合 bit 级一致（maxdiff=0），38 项调色测试全过。

分阶段结论（§11.6 目的达成情况）：GPU 合成本体已证明不是瓶颈
（pass1+grade 实测 0.7ms/帧），剩余在 decode（~15ms）、回读与编码侧
转换的内存往返。完整 D3D11 零拷贝桥（W3）仍是下一档结构性收益，
纯 ctypes 可行性已由 `wgl_shared_texture_roundtrip_amd_rx6800.md`
钉死；本轮未实施。

新发现的独立小 bug（未修，待立项）：CPU 链首帧 `_get_layer_frame`
竞态丢帧（每次导出恰好一次，输出首帧黑帧）。

#### §11.6.3 复查修复（2026-09-17）

对 §11.6.2 四项修复的整体复查发现并修复三处问题（前两处为**预存**
bug，复查实证钉死）：

1. **OpenGL GPU 导出静默丢弃 clip transform/crop**（重大，预存）：GL
   渲染器不实现 clip 几何（元素元组的 transform/crop 槽位仅 Metal
   消费），且无上游资格门。实证：2x scale clip 的 GPU 输出 vs CPU
   合成 MAD 13.7/255（输出直接错误）。修复：`_gpu_render_composite`
   对非 Metal 后端强制几何契约（非恒等 transform/crop → fail-closed
   回落 CPU）；修复后 GPU/CPU 输出差 2.27 = 编码器两次运行的噪声
   基线（同路径 CPU vs CPU 亦为 2.27）。
2. **wipe 转场侧层的 clip 效果在 OpenGL 下静默丢弃**（预存）：side
   effects 槽位仅 Metal 填充，而侧层解析 `apply_effective_opacity=False`
   不会自行拒绝。修复：wipe 分支对非 Metal 后端检查侧层效果 → 整帧
   fail-closed。
3. **10→8 降位直通的元数据残留**（§11.6.2 引入）：输出
   DecodedFrame.metadata 的 bit_depth/pixel_format 仍为 10-bit 域。
   修复：dataclass_replace 随交付域改写（下游按 metadata 判读时不再
   拿到矛盾值；plane_infos/pixel_format 本已正确）。

复查排除项：EffectPassRunner scratch 默认 LINEAR filter（后续缩放采样
无质量损失）；pass2.5 scratch ping-pong 复用链（separable/single-pass
交替读写不同纹理，无反馈环）。基准 sanity：identity/grade/blur/two_layer
四场景同档无回退。

#### §11.6.4 第三轮：pass2.5 全效果放行 + CPU grade 分解快路（2026-09-17）

| 提交 | 内容 | 实测（1080p，RX 6800，当日对照） |
|---|---|---|
| 832bd5a2 | pass2.5 白名单 blur→全部 7 种内置效果；修复 `_run_glow` 在 standalone context 的 QOpenGLFunctions 崩溃（改 `Texture.use(1)`） | sharpen 6.9→25.4fps（3.7×）、glow 5.7→27.9（4.9×） |
| fae9ae8f | saturation 线性分解：逐通道 LUT 前缀 + 3×3 矩阵（cv2.transform）；highlights/vibrance/hue_shift 维持浮点 | CPU-only grade 2.41→**27.76fps**（11.5×），与 GPU 合成档持平 |
| cee5aae5 附属 | 顺手项：首帧黑帧根因修复 + 过期 16-bit 域断言 | 首帧亮度 0.0→88.1 |

**首帧黑帧破案**：非竞态——陈旧 libtopos_codec DLL（缺
`tc_dev_symbol_hist_plane_get`）毒化进程内首次 open_clip，第二次加载
走回退成功；原代码把瞬时失败当永久丢失 → 首帧黑。修复：open 失败
立即重试一次。**根治需重建原生库**（cmake+clang，本机 shell 无工具链，
待维护者执行 `bash native/topos_codec/run_tests.sh`），重建后同批
13 个 libtopos 测试与 3 个 export_outcome 测试预计一并恢复。

**①a 结论（解码段）**：Windows 导出有意使用 CPU 解码（PyAV D3D11VA
同步 GPU→CPU 下载 ~47fps vs CPU 帧线程 ~486fps，见 factory.py 注释
bench 依据）——宿主可见帧前提下这是最快选择，无便宜账可捡。

**①b 阻塞分析（D3D11 零拷贝桥）**：Python 侧架构完全就绪——
`WindowsNativeBridge` Protocol、D3D11GLBridge（ring/fail-closed/
fallback reason）、d3d11_sync（keyed mutex）/texture_pool/effects/
pipeline、`_ExternalGpuSurfaceEncoder` 注入位、`gpu_encode_surface`
provider 合同均已落地并有测试；W3 探针钉死 ctypes interop 契约。
**缺口正是原生扩展本体**（decoder/encode/interop 的 C 实现）——
纯 ctypes 只覆盖 GL↔D3D11 纹理互操作，AMF D3D11 输入需要
PyAV 不暴露的 hw_frames_ctx 通道，必须走原生桥。立项路径：
按 `native_bridge.py` Protocol 实现 C 桥（可先 encode-only：
BGRA 共享纹理 → D3D11 VideoProcessor 转 NV12 → AMF，解码段后置），
 Python 侧无需改动即可被 `_gpu_encode_surface_provider` 注入位消费。

> 复现协作记录（2026-09-17，macOS Intel i9-9900K 侧）：按"CPU 链首帧
> 黑帧"在本机未复现——单 clip 时间线 × 1-3 层 × CPU/GPU 两链 × h264 与
> Topos 源共 11 次导出，输出首帧 luma 均正常（`tools/repro_first_frame_black.py`，
> 首 3 帧 luma 差 <2/255）。本侧 `_get_layer_frame` → provider 解码为
> 同步串行路径，未见明显首帧竞态窗口；怀疑触发条件与 Windows 侧异步
> 初始化有关，留待有失败场景的一侧定位，工具已入库可复跑。
