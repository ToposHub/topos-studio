"""P4.4 测试：开发者 Skill Studio —— 发布门禁与 SkillRelease（plan §10.6）。

> 发布产物是 ``SkillDefinition + graph template + manifest + eval version +
> content hash``。本地开发 Skill 醒目标"未验证"；第三方不得执行任意 Python、
> shell、文件系统或 UI 自动化。

验证 ``SkillReleaseGate`` / ``SkillRelease`` 的核心契约：

- **发布门禁**（``SkillReleaseGate.publish``）：必须先跑 eval 套件（P4.3）且全部
  PASS 才能发布；eval 未通过 → 拒绝（返回诊断，不抛错）。
- **发布产物** :class:`SkillRelease`：携带 manifest + graph template + eval version
  + content_hash + 发布时间 + 发布者；frozen + 可序列化。
- **source 分级**：local_dev 发布后仍标 ``unverified=True``（plan §10.6 红线）；
  official 模板直接发布也保留 official source。
- **版本**：发布前 draft.version 必须 ≥ 1.0.0（非 dev）；dev 草稿（0.x）不得发布。
- **registry 接线**：发布产物可注册进 :class:`SkillRegistry`（local_dev 仍 unverified）。

红线（plan §10.6 / §1.4 / §5.3）：

- 发布产物不含 secret / 绝对路径（继承 manifest 脱敏）。
- 第三方 skill 发布产物 executor_kind 受控；不携带任意 Python/shell 代码。
- 发布是幂等的：同草稿同版本重复发布 → 同 content_hash（视为同产物）。
"""
from __future__ import annotations


from src.features.ai_harness.skill_release import (
    PublishDenyCode,
    SkillRelease,
    SkillReleaseGate,
)
from src.features.ai_harness.skill_studio_drafts import (
    SkillDraft,
    new_draft_from_template,
)
from src.features.edit.ai_editing.skill_manifest import (
    OFFICIAL_VOICEOVER_POLISH,
    SkillSource,
)


def _make_ready_draft() -> SkillDraft:
    """构造一个 '可发布' 草稿（version ≥ 1.0.0）。"""
    d = new_draft_from_template(
        author="dev-alice",
        template=OFFICIAL_VOICEOVER_POLISH,
        title="口播精剪草稿",
    )
    # dev 草稿 0.1.0 → bump 到 1.0.0（major）以通过发布门禁
    return d.update_field("title", "口播精剪 v1", kind="major")


# ---------------------------------------------------------------------- #
# SkillRelease 数据模型
# ---------------------------------------------------------------------- #


class TestSkillReleaseModel:
    def test_release_carries_manifest_and_eval_version(self) -> None:
        release = SkillRelease.from_draft(
            draft=_make_ready_draft(),
            eval_suite_version="v1",
            released_by="dev-alice",
        )
        assert release.manifest.skill_id.startswith("draft.")
        assert release.eval_suite_version == "v1"
        assert release.content_hash  # 非空

    def test_release_carries_graph_template(self) -> None:
        from src.features.ai_harness.skill_studio_drafts import new_draft_from_workflow
        wf = {
            "workflow_id": "wf-001", "name": "T",
            "nodes": [{"node_id": "n1", "spec_id": "topos.tts.qwen3"}],
        }
        draft = new_draft_from_workflow(
            author="dev-bob", workflow_definition=wf, title="T",
        )
        draft = draft.update_field("title", "T v1", kind="major")
        release = SkillRelease.from_draft(draft=draft, eval_suite_version="v1", released_by="x")
        assert release.graph_template == wf
        assert release.workflow_hash  # workflow 来源时记 hash

    def test_release_content_hash_stable(self) -> None:
        d = _make_ready_draft()
        r1 = SkillRelease.from_draft(draft=d, eval_suite_version="v1", released_by="x")
        r2 = SkillRelease.from_draft(draft=d, eval_suite_version="v1", released_by="x")
        assert r1.content_hash == r2.content_hash

    def test_release_serializable(self) -> None:
        release = SkillRelease.from_draft(
            draft=_make_ready_draft(),
            eval_suite_version="v1",
            released_by="dev-alice",
        )
        d = release.to_dict()
        for key in ("release_id", "skill_id", "version", "content_hash",
                    "manifest", "graph_template", "eval_suite_version",
                    "released_by", "released_at", "unverified"):
            assert key in d


# ---------------------------------------------------------------------- #
# local_dev 发布后仍 unverified
# ---------------------------------------------------------------------- #


class TestLocalDevUnverified:
    def test_local_dev_release_marked_unverified(self) -> None:
        """红线：local_dev 发布产物 unverified=True。"""
        release = SkillRelease.from_draft(
            draft=_make_ready_draft(),
            eval_suite_version="v1",
            released_by="dev-alice",
        )
        assert release.unverified is True
        assert release.manifest.source == SkillSource.LOCAL_DEV

    def test_release_dict_exposes_unverified_prominently(self) -> None:
        release = SkillRelease.from_draft(
            draft=_make_ready_draft(),
            eval_suite_version="v1",
            released_by="dev-alice",
        )
        d = release.to_dict()
        assert d["unverified"] is True
        # manifest 也带 unverified
        assert d["manifest"]["unverified"] is True


# ---------------------------------------------------------------------- #
# 发布门禁
# ---------------------------------------------------------------------- #


class TestPublishGate:
    def test_publish_requires_passing_eval(self) -> None:
        gate = SkillReleaseGate()
        result = gate.publish(_make_ready_draft(), released_by="dev-alice")
        assert result.success
        assert result.release is not None
        # 门禁内部跑 eval 套件
        assert result.eval_report is not None
        assert result.eval_report.passed

    def test_publish_dev_version_blocked(self) -> None:
        """dev 草稿（0.x）不得发布。"""
        gate = SkillReleaseGate()
        dev_draft = new_draft_from_template(
            author="dev-alice",
            template=OFFICIAL_VOICEOVER_POLISH,
            title="still dev",
        )  # 0.1.0 dev
        result = gate.publish(dev_draft, released_by="dev-alice")
        assert not result.success
        assert result.deny_code == PublishDenyCode.DEV_VERSION_NOT_PUBLISHABLE

    def test_publish_eval_failure_blocked(self) -> None:
        """eval 失败的草稿不得发布。"""
        from dataclasses import replace
        from src.features.ai_harness.skill_studio_drafts import ModelRequirement
        gate = SkillReleaseGate()
        draft = _make_ready_draft()
        # 注入永远缺的能力 → success 用例必失败
        draft = replace(
            draft,
            model_requirements=draft.model_requirements + (
                ModelRequirement(capability="nonexistent_cap", required=True),
            ),
        )
        result = gate.publish(draft, released_by="dev-alice")
        assert not result.success
        assert result.deny_code == PublishDenyCode.EVAL_FAILED
        assert result.eval_report is not None
        assert not result.eval_report.passed

    def test_publish_arbitrary_code_executor_blocked(self) -> None:
        """红线：executor_kind 任意代码的草稿不得发布（门禁二次防御）。"""
        from dataclasses import replace

        class _ForgedArbitraryKind:
            """绕过 ExecutorKind 构造期防御的伪造执行后端（攻击模拟）。

            ExecutorKind("python_exec") 在构造期即被拒；这里模拟攻击者经
            反序列化 / replace 注入伪造对象，验证发布门禁的独立拒绝路径。
            """

            value = "python_exec"
            is_arbitrary_code = True

        gate = SkillReleaseGate()
        # _make_ready_draft 已 ≥ 1.0.0 → 先过版本门，逼出 executor 门
        draft = replace(_make_ready_draft(), executor_kind=_ForgedArbitraryKind())
        result = gate.publish(draft, released_by="dev-alice")
        # 无条件断言：门禁必须拒绝（success=False 时空跑通过是旧缺陷）
        assert result.success is False
        assert result.deny_code == PublishDenyCode.ARBITRARY_CODE_EXECUTOR
        assert result.release is None
        assert "arbitrary" in result.deny_message


# ---------------------------------------------------------------------- #
# 发布幂等性
# ---------------------------------------------------------------------- #


class TestPublishIdempotency:
    def test_same_draft_same_version_same_content_hash(self) -> None:
        gate = SkillReleaseGate()
        draft = _make_ready_draft()
        r1 = gate.publish(draft, released_by="dev-alice")
        r2 = gate.publish(draft, released_by="dev-alice")
        assert r1.success and r2.success
        assert r1.release.content_hash == r2.release.content_hash

    def test_different_versions_different_content_hash(self) -> None:
        gate = SkillReleaseGate()
        d1 = _make_ready_draft()
        d2 = d1.update_field("title", "v1.1", kind="minor")
        r1 = gate.publish(d1, released_by="x")
        r2 = gate.publish(d2, released_by="x")
        assert r1.release.content_hash != r2.release.content_hash


# ---------------------------------------------------------------------- #
# registry 接线
# ---------------------------------------------------------------------- #


class TestReleaseToRegistry:
    def test_register_release_into_skill_registry(self) -> None:
        """发布的 SkillRelease 可注册进 SkillRegistry（仍 unverified）。"""
        from src.features.edit.ai_editing.skill_registry import SkillRegistry
        gate = SkillReleaseGate()
        result = gate.publish(_make_ready_draft(), released_by="dev-alice")
        registry = SkillRegistry(register_official=False)
        diags = gate.register_into(registry, result.release)
        # 注册成功（无 error 级诊断）
        assert not any(str(d.severity).lower() == "error" for d in diags)
        got = registry.get(result.release.skill_id)
        assert got is not None
        assert got.source == SkillSource.LOCAL_DEV
        assert got.unverified is True

    def test_release_does_not_overwrite_official_skill(self) -> None:
        """红线：发布的 local_dev skill 不得覆盖同名 official skill。"""
        from src.features.edit.ai_editing.skill_registry import SkillRegistry
        registry = SkillRegistry(register_official=True)
        n_before = registry.count
        gate = SkillReleaseGate()
        result = gate.publish(_make_ready_draft(), released_by="dev-alice")
        gate.register_into(registry, result.release)
        # 草稿 skill_id 是 draft.* 前缀，与 official 不冲突
        assert registry.count == n_before + 1
        assert OFFICIAL_VOICEOVER_POLISH.skill_id != result.release.skill_id


# ---------------------------------------------------------------------- #
# 红线：发布产物不含 secret / 路径
# ---------------------------------------------------------------------- #


class TestReleaseNoSecrets:
    def test_release_manifest_no_abs_paths_or_secrets(self) -> None:
        release = SkillRelease.from_draft(
            draft=_make_ready_draft(),
            eval_suite_version="v1",
            released_by="dev-alice",
        )
        d = release.to_dict()
        # 序列化结果不含绝对路径 / openai key 形态
        text = repr(d)
        assert "/Users/" not in text
        assert "sk-" not in text  # openai key 前缀
        assert "password" not in text.lower()
