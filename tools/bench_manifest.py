#!/usr/bin/env python3
"""基准 manifest 与加载库断言（P0-04，A2 工作包公共助手）。

所有性能对比基准在测量前必须调用 :func:`build_manifest`，并把返回的
manifest 原样写入结果 JSON。规则（计划 §0 共同规则 3 / P0-04）：

1. 读取被加载 dylib 的 ``tc_version()``（版本 + 内嵌 git commit）；
2. 计算 dylib 的 SHA-256（产物可复现锚点）；
3. 读取当前工作树 git commit + 脏标记；
4. **断言**：dylib 内嵌 commit 非 ``unknown``，且与工作树 HEAD 一致
   （允许显式传 ``expect_commit`` 宽松到短 SHA 前缀）；不一致直接抛
   ``ManifestMismatch``——不产生任何"比较"数据。

用法::

    from tools.bench_manifest import build_manifest
    manifest = build_manifest()          # 与工作树 HEAD 严格一致
    manifest = build_manifest(allow_dirty=True)  # 脏树可跑，但标记 dirty
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


class ManifestMismatch(RuntimeError):
    """加载库与工作树目标不一致（P0-04 断言失败，禁止出基准数据）。"""


def _git(args: list) -> Optional[str]:
    try:
        r = subprocess.run(["git", *args], cwd=_REPO,
                           capture_output=True, text=True)
    except OSError:
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def _loaded_dylib_path() -> Optional[Path]:
    import src.shared.codec.topos_binding as tb

    explicit = os.environ.get("TOPOS_CODEC_LIB")
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    for cand in tb.candidate_library_paths():
        if cand.is_file():
            return cand
    return None


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def build_manifest(allow_dirty: bool = False,
                   expect_commit: Optional[str] = None) -> Dict[str, Any]:
    """构建基准 manifest；加载库与工作树不一致时抛 ManifestMismatch。

    Args:
        allow_dirty: 工作树有未提交改动时仍允许测量（manifest 标
            ``git_dirty=True``，结果只能进"历史观察"档）。
        expect_commit: 显式目标 commit（短 SHA）；缺省取工作树 HEAD。
    """
    import src.shared.codec.topos_binding as tb

    codec = tb.ToposCodec()
    ver = codec.version()

    dylib = _loaded_dylib_path()
    if dylib is None:
        raise ManifestMismatch("libtopos_codec 未找到（先构建 native 库）")
    dylib_sha = sha256_file(dylib)

    head = _git(["rev-parse", "--short", "HEAD"])
    dirty = _git(["status", "--porcelain"]) not in (None, "")
    if head is None:
        raise ManifestMismatch("无法读取工作树 git HEAD（非 git 仓库？）")

    embedded = ver.git_commit or "unknown"
    target = (expect_commit or head).strip()
    # 内嵌 stamp 可能带 -dirty 后缀（CMake P0-04 注入）；比较取前缀
    embedded_clean = embedded.split("-dirty", 1)[0]
    if embedded_clean in ("", "unknown"):
        raise ManifestMismatch(
            f"dylib 内嵌 git commit 为 {embedded!r}（陈旧/未 stamp 构建）——"
            "请重新 cmake 配置 + 构建（P0-04），不得产出基准数据")
    if not target.startswith(embedded_clean) and \
            not embedded_clean.startswith(target):
        raise ManifestMismatch(
            f"dylib commit {embedded_clean} != 工作树目标 {target}——"
            "请重建 native 库后再测量（P0-04）")
    if dirty and not allow_dirty:
        raise ManifestMismatch(
            "工作树有未提交改动（git dirty）——按计划 §0 规则 1 只接受"
            "同一 clean HEAD 的 A/B；确要测量请 allow_dirty=True 并降档归档")

    return {
        "schema": "bench_manifest.v1",
        "topos_version": f"{ver.major}.{ver.minor}.{ver.patch}",
        "dylib_commit_embedded": embedded,
        "git_head": head,
        "git_dirty": dirty,
        "dylib_path": str(dylib),
        "dylib_sha256": dylib_sha,
        "abi_version": codec.abi_version,
        "slice_threads": codec.slice_threads(),
        "simd_avx2": codec.cpu_features().avx2,
        "simd_neon": codec.cpu_features().neon,
        "cpu": _cpu_brand(),
        "os": sys.platform,
        "python": sys.version.split()[0],
    }


def _cpu_brand() -> str:
    if sys.platform == "darwin":
        r = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                           capture_output=True, text=True)
        return r.stdout.strip()
    if sys.platform.startswith("linux"):
        r = subprocess.run(["bash", "-c",
                            "grep -m1 'model name' /proc/cpuinfo | cut -d: -f2"],
                           capture_output=True, text=True)
        return r.stdout.strip()
    return "unknown"


if __name__ == "__main__":
    import json

    allow_dirty = "--allow-dirty" in sys.argv
    try:
        print(json.dumps(build_manifest(allow_dirty=allow_dirty),
                         ensure_ascii=False, indent=2))
    except ManifestMismatch as exc:
        print(f"MANIFEST MISMATCH: {exc}", file=sys.stderr)
        sys.exit(1)
