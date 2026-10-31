"""Contract tests for render-thread-owned composite export routing."""

from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from src.features.composite.export_provider import CompositeExportProvider


def test_provider_prefers_live_composite_render_thread():
    image = np.zeros((2, 3, 3), dtype=np.float32)
    internal = SimpleNamespace(
        image=image,
        error=None,
        used_tiling=False,
        tile_count=0,
        render_time_ms=1.5,
    )
    render_thread = SimpleNamespace(render_export=Mock(return_value=internal))

    result = CompositeExportProvider().render_full_res(
        snapshot=object(),
        color_image=image,
        render_thread=render_thread,
    )

    render_thread.render_export.assert_called_once()
    assert result.image is image
    assert result.error is None
    assert result.render_time_ms == 1.5
