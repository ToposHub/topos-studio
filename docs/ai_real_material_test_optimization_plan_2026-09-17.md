# Topos Color 真实 H264 素材端到端测试与优化计划

## 0. 文档信息

| 项目 | 内容 |
|---|---|
| 测试日期 | 2026-09-17 |
| 测试素材 | `tests/test_videos/2K_h264.mp4` |
| 素材类型 | H.264 / yuv420p / 1920×1080 / 25 fps / AAC 48 kHz |
| 素材长度 | 1572 帧，约 62.88 秒 |
| 测试产品 | Studio 模式启动检查；真实 Edit/Color/Composite/Deliver 业务链路 |
| 测试方式 | Qt 应用运行时初始化 + 真实项目模型 + 真实时间线命令 + 真实 Composite 图编辑 + 生产导出器 + PyAV 输出探测 |
| 目标 | 找出功能错误、数据丢失、效果未生效、导出失败、速度不足、GPU/CPU 回退和测试基础设施问题 |
| 总体结论 | 核心流程可以跑通，但 Blur、调色和多轨合成已经接近或低于 25 fps 实时目标，仍不能宣称交付级全功能高速运行 |

本轮结果文件：

- [完整工作流结果](../bench_out/real_user_workflow_h264_full/workflow_result.json)
- [完整工作流工程](../bench_out/real_user_workflow_h264_full/ai_real_h264_workflow.topos)
- [完整工作流导出文件](../bench_out/real_user_workflow_h264_full/ai_real_h264_workflow_export.mp4)
- [Studio 启动日志](../bench_out/real_user_workflow_h264_full/startup_test.log)
- [调色 CPU/GPU 对照](../tests/media/results/export_effects_matrix_real_h264_grade_groups_2026-09-17.md)
- [模糊 CPU/GPU 对照](../tests/media/results/export_effects_matrix_real_h264_blur_groups_2026-09-17.md)
- [双轨 CPU/GPU 对照](../tests/media/results/export_effects_matrix_real_h264_two_layer_groups_2026-09-17.md)

---

## 1. 本轮实际执行的用户流程

### 1.1 已执行流程

```text
启动 Studio
  → 探测真实 2K H264 素材
  → 创建项目
  → 导入素材
  → 创建视频轨和音频轨
  → 创建时间线
  → 在第 120 帧执行刀切
  → 加载 Gaussian Blur
  → 修改 Blur 参数
  → 创建调色栈
  → 写入曝光/对比度/饱和度/Gain
  → 将调色栈挂载到片段
  → 创建 Composite 图
  → 添加并连接 Blur 节点
  → 保存 .topos 工程
  → 重载工程
  → 含音频 H264 生产导出
  → PyAV 解码和输出质量检查
```

### 1.2 已实际执行的效果矩阵

使用 `scripts/bench_export_effects_matrix.py` 对真实 H264 素材执行：

- identity；
- resize；
- opacity fade；
- grade；
- Gaussian Blur；
- two layer；
- cross dissolve transition；
- sharpen；
- glow；
- adaptive、forced GPU、CPU render 三条路径对照。

### 1.3 当前测试边界

本环境的原生桌面自动化接口无法连接到已启动的 Qt 原生窗口，因此本轮没有使用鼠标坐标逐项点击 UI 控件。测试使用了应用的 Qt 运行时初始化、同一套项目模型、时间线命令、Composite 编辑服务和生产导出器，属于“应用业务层真实链路”验收。

以下内容仍需后续补充原生 UI 手工/自动化验收：

- 鼠标拖素材到 Media Pool；
- 鼠标拖素材到 Timeline；
- UI 面板参数控件的信号和刷新；
- Color 页面控件与 ColorStack 的双向同步；
- Composite 节点编辑器的可视化连线；
- Deliver 页面队列、取消和错误提示；
- UI 面板显示、布局和多产品页面切换。

这属于验证覆盖缺口，不应被误判为业务链路已通过 UI 验收。

---

## 2. 真实素材和环境探测结果

### 2.1 输入素材

| 属性 | 实测值 | 结论 |
|---|---:|---|
| 文件 | `2K_h264.mp4` | 存在 |
| 视频编码 | H.264 | 可探测 |
| 像素格式 | yuv420p | 可解码 |
| 分辨率 | 1920×1080 | 可进入时间线 |
| 帧率 | 25.000 fps | 时间线可建立 |
| 视频帧数 | 1572 | 足够执行 240 帧测试 |
| 音频编码 | AAC | 可探测 |
| 音频采样率 | 48000 Hz | 可进入音频轨 |
| 音频声道 | 2 | 立体声素材 |

### 2.2 Studio 启动检查

启动命令：

```bash
./.venv/bin/python src/main.py \
  --product studio \
  --test-startup \
  --no-startup-dialog
```

结果：进程退出码为 `0`，Color、Composite、Edit、Deliver、Teaching Animation 和 AI Workflow 模块均完成注册。

但启动日志发现：

1. `ThumbEngine` 报告 `FFmpeg NOT found`，视频缩略图功能会失败；
2. Qt 将 `C` locale 自动切换为 UTF-8，说明启动环境没有提前保证 UTF-8 locale；
3. 三次 `checkmark.svg` 加载失败，路径被拼成了：

   ```text
   /Users/testuser/Documents/vscode/topos_1/file:/Users/testuser/Documents/vscode/topos_1/src/shared/assets/icons/checkmark.svg
   ```

4. Qt Multimedia 自带 FFmpeg 7.1.5，但这不等于应用的 ThumbEngine 能找到独立 FFmpeg 可执行文件。

---

## 3. 完整工作流测试结果

### 3.1 功能步骤结果

| 步骤 | 结果 | 实测结果 |
|---|---|---|
| 真实素材探测 | 通过 | H264、1920×1080、25 fps、AAC 48 kHz |
| 创建项目 | 通过 | 成功创建项目和媒体资产 |
| 创建视频时间线 | 通过 | 1 条视频轨，初始 1 个片段 |
| 创建音频轨 | 通过 | 1 条音频轨，1 个音频片段 |
| 刀切 | 通过 | 第 120 帧切成 120+120 两段 |
| 加载效果 | 通过 | 添加 `blur`，设置为 Gaussian，X/Y 半径 6 |
| 调色 | 通过 | 创建 ColorStack，写入曝光/对比度/饱和度/Gain |
| 调色挂载 | 通过 | 调色栈成功绑定到第一段视频片段 |
| Composite 创建 | 通过 | 默认图创建成功 |
| Composite 节点编辑 | 通过 | 添加 Blur 节点，验证通过，连接数变为 3 |
| 工程保存 | 通过 | 写出 `.topos` 文件 |
| 工程重载 | 通过 | 资产、时间线、片段、效果、调色引用和合成均读回 |
| H264 导出 | 通过 | 240 帧，含音频，硬件编码无回退 |
| 视频输出解码 | 通过 | 240/240 视频帧可解码 |
| 音频输出解码 | 通过 | 输出存在 AAC 音频，451 个音频帧可解码 |

### 3.2 完整工作流导出结果

| 指标 | 实测值 |
|---|---:|
| 输出分辨率 | 1920×1080 |
| 输出视频编码 | H.264 / yuv420p |
| 输出视频帧 | 240 |
| 输出音频 | AAC / 48000 Hz / 立体声 |
| 输出文件大小 | 约 21.1 MB |
| 导出耗时 | 9.43 秒 |
| 平均速度 | 25.45 fps |
| 25 fps 实时倍率 | 1.02× |
| 硬编回退原因 | 空 |
| 输出可重新解码 | 是 |

结论：完整工作流可以完成，但只有约 1.02 倍实时速度，余量很小。换成更复杂的多层、模糊或 Composite 图后会低于实时。

### 3.3 效果是否真正生效

将完整工作流导出与无效果 H264 基线进行同帧比较：

| 帧 | 与无效果基线的平均绝对像素差 |
|---:|---:|
| 0 | 10.736 |
| 60 | 3.345 |
| 119 | 2.917 |
| 120 | 0.889 |
| 180 | 0.000 |
| 239 | 0.000 |

结论：前 120 帧的 Blur/调色链路确实产生了像素变化；刀切后的后 120 帧与无效果基线一致。这证明局部片段效果和调色引用至少在当前导出场景中生效。

仍需补充：将 Blur、ColorStack、Composite Blur 分别单独开启，做三组像素差异和视觉参考，才能精确判断每一层效果的独立贡献。

---

## 4. 实测性能结果

所有矩阵均使用真实 `2K_h264.mp4`，1920×1080，25 fps，240 帧测量，并执行 30 帧预热。以下结果是本机当前运行环境的实测值，不代表所有 GPU 和平台的最终结果。

### 4.1 效果场景

| 场景 | 实测速度 | 相对 25 fps | 判定 |
|---|---:|---:|---|
| identity | 约 151.6 fps | 6.06× | 通过 |
| resize | 约 229–236 fps | 9.17–9.44× | 通过 |
| opacity fade | 约 69.6 fps | 2.78× | 通过 |
| cross dissolve | 约 67.3 fps | 2.69× | 通过 |
| grade | 约 27.7 fps | 1.11× | 勉强通过，余量不足 |
| sharpen | 约 28.7 fps | 1.15× | 勉强通过，余量不足 |
| glow | 约 26.2 fps | 1.05× | 勉强通过，余量不足 |
| Gaussian Blur | 约 26.1 fps | 1.04× | 接近实时边界 |
| two layer | 约 16.25 fps | 0.65× | 失败，低于实时 |

### 4.2 CPU/GPU 路径对照

#### 调色

| 路径 | FPS | 结论 |
|---|---:|---|
| adaptive | 28.80 | 最快，但余量不大 |
| forced GPU | 26.88 | 没有明显收益 |
| CPU render | 26.39 | 与 GPU 差距很小 |

调色强制 GPU 与 CPU 差距很小，说明瓶颈可能在颜色栈处理、帧传输、编码表面或其他 CPU 同步阶段，而不是单纯 shader 计算。

#### Gaussian Blur

| 路径 | FPS | 结论 |
|---|---:|---|
| adaptive | 23.97 | 低于 25 fps |
| forced GPU | 23.95 | 强制 GPU 没有改善 |
| CPU render | 8.17 | 明显更慢 |

GPU 对 Blur 有明显帮助，但 GPU 结果仍然没有达到安全的 30 fps 目标。

#### 双轨叠加

| 路径 | FPS | 结论 |
|---|---:|---|
| adaptive | 16.03 | 低于实时 |
| forced GPU | 17.11 | 仅小幅改善 |
| CPU render | 5.95 | 不可用 |

双轨是当前最明确的性能阻塞点。强制 GPU 只能从 16.03 fps 提高到 17.11 fps，说明多层合成存在较大的非 shader 开销或频繁 host↔GPU 往返。

### 4.3 GPU Trail 证据

效果矩阵的 `trail_fallback_reason` 多次报告：

```text
cpu_encoder_surface_path
```

同时 `hw_fallback_reason` 为空，说明“硬件编码器没有回退”与“渲染链路完全 GPU 化”是两个不同结论：当前编码器可用，但输出表面仍经过 CPU encoder surface path。该路径很可能是当前多层、Blur 和 Composite 速度不足的重要原因之一，需要单独优化和验证。

---

## 5. 问题和优化任务总表

### 5.1 P0：影响交付和实时性能的问题

| ID | 问题 | 证据 | 影响 | 优化方向 | 验收标准 |
|---|---|---|---|---|---|
| PERF-P0-001 | 双轨合成低于实时 | adaptive 16.03 fps，forced GPU 17.11 fps | 多层剪辑无法实时预览/快速导出 | 优化多层合成、减少中间纹理、合并 pass、减少读回和重复解码 | 1920×1080 H264 双轨达到至少 30 fps；p95 帧时小于 40 ms |
| PERF-P0-002 | Blur 接近/低于实时 | adaptive 23.97 fps，p50 约 38.8 ms | 常见模糊效果会拖慢交付 | 优化分离卷积、半分辨率策略、tile/cache、pass fusion | Gaussian Blur 达到至少 30 fps，质量门通过 |
| PERF-P0-003 | 完整工作流几乎没有性能余量 | 含音频、调色、效果和 Composite 约 25.45 fps | 机器负载、长时间线和复杂素材会立即掉帧 | 建立阶段耗时分解和 GPU trail，优先优化非编码阶段 | 完整典型工作流至少 30 fps，复杂场景不低于 25 fps |
| PERF-P0-004 | GPU 强制路径未显著提升多层性能 | two layer 16.03 → 17.11 fps | 当前 GPU 选路可能被 CPU 同步/读回抵消 | 统计每帧 CPU/GPU 时间、上传/下载字节、纹理分配和等待点 | GPU trail 能解释 95% 以上帧时间；无未解释同步 |

### 5.2 P1：影响功能完整性和自动化可靠性的问题

| ID | 问题 | 证据 | 影响 | 优化方向 | 验收标准 |
|---|---|---|---|---|---|
| FUNC-P1-001 | Composite 已能编辑和持久化，但缺少独立像素归因验证 | 综合输出成功，只有前后基线差异 | 无法证明每个 Composite 节点单独正确 | 增加节点旁路、单节点输出、逐层 hash/MAE/SSIM 验证 | source→color→blur→output 每层均有独立证据 |
| FUNC-P1-002 | Composite 片段挂载缺少明确的统一命令入口 | 测试中需要通过 `with_composite_id()` 完成绑定 | AI、UI、CLI 可能走不同路径 | 增加 `AttachCompositeCommand`、预览、revision、undo 和验证 | UI/AI/CLI 使用同一命令；可撤销、可重载 |
| FUNC-P1-003 | Color 仅验证基础 ColorStack | 本轮未跑 LUT、曲线、Qualifier、Mask、Tracker、RAW、Denoise | 不能宣称 Color 全功能正常 | 按功能矩阵逐项建立真实素材测试 | 每个功能都有输入、输出、质量和性能证据 |
| FUNC-P1-004 | Deliver 只验证 H264 主路径 | 本轮未验证 HEVC、ProRes、SRT、Stems、Batch、FCPXML/OTIO | 交付功能覆盖不足 | 建立编码器、音频、字幕、队列和批处理矩阵 | 每个交付类型都能 probe、重载和失败恢复 |
| FUNC-P1-005 | UI 真实操作尚未验收 | 原生桌面自动化无法连接 Qt 窗口 | 面板信号、拖拽和提示可能存在隐藏问题 | 在可用的桌面环境执行 UI 测试或增加 Qt QTest harness | UI 操作与业务命令结果一致 |
| FUNC-P1-006 | 媒体缩略图依赖的 FFmpeg 不可用 | 启动日志明确报告 `FFmpeg NOT found` | Media Pool/File Explorer 缩略图失败 | 统一 FFmpeg 发现、路径配置、版本诊断和 fallback | 启动日志无错误；真实 H264 缩略图可显示 |
| FUNC-P1-007 | 图标路径拼接错误 | `file:/...` 被拼到绝对路径前 | UI 图标缺失，说明资源 URL 处理有问题 | 统一 `QUrl`/本地路径转换，禁止重复拼接 | checkmark.svg 正常加载且无 Qt SVG 错误 |

### 5.3 P2：测试、环境和可维护性问题

| ID | 问题 | 证据 | 优化方向 | 验收标准 |
|---|---|---|---|---|
| TEST-P2-001 | 效果矩阵结果文件会被重复运行覆盖 | 同一 `side + date` 文件名被多次重写 | 文件名加入 run ID、场景、GPU 组和 git revision | 每次运行结果可独立追溯，旧结果不被覆盖 |
| TEST-P2-002 | `--only resize` 等筛选可能命中重复矩阵项 | resize 过滤得到 2 个相同配置 | 构建 specs 时去重并显示完整 ID | 同一配置只运行一次或明确标记重复原因 |
| TEST-P2-003 | benchmark 会留下大量临时 manifest/cache | `tests/media/results` 出现 warmup/measured/child/config 文件 | 增加临时目录和 finally 清理策略 | 测试结束只保留正式 JSON/MD/必要产物 |
| TEST-P2-004 | 启动环境 locale 不稳定 | Qt 自动将 C locale 切换为 UTF-8 | 启动脚本和测试 runner 预设 UTF-8 locale | 启动日志无 locale 修正警告 |
| TEST-P2-005 | QApplication 生命周期容易被临时对象回收 | 简化 demo 曾出现 `QObject::startTimer: current thread's event dispatcher has already been destroyed` | 所有测试保留 QApplication 强引用并统一退出顺序 | Qt 测试无 event dispatcher 警告 |
| TEST-P2-006 | 编码器可用与渲染完全 GPU 化的指标没有统一展示 | `hw_fallback_reason` 为空，但 trail 为 CPU surface path | 在报告中分离 decode/render/transfer/encode 四阶段 | 每个阶段有耗时、字节量和回退原因 |
| TEST-P2-007 | 完整工作流步骤计时需要更细粒度 | 当前功能步骤几乎为 0 秒，只有导出有耗时 | 对媒体导入、分析、渲染、Composite、编码分别计时 | 报告能给出阶段耗时总和和误差 |

---

## 6. 合理优化顺序

### 第 1 阶段：先恢复基础运行可靠性

优先处理：

1. FFmpeg 可执行文件发现和缩略图失败；
2. SVG `file:/` 路径拼接；
3. UTF-8 locale 启动保证；
4. QApplication 生命周期警告；
5. 测试结果和临时文件清理。

原因：这些问题会污染后续真实用户测试，使素材导入、缩略图、UI 和日志结论不可靠。

### 第 2 阶段：建立完整性能证据

在改算法前先增加：

- 每帧 decode 时间；
- CPU 合成时间；
- GPU dispatch 时间；
- host→GPU 上传字节；
- GPU→host 回读字节；
- 纹理创建和销毁次数；
- shader 编译/缓存命中；
- 编码器等待时间；
- 音频处理时间；
- frame queue 深度；
- p50/p95/p99 帧时；
- fallback reason；
- 每个节点和每个 effect 的耗时。

没有这些证据时，不应仅凭“强制 GPU”开关判断性能是否改善。

### 第 3 阶段：优化多层合成

建议顺序：

1. 检查双轨是否重复解码同一 H264 源；
2. 对同源片段增加解码帧共享；
3. 避免每层都回读 CPU；
4. 合并 opacity、blend、transform 等轻量 pass；
5. 增加中间纹理复用池；
6. 对无变化层启用缓存；
7. 对同一帧的多层使用批量 GPU dispatch；
8. 仅在真正需要时触发颜色空间转换；
9. 优化 GPU surface 到编码器的交接；
10. 重新运行 adaptive/forced GPU/CPU 三组对照。

目标：双轨 H264 1920×1080 从 16 fps 提升到至少 30 fps；若无法达到，应明确给出实时预览代理档和最终导出档两套策略。

### 第 4 阶段：优化 Blur、Glow、Sharpen 和调色

#### Blur

- 预览使用半分辨率或动态质量档；
- Gaussian 使用 separable kernel；
- 根据半径选择固定少量采样档；
- 复用 horizontal/vertical intermediate texture；
- 相邻 Blur/Color pass 做 fusion；
- 大半径效果使用 tile cache；
- 导出和预览使用独立 quality profile；
- 将半径、质量和帧时间加入 AI 可读诊断。

#### 调色

- 检查 ColorStack 是否每帧重复构建；
- 缓存 LUT/曲线/颜色变换；
- 合并 exposure、contrast、saturation、gain pass；
- 减少 HDR/色彩空间来回转换；
- 补充 GPU 与 CPU 路径的阶段耗时；
- 验证调色结果不因缓存而读取旧 stack。

### 第 5 阶段：补齐全功能真实素材矩阵

推荐每一项都执行：

```text
真实素材探测
  → 创建工程
  → 导入媒体
  → 创建时间线
  → 执行功能
  → 保存/重载
  → 预览
  → 导出
  → 输出 probe
  → 像素/音频/元数据验证
  → 性能统计
```

---

## 7. 后续全功能测试矩阵

### 7.1 Color

| 功能 | 测试素材 | 操作 | 需要验证 |
|---|---|---|---|
| 基础调色 | 2K H264 | exposure/contrast/saturation/gain | 像素差异、保存重载、速度 |
| 色轮 | 2K H264 | lift/gamma/gain/offset | RGB 通道变化、范围和 HDR |
| 曲线 | 2K H264 | 增删移动曲线点 | 曲线序列化和输出差异 |
| HSL 曲线 | 2K H264 | 修改特定色相 | 选择范围和输出像素 |
| LUT | 2K H264 + LUT | 导入、应用、强度、移除 | LUT 路径、缓存和输出 |
| Qualifier | 2K H264 | 取色、软化、反选 | matte 预览和边缘稳定 |
| Mask | 2K H264 | 创建、羽化、跟踪 | mask 持久化、GPU 和输出 |
| Tracker | tracking fixtures + H264 | 开始、暂停、重算 | 轨迹误差、失败恢复、速度 |
| Film Grain | 2K H264 | 类型、强度、种子 | 可复现性和性能 |
| Camera RAW | RAW/可用替代素材 | RAW 参数 | 元数据和色彩正确性 |
| Denoise/Deblur | 2K H264 | 模型、强度、质量档 | 模型状态、显存、输出质量 |

### 7.2 Edit

| 功能 | 操作 | 需要验证 |
|---|---|---|
| Blade/Split | 多个切点、边界切点 | 片段范围、源帧映射、撤销 |
| Trim | 入点、出点、滑移、滑动 | 不越界、音视频同步 |
| Move/Ripple | 移动、覆盖、波纹删除 | 轨道重排和标记移动 |
| Transition | Cross dissolve、其他转场 | 首尾帧、时长和性能 |
| Audio | 增益、淡化、静音、声道 | 音频波形、响度、导出可听 |
| Subtitle | 新建、编辑、样式、图形化 | 时间码、溢出、SRT |
| Scene detect | 分析并应用切点 | 候选准确性和撤销 |
| FCPXML/OTIO | 导入导出 | schema、媒体重连和报告 |
| Proxy | 生成、切换、离线 | 源/代理一致性和恢复 |

### 7.3 Composite

| 功能 | 操作 | 需要验证 |
|---|---|---|
| Default graph | 建立 source→color→output | 图结构、持久化、输出 |
| Add node | Blur/Grade/Transform/Glow | 节点 schema、默认参数 |
| Connect | 端口连线和替换 | 类型检查、循环检查 |
| Parameters | 静态值和参数批量修改 | 范围、单位、输出 |
| Keyframes | 增删关键帧、插值 | 时间采样、曲线和值 |
| Expressions | 设置和删除表达式 | 编译错误、依赖和缓存 |
| Group | 分组、解组 | graph schema 和渲染 |
| Remove | 删除节点 | 引用、重连、回滚 |
| Multi-layer | 两层、三层、混合模式 | FPS、GPU trail、视觉质量 |
| Composite export | 图进入时间线并交付 | 实际像素效果和输出探测 |

### 7.4 Deliver

| 功能 | 操作 | 需要验证 |
|---|---|---|
| H264 | yuv420p 8-bit | 编码、帧数、音频、速度 |
| HEVC 8-bit | yuv420p | profile、速度、解码 |
| HEVC 10-bit | yuv420p10le | 位深、色彩元数据 |
| ProRes | 可用 ProRes 源/输出 | 质量、码率和可重载 |
| Audio | AAC、不同采样率/声道 | 音画同步、响度 |
| SRT | 字幕导出 | 时间码和编码 |
| Stems | 分轨导出 | 轨道内容和命名 |
| Batch | 多任务队列 | 暂停、恢复、取消、重试 |
| Overwrite | 输出已存在 | 不误覆盖，策略可解释 |
| Path grant | 受限输出目录 | 越界拒绝、审计完整 |
| Post-export verify | 重新打开输出 | 错误不伪报成功 |

---

## 8. 标准验证协议

### 8.1 性能测试协议

每个性能场景固定：

- 同一素材和同一项目配置；
- 30 帧预热；
- 240 帧正式导出；
- 至少 3 次正式运行；
- 使用中位数作为主指标；
- 记录 p50、p95、p99；
- 单独记录编码、渲染、传输和音频耗时；
- 保存输入和输出 probe；
- 保存输出 hash；
- 保存 GPU trail；
- 记录环境、GPU、驱动、Python、Qt、PyAV 和 FFmpeg 版本。

### 8.2 功能通过标准

每项功能必须同时满足：

1. 操作成功返回；
2. 状态可读取；
3. 保存后重载一致；
4. 输出可解码；
5. 视觉/音频结果确实变化；
6. 非目标区域未被错误修改；
7. 撤销/重做一致；
8. 失败时没有半完成状态；
9. AI、UI、CLI 的结果一致；
10. 性能指标达到该功能的目标档位。

### 8.3 性能目标

| 场景 | 最低可接受 | 生产目标 |
|---|---:|---:|
| identity | 60 fps | 100 fps 以上 |
| resize/opacity/简单转场 | 30 fps | 60 fps 以上 |
| 单层调色 | 25 fps | 30–60 fps |
| 单层 Blur/Glow/Sharpen | 25 fps | 30 fps 以上 |
| 双轨混合 | 25 fps | 30 fps 以上 |
| 三轨及以上 | 25 fps | 25–30 fps，视质量档可降级 |
| 完整 AI 工作流导出 | 25 fps | 30 fps 以上 |

低于最低值时，必须在报告中明确标记为 `performance_failed`，不能只因为文件成功导出就标记为通过。

---

## 9. AI 控制专项验收

本轮已验证底层业务命令可以完成一条真实工作流，但尚未验证 AI 从自然语言端到端调用这些动作。后续需要将同一流程通过 AI 控制面重复执行：

```text
get_capabilities
  → get_project_snapshot
  → search_assets
  → create_project / create_timeline
  → import_media
  → propose_edit_plan
  → validate_edit_plan
  → preview_edit_plan
  → apply_edit_plan
  → apply_color_plan
  → validate_composite_edit
  → apply_composite_edit
  → request_export
  → get_job_status
  → verify_output
  → save_project
```

AI 控制验收还必须检查：

- 是否能发现 Color、Composite 和 Deliver 的真实能力；
- 是否会误报当前关闭的 feature flag；
- 是否能处理 revision 冲突；
- 是否能处理 GPU 不可用；
- 是否能解释 `cpu_encoder_surface_path`；
- 是否会在导出失败时报告失败；
- 是否能等待导出结束而不是提前返回；
- 是否能读取像素、音频和元数据验证结果；
- 是否能撤销或恢复失败的跨域任务。

---

## 10. 建议的任务执行顺序

### P0

- [ ] 修复 FFmpeg ThumbEngine 发现和诊断；
- [ ] 修复 `checkmark.svg` 路径；
- [ ] 固定 UTF-8 locale；
- [ ] 修复 QApplication 生命周期测试警告；
- [ ] 修复 benchmark 结果覆盖和临时文件清理；
- [ ] 为 GPU trail 增加完整阶段耗时。

### P1

- [ ] 优化双轨合成，目标从 16 fps 提升到 30 fps；
- [ ] 优化 Blur，目标从 24 fps 提升到 30 fps 以上；
- [ ] 分析调色 CPU/GPU 差距过小的原因；
- [ ] 消除或解释 `cpu_encoder_surface_path`；
- [ ] 增加 `AttachCompositeCommand`；
- [ ] 增加 Composite 单节点输出验证；
- [ ] 补齐 Color 和 Deliver 的真实测试工具。

### P2

- [ ] 增加 UI QTest 或可用桌面环境中的真实点击测试；
- [ ] 完成 LUT、曲线、Qualifier、Mask、Tracker、Denoise、RAW 测试；
- [ ] 完成 HEVC、ProRes、SRT、Stems、Batch 测试；
- [ ] 完成三轨、多节点、复杂表达式和长时间线测试；
- [ ] 将完整测试结果接入 CI 发布门禁。

---

## 11. 本轮测试的最终结论

### 已确认正常

- 真实 H264 素材可以被探测和解码；
- 项目、时间线、视频轨和音频轨可以创建；
- 刀切命令正常；
- Blur 效果可以加载和设置；
- ColorStack 可以创建、修改和挂载；
- Composite 图可以创建、验证、修改和持久化；
- `.topos` 工程可以保存和重载；
- H264 含音频导出可以成功完成；
- 输出视频和音频可以重新解码；
- 局部效果经过像素差异验证确实生效；
- H264 硬件编码没有发生回退。

### 已确认需要优化

- 双轨合成明显低于实时；
- Blur 已接近实时边界；
- 调色、Sharpen、Glow 的性能余量不足；
- 强制 GPU 没有解决多层和 Blur 的主要瓶颈；
- GPU trail 显示仍存在 CPU encoder surface path；
- 缩略图 FFmpeg 依赖缺失；
- SVG 资源路径错误；
- UI 原生交互尚未完成真实验收；
- Color、Composite、Deliver 仍需扩展完整真实素材矩阵；
- benchmark 结果和临时文件管理需要改进。

因此，当前最优先的工作不是继续增加更多 AI 工具，而是先把真实媒体的多层合成、Blur、GPU surface 路径、缩略图依赖和 UI 验收基础修好。只有当这些问题通过真实 H264 矩阵后，再继续扩大 AI 全功能自主控制范围，才能避免 AI 自动调用一个速度不足或质量不可验证的底层系统。

---

## 12. 第二轮扩展测试：尽可能覆盖全部功能后的结果

### 12.1 本轮测试口径

本轮目标是把“真实用户工作流”和“已有自动化功能套件”合并验收。所有能够在当前环境运行的测试都执行；无法执行的项目必须记录阻塞原因，不以跳过代替通过。

本轮采用三种证据等级：

| 证据等级 | 含义 | 本轮示例 |
|---|---|---|
| A：真实素材业务链路 | 使用真实视频/音频，调用应用项目、时间线、效果、合成和导出服务 | H264 全流程、多机位、嵌套音频、真实 Edit 播放 |
| B：应用核心自动化 | 不一定显示原生 UI，但运行真实模块、服务、Qt 对象和持久化逻辑 | Color、Deliver、AI Editing、Media、Motion 测试套件 |
| C：暂不能判定 | 测试被原生崩溃、死锁、缺少外部工具或无 OpenGL 环境阻断 | Composite UI、macOS VT、Color 页面真实拖拽 |

“通过”只代表对应证据等级的范围通过，不能把 B 级单元测试解释为 A 级真实用户 UI 已通过。

### 12.2 真实 H264 业务链路结果

素材仍以 `tests/test_videos/2K_h264.mp4` 为主：1920×1080、25 fps、H.264/yuv420p、AAC 48 kHz、约 62.88 秒。

| 业务场景 | 结果 | 关键证据 |
|---|---|---|
| 项目/时间线/视频轨/音频轨创建 | 通过 | 项目和序列成功创建，音视频轨可读回 |
| 素材导入、媒体探测、H264 解码 | 通过 | 真实源文件可解码，帧数/帧率/分辨率一致 |
| 刀切 | 通过 | 第 120 帧分割，片段边界和源帧映射一致 |
| Blur 效果 | 通过 | Gaussian Blur 参数写入，真实导出像素与 identity 基线产生预期差异 |
| ColorStack 调色 | 通过 | Exposure/Contrast/Saturation/Gain 持久化并作用于首片段 |
| Composite 图编辑 | 通过 | 图创建、节点添加、连接、保存和重载通过；单节点像素贡献还需隔离验证 |
| 工程保存/重载 | 通过 | `.topos` 保存后重载，序列、效果和合成结构保留 |
| 含音频 H264 生产导出 | 通过 | 240 帧视频 + AAC 音频，输出可重新解码 |
| 输出质量检查 | 通过 | 视频流、音频流、时长、帧数、音频存在性均通过 |
| 完整工作流速度 | 需要优化 | 240 帧耗时 9.4316 秒，25.446 fps；能完成，但未达到 30 fps 生产目标 |

产物：

- [完整工作流 JSON](../bench_out/real_user_workflow_h264_full/workflow_result.json)
- [完整工作流工程](../bench_out/real_user_workflow_h264_full/ai_real_h264_workflow.topos)
- [完整工作流导出](../bench_out/real_user_workflow_h264_full/ai_real_h264_workflow_export.mp4)

### 12.3 真实素材批次 D/E/F

通过静态 ffmpeg 路径补齐依赖后，批次 D、E 使用真实 H264 副本和真实音频完成；批次 F 使用仓库中现有真实 `.topos` 工程进行迁移验证。原脚本固定寻找不存在的 `2K_h264.topos`，已用现有 `4K_prores422HQ.topos` 做临时真实工程别名完成验证，别名已移出仓库。

| 批次 | 内容 | 结果 |
|---|---|---|
| D | 三机位真实副本、波形互相关同步、偏移写回、切换点、角度网格取帧 | 11/11 通过；camB = −12 帧，camC = −31 帧，置信度 1.0 |
| E | 子序列嵌套真实音频、父序列生产导出、非静音检查、源区段互相关 | 7/7 通过；音频 RMS 0.01927，相关置信度 0.958 |
| F | 真实 `.topos` v1→v3 迁移、metadata 修复、legacy Blur 映射、保存重载、未来版本兼容 | 7/7 通过 |

结论：多机位同步、嵌套音频和工程迁移已经具有真实素材证据；但多机位控制器的 Qt 结果对话框在无桌面 OpenGL 环境下仍会触发 abort，见 §14 的 CRASH-P0-002。

### 12.4 真实 Edit 播放和音频播放

#### Edit 视频轨播放

驱动：`tests/benchmark/drive_real_app_edit_tracks.py tests/test_videos/2K_h264.mp4 6`

| 指标 | 结果 | 判定 |
|---|---:|---|
| timeline FPS | 25.0 | 通过 |
| 6 秒有效播放速度 | 24.94 fps | 通过，接近实时 |
| 稳态播放速度 | 25.00 fps | 通过 |
| distinct presented frames | 149 | 基本通过 |
| playhead 预期推进 | 150 帧 | 通过 |
| 丢帧 | 1 | 需要持续观察 |
| 长停顿（≥0.5 秒） | 0 | 通过 |
| telemetry total_ms p50/p95 | 21.228 / 30.255 ms | 通过 25fps 预算 |
| GPU pipeline | 未启用 | 当前环境限制，不能代表真实 Metal 机器 |

驱动日志：`bench_out/extended_real_tests_20260917/edit_tracks_offscreen.log`。

注意：由于当前会话是 `QT_QPA_PLATFORM=offscreen`，trace 显示 `no_gpu_pipeline` 和 `cpu_qimage_upload`。这个结果可以证明 Edit 时间推进、解码请求和播放控制逻辑能工作，但不能作为 GPU 播放性能的最终结论；必须在真实桌面 Metal 环境再跑一次。

#### 音视频轨实时播放

驱动：`tests/benchmark/drive_real_app_audio_tracks.py`，真实 H264 视频 + 从同一真实源提取的 PCM WAV，12 秒。

| 指标 | 结果 | 判定 |
|---|---:|---|
| 有效视频帧率 | 24.82 fps | 通过，99% realtime |
| 音频时钟推进 | 0.997× realtime | 通过 |
| AudioRingBuffer underrun | 0 | 通过 |
| AudioRingBuffer overflow | 0 | 通过 |
| playhead | 298 / 约 300 帧 | 基本通过 |
| video presented | 23.9/s | 接近实时 |

结论：基础 A/V 播放链路稳定；音频重 FX、多音频轨、转场和导出音频一致性仍需在真实桌面和多轨矩阵中单独测量。

### 12.5 自动化测试套件结果

#### 已完整通过或在明确排除环境阻塞后完整通过

| 套件 | 结果 | 备注 |
|---|---:|---|
| `tests/edit/ai_editing` | 1923 passed, 1 skipped | 加入虚拟环境静态 ffmpeg/ffprobe 路径后，含真实素材探测矩阵全部通过 |
| `tests/edit/ai_editing/test_real_media_probe_matrix.py` | 10/10 passed | 证明此前 10 个 `PROBE_UNAVAILABLE` 是依赖发现问题，不是素材或解析逻辑失败 |
| `tests/color` | 1511 passed, 4 skipped | 暂排除会触发非法指令的 `test_cache_fix.py` |
| `tests/deliver` | 142/142 passed | 导出格式、队列、预检、批量导出面板核心测试通过 |
| `tests/media` 非 macOS VT 组 | 2879 passed, 1 failed, 61 skipped | 详见 §14 的锁类型问题和析构警告 |
| `tests/scripting + tests/motion + tests/integration + tests/feature_availability` | 201 passed, 1 skipped | 脚本、运动、MCP、本地 IPC、教学动画可用性通过 |
| `tests/edit/test_real_media_edit_smoke.py` | 4/4 passed | 真实素材编辑 smoke |
| `tests/edit/test_end_to_end_playback_real_videos.py` | 11/11 passed | 真实视频播放端到端 |
| `tests/edit/test_nested_sequence_production.py` | 22/22 passed | 嵌套序列生产路径 |
| `tests/edit/test_timeline_export_effects.py` | 29/29 passed | 时间线效果导出 |
| `tests/edit/test_effect_render_operations.py` | 8/8 passed | 效果渲染操作 |

#### 部分通过、被崩溃/挂起阻断的套件

| 套件 | 结果 | 阻断点 |
|---|---|---|
| 全功能集合 | 收集 18,501 项 / 1 skipped | 在 Composite 测试阶段原生段错误，退出码 139 |
| `tests/composite/test_3d_preview_override.py` | 60 passed, 6 failed | `_on_view_mode_changed` 间接调用 `_exit_group_view`，fixture 未初始化 QWidget 超类，抛 `RuntimeError` |
| `tests/composite/test_composite_cornerpin_freeze.py` | 前 5 项通过后 segfault | Qt 事件处理期间进入 `CompositePage` lambda，需查对象生命周期/信号回调 |
| `tests/composite/test_composite_transform_move.py` | 未完成 | 同一 Composite UI 事件路径触发 segfault |
| `tests/edit/test_audio_leftover_samples.py` | 12 passed, 4 failed | H265/ProRes 在 packet boundary 和 seek 后采样连续性错误 |
| `tests/edit/test_audio_waveform.py` | 未完成 | `_start_worker` 内发生原生 segfault |
| `tests/edit` 核心大组 | 多次约 55% 后停住 | ADR、录音相关 Qt 测试出现 CPU=0 的无限等待/事件循环不退出 |
| `tests/edit/test_multicam_production.py` | 前 15 项通过后 abort | `MulticamSyncResultDialog` 在 headless Qt 环境创建时 abort |

完整日志保存在 `bench_out/extended_real_tests_20260917/` 下，包含每组命令的原始 stdout/stderr。

### 12.6 真实效果速度结果汇总

以下为 H264 1920×1080 真实素材、240 帧计时段的导出矩阵；单位为 fps，实际值以对应 JSON/Markdown 报告为准。

| 场景 | 观测速度 | 与 25fps 实时关系 | 下一阶段目标 |
|---|---:|---|---:|
| identity | 约 151.6 | 充足 | 保持，作为回归基线 |
| resize | 约 229–236 | 充足 | 保持 |
| opacity fade | 约 69.6 | 充足 | 保持 |
| cross dissolve | 约 67.3 | 充足 | 保持 |
| grade | 约 27.7–28.8 | 接近实时 | p95 稳定低于 33.3ms |
| blur | 约 24.0–26.2 | 临界/略低 | 稳定达到 30fps |
| sharpen | 约 28.7 | 接近实时 | 达到 30fps |
| glow | 约 26.2 | 接近实时 | 达到 30fps |
| two layer | 约 16.0–17.1 | 明显低于实时 | 第一阶段提升到 30fps |
| 完整工作流 | 25.446 | 刚好实时 | 达到 30fps，且不能牺牲音频同步 |

CPU/GPU 对照说明：`forced_gpu` 对 Blur 和双层混合没有带来决定性收益，部分场景仍记录 `cpu_encoder_surface_path`。这表示当前主要瓶颈不只是“是否打开 GPU”，而是渲染结果在 GPU、CPU、编码器 surface 之间的交接和多层资源生命周期。

---

## 13. 第二轮新增问题清单

### P0：不修复就不能宣称 AI 可以自主运行

| ID | 问题 | 证据 | 修复方向 | 验收标准 |
|---|---|---|---|---|
| CRASH-P0-001 | Composite CornerPin/Transform UI 事件路径触发原生段错误 | `test_composite_cornerpin_freeze.py` 前 5 项后 segfault；`test_composite_transform_move.py` 也触发 | 检查 CompositePage lambda、QObject/信号连接、GL 上下文、页面销毁和测试 fixture；禁止已销毁页面回调 | 两个文件独立运行 3 次均不崩溃；完整 Composite 套件可继续执行 |
| CRASH-P0-002 | 多机位同步完成回调创建结果对话框时 abort | `test_multicam_production.py` 在 `MulticamSyncResultDialog.__init__` abort | 将同步结果作为数据返回与 UI Dialog 解耦；headless 模式禁止创建模态对话框，改用信号/结果对象 | headless 和桌面模式都能完成同步；取消/确认均有明确结果 |
| CRASH-P0-003 | AudioWaveform worker 启动路径触发原生段错误 | `test_audio_waveform.py::test_request_new_waveform_starts_worker`，堆栈在 `audio_waveform.py:_start_worker` | 检查 QThread/QObject 所在线程、重复 worker、析构时机和 queued signal；增加 worker 状态机 | 单测 20 次循环无崩溃；工程打开/关闭期间无后台线程残留 |
| DEADLOCK-P0-004 | ADR 和录音相关 Qt 测试事件循环不退出 | `test_adr_take_chain.py`、`test_adr_take_sheet_ui.py`、`test_record_arm_wiring.py`、`test_recording_import_transaction.py` CPU=0 长时间无输出 | 所有 modal dialog、QTimer、QThread、录音设备 fake 必须有超时和 teardown；加入进程级 watchdog | 每个 UI 测试 60 秒内结束；超时产生诊断而不是静默挂死 |
| ENV-P0-005 | ffmpeg/ffprobe 发现依赖 PATH，导致真实素材探测和缩略图失败 | 未加 PATH 时 ThumbEngine 报 `FFmpeg NOT found`，AI 媒体矩阵 10 个 `PROBE_UNAVAILABLE`；加静态二进制后全部通过 | 启动时使用统一 `ffmpeg_locator`，把打包目录、venv static_ffmpeg、PATH 写入诊断；所有子进程复用同一解析结果 | 无 PATH 的打包/开发启动仍能探测 H264；启动诊断明确二进制版本和路径 |

### P1：功能正确性和可恢复性

| ID | 问题 | 证据 | 修复方向 | 验收标准 |
|---|---|---|---|---|
| FUNC-P1-008 | 音频 packet boundary/seek 后样本连续性异常 | H265、ProRes 各有 2 个失败；出现巨大采样值和 leftover 丢失提示 | 统一 PyAV/AudioDecoder 的 sample format、缩放、planar/interleaved 处理；seek 时原子清空 leftover 和 resampler 状态 | H264/H265/ProRes 采样连续性误差 ≤0.01；seek 首 100 样本与基线一致 |
| FUNC-P1-009 | `ClipSession.decode_lock` 锁类型契约不一致 | Media 组 `test_session_defaults` 期待 `_thread.lock`，实现为 `_thread.RLock` | 明确是否需要可重入；若不需要换普通 Lock，若需要更新契约并验证递归调用点 | 锁语义文档化，单测与实现一致，避免隐藏死锁 |
| FUNC-P1-010 | `WindowsHwPlanarSource` 析构时可能缺少 `_gop_seek_container` | Media 测试 warnings：`AttributeError` in `FFmpegMediaSource.__del__` | 构造阶段初始化所有资源字段；析构使用 `getattr` 防御性关闭 | 资源析构无 unraisable exception，开启 `-W error` 仍通过 |
| FUNC-P1-011 | Composite 视图切换 fixture 对 QWidget 超类依赖不安全 | `test_3d_preview_override.py` 6 项 RuntimeError | 页面方法不得假设未初始化的 `_undo_stack`/QObject 状态；测试工厂提供合法实例或纯数据 seam | 66 项全部通过，视图切换不改变 2D/3D 节点选择语义 |
| FUNC-P1-012 | 工程迁移测试固定引用不存在的 `2K_h264.topos` | 原批次 F 在打开 zip 前 FileNotFoundError；仓库实际存在 `4K_prores422HQ.topos` | 由 manifest 选择可用 `.topos` fixture，或把 fixture 纳入 `benchmark_media_manifest.json` | 新机器缺少某个命名素材时，测试给出清晰 skip/fixture error，不直接崩溃 |
| UI-P1-013 | 当前 offscreen 环境无法完成真实 Color/Composite 原生 UI 验收 | `QOpenGLWidget: Failed to create context`；Color scrub 和 Composite playback 不能完成 | 增加 macOS 桌面 Metal runner；headless 只跑无 GL 核心路径，不把 QOpenGLWidget 失败算业务通过 | 桌面 runner 可完成导入→页面切换→拖拽→参数修改→播放→关闭；每步有 UI 断言 |

### P1：性能和质量

| ID | 问题 | 证据 | 优化方向 | 验收标准 |
|---|---|---|---|---|
| PERF-P1-014 | 双层合成约 16–17fps | 真实 H264 效果矩阵 | 合并 layer pass、减少中间纹理、复用 FBO/texture、避免每层 GPU→CPU、只在最终输出 readback | 2K H264 双层至少 30fps；p95 frame time ≤33.3ms |
| PERF-P1-015 | Blur 约 24–26fps，forced GPU 未显著改善 | Blur 三组对照 | separable blur、固定半径 shader、低分辨率中间 buffer、纹理池复用、减少 surface 同步 | Gaussian Blur 半径 8 在 2K 达到 ≥30fps；质量 SSIM/边缘误差受控 |
| PERF-P1-016 | Grade/Sharpen/Glow 只有 26–29fps | 效果矩阵 | 合并调色 pass、预计算常量、限制不必要的 full-frame readback、对 Glow 采用 downsample + upsample | 每效果 ≥30fps；p50/p95 都纳入门禁 |
| PERF-P1-017 | GPU trail 仍出现 `cpu_encoder_surface_path` | 导出报告 fallback reason | 拆分“GPU 渲染能力”和“编码器 surface 能力”；实现 Metal texture→VideoToolbox surface 的零拷贝路径，不能用强制 GPU 掩盖回退 | 报告分别给出 decode/render/compose/encode path；可解释每次回退 |
| PERF-P1-018 | 完整工作流仅 25.446fps | H264 240 帧导出 | 先解决多层/Blur，再优化导出队列、音频 mux 和首帧准备；不能用减少验证帧数伪造提速 | 完整工作流 3 次平均 ≥30fps，无音画漂移，输出可重开 |

### P2：测试基础设施和可观测性

| ID | 问题 | 证据 | 修复方向 |
|---|---|---|---|
| TEST-P2-019 | EXR cache 测试在当前环境触发非法指令 | `test_cache_fix.py` 在 `np.random.rand(2000,2000,3)` 处 illegal instruction | 将大图生成改为兼容的确定性分块/零拷贝 fixture，并在 CI 标记 CPU 指令集；保留 EXR 逻辑验证 |
| TEST-P2-020 | 部分长套件只能在崩溃/挂起后得到半截结果 | 全量 18,501 项在 Composite abort；Edit 多次 CPU=0 | 每个 UI/Metal/VT 组拆成独立进程，超时、信号退出、stderr、最后测试名写入 JSON |
| TEST-P2-021 | UI 测试错误地依赖 modal dialog 和 OpenGL | Color/Composite/Multicam 驱动在 offscreen 环境失败 | 所有 UI 测试增加 `headless_safe` 标记；Dialog 提供非模态/可注入 presenter |
| TEST-P2-022 | 性能计时没有完整区分各阶段 | 当前主要只有 wall FPS 和 fallback reason | 每帧记录 decode、upload、effect、compose、readback、encode、mux、audio，输出 p50/p95/p99 |
| TEST-P2-023 | 结果文件命名和临时缓存需要更强隔离 | 矩阵脚本会重复场景，历史报告容易覆盖 | 每次 run 使用 UUID/commit/source hash；manifest、配置、日志和结果归档在同一目录 |
| TEST-P2-024 | SVG 图标资源路径拼接错误 | 启动日志把 `file:` 当普通路径拼成 `.../file:/...` | 统一使用 `QUrl.fromLocalFile` 或纯绝对路径，不重复拼接资源根目录 |

---

## 14. 下一阶段优化开发顺序

### 阶段 0：先恢复可靠的测试和运行环境

顺序不能颠倒。AI 如果拿不到稳定的能力结果、任务状态和错误码，就无法自主重试或回滚。

1. 统一 ffmpeg/ffprobe 定位，启动时输出实际路径、版本、支持的编码器和硬件能力。
2. 将所有长测试改为进程隔离，增加 60 秒默认 watchdog；超时输出最后测试名、线程列表、Qt 窗口列表和资源计数。
3. 修复 `.topos` fixture 自动发现和 manifest 校验，避免固定文件名导致假失败。
4. 修复 SVG 路径、UTF-8 locale、QApplication 生命周期和析构 warnings。
5. 建立一台真实 macOS Metal 桌面 runner；offscreen runner 只负责核心数据/服务测试。

阶段 0 的验证：

- 无 PATH 的干净 shell 仍能完成 H264 probe 和缩略图生成；
- 所有被测命令都有结构化结果：`succeeded/error_code/duration/output_path/metrics`；
- 任一崩溃或超时不会阻塞后续测试；
- 同一测试连续运行 20 次不会产生随机 abort/segfault。

### 阶段 1：修复 P0 崩溃、死锁和线程生命周期

1. Composite：先修复页面销毁、QObject 信号断开和 GL 上下文所有权，再处理 Transform/CornerPin 交互。
2. AudioWaveform：为 worker 建立 `created → running → finishing → stopped` 状态机，所有 queued signal 在 stopped 后丢弃。
3. Multicam：将同步 worker 的结果对象与 Dialog 分离；headless 直接返回结果，桌面 UI 异步展示。
4. ADR/录音：为 QTimer、QThread、modal dialog、fake device 加入显式 `stop/close/wait(timeout)`。
5. 所有跨线程对象在日志中输出 `object_id/thread_id/generation`，能够追踪过期回调。

阶段 1 的验证：

- `test_composite_cornerpin_freeze.py`、`test_composite_transform_move.py`、`test_audio_waveform.py`、`test_multicam_production.py` 独立运行 3 次无原生退出；
- ADR/录音测试每项 60 秒内结束；
- 在打开、切页、关闭工程后，线程数、GPU frame 引用数、QTimer 数量回到基线；
- AI 任务收到取消请求时，不再创建新 Dialog、不再提交新渲染请求。

### 阶段 2：修复媒体/音频正确性

1. 先修复 H265/ProRes 的 sample format 归一化和 packet leftover 连续性。
2. 明确 seek 的原子边界：停止消费、清空 leftover、重置 resampler、更新 generation、再允许新包进入。
3. 统一 `ClipSession.decode_lock` 契约，普通互斥和可重入锁不能靠实现细节猜测。
4. 修复 `WindowsHwPlanarSource` 资源字段初始化和析构路径。
5. 为每个输出增加视频帧数、音频采样数、时间戳单调性、首尾静音和源区段互相关检查。

阶段 2 的验证：

- H264/H265/ProRes/10-bit 各跑 packet boundary、seek、短片段循环 20 次；
- 音频连续性误差 ≤0.01，音频采样数与 timeline duration 误差不超过 1 个 packet；
- 输出重新打开后，视频/音频流都存在且时间戳单调；
- 中途取消导出不会留下半完成工程或锁住媒体文件。

### 阶段 3：GPU/CPU 管线性能优化

按收益排序：

1. 双层合成：先减少中间 texture 和 FBO，保证 layer 合并在 GPU 内完成。
2. Blur/Glow：采用 separable/downsample shader，并对 radius、quality 做质量档位。
3. Grade/Sharpen：合并 pass，避免单独的 full-frame readback。
4. 打通 Metal surface 到 VideoToolbox 的零拷贝；若硬件不能支持，显式选择 CPU encode 并在 UI/AI 结果里说明。
5. 对效果做静态参数缓存，对不变的 shader、uniform、LUT、scope 输入复用。
6. 将音频处理批量化，确认重 FX 不阻塞视频解码线程。

性能验收门槛：

| 场景 | 当前观察 | 第一阶段门槛 | 稳定生产目标 |
|---|---:|---:|---:|
| identity | 151.6fps | ≥100fps | ≥120fps |
| resize/fade/transition | 67–236fps | ≥60fps | ≥60fps |
| grade | 27–29fps | ≥30fps | ≥45fps |
| blur/sharpen/glow | 24–29fps | ≥30fps | ≥45fps |
| two layer | 16–17fps | ≥30fps | ≥45fps |
| 完整 H264 AI 工作流 | 25.4fps | ≥30fps | ≥40fps |

每个场景至少 3 次，报告平均值、p50、p95、p99、首帧耗时、峰值内存和 fallback reason。只报告平均 fps 不足以证明“跑得快”。

### 阶段 4：补齐所有用户功能的真实素材矩阵

在 P0 崩溃和 P1 音频/性能问题修复后，按下表执行：

| 功能域 | 真实操作组合 | 必须覆盖的素材/输出 |
|---|---|---|
| Edit | 导入、拖轨、刀切、修剪、移动、复制、速度、冻结帧、转场、撤销重做 | H264 8-bit；H265 8/10-bit；ProRes；VFR |
| Audio | 多轨、链接音频、音量/声像、自动化、EQ、压缩、混响、延迟、转场、stem | AAC、PCM WAV、多声道、48k/44.1k |
| Color | Exposure、曲线、LUT、Qualifier、Power Window、Mask、Tracker、Denoise、HDR | H264、10-bit HEVC、ProRes 422/4444、RAW/高动态素材 |
| Composite | video frame、merge、blur、transform、corner pin、mask、3D、keyframe、表达式、嵌套 | 单节点/多节点/多层，2K 和 4K |
| Text/Subtitle | 导入、编辑、样式、位置、动画、烧录、旁车字幕 | SRT/VTT/ASS，CJK 字体，透明背景 |
| Motion | easing、spring、expression、retime、motion graphic generator | 短片段、长时间线、嵌套图形 |
| Multicam | 同步、切角度、角度网格、音频跟随、保存重载、导出 | 3–4 机位，不同分辨率和偏移 |
| Media/Project | 缩略图、代理、离线/重连、归档、迁移、未来 schema | 真实容器和缺失文件场景 |
| Deliver | H264、HEVC、ProRes、GIF、批量、队列、取消、失败重试、预检 | 各平台分辨率、音频、字幕、色彩空间 |

每一行都要同时保留：功能结果、输出 probe、像素/音频质量、性能结果、可重试性、工程重载结果。

### 阶段 5：把真实测试转成 AI 自主控制验收

同一套场景必须用 AI 工具链再执行一次，禁止只测 Python 内部函数：

```text
发现能力
  → 获取项目快照
  → 选择真实 H264 素材
  → 创建项目/时间线/轨道
  → 导入媒体并验证媒体状态
  → 生成 Edit 计划
  → 校验并预览计划
  → 原子应用剪辑/效果/调色/合成
  → 保存 revision
  → 请求导出并轮询 job
  → probe 输出并做像素/音频验收
  → 保存工程、生成报告
```

AI 专项必须验证：

1. AI 能发现真实可用能力，不把隐藏或关闭的 feature 当成可用；
2. 每个动作有 schema、参数范围、前置条件、后置条件和错误码；
3. 重复调用同一个 request_id 不会重复创建片段或重复导出；
4. revision 冲突会重新读取快照、重新规划，而不是覆盖用户修改；
5. GPU、ffmpeg、媒体离线、导出失败时能自动降级或明确请求用户；
6. AI 不会在 job 尚未完成时宣称“导出成功”；
7. 失败的跨域工作流可回滚，至少能恢复到上一个工程 revision；
8. AI 能解释最终速度、fallback、质量检查和未完成步骤；
9. 同一任务通过 AI、CLI、直接服务调用得到相同的业务结果；
10. 每个长任务有取消、暂停、恢复、重试和超时策略。

---

## 15. 下一阶段建议的执行看板

| 优先级 | 任务 | 依赖 | 完成定义 |
|---|---|---|---|
| P0-1 | 统一 ffmpeg/ffprobe 定位 | 无 | 干净 PATH 下 H264 probe、缩略图、AI 交付分析全部通过 |
| P0-2 | Composite segfault 修复 | P0-1 | CornerPin/Transform/视图切换 3 次稳定运行 |
| P0-3 | AudioWaveform worker 生命周期 | 无 | 20 次创建/关闭无崩溃和线程泄漏 |
| P0-4 | Multicam/ADR/录音 UI 非模态化和 watchdog | 无 | headless 60 秒内结束，桌面模式结果一致 |
| P1-1 | H265/ProRes 音频 leftover/seek 修复 | 无 | 4 个失败场景全部通过，扩展到 20 次循环 |
| P1-2 | ClipSession lock 和 Windows 析构修复 | 无 | Media 组无失败、无 unraisable warning |
| P1-3 | Metal runner 和 GPU telemetry | P0-1 | 能区分 decode/render/compose/encode/mux 的耗时和回退 |
| P1-4 | 双层/Blur/Glow/Grade 性能优化 | P1-3 | 效果矩阵达到阶段 3 门槛 |
| P1-5 | Composite 单节点像素 attribution | P0-2 | 可证明每个节点对输出产生预期贡献 |
| P2-1 | 修复 fixture、日志、报告归档 | P0-1 | 每次测试结果唯一目录，可复现可追溯 |
| P2-2 | 补字幕/文本/跟踪/批量 Deliver 真实素材 | P0/P1 稳定后 | 阶段 4 所有功能域至少一条 A 级证据 |
| P2-3 | AI 端到端全链路回放 | P0/P1/P2-2 | AI 能独立完成并验证 H264 编辑到导出 |

---

## 16. 第二轮结论

当前项目不是“所有功能都坏”，相反，核心编辑、导出、Color 核心、Deliver、Media、Motion、AI Editing 的业务基础已经有较强测试覆盖；真实 H264 主工作流、音频播放、多机位同步、嵌套音频和工程迁移也都能跑通。

但当前还不能承诺“AI 可以任意自由操控整个软件并且所有效果都很快”，原因是：

1. Composite、AudioWaveform、Multicam UI 和部分录音/ADR 测试仍有原生崩溃或挂起；
2. ffmpeg/ffprobe 运行时发现不一致，会让 AI 误判媒体不可用；
3. H265/ProRes 音频 packet boundary 和 seek 存在真实正确性失败；
4. 双层合成、Blur、Glow、调色性能没有达到统一的 30fps 生产门槛；
5. 当前环境无桌面 Metal，Color/Composite 原生 UI 和最终 GPU 速度还没有 A 级证据；
6. AI 控制面虽然有 1923 项自动化测试通过，但完整真实用户工作流还需要通过 AI 工具接口回放，并验证重试、取消、回滚和输出质量。

因此下一阶段的正确顺序是：先修复 P0 运行稳定性和外部依赖，再修复音频正确性，然后优化双层/Blur/GPU surface，最后扩大真实素材功能矩阵并进行 AI 端到端回放。完成阶段 0–3 之前，不建议把项目标记为“全功能自主控制完成”。

---

## 17. 第三轮：使用 TOPOS 422Proxy 的逐项功能测试

### 17.1 测试口径和素材

本轮使用的主素材是：

- `tests/test_videos/2K_topos_proxy.mov`
- 1920×1080，25fps，96 帧
- TOPOS 422Proxy，`yuv422p10le`，10-bit，Topos bitstream major 7
- 色彩元数据：BT.709、limited range

项目里的 `dist/opensource/.../topos_codec.dylib` 和 `build/prof/relcur` 动态库无法打开该素材，均返回 `tpcC magic/version 不符`；`native/topos_codec/build/release/topos_codec.0.1.0.dylib` 可以正常打开、探测和解码。这个差异已记录为发布依赖问题，后续不能让 AI 自己猜测动态库版本。

本轮新增的可复现驱动：

- [bench_real_topos_edit_effects.py](/Users/testuser/Documents/vscode/topos_1/scripts/bench_real_topos_edit_effects.py)
- [bench_real_topos_color_tools.py](/Users/testuser/Documents/vscode/topos_1/scripts/bench_real_topos_color_tools.py)
- [bench_all_composite_nodes_real_topos.py](/Users/testuser/Documents/vscode/topos_1/scripts/bench_all_composite_nodes_real_topos.py)
- [real_topos_edit_effect_matrix.json](/Users/testuser/Documents/vscode/topos_1/bench_out/extended_real_tests_20260917/real_topos_edit_effect_matrix.json)
- [real_topos_color_tools.json](/Users/testuser/Documents/vscode/topos_1/bench_out/extended_real_tests_20260917/real_topos_color_tools.json)
- [real_topos_all_composite_nodes.json](/Users/testuser/Documents/vscode/topos_1/bench_out/extended_real_tests_20260917/real_topos_all_composite_nodes.json)

说明：Color 本轮使用真实 TOPOS 帧转 RGB 后进行 640×360 的 CPU 等价工具矩阵；Composite 使用真实 TOPOS 帧生成的 PNG 作为节点输入，并另行用 `video_frame` 节点验证 native TOPOS 路由。无头环境无法代表桌面 QOpenGLWidget/Metal UI，因此“核心数据路径通过”不能等同于“桌面 UI 全通过”。

### 17.2 Edit 页面效果和插件

Edit 效果注册表共发现 25 项：7 个内置视频滤镜、4 个 `.tglsl` 视频插件、8 个音频效果、2 个生成器、4 个历史/clip-native 项。

| 功能项 | 逐项结果 | 备注 |
|---|---|---|
| Blur | 通过 | TOPOS 源真实导出成功，约 4.668s/8 帧，GPU trail 仍为 `cpu_encoder_surface_path` |
| Sharpen | 通过 | 约 5.454s/8 帧，实际导出成功 |
| Denoise | 超时 | 8 帧 TOPOS 2K 导出超过 120s；必须先拆分 decode/effect/encode 计时并优化，不能判为通过 |
| Edge Detect | 通过 | 约 5.243s/8 帧 |
| Erode/Dilate | 通过 | 约 3.861s/8 帧 |
| Defocus | 通过 | 约 15.579s/8 帧，明显慢于其他内置滤镜 |
| Glow | 通过 | 约 5.248s/8 帧 |
| Bilateral Skin Smooth | 被导出预检阻断 | 有插件 catalog，但无导出后端 |
| Color Space Transform | 被导出预检阻断 | 有插件 catalog，但无导出后端 |
| HighPass Retouch | 被导出预检阻断 | 有插件 catalog，但无导出后端 |
| Film Halation | 被导出预检阻断 | 有插件 catalog，但无导出后端 |
| Transform / Crop / Opacity | 通过 | 作为 clip-native 字段分别导出，不能作为可附加 FX 处理 |
| Vignette | 按设计不可附加 | 当前是历史 ghost entry，没有生效的 Edit 导出后端 |
| Solid Color | 通过 | 生成器创建、进入时间线并导出成功 |
| Adjustment Clip | 输出成功，但有警告 | 导出日志出现 `Unsupported asset type: adjustment`；空调节片段看似通过，实际效果传播仍需专门验收 |

本轮还对 8 个音频效果使用真实 WAV 片段执行 DSP：Graphic EQ、Limiter、Compressor、Reverb、Delay、Saturation、Imager、Noise Gate 全部返回同形状、有限值的音频；具体耗时和 RMS/Peak 保存在扩展测试日志中。这个结果证明音频 DSP 单块可运行，但不替代 H265/ProRes packet boundary、seek 和完整时间线导出验收。

因此，上一轮的“代表性效果能跑”不能升级成“Edit 所有插件都通过”。当前最关键的缺口是：4 个视频插件没有导出后端，Denoise 严重超时，Vignette 没有实际 renderer，Adjustment Clip 只有空效果链被验证。

### 17.3 Color 页面工具

真实 TOPOS 帧经过 native 解码后，本轮对以下 14 项工具做了非默认参数处理，均产生有限值、非空输出：

| 工具组 | 结果 |
|---|---|
| Grading、Curves、HSL Curve、3D LUT | 通过；真实帧数据经过参数变化后输出发生变化 |
| Mixer、Warper、Soft Clip | 通过；真实帧数据经过参数变化后输出发生变化 |
| Blur/Sharpen、Film Grain | 通过；输出有限且尺寸保持一致 |
| Qualifier、Mask Shapes、Blend Modes | 通过；生成了实际 matte/混合结果 |
| Scopes histogram | 通过；生成非空分析数据 |
| Denoise | 通过，但 640×360 CPU 等价路径约 527ms |

真实数据矩阵中 Grading 约 207ms、Denoise 约 527ms（640×360 CPU 等价路径），这已经说明“功能能跑”与“可实时交互”是两项不同验收。全分辨率、GPU/Metal、连续拖动参数还没有在本轮无头环境中形成 A 级证据。

Tracker 和 Auto Color 的工程状态、序列化、服务生命周期已经有专门测试覆盖；本轮没有把它们伪装成已完成真实桌面 UI 验收。Color 插件在 `msl_composition.py` 中被明确拒绝，属于当前设计阻断：插件要么需要双源 GLSL/MSL，要么需要稳定的 GLSL→MSL 转换层，不能静默跳过。

### 17.4 Composite 页面节点

Composite `NODE_REGISTRY` 共发现 93 个注册 key。本轮两级测试结果如下：

1. 63 个常用节点的真实 TOPOS 帧 GPU 覆盖基准：62 个产生输出，1 个 `ai_generate` 返回空输出；其中 `chroma_keyer` 输出全黑、`constant` 输出全平，需要按“默认输入契约/参数”继续定向确认，不能只按不崩溃计算通过。
2. 全部 93 个注册 key 的通用单节点矩阵：79 个产生非平输出，3 个输出全平，14 个未产生输出。14 个包含 AI、3D、文件输出、源节点、`color_input`、`timeline_clip_input`、`motion_graphic` 等特殊节点；其中部分是通用 harness 没有提供专用输入或外部资源，不直接等价于产品缺陷。
3. `video_frame` 进行了定向测试：使用 `2K_topos_proxy.mov` 直接作为节点文件，TOPOS native 路由成功；预览首帧约 1208ms，导出首帧约 106ms，输出为 1920×1080 RGB，非空且非平。这个结果证明 Composite 的 TOPOS 源入口是可达的，但首帧冷启动和逐帧性能仍需独立基准。

当前无头 OpenGL 是 AMD Radeon RX 6800、GL 4.1；计算着色器不可用，节点普遍走 fragment fallback。常用节点的稳态导出大多约 40–65ms/帧，说明 2K 25fps 附近可以工作，但不满足所有节点、4K、多节点链和桌面 Metal 的生产级速度承诺。

### 17.5 当前轮新增问题

| ID | 严重性 | 问题 | 证据 | 下一步验收 |
|---|---|---|---|---|
| TOPOS-P0-025 | P0 | 发布包/默认搜索到的 TOPOS 动态库与测试素材 bitstream 不匹配 | 旧库打开 `2K_topos_proxy.mov` 返回 `-9 tpcC magic/version 不符`；release 库通过 | 启动诊断唯一确定库路径、ABI、bitstream 支持范围；代理素材可打开 |
| TOPOS-P1-026 | P1 | TOPOS 时间线导出仍尝试走 FFmpeg 高位深探测，产生无效错误日志和额外开销 | 多次出现 `Failed to open media ... VideoStream has no attribute width`，随后 native 路径仍成功 | 对 TOPOS 源先判定 native，禁止无效 FFmpeg probe；错误日志数量为 0 |
| EDIT-P0-027 | P0 | 4 个 Edit 视频插件没有导出后端 | 四个插件均被 `preflight_video_effects_export` 明确阻断 | 每个插件都有可验证的 preview/export backend；否则 UI 必须标为“仅预览/不可交付” |
| EDIT-P1-028 | P1 | Edit Denoise 在 2K TOPOS 真实导出上严重超时 | 8 帧独立子进程超过 120s | 先在 8 帧内完成；最终 2K 25fps 至少达到 30fps 或有明确质量档位 |
| EDIT-P1-029 | P1 | Vignette 注册项不可附加且没有 renderer | registry 仍可见定义，但被标为 non-attachable ghost | 删除历史入口或补齐统一 catalog/preview/export 实现 |
| EDIT-P1-030 | P1 | Adjustment Clip 空链输出成功但日志显示 unsupported asset | 导出出现 `Unsupported asset type: adjustment` | 非空效果链作用于下方轨道并可像素验证，日志无 unsupported |
| COLOR-P1-031 | P1 | Color CPU 等价处理在低于全分辨率时已偏慢 | 640×360 Grading 约 207ms，Denoise 约 527ms | 真实 2K GPU/Metal 拖动 p95 ≤33.3ms；CPU fallback 需明确质量档位 |
| COLOR-P0-032 | P0 | Color 插件没有 MSL 执行路径 | emitter 对 plugin effect 显式 ValueError/阻断 | 双源插件或受控转换器完成前，插件必须在能力清单中声明不可交付 |
| COMP-P1-033 | P1 | 无头 GL 只有 4.1，所有 compute 节点被迫 fragment fallback | 每个节点日志出现 `compute_unavailable` | 桌面 Metal/GL 4.3+ runner 输出 compute 与 fallback A/B；建立质量和速度门槛 |
| COMP-P1-034 | P1 | `ai_generate` 真实矩阵返回 None | 常用节点矩阵及 93 节点矩阵均复现 | 给 AI 节点提供外部模型/输入契约；没有模型时返回结构化 `MODEL_UNAVAILABLE` |
| COMP-P1-035 | P1 | `chroma_keyer`/`constant` 默认通用输入产生全黑/全平 | 真实 TOPOS 输入矩阵中 std=0 | 用显式绿幕/颜色参数验证 keyer；用参数非默认验证 constant；禁止“平图但状态 ok” |
| COMP-P1-036 | P1 | 93 节点通用 harness 无法覆盖源/3D/文件输出等专用契约 | 14 个 no-output 中包含 `video_frame`、3D、file_output、timeline_clip_input；定向 video_frame 已通过 | 为每个特殊节点提供专用 fixture、输入端口、外部依赖和输出断言 |

## 18. 基于第三轮结果的重新开发顺序

### 阶段 A：先修复“能力发现”和 TOPOS 运行时

1. 发布包只保留与当前素材/bitstream 兼容的 native codec，启动时输出绝对路径、ABI、支持的 major、profile 和可选符号。
2. `MediaSourceFactory`、`FrameProvider`、Timeline Exporter、Thumbnail Provider 使用同一个 TOPOS 判定和 backend 选择结果。
3. TOPOS 源一旦判定成功，禁止再调用会失败的 PyAV/FFmpeg 高位深 probe；只有 native 打开失败时才记录一次可解释 fallback。
4. 将 `TOPOS_CODEC_LIB`、ffmpeg、ffprobe 纳入统一 capability snapshot，AI 工具读 snapshot 决策，不读散落环境变量。

验证：旧库、正确库、缺库、缺 ffmpeg 四种环境各运行一次；结果必须明确是 `AVAILABLE`、`INCOMPATIBLE`、`MISSING` 或 `FALLBACK`，不能只返回 false。

### 阶段 B：先封住所有“看似通过但实际上不可交付”的功能

1. Edit 四个插件：先实现导出后端或在 UI/AI 能力清单中明确标记 `preview_only`；不可导出的插件不能进入“导出成功”路径。
2. Denoise：为 decode、预处理、滤波、upload、encode 分段计时，先解决 8 帧超时，再谈全分辨率实时。
3. Vignette：移除 ghost registry entry 或统一接入 EffectCatalog、GPU preview、CPU fallback、export preflight。
4. Adjustment Clip：验证非空效果链对下方轨道的像素影响，修复 unsupported asset 日志。
5. Composite AI/3D/source 节点：每类建立专用 fixture；缺模型、缺文件、缺输入时返回结构化状态而非空纹理。

验证：每个功能必须同时满足“输出存在、输出发生预期变化、输出可 probe、保存重载后仍一致、失败时有 error_code”五项条件。

### 阶段 C：Color 和 Composite 的 GPU/Metal 双路径验收

1. 准备桌面 macOS Metal runner，执行 Color 参数拖动、Scopes、Qualifier、Mask、Tracker、Composite 节点图和页面切换。
2. 同一真实 TOPOS 帧同时走 CPU、OpenGL fallback、Metal，输出做逐像素/统计对拍。
3. 对 10 个 MSL migrated effects 和 4 个插件分别建立状态：`gl_only`、`dual`、`msl_verified`、`exportable`。
4. 对每个 Composite 节点记录 cold、warm preview、warm export、GPU backend、readback、flat-output、error code。

验收：桌面真实 UI 完成拖动/播放/切页/保存/重载；同一任务的 AI 能力清单可以读取每个效果是否 preview-only、exportable、requires_model、requires_gpu。

### 阶段 D：性能目标

优先级必须是 Denoise/Blur/Defocus/Glow → 双层和 Composite 多节点 → Color CPU fallback → 首帧冷启动 → 4K。

第一阶段门槛：2K TOPOS 单层常用效果 ≥30fps，双层 ≥30fps，Color 实时参数拖动 p95 ≤33.3ms，Composite 常用节点 warm export ≤33.3ms；所有失败和 fallback 都要可解释。

## 19. 第三轮结论

这次已经不是只测 H264 核心链路，而是实际使用 TOPOS 422Proxy 把 Edit、Color、Composite 的大量功能逐项压测并分类：

- Edit：内置效果大多能导出，但 Denoise 超时，4 个插件没有导出后端，Vignette/Adjustment Clip 存在设计或实现缺口；不能宣称全部效果已通过。
- Color：14 个真实帧工具等价路径通过，但全分辨率 GPU/Metal UI 未在无头环境形成证据，Color 插件仍被 MSL 设计阻断。
- Composite：93 个注册节点均已纳入清单；79 个通用节点产生非平输出，特殊节点还需要专用 fixture；`video_frame` 的 TOPOS native 入口已定向通过。
- 性能：TOPOS native 解码本身可用，但导出链路仍有无效 FFmpeg 探测；compute 不可用时的 fragment fallback 和 Denoise/Defocus 是当前主要速度风险。

所以当前项目更准确的状态是：核心媒体链路和大量基础节点可运行，AI 控制面的能力清单已经足够继续建设；但在完成 TOPOS runtime 统一、插件导出、Denoise、Color/Composite 桌面 GPU 验收以及特殊节点专用测试前，仍不能把软件标记为“所有功能可由 AI 自主操控并高速交付”。

## 20. 第四轮：统一速度锚点与全链路 GPU 对比

### 20.1 测试方法

本轮新增统一基准驱动：

- [bench_topos_anchor_matrix.py](/Users/testuser/Documents/vscode/topos_1/scripts/bench_topos_anchor_matrix.py)
- [topos_anchor_matrix.json](/Users/testuser/Documents/vscode/topos_1/bench_out/topos_anchor_matrix_20260917/topos_anchor_matrix.json)
- [topos_anchor_matrix.md](/Users/testuser/Documents/vscode/topos_1/bench_out/topos_anchor_matrix_20260917/topos_anchor_matrix.md)

口径固定为同一份 `2K_topos_proxy.mov`、1920×1080、25fps、48 帧正式测量、8 帧预热、每项独立子进程：

1. 原生 TOPOS 解码：只测 `ToposMediaSource.read_frame`。
2. 原生 TOPOS 编码：用一张真实解码帧重复编码，隔离编码器成本，不把 48 帧原始平面全部留在内存中。
3. 解码→编码：逐帧真实 TOPOS 解码后立即 TOPOS 编码，作为 codec-only 生产锚点。
4. `TimelineExporter identity`：同一输出档、无效果完整导出，作为编辑器生产锚点。
5. 每个效果/合成场景相对同组选路的 identity 计算：
   - `extra_ms = effect_total - identity_total`
   - `overhead_ratio = extra_ms / identity_total`
   - `fps_ratio = effect_fps / identity_fps`
6. `TOPOS_EXPORT_GPU_RENDER=1` 只表示请求 GPU；是否真的走 GPU 以 `gpu_trail` 和 `fallback_reason` 判断，不能把环境变量当作证据。

### 20.2 TOPOS codec-only 锚点结果

| 链路 | 结果 |
|---|---:|
| 原生 TOPOS 解码 | 433.92 fps；p50 2.16ms；p95 3.25ms |
| 原生 TOPOS 编码 | 147.17 fps；p50 6.48ms；p95 9.54ms |
| 真实解码→TOPOS 编码 | 96.57 fps；解码 p50 2.34ms；编码 p50 7.26ms |

这说明 TOPOS 编解码器本身不是当前时间线导出 9.6fps 的唯一瓶颈；codec-only 链路约 96.6fps，而 TimelineExporter identity 只有约 9.6fps，编辑器帧交付、颜色转换/合成、线程流水线和封装仍有明显额外成本。

### 20.3 TimelineExporter TOPOS 输出结果

| 场景 | CPU 选路 | 强制 GPU 选路 | 相对 identity 的额外耗时 |
|---|---:|---:|---:|
| identity | 9.60 fps | 9.77 fps | 0 |
| grade | 1.60 fps | 1.60 fps | +25.09s / +25.17s |
| blur | 5.54 fps | 5.57 fps | +3.67s / +3.70s |
| sharpen | 4.04 fps | 3.80 fps | +6.89s / +7.72s |
| glow | 4.05 fps | 4.06 fps | +6.86s / +6.92s |
| two_layer | 2.97 fps | 3.01 fps | +11.17s / +11.02s |
| transition | 4.33 fps | 4.40 fps | +6.08s / +6.00s |

14 组均成功生成输出，但 14 组的 trail 都是 `cpu_encoder_surface_path`。因此本轮不能宣称 TOPOS 输出已经全链路 GPU；强制 GPU 与 CPU 的差异很小，说明 TOPOS 自定义编码器仍在吃 CPU 可见平面，GPU 结果最终被回读/交给 CPU 编码器。

### 20.4 H.264 输出控制结果

补充控制测试使用同一 TOPOS 422Proxy 源：

- H.264 identity、关闭 native VT 时，CPU render 约 8.98fps；强制 GPU render 约 33.21fps；两者 trail 都仍是 `cpu_encoder_surface_path`。这证明 GPU 合成确实能显著降低渲染成本，但编码提交仍是 CPU 路径。
- 请求 `TOPOS_EXPORT_NATIVE_VT=1` 时，TOPOS 422 Metal 输入无法直接满足 H.264 的 NV12 输出契约，trail 为 `unsupported_topos_metal_output_format`，随后 native bridge 收到 CPU 帧并以 `native GPU bridge cannot consume a CPU/host-visible frame` 失败。这个结果必须视为桥接缺陷，不能算 GPU 成功。
- H.264 blur 强制 GPU 约 37.42fps，但 trail 仍是 `cpu_encoder_surface_path`；它是“GPU 渲染 + CPU 编码”而不是全链路 GPU。

### 20.5 本轮新增性能问题

| ID | 严重性 | 问题 | 证据 | 下一步验收 |
|---|---|---|---|---|
| PERF-P0-037 | P0 | TOPOS 输出没有 GPU 编码面，GPU 渲染结果仍进入 CPU encoder | 14 组 TOPOS timeline trail 全部为 `cpu_encoder_surface_path` | 要么实现 TOPOS GPU encoder/Metal surface submit，要么明确 TOPOS 是 CPU 编码中间片并把 GPU 回读压到最低；两者都必须有 stage telemetry |
| PERF-P0-038 | P0 | TOPOS 422 Metal 输入无法直接进入 H.264/HEVC 的 NV12/P010 native VT bridge | `unsupported_topos_metal_output_format`，随后 bridge 拒绝 CPU frame | 增加 GPU 422→NV12/P010 转换 kernel 和 surface 写入；native bridge 只在输入/输出契约完整时启用 |
| PERF-P1-039 | P1 | “强制 GPU”标签没有形成可交付证据 | forced_gpu 与 cpu_render 的 TOPOS 结果都落 CPU encoder | AI/用户可看到 `gpu_render=metal`、`encode=cpu/videotoolbox/topos`、upload/download bytes 和 fallback reason |
| PERF-P1-040 | P1 | TOPOS TimelineExporter identity 远慢于 codec-only decode→encode | codec-only 约 96.6fps，identity 约 9.6fps | 分离 render、色彩转换、帧封装、队列等待、编码、mux；找到剩余约 10× 差距的第一责任阶段 |
| PERF-P1-041 | P1 | Color grade 是当前时间线效果中最大单项成本 | 2K 48 帧 grade 约 1.60fps，额外约 25s | 先做单层 grade 的 GPU 直出和参数缓存；2K 目标至少达到 30fps，p95 ≤33.3ms |
| PERF-P1-042 | P1 | 复杂合成和锐化/辉光成本仍明显超过实时预算 | two_layer 约 3fps；sharpen/glow 约 4fps | 使用同一 GPU 纹理/中间缓冲融合多个节点，禁止每个效果单独 CPU round-trip |

### 20.6 推荐开发顺序

#### 阶段 1：先保证能力和失败语义正确

1. TOPOS 素材一旦 native probe 成功，禁止重复走 FFmpeg 高位深探测；修复 `VideoStream.width` 兼容性异常日志。
2. native VT bridge 在发现 TOPOS 422 输入不支持 NV12/P010 时，必须在导出开始前拒绝该桥或切回普通 CPU encoder，不能运行到中途再因 CPU frame 崩溃。
3. 将 `decode_backend`、`render_backend`、`encode_backend`、`cpu_upload_bytes`、`cpu_download_bytes`、`fallback_reason` 纳入每次导出 manifest，给 AI 控制面直接读取。

验证：同一 identity 在 native bridge 可用、不可用、TOPOS 422 不兼容、缺 Metal 四种环境下都能得到结构化结果；不得出现“设置成功但中途失败”的半隐式状态。

#### 阶段 2：打通 H.264/HEVC 的 GPU surface 闭环

1. 在 Metal 中增加 TOPOS 422 R16 Y/U/V → NV12/P010 的融合转换，直接写 VideoToolbox surface；不要先转 BGR 再转 YUV。
2. 单层 identity 先实现 `Topos decode surface → Metal copy/convert → Metal render → VT surface → VideoToolbox encode`。
3. 再接入无颜色效果的 opacity/transform/crop，随后接 grade、blur、sharpen、glow。
4. native bridge 只接受 `GpuEncodeSurface`，但必须由上游能力协商保证不会把 CPU `DecodedFrame` 送入 bridge。

验证：输出 H.264/HEVC 可 probe，帧数一致、颜色误差在门槛内；2K identity ≥60fps、常用单效果 ≥30fps；trail 的每帧 `gpu_submit_ms` 非空，CPU upload/download 为 0 或有明确解释。

#### 阶段 3：TOPOS 自定义编码器的低拷贝/高吞吐路径

1. 如果产品要求 TOPOS 输出也全链路 GPU，需要实现 native GPU encoder 或 GPU→TOPOS 编码的受控共享内存/异步 staging；仅把 CPU encoder 包在 GPU renderer 后面不能称为全 GPU。
2. 如果短期不实现 GPU encoder，先保持 Topos planar 直通，避免 BGR48↔YUV422 多次转换，并让 GPU render 只在确实有 GPU 效果时启用；无效果 identity 直接走 codec-only fast path。
3. 对 TOPOS encoder 的 `fill/entropy/check/copy` 阶段逐项做线程数、slice_rows、码率档位矩阵，确认 CPU 编码本身不会成为效果优化后的新瓶颈。

验证：TOPOS identity 的 timeline 结果逐步逼近 codec-only 锚点；至少先达到 30fps，再以 60fps 为高性能目标。所有效果路径都必须提供“无效果总耗时、效果额外耗时、编码阶段占比、回读字节数”。

#### 阶段 4：按额外成本从高到低迁移效果

顺序固定为：Grade → 双层/Transition → Sharpen/Glow → Blur → 其他 Color/Composite 节点 → Denoise/AI 节点。每迁移一项都必须复跑同一 anchor matrix，不能只看单元测试或单帧截图。

### 20.7 当前结论

本轮对比已经回答了速度锚点问题：

- TOPOS codec-only 解码/编码基线已经建立；
- TOPOS TimelineExporter identity 基线已经建立；
- 代表性效果相对 identity 的额外成本已经量化；
- CPU/GPU 选路以及 GPU fallback 已被真实 trail 证明；
- 当前最大瓶颈不是“有没有调用 GPU”这么简单，而是 TOPOS 422 输入到目标编码面之间缺少完整 surface 契约，导致最终仍回到 CPU encoder。

因此后续优化原则是：先让每条链路的 GPU/CPU 归属可证，再做效果器优化；先消灭 CPU↔GPU round-trip，再优化 shader 数学本身。否则单独把某个效果器改快，最终导出速度仍可能被一次完整回读和 CPU 编码抵消。
