"""Shared Library and Color selection over the Files collection catalog."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
import logging
from pathlib import Path
from uuid import uuid4

from PyQt6.QtCore import QObject, Qt, pyqtSignal

from src.features.files.repository.catalog import nfc
from src.features.files.services.domain import GRADE_SET_SCOPE
from src.features.files.services.facade import FacadeOutcome, FilesFacade
from src.shared.i18n.i18n import I18N
from src.shared.media.format_registry import is_image
from src.shared.plugins.services import ServiceResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GradeSet:
    id: int
    name: str
    count: int
    is_default: bool = False


class GradeSetModel(QObject):
    """One active set, with ordered mutations and stale-read protection."""

    changed = pyqtSignal()
    ratings_changed = pyqtSignal()
    failed = pyqtSignal(str)
    _delivery = pyqtSignal(object, object)

    def __init__(self, get_facade: Callable[[], FilesFacade | None],
                 active_id: int | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.collections: tuple[GradeSet, ...] = ()
        self.active_id = active_id
        self.paths: tuple[str, ...] = ()
        self.ratings: dict[str, int] = {}
        self._get_facade = get_facade
        self._request_scope = f"grade-sets:{uuid4().hex}"
        self._closed = False
        self._writing = False
        self._writes: deque[Callable[[FilesFacade], int | None]] = deque()
        self._read_id: str | None = None
        self._ratings_read_id: str | None = None
        self._delivery.connect(self._accept_delivery, Qt.ConnectionType.QueuedConnection)

    def _accept_delivery(self, callback: Callable[[FacadeOutcome], None], outcome: FacadeOutcome) -> None:
        if not self._closed:
            callback(outcome)

    def _submit(self, facade: FilesFacade, key: str, request_id: str,
                work: Callable[[], object], done: Callable[[FacadeOutcome], None]) -> None:
        facade.run(key=f"{self._request_scope}:{key}", request_id=request_id, work=work,
                   listener=lambda outcome: self._publish(done, outcome))

    def _publish(self, done: Callable[[FacadeOutcome], None], outcome: FacadeOutcome) -> None:
        if not self._closed:
            self._delivery.emit(done, outcome)

    def refresh(self) -> None:
        if self._closed:
            return
        facade = self._get_facade()
        if facade is None:
            self.failed.emit(I18N.tr("grade_sets.unavailable", "Grade sets are unavailable"))
            return
        requested = self.active_id
        default_name = I18N.tr("grade_sets.default_name", "Default Grade Set")
        self._read_id = request_id = uuid4().hex

        def read() -> tuple[tuple[GradeSet, ...], int, tuple[str, ...]]:
            service = facade.collections
            default_id = service.ensure_default_grade_set()
            sets = []
            for item in service.list_collections(GRADE_SET_SCOPE).items:
                collection_id = int(item.result["id"])
                is_default = collection_id == default_id
                name = default_name if is_default else str(item.result["name"])
                sets.append(GradeSet(collection_id, name, int(item.result["item_count"]), is_default))
            sets.sort(key=lambda item: not item.is_default)
            active = requested if any(item.id == requested for item in sets) else default_id
            return tuple(sets), active, tuple(service.member_paths(active))

        def done(outcome: FacadeOutcome) -> None:
            if outcome.request_id != self._read_id:
                return
            if outcome.error is not None:
                self.failed.emit(outcome.error.message)
                return
            self.collections, self.active_id, self.paths = outcome.result
            self._set_ratings({path: self.ratings[path] for path in self.paths if path in self.ratings})
            self.changed.emit()
            self.refresh_ratings()

        self._submit(facade, "read", request_id, read, done)

    def select(self, collection_id: int) -> None:
        if collection_id == self.active_id or not any(item.id == collection_id for item in self.collections):
            return
        self.active_id = collection_id
        self.paths = ()
        self._ratings_read_id = None
        self._set_ratings({})
        self.changed.emit()
        self.refresh()

    def _set_ratings(self, ratings: dict[str, int]) -> None:
        if ratings != self.ratings:
            self.ratings = ratings
            self.ratings_changed.emit()

    def refresh_ratings(self, snapshot: dict[str, object] | None = None) -> None:
        """Read ratings from their Files library, independently of set loading."""
        if self._closed:
            return
        paths = self.paths
        if not paths or (snapshot is not None and not set(paths).intersection(nfc(path) for path in snapshot)):
            return
        facade = self._get_facade()
        if facade is None:
            return
        active_id = self.active_id
        self._ratings_read_id = request_id = uuid4().hex

        def read() -> dict[str, int]:
            groups: dict[str, list[str]] = {}
            for path in paths:
                library = facade.libraries.library_for_path(path)
                if library is not None:
                    groups.setdefault(library.library_id, []).append(path)
            ratings = {}
            for library_id, members in groups.items():
                for item in facade.metadata.get_metadata(members, library_id=library_id).items:
                    if item.ok:
                        rating = item.result.get("rating")
                        if type(rating) is int and 1 <= rating <= 5:
                            ratings[item.path] = rating
            return ratings

        def done(outcome: FacadeOutcome) -> None:
            if (outcome.request_id != self._ratings_read_id
                    or self.active_id != active_id or self.paths != paths):
                return
            if outcome.error is not None:
                logger.warning("Could not load grade set ratings: %s", outcome.error.message)
                return
            self._set_ratings(outcome.result)

        self._submit(facade, "ratings", request_id, read, done)

    @staticmethod
    def _images(paths: Iterable[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(str(path) for raw in paths
                                   if (path := Path(raw)).is_absolute() and is_image(str(path))
                                   and path.is_file()))

    @staticmethod
    def _require(result: ServiceResult) -> None:
        if not result.ok:
            if result.error_code == "DEFAULT_GRADE_SET_FIXED":
                raise ValueError(I18N.tr(
                    "grade_sets.default_fixed", "The default grade set is always available"))
            detail = next((item.message for item in result.items if not item.ok and item.message), "")
            raise ValueError(result.message or detail or result.error_code or "Grade set operation failed")

    def _write(self, operation: Callable[[FilesFacade], int | None]) -> None:
        if self._closed:
            return
        self._writes.append(operation)
        self._next_write()

    def _next_write(self) -> None:
        if self._writing or not self._writes or self._closed:
            return
        facade = self._get_facade()
        if facade is None:
            self._writes.clear()
            self.failed.emit(I18N.tr("grade_sets.unavailable", "Grade sets are unavailable"))
            return
        operation = self._writes.popleft()
        self._writing = True

        def done(outcome: FacadeOutcome) -> None:
            self._writing = False
            if outcome.error is not None:
                self.failed.emit(outcome.error.message)
            elif outcome.result is not None:
                self.active_id = int(outcome.result)
                self.paths = ()
            self.refresh()
            self._next_write()

        self._submit(facade, "write", uuid4().hex, lambda: operation(facade), done)

    def _require_images(self, paths: tuple[str, ...]) -> tuple[str, ...]:
        members = self._images(paths)
        if paths and not members:
            raise ValueError(I18N.tr("grade_sets.no_images", "Choose supported image files"))
        return members

    def create(self, name: str, paths: Iterable[str] = ()) -> None:
        selection = tuple(paths)

        def operation(facade: FilesFacade) -> int:
            members = self._require_images(selection)
            result = facade.collections.create_collection(GRADE_SET_SCOPE, name, paths=members)
            self._require(result)
            return int(result.message)

        self._write(operation)

    def _update(self, collection_id: int, operation: Callable[[FilesFacade], ServiceResult]) -> None:
        def write(facade: FilesFacade) -> None:
            if facade.collections.library_of(collection_id) != GRADE_SET_SCOPE:
                raise ValueError(I18N.tr("grade_sets.missing", "Grade set no longer exists"))
            self._require(operation(facade))

        self._write(write)

    def add(self, collection_id: int, paths: Iterable[str]) -> None:
        selection = tuple(paths)
        if selection:
            self._update(collection_id, lambda f: f.collections.add_members(
                collection_id, self._require_images(selection)))

    def add_dropped_images(self, paths: Iterable[str]) -> None:
        """Add successful Color drops to the selected set or the default."""
        selection = tuple(paths)
        if not selection:
            return
        requested = self.active_id

        def write(facade: FilesFacade) -> None:
            members = self._require_images(selection)
            service = facade.collections
            target = requested
            if target is None or service.library_of(target) != GRADE_SET_SCOPE:
                target = service.ensure_default_grade_set()
            self._require(service.add_members(target, members))

        self._write(write)

    def remove(self, collection_id: int, paths: Iterable[str]) -> None:
        selection = tuple(paths)
        if selection:
            self._update(collection_id, lambda f: f.collections.remove_members(collection_id, selection))

    def rename(self, collection_id: int, name: str) -> None:
        self._update(collection_id, lambda f: f.collections.rename_collection(collection_id, name))

    def delete(self, collection_id: int) -> None:
        self._update(collection_id, lambda f: f.collections.delete_collection(collection_id))

    def shutdown(self) -> None:
        self._closed = True
        self._writes.clear()
