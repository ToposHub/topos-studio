from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from src.features.edit.ai_editing import assistant_vision_fallback as fallback
from src.features.edit.ai_editing.image_attachment import (
    FileAttachment,
    ImageAttachment,
    encode_bytes_to_data_url,
    message_content,
)
from src.features.edit.ai_editing.mcp_vision_gateway import ZHIPU_VISION_ORIGIN, ZHIPU_VISION_TOOL
from src.shared.ai_models.generation.provider_profiles import (
    ZHIPU_VISION_PROVIDER_KIND,
    AiProviderProfile,
    DataPolicy,
    FakeCredentialStore,
    ProfileRegistry,
)

_PRIMARY = AiProviderProfile("text", "Writer")
_MCP = AiProviderProfile(
    "vision", "Zhipu Vision MCP", provider_kind=ZHIPU_VISION_PROVIDER_KIND,
    endpoint=ZHIPU_VISION_ORIGIN, model_id=ZHIPU_VISION_TOOL, credential_ref="fixture-zhipu",
    modalities=("image", "media_upload"), data_policy=DataPolicy(execution="cloud", allow_media_upload=True),
)
_KEY = "fixture-key-local"


def _image(payload: bytes) -> ImageAttachment:
    return ImageAttachment(encode_bytes_to_data_url(b"\xff\xd8\xff" + payload))


def _video(path: Path) -> FileAttachment:
    path.write_bytes(b"\x00\x00\x00\x10ftypmp42video")
    return FileAttachment(str(path), mime="video/mp4", media_kind="video")


class _Gateway:
    paths: list[Path]
    calls: list[tuple[str, str, bytes]]
    closed: bool

    def __init__(self, **kwargs: Any) -> None:
        assert kwargs["api_key_provider"]() == _KEY
        self.paths = []
        self.calls = []
        self.closed = False

    def invoke(self, name: str, args: dict[str, str]) -> dict[str, Any]:
        path = Path(args.get("image_ref") or args["video_ref"])
        self.paths.append(path)
        self.calls.append((name, args["prompt"], path.read_bytes()))
        return {"ok": True, "analysis": f"A red desk next to a chair. {path} {_KEY}"}

    def shutdown(self) -> None:
        self.closed = True


@pytest.fixture
def route(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeCredentialStore, list[_Gateway]]:
    store = FakeCredentialStore()
    store.set_secret(_MCP.credential_ref, _KEY)
    gateways: list[_Gateway] = []

    def gateway_factory(**kwargs: Any) -> _Gateway:
        gateway = _Gateway(**kwargs)
        gateways.append(gateway)
        return gateway

    monkeypatch.setattr(fallback, "McpVisionGateway", gateway_factory)
    return store, gateways


def test_fallback_uses_only_explicit_selected_and_authorized_service() -> None:
    registry = ProfileRegistry()
    registry.add(_MCP)
    registry.add(replace(_MCP, profile_id="no-consent", data_policy=DataPolicy()))
    registry.add(replace(_MCP, profile_id="wrong-endpoint", endpoint="https://other.example"))
    assert fallback.resolve_mcp_vision_profile(registry) is None
    for profile_id in ("no-consent", "wrong-endpoint", "vision"):
        registry.set_active_for_capability("vision", profile_id)
        assert fallback.resolve_mcp_vision_profile(registry) == (
            _MCP if profile_id == "vision" else None
        )


def test_fallback_sends_only_unsupported_visual_attachments(tmp_path: Path) -> None:
    image = _image(b"visual bytes")
    video = _video(tmp_path / "video.mp4")
    audio = FileAttachment(str(tmp_path / "sound.wav"), mime="audio/wav")
    flash = replace(_PRIMARY, modalities=("text", "image", "tool_call"))
    assert fallback.required_mcp_attachments(_PRIMARY, (image, audio, video)) == (image, video)
    assert fallback.required_mcp_attachments(flash, (image, audio, video)) == (video,)
    assert fallback.required_mcp_attachments(flash, (object(),)) == ()


def test_fallback_stages_current_video_preserves_native_image_and_sanitizes(
    tmp_path: Path, route: tuple[FakeCredentialStore, list[_Gateway]],
) -> None:
    store, gateways = route
    old_video = _video(tmp_path / "historical.mp4")
    video = _video(tmp_path / "current.mp4")
    image = _image(b"native supported")
    flash = replace(_PRIMARY, modalities=("text", "image"))
    question = f"Compare the colors /Users/person/Desktop/private.png {_KEY}"
    messages = [
        {"role": "user", "content": message_content("Earlier question", (old_video,))},
        {"role": "assistant", "content": "Earlier answer"},
        {"role": "user", "content": message_content(question, (image, video))},
    ]
    original = copy.deepcopy(messages)

    result = fallback.prepare_vision_fallback_messages(
        messages, primary=flash, required=(video,), profile=_MCP, credential_store=store,
        prompt=question, cancelled=lambda: False,
    )

    assert messages == original
    gateway, = gateways
    call, = gateway.calls
    assert call[0] == "topos.mcp.analyze_video"
    assert "/Users/" not in call[1] and _KEY not in call[1]
    assert call[2] == Path(video.path).read_bytes()
    assert "file_path" not in repr(result)
    native_image = result[-1]["content"][1]
    assert native_image == message_content("", (image,))[0]
    evidence = result[-1]["content"][-1]["text"]
    assert "A red desk" in evidence and '"trust": "untrusted"' in evidence
    assert str(gateway.paths[0]) not in evidence and _KEY not in evidence
    assert gateway.closed and not gateway.paths[0].exists()


def test_fallback_recovers_current_image_even_when_budget_strips_its_parts(
    route: tuple[FakeCredentialStore, list[_Gateway]],
) -> None:
    store, gateways = route
    image = _image(b"current image")
    messages = [
        {"role": "user", "content": message_content("", (_image(b"old image"),))},
        {"role": "user", "content": "What color is this?"},
    ]
    ready = fallback.prepare_vision_fallback_messages(
        messages, primary=_PRIMARY, required=(image,), profile=_MCP, credential_store=store,
        prompt="What color is this?", cancelled=lambda: False,
    )
    result, = ready
    assert result["content"].startswith("What color is this?")
    assert "A red desk" in result["content"]
    assert "base64" not in result["content"]
    gateway, = gateways
    assert [call[2] for call in gateway.calls] == [b"\xff\xd8\xffcurrent image"]


def test_plain_chat_uses_no_mcp_for_unsupported_prior_attachment(
    route: tuple[FakeCredentialStore, list[_Gateway]],
) -> None:
    store, gateways = route
    messages = [
        {"role": "user", "content": message_content("", (_image(b"old image"),))},
        {"role": "user", "content": "Forget the previous photo."},
    ]
    ready = fallback.prepare_vision_fallback_messages(
        messages, primary=_PRIMARY, required=(), profile=None, credential_store=store,
        prompt=messages[-1]["content"], cancelled=lambda: False,
    )
    assert ready == [messages[-1]]
    assert not gateways


def test_attachment_only_turn_does_not_rewrite_earlier_user_message(
    route: tuple[FakeCredentialStore, list[_Gateway]],
) -> None:
    store, _gateways = route
    image = _image(b"fresh")
    messages = [
        {"role": "user", "content": "Earlier request."},
        {"role": "assistant", "content": "Earlier reply."},
        {"role": "user", "content": message_content("", (image,))},
    ]
    ready = fallback.prepare_vision_fallback_messages(
        messages, primary=_PRIMARY, required=(image,), profile=_MCP, credential_store=store,
        prompt="", cancelled=lambda: False,
    )
    assert ready[:2] == messages[:2]
    assert ready[2]["role"] == "user" and "A red desk" in ready[2]["content"]


def test_staging_rejects_oversized_video_without_sending_other_attachments(
    tmp_path: Path, route: tuple[FakeCredentialStore, list[_Gateway]],
) -> None:
    store, gateways = route
    video = _video(tmp_path / "oversize.mp4")
    with Path(video.path).open("ab") as out:
        out.truncate(8_000_001)
    with pytest.raises(fallback.VisionFallbackError):
        fallback.prepare_vision_fallback_messages(
            [], primary=_PRIMARY, required=(_image(b"small"), video), profile=_MCP,
            credential_store=store, prompt="Compare", cancelled=lambda: False,
        )
    assert gateways[0].closed and not gateways[0].calls


def test_cancel_and_revocation_prevent_any_upload(
    route: tuple[FakeCredentialStore, list[_Gateway]],
) -> None:
    store, gateways = route
    with pytest.raises(fallback.VisionFallbackError):
        fallback.prepare_vision_fallback_messages(
            [], primary=_PRIMARY, required=(_image(b"cancelled"),), profile=_MCP,
            credential_store=store, prompt="Analyze", cancelled=lambda: True,
        )
    assert not gateways

    permitted = iter((True, True, False))
    with pytest.raises(fallback.VisionFallbackError):
        fallback.prepare_vision_fallback_messages(
            [], primary=_PRIMARY, required=(_image(b"revoked"),), profile=_MCP,
            credential_store=store, prompt="Analyze", cancelled=lambda: False,
            still_authorized=lambda: next(permitted),
        )
    assert gateways[0].closed and not gateways[0].calls


def test_mcp_exception_does_not_expose_staged_path_or_secret(
    monkeypatch: pytest.MonkeyPatch, route: tuple[FakeCredentialStore, list[_Gateway]],
) -> None:
    store, gateways = route

    def failed(gateway: _Gateway, _name: str, args: dict[str, str]) -> dict[str, Any]:
        path = Path(args["image_ref"])
        gateway.paths.append(path)
        raise RuntimeError(f"{path}: not authorized for {_KEY}")

    monkeypatch.setattr(_Gateway, "invoke", failed)
    with pytest.raises(fallback.VisionFallbackError) as error:
        fallback.prepare_vision_fallback_messages(
            [], primary=_PRIMARY, required=(_image(b"provider error"),), profile=_MCP,
            credential_store=store, prompt="Analyze", cancelled=lambda: False,
        )
    gateway, = gateways
    path, = gateway.paths
    assert _KEY not in str(error.value) and str(path) not in str(error.value)
    assert not path.exists() and gateway.closed
