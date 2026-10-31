# T-P2-20/21/22 执行报告：AI 控制面（批次一）

- 执行日期：2026-09-18；状态：均 **IN_PROGRESS**

## T-P2-22（AI 能力与真实性联动）本批次

1. **条目 2（计划器禁止不可交付动作）**：plan_validator 新增
   EFFECT_NOT_DELIVERABLE（TAEP_EFFECT_NOT_DELIVERABLE）——SET_EFFECT
   目标为 preview_only/unsupported 效果时计划期即拒绝，诊断 details 携带
   {effect_type, restriction=plugin_preview_only, reason, suggestion}，
   规划器可据此改提替代方案（卡要求"替代计划必须可解释"的数据基础）。
   测试：test_preview_only_effect_rejected_as_delivery_step。
2. **条目 1（get_capabilities 真实性）**：已由 T-P1-20 落地——manifest
   effects 携带 exportable/preview_only/unsupported/delivery_restriction
   （与 Deliver preflight/UI 同一 EffectExecutionCapability 真相源）；
   T-P1-60 六态矩阵补齐 blocked_missing_model/blocked_missing_resource
   的节点级解释。
3. **条目 4（AI 验证不得只读 success=true）**：底座在位——ExportOutcome
   携带 stage_telemetry（9 阶段 + full_gpu_blockers 机器可读）、
   delivery_path、native_encode_stage_stats；finalize_export_with_probe
   将其并入 AI 结果 manifest。批次二补端到端断言。

## T-P2-20 / T-P2-21 本批次

- 底座盘点在位：ToolFacade（get_composite_snapshot / validate /
  apply_composite_edit + host ApprovalGrant 强制）、EditPlan/operation
  schema（operation_id/params/undo 经 MacroCommand）、DeliveryCapability
  （T-P1-70）、WorkflowTransaction 所需的 revision/capability snapshot
  锚点（ApprovalScope 已绑 project_id+timeline_id+base_revision+plan_hash）。
- **遗留（批次二+）**：T-P2-20 全域操作 schema 矩阵（operation_id/
  idempotency_key/preview_diff/verify/compensation 字段级契约）；
  T-P2-21 WorkflowTransaction 显式状态机（QUEUED→PREFLIGHT→RUNNING→
  VERIFYING→终态）与三种失败策略的集中实现。

## 验证

- tests/edit/ai_editing/test_taep_contracts.py 40/40（含新增拒绝场景）。
- deterministic fake LLM × 20 场景回放（卡验证要求）属批次二。
