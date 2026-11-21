"""Shared retrieval path for the Files dialog and editing assistant."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from src.app.services.ai_library_embeddings import active_library_embedding_route, library_query_embedding
from src.features.files.services.facade import FilesFacade
from src.features.files.transactions import CancelToken
from src.shared.ai_models.generation.media_inference import MediaInferenceError
from src.shared.i18n.i18n import tr

logger = logging.getLogger(__name__)


class AssistantSearchCancel(CancelToken):
    """Expose the agent's cancellation/deadline to the shared Files workers."""

    def __init__(self, check: Callable[[], bool]) -> None:
        super().__init__()
        self._check = check

    @property
    def requested(self) -> bool:
        return super().requested or self._check()


def search_library(
    facade: FilesFacade, query: str, *, limit: int = 40, library_id: str | None = None,
    folder: str | None = None, kinds: tuple[str, ...] | None = None, recursive: bool = True,
    only_unanalyzed: bool = False, semantic: bool = True, allow_remote: bool = False,
    cancel: CancelToken | None = None,
) -> tuple[list[dict[str, Any]], str]:
    scope = {"library_id": library_id, "folder": folder, "kinds": kinds, "recursive": recursive, "cancel": cancel}
    embedding = None
    owners = None
    note = ""
    if semantic and not only_unanalyzed and query.strip() and not (cancel and cancel.requested):
        try:
            route = active_library_embedding_route()
            if route is None:
                note = tr("files.ai_library.keyword_fallback_model", "尚未配置向量模型，使用关键词搜索。")
            elif not facade.ai_library_embedding_status(route.revision, **scope)["embedded"]:
                note = tr("files.ai_library.keyword_fallback_empty", "当前范围还没有可用向量，使用关键词搜索。")
            elif not (cancel and cancel.requested):
                vector = library_query_embedding(facade, query[:1000], library_id, folder=folder,
                                                 allow_remote=allow_remote, route=route,
                                                 cancel_check=(lambda: cancel.requested) if cancel is not None else None)
                if vector is None and route.requires_upload:
                    note = tr("files.ai_library.keyword_fallback_consent", "当前范围未授权向量服务，使用关键词搜索。")
                elif vector is not None:
                    embedding = (vector.key, vector.vector)
                    owners = frozenset(vector.library_ids) if vector.library_ids is not None else None
                else:
                    note = tr("files.ai_library.keyword_fallback_offline", "向量服务暂不可用，使用关键词搜索。")
        except (MediaInferenceError, OSError, ValueError) as exc:
            logger.info("AI library semantic search unavailable (%s)", type(exc).__name__)
            note = tr("files.ai_library.keyword_fallback_offline", "向量服务暂不可用，使用关键词搜索。")
    hits = facade.search_ai_library(query, limit=limit, only_unanalyzed=only_unanalyzed,
                                   embedding=embedding, semantic_library_ids=owners, **scope)
    return hits, note
