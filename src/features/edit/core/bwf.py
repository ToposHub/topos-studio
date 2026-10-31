"""BWF (Broadcast Wave Format) bext 支持 — EBU Tech 3285。

P0-06（2026-08-27 剪辑页审计）：此前录音只写 RIFF INFO tag（ICMT/IART/INAM），
却在外部能力表中标称 BWF available。本模块实现真正的 bext chunk：

- ``build_bext_chunk``：构建 602 字节固定头 + coding history 的 bext chunk
- ``inject_bext_chunk``：向已写好的 WAV 原子注入 bext（PyAV 不支持直接写 bext）
- ``read_bext_chunk``：独立解析器，供测试与导入方读取时间参考

bext 布局（v0，总固定长度 602 字节）::

    Description          256 bytes  ASCII，NUL 填充
    Originator            32 bytes  制作者/应用名
    OriginatorReference   32 bytes  制作者引用号
    OriginationDate       10 bytes  "YYYY-MM-DD"
    OriginationTime        8 bytes  "HH:MM:SS"
    TimeReference          8 bytes  uint64 LE，首样本的采样索引
    Version                2 bytes  uint16 LE
    Reserved             254 bytes  v0 保留（v1 为 UMID64+Reserved190）
    CodingHistory      变长 ASCII   每行以 CR/LF 结尾

TimeReference 是 ADR/对齐的关键字段：外部 DAW/NLE 依据它把 take 放回
时间线采样位置。coding history 遵循 EBU R98/
Tech 3285 附录格式（A=PCM,F=…,W=…,M=…,T=…）。
"""

from __future__ import annotations

import logging
import os
import struct
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, Optional, Tuple

logger = logging.getLogger(__name__)

BEXT_FIXED_SIZE = 602  # 不含 coding history 的固定部分（v0）


@dataclass(frozen=True)
class BextMetadata:
    """EBU Tech 3285 bext 元数据（不可变）。"""

    description: str = ""
    originator: str = "Topos Color"
    originator_reference: str = ""
    origination_date: str = ""  # "YYYY-MM-DD"，空则用当前日期
    origination_time: str = ""  # "HH:MM:SS"，空则用当前时间
    time_reference_samples: int = 0  # 首样本的采样索引（uint64）
    coding_history: str = ""
    version: int = 0

    def __post_init__(self) -> None:
        now = datetime.now()
        if not self.origination_date:
            object.__setattr__(self, "origination_date", now.strftime("%Y-%m-%d"))
        if not self.origination_time:
            object.__setattr__(self, "origination_time", now.strftime("%H:%M:%S"))
        if not self.originator_reference:
            object.__setattr__(self, "originator_reference", uuid.uuid4().hex[:32])


def default_coding_history(
    sample_rate: int,
    bit_depth: int,
    channels: int,
    codec: str = "PCM",
) -> str:
    """按 EBU R98 格式生成 coding history 行。"""
    mode = {1: "mono", 2: "stereo"}.get(channels, f"{channels}ch")
    return (
        f"A={codec},F={int(sample_rate)},W={int(bit_depth)},"
        f"M={mode},T=Topos Color\r\n"
    )


def _pad_ascii(value: str, size: int) -> bytes:
    """ASCII 编码并 NUL 填充到固定长度（超长截断）。"""
    raw = value.encode("ascii", errors="replace")[:size]
    return raw + b"\x00" * (size - len(raw))


def build_bext_chunk(meta: BextMetadata) -> bytes:
    """构建完整的 bext chunk 字节（含 8 字节 chunk 头，偶数对齐）。"""
    body = bytearray()
    body += _pad_ascii(meta.description, 256)
    body += _pad_ascii(meta.originator, 32)
    body += _pad_ascii(meta.originator_reference, 32)
    body += _pad_ascii(meta.origination_date, 10)
    body += _pad_ascii(meta.origination_time, 8)
    body += struct.pack("<Q", int(meta.time_reference_samples) & 0xFFFFFFFFFFFFFFFF)
    body += struct.pack("<H", int(meta.version) & 0xFFFF)
    # v0：Reserved 254 字节；v1：UMID 64 + Reserved 190（同为 254，v1 的
    # UMID 生成属 SMPTE 330M，超出本实现范围，故固定使用 v0 布局置零）
    body += b"\x00" * 254
    history = meta.coding_history.encode("ascii", errors="replace")
    body += history
    if len(body) % 2 == 1:
        body += b"\x00"  # WAV chunk 必须 word 对齐
    return b"bext" + struct.pack("<I", len(body)) + bytes(body)


def _iter_chunks(data: bytes) -> Iterator[Tuple[bytes, int, int]]:
    """遍历 WAV chunk，产出 (chunk_id, data_offset, size)。"""
    pos = 12
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        yield cid, pos + 8, size
        pos += 8 + size + (size & 1)


def inject_bext_chunk(wav_path: str, meta: BextMetadata) -> bool:
    """向已有 WAV 文件原子注入 bext chunk（插在 fmt 之后）。

    PyAV/FFmpeg 的 WAV muxer 不写 bext，因此先让 PyAV 写完整音频，
    再把 bext 字节拼入并修正 RIFF 尺寸。写入经临时文件 + os.replace
    原子完成，失败时原文件保持不变。

    Returns:
        成功 True；文件不是 RIFF/WAVE 或无 fmt chunk 时 False。
    """
    try:
        with open(wav_path, "rb") as f:
            data = f.read()
    except OSError as e:
        logger.warning(f"bext inject: cannot read {wav_path}: {e}")
        return False

    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        logger.warning(f"bext inject: not a RIFF/WAVE file: {wav_path}")
        return False

    insert_at: Optional[int] = None
    for cid, offset, size in _iter_chunks(data):
        if cid == b"fmt ":
            insert_at = offset + size + (size & 1)
            break
    if insert_at is None:
        logger.warning(f"bext inject: no fmt chunk found in {wav_path}")
        return False

    bext = build_bext_chunk(meta)
    new_data = data[:insert_at] + bext + data[insert_at:]
    riff_size = min(len(new_data) - 8, 0xFFFFFFFF)
    new_data = (
        new_data[:4]
        + struct.pack("<I", riff_size)
        + new_data[8:]
    )

    tmp_path = f"{wav_path}.bext.tmp"
    try:
        with open(tmp_path, "wb") as f:
            f.write(new_data)
        os.replace(tmp_path, wav_path)
    except OSError as e:
        logger.warning(f"bext inject: failed to write {wav_path}: {e}")
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False
    logger.info(f"bext chunk injected: {wav_path} (TimeReference={meta.time_reference_samples})")
    return True


def read_bext_chunk(wav_path: str) -> Optional[BextMetadata]:
    """从 WAV 文件解析 bext chunk（独立解析器，供验证与外部读取）。

    Returns:
        BextMetadata；无 bext chunk 或文件不可读时 None。
    """
    try:
        with open(wav_path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None

    for cid, offset, size in _iter_chunks(data):
        if cid != b"bext":
            continue
        body = data[offset:offset + size]
        if len(body) < BEXT_FIXED_SIZE:
            return None
        coding = body[BEXT_FIXED_SIZE:].decode("ascii", errors="replace")
        coding = coding.rstrip("\x00")  # 剥离 word 对齐填充字节
        return BextMetadata(
            description=body[0:256].split(b"\x00")[0].decode("ascii", errors="replace"),
            originator=body[256:288].split(b"\x00")[0].decode("ascii", errors="replace"),
            originator_reference=body[288:320].split(b"\x00")[0].decode("ascii", errors="replace"),
            origination_date=body[320:330].decode("ascii", errors="replace").rstrip("\x00"),
            origination_time=body[330:338].decode("ascii", errors="replace").rstrip("\x00"),
            time_reference_samples=struct.unpack("<Q", body[338:346])[0],
            version=struct.unpack("<H", body[346:348])[0],
            coding_history=coding,
        )
    return None


def is_true_bwf_supported() -> bool:
    """是否支持真正的 BWF（bext/TimeReference/coding history）。

    P0-06：本模块已实现 bext 写入与独立解析，录音 writer 默认注入。
    """
    return True
