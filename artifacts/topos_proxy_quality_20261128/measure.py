#!/usr/bin/env python3
"""Paired native 10-bit DNxHR to ProRes and Topos Proxy measurements."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from dataclasses import asdict
from fractions import Fraction
from pathlib import Path

import av
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
sys.path.insert(0, str(ROOT))
from src.shared.codec.color_metadata_config import ColorMetadataConfig
from src.shared.codec.topos_binding import (
    TC_DECODE_FLAG_NO_DEBLOCK, TC_DECODE_MODE_FULL, ToposCodec, ToposDecoder, ToposMovieFile,
)
from src.shared.export.video_encoder import FFmpegEncoderConfig
from src.shared.media.metadata import Rational
from src.shared.export.topos_encoder import ToposVideoEncoder
from tools.measure_topos_paired_quality import PlanarFrame

SOURCES = {
    "beihai": ("北海公园4K素材2.mov", Fraction(25), 1730),
    "yuanmingyuan": ("圆明园02.mov", Fraction(30000, 1001), 709),
}
SAMPLES = {
    "beihai": [(0, 40), (420, 40), (840, 40), (1260, 40), (1660, 40)],
    "yuanmingyuan": [(0, 40), (160, 40), (320, 40), (480, 40), (640, 40)],
}
WIDTH, HEIGHT = 3840, 2160
PIX_FMT = "yuv422p10le"
SHAPES = [(HEIGHT, WIDTH), (HEIGHT, WIDTH // 2), (HEIGHT, WIDTH // 2)]
cv2.setNumThreads(1)


def source_path(clip: str) -> Path:
    return (ROOT / "tests/test_videos/达芬奇转码_测速_2026-11-27"
            / "全长_422_HQ对照/DNxHR_HQX_10bit" / SOURCES[clip][0])


def digest(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def av_frames(path: Path, spans: list[tuple[int, int]], fps: Fraction):
    with av.open(str(path)) as c:
        stream = c.streams.video[0]
        stream.thread_type = "SLICE"
        stream.thread_count = 4
        assert stream.average_rate == fps, (path, stream.average_rate, fps)
        first = stream.start_time or 0
        for start, count in spans:
            timestamp = first + math.ceil(Fraction(start, 1) / (fps * stream.time_base))
            if start or first:
                c.seek(timestamp, stream=stream, backward=True)
            previous = start - 1
            for frame in c.decode(stream):
                pts = (frame.pts - first) * stream.time_base * fps
                assert pts.denominator == 1, (path, pts)
                if pts < start:
                    continue
                assert pts == previous + 1, (path, pts, previous)
                assert frame.format.name == PIX_FMT, (path, frame.format.name)
                planes = tuple(np.ndarray(shape, dtype="<u2", buffer=memoryview(plane),
                                          strides=(plane.line_size, 2))
                               for shape, plane in zip(SHAPES, frame.planes, strict=True))
                yield int(pts), planes
                previous = int(pts)
                if previous == start + count - 1:
                    break
            assert previous == start + count - 1, (path, previous, start, count)


def payloads(path: Path) -> list[int]:
    with av.open(str(path)) as c:
        return [p.size for p in c.demux(video=0) if p.size]


class ProbeEncoder(ToposVideoEncoder):
    def __init__(self, *args, qm: int | None, qpt2: bool,
                 per_frame_targets: list[int] | None, chroma_qp: int | None, **kwargs):
        self.qm = qm
        self.qpt2 = qpt2
        self.per_frame_targets = per_frame_targets
        self.chroma_qp = chroma_qp
        self.headers = []
        super().__init__(*args, **kwargs)

    def _effective_qmatrix_id(self) -> int:
        return super()._effective_qmatrix_id() if self.qm is None else self.qm

    def _output_features(self) -> int:
        return super()._output_features() | (self._FEATURE_QPT2 if self.qpt2 else 0)

    def _build_configs(self, alpha_bit_depth):
        movie, frame = super()._build_configs(alpha_bit_depth)
        if self.chroma_qp is not None:
            frame.qp_delta_chroma = self.chroma_qp
        return movie, frame

    def _encode_packet(self, planes, frame_cfg=None):
        if self.per_frame_targets is None:
            packet, stats = super()._encode_packet(planes, frame_cfg)
        else:
            assert frame_cfg is None
            self._apply_slice_threads_once()
            packet, stats, _ = self._codec.encode_sized(
                self._frame_cfg, planes, self.per_frame_targets[len(self.headers)],
                qp_min=0, qp_max=95)
        self.headers.append([int(stats.qp_base), len(packet)])
        return packet, stats


def encode(args: argparse.Namespace) -> None:
    clip, tag = args.clip, args.tag
    _, fps, total = SOURCES[clip]
    spans = args.span or (SAMPLES[clip] if args.sample else [(0, total)])
    frame_indices = [i for start, n in spans for i in range(start, start + n)]
    dest = OUT / f"{clip}_{tag}.mov"
    record = OUT / f"{clip}_{tag}.encode.json"
    assert not dest.exists() and not record.exists(), dest
    target_bytes = None
    target_bps = args.bitrate
    if args.match:
        sizes = payloads(OUT / f"{clip}_{args.match}.mov")
        assert len(sizes) == total
        target_bytes = [sizes[i] for i in frame_indices]
        target_bps = round(Fraction(sum(target_bytes) * 8, len(target_bytes)) * fps)
    cfg = FFmpegEncoderConfig(
        codec="topos", profile=args.tier, pix_fmt=PIX_FMT,
        width=WIDTH, height=HEIGHT, fps=Rational(fps.numerator, fps.denominator), bitrate=target_bps,
        color_metadata=ColorMetadataConfig.sdr(), aq_mode=args.aq,
    )
    codec = ToposCodec()
    encoder = ProbeEncoder(str(dest), cfg, slice_threads=args.threads,
                           qm=args.qm, qpt2=args.qpt2, chroma_qp=args.chroma_qp,
                           per_frame_targets=target_bytes if args.per_frame else None)
    began = time.perf_counter()
    encoder.open()
    try:
        movie_config = encoder._frame_cfg
        features = int(movie_config.reserved[5])
        matrix = int(movie_config.qmatrix_id)
        target = encoder._frame_target_bytes()
        for output_index, (source_index, planes) in enumerate(av_frames(source_path(clip), spans, fps)):
            assert frame_indices[output_index] == source_index
            encoder.encode_frame(PlanarFrame(
                tuple(np.ascontiguousarray(p) for p in planes), PIX_FMT,
                WIDTH, HEIGHT, output_index,
            ))
            if (output_index + 1) % 100 == 0:
                print(f"{clip}/{tag} {output_index + 1}/{len(frame_indices)}", flush=True)
        encoder.close()
    except BaseException:
        if encoder.is_open:
            encoder.abort()
        raise
    elapsed = time.perf_counter() - began
    with ToposMovieFile(codec, str(dest)) as movie:
        container_count = int(movie.info().sample_count)
    assert container_count == len(encoder.headers) == len(frame_indices)
    assert payloads(dest) == [item[1] for item in encoder.headers]
    report = {
        "clip": clip, "tag": tag, "source": str(source_path(clip)),
        "fps": str(fps), "frame_indices": frame_indices,
        "tier": args.tier, "qp_packet_bytes": encoder.headers,
        "target_bytes_per_frame": target, "target_bps_override": target_bps,
        "matched_reference": args.match, "matched_per_frame": args.per_frame,
        "qmatrix": matrix, "frame_features": features,
        "aq": args.aq, "chroma_qp_override": args.chroma_qp,
        "native_version": asdict(codec.version()),
        "native_path": str(Path(codec._lib._name).resolve()),
        "native_sha256": digest(Path(codec._lib._name)),
        "encode_seconds_including_dnxhr_decode": elapsed,
        "rate_control": encoder.rate_control_telemetry(),
    }
    record.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(f"{clip}/{tag} done {elapsed:.1f}s {sum(p[1] for p in encoder.headers)} bytes", flush=True)


def topos_frames(path: Path, count: int, no_deblock: bool, workers: int = 4, frame_indices=None):
    codec = ToposCodec()
    planes = [np.empty(shape, dtype="<u2") for shape in SHAPES]
    with ToposMovieFile(codec, str(path)) as movie:
        decoder = ToposDecoder(codec)
        try:
            assert movie.info().sample_count == count
            decoder.set_max_slice_workers(workers)
            views = decoder.make_views(planes)
            request = decoder.make_request(TC_DECODE_MODE_FULL, flags=TC_DECODE_FLAG_NO_DEBLOCK) if no_deblock else None
            for index in (range(count) if frame_indices is None else frame_indices):
                packet, n = movie.packet_into(index)
                info = (decoder.decode_views(packet, views, 3, n) if request is None
                        else decoder.decode_views_request(packet, views, request, n))
                assert info.concealed_slices == 0, (path, index, info)
                assert (info.bit_depth, info.pixel_format, info.visible_width, info.visible_height) == (
                    10, 0, WIDTH, HEIGHT), (path, index, info)
                yield index, planes
        finally:
            decoder.close()


def errors(ref, cand, extra: bool) -> dict:
    sse = [cv2.norm(a, b, cv2.NORM_L2SQR) for a, b in zip(ref, cand, strict=True)]
    row = {"sse": sse}
    if extra:
        y0, y1 = ref[0], cand[0]
        e = y1.astype(np.int16) - y0
        ex = np.abs(np.diff(e[::2, :], axis=1))
        ey = np.abs(np.diff(e[:, ::2], axis=0))
        on = (ex[:, 7::8].mean() + ey[7::8].mean()) / 2
        off = (ex[:, 3::8].mean() + ey[3::8].mean()) / 2
        row.update(
            y_grid_error=float(on), y_off_grid_error=float(off),
            y_grid_excess=float(on - off),
            y_mae=float(cv2.norm(y0, y1, cv2.NORM_L1) / y0.size),
            y_bias=float(np.mean(e)),
            y_p95_abs=float(np.percentile(np.abs(e[::4, ::4]), 95)),
        )
    return row


def subset(iterator, indices):
    try:
        for desired in indices:
            for index, item in iterator:
                if index == desired:
                    yield index, item
                    break
            else:
                raise ValueError(f"Missing frame {desired}")
    finally:
        iterator.close()


def score(args: argparse.Namespace) -> None:
    clip, tag = args.clip, args.tag
    _, fps, total = SOURCES[clip]
    path = OUT / f"{clip}_{tag}.mov"
    record = OUT / f"{clip}_{tag}.encode.json"
    encoded = json.loads(record.read_text()) if record.exists() else None
    if encoded:
        sequence = encoded["frame_indices"]
        groups = []
        for index in sequence:
            if groups and index == sum(groups[-1]):
                start, count = groups[-1]
                groups[-1] = (start, count + 1)
            else:
                groups.append((index, 1))
        spans = SAMPLES[clip] if args.sample else groups
    else:
        spans = SAMPLES[clip] if args.sample else [(0, total)]
    if encoded:
        small = len(encoded["frame_indices"]) < total
        candidate = topos_frames(path, len(encoded["frame_indices"]), args.no_deblock)
        sizes = [size for _, size in encoded["qp_packet_bytes"]]
        if args.sample and not small:
            indices = (i for start, count in spans for i in range(start, start + count))
            candidate = subset(candidate, indices)
    else:
        sizes = payloads(path)
        assert len(sizes) == (sum(n for _, n in spans) if args.compact else total)
        candidate = av_frames(path, [(0, len(sizes))] if args.compact else spans, fps)
    rows = []
    for i, ((source_index, ref), (candidate_index, out)) in enumerate(zip(
            av_frames(source_path(clip), spans, fps), candidate, strict=True)):
        compact = args.compact or (encoded and len(encoded["frame_indices"]) < total)
        assert candidate_index == (i if compact else source_index)
        frame = errors(ref, out, args.extra or i % 12 == 0)
        frame.update(frame=source_index, packet_bytes=sizes[candidate_index])
        if encoded:
            assert source_index == encoded["frame_indices"][candidate_index]
            frame["qp"] = encoded["qp_packet_bytes"][candidate_index][0]
        rows.append(frame)
    name = tag + ("_no_deblock" if args.no_deblock else "") + ("_sample" if args.sample else "")
    outfile = OUT / f"{clip}_{name}.score.json"
    outfile.write_text(json.dumps(rows, separators=(",", ":")) + "\n")
    print(f"{clip}/{name} scored {len(rows)} frames", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for action in (encode, score):
        sub = actions.add_parser(action.__name__)
        sub.add_argument("clip", choices=SOURCES)
        sub.add_argument("tag")
        sub.add_argument("--sample", action="store_true")
        sub.set_defaults(run=action)
        if action is encode:
            sub.add_argument("--span", type=int, nargs=2, action="append", metavar=("START", "COUNT"))
            sub.add_argument("--tier", default="proxy")
            sub.add_argument("--threads", type=int, default=6)
            sub.add_argument("--bitrate", type=int)
            sub.add_argument("--match")
            sub.add_argument("--qm", type=int)
            sub.add_argument("--qpt2", action="store_true")
            sub.add_argument("--aq", choices=("on", "off"), default="off")
            sub.add_argument("--chroma-qp", type=int)
            sub.add_argument("--per-frame", action="store_true")
        else:
            sub.add_argument("--no-deblock", action="store_true")
            sub.add_argument("--extra", action="store_true")
            sub.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
