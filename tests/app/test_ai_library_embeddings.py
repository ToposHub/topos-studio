from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.app.services import ai_library_embeddings as service
from src.shared.ai_models.generation.provider_profiles import AiProviderProfile, ProfileRegistry


class _ConsentCatalog:
    def __init__(self, *owners: str) -> None:
        self.grants: dict[tuple[str, str | None], str] = {}
        self.libraries = SimpleNamespace(list_libraries=lambda: [SimpleNamespace(library_id=owner) for owner in owners])

    def ai_library_embedding_consent(self, library_id: str, *, folder: str | None = None) -> str | None:
        return self.grants.get((library_id, folder))


def _profile(endpoint: str, *, kind: str = "openai", model: str = "embed-v1") -> AiProviderProfile:
    return AiProviderProfile("vectors", "Text Vectors", kind, endpoint, model, capabilities=("embedding",))


def _configure(monkeypatch: pytest.MonkeyPatch, profile: AiProviderProfile) -> ProfileRegistry:
    registry = ProfileRegistry()
    registry.add(profile)
    registry.set_active_for_capability("embedding", profile.profile_id)
    resolve = service.active_library_embedding_route
    monkeypatch.setattr(service, "active_library_embedding_route", lambda: resolve(registry))
    return registry


def test_only_the_active_embedding_profile_resolves() -> None:
    registry = ProfileRegistry()
    registry.add(AiProviderProfile("chat", "Chat", endpoint="https://never-contacted.invalid/v1", model_id="chat"))
    registry.set_active_for_capability("language", "chat")
    registry.add(_profile("http://localhost:11434", kind="ollama"))
    assert service.active_library_embedding_route(registry) is None

    registry.set_active_for_capability("embedding", "vectors")
    route = service.active_library_embedding_route(registry)
    assert route is not None and not route.requires_upload
    assert route.summary()["origin"] == "http://localhost:11434"

    registry.add(_profile("http://localhost:11434/api/embed", kind="ollama", model="embed:cloud"))
    remote = service.active_library_embedding_route(registry)
    assert remote is not None and remote.requires_upload
    assert "https://ollama.com" in remote.origin and remote.approval != route.approval


def test_remote_corpus_revalidates_scope_and_route_before_and_after_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _configure(monkeypatch, _profile("https://never-contacted.invalid/v1"))
    route = service.active_library_embedding_route()
    assert route is not None
    catalog = _ConsentCatalog("library")
    folder = "/example/library/selected"
    embedder = service.library_embedder(catalog, "library", folder=folder, route=route)
    assert embedder is not None and not embedder.is_authorized()
    assert service.library_embedder(catalog, None, route=route) is None
    calls: list[tuple[str, ...]] = []

    def embed(current: service.EmbeddingRoute, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        assert current.approval == route.approval
        calls.append(tuple(texts))
        catalog.grants.pop(("library", folder))
        return ((1.0, 0.0),)

    monkeypatch.setattr(service, "_embed", embed)
    catalog.grants["library", "/example/library/other"] = route.approval
    with pytest.raises(service.EmbeddingAuthorizationError):
        embedder(("text from synthetic media",))
    assert calls == []

    catalog.grants["library", folder] = route.approval
    assert embedder.is_authorized()
    with pytest.raises(service.EmbeddingAuthorizationError):
        embedder(("text from synthetic media",))
    assert calls == [("text from synthetic media",)]
    assert not embedder.is_authorized()

    catalog.grants["library", folder] = route.approval
    registry.add(_profile("https://different.invalid/v1"))
    with pytest.raises(service.EmbeddingAuthorizationError, match="已变更"):
        embedder(("text from synthetic media",))
    assert len(calls) == 1


def test_remote_query_requires_opt_in_and_only_uses_approved_owners(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, _profile("https://never-contacted.invalid/v1"))
    route = service.active_library_embedding_route()
    assert route is not None
    catalog = _ConsentCatalog("first", "revoked", "never-approved")
    catalog.grants["first", None] = route.approval
    catalog.grants["revoked", None] = route.approval
    calls: list[tuple[str, ...]] = []

    def query(_: service.EmbeddingRoute, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        calls.append(tuple(texts))
        catalog.grants.pop(("revoked", None))
        catalog.grants["never-approved", None] = route.approval
        return ((0.0, 1.0),)

    monkeypatch.setattr(service, "_embed", query)
    assert service.library_query_embedding(catalog, "synthetic query") is None
    assert service.library_query_embedding(catalog, "synthetic query", "never-approved", allow_remote=True) is None
    assert calls == []

    result = service.library_query_embedding(catalog, "synthetic query", allow_remote=True)
    assert result == service.LibraryQueryEmbedding(route.revision, (0.0, 1.0), ("first",))
    assert calls == [("synthetic query",)]


def test_loopback_query_and_corpus_use_configured_credentials_without_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = replace(_profile("http://127.0.0.1:12345/v1"), credential_ref="topos:test-embedder")
    _configure(monkeypatch, profile)
    route = service.active_library_embedding_route()
    assert route is not None and not route.requires_upload
    reads: list[str] = []
    requests: list[tuple[str, ...]] = []
    timeouts: list[float] = []

    class MockStore:
        def get_secret(self, credential_ref: str) -> str:
            reads.append(credential_ref)
            return "fixture-key"

    original_client = service.MediaInferenceClient

    class MockClient:
        supports = staticmethod(original_client.supports)

        def __init__(self, configured: AiProviderProfile, key: str) -> None:
            assert replace(configured, limits=profile.limits) == profile and key == "fixture-key"
            timeouts.append(configured.limits.request_timeout_seconds)

        def embed_texts(self, texts: Sequence[str], *, allow_off_host: bool) -> tuple[tuple[float, ...], ...]:
            assert allow_off_host is False
            requests.append(tuple(texts))
            return ((1.0, 1.0),)

    monkeypatch.setattr(service, "get_credential_storage_preference", lambda: "fixture-storage")
    monkeypatch.setattr(service, "make_credential_store", lambda _: MockStore())
    monkeypatch.setattr(service, "MediaInferenceClient", MockClient)
    catalog = _ConsentCatalog()
    embedder = service.library_embedder(catalog, None)
    assert embedder is not None and embedder.is_authorized()
    assert embedder(("synthetic media text",)) == ((1.0, 1.0),)
    assert service.library_query_embedding(catalog, "synthetic query", timeout_seconds=1.5) == service.LibraryQueryEmbedding(
        route.revision, (1.0, 1.0), None,
    )
    assert timeouts[0] == profile.limits.request_timeout_seconds
    assert 0.1 <= timeouts[1] <= 1.5
    assert reads == ["topos:test-embedder", "topos:test-embedder"]
    assert requests == [("synthetic media text",), ("synthetic query",)]


def test_query_rechecks_cancellation_after_authorizing_and_before_using_result(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, _profile("https://never-contacted.invalid/v1"))
    route = service.active_library_embedding_route()
    assert route is not None
    catalog = _ConsentCatalog("owner")
    cancelled = False
    sent: list[tuple[str, ...]] = []

    def grant_during_cancellation(_: str, *, folder: str | None = None) -> str:
        nonlocal cancelled
        cancelled = True
        return route.approval

    def embed_during_cancellation(_: service.EmbeddingRoute, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        nonlocal cancelled
        sent.append(tuple(texts))
        cancelled = True
        return ((0.0, 2.0),)

    monkeypatch.setattr(service, "_embed", embed_during_cancellation)
    with monkeypatch.context() as authorization:
        authorization.setattr(catalog, "ai_library_embedding_consent", grant_during_cancellation)
        assert service.library_query_embedding(
            catalog, "synthetic query", allow_remote=True, cancel_check=lambda: cancelled,
        ) is None
        assert not sent

    cancelled = False
    catalog.grants["owner", None] = route.approval
    assert service.library_query_embedding(
        catalog, "synthetic query", allow_remote=True, cancel_check=lambda: cancelled,
    ) is None
    assert sent == [("synthetic query",)]
