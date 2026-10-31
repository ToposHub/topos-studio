"""TAEP Phase 5 — 素材分析测试。

验收（TAEP §12 P5）：
- AssetAnalysis fingerprint 不匹配即失效。
- provider 输出经 analysis_to_segments 归一为 SemanticSegment。
- AnalysisService 合并多 provider 结果、按 asset 查询、跨 asset 搜索。
- SceneCutProvider.from_scene_cut_analysis 复用既有 scene_cut 输出。
- 用户修订不被重新分析覆盖（user_verified 分层）。
- 分析只产生候选 segment，不写 timeline。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.analysis_models import (
    AnalysisProvenance,
    AssetAnalysis,
    AssetFingerprint,
    AudioAnalysis,
    FieldProvenance,
    OcrBox,
    OcrDetection,
    TranscriptSegment,
    analysis_to_segments,
)
from src.features.edit.ai_editing.analysis_service import (
    AnalysisService,
    SceneCutProvider,
)
from src.features.edit.ai_editing.editorial_models import SegmentKind
from src.features.edit.ai_editing.jobs import JobStatus


# ================================================================
# AssetFingerprint
# ================================================================


def test_fingerprint_match_by_size_mtime():
    a = AssetFingerprint(size=100, mtime_ns=999)
    b = AssetFingerprint(size=100, mtime_ns=999)
    c = AssetFingerprint(size=200, mtime_ns=999)
    assert a.matches(b)
    assert not a.matches(c)


def test_fingerprint_match_by_content_hash():
    a = AssetFingerprint(size=100, mtime_ns=1, content_hash="abc")
    b = AssetFingerprint(size=999, mtime_ns=2, content_hash="abc")  # hash 优先
    c = AssetFingerprint(size=100, mtime_ns=1, content_hash="xyz")
    assert a.matches(b)
    assert not a.matches(c)


# ================================================================
# AssetAnalysis 有效性
# ================================================================


def test_analysis_invalid_when_fingerprint_mismatch():
    fp = AssetFingerprint(size=100, mtime_ns=1)
    analysis = AssetAnalysis(asset_id="a1", fingerprint=fp)
    assert analysis.is_valid_for(AssetFingerprint(size=100, mtime_ns=1))
    assert not analysis.is_valid_for(AssetFingerprint(size=200, mtime_ns=1))


# ================================================================
# analysis_to_segments
# ================================================================


def test_analysis_to_segments_normalizes_all_kinds():
    analysis = AssetAnalysis(
        asset_id="a1", fingerprint=AssetFingerprint(),
        scene_cuts=({"frame": 0, "confidence": 1.0, "kind": "hard_cut"},
                    {"frame": 100, "confidence": 0.9, "kind": "soft"}),
        transcript=(TranscriptSegment(0, 50, "hello", speaker="S1", confidence=0.95),),
        tags=({"start_frame": 10, "end_frame": 60, "labels": ["product", "closeup"]},),
        audio=AudioAnalysis(integrated_lufs=-18.0, speech_ranges=[(0, 50), (100, 200)]),
        provenance=AnalysisProvenance(provider="test", model="m1"),
    )
    segments = analysis_to_segments(analysis)
    kinds = {s.kind for s in segments}
    assert SegmentKind.SHOT in kinds
    assert SegmentKind.TRANSCRIPT in kinds
    assert SegmentKind.TAG in kinds
    assert SegmentKind.AUDIO in kinds
    # provenance 保留
    assert all(s.provenance.get("provider") == "test" for s in segments)
    # transcript 文本进 payload
    t = next(s for s in segments if s.kind == SegmentKind.TRANSCRIPT)
    assert t.payload["text"] == "hello" and t.payload["speaker"] == "S1"


# ================================================================
# AnalysisService
# ================================================================


def test_service_ingest_and_query():
    svc = AnalysisService()
    analysis = AssetAnalysis(
        asset_id="a1", fingerprint=AssetFingerprint(),
        transcript=(TranscriptSegment(0, 50, "产品卖点", confidence=0.9),),
        provenance=AnalysisProvenance(provider="test"),
    )
    segments = tuple(analysis_to_segments(analysis))
    svc.ingest("a1", analysis, segments)
    assert svc.get_status("a1") == "ready"
    assert len(svc.get_segments("a1")) == 1
    # search
    results = svc.search_assets(text="产品")
    assert len(results) == 1


def test_service_merges_multiple_providers():
    svc = AnalysisService()
    # provider 1: transcript
    a1 = AssetAnalysis(asset_id="a1", fingerprint=AssetFingerprint(),
                       transcript=(TranscriptSegment(0, 50, "hi"),))
    svc.ingest("a1", a1, tuple(analysis_to_segments(a1)))
    # provider 2: tags（同 asset）
    a2 = AssetAnalysis(asset_id="a1", fingerprint=AssetFingerprint(),
                       tags=({"start_frame": 0, "end_frame": 30, "labels": ["broll"]},))
    svc.ingest("a1", a2, tuple(analysis_to_segments(a2)))
    merged = svc.get_analysis("a1")
    assert len(merged.transcript) == 1
    assert len(merged.tags) == 1
    # segments 去重合并
    segs = svc.get_segments("a1")
    assert len(segs) == 2


def test_service_invalidation_on_fingerprint_mismatch():
    svc = AnalysisService()
    fp = AssetFingerprint(size=100, mtime_ns=1)
    analysis = AssetAnalysis(asset_id="a1", fingerprint=fp,
                             scene_cuts=({"frame": 0, "confidence": 1.0},))
    svc.ingest("a1", analysis, tuple(analysis_to_segments(analysis)))
    assert svc.is_valid("a1", AssetFingerprint(size=100, mtime_ns=1))
    # 素材被替换
    assert not svc.is_valid("a1", AssetFingerprint(size=999, mtime_ns=2))
    svc.invalidate("a1")
    assert svc.get_analysis("a1") is None


def test_request_analysis_local_provider_runs():
    svc = AnalysisService()
    job, err = svc.request_analysis("a1", AssetFingerprint(size=100, mtime_ns=1), fps="24/1")
    assert err == ""
    assert job is not None
    assert job.status == JobStatus.SUCCEEDED
    # SceneCutProvider 默认返回空 cuts，但仍 ingest 了记录
    assert svc.get_status("a1") == "pending" or svc.get_status("a1") == "ready"


def test_request_analysis_unknown_provider():
    svc = AnalysisService()
    job, err = svc.request_analysis("a1", AssetFingerprint(), provider_id="bogus")
    assert job is None
    assert "bogus" in err


# ================================================================
# SceneCutProvider.from_scene_cut_analysis（复用既有输出）
# ================================================================


def test_scene_cut_provider_from_existing_analysis():
    """模拟一个既有 SceneCutAnalysis，验证转换。"""
    from src.features.edit.core.scene_cut.models import (
        SceneCutCandidate, SceneCutDecision, SceneCutKind,
    )

    # 构造 candidates（accepted + rejected）
    cands = (
        SceneCutCandidate(source_frame=0, pts=0, seconds=0.0,
                          confidence=1.0, kind=SceneCutKind.HARD_CUT, decision=SceneCutDecision.ACCEPTED),
        SceneCutCandidate(source_frame=100, pts=4.16, seconds=4.16,
                          confidence=0.9, kind=SceneCutKind.SOFT_TRANSITION, decision=SceneCutDecision.ACCEPTED),
        SceneCutCandidate(source_frame=200, pts=8.33, seconds=8.33,
                          confidence=0.5, kind=SceneCutKind.UNKNOWN, decision=SceneCutDecision.REJECTED),
    )

    # 用一个最小占位对象（duck-typing）
    @dataclass
    class FakeSceneAnalysis:
        candidates: tuple
        detector_version: str = "v1.2"
        created_at: str = "2026-07-31"

    fake = FakeSceneAnalysis(candidates=cands)
    analysis, segments = SceneCutProvider.from_scene_cut_analysis(
        fake, "a1", AssetFingerprint(size=100, mtime_ns=1), fps="25/1"
    )
    # rejected cut 被跳过 -> 只剩 2 个
    assert len(analysis.scene_cuts) == 2
    assert analysis.scene_cuts[0]["frame"] == 0
    assert analysis.scene_cuts[1]["frame"] == 100
    # segments 全是 SHOT
    assert all(s.kind == SegmentKind.SHOT for s in segments)
    assert len(segments) == 2
    # provenance 保留 detector_version
    assert analysis.provenance.model == "v1.2"


def test_analysis_does_not_write_timeline():
    """分析只产生候选 segment，不触碰 timeline（验证 service 无 timeline 依赖）。"""
    svc = AnalysisService()
    analysis = AssetAnalysis(asset_id="a1", fingerprint=AssetFingerprint(),
                             transcript=(TranscriptSegment(0, 10, "text"),))
    svc.ingest("a1", analysis, tuple(analysis_to_segments(analysis)))
    # service 没有 commit/apply/edit 方法 —— 只读查询
    public_methods = [m for m in dir(svc) if not m.startswith("_") and callable(getattr(svc, m))]
    assert not any("commit" in m or "apply" in m or "edit" in m.lower() for m in public_methods)


# ================================================================
# M2: field-level provenance（§18.2）
# ================================================================


def test_field_provenance_per_field_access():
    """FieldProvenance 按字段独立存取来源。"""
    from src.features.edit.ai_editing.analysis_models import FieldProvenance

    fp = FieldProvenance(
        scene_cuts=AnalysisProvenance(provider="scene_cut_local", model="v1"),
        transcript=AnalysisProvenance(provider="whisperx_local", model="v2"),
    )
    assert fp.for_field("scene_cuts").provider == "scene_cut_local"
    assert fp.for_field("transcript").model == "v2"
    assert fp.for_field("audio") is None  # 未设置


def test_field_provenance_with_field_immutable_update():
    from src.features.edit.ai_editing.analysis_models import FieldProvenance

    fp = FieldProvenance(scene_cuts=AnalysisProvenance(provider="a"))
    updated = fp.with_field("transcript", AnalysisProvenance(provider="b"))
    assert updated.for_field("transcript").provider == "b"
    # 原 fp 不变（不可变）
    assert fp.for_field("transcript") is None
    assert updated.for_field("scene_cuts").provider == "a"  # 其他字段保留


def test_field_provenance_unknown_field_rejected():
    from src.features.edit.ai_editing.analysis_models import FieldProvenance

    with pytest.raises(ValueError, match="unknown analysis field"):
        FieldProvenance().with_field("nonexistent", AnalysisProvenance())


def test_field_provenance_round_trip():
    from src.features.edit.ai_editing.analysis_models import FieldProvenance

    fp = FieldProvenance(
        scene_cuts=AnalysisProvenance(provider="scene_cut_local", model="v1.2"),
        transcript=AnalysisProvenance(provider="whisperx", model="base"),
    )
    d = fp.to_dict()
    assert "scene_cuts" in d and "transcript" in d
    fp2 = FieldProvenance.from_dict(d)
    assert fp2 == fp


def test_analysis_to_segments_uses_field_level_provenance():
    """segment.provenance 按字段来源标注（shot=scene_cuts，transcript=transcript）。"""
    from src.features.edit.ai_editing.analysis_models import FieldProvenance

    analysis = AssetAnalysis(
        asset_id="a1", fingerprint=AssetFingerprint(),
        scene_cuts=({"frame": 0, "confidence": 1.0},),
        transcript=(TranscriptSegment(10, 20, "hello"),),
        # 全局 provenance 是 fallback；field_provenance 应优先
        provenance=AnalysisProvenance(provider="global", model="global-m"),
        field_provenance=FieldProvenance(
            scene_cuts=AnalysisProvenance(provider="scene_cut_local", model="v1"),
            transcript=AnalysisProvenance(provider="whisperx_local", model="v2"),
        ),
    )
    segments = analysis_to_segments(analysis)
    by_kind = {s.kind: s for s in segments}
    # shot segment 用 scene_cuts 来源，不是全局
    assert by_kind[SegmentKind.SHOT].provenance["provider"] == "scene_cut_local"
    assert by_kind[SegmentKind.SHOT].provenance["model"] == "v1"
    # transcript segment 用 transcript 来源
    assert by_kind[SegmentKind.TRANSCRIPT].provenance["provider"] == "whisperx_local"
    assert by_kind[SegmentKind.TRANSCRIPT].provenance["model"] == "v2"


def test_analysis_to_segments_falls_back_to_global_provenance():
    """field_provenance 为 None 时回退全局 provenance（向后兼容）。"""
    analysis = AssetAnalysis(
        asset_id="a1", fingerprint=AssetFingerprint(),
        scene_cuts=({"frame": 0, "confidence": 1.0},),
        provenance=AnalysisProvenance(provider="global", model="m"),
        field_provenance=None,
    )
    segments = analysis_to_segments(analysis)
    shot = next(s for s in segments if s.kind == SegmentKind.SHOT)
    assert shot.provenance["provider"] == "global"


def test_analysis_provenance_for_helper():
    """AssetAnalysis.provenance_for 优先 field，回退全局。"""
    from src.features.edit.ai_editing.analysis_models import FieldProvenance

    a = AssetAnalysis(
        asset_id="a1", fingerprint=AssetFingerprint(),
        provenance=AnalysisProvenance(provider="global"),
        field_provenance=FieldProvenance(
            transcript=AnalysisProvenance(provider="asr"),
        ),
    )
    assert a.provenance_for("transcript").provider == "asr"
    assert a.provenance_for("scene_cuts").provider == "global"  # 回退


def test_service_merge_preserves_field_provenance_independently():
    """多 provider 合并：字段级 provenance 独立，不互相覆盖（§18.2）。"""
    from src.features.edit.ai_editing.analysis_models import FieldProvenance

    svc = AnalysisService()
    fp1 = AssetAnalysis(
        asset_id="a1", fingerprint=AssetFingerprint(),
        scene_cuts=({"frame": 0},),
        provenance=AnalysisProvenance(provider="global"),
        field_provenance=FieldProvenance(
            scene_cuts=AnalysisProvenance(provider="scene_cut_local", model="v1"),
        ),
    )
    svc.ingest("a1", fp1, tuple(analysis_to_segments(fp1)))
    # 第二个 provider 填 transcript，不应覆盖 scene_cuts 的来源
    fp2 = AssetAnalysis(
        asset_id="a1", fingerprint=AssetFingerprint(),
        transcript=(TranscriptSegment(0, 10, "text"),),
        provenance=AnalysisProvenance(provider="global"),
        field_provenance=FieldProvenance(
            transcript=AnalysisProvenance(provider="whisperx", model="v2"),
        ),
    )
    svc.ingest("a1", fp2, tuple(analysis_to_segments(fp2)))
    merged = svc.get_analysis("a1")
    assert merged.field_provenance.for_field("scene_cuts").provider == "scene_cut_local"
    assert merged.field_provenance.for_field("transcript").provider == "whisperx"


def test_asset_analysis_to_dict_includes_field_provenance():
    """to_dict 写入 field_provenance（旧 payload 缺该字段时 from_dict 容错）。"""
    from src.features.edit.ai_editing.analysis_models import FieldProvenance

    a = AssetAnalysis(
        asset_id="a1", fingerprint=AssetFingerprint(),
        field_provenance=FieldProvenance(
            transcript=AnalysisProvenance(provider="asr"),
        ),
    )
    d = a.to_dict()
    assert "field_provenance" in d
    assert d["field_provenance"]["transcript"]["provider"] == "asr"


# ================================================================
# M2: SemanticSegment PTS 字段（§18.2）
# ================================================================


def test_segment_with_pts_fields_round_trip():
    """segment 携带 provider 原始 PTS → to_dict/from_dict 保真。"""
    from src.features.edit.ai_editing.editorial_models import (
        FrameRange, SegmentKind, SemanticSegment,
    )

    seg = SemanticSegment(
        segment_id="seg-1", asset_id="a1",
        source_range=FrameRange(24, 24),
        kind=SegmentKind.TRANSCRIPT,
        payload={"text": "hello"},
        source_pts_start=90000, source_pts_end=180000,
        source_timebase="1/90000",
    )
    d = seg.to_dict()
    assert d["source_pts_start"] == 90000
    assert d["source_pts_end"] == 180000
    assert d["source_timebase"] == "1/90000"
    seg2 = SemanticSegment.from_dict(d)
    assert seg2 == seg
    assert seg2.source_pts_start == 90000


def test_segment_without_pts_fields_omits_from_dict():
    """旧 segment（无 PTS）→ dict 不含 PTS 字段（向后兼容旧 payload）。"""
    from src.features.edit.ai_editing.editorial_models import (
        FrameRange, SegmentKind, SemanticSegment,
    )

    seg = SemanticSegment(
        segment_id="seg-1", asset_id="a1",
        source_range=FrameRange(0, 10),
        kind=SegmentKind.SHOT,
    )
    d = seg.to_dict()
    assert "source_pts_start" not in d
    assert "source_timebase" not in d
    # 从旧 dict 加载，PTS 字段为 None
    seg2 = SemanticSegment.from_dict(d)
    assert seg2.source_pts_start is None
    assert seg2.source_timebase is None


# ================================================================
# M2: AnalysisService 三步流（§17.3）
# ================================================================


def test_service_propose_creates_unconfirmed_request():
    from src.features.edit.ai_editing.analysis_request import (
        AnalysisRecipe, AnalysisRequest, RequestState,
    )
    svc = AnalysisService()
    req = AnalysisRequest.create("p1", ["a1"], recipe_id=AnalysisRecipe.FAST_LOCAL)
    stored = svc.propose_analysis(req)
    assert stored.is_confirmed is False
    assert stored.state == RequestState.PROPOSED
    assert svc.get_request(req.request_id) is stored


def test_service_execute_rejects_unconfirmed_request():
    """§17.3 红线：未确认的 request，scheduler 必须拒绝执行。"""
    from src.features.edit.ai_editing.analysis_request import AnalysisRequest
    svc = AnalysisService()
    req = AnalysisRequest.create("p1", ["a1"])
    svc.propose_analysis(req)
    job, err = svc.execute_analysis(req.request_id)
    assert job is None
    assert "not confirmed" in err


def test_service_confirm_then_execute_succeeds():
    from src.features.edit.ai_editing.analysis_request import AnalysisRequest, RequestState
    svc = AnalysisService()
    req = AnalysisRequest.create("p1", ["a1"])
    svc.propose_analysis(req)
    svc.confirm_analysis(req.request_id, confirmed_at=1000.0)
    job, err = svc.execute_analysis(req.request_id)
    assert job is not None, f"expected job, got err={err!r}"
    final = svc.get_request(req.request_id)
    assert final.state == RequestState.COMPLETED
    assert final.is_confirmed


def test_service_confirm_unknown_request_raises():
    svc = AnalysisService()
    with pytest.raises(KeyError):
        svc.confirm_analysis("nonexistent")


def test_service_confirm_twice_rejected():
    """已确认的 request 不能再 confirm（状态机保护）。"""
    from src.features.edit.ai_editing.analysis_request import AnalysisRequest
    svc = AnalysisService()
    req = AnalysisRequest.create("p1", ["a1"])
    svc.propose_analysis(req)
    svc.confirm_analysis(req.request_id, confirmed_at=1.0)
    with pytest.raises(ValueError, match="state"):
        svc.confirm_analysis(req.request_id, confirmed_at=2.0)


def test_service_propose_rejects_already_confirmed():
    """调用方不应直接构造已确认的 request（必须经 confirm_analysis）。"""
    from src.features.edit.ai_editing.analysis_request import AnalysisRequest
    svc = AnalysisService()
    req = AnalysisRequest.create("p1", ["a1"]).with_confirmation(1.0)
    with pytest.raises(ValueError, match="unconfirmed"):
        svc.propose_analysis(req)


def test_service_list_requests_filter_by_project():
    from src.features.edit.ai_editing.analysis_request import AnalysisRequest
    svc = AnalysisService()
    svc.propose_analysis(AnalysisRequest.create("p1", ["a1"]))
    svc.propose_analysis(AnalysisRequest.create("p2", ["a2"]))
    assert len(svc.list_requests(project_id="p1")) == 1
    assert len(svc.list_requests()) == 2


# ================================================================
# M2 退出条件：VFR/CFR fixture 从 evidence 回到 source frame
# ================================================================


def test_evidence_segment_round_trips_to_source_frame_via_timebase():
    """完整链路：provider PTS → TimebaseService → segment.source_range → 回到 frame。

    这是 M2 退出条件的端到端验证（§11.2）。
    """
    from src.features.edit.ai_editing.editorial_models import (
        FrameRange, SegmentKind, SemanticSegment,
    )
    from src.features.edit.ai_editing.timebase import TimebaseService, ProviderTimebase
    from src.shared.media.metadata import Rational

    svc = TimebaseService()
    fps = Rational(24, 1)
    # WhisperX 给的转录段：PTS 1/90000 timebase，1s-2s
    pt = ProviderTimebase(pts_start=90000, pts_end=180000, timebase=Rational(1, 90000))
    res = svc.to_source_frame_range(pt, fps)
    seg = SemanticSegment(
        segment_id="t1", asset_id="a1",
        source_range=res.source_range,
        kind=SegmentKind.TRANSCRIPT,
        payload={"text": "续航很强"},
        source_pts_start=pt.pts_start,
        source_pts_end=pt.pts_end,
        source_timebase=pt.timebase_label,
    )
    # 回溯：segment 的 source_range 精确回到 frame 24-48
    assert seg.source_range.start == 24
    assert seg.source_range.end == 48
    assert seg.source_range.contains(30)
    assert not seg.source_range.contains(48)
    # 原始 PTS 也保留，可二次校验
    assert seg.source_pts_start == 90000
    assert seg.source_timebase == "1/90000"


# ================================================================
# M4：recipe → provider 解析 + 多 provider 编排（§17.4）
# ================================================================


def test_resolve_providers_fast_local():
    """FAST_LOCAL 只需 scene_cut_local。"""
    from src.features.edit.ai_editing.analysis_request import AnalysisRecipe
    from src.features.edit.ai_editing.analysis_service import resolve_providers

    assert resolve_providers(AnalysisRecipe.FAST_LOCAL) == ("scene_cut_local",)


def test_resolve_providers_editorial_local_includes_transcript():
    """EDITORIAL_LOCAL = scene_cut + transcript（口播/访谈/粗剪）。"""
    from src.features.edit.ai_editing.analysis_request import AnalysisRecipe
    from src.features.edit.ai_editing.analysis_service import resolve_providers

    ids = resolve_providers(AnalysisRecipe.EDITORIAL_LOCAL)
    assert "scene_cut_local" in ids
    assert "transcript_local" in ids
    # scene_cut 先跑（轻量），transcript 后跑（重）
    assert ids.index("scene_cut_local") < ids.index("transcript_local")


def test_providers_for_request_filters_unregistered():
    """recipe 解析出未注册的 provider → 过滤掉（不静默报错，只跑已注册的）。"""
    from src.features.edit.ai_editing.analysis_request import AnalysisRecipe, AnalysisRequest

    svc = AnalysisService()  # 只注册了 scene_cut_local
    req = AnalysisRequest.create(
        project_id="p1", asset_ids=("a1",),
        recipe_id=AnalysisRecipe.EDITORIAL_LOCAL,
        requested_by="test",
    )
    providers = svc.providers_for_request(req)
    # transcript_local 未注册 → 过滤掉，只剩 scene_cut_local
    assert len(providers) == 1
    assert providers[0].provider_id == "scene_cut_local"


def test_provider_ids_for_recipe_returns_all_including_unregistered():
    """provider_ids_for_recipe 返回含未注册的（供 UI 诊断展示能力缺口）。"""
    from src.features.edit.ai_editing.analysis_request import AnalysisRecipe

    svc = AnalysisService()
    ids = svc.provider_ids_for_recipe(AnalysisRecipe.EDITORIAL_LOCAL)
    assert "transcript_local" in ids  # 即使未注册也在列表里


# ---- 多 provider 编排：scene_cut + transcript 合并 ---- #


def _make_transcript_provider():
    """造一个最小 transcript provider（确定性，不依赖 WhisperX）。

    实现 AnalysisProvider Protocol（duck typing）。
    """
    from src.features.edit.ai_editing.analysis_models import (
        AnalysisProvenance, AssetAnalysis, FieldProvenance,
    )

    class _FakeTranscriptProvider:
        provider_id = "transcript_local"
        supported_kinds = (SegmentKind.TRANSCRIPT,)

        def analyze(self, asset_id, fingerprint, *, fps, progress=None):
            analysis = AssetAnalysis(
                asset_id=asset_id, fingerprint=fingerprint, fps=fps,
                transcript=(
                    TranscriptSegment(start_frame=0, end_frame=24, text="hello"),
                ),
                provenance=AnalysisProvenance(
                    provider="transcript_local", model="fake-v1",
                ),
                field_provenance=FieldProvenance.single(
                    "transcript",
                    AnalysisProvenance(provider="transcript_local", model="fake-v1"),
                ),
            )
            return analysis, tuple(analysis_to_segments(analysis))

    return _FakeTranscriptProvider()


def test_request_analysis_multi_provider_merges_fields():
    """多 provider 顺序跑：scene_cuts + transcript 各填各的字段，经 _merge_analysis 合并。

    M4 核心编排验收：一个 asset 跑多个 provider，结果 AssetAnalysis 同时有
    scene_cuts 和 transcript，且 field_provenance 各自独立。
    """
    svc = AnalysisService()
    svc.register(_make_transcript_provider())

    job, err = svc.request_analysis(
        "asset-1", AssetFingerprint(), fps="24/1",
        provider_ids=("scene_cut_local", "transcript_local"),
    )
    assert err == ""
    assert job is not None
    assert job.status == JobStatus.SUCCEEDED
    merged = svc.get_analysis("asset-1")
    assert merged is not None
    # 两个 provider 的字段都在（scene_cuts 可能为空——SceneCutProvider 默认返回空，
    # 但 transcript 字段必须有）
    assert len(merged.transcript) == 1
    # job manifest 记录了跑了哪些 provider
    assert "providers" in job.result_manifest
    assert set(job.result_manifest["providers"]) == {"scene_cut_local", "transcript_local"}


def test_request_analysis_multi_provider_unknown_rejected():
    """provider_ids 含未知 id → 报错，不静默跑剩下的。"""
    svc = AnalysisService()
    job, err = svc.request_analysis(
        "asset-1", AssetFingerprint(), fps="24/1",
        provider_ids=("scene_cut_local", "ghost_provider"),
    )
    assert err == "unknown provider 'ghost_provider'"
    assert job is None


def test_execute_analysis_editorial_local_runs_multi_provider():
    """端到端：EDITORIAL_LOCAL recipe → 跑 scene_cut + transcript（已注册时）。

    验收：request 经 propose/confirm/execute 后，AssetAnalysis 同时含 transcript。
    """
    svc = AnalysisService()
    svc.register(_make_transcript_provider())

    from src.features.edit.ai_editing.analysis_request import AnalysisRecipe, AnalysisRequest

    req = AnalysisRequest.create(
        project_id="p1", asset_ids=("asset-1",),
        recipe_id=AnalysisRecipe.EDITORIAL_LOCAL,
        requested_by="test",
    )
    svc.propose_analysis(req)
    svc.confirm_analysis(req.request_id)
    job, err = svc.execute_analysis(req.request_id, fps="24/1")
    assert err == ""
    assert job is not None
    analysis = svc.get_analysis("asset-1")
    assert analysis is not None
    assert len(analysis.transcript) == 1


def test_execute_analysis_no_provider_for_recipe_reports_error():
    """recipe 解析后无可用 provider（recipe 默认 provider 全未注册）→ 报错，不静默跑空。"""
    from src.features.edit.ai_editing.analysis_request import AnalysisRecipe, AnalysisRequest

    # 空 service（构造后不注册任何 provider——通过覆盖 _providers 实现）
    svc = AnalysisService()
    svc._providers.clear()  # 移除默认 scene_cut_local

    req = AnalysisRequest.create(
        project_id="p1", asset_ids=("a1",),
        recipe_id=AnalysisRecipe.FAST_LOCAL,
        requested_by="test",
    )
    svc.propose_analysis(req)
    svc.confirm_analysis(req.request_id)
    job, err = svc.execute_analysis(req.request_id, fps="24/1")
    assert "required analysis provider" in err
    assert "scene_cut_local" in err
    assert job is None


def test_execute_analysis_rejects_partial_recipe_registration() -> None:
    """An approved recipe cannot silently run only its registered subset."""
    from src.features.edit.ai_editing.analysis_request import AnalysisRecipe, AnalysisRequest

    svc = AnalysisService()  # only scene_cut_local is registered
    req = AnalysisRequest.create(
        project_id="p1", asset_ids=("a1",),
        recipe_id=AnalysisRecipe.EDITORIAL_LOCAL,
        requested_by="test",
    )
    svc.propose_analysis(req)
    svc.confirm_analysis(req.request_id)
    job, err = svc.execute_analysis(req.request_id)
    assert job is None
    assert "transcript_local" in err
    assert svc.get_request(req.request_id).state.value == "failed"


# ---- 向后兼容：单 provider 路径不变 ---- #


def test_request_analysis_single_provider_backward_compat():
    """旧 provider_id（单值）入口仍工作（向后兼容 M2 调用方）。"""
    svc = AnalysisService()
    job, err = svc.request_analysis(
        "asset-1", AssetFingerprint(), fps="24/1",
        provider_id="scene_cut_local",
    )
    assert err == ""
    assert job is not None
    assert job.status == JobStatus.SUCCEEDED


def test_request_analysis_default_provider_when_none_specified():
    """不指定 provider → 用首个注册的（向后兼容）。"""
    svc = AnalysisService()
    job, err = svc.request_analysis(
        "asset-1", AssetFingerprint(), fps="24/1",
    )
    assert err == ""
    assert job is not None


# ================================================================
# M8: OCR 字段 + 视觉搜索（§18.1）
# ================================================================


def test_asset_analysis_has_ocr_field():
    """AssetAnalysis.ocr 字段存在且默认空（M8 数据合同补齐）。"""
    a = AssetAnalysis(asset_id="a1", fingerprint=AssetFingerprint())
    assert a.ocr == ()
    d = a.to_dict()
    assert "ocr" in d
    assert d["ocr"] == []


def test_analysis_to_segments_produces_ocr_segments():
    """OCR detection → SegmentKind.OCR segment，payload.text 使搜索可命中。"""
    analysis = AssetAnalysis(
        asset_id="a1", fingerprint=AssetFingerprint(),
        ocr=(
            OcrDetection(frame=10, text="新品上市", confidence=0.92, language="zh",
                         box=OcrBox(0.1, 0.05, 0.8, 0.08)),
            OcrDetection(frame=50, text="立即购买", confidence=0.85, language="zh"),
        ),
        provenance=AnalysisProvenance(provider="ocr_local", model="rapidocr-v1"),
    )
    segments = analysis_to_segments(analysis)
    ocr_segs = [s for s in segments if s.kind == SegmentKind.OCR]
    assert len(ocr_segs) == 2
    texts = {s.payload.get("text") for s in ocr_segs}
    assert "新品上市" in texts
    assert "立即购买" in texts
    # 点事件 [frame, frame+1)
    assert all(s.source_range.duration == 1 for s in ocr_segs)
    # box 进 payload（第一个有 box）
    boxed = next(s for s in ocr_segs if s.payload.get("text") == "新品上市")
    assert boxed.payload.get("box") == {"x": 0.1, "y": 0.05, "w": 0.8, "h": 0.08}


def test_service_merges_transcript_and_ocr_independently():
    """OCR provider 与 transcript provider 合并：两字段各自保留，provenance 独立。"""
    svc = AnalysisService()
    # provider 1: transcript
    a1 = AssetAnalysis(
        asset_id="a1", fingerprint=AssetFingerprint(),
        transcript=(TranscriptSegment(0, 50, "口播内容"),),
        provenance=AnalysisProvenance(provider="transcript_local"),
        field_provenance=FieldProvenance.single(
            "transcript", AnalysisProvenance(provider="transcript_local", model="whisperx")),
    )
    svc.ingest("a1", a1, tuple(analysis_to_segments(a1)))
    # provider 2: ocr（同 asset）
    a2 = AssetAnalysis(
        asset_id="a1", fingerprint=AssetFingerprint(),
        ocr=(OcrDetection(frame=20, text="屏幕文字", confidence=0.9),),
        provenance=AnalysisProvenance(provider="ocr_local"),
        field_provenance=FieldProvenance.single(
            "ocr", AnalysisProvenance(provider="ocr_local", model="rapidocr-v1")),
    )
    svc.ingest("a1", a2, tuple(analysis_to_segments(a2)))
    merged = svc.get_analysis("a1")
    assert len(merged.transcript) == 1
    assert len(merged.ocr) == 1
    # 字段级 provenance 各自独立
    assert merged.provenance_for("transcript").provider == "transcript_local"
    assert merged.provenance_for("ocr").provider == "ocr_local"


def test_visual_search_finds_ocr_text():
    """端到端视觉搜索：ingest 含 OCR 的 analysis → search_assets 命中"屏幕文字"。

    这是 §11.1 M8 核心验收："产品特写/屏幕文字"查询能定位 frame。
    """
    svc = AnalysisService()
    analysis = AssetAnalysis(
        asset_id="a1", fingerprint=AssetFingerprint(),
        ocr=(OcrDetection(frame=30, text="限时特惠", confidence=0.9, language="zh"),),
        provenance=AnalysisProvenance(provider="ocr_local"),
    )
    svc.ingest("a1", analysis, tuple(analysis_to_segments(analysis)))
    results = svc.search_assets(text="特惠")
    assert len(results) == 1
    assert results[0].kind == SegmentKind.OCR
    assert results[0].payload.get("text") == "限时特惠"
    # 可定位 frame
    assert results[0].source_range.start == 30


def test_visual_search_finds_tags():
    """视觉搜索也能命中 TAG segment（既有路径回归 + "产品特写" 可查）。"""
    svc = AnalysisService()
    analysis = AssetAnalysis(
        asset_id="a1", fingerprint=AssetFingerprint(),
        tags=({"start_frame": 0, "end_frame": 60, "labels": ["product", "closeup"]},),
        provenance=AnalysisProvenance(provider="visual_local"),
    )
    svc.ingest("a1", analysis, tuple(analysis_to_segments(analysis)))
    results = svc.search_assets(text="product")
    assert len(results) == 1
    assert results[0].kind == SegmentKind.TAG


def test_visual_semantic_local_recipe_includes_ocr():
    """M8 接通：visual_semantic_local recipe 现含 ocr_local。"""
    from src.features.edit.ai_editing.analysis_request import AnalysisRecipe
    from src.features.edit.ai_editing.analysis_service import resolve_providers

    ids = resolve_providers(AnalysisRecipe.VISUAL_SEMANTIC_LOCAL)
    assert "ocr_local" in ids
    assert "scene_cut_local" in ids
    assert "transcript_local" in ids


def test_rapidocr_model_pack_spec_exists():
    """M8: BUILTIN_SPECS 含 rapidocr_ocr pack。"""
    from src.features.edit.ai_editing.model_pack_manager import BUILTIN_SPECS

    ocr_packs = [s for s in BUILTIN_SPECS if s.capability == "ocr"]
    assert len(ocr_packs) >= 1
    pack = ocr_packs[0]
    assert pack.pack_id == "rapidocr_ocr"
    assert pack.requires_gpu is False  # CPU 可跑
    assert "zh" in pack.languages
    assert pack.license.commercial_use is True  # Apache-2.0
