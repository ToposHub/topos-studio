"""N02 · 严格 ABI 与复验裁决测试。

覆盖审计指出的假绿路径：空/不存在目录、畸形库、扫描截断、胖二进制
静默跳过、load command 越界静默 break、COFF 可选头越界、
machine/位宽错配、min OS 只收集不裁决、B 复验脚本宿主/超时/未执行
分级与三链如实声明。
"""

from __future__ import annotations

import argparse
import importlib.util
import struct
import sys
from pathlib import Path
from types import ModuleType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.platform.bundle_abi_audit import (
    audit_bundle, parse_mach_header, strict_verdict)
from src.shared.platform.bundle_abi_audit_pe import (
    audit_bundle_pe, parse_pe_header)

ROOT = Path(__file__).resolve().parents[2]


def _macho(path: Path, *, filetype: int = 6, ncmds: int = 1,
           commands: bytes = b"", cputype: int = 0x01000007) -> None:
    """合成 64 位小端 Mach-O（header + 任意 load commands）。"""
    header = struct.pack("<8I", 0xFEEDFACF, cputype, 3, filetype,
                         ncmds, len(commands), 0, 0)
    path.write_bytes(header + commands)


def _lc_build_version(minos: int = 0x0D0200) -> bytes:
    # cmd/cmdsize/platform/minos/sdk/ntools
    return struct.pack("<6I", 0x32, 24, 1, minos, minos, 0)


def _pe(path: Path, machine: int = 0x8664, opt_magic: int = 0x20B,
        declared_opt_size: int = 240, actual_payload: int | None = None
        ) -> None:
    """合成 PE。declared_opt_size 与实际可选头内容可分离——构造
    "COFF 声明越界"的畸形件（actual_payload < declared_opt_size-2）。
    """
    dos = bytearray(b"MZ" + b"\x00" * 62)
    pe_offset = 0x80
    struct.pack_into("<I", dos, 0x3C, pe_offset)
    payload_len = (declared_opt_size - 2 if actual_payload is None
                   else actual_payload)
    body = struct.pack("<HHIIIHH", machine, 1, 0, 0, 0,
                       declared_opt_size, 0)
    body += struct.pack("<H", opt_magic)
    body += b"x" * payload_len
    blob = bytes(dos) + b"\x00" * (pe_offset - len(dos)) + b"PE\x00\x00" + body
    path.write_bytes(blob)


# ---------- Mach-O 头结构边界 ----------

class TestMachHeaderBounds:
    def test_fat_magic_is_universal_not_skipped(self, tmp_path: Path) -> None:
        fat = tmp_path / "fat.dylib"
        fat.write_bytes(struct.pack(">I", 0xCAFEBABE) + b"\x00" * 60)
        header = parse_mach_header(fat)
        assert header is not None and header.get("universal") is True

    def test_truncated_load_commands_raise(self, tmp_path: Path) -> None:
        # 声明 ncmds=2，文件只容纳 1 条命令 → 必须显式畸形
        binary = tmp_path / "trunc.dylib"
        _macho(binary, ncmds=2, commands=_lc_build_version())
        with pytest.raises(ValueError, match="truncated"):
            parse_mach_header(binary)

    def test_cmdsize_overflow_raises(self, tmp_path: Path) -> None:
        # cmdsize 声明远超文件长度
        bad_lc = struct.pack("<II", 0x0D, 0x7FFFFF)  # LC_ID_DYLIB
        binary = tmp_path / "overflow.dylib"
        _macho(binary, ncmds=1, commands=bad_lc)
        with pytest.raises(ValueError, match="exceeds file"):
            parse_mach_header(binary)

    def test_implausible_ncmds_raises(self, tmp_path: Path) -> None:
        binary = tmp_path / "ncmds.dylib"
        _macho(binary, ncmds=1_000_000, commands=b"")
        with pytest.raises(ValueError, match="ncmds"):
            parse_mach_header(binary)


# ---------- PE COFF 头结构边界 ----------

class TestPeHeaderBounds:
    def test_optional_header_exceeding_file_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.dll"
        _pe(bad, declared_opt_size=240, actual_payload=10)  # 声明超实际
        with pytest.raises(ValueError, match="optional header exceeds"):
            parse_pe_header(bad)

    def test_machine_magic_mismatch_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "mismatch.dll"
        _pe(bad, machine=0x8664, opt_magic=0x10B)  # AMD64 + PE32
        with pytest.raises(ValueError, match="machine/magic mismatch"):
            parse_pe_header(bad)

    def test_null_machine_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "nullmachine.dll"
        _pe(bad, machine=0x0)
        with pytest.raises(ValueError, match="null COFF machine"):
            parse_pe_header(bad)

    def test_missing_optional_header_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "noopt.dll"
        _pe(bad, declared_opt_size=0)
        with pytest.raises(ValueError, match="missing optional header"):
            parse_pe_header(bad)


# ---------- 严格裁决（两平台同核心） ----------

class TestStrictVerdictFailClosed:
    def test_missing_dir_fails(self, tmp_path: Path) -> None:
        audit = audit_bundle(str(tmp_path / "nope"))
        assert audit["bundle_exists"] is False
        verdict = strict_verdict(audit)
        assert verdict["strict_pass"] is False
        assert "bundle_dir_missing" in verdict["reasons"]

    def test_empty_dir_fails_not_consistent_green(self, tmp_path: Path) -> None:
        audit = audit_bundle(str(tmp_path))
        assert audit["consistent"] is True  # 旧字段确实会假绿
        verdict = strict_verdict(audit)
        assert verdict["strict_pass"] is False
        assert "no_native_binaries" in verdict["reasons"]

    def test_empty_pe_dir_fails(self, tmp_path: Path) -> None:
        audit = audit_bundle_pe(str(tmp_path))
        assert audit["consistent"] is True
        assert strict_verdict(audit)["strict_pass"] is False

    def test_malformed_fails(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        _macho(pkg / "good.dylib", commands=_lc_build_version())
        _macho(pkg / "bad.dylib", ncmds=2, commands=_lc_build_version())
        audit = audit_bundle(str(pkg))
        assert len(audit["malformed"]) == 1
        verdict = strict_verdict(audit)
        assert verdict["strict_pass"] is False
        assert any(r.startswith("malformed:") for r in verdict["reasons"])

    def test_scan_truncation_fails(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        for i in range(3):
            _macho(pkg / f"lib{i}.dylib", commands=_lc_build_version())
        audit = audit_bundle(str(pkg), max_files=2)
        assert audit["scan_truncated"] is True
        verdict = strict_verdict(audit)
        assert "scan_truncated" in verdict["reasons"]

    def test_pe_scan_truncation_fails(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        for i in range(3):
            _pe(pkg / f"lib{i}.dll")
        audit = audit_bundle_pe(str(pkg), max_files=2)
        assert audit["scan_truncated"] is True
        assert "scan_truncated" in strict_verdict(audit)["reasons"]

    def test_universal_binary_fails_single_arch_distribution(
        self, tmp_path: Path
    ) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        _macho(pkg / "thin.dylib", commands=_lc_build_version())
        fat = pkg / "fat.dylib"
        fat.write_bytes(struct.pack(">I", 0xCAFEBABE) + b"\x00" * 64)
        audit = audit_bundle(str(pkg))
        assert audit["universal_binaries"] == ["fat.dylib"]
        assert "fat.dylib" not in audit["files"]  # 不算已审 Mach-O
        reasons = strict_verdict(audit)["reasons"]
        assert any(r.startswith("universal_binaries:") for r in reasons)
        # 按分发声明允许 universal 时不因此项失败
        relaxed = strict_verdict(audit, allow_universal=True)
        assert not any(r.startswith("universal_binaries:")
                       for r in relaxed["reasons"])

    def test_arch_mismatch_fails(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        _macho(pkg / "arm.dylib", cputype=0x0100000C,
               commands=_lc_build_version())
        audit = audit_bundle(str(pkg))
        reasons = strict_verdict(audit, expect_arch="x86_64")["reasons"]
        assert any(r.startswith("arch_mismatch:") for r in reasons)

    def test_missing_required_path_fails(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        _macho(pkg / "lib.dylib", commands=_lc_build_version())
        audit = audit_bundle(str(pkg))
        reasons = strict_verdict(
            audit, required_paths=("Contents/MacOS/topos-color",))["reasons"]
        assert "missing_required_path:Contents/MacOS/topos-color" in reasons

    def test_min_os_exceeds_target_fails(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        _macho(pkg / "new.dylib", commands=_lc_build_version(0x0E0000))
        audit = audit_bundle(str(pkg))
        assert audit["files"]["new.dylib"]["min_os"] == "14.0"
        reasons = strict_verdict(audit, max_min_os="13.0")["reasons"]
        assert any(r.startswith("min_os_exceeds_target:") for r in reasons)
        # 恰等于目标 → 通过该项
        ok = strict_verdict(audit, max_min_os="14.0")
        assert not any(r.startswith("min_os_exceeds_target:")
                       for r in ok["reasons"])

    def test_good_bundle_passes(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        _macho(pkg / "main", filetype=2, commands=_lc_build_version(0x0C0000))
        _macho(pkg / "lib.dylib", commands=_lc_build_version(0x0C0000))
        audit = audit_bundle(str(pkg))
        verdict = strict_verdict(
            audit, required_paths=("main",), expect_arch="x86_64",
            max_min_os="12.0")
        assert verdict == {"strict_pass": True, "reasons": []}

    def test_good_pe_bundle_passes(self, tmp_path: Path) -> None:
        pkg = tmp_path / "WinTopos"
        internal = pkg / "_internal"
        internal.mkdir(parents=True)
        _pe(pkg / "topos.exe")
        _pe(internal / "lib.dll")
        audit = audit_bundle_pe(str(pkg))
        verdict = strict_verdict(
            audit, required_paths=("topos.exe",), expect_arch="x86_64")
        assert verdict["strict_pass"] is True, verdict["reasons"]


# ---------- B 复验脚本（宿主/超时/分级/三链声明） ----------

def _load_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "m18_machine_b_recheck_n02",
        ROOT / "scripts" / "m18_machine_b_recheck.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMachineBRecheck:
    def test_git_fingerprint_records_commit(self) -> None:
        gate = _load_gate()
        fp = gate._git_fingerprint()
        assert fp["commit"] not in ("", "unknown")
        assert len(fp["commit"]) == 40
        assert isinstance(fp["dirty_files"], list)

    def test_timeout_is_distinct_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gate = _load_gate()
        monkeypatch.setattr(gate, "_GATE_TIMEOUT_S", 0.5)
        result = gate._run([sys.executable, "-c", "import time;"
                            " time.sleep(5)"])
        assert result["status"] == "timeout"
        assert result["passed"] is False
        assert result["error"]

    def test_wrong_host_fails_fast_all_not_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gate = _load_gate()
        monkeypatch.setattr(gate, "OUT_DIR", tmp_path)
        monkeypatch.setattr(gate, "_run", lambda *a, **k: pytest.fail(
            "gates must not execute on wrong host"))
        if gate.platform.system() == "Windows":
            pytest.skip("host is Windows; wrong-host path untestable here")
        report, ok = gate._collect(
            argparse.Namespace(dist=None, main_exe=None))
        assert ok is False
        assert report["overall"] == "FAIL"
        assert report["host_check"]["passed"] is False
        for name in ("focused_gate", "golden_chain3", "pe_audit_unit",
                     "probe_s", "probe_r"):
            assert report[name]["status"] == "not_run", name
        assert report["golden_chains_claim"]["three_chains_verified"] is False

    def test_no_dist_means_installer_gate_not_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gate = _load_gate()
        monkeypatch.setattr(gate, "OUT_DIR", tmp_path)
        monkeypatch.chdir(ROOT)  # 门禁入口存在性检查用相对路径
        fp = {
            "machine": "AMD64", "system": "Windows", "release": "10",
            "version": "10.0", "python": sys.version,
            "git": gate._git_fingerprint(), "argv_dist": None,
            "captured_at": "test",
        }
        monkeypatch.setattr(gate, "_fingerprint", lambda: fp)
        monkeypatch.setattr(gate, "_run", lambda cmd, probe=False: {
            "status": "passed", "passed": True, "cmd": "stub"})
        report, ok = gate._collect(
            argparse.Namespace(dist=None, main_exe=None))
        assert ok is True
        assert report["pe_abi_audit"]["status"] == "not_run"
        assert "installer_gate_claim" in report
        assert report["golden_chains_claim"]["verified"] == ["chain3"]
        assert report["golden_chains_claim"]["three_chains_verified"] is False

    def test_main_exe_detection(self, tmp_path: Path) -> None:
        gate = _load_gate()
        assert gate._detect_main_exe(str(tmp_path)) is None
        (tmp_path / "one.exe").write_bytes(b"x")
        assert gate._detect_main_exe(str(tmp_path)) == "one.exe"
        (tmp_path / "two.exe").write_bytes(b"x")
        assert gate._detect_main_exe(str(tmp_path)) is None
