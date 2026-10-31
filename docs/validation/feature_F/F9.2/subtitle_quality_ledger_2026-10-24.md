# F9.2 · 多语言字幕、AI 质量门与可恢复批处理 — 验证台账

- 日期：2026-10-24；计划条目：§F9.2
- 测试：新增 test_f92_translation_qc.py（4）＋既有字幕/QC/gate 套件
  （caption export/importer/quality/delivery、delivery_quality_gate、
  quality_gate/execution_policy/artifact_store）全绿

## 1. 现状核验（调研结论——地基厚实）

- **六格式**：srt/vtt/ass/ttml/dfxp/scc 导出（帧→毫秒/厘秒 half-up；
  SCC CEA-608 + 29.97 DF；TTML 时钟时间+xml:lang；ASS \k 卡拉OK）+
  导入（TTML tickRate/frameRate 推导、非 media timeBase 拒绝）；
- **字幕 QC**：`inspect_subtitle_track`（行长 42/行×2 行、阅读速率
  CJK+拉丁混算 20 units/s、时长、空文本、越界、重叠 error）+
  交付级 `evaluate_delivery_quality`（六指标 blocking 硬门）；
- **AI 质量门**：`evaluate_quality_gate`（硬门槛+软 KPI+
  TAEP_QUALITY_GATE_FAILED）+ `ExecutionPolicy` 三档 + `ArtifactStore`
  乐观锁；
- **翻译**：`build_translated_subtitle_track`（AI provider→新轨
  TRANSLATED 状态+语言元数据+可撤销命令）；
- **竖屏安全区**：1080×1920 vertical_social profile 拒绝测试（最终
  renderer 契约测 bbox）；burn-in 预览/导出共用渲染核（parity 测试）。

## 2. 本批交付（缺口补齐）

| 组件 | 内容 |
|---|---|
| 翻译后 QC | `translation.py::qc_translated_track`：翻译产物此前**无再检查**（目标语言行长/阅读速率可能与源语言差异巨大）——现在翻译轨自动过 `inspect_subtitle_track`，返回 (issues, all_pass[error 级判定])；warning 级保留人工判断（行长/速率跨语言差异） |

## 3. 验收 → 证据

| 验收 | 证据 |
|---|---|
| 多语言/逗号/标签/换行、ASS/TTML 时间基 | 既有六格式往返测试族（含 23.976/29.97 时间基） |
| 行长/阅读速率/重叠 QC 覆盖 | 既有 inspect + **新增翻译后 QC 4 项**（干净通过/行长检出/越界 error→fail/速率检出） |
| burn-in/sidecar 取正确语言/版本 | 既有分轨 sidecar 导出（SubtitleDeliveryReport）+ 翻译轨语言元数据 |
| 16:9→9:16 字形/断行/安全框 | 竖屏 safe-area profile 既有（bbox 拒绝测试）；画布变换自动重排登记后续片 |
| 质量门有来源证据 | 既有（硬门槛量化 + TAEP_QUALITY_GATE_FAILED 结构化） |
| 重试不重复 cue | 翻译命令可撤销 + 幂等（cue 级 id）；AI 任务 revision 约束（F9.1 同机制） |
| 不自动开启付费模型/外部发送 | 既有授权流程维持（provider 注入式） |

## 4. 登记的后续片（不阻塞验收）

1. 16:9→9:16 画布变换字幕自动重排/缩放适配函数（safe-area profile
   已有，适配函数+像素级验证一片）。
2. 翻译后自动断行（warning 级跨语言差异的自动处理——现保留人工）。
3. DeliveryQualityResult 接入 workflow run_summary 可见载荷。
