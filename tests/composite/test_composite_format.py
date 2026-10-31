"""
Tests for ImageFormat — core spatial data type.

Covers:
- Construction and field defaults
- Immutability (frozen dataclass)
- Serialization round-trip (to_dict / from_dict)
- Format presets (HD 1080, UHD 4K, etc.)
- resized() utility
- full_bbox property
- Pixel aspect and origin
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.format import ImageFormat, BBox, FORMAT_PRESETS

import pytest
pytestmark = pytest.mark.composite_core


class TestImageFormatConstruction:
    def test_required_fields(self):
        fmt = ImageFormat(width=1920, height=1080)
        assert fmt.width == 1920
        assert fmt.height == 1080

    def test_default_pixel_aspect(self):
        fmt = ImageFormat(width=100, height=100)
        assert fmt.pixel_aspect == 1.0

    def test_default_name(self):
        fmt = ImageFormat(width=100, height=100)
        assert fmt.name == ""

    def test_default_origin(self):
        fmt = ImageFormat(width=100, height=100)
        assert fmt.origin_x == 0
        assert fmt.origin_y == 0

    def test_custom_fields(self):
        fmt = ImageFormat(
            width=720, height=486,
            pixel_aspect=0.91, name="SD NTSC",
            origin_x=10, origin_y=20,
        )
        assert fmt.pixel_aspect == 0.91
        assert fmt.name == "SD NTSC"
        assert fmt.origin_x == 10
        assert fmt.origin_y == 20


class TestImageFormatImmutability:
    def test_frozen(self):
        fmt = ImageFormat(width=100, height=100)
        with pytest.raises(AttributeError):
            fmt.width = 200


class TestImageFormatSerialization:
    def test_roundtrip(self):
        fmt = ImageFormat(width=3840, height=2160, pixel_aspect=1.5, name="Wide",
                          origin_x=5, origin_y=10)
        data = fmt.to_dict()
        fmt2 = ImageFormat.from_dict(data)
        assert fmt2.width == 3840
        assert fmt2.height == 2160
        assert fmt2.pixel_aspect == 1.5
        assert fmt2.name == "Wide"
        assert fmt2.origin_x == 5
        assert fmt2.origin_y == 10

    def test_roundtrip_minimal(self):
        fmt = ImageFormat(width=100, height=100)
        data = fmt.to_dict()
        fmt2 = ImageFormat.from_dict(data)
        assert fmt2.width == 100
        assert fmt2.pixel_aspect == 1.0
        assert fmt2.name == ""

    def test_from_dict_missing_fields(self):
        data = {"width": 640}
        fmt = ImageFormat.from_dict(data)
        assert fmt.width == 640
        assert fmt.height == 0  # default from dataclass
        assert fmt.pixel_aspect == 1.0

    def test_from_dict_empty(self):
        fmt = ImageFormat.from_dict({})
        assert fmt.width == 0
        assert fmt.height == 0

    def test_to_dict_keys(self):
        fmt = ImageFormat(width=1920, height=1080, name="HD")
        d = fmt.to_dict()
        expected_keys = {"width", "height", "pixel_aspect", "name", "origin_x", "origin_y"}
        assert set(d.keys()) == expected_keys


class TestImageFormatResized:
    def test_resized_keeps_other_fields(self):
        fmt = ImageFormat(width=1920, height=1080, pixel_aspect=1.5,
                          name="Original", origin_x=5, origin_y=10)
        r = fmt.resized(1280, 720)
        assert r.width == 1280
        assert r.height == 720
        assert r.pixel_aspect == 1.5
        assert r.origin_x == 5
        assert r.origin_y == 10

    def test_resized_clears_name(self):
        fmt = ImageFormat(width=1920, height=1080, name="HD 1080")
        r = fmt.resized(1280, 720)
        assert r.name == ""  # resized formats are no longer the named preset


class TestImageFormatFullBbox:
    def test_full_bbox(self):
        fmt = ImageFormat(width=1920, height=1080)
        bbox = fmt.full_bbox
        assert bbox.x == 0
        assert bbox.y == 0
        assert bbox.width == 1920
        assert bbox.height == 1080


class TestFormatPresets:
    def test_preset_count(self):
        assert len(FORMAT_PRESETS) >= 8

    def test_common_presets_exist(self):
        for name in ["HD 720", "HD 1080", "UHD 4K", "DCI 4K", "UHD 8K"]:
            assert name in FORMAT_PRESETS

    def test_hd1080(self):
        fmt = FORMAT_PRESETS["HD 1080"]
        assert fmt.width == 1920
        assert fmt.height == 1080
        assert fmt.pixel_aspect == 1.0
        assert fmt.name == "HD 1080"

    def test_uhd4k(self):
        fmt = FORMAT_PRESETS["UHD 4K"]
        assert fmt.width == 3840
        assert fmt.height == 2160

    def test_dci4k(self):
        fmt = FORMAT_PRESETS["DCI 4K"]
        assert fmt.width == 4096
        assert fmt.height == 2160

    def test_uhd8k(self):
        fmt = FORMAT_PRESETS["UHD 8K"]
        assert fmt.width == 7680
        assert fmt.height == 4320

    def test_anamorphic_pixel_aspect(self):
        fmt = FORMAT_PRESETS["HD 1080 Anamorphic"]
        assert fmt.pixel_aspect == 2.0

    def test_sd_ntsc_pixel_aspect(self):
        fmt = FORMAT_PRESETS["SD NTSC"]
        assert fmt.width == 720
        assert fmt.height == 486
        assert fmt.pixel_aspect == 0.91

    def test_square_presets(self):
        for name in ["Square 1024", "Square 2048", "Square 4096"]:
            fmt = FORMAT_PRESETS[name]
            assert fmt.width == fmt.height

    def test_presets_are_frozen(self):
        fmt = FORMAT_PRESETS["HD 1080"]
        with pytest.raises(AttributeError):
            fmt.width = 999

    def test_preset_serialization_roundtrip(self):
        """Every preset should round-trip through serialization."""
        for name, fmt in FORMAT_PRESETS.items():
            data = fmt.to_dict()
            fmt2 = ImageFormat.from_dict(data)
            assert fmt2 == fmt, f"Preset '{name}' failed round-trip"
