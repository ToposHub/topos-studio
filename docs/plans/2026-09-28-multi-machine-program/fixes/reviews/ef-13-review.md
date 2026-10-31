# EF-13 复查（GLM5.3）—— src/features/composite/core 14 条 Suggestion

- 复查日期：2026-10-01
- 批次提交：`22e404c8ba81d275785c5323d22a847810ba7c75`（[fix-E ef-13] … (14)）
- 白名单（e_flash_batches.json modules 键）：`src/features/composite/core`
- 执行报告：fixes/reports/ef-13.md
- 复查方式：只读。git show/log/diff + 当前 HEAD 源码逐条独立核实 + 测试文件抽查。未运行 pytest，未执行任何 git 写操作。

## §6 八项检查结论

| # | 检查项 | 判定 | 说明 |
|---|--------|------|------|
| 1 | 越界 | **通过** | `git show --stat 22e404c8b` 共 14 文件：12 源文件全在 `src/features/composite/core/` + 1 报告（fixes/reports/ef-13.md）+ 1 新测试（tests/composite/test_ef13_aud.py，638 行）。未碰 REMEDIATION_PLAN.md 与白名单外文件（含 AUD-48006 涉及的 composite_page.py——正确地未触碰） |
| 2 | 真实修复（逐条） | **通过** | 14 条中 12 FIXED + 1 PARTIAL + 1 DUPLICATE，全部在当前 HEAD 独立核实（逐 ID 表见下）。两个非 FIXED 结论的证据链均独立验证成立（见下详述） |
| 3 | GLSL/HDR 红线 | **通过** | diff 新增行零裸 `except:`、零 `except Exception: pass`、零 moderngl/GL 调用、零 HDR 钳制、零 601/709 内容。本批纯 Python 数据结构/并发修复 |
| 4 | 并发 | **通过** | NodeSnapshot/ConnectionSnapshot 冻结（见 #2）；AUD-5001 经 `graph._lock` 读节点数（无锁测试替身走 getattr 回退，防御合理）；AUD-5213 单临界区 extend；image_loader 清扫在 mutex 内 |
| 5 | 资源 | **通过** | AUD-48010 被取消 worker 的 QThread 对象经入口清扫不再滞留；AUD-48009 DUPLICATE 回归测试锁定 `_zombie_workers` 保活契约（GC 不毁运行中 QThread） |
| 6 | 异常 | **通过** | 无新增 except；AUD-4702 迁移链容错以"视为无节点/回退 v1/不盖章"返回值语义处理，非吞错 |
| 7 | 测试质量 | **通过（附注）** | 45 用例。抽查 48015（3000 深链不炸递归）、48009/48010（Mock 注入行为断言）、4707（disabled/启用双向断言）、5213（计数锁 acquired==1）均为真行为断言。附注：4910/5001/5002/5110/5204 用源码哨兵（复杂度/docstring/死代码类无法行为化区分回退的场景），报告已披露 |
| 8 | 回退有效性抽查 | **通过** | 见"回退验证建议"节 2 条；flash 报告自带 3 条回退敏感性记录（4707/48006/5213） |

## 非 FIXED 结论的独立验证（重点）

### AUD-48006 PARTIAL（2/3 冻结 + 1/3 BLOCKED）——成立
1. `graph_snapshot.py:30` `@dataclass(frozen=True) class NodeSnapshot` ✓；`:95` `@dataclass(frozen=True) class ConnectionSnapshot` ✓。
2. GraphSnapshot 未冻结，docstring（:101-110）如实记录解冻条件 ✓。
3. BLOCKED 理由独立核实：`src/features/composite/ui/composite_page.py:5253`（`snapshot.connections = [`）与 `:5275`（`snapshot.eval_order = [`）确在创建后重绑定字段；该文件属 EF-03 白名单（composite/ui），不在本批白名单内——冻结会令 3D viewer 注入路径抛 FrozenInstanceError。全仓 grep `snapshot.connections =` / `snapshot.eval_order =` 仅此两处，NodeSnapshot/ConnectionSnapshot 无再赋值点。**PARTIAL 定性正确，是白名单纪律下的正确决策而非偷懒。**
4. 遗留：GraphSnapshot 补冻需 UI 侧（composite_page._inject_viewer3d_scene_snapshot 改重建快照）配合，建议中心登记后续批次。

### AUD-48009 DUPLICATE（已被 AUD-48007 修复）——成立
1. 所引 commit `3a14b7d0d`（[fix-A w2] fix 30 Critical findings across composite module）真实存在且早于本批。
2. 当前 HEAD `image_loader.py:739-779` cancel_all：锁外有界等待（w.wait(2000)）后**重新加锁**按 `isRunning()` 过滤重建 `_cancelled_workers`，超时者入 `_zombie_workers`——审计指控的"锁外 `clear()`"已不存在。
3. 新增回归测试 `test_cancel_all_keeps_running_worker_alive` 断言运行中 worker 必须留在保活结构（裸 clear 会使断言失败），锁定该语义。DUPLICATE 定性正确。

## 逐 ID 核实表（14/14）

| ID | 报告结论 | HEAD 证据（file:line） | 复查判定 |
|----|---------|----------------------|---------|
| AUD-4702 | FIXED | graph_migration.py:187-206 `_iter_nodes`/`_iter_connections` 容错助手（缺失/null/非 list/非 dict 成员均不崩）；:907-914 版本字段非 int/bool/负数回退 v1；:921-934 链中断 migrated=False 不盖章。13 处节点迭代 + 3 处连接迭代改走助手 | CONFIRMED |
| AUD-4707 | FIXED | asset_dependency.py:591-594 `if bool(node.get("disabled", False)): continue` 跳过禁用节点；启用节点空路径仍暴露 | CONFIRMED |
| AUD-48002 | FIXED | graph_preflight.py:683-698 `_group_mapping_value` isinstance 防护助手；:810、:830 两处同型访问改走助手（计划行 791/810/1105 漂移，实际当前 792/810 两处，报告披露属实） | CONFIRMED |
| AUD-48006 | PARTIAL | graph_snapshot.py:30 NodeSnapshot frozen、:95 ConnectionSnapshot frozen、:101-110 GraphSnapshot 解冻条件注释 | CONFIRMED（见上独立验证） |
| AUD-48009 | DUPLICATE | image_loader.py:739-779（见上独立验证） | CONFIRMED |
| AUD-48010 | FIXED（主体） | image_loader.py:690-696 request_decode、:734-735 cancel_for_node 入口持锁调用 `_cleanup_finished_workers_unlocked()`；并发上限子建议以"行为变更超 Suggestion 范畴 + 每节点至多 1 worker 天然上界"为由搁置——理由成立且已披露 | CONFIRMED |
| AUD-48015 | FIXED | layer_provenance.py:189-213 显式栈迭代 pre-order DFS，`reversed(incoming)` 入栈保持与递归版一致访问序 | CONFIRMED |
| AUD-4910 | FIXED | layer_stack_controller.py:2111-2125 恒 False 成员测试已删，`get_layer(cand)` 权威碰撞检查保留 | CONFIRMED |
| AUD-5001 | FIXED | layer_stack_validator.py:358-367 经 `getattr(self.graph, "_lock")` 读 `len(graph.nodes)`，无锁替身回退直读 | CONFIRMED |
| AUD-5002 | FIXED | layer_stack_validator.py:269-285 循环前 `stack_layer_id_set = set(stack.layer_ids)`，成员测试 O(1) | CONFIRMED |
| AUD-5110 | FIXED | node_engine.py:17 deque 导入；:1332-1340、:2000-2006 两处 Kahn `deque.popleft()`；:2503-2517、:2524-2538 环检测/下游标脏一次性邻接表 | CONFIRMED |
| AUD-5204 | FIXED | node_plugin.py:162-167 docstring 如实声明 discovery 无锁、限单线程 | CONFIRMED |
| AUD-5210 | FIXED | runtime_context.py:66-77 所有权/只读契约注释；:79-87 `__post_init__` clip_effects 归一 tuple（object.__setattr__ 因 frozen 上下文）。不深拷贝的理由（EffectInstance 可能引用不可拷贝 GPU 状态）合理且已披露 | CONFIRMED |
| AUD-5213 | FIXED | runtime_diagnostics.py:66-78 整批校验后单次 `with self._lock: extend`；非法整批不发布（语义更严格，测试锁定） | CONFIRMED |

## 红线发现

- **Critical：0**。无任何红线命中。

## 回退验证建议（供中心抽检，回退后对应 pytest 节点应失败）

1. **AUD-4707** — 文件 `src/features/composite/core/asset_dependency.py` 当前 ：591-594：
   ```python
           # AUD-4707: a disabled node is bypassed at render time, so its
           # assets (including empty required paths) are not collectable
           # dependencies and must not block the whole collection.
           if bool(node.get("disabled", False)):
               continue
   ```
   回退为删除这 4 行（旧行为：禁用节点照常收集）。应失败节点：
   `tests/composite/test_ef13_aud.py::TestAud4707DisabledNodesSkipped::test_disabled_nodes_are_not_collected`
   及 `::test_disabled_empty_path_does_not_block_collect_plan`
2. **AUD-5213** — 文件 `src/features/composite/core/runtime_diagnostics.py` 当前 ：70-78：
   ```python
           batch = list(records)
           for record in batch:
               if not isinstance(record, RuntimeDiagnostic):
                   raise TypeError("record must be RuntimeDiagnostic")
           with self._lock:
               self._records.extend(batch)
   ```
   回退为逐条循环加锁发布（旧行为：前缀已发布后才抛 TypeError、每条一次临界区）。应失败节点：
   `tests/composite/test_ef13_aud.py::TestAud5213PublishMany::test_invalid_record_publishes_nothing`
   及 `::test_single_critical_section_per_batch`

## 附加验证

- py_compile：本批 12 个源文件全部通过（与 EF-02 合并跑 44/44 OK）。
- 邻接测试声明（报告：tests/composite 定向 9 文件 213 passed）由中心统一复跑确认。

## 最终结论

**PASS**。12 FIXED + 1 PARTIAL（GraphSnapshot 冻结被白名单外 composite_page.py:5253/5275 阻塞，证据独立核实成立）+ 1 DUPLICATE（AUD-48007/commit 3a14b7d0 已修，证据独立核实成立）。零越界、零红线、测试质量良好。建议中心：
1. `check_fixes.py --mark "AUD-4702,AUD-4707,AUD-48002,AUD-48010,AUD-48015,AUD-4910,AUD-5001,AUD-5002,AUD-5110,AUD-5204,AUD-5210,AUD-5213" --commit 22e404c8b`；
2. AUD-48006 以 `--note "partial:NodeSnapshot/ConnectionSnapshot 已冻结;GraphSnapshot 被白名单外 composite_page.py:5253/5275 重绑定阻塞"`、AUD-48009 以 `--note "duplicate:AUD-48007@3a14b7d0 已修,回归测试锁定"` mark；
3. 统一跑 `tests/composite/test_ef13_aud.py` 确认 45 用例全绿；
4. 登记 GraphSnapshot 补冻为后续跨白名单事项（依赖 composite/ui 侧改为重建快照）。
