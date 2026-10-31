# 计划01 全库深度审查：最终报告（FINAL_REPORT）

日期：2026-09-29 ｜ 分支：`plan/01-audit` ｜ 执行：主会话编排 + 353 个 fan-out 子智能体

## 1. 执行摘要

对 `src/` 全部 **1,483 个 Python 文件、835,840 行代码**完成了全量静态审查（398 个批次，无一跳过；batch-001 为空 `__init__.py` 标记通过）。共产出 **5,067 条结构化发现**（去重后），全部带真实代码行号证据：

| 严重度 | 数量 | 说明 |
|--------|------|------|
| Critical | **133** | 可致崩溃/数据丢失/渲染错误（复核后口径，含 3 条复核降级） |
| Major | **1,492** | 功能缺陷或明确风险 |
| Suggestion | **3,442** | 质量改进（以类型注解缺失 2,930 条为主） |

类型分布：bug 1,184 / risk 620 / process 2,930 / doc-gap 160 / perf-opportunity 130 / test-gap 43。

## 2. 质量控制与验收

- **编号唯一性**：每批次独占 AUD 编号区间（批次号×100），全程无冲突。
- **Critical 全量复核（早期）**：前 10 条 Critical 逐条独立复核——8 维持、2 降级（AUD-0310 CacheWarmer 死锁、AUD-1214 像素探测假阳性，均为死代码路径零触发），并剔除 2 处不成立子论据、补充发现 1 处同型缺陷。
- **P5 随机抽查**：32 条（14 Critical + 12 Major + 6 Suggestion，随机种子 20260929，排除已复核批次）——31 条完全成立、1 条部分成立降级（AUD-36100，D3D11 调度器无生产调用方）、**0 条推翻**。推翻率 0%，优于计划 <10% 验收线。
- 多个子智能体在报告中附了 venv 内实测复现（PyQt6 offscreen、numpy、ffmpeg），并非纯模式匹配。

## 3. 跨文件共性问题主题（修复时建议按主题横向推进）

1. **Qt 槽内未捕获异常 = 进程崩溃**（PyQt6 默认 qFatal）：未定义名/未导入（AUD-0200、AUD-10400、AUD-21100、AUD-3000、AUD-39305、AUD-31200 等 12+ 处）、槽参数误接（AUD-10910、AUD-37803）、外部数据强转（AUD-38000、AUD-33100、AUD-38404）。**建议第一优先建立全局 excepthook + 逐点修复**。
2. **undo/redo 数据丢失家族**：模型重建丢字段（AUD-16200/16201、AUD-2701~2703、AUD-36920、AUD-3700X）、快照浅拷贝共享可变对象（AUD-0501、AUD-17100、AUD-0990X）、命令误删/回移失效（AUD-16202/16203、AUD-18800/18801）。
3. **GPU 资源生命周期**：无 deferred-release 的直接替换、无上限缓存、异常路径泄漏、跨线程 release（遍布 color/core、composite evaluators、video 上传管线、Metal/VT/D3D11 后端）。
4. **HDR 语义破坏**：无理由 clamp(0,1)、/255 假设、色彩空间标签错误（AUD-18400 uint16 全白、AUD-24700 RAW_VIEW、AUD-39800 BT.601 PAL 矩阵、AUD-36200 硬编码 bt709/limited）。
5. **UI 线程阻塞**：同步探测/解码/网络/渲染（AUD-0711、AUD-2810X、AUD-19603、AUD-30600 等）。
6. **死代码/未接线功能**：播放降级系统、PREFETCH_EVALUATE、menu 路径不可达、assets_dropped 死信号——功能"存在但从未生效"是一类系统性问题。

## 4. 交付物清单

- `INVENTORY.md`：全量文件清单与 398 批次表
- `batches/batch-NNN-*.md`：398 份批次审查报告（证据与修复建议）
- `verification/`：Critical 复核 4 份 + P5 抽查任务书 2 份 + 抽查结论 2 份
- `ISSUES.md`：全量台账（本报告的索引）
- `FINDINGS.json`：5,067 条机器可读发现（总纲第七节 schema，round=1）
- `SUMMARY.md`：阶段摘要 ｜ `PROGRESS.md`：执行履历 ｜ `issues_v1.json`：早期中间版本
- `tools/check_progress.py`、`assignments/`：驱动机制存档（供第 2 轮复用）

## 5. 遗留与建议

- 本轮为静态审查，未运行动态验证（除子智能体的局部实测）；第 2 轮建议：按 REMEDIATION_PLAN 修复时逐条补回归测试（呼应 plan-02）。
- 台账中 `confidence: low` 的条目（约 3%）排期前需人工确认。
- 审查覆盖了 src/ 全部 Python 文件；native/（C++）、GLSL 资产文件、脚本目录不在本轮范围，如需可作为第 2 轮扩展。
