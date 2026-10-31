"""Tests that the planar YUV upload path configures the GPU uploader's matrix
and signal range from the source video's color metadata (P1 fix).

Previously ``_upload_planar_frame`` never called ``set_yuv_matrix()`` /
``set_color_range()`` on the YUVGPUUploader, so every BT.2020 HDR, full-range,
and BT.601 SD source was YUV->RGB-decoded with the default BT.709 + limited
range — wrong colors / wrong luminance for anything but standard HD video.

These tests inject a recording fake uploader + fake GL context and assert the
right matrix/range is applied BEFORE the render step, across the metadata
combinations that matter.
"""
import numpy as np
import pytest

from src.features.edit.core.timeline_clip_frame_provider import (
    TimelineClipFrameProvider,
)


# ── Fakes ──────────────────────────────────────────────────────────────

class _FakeTex:
    def __init__(self):
        self.glo = 1234
        self.size = (4, 4)
        self.filter = None
        self.repeat_x = None
        self.repeat_y = None
    def release(self):
        pass


class _FakeFBO:
    def use(self): pass
    def release(self): pass


class _FakeCtx:
    """Minimal ModernGL-context stand-in covering the calls in the upload path."""
    LINEAR = 0x2601
    def texture(self, size, components, dtype=None, data=None):
        return _FakeTex()
    def framebuffer(self, color_attachments):
        return _FakeFBO()
    def clear(self, *a, **k): pass


class _FakeUploader:
    """Records matrix/range calls; upload/render are no-ops returning sentinels."""
    MATRIX_BT709 = 0
    MATRIX_BT2020 = 1
    MATRIX_BT601 = 2
    RANGE_LIMITED = 0
    RANGE_FULL = 1

    def __init__(self, ctx, pool_size=5, dtype='f2'):
        self.matrix_calls = []
        self.range_calls = []
        self.render_called = False

    def set_yuv_matrix(self, m):
        self.matrix_calls.append(m)
    def set_color_range(self, r):
        self.range_calls.append(r)

    def upload_nv12(self, *a, **k):
        return (1, 2)
    def upload_p010(self, *a, **k):
        return (1, 2)
    def upload_yuv444(self, *a, **k):
        return (1, 2, 3)
    def upload_yuv422(self, *a, **k):
        return (1, 2, 3)
    def upload_yuv420(self, *a, **k):
        return (1, 2, 3)
    def render_nv12_to_rgb(self, *a, **k):
        self.render_called = True
    def render_yuv_to_rgb(self, *a, **k):
        self.render_called = True


class _FakeFrame:
    """A planar YUV420 frame (3 planes)."""
    def __init__(self, pixel_format="yuv420p", bit_depth=8):
        self.pixel_format = pixel_format
        self.bit_depth = bit_depth
        self.width = 4
        self.height = 4
        self.frame_index = 0
        # 3 planes (Y full-res, U/V quarter-res for 4:2:0).
        self.planes = [
            np.zeros((4, 4), dtype=np.uint8),
            np.zeros((2, 2), dtype=np.uint8),
            np.zeros((2, 2), dtype=np.uint8),
        ]
        self.is_planar = True
        self.data = None  # forces the planar path in _upload_decoded_frame


def _make_provider():
    """Build a provider with a fake GL context + fake uploader pre-installed."""
    p = TimelineClipFrameProvider.__new__(TimelineClipFrameProvider)
    p._gl_context = _FakeCtx()
    p._yuv_uploader = _FakeUploader(_FakeCtx())
    p._owned_textures = {}
    p._last_source_texture_id = None
    return p


# ── Tests: matrix/range applied from source metadata ──────────────────

class TestPlanarYUVMatrixRangeFromMetadata:
    """The upload path must set the uploader's matrix + range before render."""

    def test_bt2020_matrix_applied(self, monkeypatch):
        """BT.2020-gamut source → uploader gets MATRIX_BT2020 before render."""
        monkeypatch.setattr(
            "src.shared.video.yuv_gpu_upload.YUVGPUUploader", _FakeUploader
        )
        p = _make_provider()
        result = p._upload_planar_frame(_FakeFrame(), matrix_tag="bt2020",
                                        range_tag="limited")
        assert result is not None, "upload must succeed with the fakes"
        up = p._yuv_uploader
        assert up.render_called, "render step must run"
        assert _FakeUploader.MATRIX_BT2020 in up.matrix_calls
        assert _FakeUploader.RANGE_LIMITED in up.range_calls

    def test_bt601_matrix_applied(self, monkeypatch):
        """BT.601 SD source → uploader gets MATRIX_BT601."""
        monkeypatch.setattr(
            "src.shared.video.yuv_gpu_upload.YUVGPUUploader", _FakeUploader
        )
        p = _make_provider()
        p._upload_planar_frame(_FakeFrame(), matrix_tag="bt601",
                               range_tag="limited")
        assert _FakeUploader.MATRIX_BT601 in p._yuv_uploader.matrix_calls

    def test_full_range_applied(self, monkeypatch):
        """Full-range (PC/JPEG) source → uploader gets RANGE_FULL."""
        monkeypatch.setattr(
            "src.shared.video.yuv_gpu_upload.YUVGPUUploader", _FakeUploader
        )
        p = _make_provider()
        p._upload_planar_frame(_FakeFrame(), matrix_tag="bt709",
                               range_tag="full")
        assert _FakeUploader.RANGE_FULL in p._yuv_uploader.range_calls

    def test_bt709_limited_is_the_default(self, monkeypatch):
        """Default (no metadata) → BT.709 + limited, matching legacy behaviour."""
        monkeypatch.setattr(
            "src.shared.video.yuv_gpu_upload.YUVGPUUploader", _FakeUploader
        )
        p = _make_provider()
        p._upload_planar_frame(_FakeFrame(), matrix_tag="bt709",
                               range_tag="limited")
        up = p._yuv_uploader
        assert up.matrix_calls[-1] == _FakeUploader.MATRIX_BT709
        assert up.range_calls[-1] == _FakeUploader.RANGE_LIMITED

    def test_matrix_set_before_render(self, monkeypatch):
        """Matrix/range MUST be configured before render_*_to_rgb runs.

        The uploader reads them at render time (uniforms set in render_*), so
        they must be applied before that call. We verify by recording the call
        order on the fake.
        """
        monkeypatch.setattr(
            "src.shared.video.yuv_gpu_upload.YUVGPUUploader", _FakeUploader
        )

        order = []

        class _OrderingUploader(_FakeUploader):
            def set_yuv_matrix(self, m):
                order.append(("matrix", m)); super().set_yuv_matrix(m)
            def set_color_range(self, r):
                order.append(("range", r)); super().set_color_range(r)
            def render_nv12_to_rgb(self, *a, **k):
                order.append(("render", None)); super().render_nv12_to_rgb(*a, **k)
            def render_yuv_to_rgb(self, *a, **k):
                order.append(("render", None)); super().render_yuv_to_rgb(*a, **k)

        monkeypatch.setattr(
            "src.shared.video.yuv_gpu_upload.YUVGPUUploader", _OrderingUploader
        )
        p = _make_provider()
        p._yuv_uploader = _OrderingUploader(_FakeCtx())
        p._upload_planar_frame(_FakeFrame(), matrix_tag="bt2020",
                               range_tag="full")

        # matrix + range appear before any render in the recorded order.
        matrix_idx = next(i for i, (k, _) in enumerate(order) if k == "matrix")
        range_idx = next(i for i, (k, _) in enumerate(order) if k == "range")
        render_idx = next(i for i, (k, _) in enumerate(order) if k == "render")
        assert matrix_idx < render_idx, "matrix must be set before render"
        assert range_idx < render_idx, "range must be set before render"

    def test_rgb_planar_skips_matrix_config(self, monkeypatch):
        """rgb_planar mode carries no YUV matrix — must not call set_yuv_matrix."""
        monkeypatch.setattr(
            "src.shared.video.yuv_gpu_upload.YUVGPUUploader", _FakeUploader
        )
        p = _make_provider()
        # gbrp = planar RGB; _classify_planar_upload_mode returns "rgb_planar".
        p._upload_planar_frame(_FakeFrame(pixel_format="gbrp"),
                               matrix_tag="bt2020", range_tag="full")
        assert p._yuv_uploader.matrix_calls == [], (
            "rgb_planar mode must not apply a YUV matrix"
        )
