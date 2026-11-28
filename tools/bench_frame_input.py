"""Native planar inputs shared by the Topos encoding benchmarks."""
from __future__ import annotations

from dataclasses import replace
from itertools import islice

from src.shared.media.frame import DecodedFrame


def load_frames(path: str, count: int) -> list[DecodedFrame]:
    """Load up to count frames without a color conversion or an RGB round trip."""
    from src.shared.media.topos_source import ToposMediaSource, is_topos_movie

    if is_topos_movie(path):
        with ToposMediaSource(path, read_ahead=0) as source:
            frames = []
            for i in range(min(count, source.total_frames)):
                frame = source.read_frame(i)
                frames.append(replace(frame, planes=tuple(plane.copy() for plane in frame.planes)))
    else:
        import av
        import numpy as np

        frames = []
        with av.open(path) as container:
            stream = container.streams.video[0]
            stream.codec_context.thread_count = 0
            stream.codec_context.thread_type = "FRAME"
            for i, frame in enumerate(islice(container.decode(stream), count)):
                fmt = frame.format
                if not fmt.is_planar or len(fmt.components) != len(frame.planes):
                    raise ValueError(f"{path}: benchmark requires independent planes, got {fmt.name}")
                planes = []
                for plane, component in zip(frame.planes, fmt.components, strict=True):
                    dtype = np.dtype("u1" if component.bits <= 8 else ">u2" if fmt.name.endswith("be") else "<u2")
                    stride = plane.line_size // dtype.itemsize
                    plane_data = np.frombuffer(plane, dtype=dtype).reshape(plane.height, stride)
                    planes.append(plane_data[:, :plane.width].copy())
                frames.append(DecodedFrame(i, frame.pts, float(frame.time or 0),
                                           planes=tuple(planes), pixel_format=fmt.name,
                                           width=frame.width, height=frame.height))
    if not frames:
        raise ValueError(f"{path}: benchmark input has no video frames")
    return frames
