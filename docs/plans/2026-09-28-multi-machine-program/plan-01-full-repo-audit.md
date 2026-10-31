# 计划 01：全库深度审查（Full-Repo Audit）

分支：`plan/01-audit` ｜ 性质：**全程只读分析，不改任何源代码** ｜ 写路径：仅 `docs/audit/2026-09-28/`

## 1. 目标

对 `src/` 下约 1483 个 Python 文件、83 万行代码做分优先级的深度静态审查，按 `.clinerules` 与 AGENTS.md 的 Review 标准找出真实缺陷与风险，产出可执行的问题台账。本计划**不修任何代码**——修复由后续轮次基于本台账进行。

## 2. 仓库关键事实（已核实，直接使用）

- 分层：`src/products → src/features → src/shared → src/app`。
- 重点目录与规模：
  - `src/features/color/core/`（28 个文件）：`async_renderer.py`、`shader_compiler.py`、`image_engine.py`、`opengl_render_backend.py`、`cache_manager.py`、`color_manager.py`、`denoise_engine.py`、`video_denoise_gl.py`、`msl_composition.py`、`plugin_manager.py` 等 —— **渲染管线心脏，最高优先级**。
  - `src/features/edit/core/`（大量文件）：音频图、混合模式、字幕、命令系统、时间线等。
  - `src/shared/`：`render/`、`gpu/`、`effects/`、`export/`、`codec/`、`project/`、`node_canvas/`、`video/`、`media/`、`db/`、`i18n/` 等 40+ 子模块。
  - `src/app/`：`main_window.py`、`controllers/`、`module_loader.py`、`feature_registry.py` 等 UI 集成层。
  - `src/features/composite/`、`deliver/`、`shared/effects/`（Effect Catalog：`effect_catalog.py`、`effect_spec.py`、`plugin_identity.py` 等）。
- 技术栈：PyQt6 + ModernGL，HDR 内部处理（float16/32），有渲染线程 + 任务队列 + deferred-release 资源释放机制。

## 3. 审查维度（每个文件逐条过）

1. **线程安全**：UI 线程是否直接调 OpenGL/ModernGL；共享状态无锁访问；跨线程信号参数是否为可变对象。
2. **GPU 资源生命周期**：texture/buffer/FBO/program 是否在取消、失败、窗口关闭路径也走 deferred-garbage 释放；有无重复创建不释放。
3. **GLSL/HDR 正确性**（审查 `shader_compiler.py`、`shader_nodes*.py`、`blend_shader.py` 及一切内嵌 GLSL 字符串的文件）：浮点字面量缺 `.0`；循环边界非常量；动态注入变量未命名隔离；对 HDR 内部数据无理由 `clamp(0.0, 1.0)`；采样 UV 越界。
4. **信号防递归**：程序化更新控件是否有状态锁；params_changed 类信号有无成环风险。
5. **状态提交一致性**：是否绕过 command/service 直接改 model 或序列化数据；undo/redo、revision 是否同步。
6. **异常与取消路径**：宽泛 `except` 吞掉领域错误；失败路径资源泄漏；任务取消后状态残留。
7. **输入验证**：外部/可选数据（工程文件、媒体元数据、插件元数据）是否安全读取。
8. **性能模式**：UI 同步解码/渲染、重复探测、无效缓存失效（只记录明显的，不做猜测性优化建议）。
9. **类型注解缺失**（仅记录，Suggestion 级）。

## 4. 执行机制（fan-out）

- 由主会话维护**文件清单 → 批次（batch）**的映射：每批 5–10 个相关文件（按目录/主题聚类，不跨目录混批），派一个子智能体执行第 3 节审查，并行度 4–8。
- 每个子智能体的产出是**标准化的 Markdown 报告**（模板见第 6 节），写入 `docs/audit/2026-09-28/batches/batch-NN-<topic>.md`。
- 主会话只做：批次调度、报告格式校验、Critical 项抽查复核（另派一个复核子智能体只读该问题涉及的文件与行号，确认证据成立）。

## 5. 阶段划分

| 阶段 | 内容 | 交付物 | 验收标准 |
|------|------|--------|---------|
| P0 | 建立 `docs/audit/2026-09-28/` 结构；用 `find`/`cloc` 生成全量文件清单与行数表，按第 2 节优先级排批次 | `INVENTORY.md`（全清单+批次表）、`PROGRESS.md` | 批次表覆盖 src/ 全部 .py 文件 |
| P1 | **P0 优先级**：color/core 全部 28 文件 + shared/render + shared/gpu + shared/effects | batches 报告 + 台账初稿 | 每文件一份报告；Critical 全部经复核 |
| P2 | 汇总 P1 发现，产出台账 v1 | `ISSUES.md`（AUD-0001 起编号）、`SUMMARY.md` | 每条问题有 file:line、证据、置信度 |
| P3 | **P1 优先级**：edit/core、app/（main_window + controllers）、composite、deliver | 同上 | 同上 |
| P4 | **P2 优先级**：shared 其余（export/codec/project/node_canvas/video/media/db） | 同上 | 同上 |
| P5 | 抽查复审：从全部 Critical/Suggestion 中随机抽 10% 复核，修正台账；写 `FINAL_REPORT.md` | 终版台账 + 执行摘要 | 复核错误率 < 10% |
| P6 | **收口**：把 `ISSUES.md` 全量条目对账后结构化为 `FINDINGS.json`（id 前缀 `AUD-`，schema 见总纲第七节；`type` 按审查维度取 bug/risk/test-gap/doc-gap 等） | `FINDINGS.json` | 条目数与 `ISSUES.md` 一致，schema 校验通过 |

优先级定义：P0 = 渲染正确性/崩溃/数据丢失风险直接的代码；P1 = 用户可感知功能路径；P2 = 其余。

## 6. 单文件报告与台账条目模板

```markdown
<!-- batches/batch-NN-<topic>.md 内每个文件一节 -->
## <相对路径>  (行数: NNN)
### 发现
- [AUD-XXXX] [Critical|Major|Suggestion] <一句话标题>
  - 位置: <file>:<line>
  - 证据: <贴出关键代码 3–8 行>
  - 影响: <具体后果>
  - 建议: <修复方向，不写完整补丁>
  - 置信度: 高/中/低（低置信度必须说明不确定性来源）
### 通过项
- <该文件检查过且无问题的维度，一行一个>
```

`ISSUES.md` 按编号正序排列，每条一行摘要 + 链接到批次报告锚点；头部放统计表（按严重度 × 目录的矩阵）。

## 7. 边界与禁止事项

- **只读**：`src/`、`tests/`、`docs/`（除本计划输出目录外一律不写）。
- 不运行会产生写入的命令（不跑格式化、不跑会改文件的脚本）；允许只读命令：`grep`/`find`/`python -c "import ast; ..."` 级别的解析。
- 发现"测试缺失""文档缺失"类问题：一句话记录到台账即可，展开是 02/04 计划的领地，写入 `HANDOFF.md` 提示对应计划。
- `PROGRESS.md` 每完成一批更新一次；中断恢复 = 重读 `PROGRESS.md` + `INVENTORY.md` 跳过已完成批次。

## 8. 预估 token 消耗

83 万行 × 审查深度系数，P0+P1+P2 全范围一轮约 **5000 万 – 1.5 亿 token**（子智能体并行 fan-out 是大头）。若只需第一轮快速版，可只做 P0+P1 阶段（约 1/3 消耗）。

## 9. 启动提示词（在本机器的 ZCode 新会话中粘贴）

```text
你是计划 01（全库深度审查）的执行者。请先完整阅读以下两个文件并严格遵守：
1. docs/plans/2026-09-28-multi-machine-program/README.md（全局铁律，特别是：只写 docs/audit/2026-09-28/、batch+commit 节奏、fan-out 执行方式）
2. docs/plans/2026-09-28-multi-machine-program/plan-01-full-repo-audit.md（本计划全文）

然后从阶段 P0 开始执行。要求：
- 确认当前分支是 plan/01-audit，不是就先创建并切换。
- 严格按计划第 4 节的 fan-out 机制派并行子智能体做审查，主会话只做调度与抽查复核。
- 全程对 src/ 只读；每完成一个批次就更新 PROGRESS.md 并 git commit（前缀 [plan-01]）。
- 发现其他计划领域的问题只记 HANDOFF.md，绝不跨领域修改。
```
