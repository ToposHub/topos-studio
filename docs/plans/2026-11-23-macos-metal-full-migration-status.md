# macOS Metal 全链路迁移 · 实施记录

对应 [实施计划](2026-11-23-macos-metal-full-migration.md)，代码基线 `6249e136`。每完成一个编号在此
登记：提交、运行命令、测试报告、实际后端、差异与回退结果。证据与清单见
[docs/macos_metal_migration](../macos_metal_migration/README.md)。

本机为 **Intel 独显类**（i9-9900K + AMD Radeon RX 6900 XT，macOS 15.3.1，显示器 3840×2160 @ 60 Hz，
DPR = 1）。Apple Silicon 机器尚未采集，凡标「仅 Intel 独显」的验收项在另一类机器上仍为待办。

## 总览

| 阶段 | 状态 | 说明 |
|---|---|---|
| 0 环境和基线 | ✅ 0.1–0.4 完成；0.5 样本 A 完成，B–G 已登记规格与归属阶段 | Resolve 对照沿用同机 11-22 记录，本次未驱动正在运行的 Resolve |
| 1 共享 GPU 帧 | ✅ 1.1–1.3 代码与定向测试完成 | 调度器与帧事务尚未接入生产预览路径（阶段 2 接线） |
| 2 原生视口和剪辑 | ⏳ 未开始 | 开工前须先解决播放基线里的旧帧覆盖（见 0.4） |
| 3–8 | ⏳ 未开始 | — |

既有失败（与迁移无关，均在基线代码上复现或属本机环境）：
`tests/edit/test_ef01_aud_part1.py::TestAud15506::test_hotpath_dependencies_imported_at_module_level`；
`tests/media/test_topos_source.py::test_no_deblock_request_skips_output_filter` 与
`tests/native/test_topos_binding.py::test_codec_capabilities_are_truthful`（本机原生编解码库未广播
`NO_DEBLOCK`，`flags=159`，为本地构建产物陈旧）。

## 阶段 0 环境、基线和覆盖清单

### 0.1 配环境（提交：本记录首次入库的提交）

- 命令：`sw_vers` / `uname -m` / `python -V` / `ffmpeg -version` / `system_profiler SPDisplaysDataType`，
  `PYTHONPATH=. .venv/bin/python tools/probe_metal_capability.py --output-json … --output-md …`。
  工具脚本依赖仓库根在 `sys.path` 上，非 editable 安装须加 `PYTHONPATH=.`。
- 结果：`import PyQt6, Metal, Quartz, VideoToolbox` 通过；Metal 设备 AMD Radeon RX 6900 XT
  （`hasUnifiedMemory=False`，纹理为 Managed 存储，`argumentBuffersTier=1`）；MSL 编译与 dispatch
  正确；NV12 / P010 / BGRA / 4:2:2 8-bit / v210 经 CVMetalTextureCache 零拷贝导入全部通过；
  PyObjC 的 CoreVideo 路由 `-6661` 不可用，沿用 ctypes。
- 版本：macOS 15.3.1 (24D70)、Python 3.12.13、PyQt6 6.11.0、PyAV 18.1.0、moderngl 5.12.0、
  FFmpeg 9.0.2、DaVinci Resolve 21.2.0。
- 产物：[environment](../macos_metal_migration/evidence/environment-intel-2026-11-23.md)、
  [capabilities](../macos_metal_migration/evidence/capabilities-intel-amd-2026-11-23.md)。

### 0.2 建清单（同一提交）

- 命令：`rg -n 'QOpenGLWidget|OpenGL[. ]|moderngl|[.]glo\b|GLsync' src apps plugins`。
- 结果：1623 条命中 / 189 个文件，其中 89 个直接绑定 GL API；按运行路径整理为 37 条（G-01～G-37），
  每条带入口、阶段、预计能力、计划编号、当前实际后端、回退方式与销账状态。
  见 [gl-inventory.md](../macos_metal_migration/gl-inventory.md)。
- 关键事实：生产中「选择器」有两套——声明式 `backend_selector` 未接线，实际由 `ConfigManager`
  与 `TOPOS_BACKEND_GRAPHICS` 决定，且导出端（`timeline_export.py:9890`）与预览端
  （`metal_timeline_preview.py:86`）各自解析；`auto` 在导出端落 `opengl`，在预览端为 `lut`。
  6 个 `QOpenGLWidget` 视口、约 20 处 GL 上下文创建点。

### 0.3 对照工程（同一提交）

- 命令：`python tools/bench_metal_ab_topos.py --ab-dir bench_out/metal-ab.2026-11-23 --build`。
- 结果：隔离目录内重建 `Topos_UHD25_AB.topos`，4 条时间线（`h264_Original`、`h264_LUT`、
  `dnxhr_Original`、`dnxhr_LUT`），媒体路径正确，均为 400 帧、3840×2160、25/1、16 秒、无音轨。
  LUT SHA-256 前 16 位 `ffba7587f1e154dc`。验收通过。
- Resolve 侧：`resolve_bench.py` 会 `LoadProject` 切换当前工程并添加渲染任务；测量时 Resolve
  正在运行且无法确认当前库是否为测试库，**未执行**。同机 11-22 记录（H.264 LUT 6.82–6.93 秒、
  DNxHR LUT 10.21 秒）作为参考。

### 0.4 采时间与质量（同一提交）

导出（显示器清醒，首跑 + 5 次暖跑，逐轮交替顺序，18/18 成功，成片核验全部合格）：

| 路径 | 首跑 | 暖跑中位数 | 暖跑 min–max | 实际交付路径 | 分阶段 p50（毫秒） |
|---|---:|---:|---|---|---|
| GL H.264 | 11.149 秒 | 10.843 秒 | 10.708–11.861 | 通用 | 解码 6.57（`provider_session`）· 渲染 23.09（`opengl`）· 编码 13.90（CPU `h264`） |
| Metal H.264 | 7.114 秒 | 7.124 秒 | 7.050–7.302 | `metal_h264_nv12_lut`，全 GPU | 解码 0.11（`videotoolbox`）· 渲染 1.94（`metal`）· 编码 6.91（`videotoolbox`） |
| GL DNxHR | 19.225 秒 | 19.276 秒 | 18.586–19.634 | 通用 | 解码 7.46（`ffmpeg_planar`）· 渲染 27.92（`opengl`）· 编码 43.25（CPU `dnxhr`） |

- 实际码率：GL H.264 29.78、Metal H.264 28.42（目标同为 50 Mbit/s VBR）、DNxHR 728.27 Mbit/s；
  色彩标签均为 tv / bt709 / bt709 / bt709。
- 与计划起点相比：Metal 持平（7.06 → 7.12 秒）；两条 GL 路径慢约 7%（10.16 → 10.84、
  17.7–17.9 → 19.28 秒）。GL 路径是 CPU 解码 + CPU 编码，测量时 Resolve 等应用在后台运行，
  CPU 争用是最可能的原因；Resolve 耗时比：Metal H.264 ≈ 1.04×，DNxHR ≈ 1.89×（目标 ≤ 1.5×）。
- **DNxHR 由 CPU 编码主导**：编码 43.25 ms/帧 × 400 ≈ 17.3 秒，与整段墙钟同量级。

播放（显示器清醒，三种配置 × 四条时间线 × 4 轮，48/48 有效；呈现帧中位数，分母 400）：

| 配置 | H.264 Original | H.264 LUT | DNxHR Original | DNxHR LUT |
|---|---:|---:|---:|---:|
| `gl` | 399 | 398 | 370 | 368.5 |
| `auto` | 400 | 391 | 373.5 | 370 |
| `metal` | 399.5 | 399 | 374.5 | 384.5 |

- 实际渲染后端（门控诊断逐帧统计，不依赖显示器状态）：

  | 配置 | H.264 Original | H.264 LUT | DNxHR |
  |---|---|---|---|
  | `gl` | OpenGL | OpenGL | OpenGL（planar 上传） |
  | `auto` | OpenGL | **Metal** | OpenGL（planar 上传） |
  | `metal` | **Metal** | **Metal** | OpenGL（planar 上传） |

  DNxHR 解码为 planar 帧，不满足 Metal 预览的 `frame_type == "vt_pixel_buffer"`，这是 Metal 预览
  在 DNxHR 上零命中的原因。计划目标「轻剪辑 ≥ 396 帧」：H.264 基本达到，DNxHR 为 368–384。
- 播放基线文件里的 `backend_counts` 一列**不可信**（早于下述 trace 修复），已在文件内注明。
- **测量前置条件**：显示器休眠后同一构建每例只呈现个位数到几十帧（实测 398 → 5 / 17 / 6，在
  基线代码上用 `git worktree` 对照复现，排除了代码回归）。采样器现在拒绝在休眠显示器上测量。
- 计划附录 A 的 `topos_bench.py playback --repeat 601`：`playback()` 里 `--repeat` 是**重复次数**
  而非标签，按字面执行每个配置约需 13 小时。`tools/bench_metal_ab_playback.py` 用 `--repeats`
  显式限定轮数。

为证明「实际后端」而修掉的两个既有遥测缺陷：

1. 帧 trace 的 `backend` 在入队时写死 `opengl`。现在 present 描述符到达时标注实际后端，
   `summary()` 新增 `backend_counts`（`playback_frame_trace.py`、`timeline_preview_renderer.py`）。
2. `TimelinePreviewRenderer._lookup_frame_trace_id` 只查历史映射，而 `present_frame_ready` 总是先于
   `frame_ready` 到达、此时在飞条目尚未入历史——`submitted` 标注在正常播放路径上一直静默落空
   （`present_ms` 因此从未算出）。现在先查在飞表。

测试：新增 `tests/video/test_frame_trace_backend_counts.py`（8 项）、
`tests/edit/test_frame_budget_wiring.py`（+2 项）；相关遥测套件 37 项通过。

待处理（记入阶段 2 开工条件）：

- 播放基线 `stale_frame_emitted` 不为零：DNxHR 约 1460 帧出现 1 次旧帧覆盖（`gl` 两行、`auto`
  一行各 1 次），计划目标为零。
- 缩略图服务对 OpenCV 打不开的媒体持续重试（一次会话 18,935 次），伴随时间线视图重绘风暴。
- 带正确 `backend_counts` 的三配置播放重测：需显示器清醒时执行一条命令（见 README）。

### 0.5 固化项目样本（同一提交）

样本 A（单 LUT 交付与播放对照）已建成并核验，可由 `tools/bench_metal_ab_topos.py --build` 重建。
样本 B–G 已登记内容、媒体、核验点与归属阶段，各自在首次改动其覆盖的渲染路径之前建成并采集
强制 OpenGL 基线。实测发现冻结媒体集里没有 HDR 素材（`4K_H265.mov` 是 10-bit BT.709 SDR），
样本 E 的 HDR 源需要合成。见 [samples.md](../macos_metal_migration/samples.md)。
