"""Copy a still's editable grading stack into another still's Color session."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from uuid import uuid4

from PyQt6.QtCore import QObject, pyqtSignal

from src.features.color.core.layer import Layer, MaskGroup, MaskShape
from src.shared.color.lut_resource_registry import LutResourceSession, get_lut_resource_session
from src.shared.i18n.i18n import I18N
from src.shared.project.resource_resolver import (
    ResourceResolutionError, get_active_project_resolver, sha256_file,
)

if TYPE_CHECKING:
    from src.app.main_window import MainWindow


def _tr(key: str, fallback: str) -> str:
    return I18N.tr(f"grade_sets.{key}", fallback)


def _portable_mask(item: MaskGroup | MaskShape) -> tuple[MaskGroup | MaskShape | None, bool]:
    if isinstance(item, MaskGroup):
        copied = [_portable_mask(child) for child in item.children]
        children = [mask for mask, _ in copied if mask is not None]
        if not children:
            return None, bool(item.children)
        group = deepcopy(item, {id(item.children): []})
        group.children = children
        return group, any(skipped for _, skipped in copied)
    if item.type in (MaskShape.BITMAP, MaskShape.BRUSH):
        return None, True
    return MaskShape.from_dict(deepcopy(item.to_dict())), False


def _new_mask_ids(items: list[MaskGroup | MaskShape]) -> None:
    for item in items:
        item.id = str(uuid4())
        if isinstance(item, MaskGroup):
            _new_mask_ids(item.children)


@dataclass
class _PhotoGrade:
    source: str
    layers: list[Layer]
    active: int
    has_source_specific_data: bool
    lut_refs: dict[str, str] = field(default_factory=dict)
    lut_folder: TemporaryDirectory[str] | None = None
    lut_session: LutResourceSession | None = None

    @classmethod
    def capture(cls, source: str, layers: list[Layer], active: int) -> _PhotoGrade:
        snapshots: list[Layer] = []
        omitted = False
        for original in layers:
            layer = Layer(original.name)
            layer.copy_params_from(original)
            layer.visible = original.visible
            layer.plugins = [plugin for plugin in layer.plugins if plugin.get("type", "dctl") == "dctl"]
            layer.grade_keyframes.clear()
            layer._plugins_base = None
            masks = [_portable_mask(item) for item in original.mask_shapes]
            layer.mask_shapes = [mask for mask, _ in masks if mask is not None]
            if (original.mask_shapes and not layer.mask_shapes and any(skipped for _, skipped in masks)
                    or original.ai_matte is not None and original.ai_matte_enabled):
                # A portable copy must not turn a subject-only grade global.
                layer.visible = False
            omitted |= bool(original.ai_matte is not None or original.is_baked
                            or original.grade_keyframes or len(layer.plugins) != len(original.plugins)
                            or any(skipped for _, skipped in masks))
            snapshots.append(layer)
        return cls(source, snapshots, active, omitted)

    def layers_for_target(self, lut_refs: dict[str, str]) -> list[Layer]:
        layers = deepcopy(self.layers)
        for layer in layers:
            layer.id = str(uuid4())
            _new_mask_ids(layer.mask_shapes)
            if layer.active_lut_path in lut_refs:
                layer.active_lut_path = lut_refs[layer.active_lut_path]
        return layers


class GradeSetTransfer(QObject):
    """A session clipboard. File work is asynchronous and commits are target-checked."""

    _completed = pyqtSignal(object)

    def __init__(self, host: MainWindow) -> None:
        super().__init__(host)
        self._host = host
        self._grade: _PhotoGrade | None = None
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="photo-grade")
        self._closed = False
        self._busy = False
        self._completed.connect(self._finish)

    def _message(self, message: str) -> None:
        self._host.status_bar.showMessage(message, 4500)

    def _finish(self, result: Future[_PhotoGrade]) -> None:
        if self._closed:
            return
        self._busy = False
        self._host.grade_set_panel.set_grade_clipboard_available(self._grade is not None)
        try:
            value = result.result()
        except (OSError, ValueError, RuntimeError, ResourceResolutionError) as exc:
            self._lut_error(exc)
            return
        self._publish(value)

    def _publish(self, grade: _PhotoGrade) -> None:
        self._grade = grade
        self._host.grade_set_panel.set_grade_clipboard_available(True)
        self._message(_tr("grade_copied_portable", "Grade copied. Source-specific masks and baked effects were excluded.")
                      if grade.has_source_specific_data else _tr("grade_copied", "Grade copied"))

    def _lut_error(self, exc: Exception) -> None:
        self._message(_tr("lut_transfer_failed", "Could not transfer a LUT: {detail}").format(detail=exc))

    def copy(self, path: str) -> None:
        host = self._host
        if (self._closed or self._busy or path != host._active_grade_set_image()
                or host.engine.img_full_original is None or not host.engine.layers):
            return
        if host.color_resource_controller is not None:
            host.color_resource_controller.finish_transaction()
        snapshot = _PhotoGrade.capture(path, host.engine.layers, host.engine.active_layer_index)
        refs = tuple(dict.fromkeys(layer.active_lut_path for layer in snapshot.layers if layer.active_lut_path))

        if not refs:
            self._publish(snapshot)
            return

        resolver = get_active_project_resolver()
        session = get_lut_resource_session()
        leases = ExitStack()
        sources = {}
        # Retain read handles across a photo switch. Closing the source
        # project can remove its original LUT staging directory immediately.
        try:
            for ref in refs:
                local = ref
                expected_hash = None
                if ref.startswith("topos-res://"):
                    local = session.staged_path(ref)
                    entry = resolver.index.by_uri(ref) if resolver is not None else None
                    if not local and entry is not None:
                        local = resolver.local_path_for(entry)
                        expected_hash = entry.sha256
                    if not local:
                        raise FileNotFoundError(ref)
                sources[ref] = (leases.enter_context(open(local, "rb")), Path(local).name, expected_hash)
        except (OSError, ResourceResolutionError) as exc:
            leases.close()
            self._lut_error(exc)
            return

        def copy_luts() -> _PhotoGrade:
            snapshot.lut_folder = TemporaryDirectory(prefix="topos-photo-grade-")
            root = Path(snapshot.lut_folder.name)
            snapshot.lut_session = LutResourceSession(str(root / "staged"))
            with TemporaryDirectory(dir=root) as incoming:
                paths = []
                for index, (source, name, expected_hash) in enumerate(sources.values()):
                    target = Path(incoming) / f"{index}-{name}"
                    with target.open("wb") as output:
                        shutil.copyfileobj(source, output)
                    if expected_hash and sha256_file(str(target)) != expected_hash:
                        raise ValueError(name)
                    paths.append(str(target))
                results = snapshot.lut_session.import_files(paths, cancel_check=lambda: self._closed)
            for ref, result in zip(refs, results, strict=True):
                if result.entry is None:
                    raise ValueError(result.error or result.status)
                snapshot.lut_refs[ref] = result.entry.uri
            return snapshot

        def done(result: Future[_PhotoGrade]) -> None:
            leases.close()
            if not self._closed:
                self._completed.emit(result)

        self._busy = True
        host.grade_set_panel.set_grade_clipboard_available(False)
        self._worker.submit(copy_luts).add_done_callback(done)

    def paste(self, path: str) -> None:
        grade = self._grade
        if self._closed or self._busy or grade is None or path == grade.source:
            return
        self._paste_to_image(path, grade)

    def _paste_to_image(self, path: str, grade: _PhotoGrade) -> None:
        if self._closed:
            return
        host = self._host
        if (path != host.grade_set_panel.active_path
                and not host._prepare_to_leave_photo(lambda: self._paste_to_image(path, grade))):
            return
        host._open_grade_set_image(path)
        if host._active_grade_set_image() != path or host.grade_set_panel.active_path != path:
            return

        references = {}
        if grade.lut_session is not None:
            try:
                adopted = get_lut_resource_session().adopt_staged_luts(grade.lut_session, grade.lut_refs.values())
            except OSError as exc:
                self._lut_error(exc)
                return
            references = {ref: adopted[staged] for ref, staged in grade.lut_refs.items()}
        host.grade_set_panel.invalidate_graded_thumbnail(path)
        host._apply_grade_set_layers(grade.layers_for_target(references), grade.active)
        self._message(_tr("grade_pasted", "Grade pasted to {name}").format(name=Path(path).name))

    def shutdown(self) -> None:
        self._closed = True
        self._grade = None
        self._worker.shutdown(wait=False, cancel_futures=True)
