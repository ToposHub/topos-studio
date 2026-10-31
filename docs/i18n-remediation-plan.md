# UI 硬编码字符串 i18n 整改计划（DOC-001 补口）

- 日期：2026-09-29
- 来源缺口：REMEDIATION_PLAN 条目 DOC-001（Major/i18n-gap）——711 处 UI
  硬编码字符串未走 I18N
- 证据：`docs/i18n-audit-2026-09-28.md`（计划04 P4 执行产物：AST 扫描
  251 个文件 + locale 键完整性对比）
- 本文档性质：把审计结果转成可执行、可分批验收的整改计划。审计报告
  §1.6 已给出修复模式建议但声明"不在本次执行范围"，本计划补上执行口径。

---

## 1. 问题基线（引自审计 §1.3）

| 模块 | 问题条数（A+B可译+C） | 占比 |
|---|---|---|
| features/composite/ui | 357 | 50% |
| features/edit/ui | 163 | 23% |
| app/ui（含 script_panel 41） | 62 | 9% |
| features/color/ui | 62 | 9% |
| shared/ui/audio_mixer | 15 | 2% |
| features/deliver/ui | 15 | 2% |
| features/teaching_animation/ui | 15 | 2% |
| features/ai_workflow/ui | 13 | 2% |
| shared/ui(root)+widgets+app(non-ui) | 9 | 1% |
| **合计** | **711** | |

分类口径：A 中文直传 125（中文界面永远不随 locale 切换，最高优先）、
B 英文可译 538、C 中文待确认 48；B 英文技术 105 为信息项不计入。

另：251 个文件中 96 个完全未接翻译入口，其中含大量文案的面板（整面板
零 i18n）：`import_report_panel.py`（34 处）、`material_panel.py`、
`asset_dependency_panel.py`、`workflow_panel.py`、`ai_preferences_page.py`。

## 2. 整改批次与优先级

按"用户可见影响 ÷ 改动面积"排序，A 类（中文直传）一律先于同文件 B 类：

| 批次 | 范围 | 条数（约） | 验收口径 |
|---|---|---|---|
| R1 | 整面板零 i18n 的 5 个面板（composite/ui 4 + edit/ui 1） | ~110 | 各面板零硬编码（复扫该文件为 0） |
| R2 | composite/ui 其余文件（node_editor_widget 90、node_properties_panel 58、composite_viewport 33 等按 Top10 顺序） | ~250 | 同上 |
| R3 | edit/ui（timeline_items 38、ai_edit_assistant_panel 25、caption_workspace 20、relink_media_dialog 19 等） | ~165 | 同上 |
| R4 | app/ui（script_panel 41 为首）+ shared/ui root 的 A 类 4 处 | ~65 | 同上 |
| R5 | color/ui 62 + audio_mixer 15 + deliver/ui 15 + teaching_animation 15 + ai_workflow 13 | ~120 | 同上 |
| R6 | C 类"待确认"清单逐条判定（确属 UI 文案的迁入，其余注明排除理由） | 48 | 判定记录留档 |

每批次要求：
1. 同文件 A/B 同批迁移，避免二次返工（审计 §1.6 建议）。
2. 新 key 遵循既有命名空间（`composite.*`、`edit.*`、`script.*` 等）；
   新面板新增子命名空间。
3. 迁移后以审计同口径 AST 扫描复扫该文件，问题计数归零方算完成。
4. locale 侧：zh_CN 与 en_US 必须同批补齐；其余 8 个 locale 可后补
   （I18N 有 en_US 回退，且键集合完整性已在审计第二部分统一到 1785 键）。

## 3. 统一迁移模式

```python
# 文案
label.setText(I18N.tr("ns.section.title", "Default Text"))
# 带格式（占位符在 tr 之外 format，或参照 _trf）
msg = I18N.tr("ns.msg", "Value: {v}").format(v=amount)
```

- 默认文案一律英文（与既有 locale 文件基准一致）。
- 禁止把中文写进默认参数（那只是把 A 类换成 B 类的反向问题）。
- 纯技术/品牌词（codec 名、单位、`dBFS` 等，审计归类 B 英文技术）不迁移。

## 4. 排除与已知不处理项

- locale 键完整性（审计第二部分）已完成：8 个非完整 locale 各补 209 键，
  10 个 locale 非 plugins 键 1785 两两一致。剩余问题仅 `plugins.*` 的
  翻译内容覆盖（plan-03 口径，另跟踪）。
- 日志/异常消息/assert/docstring/CSS-QSS 载荷不计入（审计排除规则 §1.1）。

## 5. 进度跟踪

| 批次 | 状态 | 完成批注 |
|---|---|---|
| R1 | 未开始 | |
| R2 | 未开始 | |
| R3 | 未开始 | |
| R4 | 未开始 | |
| R5 | 未开始 | |
| R6 | 未开始 | |

每完成一批：更新本表 + 在对应 PR/变更中引用审计 §1.4 的文件定位复扫证据。
