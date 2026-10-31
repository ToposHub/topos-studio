"""wd12-g71 regression tests: batch D Major findings (AUD-27006…AUD-27302).

Covers the batch-D remediation fixes:

* AUD-27006 — quality gate transparency check runs and reports findings.
* AUD-27010 — accept() blocks every terminal revision status.
* AUD-27014 — kill switch is controller-local; frozen policy untouched.
* AUD-27019 — apply_html_layer/apply_html_template honour ``command_stack``.
* AUD-27100 — session registry get_or_create is race-free.
* AUD-27103 — upsert update keeps existing status; named args apply.
* AUD-27104 — disk write failure rolls the in-memory state back.
* AUD-27105 — reload tolerates malformed external JSON.
* AUD-27106 — grant_scope round-trip restores typed fields.
* AUD-27110 — empty SLO benchmark reports "not all meet targets".
* AUD-27201 — default scene released on load / repoint.
* AUD-27202 — render failures narrowed + logged, still fail-soft.
* AUD-27203 — frame-level render cache (no per-evaluate re-rasterization).
* AUD-27204 — u1 texture path reports the delivered 8-bit sRGB truth.
* AUD-27301 — asset sha256 streamed + cached (no whole-file read_bytes).
* AUD-27302 — unreadable asset degrades to a MISSING descriptor.

Run: .venv/bin/python -m pytest tests/teaching_animation/test_fix_wd12g71_aud.py
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from src.features.teaching_animation.ai.action_matrix import MotionGraphicAction
from src.features.teaching_animation.ai.approval_scope import MotionGraphicApprovalScope
from src.features.teaching_animation.ai.brief import MotionGraphicBrief
from src.features.teaching_animation.ai.error_codes import MOTION_GRAPHIC_REVISION_CONFLICT
from src.features.teaching_animation.ai.proposal import (
    CreativeProposal,
    NarrativeBeat,
    StyleTokens,
)
from src.features.teaching_animation.ai.quality_gate import (
    CHECK_TRANSPARENCY,
    MotionGraphicQualityGate,
    QualityGateInputs,
    RenderFrameResult,
)
from src.features.teaching_animation.ai.revision import (
    FEEDBACK_COPY,
    STATUS_ACCEPTED,
    TERMINAL_STATUSES,
    FeedbackPatch,
    MotionGraphicFailure,
    MotionGraphicRevisionLog,
)
from src.features.teaching_animation.ai.rollout import (
    COHORT_INTERNAL,
    FLAG_MOTION_GRAPHIC_AUTO,
    DictFlagSource,
    RolloutController,
    RolloutPolicy,
)
from src.features.teaching_animation.ai.service import TeachingAnimationAIService
from src.features.teaching_animation.ai.session import (
    TeachingAssistantSession,
    TeachingAssistantSessionRegistry,
)
from src.features.teaching_animation.ai.session_store import (
    MotionGraphicPendingRequest,
    MotionGraphicSessionSnapshot,
    MotionGraphicSessionStore,
)
from src.features.teaching_animation.ai.slo_benchmark import (
    BenchmarkSample,
    PerformanceSLO,
    summarize_samples,
)
from src.features.teaching_animation.core.contracts import RenderedFrame
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.core.professional import (
    SceneColorPipeline,
    read_scene_color_pipeline,
    write_scene_color_pipeline_into,
)
from src.features.teaching_animation.scene_store import get_shared_scene_store


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _clean_scene() -> TeachingSceneDocument:
    scene = TeachingSceneDocument.create_default(
        "Gate scene", width=320, height=180, duration_frames=60,
    )
    # create_default adds a placeholder title that overflows a 320x180 canvas;
    # strip it so the design validator has nothing to flag.
    return replace(scene, elements=())


def _proposal(**overrides: Any) -> CreativeProposal:
    brief = MotionGraphicBrief(
        brief_id="mg-brief-g71",
        topic="介绍 AI Agent",
        duration_seconds=15.0,
        canvas_aspect="16:9",
    )
    base = dict(
        proposal_id="mg-prop-g71",
        brief_id=brief.brief_id,
        brief_revision=brief.revision,
        brief_hash=brief.brief_hash(),
        title="方案 A",
        summary="15 秒介绍。",
        beats=(NarrativeBeat("b1", "开场", 0.0, 15.0, on_screen_copy="AI"),),
        style=StyleTokens(palette={"background": "#0f172a", "accent": "#38bdf8"}),
    )
    base.update(overrides)
    return CreativeProposal(**base)


def _scope() -> MotionGraphicApprovalScope:
    return MotionGraphicApprovalScope(
        project_id="proj-1",
        timeline_id="tl-1",
        track_id=None,
        insert_frame=12,
        brief_id="mg-brief-g71",
        brief_revision=1,
        brief_hash="hash-brief",
        scene_hash=None,
        plan_hash=None,
        diff_hash=None,
        preview_hash=None,
        asset_ids=("a1", "a2"),
        allowed_actions=(
            MotionGraphicAction.PLAN_CREATIVE,
            MotionGraphicAction.LLM_GENERATE_PROPOSAL,
        ),
    )


# --------------------------------------------------------------------------- #
# AUD-27006: transparency check
# --------------------------------------------------------------------------- #


def test_aud27006_transparency_flagged_and_check_accounted() -> None:
    gate = MotionGraphicQualityGate()
    report = gate.run(QualityGateInputs(
        scene=_clean_scene(),
        render_frames=(
            RenderFrameResult(label="first", frame_index=0, succeeded=True, has_transparency=True),
            RenderFrameResult(label="last", frame_index=59, succeeded=True),
        ),
    ))
    assert CHECK_TRANSPARENCY in report.checks_run
    transparency = [f for f in report.findings if f.check == CHECK_TRANSPARENCY]
    assert transparency, "has_transparency=True must produce a transparency finding"
    # Transparency is a review-needed warning; it must not block deliverability.
    assert transparency[0].severity == "warning"
    assert report.is_deliverable


def test_aud27006_transparency_check_run_without_transparent_frames() -> None:
    gate = MotionGraphicQualityGate()
    report = gate.run(QualityGateInputs(
        scene=_clean_scene(),
        render_frames=(
            RenderFrameResult(label="first", frame_index=0, succeeded=True),
        ),
    ))
    # The check ran and found nothing — no silent skip.
    assert CHECK_TRANSPARENCY in report.checks_run
    assert not [f for f in report.findings if f.check == CHECK_TRANSPARENCY]


# --------------------------------------------------------------------------- #
# AUD-27010: accept() blocks terminal-outcome revisions (REJECTED/FAILED);
# SUPERSEDED rollback-accept stays allowed (locked product behaviour).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("terminal", ["failed", "rejected"])
def test_aud27010_accept_blocks_terminal_revisions(terminal: str) -> None:
    log = MotionGraphicRevisionLog()
    revision = log.add_initial(_proposal())
    if terminal == "failed":
        log.mark_failed(revision.revision_id, reason="render crash")
    else:
        log.reject(revision.revision_id, reason="节奏不对")
    target = log.get(revision.revision_id)
    assert target.status in TERMINAL_STATUSES
    with pytest.raises(MotionGraphicFailure) as exc_info:
        log.accept(target.revision_id)
    assert exc_info.value.code == MOTION_GRAPHIC_REVISION_CONFLICT
    # The terminal revision was not turned into the accepted pick.
    assert log.get(target.revision_id).status != STATUS_ACCEPTED


def test_aud27010_superseded_rollback_accept_still_allowed() -> None:
    # Locked product behaviour (test_revision_log_accept_replaces_previous_
    # accept): re-accepting an older, superseded revision is the explicit
    # "roll back to this version" path and must keep working.
    log = MotionGraphicRevisionLog()
    first = log.add_initial(_proposal())
    second = log.add_feedback(
        first.revision_id, _proposal(title="v2"),
        FeedbackPatch(patch_id="fp-1", categories=(FEEDBACK_COPY,)),
    )
    assert log.get(first.revision_id).status == "superseded"
    accepted = log.accept(first.revision_id)
    assert accepted.status == STATUS_ACCEPTED
    assert log.accepted_revision(first.lineage_id).revision_id == first.revision_id
    # ``second`` was never accepted, so it keeps its pending status; the
    # rollback simply re-points the accepted pick at ``first``.
    assert log.get(second.revision_id).status == "pending"


def test_aud27010_accept_still_works_for_active_revisions() -> None:
    log = MotionGraphicRevisionLog()
    revision = log.add_initial(_proposal())
    assert log.accept(revision.revision_id).status == STATUS_ACCEPTED
    # Re-accepting the already accepted revision stays legal (idempotent).
    assert log.accept(revision.revision_id).status == STATUS_ACCEPTED
    pending = log.add_initial(_proposal(proposal_id="mg-prop-g71b"))
    assert log.accept(pending.revision_id).status == STATUS_ACCEPTED


# --------------------------------------------------------------------------- #
# AUD-27014: kill switch no longer mutates the frozen policy
# --------------------------------------------------------------------------- #


def test_aud27014_kill_switch_leaves_policy_frozen() -> None:
    policy = RolloutPolicy(min_cohort_for_auto=COHORT_INTERNAL)
    controller = RolloutController(DictFlagSource({FLAG_MOTION_GRAPHIC_AUTO: True}), policy=policy)
    assert controller.may_use_autonomous_mode(user_id="u")
    controller.activate_kill_switch(reason="copyright")
    assert not controller.may_use_autonomous_mode(user_id="u")
    # The frozen policy value object is untouched.
    assert policy.kill_switch_active is False
    assert controller.policy == policy
    controller.deactivate_kill_switch(reason="incident closed")
    assert controller.may_use_autonomous_mode(user_id="u")
    assert policy.kill_switch_active is False


def test_aud27014_shared_policy_controllers_are_independent() -> None:
    shared_policy = RolloutPolicy()
    flags = DictFlagSource({FLAG_MOTION_GRAPHIC_AUTO: True})
    first = RolloutController(flags, policy=shared_policy)
    second = RolloutController(flags, policy=shared_policy)
    first.activate_kill_switch(reason="one controller trips")
    # Only the tripping controller goes dark; the sibling keeps working.
    assert not first.may_use_autonomous_mode(user_id="u")
    assert second.may_use_autonomous_mode(user_id="u")


def test_aud27014_deactivate_records_incident() -> None:
    controller = RolloutController(DictFlagSource({FLAG_MOTION_GRAPHIC_AUTO: True}))
    controller.activate_kill_switch(reason="r")
    controller.deactivate_kill_switch(reason="fixed")
    kinds = [(incident.kind, incident.severity) for incident in controller.incidents()]
    assert ("kill_switch", "error") in kinds
    assert ("kill_switch", "info") in kinds


# --------------------------------------------------------------------------- #
# AUD-27019: HTML layer writes go through the command stack
# --------------------------------------------------------------------------- #


class _RecordingStack:
    """Minimal command-stack stand-in mirroring execute(command, target)."""

    def __init__(self) -> None:
        self.executed: list[Any] = []

    def execute(self, command: Any, target: Any) -> Any:
        self.executed.append(command)
        return command.apply(target)

    def undo(self, target: Any) -> Any:
        return self.executed.pop().undo(target)


class _FakeLlmClient:
    def __init__(self, html: str) -> None:
        self._html = html

    def complete(self, messages: Any, *, temperature: float = 0.0, max_tokens: int = 0) -> str:
        return self._html


def _writable_outcome(service: TeachingAnimationAIService) -> Any:
    outcome = service.generate_html(
        _FakeLlmClient('<section data-topos-node-id="hero"><h1>Title</h1></section>'),
        approved=True,
    )
    assert outcome.writable, outcome.blocking_reasons
    return outcome


def _html_service(name: str) -> TeachingAnimationAIService:
    return TeachingAnimationAIService(
        TeachingSceneDocument.create_default(name, width=480, height=270, duration_frames=120))


def test_aud27019_apply_html_layer_records_command_for_undo() -> None:
    service = _html_service("S1")
    stack = _RecordingStack()
    layer = service.apply_html_layer(_writable_outcome(service), command_stack=stack)
    assert len(stack.executed) == 1
    assert service.scene.html_layer is layer
    # Undo through the recorded command restores the previous (empty) layer.
    restored = stack.undo(service.scene)
    assert restored.html_layer is None


def test_aud27019_apply_html_layer_without_stack_still_applies() -> None:
    service = _html_service("S2")
    layer = service.apply_html_layer(_writable_outcome(service))
    assert service.scene.html_layer is layer


def test_aud27019_apply_html_template_records_command_for_undo() -> None:
    service = _html_service("S3")
    service.apply_html_layer(_writable_outcome(service))
    service.promote_html_template(
        template_id="tpl-g71", display_name="G71", publish_to_effects=False)
    stack = _RecordingStack()
    layer = service.apply_html_template("tpl-g71", {}, command_stack=stack)
    assert len(stack.executed) == 1
    assert service.scene.html_layer is layer
    restored = stack.undo(service.scene)
    assert restored.html_layer is not layer


# --------------------------------------------------------------------------- #
# AUD-27100: registry race
# --------------------------------------------------------------------------- #


def test_aud27100_get_or_create_single_session_under_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.features.teaching_animation.ai.session as session_module

    original = session_module.TeachingAssistantSession

    def slow_ctor(project_id: str, provider: Any = None) -> TeachingAssistantSession:
        # A yield point inside the construction region; the registry lock
        # must still serialize creators.
        session = original(project_id, provider)
        time.sleep(0.01)
        return session

    monkeypatch.setattr(session_module, "TeachingAssistantSession", slow_ctor)
    registry = TeachingAssistantSessionRegistry()
    results: list[TeachingAssistantSession] = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def worker() -> None:
        start.wait()
        session = registry.get_or_create("proj-race")
        with lock:
            results.append(session)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 8
    assert all(session is results[0] for session in results)


def test_aud27100_attach_detach_thread_safe() -> None:
    session = TeachingAssistantSession("proj-1")
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            for i in range(200):
                surface_id = f"surface-{index}-{i}"
                session.attach(surface_id)
                assert surface_id in session.attached_surface_ids()
                session.detach(surface_id)
        except BaseException as exc:  # pragma: no cover - surfaced via errors
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert session.attached_surface_ids() == ()


# --------------------------------------------------------------------------- #
# AUD-27103 / 27104 / 27105 / 27106: session store
# --------------------------------------------------------------------------- #


def _store(tmp_path: Path) -> MotionGraphicSessionStore:
    return MotionGraphicSessionStore(target_dir=str(tmp_path), project_id="proj-1")


def test_aud27103_update_keeps_status_and_applies_named_args(tmp_path: Path) -> None:
    store = _store(tmp_path)
    created = store.upsert_request(
        request_id="req-1", project_id="proj-1", timeline_id="tl-1",
        brief_id="b1", brief_revision=1, status="waiting_approval",
    )
    assert created.status == "waiting_approval"
    # Update that does not mention status: must NOT reset it to "draft".
    updated = store.upsert_request(
        request_id="req-1", project_id="proj-1", timeline_id="tl-1",
        brief_id="b1", brief_revision=1,
    )
    assert updated.status == "waiting_approval", "status silently reset to draft"
    # Named arguments now apply on update (previously silently ignored).
    bumped = store.upsert_request(
        request_id="req-1", project_id="proj-1", timeline_id="tl-2",
        brief_id="b1", brief_revision=2, note="regenerated",
    )
    assert bumped.brief_revision == 2
    assert bumped.timeline_id == "tl-2"
    assert bumped.note == "regenerated"
    assert bumped.status == "waiting_approval"
    # Explicit status still wins when the caller wants a transition.
    applied = store.upsert_request(
        request_id="req-1", project_id="proj-1", timeline_id="tl-2",
        brief_id="b1", brief_revision=2, status="applied",
    )
    assert applied.status == "applied"
    # Create defaults unchanged.
    fresh = store.upsert_request(
        project_id="proj-1", timeline_id=None, brief_id="b2", brief_revision=1)
    assert fresh.status == "draft" and fresh.note == ""


def test_aud27104_write_failure_rolls_memory_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    store.upsert_request(
        request_id="req-1", project_id="proj-1", timeline_id="tl-1",
        brief_id="b1", brief_revision=1, status="waiting_approval",
    )

    def failing_replace(src: Any, dst: Any) -> None:
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError):
        store.upsert_request(
            request_id="req-1", project_id="proj-1", timeline_id="tl-1",
            brief_id="b1", brief_revision=2,
        )
    # Memory was rolled back — no silent divergence from disk.
    after = store.get("req-1")
    assert after is not None
    assert after.brief_revision == 1
    with pytest.raises(OSError):
        store.discard("req-1")
    assert store.get("req-1") is not None
    monkeypatch.undo()
    # After the failure clears, reload from disk agrees with memory.
    store.reload()
    reloaded = store.get("req-1")
    assert reloaded is not None
    assert reloaded.brief_revision == 1
    assert reloaded.status == "waiting_approval"


def _write_session_file(tmp_path: Path, payload: dict[str, Any]) -> None:
    (tmp_path / MotionGraphicSessionStore.FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_aud27105_reload_tolerates_malformed_records(tmp_path: Path) -> None:
    good_request = MotionGraphicPendingRequest(
        request_id="req-good", project_id="proj-1", timeline_id=None,
        brief_id="b1", brief_revision=1, created_at=1.0, updated_at=2.0,
    )
    _write_session_file(tmp_path, {
        "schema_version": 1,
        "project_id": "proj-1",
        "requests": [
            good_request.to_dict(),
            # brief_revision not numeric → record dropped, not a crash.
            {"request_id": "req-bad-num", "project_id": "proj-1", "brief_id": "b",
             "brief_revision": "one"},
            # Unknown grant_scope keys + unknown action value → tolerated.
            {"request_id": "req-scope-unknown", "project_id": "proj-1", "brief_id": "b",
             "brief_revision": 1, "created_at": 1.0, "updated_at": 1.0,
             "grant_scope": {"project_id": "proj-1", "timeline_id": None, "track_id": None,
                             "insert_frame": 0, "brief_id": "b", "brief_revision": 1,
                             "brief_hash": "h", "scene_hash": None, "plan_hash": None,
                             "diff_hash": None, "preview_hash": None,
                             "future_field": {"unknown": True},
                             "allowed_actions": ["plan_creative", "not_a_real_action"]}},
        ],
        "extra": {},
    })
    store = _store(tmp_path)
    store.reload()  # must not raise
    assert store.get("req-good") is not None
    assert store.get("req-bad-num") is None
    scoped = store.get("req-scope-unknown")
    assert scoped is not None
    # Unknown action values are dropped; valid ones survive as enums.
    assert scoped.grant_scope is not None
    assert MotionGraphicAction.PLAN_CREATIVE in scoped.grant_scope.allowed_actions


def test_aud27105_reload_refuses_snapshot_level_corruption(tmp_path: Path) -> None:
    _write_session_file(tmp_path, {
        "schema_version": "not-a-number",
        "project_id": "proj-1",
        "requests": [],
    })
    store = _store(tmp_path)
    store.upsert_request(
        request_id="req-keep", project_id="proj-1", timeline_id=None,
        brief_id="b1", brief_revision=1,
    )
    store.reload()  # must not raise; malformed snapshot refused wholesale
    assert store.get("req-keep") is not None


def test_aud27106_grant_scope_round_trip_restores_types(tmp_path: Path) -> None:
    original_scope = _scope()
    record = MotionGraphicPendingRequest(
        request_id="req-scope", project_id="proj-1", timeline_id=None,
        brief_id="mg-brief-g71", brief_revision=1, created_at=1.0, updated_at=1.0,
        grant_scope=original_scope,
    )
    restored_record = MotionGraphicPendingRequest.from_dict(
        json.loads(json.dumps(record.to_dict())))
    scope = restored_record.grant_scope
    assert scope is not None
    assert isinstance(scope.asset_ids, tuple)
    assert all(isinstance(item, str) for item in scope.asset_ids)
    assert isinstance(scope.allowed_actions, tuple)
    assert all(isinstance(item, MotionGraphicAction) for item in scope.allowed_actions)
    # Frozen-dataclass equality now holds against the original scope…
    assert scope == original_scope
    # …and diff_from (which needs .value on actions) no longer explodes.
    assert scope.diff_from(_scope()) == []
    # Full store round trip through disk.
    store = _store(tmp_path)
    store.restore(MotionGraphicSessionSnapshot(
        project_id="proj-1", requests=(record,)))
    store.reload()
    reloaded = store.get("req-scope")
    assert reloaded is not None and reloaded.grant_scope == original_scope


# --------------------------------------------------------------------------- #
# AUD-27110: empty SLO report fails closed
# --------------------------------------------------------------------------- #


def _slo() -> PerformanceSLO:
    return PerformanceSLO(
        slo_id="render_frame", label="帧渲染", unit="seconds",
        p50_target=16.7, p95_target=33.4,
    )


def test_aud27110_empty_report_is_not_all_green() -> None:
    report = summarize_samples([])
    assert report.summaries == ()
    assert report.all_meet_targets is False


def test_aud27110_unknown_slo_ids_do_not_go_green() -> None:
    samples = [BenchmarkSample(slo_id="typo-ed-id", value=1.0)]
    report = summarize_samples(samples)
    assert report.summaries == ()
    assert report.all_meet_targets is False


def test_aud27110_populated_report_still_evaluates() -> None:
    good = [BenchmarkSample(slo_id="render_frame", value=8.0 + i) for i in range(10)]
    assert summarize_samples(good, slos=[_slo()]).all_meet_targets is True
    bad = [BenchmarkSample(slo_id="render_frame", value=50.0 + i) for i in range(10)]
    assert summarize_samples(bad, slos=[_slo()]).all_meet_targets is False


# --------------------------------------------------------------------------- #
# AUD-27201 / 27202 / 27203 / 27204: composite node
# --------------------------------------------------------------------------- #


class _FakeTexture:
    def __init__(self, size: tuple[int, int]) -> None:
        self.size = size
        self.glo = id(self)
        self.writes = 0
        self.released = False

    def write(self, data: Any) -> None:
        self.writes += 1

    def release(self) -> None:
        self.released = True


class _FakeGl:
    def __init__(self) -> None:
        self.textures: list[_FakeTexture] = []

    def texture(self, size: tuple[int, int], components: int, dtype: str = "u1") -> _FakeTexture:
        texture = _FakeTexture(size)
        self.textures.append(texture)
        return texture


class _CountingRenderer:
    """Scene renderer double counting prepare/render cycles."""

    calls = 0

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height

    def prepare(self, scene: Any, context: Any) -> None:
        type(self).calls += 1

    def render_frame(self, frame: Any) -> RenderedFrame:
        return RenderedFrame(
            frame=0, width=self.width, height=self.height,
            pixel_format="rgba8",
            data=bytes(self.width * self.height * 4),
        )

    def cleanup(self) -> None:
        pass


def _gl_context(frame_number: int) -> Any:
    """A GL-context stand-in carrying the evaluator's frame number.

    ``MotionGraphicNode._do_evaluate`` treats a non-CompositeEvalContext
    ``ctx`` as the GL context itself, so the fake plays both roles.
    """
    context = _FakeGl()
    context.frame_number = frame_number
    return context


def _small_scene(scene_id: str) -> TeachingSceneDocument:
    return TeachingSceneDocument.create_default(
        "Cache scene", scene_id=scene_id, width=32, height=18, duration_frames=24,
    )


@pytest.fixture()
def _router(monkeypatch: pytest.MonkeyPatch) -> type:
    from src.features.teaching_animation.render import scene_renderer_router
    _CountingRenderer.calls = 0
    monkeypatch.setattr(
        scene_renderer_router, "select_scene_renderer",
        lambda scene: _CountingRenderer(scene.width, scene.height),
    )
    return _CountingRenderer


def _motion_graphic_node():
    from src.features.teaching_animation.composite_node import MotionGraphicNode
    return MotionGraphicNode


def test_aud27201_from_dict_releases_default_scene() -> None:
    store = get_shared_scene_store()
    scene = _small_scene("aud27201-load-target")
    node = None
    try:
        store.register(scene)
        before = {item.scene_id for item in store.scenes()}
        node = _motion_graphic_node().from_dict({
            "id": "node-g71", "type": "motion_graphic", "position": [0, 0],
            "parameters": {"scene_id": scene.scene_id},
        })
        assert node.parameters["scene_id"] == scene.scene_id
        # The constructor's throwaway default scene did not survive the load.
        assert {item.scene_id for item in store.scenes()} == before
    finally:
        if node is not None:
            node.cleanup()
        store.remove(scene.scene_id)


def test_aud27201_set_parameter_repoint_releases_default_scene() -> None:
    store = get_shared_scene_store()
    scene = _small_scene("aud27201-repoint-target")
    node = None
    try:
        store.register(scene)
        node = _motion_graphic_node()()
        default_id = str(node.parameters["scene_id"])
        assert store.get(default_id) is not None
        node.set_parameter("scene_id", scene.scene_id)
        # The untouched default scene was reclaimed; the target stays.
        assert store.get(default_id) is None
        assert store.get(scene.scene_id) is scene
    finally:
        if node is not None:
            node.cleanup()
        store.remove(scene.scene_id)


def test_aud27201_edited_default_scene_is_kept() -> None:
    store = get_shared_scene_store()
    scene = _small_scene("aud27201-edited-keep")
    node = None
    default_id = ""
    try:
        store.register(scene)
        node = _motion_graphic_node()()
        default_id = str(node.parameters["scene_id"])
        # The user edited the default scene in the MG editor (registered
        # replacement content under the same id): repointing must NOT delete
        # the user's work.
        edited = replace(store.get(default_id), name="用户改过的场景")
        store.register(edited)
        node.set_parameter("scene_id", scene.scene_id)
        assert store.get(default_id) is not None
        assert store.get(default_id).name == "用户改过的场景"
    finally:
        if node is not None:
            node.cleanup()
        store.remove(scene.scene_id)
        store.remove(default_id)


def test_aud27203_frame_cache_avoids_re_render(_router: type) -> None:
    store = get_shared_scene_store()
    scene = _small_scene("aud27203-cache")
    gl = None
    node = None
    try:
        store.register(scene)
        node = _motion_graphic_node()()
        gl = _gl_context(0)
        node.parameters["scene_id"] = scene.scene_id
        first = node._do_evaluate({}, gl, None)
        assert _router.calls == 1
        assert first["image"].revision == 1
        # Same frame again: served from the frame cache — no renderer cycle,
        # no texture rewrite, content revision unchanged.
        repeat = node._do_evaluate({}, gl, None)
        assert _router.calls == 1
        assert repeat["image"].revision == 1
        assert gl.textures[0].writes == 1
        # A different frame re-renders and bumps the content revision.
        other = node._do_evaluate({}, _gl_context(7), None)
        assert _router.calls == 2
        assert other["image"].revision == 2
        # A changed scene (store register bumps revision) invalidates the cache.
        store.register(replace(scene, name="renamed"))
        changed = node._do_evaluate({}, _gl_context(7), None)
        assert _router.calls == 3
        assert changed["image"].revision == 3
    finally:
        if node is not None:
            node.cleanup()
        store.remove(scene.scene_id)


def test_aud27202_render_error_narrowed_logged_and_fail_soft(
        _router: type, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    from src.features.teaching_animation.render import scene_renderer_router

    class _ExplodingRenderer(_CountingRenderer):
        def render_frame(self, frame: Any) -> RenderedFrame:
            raise RuntimeError("HtmlRenderError: chromium crashed")

    monkeypatch.setattr(
        scene_renderer_router, "select_scene_renderer",
        lambda scene: _ExplodingRenderer(scene.width, scene.height),
    )
    store = get_shared_scene_store()
    scene = _small_scene("aud27202-failsoft")
    node = None
    try:
        store.register(scene)
        node = _motion_graphic_node()()
        node.parameters["scene_id"] = scene.scene_id
        with caplog.at_level("WARNING"):
            result = node._do_evaluate({}, _gl_context(0), None)
        assert result == {}  # fail-soft preserved
        assert any("render failed" in record.message for record in caplog.records)
    finally:
        if node is not None:
            node.cleanup()
        store.remove(scene.scene_id)


def test_aud27202_unexpected_error_not_swallowed(
        _router: type, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.teaching_animation.render import scene_renderer_router

    class _TypeErrorRenderer(_CountingRenderer):
        def render_frame(self, frame: Any) -> RenderedFrame:
            raise TypeError("programmer bug, not a domain failure")

    monkeypatch.setattr(
        scene_renderer_router, "select_scene_renderer",
        lambda scene: _TypeErrorRenderer(scene.width, scene.height),
    )
    store = get_shared_scene_store()
    scene = _small_scene("aud27202-propagate")
    node = None
    try:
        store.register(scene)
        node = _motion_graphic_node()()
        node.parameters["scene_id"] = scene.scene_id
        with pytest.raises(TypeError):
            node._do_evaluate({}, _gl_context(0), None)
    finally:
        if node is not None:
            node.cleanup()
        store.remove(scene.scene_id)


def test_aud27204_u1_path_reports_delivered_eight_bit_srgb(_router: type) -> None:
    store = get_shared_scene_store()
    scene = _small_scene("aud27204-hdr")
    node = None
    try:
        scene = replace(scene, metadata=write_scene_color_pipeline_into(
            scene.metadata, SceneColorPipeline("acescg", 32)))
        assert read_scene_color_pipeline(scene.metadata).is_hdr
        store.register(scene)
        node = _motion_graphic_node()()
        node.parameters["scene_id"] = scene.scene_id
        image = node._do_evaluate({}, _gl_context(0), None)["image"]
        # Without GPU effects the node delivers a u1 texture of tonemapped,
        # display-referred pixels: report THAT, not the declared acescg/32.
        assert image.bit_depth == 8
        assert image.color_space == "srgb"
        assert image.render_diagnostics["hdr_fallback"] == "svg_8bit_tonemap"
        assert image.render_diagnostics["color_pipeline"]["bit_depth"] == 32
    finally:
        if node is not None:
            node.cleanup()
        store.remove(scene.scene_id)


def test_aud27204_sdr_scene_unchanged(_router: type) -> None:
    store = get_shared_scene_store()
    scene = _small_scene("aud27204-sdr")
    node = None
    try:
        store.register(scene)
        node = _motion_graphic_node()()
        node.parameters["scene_id"] = scene.scene_id
        image = node._do_evaluate({}, _gl_context(0), None)["image"]
        assert image.bit_depth == 8
        assert image.color_space == "srgb"
        assert image.render_diagnostics is None
    finally:
        if node is not None:
            node.cleanup()
        store.remove(scene.scene_id)


# --------------------------------------------------------------------------- #
# AUD-27301 / 27302: local asset provider
# --------------------------------------------------------------------------- #


def test_aud27301_sha256_streamed_not_whole_file_read(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.teaching_animation.core.asset_provider import LocalAssetProvider

    payload = b"topos" * 500_000  # ~2.5 MB: larger than one hash chunk
    asset = tmp_path / "big.bin"
    asset.write_bytes(payload)

    def forbidden(self: Path) -> bytes:
        raise AssertionError("read_bytes() must not be used for hashing (AUD-27301)")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    provider = LocalAssetProvider(tmp_path, [{"asset_id": "a1", "path": "big.bin"}])
    descriptor = provider.resolve("a1")
    assert descriptor is not None
    assert descriptor.sha256 == hashlib.sha256(payload).hexdigest()


def test_aud27301_digest_cache_hit_and_invalidation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.teaching_animation.core.asset_provider import LocalAssetProvider

    asset = tmp_path / "cache.bin"
    asset.write_bytes(b"v1")
    provider = LocalAssetProvider(tmp_path, [{"asset_id": "a1", "path": "cache.bin"}])
    first = provider.resolve("a1").sha256

    real_open = Path.open

    def counting_open(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if self == asset and "b" in mode:
            raise AssertionError("cached digest must not re-read the file")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)
    # Same (path, mtime, size): served from the cache — no file re-open.
    assert provider.resolve("a1").sha256 == first
    monkeypatch.undo()
    # Content change (new size + mtime) invalidates the cache entry.
    asset.write_bytes(b"v2-different-length")
    third = provider.resolve("a1").sha256
    assert third != first
    assert third == hashlib.sha256(b"v2-different-length").hexdigest()


def test_aud27302_io_error_degrades_to_missing_descriptor(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.teaching_animation.core.asset_provider import LocalAssetProvider

    asset = tmp_path / "locked.bin"
    asset.write_bytes(b"data")
    provider = LocalAssetProvider(tmp_path, [{"asset_id": "a1", "path": "locked.bin"}])

    def failing_digest(self: LocalAssetProvider, path: Path) -> str:
        raise OSError("permission denied (simulated)")

    monkeypatch.setattr(LocalAssetProvider, "_file_sha256", failing_digest)
    descriptor = provider.resolve("a1")  # must not raise
    assert descriptor is not None
    assert descriptor.license.value == "missing"
    assert descriptor.sha256 == ""
    assert descriptor.source_uri == "locked.bin"
