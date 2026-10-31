"""TAEP Phase 1 契约测试 — schema / capability / snapshot / validator / segment。

覆盖设计文档 §5.2 / §5.4 / §5.6 / §5.8 的验收 fixture：
合法单 clip、track overlap（结构层）、离线素材、source 越界、未知 effect、
过期 revision、24/23.976/29.97 帧边界、路径脱敏、operation 白名单。

不依赖 LLM、不写 UI、不调用 GPU。
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.capability_manifest import (
    build_manifest,
    get_effect_capability,
    is_effect_known,
    is_operation_supported,
)
from src.features.edit.ai_editing.diagnostics import (
    DiagnosticSeverity,
    TaepDiagnosticCode,
)
from src.features.edit.ai_editing.edit_project_service import ProjectSequences
from src.features.edit.ai_editing.editorial_models import (
    DecisionState,
    DurationTarget,
    EditDecision,
    EditVariant,
    EditorialBrief,
    FrameRange,
    HardConstraint,
    Platform,
    SegmentKind,
    SemanticSegment,
    SourceRef,
    StoryBeat,
    UserOverride,
    VariantScores,
    VariantStatus,
)
from src.features.edit.ai_editing.models import (
    CapabilityManifest,
    DetailLevel,
    EditOperation,
    EditPlan,
    OperationKind,
    PlanMode,
    PlanStatus,
    PlanTarget,
)
from src.features.edit.ai_editing.plan_validator import validate_plan
from src.features.edit.ai_editing.schema import (
    build_plan_json_schema,
    negotiate_protocol,
    validate_plan_structure,
)
from src.features.edit.ai_editing.semantic_index import SemanticIndex
from src.features.edit.ai_editing.snapshot_service import build_snapshot
from src.features.edit.core.clip_params import CropParams, SpeedParams, TransformParams
from src.features.edit.core.project import EditProject, MediaAsset, TimelineSequenceRef
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.media.metadata import Rational


# ================================================================
# Fixtures
# ================================================================


def _clip(clip_id: str, track_id: str, start: int = 0, duration: int = 100) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id, asset_id="a1", track_id=track_id,
        timeline_start=start, timeline_duration=duration,
        source_start=0, source_duration=duration,
        speed=SpeedParams(), transform=TransformParams(), crop=CropParams(),
    )


def _state_with_asset(duration_frames: int | None = 500) -> ProjectSequences:
    seq = TimelineSequence(
        timeline_id="t1", name="Main", fps=Rational(24000, 1001),
        width=1920, height=1080, sample_rate=48000,
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0),),
    )
    asset = MediaAsset(
        asset_id="a1", path="/secret/user/home/interview.mp4",
        media_type="video", display_name="采访 A",
        metadata={"duration_frames": duration_frames} if duration_frames else {},
    )
    ref = TimelineSequenceRef(
        timeline_id="t1", name="Main", fps=float(seq.fps),
        width=1920, height=1080, sample_rate=48000,
    )
    project = EditProject(
        project_id="p1", name="AI Project",
        assets={"a1": asset},
        timelines={"t1": ref},
        root_bin_id="root",
        active_timeline_id="t1",
    )
    return ProjectSequences(project=project, sequences={"t1": seq})


# ================================================================
# schema.py
# ================================================================


def test_negotiate_protocol_accepts_1_0():
    assert negotiate_protocol("taep/1.0") == "taep/1.0"
    assert negotiate_protocol("1.0") == "taep/1.0"  # tolerate missing prefix
    assert negotiate_protocol("taep/2.0") is None


def test_validate_plan_structure_missing_fields():
    findings = validate_plan_structure({})
    codes = {f[1] for f in findings}
    assert TaepDiagnosticCode.SCHEMA_INVALID in codes
    # missing plan_id, base_revision, target, operations
    msgs = " ".join(f[2] for f in findings)
    assert "plan_id" in msgs and "operations" in msgs


def test_validate_plan_structure_unknown_kind():
    plan = {
        "plan_id": "p", "base_revision": 0,
        "target": {"project_id": "p1", "timeline_id": "t1"},
        "operations": [{"op_id": "o1", "kind": "ripple_delete", "params": {}}],
    }
    findings = validate_plan_structure(plan)
    assert any(f[1] == TaepDiagnosticCode.UNSUPPORTED_OPERATION for f in findings)


def test_validate_plan_structure_readonly_tool_rejected():
    plan = {
        "plan_id": "p", "base_revision": 0,
        "target": {"project_id": "p1", "timeline_id": "t1"},
        "operations": [{"op_id": "o1", "kind": "get_snapshot", "params": {}}],
    }
    findings = validate_plan_structure(plan)
    assert any(f[1] == TaepDiagnosticCode.UNSUPPORTED_OPERATION for f in findings)


def test_validate_plan_structure_duplicate_op_id():
    plan = {
        "plan_id": "p", "base_revision": 0,
        "target": {"project_id": "p1", "timeline_id": "t1"},
        "operations": [
            {"op_id": "dup", "kind": "add_clip", "params": {"track_id": "v1", "asset_id": "a1", "source": {"start": 0, "duration": 10}, "timeline": {"start": 0, "duration": 10}}},
            {"op_id": "dup", "kind": "add_clip", "params": {"track_id": "v1", "asset_id": "a1", "source": {"start": 0, "duration": 10}, "timeline": {"start": 10, "duration": 10}}},
        ],
    }
    findings = validate_plan_structure(plan)
    assert any("duplicate op_id" in f[2] for f in findings)


def test_validate_plan_structure_add_clip_missing_required():
    plan = {
        "plan_id": "p", "base_revision": 0,
        "target": {"project_id": "p1", "timeline_id": "t1"},
        "operations": [{"op_id": "o1", "kind": "add_clip", "params": {"track_id": "v1"}}],
    }
    findings = validate_plan_structure(plan)
    msg = " ".join(f[2] for f in findings)
    assert "asset_id" in msg and "source" in msg and "timeline" in msg


def test_validate_plan_structure_unimplemented_kind_rejected_at_schema():
    """T3.1：传入 compilable_operations 时，'协议合法但未实现'的 kind 在 schema
    阶段就被拒绝（UNSUPPORTED_OPERATION），而非留到 domain 层（TAEP §4.3）。"""
    # add_subtitle 已经接入 compiler；这里用 request_export 验证未实现 kind。
    plan = {
        "plan_id": "p", "base_revision": 0,
        "target": {"project_id": "p1", "timeline_id": "t1"},
        "operations": [{"op_id": "o1", "kind": "request_export", "params": {}}],
    }
    # 不传 compilable → schema 通过（旧行为）
    findings_old = validate_plan_structure(plan)
    assert not any(f[1] == TaepDiagnosticCode.UNSUPPORTED_OPERATION for f in findings_old)

    # 传 compilable（只含 add_clip）→ schema 直接拒绝
    findings = validate_plan_structure(
        plan, compilable_operations=frozenset({"add_clip"}),
    )
    unsup = [f for f in findings if f[1] == TaepDiagnosticCode.UNSUPPORTED_OPERATION]
    assert len(unsup) == 1
    assert "not yet implemented" in unsup[0][2]


def test_validate_plan_structure_compilable_skips_param_check():
    """T3.1：未实现 kind 被拒后不再校验其参数（参数再合法也无意义）。"""
    plan = {
        "plan_id": "p", "base_revision": 0,
        "target": {"project_id": "p1", "timeline_id": "t1"},
        "operations": [{"op_id": "o1", "kind": "add_subtitle", "params": {}}],  # 缺必填
    }
    findings = validate_plan_structure(
        plan, compilable_operations=frozenset({"add_clip"}),
    )
    # 只有 UNSUPPORTED_OPERATION，没有"missing required param"
    codes = [f[1] for f in findings]
    assert TaepDiagnosticCode.UNSUPPORTED_OPERATION in codes
    assert sum(1 for c in codes if c == TaepDiagnosticCode.UNSUPPORTED_OPERATION) == 1


def test_validate_plan_structure_compilable_empty_disables_check():
    """T3.1：传空集合 = 显式禁用早拒绝（兼容旧行为/测试）。"""
    plan = {
        "plan_id": "p", "base_revision": 0,
        "target": {"project_id": "p1", "timeline_id": "t1"},
        "operations": [{"op_id": "o1", "kind": "add_subtitle", "params": {}}],
    }
    findings = validate_plan_structure(plan, compilable_operations=frozenset())
    assert not any(f[1] == TaepDiagnosticCode.UNSUPPORTED_OPERATION for f in findings)


def test_build_plan_json_schema_has_enum():
    schema = build_plan_json_schema()
    assert schema["title"] == "TAEP EditPlan"
    kinds = schema["properties"]["operations"]["items"]["properties"]["kind"]["enum"]
    assert "add_clip" in kinds and "set_marker" in kinds


# ================================================================
# capability_manifest.py
# ================================================================


def test_manifest_built_from_real_registry():
    m = build_manifest()
    d = m.to_dict()
    # fps 含 NTSC
    assert "24000/1001" in d["timebase_support"]["accepted_fps"]
    # transitions 与白名单一致
    from src.features.edit.core.transition_catalog import TRANSITION_TYPE_OPTIONS
    assert set(d["transitions"]) == set(TRANSITION_TYPE_OPTIONS)
    # operations 含白名单
    assert "add_clip" in d["operations"] and "set_effect" in d["operations"]
    # effects 非空（来自真实 registry）
    assert len(d["effects"]) > 0
    # export codecs 非空
    assert len(d["export"]["codecs"]) > 0


def test_manifest_effect_params_extracted():
    m = build_manifest()
    # blur 效果应有参数（T-P1-20：transform ghost 已从 registry 移除，
    # clip-native 能力不再是 manifest 效果条目）
    cap = get_effect_capability(m, "blur")
    assert cap is not None
    assert len(cap.parameters) > 0
    # 参数含 name/type
    p0 = cap.parameters[0]
    assert "name" in p0 and "type" in p0
    # ghost 类型不再暴露给 AI 规划器
    for ghost in ("transform", "crop", "opacity", "vignette"):
        assert get_effect_capability(m, ghost) is None, (
            f"{ghost} 不应出现在 manifest（T-P1-20 ghost 清除）"
        )
    # 导出语义显式暴露（T-P1-20）：可导出效果 + preview_only 插件
    exportable = [c for c in m.effects if c.target == "video_clip"
                  and c.exportable]
    assert exportable, "至少内建 7 效果应标记 exportable"
    preview_only = [c for c in m.effects if c.preview_only]
    for c in preview_only:
        assert c.exportable is False
        assert c.delivery_restriction_code == "plugin_preview_only"


def test_is_operation_supported_and_effect_known():
    m = build_manifest()
    assert is_operation_supported(m, "add_clip") is True
    assert is_operation_supported(m, "ripple_delete") is False
    assert is_effect_known(m, "blur") is True
    assert is_effect_known(m, "nonexistent_effect") is False
    assert is_effect_known(m, "transform") is False


def test_manifest_only_advertises_compilable_operations():
    """M3.1：manifest 与 compiler 必须同一真相——不暴露无法编译的 operation。

    审查发现旧 build_manifest 无条件声明全部 OperationKind，但 compiler 对 9 个
    抛 CompileError。模型据此生成 plan 必然失败。现在 manifest 只暴露
    COMPILABLE_OPERATIONS。
    """
    from src.features.edit.ai_editing.plan_compiler import COMPILABLE_OPERATIONS

    m = build_manifest()
    advertised = set(m.operations)
    compilable = {k.value for k in COMPILABLE_OPERATIONS}
    # 每个 manifest 暴露的 operation 都必须可编译
    assert advertised >= compilable or advertised == compilable
    # 这些过去被错误暴露、但 compiler 不支持的 kind 现在对模型隐藏
    for unsupported in (
        "create_timeline", "request_export", "set_range", "add_review_note",
        "remove_transition",
    ):
        assert unsupported not in advertised, (
            f"{unsupported!r} advertised in manifest but not compilable (M3.1)"
        )


def test_compilable_operation_round_trips_through_compile():
    """M3.1 真值表：每个 COMPILABLE_OPERATIONS 至少有一个 compiler 分支。"""
    from src.features.edit.ai_editing.plan_compiler import COMPILABLE_OPERATIONS, compile_plan
    from src.features.edit.ai_editing.models import EditOperation

    # add_clip 是其余 op 的依赖；用一个最小 plan 验证 add_clip 能编译。
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        operations=(EditOperation("op1", OperationKind.ADD_CLIP, params={
            "track_id": "v1", "asset_id": "a1",
            "source": {"start": 0, "duration": 10},
            "timeline": {"start": 0, "duration": 10},
        }),),
    )
    compiled = compile_plan(plan)
    assert OperationKind.ADD_CLIP in COMPILABLE_OPERATIONS
    assert compiled.operation_count == 1


def test_subtitle_plan_previews_with_stable_ids_and_diff():
    """字幕计划走同一 compiler，preview 不写入且包含 subtitle cue diff。"""
    from src.features.edit.ai_editing.transaction_service import TransactionService

    plan = EditPlan.create(
        PlanTarget("p1", "t1"), 0,
        plan_id="subtitle-plan-1",
        operations=(
            EditOperation(
                "ensure", OperationKind.ENSURE_SUBTITLE_TRACK,
                params={"track_id": "sub-ai", "track_name": "AI 字幕"},
            ),
            EditOperation(
                "cue-1", OperationKind.ADD_SUBTITLE,
                params={
                    "track_id": "sub-ai",
                    "timeline": {"start": 12, "duration": 24},
                    "text": "你好，世界",
                },
            ),
        ),
    )
    txn = TransactionService()
    first = txn.preview(plan, _state_with_asset())
    second = txn.preview(plan, _state_with_asset())

    assert first.has_errors is False
    assert first.diff is not None
    assert first.diff.total_changes == 1
    assert first.diff.added_clips[0].after["text"] == "你好，世界"
    assert first.diff_hash == second.diff_hash
    assert first.proposed_sequence.subtitle_tracks[0].track_id == "sub-ai"
    assert first.proposed_sequence.subtitle_tracks[0].subtitle_clips[0].clip_id == (
        "subtitle-plan-1:subtitle:cue-1"
    )


# ================================================================
# snapshot_service.py — 路径脱敏 + detail level
# ================================================================


def test_snapshot_redacts_absolute_path():
    state = _state_with_asset()
    snap = build_snapshot(state, detail_level=DetailLevel.MINIMAL)
    d = snap.to_dict()
    # 绝不返回本机绝对路径
    assert "/secret/user/home" not in str(d)
    # 用 display_name
    assert d["assets"][0]["name"] == "采访 A"
    assert d["assets"][0]["asset_id"] == "a1"


def test_snapshot_includes_revision_and_fps_rational():
    state = _state_with_asset()
    snap = build_snapshot(state, detail_level=DetailLevel.STRUCTURE)
    d = snap.to_dict()
    tl = d["active_timeline"]
    assert tl["format"]["fps"] == "24000/1001"  # Rational 字符串
    assert tl["revision"] == 0


def test_snapshot_detail_levels_differ():
    state = _state_with_asset()
    minimal = build_snapshot(state, detail_level=DetailLevel.MINIMAL).to_dict()
    full = build_snapshot(state, detail_level=DetailLevel.FULL).to_dict()
    # minimal 不带 active_timeline 详情
    assert minimal["detail_level"] == "minimal"


def test_snapshot_asset_limit_pagination():
    # 多素材
    state = _state_with_asset()
    from dataclasses import replace

    assets = {f"a{i}": MediaAsset(asset_id=f"a{i}", path=f"/p/{i}.mp4", media_type="video", display_name=f"A{i}", metadata={}) for i in range(10)}
    state2 = ProjectSequences(
        project=replace(state.project, assets=assets),
        sequences=state.sequences,
    )
    snap = build_snapshot(state2, detail_level=DetailLevel.MINIMAL, asset_limit=3)
    assert len(snap.assets) == 3


# ================================================================
# semantic_index.py
# ================================================================


def test_semantic_index_query_and_coverage():
    idx = SemanticIndex()
    idx.add(SemanticSegment(
        segment_id="s1", asset_id="a1",
        source_range=FrameRange(0, 100), kind=SegmentKind.TRANSCRIPT,
        payload={"text": "产品卖点"}, confidence=0.9,
    ))
    idx.add(SemanticSegment(
        segment_id="s2", asset_id="a1",
        source_range=FrameRange(100, 100), kind=SegmentKind.TRANSCRIPT,
        payload={"text": "call to action"}, confidence=0.7, user_verified=True,
    ))
    idx.add(SemanticSegment(
        segment_id="s3", asset_id="a2",
        source_range=FrameRange(0, 50), kind=SegmentKind.SHOT,
        payload={"labels": ["产品", "特写"]}, confidence=0.8,
    ))
    # 按 asset
    assert len(idx.for_asset("a1")) == 2
    # 按 kind
    shots = idx.query(kind=SegmentKind.SHOT)
    assert len(shots) == 1 and shots[0].segment_id == "s3"
    # label 模糊
    prod = idx.query(label_contains="产品")
    assert {s.segment_id for s in prod} == {"s1", "s3"}
    # within range
    early = idx.query(within=FrameRange(50, 10))
    assert {s.segment_id for s in early} == {"s1"}  # s1 [0,100) ∩ [50,60)
    # user_verified 优先排序
    verified = idx.query(asset_id="a1", user_verified_only=True)
    assert len(verified) == 1 and verified[0].user_verified
    # coverage 合并
    cov = idx.coverage("a1", SegmentKind.TRANSCRIPT)
    assert cov == [(0, 200)]  # [0,100)+[100,200) 相邻合并


# ================================================================
# plan_validator.py — domain 校验
# ================================================================


def test_validate_valid_add_clip_plan():
    state = _state_with_asset(duration_frames=500)
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), base_revision=0,
        operations=(EditOperation(
            "op1", OperationKind.ADD_CLIP,
            params={
                "track_id": "v1", "asset_id": "a1",
                "source": {"start": 100, "duration": 50},
                "timeline": {"start": 0, "duration": 50},
            },
        ),),
    )
    report = validate_plan(plan, state=state)
    assert report.status == PlanStatus.VALID
    assert not report.has_errors
    assert report.current_revision == 0


def test_validate_revision_conflict_marks_stale():
    state = _state_with_asset()
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), base_revision=5,  # current is 0
        operations=(EditOperation("op1", OperationKind.SET_MARKER, params={"frame": 10}),),
    )
    report = validate_plan(plan, state=state)
    assert report.status == PlanStatus.STALE
    assert any(d.code == TaepDiagnosticCode.REVISION_CONFLICT for d in report.diagnostics)


def test_validate_rejects_plan_for_another_project():
    state = _state_with_asset()
    plan = EditPlan.create(
        PlanTarget("other-project", "t1"), base_revision=0,
        operations=(EditOperation("op1", OperationKind.SET_MARKER, params={"frame": 10}),),
    )
    report = validate_plan(plan, state=state)
    assert report.has_errors
    assert any(d.code == TaepDiagnosticCode.PROJECT_NOT_FOUND for d in report.errors)


def test_validate_source_range_out_of_bounds():
    state = _state_with_asset(duration_frames=500)
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), base_revision=0,
        operations=(EditOperation(
            "op1", OperationKind.ADD_CLIP,
            params={
                "track_id": "v1", "asset_id": "a1",
                "source": {"start": 480, "duration": 100},  # end=580 > 500
                "timeline": {"start": 0, "duration": 100},
            },
        ),),
    )
    report = validate_plan(plan, state=state)
    assert report.has_errors
    assert any(d.code == TaepDiagnosticCode.SOURCE_RANGE_INVALID for d in report.errors)


def test_validate_asset_offline():
    from dataclasses import replace

    state = _state_with_asset()
    offline_asset = replace(state.project.assets["a1"], is_offline=True)
    state2 = ProjectSequences(
        project=replace(state.project, assets={"a1": offline_asset}),
        sequences=state.sequences,
    )
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), base_revision=0,
        operations=(EditOperation(
            "op1", OperationKind.ADD_CLIP,
            params={"track_id": "v1", "asset_id": "a1",
                    "source": {"start": 0, "duration": 50},
                    "timeline": {"start": 0, "duration": 50}},
        ),),
    )
    report = validate_plan(plan, state=state2)
    assert any(d.code == TaepDiagnosticCode.ASSET_OFFLINE for d in report.errors)


def test_validate_track_type_mismatch():
    state = _state_with_asset()
    # 把 video asset 放到不存在的 audio track（序列里只有 v1 video）
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), base_revision=0,
        operations=(EditOperation(
            "op1", OperationKind.ADD_CLIP,
            params={"track_id": "a1_missing", "asset_id": "a1",
                    "source": {"start": 0, "duration": 50},
                    "timeline": {"start": 0, "duration": 50}},
        ),),
    )
    report = validate_plan(plan, state=state)
    assert any(d.code == TaepDiagnosticCode.TRACK_NOT_FOUND for d in report.errors)


def test_validate_unknown_effect():
    state = _state_with_asset()
    # 先放一个 clip 以便 set_effect 引用
    state2 = ProjectSequences(
        project=state.project,
        sequences={"t1": state.sequences["t1"].with_track_updated(
            EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=(_clip("c1", "v1"),))
        )},
    )
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), base_revision=0,
        operations=(EditOperation(
            "op1", OperationKind.SET_EFFECT,
            params={"clip_id": "c1", "effect_type": "made_up_effect"},
        ),),
    )
    report = validate_plan(plan, state=state2)
    assert any(d.code == TaepDiagnosticCode.EFFECT_INVALID for d in report.errors)


def test_validate_effect_param_out_of_range():
    state = _state_with_asset()
    state2 = ProjectSequences(
        project=state.project,
        sequences={"t1": state.sequences["t1"].with_track_updated(
            EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=(_clip("c1", "v1"),))
        )},
    )
    m = build_manifest()
    # blur.radius_x 越界（schema max 500；传 -10 越界）。
    # T-P1-20：transform 已从 registry 移除，不再作为效果条目——越界校验
    # 用真实 catalog 效果验证。
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), base_revision=0,
        operations=(EditOperation(
            "op1", OperationKind.SET_EFFECT,
            params={"clip_id": "c1", "effect_type": "blur",
                    "parameters": {"radius_x": -10.0}},
        ),),
    )
    report = validate_plan(plan, state=state2, manifest=m)
    assert any(d.code == TaepDiagnosticCode.EFFECT_PARAMETER_INVALID for d in report.errors)


def test_validate_plan_too_large():
    state = _state_with_asset()
    m = build_manifest()
    # 把 limit 设很小
    from dataclasses import replace as _r
    m_small = _r(m, limits={"max_operations_per_plan": 2, "max_preview_frames": 300})
    ops = tuple(
        EditOperation(f"op{i}", OperationKind.SET_MARKER, params={"frame": i})
        for i in range(5)
    )
    plan = EditPlan.create(PlanTarget("p1", "t1"), base_revision=0, operations=ops)
    report = validate_plan(plan, state=state, manifest=m_small)
    assert any(d.code == TaepDiagnosticCode.PLAN_TOO_LARGE for d in report.errors)


def test_validate_plan_unimplemented_kind_reported_once():
    """T3.1：未实现 kind 经 validate_plan 只报一次 UNSUPPORTED_OPERATION
    （schema 层早拒绝 + domain 层不重复报，TAEP §4.3）。"""
    state = _state_with_asset()
    # request_export 协议合法但 compiler 未实现
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), base_revision=0,
        operations=(EditOperation(
            "op1", OperationKind.REQUEST_EXPORT,
            params={},
        ),),
    )
    report = validate_plan(plan, state=state)
    unsup = [d for d in report.diagnostics
             if d.code == TaepDiagnosticCode.UNSUPPORTED_OPERATION]
    assert len(unsup) == 1, f"expected 1 UNSUPPORTED, got {len(unsup)}"


def test_validate_transition_non_adjacent():
    state = _state_with_asset()
    # 两个不相邻的 clip（中间有间隔）
    c1 = _clip("c1", "v1", start=0, duration=50)
    c2 = _clip("c2", "v1", start=200, duration=50)  # 与 c1 不相邻
    state2 = ProjectSequences(
        project=state.project,
        sequences={"t1": state.sequences["t1"].with_track_updated(
            EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=(c1, c2))
        )},
    )
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), base_revision=0,
        operations=(EditOperation(
            "op1", OperationKind.ADD_TRANSITION,
            params={"track_id": "v1", "left_clip_id": "c1", "right_clip_id": "c2",
                    "transition_type": "cross_dissolve"},
        ),),
    )
    report = validate_plan(plan, state=state2)
    assert any(d.code == TaepDiagnosticCode.TRANSITION_INVALID for d in report.errors)
    assert any("adjacent" in d.message for d in report.errors)


# ---------------------------------------------------------------- #
# M3.3：顺序模拟（add_clip → set_effect 不应误判 clip 不存在）
# ---------------------------------------------------------------- #


def test_validate_sequential_add_clip_then_set_effect_passes():
    """M3.3：add_clip(c1) 后的 set_effect(c1) 应能引用到 c1。

    旧实现对每个 op 独立查 base，c1 不存在 → 误报。顺序模拟后，第 2 个 op 在
    叠加了第 1 个 op 的投影上校验，引用成立。
    """
    state = _state_with_asset(duration_frames=5000)
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), base_revision=0,
        operations=(
            EditOperation("op1", OperationKind.ADD_CLIP, params={
                "clip_id": "c1", "track_id": "v1", "asset_id": "a1",
                "source": {"start": 0, "duration": 100},
                "timeline": {"start": 0, "duration": 100},
            }),
            EditOperation("op2", OperationKind.SET_OPACITY, params={
                "clip_id": "c1", "track_id": "v1", "value": 0.5,
            }),
        ),
    )
    report = validate_plan(plan, state=state)
    assert report.status == PlanStatus.VALID, [
        (d.code.value, d.message) for d in report.errors
    ]
    # 不应有 c1 not found 诊断
    assert not any("c1" in d.message and "not found" in d.message for d in report.diagnostics)


def test_validate_sequential_add_clip_then_add_transition_passes():
    """M3.3：add 两个相邻 clip 后 add_transition 引用它们应通过。"""
    state = _state_with_asset(duration_frames=5000)
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), base_revision=0,
        operations=(
            EditOperation("op1", OperationKind.ADD_CLIP, params={
                "clip_id": "c1", "track_id": "v1", "asset_id": "a1",
                "source": {"start": 0, "duration": 100},
                "timeline": {"start": 0, "duration": 100},
            }),
            EditOperation("op2", OperationKind.ADD_CLIP, params={
                "clip_id": "c2", "track_id": "v1", "asset_id": "a1",
                "source": {"start": 100, "duration": 100},
                "timeline": {"start": 100, "duration": 100},
            }),
            EditOperation("op3", OperationKind.ADD_TRANSITION, params={
                "track_id": "v1", "left_clip_id": "c1", "right_clip_id": "c2",
                "transition_type": "cross_dissolve",
            }),
        ),
    )
    report = validate_plan(plan, state=state)
    assert report.status == PlanStatus.VALID, [
        (d.code.value, d.message) for d in report.errors
    ]


def test_validate_non_sequential_reference_still_detected():
    """M3.3 回归保护：引用一个既不在 base、也不被任何 op 添加的 clip 仍应报错。"""
    state = _state_with_asset(duration_frames=5000)
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), base_revision=0,
        operations=(
            EditOperation("op1", OperationKind.ADD_CLIP, params={
                "clip_id": "c1", "track_id": "v1", "asset_id": "a1",
                "source": {"start": 0, "duration": 100},
                "timeline": {"start": 0, "duration": 100},
            }),
            EditOperation("op2", OperationKind.SET_OPACITY, params={
                "clip_id": "ghost", "track_id": "v1", "value": 0.5,  # 不存在
            }),
        ),
    )
    report = validate_plan(plan, state=state)
    assert report.has_errors
    assert any("ghost" in d.message for d in report.errors)


# ================================================================
# editorial_models 边界（24/23.976/29.97 帧边界、不可变）
# ================================================================


def test_frame_range_ntsc_boundaries():
    # 23.976 fps 下一秒 ≈ 24 帧（24000/1001）；边界用整帧
    r = FrameRange(0, 24)  # ~1s @ 23.976
    assert r.end == 24
    assert r.contains(23) and not r.contains(24)


def test_editorial_brief_revision_bump_immutable():
    b = EditorialBrief(brief_id="b1", target_duration=DurationTarget(750, 700, 800))
    b2 = b.with_revision_bumped()
    assert b2.revision == 1 and b.revision == 0


def test_variant_accept_does_not_overwrite_status():
    v = EditVariant(variant_id="v1", brief_id="b1", brief_revision=0)
    accepted = v.with_status(VariantStatus.ACCEPTED)
    assert accepted.status == VariantStatus.ACCEPTED
    assert v.status == VariantStatus.PROPOSED  # immutable


def test_decision_rejection_state():
    d = EditDecision(decision_id="d1", action="select_source_range",
                     subject=SourceRef("a1", FrameRange(0, 10)))
    rejected = d.with_state(DecisionState.REJECTED)
    assert rejected.state == DecisionState.REJECTED
    assert d.state == DecisionState.PROPOSED


def test_preview_only_effect_rejected_as_delivery_step():
    """T-P2-22 条目 2：preview_only 插件不得作为交付步骤进入计划。

    plan_validator 在计划期给出 EFFECT_NOT_DELIVERABLE（可解释限制码），
    而不是让计划通过后在 Deliver preflight 才被阻断。
    """
    from src.features.edit.ai_editing.diagnostics import (
        DiagnosticSeverity, TaepDiagnosticCode,
    )
    from src.features.edit.ai_editing.models import (
        EditOperation, OperationKind, PlanTarget, EditPlan,
    )
    from src.features.edit.ai_editing.plan_validator import validate_plan
    from src.features.edit.core.timeline_v2 import EditTrack

    state = _state_with_asset()
    state2 = ProjectSequences(
        project=state.project,
        sequences={"t1": state.sequences["t1"].with_track_updated(
            EditTrack(track_id="v1", name="V1", track_type="video",
                      index=0, clips=(_clip("c1", "v1"),))
        )},
    )
    m = build_manifest()
    preview_only = [c for c in m.effects if c.preview_only]
    if not preview_only:
        pytest.skip("manifest 无 preview_only 插件（环境插件缺失）")
    etype = preview_only[0].effect_type
    plan = EditPlan.create(
        PlanTarget("p1", "t1"), base_revision=0,
        operations=(EditOperation(
            "op1", OperationKind.SET_EFFECT,
            params={"clip_id": "c1", "effect_type": etype,
                    "parameters": {}},
        ),),
    )
    report = validate_plan(plan, state=state2, manifest=m)
    codes = [d.code for d in report.errors]
    assert TaepDiagnosticCode.EFFECT_NOT_DELIVERABLE in codes, (
        f"preview_only 效果 {etype!r} 未在计划期被拒绝: {codes}"
    )
    hit = next(d for d in report.errors
               if d.code == TaepDiagnosticCode.EFFECT_NOT_DELIVERABLE)
    assert hit.details is not None
    assert hit.details.get("restriction") == "plugin_preview_only"
    assert hit.details.get("effect_type") == etype


def test_set_clip_fade_compiles_to_executable_command():
    """W01 回放修复回归：set_clip_fade 旧实现发 UpdateClipParamCommand
    的未知 param_type（可编译但永不可应用）。现在必须编译为
    ChangeClipFadeCommand 且真实可执行。"""
    from src.features.edit.ai_editing.models import OperationKind
    from src.features.edit.ai_editing.plan_compiler import (
        CompileError, _compile_set_fade,
    )
    from src.features.edit.core.commands import ChangeClipFadeCommand
    from src.features.edit.core.timeline_v2 import (
        ClipInstance, EditTrack, TimelineSequence,
    )

    op = EditOperation(
        op_id="op-fade-1", kind=OperationKind.SET_CLIP_FADE,
        params={"clip_id": "c1", "track_id": "v1", "fade_in_duration": 5},
    )
    cmd = _compile_set_fade(op, op.params)
    assert isinstance(cmd, ChangeClipFadeCommand)
    assert cmd.fade_type == "in" and cmd.new_fade_in == 5

    timeline = TimelineSequence(
        timeline_id="t", name="T", fps=25, width=64, height=48,
        sample_rate=48000)
    track = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    timeline = timeline.with_track_added(track)
    clip = ClipInstance(
        clip_id="c1", asset_id="a", track_id="v1",
        timeline_start=0, timeline_duration=20,
        source_start=0, source_duration=20)
    timeline = timeline.with_clip_added("v1", clip)

    new_tl, _updated = cmd.apply(timeline)
    applied_clip = new_tl.get_track_by_id("v1").get_clip_by_id("c1")
    assert applied_clip.fade_in_duration == 5

    # fade_out 分支同样可执行
    op_out = EditOperation(
        op_id="op-fade-2", kind=OperationKind.SET_CLIP_FADE,
        params={"clip_id": "c1", "track_id": "v1", "fade_out_duration": 7},
    )
    cmd_out = _compile_set_fade(op_out, op_out.params)
    assert isinstance(cmd_out, ChangeClipFadeCommand)
    assert cmd_out.fade_type == "out" and cmd_out.new_fade_out == 7
    new_tl2, _ = cmd_out.apply(timeline)
    assert (new_tl2.get_track_by_id("v1").get_clip_by_id("c1")
            .fade_out_duration) == 7

    # AUD-14100：缺 fade 值时抛 CompileError（携带 op_id/kind），不再抛
    # 裸 ValueError 逃逸 preview() 的 except CompileError 边界。
    op_bad = EditOperation(
        op_id="op-fade-3", kind=OperationKind.SET_CLIP_FADE,
        params={"clip_id": "c1", "track_id": "v1"},
    )
    with pytest.raises(CompileError, match="fade_in_duration") as exc_info:
        _compile_set_fade(op_bad, op_bad.params)
    assert exc_info.value.op_id == "op-fade-3"


def test_set_clip_fade_without_fade_fields_rejected_at_validation():
    """AUD-14100 schema 层回归：set_clip_fade 无任何 fade 字段的 plan 在
    结构校验阶段即被拒（可修复诊断），不得以 VALID 放行到 compile。"""
    plan_dict = {
        "plan_id": "p1", "base_revision": 0,
        "target": {"project_id": "proj", "timeline_id": "t"},
        "operations": [{
            "op_id": "op1", "kind": "set_clip_fade",
            "params": {"clip_id": "c1", "track_id": "v1"},
        }],
    }
    findings = validate_plan_structure(plan_dict)
    errors = [f for f in findings if f[0] == DiagnosticSeverity.ERROR]
    assert errors, "fade-less set_clip_fade must not pass structural validation"
    assert any("fade_in_duration" in f[2] for f in errors)

    # 两种 fade 字段任一存在即通过该检查
    for fade_key in ("fade_in_duration", "fade_out_duration"):
        plan_ok = {
            "plan_id": "p1", "base_revision": 0,
            "target": {"project_id": "proj", "timeline_id": "t"},
            "operations": [{
                "op_id": "op1", "kind": "set_clip_fade",
                "params": {"clip_id": "c1", "track_id": "v1",
                           fade_key: 5},
            }],
        }
        findings_ok = [
            f for f in validate_plan_structure(plan_ok)
            if f[0] == DiagnosticSeverity.ERROR
        ]
        assert not findings_ok, f"{fade_key} present must pass"

