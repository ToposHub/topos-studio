"""The library index survives project changes, media moves and missing models."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Event

import pytest

from src.features.files.repository.catalog import FilesCatalog
from src.features.files.repository.locator import FilesDataLocator
from src.features.files.services.ai_library import AILibraryService, LibraryAnalysisPolicy, MediaEvidence


@pytest.fixture()
def library(tmp_path: Path) -> Iterator[tuple[FilesCatalog, Path, str]]:
    root = tmp_path / "media"
    root.mkdir()
    catalog = FilesCatalog(FilesDataLocator.for_config_root(tmp_path / "config"))
    record = catalog.register_library(root)
    try:
        yield catalog, root, record.library_id
    finally:
        catalog.close()


def test_timed_analyses_are_searchable_and_incremental(library: tuple[FilesCatalog, Path, str]) -> None:
    catalog, root, library_id = library
    path = root / "morning.mov"
    path.write_bytes(b"one shot")
    source = str(path.resolve())
    catalog.upsert_asset(library_id, source)
    catalog.patch_user_fields(library_id, source, comment="Hand-edited notes")
    tag = catalog.create_tag(library_id, "Approved")
    catalog.tag_asset(library_id, source, tag)
    calls: list[str] = []

    def analyze(path: str, kind: str) -> tuple[MediaEvidence, ...]:
        calls.append(kind)
        return (MediaEvidence("人物推开咖啡店的门", 3.0, 8.0, ("晨光",)),)

    service = AILibraryService(catalog)
    assert service.build_index(analyze)["indexed"] == 1
    assert service.build_index(analyze)["skipped"] == 1
    assert calls == ["video"]
    found = service.search("找一段人物推开咖啡店的门的镜头")
    assert [(item["path"], item["start_seconds"], item["end_seconds"]) for item in found] == [
        (source, 3.0, 8.0),
    ]
    assert service.search("晨光")[0]["asset_id"] == found[0]["asset_id"]
    assert found[0]["source_size"] == path.stat().st_size
    assert found[0]["source_mtime_ns"] == path.stat().st_mtime_ns
    assert service.search("Approved")[0]["start_seconds"] is None
    assert catalog.asset(library_id, source)["comment"] == "Hand-edited notes"
    assert AILibraryService(catalog).search("咖啡店")[0]["asset_id"] == found[0]["asset_id"]
    assert service.build_index(analyze, analyzer_revision="new-model")["indexed"] == 1
    assert len(service.search("咖啡店")) == 1


def test_displayed_approval_cannot_be_saved_after_folder_replacement(
    library: tuple[FilesCatalog, Path, str],
) -> None:
    catalog, root, library_id = library
    service = AILibraryService(catalog)
    child = root / "shoot"
    child.mkdir()
    stamp = service.analysis_scope_stamp(library_id, folder=str(child))
    assert stamp is not None
    policy = LibraryAnalysisPolicy("profiles", ("a" * 64,))
    service.set_analysis_policy(library_id, policy, folder=str(child), expected_scope_stamp=stamp)
    child.rename(root / "old-shoot")
    child.mkdir()
    with pytest.raises(ValueError, match="重新确认"):
        service.set_analysis_policy(library_id, policy, folder=str(child), expected_scope_stamp=stamp)
    assert not service.analysis_policy(library_id, folder=str(child)).approved_routes


def test_library_scope_applies_to_indexing_and_search(
    library: tuple[FilesCatalog, Path, str],
) -> None:
    catalog, parent, parent_id = library
    child = parent / "nested"
    child.mkdir()
    child_id = catalog.register_library(child).library_id
    first, second = parent / "bell-a.wav", child / "bell-b.wav"
    for path, scope in ((first, parent_id), (second, child_id)):
        path.write_bytes(path.name.encode())
        catalog.upsert_asset(scope, path)

    def analyze(path: str, kind: str) -> tuple[MediaEvidence, ...]:
        return (MediaEvidence("doorbell", 0.0, 2.0),)

    service = AILibraryService(catalog)
    assert service.build_index(analyze, library_id=parent_id)["indexed"] == 1
    assert service.search("doorbell", library_id=child_id) == []
    assert service.build_index(analyze, library_id=child_id)["indexed"] == 1
    assert [hit["path"] for hit in service.search("doorbell", library_id=child_id)] == [str(second)]
    assert {hit["path"] for hit in service.search("doorbell")} == {str(first), str(second)}
    assert service.build_index(analyze, library_id=parent_id)["offline"] == 0

    assert catalog.unregister_library(child_id)
    assert service.search("doorbell", library_id=child_id) == []
    assert [hit["path"] for hit in service.search("doorbell")] == [str(first)]
    assert catalog.search("bell")[1] == 1
    assert second.is_file()
    with pytest.raises(ValueError, match="已移除"):
        service.build_index(analyze, library_id=child_id)


def test_offline_and_relocated_content_keeps_stable_identity(library: tuple[FilesCatalog, Path, str]) -> None:
    catalog, root, _ = library
    original = root / "sound.wav"
    original.write_bytes(b"bell")
    duplicate = root / "duplicate.wav"
    duplicate.write_bytes(b"bell")
    service = AILibraryService(catalog)

    def analyze(path: str, kind: str) -> tuple[MediaEvidence, ...]:
        return (MediaEvidence("doorbell", 0.0, 2.0, modality="audio"),)

    report = service.build_index(analyze)
    assert (report["indexed"], report["skipped"]) == (1, 1)
    before = service.search("doorbell")
    assert len(before) == 1 and before[0]["online"]
    original.unlink()
    offline_root = root.rename(root.with_name("disconnected"))
    assert service.build_index(analyze)["offline"] == 2
    offline = service.search("doorbell")[0]
    assert not offline["online"] and offline["asset_id"] == before[0]["asset_id"]
    offline_root.rename(root)
    moved = duplicate.rename(root / "bell-renamed.wav")
    assert service.build_index(analyze)["indexed"] == 0
    online = service.search("doorbell")[0]
    assert online["online"] and online["asset_id"] == before[0]["asset_id"]
    assert Path(online["path"]).samefile(moved)


def test_provider_failure_and_cancellation_can_be_retried(library: tuple[FilesCatalog, Path, str]) -> None:
    catalog, root, _ = library
    (root / "cup.png").write_bytes(b"one photo")
    service = AILibraryService(catalog)

    def unavailable(path: str, kind: str) -> tuple[MediaEvidence, ...]:
        raise RuntimeError("visual model unavailable")

    failed = service.build_index(unavailable)
    assert failed["failed"] == 1
    assert failed["errors"][0]["message"] == "visual model unavailable"
    assert service.search("cup")[0]["modality"] == "metadata"

    token = Event()

    def cancel_after_analysis(path: str, kind: str) -> tuple[MediaEvidence, ...]:
        token.set()
        return (MediaEvidence("wrong label"),)

    assert service.build_index(cancel_after_analysis, cancel=token)["cancelled"]
    assert service.search("wrong label") == []
    assert service.build_index(lambda path, kind: (MediaEvidence("Coffee cup"),))["indexed"] == 1
    assert service.search("Coffee")[0]["text"] == "Coffee cup"


def test_cancellation_between_files_prevents_starting_next_hash(
    library: tuple[FilesCatalog, Path, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.features.files.services import ai_library

    catalog, root, _ = library
    (root / "1.mov").write_bytes(b"first")
    (root / "2.mov").write_bytes(b"second")
    hashed = []
    hash_file = ai_library._asset_identity

    def track_hash(path: str, kind: str, cancel: Event) -> str:
        hashed.append(Path(path).name)
        return hash_file(path, kind, cancel)

    monkeypatch.setattr(ai_library, "_asset_identity", track_hash)
    token = Event()

    def progress(done: int, total: int) -> None:
        if done == 1:
            token.set()

    service = AILibraryService(catalog)
    report = service.build_index(lambda path, kind: (MediaEvidence("keep the first result"),),
                                 cancel=token, progress=progress)
    assert report["cancelled"] and report["indexed"] == 1
    assert hashed == ["1.mov"]
    assert [item["name"] for item in service.search("")] == ["1.mov"]


class _AnalysisAttempt:
    def __init__(
        self, evidence: tuple[MediaEvidence, ...], warnings: tuple[str, ...] = (),
        *, retryable: bool | None = None,
    ) -> None:
        self.evidence = evidence
        self.warnings = warnings
        self.retryable_failure = retryable

    def __call__(self, path: str, kind: str) -> tuple[MediaEvidence, ...]:
        return self.evidence


def test_partial_runtime_failure_preserves_and_retries_previous_snapshot(
    library: tuple[FilesCatalog, Path, str],
) -> None:
    catalog, root, _ = library
    (root / "scene.mov").write_bytes(b"scene")
    service = AILibraryService(catalog)
    original = _AnalysisAttempt((MediaEvidence("original caption", 0.0, 3.0),
                                 MediaEvidence("original transcript", 0.0, 3.0, modality="transcript")))
    assert service.build_index(original, analyzer_revision="initial")["indexed"] == 1

    partial = _AnalysisAttempt((MediaEvidence("partial caption", 0.0, 3.0),),
                               ("audio inference failed", "audio inference failed"), retryable=True)
    for _ in range(2):
        report = service.build_index(partial, analyzer_revision="update")
        assert report["failed"] == 1 and report["skipped"] == 0
        assert [warning["message"] for warning in report["warnings"]] == ["audio inference failed"]
        assert service.search("partial") == []
        assert service.search("original transcript")[0]["modality"] == "transcript"

    corrected = _AnalysisAttempt((MediaEvidence("completed caption", 0.0, 3.0),), retryable=False)
    assert service.build_index(corrected, analyzer_revision="update")["indexed"] == 1
    assert service.search("completed caption")[0]["text"] == "completed caption"


def test_empty_unverified_analysis_does_not_erase_existing_evidence(
    library: tuple[FilesCatalog, Path, str],
) -> None:
    catalog, root, _ = library
    (root / "scene.mov").write_bytes(b"scene")
    service = AILibraryService(catalog)
    first = _AnalysisAttempt((MediaEvidence("street footage"),))
    assert service.build_index(first, analyzer_revision="initial")["indexed"] == 1
    assert service.build_index(_AnalysisAttempt(()), analyzer_revision="update")["failed"] == 1
    assert service.search("street footage")[0]["text"] == "street footage"
    assert service.build_index(first, analyzer_revision="update")["indexed"] == 1


def test_missing_models_index_metadata_and_report_each_distinct_warning_once(
    library: tuple[FilesCatalog, Path, str],
) -> None:
    catalog, root, _ = library
    service = AILibraryService(catalog)
    for i in range(12):
        (root / f"cup-{i:02d}.png").write_bytes(bytes([i]))

    class MissingModels:
        retryable_failure = False
        warnings: tuple[str, ...] = ()

        def __call__(self, path: str, kind: str) -> tuple[MediaEvidence, ...]:
            warnings = ("caption model unavailable",) * 2
            if Path(path).name == "cup-11.png":
                warnings += ("OCR model unavailable",)
            self.warnings = warnings
            return ()

    unavailable = MissingModels()
    report = service.build_index(unavailable, analyzer_revision="missing")
    assert (report["metadata_only"], report["failed"], report["indexed"]) == (12, 0, 0)
    assert [warning["message"] for warning in report["warnings"]] == [
        "caption model unavailable", "OCR model unavailable",
    ]
    hits = service.search("cup")
    assert len(hits) == 12
    assert all(hit["text"] == "" and hit["modality"] == "metadata" for hit in hits)
    assert service.build_index(unavailable, analyzer_revision="missing")["skipped"] == 12
    available = _AnalysisAttempt((MediaEvidence("cafe photo"),), retryable=False)
    assert service.build_index(available, analyzer_revision="installed")["indexed"] == 12
    assert len(service.search("cafe photo")) == 12


def test_directory_policy_defaults_local_and_reuses_other_completed_scopes(
    library: tuple[FilesCatalog, Path, str],
) -> None:
    catalog, root, library_id = library
    (root / "clip.mov").write_bytes(b"one")
    service = AILibraryService(catalog)
    assert service.analysis_policy(library_id) == LibraryAnalysisPolicy()
    authorized = LibraryAnalysisPolicy("profiles", ("a" * 64,))
    service.set_analysis_policy(library_id, authorized)
    assert AILibraryService(catalog).analysis_policy(library_id) == authorized
    calls: list[str] = []

    class Analyzer:
        def __init__(self, scope: str, modality: str, revision: str) -> None:
            self.index_scope = scope
            self.index_modalities = (modality,)
            self.revision = revision

        def __call__(self, path: str, kind: str) -> tuple[MediaEvidence, ...]:
            calls.append(self.index_scope)
            return (MediaEvidence(self.revision, modality=self.index_modalities[0]),)

    vision = Analyzer("vision", "visual", "caption1")

    def scoped(library_id: str) -> tuple[Analyzer, ...]:
        return (vision,) if service.analysis_policy(library_id).mode == "profiles" else ()

    service.build_index(Analyzer("basic", "ocr", "text1"), additional_analyzers=scoped)
    service.set_analysis_policy(library_id, LibraryAnalysisPolicy())
    service.build_index(Analyzer("basic", "ocr", "text2"), additional_analyzers=scoped)
    assert service.search("caption1")[0]["modality"] == "visual"
    assert service.search("text2")[0]["modality"] == "ocr"
    service.set_analysis_policy(library_id, authorized)
    assert service.build_index(Analyzer("basic", "ocr", "text2"), additional_analyzers=scoped)["skipped"] == 1
    assert calls == ["basic", "vision", "basic"]


def test_directory_grant_does_not_apply_to_replacement_at_same_path(
    library: tuple[FilesCatalog, Path, str],
) -> None:
    catalog, root, library_id = library
    service = AILibraryService(catalog)
    authorized = LibraryAnalysisPolicy("profiles", ("a" * 64,))
    service.set_analysis_policy(library_id, authorized)
    first_directory = root.with_name("first-disk")
    root.rename(first_directory)
    assert service.analysis_policy(library_id) == LibraryAnalysisPolicy("profiles")
    root.mkdir()
    assert service.analysis_policy(library_id) == LibraryAnalysisPolicy("profiles")
    root.rmdir()
    first_directory.rename(root)
    assert service.analysis_policy(library_id) == authorized


def test_subfolder_policy_is_persistent_and_does_not_inherit_root_grants(
    library: tuple[FilesCatalog, Path, str], tmp_path: Path,
) -> None:
    catalog, root, library_id = library
    folder = root / "chosen"
    folder.mkdir()
    service = AILibraryService(catalog)
    root_policy = LibraryAnalysisPolicy("profiles", ("a" * 64,))
    scoped = LibraryAnalysisPolicy("profiles", ("b" * 64,), (("image", "vision-model"),), ("image",), False)
    service.set_analysis_policy(library_id, root_policy)
    assert service.analysis_policy(library_id, folder=str(root)) == root_policy
    assert service.analysis_policy(library_id, folder=str(folder)) == LibraryAnalysisPolicy()
    service.set_analysis_policy(library_id, scoped, folder=str(folder))
    assert AILibraryService(catalog).analysis_policy(library_id, folder=str(folder)) == scoped
    assert scoped.to_dict()["models"] == {"image": "vision-model"}
    assert service.analysis_policy(library_id) == root_policy

    replacement = folder.with_name("old")
    folder.rename(replacement)
    folder.mkdir()
    assert AILibraryService(catalog).analysis_policy(library_id, folder=str(folder)) == LibraryAnalysisPolicy(
        "profiles", (), (("image", "vision-model"),), ("image",), False,
    )
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    link = root / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        service.set_analysis_policy(library_id, scoped, folder=str(link))
    with pytest.raises(ValueError, match="绝对文件夹"):
        service.analysis_policy(library_id, folder=str(outside))
    child_id = catalog.register_library(folder).library_id
    with pytest.raises(ValueError, match="另一个"):
        service.analysis_policy(library_id, folder=str(folder))
    assert service.analysis_policy(child_id, folder=str(folder)) == LibraryAnalysisPolicy()


def test_search_scopes_location_and_media_types_before_candidate_limits(
    library: tuple[FilesCatalog, Path, str],
) -> None:
    catalog, root, library_id = library
    chosen, sibling, deeper = root / "ads", root / "ads-other", root / "ads" / "takes"
    for folder in (chosen, sibling, deeper):
        folder.mkdir()
    featured = chosen / "bell.jpg"
    path = deeper / "alternate.jpg"
    for src in (featured, path, *(sibling / f"bell-{i}.mov" for i in range(16)),
                *(chosen / f"bell-{i}.wav" for i in range(16))):
        src.write_bytes(src.name.encode())
    service = AILibraryService(catalog)
    service.build_index(lambda path, kind: (MediaEvidence("轻响门铃"),))
    params = {"library_id": library_id, "folder": str(chosen), "kinds": ("image",), "recursive": False}
    assert [hit["path"] for hit in service.search("门铃", 1, **params)] == [str(featured)]
    assert [hit["path"] for hit in service.search("bell", 1, **params)] == [str(featured)]
    assert {hit["path"] for hit in service.search("门铃", library_id=library_id,
                                                   folder=str(chosen), kinds=("image",))} == {str(featured), str(path)}
    assert service.search("门铃", kinds=()) == []


def test_partial_scan_does_not_write_offline_flags_outside_its_observed_scope(
    library: tuple[FilesCatalog, Path, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.features.files.services import ai_library

    catalog, root, library_id = library
    target, adjacent = root / "shot", root / "shots"
    inside = target / "inside"
    for folder in (target, adjacent, inside):
        folder.mkdir()
    watched = [target / "deleted.png", target / "unselected.mov", adjacent / "adjacent.png", inside / "nested.png"]
    for path in watched:
        path.write_bytes(path.name.encode())
    service = AILibraryService(catalog)
    analyze = _AnalysisAttempt((MediaEvidence("sample evidence"),), retryable=False)
    service.build_index(analyze)
    for path in watched:
        path.unlink()
    report = service.build_index(analyze, library_id=library_id, folder=str(target), kinds=("image",), recursive=False)
    assert report["offline"] == 1

    def states() -> dict[str, int]:
        with catalog._read() as cursor:
            return {row["path"]: row["online"] for row in cursor.execute("SELECT path, online FROM ai_library_locations")}

    assert states() == {str(watched[0]): 0, **{str(path): 1 for path in watched[1:]}}

    def unavailable(path: Path, *, onerror: Callable[[OSError], None]) -> Iterator[tuple[str, list[str], list[str]]]:
        onerror(PermissionError(f"no directory access: {path}"))
        return iter(())

    monkeypatch.setattr(ai_library.os, "walk", unavailable)
    service.build_index(analyze)
    assert states() == {str(watched[0]): 0, **{str(path): 1 for path in watched[1:]}}


def test_force_replaces_only_selected_locations_and_keeps_previous_evidence_on_failure(
    library: tuple[FilesCatalog, Path, str],
) -> None:
    catalog, root, library_id = library
    subfolder = root / "takes"
    subfolder.mkdir()
    first, outside = subfolder / "audio.wav", root / "other.wav"
    for path in (first, outside):
        path.write_bytes(b"same content")
    service = AILibraryService(catalog)
    original = _AnalysisAttempt((MediaEvidence("original door"),), retryable=False)
    assert service.build_index(original)["skipped"] == 1
    opts = {"library_id": library_id, "folder": str(subfolder)}
    failing = _AnalysisAttempt((MediaEvidence("partial door"),), ("provider failed",), retryable=True)
    assert service.build_index(failing, force=True, **opts)["failed"] == 1
    assert service.search("original", **opts)[0]["path"] == str(first)
    assert service.search("partial") == []
    corrected = _AnalysisAttempt((MediaEvidence("corrected window"),), retryable=False)
    assert service.build_index(corrected, force=True, **opts)["indexed"] == 1
    assert [hit["path"] for hit in service.search("original")] == [str(outside)]
    assert [hit["path"] for hit in service.search("corrected")] == [str(first)]
    assert service.search("corrected")[0]["asset_id"] == service.search("original")[0]["asset_id"]


def test_duplicate_bytes_get_independent_models_in_separately_scoped_directories(
    library: tuple[FilesCatalog, Path, str],
) -> None:
    catalog, root, library_id = library
    first, second = root / "first", root / "second"
    for folder in (first, second):
        folder.mkdir()
        (folder / "clip.mov").write_bytes(b"same video")
    service = AILibraryService(catalog)
    scope_a = {"library_id": library_id, "folder": str(first)}
    scope_b = {"library_id": library_id, "folder": str(second)}
    analysis_a = _AnalysisAttempt((MediaEvidence("indoors"),), retryable=False)
    analysis_b = _AnalysisAttempt((MediaEvidence("outdoors"),), retryable=False)
    assert service.build_index(analysis_a, analyzer_revision="model-a", **scope_a)["indexed"] == 1
    assert service.build_index(analysis_b, analyzer_revision="model-b", **scope_b)["indexed"] == 1
    assert service.build_index(analysis_a, analyzer_revision="model-a", **scope_a)["skipped"] == 1
    assert service.search("indoors", **scope_b) == []
    assert service.search("outdoors", **scope_a) == []
    assert service.search("indoors", **scope_a)[0]["asset_id"] == service.search("outdoors", **scope_b)[0]["asset_id"]


def test_badge_and_pending_search_match_successful_current_inference(
    library: tuple[FilesCatalog, Path, str],
) -> None:
    catalog, root, library_id = library
    sources = [root / f"{name}.png" for name in ("ready", "ocr", "failed", "unindexed")]
    for index, path in enumerate(sources):
        path.write_bytes(bytes([index]))
    pending = sources[-1].rename(root / "not-a-media-file.txt")

    class Stage:
        retryable_failure = False
        warnings: tuple[str, ...] = ()

        def __call__(self, path: str, kind: str) -> tuple[MediaEvidence, ...]:
            self.retryable_failure = Path(path) == sources[2]
            return (MediaEvidence("letters", modality="ocr"),) if Path(path) == sources[1] else (
                MediaEvidence("outdoor clip"),
            )

    service = AILibraryService(catalog)
    service.build_index(Stage())
    pending.rename(sources[-1])
    catalog.upsert_asset(library_id, sources[-1])
    catalog.patch_user_fields(library_id, sources[-1], comment="Manual cue")
    requested = list(map(str, sources))
    assert service.analysis_status(requested) == {str(sources[0])}
    assert {hit["name"] for hit in service.search("", only_unanalyzed=True)} == {"ocr.png", "failed.png", "unindexed.png"}
    found = service.search("manual cue", only_unanalyzed=True)
    assert len(found) == 1 and found[0]["path"] == str(sources[-1])
    assert found[0]["online"] and found[0]["source_size"] == 1
    cancelled = Event()
    cancelled.set()
    assert service.search("", only_unanalyzed=True, cancel=cancelled) == []
    assert service.analysis_status(requested, cancel=cancelled) == set()

    analysis = _AnalysisAttempt((MediaEvidence("a flower"),), retryable=False)
    report = service.build_index(analysis, only_unanalyzed=True, analyzer_revision="changed-model")
    assert report["skipped"] == 1 and report["indexed"] == 3 and report["offline"] == 0
    assert service.search("", only_unanalyzed=True) == []
    sources[0].write_bytes(b"changed since index")
    assert str(sources[0]) not in service.analysis_status(requested)
    assert [hit["path"] for hit in service.search("", only_unanalyzed=True)] == [str(sources[0])]


def test_aggregate_index_respects_each_library_scan_scope(library: tuple[FilesCatalog, Path, str]) -> None:
    catalog, root, library_id = library
    another = root / "own-library"
    another.mkdir()
    other_id = catalog.register_library(another).library_id
    nested = root / "deeper"
    nested.mkdir()
    sources = [root / "one.jpg", root / "two.wav", nested / "three.jpg", another / "four.wav"]
    for path in sources:
        path.write_bytes(path.name.encode())
    service = AILibraryService(catalog)
    analyze = _AnalysisAttempt((MediaEvidence("model result"),), retryable=False)
    scopes = {library_id: (("image",), False), other_id: (("audio",), True)}
    assert service.build_index(analyze, per_library_scope=scopes)["indexed"] == 2
    assert {hit["path"] for hit in service.search("model")} == {str(sources[0]), str(sources[-1])}
    assert service.build_index(analyze, per_library_scope=scopes)["skipped"] == 2


def test_v1_migration_preserves_only_generated_evidence_with_a_unique_known_location(
    library: tuple[FilesCatalog, Path, str],
) -> None:
    from src.features.files.services import ai_library

    catalog, root, library_id = library
    unique, duplicate_a, duplicate_b = root / "old.png", root / "copy-a.png", root / "copy-b.png"
    unique.write_bytes(b"one file")
    duplicate_a.write_bytes(b"duplicated")
    duplicate_b.write_bytes(b"duplicated")
    records = [
        (path, ai_library._asset_identity(str(path), "image", None))
        for path in (unique, duplicate_a, duplicate_b)
    ]
    with catalog._write() as cursor:
        for statement in ai_library._SCHEMA:
            cursor.execute(statement)
        cursor.execute("INSERT INTO settings VALUES ('', 'ai_library_schema', '1')")
        for path, asset_id in records:
            cursor.execute("INSERT OR IGNORE INTO ai_library_assets VALUES (?, 'image', '1', 1.0)", (asset_id,))
            cursor.execute("INSERT INTO ai_library_locations VALUES (?, ?, ?, ?, ?, ?, 1)",
                           (str(path), str(path), library_id, asset_id, path.name,
                            ai_library._file_state(str(path))))
        cursor.executemany(
            """INSERT INTO ai_library_evidence(asset_id, text, tags, tag_search, modality)
               VALUES (?, ?, '[]', '', 'visual')""",
            [(records[0][1], "exclusive legacy caption"), (records[1][1], "ambiguous legacy caption")],
        )
    catalog.upsert_asset(library_id, duplicate_b)
    catalog.patch_user_fields(library_id, duplicate_b, comment="human-selected copy")
    service = AILibraryService(catalog)
    assert service.analysis_status([str(unique), str(duplicate_a), str(duplicate_b)]) == {str(unique)}
    assert service.search("exclusive")[0]["path"] == str(unique)
    assert service.search("ambiguous") == []
    assert service.search("human-selected copy")[0]["text"] == "human-selected copy"
    report = service.build_index(_AnalysisAttempt((MediaEvidence("fresh caption"),), retryable=False))
    assert (report["indexed"], report["skipped"]) == (1, 2)
    assert set(service.analysis_status([str(duplicate_a), str(duplicate_b)])) == {str(duplicate_a), str(duplicate_b)}
