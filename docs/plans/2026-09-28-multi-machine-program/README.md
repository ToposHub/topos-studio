# 多机并行优化计划总纲（2026-09-28）

本目录包含 5 份互相独立、可分别部署到不同机器并行执行的优化计划。每台机器认领一份计划，在**独立分支**上工作，只写**本计划的专属路径**，互不干扰，最后按顺序合并回主干。

## 一、五份计划一览

| 编号 | 计划 | 分支 | 性质 | 专属写路径 | 主要产出 |
|------|------|------|------|-----------|---------|
| 01 | 全库深度审查 | `plan/01-audit` | **只读分析** | `docs/audit/2026-09-28/` | 分级问题台账（AUD-xxx） |
| 02 | 测试覆盖补齐 | `plan/02-tests` | 写测试 | `tests/**`（仅新增）、`docs/testcoverage/2026-09-28/` | 覆盖率缺口报告 + 新测试 |
| 03 | GLSL 插件库扩充 | `plan/03-plugins` | 写插件 | `plugins/*.tglsl`（仅新增）、`tests/plugins/`、`docs/plugins/2026-09-28/`、`src/shared/i18n/locales/*`（仅新增 `plugins.*` 键） | 一批新 .tglsl 插件 + 文档 |
| 04 | 文档体系化 | `plan/04-docs` | 写文档 | `docs/`（他人路径除外）、`src/**`（仅 docstring/注解）、`README.md`、`README_EN.md` | 架构文档、docstring、README 重构 |
| 05 | 基准深度分析 | `plan/05-bench` | **只读分析** | `docs/benchreports/2026-09-28/`、`bench_out/e5_20260928_*/`（仅新增） | 性能图谱、回归基线、建议台账 |

建议机器特性：

- 01、04、05：CPU 即可，内存 ≥ 16GB，能跑 Python 环境。
- 02：需装好 `.venv` 全部依赖（要实际运行 pytest）；无 GPU 也可以（用 marker 跳过 GPU 测试）。
- 03：最好有 GPU 的机器（可做真实 shader 编译验证），无 GPU 则退化为静态验证。
- 05：如果想做"本机重跑验证"环节，需要 GPU；纯分析 CPU 即可。

## 战役完成记录（2026-09-29）

五个计划全部完成（单分支执行于 dev）：plan-01 全库审查（398 批 / 5,067 发现）、plan-02 测试补齐（1,617 新测试全绿 / 覆盖基线 55%）、plan-03 插件库（12 插件 + 126 测试 + i18n）、plan-04 文档体系（架构文档×6 / docstring×40 文件 / README 重构 / i18n 审计+补齐）、plan-05 基准图谱（70 数据卡 / 7 主题报告 / 20 回归锚点 / 17 建议）。统一优化计划表见本目录 REMEDIATION_PLAN.md（5,135 条，对账一致）。

## 一点五、执行方式变更记录（2026-09-29）

战役实际由单一会话在单机上串行完成，经用户确认改为**单分支执行**：plan-01/02/03 已按合并顺序并入 `dev` 并删除对应分支；plan-04、plan-05 及总纲收口直接在 `dev` 上进行。多机并行方案保留于此供未来分发使用。各计划的写路径白名单与产出物不变。

## 二、全局铁律（每份计划开头都会重申）

1. **一台机器一份计划一个分支。** 每台机器 fresh clone 本仓库，检出自己计划的分支（如 `git checkout -b plan/01-audit`），整个战役期间不 pull、不 merge、不 push（合并由你人工在主机器上统一做）。
2. **只写白名单路径。** 计划文档里列出的写路径是唯一允许产生变更的地方。发现其他计划领域的问题（比如测试计划跑出了 src 的 bug），**只记录到本计划输出目录的 `HANDOFF.md`，绝不跨领域修改**。
3. **公共文件冻结。** 所有机器都不得修改：`.clinerules`、`AGENTS.md`、`pytest.ini`（02 计划如需加依赖，只允许在 `constraints.txt`/`pyproject.toml` 中做纯增量添加，且这是它独有的权力）、CI 配置、`docs/plans/2026-09-28-multi-machine-program/` 本目录。
4. **batch + commit 节奏。** 每完成一个批次（batch）就 `git commit`，message 以 `[plan-0X]` 开头，例如 `[plan-02] tests: color_manager param edges (batch 7)`。
5. **进度可追溯。** 每个计划在自己的输出目录维护 `PROGRESS.md`：已完成批次、当前批次、发现的阻塞项。机器重启/中断后，新会话先读 `PROGRESS.md` 续跑。
6. **执行方式 = fan-out 并行子智能体。** 这是消耗 token 且保质的主要机制：把文件清单切成批次，每批派一个子智能体（子智能体只拿到该批文件路径 + 审查/生成标准），主会话只做调度、汇总、抽查。并行度建议 4–8，不要超过机器承受能力。
7. **质量门槛统一遵循 `.clinerules`**（要点：UI 线程不碰 GPU；浮点显式 `.0`；循环边界编译期常量；资源按 deferred-release 模式释放；新函数完整类型注解；不得用宽泛 `except` 掩盖领域错误；先测量再优化）。
8. **不做范围外的事。** 计划没写的不要顺手做；想做的记到 `PROGRESS.md` 的"后续建议"里。

## 三、输出路径分配（防冲突总表）

| 路径 | 归属 | 其他计划 |
|------|------|---------|
| `docs/audit/2026-09-28/` | 01 | 禁写 |
| `docs/testcoverage/2026-09-28/` | 02 | 禁写 |
| `docs/plugins/2026-09-28/` | 03 | 禁写 |
| `docs/benchreports/2026-09-28/` | 05 | 禁写 |
| `docs/` 其余（architecture/、adr/、INDEX.md、getstarted/ 等） | 04 | 禁写 |
| `docs/PLUGIN_DEV_GUIDE.md` | 03（可追加新示例章节） | 04 禁改 |
| `tests/` 新增文件 | 02、03（03 仅限 `tests/plugins/`） | 其余禁写 |
| `tests/` 既有文件 | **全员禁改** | — |
| `src/**` | 04（仅 docstring/类型注解）+ 03（仅 `src/shared/i18n/locales/` 新增键） | 01、05 只读；02 只读 |
| `plugins/*.tglsl` 新文件 | 03 | 其余禁写 |
| `bench_out/` 新增 `e5_*` 前缀目录 | 05 | 其余禁写；历史目录全员只读 |
| `benchmarks/` | 只读 | — |
| `README.md` / `README_EN.md` | 04 | 其余禁写 |
| `constraints.txt` / `pyproject.toml` | 02（仅增量加 coverage 依赖） | 其余禁写 |

## 四、合并顺序建议

路径两两不相交，理论上任意顺序可合并。风险从小到大建议：

1. **01、05**（纯新增报告目录，零冲突）→ 先合，越早可用。
2. **04**（docs + docstring；docstring 改动跨大量 src 文件，建议单独一个 merge commit 便于回滚）。
3. **02**（新测试合入后立即在主机器全量跑一遍 pytest 确认绿）。
4. **03**（插件 + i18n 键；合入后启动 app 确认插件被扫描发现）。

合并时若任何两个分支意外碰到同一文件，以"路径分配表"判定谁越界，越界改动丢弃并回记到该计划 `HANDOFF.md`。

## 五、token 消耗策略（针对"额度充足"这一前提）

- **单轮五计划并行**预估总消耗在 **1.5–4 亿 token** 量级（取决于批次切分与并行度）。
- 要消化数十亿量级，靠的是**多轮迭代**而不是单轮堆量：
  - 第 2 轮：01 审查第 2 批优先级范围（P1/P2 目录）；02 按第 1 轮覆盖率报告继续补；03 做第二批插件；04 补 features 其余模块 docstring；05 做回归基线第二轮校准。
  - 第 3 轮起：基于 01 台账逐条修复（此时切回正常单线开发流），修复后 02 补回归测试、04 更新架构文档——形成"审查→修复→测试→文档"闭环。
- 每轮结束后把本目录各计划文档更新一轮（或归档到 `docs/plans/archive/`，遵循仓库现有惯例）。

## 六、每台机器的启动检查清单

1. `git clone` 主仓库（或复制工作副本）→ `git checkout -b <计划分支>`。
2. 建虚拟环境并安装依赖（`pip install -r constraints.txt` 或按 `pyproject.toml`）；02 计划额外 `pip install pytest-cov`。
3. 确认 `pytest.ini` 的 marker 约定；无 GPU 的机器统一加参数 `-m "not gpu_renderer and not webengine and not html_renderer and not slow"`。
4. 把本 README + 对应计划文档喂给该机器上的 ZCode，用计划文档末尾的**启动提示词**开跑。
5. 每天看一眼各计划输出目录里的 `PROGRESS.md`。

## 七、收口：统一优化计划表（REMEDIATION_PLAN.md）

五份计划的所有发现（缺陷、风险、不足）最终必须收敛成**一份统一的优化计划表**，保存进项目，作为后续逐项优化的唯一清单。分两步：

### 7.1 各计划在分支上先产出结构化发现（FINDINGS.json）

每个计划的收尾阶段都要在自己的输出目录维护机器可读的 `FINDINGS.json`，统一 schema：

```json
{
  "id": "AUD-0012",
  "source": "plan-01",
  "type": "bug|risk|test-gap|doc-gap|i18n-gap|perf-opportunity|process",
  "severity": "Critical|Major|Suggestion",
  "title": "一句话标题",
  "location": "src/features/color/core/async_renderer.py:123",
  "evidence": "指向本计划输出报告的锚点或文件",
  "recommendation": "修复/优化方向",
  "confidence": "high|medium|low",
  "effort": "S|M|L",
  "round": 1,
  "status": "open"
}
```

约定：

- 编号前缀按计划区分：`AUD-`（01）、`BUG-`（02 的 bug）与 `GAP-`（02 的测试缺口）、`PLUG-`（03）、`DOC-`（04）、`BENCH-`（05）。
- **"不足"与"缺陷"同等收录**：缺测试、缺文档、缺 i18n 键、缺基准脚本都是 `type` 的一种，不是二等公民。
- 01 的 `ISSUES.md`、02 的 `BUGS.md` + `GAP_REPORT.md`、03 的 `HANDOFF.md` 项、04 的 i18n 审计与文档缺口、05 的 `RECOMMENDATIONS.md`，全部结构化进各自的 `FINDINGS.json`，条目数必须与对应 MD 文件一致（收口阶段做一次对账）。

### 7.2 合并回主干后的统一收口（主机器上做一次）

五条分支全部合并后，在主机器开一个会话，把五份 `FINDINGS.json` 合并生成 `docs/plans/2026-09-28-multi-machine-program/REMEDIATION_PLAN.md`（统一优化计划表）：

- **保留原编号**（AUD-/BUG-/GAP-/PLUG-/DOC-/BENCH-），每条链接回源报告锚点，可随时回溯证据；
- **优先级矩阵**：severity × effort 交叉排序——执行顺序为 Critical+S/M → Major+S → Critical+L → Major+M/L → Suggestion+S/M；`confidence: low` 的条目单独一节，标记"待人工确认后才排期"；
- **按轮次分批**：第 2 轮 quick-wins 批（Critical/Major 且 effort=S）、第 2 轮扩展批（02/03/04 的范围扩展项）、第 3 轮深度修复批（Critical/Major 且 effort=L）；
- **checkbox 状态跟踪**：每条 `- [ ]` open，修复后勾选并附 commit 号与验证证据链接；
- 顶部放统计表（type × severity 矩阵、已修复/总量）。

### 7.3 使用规则

- `REMEDIATION_PLAN.md` 是**活的清单**：每修复一条就地勾选；每轮收尾更新统计表。
- 第 3 轮起的日常开发直接从该表领任务（一条任务 = 一个引用条目 ID 的 commit）；表清空或进入稳态后，把整个 campaign 目录归档到 `docs/plans/archive/`（遵循仓库现有惯例）。
- 新一轮审查（第 2 轮 plan-01 等）产出的新发现，`round` 字段递增，合并后追加进同一份 REMEDIATION_PLAN.md。

### 7.4 收口启动提示词（分支全部合并后，在主机器的 ZCode 新会话中粘贴）

```text
多机并行战役的五条 plan/0X 分支已全部合并回主干。请执行收口：
1. 先读 docs/plans/2026-09-28-multi-machine-program/README.md 第七节。
2. 读取五份计划的 FINDINGS.json（分别在 docs/audit/2026-09-28/、docs/testcoverage/2026-09-28/、docs/plugins/2026-09-28/、docs/benchreports/2026-09-28/，以及 04 计划输出目录）并做条目对账（与各自 MD 台账数量一致）。
3. 合并生成本战役的统一优化计划表 docs/plans/2026-09-28-multi-machine-program/REMEDIATION_PLAN.md：严格按第七节的 schema、优先级矩阵与轮次分批，每条 checkbox 跟踪并链接源报告。
4. 只生成该计划表，不要开始修复任何条目；完成后报告统计摘要。
```

## 八、本目录文件

- `plan-01-full-repo-audit.md` — 全库深度审查计划
- `plan-02-test-coverage.md` — 测试覆盖补齐计划
- `plan-03-glsl-plugin-library.md` — GLSL 插件库扩充计划
- `plan-04-documentation-system.md` — 文档体系化计划
- `plan-05-benchmark-analysis.md` — 基准深度分析计划
- `REMEDIATION_PLAN.md` — 统一优化计划表（**合并五条分支之后**按第七节流程生成，是本战役的最终交付物）
