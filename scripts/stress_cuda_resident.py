"""C3.1 短时压力：Windows cuda 驻留零拷贝路径（计划表 B · 机器 B）。

范围（计划 §C3.1"先短时压力，再按风险延至 10h"——本脚本为**短压**档）：

1. **顺序播放压力**：驻留源顺序解码 × GL 会话导入循环（默认 90s），
   周期性内容 md5 抽查（对拍同解码器下载参考）；
2. **随机 seek 压力**：随机帧号 seek + 单帧导入（默认 60s），覆盖
   主会话 keyframe seek + decode-forward 路径与 slot 池churn；
3. **EOF/close/重开循环**：源生命周期边界；
4. 会话统计（slot 池水位/绑定/失败计数）与错误逐项记录。

退出码 0=全部通过（含内容抽查一致），2=有异常。
真机设备事件（TDR/拔卡）无法安全注入，登记为人工后续项。

用法：
    .venv/Scripts/python.exe scripts/stress_cuda_resident.py --tag C31_20261024_run01 --seq-seconds 90
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import av  # noqa: E402
import moderngl  # noqa: E402
from av.codec.hwaccel import HWAccel, HWDeviceType  # noqa: E402

from src.shared.media.frame_path import DecodePath  # noqa: E402
from src.shared.media.windows_hw.cuda_resident import (  # noqa: E402
    CudaGlInteropSession,
    WindowsCudaResidentSource,
)

TEST_VIDEOS = PROJECT_ROOT / "tests" / "test_videos"

RESULT: dict = {
    "schema": "stress_cuda_resident/1",
    "date": datetime.now().isoformat(timespec="seconds"),
    "phases": [],
}
ISSUES: list[str] = []


def _rows(buf, pitch: int, row_bytes: int, rows: int) -> bytes:
    if pitch == row_bytes:
        return bytes(buf[:row_bytes * rows])
    return b"".join(
        bytes(buf[r * pitch:r * pitch + row_bytes]) for r in range(rows))


def _download_reference_md5(path: Path, frame_index: int) -> tuple:
    """同解码器下载参考（is_hw_owned=False 会话，免疫 -129 毒化）。"""
    c = av.open(str(path), hwaccel=HWAccel(
        HWDeviceType.cuda, is_hw_owned=False))
    try:
        frame = None
        it = c.decode(c.streams.video[0])
        for i, f in enumerate(it):
            if i == frame_index:
                frame = f
                break
        if frame is None:
            return None
        out = []
        for idx, rows in ((0, frame.height), (1, frame.height // 2)):
            plane = frame.planes[idx]
            out.append(hashlib.md5(_rows(
                memoryview(plane), plane.line_size,
                frame.width, rows)).hexdigest())
        return tuple(out)
    finally:
        c.close()


def _readback_md5(session: CudaGlInteropSession, planes) -> tuple:
    import ctypes

    from OpenGL.GL import (
        glBindTexture, glGetTexImage, GL_TEXTURE_2D, GL_RED, GL_RG,
        GL_UNSIGNED_BYTE, GL_UNSIGNED_SHORT,
    )
    itemsize = 2 if planes[0].internal_format == "GL_R16" else 1
    out = []
    for pi, (gl_fmt, ncomp) in enumerate(((GL_RED, 1), (GL_RG, 2))):
        gl_type = (GL_UNSIGNED_BYTE if itemsize == 1 else GL_UNSIGNED_SHORT)
        w = planes[pi].width
        h = planes[pi].height
        nbytes = w * h * ncomp * itemsize
        buf = ctypes.create_string_buffer(nbytes)
        glBindTexture(GL_TEXTURE_2D, planes[pi].texture_id)
        glGetTexImage(GL_TEXTURE_2D, 0, gl_fmt, gl_type, buf)
        out.append(hashlib.md5(buf.raw).hexdigest())
    return tuple(out)


def stress_sequential(ctx, seconds: float, verify_every: int = 120) -> dict:
    path = TEST_VIDEOS / "4K_H264.mov"
    source = WindowsCudaResidentSource(str(path))
    source.open()
    session = CudaGlInteropSession(ring_size=8, max_ring_size=32)
    assert session.initialize()
    decode_path = DecodePath.WINDOWS_CUDA_GL_ZERO_COPY.value

    frames = 0
    errors: list[str] = []
    checks: list[dict] = []
    t0 = time.perf_counter()
    fi = 0
    try:
        while time.perf_counter() - t0 < seconds:
            try:
                frame = source.read_frame_resident(fi)
                result = session.bind_resident_frame(frame, decode_path)
                if result is None:
                    errors.append(
                        f"frame{fi} bind: {session.last_fallback_reason}")
                else:
                    planes, lease = result
                    lease.release()
                frame.release()
                frames += 1
                if frames % verify_every == 0:
                    got = _readback_md5(session, planes)
                    ref = _download_reference_md5(path, fi)
                    checks.append({
                        "frame": fi, "match": got == ref,
                    })
                    if got != ref:
                        errors.append(f"frame{fi} content mismatch")
                fi = (fi + 1) % 700
            except StopIteration:
                fi = 0  # wrap
            except Exception as exc:  # noqa: BLE001
                errors.append(f"frame{fi}: {type(exc).__name__}: {exc}")
                if len(errors) > 8:
                    break
    finally:
        stats = dict(session.stats)
        session.destroy()
        source.close()
    elapsed = time.perf_counter() - t0
    return {
        "seconds": round(elapsed, 1), "frames": frames,
        "fps": round(frames / elapsed, 1),
        "errors": errors[:6], "content_checks": checks,
        "session_stats": stats,
    }


def stress_random_seek(ctx, seconds: float) -> dict:
    path = TEST_VIDEOS / "4K_H264.mov"
    source = WindowsCudaResidentSource(str(path))
    source.open()
    session = CudaGlInteropSession(ring_size=4, max_ring_size=16)
    assert session.initialize()
    decode_path = DecodePath.WINDOWS_CUDA_GL_ZERO_COPY.value

    rng = random.Random(20261024)
    seeks = 0
    errors: list[str] = []
    t0 = time.perf_counter()
    try:
        while time.perf_counter() - t0 < seconds:
            fi = rng.randrange(0, 700)
            try:
                frame = source.read_frame_resident(fi)
                result = session.bind_resident_frame(frame, decode_path)
                if result is None:
                    errors.append(
                        f"seek{fi} bind: {session.last_fallback_reason}")
                else:
                    _, lease = result
                    lease.release()
                frame.release()
                seeks += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"seek{fi}: {type(exc).__name__}: {exc}")
                if len(errors) > 8:
                    break
    finally:
        stats = dict(session.stats)
        session.destroy()
        source.close()
    elapsed = time.perf_counter() - t0
    return {
        "seconds": round(elapsed, 1), "random_seeks": seeks,
        "seeks_per_s": round(seeks / elapsed, 1),
        "errors": errors[:6], "session_stats": stats,
    }


def stress_reopen_cycles(cycles: int = 8) -> dict:
    path = TEST_VIDEOS / "4K_H264.mov"
    errors = []
    decoded_total = 0
    for c in range(cycles):
        try:
            source = WindowsCudaResidentSource(str(path))
            source.open()
            frame = source.read_frame_resident(0)
            frame.release()
            source.close()
            decoded_total += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"cycle{c}: {type(exc).__name__}: {exc}")
    return {"cycles": cycles, "ok_cycles": decoded_total, "errors": errors[:4]}


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="")
    parser.add_argument("--seq-seconds", type=float, default=90.0)
    parser.add_argument("--seek-seconds", type=float, default=60.0)
    parser.add_argument("--verify-every", type=int, default=120)
    args = parser.parse_args()

    try:
        git = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True,
                             cwd=str(PROJECT_ROOT))
        RESULT["git_commit"] = git.stdout.strip()
    except Exception:
        RESULT["git_commit"] = "unavailable"

    ctx = moderngl.create_standalone_context()
    for name, fn in (
        ("sequential", lambda: stress_sequential(
            ctx, args.seq_seconds, args.verify_every)),
        ("random_seek", lambda: stress_random_seek(ctx, args.seek_seconds)),
        ("reopen_cycles", lambda: stress_reopen_cycles()),
    ):
        entry = {"phase": name, "status": "ok"}
        try:
            entry.update(fn())
        except Exception as exc:  # noqa: BLE001
            entry["status"] = "error"
            entry["error"] = f"{type(exc).__name__}: {exc}"
            ISSUES.append(f"{name}: {exc}")
            traceback.print_exc()
        RESULT["phases"].append(entry)
        print(f"[{entry['status']:>5}] {name}: "
              f"{json.dumps({k: v for k, v in entry.items() if k not in ('phase', 'status')}, ensure_ascii=False)[:220]}")
    ctx.release()

    RESULT["issues"] = ISSUES
    out = PROJECT_ROOT / "scripts" / (
        f"stress_cuda_resident_{args.tag}.json" if args.tag
        else "stress_cuda_resident.json")
    out.write_text(json.dumps(RESULT, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n结果已保存: {out}")
    print(f"issues={len(ISSUES)}")
    return 0 if not ISSUES else 2


if __name__ == "__main__":
    raise SystemExit(main())
