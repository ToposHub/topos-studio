"""Run the Edit tests in bounded independent processes, retaining every result."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parents[1] / "regression"
OUT.mkdir(parents=True, exist_ok=True)
FILES = sorted(ROOT.glob("tests/edit/**/test*.py"))


def run_group(index: int, paths: list[Path]) -> dict:
    """Bound native hangs without treating an incomplete batch as passed."""
    name = f"batch_{index:02d}"
    log_path = OUT / f"{name}.log"
    xml_path = OUT / f"{name}.xml"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", OPENBLAS_NUM_THREADS="1",
               TOPOS_CONFIG_PATH=f"/tmp/topos-regression-20261021/{name}/config.json")
    command = [sys.executable, "-m", "pytest", "-q", "--tb=short", "-o",
               "faulthandler_timeout=30", f"--junitxml={xml_path}"]
    command.extend(str(p.relative_to(ROOT)) for p in paths)
    start = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        timeout = False
        try:
            rc = process.wait(timeout=90)
        except subprocess.TimeoutExpired:
            timeout = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                rc = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                rc = process.wait()
    counts: dict[str, int] = {}
    if xml_path.exists():
        suites = ET.parse(xml_path).getroot()
        counts = {key: sum(int(s.get(key, "0")) for s in suites.iter("testsuite"))
                  for key in ("tests", "failures", "errors", "skipped")}
    return {"batch": name, "files": [str(p.relative_to(ROOT)) for p in paths],
            "command": command, "exit_code": rc, "timeout": timeout,
            "elapsed_seconds": round(time.monotonic() - start, 2), "counts": counts}


def main() -> None:
    """Save partial progress atomically so interruptions remain auditable."""
    groups = [FILES[i:i + 20] for i in range(0, len(FILES), 20)]
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = [pool.submit(run_group, i, paths) for i, paths in enumerate(groups)]
        for job in as_completed(jobs):
            result = job.result()
            results.append(result)
            ordered = sorted(results, key=lambda r: r["batch"])
            (OUT / "summary.json").write_text(json.dumps({"file_count": len(FILES),
                "batch_count": len(groups), "completed_batches": len(results),
                "results": ordered}, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
