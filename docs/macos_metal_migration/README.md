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

## 工具

| 工具 | 用途 |
|---|---|
| `tools/bench_metal_ab_topos.py` | 导出 A/B 采样：隔离目录、首跑 + N 次暖跑、逐轮交替顺序、成片核验、汇总 JSON / Markdown |
| `tools/bench_metal_ab_playback.py` | 播放 A/B 采样：三种后端配置显式声明、用例有效性判定、停滞看门狗、显示器清醒检查 |
| `tools/diag_edit_metal_preview_gate.py` | 预览门控诊断：统计每帧首个不满足的条件与实际由 Metal 渲染的帧数 |

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

# 0.4 播放基线（需要显示器清醒、窗口可见；期间不要跑其它负载）
python tools/bench_metal_ab_playback.py \
  --ab-dir bench_out/metal-ab.<日期> --configs gl,auto,metal --repeats 4 \
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
