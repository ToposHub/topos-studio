# F9.1 · 文稿驱动粗剪与删除口头语 — 验证台账

- 日期：2026-10-24；计划条目：§F9.1
- 测试：新增 test_f91_filler_removal.py（9）＋地基核验（词级时间戳/
  plan 链已有测试族，调研清单见下）全绿

## 1. 现状核验（Explore 调研结论）

**地基已存在且有测试**：
- 词级时间戳：ASR 内嵌 time_stamps / ForcedAligner 双路径
  （transcript_provider.py:667）→ `CaptionWordTiming`（帧基）+
  `source_span`（秒基）；
- canonical 链：`compile_plan` 白名单 op→command→MacroCommand（只产
  不提交）+ `state_fingerprint`（project_revision|manifest|timeline
  revision——过时计划拒绝）+ `commit_command` 统一 revision 边界；
- 审批链：`wf-talking-head-fine-cut` 模板（transcribe→plan→preview→
  approval→apply）+ PREVIEW 红线。
**缺口**：口头语检测/删除零实现（`TranscriptRevision.filler_words`
仅元数据字段）。

## 2. 本批交付

| 组件 | 内容 |
|---|---|
| 检测 | `filler_removal.py::detect_filler_words`：`CaptionWordTiming` + 内置 zh/en 词表（可扩展 extra_lexicon）→ 帧域命中区间（置信度排序）；**低置信（<0.6）不删→review_hits 人工排除**；标点/大小写归一；混合语言并集 |
| 幂等锚 | `timings_fingerprint`：同输入同哈希、内容变化哈希变——批量重试不重复删同段（配合 plan_repository content_hash 双保险） |
| 删除计划 | `build_filler_removal_plan`：可删命中 → `EditPlan`（**PREVIEW 模式** + DELETE_CLIP 白名单 op + intent 携带词/置信审计）——走既有 preview→approval 链**不新增旁路**；review 项不进计划 |

## 3. 验收 → 证据

| 验收 | 证据 |
|---|---|
| 文稿操作/画面/声音/字幕/marker 一致 | 删除计划经 canonical compile（白名单 op）+ linked A/V/字幕/marker 的 diff 预览由既有 preview_diff 节点执行（模板链） |
| 保留口头语人工排除、低置信度、混合语言 | review_hits 分离（低置信不删）+ 混合语言测试 |
| 批量重试以 plan/hash 和 revision 约束，不反复删同段 | timings_fingerprint 幂等 + base_revision 绑定（stale 拒绝既有 state_fingerprint） |
| 过时计划拒绝、取消无半提交、一次 Undo 可恢复 | 既有（state_fingerprint / compile 只产不提交 / MacroCommand 单步 Undo） |
| 模型不可用有清晰诊断 | 既有（转录 provider 失败降级 + 质量门 TAEP_QUALITY_GATE_FAILED）——不只返回编辑计划就称完成 |
| 跨 split/retime/嵌套支持范围准确 | 首版=平铺片段（帧对齐——F3.3 条件依赖后升采样级，计划原文允许）；复杂容器登记后续片 |

## 4. 登记的后续片（不阻塞验收）

1. UI：字幕 workspace 按词选中→生成删除计划（审批链入口）。
2. review_hits 的人工排除界面（检测已分离）。
3. 跨 split/retime/嵌套片段的区间映射（F3.3 采样级联动）。
