# FLASH_REMEDIATION_PLAN — 批 E / 批 F 的 GLM5.3-flash 执行计划

> 生成日期：2026-10-01 ｜ 前置：批 A(135) ✅、B(30) ✅、C(9) ✅、D(1517) ✅ 已由主修复会话完成。
> 本计划覆盖剩余批次：**E 逐条（520 条 Suggestion）+ E-rollup（186 个模块行，底层 2,922 条 process 项）+ F（PLUG-004）**。
> 执行模型：**GLM5.3-flash 逐批执行 → GLM5.3 逐批复查**。每批独立可中断、可重跑。

---

## 0. 给执行者的三条铁律（先读这个）

1. **先读计划行再动手**：每条 ID 的完整描述（问题、位置、证据）在
   `docs/plans/2026-09-28-multi-machine-program/REMEDIATION_PLAN.md` 的 §2.E（约 1732-2259 行）。
   行号会因勾选标记漂移，**用 `grep -n "AUD-xxxxx" REMEDIATION_PLAN.md` 定位**。
2. **只改白名单**：每批的白名单就是该批的模块目录（见 §2 批次表 + `fixes/e_flash_batches.json`）。
   根因在白名单外 → 记 BLOCKED，不动那个文件。
3. **不许自己勾复选框**：修复完成后由主会话（或 GLM5.3 复查通过后）用
   `python3 fixes/check_fixes.py --mark "AUD-xxx,..." --commit <hash>` 勾选。flash 会话自己 **禁止** 编辑 REMEDIATION_PLAN.md。

## 1. 范围与不变式

| 批 | 内容 | 规模 |
|----|------|------|
| E 逐条 | Suggestion 级具体改进（risk/perf-opportunity/doc-gap） | 520 条 / 393 文件 / 22 个 flash 批 |
| E-rollup | 模块级 process 改进（类型注解/命名/小清理），按目录折叠 | 186 行（底层 2,922 项） |
| F | PLUG-004：第二批 GLSL 插件（12-20 个方向） | 1 条（大） |

**全批不变式（违反任意一条 = 该批作废重做）：**
- UI 线程绝不调用 OpenGL/ModernGL；GPU 对象释放走渲染线程/延迟路径
- GLSL 浮点字面量必须带 `.0`；循环边界常量；无隐式类型转换
- 不做无依据的 HDR 数值钳制/归一化（>1.0 值域必须保留）
- 数字 601/709 limited/full range 矩阵不可混用
- 禁止裸 `except:` / 吞错 `except Exception: pass`（需具体异常类型 + 留痕）
- 新/改函数必须有类型注解；信号槽更新 UI 需防递归
- 每条修复要有正例（修复生效）测试；锁定旧行为的既有测试若与修复冲突，更新断言并在报告披露

**已知预存失败（不要试图"修"它们，出现即忽略）：**
- 4 个 GPU 位精确测试（`test_timeline_color_grade_lut::test_cached_lut_matches_float_grade_exactly_for_real_project_wheels` + `test_timeline_gpu_export_renderer` 3 例）
- R2-012 顺序依赖（tests/app + tests/ai 组合跑时）
- x265 10-bit kill-switch、VT 硬编探针失败（环境）
- `test_track_sync_lock`（既有）、`test_p2_09_session_pool`（4K 真媒体，本机挂起——**不要整目录跑 tests/edit**，用定向文件）

## 2. E 逐条批次表（22 批）

每批的 **精确 ID 清单** 在 `fixes/e_flash_batches.json`（结构：`[{batch, modules: {dir: [AUD-...]}, n}]`）。
执行顺序建议从 EF-14 起（小批练手）→ 大批；或按表顺序。**单批 >26 条时按 ID 顺序切成 ≤24 条的子运行**（EF-01→3 个子运行，EF-02/03/04→2 个）。

| 批 | 模块（白名单） | 条数 | 建议邻接测试目录 |
|----|----------------|------|------------------|
| EF-01 | src/features/edit/core | 67 | tests/edit |
| EF-02 | src/shared/media | 45 | tests/media, tests/shared |
| EF-03 | src/features/edit/ui | 39 | tests/edit, tests/ui |
| EF-04 | src/features/edit/ai_editing | 37 | tests/edit/ai_editing |
| EF-05 | src/shared/video | 24 | tests/video, tests/shared |
| EF-06 | src/features/composite/ui | 21 | tests/composite, tests/ui |
| EF-07 | src/features/color/core | 19 | tests/color |
| EF-08 | src/app/controllers | 18 | tests/app |
| EF-09 | src/features/ai_workflow/tests | 18 | 该目录自身套件 |
| EF-10 | src/features/composite/render | 16 | tests/render, tests/composite |
| EF-11 | src/features/composite/render3d | 16 | tests/composite |
| EF-12 | src/features/composite/nodes | 15 | tests/composite |
| EF-13 | src/features/composite/core | 14 | tests/composite |
| EF-14 | src/shared/ui | 13 | tests/shared, tests/ui |
| EF-15 | src/shared/ai_models + src/features/ai_harness | 23 | tests/shared, src/features/ai_harness/tests |
| EF-16 | src/features/teaching_animation/ai + src/features/ai_harness/tests | 21 | tests/teaching_animation |
| EF-17 | src/features/color/ui + src/shared/gpu | 20 | tests/color, tests/shared |
| EF-18 | src/features/teaching_animation/render + src/shared/codec + src/shared/export | 21 | tests/teaching_animation, tests/render, tests/media |
| EF-19 | src/app(根) + composite/evaluators + teaching/ui + shared/project + app/ui | 24 | tests/app, tests/composite, tests/teaching_animation |
| EF-20 | color/tracking + ai_workflow/ui + composite/utils + deliver/core + shared/effects + teaching/core + shared/tracking + ai_workflow/adapters | 24 | 各自对应目录 |
| EF-21 | 杂项散点（见 json） | 24 | 按文件就近 |
| EF-22 | src/shared/utils | 1 | tests/shared |

## 3. E 逐条：单批执行规程（GLM5.3-flash）

1. `python3 -c "import json;print(json.load(open('fixes/e_flash_batches.json'))[i])"` 取本批 ID 与目录
2. 对每条 ID：grep 计划行 → 读描述与位置 → 修复（最小改动，不重构无关代码）
3. 新建测试 `tests/<就近目录>/test_ef<批号>_aud.py` 覆盖每条（正例+反例）
4. 验证关卡（全部必须过）：
   - `.venv/bin/python -m py_compile <所有改动源文件>`
   - `.venv/bin/python -m pytest <新测试> -q` 全绿
   - `.venv/bin/python -m pytest <该批邻接测试目录中相关文件，定向 2-4 个> -q` 无新增失败
5. 写报告 `fixes/reports/ef-<批号>.md`：逐条 ID → FIXED/INVALID/DUPLICATE/BLOCKED + 摘要 + 验证输出
6. `git add <白名单内文件> <测试> <报告> && git commit -m "[fix-E ef-XX] ..."`（**dev 分支，禁止建分支/push**）
7. 报告交 GLM5.3 复查（§6），通过后由复查会话执行 `check_fixes.py --mark`

## 4. E-rollup：模块行规程

- 位置：计划 §2.E-rollup（约 2260-2448 行），每行形如 `` `src/features/edit/ai_editing/`：248 条 process 级改进（类型注解/命名/小清理）｜台账: docs/audit/2026-09-28/ISSUES.md ``
- **一次处理一个模块行**；台账明细在 `docs/audit/2026-09-28/ISSUES.md`（按模块过滤）
- 工作内容：补类型注解、统一命名、删死代码/未用导入、小清理——**纯机械，不改行为**
- 验证：`py_compile` 全部改动文件 + 该模块对应测试目录定向跑（无新失败）+ `grep -rn "except:" <模块>` 零命中
- 完成一个模块行 → 报告 `fixes/reports/efr-<模块 slug>.md` + commit + 复查后勾选该行
- 大模块（>100 条）按子目录拆多次运行；类型注解可借 `.venv/bin/python -m pyright --outputjson <file> 2>/dev/null || true`（缺失则跳过，不算失败）

## 5. 批 F：PLUG-004（第二批 GLSL 插件）

- 依据：`docs/plans/2026-09-28-multi-machine-program/plan-03-glsl-plugin-library.md`（候选方向清单）+ `PLUGIN_DEV_GUIDE.md`（.tglsl 规范）
- flash 执行：每个插件独立运行单元；产出完整 `.tglsl` 文件（@param 定义 UI 参数、严格类型、常量循环边界、性能优化）
- **强制前置样例**：先读 3 个已入库插件（如 `plugins/vignette.tglsl` 等）对齐结构
- 验证：插件必须通过仓库的插件编译扫描（`pytest tests/ -k "plugin" -q` 定向）+ 手动 `shader_compiler` 冒烟
- GLM5.3 复查重点（§6 加码）：GLSL 红线逐项过；HDR 域无钳制；性能（无每像素昂贵运算）

## 6. GLM5.3 复查清单（每批必查，一票否决）

对 flash 的每批 diff 逐项检查：

1. **真实修复**：逐条 ID 对照计划行——是真修了根因，还是表面打勾/测试造假（正向断言必须体现修复语义）
2. **GLSL/HDR 红线**：浮点字面量 `.0`、常量循环边界、无 HDR 钳制、色彩矩阵正确（601/709 × limited/full）
3. **并发**：UI 线程无 GL 调用；锁内无重活；信号无递归；线程取消检查点
4. **资源**：GPU 对象延迟释放；异常路径无泄漏（FBO/纹理/VBO/容器/解码器）
5. **异常**：无裸 except；吞错必留痕
6. **测试质量**：新测试会在修复回退时失败（回归有效性抽查——随机挑 2 条回退验证）
7. **越界**：diff 文件 ⊆ 白名单 + 测试 + 报告；未碰 REMEDIATION_PLAN.md
8. 复查产出：`fixes/reviews/ef-<批号>-review.md`（PASS / FAIL+逐条问题）；PASS 才允许 `--mark` 与下一批

## 7. flash 提示词模板（直接复制使用）

```
你是 Topos Color 仓库的修复代理。工作目录：<repo 绝对路径>（含空格，命令加引号）。
分支必须保持 dev。绝对禁止：创建/切换分支、git push、修改 REMEDIATION_PLAN.md 复选框。

任务：批 EF-XX —— 修复 <N> 条 Suggestion 级审计发现。
ID 清单与白名单目录：<粘贴 e_flash_batches.json 对应段>
对每条 ID：先 grep -n "AUD-xxxxx" docs/plans/2026-09-28-multi-machine-program/REMEDIATION_PLAN.md
读完整描述与文件位置，再修复。只改白名单目录内文件；根因在白名单外 → 报告记 BLOCKED。

约束（.clinerules 摘要）：
- UI 线程绝不调用 OpenGL/ModernGL；GPU 释放延迟到渲染线程
- 禁止裸 except / 吞错；新改函数必须类型注解；信号防递归
- 不做无依据 HDR 钳制；601/709 limited/full 矩阵不混用
- 最小改动；不重构无关代码

验证（必须全过）：
1. .venv/bin/python -m py_compile <改动文件>
2. 新建 tests/<目录>/test_efXX_aud.py 覆盖每条（正例+反例），pytest 全绿
3. 定向跑该模块邻接测试 2-4 个文件，无新增失败
4. 遇已知预存失败（见 FLASH_REMEDIATION_PLAN.md §1 白名单）直接忽略

交付：报告 fixes/reports/ef-XX.md（逐条状态+摘要+验证输出）；
git add + commit（[fix-E ef-XX]，dev 分支）；最终回复状态表。不要 push。
```

## 8. 标记与提交纪律

- 一次 commit = 一个批次；message 格式 `[fix-E ef-XX] <一句话摘要> (N)` / `[fix-E efr-<mod>] ...` / `[fix-F plug-004] ...`
- `check_fixes.py --mark` 的 commit 填**该批自己的提交哈希**；INVALID/DUPLICATE 结论也要 mark 并 `--note "invalid:<理由>"`
- 每完成 5 批：跑 `python3 fixes/check_fixes.py` 看总量推进，在 FIXLOG.md 追加一行
- 全部完成判据：`check_fixes.py` 显示 bucket E done=713 pending=0、F done=1 pending=0

## 9. 度量与兜底

- 预期 flash 单批（≤24 条）耗时 10-25 分钟；卡死/空转超过 3 轮工具调用无进展 → 弃该批，记入 `fixes/reports/ef-XX.md`（ABORTED），交 GLM5.3 直接修
- flash 产出 FAIL 率若 >20%（某批次复查否决超 1/5 条目），该模块簇剩余批次升级给 GLM5.3 执行
- 所有复查记录归档 `fixes/reviews/`，最终汇总进 FIXLOG.md

---

### 附录 A：E 批模块簇速查（修复会话已核实的分布）

edit/core 67 · shared/media 45 · edit/ui 39 · edit/ai_editing 37 · shared/video 24 · composite/ui 21 ·
color/core 19 · app/controllers 18 · ai_workflow/tests 18 · composite/render 16 · composite/render3d 16 ·
composite/nodes 15 · composite/core 14 · shared/ui 13 · ai_models 12 · ai_harness 11 · teaching/ai 11 ·
ai_harness/tests 10 · color/ui 10 · shared/gpu 10 · teaching/render 9 · codec 6 · export 6 · app/ui 4 ·
color/tracking 4 · 其余 ≤3 条的散点目录见 `fixes/e_flash_batches.json`

### 附录 B：工具与路径

- venv Python：`.venv/bin/python`（pytest 9.0.3；typeguard/pyright 未装，可选）
- 计划主文件：`docs/plans/2026-09-28-multi-machine-program/REMEDIATION_PLAN.md`
- 勾选工具：`docs/plans/2026-09-28-multi-machine-program/fixes/check_fixes.py`
- 批次数据：`fixes/e_flash_batches.json`（22 批）/ `fixes/e_bucket_map.json`（原始簇映射）
- 报告目录：`fixes/reports/`（ef-*）与 `fixes/reviews/`（ef-*-review）
