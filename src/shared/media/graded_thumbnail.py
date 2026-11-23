"""Shared limits for captured photo grades and their card previews."""

GRADED_PREVIEW_MAX_EDGE = 450
GRADED_PREVIEW_JPEG_HEADER = b"\xff\xd8\xff"


def is_webp_preview(data: bytes) -> bool:
    return data.startswith(b"RIFF") and data[8:12] == b"WEBP"
