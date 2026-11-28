#!/usr/bin/env python3
"""Re-encode the worst full-length frames with current-frame size search."""
from __future__ import annotations

import json
import math
import struct
from collections import Counter

from measure import (OUT, SOURCES, WIDTH, HEIGHT, PIX_FMT, SHAPES, ToposVideoEncoder,
                     FFmpegEncoderConfig, ColorMetadataConfig, Rational, ToposDecoder,
                     ToposCodec, av, av_frames, cv2, np, source_path)


def psnr(expected, observed) -> float:
    return 10 * math.log10(1023**2 * expected.size /
                           cv2.norm(expected, observed, cv2.NORM_L2SQR))


def single_frame(clip: str, frame_index: int, budget: int) -> dict:
    _, fps, _ = SOURCES[clip]
    config = FFmpegEncoderConfig(
        codec="topos", profile="proxy", pix_fmt=PIX_FMT, width=WIDTH, height=HEIGHT,
        fps=Rational(fps.numerator, fps.denominator), color_metadata=ColorMetadataConfig.sdr(),
    )
    codec = ToposCodec()
    encoder = ToposVideoEncoder(str(OUT / f"{clip}_rateprobe.mov"), config, slice_threads=5)
    frame_stream = av_frames(source_path(clip), [(frame_index, 1)], fps)
    encoder.open()
    try:
        _, reference = next(frame_stream)
        planes = [np.ascontiguousarray(a) for a in reference]
        encoder._apply_slice_threads_once()
        packet, stats, qp = codec.encode_sized(encoder._frame_cfg, planes, budget)
        out = [np.empty(shape, dtype="<u2") for shape in SHAPES]
        decoder = ToposDecoder(codec)
        try:
            decoder.set_max_slice_workers(4)
            info = decoder.decode_views(packet, decoder.make_views(out), 3)
            assert info.concealed_slices == 0
        finally:
            decoder.close()
        return {"qp": qp, "bytes": len(packet), "y_psnr": psnr(reference[0], out[0]),
                "slice_count": stats.slice_count}
    finally:
        frame_stream.close()
        encoder.abort()


def inspect_qscale(path, top: bool) -> dict:
    distinct = []
    layouts = Counter()
    with av.open(str(path)) as container:
        for index, packet in enumerate(container.demux(video=0)):
            if not packet.size or index % 20:
                continue
            data = bytes(packet)
            if top:
                assert data[:4] == b"TPIC" and data[6] == 7
                cursor = struct.unpack_from(">H", data, 4)[0]
                count = struct.unpack_from(">H", data, 30)[0]
                q = []
                for _ in range(count):
                    q.append(data[cursor+9] - 64)
                    cursor += 17 + struct.unpack_from(">I", data, cursor)[0]
                assert cursor == len(data)
            else:
                assert data[4:8] == b"icpf"
                pic = 8 + struct.unpack_from(">H", data, 8)[0]
                ph = data[pic] >> 3
                m = data[pic+7] >> 4
                count = 135 * ((240 >> m) + (240 & ((1 << m)-1)).bit_count())
                cursor = pic + ph + count * 2
                q = []
                for size, in struct.iter_unpack(">H", data[pic+ph:cursor]):
                    q.append(data[cursor+1])
                    cursor += size
                assert 0 <= len(data) - cursor < 128
            layouts[count] += 1
            distinct.append(sorted(set(q)))
    return {
        "packet_sample_count": len(distinct), "slice_counts": layouts,
        "unique_qscale_count_min_median_max": [
            min(map(len, distinct)), float(np.median(list(map(len, distinct)))),
            max(map(len, distinct)),
        ],
        "all_encoded_values_or_deltas": sorted(set(value for frame in distinct for value in frame)),
    }


if __name__ == "__main__":
    output = {}
    for clip in SOURCES:
        tags = ("prores_apple", "topos_default", "topos_matched_full")
        pro, top, matched = [json.loads((OUT / f"{clip}_{tag}.score.json").read_text())
                             for tag in tags]
        worst = min(range(len(top)), key=lambda i: pro[i]["sse"][0] / top[i]["sse"][0])
        budget = pro[worst]["packet_bytes"]
        trial = single_frame(clip, worst, budget)
        orig = 10 * math.log10(WIDTH*HEIGHT*1023**2/matched[worst]["sse"][0])
        output[clip] = {
            "source_frame": worst, "apple_packet_bytes": budget,
            "topos_default": top[worst], "topos_matched": matched[worst],
            "topos_matched_y_psnr": orig,
            "single_frame_reanchor": trial,
            "qscale_inspection": {
                tag: inspect_qscale(OUT / f"{clip}_{tag}.mov", tag.startswith("topos"))
                for tag in ("topos_default", "prores_apple", "prores_ks")
            },
        }
        print(clip, worst, budget, matched[worst]["packet_bytes"], orig, trial, flush=True)
    (OUT / "rate_probe.json").write_text(json.dumps(output, indent=2) + "\n")
