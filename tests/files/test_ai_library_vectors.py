"""Semantic search is a resumable derived index over authorized, live evidence."""
from collections.abc import Iterator, Sequence
from pathlib import Path
from threading import Event

import pytest

from src.features.files.repository.catalog import FilesCatalog
from src.features.files.repository.locator import FilesDataLocator
from src.features.files.services.ai_library import AILibraryService, MediaEvidence
from src.features.files.services.ai_library_vectors import BATCH_SIZE


@pytest.fixture()
def catalog(tmp_path: Path) -> Iterator[FilesCatalog]:
    catalog = FilesCatalog(FilesDataLocator.for_config_root(tmp_path / "config"))
    try:
        yield catalog
    finally:
        catalog.close()


def _media(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_bytes(name.encode("utf-8"))
    return path


def test_persistent_semantic_search_fuses_timed_evidence_with_keywords(
    catalog: FilesCatalog, tmp_path: Path,
) -> None:
    directory = tmp_path / "library"
    directory.mkdir()
    library_id = catalog.register_library(directory).library_id
    bird = _media(directory, "wild.wav")
    river = _media(directory, "water.wav")
    catalog.upsert_asset(library_id, bird)
    catalog.patch_user_fields(library_id, bird, comment="private editorial note")
    service = AILibraryService(catalog)
    service.build_index(lambda path, kind: (
        MediaEvidence("A bird sings", 1.25, 2.5, ("forest",), "audio") if Path(path) == bird
        else MediaEvidence("Flowing river", 4.0, 7.0, modality="audio"),
        MediaEvidence("Local OCR should not be sent", modality="ocr"),
    ))
    submitted: list[str] = []

    def embed(texts: Sequence[str]) -> list[list[float]]:
        submitted.extend(texts)
        return [[2.0, 1.0] if "bird" in text else [1.0, 3.0] for text in texts]

    assert service.build_embeddings(embed, model_key="bridge:one")["embedded"] == 2
    assert set(submitted) == {"A bird sings\nforest", "Flowing river"}
    reopened = AILibraryService(catalog)
    assert reopened.embedding_status("bridge:one") == {"eligible": 2, "embedded": 2, "pending": 0}
    assert reopened.search("stream") == []
    assert reopened.search("stream", embedding=("bridge:one", (0.1, 1.0)))[0]["path"] == str(river)
    assert reopened.search("stream", embedding=("bridge:two", (0.1, 1.0))) == []
    hits = reopened.search("bird", embedding=("bridge:one", (0.1, 1.0)))
    assert [hit["path"] for hit in hits] == [str(bird), str(river)]
    assert (hits[0]["start_seconds"], hits[0]["end_seconds"], hits[0]["modality"]) == (1.25, 2.5, "audio")
    assert reopened.search("bird", embedding=("bridge:one", (float("nan"), 1.0))) == reopened.search("bird")


def test_vectors_preserve_previous_batches_when_later_inference_fails_or_is_cancelled(
    catalog: FilesCatalog, tmp_path: Path,
) -> None:
    directory = tmp_path / "library"
    directory.mkdir()
    catalog.register_library(directory)
    for number in range(BATCH_SIZE + 1):
        _media(directory, f"clip-{number}.png")
    service = AILibraryService(catalog)
    service.build_index(lambda path, kind: (MediaEvidence(f"recording {Path(path).stem}"),))
    calls: list[int] = []
    progress: list[tuple[int, int]] = []

    def fail_second(texts: Sequence[str]) -> list[list[float]]:
        calls.append(len(texts))
        if len(calls) > 1:
            raise RuntimeError("test model stopped")
        return [[2.0, 3.0] for _ in texts]

    report = service.build_embeddings(
        fail_second, model_key="chosen", progress=lambda done, total: progress.append((done, total)),
    )
    assert calls == [BATCH_SIZE, 1]
    assert progress == [(0, BATCH_SIZE + 1), (BATCH_SIZE, BATCH_SIZE + 1)]
    assert report["embedded"] == BATCH_SIZE and report["error"] == "test model stopped"
    assert service.embedding_status("chosen")["pending"] == 1

    cancel = Event()

    def cancelled_batch(texts: Sequence[str]) -> list[list[float]]:
        assert len(texts) == 1
        cancel.set()
        return [[2.0, 3.0]]

    assert service.build_embeddings(cancelled_batch, model_key="chosen", cancel=cancel)["cancelled"]
    assert service.embedding_status("chosen")["pending"] == 1
    assert service.build_embeddings(lambda texts: [[2.0, 3.0]], model_key="chosen")["embedded"] == 1
    assert service.embedding_status("new-identity")["pending"] == BATCH_SIZE + 1


def test_force_and_source_changes_retire_obsolete_vector_hits(
    catalog: FilesCatalog, tmp_path: Path,
) -> None:
    directory = tmp_path / "library"
    directory.mkdir()
    catalog.register_library(directory)
    media = _media(directory, "scene.mov")
    service = AILibraryService(catalog)
    service.build_index(lambda path, kind: (MediaEvidence("Original frame", 1.0, 2.0),))
    service.build_embeddings(lambda texts: [[1.0, 0.0]], model_key="semantic")
    assert service.search("old color", embedding=("semantic", (1.0, 0.0)))
    service.build_index(lambda path, kind: (MediaEvidence("Replacement frame", 5.0, 6.0),), force=True)
    assert service.search("old color", embedding=("semantic", (1.0, 0.0))) == []
    with catalog._read() as cursor:
        assert cursor.execute("SELECT COUNT(*) FROM ai_library_evidence_vectors").fetchone()[0] == 0
    media.write_bytes(b"changed source")
    submitted: list[str] = []

    def should_not_submit(texts: Sequence[str]) -> list[list[float]]:
        submitted.extend(texts)
        return [[1.0, 0.0]]

    assert service.build_embeddings(should_not_submit, model_key="semantic")["embedded"] == 0
    assert submitted == []


def test_semantic_authorization_intersects_library_folder_and_kind_filters(
    catalog: FilesCatalog, tmp_path: Path,
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    folder = first / "shots"
    folder.mkdir(parents=True)
    second.mkdir()
    first_id = catalog.register_library(first).library_id
    second_id = catalog.register_library(second).library_id
    selected = _media(folder, "selected.png")
    _media(first, "adjacent.png")
    _media(folder, "wrong-kind.mov")
    blocked = _media(second, "outside.png")
    service = AILibraryService(catalog)
    service.build_index(lambda path, kind: (MediaEvidence(Path(path).stem),))
    service.build_embeddings(lambda texts: [[1.0, 0.0] for _ in texts], model_key="local")
    embedding = ("local", (1.0, 0.0))
    relevant = service.search(
        "unrelated phrase", library_id=first_id, folder=str(folder), kinds=("image",),
        embedding=embedding, semantic_library_ids=frozenset({first_id, second_id}),
    )
    assert [hit["path"] for hit in relevant] == [str(selected)]
    assert not service.search("unrelated phrase", embedding=embedding, semantic_library_ids=frozenset())
    allowed = service.search("outside", embedding=embedding, semantic_library_ids=frozenset({first_id}))
    assert any(hit["path"] == str(blocked) for hit in allowed)
    semantic_only = service.search("unrelated phrase", embedding=embedding, semantic_library_ids=frozenset({first_id}))
    assert all(hit["library_id"] == first_id for hit in semantic_only)
    assert catalog.unregister_library(second_id)
    assert not service.search("unrelated phrase", embedding=embedding, semantic_library_ids=frozenset({second_id}))


def test_invalid_batch_does_not_write_inconsistent_embeddings(catalog: FilesCatalog, tmp_path: Path) -> None:
    directory = tmp_path / "library"
    directory.mkdir()
    catalog.register_library(directory)
    _media(directory, "first.png")
    _media(directory, "second.png")
    service = AILibraryService(catalog)
    service.build_index(lambda path, kind: (MediaEvidence("motion"),))
    failed = service.build_embeddings(lambda texts: [[1.0], [1.0, 2.0]], model_key="local")
    assert failed["error"] and failed["embedded"] == 0
    assert service.embedding_status("local")["pending"] == 2
    assert service.build_embeddings(lambda texts: [[1.0], [2.0]], model_key="local")["embedded"] == 2


def test_live_top_k_is_not_starved_by_unverified_higher_scoring_paths(
    catalog: FilesCatalog, tmp_path: Path,
) -> None:
    directory = tmp_path / "library"
    directory.mkdir()
    catalog.register_library(directory)
    valid = _media(directory, "live.mov")
    removed = [_media(directory, f"removed-{i}.mov") for i in range(12)]
    service = AILibraryService(catalog)
    service.build_index(lambda path, kind: (MediaEvidence(Path(path).stem),))
    service.build_embeddings(
        lambda texts: [[0.9, 0.2] if text == "live" else [1.0, 0.0] for text in texts],
        model_key="local",
    )
    for path in removed:
        path.unlink()
    result = service.search("subject", 1, embedding=("local", (1.0, 0.0)))
    assert [hit["path"] for hit in result] == [str(valid)]


def test_reanalysis_while_embedding_discards_the_obsolete_batch(
    catalog: FilesCatalog, tmp_path: Path,
) -> None:
    directory = tmp_path / "library"
    directory.mkdir()
    catalog.register_library(directory)
    _media(directory, "clip.mov")
    service = AILibraryService(catalog)
    service.build_index(lambda path, kind: (MediaEvidence("Previous description"),))

    def replace_evidence(texts: Sequence[str]) -> list[list[float]]:
        assert texts == ["Previous description"]
        service.build_index(lambda path, kind: (MediaEvidence("Current description"),), force=True)
        return [[1.0, 0.0]]

    report = service.build_embeddings(replace_evidence, model_key="local")
    assert report["embedded"] == 0 and report["pending"] == 1
    assert service.search("subject", embedding=("local", (1.0, 0.0))) == []
    assert service.search("current")[0]["text"] == "Current description"


def test_embedding_schema_upgrade_keeps_existing_evidence_and_registers_an_empty_cache(
    catalog: FilesCatalog, tmp_path: Path,
) -> None:
    directory = tmp_path / "library"
    directory.mkdir()
    catalog.register_library(directory)
    _media(directory, "clip.mov")
    old_service = AILibraryService(catalog)
    old_service.build_index(lambda path, kind: (MediaEvidence("Camera motion"),))
    with catalog._write() as cursor:
        cursor.execute("DROP TABLE ai_library_evidence_vectors")
        cursor.execute("UPDATE settings SET value = '2' WHERE library_id = '' AND key = 'ai_library_schema'")
    service = AILibraryService(catalog)
    assert service.search("camera")[0]["text"] == "Camera motion"
    assert service.embedding_status("local") == {"eligible": 1, "embedded": 0, "pending": 1}
    assert service.build_embeddings(lambda texts: [[1.0]], model_key="local")["embedded"] == 1
