# Windows 全链路 GPU 优化与真实素材验收计划

> 版本：执行版 1.0
> 日期：2026-09-15
> 适用范围：Topos Color 在 Windows 上的 Edit、Color、Composite、导出、Topos 编解码以及 NVIDIA、AMD、Intel GPU 路径
> 目标：以 D3D11/DXGI texture 作为统一像素边界，把 NVDEC/D3D11VA、HLSL/DirectCompute、NVENC、AMF、oneVPL 连接成零像素回读链路；OpenGL/ModernGL 继续作为重要的预览、兼容和回退选项。
> 当前状态：设备、LUID、NV12 纹理池、帧契约和测试协议已经完成；D3D11 解码 surface、D3D11 时间线渲染器和硬编 surface 提交仍未完成，因此 Windows 不能称为全链路 GPU。

## 1. 全链路完成的定义

Windows 的像素链路目标如下：

    MF D3D11VA / NVDEC / AMF decode surface
        -> ID3D11Texture2D texture pool
        -> HLSL / DirectCompute render graph
           （NV12/P010 读取、色彩、缩放、调色、合成、转场、滤镜）
        -> GPU NV12 / P010 encode surface
        -> NVENC / AMF / oneVPL hardware encoder
        -> compressed packet
        -> CPU mux / metadata / disk write

允许 CPU 处理工程、时间线调度、音频、压缩 packet、mux 和磁盘。解码完成到硬件编码提交之间禁止把原始像素映射到 NumPy、VideoFrame、BGR/RGB 或 CPU staging。

只有下列条件全部满足，才允许显示 Windows 全链路 GPU：

| 门槛 | 要求 |
|---|---|
| 同一适配器 | 解码器、D3D11 renderer、编码器的 DXGI adapter LUID 完全一致 |
| GPU 解码 | decode surface 来自 D3D11VA/NVDEC/AMF/oneVPL GPU surface，不能是硬解后下载 |
| GPU 渲染 | HLSL/DirectCompute 覆盖所有声明支持的时间线 pass |
| GPU YUV | 最终 NV12/P010 texture 在 GPU 生成 |
| GPU 编码提交 | NVENC/AMF/oneVPL 直接接受同一 D3D11 texture |
| 零像素搬运 | ExportGpuTrail 的 cpu_upload_bytes 和 cpu_download_bytes 均为 0 |
| 证据完整 | decode、render、yuv_conversion、encode_submit 均有 Direct3D timestamp |
| 可靠回退 | 能力不足显示准确 reason，不能伪报 zero-copy |

## 2. 当前状态盘点

### 2.1 已完成并有证据的内容

| 编号 | 内容 | 当前结果 | 证据与边界 |
|---|---|---|---|
| W-D01 | Windows GPU 能力矩阵 | 已完成（能力级） | RTX 4090、RX 6800 等已有解码/编码/适配器报告；能力探针不等于时间线零拷贝 |
| W-D02 | D3D11 device 生命周期 | 已完成（设备级） | device 创建、feature level 阶梯、vtable 健全性、adapter LUID 已实现 |
| W-D03 | NV12 定长纹理池 | 已完成（设备/池级） | AMD RX 6800 真机 1,000 帧池测试通过，峰值有界；P010 手动创建在该驱动上失败，代码已 fail-closed |
| W-D04 | D3D11 GPU frame contract | 已完成（契约级） | D3D11 frame 不暴露 CPU planes，携带 DXGI format、LUID、lease/fence 语义 |
| W-D05 | T0 直通编排器 | 已完成（校验层） | LUID 检查、surface 所有权、trail 计数和 fail-closed 边界已实现；native decode/encode bridge 仍为空 |
| W-D06 | ExportGpuTrail 证据 | 已完成 | shared/export/gpu_trail.py 记录 GPU 时间、上传/下载字节和回退原因 |
| W-D07 | Windows benchmark 协议 | 已完成（协议级） | scripts/bench_timeline_export_windows.py 支持 W-T0–W-T10、输入 SHA-256/PyAV manifest、预热、三次正式 run、JSON/Markdown、D3D11 gate 组和 FFmpeg GPU 对照 |
| W-D08 | 现有硬解下载路径 | 已完成（混合路径） | hw_planar_source.py 使用硬解后下载 NV12/P010 到 CPU；这是硬解+CPU像素，不是零拷贝 |
| W-D09 | 现有硬件编码 | 已完成（host input） | NVENC/AMF 可取得真实编码吞吐；输入仍可能来自 CPU planes 或上传 |
| W-D10 | OpenGL 继续保留 | 架构已确定 | OpenGL/ModernGL 用于现有预览、已验证效果、插件兼容和 D3D11 回退 |

### 2.2 当前真实基线

AMD RX 6800 历史报告使用 2K H.264、480 帧、20 Mbps VBR、音频关闭、三次正式 run：

| 组 | 中位 FPS | 含义 |
|---|---:|---|
| CPU pipeline | 42.525 | 纯 CPU 解码、渲染、编码基线 |
| hw_download | 139.021 | 硬解下载 + 当前时间线 + AMF，仍有 CPU 像素交接 |
| FFmpeg GPU surface | 151.679 | D3D11 surface 到 AMF 的 GPU 对照路径 |

T0 的 hw_download 约为 FFmpeg GPU 参照的 91.7%，但这不能证明 Topos 自己的 D3D11 零拷贝已经实现。T1 resize + 色彩转换的 hw_download 只有 27.086 FPS，而 FFmpeg GPU 参照约 151.092 FPS，说明当前时间线 CPU 中段仍是主要瓶颈。

### 2.3 尚未完成的阻塞项

| 编号 | 未完成项 | 当前代码事实 | 完成标志 |
|---|---|---|---|
| W-B01 | 原生 D3D11 decode surface | d3d11_decoder.py 的 open/decode_next 仍是 not_yet_implemented | MF D3D11VA/NVDEC/AMF 输出真实 ID3D11Texture2D，并通过 D3D11 frame contract |
| W-B02 | D3D11 与 OpenGL bridge | d3d11_gl_bridge.py 仍是骨架 | 只作为过渡兼容；失败时明确 interop reason，不能成为全 GPU 主链 |
| W-B03 | D3D11 时间线 renderer | timeline_export.py 当前只选择 OpenGL/Metal，Windows 没有 D3D11 renderer | D3D11/HLSL 直接读取 decoder texture 并输出 GPU target |
| W-B04 | 硬编 surface 提交 | gpu_encode_surface.py 的 submit_to_encoder 返回 not_yet_implemented | NVENC/AMF/oneVPL 直接取得同 device texture 和 fence |
| W-B05 | T0 零回读 | zero_copy_probe 目前只通过设备/池层 | 真实 T0 输出可播放，upload/download 均为 0，帧数和 PTS 可验证 |
| W-B06 | T1 基础渲染 | T1 仅接入 resize + yuv420p，主级调色程序化构建未完成 | resize、主级调色、色彩转换和 P010/HDR 逐项走 HLSL |
| W-B07 | 多轨合成和转场 | T2-T6 benchmark 仍有 declared_not_run 用例 | 两轨/四轨、16 blend、空间转场、mask 和 fence 通过真实导出 |
| W-B08 | 内建效果 HLSL | GLSL/CPU 语义不能视为 HLSL 已支持 | Blur、Sharpen、Glow、形态学、Edge、Denoise、Defocus 每项有 golden test |
| W-B09 | Color/Composite 页面 | 现有页面主要走 OpenGL/CPU 交接 | 页面 render graph 使用 D3D11 texture lease；导出不回读 BGR |
| W-B10 | HDR/10-bit/alpha | RX 6800 手动 P010 texture 创建失败，不能假设所有驱动支持 | 使用 decoder/encoder 自有 P010 surface；验证 PQ/HLG、matrix/range、alpha |
| W-B11 | 厂商专项和多 GPU | CUDA、SmartAccess Video、oneVPL、多 GPU 尚无端到端门禁 | 通用 D3D11 通过后逐厂商 A/B；跨卡复制默认禁止 |
| W-B12 | 产品设置 | Windows Auto/OpenGL/D3D11 导出选择尚未完整接入 | 能力面板、后端选择、回退 reason、报告和用户提示一致 |

## 3. 后端策略：D3D11 主导导出，OpenGL 保留

Windows 不能简单删除 OpenGL。两个后端职责如下：

| 选择 | 作用 | Auto 规则 | 声明边界 |
|---|---|---|---|
| Auto | 默认用户选择 | decoder、renderer、encoder 同 LUID，零回读 probe、效果覆盖和性能门禁均通过时才选 D3D11 | 显示所选后端、硬解方式、GPU frame 比例和 fallback |
| OpenGL | 现有 ModernGL/GLSL 预览、插件、旧驱动和兼容路径 | D3D11 任一门禁失败时可用 | OpenGL GPU render 不等于 D3D11 全链路；staging/readback 必须标明 |
| D3D11 | Windows 高性能时间线导出、HDR、NV12/P010、硬编 surface 直连 | 完整 T0/T1 门禁通过后启用 | Windows 全 GPU 导出主路径 |
| CUDA | NVIDIA 专项 AI、光流、推理或有明确 A/B 收益的 kernel | 不作为普通调色/resize 基础路径 | D3D11↔CUDA 必须无 staging、无回读、无跨卡复制 |
| DirectML | AI 模型 | 仅作为独立 AI pass | 不替代 D3D11 video surface |
| OpenCL | 旧插件或专项实验 | 不作为新时间线主路径 | capability 不等于 D3D11 zero-copy 证据 |

D3D11 是 NVIDIA、AMD、Intel 共享视频 surface 的共同边界。WGL_NV_DX_interop 只作为过渡实验；D3D12 在 D3D11 全链路通过并有真实收益后再评估。

## 4. GPU frame、设备和同步契约

### 4.1 设备身份

- 运行时以 DXGI adapter LUID 作为唯一身份，不能只用厂商名称或显卡字符串。
- 解码、HLSL renderer、编码器必须绑定同一 ID3D11Device。
- 混合显卡默认选择高性能 GPU，但必须复核实际 LUID。
- LUID 不一致时立即返回 adapter_mismatch，走已验证的 OpenGL 或 hw_download fallback。

### 4.2 GPU frame contract

每帧必须携带：

- ID3D11Texture2D 资源和 DXGI format；
- width、height、array slice、plane/subresource；
- matrix、range、primaries、transfer、HDR metadata；
- PTS、duration、frame index；
- adapter LUID；
- D3D11 fence/keyed mutex 或等价同步对象；
- lease、release callback、source kind、fallback reason。

硬规则：

1. 不暴露 CPU planes，不允许隐式 PyAV frame reformat。
2. 不允许逐帧 Map 到 staging、CopyResource 到 CPU、Flush 或等待 GPU 完成。
3. surface pool 预分配 4–8 槽，按 fence 回收，队列有界并支持反压。
4. 编码器取得资源后 renderer 不得再写入；编码完成后才归还 lease。
5. unsupported format、驱动限制、编码器拒绝必须记录 reason。

### 4.3 HLSL render graph

每次时间线结构或参数版本变化时编译一次 RenderPlan：

- source texture、颜色管理和 bit-depth；
- transform/crop/opacity、blend、transition；
- effect pass 和参数 buffer；
- intermediate texture 别名；
- decoder/render/encoder fence；
- 输出 NV12/P010 surface。

480 帧循环期间不得重复解析时间线、编译 HLSL、创建 PSO、申请临时纹理或把纹理转成 CPU 图像。

## 5. 分阶段执行表

| 顺序 | 阶段 | 主要工作 | 产物 | 进入下一阶段的门禁 |
|---:|---|---|---|---|
| W0 | 固化测量协议 | 冻结机器、驱动、FFmpeg/PyAV/native bridge、输入 manifest、编码参数和输出格式 | benchmark schema、机器 manifest | 每组预热 60 + 正式 3×480 帧；JSON/MD、输出 hash 和 probe 齐全 |
| W1 | device/adapter 生命周期 | D3D11CreateDevice feature level 阶梯、LUID、device manager、pool、lease/fence | native device bridge、契约测试 | NVIDIA、AMD、Intel 各至少一台机器；1,000 帧无资源增长；跨 LUID fail-closed |
| W2 | 原生 decode surface | MF D3D11VA、NVDEC 或 AMF decode surface 接入 native protocol | topos_hwdecode_windows、D3D11 decoder source | T0 decode surface 100% GPU；没有 CPU plane；Direct3D decode timestamp 存在 |
| W3 | T0 零回读直通 | decode texture→同卡 NV12/P010 encode texture→NVENC/AMF/oneVPL | D3D11 passthrough chain、encoder bridge | 输出可播放、帧数/PTS 正确、cpu_upload/download=0；未通过不得迁移效果 |
| W4 | 最小 D3D11 renderer | HLSL 读取 NV12/P010、RGB/YUV、resize、主级调色和 metadata | D3D11TimelineRenderer、T1 runner | CPU reference 画质通过；render/YUV/encode 均有 GPU 时间 |
| W5 | 并行流水线 | decode prefetch、4–8 surface ring、异步 render/encode、fence 回收、反压 | queue telemetry、wait reason、trail 集成 | T0/T1 中位 FPS ≥ FFmpeg GPU 85%；p95 无逐帧 wait 尖峰 |
| W6 | 时间线图和合成 | transform/crop/opacity、16 blend、两轨合成、资源别名和 pass fusion | RenderPlan、T3 runner | 两轨真实素材帧数、PTS、画质和 pool peak 通过 |
| W7 | 空间转场 | wipe、iris、slide、user transition mask、双侧 source lease | T4 runner、HLSL transition kernels | 首尾/中间帧与 CPU reference 对齐；无 lease 泄漏或死锁 |
| W8 | 内建效果 | Blur 全变体、Sharpen、Glow、Erode/Dilate、Edge、Denoise、Defocus | HLSL effects、每项 golden/benchmark | 每个效果先正确后提速；未迁移项显示 OpenGL/CPU fallback |
| W9 | Color/Composite 页面 | 将 Color 节点、LUT、scope 输入和 Composite node graph 接到 GPU texture | 页面 renderer、headless/export path | 页面预览和导出不创建 CPU BGR；与 Edit 语义一致 |
| W10 | HDR/高位深/alpha | decoder/encoder 自有 P010 surface、PQ/HLG、10/12/16-bit、alpha | format matrix、metadata bridge | NVIDIA/AMD/Intel 分别实测；驱动不支持时准确回退 |
| W11 | 缓存与调度 | GPU frame/intermediate reuse、SegmentRenderKey、optimized media、预取和淘汰 | cold/warm/partial miss benchmark | 缓存命中不冒充 FPS；参数变更只重算受影响片段 |
| W12 | 厂商专项 | NVIDIA CUDA/NVENC、AMD AMF/SmartAccess Video、Intel oneVPL、多 GPU | vendor adapter modules、A/B report | 通用 D3D11 T0-T6 已通过；专项收益 ≥15% 且无 staging |
| W13 | 产品接入 | Auto/OpenGL/D3D11 偏好、能力提示、回退 UI、日志和诊断报告 | Windows settings、ExportGpuTrail UI | Auto 只在完整门禁通过的机器启用 D3D11 |
| W14 | 发布回归 | RTX 4090、RX 6800、Intel Arc/QSV、混合显卡、不同驱动 | 自托管 GPU CI、发布矩阵 | 所有声明支持的组合有真实素材报告；失败组合安全回退 |

## 6. 真实素材测试矩阵

素材统一来自 tests/test_videos。每次保存输入 SHA-256、ffprobe 信息、驱动、LUID 和输出报告。正式 480 帧、预热 60 帧、连续 3 次；runner 未实现时必须写 declared_not_run 和 blocking 原因。

| 用例 | 真实素材 | 时间线内容 | 目标 |
|---|---|---|---|
| W-T0 | 2K_h264.mp4、4K_H264.mov | 单段 H.264 直通 | D3D11VA/NVDEC→NV12→NVENC/AMF 零回读 |
| W-T1 | 2K_h264.mp4、2K_H265.mov | resize、主级调色、色彩转换 | HLSL 基础 render 和硬编 |
| W-T2 | 2K_H265.mov、4K_H265.mov | HEVC/P010、10-bit | P010、色彩 metadata、HDR |
| W-T3 | 2K_h264.mp4 + 2K_H265.mov | 两轨、transform、crop、opacity、16 blend | 多源 decode、合成、资源池 |
| W-T4 | 2K_h264.mp4 + 2K_H265.mov | wipe、iris、slide、user transition | GPU mask、双侧 lease、过渡帧 |
| W-T5 | 2K_h264.mp4 | Blur、Sharpen、Glow、形态学、Edge、Denoise、Defocus | 效果 HLSL 覆盖、画质和吞吐 |
| W-T6 | 2K_DNxHR HQX 10bit.mov、4K_DNxHR HQX 10bit.mov | 10-bit 调色和 HDR 输出 | decoder/encoder 自有 P010 surface |
| W-T7 | 2K_prores422HQ.mov、4K_prores422HQ.mov | 专业中间码流 | Windows ProRes 解码能力和 fallback |
| W-T8 | 2K_topos_proxy.mov、2K_topos_hq.mov、2K_topos_4444xq.mov | Topos 输入/输出 | Topos GPU decode/encode bridge 或明确 mixed |
| W-T9 | 4K_H264.mov、4K_H265.mov | 四轨 4K 压力时间线 | 显存、解码并行和硬编队列 |
| W-T10 | Color/Composite 页面使用以上素材 | 节点图、LUT、scope、合成 | 页面全 GPU 和导出交接 |

每个用例都要跑 CPU pipeline、当前 hw_download、Topos D3D11 GPU surface、同参数 FFmpeg GPU surface 四组。

## 7. 每轮真实测试命令

在 Windows PowerShell 或 cmd 中运行，使用项目自己的 Python 环境。tag 必须包含 GPU 型号和驱动版本。

    python scripts/probe_d3d11_device_pool_windows.py

    python scripts/probe_hw_decode_windows.py

    python scripts/probe_nvenc_encode_windows.py

    python scripts/probe_amf_encode_windows.py

    python scripts/probe_adapter_match_windows.py

    python scripts/bench_timeline_export_windows.py ^
      --cases T0,T1 --frames 480 --warmup 60 --runs 3 ^
      --tag amd_rx6800_driver_xxx --keep-outputs

完成 T0 后再启用 T2-T10：

    python scripts/bench_timeline_export_windows.py ^
      --cases T0,T1,T2,T3,T4,T5,T6,T7,T8,T9,T10 ^
      --frames 480 --warmup 60 --runs 3 ^
      --tag rtx4090_driver_xxx --keep-outputs

FFmpeg 参考必须记录完整命令，至少包含 d3d11va/NVDEC 硬件解码、hwaccel_output_format d3d11、对应厂商硬编、同分辨率、同帧数、同码率/质量档和相同音频开关。

没有 Windows 真机时，只能运行契约测试和 dry-run，不能把 macOS 或 Linux 结果写成 Windows 性能证据。当前 macOS 开发机不能替代 RTX 4090、RX 6800 或 Intel Arc 真机。

## 8. 报告字段和验证顺序

报告写入 tests/media/results/windows_full_gpu_machine_date.json 和配套 Markdown，至少包括：

| 类别 | 字段 |
|---|---|
| 环境 | Windows build、CPU、内存、GPU、显存、驱动、DXGI adapter LUID、D3D feature level、FFmpeg、PyAV、native bridge |
| 输入/输出 | 文件 SHA-256、codec、resolution、fps、pixel format、bit depth、PTS、frame count、HDR/alpha metadata |
| 阶段时间 | decode、render、yuv_conversion、encode_submit、encode_wait、mux、disk 的 CPU/GPU p50/p95 |
| GPU 证据 | Direct3D timestamp、GPU engine、queue depth、surface pool peak、GPU-resident frame ratio |
| CPU 搬运 | cpu_upload_bytes、cpu_download_bytes、Map、staging allocation、CopyResource、Flush、GPU wait |
| 正确性 | ffprobe/PyAV probe、首/中/末帧 hash、PSNR/SSIM、最大码值差、色彩 metadata |
| 回退 | 后端、codec、格式、效果、适配器不匹配的 reason 和发生帧数 |
| 性能 | cold/warm cache、三次 wall time、FPS、frame p50/p95/p99、与 FFmpeg 比值 |

固定验证顺序：

1. 记录 commit、机器、驱动、LUID、输入 manifest。
2. 跑 device、format、decode、encode probe。
3. 跑单元、帧契约、fallback、HLSL golden tests。
4. 跑 60 帧预热；正式导出 3×480 帧。
5. 用 ffprobe/PyAV 验证帧数、PTS、profile、bit depth、HDR 和 alpha。
6. 对抽帧与 CPU reference 做画质和色彩比较。
7. 读取 ExportGpuTrail 和 Direct3D timestamp，确认四个像素阶段均有 GPU 证据，upload/download 为 0。
8. 与 FFmpeg GPU surface 和上一版本比较中位 FPS、p95 和分段耗时。
9. 根据分段归因一次只改一类瓶颈，再完整重跑。
10. 任何失败都保留产物和 reason，不允许删除失败报告后只保留成功结果。

## 9. 慢点定位和修复顺序

| 观测 | 根因优先级 | 修复 |
|---|---|---|
| cpu_download_bytes > 0 | decode surface 被下载或跨卡复制 | 统一 device/LUID，直接传 ID3D11Texture2D |
| conversion 段 CPU 高 | BGR/RGB→NV12/P010 CPU 转换 | HLSL 直接写 NV12/P010 target，交给 NVENC/AMF |
| renderer GPU 时间低、Python CPU 高 | 每帧对象创建、shader compile、锁和 wait | RenderPlan、PSO cache、固定 pool、批量提交 |
| encode submit 阻塞 | pool 太浅、fence 归还晚、B-frame 队列满 | 4–8 槽 ring、有界反压、异步 encoder event |
| 两轨/四轨骤降 | decoder session 串行、显存压力 | 独立 queue、按 LUID 分组、限额预取、显存水位 |
| P010 失败 | 驱动不允许手动创建 P010 | 从 decoder/encoder 自有 surface 取得 P010；失败时明确回退 |
| 色彩/10-bit 错误 | matrix/range/bit alignment/metadata 丢失 | 先修 frame contract，速度结论作废后重测 |
| OpenGL↔D3D bridge 慢 | WGL interop 或 staging copy | D3D11 作为导出主路径；OpenGL 只保留兼容预览 |
| GPU 使用率高但 FPS 低 | 不能据此排除回读和同步 | 以 timestamp、Map/copy 计数和 trail 为准 |
| cache hit 很快 | 命中历史成片或 proxy | 分开报告 cold/warm/partial miss；缓存命中不计 pure render FPS |

修复优先级固定为：原生 decode surface；T0 encode surface 直连；删除 CPU download/upload 和 staging；建立异步 ring/fence；HLSL 最小 render graph；合成、转场、效果和页面迁移；缓存和厂商专项；最后才做编码 preset、线程和 UI 微调。

## 10. 厂商和多 GPU 规则

### NVIDIA

- 主路径：D3D11VA/NVDEC → D3D11/HLSL → NVENC。
- CUDA 只用于 AI、光流、推理或真实 A/B 证明比 HLSL 更快的专项。
- CUDA 与 D3D11 互操作必须无 staging、同步回读和跨 adapter copy。
- NVENC profile、B-frame、lookahead 和 rate control 固定后再比较 FPS。

### AMD

- 主路径：AMF DX11 decode surface → D3D11/HLSL → AMF DX11 encode surface。
- P010 不能假设可手动创建；优先使用解码器/编码器拥有的 surface。
- SmartAccess Video 只有在 APU+dGPU 真机 T3/T6 中有稳定收益才启用。
- 不把 OpenCL 或 CPU color conversion 作为通用效果路径。

### Intel

- 主路径：oneVPL/D3D11 decode → D3D11/HLSL → oneVPL encode。
- 逐项验证 QSV surface、P010、HDR 和驱动版本。
- oneVPL 退回 system memory 时，trail 必须显示原因。

### 多 GPU

- 先完成单卡完整链路。
- 同一导出任务默认固定一张 GPU，以 LUID 绑定 decoder、renderer 和 encoder。
- 第二张 GPU 只有在任务可独立分片、无跨卡像素复制且真实收益至少 15% 时启用。
- 混合显卡笔记本必须测试电源状态、Windows graphics preference 和 LUID mismatch fallback。

## 11. 回退和产品规则

- Auto 只有在完整 D3D11 T0/T1、效果覆盖和零回读门禁通过后才选择 D3D11。
- OpenGL 继续作为重要选择，用于预览、GLSL 插件、D3D11 未覆盖效果、旧硬件和驱动兼容。
- hw_download 是可用的混合加速路径，报告中必须标为 hardware decode + CPU pixels。
- 不支持的编码、bit depth、HDR、alpha、效果或适配器组合都要展示 reason。
- 用户明确选择 D3D11 但能力不足时，可以安全回退并提示具体原因；不能静默切换后仍显示 D3D11。
- 最终交付的压缩 packet 可以回 CPU mux/write；原始像素不可以回 CPU。

## 12. 最终完成定义

Windows 只有在以下条件全部满足时，才可以写“全链路 GPU 已完成”：

1. W-T0 至 W-T10 中声明支持的素材、效果和页面均有真实 Windows GPU 报告。
2. NVIDIA、AMD、Intel 至少各有一台目标机器完成对应 codec/bit-depth 矩阵。
3. decode、render、yuv_conversion、encode_submit 均有 Direct3D timestamp。
4. ExportGpuTrail 证明 cpu_upload_bytes=0、cpu_download_bytes=0，且无 staging/Map/CopyResource 像素路径。
5. decoder、renderer、encoder 的 adapter LUID 全部一致。
6. 480 帧连续导出后资源池、fence、线程和 GPU memory 不增长。
7. T0/T1 中位 FPS 达到同机 FFmpeg GPU surface 参照的 85%；低于门槛的项目已有分段归因和修复。
8. T2-T10 的画质、PTS、色彩、10-bit、HDR、alpha 和输出可播放性门禁通过。
9. Color、Composite、Edit 的渲染和导出使用一致的 GPU frame contract。
10. OpenGL 保留且回退可靠；能力不足不会造成错误导出或静默丢效果。
11. 所有失败组合都保留 JSON/Markdown 报告、驱动、LUID 和 reason，能够复现。

## 13. 相关项目文件与已有报告

- Windows 时间线子计划：docs/windows_timeline_gpu_export_optimization_plan_2026-09-15.md
- Windows 优化总计划：docs/windows_optimization_plan_2026-09-11.md
- D3D11 device：src/shared/media/windows_hw/d3d11_device.py
- D3D11 decoder：src/shared/media/windows_hw/d3d11_decoder.py
- D3D11 frame：src/shared/media/windows_hw/d3d11_frame.py
- D3D11 texture pool：src/shared/media/windows_hw/d3d11_texture_pool.py
- GPU encode surface：src/shared/media/windows_hw/gpu_encode_surface.py
- T0 passthrough chain：src/shared/media/windows_hw/d3d11_passthrough_chain.py
- Windows planar fallback：src/shared/media/windows_hw/hw_planar_source.py
- 时间线导出：src/features/edit/core/timeline_export.py
- GPU trail contract：src/shared/export/gpu_trail.py

## 14. 2026-09-15 执行记录（增量保存）

本节记录本次按计划执行的代码落地，不改变第 12 节的最终完成定义。计划中的
“已完成”是任务说明；只有下面列出的测试、真实素材和原生桥证据才可升级为真实
Windows 全链路结论。

| 批次 | 已落地内容 | 验证 | 边界 |
|---|---|---|---|
| 1 | 原生 bridge API、GPU-only decoded texture、encoder completion fence、D3D11/GL interop fail-closed、passthrough LUID/lease 校验 | `test_windows_hw_native_surface_contract.py` | `topos_hwdecode_windows` 原生模块仍未提供，不能宣称 W-B01/W-B04 完成 |
| 2 | 4–8 槽有界 decode→render→encode 调度、异步 fence 归还、RenderPlan/cache key、缓存原子写入和 vendor/multi-GPU policy | `test_windows_hw_pipeline_policy.py`、`test_windows_render_cache.py` | 仍是可注入的协议/调度层，未替代真实 native queue |
| 3 | W-T0–W-T10 输入矩阵、素材 SHA-256/PyAV manifest、D3D11 benchmark 组、格式/HDR/alpha policy、GPU timestamp + zero-readback T0 gate | `test_bench_timeline_export_windows.py` 及 Windows hardware contract tests；聚焦集合 36 passed | 没有真实 native decode/render/encode bridge 和三家 GPU 端到端报告时，D3D11 组记录 `declared_not_run` |
| 4 | 产品设置页显示 Windows D3D11 选项；导出 worker 负责 capability probe；选中 D3D11 但 renderer 接线不足时明确 CPU fallback reason | 相关导出/设置代码静态检查 | UI 线程不创建 D3D11 device；D3D11 不会伪装成 OpenGL 成功 |
| 5 | RX 6800 真机复跑 device/pool、适配器 LUID、PyAV d3d11va H.264/HEVC、AMF H.264/HEVC 8/10-bit 探针 | `d3d11_device_pool_probe_amd_rx6800_2026-09-15.*`、`adapter_match_probe_amd_6800.*`、`windows_hw_decode_probe_amd_rx6800_*.*`、`windows_amf_encode_probe_amd.*` | P010 手工 `CheckFormatSupport` 失败；AV1 AMF 失败；这些是能力/回退证据，不是 Topos 全链路证据 |
| 6 | 当前 benchmark 的 D3D11 组在 RX 6800 上执行完整 capability gate | `windows_timeline_export_amd_rx6800_gate_test_2026-09-15.{json,md}` | device/LUID 成功，但 `topos_hwdecode_windows` 缺失，T0/T1 均准确记录 `declared_not_run` |
| 7 | 基准子进程改用每组隔离的 `TOPOS_CONFIG_PATH`；T1 的 FFmpeg 参考补齐 1280×720 输出，并优先尝试 `scale_d3d11`，失败时显式降级 | `test_bench_timeline_export_windows.py` 29 passed；`windows_timeline_export_amd_rx6800_config_isolated_smoke_2026-09-15.{json,md}` | 30 帧冒烟通过；本机 `scale_d3d11` 目标纹理创建失败，参考组记录完整 `ref_mode_fallback_reason` 并降为 `hwaccel_download`；这不是正式 480 帧性能门禁 |
| 8 | 计划完成度复查并修复校验/调度层缺陷：D3D11Decoder 复用实例换源误报 `decode_size_changed`（`open()` 未清零 `_width/_height`）；`D3D11Device.release()` 与 vtable 健全性失败路径泄漏 immediate context COM 引用；pipeline 渲染 fence 契约违约路径泄漏解码纹理 lease（池容量永久损失一槽）；纹理池 `created` 计数 close 后归零、关闭后归还不减 `in_use`；外部 lease 回收线程缺异常兜底；GL bridge 失败计数无锁 | `test_windows_hw_native_surface_contract.py`、`test_windows_hw_d3d11_device_pool.py`、`test_windows_hw_pipeline_policy.py` 各新增回归；device/pool 真机探针复跑通过（1000 帧归档保持不变） | 均为校验/调度层缺陷，不改变零回读门禁语义，也不新增任何 GPU 能力声明 |
| 9 | 基准协议缺陷修复：`--keep-outputs` 未透传子进程、实际不保留任何 mp4；FFmpeg 参照组 gpu_surface→hwaccel 降级后跳过预热直接测正式 run（违反 W0 预热协议，本机 T1 v2 参照数即来自该无预热路径）；子进程超时未捕获会让父进程崩溃且不落报告；NVIDIA 参照命令误用 AMF 专属 `-quality/-rc vbr_peak`；run 记录 `fps=None` 可使中位数计算崩溃 | `test_bench_timeline_export_windows.py` 新增 keep-outputs 透传、超时结构化 error、降级重跑预热、vendor 编码参数回归；真机 T0 ffmpeg_gpu_ref 5 帧冒烟 gpu_surface 模式通过 | 历史 v2 参照的 hwaccel_download 数字属"无预热冷启动"口径；正式 480 帧复测以新协议为准 |

已保存提交：`0104170e`、`de359f9a`、`b46d63b3`、`648743cf`、`e0d83e9e`、
`1deac822`、`0acdf0cf`、`97d5fcf9`、`f9aa2606`、`1c5f1680`、`98106291`、
`c0d8e067`、`63a02d05`。当前仍不可写“Windows 全链路 GPU
已完成”：缺少 W-B01 原生 decode surface、W-B03 实际时间线 renderer 接线、W-B04
硬编 surface submit、W-B05 真实 T0 证据以及其后的真实素材/厂商验收。全量 pytest
曾在既有 `composite_page.py` Qt 后台线程处触发 Windows access violation；因此本轮
Windows 结论只采用聚焦契约测试，未把该中止误报为通过。

此前尝试按 W0 重新跑 T0/T1 的完整 60+3×480 基线，以及关闭 GPU render 的 30 帧
CPU smoke 时，进程在初始化阶段卡在用户配置目录的临时文件创建；现在已通过每个
基准子进程的隔离配置路径修复。修复后的 30 帧 T0/T1 冒烟已完成，但仍不是正式
480 帧×3 性能门禁；正式复测需在 native bridge 接通后进行。

最后一轮聚焦 Windows 回归共 170 项通过（D3D11 契约、设备/池、回退、能力探针、
native surface、流水线策略、T0 chain、zero-copy、基准协议、render cache、图形
后端选择，以及 vendor routing 与 planar source 契约；含批次 8/9 新增的 8 条回归）。
- Windows 基准脚本：scripts/bench_timeline_export_windows.py
- AMD RX 6800 实测：tests/media/results/windows_timeline_export_amd_rx6800_2026-09-15.md
- AMD RX 6800 配置隔离冒烟：tests/media/results/windows_timeline_export_amd_rx6800_config_isolated_smoke_2026-09-15.md
- AMD device/pool probe：tests/media/results/d3d11_device_pool_probe_amd_rx6800_2026-09-15.md
- Windows 能力矩阵：tests/media/results/windows_capability_matrix.md

本文件是执行计划。设备探针、硬件编码或 OpenGL 路径通过，不能替代 D3D11 解码 surface→HLSL→硬件编码 surface 的真实零回读验收。
