"""Bounded traversal of MOV and ISO media boxes."""

from collections.abc import Callable, Iterator
from typing import BinaryIO


def iter_mov_atoms(data: bytes, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    """Yield a box's type and body bounds within its parent."""
    return _iter_mov_atom_headers(lambda offset: data[offset:min(offset + 16, end)], start, end)


def iter_file_mov_atoms(source: BinaryIO, end: int) -> Iterator[tuple[bytes, int, int]]:
    """Traverse top-level boxes without reading their contents."""
    def read_header(offset: int) -> bytes:
        source.seek(offset)
        return source.read(min(16, end - offset))

    return _iter_mov_atom_headers(read_header, 0, end)


def _iter_mov_atom_headers(
    read_header: Callable[[int], bytes], start: int, end: int,
) -> Iterator[tuple[bytes, int, int]]:
    offset = start
    while offset + 8 <= end:
        header = read_header(offset)
        if len(header) < 8:
            return
        size = int.from_bytes(header[:4], 'big')
        header_size = 8
        if size == 1:
            if len(header) < 16:
                return
            size = int.from_bytes(header[8:16], 'big')
            header_size = 16
        elif size == 0:
            size = end - offset
        if size < header_size or offset + size > end:
            return
        yield header[4:8], offset + header_size, offset + size
        offset += size


def find_mov_atom(data: bytes, start: int, end: int, kind: bytes) -> tuple[int, int] | None:
    """Find the first box of the given type within its parent."""
    for name, body_start, body_end in iter_mov_atoms(data, start, end):
        if name == kind:
            return body_start, body_end
    return None
