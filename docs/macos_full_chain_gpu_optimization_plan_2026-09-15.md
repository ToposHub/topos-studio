# macOS 全链路 GPU 优化与真实素材验收计划

> 版本：执行版 1.0
> 日期：2026-09-15
> 适用范围：Topos Color 在 macOS 上的 Edit、Color、Composite、导出和 Topos 编解码路径
> 目标：让支持的素材和效果从硬件解码或 Topos GPU 解码开始，经过 Metal 调色、合成、YUV 转换，直接交给硬件或 Metal 编码 surface；CPU 只负责调度、压缩码流封装和最终写盘。
> 当前状态：Metal 时间线渲染已经可用，部分效果已经覆盖，但最终编码前仍有 GPU 到 CPU 回读，尚未达到本计划定义的全链路 GPU。

## 1. 全链路完成的定义

本计划把像素链路和文件链路分开。CPU 可以处理工程解析、时间线调度、音频、压缩码流封装和磁盘写入；CPU 不得接触从解码完成到硬件编码提交之间的原始像素。

    VideoToolbox CVPixelBuffer / Topos IOSurface
        -> CVMetalTextureCache / MTLTexture
        -> Metal render graph
           （色彩管理、缩放、调色、合成、转场、滤镜）
        -> Metal NV12 / P010 / 422 / RGBA 编码 surface
        -> VideoToolbox / Metal Topos encoder
        -> compressed packet
        -> CPU mux / metadata / disk write

只有同时满足下面各项，导出才能标为全链路 GPU：

| 门槛 | 要求 |
|---|---|
| GPU 解码 | 解码阶段有 VideoToolbox 硬件时间戳，或 Topos Metal 解码 GPU 时间戳；CPU 解码只能标为混合路径 |
| GPU 渲染 | Metal command buffer 覆盖所有声明支持的色彩、缩放、合成、转场和效果 |
| GPU YUV 转换 | NV12、P010、422 或 RGBA 输出在 GPU target 上生成，不经 NumPy、CPU staging 或 BGR 中间帧 |
| GPU 编码提交 | 编码器直接取得同一 Metal device 或同一 IOSurface 的 GPU surface |
| 零像素搬运 | ExportGpuTrail 的 cpu_upload_bytes 和 cpu_download_bytes 均为 0 |
| 证据完整 | decode、render、yuv_conversion、encode_submit 四段均有真实 GPU 时间戳，fallback_reason 为空 |

ExportGpuTrail.full_gpu_claim_reason() 返回空字符串之前，不得在 UI、日志或文档中声称全链路 GPU。

## 2. 当前状态盘点

### 2.1 已完成并有证据的内容

| 编号 | 内容 | 当前结果 | 证据与边界 |
|---|---|---|---|
| M-D01 | Metal 设备与计算能力探测 | 已完成 | RX 6900 XT 上 MTLDevice、Metal 3、MSL 编译、compute dispatch、CVMetalTextureCache、NV12/P010/BGRA 导入均通过；探针：tools/probe_metal_capability.py |
| M-D02 | VideoToolbox NV12/P010 零拷贝导入 | 已完成（支持范围内） | VT frame 可通过 CVMetalTextureCache 产生 Metal texture；CoreVideo 返回值必须显式判断为 0 |
| M-D03 | Topos 422 IOSurface 三平面导入 | 已完成（输入端） | ToposMediaSource.read_frame_metal 将 native decoder 结果写入 IOSurface Y/U/V 平面，Metal 以 R16Uint 导入 |
| M-D04 | Metal 时间线渲染器 | 已完成（时间线子集） | TimelineMetalExportRenderer 已覆盖 Metal texture 输入、色彩转换、调色、缩放、部分效果、图层合成和高位深输出 |
| M-D05 | 常用效果 Metal 实现 | 已完成（范围有限） | Blur 的 Box/Gaussian/Bilateral/Motion/Bokeh、Defocus、Erode/Dilate、Edge Detect、Denoise，以及几何、裁切、opacity 和 16 种混合模式均有不同程度覆盖 |
| M-D06 | 效果真实素材导出 | 已完成（子集） | tests/test_videos/2K_h264.mp4 的 480 帧真实导出已验证多种效果，结果经 ffprobe 核对且无 renderer fallback |
| M-D07 | 多素材 Metal 预取 | 已完成（子集） | 两个独立 VT session 可并行预取；双素材 Overlay 实测均值约 101.266 FPS |
| M-D08 | Topos 422 输入端优化 | 已完成（输入端） | 2K Topos 422 Proxy 解码阶段由约 177 FPS 提升到约 194 FPS；Metal texture 帧和 GPU YUV 输出均计数 |
| M-D09 | 真实时间线基线 | 已完成 | Topos Proxy 到 HQ 的 96 帧 Metal 导出：2K 约 44.095 FPS，4K 约 17.964 FPS；Metal composite 96/96，direct planar passthrough 0/96 |
| M-D10 | GPU 证据契约 | 已完成 | shared/export/gpu_trail.py 记录像素阶段、GPU 时间、上传/下载字节和回退原因 |

### 2.2 已完成但不能误读为全链路的结果

| 结果 | 正确解释 |
|---|---|
| Metal composite 96/96 | 只说明 Metal 渲染成功，不代表编码器直接消费 Metal surface |
| Topos IOSurface 输入 | 只说明输入端避免显式上传；Topos entropy decode/reconstruction 目前仍是 native CPU |
| GPU YUV 输出 | 当前输出 buffer 仍会被 read_buffer 读回 NumPy，存在 GPU 到 CPU 像素回读 |
| VideoToolbox 硬解 | 只适用于受支持的 H.264/HEVC/bit-depth；当前 Intel + RX 6900 XT 机器没有 ProRes VT 硬解 |
| 单效果 FPS | 是真实完整导出或 GPU render 子集的测量，不能外推到所有页面和 4K 全分辨率 |
| Metal 设备探针通过 | 只证明 API、纹理格式和 compute 可用，不证明应用导出链路已接通 |

### 2.3 尚未完成的阻塞项

| 编号 | 未完成项 | 当前原因 | 完成标志 |
|---|---|---|---|
| M-B01 | Topos 熵解码、反量化、重建完全 GPU 化 | native Topos decoder 仍主要使用 CPU C/SIMD | Metal decoder kernel 输出与 CPU reference 一致的 IOSurface，decode 段有 GPU timestamp |
| M-B02 | Metal 输出直接进入编码器 | 已覆盖的单层与 Metal stack 子集可直接写 VideoToolbox IOSurface；不支持的时间线和 host-visible 编码仍调用 read_buffer 并创建 NumPy planes | VideoToolbox 使用 IOSurface/CVPixelBuffer，Topos encoder 使用 Metal/IOSurface 输入 ABI，下载字节为 0 |
| M-B03 | 删除逐帧同步 | 部分路径仍使用 finish 或同步读回 | 使用 completion handler、fence 和有界 ring，生产帧循环不调用逐帧 finish/readback |
| M-B04 | Color 页 Metal 导出 | 实时渲染和部分 scope 仍由 OpenGL render thread 完成 | Color render graph 返回 GPU-owned texture/plane lease 并进入统一 Metal encode path |
| M-B05 | Composite 页 Metal 导出 | headless 导出仍可能最后读回 BGR，GPU graph 接口未成为主路径 | Composite 节点、mask、blend、transform 全部输出 Metal-owned resource |
| M-B06 | 空间转场 mask GPU 化 | mask 仍由 CPU compute_transition_mask 逐帧生成后上传 | wipe、iris、slide、user transition mask 在 Metal kernel 生成，上传字节为 0 |
| M-B07 | 插件 shader 跨后端 | GLSL 插件不能自动等价转成 MSL，部分插件仍回退 | EffectSpec/EffectCapability 增加 graphics backend 能力；声明支持的插件均有 MSL/golden test |
| M-B08 | 时间性效果和特殊输入 | temporal denoise、frame-blend retime、velocity-map Motion、复合片段/3D 尚未完全迁移 | 每项定义历史帧 lease、同步和色彩语义后，通过真实素材验收 |
| M-B09 | Alpha 与高位深全覆盖 | 422 P10 已有路径，4444/XQ、alpha 和部分格式组合仍需逐项验证 | RGBA16F、P010、422、4444/XQ 源/目标矩阵均通过正确性和零回读门禁 |
| M-B10 | Metal 预览表面全面替换 | Qt/OpenGL viewport 仍是主要预览表面 | 先保证 CAMetalLayer 与 QOpenGLWidget 共存，再按页面切换，不破坏 GL fallback |

## 3. 目标架构与数据契约

### 3.1 后端职责

| 后端 | 负责内容 | 继续保留的理由 |
|---|---|---|
| Metal graphics | macOS 时间线导出、Color/Composite GPU render、Metal preview surface | macOS 主 GPU API，支持 compute、IOSurface 和统一资源生命周期 |
| OpenGL/ModernGL | 现有预览、兼容效果、未迁移插件、旧硬件回退 | 现有 UI 仍依赖 QOpenGLWidget；也是可靠回退 |
| VideoToolbox | H.264/HEVC 等格式的硬解和硬件编码 | 专用视频单元速度高，输入输出保持 CVPixelBuffer/IOSurface |
| Topos Metal codec | Topos entropy/decode/encode GPU kernel 和 GPU surface ABI | 让自研 Topos 压缩和重建步骤纳入严格全 GPU 链路 |
| CPU | 时间线调度、音频、压缩 packet mux、元数据和写盘 | 这些属于允许的文件链路边界 |

### 3.2 GPU frame contract

每个 GPU frame 必须携带：

- resource 类型：MTLTexture、IOSurface 或 CVPixelBuffer；
- plane 的宽、高、stride、像素格式、bit depth、alpha；
- matrix、range、primaries、transfer、HDR metadata；
- frame index、PTS、duration；
- Metal device identity；
- lease、fence、release callback；
- source kind 和 fallback reason。

硬规则：

1. 不允许把 GPU frame 隐式转换成 ndarray、BGR、RGB 或 CPU VideoFrame。
2. 资源池必须有上限；command buffer 完成前不能回收 surface。
3. Metal device 不一致时立即返回 device mismatch，不能静默复制。
4. unsupported 组合必须返回机器可读 reason，并在报告中计入 fallback。
5. 允许把压缩 packet 交给 CPU mux；不允许把原始 YUV/RGB 平面交给 CPU 再编码。

### 3.3 渲染图编译

时间线、Color 节点图和 Composite 节点图发生结构或参数版本变化时编译一次 RenderPlan：

- 可见图层和资源依赖；
- source texture 与输出格式；
- 色彩管理 pass；
- effect pass 和参数 buffer；
- 中间纹理别名；
- command queue/fence 依赖；
- 编码 surface 生命周期。

正式 480 帧循环中不得重复解析节点图、编译 shader、创建 pipeline state 或逐帧申请 transient texture。

## 4. 分阶段执行表

每阶段必须先完成代码、契约测试、真实素材导出和报告，才能开始下一阶段。

| 顺序 | 阶段 | 主要工作 | 产物 | 进入下一阶段的门禁 |
|---:|---|---|---|---|
| M0 | 固化基线 | 固定机器、系统、Metal/OpenGL、素材 hash、编码参数和冷/热缓存协议 | macOS benchmark schema、机器 manifest | 同一素材 3 次导出稳定；帧数、PTS、色彩 metadata 可复核 |
| M1 | 设备与格式探测 | 完善 Metal device、feature set、argument buffer tier、CVMetalTextureCache、IOSurface 格式矩阵 | probe JSON/MD、失败 reason 词表 | NV12/P010/BGRA/422/alpha 明确 supported/unsupported；失败不误判 |
| M2 | GPU frame 与资源池 | 统一 GpuDecodedFrame、MetalVideoFrameRing、lease/fence、source lifetime | Metal frame contract、4–8 槽 ring | 1,000 帧无资源增长、无 use-after-free、无无界队列 |
| M3 | 源端零拷贝 | VT CVPixelBuffer→MTLTexture；Topos IOSurface→MTLTexture；避免 CPU planar upload | request_metal_frame、Topos IOSurface source、源端 trail | H.264/HEVC/Topos 真实素材 GPU frame 比例 100%；上传字节为 0 |
| M4 | Topos GPU 解码 | 将 entropy/dequant/IDCT/reconstruction 拆成 Metal kernels；CPU 仅 packet parsing 和调度 | native Metal Topos decoder、CPU reference | 2K/4K、Proxy/HQ/4444/XQ 逐平面一致；GPU decode timestamp 存在 |
| M5 | 最小 Metal 导出闭环 | 无效果单层：Metal texture→Metal output→VideoToolbox/Topos GPU encode surface | encoder surface bridge、无效果 T0 | 四个像素阶段均有 GPU 时间；upload/download 为 0；码流可播放 |
| M6 | 删除 readback 和逐帧等待 | 删除生产路径 read_buffer、NumPy plane、ctx.finish；改 completion handler + fence | async command scheduler、surface ownership diagram | 480 帧无逐帧 readback/finish；queue 有界；内存不增长 |
| M7 | Metal 色彩与 YUV | GPU 完成 RGB/YCbCr、range/matrix、resize、P010、422、HDR/EDR、alpha | Metal color contract、HDR metadata bridge | CPU reference 抽帧 PSNR/SSIM 和码值门禁通过；零回读 |
| M8 | Edit 时间线图 | transform/crop/opacity、16 blend、空间转场 mask、常用效果合并 pass | 完整 TimelineMetalExportRenderer | T1–T4 真实素材通过；fallback 明示；性能不低于门槛 |
| M9 | Color 与 Composite | 将调色节点、LUT、scope 输入、Composite node graph 统一到 Metal | ColorMetalRenderer、CompositeMetalRenderer | 预览、headless 导出、时间线导出使用同一语义；无 BGR readback |
| M10 | 插件和特殊效果 | GLSL→MSL shader contract、plugin capability、temporal/history resources、3D/复合片段 | capability matrix、golden tests | 声明支持效果 100% 通过；未支持效果显示回退 |
| M11 | 性能和产品化 | pipeline cache、参数 buffer、资源别名、预取、render cache、Metal/OpenGL 偏好 | benchmark dashboard、设置项、诊断面板 | T0/T1 达 FFmpeg GPU 中位数 85%；效果项无 5% 以上回归 |
| M12 | 发布验收 | Intel/AMD 降级机、Apple Silicon 主目标机、不同 macOS 版本 | 发布矩阵、签名/公证记录 | 所有声明支持的素材、页面、效果和输出格式均有真实报告 |

## 5. 真实素材测试矩阵

默认素材目录为 tests/test_videos。每个文件先记录 SHA-256、容器、codec、分辨率、帧率、bit depth、色彩 metadata 和总帧数。正式样本为 480 帧、预热 60 帧、连续 3 次；不能得到 480 帧时必须记录实际帧数。

| 用例 | 真实素材 | 时间线内容 | 需要证明的事情 |
|---|---|---|---|
| M-T0 | 2K_h264.mp4、4K_H264.mov | 单层无效果直通 | VT 解码、Metal surface、GPU YUV 和硬编提交 |
| M-T1 | 2K_H265.mov、4K_H265.mov | HEVC/P010、resize、主级调色 | 10-bit texture、色彩转换和 HDR metadata |
| M-T2 | 2K_topos_proxy.mov、2K_topos_hq.mov、4K_topos_proxy.mov | Topos 源→Topos 输出 | Topos GPU decode 和自研 GPU encode surface |
| M-T3 | 2K_prores422Proxy.mov、2K_prores422HQ.mov | ProRes/Topos 混合双轨 | 无 VT ProRes 硬解时的透明 CPU fallback |
| M-T4 | 2K_h264.mp4 + 2K_H265.mov | 两轨、transform、crop、opacity、16 blend | 多源并行、资源池和混合模式 |
| M-T5 | 2K_h264.mp4 + 2K_H265.mov | wipe、iris、slide、user transition | transition mask 全 GPU、首尾帧和中间帧正确 |
| M-T6 | 2K_h264.mp4 | Blur、Sharpen、Glow、Erode/Dilate、Edge、Denoise、Defocus | 每个效果的 GPU 时间、画质和 fallback |
| M-T7 | 2K_DNxHR HQX 10bit.mov、4K_DNxHR HQX 10bit.mov | 10-bit 调色、HDR 输出 | P010/422 bit 对齐、matrix/range/transfer |
| M-T8 | 2K_topos_4444.mov、2K_topos_4444xq.mov、4K_topos_4444xq.mov | alpha、4444/XQ、合成页 | alpha plane、12-bit 和高位深交付 |
| M-T9 | 四轨 4K_H264.mov / 4K_H265.mov | 四轨 4K 压力 | 解码并行、显存、ring 深度和编码队列 |
| M-T10 | Color/Composite 页面使用以上素材 | Color 节点、LUT、scope、Composite 节点图 | 两个页面与 Edit 共享 Metal 语义且不回读 |

## 6. 每轮真实测试命令

运行前把 TOPOS_CODEC_LIB 指向当前构建的 native Topos 动态库。输出目录必须保留 JSON、Markdown、输入 manifest 和成片，不能只看终端 FPS。

    python tools/probe_metal_capability.py

    .venv/bin/python tools/bench_color_tools.py \
      --video 2K --frames 96 --warmup 12

    .venv/bin/python tools/bench_color_tools.py \
      --video 4K --frames 96 --warmup 12

    TOPOS_CODEC_LIB=/absolute/path/to/topos_codec.dylib \
    .venv/bin/python tools/bench_topos_playback_path.py \
      --input tests/test_videos/2K_topos_proxy.mov \
      --frames 120 --repeats 5 --threads 16 --decoder-workers 16 \
      --read-ahead 0 --allow-dirty

    TOPOS_BACKEND_GRAPHICS=metal TOPOS_EXPORT_GPU_RENDER=1 \
    TOPOS_CODEC_LIB=/absolute/path/to/topos_codec.dylib \
    .venv/bin/python scripts/demo_real_timeline_export.py \
      --source 2K_h264.mp4 --frames 480 \
      --out-dir bench_out/macos_real_export/t0

    TOPOS_METAL_VIDEO_PREVIEW=1 \
    .venv/bin/python tools/bench_metal_video_zero_copy.py \
      --file tests/test_videos/2K_h264.mp4 \
      --frames 120 --warmup 30

Topos profile 必须至少重复以下 12 个输入：

    2K_topos_proxy.mov
    2K_topos_lt.mov
    2K_topos_standard.mov
    2K_topos_hq.mov
    2K_topos_4444.mov
    2K_topos_4444xq.mov
    4K_topos_proxy.mov
    4K_topos_lt.mov
    4K_topos_standard.mov
    4K_topos_hq.mov
    4K_topos_4444.mov
    4K_topos_4444xq.mov

基准对照必须包括 CPU pipeline、当前 Metal、删除 readback 后的候选路径、直接 FFmpeg/VideoToolbox 和 Topos direct codec-only。codec-only FPS 不能当作时间线 FPS。

## 7. 报告字段和验证顺序

每次报告写入 tests/media/results/macos_gpu_export_machine_date.json 和配套 Markdown，至少包含：

| 类别 | 字段 |
|---|---|
| 环境 | macOS、CPU、GPU、显存、Metal feature set、OpenGL version、Python、PyAV、FFmpeg、native codec revision |
| 输入/输出 | 文件名、SHA-256、codec、resolution、fps、pixel format、bit depth、matrix/range/transfer、PTS、frame count、HDR/alpha metadata |
| 阶段时间 | decode、Metal render、YUV conversion、encode submit、encode wait、mux、disk 的 p50/p95/mean |
| GPU 证据 | command buffer GPU time、queue depth、surface pool peak、Metal texture frame count、GPU render frame count、encode submit frame count |
| CPU 证据 | cpu_upload_bytes、cpu_download_bytes、read_buffer count、Map count、finish count、CPU staging allocations |
| 正确性 | 首帧/中间帧/末帧 hash、PSNR、SSIM、最大码值差、色彩 metadata compare |
| 回退 | effect/backend/source fallback reason、发生帧数、是否用户可见 |
| 性能 | cold cache、warm cache、三次 run wall time、FPS、frame p50/p95/p99 |

固定验证顺序：

1. 检查工作树、commit、native codec 动态库和输入 manifest。
2. 跑单元、契约和 shader 编译测试。
3. 跑 capability probe，确认本机实际 Metal 格式。
4. 先跑 60 帧 warmup，再跑 3 次 480 帧正式导出。
5. 用 PyAV/ffprobe 验证输出帧数、PTS、格式、码率、HDR 和 alpha。
6. 对抽帧与 CPU reference 比较 PSNR/SSIM、逐平面码值差和 hash。
7. 读取 ExportGpuTrail，确认四个像素阶段都有 GPU 时间，upload/download 为 0。
8. 和同参数 FFmpeg/VideoToolbox、上一版本和 CPU reference 比较中位数和 p95。
9. 把慢点归因到 decode、upload、render、sync、encode 或 mux 后，再做单一变量优化。
10. 优化后重复相同素材和协议，禁止用更短片段或缓存命中结果替代正式验证。

## 8. 慢点定位和修复顺序

| 观测 | 先排查 | 修复 |
|---|---|---|
| source delivery 很快，timeline FPS 很低 | render 后 readback、编码器 host copy、同步 | 先实现 GPU encode surface，再删除 read_buffer/NumPy |
| Metal render 很低但 wall time 很高 | Python 调度、逐帧锁、finish、编码队列 | completion/fence 异步化，4–8 槽 ring，有界反压 |
| GPU upload bytes > 0 | CPU planar/BGR 进入 Metal | VT/Topos IOSurface 直接导入 Metal |
| GPU download bytes > 0 | output buffer readback、host encoder ABI | CVPixelBuffer/IOSurface 或 Metal-native Topos encoder |
| 4K effect 成本线性上升 | 多 pass、重复 YUV→RGB、邻域采样 | 预转换 RGB、tile/threadgroup、pass fusion、MPS/Metal kernel A/B |
| 双轨 FPS 下降 | decoder session 串行、资源池过浅、显存压力 | 独立预取、surface ring、fence 回收、限制预取并发 |
| 某效果 fallback | capability matrix 未覆盖、参数或输入资源不满足 | 先补 MSL 与 CPU golden，再纳入 GPU 覆盖率 |
| Topos 4444/XQ 无法导出 | alpha/12-bit ABI 未定义 | 先冻结 plane layout、stride、bit alignment、metadata，再做 kernel |
| Color/Composite 仍慢 | 页面各自维护 GL/CPU 语义 | 先统一 RenderPlan 和 GPU frame contract，再迁移 UI 表面 |

修复优先级固定为：消除原始像素回读；消除逐帧 finish、lock 和资源创建；建立 decode/render/encode ring 并行；消除重复色彩转换；再优化单个效果；最后做缓存和厂商专项。

## 9. 质量、回退和发布规则

- 未支持的效果必须在效果能力面板、导出报告和日志中显示具体 reason。
- 当前 Intel 机器没有 ProRes VT 硬解时，可以使用 CPU planar + Metal render 的混合路径，但必须标记 mixed。
- Topos native CPU decode 或 CPU encode 可以作为可靠 fallback，但不计入 full_gpu_coverage。
- OpenGL/ModernGL 保留为 macOS 兼容和回退路径；Metal 选择失败不能阻塞用户导出。
- Apple Silicon 是长期性能主目标；Intel + RX 6900 XT 是无 ProRes 硬解和独显 Metal 的回归机，两类性能报告不得混为一套基线。
- 完整导出速度以同机同参数 FFmpeg/VideoToolbox 中位数为参照。T0/T1 低于 85% 时必须先完成分段归因和修复。
- 任何 5% 以上性能回归、颜色回归、PTS 错位、帧丢失、surface 泄漏都阻止合并。

## 10. 最终完成定义

macOS 只有在以下条件全部满足时，才可以写“全链路 GPU 已完成”：

1. M-T0 至 M-T10 中声明支持的素材和页面都有真实导出报告。
2. Topos、VideoToolbox、Metal、Color、Composite、Edit 的资源交接使用 GPU-owned frame contract。
3. ExportGpuTrail 的四个像素阶段均有 GPU 时间，CPU upload/download 为 0。
4. 生产路径没有逐帧 read_buffer、NumPy 原始平面、finish 或同步等待。
5. 480 帧连续导出后 surface、pipeline、Metal library 资源不增长。
6. CPU reference 的画质、色彩、PTS、alpha、HDR metadata 门禁通过。
7. T0/T1 中位 FPS 达到同机 FFmpeg/VideoToolbox GPU 参照的 85%；效果和多轨逐项有独立 A/B 结论。
8. Color 页和 Composite 页的预览、headless 导出和最终交付共享同一套 GPU 语义。
9. 不支持的素材和效果能可靠回退并明确告知。
10. Apple Silicon 与 Intel/AMD 回归机完成对应矩阵，报告包含机器、系统、驱动和 commit。

## 11. 相关项目文件与已有报告

- Metal 迁移记录：docs/metal_backend_migration_and_cross_platform_gpu_plan.md
- Metal 时间线渲染：src/features/edit/core/timeline_metal_export_renderer.py
- 时间线导出编排：src/features/edit/core/timeline_export.py
- Metal frame provider：src/shared/video/frame_provider_service.py
- Topos Metal source：src/shared/media/topos_source.py
- GPU trail contract：src/shared/export/gpu_trail.py
- Metal 时间线实测：docs/codec/topos_timeline_playback_export_2026-09-15.md
- JSON 实测结果：tests/media/results/topos_timeline_playback_export_20260915.json
- Topos codec 矩阵：docs/codec/topos_codec_speed_matrix_2026-09-15.md

本文件是执行计划，不把现有部分覆盖写成最终完成；每个阶段必须以真实素材报告和 ExportGpuTrail 证据推进。

## 12. 本次执行进度（2026-09-15）

以下记录是本次执行产生的代码、测试和机器实测证据；它们不改变第 1 节的全链路 GPU 判定规则。

| 阶段/任务 | 本次结果 | 证据/提交 |
|---|---|---|
| M0 | 已补齐结构化 benchmark report schema、输入 manifest、ffprobe 字段和 fail-closed full-GPU claim | `34f6ba57`；`tests/media/results/macos_metal_capability_20260915.{json,md}` |
| M1 | 已完成可保存的 Metal device/compute/CVMetalTextureCache 格式矩阵探针；本机 RX 6900 XT 上 NV12/P010/BGRA/422 导入通过，PyObjC CoreVideo 路径 `-6661` 明确标为不可用并使用 ctypes | `34f6ba57`；`tools/probe_metal_capability.py --output-json ... --output-md ...` |
| M2/M6 | 已完成 GPU frame contract、有限提交 scheduler、completion-driven frame ring 和 surface 所有权契约；现有 host-visible 编码路径仍保留逐帧 finish/readback，因此 M6 最终门禁尚未通过 | `fae20376`、`1a4607eb`、`848de618` |
| M3/M7 | 已完成 VT CVPixelBuffer→Metal texture 的输入路径，以及 NV12/P010 目标 CVPixelBuffer/MTLTexture 的无 CPU download 直写 seam；P010 原生编码消费已在单层三帧 HEVC/hvcC/MP4 实测，并纳入 TimelineExporter 的最小单素材路由 | `7e5b1162`、本次提交；`tests/media/test_macos_gpu_encode_surface.py::test_nv12_surface_render_has_no_cpu_download`、`::test_p010_surface_render_has_no_cpu_download`、`tests/media/test_macos_vt_compression.py::test_m7_metal_p010_surface_to_videotoolbox_hevc_mp4_smoke`、`::test_timeline_exporter_native_bridge_multi_frame` |
| M5/M-B02 | 已完成并实测一帧、三帧及 480 帧闭环：真实 Metal NV12 输入→NV12 CVPixelBuffer/IOSurface→原生 `VTCompressionSession`→H.264 packet→MP4，PyAV 可解码且 YUV CPU download 为 0；另完成真实 Metal P010 输入→P010 CVPixelBuffer/IOSurface→原生 `VTCompressionSession`→HEVC packet→hvcC/MP4 的三帧闭环；H.264/NV12 与 HEVC/P010 的显式 bridge 和 `TOPOS_EXPORT_NATIVE_VT=1` TimelineExporter 最小单素材工厂路径均完成 3 帧实测，支持的两层 Metal stack、显式 Blur 和 spatial wipe 也已有 NV12→H.264 直写验收；480 帧报告为 480/480、480 个 native VT packet、约 121.6 FPS；显式 bridge 已接入 TimelineExporter 的视频编码选择、音频 mux、绕过 host-visible passthrough，并在 flush 生命周期中回收。未覆盖的多轨/效果组合、decode/encode 真实 GPU timestamp 和产品默认无条件启用仍未完成，故不能标记为最终导出闭环完成 | `7e5b1162`、`e7a84bac`、`6e7c740c`、`502b5290`、`7c6f0063`、`06025358`、`fe2df07d`、`a39c22ab`、`01e85e6e`、`3775d159`、`9454a492`、`ae52ef34`、`f68fba13`、`b9939ecb`、本次提交；`tests/media/test_macos_vt_compression.py::test_m5_metal_surface_to_videotoolbox_mp4_smoke`、`::test_timeline_exporter_native_bridge_multi_frame`、`::test_timeline_exporter_native_bridge_multitrack_surface`、`::test_timeline_exporter_native_bridge_spatial_transition_surface`、`::test_native_videotoolbox_bridge_muxes_audio_before_video_close`、`::test_m7_metal_p010_surface_to_videotoolbox_hevc_mp4_smoke`、`tests/media/results/macos_gpu_export_20260915.{json,md}` |
| M8/M-B06 | 支持的 wipe/slide/iris/polygon/custom spatial transition mask 已改为 Metal procedural kernel，并已验证 wipe stack 可直接进入 VT NV12 surface；CPU mask 仅保留给 OpenGL/legacy 或明确不支持的组合 | `8f41160c`、本次提交；相关 Metal/CPU parity tests、`tests/media/test_macos_vt_compression.py::test_timeline_exporter_native_bridge_spatial_transition_surface` |
| ExportGpuTrail | 已接入 timeline renderer/exporter 的真实 Metal GPU time、上传/下载字节、fallback 和 encode surface evidence；缺少任何四段证据时仍 fail-closed | `7b1437c3`、`9985d06` |

本次已完成跨模块回归结果：相关导出与 macOS VT 模块 `46 passed, 5 skipped`；纳入上一轮全链路回归累计为 `316 passed, 5 skipped`。其中 M5 真实 Metal→VT→MP4 单帧、TimelineExporter 的 H.264/NV12 与 HEVC/P010 三帧闭环、支持的两层 Metal stack、spatial wipe、默认工厂最小路径和 480 帧正式报告均通过；480 帧报告仍因缺少 decode/encode 的真实 GPU timestamp 而 fail-closed。当前仍明确阻塞最终完成定义的项目包括：未覆盖的多轨与效果组合、无条件默认 GPU 路由、Topos entropy/dequant/reconstruction GPU 解码、Topos GPU 编码 ABI、生产路径逐帧同步删除、Color/Composite 统一 Metal graph、GLSL→MSL 插件覆盖、temporal/特殊效果、alpha/4444/XQ 全矩阵，以及 Metal preview surface 全面替换。因此当前仓库不得声称“全链路 GPU 已完成”。
