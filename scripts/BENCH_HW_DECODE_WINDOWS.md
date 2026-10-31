# Windows 解码/编码硬件基准与接入（AMD RX 6800 + 16C/24T CPU 实测）

日期：2026-08-29 ｜ PyAV 18.1.0 ｜ Python 3.12 ｜ Windows 11 (26200)
GPU：AMD Radeon RX 6800 ｜ CPU：16 核 24 线程
脚本：`bench_hw_decode_windows.py`（完整）、`probe_hw_decode_windows.py`（后端探测）
原始数据：`bench_hw_decode_results.json`

## 方法

用 `tests/test_videos` 真实素材（H.264 / HEVC 10bit / ProRes 422HQ / ProRes 4444 /
DNxHR HQX），顺序解码（播放热路径，不走 seek），warmup 12 帧后计时：

- `raw_sw_decode`：纯 PyAV 软解（不解码后处理）——CPU 能力上限
- `raw_hw_decode`：PyAV `HWAccel(d3d11va)` 硬解（含每帧 GPU→CPU 下载）
- `prod_cpu_planar`：生产 CPU planar 源（`FFmpegMediaSource`，含 numpy 平面提取）
- `prod_hw_planar`：生产硬解 planar 源（`WindowsHwPlanarSource`）

## 结果（fps，越高越好）

| 素材 | CPU planar | 硬解 planar | 差距 |
|---|---|---|---|
| 2K H.264 8bit | **1448** | 145 | CPU 快 ~10× |
| 4K H.264 8bit | **486** | 46.9（21.3ms/帧） | CPU 快 ~10× |
| 2K HEVC 10bit | **453** | 152 | CPU 快 ~3× |
| 4K HEVC 10bit | **172** | 42.8（23.4ms/帧） | CPU 快 ~4× |
| 2K ProRes 422HQ | **473** | —（无硬解） | |
| 4K ProRes 422HQ | **103** | — | |
| 4K ProRes 4444 12bit | **61** | — | 最紧素材，仍 >25fps |
| 2K DNxHR HQX | **566** | — | |
| 4K DNxHR HQX | **153** | — | |

（ProRes/DNxHR 的硬解列本就不适用——AMD/NVIDIA 硬解单元不支持 Intra 剪辑码流，
生产路由也从未为其选硬解。）

## 结论与针对性优化（已实施）

1. **瓶颈根因**：PyAV `HWAccel` 的默认模式（`is_hw_owned=False`）对**每帧解码后
   立即同步下载到系统内存**，解码 N+1 帧必须等第 N 帧下载完，流水线完全串行化。
   换后端无效（d3d11va/d3d12va/dxva2 实测同为 ~24ms/帧@4K）；
   `extra_hw_frames=16` 无效；PyAV 18 无公开 API 可做手动流水线下载
   （`is_hw_owned=True` 后帧留在 GPU，但没有 `transfer()` 之类的取回 API）。
2. **路由修正**：`edit_gpu_decode_manager` 的 Windows 硬解优先分支与
   `factory.create_with_hwaccel` 现按 GPU 厂商路由——**仅 NVIDIA GPU**（NVDEC
   下载路径快，未在本机验证）或显式 `TOPOS_FORCE_WIN_HW_DECODE=1` 时硬解优先；
   AMD/Intel GPU 默认走 CPU frame-threaded planar 解码（本机快 4–10 倍）。
   `TOPOS_DISABLE_WIN_HW_DECODE=1` kill switch 语义不变。
3. **CPU 多线程解码是这台机器的最优解**：24 线程 frame-threaded 解码对全部
   9 个素材都有 ≥61fps（播放目标 25/30fps），4K ProRes 4444 最紧但也足够。

## 后端探测补充（probe 脚本）

- PyAV 对不可用的设备类型会**静默回退软解**（输出仍为源格式 yuv420p10le），
  探测时必须检查输出像素格式是否为硬件面家族（nv12/p010le/p016le）——
  `WindowsHwPlanarSource._verify_hw_output` 已做此门控。
- 本机真正生效的硬解后端：`d3d11va`（h264+hevc）、`d3d12va`（仅 hevc）。
  `dxva2` 打开成功但 4K 解码报错；`cuda/qsv/vulkan` 等全部静默软解。

## 复跑

```bash
.venv/Scripts/python.exe scripts/bench_hw_decode_windows.py            # 全量
.venv/Scripts/python.exe scripts/bench_hw_decode_windows.py --only H265  # 子集
.venv/Scripts/python.exe scripts/probe_hw_decode_windows.py "tests/test_videos/4K_H265.mov"
```

---

## 机器 B 重锚（RTX 4090 · 2026-10-24 · 计划表 B §B0.2）

环境：i9-13900K / RTX 4090（驱动 **621.01**，见 `docs/perf_env_matrix_windows_2026-10.md`
驱动漂移记录）/ Python 3.13.12 / PyAV 18.1.0 / commit `8782b4eec`。
产物：`scripts/bench_hw_decode_results_B0_20261024_run0{1,2,4}*.json`（d3d11va 默认 ×3）
与 `..._run03_nvdec.json`（`--hw-backend nvdec`）。

### 脚本口径修复（本轮变更）

1. **`setup_ms` 语义澄清 + 分段计时**：旧 `setup_ms` 把整个 bench 调用（open+warmup+
   steady+close）计成"初始化"，误导归因。现保留该字段（历史连续性）并新增
   `open_ms / first_frame_ms / warmup_ms / steady_ms`，顶层 `timing_semantics` 逐字段声明。
   例（4K H265）：raw_sw open≈1191ms 首帧≈189ms；raw_hw open≈1719ms 首帧≈68ms；
   prod_hw_planar open≈1273ms 而**首帧≈1ms**——因为 `WindowsHwPlanarSource.open()`
   内含首帧格式门控验证，真实首帧成本计入 open，不再虚标 steady 段。
2. **pre-existing 缺陷修复（真机暴露）**：旧 `HWDeviceType(hw_backend)` 按**值**查 int
   枚举，`d3d11va` 默认路径在 PyAV 18.1.0 上从来就 `ValueError`——历史 run01 之前本机
   无一次 d3d11va 默认跑通记录；现改按名索引 `HWDeviceType[name]`。DNxHR 素材名
   `12-bit`→`12bit` 修正（旧名假报"缺少素材"）。
3. **结构化状态与退出语义**：每 mode `status`（ok/ok_partial/eof_before_measure/error）、
   每 source 汇总、顶层 `summary`；退出码 0=全 ok、2=有缺项但有 ok 项、3=无一成功。
   run01–03 exit=2 正确反映当时素材名笔误的 2 个 missing，run04 修复后 exit=0。
4. **运行 manifest**：每份 JSON 记 commit/branch/dirty/args/驱动/CPU 核数——本轮实测
   抓到会话中途驱动 610.88→621.01 漂移，四轮基准已证全部在 621.01 上。
5. 线程/格式/缩放实录：raw 路径 thread_type/thread_count（AUTO/0=auto）、全路径
   `frame_size` vs meta 对比（`scaled` 字段，本轮全部 native 无缩放）。
6. `--include-topos`：Topos 原生 codec 项经 `check_topos_codec_available()` 强校验，
   缺库即 fail-closed 记 error（本轮未启用）。

### 结果（steady 段 fps；格式 `run04(run01/run02 | nvdec轮:run03)`）

| 素材 | raw_sw | raw_hw(d3d11va) | prod_cpu_planar | prod_hw_planar |
|---|---|---|---|---|
| 2K H264 8bit | 3072(2762/3037 \| 3012) | 508(516/519 \| 552) | 2949(2970/3009 \| 3053) | 521(525/508 \| 531) |
| 4K H264 8bit | 1081(1098/1090 \| 1074) | 129(136/126 \| **165**) | 841(833/827 \| 828) | 152(141/154 \| 150) |
| 2K H265 10bit | 394(391/396 \| 390) | 316(343/323 \| **546**) | 391(392/393 \| 391) | **621**(548/610 \| 441) |
| 4K H265 10bit | 130(124/123 \| 124) | 89(90/90 \| **147**) | 120(124/123 \| 122) | **133**(171/170 \| 130) |
| 2K ProRes 422HQ | 542(569/535 \| 550) | — | 505(529/523 \| 520) | — |
| 4K ProRes 422HQ | 142(149/141 \| 142) | — | 138(142/139 \| 136) | — |
| 4K ProRes 4444 12bit | 98(94/98 \| 98) | — | 96(93/94 \| 94) | — |
| 2K DNxHR HQX 12bit | 710(仅 run04) | — | 637(仅 run04) | — |
| 4K DNxHR HQX 12bit | 212(仅 run04) | — | 193(仅 run04) | — |

### 读数与噪声带（初始锚点，非 5 组交错采样；正式比较按计划 §1 协议）

- **H264 8bit：CPU planar 仍显著占优**（4K 841 vs 152，≈5.5×），与 RX 6800 历史
  （486 vs 46.9）模式一致但差距收窄；**H265 10bit 翻转：prod_hw_planar 反超**
  （2K 621 vs 391；4K 133 vs 120，run01/02 曾达 170）。即「下载往返税」在 10bit HEVC
  上被软解成本盖过——这是与旧 AMD 结论（HEVC 也 CPU 快 3–4×）的**实质分歧**，
  直接支撑 B1 保留帧 PoC 的立项动机（H264 走零拷贝收益最大，H265 已有净收益）。
- **nvdec(cuda) vs d3d11va（raw_hw 下载路径）**：4K H264 165 vs 129（+28%）、
  4K H265 147 vs 89（+65%）——NVIDIA 机 raw 层 nvdec 占优，与生产路由注释
  「仅 NVIDIA 硬解优先」一致。
- 初始噪声带（同驱动三轮）：CPU 路径 ±3%；HW 下载路径 ±5–10%；**H265 的
  prod_hw_planar 方差最大（2K 441–621，±17%；4K 133–171）**，正式冻结前需按计划
  §1 做 5 组交错采样；本表只作探索性锚点。
- 全部 9 素材 CPU planar ≥93fps，播放 25/30fps 预算内无解码侧瓶颈（多轨叠加场景
  另计，见帧预算报告 §瓶颈假设）。

### 对旧文档结论的修订（计划 B v3 落实）

上文 2026-08-29 AMD 节「PyAV 18 无公开 API 手动下载、is_hw_owned 后无取回 API」
的表述**不作为 B 线依据**：PyAV 18.1.0 存在 `is_hw_owned`（本机枚举/构造已证），
保留帧与消费接口按计划 B1.1 候选 2 真机验证，不从删除调用端 transfer 开始。

---

## 导出硬件编码（AMD AMF）接入记录

日期：2026-08-29。编码端与解码端结论相反——**硬编收益实打实**：

| 4K 导出编码 | CPU | AMD AMF | 加速比 |
|---|---|---|---|
| H.264 | 27.5 fps（libx264 全线程） | **90.6 fps** | 3.3× |
| HEVC | 15.5 fps（libx265） | **88.2 fps** | 5.7× |
| ProRes 422 HQ（app 默认） | 6.8 fps | 不存在（无厂商支持） | — |

（`bench_hw_encode_windows.py`，真实素材，warmup 12 帧后计时）

### 接入方式（src/shared/export/video_encoder.py）

- `FFmpegEncoderConfig.hwaccel`：默认 `'auto'`——Windows + PyAV 带 AMF +
  H.264/HEVC **8bit 4:2:0 SDR** 时自动启用 h264_amf/hevc_amf；`'amf'` 显式
  指定；`None` 强制 CPU。kill switch：`TOPOS_DISABLE_WIN_HW_ENCODE=1`。
- 门控原因（全部实测）：
  - AMF 拒绝 10bit 输入（yuv420p10le 探针 avcodec_open2 返回 EINVAL）→
    10bit 导出保持 CPU x265；
  - HDR（PQ/HLG）依赖 x265-params 元数据，AMF 不支持 → auto 不接管 HDR 预设；
  - h264_amf/hevc_amf 注册存在 ≠ 驱动可用 → open() 时 256x256 单帧探针
    验证，失败自动回退 CPU 重开（输出文件不会被截断）。
- 选项映射：码率模式 → rc=vbr_peak（CBR 时 cbr）+ b/maxrate/bufsize
  （初版误写 `b:v`——ffmpeg CLI 流指定符语法，avcodec_open2 静默忽略，
  2026-09-12 复查批次 2 真机实证修正）；CRF 模式 → rc=cqp
  （qp_i/qp_p/qp_b = crf）；x264/x265 preset → AMF quality
  （speed/balanced/quality）。

### 验证（scripts/verify_hw_encode_export.py，全部通过）

- 路由矩阵 8 项（auto/显式/kill switch/10bit/HDR/422 格式回退）
- 端到端：4K ProRes 素材 → FFmpegVideoEncoder 全路径，AMF 19.7fps vs
  CPU 5.4fps（3.6×；剩余耗时为 rgb→yuv 转换层，非编码器）；输出回读
  帧数一致、非黑帧
- HEVC 10bit 自动回退 CPU、kill switch 强制 CPU、探针对不存在编码器
  返回 False
- 既有 96 项编码器测试全过（3 项编码器名断言已更新为路由感知）

### DNxHR 快速中间码修复（同日）

发现并修复：`ENCODER_NAME_MAP['dnxhr'] = 'dnxhr'` 指向不存在的 FFmpeg
编码器——FFmpeg 中 DNxHR/DNxHD 的编码器名是 **dnxhd**。此前交付页/
时间线导出里全部 5 个 DNxHR 档位一旦选择就会在 `add_stream` 时报错，
即 DNxHR 导出从未可用过。

修复后（`verify_hw_encode_export.py` 端到端，4K 真实素材）：

| 编码 | 速度（含转换层） | 输出 profile |
|---|---|---|
| DNxHR HQX 10bit | **12.0 fps** | DNXHR HQX ✓ |
| ProRes 422 HQ（对照，同管线） | 3.7 fps | HQ ✓ |

DNxHR HQX 同管线对比 ProRes 422 HQ 快 **3.2×**、超实时（纯编码对比
33.6 fps），是 Windows 上替代 ProRes 中间码的现实选项。profile 现以
FFmpeg 私有选项 `profile=dnxr_<name>` 显式下发（兼容短名/全名）；无
profile 时的码率回退改走 `b` 选项（原 `bit_rate` 属性是 32 位整数，
4K 444 高帧率下溢出；初版 `b:v` 为静默忽略键，复查批次 2 同修）。

### 导出管线三刀优化（同日，微基准驱动）

对 4K 帧（BGR uint8 → yuv422p10le）逐环节测量后发现：`np.flip` 返回负
stride 视图，真正代价在 `from_ndarray` 对非连续数组的逐行跨距拷贝
（~80ms/帧）；FFmpegVideoEncoder 每帧同步"转换→编码"串行；x264/x265/
dnxhd 被 `threads=4` 限制。

| 改动 | 内容 | 4K 实测收益（端到端） |
|---|---|---|
| BGR 直转快路径 | bgr24/bgra 直接喂 swscale，免 flip+跨距拷贝（输出 bit 级一致，max diff=0） | 转换链 100→57ms/帧 |
| 线程放开 | timeline_export threads=4→0（FFmpeg 全核）；FFmpegEncoderConfig 默认 0（播放期渲染缓存仍显式 2） | x264 CPU 导出 4.7→9.3fps |
| 转换/编码流水线 | `pipelined_conversion`（默认关）：后台线程转换与主线程编码重叠，队列 maxsize=2 背压，close() drain 保序 | DNxHR 12→17-20fps |

端到端汇总（verify_hw_encode_export.py，4K 真实素材，全部通过）：
DNxHR HQX **17-20fps**、H.264 AMF ~17fps、H.264 CPU 9.3fps、ProRes
4.4-4.6fps（prores_ks 编码器本身是地板）。x265 10bit 仍 1.8-1.9fps
（编码器本身重量级，建议 HEVC 10bit 交付继续用或等待未来 GPU 10bit）。

97 项编码器测试全过（新增流水线语义测试：close 后计数完整）。

### 实时线攻坚：16bit 快路径 + 稳态口径（同日续）

用户问"H.264 为何到不了实时"后的深挖发现：此前所有端到端数字都被
**16bit 转换路径拖累**——10bit 源（ProRes/DNxHR 素材）的合成帧是
uint16 BGR（bgr48le），一直走 `np.flip`+跨距拷贝老路（4K 实测
~85ms/帧，仅转换）。12 帧短窗口还有 x264/x265 lookahead 预热失真，
正式改用 120 帧稳态口径（warmup 40）。

改动：BGR 直填快路径扩展到 uint16（`bgr48le`，输出与 flip+rgb48le
传统路径 bit 级一致，全平面 diff=0；奇数宽/非连续自动回退）。
4K 转换 85→35ms/帧。

稳态端到端（120 帧，生产形态 bgr48le 帧，FFmpegVideoEncoder 全路径）：

| 导出路径 | 优化前 | 稳态终值 | vs 25fps 实时线 |
|---|---|---|---|
| 4K DNxHR HQX | ~5（曾为不可用） | **27.3 fps** | ✅ 超实时 |
| 4K HEVC AMF | ~12 | **23.8 fps** | ≈ 贴线 |
| 4K H.264 AMF | ~12 | **20.9 fps** | 0.84× |

剩余瓶颈即 uint16 转换本体（~35ms：update ~15ms + swscale 16bit 下转
~20ms），已无低垂果实；下一档需要 GPU 渲染直出 YUV 平面（结构性改造）。
97 项编码器测试全过。

### W6-02 收口：10bit HEVC AMF 接线 + 接缝统一 + 结构化诊断（2026-09-12）

前期探针（本机 RX 6800 / PyAV 18.1.0）：hevc_amf 直收 yuv420p10le 报
avcodec_open2 EINVAL（两次独立实测）；AMF 的 10bit 输入是半平面
p010le（probe_amf_encode_windows.py 已证可用）；swscale
yuv420p10le→p010le replan 可行 → 10bit 走「p010le 转换路线」。

改动（video_encoder.py）：
- `amf_input_pix_fmt()`：AMF 输入格式映射（yuv420p 直收；10bit HEVC→
  p010le；h264 10bit/422/12bit 拒绝）；`_encode_pix_fmt` 与配置格式解耦
- 接缝统一：`amf_encoder_available`/`get_encoder_name` 改消费 encoder.py
  的 `HWACCEL_ENCODER_NAME_MAP`/`probe_hwaccel_encoder_available`，
  删除平行实现（'h265' 别名不再可能探到非法名 'h265_amf'）
- 结构化诊断：探针失败回退 CPU 时产出
  `encoder_hw_unavailable:amf:<探测异常摘要>` → `hw_fallback_reason`
  属性 → `ExportOutcome.hw_fallback_reason` → worker 留痕日志
- 顺带修复两个潜伏转换缺陷：① planar 路径 `np.array(plane)` 返回扁平
  uint8、形状永不匹配 → 平面被静默跳过**输出黑帧**（从未在生产触发，
  生产走 packed BGR）——改为字节级填充（含 line_size 对齐）；
  ② packed BGR→p010le 掉进不支持的 `from_ndarray` 兜底——p0 前缀并入
  YUV 家族分支；BT.2020 numpy 路径补 p010 半平面子采样识别
- 显式 amf + HDR 放行但告警（AMF 不承载 ST2086/MaxCLL）；auto 模式
  HDR 仍锁 CPU；render_cache_encoder 维持 auto（显式决策注释）

端到端稳态对照（verify_hw_encode_export.py 全部通过，4K 真实素材）：

| 导出路径 | W6-02 收口前 | 收口后 | 备注 |
|---|---|---|---|
| 4K HEVC **10bit**（应用内默认格式） | 1.8-1.9 fps（CPU x265） | **22.6 fps（AMF p010le）** | **~12×**，应用内 HEVC 导出的主要解锁 |
| 4K HEVC 8bit | 23.8 fps | 24.5 fps | 持平（口径差异） |
| 4K H.264 8bit | 20.9 fps | 21.2 fps | 对照 CPU 10.6 fps |
| 4K DNxHR HQX | 27.3 fps | 26.2 fps | 持平 |

10bit 与 8bit AMF 仅差 ~8%：p010le 转换层未成为硬地板（pipelined_
conversion 重叠生效）。113 项编码器测试全过（新增 10bit 门控/格式
映射/reason 形态/平面不丢色度 5 项）。

---

## NVIDIA RTX 4090 真机（W6-01 NVDEC/NVENC，2026-09-12）

> GPU：NVIDIA GeForce RTX 4090（0x10DE/0x2684，24GB）+ iGPU Intel UHD 770，
> 驱动 610.88 / CUDA 13.3，Win11 26200，PyAV 18.1.0，Python 3.13。
> 原始数据：`bench_hw_decode_results_nvidia_rtx4090.json`（解码）、
> `tests/media/results/windows_nvenc_encode_probe_nvidia.json`（编码探针）。

### 解码基线（顺序 warmup 12 帧，NVDEC 后端）

| 素材 | CPU planar | NVDEC planar | 差距 | 25fps 预算 |
|---|---|---|---|---|
| 4K H.264 8bit | **945** | 162.7（NV12） | CPU 快 5.8× | 双双足额 |
| 2K HEVC 10bit | 863 | 685（P010） | CPU 快 1.3× | 双双足额 |
| 4K HEVC 10bit | **320** | 196（P010） | CPU 快 1.6× | 双双足额 |

- 与 AMD 机（硬解 4K 仅 ~47fps）对比：NVDEC 下载 4K 162-196fps，**~3-4×**。
- K-W1 判定输入（NVIDIA 行）：download 架构下 4K 单流播放预算双双足额
  （CPU 12.8×、NVDEC 7.8×），zero-copy 立项依据仍只能来自多轨并发
  （decode_layers 遥测）。

### NVENC 编码（W6-01 接线后端到端，4K 真实素材 crf20）

| 导出路径 | NVENC | CPU 基线 | 加速比 |
|---|---|---|---|
| 4K H.264 8bit（workers=3） | **32.9 fps** | x264 18.7 fps | 1.8× |
| 4K HEVC 10bit（workers=3） | **30.2 fps** | x265 4.0 fps | **7.6×** |
| 4K HEVC 10bit（workers=1） | 26.5 fps | — | — |
| 4K AV1 10bit（workers=3） | **29.4 fps** | — | Ada AV1 编码单元 |

- 自动接入语义：`detect_hw_vendor()`（DXGI）→ nvidia 路由 NVENC、amd
  路由 AMF；`hwaccel_engaged` 门控（Windows + vendor + 格式 + SDR + 注册）；
  探针失败回退 CPU 记 `encoder_hw_unavailable:nvenc:<detail>`。
- 输入格式映射：yuv420p 直收（h264/hevc/av1）；hevc/av1 10bit→p010le
  转换路线（与 AMF 同构）；h264 10bit/4:2:2 不支持锁 CPU。
- 探针实测（720p 渐变 30 帧）：h264/hevc/av1_nvenc 8bit + hevc/av1
  10bit 全部「open + 编码 + 回解」通过；8Mbps 下 NVENC PSNR 低于 CPU
  基线 10-20dB（合成素材码率不跑满 + NVENC 无 lookahead，只证码流
  sane）——同码率画质 A/B 留真实素材（W6-01 正式验收项）。
- 本机附带实证「注册 ≠ 驱动可用」：h264_amf 注册存在但 NVIDIA 驱动上
  open 失败（Errno 1313558101）——跨 vendor 显式点错通道由探针兜底回退。
