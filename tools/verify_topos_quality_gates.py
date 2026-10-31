#!/usr/bin/env python3
"""TOPOS↔ProRes 画质对齐发布门禁（画质对齐计划 §5 P7，2026-09-21）。

阻断发布的三类回归（计划 §5 P7 完成标准）：

1. **容量回归**：基准 manifest（`tests/test_videos/topos_prores_baseline_
   manifest.json`）中任一档位 topos_bytes > prores_bytes × 0.98；
2. **质量/块效应回归**：质量报告（`bench_out/topos_quality/
   quality_recal-*.json`）任一档位 PSNR/SSIM 低于地板或 8×8 块指数
   超过天花板（地板/天花板锚定 P2/P4 交付值，见 THRESHOLDS）；
3. **元数据漂移**：manifest 成对一致性（帧数/色彩/音频）失败；档位表
   target_bpp 偏离 v1.9 重标定值（改档位必须重生成语料+更新本门禁）；
   素材在场时（本机/带素材环境）sha256 指纹与 manifest 不符。

CI 用法（素材不在场，校验已提交产物）::

    .venv/bin/python tools/verify_topos_quality_gates.py

本机全量（素材在场加验 hash）::

    .venv/bin/python tools/verify_topos_quality_gates.py --require-media

输出：违规清单（人读）+ --json 机器读；任一违规退出码 1（CI 阻断）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Mapping

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "tests" / "test_videos" / "topos_prores_baseline_manifest.json"
QUALITY_DIR = REPO / "bench_out" / "topos_quality"
# N05：p5 起报告携带 provenance 绑定（commit/库 SHA/母版 SHA/命令），
# 门禁据此拒绝为变化后的 codec 放行（见 4b 检查）。
QUALITY_REPORT = QUALITY_DIR / "quality_recal-p5.json"

CAP_RATIO = 0.98

#: v1.9 重标定档位表（P2，2026-09-21）——档位 target_bpp 变更必须
#: ①重生成语料 ②更新本门禁与质量阈值 ③跑全量回归（防静默漂移）。
TIER_BPP_V19 = {
    "netproxy": 0.29108,
    "proxy": 0.64483,
    "lt": 1.35234,
    "standard": 2.11179,
    "hq": 3.15075,
    "4444": 4.26335,
    "4444xq": 6.28784,
}

#: 质量阈值（锚定 P2/P4 交付实测 quality_recal-p4.json，留 0.4–0.8 dB /
#: 0.002–0.01 SSIM / 块指数 0.05–0.15 的回归容差；更优是允许的）。
#: 格式：size/tier → (psnr_y_floor, ssim_y_floor, block_index_ceiling)
THRESHOLDS = {
    # C03：2K/proxy 源（2K_prores422Proxy）同批 prores_ks 重建 → 块指数
    # +0.20（PSNR/SSIM 持平），同源变化重锚（见下方 2026-09-27 注）。
    "2K/proxy":    (35.2, 0.9250, 2.20),
    "2K/lt":       (38.7, 0.9600, 1.45),
    "2K/standard": (40.7, 0.9750, 1.25),
    "2K/hq":       (42.4, 0.9845, 1.15),
    "2K/4444":     (43.0, 0.9880, 1.15),
    "2K/4444xq":   (43.4, 0.9915, 1.10),
    "4K/proxy":    (38.5, 0.9490, 2.15),
    "4K/lt":       (43.1, 0.9780, 1.58),
    # C03 重标定（2026-09-27）：4K 422/422HQ 源由 VT 重建改为 prores_ks
    # 重建（VT 封装不写 color_range，冻结色彩契约失败）→ 源细节更丰富，
    # 同字节预算下对源保真度自然下降。2K 对照组（源未变）新旧实测差
    # ≤0.1 dB / 0.001 SSIM，证明编码器无回归；4K/standard −2.6 dB、
    # 4K/hq −1.1 dB 全部归因源变化，按同一容差惯例重锚。
    "4K/standard": (47.2, 0.9930, 1.50),
    "4K/hq":       (54.8, 0.9980, 1.38),
    "4K/4444":     (59.4, 0.9992, 1.29),
    "4K/4444xq":   (65.0, 0.9996, 1.26),
}

#: 母版指纹（sha256 首尾 1MB + size；P0 语料锚点）——素材变更 = 基准
#: 集失效，须重跑 P0–P2 流程。
MASTER_SHA256_PARTIAL = "96dee3166282d212628baaef6cf55b938f2f5f78ceb360725dc38d64f878e057"  # 由 --record-master 生成（母版合法变更后更新）


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_bound_codec_library(provenance: Mapping[str, object]) -> list[str]:
    """严格校验产品绑定层实际加载且 ABI 兼容的动态库。"""
    from src.shared.codec.topos_binding import ToposCodec, ToposCodecError

    try:
        codec = ToposCodec()
        library_path = Path(codec._lib._name).resolve(strict=True)
        actual_sha = _sha256_file(library_path)
        version = codec.version()
    except (OSError, ToposCodecError, AttributeError, ValueError) as exc:
        return [f"quality-dylib-load: 当前 TOPOS 库不可加载/校验: {exc}"]

    violations: list[str] = []
    if actual_sha != provenance.get("dylib_sha256"):
        violations.append(
            "quality-dylib-sha: 实际加载库的完整 SHA-256 与质量报告不一致"
            f"（{library_path}）；须用该库重算报告"
        )
    if codec.abi_version != provenance.get("dylib_abi_version"):
        violations.append("quality-dylib-abi: 实际加载库 ABI 与质量报告不一致")
    if version.git_commit != provenance.get("dylib_git_commit"):
        violations.append("quality-dylib-version: 库内嵌 commit 与质量报告不一致")
    if version.build_target != provenance.get("dylib_build_target"):
        violations.append("quality-dylib-target: 库构建目标与质量报告不一致")
    return violations


def _sha256_partial(path: Path, head_bytes: int = 1 << 20) -> str:
    size = path.stat().st_size
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(head_bytes))
        if size > 2 * head_bytes:
            f.seek(-head_bytes, 2)
            h.update(f.read(head_bytes))
    h.update(str(size).encode())
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--require-media", action="store_true",
                        help="素材在场：复核全部文件 sha256 指纹")
    parser.add_argument("--record-master", action="store_true",
                        help="输出当前母版指纹（维护用：母版合法变更后更新常量）")
    parser.add_argument("--json", type=Path, default=None,
                        help="机器读结果输出路径")
    args = parser.parse_args()

    violations: list = []

    if args.record_master:
        master = REPO / "tests" / "test_videos" / "4K_prores4444.mov"
        print(_sha256_partial(master))
        return 0

    # —— 1) 档位表漂移 ——
    from src.shared.codec.topos_profiles import TOPOS_PROFILE_TIERS
    for tier_id, bpp in TIER_BPP_V19.items():
        actual = TOPOS_PROFILE_TIERS[tier_id].target_bpp
        if actual is None or abs(actual - bpp) > 1e-9:
            violations.append(
                f"tier-drift: {tier_id} target_bpp={actual!r} ≠ v1.9 定标 "
                f"{bpp}（改档位须重生成语料并更新门禁/阈值，见 THRESHOLDS 注释）")

    # —— 2) 容量 + 成对一致性 ——
    if not MANIFEST.is_file():
        violations.append(f"manifest-missing: {MANIFEST}")
    else:
        manifest = json.loads(MANIFEST.read_text())
        pairs = manifest.get("pairs", [])
        if len(pairs) != 12:
            violations.append(
                f"manifest-pairs: 期望 12 对，得到 {len(pairs)}（语料不完整）")
        for p in pairs:
            tag = f"{p['size']}/{p['tier']}"
            if not p.get("within_cap_98"):
                violations.append(
                    f"cap: {tag} topos {p['topos_bytes']} > 98% prores "
                    f"{p['prores_bytes']}（{p['topos_over_prores']:.1%}）")
            if not p.get("consistent"):
                violations.append(f"pair-consistency: {tag} 帧数/色彩/音频不一致")
            elif p["topos_bytes"] > p["prores_bytes"] * CAP_RATIO:
                violations.append(
                    f"cap-arithmetic: {tag} 复算超限（manifest 标记与数值矛盾）")

        # —— 3) 素材指纹（在场时） ——
        if args.require_media:
            for e in manifest.get("entries", []):
                f = REPO / e["path"]
                if not f.is_file():
                    violations.append(f"media-missing: {e['path']}")
                    continue
                got = _sha256_partial(f)
                if got != e.get("sha256_partial"):
                    violations.append(
                        f"media-hash: {e['path']} 指纹漂移（素材被改动——"
                        "基准失效，须重跑 P0–P2 流程）")
            master = REPO / manifest["master"]["path"]
            if master.is_file():
                got = _sha256_partial(master)
                if got != MASTER_SHA256_PARTIAL:
                    violations.append(
                        "master-hash: 母版指纹 ≠ 门禁锚点（母版变更须走 "
                        "--record-master + 全量重定标流程）")

    # —— 4) 质量阈值 ——
    if not QUALITY_REPORT.is_file():
        violations.append(f"quality-report-missing: {QUALITY_REPORT}")
    else:
        report = json.loads(QUALITY_REPORT.read_text())
        results = report.get("results", {})
        for key, (psnr_floor, ssim_floor, block_ceil) in THRESHOLDS.items():
            r = results.get(key)
            if r is None:
                violations.append(f"quality-missing: {key} 不在质量报告中")
                continue
            if r["psnr_y"] < psnr_floor:
                violations.append(
                    f"quality-psnr: {key} PSNR_Y {r['psnr_y']:.2f} < 地板 "
                    f"{psnr_floor}（画质回归，阻断发布）")
            if r["ssim_y"] < ssim_floor:
                violations.append(
                    f"quality-ssim: {key} SSIM {r['ssim_y']:.4f} < 地板 "
                    f"{ssim_floor}")
            if r["block_index_y"] > block_ceil:
                violations.append(
                    f"quality-block: {key} 8×8 块指数 {r['block_index_y']:.3f} "
                    f"> 天花板 {block_ceil}（块伪影回归，阻断发布）")

        # —— 4b) N05：报告产物绑定 ——
        # 旧报告只有生成时间与路径：codec/语料变化后仍可为新构建放行。
        # 绑定口径：报告记录度量时的 commit/库 SHA/母版完整 SHA/命令；
        # 门禁拒绝「codec 或冻结清单自度量 commit 以来有变化」的报告
        # （须重算），素材在场时加验 库 SHA 与母版完整 SHA。
        prov = report.get("provenance") or {}
        for field in ("git_commit", "dylib_sha256", "dylib_abi_version",
                      "dylib_git_commit", "dylib_build_target", "master_sha256",
                      "measurement_script_sha256", "command"):
            if not prov.get(field):
                violations.append(
                    f"quality-provenance-missing: 报告缺 {field}——旧口径"
                    "产物（无绑定），须用当前 measure_topos_baseline_quality"
                    ".py 重算（N05）")
        if prov.get("git_commit") in (None, "", "unknown"):
            violations.append("quality-git-unknown: 报告无法绑定已知 git commit")
        else:
            import subprocess as _sp
            try:
                changed = _sp.run(
                    ["git", "diff", "--name-only",
                     prov["git_commit"] + "..HEAD", "--",
                     "native/topos_codec",
                     "tests/test_videos/topos_prores_baseline_manifest.json"],
                    cwd=REPO, capture_output=True, text=True, check=True,
                ).stdout.strip()
                if changed:
                    violations.append(
                        "quality-provenance-stale: codec/冻结清单自度量 "
                        f"commit {prov['git_commit'][:12]} 以来有变化"
                        f"（{len(changed.splitlines())} 个文件）——报告数值"
                        "不代表当前构建，须重算后放行（N05）")
                # 未提交的 codec 改动同样使报告失效（commit diff 看不见）
                dirty_codec = _sp.run(
                    ["git", "status", "--porcelain", "--", "native/topos_codec"],
                    cwd=REPO, capture_output=True, text=True, check=True,
                ).stdout.strip()
                if dirty_codec:
                    violations.append(
                        "quality-codec-dirty: 工作树有未提交的 codec 改动"
                        "——报告不代表当前构建，提交或还原后重验（N05）")
            except (OSError, _sp.CalledProcessError) as exc:
                violations.append(f"quality-git-check: 无法核对 codec 源码: {exc}")
        script_sha = _sha256_file(REPO / "tools" / "measure_topos_baseline_quality.py")
        if script_sha != prov.get("measurement_script_sha256"):
            violations.append(
                "quality-measure-script: 当前度量脚本与报告生成脚本不一致"
                "——须重新度量")
        if args.require_media:
            violations.extend(verify_bound_codec_library(prov))
            master = REPO / str(report.get("master", "x"))
            if master.is_file() and prov.get("master_sha256"):
                got = _sha256_file(master)
                if got != prov["master_sha256"]:
                    violations.append(
                        "quality-master-sha: 母版完整 SHA ≠ 报告绑定——"
                        "输入已变，须重算报告（N05）")

    # —— 输出 ——
    ok = not violations
    for v in violations:
        print(f"✗ {v}")
    print(f"\n{'PASS' if ok else 'FAIL'}: {len(violations)} 项违规"
          f"（容量/质量/漂移门禁，计划 §5 P7）")
    if args.json:
        args.json.write_text(json.dumps(
            {"ok": ok, "violations": violations}, ensure_ascii=False, indent=2)
            + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    raise SystemExit(main())
