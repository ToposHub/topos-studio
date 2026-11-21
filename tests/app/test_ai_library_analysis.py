from __future__ import annotations

import sys
import wave
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from src.app.services import ai_library_analysis as adapter
from src.features.files.transactions import CancelToken


def test_image_keeps_caption_when_local_ocr_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "coffee.png"
    Image.new("RGB", (40, 20), "orange").save(source)
    analyzer = adapter.LibraryAnalyzer(mode="local_models")
    processed: list[Path] = []

    def caption(path: str) -> SimpleNamespace:
        processed.append(Path(path))
        with Image.open(path) as image:
            assert image.size == (40, 20)
        return SimpleNamespace(caption="橙色咖啡招牌", tags=["咖啡", "招牌"])

    def missing_ocr(_: adapter.LibraryAnalyzer) -> None:
        raise adapter._ModelUnavailable("OCR 模型未安装")

    analyzer.__dict__["_captioner"] = SimpleNamespace(caption_image=caption)
    monkeypatch.setattr(adapter.LibraryAnalyzer, "_ocr", property(missing_ocr))
    evidence = analyzer(str(source), "image")
    assert [item.text for item in evidence] == ["橙色咖啡招牌"]
    assert evidence[0].tags == ("咖啡", "招牌") and evidence[0].start_seconds is None
    assert analyzer.warnings == ["文字识别：OCR 模型未安装"]
    assert not analyzer.retryable_failure
    assert all(not path.exists() for path in processed)


def test_partial_inference_failure_needs_retry_until_next_item(tmp_path: Path) -> None:
    source = tmp_path / "image.png"
    Image.new("RGB", (4, 4)).save(source)
    analyzer = adapter.LibraryAnalyzer(mode="local_models")

    def broken_caption(_: str) -> SimpleNamespace:
        raise RuntimeError("本地推理失败")

    analyzer.__dict__["_captioner"] = SimpleNamespace(caption_image=broken_caption)
    analyzer.__dict__["_ocr"] = lambda _: SimpleNamespace(boxes=[], txts=["可复用文本"], scores=[1.0])
    evidence = analyzer(str(source), "image")
    assert [item.modality for item in evidence] == ["ocr"]
    assert analyzer.retryable_failure
    assert analyzer.retryable_errors == ["画面描述：本地推理失败"]

    analyzer.__dict__["_captioner"] = SimpleNamespace(
        caption_image=lambda _: SimpleNamespace(caption="测试图片", tags=[]),
    )
    assert analyzer(str(source), "image")
    assert not analyzer.retryable_failure and analyzer.warnings == []


def test_missing_models_allow_metadata_indexing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "image.png"
    Image.new("RGB", (4, 4)).save(source)

    def unavailable(_: adapter.LibraryAnalyzer) -> None:
        raise adapter._ModelUnavailable("未安装")

    monkeypatch.setattr(adapter.LibraryAnalyzer, "_captioner", property(unavailable))
    monkeypatch.setattr(adapter.LibraryAnalyzer, "_ocr", property(unavailable))
    analyzer = adapter.LibraryAnalyzer(mode="local_models")
    assert analyzer(str(source), "image") == ()
    assert len(analyzer.warnings) == 2 and not analyzer.retryable_failure


def test_audio_windows_preserve_dialogue_and_effect_offsets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("av")
    monkeypatch.setattr(adapter, "_AUDIO_WINDOW", 1)
    source = tmp_path / "door.wav"
    with wave.open(str(source), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x40" * 36000)

    analyzer = adapter.LibraryAnalyzer(mode="local_models")
    analyzer.__dict__["_asr_pack"] = "qwen3_asr"

    def transcribe(asset_id: str, fingerprint: object, *, fps: str, options: dict) -> tuple:
        assert fps == "1000/1" and options["speakers"] is False
        assert Path(asset_id).exists()
        return SimpleNamespace(transcript=[SimpleNamespace(text="你好", start_frame=100, end_frame=240)]), ()

    analyzer.__dict__["_asr"] = SimpleNamespace(analyze=transcribe)
    analyzer.__dict__["_audio_model"] = SimpleNamespace(
        understand_audio=lambda _: SimpleNamespace(description="门铃", instruments=[], mood=None, vocal_type="音效"),
    )
    evidence = analyzer(str(source), "audio")
    dialogue = [item for item in evidence if item.modality == "transcript"]
    sounds = [item for item in evidence if item.modality == "audio"]
    assert [item.start_seconds for item in dialogue] == pytest.approx([0.1, 1.1, 2.1])
    assert [item.end_seconds for item in dialogue] == pytest.approx([0.24, 1.24, 2.24])
    assert [item.end_seconds for item in sounds] == pytest.approx([1.0, 2.0, 2.25])
    assert all(item.tags == ("音效",) for item in sounds)


def test_video_uses_real_cut_detector_and_samples_each_shot(tmp_path: Path) -> None:
    av = pytest.importorskip("av")
    source = tmp_path / "color-switch.mp4"
    with av.open(str(source), mode="w") as media:
        stream = media.add_stream("mpeg4", rate=24)
        stream.width = 160
        stream.height = 120
        stream.pix_fmt = "yuv420p"
        for index in range(48):
            color = (50, 130, 220) if index < 24 else (230, 120, 40)
            frame = av.VideoFrame.from_image(Image.new("RGB", (160, 120), color))
            for packet in stream.encode(frame):
                media.mux(packet)
        for packet in stream.encode():
            media.mux(packet)

    analyzer = adapter.LibraryAnalyzer(mode="local_models")

    def caption(path: str) -> SimpleNamespace:
        with Image.open(path) as frame:
            name = "暗蓝画面" if frame.getpixel((0, 0))[0] < 100 else "明黄色画面"
        return SimpleNamespace(caption=name, tags=[])

    analyzer.__dict__["_captioner"] = SimpleNamespace(caption_image=caption)
    analyzer.__dict__["_ocr"] = lambda _: None
    evidence = analyzer(str(source), "video")
    assert [item.text for item in evidence] == ["暗蓝画面", "明黄色画面"]
    assert [item.start_seconds for item in evidence] == pytest.approx([0.0, 1.0])
    assert [item.end_seconds for item in evidence] == pytest.approx([1.0, 2.0])


def test_cancelled_analysis_does_not_read_or_load_models(tmp_path: Path) -> None:
    cancel = CancelToken()
    cancel.request()
    analyzer = adapter.LibraryAnalyzer(cancel=cancel)
    with pytest.raises(RuntimeError, match="已取消"):
        analyzer(str(tmp_path / "absent.jpg"), "image")
    assert "_packs" not in analyzer.__dict__ and analyzer.warnings == []


def test_basic_analysis_uses_only_light_local_ocr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image = tmp_path / "scan.png"
    Image.new("RGB", (20, 10), "white").save(image)
    video = tmp_path / "clip.mov"
    video.write_bytes(b"not decoded")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("basic analysis must not load or contact configured models")

    monkeypatch.setattr(adapter.LibraryAnalyzer, "_captioner", property(forbidden))
    monkeypatch.setattr(adapter, "_probe_timing", forbidden)
    monkeypatch.setattr(adapter, "active_media_routes", forbidden)
    analyzer = adapter.LibraryAnalyzer()
    analyzer.__dict__["_ocr"] = lambda _: SimpleNamespace(boxes=[], txts=["日期"], scores=[1.0])
    assert [item.modality for item in analyzer(str(image), "image")] == ["ocr"]
    assert analyzer(str(video), "video") == ()
    assert analyzer.warnings and not analyzer.retryable_failure


def test_configured_vision_requires_current_directory_consent_and_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.features.files.services.ai_library import LibraryAnalysisPolicy
    from src.shared.ai_models.generation import media_inference
    from src.shared.ai_models.generation.provider_profiles import AiProviderProfile, DataPolicy, ProfileRegistry

    source = tmp_path / "scan.png"
    Image.new("RGB", (10, 10)).save(source)
    registry = ProfileRegistry()

    def set_profile(endpoint: str, *, allowed: bool = True) -> None:
        profile = AiProviderProfile(
            "studio-vision", "Studio Vision", endpoint=endpoint, model_id="vision",
            capabilities=("vision",), modalities=("image", "media_upload"),
            data_policy=DataPolicy(execution="cloud", allow_media_upload=allowed),
        )
        registry.add(profile)
        registry.set_active_for_capability("vision", profile.profile_id)

    set_profile("https://vision.example/v1")
    read_routes = adapter.active_media_routes
    monkeypatch.setattr(adapter, "active_media_routes", lambda: read_routes(registry))
    route = adapter.active_media_routes()["vision"]
    policy = LibraryAnalysisPolicy("profiles")
    analyzer = adapter.LibraryAnalyzer(mode="vision", route=route, read_policy=lambda: policy)
    calls: list[str] = []

    class Client:
        supports = staticmethod(media_inference.MediaInferenceClient.supports)

        def __init__(self, profile: AiProviderProfile, key: str) -> None:
            calls.append(profile.endpoint)

        def analyze_image(self, path: str) -> tuple[str, tuple[str, ...]]:
            assert Path(path).is_file()
            return "照片中的咖啡杯", ("桌面静物",)

    monkeypatch.setattr(media_inference, "MediaInferenceClient", Client)
    with pytest.raises(RuntimeError, match="未允许"):
        analyzer(str(source), "image")
    assert not calls
    policy = LibraryAnalysisPolicy("profiles", (route.approval,))
    evidence = analyzer(str(source), "image")[0]
    assert evidence.text == "照片中的咖啡杯"
    assert evidence.tags == ("桌面静物",)
    set_profile("https://changed.example/v1")
    with pytest.raises(RuntimeError, match="发生变化"):
        analyzer(str(source), "image")
    set_profile("https://vision.example/v1", allowed=False)
    with pytest.raises(RuntimeError, match="未允许"):
        analyzer(str(source), "image")
    assert calls == ["https://vision.example/v1"]


def test_unsupported_protocol_is_not_offered_for_library_media() -> None:
    from src.shared.ai_models.generation.provider_profiles import AiProviderProfile, ProfileRegistry

    registry = ProfileRegistry()
    profile = AiProviderProfile(
        "visual", "Visual", provider_kind="openai_tts", endpoint="https://studio.example/v1",
        model_id="vision", capabilities=("vision",), modalities=("image",),
    )
    registry.add(profile)
    registry.set_active_for_capability("vision", profile.profile_id)
    assert adapter.active_media_routes(registry) == {}


def test_library_options_expose_inactive_compatible_models_by_kind() -> None:
    from src.shared.ai_models.generation.provider_profiles import AiProviderProfile, ProfileRegistry

    registry = ProfileRegistry()
    for profile_id, capability in (("vision-a", "vision"), ("vision-b", "vision"),
                                   ("speech", "asr"), ("chat", "language")):
        registry.add(AiProviderProfile(
            profile_id, profile_id, endpoint="http://127.0.0.1:9991/v1", model_id=profile_id,
            capabilities=(capability,),
        ))
    registry.set_active_for_capability("vision", "vision-a")
    options = {(item["kind"], item["profile_id"]): item for item in adapter.library_model_options(registry)}
    assert set(options) == {
        ("image", "vision-a"), ("image", "vision-b"), ("video", "vision-a"), ("video", "vision-b"),
        ("audio", "speech"),
    }
    assert options["image", "vision-a"]["default"] and options["video", "vision-a"]["default"]
    assert not options["image", "vision-b"]["default"]
    assert options["image", "vision-a"]["approval"] != options["video", "vision-a"]["approval"]
    assert adapter.active_media_routes(registry)["vision"].approval != options["image", "vision-a"]["approval"]


def test_explicit_route_checks_scope_kind_profile_and_approval_each_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from src.features.files.services.ai_library import LibraryAnalysisPolicy
    from src.shared.ai_models.generation.provider_profiles import AiProviderProfile, DataPolicy, ProfileRegistry

    registry = ProfileRegistry()

    def profile(endpoint: str) -> AiProviderProfile:
        return AiProviderProfile(
            "speech", "Speech model", endpoint=endpoint, model_id="speech-model", capabilities=("asr",),
            modalities=("asr", "media_upload"), data_policy=DataPolicy("cloud", True),
        )

    registry.add(profile("https://uncontacted.invalid/v1"))
    monkeypatch.setattr(adapter, "_load_profiles", lambda: registry)
    route = adapter.library_media_route("audio", "speech")
    assert route is not None
    policy = LibraryAnalysisPolicy("profiles", (route.approval,), (("audio", "speech"),), ("audio",))
    analyzer = adapter.LibraryAnalyzer(mode="asr", route=route, read_policy=lambda: policy, scope_path=str(tmp_path))
    assert analyzer.index_kinds == ("audio",)
    assert analyzer._authorized_route("asr") == route

    policy = LibraryAnalysisPolicy("profiles", (), (("audio", "speech"),), ("audio",))
    with pytest.raises(RuntimeError, match="未允许"):
        analyzer._authorized_route("asr")
    policy = LibraryAnalysisPolicy("profiles", (route.approval,), (("audio", "other"),), ("audio",))
    with pytest.raises(RuntimeError, match="素材类型或分析模型"):
        analyzer._authorized_route("asr")
    policy = LibraryAnalysisPolicy("profiles", (route.approval,), (("audio", "speech"),), ("video",))
    with pytest.raises(RuntimeError, match="素材类型或分析模型"):
        analyzer._authorized_route("asr")

    nested = tmp_path / "nested"
    nested.mkdir()
    source = nested / "speech.wav"
    source.touch()
    monkeypatch.setattr(adapter, "_probe_timing", lambda *_: adapter._MediaTiming(0.0, 1.0, True))
    monkeypatch.setattr(analyzer, "_audio", lambda *_: (
        adapter.MediaEvidence(analyzer._authorized_route("asr").profile.display_name),
    ))
    policy = LibraryAnalysisPolicy("profiles", (route.approval,), (("audio", "speech"),), ("audio",), False)
    with pytest.raises(RuntimeError, match="当前的分析范围"):
        analyzer(str(source), "audio")

    policy = LibraryAnalysisPolicy("profiles", (route.approval,), (("audio", "speech"),), ("audio",))
    registry.add(profile("https://changed.invalid/v1"))
    with pytest.raises(RuntimeError, match="地址发生变化"):
        analyzer._authorized_route("asr")


def test_verified_vault_model_adapters_do_not_use_remote_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.topos_vault.providers import model_loader
    from apps.topos_vault.providers.real_audio_understand import Qwen25OmniAudioProvider
    from apps.topos_vault.providers.real_visual_caption import Qwen3VLCaptioner

    requests: list[tuple[str, dict]] = []

    def load(path: str, **kwargs: object) -> object:
        requests.append((path, kwargs))
        return object()

    factory = SimpleNamespace(from_pretrained=load)
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        AutoProcessor=factory, Qwen3VLForConditionalGeneration=factory,
        Qwen2_5OmniForConditionalGeneration=factory,
    ))
    monkeypatch.setattr(model_loader, "resolve_torch_device", lambda: ("cpu", None))
    path = str(tmp_path)
    Qwen3VLCaptioner(local_model_path=path)._load_model()
    Qwen25OmniAudioProvider(local_model_path=path)._load_model()
    assert len(requests) == 4 and all(location == path for location, _ in requests)
    assert all(options["local_files_only"] is True for _, options in requests)
    assert requests[-1][1]["enable_audio_output"] is False


def test_audio_understanding_decodes_only_generated_text_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    import numpy as np
    from apps.topos_vault.providers.real_audio_understand import Qwen25OmniAudioProvider

    class Inputs(dict):
        input_ids = np.array([[1, 2]])

        def to(self, device: str) -> Inputs:
            assert device == "cpu"
            return self

    class Processor:
        @staticmethod
        def apply_chat_template(messages: list[dict], **options: object) -> str:
            assert messages[0]["content"][0]["type"] == "audio"
            return "prompt"

        def __call__(self, **options: object) -> Inputs:
            assert options["sampling_rate"] == 16000
            assert len(options["audio"]) == 1
            return Inputs(input_ids=Inputs.input_ids)

        @staticmethod
        def batch_decode(ids: np.ndarray, *, skip_special_tokens: bool) -> list[str]:
            assert ids.tolist() == [[3, 4]] and skip_special_tokens
            return ["情绪：宁静\n乐器：\n类型：音效\n描述：轻响的门铃"]

    def generate(**options: object) -> np.ndarray:
        assert options["return_audio"] is options["use_audio_in_video"] is False
        return np.array([[1, 2, 3, 4]])

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(no_grad=nullcontext))
    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(
        read=lambda _: (np.zeros((8, 2), dtype=np.float32), 16000),
    ))
    provider = Qwen25OmniAudioProvider()
    provider._loaded = True
    provider._model = {"model": SimpleNamespace(generate=generate), "processor": Processor(), "device": "cpu"}
    result = provider.understand_audio("door.wav")
    assert result.vocal_type == "音效" and result.description == "轻响的门铃"
