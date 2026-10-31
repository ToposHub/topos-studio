"""R4-c：QwenASRProvider 真实推理路径的覆盖测试。

不依赖真实 qwen_asr / nemo_toolkit 安装——用 monkeypatch 注入 fake engine +
fake audio extractor，验证 ``_run_qwen_asr_real`` / ``_run_sortformer_real`` 的：

- Qwen3-ASR.transcribe 输出 → _RawSegment（秒→sample 换算）
- ForcedAligner 输出 → 词级时间戳解析
- Sortformer.diarize 输出 → _RawDiarizationSegment（秒→sample + 匿名化）
- 引擎惰性初始化 + 复用
- 音频提取调用 + 临时文件清理
- 错误容错（引擎异常、空结果）
"""

from __future__ import annotations

import importlib.util
import os
from typing import Any

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.transcript_provider import (  # noqa: E402
    QwenASRProvider,
    _RawSegment,
    _asr_result_is_suspicious,
    _normalize_language,
    _parse_qwen_align_results,
    _parse_qwen_time_stamps,
    _parse_sortformer_segment,
)


# ================================================================
# 测试夹具
# ================================================================


class _FakeASRResult:
    """模拟 qwen_asr 的 transcribe 单条结果。"""

    def __init__(self, text: str, language: str, time_stamps: Any = None) -> None:
        self.text = text
        self.language = language
        self.time_stamps = time_stamps


class _FakeASREngine:
    """模拟 Qwen3ASRModel。"""

    def __init__(self, result: _FakeASRResult | None = None) -> None:
        self._result = result
        self.call_count = 0
        self.last_audio = None

    def transcribe(self, audio=None, **kwargs) -> list:
        self.call_count += 1
        self.last_audio = audio
        if self._result is None:
            return []
        return [self._result]


class _FakeAligner:
    """模拟 Qwen3ForcedAligner。"""

    def __init__(self, results: list | None = None) -> None:
        self._results = results or []
        self.call_count = 0

    def align(self, audio=None, text=None, language=None) -> list:
        self.call_count += 1
        return self._results


class _FakeDiarModel:
    """模拟 SortformerEncLabelModel。"""

    def __init__(self, segments: list[str] | None = None) -> None:
        self._segments = segments or []
        self.call_count = 0

    def diarize(self, audio=None, **kwargs) -> list:
        self.call_count += 1
        return [self._segments]  # 嵌套：[["1.0,2.0,0", ...]]

    def eval(self) -> None:
        pass


class _FakeAudioExtractor:
    """模拟 AudioExtractor——返回假的 wav 路径，不真实抽音频。"""

    def __init__(self, wav_path: str = "/tmp/fake_asr.wav") -> None:
        self._wav_path = wav_path
        self.call_count = 0

    def extract_wav(self, source_path: str, **kwargs) -> str:
        self.call_count += 1
        return self._wav_path


@pytest.fixture
def stub_qwen_asr_available(monkeypatch):
    """桩 qwen_asr / nemo 包为"已安装"。

    注意：只覆盖指定包名，其他包（torch / transformers / numpy 等）回退到原始
    find_spec——否则会破坏 transformers 的依赖检测（它用 find_spec 探测 torch）。
    """
    _real_find_spec = importlib.util.find_spec

    def _patched_find_spec(name, *args, **kwargs):
        if name in ("qwen_asr", "nemo"):
            return object()  # 模拟"已安装"
        return _real_find_spec(name, *args, **kwargs)  # 其他包走原始逻辑

    monkeypatch.setattr(importlib.util, "find_spec", _patched_find_spec)


def _provider_with_engine(
    asr_engine: Any, *, tmp_path,
    aligner: Any = None, diar_model: Any = None,
    asset_path_provider=lambda aid: f"/tmp/{aid}.mp4",
) -> QwenASRProvider:
    """构造 provider，注入 fake 引擎 + fake 音频提取。"""
    from src.features.edit.ai_editing.model_pack_manager import ModelPackManager

    mgr = ModelPackManager(cache_dir=tmp_path, strict_verification=False)
    # 装 qwen3_asr（必需）
    base_dir = tmp_path / "qwen3_asr"
    base_dir.mkdir()
    mgr.mark_installed("qwen3_asr", base_dir)

    provider = QwenASRProvider(
        model_pack_manager=mgr,
        audio_extractor=_FakeAudioExtractor(),
        asset_path_provider=asset_path_provider,
    )
    provider._asr_engine = asr_engine
    if aligner is not None:
        provider._aligner = aligner
    if diar_model is not None:
        provider._diar_model = diar_model
    return provider


# ================================================================
# _run_qwen_asr_real 核心路径
# ================================================================


def test_run_asr_real_parses_text(stub_qwen_asr_available, tmp_path, monkeypatch):
    """真实路径：Qwen3-ASR 文本输出 → _RawSegment（无对齐时 segment 级）。"""
    monkeypatch.setenv("TOPOS_ASR_REAL", "1")

    fake_result = _FakeASRResult(text="你好世界", language="Chinese")
    engine = _FakeASREngine(result=fake_result)
    provider = _provider_with_engine(engine, tmp_path=tmp_path)

    # 无 qwen3_align pack → align_available=False，不请求 time_stamps
    raw_segments = provider._run_qwen_asr_real("asset_1")

    assert len(raw_segments) == 1
    seg = raw_segments[0]
    assert seg.text == "你好世界"
    assert seg.language == "zh"  # Chinese → zh 归一化
    assert seg.words == ()  # 无对齐
    # engine.transcribe 被调用一次
    assert engine.call_count == 1


def test_run_asr_real_with_word_timestamps(stub_qwen_asr_available, tmp_path, monkeypatch):
    """真实路径 + 对齐：ASR 结果自带 time_stamps → 词级 _RawSegment.words。"""
    monkeypatch.setenv("TOPOS_ASR_REAL", "1")

    # 构造 time_stamps：[(word, start_sec, end_sec), ...]
    time_stamps = [("你", 0.0, 0.5), ("好", 0.5, 1.0), ("世界", 1.0, 1.8)]
    fake_result = _FakeASRResult(text="你好世界", language="Chinese", time_stamps=time_stamps)

    engine = _FakeASREngine(result=fake_result)

    from src.features.edit.ai_editing.model_pack_manager import ModelPackManager
    mgr = ModelPackManager(cache_dir=tmp_path, strict_verification=False)
    base_dir = tmp_path / "qwen3_asr"
    base_dir.mkdir()
    mgr.mark_installed("qwen3_asr", base_dir)
    align_dir = tmp_path / "qwen3_align"
    align_dir.mkdir()
    mgr.mark_installed("qwen3_align", align_dir)

    provider = QwenASRProvider(
        model_pack_manager=mgr,
        audio_extractor=_FakeAudioExtractor(),
        asset_path_provider=lambda aid: f"/tmp/{aid}.mp4",
    )
    provider._asr_engine = engine

    raw_segments = provider._run_qwen_asr_real("asset_1")

    assert len(raw_segments) == 1
    seg = raw_segments[0]
    assert seg.text == "你好世界"
    assert len(seg.words) == 3
    # 秒→sample 换算：0.0s × 16000 = 0；0.5s × 16000 = 8000
    assert seg.words[0] == (0, 8000, "你", 0.95)
    assert seg.words[2] == (16000, 28800, "世界", 0.95)
    # segment 覆盖首词到末词
    assert seg.pts_start == seg.words[0][0]
    assert seg.pts_end == seg.words[-1][1]


def test_run_asr_real_empty_text_returns_empty(stub_qwen_asr_available, tmp_path, monkeypatch):
    """ASR 返回空文本 → 空列表（不构造 segment）。"""
    monkeypatch.setenv("TOPOS_ASR_REAL", "1")

    fake_result = _FakeASRResult(text="", language="English")
    engine = _FakeASREngine(result=fake_result)
    provider = _provider_with_engine(engine, tmp_path=tmp_path)

    raw_segments = provider._run_qwen_asr_real("asset_1")
    assert raw_segments == []


def test_mps_garbled_result_retries_on_cpu(stub_qwen_asr_available, tmp_path, monkeypatch):
    """MPS 推理成功但输出乱码时，必须自动重跑 CPU float32。"""
    monkeypatch.setenv("TOPOS_ASR_REAL", "1")
    provider = _provider_with_engine(_FakeASREngine(), tmp_path=tmp_path)
    provider._asr_engine_device = "mps"
    bad = [_RawSegment(0, 16000, "language�_FMTonth", "unknown", 0.95, ())]
    good = [_RawSegment(0, 16000, "这是正常的中文转写", "zh", 0.8, ())]
    calls: list[bool] = []

    def _fake_run(_asset_id: str):
        calls.append(provider._asr_force_cpu)
        return good if provider._asr_force_cpu else bad

    monkeypatch.setattr(provider, "_run_qwen_asr_real", _fake_run)
    result = provider._run_asr("asset_1")

    assert result == good
    assert calls == [False, True]
    assert provider._thread_local.asr_fallback_reason == "mps_to_cpu_float32"


def test_asr_result_quality_does_not_reject_normal_single_segment() -> None:
    """单个 segment 本身不是错误，正常整段转写不能被误判。"""
    segment = _RawSegment(0, 16000, "hello world", "en", 0.9, ())
    assert not _asr_result_is_suspicious([segment])


def test_asr_result_quality_rejects_replacement_character() -> None:
    segment = _RawSegment(0, 16000, "language�_FMTonth", "unknown", 0.9, ())
    assert _asr_result_is_suspicious([segment])


def test_run_asr_real_audio_extraction_called(stub_qwen_asr_available, tmp_path, monkeypatch):
    """真实路径调 AudioExtractor.extract_wav 抽音频。"""
    monkeypatch.setenv("TOPOS_ASR_REAL", "1")

    fake_audio = _FakeAudioExtractor()
    fake_result = _FakeASRResult(text="test", language="English")

    from src.features.edit.ai_editing.model_pack_manager import ModelPackManager
    mgr = ModelPackManager(cache_dir=tmp_path, strict_verification=False)
    (tmp_path / "qwen3_asr").mkdir()
    mgr.mark_installed("qwen3_asr", tmp_path / "qwen3_asr")

    provider = QwenASRProvider(
        model_pack_manager=mgr,
        audio_extractor=fake_audio,
        asset_path_provider=lambda aid: f"/videos/{aid}.mp4",
    )
    provider._asr_engine = _FakeASREngine(result=fake_result)
    provider._run_qwen_asr_real("video_1")

    assert fake_audio.call_count == 1


# ================================================================
# _run_sortformer_real
# ================================================================


def test_run_sortformer_real_parses_segments(stub_qwen_asr_available, tmp_path, monkeypatch):
    """Sortformer 输出 "begin,end,speaker" → _RawDiarizationSegment（秒→sample）。"""
    monkeypatch.setenv("TOPOS_ASR_REAL", "1")

    diar_segments = ["0.5,3.0,0", "3.5,6.0,1"]
    diar_model = _FakeDiarModel(segments=diar_segments)

    provider = _provider_with_engine(
        _FakeASREngine(result=_FakeASRResult(text="test", language="English")),
        tmp_path=tmp_path, diar_model=diar_model,
    )

    # _run_diarization 会先检查 pack usable + find_spec + env
    from src.features.edit.ai_editing.model_pack_manager import ModelPackManager
    mgr = provider._pack_manager
    (tmp_path / "nemo_sortformer").mkdir()
    mgr.mark_installed("nemo_sortformer", tmp_path / "nemo_sortformer")

    result = provider._run_diarization("asset_1")

    assert len(result) == 2
    # 0.5s × 16000 = 8000；3.0s × 16000 = 48000
    assert result[0].sample_start == 8000
    assert result[0].sample_end == 48000
    assert result[0].speaker_index == 0
    assert result[1].speaker_index == 1


def test_run_diarization_no_pack_returns_empty(stub_qwen_asr_available, tmp_path):
    """nemo_sortformer pack 未装 → 返回空（不抛；transcript 仍可用）。"""
    provider = _provider_with_engine(
        _FakeASREngine(), tmp_path=tmp_path,
    )
    # 不装 nemo_sortformer pack
    result = provider._run_diarization("asset_1")
    assert result == []


# ================================================================
# 辅助函数
# ================================================================


def test_normalize_language():
    """语言名 → ISO 短码。"""
    assert _normalize_language("Chinese") == "zh"
    assert _normalize_language("English") == "en"
    assert _normalize_language("Japanese") == "ja"
    assert _normalize_language("Korean") == "ko"
    assert _normalize_language("French") == "fr"
    assert _normalize_language("unknown_lang") == "un"  # 取前2字符


def test_parse_qwen_time_stamps():
    """Qwen3 time_stamps 解析（秒→sample）。"""
    ts = [("hello", 1.0, 1.5), ("world", 1.6, 2.0)]
    result = _parse_qwen_time_stamps(ts, sample_rate=16000, language="en")
    assert len(result) == 2
    assert result[0] == (16000, 24000, "hello", 0.95)
    assert result[1] == (25600, 32000, "world", 0.95)


def test_parse_qwen_time_stamps_empty():
    """空 time_stamps → 空元组。"""
    assert _parse_qwen_time_stamps([], 16000, "en") == ()
    assert _parse_qwen_time_stamps(None, 16000, "en") == ()


def test_parse_sortformer_segment():
    """Sortformer 单段解析。"""
    seg = _parse_sortformer_segment("1.5,3.0,0", sample_rate=16000)
    assert seg is not None
    assert seg.sample_start == 24000
    assert seg.sample_end == 48000
    assert seg.speaker_index == 0


def test_parse_sortformer_segment_invalid():
    """无效格式 → None。"""
    assert _parse_sortformer_segment("invalid", 16000) is None
    assert _parse_sortformer_segment("1,2", 16000) is None  # 缺 speaker
    assert _parse_sortformer_segment("", 16000) is None


def test_parse_qwen_align_results():
    """ForcedAligner 独立调用结果解析。"""
    class _Item:
        def __init__(self, text, start, end):
            self.text = text
            self.start_time = start
            self.end_time = end

    items = [_Item("hello", 0.0, 0.5), _Item("world", 0.5, 1.0)]
    result = _parse_qwen_align_results(items, sample_rate=16000, language="en")
    assert len(result) == 2
    assert result[0] == (0, 8000, "hello", 0.95)


# ================================================================
# 惰性引擎初始化
# ================================================================


def test_get_asr_engine_lazy_and_reused(stub_qwen_asr_available, monkeypatch, tmp_path):
    """_get_asr_engine 惰性创建 + 复用（mock from_pretrained 避免真实加载）。"""
    import sys
    import types
    from unittest.mock import patch

    instances = [0]
    init_kwargs_seen = []
    def _fake_from_pretrained(model_id, **kwargs):
        instances[0] += 1
        init_kwargs_seen.append((model_id, kwargs))
        return _FakeASREngine(result=_FakeASRResult(text="test", language="en"))

    # 注入最小 qwen_asr 模块，避免测试环境需要安装可选的真实模型依赖。
    class _FakeQwen3ASRModel:
        @classmethod
        def from_pretrained(cls, *args, **kwargs):
            return _fake_from_pretrained(*args, **kwargs)

    monkeypatch.setitem(
        sys.modules,
        "qwen_asr",
        types.SimpleNamespace(Qwen3ASRModel=_FakeQwen3ASRModel),
    )

    # patch from_pretrained（避免真实加载模型）
    from qwen_asr import Qwen3ASRModel
    with patch.object(Qwen3ASRModel, "from_pretrained", classmethod(lambda cls, *a, **kw: _fake_from_pretrained(*a, **kw))):
        from src.features.edit.ai_editing.model_pack_manager import ModelPackManager
        mgr = ModelPackManager(cache_dir=tmp_path, strict_verification=False)
        (tmp_path / "qwen3_asr").mkdir()
        mgr.mark_installed("qwen3_asr", tmp_path / "qwen3_asr")

        provider = QwenASRProvider(model_pack_manager=mgr)
        monkeypatch.setattr(provider, "_engine_init_kwargs", lambda: {})
        provider._get_asr_engine()
        assert instances[0] == 1
        provider._get_asr_engine()  # 复用
        assert instances[0] == 1
        assert init_kwargs_seen[0][1]["max_new_tokens"] == 8192


def test_asr_options_select_model_and_long_output_limit(monkeypatch, tmp_path):
    """Per-request options override the old 512-token Qwen default."""
    from src.features.edit.ai_editing.model_pack_manager import ModelPackManager

    mgr = ModelPackManager(cache_dir=tmp_path, strict_verification=False)
    large_dir = tmp_path / "qwen3_asr_large"
    large_dir.mkdir()
    mgr.mark_installed("qwen3_asr_large", large_dir)
    provider = QwenASRProvider(model_pack_manager=mgr)
    provider._thread_local.request_options = {
        "model_size": "large",
        "max_new_tokens": 16384,
    }

    assert provider._selected_model_pack_id() == "qwen3_asr_large"
    assert provider._max_new_tokens() == 16384


def test_model_version_reads_qwen_asr(stub_qwen_asr_available, monkeypatch):
    """_model_version 经 importlib.metadata 读取 qwen-asr 版本（不触发 import）。"""
    import importlib.metadata

    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.0.6")
    version = QwenASRProvider._model_version()
    assert version == "qwen3-asr-0.0.6"


def test_model_version_fallback_when_not_installed():
    """_model_version 始终返回 qwen3-asr 前缀（装/未装都包含）。"""
    version = QwenASRProvider._model_version()
    assert "qwen3-asr" in version
