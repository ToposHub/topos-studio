"""C0.1 junit 汇总器（计划表 B · Windows 线）。

解析 testfarm/<run>/G*.xml（xunit2），产出紧凑摘要：

- 每组：tests/errors/failures/skipped/time、失败用例清单(name/message 截断)、
  skip 用例清单(name+reason)、error 用例清单
- 汇总：跨组合计、失败/skip 的 reason 聚类（与 C0.2 静态桶对拍用）

只读 XML，不改任何测试产物。

用法：
    .venv/Scripts/python.exe scripts/summarize_testfarm.py --dir testfarm/2026-10-24_machineB
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 与 scripts/audit_test_skips.py 的 RULES 对齐的轻量运行时 reason 聚类。
RT_RULES: list[tuple[tuple[str, ...], str]] = [
    (("darwin", "macos", "mac ", "pyobjc", "metal", "videotoolbox", "iosurface"), "os_macos"),
    (("win32", "windows"), "os_windows"),
    (("nvidia", "d3d11", "nvdec", "wgl", "adapter", "gpu"), "gpu_hardware"),
    (("gl context", "moderngl", "opengl", "standalone"), "gl_context"),
    (("libtopos", "topos_codec", "dylib", "hwdecode_windows", "topos native"), "topos_native_lib"),
    (("test video", "test_videos", "real media", "素材", "fixture", "material"), "media_fixture_missing"),
    (("decklink", "vlc", "camera", "webcam"), "external_device"),
    (("importorskip", "not installed", "unavailable", "不可用", "未构建", "未安装"), "optional_missing"),
    (("pyqt", "qtbot", "qapplication", "qt6"), "qt_dep"),
    (("getenv", "environ", "ci", "gating"), "env_ci_gate"),
]


def classify_reason(text: str) -> str:
    low = (text or "").lower()
    for needles, cat in RT_RULES:
        if any(n in low for n in needles):
            return cat
    return "other"


def _clip(s: str, n: int = 220) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s[:n]


def parse_group(xml_path: Path) -> dict:
    root = ET.parse(xml_path).getroot()
    suite = root.find("testsuite") if root.tag == "testsuites" else root
    if suite is None:
        return {"file": xml_path.name, "error": "no testsuite element"}
    out = {
        "file": xml_path.name,
        "tests": int(suite.get("tests", 0)),
        "errors": int(suite.get("errors", 0)),
        "failures": int(suite.get("failures", 0)),
        "skipped": int(suite.get("skipped", 0)),
        "time": float(suite.get("time", 0) or 0),
        "failed_cases": [],
        "error_cases": [],
        "skip_cases": [],
    }
    for tc in suite.iter("testcase"):
        name = f"{tc.get('classname', '')}::{tc.get('name', '')}"
        for failure in tc.findall("failure"):
            out["failed_cases"].append(
                {"name": name, "message": _clip(failure.get("message", "")),
                 "text_head": _clip(failure.text or "")}
            )
        for error in tc.findall("error"):
            out["error_cases"].append(
                {"name": name, "message": _clip(error.get("message", ""))}
            )
        for skip in tc.findall("skipped"):
            reason = skip.get("message", "") or _clip(skip.text or "")
            out["skip_cases"].append({"name": name, "reason": _clip(reason, 140)})
    return out


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="testfarm/2026-10-24_machineB")
    args = parser.parse_args()
    farm = PROJECT_ROOT / args.dir

    groups = []
    for xml in sorted(farm.glob("G*.xml")):
        groups.append(parse_group(xml))

    totals = {"tests": 0, "errors": 0, "failures": 0, "skipped": 0,
              "passed": 0, "time": 0.0}
    skip_reasons: Counter[str] = Counter()
    skip_top_reasons: Counter[str] = Counter()
    for g in groups:
        for k in ("tests", "errors", "failures", "skipped"):
            totals[k] += g.get(k, 0)
        totals["time"] += g.get("time", 0.0)
        for s in g.get("skip_cases", []):
            skip_reasons[classify_reason(s["reason"])] += 1
            skip_top_reasons[_clip(s["reason"], 60)] += 1

    totals["passed"] = totals["tests"] - totals["failures"] - totals["errors"] - totals["skipped"]

    summary = {
        "farm_dir": str(farm.relative_to(PROJECT_ROOT)),
        "groups": groups,
        "totals": totals,
        "runtime_skip_by_cluster": dict(skip_reasons.most_common()),
        "runtime_skip_top_reasons": dict(skip_top_reasons.most_common(25)),
    }
    out = farm / "summary.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"组数: {len(groups)}")
    print(f"合计: {totals}")
    print("运行时 skip 聚类:")
    for k, v in skip_reasons.most_common():
        print(f"  {k:28s} {v}")
    print(f"\n结果已保存: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
