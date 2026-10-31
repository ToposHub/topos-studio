# F2.7 批量素材属性与解释媒体 — 证据台账

- 日期：2026-10-24
- 结论：**已完成（解释覆盖核心链 + 批量入口）**。属性面板多素材混合值
  批量编辑与 production info 批量、render cache 解释指纹登记后续片（§5）。
- 提交：`c2b568a88`。

## 1. 验收对照

| 验收点 | 落地 | 证据 |
|---|---|---|
| 区分原始探测与用户解释覆盖 | `InterpretationOverride`（fps）存 `interpretation_overrides`，探测 metadata 不改写；生效值 = 覆盖优先（`effective_asset_fps`） | test_interpretation_override.py::TestInterpretationModel |
| 批量修改支持项（帧率解释、声道映射） | 帧率：多选右键"批量解释帧率…"（宏单步撤销+逐项失败报告+代理任务失效）；声道映射 = F2.1 source_patch（已有全链，单素材源监视器入口） | ::TestBatchInterpretTransaction + F2.1 台账 |
| 重新解释同步相关 clip/timebase | `UpdateClipFpsInterpretationCommand`（DaVinci Use-frame-rate 语义：源帧窗不动、fps 快照改写、时长重算、fade 钳制、锁轨拒绝、宏注册）；提交后控制器提示应用到当前时间线 N 个片段（单宏单撤销） | ::TestClipReinterpretCommand（3 项） |
| 代理失效（不只改表格） | 解释命令 apply 清 proxy_path/status + 批量入口失效在途代理任务（`_invalidate_proxy_job`，与 relink 同语义）；undo 恢复代理快照 | ::test_apply_undo_roundtrip |
| 一次事务或逐项失败报告 | ProjectMacroCommand 单步撤销；缺失素材不入事务并进摘要报告 | ::test_missing_assets_excluded_with_report_semantics |
| 撤销/重开恢复 | 命令 undo 全恢复；settings 落盘/读回（含未知载荷保留） | ::TestPersistenceRoundtrip |
| 原始媒体不改写 | 只写项目级覆盖层，源文件零触碰 | 模型设计 |
| 失败不留下半更新 | apply 抛错不入栈（ProjectCommandStack 语义）+ 越界 fps apply 即拒 | ::test_invalid_value_rejected_no_partial_write |
| 大批任务可取消 | 解释为即时项目命令（无长任务）；大批异步任务（导入/代理/分析）取消模式已有（调研 §5） | 既有测试 |
| 新插入按解释生效 | `_resolve_source_fps_metadata` 与 insert 自动检测均走 effective fps（快照建在解释值上） | ::TestInsertConsumesEffectiveFps |

## 2. 关键决策

1. **重锚语义 = 保持帧数**（DaVinci "Use frame rate"）：源窗不动、时长按
   新解释重算（100 帧 25fps→24fps 时基：时间线时长 96→100@24fps），
   与"保持时长"（变速语义）严格区分。
2. **跨时间线单事务不存在**（timelines 为 ref，各时间线独立命令栈）：
   应用到片段覆盖当前时间线并如实提示其余时间线需各自打开应用。
3. **代理失效并入解释命令**（而非仅 UI 层）：命令是唯一提交路径，
   apply 内清代理保证脚本/AI 路径同样不串片。
4. **未知载荷识别收紧**：仅含 "fps" 键的 dict 才解析为覆盖，其余原样
   保留（防把任意 dict 吞成空覆盖）。

## 3. 测试账本

新增 tests/edit/test_interpretation_override.py：15 项。护栏：source_patch/
asset_log/log_ui/fps_propagation/time_domain/replace_media/bin_tree 85 项绿。

## 4. B 依赖核对

无 Plan B 依赖。

## 5. 登记的后续片

1. 属性面板多素材混合值批量编辑（复用 clip 级 tristate/-1 索引机制）；
   production info（Reel/Scene/Shot/Take）批量命令。
2. render cache 指纹纳入解释覆盖（现 fingerprint 读 asset.metadata，
   覆盖层变化不触发；`invalidate_render_cache_for_asset` 扩展到时间线
   clip 遍历）。
3. 色彩解释覆盖（color_metadata 用户覆盖层）。
4. 换源预检 fps 拒绝与解释路径的协调提示（解释≠换源的用户文案）。
