# 计划 04：文档体系化（Documentation System）

分支：`plan/04-docs` ｜ 性质：写文档 + src 内仅 docstring/注解级改动 ｜ 写路径：`docs/`（其他计划领地除外）、`src/**`（仅 docstring 与类型注解）、`README.md`、`README_EN.md`

## 1. 目标

仓库现有 72 个 docs 根目录文件 + 若干子目录，信息散、缺索引、缺按读者组织的入口。本计划建立可持续的文档信息架构，为四大 feature 产出架构文档，为 core 模块补 docstring，重构 README 双语版本，并完成 i18n 审计。

## 2. 仓库关键事实（已核实）

- 文档现状：`docs/` 根目录 72 个文件（大量 `*_plan_*.md`、`*_report*`），子目录 `adr/ ai/ benchmarks/ codec/ image/ plans/ superpowers/ video/`；另有 `docs/PLUGIN_DEV_GUIDE.md`（658 行，归计划 03 管追加）。
- 代码分层：`src/products`（topos_color/edit/composite/studio 四产品入口）→ `src/features`（color/edit/composite/deliver/scripting/ai_harness/ai_workflow/teaching_animation/shared/models）→ `src/shared`（40+ 子模块）→ `src/app`（main_window/controllers/feature_registry/module_loader）。
- 核心链路（架构文档主线）：`image_engine.py → async_renderer.py → shader_compiler.py → opengl_render_backend.py`（均在 `src/features/color/core/`）。
- i18n：`src/shared/i18n/i18n.py` + `src/shared/i18n/locales/`；插件命名空间 `plugins.*` 归计划 03，本计划**不碰**。
- `.clinerules` 要求：结构性变更同步架构文档；新函数有完整类型注解。

## 3. 阶段划分

### P0 盘点与信息架构（先行，半天）
1. 全量盘点 `docs/` 现有文件：逐文件读首部 + 判断类型（计划/报告/指南/ADR/参考），标注"现行有效 / 已完成可归档候选 / 相互重复"。
2. 产出 `docs/INDEX.md`：按读者分类的入口（新用户 / 插件开发者 / 贡献者 / 运维打包），并明确"计划与报告类文档以 plans/ 与各子目录为准"的规则。
3. 产出信息架构方案 `docs/2026-09-28-docs-info-architecture.md`（放 docs 根，作为本计划的架构决策记录），定下目录职责表——后续阶段按此执行。

### P1 架构文档（fan-out 主体之一）
为以下模块各写一篇架构文档，输出到 `docs/architecture/`：
- `color-rendering.md`：image_engine → async_renderer → shader_compiler → opengl_render_backend 的数据流、调用链、线程边界（渲染线程/任务队列/deferred-release）、HDR 内部格式约定。
- `edit-timeline.md`：edit/core 的命令系统、时间线模型、音频图渲染。
- `composite.md`：节点系统（nodes/、ParamDef）、Effect Catalog 聚合（effect_catalog 的三个来源）、与 Edit 页共享效果的方式。
- `deliver-export.md`：导出管线、codec 层、deliver feature。
- `shared-foundations.md`：project 包/序列化与版本迁移、effects registry、i18n、config/preferences、db。
- `app-products.md`：四产品差异（products/）、feature_registry、module_loader、UI 集成边界。

每篇要求：模块职责与接口、数据流图（mermaid 或 ASCII）、关键函数调用链、线程安全边界、"改动注意事项"清单（呼应 .clinerules）。**信息必须来自实际读源码，不得凭猜**——每篇末尾列"本篇依据的源文件"。

### P2 core 模块 docstring 补全（fan-out 主体之二，token 大头）
- 范围第一批（P0 优先级）：`src/features/color/core/` 全部 28 文件；第二批：`src/shared/render/`、`src/shared/gpu/`、`src/shared/effects/`；第三批：`src/app/controllers/`。
- 子智能体规则（每批 5–10 文件）：
  - **只添加/完善 docstring 与缺失的类型注解；不改任何可执行语句、不改函数名/参数名/默认值、不重排 import**；
  - 模块级 docstring 说明职责与线程边界；公开函数补参数/返回/raises；沿用文件内已有 docstring 风格（中文或英文跟随原文件）；
  - 每批完成后跑 `python -m compileall <批次文件>` 与相关目录的 pytest 冒烟（如 `pytest tests/color -m "color_core" -q`）确认零行为影响。
- 这是跨大量文件的机械性改动：**整批一个 commit**，便于人工抽查与回滚。

### P3 README 重构与双语同步
- `README.md`（48KB）瘦身：保留产品简介 + 快速开始 + 截图位 + 指向 `docs/INDEX.md` 的导航；细节内容迁移到 `docs/getstarted/`（安装、构建、打包、常见问题）。
- 重构后同步重写 `README_EN.md`，保证两版信息等价（逐节对照表写在 `docs/getstarted/bilingual-map.md`）。

### P4 i18n 审计与补齐
- 扫描 `src/ui`、`src/app/ui`、`src/shared/ui`、各 feature 的 UI 层，找出硬编码用户可见字符串（不经过 `I18N.tr` 的），产出 `docs/i18n-audit-2026-09-28.md`（按模块统计 + top 问题清单）。**只出报告不改 UI 代码**（改 UI 代码不在本计划写路径内）。
- 检查 `locales/` 各语言文件的**键完整性差异**（除 `plugins.*` 命名空间），补齐缺失翻译键（纯追加）。

### P5 收尾
- `docs/2026-09-28-docs-final-report.md`：完成度对照表、归档候选清单（注意：只列候选，**本计划不移动/删除任何既有 docs 文件**，归档动作留给人工决定）。
- 更新 `docs/INDEX.md` 收录本计划所有新文档。
- **收口**：把 i18n 审计发现的硬编码字符串、各架构文档"改动注意事项"中记录的风险、docstring 批次中发现但按红线未处理的待办，结构化为 `FINDINGS.json`（id 前缀 `DOC-`，type 取 doc-gap/i18n-gap/risk；schema 见总纲第七节），条目数与各来源对账一致。

## 4. 边界与禁止

| 允许写 | 禁止写 |
|--------|--------|
| `docs/architecture/`、`docs/getstarted/`、`docs/INDEX.md`、docs 根**新增**文件 | `docs/audit/`、`docs/testcoverage/`、`docs/plugins/`、`docs/benchreports/`（其他计划领地） |
| `docs/` 既有文件中**仅** P4 的 locales 键补齐；其余既有 docs 文件一律不改不删 | `docs/PLUGIN_DEV_GUIDE.md`（03 计划领地） |
| `src/**` docstring 与缺失类型注解 | `src/**` 任何可执行语句的修改 |
| `README.md`、`README_EN.md` | `plugins/`、`tests/`、`bench_out/` |

- docstring 批次的验收标准是"零行为变更"：任何需要改动逻辑才能写的文档，改为记入 `HANDOFF.md`。
- 发现文档与实现不一致：文档以实现为准（记录差异），不反向"改文档凑实现"以外的代码。

## 5. 预估 token 消耗

架构文档 6 篇（每篇需深读一个子系统）+ docstring 覆盖 ~100 文件 + README 双语 + i18n 扫描，一轮约 **8000 万 – 2 亿 token**，是五计划中消耗最大的之一；第 2 轮可扩展到 features 其余模块 docstring。

## 6. 启动提示词（在本机器的 ZCode 新会话中粘贴）

```text
你是计划 04（文档体系化）的执行者。请先完整阅读以下两个文件并严格遵守：
1. docs/plans/2026-09-28-multi-machine-program/README.md（全局铁律，特别是：写路径白名单）
2. docs/plans/2026-09-28-multi-machine-program/plan-04-documentation-system.md（本计划全文）

然后从阶段 P0 开始执行。要求：
- 确认当前分支是 plan/04-docs，不是就先创建并切换。
- 架构文档的信息必须来自实际读源码，每篇附"依据的源文件"清单。
- docstring 批次严格执行"零行为变更"红线：只加 docstring/类型注解，每批 compileall + 冒烟测试后整批 commit（前缀 [plan-04]）。
- 不改既有 docs 文件（除计划明确允许的 locales 键补齐）；不碰 plugins.* i18n 命名空间与 PLUGIN_DEV_GUIDE.md。
- 每完成一个阶段更新 PROGRESS.md。
```
