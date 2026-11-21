"""Authorized media-model adapters for the background library index."""
from __future__ import annotations

import hashlib
import importlib.util
import math
import os
import re
import wave
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

from src.features.edit.ai_editing.analysis_models import AssetFingerprint
from src.features.edit.ai_editing.mcp_vision_gateway import (
    McpVisionGateway,
    ZHIPU_VISION_ORIGIN,
    ZHIPU_VISION_PROVIDER_KIND,
    ZHIPU_VISION_TOOL,
)
from src.features.files.services.ai_library import LibraryAnalysisPolicy, MediaEvidence
from src.features.files.transactions import CancelToken
from src.shared.ai_models.generation.model_pack_manager import (
    ModelPackManager,
    ModelPackNotInstalledError,
)
from src.shared.ai_models.generation.provider_profiles import AiProviderProfile, ProfileRegistry

_CAPTION_PACK = "qwen3_vl_8b_awq"
_AUDIO_PACK = "qwen2_5_omni_7b_awq"
_OCR_PACK = "rapidocr_ocr"
_ASR_PACKS = ("qwen3_asr", "qwen3_asr_large")
_AUDIO_RATE = 16000
_AUDIO_WINDOW = 30
_VISUAL_WINDOW = 20.0
_TRANSCRIPT_CLOCK = 1000
_MediaCapability = Literal["vision", "asr"]
LibraryMediaKind = Literal["image", "video", "audio"]
LIBRARY_MEDIA_KINDS: tuple[LibraryMediaKind, ...] = ("image", "video", "audio")
_AnalysisMode = Literal["basic", "vision", "asr", "local_models"]


@dataclass(frozen=True)
class MediaRoute:
    capability: _MediaCapability
    profile: AiProviderProfile
    origin: str
    requires_upload: bool
    approval: str
    kind: LibraryMediaKind | None = None

    @property
    def native_asr(self) -> bool:
        return self.capability == "asr" and self.profile.provider_kind == "local_qwen_asr"

    def summary(self, *, default: bool = False) -> dict[str, Any]:
        return {"capability": self.capability, "kind": self.kind, "profile_id": self.profile.profile_id,
                "name": self.profile.display_name, "origin": self.origin, "default": default,
                "model": self.profile.model_id, "approval": self.approval, "upload": self.requires_upload,
                "upload_allowed": self.profile.allows_media_upload}


def _load_profiles() -> ProfileRegistry:
    registry = ProfileRegistry()
    registry.load_from_user_file()
    return registry


def _media_route(
    profile: AiProviderProfile, capability: _MediaCapability, kind: LibraryMediaKind | None = None,
) -> MediaRoute | None:
    from src.shared.ai_models.generation.media_inference import (
        MediaInferenceClient,
        MediaInferenceConfigurationError,
        validate_media_endpoint,
    )

    if not profile.model_id.strip() or capability not in profile.routed_capabilities:
        return None
    endpoint = profile.endpoint.strip()
    if capability == "asr" and profile.provider_kind == "local_qwen_asr" and not endpoint:
        if profile.model_id not in _ASR_PACKS:
            return None
        origin = "本机模型包"
        requires_upload = False
    elif profile.provider_kind == ZHIPU_VISION_PROVIDER_KIND:
        if capability != "vision" or endpoint != ZHIPU_VISION_ORIGIN or profile.model_id != ZHIPU_VISION_TOOL:
            return None
        origin = ZHIPU_VISION_ORIGIN
        requires_upload = True
    else:
        if not MediaInferenceClient.supports(profile.provider_kind, capability):
            return None
        try:
            parsed, loopback = validate_media_endpoint(endpoint)
        except MediaInferenceConfigurationError:
            return None
        origin = f"{parsed.scheme}://{parsed.netloc}"
        requires_upload = not loopback
    identity = capability, profile.profile_id, profile.provider_kind, endpoint, profile.model_id
    # Retain the original approval fingerprint for existing capability routes.
    approval = hashlib.sha256(repr(identity if kind is None else (*identity, kind)).encode("utf-8")).hexdigest()
    return MediaRoute(capability, profile, origin, requires_upload, approval, kind)


def active_media_routes(registry: ProfileRegistry | None = None) -> dict[str, MediaRoute]:
    """Find the global routes used by legacy directory policies."""
    registry = registry if registry is not None else _load_profiles()
    supported: tuple[_MediaCapability, ...] = ("vision", "asr")
    routes: dict[str, MediaRoute] = {}
    for capability in supported:
        profile = registry.active_profile_for_capability(capability)
        if profile is not None and (route := _media_route(profile, capability)) is not None:
            routes[capability] = route
    return routes


def library_media_route(
    kind: LibraryMediaKind, profile_id: str, registry: ProfileRegistry | None = None,
) -> MediaRoute | None:
    """Resolve one explicitly selected profile without using a global fallback."""
    registry = registry if registry is not None else _load_profiles()
    profile = registry.get(profile_id)
    capability: _MediaCapability = "asr" if kind == "audio" else "vision"
    return _media_route(profile, capability, kind) if profile is not None else None


def library_model_options(registry: ProfileRegistry | None = None) -> list[dict[str, Any]]:
    """Present compatible configured profiles by media kind without loading models."""
    registry = registry if registry is not None else _load_profiles()
    options = []
    for kind in LIBRARY_MEDIA_KINDS:
        for profile in registry.list():
            route = library_media_route(kind, profile.profile_id, registry)
            if route is not None:
                active = registry.active_profile_for_capability(route.capability)
                options.append(route.summary(default=active == profile))
    return options


def configured_library_routes(
    policy: LibraryAnalysisPolicy,
) -> tuple[tuple[MediaRoute, ...], tuple[LibraryMediaKind, ...]]:
    """Return the explicit routes and any selected kinds missing a compatible model."""
    registry = _load_profiles()
    selected = dict(policy.models or ())
    routes: list[MediaRoute] = []
    missing: list[LibraryMediaKind] = []
    for kind in LIBRARY_MEDIA_KINDS:
        if kind not in policy.kinds:
            continue
        route = library_media_route(kind, selected.get(kind, ""), registry)
        if route is not None and route.native_asr:
            try:
                # Creating the adapter verifies the dependency and model pack
                # without allocating model weights or opening a media file.
                _ = LibraryAnalyzer(mode="asr", route=route)._asr
            except (ImportError, _ModelUnavailable, ModelPackNotInstalledError):
                route = None
        if route is None:
            missing.append(kind)
        else:
            routes.append(route)
    return tuple(routes), tuple(missing)


@dataclass(frozen=True)
class _Shot:
    start: float
    end: float


@dataclass(frozen=True)
class _MediaTiming:
    start: float
    end: float
    has_audio: bool


class _ModelUnavailable(RuntimeError):
    """A modality cannot run until the verified model or runtime changes."""


class LibraryAnalyzer:
    """Analyze registered media through the selected, authorized analysis stage."""

    def __init__(
        self, *, cancel: CancelToken | None = None,
        model_pack_manager: ModelPackManager | None = None,
        mode: _AnalysisMode = "basic",
        route: MediaRoute | None = None,
        read_policy: Callable[[], LibraryAnalysisPolicy] | None = None,
        scope_path: str | None = None,
    ) -> None:
        self._cancel = cancel
        self._manager = model_pack_manager
        self._mode = mode
        self._route = route
        self._read_policy = read_policy
        self._scope_path = Path(scope_path) if scope_path is not None else None
        self._source: tuple[Path, str] | None = None
        self.index_scope = {"local_models": "complete"}.get(mode, mode)
        self.index_modalities = {"basic": ("ocr",), "vision": ("visual",), "asr": ("transcript",)}.get(mode, ())
        self.index_kinds = {"vision": ("image", "video"), "asr": ("audio", "video")}.get(mode, ())
        if route is not None and route.kind is not None:
            self.index_kinds = (route.kind,)
        self._active_heavy_model: Any = None
        self._mcp_gateway: McpVisionGateway | None = None
        self._mcp_key: str | None = None
        self.warnings: list[str] = []
        self.retryable_errors: list[str] = []

    @property
    def retryable_failure(self) -> bool:
        return bool(self.retryable_errors)

    def close(self) -> None:
        if self._mcp_gateway is not None:
            self._mcp_gateway.shutdown()
            self._mcp_gateway = None
        self._mcp_key = None

    @cached_property
    def _packs(self) -> ModelPackManager:
        if self._manager is not None:
            return self._manager
        from src.app.controllers.ai_editing_mixin import _build_model_pack_manager

        return _build_model_pack_manager()

    @cached_property
    def revision(self) -> str:
        # Read the installed inventory once per scan. Adding a backend or
        # updating a pack invalidates the earlier partial analysis.
        snapshot: list[tuple[Any, ...]] = [("mode", self._mode)]
        if self._mode == "basic":
            packs = (_OCR_PACK,)
            modules = ("rapidocr", "rapidocr_onnxruntime")
        elif self._mode == "local_models":
            packs = (_CAPTION_PACK, _AUDIO_PACK, _OCR_PACK, *_ASR_PACKS)
            modules = ("av", "qwen_asr", "torch", "transformers", "soundfile", "rapidocr", "rapidocr_onnxruntime")
        elif self._route is not None and self._route.native_asr:
            packs = (self._route.profile.model_id,)
            modules = ("av", "qwen_asr", "torch")
        else:
            packs = ()
            modules = ("av",)
        for pack in packs:
            status = self._packs.get_status(pack)
            spec = self._packs.get_spec(pack)
            snapshot.append((pack, status.usable and not status.is_demo, spec.sha256 if spec else ""))
        for module in modules:
            snapshot.append((module, importlib.util.find_spec(module) is not None))
        if self._mode in ("asr", "local_models"):
            snapshot.append(("asr-real", os.environ.get("TOPOS_ASR_REAL") != "0"))
        if self._mode == "vision":
            snapshot.append(("output", "caption-and-tags-v1"))
        if self._route is not None:
            snapshot.append(("route", self._route.approval))
        return "library-analysis-v2:" + hashlib.sha256(repr(snapshot).encode()).hexdigest()[:24]

    def _authorized_route(self, capability: _MediaCapability) -> MediaRoute:
        self._check_cancel()
        policy = self._read_policy() if self._read_policy is not None else LibraryAnalysisPolicy()
        if policy.mode != "profiles" or self._route is None:
            raise RuntimeError("已停止使用目录的模型配置")
        if self._source is not None:
            path, source_kind = self._source
            scope = self._scope_path
            if source_kind not in policy.kinds or (scope is not None and (
                not path.is_relative_to(scope) or (not policy.recursive and path.parent != scope)
            )):
                raise RuntimeError("素材不在此目录当前的分析范围内，请重新开始分析")
        kind = self._route.kind
        if kind is None:
            current = active_media_routes().get(capability) if policy.models is None else None
        else:
            selected = dict(policy.models or ())
            if kind not in policy.kinds or selected.get(kind) != self._route.profile.profile_id:
                raise RuntimeError("此目录的素材类型或分析模型已发生变化，请重新开始分析")
            current = library_media_route(kind, self._route.profile.profile_id)
        if current is None or current.approval != self._route.approval:
            raise RuntimeError("已绑定的模型地址发生变化，请重新确认目录设置")
        if current.requires_upload and (
            not current.profile.allows_media_upload or current.approval not in policy.approved_routes
        ):
            raise RuntimeError("此目录未允许向当前模型发送媒体")
        return current

    def _media_client(self, capability: _MediaCapability) -> Any:
        from src.shared.ai_models.generation.credential_store import (
            get_credential_storage_preference,
            make_credential_store,
        )
        from src.shared.ai_models.generation.media_inference import MediaInferenceClient

        route = self._authorized_route(capability)
        profile = route.profile
        key = None
        if profile.credential_ref:
            store = make_credential_store(get_credential_storage_preference())
            key = store.get_secret(profile.credential_ref)
        if profile.provider_kind == ZHIPU_VISION_PROVIDER_KIND:
            self._mcp_key = key
            if self._mcp_gateway is None:
                self._mcp_gateway = McpVisionGateway(
                    api_key_provider=lambda: self._mcp_key, request_timeout=profile.limits.request_timeout_seconds,
                    cancel_requested=(lambda: self._cancel.requested) if self._cancel is not None else None,
                )
            return self._mcp_gateway
        return MediaInferenceClient(profile, key or "")

    def _check_cancel(self) -> None:
        if self._cancel is not None and self._cancel.requested:
            raise RuntimeError("素材分析已取消")

    def _warn(self, text: str, *, retryable: bool = False) -> None:
        if text not in self.warnings and len(self.warnings) < 10:
            self.warnings.append(text)
        if retryable and text not in self.retryable_errors and len(self.retryable_errors) < 10:
            self.retryable_errors.append(text)

    def _record_failure(self, label: str, error: Exception) -> None:
        unavailable = isinstance(error, (_ModelUnavailable, ModelPackNotInstalledError, ImportError))
        self._warn(f"{label}：{error}", retryable=not unavailable)

    def _model_path(self, pack: str) -> str:
        status = self._packs.get_status(pack)
        if status.is_demo:
            raise ModelPackNotInstalledError(pack, detail="需要通过完整性校验的模型包")
        return self._packs.require_usable(pack)

    @cached_property
    def _captioner(self) -> Any:
        from apps.topos_vault.providers.real_visual_caption import Qwen3VLCaptioner

        captioner = Qwen3VLCaptioner(local_model_path=self._model_path(_CAPTION_PACK))
        if not captioner.is_available():
            raise _ModelUnavailable("视觉模型的 transformers 或 torch 运行依赖未安装")
        return captioner

    @cached_property
    def _audio_model(self) -> Any:
        from apps.topos_vault.providers.real_audio_understand import Qwen25OmniAudioProvider

        provider = Qwen25OmniAudioProvider(local_model_path=self._model_path(_AUDIO_PACK))
        if not provider.is_available():
            raise _ModelUnavailable("音频理解模型的 transformers、torch 或 soundfile 运行依赖未安装")
        provider.AUDIO_PROMPT = (
            "请根据可听见的内容标注这段音频，用中文输出以下四行。"
            "情绪：<情绪>\n乐器：<明确听见的乐器，逗号分隔；没有则留空>\n"
            "类型：<对白、音乐、音效、环境声或混合之一>\n"
            "描述：<描述声音及可能的来源，如开门、风雨或车流，无法判断时说明不确定>"
        )
        return provider

    @cached_property
    def _ocr(self) -> Any:
        root = Path(self._model_path(_OCR_PACK))
        paths: dict[str, str] = {}
        for role in ("det", "cls", "rec"):
            candidates = sorted(path for path in root.rglob("*.onnx")
                                if role in re.split(r"[^a-z0-9]+", path.stem.lower()))
            if len(candidates) != 1:
                raise _ModelUnavailable(f"RapidOCR 模型包需提供唯一的 {role} ONNX 权重")
            paths[role] = str(candidates[0])
        dictionaries = sorted(path for path in root.rglob("*.txt")
                              if "dict" in path.stem.lower() or "keys" in path.stem.lower())
        if len(dictionaries) != 1:
            raise _ModelUnavailable("RapidOCR 模型包需提供唯一的本地识别字典")
        try:
            from rapidocr import RapidOCR
        except ImportError:
            from rapidocr_onnxruntime import RapidOCR

            kwargs = {f"{role}_model_path": path for role, path in paths.items()}
            kwargs["rec_keys_path"] = str(dictionaries[0])
            return RapidOCR(**kwargs)

        params = {f"{role.capitalize()}.model_path": path for role, path in paths.items()}
        params["Rec.rec_keys_path"] = str(dictionaries[0])
        return RapidOCR(params=params)

    @cached_property
    def _asr(self) -> Any:
        from src.features.edit.ai_editing.transcript_provider import QwenASRProvider

        if os.environ.get("TOPOS_ASR_REAL") == "0":
            raise _ModelUnavailable("当前环境禁用了真实语音识别")
        if importlib.util.find_spec("qwen_asr") is None:
            raise _ModelUnavailable("qwen_asr 语音识别运行依赖未安装")
        # Guard both model choices before the provider is allowed to resolve
        # an engine. For library work a missing local model cannot use HF.
        self._model_path(self._asr_pack)
        class _LocalASR(QwenASRProvider):
            def _safe_pack_path(self, pack_id: str) -> str:
                return self._require_pack(pack_id)

        return _LocalASR(model_pack_manager=self._packs, asset_path_provider=str)

    def _use_heavy_model(self, model: Any) -> Any:
        # The visual and standalone audio models may each need several GB.
        # Keep only the currently used large model loaded across assets.
        previous = self._active_heavy_model
        if previous is not None and previous is not model:
            unload = getattr(previous, "unload", None)
            if callable(unload):
                unload()
        self._active_heavy_model = model
        return model

    @cached_property
    def _asr_pack(self) -> str:
        if self._route is not None and self._route.native_asr:
            pack = self._route.profile.model_id
            if pack not in _ASR_PACKS:
                raise _ModelUnavailable("请选择已安装的 Qwen3-ASR 模型包")
            return pack
        usable = self._packs.get_status("qwen3_asr_large")
        return "qwen3_asr_large" if usable.usable and not usable.is_demo else "qwen3_asr"

    def _attempt(
        self, label: str, work: Callable[[], tuple[MediaEvidence, ...]],
    ) -> tuple[MediaEvidence, ...]:
        self._check_cancel()
        try:
            result = work()
        except (OSError, RuntimeError, ValueError, ImportError) as error:
            self._check_cancel()
            self._record_failure(label, error)
            return ()
        self._check_cancel()
        return result

    def __call__(self, path: str, kind: str) -> tuple[MediaEvidence, ...]:
        self.warnings.clear()
        self.retryable_errors.clear()
        self._check_cancel()
        if not Path(path).is_file():
            raise RuntimeError("素材文件当前不可读")
        if self._mode == "basic" and kind != "image":
            self._warn("基础模式为视频和音频登记文件信息")
            return ()
        if self.index_kinds and kind not in self.index_kinds:
            return ()
        self._source = (Path(path), kind)
        try:
            if kind == "image":
                result = self._image(path)
            elif kind in ("video", "audio"):
                timing = _probe_timing(path, kind)
                result = (self._attempt("视频画面", lambda: self._video(path, timing))
                          if kind == "video" and self._mode != "asr" else ())
                if timing.has_audio and self._mode != "vision":
                    result += self._attempt("音频", lambda: self._audio(path, kind, timing))
            else:
                return ()
        finally:
            self._source = None
        if not result and self.retryable_errors:
            raise RuntimeError("；".join(self.retryable_errors))
        return result

    def _image(self, path: str) -> tuple[MediaEvidence, ...]:
        ocr = self._available("_ocr", "文字识别") if self._mode != "vision" else False
        if self._mode == "basic" and ocr is False:
            return ()
        from PIL import Image, ImageOps

        with TemporaryDirectory(prefix="topos-library-image-") as temp:
            staging = str(Path(temp) / "frame.png")
            with Image.open(path) as opened:
                if opened.width * opened.height > 100_000_000:
                    raise RuntimeError("图片超出批量分析的像素上限")
                opened.draft("RGB", (1600, 1600))
                image = ImageOps.exif_transpose(opened)
                image.thumbnail((1600, 1600))
                image.convert("RGB").save(staging)
            return self._describe_frame(staging, None, caption=self._mode != "basic", ocr=ocr)

    def _describe_frame(self, path: str, shot: _Shot | None, *, caption: Any = None,
                        ocr: Any = None) -> tuple[MediaEvidence, ...]:
        start, end = (shot.start, shot.end) if shot else (None, None)

        def describe() -> tuple[MediaEvidence, ...]:
            if self._mode == "vision":
                text, tags = self._media_client("vision").analyze_image(path)
                if not tags:
                    self._warn("视觉服务未返回可用标签，已保存画面描述")
            else:
                observed = self._use_heavy_model(
                    self._captioner if caption in (None, True) else caption
                ).caption_image(path)
                text = observed.caption.strip()
                tags = tuple(observed.tags)
            return (MediaEvidence(text, start, end, tags, "visual"),) if text else ()

        def recognize() -> tuple[MediaEvidence, ...]:
            from src.features.edit.ai_editing.ocr_provider import _normalize_rapidocr_output

            _, lines, _ = _normalize_rapidocr_output((ocr or self._ocr)(path))
            text = " ".join(line.strip() for line in lines if line.strip())
            return (MediaEvidence(text, start, end, modality="ocr"),) if text else ()

        evidence = self._attempt("画面描述", describe) if caption is not False else ()
        return evidence + (self._attempt("文字识别", recognize) if ocr is not False else ())

    def _available(self, attr: str, title: str) -> Any:
        try:
            return getattr(self, attr)
        except (OSError, RuntimeError, ValueError, ImportError) as error:
            self._record_failure(title, error)
            return False

    def _video(self, path: str, timing: _MediaTiming) -> tuple[MediaEvidence, ...]:
        caption = True if self._mode == "vision" else self._available("_captioner", "画面描述")
        ocr = False if self._mode == "vision" else self._available("_ocr", "文字识别")
        if caption is False and ocr is False:
            return ()
        shots = self._detect_shots(path, timing)
        output: list[MediaEvidence] = []
        with _open_media(path) as video, TemporaryDirectory(prefix="topos-library-video-") as temp:
            stream = video.streams.video[0]
            staging = str(Path(temp) / "frame.png")
            for shot in shots:
                self._check_cancel()
                image = _sample_shot(video, stream, timing.start, shot, self._check_cancel)
                if image is None:
                    self._warn(f"镜头 {shot.start:.2f} 秒：无法读取代表帧", retryable=True)
                    continue
                image.thumbnail((1280, 1280))
                image.save(staging)
                output.extend(self._describe_frame(staging, shot, caption=caption, ocr=ocr))
        return tuple(output)

    def _detect_shots(self, path: str, timing: _MediaTiming) -> tuple[_Shot, ...]:
        from src.features.edit.core.scene_cut.detector import SceneCutDetector
        from src.features.edit.core.scene_cut.frame_sampler import CancelToken as DetectorToken, CancelledError
        from src.features.edit.core.scene_cut.models import SceneCutKind

        class _LinkedToken(DetectorToken):
            def is_cancelled(inner_self) -> bool:
                return bool(self._cancel and self._cancel.requested) or super().is_cancelled()

            def check_and_raise(inner_self) -> None:
                if inner_self.is_cancelled():
                    raise CancelledError("素材分析已取消")

        bounds = [0.0]
        try:
            analysis = SceneCutDetector(path, Path(path).name).detect(cancel_token=_LinkedToken())
        except CancelledError as error:
            raise RuntimeError("素材分析已取消") from error
        self._check_cancel()
        end = timing.end - timing.start
        for candidate in analysis.candidates:
            if (candidate.kind in (SceneCutKind.HARD_CUT, SceneCutKind.SOFT_TRANSITION)
                    and not candidate.is_rejected and not candidate.is_low_confidence):
                cut = candidate.seconds - timing.start
                if bounds[-1] < cut < end:
                    bounds.append(cut)
        bounds.append(end)
        return tuple(
            _Shot(start + i * _VISUAL_WINDOW, min(stop, start + (i + 1) * _VISUAL_WINDOW))
            for start, stop in zip(bounds, bounds[1:])
            for i in range(math.ceil((stop - start) / _VISUAL_WINDOW))
        )

    def _audio(self, path: str, kind: str, timing: _MediaTiming) -> tuple[MediaEvidence, ...]:
        local_asr = self._mode == "local_models" or bool(self._route and self._route.native_asr)
        asr = self._available("_asr", "语音转写") if local_asr else True
        # Video already has visual evidence. Use the audio-understanding
        # model for standalone audio, where music and effects matter most.
        audio = self._available("_audio_model", "音频理解") if kind == "audio" and self._mode == "local_models" else False
        if asr is False and audio is False:
            return ()
        output: list[MediaEvidence] = []
        with TemporaryDirectory(prefix="topos-library-audio-") as temp:
            stage = Path(temp) / "window.wav"
            for start, pcm in _audio_windows(path, timing.start, self._check_cancel):
                self._check_cancel()
                end = min(timing.end - timing.start, start + len(pcm) / (2 * _AUDIO_RATE))
                if end <= start:
                    continue
                with wave.open(str(stage), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(_AUDIO_RATE)
                    wav.writeframes(pcm)
                if asr is not False:
                    output.extend(self._attempt("语音转写", lambda: self._transcript(stage, start, end, asr)))
                if audio is not False:
                    output.extend(self._attempt("音频理解", lambda: self._describe_audio(
                        stage, start, end, self._use_heavy_model(audio))))
        return tuple(output)

    def _transcript(self, stage: Path, start: float, end: float, asr: Any) -> tuple[MediaEvidence, ...]:
        if self._mode == "asr":
            if self._route is None or not self._route.native_asr:
                spans = self._media_client("asr").transcribe(stage)
                return tuple(MediaEvidence(span.text, start + span.start_seconds,
                                           min(end, start + span.end_seconds), ("对白",), "transcript")
                             for span in spans if 0.0 <= span.start_seconds < end - start
                             and span.start_seconds < span.end_seconds and span.text.strip())
            self._authorized_route("asr")
        from src.features.edit.ai_editing.transcript_provider import ModelPackNotInstalledError as AsrModelUnavailable

        stat = stage.stat()
        try:
            analysis, _ = asr.analyze(
                str(stage), AssetFingerprint(size=stat.st_size, mtime_ns=stat.st_mtime_ns),
                fps=f"{_TRANSCRIPT_CLOCK}/1",
                options={"model_size": "large" if self._asr_pack.endswith("_large") else "small",
                         "timestamps": "segment", "speakers": False},
            )
        except AsrModelUnavailable as error:
            raise _ModelUnavailable(str(error)) from error
        output: list[MediaEvidence] = []
        for line in analysis.transcript:
            text = line.text.strip()
            first = start + line.start_frame / _TRANSCRIPT_CLOCK
            last = min(end, start + line.end_frame / _TRANSCRIPT_CLOCK)
            if text and start <= first < last:
                output.append(MediaEvidence(text, first, last, ("对白",), "transcript"))
        return tuple(output)

    @staticmethod
    def _describe_audio(stage: Path, start: float, end: float, provider: Any) -> tuple[MediaEvidence, ...]:
        observed = provider.understand_audio(str(stage))
        tags = tuple(value for value in (observed.vocal_type, observed.mood, *observed.instruments) if value)
        text = observed.description.strip()
        return (MediaEvidence(text, start, end, tags, "audio"),) if text else ()


@contextmanager
def _open_media(path: str) -> Iterator[Any]:
    import av

    try:
        with av.open(path, options={"protocol_whitelist": "file,crypto,data"}) as media:
            yield media
    except av.error.FFmpegError as error:
        raise RuntimeError(f"媒体解码失败：{error.strerror or type(error).__name__}") from error


def _probe_timing(path: str, kind: str) -> _MediaTiming:
    import av

    # AV_TIME_BASE units are indexed by the container when its stream does
    # not declare its own duration.
    with _open_media(path) as media:
        if {"hls", "concat", "dash"} & set(media.format.name.split(",")):
            raise RuntimeError("请提供本地媒体文件，不支持包含其他媒体地址的播放列表")
        streams = media.streams.video if kind == "video" else media.streams.audio
        if not streams:
            raise RuntimeError("素材不包含可分析的媒体流")
        stream = streams[0]
        start = float(stream.start_time * stream.time_base) if stream.start_time is not None else 0.0
        duration = (float(stream.duration * stream.time_base) if stream.duration and stream.time_base
                    else float(media.duration / av.time_base) if media.duration else 0.0)
        if not math.isfinite(duration) or duration <= 0:
            raise RuntimeError("无法获得素材时长")
        return _MediaTiming(start, start + duration, bool(media.streams.audio))


def _sample_shot(video: Any, stream: Any, offset: float, shot: _Shot,
                 check_cancel: Callable[[], None]) -> Any:
    target = offset + (shot.start + shot.end) / 2
    video.seek(int(target / stream.time_base), stream=stream, backward=True)
    previous = None
    for frame in video.decode(stream):
        check_cancel()
        if frame.time is None or frame.time < offset + shot.start:
            continue
        if frame.time >= offset + shot.end:
            break
        if frame.time >= target:
            return frame.to_image()
        previous = frame
    return previous.to_image() if previous is not None else None


def _audio_windows(path: str, origin: float,
                   check_cancel: Callable[[], None]) -> Iterator[tuple[float, bytes]]:
    import av

    block = _AUDIO_WINDOW * _AUDIO_RATE * 2
    pcm = bytearray()
    with _open_media(path) as source:
        stream = source.streams.audio[0]
        start = (float(stream.start_time * stream.time_base) if stream.start_time is not None else origin) - origin
        resampler = av.AudioResampler(format="s16", layout="mono", rate=_AUDIO_RATE)

        def frames() -> Iterator[Any]:
            for frame in source.decode(stream):
                check_cancel()
                yield from resampler.resample(frame)
            yield from resampler.resample(None)

        for frame in frames():
            check_cancel()
            samples = frame.to_ndarray().astype("<i2", copy=False).tobytes()
            timestamp = frame.time - origin if frame.time is not None else start + len(pcm) / (2 * _AUDIO_RATE)
            if abs(timestamp - start - len(pcm) / (2 * _AUDIO_RATE)) > 0.04:
                if pcm:
                    yield start, bytes(pcm)
                    pcm.clear()
                start = timestamp
            if timestamp < 0.0:
                samples = samples[2 * math.ceil(-timestamp * _AUDIO_RATE):]
                start = 0.0
            pcm.extend(samples)
            while len(pcm) >= block:
                yield start, bytes(pcm[:block])
                del pcm[:block]
                start += _AUDIO_WINDOW
        if pcm:
            yield start, bytes(pcm)
