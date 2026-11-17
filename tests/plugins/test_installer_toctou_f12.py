"""F12：安装目标抵抗并发路径替换——fd 锚定、可信 root 及祖先边界证明。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.shared.plugins.installer import (
    InstallerError,
    LocalPackageInstaller,
    verify_trusted_root,
)

HOST = dict(host_api=1, topos_version="3.0.0", platform="darwin", arch="x86_64")


def _pkg(root: Path, plugin_id="files.t", version="1.0.0") -> Path:
    import json

    folder = root / f"pkg-{version}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "topos.plugin.json").write_text(json.dumps({
        "plugin_id": plugin_id, "display_name": "T", "version": version,
        "capabilities": [{"capability_id": "launch", "kind": "command"}]}),
        encoding="utf-8")
    (folder / "code.txt").write_text("payload", encoding="utf-8")
    return folder


# ---------------------------------------------------------------------------
# 可信 root 及祖先边界证明
# ---------------------------------------------------------------------------

def test_verify_trusted_root_proves_ancestor_chain(tmp_path):
    root = tmp_path / "managed" / "plugins"
    root.mkdir(parents=True)
    proof = verify_trusted_root(root, boundary=tmp_path)
    assert proof[0] == tmp_path and proof[-1] == root
    assert proof == sorted(proof, key=lambda item: len(item.parts))  # 逐级


def test_verify_trusted_root_refuses_symlinked_ancestor(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    root = link / "plugins"
    root.mkdir()
    with pytest.raises(InstallerError) as exc:
        verify_trusted_root(root, boundary=tmp_path)
    assert exc.value.code == "PATH_NOT_OWNED"
    assert "符号链接" in exc.value.message


def test_verify_trusted_root_refuses_root_outside_boundary(tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(InstallerError) as exc:
        verify_trusted_root(other, boundary=tmp_path / "managed")
    assert exc.value.code == "PATH_NOT_OWNED"


def test_verify_trusted_root_refuses_file_in_chain(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    with pytest.raises(InstallerError):
        verify_trusted_root(blocker / "plugins", boundary=tmp_path)


# ---------------------------------------------------------------------------
# TOCTOU 抵抗：核验后、使用前的并发替换被 fd 锚定拒绝
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not getattr(os, "supports_dir_fd", False),
                    reason="平台不支持 dir_fd（回退路径版，如实跳过锚定断言）")
def test_versions_swap_after_verify_is_refused_by_anchor(tmp_path, monkeypatch):
    """核验（_ensure_real_dir）与使用（锚 open）之间把 versions 换成
    符号链接——锚定 open（O_NOFOLLOW）必须失败，且不写入攻击者目录。"""
    attacker = tmp_path / "attacker-versions"
    attacker.mkdir()
    installer = LocalPackageInstaller(tmp_path / "installed", host_facts=HOST)
    original_ensure = LocalPackageInstaller._ensure_real_dir

    def ensure_then_swap(self, path, *, what):
        result = original_ensure(self, path, what=what)
        if what == "插件目录":
            # 并发攻击：在宿主核验插件目录后、使用 versions 之前，
            # 预置 versions 再替换为指向攻击者目录的符号链接
            victim = Path(path) / "versions"
            victim.mkdir(exist_ok=True)
            victim.rename(victim.with_name("versions.real"))
            victim.symlink_to(attacker, target_is_directory=True)
        return result

    monkeypatch.setattr(LocalPackageInstaller, "_ensure_real_dir",
                        ensure_then_swap)
    with pytest.raises(InstallerError) as exc:
        installer.install(_pkg(tmp_path))
    assert exc.value.code == "PATH_NOT_OWNED" or "符号链接" in str(exc.value)
    # 攻击者目录未被写入任何版本载荷
    assert list(attacker.iterdir()) == []


@pytest.mark.skipif(not getattr(os, "supports_dir_fd", False),
                    reason="平台不支持 dir_fd")
def test_plugin_root_swap_after_verify_is_refused(tmp_path, monkeypatch):
    attacker = tmp_path / "attacker-root"
    attacker.mkdir()
    installer = LocalPackageInstaller(tmp_path / "installed", host_facts=HOST)
    original_ensure = LocalPackageInstaller._ensure_real_dir

    def ensure_then_swap(self, path, *, what):
        result = original_ensure(self, path, what=what)
        if what == "插件目录":
            victim = Path(path)
            victim.rename(victim.with_name("plugin.real"))
            victim.symlink_to(attacker, target_is_directory=True)
        return result

    monkeypatch.setattr(LocalPackageInstaller, "_ensure_real_dir",
                        ensure_then_swap)
    with pytest.raises(InstallerError):
        installer.install(_pkg(tmp_path))
    assert list(attacker.iterdir()) == []


def test_anchored_install_end_to_end_still_atomic(tmp_path):
    """正常路径：锚定安装产出的布局与指针语义与既有契约一致。"""
    installer = LocalPackageInstaller(tmp_path / "installed", host_facts=HOST)
    outcome = installer.install(_pkg(tmp_path, version="1.0.0"))
    assert outcome.outcome == "installed"
    plugin_root = tmp_path / "installed" / "files.t"
    current = plugin_root / "current"
    assert current.is_symlink()
    assert os.readlink(current) == "versions/1.0.0"
    assert (plugin_root / "versions" / "1.0.0" / "code.txt").read_text(
        encoding="utf-8") == "payload"
    # 更新仍原子（旧版本保留供回滚）
    outcome2 = installer.install(_pkg(tmp_path, version="2.0.0"))
    assert outcome2.outcome == "updated"
    assert os.readlink(current) == "versions/2.0.0"
    assert (plugin_root / "versions" / "1.0.0").is_dir()


def test_source_ownership_refusals_unaffected(tmp_path):
    """源侧所有权（包/安装根重叠/符号链接）拒绝不因锚定改变。"""
    installer = LocalPackageInstaller(tmp_path / "installed", host_facts=HOST)
    linked = tmp_path / "linked-pkg"
    linked.symlink_to(_pkg(tmp_path))
    with pytest.raises(InstallerError) as exc:
        installer.install(linked)
    assert exc.value.code == "PACKAGE_SYMLINK_MEMBER"
    # 安装根位于包目录内：拒绝
    nested = _pkg(tmp_path / "nested")
    inner = LocalPackageInstaller(nested / "installed", host_facts=HOST)
    with pytest.raises(InstallerError) as exc2:
        inner.install(nested)
    assert exc2.value.code == "PACKAGE_SOURCE_OVERLAP"
