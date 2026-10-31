"""M18 · 机器 B（Windows）一键复验入口（用户确认接入后执行）。

在 Windows 机器 B 的本仓库检出根目录运行：

    python scripts/m18_machine_b_recheck.py [--dist <打包目录>]

产出 ``docs/validation/upgrade_2026-10-24/M18/machine_B/report/``
下的环境指纹 + 门禁结果 + Windows 侧 PE ABI manifest + 黄金链 3
结果（JSON）。接入步骤见同目录 README.md。

只做只读审计与测试运行，不改代码、不改配置。

N02 收口（严格复验裁决）：

- 宿主校验：非 Windows 宿主直接 FAIL（在错误平台上的"通过"不代表
  Windows 门）；
- git 指纹：记录实际 HEAD 提交与 dirty 文件清单，报告与代码版本
  可对齐；
- 超时分级：门禁超时是显式 ``timeout`` 状态，不是普通失败；
- 未执行分级：探针脚本缺失/无 ``--dist`` 是 ``not_run``，overall
  不得因此称安装包门/三链门通过（默认只跑 chain3，三链声明恒 False
  直到三条链都有独立运行记录）；
- dist 审计走 ``strict_verdict``（consistent 单字段不可作发布裁决）。
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "validation" / "upgrade_2026-10-24" / "M18" \
    / "machine_B" / "report"

#: 单门超时（秒）；超时是显式状态而非普通失败
_GATE_TIMEOUT_S = 1800

#: 复验门清单（与 deferred_gates.md 的复开动作对齐）。
#: 注意：golden_chain 只含 chain3——chain1/chain2 无独立测试文件，
#: 报告的 three_chains_verified 因此恒为 False（如实声明）。
_GATES = [
    ("focused_gate", [sys.executable, str(ROOT / "scripts"
                   / "m00_focused_gate.py")]),
    ("golden_chain3", [sys.executable, "-m", "pytest", "-q",
                       "tests/edit/test_m18_golden_chain3.py"]),
    ("pe_audit_unit", [sys.executable, "-m", "pytest", "-q",
                       "tests/shared/test_m18_bundle_abi_audit_pe.py"]),
    ("probe_s", [sys.executable, str(
        ROOT / "docs/validation/upgrade_2026-10-24/"
               "reproduce_logic_gaps.py")]),
    ("probe_r", [sys.executable, str(
        ROOT / "docs/validation/upgrade_2026-10-24/"
               "plan_omission_probes.py")]),
]


def _git_fingerprint() -> dict:
    """实际 HEAD + dirty 清单（报告与受测代码版本可对齐，N02）。"""
    def _git(*args: str) -> str:
        try:
            proc = subprocess.run(
                ["git", *args], cwd=str(ROOT), capture_output=True,
                text=True, timeout=30)
            return proc.stdout.strip() if proc.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""
    dirty = [line for line in _git("status", "--porcelain").splitlines()
             if line.strip()][:50]
    return {
        "commit": _git("rev-parse", "HEAD") or "unknown",
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown",
        "dirty_files": dirty,
        "is_dirty": bool(dirty),
    }


def _fingerprint() -> dict:
    return {
        "machine": platform.machine(),
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "python": sys.version,
        "git": _git_fingerprint(),
        "argv_dist": None,  # 由调用方补充
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def _probe_findings(stdout: str) -> list[dict]:
    """读取探针结果；退出零仅证明运行完毕，不代表缺口已关闭。"""
    findings = json.loads(stdout)
    if not isinstance(findings, list) or not findings:
        raise ValueError("probe must return a non-empty result list")
    if any(not isinstance(item, dict) or not item.get("id")
           or type(item.get("issue_present")) is not bool
           for item in findings):
        raise ValueError("probe results require id and boolean issue_present")
    return findings


def _run(cmd: list, *, probe: bool = False) -> dict:
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                              text=True, timeout=_GATE_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        # N02：超时是独立状态——无结论 ≠ 失败细节可读，但都不是通过。
        return {
            "cmd": " ".join(cmd[-4:]),
            "status": "timeout",
            "seconds": round(time.time() - t0, 1),
            "error": f"timeout after {_GATE_TIMEOUT_S}s: {exc}",
            "passed": False,
        }
    result = {
        "cmd": " ".join(cmd[-4:]),
        "exit_code": proc.returncode,
        "seconds": round(time.time() - t0, 1),
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-2000:],
        "status": "passed" if proc.returncode == 0 else "failed",
        "passed": proc.returncode == 0,
    }
    if probe and proc.returncode == 0:
        try:
            findings = _probe_findings(proc.stdout)
        except (ValueError, TypeError) as exc:
            result["passed"] = False
            result["status"] = "failed"
            result["probe_error"] = str(exc)
        else:
            result["findings"] = findings
            result["passed"] = all(not item["issue_present"] for item in findings)
            if not result["passed"]:
                result["status"] = "failed"
    return result


def _not_run(reason: str) -> dict:
    return {"status": "not_run", "passed": False, "reason": reason}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", default=None,
                        help="Windows 打包产物目录（含主 .exe）")
    parser.add_argument("--main-exe", default=None,
                        help="dist 内主入口 .exe 相对路径（默认自动探测"
                             "顶层唯一 .exe）")
    args = parser.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        report, ok = _collect(args)
    except Exception:  # noqa: BLE001 —— 顶层兜底：报告必须落盘（N02）
        report = {"overall": "ERROR", "traceback": traceback.format_exc()}
        ok = False
    out = OUT_DIR / "machine_b_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"[machine_B] overall={report.get('overall')} -> {out}")
    return 0 if ok else 1


def _collect(args: argparse.Namespace) -> tuple[dict, bool]:
    report: dict = {"fingerprint": _fingerprint()}
    report["fingerprint"]["argv_dist"] = args.dist

    # N02 宿主门：复验必须在 Windows 宿主上执行；错误宿主上的
    # "绿"不构成 Windows 证据——全部门 not_run，整体 FAIL，快速失败。
    is_windows = report["fingerprint"]["system"] == "Windows"
    report["host_check"] = {
        "system": report["fingerprint"]["system"],
        "is_windows": is_windows,
        "passed": is_windows,
        "status": "passed" if is_windows else "failed",
    }
    ok = is_windows

    if not is_windows:
        for name, _cmd in _GATES:
            report[name] = _not_run(
                f"wrong host: {report['fingerprint']['system']} "
                "(recheck requires Windows)")
        report["golden_chains_claim"] = {
            "verified": [], "three_chains_verified": False,
            "note": "wrong host; no chain executed",
        }
        report["pe_abi_audit"] = _not_run("wrong host")
        report["overall"] = "FAIL"
        return report, False

    for name, cmd in _GATES:
        print(f"[machine_B] running {name} ...")
        script = Path(cmd[-1])
        if not script.is_file():
            # 未执行分级：入口缺失不是通过，也不伪装成测试失败。
            report[name] = _not_run(f"entry missing: {script}")
            ok = False
            continue
        result = _run(cmd, probe=name in {"probe_s", "probe_r"})
        report[name] = result
        ok = ok and result["passed"]

    # 三链声明（如实）：默认只运行 chain3。
    report["golden_chains_claim"] = {
        "verified": ["chain3"],
        "three_chains_verified": False,
        "note": "chain1/chain2 无独立复验入口；三链声明需各自运行记录",
    }

    # 安装包 ABI 门（严格裁决，N02）：无 --dist = not_run，不代表通过。
    if args.dist:
        from src.shared.platform.bundle_abi_audit_pe import (
            audit_bundle_pe, strict_verdict)
        main_exe = args.main_exe or _detect_main_exe(args.dist)
        if main_exe is None:
            report["pe_abi_audit"] = _not_run(
                "no top-level .exe found in dist (use --main-exe)")
            ok = False
        else:
            audit = audit_bundle_pe(args.dist)
            verdict = strict_verdict(
                audit, required_paths=(main_exe,),
                expect_arch="x86_64" if report["fingerprint"]["machine"]
                in ("AMD64", "x86_64") else None)
            report["pe_abi_audit"] = {
                "status": "passed" if verdict["strict_pass"] else "failed",
                "passed": verdict["strict_pass"],
                "main_exe": main_exe,
                "strict_verdict": verdict,
                **{k: v for k, v in audit.items() if k != "files"},
            }
            (OUT_DIR / "windows_bundle_abi_manifest.json").write_text(
                json.dumps(audit, ensure_ascii=False, indent=1),
                encoding="utf-8")
            ok = ok and verdict["strict_pass"]
    else:
        report["pe_abi_audit"] = _not_run(
            "--dist not provided; installer gate NOT verified")
        report["installer_gate_claim"] = (
            "not_run: 无 --dist 时本报告不构成安装包门证据")

    report["overall"] = "PASS" if ok else "FAIL"
    return report, ok


def _detect_main_exe(dist_dir: str) -> str | None:
    """dist 顶层唯一 .exe 视为主入口；多个/零个则要求 --main-exe。"""
    top = sorted(Path(dist_dir).glob("*.exe"))
    if len(top) == 1:
        return top[0].name
    return None


if __name__ == "__main__":
    raise SystemExit(main())
