"""Small, dependency-free implementation of the Tangent TIPC/TUBE wire format.

The managed Tangent API uses a length-prefixed TCP stream.  Integers and floats
are big-endian, and each framed payload starts with a 32-bit command ID.  This
module deliberately contains no Qt code so framing and decoding can be tested
without starting the application.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import struct
from typing import Final


FRAME_HEADER_SIZE: Final[int] = 4
MAX_FRAME_SIZE: Final[int] = 1024 * 1024


class FromHubCommand(IntEnum):
    """Commands sent from Tangent Hub to a managed application."""

    INITIATE_COMMS = 0x01
    PARAMETER_CHANGE = 0x02
    PARAMETER_RESET = 0x03
    PARAMETER_VALUE_REQUEST = 0x04
    ACTION_ON = 0x08
    ACTION_OFF = 0x0B


class ToHubCommand(IntEnum):
    """Commands sent from a managed application to Tangent Hub."""

    APPLICATION_DEFINITION = 0x81
    PARAMETER_VALUE = 0x82
    ALL_CHANGE = 0x84


class PanelType(IntEnum):
    """Panel type IDs used by the Tangent managed API."""

    ELEMENT_TK = 0x0C
    ELEMENT_MF = 0x0D
    ELEMENT_KB = 0x0E
    ELEMENT_BT = 0x0F
    RIPPLE = 0x11


@dataclass(frozen=True)
class PanelInfo:
    """A panel configured in Tangent Hub's panel list."""

    panel_type: int
    panel_id: int


@dataclass(frozen=True)
class TangentCommand:
    """Decoded command with a typed tuple payload.

    Payload shapes are documented by the command enum:

    * ``INITIATE_COMMS``: ``(protocol_revision, tuple[PanelInfo, ...])``
    * ``PARAMETER_CHANGE``: ``(parameter_id, increment)``
    * ``PARAMETER_RESET`` / ``PARAMETER_VALUE_REQUEST``: ``(parameter_id,)``
    * ``ACTION_ON`` / ``ACTION_OFF``: ``(action_id,)``
    """

    command: int
    payload: tuple[object, ...]


def _pack_u32(value: int) -> bytes:
    if not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"uint32 out of range: {value!r}")
    return struct.pack(">I", value)


def _pack_f32(value: float) -> bytes:
    if not isinstance(value, (int, float)):
        raise ValueError(f"float32 requires a number: {value!r}")
    try:
        return struct.pack(">f", float(value))
    except OverflowError as exc:
        # AUD-32803: values beyond the float32 range made struct.pack raise
        # OverflowError, escaping the documented ValueError contract of the
        # other packers here — and propagating uncaught out of
        # send_parameter_value into the calling (UI) thread. Re-raise as the
        # documented error type, chained for the original stack.
        raise ValueError(f"float32 out of range: {value!r}") from exc


def _read_u32(data: bytes, offset: int) -> tuple[int, int]:
    if offset < 0 or offset + 4 > len(data):
        raise ValueError("truncated Tangent uint32")
    return struct.unpack_from(">I", data, offset)[0], offset + 4


def _read_f32(data: bytes, offset: int) -> tuple[float, int]:
    if offset < 0 or offset + 4 > len(data):
        raise ValueError("truncated Tangent float32")
    return struct.unpack_from(">f", data, offset)[0], offset + 4


def _pack_string(value: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"Tangent string requires str: {value!r}")
    encoded = value.encode("utf-8")
    return _pack_u32(len(encoded)) + encoded


def frame_message(payload: bytes) -> bytes:
    """Return a complete length-prefixed message for the Hub socket."""

    if not isinstance(payload, bytes):
        raise TypeError("Tangent payload must be bytes")
    if not 0 < len(payload) <= MAX_FRAME_SIZE:
        raise ValueError(f"invalid Tangent frame size: {len(payload)}")
    return _pack_u32(len(payload)) + payload


def encode_application_definition(
    application_name: str,
    system_path: str,
    user_path: str | None = None,
) -> bytes:
    """Encode the managed application's XML definition announcement."""

    payload = (
        _pack_u32(ToHubCommand.APPLICATION_DEFINITION)
        + _pack_string(application_name)
        + _pack_string(system_path)
        + _pack_string(user_path or "")
    )
    return frame_message(payload)


def encode_parameter_value(
    parameter_id: int,
    value: float,
    at_default: bool = False,
) -> bytes:
    """Encode an absolute parameter value update."""

    payload = (
        _pack_u32(ToHubCommand.PARAMETER_VALUE)
        + _pack_u32(parameter_id)
        + _pack_f32(value)
        + _pack_u32(1 if at_default else 0)
    )
    return frame_message(payload)


def encode_all_change() -> bytes:
    """Ask Hub to request the values of active software controls."""

    return frame_message(_pack_u32(ToHubCommand.ALL_CHANGE))


def decode_message(payload: bytes) -> TangentCommand:
    """Decode one unframed Tangent command payload.

    The Hub sends one command per frame in normal operation.  Keeping this
    function frame-oriented makes malformed or truncated data fail locally and
    keeps the socket worker small.
    """

    if not isinstance(payload, bytes):
        raise TypeError("Tangent payload must be bytes")
    command_id, offset = _read_u32(payload, 0)

    if command_id == FromHubCommand.INITIATE_COMMS:
        protocol_revision, offset = _read_u32(payload, offset)
        panel_count, offset = _read_u32(payload, offset)
        if panel_count > 256:
            raise ValueError(f"unreasonable Tangent panel count: {panel_count}")
        panels: list[PanelInfo] = []
        for _ in range(panel_count):
            panel_type, offset = _read_u32(payload, offset)
            panel_id, offset = _read_u32(payload, offset)
            panels.append(PanelInfo(panel_type=panel_type, panel_id=panel_id))
        if offset != len(payload):
            raise ValueError("unexpected bytes after Tangent initiate command")
        return TangentCommand(
            command=command_id,
            payload=(protocol_revision, tuple(panels)),
        )

    if command_id == FromHubCommand.PARAMETER_CHANGE:
        parameter_id, offset = _read_u32(payload, offset)
        increment, offset = _read_f32(payload, offset)
        # Some Hub/Mapper versions append revision-specific metadata to this
        # command.  The frame boundary already isolates the command, and the
        # application only needs the stable ID + increment fields, so ignore
        # such trailing fields instead of dropping the control-surface link.
        return TangentCommand(
            command=command_id,
            payload=(parameter_id, increment),
        )

    if command_id in (
        FromHubCommand.PARAMETER_RESET,
        FromHubCommand.PARAMETER_VALUE_REQUEST,
        FromHubCommand.ACTION_ON,
        FromHubCommand.ACTION_OFF,
    ):
        value, offset = _read_u32(payload, offset)
        # Tangent Hub versions may append revision-specific metadata to reset,
        # value-request, and action commands too.  The first uint32 is the
        # stable field needed by Topos; the frame boundary safely contains the
        # extension, so do not tear down an otherwise healthy session.
        return TangentCommand(command=command_id, payload=(value,))

    # Hub can send menu/mode/transport notifications that this first color
    # integration does not need. Keep the connection alive and let the client
    # ignore them instead of treating a valid future protocol extension as a
    # broken socket.
    return TangentCommand(command=command_id, payload=())
