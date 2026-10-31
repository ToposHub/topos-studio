import os, subprocess, sys, glob

os.environ["QT_QPA_PLATFORM"] = "offscreen"
EXCLUDE = {
    "test_end_to_end_playback_real_videos.py",
    "test_p2_09_session_pool.py",
}
files = sorted(
    f for f in glob.glob("tests/edit/test_*.py")
    if os.path.basename(f) not in EXCLUDE
)
passed, failed, timed_out = [], [], []
for f in files:
    try:
        proc = subprocess.run(
            [".venv/bin/python", "-m", "pytest", "-q", f, "-p", "no:cacheprovider", "--no-header"],
            capture_output=True, text=True, timeout=150,
        )
        out = proc.stdout + proc.stderr
        if proc.returncode == 0:
            passed.append(f)
        else:
            failed.append((f, out))
            print(f"FAIL {f}", flush=True)
    except subprocess.TimeoutExpired:
        timed_out.append(f)
        print(f"TIMEOUT {f}", flush=True)
print(f"\n== PASS={len(passed)} FAIL={len(failed)} TIMEOUT={len(timed_out)} ==")
with open(".testrun/summary.txt", "w") as fh:
    fh.write(f"PASS {len(passed)}\nFAIL {len(failed)}\nTIMEOUT {len(timed_out)}\n")
    for f, out in failed:
        fh.write(f"\n===== FAIL {f} =====\n{out[-4000:]}\n")
    for f in timed_out:
        fh.write(f"TIMEOUT {f}\n")
