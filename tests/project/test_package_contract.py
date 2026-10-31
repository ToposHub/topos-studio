"""DESK-01A/08A · .topos 包契约合同测试（黄金包 + 规范哈希 + 版本分级）。

验收锚点（docs/adr/0005）：
- 黄金包由生产 save 路径产出，逐条目通过 ``package_spec`` 冻结 schema
  ——证明"schema 与生产 serializer 一致"；
- ``spec_digest`` 金丝雀钉死契约哈希——任何字段/版本变更必须显式更新
  该值（= 有意识的契约变更，不允许静默漂移）；
- 更高版本包保存不降级、未知键/未知模块 load→save 存活；
- ``classify_package_versions`` 的 current/legacy/future/unreadable 分级
  供 DESK-01D 加载预检消费。
"""

from __future__ import annotations

import json
import os
import zipfile

import pytest

from tests.project.package_golden import save_golden_package

from src.shared.contracts import timebase as tb
from src.shared.project import package_spec as spec
from src.shared.project.payload_cache import ProjectArchivePayload
from src.shared.project.topos_project_io import ToposProjectIO

pytestmark = pytest.mark.regression


def _read_entry(path: str, name: str) -> dict:
    with zipfile.ZipFile(path) as zf:
        return json.loads(zf.read(name))


def _entry_names(path: str) -> set[str]:
    with zipfile.ZipFile(path) as zf:
        return {info.filename for info in zf.infolist() if not info.is_dir()}


def _rewrite_entry(path: str, name: str, data: dict) -> None:
    """在既有包内改写一个 JSON 条目（其余字节原样保留）。"""
    with zipfile.ZipFile(path) as zf:
        contents = {info.filename: zf.read(info.filename)
                    for info in zf.infolist() if not info.is_dir()}
    contents[name] = json.dumps(data, indent=2, ensure_ascii=False).encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry_name, raw in contents.items():
            zf.writestr(entry_name, raw)


@pytest.fixture(scope="module")
def golden_path(tmp_path_factory) -> str:
    return save_golden_package(
        str(tmp_path_factory.mktemp("golden")), "golden.topos")


class TestSpecDigestCanary:
    def test_digest_matches_pinned_value(self) -> None:
        """契约金丝雀：冻结规范的 SHA-256。

        该值变化 = 包契约变化（字段 schema / 版本 / URI 文法任一改动）。
        有意变更时：先在 ADR 0005 记录决策，再更新此处的钉住值。
        """
        assert spec.spec_digest() == (
            "05479f77c8c08a170877ae788bc63f54f993bdfaa2c5946360ec6fa23aef25f0"
        )

    def test_digest_is_deterministic(self) -> None:
        assert spec.spec_digest() == spec.spec_digest()

    def test_timebase_vectors_digest_recorded(self) -> None:
        """向量集摘要随契约测试登记（双端核对向量一致性用）。"""
        vectors = tb.load_vectors_file()
        digest = tb.vectors_digest(vectors)
        assert len(digest) == 64
        # 2026-10-25 冻结值；加向量点（规则不变）时同步更新
        assert digest.startswith("7018f73246cf080f")


class TestGoldenPackageConformance:
    """黄金包：生产 save 真实产出 → 冻结 schema 全过。"""

    def test_all_frozen_entries_conform(self, golden_path: str) -> None:
        names = _entry_names(golden_path)
        # 黄金包必须覆盖冻结条目的核心集（无资源索引是合法旧形态）
        for required in (
            "manifest.json", "project.json",
            "edit/media_pool.json", "edit/bins.json",
            "edit/timelines.json", "edit/timeline_sequences.json",
            "cache_manifest.json",
        ):
            assert required in names
        checked = 0
        for name in names:
            if name not in spec.PACKAGE_FIELD_SCHEMAS:
                continue
            data = _read_entry(golden_path, name)
            errors = spec.validate_payload(name, data)
            assert not errors, f"{name} 违反冻结 schema: {errors}"
            checked += 1
        assert checked >= 7

    def test_manifest_versions_are_frozen(self, golden_path: str) -> None:
        manifest = _read_entry(golden_path, "manifest.json")
        assert manifest["schema_version"] == spec.PACKAGE_SCHEMA_VERSION == 3
        assert manifest["edit_content_schema_version"] == \
            spec.EDIT_CONTENT_SCHEMA_VERSION == 4
        assert manifest["format"] == "topos_project_archive"
        assert manifest["project_mode"] == "edit_project"
        assert "edit" in manifest["present_modules"]

    def test_timeline_fps_is_rational_string(self, golden_path: str) -> None:
        """fps 有理数字符串契约（跨端时间精度之根，不得退化为 float）。"""
        sequences = _read_entry(golden_path, "edit/timeline_sequences.json")
        main = sequences["golden-timeline-main"]
        assert main["fps"] == "30000/1001"
        # schema 层面也必须拒绝 float 形态
        broken = {k: dict(v) for k, v in sequences.items()}
        broken["golden-timeline-main"]["fps"] = 29.97002997002997
        errs = spec.validate_payload(
            "edit/timeline_sequences.json", broken)
        assert any("fps" in e for e in errs)

    def test_production_reload(self, golden_path: str) -> None:
        """生产 load 路径可读回黄金包，完整数据齐备。"""
        project, full_data = ToposProjectIO.load(golden_path)
        assert project is not None
        assert project.project_id == "golden-desk01a-0001"
        assert full_data is not None
        assert set(full_data.get("timeline_sequences", {})) == \
            {"golden-timeline-main"}
        assert "assets" in full_data and "bins" in full_data


class TestPayloadPassthrough:
    """原始载荷层往返：另一端（Dart）读写等价性的桌面参照。"""

    def test_payload_roundtrip_preserves_all_entries(self, tmp_path) -> None:
        src = save_golden_package(str(tmp_path), "a.topos")
        payload = ProjectArchivePayload.load_from_file(src)
        dst = os.path.join(str(tmp_path), "b.topos")
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
            payload.write_to_zip(zf)

        with zipfile.ZipFile(src) as za, zipfile.ZipFile(dst) as zb:
            names_a = {i.filename for i in za.infolist() if not i.is_dir()}
            names_b = {i.filename for i in zb.infolist() if not i.is_dir()}
            assert names_a == names_b
            for name in sorted(names_a):
                assert za.read(name) == zb.read(name), name


class TestUnknownFieldSurvival:
    """未知键/未知模块经生产 save 存活（DESK-04A 的基础钉住）。"""

    def test_unknown_manifest_and_module_survive_save(self, tmp_path) -> None:
        path = save_golden_package(str(tmp_path), "unknown.topos")
        # 注入未知 manifest 键 + 未来模块 + 未知 JSON 条目
        manifest = _read_entry(path, "manifest.json")
        manifest["future_writer_key_v9"] = {"provenance": "mobile-2077"}
        _rewrite_entry(path, "manifest.json", manifest)
        with zipfile.ZipFile(path) as zf:
            contents = {i.filename: zf.read(i.filename)
                        for i in zf.infolist() if not i.is_dir()}
        contents["future_module/data.json"] = json.dumps(
            {"schema": 9, "payload": [1, 2]}).encode()
        contents["assets/binary_blob.bin"] = b"\x00\x01\x02golden"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, raw in contents.items():
                zf.writestr(name, raw)

        # 生产 save（覆盖同一路径）必须保留全部未知内容
        from tests.project.package_golden import golden_save_kwargs
        kwargs = golden_save_kwargs()
        assert ToposProjectIO.save(kwargs.pop("project"), path, **kwargs)

        assert _read_entry(path, "manifest.json").get(
            "future_writer_key_v9") == {"provenance": "mobile-2077"}
        with zipfile.ZipFile(path) as zf:
            assert json.loads(zf.read("future_module/data.json")) == \
                {"schema": 9, "payload": [1, 2]}
            assert zf.read("assets/binary_blob.bin") == b"\x00\x01\x02golden"


class TestFutureVersionNoDowngrade:
    def test_future_versions_survive_save(self, tmp_path) -> None:
        path = save_golden_package(str(tmp_path), "future.topos")
        manifest = _read_entry(path, "manifest.json")
        manifest["schema_version"] = spec.PACKAGE_SCHEMA_VERSION + 7
        manifest["edit_content_schema_version"] = \
            spec.EDIT_CONTENT_SCHEMA_VERSION + 3
        _rewrite_entry(path, "manifest.json", manifest)

        from tests.project.package_golden import golden_save_kwargs
        kwargs = golden_save_kwargs()
        assert ToposProjectIO.save(kwargs.pop("project"), path, **kwargs)

        saved = _read_entry(path, "manifest.json")
        assert saved["schema_version"] == spec.PACKAGE_SCHEMA_VERSION + 7
        assert saved["edit_content_schema_version"] == \
            spec.EDIT_CONTENT_SCHEMA_VERSION + 3


class TestVersionClassification:
    """DESK-01D 预检消费的版本分级。"""

    def _classify(self, schema_version, edit_version=None):
        manifest = {"schema_version": schema_version}
        if edit_version is not None:
            manifest["edit_content_schema_version"] = edit_version
        return spec.classify_package_versions(manifest)

    def test_current(self) -> None:
        status = self._classify(3, 4)
        assert status["package"].status == "current"
        assert status["edit_content"].status == "current"
        assert status["package"].is_writable_without_downgrade

    def test_legacy(self) -> None:
        status = self._classify(2, 1)
        assert status["package"].status == "legacy"
        assert status["edit_content"].status == "legacy"

    def test_future(self) -> None:
        status = self._classify(99, 99)
        assert status["package"].status == "future"
        assert status["edit_content"].status == "future"

    def test_unreadable(self) -> None:
        status = self._classify(0, 0)
        assert status["package"].status == "unreadable"
        assert status["edit_content"].status == "unreadable"
        assert not status["package"].is_writable_without_downgrade

    def test_missing_resource_index_is_legacy(self) -> None:
        status = spec.classify_package_versions({"schema_version": 3})
        assert status["resource_index"].status == "legacy"
        assert status["resource_index"].file_version == 0

    def test_resource_index_version(self) -> None:
        status = spec.classify_package_versions(
            {"schema_version": 3}, resource_index_version=1)
        assert status["resource_index"].status == "current"
        future = spec.classify_package_versions(
            {"schema_version": 3}, resource_index_version=2)
        assert future["resource_index"].status == "future"


class TestResourceUriGrammar:
    @pytest.mark.parametrize("uri", [
        "topos-res://res-1",
        "topos-res://abc",
        "topos-res://sha256-" + "a" * 64,
    ])
    def test_valid(self, uri: str) -> None:
        assert spec.is_valid_resource_uri(uri)

    @pytest.mark.parametrize("uri", [
        "", "topos://res-1", "topos-res://", "topos-res://sha256-abc",
        "topos-res://sha256-" + "A" * 64, "topos-res://res/1",
        "topos-res://res 1", "file://x",
    ])
    def test_invalid(self, uri: str) -> None:
        assert not spec.is_valid_resource_uri(uri)


class TestValidatorPrimitives:
    def test_bool_is_not_integer_or_number(self) -> None:
        """bool 不算 integer/number（Dart 侧同语义）。"""
        schema = {"type": "object", "properties": {
            "schema_version": {"type": "integer"}}}
        errors: list[str] = []
        spec._validate({"schema_version": True}, schema, "x", errors)
        assert errors

    def test_unknown_keys_pass(self) -> None:
        data = _read_entry_manifest_minimal()
        data["anything_unknown"] = {"deep": [1, 2, {"x": None}]}
        assert spec.validate_payload("manifest.json", data) == []

    def test_missing_required_reported(self) -> None:
        data = _read_entry_manifest_minimal()
        del data["project_id"]
        errors = spec.validate_payload("manifest.json", data)
        assert any("project_id" in e for e in errors)


def _read_entry_manifest_minimal() -> dict:
    return {
        "format": "topos_project_archive",
        "schema_version": 3,
        "project_id": "p1",
        "project_mode": "edit_project",
    }
