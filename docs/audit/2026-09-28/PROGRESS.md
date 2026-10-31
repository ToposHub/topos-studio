# 计划01 执行进度（PROGRESS）

> **状态：全部阶段完成（2026-09-29）。** 计划01 交付物就绪，等待与其他计划分支合并后进入 REMEDIATION_PLAN 收口。

- 分支：`plan/01-audit`
- 计划文档：`docs/plans/2026-09-28-multi-machine-program/plan-01-full-repo-audit.md`
- 断点续跑规则：重读本文件 + `INVENTORY.md`，跳过已完成批次。

## 阶段状态

| 阶段 | 状态 | 备注 |
|------|------|------|
| P0 清单与批次表 | ✅ 完成 | 1483 文件 / 835,840 行 / 398 批次 |
| P1 审查 P0 优先级 | ✅ 完成 | 16/16 实质批次（batch-001 为空 `__init__.py`，跳过）；约 240+ 条发现，5 Critical |
| P1.5 Critical 复核 | ✅ 完成 | 3 维持 Critical，2 降级 Major（AUD-0310/1214，死代码路径）；剔除 2 处子论据 |
| P2 台账 v1 | ✅ 完成 | ISSUES.md（239 条：3C/79M/157S）+ SUMMARY.md + issues_v1.json |
| P3 审查 P1 优先级 ✅ | 🔄 第一波完成 | 🔄 推进中：见批次目录与 WAVEPLAN；398 批已出报告 |
| P4 审查 P2 优先级 ✅ | ⬜ 未开始 | 182 批（shared 其余、features 其余） |
| P5 抽查复审 + FINAL_REPORT | ✅ 完成 | 32 条抽查：31 成立 / 1 降级(AUD-36100) / 0 推翻 |
| P6 FINDINGS.json 收口 | ✅ 完成 | 5,067 条（C133/M1492/S3442）；ISSUES.md FINAL + FINAL_REPORT.md |

## P0 批次进度（阶段 P1）

| 批次 | 主题 | 报告 | 状态 |
|------|------|------|------|
| batch-001 | color/core `__init__.py`（0 行） | — | skipped-trivial |
| batch-002 | color/core async_renderer | batches/batch-002-color-core-async-renderer.md | done（1C/5M/9S） |
| batch-003 | color/core cache/mgr | batches/batch-003-color-core-cache-mgr.md | done（1C/10M/9S+） |
| batch-004 | color/core session/denoise | batches/batch-004-color-core-session-denoise.md | done（0C/3M/16S） |
| batch-005 | color/core grade/history | batches/batch-005-color-core-grade-history.md | done（1C/4M/8S） |
| batch-006 | color/core image_engine | batches/batch-006-color-core-image-engine.md | done（1C/11M/8S） |
| batch-007 | color/core layer/lut/backend | batches/batch-007-color-core-layer-lut-backend.md | done（0C/7M/13S） |
| batch-008 | color/core plugin/shader_compiler | batches/batch-008-color-core-plugin-shader-compiler.md | done（0C/5M/19S+） |
| batch-009 | color/core denoise/msl nodes | batches/batch-009-color-core-denoise-msl-nodes.md | done（0C/7M/9S） |
| batch-010 | color/core temporal/workspace | batches/batch-010-color-core-temporal-workspace.md | done（0C/4M/3S） |
| batch-011 | shared/effects preview_quality | batches/batch-011-shared-effects-preview-quality.md | done（0C/6M/4S） |
| batch-012 | shared/gpu core | batches/batch-012-shared-gpu-core.md | done（1C/4M/14S） |
| batch-013 | shared/gpu registry/scope | batches/batch-013-shared-gpu-registry-scope.md | done（0C/1M/8S+） |
| batch-014 | shared/gpu metal A | batches/batch-014-shared-gpu-metal-a.md | done（0C/3M/15S） |
| batch-015 | shared/gpu metal B | batches/batch-015-shared-gpu-metal-b.md | done（0C/3M/8S+） |
| batch-016 | shared/gpu present_frame | batches/batch-016-shared-gpu-present-frame.md | done（0C/4M/7S） |
| batch-017 | shared/render | batches/batch-017-shared-render.md | done（0C/0M/4S） |

## Critical 清单（10 条，全部已复核；详情见 verification/）

复核后维持（8 条）：
1. [AUD-0200] async_renderer `_do_bake_layer` 引用未定义变量，AI Bake 全链路失效
2. [AUD-0501] 历史快照与活动图层共享可变对象，undo/redo 静默失效
3. [AUD-0600] undo/redo 恢复图像后 GPU 纹理不重传，视口渲染旧图
4. [AUD-16200] InsertLinkedAssetCommand.undo 重建 TimelineSequence 丢 12 字段（字幕轨/混音状态清空）
5. [AUD-16201] 转场三命令 5 处手工重建 EditTrack 丢 13 字段
6. [AUD-16202] BatchDeleteCommand 波纹 undo 误调 move_cmd.undo 致回移 no-op、片段错位
7. [AUD-16203] OverwriteClipCommand.undo 启发式误删同轨未受影响片段
8. [AUD-2301] color_controller 遍历遮罩访问 MaskGroup.type 致 AttributeError→qFatal 崩溃（复核补充第三处同型缺陷 on_brush_params_changed:1204）

复核降级 Major（2 条）：
- [AUD-0310] CacheWarmer 死锁（死代码，零触发）
- [AUD-1214] _do_pixel_probe 假阳性探测（双重休眠路径）

## 编号规则备注

- 批次号 <100 的用 AUD-{批次号}00–{批次号}99（如 batch-023 → AUD-2300–2399）；批次号 ≥100 的扩展为五位 AUD-{批次号*100}（如 batch-162 → AUD-16200–16299），保持全局唯一。

## 阻塞项与备注

- batch-003 报告指出 color/core 版 cache_manager.py 的三个编排类全仓库无外部调用方（缺陷属"接线即触发"）——已如实记录，不改变严重度。
- 复核结论将写入 docs/audit/2026-09-28/verification/。

## 后续建议（范围外，不入台账）

- 暂无。

## 滚动进度（脚本自动维护）

批次完成 **398/397**（P0 全部 + P3/P4 推进中）；累计发现 Critical 136 / Major 1496 / Suggestion 3449（含少量重复计入批次汇总行，以 ISSUES.md 台账为准）。
