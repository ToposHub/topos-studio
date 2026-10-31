# 批次报告：batch-144 — src/features/edit/ai_editing（8 文件，2,582 行）

编号区间：AUD-14400–AUD-14499。审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。

本批次为 AI 编辑功能层（TAEP M9 Skill 体系、语义索引、会话目录、快照/语音/启动诊断），不直接触碰 OpenGL、GLSL 或渲染线程；GPU/GLSL/信号防递归维度逐文件核定为不适用或无问题。

汇总：Critical 0 · Major 4 · Suggestion 18。

---

## src/features/edit/ai_editing/semantic_index.py (行数: 303)

### 发现

- [AUD-14400] [Suggestion] `add()` 覆盖同 ID segment 时不清理旧 embedding，缓存与内容脱节
  - 位置: src/features/edit/ai_editing/semantic_index.py:62-71
  - 证据:
    ```python
    def add(self, segment: SemanticSegment) -> None:
        self._segments[segment.segment_id] = segment
    ...
    def remove(self, segment_id: str) -> None:
        self._segments.pop(segment_id, None)
        self._embeddings.pop(segment_id, None)
    ```
  - 影响: `remove` 会同步清理 `_embeddings`，但 `add` 对同 `segment_id` 的覆盖写入（如重新分析后 re-add）保留旧向量，`similarity_search` 之后用陈旧 embedding 给新 segment 打分，结果错误且无提示。
  - 建议: `add()` 中当 `segment_id` 已存在时同时 `self._embeddings.pop(segment_id, None)`，由调用方决定是否重新 attach。
  - 置信度: 中（当前调用方 `build_index_from_analysis` 总是新建索引，覆盖路径未观察到现网触发，但 API 明确允许）。

- [AUD-14401] [Suggestion] 零向量 embedding 在 `similarity_search` 中以 -1.0 分入选
  - 位置: src/features/edit/ai_editing/semantic_index.py:218-220
  - 证据:
    ```python
    denom = qnorm * math.sqrt(sum(value * value for value in embedding))
    score = sum(a * b for a, b in zip(query, embedding)) / denom if denom else -1.0
    if score < min_score:
        continue
    ```
  - 影响: `add_embedding` 只校验非空与 finite（L82），不拒绝全零向量；零向量 embedding 的 denom=0 → score=-1.0，而默认 `min_score=-1.0` 时 `-1.0 < -1.0` 为 False，仍进入结果集（排最后）。边缘 case，影响极小。
  - 建议: `add_embedding` 拒绝零向量，或 denom==0 时 continue。
  - 置信度: 低（行为确凿，但实际影响可忽略）。

### 通过项
- `query` 的稳定排序（user_verified 优先 → confidence 降序 → range.start）与分页 `limit` 边界（`results[:limit]`，search/similarity 的 `[:max(0, limit)]`）正确。
- `coverage` 区间合并逻辑正确（排序后 `start <= last_end` 合并，含相邻区间）。
- `add_embedding` 对未知 segment 抛 KeyError、对非 finite/空向量抛 ValueError——外部数据验证到位。
- `build_index_from_analysis` 只读 `get_segments`/`get_embedding`（两者在 analysis_service.py:553/563 均存在），不触发分析、不写 timeline，符合"查询层"边界。
- 线程模型在类 docstring 明示不做并发控制、由 service 层负责；本文件无共享可变状态的隐藏并发使用。
- 类型注解完整；`search_evidence` 永不返回裸 label（evidence 携带 `segment.to_dict()`）。

---

## src/features/edit/ai_editing/session_models.py (行数: 410)

### 发现

- [AUD-14402] [Major] `SessionCatalog.from_dict` 接受未来版本号，未知字段在下次落盘时被静默丢弃
  - 位置: src/features/edit/ai_editing/session_models.py:345-357
  - 证据:
    ```python
    version = int(data.get("version", LEGACY_SNAPSHOT_VERSION))
    if version < SESSION_CATALOG_VERSION:
        # 旧版（v1 单快照）→ 迁移
        return migrate_legacy_snapshot(data)
    catalog = SessionCatalog(version=version)
    catalog.active_session_id = str(data.get("active_session_id", ""))
    ...
    for sid, ed in data.get("sessions", {}).items():
        catalog.sessions[sid] = SessionCatalogEntry.from_dict(ed)
    ```
  - 影响: `version > SESSION_CATALOG_VERSION`（如新版程序写的 v3 文件被旧版程序打开）不做拒绝也不保留原始数据：`from_dict` 只取已知字段，`AssistantSessionStore.load`（assistant_session_store.py:201）加载成功后任何一次 `to_dict()` 落盘都会以 v3 版本号写出已丢失新字段的目录——会话数据静默丢失且无提示。
  - 建议: 对 `version > SESSION_CATALOG_VERSION` 显式拒绝（返回空目录+诊断）或保留 raw dict 原样透传，禁止"解析成功但字段蒸发"的 round-trip。
  - 置信度: 中（代码路径确凿；触发需要版本降级打开新工程的真实场景）。

- [AUD-14403] [Suggestion] `migrate_legacy_snapshot` 存在死代码，注释声称保留 variant/decision 引用但实际是 no-op
  - 位置: src/features/edit/ai_editing/session_models.py:393-396
  - 证据:
    ```python
    for vid in (data.get("variants", {}) or {}).keys():
        entry.decision_ids  # noop placeholder
    # 把 variant/decision id 作为引用保留（不复制内容）
    entry.artifact_ids = []
    ```
  - 影响: 循环体只读取属性不产生副作用，`entry.artifact_ids = []` 也是空赋值；上一行注释"旧 variant/decision 的 ID 引用进会话条目（便于 UI 定位）"并未发生。若 UI 依赖 `entry.decision_ids`/`artifact_ids` 定位旧数据将取不到（原始数据仍在 `legacy_snapshot` 中，未丢失但不可定位）。
  - 建议: 实现引用收集（`entry.decision_ids = list((data.get("decisions", {}) or {}).keys())` 等）或删除死循环并修正注释。
  - 置信度: 高。

- [AUD-14404] [Suggestion] `archive_session`/`delete_session` 用插入序（创建序）选"最近"会话，与注释语义不符
  - 位置: src/features/edit/ai_editing/session_models.py:296-301, 326-330
  - 证据:
    ```python
    if self.active_session_id == session_id:
        # 归档当前会话：切到最近一个未归档会话，没有则置空
        non_archived = [
            sid for sid, e in self.sessions.items() if not e.is_archived
        ]
        self.active_session_id = non_archived[-1] if non_archived else ""
    ```
  - 影响: `dict.items()` 是插入序（=创建序），`[-1]` 选中的是最晚**创建**的会话，而注释说"最近一个"且 `list_sessions`（L318）按 `updated_at` 排序——归档/删除当前会话后自动激活的可能不是用户最近活跃的会话。
  - 建议: `non_archived` 按 `e.updated_at` 排序后取最后一个，与 `list_sessions` 语义对齐。
  - 置信度: 高。

- [AUD-14405] [Suggestion] `MessageSummary` 非法 role 被静默改写为 "user"
  - 位置: src/features/edit/ai_editing/session_models.py:51-53
  - 证据:
    ```python
    def __post_init__(self) -> None:
        if self.role not in ("user", "assistant", "system"):
            object.__setattr__(self, "role", "user")
    ```
  - 影响: 从持久化文件反序列化（`from_dict` 直传 role）时，损坏或未知 role 的 assistant/system 消息被无提示地标成 user 消息，历史对话呈现错误且无诊断。
  - 建议: 至少记 warning，或收敛到 "system"，或在 `from_dict` 中过滤非法条目。
  - 置信度: 高（行为确凿；实际后果为展示层错标）。

- [AUD-14406] [Suggestion] `from_dict` 系列对容器元素类型不校验，字符串会被 `tuple()` 拆成字符
  - 位置: src/features/edit/ai_editing/session_models.py:150-151, 229-232
  - 证据:
    ```python
    preserved_evidence_refs=tuple(d.get("preserved_evidence_refs", [])),
    preserved_artifact_ids=tuple(d.get("preserved_artifact_ids", [])),
    ...
    artifact_ids=list(d.get("artifact_ids", [])),
    ```
  - 影响: 外部 JSON 若把列表字段写成字符串（如 `"preserved_evidence_refs": "abc"`），`tuple("abc")` 得 `('a','b','c')`，静默产出错误引用；`messages` 列表元素若非 dict 则在 `MessageSummary.from_dict` 内 `d.get` 抛 AttributeError。
  - 建议: 容器字段先做 `isinstance(x, (list, tuple))` 检查，非法时取默认值并记录。
  - 置信度: 中（需外部数据损坏才触发；违反"外部数据须验证"铁律的严格解读）。

### 通过项
- 脱敏红线执行到位：所有落盘字段经 `sanitize_for_storage`（invocation_models.py:669，递归替换绝对路径/secret）；只存 run_id/artifact_id 引用不存大缓存。
- 序列化对称性：各 `to_dict`/`from_dict` 字段一一对应，`archived_at` 的 None 处理正确。
- `record_run` 对重复 run_id 做替换去重；`set_active` 拒绝归档会话；`create_session` 不删除既有会话（符合"新对话不能删除工程历史"红线）。
- 迁移路径保留 v1 全量数据于 `legacy_snapshot`，未恢复旧 grant（符合红线）。
- 纯数据模块，无线程/GPU/信号维度问题；类型注解完整。

---

## src/features/edit/ai_editing/skill_manifest.py (行数: 484)

### 发现

- [AUD-14407] [Suggestion] `ToolPolicy` 空 `allowed_tools` 语义为"允许全部（≤ceiling）"，默认策略默认开放
  - 位置: src/features/edit/ai_editing/skill_manifest.py:104-107, 197
  - 证据:
    ```python
    if tool_name in self.forbidden_tools:
        return False
    if self.allowed_tools and tool_name not in self.allowed_tools:
        return False
    ...
    tool_policy: ToolPolicy = field(default_factory=lambda: ToolPolicy())
    ```
  - 影响: `allowed_tools=()`（`ToolPolicy()` 默认值，也是 `SkillManifest` 的默认 tool_policy）时 allowlist 检查被跳过——任何 permission ≤ ceiling 的工具都放行。与"``allowed_tools`` 是白名单"的直觉相反；忘记声明 tool_policy 的第三方 Skill 默认可见全部 read 工具，与"最小权限"红线方向相悖。
  - 建议: 明确文档化"空=不限（受 ceiling 约束）"，或改为 default-deny（空 allowlist 拒绝一切），并要求注册时 tool_policy 非默认。
  - 置信度: 中（可能是有意设计；官方 Skill 均显式声明，风险在第三方/本地 Skill 路径）。

- [AUD-14408] [Suggestion] frozen `SkillManifest` 的 dict 字段可变，原地修改后 `content_hash`/signature 与内容脱节
  - 位置: src/features/edit/ai_editing/skill_manifest.py:194-206, 222-226
  - 证据:
    ```python
    brief_template: dict[str, Any] = field(default_factory=dict)
    input_schema: dict[str, Any] = field(default_factory=dict)
    ...
    @property
    def content_hash(self) -> str:
        if self.signature is not None:
            return self.signature.content_hash
        return compute_content_hash(self)
    ```
  - 影响: frozen 只保证字段引用不可变，`brief_template`/`input_schema`/`output_schema`/`ui_card` 是共享可变 dict；外部代码原地改 dict 后，带签名的 manifest 的 `content_hash` 属性仍返回旧签名 hash（不重算）。官方 Skill 在 `SkillRegistry.__init__` 注册时跳过完整性自检（skill_registry.py:94-96），此类漂移对官方 Skill 不可见，削弱"manifest 版本/hash 不可变"红线。
  - 建议: 构造时深拷贝/冻结 dict 字段（如转 JSON 串或 MappingProxyType），或 `content_hash` 始终即时重算并与 signature 比对。
  - 置信度: 中（需要外部代码主动原地修改才触发；防线缺口确凿）。

- [AUD-14409] [Suggestion] `_PERM_ORDER` 本地镜像 ToolPermission 顺序，常量双份存在漂移风险
  - 位置: src/features/edit/ai_editing/skill_manifest.py:109
  - 证据:
    ```python
    _PERM_ORDER = ("read", "preview", "proposal", "write", "expensive", "file_write")
    ```
  - 影响: 已对照 tool_facade.py:69-77 的 `ToolPermission`，当前一致；但若枚举新增级别或调整顺序（如 file_write 与 expensive 换序），此处不会同步，`permits` 的 ceiling 判定静默失真（多放或少放权限）。非法值已有 ValueError→False 兜底，但顺序漂移不报错。
  - 建议: 从 `ToolPermission` 枚举推导顺序（模块级 `tuple(t.value for t in ToolPermission)`，必要时延迟导入避免循环），消除双份事实。
  - 置信度: 中（风险面向未来；当前值一致）。

### 通过项
- `compute_content_hash` 覆盖全部语义字段、不含 signature 本身（无循环），`sort_keys=True`+`default=str` 保证稳定。
- `with_signature` 用 `dataclasses.replace` 正确处理 frozen；三个官方 Skill 均带签名构造。
- `_OFFICIAL_TOOL_POLICY` 不含 write/expensive/file_write 级工具，显式 forbidden `apply_edit_plan`/`render_preview`/`request_export`，符合"Skill 不自动取得写权限"红线。
- `SkillManifest.__post_init__` 拒绝空 skill_id；roles 规范化去重。
- `SkillSource.unverified` 标记 local_dev；`FORBIDDEN_CAPABILITIES` 镜像与 tool_facade.py:458-461 逐项一致（已对照）。
- 无线程/GPU/信号维度；类型注解完整。

---

## src/features/edit/ai_editing/skill_registry.py (行数: 287)

### 发现

- [AUD-14410] [Major] REVISION_CONFLICT 诊断模板变量不匹配，升级失败的真实原因被整体丢弃、消息渲染为原始占位符
  - 位置: src/features/edit/ai_editing/skill_registry.py:176-184
  - 证据:
    ```python
    if not _version_is_higher(manifest.version, existing.version):
        diags.append(make_diagnostic(
            DiagnosticSeverity.ERROR, TaepDiagnosticCode.REVISION_CONFLICT,
            detail=(
                f"skill {manifest.skill_id} already registered at v{existing.version}; "
                f"new v{manifest.version} is not higher (use upgrade with higher version)"
            ),
        ))
    ```
  - 影响: `make_diagnostic` 把 `detail=` 收进 `**message_vars`（diagnostics.py:258-277），而 REVISION_CONFLICT 模板需要 `{base}`/`{current}`（diagnostics.py:97-100）；`format` 抛 KeyError 被 `format_message` 捕获后返回**未渲染的原始模板**"项目已被修改（base={base}，current={current}）…"。用户看到的是带花括号占位符、且语境完全错误（讲的是工程 revision 冲突而非 Skill 版本冲突）的消息；版本不更高的具体原因（含两个版本号）既不在 message 也不在 details 字段，完全丢失。
  - 建议: 传入模板所需变量（`base=existing.version, current=manifest.version`），并把完整说明放进 `details=`；或为 skill 体系使用匹配的模板/新增专用诊断码。
  - 置信度: 高（模板与调用点均已核实）。

- [AUD-14411] [Suggestion] `register` 升级替换时重置 enabled 状态，已禁用 Skill 升级后被静默重新启用
  - 位置: src/features/edit/ai_editing/skill_registry.py:174-189
  - 证据:
    ```python
    existing = self._entries.get(manifest.skill_id)
    if existing is not None:
        if not _version_is_higher(manifest.version, existing.version):
            ...
    # 通过校验 → 注册
    self._entries[manifest.skill_id] = _RegistryEntry(
        manifest=manifest, enabled=enabled,
    )
    ```
  - 影响: 用户显式 `disable` 的（例如 unverified 的 local_dev）Skill，经 `register` 高版本升级后 `enabled` 回到默认 True，禁用意图被静默覆盖——未验证 Skill 重新可见可调。
  - 建议: 升级路径继承 `existing.enabled`，或至少在替换时对"原为 disabled"的情况给出诊断/要求显式 `enabled` 参数。
  - 置信度: 中（代码路径确凿；宿主可能总是显式传 enabled，未见调用方）。

- [AUD-14412] [Suggestion] `register` 不校验 `roles` 是否为合法 TaskRole 值
  - 位置: src/features/edit/ai_editing/skill_registry.py:143-190
  - 证据:
    ```python
    # 1. content_hash 完整性自检
    if manifest.signature is not None:
    ...
    # 2. 工具策略合法性
    forbidden_declared = [
    ```
  - 影响: 注册校验只覆盖 hash/工具策略/版本，`roles` 中非法角色名（如拼写错误）在注册期放行，到运行期才被 `DefaultGraphBuilder` 静默丢弃（见 AUD-14413），把本可在安装期报错的问题推迟为运行期"空成功"。
  - 建议: `register` 对 `manifest.roles` 非空的 manifest 增加 `all(r in {rr.value for rr in TaskRole} for r in manifest.roles)` 校验，非法角色名直接拒绝注册并返回诊断。
  - 置信度: 高（校验缺失确凿；后果与 AUD-14413 联动）。

### 通过项
- content_hash 完整性自检实现正确（重算比对，不一致拒绝且诊断含两 hash 前缀）。
- FORBIDDEN_CAPABILITIES 镜像与 tool_facade 完全一致（含注释说明避免循环导入的动机）；`tools_for`/`validate_tool_call` 对未注册/禁用 Skill 一律拒绝，不静默放行。
- `_version_is_higher` 语义比较补齐长度、非法版本回退字符串比较（有注释说明，非吞错）。
- `remove` 保护官方内置不可卸载；`list_skills` 稳定排序。
- 无线程/GPU/信号维度；类型注解完整。

---

## src/features/edit/ai_editing/skill_runtime.py (行数: 659)

### 发现

- [AUD-14413] [Major] `DefaultGraphBuilder` 静默丢弃未知 role，全部非法时产出空图并"成功"返回空结果——失败不可见
  - 位置: src/features/edit/ai_editing/skill_runtime.py:607-611
  - 证据:
    ```python
    declared = [TaskRole(r) for r in manifest.roles if r in {rr.value for rr in TaskRole}]
    if not declared:
        # 无角色声明 → 空 graph（SkillRuntime 会走 official 分支，不应到这）
        return TaskGraph(skill_id=manifest.skill_id, timeline_id=inp.timeline_id)
    ```
  - 影响: `manifest.roles` 非空但值全部非法时（注册期不校验，见 AUD-14412），`declared` 为空 → 返回无节点空图；`TaskGraph.validate`（task_graph.py:226-243，无节点时无悬空/无环 → 通过）放行，`TaskGraphExecutor.execute` 返回 `()`，最终 `SkillResult` 的 `artifacts=()`、`degradation=None`、无 failed/cancelled——一次注册成功、运行"成功"却什么都没做的 Skill，用户得不到任何失败信号。部分非法时同样静默少跑角色。违反"失败可见（§6.2 规则 5）绝不编造完成"红线。注释自己也承认"不应到这"，但没有防御。
  - 建议: `declared` 为空时抛错或返回带 FAILED 节点的图；对"声明了但被过滤掉的角色"至少生成 degradation 诊断。
  - 置信度: 高（整条代码路径已核实；触发需 malformed manifest 注册成功）。

- [AUD-14414] [Suggestion] `_validate_input` 三处诊断缺模板变量，消息渲染为带 `{asset_id!r}` 等占位符的原文
  - 位置: src/features/edit/ai_editing/skill_runtime.py:571-584
  - 证据:
    ```python
    diags.append(make_diagnostic(
        DiagnosticSeverity.ERROR, TaepDiagnosticCode.SCHEMA_INVALID,
        details={"reason": "asset_ids must be non-empty"},
    ))
    ...
    diags.append(make_diagnostic(
        DiagnosticSeverity.ERROR, TaepDiagnosticCode.SOURCE_RANGE_INVALID,
        details={"reason": "target_duration must be > 0"},
    ))
    ```
  - 影响: SCHEMA_INVALID 模板需要 `{detail}`、SOURCE_RANGE_INVALID 需要 `{asset_id}/{duration}/{start}/{dur}`（diagnostics.py:155-160, 171-175），三处调用只传 `details=`（结构化字段，不参与模板渲染），`format_message` 捕获 KeyError 后返回未渲染模板——用户看到"计划不符合 TAEP JSON Schema：{detail}"/"源范围无效：素材 {asset_id!r} 时长 {duration} 帧…"这类含花括号的乱码消息；真实原因只藏在 details 里。与 AUD-14410 同类问题。
  - 建议: 补传模板变量（如 `detail="asset_ids must be non-empty"`），或使用无变量模板的专用诊断码。
  - 置信度: 高。

- [AUD-14415] [Suggestion] `_validate_input` 与 manifest `input_schema` 的 required 不一致（brief_id/base_revision 未校验）
  - 位置: src/features/edit/ai_editing/skill_runtime.py:567-585
  - 证据:
    ```python
    if not inp.asset_ids:
        ...
    if not inp.timeline_id or not inp.project_id:
        ...
    if inp.target_duration <= 0:
        ...
    ```
  - 影响: 三个官方 Skill 的 `input_schema` 都声明 `required: ["asset_ids", "timeline_id", "project_id", "brief_id"]`，但校验不检查 `brief_id`，也不检查 `base_revision >= 0`；缺 brief_id 的输入通过 runtime 校验，失败被推迟到 backend（或产生挂错会话的产物）。
  - 建议: 校验 `inp.brief_id` 非空与 `base_revision` 非负；理想情况按 `manifest.input_schema.required` 驱动。
  - 置信度: 高。

- [AUD-14416] [Suggestion] `DeterministicRoleExecutor._media_librarian` 把所有 citation 硬编码 `asset_id=primary`、`trust="trusted"`
  - 位置: src/features/edit/ai_editing/skill_runtime.py:244-247
  - 证据:
    ```python
    citations = tuple(
        EvidenceCitation(segment_id=ref, asset_id=primary, trust="trusted")
        for ref in ev_refs
    )
    ```
  - 影响: 多素材输入时，属于其他 asset 的 segment 引用被标成 `asset_id=primary`（溯源错位）；OCR 等 untrusted 证据也被标 trusted（task_artifacts.py:118-128 定义了 trust 语义，§1.2 不变量 #2 要求 untrusted 不得视为可信）。下游 `_qa_reviewer` 的 `untrusted_count > 0` 检查（L336-341）在该执行器下永远为 0，成为死分支。
  - 建议: 从 SemanticIndex/analysis 查 ref 的真实 asset_id 与 trust 标记；v1 stub 至少应在注释/字段里显式声明该近似，避免 QA 逻辑形同虚设。
  - 置信度: 中（v1 确定性 stub 的既有注释表明部分知情，但 trust 硬编码与红线冲突确凿）。

- [AUD-14417] [Suggestion] QA 时长消息误用 `role_hint` 兜底 '30s'，而 `DefaultGraphBuilder` 把 role_hint 填成 Skill title
  - 位置: src/features/edit/ai_editing/skill_runtime.py:348, 634
  - 证据:
    ```python
    message=f"目标时长需用户确认（首版默认 {context.role_hint or '30s'}）",
    ...
    context = ContextSlice(
        ...
        role_hint=manifest.title,  # 用 Skill title 作为角色提示
    )
    ```
  - 影响: 默认图下 `role_hint` 是 manifest.title（如"口播精剪"），QA 消息渲染为"目标时长需用户确认（首版默认 口播精剪）"——语义错误的信息进入用户可见的 QA finding。`SkillInput.target_duration` 才是真实时长但未进 ContextSlice。
  - 建议: 消息改用 `inp.target_duration`（必要时将其加入 ContextSlice），删除 role_hint 兜底。
  - 置信度: 高。

### 通过项
- 单写者编排正确：`run()` 先 `acquire`（同 skill 重入安全，task_graph.py:324-335 已核），冲突时带 holder 的 degradation 返回且不误释放；`try/finally` 保证成功/失败/降级路径都 `release`。TaskGraphExecutor 仅做 `assert_subagent_no_write`，无双占/泄漏。
- 取消传播：`cancel_requested` 透传 `TaskGraphExecutor.execute`（每节点派发前检查，已核 task_graph.py:423-456）；官方路径不透传有注释说明（宿主 M7 层负责）。
- 失败可见：FAILED/CANCELLED/NEEDS_USER_INPUT/degraded_tasks 全部映射为 `degradation` 文案并入 `SkillResult`；executor 对 role 异常（含 NotImplementedError）捕获并标 FAILED（task_graph.py:545-551）。
- `_artifact_kind_matches` 契约校验存在（task_graph.py:553-566），DeterministicRoleExecutor 各角色产物类型与之一致（ScriptDraft/EvidenceSet/StoryOutline/QaReport，字段签名已逐一核对 task_artifacts.py）。
- 输入校验覆盖 asset_ids/timeline_id/project_id/target_duration；`run()` 早退路径（未注册/禁用/校验失败）不占写候选。
- `severity == "error"` 判断与 `Diagnostic.to_dict` 的 `severity.value`（"error"）匹配（diagnostics.py:22-27 已核）。
- 无 GPU/GLSL/信号维度；类型注解完整。

---

## src/features/edit/ai_editing/snapshot_service.py (行数: 183)

### 发现

- [AUD-14418] [Major] `analysis_provider.get_status` 被 `except Exception` 整体吞掉且无日志，掩盖领域错误
  - 位置: src/features/edit/ai_editing/snapshot_service.py:128-132
  - 证据:
    ```python
    analysis_status = "unknown"
    if analysis_provider is not None:
        try:
            analysis_status = analysis_provider.get_status(asset_id)
        except Exception:
            analysis_status = "unknown"
    ```
  - 影响: 违反 .clinerules 铁律"不得宽泛 except 掩盖领域错误"。provider 的任何错误（包括 AttributeError 之类的编程错误、授权/状态机领域错误）都被静默折成 "unknown"：AI 拿到的世界状态里所有素材分析状态可能全部静默变为"未知"，诱导 planner 重复发起分析或误报"请先分析素材"，且无任何日志可排查。
  - 建议: 捕获收窄到预期异常（或至少排除 AttributeError/TypeError 等），并 `logger.warning` 记录 asset_id 与异常再回退。
  - 置信度: 高（代码确凿；实际后果取决于 provider 实现的健壮性）。

- [AUD-14419] [Suggestion] `_build_track_summary` 的 `sequence` 参数从未使用
  - 位置: src/features/edit/ai_editing/snapshot_service.py:42-44
  - 证据:
    ```python
    def _build_track_summary(
        sequence, track, detail_level: DetailLevel
    ) -> SnapshotTrack:
        """构建单轨摘要。full 级别才带 clip 详情。"""
    ```
  - 影响: 函数体只使用 `track` 与 `detail_level`；`sequence` 形参（调用点 L160 也传了）是残留参数，误导阅读并缺少类型注解（该参数无注解，与本文件其余强类型风格不一致）。
  - 建议: 删除 `sequence` 参数并同步调用点。
  - 置信度: 高。

### 通过项
- 路径脱敏正确：`_redact_path` 兼容 win/unix 分隔符只留 basename；asset 名优先 display_name，不含绝对路径/secret。
- 结构分级正确：STRUCTURE 只给 `_count`，FULL 给完整 clip 字段；subtitle 轨走 `subtitle_clips` 且 `style_override` 有 None 保护；`caption_metadata` 直调 `.to_dict()` 安全（SubtitleClip 的该字段为 `default_factory=CaptionCueMetadata` 且 `__post_init__` 校验非 None，timeline_v2.py:1355/1384-1387 已核）。
- duration_frames 元数据推断有 try/except (TypeError, ValueError) 保护并回退 None（窄捕获，合理）。
- 无线程/GPU/信号维度；除 AUD-14419 外类型注解完整。

---

## src/features/edit/ai_editing/speech_preprocessor.py (行数: 135)

### 发现

- [AUD-14420] [Suggestion] `unavailable_reason` 对"manager 存在但无 demucs_v4 spec"视为可用，失败延迟到 subprocess 才暴露
  - 位置: src/features/edit/ai_editing/speech_preprocessor.py:75-82
  - 证据:
    ```python
    manager = self._model_pack_manager
    if manager is not None and getattr(manager, "get_spec", None):
        spec = manager.get_spec("demucs_v4")
        if spec is not None:
            status = manager.get_status("demucs_v4")
            if not bool(getattr(status, "usable", False)):
                return "Demucs v4 模型包未安装或未通过校验"
    return ""
    ```
  - 影响: 注入了 model_pack_manager 但 `get_spec("demucs_v4")` 返回 None（模型包未注册）时直接判可用；若环境确实依赖模型包分发权重，错误要到 subprocess 生成/下载失败才出现，且报错信息不含"模型包未注册"这一根因。
  - 建议: 明确 spec=None 的语义（不托管→可用；托管但未注册→不可用），后者返回相应原因。
  - 置信度: 低（spec=None 的业务语义未知，两种解读都可能成立）。

### 通过项
- 异常与清理路径完整：temp 目录在 OSError/TimeoutExpired、非零退出码、找不到 vocals.wav 三条失败路径均 `shutil.rmtree(ignore_errors=True)` 后抛 RuntimeError；成功路径经 `PreprocessedAudio.cleanup()` 交调用方（职责明确）。
- subprocess 安全：参数列表形式无 shell 注入面；`check=False` 手工检查 returncode；窄捕获 `(OSError, subprocess.TimeoutExpired)`；超时由 `subprocess.run` 终止子进程；错误消息截取 stderr 尾部 800 字符避免 flooding。
- 输入验证：`process` 对空/不存在路径抛 FileNotFoundError；`is_available` 前置检查。
- `importlib.util.find_spec` 探测可选依赖、不导入 PyTorch 进 Qt 进程（子进程边界符合注释声明的设计）。
- 无线程共享状态/GPU/信号维度；类型注解完整。

---

## src/features/edit/ai_editing/startup_diagnostics.py (行数: 121)

### 发现

- [AUD-14421] [Suggestion] `_profile_check` 混用两个 check_id（llm_profile / llm_credentials），成功时后者无记录
  - 位置: src/features/edit/ai_editing/startup_diagnostics.py:85-87
  - 证据:
    ```python
    if provider_kind not in {"ollama", "local_http", "openai_compat"} and not credential_available:
        return StartupCheck("llm_credentials", CheckState.FAIL, "AI profile 缺少凭据；不会静默降级到演示 planner")
    return StartupCheck("llm_profile", CheckState.PASS, f"AI profile 配置完整（{provider_kind}/{model_id}）")
    ```
  - 影响: 凭据缺失分支返回 id 为 `llm_credentials` 的 FAIL，但通过时返回的 PASS 挂在 `llm_profile` 上——`checks` 里永远不存在 `llm_credentials` 的非 FAIL 状态；按 check_id 聚合/渲染的消费方会看到一个从未"被检查过"却 FAIL 的条目，或需要特判。
  - 建议: 凭据检查单独成一条（缺凭据时 profile 合格 + credentials FAIL 两条），或统一挂在 llm_profile 下。
  - 置信度: 高（结构不一致确凿；当前 summary() 只按 state 聚合所以暂无可见 bug）。

### 通过项
- 无副作用红线遵守：只做 urlparse/属性读取，不发网络/付费请求，符合 docstring 声明。
- `production_ready` 门控逻辑自洽：需无 FAIL 且存在可用 production 级能力（排除 offline_help）；`tier` 为 CapabilityTier 枚举，`.value == "production"` 成立（capability_status.py:65-82 已核）。
- `openai_compat` 归入免凭据本地组与全库一致（capability_status.py:192、ai_editing_mixin.py:514 同集合，已核）——非缺陷。
- endpoint 校验（scheme ∈ http/https 且有 netloc）、model_id 非空、缺凭据 FAIL（不静默降级）均正确。
- feature flag 探测用 `getattr(..., lambda _: True)` 宽容默认，注释明确。
- 无线程/GPU/信号维度；类型注解完整。

---

## 附注（跨文件核对记录）

- `make_diagnostic`/`format_message` 的 `**message_vars` 契约是 AUD-14410/14414 的共同根因：模板变量缺失时静默回退原始模板（diagnostics.py:239-255），调用方无任何报错面。建议后续批次统一加"未知/缺失模板变量"的断言或测试。
- `TaskGraphExecutor`/`SingleWriterPolicy`/`TaskBudget`/`TaskState` 的语义按 task_graph.py 原文核实（含重入、NEEDS_USER_INPUT、30s 默认预算），本批次对 skill_runtime 的通过项结论均基于该核实。
