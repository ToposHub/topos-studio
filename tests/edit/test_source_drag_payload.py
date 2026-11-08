"""VP-B08：Source 拖拽载荷——有版本 MIME、Out 包含、损坏回退。"""

from __future__ import annotations

import json

import pytest

from src.features.edit.core.source_drag_payload import (
    MIME_TYPE_SOURCE_CLIP,
    PAYLOAD_VERSION,
    SourceDragPayload,
    build_source_drag_payload,
    decode_source_drag_payload,
)

pytestmark = pytest.mark.edit_core


def test_b08_roundtrip_all_intents():
    for intent in ("av", "video", "audio"):
        payload = build_source_drag_payload(
            "asset-1", 10, 49, intent, source_fps=(24000, 1001))
        assert payload is not None
        assert payload.source_length == 40          # Out 包含
        assert decode_source_drag_payload(payload.encode()) == payload
        assert decode_source_drag_payload(
            payload.encode().decode()) == payload   # str 输入同样支持


def test_b08_mime_type_is_versioned_internal():
    assert MIME_TYPE_SOURCE_CLIP == "application/x-topos-source-clip"
    data = build_source_drag_payload("a", 0, 9).encode()
    assert b'"version": 1' in data


def test_b08_builder_rejects_bad_input_with_none():
    assert build_source_drag_payload("", 0, 9) is None
    assert build_source_drag_payload("a", -1, 9) is None
    assert build_source_drag_payload("a", 20, 10) is None   # In > Out
    assert build_source_drag_payload("a", 0, 9, "both") is None
    assert build_source_drag_payload("a", 0, 9, source_fps=(0, 1001)) is None


def test_b08_decode_never_raises_junk_matrix():
    for junk in (b"", b"{not json", b"[]", b'{"version": 2}', None,
                 42, b'{"version":1}', b'{"version":1,"asset_id":""}',
                 b'{"version":1,"asset_id":"a","in_frame":-2,"out_frame":9}',
                 b'{"version":1,"asset_id":"a","in_frame":9,"out_frame":2}',
                 b'{"version":1,"asset_id":"a","in_frame":0,"out_frame":9,'
                 b'"stream_intent":"video+audio"}',
                 b'{"version":1,"asset_id":"a","in_frame":true,'
                 b'"out_frame":9}'):
        assert decode_source_drag_payload(junk) is None, junk


def test_b08_single_frame_range_is_one_frame():
    payload = build_source_drag_payload("a", 7, 7)
    assert payload is not None
    assert payload.source_length == 1               # 一帧范围不为零长度


def test_b08_optional_fps_omitted_when_absent():
    encoded = build_source_drag_payload("a", 0, 9).encode()
    assert b"source_fps_num" not in encoded
    revived = decode_source_drag_payload(encoded)
    assert revived.source_fps_num is None


class _QByteArrayLike:
    """QMimeData.data() 返回的 QByteArray 鸭子件（codec 不 import Qt）。"""

    def __bytes__(self) -> bytes:
        return b'{"version": 1, "asset_id": "a", "in_frame": 3, "out_frame": 9}'


def test_b08_decode_accepts_qbytearray_like():
    from PyQt6.QtCore import QByteArray

    encoded = build_source_drag_payload("a", 3, 9, "audio").encode()
    assert decode_source_drag_payload(QByteArray(encoded)) == \
        decode_source_drag_payload(encoded)
    stub = decode_source_drag_payload(_QByteArrayLike())
    assert stub is not None and stub.asset_id == "a"
    assert decode_source_drag_payload(QByteArray(b"\xff\xfe")) is None


@pytest.mark.parametrize("version", [True, False, 1.0, "1", None])
def test_decode_rejects_coercible_or_missing_integer_version(version: object) -> None:
    data = json.loads(build_source_drag_payload("a", 0, 9).encode())
    data["version"] = version
    assert decode_source_drag_payload(json.dumps(data)) is None


@pytest.mark.parametrize("value", [True, False, 0.0, 1.5, "1", None, [], {}])
@pytest.mark.parametrize("field", ["in_frame", "out_frame"])
def test_builder_and_decoder_require_integer_frames(field: str, value: object) -> None:
    data = {"asset_id": "a", "in_frame": 0, "out_frame": 9}
    data[field] = value
    assert build_source_drag_payload(**data) is None
    assert decode_source_drag_payload(json.dumps({"version": 1, **data})) is None


@pytest.mark.parametrize("asset_id", [True, 1, [], {}, None, "", "   "])
def test_builder_and_decoder_require_nonempty_asset_string(asset_id: object) -> None:
    assert build_source_drag_payload(asset_id, 0, 9) is None
    assert decode_source_drag_payload(json.dumps({
        "version": 1, "asset_id": asset_id, "in_frame": 0, "out_frame": 9,
    })) is None


@pytest.mark.parametrize("value", [True, False, 24.0, "24", None, 0, -1, [], {}])
@pytest.mark.parametrize("field", ["source_fps_num", "source_fps_den"])
def test_explicit_fps_requires_positive_integer_pair(field: str, value: object) -> None:
    data = json.loads(build_source_drag_payload("a", 0, 9, source_fps=(24, 1)).encode())
    data[field] = value
    assert decode_source_drag_payload(json.dumps(data)) is None
    assert build_source_drag_payload(
        "a", 0, 9, source_fps=(data["source_fps_num"], data["source_fps_den"]),
    ) is None


@pytest.mark.parametrize("source_fps", [(), (24,), (24, 1, 1), "24/1", 24, True, {}])
def test_malformed_builder_fps_returns_none_without_coercion(source_fps: object) -> None:
    assert build_source_drag_payload("a", 0, 9, source_fps=source_fps) is None


@pytest.mark.parametrize("field", ["source_fps_num", "source_fps_den"])
def test_partial_fps_pair_rejects_payload(field: str) -> None:
    data = json.loads(build_source_drag_payload("a", 0, 9).encode())
    data[field] = 24
    assert decode_source_drag_payload(json.dumps(data)) is None
