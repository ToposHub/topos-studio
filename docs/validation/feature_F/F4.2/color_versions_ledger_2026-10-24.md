# F4.2 · 命名调色版本与共享 look 锁定 — 验证台账

- 日期：2026-10-24
- 计划条目：docs/feature_plan_F_davinci_2026-10-22.md §F4.2
- 测试：tests/edit/test_color_versions.py（14）＋回归全绿
  （color stack 40/组 13/切换持久化/source patch 消费/save router/
  schema v3/archive/interpretation/audio sync 共 300+）

## 1. 交付

| 组件 | 内容 |
|---|---|
| 命名版本 | `color_versions.py`：`SaveColorVersionCommand`（完整 v2 栈快照 + `plugin_versions` 清单 + saved_at；重名拒绝/replace 覆盖）＋ `ActivateColorVersionCommand`（**激活=内容互换**，身份字段 stack_id/scope/timeline_id/clip_id 保留——引用/共享/F4.1 组链全部稳定；undo 单步恢复内容+active 指针）＋ `DeleteColorVersionCommand`（删版本不动栈内容；active 指向被删版本时清指针） |
| 共享锁 | `SetStackLockCommand` + `lock_shared_stacks`（多镜头引用/asset_default 默认锁）+ `shared_stack_ids`/`stack_dependent_clips`（影响范围预览）；`UpdateColorStackCommand` apply 头部强制：锁定栈整体拒绝（ValueError→面板提示），解锁或 MakeLocalGradeCopy（既有命令）是唯一改 look 路径；版本管理命令不受锁限制 |
| 持久化 | `EditProject.color_stack_versions` + IO settings 键（旧工程无键=空表；roundtrip 精确含 active/locked/versions） |
| 插件缺失 | 版本载荷带 `plugin_versions`；激活时比对注册表得 `missing_plugins`（注册表不可用时保守为空，不冒称）；数据永不因缺失丢弃 |

## 2. 验收 → 证据

| 计划验收 | 证据 |
|---|---|
| 同镜头 A/B/C 一键比较/回退 | `test_save_activate_abc_compare_and_revert`（三版本激活逐一比较；undo 单步回退前一版本内容与 active 指针） |
| 跨镜头共享变化的影响范围可预览 | `test_impact_preview_dependent_clips`（stack_dependent_clips 返回 (timeline, clip) 全集；单镜头 trim 只列自身） |
| 复制/删除版本、Undo、保存恢复一致 | 命令事务 4 项（重名拒绝/replace/删除/冲突拒绝零半写）+ IO roundtrip（含旧工程空表） |
| 共享 look 默认防误改，显式解锁/复制独立版本 | `test_shared_stacks_detected_and_locked_by_default` + `test_locked_stack_refuses_direct_edit` + `test_unlock_then_edit` + `test_lock_does_not_block_version_management` |
| 切换不覆盖其他镜头私有修改 | `test_version_switch_preserves_private_trims`（版本激活后 F4.1 微调栈逐镜头原样） |
| 非当前序列导出取正确版本 | `test_active_version_content_reaches_export_resolution`（激活=内容互换，`_get_clip_color_grade` 读栈即读激活版本） |
| 插件缺失不丢版本数据，明确不可渲染原因 | `test_version_payload_carries_plugin_manifest`（载荷保留 layers+清单；missing_plugins 计算不阻断、失败保守为空） |
| 工程级版本留给 F8.1 | 未实现工程快照（登记 F8.1 联动） |

## 3. 顺手修复的 pre-existing 缺陷（本轮实测发现，均较严重）

1. **`EditProject.update_color_stack`/`delete_color_stack` 字段丢失**：显式
   `EditProject(...)` 构造器未携带后加字段——**任何调色编辑及其 undo 会静默
   抹掉 `source_patch_overrides`（F2.1）/`interpretation_overrides`（F2.7）/
   `audio_sync_bindings`（F2.10）**。已改 `dataclasses.replace`（AUD-16200
   同语义）。工程内其余 40 处显式构造器同类风险登记后续片统一清理
   （本轮审计尝试全量改写时发现文本手术风险过高，回退为两处精确修复）。
2. **IO 转换器 Rational fps TypeError**：`TimelineSequence.fps` 是 Rational
   （无序比较），`TimelineRef` 校验 `fps <= 0` 直接 TypeError——任何带时间线
   的工程经 `convert_edit_project_to_topos_project` 崩溃。已 `float()` 化。
3. UpdateColorStackCommand 锁定 enforcement（本为 F4.2 交付项，列出以示
   唯一写路径收口）。

## 4. 登记的后续片（不阻塞验收）

1. Color 页 UI：版本列表/一键比较/锁定徽标（模型/命令已就绪）。
2. `edit_controller._on_use_shared_grade_requested` 分享后自动 lock（当前
   需显式 lock_shared_stacks；一行接线，随 UI 片落地）。
3. 工程内其余显式 EditProject 构造器的 replace 化清理（40 处，审计后统一）。
4. 工程级快照版本（F8.1 联动，计划原文即如此划分）。

## 5. 诚实边界

- 版本激活是内容互换：版本历史不是增量 diff（完整快照，存储换简单性）。
- `missing_plugins` 只对注册表健康检查可见的缺失生效；注册表不可用时不
  冒称缺失（保守空表），不可渲染原因在 UI/预检消费该字段时展示。
