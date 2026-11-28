#!/usr/bin/env python3
"""Measure short native UHD windows and export registered image comparisons."""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import threading

from measure import (OUT, SOURCES, SAMPLES, WIDTH, HEIGHT, PIX_FMT, av_frames,
                     source_path, subset, topos_frames)

SPAN_LENGTH = 16


def spans(clip: str) -> list[tuple[int, int]]:
    return [(start + 8, SPAN_LENGTH) for start, _ in SAMPLES[clip]]


def indices(windows: list[tuple[int, int]]) -> list[int]:
    return [i for start, n in windows for i in range(start, start + n)]


def candidate(clip: str, tag: str, windows: list[tuple[int, int]]):
    _, fps, total = SOURCES[clip]
    path = OUT / f"{clip}_{tag}.mov"
    record = OUT / f"{clip}_{tag}.encode.json"
    original = indices(windows)
    if record.exists():
        mapping = json.loads(record.read_text())["frame_indices"]
        positions = {frame: i for i, frame in enumerate(mapping)}
        reduced = [positions[i] for i in original]
        return topos_frames(path, len(mapping), False, workers=3, frame_indices=reduced)
    if tag == "prores_ks":
        positions = {frame: i for i, frame in enumerate(indices(SAMPLES[clip]))}
        windows = [(positions[start], n) for start, n in windows]
    return av_frames(path, windows, fps)


def write_frames(fd: int, iterator, failures: list) -> None:
    try:
        with os.fdopen(fd, "wb", buffering=0) as output:
            for _, planes in iterator:
                for plane in planes:
                    data = memoryview(plane.tobytes())
                    while data:
                        n = output.write(data)
                        data = data[n:]
    except BaseException as error:
        failures.append(error)


def compare(clip: str, tag: str, start: int, count: int, kind: str) -> dict:
    _, fps, _ = SOURCES[clip]
    tag_file = f"{clip}_{tag}_{start}_{kind}"
    ext = "vmaf.json" if kind == "vmaf" else "ssim.raw"
    result = OUT / f"{tag_file}.{ext}"
    raw_opts = ["-f", "rawvideo", "-pixel_format", PIX_FMT, "-video_size",
                f"{WIDTH}x{HEIGHT}", "-framerate", str(fps)]
    r_out, w_out = os.pipe()
    r_src, w_src = os.pipe()
    metric = ("libvmaf=model=version=vmaf_4k_v0.6.1:log_fmt=json:"
              f"log_path={result}:n_threads=4:shortest=1") if kind == "vmaf" else (
              f"ssim=stats_file={result}:shortest=1")
    proc = subprocess.Popen([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-filter_complex_threads", "2",
        *raw_opts, "-i", f"pipe:{r_out}", *raw_opts, "-i", f"pipe:{r_src}",
        "-filter_complex", f"[0:v][1:v]{metric}", "-an", "-f", "null", "-",
    ], pass_fds=(r_out, r_src))
    os.close(r_out)
    os.close(r_src)
    failures = []
    feeds = [
        threading.Thread(target=write_frames,
                         args=(w_out, candidate(clip, tag, [(start, count)]), failures)),
        threading.Thread(target=write_frames, args=(
            w_src, av_frames(source_path(clip), [(start, count)], fps), failures)),
    ]
    for feed in feeds:
        feed.start()
    for feed in feeds:
        feed.join()
    code = proc.wait()
    if code or failures:
        raise RuntimeError((clip, tag, start, code, failures))
    if kind == "vmaf":
        data = json.loads(result.read_text())
        values = [x["metrics"]["vmaf"] for x in data["frames"]]
    else:
        values = [float(line.split("All:")[1].split()[0]) for line in result.read_text().splitlines()]
    assert len(values) == count, (result, len(values))
    return {"start": start, "values": values}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("vmaf", "ssim"))
    parser.add_argument("clip", choices=SOURCES)
    parser.add_argument("tags", nargs="+")
    parser.add_argument("--window", type=int, nargs=2, action="append", metavar=("START", "COUNT"))
    args = parser.parse_args()
    for tag in args.tags:
        rows = [compare(args.clip, tag, start, count, args.kind) for start, count in (args.window or spans(args.clip))]
        avg = sum(sum(row["values"]) for row in rows) / sum(len(row["values"]) for row in rows)
        suffix = ("_" + "_".join(f"{start}-{n}" for start, n in args.window)
                  if args.window else "")
        p = OUT / f"{args.clip}_{tag}{suffix}.{args.kind}.json"
        p.write_text(json.dumps({"average": avg, "windows": rows}, indent=2) + "\n")
        print(f"{args.clip}/{tag} {args.kind} {avg:.6f}", flush=True)


if __name__ == "__main__":
    main()
