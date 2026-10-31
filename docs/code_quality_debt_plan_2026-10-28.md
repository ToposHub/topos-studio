# 代码质量还债实施计划（2026-10-28）

> 本文是对一份外部质量清单的核实结论 + 可直接执行的实施方案。
> 核实方式：全库 AST/grep 实测（2026-10-28，fix-ui 分支，工作区干净）。
> 执行纪律沿用仓库现行惯例：每个批次独立提交、注明域内测试结果与 GATE 结论、
> 行为变化必须如实声明（不冒称、不静默吞）。

---

## 一、核实结论：值不值得做？

**总判断：值得做，但有两处数据要纠正，一项定位要改。** 清单 7 项中 5 项属实、
1 项被低估、1 项半错。逐项实测如下：

| # | 清单项 | 核实结论 | 实测数据（2026-10-28） |
|---|--------|----------|------------------------|
| 1 | 静默异常清理 | ✅ 属实，数字略保守 | `except:...pass` **1121 处**、裸 `except:` **20 处**（src/ 全库，except 子句共 7004 个）。重灾区纠正：composite_page.py **68**、edit_gpu_preview_renderer.py **60**、timeline_preview_renderer.py **43**、file_controller.py **43**、ai_edit_controller.py **40**。清单说的 scene3d_node.py（46 处）不对——它是 except 总数多（96），静默 pass 并非最多 |
| 2 | lint + 类型检查工具链 | ⚠️ 工具链缺失属实，**债务规模被大幅夸大** | 全仓确无 ruff/mypy/pyright 配置。但函数返回注解率实测 **93%**（30574/32759），最差的 timeline_export.py 也有 **92%**（192/208）——清单说的"77% / 44%"不成立。定位应从"清还巨额欠债"改为"**锁定高质量存量、防退化**"，工作量比清单预估小得多 |
| 3 | i18n 硬编码泄漏 | ✅ 属实且**被低估** | 10 语言（de/en/es/fr/it/ja/ko/pt/ru/zh）结构完备确认。AST 只扫 5 种常见 setter（setText/setWindowTitle/setToolTip/setPlaceholderText/setTitle）就发现 **217 处字面量、分布 64 个文件**——清单说的 46 处明显偏少，实际规模约 4~5 倍。重灾：node_properties_panel.py 27、script_panel.py 13、color_picker.py 12、caption_workspace.py 9、mask_panel.py 8 |
| 4 | 巨型文件拆分 | ✅ 属实且**在持续增长** | 9 个文件超 1 万行确认。commands.py 实测 **19,735 行**（清单统计时 19,086，两周内又涨 ~650 行——越晚拆越贵）。其余：timeline_view.py 17,939 / timeline_preview_renderer.py 12,305 / timeline_export.py 12,002 / edit_controller.py 11,681 / composite_viewport.py 11,642 / media_pool_panel.py 11,389 / timeline_items.py 11,182 / composite_page.py 10,643 |
| 5 | 补无测试模块 | ✅ 属实 | src/main.py（339 行启动引导）、src/products/（6 个产品装配模块）、src/shared/ 下 logging/paths/diagnostics/layout/preferences/control_surfaces 等约 10 个小模块确认无专测；export、scripting 域偏薄（tests/ 有目录但量少） |
| 6 | 全量测试实跑 + skip 审计 | ⚠️ **半错，定位需改** | "从没有全量门禁"不成立：`.github/workflows/pr-fast-gate.yml`（P0-CI-01，2026-09-20 审计整改）已有 **nightly 全量**（CPU/软件 GL，junit 上传），PR gate 跑 core-gates 必过子集 + watch-only 已知失败（P1-ARCH-01 等六项）。skip 实测 **758 处**（清单 844）。**仍然值得做的部分**：CI 是 ubuntu+offscreen 软件 GL，本机是 macOS+Metal 真_gpu，大量 "gpu_renderer/headless" 条件 skip 在本机其实能跑——本地全量实跑能把"假 skip"激活成真覆盖，这是 CI 给不了的 |
| 7 | GLSL 插件扩充 | ✅ 属实 | 37 个 .tglsl（清单 35）、PLUGIN_DEV_GUIDE.md、tests/color/test_builtin_plugin_compile.py 真实 GL 编译兜底均确认 |

**顺带项核实：**
- **AGENTS.md 路径过时：确认属实。** 文中引用的 `src/core/image_engine.py`、
  `src/core/async_renderer.py`、`src/core/shader_compiler.py` 均不存在；实际位置在
  `src/features/color/core/`（async_renderer.py 在此）与 `src/shared/gpu/`。
  这会误导每一个新派出的 agent，**应最先修**。
- "CI 缺全量+lint 合并门禁"：**lint 门禁缺失属实**；全量门禁已有 nightly
  （见第 6 项），正确改述为"给 PR gate 补 lint job"。

---

## 二、优先级与执行顺序（修正版）

```
P0  AGENTS.md 路径修正 ── 半小时级，先做（它误导所有后续 agent）
 │
P1  静默异常清理 ──────── 阶段一主菜，按模块并行 fan-out
 │
P2  ruff + mypy 落地 ──── 宽松起步锁存量 → CI lint 门禁 → 逐轮收紧
 │
P3  i18n 硬编码修复 ───── 按文件分批（64 文件 ~217+ 处，比预估大 4~5 倍）
 │
P4  本地全量实跑 + skip 审计（对照 nightly junit，激活本机可跑的假 skip）
 │
P5  补无测试模块 ──────── 并行 fan-out（每个模块独立任务）
 │
P6  commands.py 拆分 ──── 独立立项、分批提交（最烧 Token，最后上）
 │
P7  GLSL 插件扩充 ─────── 随时可插入当调剂（独立交付物、可预期消耗）
```

依赖关系：P2 → CI lint 门禁（做完 P2 才有东西可挂）；P4 建议在 P1 之后
（异常清理会改动大量文件，先跑全量拿基线会立刻过期）；P6 强烈建议在 P1/P2/P5
之后（拆分时 3.5 万测试 + lint 门禁双兜底）。P3、P5、P7 相互独立可并行。

---

## 三、分项实施方案

### P0 · AGENTS.md 路径修正（前置小活）

| 项 | 内容 |
|----|------|
| 目标 | 消除对后续所有 agent 的路径误导 |
| 做法 | 1) 全文把 `src/core/X` 逐个映射到真实路径：`async_renderer.py` → `src/features/color/core/async_renderer.py`；`shader_compiler.py`/`image_engine.py` → 在 `src/features/color/core/` 与 `src/shared/gpu/` 下定位后逐一改写；2) 示例 prompt 里的路径同步更新；3) 顺手补一句"引用文件前先确认路径存在" |
| 验收 | 文中出现的每个 .py 路径 `ls` 均存在 |
| 预估 | 单 agent 一次往返，半小时内 |

### P1 · 静默异常清理（阶段一主菜）

**范围**：src/ 全库 1121 处 `except:...pass` + 20 处裸 `except:`。
**原则**（每处三选一，写入提交说明统计）：
1. **该吞**——回读采样、可选功能探测、清理路径等幂等降级点：保留 pass，
   但裸 `except:` 一律改为 `except Exception:`（杜绝吞 `KeyboardInterrupt`/`SystemExit`）；
2. **该记**——吞了会导致"看不见的坏"的：加 `logger.debug/warning`（用所在模块
   已有 logger，没有则不新建、上报该文件交人工定）；
3. **该抛**——吞了会导致数据损坏/静默错误结果的：删 try 或改上抛，**此类必须
   单独列出逐条人工复审**，因为它改变行为。

**批次划分**（按实测重灾区 + 域聚合，每批一个 agent，跑完即提交）：

| 批次 | 范围 | 静默数（实测） |
|------|------|---------------|
| B1 | src/features/composite/ui/（composite_page 68 + composite_viewport 24 + …） | ~110 |
| B2 | src/features/edit/core/ 三大渲染器（gpu_preview 60 + timeline_preview 43 + …） | ~120 |
| B3 | src/app/controllers/（file 43 + ai_edit 40 + edit 22 + page 18 + …） | ~140 |
| B4 | src/features/edit/core/ 其余 + audio_playback 23 | ~60 |
| B5 | src/shared/export/（topos_encoder 16 + …） | ~40 |
| B6-B10 | 其余按目录均分 | 每批 ~80-120 |

**每批验收门禁**：
- 域内 pytest 全绿（B1→tests/composite、B2/B4→tests/edit+tests/render、
  B3→tests/app、B5→tests/export + tests/deliver）；
- `grep -c "except:.*$\|except Exception:.*pass"` 该批次文件清零或仅剩
  白名单（白名单随提交说明登记文件:行号:理由）；
- 行为变更类（该抛）单独 commit，逐条列 diff 摘要。

**预估**：10 批 × 单批 1 次 agent 往返；读密集但每处独立，中断无损。
**风险**：低（90% 是加日志/改 except 类型）；"该抛"类是唯一行为风险点，靠
人工复审 + 测试兜底。

### P2 · lint + 类型检查工具链落地

**定位修正**：存量质量已高（注解率 93%），本项是"**上锁**"不是"还债"，
比清单预估便宜得多。

**步骤**：
1. **ruff 宽松起步**：`[tool.ruff]` 进 pyproject.toml。首版只开
   `E9/F63/F7/F82`（语法/未定义名级，零误报）+ `line-length = 120`（宽）。
   首跑清零（预期 <50 处）。
2. **mypy 增量**：`[tool.mypy]` `ignore_missing_imports = true`、
   `follow_imports = silent`、先 `files = ["src/shared", "src/products"]`
   （小而稳的域），逐轮把 `src/features/*`、`src/app` 加进 files。
   每扩一个域，当轮清零该域 error。
3. **CI 门禁**：pr-fast-gate.yml 加 `lint` job（ruff check + mypy 当前
   files 范围），设为 required；与 core-gates 并列。
4. **收紧节奏**（每轮独立提交，都从 PR 过）：
   - 第 2 轮：ruff 加 `F`（pyflakes 全量）+ `B`（bugbear，选 `B006/B008`）；
   - 第 3 轮：加 `UP`（pyupgrade，限 `--target-version py310`）；
   - 之后按需 `SIM`/`C4`，每轮先看违规数再决定收不收。

**预估**：配置 + 首轮清零 1 次往返；每轮收紧 0.5~1 次往返。
**风险**：低。mypy 对 PyQt6/ModernGL 的 stub 缺失靠 ignore_missing_imports
兜住，不追求 strict。

### P3 · i18n 硬编码修复（规模上调为 ~217 处 / 64 文件）

**步骤**：
1. 先跑一次全量 AST 扫描（不止 5 种 setter，补 `QPushButton("...")`、
   `addAction("...")`、`setTabText`、`QMenu("...")` 等构造器/方法），
   产出完整清单存 `docs/i18n_leak_inventory_2026-10-28.md`（含文件:行号:原文）；
2. 明确豁免类（不进清单）：调试/日志字符串、异常 message、key 名、
   数字/符号类（"100%" 这类保留原文不翻译）；
3. 按文件分批（约 8~10 批），每批：补 `tr("key")` → 在 10 个
   `locales/*.json` 补齐翻译（en_US 先定源文案，其余 9 语言翻译）→
   域内测试 + 手动冒烟对应面板；
4. **防回归**：P2 完成后加一条 ruff 自定义检查或脚本
   `tools/check_i18n_literals.py` 进 CI（扫新增字面量 setter 直接红）。

**预估**：每批 1 次往返 + 人工过一眼译文；总量比清单预估多 4~5 倍，排期
按 64 文件算而不是 46 处。
**风险**：低（纯加 key，不动逻辑）；注意布局——译文长度变化可能撑破
固定宽度控件，冒烟时看一眼。

### P4 · 本地全量实跑 + skip 审计

**定位修正**：CI 已有 nightly 全量（ubuntu + 软件 GL），本项的独特价值是
**macOS/Metal 真 GPU 环境**——大量 `gpu_renderer`/headless-GL 条件 skip
在本机不该 skip。

**步骤**：
1. 本机全量：`python -m pytest -p no:cacheprovider --junitxml=local-full.xml`
   （预计 3.5 万用例，先用 `-x` 无关，全跑；注意 pytest.ini 已有
   `faulthandler_timeout = 300` 兜卡死）；
2. 拉最近一次 nightly junit（Actions artifact `nightly-junit`）做差集：
   - CI 绿 / 本机红 → macOS 特有回归，逐个立案（参考仓库已有纪律：
     已知失败登记在案不许静默）；
   - CI 红（watch 域）/ 本机绿 → 提信息给对应 P1-* 已知失败收口；
3. skip 审计 758 处：分类统计 skip 条件（GL 能力 / 真机媒体 / 平台 /
   外部服务），标出"本机满足条件却因条件写得太保守而 skip"的**假 skip**，
   逐个改成能力探测式（如 `pytest.mark.skipif(not has_metal_ctx(), ...)`），
   激活成真覆盖并在提交里登记激活数；
4. 产出 `docs/local_full_run_report_2026-10-28.md`（通过率/差集/激活清单）。

**风险**：中——本机跑全量可能暴露 macOS 段错误（仓库已知：standalone
Metal 上下文与 Qt 控件同进程随机 Segfault，CI 用 ubuntu+offscreen 规避的
正是它）。若复现，如实登记为已知问题，不硬修。

### P5 · 补无测试模块测试（并行 fan-out）

每个模块一个独立任务，互不依赖，可全并行：

| 任务 | 模块 | 测试要点 |
|------|------|----------|
| T1 | src/main.py（339 行） | 启动参数解析、早期致命错的退出路径、`--version/--help`；不起真 Qt 事件循环（monkeypatch QApplication） |
| T2 | src/products/（6 文件） | 产品装配矩阵：各产品 feature 集合正确、互斥项不装配、packaging 元数据 |
| T3 | src/shared/logging + paths + diagnostics | 路径解析（含中文/空格目录）、日志初始化幂等、诊断包内容 |
| T4 | src/shared/layout + preferences | 布局序列化/恢复往返、偏好读写与默认值合并 |
| T5 | src/shared/control_surfaces | 协议解析、按键映射、无设备时的空转 |
| T6 | export/scripting 域加固 | 按现有 tests/export、tests/scripting 缺口补 |

**验收**：每任务新增测试独立提交，接入 pr-fast-gate 的 core-gates 或
对应域 job；覆盖率不设硬指标，以"公共 API 每个至少一条正路径 + 一条
失败路径"为准。
**预估**：单任务 1~2 次往返，6 任务全并行。

### P6 · commands.py 拆分（独立立项，最后上）

**现状**：19,735 行 / 667 函数，且仍在增长（两周 +650 行）。
**前置条件**：P1（该文件静默异常已清）+ P2（lint 门禁已挂）+ P5（edit 域
测试加固）完成后再动。

**步骤**（一次只拆一个切面，拆完必提交）：
1. **只读侦察**（不产码）：用 AST 导出 667 个函数的清单——每个函数的
   类归属、被引用方（全库 grep 调用点）、彼此调用簇。产出聚类草案：
   按 Command 基类族 + mixin 切（预判：timeline 命令簇 / clip 操作簇 /
   track 簇 / undo 栈基础设施 / 序列化簇）；
2. **切面一：纯搬移**——把独立性最高的簇（被引用最少、边界最清晰）搬去
   `src/features/edit/core/commands/` 包内独立模块，原 `commands.py`
   保留 re-export（`from .commands.timeline import *` 式），**保证所有
   既有 import 路径不破**；
3. 每搬一簇：`grep` 全库旧路径引用清零确认 + tests/edit 全量 +
  core-gates 绿 → 提交；
4. 全部簇搬完后，视情况把 `commands.py` 降级为纯 facade 或删除
  （这一步单独评估，不做也成立——re-export facade 已经解决了"1 万 9
  行单文件"的可维护性问题）；
5. 其余 8 个万行文件**不在本计划内**，每个另行立项（timeline_view.py
  17,939 行是下一个，但 UI 文件拆分风险高于 commands.py，先攒拆分经验）。

**预估**：侦察 1~2 次往返；每簇搬移 0.5~1 次往返；总计 10+ 次往返，
全程最贵。
**风险**：中——纯搬移 + re-export 策略下行为风险极低，主要风险是
循环 import（簇间互引），侦察阶段用调用簇分析提前识别，互引簇不强拆。

### P7 · GLSL 插件库扩充（调剂项，随时可插）

**前置**：每个新插件开工前先对照现有 37 个避免重复
（`ls plugins/*.tglsl` + PLUGIN_DEV_GUIDE.md）。
**流程**（每个插件 = 一个独立交付物）：
1. 候选方向确认后写 `.tglsl`（严格遵循 `.tglsl` 规范：数字带 `.0`、
   循环边界常量、`@param` 定义 UI 参数）；
2. `tests/color/test_builtin_plugin_compile.py` 真实 GL 编译过；
3. 补 `tests/color/test_plugin_params.py` 参数契约用例；
4. 提交（一插件一提交）。
**候选方向**（开工前需对照现有 37 个去重）：胶片颗粒/氛围光效类、
皮肤修饰频段分离类、天空替换辅助类——以侦察现有清单后的空缺为准。

---

## 四、CI 收尾（P2/P4 完成后）

pr-fast-gate.yml 增加：
- `lint` job（ruff + mypy，required）；
- 可选：`tools/check_i18n_literals.py` 作为 watch-only job 先跑观察，
  误报率可接受后翻 required。

不新增独立 workflow——nightly 全量已存在，不重复造。

---

## 五、执行总表

| 阶段 | 内容 | 并行度 | 预估往返 | 里程碑验收 |
|------|------|--------|----------|------------|
| P0 | AGENTS.md 修正 | 1 | 1 | 路径全部存在 |
| P1 | 静默异常 10 批 | 5~10 agent | 10 | 静默 except 清零/白名单登记，域测全绿 |
| P2 | ruff+mypy+CI | 串行 | 3~5 | lint 门禁 required，首轮清零 |
| P3 | i18n 8~10 批 | 4~8 agent | 8~10 | 清单文件清零，10 语言补齐 |
| P4 | 本地全量+skip | 1~2 | 2~3 | 差集报告 + 假 skip 激活 |
| P5 | 补测试 6 任务 | 6 agent | 6~12 | 无测试模块清零 |
| P6 | commands.py 拆分 | 1（串行） | 10+ | 包化完成，import 不破，全量绿 |
| P7 | 插件扩充 | 1~2 | 每插件 1 | 编译测试过 + 参数契约 |

**不建议做的**：无——清单 7 项核实后全部成立（含两处数据修正）。
唯一砍掉的是"新建全量 CI workflow"：nightly 已存在，补 lint job 即可。

---

## 六、完成定义（整体 DoD）

1. `except:...pass` 仅剩登记在案的白名单；裸 `except:` 为 0；
2. `ruff check` + `mypy`（files=src 全量）在 PR required 门禁里绿；
3. i18n 清单文件清零，防回归检查入 CI；
4. 本机全量通过率/差集报告在案，假 skip 激活数登记；
5. src/main.py、products、shared 小模块均有专测并入门禁；
6. commands.py 包化，`git log` 可追溯每簇搬移，全量测试绿。
