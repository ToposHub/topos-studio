"""Grade sets share the Files catalog across collections and application launches."""
from pathlib import Path

from src.app.controllers.grade_sets import GradeSetModel
from src.features.files.repository import FilesDataLocator
from src.features.files.repository.catalog import FilesCatalog
from src.features.files.services.domain import GRADE_SET_SCOPE
from src.features.files.services.facade import FilesFacade


def test_grade_sets_persist_order_and_share_paths_without_touching_files(qtbot, tmp_path: Path) -> None:
    locator = FilesDataLocator.for_config_root(tmp_path)
    locator.ensure_layout()
    catalog = FilesCatalog(locator)
    facade = FilesFacade(catalog)
    models = []
    try:
        first, second, video = (tmp_path / name for name in ("b.png", "a.jpg", "movie.mov"))
        for path in (first, second, video):
            path.write_bytes(b"source")
        model = GradeSetModel(lambda: facade)
        models.append(model)
        model.create("First", [str(first), str(video), str(second), str(first)])
        qtbot.waitUntil(lambda: model.paths == (str(first), str(second)))
        first_id = model.active_id
        model.create("Second", [str(first)])
        qtbot.waitUntil(lambda: len(model.collections) == 3)
        second_id = model.active_id
        model.remove(first_id, [str(first)])
        qtbot.waitUntil(lambda: any(item.id == first_id and item.count == 1 for item in model.collections))
        model.select(first_id)
        qtbot.waitUntil(lambda: model.paths == (str(second),))
        reopened = GradeSetModel(lambda: facade, second_id)
        models.append(reopened)
        reopened.refresh()
        qtbot.waitUntil(lambda: reopened.paths == (str(first),))
        reopened.delete(second_id)
        qtbot.waitUntil(lambda: reopened.active_id == reopened.collections[0].id
                        and reopened.collections[0].is_default and len(reopened.collections) == 2)
        assert all(path.read_bytes() == b"source" for path in (first, second, video))
    finally:
        for model in models:
            model.shutdown()
        facade.shutdown(wait=True)
        catalog.close()


def test_default_set_initializes_once_and_accepts_drops_before_the_first_read(qtbot, tmp_path: Path) -> None:
    locator = FilesDataLocator.for_config_root(tmp_path)
    locator.ensure_layout()
    catalog = FilesCatalog(locator)
    facade = FilesFacade(catalog)
    first = GradeSetModel(lambda: facade)
    second = GradeSetModel(lambda: facade, active_id=-1)
    models = [first, second]
    source_paths = tuple(str(tmp_path / name) for name in ("portrait.png", "landscape.jpg"))
    for path in map(Path, source_paths):
        path.write_bytes(b"photo")
    try:
        first.add_dropped_images((*source_paths, source_paths[0]))
        second.refresh()
        qtbot.waitUntil(lambda: first.paths == source_paths and bool(second.collections))
        default = first.collections[0]
        assert default.is_default
        assert default.id == first.active_id == second.active_id
        assert len(facade.collections.list_collections(GRADE_SET_SCOPE).items) == 1
        assert not facade.collections.rename_collection(default.id, "Other name").ok
        assert not facade.collections.delete_collection(default.id).ok

        second.create("Existing Project")
        qtbot.waitUntil(lambda: second.active_id != default.id)
        second.add_dropped_images([source_paths[-1]])
        qtbot.waitUntil(lambda: second.paths == (source_paths[-1],))
        reopened = GradeSetModel(lambda: facade, active_id=second.active_id)
        models.append(reopened)
        reopened.refresh()
        qtbot.waitUntil(lambda: reopened.paths == (source_paths[-1],))
        assert reopened.active_id == second.active_id
        assert sum(item.is_default for item in reopened.collections) == 1
        assert reopened.collections[0].id == default.id
        assert facade.collections.member_paths(default.id) == list(source_paths)
    finally:
        for model in models:
            model.shutdown()
        facade.shutdown(wait=True)
        catalog.close()


def test_grade_set_ratings_follow_members_from_their_own_libraries(qtbot, tmp_path: Path) -> None:
    locator = FilesDataLocator.for_config_root(tmp_path)
    locator.ensure_layout()
    catalog = FilesCatalog(locator)
    facade = FilesFacade(catalog)
    model = GradeSetModel(lambda: facade)
    try:
        ratings: dict[str, int] = {}
        for name, score in (("one", 4), ("two", 2)):
            root = tmp_path / name
            root.mkdir()
            source = root / "image.png"
            source.write_bytes(b"photo")
            library = catalog.register_library(root)
            catalog.upsert_asset(library.library_id, source)
            catalog.patch_user_fields(library.library_id, source, rating=score)
            ratings[str(source)] = score
        model.create("Both libraries", ratings)
        qtbot.waitUntil(lambda: model.ratings == ratings)
        collection = model.active_id
        model.create("Second only", [str(source)])
        qtbot.waitUntil(lambda: model.ratings == {str(source): 2})
        catalog.patch_user_fields(library.library_id, source, rating=5)
        model.refresh_ratings({str(source): {"rating": 5}})
        qtbot.waitUntil(lambda: model.ratings == {str(source): 5})
        model.select(collection)
        qtbot.waitUntil(lambda: model.ratings == {**ratings, str(source): 5})
    finally:
        model.shutdown()
        facade.shutdown(wait=True)
        catalog.close()
