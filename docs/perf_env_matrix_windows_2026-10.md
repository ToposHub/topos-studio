# B0.1 环境兼容矩阵实录（Windows 线 · 机器 B）

> 计划：`docs/perf_plan_B_windows_2026-10-22.md` §B0.1。
> 采集脚本：`scripts/probe_env_matrix_windows.py`（只采集，不切分支/不 pull/不降级）；
> 原始 JSON：`scripts/env_matrix_windows_<tag>.json`。每台机、每次环境变更重新采集并分层归档，不混入同一提升比例。

## 机器 B · 2026-10-24 首录（tag `B_20261024_run01`）

> **⚠ 驱动漂移事件（同日）**：首录时（~11:40）NVIDIA 驱动为 **610.88**；
> 约 25 分钟后（12:06 起，B0.2 基准 run01–run04 期间）实查为 **621.01**
> （nvidia-smi / Win32_VideoController 32.0.16.2101 / probe 三源一致），
> 系 Windows 自动更新所致。已补 `B_20261024_run02_env` 刷新实录。
> **B0.2 全部四轮基准均在 621.01 上执行**（各 JSON `manifest.gpu_driver` 可证）；
> 610.88 时段只有本节首录，无性能数据。跨驱动版本结果分层归档，不混算。

| 维度 | 实录 |
|---|---|
| 主机 | DESKTOP-FMEKKRO · Windows 11 build 26200 · i9-13900K（32 逻辑核） |
| Python | 3.13.12 CPython（项目 `.venv`） |
| PyAV / FFmpeg | av 18.1.0；libavcodec 62.28.102、libavutil 60.26.102、libavformat 62.12.102、swscale 9.5.102（FFmpeg 8 系） |
| 渲染栈 | moderngl 5.12.0（standalone ctx: GL 3.3.0 NVIDIA 610.88）；PyQt6 6.11.0 / Qt 运行时 6.11.0 |
| GPU | RTX 4090（LUID `00011670-00000000`，驱动 610.88，24 GB，nvidia-smi 24564 MiB）；Intel UHD 770（`00012829-00000000`）；Microsoft Basic Render Driver |
| D3D11 | 默认适配器 = RTX 4090，feature level 0xC1（12.1）；CheckFormatSupport：NV12 / P010 / BGRA 全部通过（支持位见 JSON） |
| 原生扩展 | `topos_hwdecode_windows` **未安装**：`load_windows_native_bridge()` 返回 `not_yet_implemented:native_bridge:No module named 'topos_hwdecode_windows'`；site-packages 无残留 |
| 实际加载库 | av 自带全套 FFmpeg DLL（avcodec-62 / avformat-62 / swscale-9 等，路径+大小见 JSON）；系统侧 d3d11.dll / dxgi.dll / opengl32.dll；av.libs 中含 x264/x265/dav1d/vpx，**无任何 Topos 自有 codec 库** |
| git manifest | commit `8b2d36031`（feature 分支；采集时工作树含本批次新文件） |

PyAV 18.1.0 `HWDeviceType` 枚举（真机 import 实查）：`none, vdpau, cuda, vaapi, dxva2, qsv, videotoolbox, d3d11va, drm, opencl, mediacodec, vulkan, d3d12va, amf, ohcodec` —— B1.1 候选路线 2（`HWAccel(is_hw_owned=True)` 的 cuda / d3d11va）在本机枚举层面可用，帧保留行为仍待 PoC 实证。

## 依赖检查（constraints.txt 对拍）

41 项 pin 不一致，分类如下（原则：只登记、不自动改环境；版本变更单独提案）：

1. **平台预期缺项（11 项，不算漂移）**：`pyobjc-core` 与 7 个 `pyobjc-framework-*`（macOS 专属，constraints 自述平台包不参与解析）、以及仅发布工具链使用的 `twine/readme-renderer/keyring/jaraco.*` 家族成员。
2. **真实版本漂移（约 16 项，本机现状）**：`numpy` 2.4.4（pin 2.5.2）、`opencv-python` 4.13.0.92（pin 5.0.0.93）、`PyQt6-Qt6` 6.11.0（pin 6.11.2）、`scipy` 1.17.1（pin 1.18.1）、`pillow` 12.2.0（pin 12.3.0）、`onnxruntime` 1.24.4（pin 1.23.2）、`urllib3` 2.8.0（pin 2.7.0）、`protobuf` 7.34.1（pin 7.36.0）、`packaging` 24.2（pin 26.3）、`click` 8.3.2（pin 8.5.0）等——完整清单见 JSON `deps_check.mismatches`。
3. **仅开发机安装的额外包（181 项）**：whisperx / wandb / uvicorn 等无关工具链，不影响本计划基线，但说明该 venv 非干净 CI 环境——**跨机对比只认 JSON 内 manifest，不认"同一份 constraints"**。
4. **缺失于本机**：`OpenEXR`（F4.3 的 EXR 通路走 cv2，不依赖该包）、`pytest-qt`（影响 C0.1 分组，见 C0.1 报告）、`static-ffmpeg`、`playwright`（HTML 渲染门禁用，非本计划依赖）。

## 对计划的影响（进入 B0.2/B0.3/B1 的输入）

- **版本组合**：Python 3.13.12 + av 18.1.0 + moderngl 5.12.0 + Qt 6.11.0 在 4090 上共存可导入、可建 D3D11/GL 上下文；不降级 av 17（计划 §B0.1 约束）。
- **原生桥接路线（B1.1 候选 1）本机不可用**：默认加载目标缺失。B1.1 只能先走候选 2（PyAV `is_hw_owned=True`）或候选 3（自管窄桥接），除非先补建扩展。
- **纯 H264/H265 基准不依赖 Topos codec 库**成立：av.libs 无 Topos 自有库；Topos codec 相关项（如 `2K_topos.mov`）在 B0.2 中按"加载库强校验"单独分组，不通则如实记 fail。
- **同机噪声锚点**：后续 B0.2 基线必须引用本实录的 git manifest + 驱动版本作为环境指纹。注意上文驱动漂移：性能类比较一律以各 JSON 自带 `manifest` 为准。

## 机器 C（RX 6800）· 待采

C 机环境可用后运行同一脚本（`--tag C_<日期>_run01`），在本节登记同样维度；三机矩阵（B2.2）只收录带各自环境实录的结果。
