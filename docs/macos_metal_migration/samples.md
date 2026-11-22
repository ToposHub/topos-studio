# 回归样本工程清单（阶段 0.3 / 0.5）

对应 [实施计划](../plans/2026-11-23-macos-metal-full-migration.md) 的阶段 0 与附录 B。样本只用
`tests/test_videos/` 里已冻结的媒体（矩阵见
[`tests/test_videos/benchmark_media_manifest.json`](../../tests/test_videos/benchmark_media_manifest.json)），
文件名入档、绝对路径不入档；另一位开发者用同一份媒体即可重建并得到可比较的证据。

## 已建成并可重建

| 编号 | 用途 | 构建方式 | 媒体 | 期望 | 状态 |
|---|---|---|---|---|---|
| A | 单 LUT 交付与播放对照（clean + 单 LUT） | `tools/bench_metal_ab_topos.py --ab-dir <目录> --build` | `4K_H264.mov`、`4K_DNxHR HQX 10bit.mov` | 4 条时间线各 400 帧、3840×2160、25/1、16 秒、无音轨；LUT `Topos_Resolve_AB_33.cube`（SHA-256 前 16 位 `ffba7587f1e154dc`，33³） | ✅ 已验证 |

### 样本 A 的结构

| 时间线 | 源 | 调色 | 片段（源区间 → 时间线区间，半开） |
|---|---|---|---|
| `h264_Original` | `4K_H264.mov` | 无 | `[25,225)` → `[0,200)`；`[275,475)` → `[200,400)` |
| `h264_LUT` | `4K_H264.mov` | 单 Corrector + 33³ LUT，强度 1.0 | 同上 |
| `dnxhr_Original` | `4K_DNxHR HQX 10bit.mov` | 无 | 同上 |
| `dnxhr_LUT` | `4K_DNxHR HQX 10bit.mov` | 单 Corrector + 33³ LUT | 同上 |

源的切点 `[25,225)` 与 `[275,475)` 对应 Resolve 工程 `Resolve_UHD25_AB.drp` 的两处裁切。
H.264 源带一条 `pcm_s24le` 音轨，样本 A 的导出配置 `include_audio=False`。

### 冻结媒体里与后续样本相关的实测事实

`ffprobe` 实测，供计划样本 E 等选材时直接引用：

| 媒体 | 编码 | 像素格式 | 尺寸 / 帧率 | 色彩标签（range / matrix / transfer / primaries） | 其它 |
|---|---|---|---|---|---|
| `4K_H264.mov` | H.264 High | `yuv420p` | 3840×2160 / 25 | tv / bt709 / bt709 / bt709 | `pcm_s24le` 音轨 |
| `4K_DNxHR HQX 10bit.mov` | DNxHR HQX | `yuv422p10le` | 3840×2160 / 25 | tv / bt709 / bt709 / bt709 | `pcm_s24le` 音轨 |
| `4K_H265.mov` | HEVC Main 10 | `yuv420p10le` | 3840×2160 / 25 | tv / bt709 / bt709 / bt709 | 10-bit **SDR**，不是 HDR；19.48 秒，48 kHz 立体声 |
| `2K_Prores4444_Alpha.mov` | ProRes 4444 | `yuva444p12le` | 3840×2160 / 25（名字里的 2K 不代表尺寸） | tv / bt709 / bt709 / bt709 | 带 Alpha，6.12 秒，48 kHz 立体声 |

## 重建与核验

```bash
source .venv/bin/activate
export PYTHONPATH=.
python tools/bench_metal_ab_topos.py \
  --ab-dir bench_out/metal-ab.<日期> --build --rounds 1 --no-verify
```

`--build` 在独立目录里复制基准脚本与 LUT 并重建工程，配置写到该目录的 `topos_runtime/`，
与用户配置隔离。重建后按上表的期望核验媒体路径、帧数、尺寸、帧率与时长。

## 计划中的样本与归属阶段

每个样本在**首次改动它所覆盖的渲染路径之前**建成并采集一次强制 OpenGL 基线，届时把本节
对应行移入上表。

| 编号 | 内容 | 媒体 | 核验点 | 建成时机 |
|---|---|---|---|---|
| B | 音频、字幕与转场：复制 A 的 `h264_LUT`，两段之间加交叉溶解，叠第二层与字幕，加双声道音轨；开头留空，在已知时间放脉冲 | `4K_H264.mov` + 带音轨素材 | seek 后同步、AAC delay 与尾部 trim、取消、字幕位置与透明度 | 阶段 3（含音频 H.264）开工前 |
| C | 调色图：多个串联与并行 Corrector、曲线、LUT、动态遮罩、关键帧、旁路、示波器 | `4K_H264.mov` | 节点顺序、静止与连续帧一致、Alpha、亮部、缓存失效 | 阶段 4 开工前 |
| D | 合成与 3D：独立页与 Comp Clip 播放同一合成，含遮罩、模糊、透明 PNG、相机、深度与 AOV | 合成节点 + 透明素材 | 2D ROI、3D 深度/ID、缓存、分块、同步、交付 | 阶段 5 开工前 |
| E | HDR 与 Alpha：已知 transfer/色域的 P010 或 10-bit HDR 工程，以及带 Alpha 的合成（明确数值的色块） | HDR 源需**合成**（冻结媒体集里没有 HDR 素材）；Alpha 用 `2K_Prores4444_Alpha.mov` | 元数据、负值、高光、Alpha、外屏、HDR→SDR、编码限制诊断 | 阶段 3（HDR）与阶段 4.4 开工前 |
| F | 插件与老工程：早期 `.topos` 副本；GL-only、已验 Metal、缺失插件、编译失败样本 | `plugins/*.tglsl`、`docs/validation/color_resources/*/baseline_project.topos` | 工程 ID 与参数可重开、兼容路径可用、错误定位到效果 | 阶段 6 开工前 |
| G | 压力与故障：多次 seek、暂停、跳帧、切页、切换显示器、暂时无 drawable；注入设备、编译、队列与封装错误 | 样本 A | 资源归还、拒绝旧帧、UI 响应、诊断、临时文件与原子发布 | 阶段 2（视口）验收时起，逐阶段复用 |

## 测量前置条件（本机实测）

- **显示器必须清醒。** 本机 `displaysleep 30`；显示器休眠后同一构建每例只呈现个位数到几十帧
  （实测 398 → 5 / 17 / 6）。`tools/bench_metal_ab_playback.py` 在显示器休眠时拒绝测量。
- 播放测量期间不要并行运行测试或其他重负载（实测并行 pytest 时每例 0–1 帧）。
- 不要中途强杀基准进程：会留下 `.topos.autosave`，下次启动进入恢复提示，工程不加载
  （表现为 `_total_frames=0`）。采样器会清理基准目录里的这类残留。
- Resolve 对照需要 Resolve 正在运行且开启外部脚本；`resolve_bench.py` 会切换当前工程并添加
  渲染任务，只应在测试库里运行。
