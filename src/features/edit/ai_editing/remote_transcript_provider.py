"""Provider-neutral HTTP transcription adapter.

The adapter intentionally supports two wire formats:

* OpenAI-compatible ``/audio/transcriptions`` multipart APIs;
* Volcengine v3 file-ASR APIs using ``X-Api-Key`` and
  ``X-Api-Resource-Id`` headers.

It returns the same ``AssetAnalysis`` contract as local Qwen3-ASR.  The
profile owns the endpoint and model/resource ID, so a vendor-specific plan or
future endpoint can be adjusted in Preferences without changing the editor.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import ssl
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.features.edit.ai_editing.analysis_models import (
    AnalysisProvenance,
    AssetAnalysis,
    AssetFingerprint,
    FieldProvenance,
    TranscriptSegment,
    WordSegment,
    analysis_to_segments,
)
from src.features.edit.ai_editing.credential_store import CredentialStore
from src.features.edit.ai_editing.provider_profiles import AiProviderProfile
from src.features.edit.ai_editing.timebase import ProviderTimebase, TimebaseService
from src.shared.media.metadata import Rational


_MAX_AUDIO_BYTES = 100 * 1024 * 1024
_DEFAULT_ASR_RESOURCE_ID = "volc.seedasr.sauc.duration"


class RemoteTranscriptError(RuntimeError):
    """Sanitized remote ASR error; response bodies are never included."""


class RemoteTranscriptProvider:
    """Run one configured remote ASR profile against a local media asset."""

    supported_kinds = ()

    def __init__(
        self,
        profile: AiProviderProfile,
        credential_store: CredentialStore,
        *,
        asset_path_provider: Any | None = None,
        opener: Any | None = None,
    ) -> None:
        self._profile = profile
        self._credential_store = credential_store
        self._asset_path_provider = asset_path_provider
        self._opener = opener or urllib.request.urlopen
        self._timebase = TimebaseService()

    @property
    def provider_id(self) -> str:
        return self._profile.profile_id

    def is_available(self) -> bool:
        return bool(
            self._profile.endpoint
            and self._credential_store.get_secret(self._profile.credential_ref)
        )

    def validate_options(self, _options: dict[str, Any] | None = None) -> str | None:
        # ASR uploads the source media itself.  Do not infer consent merely
        # from selecting a cloud preset; the explicit media-upload checkbox
        # is required for every remote transcription request.
        if not self._profile.allows_media_upload:
            return "当前 ASR Profile 未允许素材上传，请在 AI editing → 语音模型中勾选上传策略"
        if not self._profile.endpoint:
            return "当前 ASR Profile 未配置 URL"
        if not self._credential_store.get_secret(self._profile.credential_ref):
            return "当前 ASR Profile 未配置 API Key"
        return None

    def analyze(
        self,
        asset_id: str,
        fingerprint: AssetFingerprint,
        *,
        fps: str,
        progress: Any | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[AssetAnalysis, tuple[Any, ...]]:
        validation = self.validate_options(options)
        if validation:
            raise RemoteTranscriptError(validation)
        if self._asset_path_provider is None:
            raise RemoteTranscriptError("当前工程没有提供素材路径，无法上传音频")
        path = str(self._asset_path_provider(asset_id) or "")
        if not path or not os.path.isfile(path):
            raise RemoteTranscriptError("音频素材文件不存在")
        if os.path.getsize(path) > _MAX_AUDIO_BYTES:
            raise RemoteTranscriptError("音频文件超过远程 ASR 的 100 MB 限制")
        if progress is not None:
            progress(0.1, "正在将音频发送到已选择的第三方 ASR")
        payload = self._request(path, options or {})
        if progress is not None:
            progress(0.8, "第三方 ASR 已返回，正在整理时间码")
        segments = self._segments_from_payload(payload, fps=fps, path=path)
        if not segments:
            raise RemoteTranscriptError("第三方 ASR 返回了空转录结果")
        provenance = AnalysisProvenance(
            provider=self.provider_id,
            model=self._profile.model_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            extra={"execution": "cloud", "remote_protocol": self._protocol()},
        )
        analysis = AssetAnalysis(
            asset_id=asset_id,
            fingerprint=fingerprint,
            fps=fps,
            transcript=tuple(segments),
            provenance=provenance,
            field_provenance=FieldProvenance.single("transcript", provenance),
        )
        if progress is not None:
            progress(1.0, "第三方 ASR 转录完成")
        return analysis, tuple(analysis_to_segments(analysis))

    def _protocol(self) -> str:
        kind = self._profile.provider_kind.lower()
        return "volcengine_v3_asr" if kind.startswith("volcengine_") else "openai_transcription"

    def _request(self, path: str, options: Mapping[str, Any]) -> dict[str, Any]:
        secret = self._credential_store.get_secret(self._profile.credential_ref) or ""
        if self._protocol() == "volcengine_v3_asr":
            return self._request_volcengine(path, secret, options)
        return self._request_openai(path, secret, options)

    def _request_volcengine(
        self, path: str, secret: str, options: Mapping[str, Any],
    ) -> dict[str, Any]:
        task_id = str(uuid.uuid4())
        request = urllib.request.Request(
            self._profile.endpoint,
            data=Path(path).read_bytes(),
            headers={
                "X-Api-Key": secret,
                "X-Api-Resource-Id": self._profile.model_id or _DEFAULT_ASR_RESOURCE_ID,
                "X-Api-Request-Id": task_id,
                "X-Api-Sequence": "-1",
                "Content-Type": "application/octet-stream",
                "Accept": "application/json",
            },
            method="POST",
        )
        return self._open_json(request)

    def _request_openai(
        self, path: str, secret: str, options: Mapping[str, Any],
    ) -> dict[str, Any]:
        boundary = f"----ToposASR{uuid.uuid4().hex}"
        fields = {
            "model": self._profile.model_id or "whisper-1",
            "response_format": "json",
        }
        if options.get("language") and options["language"] != "auto":
            fields["language"] = str(options["language"])
        chunks: list[bytes] = []
        for key, value in fields.items():
            chunks.extend([
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
                str(value).encode(), b"\r\n",
            ])
        filename = os.path.basename(path).replace('"', "_")
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            Path(path).read_bytes(), b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ])
        request = urllib.request.Request(
            self._audio_transcription_url(),
            data=b"".join(chunks),
            headers={
                "Authorization": f"Bearer {secret}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
            },
            method="POST",
        )
        return self._open_json(request)

    def _audio_transcription_url(self) -> str:
        endpoint = self._profile.endpoint.rstrip("/")
        if endpoint.endswith("/audio/transcriptions"):
            return endpoint
        return endpoint + "/audio/transcriptions"

    def _open_json(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            response = self._open(request)
            with response:
                raw = response.read(_MAX_AUDIO_BYTES)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise RemoteTranscriptError("第三方 ASR 鉴权失败") from exc
            if exc.code == 429:
                raise RemoteTranscriptError("第三方 ASR 触发限流") from exc
            raise RemoteTranscriptError(f"第三方 ASR HTTP 错误（{exc.code}）") from exc
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise RemoteTranscriptError("第三方 ASR 网络请求失败") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteTranscriptError("第三方 ASR 返回格式不是 JSON") from exc
        if not isinstance(payload, dict):
            raise RemoteTranscriptError("第三方 ASR 返回格式无效")
        return payload

    def _open(self, request: urllib.request.Request) -> Any:
        timeout = max(0.1, float(self._profile.limits.request_timeout_seconds))
        if self._opener is not urllib.request.urlopen:
            return self._opener(request, timeout=timeout)
        try:
            import certifi
            context = ssl.create_default_context(cafile=certifi.where())
            return self._opener(request, timeout=timeout, context=context)
        except (ImportError, OSError, TypeError):
            return self._opener(request, timeout=timeout)

    def _segments_from_payload(
        self, payload: Mapping[str, Any], *, fps: str, path: str,
    ) -> list[TranscriptSegment]:
        root: Mapping[str, Any] = payload
        for key in ("result", "data", "output"):
            value = root.get(key)
            if isinstance(value, Mapping):
                root = value
                break
        utterances = root.get("utterances") or root.get("sentences") or root.get("segments")
        if not isinstance(utterances, list):
            text = root.get("text") or payload.get("text")
            if not text:
                return []
            duration_ms = self._duration_ms(path)
            if duration_ms is None:
                # AUD-14301：远端只返回整段 text 且 ffprobe 缺失/失败时，
                # 无法得知真实时长——默认 1000ms 会把数分钟音频的转录
                # 末帧钳到 1 秒处（静默数据错误）。显式失败，不猜。
                raise RemoteTranscriptError(
                    "无法探测音频时长（ffprobe 不可用或解析失败），"
                    "整段转录需要真实时长，拒绝生成时间码"
                )
            utterances = [{"start_time": 0, "end_time": duration_ms, "text": str(text)}]

        media_fps = Rational.from_string(fps)
        speakers: dict[str, str] = {}
        out: list[TranscriptSegment] = []
        cursor_ms = 0.0
        times_are_seconds = self._protocol() != "volcengine_v3_asr"
        for item in utterances:
            if not isinstance(item, Mapping):
                continue
            text = str(item.get("text") or item.get("sentence") or "").strip()
            if not text:
                continue
            # AUD-14302：显式 null / 非法时间不得静默回 0.0（片段跳回帧 0），
            # 而是沿用游标（start）/ 起点之后（end）的可观测回退。
            start_ms = self._time_ms(
                item.get("start_time", item.get("start", cursor_ms)),
                seconds=times_are_seconds,
                fallback=cursor_ms,
            )
            end_ms = self._time_ms(
                item.get("end_time", item.get("end", start_ms + 1000)),
                seconds=times_are_seconds,
                fallback=start_ms + 1000.0,
            )
            if end_ms <= start_ms:
                end_ms = start_ms + 1
            start_frame, end_frame = self._frame_range(start_ms, end_ms, media_fps)
            words = self._parse_words(item.get("words"), media_fps, seconds=times_are_seconds)
            raw_speaker = item.get("speaker") or (item.get("additions") or {}).get("speaker")
            speaker = None
            if raw_speaker is not None:
                key = str(raw_speaker)
                speaker = speakers.setdefault(key, f"S{len(speakers) + 1}")
            out.append(TranscriptSegment(
                start_frame=start_frame,
                end_frame=end_frame,
                text=text,
                speaker=speaker,
                confidence=self._confidence(item),
                language=str(root.get("language")) if root.get("language") else None,
                words=words,
            ))
            cursor_ms = end_ms
        return out

    def _parse_words(
        self, raw: Any, fps: Rational, *, seconds: bool,
    ) -> tuple[WordSegment, ...]:
        if not isinstance(raw, list):
            return ()
        out: list[WordSegment] = []
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            text = str(item.get("text") or item.get("word") or "").strip()
            if not text:
                continue
            start_ms = self._time_ms(
                item.get("start_time", item.get("start", 0)), seconds=seconds,
            )
            end_ms = self._time_ms(
                item.get("end_time", item.get("end", start_ms + 1)), seconds=seconds,
                fallback=start_ms + 1.0,
            )
            start_frame, end_frame = self._frame_range(start_ms, end_ms, fps)
            out.append(WordSegment(
                start_frame=start_frame,
                end_frame=end_frame,
                text=text,
                confidence=self._confidence(item),
            ))
        return tuple(out)

    def _frame_range(self, start_ms: float, end_ms: float, fps: Rational) -> tuple[int, int]:
        result = self._timebase.to_source_frame_range(
            ProviderTimebase(
                pts_start=int(round(max(0.0, start_ms))),
                pts_end=int(round(max(start_ms, end_ms))),
                timebase=Rational(1, 1000),
            ),
            fps,
        )
        return result.source_range.start, result.source_range.end

    @staticmethod
    def _time_ms(value: Any, *, seconds: bool = False, fallback: float = 0.0) -> float:
        """把远端时间值转换为毫秒；非法/None/非有限值回退 *fallback*。

        AUD-14302：此前非法输入统一静默映射 0.0（外部数据校验 fail-open，
        片段跳回帧 0）；现在由调用方指定语义回退值（段落 start 沿用游标、
        end 回退起点之后），错误时间码不再静默入库。
        """
        safe_fallback = fallback if math.isfinite(fallback) else 0.0
        try:
            number = float(value)
        except (TypeError, ValueError):
            return safe_fallback
        if not math.isfinite(number):
            return safe_fallback
        return number * 1000.0 if seconds else number

    @staticmethod
    def _confidence(item: Mapping[str, Any], default: float = 0.9) -> float:
        """AUD-14300：远端返回非数值 confidence 时安全回退默认值。

        ``float()`` 的 ValueError/TypeError 属于远端数据畸形，不得以未净化
        异常逃逸（模块契约保证调用方只见 RemoteTranscriptError 或正常结果，
        异常消息可能携带远端响应内容）。
        """
        if "confidence" not in item:
            return default
        raw = item.get("confidence")
        if raw is None:
            return default
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return default
        return number if math.isfinite(number) else default

    @classmethod
    def _duration_ms(cls, path: str) -> float | None:
        """ffprobe 探测音频总时长（毫秒）；失败返回 None 而非猜测值。

        AUD-14301：此前 ffprobe 失败回退 1000ms，整段转录的 end_frame 被
        钳到 1 秒处（静默数据错误）。现在返回 None，由调用方决定显式失败。
        """
        try:
            completed = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, check=True, timeout=10,
            )
            duration_ms = float(completed.stdout.strip()) * 1000.0
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        if not math.isfinite(duration_ms):
            return None
        return max(1.0, duration_ms)


__all__ = ["RemoteTranscriptError", "RemoteTranscriptProvider"]
