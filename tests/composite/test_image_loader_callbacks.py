import threading

import cv2
import numpy as np
import pytest

from src.features.composite.core.image_loader import ImageLoader


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
