"""Resolve and authorize the text embedding route for library workers."""
from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from src.shared.ai_models.generation.credential_store import (
    get_credential_storage_preference,
    make_credential_store,
)
from src.shared.ai_models.generation.media_inference import (
    MediaInferenceClient,
    MediaInferenceConfigurationError,
    MediaInferenceError,
    embedding_leaves_host,
    is_ollama_cloud_model,
    validate_media_endpoint,
)
from src.shared.ai_models.generation.provider_profiles import AiProviderProfile, ProfileRegistry

if TYPE_CHECKING:
    from src.features.files.services.facade import FilesFacade

_MAX_QUERY_SECONDS = 8.0


class EmbeddingAuthorizationError(MediaInferenceError):
    """The configured recipient changed or the directory no longer authorizes it."""


@dataclass(frozen=True)
class EmbeddingRoute:
    profile: AiProviderProfile
    origin: str
    requires_upload: bool
    approval: str

    @property
    def revision(self) -> str:
        return f"library-text-embedding-v1:{self.approval}"

    def summary(self) -> dict[str, str | bool]:
        return {
            "profile_id": self.profile.profile_id,
            "name": self.profile.display_name,
            "model": self.profile.model_id,
            "origin": self.origin,
            "approval": self.approval,
            "upload": self.requires_upload,
            "upload_allowed": True,
        }


def active_library_embedding_route(registry: ProfileRegistry | None = None) -> EmbeddingRoute | None:
    """Find the active Embeddings profile without borrowing the chat profile."""
    if registry is None:
        registry = ProfileRegistry()
        registry.load_from_user_file()
    profile = registry.active_profile_for_capability("embedding")
    if profile is None or not MediaInferenceClient.supports(profile.provider_kind, "embedding"):
        return None
    model = profile.model_id.strip()
    if not model or len(model) > 512 or not model.isprintable():
        return None
    try:
        endpoint, loopback = validate_media_endpoint(profile.endpoint)
    except MediaInferenceConfigurationError:
        return None
    requires_upload = embedding_leaves_host(profile, loopback)
    identity = ("embedding", profile.profile_id, profile.provider_kind, endpoint.geturl(), model, requires_upload)
    approval = hashlib.sha256(repr(identity).encode("utf-8")).hexdigest()
    origin = f"{endpoint.scheme}://{endpoint.netloc}"
    if loopback and is_ollama_cloud_model(profile):
        origin = f"https://ollama.com via {origin}"
    return EmbeddingRoute(profile, origin, requires_upload, approval)


def _current_route(expected: EmbeddingRoute) -> EmbeddingRoute:
    current = active_library_embedding_route()
    if current is None or current.approval != expected.approval:
        raise EmbeddingAuthorizationError("向量模型或服务地址已变更，请重新确认后再试。")
    return current


def _scope_approved(facade: FilesFacade, library_id: str, folder: str | None, route: EmbeddingRoute) -> bool:
    try:
        return facade.ai_library_embedding_consent(library_id, folder=folder) == route.approval
    except (OSError, ValueError):
        return False


def _embed(route: EmbeddingRoute, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
    profile = route.profile
    key = None
    if profile.credential_ref:
        store = make_credential_store(get_credential_storage_preference())
        key = store.get_secret(profile.credential_ref)
    return MediaInferenceClient(profile, key).embed_texts(texts, allow_off_host=route.requires_upload)


@dataclass(frozen=True)
class LibraryEmbedder:
    """Embed corpus text inside exactly one saved remote scope or locally."""

    route: EmbeddingRoute
    _facade: FilesFacade
    _library_id: str | None
    _folder: str | None

    @property
    def revision(self) -> str:
        return self.route.revision

    def _authorized_route(self) -> EmbeddingRoute:
        route = _current_route(self.route)
        if route.requires_upload and (
            self._library_id is None or not _scope_approved(self._facade, self._library_id, self._folder, route)
        ):
            raise EmbeddingAuthorizationError("此素材库范围未授权向当前向量服务发送文本。")
        return route

    def is_authorized(self) -> bool:
        try:
            self._authorized_route()
        except EmbeddingAuthorizationError:
            return False
        return True

    def __call__(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        vectors = _embed(self._authorized_route(), texts)
        self._authorized_route()
        return vectors


def library_embedder(
    facade: FilesFacade, library_id: str | None, *, folder: str | None = None, route: EmbeddingRoute | None = None,
) -> LibraryEmbedder | None:
    """Create a corpus embedder. Global corpus writes can use a loopback model only."""
    resolved = route or active_library_embedding_route()
    if resolved is None or (library_id is None and (resolved.requires_upload or folder is not None)):
        return None
    return LibraryEmbedder(resolved, facade, library_id, folder)


@dataclass(frozen=True)
class LibraryQueryEmbedding:
    key: str
    vector: tuple[float, ...]
    library_ids: tuple[str, ...] | None


def library_query_embedding(
    facade: FilesFacade, text: str, library_id: str | None = None, *, folder: str | None = None,
    allow_remote: bool = False, route: EmbeddingRoute | None = None,
    cancel_check: Callable[[], bool] | None = None, timeout_seconds: float = _MAX_QUERY_SECONDS,
) -> LibraryQueryEmbedding | None:
    """Send the query once and return the permitted owners for semantic matching."""
    if not text.strip() or (library_id is None and folder is not None):
        return None
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or (cancel_check and cancel_check()):
        return None
    deadline = time.monotonic() + min(timeout_seconds, _MAX_QUERY_SECONDS)
    resolved = route or active_library_embedding_route()
    if resolved is None or (resolved.requires_upload and allow_remote is not True):
        return None
    current = _current_route(resolved)
    allowed: tuple[str, ...] | None = None
    if current.requires_upload:
        owners = (library_id,) if library_id is not None else tuple(
            record.library_id for record in facade.libraries.list_libraries()
        )
        allowed = tuple(owner for owner in owners if _scope_approved(facade, owner, folder, current))
        if not allowed:
            return None
    remaining = deadline - time.monotonic()
    if remaining < 0.1 or (cancel_check and cancel_check()):
        return None
    limits = replace(current.profile.limits, request_timeout_seconds=min(
        remaining, current.profile.limits.request_timeout_seconds,
    ))
    vector, = _embed(replace(current, profile=replace(current.profile, limits=limits)), (text,))
    if time.monotonic() >= deadline or (cancel_check and cancel_check()):
        return None
    current = _current_route(current)
    if allowed is not None:
        allowed = tuple(owner for owner in allowed if _scope_approved(facade, owner, folder, current))
    if allowed == () or time.monotonic() >= deadline or (cancel_check and cancel_check()):
        return None
    return LibraryQueryEmbedding(current.revision, vector, allowed)
