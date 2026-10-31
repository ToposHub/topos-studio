"""M18.4b · Windows 侧 PE ABI 审计（合成 PE 头测试——Mac 开发，
机器 B 实测其 dist 产物）。"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.shared_core

from src.shared.platform.bundle_abi_audit_pe import (
    audit_bundle_pe,
    parse_pe_header,
)


def _synthetic_pe(path: Path, machine: int = 0x8664,
                  opt_magic: int = 0x20B, payload: bytes = b"x" * 64):
    """手工合成最小 PE 文件（DOS 头 + e_lfanew + PE 签名 + 文件头 +
    可选头 Magic + 载荷）。

    N02 后 COFF 声明的 SizeOfOptionalHeader 边界被校验：载荷补齐到
    声明尺寸（旧助手只写 64 字节却声明 240——在无边界检查时代通过，
    现属畸形件，故修正助手本身保持文件自洽）。
    """
    dos = bytearray(b"MZ" + b"\x00" * 62)  # DOS 头 64 字节
    pe_offset = 0x80
    struct.pack_into("<I", dos, 0x3C, pe_offset)
    opt_size = 240
    body = bytearray()
    body += b"PE\x00\x00"
    # COFF 文件头 20 字节：Machine/Sections/TimeDateStamp/SymPtr/
    # NumSymbols/SizeOfOptionalHeader/Characteristics
    body += struct.pack("<HHIIIHH", machine, 1, 0, 0, 0, opt_size, 0)
    body += struct.pack("<H", opt_magic)  # 可选头 Magic（PE32+/PE32）
    body += (payload + b"\x00" * opt_size)[:opt_size - 2]
    blob = bytes(dos) + b"\x00" * (pe_offset - len(dos)) + bytes(body)
    path.write_bytes(blob)


class TestParsePeHeader:
    def test_x64_dll_shape(self, tmp_path):
        pe = tmp_path / "lib.dll"
        _synthetic_pe(pe, machine=0x8664, opt_magic=0x20B)
        header = parse_pe_header(pe)
        assert header["pe"] is True
        assert header["cpu"] == "x86_64" and header["bits"] == 64
        assert header["optional_header"] == "PE32+"

    def test_x86_and_arm64(self, tmp_path):
        x86 = tmp_path / "x.exe"
        _synthetic_pe(x86, machine=0x14C, opt_magic=0x10B)
        h = parse_pe_header(x86)
        assert h["cpu"] == "x86" and h["bits"] == 32
        arm = tmp_path / "a.dll"
        _synthetic_pe(arm, machine=0xAA64, opt_magic=0x20B)
        assert parse_pe_header(arm)["cpu"] == "arm64"

    def test_non_pe_returns_not_pe(self, tmp_path):
        plain = tmp_path / "data.bin"
        plain.write_bytes(b"plain data" * 8)
        assert parse_pe_header(plain) == {"pe": False}

    def test_malformed_e_lfanew_raises(self, tmp_path):
        bad = tmp_path / "bad.exe"
        blob = bytearray(b"MZ" + b"\x00" * 62)
        struct.pack_into("<I", blob, 0x3C, 0x7FFFFF00)  # 越界指针
        bad.write_bytes(bytes(blob))
        with pytest.raises(ValueError):
            parse_pe_header(bad)


class TestAuditBundlePe:
    def test_consistent_single_arch_with_sha_manifest(self, tmp_path):
        pkg = tmp_path / "WinTopos" / "_internal"
        pkg.mkdir(parents=True)
        _synthetic_pe(tmp_path / "WinTopos" / "topos.exe")
        for name in ("liba.dll", "libb.dll"):
            _synthetic_pe(pkg / name)
        (pkg / "data.json").write_text("{}")  # 非 PE 跳过
        audit = audit_bundle_pe(str(tmp_path / "WinTopos"))
        assert audit["pe_count"] == 3
        assert audit["architectures"] == ["x86_64"]
        assert audit["consistent"] is True
        assert audit["skipped_non_pe"] == 1
        assert all(len(f["sha256"]) == 64 for f in audit["files"].values())

    def test_mixed_architecture_flagged(self, tmp_path):
        pkg = tmp_path / "mixed"
        pkg.mkdir()
        _synthetic_pe(pkg / "main.exe", machine=0x8664)
        _synthetic_pe(pkg / "legacy32.dll", machine=0x14C,
                      opt_magic=0x10B)
        audit = audit_bundle_pe(str(pkg))
        assert audit["consistent"] is False
        assert audit["architectures"] == ["x86", "x86_64"]
