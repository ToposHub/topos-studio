# 批次报告：batch-225 — src/features/ai_harness/tests/

- 编号区间：AUD-22500–AUD-22599
- 审查日期：2026-09-28
- 文件数：8（1,861 行）
- 汇总：Critical 0 / Major 1 / Suggestion 10
- 说明：本批全部为单元测试文件，无线程/GPU/GLSL/信号维度问题；审查重点为测试有效性
  （空转断言、宽泛异常、私有 API 耦合）、覆盖与文档一致性、标记一致性与类型注解。
  所有发现均已对照被测源码（analysis_request.py / generation_adapter.py /
  editorial_skills.py / legacy_adapters.py / help_assistant.py / tests/conftest.py /
  pytest.ini）核实。

---

## src/features/ai_harness/tests/__init__.py (行数: 0)

### 发现

（无）

### 通过项

- 通过（无逻辑）：0 字节空文件，仅作包标记，无任何导出。

---

## src/features/ai_harness/tests/test_analysis_skills.py (行数: 299)

### 发现

- [AUD-22501] [Suggestion] `pytest.raises(Exception)` 宽泛断言掩盖领域错误类型
  - 位置: src/features/ai_harness/tests/test_analysis_skills.py:149
  - 证据:
    ```python
    def test_cloud_recipe_requires_cloud_policy(self):
        ...
        # CLOUD_ENHANCED + LOCAL_ONLY → AnalysisRequest 拒绝（构造期）
        with pytest.raises(Exception):
            build_analysis_request(
                project_id="p1", scope=scope,
                recipe_id=AnalysisRecipe.CLOUD_ENHANCED,
                data_policy=AnalysisDataPolicy.LOCAL_ONLY,
            )
    ```
  - 影响: 被测契约是 `AnalysisRequest.__post_init__` 抛出的 `ValueError`
    （analysis_request.py:223–227），且该方法还可能抛 `TypeError`（analysis_request.py:218–220）。
    用 `Exception` 匹配意味着将来 kwargs 拼写错误等真实 bug 抛出的 `TypeError` 也会让测试通过，
    红线测试（云配方必须配云策略）失去鉴别力——与「不得宽泛 except 掩盖领域错误」的测试侧同构问题。
  - 建议: 收窄为 `pytest.raises(ValueError, match="APPROVED_CLOUD")`。
  - 置信度: 高

- [AUD-22502] [Suggestion] 模块级辅助函数与测试方法缺类型注解
  - 位置: src/features/ai_harness/tests/test_analysis_skills.py:162
  - 证据:
    ```python
    def _analysis_with_router(asset_id, selection, quality=0.5, words=False):
        """构造带 router_selection 的 fake AssetAnalysis。"""
    ```
  - 影响: 全文件所有 `def test_*` 与 `_analysis_with_router` 均无参数/返回注解，
    与仓库强类型约定不符；fixture 工厂参数类型（str / float / bool）只能靠读实现推断。
  - 建议: 为辅助工厂补 `-> AssetAnalysis` 及参数注解；测试方法补 `-> None`。
  - 置信度: 高

### 通过项

- 红线断言充分：analyze_all 二次确认（L85–94）、pool 开关拒绝（L96–102）、max_assets=200 守门（L128–136）
- 状态提交一致性：is_confirmed=False（PROPOSED）断言正确（L126）
- 异常与取消路径：构造期拒绝路径有覆盖（见 AUD-22501 的收窄建议）
- 输入验证：空 scope 返回 None（L138–142）、空池提示（L104–107）覆盖到位
- 脱敏/引用红线：evidence 仅版本化引用、无 text 字段（L255–272）、OCR trust=untrusted（L274–283）
- 性能模式：无重复重型构造，fixture 复用合理

---

## src/features/ai_harness/tests/test_composite_render_skill.py (行数: 161)

### 发现

- [AUD-22503] [Suggestion] 缺 `pytestmark = pytest.mark.edit_core`，与同目录兄弟文件不一致
  - 位置: src/features/ai_harness/tests/test_composite_render_skill.py:14
  - 证据:
    ```python
    import pytest

    from src.features.ai_harness.editorial_skills import (
        SkillArtifact,
        build_composite_render_import,
    )
    ```
    （本文件无 `pytestmark = pytest.mark.edit_core`；同批 test_analysis_skills.py:23、
    test_editorial_skills.py:23、test_generation_adapter.py:24、test_help_assistant.py:19、
    test_invocation_models.py:23、test_invocation_router.py:18 均有）
  - 影响: `edit_core` 标记在 pytest.ini:23 注册、语义为「requires edit module」
    （tests/conftest.py:246–250）。本文件经 `editorial_skills` 传递硬依赖
    `src.features.edit.ai_editing.*`（editorial_skills.py:30–38），却未打标记；
    `pytest -m "not edit_core"` 选择时该文件仍会执行，在缺 edit 模块的环境中会
    Collection Error，破坏按标记分组的 CI 矩阵。
  - 建议: 与兄弟文件对齐，添加 `pytestmark = pytest.mark.edit_core`。
  - 置信度: 高

- [AUD-22504] [Suggestion] 四处直接调用私有方法 `_build_editorial_outcome`
  - 位置: src/features/ai_harness/tests/test_composite_render_skill.py:133
  - 证据:
    ```python
    outcome = adapter._build_editorial_outcome(
        _composite_invocation(), cancel_requested=lambda: False,
    )
    ```
    （L145、L151、L157 同样直接调用）
  - 影响: 测试与 `LegacyHarnessAdapter` 的私有实现耦合（legacy_adapters.py:433），
    重命名即断；且绕过了公开 `invoke()` 路径中 cancel_requested 与 runtime.is_cancelled
    的接线（legacy_adapters.py:387–391、L420），使「取消传播」仅在替身层面被验证。
  - 建议: 至少一条用例走公开 `invoke()` 入口验证 composite 分支端到端行为；
    其余用例可保留私有调用但标注为白盒测试。
  - 置信度: 高

### 通过项

- 红线断言充分：plan is None（不产时间线写入计划，L51）、未导入前 media_asset_id=""（L55）、
  PENDING_IMPORT 提示（L57、L140）
- 异常与取消路径：provider 返回 None 降级（L142–146）、无输出降级（L155–161）覆盖到位
- 输入验证：显式 rendered_media_asset_id 回退路径覆盖（L148–153）
- 未知 skill 拒绝、_RecordingRuntime 替身不触碰真实 runtime 状态，无副作用
- 类型注解：辅助函数 `_render_ref` / `_RecordingRuntime` 均有完整注解

---

## src/features/ai_harness/tests/test_editorial_skills.py (行数: 303)

### 发现

- [AUD-22505] [Suggestion] 模块文档声称覆盖「reading_speed 诊断」但无对应测试
  - 位置: src/features/ai_harness/tests/test_editorial_skills.py:6
  - 证据:
    ```python
    覆盖四个端到端 Skill 产物构造器：

    - build_auto_caption：转录 evidence → 字幕草稿 artifact + EditPlan（PREVIEW，
      ENSURE_SUBTITLE_TRACK + ADD_SUBTITLE）；无证据降级；reading_speed 诊断。
    ```
  - 影响: 全文件 grep 无任何 reading_speed / READING_SPEED_FAST 断言；源码中存在
    `READING_SPEED_FAST` 诊断码（editorial_skills.py，诊断码集合含
    READING_SPEED_FAST / TTS_MEDIA_DURATION_MISSING / TTS_MEDIA_NOT_MATERIALIZED）
    但全部未被本文件覆盖。文档承诺与实际覆盖不符，易让人误判 P2.2 验收完整。
  - 建议: 补一条超速/慢速 reading_speed 场景断言 READING_SPEED_FAST 诊断，或修正文档串。
  - 置信度: 高

- [AUD-22506] [Suggestion] `test_subtitle_op_carries_evidence_ref` 断言在 for 循环体内，可空转通过
  - 位置: src/features/ai_harness/tests/test_editorial_skills.py:85
  - 证据:
    ```python
    sub_ops = [op for op in out.plan.operations if op.kind == OperationKind.ADD_SUBTITLE]
    # 每条字幕都带 evidence_ref（可追溯）
    for op in sub_ops:
        assert op.params.get("evidence_ref")
        assert op.intent.get("evidence_ref")
    ```
  - 影响: 若 `build_auto_caption` 回归导致不再生成 ADD_SUBTITLE 操作，`sub_ops` 为空，
    循环体不执行，本测试静默通过——可追溯性红线（每条字幕带 evidence_ref）失去守护。
  - 建议: 循环前补 `assert sub_ops, "expected ADD_SUBTITLE operations"`。
  - 置信度: 高

- [AUD-22507] [Suggestion] 辅助函数与测试方法缺类型注解
  - 位置: src/features/ai_harness/tests/test_editorial_skills.py:43
  - 证据:
    ```python
    def _transcript_segs():
        return tuple(
            SemanticSegment(
    ```
  - 影响: `_transcript_segs` 无返回注解（实际返回 `tuple[SemanticSegment, ...]`），
    全部测试方法也无 `-> None`，与仓库强类型约定不符。
  - 建议: 补 `-> tuple[SemanticSegment, ...]` 与各方法 `-> None`。
  - 置信度: 高

### 通过项

- 状态提交一致性（本批核心红线）：COMMIT 强制降为 PREVIEW（L193–203）、
  TestAllPlansArePreview 三通道复核（L274–303）、rough_cut 不直接 commit
- 异常与取消路径：无转录降级 NO_TRANSCRIPT（L91–98）、无 variant 降级（L217–222）、
  无 TTS 仅产翻译不伪造音轨（L176–184）
- 输入验证：compile_plan 二次校验操作数与种类（L158–162）
- 产物信任级别：subtitle_draft trust=unverified（L78）
- manifest 契约：skill_id 集合精确匹配、produces_plan/roles/input_schema 字段断言（L230–266）
- 性能模式：无重复重型构造

---

## src/features/ai_harness/tests/test_generation_adapter.py (行数: 246)

### 发现

- [AUD-22500] [Major] `test_safety_blocked_recorded` 为空转测试，SAFETY_BLOCKED 安全路径零验证
  - 位置: src/features/ai_harness/tests/test_generation_adapter.py:238
  - 证据:
    ```python
    def test_safety_blocked_recorded(self, adapter):
        """若 provider 返回 safety.blocked → SAFETY_BLOCKED 诊断（不进 timeline）。"""
        ad, rt = adapter
        rid = ad.invoke(_gen_invocation(consent=_full_consent()))
        # deterministic provider 不 block；这里只验证诊断机制存在
        # （真实 block 场景由 provider 返回 blocked 候选触发）
        rec = rt.get_run(rid)
        # 至少不崩溃
        assert rec.status in (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED_WITH_WARNINGS, RunStatus.FAILED)
    ```
  - 影响: 源码中存在真实分支 `if getattr(cand.safety, "blocked", False): ... "code": "SAFETY_BLOCKED"`
    （generation_adapter.py:350–352），但本测试从未注入 blocked 候选、从未断言 SAFETY_BLOCKED
    诊断码；唯一断言覆盖了除 cancelled 外的几乎全部终态，恒真。测试名与 docstring 声称验证
    「安全拦截诊断记录」，实际对安全红线（被拦截内容不进 timeline）提供虚假覆盖信心——
    该分支若被回归删除，测试依然绿。
  - 建议: 注册一个返回 `safety.blocked=True` 候选的 stub provider，断言
    `rec.diagnostics` 含 `SAFETY_BLOCKED` 且 blocked 候选不产生 timeline 写入事件；
    或将本用例改名为 smoke 测试并另行补真实用例。
  - 置信度: 高

- [AUD-22508] [Suggestion] 将非法 modality 静默回退 IMAGE 固化为预期行为
  - 位置: src/features/ai_harness/tests/test_generation_adapter.py:97
  - 证据:
    ```python
    def test_invalid_modality_defaults_image(self):
        inv = _gen_invocation(modality="unknown")
        req = invocation_to_generation_request(inv)
        assert req.modality == GenerationModality.IMAGE
    ```
  - 影响: 用户侧输入（外部数据）非法时被静默降级而非拒绝/追问，与「外部数据须验证」
    红线的严格取向相悖（对照 router 对 unknown 文本显式不匹配的做法）；测试将其固化为
    契约后，后续收紧为显式报错需要先改测试。
  - 建议: 若属产品决策（宽容回退），在断言处补注释说明依据；否则改为断言
    WAITING_FOR_USER/诊断码，与 consent gate 的严格风格一致。
  - 置信度: 中

- [AUD-22509] [Suggestion] 模块级辅助函数缺类型注解
  - 位置: src/features/ai_harness/tests/test_generation_adapter.py:48
  - 证据:
    ```python
    def _full_consent(provider_id="deterministic-stub"):
        return GenerationConsent(
    ```
  - 影响: `_full_consent`（L48）、`_gen_service`（L56）、`_gen_invocation`（L62）
    均无参数/返回注解；`_gen_invocation` 的关键字参数含义（allow_generation/consent/prompt/modality）
    只能靠读实现，与仓库强类型约定不符。
  - 建议: 补注解（`_full_consent(provider_id: str = ...) -> GenerationConsent` 等）。
  - 置信度: 高

### 通过项

- consent gate 红线：无 consent → WAITING_FOR_USER + CONSENT_INCOMPLETE 且列出缺失字段（L164–178）、
  部分 consent 仍等待（L180–188）
- policy gate：allow_generation=False → FAILED/GENERATION_DISABLED（L197–202）
- 不自动进 timeline 红线：无 approval/commit/apply 事件（L211–219）、候选为引用非 clip（L221–229）
- 状态提交一致性：license UNKNOWN → SUCCEEDED_WITH_WARNINGS + LICENSE_UNKNOWN（L143–148）
- 请求映射：request_id 绑定 invocation_id（L121–124）、扁平 consent 字段聚合（L107–119）
- fixture 使用 yield 无泄漏；deterministic provider 无网络/磁盘副作用

---

## src/features/ai_harness/tests/test_help_assistant.py (行数: 209)

### 发现

- [AUD-22510] [Suggestion] 通过私有属性 `_entries` 直接装配测试索引
  - 位置: src/features/ai_harness/tests/test_help_assistant.py:38
  - 证据:
    ```python
    def _index_with(*entries: HelpEntry) -> HelpIndex:
        """绕过 build()，直接填 _entries（仅测试用）。"""
        idx = HelpIndex()
        for e in entries:
            idx._entries[e.entry_id] = e  # type: ignore[attr-defined]
        return idx
    ```
  - 影响: 测试与 `HelpIndex` 内部存储结构（dict 名、键语义）硬耦合，内部重构（如改用
    排序结构/加派生索引）会同时破坏全部 16 个用例；`# type: ignore[attr-defined]`
    也说明缺少受支持的装配缝。
  - 建议: 在 HelpIndex 上提供公共 `from_entries(...)` / `add(entry)` 测试缝，或在测试内
    构造 entries 后走 build() 的轻量入口。
  - 置信度: 高

### 通过项

- 不编造红线：not found → found=False + 固定文案、items 为空（L77–84）
- 输入验证：空查询返回 not found（L98–102）、无索引友好提示 + available/attach 语义（L126–142）
- 不可用功能标注未启用（L86–96）；entry_ids_for_query 空索引/limit 行为（L150–167）
- 异常路径：explain_diagnostic 未知码不 found（L188–191）；装配源抛错不阻断启动为源码
  显式决策（help_assistant.py:180–181 带 noqa 注释），测试仅固化该契约，不计为发现
- 类型注解：`_index_with` / `_entry` 均有完整注解

---

## src/features/ai_harness/tests/test_invocation_models.py (行数: 287)

### 发现

（无）

### 通过项

- 输入验证：project_id 必填（L47–49）、未知 source/permission/timeline_write 拒绝（L81–87、L124–133）、
  workflow/developer 源必须带 skill_id（L128–138）
- 状态机一致性：合法/非法迁移、终态闭锁（含终态到任意态的双重循环穷举，L195–199）、
  transition 抛 ValueError（L176–178）、draft 非 active（L258–260）
- 脱敏红线：绝对路径（Unix/Windows）、sk- key、api_key 赋值、Authorization 头、
  dict/list/tuple 递归、标量透传、中文安全文本不误伤（L207–247）、
  user_text 进 public dict 前脱敏（L149–156）、RunEvent payload 脱敏（L280–287）
- 不可变更新：with_skill 不改原对象且保留 invocation_id（L142–147）
- analyze_all_assets 默认 False + effective_asset_ids 空占位语义（L51–66）
- 无线程/GPU/信号维度内容；无异常吞咽；to_public_dict 形状断言具体

---

## src/features/ai_harness/tests/test_invocation_router.py (行数: 356)

### 发现

（无）

### 通过项

- plan §10.3 三句验收：分析所有素材（含 scope_hint 全部提示）/中文配音（语言确定性抽取、
  无 clarify）/怎么用→help（L39–62）；字幕请求不误入粗剪的反证（L64–70）
- 红线：unknown 不 matched 且不默认进 VariantPlanner（L72–78）、
  to_invocation 的 user_text 永远为空（L237–245）、help 不产 invocation（L255–261）
- 脱敏：路径/secret 不泄漏进 scope_hint/description/extracted_inputs（L286–302）
- 槽位与最小问题：缺语言只问一个问题（L146–152）、prefilled 跳过（L154–159）、
  placement_seconds 显式断言浮点类型 10.0（L272–278）
- 异常路径：help_search/skill_resolver 回调抛错不崩溃为源码显式韧性设计（注释 L211–213），测试固化
- 自定义规则优先级（含同关键词覆盖内置）语义明确（L310–331）
- 参数化矩阵 19 条短语意图映射，覆盖面好；无性能问题

---

## 汇总统计

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 1 | AUD-22500 |
| Suggestion | 10 | AUD-22501–AUD-22510 |
