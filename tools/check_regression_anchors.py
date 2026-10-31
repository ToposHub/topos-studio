#!/usr/bin/env python3
"""回归锚点来源登记与完整性审计（BENCH-0017 / REGRESSION_BASELINE）。

问题（playback 系报告 cards-group2/4/5 各目录可比性备注 +
REGRESSION_BASELINE.md §10）：20 条锚点（ANC-01~20）的**数据来源**大部分
不在 P0-04 manifest 结果树内——readahead/*.txt、real_2k_h264_20260907、
export_baseline、p0_runner、benchmarks/timeline_occlusion*、e1_encode 的
dirty 试点目录均无 bench_manifest.v1 锚定，锚点一旦被引用或复测，无法核验
其底层数据文件是否仍然存在、是否被静默改动（HISTORICAL P2-08：仅
``<head8>/<dylib8>/<manifest8>/<utc>/`` 结果树为当前有效体系）。

本脚本把每条锚点的来源文件**逐个 SHA-256 登记**并分类：

- ``p04_manifest_tree``：bench_out 五段路径（head/dylib8/manifest8/utc/
  result.json）且文件内 manifest.schema == bench_manifest.v1——登记时顺带
  抄录 git_head / git_dirty / dylib_sha256 前 8 位；
- ``bench_out_off_tree``：bench_out 下但无 manifest 锚定（历史观察数据）；
- ``repo_benchmarks_off_tree``：``benchmarks/`` 下的测试侧产物；
- ``other_off_tree``：其余（含 docs 侧证据文件）。

任何来源文件缺失/无法解析 → 该锚点记 ``missing``，脚本 **exit 1**——
防止锚点底层数据被移动后基线仍被当作有效引用（完整性门禁）。

登记产物写入 ``bench_out/anchor_registry/<utc>/anchor_registry.json``
（schema=anchor_registry.v1，含 baseline json 自身 SHA），使锚点来源在
P0-04 风格的结果树中有可追溯的登记位置；后续复测产物应落
``bench_out/<head8>/<dylib8>/<manifest8>/<utc>/`` 并更新本登记。

用法：
    .venv/bin/python tools/check_regression_anchors.py             # 登记并写盘
    .venv/bin/python tools/check_regression_anchors.py --dry-run    # 只打印不写盘
    .venv/bin/python tools/check_regression_anchors.py --baseline <json>

退出码：0=全部锚点来源完整；1=存在缺失/不可解析来源；2=baseline 文件不可读。
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = PROJECT_ROOT / "docs" / "benchreports" / (
    "2026-09-28") / "REGRESSION_BASELINE.json"
DEFAULT_OUT_ROOT = PROJECT_ROOT / "bench_out" / "anchor_registry"

#: source 字段里的中文括注（如"（decode / decode_comparison 段）"）不参与路径解析。
_CJK_PAREN = re.compile(r"（[^）]*）")

#: P0-04 结果树的合法路径长度（含 "bench_out" 与文件名的 parts 总数）：
#: 6 = bench_out/<head8>/<dylib8>/<manifest8>/<utc>/<file>（058d29da 式根树，
#: 如 6b3dfe9d / 5bc83bef）；7 = bench_out/<family>/<head8>/<dylib8>/
#: <manifest8>/<utc>/result.json（d1_playback / e1_encode 等族前缀树）。
#: manifest.schema == bench_manifest.v1 才是权威信号，深度只做前置过滤。
_P04_TREE_DEPTHS = (6, 7)


def extract_source_paths(source_field: str) -> list[str]:
    """从 baseline 的 source 字段解析出相对路径列表（纯函数供单测）。

    处理三类写法：分号分隔多路径；全角括号中文注记；
    ``{a,b,c}`` 花括号组与 ``*`` 通配（展开由 :func:`resolve_paths` 对文件
    系统执行，这里只做花括号笛卡尔展开）。
    """
    text = _CJK_PAREN.sub("", source_field or "")
    paths: list[str] = []
    for chunk in text.split(";"):
        cleaned = chunk.strip()
        if not cleaned:
            continue
        # 花括号组展开：{a,b,c} → a / b / c（仅支持单层，baseline 现状够用）
        match = re.search(r"\{([^{}]*)\}", cleaned)
        if match:
            options = [opt.strip() for opt in match.group(1).split(",")]
            expanded = []
            for opt in options:
                expanded.append(
                    cleaned[:match.start()] + opt + cleaned[match.end():])
            # 递归展开剩余花括号组（如 {a,b}-match/x_{1,2}.json）
            further: list[str] = []
            for item in expanded:
                if "{" in item:
                    further.extend(extract_source_paths(item))
                else:
                    further.append(item)
            paths.extend(further)
        else:
            paths.append(cleaned)
    return paths


def resolve_paths(repo_root: Path, patterns: list[str]) -> list[dict]:
    """把模式列表解析为具体文件（``*`` 用 glob 展开；未命中保留原样标 missing）。"""
    resolved: list[dict] = []
    for pattern in patterns:
        if "*" in pattern:
            matches = sorted(
                Path(pattern).glob(pattern.name)
                if Path(pattern).is_absolute()
                else repo_root.glob(pattern))
        else:
            matches = []
        if matches:
            for path in matches:
                resolved.append({"pattern": pattern, "path": path,
                                 "matched": True})
        else:
            path = Path(pattern) if Path(pattern).is_absolute() \
                else repo_root / pattern
            resolved.append({"pattern": pattern, "path": path,
                             "matched": False})
    return resolved


def classify_source(rel_path: Path, repo_root: Path) -> dict:
    """按位置与内容分类一个来源文件（纯读，不修改）。

    返回 classification 与（p04 树内的）manifest 摘要。P0-04 树判定必须
    同时满足：bench_out 下合法深度 + 结果文件名 + 内含 bench_manifest.v1
    schema——只有目录形状不算数（防止把普通目录误判为 manifest 锚定数据）。
    """
    try:
        rel = rel_path.relative_to(repo_root)
    except ValueError:
        rel = rel_path
    parts = rel.parts
    info: dict = {"classification": "other_off_tree"}
    if parts and parts[0] == "bench_out":
        if (len(parts) in _P04_TREE_DEPTHS
                and parts[-1] in ("result.json", "rd_result.json")
                and rel_path.is_file()):
            try:
                with open(rel_path, "r", encoding="utf-8") as fh:
                    record = json.load(fh)
                manifest = record.get("manifest") if isinstance(
                    record, dict) else None
                if isinstance(manifest, dict) and manifest.get(
                        "schema") == "bench_manifest.v1":
                    info["classification"] = "p04_manifest_tree"
                    dylib_sha = str(manifest.get("dylib_sha256", ""))
                    info["manifest"] = {
                        "git_head": manifest.get("git_head"),
                        "git_dirty": manifest.get("git_dirty"),
                        "dylib_sha8": dylib_sha[:8] if dylib_sha else None,
                    }
            except (OSError, json.JSONDecodeError):
                pass  # 解析失败按 off-tree 处理（不中断整表）
        if info["classification"] == "other_off_tree":
            info["classification"] = "bench_out_off_tree"
    elif parts and parts[0] == "benchmarks":
        info["classification"] = "repo_benchmarks_off_tree"
    return info


def sha256_file(path: Path, chunk: int = 1 << 20) -> str | None:
    """文件 SHA-256；不可读（含目录/缺失）返回 None。"""
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(chunk), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def audit_baseline(baseline_path: Path,
                   repo_root: Path = PROJECT_ROOT) -> dict:
    """审计整个 baseline：逐锚点解析来源、SHA、分类（纯函数供单测复用）。"""
    with open(baseline_path, "r", encoding="utf-8") as fh:
        anchors = json.load(fh)
    if not isinstance(anchors, list) or not anchors:
        raise ValueError("baseline 必须是非空 JSON 数组（anchor 列表）")

    results: list[dict] = []
    for anchor in anchors:
        name = str(anchor.get("name", ""))
        anchor_id = name.split()[0] if name else "UNKNOWN"
        sources_field = str(anchor.get("source", ""))
        patterns = extract_source_paths(sources_field)
        entries: list[dict] = []
        for item in resolve_paths(repo_root, patterns):
            path: Path = item["path"]
            exists = path.is_file()
            entry = {
                "pattern": item["pattern"],
                "rel_path": str(path.relative_to(repo_root)
                                if path.is_absolute() and repo_root in
                                path.parents else path),
                "exists": exists,
                "sha256": sha256_file(path) if exists else None,
            }
            entry.update(classify_source(path, repo_root))
            entries.append(entry)
        missing = [e for e in entries if not e["exists"]]
        results.append({
            "anchor_id": anchor_id,
            "name": name,
            "tolerance": anchor.get("tolerance"),
            "sources": entries,
            "status": "missing" if (missing or not entries) else "ok",
        })

    summary = {
        "anchors_total": len(results),
        "anchors_ok": sum(1 for r in results if r["status"] == "ok"),
        "anchors_missing": sum(1 for r in results if r["status"] == "missing"),
        "sources_p04_manifest_tree": sum(
            1 for r in results for e in r["sources"]
            if e["classification"] == "p04_manifest_tree"),
        "sources_off_tree": sum(
            1 for r in results for e in r["sources"]
            if e["classification"] != "p04_manifest_tree"),
    }
    return {"anchors": results, "summary": summary,
            "baseline_sha256": sha256_file(baseline_path)}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="回归锚点来源登记与完整性审计（BENCH-0017）")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE,
                        help="REGRESSION_BASELINE.json 路径")
    parser.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT,
                        help="登记产物根目录（默认 bench_out/anchor_registry）")
    parser.add_argument("--dry-run", action="store_true",
                        help="计算并打印审计结果，不写登记文件")
    return parser


def print_report(audit: dict) -> None:
    """控制台一览：每锚点一行 + 分类统计。"""
    for anchor in audit["anchors"]:
        marks = []
        for entry in anchor["sources"]:
            tag = {"p04_manifest_tree": "P04",
                   "bench_out_off_tree": "off",
                   "repo_benchmarks_off_tree": "off",
                   "other_off_tree": "?"}.get(
                entry["classification"], "?")
            marks.append(f"{tag}:{entry['rel_path']}"
                         + ("" if entry["exists"] else " (MISSING)"))
        status = "OK " if anchor["status"] == "ok" else "MISS"
        print(f"[{status}] {anchor['anchor_id']:<7} "
              f"{' | '.join(marks) if marks else '(无来源)'}")
    s = audit["summary"]
    print(f"\n锚点 {s['anchors_ok']}/{s['anchors_total']} 完整；来源文件 "
          f"P04 树内 {s['sources_p04_manifest_tree']} / 树外 "
          f"{s['sources_off_tree']}")


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    baseline = (args.baseline if args.baseline.is_absolute()
                else PROJECT_ROOT / args.baseline)
    try:
        audit = audit_baseline(baseline)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"baseline 不可读：{exc}", file=sys.stderr)
        return 2

    print(f"baseline: {baseline.relative_to(PROJECT_ROOT)} "
          f"(sha256 {audit['baseline_sha256'][:12]}…)")
    print_report(audit)

    if not args.dry_run:
        utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir = args.out_root / utc
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = {"schema": "anchor_registry.v1", "generated_at": utc,
                   "tool": "tools/check_regression_anchors.py",
                   **audit}
        out_path = out_dir / "anchor_registry.json"
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"\n登记产物：{out_path.relative_to(PROJECT_ROOT)}")

    return 1 if audit["summary"]["anchors_missing"] else 0


if __name__ == "__main__":
    sys.exit(main())
