"""Batch D 修复回归测试（wd12-g52）。

覆盖 REMEDIATION_PLAN 批 D 的 16 条 Major 条目：

- AUD-14811/14812/14813 — transaction_service（commit 授权文档契约 / simulate
  诊断码 / compute_diff 复合变更双记）
- AUD-14900/14901/14902 — transcript_provider（diarization 量纲 / 引擎双检锁 /
  Sortformer 运行期失败降级）
- AUD-14910 — transcript_router（强制去伴奏失败降级必标注）
- AUD-14913 — tts_provider（失败 cue 0 时长占位保持索引对位）
- AUD-14920 — variant_planner（locked 优先选段 + 硬约束真实计算）
- AUD-15000/15001 — visual_semantic_provider（装配异常分流 / 关键帧延迟解码）
- AUD-15007/15008 — whisperx_provider（word 级外部数据校验 / 引擎锁与串行化）
- AUD-15011 — workflow（ArtifactOverrideError → degradation 而非异常）
- AUD-15203 — audio_capabilities（unknown 三态 + 缓存升级重探测）
- AUD-15300 — audio_dsp（ReverbEffect 重建分支应用新 decay/damping）
"""

from __future__ import annotations

import importlib.machinery
import sys
import threading
import types

import numpy as np
import pytest

pytestmark = pytest.mark.edit_core


# ================================================================
# 公共 fixture：注入 fake 可选依赖模块（不触发真实模型加载）
# ================================================================


@pytest.fixture
def fake_nemo(monkeypatch):
    """注入 fake nemo 模块层级（Sortformer 加载可观测、可计数）。"""
    loads = {"count": 0}
    lock = threading.Lock()

    class _FakeSortformer:
        def restore_from(self, restore_path, strict=False):
            with lock:
                loads["count"] += 1
            return self

        def from_pretrained(self, model_id):
            with lock:
                loads["count"] += 1
            return self

        def eval(self):
            return self

    modules = {}
    names = ["nemo", "nemo.collections", "nemo.collections.asr", "nemo.collections.asr.models"]
    for i, name in enumerate(names):
        mod = types.ModuleType(name)
        mod.__spec__ = importlib.machinery.ModuleSpec(name, None)
        mod.__path__ = []  # 标记为包
        modules[name] = mod
        monkeypatch.setitem(sys.modules, name, mod)
    modules["nemo.collections.asr.models"].SortformerEncLabelModel = _FakeSortformer()
    return modules, loads


@pytest.fixture
def caps_cache_reset():
    from src.features.edit.core import audio_capabilities as caps_mod
    caps_mod.reset_audio_capabilities_cache()
    yield
    caps_mod.reset_audio_capabilities_cache()


# ================================================================
# AUD-14811 — commit 的 ApprovalService 直传路径：文档不再承诺，行为恒拒绝
# ================================================================


def _ts_state():
    from tests.edit.ai_editing.test_transaction_service import _state
    return _state()


def _ts_add_clip_op(op_id, track_id, asset_id, t_start, t_dur, s_start=0):
    from tests.edit.ai_editing.test_transaction_service import _add_clip_op
    return _add_clip_op(op_id, track_id, asset_id, t_start, t_dur, s_start)


class TestAud14811ApprovalServiceDirectRejected:
    def test_direct_service_is_rejected_not_silent_success(self):
        """按修正后的文档契约：直接传 ApprovalService 不是有效授权。"""
        from src.features.edit.ai_editing.approval_service import ApprovalService
        from src.features.edit.ai_editing.models import EditPlan, PlanTarget
        from src.features.edit.ai_editing.transaction_service import TransactionService

        state = _ts_state()
        plan = EditPlan.create(
            PlanTarget("p1", "t1"), 0,
            operations=(_ts_add_clip_op("op1", "v1", "a1", 0, 100),),
        )
        svc = TransactionService()
        result = svc.commit(plan, state, approval=ApprovalService())
        assert result.applied is False
        assert "ApprovalGrant" in result.error
        # 状态不变
        assert len(result.new_sequences["t1"].video_tracks[0].clips) == 0

    def test_docstring_no_longer_promises_service_auto_consume(self):
        """docstring 已删改：明确直传 service 不是有效授权（恒拒绝）。"""
        from src.features.edit.ai_editing.transaction_service import TransactionService

        doc = TransactionService.commit.__doc__ or ""
        assert "AUD-14811" in doc
        assert "不是**有效授权" in doc
        # 旧的"传入 ApprovalService 即自动 consume"承诺条目已删除：
        # 授权契约列表中不再有以 ApprovalService 作为 approval 实参的 bullet。
        assert "一个 ``ApprovalService``（此时内部会用" not in doc


# ================================================================
# AUD-14812 — simulate 失败使用 INTERNAL_ERROR，不再伪装 SCHEMA_INVALID
# ================================================================


class TestAud14812SimulateDiagnosticCode:
    def test_simulate_failure_maps_to_internal_error(self):
        from src.features.edit.ai_editing.models import EditPlan, PlanTarget
        from src.features.edit.ai_editing.transaction_service import TransactionService

        state = _ts_state()
        # 两个重叠 clip → MacroCommand.apply 内 overlap 报错（运行期失败）
        plan = EditPlan.create(
            PlanTarget("p1", "t1"), 0,
            operations=(
                _ts_add_clip_op("op1", "v1", "a1", 0, 100),
                _ts_add_clip_op("op2", "v1", "a1", 50, 100, s_start=50),
            ),
        )
        result = TransactionService().preview(plan, state)
        assert result.has_errors
        codes = [d.code.value for d in result.report.errors]
        assert "TAEP_INTERNAL_ERROR" in codes
        assert "TAEP_SCHEMA_INVALID" not in codes
        # detail 保留异常类型（不掩盖领域错误）
        assert any("simulation failed" in d.message for d in result.report.errors)


# ================================================================
# AUD-14813 — compute_diff：跨轨移动+时长变化双记，载荷含 track
# ================================================================


class TestAud14813DiffCompositeChange:
    def test_cross_track_move_with_trim_is_double_recorded(self):
        from src.features.edit.ai_editing.transaction_service import compute_diff
        from tests.edit.ai_editing.test_transaction_service import _clip
        from src.features.edit.core.clip_params import CropParams, SpeedParams, TransformParams
        from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence

        def _seq(clips_by_track):
            return TimelineSequence(
                timeline_id="t1",
                video_tracks=tuple(
                    EditTrack(track_id=tid, name=tid.upper(), track_type="video",
                              index=i, clips=tuple(clips))
                    for i, (tid, clips) in enumerate(clips_by_track)
                ),
            )

        base = _seq([("v1", [_clip("c1", "v1", 0, 100)])])
        moved_clip = ClipInstance(
            clip_id="c1", asset_id="a1", track_id="v2",
            timeline_start=50, timeline_duration=80,
            source_start=0, source_duration=80,
            speed=SpeedParams(), transform=TransformParams(), crop=CropParams(),
        )
        proposed = _seq([("v1", []), ("v2", [moved_clip])])

        d = compute_diff(base, proposed)
        # 移动 + 裁切都被记录（旧实现单标签只报 trimmed）
        assert any(c.clip_id == "c1" and c.change == "moved" for c in d.moved_clips)
        assert any(c.clip_id == "c1" and c.change == "trimmed" for c in d.trimmed_clips)
        # 载荷含 track：批准 UI 能看到轨道变更
        moved = next(c for c in d.moved_clips if c.clip_id == "c1")
        assert moved.before["track"] == "v1"
        assert moved.after["track"] == "v2"
        # 两条轨道都算受影响
        assert set(d.affected_track_ids) == {"v1", "v2"}
        assert d.total_changes == 2

    def test_payload_contains_track_field_for_plain_trim(self):
        from src.features.edit.ai_editing.transaction_service import compute_diff
        from tests.edit.ai_editing.test_transaction_service import _clip
        from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence

        base = TimelineSequence(
            timeline_id="t1",
            video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                                    clips=(_clip("c1", "v1", 0, 100),)),),
        )
        proposed = TimelineSequence(
            timeline_id="t1",
            video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                                    clips=(_clip("c1", "v1", 0, 60),)),),
        )
        d = compute_diff(base, proposed)
        assert len(d.trimmed_clips) == 1
        assert d.trimmed_clips[0].before["track"] == "v1"
        assert d.trimmed_clips[0].after["track"] == "v1"


# ================================================================
# AUD-14900 — merge_diarization 帧/sample 量纲统一
# ================================================================


class TestAud14900DiarizationCoordinateDomains:
    def test_sample_coords_converted_to_frame_with_ratio(self):
        from src.features.edit.ai_editing.analysis_models import TranscriptSegment
        from src.features.edit.ai_editing.transcript_provider import (
            ASR_SAMPLE_RATE, _RawDiarizationSegment, merge_diarization,
        )
        from src.features.edit.ai_editing.timebase import TimebaseService
        from src.shared.media.metadata import Rational

        tb = TimebaseService()
        fps = Rational(24, 1)
        # 转录段 10-20s（frame 坐标 240..480 @24fps）；
        # 说话人 0 = 0-10s，说话人 1 = 10-20s（16kHz sample 坐标）
        transcript = [TranscriptSegment(240, 480, "second sentence", confidence=0.9)]
        diar = [
            _RawDiarizationSegment(0, 160000, 0, sample_rate=ASR_SAMPLE_RATE),
            _RawDiarizationSegment(160000, 320000, 1, sample_rate=ASR_SAMPLE_RATE),
        ]

        with_ratio = merge_diarization(
            transcript, diar, media_fps_ratio=fps, timebase_service=tb,
        )
        assert with_ratio[0].speaker == "S2"  # 归属 10-20s 的第二位说话人

        # 回归对照：不传 ratio（缺陷形态）→ sample 坐标与 frame 坐标直接比较，
        # 转录段全部落进第一个 diar 段的巨大 sample 区间 → 说话人被错标为 S1
        without_ratio = merge_diarization(transcript, diar)
        assert without_ratio[0].speaker == "S1"

    def test_analyze_passes_fps_ratio_and_timebase(self, monkeypatch):
        """analyze() 的 merge_diarization 调用必须传 media_fps_ratio/timebase_service。"""
        from src.features.edit.ai_editing import transcript_provider as tp

        captured: dict = {}

        def _fake_merge(transcript, diarization, **kwargs):
            captured.update(kwargs)
            return transcript

        monkeypatch.setattr(tp, "merge_diarization", _fake_merge)
        provider = tp.QwenASRProvider()
        monkeypatch.setattr(
            provider, "_run_diarization",
            lambda asset_id, progress=None: [tp._RawDiarizationSegment(0, 1600, 0)],
        )
        monkeypatch.setattr(
            provider, "_speakers_requested", lambda: True,
        )
        monkeypatch.setenv("TOPOS_ASR_REAL", "0")  # stub ASR，无需模型
        # 注入 fake qwen_asr（find_spec 依赖 __spec__ 存在）
        fake = types.ModuleType("qwen_asr")
        fake.__spec__ = importlib.machinery.ModuleSpec("qwen_asr", None)
        monkeypatch.setitem(sys.modules, "qwen_asr", fake)
        from src.features.edit.ai_editing.analysis_models import AssetFingerprint
        provider.analyze("a1", AssetFingerprint(), fps="24/1")
        assert captured.get("media_fps_ratio") is not None
        assert captured.get("timebase_service") is provider._timebase


# ================================================================
# AUD-14901 — 惰性引擎缓存双检锁
# ================================================================


class TestAud14901EngineLazyInitLock:
    def test_engine_lock_exists_and_force_cpu_is_atomic(self):
        from src.features.edit.ai_editing.transcript_provider import QwenASRProvider

        provider = QwenASRProvider()
        assert isinstance(provider._engine_lock, type(threading.Lock()))
        provider._asr_engine = object()
        provider._asr_engine_device = "mps"
        provider._aligner = object()
        provider._force_cpu_asr_engine()
        assert provider._asr_engine is None
        assert provider._asr_engine_device is None
        assert provider._aligner is None
        assert provider._asr_force_cpu is True

    def test_concurrent_diar_model_load_is_singleton(self, fake_nemo):
        """并发首次调用只加载一次 Sortformer（双检锁）。"""
        from src.features.edit.ai_editing.transcript_provider import QwenASRProvider

        _modules, loads = fake_nemo
        provider = QwenASRProvider()
        results: list = []
        barrier = threading.Barrier(4)

        def _worker():
            barrier.wait()
            results.append(provider._get_diar_model())

        threads = [threading.Thread(target=_worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert len(results) == 4
        assert all(r is results[0] for r in results)
        assert loads["count"] == 1

    def test_patch_registration_is_idempotent_under_lock(self, fake_nemo, monkeypatch):
        """模块级 monkeypatch 幂等标志的 check-then-set 原子（并发注册只 patch 一次）。"""
        pytest.importorskip("qwen_asr")
        from src.features.edit.ai_editing import transcript_provider as tp

        calls = {"processor": 0, "rope": 0}
        real_proc = tp._patch_qwen3_processor_compat
        real_rope = tp._patch_qwen3_rope_scaling_compat

        def _counting_proc():
            calls["processor"] += 1
            return real_proc()

        def _counting_rope():
            calls["rope"] += 1
            return real_rope()

        monkeypatch.setattr(tp, "_patch_qwen3_processor_compat", _counting_proc)
        monkeypatch.setattr(tp, "_patch_qwen3_rope_scaling_compat", _counting_rope)
        barrier = threading.Barrier(4)

        def _worker():
            barrier.wait()
            tp._patch_qwen3_processor_compat()
            tp._patch_qwen3_rope_scaling_compat()

        threads = [threading.Thread(target=_worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        # 幂等：标志已置（无论注册发生几次，重复调用立即返回）
        assert tp._processor_compat_patched is True
        assert tp._rope_scaling_patched is True


# ================================================================
# AUD-14902 — Sortformer 运行期失败降级
# ================================================================


class _FakePackStatus:
    def __init__(self, usable: bool) -> None:
        self.usable = usable


class _FakePackManager:
    def __init__(self, usable: dict[str, bool]) -> None:
        self._usable = usable

    def get_status(self, pack_id):
        return _FakePackStatus(self._usable.get(pack_id, False))

    def require_usable(self, pack_id):
        if not self._usable.get(pack_id, False):
            from src.features.edit.ai_editing.model_pack_manager import (
                ModelPackNotInstalledError,
            )
            raise ModelPackNotInstalledError(pack_id, detail="not installed")
        return "/packs/" + pack_id


class TestAud14902DiarizationRuntimeFailureDegrades:
    def test_runtime_error_returns_empty_not_raise(self, fake_nemo, monkeypatch):
        from src.features.edit.ai_editing import transcript_provider as tp

        provider = tp.QwenASRProvider(model_pack_manager=_FakePackManager(
            {"nemo_sortformer": True},
        ))
        monkeypatch.setenv("TOPOS_ASR_REAL", "1")

        def _boom(asset_id):
            raise RuntimeError("CUDA out of memory during diarize()")

        monkeypatch.setattr(provider, "_run_sortformer_real", _boom)
        # 运行期错误 → 降级为 []（不击溃已完成的转录）
        assert provider._run_diarization("a1") == []

    def test_import_failure_still_degrades(self, monkeypatch):
        from src.features.edit.ai_editing import transcript_provider as tp

        provider = tp.QwenASRProvider(model_pack_manager=_FakePackManager(
            {"nemo_sortformer": True},
        ))
        monkeypatch.setenv("TOPOS_ASR_REAL", "1")

        def _no_nemo(asset_id):
            raise ModuleNotFoundError("No module named 'nemo'")

        monkeypatch.setattr(provider, "_run_sortformer_real", _no_nemo)
        assert provider._run_diarization("a1") == []


# ================================================================
# AUD-14910 — 强制去伴奏失败降级必标注 provenance
# ================================================================


def _router_analysis(text: str = "hello world", confidence: float = 0.95):
    from src.features.edit.ai_editing.analysis_models import (
        AnalysisProvenance, AssetAnalysis, AssetFingerprint, TranscriptSegment,
    )
    analysis = AssetAnalysis(
        asset_id="a1", fingerprint=AssetFingerprint(),
        transcript=(TranscriptSegment(0, 100, text, confidence=confidence),),
        provenance=AnalysisProvenance(provider="test", model="m1"),
    )
    return analysis


class _FakeQwenRouterBackend:
    def __init__(self) -> None:
        self.preprocessor_calls = 0

    def analyze(self, asset_id, fingerprint, *, fps, progress=None, options=None):
        return _router_analysis(), ()

    def analyze_with_preprocessor(
        self, asset_id, fingerprint, *, fps, preprocessor, progress=None, options=None,
    ):
        self.preprocessor_calls += 1
        raise RuntimeError("demucs crashed")


class _FailingDemucs:
    def is_available(self) -> bool:
        return True


class TestAud14910ForcedDemucsDegradationAnnotated:
    def test_degradation_recorded_in_provenance(self, monkeypatch):
        from src.features.edit.ai_editing.analysis_models import AssetFingerprint
        from src.features.edit.ai_editing.transcript_router import AdaptiveTranscriptProvider

        monkeypatch.delenv("TOPOS_TRANSCRIPT_MODE", raising=False)
        qwen = _FakeQwenRouterBackend()
        router = AdaptiveTranscriptProvider(qwen, demucs_preprocessor=_FailingDemucs())
        analysis, _segments = router.analyze(
            "a1", AssetFingerprint(), fps="24/1",
            options={"background_music": "remove"},
        )
        # 软降级保住 transcript，但 provenance 必须标注降级
        assert qwen.preprocessor_calls == 1
        extra = analysis.provenance.extra
        assert extra["router_selection"] == "qwen3_asr_original"
        assert "demucs_requested_but_failed" in extra
        assert "RuntimeError" in extra["demucs_requested_but_failed"]

    def test_no_annotation_when_demucs_not_requested(self, monkeypatch):
        from src.features.edit.ai_editing.analysis_models import AssetFingerprint
        from src.features.edit.ai_editing.transcript_router import AdaptiveTranscriptProvider

        monkeypatch.delenv("TOPOS_TRANSCRIPT_MODE", raising=False)
        qwen = _FakeQwenRouterBackend()
        router = AdaptiveTranscriptProvider(qwen, demucs_preprocessor=_FailingDemucs())
        analysis, _segments = router.analyze(
            "a1", AssetFingerprint(), fps="24/1", options={},
        )
        # 未请求去伴奏 → demucs 路径不触发，无降级标注
        assert qwen.preprocessor_calls == 0
        assert "demucs_requested_but_failed" not in (analysis.provenance.extra or {})


# ================================================================
# AUD-14913 — 失败 cue 0 时长占位，索引对位保持
# ================================================================


class _FlakyTTSEngine:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def generate_custom_voice(self, **kwargs):
        self.calls.append(kwargs["text"])
        if kwargs["text"] == "second":
            return [], 24000  # 引擎对个别 cue 返回空
        return [np.zeros(2400, dtype=np.float32)], 24000


class TestAud14913TtsCueIndexAlignment:
    def test_failed_cue_kept_as_zero_duration_placeholder(self, monkeypatch):
        from src.features.edit.ai_editing.tts_provider import Qwen3TTSProvider, SpeechCue
        from src.features.edit.ai_editing.tts_settings import TtsSettings

        provider = Qwen3TTSProvider()
        monkeypatch.setitem(sys.modules, "soundfile", types.SimpleNamespace(
            write=lambda *_args, **_kwargs: None,
        ))
        engine = _FlakyTTSEngine()
        monkeypatch.setattr(provider, "_get_engine", lambda kind: engine)
        cues = (
            SpeechCue("first", 0), SpeechCue("second", 100), SpeechCue("third", 200),
        )

        waves = provider._synthesize_custom_voice(cues, TtsSettings(mode="custom_voice"))
        # 失败 cue 不再被 continue 跳过：3 条输入 → 3 条输出，索引对位
        assert len(waves) == 3
        assert len(waves[0][1]) == 2400
        assert len(waves[1][1]) == 0  # 0 时长占位
        assert waves[1][2] == provider._OUTPUT_SAMPLE_RATE

        result = provider._lay_down_waves(
            waves, cues, 24.0, TtsSettings(mode="custom_voice"), None,
        )
        assert len(result.cue_durations) == len(cues)
        assert result.cue_durations[0] == pytest.approx(0.1)
        assert result.cue_durations[1] == 0.0  # 第 2 条对应失败 cue，不再错位
        assert result.cue_durations[2] == pytest.approx(0.1)

    def test_all_cues_failed_still_raises(self, monkeypatch):
        from src.features.edit.ai_editing.tts_provider import Qwen3TTSProvider, SpeechCue
        from src.features.edit.ai_editing.tts_settings import TtsSettings

        provider = Qwen3TTSProvider()

        class _EmptyEngine:
            def generate_custom_voice(self, **kwargs):
                return [], 24000

        monkeypatch.setattr(provider, "_get_engine", lambda kind: _EmptyEngine())
        cues = (SpeechCue("first", 0), SpeechCue("second", 100))
        waves = provider._synthesize_custom_voice(cues, TtsSettings(mode="custom_voice"))
        with pytest.raises(RuntimeError, match="未产生任何音频"):
            provider._lay_down_waves(
                waves, cues, 24.0, TtsSettings(mode="custom_voice"), None,
            )


# ================================================================
# AUD-14920 — locked 选段优先 + 硬约束真实计算
# ================================================================


def _planner_index():
    from src.features.edit.ai_editing.editorial_models import (
        FrameRange, SegmentKind, SemanticSegment,
    )
    from src.features.edit.ai_editing.semantic_index import SemanticIndex

    index = SemanticIndex()
    index.add(SemanticSegment(
        segment_id="seg-hi", asset_id="a1", source_range=FrameRange(0, 100),
        kind=SegmentKind.TRANSCRIPT, payload={"text": "high confidence take"},
        confidence=0.95,
    ))
    index.add(SemanticSegment(
        segment_id="seg-locked", asset_id="a1", source_range=FrameRange(300, 100),
        kind=SegmentKind.TRANSCRIPT, payload={"text": "user locked take"},
        confidence=0.4,
    ))
    return index


def _planner_input():
    from src.features.edit.ai_editing.variant_planner import PlannerInput
    return PlannerInput(
        brief_id="b1", brief_revision=1, asset_id="a1", timeline_id="t1",
        project_id="p1", base_revision=0, target_duration=720,
    )


class TestAud14920LockedRangeConstraint:
    def test_locked_candidate_preferred_over_higher_confidence(self):
        from src.features.edit.ai_editing.brief_service import LockedSourceRange
        from src.features.edit.ai_editing.editorial_models import FrameRange
        from src.features.edit.ai_editing.variant_planner import (
            STRATEGY_A_TALKING_HEAD, VariantPlanner,
        )

        planner = VariantPlanner()
        locked = (LockedSourceRange("a1", FrameRange(300, 100), label="user pick"),)
        beat_range = FrameRange(0, 720)
        chosen = planner._select_segment_for_beat(
            "a1", beat_range, _planner_index(), STRATEGY_A_TALKING_HEAD, locked, (),
        )
        assert chosen is not None
        assert chosen.segment_id == "seg-locked"

    def test_hard_constraint_pass_when_locked_covered(self):
        from src.features.edit.ai_editing.brief_service import BriefService, LockedSourceRange
        from src.features.edit.ai_editing.editorial_models import (
            DurationTarget, EditorialBrief, FrameRange, Platform,
        )
        from src.features.edit.ai_editing.variant_planner import (
            PlannerConstraints, VariantPlanner,
        )

        brief = BriefService()
        brief.save(EditorialBrief(
            brief_id="b1", revision=1, platform=Platform.VERTICAL_SOCIAL,
            target_duration=DurationTarget(720, 648, 792),
            required_beats=("hook",),
        ))
        planner = VariantPlanner(brief)
        locked = (LockedSourceRange("a1", FrameRange(300, 100)),)
        variants = planner.plan(
            _planner_input(), _planner_index(),
            constraints=PlannerConstraints(locked_ranges=locked),
            target_beats=("hook", "product_value", "call_to_action"),
        )
        assert variants
        assert variants[0].scores.hard_constraints_passed is True
        # 锁定区间被真实选为 evidence
        assert any(
            d.subject is not None and d.subject.source.overlaps(FrameRange(300, 100))
            for v in variants for d in v.decisions
        )

    def test_hard_constraint_fails_when_locked_range_dropped(self):
        from src.features.edit.ai_editing.brief_service import BriefService, LockedSourceRange
        from src.features.edit.ai_editing.editorial_models import (
            DurationTarget, EditorialBrief, FrameRange, Platform,
        )
        from src.features.edit.ai_editing.variant_planner import (
            PlannerConstraints, VariantPlanner,
        )

        brief = BriefService()
        brief.save(EditorialBrief(
            brief_id="b1", revision=1, platform=Platform.VERTICAL_SOCIAL,
            target_duration=DurationTarget(720, 648, 792),
            required_beats=("hook",),
        ))
        planner = VariantPlanner(brief)
        # 锁定区间不与任何 beat 选段相交 → 硬约束必须 fail（不再硬编码 True）
        locked = (LockedSourceRange("a1", FrameRange(5000, 100)),)
        variants = planner.plan(
            _planner_input(), _planner_index(),
            constraints=PlannerConstraints(locked_ranges=locked),
            target_beats=("hook",),
        )
        assert variants
        assert variants[0].scores.hard_constraints_passed is False
        assert "硬约束未通过" in variants[0].scores.explanation


# ================================================================
# AUD-15000 — 装配异常分流：真实错误不再伪装"缺模型包"
# ================================================================


class TestAud15000VaultAssemblyErrorClassification:
    def test_construction_error_not_classified_as_missing_pack(self, monkeypatch, tmp_path):
        from src.features.edit.ai_editing import visual_semantic_provider as vsp

        manager = _FakePackManager({"qwen3_vl_8b_awq": True, "siglip2_base": False})

        class _BoomCaptioner:
            def __init__(self) -> None:
                raise RuntimeError("libcudart version mismatch")

        import apps.topos_vault.providers.real_visual_caption as cap_mod
        monkeypatch.setattr(cap_mod, "Qwen3VLCaptioner", _BoomCaptioner)
        provider = vsp.build_vault_visual_provider(
            model_pack_manager=manager,
            sampler_provider=lambda aid: None,
            total_frames_provider=lambda aid: 0,
            scene_cuts_provider=lambda aid: (),
            cache_root=str(tmp_path),
        )
        reason = provider._unavailable_reason
        # 真实错误保留异常摘要，不再统一伪装成"verified model pack"
        assert "init failed" in reason
        assert "RuntimeError" in reason
        assert "libcudart" in reason
        assert "qwen3_vl_8b_awq verified model pack" not in reason

    def test_missing_pack_still_classified_as_missing(self, monkeypatch, tmp_path):
        from src.features.edit.ai_editing import visual_semantic_provider as vsp

        manager = _FakePackManager({"qwen3_vl_8b_awq": False, "siglip2_base": False})
        provider = vsp.build_vault_visual_provider(
            model_pack_manager=manager,
            sampler_provider=lambda aid: None,
            total_frames_provider=lambda aid: 0,
            scene_cuts_provider=lambda aid: (),
            cache_root=str(tmp_path),
        )
        reason = provider._unavailable_reason
        assert "qwen3_vl_8b_awq verified model pack" in reason


# ================================================================
# AUD-15001 — 关键帧图像延迟解码（不再全量物化）
# ================================================================


class _CountingSampler:
    def __init__(self) -> None:
        self.sample_calls: list[int] = []

    def sample(self, frame_index):
        self.sample_calls.append(int(frame_index))
        return np.zeros((4, 4, 3), dtype=np.uint8)

    def close(self) -> None:
        pass


class _RecordingVisualBackend:
    is_demo = False
    provider_id = "fake-visual"
    model_version = "fake-visual-v1"

    def __init__(self) -> None:
        self.saw_decoded_image: list[bool] = []

    def infer(self, keyframe):
        from src.features.edit.ai_editing.visual_semantics import VisualObservation
        self.saw_decoded_image.append(keyframe.image is not None)
        return (VisualObservation(
            asset_id=keyframe.asset_id, shot_index=keyframe.shot_index,
            frame=keyframe.frame, kind="caption", value="a shot",
            confidence=0.7, field="caption", provenance={},
        ),)


class TestAud15001LazyKeyframeDecode:
    def test_no_decode_sampler_returns_none(self):
        from src.features.edit.ai_editing.visual_semantic_provider import _NoDecodeSampler
        assert _NoDecodeSampler().sample(123) is None

    def test_frames_decoded_once_per_keyframe_during_inference(self):
        from src.features.edit.ai_editing.analysis_models import AssetFingerprint
        from src.features.edit.ai_editing.visual_semantic_provider import (
            VisualSemanticAnalysisProvider,
        )

        sampler = _CountingSampler()
        backend = _RecordingVisualBackend()
        provider = VisualSemanticAnalysisProvider(
            sampler_provider=lambda aid: sampler,
            total_frames_provider=lambda aid: 1000,
            scene_cuts_provider=lambda aid: ({"frame": 0, "confidence": 1.0,
                                              "kind": "hard_cut"}, {"frame": 500,
                                              "confidence": 1.0, "kind": "hard_cut"}),
            visual_providers=(backend,),
        )
        analysis, segments = provider.analyze(
            "a1", AssetFingerprint(), fps="24/1",
        )
        # 2 个镜头 × head/mid/tail → 每个关键帧恰好解码一次（延迟解码）
        expected = 6
        assert len(sampler.sample_calls) == expected
        assert len(backend.saw_decoded_image) == expected
        assert all(backend.saw_decoded_image)
        assert analysis.provenance.extra["providers"] == ["fake-visual"]
        assert segments


# ================================================================
# AUD-15007 — word 级时间戳外部数据校验
# ================================================================


class TestAud15007WordTimestampValidation:
    def test_malformed_words_do_not_fail_whole_transcript(self):
        from src.features.edit.ai_editing.whisperx_provider import WhisperXProvider

        result = {
            "segments": [{
                "text": "hello world",
                "start": 0.0,
                "end": 2.0,
                "words": [
                    {"word": "hello", "start": 0.0, "end": 0.5},
                    {"word": "reversed", "start": 0.9, "end": 0.4},   # end < start
                    {"word": "negative", "start": -0.1, "end": 0.2},  # 负值
                    {"word": "nan", "start": float("nan"), "end": 1.0},
                    {"word": "", "start": 1.0, "end": 1.5},           # 空文本
                ],
            }],
            "language": "en",
        }
        segments = WhisperXProvider._convert_segments(result, "24/1")
        assert len(segments) == 1
        words = segments[0].words
        # 非有限与空文本被跳过；倒置/负值被钳制后保留
        texts = [w.text for w in words]
        assert "hello" in texts and "reversed" in texts and "negative" in texts
        assert "nan" not in texts and "" not in texts
        for w in words:
            assert w.end_frame > w.start_frame  # 换算成功（未抛 ValueError）

    def test_reversed_word_clamped_not_inverted(self):
        from src.features.edit.ai_editing.whisperx_provider import WhisperXProvider

        result = {
            "segments": [{
                "text": "a b",
                "start": 0.0, "end": 2.0,
                "words": [
                    {"word": "a", "start": 1.2, "end": 1.0},
                    {"word": "b", "start": 1.4, "end": 1.8},
                ],
            }],
        }
        segments = WhisperXProvider._convert_segments(result, "24/1")
        a, b = segments[0].words
        assert a.end_frame > a.start_frame
        assert b.start_frame >= a.start_frame


# ================================================================
# AUD-15008 — WhisperX 引擎双检锁 + 推理串行化
# ================================================================


class TestAud15008WhisperxEngineLock:
    def test_engine_lazy_load_singleton_under_concurrency(self):
        from src.features.edit.ai_editing.whisperx_provider import WhisperXProvider

        loads = {"count": 0}
        lock = threading.Lock()

        class _FakeWhisperx:
            def load_model(self, model_name, device, **kwargs):
                with lock:
                    loads["count"] += 1
                return object()

        provider = WhisperXProvider(device="cpu")
        fake = _FakeWhisperx()
        barrier = threading.Barrier(4)
        results: list = []

        def _worker():
            barrier.wait()
            results.append(provider._get_engine(fake))

        threads = [threading.Thread(target=_worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert all(r is results[0] for r in results)
        assert loads["count"] == 1

    def test_transcribe_serialized_by_engine_lock(self):
        """analyze 的引擎加载+transcribe 段受同一把锁保护（RLock 重入安全）。"""
        from src.features.edit.ai_editing.whisperx_provider import WhisperXProvider

        provider = WhisperXProvider(device="cpu")
        assert isinstance(provider._engine_lock, type(threading.RLock()))
        # 锁内再调 _get_engine（重入）不死锁——用 with + 短超时验证
        acquired = provider._engine_lock.acquire(timeout=0.1)
        assert acquired
        try:
            provider._engine_lock.acquire(timeout=0.1)  # RLock 可重入
            provider._engine_lock.release()
        finally:
            provider._engine_lock.release()


# ================================================================
# AUD-15011 — 字幕/多语言 artifact 保存冲突 → degradation
# ================================================================


def _wf_runner(analysis, store, translation_provider=None):
    from src.features.edit.ai_editing.analysis_service import AnalysisService
    from src.features.edit.ai_editing.analysis_models import analysis_to_segments
    from src.features.edit.ai_editing.brief_service import BriefService
    from src.features.edit.ai_editing.editorial_models import (
        DurationTarget, EditorialBrief, Platform,
    )
    from src.features.edit.ai_editing.variant_planner import VariantPlanner
    from src.features.edit.ai_editing.workflow import WorkflowRunner

    svc = AnalysisService()
    svc.ingest("a1", analysis, tuple(analysis_to_segments(analysis)))
    brief = BriefService()
    brief.save(EditorialBrief(
        brief_id="b1", revision=1, platform=Platform.VERTICAL_SOCIAL,
        target_duration=DurationTarget(720, 648, 792),
        required_beats=("hook",),
    ))
    return WorkflowRunner(
        planner=VariantPlanner(brief), analysis_service=svc, brief_service=brief,
        artifact_store=store, translation_provider=translation_provider,
    )


def _wf_transcript_analysis():
    from src.features.edit.ai_editing.analysis_models import (
        AnalysisProvenance, AssetAnalysis, AssetFingerprint, TranscriptSegment,
    )
    return AssetAnalysis(
        asset_id="a1", fingerprint=AssetFingerprint(),
        transcript=(
            TranscriptSegment(0, 108, "开场", speaker="S1", confidence=0.95),
            TranscriptSegment(108, 432, "主体", speaker="S1", confidence=0.9),
        ),
        provenance=AnalysisProvenance(provider="test", model="m1"),
    )


class TestAud15011ArtifactOverrideDegrades:
    def test_subtitle_rerun_after_user_edit_degrades(self):
        from src.features.edit.ai_editing.artifact_store import ArtifactStore
        from src.features.edit.ai_editing.workflow import WorkflowId, WorkflowInput

        store = ArtifactStore()
        runner = _wf_runner(_wf_transcript_analysis(), store)
        inp = WorkflowInput(
            workflow_id=WorkflowId.SUBTITLE_DRAFT, asset_ids=("a1",),
            timeline_id="t1", project_id="p1", base_revision=0, brief_id="b1",
        )
        first = runner.run(inp)
        assert first.artifact_id is not None
        artifact = store.require(first.artifact_id)
        saved = store.save_user_edit(artifact.with_user_edit())
        assert saved.user_edited is True

        rerun = runner.run(inp)  # 旧实现：ArtifactOverrideError 穿透
        assert rerun.degraded
        assert rerun.artifact_id is None
        assert "编辑" in rerun.degradation
        # 用户版本未被覆盖
        assert store.require(saved.artifact_id).user_edited is True

    def test_multilingual_rerun_after_user_edit_degrades(self):
        from src.features.edit.ai_editing.artifact_models import ScriptLine
        from src.features.edit.ai_editing.artifact_store import ArtifactStore
        from src.features.edit.ai_editing.workflow import WorkflowId, WorkflowInput

        def _translate(segments, language):
            return tuple(
                ScriptLine(
                    line_id=f"l{i}",
                    text=f"[{language}] {s.payload.get('text', '')}",
                )
                for i, s in enumerate(segments)
            )

        store = ArtifactStore()
        runner = _wf_runner(_wf_transcript_analysis(), store, translation_provider=_translate)
        inp = WorkflowInput(
            workflow_id=WorkflowId.MULTILINGUAL_VERSION, asset_ids=("a1",),
            timeline_id="t1", project_id="p1", base_revision=0, brief_id="b1",
            target_language="en",
        )
        first = runner.run(inp)
        assert first.artifact_id is not None
        artifact = store.require(first.artifact_id)
        saved = store.save_user_edit(artifact.with_user_edit())

        rerun = runner.run(inp)
        assert rerun.degraded
        assert rerun.artifact_id is None
        assert "编辑" in rerun.degradation
        assert store.require(saved.artifact_id).user_edited is True


# ================================================================
# AUD-15203 — 输出设备 unknown 三态 + 缓存升级重探测
# ================================================================


class TestAud15203OutputDeviceUnknownCache:
    def test_skipped_probe_is_unknown_not_available(self):
        from src.features.edit.core.audio_capabilities import probe_audio_capabilities

        caps = probe_audio_capabilities(check_output_device=False)
        assert caps.output_device.available is False
        assert caps.output_device.unknown is True
        # unknown ≠ 不可用：不计入 disabled_components
        assert "Audio Output Device" not in caps.disabled_components()

    def test_true_request_upgrades_unknown_cache(self, monkeypatch, caps_cache_reset):
        from src.features.edit.core import audio_capabilities as caps_mod

        monkeypatch.setattr(
            caps_mod, "_probe_output_device",
            lambda: caps_mod.CapabilityResult(True),
        )
        # DSP 路径先以 False 填充缓存（unknown 占位）
        c1 = caps_mod.get_audio_capabilities(check_output_device=False)
        assert c1.output_device.unknown is True
        # 健康报告路径以 True 请求 → 缓存被升级为真实探测结果
        c2 = caps_mod.get_audio_capabilities(check_output_device=True)
        assert c2.output_device.unknown is False
        assert c2.output_device.available is True
        # 升级后的缓存被后续调用复用（不再 unknown）
        c3 = caps_mod.get_audio_capabilities(check_output_device=True)
        assert c3.output_device.available is True
        assert c3 is c2

    def test_upgraded_cache_reflects_missing_device(self, monkeypatch, caps_cache_reset):
        from src.features.edit.core import audio_capabilities as caps_mod

        monkeypatch.setattr(
            caps_mod, "_probe_output_device",
            lambda: caps_mod.CapabilityResult(False, "simulated: no device"),
        )
        caps_mod.get_audio_capabilities(check_output_device=False)
        upgraded = caps_mod.get_audio_capabilities(check_output_device=True)
        assert upgraded.output_device.unknown is False
        assert upgraded.output_device.available is False
        assert any("Audio Output Device" in d for d in upgraded.disabled_components())


# ================================================================
# AUD-15300 — ReverbEffect 重建分支应用新 decay/damping
# ================================================================


class TestAud15300ReverbRebuildAppliesNewParams:
    def test_predelay_plus_decay_change_applies_new_comb_params(self):
        from src.features.edit.core.audio_dsp import (
            ReverbEffect, _COMB_DELAY_MS, _damping_hz_to_linear, _decay_to_feedback,
        )

        fx = ReverbEffect(sample_rate=48000.0)
        fx.set_params({
            "pre_delay_ms": 50.0,   # 触发 _rebuild_filters 分支
            "decay": 5.0,           # 新 decay（默认 2.0）
            "damping_hz": 12000.0,  # 新 damping（默认 5000.0）
            "mix": 30.0, "enabled": True,
        })
        expected_fb = _decay_to_feedback(5.0, _COMB_DELAY_MS[0], 48000.0)
        expected_damp = _damping_hz_to_linear(12000.0, 48000.0)
        assert fx._combs[0]._feedback == pytest.approx(expected_fb)
        assert fx._combs[0]._damp1 == pytest.approx(expected_damp)

    def test_sample_rate_change_branch_applies_new_params(self):
        from src.features.edit.core.audio_dsp import (
            ReverbEffect, _COMB_DELAY_MS, _damping_hz_to_linear, _decay_to_feedback,
        )

        fx = ReverbEffect(sample_rate=48000.0)
        fx.set_params(
            {"decay": 8.0, "damping_hz": 15000.0, "mix": 40.0, "enabled": True},
            sample_rate=96000.0,  # 触发 sr_changed 重建分支
        )
        expected_fb = _decay_to_feedback(8.0, _COMB_DELAY_MS[0], 96000.0)
        expected_damp = _damping_hz_to_linear(15000.0, 96000.0)
        assert fx._combs[0]._feedback == pytest.approx(expected_fb)
        assert fx._combs[0]._damp1 == pytest.approx(expected_damp)

    def test_repeat_same_params_keeps_values(self):
        from src.features.edit.core.audio_dsp import (
            ReverbEffect, _COMB_DELAY_MS, _decay_to_feedback,
        )

        fx = ReverbEffect(sample_rate=48000.0)
        params = {"pre_delay_ms": 50.0, "decay": 5.0, "damping_hz": 12000.0,
                  "mix": 30.0, "enabled": True}
        fx.set_params(params)
        fb_after_first = fx._combs[0]._feedback
        # 同值重复 set_params（工程加载后 UI 重设同值）：值保持，不回退默认
        fx.set_params(params)
        assert fx._combs[0]._feedback == pytest.approx(fb_after_first)
        assert fx._combs[0]._feedback == pytest.approx(
            _decay_to_feedback(5.0, _COMB_DELAY_MS[0], 48000.0)
        )
