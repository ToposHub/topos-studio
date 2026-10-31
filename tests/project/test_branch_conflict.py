"""DESK-07C · 分支冲突诊断测试：不按 mtime 裁决、不静默覆盖。"""

from __future__ import annotations

import json
import os
import zipfile

import pytest

from tests.project.package_golden import save_golden_package

from src.shared.project.branch_conflict import (
    classify_divergence,
    overwrite_guard,
    package_identity,
)

pytestmark = pytest.mark.regression


def _mutate(path: str, *, new_name: str | None = None,
            bump_modified: bool = False) -> None:
    with zipfile.ZipFile(path) as zf:
        contents = {i.filename: zf.read(i.filename)
                    for i in zf.infolist() if not i.is_dir()}
    project = json.loads(contents["project.json"])
    if new_name:
        project["name"] = new_name
        contents["project.json"] = json.dumps(project, indent=2).encode()
    if bump_modified:
        manifest = json.loads(contents["manifest.json"])
        manifest["modified_at"] = manifest.get("modified_at", 0) + 1000
        contents["manifest.json"] = json.dumps(manifest, indent=2).encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, raw in contents.items():
            zf.writestr(name, raw)


class TestClassifyDivergence:
    def test_identical(self, tmp_path) -> None:
        a = save_golden_package(str(tmp_path), "a.topos")
        b = os.path.join(str(tmp_path), "copy.topos")
        with open(a, "rb") as src, open(b, "wb") as dst:
            dst.write(src.read())
        relation, _ = classify_divergence(
            package_identity(a), package_identity(b))
        assert relation == "identical"

    def test_unrelated(self, tmp_path) -> None:
        a = save_golden_package(str(tmp_path), "a.topos")
        b = save_golden_package(str(tmp_path), "other.topos")
        # 不同 project_id（重新生成黄金包 id 相同——手工改 id）
        _mutate(b, new_name=None)
        with zipfile.ZipFile(b) as zf:
            contents = {i.filename: zf.read(i.filename)
                        for i in zf.infolist() if not i.is_dir()}
        manifest = json.loads(contents["manifest.json"])
        manifest["project_id"] = "another-project"
        contents["manifest.json"] = json.dumps(manifest).encode()
        with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, raw in contents.items():
                zf.writestr(name, raw)
        relation, _ = classify_divergence(
            package_identity(a), package_identity(b))
        assert relation == "unrelated"

    def test_diverged_without_direction(self, tmp_path) -> None:
        """同 id 不同内容 = diverged；即便一边 mtime 更晚也不裁决先后。"""
        a = save_golden_package(str(tmp_path), "a.topos")
        b = os.path.join(str(tmp_path), "b.topos")
        with open(a, "rb") as src, open(b, "wb") as dst:
            dst.write(src.read())
        _mutate(b, new_name="Phone Edit", bump_modified=True)
        relation, message = classify_divergence(
            package_identity(a), package_identity(b))
        assert relation == "diverged"
        assert "不" in message and "自动裁决" in message


class TestOverwriteGuard:
    def test_new_target_safe(self, tmp_path) -> None:
        a = save_golden_package(str(tmp_path), "a.topos")
        verdict, _ = overwrite_guard(a, os.path.join(str(tmp_path), "new.topos"))
        assert verdict == "safe"

    def test_diverged_target_warns(self, tmp_path) -> None:
        a = save_golden_package(str(tmp_path), "a.topos")
        b = os.path.join(str(tmp_path), "b.topos")
        with open(a, "rb") as src, open(b, "wb") as dst:
            dst.write(src.read())
        _mutate(b, new_name="Phone Edit")
        # 当前打开 a，保存目标是 b（同工程另一分支）→ 必须告警
        verdict, message = overwrite_guard(a, b)
        assert verdict == "diverged_warning"
        assert "备份" in message

    def test_identical_target_safe(self, tmp_path) -> None:
        a = save_golden_package(str(tmp_path), "a.topos")
        verdict, _ = overwrite_guard(a, a)
        assert verdict == "safe"

    def test_unreadable_identity_unknown(self, tmp_path) -> None:
        bad = os.path.join(str(tmp_path), "bad.topos")
        with open(bad, "wb") as fh:
            fh.write(b"not a zip")
        relation, _ = classify_divergence(package_identity(bad), None)
        assert relation == "unknown"
