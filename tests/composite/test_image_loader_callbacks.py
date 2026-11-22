import threading

import cv2
import numpy as np
import pytest

from src.features.composite.core.image_loader import ImageLoader, decode_image_file


pytestmark = pytest.mark.composite_core


def test_image_loader_direct_callback_without_qt_event_loop(tmp_path):
    image_path = tmp_path / "plate.png"
    pixels = np.full((8, 8, 3), 127, dtype=np.uint8)
    assert cv2.imwrite(str(image_path), pixels)

    decoded_event = threading.Event()
    decoded_results = []

    loader = ImageLoader(
        on_decoded=lambda node_id, decoded: (
            decoded_results.append((node_id, decoded.width, decoded.height)),
            decoded_event.set(),
        ),
        on_failed=lambda *args: (
            decoded_results.append(args),
            decoded_event.set(),
        ),
    )

    try:
        loader.request_decode("image_node", str(image_path))
        assert decoded_event.wait(1.0)
        assert decoded_results == [("image_node", 8, 8)]
    finally:
        loader.cancel_all()


@pytest.mark.parametrize("suffix, version", [(".psd", 1), (".psb", 2)])
def test_photoshop_decoding_preserves_alpha_and_depth(tmp_path, suffix, version):
    psd_tools = pytest.importorskip("psd_tools")
    path = tmp_path / f"plate{suffix}"
    doc = psd_tools.PSDImage.new("RGBA", (1, 1), color=(1.0, 0.5, 0.5, 0.5), depth=16)
    doc._record.header.version = version
    doc.save(path)

    decoded = decode_image_file(str(path))
    assert decoded is not None and decoded.has_alpha
    assert (decoded.width, decoded.height, decoded.components, decoded.bit_depth) == (1, 1, 4, "uint16")
    assert decoded.color_space_hint == "srgb"
    np.testing.assert_allclose(decoded.data[0, 0], [1, 0, 0, 32768 / 65535], atol=3 / 65535)
