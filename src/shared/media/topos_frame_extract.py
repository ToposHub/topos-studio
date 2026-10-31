"""Zero-reencode extraction of a Topos MOV frame as a ``.toos`` image.

Topos MOV samples are already self-contained TPIC elementary packets.  This
module only demuxes one packet and wraps it in the TPIM image envelope; it does
not decode pixels or run the encoder again.  One exception (v1.8, ADR-C056):
inter-frame sources (LP / V7-R3, V9) get a decode precheck so a reference-
dependent P packet is rejected instead of published as an undecodable image.
"""
from __future__ import annotations

import operator
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.shared.codec.topos_binding import (
    ToposCodec,
    ToposCodecError,
    ToposMovieFile,
)
from src.shared.codec.topos_image_binding import (
    ToposImageCodec,
    get_image_codec,
)


@dataclass(frozen=True)
class ToposFrameExtractionResult:
    """Result of one MOV sample → ``.toos`` extraction."""

    movie_path: str
    output_path: str
    frame_index: int
    pts: int
    duration: int
    packet_bytes: int
    file_bytes: int
    image_profile: int


def _image_profile_for_movie(summary) -> int:
    """Map a Topos MOV stream to the matching TPIM image profile.

    The mapping is intentionally strict and mirrors the native image writer's
    cross-validation rules.  A MOV configuration that cannot be represented by
    a v1 ``.toos`` envelope is rejected before any output is published.
    """
    profile = int(summary.profile)
    pixel_format = int(summary.pixel_format)
    bit_depth = int(summary.bit_depth)
    if profile == 3 and pixel_format == 0 and bit_depth in (10, 12):
        return 0  # YUV 4:2:2 Preview
    if profile == 5 and pixel_format == 2 and bit_depth == 10:
        return 1  # GBR 4:4:4 HQ
    if profile in (5, 6) and pixel_format == 2 and bit_depth == 12:
        return 2  # GBR 4:4:4 XQ
    if (profile == 7 and pixel_format == 3 and bit_depth in (12, 16)):
        return 3  # TRAW RAW（12=LOG0 制作 / 16=linear 归档，批 4）
    raise ToposCodecError(
        "Topos MOV stream cannot be represented as a v1 .toos image "
        f"(profile={profile}, pixel_format={pixel_format}, bit_depth={bit_depth})"
    )


class ToposFrameExtractor:
    """Reusable extractor for one Topos MOV.

    Keep one instance when exporting multiple stills from the same movie so
    the MOV index, file descriptor and packet scratch buffer are reused.
    """

    def __init__(self, movie_path: str, *, codec: Optional[ToposCodec] = None,
                 image_codec: Optional[ToposImageCodec] = None) -> None:
        self._movie_path = str(movie_path)
        self._codec = codec or ToposCodec()
        self._image_codec = image_codec or get_image_codec()
        self._movie: Optional[ToposMovieFile] = None
        try:
            self._movie = ToposMovieFile(self._codec, self._movie_path)
            self._summary = self._movie.info()
            self._image_profile = _image_profile_for_movie(self._summary)
            self._interframe = self._detect_interframe(self._summary)
        except Exception:
            self.close()
            raise

    def _detect_interframe(self, summary) -> bool:
        """帧间载体识别（与 topos_source 同口径；ADR-C048）：V9 流 tpcC
        major=9；V7-R3 与帧内 V7-R2 同 major=7——靠首包熵字节区分
        （R3=8，R2=7；包头自描述）。"""
        major = int(getattr(summary, "bitstream_major", 0) or 0)
        if major == 9:
            return True
        if major == 7 and int(summary.sample_count) > 0:
            pkt0 = self._movie.packet(0)
            return len(pkt0) > 45 and pkt0[45] == 8 and pkt0[15] == 0
        return False

    @property
    def frame_count(self) -> int:
        """Number of samples in the source MOV."""
        return int(self._summary.sample_count)

    @property
    def image_profile(self) -> int:
        """TPIM image profile used for all frames in this source stream."""
        return self._image_profile

    def extract(self, frame_index: int, output_path: str, *,
                overwrite: bool = False, durable: bool = True
                ) -> ToposFrameExtractionResult:
        """Extract one frame without decoding or re-encoding.

        ``frame_index`` is the zero-based MOV sample index.  The output must
        use the canonical ``.toos`` suffix.  Existing output is rejected unless
        ``overwrite=True``; writes use the image binding's atomic temp+rename
        path and optional fsync.
        """
        movie = self._movie
        if movie is None:
            raise RuntimeError("ToposFrameExtractor is closed")
        try:
            index = operator.index(frame_index)
        except TypeError as exc:
            raise TypeError("frame_index must be an integer") from exc
        if index < 0 or index >= self.frame_count:
            raise IndexError(
                f"Topos MOV frame index {index} out of range [0, {self.frame_count})"
            )
        target = Path(output_path)
        if target.suffix.lower() != ".toos":
            raise ValueError(
                f"Topos image output must use the .toos suffix: {output_path!r}"
            )
        if target.exists() and not overwrite:
            raise FileExistsError(str(target))

        packet = movie.packet(index)
        pts, duration = movie.pts(index)
        if self._interframe:
            # LP（v1.8）等帧间源：P 帧包依赖参考帧，无状态解码入口显式
            # TC_ERR_STATE 拒收——照原样封装会产出"写成功但永远解不开"的
            # .toos（静默数据丢失）。I 帧不受影响；拒绝时不落任何文件。
            try:
                self._codec.decode(packet)
            except ToposCodecError as exc:
                raise ToposCodecError(
                    f"帧 {index} 是帧间（P）包，无法独立封装 .toos——"
                    "请提取 I 帧（GOP 首帧）或改用帧内档位 MOV"
                ) from exc
        # v1.7 TMET：源 MOV 的 tpcD 档位随帧转发（提取不改码流——档位语义
        # 不变；旧源无 tpcD 时 tier_id=0，仅落 vendor）。
        from src.shared.codec.topos_meta import (
            TOPOS_IMAGE_META_CHUNK_TYPE,
            encode_topos_meta,
        )
        from src.shared.codec.topos_profiles import topos_tier_label_by_numeric_id
        tier_id = int(getattr(self._summary, "tier_id", 0) or 0)
        meta_payload = encode_topos_meta(
            tier_id=tier_id,
            vendor="TOPOS",
            label=topos_tier_label_by_numeric_id(tier_id) or "",
        )
        file_bytes = self._image_codec.write_packet(
            str(target), packet, self._image_profile, durable=durable,
            extra_chunks=[(TOPOS_IMAGE_META_CHUNK_TYPE, meta_payload)],
        )
        return ToposFrameExtractionResult(
            movie_path=self._movie_path,
            output_path=str(target),
            frame_index=index,
            pts=int(pts),
            duration=int(duration),
            packet_bytes=len(packet),
            file_bytes=int(file_bytes),
            image_profile=self._image_profile,
        )

    def close(self) -> None:
        """Close the MOV handle; safe to call repeatedly."""
        movie, self._movie = self._movie, None
        if movie is not None:
            movie.close()

    def __enter__(self) -> "ToposFrameExtractor":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def extract_topos_frame_to_toos(
    movie_path: str,
    frame_index: int,
    output_path: str,
    *,
    codec: Optional[ToposCodec] = None,
    image_codec: Optional[ToposImageCodec] = None,
    overwrite: bool = False,
    durable: bool = True,
) -> ToposFrameExtractionResult:
    """Convenience API for one Topos MOV frame → ``.toos`` extraction.

    For a batch, use :class:`ToposFrameExtractor` directly so the source MOV
    handle and packet buffer remain open across calls.
    """
    with ToposFrameExtractor(
        movie_path, codec=codec, image_codec=image_codec
    ) as extractor:
        return extractor.extract(
            frame_index, output_path, overwrite=overwrite, durable=durable
        )


__all__ = [
    "ToposFrameExtractionResult",
    "ToposFrameExtractor",
    "extract_topos_frame_to_toos",
]
