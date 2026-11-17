"""PL-08 回归：本地包安装/更新/卸载事务——staged、可回滚、拒绝半包。"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src.shared.plugins.installer import InstallerError, LocalPackageInstaller

HOST = dict(host_api=1, topos_version="1.6.0", platform="darwin", arch="x86_64")


def _pkg(root: Path, plugin_id="files.my_tool", version="1.0.0", **extra) -> Path:
    folder = root / "pkg"
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {"plugin_id": plugin_id, "display_name": "My Tool", "version": version,
                "capabilities": [{"capability_id": "launch", "kind": "command"}]}
    manifest.update(extra)
    (folder / "topos.plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "code.txt").write_text("static", encoding="utf-8")
    return folder


def _zip_of(folder: Path, target: Path) -> Path:
    with zipfile.ZipFile(target, "w") as archive:
        for path in sorted(folder.rglob("*")):
            archive.write(path, path.relative_to(folder).as_posix())
    return target


def _installer(root: Path) -> LocalPackageInstaller:
    return LocalPackageInstaller(root / "installed", host_facts=HOST)


def test_install_from_folder_creates_versioned_layout(tmp_path):
    installer = _installer(tmp_path)
    outcome = installer.install(_pkg(tmp_path))
    assert outcome.outcome == "installed"
    plugin_root = tmp_path / "installed" / "files.my_tool"
    assert (plugin_root / "current").is_symlink()
    assert installer.current_version("files.my_tool") == "1.0.0"
    manifest = installer.installed_manifest("files.my_tool")
    assert manifest is not None and manifest.display_name == "My Tool"


def test_update_keeps_previous_version_for_rollback(tmp_path):
    installer = _installer(tmp_path)
    installer.install(_pkg(tmp_path, version="1.0.0"))
    (tmp_path / "v2").mkdir()
    pkg2 = _pkg(tmp_path / "v2", version="2.0.0")
    outcome = installer.install(pkg2)
    assert outcome.outcome == "updated"
    assert outcome.previous_version == "1.0.0"
    assert Path(outcome.rollback_path).is_dir()                 # 旧版本保留
    assert installer.current_version("files.my_tool") == "2.0.0"


def test_same_version_is_unchanged_and_lower_version_rejected(tmp_path):
    installer = _installer(tmp_path)
    installer.install(_pkg(tmp_path, version="1.2.0"))
    same = _pkg(tmp_path / "again", version="1.2.0")
    assert installer.install(same).outcome == "unchanged"
    older = _pkg(tmp_path / "older", version="1.1.0")
    with pytest.raises(InstallerError) as exc:
        installer.install(older)
    assert exc.value.code == "VERSION_NOT_NEWER"
    assert installer.install(older, allow_downgrade=True).outcome == "updated"


def test_rejected_package_leaves_nothing_behind(tmp_path):
    installer = _installer(tmp_path)
    bad = _pkg(tmp_path, plugin_id="files.bad", host_api=[9, 9])  # 宿主不兼容
    with pytest.raises(InstallerError) as exc:
        installer.install(bad)
    assert exc.value.code == "MANIFEST_REJECTED"
    assert list((tmp_path / "installed").glob("*")) == [] or not (
        tmp_path / "installed" / "files.bad").exists()
    staging = tmp_path / "installed" / ".staging"
    assert not any(path.is_dir() for path in staging.glob("*")) if staging.exists() else True


@pytest.mark.parametrize("code,make_pkg", [
    ("MANIFEST_MISSING", lambda root: _write(root, {"topos.plugin.json": None})),
    ("INSTALL_HOOK_FORBIDDEN", lambda root: _pkg(root, metadata={"runAfterInstall": "x.py"})),
    ("MANIFEST_INVALID", lambda root: _write(root, {"topos.plugin.json": "{ bad"})),
])
def test_validation_failures_reject_with_code(tmp_path, code, make_pkg):
    installer = _installer(tmp_path)
    with pytest.raises(InstallerError) as exc:
        installer.install(make_pkg(tmp_path))
    assert exc.value.code == code


def _write(root: Path, files: dict) -> Path:
    folder = root / "pkg"
    folder.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        if content is None:
            continue
        (folder / name).write_text(content, encoding="utf-8")
    return folder


def test_zip_traversal_and_symlink_and_bomb_are_refused(tmp_path):
    installer = _installer(tmp_path)
    traversal = tmp_path / "evil.zip"
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../escape.txt", "pwn")
    with pytest.raises(InstallerError, match="非法路径|路径穿越|traversal"):
        installer.install(traversal)
    absolute = tmp_path / "abs.zip"
    with zipfile.ZipFile(absolute, "w") as archive:
        archive.writestr("/etc/pwn.txt", "x")
    with pytest.raises(InstallerError):
        installer.install(absolute)
    bogus = tmp_path / "notzip.zip"
    bogus.write_text("plain text", encoding="utf-8")
    with pytest.raises(InstallerError) as exc:
        installer.install(bogus)
    assert exc.value.code == "PACKAGE_NOT_ZIP"


def test_expected_identity_mismatch_rejected(tmp_path):
    installer = _installer(tmp_path)
    pkg = _pkg(tmp_path, plugin_id="files.a")
    with pytest.raises(InstallerError) as exc:
        installer.install(pkg, expected_plugin_id="files.b")
    assert exc.value.code == "PLUGIN_ID_MISMATCH"


def test_failure_between_move_and_swap_restores_previous_state(tmp_path, monkeypatch):
    """指针切换失败：新版本目录必须清掉，旧版本原样可用。"""
    installer = _installer(tmp_path)
    installer.install(_pkg(tmp_path, version="1.0.0"))
    pkg2 = _pkg(tmp_path / "v2", version="2.0.0")
    import src.shared.plugins.installer as installer_module
    real_replace = installer_module.os.replace

    def broken_replace(src, dst, *args, **kwargs):
        if str(dst).endswith("current"):
            raise OSError("disk full during pointer swap")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(installer_module.os, "replace", broken_replace)
    with pytest.raises(InstallerError) as exc:
        installer.install(pkg2)
    assert exc.value.code == "INSTALL_IO_ERROR"
    monkeypatch.undo()
    assert installer.current_version("files.my_tool") == "1.0.0"   # 旧版本完好
    versions = tmp_path / "installed" / "files.my_tool" / "versions"
    assert [path.name for path in versions.iterdir()] == ["1.0.0"]  # 半包不留下


def test_validator_hook_can_veto_packages(tmp_path):
    installer = LocalPackageInstaller(
        tmp_path / "installed", host_facts=HOST,
        validator=lambda manifest: ["不许可的权限"] if manifest.plugin_id == "files.shady" else [])
    ok = _pkg(tmp_path / "ok", plugin_id="files.fine")
    shady = _pkg(tmp_path / "shady", plugin_id="files.shady")
    assert installer.install(ok).outcome == "installed"
    with pytest.raises(InstallerError) as exc:
        installer.install(shady)
    assert exc.value.code == "PACKAGE_VALIDATOR_REJECTED"


def test_uninstall_separates_plugin_removal_from_data_deletion(tmp_path):
    installer = _installer(tmp_path)
    installer.install(_pkg(tmp_path))
    with pytest.raises(InstallerError) as exc:
        installer.uninstall("files.my_tool", is_active=lambda pid: True)
    assert exc.value.code == "PLUGIN_ACTIVE"
    deleted: list[str] = []
    outcome = installer.uninstall("files.my_tool", is_active=lambda pid: False)
    assert outcome.outcome == "uninstalled"
    assert deleted == []                                          # 默认不动用户数据
    with pytest.raises(InstallerError):
        installer.uninstall("files.missing", is_active=lambda pid: False)
    installer.install(_pkg(tmp_path / "again", version="1.1.0"))
    installer.uninstall("files.my_tool", is_active=lambda pid: False,
                        remove_data=True, delete_data=deleted.append)
    assert deleted == ["files.my_tool"]                           # 数据删除是显式动作


@pytest.mark.parametrize("kind", ["file", "directory", "dangling", "manifest", "root"])
def test_directory_symlinks_are_rejected_before_copy_or_manifest_read(tmp_path, monkeypatch, kind):
    import src.shared.plugins.installer as module
    package = _pkg(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "private_fixture.txt"
    sentinel.write_text("temporary outside fixture", encoding="utf-8")
    if kind == "manifest":
        (package / "topos.plugin.json").unlink()
        (package / "topos.plugin.json").symlink_to(sentinel)
    elif kind == "root":
        linked = tmp_path / "package_link"
        linked.symlink_to(package, target_is_directory=True)
        package = linked
    else:
        target = sentinel if kind == "file" else outside if kind == "directory" else outside / "missing"
        (package / "linked").symlink_to(target, target_is_directory=kind == "directory")
    copied = []
    parsed = []
    monkeypatch.setattr(module.shutil, "copytree", lambda *args, **kwargs: copied.append(args))
    monkeypatch.setattr(module, "parse_manifest_file", lambda *args: parsed.append(args))
    installer = _installer(tmp_path)
    with pytest.raises(InstallerError) as exc:
        installer.install(package)
    assert exc.value.code == "PACKAGE_SYMLINK_MEMBER"
    assert copied == parsed == []
    assert sentinel.read_text(encoding="utf-8") == "temporary outside fixture"
    assert not (installer.install_root / "files.my_tool").exists()


@pytest.mark.parametrize("plugin_id", ["", " ", ".", "..", "../escape", "files/escape", r"files\escape", r"C:\escape"])
def test_uninstall_invalid_identity_never_removes_install_root(tmp_path, plugin_id):
    installer = _installer(tmp_path)
    installer.install(_pkg(tmp_path))
    with pytest.raises(InstallerError) as exc:
        installer.uninstall(plugin_id, is_active=lambda _: False)
    assert exc.value.code == "PLUGIN_ID_INVALID"
    assert installer.current_version("files.my_tool") == "1.0.0"
    assert installer.installed_manifest("files.my_tool") is not None


def test_missing_data_deletion_policy_is_rejected_before_removal(tmp_path):
    installer = _installer(tmp_path)
    installer.install(_pkg(tmp_path))
    with pytest.raises(InstallerError) as exc:
        installer.uninstall("files.my_tool", is_active=lambda _: False, remove_data=True)
    assert exc.value.code == "DATA_POLICY_MISSING"
    assert installer.installed_manifest("files.my_tool") is not None
