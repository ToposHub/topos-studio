from __future__ import annotations

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
from typing import Any, Callable

from PyQt6.QtWidgets import QInputDialog
from pytest import MonkeyPatch
from pytestqt.qtbot import QtBot

from src.features.edit.ai_editing.ai_provider_catalog import get_preset
from src.features.edit.ui import ai_preferences_page
from src.features.edit.ui.ai_preferences_page import AiEditingPreferencePage
from src.shared.ai_models.generation import user_ai_config
from src.shared.ai_models.generation.credential_store import FileCredentialStore, HybridCredentialStore
from src.shared.ai_models.generation.provider_profiles import (
    AiProviderProfile,
    DataPolicy,
    FakeCredentialStore,
    ProfileRegistry,
    ProviderLimits,
)


class _Config(dict[str, Any]):
    def set(self, key: str, value: Any, auto_save: bool = False) -> None:
        self[key] = value


def test_model_defaults_and_keys_apply_and_rollback(qapp, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(user_ai_config, "USER_AI_CONFIG_PATH", tmp_path / "ai_profiles.json")
    registry = ProfileRegistry()
    for profile_id, capability in (("chat", "language"), ("speech", "asr"), ("vision", "vision_understanding")):
        registry.add(AiProviderProfile(
            profile_id, profile_id, endpoint="https://models.example.com/v1",
            model_id=profile_id, credential_ref=profile_id, capabilities=(capability,),
        ))
    registry.set_active("chat")
    registry.save_to_user_file()
    store = FakeCredentialStore()
    store.set_secret("chat", "existing")
    page = AiEditingPreferencePage(credential_store=store)
    widget = page.create_widget()
    config = _Config()
    page.load(config)
    for cap in ("vision", "asr"):
        combo = page._route_widgets[cap]
        combo.setCurrentIndex(combo.findData("speech" if cap == "asr" else cap))
    page._widgets["api_key"].setText("draft")
    page._widgets["category_tabs"].setCurrentIndex(1)
    assert page._current_profile_id == "vision"
    assert store.get_secret("chat") == "existing"

    page.apply(config)
    installed = ProfileRegistry()
    assert installed.load_from_user_file()
    assert installed.active_profile_id == "chat"
    assert installed.active_profile_for_capability("vision").profile_id == "vision"
    assert installed.active_profile_for_capability("asr").profile_id == "speech"
    assert store.get_secret("chat") == "draft"

    page.rollback()
    assert installed.load_from_user_file()
    assert installed.active_profile_id == "chat"
    assert installed.active_profile_for_capability("vision") is None
    assert store.get_secret("chat") == "existing"
    widget.close()


def test_full_modalities_round_trip_and_removed_route_stays_clear(qapp) -> None:
    preset = get_preset("volcengine-agent-plan-text")
    profile = AiProviderProfile(
        "multi", "Multi", preset.provider_kind, preset.endpoint, preset.model_id,
        modalities=preset.modalities, source_kind=preset.source_kind, preset_id=preset.preset_id,
        capabilities=preset.capabilities,
    )
    registry = ProfileRegistry()
    registry.add(profile)
    registry.set_active("multi")
    page = AiEditingPreferencePage(registry, FakeCredentialStore())
    widget = page.create_widget()
    page._widgets["display_name"].setText("Renamed")
    saved = registry.get("multi")
    assert set(saved.modalities) == set(profile.modalities)
    assert set(saved.capabilities) == set(profile.capabilities)
    assert saved.preset_id == preset.preset_id
    registry.add(replace(saved, capabilities=("language",)))
    assert registry.active_profile_for_capability("vision") is None
    registry.add(saved)
    assert registry.active_profile_for_capability("vision") is None
    widget.close()


def test_reselect_saved_preset_preserves_profile_and_allows_edits(
    qtbot: QtBot, tmp_path: Path, monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(user_ai_config, "USER_AI_CONFIG_PATH", tmp_path / "ai_profiles.json")
    registry = ProfileRegistry()
    local = get_preset("local-openai-compatible")
    flash = get_preset("deepseek-flash")
    assert local is not None and flash is not None
    for preset, profile_id in ((local, "local"), (flash, "flash")):
        registry.add(AiProviderProfile(
            profile_id, preset.label, preset.provider_kind, preset.endpoint, preset.model_id,
            source_kind=preset.source_kind, preset_id=preset.preset_id, credential_ref=profile_id,
            capabilities=preset.capabilities, modalities=preset.modalities,
        ))
    initial = registry.get("flash")
    assert initial is not None
    saved = replace(
        initial,
        display_name="My DeepSeek",
        endpoint="https://api.deepseek.com/v1",
        model_id="deepseek-v4-flash",
        capabilities=("language",),
        modalities=("media_upload", "text", "tool_call"),
        data_policy=DataPolicy(execution="cloud", allow_media_upload=True, retain_prompts=True),
        limits=ProviderLimits(max_context_tokens=1_000_000, request_timeout_seconds=50),
    )
    registry.add(saved)
    registry.set_active("flash")
    registry.save_to_user_file()
    store = FakeCredentialStore()
    store.set_secret("flash", "saved-key")
    config = _Config()
    page = AiEditingPreferencePage(credential_store=store)
    widget = page.create_widget()
    qtbot.addWidget(widget)
    page.load(config)
    picker = page._widgets["profile_picker"]
    with qtbot.assertNotEmitted(page._widgets["preset_id"].currentIndexChanged):
        for profile_id in ("flash", "local", "flash"):
            picker.setCurrentIndex(picker.findData(profile_id))

    assert page.registry.get("flash") == saved
    assert page._widgets["endpoint"].text() == saved.endpoint
    assert page._widgets["model_id"].text() == saved.model_id
    assert page._widgets["allow_media_upload"].isChecked()
    assert page._widgets["api_key"].text() == ""
    assert page._api_key_for(saved) == "saved-key"

    page._widgets["display_name"].setText("My DeepSeek Flash")
    page._widgets["model_id"].setText("deepseek-flash")
    page._widgets["request_timeout"].setValue(65)
    page.apply(config)
    widget.close()

    reopened = AiEditingPreferencePage(credential_store=store)
    reopened_widget = reopened.create_widget()
    qtbot.addWidget(reopened_widget)
    reopened.load(config)
    picker = reopened._widgets["profile_picker"]
    picker.setCurrentIndex(picker.findData("flash"))
    assert reopened.registry.get("flash") == replace(
        saved, display_name="My DeepSeek Flash", model_id="deepseek-flash",
        limits=replace(saved.limits, request_timeout_seconds=65),
    )
    assert reopened._widgets["request_timeout"].value() == 65
    assert reopened._api_key_for(reopened.registry.get("flash")) == "saved-key"
    reopened_widget.close()


def test_recipient_change_discards_draft_without_overwriting_shared_key(qapp, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(user_ai_config, "USER_AI_CONFIG_PATH", tmp_path / "ai_profiles.json")
    registry = ProfileRegistry()
    for profile_id in ("first", "second"):
        registry.add(AiProviderProfile(
            profile_id, profile_id, endpoint="https://original.example/v1", model_id="chat",
            credential_ref="shared", capabilities=("language",),
        ))
    registry.save_to_user_file()
    store = FakeCredentialStore()
    store.set_secret("shared", "saved")
    page = AiEditingPreferencePage(credential_store=store)
    widget = page.create_widget()
    cfg = _Config()
    page.load(cfg)

    page._widgets["api_key"].setText("original-draft")
    page._widgets["endpoint"].setText("https://new.example/v1")
    assert page._api_key_for(page.registry.get("first")) == ""
    assert page._widgets["api_key"].text() == ""
    page._widgets["api_key"].setText("new-draft")
    page.apply(cfg)

    first = page.registry.get("first")
    second = page.registry.get("second")
    assert first.credential_ref != second.credential_ref
    assert store.get_secret(first.credential_ref) == "new-draft"
    assert store.get_secret(second.credential_ref) == "saved"
    widget.close()


def test_hybrid_key_rollback_restores_original_backend(tmp_path) -> None:
    secure = FakeCredentialStore()
    file = FileCredentialStore(tmp_path / "profiles.json")
    store = HybridCredentialStore(secure, file, prefer="file")
    secure.set_secret("key", "secure-only")
    restore = store.snapshot_for_rollback("key")

    store.set_secret("key", "new-file-key")
    assert secure.get_secret("key") is None
    restore()

    assert secure.get_secret("key") == "secure-only"
    assert file.get_secret("key") is None


def test_switch_to_ollama_and_clear_language_route(qapp) -> None:
    page = AiEditingPreferencePage(credential_store=FakeCredentialStore())
    widget = page.create_widget()
    page._on_add_profile()
    combo = page._widgets["source_kind"]
    combo.setCurrentIndex(combo.findData("ollama"))
    current = page.registry.get(page._current_profile_id)
    assert current.provider_kind == "ollama"
    assert current.endpoint == "http://127.0.0.1:11434"
    assert current.model_id == ""
    assert current.preset_id == ""
    page.registry.set_active_for_capability("language", current.profile_id)
    page._capability_widgets["language"].setChecked(False)
    assert page.registry.active_profile_for_capability("language") is None
    assert page.registry.get(current.profile_id).routed_capabilities == ("service",)
    widget.close()


def test_connection_form_shows_fields_for_selected_source(qtbot) -> None:
    page = AiEditingPreferencePage(credential_store=FakeCredentialStore())
    widget = page.create_widget()
    qtbot.addWidget(widget)
    widget.resize(600, 460)
    widget.show()
    controls = page._widgets
    controls["category_tabs"].setCurrentIndex(2)
    controls["add"].click()
    source = controls["source_kind"]
    preset = controls["preset_id"]

    assert preset.isVisible() and not controls["provider_kind"].isVisible()
    source.setCurrentIndex(source.findData("local"))
    assert preset.currentData() == "local-qwen-asr"
    assert not controls["endpoint"].isVisible()
    assert not controls["api_key"].isVisible()
    assert not controls["discover_models"].isVisible()
    assert controls["model_id"].isVisible()
    assert controls["test_btn"].isVisible()

    source.setCurrentIndex(source.findData("custom"))
    protocol = controls["provider_kind"]
    assert protocol.isVisible() and not preset.isVisible()
    protocol.setCurrentIndex(protocol.findData("openai"))
    assert controls["endpoint"].isVisible() and controls["api_key"].isVisible()
    assert controls["discover_models"].isVisible()
    assert page.registry.get(page._current_profile_id).provider_kind == "openai"


def test_zhipu_mcp_profile_can_be_bound_tested_and_saved(
    qtbot: QtBot, tmp_path: Path, monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(user_ai_config, "USER_AI_CONFIG_PATH", tmp_path / "ai_profiles.json")
    calls: list[str] = []

    class VisionGateway:
        def __init__(self, *, api_key_provider: Callable[[], str], request_timeout: float) -> None:
            assert request_timeout > 0
            calls.append(api_key_provider())

        def test_vision(self) -> str:
            return "智谱视觉 MCP 测试成功"

        def shutdown(self) -> None:
            calls.append("closed")

    monkeypatch.setattr(ai_preferences_page, "McpVisionGateway", VisionGateway)
    store = FakeCredentialStore()
    page = AiEditingPreferencePage(credential_store=store)
    widget = page.create_widget()
    qtbot.addWidget(widget)
    page.load(_Config())
    controls = page._widgets
    controls["category_tabs"].setCurrentIndex(1)
    controls["add"].click()
    source = controls["source_kind"]
    source.setCurrentIndex(source.findData("preset"))
    preset = controls["preset_id"]
    preset.setCurrentIndex(preset.findData("zhipu-vision-mcp"))
    assert controls["endpoint"].isReadOnly() and controls["model_id"].isReadOnly()
    assert not page._capability_widgets["language"].isEnabled()
    assert page._route_widgets["language"].findData(page._current_profile_id) < 0
    controls["api_key"].setText("vision-key")
    controls["test_btn"].click()
    qtbot.waitUntil(lambda: page._test_worker is None)
    assert calls == ["vision-key", "closed"]
    route = page._route_widgets["vision"]
    route.setCurrentIndex(route.findData(page._current_profile_id))
    page.apply(_Config())

    saved = ProfileRegistry()
    assert saved.load_from_user_file()
    profile = saved.active_profile_for_capability("vision")
    assert profile is not None and profile.provider_kind == "zhipu_vision_mcp"
    assert not profile.allows_media_upload
    assert store.get_secret(profile.credential_ref) == "vision-key"


def test_preferences_discovery_and_vision_probe_over_http(qapp, qtbot, monkeypatch) -> None:
    requests: list[tuple[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append((self.path, None))
            self._reply({"data": [{"id": "lan-vision"}]})

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body))
            self._reply({"choices": [{"message": {"content": "Red"}}]})

        def _reply(self, result: dict[str, Any]) -> None:
            payload = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        registry = ProfileRegistry()
        registry.add(AiProviderProfile(
            "lan", "LAN", endpoint=f"http://127.0.0.1:{server.server_port}/v1",
            model_id="", capabilities=("vision",), modalities=("image",),
        ))
        page = AiEditingPreferencePage(registry, FakeCredentialStore())
        widget = page.create_widget()
        widget.show()
        page._widgets["category_tabs"].setCurrentIndex(1)
        monkeypatch.setattr(QInputDialog, "getItem", staticmethod(lambda *_: ("lan-vision", True)))

        page._on_discover_models()
        qtbot.waitUntil(lambda: registry.get("lan").model_id == "lan-vision", timeout=4000)
        page._on_test_connection()
        qtbot.waitUntil(lambda: page._test_worker is None, timeout=4000)

        assert requests[0][0] == "/v1/models"
        path, data = requests[1]
        assert path == "/v1/chat/completions"
        assert data["model"] == "lan-vision"
        assert any(part["type"] == "image_url" for part in data["messages"][-1]["content"])
        assert page._probe_results["lan", "vision"] == "视觉模型测试成功"
        widget.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_embedding_preset_discovers_tests_and_saves_a_default_route(
    qtbot: QtBot, tmp_path: Path, monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(user_ai_config, "USER_AI_CONFIG_PATH", tmp_path / "ai_profiles.json")
    requests: list[tuple[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append((self.path, None))
            self._reply({"models": [{"name": "local-text-vectors"}]})

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body))
            self._reply({"embeddings": [[0.25, 0.5]]})

        def _reply(self, result: dict[str, Any]) -> None:
            payload = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        registry = ProfileRegistry()
        registry.add(AiProviderProfile("chat", "Chat", "openai", "https://never-contacted.invalid/v1", "chat"))
        registry.set_active_for_capability("language", "chat")
        registry.save_to_user_file()
        config = _Config()
        store = FakeCredentialStore()
        page = AiEditingPreferencePage(credential_store=store)
        widget = page.create_widget()
        qtbot.addWidget(widget)
        widget.show()
        page.load(config)
        page._widgets["category_tabs"].setCurrentIndex(3)
        page._on_add_profile()
        profile_id = page._current_profile_id
        preset_picker = page._widgets["preset_id"]
        assert preset_picker.currentData() == "local-openai-compatible-embedding"
        assert page.registry.get(profile_id).endpoint == "http://127.0.0.1:8000/v1"
        preset_picker.setCurrentIndex(preset_picker.findData("ollama-embedding"))
        assert page.registry.get(profile_id).endpoint == "http://127.0.0.1:11434/api/embed"
        page._widgets["endpoint"].setText(f"http://127.0.0.1:{server.server_port}/api/embed")
        monkeypatch.setattr(QInputDialog, "getItem", staticmethod(lambda *_: ("local-text-vectors", True)))

        page._on_discover_models()
        qtbot.waitUntil(lambda: page.registry.get(profile_id).model_id == "local-text-vectors", timeout=4000)
        page._on_test_connection()
        qtbot.waitUntil(lambda: page._test_worker is None, timeout=4000)
        assert requests == [
            ("/api/tags", None),
            ("/api/embed", {"model": "local-text-vectors", "input": ["Topos embedding probe"], "truncate": False}),
        ]
        assert page._probe_results[profile_id, "embedding"] == "向量化模型测试成功"

        route = page._route_widgets["embedding"]
        route.setCurrentIndex(route.findData(profile_id))
        page.apply(config)

        reopened = AiEditingPreferencePage(credential_store=store)
        reopened_widget = reopened.create_widget()
        qtbot.addWidget(reopened_widget)
        reopened.load(config)
        assert reopened._route_widgets["embedding"].currentData() == profile_id
        active = reopened.registry.active_profile_for_capability("embedding")
        assert active is not None and active.provider_kind == "ollama" and active.model_id == "local-text-vectors"
        assert reopened._route_widgets["language"].currentData() == "chat"

        page.rollback()
        reopened.load(config)
        assert reopened._route_widgets["embedding"].currentData() == ""
        assert reopened._route_widgets["language"].currentData() == "chat"
        widget.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
