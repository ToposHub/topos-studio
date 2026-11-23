# Topos Studio macOS Metal 全链路迁移实施计划

基线提交 `6249e136`，2026-11-22。目标是在 macOS 上让剪辑、调色、合成、交付和受支持的着色器插件默认通过 Metal 渲染。预览和导出从同一工程快照求值，共享帧格式、色彩语义和效果参数。Windows/Linux、macOS 兼容模式和画质对拍使用独立的 OpenGL 路径。已有的 ADR、跨平台迁移计划和项目格式是施工依据。 [GPU 后端 ADR](../adr/0003-metal-gpu-backend-dual-track-and-phase1-gates.md) · [早期迁移计划](../metal_backend_migration_and_cross_platform_gpu_plan.md)

## 从哪里开始

1. 用阶段 0 配好环境，生成能力报告和基线。项目和素材用独立副本，测试缓存与用户配置分开。
2. 每个编号单独实现。先复现要解决的问题，记录现有行为，再加新路径和针对性测试。增加或改变 GPU 能力时，先由 Explore 梳理调用关系，再设计实现，最后做 Review 和真机验证。
3. 每完成一个编号，在实施记录中填入提交号、运行命令、测试报告、实际后端、差异和回退结果。表中涉及的文件是**现有入口**，新增结构和参数在正文标明。带「参考实现」的片段是任务的接口或算法示例，需接入表中指定的生产入口。
4. 按依赖推进。阶段 1 和 2 是下一批开发任务。阶段 3 的音频封装与 DNxHR 分段测量可以并行。阶段 5 的 3D 和阶段 6 的插件长尾可独立验收。任何默认切换都要过阶段 8 的发布门。

| 阶段 | 前置 | 交付物 | 通过标志 |
|---|---|---|---|
| 0 环境和基线 | 当前提交 | 可重建的 A/B 工程、机器和媒体清单、GPU 调用清单 | 留存原始日志并能重跑同一工程 |
| 1 共享 GPU 帧 | 0 | 设备身份、纹理描述、完成与租约协议 | 压测下槽位安全复用，过期帧安全丢弃 |
| 2 原生视口和剪辑 | 1 | Program Viewer、Source Viewer 和剪辑渲染链 | 单 LUT 直接 Metal 上屏，再覆盖常规多轨工程 |
| 3 交付和 DNxHR | 1，剪辑语义与 2 同步 | 含音频 H.264、复杂工程覆盖、DNxHR 性能分解 | 帧数、PTS、音频、取消和输出原子提交均通过 |
| 4 调色 | 1、2 的视口宿主 | 共享 Color 图执行、Color Viewer、示波器、画廊 | 编辑、播放和交付使用一致的图与色彩语义 |
| 5 合成 | 1、2、4 的图像与色彩契约 | 2D、时间类节点、缓存、3D 和 Composite Viewer | 独立合成、Comp Clip 和交付三路对拍 |
| 6 插件和效果 | 4、5 的宿主执行接口 | 内置效果和插件后端能力、可诊断回退 | 老工程可重开，已启用插件两端输出一致 |
| 7 全应用清理 | 2–6 | GPU 使用面逐项销账、独立发布构建 | 受支持的默认工程跑完时 Topos OpenGL 计数为零 |
| 8 发布和维护 | 0–7 | 构建证据、回退开关、持续回归矩阵 | 真机、跨平台、安装包和回退验收齐全 |

~~~mermaid
flowchart LR
    A["工程快照与时间线时钟"] --> P["调度与效果能力预检"]
    D["VideoToolbox 或 CPU 解码"] --> F["共享 GPU 帧"]
    F --> R["剪辑、调色和合成 Metal 执行"]
    P --> R
    R --> V["Metal 视口与 GPU 示波器"]
    R --> E["VideoToolbox 或 CPU 编码"]
    AU["既有音频混音和采样时钟"] --> M["封装与原子发布"]
    E --> M
~~~

调度以工程快照、帧时刻和设备能力决定路径。帧携带像素格式、色彩描述和生命周期。执行器可替换，但项目文件的节点、参数、轨道、关键帧和效果标识继续由既有服务读写。录入新效果时由 Effect Catalog 声明预览、导出和格式能力。GPU 对象由工作线程或指定的呈现线程管理，窗口和事件由 UI 线程管理。 [仓库开发约束](../../.clinerules)

### 施工中会遇到的术语

- **帧租约**表示使用方持有纹理的资格，归还后才可能重新分配槽位。GPU 写入完成和视口读完是独立事件。
- **代际**是在跳转、切换工程、重配置显示或重建设备后递增的版本。过期结果只用于回收，不用于更新显示。
- **对拍**指相同输入与参数分别运行 Metal 和参考后端，再比对 帧、画质、时间戳和行为。**A/B 性能**另外比较耗时与资源。

### 当前实测起点

| UHD 25 fps、400 帧、单 LUT、无音频 | Topos OpenGL | Topos Metal | Resolve Studio | 当前结论 |
|---|---:|---:|---:|---|
| H.264 输入 → H.264 High，50 Mbit/s VBR 目标 | 10.16 秒 | 7.06 秒 | 6.88 秒 | Metal 路径为 Resolve 的约 1.026 倍 |
| DNxHR HQX 10-bit 输入 → DNxHR HQX | 17.67–17.91 秒 | 同场景待测 | 10.21 秒 | 现有实测约 1.73–1.75 倍，1.5 倍目标约为 15.31 秒 |

H.264 原生交付目前按条件启用，覆盖部分同尺寸 SDR H.264、最多四个符合条件的串联 Corrector 和双边交叉溶解。实际交付含音频轨、部分复杂图或不匹配的媒体格式时，仍可能走兼容路径。剪辑页当前可让单层 NV12 的单 LUT 预览用 Metal 渲染，由 OpenGL 视口呈现。Color 已有 MSL 内核和测试图执行器，Composite 已有部分 Metal compute 和 CPU 搬运桥。 [交付入口说明](../architecture/deliver-export.md) · [剪辑架构](../architecture/edit-timeline.md) · [合成搬运桥](../../src/features/composite/render/metal_compute_bridge.py)

可重建基准包含两段源帧 `[25, 225)` 和 `[275, 475)`，工程、33³ LUT、配置和脚本位于仓库基准目录。上述 Metal 结果来自当前机器的本地 `.topos_cache/metal_rollout_8SVusV/rollout_results.json`，DNxHR 区间来自 `.topos_cache/export_next_X2ca7W/results.json`。缓存报告随运行生成。记录实际码率、色彩标签、编码器和加速路径，H.264 的目标码率相同但实际码率可有差别。为多轨、HDR、插件、带音频和 3D 各自建立结果。性能对照优先用 H.264 和 DNxHR，ProRes 仅列在已有工程的兼容性回归范围。 [基准工程和脚本](../../bench_out/resolve_vs_topos_2026-11-22)

## 阶段 0 环境、基线和覆盖清单

| 编号 | 实施方法 | 验收方法与产物 |
|---|---|---|
| 0.1 配环境 | 在 macOS 本地桌面会话执行。使用当前 CPU 架构对应的 Python 虚拟环境和现有项目安装方式。记录 macOS、CPU 架构、GPU、显示器、Python、FFmpeg、Resolve 版本和提交号。 | `import Metal, Quartz, VideoToolbox` 与探针通过，报告包含 Metal device、支持的格式、导入结果。没有可用设备时记录原因，保留 GL 路径。 |
| 0.2 建清单 | 搜索 `moderngl`、`QOpenGLWidget`、`OpenGL.GL`、`.glo`、`GLsync`，逐条归属到入口、上传、处理、呈现、导出或兼容后端。以配置入口、插件查找和运行日志补齐动态分派。 | 每条记录有入口、阶段、预计能力、测试编号、实际后端和回退方式。运行到达的路径优先于静态命中数量。 |
| 0.3 对照工程 | 复制基准脚本和 LUT，以独立路径重建 Topos 工程。在 Resolve 导入基准 DRP 的副本或建立同设置工程，重连用户素材，核对两处裁切。分别保存 clean、单 LUT、DNxHR、含音频、多轨、HDR/Alpha 和插件工程。 | 重建 clean 和单 LUT 后媒体路径正确，400 帧、3840×2160、25/1、16 秒。若当前机器探针结果不同，另建基线记录。 |
| 0.4 采时间与质量 | 按附录 A 运行现有脚本。每条路径单独跑，记录首跑和五次暖跑，交替测试顺序；预览固定同一输出区域和 DPR。新增工作流按对应测试矩阵采原生诊断。 | 报告含逐轮耗时、中位数、帧数、播放掉帧、实际后端、帧搬运、中断与取消结果。先定位差异再变更阈值。 |
| 0.5 固化项目样本 | 为每个新流程保存脱敏的可重建测试配置、实际素材名、轨道时间范围、LUT/插件版本、效果参数和期望。旧工程样本取正常交付副本与早期缺少新字段的副本。 | 另一位开发者可以用同一份媒体重建并得出可比较的证据。保留一次强制 OpenGL 对拍。 |

从仓库根目录执行以下命令。`mktemp` 建立的路径用于本次验收，后续命令使用同一 shell 中的变量。新机器先按安装指南安装 FFmpeg。在仓库根目录用本机架构的 Python 创建环境，运行 `python3 -m venv .venv`，激活后执行 `python -m pip install -e '.[dev]'`。已有可用环境时直接激活。

~~~bash
source .venv/bin/activate
git status --short --branch
git rev-parse HEAD
sw_vers
uname -m
python -V
ffmpeg -version | head -n 1
ffprobe -version | head -n 1
python -c 'import PyQt6, Metal, Quartz, VideoToolbox'
metal_reports=$(mktemp -d /tmp/topos-metal-validation.XXXXXX)
python tools/probe_metal_capability.py \
  --output-json "$metal_reports/capabilities.json" \
  --output-md "$metal_reports/capabilities.md"
rg -n 'QOpenGLWidget|OpenGL[. ]|moderngl|[.]glo\b|GLsync' src apps plugins \
  > "$metal_reports/gl-inventory.txt"
~~~

在 Apple Silicon 和受支持的 Intel/AMD Mac 分别执行。使用支持矩阵和运行时能力判定决定 NV12、P010、渲染目标、EDR、共享同步和设备互操作。用 `ffprobe` 核对媒体编解码标记，用导入后的元数据核对实际纹理格式。 [安装指南](../getstarted/installation.md) · [能力探针](../../tools/probe_metal_capability.py)

## 阶段 1 共享 GPU 帧和生命周期

`GpuDecodedFrame` 已能携带 Metal 平面，`TextureLease.from_resource()` 也能保存原生资源。`GpuPresentFrame` 目前仍以 GL 纹理名和 GL fence 发布。先扩展发布协议并保持既有 GL 消费者兼容，再让原生帧进入视口。涉及的资源仅存在于运行时。 [GPU 帧](../../src/shared/media/gpu_frame.py) · [呈现协议](../../src/shared/gpu/present_frame.py)

| 编号 | 实施方法与现有入口 | 验收与回退 |
|---|---|---|
| 1.1 | 给 `TextureLease.make_present_frame()` 新增原生发布分支。描述符带 `api`、设备身份、像素格式、可见区域、方向、色彩四元组与帧号。租约键包含资源池代次、槽号和递增提交号。保留现有 GL 构造参数与兼容元组。 | 协议回归覆盖重复确认、过期确认和设备变化。旧消费者继续收到原有 GL 描述符。设备或格式不匹配时按现有路径重取本帧并记录原因。 |
| 1.2 | 对接 `GpuDecodedFrame.validate_for_device()`、`CVMetalVideoBridge` 和 `MetalSubmissionScheduler`。提交 GPU 工作前预留有界槽位。驱动回调只将完成结果放入队列，持有资源的线程执行回收。解码帧等待最后一次 GPU 读取结束，呈现纹理还要等显示消费者结束。 | 注入槽满、提交失败、GPU 失败与重复回调。输入、输出分别释放一次。槽满沿用 hold/drop。已录制但未提交的 Metal 命令只在本帧事务中重试或整组丢弃。 [Metal 提交调度](../../src/shared/gpu/metal/submission_scheduler.py) · [CVMetal 导入](../../src/shared/media/macos_vt/cv_metal_bridge.py) |
| 1.3 | 按现有 `set_generation_id()` 和退役资源池语义处理 seek、取消、页面切换、关闭。停止接收旧帧后，丢弃未呈现结果，收齐已发出工作的完成事件，再由生产线程销毁池。设备切换将旧池单独退役。 | 连续拖动与反复开关窗口无旧帧覆盖、无悬挂槽和超预算增长。完成事件超时则暂停该设备的新提交、保留上次画面并报告原因，确认 GPU 停用后才销毁资源。 [现有渲染线程](../../src/features/edit/core/edit_gpu_preview_renderer.py) |

下例可在仓库根目录运行，使用现有 `GpuFrameRelease` 验证两个消费者都结束后只回收一次。生产代码中完成回调设置状态，资源线程负责轮询。

```bash
.venv/bin/python - <<'PY'
from threading import Event
from src.shared.media.gpu_frame import GpuFrameRelease

write_done, display_read_done = Event(), Event()
returned: list[str] = []
lease = GpuFrameRelease(fence=object(), owner_backend="metal",
                        release_callback=lambda: returned.append("return"))

def completed(_: object) -> bool:
    return write_done.is_set() and display_read_done.is_set()

write_done.set()
assert not lease.release_when_fenced(completed)
display_read_done.set()
assert lease.release_when_fenced(completed)
assert lease.release_when_fenced(completed) and returned == ["return"]
PY
.venv/bin/python -m pytest -q tests/media/test_gpu_frame.py \
  tests/edit/test_gpu_present_frame.py tests/edit/test_present_protocol_cleanup.py
```

## 阶段 2 原生 Metal 视口和剪辑预览

当前自动只选择合格单 LUT 画面的 Metal 计算，成片预览仍经 BGRA IOSurface 进入 Program Viewer 的 GL 槽。原生试点先接同一批 BT.709 限幅 NV12 视频及已验证的单 LUT。每次请求继续由时间线调度发起，预览走哪种输出按场景能力决定。 [现有预览契约](../architecture/edit-timeline.md) · [Metal 预览实现](../../src/features/edit/core/metal_timeline_preview.py)

| 编号 | 实施方法与现有入口 | 验收与回退 |
|---|---|---|
| 2.1 | 在 `ProgramViewerWidget.set_frame()` 下接待新增的原生视口适配器。GUI 线程装拆 `NSView` 和 `CAMetalLayer`、发布尺寸与屏幕状态。渲染线程获取 drawable、编码并呈现。复用设备与队列，限制在途 drawable。 | 连续 resize、隐藏、全屏、换屏、关闭时 GUI 无等待。drawable 暂不可用时保留最后成功帧并遵守帧截止时间。该会话能力验证失败时恢复现有 GL Viewer。 [Program Viewer](../../src/features/edit/ui/program_viewer.py) |
| 2.2 | 从 `RealtimePlaybackEngine.request_present()` → `TimelineGpuCompositor.submit_backend_task()` 接入，保持播放、拖动预览与精确 seek 各自的优先级。送入原生视口前再次比对 `generation`、精确拖动令牌和渲染尺寸代次。音频主时钟与丢帧策略继续由引擎统一管理。 | 同一时间点逐帧比对画面和音画同步。拖动结束只接受当前精确请求。新代次拒绝旧帧且仍回收其租约。CPU 快照与不支持场景继续由已有输出路径呈现。 [播放引擎](../../src/features/edit/core/realtime_playback/engine.py) · [合成调度](../../src/features/edit/core/realtime_playback/timeline_gpu_compositor.py) |
| 2.3 | 使用 `ProgramViewer.paintGL()` 的显示矩形与叠加输入。复用字幕、辅助线、变换手柄、错误状态及取色坐标，原生视口绘制独立叠加层。为原生视口单独配色彩空间和 drawable 格式。DPR、刷新率、ICC/HDR 或设备变更时，换新尺寸代次并退役旧缓冲。 | 复核字幕拖拽命中、黑边、裁切和混合。复杂叠加或屏幕模式尚未通过像素和交互对照时回 GL。将 SDR 单一颜色附件作为首版，波形或遮罩需要额外附件时单独验证 MRT 的格式、清理、存储动作以及显存和带宽开销。 |
| 2.4 | 在 Intel 独显和 Apple Silicon 分别验证实际原生 drawable 链路。记录显示帧数、掉帧、首帧和 seek p95、GPU 时间、峰值 GPU 资源占用与每帧搬运量。Intel 额外核对跨设备导入和同步，Apple Silicon 核对统一内存压力。A/B 使用相同素材、片段、质量与刷新率。 | 首先守住每台机器现有 UHD 25 fps 的显示帧数和 seek p95。Metal 设备、共享资源或显示链路不可用时，继续选择已验证的 GL 或 IOSurface 路径。每个平台和场景分别启用自动选择。 |

以下是待新增的呈现适配器接口示意。收到 GPU 已写完的帧后，渲染线程按同一帧期限判断是否接收。seek 与显示配置变化各自更新代次。实时播放的拖动令牌为空。

```python
def handle_ready(frame: NativePresentFrame, port: MetalPresentPort) -> None:
    # 两个类型由本阶段新增。ready 由生产端完成事件发布。
    if not port.matches_request(frame) or port.past_deadline(frame):
        port.retire_on_owner_thread(frame)
        return
    if not port.try_present(frame):
        port.retire_on_owner_thread(frame)
        port.hold_last_frame()
    # true 表示提交成功。读完成后入队确认租约，由实际显示事件统计呈现帧率。
```

下面的共存探针仅验证小窗口中的 GL 与 Metal 色块、重叠、尺寸和呈现节奏。在独立目录运行探针以保留既有报告。探针包含同步等待，实际视口实现应使用完成回调。实际时间线 A/B 还要覆盖现有预览像素测试及后续原生视口的帧号和叠加测试。 [探针源码](../../tools/probe_qt_metal_coexistence.py) · [历史探针输出](../../tests/media/results/qt_metal_coexistence.md)

```bash
metal_qt_probe=$(mktemp -d /tmp/topos-qt-metal-probe.XXXXXX)
mkdir -p "$metal_qt_probe/tools"
cp tools/probe_qt_metal_coexistence.py "$metal_qt_probe/tools/"
git rev-parse HEAD > "$metal_qt_probe/commit.txt"
.venv/bin/python "$metal_qt_probe/tools/probe_qt_metal_coexistence.py"
.venv/bin/python -m pytest -q tests/edit/test_metal_timeline_preview.py \
  tests/edit/test_realtime_engine_hotpath.py tests/edit/test_preview_view_transform.py \
  tests/edit/test_subtitle_drag_preview_throttle.py tests/edit/test_present_frame_integration.py
```

### 原生宿主的接线示例

先用红色 drawable 验证渲染线程与窗口衔接，再把清屏替换成从当前 GPU 帧采样的全屏三角形。以下演示沿用当前探针的 PyObjC API。现有 Qt 主线程创建带 `WA_NativeWindow` 的专用宿主，并在有窗口句柄后调用 `attach_test_layer`。窗口装拆、Retina 的 drawable 尺寸和屏幕变化在 GUI 线程更新。后续 `present_test_color` 由专用呈现队列调用，使用启动时创建的同设备 queue。调用方已取得在途槽位，收到完成回调后归还。每次调用放在该队列的 autorelease pool 内。

~~~python
from ctypes import c_void_p
from typing import Any, Callable

import Metal
import Quartz
import objc
from PyQt6.QtWidgets import QWidget

def attach_test_layer(host: QWidget, device: Any) -> tuple[Any, Any]:
    view = objc.objc_object(c_void_p=c_void_p(int(host.winId())))
    child = Quartz.NSView.alloc().initWithFrame_(view.bounds())
    layer = Quartz.CAMetalLayer.alloc().init()
    layer.setDevice_(device)
    layer.setPixelFormat_(Metal.MTLPixelFormatBGRA8Unorm)
    layer.setFramebufferOnly_(True)
    layer.setAllowsNextDrawableTimeout_(True)
    child.setLayer_(layer)
    child.setWantsLayer_(True)
    view.addSubview_(child)
    scale = host.devicePixelRatioF()
    layer.setContentsScale_(scale)
    layer.setDrawableSize_(
        (max(1, round(host.width() * scale)), max(1, round(host.height() * scale)))
    )
    return child, layer

def present_test_color(
    layer: Any, queue: Any, post_completion: Callable[[int], None]
) -> bool:
    drawable = layer.nextDrawable()
    if drawable is None:
        return False
    cmd = queue.commandBuffer()
    if cmd is None:
        return False
    render_pass = Metal.MTLRenderPassDescriptor.renderPassDescriptor()
    color = render_pass.colorAttachments().objectAtIndexedSubscript_(0)
    color.setTexture_(drawable.texture())
    color.setLoadAction_(Metal.MTLLoadActionClear)
    color.setStoreAction_(Metal.MTLStoreActionStore)
    color.setClearColor_((1.0, 0.0, 0.0, 1.0))
    encoder = cmd.renderCommandEncoderWithDescriptor_(render_pass)
    if encoder is None:
        return False
    encoder.endEncoding()
    cmd.presentDrawable_(drawable)
    cmd.addCompletedHandler_(lambda completed: post_completion(int(completed.status())))
    cmd.commit()
    return True
~~~

`nextDrawable()` 可能等待空闲 drawable。因此只在当前视口可见、持有槽位并且帧请求仍有效时调用，返回 `False` 由调用方归还槽位和输入帧。命令完成和实际显示是两个事件。资源所有者处理完成与失败，播放统计订阅 actual presentation。GUI 路径只传输状态。验证依次用静止色块、视频帧、字幕/手柄、Retina 扩缩、显示器变更和 100 次 seek。 [Apple 的 drawable 行为](https://developer.apple.com/documentation/quartzcore/cametallayer/nextdrawable%28%29?language=objc) · [Qt 的窗口宿主与层叠限制](https://doc.qt.io/qt-6/qwidget.html#createWindowContainer)

### 视口扩展顺序

| 编号 | 实施方法 | 验收与回退 |
|---|---|---|
| 2.5 | Program Viewer 单层稳定后，把同一原生视口适配到 `SourceViewer`。按源媒体时刻取帧。再接 Trim 多画面、共享源播放器、多机位的窗口或网格。每个消费者持有独立 lease。 | 反复切源、拖动、源与时间线切换、双 Viewer 并行。不同尺寸和 DPR 互不串帧，逐项跑实际呈现 A/B。老路径按会话能力选择。 [Source Viewer](../../src/features/edit/ui/source_viewer.py) |
| 2.6 | 扩大剪辑 GPU 计划的覆盖，依次增加多层、变换、裁切、缩放、混合、变速、转场、字幕/图形和嵌套序列。复用 `TimelineGpuCompositor` 的时间线求值和音频主时钟，在计划构建时逐项预检。 | 第一个完整项目使用 H.264、单 LUT、交叉溶解和主混音。然后加入两层 alpha 和字幕，按图逐一与 GL 对拍。混合帧的两侧都遵循现有裁切、色彩和 Alpha 顺序。未通过的计划回退整帧或整会话。 |

## 阶段 3 H.264 导出扩展和 DNxHR HQX

`6249e136` 已有 Metal → NV12 → VideoToolbox H.264 快路径。适用场景为同尺寸、等整数帧率的 BT.709 限幅 8-bit H.264，交付 MP4/MOV，支持最多四个静态串联 Corrector、一级调色与 LUT，以及合格两段的 cross-dissolve。快速出口仍拦截带音频的序列。阶段 0 的单 LUT 数据作为性能起点。带音频、复杂图和 HDR 用各自的媒体与设置复测。 [交付架构](../architecture/deliver-export.md)

| 任务 | 方法与实际入口 | 验收 | 回退 |
| --- | --- | --- | --- |
| 3.1 48 kHz 主混音 | 优先放开 AAC mono/stereo。`TimelineExporter._try_export_metal_h264_lut()` 接入已有的 `expand_sequence_audio_for_render()`、`_audio_export_worker()` 与主混音电平、声道和响度处理。`VideoToolboxGpuExportBridge.add_audio_stream()` 必须在首个视频包进入容器前调用。渲染线程独占 GPU 资源，音频 worker 产 PCM。复用桥的 `write_audio_samples()` 和串行封装边界，扩展累计样本 PTS。[时间线导出](../../src/features/edit/core/timeline_export.py) · [VT 桥](../../src/shared/media/macos_vt/gpu_export_bridge.py) | 检查静音段、源音轨偏移、剪切点、长片同步与 AAC priming。对照常规导出的 PCM 长度和声道排序。保留帧数、调色、profile、目标码率与色彩标签。 | 多轨 stems、非 48 kHz、特殊声道布局或声明失败保留常规路径。若同容器调度暂未通过，先输出临时 H.264 和主混音，再显式映射轨道并 `-c:v copy` 封装。 |
| 3.2 时间戳与提交 | `VideoToolboxPacketMuxer.write_packet()` 有现成压缩包入口。扩展时把 CoreMedia PTS、DTS、duration 分别按自身 timescale 换算到统一时基。跨轨交错按 DTS 调度，保留 B 帧 PTS，音频按逐样本计数打时间戳。扩展分数帧率时修改桥接 fps 与 Qt 计划的精确有理数时基，以 24000/1001 和 30000/1001 的 10 分钟合成样本检查末端漂移。使用桥现有 `expected_frames`、`should_cancel`、`close()` 和 `abort()`，双阶段封装时由最终容器独占发布权限。[VT 压缩与混流](../../src/shared/media/macos_vt/vt_compression.py) | 检查逐轨 DTS、视频展示序列、AAC 延迟与尾部样本。分别在首帧、渲染、排空、封装期取消。旧目标保留，临时文件与 surface 租约释放。 | 编码、音轨或校验失败后取消对应临时作业，按完整区间重走常规路径。取消直接终止作业。禁止发布漏音轨的视频。 |
| 3.3 复杂场景和 HDR | 用原生快路径现有白名单逐项加能力。优先做多层、字幕和遮罩的合成等价测试，再开放对应配置。HDR 单独验证色域、传递函数、矩阵、峰值和静态元数据，使用已有 P010/HEVC Main10 桥接做独立试验。[Metal 导出渲染器](../../src/features/edit/core/timeline_metal_export_renderer.py) | 搭配裁切、关键帧、插件、字幕和 HDR 测片，逐帧与常规导出比较，并用探针核对像素格式和标签。 | 每项语义未验证前继续走已有支持路径。保留请求的位深和 HDR 交付规格。 |
| 3.4 DNxHR HQX | `_try_copy_dnxhr_hqx()` 仅对同源、逐帧对齐、无音频及无像素改动复用包。调色后用已验证的 OpenGL 10-bit 4:2:2 平面 → PyAV `dnxhd`/`dnxhr_hqx`。用分阶段遥测和采样器拆开解码、GPU 渲染/回读、队列等待、编码及落盘，实测线程预算和有界单帧预取。Metal 平面等价通过后再接入。[常规渲染器](../../src/features/edit/core/timeline_gpu_export_renderer.py) · [编码器](../../src/shared/export/video_encoder.py) · [遥测](../../src/shared/export/export_stage_telemetry.py) | 交付 `yuv422p10le`，检查画面、色彩标签、切点与帧数。智能复用检查包哈希。报告 wall time、p50/p95 和 CPU 占用，区分纯 FFmpeg 计算基线与交付写盘。 | 优化不达标时保留现有 OpenGL 平面或常规 HQX。维持 10-bit HQX。 |

音频写入参考。`layout` 沿用声明流时解析的布局，`pcm` 来自同一导出区间的 48 kHz float32 平面主混音。将以下做法接入 `VideoToolboxPacketMuxer.write_audio_samples()`，沿用其锁与唯一封装持有方。首块 `start_sample` 取相对导出起点的样本偏移，用返回值推进后续块。现有 `close()` 负责排空 AAC 编码器，取消时由 `abort()` 清理。

```python
from fractions import Fraction

import av
import numpy as np


def mux_aac_pcm(container: av.container.OutputContainer,
                stream: av.AudioStream, pcm: np.ndarray, layout: str,
                start_sample: int = 0) -> int:
    for offset in range(0, pcm.shape[1], 1024):
        block = np.ascontiguousarray(pcm[:, offset:offset + 1024], dtype=np.float32)
        frame = av.AudioFrame.from_ndarray(block, format="fltp", layout=layout)
        frame.sample_rate = 48_000
        frame.time_base = Fraction(1, 48_000)
        frame.pts = start_sample + offset
        for packet in stream.encode(frame):
            container.mux(packet)
    return start_sample + pcm.shape[1]
```

两阶段试验用已经对齐时间线起点的 48 kHz 主混音和 video-only 文件。AAC 码率按交付设置传入。应用中生成唯一的同目录临时路径，核验视频和音频的起止时间、帧数与声道后，在取消检查内原子发布。禁止用 `-shortest` 裁掉超长音轨来掩盖对齐问题。

```bash
video='path/to/video-only.mp4'
master='path/to/master-48k.wav'
staged='path/to/.delivery-audio-stage.mp4'
ffmpeg -hide_banner -nostdin -n -i "$video" -i "$master" \
  -map 0:v:0 -map 1:a:0 -c:v copy -c:a aac -b:a 256k "$staged"
```

仓库根目录执行。先核查探针结果，再在同机、同参数下重复跑分。`output` 改为实际交付文件。两条 DNxHR 命令分别给出解码与解码加编码的诊断，需用采样和交付阶段统计定位实际瓶颈。完整导出记录写盘和 GPU 数据。针对编码尝试用已符号化的 FFmpeg/PyAV 构建分析 FDCT、量化与线程负载，每次只改一种优化。修改 SIMD 实现前，用标量参考建立逐块一致性、码流兼容性和性能用例。

```bash
output='path/to/delivery.mp4'
ffprobe -v error -count_frames -of json \
  -show_entries 'stream=index,codec_type,codec_name,profile,pix_fmt,width,height,avg_frame_rate,time_base,start_time,duration,nb_read_frames,color_range,color_space,color_transfer,color_primaries,sample_rate,channels,channel_layout' "$output"
ffprobe -v error -show_packets -of json \
  -show_entries 'packet=stream_index,pts,dts,duration,flags:packet_side_data=side_data_type,skip_samples,discard_padding' "$output"

src='tests/test_videos/4K_DNxHR HQX 10bit.mov'
ffmpeg -hide_banner -benchmark -threads 4 -i "$src" \
  -map 0:v:0 -an -frames:v 400 -f null -
ffmpeg -hide_banner -benchmark -threads 4 -i "$src" \
  -map 0:v:0 -an -frames:v 400 -c:v dnxhd -profile:v dnxhr_hqx \
  -pix_fmt yuv422p10le -threads:v 16 -f null -

.venv/bin/python -m pytest -q tests/edit/test_dnxhr_packet_copy.py
.venv/bin/python -m pytest -q tests/media/test_macos_vt_compression.py -k without_hardware
.venv/bin/python -m pytest -q tests/media/test_macos_vt_compression.py \
  -k test_native_videotoolbox_bridge_muxes_audio_before_video_close
```

## 阶段 4 调色图、Color Viewer 和示波器

先让单个工程的预览与导出消费同一份调色图，再增加节点和格式。当前 `ColorGraphMetalExecutor.execute_plan()` 接收 NumPy RGBA16F，适合作为已有算法和测试参考。生产路径需要接收阶段 1 的 GPU 帧并发布带租约的 GPU 结果。`msl_composition.py` 负责已迁移的内置效果生成，可复用其参数 manifest 和 MSL 片段。 [图执行参考](../../src/features/color/core/color_graph_metal.py) · [MSL 生成器](../../src/features/color/core/msl_composition.py)

| 编号 | 实施方法 | 难点和验收 |
|---|---|---|
| 4.1 共享图入口 | 沿用 `color_graph_service` 的工程快照、`color_graph_preflight` 的能力判定与 `color_graph_compiler` 的计划。按媒体、帧时刻、效果修订、工作色域与目标格式生成需求。连接 `async_renderer.py`、剪辑渲染和交付。独立 `image_engine.py` 入口按相同契约接入。 | 编辑、拖动和导出对同一帧得到相同图顺序、节点 ID、参数和旁路状态。以断边、未知节点、取消、旧工程样本验证预检与后端回退。 |
| 4.2 纹理原生执行 | 把 LUT、Primary、曲线、HSL、混色器和 Warper 按现有图计划逐个接到 Metal 纹理。为相邻同尺寸 pass 重用池，为多输入节点持有独立租约。新参数绑定沿用 `composition_uniforms.py`，把 LUT、曲线、色域变换纹理按资源版本缓存。 | 每个节点含无操作、典型、极值及多实例组合。检查 LUT 边界、节点顺序、负值、超过 1 的高光和 NaN。低精度输入只在契约规定的边界量化。 |
| 4.3 遮罩和复杂节点 | 增加 qualifier、窗口、跟踪遮罩、Alpha、并/层混合和关键帧。每种混合遵循当前图的颜色空间和 Alpha 关联规则。关键帧从同一个帧时间求值。降噪、时域分析和 Normalize 分开接入，有状态节点明确前后帧依赖。 | 带半透明边缘、反向遮罩、羽化、关键帧跨剪切、并行图和倒放样本。切片、seek 或并行导出前重置时域历史，逐帧比对整段导出和分段导出。 |
| 4.4 色彩管理和 HDR | 将输入 YUV 矩阵、range、色度采样位置、transfer、色域、可见区交给帧描述。复用现有 OCIO/ACES 配置和展示变换。验证 NV12 SDR 后再验证 P010 及 HDR 中间结果。Color Viewer 的 EDR 配合屏幕变更重新配置显示端。 | 同一线性源在不同显示器分别验证显示变换。比较 HDR 测试图的线性值、Alpha、码值、导出标记和 SDR 映射。显示变换只在对应输出边界施加一次。 |
| 4.5 Color Viewer 和示波器 | 用阶段 2 的宿主替换 `gl_viewport.py` 的原生上屏实现。沿用参考图对比、分屏、缩放、平移和安全框。逐项迁 `gpu_scope_engine.py` 的采样及显示统计、`gpu_scope_widget.py` 的呈现。 | 波形、Parade、矢量、直方图、CIE 等逐项用已知测试图对拍。播放时验证示波器设置和视口互不阻塞，GPU 数据留在设备上。 |
| 4.6 静帧和辅助入口 | 迁节点缩略图、画廊、参考图对比、LUT 预览、CFA 解拜耳及调色降噪导出桥。缓存记录格式、后端和工作色彩语义。用户截图和显式像素比对走单独的异步读回请求。 | 播放、切页、清缓存、节点撤销和更换 LUT 后无旧图。缩略图与静帧保存正确，计数能区分显式读回和播放读回。 |

**参考实现 4A · Metal 纹理和遮罩的最小接入**

将该示例作为 GPU 接线测试，输入输出为同尺寸的浮点 RGBA，遮罩为同尺寸单通道。宿主先验证尺寸、像素格式及纹理设备，再按输出尺寸 dispatch。乘法作用在线性空间，原 Alpha 原样写出。生产 Corrector 用已验证的调色函数替换示例中的 gain 算子。

~~~metal
#include <metal_stdlib>
using namespace metal;

kernel void test_linear_gain_with_matte(
    texture2d<float, access::read> source [[texture(0)]],
    texture2d<float, access::read> matte [[texture(1)]],
    texture2d<float, access::write> output [[texture(2)]],
    constant float& gain [[buffer(0)]],
    uint2 p [[thread_position_in_grid]]
) {
    if (p.x >= output.get_width() || p.y >= output.get_height()) {
        return;
    }
    const float4 input = source.read(p);
    const float coverage = clamp(matte.read(p).r, 0.0f, 1.0f);
    const float3 rgb = mix(input.rgb, input.rgb * gain, coverage);
    output.write(float4(rgb, input.a), p);
}
~~~

用三个像素的遮罩 `[0.0, 0.5, 1.0]`、相同输入 `(-0.25, 0.5, 2.0, 0.5)` 和 `gain = 2.0` 验证。预期 RGB 依次为 `(-0.25, 0.5, 2.0)`、`(-0.375, 0.75, 3.0)`、`(-0.5, 1.0, 4.0)`，Alpha 均为 `0.5`。再验证尺寸不整除线程组、两次提交乱序完成和透传旁路。色管理在输入/输出契约处通过，其测试与 kernel 单独运行。

将上面的 kernel 保存成当前验证目录的 `gain_with_matte.metal`，以绝对路径作为 `metal_kernel` 变量。运行以下像素用例。测试使用的同步提交和读回只出现于验证入口。

~~~bash
metal_kernel="$metal_reports/gain_with_matte.metal"
python - "$metal_kernel" <<'PY'
from pathlib import Path
import sys

import Metal
import numpy as np
from src.shared.gpu.metal.context import MetalComputeContext

ctx = MetalComputeContext()
kernel = ctx.compile_kernel("test_linear_gain_with_matte", Path(sys.argv[1]).read_text())
rgba = np.tile(np.array([-0.25, 0.5, 2.0, 0.5], dtype=np.float16), (1, 3, 1))
mask = np.array([[0.0, 0.5, 1.0]], dtype=np.float16)
source = ctx.create_texture_from_numpy(rgba, Metal.MTLPixelFormatRGBA16Float, 3, 1)
matte = ctx.create_texture_from_numpy(mask, Metal.MTLPixelFormatR16Float, 3, 1)
output = ctx.create_texture_2d(Metal.MTLPixelFormatRGBA16Float, 3, 1, shader_write=True)
gain = ctx.create_buffer_from_numpy(np.array([2.0], dtype=np.float32))
ctx.dispatch(kernel, buffers={0: gain}, textures={0: source, 1: matte, 2: output},
             threads=(3, 1, 1))
ctx.submit(wait=True)
got = ctx.read_texture(output, 12, 1, dtype=np.float16, region_width=3).reshape(3, 4)
np.testing.assert_array_equal(got, np.array([
    [-0.25, 0.5, 2.0, 0.5], [-0.375, 0.75, 3.0, 0.5], [-0.5, 1.0, 4.0, 0.5],
]))
PY
~~~

~~~bash
python -m pytest -q \
  tests/shared/gpu/metal/test_color_graph_metal.py \
  tests/color/test_metal_composition_*.py \
  tests/color/test_composition_uniform_manifest.py \
  tests/color/test_color_transform_provider_parity.py
~~~

**回退单元**为一次完整图或会话刷新。产生不支持诊断时，应保有同一帧时刻、工程修订和节点参数。应用以可见诊断选择现有兼容图执行；禁止把失败节点的输出替换为原图继续产出。默认启用需要覆盖老工程对拍和视口真机验收。

## 阶段 5 二维合成、时间类效果和 3D

以 `CompositeGraphRenderService` 为调度边界，以节点注册、图预检和 shared frame 为稳定接口。Standalone Composite、Edit 的 Comp Clip、Deliver 走同一份图和采样规则。现有 CPU 和 OpenGL 路径提供对拍，其 `MetalComputeBridge` 用于理解早期搬运成本。原生链路优先把相邻节点的工作纹理留在同一设备。 [合成图服务](../../src/features/composite/render/composite_graph_render_service.py) · [合成架构](../architecture/composite.md)

| 编号 | 实施方法 | 难点和验收 |
|---|---|---|
| 5.1 跑通最小 2D 链 | 通过 `graph_preflight.py` 判定能力，按 Input → Transform → Merge → Output 构建 Metal pass。沿用 `node_engine.py` 图、端口和参数。把 `CompositeGpuFrameHandle` 的发布接到阶段 1 的租约与完成语义。 | 用色块与棋盘素材做翻转、亚像素缩放、裁切、空画布、三个 Alpha 值。验证线性光、边缘采样、直通节点、透明底与 Comp Clip。 |
| 5.2 矩形、遮罩和效果 | 迁裁切、混合模式、Premultiply、Keyer、Matte、模糊、形态学、Glow、Light Wrap、文本图层、PSD 合成和效果目录中的常用着色器。每个节点登记输入格式、ROI、端口和预览/导出能力。 | 每个输出端口对拍。覆盖 straight/premultiplied、彩色透明边缘、不同尺寸、多输入节点，以及 alpha=0 时 RGB 处理。保留 PSD sRGB 混合到线性工作空间的既有规则。 |
| 5.3 Tile 与缓存 | 为卷积和阴影推导 halo，按全图坐标反推每个输入 ROI，边缘采样遵循效果现有规格。输出 tile 裁回中心，缓存键包含工程修订、帧、节点端口、尺寸和色彩描述。按字节计量 Metal 资源，纹理回收等待 GPU 完成。 | 同一 8K 及仓库支持的最高尺寸工作负载，完整帧、分块、多 tile 尺寸对拍，检查接缝。多节点验证 halo 传播。缓存命中与冷渲染逐像素对照。 |
| 5.4 历史与多帧 | 对重定时、光流、跟踪、粒子和依赖上一帧的节点明确历史跨度、确定性种子、起点与重置边界。关键帧仍由当前求值器按指定时间采样。 | 正放、倒放、seek、跨剪切、单帧重试及分段导出；对相同输入序列验证种子与历史一致。使用专门场景采样真实播放帧。 |
| 5.5 3D 与 AOV | 在 `render3d/` 迁网格/材质、相机、深度、灯光、阴影、骨骼、操控器和多渲染目标。先用基本网格确认矩阵、裁剪空间和深度约定，再加透明排序、多相机、粒子、法线/运动向量、深度与其他 AOV。 | 固定相机阵列和黄金图。颜色可按阈值，ID/通道映射用精确比较，深度、法线和速度用各自域比较。彻底验证遮挡、正反面、选取、Retina 操控器、深度合成和 Cryptomatte。 |
| 5.6 输出和资源池 | 有界调度预览和后台输出。迁 `gpu_resource_pool.py` 中实际使用的纹理形态。将 `composite_export_renderer.py` 多 pass、图像序列、AOV 和无头输出接到同一 GPU 计划。设备或代际不匹配按已定义传输策略处理。 | 同一合成在独立页、时间线、导出取相同帧对拍。压测多窗口、大资源缓存、取消与错误，GPU 句柄和后台任务均回到空闲上限。 |

**参考实现 5A · 有限半径效果的 tile halo**

以下是同尺寸、无旋转的局部滤波所用 ROI 示例。接入点是图计划对每个输入的 ROI 推导。矩形采用半开区间。对多 pass 需要沿图递归传播半径。Transform 要反投影输入坐标并计入插值范围，全局统计效果要单独做完整输入分析。

~~~python
Rect = tuple[int, int, int, int]

def input_roi(
    tile: Rect, radius: int, width: int, height: int
) -> tuple[Rect, Rect]:
    x0, y0, x1, y1 = tile
    if radius < 0 or not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError("invalid tile bounds or radius")
    left = max(0, x0 - radius)
    top = max(0, y0 - radius)
    right = min(width, x1 + radius)
    bottom = min(height, y1 + radius)
    expanded = (left, top, right, bottom)
    crop = (x0 - left, y0 - top, x1 - left, y1 - top)
    return expanded, crop

assert input_roi((16, 32, 32, 48), 2, 64, 64) == (
    (14, 30, 34, 50), (2, 2, 18, 18)
)
assert input_roi((0, 0, 16, 16), 2, 64, 64) == (
    (0, 0, 18, 18), (0, 0, 16, 16)
)
~~~

用 `input_roi` 扩张区域渲染，再按 `crop` 写入输出的 `tile`。取相同画面切成 17×19 和 64×64 的 tile，比较每条接缝和完整像素。Alpha 的最小语义例是在线性、预乘约定下，半透明红 `(0.5, 0, 0, 0.5)` 覆盖不透明蓝 `(0, 0, 1, 1)`，期望 `(0.5, 0, 0.5, 1)`。以现有节点定义的颜色空间处理特殊混合。

~~~bash
python -m pytest -q \
  tests/composite/test_graph_preflight.py \
  tests/composite/test_composite_graph_render_service_contract.py \
  tests/composite/test_compute_shader_manager_metal.py \
  tests/composite/test_cache_strategy.py \
  tests/composite/test_scene3d_motion_aov.py \
  tests/composite/test_cryptomatte_select.py \
  tests/composite/test_deep_compositing.py \
  tests/edit/test_comp_clip_timeline_export.py
~~~

**回退单元**为完整合成子图或渲染请求。启用兼容图前释放上一代未提交资源。时域图按同一输入起点重建。高级节点分批启用，缺少实现的节点应显示 ID、类型和当前图路径。

## 阶段 6 着色器插件和内置效果

保留插件 ID、参数名、默认值、版本与项目序列化，通过既有 `EffectSpec`、`EffectCatalog`、`EffectCapability` 管理能力。首先迁宿主自带高频效果和可重建样本，然后扩大第三方覆盖。`.tai` 按 ONNX Runtime provider 验证，Python 插件按宿主和脚本服务验证。只有它们调用图像帧或图形设备的边界需要采用共享帧契约。 [插件开发指南](../PLUGIN_DEV_GUIDE.md) · [能力 API](../../src/features/shared/effects/effect_capability.py)

| 编号 | 实施方法 | 验收方法 |
|---|---|---|
| 6.1 建兼容矩阵 | 枚举仓库效果、插件加载目录、预设、转场、节点插件及旧工程 ID。记录各自入口、参数与版本、图形/计算后端、HDR、Alpha、确定性、仅预览或可导出。 | 每个 ID 有唯一登记和一个命名样本。旧 `.tglsl` 无后端声明时继续按 OpenGL 能力解析。 |
| 6.2 内置效果 | 优先复用已验证的 MSL 片段和参数布局。每个效果实现原生着色器或由同一受控模板生成，输入纹理、sampler、uniform、输出格式显式绑定。 | 值域端点、多个实例并存、纹理和 LUT、曲线、不同大小、HDR/Alpha、随机种子和导出适用范围逐项对照。开启 Metal 之前保存两端证据。 |
| 6.3 可译 `.tglsl` | 先限定纯像素算子、标量/向量参数与受控纹理采样的子集。用已解析的 `PluginBinding` 和 manifest 生成完整 shader。可选择受控 AST 或以固定版本的 glslang 与 SPIRV-Cross 验证转译原型。显式绑定纹理与 sampler，测试坐标翻转、精度、采样边界和结构对齐。 | 两个真实插件先跑通。循环、宏、扩展、深度、多个入口或不支持的调用给出明确诊断。无通用翻译能力时保留该 ID 的兼容路径。实际运行结果通过方可登记 `metal`。 |
| 6.4 高级插件 | 为复杂 GPU 插件提供显式 MSL 实现，将源码指纹、宿主 ABI、编译参数、像素格式、设备能力和插件版本纳入缓存键。编译在适配工作线程完成，注册和热重载按新修订提交。 | 冷热编译、旧版本、热更、编译错误、超时和参数错配覆盖。并发调用两个效果时参数独立，旧编译结果离场，UI 仍可操作。 |
| 6.5 诊断与发布 | 预览与导出分别走现有能力预检。只有兼容链路完整可用才回退，必要时在元素上标示原因。发行包验证所有声明支持的插件，无可用实现时告知用户哪个效果需要升级。 | 用老工程打开→编辑→保存→重开→导出，并对比输出、插件 ID、参数和未知字段。禁用、删除或损坏插件时错误能定位，导出未生成省略效果的文件。 |

**参考实现 6A · 用现有 API 验证效果能力选择**

从仓库根目录运行。下面的 ID 用于说明宿主如何探测，正式接入时通过 Catalog 获取实际能力，并在计划变更时同时检查预览与交付能力。表明 Metal 支持前要先通过效果样本。

~~~python
from src.features.shared.effects.effect_capability import (
    EffectCapability,
    check_backend_support,
)

legacy = EffectCapability(
    gpu_preview=True, cpu_preview=False, gpu_export=True, cpu_export=False
)
assert check_backend_support("example.effect", legacy, graphics="metal") is not None
assert check_backend_support("example.effect", legacy, graphics="opengl") is None

dual = EffectCapability(
    gpu_preview=True,
    cpu_preview=False,
    gpu_export=True,
    cpu_export=False,
    graphics_backends=frozenset({"opengl", "metal"}),
)
assert check_backend_support("example.effect", dual, graphics="metal") is None
~~~

**参考实现 6B · 实现一个受限着色器算子**

从已有插件元数据提取 `exposure`，作为 manifest 的 float 参数。以下是线性空间曝光的最小函数对。Metal 生成器将插件参数绑定到已有组合参数缓冲，再以明确参数调用 helper。一个插件有两个实例时，两次调用分别传各自的绑定值。

~~~glsl
// --- Topos Plugin Metadata ---
// @name Migration Exposure Sample
// @input_space scene-linear
// @output_space scene-linear
// @param slider exposure "Exposure" 0.0 -5.0 5.0
// -----------------------------
vec3 process(vec3 color, vec2 uv) {
    return color * exp2(exposure);
}
~~~

~~~metal
#include <metal_stdlib>
using namespace metal;

inline float3 migration_exposure(float3 linear_rgb, float exposure) {
    return linear_rgb * exp2(exposure);
}
~~~

校验步骤为曝光 0 的透传、曝光 -1 和 +1 的逐像素倍率、负值和超白、同图两个实例以及保存后重开。GLSL 宿主可按现有插件方式运行左侧样本。把右侧函数登记到新增 MSL 效果注册后再声明 Metal 能力。宿主保持 Alpha，并在图像色彩契约允许的阶段执行。

对着色器转译原型，保留由宿主生成的测试 shader 源、编译器版本、反射绑定与对拍 JSON。源语言按插件契约解析，纹理和 sampler 明确映射到 MSL 插槽。测试诊断包含插件 ID、版本、阶段、后端与精简编译消息。 [Khronos SPIRV-Cross](https://github.com/KhronosGroup/SPIRV-Cross/blob/main/README.md)

~~~bash
python -m pytest -q \
  tests/shared/test_effect_spec.py \
  tests/shared/test_effect_catalog_contract.py \
  tests/color/test_plugin_manifest_binding.py \
  tests/edit/test_effect_catalog_capabilities.py \
  tests/edit/test_effect_capability_diagnostics.py \
  tests/edit/test_plugin_manager_paths.py \
  tests/edit/test_timeline_export_effects.py
~~~

## 阶段 7 辅助渲染、无头入口和 OpenGL 依赖清理

使用阶段 0 的清单逐条确认真实调用。每项登记 Metal、显式兼容、非图形或退役四种状态，记录证据和适用机器。先迁入口与调用方，再清理生产路径的裸纹理名、GL context 共享和初始化依赖。

| 编号 | 使用面 | 实施方法 | 验收方法 |
|---|---|---|---|
| 7.1 媒体播放与多机位 | Source、素材池、文件页、共享播放器、Trim 多画面和多机位 | 在 `gl_render_worker.py` 和预览入口共用 GPU 帧描述、同步与视口宿主。检测码率、像素格式和显示尺寸，用有界队列维持拖动响应。 | 切换 H.264、DNxHR、旋转素材、4:2:2、代理、变速、多机位和多个窗口。设备、帧号与色彩正确。 [共享播放线程](../../src/shared/playback/gl_render_worker.py) |
| 7.2 全应用媒体小图 | 画廊、缩略图、章节小图、缓存、CFA、代理和预渲染 | 在领域服务调用共享帧。静帧读回放专用任务队列，GPU 结果与磁盘缓存明确分界。代理和预渲染按媒体格式选 codec。 | 触发旧缓存命中、失效、改 LUT、换素材和大量小图，验证播放节奏、色彩和资源上限。 [缩略图](../../src/app/controllers/node_thumbnail_evaluator.py) · [CFA](../../src/shared/video/cfa_gpu_debayer.py) |
| 7.3 外部显示与抓帧 | DeckLink、外接监看、镜像、截图、AI 抓帧和脚本采样 | 显示帧按拥有方完成事件发布，借用共享平面或做有界异步 GPU→CPU 搬运。登记监看的尺寸、range、色度采样和帧率。 | 接拔、改分辨率、帧号、色条、音画同步和截图请求独立生效。 [监看入口](../../src/app/controllers/file_controller.py) · [抓帧入口](../../src/app/controllers/ai_edit_host_bridge_impl.py) |
| 7.4 其他渲染模式 | 调色批处理、合成无头输出、后台队列、教学动画、`.toos`/TOPOS 和 CLI | 对独立渲染器做能力探测。教学动画在进入视频帧合成或交付处接 GPU 帧。TOPOS 编解码沿用可用 Metal/CPU 实现和存盘格式，验证帧适配。 | 覆盖无 GUI、前台与后台并行、独立交付产物、无预览或无 GPU 的兼容配置。 [独立渲染器](../../src/shared/video/video_async_renderer.py) · [动画基准](../../src/features/teaching_animation/render/benchmark.py) · [交付控制器](../../src/app/controllers/deliver_controller.py) |
| 7.5 收拢 GL 所有权 | `src/main.py` 初始化、PyQt 窗口、裸 `.glo`、GL fence 和资源工厂 | GL 创建和共享上下文设置集中到兼容会话或兼容启动模式。用创建前能力探测选择视口，运行时回退退役旧路径。若 Qt 属性需要应用创建前设置，记录哪些模式需重启。 | 冷启动、典型默认工程、导出和退出过程，Topos 创建的 GL 上下文和线程为零。显式兼容模式通过 Windows/Linux 与 macOS 对拍。 [应用入口](../../src/main.py) |

在 `GraphicsBackend`、资源工厂和视口宿主计数，每次兼容后端调用记下域、原因与会话。用测试注入拒绝 GL 工厂，并运行典型 macOS 工作流。对需要兼容的样本运行预期回退用例。 [图形接口](../../src/shared/gpu/graphics_backend.py)

## 阶段 8 验收、打包和默认切换

| 编号 | 实施方法 | 完成要求 |
|---|---|---|
| 8.1 能力矩阵 | 每个场景跑默认、显式 Metal、显式 OpenGL 和不可用能力注入。Apple Silicon 与仍受支持的 Intel/AMD 分开登记，Windows/Linux 按仓库 CI 运行。 | 每行有主后端、回退预期、测试结果、日志、机器配置和日期。GPU 测试全部跳过的机器只提供逻辑证据。 |
| 8.2 画质和工程兼容 | 比较编码前的完整帧与最终成片。按输入域设阈值，采负值、亮部、边缘、透明度、裁切和切点。旧工程副本存盘重开后再导出。 | 端口、轨道、帧数、PTS、节点 ID 和输出规格精确符合预期。颜色与有损编码按对应容差通过，工程未知字段和插件配置可读回。 |
| 8.3 性能和稳定性 | 一次冷启动、五次暖跑，记录同机同设置 A/B。增加 30 分钟 UHD 播放、切页和导出并发测试，观察 CPU/GPU 时序、读回、在途租约与内存压力。 | 样例达到下表目标。取消与关闭后活动任务和租约回到空闲水平。缓存稳定在配置预算内。 |
| 8.4 安装包 | 从 `packaging/pyinstaller/` 构建各产品。运行时 MSL 检查框架加载、冷编译和资源。采用编译产物时核对架构切片、最低系统、签名与公证。 | 干净账户或独立机器完成安装、首次启动、打开项目、加载插件、GPU 预览和离线导出。安装包无本机绝对路径依赖。 [打包入口](../../packaging/pyinstaller/topos_studio.spec) |
| 8.5 分批启用 | 先显式开启，再对验收通过的场景启用默认。回退以当前图或完整导出任务为单位，记录每次默认切换和兼容条件。 | 模拟设备、格式、shader、drawable 或 VT 失败，诊断可见。兼容路径交付合格。已取消任务无新文件，原目标不被意外覆盖。 |
| 8.6 维护与退役 | 按平台、功能和实际执行路径处理反馈。发现回归先关闭对应能力，再修正、复测和恢复。验证稳定后移除销账的中转调用点。 | 回退开关有可复现测试，旧工程和跨平台通过既有门禁，后续新增能力登记兼容性和帧生命周期。 |

### 首批性能目标和判定口径

下列为拟采用的上线目标。先在每台机器保存阶段 0 的基线，变更目标时记录场景和依据。编解码比对固定画质、码率策略、Alpha/HDR 与格式需求。

| 工作流 | 判定口径 | 目标 |
|---|---|---|
| UHD25 H.264 单 LUT | 从提交任务到文件关闭的墙钟中位数，固定 400 帧、50 Mbit/s VBR 目标、同 LUT、标签和 profile，同时报告实际码率。 | 同机守住已测约 7.06 秒的水平，较复跑基线超过 5% 时排查。Resolve 耗时比 ≤1.5。 |
| UHD25 DNxHR HQX | 有调色、10-bit 4:2:2 交付。包复用轻剪辑单独记账。 | 同机目标为 Resolve 耗时 ≤1.5 倍，当前约 15.31 秒。CPU 编码主导时单列性能缺口与归因。 |
| UHD25 时间线播放 | 分开计数请求、渲染、提交呈现、真实呈现。以区间唯一期望帧为分母。 | 400 帧轻剪辑拟达到至少 396 帧实际呈现。复杂工程守住该设备基线。播放期同步像素读回为零。 |
| 拖动与切页 | 至少 100 次连续和随机 seek，记录用户动作到正确帧显示的 p50/p95。包含关键帧跨越。 | p95 不高于相同场景基线，旧帧覆盖次数为零。 |
| 画质 | 编码前完整帧与有损成片分开比，记录 MAE、P99、最大差异与切点样本。 | 单 LUT 固定对照片的采样帧最大 MAE 以已有约 1.45 个 RGB8 码值为量级。HDR、Alpha、边缘和其他算子用其像素契约与测试阈值。 |
| 资源与稳定性 | 记录在途分配、归还次数、资源池预算和压力测试故障。 | 取消、关窗和切页后未归还 lease 为零，跑满缓存后资源稳定，故障后新会话恢复。 |

解读回退原因时区分场景不支持、耗时超限、暂时缺 drawable、shader 错误和设备失效。暂时缺帧沿用播放主时钟维持有效画面。持续退化以会话为单位切换。导出按完整区间重跑并验证后发布。安装包的默认与兼容模式均保留各自启动记录。

## 附录 A 可复制的 Topos 与 Resolve 对照

以下使用已有素材，输出进新目录。脚本依据位置定位仓库，因此把副本放在 `bench_out` 下且保持相同深度。全部命令从仓库根目录执行。

~~~bash
source .venv/bin/activate
metal_ab=$(mktemp -d "$PWD/bench_out/metal-ab.XXXXXX")
printf '*\n' > "$metal_ab/.gitignore"
metal_fixture="$PWD/bench_out/resolve_vs_topos_2026-11-22"
cp "$metal_fixture"/{topos_bench.py,verify_exports.py,resolve_bench.py,Topos_Resolve_AB_33.cube} "$metal_ab/"
test -f tests/test_videos/4K_H264.mov
test -f "tests/test_videos/4K_DNxHR HQX 10bit.mov"
export TOPOS_CONFIG_PATH="$metal_ab/topos_runtime/config.json"

python "$metal_ab/topos_bench.py" build
TOPOS_BACKEND_GRAPHICS=opengl TOPOS_EXPORT_NATIVE_VT=0 \
  python "$metal_ab/topos_bench.py" export --source h264 --codec h264 --graded --repeat 101
TOPOS_BACKEND_GRAPHICS=metal \
  python "$metal_ab/topos_bench.py" export --source h264 --codec h264 --graded --repeat 201
TOPOS_BACKEND_GRAPHICS=opengl \
  python "$metal_ab/topos_bench.py" export --source dnxhr --codec dnxhr --graded --repeat 301

python "$metal_ab/verify_exports.py" \
  "$metal_ab/Topos_h264_LUT_to_h264_101.mp4" \
  "$metal_ab/Topos_h264_LUT_to_h264_201.mp4" \
  "$metal_ab/Topos_dnxhr_LUT_to_dnxhr_301.mov"
python - "$metal_ab/export_verification.json" <<'PY'
import json
import sys
from pathlib import Path

rows = json.loads(Path(sys.argv[1]).read_text())
assert len(rows) == 3, rows
assert all(row["format_ok"] for row in rows), rows
PY
~~~

`--repeat` 是输出标签，每次调用执行一遍。新增轮次用不同编号保留成片，交替 GL 和 Metal 起始顺序。核对 `topos_exports.jsonl` 与应用日志。合格单 LUT 要走原生桥，画面读回为零。脚本自动检查帧数、编码器、像素格式、尺寸、帧率与无音频。颜色、画质和编码设置另与本轮报告核对。

在 Resolve 测试库中导入 `Resolve_UHD25_AB.drp` 副本，重连媒体，核对 `H264 - LUT`、`DNXHR - LUT`、裁切和调色。通过界面设置独立输出目录并交付，或在默认安装位置并启用外部脚本的机器上运行以下命令。脚本按工程名选择已有测试工程。

~~~bash
metal_resolve_modules='/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules'
metal_resolve_lib='/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so'
test -f "$metal_resolve_modules/DaVinciResolveScript.py"
test -f "$metal_resolve_lib"
PYTHONPATH="$metal_resolve_modules" RESOLVE_SCRIPT_LIB="$metal_resolve_lib" \
  python "$metal_ab/resolve_bench.py" probe
PYTHONPATH="$metal_resolve_modules" RESOLVE_SCRIPT_LIB="$metal_resolve_lib" \
  python "$metal_ab/resolve_bench.py" render --source h264 --codec h264 --graded --repeat 401
PYTHONPATH="$metal_resolve_modules" RESOLVE_SCRIPT_LIB="$metal_resolve_lib" \
  python "$metal_ab/resolve_bench.py" render --source dnxhr --codec dnxhr --graded --repeat 501
~~~

使用同一 `metal_ab` 目录评估播放。保持窗口可见，固定 Viewer 像素面积与刷新率。更换视口时，保留脚本依赖的 `frame_presented` 事件和实际呈现定义。

~~~bash
TOPOS_BACKEND_GRAPHICS=opengl TOPOS_EDIT_METAL_PREVIEW=0 \
  python "$metal_ab/topos_bench.py" playback --repeat 601
TOPOS_BACKEND_GRAPHICS=auto \
  python "$metal_ab/topos_bench.py" playback --repeat 602
TOPOS_BACKEND_GRAPHICS=metal \
  python "$metal_ab/topos_bench.py" playback --repeat 603

python tools/bench_render_ab_metal_gl.py --frames 300 --reps 5 \
  --sizes 1920x1080,3840x2160 --formats bgra8,rgba16f \
  --output-json "$metal_ab/render-ab.json"

TOPOS_EXPORT_GPU_RENDER=1 python tools/bench_macos_gpu_vt_export.py \
  --source tests/test_videos/4K_H264.mov --frames 400 --fps 25 \
  --output "$metal_ab/vt-smoke.mp4" \
  --report "$metal_ab/vt-smoke.json" --markdown "$metal_ab/vt-smoke.md"

unset TOPOS_CONFIG_PATH
~~~

纯 GPU A/B 隔离 kernel 耗时。桥接 smoke 验证 GPU 编码。实际 Viewer 和完整交付分别使用应用或项目基准。每份证据记录测试层。 [GPU A/B](../../tools/bench_render_ab_metal_gl.py) · [GPU 导出探针](../../tools/bench_macos_gpu_vt_export.py)

## 附录 B 回归工程和故障注入

| 样本 | 如何构造 | 核验点 |
|---|---|---|
| A 单 LUT 交付 | 附录 A 工程。先无调色，再单 LUT | 400 帧、切点、VUI、PTS、墙钟、实际码率和后端 |
| B 音频、字幕与转场 | 复制 A，在有可用素材余量的两段加交叉溶解、第二层、字幕与双声道音轨。开头留空，在已知时间放脉冲 | seek 后同步、AAC delay 和尾部 trim、取消、字幕位置和透明度 |
| C 调色图 | 建多个串联和并行 Corrector、曲线、LUT、动态遮罩、关键帧、旁路和示波器 | 节点顺序、静止和连续帧一致、Alpha、亮部与缓存失效 |
| D 合成与 3D | 在独立页和 Comp Clip 播放同一合成，加遮罩、模糊、透明 PNG、相机、深度和 AOV | 2D ROI、3D 深度/ID、缓存、分块、同步和交付 |
| E HDR 和 Alpha | 建已知 transfer/色域的 P010 或 10-bit HDR 工程和带 Alpha 的合成，使用明确数值的色块 | 元数据、负值、高光、Alpha、外屏、HDR→SDR 和编码限制诊断 |
| F 插件和老工程 | 复制早期 `.topos`。用 GL-only、已验 Metal、缺失插件和编译失败样本 | 工程 ID 和参数可重开，兼容路径可用，错误定位到效果 |
| G 压力与故障 | 多次 seek、暂停、跳帧、切页、切换显示器、暂时无 drawable。注入设备、编译、队列和封装错误 | 资源归还、拒绝旧帧、UI 响应、诊断、临时文件和原子发布。三轮后能正常运行。 |

固定 clip 起止、轨道时间、参数、素材 hash 和媒体重连规则。新增变量单独测量。每个任务单至少记录 **阶段编号和提交号、环境和实际后端、完整命令、结果文件、兼容及回退结果**。能用同一份素材复现后才标完成。

官方接口参考。 [Apple CAMetalLayer](https://developer.apple.com/documentation/quartzcore/cametallayer) · [Apple drawable 获取](https://developer.apple.com/documentation/quartzcore/cametallayer/nextdrawable%28%29?language=objc) · [Apple Metal 视口示例](https://developer.apple.com/documentation/metal/creating-a-custom-metal-view) · [Qt 原生窗口嵌入](https://doc.qt.io/qt-6/qtdoc-demos-windowhosting-example.html)
