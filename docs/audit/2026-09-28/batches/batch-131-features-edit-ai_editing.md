# 批次报告：batch-131 [P1] src/features/edit/ai_editing/

- 编号区间：AUD-13100–AUD-13199（实际使用 AUD-13100–AUD-13117，共 18 条）
- 统计：Critical 0 / Major 4 / Suggestion 14
- 审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失

---

## src/features/edit/ai_editing/audit_store.py (行数: 176)

### 发现

- [AUD-13100] [Suggestion] AuditStore 全程无锁，与同包 mission/checkpoint store 的线程安全约定不一致
  - 位置: src/features/edit/ai_editing/audit_store.py:100-137
  - 证据:
    ```python
    def record(self, entry: AuditEntry) -> None:
        # 内存记录始终成功（即使落盘失败也保留内存条目，供 UI/审计查询）。
        self._entries.append(entry)
        if len(self._entries) > self._max_entries:
            del self._entries[:-self._max_entries]
    ```
    对比同包 autonomous_mission.py:120 / autonomous_recovery.py:108 均有 `self._lock = RLock()` 并在所有读写路径加锁；本类的 `list_entries/get/clear/export_all` 也均裸访问 `self._entries`。
  - 影响: 当前调用方（ai_edit_controller.py，主线程信号槽驱动）暂未并发访问，但 runtime 状态事件路径一旦演进到非主线程触发 record，`del` 切片与 `export_all` 的列表推导并发会产生漏记/重复条目，或 `restore_autonomous_audit` 的 clear+import 与 record 交错丢条目。属潜伏数据竞争，而非现行缺陷。
  - 建议: 仿照同包兄弟 store 加 `threading.RLock` 覆盖 record/list_entries/get/clear/export_all/import_all。
  - 置信度: 中

- [AUD-13101] [Suggestion] record 的宽泛 `except Exception` 会把 JSON 序列化失败误标为"路径越权"，且 safe_filename 碰撞时静默丢审计文件
  - 位置: src/features/edit/ai_editing/audit_store.py:114-128
  - 证据:
    ```python
    if not fpath.exists():
        fpath.write_text(
            json.dumps(entry.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except (OSError, ValueError) as exc:
        ...
    except Exception as exc:  # PathPolicyError 等：路径越权
        logging.getLogger(__name__).warning(
            "audit path rejected for plan %r: %s", entry.plan_id, exc
        )
    ```
  - 影响: `json.dumps` 在 try 块内，若 `plan_snapshot` 含不可序列化对象抛 `TypeError`，会被最后一个 `except Exception` 捕获并记为 "audit path rejected"，误导排障方向（与 .clinerules"不得宽泛 except 掩盖领域错误"相悖）。另外 `if not fpath.exists()` 的"不覆盖"策略叠加 `safe_filename` 归一化：两个不同 plan_id 规范化到同名文件时，第二条审计记录永远不落盘且无任何日志。
  - 建议: 将 `json.dumps` 移出 try 或单独捕获 TypeError 记为序列化失败；对"文件已存在但 plan_id 不同"的情况记一条 debug/warning 日志。
  - 置信度: 高

### 通过项
- 输入验证：from_dict 对所有字段做类型收敛与长度截断，非法行由 import_all 跳过且不阻断工程加载
- 路径安全：落盘经 safe_filename 规范化 + SafePathPolicy.resolve_within 双重防护，杜绝路径穿越
- 异常降级正确：审计 I/O 失败仅 warning，内存条目保留，不会把已成功的编辑误报为失败
- 持久化一致性：schema_version 校验、有界（max_entries）、export 前统一 sanitize_for_storage
- GPU/GLSL/信号防递归维度不适用（纯逻辑模块，无 Qt、无 OpenGL）

---

## src/features/edit/ai_editing/autonomous_agent.py (行数: 415)

### 发现

- [AUD-13102] [Major] rollback_ai_command 两次调用均未包裹异常处理，破坏 apply "总是返回结构化结果"的契约
  - 位置: src/features/edit/ai_editing/autonomous_agent.py:287-293, 324-342
  - 证据:
    ```python
    if new_revision <= current_revision:
        rollback = self._host.rollback_ai_command(
            timeline_id,
            expected_undo_label=undo_label,
            expected_revision=new_revision,
        )
    ```
    对比 commit 路径 268-283 行整体包在 `try ... except Exception as exc: return self._blocked(plan_id, "AUTO_COMMIT_EXCEPTION", str(exc))` 中；325-329 行的后置校验失败回滚同样裸调用。
  - 影响: host 回滚抛异常（如 undo 栈标签不匹配）时异常直接逃出 `apply()`。调用方 ai_edit_controller.py:5660-5664 虽有兜底 except，但结果退化为通用 `AUTO_APPLY_EXCEPTION`，丢失 `rollback_failures`、`AUTO_VERIFY_FAILED`/`AUTO_POST_VERIFY_FAILED` 等结构化诊断——而此时时间线恰处于"已提交但校验失败"的最需要精确记录的状态，mission 台账与审计记录均失真。
  - 建议: 两处 rollback 调用各自 try/except，异常时返回带 `rolled_back=False`、`rollback_failures=(str(exc),)` 的结构化结果。
  - 置信度: 高

- [AUD-13103] [Major] 预览-提交一致性门禁过弱：仅比较 plan_id 与 operation_count，未比较可用的 operation_summaries
  - 位置: src/features/edit/ai_editing/autonomous_agent.py:378-389
  - 证据:
    ```python
    @staticmethod
    def _compiled_results_match(preview_compiled: Any, commit_compiled: Any) -> bool:
        """Compare stable compile metadata without comparing command objects."""
        if preview_compiled is None or commit_compiled is None:
            return False
        for attribute in ("plan_id", "operation_count"):
            preview_value = getattr(preview_compiled, attribute, None)
            commit_value = getattr(commit_compiled, attribute, None)
            if preview_value is not None and commit_value is not None:
                if preview_value != commit_value:
                    return False
        return getattr(commit_compiled, "macro", None) is not None
    ```
    而 CompiledPlan（plan_compiler.py:105）带有 `operation_summaries: dict[str, dict[str, Any]]`（"op_id -> 编译摘要（用于 diff / 审计），不含 command 对象本身"）却未被比较。
  - 影响: 210-213 行注释明确此门禁旨在防"provider/compiler race 授权一份 diff 却写入另一份"。但同 plan 两次编译若因编译器行为中途变化（如插件注册表/效果目录在预览与提交之间刷新）产出内容不同、数量相同的命令，门禁放行；被消费的 grant 绑定的是 preview 的 diff_hash，实际提交的是另一份 macro，安全性声明与实现不匹配。
  - 建议: 在比较中加入 `operation_summaries` 相等性（或比较 diff_hash / 命令摘要哈希），不引入 command 对象比较。
  - 置信度: 中

- [AUD-13104] [Suggestion] 紧急停用分支中 revoke 自身抛异常会从 apply 逃逸且 grant 泄漏
  - 位置: src/features/edit/ai_editing/autonomous_agent.py:250-264
  - 证据:
    ```python
    except Exception:
        if approval_service is not None:
            approval_service.revoke(grant.grant_id)
        return self._blocked(
            plan_id, "AUTO_MODE_DISABLED", "无法确认自主剪辑安全状态",
        )
    ```
  - 影响: except 分支里的 `revoke` 未受保护；若 revoke 再抛异常，异常传播出 `apply()`，且已签发的一次性 grant 既未消费也未撤销，只能等 TTL 过期。
  - 建议: revoke 调用包一层 try/except 并记录诊断日志。
  - 置信度: 高

- [AUD-13105] [Suggestion] 跨模块导入另一个模块的私有符号 `_expected_commit_scope`
  - 位置: src/features/edit/ai_editing/autonomous_agent.py:268-274
  - 证据:
    ```python
    try:
        from src.features.edit.ai_editing.transaction_service import _expected_commit_scope

        approval_service.consume(
            grant.grant_id,
            _expected_commit_scope(plan, diff_hash=getattr(preview, "diff_hash", None)),
        )
    ```
  - 影响: 依赖 transaction_service 的下划线私有 API（controller 中另有 5 处同样模式），transaction_service 内部重构该函数签名/名称时此处运行时才失败（ImportError 被下方 except 吞成 AUTO_COMMIT_EXCEPTION，难定位）。
  - 建议: 在 transaction_service 中将 `_expected_commit_scope` 提升为公开 API（去下划线并导出），或提供公开的 `consume_for_commit(...)` 封装。
  - 置信度: 高

- [AUD-13106] [Suggestion] 空选择时素材范围检查被整体跳过；_asset_ids 只识别单数键 `asset_id`
  - 位置: src/features/edit/ai_editing/autonomous_agent.py:149-156, 391-399
  - 证据:
    ```python
    selected_asset_ids = tuple(self._host.get_selected_asset_ids() or ())
    if selected_asset_ids and not set(plan_asset_ids).issubset(set(selected_asset_ids)):
        return self._blocked(...)
    ...
    asset_id = str(params.get("asset_id", "") or "")
    if asset_id and asset_id not in values:
        values.append(asset_id)
    ```
  - 影响: `selected_asset_ids` 为空元组时任何 plan 均通过范围检查（"空选择 = 不限范围"的语义未在注释说明，与"当前选择范围之外必须重新规划"的声明存在张力）；此外 operation 以 `asset_ids` 列表或其他键名引用素材时不会被计入 plan_asset_ids，范围核算不完整。
  - 建议: 注释明确空选择的语义；如属有意放行建议改为显式配置项；_asset_ids 扩展识别 `asset_ids` 列表键。
  - 置信度: 中

### 通过项
- 状态提交一致性：操作数上限、禁用操作表、工程/时间线范围、base_revision 乐观锁、重放检测（ambiguous/already_applied）逐级门禁齐全
- grant 一次性消费且绑定 plan 与 preview 的 diff_hash；授权后复查 live kill-switch 关闭"编译期间停用"竞态
- 提交后 revision 前进校验 + 可选后置质量校验，失败路径均构造带回滚信息的结构化结果（rollback 调用本身的异常处理见 AUD-13102）
- 异常路径：preview/compile/policy/commit 各阶段异常均转为 blocked 结果，不向调用方裸抛
- 输入验证：operations 数量、plan_id/target 字段、asset 范围均有检查
- GPU/GLSL/信号防递归维度不适用（Qt/GPU/provider 无关的纯逻辑类）

---

## src/features/edit/ai_editing/autonomous_mission.py (行数: 307)

### 发现

- [AUD-13107] [Suggestion] start() 对同 run_id 不同 plan_id 的 mission 静默覆盖，旧状态无提示丢失
  - 位置: src/features/edit/ai_editing/autonomous_mission.py:122-130
  - 证据:
    ```python
    with self._lock:
        old = self._items.get(mission.run_id)
        if old is not None and old.plan_id == mission.plan_id:
            return old
        self._items[mission.run_id] = mission
        self._trim_locked()
    return mission
    ```
  - 影响: run_id 被复用于不同 plan_id 时（上游生成器缺陷或恢复场景错配），原 mission 的阶段进度/质量门记录被静默重置为 PLAN 起点，历史审计链断裂；无日志可查。
  - 建议: 覆盖不同 plan_id 时记录 warning，或返回错误让调用方显式处理 run_id 冲突。
  - 置信度: 高

### 通过项
- 线程安全：所有读写（start/get/record/mark_cancelled/export_all/import_all/clear/_trim）均在 RLock 内；export 先在锁内拷贝快照再序列化
- 输入验证：run_id/plan_id 非空、长度 ≤256、无 NUL；completed_stages 必须是 _STAGE_ORDER 的有序前缀；record 强制阶段顺序，违规 fail-closed 转 NEEDS_REPLAN
- 有界性：max_items 上限 32，_trim 按 updated_at 淘汰
- 持久化：schema_version 校验、stage_results/diagnostics 数量与长度截断、reason 经 redact_text + 关键词二次脱敏、malformed 行跳过不阻断加载
- 终态不可变：terminal 状态后 record/mark_cancelled 直接返回旧值，阶段不可跳过或重复

---

## src/features/edit/ai_editing/autonomous_recovery.py (行数: 193)

### 发现

- [AUD-13108] [Suggestion] mark()/upsert() 不校验状态转移方向，APPLIED 可被改回 PENDING 重新进入恢复队列
  - 位置: src/features/edit/ai_editing/autonomous_recovery.py:110-149
  - 证据:
    ```python
    def mark(self, run_id: str, status: str, reason: str = "") -> AutonomousCheckpoint | None:
        if status not in AutonomousCheckpointStatus.ALL:
            raise ValueError(f"unknown checkpoint status: {status!r}")
        with self._lock:
            old = self._items.get(str(run_id))
            if old is None:
                return None
            updated = replace(
                old, status=status, reason=str(reason)[:240], updated_at=time.time(),
            )
    ```
  - 影响: 只校验目标状态在枚举内，不校验转移合法性（如 APPLIED→PENDING、CANCELLED→PENDING）。一旦上游缺陷或恢复逻辑回放时误改，已应用计划会重新出现在 list_pending() 中触发恢复尝试；目前靠 AutonomousAgent 的 detect_autonomous_replay 兜底，但 store 层状态机不完整是隐患。
  - 建议: 定义合法转移表（PENDING→{NEEDS_REPLAN, APPLIED, CANCELLED, EXPIRED} 及单向），非法转移拒绝或记日志。
  - 置信度: 中

### 通过项
- 线程安全：RLock 覆盖全部读写路径；list_pending 在锁内完成过期标记与收集
- 输入验证：五个 ID 字段非空/≤256/无 NUL、base_revision 非负、asset_ids ≤128 且逐项校验、状态枚举校验（from_dict 构造同样过 __post_init__）
- TTL 过期：list_pending 将超时 PENDING 就地转 EXPIRED 并给出明确 reason
- 持久化：schema_version 校验、导出有界（MAX_CHECKPOINTS=32）且统一 sanitize_for_storage、malformed 行跳过
- 状态类用字符串常量类而非 Enum（与 MissionStatus 风格不一致）仅为风格问题，不构成缺陷

---

## src/features/edit/ai_editing/brief_service.py (行数: 349)

### 发现

- [AUD-13109] [Major] propose_patch_from_text 的 text_lower 为死变量：英文关键词匹配用原始 user_text，大小写敏感导致推断失败
  - 位置: src/features/edit/ai_editing/brief_service.py:290-299, 313-319
  - 证据:
    ```python
    text_lower = user_text.lower()
    # 平台启发式
    if any(k in user_text for k in ("竖版", "抖音", "竖屏", "vertical", "9:16", "tiktok")):
    ...
    elif any(k in user_text for k in ("横版", "横屏", "horizontal", "16:9", "youtube")):
    ...
    if any(k in user_text for k in ("快节奏", "快", "fast", "节奏")):
    ```
  - 影响: `text_lower` 赋值后从未使用（全文件仅此一处出现），所有英文关键词按小写书写却对原始大小写文本匹配。用户输入"TikTok"/"Vertical"/"Fast and warm"等常规大小写时平台与语气推断全部漏判，生成的提议 patch 缺平台/语气字段，用户批准后 brief 与实际需求不符。
  - 建议: 平台/语气/节奏匹配统一改用 `text_lower`（中文关键词不受影响），或删除死变量并在注释说明。
  - 置信度: 高

- [AUD-13110] [Suggestion] _merge 对外部 patch changes 的类型校验缺失：dict 形式 target_duration 缺键抛裸 KeyError，字符串被 tuple() 拆成单字符
  - 位置: src/features/edit/ai_editing/brief_service.py:149-166
  - 证据:
    ```python
    elif isinstance(td, dict):
        kwargs["target_duration"] = DurationTarget(
            int(td["ideal"]), int(td["min"]), int(td["max"])
        )
    if "required_beats" in changes:
        kwargs["required_beats"] = tuple(changes["required_beats"])
    if "tone" in changes:
        kwargs["tone"] = tuple(changes["tone"])
    ```
  - 影响: BriefPatch.changes 是对外契约（未来可来自持久化或 provider 反序列化），但 dict 缺 ideal/min/max 键时抛 KeyError、值非数值时抛 ValueError/TypeError，均非本类自定义的 BriefValidationError，违反自身错误契约与 .clinerules"外部数据须验证"；`tuple("fast_paced")` 会把字符串拆成 ('f','a','s',...) 静默写入 required_beats/tone/audience。当前仓内调用方（propose_patch_from_text）构造安全，故为 Suggestion。
  - 建议: _merge 对每个字段做 isinstance 校验，非法时统一抛 BriefValidationError；字符串先包成单元素元组或拒绝。
  - 置信度: 高

### 通过项
- 状态提交一致性：apply_patch 乐观锁（base_revision 冲突抛 BriefValidationError）+ with_revision_bumped；add_hard/add_soft 手动 +1 一致
- create_default 幂等：已存在时原样返回，不覆盖用户修改
- 锁定 source range 以硬约束形式落盘，locked_source_ranges/forbidden_labels/must_include_claim_ids 读取与写入对称
- variants_need_replan 逐项比较 brief_revision，语义正确
- 纯逻辑无 Qt/无 GPU 资源，信号防递归维度不适用；类型注解完整

---

## src/features/edit/ai_editing/capability_catalog.py (行数: 367)

### 发现

- [AUD-13111] [Suggestion] missing_evidence_for 对未声明工具返回 ()（无缺失证据），与模块 fail-closed 声明相悖
  - 位置: src/features/edit/ai_editing/capability_catalog.py:206-215
  - 证据:
    ```python
    def missing_evidence_for(
        self,
        tool_name: str,
        available_evidence: Iterable[str],
    ) -> tuple[str, ...]:
        entry = self.get(tool_name)
        if entry is None:
            return ()
    ```
  - 影响: 模块 docstring 声明"a tool not in the catalog is refused by the planner"，require() 也确实抛 KeyError；但本方法对未声明工具返回"证据全部满足"。若 planner 将来先查证据后 require，未声明的伪造工具会静默通过证据门（fail-open 陷阱）。当前仓内无生产调用方，属潜伏 API 契约缺陷，故定 Suggestion。
  - 建议: entry 为 None 时抛 KeyError/ValueError，或返回特殊值并在 docstring 注明调用方必须先 require。
  - 置信度: 高

- [AUD-13112] [Suggestion] remove() 在 owner_plugin_id 为空时可删除任何条目（含核心条目），违背自身 docstring
  - 位置: src/features/edit/ai_editing/capability_catalog.py:180-189
  - 证据:
    ```python
    def remove(self, name: str, *, owner_plugin_id: str = "") -> bool:
        """Remove an extension-owned entry without touching core entries."""
        entry = self._entries.get(str(name))
        if entry is None:
            return False
        if owner_plugin_id and entry.owner_plugin_id != owner_plugin_id:
            return False
        del self._entries[str(name)]
        return True
    ```
  - 影响: 核心条目 owner_plugin_id=""，扩展或任意代码调用 `remove("topos.apply_edit_plan")`（不带 owner 参数）即删掉核心写工具声明。方向是 fail-closed（工具被拒绝而非提权），且同进程 Python API 本难设硬边界，但与"without touching core entries"的文档承诺不符。
  - 建议: 空 owner 仅允许删除 owner_plugin_id 非空的条目；核心条目的移除走显式的单独方法。
  - 置信度: 高

- [AUD-13113] [Suggestion] register 的关键字参数 replace 遮蔽模块级 dataclasses.replace；CompensationAction.strategy 无枚举校验
  - 位置: src/features/edit/ai_editing/capability_catalog.py:174-178, 61-67
  - 证据:
    ```python
    def register(self, entry: CapabilityCatalogEntry, *, replace: bool = False) -> bool:
        if entry.name in self._entries and not replace:
            return False
    ```
    及：
    ```python
    @dataclass(frozen=True)
    class CompensationAction:
        """How to undo a tool's effect if a later step fails."""
        strategy: str                         # undo / rollback_group / manual
    ```
  - 影响: 参数名遮蔽 `from dataclasses import ... replace`（本方法内未用到故无运行时 bug，但属易错阴影）；CompensationAction.strategy 注释约定 undo/rollback_group/manual 三值却不校验，与同文件其他字段（domain/idempotency/cancel_semantics/execution_mode）的严格 __post_init__ 校验不一致，拼错策略名静默通过。
  - 建议: register 参数改名 overwrite；CompensationAction 增加 strategy 枚举校验。
  - 置信度: 高

### 通过项
- 输入验证：CapabilityCatalogEntry.__post_init__ 对 domain、幂等策略、取消语义、非负预算、执行模式、owner_plugin_id 空白均严格校验
- 种子目录对高影响工具（apply_edit_plan/request_export/generate_media）携带补偿动作、质量门与幂等策略，审计策略保留
- 与 tool_facade.list_tool_definitions 的对齐回填防漂移；execution_mode 冲突时以 facade 声明为准并显式 replace=True
- 注册表仅启动期构建，无运行期并发写场景；类型注解完整；纯逻辑无 Qt/GPU

---

## src/features/edit/ai_editing/capability_manifest.py (行数: 414)

### 发现

- [AUD-13114] [Major] build_manifest 两处宽泛 except 静默吞错且全程无日志：registry/编解码器读取失败时 manifest 静默失去整个能力面
  - 位置: src/features/edit/ai_editing/capability_manifest.py:236-239, 329-334
  - 证据:
    ```python
    try:
        definitions = effect_registry.get_all_definitions()
    except Exception:
        definitions = []
    ```
    及：
    ```python
    try:
        from src.features.edit.core.timeline_export import ExportCodec
        codecs = [c.value for c in ExportCodec]
    except Exception:
        codecs = []
    ```
  - 影响: 全文件无 logging。EffectRegistry 读取抛错（哪怕是一次性的 IO/注册表损坏）→ manifest 声明零效果；ExportCodec 导入失败 → 可导出编解码器清空。AI/脚本客户端据此认为"该工程不支持任何效果/导出格式"，生成规避效果的 plan 或拒绝导出，用户看不到任何原因（违反 .clinerules"不得宽泛 except 掩盖领域错误"）。虽属 fail-closed 方向（不会调用不存在的能力），但错误被完全掩盖且难以诊断。
  - 建议: 两处 except 至少 logger.warning 带异常信息；或在 manifest 中带上 degradation 标记（类似 topos_removed_reason_code 的机制）让客户端可感知。
  - 置信度: 高

- [AUD-13115] [Suggestion] 效果循环内重复局部 import，且 plugin/非 plugin 两个分支代码重复仅 fallback 不同
  - 位置: src/features/edit/ai_editing/capability_manifest.py:254-291
  - 证据:
    ```python
    if target == "video_clip" and not et.startswith("plugin:"):
        try:
            from src.features.shared.effects import (
                get_effect_catalog, resolve_execution_capability,
            )
            execution = resolve_execution_capability(
                et, catalog_known=get_effect_catalog().is_known(et))
            ...
        except Exception:
            exportable = False
            unsupported = True
            ...
    elif target == "video_clip":
        try:
            from src.features.shared.effects import (
                get_effect_catalog, resolve_execution_capability,
            )
    ```
  - 影响: import 在 for 循环内对每个 definition 执行（模块缓存后开销小但冗余）；两个 try 块主体逐字相同，仅 except fallback 值不同，维护时易只改一处。
  - 建议: 循环前导入一次，抽出单个 `_resolve_effect_export_capability(et, *, plugin: bool)` 辅助函数统一 fallback。
  - 置信度: 高

### 通过项
- 状态一致性：operations 与 plan_compiler.COMPILABLE_OPERATIONS 同一真相源，杜绝模型生成必然编译失败的 plan
- fail-closed 交付门禁：runtime snapshot 中 codec.topos 非 AVAILABLE 时从 codecs 移除 "topos" 并记录 reason_code/detail
- timeline 能力块取自 GUI-free 的 UI 模块（timeline_commands 等文档明确 headless 可用），CLI/无头场景安全；commands/error_codes/row_types 由 dir()/常量派生，配合测试防漂移
- 表达式函数名来自运行时同一 parser（SAFE_MATH_FUNCTIONS | RUNTIME_FUNCTION_NAMES）
- 输入验证：_params_schema_to_capabilities 对 dict/对象两种 schema 形态兼容读取，min/max/step/choices 按存在性输出
- 类型注解完整；纯数据构建无 Qt/GPU 资源、无信号

---

## src/features/edit/ai_editing/capability_status.py (行数: 379)

### 发现

- [AUD-13116] [Suggestion] _provider_is_available 对未实现 is_available 的 provider 默认 True（fail-open），生成能力可能被标 AVAILABLE 而实际不可达
  - 位置: src/features/edit/ai_editing/capability_status.py:123-125, 332-340
  - 证据:
    ```python
    def _provider_is_available(provider: Any) -> bool:
        checker = getattr(provider, "is_available", None)
        return bool(checker()) if callable(checker) else True
    ```
    及 build_capability_matrix 中：
    ```python
    active = next((p for p in matching if _provider_is_available(p)), None)
    ...
    elif _provider_is_demo(active):
        state = CapabilityState.DEMO
    else:
        state = CapabilityState.AVAILABLE
    ```
  - 影响: 模块开篇强调"确定性测试 provider 绝不能显示为生产生成器"，但可用性判定本身默认放行：provider 未实现 is_available 且非 demo 时直接标 AVAILABLE（生成能力没有类似 LLM 的 UNVERIFIED/连接测试通道），能力矩阵的"诚实性"在这一路径上打折。
  - 建议: 无 is_available 方法时返回 False 或映射到 UNVERIFIED/NOT_INSTALLED，要求 provider 显式声明可用性。
  - 置信度: 中

- [AUD-13117] [Suggestion] agent_auto_mode/automatic_export/cloud_upload 仅凭 feature flag 开启即标 AVAILABLE，未验证对应子系统真实就绪
  - 位置: src/features/edit/ai_editing/capability_status.py:355-370
  - 证据:
    ```python
    for capability_id, label, flag_name in (
        ("agent_auto_mode", "受限自动模式", "agent_auto_mode"),
        ("automatic_export", "自动导出", "auto_export"),
        ("cloud_upload", "云端素材上传", "cloud_upload"),
    ):
        enabled = _flag_enabled(feature_flags, flag_name, False)
        statuses.append(CapabilityStatus(
            ...
            state=CapabilityState.AVAILABLE if enabled else CapabilityState.BLOCKED_BY_POLICY,
    ```
  - 影响: flag 开启 ≠ 子系统可用（如自动导出开启但导出器/编解码缺失），状态展示过于乐观；与矩阵其余条目按实际注册状态判定的做法不一致。
  - 建议: 有条件时叠加子系统就绪检查，或使用独立的 "enabled/pending" 语义而非 AVAILABLE。
  - 置信度: 中

### 通过项
- 构建为纯函数、无网络副作用（连接测试结果仅作为输入注入），可测试性好
- 诚实性核心规则落实到位：DEMO 状态永不等于 available；LLM 云端 profile 未完成连接测试标 UNVERIFIED 而非 AVAILABLE
- 本地模型包状态逐级判定严谨：缺 release manifest SHA-256 → NOT_INSTALLED、目录存在校验的演示模型 → DEMO、已安装但校验失败 → FAILED(可重试)
- 输入验证：_pack_state 捕获范围收窄到 (AttributeError, KeyError, OSError, RuntimeError) 并保留异常类型名，无宽泛 except
- 类型注解完整；无 Qt/GPU/信号维度
