# MotionGraphicClip 后续优化与发布收口计划

版本：v1.0
日期：2026-08-13
适用范围：`src/features/teaching_animation/`、Edit 时间线、Composite、共享 Effect Catalog、AI Assistant 及发布验证链路

## 1. 计划定位

原始 MotionGraphicClip 优化计划的 W0～W6 核心实现已经完成。本计划不再重复建设已经落地的 schema、编辑器、HTML 主链、素材/音频/合成和基础专业能力，而是处理以下三类剩余工作：

1. 把“代码已完成”推进到“全量回归、真实后端和发布门均可信”；
2. 用固定样本测量性能，再决定是否投入 retained GPU scene renderer、增量失效和更深缓存；
3. 把 W6 中已经声明但仍是最小闭环的表达式、粒子和 HDR 能力接入完整运行时链路。

核心原则：

- 在全量回归不稳定、GPU/Chromium 真实门未通过前，不继续堆叠新的高成本视觉功能；
- 先测量再优化，任何性能目标必须绑定机器、renderer build、字体集、素材 hash、DPR、profile 和样本场景；
- SVG fallback、Composite GPU 精确路径和真正 HDR 输出必须分别声明，不把降级结果标为等价结果；
- 每个重要批次独立提交，提交前必须有 focused tests、静态检查和明确的阻塞记录。

## 2. 当前基线与事实

### 2.1 已完成能力

W0～W6 的核心实现已经保存，主要提交包括：

| 能力 | 代表提交 |
|---|---|
| AI HTML 生产派发、异步预览、编辑器批量能力、组件与响应式变体 | `07b89136`、`41699089`、`5b95dc50`、`08b07d99` |
| 声明式效果、SVG fallback、Composite shared Effect Catalog bridge | `8bd79f46`、`4dbfea72` |
| AudioRenderPlan、LocalAssetProvider、precomp/mask/composition | `82fdb895`、`bfefac77`、`5e35aed4` |
| 表达式、motion blur、粒子、色彩/HDR 合同与专业能力命令 | `af9ac39f` |
| 原始计划状态收口 | `dbf4387d` |

计划相关跨层 focused tests 已通过 292 项。当前仓库工作区应保持干净，后续变更应从新的批次提交开始。

### 2.2 不能误判为已完成的部分

以下项目仍然是发布前任务或后续运行时任务：

- 本机 Python 3.10 缺少 `tomllib`，不能作为完整验证环境；
- 全量 pytest 曾在 macOS 音频 waveform 生命周期测试处触发 Bus error，并且存在与本计划无关的既有 UI/3D 测试失败；
- 真实 Chromium gate、ModernGL/GPU 压力、显存回收和长时间取消/重启仍需目标 CI/硬件执行；
- `evaluate_expression()` 已有安全纯函数实现，但尚未完整接入 MotionGraphic scene evaluator、命令和缓存语义；
- 粒子当前有确定性 SVG 简化路径，GPU 精确粒子系统尚未在 Teaching 层产品化；
- `SceneColorPipeline` 已有声明、RenderPlan 和 fallback 证据，但 Teaching SVG 仍输出 8-bit；真正 HDR 需完成 Composite float16/float32 → tone map → Export 闭环；
- 现阶段没有足够固定的 S0～S3 性能基线，不能直接承诺 1080p/4K 的交互和导出 SLO。

## 3. 总体优先级

| 优先级 | 方向 | 目标 | 是否阻断后续新功能 |
|---|---|---|---|
| P0 | 验证环境与全量回归稳定性 | 让失败可复现、可分类、可归因 | 是 |
| P0 | 真实 HTML/GPU/资源生命周期发布门 | 证明后端不是“只在纯逻辑测试中通过” | 是 |
| P1 | 性能基线、缓存与局部失效 | 找到 SVG/浏览器/Composite 的真实瓶颈 | 强烈建议先做 |
| P1 | preview/export/Composite 端到端证据 | 证明场景在宿主链路中保持时间、alpha、色彩和资源语义 | 否，但阻断发布 |
| P2 | 表达式进入 evaluator | 让 W6 表达式成为真实可用的动画能力 | 否 |
| P2 | HDR float pipeline | 面向专业交付完成真实 HDR | 仅在 HDR 产品需求明确时执行 |
| P2 | GPU 粒子精确路径 | 提高粒子表现力和规模 | 仅在性能/创意样本证明值得时执行 |
| P3 | 新的 3D、复杂混合和更多模板 | 扩大能力面 | 暂缓 |

## 4. 分批实施计划

### Batch 0：统一验证环境与全量回归修复（P0）

目标：建立可信的“绿灯/红灯”基础，不让环境问题掩盖代码回归。

实施任务：

1. 统一 CI 和本地验证解释器到 Python 3.11 或更高版本，固定 pytest、PyQt、Playwright、ModernGL、FFmpeg 版本；
2. 将测试拆为 `unit_core`、`teaching_render`、`html_renderer`、`gpu_renderer`、`ui_qt`、`full_regression` 六类 job；
3. 修复或隔离音频 waveform 生命周期测试的 macOS Bus error，确认是否涉及 PyQt/线程/原生音频库；
4. 把既有 UI/3D 失败按“本次回归 / 基线失败 / 环境失败”分类，不允许只用整体失败数字；
5. 为每个后端缺失环境输出 `skipped with reason`，禁止把 skip 静默当作 pass；
6. 保存一次干净环境的全量测试摘要，作为后续批次的回归基线。

代码边界：

- `.github/workflows/`、`pyproject.toml`、测试配置、必要的测试隔离和生命周期修复；
- 不改变 MotionGraphic scene schema，不借机重构 UI 或 renderer。

验收标准：

- `python -m pytest tests -q` 能完成收集和执行；
- Bus error 不再发生，或者已被最小复现、明确标注为独立环境阻塞；
- `html_renderer` 和 `gpu_renderer` 缺环境时显示原因，具备环境时不允许 skip；
- 所有新增失败都有测试路径、责任模块和下一步处理结论。

提交建议：

`chore(test): stabilize motion graphic validation matrix`

### Batch 1：真实后端与资源生命周期发布门（P0）

目标：证明 HTML、GPU、Composite 和导出服务在真实后端中可以重复运行、取消、重启并释放资源。

实施任务：

1. 执行固定 Chromium build 的 S0 HTML fixture：乱序帧、重复帧、2×DPR、字体、素材、CSP、无网络、取消和 worker 重启；
2. 执行 ModernGL/GPU fixture：blur、motion blur、glow、透明 alpha、链式 effect、GPU 不可用 fallback、资源释放；
3. 对 `MotionGraphicNode` 执行 MG → Composite → Export 的跨层抽样；
4. 对音频、LocalAssetProvider、precomp、mask 和导出 manifest 做缺失素材、错误路径、seek 越界和项目重开测试；
5. 运行至少 100 次 render/cancel/restart 循环，记录旧帧、线程、page、texture、FBO 和 scratch texture 是否残留；
6. 在一台无开发缓存的干净机器上打开样本工程并导出 S0～S3。

代码边界：

- 优先补测试、diagnostics、manifest 和 CI，不先引入新 renderer；
- 所有 GPU 对象仍由现有 render thread/context 所有；
- HTML browser page/process 仍由 `HtmlRendererService` 所有，UI 不直接持有 WebEngine/Playwright 对象。

验收标准：

- S0～S3 关键帧 preview/export/Composite 的 frame、alpha、color metadata 一致；
- 取消后没有旧帧回调；
- 缺素材、缺字体、不支持 effect 时有明确诊断或 fail-closed；
- 真实后端 job 通过，或阻塞项记录了机器、驱动、版本和可复现步骤。

提交建议：

`test(render): add real backend release gates`

### Batch 2：性能基准与热点定位（P1）

目标：在决定是否建设 retained GPU scene renderer 之前，取得可比较数据。

固定测量维度：

| 维度 | 最低覆盖 |
|---|---|
| 分辨率 | 1920×1080、3840×2160 |
| profile | draft、interactive、preview、final |
| 场景 | 纯 SVG、HTML、文字/图表、mask/precomp、effect chain、Composite bridge |
| 帧位置 | 首帧、中间帧、转场帧、末帧、随机 seek |
| 状态 | cold、warm、修改单元素后、连续 scrub、取消后重试 |
| 指标 | 首帧、P50/P95 帧耗时、导出 fps、内存峰值、缓存命中率、GPU texture 数量 |

实施任务：

1. 增加不依赖墙钟语义的 benchmark harness，墙钟只作为测量指标；
2. 将 renderer build、机器、字体集、asset hash、DPR、profile 和 fixture hash 写入 benchmark manifest；
3. 分别测量 SVG 生成、Qt rasterize、HTML capture、Composite effect pass 和 Export encode；
4. 实现或完善 scene revision、element bbox、effect chain 和 asset hash 的缓存 key；
5. 先落地低风险的 retained SVG/effect/cache 与 dirty range，再根据数据决定是否建设 `ModernGLSceneRenderer`；
6. 对 4K 修改单元素场景验证“无关区域不重算”的收益。

决策门：

- 若 SVG 生成/栅格化占主要耗时：优先 retained scene/cache；
- 若 effect pass 占主要耗时：优先共享 GPU pass、scratch texture 复用和 profile 降级；
- 若 HTML capture 占主要耗时：优先 browser pool、请求合并、页面复用和字体/素材缓存；
- 只有测量证明 SVG 架构成为主要瓶颈时，才建设完整 retained GPU scene renderer。

验收标准：

- 形成首版性能基线和报告；
- 每次优化前后能比较同一 fixture 的 P50/P95、内存和输出 hash；
- 不因缓存优化改变 frame、alpha、color 或 capability evidence；
- 超过预算的场景显示诊断，不静默降低质量。

提交建议：

`perf(teaching-animation): add render benchmark and cache evidence`

### Batch 3：跨层一致性与产品质量收口（P1）

目标：将“功能测试通过”提升到“用户可以稳定交付”。

实施任务：

1. 固定 S0～S3 golden fixture，覆盖中文、英文、横竖屏、安全区、透明背景、字体替换、缺素材和 effect fallback；
2. 记录 SVG、HTML、Composite 和 Export 的 `RenderEvidence`，包括 renderer build、font hash、asset hash、profile、fallback 和 frame hash；
3. 增加 preview/export/Composite 三端的严格 hash 与感知 diff 对照；
4. 对文本溢出、safe area、动画过冲、blank frame、字体替换、alpha 黑边和 HDR fallback 增加质量门；
5. 增加错误卡和状态栏诊断的用户可读验证；
6. 生成一份发布 manifest，锁定 scene/template/html/sanitizer/font/asset/effect/render profile。

验收标准：

- 关键帧无阻断视觉差异；
- 所有降级都有 capability evidence；
- 导出工程在干净机器上可重开、可渲染、可导出；
- 人工验收记录包含机器、浏览器/GPU、字体和版本，而不是只记录“看起来正常”。

提交建议：

`test(teaching-animation): close cross-layer visual acceptance gates`

### Batch 4：将受控表达式接入真实 Motion evaluator（P2）

目标：让已有的安全表达式从独立工具变成可保存、可撤销、可缓存的动画能力。

实施任务：

1. 定义 scene-owned expression schema：表达式文本、版本、目标属性、默认值、seed、markers 和错误策略；
2. 通过 immutable command 写入/修改表达式，支持 undo/redo、schema 校验和 source revision；
3. 在 evaluator 中以 `ExpressionContext(frame, fps, seed, markers)` 求值；表达式失败时使用显式 fallback 并记录诊断；
4. 将表达式源、版本、seed、markers 纳入 render/cache key；
5. 限制复杂度、数值范围、帧耗时和递归/循环风险；
6. 为 preview/export 使用相同求值函数，增加乱序帧、跨进程和边界帧测试。

验收标准：

- 同一输入和 frame 得到同一值；
- 非法表达式不会触碰文件、网络、属性或 Python runtime；
- undo/redo、保存/重开、preview/export 保持一致；
- 表达式超预算或出错时不会产生黑帧，且质量报告可解释。

提交建议：

`feat(teaching-animation): integrate safe expressions into evaluator`

### Batch 5：HDR 与 GPU 粒子产品化（P2，按需求选择）

只有 Batch 0～3 完成且有真实创意/性能需求时执行。

#### HDR 子批次

- 从 `SceneColorPipeline` 读取 working space、bit depth、alpha mode 和 tone map；
- 让 Composite bridge 在 HDR 场景使用 `f2`/float16 或更高精度，而不是固定 `f1`；
- 完成 HDR → SDR preview tone map 与 HDR export metadata 的明确分支；
- 验证高光保留、透明 alpha、颜色空间转换、预览与导出证据；
- 任何只经过 SVG 8-bit 的结果继续报告 `svg_8bit_tonemap`，不标记为 HDR 等价。

#### GPU 粒子子批次

- 先复用 Composite/shared effect 或受控 node 机制，不在 Teaching 中建立第二套 shader registry；
- 明确 emitter 参数、seed、生命周期、边界、混合、资源预算和取消释放；
- SVG fallback 与 GPU 输出分别建立 capability 和 golden tolerance；
- 粒子数量、4K 帧耗时、显存占用超过预算时自动降级或阻断。

提交建议：

- `feat(teaching-animation): complete hdr composite delivery`
- `feat(teaching-animation): add shared gpu particle path`

### Batch 6：持续治理与新能力扩展（P3）

在前述批次稳定后再考虑：

- 更复杂的 blend/matte/displacement/turbulence；
- 3D camera、geometry、light 的受控 precomp；
- 更多模板和创意评测集；
- 持久化 render cache、跨项目素材缓存、远程/分布式导出。

每一项新能力必须先补 schema、capability matrix、fallback、资源预算、迁移、golden、取消/回收和发布门，再进入 UI 或 AI 能力清单。

## 5. 推荐执行顺序

```text
Batch 0 验证环境与全量回归
        ↓
Batch 1 HTML/GPU/资源真实后端发布门
        ↓
Batch 2 性能基准与热点定位
        ↓
Batch 3 跨层一致性与人工视觉验收
        ↓
Batch 4 表达式运行时接入
        ↓
Batch 5 HDR / GPU 粒子（按产品需求）
        ↓
Batch 6 新效果、3D、模板与长期缓存
```

如果只能选择一个下一批，选择 **Batch 0 + Batch 2 的最小切片**：先让全量回归可复现，同时测出 S0～S3 的真实瓶颈。没有这两项数据，直接重写 renderer 或继续增加专业效果的风险最高。

## 6. 每批次完成定义

每个批次必须同时满足：

1. 代码变更有明确边界，不夹带无关重构；
2. 有 focused tests，并运行受影响的跨层回归；
3. 有 `git diff --check` 和类型/语法检查；
4. 失败、skip、外部环境阻塞均记录原因；
5. 重要批次独立提交，提交信息可对应本计划批次；
6. 更新本计划中的状态、证据、剩余风险和下一步；
7. 不把 fallback、占位、skip 或仅元数据声明写成“精确实现”。

## 7. 发布前最终门

只有同时满足以下条件，才能把 MotionGraphicClip 标记为“发布就绪”：

- 全量回归可以完成，且失败均有明确归因；
- HTML renderer gate 在固定 Chromium build 上通过；
- GPU renderer gate 在目标 GPU/驱动上通过，或 capability 明确阻断/降级；
- S0～S3 在 preview、Composite 和 export 中通过关键帧一致性；
- 连续 render/cancel/restart 不产生旧帧、线程、browser page、GL texture 或 FBO 泄漏；
- 4K 性能基线、内存预算和缓存命中率已有实测记录；
- 字体、素材、effect、HTML sanitizer、renderer build、DPR 和 asset hash 都进入发布 manifest；
- HDR、粒子和任何专业能力的实际支持范围与用户界面、AI capability 清单和导出门一致。

## 8. 当前状态记录

截至 2026-08-13，实施记录如下：

- 原始计划 W0～W6 核心代码：已完成；
- Batch 0：已完成第一阶段。`8d948ba1` 增加 Python 3.10 的 `tomli` 兼容、统一 focused/waveform 验证脚本和 CI 矩阵；`72af55da` 修复干净 editable install 的标准 setuptools backend。Python 3.10 focused gate 为 278 passed，waveform 生命周期独立 gate 为 1 passed。
- Batch 0：架构门已完成“可分类”收口。提交 `3f4b17cb` 将两项既有 app/shared 深层 import 违规锁为数量+SHA-256 指纹基线，新增或变化仍失败；`qtpy/PyQt6 AttributeError: pos` 通过 Deliver 可选模块异常隔离后，architecture gate 为 `40 passed / 2 xfailed`。另提交线程生命周期修复（当前未单独计为 MotionGraphic 能力变更）：embedded file-explorer worker 支持 stop/join，并在 `TOPOS_TEST_MODE=1` 不启动；目标 Composite 文件可完成 `51 passed / 1` 既有序列化失败且不再由缩略图线程崩溃。全量 17,755 项长进程仍在 macOS Qt pytest setup、约 15% Composite 阶段退出 139；此时栈已无缩略图线程，剩余为 Qt/WebEngine/Composite 原生累积环境阻塞，waveform 独立 gate 仍通过。
- Batch 1：真实 HTML gate 已在固定 Chromium `playwright-chromium/py1.62.0/chrome-151.0.7922.34` 上通过 19 项，2 项为有明确原因的 skip；真实 Apple M1 ModernGL motion blur gate 与资源释放已通过。
- Batch 1 的 GPU gate、marker 和实测证据已进入当前提交；Linux/CI GPU 驱动矩阵仍待带 GPU 的目标环境执行。
- Batch 2：已完成第一阶段，提交 `9bc036ff` 增加可重复的 `scripts/benchmark_motion_graphic.py`、固定参考场景、环境/fixture/asset hash、P50/P95、导出吞吐、单元素更新和缓存命中报告；`6f1f470c` 为参考场景补充了明确帧动画，避免静态场景掩盖随机 seek 差异。Focused gate 当前为 280 passed。
- Batch 2 当前 SVG 基线（Apple M1，本机，`teaching-animation-svg/0.1`，preview，24 帧，16 元素，3 次迭代）：1080p 首帧 P50/P95 `1.30/1.86 ms`、交互帧 `1.07/1.11 ms`、导出 `1775/1791 FPS`、单元素更新 `1.02/1.05 ms`；4K 为 `1.32/1.89 ms`、`1.05/1.09 ms`、`1753/1792 FPS`、`1.04/1.10 ms`。两档输出 hash 跨迭代稳定，缓存记录每轮 1 次 warm hit；这些数字只代表 SVG 字节生成，不代表 HTML capture、Qt rasterize 或 GPU pass。
- Batch 2：提交 `3367407b` 增加同一 JSON 证据中的可选分段测量：Qt rasterize、HTML capture、Composite GPU effect，并为缺失后端保存明确 `skipped` 原因。提交 `9bf7f033` 补充 PyAV export encode 分段。320×180、2 帧、1 次迭代的本机真实 smoke：Qt `341.6/0.67 ms`（首轮字体初始化明显）、HTML `277.0/131.6 ms`、GPU motion-blur `392.7 ms`（首轮 shader 初始化）、PyAV MPEG-4 encode `29.8 ms`；输出 hash 稳定。该 smoke 不替代 1080p/4K 后端矩阵，也不把初始化成本误当 warm throughput。
- Batch 2：提交 `6c73b6b2` 增加缓存半开帧区间失效。测试证明只修改脏区间 `[1,3)` 时，frame 0/3 保持命中，frame 1/2 重新生成；invalidations 进入 cache stats。该实现仍是整帧缓存，bbox 的空间裁剪留给后续 render plan，不会把“帧级局部失效”误称为 tile-level retained renderer。
- Batch 2：4K、12 帧、2 次迭代的 profile 矩阵已完成：draft 首帧/交互/导出 P50 `1.55 ms / 1.07 ms / 1774 FPS`，interactive `1.47 / 1.08 / 1690`，preview `2.65 / 1.24 / 1780`，final `2.63 / 1.51 / 1201`。测量波动和 effect/profile 分支仍需更多迭代，暂不把这些结果冻结成预算。`128b0425` 为报告加入进程 RSS 起始/峰值；同规格 preview 当前约 `29.2 MiB → 29.7 MiB`，cache `2 hits / 52 misses / 0 invalidations`。
- Batch 2：补充真实 ModernGL 上下文的 texture/FBO 数量快照与 64 次 HDR effect soak：scratch dtype 为 `f2`，texture/FBO 各稳定为 3，cleanup 后归零。当前仍未宣称性能 SLO 已冻结；4K 长时间 profile 与 GPU 驱动矩阵仍待目标机。
- Batch 3：已完成第一阶段，提交 `c915d584` 增加发布 manifest 与跨序列严格一致性工具。固定 SVG fixture 在 64×48 的 preview/final 三个关键帧（0、中、末）均为 strict pass，manifest 锁定 scene/cache hash、renderer build、DPR、alpha、色彩空间、bit depth、字体/素材 hash、fallback 和逐帧 RenderEvidence；focused gate 当前 284 passed。
- Batch 3：提交 `06b694ed` 将真实 HTML capture 与 GPU probe 接入 manifest 脚本。Chromium 三帧重复 capture strict pass，实际 build 为 `playwright-chromium/py1.62.0/chrome-151.0.7922.34`，font-set hash、DPR、alpha 和 frame hash 均已记录；GPU motion-blur probe 通过并完成 bridge cleanup。当前 Composite 仍是资源/效果 probe，不是完整跨层像素 golden。
- Batch 3：固定 Chromium sustained-soak 已复跑通过：`tests/render/test_html_s0_determinism.py` 的 5 个 HTML 后端测试、含 30 帧完整 worker→adapter→browser→cache 路径，`5 passed in 11.12s`；重复 pass 命中缓存、动画帧 hash 不全相同、无 blank frame。现有 focused 也覆盖中文 locale、透明 alpha、缺素材降级和 effect fallback 的逻辑门，组合 fixture 已由后续提交锁定。
- Batch 3：提交 `5928a6b4` 增加固定 S0～S3 组合 fixture 与脚本：英文、中文、透明背景、缺素材+effect fallback 各三帧双次严格字节 golden；manifest 记录 alpha、asset、fallback、frame hash。提交 `4b4d2ccd` 补充 reopen gate，将四个 fixture 序列化为 JSON、在无共享 scene/cache 状态下重开，scene hash 前后一致且严格渲染通过；当前 focused gate 为 309 passed。目标后端更长取消/重启矩阵仍以 SVG 100 次 soak、HTML 30 帧 sustained soak 为当前证据。
- Batch 4：提交 `d3b882a9` 完成表达式进入 scene evaluator 的第一阶段：新增 scene-owned `SceneExpression` schema/round-trip、validator、`SetSceneExpressionsCommand` undo/redo；evaluator 使用受控 `ExpressionContext(frame, fps, seed, markers)`，错误回退到显式 default 并把诊断写入 SVG frame metadata；cache key 随 scene JSON 自动包含表达式源/版本/seed。287 项 focused passed。
- Batch 4：提交 `ca33fba0` 增加数值范围预算（绝对值上限 `1e9`）和 V2 preview/final strict frame hash 一致性测试；当前表达式受 AST 节点数、源长度、有限数值和安全 allowlist 约束。
- Batch 4：提交 `3ca7a01b` 增加跨进程乱序帧 SVG golden、表达式单帧耗时/超预算统计，并在 advanced AI capability registry 中注册 `expressions`：明确 SVG/Composite 求值范围、HTML layer 暂不支持、sandbox/determinism/budget 质量门。随后 GPU gate 增加真实 MotionGraphicNode 两帧表达式语义验证（共享 SVG evaluator→Composite source node）并通过；相关表达式测试与 Apple M1 GPU gate 通过，Linux/CI 目标机仍待执行。
- Batch 5：提交 `b640465b` 完成 HDR GPU effect precision 选择器：`SceneColorPipeline` 的 8-bit→`f1`、HDR 10/16-bit→`f2`、32-bit→`f4` 进入 Composite bridge 的显式参数；SVG 仍保留 `svg_8bit_tonemap` 证据，不冒充 HDR 等价输出。291 项 focused passed。
- Batch 5：提交 `0a2331c2` 修正 MotionGraphicNode 将 scene `SceneColorPipeline` 传入 Composite bridge，使 HDR 10/16-bit 选择 `f2`、32-bit 选择 `f4`；导出 `_context` 与 manifest 现在携带 working space、bit depth、tone map、alpha，并明确当前所有 sequence/FFmpeg writer 仍消费 RGBA8，状态为 `explicit_8bit_fallback / svg_8bit_tonemap`。提交 `e618ff84` 增加真实 HDR scratch texture/FBO 64 次 soak；提交 `2f45e1e1` 增加真实 Composite expression frame gate。该批次完成“证据诚实+精度参数不丢失+资源回收”子批次，但不宣称真正 HDR 文件交付。GPU 粒子精确路径仍按真实创意/性能需求决定。
- 发布治理补充：提交 `9e5375ad` 为 V2 RenderEvidence 传递 SVG effect fallback，并加入 100 次 SVG cancel→restart soak；每轮 progress job 单一、重启帧正确，focused gate 当前 293 passed。
- 浏览器级页面转场（2026-08-18，提交 `79e82ede`）：为 HTML 页面时序增加 `cut`、`crossfade`、slide、zoom、wipe 转场及可调时长；编辑器浏览器预览、HTML 导出运行时和逐帧 Chromium 捕获共用同一转场状态。Motion Graphic Clip 默认使用 0.45 秒交叉淡化，用户仍可在 HTML Inspector 中改为硬切或其他浏览器转场。新增真实 Chromium 边界帧测试验证上一页、混合帧和下一页；本批 focused/UI/HTML 回归 `44 passed`，完整 HTML S0 `14 passed`。
- 最终复核：architecture gate 为 40 passed / 2 xfailed；Batch 0 的 macOS 全量 pytest 仍在 Qt/WebEngine/Composite 原生累积处退出 139，不标记为“完美完成”。
- 下一行动：当前本机可安全继续的任务已收口；发布前仍需在 CI/目标硬件执行 Linux/驱动矩阵、人工视觉验收和分段全量回归。若产品明确需要 HDR 文件交付，再实现带目标容器/编码器的 float16/float32 writer；当前 manifest 已明确阻断，不把 8-bit 结果冒充 HDR。

### 8.1 当前最终验证摘要

- focused：`309 passed`；architecture：`40 passed / 2 xfailed`（2 项固定基线指纹）；waveform：`1 passed`。
- HTML：固定 Chromium `playwright-chromium/py1.62.0/chrome-151.0.7922.34`，`19 passed / 2 explicit skipped`。
- GPU：Apple M1 standalone ModernGL，`3 passed`，包括 motion blur 真实 shader、HDR `f2` texture/FBO 64 次 soak、Composite MotionGraphicNode expression 两帧语义。
- full：macOS Python 3.10 共收集 `17755 items / 1 skipped`，在约 15% Composite 阶段因 Qt/WebEngine/原生并发累积退出 `139`；缩略图 worker 已停止协议化且不再是崩溃栈线程，仍需目标 CI/新 Python/分进程矩阵继续归因。另有若干既有普通断言失败，不能汇总成 MotionGraphicClip 新回归。
