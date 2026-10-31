"""M18.4 · 打包库 OS/架构/hash/ABI 审计（Mac 侧，纯 Python Mach-O 解析）。

M18 实施："验证打包库的 OS/架构/hash/ABI"。Windows 侧 ABI 需机器 B
（deferred_gates 在案）；本模块对**本机打包产物**（PyInstaller onedir
布局）自动化审计：

- 遍历 bundle 内全部 Mach-O（主可执行 + ``*.dylib`` + ``*.so`` +
  无扩展名 ELF/Mach-O 垾件）；
- 解析 Mach-O 头（magic → 64/32 位；cputype → x86_64/arm64/...）
  与 ``LC_BUILD_VERSION``/``LC_VERSION_MIN_*`` 的 min OS；
- 逐文件 sha256 → manifest（可随交付归档、复验比对）；
- 一致性裁决：单一目标架构（混合架构 dylib 在同 bundle 中即 ABI 破绽
  ——加载即崩）与 min OS 不高于构建声明目标；
- 严格裁决 ``strict_verdict``（N02）：``consistent`` 只说明架构集合
  不混——空目录/目录不存在/畸形/扫描截断/胖二进制（FAT）都可能仍为
  true。发布门必须用 ``strict_verdict``：目录存在、native 文件非空、
  无畸形、未截断、无未声明 universal、架构与声明目标一致、主入口
  在 manifest、min OS 不超目标，全部满足才 PASS。

非 Mach-O 文件跳过（数据/资源）；解析失败**显式登记**不静默。
FAT/universal 头（0xCAFEBABE/BE/0xCAFEBABF）单独登记为
``universal_binaries``——单架构分发中出现即 ABI 破绽，不算"非
Mach-O 跳过"。
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import Any, Dict, List, Optional

# Mach-O 常量（usr/include/mach-o/loader.h）
_MAGIC_64 = 0xFEEDFACF
_MAGIC_64_SWAPPED = 0xCFFAEDFE          # 小端读出的字节序（文件首 4 字节）
_MAGIC_32 = 0xFEEDFACE
_CPUTYPES = {0x01000007: "x86_64", 0x0100000C: "arm64",
             7: "x86", 12: "arm"}
_LC_BUILD_VERSION = 0x32
_LC_VERSION_MIN_MACOSX = 0x24
#: FAT/universal 魔数（fat_header，N02：不算"非 Mach-O 跳过"）
_FAT_MAGICS = {0xCAFEBABE, 0xBEBAFECA, 0xCAFEBABF, 0xBFBAFECA}


def _parse_os(packed: int) -> str:
    major, minor, patch = packed >> 16, (packed >> 8) & 0xFF, packed & 0xFF
    return f"{major}.{minor}" + (f".{patch}" if patch else "")


def parse_mach_header(path: Path) -> Optional[Dict[str, Any]]:
    """解析 Mach-O 头 + 最低 OS（load commands 前段扫描）。

    非 Mach-O 返回 None；FAT/universal 返回 ``{"universal": True}``；
    畸形 Mach-O（load command 越界/cmdsize 溢出文件）抛 ValueError
    （调用方显式登记）。
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"unreadable: {exc}") from exc
    if len(raw) < 32:
        return None
    magic_le = struct.unpack_from("<I", raw, 0)[0]
    magic_be = struct.unpack_from(">I", raw, 0)[0]
    if magic_le in _FAT_MAGICS or magic_be in _FAT_MAGICS:
        return {"universal": True, "size_bytes": len(raw)}
    if magic_le == 0xFEEDFACF:
        endian, bits = "<", 64
    elif magic_be == 0xFEEDFACF:
        endian, bits = ">", 64
    elif magic_le == 0xFEEDFACE:
        endian, bits = "<", 32
    elif magic_be == 0xFEEDFACE:
        endian, bits = ">", 32
    else:
        return None  # 非 Mach-O（数据/资源/脚本）
    cputype = struct.unpack_from(f"{endian}I", raw, 4)[0]
    filetype = struct.unpack_from(f"{endian}I", raw, 12)[0]
    ncmds = struct.unpack_from(f"{endian}I", raw, 16)[0]
    header_size = 32 if bits == 64 else 28
    min_os: Optional[str] = None
    offset = header_size
    # N02：load command 边界校验——声明的 ncmds 条命令必须全部可行走
    # （每步 cmdsize≥8 且不超文件）；min_os 命中后仍继续行走校验，
    # 不提前 break（尾部截断不能因早停被漏掉）。
    if ncmds > 65535:
        raise ValueError(f"implausible ncmds: {ncmds}")
    for _ in range(ncmds):
        if offset + 8 > len(raw):
            raise ValueError(
                f"load commands truncated at offset {offset} "
                f"(ncmds={ncmds}, file={len(raw)}B)")
        cmd, size = struct.unpack_from(f"{endian}II", raw, offset)
        if size < 8:
            raise ValueError(
                f"load command 0x{cmd:X} cmdsize={size} < 8 "
                f"at offset {offset}")
        if offset + size > len(raw):
            raise ValueError(
                f"load command 0x{cmd:X} cmdsize={size} exceeds file "
                f"at offset {offset} (file={len(raw)}B)")
        if min_os is None and cmd == _LC_BUILD_VERSION and size >= 24:
            # build_version_command: cmd/cmdsize/platform/minos/sdk/ntools。
            packed = struct.unpack_from(f"{endian}I", raw, offset + 12)[0]
            min_os = _parse_os(packed)
        elif (min_os is None and cmd == _LC_VERSION_MIN_MACOSX
                and size >= 12):
            version = struct.unpack_from(f"{endian}I", raw, offset + 8)[0]
            min_os = _parse_os(version)
        offset += size
    return {
        "bits": bits,
        "cpu": _CPUTYPES.get(cputype, hex(cputype)),
        "filetype": filetype,
        "min_os": min_os,
        "size_bytes": len(raw),
    }


def audit_bundle(bundle_dir: str, *, max_files: int = 4000
                 ) -> Dict[str, Any]:
    """审计打包目录：架构一致性 + min OS + sha256 manifest。

    Returns:
        {"files": {relpath: {arch…, sha256}}, "macho_count",
         "architectures": [..], "consistent": bool,
         "malformed": [{path, error}], "skipped_non_macho": int,
         "universal_binaries": [relpath], "bundle_exists": bool,
         "scan_truncated": bool}
    """
    root = Path(bundle_dir)
    files: Dict[str, Any] = {}
    architectures: List[str] = []
    malformed: List[Dict[str, str]] = []
    universal: List[str] = []
    skipped = 0
    count = 0
    truncated = False
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if count >= max_files:
            # N02：截断必须显式可见——继续枚举只为确认还有后续文件。
            truncated = True
            continue
        rel = str(path.relative_to(root))
        try:
            header = parse_mach_header(path)
        except ValueError as exc:
            malformed.append({"path": rel, "error": str(exc)})
            continue
        if header is None:
            skipped += 1
            continue
        if header.get("universal"):
            universal.append(rel)
            continue
        count += 1
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entry = dict(header)
        entry["sha256"] = digest
        files[rel] = entry
        architectures.append(header["cpu"])
    arch_set = sorted(set(architectures))
    return {
        "files": files,
        "macho_count": count,
        "architectures": arch_set,
        "consistent": len(arch_set) <= 1,
        "malformed": malformed,
        "skipped_non_macho": skipped,
        "universal_binaries": universal,
        "bundle_exists": root.is_dir(),
        "scan_truncated": truncated,
    }


def _version_tuple(os_str: str) -> tuple:
    return tuple(int(part) for part in os_str.split(".") if part != "")


def strict_verdict(audit: Dict[str, Any], *, required_paths: tuple = (),
                   expect_arch: Optional[str] = None,
                   max_min_os: Optional[str] = None,
                   allow_universal: bool = False,
                   ) -> Dict[str, Any]:
    """N02 · 发布门严格裁决（Mach-O/PE 审计通用）。

    ``consistent`` 只覆盖"架构集合不混"，空目录/目录不存在/畸形/截断
    都可能仍为 true——本函数按独立严格条件逐项裁决，任一不满足即
    ``strict_pass=False`` 并给出机器可读原因清单：

    - bundle_exists：审计目录真实存在；
    - native 文件数非零（macho_count/pe_count）；
    - 无畸形（malformed 空）；
    - 未截断（scan_truncated=False）；
    - 无 universal/胖二进制（除非 allow_universal——按分发声明）；
    - 架构集合 == [expect_arch]（声明目标架构）；
    - required_paths 全部在 manifest（主入口/必需 codec 库）；
    - Mach-O：全部 min_os <= max_min_os（声明最低目标 OS）。
    """
    reasons: List[str] = []
    if not audit.get("bundle_exists", True):
        reasons.append("bundle_dir_missing")
    native_count = audit.get("macho_count", audit.get("pe_count", 0))
    if not native_count:
        reasons.append("no_native_binaries")
    if audit.get("malformed"):
        reasons.append(f"malformed:{len(audit['malformed'])}")
    if audit.get("scan_truncated"):
        reasons.append("scan_truncated")
    universal = audit.get("universal_binaries")
    if universal and not allow_universal:
        reasons.append(f"universal_binaries:{len(universal)}")
    if expect_arch is not None:
        archs = audit.get("architectures", [])
        if archs != [expect_arch]:
            reasons.append(
                f"arch_mismatch:have={','.join(archs) or 'none'},"
                f"want={expect_arch}")
    for rel in required_paths:
        if rel not in audit.get("files", {}):
            reasons.append(f"missing_required_path:{rel}")
    if max_min_os is not None:
        ceiling = _version_tuple(max_min_os)
        for rel, entry in audit.get("files", {}).items():
            min_os = entry.get("min_os")
            if min_os and _version_tuple(min_os) > ceiling:
                reasons.append(f"min_os_exceeds_target:{rel}:{min_os}")
    return {"strict_pass": not reasons, "reasons": reasons}


def audit_report(audit: Dict[str, Any]) -> str:
    """人类可读审计结论（manifest 另存 JSON）。"""
    lines = [
        f"Mach-O 文件数：{audit['macho_count']}",
        f"架构集合：{', '.join(audit['architectures']) or '—'}",
        f"架构一致：{'是' if audit['consistent'] else '否（混合架构！）'}",
        f"非 Mach-O 跳过：{audit['skipped_non_macho']}",
        f"畸形登记：{len(audit['malformed'])}",
    ]
    if audit.get("universal_binaries"):
        lines.append(f"universal/胖二进制：{len(audit['universal_binaries'])}")
    if not audit.get("bundle_exists", True):
        lines.append("审计目录不存在！")
    if audit.get("scan_truncated"):
        lines.append("扫描被 max_files 截断（覆盖不完整）！")
    min_os_list = sorted({
        f["min_os"] for f in audit["files"].values()
        if f.get("min_os")})
    if min_os_list:
        lines.append(f"min OS 集合：{', '.join(min_os_list)}")
    return "\n".join(lines)


__all__ = ["parse_mach_header", "audit_bundle", "audit_report",
           "strict_verdict"]
