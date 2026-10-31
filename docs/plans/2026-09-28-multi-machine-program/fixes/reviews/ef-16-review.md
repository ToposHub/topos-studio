# EF-16 复查报告（GLM5.3）

- 批次：EF-16（src/features/teaching_animation/ai 11 条 + src/features/ai_harness/tests 10 条，共 21 条）
- 提交哈希：`b6135bdd0`（b6135bdd02a1749341891a7b82c1b1f9383079f1，`[fix-E ef-16] … (21)`）
- 白名单：src/features/teaching_animation/ai、src/features/ai_harness/tests ∪ tests/ ∪ fixes/reports/
- 执行报告：fixes/reports/ef-16.md
- 复查日期：2026-10-01 ｜ 复查方式：只读（git show / grep / HEAD 源码逐条核实，未运行 pytest、未做任何 git 写操作）

## §6 八项检查逐项判定

1. **越界检查：通过**。`git show --stat b6135bdd0` 共 20 个文件：10 个 teaching_animation/ai 源文件、8 个 ai_harness/tests 文件、tests/teaching_animation/test_ef16_aud.py（新建）、fixes/reports/ef-16.md。全部 ⊆ 白名单 ∪ tests ∪ fixes/reports。未触碰 REMEDIATION_PLAN.md、e_flash_batches.json、FIXLOG.md；无 i18n locale 文件改动。
2. **真实修复：CONFIRMED（21/21）**。20 条 FIXED 逐条对照计划行指控，在当前 HEAD 独立核实 file:line 证据（见下表）；1 条 BLOCKED（AUD-22510）证据成立（见下）。
3. **红线扫描：通过**。新增行无裸 `except:`；无 `except Exception: pass`；无 UI 线程 GL/moderngl 调用（本批不涉 GL）；无 HDR 钳制/601/709 内容。两处 `except OSError: pass` 属 best-effort 清理路径（见"红线发现"）。
4. **测试质量：通过**。test_ef16_aud.py 28 用例逐 ID 正例+反例；ai_harness 10 条在各自套件内修复并断言修复语义（非空转）。断言均为修复语义断言（回退即失败），如 `assert sub_ops`（AUD-22506）、`assert outcome.edit_plan is not None`（AUD-22701）、`assert tampered.executor_kind.is_arbitrary_code is True`（AUD-22706）。
5. **并发**：AUD-27012 RLock 覆盖全部 6 个变更方法（revision.py:241,281,312,364,403,436,465），add_feedback latest 检查+写入同一原子段；8 线程并发测试在案。AUD-26819 `is_terminal` 持锁（export_job.py:229-235）。无锁内重活。
6. **资源**：AUD-26816 原子 no-clobber 发布（os.link → FileExistsError → 拒绝/经批准 os.replace），竞态窗口测试模拟覆盖；拒绝/取消路径 temp 清理。
7. **异常**：无吞错无痕（见红线发现的两处例外说明）；`except BaseException:`（test_legacy_adapters.py，AUD-22602）清理后原样 re-raise，不吞错。
8. **回归有效性**：报告抽 2 条回退验证（AUD-26700、AUD-26809）结论可信；本复查另给出 2 条回退建议（见下）。

## 逐 ID 判定表（21/21）

| ID | 计划指控摘要 | 报告结论 | 复查判定 | HEAD 证据（file:line） |
|----|-------------|---------|---------|----------------------|
| AUD-26700 | from_dict/apply_patch 数值字段不归一，畸形数据 TypeError | FIXED | CONFIRMED | brief.py:335-355（fps/duration/width/height/budget 走 `_to_float_or_none`/`_to_int_or_none`，:443/:464 定义，非法值抛带字段名 ValueError）；`_normalize_patch_values` :521-529 对 patch 数值键同规则 |
| AUD-26708 | set_background/add_audio_ref 任意 Mapping 直通；set_brand_theme 非法类别静默降级 | FIXED | CONFIRMED | command_catalog.py:315-318（color 必填非空 str）、:533-536（audio_id 必填）、:574-584（`_cast_map` 存在但非 Mapping → TeachingAIProposalError，缺省仍 `{}`） |
| AUD-26714 | lru_cache 冻结能力面与文档承诺矛盾 | FIXED | CONFIRMED | context_provider.py:60-63 docstring 声明 + :102-110 `invalidate_ai_capability_catalog()` → `cache_clear()`；测试 monkeypatch EXTENDED_COMMAND_NAMES 验证失效后直达 prompt |
| AUD-26802 | 文档承诺 version_of 但方法不存在 | FIXED | CONFIRMED | delivery_profile.py:274-299：`_install` 记录 displaced（`_versions` oldest-first），`version_of()` 返回旧版本元组；`__init__` extras 同经 `_install` |
| AUD-26809 | _check_overlap O(n²) 不因超限截断 | FIXED | CONFIRMED | design_validator.py:732-733 `if len(elements) > options.max_elements_error: return`（复杂度 error 已由先行 _check_complexity 阻断）；限内检查保留，双向测试锁定 |
| AUD-26812 | from_dict timestamp=None 崩溃、brief_revision 不收敛 | FIXED | CONFIRMED | error_codes.py:317-323 `float(... or 0.0)` 防护 + `int(brief_revision) if ... is not None else None` |
| AUD-26816 | 覆盖审批与 os.replace 间 TOCTOU | FIXED | CONFIRMED | export_job.py:152-192 `_publish_output`：os.link 原子 no-clobber → FileExistsError 未批准即返回 EXPORT_OVERWRITE_REQUIRED；硬链接不可用回退旧路径并注释残余竞态；测试模拟竞态窗口（monkeypatch os.link）验证不被覆盖 |
| AUD-26819 | 未用导入 Callable/Iterable；is_terminal 无锁 | FIXED | CONFIRMED | export_job.py:26 `from typing import Any, Mapping`（grep Callable/Iterable 零命中）；:229-235 is_terminal 在 `self._lock` 内读取，注释说明锁协议一致性诉求 |
| AUD-27001 | 文档称 hashable 实际 hash 抛 TypeError | FIXED | CONFIRMED | preview.py:126-128（模块 docstring "not hashable…identity via preview hash"）+ :131-133（类 docstring "Not hashable"，指引 canonical-hash）；测试锁定 `hash()` 抛 TypeError |
| AUD-27012 | RevisionLog 无锁，add_feedback check-then-act | FIXED | CONFIRMED | revision.py:241 `RLock`；:281/:312/:364/:403/:436/:465 六个变更方法全持锁；锁探针 + 8 线程恰好 1 成功测试在案 |
| AUD-27114 | required_layers 命名与 any() 语义矛盾 | FIXED | CONFIRMED | test_matrix.py：字段更名 `any_layer_of`（:72、:85 to_dict 键同步），docstring :8/:63-65 声明 any-of 语义，:229 `any(layer in layers_present ...)`；全仓 grep 无 `required_layers` 残留 |
| AUD-22505 | 文档声称 reading_speed 诊断无测试 | FIXED | CONFIRMED | test_editorial_skills.py:92-110 `test_reading_speed_fast_diagnostic`（27 字/24 帧 > 15cps → 断言 code/severity=line_ids） |
| AUD-22506 | for 循环内断言可空转 | FIXED | CONFIRMED | test_editorial_skills.py:85-89 循环前 `assert sub_ops, "…must emit ADD_SUBTITLE…"` |
| AUD-22510 | 测试经私有 `_entries` 装配索引 | BLOCKED | BLOCKED 成立 | 根因在 src/features/edit/ai_editing/help_index.py（白名单外，EF-16 未触碰该文件，git stat 证实）。核实：HelpIndex 公开 API 仅 `build(sources: HelpSources)`（help_index.py:197），写入 `_entries` 的全部是 build 内私有 `_add_*`；panel 载荷（`_add_panels` :360-382）不产生 shortcut_hint/next_steps，诊断 next_steps 来自硬编码 `_DIAGNOSTIC_EXPLANATIONS` —— 测试所需「panel 类目 + shortcut_hint + next_steps」字段组合无法经协议表达，加公共 from_entries()/add() 缝需改白名单外文件。测试文件 test_help_assistant.py:33-38 确以 `idx._entries[...]` 装配。记录合规，留待拥有 edit/ai_editing 白名单的批次处理 |
| AUD-22602 | fixture 构造期异常 EditSession 泄漏 | FIXED | CONFIRMED | test_legacy_adapters.py:163-181 bind 后构造序列 `try/except BaseException: EditSession.unbind(); raise`（清理后 re-raise，不吞错） |
| AUD-22608 | fake client 内部 assert 被宽异常吞掉 | FIXED | CONFIRMED | test_llm_role_model_client.py:20-43 `_Client` 记录 roles/json_mode/temperature/max_tokens，契约在 :60-81 测试正文断言；另补 timeout_ms=0 不得触达底层 client 断言 |
| AUD-22615 | 绑定契约测试未验证绑定 | FIXED | CONFIRMED | test_p1_host_wiring.py:73-100：正向（router 持有 `_skill_resolver` 且已注册→True/未注册→False）+ 新增负向 `test_router_intercepts_skill_when_resolver_denies`（resolver 拒绝 → matched=False、skill_id 清空、提示语） |
| AUD-22620 | docstring 与断言矛盾 | FIXED | CONFIRMED | test_p2_wiring.py:10-11 docstring 改为 target=editorial（editorial_skills 构造器路径），与 :147-162 断言一致 |
| AUD-22701 | 红线断言被 if 包裹空转 | FIXED | CONFIRMED | test_p4_acceptance.py:271-279 无条件 `assert outcome.edit_plan is not None` + mode 断言 |
| AUD-22706 | 未知值回退路径未覆盖 | FIXED | CONFIRMED | test_p4_security.py:192-207 `replace(release, executor_kind_value="python_exec")` → `is_arbitrary_code is True`，锁定 skill_release.py 未知值回退分支 |
| AUD-22714 | 测试方法缺 -> None | FIXED | CONFIRMED | test_runtime.py：`grep -c "def test_"` = 39，`grep -c "def test_.*-> None"` = 39（全量覆盖） |

## 红线发现

- 无裸 `except:`；无新增 `except Exception: pass`；无 GL/moderngl/HDR/色彩矩阵相关改动。
- 两处 `except OSError: pass`（export_job.py `_remove_temp_file` 与 `_publish_output` unlink temp）：特定异常类型 + docstring/注释明确说明"清理路径失败不得掩盖作业终态/不得让已完成导出失败"的取舍，属有意设计且可读留痕，判定可接受，不构成违规。
- `except BaseException:`（test_legacy_adapters.py:170，测试基建）与 AUD-30134 同模式：清理后原样 re-raise，不吞错。可接受。

## 回退验证建议（抽 2 条）

1. **AUD-26700**（src/features/teaching_animation/ai/brief.py:342-344）
   - 当前原文（逐字）：
     ```python
            duration_seconds=_to_float_or_none(
                data.get("duration_seconds"), "duration_seconds"),
     ```
   - 回退为：
     ```python
            duration_seconds=data.get("duration_seconds"),
     ```
   - 应失败节点：`tests/teaching_animation/test_ef16_aud.py::test_aud26700_from_dict_rejects_unparseable_number_as_value_error`（畸形数字串将以 TypeError 而非带字段名 ValueError 崩溃；`_to_float_or_none` 归一断言失败）
2. **AUD-26809**（src/features/teaching_animation/ai/design_validator.py:732-733）
   - 当前原文（逐字）：
     ```python
    if len(elements) > options.max_elements_error:
        return
     ```
   - 回退为：删除上述两行（恢复无条件 O(n²) 重叠检查）
   - 应失败节点：`tests/teaching_animation/test_ef16_aud.py::test_aud26809_overlap_check_skipped_beyond_hard_limit`（超硬上限场景将再次出现本应跳过的 MOTION_GRAPHIC_TEXT_OVERFLOW 重叠 warning）

## 过程备注（不扣分）

- 执行报告自称"未做任何 git add/commit"，提交 `b6135bdd0` 系中心后续统一收编（作者/时间与批 EF-17/18 相邻）。工作树内容与提交一致，无越界，不影响判定。
- 后续 efr-teaching-aiharness 机械清理提交（9240787a4）触碰了同目录文件；本复查全部证据取自当前 HEAD，确认 21 条修复仍全部在位、未被后续提交破坏。

## 最终结论

**PASS**。20 条 FIXED 全部独立核实为真实根因修复；1 条 BLOCKED（AUD-22510）证据链完整、处置合规（未越白名单、如实记录）。Critical 0 ｜ ISSUE 0。允许 --mark（含 AUD-22510 的 BLOCKED 标注）。
