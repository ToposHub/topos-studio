# 调色页工具性能基准与优化报告（2026-08-21）

测量环境：headless 真实渲染管线（`AsyncRenderer` + 共享 GL 上下文 + `glFinish` 同步），
PyAV 预解码真实素材（`tests/test_videos/2K_h264.mp4`、`4K_H264.mov`，yuv420p 原始平面逐帧上传），
每个数字为**独立进程、200 帧测量窗口、15 帧 warmup** 的结果（隔离运行以排除热降频漂移；
连续跑多场景会因散热导致整体帧率下降 20-40%，比较必须在同等热状态下进行）。

工具：`tools/bench_color_tools.py`（场景基准）、`tools/shader_visual_regression.py`（视觉回归）。

## 4K 基准结果（优化前 → 优化后）

| 场景 | 优化前 fps | 优化后 fps | p50 ms 前→后 | 说明 |
|---|---|---|---|---|
| baseline（无调色） | 155 | 155-166 | 5.6 → 5.3-5.7 | 上传+管线基础成本 |
| **DCTL 双边滤波磨皮** | **65-68** | **91** | 13.8 → 9.9 | 128→64 采样/像素 |
| **USM 锐化重参数(mode1,r5,it4)** | **70-76** | **83** | 13.1 → 11.4 | 16→12 采样×迭代+中心外提 |
| **光学虚化 bokeh** | **96-105** | **122** | 9.4 → 7.7 | 48→28 采样 |
| DCTL 高频修复 | 92-102 | 119 | 9.8 → 8.3 | 受益于 fast_blur 降采样 |
| 质感模糊 bilateral | 123-125 | 150 | 7.5 → 6.4 | 7×7→5×5 网格 |
| **限定器+羽化+去噪散点**¹ | **35** | **112** | 28.3 → 8.5 | 形态学可分离化 15× |
| 单层全开(全部工具)² | 54-57 | 70 | 17.5 → 13.1 | 各项叠加收益 |
| 一级/曲线/HSL/混色器/Warper/LUT/柔切/颗粒/限定器/Window | 145-172 | ≈baseline | — | **全部近乎免费** |
| 双图层 / 三图层 | 129/117 | 151/132 | — | 每层线性叠加，均便宜 |

¹ 同口径比较：都是"逐帧重抠 matte"（正确行为）。优化前 28.3ms 是 (2r+1)² 全窗口形态学；
   可分离 H+V 两遍后 8.5ms，**bit-exact 等价**（min/max 方形窗口严格可分离）。
² 含 qualifier → 含逐帧重抠。修复 matte 缓存 bug 前的 17.5ms 是错误行为（复用第一帧 matte）下的值。

2K（1080p）：所有场景优化后 p50 均 < 5ms（>200fps），不构成任何预览压力。

## 视觉等价性验证（tools/shader_visual_regression.py，真实视频帧，旧 vs 新）

| 算法 | PSNR | 结论 |
|---|---|---|
| bilateral r10 / r30 | 57.1 / 48.8 dB | 无损/高保真 |
| bokeh r10 | 43.3 dB | 视觉不可区分 |
| fast_blur / USM | 60.0 / 59.9 dB | 无损 |
| 磨皮插件 | inf（8bit 量化下完全一致） | 无损 |
| 形态学 erode/dilate r=3/8/15 | bit-exact | 数学等价 |

## 实施的修改

Shader 优化（`shader_nodes.py` / `bilateral_skin_smooth.tglsl`）：
1. 磨皮插件 Fermat 螺旋 64→32 采样（两 pass 共 128→64 次纹理采样/像素）
2. bokeh 48→28 采样（dither 螺旋 + LINEAR 过滤补偿）
3. bilateral 7×7→5×5 dither 网格（步长补偿保持等效窗口）
4. fast_blur 16→12 采样（同时惠及 USM、高频修复、快速模糊三处）
5. USM 迭代循环中心采样外提（循环不变量）

管线/正确性修复：
6. **形态学可分离化**（`morphology.glsl` + 渲染器/引擎两处调用点）：H+V 两遍，
   15.5× 采样削减，bit-exact
7. **视频播放 qualifier matte 逐帧失效**：matte 缓存键加入帧 epoch——修复播放中
   抠像一直沿用第一帧 matte 的正确性 bug（mask-only 层不受影响，仍静态缓存）
8. **shader 编译一致性 bug**：插件未加载时（目录缺失/插件被移动）不再生成
   `process_P0` 调用却无定义 → 消除每帧编译失败重试风暴（`shader_compiler.py` 两处）

## 测试

- `tests/color/` + `tests/video/`：1356 passed（1 个 pre-existing 失败与本次无关，
  HEAD 上同样失败：`test_render_selects_correct_source_texture` 源码字符串断言过时）

## 剩余优化空间（未实施，按预期收益排序）

1. **播放降级对重效果基本无效**：`combo_full_pb ≈ combo_full`。`preview_quality` 把
   grain intensity 归 0（uniform 早退，有效）但 blur 钳制 radius≤20 后 bilateral 仍全量
   采样；磨皮插件无 `@preview_cost` 标记完全不受降级管理。可给 blur/插件按
   `reason=='playback'` 切换低采样变体 shader（结构 flag 进缓存 key，播放起停各编译一次）。
2. **USM 迭代模式**仍是最贵内置工具（11.4ms@4K 重参数）：迭代本质是对递增半径模糊的
   detail 叠加，可改为单 pass 多环采样或降分辨率预计算模糊图。
3. **4K 解码 49-54fps**（PyAV CPU）低于渲染吞吐：播放链路中解码先于调色成为瓶颈，
   硬解（VT 零拷贝）仅 Edit 页接入，Color 页播放仍是 CPU 解码。
4. `shader_compiler.build_composition_shader` 无条件内联全部 snippet（含未启用工具），
   驱动编译时间长（首帧卡顿感），运行时分支开销可忽略——若要改善首帧可按需拼装。
5. 播放时 shader 结构变化的场景（开关工具/插件）会重编译整个融合 program（含插件
   正则改写），拖动 select 类插件参数期间可能反复重编译——结构参数已进缓存 key，
   但编译本身 100ms 级，可考虑预编译常用排列。
