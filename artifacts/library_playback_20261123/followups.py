"""Run diagnostic A/B cases sequentially, so benchmarks do not compete."""
from pathlib import Path
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PLAYER = "/tmp/topos-player-comparison-20261123.app/Contents/MacOS/comparison"
jobs = [
    ("player_chinese", [PLAYER, "1920", "1080", "测试素材01", str(HERE / "player_chinese.json")]),
    ("cadence_original", [sys.executable, str(HERE / "probe.py"), "--filter", "2K_prores422HQ", "--seconds", "9", "--trace", "--output", "cadence_original.json"]),
    ("cadence_poll5", [sys.executable, str(HERE / "probe.py"), "--filter", "2K_prores422HQ", "--seconds", "9", "--trace", "--poll-ms", "5", "--output", "cadence_poll5.json"]),
    ("dnx_slice8", [sys.executable, str(HERE / "stages.py"), "--filter", "4K_DNxHR 444 10bit", "--frames", "120", "--thread-type", "SLICE", "--threads", "8", "--output", "dnx_slice8.json"]),
]
for name, match in [("dnx444", "4K_DNxHR 444 10bit"), ("proresxq", "4K_prores4444XQ"),
                    ("toposxq", "4K_topos_4444xq"), ("2kproreshq", "2K_prores422HQ"),
                    ("hevc", "4K_H265")]:
    jobs.append(("studio_wide_" + name, [sys.executable, str(HERE / "probe.py"),
        "--filter", match, "--width", "3600", "--height", "1900", "--seconds", "7",
        "--output", "studio_wide_" + name + ".json"]))
    jobs.append(("player_wide_" + name, [PLAYER, "3600", "1900", match,
        str(HERE / ("player_wide_" + name + ".json"))]))
for name, command in jobs:
    print("START", name, flush=True)
    start = time.monotonic()
    with (HERE / (name + ".log")).open("w") as log:
        try:
            result = subprocess.run(command, cwd=ROOT, stdout=log,
                                    stderr=subprocess.STDOUT, timeout=90)
            print("END", name, "rc", result.returncode,
                  "seconds", round(time.monotonic() - start, 1), flush=True)
        except subprocess.TimeoutExpired:
            print("TIMEOUT", name, flush=True)
