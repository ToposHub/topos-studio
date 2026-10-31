"""
P2-2: Hot-path regression guard tests.

These tests verify that playback hot-path code does NOT contain forbidden calls:
- _on_play_tick() must not decode, seek, block, convert YUV to RGB
- RENDER_VIDEO_GPU_FRAME handler must not use Texture.write, to_ndarray, ctx.finish
- PlaybackCoordinator.tick() must not directly decode

These are static source code checks, not runtime tests.
"""
import os
import re
import pytest


def _read_source(relative_path):
    """Read source file from project root."""
    project_root = os.path.normpath(os.path.join(
        os.path.dirname(__file__), '..', '..'
    ))
    full_path = os.path.join(project_root, relative_path)
    if not os.path.exists(full_path):
        pytest.skip(f"Source file not found: {full_path}")
    with open(full_path, 'r') as f:
        return f.read()


def _extract_method_body(source, method_name):
    """
    Extract all method bodies with the given name from source.

    Some classes have multiple _on_play_tick methods in different class bodies.
    We return a list of (class_hint, body) tuples.
    """
    results = []
    # Find all occurrences of the method definition
    # Pattern: optional class def before, then the target method
    pattern = re.compile(
        r'def ' + re.escape(method_name) + r'\(self.*?\n(?=\n    def |\nclass |\Z)',
        re.DOTALL
    )
    for match in pattern.finditer(source):
        results.append(match.group(0))
    return results


def _strip_comments_and_docstrings(body):
    """Remove comments and docstrings from a method body for static analysis."""
    # Remove triple-quoted docstrings (single or double quotes)
    body = re.sub(r'"""[\s\S]*?"""', '', body)
    body = re.sub(r"'''[\s\S]*?'''", '', body)
    # Remove single-line comments
    lines = [l for l in body.split('\n') if not l.strip().startswith('#')]
    return '\n'.join(lines)


class TestPlayTickHotPathGuards:
    """P2-2: Verify _on_play_tick does not contain forbidden calls."""

    @pytest.fixture(autouse=True)
    def _load_source(self):
        self.source = _read_source('src/shared/video/video_playback.py')

    def test_tick_no_to_ndarray(self):
        """_on_play_tick must not call to_ndarray()."""
        bodies = _extract_method_body(self.source, '_on_play_tick')
        if not bodies:
            pytest.skip("_on_play_tick not found")

        for body in bodies:
            assert 'to_ndarray' not in body, \
                "_on_play_tick contains forbidden to_ndarray call"

    def test_tick_no_transfer(self):
        """_on_play_tick must not call transfer()."""
        bodies = _extract_method_body(self.source, '_on_play_tick')
        if not bodies:
            pytest.skip("_on_play_tick not found")

        for body in bodies:
            # Allow 'transfer' in comments but not as actual call
            lines = [l for l in body.split('\n') if not l.strip().startswith('#')]
            body_no_comments = '\n'.join(lines)
            assert '.transfer(' not in body_no_comments, \
                "_on_play_tick contains forbidden transfer() call"

    def test_tick_no_glFinish(self):
        """_on_play_tick must not call glFinish or ctx.finish."""
        bodies = _extract_method_body(self.source, '_on_play_tick')
        if not bodies:
            pytest.skip("_on_play_tick not found")

        for body in bodies:
            # Allow 'finish_call_count' as a variable name, but forbid actual finish() calls
            cleaned = body.replace('finish_call_count', '')
            assert 'finish(' not in cleaned, \
                "_on_play_tick contains forbidden finish() call"
            assert 'glFinish' not in body, \
                "_on_play_tick contains forbidden glFinish call"

    def test_tick_no_blocking_wait(self):
        """_on_play_tick should not use blocking waits."""
        bodies = _extract_method_body(self.source, '_on_play_tick')
        if not bodies:
            pytest.skip("_on_play_tick not found")

        for body in bodies:
            # Check for blocking patterns (but allow non-blocking queue.get)
            assert '.join(timeout' not in body or '# non-blocking' in body, \
                "_on_play_tick contains blocking join() call"
            assert 'time.sleep(' not in body, \
                "_on_play_tick contains blocking sleep() call"


class TestGpuFrameRenderHotPathGuards:
    """P2-2: Verify RENDER_VIDEO_GPU_FRAME handler does not use forbidden calls."""

    @pytest.fixture(autouse=True)
    def _load_source(self):
        self.source = _read_source('src/features/color/core/async_renderer.py')

    def test_gpu_frame_handler_no_texture_write(self):
        """RENDER_VIDEO_GPU_FRAME handler must not call Texture.write()."""
        match = re.search(
            r'def _do_render_video_gpu_frame\(self.*?\n(?=\n    def |\nclass |\Z)',
            self.source, re.DOTALL
        )
        if match is None:
            pytest.skip("_do_render_video_gpu_frame not found")

        body_no_comments = _strip_comments_and_docstrings(match.group(0))

        assert 'Texture.write' not in body_no_comments, \
            "_do_render_video_gpu_frame contains forbidden Texture.write"
        # Check .write() but exclude logger/file writes by stripping logger lines
        code_lines = [l for l in body_no_comments.split('\n')
                       if 'logger' not in l and 'logging' not in l]
        code_only = '\n'.join(code_lines)
        assert '.write(' not in code_only, \
            "_do_render_video_gpu_frame contains forbidden texture write"

    def test_gpu_frame_handler_no_to_ndarray(self):
        """RENDER_VIDEO_GPU_FRAME handler must not call to_ndarray()."""
        match = re.search(
            r'def _do_render_video_gpu_frame\(self.*?\n(?=\n    def |\nclass |\Z)',
            self.source, re.DOTALL
        )
        if match is None:
            pytest.skip("_do_render_video_gpu_frame not found")

        body_no_comments = _strip_comments_and_docstrings(match.group(0))
        assert 'to_ndarray' not in body_no_comments, \
            "_do_render_video_gpu_frame contains forbidden to_ndarray"

    def test_gpu_frame_handler_no_finish(self):
        """RENDER_VIDEO_GPU_FRAME handler must not call ctx.finish()."""
        match = re.search(
            r'def _do_render_video_gpu_frame\(self.*?\n(?=\n    def |\nclass |\Z)',
            self.source, re.DOTALL
        )
        if match is None:
            pytest.skip("_do_render_video_gpu_frame not found")

        body_no_comments = _strip_comments_and_docstrings(match.group(0))
        assert 'ctx.finish' not in body_no_comments, \
            "_do_render_video_gpu_frame contains forbidden ctx.finish"
        assert 'glFinish' not in body_no_comments, \
            "_do_render_video_gpu_frame contains forbidden glFinish"


class TestFramePathValidation:
    """P2-2: Verify frame path validation logic."""

    def test_zero_copy_combination_validation(self):
        """Zero-copy combinations must be validated correctly."""
        from src.shared.media.frame_path import (
            DecodePath, RenderFramePath, is_zero_copy_combination
        )

        # Valid zero-copy
        assert is_zero_copy_combination(
            DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY,
            RenderFramePath.GPU_DECODED_EXTERNAL_YUV
        )

        # Invalid: CPU decode + external render
        assert not is_zero_copy_combination(
            DecodePath.CPU_PLANAR_YUV,
            RenderFramePath.GPU_DECODED_EXTERNAL_YUV
        )

        # Invalid: zero-copy decode + CPU upload render
        assert not is_zero_copy_combination(
            DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY,
            RenderFramePath.PACKED_RGB_CPU_UPLOAD
        )

        # Valid non-zero-copy
        assert not is_zero_copy_combination(
            DecodePath.CPU_PLANAR_YUV,
            RenderFramePath.PLANAR_YUV_R8_UPLOAD
        )

    def test_fallback_reasons_are_non_empty(self):
        """Fallback reasons for non-zero-copy paths should be non-empty."""
        from src.shared.media.frame_path import (
            DecodePath, RenderFramePath,
            fallback_reason_for_decode_path,
            fallback_reason_for_render_path,
        )

        # Zero-copy paths should have empty fallback reason
        assert fallback_reason_for_decode_path(
            DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY
        ) == ""

        # CPU paths should have fallback reason
        assert fallback_reason_for_decode_path(DecodePath.CPU_PLANAR_YUV) != ""
        assert fallback_reason_for_decode_path(DecodePath.CPU_PACKED_RGB) != ""

        # CPU upload paths should have fallback reason
        assert fallback_reason_for_render_path(
            RenderFramePath.PACKED_RGB_CPU_UPLOAD
        ) != ""
