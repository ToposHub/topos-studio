# macOS Metal 全链路迁移 · 证据与清单

对应 [实施计划](../plans/2026-11-23-macos-metal-full-migration.md) 与
[实施记录](../plans/2026-11-23-macos-metal-full-migration-status.md)。本目录保存可重建的证据：
环境、能力探针、GPU 调用清单、A/B 基线与回归样本。大体积产物（成片、缓存）留在
`bench_out/`（已忽略），入库的只有报告与配置。

## 文件

| 文件 | 对应编号 | 内容 |
|---|---|---|
| [gl-inventory.md](gl-inventory.md) | 0.2 | GPU 调用清单：37 条运行路径（G-01～G-37），入口 / 阶段 / 预计能力 / 计划编号 / 实际后端 / 回退 |
| `evidence/gl-inventory-raw-6249e136.txt` | 0.2 | 静态命中原始列表（1623 条） |
| [evidence/environment-intel-2026-11-23.md](evidence/environment-intel-2026-11-23.md) | 0.1 | 机器、CPU、GPU、显示器、Python、FFmpeg、Resolve、提交号 |
| [evidence/capabilities-intel-amd-2026-11-23.md](evidence/capabilities-intel-amd-2026-11-23.md) | 0.1 | Metal 能力探针报告（设备、格式矩阵、导入结果） |
| `evidence/capabilities-intel-amd-2026-11-23.json` | 0.1 | 同上的结构化结果 |
| [evidence/ab-export-baseline-6249e136.md](evidence/ab-export-baseline-6249e136.md) | 0.4 | 导出基线：三条路径 × 首跑 + 5 次暖跑，墙钟、分阶段后端、成片核验 |
| `evidence/ab-export-baseline-6249e136.json` | 0.4 | 同上的逐轮明细 |
| [evidence/ab-playback-baseline-6249e136.md](evidence/ab-playback-baseline-6249e136.md) | 0.4 | 播放基线：三种后端配置 × 四条时间线 × 4 轮的实际呈现帧数 |
| `evidence/ab-playback-baseline-6249e136.json` | 0.4 | 同上的逐例明细 |
| [evidence/metal-preview-gate-diag.json](evidence/metal-preview-gate-diag.json) | 0.4 | `metal` 配置下预览门控的逐帧判定统计（实际后端的直接证据） |
| [evidence/metal-preview-gate-diag-auto.json](evidence/metal-preview-gate-diag-auto.json) | 0.4 | 同上，`auto` 配置 |
| [samples.md](samples.md) | 0.3 / 0.5 | 回归样本工程清单（A～G）、媒体实测事实、测量前置条件 |
| `evidence/ab-playback-native-smoke-5292d843.json` | 2.4 | 原生视口在真实应用里的功能冒烟（显示器休眠，帧数不作比较） |
| [effect-compat-matrix.md](effect-compat-matrix.md) | 6.1 | 效果、`.tglsl` 插件、转场、预设与其它插件类型的兼容矩阵（生成物，不手改） |
| `evidence/effect-metal-parity-2026-11-23.json` | 6.2 / 6.3 | 21 个已验证插件与内置曝光的 GL↔Metal 真机对拍：度量、门限、编译缓存键、反射绑定、编译器身份 |
| `evidence/effect-metal-msl/*.metal` | 6.3 | 对拍时生成的 MSL 源（每个效果一份） |
| `evidence/graphics-ledger-{startup,export-native,export-regular}-2026-11-24.json` | 7.5 | 图形调用台账：启动自检与一次导出（两条路径）的图形上下文；用修正了重复计数的台账重采 |
| `evidence/graphics-ledger-session-{gl,auto,metal,native}-2026-11-24.json` | 7.5 | 同上，四种配置各一次真实 GUI 会话（启动 → Edit 页 → 四条时间线各播一遍 → 退出） |
| `evidence/graphics-ledger-pages-{gl,metal,native}-2026-11-24.json` | 7.5 | 同上，遍历全部页面（Edit → Color → Composite → Deliver → Library）的三种配置 |
| `evidence/graphics-ledger-pages-metal-colormetal-2026-11-24.json` | 4.6 / 7.5 | 同上，Metal 配置加 `TOPOS_COLOR_METAL=1`：节点缩略图不再建 GL 上下文 |
| [evidence/ab-playback-interleaved-6249e136-vs-c92d8703-2026-11-24.md](evidence/ab-playback-interleaved-6249e136-vs-c92d8703-2026-11-24.md) | 2.4 | 播放：迁移前基线与当前提交同机交替对照（`gl` / `metal` 各两轮，16 组总和相差 +0.4%） |
| `evidence/ab-playback-interleaved-6249e136-vs-c92d8703-2026-11-24.json` | 2.4 | 同上的逐会话明细 |
| [evidence/ab-playback-9fe07060.md](evidence/ab-playback-9fe07060.md) | 2.4 | 采样工具的正式一轮：`gl` / `auto` / `metal` / `native` 各两轮（第二轮倒序），32 个用例全部有效；机器不空闲，绝对帧数低于 11-23 基线 |
| `evidence/ab-playback-9fe07060.json`、`evidence/ab-playback-9fe07060-conditions.json` | 2.4 | 同上的逐例明细，以及八个会话的显示器状态与采集时间 |
| [evidence/ab-playback-interleaved-gl-vs-native-9fe07060-2026-11-24.md](evidence/ab-playback-interleaved-gl-vs-native-9fe07060-2026-11-24.md) | 2.4 | 播放：`gl` 与原生配置交替各 4 轮——H.264 原生更高，DNxHR（GL 路径）稳定低约 3%，尚未归因 |
| `evidence/ab-playback-interleaved-gl-vs-native-9fe07060-2026-11-24.json` | 2.4 | 同上的逐会话明细 |

## 工具

| 工具 | 用途 |
|---|---|
| `tools/bench_metal_ab_topos.py` | 导出 A/B 采样：隔离目录、首跑 + N 次暖跑、逐轮交替顺序、成片核验、汇总 JSON / Markdown |
| `tools/bench_metal_ab_playback.py` | 播放 A/B 采样：`gl` / `auto` / `metal` / `native` 四种配置显式声明（`native` = `TOPOS_BACKEND_GRAPHICS=metal` + `TOPOS_EDIT_METAL_VIEWPORT=1`，Metal 帧直接进 `CAMetalLayer`）、用例有效性判定、停滞看门狗、显示器清醒检查 |
| `tools/diag_edit_metal_preview_gate.py` | 预览门控诊断：统计每帧首个不满足的条件与实际由 Metal 渲染的帧数 |
| `tools/build_effect_compat_matrix.py` | 从效果目录与验证登记表生成效果兼容矩阵（`--check` 在文档过期时返回 1） |

## 重跑

从仓库根目录执行，使用本机架构的虚拟环境。`tools/` 下的脚本依赖仓库根在 `sys.path` 上，
非 editable 安装时需要 `PYTHONPATH=.`。

```bash
source .venv/bin/activate
export PYTHONPATH=.

# 0.1 能力探针
python tools/probe_metal_capability.py \
  --output-json docs/macos_metal_migration/evidence/capabilities.json \
  --output-md docs/macos_metal_migration/evidence/capabilities.md

# 0.2 调用清单原始命中
rg -n 'QOpenGLWidget|OpenGL[. ]|moderngl|[.]glo\b|GLsync' src apps plugins \
  > docs/macos_metal_migration/evidence/gl-inventory-raw.txt

# 0.4 导出基线（首跑 + 5 次暖跑，逐轮交替顺序）
python tools/bench_metal_ab_topos.py \
  --ab-dir bench_out/metal-ab.<日期> --build --rounds 6 \
  --output-json docs/macos_metal_migration/evidence/ab-export-<提交>.json \
  --output-md docs/macos_metal_migration/evidence/ab-export-<提交>.md

# 0.4 / 2.4 播放基线（需要显示器清醒、窗口可见；期间不要跑其它负载）
python tools/bench_metal_ab_playback.py \
  --ab-dir bench_out/metal-ab.<日期> --configs gl,auto,metal,native --repeats 4 \
  --output-json docs/macos_metal_migration/evidence/ab-playback-<提交>.json \
  --output-md docs/macos_metal_migration/evidence/ab-playback-<提交>.md
```

## 本机事实（Intel 独显类）

- AMD Radeon RX 6900 XT，`hasUnifiedMemory=False`，纹理为 **Managed** 存储：GPU 写入后 CPU
  读回需显式同步，跨设备导入与同步必须单独验证。
- Metal 设备、CVMetal 纹理缓存、NV12 / P010 / BGRA / 4:2:2 导入全部通过；PyObjC 的
  CoreVideo 路由不可用（`-6661`），沿用 ctypes。
- 显示器 3840×2160 @ 60 Hz，DPR = 1。Apple Silicon 机器**尚未采集**，对应验收项保持待办。

## 实测中得到的、会影响后续施工的事实

- **Metal 预览在播放链路里是命中的，但只覆盖一部分帧。** 门控诊断（`TOPOS_BACKEND_GRAPHICS=metal`，
  一轮）：H.264 时间线由 Metal 渲染（477～769 帧 / 轮，视呈现而定），DNxHR 解码为 planar 帧，
  不满足 `frame_type == "vt_pixel_buffer"`，留在 OpenGL。`auto` 配置下只有「带 LUT 的单层
  NV12」走 Metal，未调色的 H.264 仍走 OpenGL（`lut_mode_without_grade`）。
- **阶段 0 期间修掉的两个既有遥测缺陷**（否则「实际后端」无从证明）：
  1. 帧 trace 的 `backend` 在入队时写死为 `opengl`，Metal 预览帧也如此。现在 present 描述符到达时
     标注实际后端，汇总里新增 `backend_counts`。
  2. `TimelinePreviewRenderer._lookup_frame_trace_id` 只查历史映射，而 `present_frame_ready` 总是
     先于 `frame_ready` 到达、此时在飞条目尚未入历史——`submitted` 标注在正常播放路径上一直静默
     落空。现在先查在飞表。
- **播放基线里 `stale_frame_emitted` 不为零**：DNxHR 播放约每 1460 帧出现 1 次旧帧覆盖
  （`gl` 两行、`auto` 一行各 1 次）。计划目标是零，需在阶段 2 前定位。
- **DNxHR 导出由 CPU 编码主导**：`dnxhr` 编码阶段 p50 43.3 ms/帧，400 帧串行约 17.3 秒，
  与整段墙钟（19.3 秒）同量级；解码与 GL 渲染分别约 7.5 与 27.9 ms/帧。
- **缩略图服务对 OpenCV 打不开的媒体会持续重试**：一次约 6.5 分钟的会话里
  `Failed to open video: 4K_DNxHR HQX 10bit.mov` 出现 18,935 次，并伴随时间线视图的重绘风暴。
  与 Metal 无关，但会干扰播放测量，建议单独处理。
## 原生 Metal 视口（阶段 2）

- 默认关闭。会话设置 `TOPOS_EDIT_METAL_VIEWPORT=1` 且 Metal 预览模式不是 `off` 时，合格的帧
  （单层 SDR VideoToolbox NV12，可选单个 LUT）直接呈现到 `CAMetalLayer`；其余帧照常经 GL Viewer。
- 报告里的 `实际呈现面` 一列（`present_path_counts`）区分 `native_metal` 与 `gl_viewer`。只看
  `实际渲染后端` 不够：Metal 计算的帧也可以经 IOSurface 落回 GL 上屏。
- 播放采样器带 `native` 配置；显示器休眠时拒绝测量，除非显式 `--allow-display-asleep`（数值不可比）。
- **宿主是窗口里唯一的原生控件**（`c92d8703`）。Qt 6 默认给原生子控件的每个兄弟控件也建一个原生窗口，祖先
  随之变成原生，`WA_DontCreateNativeAncestors` 挡不住；真实应用里节目监视器周围因此多出 13 个原生窗口，
  每次重绘各走一次软件刷新。宿主创建自己的窗口前先设
  `AA_DontCreateNativeWidgetSiblings`，于是图层始终压在非原生的兄弟控件之上，要显示在画面之上的内容一律走
  叠加层图像。
- 真实应用里的帧数与截图对照见实施记录 2.4：显示器清醒时 H.264 时间线 392～398 / 399 帧（实时），画面位置与
  GL Viewer 逐像素一致。

## 回退开关与启用台账（阶段 8.5 / 8.6）

发现回归时先关掉对应能力（整个会话留在兼容路径），再修正、复测、去掉开关：

```bash
TOPOS_METAL_DISABLE=native_viewport,edit_metal_composite <应用>
```

开关优先于各能力自己的开启方式；被关掉的能力在日志与 trace 里的原因是
`disabled_by_switch:<能力>`，可与设备、格式、shader、drawable 失败区分。未知名称只是被忽略
（不关掉任何东西）。能力清单与各自的状态在 `src/shared/gpu/metal/feature_switches.py` 的
`ROLLOUT`，每次把某项改成默认开启时在那里记下提交与兼容条件。

| 能力 | 默认 | 开启方式 | 转为默认的前提 |
|---|---|---|---|
| `edit_metal_preview` | macOS 的 `auto` / `metal` | `TOPOS_EDIT_METAL_PREVIEW=0` 回退 | 每帧按实际输入和调色能力选择 |
| `edit_metal_composite` | 跟随剪辑预览 | `TOPOS_METAL_DISABLE=edit_metal_composite` 回退 | 本机已跑 4K 多轨工程，Apple Silicon 单独验收 |
| `native_viewport` | 首个合格帧触发挂载 | `TOPOS_EDIT_METAL_VIEWPORT=0` 关闭原生呈现 | Retina 坐标、过期帧、异步卸载有定向测试 |
| `composite_metal` | 关 | `TOPOS_COMPOSITE_METAL=1` | 有生产调用方（comp clip、无头导出）且与 CPU/GL 取帧一致 |
| `color_graph_metal` | 关 | `TOPOS_COLOR_METAL=1` | 已接调色图、Color Viewer、节点缩略图及无蒙版波形和直方图，在 Intel 独显上对照 GL |
| `export_native_vt` | 开（macOS，graphics 为 auto/metal） | `TOPOS_EXPORT_NATIVE_VT=0` 关闭 | 导出文件上验证音频、分数帧率与取消（同机已做，见实施记录 3.1/3.2；B 帧与 29.97 仅有合成/生成素材的证据） |

`tests/shared/gpu/metal/test_feature_switches.py` 为每个能力保留一条「设置开关 → 能力拒绝」的
可复现回退测试。

## 图形调用台账（阶段 7.5 的度量工具）

`TOPOS_GRAPHICS_LEDGER=1` 启动应用时，记录本次会话经 moderngl 两个工厂（`create_context`、
`create_standalone_context`）和 `MetalComputeContext` 构造函数创建的每一个图形上下文：API（`opengl` / `metal`）、
调用方所在域、原因（模块.函数）、线程、会话；退出时把快照写到 `TOPOS_GRAPHICS_LEDGER_FILE`（未设置则打到
stderr）。未设置时不安装任何钩子。

**口径**（读数前必须知道的三件事）：

- **接管也算。** `moderngl.create_context()` 不带参数时绑定调用线程上**当前**的 Qt 上下文：各渲染线程都是先把自己的
  `QOpenGLContext` 设为 current 再调它，示波器控件在 `initializeGL` 里经 `GLContextManager.initialize` 调它。这类记录的
  `kind` 是 `adopt`——上下文是 Qt 建的，但同样是 Topos 在用的 GL 上下文，照常计数（这也是 Qt 建的上下文在台账里唯一的
  可见方式）。moderngl 自己建的（`create_standalone_context`，或没有当前上下文时的 `create_context()`）`kind` 是 `new`。
- **Qt 为自己的 `QOpenGLWidget` 建的上下文不经过这两个工厂，不计入**，除非随后被接管。所以 `counts["opengl"]` 是本次
  会话 GL 上下文数的下界——节目监视器、Source 监视器这类 `QOpenGLWidget` 即使在 `counts["opengl"] == 0` 时也各有一个
  GL 上下文。
- **standalone 只记一次。** moderngl 5.12 的 `create_standalone_context()` 内部就是 `create_context(standalone=True)`，
  2026-11-23 的台账把每个 standalone 上下文记成了两条（常规导出路径因此显示 2 个，实为 1 个）；已修正，三个快照按
  2026-11-24 重采。

`opengl_threads` 是调用 GL 工厂的线程。

```bash
TOPOS_GRAPHICS_LEDGER=1 TOPOS_GRAPHICS_LEDGER_FILE=/tmp/ledger.json <应用>
```

`TOPOS_GRAPHICS_FACTORY=deny` 让 GL 上下文工厂在创建任何东西之前抛 `GraphicsFactoryRefused`（仅当台账钩子已
安装时生效），用来在有正常 GL 的机器上测试调用方的回退；`TOPOS_METAL_DISABLE` 与它互不影响。

启动与导出（`evidence/graphics-ledger-{startup,export-native,export-regular}-2026-11-24.json`，本机 Intel + AMD）：

| 工作流 | GL 上下文 | GL 线程 | Metal 上下文 |
|---|---:|---|---:|
| 启动自检（`--test-startup`，无 GUI） | 0 | 无 | 0 |
| 导出 1080p25 LUT + 音频，Metal H.264 快路径（macOS 默认） | **0** | **无** | 2（导出渲染器 1、`cv_metal_bridge` 1） |
| 同一时间线，常规导出路径 | 1（standalone） | `export-render-pipeline` | 0 |

导出这条工作流上，7.5「Topos 创建的 GL 上下文和线程为零」已有证据：Metal H.264 快路径一个都不建。

2026-11-24 真实 GUI 会话（显示器清醒，本机 Intel + AMD；`evidence/graphics-ledger-session-*-2026-11-24.json`）。
每个会话：启动 → Edit 页 → 四条时间线各播一遍 → 退出：

| 配置 | GL 上下文 | GL 线程 | Metal 上下文 | 记录 |
|---|---:|---|---:|---|
| `gl`（`TOPOS_BACKEND_GRAPHICS=opengl`，Metal 预览关） | 1 | 剪辑预览渲染线程 | 0 | `edit_gpu_preview_renderer.run` 接管 Qt 上下文 |
| `auto` | 1 | 同上 | 1 | 同上；Metal 在 `metal_timeline_preview.__init__` |
| `metal` | 1 | 同上 | 1 | 同上 |
| `metal` + 原生视口（`TOPOS_EDIT_METAL_VIEWPORT=1`） | 1 | 同上 | 1 | 同上 |

预览这条工作流上，四种配置都还留着剪辑预览渲染线程上的那一个 GL 上下文（Qt 建、moderngl 接管）；其余是 Qt
自己的 `QOpenGLWidget` 上下文（不在台账里）。

2026-11-24 遍历全部页面（启动 → Edit → Color → Composite → Deliver → Library，每页停 6 秒；
`evidence/graphics-ledger-pages-{gl,metal,native}-2026-11-24.json`。合成页打开的是临时图——普通时间线片段没有绑定
comp clip）：

| 配置 | GL 上下文 | Metal 上下文 |
|---|---:|---:|
| `gl` | 5 | 0 |
| `metal` | 5 | 1 |
| `metal` + 原生视口 | 5 | 1 |

五个 GL 上下文在三种配置下相同：

| 域 | 调用处 | 线程 | 来源 |
|---|---|---|---|
| `edit` | `edit_gpu_preview_renderer.run` | 剪辑预览渲染线程 | 接管 Qt 上下文 |
| `shared_gpu` | `gl_backend.initialize`（示波器控件） | **主线程** | 接管 `QOpenGLWidget` 的上下文 |
| `color` | `async_renderer.run` | 调色异步渲染线程 | 接管 Qt 上下文 |
| `app` | `node_thumbnail_evaluator._evaluate_job` | `node-thumbnail-evaluator` | moderngl standalone |
| `composite` | `composite_renderer.run` | 合成渲染线程 | 接管 Qt 上下文 |

Metal 配置与 GL 配置在这一轮里的 GL 上下文**数量相同**：Metal 预览只是把 H.264 帧的渲染挪到 Metal，剪辑预览线程仍然要
GL 上下文（DNxHR 与 GL Viewer 路线都用它），调色页、合成页与节点缩略图评测器完全没动。这五处就是 7.5 余下的清单。

再加 `TOPOS_COLOR_METAL=1`（Metal 配置，`evidence/graphics-ledger-pages-metal-colormetal-2026-11-24.json`）：GL 上下文 4 个、
Metal 2 个——节点缩略图评测器的线程不再建 standalone GL 上下文，改为自持一个 Metal 上下文（4.6 接线后的样子）。

