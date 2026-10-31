"""R-05/R-06: three-tier template store — discovery, import/export, protection.

Users must be able to add and override Motion Graphic HTML templates by
copying a folder into a writable directory (user / project), never by
editing the Python package.  These tests pin the origin precedence
(project > user > bundled), the atomic validated writes and the
read-only protection of the bundled library.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.features.teaching_animation.core.motion_graphic_template_library import (
    default_motion_graphic_template_root,
)
from src.features.teaching_animation.core.motion_graphic_template_store import (
    ORIGIN_BUNDLED,
    ORIGIN_PROJECT,
    ORIGIN_USER,
    StoredTemplatePackage,
    TemplateStoreError,
    discover_stored_templates,
    export_template_zip,
    import_template_folder,
    import_template_zip,
    project_template_root,
    remove_template,
    user_template_root,
    validate_template_folder,
)

BUNDLED_PACKAGE = default_motion_graphic_template_root() / "particle_flow_background"


def _make_package(dest: Path, template_id: str, version: int, *, name: str = "Copy") -> Path:
    """Materialize a valid package by copying the real bundled one."""
    dest.mkdir(parents=True, exist_ok=True)
    for item in BUNDLED_PACKAGE.iterdir():
        if item.is_file():
            target = dest / item.name
            target.write_bytes(item.read_bytes())
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    manifest["template_id"] = template_id
    manifest["version"] = version
    manifest["display_name"] = f"{name} {template_id}"
    (dest / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return dest


@pytest.fixture()
def roots(tmp_path: Path):
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    project = tmp_path / "project" / ".topos" / "motion_graphic_templates"
    _make_package(bundled / "particle-flow-background", "particle-flow-background", 1)
    return bundled, user, project, tmp_path / "project"


# -- discovery & precedence ------------------------------------------------------

def test_discovery_merges_origins_with_project_user_bundled_precedence(roots):
    bundled, user, project, project_path = roots
    _make_package(user / "particle-flow-background", "particle-flow-background", 2)
    _make_package(user / "user-only", "user-only", 1)
    stored = {s.template_id: s for s in discover_stored_templates(
        project_path=project_path, user_root=user, bundled_root=bundled)}
    # project empty → user (v2) overrides bundled (v1); user-only discovered.
    assert stored["particle-flow-background"].origin == ORIGIN_USER
    assert stored["particle-flow-background"].package.template.version == 2
    assert stored["user-only"].origin == ORIGIN_USER

    _make_package(project / "particle-flow-background", "particle-flow-background", 3)
    stored = {s.template_id: s for s in discover_stored_templates(
        project_path=project_path, user_root=user, bundled_root=bundled)}
    assert stored["particle-flow-background"].origin == ORIGIN_PROJECT
    assert stored["particle-flow-background"].package.template.version == 3


def test_bundled_only_discovery_reports_bundled_origin(roots):
    bundled, _user, _project, project_path = roots
    stored = discover_stored_templates(
        project_path=project_path, user_root=bundled.parent / "empty-user",
        bundled_root=bundled)
    assert all(s.origin == ORIGIN_BUNDLED for s in stored)
    assert any(s.template_id == "particle-flow-background" for s in stored)


def test_user_and_project_roots_never_live_in_the_wheel(tmp_path: Path):
    assert user_template_root().resolve() != default_motion_graphic_template_root().resolve()
    assert default_motion_graphic_template_root().resolve() not in user_template_root().resolve().parents
    assert project_template_root(tmp_path) == tmp_path / ".topos" / "motion_graphic_templates"


# -- validation & import ---------------------------------------------------------

def test_validate_reports_actionable_errors(tmp_path: Path):
    empty = tmp_path / "broken"
    empty.mkdir()
    errors = validate_template_folder(empty)
    assert errors and "manifest.json" in errors[0]


def test_import_folder_validates_and_copies_atomically(roots, tmp_path: Path):
    _bundled, user, _project, _pp = roots
    source = _make_package(tmp_path / "incoming" / "fresh-template", "fresh-template", 1)
    stored = import_template_folder(source, user)
    assert isinstance(stored, StoredTemplatePackage)
    assert (user / "fresh-template" / "manifest.json").is_file()

    broken = tmp_path / "incoming" / "broken"
    broken.mkdir()
    with pytest.raises(TemplateStoreError, match="manifest.json"):
        import_template_folder(broken, user)
    assert not (user / "broken").exists(), "a rejected package must leave nothing behind"


def test_import_conflict_needs_explicit_overwrite(roots, tmp_path: Path):
    _bundled, user, _project, _pp = roots
    source = _make_package(tmp_path / "incoming" / "dup", "dup-template", 1)
    import_template_folder(source, user)
    with pytest.raises(TemplateStoreError, match="already exists"):
        import_template_folder(source, user)
    stored = import_template_folder(source, user, overwrite=True)
    assert stored.template_id == "dup-template"


def test_bundled_library_is_read_only(roots, tmp_path: Path):
    bundled, user, _project, _pp = roots
    source = _make_package(tmp_path / "incoming" / "evil", "evil-template", 1)
    # The guard fires before any copy, so probing the real wheel directory
    # is safe.
    with pytest.raises(TemplateStoreError, match="read-only"):
        import_template_folder(source, default_motion_graphic_template_root())
    with pytest.raises(TemplateStoreError, match="read-only"):
        import_template_folder(source, user, origin=ORIGIN_BUNDLED)
    from src.features.teaching_animation.core.motion_graphic_template_library import (
        load_motion_graphic_template_package,
    )
    stored = StoredTemplatePackage(
        load_motion_graphic_template_package(bundled / "particle-flow-background"),
        ORIGIN_BUNDLED, bundled / "particle-flow-background")
    assert stored.writable is False
    with pytest.raises(TemplateStoreError, match="bundled"):
        remove_template(stored)


# -- export round-trip & remove ---------------------------------------------------

def test_export_zip_and_import_zip_roundtrip(roots, tmp_path: Path):
    bundled, user, _project, _pp = roots
    archive = export_template_zip(bundled / "particle-flow-background",
                                  tmp_path / "share.zip")
    assert archive.is_file()
    destination = tmp_path / "other-library"
    stored = import_template_zip(archive, destination)
    assert stored.template_id == "particle-flow-background"
    assert (destination / "particle-flow-background" / "template.html").is_file()


def test_remove_deletes_only_writable_templates(roots, tmp_path: Path):
    _bundled, user, _project, _pp = roots
    source = _make_package(tmp_path / "incoming" / "goner", "goner", 1)
    stored = import_template_folder(source, user)
    remove_template(stored)
    assert not (user / "goner").exists()
