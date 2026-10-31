# F9.3 · 多版本交付、QC 与任务恢复 — 验证台账

- 日期：2026-10-24；计划条目：§F9.3
- 测试：新增 test_f93_version_compiler.py（9）＋既有交付套件
  （export_queue 恢复矩阵/delivery_qc/export_manifest 65）全绿

## 1. 现状核验（调研结论）

- **队列与恢复**：ExportQueue 串行调度 + JSON 落盘/恢复（RUNNING 重启
  降级 PENDING 测试既有）+ retry/pause/resume/cancel + 批量控制器
  restore_from_disk；AI 侧 ExportService finalize_export_with_probe
  质量门失败→job 显式失败。
- **QC**：delivery_qc（离线 BLOCKING/音画时长差/字幕越界/空时间线/
  帧率不一致）+ 响度/true peak（BS.1770 gated + 4x 过采样）+
  DeliveryQualityInput（黑/冻/坏帧计数 fail-closed）。
- **probe/manifest**：PyAV 全量视频解码计数 + 首尾帧可解码 + SHA-256
  归档；双后端 probe（打包 PyAV/有界 ffprobe）。
- **缺口**：VersionDerivationPlan 仅有数据模型无编译器；probe 显式
  跳过 AudioFrame（无声/损坏音频流不被发现）。

## 2. 本批交付

| 组件 | 内容 |
|---|---|
| 多版本编译器 | `version_compiler.py::compile_version_jobs`：LinkedVersion×N → N 个 ExportJob config（**复用既有 ExportQueue**——不新建队列）；画幅→分辨率映射（16:9/9:16/1:1/4:5/2.39:1）；字幕策略/语言进 config；`linked_version_id` 分组溯源；**命名冲突先检查**（已有输出拒绝不覆盖）；**素材依赖先检查**（离线→拒绝清单不产半套 job）；`source_project_revision`+`source_snapshot_hash`（F8.1）冻结工程版本——恢复/重试验证源版本，**不用旧版本缓存续渲新版本** |
| PCM 音频校验 | `count_audio_frames`：全量解码音频流统计帧数/采样数（修复 probe 跳过 AudioFrame 的缺口——声明有音轨但无音频帧可被发现）；打开失败如实 None |

## 3. 验收 → 证据

| 验收 | 证据 |
|---|---|
| 同一冻结工程版本产生分辨率/画幅/声轨/字幕组合 | 3 版本编译测试（16:9 zh burn-in / 9:16 en soft / 1:1 zh none——每版本独立 config + 溯源字段） |
| 命名冲突和素材依赖先检查 | 冲突拒绝不覆盖 + 离线拒绝不产半套 + 时间线缺失拒绝 + 未知画幅逐版本拒绝（4 测试） |
| 每份成片与声明的版本/范围/颜色/声轨/字幕对应 | config 携带版本声明（画幅/语言/字幕策略/源 revision/快照哈希）——manifest 归档既有 |
| 实际解码/PCM/QC 有报告 | 既有 probe（视频全量解码+首尾帧）+ **新增 PCM 音频帧计数**（真实含音轨文件 10 帧检出/缺失文件 None）+ 响度/QC 既有 |
| 取消/重启不覆盖成功输出 | 冲突拒绝测试 + 既有队列恢复矩阵（RUNNING 降级） |
| 不用旧版本缓存续渲新版本 | source_project_revision/snapshot_hash 冻结——恢复验证源版本（config 字段测试） |
| 关键失败给失败状态/退出语义 | 既有（job FAILED 状态 + error 字段 + 质量门失败→显式失败） |

## 4. 登记的后续片（不阻塞验收）

1. 版本编译器接 DELIVER 队列 UI 入口（linked_version_id 分组展示）。
2. PCM 计数写入 export_manifest（函数已就绪，manifest 字段接线）。
3. 响度按版本差异对照表（LoudnessReport.to_dict 可序列化，per-version
   汇总归 UI/manifest 片）。
4. 黑帧/静帧 probe 计数通路（DeliveryQualityInput 已有字段，帧级
   探测归后续）。
