"""PL-10 回归：作者侧一键校验与确定性打包。"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src.shared.plugins.pack import (
    build_package,
    package_digest,
    validate_package_folder,
    verify_package,
)

HOST = dict(host_api=1, topos_version="1.6.0", platform="darwin", arch="x86_64")


def _folder(root: Path, *, plugin_id="files.dev_tool", version="0.1.0") -> Path:
    folder = root / "dev_tool"
    folder.mkdir(parents=True)
    (folder / "topos.plugin.json").write_text(json.dumps({
        "plugin_id": plugin_id, "display_name": "Dev Tool", "version": version,
        "source": "local_dev",
        "capabilities": [{"capability_id": "launch", "kind": "command"}],
    }), encoding="utf-8")
    (folder / "main.py").write_text("print('never executed by tooling')\n", encoding="utf-8")
    return folder


def test_validate_report_flags_errors_warnings_and_entries(tmp_path):
    folder = _folder(tmp_path)
    (folder / "__pycache__").mkdir()
    (folder / "__pycache__" / "main.pyc").write_bytes(b"junk")
    report = validate_package_folder(folder, host_facts=HOST)
    assert report.ok is True                              # 派生文件只是警告
    assert any(item.code == "JUNK_ENTRY" and item.severity == "warning"
               for item in report.diagnostics)
    assert "topos.plugin.json" in report.entry_files
    missing = validate_package_folder(tmp_path / "nope", host_facts=HOST)
    assert missing.ok is False
    assert any(item.code == "FOLDER_MISSING" for item in missing.diagnostics)


def test_strict_build_refuses_invalid_package(tmp_path):
    folder = _folder(tmp_path, plugin_id="BAD ID")
    with pytest.raises(ValueError, match="拒绝构建"):
        build_package(folder, tmp_path / "out.zip", host_facts=HOST)
    assert not (tmp_path / "out.zip").exists()


def test_build_is_deterministic_and_sidecar_verifies(tmp_path):
    folder = _folder(tmp_path)
    zip_a, report_a = build_package(folder, tmp_path / "a.zip", host_facts=HOST)
    zip_b, _ = build_package(folder, tmp_path / "b.zip", host_facts=HOST)
    assert zip_a.read_bytes() == zip_b.read_bytes()       # 同输入同字节
    assert report_a.content_digest == package_digest(zip_a)
    assert verify_package(zip_a) is True                  # sidecar 校验通过
    zip_a.write_bytes(zip_a.read_bytes() + b"tampered")
    assert verify_package(zip_a) is False


def test_built_package_installs_through_installer(tmp_path):
    from src.shared.plugins.installer import LocalPackageInstaller
    folder = _folder(tmp_path, version="1.0.0")
    package, _ = build_package(folder, tmp_path / "tool.zip", host_facts=HOST)
    with zipfile.ZipFile(package) as archive:
        assert archive.namelist() == ["main.py", "topos.plugin.json"]  # 排序稳定
    installer = LocalPackageInstaller(tmp_path / "installed", host_facts=HOST)
    outcome = installer.install(package)
    assert outcome.outcome == "installed"
    assert installer.installed_manifest("files.dev_tool").version == "1.0.0"
