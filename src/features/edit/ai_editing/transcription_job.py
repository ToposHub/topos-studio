"""转录与说话人检测——独立 job 协议（P1-3）。

媒体池/时间线的转录能力此前分散在两条入口（媒体池三步流 provider
``transcript_auto`` 与 clip 级专用 QThread），缺少统一的 job 契约。本模块
定义协议数据类（值对象），把"独立 job、语言、时间范围、证据时间戳、
取消/重试、结果与源素材 revision 绑定"固化为单一契约：

- UI 只发意图：构造 :class:`TranscriptionJobRequest` 交给宿主（controller/
  scheduler），不直接调用推理 provider；
- 取消：宿主按 request_id 取消（沿用 P0-8 的标签取消协议；转录请求自带
  ``cancel_tag`` 供 scheduler 定位）；
- 重试：``TranscriptionJobError.retryable`` 声明可否重试；重试 = 发一个
  **新** request（携带同一 asset/范围），不自动重试、不复用旧 request_id；
- 源绑定：结果携带发起时的工程 revision 与源指纹（size+mtime_ns），
  :meth:`TranscriptionJobResult.is_stale_for` 判定源已变化——过期结果
  不得写入时间线/字幕轨；
- 证据时间戳：segment 的秒域 [start, end) 来自 provider 输出的帧精度
  映射，写字幕时再按时间线 fps 折算（帧精度映射由消费方持久化）。

推理本体仍由既有 AnalysisService provider（QwenASR/WhisperX）承担，
本模块不重复实现推理，只做协议与校验。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    "TRANSCRIPTION_JOB_SCHEMA_VERSION",
    "TranscriptionJobError",
    "TranscriptionJobRequest",
    "TranscriptionSegment",
    "TranscriptionJobResult",
    "trim_segments_to_range",
    "segments_from_dicts",
    "segments_to_dicts",
]


TRANSCRIPTION_JOB_SCHEMA_VERSION = 1

#: 取消/重试协议的错误码域（UI 据此分流，不解析自由文本）
TRANSCRIPTION_ERROR_CODES = (
    "cancelled",           # 用户取消（retryable=False——重试=新请求）
    "no_audio_stream",     # 素材无音频流（retryable=False，换素材）
    "decode_failed",       # 音频解码失败（retryable=True）
    "model_unavailable",   # 本地模型缺失/未授权（retryable=False）
    "provider_error",      # provider 执行失败（retryable=True）
    "language_unsupported",# 指定语言不支持（retryable=True，换语言）
    "source_stale",        # 源在执行期被替换/修改（retryable=True，重新探测）
    "budget_exceeded",     # 时长/预算超限（retryable=True，缩小范围）
)


class TranscriptionJobError(ValueError):
    """转录 job 协议数据不合法（构造期 fail-loud）。"""


def _fingerprint(value: Any) -> tuple[int, int]:
    """指纹载荷 {(size_bytes, mtime_ns) | dict} → 规整元组。"""
    if isinstance(value, dict):
        size = value.get("size_bytes")
        mtime = value.get("mtime_ns")
    elif isinstance(value, (tuple, list)) and len(value) == 2:
        size, mtime = value
    else:
        raise TranscriptionJobError(
            f"源指纹必须是 (size_bytes, mtime_ns)，实际: {value!r}")
    if (isinstance(size, bool) or not isinstance(size, int) or size < 0
            or isinstance(mtime, bool) or not isinstance(mtime, int)):
        raise TranscriptionJobError(f"源指纹数值非法: {value!r}")
    return (int(size), int(mtime))


@dataclass(frozen=True)
class TranscriptionJobRequest:
    """一次转录/说话人检测意图（UI 构造，宿主消费；纯值对象）。"""

    asset_id: str
    project_id: str
    source_revision: int
    source_fingerprint: tuple[int, int]
    language: Optional[str] = None            # None = 自动检测（BCP-47 码）
    time_range_frames: Optional[tuple[int, int]] = None  # None = 全素材（含端点）
    fps: Optional[float] = None               # 帧域范围折算秒域用
    speaker_diarization: bool = False
    data_policy: str = "local_only"           # local_only | approved_cloud
    cancel_tag: str = ""                      # scheduler 标签取消定位
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    requested_at: float = field(default_factory=time.time)
    schema_version: int = TRANSCRIPTION_JOB_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.asset_id or not self.project_id:
            raise TranscriptionJobError("asset_id/project_id 不能为空")
        if self.source_revision < 0:
            raise TranscriptionJobError("source_revision 不能为负")
        object.__setattr__(self, "source_fingerprint",
                           _fingerprint(self.source_fingerprint))
        if self.time_range_frames is not None:
            start, end = self.time_range_frames
            if (isinstance(start, bool) or isinstance(end, bool)
                    or not isinstance(start, int) or not isinstance(end, int)):
                raise TranscriptionJobError("时间范围必须是整数帧对")
            if start < 0 or end <= start:
                raise TranscriptionJobError(
                    f"时间范围不合法: [{start}, {end}]")
            if self.fps is None or self.fps <= 0:
                raise TranscriptionJobError("帧域时间范围需要正 fps")
        if self.data_policy not in ("local_only", "approved_cloud"):
            raise TranscriptionJobError(f"未知隐私策略: {self.data_policy}")
        if not self.cancel_tag:
            object.__setattr__(self, "cancel_tag", f"transcribe:{self.asset_id}")

    @property
    def time_range_seconds(self) -> Optional[tuple[float, float]]:
        """帧域范围 → 秒域 [start, end)（无范围为 None）。"""
        if self.time_range_frames is None or not self.fps:
            return None
        start, end = self.time_range_frames
        return (start / self.fps, (end + 1) / self.fps)


@dataclass(frozen=True)
class TranscriptionSegment:
    """一条转录片段（证据时间戳：秒域 [start, end)，说话人可空）。"""

    start_seconds: float
    end_seconds: float
    text: str
    speaker: Optional[str] = None
    confidence: Optional[float] = None

    def __post_init__(self) -> None:
        if not isinstance(self.start_seconds, (int, float)) or \
                not isinstance(self.end_seconds, (int, float)):
            raise TranscriptionJobError("segment 时间戳必须是数值")
        if self.start_seconds < 0 or self.end_seconds <= self.start_seconds:
            raise TranscriptionJobError(
                f"segment 时间戳不合法: [{self.start_seconds}, {self.end_seconds})")
        if not isinstance(self.text, str):
            raise TranscriptionJobError("segment text 必须是字符串")


def segments_from_dicts(raw: Any) -> tuple[TranscriptionSegment, ...]:
    """持久化 dicts → segments（顺序与数值校验）。"""
    if not isinstance(raw, (list, tuple)):
        raise TranscriptionJobError("segments 载荷必须是列表")
    segments = tuple(
        TranscriptionSegment(
            start_seconds=float(item["start_seconds"]),
            end_seconds=float(item["end_seconds"]),
            text=str(item.get("text", "")),
            speaker=item.get("speaker"),
            confidence=item.get("confidence"),
        )
        for item in raw if isinstance(item, dict)
    )
    for previous, current in zip(segments, segments[1:]):
        if current.start_seconds < previous.start_seconds - 1e-9:
            raise TranscriptionJobError("segments 必须按 start_seconds 升序")
    return segments


def segments_to_dicts(segments: tuple[TranscriptionSegment, ...]) -> list[dict]:
    return [
        {
            "start_seconds": seg.start_seconds,
            "end_seconds": seg.end_seconds,
            "text": seg.text,
            "speaker": seg.speaker,
            "confidence": seg.confidence,
        }
        for seg in segments
    ]


def trim_segments_to_range(
    segments: tuple[TranscriptionSegment, ...],
    range_seconds: tuple[float, float],
) -> tuple[TranscriptionSegment, ...]:
    """把全素材结果裁到请求的时间范围（半开区间边界，证据时间戳保留）。"""
    low, high = range_seconds
    trimmed: list[TranscriptionSegment] = []
    for seg in segments:
        if seg.end_seconds <= low or seg.start_seconds >= high:
            continue
        start = max(seg.start_seconds, low)
        end = min(seg.end_seconds, high)
        if end <= start:
            continue
        # 与范围相交：段的时间戳钳到范围（证据仍是 provider 原始秒域）
        trimmed.append(TranscriptionSegment(
            start_seconds=start, end_seconds=end, text=seg.text,
            speaker=seg.speaker, confidence=seg.confidence))
    return tuple(trimmed)


@dataclass(frozen=True)
class TranscriptionJobResult:
    """转录结果（与源素材 revision/指纹绑定；过期不得写入时间线）。"""

    request_id: str
    asset_id: str
    project_id: str
    source_revision: int
    source_fingerprint: tuple[int, int]
    language: Optional[str]
    segments: tuple[TranscriptionSegment, ...]
    speaker_diarization: bool = False
    completed_at: float = field(default_factory=time.time)
    schema_version: int = TRANSCRIPTION_JOB_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_fingerprint",
                           _fingerprint(self.source_fingerprint))

    def is_stale_for(
        self,
        fingerprint: tuple[int, int] | dict,
        revision: Optional[int] = None,
    ) -> bool:
        """源已变化（指纹不一致）/工程修订推进 → True（结果过期）。"""
        current = _fingerprint(fingerprint)
        if current != self.source_fingerprint:
            return True
        if revision is not None and revision != self.source_revision:
            return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "asset_id": self.asset_id,
            "project_id": self.project_id,
            "source_revision": self.source_revision,
            "source_fingerprint": {
                "size_bytes": self.source_fingerprint[0],
                "mtime_ns": self.source_fingerprint[1],
            },
            "language": self.language,
            "speaker_diarization": self.speaker_diarization,
            "completed_at": self.completed_at,
            "segments": segments_to_dicts(self.segments),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranscriptionJobResult":
        return cls(
            request_id=str(data["request_id"]),
            asset_id=str(data["asset_id"]),
            project_id=str(data["project_id"]),
            source_revision=int(data["source_revision"]),
            source_fingerprint=_fingerprint(data.get("source_fingerprint")),
            language=data.get("language"),
            segments=segments_from_dicts(data.get("segments", ())),
            speaker_diarization=bool(data.get("speaker_diarization", False)),
            completed_at=float(data.get("completed_at", 0.0)),
        )


@dataclass(frozen=True)
class TranscriptionJobFailure:
    """转录 job 终态失败（错误码域 + 可否重试；取消也是失败的一种）。

    重试协议：retryable=True 时 UI 发**新** TranscriptionJobRequest（同
    asset/范围，新 request_id）；retryable=False 只能换素材/换语言/装模型。
    """

    request_id: str
    error_code: str
    message: str

    def __post_init__(self) -> None:
        if self.error_code not in TRANSCRIPTION_ERROR_CODES:
            raise TranscriptionJobError(f"未知错误码: {self.error_code}")

    @property
    def retryable(self) -> bool:
        return self.error_code in (
            "decode_failed", "provider_error", "language_unsupported",
            "source_stale", "budget_exceeded")

    @property
    def is_cancellation(self) -> bool:
        return self.error_code == "cancelled"
