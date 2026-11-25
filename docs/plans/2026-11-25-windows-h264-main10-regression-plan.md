# Windows 复测计划 · H.264 播放与 HEVC Main10

2026-11-25 · 修复提交 `4959f0ca`，对照提交 `07ce9dcf`。在同一台 Windows 电脑上比较两个版本。跨应用测速继续沿用已有计划。 [Windows 性能优化计划](2026-11-24-windows-resolve-performance-plan.md)

## 1. 验收目标

| 问题 | Windows 要验证的结果 |
|---|---|
| H.264 原片掉帧和切点长停顿 | 正常 Viewer 能连续播放到末帧。记录实际呈现帧、切点间隔、首帧等待和实际解码路径。macOS 的严格 VideoToolbox 硬解及扩展切点扫描有平台门控。Windows 复测共用的切点预热、缓存键和收尾行为，是否提速以本机数据为准。 |
| HEVC Main10 软件编码曾被崩溃保护挡住 | 通过正常交付入口完成真实 libx265 Main10 编码和完整回解。取消或编码子进程异常退出时保留旧成片、清理暂存，并能重试。 |

## 2. 准备环境

1. 在本机桌面会话打开 PowerShell 并进入仓库根目录。记录 Windows build、CPU、GPU/驱动、显示器刷新率、供电模式、PyAV 和 FFmpeg 版本。A/B 保持同一显示器、窗口尺寸、预览设置和供电。跑分时一次只运行一个播放或导出作业。
2. 素材放到各自 checkout 的 `tests/test_videos`。必备 `4K_H264.mov`。自动 Viewer 脚本还需要 `4K_DNxHR HQX 10bit.mov`，因为会同时构建 DNxHR 对照。只有 H.264 时可在 GUI 手工创建两条 H.264 时间线。可选 `4K_H265.mov` 用于 Main10 输入扩展测试。记录素材和 LUT 的 SHA-256。
3. 使用相同的 x64 Python 和依赖。已有 Windows 原始基线时优先复用。同机新建对照可运行 `git -c core.longpaths=true worktree add --detach ../topos-win-before 07ce9dcf`。两个 checkout 使用各自的配置和输出目录。修复版运行 `git merge-base --is-ancestor 4959f0ca HEAD`，核对退出码为 0。旧版软件测试保持原有崩溃保护开启。
4. Python 依赖和 FFprobe 安装可按已有性能计划第 2 节准备。`$Py` 也可直接指定两个 checkout 共用的 Python 绝对路径。下方脚本从当前源码导入。

在各自 checkout 根目录执行，基线轮把 `$Label` 改为 `before`。

```powershell
$ErrorActionPreference = 'Stop'
$Label = 'after'
$Root = (Get-Location).Path
$Py = (Resolve-Path '.venv/Scripts/python.exe').Path
$Out = (New-Item -ItemType Directory -Path "bench_out/win-regression-$Label-$(Get-Date -Format yyyyMMdd-HHmmss)").FullName
'*' | Set-Content "$Out/.gitignore" -Encoding ascii
$Media = Join-Path $Root 'tests/test_videos'
foreach ($Name in @(
    'TOPOS_BACKEND_GRAPHICS', 'TOPOS_EXPORT_GPU_RENDER',
    'TOPOS_DISABLE_WIN_HW_DECODE', 'TOPOS_FORCE_WIN_HW_DECODE',
    'TOPOS_WIN_HW_RESIDENT_DECODE', 'TOPOS_DISABLE_WIN_HW_ENCODE',
    'TOPOS_ALLOW_KNOWN_X265_10BIT_CRASH', 'QT_QPA_PLATFORM'
)) {
    Remove-Item "Env:$Name" -ErrorAction SilentlyContinue
}
$env:TOPOS_CONFIG_PATH = Join-Path $Out 'topos_runtime/config.json'
$env:PYTHONPATH = $Root
$env:TOPOS_EDIT_PLAYBACK_TRACE = 'summary'
git rev-parse HEAD | Set-Content "$Out/commit.txt"
& $Py -c "import av, sys; print(sys.executable, av.__version__, av.library_versions)" |
    Set-Content "$Out/pyav.txt"
Get-FileHash -Algorithm SHA256 -LiteralPath "$Media/4K_H264.mov" |
    Format-List | Out-String | Set-Content "$Out/media.txt"
Get-CimInstance Win32_VideoController | Select-Object Name, DriverVersion |
    Format-Table | Out-String | Set-Content "$Out/gpu.txt"
```

## 3. H.264 真实 Viewer 播放

1. 建立 3840×2160、25 fps、单视频轨、1×速度、无音频的时间线。同一份 H.264 依次取零基源帧 **[25, 225)** 和 **[275, 475)**，在时间线上相邻，总计 **400 帧**。切点在时间线第 200 帧。复制为第二条时间线，给两个片段各应用同一枚 33 点 LUT，强度 100%。
2. 关闭代理、优化媒体、自动缓存和自适应预览降级，在 Edit 页选择 Full。记录实际渲染和解码尺寸，监看预算仍可能封顶。每版先跑一轮功能检查，再跑至少三轮有效原片和单 LUT，尽量交替运行。稳定性异常时额外复测两对，并记录是否重启应用或重新绑定时间线。
3. 同配置另做三次跨切点跳转、暂停后从切点恢复、播放结束后关闭工程。检查画面顺序、首末帧、黑帧和关闭卡顿。软件回退另开一轮。`TOPOS_DISABLE_WIN_HW_DECODE=1` 可用于关闭专用 Windows 硬解路径，最终是否进入通用硬解要以日志核对。Windows 的专用硬解平面也可能被旧 trace 标记为 `videotoolbox_planar`。以 `decode_manager.create_source` 记录的实际 decoder 和适配器归因。

现有脚本打开真实 Viewer 并记录 `frame_presented`。每轮执行 H.264 和 DNxHR 各自的原片及 LUT 四个场景。`--source h264` 只影响导出子命令。 [Viewer 采集入口](../../bench_out/resolve_vs_topos_2026-11-22/topos_bench.py)

```powershell
$History = 'bench_out/resolve_vs_topos_2026-11-22'
Copy-Item -LiteralPath @("$History/topos_bench.py", "$History/Topos_Resolve_AB_33.cube") -Destination $Out
Get-FileHash -Algorithm SHA256 -LiteralPath @(
    "$Media/4K_DNxHR HQX 10bit.mov", "$Out/Topos_Resolve_AB_33.cube"
) | Format-List | Out-String | Add-Content "$Out/media.txt"
$Bench = Join-Path $Out 'topos_bench.py'
& $Py $Bench build
if ($LASTEXITCODE -ne 0) { throw 'Benchmark project build failed' }

& $Py $Bench playback --repeat 1 | Tee-Object "$Out/playback-smoke.log"
if ($LASTEXITCODE -ne 0) { throw 'Viewer smoke failed' }
Move-Item -LiteralPath "$Out/topos_playback.jsonl" -Destination "$Out/playback-smoke.jsonl"

& $Py $Bench playback --repeat 3 | Tee-Object "$Out/playback.log"
if ($LASTEXITCODE -ne 0) { throw 'Viewer benchmark failed' }
```

确认每一行 `PLAY` 都从第 0 帧开始，播放状态和 Viewer 可见状态为 True，总长 400 帧。有效样本正常运行约 16 秒并走到第 399 帧。脚本达到 35 秒也会记录结束，因此遇到超时、未加载或 Viewer 不可见须标为无效。末帧验收同时检查播放头和 `frame_presented` 记录。

把下面代码保存为本轮目录的 `summarize-playback.py`。切点间隔取跨过 200 帧前后的呈现时间差。

```python
import json
from pathlib import Path
from statistics import median, quantiles

root = Path(__file__).resolve().parent
groups: dict[bool, list[int]] = {}
failures = 0
for line in (root / "topos_playback.jsonl").read_text(encoding="utf-8").splitlines():
    row = json.loads(line)
    if row["source"] != "h264":
        continue
    frames = [(t, f) for t, f in row["frames"] if 0 <= f < 400]
    if row["playhead"] != 399 or not frames or row["wall_s"] >= 35:
        print("INVALID", row["graded"], row["repeat"])
        failures += 1
        continue
    seen = {f for _, f in frames}
    last_frame_presented = 399 in seen
    if not last_frame_presented:
        failures += 1
    gaps = [(b[0] - a[0]) * 1000 for a, b in zip(frames, frames[1:])]
    left = max((t for t, f in frames if f < 200), default=None)
    right = min((t for t, f in frames if f >= 200), default=None)
    print(json.dumps({
        "lut": row["graded"], "trial": row["repeat"], "presented": len(seen),
        "last_frame_presented": last_frame_presented,
        "missing": sorted(set(range(400)) - seen), "wall_seconds": row["wall_s"],
        "interval_p95_ms": quantiles(gaps, n=20)[18] if len(gaps) > 1 else None,
        "cut_gap_ms": (right - left) * 1000 if left is not None and right is not None else None,
        "first_present_ms": frames[0][0] * 1000,
        "decode_paths": row["trace"].get("decode_path_counts"),
        "render_paths": row["trace"].get("render_path_counts"),
    }))
    groups.setdefault(row["graded"], []).append(len(seen))
for graded, counts in groups.items():
    print("median", "LUT" if graded else "original", median(counts), counts)
if failures or len(groups) != 2:
    raise SystemExit("Viewer did not pass the sample validity or final-frame check")
```

```powershell
& $Py "$Out/summarize-playback.py" | Tee-Object "$Out/playback-summary.txt"
if ($LASTEXITCODE -ne 0) { throw 'Viewer sample or final-frame check failed' }
```

**判定**。同机已有流畅 UHD25 基线时，原片三轮中位数目标至少 **392/400**，切点前后正常呈现目标间隔 **≤80 ms**。中位数比旧版少超过 4 帧、修复版两轮以上出现同一段新的连续缺帧，或切点间隔明显变长，均记为疑似回归并复测。基线本身达不到目标时，同时保留两版的实际呈现帧数、缺帧位置、切点间隔和后端。软件回退单列。

## 4. HEVC Main10 实际交付

从新进程运行，确保环境覆盖生效。在软件轮设置 `TOPOS_DISABLE_WIN_HW_ENCODE=1`，然后从该终端启动正常 GUI。关掉软件轮进程后删除环境变量，再测硬件默认模式。

```powershell
$env:TOPOS_DISABLE_WIN_HW_ENCODE = '1'
& $Py -m apps.topos_studio.main
```

| 场景 | 操作 | 验收 |
|---|---|---|
| 软件短片 | 从同一 H.264 源取 [25, 49)，UHD25，无音频，交付 HEVC Main10 MP4，10-bit 4:2:0，Rec.709 | 24 帧完整回解，实际选择软件 libx265。 |
| 软件 400 帧 | 使用第 3 节原片时间线，同规格交付。有合适音频时再复制一条，打开音频复测 | 400 帧完整回解，检查时间戳和音轨。 |
| 取消与重试 | 在测试目录做一份旧成片、记录 SHA-256。向同名文件重编码，在进度中途取消，随后重试 | 旧文件哈希一致，暂存清理，重试能生成可回解文件。 |
| 默认硬件与回退 | 软件轮退出后执行 `Remove-Item Env:TOPOS_DISABLE_WIN_HW_ENCODE -ErrorAction SilentlyContinue`，重开 GUI。用至少 1080p 的 SDR 短片复测 | 真命中硬编时记录实际编码后端，如 `hevc_nvenc` 或 `hevc_amf`。实际尝试后失败时记录回退原因，软件结果也能回解。机器无对应能力时记录未覆盖路径。 |

硬编证据可查交付 manifest 的 `stage_telemetry.stages` 中 `encode` 的 backend。FFprobe 的 `codec_name=hevc` 只确认格式。PQ 和 HLG 项目时间线使用现有的隔离软件路径，另靠第 5 节验证这些信号的元数据。正常 SDR 素材保留 Rec.709。

将下方 `$File` 指向实际 SDR 输出。短片把 `$Expected` 改成 24。使用 1080p 时把 `$Width` 和 `$Height` 改成 1920 和 1080。音频用例同时检查音轨数量、采样率和时长。

```powershell
$File = Join-Path $Out 'win-main10-400.mp4'
$Expected = 400
$Width = 3840
$Height = 2160
$Raw = ffprobe -v error -count_frames -show_streams -show_format -of json "$File"
if ($LASTEXITCODE -ne 0) { throw 'Output probe failed' }
$Raw | Set-Content "$File.ffprobe.json" -Encoding utf8
$Probe = $Raw | Out-String | ConvertFrom-Json
$Video = @($Probe.streams | Where-Object { $_.codec_type -eq 'video' })
if ($Video.Count -ne 1 -or $Video[0].codec_name -ne 'hevc' -or
    $Video[0].profile -ne 'Main 10' -or $Video[0].pix_fmt -ne 'yuv420p10le' -or
    $Video[0].width -ne $Width -or $Video[0].height -ne $Height -or
    $Video[0].color_primaries -ne 'bt709' -or $Video[0].color_transfer -ne 'bt709' -or
    $Video[0].color_space -ne 'bt709' -or
    $Video[0].avg_frame_rate -ne '25/1' -or [int]$Video[0].nb_read_frames -ne $Expected) {
    throw 'Main10 output does not match the test settings'
}
ffmpeg -v error -xerror -i "$File" -map 0:v:0 -map '0:a?' -f null -
if ($LASTEXITCODE -ne 0) { throw 'Full decode failed' }
```



## 5. 故障注入与回归

故障脚本和软件交付测试使用 `TOPOS_DISABLE_WIN_HW_ENCODE=1`，使普通 SDR 时间线路由到软件编码。恢复默认环境后运行硬编路由、回退和播放回归。仓库里用 `SIGKILL` 和 `SIGBUS` 注入的两项 POSIX 用例在 Windows 会跳过。用下面的 Windows 复测补齐启动和关闭两个阶段。 [Main10 回归用例](../../tests/deliver/test_hdr_video_delivery.py) · [硬编回退用例](../../tests/media/test_ffmpeg_encoder_hevc10_guard.py)

把代码保存为 `$Out/test-main10-worker-fault.py`，从仓库根目录运行。脚本只替换自己启动的测试子进程入口。必须保存成文件，Windows 的 spawn 需要重新导入它。启动故障模拟尚未就绪时退出。关闭故障模拟孩子写入暂存并发出 ACK 后异常退出。两种情况均要保住旧文件、清理暂存，随后由**同一个父进程**完成一次真实的软件 Main10 导出。

```python
import os
from pathlib import Path

import av
import numpy as np

from src.shared.export import hdr_hevc_encoder as isolated
from src.shared.export.video_encoder import FFmpegEncoderConfig, FFmpegVideoEncoder
from src.shared.media.frame import DecodedFrame
from src.shared.media.metadata import Rational


def exit_before_ready(*_args: object) -> None:
    os._exit(37)


def exit_after_close_ack(channel, _buffer, path, _config, _frames, _chroma) -> None:
    channel.send(None)
    operation, _ = channel.recv()
    if operation != "close":
        os._exit(38)
    Path(path).write_bytes(b"unverified worker output")
    channel.send(None)
    os._exit(37)


def main() -> None:
    root = Path(__file__).resolve().parent / "main10-fault"
    root.mkdir()
    output = root / "previous.mov"
    original = b"sentinel for a previous successful export"
    output.write_bytes(original)
    config = FFmpegEncoderConfig(
        codec="hevc", hwaccel="off", pix_fmt="yuv420p10le",
        width=64, height=32, fps=Rational(24, 1),
    )

    real_worker = isolated._encode_main10
    try:
        for phase, worker in (
            ("startup", exit_before_ready),
            ("close", exit_after_close_ack),
        ):
            isolated._encode_main10 = worker
            encoder = FFmpegVideoEncoder(str(output), config)
            try:
                encoder.open()
                encoder.close(expected_frames=0)
            except RuntimeError as error:
                assert "exited (37)" in str(error) or "exit 37" in str(error), error
                print(phase, str(error))
            else:
                raise AssertionError(f"Unexpected success after {phase} fault")
            finally:
                encoder.abort()
            assert output.read_bytes() == original
            assert not list(root.glob(".topos-hdr-*"))
    finally:
        isolated._encode_main10 = real_worker

    retry = root / "retry.mp4"
    encoder = FFmpegVideoEncoder(str(retry), config, total_frames=24)
    try:
        encoder.open()
        image = np.full((32, 64, 3), 32768, np.uint16)
        for index in range(24):
            encoder.encode_frame(DecodedFrame(
                frame_index=index, pts=index, time_seconds=index / 24,
                data=image, width=64, height=32, pixel_format="bgr48le",
            ))
        assert encoder.close(expected_frames=24, should_cancel=lambda: False)
    finally:
        encoder.abort()
    with av.open(str(retry)) as container:
        frames = list(container.decode(video=0))
        assert len(frames) == 24 and frames[0].format.name == "yuv420p10le"
    assert not list(root.glob(".topos-hdr-*"))
    print("Both fault paths preserved the old file, and the retry decoded 24 frames.")


if __name__ == "__main__":
    main()
```

使用新的实验目录复跑故障脚本，每次保留独立日志。保留 pytest 的 passed、skipped、failed 和理由。

```powershell
$env:TOPOS_DISABLE_WIN_HW_ENCODE = '1'
try {
    & $Py "$Out/test-main10-worker-fault.py" | Tee-Object "$Out/main10-fault.log"
    if ($LASTEXITCODE -ne 0) { throw 'Main10 fault recovery failed' }

    & $Py -m pytest -q -rs tests/deliver/test_hdr_video_delivery.py |
        Tee-Object "$Out/main10-software-pytest.log"
    if ($LASTEXITCODE -ne 0) { throw 'Software delivery regression suite failed' }
} finally {
    Remove-Item Env:TOPOS_DISABLE_WIN_HW_ENCODE -ErrorAction SilentlyContinue
}

& $Py -m pytest -q -rs tests/media/test_ffmpeg_encoder_hevc10_guard.py `
    tests/video/test_ffmpeg_encoder.py tests/edit/test_edit_gpu_decode_manager.py `
    tests/edit/test_timeline_preview_renderer.py | Tee-Object "$Out/regression-pytest.log"
if ($LASTEXITCODE -ne 0) { throw 'Regression suite failed' }
```

**判定**。短片、400 帧成片和软件回归通过时，软件交付记为通过。故障脚本两个阶段、旧文件、清理和重试均通过时，崩溃隔离记为通过。记录实际硬件覆盖情况。交付预设拒绝、回解错误、帧数不符、主程序退出、旧成片被覆盖或暂存残留均判失败。

## 6. 回填结果

| 检查 | 旧版结果 | 修复版结果 | 实际后端及证据文件 |
|---|---|---|---|
| H.264 原片 400 帧，默认路径 | 待测 | 待测 | 播放 JSONL、切点及缺帧 |
| H.264 单 LUT 400 帧，默认路径 | 待测 | 待测 | 播放 JSONL、项目设置 |
| H.264 回退、跳转与恢复 | 待测 | 待测 | 源解码日志 |
| 软件 Main10 24 帧与 400 帧 | 待测 | 待测 | manifest、FFprobe、完整回解 |
| 取消、启动故障、关闭故障、重试 | 待测 | 待测 | 旧文件哈希、故障日志 |
| 默认硬件与编码回退 | 待测 | 待测 | 实际编码器、驱动和回退原因 |
| PQ、HLG、母版元数据及双声道 PCM | 待测 | 待测 | pytest 和回解结果 |

基准原始记录和报告可保留在 `bench_out`。项目的 `tests/test_videos` 继续使用本地测试素材。
