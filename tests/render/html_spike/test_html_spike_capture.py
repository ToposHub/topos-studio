"""W0-A spike: determinism + capture-backend helpers (no browser needed).

Tests the pure-function determinism layer and the blank-frame detector that
the real-browser test relies on to classify offscreen grab() failures.
"""

from __future__ import annotations

import pytest

from src.features.teaching_animation.render.html_renderer_spike import (
    CaptureEvidence,
    CaptureFailureCode,
    CaptureSurface,
    CapturedFrame,
    build_cache_key,
    is_uniform_frame,
    frame_hashes_equal,
    summarize_out_of_order,
    summarize_repetition,
)


def _frame(data: bytes) -> CapturedFrame:
    surface = CaptureSurface(2, 2, 1.0)
    return CapturedFrame(data=data, surface=surface, evidence=CaptureEvidence(
        backend_id="test", backend_build="test/0", capture_method="t",
        viewport=(2, 2), dpr=1.0,
    ))


class TestUniformFrameDetector:
    def test_all_white_detected_as_uniform(self) -> None:
        data = bytes([255, 255, 255, 255]) * (4 * 4)
        uniform, sample = is_uniform_frame(data, 4, 4)
        assert uniform is True
        assert sample == (255, 255, 255, 255)

    def test_all_black_detected_as_uniform(self) -> None:
        data = bytes([0, 0, 0, 255]) * (4 * 4)
        uniform, sample = is_uniform_frame(data, 4, 4)
        assert uniform is True
        assert sample == (0, 0, 0, 255)

    def test_mixed_frame_not_uniform(self) -> None:
        data = bytearray()
        for i in range(16):
            data += bytes([i, 255 - i, 100, 255])
        uniform, sample = is_uniform_frame(bytes(data), 4, 4)
        assert uniform is False
        assert sample is None

    def test_short_data_not_uniform(self) -> None:
        uniform, sample = is_uniform_frame(b"\x00" * 4, 8, 8)
        assert uniform is False
        assert sample is None

    def test_stride_visits_across_scanlines(self) -> None:
        # A frame uniform except one pixel in the bottom region.
        data = bytearray([10, 20, 30, 255]) * (8 * 8)
        data[(8 * 7 + 4) * 4:(8 * 7 + 4) * 4 + 4] = bytes([200, 200, 200, 255])
        uniform, _ = is_uniform_frame(bytes(data), 8, 8)
        assert uniform is False


class TestCapturedFrameValidation:
    def test_wrong_data_length_rejected(self) -> None:
        with pytest.raises(ValueError):
            _frame(b"\x00" * 10)  # 2x2 needs 16 bytes

    def test_content_hash_stable(self) -> None:
        data = bytes(range(16))
        assert _frame(data).content_hash == _frame(data).content_hash


class TestDeterminismHelpers:
    def test_repetition_identical(self) -> None:
        data = bytes(range(16))
        captures = tuple(_frame(data) for _ in range(5))
        report = summarize_repetition(captures)
        assert report.same_frame_repeats_identical is True
        assert len(report.observed_hashes) == 5
        assert len(set(report.observed_hashes)) == 1

    def test_repetition_drift_detected(self) -> None:
        captures = (_frame(bytes([v] * 16)) for v in (1, 1, 2, 1, 1))
        report = summarize_repetition(tuple(captures))
        assert report.same_frame_repeats_identical is False

    def test_out_of_order_matches_ordered(self) -> None:
        frames = {0: _frame(bytes([0] * 16)), 5: _frame(bytes([5] * 16)), 9: _frame(bytes([9] * 16))}
        report = summarize_out_of_order(frames, dict(frames))
        assert report.out_of_order_identical is True

    def test_out_of_order_mismatch_detected(self) -> None:
        ordered = {0: _frame(bytes([0] * 16)), 5: _frame(bytes([5] * 16))}
        shuffled = {0: _frame(bytes([0] * 16)), 5: _frame(bytes([99] * 16))}
        report = summarize_out_of_order(ordered, shuffled)
        assert report.out_of_order_identical is False

    def test_frame_hashes_equal_requires_same_surface(self) -> None:
        data = bytes(range(16))
        a = _frame(data)
        b = CapturedFrame(
            data=data,
            surface=CaptureSurface(4, 1, 1.0),
            evidence=a.evidence,
        )
        assert frame_hashes_equal(a, b) is False


class TestCacheKey:
    def test_all_pixel_affecting_inputs_participate(self) -> None:
        base = dict(
            backend_id="qt", backend_build="v1", source_hash="s", frame=10,
            fps_num=24, fps_den=1, width=1920, height=1080, dpr=1.0,
            font_set_hash="f" * 64, asset_set_hash="a" * 64,
        )
        key0 = build_cache_key(**base)
        # Changing any pixel-affecting input must change the key.
        for field in ("backend_build", "source_hash", "frame", "fps_num", "fps_den",
                      "width", "height", "dpr", "font_set_hash", "asset_set_hash"):
            modified = dict(base)
            modified[field] = "DIFFERENT" if isinstance(base[field], str) else base[field] + 1
            assert build_cache_key(**modified) != key0, f"key insensitive to {field}"


def test_capture_clock_uses_stable_epoch_and_owns_page_autoplay() -> None:
    """Fresh per-frame pages must not re-randomize Date.now/page timers."""
    from src.features.teaching_animation.render.html_renderer_spike.playwright_backend import (
        _CLOCK_SHIM, _SEEK_BRIDGE,
    )

    assert "const wall0 = 1700000000000;" in _CLOCK_SHIM
    assert "pageAutoplay" in _CLOCK_SHIM
    assert "suppressPageAutoplay" in _CLOCK_SHIM
    assert "window.__toposClock.suppressPageAutoplay()" in _SEEK_BRIDGE
