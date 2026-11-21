"""Derived text vectors for successful, currently bound AI library evidence."""
from __future__ import annotations

import heapq
from collections.abc import Callable, Sequence
from sqlite3 import Row
from typing import TYPE_CHECKING, Any

from src.features.files.repository.catalog import FilesCatalog

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

VECTOR_SCHEMA = """
    CREATE TABLE ai_library_evidence_vectors (
        model_key TEXT NOT NULL,
        evidence_id INTEGER NOT NULL REFERENCES ai_library_evidence(id) ON DELETE CASCADE,
        dimensions INTEGER NOT NULL,
        vector BLOB NOT NULL,
        PRIMARY KEY(model_key, evidence_id)
    ) WITHOUT ROWID
"""
VECTOR_EVIDENCE_INDEX = (
    "CREATE INDEX ai_library_evidence_vectors_evidence ON ai_library_evidence_vectors(evidence_id)"
)
BATCH_SIZE = 16
_READ_BATCH_SIZE = 256
_MAX_DIMENSIONS = 4096
_MAX_TEXT_LENGTH = 1500
_AI_MODALITIES = ("visual", "transcript", "audio")

EmbedTexts = Callable[[Sequence[str]], Sequence[Sequence[float]]]


def model_identity(model_key: str) -> str:
    if not isinstance(model_key, str) or not model_key.strip() or len(model_key) > 256:
        raise ValueError("素材向量模型标识无效")
    return model_key


def _unit_vector(values: Sequence[float]) -> NDArray[np.float32]:
    import numpy as np

    if not 1 <= len(values) <= _MAX_DIMENSIONS:
        raise ValueError("素材向量维度无效")
    vector = np.asarray(values, dtype=np.float64)
    if vector.ndim != 1 or not np.isfinite(vector).all():
        raise ValueError("素材向量须包含有限数值")
    scale = np.abs(vector).max()
    if scale == 0.0:
        raise ValueError("素材向量不能为零")
    vector = vector / scale
    return (vector / np.linalg.norm(vector)).astype("<f4")


def _eligible_evidence(scope: str) -> str:
    return f"""s.completed = 1 AND e.modality IN ({",".join("?" for _ in _AI_MODALITIES)})
               AND (trim(e.text) != '' OR trim(e.tag_search) != '')
               AND EXISTS (
                   SELECT 1 FROM ai_library_location_analyses b
                   JOIN ai_library_locations l ON l.path = b.path
                   JOIN ai_library_assets i ON i.asset_id = l.asset_id
                   WHERE b.snapshot_id = s.id AND l.online = 1 AND l.asset_id = e.asset_id AND {scope}
               )"""


def _row_text(row: Row) -> str:
    text, tags = row["text"].strip(), row["tag_search"].strip()
    return "\n".join(value for value in (text, tags) if value)[:_MAX_TEXT_LENGTH]


class EvidenceVectors:
    """Read and write derived vectors without holding SQLite across inference."""

    def __init__(self, catalog: FilesCatalog, scope: str, params: tuple[Any, ...], model_key: str) -> None:
        self._catalog = catalog
        self._eligible = _eligible_evidence(scope)
        self._params = (*_AI_MODALITIES, *params)
        self._model_key = model_identity(model_key)

    def status(self) -> tuple[int, int, int]:
        with self._catalog._read() as cursor:
            row = cursor.execute(
                f"""SELECT COUNT(*) AS eligible, COUNT(v.evidence_id) AS embedded, COALESCE(MAX(e.id), 0) AS last
                    FROM ai_library_evidence e JOIN ai_library_analysis_snapshots s ON s.id = e.snapshot_id
                    LEFT JOIN ai_library_evidence_vectors v ON v.evidence_id = e.id AND v.model_key = ?
                    WHERE {self._eligible}""",
                (self._model_key, *self._params),
            ).fetchone()
        return row["eligible"], row["embedded"], row["last"]

    def pending_after(self, after: int, maximum: int) -> list[Row]:
        with self._catalog._read() as cursor:
            return cursor.execute(
                f"""SELECT e.* FROM ai_library_evidence e
                    JOIN ai_library_analysis_snapshots s ON s.id = e.snapshot_id
                    WHERE e.id > ? AND e.id <= ? AND {self._eligible}
                      AND NOT EXISTS (SELECT 1 FROM ai_library_evidence_vectors v
                                      WHERE v.evidence_id = e.id AND v.model_key = ?)
                    ORDER BY e.id LIMIT ?""",
                (after, maximum, *self._params, self._model_key, BATCH_SIZE),
            ).fetchall()

    def embed_batch(
        self, rows: Sequence[Row], embed: EmbedTexts, *, cancelled: Callable[[], bool],
    ) -> int:
        if not rows or cancelled():
            return 0
        replies = embed([_row_text(row) for row in rows])
        if cancelled():
            return 0
        if len(replies) != len(rows):
            raise ValueError("素材向量数量与请求不一致")
        vectors = [_unit_vector(values) for values in replies]
        dimensions = vectors[0].size
        if any(vector.size != dimensions for vector in vectors):
            raise ValueError("同一模型返回了不同的素材向量维度")
        with self._catalog._write() as cursor:
            prior = cursor.execute(
                "SELECT dimensions FROM ai_library_evidence_vectors WHERE model_key = ? LIMIT 1",
                (self._model_key,),
            ).fetchone()
            if prior is not None and prior[0] != dimensions:
                raise ValueError("素材向量维度已变更，请重新保存模型配置")
            cursor.executemany(
                f"""INSERT OR IGNORE INTO ai_library_evidence_vectors
                    SELECT ?, e.id, ?, ? FROM ai_library_evidence e
                    JOIN ai_library_analysis_snapshots s ON s.id = e.snapshot_id
                    WHERE e.id = ? AND e.snapshot_id = ? AND e.text = ? AND e.tag_search = ? AND {self._eligible}""",
                ((self._model_key, dimensions, vector.tobytes(), row["id"], row["snapshot_id"],
                  row["text"], row["tag_search"], *self._params)
                 for row, vector in zip(rows, vectors, strict=True)),
            )
            return cursor.rowcount

    def nearest(
        self, values: Sequence[float], limit: int, *, cancelled: Callable[[], bool],
        live_evidence: Callable[[Sequence[Row]], set[int]],
    ) -> list[Row]:
        """Find the exact highest cosine scores in bounded memory and DB batches."""
        import numpy as np

        query = _unit_vector(values)
        dimensions = query.size
        best: list[tuple[float, int, Row]] = []
        after = 0
        while not cancelled():
            with self._catalog._read() as cursor:
                rows = cursor.execute(
                    f"""SELECT e.*, v.vector FROM ai_library_evidence_vectors v
                        JOIN ai_library_evidence e ON e.id = v.evidence_id
                        JOIN ai_library_analysis_snapshots s ON s.id = e.snapshot_id
                        WHERE v.model_key = ? AND v.evidence_id > ? AND v.dimensions = ?
                          AND {self._eligible}
                        ORDER BY v.evidence_id LIMIT ?""",
                    (self._model_key, after, dimensions, *self._params, _READ_BATCH_SIZE),
                ).fetchall()
            if not rows:
                break
            after = rows[-1]["id"]
            valid = [row for row in rows if len(row["vector"]) == dimensions * 4]
            if not valid:
                continue
            matrix = np.stack([np.frombuffer(row["vector"], dtype="<f4") for row in valid])
            scores = matrix @ query
            promising = sorted(
                ((float(score), -row["id"], row) for score, row in zip(scores, valid, strict=True)
                 if np.isfinite(score) and score > 0.0),
                reverse=True,
            )
            for start in range(0, len(promising), _READ_BATCH_SIZE // 4):
                if cancelled():
                    return []
                batch = promising[start:start + _READ_BATCH_SIZE // 4]
                if len(best) == limit:
                    batch = [entry for entry in batch if entry[:2] > best[0][:2]]
                    if not batch:
                        break
                live = live_evidence([row for _, _, row in batch])
                for entry in batch:
                    if entry[2]["id"] not in live:
                        continue
                    if len(best) < limit:
                        heapq.heappush(best, entry)
                    elif entry[:2] > best[0][:2]:
                        heapq.heapreplace(best, entry)
        return [] if cancelled() else [row for _, _, row in sorted(best, reverse=True)]
