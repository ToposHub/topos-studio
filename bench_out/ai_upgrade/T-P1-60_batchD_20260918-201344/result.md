# T-P1-60 批次 D：MODEL_UNAVAILABLE evaluator 级结构化返回 + 节点级 performance attribution

- 执行日期：2026-09-18；环境：无头 GL 4.1 fragment fallback（AMD Radeon RX 6800）
- 关联：T-P1-60 卡步骤"验证"（ai_generate 缺模型返回 MODEL_UNAVAILABLE）与步骤 6
  （performance attribution）；批次 C 见 `T-P1-60_batchC_20260918-191858/`

## 一、MODEL_UNAVAILABLE 结构化返回（不再无解释 None）

- 求值器每轮重置诊断收集（`base_evaluator._evaluate_snapshot`）；AI 契约占位节点
  触发 `{"code": "MODEL_UNAVAILABLE", "node_id", "node_type"}`。
- 经 `_finish_trace` 合入 `ExportResult.diagnostics`；`CompositeFrameRenderer`
  暴露 `last_diagnostics`（出图成功/失败路径都置值）。
- bench `classify_node_result` 结构化诊断优先于 None/报错文本启发式。

**顺带修复真实缺陷**：`AIGenerateNode` 从未定义 input 端口——快照按端口名校验
连接，上游连接全被丢弃 → P0-3 占位直通从未生效 → ai_generate 是矩阵里唯一
裸 None 行。补 `Port("image", "input", "image")` 后与其余 AI 占位节点
（ai_inpaint/ai_expand/ai_denoise 均有 inputs、直通正常）行为一致。

回归：`tests/composite/test_model_unavailable_diagnostic.py` 3 项（真实 GL 管线）：
无输入 → 图无输出但诊断必须说明缺模型；有输入 → 直通出图 + 诊断共存；
诊断含 node_type 断言。

## 二、performance attribution（卡步骤 6）

- `ExportResult` 新增 `eval_ms`/`readback_ms` 字段（None=该路径未测，遵守
  P0-4"缺测量标 None 不标 0"规则）；`_render_full_frame` 拆分求值/读回计时。
- `_finish_trace` 将其写进 frame trace（此前 `eval_ms`/`readback_ms` 字段
  在 schema 里存在但**无任何 producer 填过**——本次补上真实测量）。
- `CompositeFrameRenderer.last_frame_trace` 暴露；bench 矩阵行新增
  `attribution_preview` / `attribution_export`（eval_ms、readback_ms、
  total_ms、transfer 字节、cache_hit、strategy、backend、node_count、
  readback_format）。

实测样例（blur@hd，1080p）：

| 阶段 | eval_ms | readback_ms | download |
|---|---|---|---|
| preview (srgb_u8) | 0.414 | 3.948 | 6,220,800 B（=H×W×3 u8） |
| export (linear_f32) | 0.759 | 43.571 | 24,883,200 B（=H×W×4 f32） |

cache_hit 该路径为 null（未接线，如实标 None）。

## 三、93 节点矩阵终态（0 failed 保持）

`by_status={"blocked_missing_model": 4, "blocked_missing_resource": 13, "passed_nonflat": 76}`

与批次 C（79/1/13/0）的差异全部是**诚实重分类**，无节点行为回归：

- ai_denoise / ai_expand / ai_inpaint 从 passed_nonflat（普通直通冒充）转为
  blocked_missing_model——直通图仍非平存活（mean=99.49），同时带结构化
  MODEL_UNAVAILABLE 诊断，说明 AI 功能不可用的真相。
- ai_generate 同类，且其直通首次真正生效（input 端口修复）。
- chroma_keyer（63.75）/ cryptomatte_select（127.5）保持 passed_nonflat。

中途曾出现 12×failed 的假回归——根因是 bench `render_frames` 丢了
"render returned None" 契约标记导致 classify 误判，已修复并重跑确认
（source/3D 节点回到 blocked_missing_resource）。

## 四、回归

- 渲染器邻接套件：test_frame_trace(6)/render_scheduler(36)/
  render_lifecycle_contract(4)/graph_evaluator_consistency(14)/
  composite_cache(47)/cache_strategy(25)/async_present_readback(27) 全过。
- tests/composite 全目录联跑存在预存段错误（干净树复现，与本次改动无关，
  与已知 tests/edit 段错误同类环境缺陷）；test_3d_preview_override 的
  test_switch_to_2d_keeps_3d_node_focus 在干净树同样失败（预存）。

## 遗留

- T-P1-60 全部卡步骤至此收口；桌面 Metal/GL4.3 compute runner 验证
  （X-TEST-10）仍限 BLOCKED: DESKTOP_GPU_NOT_VERIFIED。
