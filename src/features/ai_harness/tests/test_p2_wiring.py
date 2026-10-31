"""P2.3 Skill 接入 HarnessRuntime 测试（plan §10.4 P2.3）。

覆盖 P2 skill 的注册 + catalog 显示 + router 路由接线：

- register_p2_skills：把 topos.auto_caption / topos.subtitle_localization 注册
  进 SkillRegistry；重复注册幂等。
- SkillCatalogView 显示 P2 skill 卡（含 P2_SKILL_META 元信息）。
- InvocationRouter 把"生成字幕"路由到 topos.auto_caption，"配音"路由到
  topos.subtitle_localization。
- SkillCompiler 能编译 P2 skill 的 invocation（target=editorial，走
  editorial_skills 构造器路径，而非 M7 workflow）。

红线（plan §10.4 P2.3）：四个官方 Skill 在聊天优先呈现，节点图复用相同模板。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.ai_harness.editorial_skills import register_p2_skills
from src.features.ai_harness.invocation_models import (
    Invocation, InvocationScope, InvocationSource, RunPolicy,
)
from src.features.ai_harness.invocation_router import InvocationRouter
from src.features.ai_harness.skill_catalog import SkillCatalogView
from src.features.ai_harness.skill_compiler import (
    SkillCompiler, definition_from_manifest,
)
from src.features.edit.ai_editing.editorial_models import SegmentKind
from src.features.edit.ai_editing.skill_registry import SkillRegistry


@pytest.fixture
def registry_with_p2():
    reg = SkillRegistry()  # 注册 3 个既有官方 skill
    register_p2_skills(reg)  # +2 个 P2 skill
    return reg


# ================================================================
# register_p2_skills
# ================================================================


class TestRegisterP2:
    def test_adds_official_skills(self, registry_with_p2):
        ids = {m.skill_id for m in registry_with_p2.list_skills()}
        assert "topos.auto_caption" in ids
        assert "topos.subtitle_localization" in ids
        assert "topos.composite_render_import" in ids
        # 既有 3 个 + P2 3 个（含 plan §9.5 composite）= 6
        assert registry_with_p2.count == 6

    def test_idempotent(self, registry_with_p2):
        """重复注册不报错、不翻倍。"""
        register_p2_skills(registry_with_p2)
        assert registry_with_p2.count == 6

    def test_p2_skills_enabled(self, registry_with_p2):
        assert registry_with_p2.is_enabled("topos.auto_caption")
        assert registry_with_p2.is_enabled("topos.subtitle_localization")


# ================================================================
# SkillCatalogView 显示 P2 skill
# ================================================================


class TestCatalogShowsP2:
    def test_p2_cards_visible(self, registry_with_p2):
        view = SkillCatalogView(
            registry_with_p2,
            available_evidence_kinds=(SegmentKind.TRANSCRIPT.value,),
            installed_models=("qwen3_asr",),
            has_profile=True,
        )
        cards = view.cards()
        ids = {c.skill_id for c in cards}
        assert "topos.auto_caption" in ids
        assert "topos.subtitle_localization" in ids

    def test_auto_caption_card_has_p2_meta(self, registry_with_p2):
        view = SkillCatalogView(registry_with_p2)
        c = view.card("topos.auto_caption")
        assert c is not None
        assert c.category == "字幕"  # P2_SKILL_META
        assert c.trigger_examples  # 有触发示例

    def test_subtitle_localization_card(self, registry_with_p2):
        view = SkillCatalogView(registry_with_p2)
        c = view.card("topos.subtitle_localization")
        assert c is not None
        assert c.category == "配音"

    def test_p2_prereq_with_transcript(self, registry_with_p2):
        """有 transcript 证据 + qwen3_asr → auto_caption ready。"""
        view = SkillCatalogView(
            registry_with_p2,
            available_evidence_kinds=(SegmentKind.TRANSCRIPT.value,),
            installed_models=("qwen3_asr",),
            has_profile=True,
        )
        c = view.card("topos.auto_caption")
        assert c.prereq.ready is True

    def test_p2_prereq_without_evidence_not_ready(self, registry_with_p2):
        view = SkillCatalogView(
            registry_with_p2, available_evidence_kinds=(), has_profile=True,
        )
        c = view.card("topos.auto_caption")
        assert c.prereq.ready is False


# ================================================================
# InvocationRouter 路由到 P2 skill
# ================================================================


class TestRouterP2Routing:
    def test_subtitle_routes_to_auto_caption(self):
        rt = InvocationRouter()
        r = rt.route("生成字幕")
        assert r.skill_id == "topos.auto_caption"

    def test_dubbing_routes_to_localization(self):
        rt = InvocationRouter()
        r = rt.route("给字幕配中文配音")
        assert r.skill_id == "topos.subtitle_localization"
        # 中文已被抽到
        assert r.extracted_inputs.get("target_language") == "中文"

    def test_skill_resolver_validates_p2(self, registry_with_p2):
        """router 的 skill_resolver 校验 P2 skill 已注册启用。"""
        rt = InvocationRouter(skill_resolver=lambda sid: registry_with_p2.is_enabled(sid))
        r = rt.route("生成字幕")
        assert r.matched is True
        assert r.skill_id == "topos.auto_caption"


# ================================================================
# SkillCompiler 编译 P2 skill
# ================================================================


class TestCompilerP2:
    def test_compile_auto_caption(self, registry_with_p2):
        """auto_caption → target=editorial（走 editorial_skills 构造器，非 M7 workflow）。"""
        compiler = SkillCompiler()
        manifest = registry_with_p2.require("topos.auto_caption")
        definition = definition_from_manifest(manifest, is_official=True)
        inv = Invocation(
            source=InvocationSource.WORKFLOW, skill_id="topos.auto_caption",
            scope=InvocationScope(
                project_id="p1", timeline_id="t1", selected_asset_ids=("a1",),
            ),
            policy=RunPolicy(),
            inputs={},
        )
        compiled = compiler.compile(definition, inv)
        assert compiled.target == "editorial"
        assert compiled.runnable is True


# ================================================================
# P2.3 端到端：invoke_compiled 走 editorial 路径
# ================================================================


class TestEditorialInvokePath:
    """editorial target → invoke_compiled → editorial_skills 构造器 → RunRecord。"""

    def test_auto_caption_produces_plan_and_artifact(self, registry_with_p2):
        from src.features.ai_harness.runtime import HarnessRuntime
        from src.features.ai_harness.legacy_adapters import LegacyHarnessAdapter
        from src.features.ai_harness.skill_compiler import SkillCompiler
        from src.features.edit.ai_editing.editorial_models import (
            FrameRange, SegmentKind, SemanticSegment,
        )

        # evidence provider：返回 transcript 段
        def provider(asset_ids, kind):
            if kind == "transcript":
                return [
                    SemanticSegment(
                        segment_id="t1", asset_id="a1", kind=SegmentKind.TRANSCRIPT,
                        source_range=FrameRange(0, 100), payload={"text": "hello"},
                        confidence=0.9,
                    ),
                    SemanticSegment(
                        segment_id="t2", asset_id="a1", kind=SegmentKind.TRANSCRIPT,
                        source_range=FrameRange(100, 100), payload={"text": "world"},
                        confidence=0.9,
                    ),
                ]
            return ()

        rt = HarnessRuntime()
        adapter = LegacyHarnessAdapter(None, rt)  # service=None（editorial 不用 service）
        adapter.set_editorial_provider(provider)
        compiler = SkillCompiler()
        manifest = registry_with_p2.require("topos.auto_caption")
        definition = definition_from_manifest(manifest, is_official=True)

        inv = Invocation(
            source=InvocationSource.CHAT, skill_id="topos.auto_caption",
            scope=InvocationScope(
                project_id="p1", timeline_id="t1", selected_asset_ids=("a1",),
            ),
            policy=RunPolicy(),
            inputs={"brief_id": "b1"},
            parent_session_id="sess1",
        )
        compiled = compiler.compile(definition, inv)
        rid = adapter.invoke_compiled(compiled, inv)
        rec = rt.get_run(rid)
        # 成功（产 plan + artifact）
        assert rec.status in ("succeeded", "succeeded_with_warnings")
        assert len(rec.artifacts) == 1
        assert rec.artifacts[0].artifact_type == "subtitle_draft"

    def test_auto_caption_no_evidence_degrades(self, registry_with_p2):
        """无 transcript evidence → 降级（with_warnings，无 plan）。"""
        from src.features.ai_harness.runtime import HarnessRuntime
        from src.features.ai_harness.legacy_adapters import LegacyHarnessAdapter
        from src.features.ai_harness.skill_compiler import SkillCompiler

        rt = HarnessRuntime()
        adapter = LegacyHarnessAdapter(None, rt)
        # 无 provider → evidence 为空
        compiler = SkillCompiler()
        manifest = registry_with_p2.require("topos.auto_caption")
        definition = definition_from_manifest(manifest, is_official=True)
        inv = Invocation(
            source=InvocationSource.CHAT, skill_id="topos.auto_caption",
            scope=InvocationScope(
                project_id="p1", timeline_id="t1", selected_asset_ids=("a1",),
            ),
            policy=RunPolicy(), inputs={"brief_id": "b1"},
        )
        compiled = compiler.compile(definition, inv)
        rid = adapter.invoke_compiled(compiled, inv)
        rec = rt.get_run(rid)
        # 降级为 with_warnings（无 plan 产出，但不崩溃）
        assert rec.status == "succeeded_with_warnings"
        assert any(d.get("code") == "NO_TRANSCRIPT" for d in rec.diagnostics)

    def test_subtitle_localization_with_language(self, registry_with_p2):
        from src.features.ai_harness.runtime import HarnessRuntime
        from src.features.ai_harness.legacy_adapters import LegacyHarnessAdapter
        from src.features.ai_harness.skill_compiler import SkillCompiler
        from src.features.edit.ai_editing.editorial_models import (
            FrameRange, SegmentKind, SemanticSegment,
        )

        def provider(asset_ids, kind):
            return [SemanticSegment(
                segment_id="t1", asset_id="a1", kind=SegmentKind.TRANSCRIPT,
                source_range=FrameRange(0, 100), payload={"text": "hi"},
                confidence=0.9,
            )] if kind == "transcript" else []

        rt = HarnessRuntime()
        adapter = LegacyHarnessAdapter(None, rt)
        adapter.set_editorial_provider(provider)
        compiler = SkillCompiler()
        manifest = registry_with_p2.require("topos.subtitle_localization")
        definition = definition_from_manifest(manifest, is_official=True)
        inv = Invocation(
            source=InvocationSource.CHAT, skill_id="topos.subtitle_localization",
            scope=InvocationScope(
                project_id="p1", timeline_id="t1", selected_asset_ids=("a1",),
            ),
            policy=RunPolicy(),
            inputs={"brief_id": "b1", "target_language": "英文"},
        )
        compiled = compiler.compile(definition, inv)
        rid = adapter.invoke_compiled(compiled, inv)
        rec = rt.get_run(rid)
        assert rec.status in ("succeeded", "succeeded_with_warnings")
        # 产了翻译 text_document artifact（无 TTS → 诊断 NO_TTS_MEDIA）
        types = {a.artifact_type for a in rec.artifacts}
        assert "text_document" in types
        assert any(d.get("code") == "NO_TTS_MEDIA" for d in rec.diagnostics)

    def test_subtitle_localization_with_imported_tts_generates_audio_plan(self, registry_with_p2):
        from src.features.ai_harness.runtime import HarnessRuntime
        from src.features.ai_harness.legacy_adapters import LegacyHarnessAdapter
        from src.features.ai_harness.skill_compiler import SkillCompiler
        from src.features.edit.ai_editing.editorial_models import (
            FrameRange, SegmentKind, SemanticSegment,
        )

        def provider(asset_ids, kind):
            return [SemanticSegment(
                segment_id="t1", asset_id="a1", kind=SegmentKind.TRANSCRIPT,
                source_range=FrameRange(24, 100), payload={"text": "hello"},
                confidence=0.9,
            )] if kind == "transcript" else []

        runtime = HarnessRuntime()
        adapter = LegacyHarnessAdapter(None, runtime)
        adapter.set_editorial_provider(provider)
        compiler = SkillCompiler()
        manifest = registry_with_p2.require("topos.subtitle_localization")
        definition = definition_from_manifest(manifest, is_official=True)
        invocation = Invocation(
            source=InvocationSource.CHAT, skill_id="topos.subtitle_localization",
            scope=InvocationScope(project_id="p1", timeline_id="t1", selected_asset_ids=("a1",)),
            policy=RunPolicy(),
            inputs={
                "brief_id": "b1", "target_language": "英文",
                "tts_media_asset_id": "imported-tts-1",
                "tts_duration_frames": 120, "tts_start_frame": 24,
            },
        )
        compiled = compiler.compile(definition, invocation)
        run_id = adapter.invoke_compiled(compiled, invocation)
        record = runtime.get_run(run_id)
        assert record.status == "succeeded"
        assert not any(d.get("code") == "NO_TTS_MEDIA" for d in record.diagnostics)
        plan = next(artifact for artifact in record.artifacts if artifact.artifact_type == "media_generation_ref")
        assert plan.artifact_id == "media_asset:imported-tts-1"
