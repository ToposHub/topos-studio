from __future__ import annotations

import struct

from src.shared.control_surfaces.tangent.protocol import (
    FromHubCommand,
    PanelInfo,
    PanelType,
    ToHubCommand,
    decode_message,
    encode_application_definition,
    encode_parameter_value,
    frame_message,
)


def _unframe(message: bytes) -> bytes:
    length = int.from_bytes(message[:4], "big")
    assert length == len(message) - 4
    return message[4:]


def test_frame_message_uses_big_endian_length_prefix() -> None:
    assert frame_message(b"abc") == b"\x00\x00\x00\x03abc"


def test_decode_initiate_comms_with_ripple() -> None:
    payload = struct.pack(
        ">IIIII",
        int(FromHubCommand.INITIATE_COMMS),
        4,
        1,
        int(PanelType.RIPPLE),
        1234,
    )

    message = decode_message(payload)

    assert message.command == FromHubCommand.INITIATE_COMMS
    assert message.payload == (4, (PanelInfo(int(PanelType.RIPPLE), 1234),))


def test_encode_application_definition_and_parameter_value() -> None:
    definition = _unframe(
        encode_application_definition("Topos Color", "/tmp/tangent", None)
    )
    assert definition[:4] == struct.pack(">I", int(ToHubCommand.APPLICATION_DEFINITION))

    offset = 4
    app_len = int.from_bytes(definition[offset : offset + 4], "big")
    offset += 4
    assert definition[offset : offset + app_len] == b"Topos Color"
    offset += app_len
    path_len = int.from_bytes(definition[offset : offset + 4], "big")
    offset += 4
    assert definition[offset : offset + path_len] == b"/tmp/tangent"

    value = _unframe(encode_parameter_value(0x00310021, 12.5, at_default=False))
    assert value[:4] == struct.pack(">I", int(ToHubCommand.PARAMETER_VALUE))
    assert value[4:8] == struct.pack(">I", 0x00310021)
    assert value[8:12] == struct.pack(">f", 12.5)
    assert value[12:16] == struct.pack(">I", 0)


def test_decode_parameter_change_allows_extension_metadata() -> None:
    payload = struct.pack(
        ">IIfI", int(FromHubCommand.PARAMETER_CHANGE), 0x00310021, 2.5, 7
    )

    message = decode_message(payload)

    assert message.command == FromHubCommand.PARAMETER_CHANGE
    assert message.payload == (0x00310021, 2.5)


def test_decode_scalar_commands_allow_extension_metadata() -> None:
    payload = struct.pack(">II", int(FromHubCommand.PARAMETER_RESET), 1) + b"x"

    message = decode_message(payload)

    assert message.command == FromHubCommand.PARAMETER_RESET
    assert message.payload == (1,)
