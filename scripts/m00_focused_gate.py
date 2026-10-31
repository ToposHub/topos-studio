#!/usr/bin/env python
"""M00 focused regression gate — 升级计划 M (docs/upgrade_plan_M_mac_2026-10-24.md §M00)。

真实阻断语义（M00 验收句）：
- 每个分域在独立子进程运行（避免 Qt/C 解码器累积挂起拖垮整轮）；
- 任何域非零退出码 → 门禁失败（exit 1），**不允许**用"与旧基线失败集合相等"放行；
- 本门禁只判定"零失败"，不做失败集合对比；历史失败集合登记在
  docs/validation/upgrade_2026-10-24/M00/<head>/<run_id>/known_failures.json。

用法：
    python scripts/m00_focused_gate.py [--report PATH] [--timeout SEC] [--list]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 分域清单：M00 冻结的 12 个 focused 文件按域拆分；每域一个子进程。
# 域的划分跟随 pytest 目录结构（tests/<domain>/...），域内共享 Qt/解码器状态，
# 域间完全隔离——单域挂起只超时该域，不影响其余域的判定。
# N20 扩充（2026-10-25）：原 13 文件不含升级期新增关键用例——补打包 ABI
# （M18.4/N02 严格裁决）、黄金链 3、W01 加载器 ABI 门、W02/W06 L1 替身、
# 审计小修回归（F 族）入 required；仍只收快而稳的逻辑用例，重量级真机
# /浸泡类按各自域门执行。
DOMAINS: dict[str, list[str]] = {
    "edit_core": [
        "tests/edit/test_f81_project_snapshots.py",
        "tests/edit/test_f82_review_comments.py",
        "tests/edit/test_f83_audition.py",
        "tests/edit/test_audio_sync_binding.py",
        "tests/edit/test_color_group.py",
        "tests/edit/test_color_versions.py",
        "tests/edit/test_proxy_cache_product_loop.py",
        "tests/edit/test_realtime_playback_telemetry.py",
        "tests/edit/test_m02_snapshot_io_fidelity.py",
        "tests/edit/test_m18_golden_chain3.py",
    ],
    "edit_ai": [
        "tests/edit/ai_editing/test_f91_filler_removal.py",
    ],
    "deliver": [
        "tests/deliver/test_f93_version_compiler.py",
    ],
    "color": [
        "tests/color/test_qualifier_keyframes.py",
    ],
    "shared": [
        "tests/shared/test_f64_frame_manifest.py",
        "tests/shared/test_m18_bundle_abi_audit.py",
        "tests/shared/test_m18_bundle_abi_audit_pe.py",
        "tests/shared/test_n02_strict_abi_verdict.py",
        "tests/shared/test_m17_gpu_scale_readback.py",
        "tests/shared/test_upgrade_audit_gate_regressions.py",
    ],
    "media_loader": [
        "tests/media/test_topos_codec_loader_w01.py",
        "tests/media/test_cuda_resident_lifecycle_w02.py",
        "tests/media/test_cuda_extras_w06.py",
    ],
}

ENV_OVERRIDES = {
    "QT_QPA_PLATFORM": "offscreen",
    "TOPOS_HEADLESS_GL": "1",
    "TOPOS_TEST_MODE": "1",
    "OPENBLAS_NUM_THREADS": "1",
}


def run_domain(domain: str, files: list[str], timeout: float) -> dict:
    env = dict(os.environ)
    env.update(ENV_OVERRIDES)
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *files,
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
    ]
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            env=env,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
        return {
            "domain": domain,
            "command": cmd,
            "exit_code": proc.returncode,
            "duration_sec": round(time.time() - started, 2),
            "timeout": False,
            "stdout_tail": proc.stdout[-4000:],
            "stderr_tail": proc.stderr[-2000:],
        }
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return {
            "domain": domain,
            "command": cmd,
            "exit_code": None,
            "duration_sec": round(time.time() - started, 2),
            "timeout": True,
            "stdout_tail": out[-4000:],
            "stderr_tail": "subprocess timeout after %.0fs" % timeout,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default=None, help="write JSON report to this path")
    parser.add_argument("--timeout", type=float, default=900.0, help="per-domain subprocess timeout in seconds")
    parser.add_argument("--list", action="store_true", help="list domains and exit")
    args = parser.parse_args()

    if args.list:
        for domain, files in DOMAINS.items():
            print(f"{domain}:")
            for f in files:
                print(f"  {f}")
        return 0

    missing = [
        f
        for files in DOMAINS.values()
        for f in files
        if not (REPO_ROOT / f).exists()
    ]
    if missing:
        print("GATE FAIL: focused files missing from HEAD:", file=sys.stderr)
        for f in missing:
            print(f"  {f}", file=sys.stderr)
        return 2

    results = []
    failed_domains: list[str] = []
    for domain, files in DOMAINS.items():
        print(f"[m00-gate] running domain {domain} ({len(files)} file(s))...", flush=True)
        result = run_domain(domain, files, args.timeout)
        results.append(result)
        status = "TIMEOUT" if result["timeout"] else f"exit={result['exit_code']}"
        print(f"[m00-gate] {domain}: {status} in {result['duration_sec']}s", flush=True)
        if result["timeout"] or result["exit_code"] != 0:
            failed_domains.append(domain)
            print(result["stdout_tail"][-2000:], file=sys.stderr)

    report = {
        "gate": "m00_focused_gate",
        "plan": "docs/upgrade_plan_M_mac_2026-10-24.md#M00",
        "blocking_semantics": "any domain failure/timeout fails the gate; baseline comparison is not an excuse",
        "env_overrides": ENV_OVERRIDES,
        "domains": results,
        "failed_domains": failed_domains,
        "passed": len(failed_domains) == 0,
    }
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"[m00-gate] report written to {report_path}")

    if failed_domains:
        print(f"GATE FAIL: domains failed: {', '.join(failed_domains)}", file=sys.stderr)
        return 1
    print("GATE PASS: all focused domains green (0 failures).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
