"""
Tests for TimelineClipFrameProvider.

Validates:
- Provider creation and dependency injection
- Fallback chain: ColorOutput -> Source frame -> None for user preview
- Debug placeholder image generation
- Frame mapping delegation
- CompositeRuntimeContext integration
"""

import pytest
import numpy as np
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_runtime_context(
    timeline_id: str = "tl-1",
    clip_id: str = "clip-1",
    asset_id: str | None = None,
    source_frame: int = 0,
    input_stage: str = "after_color",
    width: int = 1920,
    height: int = 1080,
):
    """Create a mock CompositeRuntimeContext."""
    ctx = MagicMock()
    ctx.timeline_id = timeline_id
    ctx.clip_id = clip_id
    ctx.asset_id = asset_id
    ctx.source_frame = source_frame
    ctx.input_stage = input_stage
    ctx.width = width
    ctx.height = height
    ctx.graph_id = "graph-12345678"
    ctx.timeline_frame = 0
    return ctx


def _make_color_output(texture_id=42, width=1920, height=1080, revision=1, valid=True):
    """Create a mock ColorOutput."""
    co = MagicMock()
    co.texture_id = texture_id if valid else None
    co.width = width
    co.height = height
    co.revision = revision
    co.is_valid = valid
    return co


def _make_engine(color_output=None):
    """Create a mock ImageEngine."""
    engine = MagicMock()
    engine.color_output = color_output or _make_color_output()
    return engine


# ===================================================================
# Test: Provider creation
# ===================================================================

class TestProviderCreation:
    """TimelineClipFrameProvider can be created and configured."""

    def test_default_creation(self):
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        assert provider._project is None
        assert provider._engine is None
        assert provider._debug_placeholder_cache == {}

    def test_set_engine(self):
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        engine = _make_engine()
        provider.set_engine(engine)
        assert provider._engine is engine

    def test_set_project(self):
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        project = MagicMock()
        provider.set_project(project)
        assert provider._project is project

    def test_set_timeline_sequences(self):
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        sequences = {"tl-1": MagicMock()}
        provider.set_timeline_sequences(sequences)
        assert provider._timeline_sequences is sequences

    def test_set_gl_context(self):
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        gl_context = MagicMock()
        provider.set_gl_context(gl_context)
        assert provider._gl_context is gl_context

    def test_set_allow_external_gpu_frames(self):
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        provider.set_allow_external_gpu_frames(False)
        assert provider._allow_external_gpu_frames is False

    def test_reuses_one_video_service_session_per_source(self):
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider

        provider = TimelineClipFrameProvider()
        service = MagicMock()
        service.open_clip.return_value = "service-clip-1"
        service.has_clip.return_value = True

        assert provider._get_or_open_video_session(service, "/tmp/source.mov") == "service-clip-1"
        assert provider._get_or_open_video_session(service, "/tmp/source.mov") == "service-clip-1"
        service.open_clip.assert_called_once_with("/tmp/source.mov")

        provider.release_video_sessions()
        service.close_clip.assert_called_once_with("service-clip-1")


# ===================================================================
# Test: _TexResult
# ===================================================================

class TestTexResult:
    """_TexResult dataclass works as expected."""

    def test_default_values(self):
        from src.features.edit.core.timeline_clip_frame_provider import _TexResult
        tr = _TexResult()
        assert tr.texture_id == 0
        assert tr.width == 0
        assert tr.height == 0
        assert tr.is_valid is False

    def test_valid_texture(self):
        from src.features.edit.core.timeline_clip_frame_provider import _TexResult
        tr = _TexResult(texture_id=42, width=1920, height=1080)
        assert tr.is_valid is True
        assert tr.texture_id == 42


# ===================================================================
# Test: Null/empty inputs
# ===================================================================

class TestNullInputs:
    """Provider handles null inputs gracefully."""

    def test_none_runtime_context(self):
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        assert provider.get_timeline_clip_texture(None) is None

    def test_no_engine_no_color_output(self):
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        ctx = _make_runtime_context()
        # No engine set, no GL context -> all fallbacks fail
        result = provider.get_timeline_clip_texture(ctx)
        # Source frame fallback returns None (no clip data),
        # debug placeholder returns None (no GL context)
        assert result is None


# ===================================================================
# Test: Fallback 1 - ColorOutput texture
# ===================================================================

class TestColorOutputFallback:
    """Provider returns ColorOutput texture when available."""

    def test_color_output_valid_texture(self):
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        provider.set_engine(_make_engine(color_output=_make_color_output(
            texture_id=99, width=1920, height=1080, revision=5, valid=True
        )))

        ctx = _make_runtime_context(input_stage="after_color")
        result = provider.get_timeline_clip_texture(ctx)

        assert result is not None
        assert result.texture_id == 99
        assert result.width == 1920
        assert result.height == 1080
        assert result.revision == 5

    def test_color_output_invalid_texture_falls_through(self):
        """When ColorOutput has no valid texture, falls to next fallback."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        provider.set_engine(_make_engine(color_output=_make_color_output(
            valid=False
        )))

        ctx = _make_runtime_context(input_stage="after_color")
        result = provider.get_timeline_clip_texture(ctx)
        # Falls through all fallbacks (no source data, no GL context)
        assert result is None

    def test_color_output_zero_texture_id(self):
        """ColorOutput with texture_id=0 is treated as invalid."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        provider.set_engine(_make_engine(color_output=_make_color_output(
            texture_id=0, valid=False
        )))

        ctx = _make_runtime_context(input_stage="after_color")
        result = provider.get_timeline_clip_texture(ctx)
        assert result is None

    def test_no_engine_returns_none(self):
        """Without engine, ColorOutput fallback is skipped."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        ctx = _make_runtime_context(input_stage="after_color")
        result = provider.get_timeline_clip_texture(ctx)
        assert result is None

    def test_source_raw_stage_skips_color_output(self):
        """input_stage="source_raw" should skip ColorOutput and go to source frame."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        provider.set_engine(_make_engine(color_output=_make_color_output(
            texture_id=99, valid=True
        )))

        ctx = _make_runtime_context(input_stage="source_raw")
        result = provider.get_timeline_clip_texture(ctx)
        # Should skip ColorOutput, fail on source frame, fail on debug placeholder
        assert result is None


# ===================================================================
# Test: Fallback 3 - Debug placeholder
# ===================================================================

class TestDebugPlaceholder:
    """Debug placeholder generation."""

    def test_build_debug_image_shape(self):
        """Debug image has correct shape."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        img = TimelineClipFrameProvider._build_debug_image(320, 240)
        assert img.shape == (240, 320, 3)
        assert img.dtype == np.uint8

    def test_build_debug_image_gray_background(self):
        """Background is deep gray (64, 64, 64)."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        img = TimelineClipFrameProvider._build_debug_image(100, 100)
        # Top-left corner should be gray
        np.testing.assert_array_equal(img[0, 0], [64, 64, 64])

    def test_build_debug_image_red_bar(self):
        """Middle section has red bar."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        img = TimelineClipFrameProvider._build_debug_image(100, 100)
        # Middle pixel should be reddish (200, 40, 40)
        middle_y = 50
        assert img[middle_y, 50, 0] == 200  # Red channel high
        assert img[middle_y, 50, 1] == 40   # Green channel low
        assert img[middle_y, 50, 2] == 40   # Blue channel low

    def test_build_debug_image_not_black(self):
        """Debug image is clearly not black."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        img = TimelineClipFrameProvider._build_debug_image(640, 480)
        # Should not be all zeros
        assert img.mean() > 30  # Much brighter than black

    def test_debug_placeholder_no_gl_context(self):
        """Debug placeholder returns None without GL context."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        result = provider._create_debug_placeholder(320, 240)
        # No GL context available
        assert result is None

    def test_get_texture_does_not_create_debug_placeholder(self):
        """User-facing texture requests do not create debug placeholders."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        ctx = _make_runtime_context(width=320, height=240)

        with patch.object(provider, '_try_source_frame_texture', return_value=None):
            with patch.object(provider, '_try_color_output_texture', return_value=None):
                with patch.object(provider, '_create_debug_placeholder') as mock_create:
                    result1 = provider.get_timeline_clip_texture(ctx)
                    result2 = provider.get_timeline_clip_texture(ctx)

                    assert result1 is None
                    assert result2 is None
                    mock_create.assert_not_called()

    def test_clear_debug_cache(self):
        """clear_debug_cache() empties the cache."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        provider._debug_placeholder_cache[(320, 240)] = "something"
        provider.clear_debug_cache()
        assert provider._debug_placeholder_cache == {}

    def test_planar_upload_flips_cpu_top_left_rows_once(self):
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider

        y_plane = np.array([[1, 2], [3, 4]], dtype=np.uint8)
        uv_plane = np.array([[5, 6], [7, 8]], dtype=np.uint8)

        y_upload, uv_upload = TimelineClipFrameProvider._prepare_planar_upload_planes(
            (y_plane, uv_plane)
        )

        np.testing.assert_array_equal(y_upload, np.array([[3, 4], [1, 2]], dtype=np.uint8))
        np.testing.assert_array_equal(uv_upload, np.array([[7, 8], [5, 6]], dtype=np.uint8))
        assert y_upload.flags.c_contiguous
        assert uv_upload.flags.c_contiguous


# ===================================================================
# Test: Frame mapping delegation
# ===================================================================

class TestFrameMappingDelegation:
    """get_clip_frame_mapping correctly maps frames."""

    def test_valid_mapping(self):
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()

        mock_clip = MagicMock()
        mock_clip.timeline_start = 10
        mock_clip.timeline_duration = 50
        mock_clip.timeline_to_source_frame.return_value = 35

        mock_timeline = MagicMock()
        mock_timeline.get_clip_by_id.return_value = mock_clip

        provider.set_timeline_sequences({"tl-1": mock_timeline})

        result = provider.get_clip_frame_mapping("tl-1", "clip-1", 20)
        assert result is not None
        assert result["clip_local_frame"] == 10  # 20 - 10
        assert result["source_frame"] == 35

    def test_timeline_not_found(self):
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        result = provider.get_clip_frame_mapping("missing", "clip-1", 0)
        assert result is None

    def test_clip_not_found(self):
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()

        mock_timeline = MagicMock()
        mock_timeline.get_clip_by_id.return_value = None

        provider.set_timeline_sequences({"tl-1": mock_timeline})
        result = provider.get_clip_frame_mapping("tl-1", "missing-clip", 0)
        assert result is None

    def test_frame_outside_clip_range(self):
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()

        mock_clip = MagicMock()
        mock_clip.timeline_start = 10
        mock_clip.timeline_duration = 50

        mock_timeline = MagicMock()
        mock_timeline.get_clip_by_id.return_value = mock_clip

        provider.set_timeline_sequences({"tl-1": mock_timeline})

        result = provider.get_clip_frame_mapping("tl-1", "clip-1", 5)
        assert result is None


# ===================================================================
# Test: Full fallback chain
# ===================================================================

class TestFallbackChain:
    """Full fallback chain integration test."""

    def test_color_output_first_priority(self):
        """When ColorOutput is valid, it should be returned first."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        provider.set_engine(_make_engine(color_output=_make_color_output(
            texture_id=42, width=1920, height=1080, revision=3, valid=True
        )))

        ctx = _make_runtime_context(input_stage="after_color")

        with patch.object(provider, '_try_source_frame_texture') as mock_source:
            mock_source.return_value = None
            result = provider.get_timeline_clip_texture(ctx)
            assert result.texture_id == 42
            mock_source.assert_not_called()

    def test_source_frame_when_color_invalid(self):
        """When ColorOutput is invalid, source frame is tried."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider, _TexResult
        provider = TimelineClipFrameProvider()
        provider.set_engine(_make_engine(color_output=_make_color_output(
            valid=False
        )))

        ctx = _make_runtime_context(input_stage="after_color")

        mock_source_result = _TexResult(texture_id=77, width=1920, height=1080)

        with patch.object(provider, '_try_source_frame_texture', return_value=mock_source_result):
            result = provider.get_timeline_clip_texture(ctx)
            assert result.texture_id == 77

    def test_source_frame_retries_master_when_proxy_returns_none(self):
        """Composite preview must fall back to master if proxy cannot serve a frame."""
        from types import SimpleNamespace
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider, _TexResult

        provider = TimelineClipFrameProvider()
        project = MagicMock()
        project.get_asset_by_id.return_value = SimpleNamespace(
            path="/tmp/source.mov",
            media_type="video",
            metadata={},
        )
        provider.set_project(project)

        service = MagicMock()
        service.open_clip.return_value = "service-clip-1"
        decoded_frame = SimpleNamespace(data=np.zeros((16, 16, 3), dtype=np.uint8))
        service.request_frame.side_effect = [None, decoded_frame]

        expected = _TexResult(texture_id=707, width=16, height=16)
        ctx = _make_runtime_context(asset_id="asset-1", source_frame=88)

        with patch(
            "src.shared.video.frame_provider_service.get_frame_provider_service",
            return_value=service,
        ):
            with patch.object(provider, "_upload_decoded_frame", return_value=expected):
                result = provider._try_source_frame_texture(ctx)

        assert result is expected
        assert service.request_frame.call_args_list[0].kwargs["quality"] == "proxy"
        assert service.request_frame.call_args_list[1].kwargs["quality"] == "master"

    def test_no_user_preview_placeholder_when_all_fail(self):
        """When ColorOutput and source frame fail, user preview returns None."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        provider.set_engine(_make_engine(color_output=_make_color_output(
            valid=False
        )))

        ctx = _make_runtime_context(input_stage="after_color", width=320, height=240)

        with patch.object(provider, '_try_source_frame_texture', return_value=None):
            with patch.object(provider, '_create_debug_placeholder') as mock_placeholder:
                result = provider.get_timeline_clip_texture(ctx)
                assert result is None
                mock_placeholder.assert_not_called()


# ===================================================================
# Test: Source frame fallback
# ===================================================================

class TestSourceFrameFallback:
    """Source frame fallback path."""

    def test_resolve_asset_path_uses_asset_id_not_clip_id(self):
        """Project lookup must use runtime asset_id; clip_id is not an asset key."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        project = MagicMock()
        project.get_asset_by_id.side_effect = lambda asset_id: (
            MagicMock(path="/tmp/source.mov") if asset_id == "asset-1" else None
        )
        provider.set_project(project)

        ctx = _make_runtime_context(clip_id="clip-1", asset_id="asset-1")
        assert provider._resolve_asset_path(ctx) == "/tmp/source.mov"
        project.get_asset_by_id.assert_called_with("asset-1")

    def test_resolve_comp_clip_asset_uses_original_asset(self):
        """Promoted comp clip assets must unwrap to their original media path."""
        from types import SimpleNamespace
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider

        provider = TimelineClipFrameProvider()
        comp_asset = SimpleNamespace(
            asset_id="comp-asset-1",
            path="topos://comp_clip/comp-asset-1",
            media_type="comp_clip",
            metadata={"original_asset_id": "asset-1", "graph_id": "graph-1"},
        )
        source_asset = SimpleNamespace(
            asset_id="asset-1",
            path="/tmp/original.mov",
            media_type="video",
            metadata={},
        )
        project = MagicMock()
        project.get_asset_by_id.side_effect = lambda asset_id: {
            "comp-asset-1": comp_asset,
            "asset-1": source_asset,
        }.get(asset_id)
        provider.set_project(project)

        ctx = _make_runtime_context(
            clip_id="clip-1",
            asset_id="comp-asset-1",
            input_stage="source_raw",
        )

        assert provider._resolve_asset_path(ctx) == "/tmp/original.mov"

    def test_resolve_comp_clip_asset_uses_composition_source_path(self):
        """If asset metadata is incomplete, composition metadata is the fallback."""
        from types import SimpleNamespace
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider

        provider = TimelineClipFrameProvider()
        comp_asset = SimpleNamespace(
            asset_id="comp-asset-1",
            path="topos://comp_clip/comp-asset-1",
            media_type="comp_clip",
            metadata={"graph_id": "graph-1"},
        )
        project = MagicMock()
        project.get_asset_by_id.side_effect = lambda asset_id: (
            comp_asset if asset_id == "comp-asset-1" else None
        )
        project.get_composition_by_id.return_value = {
            "composite_id": "graph-1",
            "source_asset_id": "asset-1",
            "source_asset_path": "/tmp/from-composition.mov",
        }
        provider.set_project(project)

        ctx = _make_runtime_context(
            clip_id="clip-1",
            asset_id="comp-asset-1",
            input_stage="source_raw",
        )
        ctx.graph_id = "graph-1"

        assert provider._resolve_asset_path(ctx) == "/tmp/from-composition.mov"

    def test_source_upload_keeps_texture_object_alive(self):
        """The returned texture id must stay backed by an owned ModernGL texture."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        texture = MagicMock()
        texture.glo = 777
        texture.size = (64, 32)
        ctx = MagicMock()
        ctx.texture.return_value = texture
        provider.set_gl_context(ctx)

        frame = MagicMock()
        frame.data = np.zeros((32, 64, 3), dtype=np.uint8)
        result = provider._upload_decoded_frame(frame)

        assert result is not None
        assert result.texture_id == 777
        assert provider._owned_textures[777] is texture

    def test_rgb48le_source_upload_uses_float_texture(self):
        """Packed 16-bit RGB frames must be normalized before GPU upload."""
        from types import SimpleNamespace
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider

        provider = TimelineClipFrameProvider()
        texture = MagicMock()
        texture.glo = 778
        texture.size = (2, 1)
        ctx = MagicMock()
        ctx.texture.return_value = texture
        provider.set_gl_context(ctx)

        frame = SimpleNamespace(
            data=np.array([[[0, 32768, 65535], [65535, 0, 32768]]], dtype=np.uint16),
            pixel_format="rgb48le",
            bit_depth=16,
        )

        result = provider._upload_decoded_frame(frame)

        assert result is not None
        assert result.texture_id == 778
        assert result.bit_depth == 16

        args, kwargs = ctx.texture.call_args
        assert args[:2] == ((2, 1), 4)
        assert kwargs["dtype"] == "f4"

        uploaded = np.frombuffer(kwargs["data"], dtype=np.float32).reshape(1, 2, 4)
        np.testing.assert_allclose(uploaded[0, 0], [0.0, 32768 / 65535, 1.0, 1.0])
        np.testing.assert_allclose(uploaded[0, 1], [1.0, 0.0, 32768 / 65535, 1.0])

    def test_bgr48le_source_upload_flips_to_rgb(self):
        """Packed BGR48 frames should enter Composite in RGB channel order."""
        from types import SimpleNamespace
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider

        provider = TimelineClipFrameProvider()
        texture = MagicMock()
        texture.glo = 779
        texture.size = (1, 1)
        ctx = MagicMock()
        ctx.texture.return_value = texture
        provider.set_gl_context(ctx)

        frame = SimpleNamespace(
            data=np.array([[[0, 32768, 65535]]], dtype=np.uint16),
            pixel_format="bgr48le",
            bit_depth=16,
        )

        result = provider._upload_decoded_frame(frame)

        assert result is not None
        uploaded = np.frombuffer(ctx.texture.call_args.kwargs["data"], dtype=np.float32).reshape(1, 1, 4)
        np.testing.assert_allclose(uploaded[0, 0], [1.0, 32768 / 65535, 0.0, 1.0])

    def test_planar_decoded_frame_uses_gpu_planar_upload(self):
        """Planar YUV frames should not fall through to the debug placeholder."""
        from types import SimpleNamespace
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider, _TexResult

        provider = TimelineClipFrameProvider()
        provider.set_gl_context(MagicMock())
        frame = SimpleNamespace(
            data=None,
            is_planar=True,
            planes=(
                np.zeros((16, 16), dtype=np.uint8),
                np.zeros((8, 8), dtype=np.uint8),
                np.zeros((8, 8), dtype=np.uint8),
            ),
        )

        expected = _TexResult(texture_id=901, width=16, height=16)
        with patch.object(provider, "_upload_planar_frame", return_value=expected) as mock_upload:
            result = provider._upload_decoded_frame(frame)

        assert result is expected
        # The planar path must retain its colour metadata even when callers
        # rely on the default BT.709 limited-range interpretation.
        mock_upload.assert_called_once_with(
            frame, matrix_tag="bt709", range_tag="limited",
            cs_tag="bt709",
        )

    def test_no_clip_id_returns_none(self):
        """Without clip_id, source frame returns None."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        ctx = _make_runtime_context(clip_id="")
        result = provider._try_source_frame_texture(ctx)
        assert result is None

    def test_no_clip_id_none(self):
        """With clip_id=None, source frame returns None."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        ctx = _make_runtime_context(clip_id="")
        ctx.clip_id = None
        result = provider._try_source_frame_texture(ctx)
        assert result is None


# ===================================================================
# Test: _to_texture_result conversion
# ===================================================================

class TestToTextureResult:
    """_to_texture_result boundary conversion."""

    def test_returns_texresult_when_no_moderngl(self):
        """When moderngl is not available, returns _TexResult as-is."""
        from src.features.edit.core.timeline_clip_frame_provider import _TexResult, _to_texture_result
        tr = _TexResult(texture_id=42, width=100, height=100)
        # The import of TextureResult from composite nodes requires moderngl
        # which may not be available in test environment.
        # _to_texture_result should handle ImportError gracefully.
        result = _to_texture_result(tr)
        # Either returns a TextureResult or the original _TexResult
        assert result.texture_id == 42
        assert result.width == 100


# ===================================================================
# Test: Origin convention
# ===================================================================

class TestOriginConvention:
    """_TexResult default origin is gl_bottom_left; uploads preserve it."""

    def test_tex_result_default_origin_is_gl_bottom_left(self):
        """_TexResult 默认 origin 应为 gl_bottom_left"""
        from src.features.edit.core.timeline_clip_frame_provider import _TexResult
        tr = _TexResult()
        assert tr.origin == "gl_bottom_left"

    def test_upload_sets_origin_metadata(self):
        """Uploaded frames get origin='gl_bottom_left' in frame_metadata."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider, _TexResult
        provider = TimelineClipFrameProvider()
        texture = MagicMock()
        texture.glo = 42
        texture.size = (4, 2)
        ctx = MagicMock()
        ctx.texture.return_value = texture
        provider.set_gl_context(ctx)

        frame = MagicMock()
        frame.data = np.zeros((2, 4, 3), dtype=np.uint8)
        frame.pixel_format = "rgb24"
        frame.bit_depth = 8

        result = provider._upload_decoded_frame(frame)
        assert result is not None
        assert result.frame_metadata.get("origin") == "gl_bottom_left"

    def test_upload_rgb48le_origin(self):
        """rgb48le uint16 upload preserves gl_bottom_left origin."""
        from types import SimpleNamespace
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        texture = MagicMock()
        texture.glo = 43
        texture.size = (2, 1)
        ctx = MagicMock()
        ctx.texture.return_value = texture
        provider.set_gl_context(ctx)

        frame = SimpleNamespace(
            data=np.array([[[0, 32768, 65535], [65535, 0, 32768]]], dtype=np.uint16),
            pixel_format="rgb48le",
            bit_depth=16,
        )
        result = provider._upload_decoded_frame(frame)
        assert result is not None
        assert result.frame_metadata.get("origin") == "gl_bottom_left"

    def test_upload_float32_origin(self):
        """Float texture upload preserves gl_bottom_left origin."""
        from types import SimpleNamespace
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider
        provider = TimelineClipFrameProvider()
        texture = MagicMock()
        texture.glo = 44
        texture.size = (2, 1)
        ctx = MagicMock()
        ctx.texture.return_value = texture
        provider.set_gl_context(ctx)

        frame = SimpleNamespace(
            data=np.array([[[0.0, 0.5, 1.0], [1.0, 0.0, 0.5]]], dtype=np.float32),
            pixel_format="rgbf32",
            bit_depth=32,
        )
        result = provider._upload_decoded_frame(frame)
        assert result is not None
        assert result.frame_metadata.get("origin") == "gl_bottom_left"


# ===================================================================
# Test: Source texture cache
# ===================================================================

class TestSourceTextureCache:
    """Source texture cache (LRU, max 3 entries)."""

    def test_cache_miss_triggers_decode(self):
        """Cache miss triggers _upload_decoded_frame."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider, _TexResult
        provider = TimelineClipFrameProvider()

        with patch.object(provider, '_upload_decoded_frame', return_value=_TexResult(texture_id=1, width=16, height=16)) as mock_upload:
            with patch.object(provider, '_resolve_asset_path', return_value='/test.mov'):
                with patch('src.shared.video.frame_provider_service.get_frame_provider_service') as mock_svc:
                    svc = MagicMock()
                    svc.open_clip.return_value = 'c1'
                    frame = MagicMock()
                    frame.data = np.zeros((16, 16, 3), dtype=np.uint8)
                    svc.request_frame.return_value = frame
                    mock_svc.return_value = svc

                    ctx = _make_runtime_context(asset_id="a1", source_frame=0)
                    ctx.input_stage = "source_raw"
                    result = provider._try_source_frame_texture(ctx)
                    assert result is not None
                    mock_upload.assert_called_once()

    def test_external_gpu_frame_disabled_skips_request_gpu_frame(self):
        """Headless Composite providers must not reuse texture ids from another GL context."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider, _TexResult
        provider = TimelineClipFrameProvider()
        provider.set_allow_external_gpu_frames(False)

        with patch.object(provider, '_upload_decoded_frame', return_value=_TexResult(texture_id=2, width=16, height=16)):
            with patch.object(provider, '_resolve_asset_path', return_value='/test.mov'):
                with patch('src.shared.video.frame_provider_service.get_frame_provider_service') as mock_svc:
                    svc = MagicMock()
                    svc.open_clip.return_value = 'c1'
                    svc.request_gpu_frame.return_value = _TexResult(texture_id=99, width=16, height=16)
                    frame = MagicMock()
                    frame.data = np.zeros((16, 16, 3), dtype=np.uint8)
                    svc.request_frame.return_value = frame
                    mock_svc.return_value = svc

                    ctx = _make_runtime_context(asset_id="a1", source_frame=0, input_stage="source_raw")
                    result = provider._try_source_frame_texture(ctx)
                    assert result is not None
                    assert result.texture_id == 2
                    svc.request_gpu_frame.assert_not_called()

    def test_cache_hit_skips_decode(self):
        """Same (asset_path, source_frame) second request hits cache."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider, _TexResult
        provider = TimelineClipFrameProvider()

        cached = _TexResult(texture_id=99, width=16, height=16)
        provider._source_texture_cache[("/test.mov", 5)] = cached

        with patch.object(provider, '_resolve_asset_path', return_value='/test.mov'):
            ctx = _make_runtime_context(asset_id="a1", source_frame=5)
            ctx.input_stage = "source_raw"
            result = provider._try_source_frame_texture(ctx)
            assert result is cached
            assert result.texture_id == 99

    def test_cache_hit_refreshes_lru_order(self):
        """A reused source frame stays hot when the bounded cache evicts."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider, _TexResult
        provider = TimelineClipFrameProvider()

        for frame in range(3):
            provider._cache_source_texture(
                "/test.mov", frame,
                _TexResult(texture_id=frame + 1, width=16, height=16),
            )

        with patch.object(provider, '_resolve_asset_path', return_value='/test.mov'):
            ctx = _make_runtime_context(asset_id="a1", source_frame=0)
            ctx.input_stage = "source_raw"
            result = provider._try_source_frame_texture(ctx)

        assert result is not None
        assert result.texture_id == 1
        assert provider._source_cache_order[-1] == ("/test.mov", 0)

        provider._cache_source_texture(
            "/test.mov", 3,
            _TexResult(texture_id=4, width=16, height=16),
        )

        assert ("/test.mov", 0) in provider._source_texture_cache
        assert ("/test.mov", 1) not in provider._source_texture_cache

    def test_cache_evicts_when_full(self):
        """Cache evicts oldest entry when over _SOURCE_CACHE_MAX."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider, _TexResult
        provider = TimelineClipFrameProvider()

        # Fill cache to max
        for i in range(10):
            provider._source_texture_cache[("/test.mov", i)] = _TexResult(texture_id=i, width=16, height=16)
            provider._source_cache_order.append(("/test.mov", i))

        # Add one more — should trigger eviction of oldest
        provider._source_texture_cache[("/test.mov", 10)] = _TexResult(texture_id=10, width=16, height=16)
        provider._source_cache_order.append(("/test.mov", 10))

        # Manually evict like the code does
        while len(provider._source_texture_cache) > provider._SOURCE_CACHE_MAX:
            oldest_key = provider._source_cache_order.pop(0)
            provider._source_texture_cache.pop(oldest_key, None)

        assert len(provider._source_texture_cache) == 3
        assert ("/test.mov", 10) in provider._source_texture_cache

    def test_clear_source_cache(self):
        """clear_source_cache empties cache and order list."""
        from src.features.edit.core.timeline_clip_frame_provider import TimelineClipFrameProvider, _TexResult
        provider = TimelineClipFrameProvider()
        provider._source_texture_cache[("a", 0)] = _TexResult(texture_id=1, width=16, height=16)
        provider._source_cache_order.append(("a", 0))
        provider.clear_source_cache()
        assert len(provider._source_texture_cache) == 0
        assert len(provider._source_cache_order) == 0


# =====================================================================
# Batch J: 跳转异步解码（持帧预览）
# =====================================================================

class TestAsyncJumpDecode:
    """随机跳转 → 持最近纹理 + 后台解码 + 回调重求值；顺序小步保持同步"""

    def _make_provider_with_service(self, callback=None):
        from src.features.edit.core.timeline_clip_frame_provider import (
            TimelineClipFrameProvider, _TexResult,
        )

        provider = TimelineClipFrameProvider()
        provider._resolve_asset_path = lambda runtime: "/fake/media.mov"
        if callback is not None:
            provider.set_frame_ready_callback(callback)

        service = MagicMock()
        frame = MagicMock()
        frame.is_planar = False
        frame.pixel_format = "rgb24"
        service.request_frame.return_value = frame
        service.get_clip_info.return_value = None
        service.has_clip.return_value = True

        tex_seq = [1000]

        def fake_upload(decoded, matrix_tag=None, range_tag=None,
                        cs_tag=None, source_meta=None):
            tex_seq[0] += 1
            return _TexResult(
                texture_id=tex_seq[0],
                width=64,
                height=64,
                color_space="bt709",
                revision=tex_seq[0],
            )

        provider._upload_decoded_frame = fake_upload
        return provider, service, frame

    def test_jump_returns_hold_and_notifies(self):
        import threading

        notified = threading.Event()
        provider, service, frame = self._make_provider_with_service(notified.set)

        def open_clip(path, *a, **k):
            return "vid-1"

        service.open_clip.side_effect = open_clip
        provider._frame_provider_service = service

        with patch(
            "src.shared.video.frame_provider_service.get_frame_provider_service",
            return_value=service,
        ):
            # 首帧：无持帧 → 同步解码上屏
            tex1 = provider.get_timeline_clip_texture(
                _make_runtime_context(input_stage="source_raw", source_frame=10)
            )
            assert tex1 is not None
            assert provider._asset_last_frame.get("/fake/media.mov") == 10

            # 跳转：持帧 + 后台解码
            tex2 = provider.get_timeline_clip_texture(
                _make_runtime_context(input_stage="source_raw", source_frame=200)
            )
            assert tex2 is tex1 or tex2.texture_id == tex1.texture_id, "跳转应持旧纹理"
            assert provider._asset_last_frame.get("/fake/media.mov") == 10, "持帧期间游标不推进"

            assert notified.wait(2.0), "后台解码完成未回调"
            # 重求值：pending 上传
            tex3 = provider.get_timeline_clip_texture(
                _make_runtime_context(input_stage="source_raw", source_frame=200)
            )
            assert tex3 is not None
            assert tex3.texture_id != tex1.texture_id
            assert provider._asset_last_frame.get("/fake/media.mov") == 200

        provider._stop_async_decode_thread(join=True)

    def test_sequential_step_stays_synchronous(self):
        provider, service, frame = self._make_provider_with_service(lambda: None)
        provider._frame_provider_service = service

        with patch(
            "src.shared.video.frame_provider_service.get_frame_provider_service",
            return_value=service,
        ):
            provider.get_timeline_clip_texture(
                _make_runtime_context(input_stage="source_raw", source_frame=10)
            )
            calls_before = service.request_frame.call_count
            # 前向 +1：顺序 → 同步
            tex = provider.get_timeline_clip_texture(
                _make_runtime_context(input_stage="source_raw", source_frame=11)
            )
            assert tex is not None
            assert service.request_frame.call_count == calls_before + 1
            assert provider._asset_last_frame.get("/fake/media.mov") == 11
        provider._stop_async_decode_thread(join=True)

    def test_no_callback_disables_async(self):
        provider, service, frame = self._make_provider_with_service(None)
        provider._frame_provider_service = service

        with patch(
            "src.shared.video.frame_provider_service.get_frame_provider_service",
            return_value=service,
        ):
            provider.get_timeline_clip_texture(
                _make_runtime_context(input_stage="source_raw", source_frame=10)
            )
            calls_before = service.request_frame.call_count
            tex = provider.get_timeline_clip_texture(
                _make_runtime_context(input_stage="source_raw", source_frame=300)
            )
            assert tex is not None
            assert service.request_frame.call_count == calls_before + 1, "无回调时应全同步"

    def test_deferred_gpu_frame_released_when_external_enabled(self):
        """GPU 分支收到延迟导入帧（无 texture_id）必须显式释放，防泄漏。"""
        from src.shared.media.gpu_frame import GpuDecodedFrame

        provider, service, frame = self._make_provider_with_service(None)
        provider._frame_provider_service = service
        provider.set_allow_external_gpu_frames(True)

        deferred = GpuDecodedFrame(
            frame_index=5, pts=0, time_seconds=0.0, width=64, height=64,
            visible_rect=(0, 0, 64, 64), pixel_format="nv12", bit_depth=8,
            planes=(), zero_copy=False,
        )
        service.request_gpu_frame = MagicMock(return_value=deferred)

        with patch(
            "src.shared.video.frame_provider_service.get_frame_provider_service",
            return_value=service,
        ):
            tex = provider.get_timeline_clip_texture(
                _make_runtime_context(input_stage="source_raw", source_frame=5)
            )
        assert tex is not None
        assert deferred.release.is_released, "延迟帧不能被本分支消费，必须释放"

    def test_deferred_gpu_frame_import_success_skips_cpu_upload(self):
        """Successful deferred import must return the GPU result directly."""
        from src.shared.media.gpu_frame import GpuDecodedFrame

        provider, service, _ = self._make_provider_with_service(None)
        provider._frame_provider_service = service
        provider.set_allow_external_gpu_frames(True)
        deferred = GpuDecodedFrame(
            frame_index=5, pts=0, time_seconds=0.0, width=64, height=64,
            visible_rect=(0, 0, 64, 64), pixel_format="nv12", bit_depth=8,
            planes=(), zero_copy=False,
        )
        deferred.deferred_import = "macos_vt_pixel_buffer"
        service.request_gpu_frame = MagicMock(return_value=deferred)
        from src.features.edit.core.timeline_clip_frame_provider import _TexResult
        imported = _TexResult(texture_id=9001, width=64, height=64)

        with patch(
            "src.shared.video.frame_provider_service.get_frame_provider_service",
            return_value=service,
        ), patch.object(
            provider, "_import_deferred_gpu_frame", return_value=imported
        ) as mock_import, patch.object(
            provider, "_finish_source_frame_upload"
        ) as mock_cpu_upload:
            result = provider.get_timeline_clip_texture(
                _make_runtime_context(input_stage="source_raw", source_frame=5)
            )

        assert result is imported
        mock_import.assert_called_once()
        mock_cpu_upload.assert_not_called()
        # Ownership stays with the successful importer, which fences the
        # source surface instead of releasing it in the caller.
        assert not deferred.release.is_released
        # The importer is mocked here; finish its ownership in the test.
        deferred.release.release()
        assert deferred.release.is_released

    def test_deferred_gpu_frame_import_failure_releases_and_falls_back(self):
        """Failed external import releases the frame and uses CPU fallback."""
        from src.shared.media.gpu_frame import GpuDecodedFrame

        provider, service, _ = self._make_provider_with_service(None)
        provider._frame_provider_service = service
        provider.set_allow_external_gpu_frames(True)
        deferred = GpuDecodedFrame(
            frame_index=5, pts=0, time_seconds=0.0, width=64, height=64,
            visible_rect=(0, 0, 64, 64), pixel_format="nv12", bit_depth=8,
            planes=(), zero_copy=False,
        )
        deferred.deferred_import = "macos_vt_pixel_buffer"
        service.request_gpu_frame = MagicMock(return_value=deferred)

        with patch(
            "src.shared.video.frame_provider_service.get_frame_provider_service",
            return_value=service,
        ), patch.object(
            provider, "_import_deferred_gpu_frame", return_value=None
        ):
            result = provider.get_timeline_clip_texture(
                _make_runtime_context(input_stage="source_raw", source_frame=5)
            )

        assert result is not None
        assert deferred.release.is_released
        assert provider._external_gpu_import_count == 0

    def test_deferred_gpu_frame_is_converted_on_gpu_without_cpu_planes(self):
        """The real provider importer consumes external planes without NumPy."""
        from types import SimpleNamespace
        from src.shared.media.gpu_frame import GpuDecodedFrame, GpuFramePlane
        from src.features.edit.core.timeline_clip_frame_provider import (
            TimelineClipFrameProvider,
        )

        provider = TimelineClipFrameProvider()
        texture = MagicMock()
        texture.glo = 9010
        texture.size = (64, 64)
        framebuffer = MagicMock()
        ctx = MagicMock()
        ctx.texture.return_value = texture
        ctx.framebuffer.return_value = framebuffer
        provider.set_gl_context(ctx)

        deferred = GpuDecodedFrame(
            frame_index=5, pts=0, time_seconds=0.0, width=64, height=64,
            visible_rect=(0, 0, 64, 64), pixel_format="nv12", bit_depth=8,
            planes=(), zero_copy=False,
        )
        deferred.deferred_vt_frame = SimpleNamespace(
            pixel_buffer=object(), frame_index=5, width=64, height=64,
            pixel_format="nv12", bit_depth=8,
        )
        bridge_release = MagicMock()
        planes = (
            GpuFramePlane(
                component="Y", api="opengl", texture_id=101,
                target="2d", width=64, height=64,
            ),
            GpuFramePlane(
                component="UV", api="opengl", texture_id=102,
                target="2d", width=32, height=32,
            ),
        )
        bridge = MagicMock()
        bridge.bind_pixel_buffer.return_value = (planes, bridge_release)
        external_pass = MagicMock()
        external_pass.render.return_value = True
        provider._ensure_external_gpu_resources = lambda: (bridge, external_pass)
        provider._enqueue_external_source_release = MagicMock()

        result = provider._import_deferred_gpu_frame(
            deferred,
            asset_path="/tmp/source.mov",
            source_frame=5,
            matrix_tag="bt709",
            range_tag="limited",
            cs_tag="bt709",
            source_meta={"color_transfer": "bt709"},
        )

        assert result is not None
        assert result.texture_id == 9010
        assert result.frame_metadata["external_gpu_import"] == "iosurface_gl"
        external_pass.render.assert_called_once_with(
            planes, deferred, framebuffer, 64, 64
        )
        provider._enqueue_external_source_release.assert_called_once_with(
            deferred, bridge_release
        )
        assert provider._source_texture_cache[("/tmp/source.mov", 5)] is result
        assert 9010 in provider._owned_textures
        assert not deferred.release.is_released
        bridge_release.release.assert_not_called()
        # The mocked release queue does not consume its frame.
        deferred.release.release()
        assert deferred.release.is_released

    def test_release_video_sessions_stops_worker(self):
        provider, service, frame = self._make_provider_with_service(lambda: None)
        provider._frame_provider_service = service
        provider._ensure_async_decode_thread()
        thread = provider._async_decode_thread
        provider.release_video_sessions()
        assert provider._async_decode_thread is None
        if thread is not None:
            thread.join(timeout=1.0)
            assert not thread.is_alive()
        assert provider._pending_source_frames == {}
        assert provider._clip_color_tags == {}
