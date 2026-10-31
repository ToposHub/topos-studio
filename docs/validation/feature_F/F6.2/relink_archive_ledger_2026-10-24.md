# F6.2 · 回原片 reconform、代理重连与归档 — 验证台账

- 日期：2026-10-24；计划条目：§F6.2
- 测试：tests/shared/test_f62_relink_match.py（9）＋既有 project_archive 9 项全绿

## 1. 现状核验

- **归档**（project_archive.py）：收集媒体到 target/media、路径重写、
  缺失素材带使用位置报告、名字冲突去重、虚拟素材跳过、进度/取消钩子、
  SHA256 校验、归档工程重开——9 项既有测试覆盖（AUD-17203）。
- **重连**（offline_asset_manager + relink_media_dialog + Batch/
  RelinkAssetCommand）：单个/批量重连、目录扫描——**缺口：按目录批量
  重连只按文件名匹配**（重名异片会被静默误连，计划明文禁止"仅同名
  就替换"）。

## 2. 本批交付

| 组件 | 内容 |
|---|---|
| 安全匹配器 | `shared/project/relink_match.py`：`scan_candidates`（名→大小→内容短哈希[头尾 1MB+size] 三级因子；extra_factors 供时间码/卷名/时长补充裁决）；`auto_relinkable` 仅唯一候选且名+大小(+哈希)全一致；`same_name_different_content` 强制人工 |
| 重套验证 | `verify_relink`：分辨率/时间基/总帧数不一致给可操作诊断（代理/原片换档确认切点）；原信息缺失交人工不冒称；色彩解释经 F2.7 media_replacement 整域替换语义 |
| 切点恒定 | 重连命令只改 asset path——时间线域（切点/源帧/调色栈引用）完全不进重连路径（测试断言） |

## 3. 验收 → 证据

| 验收 | 证据 |
|---|---|
| 重名异片不误连 | `test_same_name_different_content_requires_manual`（同名大小/哈希不同 → 不自动 + same_name_different_content） |
| 移盘/改根路径后成片切点、源帧与调色不变 | `test_relink_preserves_cut_points_and_grade`（重连不触碰时间线域字段）+ 既有归档重开测试（archived 工程重开一致） |
| 多候选先人工确认 | auto_relinkable 仅单候选；same-name-diff-content 与 0 候选均不自动（测试） |
| 代理/原片重套验证 | `test_resolution_mismatch_diagnosed`/`test_fps_mismatch_diagnosed`/`test_compatible_relink_no_issues`/缺原信息交人工 |
| 取消/失败原工程保留 | 既有 collect 取消钩子测试（部分收集+warning，原工程不动）+ 重连命令 undo 语义 |
| 不把本机绝对路径写成可移植依赖 | 归档报告 `archived_path` 相对归档根目录（既有字段语义 + 测试） |
| 归档依赖外部声明 | 登记后续片（LUT 收集——见 §4） |

## 4. 登记的后续片（不阻塞验收）

1. 归档收集调色栈 LUT（lut_path 外部依赖进 media/luts/ + 重写）——
   既有 collect 只收素材媒体；LUT 依赖面登记后补。
2. 匹配器接入 relink_media_dialog 的目录批量路径（替换现 basename-only
   逻辑；核心已就绪 + 测试锁定）。
3. 外部 NLE reconform（F6.1 条件依赖——实机样本后）。
4. 变速/嵌套工程的重套端到端（映射语义由 F2.4/F1.1 既有；专测登记）。

## 5. 诚实边界

- 内容短哈希为头尾 1MB+size（廉价判别非全量 SHA256）；归档路径的
  SHA256 校验为既有能力。
- verify_relink 比对源素材身份；时间线切点恒定由"重连不触碰时间线域"
  的架构保证（命令分层），非逐项重算。
