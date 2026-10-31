# W00 · Windows 能力/环境基线 manifest（机器 B · 2026-10-24）

> 责任包：W00（`docs/upgrade_plan_W_windows_2026-10-24.md` §3）。
> 采集时点：HEAD `12b998754`（feature 分支，工作树干净）；本 manifest 由本轮
> W 计划执行机现场采集，非复制旧 B 矩阵。
> 采集器：`scripts/probe_env_matrix_windows.py --tag W00_20261024_run01`
> （结构化产物：`env_matrix_W00_20261024_run01.json`，本目录副本）。

## 1. 主机与适配器

| 项 | 值 |
|---|---|
| OS | Microsoft Windows 11 专业版，build 26200 |
| CPU | 13th Gen Intel Core i9-13900K（32 逻辑核） |
| GPU 0（CUDA/NVDEC） | NVIDIA GeForce RTX 4090，驱动 32.0.16.2101（621.01），24 GB，compute 8.9 |
| GPU 1（核显） | Intel UHD Graphics 770，驱动 32.0.101.7088 |
| 显示适配器 2 | USB Mobile Monitor Virtual Display（虚拟屏，driver 2.0.0.1） |

4090+核显同机在场：W04"4090+核显显示组合"与错设备注入可在本机取证。

## 2. 软件栈（venv 实测）

| 组件 | 版本 |
|---|---|
| Python | 3.13.12（`.venv`） |
| av (PyAV) | 18.1.0（FFmpeg 8.1.2 系） |
| PyQt6 | 6.11.0 |
| ModernGL | 5.12.0 |
| PyOpenGL | 3.1.10 |
| numpy | 2.4.4 |
| cuda-bindings | 13.4.2（cuda-pathfinder 1.8.2） |
| torch（开发环境间接存在） | 2.8.0+cu128 |
| PyInstaller | **缺失**（W06 需处理；冻结包验收暂不可跑） |
| pytest / pytest-qt | 9.1.1 / 4.5.0 |
| ziglang（PyPI 包内含 zig.exe） | 0.16.0 |
| cmake / ninja（python -m 模块） | 4.4.3 / 1.13.2 |

`pip check`：1 项不一致——`qwen-tts 0.1.1` 要求 `transformers==4.57.3`，
装的是 4.57.6（开发期 TTS 工具，非产品核心依赖；登记不阻塞）。
constraints.txt 对拍 40 项 pin 不齐（多为补装/未装的可选件，全表见
env matrix JSON `deps_check`）。

**与旧 B 矩阵的关系**：Python 3.13.12 / PyAV 18.1.0 / 驱动 621.01 与旧报告
数值一致系同机同栈自然结果，本轮全部现场重采，未复制旧值。

## 3. 原生 codec 库现状（W-I01 红基线，待 W01 重建）

`candidate_library_paths()` 实查（本机 build 目录 4 个 DLL）：

| 候选 | 大小 | SHA256（前16） | 加载 | ABI（含 tc_mux_add_audio） |
|---|---|---|---|---|
| win-half-tests | 4293120 | 4f8cc6cfb8d37ca0 | 可加载 | **缺符号** |
| win-release-zig4 | — | — | WinError 193（伪/损坏） | — |
| win-release-zig7 | — | — | 可加载 | **缺符号** |
| win-release-zig8 | 729088 | a27fb1c43433b21c | 可加载 | **缺符号** |

`resolve_topos_codec_library()` → `incompatible`（缺 `tc_mux_add_audio`）。
源码 `native/topos_codec/src/mov/mov.c:985` 已含该函数——所有现存 DLL 均为
旧构建。工具链（cmake/ninja/zig）现已在 venv 内具备，W01 在本机重建。
另：候选排序按字母序使 `win-half-tests` 测试构建先于正式构建被尝试，W01
一并收紧（仅 `release`/`win-release*` 目录可作生产候选 + PE 架构校验）。

## 4. 基线测试指纹（开工时点，2 轮间无 src 改动）

| 套件 | 结果 |
|---|---|
| `tests/media/test_windows_cuda_resident.py`（L1/L2） | **37 passed** / 0 skipped |
| `tests/edit/test_cuda_resident_playback_l3.py`（L3，`QT_QPA_PLATFORM=windows`） | **3 passed** / 0 skipped（含 B2.1 已登记快照缺陷的宽松断言，W05 将收紧） |

L3 offscreen 平台不可跑 GL（`QOpenGLContext.create() failed`）——须
`QT_QPA_PLATFORM=windows` 真窗口平台，与 0b57c322d 结论一致。

## 5. 输入媒体 hash（复现锚点）

| 素材 | SHA256（前20） | 大小 |
|---|---|---|
| tests/test_videos/4K_H264.mov | 044b677f60b6bfb5561b | 53743212 B |
| tests/test_videos/2K_H265.mov | 0aee87a9fc159d2e28bf | 42352987 B |

## 6. 能力边界声明（本轮开工时点）

- CUDA 驻留解码：**opt-in**（`TOPOS_WIN_HW_RESIDENT_DECODE`），默认关闭；
  默认路由为 NVDEC/D3D11 下载 planar（3.5）与 CPU planar。
- D3D11 合成/直送、Topos GPU 编码：维持 B 线"评估不适用/延期"决策。
- AMD/Intel 驻留（UW02）：本机 UHD 770 仅作 W04 错设备/核显组合取证，
  不冒充 AMD/Intel 驻留验证（另机未验）。
- 冻结包（PyInstaller）：本机未装，W06 只能做声明+干净 venv 探针，
  冻结 exe 验收登记未验。
- 旧 A/B/F 勾选数（45/53）不作为本线绿灯；本线按 W01–W10 逐包收口。

## 7. 冻结指标与允许退化

- 性能指标以 B1.2 实测为参考（4K H264 驻留 241fps / H265-10bit 306fps vs
  下载 102/80fps），W02–W05 修复后允许 ±10% 波动；**正确性不允许退化**
  （L2 md5 对拍、L3 确定性断言只许收紧）。
- 取消/释放契约：源帧/slot/租约 exactly-once，关闭路径排水（租约归零才
  close），任何修复不得引入双释放或漏释放。

## 8. 现有失败 ID（开工时点继承）

W-I01（codec DLL 旧构建红基线）、W-I02（驻留源失败无恢复链）、
W-I03（CUDA 生命周期/回滚/池）、W-I04（设备 0 假设+格式门薄）、
W-I05（快照缺陷+宽松断言）、W-I06（extras/冻结包/CI 收口）、
W-I07（浸泡/多轨/矩阵未验）。共享问题 S01–S12/R01–R13 按 §2 清单由
Mac M 线提交后在本机复验（当前 M 线提交未入本仓库，W07 只能复验本仓库
已有部分，如实登记）。
