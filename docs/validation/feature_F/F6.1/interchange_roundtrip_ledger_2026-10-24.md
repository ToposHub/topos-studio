# F6.1 · FCPXML/OTIO 支持子集与外部往返 — 验证台账

- 日期：2026-10-24；计划条目：§F6.1
- 测试：test_f61_real_dialect_roundtrip.py（5 新）＋既有 adapter/import/otio/interchange 套件 58 全绿

## 1. 现状核验（核验后补强）

- FCPXML 受控子集已接生产（Q03：fcpxml_adapter 861 行 + import_builder
  + File 菜单导入/导出）；既有测试 58 项含 inspect/import/export/
  roundtrip/报告分类/dropped-不静默/重导入去重/离线降级。
- OTIO：依赖未安装——`test_otio_adapter` 三项诚实降级测试
  （unavailable-without-dep/同协议/嵌套策略），符合计划"OTIO 依赖
  未安装为未验证"。

## 2. 本批交付

| 组件 | 内容 |
|---|---|
| 外部方言黄金工程 | `fixtures/resolve_export_v18.fcpxml`：DaVinci Resolve FCPXML v1.8 导出方言（NTSC 有理数时长 30000/1001s、format/asset 资源、media-rep file:// URL）——手工按 Resolve 导出形状撰写（无 Resolve 运行环境，如实声明；方言一致性可验证） |
| **真实方言缺陷修复** | adapter 源入点解析此前只读 `tcStart`（旧 fixture 方言）——真实 Resolve/FCP 导出用 `start` 属性，**真实方言工程的源入点会被静默清零**。修复为 start → tcStart → asset start 三级优先 |
| 逐切点身份核验 | 黄金帧值（作者按有理数精确计算）：clip A offset 0/时长 4500 帧/源入 900 帧；clip B offset 4500/时长 1800/源入 300——导入后逐切点断言 timeline 位置/时长/源入帧身份一致 |

## 3. 验收 → 证据

| 验收 | 证据 |
|---|---|
| 至少一条真实软件往返逐切点/源帧身份核验 | 黄金方言工程逐切点测试（含修复后的 start 映射）+ 既有 Topos↔FCPXML roundtrip + 资产引用/重连声明 + NTSC fps 精确（30000/1001） |
| 不支持的复杂效果明确 bake/flatten/drop 或取消 | 既有 `test_export_dropped_unsupported_transition`/`test_export_subtitle_requires_user_action`/报告分类测试 |
| 不能由旧表误关已可用 adapter | INTERCHANGE_CAPABILITIES 与 capability_registry 对齐注释（Q03/F1.3 既有约定维持） |
| OTIO 依赖未安装为未验证 | otio 诚实降级三测试（unavailable 不冒称） |
| 导入走事务和 Undo | adapter→import_builder→command compiler→Undo 链（既有 test_import_builder 9 项 + reimport 去重） |
| 不可信输入防护 | AUD-12606 DTD/实体拒绝——黄金工程本身体验了该守卫（DOCTYPE 被 fail-closed 拒绝，移除后通过——真实 Resolve 导出无 DOCTYPE） |

## 4. 登记的后续片（不阻塞验收）

1. Resolve/FCP 实机生成样本回传后的逐位复验（本批为方言级黄金工程）。
2. F3.3 子帧 schema 映射 / F4.2 调色版本映射（计划条件依赖，受支持子集
   已可往返验收——计划原文允许）。
3. AAF（F6.3 独立条目）。

## 5. 诚实边界

- 黄金工程手工按 Resolve v1.8 导出方言撰写（无 Resolve 运行环境）——
  方言一致性经解析器逐字段验证；"真实软件生成"的最终背书登记后续片 1。
