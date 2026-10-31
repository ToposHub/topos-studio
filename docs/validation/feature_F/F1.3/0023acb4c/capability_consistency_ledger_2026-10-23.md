# F1.3 · 统一能力声明一致性——处置台账

> 日期：2026-10-23；基线 commit：`0023acb4c`（feat/f-plan）。输入：F0.2 ledger R1–R9。

## 1. R1–R9 逐项处置

| 编号 | 问题 | 处置 | 证据 |
|---|---|---|---|
| R1 | FCPXML 静态表 True vs 注册表 PARTIAL 永久漂移 | 漂移判定语义修正：PARTIAL=受控子集已交付生产入口，与布尔 available=True **一致**，不计漂移（capability_registry.py find_status_drift 注释块） | TestF13Consistency::test_zero_drift_all_formats |
| R2 | 静态能力表零生产消费 | 定位为「人工评估台账 + 漂移检测输入」——注册表为单一事实来源（导出 manifest 消费），静态表仅作交叉校验源，语义已在两模块头部写明 | interchange.py 头注 + 注册表 docstring |
| R3 | interchange.py DORMANT 头注过期 | 重写头注：时间码 6 处生产调用 + BWF 生产写入 + FCPXML/OTIO File 菜单接线；无入口部分（CSV cue sheet）如实标注 | interchange.py:1-7 |
| R4 | CSV Cue Sheet 状态虚高（无生产入口却 AVAILABLE） | 注册表改 **DORMANT**（按其自身 available=有生产入口定义）；静态表同步 available=False | TestF13Consistency + test_csv_available |
| R5 | DORMANT 枚举无使用者/休眠模块不在册 | audio_health 列 DORMANT（实现+测试在、无生产消费者） | TestF13Consistency::test_audio_domain_entries_registered |
| R6 | loudness/录音ADR/音频图门不在册 | 三条 **AVAILABLE** 条目入册（含入口/测试证据）；Cue Sheet 降级后注册表从 12→16 条，音频域真实能力对导出 manifest/AI 可见 | 同上 |
| R7 | AI 侧另一套 capability_manifest 零引用注册表 | 如实登记为**两层设计**：编辑域能力唯一权威=capability_registry（导出 manifest 消费）；AI 域从 Effect/transition/codec 注册表生成自己的操作能力（语义不同，不合并）；交叉引用见本台账 | 本台账（不改代码——合并两层属 F9.x AI 联动时再评估） |
| R8 | EditPage.create_timeline 死发射 | **移除**死方法 + timeline_created 死信号（全库无 connect/无调用方）；真实创建路径=媒体池 timeline_create_requested→edit_controller（G4 驱动已验证） | edit_page.py:119/2578 注释；grep 零残留 |
| R9 | 跨序列粘贴按原轨 id 跳过 | **保留登记**（现行语义），处置归 F2.1（源/目的轨映射随 Source Patching 一并设计） | F0.2 ledger §5 |

## 2. 顺手修复的基线缺陷（pre-existing，stash 对照确认）

- `export_manifest._probe_output`：输出文件不存在时恒报 `present: True`（只带 hash_error）——"缺失输出被记为在场"。修复为存在性先行判定（不存在→present False；存在但 hash 失败→present True + hash_error）。`tests/edit/test_export_manifest.py::test_missing_output_recorded` 由红转绿（该测试在干净树上亦失败，非本批引入）。

## 3. 测试

- 新增 `TestF13Consistency`（零漂移全表断言 + 新条目状态/证据断言）。
- 契约更新：BWF writer 不可用→PARTIAL 降级不再计漂移（原 AUD-15700 测试改为断言降级语义）；FCPXML 欠承诺检测改 monkeypatch 静态表保留契约（AUD-15701）。
- 全量 F1.3 影响域：132 passed。

## 4. 未验证/边界

- Windows/DeckLink/Metal 行＝注册表既有 UNVERIFIED/评估条目，本批未改（B 线延期口径，见 F0.1 manifest §3）。
- 「即将推出」文案全量清点：U07（F1.1）已锁 linked_selection/show_markers；其余 UI 文案随 F2.x 各任务入口变化时同步。
