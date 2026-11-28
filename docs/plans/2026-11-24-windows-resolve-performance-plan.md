# Topos Studio Windows 性能优化计划

2026-11-24 · 范围为 Windows 上的剪辑播放、调色和交付，以及这些路径依赖的媒体解码、编码和封装。合成与插件的大规模迁移另行实施。

## 1. 当前状态与目标

macOS 参考来自同一台 Mac、同一工程的 4K、400 帧、三节点串行 LUT 导出。Metal 的中位耗时比 OpenGL 少 **9.34 秒（46.2%）**。Windows 从同一台 Windows 电脑上的 Topos、DaVinci Resolve 和 FFmpeg 建立新基线。

| macOS 渲染路径 | 第 1 次 s | 第 2 次 s | 第 3 次 s | 中位数 s |
|---|---:|---:|---:|---:|
| OpenGL | 20.210 | 20.830 | 19.924 | 20.210 |
| Metal | 10.869 | 11.257 | 9.818 | 10.869 |

| 里程碑 | 验收条件 |
|---|---|
| Windows 基线 | H.264、DNxHR、TOPOS 422 各有独立记录。Topos 和 Resolve 有同素材、同时间线、同规格的原始样本和可回解输出。 |
| 首个交付目标 | 优先让 H.264 → DNxHR HQX 10-bit 的纯剪与单 LUT 导出分别达到 **Topos 耗时 ≤ Resolve 的 1.5 倍**。达标后以同样口径推进 H.264 → H.264 和 DNxHR → DNxHR。 |
| 小步优化 | 每轮只动一个瓶颈。400 帧配对复测能稳定节省 **0.10 秒**即记为一次收益。接近时继续向 1.0 倍推进。 |
| 剪辑播放 | Full 分辨率下按 25 fps 的 40 ms 帧预算分析掉帧和长帧。分别记录原片、单 LUT、切点和随机跳转。 |
| TOPOS 422 | 单独建立完整重编码、加 LUT 重编码和视频包复用三条基线。TOPOS 的内部目标是减少同规格重编码耗时，并验证绕过重编码逐包保真。 |

## 2. Windows 首次准备

### 2.1 固定机器、版本和素材

在 Windows 上打开 PowerShell，进入仓库根目录。使用 x64 Python 和仓库现有安装方式，并将 FFmpeg 与 FFprobe 加入 PATH。保留平常使用的独显、供电模式和驱动版本。跑分期间一次只启动一个编辑器的播放或渲染，关闭录屏、同步和其它重负载。把素材及输出放在有足够空间的同一块本地 SSD，记录磁盘型号。混合显卡机器记录**实际**承载解码、渲染和编码的适配器，两个应用选同一块主 GPU。以下每次创建新的工作目录以保留原始数据。 [FFmpeg 下载入口](https://ffmpeg.org/download.html)

```powershell
$ErrorActionPreference = 'Stop'
$Tag = 'win-baseline-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
$Run = New-Item -ItemType Directory -Path "bench_out/$Tag"
$Out = $Run.FullName
$Media = Join-Path (Get-Location).Path 'tests/test_videos'
$History = 'bench_out/resolve_vs_topos_2026-11-22'

$Overrides = @(
  'TOPOS_BACKEND_GRAPHICS', 'TOPOS_EXPORT_GPU_RENDER',
  'TOPOS_FORCE_WIN_HW_DECODE', 'TOPOS_DISABLE_WIN_HW_DECODE',
  'TOPOS_DISABLE_WIN_HW_ENCODE', 'TOPOS_WIN_HW_RESIDENT_DECODE',
  'TOPOS_EXPORT_DIRECT_PIPELINE', 'TOPOS_DECODE_WORKERS',
  'TOPOS_CODEC_PROFILE', 'TOPOS_CODEC_LIB', 'TOPOS_CONFIG_PATH',
  'TOPOS_EDIT_PLAYBACK_TRACE', 'QT_QPA_PLATFORM'
)
foreach ($Name in $Overrides) {
  Remove-Item "Env:$Name" -ErrorAction SilentlyContinue
}
$env:TOPOS_CONFIG_PATH = Join-Path $Out 'topos_runtime/config.json'

if (-not (Test-Path '.venv/Scripts/python.exe')) {
  py -3 -m venv .venv
  if ($LASTEXITCODE -ne 0) { throw 'Python virtual environment failed' }
}
$Py = (Resolve-Path '.venv/Scripts/python.exe').Path
& $Py -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Project dependencies failed to install' }
& $Py -m pip install -e '.[dev]'
if ($LASTEXITCODE -ne 0) { throw 'Editable installation failed' }
& $Py -c "import platform, struct, av; print(platform.machine(), struct.calcsize('P') * 8, av.__version__, av.library_versions)" |
  Set-Content "$Out/python-pyav.txt"
& $Py -m pip freeze | Set-Content "$Out/pip-freeze.txt"
Get-Command ffmpeg, ffprobe | Select-Object Name, Source | Format-Table |
  Out-String | Set-Content "$Out/ffmpeg-path.txt"
ffmpeg -version | Set-Content "$Out/ffmpeg-version.txt"
ffprobe -version | Set-Content "$Out/ffprobe-version.txt"
git rev-parse HEAD | Set-Content "$Out/commit.txt"
git status --short --branch | Set-Content "$Out/git-status.txt"
git diff --binary HEAD --output="$Out/worktree.patch"
Get-ComputerInfo -Property WindowsProductName, WindowsVersion, OsBuildNumber |
  Format-List | Out-String | Set-Content "$Out/windows.txt"
Get-CimInstance Win32_Processor | Select-Object Name, NumberOfCores, NumberOfLogicalProcessors |
  Format-List | Out-String | Set-Content "$Out/cpu.txt"
Get-CimInstance Win32_VideoController | Select-Object Name, DriverVersion, PNPDeviceID |
  Format-List | Out-String | Set-Content "$Out/gpu.txt"
powercfg /GETACTIVESCHEME | Set-Content "$Out/power.txt"

$Files = @('4K_H264.mov', '4K_DNxHR HQX 10bit.mov', '4K_topos_422.mov', '4K_H265.mov')
foreach ($File in $Files) {
  $Path = Join-Path $Media $File
  if (-not (Test-Path -LiteralPath $Path)) { throw "Missing benchmark media: $Path" }
  Get-FileHash -Algorithm SHA256 -LiteralPath $Path |
    Select-Object @{Name='File';Expression={$File}}, Hash |
    Export-Csv "$Out/media-sha256.csv" -Append -NoTypeInformation
}
```

`4K_H265.mov` 用于后续 Main10 回归。如果 Windows 一开始只有 H.264 和 DNxHR，可先在 `$Files` 留下这两项。视频大文件需从已有素材库或原测试机搬到 `tests/test_videos`，补测前记录 SHA-256。历史同规格基准使用 3840×2160、25 fps、Rec.709，实际属性以当前探测报告为准。另记 Resolve 的版本和 Free／Studio、GPU 模式、实际编码器选项、预览显示器刷新率和色彩管理设置。 [安装说明](../getstarted/installation.md) · [素材说明](../../tests/test_videos/README.md) · [素材指纹](../../tests/test_videos/benchmark_media_manifest.json)

### 2.2 确认 TOPOS 原生库

H.264 和 DNxHR 可先跑。如果 Windows 已有与该源码兼容的 TOPOS x64 Release DLL，可直接指定其绝对路径。需要本机编译时，在已安装 CMake 和 Visual Studio 2022 C++ 桌面开发工具的 PowerShell 中执行。

```powershell
$BuildDir = 'native/topos_codec/build/win-perf'
cmake -S native/topos_codec -B $BuildDir -G 'Visual Studio 17 2022' -A x64 -DTOPOS_ENABLE_TESTS=ON
if ($LASTEXITCODE -ne 0) { throw 'CMake configuration failed' }
cmake --build $BuildDir --config Release --parallel
if ($LASTEXITCODE -ne 0) { throw 'Release build failed' }
ctest --test-dir $BuildDir -C Release --output-on-failure
if ($LASTEXITCODE -ne 0) { throw 'Native tests failed' }

$Dlls = @(Get-ChildItem $BuildDir -Recurse -Filter 'topos_codec.dll' -File |
  Where-Object { $_.Directory.Name -eq 'Release' })
if ($Dlls.Count -ne 1) { throw 'Set TOPOS_CODEC_LIB to the correct x64 Release DLL' }
$env:TOPOS_CODEC_LIB = $Dlls[0].FullName
Get-FileHash -Algorithm SHA256 -LiteralPath $env:TOPOS_CODEC_LIB |
  Format-List | Out-String | Set-Content "$Out/topos-dll.txt"
& $Py -c "from dataclasses import asdict; from src.shared.codec.topos_binding import ToposCodec; c=ToposCodec(); print(c._lib._name); print(asdict(c.version()))" |
  Add-Content "$Out/topos-dll.txt"
if ($LASTEXITCODE -ne 0) { throw 'TOPOS DLL failed to load' }
```

记录 DLL 哈希和 ABI 后，再在 Topos 查看 `4K_topos_422.mov` 的实际子档位。完整重编码和视频包复用两项均使用匹配的 422 10-bit Standard／LT／HQ 子档位。加载报缺少符号时，用同一源码重建 Release DLL，再检查实际加载路径。普通 FFmpeg 可能将 TOPOS 视频标成未知编解码器，使用 Topos 原生读取器验收。 [原生工程](../../native/topos_codec/CMakeLists.txt) · [交付路径](../architecture/deliver-export.md#33-topos-直通交付t-p1-10-快路径)

## 3. 建出同样的 400 帧时间线

1. 复制下方现有的 Topos 脚本、LUT 和 Resolve 工程到当前实验目录。脚本从仓库根目录的 `bench_out/<本轮目录>` 寻找素材，会向自己的目录写工程、输出和 JSONL。Topos 脚本的 `build` 会创建四条时间线并设置各片段的 LUT。
2. Resolve 从项目管理器导入 `Resolve_UHD25_AB.drp`，或新建同规格临时项目。将离线媒体重新链接到本地原片，在 Color Management 的 LUT 文件夹打开入口导入同一份 `.cube`。检查媒体链接和两段片段各自的节点。现有 Resolve `setup` 脚本含 macOS LUT 默认路径，Windows 首轮走界面操作。
3. 双方时间线均为 UHD 3840×2160、25 fps、单视频轨、1×速度、100% Full 预览、总长 16 秒。源帧区间按零基帧号且右端排除，依次取 **[25, 225)** 和 **[275, 475)**。两段在时间线上从 0 和 200 开始；检查第 199 和 200 帧正好位于切点两侧。自由添加其他素材会改变基线，转场、缩放、双轨后续另建场景。
4. 关闭代理、优化媒体、渲染缓存和动态预览降级。Resolve Deliver 关闭使用代理、优化媒体、缓存图像和绕过重编码的选项（若当前版本的面板提供）。Topos 高级设置同样关闭绕过。复制原片时间线生成 LUT 版本，在每个片段挂一枚同 SHA 的 33 点 LUT，强度 100%。用 SDR Rec.709、匹配的数据级别及输出标签，确认 LUT 插入位置、转换顺序和插值方式。首轮关闭音频、字幕、片头尾和输出缩放。界面名称可随 Resolve 版本变化，保存设置截图并记录实际选项。 [Resolve 用户手册](https://documents.blackmagicdesign.com/UserManuals/DaVinciResolveReferenceManual.pdf?_v=1658361162000) · [Resolve 20 调色指南](https://documents.blackmagicdesign.com/UserManuals/DaVinci-Resolve-20-Colorist-Guide.pdf?_v=1757574010000)
5. 在 Topos 和 Resolve 各做一次编辑检查，拖动播放头到切点前后，检查跳帧、源片段接续和 LUT。TOPOS 422 用相同切法另建 Topos 时间线。再从纯剪时间线复制一条场景专用于绕过重编码，在 Topos 交付页启用「高级设置 → 尽可能绕过重新编码」，注明源档位、码流版本和选项状态。
6. 在交付页选择整条时间线，设置单文件输出和下表的规格，目标目录选本轮目录，文件名含程序、场景和轮次。Resolve 加入渲染队列后逐条渲染。Topos 同样逐条提交。看到作业完成后立即按第 4 节检查输出，再进行下一轮。

```powershell
Copy-Item -LiteralPath @(
  "$History/topos_bench.py",
  "$History/Topos_Resolve_AB_33.cube",
  "$History/Resolve_UHD25_AB.drp"
) -Destination $Out
Get-FileHash "$Out/Topos_Resolve_AB_33.cube" -Algorithm SHA256 |
  Format-List | Out-String | Set-Content "$Out/lut-sha256.txt"
$Bench = Join-Path $Out 'topos_bench.py'
& $Py $Bench build
if ($LASTEXITCODE -ne 0) { throw 'Topos benchmark project could not be built' }

# 一次检查真实 Viewer 是否顺利运行，四条时间线各一遍
& $Py $Bench playback --repeat 1

# 独立 Topos 作业只测 worker 启动到完成，编号用于唯一文件名
& $Py $Bench export --source h264 --codec dnxhr --repeat 1
& $Py $Bench export --source h264 --codec dnxhr --graded --repeat 1
```

如果现有脚本无法在本机打开 Viewer 或 OpenGL 上下文，用 `& $Py -m apps.topos_studio.main` 启动正常程序，打开本轮 `.topos` 工程后用界面做同规格测试，记录脚本失败的路径和原因。正式跨应用总耗时用双方同一计时边界。Topos worker 日志用于后续精确配对。

### 对照矩阵

| 优先级 | 场景 | Topos 与 Resolve 所需设置 | 记录 |
|---|---|---|---|
| 1 | H.264 播放，原片／单 LUT | UHD25，两个 200 帧切段 | 首帧、切点、掉帧、呈现帧间隔、跳转响应 |
| 1 | H.264 → DNxHR HQX，原片／单 LUT | MOV，HQX 10-bit、4:2:2，尽可能确认与源同范围标签 | 交付耗时、回解、阶段遥测；另跑 FFmpeg 原片参考 |
| 1 | DNxHR HQX 播放，原片／单 LUT | 同一份 10-bit 源，UHD25 | 同播放字段 |
| 1 | H.264 → H.264，原片／单 LUT | MP4、High、8-bit 4:2:0、50 Mbps VBR、单遍。相同编码模式配对，记录实际编码器名称、预设、码率和 GOP。 | 软件与硬件分别记录，比较前验证实际体积、画质和输出设置 |
| 2 | DNxHR HQX → DNxHR HQX，原片／单 LUT | MOV、同规格。符合条件的 Topos 纯剪会自动尝试视频包复用，按真实路径填表。 | 单 LUT 用于必经重编码对照，纯剪包复用单列 |
| 2 | TOPOS 422 → TOPOS 422 | Topos 本机，同子档位。分开跑纯剪重编码、单 LUT、纯剪包复用 | 回解、保真、路径名称和本项目提速 |
| 3 | 三 LUT、双轨、转场、音频、长片、4K HEVC Main10 | 每个变量单独设场景，分别记录同等设置 | 首轮矩阵稳定后扩展 |

两边如无法选择同一种 H.264 编码模式，将原始结果标为各自默认生产配置，保留用户体验数据并单列归因比较。硬编可用性由显卡、驱动、输入格式、应用版本和实际工厂选择决定。在 Topos 和 Resolve 都能启用同规格包复用时，可追加 DNxHR → DNxHR 的同编码快路径对照。TOPOS 422 的项目内三条数据独立统计。需要定位 TOPOS 同规格重编码时，追加一条源帧 `[25,425)` 的连续单段场景，以覆盖现有单段 planar 交付快路径。首轮同规格测试使用 H.264 与 DNxHR。

## 4. 统一计时、回解和记录

### 导出与播放

- 每个正式场景先做一轮功能检查和一次同规格预热，把冷启动另记。发现错误后重新预热。正式首轮至少三对，确定收益再取至少五对，轮换 Topos→Resolve、Resolve→Topos 的先后次序。任何暂停、中断、磁盘耗尽或参数改动均保留原始行并标出原因。
- GUI 记点击开始到作业成功且最终文件已经发布的墙钟时间。脚本计时比较用同一进程边界内的同一时钟；跨 App 使用各自 API 时记录轮询周期和量化误差。历史 Resolve 脚本以 150 ms 轮询，小于或接近这个幅度的差距用 Topos 同入口自动配对测量。图形录屏、WPR、调试日志另跑。记录首次初始化、稳态处理、编码 flush 和发布开销的归属。
- 播放从同一帧开始，保持窗口布局、预览比例、显示器和掉帧／音频同步策略固定。保存 Topos 的实际 Viewer `frame_presented` 事件、帧号、时间戳。Resolve 播放 FPS 或播放头时间码可记录为单独指标。要直接比较两边实际呈现，制作一份双方共用的帧编号测试样本，用外部录屏设备或高帧率摄像统一数显示帧与重复帧，单独标注这组样本。无法可靠读出实际呈现时，只填写观测到的指标。
- 把起播、稳态、切点和拖动后第一帧分开。稳态记录呈现数量、丢失帧号、连续不可见帧最长长度、呈现间隔 P95/P99；拖动使用固定帧号序列，至少 20 次，记录事件到目标帧实际出现的 P50/P95。25 fps 下 40 ms 是目标帧预算，观察显示器刷新周期量化和偶发长尾。只改负载分辨率时单列结果。

### 检查每个交付文件

对 H.264 与 DNxHR 文件实际回解并核对 400 帧。检查 3840×2160、25/1、约 16 秒、像素格式、编码 profile、Rec.709 色彩标签、范围、码率、无音轨和时间戳。至少对照 0、25、199、200、399 帧及高光、暗部、肤色和色彩梯度，确认切点、LUT 层次、限幅和色调。编码、GPU 后端或精度改变时，以同范围、同色彩转换下的解码帧和既定容差验证画质，保存测试图和差异。只改调度或缓存且同编码器设置可复现时，检查原有输出的帧值是否一致。比较 Resolve 与 Topos 的重编码交付时，记录压缩质量差异并独立解释。

```powershell
$File = Join-Path $Out 'Topos_h264_Original_to_dnxhr_1.mov'
ffprobe -v error -count_frames -show_streams -show_format -of json "$File" |
  Set-Content "$File.ffprobe.json" -Encoding utf8
if ($LASTEXITCODE -ne 0) { throw 'ffprobe failed' }
$Probe = Get-Content "$File.ffprobe.json" -Raw | ConvertFrom-Json
$Video = @($Probe.streams | Where-Object { $_.codec_type -eq 'video' })
$Audio = @($Probe.streams | Where-Object { $_.codec_type -eq 'audio' })
if ($Video.Count -ne 1 -or $Audio.Count -ne 0 -or
    $Video[0].nb_read_frames -ne '400' -or $Video[0].width -ne 3840 -or
    $Video[0].height -ne 2160 -or $Video[0].avg_frame_rate -ne '25/1' -or
    $Video[0].profile -ne 'DNxHR HQX' -or $Video[0].pix_fmt -ne 'yuv422p10le') {
  throw "Unexpected UHD25 DNxHR HQX output: $File"
}
ffmpeg -v error -xerror -i "$File" -map 0:v:0 -f null -
if ($LASTEXITCODE -ne 0) { throw 'Output failed to decode' }
```

同规格 **TOPOS 视频包复用**需要显示 `delivery_path=topos_packet_copy`，且匹配每一帧的 payload。把下例保存为本轮目录下的 `verify-topos-copy.py`，从仓库根目录运行。时间戳和选区回解另查，音频回归另开场景。DNxHR 包复用可用 FFprobe 的 `-show_packets -show_data_hash sha256` 验证相同选区的包体，条件满足时预期遥测为 `dnxhr_packet_copy`。 [FFprobe 选项](https://www.ffmpeg.org/ffprobe.html) · [原生 TOPOS 读接口](../../src/shared/codec/topos_binding.py)

```python
import ctypes
import hashlib
import sys

from src.shared.codec.topos_binding import ToposCodec, ToposMovieFile


def packet_digest(movie: ToposMovieFile, index: int) -> bytes:
    buffer, size = movie.packet_into(index)
    # 立即消费。下一次访问同一对象会覆盖其复用缓冲。
    return hashlib.sha256(ctypes.string_at(buffer, size)).digest()


codec = ToposCodec()
indices = [*range(25, 225), *range(275, 475)]
with ToposMovieFile(codec, sys.argv[1]) as source, ToposMovieFile(codec, sys.argv[2]) as dest:
    if dest.info().sample_count != len(indices):
        raise SystemExit("Unexpected TOPOS output frame count")
    for new, old in enumerate(indices):
        if packet_digest(source, old) != packet_digest(dest, new):
            raise SystemExit(f"Different compressed payload: output {new}, source {old}")
print("Verified 400 TOPOS packets in timeline order")
```

```powershell
& $Py "$Out/verify-topos-copy.py" "$Media/4K_topos_422.mov" "$Out/Topos_422_Copy.mov"
```

### 汇总规则与表格

`差距倍数 = Topos 正式有效样本中位秒数 ÷ Resolve 正式有效样本中位秒数`。表中填中位数和范围，保留每轮原值、计时边界、输出和日志。Topos 优化前后用配对 `delta = 旧耗时 - 新耗时`。若要认定稳定节省 0.10 秒，至少五对有效记录、节省中位数 ≥0.10 秒、至少四对同向，并且收益大于同版本重复配对测出的抖动中位数。噪声接近收益时追加配对或放大到 4,000 帧确认，上报长短两个场景的原始结果。

| 日志行 | 场景／路径 | 程序／版本／顺序 | 第几次 | 耗时 s | 出帧／规格 | 呈现 P95 或阶段证据 | 证据文件／异常 |
|---|---|---|---|---:|---|---|---|
| 例 A01 | H.264 → DNxHR，单 LUT | Topos／SHA／先跑 | 1 | 待测 | 待验 | 待测 | JSONL、ffprobe、设置截图 |
| 例 A02 | H.264 → DNxHR，单 LUT | Resolve／版本／后跑 | 1 | 待测 | 待验 | 待测 | 项目、设置截图、输出探测 |
| 例 B01 | TOPOS 422 包复用 | Topos／SHA | 1 | 待测 | 待验 | 待测 | 遥测、逐包 SHA-256 |

| 迭代 | 当前瓶颈和证据 | 唯一变量 | 旧 s／新 s／节省 s | Resolve s／比值 | 画质与稳定性 | 暂存／合入／撤回 |
|---|---|---|---|---|---|---|
| W-001 | 待首轮基线 | 待定 | 待测 | 待测 | 待验 | 待定 |

## 5. 用 FFmpeg 和阶段遥测定位差距

H.264 → DNxHR 首轮加一条 FFmpeg 软件参考，读取完全相同的两段源帧。保留 FFmpeg 版本和 `-benchmark` 的 `rtime`、`utime`、`stime`。剪选命令从文件开始顺序解码到第 474 帧，中间仍会解码未交付帧。它还用单进程 filter graph 重打时间戳，因此这组结果用作编解码及转换量级参照。归因时再做下方连续 400 帧微基准，避免把搜索策略、每段重新打开、线程和异步交接混入判断。Topos 实际使用的 PyAV/FFmpeg 库版本与命令行版本分别记录。

```powershell
# FFmpeg 把进度和 benchmark 报告写到 stderr。
# 运行于 Windows PowerShell 5.1 时，允许原生命令重定向 stderr，仍用退出码判错。
$PreviousErrorPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
  $InputVideo = Join-Path $Media '4K_H264.mov'
  $Cut = "select='between(n,25,224)+between(n,275,474)',setpts=N/(25*TB)"
  $Ref = Join-Path $Out 'ffmpeg-h264-to-hqx-cut-01.mov'
  ffmpeg -hide_banner -benchmark -n -hwaccel none -i "$InputVideo" -map 0:v:0 -an `
    -vf "$Cut" -frames:v 400 -c:v dnxhd -profile:v dnxhr_hqx -pix_fmt yuv422p10le `
    -color_range tv -colorspace bt709 -color_primaries bt709 -color_trc bt709 "$Ref" `
    2> "$Out/ffmpeg-h264-to-hqx-cut-01.log"
  if ($LASTEXITCODE -ne 0) { throw 'FFmpeg reference failed' }

  ffmpeg -hide_banner -benchmark -hwaccel none -i "$InputVideo" -map 0:v:0 `
    -an -frames:v 400 -f null - 2> "$Out/decode-h264-01.log"
  if ($LASTEXITCODE -ne 0) { throw 'FFmpeg decode reference failed' }

  $DnxInput = Join-Path $Media '4K_DNxHR HQX 10bit.mov'
  ffmpeg -hide_banner -benchmark -hwaccel none -i "$DnxInput" -map 0:v:0 `
    -an -frames:v 400 -f null - 2> "$Out/decode-dnxhr-01.log"
  if ($LASTEXITCODE -ne 0) { throw 'FFmpeg DNxHR decode reference failed' }
} finally {
  $ErrorActionPreference = $PreviousErrorPreference
}
```

要验证 DNxHR 编码本身，另用预热后可驻留内存的小批固定 `yuv422p10le` 帧，给 FFmpeg 与 Topos 使用的编码入口各喂相同帧序、线性 stride 与编码档位。计时范围包括喂帧、编码器 flush 和所选封装策略，分别记持续吞吐和初始化。确认固定输入环形复用不会被异步编码器提前覆盖。若从磁盘喂 rawvideo，单独测读取耗时和带宽；UHD 400 帧未压缩 4:2:2 10-bit 约 13.3 GB，首测容易被磁盘主导。对 TOPOS 则用同一 DLL 分别测 native 解码、native 编码和完整交付，并同时记录原生分段与队列阻塞。

| 看到的现象 | 下一步验证 | 先改哪里 |
|---|---|---|
| Topos 纯剪显著慢于 FFmpeg；解码吞吐接近 | 比对两边格式转换、队列等待、输入和编码器打开次数；跑连续 400 帧与带切点两种。 | 解码会话复用、planar 直送、颜色转换、队列和 I/O |
| Topos 纯剪接近 FFmpeg；两者都显著慢于 Resolve | 对齐 FFmpeg/PyAV 库、线程、HQX 10-bit、码率和输出质量；做固定帧编码测试并将 Resolve 端到端结果单独列出。 | 比对可测的编码器实现与主机配置。Resolve 缺少独立阶段计时的部分保留为待归因。 |
| 原片快，加一枚 LUT 后变慢 | 比较有无 LUT 的 render、GPU wait、RGB/YUV 转换、回读字节和纹理分配。 | 先消除重复纹理上传、分配、转换和同步，再评估新后端。 |
| CPU 低且 GPU 引擎低，队列常空 | 查 ETW 的等待栈、线程饥饿、每帧 fence 和尽头 flush。 | 供帧和线程交接 |
| GPU Video Decode 活跃，端到端仍慢 | 测实际 CPU 下载、上传、map、转色和视频表面等待。对照 CPU planar 与驻留路径。 | 选择成本更低的数据流，并按 GPU、编码、分辨率再量。 |
| TOPOS 全重编慢；包复用正常 | 拆开 native 解码、native 编码、编解码同时运行和容器 I/O。 | 调度或 SIMD 的实际热段 |
| 播放平均接近 25 fps，切点或拖动卡 | 查切点重新打开、关键帧回追、旧 seek 回调、GPU fence 和主线程事件排队。 | 首帧延迟和长尾 |

读取 `topos_exports.jsonl` 的 `stage_telemetry`，查看实际 `delivery_path`、`fallback_root_cause`、各阶段 `backend/sample_count/wall_p95_ms` 与 CPU 上传、下载字节。GPU 提交和等待字段按实际记录填写，缺失字段或未经过已埋点的 stage 记为未知。异步阶段可重叠，阶段总耗时不能直接相加或用整条耗时相减推算编码耗时。确认真实路径后可用 WPR 查 CPU 栈及线程等待。有主题需要时再启用对应的磁盘或 GPU 记录。性能采样与正式跑分分开，且以本机 `wpr -profiles` 列出的配置为准。 [阶段遥测实现](../../src/shared/export/export_stage_telemetry.py) · [WPR 命令行](https://learn.microsoft.com/en-us/windows-hardware/test/wpt/wpr-command-line-options) · [WPR 内置配置](https://learn.microsoft.com/en-us/windows-hardware/test/wpt/built-in-recording-profiles)

```powershell
# 管理员 PowerShell，从有 $Out 的会话执行。记录开始后重现一次慢作业。
wpr -profiles
wpr -start CPU.light -filemode
# 慢作业结束后保存一份采样，随后用 WPA 打开分析。
wpr -stop "$Out/topos-cpu.etl"
```

### Windows 低层诊断

以下命令用于定位路径，正式 4K GUI 基线仍按前面的矩阵。机器首次跑硬件探针前先完成纯默认的实测。探针以唯一 tag 保存报告，按终端提示将 JSON 和 Markdown 拷到本轮目录。

```powershell
& $Py scripts/probe_env_matrix_windows.py --tag $Tag
& $Py scripts/bench_hw_decode_windows.py --only '4K_H264.mov' --frames-4k 120 --tag $Tag
& $Py scripts/bench_timeline_export_windows.py --cases T0,T1 `
  --groups cpu_pipeline,hw_download,ffmpeg_gpu_ref --frames 400 --warmup 60 `
  --runs 3 --bitrate-mbps 20 --keep-outputs --tag $Tag --out-dir "$Out/t0t1"

# D3D11 候选路线再跑。按本机选 d3d11va，NVIDIA 可追加 nvdec。
& $Py scripts/probe_windows_capability_matrix.py --backends d3d11va --tag $Tag
& $Py scripts/probe_d3d11_device_pool_windows.py --frames 1000 --depth 8 `
  --json "$Out/d3d11-pool.json"
```

导出脚本已可运行的 T0/T1 首选素材均为 `2K_h264.mp4`。T0 原尺寸，T1 720p 缩放，无 LUT。报告中 `hw_download` 是组名，母版导出仍可能 CPU 解码后硬编。检查遥测、FFmpeg `ref_mode`、回退原因、回解帧数和尺寸。该脚本的 Qt offscreen 环境适用于诊断，真实 Viewer、WGL、4K LUT 与 DNxHR 对照由正式矩阵提供。`d3d11_gpu_surface` 组当前仅探测能力。若手动运行完整脚本单元测试，素材存在性检查还覆盖未开始的 T8，届时从素材库补齐 `2K_topos_proxy.mov`。 [Windows 解码基准说明](../../scripts/BENCH_HW_DECODE_WINDOWS.md) · [Windows 导出基准实现](../../scripts/bench_timeline_export_windows.py)

硬件路径 A/B 每次从默认环境开始，仅修改当前进程的环境变量，在导入应用模块之前生效。用新进程重复同一场景，结束后清掉覆盖值。优先对照 H.264 4K 的裸解码与实际 Viewer。

| 本轮变量 | 观察内容 |
|---|---|
| `TOPOS_FORCE_WIN_HW_DECODE=1` 或 `TOPOS_DISABLE_WIN_HW_DECODE=1` | 分开试专用硬解与关闭该专用路径。最终是否进入通用硬解或 CPU，以工厂选择与遥测为准。 |
| `TOPOS_DISABLE_WIN_HW_ENCODE=1` | 隔离 H.264 硬编。导出后确认实际编码器和质量档。 |
| `TOPOS_EXPORT_GPU_RENDER=0` | 在同一调色场景诊断关闭 GPU 渲染的影响。保留渲染结果以复核颜色。 |
| `TOPOS_WIN_HW_RESIDENT_DECODE=1` | 在兼容的 NVIDIA CUDA/GL 路线上找出是否减少下载、等待和丢帧。若可用性依赖 `cuda-windows` 额外依赖，隔离安装并记录新环境。 |

## 6. 实施计划

表中入口用于定位已有逻辑。每轮先提交一份证据和假设，开发和验收使用同一场景 ID。单次变更保留独立开关或提交，使前后对照和恢复可复现。

| 顺序 | 任务及实施方法 | 难点与定位入口 | 完成验收 |
|---|---|---|---|
| W0 · 冻结证据 | 完成第 2–5 节。确定首个要追的导出场景和一个播放回归场景。把开发跑分和交付 GUI 分开存。 | 重编码可能悄悄进入包复用，软件组也可能隐藏硬件回退。实际检查 `delivery_path` 和输出，而后建立基准。 | 同一场景三对有效样本可回解，保留设置、版本、散布、指纹与实际路径。 |
| W1 · 补可观测性 | 沿解复用、解码、帧队列、渲染提交、回读、编码提交、编码和封装，给缺口加低开销采样。帧序号贯穿多个线程；记录局部单调时钟、队列水位、等待时长、数据格式、拷贝字节、输入/输出帧和回退原因。仅追踪时启用昂贵细项。 | 先复用现有阶段遥测和播放 trace。将 CPU submit、GPU execution 与 async wait 分列。遇到无样本记录为空。 [交付实现](../../src/features/edit/core/timeline_export.py) · [阶段遥测](../../src/shared/export/export_stage_telemetry.py) · [播放 trace](../../src/shared/video/playback_frame_trace.py) | 400 帧的路径能解释主要等待和 copy 边界，可定位切点及 finish 延迟。关闭详细采样时运行量和回解结果回归通过。 |
| W2 · 纯剪与 DNxHR | 先测原片单段、双切点和 FFmpeg。查同规格 planar 直送，合并经证实冗余的转换；8-bit 4:2:0 → 10-bit 4:2:2 保留一次明确重采样。编码器、重采样器和读取句柄按交付会话复用。先记录线程预算、排队和缓存结果，再调预读/缓冲。 | 10-bit 舍入、strides、色度定位、矩阵、范围和 flush。不能让有 LUT、转场、变速等场景误走快路径。 [编码器](../../src/shared/export/video_encoder.py) · [帧供给](../../src/shared/video/frame_provider_service.py) · [架构](../architecture/deliver-export.md) | 有效输出逐帧回解。关闭绕过的场景真正重编码；对合格包复用单测逐包确认。双切点和单段都复测，H.264→DNxHR 第一目标按第 1 节计算。 |
| W3 · TOPOS 原生 | 先分组测包复用、同档完整重编和单 LUT。原生慢时，用 `TOPOS_CODEC_PROFILE=1` 在独立诊断轮定位 I/O、逆变换、滤波、编码或线程竞争。分别调 `TOPOS_DECODE_WORKERS`、编码线程预算与交接深度，每次只动一项；记录实际生效的路由和资源上限。只有核函数主导才动 AVX2。 | 原生读回调、fd 并发和线程池在 Windows 与 macOS 不同。AVX2 通过运行时 dispatch，保留 scalar，缓冲处理见下例。MSVC Release 重新构建 DLL，确认实载哈希和 ABI。 [母版源](../../src/shared/media/topos_source.py) · [C 编解码核心](../../native/topos_codec/src/codec/codec.c) · [运行时调度](../../native/topos_codec/src/simd/dispatch.c) · [AVX2 内核](../../native/topos_codec/src/simd/transform_avx2.c) | 原生 CTest、旧素材读回、奇数尺寸和高动态范围用例通过。切点、逐包保真、取消和失败清理通过；同时看 400 帧绝对时间和长跑内存。 |
| W4 · 剪辑播放 | 从 W1 找掉帧原因。对照 CPU planar、硬解下载、可用的 GPU 驻留。切点前只预取下一段必需帧。快进、拖动与停止使用请求代际取消旧帧，按媒体 ID、时间戳、尺寸和色彩合同复用缓存；长 GOP 做连续解码与随机 seek 分组。 | 同步由时间线现有时钟维护，避免界面线程等待解码或 fence。跨 GPU/硬件厂商以实际探针和表现决定是否开启。 [帧供给](../../src/shared/video/frame_provider_service.py) · [旧 Windows 解码矩阵](../validation/windows/b22_decode_matrix_machineB_2026-10.md) | 同 Full 分辨率，至少 20 次跳转的 P95 改善；播放掉帧和卡顿下降，切点正确。后加实时音频、拖动、停止重放、多屏和失焦恢复。 |
| W5 · Windows 调色热路径 | 先用现有 OpenGL 路线测原片→单 LUT→同一源三 LUT。按 LUT 内容、采样与设备复用上传，按帧协议复用纹理/中间平面，先去重复转换和同步。多节点保持节点顺序，以及色彩管理、精度和数据范围。 | 常见热点是贴图反复分配、转色、多次回读或前后端等待。跨线程显式移交资源所有权。 [交付 GPU 渲染器](../../src/features/edit/core/timeline_gpu_export_renderer.py) · [色彩图纹理](../../src/features/color/core/color_graph_texture.py) · [HDR 数学约束](../architecture/hdr_math_contract.md) | 无 LUT、单 LUT、三 LUT 各回解抽查，覆盖灰阶、肤色、过曝和饱和色；切换窗口、退出、连续渲染资源稳定。调色收益单独记录。 |
| W6 · D3D11 窄路径 | 只有 W1 证明 CPU 往返或同步主导才做。先按同适配器 LUID 验证 H.264/NV12，接入解码表面→必要 shader→编码表面。用实际产物、GPU 时间戳和空往返计数证明。单 LUT、P010、缩放、转场逐个独立放开。 | 核对 DXGI 枚举与绑定标志、plane view、row pitch、有限/全范围、查询时序、提交线程、表面租约和设备丢失，同适配器验证通过后才尝试多 GPU。 [D3D11 capability](../../src/shared/media/windows_hw/d3d11_export_capability.py) · [D3D11 pipeline](../../src/shared/media/windows_hw/d3d11_pipeline.py) · [跨平台编码表面合同](../../src/shared/export/gpu_encode_surface.py) | 真实 H.264 输出播放且帧数正确，查询有有效采样，无 CPU readback/upload、staging、map 和 `copy_resource`。特性不支持时带原因回到经验证路径；真实窗口和打包版本验证通过后逐步默认启用。 |
| W7 · 发布及回归 | 用第 7 节矩阵验证真实显卡和软件回退；每个机型维护自己的基线。记录驱动限制与现有外部代码风险，对照发布候选版和基线。 | NVIDIA CUDA/GL 仅在已验证配置上试；AMD/Intel 单独实测。新硬件策略按能力与实测收益收敛。 [Windows 升级计划](platform/upgrade_plan_W_windows_2026-10-24.md) · [既有门禁决策](../validation/windows/b23_b24_b3_b4_gate_decisions_2026-10.md) | 普通用户入口、取消、磁盘失败、GPU 丢失、不支持能力、重开工程、长跑都通过。每轮有复测证据和可恢复的独立改动。 |

### 难点示例 A · 异步编解码的表面所有权

把重复分配换成固定容量时，所有权跟着每个帧槽走。生产者写入 → 发布给下游 → 下游标记最后一次读取完成 → 回收给生产者。内存视图跨线程或交给异步编码器时，以实际消费完成事件作为回收边界。同步编码器明确保证返回即完成读取的场景可直接归还。桌面剪辑跳转使用 generation 丢弃过期显示结果；即使显示请求已过期，GPU 仍占用的槽也等 fence 或合约定义的完成事件。

以下是实现逻辑示例。它假设槽位由既有队列的拥有者线程独占管理，`is_complete` 由实际后端实现或测试替身提供。生产代码应复用各后端的 buffer/surface 类型和释放合同。

```python
from collections import deque
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class PendingSurface:
    slot: int
    is_complete: Callable[[], bool]


def reclaim_ready(
    pending: deque[PendingSurface], free_slots: deque[int]
) -> None:
    # FIFO 适用于提交与完成按合同有序的单一队列。
    while pending and pending[0].is_complete():
        free_slots.append(pending.popleft().slot)
```

先用深度 2 的测试替身，故意延迟第一帧完成，断言它没被第 3 帧覆盖；结束时断言每个槽仅归还一次。随后用实际 H.264、DNxHR 或 TOPOS 跑 400 帧，加入中途取消和写盘失败，检查序号、已发布文件、原生引用计数和内存峰值。取消等待已提交引用有界收敛，失败路径按真实 ownership 释放。

### 难点示例 B · D3D11 真实 GPU 时间戳

在设备拥有者线程，使用 `D3D11_QUERY_TIMESTAMP_DISJOINT` 的 Begin/End 包住样本区间，两个 `D3D11_QUERY_TIMESTAMP` 用 End 标记区间起止。每帧入有限深度查询环，从已提交的旧区间异步取数。以下示例用于 W6 的度量设计；探针拿到此耗时后还需核对 LUID、输出回解和传输计数。 [Microsoft 查询类型](https://learn.microsoft.com/en-us/windows/win32/api/d3d11/ne-d3d11-d3d11_query) · [Microsoft 时钟稳定性结构](https://learn.microsoft.com/en-us/windows/win32/api/d3d11/ns-d3d11-d3d11_query_data_timestamp_disjoint)

```cpp
#include <d3d11.h>

enum class TimestampResult { Ready, Pending, Invalid };

TimestampResult ReadGpuMs(ID3D11DeviceContext& ctx, ID3D11Query* disjoint,
                          ID3D11Query* begin, ID3D11Query* end, double& ms) {
    D3D11_QUERY_DATA_TIMESTAMP_DISJOINT clock{};
    UINT64 first = 0;
    UINT64 last = 0;
    const UINT flags = D3D11_ASYNC_GETDATA_DONOTFLUSH;
    const HRESULT clock_hr = ctx.GetData(disjoint, &clock, sizeof(clock), flags);
    const HRESULT first_hr = ctx.GetData(begin, &first, sizeof(first), flags);
    const HRESULT last_hr = ctx.GetData(end, &last, sizeof(last), flags);

    if (FAILED(clock_hr) || FAILED(first_hr) || FAILED(last_hr)) {
        return TimestampResult::Invalid;
    }
    if (clock_hr != S_OK || first_hr != S_OK || last_hr != S_OK) {
        return TimestampResult::Pending;
    }
    if (clock.Disjoint || clock.Frequency == 0 || last < first) {
        return TimestampResult::Invalid;
    }
    ms = 1000.0 * static_cast<double>(last - first) /
         static_cast<double>(clock.Frequency);
    return TimestampResult::Ready;
}
```

验收时模拟未就绪、GPU 时钟不可用、设备丢失和成功四种结果。未就绪在后续异步查询，使用有界查询队列和有效样本计数。原始 CPU 提交耗时与 GPU 执行耗时均记录。NVIDIA 过去验证的 CUDA→GL 驻留预览是可选能力，可在同机确认兼容后用 `TOPOS_WIN_HW_RESIDENT_DECODE=1` 单独试跑；D3D11 全链路需真实表面、验证过的 T0 证据与可回解交付。 [驻留预览实现历史](../validation/windows/b21_cuda_resident_wiring_2026-10.md) · [T0 证据合同](../../src/shared/media/windows_hw/d3d11_export_capability.py)

## 7. 每轮验证、扩大覆盖及暂停条件

| 层级 | 内容 | 标准 |
|---|---|---|
| 每个补丁 | 同环境五对 400 帧，至少一条无关场景，运行相关现有测试；保留指纹和输出。 | 达到本轮收益阈值；画质、帧序、LUT、标签和音频（有音频测试时）正确，原有功能和回退可用。 |
| 路径改动 | 分别测默认、软件回退、显卡最适合路线；测试 H.264 8-bit、DNxHR HQX 10-bit、TOPOS 422 10-bit，并扩展 HEVC Main10、VFR/混帧率、变速、转场和多轨。 | 只有满足能力、画质和性能门的组合启用新路线；记录其他组合的实际路径。 |
| 每次发布候选版 | 本机及可用的 NVIDIA、AMD、Intel 或混合显卡机上跑打包版本。重复起播、拖动、停止、取消、失败恢复和混音导出；用更长的播放、4,000 帧交付及代理开/关补充。 | 内存和显存峰值可解释，无持续增长、文件残留或掉帧退化；机器实际覆盖与缺口写进报告。 |
| 暂停该方向 | 两轮独立改进均未超过抖动，或已满足主目标且剩余开销由编码器或磁盘主导，或替代路径代价显著更高。 | 固定当前基线，留下一份数据和复测条件。遇到新素材、硬件、驱动或阶段指标回归时重新立项。 |

按本轮改动挑对应的现有回归测试。下面是交付和 Windows 路由的起始组合，原生改动仍要跑 CTest。需要真实素材或显卡的用例若跳过，记录原因并通过真机矩阵补上。

```powershell
& $Py -m pytest -q tests/edit/test_export_perf_batch.py `
  tests/edit/test_dnxhr_packet_copy.py tests/media/test_topos_packet_copy.py `
  tests/media/test_windows_hw_pipeline_policy.py tests/media/test_windows_hw_t0_chain.py
```

Windows 首次复测完成后，优先保留 **H.264 → DNxHR 原片及单 LUT** 的三对有效数据、两边的导出设置、Topos 阶段遥测与 FFmpeg 参考。加上 TOPOS 的完整重编及逐包复用样本，即可按第 5 节选择第一项优化。
