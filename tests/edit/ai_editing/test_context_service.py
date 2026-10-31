"""TAEP M5 — ContextService + AssistantService._emit_state 缺口测试。

验收（§7.1 / §7.2 / M5 退出条件）：
- ContextService 从 producer 聚合出 block 化 ContextBundle（每块 token/优先级/为何包含）。
- brief/snapshot/decisions/evidence/tool manifest 各自成块。
- 脱敏：evidence 文本截断（§7.2 可逆索引）；不抓 secret（假定 sources 已脱敏）。
- with_budget 在 build_bundle 内自动应用。
- _emit_state 派生真实 allow_media_upload（profile.data_policy）+ ContextSummary。
  （之前一直是默认 False / 空 —— 红线 bug，M5 修）。
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.context_bundle import (
    ContextBlockPriority,
    KIND_BRIEF,
    KIND_DECISIONS,
    KIND_EVIDENCE,
    KIND_SELECTION,
    KIND_SNAPSHOT,
    KIND_SYSTEM_POLICY,
    KIND_TOOL_MANIFEST,
)
from src.features.edit.ai_editing.context_service import (
    ContextBuildRequest,
    ContextService,
)
from src.features.edit.ai_editing.editorial_models import (
    FrameRange,
    SegmentKind,
    SemanticSegment,
)


# ---------------------------------------------------------------------- #
# stub ContextSources
# ---------------------------------------------------------------------- #


class _StubSources:
    """最小 ContextSources 实现（带全部源，用于 ContextService 测试）。"""

    def __init__(self, *, with_segments: bool = True) -> None:
        self._with_segments = with_segments

    def get_brief_dict(self):
        return {"brief_id": "b1", "revision": 2, "primary_goal": "口播粗剪",
                "hard_constraints": []}

    def get_snapshot_dict(self, *, detail_level, asset_limit):
        return {"project_id": "p1", "revision": 1, "assets": [{"asset_id": "a1"}]}

    def get_planning_constraints(self):
        return {"must_preserve": ["d1"], "must_avoid": ["a2:0-24"], "user_overrides": []}

    def get_tool_manifest(self):
        return [{"name": "topos.search_assets", "permission": "READ",
                 "description": "search media"}]

    def get_capability_manifest(self):
        return {"operations": ["add_clip"]}

    def get_candidate_segments(self):
        if not self._with_segments:
            return None
        return (
            SemanticSegment(
                segment_id="seg-1", asset_id="a1", source_range=FrameRange(0, 24),
                kind=SegmentKind.TRANSCRIPT,
                payload={"text": "hello world " * 50},  # 长文本（测截断）
                confidence=0.9,
            ),
        )


# ---------------------------------------------------------------------- #
# ContextService 聚合
# ---------------------------------------------------------------------- #


class TestContextServiceAssembly:
    def test_build_bundle_includes_all_blocks(self) -> None:
        svc = ContextService()
        req = ContextBuildRequest(
            sources=_StubSources(),
            user_request="剪一个口播",
            selected_asset_ids=("a1",),
            target_beats=("hook",),
            target_duration=720,
        )
        bundle = svc.build_bundle(req)
        kinds = {b.kind for b in bundle.blocks}
        # 系统策略 + tool manifest + brief + selection + decisions + evidence + user_request
        assert KIND_SYSTEM_POLICY in kinds
        assert KIND_TOOL_MANIFEST in kinds
        assert KIND_BRIEF in kinds
        assert KIND_SELECTION in kinds
        assert KIND_DECISIONS in kinds
        assert KIND_EVIDENCE in kinds
        assert "user_request" in kinds

    def test_each_block_has_required_metadata(self) -> None:
        """§7.1：每块带 token 估算 / 优先级 / 为何包含 / provenance。"""
        svc = ContextService()
        bundle = svc.build_bundle(ContextBuildRequest(sources=_StubSources()))
        for b in bundle.blocks:
            assert b.token_estimate >= 1
            assert isinstance(b.priority, ContextBlockPriority)
            assert b.why_included  # 非空

    def test_evidence_block_carries_segment_id_provenance(self) -> None:
        svc = ContextService()
        bundle = svc.build_bundle(ContextBuildRequest(sources=_StubSources()))
        evidence = [b for b in bundle.blocks if b.kind == KIND_EVIDENCE]
        assert len(evidence) == 1
        assert evidence[0].provenance.get("segment_id") == "seg-1"
        assert evidence[0].provenance.get("asset_id") == "a1"

    def test_evidence_text_truncated(self) -> None:
        """§7.2 可逆索引：长转录截断，原文经 index 可按需取回。"""
        svc = ContextService()
        bundle = svc.build_bundle(ContextBuildRequest(sources=_StubSources()))
        evidence = next(b for b in bundle.blocks if b.kind == KIND_EVIDENCE)
        text = evidence.content.get("text", "")
        # 截断阈值 240；原文是 "hello world " × 50 = 600 字符
        assert len(text) <= 240

    def test_brief_block_has_revision_provenance(self) -> None:
        svc = ContextService()
        bundle = svc.build_bundle(ContextBuildRequest(sources=_StubSources()))
        brief = next(b for b in bundle.blocks if b.kind == KIND_BRIEF)
        assert brief.provenance.get("revision") == 2
        assert brief.provenance.get("brief_id") == "b1"

    def test_decisions_block_has_constraints(self) -> None:
        svc = ContextService()
        bundle = svc.build_bundle(ContextBuildRequest(sources=_StubSources()))
        decisions = next((b for b in bundle.blocks if b.kind == KIND_DECISIONS), None)
        assert decisions is not None
        assert "must_preserve" in decisions.content
        assert decisions.content["must_avoid"] == ["a2:0-24"]

    def test_tool_manifest_compact(self) -> None:
        """tool manifest 只带 name/permission/desc，不带完整 input_schema（省 token）。"""
        svc = ContextService()
        bundle = svc.build_bundle(ContextBuildRequest(sources=_StubSources()))
        manifest = next((b for b in bundle.blocks if b.kind == KIND_TOOL_MANIFEST))
        tool0 = manifest.content["tools"][0]
        assert "name" in tool0 and "permission" in tool0
        assert "input_schema" not in tool0  # 完整 schema 不进上下文

    def test_no_segments_skips_evidence(self) -> None:
        svc = ContextService()
        bundle = svc.build_bundle(
            ContextBuildRequest(sources=_StubSources(with_segments=False))
        )
        kinds = {b.kind for b in bundle.blocks}
        assert KIND_EVIDENCE not in kinds

    def test_no_sources_data_still_returns_policy_block(self) -> None:
        """所有 producer 返回 None → 至少有系统策略块（不崩）。"""

        class _EmptySources:
            def get_brief_dict(self):
                return None
            def get_snapshot_dict(self, *, detail_level, asset_limit):
                return None
            def get_planning_constraints(self):
                return None
            def get_tool_manifest(self):
                return None
            def get_capability_manifest(self):
                return None
            def get_candidate_segments(self):
                return None

        svc = ContextService()
        bundle = svc.build_bundle(ContextBuildRequest(sources=_EmptySources()))
        assert bundle.block_count == 1
        assert bundle.blocks[0].kind == KIND_SYSTEM_POLICY


class TestContextServiceBudget:
    def test_budget_prunes_low_priority(self) -> None:
        """build_bundle 自动 with_budget：超限时丢弃低优先级。"""
        from src.features.edit.ai_editing.context_bundle import TokenBudget

        svc = ContextService()
        req = ContextBuildRequest(
            sources=_StubSources(),
            budget=TokenBudget(limit=1, reserved_for_response=0),  # 极小预算
        )
        bundle = svc.build_bundle(req)
        # HARD_CONSTRAINT（policy + manifest）保留；其余按优先级丢
        remaining_priorities = {b.priority for b in bundle.blocks}
        assert ContextBlockPriority.HARD_CONSTRAINT in remaining_priorities
        # omitted 非空（有块被丢）
        assert len(bundle.omitted_block_ids) >= 1


class TestContextServiceMessages:
    def test_to_messages_has_system_and_user(self) -> None:
        svc = ContextService()
        bundle = svc.build_bundle(ContextBuildRequest(
            sources=_StubSources(), user_request="test request",
        ))
        msgs = bundle.to_messages()
        roles = [m["role"] for m in msgs]
        assert "system" in roles
        assert "user" in roles

    def test_user_request_in_user_message(self) -> None:
        svc = ContextService()
        bundle = svc.build_bundle(ContextBuildRequest(
            sources=_StubSources(), user_request="帮我剪口播",
        ))
        msgs = bundle.to_messages()
        user_msg = next(m for m in msgs if m["role"] == "user")
        assert "帮我剪口播" in user_msg["content"]


# ---------------------------------------------------------------------- #
# AssistantService._emit_state 缺口修复（M5）
# ---------------------------------------------------------------------- #


class TestEmitStateGaps:
    def test_allow_media_upload_defaults_false_without_lookup(self) -> None:
        """无 profile_lookup → allow_media_upload 保持 False（向后兼容）。"""
        from src.features.edit.ai_editing.assistant_service import AssistantService

        svc = AssistantService(session=MagicMock())
        captured: list = []
        svc.on_state_changed = lambda snap: captured.append(snap)
        svc._emit_state()
        assert captured[-1].allow_media_upload is False

    def test_allow_media_upload_true_when_profile_allows(self) -> None:
        """profile.data_policy.allow_media_upload=True → snapshot 反映 True（之前永远 False）。"""

        @dataclass
        class _Policy:
            allow_media_upload: bool = True

        @dataclass
        class _Profile:
            profile_id: str = "p1"
            data_policy: _Policy = None  # type: ignore

            def __post_init__(self):
                if self.data_policy is None:
                    self.data_policy = _Policy()

            def allows_media_upload(self) -> bool:
                return self.data_policy.allow_media_upload

        from src.features.edit.ai_editing.assistant_service import AssistantService

        svc = AssistantService(session=MagicMock())
        svc.set_profile_lookup(lambda pid: _Profile() if pid == "p1" else None)
        # 模拟 active profile
        svc._active_profile_id = "p1"
        captured: list = []
        svc.on_state_changed = lambda snap: captured.append(snap)
        svc._emit_state()
        assert captured[-1].allow_media_upload is True

    def test_allow_media_upload_false_when_profile_disallows(self) -> None:
        @dataclass
        class _Profile:
            profile_id: str = "p1"
            data_policy: object = None  # type: ignore

            def allows_media_upload(self) -> bool:
                return False

        from src.features.edit.ai_editing.assistant_service import AssistantService

        svc = AssistantService(session=MagicMock())
        svc.set_profile_lookup(lambda pid: _Profile())
        svc._active_profile_id = "p1"
        captured: list = []
        svc.on_state_changed = lambda snap: captured.append(snap)
        svc._emit_state()
        assert captured[-1].allow_media_upload is False

    def test_allow_media_upload_supports_provider_profile_property(self) -> None:
        """真实 AiProviderProfile 使用 property，启动时不能把 bool 当函数调用。"""
        from src.features.edit.ai_editing.assistant_service import (
            AssistantConfig,
            AssistantService,
        )
        from src.features.edit.ai_editing.provider_profiles import (
            AiProviderProfile,
            DataPolicy,
        )

        profile = AiProviderProfile(
            profile_id="p1",
            display_name="Local",
            modalities=("text", "tool_call", "media_upload"),
            data_policy=DataPolicy(execution="local", allow_media_upload=True),
        )
        svc = AssistantService(session=MagicMock())
        svc.set_profile_lookup(lambda pid: profile if pid == "p1" else None)
        svc._active_profile_id = "p1"
        captured: list = []
        svc.on_state_changed = lambda snap: captured.append(snap)

        # 复现启动路径：无工程时 start_session 立即发出 NO_PROJECT snapshot。
        svc.start_session(AssistantConfig(), active_profile_id="p1")

        assert svc.state.value == "no_project"
        assert captured[-1].allow_media_upload is True

    def test_context_summary_from_provider(self) -> None:
        """context_provider 注入后 → ContextSummary 反映真实计数（之前永远空）。"""
        from src.features.edit.ai_editing.assistant_service import AssistantService

        svc = AssistantService(session=MagicMock())
        svc.set_context_provider(lambda field: {
            "asset_count": 10,
            "analyzed_count": 3,
            "music_count": 1,
            "locked_count": 2,
            "snapshot_revision": 5,
            "analysis_fresh": True,
        }.get(field, 0))
        captured: list = []
        svc.on_state_changed = lambda snap: captured.append(snap)
        svc._emit_state()
        ctx = captured[-1].context
        assert ctx.asset_count == 10
        assert ctx.analyzed_count == 3
        assert ctx.locked_count == 2
        assert ctx.snapshot_revision == 5
        assert ctx.analysis_fresh is True

    def test_context_summary_empty_without_provider(self) -> None:
        """无 context_provider → ContextSummary 为默认值（向后兼容）。"""
        from src.features.edit.ai_editing.assistant_service import AssistantService

        svc = AssistantService(session=MagicMock())
        captured: list = []
        svc.on_state_changed = lambda snap: captured.append(snap)
        svc._emit_state()
        ctx = captured[-1].context
        assert ctx.asset_count == 0
        assert ctx.analyzed_count == 0
