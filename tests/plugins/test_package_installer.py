"""PL-08 回归：本地包安装/更新/卸载事务——staged、可回滚、拒绝半包。"""
from __future__ import annotations

import json
import threading
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
    assert [path.name for path in versions.iterdir() if path.is_dir()] == ["1.0.0"]  # 半包目录不留下
    assert not (versions / "2.0.0.install.json").exists()           # 半包来源记录同样回滚


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


# ---------------------------------------------------------------------------
# PR-01 回归：安装根所有权、事务与版本身份
# ---------------------------------------------------------------------------

def test_versions_dir_linked_outside_never_receives_files(tmp_path):
    """复现 7：versions 指向受管根之外时，不得把版本写到链接目标。"""
    installer = _installer(tmp_path)
    plugin_root = tmp_path / "installed" / "files.probe"
    (plugin_root).mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (plugin_root / "versions").symlink_to(outside, target_is_directory=True)
    with pytest.raises(InstallerError) as exc:
        installer.install(_pkg(tmp_path, plugin_id="files.probe"))
    assert exc.value.code == "PATH_NOT_OWNED"
    assert list(outside.iterdir()) == []                       # 链接目标未收到任何文件
    assert installer.current_version("files.probe") == ""      # 也没有留下指针


def test_symlinked_install_root_is_refused_before_any_write(tmp_path):
    real = tmp_path / "real-root"
    real.mkdir()
    linked = tmp_path / "linked-root"
    linked.symlink_to(real, target_is_directory=True)
    installer = LocalPackageInstaller(linked, host_facts=HOST)
    with pytest.raises(InstallerError) as exc:
        installer.install(_pkg(tmp_path))
    assert exc.value.code == "PATH_NOT_OWNED"
    assert list(real.iterdir()) == []                          # 没有穿过链接落任何文件/锁


@pytest.mark.parametrize("target", ["../files.a/versions/1.0.0", "/tmp/escape", "versions"])
def test_foreign_or_abnormal_current_pointer_is_refused(tmp_path, target):
    """current 必须解析为本插件 versions 的直接子目录（跨插件/绝对/自指都拒绝）。"""
    installer = _installer(tmp_path)
    installer.install(_pkg(tmp_path, plugin_id="files.a", version="1.0.0"))
    other_root = tmp_path / "installed" / "files.b"
    versions = other_root / "versions"
    versions.mkdir(parents=True)
    (versions / "1.0.0").mkdir()
    pointer = other_root / "current"
    pointer.symlink_to(Path(target))
    with pytest.raises(InstallerError) as exc:
        installer.install(_pkg(tmp_path / "b", plugin_id="files.b", version="2.0.0"))
    assert exc.value.code == "POINTER_INVALID"
    assert pointer.is_symlink()                                # 指针保持原状，未被改写
    assert (versions / "2.0.0").exists() is False              # 新版本没有写入


def test_current_pointer_that_is_a_regular_file_is_refused(tmp_path):
    installer = _installer(tmp_path)
    installer.install(_pkg(tmp_path))
    pointer = tmp_path / "installed" / "files.my_tool" / "current"
    pointer.unlink()
    pointer.write_text("1.0.0", encoding="utf-8")              # 异常指针：普通文件
    with pytest.raises(InstallerError) as exc:
        installer.current_version("files.my_tool")
    assert exc.value.code == "POINTER_INVALID"


def test_prerelease_to_release_is_an_upgrade(tmp_path):
    """复现 8：完整 SemVer 顺序——预发布低于同名正式版，标识按类型比较。"""
    installer = _installer(tmp_path)
    installer.install(_pkg(tmp_path, version="1.0.0-beta.2"))
    assert installer.install(_pkg(tmp_path / "v1", version="1.0.0-beta.10")).outcome == "updated"  # 数字按数值：10 > 2
    assert installer.install(_pkg(tmp_path / "v2", version="1.0.0-rc.1")).outcome == "updated"     # 字母标识高于数字
    assert installer.install(_pkg(tmp_path / "v3", version="1.0.0")).outcome == "updated"          # 预发布 → 正式是升级
    with pytest.raises(InstallerError) as exc:
        installer.install(_pkg(tmp_path / "v4", version="1.0.0-beta.999"))                          # 回退到预发布是降级
    assert exc.value.code == "VERSION_NOT_NEWER"
    assert installer.current_version("files.my_tool") == "1.0.0"


def test_same_version_different_content_is_a_conflict(tmp_path):
    installer = _installer(tmp_path)
    installer.install(_pkg(tmp_path))
    twin = _pkg(tmp_path / "twin", version="1.0.0")
    (twin / "code.txt").write_text("different payload", encoding="utf-8")
    with pytest.raises(InstallerError) as exc:
        installer.install(twin)
    assert exc.value.code == "VERSION_CONTENT_CONFLICT"
    assert (tmp_path / "installed" / "files.my_tool" / "versions" / "1.0.0" / "code.txt"
            ).read_text(encoding="utf-8") == "static"          # 已装内容原样
    assert installer.install(_pkg(tmp_path / "same", version="1.0.0")).outcome == "unchanged"


def test_folder_sources_use_the_same_size_budget_as_zip(tmp_path, monkeypatch):
    import src.shared.plugins.installer as module
    monkeypatch.setattr(module, "MAX_MEMBER_BYTES", 1024)
    installer = _installer(tmp_path)
    heavy = _pkg(tmp_path, plugin_id="files.heavy")
    (heavy / "big.bin").write_bytes(b"\0" * 2048)
    with pytest.raises(InstallerError) as exc:
        installer.install(heavy)
    assert exc.value.code == "PACKAGE_MEMBER_TOO_LARGE"
    monkeypatch.setattr(module, "MAX_MEMBER_BYTES", 1024 * 1024)
    monkeypatch.setattr(module, "MAX_PACKAGE_BYTES", 2048)
    total = _pkg(tmp_path / "total", plugin_id="files.total")
    (total / "a.bin").write_bytes(b"\0" * 1500)
    (total / "b.bin").write_bytes(b"\0" * 1500)
    with pytest.raises(InstallerError) as exc:
        installer.install(total)
    assert exc.value.code == "PACKAGE_TOO_LARGE"
    assert not (tmp_path / "installed" / "files.total").exists()


def test_uninstall_data_deletion_failure_reports_partial_state(tmp_path):
    installer = _installer(tmp_path)
    installer.install(_pkg(tmp_path))

    def failing_delete(plugin_id: str) -> None:
        raise OSError("disk full")

    with pytest.raises(InstallerError) as exc:
        installer.uninstall("files.my_tool", is_active=lambda _: False,
                            remove_data=True, delete_data=failing_delete)
    assert exc.value.code == "DATA_DELETE_FAILED"              # 插件已移除、数据删除失败
    assert not (tmp_path / "installed" / "files.my_tool").exists()
    with pytest.raises(InstallerError) as retry:
        installer.uninstall("files.my_tool", is_active=lambda _: False)
    assert retry.value.code == "PLUGIN_NOT_INSTALLED"          # 重试卸载幂等且明确


def test_provenance_is_installer_owned_not_package_declared(tmp_path):
    installer = _installer(tmp_path)
    installer.install(_pkg(tmp_path, source="builtin"))        # 包自称 builtin
    provenance = installer.installed_provenance("files.my_tool")
    assert provenance is not None
    assert provenance["source"] == "local_package"             # 信任来自安装器，不来自声明
    assert provenance["plugin_id"] == "files.my_tool"
    assert len(provenance["content_digest"]) == 64


def test_crashed_transaction_leftovers_are_swept_with_diagnostics(tmp_path):
    installer = _installer(tmp_path)
    installer.install(_pkg(tmp_path))
    staging_junk = tmp_path / "installed" / ".staging" / "1111-deadbeef"
    staging_junk.mkdir(parents=True)
    (staging_junk / "half.txt").write_text("half", encoding="utf-8")
    stale_pointer = tmp_path / "installed" / "files.my_tool" / ".current.2222-abcd.tmp"
    stale_pointer.symlink_to(Path("versions") / "1.0.0")
    outcome = installer.install(_pkg(tmp_path / "v2", version="2.0.0"))
    assert outcome.outcome == "updated"
    assert not staging_junk.exists()                           # 崩溃暂存被清
    assert not stale_pointer.exists()                          # 崩溃临时指针被清
    assert list((tmp_path / "installed" / "files.my_tool").glob(".current.*.tmp")) == []
    assert installer.last_cleanup_notes == ()                  # 清理成功：无诊断残留


def test_concurrent_double_update_is_serialized(tmp_path):
    installer = _installer(tmp_path)
    installer.install(_pkg(tmp_path, version="1.0.0"))
    pkg2 = _pkg(tmp_path / "v2", version="2.0.0")
    results: list[str] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            results.append(installer.install(pkg2).outcome)
        except Exception as exc:  # pragma: no cover - 失败即报
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert errors == []
    assert sorted(results) == ["unchanged", "updated"]         # 串行化：一更新一幂等
    assert installer.current_version("files.my_tool") == "2.0.0"
    versions = tmp_path / "installed" / "files.my_tool" / "versions"
    assert sorted(p.name for p in versions.iterdir() if p.is_dir()) == ["1.0.0", "2.0.0"]  # 旧版本保留、无半包


def test_symlinked_transaction_lock_never_opens_outside_file(tmp_path):
    installer = _installer(tmp_path)
    installer.install_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched", encoding="utf-8")
    (installer.install_root / ".transactions.lock").symlink_to(outside)
    with pytest.raises(InstallerError) as exc:
        installer.install(_pkg(tmp_path))
    assert exc.value.code == "PATH_NOT_OWNED"
    assert outside.read_text(encoding="utf-8") == "untouched"
    assert not (installer.install_root / ".staging").exists()


def test_transaction_lock_failure_rejects_and_releases_thread_lock(tmp_path, monkeypatch):
    import src.shared.plugins.installer as module
    installer = _installer(tmp_path)
    package = _pkg(tmp_path)
    with monkeypatch.context() as patch:
        def fail_lock(*args):
            raise OSError("locking unavailable")
        patch.setattr(module.fcntl, "flock", fail_lock)
        with pytest.raises(InstallerError) as exc:
            installer.install(package)
        assert exc.value.code == "LOCK_UNAVAILABLE"
    assert installer.install(package).outcome == "installed"


def test_preexisting_provenance_symlink_cannot_overwrite_outside_file(tmp_path):
    installer = _installer(tmp_path)
    versions = installer.install_root / "files.my_tool" / "versions"
    versions.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("untouched", encoding="utf-8")
    (versions / "1.0.0.install.json").symlink_to(outside)
    with pytest.raises(InstallerError) as exc:
        installer.install(_pkg(tmp_path))
    assert exc.value.code == "PATH_NOT_OWNED"
    assert outside.read_text(encoding="utf-8") == "untouched"
    assert not (versions / "1.0.0").exists()
    assert installer.current_version("files.my_tool") == ""


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_failure_after_pointer_swap_preserves_a_resolvable_current(tmp_path, monkeypatch, rollback_fails):
    import src.shared.plugins.installer as module
    installer = _installer(tmp_path)
    installer.install(_pkg(tmp_path))
    read_pointer = installer._read_pointer
    rejected = False

    def reject_new_once(plugin_id, *, strict=True):
        nonlocal rejected
        value = read_pointer(plugin_id, strict=strict)
        if value == "2.0.0" and not rejected:
            rejected = True
            raise InstallerError("POINTER_INVALID", "injected post-swap verification failure")
        return value

    monkeypatch.setattr(installer, "_read_pointer", reject_new_once)
    replace = module.os.replace

    def replace_with_optional_failure(source, destination):
        if rollback_fails and "rollback" in str(source):
            raise OSError("injected rollback failure")
        return replace(source, destination)

    monkeypatch.setattr(module.os, "replace", replace_with_optional_failure)
    with pytest.raises(InstallerError) as exc:
        installer.install(_pkg(tmp_path / "v2", version="2.0.0"))
    assert exc.value.code == ("INSTALL_ROLLBACK_FAILED" if rollback_fails else "POINTER_INVALID")
    assert installer.current_version("files.my_tool") == ("2.0.0" if rollback_fails else "1.0.0")
    assert (installer.install_root / "files.my_tool" / "current" / "code.txt").is_file()
    assert installer.installed_manifest("files.my_tool") is not None


def test_directory_and_built_zip_have_identical_executable_payload_identity(tmp_path):
    from src.shared.plugins.pack import build_package
    installer = _installer(tmp_path)
    package = _pkg(tmp_path)
    (package / "code.txt").chmod(0o755)
    installer.install(package)
    built, _ = build_package(package, tmp_path / "package.zip", host_facts=HOST)
    assert installer.install(built).outcome == "unchanged"


@pytest.mark.parametrize("version", ["01.0.0", "1.0.0-01", "1.0.0-a..b", "1.0.0-.", "1.0.0+a..b"])
def test_invalid_semver_is_rejected_even_on_first_install(tmp_path, version):
    installer = _installer(tmp_path)
    with pytest.raises(InstallerError) as exc:
        installer.install(_pkg(tmp_path, version=version))
    assert exc.value.code in {"VERSION_INVALID", "MANIFEST_INVALID", "MANIFEST_REJECTED"}
    assert installer.current_version("files.my_tool") == ""


def test_install_root_inside_source_is_rejected_before_staging(tmp_path):
    package = _pkg(tmp_path)
    installer = LocalPackageInstaller(package / "installed", host_facts=HOST)
    with pytest.raises(InstallerError) as exc:
        installer.install(package)
    assert exc.value.code == "PACKAGE_SOURCE_OVERLAP"
    assert not installer.install_root.exists()


def test_stale_staging_cleanup_failure_remains_visible(tmp_path, monkeypatch):
    import src.shared.plugins.installer as module
    installer = _installer(tmp_path)
    installer.install(_pkg(tmp_path))
    stale = installer.install_root / ".staging" / "stale"
    stale.mkdir()
    real_remove = module.shutil.rmtree

    def fail_one(path, *args, **kwargs):
        if Path(path) == stale:
            raise OSError("denied stale cleanup")
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(module.shutil, "rmtree", fail_one)
    assert installer.install(_pkg(tmp_path / "v2", version="2.0.0")).outcome == "updated"
    assert any("stale" in note and "denied stale cleanup" in note for note in installer.last_cleanup_notes)


def test_retry_after_crash_between_version_move_and_pointer_swap(tmp_path):
    import shutil
    installer = _installer(tmp_path)
    installer.install(_pkg(tmp_path))
    package = _pkg(tmp_path / "v2", version="2.0.0")
    # Simulate a process killed after rename(version) but before provenance/current.
    orphan = installer.install_root / "files.my_tool" / "versions" / "2.0.0"
    shutil.copytree(package, orphan)
    assert installer.install(package).outcome == "updated"
    assert installer.current_version("files.my_tool") == "2.0.0"
    assert installer.installed_provenance("files.my_tool")["version"] == "2.0.0"


def test_explicit_downgrade_can_reuse_unchanged_retained_version(tmp_path):
    installer = _installer(tmp_path)
    old = _pkg(tmp_path)
    installer.install(old)
    installer.install(_pkg(tmp_path / "v2", version="2.0.0"))
    assert installer.install(old, allow_downgrade=True).outcome == "updated"
    assert installer.current_version("files.my_tool") == "1.0.0"


def test_same_version_never_uses_stale_digest_to_hide_installed_corruption(tmp_path):
    installer = _installer(tmp_path)
    package = _pkg(tmp_path)
    installer.install(package)
    installed = installer.install_root / "files.my_tool" / "versions" / "1.0.0" / "code.txt"
    installed.write_text("corrupted after installation", encoding="utf-8")
    with pytest.raises(InstallerError) as exc:
        installer.install(package)
    assert exc.value.code == "PROVENANCE_INVALID"
