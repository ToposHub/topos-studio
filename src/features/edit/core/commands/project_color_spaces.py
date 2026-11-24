"""Undoable edits to the project's video color spaces."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Mapping

from src.features.edit.core.project import EditProject


@dataclass(frozen=True)
class SetProjectColorSpacesCommand:
    label: str
    selections: tuple[tuple[str, str], ...]
    previous: tuple[tuple[str, Any], ...] = ()

    def apply(
        self, project: EditProject,
    ) -> tuple[EditProject, SetProjectColorSpacesCommand]:
        settings = dict(project.color_management)
        previous = tuple((key, deepcopy(settings[key])) for key, _ in self.selections
                         if key in settings)
        settings.update(self.selections)
        return project.with_color_management(settings), replace(self, previous=previous)

    def undo(self, project: EditProject) -> EditProject:
        settings = dict(project.color_management)
        for key, _ in self.selections:
            settings.pop(key, None)
        settings.update(self.previous)
        return project.with_color_management(settings)
