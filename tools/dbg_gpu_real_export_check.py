#!/usr/bin/env python3
"""P0-GPU 真实工程验证：真实 4K Topos 素材的 GPU/CPU 成对导出。

构造真实 EditProject + TimelineSequence + FrameProvider（provider 返回
从 tests/test_videos/4K_topos.mov 真实解码的 planar yuv422p10le 帧），
跑完整 export_timeline 两次：

  A. GPU 合成（默认，产品路径含 render∥encode 流水线）
  B. CPU 合成（_gpu_render_disabled=True）

对比：(1) 交给编码器前的帧级 parity（_encode_frame 捕获）；
(2) 导出墙钟；(3) 输出文件再解码的帧级 parity。

用法：
    python tools/dbg_gpu_real_export_check.py [--frames 8] [--media PATH]
        [--no-grade]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

GRADE_TYPICAL = {
    "exposure": 8.0, "temp": 4.0, "tint": -2.0, "contrast": 6.0,
    "lift": (1.5, 0.0, -1.5), "gamma": (0.0, 2.0, 0.0),
    "gain": (2.0, 0.0, -2.0), "shadow": -8.0, "highlights": 6.0,
    "vibrance": 10.0, "saturation": 5.0,
}


def decode_real_frames(path: str, count: int):
    """用 Topos 解码真实 planar 帧 → DecodedFrame 列表。"""
    from src.shared.codec import topos_binding as tb
    from src.shared.media.frame import DecodedFrame

    codec = tb.ToposCodec()
    movie = tb.ToposMovieFile(codec, path)
    total = movie.info().sample_count
    n = min(count, total)
    meta = SimpleNamespace(color_range="limited", color_matrix="bt709")
    frames = []
    for i in range(n):
        f = codec.decode(movie.packet(i))
        planes = [np.frombuffer(p, dtype=np.uint16) for p in f.planes]
        w, h = f.info.visible_width, f.info.visible_height
        y = planes[0].reshape(h, w)
        u = planes[1].reshape(h, w // 2)
        v = planes[2].reshape(h, w // 2)
        frames.append(DecodedFrame(
            frame_index=i, pts=0, time_seconds=i / 24.0,
            planes=(np.ascontiguousarray(y), np.ascontiguousarray(u),
                    np.ascontiguousarray(v)),
            pixel_format="yuv422p10le", metadata=meta))
    movie.close()
    if not frames:
        raise RuntimeError(f"源无视频帧: {path}")
    return frames


class _Provider:
    """真实帧的最小 FrameProvider（帧已预解码，导出路径全程真实）。"""

    def __init__(self, frames):
        self._frames = frames

    def open_clip(self, path, share_by_path=False, **_kw):
        return "clip-real"

    def request_frame(self, clip_id, frame, quality="master"):
        return self._frames[int(frame) % len(self._frames)]

    def close_clip(self, clip_id, **_kw):
        pass


def run_export(media_path: str, frames, out_path: str, gpu: bool,
               with_grade: bool, capture: list):
    from src.features.edit.core.project import EditProject
    from src.features.edit.core.timeline_export import (
        ExportCodec, ExportRangeType, ExportSource, TimelineExportConfig,
        TimelineExporter,
    )
    from src.features.edit.core.timeline_v2 import (
        ClipInstance, EditTrack, TimelineSequence,
    )
    from src.shared.media.metadata import Rational

    h, w = frames[0].planes[0].shape
    project = EditProject.create("gpu-verify")
    project = project.import_asset(
        path=media_path, media_type="video",
        metadata={"width": w, "height": h, "fps": 24.0,
                  "total_frames": len(frames),
                  "duration_seconds": len(frames) / 24.0})
    asset_id = list(project.assets.keys())[-1]
    if with_grade:
        project.color_stacks["sid-verify"] = SimpleNamespace(
            grading_params=dict(GRADE_TYPICAL), layers=[],
            active_layer_index=None)
    clip = ClipInstance(
        clip_id="c1", asset_id=asset_id, track_id="t0", timeline_start=0,
        timeline_duration=len(frames), source_start=0,
        source_duration=len(frames),
        color_stack_id="sid-verify" if with_grade else None)
    sequence = TimelineSequence(
        name="gpu-verify", width=w, height=h, fps=Rational(24, 1),
        video_tracks=(EditTrack(
            track_id="t0", name="V1", track_type="video", index=0,
            clips=(clip,),),),
    )
    exporter = TimelineExporter(
        project=project, sequence=sequence,
        frame_provider=_Provider(frames))
    if not gpu:
        exporter._gpu_render_disabled = True

    orig_encode = exporter._encode_frame

    def _wrapped(frame_data):
        if len(capture) < 6:
            capture.append(np.array(frame_data, copy=True))
        return orig_encode(frame_data)

    exporter._encode_frame = _wrapped
    cfg = TimelineExportConfig(
        source=ExportSource.TIMELINE, timeline_id=sequence.timeline_id,
        range_type=ExportRangeType.ENTIRE, codec=ExportCodec.TOPOS_HQ,
        output_path=out_path, has_alpha=False, include_audio=False,
        burn_subtitles=False, quality=6, quality_mode="crf",
        resolution=(w, h))
    t0 = time.perf_counter()
    outcome = exporter.export_timeline(cfg)
    wall = time.perf_counter() - t0
    return outcome.success, wall, len(frames)


def decode_output(path: str, count: int):
    from src.shared.codec import topos_binding as tb

    codec = tb.ToposCodec()
    movie = tb.ToposMovieFile(codec, path)
    total = movie.info().sample_count
    n = min(count, total)
    out = []
    for i in range(n):
        f = codec.decode(movie.packet(i))
        planes = [np.frombuffer(p, dtype=np.uint16) for p in f.planes]
        w, h = f.info.visible_width, f.info.visible_height
        out.append((planes[0].reshape(h, w), planes[1].reshape(h, w // 2),
                    planes[2].reshape(h, w // 2)))
    movie.close()
    return out


def stats(a: np.ndarray, b: np.ndarray) -> str:
    d = np.abs(a.astype(np.int32) - b.astype(np.int32))
    return f"maxdiff {d.max()} | 平均 |Δ| {d.mean():.4f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=8)
    ap.add_argument("--media", default=None)
    ap.add_argument("--no-grade", action="store_true")
    args = ap.parse_args()

    candidates = [
        args.media,
        str(PROJECT_ROOT / "tests/test_videos/4K_topos.mov"),
        str(PROJECT_ROOT / "tests/test_videos/2K_topos.mov"),
    ]
    media = next((c for c in candidates if c and Path(c).is_file()), None)
    if media is None:
        print("未找到真实测试媒体（tests/test_videos/*_topos.mov）")
        return

    frames = decode_real_frames(media, args.frames)
    h, w = frames[0].planes[0].shape
    grade = "典型调色" if not args.no_grade else "无调色"
    print(f"== 真实导出对拍：{Path(media).name} {w}x{h} "
          f"yuv422p10le→TOPOS_HQ(u16 交付)，{len(frames)} 帧，{grade} ==")

    out_dir = PROJECT_ROOT / "bench_out"
    out_dir.mkdir(exist_ok=True)
    cap_gpu, cap_cpu = [], []
    ok_g, wall_g, n = run_export(media, frames, str(out_dir / "gpu_real.mov"),
                                 gpu=True, with_grade=not args.no_grade,
                                 capture=cap_gpu)
    ok_c, wall_c, _ = run_export(media, frames, str(out_dir / "cpu_real.mov"),
                                 gpu=False, with_grade=not args.no_grade,
                                 capture=cap_cpu)
    print(f"GPU: success={ok_g}  {wall_g:.2f}s ({n / wall_g:.2f} fps)")
    print(f"CPU: success={ok_c}  {wall_c:.2f}s ({n / wall_c:.2f} fps)")
    print(f"端到端加速比 {wall_c / max(wall_g, 1e-9):.1f}×")

    k = min(len(cap_gpu), len(cap_cpu))
    if k:
        print(f"\n-- 编码前帧级 parity（前 {k} 帧，dtype {cap_gpu[0].dtype}）--")
        for i in range(k):
            print(f"  frame{i}: {stats(cap_cpu[i], cap_gpu[i])}")

    print("\n-- 输出文件再解码 parity --")
    dec_g = decode_output(str(out_dir / "gpu_real.mov"), len(frames))
    dec_c = decode_output(str(out_dir / "cpu_real.mov"), len(frames))
    for i, (fg, fc) in enumerate(zip(dec_g, dec_c)):
        dy = np.abs(fg[0].astype(np.int32) - fc[0].astype(np.int32))
        print(f"  frame{i} Y: max {dy.max()} 平均 {dy.mean():.4f}")


if __name__ == "__main__":
    main()
