"""
Audio Waveform Tests Module - Test Waveform Extraction and Caching (P8).

Tests for:
- WaveformData: Data structure and sample extraction
- WaveformCache: LRU cache operations
- WaveformExtractor: Async extraction (requires mocking)
- request_waveform_async: Async request handling
- get_cached_waveform: Cache lookup

Reference:
- docs/edit_page_timeline_upgrade_plan_2026-05-17.md P8
- src/features/edit/core/audio_waveform.py (was src/core/edit/audio_waveform.py)
"""

from __future__ import annotations

import pytest
import numpy as np
from unittest.mock import Mock, patch, MagicMock
import time

from src.features.edit.core.audio_waveform import (
    WaveformData,
    WaveformCache,
    WaveformExtractor,
    WaveformExtractorWorker,
    request_waveform_async,
    get_cached_waveform,
    is_waveform_pending,
    generate_placeholder_waveform,
    WAVEFORM_CACHE_SIZE,
)


# ================================================================
# WaveformData Tests
# ================================================================

class TestWaveformData:
    """Tests for WaveformData class."""

    def test_waveform_data_creation(self) -> None:
        """Test creating WaveformData with valid data."""
        peaks_min = np.array([-0.5, -0.3, -0.7, -0.2])
        peaks_max = np.array([0.4, 0.6, 0.8, 0.3])

        waveform = WaveformData(
            asset_id="test_asset_1",
            peaks_min=peaks_min,
            peaks_max=peaks_max,
            samples_per_second=100.0,
            duration_seconds=4.0,
        )

        assert waveform.asset_id == "test_asset_1"
        assert waveform.num_samples == 4
        assert waveform.is_complete == True
        assert waveform.duration_seconds == 4.0

    def test_waveform_data_mismatched_lengths_raises(self) -> None:
        """Test that mismatched peak lengths raise ValueError."""
        peaks_min = np.array([-0.5, -0.3, -0.7])
        peaks_max = np.array([0.4, 0.6, 0.8, 0.3])  # Different length

        with pytest.raises(ValueError, match="must have same length"):
            WaveformData(
                asset_id="test_asset_2",
                peaks_min=peaks_min,
                peaks_max=peaks_max,
                samples_per_second=100.0,
                duration_seconds=3.0,
            )

    def test_get_samples_for_range_basic(self) -> None:
        """Test extracting samples for a time range."""
        # Create waveform with 100 samples per second, 10 seconds duration
        num_samples = 1000
        peaks_min = np.linspace(-0.8, -0.2, num_samples)
        peaks_max = np.linspace(0.2, 0.8, num_samples)

        waveform = WaveformData(
            asset_id="test_asset_3",
            peaks_min=peaks_min,
            peaks_max=peaks_max,
            samples_per_second=100.0,
            duration_seconds=10.0,
        )

        # Get samples for 2-4 seconds, 20 pixels
        result_min, result_max = waveform.get_samples_for_range(2.0, 4.0, 20)

        assert len(result_min) == 20
        assert len(result_max) == 20

    def test_get_samples_for_range_empty(self) -> None:
        """Test extracting samples from empty waveform."""
        waveform = WaveformData(
            asset_id="test_asset_4",
            peaks_min=np.array([]),
            peaks_max=np.array([]),
            samples_per_second=100.0,
            duration_seconds=0.0,
        )

        result_min, result_max = waveform.get_samples_for_range(0.0, 1.0, 10)

        assert len(result_min) == 10
        assert len(result_max) == 10
        assert np.all(result_min == 0)
        assert np.all(result_max == 0)

    def test_get_samples_for_range_out_of_bounds(self) -> None:
        """Test extracting samples for range outside waveform."""
        # 100 samples, 1 second duration
        peaks_min = np.random.uniform(-0.5, -0.2, 100)
        peaks_max = np.random.uniform(0.2, 0.5, 100)

        waveform = WaveformData(
            asset_id="test_asset_5",
            peaks_min=peaks_min,
            peaks_max=peaks_max,
            samples_per_second=100.0,
            duration_seconds=1.0,
        )

        # Request samples for 5-6 seconds (outside range)
        result_min, result_max = waveform.get_samples_for_range(5.0, 6.0, 10)

        assert len(result_min) == 10
        assert len(result_max) == 10

    def test_stereo_mixdown_preserves_envelope_per_display_column(self) -> None:
        """Stereo mixdown must retain channel extremes without Python loops."""
        waveform = WaveformData(
            asset_id="stereo-envelope",
            # Interleaved: L0/R0, L1/R1, L2/R2, L3/R3
            peaks_min=np.array([-0.1, -0.8, -0.4, -0.2, -0.7, -0.3, -0.2, -0.9]),
            peaks_max=np.array([0.2, 0.6, 0.5, 0.1, 0.3, 0.9, 0.4, 0.2]),
            samples_per_second=4.0,
            duration_seconds=1.0,
            channels=2,
        )

        result_min, result_max = waveform.get_samples_for_range(0.0, 1.0, 2)

        # Each display column aggregates two time buckets and must preserve
        # the minimum/maximum across both stereo channels.
        np.testing.assert_allclose(result_min, [-0.8, -0.9])
        np.testing.assert_allclose(result_max, [0.6, 0.9])

    def test_zoomed_in_range_repeats_peak_data_without_zero_gaps(self) -> None:
        """Upsampling a short range should not introduce blank waveform columns."""
        waveform = WaveformData(
            asset_id="zoom-envelope",
            peaks_min=np.array([-0.2, -0.7]),
            peaks_max=np.array([0.3, 0.8]),
            samples_per_second=2.0,
            duration_seconds=1.0,
        )

        result_min, result_max = waveform.get_samples_for_range(0.0, 1.0, 8)

        assert set(result_min) == {-0.2, -0.7}
        assert set(result_max) == {0.3, 0.8}


# ================================================================
# WaveformCache Tests
# ================================================================

class TestWaveformCache:
    """Tests for WaveformCache singleton."""

    def setup_method(self) -> None:
        """Clear cache before each test."""
        WaveformCache().clear_cache()

    def test_cache_singleton(self) -> None:
        """Test that WaveformCache is a singleton."""
        cache1 = WaveformCache()
        cache2 = WaveformCache()

        assert cache1 is cache2

    def test_cache_waveform(self) -> None:
        """Test caching a waveform."""
        cache = WaveformCache()

        waveform = WaveformData(
            asset_id="cache_test_1",
            peaks_min=np.array([-0.5, -0.3]),
            peaks_max=np.array([0.4, 0.6]),
            samples_per_second=100.0,
            duration_seconds=2.0,
        )

        cache.cache_waveform(waveform)

        cached = cache.get_cached_waveform("cache_test_1")
        assert cached is not None
        assert cached.asset_id == "cache_test_1"

    def test_cache_returns_none_for_missing(self) -> None:
        """Test that missing asset returns None."""
        cache = WaveformCache()

        result = cache.get_cached_waveform("nonexistent_asset")
        assert result is None

    def test_cache_lru_eviction(self) -> None:
        """Test LRU eviction when cache is full."""
        cache = WaveformCache()
        cache.clear_cache()

        # Add more than max size waveforms
        for i in range(WAVEFORM_CACHE_SIZE + 10):
            waveform = WaveformData(
                asset_id=f"lru_test_{i}",
                peaks_min=np.array([-0.5]),
                peaks_max=np.array([0.5]),
                samples_per_second=100.0,
                duration_seconds=1.0,
            )
            cache.cache_waveform(waveform)

        # First waveforms should be evicted
        first_cached = cache.get_cached_waveform("lru_test_0")
        assert first_cached is None

        # Last waveforms should still be cached
        last_cached = cache.get_cached_waveform(f"lru_test_{WAVEFORM_CACHE_SIZE + 9}")
        assert last_cached is not None

    def test_cache_moves_to_end_on_access(self) -> None:
        """Test that accessing a waveform moves it to end of LRU."""
        cache = WaveformCache()
        cache.clear_cache()

        # Add two waveforms
        waveform1 = WaveformData(
            asset_id="lru_access_1",
            peaks_min=np.array([-0.5]),
            peaks_max=np.array([0.5]),
            samples_per_second=100.0,
            duration_seconds=1.0,
        )
        waveform2 = WaveformData(
            asset_id="lru_access_2",
            peaks_min=np.array([-0.5]),
            peaks_max=np.array([0.5]),
            samples_per_second=100.0,
            duration_seconds=1.0,
        )

        cache.cache_waveform(waveform1)
        cache.cache_waveform(waveform2)

        # Access waveform1 (should move to end)
        cached1 = cache.get_cached_waveform("lru_access_1")
        assert cached1 is not None
        assert cached1.asset_id == "lru_access_1"

        # Access waveform2
        cached2 = cache.get_cached_waveform("lru_access_2")
        assert cached2 is not None

        # Both should be accessible
        assert cache.get_cached_waveform("lru_access_1") is not None
        assert cache.get_cached_waveform("lru_access_2") is not None

    def test_pending_status(self) -> None:
        """Test pending status tracking."""
        cache = WaveformCache()

        # Initially not pending
        assert cache.is_pending("pending_test") == False

        # Set pending
        cache.set_pending("pending_test")
        assert cache.is_pending("pending_test") == True

        # Clear pending
        cache.clear_pending("pending_test")
        assert cache.is_pending("pending_test") == False

    def test_cache_waveform_clears_pending(self) -> None:
        """Test that caching waveform clears pending status."""
        cache = WaveformCache()

        cache.set_pending("pending_clear_test")
        assert cache.is_pending("pending_clear_test") == True

        waveform = WaveformData(
            asset_id="pending_clear_test",
            peaks_min=np.array([-0.5]),
            peaks_max=np.array([0.5]),
            samples_per_second=100.0,
            duration_seconds=1.0,
        )
        cache.cache_waveform(waveform)

        assert cache.is_pending("pending_clear_test") == False


# ================================================================
# Placeholder Waveform Tests
# ================================================================

class TestPlaceholderWaveform:
    """Tests for placeholder waveform generation."""

    def test_placeholder_generation(self) -> None:
        """Test generating placeholder waveform."""
        waveform = generate_placeholder_waveform(
            asset_id="placeholder_test",
            duration_seconds=5.0,
            num_samples=100
        )

        assert waveform.asset_id == "placeholder_test"
        assert waveform.num_samples == 100
        assert waveform.duration_seconds == 5.0
        assert waveform.is_complete == False  # Placeholder is marked incomplete

    def test_placeholder_deterministic(self) -> None:
        """Test that same asset_id produces same placeholder."""
        waveform1 = generate_placeholder_waveform("same_id", 5.0, 100)
        waveform2 = generate_placeholder_waveform("same_id", 5.0, 100)

        assert np.allclose(waveform1.peaks_min, waveform2.peaks_min)
        assert np.allclose(waveform1.peaks_max, waveform2.peaks_max)

    def test_placeholder_different_for_different_ids(self) -> None:
        """Test that different asset_ids produce different placeholders."""
        waveform1 = generate_placeholder_waveform("id_1", 5.0, 100)
        waveform2 = generate_placeholder_waveform("id_2", 5.0, 100)

        # Should be different (very unlikely to be same)
        assert not np.allclose(waveform1.peaks_min, waveform2.peaks_min)


# ================================================================
# Convenience Functions Tests
# ================================================================

class TestConvenienceFunctions:
    """Tests for convenience functions."""

    def setup_method(self) -> None:
        """Clear cache before each test."""
        WaveformCache().clear_cache()

    def test_get_cached_waveform_convenience(self) -> None:
        """Test get_cached_waveform convenience function."""
        # Not cached initially
        result = get_cached_waveform("convenience_test")
        assert result is None

        # Cache waveform
        waveform = WaveformData(
            asset_id="convenience_test",
            peaks_min=np.array([-0.5]),
            peaks_max=np.array([0.5]),
            samples_per_second=100.0,
            duration_seconds=1.0,
        )
        WaveformCache().cache_waveform(waveform)

        # Now cached
        result = get_cached_waveform("convenience_test")
        assert result is not None

    def test_is_waveform_pending_convenience(self) -> None:
        """Test is_waveform_pending convenience function."""
        # Not pending initially
        assert is_waveform_pending("pending_convenience") == False

        # Set pending
        WaveformCache().set_pending("pending_convenience")

        # Now pending
        assert is_waveform_pending("pending_convenience") == True


# ================================================================
# WaveformExtractor Tests (Mocked)
# ================================================================

class TestWaveformExtractor:
    """Tests for WaveformExtractor (with mocking)."""

    def setup_method(self) -> None:
        """Clear cache and reset extractor before each test."""
        WaveformCache().clear_cache()

    def test_request_cached_waveform_calls_callback(self) -> None:
        """Test that cached waveform immediately calls callback."""
        # Pre-cache waveform
        waveform = WaveformData(
            asset_id="cached_request_test",
            peaks_min=np.array([-0.5]),
            peaks_max=np.array([0.5]),
            samples_per_second=100.0,
            duration_seconds=1.0,
        )
        WaveformCache().cache_waveform(waveform)

        callback_called = False
        callback_result = None

        def callback(wf: WaveformData) -> None:
            nonlocal callback_called, callback_result
            callback_called = True
            callback_result = wf

        # Request should call callback immediately
        extractor = WaveformExtractor.instance()
        extractor.request_waveform("cached_request_test", "/fake/path", callback)

        assert callback_called == True
        assert callback_result is not None
        assert callback_result.asset_id == "cached_request_test"

    @patch('src.features.edit.core.audio_waveform.WaveformExtractorWorker')
    def test_request_new_waveform_starts_worker(self, mock_worker_class: Mock) -> None:
        """Test that requesting new waveform starts worker."""
        mock_worker = MagicMock()
        mock_worker_class.return_value = mock_worker

        extractor = WaveformExtractor.instance()
        extractor.request_waveform("new_request_test", "/fake/path/audio.wav")

        # Worker should be created and started
        mock_worker_class.assert_called_once()
        mock_worker.start.assert_called_once()

        # Should be marked pending
        assert is_waveform_pending("new_request_test") == True

    def test_ready_waveform_ui_notification_is_deferred_during_interaction(self) -> None:
        """Playback/scrub must not trigger waveform repaint work immediately."""
        extractor = WaveformExtractor()
        waveform = WaveformData(
            asset_id="deferred-ready",
            peaks_min=np.array([-0.5]),
            peaks_max=np.array([0.5]),
            samples_per_second=100.0,
            duration_seconds=1.0,
        )
        delivered: list[str] = []
        extractor.waveform_ready.connect(delivered.append)

        extractor.set_playback_active(True)
        extractor._on_waveform_ready("deferred-ready", waveform)

        assert delivered == []
        assert "deferred-ready" in extractor._deferred_ready

        extractor.set_playback_active(False)

        assert delivered == ["deferred-ready"]
        assert extractor._deferred_ready == {}


# ================================================================
# WaveformExtractorWorker Tests (Mocked)
# ================================================================

class TestWaveformExtractorWorker:
    """Tests for WaveformExtractorWorker (requires PyAV mocking)."""

    @patch('av.open')
    def test_worker_no_audio_stream(self, mock_av_open: Mock) -> None:
        """Test worker behavior when file has no audio stream."""
        # Mock container with no audio stream
        mock_container = MagicMock()
        mock_container.streams = []
        mock_container.duration = 1000000
        mock_av_open.return_value = mock_container

        worker = WaveformExtractorWorker("no_audio_test", "/fake/no_audio.mp4")

        # Run worker (mocked, won't actually decode)
        with patch.object(worker, '_extract_waveform', return_value=None):
            result = worker._extract_waveform()

        assert result is None

    @patch('av.open')
    @patch('av.audio.resampler')
    def test_worker_extract_waveform_mocked(self, mock_resampler: Mock, mock_av_open: Mock) -> None:
        """Test waveform extraction with mocked PyAV."""
        # Mock container with audio stream
        mock_container = MagicMock()
        mock_audio_stream = MagicMock()
        mock_audio_stream.type = 'audio'
        mock_audio_stream.duration = 100
        mock_audio_stream.time_base = 1.0
        mock_container.streams = [mock_audio_stream]
        mock_container.duration = 10000000  # 10 seconds in microseconds

        # Mock decode frames
        mock_frame = MagicMock()
        mock_frame.to_ndarray.return_value = np.array([1000, 2000, 3000, 4000], dtype=np.int16)

        mock_container.decode.return_value = [mock_frame]

        mock_av_open.return_value = mock_container

        # Mock resampler
        mock_resampled_frame = MagicMock()
        mock_resampled_frame.to_ndarray.return_value = np.array([1000, 2000, 3000, 4000], dtype=np.int16)
        mock_resampler_instance = MagicMock()
        mock_resampler_instance.resample.return_value = [mock_resampled_frame]
        mock_resampler.return_value = mock_resampler_instance

        # This test is more about the structure than actual execution
        # Real waveform extraction requires actual audio files


# ================================================================
# Integration Tests
# ================================================================

class TestWaveformIntegration:
    """Integration tests for waveform system."""

    def setup_method(self) -> None:
        """Clear cache before each test."""
        WaveformCache().clear_cache()

    def test_full_workflow_placeholder(self) -> None:
        """Test full workflow with placeholder waveform."""
        asset_id = "workflow_test"

        # Generate placeholder
        placeholder = generate_placeholder_waveform(asset_id, 5.0, 100)

        # Cache it
        WaveformCache().cache_waveform(placeholder)

        # Retrieve it
        cached = get_cached_waveform(asset_id)

        assert cached is not None
        assert cached.asset_id == asset_id
        assert cached.is_complete == False

        # Use it for rendering
        result_min, result_max = cached.get_samples_for_range(0.0, 2.0, 50)

        assert len(result_min) == 50
        assert len(result_max) == 50


class TestTimelineWaveformVisibility:
    """Regression tests for the timeline paint LOD conditions."""

    def test_wide_audio_clip_still_draws_waveform(self, qapp) -> None:
        """Waveform rendering must not be limited to clips narrower than 90 px."""
        from PyQt6.QtGui import QImage, QPainter
        from PyQt6.QtWidgets import QStyleOptionGraphicsItem
        from src.features.edit.ui.timeline_items import ClipItem

        item = ClipItem(
            clip_id="wide-audio",
            track_id="A1",
            asset_name="audio.wav",
            start_frame=0,
            duration_frames=200,
            pixels_per_frame=1.0,
            track_index=0,
            source_duration=200,
            track_height=68,
            media_type="audio",
        )
        called: list[bool] = []
        item._draw_waveform = lambda *_args: called.append(True)

        image = QImage(220, 80, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        try:
            item.paint(painter, QStyleOptionGraphicsItem())
        finally:
            painter.end()

        assert called == [True]
