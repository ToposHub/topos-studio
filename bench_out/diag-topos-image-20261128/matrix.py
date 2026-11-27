from __future__ import annotations

import argparse
from dataclasses import dataclass
from hashlib import sha256
import json
import logging
from math import ceil
from pathlib import Path
import struct
import sys
from time import perf_counter
from typing import Any

import av
import numpy as np

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
sys.path.insert(0, str(ROOT))

from src.features.edit.core.sequence_classify import classify_import_batch
from src.shared.codec.topos_binding import ToposCodec, ToposMovieFile
from src.shared.codec.topos_image_binding import get_image_codec
from src.shared.codec.topos_profiles import available_topos_image_tiers
from src.shared.export.topos_encoder import ToposImageSequenceEncoder, ToposVideoEncoder
from src.shared.export.video_encoder import FFmpegEncoderConfig
from src.shared.media.factory import MediaSourceFactory
from src.shared.media.frame import DecodedFrame
from src.shared.media.metadata import Rational
from src.shared.media.topos_image_sequence import SequenceOptions, open_sequence
from src.shared.media.topos_source import ToposMediaSource

WIDTH, HEIGHT = 3840, 2160
FPS = Rational(25, 1)
THREADS = 8


@dataclass(frozen=True)
class Case:
    name: str
    image_profile: str
    movie_profile: str
    fmt: str
    depth: int
    qm: int
    qp: int
    family: str


def cases() -> list[Case]:
    profiles = {"int422": "standard", "int444": "4444", "float": "4444", "raw": "raw"}
    specs = [
        Case(t.tier_id, t.tier_id, profiles[t.family], t.pix_fmt,
             t.bit_depth, t.qmatrix, t.qp, t.family)
        for t in available_topos_image_tiers()
    ]
    return specs + [
        Case("422-int12", "standard", "standard", "yuv422p12le", 12, 0, 58, "int422"),
        Case("444-int12-xq", "4444xq", "4444xq", "gbrp12le", 12, 0, 58, "int444"),
    ]


def native_source() -> Path:
    candidates = Path("D:/\u6d4b\u8bd5\u7d20\u6750").rglob("*_4K_120fps_ProRes422.mov")
    return next(candidates)


def planes(frame: av.VideoFrame, pixel_format: str) -> tuple[np.ndarray, ...]:
    converted = frame.reformat(format=pixel_format)
    return tuple(
        np.frombuffer(p, dtype="<u2").reshape(p.height, p.line_size // 2)[:, :p.width].copy()
        for p in converted.planes
    )


def prepare_inputs(path: Path, count: int, source: Path, formats: set[str]) -> None:
    source_marker = path / "source.txt"
    if source_marker.exists() and source_marker.read_text(encoding="utf-8") != str(source):
        raise ValueError(f"{path}: different source already prepared, use another output root")
    pending = {
        (fmt, i) for fmt in formats for i in range(count)
        if not (path / fmt / f"{i}.npz").is_file()
    }
    if not pending:
        return
    with av.open(str(source)) as container:
        for i, frame in enumerate(container.decode(video=0)):
            if i == count:
                break
            assert (frame.width, frame.height) == (WIDTH, HEIGHT)
            yuv10 = planes(frame, "yuv422p10le")
            gbr10 = planes(frame, "gbrp10le")
            gbr12 = planes(frame, "gbrp12le")
            g, b, r = [
                np.power(p.astype(np.float32) / 4095.0, 2.2)
                for p in gbr12
            ]
            # HDR-like linear plates and Bayer-phase inputs derived from real
            # footage for reproducible codec tests. No camera-RAW claim.
            rgb_half = (np.stack((r, g, b), axis=-1) * 2.4 - 0.015).astype(np.float16)
            phase = (r[0::2, 0::2], g[0::2, 1::2], g[1::2, 0::2], b[1::2, 1::2])
            raw16 = tuple(np.rint(p * 65535).astype("<u2") for p in phase)
            raw12 = tuple(
                np.rint(np.log1p(63 * p) / np.log(64) * 4095).astype("<u2")
                for p in phase
            )
            inputs = {
                "yuv422p10le": yuv10,
                "yuv422p12le": tuple(p << 2 for p in yuv10),
                "gbrp10le": gbr10,
                "gbrp12le": gbr12,
                "gbrph16le": (rgb_half,),
                "topos_cfa12le": raw12,
                "topos_cfa16le": raw16,
            }
            for fmt, channels in inputs.items():
                if (fmt, i) not in pending:
                    continue
                dest = path / fmt
                dest.mkdir(parents=True, exist_ok=True)
                np.savez(dest / f"{i}.npz", *channels)
    if any(not (path / fmt / f"{i}.npz").is_file() for fmt, i in pending):
        raise ValueError(f"{source}: expected at least {count} decoded frames")
    source_marker.write_text(str(source), encoding="utf-8")


def load_inputs(path: Path, case: Case, count: int) -> list[DecodedFrame]:
    frames = []
    for i in range(count):
        with np.load(path / case.fmt / f"{i}.npz") as content:
            arrays = tuple(content[key] for key in content.files)
        args = dict(data=arrays[0], pixel_format="rgbf16") if case.family == "float" else dict(
            planes=arrays, pixel_format=case.fmt)
        frames.append(DecodedFrame(i, i, i / 25, width=WIDTH, height=HEIGHT, **args))
    return frames


def encoder_for(case: Case, path: Path, sequence: bool) -> ToposVideoEncoder:
    # Fixed QP, matrix, packet version and slice count for the MOV comparator.
    # Bypass only its tier's default matrix/SAR choices; the sink is production.
    overrides = {}
    if case.name in ("422-int12", "444-int12-xq"):
        overrides["_effective_qmatrix_id"] = lambda self: case.qm
    if not sequence:
        overrides["_effective_qmatrix_id"] = lambda self: case.qm
    base = ToposImageSequenceEncoder if sequence else ToposVideoEncoder
    implementation = type("MatchedExport", (base,), overrides) if overrides else base
    config = FFmpegEncoderConfig(
        codec="topos_image_sequence" if sequence else "topos",
        profile=case.image_profile if sequence else case.movie_profile,
        width=WIDTH, height=HEIGHT, fps=FPS, pix_fmt=case.fmt,
        crf=case.qp, video_only=True,
    )
    return implementation(str(path), config, slice_threads=THREADS, slice_rows=16)


def export(case: Case, path: Path, frames: list[DecodedFrame], seq: bool) -> dict:
    encoder = encoder_for(case, path, seq)
    timings = {"codec": 0.0, "image_write": 0.0}

    def time_stage(function: Any, stage: str) -> Any:
        def observed(*args: Any, **kwargs: Any) -> Any:
            begin = perf_counter()
            result = function(*args, **kwargs)
            timings[stage] += perf_counter() - begin
            return result
        return observed

    encoder._encode_packet = time_stage(encoder._encode_packet, "codec")
    if seq:
        encoder._write_image_packet = time_stage(encoder._write_image_packet, "image_write")
    start = perf_counter()
    encoder.open()
    try:
        config = encoder._frame_cfg
        settings = [
            config.profile, config.pixel_format, config.bit_depth, config.qp_base,
            config.qp_delta_chroma, config.qmatrix_id, config.slice_rows, *config.reserved,
        ]
        frame_start = perf_counter()
        for frame in frames:
            encoder.encode_frame(frame)
        close_start = perf_counter()
        encoder.close()
    except Exception:
        encoder.abort()
        raise
    end = perf_counter()
    paths = sorted(path.glob("*.toos")) if seq else [path]
    assert len(paths) == (len(frames) if seq else 1)
    return {"export_fps": len(frames) / (end - start), "loop_s": close_start - frame_start,
            "open_ms": 1000 * (frame_start - start), "close_ms": 1000 * (end - close_start),
            "codec_fps": len(frames) / timings["codec"],
            "image_write_ms_per_frame": 1000 * timings["image_write"] / len(frames),
            "size_bytes": sum(p.stat().st_size for p in paths), "settings": settings}


def import_source(path: Path, count: int) -> dict:
    paths = sorted(path.glob("*.toos"))
    candidates = [str(p) for p in paths]
    start = perf_counter()
    groups, singles = classify_import_batch(candidates)
    assert len(groups) == 1 and not singles and groups[0].frame_count == count
    grouped = perf_counter()
    source = MediaSourceFactory.create(groups[0].first_path, auto_open=True)
    try:
        meta = source.probe()
        assert meta.codec_name == "topos-image-sequence" and meta.total_frames == count
        assert (meta.width, meta.height) == (WIDTH, HEIGHT)
        return {"group_ms": 1000 * (grouped - start),
                "open_ms": 1000 * (perf_counter() - grouped),
                "fmt": meta.pixel_format, "count": meta.total_frames}
    finally:
        source.close()


def read_rate(path: Path, count: int, seq: bool, preview: bool) -> dict:
    target = (1920, 1080) if preview else None
    if seq:
        source = open_sequence(str(path / "frame_0001.toos"), SequenceOptions(
            fps=FPS, target_size=target, prefetch_depth=0, plane_cache_bytes=0))
    else:
        source = ToposMediaSource(str(path), target_size=target, read_ahead=0,
                                  decode_workers=THREADS, decode_role="playback")
        source.open()
    configure = ToposCodec()
    configure.set_slice_threads(THREADS)
    try:
        first_started = perf_counter()
        frame = source.read_frame(0)
        first_ms = 1000 * (perf_counter() - first_started)
        geometry = [frame.width, frame.height]
        assert frame.planes and tuple(geometry) == (target or (WIDTH, HEIGHT))
        times = []
        frame_ms = []
        for _ in range(3):
            start = perf_counter()
            for index in range(1, count):
                frame_started = perf_counter()
                frame = source.read_frame(index)
                frame_ms.append(1000 * (perf_counter() - frame_started))
                assert frame.frame_index == index
            times.append(perf_counter() - start)
        frame_ms.sort()
        digest = sha256()
        for index in (0, count // 2, count - 1):
            frame = source.read_frame(index)
            for plane in frame.planes:
                digest.update(memoryview(np.ascontiguousarray(plane)).cast("B"))
        return {"fps": [(count - 1) / elapsed for elapsed in times], "sha": digest.hexdigest(),
                "first_ms": first_ms, "p95_ms": frame_ms[ceil(0.95 * len(frame_ms)) - 1],
                "p99_ms": frame_ms[ceil(0.99 * len(frame_ms)) - 1],
                "geometry": geometry, "fmt": frame.pixel_format, "dtype": str(frame.planes[0].dtype)}
    finally:
        source.close()


def payloads(case: Case, seq_path: Path, mov_path: Path, count: int) -> dict:
    image_codec = get_image_codec()
    doc = image_codec.validate(str(seq_path / "frame_0001.toos"), deep=True)
    digest = sha256()
    identical = 0
    payload_body_identical = 0
    version = None
    with ToposMovieFile(ToposCodec(), str(mov_path)) as mov:
        for index in range(count):
            path = seq_path / f"frame_{index + 1:04d}.toos"
            info = image_codec.probe(str(path))
            with path.open("rb") as file:
                _, directory, entry_size, entries = struct.unpack_from(">QQII", file.read(64), 16)
                file.seek(directory)
                records = [file.read(entry_size) for _ in range(entries)]
                record, = [r for r in records if r.startswith(b"PIXL")]
                offset, size = struct.unpack_from(">QQ", record, 8)
                file.seek(offset)
                data = file.read(size)
            assert len(data) == info.pixl_size
            video_packet = mov.packet(index)
            identical += data == video_packet
            payload_body_identical += data[53:] == video_packet[53:]
            digest.update(data)
            version = info.payload_version
    return {"identical": identical, "body_identical": payload_body_identical,
            "image_profile": doc.image_profile,
            "sample_kind": doc.sample_kind, "bit_depth": doc.bit_depth,
            "payload_version": version, "sha": digest.hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("build", choices=("old", "new", "smoke"))
    parser.add_argument("--frames", type=int, default=12)
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--only", nargs="+")
    parser.add_argument("--source", type=Path, help="3840×2160 source video")
    parser.add_argument("--output-root", type=Path, default=OUT)
    parser.add_argument("--decode-reference", type=Path,
                        help="existing matrix run containing per-tier .toos directories")
    parser.add_argument("--encode-only", action="store_true",
                        help="skip container import, decode timing, and pair verification")
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    if args.frames < 3:
        parser.error("--frames requires at least three input frames")
    specs = cases()
    if args.only:
        unknown = set(args.only) - {c.name for c in specs}
        if unknown:
            parser.error(f"unrecognized tiers: {', '.join(sorted(unknown))}")
        specs = [c for c in specs if c.name in args.only]
    source = (args.source or native_source()).resolve(strict=True)
    output_root = args.output_root.resolve()
    raw = output_root / "source"
    prepare_inputs(raw, args.frames, source, {c.fmt for c in specs})
    top = output_root / f"{args.build}-{args.round}"
    top.mkdir(exist_ok=False)
    codec = ToposCodec()
    library = Path(codec._lib._name)
    dll = sha256(library.read_bytes()).hexdigest()
    print(json.dumps({"event": "run", "build": args.build, "library": str(library),
                       "sha": dll, "round": args.round, "count": args.frames,
                       "source": str(source), "output_root": str(output_root)}), flush=True)
    if args.round % 2 == 0:
        specs.reverse()
    for case in specs:
        try:
            seq_path = top / case.name
            mov_path = top / f"{case.name}.mov"
            frames = load_inputs(raw, case, args.frames)
            outcomes = {}
            order = (True, False) if args.round % 2 else (False, True)
            for seq in order:
                if seq or args.build != "old":
                    outcomes["sequence" if seq else "mov"] = export(
                        case, seq_path if seq else mov_path, frames, seq)
            del frames
            record = {"case": case.name, "build": args.build, "round": args.round,
                      "count": args.frames, **outcomes}
            if args.encode_only:
                print(json.dumps(record), flush=True)
                continue
            if args.decode_reference:
                decode_seq = args.decode_reference / case.name
            elif args.build == "old":
                decode_seq = output_root / f"new-{args.round}" / case.name
            else:
                decode_seq = seq_path
            import_record = import_source(decode_seq, args.frames)
            dec = {key: read_rate(decode_seq, args.frames, True, key == "half")
                   for key in ("full", "half")}
            record.update({"import": import_record, "image_read": dec})
            if args.build != "old":
                record["movie_read"] = {
                    key: read_rate(mov_path, args.frames, False, key == "half")
                    for key in ("full", "half")
                }
                record["payload"] = payloads(case, seq_path, mov_path, args.frames)
            print(json.dumps(record), flush=True)
        except Exception as exc:
            print(json.dumps({"case": case.name, "round": args.round, "error": repr(exc)}),
                  flush=True)
            if args.build == "smoke":
                raise


if __name__ == "__main__":
    main()
