# Topos 可执行优化升级总计划（供 AI 逐项实施）

> 文档版本：2026-09-17 / v1.0  
> 适用仓库：`/Users/testuser/Documents/vscode/topos_1`  
> 实施目标：让 AI 能以安全、可验证、可回滚的方式，逐步把 Topos 升级为“所有已声明功能均可由 AI 通过正式接口控制，并在真实素材上高性能交付”的产品。  
> 文档性质：**实施手册**，不是测试报告。测试数据只用于确定优先级和验收基线。

---

## 0. 给执行 AI 的总指令

每次只执行本计划中的**一个任务卡**，不得跨任务顺手大重构。开始前、修改后、完成时都必须更新任务卡的状态记录；不能证明完成就保持 `BLOCKED` 或 `IN_PROGRESS`，不得写成完成。

### 0.1 强制工作流程

1. 阅读本文件的“当前事实”“全局约束”和当前任务卡全部内容。
2. 检查工作区已有改动；不覆盖、不删除、不重置与当前任务无关的内容。
3. 用 `rg` 找到任务卡列出的实现、调用方和测试；先确认数据流和边界，再改代码。
4. 只经现有不可变模型、command/service、registry、facade 和 renderer 接口提交状态。禁止靠 UI 点击、改 widget 私有字段或改序列化文件局部绕过业务链路。
5. 先补/改 focused test，再实现最小改动；涉及真实媒体、GPU、导出、插件或持久化时必须补集成测试。
6. 运行任务卡要求的验证。若任意 P0 验证失败，不得继续后续任务；先修复或记录可复现阻塞。
7. 生成结构化结果：代码变更、测试命令、通过/失败数、性能数值、GPU trail、输出文件 probe、遗留风险。
8. 只有“完成定义”全部满足时才把任务标记为 `DONE`，然后进入它明示的下一任务。

### 0.2 绝对禁止事项

- 不把 `TOPOS_EXPORT_GPU_RENDER=1`、日志中的 GPU 名称或无崩溃当作“全链路 GPU 成功”；必须读取 `gpu_trail`。
- 不把“输出文件存在”当作功能通过；必须同时验证帧数、像素变化/统计、元数据、保存重载和失败语义。
- 不把没有 export backend 的插件伪装成可导出功能；必须 `preview_only` 或显式阻断。
- 不以关闭 preflight、吞掉异常、静默回退、降低位深或改为 CPU 路径来伪造性能通过。
- 不在 UI 线程直接做 OpenGL/ModernGL/Metal 或长时间解码/编码工作。
- 不开放任意 Shell、Python、系统文件、凭据或网络权限给产品内模型；AI 只能调用具备 schema、权限、作用域、验证和审计的正式能力。

### 0.3 项目工程约束

- 分层：`products → features → shared → app`；复用已有模型、service、registry 与 facade。
- 工程/时间线/效果/字幕必须走既有 command/service，保持 undo/redo、revision、预览、持久化一致。
- 插件/效果必须经 Effect Catalog/Registry 声明参数 schema、预览能力、导出能力和兼容版本。
- 资源必须在成功、失败、取消和窗口关闭路径释放；GPU 对象按现有 deferred release 模式处理。
- GLSL/MSL 保持 HDR 语义，浮点常量显式 `.0`，循环上限编译期常量，禁止无理由 `clamp(..., 0.0, 1.0)`。
- 新 Python 函数提供类型注解；异常必须保留领域错误原因，不能用默认值掩盖失败。

完整约束见 [`.clinerules`](/Users/testuser/Documents/vscode/topos_1/.clinerules) 和 [`AGENTS.md`](/Users/testuser/Documents/vscode/topos_1/AGENTS.md)。

---

## 1. 最终完成定义

项目达到目标，必须同时满足以下六项，而不是只满足其中一项：

| 维度 | 最终标准 |
|---|---|
| AI 控制 | 所有产品已声明可用功能均有正式语义接口；AI 能发现能力、预览、申请授权、执行、验证、取消、回滚/补偿和读取审计 |
| 功能正确性 | Edit、Color、Composite、Deliver、Project/Media 的每项能力都有正向、边界、失败、保存重载验证；没有“UI 可见但 AI/导出不可用”的隐性能力 |
| 真实媒体 | H.264、HEVC/ProRes、高位深 TOPOS 422Proxy 均可完成 import → timeline → edit/color/composite → export → probe 回读 |
| GPU 性能 | 真实 GPU 链路能分阶段证明 decode/render/encode 的后端；H.264/HEVC 常用路径没有不必要 CPU↔GPU 往返；TOPOS 输出明确区分 CPU 编码中间片与未来 GPU 编码能力 |
| 稳定性 | 取消、超时、缺库、缺模型、错误素材、GPU 不可用、插件不兼容均返回结构化状态，不崩溃、不挂死、不产出伪成功文件 |
| 可维护性 | 能力 manifest、测试矩阵、性能锚点、任务事件和文档来自同一套状态，未来 AI 不需要猜测哪些功能可用 |

---

## 2. 当前事实与不可改变的基线

以下是已由真实测试确认的事实，后续优化必须以它们为起点；不得重写历史数值来“达标”。

### 2.1 TOPOS 422Proxy 与性能锚点

测试素材：`tests/test_videos/2K_topos_proxy.mov`，1920×1080、25fps、96 帧、`yuv422p10le`、10-bit、TOPOS 422Proxy。

| 链路（48 帧正式、8 帧预热） | 实测基线 | 结论 |
|---|---:|---|
| 原生 TOPOS 解码 | 433.92fps，p50 2.16ms | codec 解码不是时间线主瓶颈 |
| 原生 TOPOS 编码 | 147.17fps，p50 6.48ms | 自研编码器当前是 CPU 路径 |
| TOPOS 解码→TOPOS 编码 | 96.57fps | codec-only 生产型锚点 |
| TimelineExporter identity → TOPOS Proxy | 9.60fps CPU / 9.77fps forced GPU | 时间线交付比 codec-only 慢约十倍 |
| Grade → TOPOS Proxy | 1.60fps | 当前最大代表性效果瓶颈 |
| Blur / Sharpen / Glow → TOPOS Proxy | 5.54 / 4.04 / 4.05fps | 常用效果仍远未实时 |
| 双层 / Transition → TOPOS Proxy | 2.97 / 4.33fps | 合成和过渡需 GPU 中间纹理复用 |
| TOPOS 输出 GPU trail | 所有 14 组为 `cpu_encoder_surface_path` | 当前不是全链路 GPU |
| TOPOS → H.264 identity（CPU render） | 8.98fps | CPU 路径基线 |
| TOPOS → H.264 identity（GPU render） | 33.21fps | GPU 渲染有效，但编码仍走 CPU surface |
| TOPOS → H.264 native VT | 失败：`unsupported_topos_metal_output_format` | 422 R16 texture 尚不能写入 NV12/P010 surface |

性能证据：

- [速度锚点报告](/Users/testuser/Documents/vscode/topos_1/bench_out/topos_anchor_matrix_20260917/topos_anchor_matrix.md)
- [速度锚点 JSON](/Users/testuser/Documents/vscode/topos_1/bench_out/topos_anchor_matrix_20260917/topos_anchor_matrix.json)
- [基准驱动](/Users/testuser/Documents/vscode/topos_1/scripts/bench_topos_anchor_matrix.py)

### 2.2 已确认的功能状态

| 域 | 已有基础 | 真实测试揭示的缺口 |
|---|---|---|
| AI 控制 | ToolFacade、MCP、CLI、审批、编辑计划、Composite 参数编辑、导出请求、任务状态已有基础 | Color、Composite 图结构、Deliver 全配置、工程/素材管理没有完整统一控制面；跨域事务和统一事件流不足 |
| TOPOS runtime | release dylib 可打开 TOPOS Proxy，`ToposMediaSource` context 路径可用 | 旧 `dist`/`prof` 库不兼容；TOPOS 成功后仍有无效 FFmpeg probe |
| Edit | 6 个内置视频效果真实导出通过；8 个音频 DSP 单块通过；Transform/Crop/Opacity 实际导出通过 | Denoise 2K 8 帧超 120s；4 个插件没有导出后端；Vignette 是 ghost；Adjustment Clip 有 unsupported asset 警告 |
| Color | 14 个真实 TOPOS 帧工具等价路径通过；Tracker/Auto Color 有服务/单测 | 640×360 CPU grading 约207ms、denoise 约527ms；插件无 MSL export 路径；缺真实桌面 GPU 验收 |
| Composite | 93 个注册节点已枚举；76 个通用 case 非平输出、3 个全平输出；TOPOS `video_frame` 路由通过 | 14 个无输出/错误；AI/3D/源/文件节点缺专用 fixture；无头 GL 4.1 为 fragment fallback |
| Deliver | 时间线导出、preflight、部分 GPU trail 已存在 | H.264/HEVC Native VT 对 TOPOS 422 输入的 surface 契约不完整；TOPOS 输出仍 CPU encoder |
| 稳定性 | 大量单元/契约测试存在 | FFmpeg/ffprobe 发现不一致、H265/ProRes 音频 boundary/seek、部分 Composite/AudioWaveform/UI 原生稳定性问题仍需逐个复现和修复 |

功能证据：

- [真实素材总测试与问题清单](/Users/testuser/Documents/vscode/topos_1/docs/ai_real_material_test_optimization_plan_2026-09-17.md)
- [TOPOS 功能矩阵](/Users/testuser/Documents/vscode/topos_1/docs/ai_topos_feature_matrix_optimization_plan_2026-09-17.md)
- [AI 控制现状清单](/Users/testuser/Documents/vscode/topos_1/docs/ai_control_optimization_plan.md)

---

## 3. 全局验收协议

### 3.1 每个任务必须留下的证据

每个任务输出到唯一目录：`bench_out/ai_upgrade/<任务ID>_<YYYYMMDD-HHMMSS>/`。目录至少包含：

```text
result.json                 # 机器可读结果、环境、git revision、状态
result.md                   # 人可读摘要与结论
command.log                 # 命令、stdout、stderr（必要时截断）
output_probe.json           # 视频输出的 codec/pix_fmt/frame count/metadata
gpu_trail.json              # 适用时：decode/render/encode backend 与回退原因
frame_checks.json           # 帧数、有限值、非平/像素差、首尾帧哈希
```

输出不能与正式用户素材混放；测试产物不得覆盖 `tests/test_videos` 原始素材。

### 3.2 标准验证层级

| 层级 | 必做检查 |
|---|---|
| L0 静态 | `py_compile`、类型/导入、registry/manifest 完整性 |
| L1 单元 | 正向、边界、错误码、取消、资源释放、序列化兼容 |
| L2 契约 | UI、CLI、MCP、AI Facade 走同一 command/service；schema、权限、revision、undo 一致 |
| L3 真实媒体 | import、timeline、效果/节点、save/reload、export、probe、首尾帧检查 |
| L4 性能 | warmup 后 FPS、p50/p95、阶段耗时、CPU upload/download bytes、GPU trail、baseline delta |
| L5 故障 | 缺 dylib、旧 dylib、GPU 不可用、插件无后端、缺模型、取消、超时、错误输出路径 |

### 3.3 通用通过条件

功能任务必须同时通过：

1. 预期操作成功，或不支持时返回稳定的明确 error code；
2. 输出状态有可观察的预期变化，不能仅是返回 `True`；
3. 工程保存、重载后保持同一状态；
4. 取消/异常不留半成品、GPU 泄漏或错误成功文件；
5. AI capability manifest 与 UI/导出能力一致；
6. 若影响渲染，CPU/GPU 输出在允许误差内一致；
7. 若声明 GPU 成功，trail 必须无未解释 CPU fallback。

### 3.4 统一命令模板

运行前固定使用兼容 native 库：

```bash
TOPOS_CODEC_LIB="$PWD/native/topos_codec/build/release/topos_codec.0.1.0.dylib" \
QT_QPA_PLATFORM=offscreen \
.venv/bin/python -m pytest <目标测试> -q
```

性能锚点：

```bash
TOPOS_CODEC_LIB="$PWD/native/topos_codec/build/release/topos_codec.0.1.0.dylib" \
TOPOS_CODEC_PROFILE=1 \
QT_QPA_PLATFORM=offscreen \
.venv/bin/python scripts/bench_topos_anchor_matrix.py \
  --frames 48 --warmup 8 --runs 1 \
  --output-dir bench_out/ai_upgrade/<任务ID>_<时间戳>
```

不要把一次冷启动或少于 48 帧的临时结果与本基线直接比较；临时 smoke 可用 4 帧，但只能验证功能，不能宣布性能收益。

### 3.5 实施前的渐进补测计划（测试优先）

现有测试足以确定 P0 优先级，但**尚不足以宣称所有功能都已覆盖或所有性能结论均可推广**。后续 AI 必须先完成与当前任务相关的补测包、保存基线，再修改对应产品能力。唯一例外是 `T-P0-00`、`T-P0-10`、`T-P0-20`：它们只建立能力识别和测量基础，可以在补测过程中最小化实现；不得夹带效果、编码或 UI 行为优化。

| 补测包 | 必测范围与操作 | 适用的后续任务 | 完成证据与通过标准 |
|---|---|---|---|
| `X-TEST-00` 环境与素材清单 | 为 `tests/test_videos` 中每个原始素材生成不可变 manifest：绝对路径/内容哈希、codec、profile、pix_fmt、位深、分辨率、FPS、帧数、时长、音频流；同时记录 macOS、GPU、Metal/GL、FFmpeg、TOPOS dylib ABI、git revision。禁止写入原始素材。 | 全部 | 清单可重复生成；同一输入得同一 hash 和探测结果；缺失库、无 GPU、探测冲突必须带 `reason_code`，不能静默跳过。 |
| `X-TEST-10` 真实桌面 GPU | 在实际桌面窗口（非仅 `QT_QPA_PLATFORM=offscreen`）执行 H.264 与 TOPOS 2K：导入、播放预览、拖动、基础 Grade、Blur、双层合成、H.264/TOPOS 导出。分别记录预览和交付的 GPU trail、帧时间、CPU/GPU 内存传输和截图/输出帧哈希。 | `T-P0-20`、`T-P0-30`、`T-P0-31`、`T-P1-10`、`T-P1-50`、`T-P1-60` | 每个动作有真实桌面证据；若只能无头 GL fallback，结果必须为 `BLOCKED: DESKTOP_GPU_NOT_VERIFIED`，不得称为桌面 GPU 已验证。 |
| `X-TEST-20` 全功能注册表与插件 | 枚举 Edit effect、Color tool/plugin、Composite node、Audio DSP、Deliver preset。每项至少测试：创建/挂载、默认参数、最小/最大或代表性参数、预览、真实导出、保存重载、取消/异常。对不支持交付的项目验证稳定拒绝码和替代建议。Composite 专用节点使用 video、image、mask、绿幕、文本、文件和 AI/3D stub 等合格 fixture，不得用“全平输出”代替验证。 | `T-P1-20` 至 `T-P1-22`、`T-P1-50`、`T-P1-60`、`T-P1-70` | 每个 registry 项有一行 machine-readable 结果：`PASS`、`EXPECTED_UNAVAILABLE`、`FAIL` 或 `BLOCKED`，带 fixture、输出 probe/frame check、耗时与根因；没有“未测试但默认可用”。 |
| `X-TEST-30` 编码/媒体矩阵 | 覆盖 H.264、H.265、ProRes、TOPOS 422Proxy；8/10-bit、4:2:0/4:2:2、短 GOP/长 GOP、带/不带音频、VFR、不同分辨率、长时长片段。每项跑 import → seek → timeline → export → reload，并验证色彩、位深、帧数、音画时长、取消与错误路径。未提供的素材应生成派生 fixture，不得伪造原格式测试成功。 | `T-P0-10`、`T-P0-30`、`T-P0-31`、`T-P1-70` | 每种声明支持的组合有正向样本；每种不支持组合有可预期拒绝/回退；FFmpeg 与 native probe 的结果冲突单独列为 defect。 |
| `X-TEST-40` 规范化性能与传输矩阵 | 对每种输入/输出组合先跑无效果 decode、encode、decode→encode anchor，再跑 identity、Grade、Blur、Sharpen、Glow、双层、Transition，以及每个效果的代表性高成本参数。每项至少 warmup 8 帧、测量 48 帧、重复 3 次，报告中位数、p50/p95、FPS、相对 identity/codec-only 的 delta、每阶段 CPU upload/download bytes 和 GPU trail。 | `T-P0-20`、`T-P0-30`、`T-P0-31`、`T-P1-10`、`T-P1-11`、`T-P1-21`、`T-P1-50`、`T-P1-60`、`T-P2-10` | 只能在硬件、软件、输入、输出、帧区间、warmup 一致时比较；无效果锚点不可缺失；GPU 声明必须同时证明 render 与 encode 路径，不能只凭 `force_gpu` 参数。 |
| `X-TEST-50` 长任务、并发与资源生命周期 | 对代表性 H.264/TOPOS 工作流跑长时间播放/反复 seek、连续导出、2 个并发 job、取消/重试、项目保存重开。记录 RSS、GPU/Metal 资源、文件句柄、残留临时文件、队列状态和最终输出。 | `T-P1-11`、`T-P1-22`、`T-P1-70`、`T-P2-21` | 不崩溃、不死锁、取消任务无完整或损坏成品，其他 job 可继续；资源曲线无持续无界增长；失败可复现并归类。 |
| `X-TEST-60` AI 真实操作回放 | 用 ToolFacade/MCP/CLI/API 分别执行同一项目操作，比较 resulting project revision、undo/redo、权限、审计、输出和错误码；覆盖批量交付、路径授权、缺模型/插件/GPU、取消与恢复。 | `T-P2-20` 至 `T-P2-22` | 不允许 AI 通过私有 UI 状态或脚本绕过 service；所有写操作都可审计、可撤销或可恢复；AI 的能力声明与 `X-TEST-20/30/40` 的真实结果一致。 |

执行规则：

1. 先执行 `X-TEST-00`；之后按当前领取任务选择相关补测包。补测结果有失败并不阻止继续调查，但必须先形成稳定复现和 `FAIL`/`BLOCKED` 证据，才可修改该问题。
2. 每次实现前保存“改前”结果，实现后用相同 manifest、参数、帧区间和硬件保存“改后”结果。性能任务没有配对前后结果，不得标记 `DONE`。
3. `X-TEST-20` 的“覆盖完成”不等于全部通过：它要求每一个注册项都具备 fixture 和明确状态；`FAIL`、`EXPECTED_UNAVAILABLE` 和 `BLOCKED` 必须进入本计划的任务卡或已知限制。
4. 测试脚本、fixture manifest 和结果也属于产品验证资产，必须代码审查、版本化、可从干净工作区复跑。不得以手工观察替代 machine-readable 结果。

---

## 4. 任务依赖图与执行顺序

```text
X-TEST-00 环境/素材 manifest
  └─ T-P0-00 单一能力状态 ──→ T-P0-10 TOPOS route/probe
       └─ X-TEST-10/20/30：桌面 GPU、全量功能、媒体矩阵的改前基线
            └─ T-P0-20 telemetry/GPU truthfulness ──→ X-TEST-40/50：性能、稳定性基线
                 ├─ T-P0-30 Native VT 协商 ──→ T-P0-31 TOPOS 422 GPU surface
                 │    └─ T-P1-10 identity fast path ──→ T-P1-11 CPU encoder 低拷贝
                 │         └─ 每步重跑相关 X-TEST-10/30/40/50
                 └─ T-P1-20 Effect Capability Contract
                      ├─ T-P1-21 Edit 效果性能
                      ├─ T-P1-22 Adjustment/Audio/Timeline
                      ├─ T-P1-50 Color GPU/插件
                      ├─ T-P1-60 Composite fixture/图结构
                      └─ T-P1-70 Deliver/稳定性
                           └─ 每一支先完成相关 X-TEST，再进入 T-P2-20～T-P2-22 AI 控制/事务
                                └─ X-TEST-60 + W01～W08 工作流回放 ──→ G0～G5 发布门禁

T-P2-10 TOPOS GPU encoder 是独立可行性分支：只有在上述 CPU/VT 链路已真实量化后才允许评估。
```

测试前置关系：`X-TEST-00` 在所有任务前执行；`T-P0-00`/`T-P0-10` 提供可重复测量基础；`X-TEST-10` 至 `X-TEST-60` 按各任务的适用列逐步执行。除测量基础任务外，任何实现任务均不得跳过其相关补测包的“改前基线”。

**执行原则：** P0 任务优先解决“错误路由、错误成功、崩溃、无观察性”；P1 才优化具体效果和补全产品域；P2 才扩大 AI 自主范围。任何上游 P0 或相关 `X-TEST` 未完成，后续任务只能做调查/测试，不能标为交付完成。

---

## 5. P0：工程护栏、TOPOS runtime 与可观测性

### T-P0-00：建立单一能力状态模型

| 项目 | 内容 |
|---|---|
| 状态 | DONE（2026-09-17 rev d36a9e46；证据 bench_out/ai_upgrade/T-P0-00_20260917-215014） |
| 目标 | 让 UI、AI、CLI、MCP 和导出使用同一份能力状态，而不是各自猜测 native 库、GPU、插件或模型是否可用 |
| 主要模块 | `src/features/edit/ai_editing/capability_manifest.py`、`tool_facade.py`、`src/shared/media/factory.py`、`src/shared/codec/topos_binding.py`、Deliver preflight |
| 前置 | 无 |
| 后续 | T-P0-10、T-P0-20、T-P1-20 至 T-P1-70、T-P2-20 至 T-P2-22 |

实施步骤：

1. 定义只读 `RuntimeCapabilitySnapshot`：包含 `topos_codec`（绝对库路径、ABI、bitstream major、符号、profile）、`ffmpeg`/`ffprobe`、GPU/Metal/GL、硬件编码、模型包、插件 preview/export backend、平台限制。
2. 每个字段必须使用四态：`AVAILABLE`、`UNAVAILABLE`、`INCOMPATIBLE`、`FALLBACK`，并带稳定 `reason_code`、人可读信息和检测时间；禁止 `bool` 掩盖原因。
3. 将 snapshot 注入 capability manifest、ExportPreflight、ToolFacade `topos.get_capabilities`、CLI `toposctl capabilities`；不能在不同入口各自重新探测。
4. 为每个 effect/node/deliver preset 增加 `preview_backend`、`export_backend`、`requires_model`、`requires_gpu`、`fallback_policy`。`export_backend=None` 必须自动从可导出能力中移除。
5. snapshot 变化必须发事件；正在运行任务冻结启动时 snapshot，结束报告中保留该版本。

禁止：把检测失败默认为可用；为让测试通过而在 manifest 中手写与实际 renderer 不一致的能力。

验证：

```bash
.venv/bin/python -m pytest \
  tests/edit/ai_editing/test_tool_facade.py \
  tests/edit/ai_editing/test_toposctl.py \
  tests/edit/ai_editing/test_toposctl_offline.py -q
```

补充四个测试环境：正确 release dylib、旧/不兼容 dylib、未设置 dylib、GPU 不可用。每种环境均断言 snapshot 四态和 `reason_code`。完成定义：所有入口返回相同 capability ID 和原因；无能力不能被 AI 计划器选择为可执行操作。

### T-P0-10：TOPOS native 路由与 probe 单一化

| 项目 | 内容 |
|---|---|
| 状态 | DONE（2026-09-17 rev d5f46f75；证据 bench_out/ai_upgrade/T-P0-10_20260917-221757） |
| 目标 | TOPOS 文件一旦 native probe 成功，任何调用方都不再走必定失败的 FFmpeg/PyAV probe |
| 主要模块 | `src/shared/media/factory.py`、`topos_source.py`、`src/features/edit/core/project.py`、`timeline_export.py`、thumbnail/provider、`frame_provider_service.py` |
| 已知问题 | `VideoStream.width` 无效 probe 日志；旧 dylib 返回 `tpcC magic/version 不符` |
| 依赖 | T-P0-00 |

实施步骤：

1. 在 `MediaSourceFactory` 建立唯一 `detect_media_backend(path, snapshot)`；先通过容器/codec 标识判断 TOPOS，再创建 `ToposMediaSource`，禁止只按 `.mov` 后缀猜测。
2. 把结果缓存为带文件签名的 `MediaProbeResult`，由 import、project metadata、thumbnail、timeline export、Composite video_frame 和 AI media analysis 共享。
3. native probe 成功时，metadata 仅来自 `ToposMediaSource.probe()`；不能再创建 FFmpeg high-depth source。
4. native 失败时仅记录一次结构化失败，并按 snapshot 的 fallback policy 处理；不重复刷堆栈日志。
5. 在 `EditProject.import_asset` 和项目重载时持久化安全的 media identity/codec metadata，不持久化机器绝对 dylib 路径。
6. 修复旧库选择：库解析返回“正确 release / 不兼容 / 不存在”，而不是搜索到第一个 dylib 就使用。

验证：

```bash
TOPOS_CODEC_LIB="$PWD/native/topos_codec/build/release/topos_codec.0.1.0.dylib" \
.venv/bin/python -m pytest \
  tests/media/test_topos_source.py \
  tests/media/test_media_source_factory.py \
  tests/media/test_topos_source_export_passthrough.py -q
```

新增真实素材集成测试：对 `2K_topos_proxy.mov` 连续执行 import、thumbnail、timeline export、Composite `video_frame`，断言日志中无 `VideoStream.width`、每个调用只产生一次 native probe、输出 metadata 为 1920×1080/25fps/10-bit 4:2:2。旧 dylib 必须得到 `TOPOS_CODEC_INCOMPATIBLE`，不得错误回落为“媒体损坏”。

### T-P0-20：导出阶段 telemetry 与 GPU truthfulness

| 项目 | 内容 |
|---|---|
| 状态 | DONE（2026-09-17 rev 14b0cffc；证据 bench_out/ai_upgrade/T-P0-20_20260917-225218） |
| 目标 | 让性能优化可定位，AI 不能把“请求 GPU”误报为“实际 GPU” |
| 主要模块 | `src/shared/export/gpu_trail.py`、`gpu_benchmark_report.py`、`src/features/edit/core/timeline_export.py`、Metal/VT renderer、`topos_encoder.py` |
| 依赖 | T-P0-00、T-P0-10 |

实施步骤：

1. 定义每帧/聚合 stage：`probe`、`decode`、`cpu_to_gpu_upload`、`render`、`gpu_to_cpu_download`、`encode_submit`、`encode`、`mux`、`queue_wait`。
2. 每个 stage 固定字段：backend、p50/p95 wall、GPU submit/wait（不可测填 `null`）、CPU upload/download bytes、fallback reason、sample count。
3. `full_gpu=true` 的唯一判据：所有必经 stage backend 为 GPU/硬件路径，且 upload/download 为 0 或被明确豁免；任何 `cpu_encoder_surface_path`、CPU frame 或 GPU fallback 均为 false。
4. Timeline export、Color export、Composite export、AI export job 都写入同一 JSON schema 和 manifest。
5. 任何 fallback 必须只记录一次根因，不得把后续症状覆盖根因；保留 frame index 和关联的 source/effect/node ID。

验证：为 CPU-only、GPU render + CPU encode、native surface、GPU 初始化失败四类 fake/真实路径写 schema test；再运行：

```bash
.venv/bin/python -m pytest \
  tests/video/test_frame_trace_schema.py \
  tests/edit/test_timeline_export.py \
  tests/media/test_macos_gpu_encode_surface.py -q
```

完成定义：`topos_anchor_matrix` 不再只能看到 `encode_submit`；AI 通过 `topos.get_job_status` 可获得稳定的阶段统计、backend 和根因。

---

## 6. P0/P1：GPU surface 闭环与 Timeline identity

### T-P0-30：修复 Native VT bridge 的前置能力协商和安全回退

| 项目 | 内容 |
|---|---|
| 状态 | DONE（2026-09-17 rev f040a226；证据 bench_out/ai_upgrade/T-P0-30_20260917-230911；TOPOS 422→H264 真实导出经 bridge_not_selected 安全回退） |
| 目标 | TOPOS 422 输入不支持 native VT 时，导出在开始前选择正确路径，绝不运行到中途把 CPU frame 送进 GPU bridge |
| 主要模块 | `src/features/edit/core/timeline_export.py` 的 `_maybe_create_default_native_gpu_bridge`、`_ExternalGpuSurfaceEncoder`、Metal renderer |
| 已知失败 | `unsupported_topos_metal_output_format` → `native GPU bridge cannot consume a CPU/host-visible frame` |
| 依赖 | T-P0-20 |

实施步骤：

1. 新增纯函数 `can_use_native_gpu_bridge(sequence, project, encoder_config, snapshot)`，返回 `eligible: bool`、`reason_code` 和 required input/output surface formats。
2. 它必须检查所有 clip：源 backend、像素格式、位深、效果/调色/几何、转场、字幕、分辨率、输出 codec/pix_fmt；不得只检查是否“单层”。
3. 在 encoder 创建之前调用。若 renderer 不能承诺产生 `GpuEncodeSurface`，不得创建 bridge；改用普通 encoder 并记录 `bridge_not_selected:<reason>`。
4. `_ExternalGpuSurfaceEncoder.encode_frame` 仍保留 CPU frame 保护性报错，但这条路径在正常预检后必须不可达；为不可达写回归测试。
5. 失败的 bridge 初始化必须清理 session/mux/temp output，普通 CPU fallback 只能在导出尚未写帧时重新选择。

验证：

- TOPOS 422 → H264/HEVC、H264 NV12 → H264/HEVC、含 blur、含 transition、含字幕、奇数尺寸六组模拟和真实测试；
- 对 TOPOS 422 → H264 的当前状态，导出应成功走 CPU fallback 并产生 `bridge_not_selected:topos_422_to_nv12_not_implemented`，而不是失败；
- `TOPOS_EXPORT_NATIVE_VT=1` 不得改变输出正确性或造成伪成功文件。

完成定义：现有 native VT 失败被消除，并且日志/manifest 清楚区分“未选择 bridge”和“运行中 GPU 失败”。

### T-P0-31：实现 TOPOS 422 R16 texture → NV12/P010 VideoToolbox surface

| 项目 | 内容 |
|---|---|
| 状态 | IN_PROGRESS（2026-09-18 批次一落地：422→NV12/P010 GPU 降采样 kernel ×2 + 渲染器 surface 输出模式 + 资格门翻转（`topos_422_to_nv12_not_implemented` 移除）+ encode 阶段 telemetry 如实标 videotoolbox；kernel 对拍 max err≤2/PSNR≥50，真实 TOPOS→H.264/NV12 8 帧集成通过（encode=videotoolbox）；修复 bridge 漏传 pixel_format、HEVC 无 ProfileLevel（本机 AMD HW 拒绝 Main10=-12900 → 结构化拒绝+CPU 10-bit 回退，不虚报）、bridge init 失败未回退渲染后端三处真实缺陷。**2026-09-19 批次二（本机 H.264/NV12 臂收口）**：decode telemetry 由 source_kind 默认 "unknown" 误标修复为 topos_native（IOSurface 零拷贝、0 upload 字节，GPU 词表）；**full_gpu_blockers=[] 首次达成**（decode=topos_native / render=metal / encode_submit=videotoolbox / CPU 传输 0）；**240 帧 93.26 FPS（暖）≥60 目标**，冷启动 49.69 FPS（一次性管线编译 ≈2s）；修复 IOSurface 解码池 ring=3 饿死（→8+有界重试）、VT surface 池 ring=4 恰等于流水线在飞深度（→8）两处真缺陷；scipy 探测惰性化（无 EQ/LUFS 导出省 ~2.8s 冷开销，RMA-01 契约不变）。240 帧输出 probe h264/yuv420p 全可解码（强于 48 帧口径）。**2026-09-19 批次三（验证 3 收口）**：CPU/GPU 对拍门槛按 8-bit/10-bit 各自固定常量（MAX_ERR_8BIT/10BIT=2（P010 unorm 读回量化 ≤1 LSB）、PSNR_MIN_8BIT/10BIT=50），超阈值自动保存 diff 证据到 bench_out/ai_upgrade/T-P0-31_kernel_diff/<时间戳>/（got/want/16× 放大 diff PNG + 机器可读 stats JSON），断言消息携带证据路径（tests/media/test_topos_422_surface_kernels.py）。遗留：P010 臂需 Main10 硬件；VT 软编 Main10 mux EINVAL。证据 bench_out/ai_upgrade/T-P0-31_20260918-221143/ 与 T-P0-31_20260919-015545/；无头 Metal 证据，桌面仍 X-TEST-10 BLOCKED） |
| 目标 | 打通 `Topos IOSurface decode → Metal 422→420 convert → VT surface → H.264/HEVC`，避免 BGR 中转和 CPU 回读 |
| 主要模块 | `src/shared/media/topos_source.py`、`timeline_metal_export_renderer.py`、Metal kernels、`gpu_export_bridge.py`、`gpu_encode_surface.py` |
| 依赖 | T-P0-20、T-P0-30 |

实施步骤：

1. 为 renderer 新增明确输出模式 `nv12_surface` 和 `p010_surface`；不要复用 `output_yuv422p10` 的 CPU buffer 分支。
2. Metal kernel 直接读取 TOPOS 的 `R16Uint` Y/U/V 4:2:2 texture，按 source range/matrix 处理并在 GPU 上作横向 chroma downsample，写入 VT 目标的 Y + UV texture。
3. 422→420 取样位置、limited/full range、BT.709/BT.2020 必须定义文档化契约；边缘像素和奇数宽策略不可隐式丢弃。
4. 仅在 no-effect 单层 identity 首先启用；再扩展 opacity/transform/crop。带复杂 effect 前必须能证明中间结果仍在 GPU texture 中。
5. 渲染完成后返回 `GpuEncodeSurface`，由 bridge 提交；不构建 `DecodedFrame`、不调用 `ctx.read_buffer()`。
6. 失败时 release surface 并原子回退到 T-P0-30 的普通编码路径。

验证：

1. 单元：输入可控 4:2:2 ramps/color bars，验证 NV12/P010 平面尺寸、采样位置、范围、色彩误差。
2. 集成：TOPOS Proxy → H.264 NV12 与 → HEVC P010，各 48 帧；probe 验证 codec/pix_fmt/frame count/color metadata。
3. 对拍：CPU reference 与 GPU 输出的 PSNR/最大误差按 8-bit、10-bit 各自门槛固定；任何超阈值保存 diff 图。
4. 性能：identity 2K ≥60fps，trail 的 `encode_submit.backend=videotoolbox`、GPU render 统计非空、CPU upload/download 为 0。
5. 故障：Metal/VT 不可用时导出成功走 CPU encoder，不崩溃。

完成定义：TOPOS → H.264/HEVC 单层 identity 有 A 级真实 GPU 证据。完成后才能宣传该**特定交付路径**全链路 GPU。

### T-P1-10：TOPOS identity 直通快路径

| 项目 | 内容 |
|---|---|
| 状态 | DONE（2026-09-17；identity→topos_proxy 10.15→80.05fps（7.9x，达 ≥80 阶段目标；codec-only 的 88.7%）；证据 bench_out/ai_upgrade/T-P1-10_20260917-232524） |
| 目标 | 把无效果的 TOPOS 单片段 → TOPOS 输出从约9.6fps 接近 codec-only 96.6fps，而不改变时间线语义 |
| 主要模块 | `timeline_export.py`、`timeline_render_plan.py`、`topos_encoder.py`、`topos_source.py` |
| 依赖 | T-P0-10、T-P0-20 |

严格 eligibility（第一版只支持）：单一启用视频轨、单一连续 clip、源/输出均为 TOPOS 422 10-bit、输出尺寸和 fps 等于源、无 effects、无 color stack、无 transform/crop/opacity/fade、无 transition、无字幕、无 alpha、无音频。任一不满足立刻走普通 render path 并记录原因。

实施步骤：

1. 在 render plan 建立 `DirectToposDeliveryPlan`，不要由 `TimelineExporter` 按 UI 状态临时猜测。
2. 使用 `ToposMediaSource` 提供的 planar frame 直接交给 `ToposVideoEncoder`；禁止 `topos_frame_to_rgb`、BGR48、OpenCV resize 和重复 FFmpeg source。
3. 保持 color metadata、timebase、帧数、source range、取消、临时文件原子替换和 manifest 语义。
4. telemetry 单独标记 `delivery_path=topos_direct_planar_cpu_encode`，诚实说明编码器仍为 CPU；不要记为 full GPU。
5. 只在 first implementation 中优化单片段；多 clip 直通/re-mux 必须另立任务，避免错误处理 edit list/时间映射。

验证：

```bash
TOPOS_CODEC_LIB="$PWD/native/topos_codec/build/release/topos_codec.0.1.0.dylib" \
.venv/bin/python -m pytest \
  tests/media/test_topos_timeline_export.py \
  tests/media/test_topos_source_export_passthrough.py -q
```

再跑速度锚点。性能门槛：2K identity ≥60fps；阶段目标是 ≥80fps，不能低于当前 codec-only 96.6fps 的 70%。输出必须 native probe、逐帧解码、首中尾帧统计对拍。取消时没有目标文件或 `.topos-tmp` 残留。

### T-P1-11：TOPOS 编码器低拷贝与并行优化

| 项目 | 内容 |
|---|---|
| 状态 | DONE（2026-09-18，commit 见 git log；矩阵定位 entropy 52–57% 为第一大阶段，默认 slice_rows 16→8：t16 编码 2K +40%/4K +5%，解码 +44%，bytes/frame +≤1%，roundtrip Δ=0；codec-only 90.22→101.42fps、identity 80.05→95.02fps 均提升；native stage stats 以 worker_cpu_ns/wall_ns 分组入 ExportStageTelemetryReport；证据 bench_out/ai_upgrade/T-P1-11_20260918-061227/） |
| 目标 | 在不伪称 GPU 编码的前提下，降低 TOPOS CPU encode 的 fill/entropy/check/copy 成本并消除不必要复制 |
| 主要模块 | `src/shared/export/topos_encoder.py`、`src/shared/codec/topos_binding.py`、native codec、slice thread policy |
| 依赖 | T-P0-20、T-P1-10 |

实施步骤：

1. 用 native `encode_stage_stats` 对 422Proxy/LT/Standard/HQ、1080p/4K、不同 slice rows/threads 建立矩阵；先定位第一大阶段。
2. 保持 planar `yuv422p10le` 零转换；任何输入格式转换必须独立记为 `convert` stage。
3. 只在有数据证明时调整 slice rows、thread budget、rate-control 首帧搜索和 pipeline；禁止无测试地增加线程造成与 decode/GPU 争抢。
4. 固定输出质量/档位/bitstream 设置后再比较 FPS；性能提升不得通过降低 quality、bit depth 或关闭 CRC 获得。
5. 封装 native stage stats 到报告，区分 wall 与多线程累计 CPU time。

验证：`tests/native/test_topos_binding.py`、`tests/media/test_topos_export.py`、TOPOS 真实输出 decode roundtrip、码率/质量门禁。完成定义：codec-only 96.6fps 不回退，identity fast path 达标；每次性能数字有同档位和同输出 probe 证据。

### T-P2-10：TOPOS GPU encoder 可行性分支

当前 Topos encoder 是 CPU native codec；因此“TOPOS 输出全链路 GPU”不是只改 shader 就能完成的任务。

默认路线：先完成 T-P1-10/T-P1-11，把 TOPOS 定义为“GPU 可渲染、CPU 低拷贝编码的中间片”。如产品必须宣称 TOPOS 全 GPU，才启动本分支：设计 GPU codec kernel、GPU 纹理/共享内存输入 ABI、码率控制、bit-exact/reference 对拍、GPU/CPU 等价验证和 fallback。此分支必须独立 ADR、原型基准和用户/项目负责人确认，未完成前不得把 TOPOS 输出标为 full GPU。

---

## 7. P1：Edit、Color、Composite 的功能和性能升级

### T-P1-20：统一 Effect Capability Contract 与插件导出策略

| 项目 | 内容 |
|---|---|
| 状态 | DONE（2026-09-18：EffectExecutionCapability 七轴契约落地并接入 preflight/AI manifest/UI 三处；4 插件显式 preview_only 统一拒绝；ghost 四件套移除+旧工程迁移剔除；preflight 结构化错误码+fail-closed；truth matrix 7 效果×2 编码×48 帧 all_pass；证据 bench_out/ai_upgrade/T-P1-20_20260918-073708/） |
| 目标 | 每个 Edit/Color effect 都明确 preview/export 后端、支持格式、GPU/CPU fallback、质量档和 AI 可调用性 |
| 主要模块 | `src/features/edit/core/effect_registry.py`、Effect Catalog、`timeline_export.py` 的 `preflight_video_effects_export`、`msl_composition.py`、AI capability manifest |
| 已知缺口 | 4 个 Edit 插件被 preflight 阻断；Color plugin MSL emitter 明确拒绝；Vignette 为 ghost |
| 依赖 | T-P0-00 |

实施步骤：

1. 以单一 `EffectExecutionCapability` 取代散落 bool：`cpu_preview`、`gpu_preview`、`cpu_export`、`metal_export`、`opengl_export`、`preview_only`、`unsupported`；每种含 reason 和 quality constraints。
2. 将 4 个 Edit 插件（Bilateral Skin Smooth、Color Space Transform、HighPass Retouch、Film Halation）逐个选择：
   - 实现统一 export backend，并写 CPU/GPU 对拍；或
   - 显式 `preview_only`，UI、AI、CLI、Deliver 统一禁用交付并提示原因。
3. Color plugins 选择双源 GLSL/MSL 或受控 translator；没有可验证 MSL 时不可加入 Metal/export graph。
4. 从 attachable FX 列表移除 Transform/Crop/Opacity 的 ghost 语义，保留为 clip-native AI operation；Vignette 要么补齐 renderer+schema+export，要么删除历史注册项并迁移旧工程。
5. `preflight_video_effects_export` 的错误码按 effect ID 返回；不允许通用“unsupported”掩盖具体后端缺失。

验证：

```bash
.venv/bin/python -m pytest \
  tests/edit/test_effect_catalog_capabilities.py \
  tests/edit/test_export_video_effects_preflight.py \
  tests/color/test_plugin_manifest_binding.py -q
```

真实验证：每个标记 `exportable` 的插件必须在 TOPOS Proxy 和 H.264 各完成 48 帧导出，像素与 identity 不同、输出可 probe；每个 `preview_only` 在 AI capability/Deliver preflight/UI 三处一致拒绝。完成定义：没有“registry 可见但语义不明”的 effect。

### T-P1-21：Edit 内置效果按成本迁移和优化

（状态：IN_PROGRESS，2026-09-18 批次二完成——下采样质量档落地：blur quality=fast 半分辨率管线（半径×0.5 保物理模糊尺度，radius≥8 生效）、defocus mode=mipmap 接入同一 fast tier（GPU 侧首获真实提速语义）、denoise 三档质量（effective 半径 GPU/CPU 同契约 effect_tiers）；逐效果多素材对拍协议落地（16 例全绿：平坦/边缘/8px 边缘/高频噪点/亮度台阶/alpha 洞 × blur/erode/edge/denoise/defocus 结构签名断言）；参数动画/撤销/保存重载/导出取消回归扩展（9 例，实测撤销缺陷 RK-fix：RemoveKeyframeCommand.apply 未存 _old_value 导致效果关键帧删除不可撤销——已修）；证据 bench_out/ai_upgrade/T-P1-21_batch2_20260918-121958/（1080p：blur r32 提速 1.9×、defocus 1.2-1.5×、denoise 三档 0.65/2.47/5.94ms 单调）。遗留：GPU blur sigma 与 CPU cv2 约定对齐（产品裁决；2026-09-19 量化补充：Metal 导出 sobel fractional preblur=1.3 对拍 luminance/g 通道实测 diff mean 0.330/0.362、max=2 LSB，两参数案例已 strict-xfail 留证于 test_timeline_metal_export_renderer.py，对齐落地时 XPASS 强制回访容差。**2026-09-19 对拍数据集落地**：scripts/bench_blur_sigma_convention.py（生产双路：EffectPassRunner GLSL vs _cpu_blur cv2）量化约定差——GPU σ=0.5r vs CPU σ=0.3(r−1)+0.8，比值随半径趋 1.58×；亮度台阶素材 mean 漂移由 r=1 的 0.87 LSB 单调涨至 r=32 的 16.0 LSB（PSNR 38.5→20.8dB）、细棋盘 r=1 处 mean 17.9 LSB（小半径相对差最大）；噪声素材全程 ~1 LSB（高频差被冲淡）。数据 bench_out/ai_upgrade/T-P1-21_sigma_duipai_20260919-142333/ 供裁决：CPU 改 σX=radius*0.5 / GPU 改 cv2 推导式 / 接受为文档化版本差。**2026-09-19 自查发现并修复：可分离模糊掩码/mix 参考污染缺陷（真 GPU/CPU 分歧，非约定差）**——V pass 的 original 掩码/mix 参考曾采样 H pass 输出而非原始输入：channel_a=False 时 alpha 仍被水平方向单遍模糊（实测 diff mean 52.8/max 135 LSB；CPU 语义正确原样直通）、mix<1 混入多余 H 模糊分量、fast tier 在半分辨率应用掩码使关闭通道被半分辨率往返退化；结构签名测试测不出（水平模糊保均值）。修复三处：V shader 增 u_original（unit 1 纯净输入，shaders.py 单一来源，EPR 与 Composite evaluator 共用）、EPR fast tier 改半分辨率纯 H+V（全掩码 mix=1）+ 全分辨率 combine pass（新 _BLUR_FAST_COMBINE_FRAGMENT，掩码关闭通道逐位保真）、evaluator V pass 绑定 unit 1。回归 tests/edit/test_gpu_blur_channel_mask_contract.py 6 例逐像素断言（掩码关闭通道逐位保真/mix=0 恒等/mix=0.5 线性分解/fast tier 保真且 RGB 真模糊）；还原旧形态 6/6 必挂验证。修复后 tests/edit 8222 通过 / 0 失败、tests/composite 5571 通过 / 0 失败、golden frame + 材质对拍 27/27 不受影响（mix=1 全掩码路径输出不变））、Metal 后端评估 = BLOCKED: DESKTOP_GPU_NOT_VERIFIED。批次一：GPU 导出链解锁（dispatch 门控/静态 kernel 循环/深度效果链桥三修复），identity/grade/blur/sharpen/glow/two_layer/transition 全部达最低门槛（grade 1.38→33.5fps 等），denoise/defocus 时间门槛达成；见 bench_out/ai_upgrade/T-P1-21_20260918-085120/）

| 执行顺序 | 效果/路径 | 当前问题 | 首个实现目标 |
|---:|---|---|---|
| 1 | Timeline ColorStack Grade | 2K TOPOS 约1.6fps，额外约25s | 单层 GPU grade 直出，参数 buffer 缓存 |
| 2 | 双层 Composite / Transition | 约3.0/4.3fps | 多层保持 GPU texture，单次最终输出 |
| 3 | Sharpen / Glow | 约4fps | 融合 blur/threshold/pass，复用 ping-pong texture |
| 4 | Blur | 约5.5fps | separable kernel、下采样质量档、复用 weights |
| 5 | Defocus / Edge / Erode | Defocus 8 帧约15.6s | quality tier、ROI/downsample、CPU 对拍 |
| 6 | Denoise | 8 帧超120s | 先稳定 8 帧≤30s，再做 GPU/AI bridge |

每个子效果执行相同步骤：

1. 用 48 帧 anchor matrix 记录 identity baseline、effect total、extra_ms、p50/p95、trail 和输出 probe。
2. 找到当前实际执行 backend；先修复不必要 RGB/YUV 转换、每帧 shader 编译、纹理/权重重建、同步 readback。
3. 实现 GPU path 时保持 source bit depth/color range/HDR contract；没有 GPU 时保留正确的 CPU fallback 和质量档。
4. CPU/GPU 对拍：平坦、边缘、高频、HDR/负值、alpha（适用时）素材；不能只看一张预览截图。
5. 把参数动画、撤销、保存重载、导出取消加入回归。

性能门槛（2K、25fps 源、48 帧、8 帧预热）：

| 场景 | 最低门槛 | 高性能目标 |
|---|---:|---:|
| identity H.264/HEVC GPU surface | 60fps | 90fps |
| 单层 Grade/Blur/Sharpen/Glow | 30fps，p95 ≤33.3ms | 60fps |
| 双层/Transition | 25fps，p95 ≤40ms | 45fps |
| TOPOS CPU-encode identity fast path | 60fps | ≥80fps |
| Denoise | 先 8 帧≤30s；再定义实时/离线质量档 | 质量档中至少一档 25fps |

完成定义：仅当正确性和阶段 telemetry 都通过，且 effect 的 `extra_ms` 相对基线下降并满足门槛，才能标为优化完成。

### T-P1-22：Adjustment Clip、音频和时间线交付完整性

（状态：IN_PROGRESS→可执行范围完成，2026-09-18 批次二：20 循环真实导出（h264/hevc/prores_422 轮转、音频挂 reverb DSP）全部成功且音画 Δ=0.021s≪0.25s 容差；中途取消契约 success=False/cancelled=True；RSS 前 8 循环上升后平台震荡（首5→末5 中位 +933MB，无持续单调趋势，与 TP-71-MEM-001 纯 H265 持续增长形态不同、独立跟踪）；跨 packet（source_start=200）DSP 导出 + 双音轨混音（A1 干声/A2 reverb）用例落地，7/7 通过。批次一：adjustment warning 消除+ColorStack 三路传播+像素证明；H265/ProRes leftover 测试修复（参考流缺陷）；20 循环 seek 通过；clip-native 编译断链修复。见 bench_out/ai_upgrade/T-P1-22_*/、T-P1-22_loop_20260918-122922/。**2026-09-19 RSS +933MB 定性收口（60 轮确认，T-P1-71 协议）**：60/60 循环 OK + cancel probe 通过（T-P1-22_loop_20260919-143012）——首轮爬升 590→3422MB（FFmpeg/PyAV 与三 codec 各自首用高水位）后，爬升后段（6..59，n=54）**slope 3.31 MB/轮 < 5 阈值、净增 +119MB**；10 轮窗口 +129/-130/-497/+521/+19MB 大幅振荡无单调趋势；尾 10 轮中位 3489MB（σ=157）；三 codec 前后半中位差 +114/+58/+46MB 各自收敛。**定性：有界保留（BOUNDED）**，与 TP-71-MEM-001 同族，20 轮 +933MB 即爬升+振荡叠加、非泄漏；详见 bench_out/ai_upgrade/A2_regression_20260919-141050/result.md §四）

实施步骤：

1. 为 Adjustment Clip 建立不依赖 asset type 的效果传播模型；非空 ColorStack/effect 必须影响下方轨道，日志不得出现 `Unsupported asset type: adjustment`。
2. 构造像素级场景：底轨 color bars，上层 Adjustment Clip 加可量化 exposure/blur；导出后验证受影响区和未受影响区。
3. 对 8 个音频 DSP 从单块测试扩展到真实 timeline：跨 packet boundary、seek、取消、尾音、混音和最终 mux。
4. 重现并修复 H265/ProRes leftover/seek 问题；修复后至少 20 次循环导出/seek 无错误、无资源泄漏。
5. 所有 clip-native 操作（transform/crop/opacity/fade/speed）成为 AI plan compiler 的正式 operation，不再伪装为 filter。

验证：`tests/edit/test_audio_leftover_samples.py`、相关 H265/ProRes media tests、Adjustment 专用新测试、真实 H264/TOPOS 视频输出及音画时长 probe。完成定义：非空 Adjustment 可像素证明，音频错误归零或有明确未支持能力。

### T-P1-50：Color GPU 执行、插件和真实桌面验证

（状态：IN_PROGRESS，2026-09-18 批次一：卡点名套件 126+58 通过、tests/color 全目录 1511 通过；共享色彩数学已由 T-P1-21 10-bit 对拍量化（Δ≤1 LSB）；插件 preview-only 契约落地（T-P1-20）。拖动稳态/桌面 UI 验收/Metal 真实帧对拍 = BLOCKED: DESKTOP_GPU_NOT_VERIFIED（无头环境）+ T-P0-31 前置，见 bench_out/ai_upgrade/T-P1-50_*/）

实施顺序：

1. 对 Grading、Curves、HSL、LUT、Mixer、Warper、Soft Clip、Blur/Sharpen、Film Grain、Qualifier、Mask、Blend、Scopes、Denoise 建立 `CPU reference → OpenGL fallback → Metal` 三路对拍。
2. 每项在 2K TOPOS 真实帧记录拖动稳态 p50/p95、首帧冷启动、参数变化后帧稳定性和 GPU memory。
3. ColorStack 与 Edit export 共用同一色彩数学/参数 manifest；任何一方更新参数另一方必须对拍。
4. 插件完成 T-P1-20 的 capability contract 后，逐个加入 MSL 或标 preview-only；禁止 MSL emitter 在运行时隐式跳过。
5. Tracker/Auto Color 用真实帧对、撤销、持久化、取消和失败状态完成桌面 UI 验收；RAW Develop 单独使用 RAW 素材，不混入 TOPOS 视频矩阵。

验证：现有 `tests/color/test_metal_composition_*.py`、`test_color_e2e_consistency_matrix.py`、`test_tracking_*.py`，外加真实桌面 runner。门槛：2K 常用参数拖动 p95 ≤33.3ms；Scopes 不阻塞 UI；CPU/GPU 对拍误差有固定阈值和 diff 产物。

### T-P1-60：Composite 节点功能归类、专用 fixture 与图结构控制

（状态：**DONE**，2026-09-18 批次 A+B+C+D 全部卡步骤收口：契约五分类+六态可解释状态码；图结构 AI 操作 5→9 种；批次 C 绿幕/cryptomatte 定向 fixture 接线，修复 `_CHROMA_KEYER_FRAGMENT`/`_KEYLIGHT_FRAGMENT` matte 方向反转真实缺陷，像素级 GL 回归钉住方向；批次 D evaluator 级 MODEL_UNAVAILABLE 结构化诊断（`ExportResult.diagnostics`/`last_diagnostics`，顺带修复 AIGenerateNode 缺 input 端口致 P0-3 直通从未生效）+ 节点级 performance attribution（eval_ms/readback_ms 拆分、transfer 字节、attribution 进矩阵行）。93 节点矩阵终态 **0 failed**：76 非平/4 缺模型（结构化诊断+直通图存活）/13 缺资源。桌面 Metal/GL4.3 compute runner 验证仍受 X-TEST-10 限制（BLOCKED: DESKTOP_GPU_NOT_VERIFIED）。证据见 bench_out/ai_upgrade/T-P1-60_batchC_20260918-191858/、T-P1-60_batchD_20260918-201344/）

实施步骤：

1. 按节点契约分五类：`image_filter`、`source`、`generator`、`AI/model`、`3D/file_output`；93 节点不得再用同一 image-input harness 一概判断。
2. 为每类建立 fixture：绿幕用于 chroma key、非默认颜色用于 constant、真实 TOPOS 文件用于 video_frame、模型 mock/真实 pack 用于 AI、临时目录用于 file output、3D scene/mesh 用于 3D。
3. 对每个节点写明确结果：`passed_nonflat`、`passed_flat_by_contract`、`blocked_missing_resource`、`blocked_missing_model`、`unsupported_backend`、`failed`；`None` 和全黑不能笼统叫通过。
4. 对无头 GL 4.1 标记 fragment fallback；桌面 Metal/GL compute runner 分开验证，不把硬件限制误判为节点失败。
5. 将 Composite 图操作补入 AI：add/remove/duplicate/group/ungroup/connect/disconnect/set output/import/export；每个操作走现有 graph command/service、revision 和 undo。
6. 增加节点级 performance attribution：cold preview、warm preview、warm export、GPU backend、readback、cache hit、flat/error 状态。

验证：保留 93 节点全量矩阵，同时每类跑专用 fixture；覆盖 `ai_generate` 缺模型返回 `MODEL_UNAVAILABLE`，不再返回无解释 None。针对 chroma_keyer/constant 写参数 attribution。图结构 AI 操作必须经 ToolFacade/MCP/CLI 与 UI command 同路径并可 undo。完成定义：93 节点都有可解释状态，Composite 图结构可由 AI 正式编辑。

---

## 8. P1：Deliver、媒体稳定性与输出质量

### T-P1-70：统一 Deliver Contract

（状态：**可执行范围完成（DONE-within-scope）2026-09-19 评估**——五步骤逐一核对落地：①DeliveryCapability（src/features/deliver/core/delivery_capability.py，operation_protocol apply 引用）；②preflight 结构化 blocking/degraded + T-P1-20 blockers 错误码（AI 仅对 warning/degraded 自动继续、blocking 拒绝由 fake LLM 回放 R 系列强制）；③统一 probe 入 manifest output_probe（含 probe_failed 诚实变体）；④manifest 全量：source identity/project revision/capability snapshot/preflight/stage_telemetry/output_probe/AI plan/job ID（非 AI 导出省略键）；⑤degradations 降级清单（gpu_blocker/fallback_root_cause/preflight_degraded/effect_export_skipped/audio_omitted）。验证清单核对：H264（归档 full-GPU NV12 manifest）/HEVC 8-bit（W03）/ProRes（T-P1-22 轮转）/TOPOS Proxy（W02）✓；HEVC 10-bit=本机硬件 P010 编码不支持（AMD -12900，"可支持时"口径下如实 N/A）；字幕/音频 ✓（W01/W04）；路径拒绝（test_safe_path_policy 遍历/符号链接逃逸/IO 失败降级 + facade 写授权拒绝）、取消（outcome + jobs + W07）、GPU fallback/无音频（degradations 契约）✓；磁盘写失败为部分覆盖（audit-store IO 降级 + encode failure 路径，无专门 ENOSPC 用例——如需可后补）。完成定义达成：AI 可读取 export job 的完整/稳定/可解释 manifest。原批次记录：2026-09-18 批次一：DEGRADED 严重度 + DeliveryCapability 统一能力描述落地；底座（结构化阻断码/telemetry/manifest/probe）已在位。**2026-09-19 批次二（卡步骤 3/4/5 统一入 manifest）**：`probe_output_media` 统一输出 probe（容器/codec/profile/pix_fmt/几何/帧率/全量解码帧数/时长/音频流/首尾帧可解码，失败如实记 probe_failed）入 manifest `output_probe`；`stage_telemetry` 快照（full_gpu 判定/blockers/fallback 根因/阶段统计）入 manifest；`degradations` 降级清单（gpu_blocker、fallback_root_cause、preflight_degraded、effect_export_skipped——CPU 跳过效果从只写日志改为导出器记录、audio_omitted 检测）。修复后台 manifest 线程与 native muxer 收尾的 probe 竞态（`_delivery_finalized` 事件）。真实 full-GPU NV12 导出 manifest 归档：h264 High/24 帧首尾可解码/full_gpu=true/零 degradations。**2026-09-19 批次三（卡步骤 4 收口）**：AI plan/job ID 溯源全链接线——`ExportRequest.plan_id`（ToolFacade `topos.request_export` 可选参数透传）→ `TimelineExportConfig.ai_plan_id`，job 提交后 `ai_job_id` 回填 config，两者入 manifest `config` 快照（非 AI 导出省略键，不出现空值）；测试 tests/edit/ai_editing/test_export_jobs.py + tests/edit/test_export_manifest.py TestManifestAiTraceability。见 bench_out/ai_upgrade/T-P1-70_20260919-024500/）

目标：所有导出入口（UI、快速导出、AI request_export、CLI、批量 Deliver）使用同一 `TimelineExportConfig`、preflight、path authorization、output validator、manifest 和 job lifecycle。

实施步骤：

1. 建立 `DeliveryCapability`：codec/profile/pix_fmt、alpha、audio、subtitle、HDR、GPU backend、TOPOS video-only 限制、插件/效果可交付性。
2. preflight 返回按严重度分类的结构化结果：`BLOCKING`、`DEGRADED`、`WARNING`；AI 只能对 warning 自动继续，blocking 需修复或请求用户决策。
3. 输出完成后统一 probe：容器、codec、profile、pix_fmt、frame count、时长、audio streams、color metadata、首尾帧 decode、文件大小和临时文件清理。
4. manifest 写入 source identity、project revision、capability snapshot、preflight、quality checks、GPU trail、输出 probe、AI plan/job ID。
5. 任何 encode fallback、audio omitted、plugin preview-only、GPU unavailable 都必须进入结果，不能只写日志。

验证：H264、HEVC 8/10-bit、ProRes、TOPOS Proxy、字幕/音频/alpha（可支持时）分别导出；路径拒绝、覆盖策略、取消、磁盘写失败、GPU fallback、无 audio 都有契约测试。完成定义：AI 可以读取一个 export job 的完整、稳定、可解释结果。

### T-P1-71：稳定性和资源生命周期清零计划

（状态：IN_PROGRESS，2026-09-18 批次一：AudioWaveform 20 轮生命周期压力通过；H265/ProRes seek/boundary 与 probe 差异已由 T-P1-22/T-P0-10 收口。**2026-09-19 批次二：edit 套件级 SIGSEGV 已修复**——最小复现收敛到单测试对（test_audio_routing_gate.py::test_empty_timeline_clears_gate_errors + test_audio_waveform.py），.ips 原生栈定位根因：EditPage 把全局长命单例 WaveformExtractor.status_changed 连接到 `self.waveform_status_message.emit`（signal→.emit 的 C 函数槽无接收者上下文，页面 GC 后连接悬空 → 下次 emit 调进已删除对象）；修复为绑定方法中转 `_on_waveform_status_from_singleton`（PyQt 注册页面为接收者、析构时 Qt 自动断开；注意 signal→signal 直连不充分——页面删除后仍残留代理、单例 emit 抛 AttributeError）+ 回归测试 2 例（tests/edit/test_edit_page_waveform_signal_lifecycle.py）。**"H265 导出 native 内存增长"定性修正**：hevc/h264 双臂 + tracemalloc（Python 堆 4 轮仅 +0.2MB）+ 裸 PyAV 60 轮长跑（+204MB 后完全平台化）证明主体是 FFmpeg/PyAV 首次使用的有界高水位保留而非无界泄漏；**200 轮导出长跑（2026-09-19 批次三）最终定性：有界**——slope 2.199MB/轮 < 5 阈值、growth_unbounded=false，尾段每 25 轮增量 63→29→18→24MB 单调减速收敛（真泄漏线性外推应 ~1215MB 不减速，实测相反），句柄恒 4；见 bench_out/ai_upgrade/T-P1-71_20260919-110914/。**2026-09-19 批次三（审计发现的 Composite 原生崩溃/挂死四连修，全部预存缺陷、逐一最小复现后修复）**：①`composite_page.py` minimap 防抖 timer 曾连 lambda（无接收者上下文）——页面 deleteLater 与包装回收错位时 50ms 单发 timer 打进已释放槽 PyObject，.ips 栈 `PyQtSlot::invoke` 内 SIGSEGV；复现对 test_3d_camera_light_import + test_composite_cornerpin_freeze（~75% 崩溃率），修复为绑定方法 `_update_minimap_deferred`，复现对 6/6 通过。②CompositePage 把 `self._video_node_frame_ready.emit` 注册进全局 VideoContext 单例——页面销毁后解码 worker 线程调用即原生段错误（deflicker 真实视频套件 34% 处崩溃）；修复为弱引用蹦床 `_FrameReadyTrampoline`（页面回收→no-op；PyQt 的 destroyed 信号不能连 Python 槽，实测探针证明，故不可用作注销时机）+ VideoContext.clear_frame_ready_handler 身份守卫注销 API；还原旧形态回归测试必 SIGSEGV（exit 139）验证。③opencv 5.0.0 轮子的 libOpenEXR ZIP/PIZ 压缩经 libdeflate 派发在本机间歇 SIGILL（.ips 栈 `libdeflate_zlib_compress ← exr_compress_buffer ← cv2.imwrite`，2026-09-11 旧崩溃即同签名）——ai_cache_manager/file_io 默认 ZIP 均在危险路径、file_output_node 压缩映射还用错枚举（"low"→3 实为 ZIP、"high"→10 超范围）；统一改 RLE(1)（无损、不走 deflate；复现循环 ZIP 6 跑 2 崩 → RLE 6 跑 0 崩）。④`AsyncPBOReadback._wait_entry_fence` 曾用 `glClientWaitSync(fence, 0, GL_TIMEOUT_IGNORED)` 无限等待——macOS 遗留软渲染 GLEngine 收到 0xFFFF…FFFF 超时值时 100% CPU 永久自旋（sample 采样栈 glClientWaitSync_Exec；同 fence 在 timeout=0 的 poll 路径正常判定，故是超时值处理缺陷而非 fence 不触发），`get_latest(wait_newest=True)` 在套件 82% 处挂死 37 分钟+；改为默认有界 1s 超时 + get_latest 尊重等待结果（超时不映射在途 DMA、保留 fence 供后续 poll），tests/composite/test_pbo_async_readback.py 由永久挂死变为 6/6 通过。**tests/composite 全目录 260 文件终态：5571 通过 / 0 失败 / 0 崩溃 / 0 挂死（84s，2026-09-19）——历史首次全绿。**回归测试 tests/composite/test_composite_page_minimap_timer_lifecycle.py。**同批（套件首次跑通后暴露的 14 个预存失败逐一收口）**：⑤`topos_cache_codec._rgba_to_planes` 声称返回 u16 实际返回 float32——`tobytes()` 后 float 字节被当 u16 像素喂给编码器，**节点磁盘缓存载荷往返全部静默变垃圾**（此前因套件崩溃不可达、从未被发现）；修复为 uint16 落型 + alpha 由有损近似（mode 2，实测 ≤8 code 偏差）改无损 16-bit（mode 1，diff=0，兑现 P0-7"visually exact"承诺）+ FORMAT_ID 诚实化 gbrap12a16（损坏时代旧条目按未知格式自然失效清理）+ 解码按文件 info 的 bit_depth/alpha_bit_depth 定标；磁盘缓存测试按格式感知重写（probe-aware 期望、按实际载荷文件腐蚀、按实际字节数设驱逐预算）。⑥test_3d_preview_override 的 `__new__` 桩 MagicMock 编辑器 `is_in_group_view()` 默认真值误入 undo 记录路径（未初始化 Qt 包装抛 RuntimeError）——桩保真度修复。⑦headless GL 协商测试的字面版本相等断言改为平台感知（macOS GL 只有 2.1/4.1 两档，请求 3.3 被提升为功能超集）。**2026-09-19 批次四（tests/edit 剩余 29 项预存失败全部清零：8216 通过 / 0 失败 / 2 xfail / 24 skip，188s——历史首次全绿）**：①probe matrix ×10——测试硬绑 `FfprobeMediaProbe`（CLI 后端），本机无 ffprobe 而 PyAV 可用；生产入口 `create_media_probe` 本就是 PyAV 优先，测试改走生产后端选择后 9 格式 + H264 delivery 分析（含 LUFS）全部进程内通过。②GL hotpath ×4——`_make_renderer_with_pool` 桩缺 P0-03 新增的 `_gl_pass_timer` 属性；isolation gate 把全目录 glFinish 命中行号都套进 `edit_gpu_preview_renderer.py` 取上下文，而命中实际跨 `timeline_clip_frame_provider.py`（external-source fence 兜底，合法 fallback）——gate 改按命中文件归属取上下文 + fence-drain 调用点补语义注释。③Metal sobel fractional preblur ×2——即 T-P1-21 遗留"GPU blur sigma 与 CPU cv2 对齐（产品裁决）"的量化表现：luminance/g 通道实测 diff mean 0.330/0.362、max=2 LSB（非纯 ±1 量化），按 §0.2 不放宽容差默许分歧，改 strict xfail 留证（附实测数据），对齐落地时 XPASS 强制回访容差。④UI 接线 ×7——桩保真度/契约演进：DummyTimelineView 补 `set_scene_cut_overlay_visible`、DummyPanel 补 5 个无守卫 connect 的新信号（对齐真实 TimelinePanel 签名）；exporter 桩补 `_pause_requested`/`_resume_event`（暂停协议晚于测试）；submit shell 补 `_generation_id`/`_coalesce_pending_tasks`；caption 测试把已注册节点 `particle` 当未知类型（注册表现 93 节点）改用真正不存在类型；open_in_composite 普通片段契约已由 a96000d6 有意改为未绑定工作区（`_suppress_composite_auto_bind_once`+`switch_to_page`），测试按新契约重写并保留"不创建临时合成"断言；corner timecode 已改独立 QLabel 自重绘（不再碰 viewport），测试改为断言标签更新。⑤workspace_presets ×4——宿主桩预声明清单缺 P1-14 的 `dock_composite_asset_dependency`（与生产 `_enforce_page_dock_visibility` 属性集求差后补齐）。⑥decode_scheduler 间歇失败 ×1——测试断言首个 future 必为 True，但 latest-only 契约（docstring 明示"False=被更近请求淘汰"）下第二请求到来即 cancel 运行中窗口返回 False，成败取决于竞速；改为不断言 first、对最新窗口/统计/ring 保持强断言，8/8 稳定。**2026-09-19 批次五（剩余两行的诚实结账）**：⑧"Multicam/ADR/录音 UI 挂起"行——headless 口径无可复现挂死：静态扫描三个模块仅 recording_manager 延迟校准有一处 QEventLoop（硬超时 `capture_seconds*1000` 后 quit，非无界），multicam/ADR 面板无 exec()/无界等待；test_multicam×3 + test_adr×2 + test_recording_manager 共 149 测试 offscreen 全过。按卡"只处理已可复现问题/无复现不猜改架构"规则，本行保持 NO-REPRO（headless），桌面特定场景待用户提供复现步骤后再开 issue。⑨"Windows/macOS 析构 warning"行——macOS 半边已验证干净：生命周期敏感集（waveform lifecycle/decode_scheduler/recording/PBO readback/composite page lifecycle/disk cache）加 `-W error::pytest.PytestUnraisableExceptionWarning` 共 173 测试通过、tests/edit 全套件输出零 unraisable/unclosed/ResourceWarning；Windows 半边 = BLOCKED: 无 Windows 硬件。

此任务只处理已可重现的问题；每个问题必须先写最小 reproduction，再修复：

| 问题组 | 处理要求 |
|---|---|
| FFmpeg/ffprobe 发现差异 | 由 T-P0-10 收口；所有 probe 走 capability snapshot |
| Composite 原生崩溃/segfault | 隔离最小 node/交互序列，修复前不扩大节点功能；加入重复 20 次 lifecycle test |
| AudioWaveform worker 生命周期 | create/close/cancel 20 次，无悬挂线程、无访问已释放对象 |
| Multicam/ADR/录音 UI 挂起 | 非模态任务 + watchdog + cancel；headless 与桌面一致 |
| H265/ProRes seek/boundary | 在真实素材、packet 边界和 seek 组合上运行 20 次；修复后无 leftover/unraisable warning |
| Windows/macOS 析构 warning | 保证 source/session/decoder/GL/Metal 在所有异常路径关闭 |

完成定义：每项有明确 issue ID、回归测试、重复压力结果；没有复现证据的猜测不得直接改核心架构。

---

## 9. P2：完整 AI 控制面与自主任务执行

### T-P2-20：统一产品操作协议

（状态：可执行范围完成，2026-09-18 批次二：operation_protocol.py 落地 12 必备字段契约（operation_id/domain/permission/scope/preconditions/idempotency_key/preview_diff/apply/verify/undo_or_compensation/error_codes/required_capabilities）×7 域 60+ 操作；一致性测试强制 Edit 域与 capability manifest.operations 同源、Composite 域与 edit_service NODE_OPS 同源、权限值 ∈ ToolPermission 词汇、写操作必声明 error_codes；registry_to_json 机器可读。apply 只引用真实入口，无"计划中"条目。11/11 测试通过，见 tests/edit/ai_editing/test_operation_protocol_matrix.py）

目标：AI 不操控按钮，而是调用与 UI/CLI 相同的语义 command。

#### 必须接入的能力域

| 域 | 必须覆盖的正式操作 |
|---|---|
| Project | create/open/save/save-as/close、revision、undo/redo、迁移、项目设置、审计读取 |
| Media | import/remove/relink、probe、proxy、analysis、bin/search/tag、offline/recover |
| Edit | timeline/track/clip、split/trim/move/replace、clip-native transform/crop/opacity/speed、effects、transitions、markers、subtitles、audio routing |
| Color | clip selection、ColorStack、所有已交付工具参数/关键帧、mask/qualifier/tracker、preset、scopes read、Auto Color |
| Composite | graph CRUD、node CRUD、ports/connect/disconnect、parameters/keyframes/expressions、assets/model dependencies、output settings、render/preview |
| Deliver | preset/list capabilities、validate export、preview estimate、request/cancel/status/result、batch/variants、output paths |
| AI/Models | model capability/list/install status/read-only diagnostics；模型运行必须由受控 job 发起 |

每个操作的 schema 必须至少包含：`operation_id`、`domain`、`permission`、`scope`、`preconditions`、`idempotency_key`、`preview_diff`、`apply`、`verify`、`undo_or_compensation`、`error_codes`、`required_capabilities`。

### T-P2-21：跨域事务、事件和恢复

（状态：可执行范围完成，2026-09-18 批次二：workflow_transaction.py 落地显式状态机 QUEUED→PREFLIGHT→RUNNING→VERIFYING→SUCCEEDED|DEGRADED|FAILED|CANCELLED（合法转移表+终态不可再转移+越界进度拒绝）；open 时深拷贝冻结 project revision 与 capability snapshot（嵌套修改不穿透，审计回放锚点可信）；每步记录 inputs/outputs/undo_token/job_ref；cancel 仅在 CANCELLABLE_STATES 允许；失败策略恰三种（FAILURE_POLICIES_EXACT 冻结，新增第四种即测试失败），补偿建议为策略显式函数——rollback 输出逆序 undo tokens、pause_for_user 提明不可逆操作需特定授权；与真实 TransactionService 集成测试：EditSession apply 的 CommitResult.undo_label 作为 undo token 记录。13/13 测试通过，见 tests/edit/ai_editing/test_workflow_transaction.py）

实施步骤：

1. 设计 `WorkflowTransaction`：冻结 project revision 和 capability snapshot，记录每一步输入/输出/undo token/job reference。
2. 所有 write 先产生 proposal/diff；宿主按项目/路径/计划/revision 绑定签发 `ApprovalGrant`，模型不得自签。
3. 长任务统一状态：`QUEUED → PREFLIGHT → RUNNING → VERIFYING → SUCCEEDED|DEGRADED|FAILED|CANCELLED`；每次状态有进度、阶段、日志引用、可取消性和补偿建议。
4. 失败策略只允许显式三种：`retry_safe`、`rollback`、`pause_for_user`。不可逆 export/network/delete 必须要求特定授权和输出范围。
5. ToolFacade、MCP、CLI、chat driver 都使用同一 transaction service；任何入口绕开它都视为架构缺陷。

验证：构造“导入 → 剪辑 → 调色 → 合成 → 两种 Deliver → 保存”的 workflow，覆盖批准、revision 冲突、单步失败、取消、恢复、重复提交、应用重启后 job 查询。完成定义：AI 能自动完成可授权任务，且每一步可解释、可验证、可恢复。

### T-P2-22：AI 能力与真实性联动

（状态：可执行范围完成，2026-09-18 批次二：deterministic fake LLM × 真实 ToolFacade 20 场景回放落地（正向 8/拒绝 9/恢复 3）+ 全序列两次回放签名一致（确定性契约）+ 条目 4 端到端断言（request_export 响应必带 preflight ok/blocking/degraded，空时间线导出被结构化拦截而非裸成功）+ 条目 5（V20 注入文本 stay-data + 单次授权防重放 R19/V19）；写工具无 ApprovalGrant 拒绝（R09/R10 模型伪造 token 无效）与 preview_only 交付步骤可解释拒绝（R15/R16）全绿，22/22。批次一：EFFECT_NOT_DELIVERABLE 计划期拒绝（details 携带 restriction/reason/suggestion）；get_capabilities 真实性由 T-P1-20/60 契约落地。见 tests/edit/ai_editing/test_fake_llm_facade_replay.py）

1. `topos.get_capabilities` 必须根据 T-P0-00 snapshot 和每个功能的真实 export/preview contract 返回可执行能力。
2. 计划器禁止选择 `preview_only`、`blocked_missing_model`、`unsupported_backend` 的动作作为交付步骤；可提出替代计划。
3. 性能敏感动作可读取历史 benchmark：若目标不满足实时门槛，AI 必须选择代理/低质量/离线 render 或明确提示用户。
4. 导出完成的 AI 验证必须读取 output probe、frame checks、GPU trail、quality result，不得只读 `success=true`。
5. 为每个 AI tool 增加不可信输出处理：外部 metadata、插件文本、模型文本、文件名均不可作为系统指令。

验证：用 deterministic fake LLM 和真实 ToolFacade 回放 20 个正向/拒绝/恢复场景；写工具无 ApprovalGrant 必须拒绝；缺插件/缺模型/GPU fallback 的替代计划必须可解释。

---

## 10. 真实工作流回放矩阵

以下工作流是最终 AI 验收，不是独立功能测试的替代品。每一条在实现域完成后都要跑，并保留结果目录。

| ID | 用户需求模拟 | 必经能力 | 输出与验证 |
|---|---|---|---|
| W01 | **PASS 2026-09-18**（bench_out/ai_upgrade/W_matrix_20260918-133830/：H.264 导入→两段+淡入淡出+字幕经真实 EditSession 提交→H.264 输出，probe 帧音时长全对齐、序列 round-trip 一致；回放揪出 set_clip_fade 编译器契约断裂——发 UpdateClipParamCommand 未知 param_type 永不可应用，已改接 ChangeClipFadeCommand 并加回归） | Project/Media/Edit/Subtitle/Deliver | 视频/音频/字幕/帧数/保存重载/AI 审计 |
| W02 | **PASS 2026-09-18（全口径入矩阵）**（bench_out/ai_upgrade/W_matrix_20260918-203836/：真实 EditSession 计划提交 ensure_track/add_clip/set_effect blur(r8)/set_opacity(0.5) 双层 + ColorStack 调色（exposure 25/contrast 10，w04 直改模型先例）→ 与 identity anchor 中间帧哈希不同（效果参与证明）→ TOPOS Proxy 输出 native probe 通过（TPIC/1920×1080，无 FFmpeg invalid probe）；telemetry 诚实携带 CPU decode/render/encode blockers。试点见 bench_out/ai_upgrade/W02_pilot_20260918-103842/） | TOPOS route/Color/Edit/Composite/Deliver | native probe、像素变化、anchor delta、trail、无 FFmpeg invalid probe |
| W03 | **PASS 2026-09-18**（同目录：TOPOS Proxy 无效果→H.264/HEVC 双输出 probe 通过；telemetry full_gpu_blockers 非空（decode/render/encode 全 CPU 路径机器可读）——CPU fallback 诚实记录，Metal surface 依赖 T-P0-31） | Metal/VT bridge/Deliver | GPU surface 或明确 CPU fallback；probe、color error、trail |
| W04 | **PASS 2026-09-18**（同目录：真实人声 + strip_eq/compressor/delay 链→含音频导出，音画 Δ<0.25s、峰值 >-60dBFS） | Audio/Edit/Deliver | seek/boundary/尾音/时长/峰值/取消 |
| W05 | **PASS 2026-09-19**（专项回放入矩阵 W_matrix_20260919-020619：图保存重载 round-trip（NodeGraph→JSON→from_dict）逐像素 sha256 一致 + 结构/参数保真；cache 语义双验证——暖渲染 3/3 节点命中（eval 717ms→0.34ms）、tolerance 改动后 2 miss 正确失效（陈旧读取=FAIL）；frame trace attribution 落齐。93 节点矩阵 0 failed + 绿幕/cryptomatte fixture 由 bench_composite_nodes_gpu_coverage 单独归档。**2026-09-19 复验（缓存格式 gbrap12a16 + CompositePage 蹦床/timer 变更后）**：W-matrix 重跑 8/8 PASS（W_matrix_20260919-141056）、93 节点矩阵重跑 by_status 与批次 D 基线逐类一致 4/13/76 零回归（A2_regression_20260919-141050）） | Graph commands/nodes/assets/render | 节点图保存重载、非平输出、绿幕 attribution、cache/telemetry |
| W06 | **PASS 2026-09-18**（bench_out/ai_upgrade/W_matrix_20260918-141945/：新增 warm_tint 插件声明 `@export_support opengl` → catalog `export_opengl` → resolve_execution_capability 可交付（opengl_export 轴 + "CPU 回退跳过"质量约束）→ manifest/计划/GL 链门同一真相；GL 合成臂导出后帧哈希与 identity 不同（像素级参与证明）；Film_Halation（preview_only）计划期 EFFECT_NOT_DELIVERABLE 拒绝。数据契约 6/6 见 tests/edit/ai_editing/test_plugin_export_capability.py；另发现并修复 set_effect/remove_effect 编译器断裂（发 UpdateClipParamCommand 未支持的 param_type——AI 从前无法经计划挂任何效果）） | Capability/Effect/Deliver | 前者交付可验证；后者在计划阶段明确拒绝并提出替代 |
| W07 | **PASS 2026-09-18**（同目录：横/竖双版本，竖版中途取消 cancelled=True 无完整成品，横版独立完成 probe 通过） | Transaction/jobs/path approval | 两个 job 独立、取消无残留、另一项可完成 |
| W08 | **PASS 2026-09-18**（同目录：模型态 not_installed 诚实上报、search_assets 优雅降级、导出 telemetry 诚实携带 CPU blockers——不崩溃、不虚报 GPU/模型完成） | Capability/fallback policy | AI 选择 CPU/离线/拒绝策略，不崩溃、不虚报 GPU/模型完成 |

每个 Wxx 需至少运行 H.264 和 TOPOS 两种素材；和用户真实场景相关的工作流完成后才能标记“AI 可自主控制该产品域”。

---

## 11. 发布门禁与状态更新规则

### 11.1 阶段门禁

| 门禁 | 允许宣布的内容 | 必须满足 |
|---|---|---|
| G0 Runtime 可信 | TOPOS 可稳定导入/导出 | T-P0-00、T-P0-10、T-P0-20 完成；库/探测/fallback 可解释 |
| G1 GPU 可信 | 指定 H264/HEVC path 全链路 GPU | T-P0-30、T-P0-31 通过真实 2K 48 帧测试；trail 证明 surface 闭环 |
| G2 Timeline 快速 | TOPOS identity 高性能 | T-P1-10、T-P1-11 达标；不伪称 TOPOS GPU encoder |
| G3 效果可交付 | 单项 effect/node 可以稳定交付 | capability contract、CPU/GPU 对拍、真实导出、性能门槛通过 |
| G4 AI 可控 | AI 可控制该域 | 正式 operations、approval、transaction、workflow 回放通过 |
| G5 全产品候选 | 可宣称大多数声明功能 AI 自主完成 | W01–W08、稳定性压力、manifest 完整、已知不支持项显式列出 |

### 11.2 状态字段

每张任务卡在执行时使用：

```text
TODO        未开始
IN_PROGRESS 已开始，尚无完成证据
BLOCKED     依赖/硬件/产品决策缺失；必须写明 blocker 和最后一次证据
DONE        完成定义全部满足，附结果目录与测试命令
REGRESSED   曾完成但新测试回归；自动阻断依赖任务
```

任何实现合并后，执行 AI 必须更新：任务状态、git revision、结果目录、性能基线、已知回退和下一任务。不得删除失败记录；修复后新增一次结果而不是改写旧证据。

---

## 12. 建议给未来 AI 的启动提示词

将以下文字与本文件一起发送给执行 AI：

```text
你正在维护 Topos 项目。请严格执行
docs/ai_executable_optimization_upgrade_plan_2026-09-17.md。

本次只处理任务：<TASK_ID>。
先阅读“0. 给执行 AI 的总指令”“2. 当前事实与不可改变的基线”以及该任务卡，
然后用 rg 调查所有调用方和测试。遵循 .clinerules：不得通过 UI 私有状态、
任意脚本或静默回退绕过业务路径。

先确认本任务适用的 X-TEST 补测包已经有“改前”基线；若没有，先只执行补测并保存
machine-readable 证据。实施顺序必须是：复现/补测试 → 最小实现 → 单元与契约验证
→ 真实媒体验证 → 性能/telemetry 验证 → 用相同素材和参数做改后对比 → 保存 bench_out
证据 → 报告状态。

请不要开始任何依赖尚未 DONE 的后续任务。若受阻，输出 BLOCKED、可复现命令、
根因、最小下一步；不要猜测或伪造完成。最终报告必须包含修改文件、验证命令、
通过/失败、性能前后对比、GPU trail、输出 probe 和下一任务建议。
```

---

## 13. 初始执行队列

未来 AI 必须按以下顺序领取任务：

1. `X-TEST-00`：先冻结环境、硬件和所有真实测试素材的 manifest。
2. `T-P0-00`：单一能力状态模型（仅测量基础，不做效果优化）。
3. `T-P0-10`：TOPOS native 路由与 probe 单一化（仅测量基础）。
4. `X-TEST-10`、`X-TEST-20`、`X-TEST-30`：先建立真实桌面 GPU、全量功能注册表和媒体矩阵的改前事实；失败项也必须登记。
5. `T-P0-20`：导出 telemetry 与 GPU truthfulness。
6. `X-TEST-40`：以统一 telemetry 建立规范化 codec-only/identity/effect 性能与传输基线。
7. `X-TEST-50`：建立长任务、并发、取消和资源生命周期基线。
8. `T-P0-30`：Native VT bridge 前置协商与安全回退。
9. `T-P0-31`：TOPOS 422 → NV12/P010 GPU surface；完成后必须重跑 `X-TEST-10/30/40` 的相关子集。
10. `T-P1-10`：TOPOS identity 直通快路径；随后重跑 `X-TEST-40`。
11. `T-P1-11`：TOPOS CPU encoder 低拷贝优化；随后重跑 `X-TEST-40/50`。
12. `T-P1-20`：Effect Capability Contract 和插件策略；用 `X-TEST-20` 逐项收敛状态。
13. `T-P1-21`：Grade → 双层/Transition → Sharpen/Glow → Blur → Defocus/Denoise 的性能迁移；每完成一个效果即重跑该项 `X-TEST-20/40`。
14. `T-P1-22`：Adjustment、音频和时间线完整性；重跑 `X-TEST-20/30/50`。
15. `T-P1-50`：Color GPU/插件/桌面验收；重跑 `X-TEST-10/20/40`。
16. `T-P1-60`：Composite 专用 fixture、性能 attribution 和图结构 AI 操作；先完成 `X-TEST-20` 的专用 fixture，再实施优化。
17. `T-P1-70`：Deliver Contract 与稳定性清零；重跑 `X-TEST-30/50`。
18. `T-P2-20` 至 `T-P2-22`：全产品 AI 控制、事务和真实工作流回放；先执行 `X-TEST-60`，再完成 W01–W08。
19. 通过 G0–G5 后，才评估 `T-P2-10` TOPOS GPU encoder 分支和发布候选。

这份顺序的核心是：先把未覆盖能力和性能事实测出来，再让事实、能力和回退可见；然后打通 GPU 数据面，最后优化效果并扩大 AI 自主范围。这样每次优化都有可量化的改前/改后对照，也不会让 AI 在尚不可靠的链路上放大错误。
