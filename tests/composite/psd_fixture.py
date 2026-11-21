"""Minimal hand-crafted PSD writer for B07 fixtures (test-only).

Builds version-1 PSD documents with raw (uncompressed) 8-bit RGBA pixel
layers — just enough structure for psd-tools to read layers, their
offsets, opacity, visibility and names.  No production dependency; the
byte layout follows the Adobe PSD specification.
"""

from __future__ import annotations

import struct
from typing import Dict, List, Optional


def _pascal_name(name: str) -> bytes:
    raw = name.encode("utf-8")[:255]
    data = bytes([len(raw)]) + raw
    while len(data) % 4:
        data += b"\x00"
    return data


def _layer_channel(payload: bytes, channel_id: int) -> bytes:
    # compression 0 (raw) marker + plane data; length prefix added per spec
    # position in the layer record.
    return struct.pack(">hI", channel_id, len(payload) + 2) + b"\x00\x00" + payload


def build_psd(width: int, height: int,
              layers: List[Dict],
              merged_rgb: Optional[bytes] = None) -> bytes:
    """Assemble a PSD byte stream.

    Each layer dict:
      name, left, top, right, bottom,
      rgba: {"r": plane, "g": plane, "b": plane, "a": plane} or
      fill: (r, g, b, a) uniform byte values (planes generated),
      opacity: 0-255, visible: bool, blend: "norm"
    Planes hold (bottom-top) × (right-left) raw bytes.
    """
    channel_count = 3
    header = struct.pack(
        ">4sH6sHIIHH", b"8BPS", 1, b"\x00" * 6, channel_count,
        height, width, 8, 3)  # 8 bpc, mode 3 = RGB
    color_mode_data = struct.pack(">I", 0)
    image_resources = struct.pack(">I", 0)

    records = bytearray()
    channel_blobs = bytearray()
    for layer in layers:
        left, top = int(layer["left"]), int(layer["top"])
        right, bottom = int(layer["right"]), int(layer["bottom"])
        w, h = max(0, right - left), max(0, bottom - top)
        if "rgba" in layer:
            planes = {cid: layer["rgba"][key]
                      for cid, key in ((0, "r"), (1, "g"), (2, "b"), (-1, "a"))}
        else:
            r, g, b, a = layer.get("fill", (255, 0, 0, 255))
            planes = {cid: bytes([value]) * (w * h)
                      for cid, value in ((0, r), (1, g), (2, b), (-1, a))}
        records += struct.pack(">iiii", top, left, bottom, right)
        records += struct.pack(">H", len(planes))
        for cid in (0, 1, 2, -1):
            plane = planes[cid]
            records += struct.pack(">hI", cid, len(plane) + 2)
        records += b"8BIM"
        records += (layer.get("blend", "norm").encode("ascii")
                    if len(layer.get("blend", "norm")) == 4
                    else b"norm")
        records += struct.pack(">BBBB", int(layer.get("opacity", 255)),
                               0, 0 if layer.get("visible", True) else 2, 0)
        name_block = _pascal_name(str(layer["name"]))
        extra = struct.pack(">I", 0) + struct.pack(">I", 0) + name_block
        records += struct.pack(">I", len(extra)) + extra
        for cid in (0, 1, 2, -1):
            channel_blobs += b"\x00\x00" + planes[cid]

    layer_info_body = struct.pack(">h", len(layers)) + bytes(records) + bytes(channel_blobs)
    if len(layer_info_body) % 2:
        layer_info_body += b"\x00"
    layer_info_section = struct.pack(">I", len(layer_info_body)) + layer_info_body
    layer_and_mask_body = layer_info_section + struct.pack(">I", 0)  # global mask info
    layer_and_mask = struct.pack(">I", len(layer_and_mask_body)) + layer_and_mask_body

    if merged_rgb is None:
        merged_rgb = bytes([0]) * (width * height)
    merged = struct.pack(">H", 0)  # raw compression
    for _ in range(3):
        merged += merged_rgb

    return header + color_mode_data + image_resources + layer_and_mask + merged


def sample_layer_document() -> bytes:
    """The B07 acceptance fixture: 16×16 canvas with four pixel layers.

    * ``Back``     — full-canvas opaque red (visible).
    * ``Half``     — 8×8 at (4, 4) with 50 % alpha (0x80) green.
    * ``NegOff``   — 8×8 at (-4, -4) partially outside the canvas, blue.
    * ``Outside``  — 4×4 at (-32, -32), fully outside the canvas.
    * ``Dup`` ×2   — same display name, distinct pixels (no name guessing).
    """
    layers = [
        dict(name="Back", left=0, top=0, right=16, bottom=16,
             fill=(255, 0, 0, 255), visible=True, opacity=255),
        dict(name="Half", left=4, top=4, right=12, bottom=12,
             fill=(0, 255, 0, 128), visible=True, opacity=255),
        dict(name="NegOff", left=-4, top=-4, right=4, bottom=4,
             fill=(0, 0, 255, 255), visible=True, opacity=255),
        dict(name="Outside", left=-32, top=-32, right=-28, bottom=-28,
             fill=(255, 255, 0, 255), visible=True, opacity=255),
        dict(name="Dup", left=0, top=0, right=2, bottom=2,
             fill=(10, 10, 10, 255), visible=True, opacity=255),
        dict(name="Dup", left=2, top=0, right=4, bottom=2,
             fill=(20, 20, 20, 255), visible=True, opacity=255),
    ]
    return build_psd(16, 16, layers)
