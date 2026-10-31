# 4K 零拷贝播放与 Quick Project 崩溃复盘优化计划

检查时间：2026-05-25 01:05 CST

## 结论

当前实现还不能判定为“全程 GPU 零拷贝播放”已完成。你这次的改动把 macOS VT/IOSurface 的实现标记从 `False` 改成了 `True`，并补了一批 PyObjC / PyOpenGL 调用，但本机验证显示 capability gate 仍会在 macOS 上抛出 `NameError: name 'objc' is not defined`，导致硬解预览路径不能稳定 fallback。

Quick Project 的核心数据创建和保存本身可以成功；“点击创建后软件崩溃”的更高概率触发点在创建后进入 Edit/Color 预览加载链路时，`MediaSourceFactory.create_with_hwaccel(... DecodeTarget.PREVIEW ...)` 会尝试导入 macOS zero-copy capability，而当前导入失败不是普通禁用状态，而是未捕获异常。

另一个需要一起修的 UI 风险是：`QuickProjectDialog._on_create()` 在 `self.accept()` 前同步发射 `quick_project_created` 信号，主窗口 slot 会在模态对话框尚未关闭时创建项目、切页面、写 timeline、保存文件。这种重入对 Qt 页面/模型状态不友好，应该改成 dialog 先关闭，MainWindow 再读取结果并创建项目。

## 已验证现象

| 验证项 | 命令/位置 | 结果 |
|---|---|---|
| Quick Project 数据层创建并保存 | `EditProject.create_quick_project()` + `EditProjectIO.save_project()` | 成功，能生成 `.topos` |
| macOS zero-copy capability | `MediaSourceFactory.create_with_hwaccel(... DecodeTarget.PREVIEW ...)` | 失败，抛 `NameError: name 'objc' is not defined` |
| macOS zero-copy 测试收集 | `pytest tests/media/test_macos_vt_zero_copy.py` | 失败，测试收集阶段即导入 `vt_session.py` 报错 |
| 语法编译 | `py_compile` 相关文件 | 语法层通过，但运行期导入和原生调用仍有问题 |

## 问题与优化计划表

| 优先级 | 问题 | 证据/位置 | 根因判断 | 修复方案 | 完成后检查 |
|---|---|---|---|---|---|
| P0 | macOS zero-copy capability 导入崩溃，不能 fallback | `src/core/media/macos_vt/vt_session.py:51`, `:272`, `:311`；导入时报 `NameError: objc` | `from Cocoa import NSObject, objc` 失败后 `_PYOBJC_AVAILABLE=False`，但文件继续定义 `class VtDecompressionOutputCallback(NSObject)` 和 `@objc.python_method` | 改成 `import objc` 与 `from Foundation import NSObject` 分开导入；如果 PyObjC 不可用，不定义 NSObject 子类，或定义纯 Python no-op fallback；`capability.py` 捕获 `Exception` 并返回 `available=False` | `pytest tests/media/test_macos_vt_zero_copy.py -q` 不再收集失败；`create_with_hwaccel(PREVIEW)` 在缺 PyObjC 时返回 CPU fallback，不抛异常 |
| P0 | capability gate 被过早放行 | `src/core/media/macos_vt/vt_session.py:46`, `src/core/media/macos_vt/iosurface_gl_bridge.py:38` | `_VT_DECODE_IMPLEMENTED=True`、`_IOSURFACE_BRIDGE_IMPLEMENTED=True` 不是由真实 probe 驱动 | 在真实 4K probe JSON 通过前恢复为 `False`，或增加 `TOPOS_VIDEO_ZERO_COPY_PROBE_JSON` 强制验收；`force` 也不能绕过实现缺失 | 无 probe 时 HUD/日志必须显示 `zero_copy=false` 与 fallback reason；`get_zero_copy_capability().available == False` |
| P0 | Quick Project 创建后 UI 重入风险 | `src/ui/edit/quick_project_dialog.py:_on_create`, `src/ui/main_window.py:_on_quick_project_requested` | dialog 内同步 emit，MainWindow 在 `dialog.exec()` 未返回时切页面、设置 project、append clip、保存 | 去掉同步 `quick_project_created` 流程。`_on_create()` 只保存参数并 `accept()`；MainWindow 在 `if dialog.exec() == Accepted:` 后调用 `_create_and_open_quick_project()` | 手动双击视频 -> Create Project，窗口先关闭，再进入项目；无崩溃、无重复 slot、无半创建状态 |
| P0 | Quick Project 异常 fallback 会再次触发视频加载崩溃 | `src/ui/main_window.py:7378-7385` | `_create_and_open_quick_project()` 捕获任何异常后直接 `self.load_video(media_path)`，而当前 video load 正好会踩 zero-copy capability 崩溃 | 创建失败时只提示错误并保持当前页面；如果要 fallback 播放，必须显式走 CPU/fallback-safe 路径，或先设置 `TOPOS_VIDEO_ZERO_COPY=off` 级别的运行时开关 | 故意制造保存失败时软件不崩；日志包含失败原因，不自动进入不安全播放路径 |
| P0 | VT packet -> CMSampleBuffer 实现不可验证，且可能没有把 packet bytes 写入 block buffer | `src/core/media/macos_vt/vt_session.py:1127-1164` | `NSData` 创建后未使用；`CMBlockBufferCreateWithMemoryBlock` 调用签名/内存填充方式可疑；`CMSampleTimingInfo` 直接填 int，不是明确 CMTime | 用 PyObjC 可用的标准 CoreMedia API 重写：创建 `CMBlockBuffer` 时传入真实 packet bytes 或 `CMBlockBufferReplaceDataBytes`；PTS/DTS 使用 `CMTimeMake`；sample size 数组必须正确 | 独立 probe 可连续解码 120 帧 H.264/HEVC；无 timeout；帧数、PTS 单调 |
| P0 | VT 回调注册方式不可靠 | `src/core/media/macos_vt/vt_session.py:571-582`, `:311-356` | `VTDecompressionSessionCreate` 需要正确的 output callback record/selector 绑定；直接传 NSObject 实例可能不是有效 PyObjC 签名 | 做一个最小独立原型验证回调能收到 `CVPixelBufferRef`；失败就不要接主工程。回调线程只 retain + enqueue，绝不碰 GL | probe 日志显示 callback frame count > 0；队列 drop/release 统计正确 |
| P0 | IOSurface -> CGL 返回值判断反了 | `src/core/media/macos_vt/iosurface_gl_bridge.py:404-417` | `CGLTexImageIOSurface2D` 返回 `CGLError`，通常 `0` 表示成功；代码用 `if not success:` 会把成功当失败 | 改成 `err = CGLTexImageIOSurface2D(...)`，只有 `err != 0` 才失败，并记录 CGLError code | bridge 单元/集成 probe 显示绑定成功帧数 > 0 |
| P0 | OpenGL context 线程边界不安全 | `src/core/media/macos_vt/source.py:179-235` | `read_next_frame()` 里直接创建/绑定 GL texture，但该函数可能在解码/读取线程执行，不一定是 ModernGL 渲染线程 | `MacOSVtZeroCopySource` 只输出 retained `CVPixelBuffer` 或 `GpuDecodedFrame` 的未绑定句柄；真正 `CGLTexImageIOSurface2D` 移到 `AsyncRenderer` 的 GL 线程执行 | 添加断言/日志：VT callback thread 无 GL，read thread 无 GL，render thread 才 bind IOSurface |
| P0 | CVPixelBuffer 生命周期有泄漏和错放风险 | `src/core/media/macos_vt/source.py:219-285`, `iosurface_gl_bridge.py:261-265`, `:492-505` | queue retain、source retain、bridge retain 三层叠加；queue retain 未释放；bridge 只记 `_previous_pixel_buffer`，多帧在飞时会覆盖错误对象 | 每个 frame 建独立 release token：记录该帧 pixel_buffer 的 retain 来源；渲染完成后按来源 exactly-once release；ring buffer slot 复用前等待/释放对应 slot | 压测 300/3000 帧，retain_count 回到 0；队列 clear、seek、drop、close 都 release exactly once |
| P1 | IOSurface texture target 与 shader 坐标不明确 | `iosurface_gl_bridge.py:338-342`, GpuFramePlane target | 注释说 IOSurface 常用 `GL_TEXTURE_RECTANGLE`，代码强行 `GL_TEXTURE_2D`；现有 shader 是否支持 rectangle 未验证 | 先在 probe 中确认 target。若用 rectangle，新增 shader path 用 pixel coordinates；若用 2D，必须证明 CGL 接受且采样正确 | probe 带颜色校验；NV12/P010 输出不翻转、不偏色 |
| P1 | P010 格式被降成 normalized `GL_R16/GL_RG16`，HDR/10-bit 语义不清 | `iosurface_gl_bridge.py:_PLANE_MAP_P010_LDR` | 10-bit P010 实际有效位和采样归一化需要 shader 明确处理；不能只为了兼容 shader 改格式 | 明确 P010 采样策略：integer path 或 normalized path 二选一，并写明 bit shift/range/matrix | H.265 PQ/HLG fixture 色彩误差在阈值内；HDR metadata 传递完整 |
| P1 | Fallback 日志与 HUD 需要更硬的验收 | `performance_monitor.py`, `playback_hud.py`, `factory.py` | 当前崩溃前没有稳定落到 `zero_copy=false`，也不能让 stub 被 HUD 显示成成功 | 每次 zero-copy 不可用记录 `zero_copy=false fallback_reason=... decode_path=cpu/ffmpeg_hw_download`；HUD 只在 probe 成功且无 CPU copy 时显示 ON | 播放普通视频时 HUD 明确显示 zero-copy OFF/ON；日志可 grep |
| P1 | Quick Project 保存使用临时 `EditProjectIO` 并启动 autosave timer | `src/ui/main_window.py:7346-7354`, `edit_project_io.py:349-367` | 创建临时 IO 对象只为保存一次，却启动 QTimer；对象不保存为 MainWindow 状态 | 使用已有 `self.edit_project_io` 或创建 `EditProjectIOConfig(autosave_interval=0, backup_on_save=True)` 的一次性保存器 | Quick Project 保存后无多余 timer 警告；当前项目 IO 状态统一 |
| P2 | Windows zero-copy 仍是占位/独立路线 | `src/core/media/windows_hw/*` | 与 macOS 无关，但不能把 macOS 成功当多平台完成 | 继续保持平台能力独立：Windows D3D11/NVDEC probe 单独做，不复用 IOSurface 逻辑 | Windows 下独立 probe JSON 与 macOS probe JSON 分开验收 |

## 推荐实施顺序

| 阶段 | 目标 | 任务 | 验收命令 |
|---|---|---|---|
| Phase 1 | 先止血，保证软件不崩 | 修 PyObjC import/fallback；zero-copy flag 恢复保守；`factory.py` 捕获 capability 的所有普通异常；Quick Project dialog 改为 exec 返回后创建 | `pytest tests/media/test_macos_vt_zero_copy.py tests/media/test_v3_hwaccel_policy.py -q` |
| Phase 2 | Quick Project 稳定性 | 加 GUI/非 GUI 回归：创建项目、保存、设置 project、append clip；创建失败不自动 `load_video` | 手动：Color/Library 双击视频 -> Create Project -> 进入 Edit，重复 10 次不崩 |
| Phase 3 | macOS VT 独立 probe | 只在独立脚本里实现 packet demux -> VT decode -> CVPixelBuffer/IOSurface，不接主播放 | `python src/core/media/macos_vt/vt_zero_copy_probe.py --file tests/media/test_h264_sdr.mp4 --frames 120 --warmup 10 --output-json tests/media/results/macos_vt_probe.json` |
| Phase 4 | IOSurface GL bridge probe | 在渲染线程绑定 IOSurface plane；校验 CGL 返回码、texture target、NV12/P010 shader | probe JSON 中 `zero_copy=true`, `cpu_download_frames=0`, `texture_write_frames=0`, `frames_tested>=120` |
| Phase 5 | 接入主播放链路 | `MacOSVtZeroCopySource` 不在 decode/read 线程碰 GL；由 `AsyncRenderer` bind/render/release | `verify_zero_copy_playback.py --require-zero-copy --verify-color` 通过 |
| Phase 6 | 长跑和回归 | seek/drop/close/循环播放/4K 60fps；检查 retain/release 与 HUD | 3000 帧无泄漏、无崩溃；HUD 和日志路径一致 |

## 给后续 AI 的硬性约束

1. 在真实 probe 通过前，不允许把 `_VT_DECODE_IMPLEMENTED` 或 `_IOSURFACE_BRIDGE_IMPLEMENTED` 设为 `True`。
2. 不允许把 CPU upload/download 包装成 `GpuDecodedFrame.zero_copy=True`。
3. VT callback 线程只能 retain `CVPixelBuffer` 并入队，不能调用 OpenGL。
4. `CGLTexImageIOSurface2D` 只能在持有正确共享 OpenGL/CGL context 的渲染线程调用。
5. 每个 `CVPixelBufferRetain` 必须有可追踪、exactly-once 的 `CVPixelBufferRelease`。
6. zero-copy 失败必须走 CPU fallback，并在日志/HUD 中明确 `zero_copy=false` 与 `fallback_reason`。
7. Quick Project 的 UI 流程必须避免 dialog 内同步触发主窗口切页和 timeline 修改。

## 复现与检查命令

```bash
.venv/bin/python -m pytest tests/media/test_macos_vt_zero_copy.py -q --tb=short
```

当前失败点：测试收集阶段导入 `vt_session.py`，`@objc.python_method` 处 `NameError`。

```bash
.venv/bin/python - <<'PY'
from src.core.media.factory import MediaSourceFactory
from src.core.media.decode_policy import DecodeTarget

source, status = MediaSourceFactory.create_with_hwaccel(
    "tests/media/test_h264_sdr.mp4",
    decode_target=DecodeTarget.PREVIEW,
    auto_open=False,
)
print(type(source).__name__, status.decoder_type, status.fallback_reason)
if hasattr(source, "close"):
    source.close()
PY
```

修复后预期：即使 PyObjC 不完整，也应该返回 CPU/FFmpeg fallback，不应抛异常。

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from src.core.edit.project import EditProject
from src.core.edit.edit_project_io import EditProjectIO, EditProjectIOConfig

media = Path("tests/media/test_h264_sdr.mp4").resolve()
with TemporaryDirectory() as d:
    project, asset_id, timeline_id = EditProject.create_quick_project("quick_probe", str(media))
    out = Path(d) / "quick_probe.topos"
    ok = EditProjectIO(EditProjectIOConfig(autosave_interval=0)).save_project(str(out), project)
    print({"ok": ok, "exists": out.exists(), "asset": bool(asset_id), "timeline": bool(timeline_id)})
PY
```

当前结果：数据层成功。后续要补的是 UI 创建流程和播放预览 fallback 稳定性。
