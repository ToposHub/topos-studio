"""TAEP M4 — QwenASRProvider 测试（R4-c：Qwen3-ASR + ForcedAligner + Sortformer）。

验收（§11.1 M4 / §16.3）：
- provider_id / supported_kinds 契约。
- qwen_asr 包未装 → 抛 ModelPackNotInstalledError（不静默降级）。
- 模型 pack 未装 / 未校验 → 抛错（§11.1 M4：能力缺失明示）。
- 确定性 stub 路径：固定输入 → 固定 TranscriptSegment + WordSegment。
- 词级时间码：words 非空且 frame 坐标正确（经 TimebaseService）。
- 无对齐模型降级：words 空 + confidence 下调 + extra.alignment="none"。
- diarization 匿名化：speaker 仅 S1/S2（绝不实名）；重叠段不丢失。
- PTS(sample) → frame 经整数换算（绝不用秒 × fps）。
- 不写 timeline（只产 AssetAnalysis.transcript）。

R4-c：类从 WhisperXTranscriptProvider 重命名为 QwenASRProvider；mock 从 whisperx
改为 qwen_asr；pack_id 从 whisperx_*/pyannote_* 改为 qwen3_*/nemo_sortformer。
"""

from __future__ import annotations

import importlib.util

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.analysis_models import (
    AnalysisProvenance,
    AssetAnalysis,
    AssetFingerprint,
    FieldProvenance,
    TranscriptSegment,
    WordSegment,
)
from src.features.edit.ai_editing.editorial_models import SegmentKind
from src.features.edit.ai_editing.model_pack_manager import ModelPackManager
from src.features.edit.ai_editing.transcript_provider import (
    ModelPackNotInstalledError,
    QwenASRProvider,
    _anon_speaker,
    _stub_diarization,
    _stub_transcript,
    merge_diarization,
)


# ---------------------------------------------------------------------- #
# helper：构造一个带可用模型 pack 的 provider（stub 路径，不依赖真实 qwen_asr）
# ---------------------------------------------------------------------- #


def _provider_with_packs(tmp_path, *, align: bool = True, diarize: bool = True) -> QwenASRProvider:
    """构造 provider：qwen3_asr 已装；align/diarize 按需装。

    注：find_spec("qwen_asr") 在测试环境通常为 None；provider.analyze 第一步就抛
    ModelPackNotInstalledError。因此本测试组用 monkeypatch 把 find_spec 桩成"已装"，
    以验证 pack 管理 + 时间换算 + diarization 逻辑（真实推理走 _run_qwen_asr_real，
    由 TOPOS_ASR_REAL=1 启用）。
    """
    mgr = ModelPackManager(cache_dir=tmp_path, strict_verification=False)
    # 装 qwen3_asr（transcript 必需）
    base_dir = tmp_path / "qwen3_asr"
    base_dir.mkdir()
    mgr.mark_installed("qwen3_asr", base_dir)
    if align:
        align_dir = tmp_path / "qwen3_align"
        align_dir.mkdir()
        mgr.mark_installed("qwen3_align", align_dir)
    if diarize:
        dia_dir = tmp_path / "nemo_sortformer"
        dia_dir.mkdir()
        mgr.mark_installed("nemo_sortformer", dia_dir)
    return QwenASRProvider(mgr)


@pytest.fixture
def stub_whisperx_available(monkeypatch):
    """桩 qwen_asr / nemo 包为"已安装"，使 provider 能走到 stub 推理路径。

    R4-c：保留旧 fixture 名（stub_whisperx_available）避免 22 个测试逐个改参数；
    实际桩的是 qwen_asr / nemo 包。

    注意：只覆盖指定包名，其他包（torch / transformers / numpy 等）回退到原始
    find_spec——否则会破坏 transformers 的依赖检测（它用 find_spec 探测 torch）。
    """
    _real_find_spec = importlib.util.find_spec

    def _patched_find_spec(name, *args, **kwargs):
        if name in ("qwen_asr", "nemo"):
            return object()  # 模拟"已安装"
        return _real_find_spec(name, *args, **kwargs)  # 其他包走原始逻辑

    monkeypatch.setattr(importlib.util, "find_spec", _patched_find_spec)


@pytest.fixture(autouse=True)
def _force_stub_inference(monkeypatch):
    """本文件测的是 pack 管理 / 时间换算 / diarization 逻辑，显式选 stub 路径。

    生产代码默认走真实推理；这里通过 ``TOPOS_ASR_REAL=0`` 强制走确定性 stub，
    使本文件约 18 个依赖 ``_stub_transcript`` / ``_stub_diarization`` 固定输出的
    测试无需改动断言。真实推理路径由 ``test_transcript_provider_real.py`` 覆盖。
    """
    monkeypatch.setenv("TOPOS_ASR_REAL", "0")


# ---------------------------------------------------------------------- #
# 契约
# ---------------------------------------------------------------------- #


class TestProviderContract:
    def test_provider_id_and_supported_kinds(self) -> None:
        assert QwenASRProvider.provider_id == "transcript_local"
        assert QwenASRProvider.supported_kinds == (SegmentKind.TRANSCRIPT,)

    def test_supports_word_segments_model(self) -> None:
        """M4：TranscriptSegment 必须能携带 WordSegment（词级时间码）。"""
        seg = TranscriptSegment(
            start_frame=0, end_frame=24, text="hello",
            words=(WordSegment(start_frame=0, end_frame=12, text="hello"),),
        )
        assert len(seg.words) == 1
        assert seg.words[0].text == "hello"


# ---------------------------------------------------------------------- #
# 缺失依赖：不静默降级（§11.1 M4 红线）
# ---------------------------------------------------------------------- #


class TestMissingDependency:
    def test_qwen_asr_package_missing_raises(self, tmp_path) -> None:
        """qwen_asr python 包未装 → 抛错，不返回空结果。

        注：qwen_asr 已安装时此测试跳过（无法模拟"未装"场景）。
        """
        if importlib.util.find_spec("qwen_asr") is not None:
            pytest.skip("qwen_asr is installed; cannot test 'missing package' path")
        provider = QwenASRProvider()
        with pytest.raises(ModelPackNotInstalledError) as exc_info:
            provider.analyze("asset-1", AssetFingerprint(), fps="24/1")
        assert "qwen3_asr" in str(exc_info.value) or "qwen_asr" in str(exc_info.value).lower()

    def test_model_pack_not_installed_raises(
        self, tmp_path, monkeypatch,
    ) -> None:
        """qwen_asr 包可 import 但模型 pack 未下载 → 抛错（明示缺失）。"""
        _real_find_spec = importlib.util.find_spec
        monkeypatch.setattr(
            importlib.util, "find_spec",
            lambda name, *a, **kw: object() if name == "qwen_asr" else _real_find_spec(name, *a, **kw),
        )
        mgr = ModelPackManager(cache_dir=tmp_path)  # 不装任何 pack
        provider = QwenASRProvider(mgr)
        with pytest.raises(ModelPackNotInstalledError) as exc_info:
            provider.analyze("asset-1", AssetFingerprint(), fps="24/1")
        assert exc_info.value.pack_id == "qwen3_asr"


# ---------------------------------------------------------------------- #
# 确定性 stub 推理路径
# ---------------------------------------------------------------------- #


class TestStubTranscript:
    def test_analyze_returns_transcript_field(
        self, tmp_path, stub_whisperx_available,
    ) -> None:
        provider = _provider_with_packs(tmp_path)
        analysis, segments = provider.analyze(
            "asset-1", AssetFingerprint(), fps="24/1",
        )
        assert isinstance(analysis, AssetAnalysis)
        assert analysis.asset_id == "asset-1"
        assert len(analysis.transcript) == 2  # stub 返回两段
        # 只填 transcript 字段（其余空，留给 _merge_analysis 合并）
        assert analysis.scene_cuts == ()

    def test_transcript_segments_have_frame_coords(
        self, tmp_path, stub_whisperx_available,
    ) -> None:
        provider = _provider_with_packs(tmp_path)
        analysis, _ = provider.analyze("asset-1", AssetFingerprint(), fps="24/1")
        for seg in analysis.transcript:
            assert seg.end_frame > seg.start_frame  # 半开区间，>=1 帧
            assert seg.text  # 非空
            assert seg.language == "en"

    def test_word_level_timecode_present(
        self, tmp_path, stub_whisperx_available,
    ) -> None:
        """M4 验收：词/段时间码。align 可用时 words 非空。"""
        provider = _provider_with_packs(tmp_path, align=True)
        analysis, _ = provider.analyze("asset-1", AssetFingerprint(), fps="24/1")
        first = analysis.transcript[0]
        assert len(first.words) >= 1
        for w in first.words:
            assert w.end_frame > w.start_frame
            assert w.text

    def test_word_frames_within_segment_range(
        self, tmp_path, stub_whisperx_available,
    ) -> None:
        """词的 frame 坐标必须落在所属 segment 的 frame 范围内（无越界）。"""
        provider = _provider_with_packs(tmp_path)
        analysis, _ = provider.analyze("asset-1", AssetFingerprint(), fps="24/1")
        for seg in analysis.transcript:
            for w in seg.words:
                assert w.start_frame >= seg.start_frame - 1  # 允许 round 误差
                assert w.end_frame <= seg.end_frame + 1


# ---------------------------------------------------------------------- #
# 降级：无对齐模型（§11.1 M4：降级必标注，不静默）
# ---------------------------------------------------------------------- #


class TestAlignmentDowngrade:
    def test_no_align_pack_marks_confidence_penalty(
        self, tmp_path, stub_whisperx_available,
    ) -> None:
        """align pack 未装 → words 空 + confidence 下调 + provenance.extra.alignment='none'。"""
        provider = _provider_with_packs(tmp_path, align=False)
        analysis, _ = provider.analyze("asset-1", AssetFingerprint(), fps="24/1")
        # words 应为空（降级为段级）
        for seg in analysis.transcript:
            assert seg.words == ()
        # confidence 被惩罚
        for seg in analysis.transcript:
            assert seg.confidence < 0.95  # stub 原 0.95/0.91 × 0.7
        # provenance 标注降级
        prov = analysis.field_provenance.for_field("transcript") if analysis.field_provenance else None
        assert prov is not None
        assert prov.extra.get("alignment") == "none"
        assert "confidence_penalty" in prov.extra

    def test_with_align_no_downgrade(
        self, tmp_path, stub_whisperx_available,
    ) -> None:
        provider = _provider_with_packs(tmp_path, align=True)
        analysis, _ = provider.analyze("asset-1", AssetFingerprint(), fps="24/1")
        prov = analysis.field_provenance.for_field("transcript")
        assert prov is not None
        assert "alignment" not in prov.extra  # 无降级标注
        # confidence 不打折
        assert analysis.transcript[0].confidence == pytest.approx(0.95)


# ---------------------------------------------------------------------- #
# diarization 匿名化（§11.1 M4 红线：speaker 仅 S1/S2，绝不实名）
# ---------------------------------------------------------------------- #


class TestAnonymousDiarization:
    def test_speakers_are_anonymous_s1_s2(
        self, tmp_path, stub_whisperx_available,
    ) -> None:
        provider = _provider_with_packs(tmp_path, diarize=True)
        analysis, _ = provider.analyze("asset-1", AssetFingerprint(), fps="24/1")
        speakers = {seg.speaker for seg in analysis.transcript}
        # 仅 S1/S2 这种匿名标签，绝不出现 Sortformer/pyannote 原标签
        assert speakers.issubset({"S1", "S2", None})
        assert "SPEAKER" not in str(speakers)

    def test_anon_speaker_naming(self) -> None:
        assert _anon_speaker(0) == "S1"
        assert _anon_speaker(1) == "S2"
        assert _anon_speaker(5) == "S6"

    def test_merge_diarization_assigns_speaker_by_overlap(self) -> None:
        """transcript 段按重叠最长的 diarization 段赋 speaker。"""
        segs = [
            TranscriptSegment(start_frame=10, end_frame=50, text="a"),   # 与 diar[0] 重叠多
            TranscriptSegment(start_frame=100, end_frame=150, text="b"),  # 与 diar[1] 重叠多
        ]
        # diar 用 frame 坐标（media_fps_ratio=None 时直接当 frame）
        from src.features.edit.ai_editing.transcript_provider import _RawDiarizationSegment
        diar = [
            _RawDiarizationSegment(sample_start=0, sample_end=60, speaker_index=0),
            _RawDiarizationSegment(sample_start=90, sample_end=160, speaker_index=1),
        ]
        merged = merge_diarization(segs, diar)
        assert merged[0].speaker == "S1"
        assert merged[1].speaker == "S2"

    def test_merge_diarization_no_overlap_keeps_none(self) -> None:
        """transcript 段与任何 diarization 都不重叠 → speaker=None。"""
        segs = [TranscriptSegment(start_frame=200, end_frame=250, text="isolated")]
        from src.features.edit.ai_editing.transcript_provider import _RawDiarizationSegment
        diar = [_RawDiarizationSegment(sample_start=0, sample_end=50, speaker_index=0)]
        merged = merge_diarization(segs, diar)
        assert merged[0].speaker is None

    def test_merge_diarization_empty_returns_speakers_none(self) -> None:
        """无 diarization → 所有 speaker 为 None（不丢段）。"""
        segs = [TranscriptSegment(start_frame=0, end_frame=10, text="x", speaker="S1")]
        merged = merge_diarization(segs, [])
        assert len(merged) == 1
        assert merged[0].speaker is None

    def test_no_diarize_pack_keeps_speakers_none(
        self, tmp_path, stub_whisperx_available,
    ) -> None:
        """diarize pack 未装 → speaker 全 None（不抛；transcript 仍可用）。"""
        provider = _provider_with_packs(tmp_path, diarize=False)
        analysis, _ = provider.analyze("asset-1", AssetFingerprint(), fps="24/1")
        for seg in analysis.transcript:
            assert seg.speaker is None


# ---------------------------------------------------------------------- #
# provenance + field-level（§18.2）
# ---------------------------------------------------------------------- #


class TestProvenance:
    def test_field_provenance_transcript(
        self, tmp_path, stub_whisperx_available,
    ) -> None:
        provider = _provider_with_packs(tmp_path)
        analysis, _ = provider.analyze("asset-1", AssetFingerprint(), fps="24/1")
        assert analysis.field_provenance is not None
        prov = analysis.field_provenance.for_field("transcript")
        assert prov is not None
        assert prov.provider == "transcript_local"
        assert "sample_rate" in prov.extra  # 记录采样率

    def test_segments_carry_transcript_kind(
        self, tmp_path, stub_whisperx_available,
    ) -> None:
        provider = _provider_with_packs(tmp_path)
        _, segments = provider.analyze("asset-1", AssetFingerprint(), fps="24/1")
        assert all(s.kind == SegmentKind.TRANSCRIPT for s in segments)
        # 每个 segment 的 payload 带 text（供 search）
        for s in segments:
            assert "text" in s.payload


# ---------------------------------------------------------------------- #
# 时间换算正确性（sample → frame 经 TimebaseService）
# ---------------------------------------------------------------------- #


class TestTimeConversion:
    def test_24fps_one_second_sample_maps_to_24_frames(
        self, tmp_path, stub_whisperx_available,
    ) -> None:
        """stub 第一段 pts_start=1s(16000 sample)，24fps → 第 24 帧（0-indexed）。"""
        provider = _provider_with_packs(tmp_path)
        analysis, _ = provider.analyze("asset-1", AssetFingerprint(), fps="24/1")
        first = analysis.transcript[0]
        # 16000 sample / 16000 sample_rate * 24 fps = 24 frame
        assert first.start_frame == 24

    def test_25fps_maps_differently(self, tmp_path, stub_whisperx_available) -> None:
        provider = _provider_with_packs(tmp_path)
        analysis, _ = provider.analyze("asset-1", AssetFingerprint(), fps="25/1")
        first = analysis.transcript[0]
        # 16000 / 16000 * 25 = 25
        assert first.start_frame == 25


# ---------------------------------------------------------------------- #
# 不写 timeline（§0 红线）
# ---------------------------------------------------------------------- #


class TestNoTimelineWrite:
    def test_analyze_does_not_return_timeline_ops(
        self, tmp_path, stub_whisperx_available,
    ) -> None:
        """分析只产 AssetAnalysis + segments；无任何 timeline/command 输出。"""
        provider = _provider_with_packs(tmp_path)
        analysis, segments = provider.analyze("asset-1", AssetFingerprint(), fps="24/1")
        # AssetAnalysis 不携带任何 timeline 变更字段
        assert not hasattr(analysis, "commands")
        assert not hasattr(analysis, "timeline_ops")
        # segments 是只读证据，非 command
        for s in segments:
            assert s.kind == SegmentKind.TRANSCRIPT
