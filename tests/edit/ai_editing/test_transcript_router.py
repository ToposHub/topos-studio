from src.features.edit.ai_editing.analysis_models import (
    AnalysisProvenance,
    AssetAnalysis,
    AssetFingerprint,
    TranscriptSegment,
)
from src.features.edit.ai_editing.editorial_models import SegmentKind
from src.features.edit.ai_editing.transcript_router import AdaptiveTranscriptProvider


def _analysis(provider: str, confidence: float, text: str = "hello") -> AssetAnalysis:
    return AssetAnalysis(
        asset_id="a1",
        fingerprint=AssetFingerprint(size=1, mtime_ns=1),
        fps="24/1",
        transcript=(
            TranscriptSegment(0, 24, text, confidence=confidence),
        ),
        provenance=AnalysisProvenance(provider=provider, model="test"),
    )


class _Qwen:
    def __init__(self, original: AssetAnalysis, separated: AssetAnalysis) -> None:
        self.original = original
        self.separated = separated
        self.calls = []

    def analyze(self, *args, **kwargs):
        self.calls.append("original")
        return self.original, ()

    def analyze_with_preprocessor(self, *args, **kwargs):
        self.calls.append("demucs")
        return self.separated, ()


class _Backend:
    def __init__(self, analysis: AssetAnalysis, available: bool = True) -> None:
        self.analysis = analysis
        self.available = available
        self.calls = 0

    def is_available(self):
        return self.available

    def analyze(self, *args, **kwargs):
        self.calls += 1
        return self.analysis, ()


def test_auto_keeps_good_qwen_result_without_demucs_or_whisperx() -> None:
    qwen = _Qwen(_analysis("transcript_local", 0.95), _analysis("transcript_local", 0.95))
    router = AdaptiveTranscriptProvider(qwen, mode="auto")

    analysis, _ = router.analyze("a1", AssetFingerprint(1, 1), fps="24/1")

    assert qwen.calls == ["original"]
    assert analysis.provenance.extra["router_selection"] == "qwen3_asr_original"


def test_auto_uses_demucs_when_it_improves_low_confidence_qwen() -> None:
    qwen = _Qwen(_analysis("transcript_local", 0.2), _analysis("transcript_local", 0.9))
    demucs = _Backend(_analysis("unused", 0.9))
    router = AdaptiveTranscriptProvider(
        qwen, demucs_preprocessor=demucs, mode="auto", quality_threshold=0.65
    )

    analysis, _ = router.analyze("a1", AssetFingerprint(1, 1), fps="24/1")

    assert qwen.calls == ["original", "demucs"]
    assert analysis.provenance.extra["router_selection"] == "qwen3_asr_demucs_v4"


def test_remove_background_music_forces_demucs_before_qwen() -> None:
    qwen = _Qwen(_analysis("transcript_local", 0.95), _analysis("transcript_local", 0.9))
    demucs = _Backend(_analysis("unused", 0.9))
    router = AdaptiveTranscriptProvider(qwen, demucs_preprocessor=demucs, mode="auto")

    analysis, _ = router.analyze(
        "a1", AssetFingerprint(1, 1), fps="24/1",
        options={"background_music": "remove"},
    )

    assert qwen.calls == ["demucs"]
    assert analysis.provenance.extra["router_selection"] == "qwen3_asr_demucs_forced"


def test_remove_background_music_reports_unavailable_backend_before_running() -> None:
    qwen = _Qwen(_analysis("transcript_local", 0.95), _analysis("transcript_local", 0.9))
    demucs = _Backend(_analysis("unused", 0.9), available=False)
    router = AdaptiveTranscriptProvider(qwen, demucs_preprocessor=demucs, mode="auto")

    error = router.validate_options({"background_music": "remove"})

    assert error is not None
    assert "无法启动人声分离" in error


def test_auto_falls_back_to_whisperx_when_optional_preprocessing_unavailable() -> None:
    qwen = _Qwen(_analysis("transcript_local", 0.2), _analysis("transcript_local", 0.2))
    whisperx = _Backend(_analysis("transcript_whisperx", 0.85), available=True)
    demucs = _Backend(_analysis("unused", 0.2), available=False)
    router = AdaptiveTranscriptProvider(
        qwen,
        whisperx_provider=whisperx,
        demucs_preprocessor=demucs,
        mode="auto",
        quality_threshold=0.65,
    )

    analysis, _ = router.analyze("a1", AssetFingerprint(1, 1), fps="24/1")

    assert whisperx.calls == 1
    assert analysis.provenance.extra["router_selection"] == "whisperx_fallback"
