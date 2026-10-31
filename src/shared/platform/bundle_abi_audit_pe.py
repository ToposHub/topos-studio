"""M18.4b · 打包库 PE ABI 审计（Windows 侧，纯 Python PE 解析）。

M18 用户确认接入 Windows 机器 B 后的 Windows 侧对应件：对 Windows
打包产物（PyInstaller onedir）审计 ``.exe``/``.dll`` 的目标架构与
sha256 manifest——与 Mac 侧 ``bundle_abi_audit``（Mach-O）同口径：

- DOS头 ``MZ`` → ``e_lfanew`` → PE 签名 ``PE\\0\\0`` → Machine 字段
  （0x8664 AMD64 / 0x14C i386 / 0xAA64 ARM64）+ 可选头 Magic（0x20B
  PE32+ / 0x10B PE32）；
- 逐文件 sha256 → manifest（与 Mac manifest 同 schema，可交叉比对）；
- 架构一致性裁决：单 bundle 混合架构即 ABI 破绽（x64 进程加载 32 位
  DLL 即崩）。

非 PE 文件跳过；畸形显式登记不静默。**本模块在 Mac 上开发、用合成
PE 头测试；实测由机器 B 对其 dist 产物运行**（接入步骤见
docs/validation/upgrade_2026-10-24/M18/machine_B/）。
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import Any, Dict, List

_PE_MACHINES = {0x8664: "x86_64", 0x14C: "x86", 0xAA64: "arm64",
                0x1C0: "arm"}
_OPT_MAGIC_NAMES = {0x20B: "PE32+", 0x10B: "PE32"}
#: N02：Machine 与可选头 Magic 的合法配对（错配 = 畸形头结构）。
_MACHINE_OPT_MAGIC = {
    0x8664: 0x20B,   # AMD64 只能 PE32+
    0xAA64: 0x20B,   # ARM64 只能 PE32+
    0x14C: 0x10B,    # i386 只能 PE32
    0x1C0: 0x10B,    # ARM Thumb-2 只能 PE32
}


def parse_pe_header(path: Path) -> Dict[str, Any]:
    """解析 PE 头（架构/位宽 + COFF 头结构校验，N02）。

    非 PE 返回 None 形态（{"pe": False} 不入 manifest）；畸形抛
    ValueError（调用方显式登记）：e_lfanew 越界、可选头越界（COFF
    声明的 SizeOfOptionalHeader 超文件）、null Machine、缺可选头
    Magic、Machine 与 PE32/PE32+ 位宽错配。
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"unreadable: {exc}") from exc
    if len(raw) < 64 or raw[:2] != b"MZ":
        return {"pe": False}
    e_lfanew = struct.unpack_from("<I", raw, 0x3C)[0]
    if e_lfanew <= 0 or e_lfanew + 24 > len(raw):
        raise ValueError(f"e_lfanew out of range: {e_lfanew}")
    if raw[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
        return {"pe": False}
    machine = struct.unpack_from("<H", raw, e_lfanew + 4)[0]
    if machine == 0:
        raise ValueError("null COFF machine type")
    opt_header_size = struct.unpack_from("<H", raw, e_lfanew + 20)[0]
    if opt_header_size < 2:
        raise ValueError(f"missing optional header: size={opt_header_size}")
    if e_lfanew + 24 + opt_header_size > len(raw):
        raise ValueError(
            f"optional header exceeds file: size={opt_header_size}, "
            f"file={len(raw)}B")
    opt_magic = struct.unpack_from("<H", raw, e_lfanew + 24)[0]
    bits = 64 if opt_magic == 0x20B else (32 if opt_magic == 0x10B else None)
    if bits is None:
        raise ValueError(f"unknown optional header magic: 0x{opt_magic:X}")
    expected_magic = _MACHINE_OPT_MAGIC.get(machine)
    if expected_magic is not None and opt_magic != expected_magic:
        raise ValueError(
            f"machine/magic mismatch: "
            f"{_PE_MACHINES.get(machine, hex(machine))} with "
            f"{_OPT_MAGIC_NAMES.get(opt_magic, hex(opt_magic))}")
    return {
        "pe": True,
        "cpu": _PE_MACHINES.get(machine, hex(machine)),
        "machine": machine,
        "bits": bits,
        "optional_header": _OPT_MAGIC_NAMES.get(opt_magic),
        "size_bytes": len(raw),
    }


def audit_bundle_pe(bundle_dir: str, *, max_files: int = 4000
                    ) -> Dict[str, Any]:
    """审计 Windows 打包目录（与 Mac 侧 audit_bundle 同 schema）。

    N02：新增 ``bundle_exists``/``scan_truncated``/``universal_binaries``
    （恒空，PE 无胖格式；保持与 Mach-O schema 对齐供 strict_verdict
    通用裁决）。
    """
    root = Path(bundle_dir)
    files: Dict[str, Any] = {}
    architectures: List[str] = []
    malformed: List[Dict[str, str]] = []
    skipped = 0
    count = 0
    truncated = False
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if count >= max_files:
            truncated = True
            continue
        rel = str(path.relative_to(root))
        try:
            header = parse_pe_header(path)
        except ValueError as exc:
            malformed.append({"path": rel, "error": str(exc)})
            continue
        if not header.get("pe"):
            skipped += 1
            continue
        count += 1
        entry = {k: v for k, v in header.items() if k != "pe"}
        entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        files[rel] = entry
        architectures.append(entry["cpu"])
    arch_set = sorted(set(architectures))
    return {
        "files": files,
        "pe_count": count,
        "architectures": arch_set,
        "consistent": len(arch_set) <= 1,
        "malformed": malformed,
        "skipped_non_pe": skipped,
        "universal_binaries": [],
        "bundle_exists": root.is_dir(),
        "scan_truncated": truncated,
    }


def strict_verdict(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """与 Mac 侧同实现的严格裁决（N02），见 bundle_abi_audit。"""
    from src.shared.platform.bundle_abi_audit import strict_verdict as _sv
    return _sv(*args, **kwargs)


__all__ = ["parse_pe_header", "audit_bundle_pe", "strict_verdict"]
