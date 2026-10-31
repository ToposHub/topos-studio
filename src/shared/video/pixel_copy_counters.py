"""
Pixel Copy Byte Counters (P0-04)
CPU/GPU 像素拷贝字节审计计数器（性能总计划 §6 P0-04）。

## 职责

- 统计 CPU→GPU upload、GPU→CPU download/readback、surface map、
  texture write、cross-device copy 的累计字节数与发生次数。
- "零拷贝为 0" 只能由真实计数证明（fail-closed）：没有任何记录点
  写入时 snapshot 为 0，但 listeners 可以核对 counters_enabled。

## 用法

```python
from src.shared.video.pixel_copy_counters import add_pixel_copy, CopyKind

add_pixel_copy(CopyKind.CPU_UPLOAD, qimage.sizeInBytes())
add_pixel_copy(CopyKind.TEXTURE_WRITE, data.nbytes)

snapshot = snapshot_pixel_copies()          # 累计值
reset_pixel_copies()                        # 新 benchmark 会话前清零
```

计数器是进程级累计值（用于会话/基准报告）；每帧归因由
EditPlaybackFrameTrace 的 *_bytes 字段在测量点就地填充。
"""

from __future__ import annotations

import threading
from typing import Dict, Final, Tuple


class CopyKind:
    """拷贝种类词表（P0-04）。"""

    CPU_UPLOAD: Final[str] = "cpu_upload"                # CPU 内存 → GL/D3D/Metal 纹理
    CPU_DOWNLOAD: Final[str] = "cpu_download"            # GPU readback → CPU 内存
    SURFACE_MAP: Final[str] = "surface_map"              # IOSurface/D3D11 surface map 到 CPU
    TEXTURE_WRITE: Final[str] = "texture_write"          # GL texture.write()/texSubImage 字节
    CROSS_DEVICE: Final[str] = "cross_device"            # 跨 GPU adapter 拷贝


#: 全部合法 kind（snapshot 键形状固定，消费方无需判空）。
COPY_KINDS: Tuple[str, ...] = (
    CopyKind.CPU_UPLOAD,
    CopyKind.CPU_DOWNLOAD,
    CopyKind.SURFACE_MAP,
    CopyKind.TEXTURE_WRITE,
    CopyKind.CROSS_DEVICE,
)

_lock = threading.Lock()
_bytes: Dict[str, int] = {kind: 0 for kind in COPY_KINDS}
_events: Dict[str, int] = {kind: 0 for kind in COPY_KINDS}


def add_pixel_copy(kind: str, nbytes: int) -> None:
    """累加一次拷贝的字节数（线程安全；负数/未知 kind 被拒绝）。

    AUD-39304: 负数字节（如 signed 差值下溢的调用点 bug）与未知 kind
    一样抛 ValueError 拒绝——"零拷贝为 0 只能由真实计数证明"（fail-closed）
    的设计下不允许静默吞掉错误数据。nbytes == 0 仍是合法的零字节拷贝，
    保持静默跳过、不计事件。

    任何审计点都不应静默吞掉拷贝：如果这里报错说明调用点传参有 bug，
    允许向上抛（测量点都在 try 块外围或 best-effort 遥测内）。
    """
    if kind not in _bytes:
        raise ValueError(f"unknown copy kind: {kind!r}")
    nbytes = int(nbytes)
    if nbytes < 0:
        raise ValueError(
            f"negative copy bytes rejected (kind={kind!r}, nbytes={nbytes})"
        )
    if nbytes == 0:
        return
    with _lock:
        _bytes[kind] += nbytes
        _events[kind] += 1


def snapshot_pixel_copies() -> Dict[str, Dict[str, int]]:
    """返回当前累计快照：{kind: {"bytes": n, "events": n}}。"""
    with _lock:
        return {
            kind: {"bytes": _bytes[kind], "events": _events[kind]}
            for kind in COPY_KINDS
        }


def reset_pixel_copies() -> None:
    """清零全部计数（benchmark 会话开始时调用）。"""
    with _lock:
        for kind in COPY_KINDS:
            _bytes[kind] = 0
            _events[kind] = 0
