"""§13.1 矩阵补齐：music_policy + assistant_session_store + assistant_service。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.assistant_service import (
    AssistantConfig,
    AssistantService,
)
from src.features.edit.ai_editing.assistant_session_store import (
    AssistantSessionStore,
)
from src.features.edit.ai_editing.brief_service import BriefService
from src.features.edit.ai_editing.editorial_models import (
    DecisionState,
    EditDecision,
    EditVariant,
    EditorialBrief,
    FrameRange,
    SegmentKind,
    SemanticSegment,
    SourceRef,
    UserOverride,
)
from src.features.edit.ai_editing.music_policy import (
    LicenseStatus,
    MusicAsset,
    MusicPolicy,
)
from src.features.edit.ai_editing.semantic_index import SemanticIndex
from src.features.edit.ai_editing.variant_planner import VariantPlanner
from src.features.edit.ai_editing.edit_api import EditSession


# ================================================================
# music_policy（§11.7）
# ================================================================


def test_unlicensed_music_not_applicable():
    m = MusicAsset(asset_id="m1", name="未知来源", duration_frames=1000,
                   license_status=LicenseStatus.UNKNOWN)
    assert m.is_applicable is False


def test_licensed_music_applicable():
    m = MusicAsset(asset_id="m2", name="已授权", duration_frames=1000,
                   license_status=LicenseStatus.LICENSED, source_label="授权号 123")
    assert m.is_applicable is True


def test_music_policy_filters_applicable():
    p = MusicPolicy()
    p.register(MusicAsset(asset_id="m1", name="未授权", duration_frames=1000, license_status=LicenseStatus.UNKNOWN))
    p.register(MusicAsset(asset_id="m2", name="已授权", duration_frames=1000, license_status=LicenseStatus.LICENSED, bpm=120.0))
    apps = p.applicable_music()
    assert len(apps) == 1
    assert apps[0].asset_id == "m2"


def test_music_decision_for_unlicensed_not_applicable():
    p = MusicPolicy()
    unlicensed = MusicAsset(asset_id="m1", name="未知", duration_frames=500, license_status=LicenseStatus.UNKNOWN)
    decision = p.propose(unlicensed, FrameRange(0, 100))
    assert decision.is_applicable is False  # 没有应用按钮


def test_best_matches_by_duration_and_mood():
    p = MusicPolicy()
    p.register(MusicAsset(asset_id="m1", name="短曲", duration_frames=100, license_status=LicenseStatus.LICENSED, mood="upbeat"))
    p.register(MusicAsset(asset_id="m2", name="长曲 upbeat", duration_frames=1000, license_status=LicenseStatus.LICENSED, mood="upbeat", bpm=128))
    p.register(MusicAsset(asset_id="m3", name="长曲 calm", duration_frames=1000, license_status=LicenseStatus.LICENSED, mood="calm"))
    matches = p.best_matches(target_duration=500, target_mood="upbeat")
    # m1 时长不够被过滤；m2 mood 命中优先于 m3
    assert matches[0].asset_id == "m2"


def test_bpm_missing_does_not_invent_data():
    m = MusicAsset(asset_id="m1", name="无 BPM", duration_frames=100, license_status=LicenseStatus.LICENSED)
    assert m.bpm is None  # 不编造


# ================================================================
# assistant_session_store（§11.8 / §6.3）
# ================================================================


def test_session_store_round_trip(tmp_path):
    store = AssistantSessionStore()
    store.save_brief(EditorialBrief(brief_id="b1", revision=2))
    store.save_variant(EditVariant(variant_id="v1", brief_id="b1", brief_revision=2))
    store.save_decision(EditDecision(decision_id="d1", action="select"))
    store.record_override(UserOverride(override_id="o1", kind="trim"))

    assert store.persist(tmp_path)
    # 重新加载
    store2 = AssistantSessionStore()
    assert store2.load(tmp_path)
    assert store2.get_brief("b1").revision == 2
    assert store2.get_variant("v1").variant_id == "v1"
    assert store2.get_decision("d1").decision_id == "d1"


def test_session_store_no_secret(tmp_path):
    store = AssistantSessionStore()
    store.save_brief(EditorialBrief(brief_id="b1"))
    store.persist(tmp_path)
    import json
    content = (tmp_path / "ai" / "session.json").read_text()
    assert "api_key" not in content.lower()
    assert "secret" not in content.lower()


def test_session_store_load_missing_returns_false(tmp_path):
    store = AssistantSessionStore()
    assert store.load(tmp_path) is False  # 文件不存在，非错误


# ================================================================
# assistant_service（§11.5 状态机 / §11.6 编排）
# ================================================================


class _FakeHost:
    """EditSessionHost 测试桩。"""

    def __init__(self, state):
        self._state = state
        self.committed = []

    def get_state(self):
        return self._state

    def get_manifest(self):
        from src.features.edit.ai_editing.capability_manifest import build_manifest
        return build_manifest()

    def on_committed(self, result):
        self.committed.append(result)

    def request_user_approval(self, plan, diff):
        return "auto"


def _build_service():
    EditSession.unbind()
    from src.features.edit.ai_editing.edit_project_service import ProjectSequences
    from src.features.edit.core.project import EditProject, MediaAsset, TimelineSequenceRef
    from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
    from src.shared.media.metadata import Rational
    seq = TimelineSequence(timeline_id="t1", name="Main", fps=Rational(24, 1),
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0),))
    asset = MediaAsset(asset_id="a1", path="/x.mp4", media_type="video", display_name="A", metadata={"duration_frames": 5000})
    ref = TimelineSequenceRef(timeline_id="t1", name="Main", fps=24.0, width=1920, height=1080, sample_rate=48000)
    project = EditProject(project_id="p1", name="P", assets={"a1": asset}, timelines={"t1": ref}, root_bin_id="root", active_timeline_id="t1")
    state = ProjectSequences(project=project, sequences={"t1": seq})
    host = _FakeHost(state)
    session = EditSession.bind(host)
    bs = BriefService()
    bs.save(EditorialBrief(brief_id="b1", revision=1))
    service = AssistantService(session, brief_service=bs)
    EditSession.unbind()
    return service, host


def test_service_state_machine_idle_to_planning():
    service, host = _build_service()
    states = []
    service.on_state_changed = lambda s: states.append(s.state)
    service.start_session(
        AssistantConfig(asset_id="a1", timeline_id="t1", project_id="p1"),
        active_profile_id="p1", profile_connected=True,
    )
    service.submit_request("30 秒竖版宣传", brief_id="b1")
    # 状态推进到 GATHERING_CONTEXT
    assert any(s.value == "gathering_context" for s in states)


def test_service_no_project_state():
    service, host = _build_service()
    service.start_session(AssistantConfig())  # 空 config
    assert service.state.value == "no_project"


def test_service_no_profile_state():
    service, host = _build_service()
    service.start_session(
        AssistantConfig(asset_id="a1", timeline_id="t1", project_id="p1"),
        active_profile_id="",  # 无 profile
    )
    assert service.state.value == "no_profile"


def test_service_reject_decision_feedback():
    service, host = _build_service()
    service._decisions["d1"] = EditDecision(decision_id="d1", action="select")
    service.reject_decision("d1")
    assert service._decisions["d1"].state == DecisionState.REJECTED


def test_service_plan_variants_produces_variants():
    service, host = _build_service()
    service.start_session(
        AssistantConfig(asset_id="a1", timeline_id="t1", project_id="p1"),
        active_profile_id="p1", profile_connected=True,
    )
    service._active_brief_id = "b1"
    idx = SemanticIndex()
    idx.add(SemanticSegment(segment_id="t0", asset_id="a1", source_range=FrameRange(0, 100),
        kind=SegmentKind.TRANSCRIPT, payload={"text": "hi"}, confidence=0.9))
    config = AssistantConfig(asset_id="a1", timeline_id="t1", project_id="p1")
    variants = service.plan_variants(idx, config)
    assert len(variants) >= 2
    assert service.state.value == "variants_ready"
