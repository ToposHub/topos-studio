"""Read Darwin's cached mount table without probing mounted filesystems."""

from __future__ import annotations

import ctypes
from functools import lru_cache
import os


class _StatFS64(ctypes.Structure):
    # Darwin SDK <sys/mount.h>, __DARWIN_STRUCT_STATFS64.
    _fields_ = [
        ("block_size", ctypes.c_uint32), ("io_size", ctypes.c_int32),
        ("counts", ctypes.c_uint64 * 5), ("fsid", ctypes.c_int32 * 2),
        ("owner", ctypes.c_uint32), ("type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32), ("subtype", ctypes.c_uint32),
        ("fs_type_name", ctypes.c_char * 16), ("mount_point", ctypes.c_char * 1024),
        ("device", ctypes.c_char * 1024), ("reserved", ctypes.c_uint32 * 8),
    ]


@lru_cache(maxsize=1)
def _mount_reader() -> ctypes._CFuncPtr:
    libc = ctypes.CDLL(None, use_errno=True)
    # Intel exports distinct inode ABIs; Apple Silicon exports the 64-bit ABI directly.
    reader = getattr(libc, "getfsstat$INODE64", None) or libc.getfsstat
    reader.argtypes = [ctypes.POINTER(_StatFS64), ctypes.c_int, ctypes.c_int]
    reader.restype = ctypes.c_int
    return reader


def cached_mounts() -> tuple[tuple[str, bytes], ...]:
    """Return mount points and device identifiers without issuing volume I/O."""
    nowait = 2  # MNT_NOWAIT requests cached statfs information on Darwin.
    read = _mount_reader()
    count = read(None, 0, nowait)
    if count < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    # Allow mounts added between the count and the snapshot.
    entries = (_StatFS64 * (count + 16))()
    count = read(entries, ctypes.sizeof(entries), nowait)
    if count < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return tuple((os.fsdecode(entry.mount_point), entry.device) for entry in entries[:count])
