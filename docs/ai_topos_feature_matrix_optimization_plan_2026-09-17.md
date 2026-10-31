# TOPOS 422Proxy 功能矩阵测试与 AI 控制优化计划

生成日期：2026-09-17  
主素材：`tests/test_videos/2K_topos_proxy.mov`  
测试库：`native/topos_codec/build/release/topos_codec.0.1.0.dylib`

## 1. 结论先行

本轮已经把 Edit 效果注册表、Color 核心工具矩阵、Composite 注册节点矩阵全部枚举并逐项尝试，但结果不是“全部通过”：

- Edit：25 个注册效果全部纳入清单；内置视频效果 6 项通过、Denoise 超时、4 个视频插件被导出预检阻断、4 个历史/clip-native 项不可附加；Transform/Crop/Opacity 另外作为 clip-native 实际导出通过。
- Audio：8 个 DSP 效果使用真实 WAV 逐项处理，8/8 返回有效音频。
- Color：14 个真实 TOPOS 帧工具等价路径通过；Color 插件因 MSL 设计阻断；Tracker/Auto Color 有单元和服务测试，但本轮没有冒充桌面真实 UI 通过。
- Composite：93 个注册 key 全部尝试；79 个产生非平输出，3 个全平，14 个没有输出。特殊源节点、3D、AI、文件输出需要专用 fixture，不能只用通用 image-input harness 判定。
- TOPOS：release native 库能解码；项目中旧的 dist/prof/relcur 库与测试素材不兼容。TOPOS 时间线导出仍有无效 FFmpeg 高位深探测日志，需要统一 native 路由。

因此，当前目标应定义为“建立可解释、可回滚、可验证的 AI 控制能力”，而不是把所有未实现或仅预览的效果标成可交付。

## 2. 测试证据

| 证据 | 文件 |
|---|---|
| Edit 逐效果矩阵 | [real_topos_edit_effect_matrix.json](/Users/testuser/Documents/vscode/topos_1/bench_out/extended_real_tests_20260917/real_topos_edit_effect_matrix.json) |
| Color 真实帧工具矩阵 | [real_topos_color_tools.json](/Users/testuser/Documents/vscode/topos_1/bench_out/extended_real_tests_20260917/real_topos_color_tools.json) |
| Composite 93 节点矩阵 | [real_topos_all_composite_nodes.json](/Users/testuser/Documents/vscode/topos_1/bench_out/extended_real_tests_20260917/real_topos_all_composite_nodes.json) |
| Composite 63 常用节点日志 | [real_topos_composite_nodes_bench.log](/Users/testuser/Documents/vscode/topos_1/bench_out/extended_real_tests_20260917/real_topos_composite_nodes_bench.log) |
| TOPOS 专项总报告 | [ai_real_material_test_optimization_plan_2026-09-17.md](/Users/testuser/Documents/vscode/topos_1/docs/ai_real_material_test_optimization_plan_2026-09-17.md) |
| Edit 驱动 | [bench_real_topos_edit_effects.py](/Users/testuser/Documents/vscode/topos_1/scripts/bench_real_topos_edit_effects.py) |
| Color 驱动 | [bench_real_topos_color_tools.py](/Users/testuser/Documents/vscode/topos_1/scripts/bench_real_topos_color_tools.py) |
| Composite 全注册节点驱动 | [bench_all_composite_nodes_real_topos.py](/Users/testuser/Documents/vscode/topos_1/scripts/bench_all_composite_nodes_real_topos.py) |

## 3. 素材与运行环境基线

| 项目 | 实际值 | 验收要求 |
|---|---|---|
| 源 | 2K TOPOS 422Proxy MOV | native probe 成功 |
| 画面 | 1920×1080 / 25fps / 96 帧 | 帧数、时间戳、色彩元数据正确 |
| 像素 | yuv422p10le / 10-bit | 不经过不必要的 8-bit 往返 |
| native release 库 | 可打开 | 启动诊断唯一确定路径 |
| dist/prof/relcur 库 | `tpcC magic/version 不符` | 不应被默认搜索到 |
| 无头 GL | AMD Radeon RX 6800 / GL 4.1 | compute fallback 必须可观测 |
| FFmpeg fallback | 会出现 `VideoStream.width` 无效探测日志 | TOPOS native 成功后不得重复探测 |

## 4. Edit 功能清单

### 4.1 视频效果

| ID | 效果 | 当前状态 | 优先级 | 修复/验收 |
|---|---|---|---|---|
| E01 | Blur | 8 帧导出通过，GPU trail 仍 CPU encoder surface | P1 | 2K ≥30fps；p95 ≤33.3ms |
| E02 | Sharpen | 8 帧导出通过 | P1 | 2K ≥30fps；输出有像素变化 |
| E03 | Denoise | 8 帧超过 120s | P0 | 8 帧先在 30s 内完成，再做全分辨率实时 |
| E04 | Edge Detect | 8 帧导出通过 | P1 | 对平坦/边缘素材分别验证非黑输出 |
| E05 | Erode/Dilate | 8 帧导出通过 | P1 | 正/负 size、边缘模式和 alpha 逐项验证 |
| E06 | Defocus | 8 帧导出通过，约 15.6s | P1 | 质量档位、半径上限、GPU downsample |
| E07 | Glow | 8 帧导出通过 | P1 | threshold/radius/intensity 与输出 attribution |
| E08 | Bilateral Skin Smooth | 导出预检阻断，无 export backend | P0 | 接入导出后端，或能力标记为 preview-only |
| E09 | Color Space Transform | 导出预检阻断，无 export backend | P0 | 接入导出后端，或能力标记为 preview-only |
| E10 | HighPass Retouch | 导出预检阻断，无 export backend | P0 | 接入导出后端，或能力标记为 preview-only |
| E11 | Film Halation | 导出预检阻断，无 export backend | P0 | 接入导出后端，或能力标记为 preview-only |
| E12 | Transform | clip-native 实际导出通过 | P1 | 从历史 FX 列表移出，保留 Inspector 能力 |
| E13 | Crop | clip-native 实际导出通过 | P1 | 与 Composite crop、导出裁切语义对拍 |
| E14 | Opacity | clip-native 实际导出通过 | P1 | 与淡入淡出、混合模式逐项对拍 |
| E15 | Vignette | ghost/non-attachable，无实际 FX renderer | P1 | 删除入口或统一接入 catalog/preview/export |

### 4.2 生成器和音频

| ID | 功能 | 当前状态 | 后续验收 |
|---|---|---|---|
| E16 | Solid Color | 创建、入轨、导出通过 | 颜色、alpha、叠加顺序、保存重载 |
| E17 | Adjustment Clip | 空链导出成功，但日志有 unsupported asset | 加非空调色/效果链，证明影响下方轨道 |
| E18 | Graphic EQ | 真实 WAV DSP 通过 | 完整 timeline export、频响和实时块耗时 |
| E19 | Limiter | 真实 WAV DSP 通过 | 峰值不超过阈值，seek 后状态清零 |
| E20 | Compressor | 真实 WAV DSP 通过 | attack/release、knee、ratio 对拍 |
| E21 | Reverb | 真实 WAV DSP 通过 | 尾音长度、取消导出、内存上限 |
| E22 | Delay | 真实 WAV DSP 通过 | ping-pong、feedback、seek 状态 |
| E23 | Saturation | 真实 WAV DSP 通过 | drive 扫描、无 NaN/Inf、峰值控制 |
| E24 | Imager | 真实 WAV DSP 通过 | M/S、width、pan 和单声道输入 |
| E25 | Noise Gate | 真实 WAV DSP 通过 | attack/release、阈值边界和噪声底 |

## 5. Color 功能清单

| ID | 工具 | 当前结果 | 后续验收 |
|---|---|---|---|
| C01 | Grading | 真实 TOPOS 帧等价路径通过；640×360 CPU 约207ms | Metal/GPU 2K 拖动 p95 ≤33.3ms |
| C02 | RGB Curves | 通过 | 1D LUT、HDR、边界和 UI 持久化 |
| C03 | HSL Curves | 通过 | 色相环绕、饱和度边界、GPU 对拍 |
| C04 | 3D LUT | 通过 | 33³/65³、HDR、强度混合、导入导出 |
| C05 | Mixer | 通过 | 8 色相区、串联顺序、GPU 对拍 |
| C06 | Warper | 通过等价路径 | 网格分辨率、边缘采样、GPU 对拍 |
| C07 | Soft Clip | 通过 | toe/shoulder、负值/HDR、参数动画 |
| C08 | Blur/Sharpen | 通过 | 四种 blur mode、radius、iterations、GPU |
| C09 | Film Grain | 通过 | seed、时间稳定性、HDR 和 alpha |
| C10 | Qualifier | 通过 matte 等价路径 | hue/sat/lum、softness、clean/denoise |
| C11 | Mask Shapes | 通过 circle matte | rect/curve/gradient/bitmap/brush、布尔运算 |
| C12 | Blend Modes | 通过 | 全部 blend enum、alpha、HDR |
| C13 | Scopes | 通过 histogram 输出 | waveform/vectorscope/histogram、实时刷新 |
| C14 | Denoise | 通过但 640×360 CPU 约527ms | GPU 半分辨率和质量档位 |
| C15 | Tracker | 服务/单测覆盖；本轮不宣称桌面 UI 通过 | 真实帧对、点/区域/平面、失败标记、回放 |
| C16 | Auto Color | 控制器/单测覆盖 | 真实 TOPOS 帧端到端、撤销、可解释参数 |
| C17 | Color Plugins | 当前 MSL emitter 显式阻断 | 双源 GLSL/MSL 或 preview-only 能力声明 |
| C18 | RAW Develop | TOPOS 视频不适用 | 使用 RAW 素材单独矩阵，不与 TOPOS 结果混淆 |

## 6. Composite 节点清单和分组

| 分组 | 当前证据 | 下一步 |
|---|---|---|
| 常用 GPU/fragment 节点 63 项 | 62 项有输出；chroma_keyer 全黑；ai_generate None；constant 全平 | 用绿幕、颜色参数、AI 模型 fixture 做 attribution |
| 全注册表 93 项 | 79 非平输出、3 全平、14 no-output | 每个特殊节点专用 graph fixture |
| TOPOS `video_frame` | native 路由定向通过；预览首帧约1208ms，导出首帧约106ms | 跑 96 帧顺序/seek/缓存/冷暖启动矩阵 |
| AI 节点 | `ai_generate` 返回 None；其他 AI 依赖输入/模型 | 缺模型时返回 `MODEL_UNAVAILABLE`，有模型时验证结果质量 |
| 3D 节点 | camera3d/geo3d/light3d/model3d 等通用输入不足 | 提供相机、灯光、模型资产和输出尺寸 fixture |
| 源/文件节点 | image_input、color_input、timeline_clip_input、file_output 等通用图不适用 | 为 source/output 设计独立输入与文件断言 |
| 生成节点 | constant 全平；noise/ramp 已可输出 | 固定非默认参数和颜色范围，禁止默认全平被判成功 |

## 7. 新问题优先级

| 优先级 | 问题 | 处理顺序 |
|---|---|---|
| P0 | native codec 版本不匹配 | 先修发布包、路径和 capability snapshot |
| P0 | 4 个视频插件没有导出后端 | 先决定 exportable 或 preview-only，再继续 AI 自主导出 |
| P0 | Denoise 2K TOPOS 8 帧超时 | 分段 profiling，先恢复可预测终止 |
| P0 | Color 插件 MSL 阻断 | 明确双源插件 ABI，不允许静默旁路 |
| P1 | TOPOS 重复 FFmpeg probe | native 成功后短路错误后端 |
| P1 | Adjustment Clip unsupported warning | 补真实非空效果链和下方轨道像素验证 |
| P1 | Composite compute fallback | 建立 Metal/GL4.3+ runner 和 A/B 质量证据 |
| P1 | AI/3D/源节点通用 harness 失败 | 专用 fixture，不把缺输入当实现通过 |
| P1 | chroma_keyer/constant 平输出 | 非默认参数、专用输入和非平输出门禁 |
| P1 | Color CPU fallback 偏慢 | GPU/Metal 优先，CPU 降级需显式质量等级 |

## 8. 推荐开发顺序

### 阶段 0：运行时和能力发现

1. 固定 TOPOS native library 搜索顺序，删除/隔离不兼容旧库。
2. 统一 `ffmpeg/ffprobe/topos_codec` capability snapshot。
3. MediaSourceFactory、FrameProvider、Exporter、Thumbnail Provider 共享同一路由结果。
4. TOPOS native 成功后禁止无效 FFmpeg 高位深探测。
5. AI 工具返回能力、版本、可用 backend、preview-only/exportable/requires-model 标志。

完成标准：干净 PATH、缺 ffmpeg、旧 TOPOS 库、正确 TOPOS 库四种环境都有结构化结果和可操作错误码。

### 阶段 1：可交付正确性

1. 为 4 个插件实现 export backend，或从 UI/AI export catalog 中标为 preview-only。
2. 修复 Denoise 超时并加入取消、超时和内存保护。
3. 修复 Vignette ghost 入口与 Adjustment Clip unsupported 路径。
4. 给 AI/3D/source/file 节点补专用 fixture 和错误状态。
5. 对输出增加“存在、非空、发生预期变化、可 probe、可保存重载”五项门禁。

完成标准：没有任何效果在预览中可见、导出却静默丢失；没有 `None` 被当成成功图像。

### 阶段 2：桌面 GPU/Metal 真实性能

1. 建立 macOS 桌面 Metal runner，补足 Color/Composite UI 操作。
2. 同一 TOPOS 帧对拍 CPU、OpenGL fallback、Metal。
3. 为每个效果记录 decode/upload/effect/compose/readback/encode/mux/audio 阶段。
4. 先优化 Denoise、Defocus、Blur、Glow，再优化多层和 Composite 多节点。
5. 首帧冷启动和连续 96 帧顺序播放分别设门槛。

完成标准：2K 常用 Edit/Color/Composite 效果 ≥30fps；参数拖动 p95 ≤33.3ms；性能回退有明确 reason。

### 阶段 3：AI 全自主闭环

AI 必须按以下事务序列运行：

```text
读取 capability snapshot
→ 创建项目和时间线
→ 导入 TOPOS/H264 素材并 probe
→ 读取效果/调色/节点能力
→ 生成并预检计划
→ 原子应用
→ 保存 revision
→ 预览并读取质量/性能 telemetry
→ 启动导出 job
→ 轮询直到完成/失败/取消
→ probe 输出并做像素/音频检查
→ 保存结果和可恢复报告
```

完成标准：重复 request 不重复创建；revision 冲突不覆盖用户修改；缺插件、缺模型、缺 native 库、GPU fallback、导出失败都能自动重试或明确请求用户；AI 不在 job 完成前宣称成功。

## 9. 回归命令

```bash
TOPOS_CODEC_LIB="$PWD/native/topos_codec/build/release/topos_codec.0.1.0.dylib" \
  ./.venv/bin/python scripts/bench_real_topos_edit_effects.py

TOPOS_CODEC_LIB="$PWD/native/topos_codec/build/release/topos_codec.0.1.0.dylib" \
  ./.venv/bin/python scripts/bench_real_topos_color_tools.py

TOPOS_HEADLESS_GL=1 QT_QPA_PLATFORM=offscreen \
  ./.venv/bin/python scripts/bench_all_composite_nodes_real_topos.py
```

运行结果必须保存 JSON、stderr、native library 路径、GPU backend、素材 hash、git revision 和最后一个测试项。任何 segfault、timeout 或空输出都不能只显示为“失败”，必须转成 AI 可读取的结构化状态。
