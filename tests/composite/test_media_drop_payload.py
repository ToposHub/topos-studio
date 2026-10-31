"""Tests for the media-drop payload bridge (plan §5)."""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.media_drop_payload import (
    MediaDropPayload, parse_media_pool_mime, resolve_drop_payloads,
    MIME_TYPE_MEDIA_POOL_ASSETS, MEDIA_TYPE_IMAGE, MEDIA_TYPE_VIDEO,
    MIME_TYPE_TOPOS_MEDIA_ASSET, MEDIA_TYPE_SEQUENCE,
    MEDIA_TYPE_MODEL3D, MEDIA_TYPE_TO_NODE_TYPE,
)

pytestmark = pytest.mark.composite_core


class _FakeMimeData:
    """Minimal QMimeData stand-in for pure-python tests (no PyQt needed)."""

    def __init__(self, formats: dict = None, text: str = "", urls=None):
        self._formats = formats or {}
        self._text = text
        self._urls = urls or []

    def data(self, mime: str):
        data = self._formats.get(mime)
        if data is None:
            return None

        class _QBA:
            """QByteArray lookalike: supports bytes() and size()."""

            def __init__(self, payload: bytes):
                self._payload = payload

            def size(self):
                return len(self._payload)

            def __bytes__(self):
                return self._payload

        return _QBA(data.encode("utf-8"))

    def text(self):
        return self._text


class TestParseMime(unittest.TestCase):

    def test_custom_mime_round_trip(self):
        mime = _FakeMimeData(formats={
            MIME_TYPE_MEDIA_POOL_ASSETS: '["a1", "a2"]',
        })
        self.assertEqual(parse_media_pool_mime(mime), ["a1", "a2"])

    def test_text_fallback(self):
        mime = _FakeMimeData(text="a1,a2,a3")
        self.assertEqual(parse_media_pool_mime(mime), ["a1", "a2", "a3"])

    def test_literal_plan_mime_accepts_single_payload(self):
        mime = _FakeMimeData(formats={
            MIME_TYPE_TOPOS_MEDIA_ASSET: '{"asset_id": "a1", "path": "/x.png"}',
        })
        self.assertEqual(parse_media_pool_mime(mime), ["a1"])

    def test_custom_mime_deduplicates_ids(self):
        mime = _FakeMimeData(formats={
            MIME_TYPE_MEDIA_POOL_ASSETS: '["a1", "a1", "a2"]',
        })
        self.assertEqual(parse_media_pool_mime(mime), ["a1", "a2"])

    def test_path_like_text_not_treated_as_ids(self):
        # text with slashes should not be misread as id list
        mime = _FakeMimeData(text="/media/clip.mov")
        self.assertEqual(parse_media_pool_mime(mime), [])

    def test_empty_mime(self):
        self.assertEqual(parse_media_pool_mime(_FakeMimeData()), [])
        self.assertEqual(parse_media_pool_mime(None), [])


class TestResolvePayloads(unittest.TestCase):

    def test_resolve_via_asset_lookup(self):
        mime = _FakeMimeData(formats={
            MIME_TYPE_MEDIA_POOL_ASSETS: '["a1", "a2"]',
        })
        table = {
            "a1": {"asset_id": "a1", "path": "/m/img.png", "media_type": "image"},
            "a2": {"asset_id": "a2", "path": "/m/clip.mov", "media_type": "video",
                   "duration": 100, "width": 1920, "height": 1080},
        }
        payloads = resolve_drop_payloads(mime, asset_lookup=lambda i: table.get(i))
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0].media_type, MEDIA_TYPE_IMAGE)
        self.assertEqual(payloads[0].node_type, "image_input")
        self.assertEqual(payloads[1].media_type, MEDIA_TYPE_VIDEO)
        self.assertEqual(payloads[1].node_type, "video_frame")
        self.assertEqual(payloads[1].duration, 100)

    def test_resolve_skips_unknown_ids(self):
        mime = _FakeMimeData(formats={
            MIME_TYPE_MEDIA_POOL_ASSETS: '["a1", "ghost"]',
        })
        payloads = resolve_drop_payloads(
            mime, asset_lookup=lambda i: {"a1": {"path": "/x.png"}} if i == "a1" else None)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0].asset_id, "a1")

    def test_media_type_inference_from_extension(self):
        mime = _FakeMimeData(formats={
            MIME_TYPE_MEDIA_POOL_ASSETS: '["a1"]',
        })
        payloads = resolve_drop_payloads(
            mime, asset_lookup=lambda i: {"path": "/m/seq.0001.exr"})
        self.assertEqual(payloads[0].media_type, MEDIA_TYPE_IMAGE)
        self.assertEqual(payloads[0].node_type, "image_input")

    def test_display_name_from_path(self):
        p = MediaDropPayload(asset_id="a", path="/media/My Clip.mov")
        self.assertEqual(p.display_name, "My Clip")

    def test_model_asset_resolves_to_model3d_source(self):
        mime = _FakeMimeData(formats={
            MIME_TYPE_MEDIA_POOL_ASSETS: '["model-1"]',
        })
        payloads = resolve_drop_payloads(
            mime,
            asset_lookup=lambda _: {
                "asset_id": "model-1",
                "path": "/media/character.glb",
                "media_type": "model",
                "display_name": "Character",
                "fps": 30.0,
            },
        )
        self.assertEqual(payloads[0].media_type, MEDIA_TYPE_MODEL3D)
        self.assertEqual(payloads[0].node_type, "model3d")
        self.assertEqual(payloads[0].display_name, "Character")
        self.assertEqual(payloads[0].extras["fps"], 30.0)

    def test_model_extension_is_inferred_without_asset_type(self):
        mime = _FakeMimeData(formats={
            MIME_TYPE_MEDIA_POOL_ASSETS: '["model-1"]',
        })
        payloads = resolve_drop_payloads(
            mime, asset_lookup=lambda _: {"path": "/media/character.fbx"})
        self.assertEqual(payloads[0].media_type, MEDIA_TYPE_MODEL3D)
        self.assertEqual(payloads[0].node_type, "model3d")


class TestNodeTypeMapping(unittest.TestCase):

    def test_all_layer_media_types_map_to_source_nodes(self):
        for mt in (MEDIA_TYPE_IMAGE, MEDIA_TYPE_VIDEO, MEDIA_TYPE_SEQUENCE,
                   MEDIA_TYPE_MODEL3D):
            self.assertIn(mt, MEDIA_TYPE_TO_NODE_TYPE)


if __name__ == "__main__":
    unittest.main()
