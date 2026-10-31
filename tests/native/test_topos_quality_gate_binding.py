"""G00：质量报告必须绑定产品路径实际加载的 native 库。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from src.shared.codec.topos_binding import ToposCodec, candidate_library_paths
from tools import measure_topos_baseline_quality
from tools.measure_topos_baseline_quality import _provenance
from tools.verify_topos_quality_gates import verify_bound_codec_library


@pytest.fixture
def built_library() -> Path:
    # W01：裸取第一个存在候选会命中 `!<arch>` 伪 DLL（win-release-zig4
    # 实案，显式加载 WinError 193）——统一走三态解析器拿可加载库。
    from src.shared.codec import topos_binding
    status, lib_path, _lib = topos_binding.first_resolved_library()
    if status != "ok" or lib_path is None:
        pytest.skip(f"TOPOS native 库未构建或不可加载（{status}）")
    return Path(lib_path)


@pytest.fixture
def small_master(monkeypatch, tmp_path: Path) -> None:
    master = tmp_path / "master.bin"
    master.write_bytes(b"known source")
    monkeypatch.setattr(measure_topos_baseline_quality, "MASTER", master)


def test_explicit_missing_library_fails_closed(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TOPOS_CODEC_LIB", str(tmp_path / "missing.dylib"))
    assert any("quality-dylib-load" in item
               for item in verify_bound_codec_library({}))


def test_explicit_invalid_library_fails_closed(monkeypatch, tmp_path: Path) -> None:
    bogus = tmp_path / "topos_codec.dylib"
    bogus.write_bytes(b"invalid library content")
    monkeypatch.setenv("TOPOS_CODEC_LIB", str(bogus))
    assert any("quality-dylib-load" in item
               for item in verify_bound_codec_library({}))


def test_report_binds_loaded_library_and_rejects_sha_drift(
    monkeypatch, built_library: Path, small_master: None,
) -> None:
    monkeypatch.setenv("TOPOS_CODEC_LIB", str(built_library))
    provenance = _provenance(ToposCodec())
    assert provenance["dylib_sha256"] == hashlib.sha256(
        built_library.read_bytes()).hexdigest()
    assert verify_bound_codec_library(provenance) == []

    stale = {**provenance, "dylib_sha256": "0" * 64}
    assert any("quality-dylib-sha" in item
               for item in verify_bound_codec_library(stale))


def test_report_rejects_abi_or_build_metadata_drift(
    monkeypatch, built_library: Path, small_master: None,
) -> None:
    monkeypatch.setenv("TOPOS_CODEC_LIB", str(built_library))
    provenance = _provenance(ToposCodec())
    stale = {**provenance, "dylib_abi_version": -1,
             "dylib_build_target": "wrong"}
    violations = verify_bound_codec_library(stale)
    assert any("quality-dylib-abi" in item for item in violations)
    assert any("quality-dylib-target" in item for item in violations)
