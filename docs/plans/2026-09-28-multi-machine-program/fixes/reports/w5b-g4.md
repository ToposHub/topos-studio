# 修复报告 w5b-g4 — 批 A（Critical × 2）

- 日期：2026-09-29
- 证据报告：docs/audit/2026-09-28/batches/batch-166-features-edit-core.md、batch-220-features-ai_harness.md
- 改动文件（生产代码全部在白名单内）：
  - `src/features/edit/core/edit_gpu_preview_renderer.py`
  - `src/features/edit/core/timeline_preview_renderer.py`
  - `src/features/ai_harness/mcp_outbound.py`
- 新增测试：
  - `tests/edit/test_fix_w5b_aud5b.py`（AUD-16600）
  - `src/features/ai_harness/tests/test_fix_w5b_aud5b.py`（AUD-22020，ai_harness 既有测试惯例为 in-source `src/features/ai_harness/tests/`）
- 因锁定旧行为而更新的既有测试（3 个文件，见各条目"测试影响"）：
  - `src/features/ai_harness/tests/test_mcp_outbound.py`
  - `src/features/ai_harness/tests/test_p3_red_lines.py`
  - `tests/edit/ai_editing/test_mcp_vision_gateway.py`

## 逐条结论

| 编号 | 结论 |
|------|------|
| AUD-16600 | **FIXED** |
| AUD-22020 | **FIXED** |

---

## AUD-16600 [Critical/bug] adjustment_layers 生产方为 3 元组，scoped 合成按 2 元解包

**状态：FIXED**

**根因**：生产方 `timeline_preview_renderer.py:2080`（T-P1-22 之后）按三元组
`(ClipLayerInfo, effects_tuple, preview_grade)` 下发调整剪辑，而消费方
`_build_scoped_composite_work` 仍按 FX-C02 时期的二元组
`for adj_layer, effects in adjustment_layers:` 解包——含调整剪辑的 GPU 帧在
`_process_task → _composite_with_adjustment_scope → _build_scoped_composite_work`
必然抛 `ValueError: too many values to unpack`，被 `run()` 捕获后每帧
`render_error`，GPU 呈现链对该类时间线整体失效。次生问题：即便解包正确，
`_composite_with_adjustment_scope` 两处调用点只回传 `[(adj_layer, effects)]`，
grade 会被静默丢弃（`_apply_adjustment_clips` 本身已按三元组兼容写好，只是
从未收到过第三元）。

**修复**（形状契约对齐生产方三元组；未引入 namedtuple/dataclass，因
`_apply_adjustment_clips`（4506-4509 行）已确立"三元组 + 旧二元组兼容"的
访问式契约，统一结构体属更大的重构，超出本条根因修复范围）：

1. `edit_gpu_preview_renderer.py` `_build_scoped_composite_work`：解包改为
   `adj_entry[0]/[1]/[2 if len>2 else None]`（与 `_apply_adjustment_clips` 同款
   兼容写法），work item 由 5 元扩为 6 元
   `(adj_idx, 1, 'adjustment', adj_layer, effects, grade)`，grade 随 work 下发；
   docstring 写明三元组契约与二元组兼容。
2. `_composite_with_adjustment_scope` 两处 `_apply_adjustment_clips` 调用点
   （首项调整分支 + 循环调整分支）改为透传完整三元组
   `[(adj_layer, effects, grade)]`，grade 不再丢弃；首项分支的执行条件由
   `if effects:` 改为 `if effects or grade_first:`（grade-only 的调整项原为
   静默 no-op，现在与循环分支语义一致）；docstring Args 同步三元组契约。
3. `EditRenderTask.adjustment_layers` 注释更新为真实三元组契约（原注释声明
   二元组，与生产方不符——即 AUD-16611 指出的过期注释在本字段上的实例）。
4. 生产方 `timeline_preview_renderer.py:1999` 的局部类型注解由
   `list[tuple[ClipLayerInfo, tuple]]` 修正为
   `list[tuple[ClipLayerInfo, tuple, Optional[Dict[str, Any]]]]`，与 2080 行
   实际 append 的形状一致。

**测试影响**：`tests/edit/test_gpu_adjustment_scope_composite.py`（`_adj` 只产
二元组）全部通过——二元组兼容是有意保留（消费方按"长度取值"而非严格解包），
非锁定旧行为。

**验证**：`tests/edit/test_fix_w5b_aud5b.py` 8 项——生产方三元组形状不再
ValueError、`None` grade、旧二元组兼容、work item 携带 grade、z 序（FX-C02
scope）不因三元组改变、scoped 合成（桩 self，无 GL）在首项调整与循环调整两
条路径把三元组原样透传给 `_apply_adjustment_clips`、二元组透传为
`(layer, effects, None)`。

**协调提示（重要）**：AUD-16603（调整剪辑 grade 用槽位索引 `1000 + adj_index`，
`_ensure_graded_track_fbo` 会顺序创建 1001 张全尺寸纹理）此前被本缺陷掩盖
（ValueError 先抛、分支不可达），本次修复后**已可达**。scoped 合成每次只回传
单条调整项（`adj_index` 恒 0 → 恒用索引 1000），带 ColorStack 的调整剪辑首帧
即触发该分配。AUD-16603 在 REMEDIATION_PLAN 有独立条目，应由其所属批次按
"专用 scratch/按需 dict"修复；本批次不在白名单外扩改该分配策略，避免跨批次
冲突。

## AUD-22020 [Critical/bug] 写审批闸门使用晚发现 manifest 前计算的 is_write

**状态：FIXED**

**根因**：`mcp_outbound.py` `call_tool` 在 manifest 未缓存（会话首调）时先用
`tool_spec=None` 计算 `is_write`（只剩名字启发式 + profile scope），随后才补
`discover()` 并更新 `tool_spec`，但 `is_write` 不重算——注解标记为写、名字不含
write/create/update/... 启发词的工具（如 `rename_clip`）在
`requires_approval_for_writes=True`（默认）、`write_approved=False` 下被直接执
行，且后续审计事件 `write_operation` 沿用旧 False。

**修复**（`call_tool` 授权链重排，`_is_write_operation` 语义不变）：

1. 保留 discovery 前的启发式**预闸**：名字启发式/profile 判写的调用在触发任
   何远程请求之前即拒绝（原设计不变式"A denied write must never cause a
   remote process or HTTP request" 对启发式可判写的部分原样保留）。
2. 补发现后基于**最终工具注解**重算
   `is_write = self._is_write_operation(profile, tool_name, tool_spec)` 并**重走
   审批闸门**（denied 则返回，绝不到达 tools/call）——注解标记的写不再因
   manifest 晚到而绕过审批；后续 `_record(..., write=is_write)`（rate-limit、
   远端错误、allowed 等全部审计点）随之使用最终值。
3. **fail-closed**：发现失败（manifest/工具 spec 拿不到）→ 写属性无法确定 →
   `is_write = True` 按写处理——无审批即拒，不再用 discovery 前的启发式缓存
   值放行未知工具（同时收窄了 AUD-22021 fail-open 场景的写维度暴露面；schema
   校验维度的 AUD-22021 仍属其自身条目）。
4. 拒绝分支收敛为局部 `_deny_write()`（带类型注解），类 docstring 红线段补记
   新契约。

**测试影响（锁定旧行为，按协议更新，3 个文件）**：

- `src/features/ai_harness/tests/test_mcp_outbound.py`
  - `test_transport_error_recorded`：原断言"crash transport 下 get_x →
    error"，依赖的是 fail-open（发现失败仍放行到 transport）。更新为：无审批
    → `denied_write_needs_approval`（fail-closed 新契约）；`write_approved=True`
    → 仍到达 transport 并归审计为 `error`（原测试意图保留）。
  - `test_no_transport_factory`：同型更新（无 transport → 发现失败 → 先拒；
    获批后 → `error`）。
- `src/features/ai_harness/tests/test_p3_red_lines.py`：`_T` fake 原对
  tools/list 返回 tool-call 结果（discover 判为 legacy 响应 → 发现失败 → 旧
  代码 fail-open 放行）。更新 `_T` 像真实 server 一样应答 tools/list（get_x
  声明 readOnlyHint，write_data 无注解）——红线测试意图（写需审批、输出只作
  数据）不变，全部通过。
- `tests/edit/ai_editing/test_mcp_vision_gateway.py`：`_FakeTransport` 原不实
  现 tools/list（7 项路由测试依赖 fail-open 放行）。更新为应答 tools/list，
  返回网关全部 MCP 工具（analyze_image / analyze_video /
  extract_text_from_screenshot / ui_diff_check / web_search_prime / webReader）
  且声明 `readOnlyHint: true`——与真实 server 行为一致（网关工具 schema 本就
  来自实测 tools/list）；39 项全部通过。

生产影响评估：网关三个 profile 均为 read_only scope；真实 Zhipu server 均支持
tools/list（发现成功 → 注解/名字判读 → 网关只读工具不受影响）；只有 server
不可发现时网关调用才会要求写审批（fail-closed 的预期代价，替代旧的静默放行）。

**验证**：`src/features/ai_harness/tests/test_fix_w5b_aud5b.py` 7 项——
`readOnlyHint=False` + 无启发词 → 无审批拒（且只发生 tools/list、绝无
tools/call）、`destructiveHint=True` 同、获批后放行且审计
`write_operation=True`、readOnlyHint=True 只读工具首调仍免审批、拒绝事件带
write 标记、发现失败 fail-closed、启发式预闸在 discovery 前拒绝且远程零请求。

---

## 验证记录

- `.venv/bin/python -m py_compile`（逐个改动文件）：
  - `src/features/edit/core/edit_gpu_preview_renderer.py` → OK
  - `src/features/edit/core/timeline_preview_renderer.py` → OK
  - `src/features/ai_harness/mcp_outbound.py` → OK
- 新建测试：`tests/edit/test_fix_w5b_aud5b.py`（8 passed）+
  `src/features/ai_harness/tests/test_fix_w5b_aud5b.py`（7 passed）= **15 passed**。
- 邻接既有测试：
  - `tests/edit/test_gpu_adjustment_scope_composite.py` +
    `test_timeline_preview_renderer.py` + `test_edit_gpu_pipeline.py` +
    `test_color_stack_preview_grade.py` + `test_adjustment_clip_scope.py` +
    `test_adjustment_transform_opacity.py` + `test_gpu_transition_preview.py` +
    `test_clip_blend_modes.py` → **211 passed**。
  - `src/features/ai_harness/tests/`（全部 24 个文件，含更新后的
    test_mcp_outbound / test_p3_red_lines）+ `tests/edit/ai_editing/test_mcp_vision_gateway.py`
    → **595 passed**。
  - `tests/shared/test_dctl_and_preview.py` + `tests/edit/ai_editing/`（全部）→
    **2044 passed, 4 skipped**。
  - `tests/edit/`（全量）→ **8485 passed, 18 skipped, 1 failed**。

## 已知无关失败（非本批引入）

`tests/edit/test_plugin_identity.py::TestEffectInstancePluginIdentity::test_catalog_plugin_metadata_reaches_add_effect_command`
失败：`capabilities` 实际为 `["gpu_preview", "export_opengl"]`，断言仅
`["gpu_preview"]`。该行为源自 `src/features/edit/core/…/effect_catalog.py:567-572`
（已提交代码，DCTL 插件同时授予 gpu_preview + export_opengl），属 shared-effects
审计域（batch-263，AUD-26307 相关区域），与本批 3 个改动文件无导入/数据关联，
单测隔离运行同样失败 → 判定为**先在失败**，留待对应批次处理。

## 遗留与移交

- **AUD-16603 现已可达**（见 AUD-16600 条目"协调提示"）：带 ColorStack 的调
  整剪辑会走 `_ensure_graded_track_fbo(1000)` 的 1001 张纹理分配路径，需其
  所属批次尽快按"专用 scratch 资源"修复。
- AUD-16601/16602/16604/16605/16611、AUD-22021/22022 等同批次报告内其余发现
  未在本批范围内改动。
