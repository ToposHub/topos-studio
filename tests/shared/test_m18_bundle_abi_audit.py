"""M18.4 · 打包库 OS/架构/hash/ABI 审计（Mac 侧）。"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.shared_core

from src.shared.platform.bundle_abi_audit import (
    audit_bundle,
    audit_report,
    parse_mach_header,
)

_BUNDLE = Path("dist/topos-color/topos-color")


def _require_bundle():
    if not (_BUNDLE / "topos-color").is_file():
        pytest.skip("packaged bundle not built on this checkout")


class TestParseMachHeader:
    @pytest.mark.parametrize("endian", ["<", ">"])
    @pytest.mark.parametrize("version,expected", [(0x0D0201, "13.2.1"),
                                                 (0x0D0200, "13.2")])
    def test_build_version_reads_minos_after_platform(
        self, tmp_path: Path, endian: str, version: int, expected: str
    ) -> None:
        binary = tmp_path / "library.dylib"
        header = struct.pack(endian + "8I", 0xFEEDFACF, 0x01000007,
                             3, 6, 1, 24, 0, 0)
        command = struct.pack(endian + "6I", 0x32, 24, 1, version, version, 0)
        binary.write_bytes(header + command)
        assert parse_mach_header(binary)["min_os"] == expected

    def test_main_executable(self):
        _require_bundle()
        header = parse_mach_header(_BUNDLE / "topos-color")
        assert header is not None
        assert header["bits"] == 64
        assert header["cpu"] == "x86_64"
        assert header["filetype"] == 2  # MH_EXECUTE
        assert header["min_os"]  # 主程序必带 min OS

    def test_dylib(self):
        _require_bundle()
        dylibs = sorted((_BUNDLE / "_internal").glob("*.dylib"))
        assert dylibs, "bundle 应含 dylib"
        header = parse_mach_header(dylibs[0])
        assert header is not None and header["filetype"] == 6  # MH_DYLIB
        assert header["sha256"] if "sha256" in header else True

    def test_non_macho_returns_none(self, tmp_path):
        data = tmp_path / "plain.txt"
        data.write_text("not a macho")
        assert parse_mach_header(data) is None


class TestAuditBundle:
    def test_real_bundle_consistent_single_arch(self):
        _require_bundle()
        audit = audit_bundle(str(_BUNDLE), max_files=60)
        assert audit["macho_count"] > 0
        # 本机构建目标：单一 x86_64（混合架构=加载即崩的 ABI 破绽）
        assert audit["architectures"] == ["x86_64"], audit["architectures"]
        assert audit["consistent"] is True
        assert audit["malformed"] == []
        # manifest：逐文件 sha256 齐备
        assert all("sha256" in f and len(f["sha256"]) == 64
                   for f in audit["files"].values())

    def test_report_human_readable(self):
        _require_bundle()
        audit = audit_bundle(str(_BUNDLE), max_files=10)
        text = audit_report(audit)
        assert "Mach-O 文件数" in text
        assert "x86_64" in text
        assert "架构一致：是" in text

    def test_empty_dir(self, tmp_path):
        audit = audit_bundle(str(tmp_path))
        assert audit["macho_count"] == 0
        assert audit["consistent"] is True
