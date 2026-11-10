"""P1-3 转录与说话人检测——独立 job 协议验收。

对应《媒体池后端交接计划》P1 第 3 项：
- 独立 job、语言、时间范围、证据时间戳和取消/重试协议；
- 结果与源素材 revision 绑定（过期不写入）；
- UI 只发意图（协议为纯值对象，不触碰推理 provider）。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.ai_edit

from src.features.edit.ai_editing.transcription_job import (
    TranscriptionJobError,
    TranscriptionJobFailure,
    TranscriptionJobRequest,
    TranscriptionJobResult,
    TranscriptionSegment,
    segments_from_dicts,
    trim_segments_to_range,
)


def _request(**overrides) -> TranscriptionJobRequest:
    payload = dict(
        asset_id="a1", project_id="p1", source_revision=7,
        source_fingerprint=(1024, 555),
    )
    payload.update(overrides)
    return TranscriptionJobRequest(**payload)


# ================================================================
# 请求协议
# ================================================================

def test_request_defaults_and_cancel_tag() -> None:
    req = _request()
    assert req.language is None            # 自动检测
    assert req.time_range_frames is None   # 全素材
    assert req.speaker_diarization is False
    assert req.data_policy == "local_only"
    assert req.cancel_tag == "transcribe:a1"  # scheduler 标签取消定位
    assert req.request_id


def test_request_frame_range_converts_to_seconds() -> None:
    req = _request(time_range_frames=(24, 48), fps=24.0)
    # [24, 48] 含端点 → 秒域 [24/fps, (48+1)/fps)
    assert req.time_range_seconds == pytest.approx((1.0, 49 / 24))


@pytest.mark.parametrize("overrides,hint", [
    (dict(asset_id=""), "asset_id"),
    (dict(source_revision=-1), "revision"),
    (dict(source_fingerprint=(0,)), "指纹"),
    (dict(time_range_frames=(10, 5)), "范围不合法"),
    (dict(time_range_frames=(0, 10)), "fps"),
    (dict(data_policy="everywhere"), "隐私策略"),
])
def test_request_validation_fail_loud(overrides, hint) -> None:
    with pytest.raises(TranscriptionJobError, match=hint):
        _request(**overrides)


# ================================================================
# 结果协议：证据时间戳 + 源绑定 + 序列化
# ================================================================

def _result(segments=None, fingerprint=(1024, 555), revision=7) -> TranscriptionJobResult:
    return TranscriptionJobResult(
        request_id="r1", asset_id="a1", project_id="p1",
        source_revision=revision, source_fingerprint=fingerprint,
        language="zh", speaker_diarization=True,
        segments=segments if segments is not None else (
            TranscriptionSegment(0.0, 2.0, "你好", speaker="SPEAKER_00"),
            TranscriptionSegment(2.0, 4.5, "世界", speaker="SPEAKER_01"),
        ))


def test_segments_keep_evidence_timestamps_and_speakers() -> None:
    result = _result()
    first, second = result.segments
    assert (first.start_seconds, first.end_seconds) == (0.0, 2.0)
    assert first.speaker == "SPEAKER_00"
    assert second.text == "世界"


def test_result_stale_detection_binds_source_and_revision() -> None:
    result = _result()
    assert result.is_stale_for((1024, 555), revision=7) is False
    # 源文件变化（重导出/替换）
    assert result.is_stale_for((1024, 556), revision=7) is True
    # 工程修订推进（源窗口可能已改）
    assert result.is_stale_for((1024, 555), revision=8) is True
    assert result.is_stale_for({"size_bytes": 1024, "mtime_ns": 555}) is False


def test_result_serialization_roundtrip() -> None:
    result = _result()
    restored = TranscriptionJobResult.from_dict(result.to_dict())
    assert restored.asset_id == result.asset_id
    assert restored.source_fingerprint == (1024, 555)
    assert restored.segments == result.segments
    assert restored.speaker_diarization is True


def test_segments_from_dicts_rejects_disorder() -> None:
    with pytest.raises(TranscriptionJobError, match="升序"):
        segments_from_dicts([
            {"start_seconds": 5.0, "end_seconds": 6.0, "text": "b"},
            {"start_seconds": 1.0, "end_seconds": 2.0, "text": "a"},
        ])


def test_trim_segments_to_range() -> None:
    segments = (
        TranscriptionSegment(0.0, 2.0, "相交前"),
        TranscriptionSegment(2.0, 4.5, "全含"),
        TranscriptionSegment(4.5, 6.0, "相交后"),
        TranscriptionSegment(5.0, 7.0, "外后"),
    )
    trimmed = trim_segments_to_range(segments, (1.0, 5.0))
    assert [seg.text for seg in trimmed] == ["相交前", "全含", "相交后"]
    assert trimmed[0].start_seconds == 1.0  # 相交段钳到范围
    assert trimmed[2].end_seconds == 5.0
    assert trimmed[1].start_seconds == 2.0 and trimmed[1].end_seconds == 4.5


# ================================================================
# 失败协议：错误码域 + 取消/重试
# ================================================================

def test_failure_error_code_domain_and_retryability() -> None:
    cancelled = TranscriptionJobFailure("r1", "cancelled", "用户取消")
    assert cancelled.is_cancellation
    assert cancelled.retryable is False  # 取消不自动重试；重试=新请求
    assert TranscriptionJobFailure("r1", "provider_error", "x").retryable
    assert TranscriptionJobFailure("r1", "no_audio_stream", "x").retryable is False
    with pytest.raises(TranscriptionJobError):
        TranscriptionJobFailure("r1", "mystery", "x")


def test_ui_intent_only_no_provider_import() -> None:
    """协议模块不得 import 推理实现/线程类（UI 只发意图的架构约束）。"""
    import ast
    import src.features.edit.ai_editing.transcription_job as module
    tree = ast.parse(open(module.__file__, encoding="utf-8").read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any("whisperx" in name.lower() or "qwen" in name.lower()
                   for name in imported)
    assert not any("analysis_service" in name for name in imported)
    assert not any("PyQt6" in name for name in imported)
