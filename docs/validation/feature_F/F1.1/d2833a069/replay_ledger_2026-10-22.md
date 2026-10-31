# F1.1 · 26 项历史缺陷复测台账

> 日期：2026-10-22；基线 commit：`d2833a069`（feat/f-plan）。
> 方法：逐项映射既有 focused 测试与修复提交（映射审计另存），在当前 HEAD 重放证据批次；残余缺口补 focused 测试/真实窗口驱动；仍复现的按「先补测试→修生产入口」处置。

## 1. 重放结果总览

- **证据批次重放**（20 项的回归测试集，`tests/edit/test_fix_round6_*`、`test_fix_round7_*`、`tests/deliver/test_fix_round6_phase1a_delivery.py`、`test_fix_round7_delivery_context.py`、`test_delivery_capability.py`、`test_interchange.py`）：**191 passed**（offscreen 单进程）。
- **新增补齐测试**：`tests/edit/test_f1_replay_gaps.py` **6 passed**（D03×3 / D07×1 / U07×2）。
- **真实窗口重放（U06）**：`tests/validation/drive_source_switch_stress.py` 30 次源替换+跨段 scrub → **5/5 PASS**（0 异常 / 0 GL 1282 / App 存活 / RSS +38MB）。报告：`u06_run_2026-10-22/report.json`。
- **Q01 单进程全量**：tests/edit 全量单进程运行，结果见 §3 Q01 行（R22 子进程硬超时门禁之上补充「单进程不挂起」证据）。

## 2. 逐项判定（26 项）

| 编号 | 判定 | 证据 |
|---|---|---|
| C01 | 当前通过 | 重放批次（TestC01TransformRanges + R11 迁移 4 例） |
| C02 | 当前通过 | 重放批次（TestC02MoveAnimationShift + R14 undo 4 例） |
| C03 | 当前通过 | 重放批次（TestC03MoveTransitions） |
| C04 | 当前通过（密度低，登记） | 单例 `TestC04LinkedSpeedUndoTransition`；加密归 F2.8 变速验收 |
| C05 | 当前通过 | 重放批次（TestC05FreezeMigration + test_freeze_frame 9 例） |
| C06 | 当前通过 | 重放批次（TestC06LockedTrackWrites + 锁轨批） |
| D01 | 当前通过 | 重放批次（TestD01MasterRetime + round7 12 例） |
| D02 | 当前通过 | 重放批次（TestD02NormalizeAnalysis 反相/静音） |
| D03 | **仍复现并已修** | 复现：分析返回前关闭对话框 → 旧 `_on_finished` 对已销毁控件 setText/setValue（RuntimeError + 过时增益回写）。修复：`TimelineView._finalize_normalize_analysis` 静态方法（窄域 RuntimeError 守卫 + 线程尽力 quit），`timeline_view.py` 重接线；`test_f1_replay_gaps.py` 3 用例锁定（存活写入/销毁静默丢弃/取消不触碰） |
| D04 | 当前通过 | 重放批次（TestD04AnimatedFxAudio + R08） |
| D05 | 当前通过 | 重放批次（TestD05StemMasterParity 4 例） |
| D06 | 当前通过 | 重放批次（TestD06NonIntegerFps 总样本 480480）+ F0.4 精确向量 |
| D07 | 当前通过（本轮补测试） | 生产代码异常路径已有逐 clip `except→continue` + `finally` 关闭全部 decoder（timeline_export.py:4411-4422）；补异常路径 focused 测试（decoder 抛异常→全部关闭+渲染继续） |
| D08 | 当前通过 | 重放批次（TestD08AssCommas 4 例） |
| D09 | 当前通过 | 重放批次（TestD09TtmlTimebase 5 例 + R12/R13 11 例） |
| D10 | 当前通过（HDR 行登记） | 重放批次（TestD10EnumValueExtraction + R20）；HDR 专属 enum 用例归 F5.1 色彩闭环 |
| U01 | 当前通过 | 重放批次（TestU01CustomRangePreflight 5 例） |
| U02 | 当前通过 | 重放批次（TestU02NestedSequencePlumbing + R02） |
| U03 | 当前通过 | 重放批次（TestU03OtherTimelineSelection 6 例） |
| U04 | 当前通过（信号层） | 重放批次（test_fix_round6_u04 7 例）；像素级四端同帧归 F2.3 Trim Viewer 验收 |
| U05 | 当前通过（信号层） | 重放批次（TestU05CaptionFindSeek 3 例）；画面像素级归 F9.2 |
| U06 | 当前通过（真实窗口重放锁定） | 30 次真实切源+scrub：0 异常/0 GL 1282/存活/RSS+38MB（R21 修复获 G 级回归）；driver `tests/validation/drive_source_switch_stress.py` |
| U07 | 当前通过（本轮补测试） | locale 断言：linked_selection/show_markers 条目存在且无「即将推出」占位 |
| Q01 | **仍复现并部分修复（根因全链定位；残项移交 F1.2）** | 复现①：单进程全量在 `test_p2_09_session_pool.py` 挂起——转储栈：主线程 `__del__→close()→container.close()` 冻死。**已修**：`__del__` 路径完全不做容器关闭（`from_gc=True` 停靠 `_DEFERRED_CONTAINERS` 注册表，防 PyAV dealloc 再触发）；focused 测试锁定（GC 路径不关容器+停靠注册表）。复现②：`test_timeline_metal_export_renderer.py`（92%）显式 close 楔死——四次转储（run2 join / run3 join / run4 Thread.start / run5 常驻 closer）逐步收敛根因：**av 容器 close 与并发解码在 libav↔GIL 间锁序反转**（close 持 GIL 等 libav 锁；解码线程持 libav 锁等 GIL），楔死期间任何 Python 线程无法推进。缓解已落地：容器关闭全部入队单例常驻守护线程（主线程只 `queue.put`，不 spawn/join），R22 子进程门禁继续兜底 CI。**根治方案登记 F1.2 实施**：FFmpegMediaSource 全解码入口加进程级 in-flight 计数，仅计数归零的空闲窗口执行容器关闭（打断锁序反转）。证据：`/tmp/q01_full_edit_run{1..5}.log`（转储栈逐次收敛）；focused 测试 ×4 |
| Q02 | 当前通过 | 重放批次（nested preflight 完整模型夹具） |
| Q03 | 当前通过 | 重放批次（TestInterchangeCapabilities） |

## 3. Q01 单进程全量重放（五轮收敛）

- **run1（修复前）**：`test_p2_09_session_pool.py` 挂起（faulthandler 5 分钟转储）——Q01 历史挂起真实复现。
- **run2（限时 join）**：同点挂起，转储证明 join 也被 GIL 冻死。
- **run3（GC 停靠）**：**越过 session_pool 挂起点**（原 Q01 __del__ 场景修复生效）；在 92% `test_timeline_metal_export_renderer.py` 发现第二个楔死点（显式 close + 并发解码）。
- **run4（发射后不等待）**：主线程冻在 `Thread.start()`——spawn 同样被 GIL 冻死。
- **run5（常驻 closer + 队列）**：主线程不再 spawn/join；但常驻 closer 自身执行到死锁的 av close（持 GIL 等 libav），92% 同点楔死——根因最终收敛为 **libav↔GIL 锁序反转**，Python 层无法在被楔死的进程内自救，根治需空闲窗口关闭（in-flight 计数），登记 F1.2。
- 五轮中 session_pool 挂起点（run1 唯一复现点）在 run3-5 均不再出现——已修部分有回归证据；`test_timeline_metal_export_renderer` 及其后 8% 测试受 R22 子进程门禁保护（CI 口径不受影响）。
- 媒体域抽样批次：3455 passed / 1 env-failed（`test_declared_sources_exist`：本机缺可选真实素材 2K_topos_proxy.mov，预先存在的环境缺项，非回归）。

## 4. 跨页闭环（Edit→Color→Composite→Deliver→保存/重启）

F0.3 扩展黄金工程已覆盖结构层（双序列/嵌套/链接/调色栈/字幕/离线 + 序列化往返 + Undo 回环）；逐页帧数/身份/时码一致性在 F3.1/F5.1/F9.3 各自验收中用该黄金工程驱动。

## 5. 结论

26 项处置完毕：20 项当前通过（D07/U07 本轮补测试、U06 本轮补真实窗口证据）；D03 仍复现并已修（守卫+3 用例）；Q01 部分修复（原 `__del__` 挂起场景已修并有五轮回归证据；并发解码下显式 close 楔死点根因全链定位——libav↔GIL 锁序反转，缓解=常驻 closer 队列已落地，根治=in-flight 空闲窗口关闭，移交 F1.2）；3 项登记后续任务像素级加密（C04→F2.8、D10-HDR→F5.1、U04/U05→F2.3/F9.2）。关键内容/状态损失项：无。
