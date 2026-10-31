# 计划 02：测试覆盖补齐（Test Coverage）

分支：`plan/02-tests` ｜ 性质：写测试（`tests/` 仅新增文件） ｜ 写路径：`tests/**`（仅新增）、`docs/testcoverage/2026-09-28/`、`constraints.txt`/`pyproject.toml`（仅增量加 coverage 依赖）

## 1. 目标

项目已有 1560 个测试文件，但覆盖不均匀。本计划：跑出真实覆盖率基线 → 找出低覆盖的高风险模块 → 按批次补针对性测试。**只加新测试文件，不改既有测试文件，不修 src 代码。**

## 2. 仓库关键事实（已核实）

- pytest 配置在 `pytest.ini`：`testpaths = tests`，marker 体系丰富（`gpu_renderer`、`webengine`、`html_renderer`、`slow`、`real_media`、`visual` 等约 25 个 marker）。
- 无 GPU 机器统一参数：`-m "not gpu_renderer and not webengine and not html_renderer and not slow"`。
- **项目尚未安装 pytest-cov**（pyproject/constraints 里没有），需要先补。
- `tests/` 现有子目录：`ai/ app/ architecture/ benchmark/ color/ composite/ core/ deliver/ edit/ export/ feature_availability/ file_explorer/ fixtures/ integration/ media/ motion/ native/ project/ render/ scripting/ shared/ ui/ vault/ video/ video_denoise/ visual/`，根目录有 `conftest.py`、`conftest_module_isolation.py`、`node_test_base.py`、`real_media.py`。
- 被测主体：`src/features/color/core/`（28 文件渲染管线）、`src/features/edit/core/`、`src/shared/`（40+ 子模块）、`src/app/`。

## 3. 阶段划分

### P0 环境与基线（半天机器时）
1. `pip install pytest-cov`，并在 `constraints.txt` 追加一行 `pytest-cov`（本计划对依赖文件仅有此一权力，纯增量）。
2. 先 `pytest --collect-only -q` 确认收集无错误（收集错误本身记入报告）。
3. 分片跑全量覆盖率（避免单进程跑 1560 个文件超时）：按 `tests/` 子目录分 6–8 片，每片：
   ```bash
   python -m pytest tests/<area> -m "not gpu_renderer and not webengine and not html_renderer and not slow" \
     --cov=src --cov-report=json:docs/testcoverage/2026-09-28/cov-<area>.json -p no:cacheprovider -q
   ```
   有 GPU 的机器可另跑 `gpu_renderer` 片。跑不通的环境问题记录后跳过，不阻塞。
4. 合并各片 jsoncov → `COVERAGE_BASELINE.md`（每模块行覆盖率排名表）。

### P1 缺口分析（纯分析）
- 从基线中筛出 **top 30 低覆盖模块**，按风险排序。排序依据（权重从高到低）：
  1. 属于 `features/color/core`、`shared/render`、`shared/gpu`、`shared/export`、`shared/codec`、`shared/project`（崩溃/数据丢失风险区）；
  2. 行数大且覆盖率 < 40%；
  3. 既有测试只有 happy path 的迹象（读一眼现有测试判断）。
- 产出 `GAP_REPORT.md`：每模块一节——现状覆盖率、现有测试文件、缺什么类型的用例（边界/异常/取消/并发/序列化兼容）。

### P2–P4 批次补测试（主体工作，fan-out）
- 每批 = 1 个目标模块。子智能体流程：
  1. 读目标模块源码 + 其现有测试（学习 fixture 与 mock 惯例）；
  2. 设计用例清单（正常路径 / 边界 / 异常与失败 / 取消路径 / 并发或重入——按模块性质选）；
  3. 写**新文件** `tests/<对应目录>/test_<module>_coverage.py`（或模块名冲突时 `test_<module>_gap.py`）；
  4. 实际运行新文件直到全绿；禁止为了通过而修改 src。
- 批次顺序 = P1 排序。并行度 2–4（pytest 运行占 CPU，别和子智能体数量叠满）。
- 每 3 批跑一次相关目录的整体 pytest，确认新文件与既有测试无相互干扰。

### P5 汇总
- `COVERAGE_SUMMARY.md`：before/after 对比、每个新测试文件覆盖了什么、未覆盖剩余项及原因（不可测/需 GPU/需真实媒体）。
- `FINAL_REPORT.md` + 更新 `PROGRESS.md`。
- **收口**：把 `BUGS.md` 全部条目（id 前缀 `BUG-`）与 `GAP_REPORT.md` 中仍未覆盖的缺口项（id 前缀 `GAP-`，type 为 test-gap）结构化为 `FINDINGS.json`，条目数与两个 MD 文件对账一致（schema 见总纲第七节）。

## 4. 硬性规则

1. **不改**：`src/**`、既有任何 `tests/` 文件、`pytest.ini`、CI 配置。发现 src 疑似 bug → 写 `BUGS.md`（格式：复现测试描述、file:line、证据、置信度），**不修**。
2. 新测试必须有类型注解，遵循 conftest 现有 fixture 惯例；mock 外部 IO/文件系统/GPU（需要 GPU 的用例直接标 `@pytest.mark.gpu_renderer`，让无 GPU 机器自动跳过）。
3. 不引入新第三方依赖（stdlib + pytest + pytest-cov + 项目已有依赖）。
4. 禁止写"凑数测试"（只断言不抛异常、快照无断言之类）——每个测试必须有会失败的断言。
5. `real_media` 相关用例依赖仓库内媒体矩阵，确认存在后再写，不存在就跳过该类用例。

## 5. 边界速查

| 允许写 | 禁止写 |
|--------|--------|
| `tests/**` 新增文件 | `tests/` 既有文件的任何修改 |
| `docs/testcoverage/2026-09-28/` | `docs/` 其他任何路径 |
| `constraints.txt` 追加 pytest-cov 一行 | `pyproject.toml` 其他字段、`pytest.ini` |
| — | `src/**`（发现 bug 只记 BUGS.md） |

## 6. 预估 token 消耗

基线运行 + 30 模块 × (读源码 + 设计 + 写测试 + 调试循环)，一轮约 **6000 万 – 1.5 亿 token**。第 2 轮可基于第一轮 SUMMARY 继续做剩余缺口。

## 7. 启动提示词（在本机器的 ZCode 新会话中粘贴）

```text
你是计划 02（测试覆盖补齐）的执行者。请先完整阅读以下两个文件并严格遵守：
1. docs/plans/2026-09-28-multi-machine-program/README.md（全局铁律，特别是：写路径白名单、不改既有测试、发现 src bug 只记 BUGS.md）
2. docs/plans/2026-09-28-multi-machine-program/plan-02-test-coverage.md（本计划全文）

然后从阶段 P0 开始执行。要求：
- 确认当前分支是 plan/02-tests，不是就先创建并切换。
- 无 GPU 机器统一加 marker 排除参数（见计划第 2 节）。
- 严格按计划的批次机制补测试：每批只新增测试文件、实际跑到全绿才算完成。
- 每完成一个批次更新 PROGRESS.md 并 git commit（前缀 [plan-02]）。
```
