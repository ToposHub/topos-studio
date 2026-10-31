# 批次报告 batch-141 — src/features/edit/ai_editing（8 文件，2,974 行）

审查人：ZCode 子智能体（全库审查 2026-09-28）
编号区间：AUD-14100–AUD-14199
维度：线程安全 / GPU 资源生命周期 / GLSL·HDR / 信号防递归 / 状态提交一致性 / 异常与取消 / 输入验证 / 性能 / 类型注解
说明：本批次为 AI 编辑协议层（plan 编译·校验·仓库·后置条件·技能·provider 路由），无 UI 控件、无 GLSL、无直接 GPU 资源；相应维度按不适用或通过处理。

---

## src/features/edit/ai_editing/plan_compiler.py (行数: 756)

### 发现

- [AUD-14100] [Major] `_compile_set_fade` 抛裸 `ValueError` 而非 `CompileError`，且 schema/validator 均不要求 fade 字段，异常穿透 `preview()` 的 `except CompileError`
  - 位置: src/features/edit/ai_editing/plan_compiler.py:616
  - 证据:
    ```python
    else:
        raise ValueError("set_clip_fade requires fade_in_duration or "
                         "fade_out_duration")
    ```
    而 `_compile_operation` 只转换 KeyError（plan_compiler.py:331-332）：
    ```python
    except KeyError as exc:
        raise CompileError(op.op_id, kind.value, f"missing required param {exc}") from exc
    ```
    消费端 transaction_service.py:361-362 仅捕 `CompileError`。schema.py:76 对 SET_CLIP_FADE 只要求 `("clip_id",)`；plan_validator.py:304-310 对该 kind 只查 clip 存在性。
  - 影响: 模型生成 `set_clip_fade{clip_id}`（无 fade 值）可通过结构校验与 domain 校验（状态 VALID），编译时抛出未转换的 ValueError，直接逃逸 `TransactionService.preview()` 的异常边界——请求路径未处理异常崩溃，且得不到可供 LLM 修复的 op 级诊断，违背模块自身“任一 operation 无法编译则整 plan 失败（CompileError）”的契约。
  - 建议: `_compile_set_fade` 接收 `op_id` 并改抛 `CompileError`；或在 `_compile_operation` 把 ValueError 一并转换（见 AUD-14101）；同时在 validator/schema 层为 SET_CLIP_FADE 补 fade 字段存在性校验（见 AUD-14106）。
  - 置信度: 高

- [AUD-14101] [Major] `_compile_operation` 仅把 `KeyError` 转 `CompileError`，`int()`/字符串索引强转产生的 `ValueError`/`TypeError` 全部逃逸
  - 位置: src/features/edit/ai_editing/plan_compiler.py:331
  - 证据（多处强转点，schema 层 `params` 只定义为 `{"type": "object"}` 不查值类型）:
    ```python
    frame = int(p.get("split_frame", p.get("frame", 0)))          # :171
    duration = int(p.get("duration", 12))                          # :180
    int(position) if position is not None else None                # :353
    return FrameRange(start=int(raw["start"]), duration=int(raw["duration"]))  # :513
    state = p.get("state", {})                                     # :738
    if "mute" in state: ... bool(state["mute"])                    # :741-744
    ```
    当 `state` 为字符串 `"mute"` 时 `"mute" in state` 为子串命中 True，`state["mute"]` 抛 `TypeError`。
  - 影响: 任何类型畸形的 params（duration="abc"、start=None、state="mute"、position="two" 等）都会以未分类异常穿透 compile 与 `preview()`/`_advance_projection` 之外的调用边界，破坏“compile 失败 → Diagnostic”的错误处理协议；这些输入全部来自 LLM 生成的外部数据，违反“外部数据须验证”铁律。
  - 建议: `_compile_operation` 的 except 扩为 `except (KeyError, ValueError, TypeError) as exc` 统一转 `CompileError`；或每个强转点显式 try/except 转 `CompileError`（携带 op_id）。
  - 置信度: 高

- [AUD-14102] [Suggestion] REMOVE_TRANSITION 编译分支重复，第二处为不可达死代码
  - 位置: src/features/edit/ai_editing/plan_compiler.py:320-328
  - 证据:
    ```python
    elif kind in (OperationKind.CREATE_TIMELINE, OperationKind.SET_RANGE,
                  OperationKind.ADD_REVIEW_NOTE, OperationKind.REQUEST_EXPORT,
                  OperationKind.REMOVE_TRANSITION):
        raise CompileError(...)
    ```
    而 :190-200 已有 `elif kind == OperationKind.REMOVE_TRANSITION:` 先行 raise。
  - 影响: 元组中的 REMOVE_TRANSITION 分支永不执行；两处错误文案不同（一处指向 transaction_service.apply 注入路径，一处称“not yet wired”），维护者易被误导。
  - 建议: 从 :321 的元组中删除 REMOVE_TRANSITION，或合并两分支统一文案。
  - 置信度: 高

- [AUD-14103] [Suggestion] `_compile_set_track_state` 抛 `CompileError` 时把 track_id 当 op_id 传入
  - 位置: src/features/edit/ai_editing/plan_compiler.py:753-755
  - 证据:
    ```python
    raise CompileError(
        p.get("track_id", "?"), "set_track_state",
        "set_track_state requires a 'state' dict with mute/lock/solo/visibility",
    )
    ```
    该函数签名没有 `op_id` 参数。
  - 影响: 诊断的 op_id 字段显示 track_id（或 "?"），LLM 修复回路无法定位到具体 operation。
  - 建议: 给 `_compile_set_track_state` 增加 `op_id` 参数，调用处传 `op.op_id`。
  - 置信度: 高

### 通过项
- 状态提交一致性：compiler 只产出 command、不 apply/不 commit/不 bump revision，提交边界集中在 EditProjectService（模块不变量成立）。
- 输入验证：update_subtitle / update_subtitle_track 的字段白名单 + 未知字段显式拒绝（:440-453, :486-492）；add_subtitle 的 track_id/duration 前置校验；`_coerce_clip_param_value` 把非法 payload 在 compile 期转为 CompileError（非吞错）。
- 一致性：确定性 clip_id/track_id/字幕 clip_id 推导（sha256(plan_id|op_id)），保证 preview 与 commit 两次 compile 产生相同 diff_hash（预览即提交）。
- COMPILABLE_OPERATIONS 作为"可声明=可编译"唯一真相源，manifest 与 validator 均由此派生；REMOVE_TRANSITION 未列入（与显式 raise 一致）。
- `effect` / `remove_effect` param_type 与命令层 apply 契约吻合（commands.py:2677-2712 支持 dict 描述符与字符串 effect_type）。
- 线程安全 / GPU 资源 / GLSL·HDR / 信号防递归：不涉及（纯编译层，无共享可变状态、无 OpenGL、无 Qt 信号）。
- 性能：lazy import 仅按分支加载；无热点循环问题。

---

## src/features/edit/ai_editing/plan_repository.py (行数: 268)

### 发现

- [AUD-14104] [Suggestion] `StoredPlan.from_dict` 直接信任持久化的 `content_hash`，导入时不复核哈希与 plan 内容的一致性
  - 位置: src/features/edit/ai_editing/plan_repository.py:107-109
  - 证据:
    ```python
    return cls(
        plan=plan,
        canonical_json=data["canonical_json"].encode("utf-8"),
        content_hash=str(data["content_hash"]),
        ...
    )
    ```
  - 影响: grant 绑定的是 `content_hash`。若 `EditPlan.to_dict/from_dict` 在版本演化间不再互逆（或持久化文件被篡改/损坏但字段齐全），恢复后的 plan 对象与其携带的 hash 静默脱钩——“用户批准的那一份 hash”不再代表实际将 commit 的 plan 内容，破坏“预览即提交”（§1.4）不变量且无任何诊断。
  - 建议: `from_dict` 内用 `_sha256(canonical_json)`（或对 plan 重新 `_canonical_json`）与传入 hash 比对，不一致时丢弃该条目（import_all 已具备跳过机制）。
  - 置信度: 中

- [AUD-14105] [Suggestion] `find_by_hash` O(n) 线性扫描，且仓库无任何淘汰/上限策略
  - 位置: src/features/edit/ai_editing/plan_repository.py:184-189
  - 证据:
    ```python
    def find_by_hash(self, content_hash: str) -> StoredPlan | None:
        with self._lock:
            for sp in self._by_id.values():
                if sp.content_hash == content_hash:
                    return sp
        return None
    ```
  - 影响: commit 路径按 hash 校验批准副本时随仓库规模线性增长；长会话中 AI plan 只进不出（仅显式 remove/clear），内存无界增长。
  - 建议: 增加 `_by_hash` 反向索引（store/attach 时同步维护）；为仓库设置容量上限或 LRU 淘汰。
  - 置信度: 高

### 通过项
- 线程安全：全部读写经 RLock 保护；`export_all` 在锁内取快照、锁外序列化（StoredPlan frozen，快照安全）。
- 状态提交一致性：同 plan_id 重存仅在 content_hash 一致时保留旧 preview 绑定（内容变化自动丢弃过期 preview）；`is_preview_stale` 对缺失 plan/preview/指纹一致按 stale 处理（fail-safe）。
- 异常与取消：`import_all` 单条损坏（KeyError/TypeError/ValueError）跳过不阻塞整体恢复；`from_dict` 对 preview 反序列化失败降级为 None 并注明“恢复后必须重新 preview”。
- 输入验证：import_all 对非 dict 条目、缺 "plan" 字段条目过滤。
- 类型注解完整；纯内存实现不涉及 GPU/GLSL/信号。

---

## src/features/edit/ai_editing/plan_validator.py (行数: 569)

### 发现

- [AUD-14106] [Major] SET_CLIP_FADE 的 fade 字段、ADD_TRANSITION 的 duration 等值约束在 schema/validator 两层全部缺失，畸形 plan 以 VALID 放行后必然在 compile 期以未分类异常炸出
  - 位置: src/features/edit/ai_editing/plan_validator.py:304-310
  - 证据: SET_CLIP_FADE 仅参与"clip 存在性"检查：
    ```python
    if kind in (OperationKind.MOVE_CLIP, OperationKind.TRIM_CLIP,
                OperationKind.SPLIT_CLIP, OperationKind.DELETE_CLIP,
                ...
                OperationKind.SET_CLIP_FADE,
                OperationKind.SET_EFFECT, OperationKind.REMOVE_EFFECT, ...):
        clip_id = params.get("clip_id")
        if clip_id:
            clip = sequence.get_clip_by_id(clip_id)
    ```
    ADD_TRANSITION 检查（:398-416）只覆盖 transition_type/track/邻接，不查 `duration` 的类型与正负（compiler 默认 `int(p.get("duration", 12))`）。
  - 影响: `set_clip_fade` 缺 fade 值、`duration: "abc"` 等来自 LLM 的畸形 params 通过全部校验（状态 VALID），随后 compile 抛裸 ValueError/TypeError 逃逸 preview 边界（与 AUD-14100/14101 叠加成完整故障链）——校验层本应在此产出可修复诊断而非放行。
  - 建议: 在 `_validate_operation_domain` 为 SET_CLIP_FADE 增加 fade_in/fade_out 至少其一且为非负 int 的检查；为 ADD_TRANSITION 增加 duration 为正 int 的检查（复用 SET_MARKER/ADD_SUBTITLE 已有的 `not isinstance(x, int) or isinstance(x, bool)` 模式，:384-395, :453）。
  - 置信度: 高

- [AUD-14107] [Suggestion] 同一 from-import 中 `get_effect_capability` 重复导入两次
  - 位置: src/features/edit/ai_editing/plan_validator.py:27-28
  - 证据:
    ```python
    from src.features.edit.ai_editing.capability_manifest import (
        get_effect_capability,
        get_effect_capability,
        is_effect_known,
        is_operation_supported,
    )
    ```
  - 影响: 冗余（lint F811 级），无功能危害；暗示合并 PR 时未清理。
  - 建议: 删除重复行。
  - 置信度: 高

- [AUD-14108] [Suggestion] `_clip_not_found_diag` 复用 `TRACK_NOT_FOUND` 诊断码上报 clip 缺失
  - 位置: src/features/edit/ai_editing/plan_validator.py:483-490
  - 证据:
    ```python
    def _clip_not_found_diag(op: EditOperation, clip_id: str, lang: str) -> Diagnostic:
        return Diagnostic(
            severity=DiagnosticSeverity.ERROR,
            code=TaepDiagnosticCode.TRACK_NOT_FOUND,  # 复用：找不到引用对象
            message=f"clip {clip_id!r} not found in timeline (op {op.op_id})",
    ```
  - 影响: 按诊断码路由的调用方（与 LLM 修复回路）会把"clip 不存在"误读为"轨道不存在"，suggested_action 指向错误方向（如去建轨）。
  - 建议: 若诊断码枚举无 CLIP_NOT_FOUND，则新增专用码；至少在 make_diagnostic 的 detail 中显式区分对象类型。
  - 置信度: 高

### 通过项
- 状态提交一致性：revision 乐观锁（base_revision != sequence.revision → REVISION_CONFLICT，状态映射 STALE 优先于 REJECTED）。
- 输入验证（已覆盖项）：asset 存在/离线、source range 越界、track 类型匹配、字幕轨类型/锁定态、字幕 start/duration 严格 int 校验（含 bool 排除）、SET_MARKER frame 非负、transition 邻接（端点相接而非仅索引相邻）、effect registry 存在性 + preview_only/unsupported 交付拒绝 + 参数 min/max/choices 范围。
- M3.3 顺序投影：`_advance_projection` 将前序结构性 op 叠加到投影 sequence，修复 add→set 误判（其宽 except 有注释说明且仅影响投影、诊断由他处产生，符合不吞错原则）。
- 异常路径：schema 编译期早拒绝失败时降级为 manifest 级白名单检查（compilable_set 为空分支），capability 早拒绝避免重复诊断。
- 性能：逐 op 校验为 O(ops)，`_advance_projection` 每 op 一次轻量 compile，量级可控。
- 不涉及 GPU/GLSL/HDR/Qt 信号；类型注解完整。

---

## src/features/edit/ai_editing/postcondition_checker.py (行数: 270)

### 发现

- [AUD-14109] [Major] 未注册任何 postcondition 的任务种类返回 `outcome=PASSED`，而唯一消费者按 `outcome != "failed"` 判定成功——零检查即 SUCCEEDED
  - 位置: src/features/edit/ai_editing/postcondition_checker.py:156-157
  - 证据:
    ```python
    if not conditions:
        return VerificationResult(outcome=VerificationOutcome.PASSED)
    ```
    `verified` 属性本为防此情形设计（:60-68: "a run with no registered postcondition is not silently trusted"），但消费者 chat_run_driver.py:414 直接用 outcome：
    ```python
    success = verification.outcome != "failed"
    ```
    `create_default_checker`（:241-256）只为 CHAT/MOTION_GRAPHIC/IMAGE/VIDEO/MUSIC 注册检查；AgentTaskKind（agent_models.py:186-201）另有 AGENT/SPEECH_TO_TEXT/TEXT_TO_SPEECH/VISION_UNDERSTANDING/EMBEDDING/ANALYSIS/EXPORT/UNKNOWN 共 8 个种类无任何注册。
  - 影响: 这些种类的任务（如 export、embedding、analysis 经 chat_run_driver 驱动时）无需任何确定性验证即被提升为成功，直接违背模块 docstring "the model never self-reports success"（plan §4.2 rule 4）与 plan §A6 的验收目标；`verified` 防线形同虚设。
  - 建议: 无注册条件时返回 `outcome=FAILED`（或新增 UNKNOWN outcome 并让 `is_runnable` 语义 fail-closed）；或强制消费者改用 `verification.verified`/`all_passed`。两者取一即可闭合。
  - 置信度: 高

- [AUD-14110] [Suggestion] `no_tool_failure` 已定义并导出但从未注册到任何任务种类，"工具失败阻断成功"门从未生效
  - 位置: src/features/edit/ai_editing/postcondition_checker.py:225-238
  - 证据: 定义处：
    ```python
    def no_tool_failure() -> Postcondition:
        """No tool invocation ended in failure or denial."""
    ```
    `create_default_checker`（:241-256）注册列表中不含它；全库 grep 无其他引用。
  - 影响: 工具调用失败/被拒不会阻止任务判成功，与 §A6 "Separates 'the model returned text' from 'the task actually succeeded'" 的意图存在缺口。
  - 建议: 将 `no_tool_failure()` 注册进 AGENT（及含工具回路的任务种类）的默认检查集，或删除以免误导。
  - 置信度: 高

- [AUD-14111] [Suggestion] `html_artifact_present` 回退条件过宽：任意非 HTML 产物也判通过
  - 位置: src/features/edit/ai_editing/postcondition_checker.py:201-204
  - 证据:
    ```python
    def _check(ctx: VerificationContext) -> bool:
        if ArtifactKind.HTML_DOCUMENT in ctx.artifact_kinds:
            return True
        return bool(ctx.artifact_refs)
    ```
  - 影响: artifact_kinds 缺失（未并行提供）时，一个文本/图片句柄即可让 Motion Graphic 任务的 HTML 基线检查通过；文档已声明为 baseline，但回退方向应偏严。
  - 建议: artifact_kinds 非空但不含 HTML_DOCUMENT 时直接判 False；仅 kinds 完全缺失时才回退 `bool(artifact_refs)`。
  - 置信度: 高

### 通过项
- 异常路径：`check()` 内 `except Exception` 将检查器崩溃转为该项失败（有 pragma 注释，非吞错，方向保守）。
- 数据建模：VerificationResult/Context/Postcondition 均 frozen dataclass；`__post_init__` 对非法 outcome 归一为 FAILED（fail-closed）；Postcondition 空名拒绝。
- PARTIAL/FAILED 分级逻辑正确（有 passed 有 failed → PARTIAL）。
- 无状态共享（registry 仅构造期写入）；类型注解完整；不涉及 GPU/GLSL/HDR/信号。

---

## src/features/edit/ai_editing/privacy_guard.py (行数: 75)

### 发现

- [AUD-14112] [Suggestion] `redact_mapping` 不递归 list/tuple 内的 Mapping/str——嵌套消息列表中的密钥与路径原样透传，且该函数当前全库无调用方
  - 位置: src/features/edit/ai_editing/privacy_guard.py:44-49
  - 证据:
    ```python
    elif isinstance(item, Mapping):
        result[str(key)] = redact_mapping(item)
    elif isinstance(item, str):
        result[str(key)] = redact_text(item)
    else:
        result[str(key)] = item   # list/tuple 整体原样保留
    ```
  - 影响: 对隐私脱敏函数而言，`{"messages": [{"content": "…sk-abc123…"}]}` 这类最常见的审计载荷形状不会被脱敏（潜在密钥/机器路径泄露）。缓解因素：grep 确认 src 内无任何调用方（仅 `redact_text` 被使用），风险当前为潜伏状态。
  - 建议: else 分支对 list/tuple 逐项应用同样的 Mapping/str 递归规则；同时要么接入声称的 consumers（模块 docstring 所称 transports/audit adapters），要么收缩 docstring。
  - 置信度: 高

- [AUD-14113] [Suggestion] `PrivacyViolation` 定义并导出但全库无 raise/catch，死代码
  - 位置: src/features/edit/ai_editing/privacy_guard.py:18-20
  - 证据:
    ```python
    class PrivacyViolation(ValueError):
        """A caller attempted an upload or retention action without consent."""
    ```
  - 影响: `check_media_upload` 对违规一律返回 `UploadDecision(False, …)` 而非抛该异常；调用方无从区分"违规拒绝"与"异常路径"，导出的异常类型无消费者。
  - 建议: 删除，或在 retention/audit 相关入口真正投入使用。
  - 置信度: 高

### 通过项
- 输入验证：`check_media_upload` 三重门（空素材→拒绝；未确认→拒绝；profile 未授权→拒绝），全部 fail-closed。
- 正则脱敏覆盖 bearer/sk- 前缀/key=value 形态；`match.lastindex` 保护无组模式；绝对路径脱敏不存储原始 prompt。
- 类型注解完整；纯函数无线程/GPU/信号维度。

---

## src/features/edit/ai_editing/professional_skills.py (行数: 376)

### 发现

- [AUD-14114] [Suggestion] `register_recommended_skills` 用宽泛 `except Exception: continue` 静默吞掉注册失败
  - 位置: src/features/edit/ai_editing/professional_skills.py:356-357
  - 证据:
    ```python
        try:
            manifest = skill.to_manifest()
            diags = registry.register(manifest, enabled=True)
            if not diags:
                registered.append(skill.skill_id)
        except Exception:  # noqa: BLE001
            continue
    ```
  - 影响: to_manifest/register 的领域错误（如 SkillManifest 构造校验失败）被无声吞掉，仅体现为"返回列表少一个 id"，失败原因完全不可见——违反"不得宽泛 except 掩盖领域错误"铁律。
  - 建议: 至少捕获后记录诊断（返回 (registered, errors) 或日志）；缩窄异常类型。
  - 置信度: 高

- [AUD-14115] [Suggestion] `SKILL_ALIASES` 把 `topos.interview_highlights` 映射到另一个不同技能 `topos.voiceover_polish`，且整个别名表无任何调用方
  - 位置: src/features/edit/ai_editing/professional_skills.py:318-336
  - 证据:
    ```python
    SKILL_ALIASES: dict[str, str] = {
        "topos.product_ad": "topos.product_ad_30s",
        "topos.product_ad_30s": "topos.product_ad",
        # Interview highlights vs voiceover polish are distinct but related
        "topos.interview_highlights": "topos.voiceover_polish",
    }
    ```
    注释自述两技能 "distinct"；grep 确认 `resolve_skill_id`/`SKILL_ALIASES` 在 src 中零调用。
  - 影响: 一旦接线，`recommended_skill_registry()[resolve_skill_id("topos.interview_highlights")]` 必 KeyError（voiceover_polish 不在推荐表）或从官方注册表取到错误技能定义——访谈精剪请求被路由到配音润色。当前为潜伏缺陷（无调用方）。
  - 建议: 删除该映射（保留 product_ad 双向别名）；若保留表则同步补 `resolve_skill_id` 的调用与测试。
  - 置信度: 高

### 通过项
- 输入验证：`ProfessionalSkill.__post_init__` 校验 skill_id 非空、domain ∈ 4 值集合、stability_tier ∈ 3 值集合。
- 风险建模：to_manifest 的风险分级保守（确认集含写操作 → WRITE_REQUIRES_APPROVAL；纯读 → READ_ONLY）；非 ga 一律 LOCAL_DEV source（不可冒充官方）。
- 8 个推荐技能定义均为纯 frozen 数据，无运行时副作用；类型注解完整；不涉及线程/GPU/信号。

---

## src/features/edit/ai_editing/profile_tts_provider.py (行数: 229)

### 发现

- [AUD-14116] [Suggestion] `_remote_speaker` 静默丢弃用户配置的非 `zh_` 前缀音色，强制替换为硬编码中文女声
  - 位置: src/features/edit/ai_editing/profile_tts_provider.py:147-152
  - 证据:
    ```python
    @staticmethod
    def _remote_speaker(settings: Any) -> str:
        speaker = str(getattr(settings, "speaker", "") or "")
        if speaker.startswith("zh_"):
            return speaker
        return "zh_female_vv_uranus_bigtts"
    ```
  - 影响: 远程路径当前仅支持火山方舟（`_remote` :60-68），其音色族含 `en_` 等非 zh 前缀音色；用户选择英文/其他音色时被静默改声且无任何提示。反向同样不成立——以 `zh_` 开头的本地模型音色名会被原样传给远程端而未必有效。启发式双向皆错。
  - 建议: 按 profile 能力声明可选音色并校验（无效音色显式报错），而非前缀猜测 + 静默覆盖；至少把覆盖行为记录到返回元数据/日志。
  - 置信度: 中

- [AUD-14117] [Suggestion] 远程合成逐 cue 同步调用 `provider.generate`（无超时/取消）+ 每 cue 一次 ffmpeg 子进程，长字幕轨长时间阻塞调用线程
  - 位置: src/features/edit/ai_editing/profile_tts_provider.py:108-138
  - 证据:
    ```python
    for index, cue in enumerate(clean_cues):
        request = GenerationRequest(...)
        result = provider.generate(request)
        if result.error_code or not result.candidates:
            raise RuntimeError(result.error_message or "远程 TTS 未返回音频")
        waveform, sample_rate = self._decode_audio(handle)
    ```
    `_decode_audio` 自身有 `timeout=60`（:166-172），但 `provider.generate` 无超时，循环亦无取消检查点。
  - 影响: N 条 cue = N 次付费远程往返 + N 次 ffmpeg 解码，任一远程挂起则整段合成无限期阻塞；若调用方在 UI 线程则界面冻结（调用线程未在本批次内确认，按风险记录）。
  - 建议: 循环内增加取消 token 检查；为 generate 传入预算/超时；考虑并发上限受控的批量提交。
  - 置信度: 中

### 通过项
- 状态一致性：远程布局与本地 Qwen3TTSProvider 契约同构（顺序拼接 + 0.3s 句间静音 + first_frame=min(start_frame)，tts_provider.py:236-299），下游时间线语义一致。
- 异常路径：ffmpeg 缺失/解码失败/空音频/采样率异常均转带中文可操作信息的 RuntimeError；subprocess 使用 check=True + timeout=60 + 显式异常包装。
- 音频处理：f32→int16 PCM 的 `np.clip(-1.0, 1.0)` 属音频量化标准操作，不构成 HDR 无理由 clamp 违例；`np.frombuffer(...).copy()` 避免只读缓冲被就地 fade 修改。
- 输入验证：空 cue 列表、无 track_id、非正 duration 在 compile 层前置拒绝；`clean_cues` 过滤空文本。
- is_model_ready 对远程 profile 校验 endpoint + 凭据存在；类型注解完整；无 GPU/GLSL/Qt 信号维度。

---

## src/features/edit/ai_editing/provider_capabilities.py (行数: 431)

### 发现

（无）

### 通过项
- 线程安全：全模块为 frozen dataclass + 纯函数，无共享可变状态。
- 状态提交一致性/异常路径：CapabilityResolution 对未知 availability 归一为 UNCONFIGURED（fail-closed，过期值不可能开启生成路径）；demo 标记强制覆盖 AVAILABLE → DEMO_ONLY；`capabilities_for_mode` 未知 mode 回落 LANGUAGE（不会误触付费媒体生成）；assistant_plugins 导入失败仅 (ImportError, AttributeError) 且有注释说明。
- 输入验证：EndpointStyle/CapabilityKind/Availability 均有 KNOWN 白名单并在 `__post_init__` 归一；`normalize_endpoint` 对无 scheme/无 netloc/ValueError 均回退原值不破坏用户配置（仅传输层规范化，不覆写原始配置）。
- 性能：normalize/detect 均为轻量字符串操作，`/v1/v1` 折叠为循环消除任意重复。
- to_dict/from_dict 往返字段完整；类型注解完整；不涉及 GPU/GLSL/HDR/Qt 信号。
- 附注（不计发现）：`detect_endpoint_issue` 中 `raw.startswith("http://")`/`("https://")` 两条件因已含 "://" 恒为冗余（provider_capabilities.py:413）；`CapabilityResolution.is_runnable` 的 `not (self.demo and is_blocking(...))` 恒真（:301-304）。均为无害冗余。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 4 | AUD-14100 / AUD-14101 / AUD-14106 / AUD-14109 |
| Suggestion | 14 | AUD-14102–14105, AUD-14107, AUD-14108, AUD-14110–14117 |

Major 主线（两条独立故障链）：
1. 编译错误协议破裂：validator 放行 + compiler 抛裸 ValueError/TypeError + transaction_service 只捕 CompileError（AUD-14100 / AUD-14101 / AUD-14106）。
2. 零检查即 SUCCEEDED：无 postcondition 任务种类 outcome=PASSED 且消费者按 outcome 判成功（AUD-14109）。

其余为隐私脱敏缺口（潜伏）、注册静默吞错、技能别名错映射、TTS 音色静默覆盖等潜伏缺陷与质量改进。
