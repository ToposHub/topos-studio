# Topos 编解码 macOS 验证与优化计划

2026-11-27 · Apple Silicon、原生 Intel Mac、RX 6900 XT 黑苹果

2026-11-28 补记 · 素材库播放器、`.toos` 序列交付正确性与性能

用同一台机器的改前和改后版本确认加速，用同一份 10-bit 4:2:2 母版确认画质。首轮优先跑 1080p 和 UHD 的 Proxy、LT，再测 422、HQ 和高位深控制组。Windows 数据供追溯，Mac 的实际吞吐、画质和后端以各台真机结果为准。

## 1. 当前改动在 macOS 的覆盖范围

| 改动 | Apple Silicon 原生 arm64 | Intel Mac 与 x86_64 黑苹果 | 真机要确认的证据 |
| --- | --- | --- | --- |
| RANS2 直接写 YUV 平面、纯 DC 块提前识别、反量化准备简化、跳过无用 Rice 预热 | 共用原生 C 路径，符合码流条件即可使用 | 同左 | 同一旧 MOV 的完整 Y/U/V 逐帧一致，记录新旧吞吐、P95 和编译库哈希 |
| 切片内去块、跨切片边界合并、批次去块调度 | 共用调度代码，像素滤波执行通用实现。这批手写 AVX2 去块没有 NEON 对应实现 | 支持 AVX2 的原生 x86_64 进程有条件使用新内核，其余走通用路径 | 测原尺寸 V7 RANS2 10-bit 4:2:2 去块码流，复核跨片边界、低档吞吐和尾延迟 |
| AVX2 分发修正、单 AC 块融合、x86 纯 DC 写入及 AVX2 变换优化 | 使用现有 NEON 前向变换、量化及部分逆变换等路径；这批 x86 SIMD 专项无 ARM 版本 | SSE2/AVX2 各按自己的条件启用，CPU 及其运行环境决定 AVX2 | 实际后端 `neon`、`avx2` 或 `scalar`，同时记录 CPU 能力和进程架构。6900 XT 型号不参与 CPU SIMD 选择 |
| 批量预读、解码 worker 公平分配 | 共用 worker 分配和预读能力。macOS 播放预算上限为 16，默认取可见 CPU 数与上限较小值。默认播放预读为 0，导出预读最多 4 且受内存预算约束 | 同左，含黑苹果。Windows 特有的 24 worker 及符合条件的低档自动播放预读不会触发 | 验证默认 `read_ahead_depth=0`，单流及多流分配，并用深度 0/2/4 单独试验 |
| Windows CRC32 快路 | 继续使用可移植表实现 | 同左 | 记录 CRC 或数据校验在纯编码和纯解码中的占比，保持 IEEE CRC32 校验结果一致 |
| UHD Proxy、1080p LT 画质策略 | 同一生产 `ToposVideoEncoder` 条件下新导出用 `qmatrix_id=0` | 同左 | 3840×2160 Proxy 与 1920×1080 LT 分别验证。1080p Proxy、UHD LT 做范围控制组 |
| UHD HQ 首帧码控、旧 LT 逐包直拷 | 共用 Python 策略。HQ 满足条件时探测相邻 QP，旧 LT 直拷保留原矩阵 | 同左 | HQ 五代链验证选包与码率，旧 LT 加 PCM 后校验每个视频包 |
| `.toos` 帧内核心 | 图片序列和同配置 MOV 共用原生帧编码及解码核心。逐文件 I/O、HALF 码值转换和时间线取帧各有自己的路径 | 同左，实际 SIMD 依赖 CPU 和构建产物 | 分别测原生核心、逐文件交付、媒体源读帧、时间线完整导出。先验逐帧正确性 |

画质矩阵策略针对 SDR Rec.709 限幅、不透明、10-bit 4:2:2、profile 3、RANS2 帧内 MOV 的码控导出。固定 QP、AQ、HDR、Alpha 和其他画幅用于确认分支边界。HQ 的探测还要求初始 QP ≥ 64、原包达到目标字节数的 98%、没有硬性文件或视频字节上限。选中的新包须达到目标的 98% 且不超目标和原包。以前导出的文件保持原来写入的矩阵和码流，验证新画质需要重新从母版编码。 [导出分支](../../src/shared/export/topos_encoder.py) · [原生解码](../../native/topos_codec/src/codec/codec.c) · [SIMD 分发](../../native/topos_codec/src/simd/dispatch.c) · [预读](../../src/shared/media/topos_source.py) · [worker 预算](../../src/shared/codec/decode_worker_budget.py)

支持的 Topos 4:2:2 原尺寸流在 macOS 可走原生解码器写 IOSurface、Metal 消费的路径。原生重建仍由 CPU 执行。记录 `metal_surface_frames`、`metal_surface_failures` 和实际渲染设备。ProRes/H.264/H.265 另按实际会话记录 CPU 或 VideoToolbox，区分硬解、硬编、零拷贝和 CPU 下载。M1 Pro/Max 等有专用 ProRes 媒体引擎的机器也按实际会话验证。 [Topos Metal 输出](../../src/shared/media/topos_source.py) · [媒体入口](../../src/shared/media/factory.py) · [Apple 芯片规格示例](https://support.apple.com/en-us/111901) · [VideoToolbox 解码状态](https://developer.apple.com/documentation/videotoolbox/kvtdecompressionpropertykey_usinghardwareacceleratedvideodecoder) · [VideoToolbox 编码状态](https://developer.apple.com/documentation/videotoolbox/kvtcompressionpropertykey_usinghardwareacceleratedvideoencoder)

## 2. 机器与环境登记

| 机器 | 优先跑哪些项 | 额外记录 | 本机填写 |
| --- | --- | --- | --- |
| M 系列，原生 `arm64` | 先跑 A0、A9，再测 Proxy/LT、4K120、素材库 Retina 大窗和图片核心。手边有基础款和 Pro/Max/Ultra 时各选一台 | 芯片全名、P/E 核、内存、显示刷新率、ProRes VT 会话状态，`neon` 实际分发 | — |
| 原生 Intel Mac，`x86_64` | 先跑 A0、A9，再测 Proxy/LT、顺播与快拖、DNxHR 444 和 FP16 大窗/导出 | Intel CPU 全名、AVX2、实际 GPU、核显/独显/外接显卡、后备路径。无 AVX2 的在用机型可补一台 | — |
| RX 6900 XT 黑苹果，`x86_64` | 先跑 A0、A9，核对 Metal 设备、素材库 OpenGL 呈现、VT 会话、播放退出与回读 | CPU 厂商和型号、显卡 Device ID、显存、macOS build、OpenCore 和相关驱动或 kext 版本、SMBIOS 型号、核显状态、显示器接线和刷新率。Intel 与 AMD CPU 配置各记一行 | — |
| M 系列上已有 Rosetta 环境 | 可选 A0/A1/A4，另记一组 | 进程 `x86_64`、转译状态、加载的 `x86_64` 库切片及其实际 SIMD，保留原生 arm64 数据 | — |

Apple 公布的 RX 6900 XT 支持信息适用于其文档中的 Intel Mac eGPU 配置（列出 Device ID `0x73BF`）。黑苹果须按这台机器的 macOS、CPU、显卡和系统配置做实测。先冻结当前能启动的配置，记录 Topos 选中的 Metal 设备以及 `VideoToolbox` 解码/编码会话状态。出现硬件回退时，用同一版本复跑 CPU 路径定位。只转交型号和版本，设备序列号等机器标识留在本机。 [Apple eGPU 说明](https://support.apple.com/en-ie/102363) · [Rosetta 与应用架构](https://support.apple.com/en-us/102527)

项目已有 i9-9900K、RX 6900 XT 16 GB、macOS 15.3.1 的机器基线。11 月 23 日的探针识别了 6900 XT，NV12、P010、BGRA 和 4:2:2 测试表面可用，同时记录了 `pyobjc_corevideo_route_unusable`。11 月 25 日旧库回归的 UHD25 H.264 播放以 VT 零拷贝为主。若测试同一台机器，沿用它作为 6900 XT 对照并保留这些旧记录，补测本轮 Topos 和 ProRes。已有证据覆盖该机对应的表面或 H.264 路径。 [机器基线](../perf_baseline_freeze_mac_2026-10.md) · [6900 XT Metal 探针](../macos_metal_migration/evidence/capabilities-intel-amd-2026-11-23.md) · [旧库播放实测](../validation/2026-11-25-macos-h264-main10-regression.md)

## 3. 版本和素材准备

- **A 组**是本轮编解码改动之前的 macOS 源码及其 Release dylib。**B 组**是包含本轮 C、Python 改动的源码及其 Release dylib。当前 Windows 源码的性能和画质改动还在工作树，单靠 `git pull` 无法拿到整套。同步完整审阅过的版本，带上新文件 `native/topos_codec/src/simd/deblock_avx2.c` 和 `.h`，各组固定一份源码快照。Windows DLL 作为 Windows 对照记录，macOS 两组分别编译自己的 `.dylib`。
- 两组先跑同一组固定输入和相同配置，每次切换启动新进程。拆分归因时先用旧 MOV 跑解码 A/B，再用同一 10-bit YUV 母版和固定 QP 跑编码 A/B，最后用新旧**默认码控预设**跑产品 A/B。前两项允许要求逐位一致，产品画质策略故意改变的输出用画质和实际码率验收。
- 保存 Git HEAD、是否 dirty、编译参数、dylib SHA-256、输入 SHA-256、实际帧数、测试时系统状态和所有跑次。USB 移动盘用于搬运，高吞吐对照放同一块本地 SSD。插电、固定电源模式、显示配置、软件版本及后台负载，交替执行 `A → B → B → A`，每组至少六次，延迟按实际帧数统计。

| 素材 | 要带什么 | 用途 |
| --- | --- | --- |
| 乌兰布统 120 fps | 从 Windows `D:/测试素材/乌兰布统_120fps_播放压测` 复制。`Topos_120fps` 留存两种分辨率的旧 Proxy 和 LT，供旧码流 A/B。Net Proxy、Proxy、LT、422、HQ、LP、4444、4444 XQ 全档位从 `Topos_120fps_优化版_2026-11-27` 取。保留 `120fps_素材与逐帧校验.json` 以及两条 `ProRes422.mov` | 首测 Proxy/LT，扩展八档。1920×1080 和 3840×2160 各 2693 帧、120/1、约 22.44 秒。源帧加速保留，每条已有 2693 个不同的解码帧哈希。这里文件名中的「2K」指 1080p。DCI 2048×1080 或 4096×2160 有需求时另跑分支边界 |
| ProRes、DNxHR、H.264 和 H.265 | 从 `达芬奇转码_测速_2026-11-27` 取 `前487帧_扩展档位`（北海公园全档、圆明园02 的 ProRes 422 Proxy/LT/Standard）、`全长_422_HQ对照`（北海公园和圆明园02）及两种分辨率的 `H264_H265_全长`。带上原素材清单 | 对齐源、起始帧、不同帧率及实际位深，标注 `DaVinci Resolve`、`prores_ks` 或 `VideoToolbox`。乌兰布统现有 120 fps ProRes 文件是 422 Standard，做低档对照时另建同画面 Proxy/LT，核对 `120/1` 和帧内容 |
| 画质母版与对照 | 从 Windows `bench_out/quality-parity-20261127/cases` 取 `aom-aerial-hd`、`aom-food-hd`、`chart-hd-final`、`chart-uhd`、`svt-midnight-uhd` 内的 `case.json`、`reference.yuv`、`reference.mov`、`resolve-control.mov`、`resolve-<档>.mov` | 前两段自然素材和图卡检查 LT，UHD 图卡和 Midnight Sun 检查 Proxy，Midnight Sun 验证 HQ 五代。再用乌兰布统、北海公园、圆明园作独立验证 |
| 兼容性素材 | 一段已知矩阵 4 的旧 1080p LT 文件、带真实 Alpha 的 4444、10-bit HEVC Main10、H.264 和一段 HDR 短样，另备 PCM 双声道 | 旧包直拷与添加音轨、Alpha、HDR、硬件路由、seek/切点和色彩范围回归 |
| `.toos` 序列及配对 MOV | 从 `bench_out/diag-topos-image-20261128/new-1` 首批取 `422-low`、`444-high`、`422-int12`、`444-int12-xq`、`float-ultra`、`raw12-4`、`raw16-4`。每档带 12 张连续 `.toos` 及同名 MOV，扩大验证时再取其余 17 组。保留 `summary.json`、`workflow.jsonl`、`workflow-verified.json`、`matrix.py`、`workflow.py` 和 Windows 原始计时 JSONL | SDR 序列重导、CFA 直出、FP16 样本域和同码流对照。CFA 样片由乌兰布统画面生成，随后再补真实相机 RAW。每组现有素材均为 12 张不同的 UHD 画面 |

`bench_out` 下的画质和速度报告、脚本、冻结素材需单独同步。Windows 的测速记录在 `bench_out/diag-codec-target-20261127/report.md` 和 `bench_out/diag-codec-polish-20261127/report.md`。画质记录在 `bench_out/quality-parity-20261127/parity-progress.md` 和 `parity-round2-report.md`。图片基准的 `source/`、`old-*`、`new-2/3` 和 `smoke-*` 都可从母版重建。搬运前同时备份本轮未提交的计划、工作树源码和实测库清单。

## 4. 逐项验收表

| 编号 | 做什么 | 通过条件与记录项 | M 系列 | Intel Mac | 6900 XT 黑苹果 |
| --- | --- | --- | --- | --- | --- |
| A0 · 必测 | 读取实际加载库、Mach-O 架构、版本和 SIMD。源码启动与安装包启动各一次 | A/B 库 SHA 对应到各组，CPU 能力和实际分发都有记录。两种启动方式都能导入 Proxy/LT 并导出回读 | ☐ | ☐ | ☐ |
| A1 · 必测 | 同一旧 MOV，1080p/UHD Proxy、LT，用原尺寸 YUV 顺播。每条取 0、640、1700 三段，每段前 12 帧预热、后 96 帧计时，或先用脚本做 1 个 240 帧长窗 | 相同帧号、Y/U/V 哈希和帧数，损坏和触发错误补偿的切片均为 0。记录每窗 fps 中位数、逐帧 P95/P99、worker、预读和 RSS。加速量按本机 `B/A−1` 计算 | ☐ | ☐ | ☐ |
| A2 · 必测 | A/B 固定 QP 纯编码，同一母版、线程、切片行数、矩阵和色彩设置。再测默认码控导出。每组至少六次，中位数 | 固定设置的视频包逐帧摘要一致，产量正确。默认策略另记实际视频字节、首帧耗时和整段 fps，填写 A/B 变化 | ☐ | ☐ | ☐ |
| A3 · 必测 | 同机 ProRes Proxy/LT/422/HQ 对照。用相同画面、输出尺寸、位深、帧号和时间码，Topos 走项目源，CPU 对照用 FFmpeg planar。另从应用记录 VT/Metal 和 Resolve 完整导出 | 低档 CPU 同口径目标 `ProRes fps ÷ Topos fps ≤ 1.50`。应用端另填实际硬/软路径及比值。DNxHR 标明 HQX/444 的实际位深，H.264/H.265 作为长 GOP 播放/跳转控制组 | ☐ | ☐ | ☐ |
| A4 · 必测 | 剪辑页原尺寸播放、随机跳转、拖拽、暂停后续播、切点、两层重叠，1080p120 与 4K120 各做两次，每次完整循环测试 ≥2 分钟。然后测单条 4K60 | 记录交付帧数、漏帧率、卡顿、首帧 P95/seek P95、RSS、显示刷新率、Metal 设备及 CPU/IOSurface 路由。120 fps 每帧预算 8.33 ms。60 Hz 显示器只给出解码/交付结论；120 Hz 显示端另外验收呈现 | ☐ | ☐ | ☐ |
| A5 · 必测 | 重新导出 1080p LT、UHD Proxy、UHD HQ，核对策略触发。跑原方案的默认码率和同有效视频码率两套画质 | LT、Proxy 样本描述矩阵 0，控制组符合原策略。正式同码率比较的实际视频包量差 ≤2%，录入 PSNR Y/U/V、VMAF、MS-SSIM、CAMBI 和局部细节 | ☐ | ☐ | ☐ |
| A6 · 必测 | 旧 LT 原样直拷和新增 PCM 音轨，旧 MOV 回读、带 Alpha 4444、HDR 控制组。Windows 读 Mac 产物，Mac 读 Windows 产物 | 旧 LT 源及产物 `qmatrix_id` 一致且每个视频包一致；音频采样数和对齐正确；跨平台帧数、位深、色域、范围和 Alpha 符合预设 | ☐ | ☐ | ☐ |
| A7 · 扩展 | 16 个 Topos 样本（八档 × 两分辨率）、多流播放、播放时后台导出、不同 worker/预读。黑苹果补黑屏、绿帧、挂起/恢复、多显示器切换及 VT fallback | 记录错误/回退原因、4K 长跑温度与吞吐衰减、RSS 峰值及退出释放。默认与试验配置分别保留日志。若只在某机器复现，附 macOS build、GPU 路由和最短复现素材 | ☐ | ☐ | ☐ |
| A8 · 必测 | 素材库播放 ProRes 422 HQ/4444 XQ、DNxHR HQX/444 及 Topos Proxy/LT。普通窗口和最大窗口均测试 | 在 25/29.97 fps 窗口核对首帧、画面切换、播放索引、暂停拖动与续播。分别记录解码、Qt 呈现、墙钟落后、实际设备像素和屏幕刷新率 | ☐ | ☐ | ☐ |
| A9 · 交付阻塞 | 在媒体池成组导入 422 与 FP16，默认 SDR 时间线重导 `.toos`。将 RAW12/16 的 CFA MOV 和 `.toos` 各放到无效果时间线，分别导出 MOV 和 `.toos` | 回读的帧数、帧序和有效画面与输入一致，422/FP16 的 12 张输出各有 12 个不同哈希。RAW 保留正确的 CFA 相位、位深和缩放几何。遇到缺帧、不可交付 RAW 的效果或 RGB 输入时给出明确错误 | ☐ | ☐ | ☐ |
| A10 · 必测 | 先测 7 个代表图片规格，再扩展到 24 种配置。先跑固定参数的核心对照，再测 `.toos` 和 MOV 端到端 | A/B 固定设置有码流与可见平面一致性证据。填写原生编码、封装/定稿、开源与读帧、时间线导出、P95/P99。A9 通过后再用重导结果做性能验收 | ☐ | ☐ | ☐ |

首轮顺序为 A0 → A9 → A1/A2 低档 → A4/A8 播放 → A5 画质 → A10 图片性能。若 A9 复现，保留逐帧证据并继续独立核心对照，修复后再验收完整交付。播放建议目标为单流掉帧率 ≤1%，同时检查明显停顿和连续掉帧。若加速差值在 ±3% 内、相邻跑次波动大或结果接近验收线，增加到至少 12 次交错测试并记录区间。统一记录 `fps = 实际交付帧数 / 计时秒数`。完整导出从提交任务计到文件收尾，含输入解码、渲染、编码和封装。120 fps 若 Resolve 的目标时间线或导出预设无法保持 120/1，改用已验证的 120 fps 文件做播放测试，并另建两边相同的 24/60 fps 导出测速组。

## 5. Mac 终端操作

### A0 · 构建、检查和留档

在 A 或 B 的独立仓库根目录执行。以下用该工作树的 Python 虚拟环境，首次启动和依赖安装按项目说明操作。`TOPOS_CODEC_LIB` 用于固定基准取样的 dylib。 [安装说明](../getstarted/installation.md)

```bash
PY="$PWD/.venv/bin/python"
ARCH="$("$PY" -c 'import platform; print(platform.machine())')"
GROUP="B"  # A 工作树改成 A
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$PWD/bench_out/macos-codec/$ARCH/$GROUP/$RUN_ID"
BUILD="$PWD/native/topos_codec/build/mac-validation-$ARCH"
mkdir -p "$OUT"

# 每个 A/B 工作树使用自己的构建目录。Intel Mac 和黑苹果预期 x86_64。
cmake -S native/topos_codec -B "$BUILD" -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_OSX_ARCHITECTURES="$ARCH" -DTOPOS_ENABLE_TESTS=ON
cmake --build "$BUILD" --parallel "$(sysctl -n hw.ncpu)"
ctest --test-dir "$BUILD" --output-on-failure > "$OUT/ctest.log" 2>&1
export TOPOS_CODEC_LIB="$BUILD/topos_codec.dylib"

{
  sw_vers
  uname -m
  sysctl -n machdep.cpu.brand_string
  sysctl -n hw.ncpu hw.memsize
  sysctl -n sysctl.proc_translated 2>/dev/null || true
  system_profiler SPDisplaysDataType
  "$PY" -V
  "$PY" -c 'import av; print(av.__version__, av.library_versions)'
  git rev-parse HEAD
  git status --short
  file "$PY" "$TOPOS_CODEC_LIB"
  lipo -archs "$TOPOS_CODEC_LIB"
  shasum -a 256 "$TOPOS_CODEC_LIB"
  ffmpeg -version | head -n 1
} > "$OUT/environment.txt" 2>&1

"$PY" native/topos_codec/scripts/dll_load_test.py "$TOPOS_CODEC_LIB"
"$PY" tools/bench_manifest.py --allow-dirty > "$OUT/manifest.json"
"$PY" - <<'PY' | tee "$OUT/native-route.txt"
import ctypes
import platform
from src.shared.codec.topos_binding import ToposCodec, load_library
from src.shared.codec.decode_worker_budget import DECODE_WORKER_BUDGET

lib = load_library()
codec = ToposCodec(lib)
backend = getattr(lib, "tc_dev_simd_backend", None)
print("runtime", platform.machine(), lib._name, codec.version())
print("features", codec.cpu_features(), "slice_threads", codec.slice_threads())
if backend is not None:
    backend.restype = ctypes.c_char_p
    print("simd", backend().decode())
print("decode_worker_limit", getattr(DECODE_WORKER_BUDGET, "max_workers", None))
print("decode_worker_budget", DECODE_WORKER_BUDGET.describe())
PY

"$PY" -m pytest -ra tests/native/test_decode_worker_budget.py \
  tests/media/test_topos_export.py tests/media/test_topos_packet_copy.py \
  tests/media/test_topos_source.py > "$OUT/pytest.log" 2>&1
```

若 dylib 没导出开发诊断符号，记录 `tc_query_cpu_features` 对应的 flags 和原生分发测试结果。Python `platform.machine()` 与 `lipo -archs` 一起存档。A 组若早于 `max_workers` 属性，读取 `DECODE_WORKER_BUDGET.describe()` 的 `capacity`。旧代码和新代码若在同一 Git HEAD 上有未提交差异，`--allow-dirty` 允许输出观察结果。正式验收给 A/B 各自固定的源快照和库哈希。缺媒体夹具的 skip 和旧版已有失败登记原因后重跑相关组。

**安装包复测。** PyInstaller 先看 `TOPOS_CODEC_LIB`，再找 SDK `dist` 和构建目录。自动搜索认 `libtopos_codec.dylib`，仓库脚本和 CMake 常生成 `topos_codec.dylib`。如果两者并存，源码跑成功也可能把另一份库装进包或漏包。打包前用绝对路径显式指定目标库，核查包内库的版本、架构、ABI、实际加载位置并跑回读。搬到没有源码和开发库的环境，用 `env -u TOPOS_CODEC_LIB` 启动安装版，以正在运行的进程加载路径为准，例如 `vmmap <PID> | grep -i topos_codec`。签名或重定位可能修改文件字节，记录打包输入和包内最终哈希，用嵌入提交、架构和回读验证对应关系。包验证脚本当前只查带 `lib` 前缀的 macOS 文件名且只查包根/`_internal`，遇到其他布局时结合 `find` 与实际加载结果判断。Universal 发行需检查应用及库各自具备目标切片，并在两种原生架构各做冒烟。 [真实打包查找](../../packaging/pyinstaller/base_spec.py) · [包验证脚本](../../scripts/verify_product_bundle.py) · [SDK 构建](../../native/topos_codec/scripts/build_sdk.sh)

### A1–A3 · 纯解码、纯编码和 CPU 对照

```bash
MEDIA="$HOME/Movies/测试素材"
P4="$MEDIA/乌兰布统_120fps_播放压测/Topos_120fps/4K/乌兰布统_4K_120fps_Topos_422_Proxy.mov"
P2="$MEDIA/乌兰布统_120fps_播放压测/Topos_120fps/2K_1080p/乌兰布统_2K_120fps_Topos_422_Proxy.mov"
shasum -a 256 "$P4" "$P2" > "$OUT/inputs.sha256"
for SOURCE in "$P2" "$P4"; do
  "$PY" tools/bench_topos_playback_path.py --input "$SOURCE" --frames 240 \
    --repeats 6 --random-seeks 60 --threads 4 --allow-dirty \
    | tee "$OUT/$(basename "$SOURCE").decode.log"
done

# 复制同一 v210 参考 MOV 到两组均可访问的位置。
MASTER="$PWD/bench_out/quality-parity-20261127/cases/svt-midnight-uhd/reference.mov"
env -u TOPOS_CODEC_PROFILE "$PY" tools/bench_encode_stages.py \
  --input "$MASTER" --profile proxy --frames 12 --warmup 2 --threads 4 \
  --no-native-profile --allow-dirty --out-root "$OUT/encode-wall"
TOPOS_CODEC_PROFILE=1 "$PY" tools/bench_decode_stages.py \
  --videos "$P4" "$P2" --frames 120 --allow-dirty > "$OUT/decode-stages.log"
```

旧码流 A/B 可将输入替换为旧 LT。测试 422、HQ 等其余档位时，从新版素材目录取同一文件给 A、B 两组。脚本给出实际结果目录。`bench_topos_playback_path.py` 的 `--threads` 设置原生全局池，`--decoder-workers` 控制每个 context 的请求，实际分配读取输出 JSON。播放器默认组沿用项目策略，同线程的 CPU 对照组另外固定 worker 数和 `--read-ahead 0`。短编码命令仅用于路径冒烟，剖析脚本写出 25 fps 测速文件；正式测速使用同内容且至少 120 帧的 v210 片段，保留六次配对计时。固定 QP 吞吐筛查可用 `--qp`，逐包对比还需钉住矩阵和相同生产输入。`TOPOS_CODEC_PROFILE=1` 仅作阶段归因，其测量单独留档。 [播放基准](../../tools/bench_topos_playback_path.py) · [编码分阶段基准](../../tools/bench_encode_stages.py) · [解码分阶段基准](../../tools/bench_decode_stages.py)

CPU 对照从同源 ProRes 读入 `FFmpegMediaSource.read_frame_planar`，Topos 读入 `ToposMediaSource.read_frame`，计时截止到 CPU YUV 交付。先确认这两个文件来自同一母版、同一段和对应档位。下面以两份至少 108 帧的同源文件为例，`REF` 换成 Mac 上的实际路径，文件名也可换为同一组 LT。该测试用项目各自的默认 CPU 线程策略。

```bash
REF="$MEDIA/乌兰布统_4K_120fps_ProRes422_Proxy.mov"
TOP_MOV="$P4" REF_MOV="$REF" "$PY" - <<'PY' | tee "$OUT/cpu-compare-4k-proxy.json"
import json
import os
from statistics import median
from time import perf_counter
from src.shared.media.ffmpeg_source import FFmpegMediaSource
from src.shared.media.topos_source import ToposMediaSource

def timed(key: str) -> dict:
    path = os.environ[key]
    src = ToposMediaSource(path) if key == "TOP_MOV" else FFmpegMediaSource(path)
    src.open()
    try:
        read = src.read_frame if key == "TOP_MOV" else src.read_frame_planar
        times = []
        for i in range(108):
            start = perf_counter()
            frame = read(i)
            elapsed = perf_counter() - start
            assert frame.frame_index == i and frame.pixel_format == "yuv422p10le"
            assert frame.planes and not (frame.extra or {}).get("topos_concealed_slices")
            del frame
            if i >= 12:
                times.append(elapsed)
        ordered = sorted(times)
        return {"fps": len(times) / sum(times), "p95_ms": 1000 * ordered[91],
                "p99_ms": 1000 * ordered[95]}
    finally:
        src.close()

keys = ("TOP_MOV", "REF_MOV", "REF_MOV", "TOP_MOV")
runs = {key: [] for key in set(keys)}
for key in keys * 3:
    runs[key].append(timed(key))
fps = {key: median(run["fps"] for run in values) for key, values in runs.items()}
print(json.dumps({"runs": runs, "fps": fps, "ratio": fps["REF_MOV"] / fps["TOP_MOV"]}, indent=2))
PY
```

`bench_playback_decode_compare.py` 的 CPU 和 VT 两行分别是 CPU 平面与 PixelBuffer 交付，可用来识别参考路径和量级。用 Resolve 完整导出计时对照产品时，固定同一 v210 输入、帧数、调色和音频条件。仓库 `bench_topos_vs_dnxhr_prores.py` 的对照是 FFmpeg `prores_ks` 与 `dnxhd` 软件编码，可补充 CPU 对照。 [CPU/VT 读取基准](../../tools/bench_playback_decode_compare.py) · [软件编码基准](../../tools/bench_topos_vs_dnxhr_prores.py)

### A4 · 剪辑页和 Metal 实际路由

1. 用 `bash run_topos.command` 启动对应 A/B 源码进程。把两条真实 120 fps 样片导入测试工程，时间线能设 120 fps 时保持源速、原尺寸、关闭自动代理和渲染缓存。时间线只支持较低帧率时，同时记下时间线和素材帧率，用 A1 验证 120 帧交付能力。
2. 分别跑源监视器、单层时间线、双层切点三种场景。固定跳转位置 `0 / 640 / 1700 / 2692`，切点前后各拖一次，暂停再续播并观察末帧。再测一次启用正常调色或叠加的工程，记录实际后端和是否回退。
3. 同一版本保持默认设置跑完再做诊断。需要定位渲染差异时，新进程用 `TOPOS_BACKEND_GRAPHICS=metal bash run_topos.command` 复跑，再按项目的正常后备路径复测。Intel Mac 和黑苹果核对实际使用的 GPU。只有画面交付到 Metal、且实际帧记录能验证 IOSurface，才记录为 Metal 表面路径。
4. 若某组 ProRes/H.264/H.265 的 VT 结果异常，可用 `TOPOS_PLAYBACK_VT_POLICY=force_ffmpeg` 新进程复跑同样操作。保留实际使用的解码器和 fallback 原因。6900 XT 优先用同一接线和刷新率重现，再做休眠恢复、换显示器和切换显示器刷新率。 [现有 Metal 后端和复现](2026-11-23-macos-metal-full-migration-status.md) · [VT 路由策略](../../src/shared/media/macos_vt/hardware_capability.py)
5. 在素材库双击同一批 Topos、ProRes 和 DNxHR 素材，核对第一帧可见，再在暂停状态下拖动进度条、缩放窗口和切换视频。单独记录素材库的出图与刷新率。该播放器使用独立的 OpenGL 画布，macOS 上可能经渲染线程回读后呈现。

### A5–A6 · 逐位核对与画质复测

旧 MOV 解码、固定 QP 编码以及包拷贝的校验可用下列代码。把 A/B 指向同一个旧 MOV 核对像素，把 `MOV` 指向两组固定设置产物或源/直拷出来的 MOV 核对视频包。哈希只读取有效平面样本和视频包有效字节，当前示例为三个各 108 帧的窗口。文件短于 108 帧时核对全部帧。

```bash
MOV="$P4" "$PY" - <<'PY'
import hashlib
import os
import numpy as np
from src.shared.codec.topos_binding import ToposCodec, ToposMovieFile
from src.shared.media.topos_source import ToposMediaSource

path = os.environ["MOV"]
with ToposMovieFile(ToposCodec(), path) as movie:
    info = movie.info()
    print(info)
    for start in (0, 640, 1700):
        end = min(start + 108, info.sample_count)
        if start >= end:
            continue
        packets = hashlib.sha256()
        for i in range(start, end):
            data, size = movie.packet_into(i)
            packets.update(size.to_bytes(8, "little"))
            packets.update(memoryview(data).cast("B")[:size])
        print(start, end, "packets", packets.hexdigest())
with ToposMediaSource(path, read_ahead=0) as src:
    for start in (0, 640, 1700):
        end = min(start + 108, src.total_frames)
        if start >= end:
            continue
        pixels = hashlib.sha256()
        for i in range(start, end):
            frame = src.read_frame(i)
            assert frame.frame_index == i and not (frame.extra or {}).get("topos_concealed_slices")
            for plane in frame.planes:
                pixels.update(np.ascontiguousarray(plane, dtype="<u2").tobytes())
            del frame
        print(start, end, "pixels", pixels.hexdigest())
PY
```

完整验收将窗口替换成全部帧。兼容性编号 A6 应核查重封装后的视频包和音频时间线。封装信息差异会影响 MOV 的文件级 SHA。Alpha 单独核查 A 平面和透明边缘。对普通素材可用 `ffprobe` 检查规格。Topos 自研 FOURCC 的分辨率、位深、矩阵、帧数优先用原生 `ToposMovieFile.info()`。

| 画质复现 | 样本与操作 | Windows 参考与 Mac 验收 |
| --- | --- | --- |
| 1080p LT | Aerial、FoodMarket、1080p 图卡和乌兰布统。新旧同有效码率，再跑默认码控 | 同码率 Aerial 的 VMAF 96.69→97.22，ProRes 99.01。FoodMarket 99.12→99.35，图卡 96.65→97.53。Mac 核对矩阵 0、改善方向、U/V 及局部放大和默认包量 |
| UHD Proxy | Midnight Sun、4K 图卡、乌兰布统，再补北海或圆明园 | 同码率 Midnight Sun 94.766→95.406，ProRes 98.618。Mac 核对矩阵 0 和草木、噪声、文字及色度纹理，保留 1080p Proxy 控制组。两边都与自己的无损母版对齐 |
| UHD HQ 多代 | `svt-midnight-uhd`，3840×2160、24 fps、12 帧，Topos 目标 `682952960` bps。G1 编自母版，G2–G5 每代把上一代解码的 YUV 原样喂给同档编码器。ProRes 用 `svt-midnight-uhd-hq-recode-g02` 至 `g05` 冻结对照，或用 Resolve 经逐位验证过的无损 v210 中间文件复做五代 | Windows G1 两版 Y PSNR 均 53.939 dB，G5 旧 52.021→新 53.925 dB，ProRes 53.739 dB。参考触发为 G2 由 QP71 选到 72。Mac 同时记录首包候选 QP、实际选包、字节和是否接受。常规素材用作不触发控制 |

原测量可复用 Windows 本地的 `assess.py` 和 `prepare.py`，放回两个工作树各自的 `bench_out/quality-parity-20261127`，仅放源与已冻结的 Resolve 对照文件。`assess.py` 跳过已存在的 Topos 输出，开始两组测量前用没有 `topos-*.mov` 和 `score.json` 的案例目录。先确认 Mac 的 FFmpeg 编译了 `libvmaf`。下面的命令使用复制过来的母版、图卡和 Resolve 对照。Windows 脚本内的素材准备和 Resolve 自动化有本地 Windows 路径，Mac 可以用 Resolve 手动从验证过的母版出同一档。HQ 产品验证用 `assess.encode` 的生产编码入口固定上述目标，G2 起传同组上一代 `input_path`。每代检查帧数、顺序和实际视频包量。

```bash
ffmpeg -hide_banner -filters | grep libvmaf
QUALITY="$PWD/bench_out/quality-parity-20261127"
"$PY" "$QUALITY/assess.py" aom-aerial-hd aom-food-hd chart-hd-final --tiers lt
"$PY" "$QUALITY/assess.py" chart-uhd svt-midnight-uhd --tiers proxy

QUALITY="$QUALITY" RESULTS="$OUT" "$PY" - <<'PY' | tee "$OUT/hq-topos-generations.jsonl"
import json
import os
from pathlib import Path
import sys

quality = Path(os.environ["QUALITY"])
sys.path.insert(0, str(quality))
from assess import ToposCodec, encode, payload, score

case = quality / "cases" / "svt-midnight-uhd"
meta = json.loads((case / "case.json").read_text(encoding="utf-8"))
previous = None
codec = ToposCodec()
for generation in range(1, 6):
    label = f"hq-generation-{generation}"
    movie = Path(os.environ["RESULTS"]) / (label + ".mov")
    if movie.exists():
        raise FileExistsError(movie)
    encode(case, meta, "hq", movie, bitrate=682952960, input_path=previous)
    byte_count, frames = payload(movie, codec)
    assert frames == meta["frames"]
    metrics = score(case, meta, movie, label, True, False)
    print(json.dumps({"generation": generation, "payload_bytes": byte_count,
                      "frames": frames, "metrics": metrics}, allow_nan=False))
    previous = movie
PY
```

每一档保存默认和 ±2% 视频包量对照。计算 PSNR Y/U/V、PSNR Y 的 P5、局部 PSNR、MS-SSIM 和 CAMBI。保持与 Windows 一样的 VMAF 模型及抽样，1080p 用 `vmaf_v0.6.1neg`，UHD 用 `vmaf_4k_v0.6.1`，每两帧取一帧。放大同一位置检查梯度带状纹、细纹、噪声、跃动和色边。对齐后按档与 ProRes 做盲看，保存明显优劣和对应帧号。本轮给「初步同档画质」设置工作门槛，所有自然留出样本的 VMAF 差 ≤1、Y/U/V PSNR 分别落后 ≤0.5 dB，图卡和盲看无稳定可辨的系统性缺陷，才把该档标记为接近。留出样本和对照码率未齐时保留分数，结论待测。 [Apple ProRes 档位与多代工作流](https://www.apple.com/final-cut-pro/docs/Apple_ProRes.pdf)

### A8 · 素材库播放器

1. 用素材库原生播放器打开北海公园 UHD25 的 ProRes HQ/4444 XQ、DNxHR HQX/444 10/12-bit，再用圆明园 02 的 UHD29.97 ProRes HQ 与 DNxHR HQX 对照。另取乌兰布统的 Topos Proxy/LT 和 ProRes 422 作高帧率控制。先用约 1920×1080 设备像素的预览窗口，再测最大窗口，记录实际画布像素、DPR 和显示刷新率。每组先跑冷启动，再预热 1 秒，取至少两个 6.5 秒稳态窗口。
2. 分别记录首帧、demux/解码、CPU 重采样和拷贝、GL 发布、Qt 呈现帧号、P95/P99 间隔与持续墙钟落后。测试暂停拖动、恢复、缩放和连续换片，核对有效画面。120 fps 控制组再记显示器实际刷新率和可见呈现，避免从 Qt 发布次数推断物理屏扫描。
3. DNxHR 444 或 ProRes XQ 在最大窗口不稳时，用原尺寸平面解码与同画布尺寸的 CPU 缩放对照定位。先记录产品默认，再单独试复用 `VideoReformatter`、把放大交给 GPU、DNxHR 的 FRAME/SLICE 与线程数。复核尺寸、色彩范围、Alpha、帧像素及拖动后的正确性。

Windows i9-13900K 实测 UHD 25/29.97 fps 的 ProRes 和 DNxHR 无帧序缺口。UHD120 ProRes 约 77–80 fps 解码、59–60 fps Qt 呈现，虚拟显示环境的数值用于比对应用吞吐。已有 Intel i9-12900T / RX 6800 的 Mac 基线里，UHD DNxHR 444 大窗口约 18 fps，ProRes XQ 约 18 fps。诊断中 DNxHR 444 缩放阶段约 24.2 ms，复用重采样器后约 2.85 ms。对应机器、代码版本和窗口口径均需随测量存档。 [Windows 素材库实测](../../bench_out/diag-app-codec-20261127/library-mezzanine-play.jsonl) · [Mac 回放基线与脚本](../../artifacts/library_playback_20261123/report.txt)

### A9 · 图片序列交付回读

1. 复制 A9 短样到本地 SSD。媒体池一次选择同档全部 12 张，检查成组为一个 `image_sequence`，且能读到第一、六、十二帧。记录批量导入时间、开源/首次读帧耗时、序列帧率与每帧编号。随后在默认 SDR、无效果的单层时间线导出 422 和 FP16 序列，再把新序列导入回读。
2. 核对 12 张有效平面的哈希、黑电平和动态范围。逐包拷贝要求对应帧的每个平面一致。重编码使用同配置编码结果做对应帧对照，因为量化会改变像素。FP16 另加含负值、大于 1.0 与细梯度的线性样片，验收 `float16` 样本类型和线性标签。匹配 MOV 目前会返回 `gbrp16le` 整数码值，使用 `half_codes_to_float16` 还原后核对同一码流的逐位输出。
3. RAW12 LOG0 和 RAW16 linear 分别从 CFA MOV、CFA `.toos` 重导到两种容器。记录四相位 R/Gr/Gb/B、位深、传递函数、黑白电平元数据、每平面几何和输出帧号。全尺寸可核对两种容器的 CFA 平面。缩小读取要分别记录原生 reduced 与整帧解码后 box 缩小。再加普通 RGB 输入和缺帧控制组，检查具体错误及输出目录是否被正确回滚。
4. 在工作树和安装版各跑一次。自动化覆盖以下四组，实际时间线的导出成功信号还需逐帧回读佐证。

```bash
"$PY" -m pytest -ra tests/media/test_topos_image_sequence_export.py \
  tests/media/test_topos_image_sequence.py tests/media/test_topos_half_float.py \
  tests/media/test_topos_raw_direct.py > "$OUT/image-sequence-tests.log" 2>&1
```

Windows 已复现普通 SDR 时间线重导 422/FP16 两组均报告成功，12 张输出实际全黑。交付取帧中 `image_sequence` 当前只进入 ACES/HDR 的相关分支，默认 SDR 记录 `Unsupported asset type`。RAW 序列在第一帧收到 packed RGB 后失败，RAW MOV 的匹配直通已成功。两项按平台独立复测。完整矩阵的 `new-1` 放到同目录后可运行 `workflow.py` 复现实测，输出写到同级全新的 `timeline/`。 [逐帧回读](../../bench_out/diag-topos-image-20261128/workflow-verified.json) · [工作流事件](../../bench_out/diag-topos-image-20261128/workflow.jsonl) · [交付取帧](../../src/features/edit/core/timeline_export.py)

### A10 · 图片序列和 MOV 的同参数基准

准备一条 3840×2160 乌兰布统母版，保留源文件 SHA。固定 8 个切片 worker、片高、QP、量化矩阵和源帧，按 A/B 各自实际加载的 dylib 比较。复制 `matrix.py` 到两组工作树内的 `bench_out/diag-topos-image-20261128/`，在两组 A0 会话中使用同一个绝对路径结果根目录。先用冻结的 Windows `new-1` 做读解码参考，每组 12 帧，以便 A/B 输入字节一致。下面在 B 会话执行，在 A 会话把 `GROUP` 设为 A；A 用 `old`，B 用 `new`。每次改 `ROUND`，交错运行，保持各组输出目录唯一。

```bash
IMAGE_TOOL="$PWD/bench_out/diag-topos-image-20261128/matrix.py"
IMAGE_OUT="$HOME/topos-mac-validation/image-matrix"
IMAGE_REF="$PWD/bench_out/diag-topos-image-20261128/new-1"
IMAGE_SOURCE="$MEDIA/乌兰布统_4K_120fps_ProRes422.mov"
ROUND=1
BUILD_LABEL=new
if [ "$GROUP" = A ]; then BUILD_LABEL=old; fi
mkdir -p "$IMAGE_OUT"
"$PY" "$IMAGE_TOOL" "$BUILD_LABEL" --source "$IMAGE_SOURCE" --output-root "$IMAGE_OUT" \
  --round "$ROUND" --frames 12 --decode-reference "$IMAGE_REF" \
  --only 422-low 444-high 422-int12 444-int12-xq float-ultra raw12-4 raw16-4 \
  | tee "$IMAGE_OUT/$BUILD_LABEL-$ROUND.jsonl"
```

首轮七档通过后，去掉 `--only` 跑完整 24 项。正式性能验收另做长窗。长窗的共同读源可先由 B 生成，再把生成的 `new-*/` 作为两组的 `--decode-reference`，两边帧数相同。只测固定输入的编码和写盘时可用 `--encode-only`。每轮检查 JSONL 中逐档的 `error` 和实际加载库 SHA。规格在旧版缺失时标记不适用。

分开记录原生包编码、逐文件写入、定稿、导入分组、全尺寸和 1080p 目标读帧。产品路径再以同一段时间线计时到定稿，明确平面直通、RGB 渲染和缩放后端。对同配置输出比对图片 `PIXL` 和 MOV 帧包，RAW 的像素宽高比及头部 CRC 可因容器语义而不同，单独核对包体与全尺寸 CFA 平面。按默认设置保留一轮预览以观察真实缓存收益，再对照关闭已解码平面缓存和预取的顺读吞吐。

Windows 24 项固定配置的新版原生编码约提升 5–36%，同配置 `.toos` 与 MOV 的核心速率相差约 0–8%。4K 422 Low 的封装级导出约 63/110 fps，时间线完整交付约 3/80 fps。四档 FP16 的原尺寸读帧约 10–16 fps；两种 RAW 4:1 的 1080p 目标读取，图片约 71–77 fps，MOV 约 10 fps，使用不同缩小路径。以上为定位参照，Mac 以本机多轮数据定目标。 [完整 24 项数据](../../bench_out/diag-topos-image-20261128/summary.json)

## 6. 根据 Mac 实测推进的优化排期

| 优先级 | 出现什么数据就启动 | 要做的事 | 完成条件 |
| --- | --- | --- | --- |
| P0 · 先解决版本 | 应用实际库、架构或 commit 与实验不一致，或包内库缺失/陈旧 | 对齐打包和加载的 dylib 名称、搜索顺序和架构校验，CI 加原生 arm64/x86_64 的包内探测 | 两类 Mac 安装版和黑苹果兼容组都能正常加载目标库、导入并回读。日志显示实际选库 |
| P0 · 图片交付正确性 | A9 出现全黑成功、帧序错误或 RAW 首帧失败 | 接通 SDR 序列取帧，定义同位深 CFA 输入的直通交付，保持 RGB→RAW 明确拒绝。对取消、缺帧和不可用输入保留明确失败状态 | 422/FP16 连号导出逐帧回读，RAW 保留域与相位，普通 RGB 和缺帧控制组得到预期诊断 |
| P1 · ARM 解码 | M 系列 Proxy/LT 落后且原生分阶段数据指向去块或稀疏逆变换 | 先用单线程取热点。若去块占比较高，移植水平和垂直去块到 NEON；若单 AC 块占比较高，再做 NEON 融合反量化与写入。每次只改一个热段 | Scalar/NEON、旧/新、原尺寸/边界三类同输入像素哈希一致。目标低档中位数提升 ≥5%，P95/P99 和多流都通过，再考虑默认启用 |
| P1 · Mac 播放调度 | 原生吞吐充裕但播放掉帧、seek 慢，或低档 1080p 单流有明显调度等待 | 逐台测 1、4、8、12、16（受本机可用核心约束）和预读 0/2/4，先排查 GUI/上传等待，再测 IOSurface 池占用。观察两路播放和后台导出争用 | 只有改善单流且多流/首帧/seek 尾延迟可接受、RSS 无持续增长的组合进入默认候选；M 系列和 Intel 策略可独立定标 |
| P1 · 素材库大窗 | A8 原始读帧能跟上，重采样或呈现 P95/P99 仍超帧预算 | 分别验证持久重采样器、GPU 放大和 DNxHR 线程策略。保留真实 GL 发布、Qt 呈现和切换素材的计数 | 两种窗口尺寸下达到各自源帧率，画质、首帧、拖动和长时间内存一并通过 |
| P1 · 图片吞吐 | A9 通过，A10 核心已接近但图片应用仍落后 | 对单一同格式剪辑量测平面直通收益，剖析逐帧重复探测、封装写入、目录提交以及 HALF 还原。每次只改变已定位的主要瓶颈 | 保留逐帧一致性、封装兼容和失败回滚，报告核心与完整交付各自的速度变化 |
| P1 · 低档画质 | Proxy 在同码率持续落后，或 1080p LT 仍有内容相关差距 | 锁定解码像素和格式兼容性，优先试编码端 RANS2 量化决策；保留大纹理、低纹理、高色度和留出运动样本。用 Windows 与 Mac 同一批素材作匹配复验 | 默认与同码率都记录。常规档不倒退，低档最差素材有改善，解码无回退，便于维持低档 ≤1.5× 目标 |
| P2 · 编码热点 | 固定配置长窗证实编码耗时回退，或 ARM 耗时集中在 CRC、变换、量化/熵编码 | 阶段剖析后再做 ARM 的 IEEE CRC32 或量化/熵编码热段优化。若只发生在 HQ 首帧，单独检查额外试编码和命中频率 | 固定码流逐包一致、解码 CRC/边界通过、六轮持续编码有效改善；默认画质组单独验收 |
| P2 · Intel 与 6900 XT 兼容 | 只在 x86 macOS 复现 AVX2 分发异常、Metal/VT 回退或花帧 | 先定位 CPU 解码还是 GPU 交付。复跑 AVX2 与通用 CPU 路径，对 Metal 分别检查 GPU 选择、表面格式、音视频同步、热机与休眠恢复。保留每种平台的最短复现输入 | CPU 与 GPU 分支能解释回退，正常情况有码流/像素/交付证据，硬件不支持或不可用时进入可用的已知回退路径 |

### 每台机器复制一份结果记录

| 机器 / 编号 | 素材、尺寸、帧率 / 档位 | A / B 源快照及库哈希 | 后端、worker / 预读 | A fps / B fps / 参考 fps | A/B P95 和 P99、掉帧、RSS | Y/U/V、VMAF、实际包量比或像素/包 SHA | 通过 / 待查 / 不适用，证据路径 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| | | | | | | | |
| | | | | | | | |
| | | | | | | | |

| A8–A10 补测场景 | 样本域、尺寸与帧数 | 耗时与速率 | 正确性与定位 | 证据路径 |
| --- | --- | --- | --- | --- |
| 素材库普通窗 / 最大窗 | 显示刷新率、DPR、画布设备像素、源帧率 | 解码、缩放、Qt 呈现、首帧与 P95/P99、墙钟落后 | 载荷切换、黑帧、拖动、续播、后端 | |
| 图片序列导入 / 重导 | 位深、样本域、帧数、缩放方式 | 分组、首次读帧、顺读、原生编码、封装定稿、时间线总耗时 | 帧号、非黑/不同帧数、包与平面哈希、失败原因 | |

Windows 参考值为 i9-13900K 上同窗口 CPU 平面交付。最后一轮 UHD Proxy 的 ProRes/Topos 为 1.14×，1080p Proxy 为 1.21×。UHD LT 为约 0.90×，1080p LT 约 1.19×。这些数值作为 Windows 锚点。macOS 需填写**同机 A/B**和**同机 ProRes 对照**，才可确认当前优化实益及剩余差距。
